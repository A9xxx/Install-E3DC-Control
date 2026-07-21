#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Typisierter PV-Forecast- und Headroom-Topologievertrag.

Das Modul enthält ausschließlich deterministische Produktlogik. Es liest keine
Provider- oder Gerätedaten und sendet keine Hardwarebefehle.
"""

from __future__ import annotations

import hashlib
import json
import math
from typing import Any, Dict, Iterable, Mapping, Optional


TOPOLOGY_SCHEMA = "pv_forecast_topology_v1"
SLOT_TOPOLOGY_SCHEMA = "pv_forecast_slot_topology_v1"
HEADROOM_SCHEMA = "pv_headroom_pressure_v1"
PCC_LIMIT_SCHEMA = "pcc_headroom_limit_v1"
COUPLING_E3DC_DC = "E3DC_DC"
COUPLING_EXTERNAL_AC = "EXTERNAL_AC"
VALID_COUPLINGS = {COUPLING_E3DC_DC, COUPLING_EXTERNAL_AC}
EXTERNAL_AC_LIMIT_KEY = "pv_external_ac_inverter_limit_w"
E3DC_DC_LIMIT_KEY = "pv_e3dc_dc_inverter_limit_w"
RESOURCE_PROJECTION_ABS_TOLERANCE_W = 5.0
RESOURCE_PROJECTION_REL_TOLERANCE = 0.001

RESOURCE_SPECS = (
    ("FC1", "forecast1", "solcast_resource_id", "pv_forecast_coupling_fc1"),
    ("FC2", "forecast2", "solcast_resource_id_2", "pv_forecast_coupling_fc2"),
    ("FC3", "forecast3", "solcast_resource_id_3", "pv_forecast_coupling_fc3"),
    ("FC4", None, "solcast_resource_id_4", "pv_forecast_coupling_fc4"),
)


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        number = float(str(value).strip().replace(",", "."))
        return number if math.isfinite(number) else default
    except (TypeError, ValueError):
        return default


def _canonical_hash(value: Any) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def _configured(config: Mapping[str, Any], key: Optional[str]) -> bool:
    return bool(key and str(config.get(key) or "").strip())


def _coupling(value: Any) -> Optional[str]:
    text = str(value or "").strip().upper().replace("-", "_")
    return text if text in VALID_COUPLINGS else None


def resolve_buffered_pcc_limit(
    configured_limit_w: Any,
    live_readback_limit_w: Any,
    buffer_w: Any,
) -> Dict[str, Any]:
    """Materialisiert den PCC-Grenzvertrag des Storage-Reglers.

    Nur positive Konfigurations-/Readbackwerte sind aktive Quellen. Ein daraus
    resultierendes ``limit_w == 0`` bleibt durch ``active == True`` eindeutig
    von fehlenden Quellen unterschieden. Der Puffer wird exakt einmal nach der
    Auswahl des kleineren harten Limits abgezogen.
    """

    configured = max(0.0, _safe_float(configured_limit_w, 0.0) or 0.0)
    live = max(0.0, _safe_float(live_readback_limit_w, 0.0) or 0.0)
    buffer_value = max(0.0, _safe_float(buffer_w, 0.0) or 0.0)
    configured_active = configured > 0.0
    live_active = live > 0.0

    if configured_active and live_active:
        hard_limit = min(configured, live)
        source = "config_below_rscp_buffered" if configured < live - 50.0 else "rscp_buffered"
    elif live_active:
        hard_limit = live
        source = "rscp_buffered"
    elif configured_active:
        hard_limit = configured
        source = "config_buffered"
    else:
        hard_limit = 0.0
        source = "none"

    active = configured_active or live_active
    effective_limit = max(0.0, hard_limit - buffer_value) if active else 0.0
    return {
        "schema_version": PCC_LIMIT_SCHEMA,
        "active": active,
        "limit_w": round(effective_limit, 3) if active else None,
        "hard_limit_w": round(hard_limit, 3) if active else None,
        "buffer_w": round(buffer_value, 3),
        "source": source,
        "configured": {
            "active": configured_active,
            "limit_w": round(configured, 3) if configured_active else None,
        },
        "live_readback": {
            "active": live_active,
            "limit_w": round(live, 3) if live_active else None,
        },
        "zero_semantics": "ACTIVE_ZERO_ONLY_WHEN_SOURCE_PRESENT_AND_BUFFERED",
    }


def build_pv_forecast_topology(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """Materialisiert die nichtgeheime Ressourcen- und Limitbindung.

    Resource-IDs und Provider-Schlüssel werden bewusst niemals ausgegeben oder
    in die Revision aufgenommen. Nur die neutralen FC-Labels und die physische
    Kopplung sind fachliches Planmaterial.
    """

    cfg = {str(key).lower(): value for key, value in dict(config or {}).items()}
    resources = []
    missing = []
    invalid = []
    for label, forecast_key, provider_key, coupling_key in RESOURCE_SPECS:
        has_geometry = _configured(cfg, forecast_key)
        has_provider_resource = _configured(cfg, provider_key)
        if not (has_geometry or has_provider_resource):
            continue
        raw_coupling = str(cfg.get(coupling_key) or "").strip()
        coupling = _coupling(raw_coupling)
        if not raw_coupling:
            missing.append(label)
        elif coupling is None:
            invalid.append(label)
        resources.append({
            "resource_key": label,
            "coupling": coupling or "UNBOUND",
            "coupling_key": coupling_key,
            "has_geometry": bool(has_geometry),
            "has_provider_resource": bool(has_provider_resource),
            "mapping_source": "explicit" if coupling else "missing_or_invalid",
        })

    external_limit = max(0.0, _safe_float(cfg.get(EXTERNAL_AC_LIMIT_KEY), 0.0) or 0.0)
    e3dc_limit = max(0.0, _safe_float(cfg.get(E3DC_DC_LIMIT_KEY), 0.0) or 0.0)
    external_bound = any(item["coupling"] == COUPLING_EXTERNAL_AC for item in resources)
    e3dc_bound = any(item["coupling"] == COUPLING_E3DC_DC for item in resources)

    if not resources:
        status = "disabled_no_resources"
        reason = "NO_FORECAST_RESOURCES_CONFIGURED"
    elif invalid:
        status = "invalid"
        reason = "INVALID_RESOURCE_COUPLING"
    elif missing:
        status = "topology_unbound"
        reason = "RESOURCE_COUPLING_MISSING"
    elif external_bound and external_limit <= 0.0:
        status = "invalid"
        reason = "EXTERNAL_AC_INVERTER_LIMIT_MISSING"
    else:
        status = "bound"
        reason = "OK"

    material = {
        "schema_version": TOPOLOGY_SCHEMA,
        "status": status,
        "reason": reason,
        "resources": [
            {
                "resource_key": item["resource_key"],
                "coupling": item["coupling"],
                "coupling_key": item["coupling_key"],
                "has_geometry": item["has_geometry"],
                "has_provider_resource": item["has_provider_resource"],
            }
            for item in resources
        ],
        "limits_w": {
            "e3dc_dc_configured": round(e3dc_limit, 3) if e3dc_limit > 0.0 else None,
            "external_ac_inverter": round(external_limit, 3) if external_limit > 0.0 else None,
        },
    }
    return {
        **material,
        "revision": _canonical_hash(material),
        "resource_count": len(resources),
        "missing_resource_keys": sorted(missing),
        "invalid_resource_keys": sorted(invalid),
        "split_usable": status == "bound",
        "e3dc_dc_bound": bool(e3dc_bound),
        "external_ac_bound": bool(external_bound),
        "legacy_policy": "TOTAL_PV_PCC_ONLY_NO_DC_SPLIT_NO_AGGRESSIVE_CHANGE",
        "privacy": "neutral_resource_keys_only_no_provider_ids",
    }


def project_slot_topology(
    total_pv_w: Any,
    resource_contributions_w: Optional[Mapping[str, Any]],
    topology: Optional[Mapping[str, Any]],
    *,
    projection_status: Any = "complete",
    projection_reason: Any = "OK",
) -> Dict[str, Any]:
    """Bindet Ressourcenbeiträge verlustfrei an einen Gesamt-PV-Slot.

    Bei fehlender oder inkonsistenter Zuordnung bleibt der Gesamtwert erhalten;
    ein DC-/AC-Split wird dann nicht erfunden. Bei gebundener externer AC-PV
    begrenzt deren konfiguriertes WR-Limit ausschließlich diesen AC-Anteil.
    """

    total = max(0.0, _safe_float(total_pv_w, 0.0) or 0.0)
    contract = dict(topology or {})
    resources = [dict(item) for item in contract.get("resources", []) if isinstance(item, dict)]
    raw = {
        str(key): max(0.0, _safe_float(value, 0.0) or 0.0)
        for key, value in dict(resource_contributions_w or {}).items()
    }
    required_keys = [str(item.get("resource_key") or "") for item in resources if item.get("resource_key")]
    supplied_projection_status = str(projection_status or "").strip().lower()
    supplied_projection_reason = str(projection_reason or "").strip().upper()
    source_complete = supplied_projection_status == "complete"
    complete = source_complete and bool(required_keys) and all(key in raw for key in required_keys)
    raw_sum = sum(raw.get(key, 0.0) for key in required_keys)
    projection_tolerance = max(
        RESOURCE_PROJECTION_ABS_TOLERANCE_W,
        total * RESOURCE_PROJECTION_REL_TOLERANCE,
    )
    projection_delta = abs(total - raw_sum)
    totals_coherent = projection_delta <= projection_tolerance
    split_usable = (
        bool(contract.get("split_usable"))
        and complete
        and totals_coherent
        and (total <= projection_tolerance or raw_sum > 0.0)
    )

    if not split_usable:
        reason = str(contract.get("reason") or "TOPOLOGY_UNAVAILABLE")
        if bool(contract.get("split_usable")) and not source_complete:
            reason = (
                supplied_projection_reason
                if supplied_projection_reason in {
                    "RESOURCE_PROJECTION_INCOMPLETE",
                    "RESOURCE_PROJECTION_TOTAL_MISMATCH",
                }
                else "RESOURCE_PROJECTION_INCOMPLETE"
            )
        elif bool(contract.get("split_usable")) and not complete:
            reason = "RESOURCE_PROJECTION_INCOMPLETE"
        elif bool(contract.get("split_usable")) and total > 0.0 and raw_sum <= 0.0:
            reason = "RESOURCE_PROJECTION_EMPTY"
        elif bool(contract.get("split_usable")) and not totals_coherent:
            reason = "RESOURCE_PROJECTION_TOTAL_MISMATCH"
        return {
            "schema_version": SLOT_TOPOLOGY_SCHEMA,
            "status": "topology_unbound",
            "reason": reason,
            "topology_revision": contract.get("revision"),
            "total_pv_w": round(total, 3),
            "e3dc_dc_pv_w": None,
            "external_ac_pv_w": None,
            "resources": [
                {
                    "resource_key": key,
                    "coupling": next(
                        (str(item.get("coupling") or "UNBOUND") for item in resources if item.get("resource_key") == key),
                        "UNBOUND",
                    ),
                    "forecast_w": round(raw.get(key, 0.0), 3) if key in raw else None,
                }
                for key in required_keys
            ],
            "external_ac_capped_w": 0.0,
            "split_usable": False,
            "resource_projection_status": "unbound",
            "resource_projection_reason": reason,
            "resource_projection_delta_w": round(projection_delta, 3),
            "resource_projection_tolerance_w": round(projection_tolerance, 3),
        }

    # Ausschließlich kleine Rundungsdifferenzen normalisieren. Größere Lücken
    # sind oben fail-closed und dürfen keinen erfundenen DC-/AC-Split erzeugen.
    scale = total / raw_sum if raw_sum > 0.0 else 0.0
    normalized = {key: raw.get(key, 0.0) * scale for key in required_keys}
    by_coupling = {COUPLING_E3DC_DC: 0.0, COUPLING_EXTERNAL_AC: 0.0}
    coupling_by_key = {str(item.get("resource_key")): str(item.get("coupling")) for item in resources}
    for key, value in normalized.items():
        coupling = coupling_by_key.get(key)
        if coupling in by_coupling:
            by_coupling[coupling] += value

    external_limit = max(
        0.0,
        _safe_float((contract.get("limits_w") or {}).get("external_ac_inverter"), 0.0) or 0.0,
    )
    external_before = by_coupling[COUPLING_EXTERNAL_AC]
    external_after = min(external_before, external_limit) if external_limit > 0.0 else external_before
    if external_before > 0.0 and external_after < external_before:
        external_scale = external_after / external_before
        for key in normalized:
            if coupling_by_key.get(key) == COUPLING_EXTERNAL_AC:
                normalized[key] *= external_scale
        by_coupling[COUPLING_EXTERNAL_AC] = external_after

    adjusted_total = by_coupling[COUPLING_E3DC_DC] + by_coupling[COUPLING_EXTERNAL_AC]
    return {
        "schema_version": SLOT_TOPOLOGY_SCHEMA,
        "status": "bound",
        "reason": "OK",
        "topology_revision": contract.get("revision"),
        "total_pv_w": round(adjusted_total, 3),
        "e3dc_dc_pv_w": round(by_coupling[COUPLING_E3DC_DC], 3),
        "external_ac_pv_w": round(by_coupling[COUPLING_EXTERNAL_AC], 3),
        "resources": [
            {
                "resource_key": key,
                "coupling": coupling_by_key.get(key),
                "forecast_w": round(normalized.get(key, 0.0), 3),
            }
            for key in required_keys
        ],
        "external_ac_capped_w": round(max(0.0, external_before - external_after), 3),
        "split_usable": True,
        "resource_projection_status": "complete",
        "resource_projection_reason": "OK",
        "resource_projection_delta_w": round(projection_delta, 3),
        "resource_projection_tolerance_w": round(projection_tolerance, 3),
    }


def slot_headroom_pressure(
    *,
    total_pv_w: Any,
    e3dc_dc_pv_w: Any,
    external_ac_pv_w: Any,
    topology_status: Any,
    topology_revision: Any,
    expected_topology_revision: Any,
    e3dc_dc_limit_w: Any,
    pcc_limit_w: Any,
    safe_consumers_w: Any,
    charge_limit_w: Any,
    pcc_limit_active: Optional[bool] = None,
    e3dc_dc_limit_source: str = "",
    pcc_limit_source: str = "",
) -> Dict[str, Any]:
    """Berechnet DC- und PCC-Druck ohne Doppelzählung derselben PV-Energie."""

    total = max(0.0, _safe_float(total_pv_w, 0.0) or 0.0)
    dc = _safe_float(e3dc_dc_pv_w, None)
    external = _safe_float(external_ac_pv_w, None)
    dc_limit = max(0.0, _safe_float(e3dc_dc_limit_w, 0.0) or 0.0)
    pcc_limit = max(0.0, _safe_float(pcc_limit_w, 0.0) or 0.0)
    pcc_active = bool(pcc_limit > 0.0) if pcc_limit_active is None else pcc_limit_active is True
    consumers = max(0.0, _safe_float(safe_consumers_w, 0.0) or 0.0)
    charge_limit = max(0.0, _safe_float(charge_limit_w, 0.0) or 0.0)
    revision_match = bool(topology_revision) and str(topology_revision) == str(expected_topology_revision)
    split_valid = (
        str(topology_status) == "bound"
        and revision_match
        and dc is not None
        and external is not None
        and abs(total - (max(0.0, dc) + max(0.0, external))) <= max(5.0, total * 0.001)
    )

    dc_pressure = max(0.0, max(0.0, dc or 0.0) - dc_limit) if split_valid and dc_limit > 0.0 else 0.0
    pcc_pressure = max(0.0, total - consumers - pcc_limit) if pcc_active else 0.0
    combined = max(dc_pressure, pcc_pressure)
    preventable = min(charge_limit, combined)
    unavoidable = max(0.0, combined - preventable)
    if not split_valid:
        status = "topology_unbound"
        reason = "TOPOLOGY_OR_REVISION_UNAVAILABLE__PCC_ONLY"
    elif dc_limit <= 0.0:
        status = "dc_limit_unavailable"
        reason = "E3DC_DC_LIMIT_UNAVAILABLE__PCC_ONLY"
    else:
        status = "bound"
        reason = "OK"
    binding = []
    if dc_pressure > 0.0:
        binding.append("E3DC_DC")
    if pcc_pressure > 0.0:
        binding.append("PCC")
    return {
        "schema_version": HEADROOM_SCHEMA,
        "status": status,
        "reason": reason,
        "topology_revision": topology_revision,
        "revision_match": revision_match,
        "total_pv_w": round(total, 3),
        "e3dc_dc_pv_w": round(max(0.0, dc), 3) if dc is not None else None,
        "external_ac_pv_w": round(max(0.0, external), 3) if external is not None else None,
        "safe_consumers_w": round(consumers, 3),
        "dc_pressure_w": round(dc_pressure, 3),
        "pcc_pressure_w": round(pcc_pressure, 3),
        "combined_pressure_w": round(combined, 3),
        "preventable_w": round(preventable, 3),
        "unavoidable_w": round(unavoidable, 3),
        "combination_rule": "max_dc_pcc_no_double_count",
        "binding_limits": binding,
        "limits_w": {
            "e3dc_dc": round(dc_limit, 3) if dc_limit > 0.0 else None,
            "pcc": round(pcc_limit, 3) if pcc_active else None,
        },
        "limit_active": {
            "e3dc_dc": bool(dc_limit > 0.0),
            "pcc": pcc_active,
        },
        "limit_sources": {
            "e3dc_dc": str(e3dc_dc_limit_source or "unavailable"),
            "pcc": str(pcc_limit_source or "unavailable"),
        },
    }


def topology_resource_keys(topology: Optional[Mapping[str, Any]]) -> Iterable[str]:
    for item in (topology or {}).get("resources", []) or []:
        if isinstance(item, dict) and item.get("resource_key"):
            yield str(item["resource_key"])
