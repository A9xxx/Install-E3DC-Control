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
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple


TOPOLOGY_SCHEMA = "pv_forecast_topology_v2"
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

# The one versioned V4 contract deliberately remains data, not a positional
# list of FC slots: a group ID is stable across provider, surface and account
# changes.  Legacy FC fields below are read-only fallback only.
TOPOLOGY_CONFIG_KEY = "pv_forecast_topology_config"
TOPOLOGY_CONFIG_SCHEMA = "pv_forecast_topology_config_v1"
PROVIDER_SOLCAST = "solcast"

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


def _as_list(value: Any) -> list:
    """Accept a V4 JSON list or a JSON-encoded legacy-compatible value."""

    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, str) and value.strip():
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError):
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _topology_config(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    cfg = {str(key).lower(): value for key, value in dict(config or {}).items()}
    raw = cfg.get(TOPOLOGY_CONFIG_KEY)
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str) and raw.strip():
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(parsed) if isinstance(parsed, Mapping) else {}
    return {}


def has_explicit_topology_config(config: Optional[Mapping[str, Any]]) -> bool:
    """Distinguish absent/blank legacy input from an explicit invalid contract."""

    cfg = {str(key).lower(): value for key, value in dict(config or {}).items()}
    if TOPOLOGY_CONFIG_KEY not in cfg:
        return False
    value = cfg.get(TOPOLOGY_CONFIG_KEY)
    return not (isinstance(value, str) and not value.strip())


def _stable_id(value: Any) -> str:
    """Return a user-provided stable identifier without inventing one."""

    return str(value or "").strip()


def _surface(value: Any) -> Tuple[Optional[Dict[str, Any]], str]:
    if not isinstance(value, Mapping):
        return None, "SURFACE_NOT_OBJECT"
    surface_id = _stable_id(value.get("id") or value.get("surface_id"))
    if not surface_id:
        return None, "SURFACE_ID_MISSING"
    tilt = _safe_float(value.get("tilt"), None)
    azimuth = _safe_float(value.get("azimuth"), None)
    kwp = _safe_float(value.get("kwp"), None)
    if tilt is None or azimuth is None or kwp is None or kwp <= 0.0:
        return None, "SURFACE_GEOMETRY_INVALID"
    return {
        "surface_id": surface_id,
        "tilt": round(tilt, 3),
        "azimuth": round(azimuth, 3),
        "kwp": round(kwp, 4),
    }, ""


def configured_generator_groups(config: Optional[Mapping[str, Any]]) -> list:
    """Read explicit groups without ever rewriting legacy configuration.

    A group owns one electrical coupling and one or more physical surfaces.
    Surface IDs are stable independently of provider resources.  Invalid input
    is represented by a diagnostic entry so consumers can fail closed.
    """

    contract = _topology_config(config)
    groups = []
    seen = set()
    seen_surface_ids = set()
    for index, raw_group in enumerate(_as_list(contract.get("generator_groups")), start=1):
        if not isinstance(raw_group, Mapping):
            groups.append({"group_id": "", "invalid": "GROUP_NOT_OBJECT", "surfaces": []})
            continue
        group_id = _stable_id(raw_group.get("id") or raw_group.get("group_id"))
        if not group_id:
            groups.append({"group_id": "", "invalid": "GROUP_ID_MISSING", "surfaces": []})
            continue
        if group_id in seen:
            groups.append({"group_id": group_id, "invalid": "GROUP_ID_DUPLICATE", "surfaces": []})
            continue
        seen.add(group_id)
        raw_surfaces = raw_group.get("surfaces")
        if raw_surfaces is None and any(key in raw_group for key in ("tilt", "azimuth", "kwp")):
            raw_surfaces = [raw_group]
        surfaces = []
        surface_errors = []
        for raw_surface in _as_list(raw_surfaces):
            surface, surface_error = _surface(raw_surface)
            if surface_error:
                surface_errors.append(surface_error)
            elif surface["surface_id"] in seen_surface_ids:
                surface_errors.append("SURFACE_ID_DUPLICATE")
            else:
                seen_surface_ids.add(surface["surface_id"])
                surfaces.append(surface)
        coupling = _coupling(raw_group.get("coupling"))
        errors = []
        if coupling is None:
            errors.append("GROUP_COUPLING_INVALID_OR_MISSING")
        if not surfaces and not surface_errors:
            errors.append("GROUP_SURFACES_MISSING")
        errors.extend(surface_errors)
        groups.append({
            "group_id": group_id,
            "coupling": coupling,
            "inverter_group": _stable_id(raw_group.get("inverter_group")) or None,
            "surfaces": surfaces,
            "invalid": errors[0] if errors else "",
            "invalid_reasons": errors,
        })
    return groups


def configured_provider_bindings(config: Optional[Mapping[str, Any]]) -> list:
    """Read explicit provider-resource bindings, preserving Missingness.

    One site may deliberately name several groups.  This is valid for total PV
    but never proves a source split when those groups span both couplings.
    """

    contract = _topology_config(config)
    bindings = []
    seen = set()
    seen_resources = set()
    for index, raw_binding in enumerate(_as_list(contract.get("provider_resources")), start=1):
        if not isinstance(raw_binding, Mapping):
            bindings.append({"binding_id": "", "invalid": "BINDING_NOT_OBJECT"})
            continue
        provider = str(raw_binding.get("provider") or PROVIDER_SOLCAST).strip().lower()
        resource_id = str(raw_binding.get("resource_id") or raw_binding.get("provider_resource_id") or "").strip()
        binding_id = _stable_id(raw_binding.get("id") or raw_binding.get("binding_id"))
        allocations = []
        raw_allocations = raw_binding.get("allocations")
        if raw_allocations is not None:
            for allocation in _as_list(raw_allocations):
                if not isinstance(allocation, Mapping):
                    continue
                group_id = _stable_id(allocation.get("generator_group_id"))
                share = _safe_float(allocation.get("share"), None)
                if group_id and share is not None and share > 0.0:
                    allocations.append({"group_id": group_id, "share": share})
        else:
            group_id = _stable_id(raw_binding.get("generator_group_id"))
            if group_id:
                allocations.append({"group_id": group_id, "share": 1.0})
        group_ids = [item["group_id"] for item in allocations]
        resource_identity = (
            provider,
            resource_id.casefold(),
        )
        invalid = ""
        if not binding_id:
            invalid = "BINDING_ID_MISSING"
        elif provider != PROVIDER_SOLCAST:
            invalid = "PROVIDER_UNSUPPORTED"
        elif not resource_id:
            invalid = "PROVIDER_RESOURCE_ID_MISSING"
        elif resource_identity in seen_resources:
            invalid = "PROVIDER_RESOURCE_DUPLICATE"
        elif str(raw_binding.get("account_slot", raw_binding.get("solcast_account_slot", ""))).strip() not in {"1", "2"}:
            invalid = "PROVIDER_ACCOUNT_SLOT_INVALID_OR_MISSING"
        elif not group_ids:
            invalid = "BINDING_GROUP_MISSING"
        elif len(set(group_ids)) != len(group_ids):
            invalid = "BINDING_GROUP_DUPLICATE"
        elif abs(sum(item["share"] for item in allocations) - 1.0) > 0.0001:
            invalid = "BINDING_ALLOCATION_SHARE_INVALID"
        elif binding_id in seen:
            invalid = "BINDING_ID_DUPLICATE"
        if binding_id:
            seen.add(binding_id)
        if resource_id:
            seen_resources.add(resource_identity)
        bindings.append({
            "binding_id": binding_id,
            "provider": provider,
            "resource_id": resource_id,
            "group_ids": group_ids,
            "allocations": allocations,
            "account_slot": raw_binding.get("account_slot", raw_binding.get("solcast_account_slot")),
            "invalid": invalid,
        })
    return bindings


def legacy_provider_resource_duplicate_keys(
    config: Optional[Mapping[str, Any]],
) -> list:
    """Return only neutral FC labels for duplicate legacy provider resources."""

    cfg = {str(key).lower(): value for key, value in dict(config or {}).items()}
    labels_by_identity: Dict[Tuple[str, str], list] = {}
    for label, _forecast_key, provider_key, _coupling_key in RESOURCE_SPECS:
        resource_id = str(cfg.get(provider_key) or "").strip()
        if not resource_id:
            continue
        identity = (PROVIDER_SOLCAST, resource_id.casefold())
        labels_by_identity.setdefault(identity, []).append(label)
    return sorted(
        {
            label
            for labels in labels_by_identity.values()
            if len(labels) > 1
            for label in labels
        }
    )


def _provider_binding_identity_digest(
    cfg: Mapping[str, Any],
    *,
    explicit_contract: bool,
    explicit_bindings: Iterable[Mapping[str, Any]],
) -> str:
    """Bind provider reconfiguration without publishing provider identifiers.

    The returned digest is used only as input material for the public topology
    revision. Provider resource IDs never leave this function in clear text.
    """

    identities = []
    if explicit_contract:
        for binding in explicit_bindings:
            allocations = sorted(
                (
                    {
                        "group_id": str(item.get("group_id") or ""),
                        "share": round(float(item.get("share") or 0.0), 6),
                    }
                    for item in binding.get("allocations", [])
                    if isinstance(item, Mapping)
                ),
                key=lambda item: item["group_id"],
            )
            identities.append({
                "binding_id": str(binding.get("binding_id") or ""),
                "provider": str(binding.get("provider") or ""),
                "resource_id": str(binding.get("resource_id") or ""),
                "account_slot": str(binding.get("account_slot") or ""),
                "allocations": allocations,
            })
    else:
        legacy_secondary_slot = "2" if _configured(cfg, "solcast_api_key_2") else "1"
        for index, (label, _forecast_key, provider_key, _coupling_key) in enumerate(
            RESOURCE_SPECS,
            start=1,
        ):
            resource_id = str(cfg.get(provider_key) or "").strip()
            if not resource_id:
                continue
            mapping_key = "solcast_api_slot_fc{}".format(index)
            raw_slot = str(cfg.get(mapping_key) or "").strip()
            account_slot = raw_slot or ("1" if index == 1 else legacy_secondary_slot)
            identities.append({
                "binding_id": label,
                "provider": PROVIDER_SOLCAST,
                "resource_id": resource_id,
                "account_slot": account_slot,
                "allocations": [{"group_id": label, "share": 1.0}],
            })
    identities.sort(key=lambda item: (item["binding_id"], item["provider"]))
    return _canonical_hash({
        "schema_version": "pv_provider_binding_identity_v1",
        "bindings": identities,
    })


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

    Resource-IDs und Provider-Schlüssel werden bewusst niemals ausgegeben.
    Die Revision bindet Re-Konfigurationen nur über einen nicht veröffentlichten
    SHA-256-Digest, damit ein alter Forecast nicht unter neuer Zuordnung läuft.
    """

    cfg = {str(key).lower(): value for key, value in dict(config or {}).items()}
    topology_config = _topology_config(cfg)
    use_explicit_contract = has_explicit_topology_config(cfg)
    explicit_groups = configured_generator_groups(cfg)
    explicit_bindings = configured_provider_bindings(cfg)
    resources = []
    missing = []
    missing_geometry = []
    invalid = []
    provider_invalid = []
    binding_diagnostics = []

    if use_explicit_contract:
        if topology_config.get("schema_version") != TOPOLOGY_CONFIG_SCHEMA:
            invalid.append("TOPOLOGY_CONFIG_SCHEMA")
        group_by_id = {
            str(group.get("group_id")): group
            for group in explicit_groups
            if str(group.get("group_id") or "")
        }
        bindings_by_group: Dict[str, list] = {}
        for binding in sorted(explicit_bindings, key=lambda item: str(item.get("binding_id") or "")):
            binding_id = str(binding.get("binding_id") or "")
            group_ids = sorted(str(item) for item in binding.get("group_ids", []) if str(item))
            unresolved = [group_id for group_id in group_ids if group_id not in group_by_id]
            coupling_set = {
                str(group_by_id[group_id].get("coupling") or "UNBOUND")
                for group_id in group_ids if group_id in group_by_id
            }
            state = "bound"
            reason = "OK"
            if binding.get("invalid"):
                state = "invalid"
                reason = str(binding["invalid"])
            elif unresolved:
                state = "unbound"
                reason = "BINDING_GROUP_UNKNOWN"
            elif len(coupling_set) > 1:
                # A provider site that spans AC and DC cannot prove the split.
                state = "spans_couplings"
                reason = "PROVIDER_RESOURCE_SPANS_COUPLINGS"
            allocations = sorted(
                (
                    {
                        "group_id": str(item.get("group_id") or ""),
                        "share": round(float(item.get("share") or 0.0), 6),
                    }
                    for item in binding.get("allocations", [])
                ),
                key=lambda item: item["group_id"],
            )
            allocation_by_group = {
                str(item.get("group_id")): float(item.get("share") or 0.0)
                for item in allocations if str(item.get("group_id") or "")
            }
            for group_id in group_ids:
                bindings_by_group.setdefault(group_id, []).append({
                    "binding_id": binding_id,
                    "provider": binding.get("provider"),
                    "allocation_share": allocation_by_group.get(group_id),
                    "state": state,
                    "reason": reason,
                })
            binding_diagnostics.append({
                "binding_id": binding_id,
                "provider": binding.get("provider"),
                "group_ids": group_ids,
                "allocations": allocations,
                "state": state,
                "reason": reason,
            })

        for group in sorted(explicit_groups, key=lambda item: str(item.get("group_id") or "")):
            group_id = str(group.get("group_id") or "")
            group_invalid = str(group.get("invalid") or "")
            if group_invalid:
                invalid.append(group_invalid)
            group_bindings = sorted(
                bindings_by_group.get(group_id, []),
                key=lambda item: str(item.get("binding_id") or ""),
            )
            group_surfaces = sorted(
                [dict(surface) for surface in group.get("surfaces", []) if isinstance(surface, Mapping)],
                key=lambda item: str(item.get("surface_id") or ""),
            )
            # Local geometry is provider-independent.  A source split uses it
            # as an independent model, but provider bindings must be explicit
            # for provider data to participate in that split.
            resources.append({
                "resource_key": group_id,
                "coupling": group.get("coupling") or "UNBOUND",
                "coupling_key": None,
                "has_geometry": bool(group_surfaces),
                "has_provider_resource": bool(group_bindings),
                "inverter_group": group.get("inverter_group"),
                "surface_count": len(group_surfaces),
                "surfaces": group_surfaces,
                "kwp": round(sum(float(surface.get("kwp") or 0.0) for surface in group_surfaces), 4),
                "mapping_source": "explicit_group",
                "provider_binding_states": group_bindings,
            })
        if not resources:
            missing.append("GENERATOR_GROUPS")
        for diagnostic in sorted(binding_diagnostics, key=lambda item: str(item.get("binding_id") or "")):
            if diagnostic["state"] in {"invalid", "unbound", "spans_couplings"}:
                provider_invalid.append(str(diagnostic.get("reason") or "BINDING_INVALID"))
    else:
        # Legacy compatibility: preserve the previously explicit per-FC
        # coupling semantics, but never infer a relation from list position.
        legacy_duplicate_labels = set(
            legacy_provider_resource_duplicate_keys(cfg)
        )
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
            if label in legacy_duplicate_labels:
                invalid.append(label)
            if has_provider_resource and not has_geometry:
                missing_geometry.append(label)
            resources.append({
                "resource_key": label,
                "coupling": coupling or "UNBOUND",
                "coupling_key": coupling_key,
                "has_geometry": bool(has_geometry),
                "has_provider_resource": bool(has_provider_resource),
                "mapping_source": "legacy_explicit" if coupling else "legacy_missing_or_invalid",
                "provider_binding_states": [],
            })

    resources = sorted(resources, key=lambda item: str(item.get("resource_key") or ""))
    binding_diagnostics = sorted(binding_diagnostics, key=lambda item: str(item.get("binding_id") or ""))
    invalid = sorted(str(item) for item in invalid if str(item))
    missing = sorted(str(item) for item in missing if str(item))
    missing_geometry = sorted(str(item) for item in missing_geometry if str(item))
    provider_invalid = sorted(str(item) for item in provider_invalid if str(item))
    external_limit = max(0.0, _safe_float(cfg.get(EXTERNAL_AC_LIMIT_KEY), 0.0) or 0.0)
    e3dc_limit = max(0.0, _safe_float(cfg.get(E3DC_DC_LIMIT_KEY), 0.0) or 0.0)
    external_bound = any(item["coupling"] == COUPLING_EXTERNAL_AC for item in resources)
    e3dc_bound = any(item["coupling"] == COUPLING_E3DC_DC for item in resources)

    if invalid:
        status = "invalid"
        reason = (
            str(invalid[0])
            if use_explicit_contract
            else (
                "PROVIDER_RESOURCE_DUPLICATE"
                if legacy_duplicate_labels
                else "INVALID_RESOURCE_COUPLING"
            )
        )
    elif not resources:
        status = "disabled_no_resources"
        reason = "NO_FORECAST_RESOURCES_CONFIGURED"
    elif missing_geometry:
        status = "topology_unbound"
        reason = "RESOURCE_GEOMETRY_MISSING"
    elif missing:
        status = "topology_unbound"
        reason = "RESOURCE_COUPLING_MISSING"
    elif external_bound and external_limit <= 0.0:
        status = "invalid"
        reason = "EXTERNAL_AC_INVERTER_LIMIT_MISSING"
    else:
        status = "bound"
        reason = "OK"

    if not use_explicit_contract:
        legacy_provider_configured = any(
            item.get("has_provider_resource") for item in resources
        )
        if not legacy_provider_configured:
            provider_status = "disabled_no_bindings"
            provider_reason = "NO_PROVIDER_BINDINGS_CONFIGURED"
        elif status == "bound":
            provider_status = "bound"
            provider_reason = "OK"
        else:
            provider_status = "invalid"
            provider_reason = reason
    elif not binding_diagnostics:
        provider_status = "disabled_no_bindings"
        provider_reason = "NO_PROVIDER_BINDINGS_CONFIGURED"
    elif provider_invalid:
        provider_status = "invalid"
        provider_reason = provider_invalid[0]
    else:
        provider_status = "bound"
        provider_reason = "OK"

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
                "inverter_group": item.get("inverter_group"),
                "surface_count": item.get("surface_count"),
                "surfaces": item.get("surfaces", []),
                "kwp": item.get("kwp"),
                "provider_binding_states": sorted(
                    item.get("provider_binding_states", []),
                    key=lambda binding: str(binding.get("binding_id") or ""),
                ),
            }
            for item in resources
        ],
        # Nur stabile Binding-IDs und elektrische Zuordnung werden
        # veröffentlicht; Provider-Resource-IDs bleiben privat.
        "provider_bindings": binding_diagnostics,
        "provider_status": provider_status,
        "provider_reason": provider_reason,
        "limits_w": {
            "e3dc_dc_configured": round(e3dc_limit, 3) if e3dc_limit > 0.0 else None,
            "external_ac_inverter": round(external_limit, 3) if external_limit > 0.0 else None,
        },
    }
    provider_binding_identity_digest = _provider_binding_identity_digest(
        cfg,
        explicit_contract=use_explicit_contract,
        explicit_bindings=explicit_bindings,
    )
    revision_material = {
        "public_topology": material,
        # Der private Digest verhindert stale Rebindings, ohne Resource-IDs
        # oder Accountmaterial in Diagnose- oder Planflächen auszugeben.
        "provider_binding_identity_digest": provider_binding_identity_digest,
    }
    return {
        **material,
        "revision": _canonical_hash(revision_material),
        "resource_count": len(resources),
        "missing_resource_keys": sorted(missing),
        "missing_geometry_resource_keys": sorted(missing_geometry),
        "invalid_resource_keys": sorted(invalid),
        "invalid_provider_reasons": sorted(provider_invalid),
        # Local geometry/coupling is the source of truth. Provider failures
        # remain model-local and must not disable complete M1/M2 geometry.
        "split_usable": status == "bound",
        "provider_split_usable": provider_status == "bound",
        "e3dc_dc_bound": bool(e3dc_bound),
        "external_ac_bound": bool(external_bound),
        "contract_mode": "explicit_generator_groups" if use_explicit_contract else "legacy_read_compatibility",
        "config_schema": TOPOLOGY_CONFIG_SCHEMA if use_explicit_contract else None,
        "provider_bindings": binding_diagnostics,
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
    raw = {}
    for key, value in dict(resource_contributions_w or {}).items():
        parsed = _safe_float(value, None)
        # ``None`` and malformed values mean Missing, not a zero-watt roof.
        if parsed is not None:
            raw[str(key)] = max(0.0, parsed)
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
                    "forecast_w": round(raw[key], 3) if key in raw else None,
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
