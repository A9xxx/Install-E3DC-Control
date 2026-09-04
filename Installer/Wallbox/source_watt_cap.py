"""Sitzungsgebundene Quellen-Wattdeckel für Wallboxen.

Dieses Modul entscheidet weder Ladeleistung noch Hardwarebefehle. Es
versiegelt ausschließlich einen bereits berechneten ladepunktspezifischen
Quellendeckel und prüft später, ob derselbe Regelzyklus, dieselbe Stecksession,
derselbe Live-Snapshot und dasselbe Wallbox-Statussample noch gelten.

Der Vertrag ist bewusst vom finalen Treiber-Sollstrom getrennt: Nachgelagerte
Fahrzeug-, Phasen- und Hardwaregrenzen dürfen den Ausgang weiter senken. Für
den Energie-Wächter ist jedoch der unveränderliche PV-/Speicher-Deckel die
fachliche Obergrenze. Ein fehlender Nachweis bleibt ``None`` und wird niemals
als 0 W interpretiert.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Mapping, Optional, Sequence, Tuple

from .modes import (
    MODE_BASE,
    MODE_BATTERY_DEPARTURE,
    MODE_CURVE,
    MODE_OFF,
    MODE_PRICE,
    MODE_TARGET,
    normalize_wb_mode,
)


CAP_SCHEMA = "wallbox_session_source_watt_cap_v1"
STATUS_SAMPLE_SCHEMA = "wallbox_status_sample_binding_v1"
MEASUREMENT_SCHEMA = "wallbox_measured_power_evidence_v1"

_VALID_CAP_BASES = frozenset({
    "group_allocation_target",
    "single_wallbox_budget",
    "unique_running_storage_group_cap",
    "unmanaged_running_reservation",
})
_SEALED_FIELDS = (
    "schema",
    "wb_id",
    "plug_session_id",
    "session_context_key",
    "cycle_token",
    "live_snapshot_id",
    "live_sample_ts",
    "live_file_revision_ns",
    "status_sample_key",
    "status_sample_ts",
    "status_sample_seq",
    "budget_revision",
    "decision_generation",
    "public_mode",
    "source_class",
    "eligible_budget_tiers",
    "source_binding_key",
    "cap_basis",
    "cap_w",
    "source_ceiling_w",
    "authorized_phases",
    "group_budget_w",
    "allocation_scope",
    "strict_watt_cap",
)


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _nonnegative(value: Any) -> Optional[float]:
    result = _finite(value)
    return result if result is not None and result >= 0.0 else None


def _positive_int(value: Any) -> int:
    if isinstance(value, bool):
        return 0
    try:
        result = int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
    return result if result > 0 else 0


def _sha256(value: Mapping[str, Any]) -> str:
    encoded = json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(encoded).hexdigest()


def _sha256_reference(value: Any) -> str:
    text = str(value or "").strip()
    digest = text[7:] if text.startswith("sha256:") else ""
    return text if (
        len(text) == 71
        and len(digest) == 64
        and all(char in "0123456789abcdef" for char in digest)
    ) else ""


def _expected_tiers(public_mode: Any) -> Tuple[str, ...]:
    mode = normalize_wb_mode(public_mode)
    if mode in (MODE_CURVE, MODE_BASE):
        return ("pv",)
    if mode in (MODE_TARGET, MODE_PRICE, MODE_BATTERY_DEPARTURE):
        return ("pv", "storage")
    return ()


def _normalize_tiers(value: Any) -> Tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        raw: Sequence[Any] = (value,)
    elif isinstance(value, (tuple, list)):
        raw = value
    else:
        return ()
    tiers = tuple(str(item or "").strip().lower() for item in raw)
    return tiers if tiers in (("pv",), ("pv", "storage")) else ()


def _source_class(tiers: Tuple[str, ...]) -> str:
    if tiers == ("pv",):
        return "pv_only"
    if tiers == ("pv", "storage"):
        return "storage_supported"
    return "unknown"


def status_sample_binding(status: Any) -> Dict[str, Any]:
    """Bindet ein bereits normalisiertes, frisches Treibersample typisiert."""

    data = status if isinstance(status, Mapping) else {}
    candidates = tuple(
        value
        for value in (
            _finite(data.get("native_status_sample_ts")),
            _finite(data.get("driver_status_last_sample_ts")),
            _finite(data.get("driver_status_last_ok_ts")),
        )
        if value is not None and value > 0.0
    )
    sample_ts = max(candidates) if candidates else None
    sample_seq = _positive_int(data.get("native_status_sample_seq"))
    fresh = bool(
        data.get("driver_status_valid") is True
        and data.get("driver_status_stale") is not True
        and data.get("driver_status_degraded") is not True
        and data.get("driver_status_glitch") is not True
        and data.get("driver_status_plausible") is not False
        and data.get("valid") is not False
        and data.get("stale") is not True
    )
    valid = bool(fresh and sample_ts is not None)
    material = {
        "schema": STATUS_SAMPLE_SCHEMA,
        "sample_ts": round(float(sample_ts), 6) if valid else None,
        "sample_seq": int(sample_seq),
        "driver_status_source": str(
            data.get("driver_status_source") or ""
        ),
    }
    return {
        **material,
        "valid": valid,
        "sample_key": _sha256(material) if valid else "",
        "reason": "current_status_sample" if valid else "status_sample_unbound",
    }


def measured_power_evidence(status: Any) -> Dict[str, Any]:
    """Liefert ausschließlich direkt belegte Wallbox-Leistung in Watt."""

    data = status if isinstance(status, Mapping) else {}
    sample = status_sample_binding(data)
    result = {
        "schema": MEASUREMENT_SCHEMA,
        "valid": False,
        "power_w": None,
        "sample_key": str(sample.get("sample_key") or ""),
        "sample_ts": sample.get("sample_ts"),
        "sample_seq": sample.get("sample_seq", 0),
        "source": "none",
        "reason": "status_sample_unbound",
    }
    if sample.get("valid") is not True:
        return result
    if data.get("phase_power_verified") is not True:
        result["reason"] = "direct_power_not_verified"
        return result

    phase_values = []
    phase_fields_present = False
    for key in (
        "phase_power_l1_w",
        "phase_power_l2_w",
        "phase_power_l3_w",
    ):
        if key not in data:
            continue
        phase_fields_present = True
        value = _nonnegative(data.get(key))
        if value is None:
            result["reason"] = "phase_power_invalid"
            return result
        phase_values.append(value)
    phase_sum = sum(phase_values) if phase_fields_present else None
    reported_sum = _nonnegative(data.get("phase_power_sum_w"))
    if phase_sum is not None and phase_sum > 50.0:
        if reported_sum is not None and reported_sum > 50.0:
            tolerance = max(250.0, 0.15 * max(phase_sum, reported_sum))
            if abs(phase_sum - reported_sum) > tolerance:
                result["reason"] = "phase_power_sum_conflict"
                return result
        power_w = phase_sum
        source = "verified_phase_power_sum"
    elif reported_sum is not None and reported_sum > 50.0:
        power_w = reported_sum
        source = "verified_driver_phase_sum"
    else:
        result["reason"] = "verified_power_below_evidence_floor"
        return result

    result.update({
        "valid": True,
        "power_w": round(float(power_w), 6),
        "source": source,
        "reason": "direct_measured_power",
    })
    return result


def _seal_material(contract: Mapping[str, Any]) -> Dict[str, Any]:
    return {key: contract.get(key) for key in _SEALED_FIELDS}


def build_source_watt_cap(
    *,
    wb_id: Any,
    plug_session_id: Any,
    session_context_key: Any,
    cycle_token: Any,
    live_snapshot: Any,
    status: Any,
    budget_revision: Any,
    decision_generation: Any,
    public_mode: Any,
    cap_w: Any,
    source_ceiling_w: Any,
    authorized_phases: Any,
    group_budget_w: Any,
    allocation_scope: Any,
    allocation_ready: bool,
    safety_binding_valid: bool,
    source_contract_valid: bool,
    cap_basis: Any,
    eligible_budget_tiers: Any = None,
    source_binding_key: Any = "",
) -> Dict[str, Any]:
    """Versiegelt den unveränderlichen Quellen-Wattdeckel fail-closed."""

    snapshot = live_snapshot if isinstance(live_snapshot, Mapping) else {}
    sample = status_sample_binding(status)
    charger_id = _positive_int(wb_id)
    session_id = str(plug_session_id or "").strip()
    session_context = _sha256_reference(session_context_key)
    token = str(cycle_token or "").strip()
    mode = normalize_wb_mode(public_mode)
    expected_tiers = _expected_tiers(mode)
    provided_tiers = (
        _normalize_tiers(eligible_budget_tiers)
        if eligible_budget_tiers is not None
        else expected_tiers
    )
    cap = _nonnegative(cap_w)
    ceiling = _nonnegative(source_ceiling_w)
    group_budget = _nonnegative(group_budget_w)
    phases = _positive_int(authorized_phases)
    scope = str(allocation_scope or "").strip()
    basis = str(cap_basis or "").strip()
    budget_ref = _sha256_reference(budget_revision)
    decision_ref = _sha256_reference(decision_generation)
    source_binding = str(source_binding_key or "").strip()
    snapshot_id = str(snapshot.get("snapshot_id") or "").strip()
    live_ts = _finite(snapshot.get("sample_ts"))
    live_revision = _positive_int(snapshot.get("file_revision_ns"))

    blockers = []
    if charger_id <= 0:
        blockers.append("wb_id_invalid")
    if not session_id or len(session_id) > 256:
        blockers.append("plug_session_unbound")
    if not session_context:
        blockers.append("session_context_unbound")
    if not token or len(token) > 128:
        blockers.append("cycle_token_unbound")
    if mode == MODE_OFF:
        blockers.append("mode_off")
    if not expected_tiers or provided_tiers != expected_tiers:
        blockers.append("source_tier_mode_mismatch")
    if cap is None or ceiling is None or group_budget is None:
        blockers.append("watt_cap_invalid")
    elif cap > ceiling + 1e-6 or ceiling > group_budget + 1e-6:
        blockers.append("watt_cap_not_monotonic")
    if phases not in (1, 2, 3):
        blockers.append("authorized_phases_invalid")
    if scope not in ("single", "multi"):
        blockers.append("allocation_scope_invalid")
    elif scope == "multi" and allocation_ready is not True:
        blockers.append("group_allocation_not_ready")
    if basis not in _VALID_CAP_BASES:
        blockers.append("cap_basis_invalid")
    if safety_binding_valid is not True:
        blockers.append("safety_binding_invalid")
    if source_contract_valid is not True:
        blockers.append("source_contract_invalid")
    if not budget_ref:
        blockers.append("budget_revision_invalid")
    if not decision_ref:
        blockers.append("decision_generation_invalid")
    if not (
        snapshot.get("valid") is True
        and snapshot_id
        and live_ts is not None
        and live_ts > 0.0
        and live_revision > 0
    ):
        blockers.append("live_snapshot_unbound")
    if sample.get("valid") is not True:
        blockers.append("status_sample_unbound")
    if not source_binding:
        blockers.append("source_binding_key_missing")
    elif not _sha256_reference(source_binding):
        blockers.append("source_binding_key_invalid")

    valid = not blockers
    contract = {
        "schema": CAP_SCHEMA,
        "wb_id": charger_id,
        "plug_session_id": session_id,
        "session_context_key": session_context,
        "cycle_token": token,
        "live_snapshot_id": snapshot_id,
        "live_sample_ts": (
            round(float(live_ts), 6) if live_ts is not None else None
        ),
        "live_file_revision_ns": live_revision or None,
        "status_sample_key": str(sample.get("sample_key") or ""),
        "status_sample_ts": sample.get("sample_ts"),
        "status_sample_seq": int(sample.get("sample_seq", 0) or 0),
        "budget_revision": budget_ref,
        "decision_generation": decision_ref,
        "public_mode": mode,
        "source_class": _source_class(provided_tiers),
        "eligible_budget_tiers": list(provided_tiers),
        "source_binding_key": source_binding,
        "cap_basis": basis,
        "cap_w": round(float(cap), 6) if cap is not None and valid else None,
        "source_ceiling_w": (
            round(float(ceiling), 6)
            if ceiling is not None and valid
            else None
        ),
        "authorized_phases": phases,
        "group_budget_w": (
            round(float(group_budget), 6)
            if group_budget is not None and valid
            else None
        ),
        "allocation_scope": scope,
        "strict_watt_cap": True,
        "active": valid,
        "valid": valid,
        "reason": "source_watt_cap_sealed" if valid else blockers[0],
        "blockers": blockers,
    }
    contract["seal"] = _sha256(_seal_material(contract)) if valid else ""
    return contract


def validate_source_watt_cap(
    contract: Any,
    *,
    expected_wb_id: Any,
    expected_plug_session_id: Any,
    expected_session_context_key: Any,
    expected_cycle_token: Any,
    expected_live_snapshot_id: Any,
    expected_status: Any,
    expected_budget_revision: Any,
    expected_decision_generation: Any,
    expected_public_mode: Any,
) -> Dict[str, Any]:
    """Prüft Seal und alle veränderlichen Laufzeitbindungen erneut."""

    data = contract if isinstance(contract, Mapping) else {}
    expected_sample = status_sample_binding(expected_status)
    reasons = []
    if not (
        data.get("schema") == CAP_SCHEMA
        and data.get("active") is True
        and data.get("valid") is True
        and data.get("strict_watt_cap") is True
    ):
        reasons.append("cap_contract_inactive_or_invalid")
    seal = str(data.get("seal") or "")
    if not seal or seal != _sha256(_seal_material(data)):
        reasons.append("cap_seal_mismatch")
    expected_values = {
        "wb_id": _positive_int(expected_wb_id),
        "plug_session_id": str(expected_plug_session_id or "").strip(),
        "session_context_key": _sha256_reference(
            expected_session_context_key
        ),
        "cycle_token": str(expected_cycle_token or "").strip(),
        "live_snapshot_id": str(expected_live_snapshot_id or "").strip(),
        "status_sample_key": str(expected_sample.get("sample_key") or ""),
        "budget_revision": _sha256_reference(expected_budget_revision),
        "decision_generation": _sha256_reference(
            expected_decision_generation
        ),
        "public_mode": normalize_wb_mode(expected_public_mode),
    }
    if expected_sample.get("valid") is not True:
        reasons.append("expected_status_sample_unbound")
    for key, expected in expected_values.items():
        if not expected or data.get(key) != expected:
            reasons.append("%s_mismatch" % key)
    cap = _nonnegative(data.get("cap_w"))
    ceiling = _nonnegative(data.get("source_ceiling_w"))
    group_budget = _nonnegative(data.get("group_budget_w"))
    if (
        cap is None
        or ceiling is None
        or group_budget is None
        or cap > ceiling + 1e-6
        or ceiling > group_budget + 1e-6
    ):
        reasons.append("sealed_watt_cap_invalid")
    valid = not reasons
    return {
        "contract": "wallbox_session_source_watt_cap_validation_v1",
        "valid": valid,
        "reason": "source_watt_cap_current" if valid else reasons[0],
        "blockers": reasons,
        "cap_w": round(float(cap), 6) if valid and cap is not None else None,
        "source_class": str(data.get("source_class") or ""),
        "eligible_budget_tiers": list(
            _normalize_tiers(data.get("eligible_budget_tiers"))
        ),
        "cap_basis": str(data.get("cap_basis") or ""),
        "session_context_key": (
            str(data.get("session_context_key") or "") if valid else ""
        ),
        "seal": seal if valid else "",
    }
