#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plausibilitätsprüfung der Konfiguration gegen aktuelle E3DC-/RSCP-Werte.

Die Prüfung ist bewusst beratend: Nutzereingaben behalten Vorrang, wo der
Manager sie bereits unterstützt; starke Abweichungen von aktuellen E3DC-Werten
werden jedoch in der WebUI deutlich angezeigt.
"""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import tempfile
import time
from typing import Any, Dict, Iterable, Optional, Tuple

try:
    from .heatpump_pv_contract import (
        heatpump_pv_config as _heatpump_pv_config,
        heatpump_start_semantics, heatpump_idle_threshold_w,
        HEATPUMP_START_SEMANTICS_SG_READY,
        heatpump_start_reservation_duration_s,
    )
    from .consumer_priority import CONSUMER_MIN_W
except ImportError:  # pragma: no cover - direkter Skriptaufruf
    from heatpump_pv_contract import (
        heatpump_pv_config as _heatpump_pv_config,
        heatpump_start_semantics, heatpump_idle_threshold_w,
        HEATPUMP_START_SEMANTICS_SG_READY,
        heatpump_start_reservation_duration_s,
    )
    from consumer_priority import CONSUMER_MIN_W

try:
    from reserve import live_ep_reserve_details
except Exception:  # pragma: no cover - package import fallback
    from .reserve import live_ep_reserve_details  # type: ignore

try:
    from .config_secret_permissions import apply_config_secret_permissions
except Exception:  # pragma: no cover - direct script execution fallback
    from config_secret_permissions import apply_config_secret_permissions  # type: ignore
try:
    from .json_cache import atomic_write_on_change as _atomic_write_on_change
except Exception:  # pragma: no cover - direct script execution fallback
    from json_cache import atomic_write_on_change as _atomic_write_on_change  # type: ignore
try:
    from .aux_inverter_contract import effective_contract as _aux_inverter_contract
except Exception:  # pragma: no cover - direct script execution fallback
    from aux_inverter_contract import effective_contract as _aux_inverter_contract  # type: ignore
try:
    from .tariff_schedule import (
        supports_heat_price_boost,
        supports_spot_market_prices,
    )
except Exception:  # pragma: no cover - direct script execution fallback
    from tariff_schedule import (  # type: ignore
        supports_heat_price_boost,
        supports_spot_market_prices,
    )
try:
    from .market_economics import market_charge_profile_contract as _market_charge_profile_contract
except Exception:  # pragma: no cover - direct script execution fallback
    from market_economics import market_charge_profile_contract as _market_charge_profile_contract  # type: ignore
try:
    from .Heat.price_boost import parse_allowed_windows as _parse_heat_price_boost_windows
except Exception:  # pragma: no cover - direct script execution fallback
    from Heat.price_boost import parse_allowed_windows as _parse_heat_price_boost_windows  # type: ignore
try:
    from .pv_forecast_topology import (
        build_pv_forecast_topology,
        has_explicit_topology_config,
        legacy_provider_resource_duplicate_keys,
    )
except Exception:  # pragma: no cover - direct script execution fallback
    from pv_forecast_topology import (  # type: ignore
        build_pv_forecast_topology,
        has_explicit_topology_config,
        legacy_provider_resource_duplicate_keys,
    )
# Zusatzwechselrichter (Sungrow Modbus TCP, nur lesend) – Grenzen aus dem Treiber.
try:
    from .ext_inverter_modbus import (
        EXT_INVERTER_TYPES as _EXT_INVERTER_TYPES,
        DEFAULT_PORT as _EXT_INVERTER_DEFAULT_PORT,
        DEFAULT_UNIT_ID as _EXT_INVERTER_DEFAULT_UNIT_ID,
        DEFAULT_POLL_S as _EXT_INVERTER_DEFAULT_POLL_S,
        MIN_POLL_S as _EXT_INVERTER_MIN_POLL_S,
        MAX_POLL_S as _EXT_INVERTER_MAX_POLL_S,
        ext_inverter_settings as _ext_inverter_settings,
    )
except Exception:  # pragma: no cover - direct script execution fallback
    from ext_inverter_modbus import (  # type: ignore
        EXT_INVERTER_TYPES as _EXT_INVERTER_TYPES,
        DEFAULT_PORT as _EXT_INVERTER_DEFAULT_PORT,
        DEFAULT_UNIT_ID as _EXT_INVERTER_DEFAULT_UNIT_ID,
        DEFAULT_POLL_S as _EXT_INVERTER_DEFAULT_POLL_S,
        MIN_POLL_S as _EXT_INVERTER_MIN_POLL_S,
        MAX_POLL_S as _EXT_INVERTER_MAX_POLL_S,
        ext_inverter_settings as _ext_inverter_settings,
    )


RAMDISK = "/var/www/html/ramdisk"
CONFIG_VALIDATION_F = os.path.join(RAMDISK, "config_validation.json")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
            if value == "":
                return float(default)
        result = float(value)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def _configured_finite_number(
    cfg: Dict[str, Any],
    key: str,
    default: float,
) -> Tuple[float, bool]:
    raw = cfg.get(key, default)
    try:
        if isinstance(raw, str):
            raw = raw.strip().replace(",", ".")
        value = float(raw)
    except (TypeError, ValueError):
        return float(default), False
    return value, math.isfinite(value)


def _fmt_de(value: float, digits: int) -> str:
    """Zahl mit Dezimalkomma für Meldungstexte."""
    try:
        return ("%%.%df" % int(digits) % float(value)).replace(".", ",")
    except (TypeError, ValueError):
        return str(value)


def _market_profile_label(profile: str) -> str:
    return {
        "economic": "Wirtschaftlich",
        "balanced": "Ausgeglichen",
        "comfort": "Komfort",
        "custom": "Eigene Einstellungen",
    }.get(str(profile or ""), str(profile or ""))


def _has_user_value(cfg: Dict[str, Any], key: str) -> bool:
    if key not in cfg:
        return False
    raw = cfg.get(key)
    return raw is not None and str(raw).strip() != ""


def _is_enabled(cfg: Dict[str, Any], key: str) -> bool:
    return str(cfg.get(key, "0")).strip().lower() in {"1", "true", "yes", "on", "ein"}


def _direct_marketing_aux_ac_mode(cfg: Dict[str, Any]) -> Tuple[str, str]:
    """Normalisiert die aktuelle Nutzerfreigabe ohne Plan- oder Runtime-Fallback."""

    aliases = {
        "aus": "off",
        "disabled": "off",
        "reserve": "reserve_only",
        "reserve_sichern": "reserve_only",
        "reserve_only": "reserve_only",
        "hausversorgung": "house_supply",
        "hausversorgung_sichern": "house_supply",
        "house_supply": "house_supply",
        "wirtschaftlich": "economic",
        "economical": "economic",
        "economic": "economic",
    }
    for key in (
        "direct_marketing_aux_inverter_ac_storage_mode",
        "direct_marketing_pv_store_aux_ac_mode",
    ):
        if key not in cfg:
            continue
        raw = str(cfg.get(key) or "").strip().lower().replace("-", "_").replace(" ", "_")
        mode = aliases.get(raw, raw)
        if mode in {"off", "reserve_only", "house_supply", "economic"}:
            return mode, key
        return "off", f"{key}_invalid"
    if _is_enabled(cfg, "direct_marketing_aux_inverter_ac_storage_enable"):
        return "reserve_only", "legacy_bool_explicit_true"
    return "off", (
        "legacy_bool_explicit_false"
        if "direct_marketing_aux_inverter_ac_storage_enable" in cfg
        else "default_off"
    )


def _first_live_value(live: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[float], Optional[str]]:
    for key in keys:
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and abs(value) > 0.0001:
            return value, key
    return None, None


def _power_live_value(live: Dict[str, Any], keys: Iterable[str]) -> Tuple[Optional[float], Optional[str]]:
    dynamic_limit_keys = {
        "user_charge_limit_w",
        "used_charge_limit_w",
        "remaining_charge_w",
        "ems_max_charge_power_w",
        "user_discharge_limit_w",
        "used_discharge_limit_w",
        "remaining_discharge_w",
        "ems_max_discharge_power_w",
    }
    limits_active = str(live.get("power_limits_active", "")).strip().lower() in {"1", "true", "yes", "on"}
    for key in keys:
        if limits_active and key in dynamic_limit_keys:
            continue
        value = safe_float(live.get(key), float("nan"))
        if not math.isfinite(value) or abs(value) <= 0.0001:
            continue
        value = abs(value)
        if value >= 300.0:
            return value, key
    return None, None


def _battery_pack_count(live: Dict[str, Any]) -> int:
    for key in (
        "bat_total_dcb_count",
        "bat_dcb_count",
        "bat_pack_count",
        "battery_pack_count",
        "pack_count",
        "dcb_count",
    ):
        value = int(round(safe_float(live.get(key), 0.0)))
        if value > 1:
            return value
    return 1


def _normalise_capacity_kwh(live: Dict[str, Any], value: float, key: str) -> Tuple[float, str]:
    """Normalisiert E3DC-Kapazitätswerte, die teilweise je Batteriepack gemeldet werden."""
    pack_count = _battery_pack_count(live)
    if key in {"bat_usable_kwh", "bat_full_cap_kwh", "bat_capacity_kwh"}:
        if pack_count > 1 and 0.1 < value < 5.0:
            return round(value * pack_count, 3), f"{key}*bat_dcb_count"
    return value, key


def _capacity_from_specification(usable_kwh: float, full_kwh: float, specified_kwh: float, key: str) -> Tuple[Optional[float], Optional[str]]:
    if specified_kwh <= 0.1:
        return None, None
    usable = usable_kwh
    if usable <= 0.1:
        usable = full_kwh
    if usable <= 0.1 or usable > specified_kwh * 1.15 or usable < specified_kwh * 0.45:
        return round(specified_kwh * 0.9, 3), f"{key}_specified_kwh*0.9"
    return round(usable, 3), key


def _cabinet_capacity_live_value(live: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    cabinets = []
    for prefix in ("bat", "bat1", "bat2", "bat3"):
        usable = safe_float(live.get(f"{prefix}_usable_kwh"), float("nan"))
        full = safe_float(live.get(f"{prefix}_full_cap_kwh"), float("nan"))
        specified = safe_float(live.get(f"{prefix}_specified_kwh"), float("nan"))
        voltage = safe_float(live.get(f"{prefix}_v"), 0.0)
        active = voltage > 5.0 or (math.isfinite(specified) and specified > 0.1)
        if not active:
            continue
        if not math.isfinite(usable):
            usable = 0.0
        if not math.isfinite(full):
            full = 0.0
        if not math.isfinite(specified):
            specified = 0.0
        value, source = _capacity_from_specification(usable, full, specified, prefix)
        if value is None and usable > 0.1:
            value, source = _normalise_capacity_kwh(live, usable, f"{prefix}_usable_kwh")
        if value is not None and value > 0.1:
            cabinets.append((value, source or f"{prefix}_usable_kwh"))
    if not cabinets:
        return None, None
    total = round(sum(value for value, _source in cabinets), 3)
    sources = "+".join(source for _value, source in cabinets)
    return total, sources


def _capacity_live_value(live: Dict[str, Any]) -> Tuple[Optional[float], Optional[str]]:
    for key in (
        "bat_total_usable_kwh",
        "bat_total_full_cap_kwh",
        "bat_total_specified_kwh",
    ):
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and value > 0.1:
            if key == "bat_total_specified_kwh":
                return round(value * 0.9, 3), f"{key}*0.9"
            return value, key
    cabinet_value, cabinet_key = _cabinet_capacity_live_value(live)
    if cabinet_value is not None:
        return cabinet_value, cabinet_key
    for key in (
        "real_usable_capacity_kwh",
        "usable_capacity_kwh",
        "bat_usable_kwh",
        "bat_capacity_kwh",
        "bat_full_cap_kwh",
    ):
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and value > 0.1:
            return _normalise_capacity_kwh(live, value, key)
    for key in (
        "real_usable_capacity_wh",
        "usable_capacity_wh",
        "installed_capacity_wh",
        "battery_capacity_wh",
    ):
        value = safe_float(live.get(key), float("nan"))
        if math.isfinite(value) and value > 100.0:
            return value / 1000.0, key
    return None, None


def _deviation(configured: float, live_value: float) -> Tuple[float, float]:
    delta = abs(configured - live_value)
    rel = delta / max(abs(live_value), 1.0)
    return delta, rel


def _entry(
    *,
    key: str,
    label: str,
    unit: str,
    configured: Any,
    live_value: Optional[float],
    live_key: Optional[str],
    effective: Any,
    source: str,
    severity: str,
    message: str,
) -> Dict[str, Any]:
    return {
        "key": key,
        "label": label,
        "unit": unit,
        "configured": configured,
        "rscp": live_value,
        "rscp_key": live_key,
        "effective": effective,
        "source": source,
        "severity": severity,
        "message": message,
    }


def _warning_count(*groups: Dict[str, Dict[str, Any]]) -> int:
    return sum(1 for group in groups for item in group.values() if item.get("severity") == "warning")


def validate_solcast_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Validiert die nichtgeheime Site-zu-Account-Zuordnung fail-closed."""
    cfg = {str(k).lower(): v for k, v in (cfg or {}).items()}
    key_available = {
        1: bool(str(cfg.get("solcast_api_key") or "").strip()),
        2: bool(str(cfg.get("solcast_api_key_2") or "").strip()),
    }
    legacy_secondary_slot = 2 if key_available[2] else 1
    duplicate_labels = (
        set()
        if has_explicit_topology_config(cfg)
        else set(legacy_provider_resource_duplicate_keys(cfg))
    )
    result: Dict[str, Dict[str, Any]] = {}
    for label, resource_key, mapping_key, legacy_slot in (
        ("FC1", "solcast_resource_id", "solcast_api_slot_fc1", 1),
        ("FC2", "solcast_resource_id_2", "solcast_api_slot_fc2", legacy_secondary_slot),
        ("FC3", "solcast_resource_id_3", "solcast_api_slot_fc3", legacy_secondary_slot),
        ("FC4", "solcast_resource_id_4", "solcast_api_slot_fc4", legacy_secondary_slot),
    ):
        resource_configured = bool(str(cfg.get(resource_key) or "").strip())
        raw_slot = str(cfg.get(mapping_key) or "").strip()
        explicit = bool(raw_slot)
        valid_slot = raw_slot in {"1", "2"} if explicit else True
        effective_slot = int(raw_slot) if valid_slot and explicit else int(legacy_slot)
        severity = "ok"
        message = f"{label} nutzt Konto {effective_slot}."
        if not resource_configured:
            severity = "info"
            message = f"{label} ist nicht konfiguriert."
        elif label in duplicate_labels:
            severity = "warning"
            message = (
                f"{label}: Resource ID ist mehrfach gebunden; "
                "M3 bleibt vollständig gesperrt."
            )
        elif not valid_slot:
            severity = "warning"
            message = f"{label}: Accountslot muss 1 oder 2 sein; M3 bleibt gesperrt."
        elif not key_available[effective_slot]:
            severity = "warning"
            message = f"{label}: Gewähltes Konto {effective_slot} besitzt keinen Schlüssel; M3 bleibt gesperrt."
        result[mapping_key] = _entry(
            key=mapping_key,
            label=f"Solcast {label} Konto",
            unit="-",
            configured=int(raw_slot) if raw_slot in {"1", "2"} else None,
            live_value=None,
            live_key=None,
            effective=effective_slot if valid_slot else None,
            source="user" if explicit else "legacy_default",
            severity=severity,
            message=message,
        )
    return result


def validate_pv_forecast_topology_config(cfg: Optional[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Projiziert den typisierten Topologievertrag in die Config-Diagnose."""

    cfg = {str(k).lower(): v for k, v in (cfg or {}).items()}
    contract = build_pv_forecast_topology(cfg)
    result: Dict[str, Dict[str, Any]] = {}
    diagnostics_raw = str(cfg.get("forecast_diagnostics_enable", "0")).strip().lower()
    diagnostics_valid = diagnostics_raw in {
        "",
        "0",
        "1",
        "false",
        "true",
        "off",
        "on",
        "no",
        "yes",
        "nein",
        "ein",
    }
    diagnostics_enabled = diagnostics_raw in {"1", "true", "on", "yes", "ein"}
    result["forecast_diagnostics_enable"] = _entry(
        key="forecast_diagnostics_enable",
        label="PV-Prognosediagnose",
        unit="-",
        configured=diagnostics_raw if _has_user_value(cfg, "forecast_diagnostics_enable") else None,
        live_value=None,
        live_key=None,
        effective=diagnostics_enabled if diagnostics_valid else False,
        source="user" if _has_user_value(cfg, "forecast_diagnostics_enable") else "default",
        severity="info" if diagnostics_valid else "warning",
        message=(
            "Rein diagnostische Auswertung; keine automatische Regelungs- oder Modellwirkung."
            if diagnostics_enabled
            else "Diagnose ist ausgeschaltet und erzeugt keine Hintergrundlast."
            if diagnostics_valid
            else "Ungültiger Schalterwert; Diagnose bleibt ausgeschaltet."
        ),
    )
    explicit_contract = contract.get("contract_mode") == "explicit_generator_groups"
    if explicit_contract:
        group_count = int(contract.get("resource_count") or 0)
        binding_count = len(contract.get("provider_bindings") or [])
        topology_ok = contract.get("status") == "bound"
        provider_status = str(contract.get("provider_status") or "")
        provider_ok = provider_status in {"bound", "disabled_no_bindings"}
        result["pv_forecast_topology_config"] = _entry(
            key="pv_forecast_topology_config",
            label="PV-Prognose-Topologie",
            unit="-",
            configured=group_count if _has_user_value(cfg, "pv_forecast_topology_config") else None,
            live_value=None,
            live_key=None,
            effective={"generator_groups": group_count, "provider_resources": binding_count},
            source="user",
            severity="ok" if topology_ok and provider_ok else "warning",
            message=(
                (
                    f"Lokale Topologie enthält {group_count} Generatorgruppe(n); "
                    f"Providerbindung bleibt gesperrt ({contract.get('provider_reason')})."
                )
                if topology_ok and not provider_ok
                else f"Schema enthält {group_count} Generatorgruppe(n) und {binding_count} Providerressource(n)."
                if topology_ok
                else f"Topologievertrag ist nicht vollständig bindbar ({contract.get('reason')}); Quellsplit bleibt gesperrt."
            ),
        )
    by_key = {
        str(item.get("coupling_key")): item
        for item in contract.get("resources", [])
        if isinstance(item, dict) and item.get("coupling_key")
    }
    for _label, _forecast_key, _resource_key, coupling_key in (
        ("FC1", "forecast1", "solcast_resource_id", "pv_forecast_coupling_fc1"),
        ("FC2", "forecast2", "solcast_resource_id_2", "pv_forecast_coupling_fc2"),
        ("FC3", "forecast3", "solcast_resource_id_3", "pv_forecast_coupling_fc3"),
        ("FC4", None, "solcast_resource_id_4", "pv_forecast_coupling_fc4"),
    ):
        item = by_key.get(coupling_key)
        configured = cfg.get(coupling_key) if _has_user_value(cfg, coupling_key) else None
        if item is None:
            severity = "info"
            message = f"{_label} ist nicht als Forecastressource konfiguriert."
            effective = None
        elif not item.get("has_geometry"):
            severity = "warning"
            effective = None
            message = (
                f"{_label} besitzt keine lokale Geometrie; "
                "M1/M2-Quellsplit bleibt gesperrt."
            )
        elif item.get("coupling") in {"E3DC_DC", "EXTERNAL_AC"}:
            severity = "ok"
            effective = item.get("coupling")
            message = f"{_label} ist eindeutig an {effective} gebunden."
        else:
            severity = "warning"
            effective = None
            message = f"{_label} ist topologisch nicht gebunden; zusätzlicher DC-Headroom bleibt gesperrt."
        result[coupling_key] = _entry(
            key=coupling_key,
            label=f"PV-Topologie {_label}",
            unit="-",
            configured=configured,
            live_value=None,
            live_key=None,
            effective=effective,
            source="user" if configured is not None else "missing",
            severity=severity,
            message=message,
        )

    external_limit = safe_float(cfg.get("pv_external_ac_inverter_limit_w"), 0.0)
    external_required = bool(contract.get("external_ac_bound"))
    result["pv_external_ac_inverter_limit_w"] = _entry(
        key="pv_external_ac_inverter_limit_w",
        label="Externer AC-Wechselrichter",
        unit="W",
        configured=external_limit if _has_user_value(cfg, "pv_external_ac_inverter_limit_w") else None,
        live_value=None,
        live_key=None,
        effective=external_limit if external_limit > 0.0 else None,
        source="user" if external_limit > 0.0 else "missing",
        severity="warning" if external_required and external_limit <= 0.0 else "ok",
        message=(
            "Für EXTERNAL_AC fehlt das separate AC-WR-Limit; der Topologiesplit bleibt gesperrt."
            if external_required and external_limit <= 0.0
            else "Das externe AC-WR-Limit ist typisiert gebunden."
            if external_limit > 0.0
            else "Kein externer AC-Wechselrichter gebunden."
        ),
    )
    e3dc_limit = safe_float(cfg.get("pv_e3dc_dc_inverter_limit_w"), 0.0)
    meter_mode = cfg.get("pv_external_ac_observation_mode", "unverified")
    known_meter_modes = ("unverified", "generation_meter", "generation_meter_uncontrolled")
    result["pv_external_ac_observation_mode"] = _entry(
        key="pv_external_ac_observation_mode", label="Ist-Messung des Zusatzwechselrichters", unit="",
        configured=meter_mode, live_value=None, live_key=None,
        effective=meter_mode if meter_mode in known_meter_modes else "unverified",
        source="user" if meter_mode in known_meter_modes[1:] else "unverified",
        severity="info" if meter_mode in known_meter_modes else "warning",
        message="Die Betreiberangabe gilt ab Erfassung. Begrenzungsstatus und AC-Limit werden separat geprüft."
                if meter_mode in known_meter_modes[1:] else "Die Messzuordnung ist noch nicht bestätigt; kein neuer Kalibrierfaktor aus diesen Messungen.",
    )
    result["pv_e3dc_dc_inverter_limit_w"] = _entry(
        key="pv_e3dc_dc_inverter_limit_w",
        label="E3DC-DC-Wechselrichterlimit",
        unit="W",
        configured=e3dc_limit if _has_user_value(cfg, "pv_e3dc_dc_inverter_limit_w") else None,
        live_value=None,
        live_key=None,
        effective=e3dc_limit if e3dc_limit > 0.0 else None,
        source="user" if e3dc_limit > 0.0 else "rscp_runtime",
        severity="ok" if e3dc_limit > 0.0 else "info",
        message=(
            "Konfiguriertes E3DC-DC-Limit dient als Fallback zum frischen PVI-Readback."
            if e3dc_limit > 0.0
            else "Ohne Konfigwert muss der frische PVI-Readback das E3DC-DC-Limit liefern."
        ),
    )
    return result


def validate_heatpump_pv_config(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Beratende Prüfung; fehlende Deckung sperrt nur neue optionale PV-Starts."""
    if not _is_enabled(cfg, "luxtronik") or safe_float(cfg.get("wp_type"), -1.0) != 0:
        return {}
    profile = _heatpump_pv_config(cfg)
    measured = profile.get("control_mode") == "measured"
    entries = {}
    invalid_fields = []
    rules = (
        ("wp_pv_max_power_w", "WP: maximale elektrische Aufnahme", "W", "max_power_w", 0.0),
        ("wp_pv_battery_limit_wh", "WP: Akkuenergie in 24 Stunden", "Wh", "battery_limit_wh", 0.0),
        ("wp_pv_grid_limit_wh", "WP: Netzenergie in 24 Stunden", "Wh", "grid_limit_wh", 0.0),
        ("wp_pv_battery_max_w", "WP: Akku-Überbrückungsleistung", "W", "battery_max_w", 0.0),
        ("wp_pv_grid_max_w", "WP: Netz-Überbrückungsleistung", "W", "grid_max_w", 0.0),
        ("wp_pv_reaction_s", "WP: Reaktionsfrist", "s", "reaction_s", 30.0),
        ("wp_pv_start_wait_s", "WP: Verdichter-Startwartefrist", "s", "start_wait_s", 600.0),
        ("wp_pv_handoff_timeout_s", "WP: Übergabefrist Wallbox", "s", "handoff_timeout_s", 120.0),
    )
    if measured or "wp_pv_start_power_w" in cfg:
        rules += (("wp_pv_start_power_w", "WP: Startleistung", "W", "start_power_w", 0.0),)
    mode = str(cfg.get("wp_pv_control_mode", "reserved")).strip().lower()
    if mode not in {"measured", "reserved"}:
        invalid_fields.append("wp_pv_control_mode")
    for key, label, unit, profile_key, default in rules:
        raw = cfg.get(key, default)
        try:
            numeric = float(raw)
            valid = not isinstance(raw, bool) and math.isfinite(numeric) and numeric >= 0
        except (TypeError, ValueError, OverflowError):
            valid = False
        effective = profile[profile_key]
        if key in {"wp_pv_reaction_s", "wp_pv_handoff_timeout_s"} or (key == "wp_pv_max_power_w" and not measured):
            valid = valid and effective > 0
        elif key == "wp_pv_start_wait_s":
            valid = valid and effective >= profile["signal_hold_s"]
        message = "Konfigurationswert ist plausibel; aktuelle Quellen- und Schutzgrenzen werden zusätzlich geprüft."
        if not valid:
            invalid_fields.append(key)
            message = "Profilwert fehlt oder ist ungültig. Neue optionale PV-Starts warten; normale Heizung und Warmwasser bleiben unabhängig."
        elif key == "wp_pv_max_power_w" and effective == 0:
            message = "Keine elektrische Obergrenze eingetragen. Die Automatik verwendet die Startleistung; aktuelle Quellen- und Hardwaregrenzen bleiben wirksam."
        elif key == "wp_pv_start_power_w":
            message = "Startwert für die Überschussqualifikation. 0 übernimmt die bestehende Start-Grenze; nach dem Start zählt die gemessene Aufnahme."
        elif effective == 0:
            message = "Diese Überbrückungsquelle ist gesperrt. Leistung und Wh-Kontingent müssen beide größer als null sein."
        entries[key] = _entry(
            key=key, label=label, unit=unit,
            configured=raw if _has_user_value(cfg, key) else None,
            live_value=None, live_key=None, effective=effective,
            source="user" if _has_user_value(cfg, key) else "default",
            severity="ok" if valid else "warning", message=message,
        )
    for key, default, positive in (("wp_min_runtime_min", 30.0, True),
                                   ("wp_restart_block_min", 20.0, False)):
        raw = cfg.get(key, default)
        try:
            numeric = float(raw)
            valid = not isinstance(raw, bool) and math.isfinite(numeric) and (
                numeric > 0 if positive else numeric >= 0
            )
        except (TypeError, ValueError, OverflowError):
            valid = False
        if not valid:
            invalid_fields.append(key)
    duration_s = (profile["reaction_s"] if measured else
                  profile["min_runtime_s"] + profile["start_wait_s"] + profile["reaction_s"])
    hours = duration_s / 3600.0
    reserve_power_w = (profile["max_power_w"] or profile.get("start_power_w", 0.0)) if measured else profile["max_power_w"]
    if measured:
        reserve_power_w = min(reserve_power_w, sum(
            profile[source + "_max_w"]
            for source in ("battery", "grid") if profile[source + "_limit_wh"] > 0.0
        ))
    required_wh = reserve_power_w * hours
    source_available_wh = {
        source: min(profile[source + "_limit_wh"], profile[source + "_max_w"] * hours)
        for source in ("battery", "grid")
    }
    available_wh = sum(source_available_wh.values())
    missing_wh = max(0.0, required_wh - available_wh)
    funded = profile["valid"] and available_wh + 1e-6 >= required_wh
    message = (
        "Die eingestellten Quellen können rechnerisch die volle Schutzfrist tragen. Bereits verbrauchte oder gebundene Wh, Speicherreserve und aktuelle Leistungsgrenzen werden vor jedem Start zusätzlich berücksichtigt."
        if funded else
        "Die eingestellte Überbrückungsenergie reicht für die abgesicherte Start- und Laufzeit nicht aus. Neue zusätzliche PV-Starts warten; normale Heizung und Warmwasserbereitung bleiben unabhängig."
    )
    if measured:
        message = (
            "Die eingestellten Quellen decken rechnerisch den kurzen Puffer ihrer erlaubten Überbrückungsleistung. Die Startfreigabe folgt der eingestellten PV-Startleistung und Startverzögerung; im Lauf zählt die Istaufnahme. Erreichte Wh-Wächter beenden den zusätzlichen Boost nach der geschützten Laufzeit; der Verbrauch bis dahin wird weitergezählt."
            if funded else
            "Das Quellenkontingent begrenzt die mögliche Überbrückungsleistung innerhalb der Reaktionsfrist. Es ist keine zusätzliche PV-Startsperre. Für den Start müssen die eingestellte PV-Startleistung und Startverzögerung erfüllt sein."
        )
    if not profile["valid"]:
        message = "Das Leistungsprofil ist noch unvollständig oder ungültig. Bitte die markierten Profilwerte prüfen. Normale Heizung und Warmwasserbereitung bleiben unabhängig."
    entries["wp_pv_energy_reservation"] = _entry(
        key="wp_pv_energy_reservation", label="WP: Energie vor PV-Start", unit="Wh",
        configured=None, live_value=None, live_key=None, effective=required_wh,
        source="derived", severity="ok" if funded else "warning",
        message=message,
    )
    # Zahlen aus derselben Prüfung für die Anzeige bereitstellen. Sie beschreiben
    # Konfigurationsgrenzen, weder Live-Restkontingente noch E3DC-Messwerte.
    entries["wp_pv_energy_reservation"]["calculation"] = {
        "control_mode": profile.get("control_mode", "reserved"),
        "start_power_w": profile.get("start_power_w", 0.0),
        "reservation_power_w": reserve_power_w,
        "battery_limit_wh": profile["battery_limit_wh"],
        "grid_limit_wh": profile["grid_limit_wh"],
        "max_power_w": profile["max_power_w"],
        "min_runtime_s": profile["min_runtime_s"],
        "start_wait_s": profile["start_wait_s"],
        "reaction_s": profile["reaction_s"],
        "duration_s": duration_s,
        "required_wh": required_wh,
        "available_wh": available_wh,
        "missing_wh": missing_wh,
        "battery_available_wh": source_available_wh["battery"],
        "grid_available_wh": source_available_wh["grid"],
        "profile_valid": profile["valid"],
        "invalid_fields": invalid_fields,
    }
    return entries


def _ext_inverter_int_entry(
    cfg: Dict[str, Any],
    key: str,
    label: str,
    unit: str,
    default: int,
    minimum: int,
    maximum: int,
    active: bool,
) -> Dict[str, Any]:
    """Ganzzahl im Bereich [minimum, maximum]; ungültige Nutzerwerte fallen auf den Standard."""
    user = _has_user_value(cfg, key)
    candidate = None
    if user:
        try:
            candidate = int(float(str(cfg.get(key)).strip().replace(",", ".")))
        except (TypeError, ValueError):
            candidate = None
    valid = bool(candidate is not None and minimum <= candidate <= maximum)
    effective = candidate if valid else default
    if user and not valid:
        severity = "warning"
        message = "%s muss eine ganze Zahl zwischen %d und %d sein; wirksam ist der Standard %d." % (label, minimum, maximum, default)
    elif not active:
        severity = "ok"
        message = "Nur mit Typ sungrow_modbus wirksam."
    else:
        severity = "ok"
        message = "%s der Modbus-TCP-Direktlesung (Standard %d)." % (label, default)
    return _entry(
        key=key,
        label=label,
        unit=unit,
        configured=candidate if valid else (cfg.get(key) if user else None),
        live_value=None,
        live_key=None,
        effective=effective,
        source="user" if valid else ("invalid" if user else "default"),
        severity=severity,
        message=message,
    )


def validate_ext_inverter_config(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Zusatzwechselrichter (Sungrow, Modbus TCP, nur lesend) – Typ, IPv4, Port,
    Unit-ID und Abfrageintervall beratend prüfen. Die Regelung bleibt beim E3DC-Messwert; die Direktlesung
    liefert nur Phasen-/String-Daten und Diagnose."""
    entries: Dict[str, Dict[str, Any]] = {}
    settings = _ext_inverter_settings(cfg)
    kind_user = _has_user_value(cfg, "ext_inverter_type")
    kind_raw = str(cfg.get("ext_inverter_type") if kind_user else "").strip().lower()
    kind_valid = (not kind_user) or kind_raw in _EXT_INVERTER_TYPES
    active = settings["type"] != "none"
    if kind_user and not kind_valid:
        kind_severity = "warning"
        kind_message = "Der Typ des Zusatzwechselrichters kennt nur none oder sungrow_modbus; wirksam ist none (keine Direktlesung)."
    elif active and settings["enabled"]:
        kind_severity = "info"
        kind_message = (
            "Direktlesung aktiv (nur lesend): Der Live-Dienst liest den Sungrow-Wechselrichter alle %d s per Modbus TCP "
            "und stellt Phasen-, String- und Diagnosedaten bereit. Die Regelung nutzt weiterhin den E3DC-Messwert." % settings["poll_s"]
        )
    elif active:
        kind_severity = "warning"
        kind_message = "Direktlesung konfiguriert, aber deaktiviert: %s. IP-Adresse, Port, Unit-ID und Intervall prüfen." % settings["error"]
    else:
        kind_severity = "ok"
        kind_message = "Kein Zusatzwechselrichter (Standard). Nur setzen, wenn ein zweiter Wechselrichter über LAN erreichbar ist."
    entries["ext_inverter_type"] = _entry(
        key="ext_inverter_type",
        label="Zusatzwechselrichter Typ",
        unit="-",
        configured=kind_raw if kind_user else None,
        live_value=None,
        live_key=None,
        effective=settings["type"],
        source="user" if (kind_user and kind_valid) else ("invalid" if kind_user else "default"),
        severity=kind_severity,
        message=kind_message,
    )
    ip_user = _has_user_value(cfg, "ext_inverter_ip")
    ip_raw = str(cfg.get("ext_inverter_ip") if ip_user else "").strip()
    try:
        ipaddress.IPv4Address(ip_raw)
        ip_valid = True
    except (ipaddress.AddressValueError, ValueError):
        ip_valid = False
    if active and not ip_valid:
        ip_severity = "warning"
        ip_message = "IPv4-Adresse des Zusatzwechselrichters fehlt oder ist ungültig; die Direktlesung bleibt deaktiviert."
    elif ip_user and not ip_valid:
        ip_severity = "warning"
        ip_message = "Keine gültige IPv4-Adresse (z. B. 192.0.2.10)."
    elif not active:
        ip_severity = "ok"
        ip_message = "Nur mit Typ sungrow_modbus wirksam."
    else:
        ip_severity = "ok"
        ip_message = "Adresse des Wechselrichters bzw. seines Kommunikationsmoduls (Modbus TCP)."
    entries["ext_inverter_ip"] = _entry(
        key="ext_inverter_ip",
        label="Zusatzwechselrichter IP",
        unit="-",
        configured=ip_raw if ip_user else None,
        live_value=None,
        live_key=None,
        effective=ip_raw if (ip_valid and active) else "",
        source="user" if ip_valid else ("invalid" if ip_user else "default"),
        severity=ip_severity,
        message=ip_message,
    )
    entries["ext_inverter_port"] = _ext_inverter_int_entry(
        cfg, "ext_inverter_port", "Modbus-TCP-Port", "", _EXT_INVERTER_DEFAULT_PORT, 1, 65535, active
    )
    entries["ext_inverter_unit_id"] = _ext_inverter_int_entry(
        cfg, "ext_inverter_unit_id", "Modbus-Unit-ID", "", _EXT_INVERTER_DEFAULT_UNIT_ID, 0, 247, active
    )
    entries["ext_inverter_poll_s"] = _ext_inverter_int_entry(
        cfg, "ext_inverter_poll_s", "Abfrageintervall", "s", _EXT_INVERTER_DEFAULT_POLL_S,
        _EXT_INVERTER_MIN_POLL_S, _EXT_INVERTER_MAX_POLL_S, active
    )
    return entries


_WP_BUFFER_SENSORS = {
    "none": None,
    "luxtronik_ruecklauf_extern": 0,
    "stiebel_puffer": 4,
}


def validate_heatpump_display_config(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Beratende Prüfung der neuen WP-Ansicht (nur Anzeige, keine Regelwirkung)."""
    entries = {}
    wp_type = int(safe_float(cfg.get("wp_type"), -1.0))
    if _has_user_value(cfg, "wp_buffer_sensor"):
        raw = str(cfg.get("wp_buffer_sensor")).strip().lower()
        if raw not in _WP_BUFFER_SENSORS:
            severity = "warning"
            effective = "none"
            message = "Unbekannter Pufferfühler; die Ansicht zeigt keinen Pufferspeicher. Erlaubt: keiner, externer Rücklauf der Luxtronik oder Pufferfühler der Stiebel-ISG."
        elif _WP_BUFFER_SENSORS[raw] is not None and _WP_BUFFER_SENSORS[raw] != wp_type:
            severity = "warning"
            effective = raw
            message = "Der gewählte Pufferfühler passt nicht zum Wärmepumpen-Typ; die Ansicht zeigt den Puffer ohne Wert („--“)."
        else:
            severity = "ok"
            effective = raw
            message = "Pufferfühler für die Anzeige der neuen Wärmepumpen-Ansicht."
        entries.update({
            "wp_buffer_sensor": _entry(
                key="wp_buffer_sensor", label="WP-Ansicht: Pufferfühler", unit="",
                configured=cfg.get("wp_buffer_sensor"), live_value=None, live_key=None,
                effective=effective, source="user" if severity == "ok" else "invalid",
                severity=severity, message=message,
            )
        })
    if _has_user_value(cfg, "wp_heating_circuit_pump"):
        raw = str(cfg.get("wp_heating_circuit_pump")).strip().lower()
        if raw not in {"hup", "fup1", "zup", "none"}:
            severity = "warning"
            effective = None
            message = "Unbekanntes Heizkreispumpensignal; der Heizkreiszustand bleibt in der Ansicht unbekannt. Erlaubt: HUP, FUP 1, ZUP oder Kein Pumpensignal."
        elif wp_type != 0 and raw != "none":
            severity = "warning"
            effective = "none"
            message = "Das Heizkreispumpensignal ist nur für Luxtronik wählbar; bei diesem Wärmepumpen-Typ bleibt die bisherige Anzeige erhalten."
        else:
            severity = "ok"
            effective = raw
            message = "Heizkreispumpensignal für die Anzeige der neuen Wärmepumpen-Ansicht; keine Regelwirkung."
        entries["wp_heating_circuit_pump"] = _entry(
            key="wp_heating_circuit_pump", label="WP-Ansicht: Heizkreispumpe", unit="",
            configured=cfg.get("wp_heating_circuit_pump"), live_value=None, live_key=None,
            effective=effective, source="user" if severity == "ok" else "invalid",
            severity=severity, message=message,
        )
    return entries


def _address_configured(cfg: Dict[str, Any], key: str) -> bool:
    return str(cfg.get(key, "") or "").strip().lower() not in {"", "0", "0.0.0.0", "none", "null"}


def validate_stiebel_sg_ready_config(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Beratende Prüfung des experimentellen SG-Ready-Ausgangs über das Stiebel-ISG.

    Keine Sperre: Der Energy Manager prüft dieselben Bedingungen zur Laufzeit
    und schreibt ohne sie nichts. Die Meldung erklärt nur, warum der Schalter
    wirkt oder nicht wirkt.
    """
    if not _is_enabled(cfg, "stiebel_isg_sg_ready_write"):
        return {}
    wp_type = int(safe_float(cfg.get("wp_type"), -1.0))
    severity = "warning"
    effective = 0
    if wp_type != 4:
        message = "Wirkt nur beim Wärmepumpen-Typ Stiebel Eltron ISG / WPM; es wird nichts geschrieben."
    elif not _is_enabled(cfg, "luxtronik"):
        message = "WP-/Verbrauchslogging ist aus; der SG-Ready-Ausgang bleibt inaktiv."
    elif not _address_configured(cfg, "stiebel_isg_ip"):
        message = "ISG-Adresse fehlt; der SG-Ready-Ausgang bleibt inaktiv."
    elif _address_configured(cfg, "shelly_sg_ip") or _address_configured(cfg, "shelly_pause_ip"):
        message = (
            "Ein Shelly-SG-Ready- oder EVU-Kontakt ist eingetragen und behält Vorrang. "
            "Das ISG wird nicht beschrieben; nur eine SG-Ready-Steuerung darf aktiv sein."
        )
    elif str(cfg.get("auto_mode", "1")).strip().lower() in {"0", "false", "off", "aus", "nein"}:
        message = "„Automatik darf Geräte steuern“ ist aus; der SG-Ready-Ausgang schreibt dann nicht."
    else:
        severity = "ok"
        effective = 1
        message = (
            "Experimentell: Der Wärmepumpen-Manager schreibt bei Zustandswechseln der zentralen "
            "Entscheidung nur SG-Ready-Eingang 1 (4002). Voraussetzungen im WPM: SG Ready aktiviert, "
            "SG-Ready-Eingang = Modbus, Sicherheitstemperaturbegrenzer im Heizungsvorlauf, keine "
            "zweite SG-Ready-Steuerung. Wirksam nach Neustart des Wärmepumpen-Managers."
        )
    return {
        "stiebel_isg_sg_ready_write": _entry(
            key="stiebel_isg_sg_ready_write", label="Stiebel: SG Ready schreiben (experimentell)", unit="",
            configured=cfg.get("stiebel_isg_sg_ready_write"), live_value=None, live_key=None,
            effective=effective, source="user", severity=severity, message=message,
        )
    }


def validate_heatpump_start_config(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Beratende Startgrenzen; keine zusätzliche Betriebsfreigabe oder Sperre."""
    if not _is_enabled(cfg, "luxtronik"):
        return {}
    minimum = CONSUMER_MIN_W["heatpump"]
    start = abs(safe_float(cfg.get("grid_start_limit"), -3500.0))
    delay = safe_float(cfg.get("pv_boost_delay"), 30.0)
    sg_ready = heatpump_start_semantics(cfg) == HEATPUMP_START_SEMANTICS_SG_READY
    duration = heatpump_start_reservation_duration_s(dict(cfg, pv_boost_delay=0))
    entries = {}
    for key, label, unit, value, warning, message in (
        ("grid_start_limit", "WP-Startgrenze", "W", start, start < minimum,
         f"Der Betrag ist die benötigte Freigabe aus dem freien Verbraucherbudget nach Akkuladung. "
         f"Die Zuteilung benötigt mindestens {minimum} W; ein kleinerer Betrag ermöglicht keinen Start. "
         "Die Einspeisung am Netzpunkt ist nicht der Vergleichswert."),
        ("pv_boost_delay", "WP-Startverzögerung", "s", delay, delay >= duration,
         f"Die Startverzögerung erreicht ab {duration:g} s das normale Reservierungsfenster dieser Startart. "
         + ("Bei SG Ready wird das Fenster auf mindestens Verzögerung plus 120 s erweitert; "
            "lange Verzögerungen binden entsprechend lange Verbraucherbudget."
            if sg_ready else "Direkte Sollwertangebote behalten ihre kurze Budgetreservierung; die Verzögerung bitte prüfen.")),
    ):
        entries[key] = _entry(key=key, label=label, unit=unit, configured=cfg.get(key),
            live_value=None, live_key=None, effective=value, source="user",
            severity="warning" if warning else "ok", message=message)
    if str(cfg.get("wp_type", "")).strip() == "4":
        standby = safe_float(cfg.get("stiebel_isg_standby_w"), 35.0)
        entries["stiebel_isg_standby_w"] = _entry(
            key="stiebel_isg_standby_w", label="Stiebel Standby", unit="W",
            configured=cfg.get("stiebel_isg_standby_w"), live_value=None, live_key=None,
            effective=heatpump_idle_threshold_w(cfg), source="user",
            severity="warning" if not 0 <= standby <= 75 else "ok",
            message="Leerlaufgrenze: Standby plus 25 W Messspielraum, mindestens 50 und höchstens 100 W. "
                    "Nur frische Leistungsdaten, ein bestätigter stehender Verdichter und eine zurückgenommene "
                    "Startfreigabe erlauben das Entsperren. Standard 35 W ergibt eine Grenze von 60 W.")
    return entries


def validate_storage_config(cfg: Optional[Dict[str, Any]], live: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Liefert die beratende Prüfung der Speicherkonfiguration.

    Quellenreihenfolge:
    - Laden/Entladen/Kapazität: gültige Nutzereingabe -> RSCP-Livewert -> Projektstandard
    - Notstromreserve: Die E3DC-Livereserve ist die Schutzgrenze. Der konfigurierte
      Wert dient nur als zusätzlicher Rückfallboden und darf RSCP nicht unterschreiten.
    """

    cfg = {str(k).lower(): v for k, v in (cfg or {}).items()}
    aux_inverter_contract = _aux_inverter_contract(cfg)
    live = live or {}
    now = int(time.time())
    storage: Dict[str, Dict[str, Any]] = {}
    wallbox: Dict[str, Dict[str, Any]] = {}
    consumer: Dict[str, Dict[str, Any]] = {}
    consumer.update(validate_heatpump_pv_config(cfg))
    consumer.update(validate_heatpump_display_config(cfg))
    consumer.update(validate_stiebel_sg_ready_config(cfg))
    consumer.update(validate_heatpump_start_config(cfg))
    price: Dict[str, Dict[str, Any]] = {}
    forecast = validate_solcast_config(cfg)
    forecast.update(validate_pv_forecast_topology_config(cfg))
    forecast.update(validate_ext_inverter_config(cfg))

    power_rules = {
        "maximumladeleistung": {
            "label": "Max. Ladeleistung",
            "default": 12000.0,
            "live_keys": (
                "bat_charge_limit_w",
                "user_charge_limit_w",
            ),
        },
        "maximaleentladeleistung": {
            "label": "Max. Entladeleistung",
            "default": 11000.0,
            "live_keys": (
                "user_discharge_limit_w",
                "bat_discharge_limit_w",
            ),
        },
    }
    for key, rule in power_rules.items():
        configured = safe_float(cfg.get(key), float("nan")) if _has_user_value(cfg, key) else None
        live_value, live_key = _power_live_value(live, rule["live_keys"])
        source = "default"
        effective = float(rule["default"])
        severity = "info"
        message = "Kein RSCP-Wert verfügbar; der Projektstandard wirkt."
        if configured is not None and math.isfinite(configured) and configured >= 300.0:
            source = "user"
            effective = configured
            severity = "ok"
            message = "Nutzereingabe ist aktiv."
            if live_value is not None:
                delta, rel = _deviation(configured, live_value)
                if delta >= 2000.0 and rel >= 0.30:
                    severity = "warning"
                    message = (
                        "Nutzereingabe weicht deutlich vom E3DC-RSCP-Wert ab. "
                        "Die Eingabe bleibt aktiv; bitte bewusst prüfen."
                    )
        elif live_value is not None:
            source = "rscp"
            effective = live_value
            severity = "ok"
            message = "RSCP-Wert wird als Default/Fallback genutzt."
        storage[key] = _entry(
            key=key,
            label=str(rule["label"]),
            unit="W",
            configured=configured if configured is not None and math.isfinite(configured) else None,
            live_value=live_value,
            live_key=live_key,
            effective=effective,
            source=source,
            severity=severity,
            message=message,
        )

    capacity_cfg = safe_float(cfg.get("speichergroesse"), float("nan")) if _has_user_value(cfg, "speichergroesse") else None
    capacity_live, capacity_live_key = _capacity_live_value(live)
    capacity_effective = 15.0
    capacity_source = "default"
    capacity_severity = "info"
    capacity_message = "Kein RSCP-Kapazitätswert verfügbar; der Rückfallwert wirkt."
    if capacity_cfg is not None and math.isfinite(capacity_cfg) and capacity_cfg > 0.1:
        capacity_effective = capacity_cfg
        capacity_source = "user"
        capacity_severity = "ok"
        capacity_message = "Nutzereingabe ist aktiv."
        if capacity_live is not None:
            delta, rel = _deviation(capacity_cfg, capacity_live)
            if delta >= 2.0 and rel >= 0.20:
                capacity_severity = "warning"
                capacity_message = (
                    "Nutzbare Speichergröße weicht deutlich vom E3DC-Wert ab. "
                    "Die Eingabe bleibt aktiv; bitte Datenblatt/RSCP prüfen."
                )
    elif capacity_live is not None:
        capacity_effective = capacity_live
        capacity_source = "rscp"
        capacity_severity = "ok"
        capacity_message = "RSCP-Wert wird als Default/Fallback genutzt."
    storage["speichergroesse"] = _entry(
        key="speichergroesse",
        label="Speichergröße",
        unit="kWh",
        configured=capacity_cfg if capacity_cfg is not None and math.isfinite(capacity_cfg) else None,
        live_value=capacity_live,
        live_key=capacity_live_key,
        effective=capacity_effective,
        source=capacity_source,
        severity=capacity_severity,
        message=capacity_message,
    )

    reserve_cfg = safe_float(cfg.get("ep_reserve_pct"), float("nan")) if _has_user_value(cfg, "ep_reserve_pct") else None
    reserve_details = live_ep_reserve_details(cfg, live)
    reserve_live = reserve_details.get("effective_pct")
    reserve_live_key = reserve_details.get("source") or reserve_details.get("raw_key")
    reserve_default = 8.0
    reserve_effective = max(
        0.0,
        reserve_cfg if reserve_cfg is not None and math.isfinite(reserve_cfg) else reserve_default,
        safe_float(reserve_live, 0.0),
    )
    reserve_source = "rscp" if reserve_live is not None else ("user" if reserve_cfg is not None else "default")
    reserve_severity = "ok" if reserve_live is not None else "info"
    reserve_message = "Die E3DC-Notstromreserve ist die Sicherheitsführung; der Rückfallwert greift nur, wenn RSCP fehlt."
    if reserve_details.get("normalised"):
        reserve_message = (
            "E3DC-Notstromreserve wurde aus Wh auf die Gesamtspeicherkapazität normalisiert; "
            "der Prozent-Rohwert bezieht sich nur auf den Reserve-Kreis."
        )
    if reserve_live is None:
        reserve_message = "Kein RSCP-Reservewert verfügbar; die Rückfallreserve wirkt."
    elif reserve_cfg is not None and math.isfinite(reserve_cfg):
        delta, _rel = _deviation(reserve_cfg, safe_float(reserve_live, 0.0))
        if delta >= 2.0 and not reserve_details.get("normalised"):
            reserve_severity = "warning"
            reserve_message = (
                "Fallback-Reserve weicht vom E3DC-Wert ab. "
                "Die Regelung nutzt sicherheitshalber den höheren Wert."
            )
    storage["ep_reserve_pct"] = _entry(
        key="ep_reserve_pct",
        label="Notstromreserve Fallback",
        unit="%",
        configured=reserve_cfg if reserve_cfg is not None and math.isfinite(reserve_cfg) else None,
        live_value=safe_float(reserve_live, float("nan")) if reserve_live is not None else None,
        live_key=reserve_live_key,
        effective=reserve_effective,
        source=reserve_source,
        severity=reserve_severity,
        message=reserve_message,
    )

    dc_first_enabled = _is_enabled(cfg, "storage_dc_first_charge_limit_enable")
    dc_first_total_raw = live.get("PV_Power")
    dc_first_external_raw = live.get("Ext_PV_Power")
    dc_first_values_typed = bool(
        isinstance(dc_first_total_raw, (int, float))
        and not isinstance(dc_first_total_raw, bool)
        and isinstance(dc_first_external_raw, (int, float))
        and not isinstance(dc_first_external_raw, bool)
        and math.isfinite(float(dc_first_total_raw))
        and math.isfinite(float(dc_first_external_raw))
    )
    dc_first_values_nonnegative = bool(
        dc_first_values_typed
        and float(dc_first_total_raw) >= 0.0
        and float(dc_first_external_raw) >= 0.0
    )
    dc_first_total_pv_w = (
        max(0.0, float(dc_first_total_raw)) if dc_first_values_typed else 0.0
    )
    dc_first_external_pv_w = (
        max(0.0, float(dc_first_external_raw)) if dc_first_values_typed else 0.0
    )
    dc_first_e3dc_pv_w = max(0.0, dc_first_total_pv_w - dc_first_external_pv_w)
    dc_first_live_ts = safe_float(live.get("_ts"), 0.0)
    dc_first_live_age_s = float(now) - dc_first_live_ts if dc_first_live_ts > 0.0 else float("inf")
    dc_first_max_age_s = max(1.0, min(30.0, safe_float(cfg.get("storage_live_stale_guard_s"), 10.0)))
    dc_first_max_future_skew_s = 5.0
    dc_first_source_valid = bool(
        dc_first_values_nonnegative
        and live.get("RSCP_Sample_Valid") is True
        and live.get("Power_Decision_Usable") is True
        and live.get("Ext_PV_Power_Valid") is True
        and dc_first_live_ts > 0.0
        and -dc_first_max_future_skew_s <= dc_first_live_age_s <= dc_first_max_age_s
        and dc_first_external_pv_w <= dc_first_total_pv_w
    )
    dc_first_severity = "info"
    dc_first_message = "Aus: Die bestehende Speicher-Ladeführung bleibt unverändert."
    if dc_first_enabled and dc_first_source_valid:
        dc_first_severity = "ok"
        dc_first_message = (
            "Aktiv: Kurvenladung und DV-PV-Speichern werden auf die frische E3DC-PV-Leistung begrenzt; "
            "Entladen bleibt offen."
        )
    elif dc_first_enabled:
        dc_first_severity = "warning"
        dc_first_message = (
            "Aktiv, aber der frische E3DC-/Zusatz-PV-Split fehlt. "
            "Kurvenladung und DV-PV-Speichern bleiben deshalb sicher auf 0 W begrenzt."
        )
    storage["storage_dc_first_charge_limit_enable"] = _entry(
        key="storage_dc_first_charge_limit_enable",
        label="Laden an E3DC-PV koppeln",
        unit="",
        configured=dc_first_enabled,
        live_value=dc_first_e3dc_pv_w if dc_first_source_valid else None,
        live_key="PV_Power-Ext_PV_Power" if dc_first_source_valid else None,
        effective=dc_first_enabled,
        source="user" if _has_user_value(cfg, "storage_dc_first_charge_limit_enable") else "default",
        severity=dc_first_severity,
        message=dc_first_message,
    )
    shortfall_aux_ac_enabled = _is_enabled(
        cfg,
        "storage_forecast_shortfall_aux_ac_charge_enable",
    )
    if not shortfall_aux_ac_enabled:
        shortfall_aux_ac_severity = "info"
        shortfall_aux_ac_message = (
            "Nicht freigegeben und aus: Zusatz-AC bleibt auch bei einem "
            "Prognosedefizit vom Speicher-Laderahmen ausgeschlossen."
        )
    else:
        shortfall_aux_ac_severity = "warning"
        shortfall_aux_ac_message = (
            "Konfiguration erkannt, aber nicht freigegeben (EVIDENCE_LIMIT): "
            "Getrennte PV- und "
            "Lastquantile dürfen nicht zu einem vermeintlichen Quantil des "
            "Speicherüberschusses verrechnet werden. Erst eine historisch "
            "kalibrierte gemeinsame Horizontverteilung der tatsächlich im "
            "Speicher ankommenden E3DC-DC-Energie und eine fachlich beschlossene "
            "Risikoschwelle dürfen einen versiegelten Plan-/Slot-/Action-Nachweis "
            "freigeben. Beides wird derzeit nicht erzeugt; E3DC_DC_ONLY bleibt "
            "wirksam. Fehlende oder schlechte Daten sind keine Freigabe, "
            "Netzbezug bleibt gesperrt. Die DV-Zusatzwechselrichter-Auswahl "
            "bleibt davon getrennt."
        )
    storage["storage_forecast_shortfall_aux_ac_charge_enable"] = _entry(
        key="storage_forecast_shortfall_aux_ac_charge_enable",
        label=(
            "Prognose 100%: Zusatz-AC bei belegtem "
            "Prognoseminderertrag (nicht freigegeben)"
        ),
        unit="",
        configured=shortfall_aux_ac_enabled,
        live_value=None,
        live_key=None,
        effective=False,
        source=(
            "evidence_limit"
            if shortfall_aux_ac_enabled
            else (
                "user"
                if _has_user_value(
                    cfg,
                    "storage_forecast_shortfall_aux_ac_charge_enable",
                )
                else "default"
            )
        ),
        severity=shortfall_aux_ac_severity,
        message=shortfall_aux_ac_message,
    )

    # Prognose-100 später Vollstand nur mit Grund; spiegelt storage_simulator._cfg_bool(v, True).
    late_full_mode = str(cfg.get("storage_curve_target_mode", "anchored") or "").strip().lower()
    late_full_mode = late_full_mode.replace("-", "_").replace(" ", "_")
    late_full_forecast_mode = late_full_mode in (
        "forecast_100", "forecast_only_100", "forecast_only", "prognose_100", "prognose",
    )
    late_full_raw = cfg.get("storage_forecast100_late_full_guard_enable")
    late_full_present = late_full_raw is not None
    late_full_enabled = (
        str(late_full_raw).strip().lower() in {"1", "true", "yes", "on", "ja", "ein"}
        if late_full_present
        else True
    )
    if late_full_present and str(late_full_raw).strip() == "":
        late_full_severity = "warning"
        late_full_source = "invalid"
        late_full_message = (
            "Leerer Wert wirkt wie Aus: Das späte 100-%-Kurvenende wird nie geplant. "
            "Für den Standard 1 (an) eintragen oder 0 für bewusst aus."
        )
    elif not late_full_forecast_mode:
        late_full_severity = "info"
        late_full_source = "user" if late_full_present else "default"
        late_full_message = (
            "Ohne Wirkung in der Ankerkurve; wirkt nur im Zielkurven-Modus „Prognose auf 100%“. "
            "Die Auswahl bleibt für den Rückwechsel gespeichert."
        )
    elif late_full_enabled:
        late_full_severity = "ok"
        late_full_source = "user" if late_full_present else "default"
        late_full_message = (
            "An: 100 % werden nur dann erst kurz vor dem PV-Ende geplant, wenn ein Grund vorliegt "
            "(Einspeiselimit, Abregeldruck in der Prognose, Direktvermarktung oder Pre-Dump). "
            "Ohne Grund endet die Kurve am letzten nutzbaren Überschuss minus Kurvenende-Puffer; "
            "der Grund steht in der Plan-Diagnose (late_full_reason)."
        )
    else:
        late_full_severity = "info"
        late_full_source = "user"
        late_full_message = (
            "Aus: Das Kurvenende folgt immer dem letzten nutzbaren Überschuss minus Kurvenende-Puffer, "
            "auch bei Einspeiselimit, Abregeldruck, Direktvermarktung oder Pre-Dump. "
            "Abregelschutz und Pre-Dump bleiben aktiv."
        )
    storage["storage_forecast100_late_full_guard_enable"] = _entry(
        key="storage_forecast100_late_full_guard_enable",
        label="Prognose 100%: später Vollstand nur mit Grund",
        unit="",
        configured=late_full_raw if late_full_present else None,
        live_value=None,
        live_key=None,
        effective=bool(late_full_enabled and late_full_forecast_mode),
        source=late_full_source,
        severity=late_full_severity,
        message=late_full_message,
    )
    guard_min_user = _has_user_value(cfg, "storage_curve_end_guard_min")
    guard_min_raw = (
        safe_float(cfg.get("storage_curve_end_guard_min"), float("nan"))
        if guard_min_user
        else None
    )
    if not guard_min_user:
        guard_min_effective, guard_min_source, guard_min_severity = 45.0, "default", "ok"
        guard_min_message = (
            "Standard 45 min: Die Ladekurve endet 45 min vor dem letzten nutzbaren PV-Überschuss "
            "(Prognose 100% mit Grund: vor dem PV-Ende)."
        )
    elif guard_min_raw is None or not math.isfinite(guard_min_raw):
        guard_min_effective, guard_min_source, guard_min_severity = 45.0, "invalid", "warning"
        guard_min_message = (
            "Kein gültiger Wert ('%s'); wirksam bleibt der Standard 45 min (Bereich 30–120)."
            % str(cfg.get("storage_curve_end_guard_min"))[:20]
        )
    elif guard_min_raw < 30.0 or guard_min_raw > 120.0:
        guard_min_effective = max(30.0, min(120.0, guard_min_raw))
        guard_min_source, guard_min_severity = "user", "warning"
        guard_min_message = (
            "%.0f min liegt außerhalb 30–120 min; wirksam sind %.0f min." % (guard_min_raw, guard_min_effective)
        )
    else:
        guard_min_effective, guard_min_source, guard_min_severity = guard_min_raw, "user", "ok"
        guard_min_message = (
            "Die Ladekurve endet %.0f min vor dem letzten nutzbaren PV-Überschuss. Größer = früher voll "
            "und mehr Reserve gegen Wolken, kleiner = später voll." % guard_min_raw
        )
    storage["storage_curve_end_guard_min"] = _entry(
        key="storage_curve_end_guard_min",
        label="Kurvenende-Puffer",
        unit="min",
        configured=cfg.get("storage_curve_end_guard_min") if guard_min_user else None,
        live_value=None,
        live_key=None,
        effective=round(float(guard_min_effective), 1),
        source=guard_min_source,
        severity=guard_min_severity,
        message=guard_min_message,
    )

    grid_user_present = _has_user_value(cfg, "grid_max_amps")
    grid_candidate = (
        safe_float(cfg.get("grid_max_amps"), float("nan"))
        if grid_user_present
        else None
    )
    grid_invalid = bool(
        grid_user_present
        and (
            grid_candidate is None
            or not math.isfinite(grid_candidate)
            or grid_candidate <= 0.0
        )
    )
    if grid_invalid:
        grid_amps = 0.0
        grid_source = "invalid"
        grid_severity = "warning"
        grid_message = (
            "Hausabsicherung ist ungültig (Wert <= 0 oder keine Zahl). "
            "Bitte einen gültigen Wert je Phase wie z. B. 35, 40, 50 oder 63 A "
            "in den Einstellungen hinterlegen. Gesteuerte Wallbox-Ladung bleibt "
            "aus Sicherheitsgründen gesperrt."
        )
    elif grid_candidate is not None and math.isfinite(grid_candidate) and grid_candidate > 0.0:
        grid_amps = grid_candidate
        grid_source = "user"
        if grid_amps < 16.0 or grid_amps > 100.0:
            grid_severity = "warning"
            grid_message = "Hausanschlusslimit wirkt unplausibel; typische Werte liegen etwa zwischen 16 A und 100 A je Phase."
        else:
            grid_severity = "ok"
            grid_message = "Hausanschlusslimit ist plausibel."
    else:
        grid_amps = 35.0
        grid_source = "default"
        # Der stille Standard 35 A gibt den einphasigen
        # openWB-Pro-Deckel nicht über 20 A frei (grid_limit_not_explicit); nur eine
        # ausdrücklich eingetragene Hausabsicherung zählt. Warnung nur, wenn ein
        # Ladepunkt tatsächlich mehr als 20 A einphasig konfiguriert hat.
        _one_phase_over_20_configured = any(
            _has_user_value(cfg, f"wb{_charger}_openwb_pro_1p_max_amp")
            and safe_float(cfg.get(f"wb{_charger}_openwb_pro_1p_max_amp"), 0.0) > 20.0
            for _charger in (1, 2)
        )
        grid_severity = "warning" if _one_phase_over_20_configured else "info"
        grid_message = (
            "Hausanschlusslimit verwendet den Standardwert von 35 A je Phase. "
            "Für eine einphasige openWB-Pro-Freigabe über 20 A die Hausabsicherung "
            "ausdrücklich eintragen."
        )

    wallbox["grid_max_amps"] = _entry(
        key="grid_max_amps",
        label="Hausanschluss",
        unit="A",
        configured=(
            grid_candidate
            if grid_user_present and not grid_invalid
            else (cfg.get("grid_max_amps") if grid_user_present else None)
        ),
        live_value=None,
        live_key=None,
        effective=grid_amps,
        source=grid_source,
        severity=grid_severity,
        message=grid_message,
    )

    grid_reserve_present = _has_user_value(cfg, "grid_wallbox_reserve_amps")
    grid_reserve_candidate = (
        safe_float(cfg.get("grid_wallbox_reserve_amps"), float("nan"))
        if grid_reserve_present
        else None
    )
    grid_reserve_invalid = bool(
        grid_reserve_present
        and (
            grid_reserve_candidate is None
            or not math.isfinite(grid_reserve_candidate)
            or grid_reserve_candidate < 0.0
        )
    )
    grid_reserve = (
        grid_reserve_candidate
        if grid_reserve_candidate is not None
        and math.isfinite(grid_reserve_candidate)
        and grid_reserve_candidate >= 0.0
        else 2.0
    )
    grid_reserve_severity = "ok"
    grid_reserve_message = (
        "Die globale Betriebsreserve für die gemeinsamen Wallbox-Phasenlimits "
        "ist plausibel."
    )
    if grid_reserve_invalid:
        grid_reserve_severity = "warning"
        grid_reserve_message = (
            "Die globale Wallbox-Reserve ist ungültig; der sichere Rückfallwert "
            "von 2 A wird verwendet."
        )
    elif grid_reserve >= grid_amps:
        grid_reserve_severity = "warning"
        grid_reserve_message = (
            "Die globale Wallbox-Reserve muss nichtnegativ und kleiner als die "
            "Hausabsicherung je Phase sein."
        )
    wallbox["grid_wallbox_reserve_amps"] = _entry(
        key="grid_wallbox_reserve_amps",
        label="Globale Wallbox-Reserve",
        unit="A",
        configured=(
            grid_reserve_candidate
            if grid_reserve_candidate is not None
            and math.isfinite(grid_reserve_candidate)
            else None
        ),
        live_value=None,
        live_key=None,
        effective=grid_reserve,
        source=(
            "invalid"
            if grid_reserve_invalid
            else ("user" if grid_reserve_present else "default")
        ),
        severity=grid_reserve_severity,
        message=grid_reserve_message,
    )
    phase_grid_limits = {}
    phase_grid_reserves = {}
    phase_grid_contract_valid = {}
    for phase in (1, 2, 3):
        limit_key = f"grid_max_amps_l{phase}"
        reserve_key = f"grid_wallbox_reserve_amps_l{phase}"
        limit_present = _has_user_value(cfg, limit_key)
        reserve_present = _has_user_value(cfg, reserve_key)
        limit_candidate = (
            safe_float(cfg.get(limit_key), float("nan"))
            if limit_present
            else None
        )
        reserve_candidate = (
            safe_float(cfg.get(reserve_key), float("nan"))
            if reserve_present
            else None
        )
        limit_invalid = bool(
            limit_present
            and (
                limit_candidate is None
                or not math.isfinite(limit_candidate)
            )
        )
        reserve_invalid = bool(
            reserve_present
            and (
                reserve_candidate is None
                or not math.isfinite(reserve_candidate)
            )
        )
        limit_configured = None if limit_invalid else limit_candidate
        reserve_configured = None if reserve_invalid else reserve_candidate
        limit_effective = (
            None
            if limit_invalid
            else (limit_configured if limit_configured is not None else grid_amps)
        )
        phase_reserve_effective = (
            None
            if reserve_invalid
            else (
                reserve_configured
                if reserve_configured is not None
                else grid_reserve
            )
        )
        phase_grid_limits[phase] = limit_effective
        phase_grid_reserves[phase] = phase_reserve_effective
        phase_severity = "ok"
        phase_message = "Phasenlimit und Wallbox-Reserve sind plausibel."
        if limit_invalid or reserve_invalid:
            phase_severity = "warning"
            phase_message = (
                "Ein expliziter Phasenwert ist ungültig; diese Netzphase "
                "bleibt für dynamische Freigaben gesperrt."
            )
        elif limit_effective <= 0.0 or phase_reserve_effective < 0.0:
            phase_severity = "warning"
            phase_message = "Phasenlimit und Reserve müssen positiv bzw. nichtnegativ sein."
        elif phase_reserve_effective >= limit_effective:
            phase_severity = "warning"
            phase_message = "Die Wallbox-Reserve muss kleiner als das Phasenlimit sein."
        phase_grid_contract_valid[phase] = phase_severity == "ok"
        wallbox[limit_key] = _entry(
            key=limit_key,
            label=f"Netzphase L{phase}",
            unit="A",
            configured=limit_configured,
            live_value=None,
            live_key=None,
            effective=limit_effective,
            source=(
                "invalid"
                if limit_invalid
                else ("user" if limit_configured is not None else "global")
            ),
            severity=phase_severity,
            message=phase_message,
        )
        wallbox[reserve_key] = _entry(
            key=reserve_key,
            label=f"Wallbox-Reserve L{phase}",
            unit="A",
            configured=reserve_configured,
            live_value=None,
            live_key=None,
            effective=phase_reserve_effective,
            source=(
                "invalid"
                if reserve_invalid
                else ("user" if reserve_configured is not None else "global")
            ),
            severity=phase_severity,
            message=phase_message,
        )

    global_wb_max = safe_float(cfg.get("wbmaxladestrom"), 16.0)
    for key, label, fallback in (
        ("wbmaxladestrom", "Standard-Maximalstrom je Wallbox", 16.0),
        ("wb1_max_amp", "WB1 max. Ladestrom", global_wb_max),
        ("wb2_max_amp", "WB2 max. Ladestrom", global_wb_max),
    ):
        configured = safe_float(cfg.get(key), float("nan")) if _has_user_value(cfg, key) else None
        effective = configured if configured is not None and math.isfinite(configured) else float(fallback)
        severity = "ok"
        message = (
            "Der Standard-Maximalstrom je Wallbox ist plausibel."
            if key == "wbmaxladestrom"
            else "Die Ladestrom-Grenze des Ladepunkts ist plausibel."
        )
        if effective < 6.0 or effective > 32.0:
            severity = "warning"
            message = (
                "Der Standard-Maximalstrom je Wallbox muss zwischen 6 A und 32 A liegen."
                if key == "wbmaxladestrom"
                else "Der Wallbox-Ladestrom je Ladepunkt muss zwischen 6 A und 32 A liegen."
            )
        elif effective > grid_amps:
            severity = "warning"
            message = (
                "Der Standard-Maximalstrom je Wallbox liegt über der Hausabsicherung je Phase."
                if key == "wbmaxladestrom"
                else "Der Wallbox-Ladestrom je Ladepunkt liegt über der Hausabsicherung je Phase."
            )
        wallbox[key] = _entry(
            key=key,
            label=label,
            unit="A",
            configured=configured if configured is not None and math.isfinite(configured) else None,
            live_value=None,
            live_key=None,
            effective=effective,
            source="user" if configured is not None and math.isfinite(configured) else "fallback",
            severity=severity,
            message=message,
        )

    # Messbasis, PF-Reserve und Schieflast-Wächter des
    # einphasigen openWB-Pro-Deckels (Manager: _wb_pcc_phase_basis,
    # _wb_pcc_power_factor_margin, _grid_pcc_imbalance_max_a – identische Regeln).
    pcc_basis_user = _has_user_value(cfg, "wb_pcc_phase_basis")
    pcc_basis_raw = str(cfg.get("wb_pcc_phase_basis", "e3dc_pm_active_power") or "").strip().lower()
    if pcc_basis_raw in ("", "e3dc_pm_active_power"):
        pcc_basis_effective = "e3dc_pm_active_power"
        pcc_basis_unknown = False
    elif pcc_basis_raw == "off":
        pcc_basis_effective = "off"
        pcc_basis_unknown = False
    else:
        pcc_basis_effective = "off"
        pcc_basis_unknown = True
    if pcc_basis_unknown:
        pcc_basis_severity = "warning"
        pcc_basis_message = (
            "Unbekannter Wert „%s“ für die Messbasis des 1p-Deckels; er wirkt wie „off“ "
            "(fest 20 A). Gültig sind e3dc_pm_active_power und off."
            % str(cfg.get("wb_pcc_phase_basis"))
        )
    elif pcc_basis_effective == "off":
        pcc_basis_severity = "ok"
        pcc_basis_message = (
            "Messbasis aus: einphasiges Laden an der openWB Pro bleibt fest bei höchstens 20 A."
        )
    else:
        pcc_basis_severity = "ok"
        pcc_basis_message = (
            "Standard: Netzbezug je Phase vom E3DC-Wurzelzähler geteilt durch die "
            "Wechselrichter-Phasenspannung (sonst 230 V) plus gemessener Wallbox-Strom; "
            "mehr als 20 A nur mit ausdrücklicher Hausabsicherung, gebundener Zuordnung "
            "und bestätigtem Software-Nachweis, sonst 20 A."
        )
    wallbox["wb_pcc_phase_basis"] = _entry(
        key="wb_pcc_phase_basis",
        label="Messbasis 1p-Deckel openWB Pro",
        unit="-",
        configured=str(cfg.get("wb_pcc_phase_basis")) if pcc_basis_user else None,
        live_value=None,
        live_key=None,
        effective=pcc_basis_effective,
        source="user" if pcc_basis_user else "default",
        severity=pcc_basis_severity,
        message=pcc_basis_message,
    )

    pf_user = _has_user_value(cfg, "wb_pcc_power_factor_margin")
    pf_candidate = safe_float(cfg.get("wb_pcc_power_factor_margin"), float("nan")) if pf_user else None
    pf_valid = bool(
        pf_candidate is not None
        and math.isfinite(pf_candidate)
        and 0.8 <= pf_candidate <= 1.0
    )
    pf_effective = pf_candidate if pf_valid else 0.9
    if pf_user and not pf_valid:
        pf_severity = "warning"
        pf_message = (
            "Die Leistungsfaktor-Reserve muss zwischen 0,8 und 1,0 liegen; "
            "der Standard 0,9 wird verwendet."
        )
    else:
        pf_severity = "ok"
        pf_message = (
            "Der Fremdanteil des Netzbezugs auf der Wallbox-Phase wird durch diesen Faktor "
            "geteilt (Blindstrom aus Wirkleistung nicht sichtbar); 1,0 nur bei rein ohmschen Lasten."
        )
    wallbox["wb_pcc_power_factor_margin"] = _entry(
        key="wb_pcc_power_factor_margin",
        label="Leistungsfaktor-Reserve Fremdlast",
        unit="-",
        configured=pf_candidate if pf_valid else (cfg.get("wb_pcc_power_factor_margin") if pf_user else None),
        live_value=None,
        live_key=None,
        effective=pf_effective,
        source="user" if pf_valid else ("invalid" if pf_user else "default"),
        severity=pf_severity,
        message=pf_message,
    )

    imbalance_user = _has_user_value(cfg, "grid_pcc_imbalance_max_a")
    imbalance_candidate = (
        safe_float(cfg.get("grid_pcc_imbalance_max_a"), float("nan")) if imbalance_user else None
    )
    imbalance_valid = bool(
        imbalance_candidate is not None
        and math.isfinite(imbalance_candidate)
        and 10.0 <= imbalance_candidate <= 32.0
    )
    imbalance_effective = imbalance_candidate if imbalance_valid else 20.0
    if imbalance_user and not imbalance_valid:
        imbalance_severity = "warning"
        imbalance_message = (
            "Der Schieflast-Wächter muss zwischen 10 A und 32 A liegen; "
            "der Standard 20 A (4,6 kVA) wird verwendet."
        )
    elif imbalance_valid and imbalance_candidate > 20.0:
        imbalance_severity = "info"
        imbalance_message = (
            "Netzbetreiber-Schieflastgrenze prüfen (Standard 4,6 kVA ≙ 20 A); "
            "höhere Werte nur nach Rücksprache mit dem Netzbetreiber."
        )
    else:
        imbalance_severity = "ok"
        imbalance_message = (
            "Höchste zulässige Unsymmetrie des Netzbezugs zwischen der Wallbox-Phase und "
            "der am wenigsten belasteten Phase; der Deckel fällt dadurch nie unter 20 A."
        )
    wallbox["grid_pcc_imbalance_max_a"] = _entry(
        key="grid_pcc_imbalance_max_a",
        label="Schieflast-Wächter am Netzpunkt",
        unit="A",
        configured=imbalance_candidate if imbalance_valid else (cfg.get("grid_pcc_imbalance_max_a") if imbalance_user else None),
        live_value=None,
        live_key=None,
        effective=imbalance_effective,
        source="user" if imbalance_valid else ("invalid" if imbalance_user else "default"),
        severity=imbalance_severity,
        message=imbalance_message,
    )

    # Hochschaltung 1p→3p – Vorlauf (Standard 60 s, Untergrenze 30 s,
    # die Sperre nach einem Wechsel bleibt davon getrennt) und Symmetrie-Klausel (0/1) am
    # Schieflastwert grid_pcc_imbalance_max_a.
    up_hold_user = _has_user_value(cfg, "wb_phase_up_forecast_hold_s")
    up_hold_candidate = safe_float(cfg.get("wb_phase_up_forecast_hold_s"), float("nan")) if up_hold_user else None
    up_hold_valid = bool(
        up_hold_candidate is not None
        and math.isfinite(up_hold_candidate)
        and 30.0 <= up_hold_candidate <= 3600.0
    )
    up_hold_effective = up_hold_candidate if up_hold_valid else 60.0
    if up_hold_user and not up_hold_valid:
        up_hold_severity = "warning"
        up_hold_message = (
            "Der Vorlauf 1p→3p muss zwischen 30 s und 3600 s liegen; die Regelung hebt kleinere "
            "Werte auf 30 s an, wirksam ist sonst der Standard 60 s."
        )
    elif up_hold_valid and up_hold_candidate >= 480.0:
        up_hold_severity = "info"
        up_hold_message = (
            "Vorlauf 1p→3p von %.0f s: Die Hochschaltbedingung (Überschuss ≥ 3p-Minimum + Puffer und "
            "1p ausgereizt, 30-s-Mittel) muss so lange ununterbrochen gelten; Standard 60 s. Die Sperre "
            "nach jedem Phasenwechsel (480 s) bleibt davon getrennt." % up_hold_candidate
        )
    else:
        up_hold_severity = "ok"
        up_hold_message = (
            "Vorlauf 1p→3p: So lange muss die Hochschaltbedingung (Überschuss ≥ 3p-Minimum + Puffer "
            "und 1p ausgereizt, 30-s-Mittel) ununterbrochen gelten, zusätzlich zum Export-Wh-Konto; "
            "die Sperre nach jedem Phasenwechsel (480 s) bleibt davon getrennt."
        )
    wallbox["wb_phase_up_forecast_hold_s"] = _entry(
        key="wb_phase_up_forecast_hold_s",
        label="Vorlauf 1p→3p",
        unit="s",
        configured=up_hold_candidate if up_hold_valid else (cfg.get("wb_phase_up_forecast_hold_s") if up_hold_user else None),
        live_value=None,
        live_key=None,
        effective=up_hold_effective,
        source="user" if up_hold_valid else ("invalid" if up_hold_user else "default"),
        severity=up_hold_severity,
        message=up_hold_message,
    )

    symmetry_user = _has_user_value(cfg, "wb_phase_up_symmetry_enable")
    symmetry_raw = str(cfg.get("wb_phase_up_symmetry_enable") if symmetry_user else "").strip().lower()
    symmetry_valid = bool(symmetry_user and symmetry_raw in ("0", "1", "true", "false", "on", "off", "ja", "nein", "yes", "no"))
    symmetry_effective = bool(symmetry_valid and symmetry_raw in ("1", "true", "on", "ja", "yes"))
    if symmetry_user and not symmetry_valid:
        symmetry_severity = "warning"
        symmetry_message = (
            "Die Symmetrie-Klausel kennt nur 0 (aus) oder 1 (ein); der Standard aus wird verwendet."
        )
    elif symmetry_effective:
        symmetry_severity = "info"
        symmetry_message = (
            "Symmetrie-Klausel ein: Eine einphasige Ladung ab %.0f A (Schieflast-Wächter) gilt als "
            "ausgereizt und darf mit ausreichend Überschuss auf drei Phasen wechseln. Die "
            "Unsymmetriegrenze des Netzbetreibers (4,6 kVA) muss auch für die Einspeiseseite gelten." % imbalance_effective
        )
    else:
        symmetry_severity = "ok"
        symmetry_message = (
            "Symmetrie-Klausel aus (Standard): Die Hochschaltung 1p→3p folgt nur dem Überschuss "
            "und dem wirksamen 1p-Deckel."
        )
    wallbox["wb_phase_up_symmetry_enable"] = _entry(
        key="wb_phase_up_symmetry_enable",
        label="Symmetrie-Klausel 1p→3p",
        unit="",
        configured=(1 if symmetry_effective else 0) if symmetry_valid else (cfg.get("wb_phase_up_symmetry_enable") if symmetry_user else None),
        live_value=None,
        live_key=None,
        effective=1 if symmetry_effective else 0,
        source="user" if symmetry_valid else ("invalid" if symmetry_user else "default"),
        severity=symmetry_severity,
        message=symmetry_message,
    )

    # Experimenteller E3DC-Direktvertrag für Phasenwechsel sichtbar und
    # ehrlich bewerten (Standard aus). Wirksamkeit wie im Treiber: je Wallbox wbN_..., sonst global.
    _rm_direct_truthy = ("1", "true", "yes", "on", "ja", "ein")
    _rm_direct_global_user = _has_user_value(cfg, "wb_e3dc_direct_phase_control_enable")
    _rm_direct_global_on = bool(
        _rm_direct_global_user
        and str(cfg.get("wb_e3dc_direct_phase_control_enable")).strip().lower() in _rm_direct_truthy
    )
    _rm_direct_any_user = _rm_direct_global_user
    _rm_direct_on_ids = []
    for _rm_direct_wb in (1, 2):
        _rm_direct_key = "wb%d_e3dc_direct_phase_control_enable" % _rm_direct_wb
        if _has_user_value(cfg, _rm_direct_key):
            _rm_direct_any_user = True
            if str(cfg.get(_rm_direct_key)).strip().lower() in _rm_direct_truthy:
                _rm_direct_on_ids.append(_rm_direct_wb)
        elif _rm_direct_global_on:
            _rm_direct_on_ids.append(_rm_direct_wb)
    if _rm_direct_on_ids:
        _rm_direct_severity = "warning"
        _rm_direct_message = (
            "E3DC-Direktvertrag eingeschaltet (%s): experimentell. Bei ausdrücklich gewählter efy oder "
            "Multi Connect schreibt die Regelung je Phasenwechsel die Geräteeinstellungen Sonnenmodus, "
            "automatische Phasenumschaltung und Phasenzahl und stellt sie bei der Rückgabe wieder her. "
            "Ob die Wallbox diese Einstellungen dauerhaft speichert, ist nicht belegt – nicht für den "
            "Dauerbetrieb empfohlen. Die Sonnenmodus-Übergabe an die E3/DC-Automatik ist in dieser Zeit "
            "stillgelegt." % ", ".join("WB%d" % i for i in _rm_direct_on_ids)
        )
    else:
        _rm_direct_severity = "ok"
        _rm_direct_message = (
            "E3DC-Direktvertrag aus (Standard): Die Regelung schreibt keine Phasen-Geräteeinstellungen "
            "der E3/DC-Wallbox."
        )
    wallbox["wb_e3dc_direct_phase_control_enable"] = _entry(
        key="wb_e3dc_direct_phase_control_enable",
        label="E3DC-Direktvertrag Phasenwechsel (experimentell)",
        unit="",
        configured=cfg.get("wb_e3dc_direct_phase_control_enable") if _rm_direct_global_user else None,
        live_value=None,
        live_key=None,
        effective=1 if _rm_direct_on_ids else 0,
        source="user" if _rm_direct_any_user else "default",
        severity=_rm_direct_severity,
        message=_rm_direct_message,
    )

    # Startfenster der openWB Pro – Startfenster, Wiederholzyklus,
    # CP-Karenz (Untergrenze 60 s) und Deckel der Weckimpulse je Stecksession.
    def _openwb_pro_start_window_entry(key, label, default, low, high, ok_message, unit="s"):
        user = _has_user_value(cfg, key)
        candidate = safe_float(cfg.get(key), float("nan")) if user else None
        valid = bool(candidate is not None and math.isfinite(candidate) and low <= candidate <= high)
        effective = candidate if valid else default
        if user and not valid:
            severity = "warning"
            message = (
                "Der Wert liegt außerhalb von %d–%d %s; wirksam ist der Standard %d %s."
                % (int(low), int(high), unit, int(default), unit)
            )
        else:
            severity = "ok"
            message = ok_message
        wallbox[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=candidate if valid else (cfg.get(key) if user else None),
            live_value=None,
            live_key=None,
            effective=effective,
            source="user" if valid else ("invalid" if user else "default"),
            severity=severity,
            message=message,
        )

    _openwb_pro_start_window_entry(
        "openwb_pro_start_hold_s",
        "Pro Startfenster",
        180.0,
        60.0,
        600.0,
        "So lange bleibt das Stromangebot nach dem bestätigten Angebot der Box (Readback ≥ 6 A) "
        "stehen, bis das Auto Leistung aufnimmt – kein 0 A, keine Anhebung; eine Absenkung ab 6 A folgt sofort; "
        "dieselbe Zeit gilt als Abschaltverzögerung ohne PV-Budget (wie evcc, 3 min).",
    )
    _openwb_pro_start_window_entry(
        "openwb_pro_start_retry_cycle_s",
        "Pro Start-Wiederholzyklus",
        300.0,
        180.0,
        1200.0,
        "Nimmt das Auto das Angebot nicht an, zählt dieser Abstand als neuer Startzyklus: höchstens ein "
        "Weckimpuls je Zyklus, höchstens drei Zyklen je Stecken; danach bleibt das Angebot mit Meldung stehen.",
    )

    grace_user = _has_user_value(cfg, "openwb_pro_start_cp_grace_s")
    grace_candidate = safe_float(cfg.get("openwb_pro_start_cp_grace_s"), float("nan")) if grace_user else None
    grace_numeric = bool(grace_candidate is not None and math.isfinite(grace_candidate))
    if grace_numeric:
        grace_effective = max(60.0, min(600.0, grace_candidate))
    else:
        grace_effective = 60.0
    if grace_user and not grace_numeric:
        grace_severity = "warning"
        grace_message = "Die CP-Karenz ist keine Zahl; wirksam sind 60 s."
    elif grace_numeric and grace_candidate < 60.0:
        grace_severity = "warning"
        grace_message = (
            "Eine Karenz unter 60 s wird auf 60 s angehoben: ein normal startendes Auto zieht innerhalb von "
            "10–60 s Strom und wird nicht unterbrochen; ein Fahrzeugprofil verkürzt die Karenz nicht."
        )
    elif grace_numeric and grace_candidate > 600.0:
        grace_severity = "warning"
        grace_message = "Die CP-Karenz ist auf 600 s begrenzt."
    else:
        grace_severity = "ok"
        grace_message = (
            "Wartezeit ab dem bestätigten Stromangebot der openWB Pro, bevor der erste Weckimpuls kommt; "
            "nie unter 60 s, nie ohne Angebot ≥ 6 A der Box, nie bei angenommener PWM."
        )
    wallbox["openwb_pro_start_cp_grace_s"] = _entry(
        key="openwb_pro_start_cp_grace_s",
        label="Pro Start-CP-Karenz",
        unit="s",
        configured=grace_candidate if grace_numeric else (cfg.get("openwb_pro_start_cp_grace_s") if grace_user else None),
        live_value=None,
        live_key=None,
        effective=grace_effective,
        source="user" if grace_numeric else ("invalid" if grace_user else "default"),
        severity=grace_severity,
        message=grace_message,
    )

    cp_user = _has_user_value(cfg, "wb_openwb_start_cp_retries")
    cp_candidate = safe_float(cfg.get("wb_openwb_start_cp_retries"), float("nan")) if cp_user else None
    cp_valid = bool(
        cp_candidate is not None
        and math.isfinite(cp_candidate)
        and float(cp_candidate).is_integer()
        and 1 <= int(cp_candidate) <= 3
    )
    wallbox["wb_openwb_start_cp_retries"] = _entry(
        key="wb_openwb_start_cp_retries",
        label="Pro Weckimpulse je Stecken",
        unit="",
        configured=int(cp_candidate) if cp_valid else (cfg.get("wb_openwb_start_cp_retries") if cp_user else None),
        live_value=None,
        live_key=None,
        effective=int(cp_candidate) if cp_valid else 3,
        source="user" if cp_valid else ("invalid" if cp_user else "default"),
        severity="info" if cp_valid else ("warning" if cp_user else "ok"),
        message=(
            "Deckel der Weckimpulse je Stecksession über alle Wiederholzyklen (1–3)."
            if cp_valid or not cp_user
            else "Nur 1 bis 3 Weckimpulse je Stecken sind gültig; wirksam sind 3."
        ),
    )

    for charger_id in (1, 2):
        phase_key = f"wb{charger_id}_grid_phase"
        limit_key = f"wb{charger_id}_openwb_pro_1p_max_amp"
        phase_raw = cfg.get(phase_key)
        try:
            phase_value = float(phase_raw)
        except (TypeError, ValueError):
            phase_value = float("nan")
        phase_valid = bool(
            math.isfinite(phase_value)
            and phase_value.is_integer()
            and int(phase_value) in (1, 2, 3)
        )
        if phase_valid:
            mapped_phase = int(phase_value)
        else:
            mapped_phase = 0
        configured_limit_candidate = (
            safe_float(cfg.get(limit_key), float("nan"))
            if _has_user_value(cfg, limit_key)
            else None
        )
        configured_limit_invalid = bool(
            _has_user_value(cfg, limit_key)
            and (
                configured_limit_candidate is None
                or not math.isfinite(configured_limit_candidate)
            )
        )
        configured_limit = (
            None if configured_limit_invalid else configured_limit_candidate
        )
        requested_limit = (
            configured_limit if configured_limit is not None else 20.0
        )
        # Messbasis Wurzelzähler je Phase ÷ WR-Spannung
        # freigegeben. Mehr als 20 A werden zur Laufzeit dynamisch freigegeben, wenn die
        # Messbasis nicht „off“ ist, die Zuordnung gebunden, die Hausabsicherung
        # ausdrücklich eingetragen (Skalar oder Phase – der Standard 35 A zählt nicht)
        # und das Betriebslimit ≥ 6 A ist; ohne frische Messwerte bleibt es bei 20 A.
        effective_limit = min(requested_limit, 20.0)
        limit_source = "safe_fallback" if configured_limit is None else "user"
        severity = "ok"
        message = "Einphasige openWB-Pro-Grenze bleibt beim sicheren 20-A-Fallback."
        if configured_limit_invalid:
            limit_source = "invalid"
            severity = "warning"
            message = (
                "Die einphasige openWB-Pro-Grenze ist ungültig; "
                "der sichere 20-A-Fallback bleibt aktiv."
            )
        elif requested_limit < 6.0 or requested_limit > 32.0:
            severity = "warning"
            message = "Die einphasige openWB-Pro-Grenze muss zwischen 6 A und 32 A liegen."
        elif requested_limit > 20.0:
            limit_source = "user_capped_fail_closed"
            phase_limit_explicit = bool(
                phase_valid
                and (
                    _has_user_value(cfg, f"grid_max_amps_l{mapped_phase}")
                    or grid_user_present
                )
            )
            if pcc_basis_effective == "off":
                severity = "warning"
                message = (
                    "Mehr als 20 A bleiben gesperrt, weil die Messbasis des "
                    "1p-Deckels auf „Aus“ steht."
                )
            elif not phase_valid:
                severity = "warning"
                message = (
                    "Mehr als 20 A bleiben gesperrt, bis lokale Wallbox-L1 "
                    "eindeutig einer Netzphase zugeordnet ist."
                )
            elif not phase_limit_explicit:
                severity = "warning"
                message = (
                    "Mehr als 20 A bleiben gesperrt, bis die Hausabsicherung "
                    "ausdrücklich eingetragen ist (global oder für die zugeordnete "
                    "Netzphase); der Standard 35 A zählt dafür nicht."
                )
            elif not phase_grid_contract_valid.get(mapped_phase, False):
                severity = "warning"
                message = (
                    "Mehr als 20 A bleiben gesperrt, weil Phasenlimit oder "
                    "Reserve ungültig sind."
                )
            else:
                operating_limit = (
                    phase_grid_limits[mapped_phase]
                    - phase_grid_reserves[mapped_phase]
                )
                if operating_limit < 6.0:
                    severity = "warning"
                    message = "Das phasenbezogene Betriebslimit lässt keinen sicheren Ladestrom zu."
                else:
                    effective_limit = min(requested_limit, operating_limit)
                    limit_source = "user_dynamic_pcc_pm"
                    severity = "ok"
                    message = (
                        "Freigabe > 20 A zur Laufzeit nach Software-Nachweis der "
                        "Phasenzuordnung (Dashboard: Zuordnung bestätigt); ohne "
                        "frische Messwerte 20 A."
                    )
        wallbox[phase_key] = _entry(
            key=phase_key,
            label=f"WB{charger_id} lokale L1 auf Netzphase",
            unit="",
            configured=mapped_phase if phase_valid else None,
            live_value=None,
            live_key=None,
            effective=mapped_phase if phase_valid else None,
            source="user" if phase_valid else "unbound",
            severity=(
                "ok"
                if phase_valid or requested_limit <= 20.0
                else "warning"
            ),
            message=(
                "Phasenzuordnung ist gebunden; E3DC-Control bestätigt sie beim "
                "einphasigen Laden automatisch (Dashboard: Zuordnung bestätigt)."
                if phase_valid
                else "Keine Phasenzuordnung hinterlegt."
            ),
        )
        wallbox[limit_key] = _entry(
            key=limit_key,
            label=f"WB{charger_id} openWB Pro 1p max.",
            unit="A",
            configured=configured_limit,
            live_value=None,
            live_key=None,
            effective=effective_limit,
            source=limit_source,
            severity=severity,
            message=message,
        )
        # Angebot der openWB Pro nach dem Abstecken; ungültig gilt „safe“.
        unplug_key = f"wb{charger_id}_openwb_pro_unplug_offer"
        unplug_user = _has_user_value(cfg, unplug_key)
        unplug_raw = str(cfg.get(unplug_key) or "").strip().lower()
        unplug_valid = unplug_raw in ("safe", "fast_start")
        unplug_effective = unplug_raw if unplug_valid else "safe"
        wallbox[unplug_key] = _entry(
            key=unplug_key,
            label=f"WB{charger_id} openWB Pro nach dem Abstecken",
            unit="",
            configured=unplug_raw if unplug_user else None,
            live_value=None,
            live_key=None,
            effective=unplug_effective,
            source=(
                "user" if unplug_user and unplug_valid
                else ("invalid" if unplug_user else "default")
            ),
            severity="warning" if unplug_user and not unplug_valid else "ok",
            message=(
                "Nach dem Abstecken einmalig 6 A: das Fahrzeug startet beim "
                "Anstecken sofort, danach regelt die normale Logik."
                if unplug_effective == "fast_start"
                else (
                    "Ungültiger Wert; wirksam ist die Sicherheitsvariante (0 A)."
                    if unplug_user and not unplug_valid
                    else "Nach dem Abstecken einmalig 0 A: beim Anstecken "
                    "startet nichts, bevor die Regelung entscheidet."
                )
            ),
        )

    native_wallbox_active = _is_enabled(cfg, "wb_native_enable")
    wallbox["wallbox_phase_budget"] = _entry(
        key="wallbox_phase_budget",
        label="Wallbox-Phasenbudget",
        unit="",
        configured=None,
        live_value=None,
        live_key=None,
        effective="runtime_phase_vector" if native_wallbox_active else "status_only",
        source="phase_vector_contract",
        severity="ok",
        message=(
            "Die native Regelung verteilt nach Ladeleistung und projiziert jeden "
            "Ladepunkt auf L1/L2/L3. Ein- und dreiphasige Amperewerte werden "
            "nicht skalar addiert."
            if native_wallbox_active
            else
            "Ohne native Regelung werden Wallbox-Maximalströme nur einzeln "
            "geprüft; eine pauschale Ampere-Summe wäre physikalisch nicht "
            "aussagekräftig."
        ),
    )

    wbminsoc_val = safe_float(cfg.get("wbminsoc"), 70.0)
    if wbminsoc_val < reserve_effective:
        raise ValueError(
            f"Wallbox-Mindest-SoC (wbminsoc={wbminsoc_val}%) darf nicht kleiner als "
            f"die effektive Notstromreserve ({reserve_effective}%) sein."
        )
    wbminsoc_severity = "ok"
    wbminsoc_message = "Wallbox-Mindest-SoC schützt die Hausreserve plausibel."
    if wbminsoc_val < 20.0:
        wbminsoc_severity = "warning"
        wbminsoc_message = (
            "Wallbox-Mindest-SoC ist sehr niedrig; nachts kann der Hausspeicher durch Fahrzeugladung "
            "stärker entladen werden."
        )
    elif wbminsoc_val > 90.0:
        wbminsoc_severity = "warning"
        wbminsoc_message = "Wallbox-Mindest-SoC ist sehr hoch; die Wallbox wird selten freigegeben."
    wallbox["wbminsoc"] = _entry(
        key="wbminsoc",
        label="Wallbox Mindest-SoC",
        unit="%",
        configured=wbminsoc_val if _has_user_value(cfg, "wbminsoc") else None,
        live_value=None,
        live_key=None,
        effective=wbminsoc_val,
        source="user" if _has_user_value(cfg, "wbminsoc") else "default",
        severity=wbminsoc_severity,
        message=wbminsoc_message,
    )

    wb_bat_target_soc_val = safe_float(cfg.get("wb_bat_target_soc"), 90.0)
    wb_bat_target_soc_severity = "ok"
    wb_bat_target_soc_message = "Ziel-SoC am Abend liegt plausibel zur Wallbox-Reserve."
    if wb_bat_target_soc_val < wbminsoc_val:
        wb_bat_target_soc_severity = "warning"
        wb_bat_target_soc_message = "Ziel-SoC am Abend liegt unter dem Wallbox-Mindest-SoC."
    wallbox["wb_bat_target_soc"] = _entry(
        key="wb_bat_target_soc",
        label="Wallbox Ziel-SoC Abend",
        unit="%",
        configured=wb_bat_target_soc_val if _has_user_value(cfg, "wb_bat_target_soc") else None,
        live_value=None,
        live_key=None,
        effective=wb_bat_target_soc_val,
        source="user" if _has_user_value(cfg, "wb_bat_target_soc") else "default",
        severity=wb_bat_target_soc_severity,
        message=wb_bat_target_soc_message,
    )

    # Messreserve der PV-only-Entladeklemme
    # (Akkustützung nach Korridorlage). storage_manager.py liest den Schlüssel mit
    # safe_int(…, 300) und hebt ihn im Code auf max(300, Wert) an; hier nur
    # beratend: numerisch, mindestens 300 W, über 3000 W als Hinweis.
    pv_only_reserve_default_w = 300.0
    pv_only_reserve_user = _has_user_value(cfg, "wb_curve_pv_only_house_reserve_w")
    pv_only_reserve_configured, pv_only_reserve_finite = _configured_finite_number(
        cfg, "wb_curve_pv_only_house_reserve_w", pv_only_reserve_default_w
    )
    if pv_only_reserve_user and pv_only_reserve_finite:
        pv_only_reserve_effective_w = max(pv_only_reserve_default_w, float(int(pv_only_reserve_configured)))
    else:
        pv_only_reserve_effective_w = pv_only_reserve_default_w
    pv_only_reserve_severity = "ok"
    pv_only_reserve_message = (
        "Unter der Ladekurve ohne Stützkontingent deckt der Akku Hauslast und Wärmepumpe plus diese "
        "Reserve; die Wallboxen erhalten nur PV-Überschuss."
    )
    if pv_only_reserve_user and not pv_only_reserve_finite:
        pv_only_reserve_severity = "warning"
        pv_only_reserve_message = (
            "Reserve unter der Kurve ist nicht numerisch; die Regelung nutzt den Standard 300 W."
        )
    elif pv_only_reserve_user and pv_only_reserve_configured < pv_only_reserve_default_w:
        pv_only_reserve_severity = "warning"
        pv_only_reserve_message = (
            "Reserve unter der Kurve liegt unter 300 W; die Regelung hebt sie automatisch auf 300 W an, "
            "damit Hauslastsprünge nicht bis zum nächsten Heartbeat aus dem Netz kommen."
        )
    elif pv_only_reserve_user and pv_only_reserve_configured > 3000.0:
        pv_only_reserve_severity = "warning"
        pv_only_reserve_message = (
            "Reserve unter der Kurve ist sehr hoch; der Akku darf dann weit über die Hauslast hinaus "
            "entladen und finanziert die Wallboxen wieder teilweise mit."
        )
    wallbox["wb_curve_pv_only_house_reserve_w"] = _entry(
        key="wb_curve_pv_only_house_reserve_w",
        label="Reserve unter der Ladekurve",
        unit="W",
        configured=(
            pv_only_reserve_configured if pv_only_reserve_user and pv_only_reserve_finite
            else (str(cfg.get("wb_curve_pv_only_house_reserve_w")) if pv_only_reserve_user else None)
        ),
        live_value=None,
        live_key=None,
        effective=pv_only_reserve_effective_w,
        source="user" if pv_only_reserve_user and pv_only_reserve_finite else "default",
        severity=pv_only_reserve_severity,
        message=pv_only_reserve_message,
    )

    # Wärmepumpe im Hauswert. storage_manager.augment_consumer_live
    # (Provider energy_decision, Shelly Pro3EM, E3DC-Leistungsmesser) und Storage/predump.py
    # normalisieren ``storage_home_wp_split`` über dieselbe Alias-Tabelle; hier nur beratend
    # gespiegelt (keine Regeländerung): auto = Heuristik (WP gilt als im Hauswert enthalten,
    # wenn Home_roh ≥ max(500 W, 0,55·WP)), include = Home = max(0, Home_roh − WP),
    # separate = WP zusätzlich zum Hauswert. Unbekannte Strings wirken wie auto → warning.
    home_wp_split_user = _has_user_value(cfg, "storage_home_wp_split")
    home_wp_split_raw = str(cfg.get("storage_home_wp_split", "auto") or "auto").strip().lower()
    if home_wp_split_raw in ("1", "true", "yes", "on", "include", "included", "home_includes_wp"):
        home_wp_split_mode = "include"
    elif home_wp_split_raw in ("0", "false", "no", "off", "separate", "excluded", "home_excludes_wp"):
        home_wp_split_mode = "separate"
    else:
        home_wp_split_mode = "auto"
    home_wp_split_unknown = bool(
        home_wp_split_user and home_wp_split_raw != "auto" and home_wp_split_mode == "auto"
    )
    home_wp_split_severity = "ok"
    if home_wp_split_mode == "include":
        home_wp_split_message = (
            "Die Wärmepumpe steckt im E3DC-Hauswert: Die Regelung rechnet die gemeldete Wärmepumpenleistung "
            "aus dem Hauswert heraus (Haus = max(0, Hauswert − WP)) und führt Hauslast und Wärmepumpe getrennt."
        )
    elif home_wp_split_mode == "separate":
        home_wp_split_message = (
            "Die Wärmepumpe wird getrennt gemessen und zählt zusätzlich zum E3DC-Hauswert; "
            "der Hauswert bleibt unverändert."
        )
    else:
        home_wp_split_message = (
            "Automatik: Die Wärmepumpe gilt je Messung als im Hauswert enthalten, wenn der rohe Hauswert "
            "mindestens max(500 W, 55 % der Wärmepumpenleistung) erreicht; darunter zählt sie zusätzlich. "
            "Bei kleiner Rohhauslast kann die Zuordnung springen."
        )
    if home_wp_split_unknown:
        home_wp_split_severity = "warning"
        home_wp_split_message = (
            "Unbekannter Wert „%s“ für „Wärmepumpe im Hauswert“; die Regelung behandelt ihn wie „auto“ "
            "(Heuristik). Gültig sind auto, include und separate." % str(cfg.get("storage_home_wp_split"))
        )
    elif (
        home_wp_split_mode == "auto"
        and _has_user_value(cfg, "wurzelzaehler")
        and safe_float(cfg.get("wurzelzaehler"), -1.0) == 0.0
    ):
        home_wp_split_severity = "info"
        home_wp_split_message += (
            " Hinweis: Ohne separaten Wurzelzähler (wurzelzaehler = 0) steckt eine im E3DC-Hausverbrauch "
            "mitgemessene Wärmepumpe im Hauswert – dann ist „include“ die eindeutige Wahl."
        )
    storage["storage_home_wp_split"] = _entry(
        key="storage_home_wp_split",
        label="Wärmepumpe im Hauswert",
        unit="-",
        configured=str(cfg.get("storage_home_wp_split")) if home_wp_split_user else None,
        live_value=None,
        live_key=None,
        effective=home_wp_split_mode,
        source="user" if home_wp_split_user else "default",
        severity=home_wp_split_severity,
        message=home_wp_split_message,
    )

    openwb_pro_phase_wait_configured = (
        safe_float(cfg.get("openwb_pro_phase_wait_s"), 480.0)
        if _has_user_value(cfg, "openwb_pro_phase_wait_s")
        else None
    )
    openwb_pro_phase_wait_effective = max(
        480.0,
        openwb_pro_phase_wait_configured if openwb_pro_phase_wait_configured is not None else 480.0,
    )
    openwb_pro_phase_wait_severity = "ok"
    openwb_pro_phase_wait_message = (
        "Phasenwechsel-Cooldown und Reservierung sind plausibel; dieser Wert ist kein CP-Dauernachweis."
    )
    if openwb_pro_phase_wait_configured is not None and openwb_pro_phase_wait_configured < 480.0:
        openwb_pro_phase_wait_severity = "warning"
        openwb_pro_phase_wait_message = (
            "Phasenwechsel-Cooldown ist kleiner als 480s; die Regelung hebt ihn automatisch an. "
            "Der Cooldown ist kein CP-Dauernachweis; die reale CP-Unterbrechung besitzt einen "
            "getrennten Schutzwert."
        )
    wallbox["openwb_pro_phase_wait_s"] = _entry(
        key="openwb_pro_phase_wait_s",
        label="openWB Pro Phasen-Cooldown",
        unit="s",
        configured=openwb_pro_phase_wait_configured,
        live_value=None,
        live_key=None,
        effective=openwb_pro_phase_wait_effective,
        source="user" if openwb_pro_phase_wait_configured is not None else "default",
        severity=openwb_pro_phase_wait_severity,
        message=openwb_pro_phase_wait_message,
    )

    phase_cp_configured = (
        safe_float(cfg.get("openwb_pro_phase_cp_interrupt_duration_s"), 5.0)
        if _has_user_value(cfg, "openwb_pro_phase_cp_interrupt_duration_s")
        else None
    )
    phase_cp_requested = phase_cp_configured if phase_cp_configured is not None else 5.0
    phase_cp_legacy = phase_cp_requested >= 480.0
    phase_cp_effective = 5.0 if phase_cp_legacy else min(30.0, max(2.0, phase_cp_requested))
    phase_cp_severity = "ok"
    phase_cp_message = (
        "Kurzer Geräteimpuls für den Phasenwechsel; der getrennte 480-s-Cooldown sperrt nur einen weiteren Wechsel."
    )
    if phase_cp_legacy:
        phase_cp_severity = "warning"
        phase_cp_message = (
            "Der frühere 480-s-CP-Wert wird als Altvertrag erkannt und wirksam auf 5s migriert. "
            "Der 480-s-Schutz bleibt getrennt als Sperre gegen den nächsten Phasenwechsel bestehen."
        )
    elif phase_cp_configured is not None and not 2.0 <= phase_cp_configured <= 30.0:
        phase_cp_severity = "warning"
        phase_cp_message = (
            "Die CP-Dauer liegt außerhalb 2 bis 30s und wird auf den sicheren Bereich begrenzt. "
            "Der Phasenwechsel-Cooldown bleibt davon getrennt."
        )
    wallbox["openwb_pro_phase_cp_interrupt_duration_s"] = _entry(
        key="openwb_pro_phase_cp_interrupt_duration_s",
        label="openWB Pro Phasen-CP-Unterbrechung",
        unit="s",
        configured=phase_cp_configured,
        live_value=None,
        live_key=None,
        effective=phase_cp_effective,
        source="user" if phase_cp_configured is not None else "default",
        severity=phase_cp_severity,
        message=phase_cp_message,
    )

    # Freigabe-Hysterese und
    # Gnadenfrist des PV-only-Laufhalts (Installer/Wallbox/decision.py,
    # pv_only_running_minimum_hold_release_gate). Die Regelung klemmt die Werte
    # selbst (Freigabe >= 30 s, Gnadenfrist 10..300 s); hier nur der Hinweis.
    pv_only_release_hold_configured = (
        safe_float(cfg.get("wb_pv_only_release_hold_s"), 120.0)
        if _has_user_value(cfg, "wb_pv_only_release_hold_s")
        else None
    )
    pv_only_release_hold_effective = max(
        30.0,
        pv_only_release_hold_configured if pv_only_release_hold_configured is not None else 120.0,
    )
    pv_only_release_hold_severity = "ok"
    # Dieselbe Zeit gilt für den Wiederanlauf nach
    # einem Kaskaden-Stop des Gruppen-Defizitreglers (stabiles PV-Budget).
    pv_only_release_hold_message = (
        "Freigabe-Hysterese des PV-only-Laufhalts: Eine Slot-Erhöhung wird erst nach dieser Zeit "
        "stabil über dem Mindeststrom übernommen; Slot 0 hält sofort wieder. Dieselbe Zeit gilt für den "
        "Wiederanlauf nach einem Kaskaden-Stop des Defizitreglers: Das PV-Budget muss die Mindestleistung "
        "der erwarteten Phasenzahl so lange durchgehend decken, bevor die Wallbox wieder startet."
    )
    if pv_only_release_hold_configured is not None and pv_only_release_hold_configured < 30.0:
        pv_only_release_hold_severity = "warning"
        pv_only_release_hold_message = (
            "Die Halt-Freigabe ist kleiner als 30s; die Regelung hebt sie auf 30s an. Unter 30s "
            "schlägt das Zuteilungs-Pendeln (Slot 0 <-> voller Slot) wieder auf den Draht durch."
        )
    wallbox["wb_pv_only_release_hold_s"] = _entry(
        key="wb_pv_only_release_hold_s",
        label="PV-only Halt-Freigabe",
        unit="s",
        configured=pv_only_release_hold_configured,
        live_value=None,
        live_key=None,
        effective=pv_only_release_hold_effective,
        source="user" if pv_only_release_hold_configured is not None else "default",
        severity=pv_only_release_hold_severity,
        message=pv_only_release_hold_message,
    )

    pv_only_stale_guard_configured = (
        safe_float(cfg.get("wb_pv_only_hold_stale_guard_s"), 45.0)
        if _has_user_value(cfg, "wb_pv_only_hold_stale_guard_s")
        else None
    )
    pv_only_stale_guard_effective = min(
        300.0,
        max(
            10.0,
            pv_only_stale_guard_configured if pv_only_stale_guard_configured is not None else 45.0,
        ),
    )
    pv_only_stale_guard_severity = "ok"
    pv_only_stale_guard_message = (
        "Gnadenfrist der Halt-Freigabe: Ein Poll-Aussetzer oder eine kurz nicht bereite "
        "Gruppenzuteilung trägt den Halt-Zustand so lange weiter, ohne einen Halt zu erteilen."
    )
    if pv_only_stale_guard_configured is not None and not 10.0 <= pv_only_stale_guard_configured <= 300.0:
        pv_only_stale_guard_severity = "warning"
        pv_only_stale_guard_message = (
            "Die Gnadenfrist liegt außerhalb 10 bis 300s und wird auf den sicheren Bereich begrenzt. "
            "Zu kurz verliert ein einzelner Aussetzer die Halt-Freigabe, zu lang trägt ein abgesteckter "
            "Zustand unnötig lange."
        )
    wallbox["wb_pv_only_hold_stale_guard_s"] = _entry(
        key="wb_pv_only_hold_stale_guard_s",
        label="PV-only Halt-Gnadenfrist",
        unit="s",
        configured=pv_only_stale_guard_configured,
        live_value=None,
        live_key=None,
        effective=pv_only_stale_guard_effective,
        source="user" if pv_only_stale_guard_configured is not None else "default",
        severity=pv_only_stale_guard_severity,
        message=pv_only_stale_guard_message,
    )

    # Einschwingfrist des Defizitreglers nach neuem Netzbezug
    # (wallbox_manager._wallbox_grid_import_settle_s klemmt auf 0 … 30 s).
    settle_configured = (
        safe_float(cfg.get("wb_grid_import_settle_s"), 10.0)
        if _has_user_value(cfg, "wb_grid_import_settle_s")
        else None
    )
    settle_requested = settle_configured if settle_configured is not None else 10.0
    if not math.isfinite(settle_requested):
        settle_requested = 10.0
    settle_effective = min(30.0, max(0.0, settle_requested))
    settle_severity = "ok"
    settle_message = (
        "Einschwingfrist nach neuem Netzbezug: Der Speicher gleicht eine Laständerung erst nach einigen "
        "Sekunden aus. Solange der Bezug kürzer ansteht und in der Reichweite des Speichers liegt, senkt "
        "die Wallbox nicht ab. Hausanschluss je Phase, Nutzer-Aus und Bezug über der Speicherreichweite "
        "wirken sofort."
    )
    if settle_configured is not None and not 0.0 <= settle_configured <= 30.0:
        settle_severity = "warning"
        settle_message = (
            "Die Einschwingfrist liegt außerhalb 0 bis 30s und wird auf diesen Bereich begrenzt."
        )
    elif settle_configured is not None and settle_effective <= 0.0:
        settle_message = (
            "Einschwingfrist aus: Der Defizitregler senkt bei jedem Netzbezug sofort ab (bisheriges "
            "Verhalten). Kurze Lastspitzen können dann zu Absenkung und erneuter Anhebung führen."
        )
    elif settle_configured is not None and settle_effective < 5.0:
        settle_severity = "warning"
        settle_message = (
            "Die Einschwingfrist ist kürzer als die Reaktionszeit des Speichers (etwa 5 bis 8s); die Wallbox "
            "kann dann Lastspitzen nachregeln, die der Speicher selbst ausgleicht."
        )
    wallbox["wb_grid_import_settle_s"] = _entry(
        key="wb_grid_import_settle_s",
        label="Einschwingfrist Netzbezug",
        unit="s",
        configured=settle_configured,
        live_value=None,
        live_key=None,
        effective=settle_effective,
        source="user" if settle_configured is not None else "default",
        severity=settle_severity,
        message=settle_message,
    )

    valid_priority = ["heatpump", "wallbox", "heater"]
    order_raw = str(cfg.get("consumer_priority_order", "heatpump,wallbox,heater")).strip()
    order = [part.strip().lower() for part in order_raw.split(",") if part.strip()]
    active_consumers = []
    if _is_enabled(cfg, "luxtronik") or safe_float(cfg.get("wp_type"), -1.0) >= 0:
        active_consumers.append("heatpump")
    if _is_enabled(cfg, "wb_native_enable"):
        active_consumers.append("wallbox")
    if _is_enabled(cfg, "heizstab") or _has_user_value(cfg, "heizstab_ip") or _has_user_value(cfg, "shelly_heiz_ip"):
        active_consumers.append("heater")
    priority_severity = "ok"
    priority_message = "Verbraucherpriorität ist plausibel."
    if sorted(order) != sorted(valid_priority) or len(set(order)) != len(order):
        priority_severity = "warning"
        priority_message = "Verbraucherpriorität muss heatpump, wallbox und heater genau einmal enthalten."
    elif "heatpump" in active_consumers and order[0] != "heatpump":
        priority_severity = "warning"
        priority_message = "Die Wärmepumpe ist aktiv, steht aber nicht an erster Stelle; Nachlauf und Komfort bitte bewusst prüfen."
    consumer["consumer_priority_order"] = _entry(
        key="consumer_priority_order",
        label="Verbraucherpriorität",
        unit="",
        configured=order_raw,
        live_value=None,
        live_key=None,
        effective=",".join(order),
        source="user" if _has_user_value(cfg, "consumer_priority_order") else "default",
        severity=priority_severity,
        message=priority_message,
    )

    wp_runon = safe_float(cfg.get("consumer_priority_wp_runon_s"), 600.0)
    consumer["consumer_priority_wp_runon_s"] = _entry(
        key="consumer_priority_wp_runon_s",
        label="WP-Nachlaufzeit",
        unit="s",
        configured=wp_runon if _has_user_value(cfg, "consumer_priority_wp_runon_s") else None,
        live_value=None,
        live_key=None,
        effective=wp_runon,
        source="user" if _has_user_value(cfg, "consumer_priority_wp_runon_s") else "default",
        severity="warning" if wp_runon < 60.0 or wp_runon > 3600.0 else "ok",
        message=(
            "WP-Nachlaufzeit wirkt unplausibel; empfohlen sind grob 60 bis 3600 Sekunden."
            if wp_runon < 60.0 or wp_runon > 3600.0 else "WP-Nachlaufzeit ist plausibel."
        ),
    )

    luxtronik_pause_configured = (
        safe_float(cfg.get("luxtronik_pause_setpoint_c"), 20.0)
        if _has_user_value(cfg, "luxtronik_pause_setpoint_c")
        else None
    )
    luxtronik_pause_raw = (
        luxtronik_pause_configured
        if luxtronik_pause_configured is not None
        else 20.0
    )
    luxtronik_pause_effective = max(15.0, min(22.0, luxtronik_pause_raw))
    luxtronik_pause_in_range = 15.0 <= luxtronik_pause_raw <= 22.0
    consumer["luxtronik_pause_setpoint_c"] = _entry(
        key="luxtronik_pause_setpoint_c",
        label="Luxtronik Absenk-Sollwert",
        unit="°C",
        configured=luxtronik_pause_configured,
        live_value=None,
        live_key=None,
        effective=luxtronik_pause_effective,
        source="user" if luxtronik_pause_configured is not None else "default",
        severity="ok" if luxtronik_pause_in_range else "warning",
        message=(
            "Weiche SHI-Sollwertsperre liegt im EMS-Sicherheitsbereich von 15 bis 22 °C."
            if luxtronik_pause_in_range
            else "Wert außerhalb 15 bis 22 °C; die Laufzeitregelung begrenzt ihn auf den Sicherheitsbereich."
        ),
    )

    tariff = str(cfg.get("stromtarif_typ", "static")).strip().lower()
    basis = safe_float(cfg.get("strompreis_basis"), 25.0)
    cheap = safe_float(cfg.get("strompreis_cheap"), 18.0)
    uht = safe_float(cfg.get("strompreis_uht"), 32.0)
    price_limit = safe_float(cfg.get("price_limit"), 20.0)
    price_pause = safe_float(cfg.get("price_pause_limit"), 35.0)
    price_hard = safe_float(cfg.get("price_hard_limit"), -99.0)
    cheap_grid_limit = safe_float(cfg.get("cheap_grid_price_limit_ct"), 0.0)
    cheap_grid_enabled = _is_enabled(cfg, "cheap_grid_boost_enable")
    cheap_grid_supported = supports_spot_market_prices(cfg)
    heat_price_boost_requested = _is_enabled(cfg, "price_boost_enable")
    heat_price_scope_aliases = {
        "heat": "heating",
        "heizen": "heating",
        "heizung": "heating",
        "heating": "heating",
        "warmwasser": "dhw",
        "ww": "dhw",
        "dhw": "dhw",
        "beide": "both",
        "both": "both",
    }
    heat_price_scope_raw = str(
        cfg.get("heat_price_boost_scope", "both")
    ).strip().lower()
    heat_price_scope_valid = heat_price_scope_raw in heat_price_scope_aliases
    heat_price_scope = heat_price_scope_aliases.get(heat_price_scope_raw)
    heat_price_windows_raw = str(
        cfg.get("heat_price_boost_windows", "") or ""
    ).strip()
    heat_price_windows, heat_price_invalid_windows = (
        _parse_heat_price_boost_windows(heat_price_windows_raw)
    )
    heat_price_windows_valid = bool(
        not heat_price_invalid_windows
        and (
            not heat_price_windows_raw
            or heat_price_windows
        )
    )
    heat_price_tariff_allowed = supports_heat_price_boost(cfg)
    market_min_margin = safe_float(
        cfg.get("market_min_margin_pct"),
        safe_float(cfg.get("direct_marketing_min_margin_pct"), 10.0),
    )
    market_safety_correction = safe_float(
        cfg.get("market_safety_correction_ct_per_kwh"),
        safe_float(cfg.get("direct_marketing_safety_margin_ct_per_kwh"), 0.0),
    )
    # Ladeprofil-Vertrag (reine Funktion des Planers) – Marge/Sicherheit werden von
    # economic/balanced/comfort gesetzt; nur 'custom' liest die Schlüssel. Fehlender Schlüssel -> Ableitung.
    market_profile_raw = str(cfg.get("market_charge_profile") or "").strip().lower()
    market_profile = _market_charge_profile_contract(cfg)
    market_profile_name = str(market_profile.get("profile") or "economic")
    market_profile_unknown = bool(market_profile_raw and market_profile.get("source_class") == "default_fallback")
    market_profile_custom = market_profile_name == "custom"
    market_profile_comfort = market_profile_name == "comfort"
    market_price_limit = safe_float(cfg.get("market_price_limit_ct"), 0.0)
    # Der normale Marktpfad darf ausreichende Speicher-/PV-Deckung nicht per
    # Nutzerschalter übergehen. market_economics.py behandelt dies bereits als
    # feste Schutzinvariante; der historische Schlüssel bleibt wirkungslos.
    # Im Komfort-Profil ersetzt das Zeitziel die Invariante (nur über das Profil, nie über den Schlüssel).
    market_autarky_first = bool(market_profile.get("autarky_first_invariant", True))
    market_autarky_low_soc = safe_float(cfg.get("market_autarky_low_soc_pct"), 20.0)
    market_autarky_buffer_wh = safe_float(cfg.get("market_autarky_horizon_buffer_wh"), 500.0)
    octopus_bad_order = tariff == "octopus_heat" and not (cheap < basis < uht)
    peak_enabled = _is_enabled(cfg, "peak_shaving_enable")
    peak_grid_recharge_enabled = _is_enabled(
        cfg,
        "peak_shaving_grid_recharge_enable",
    )
    peak_values = {
        "peak_shaving_grid_import_limit_w": _configured_finite_number(
            cfg,
            "peak_shaving_grid_import_limit_w",
            10_000.0,
        ),
        "peak_shaving_reserve_soc_pct": _configured_finite_number(
            cfg,
            "peak_shaving_reserve_soc_pct",
            30.0,
        ),
        "peak_shaving_max_discharge_w": _configured_finite_number(
            cfg,
            "peak_shaving_max_discharge_w",
            0.0,
        ),
        "peak_shaving_recharge_max_w": _configured_finite_number(
            cfg,
            "peak_shaving_recharge_max_w",
            0.0,
        ),
        "peak_shaving_control_margin_w": _configured_finite_number(
            cfg,
            "peak_shaving_control_margin_w",
            300.0,
        ),
        "peak_shaving_hysteresis_w": _configured_finite_number(
            cfg,
            "peak_shaving_hysteresis_w",
            600.0,
        ),
        "peak_shaving_soc_hysteresis_pct": _configured_finite_number(
            cfg,
            "peak_shaving_soc_hysteresis_pct",
            1.0,
        ),
        "peak_shaving_max_sample_gap_s": _configured_finite_number(
            cfg,
            "peak_shaving_max_sample_gap_s",
            10.0,
        ),
        "peak_shaving_release_debounce_s": _configured_finite_number(
            cfg,
            "peak_shaving_release_debounce_s",
            20.0,
        ),
    }
    peak_limit_w = peak_values["peak_shaving_grid_import_limit_w"][0]
    peak_reserve_soc = peak_values["peak_shaving_reserve_soc_pct"][0]
    peak_validation = {
        "peak_shaving_grid_import_limit_w": (
            peak_values["peak_shaving_grid_import_limit_w"][1]
            and peak_limit_w >= 1000.0
        ),
        "peak_shaving_reserve_soc_pct": (
            peak_values["peak_shaving_reserve_soc_pct"][1]
            and peak_reserve_soc > reserve_effective
            and peak_reserve_soc <= 100.0
        ),
        "peak_shaving_max_discharge_w": (
            peak_values["peak_shaving_max_discharge_w"][1]
            and peak_values["peak_shaving_max_discharge_w"][0] >= 0.0
        ),
        "peak_shaving_recharge_max_w": (
            peak_values["peak_shaving_recharge_max_w"][1]
            and peak_values["peak_shaving_recharge_max_w"][0] >= 0.0
        ),
        "peak_shaving_control_margin_w": (
            peak_values["peak_shaving_control_margin_w"][1]
            and 0.0 <= peak_values["peak_shaving_control_margin_w"][0]
            <= max(0.0, peak_limit_w - 300.0)
        ),
        "peak_shaving_hysteresis_w": (
            peak_values["peak_shaving_hysteresis_w"][1]
            and peak_values["peak_shaving_hysteresis_w"][0] > 300.0
        ),
        "peak_shaving_soc_hysteresis_pct": (
            peak_values["peak_shaving_soc_hysteresis_pct"][1]
            and 0.1
            <= peak_values["peak_shaving_soc_hysteresis_pct"][0]
            <= 5.0
        ),
        "peak_shaving_max_sample_gap_s": (
            peak_values["peak_shaving_max_sample_gap_s"][1]
            and 2.0 <= peak_values["peak_shaving_max_sample_gap_s"][0] <= 60.0
        ),
        "peak_shaving_release_debounce_s": (
            peak_values["peak_shaving_release_debounce_s"][1]
            and 5.0
            <= peak_values["peak_shaving_release_debounce_s"][0]
            <= 120.0
        ),
    }
    peak_messages = {
        "peak_shaving_grid_import_limit_w": (
            "Die 15-Minuten-Grenze muss mindestens 1.000 W betragen."
        ),
        "peak_shaving_reserve_soc_pct": (
            "Die Lastspitzen-Schwelle muss über der wirksamen "
            f"Notstromreserve von {reserve_effective:.1f} % liegen und darf "
            "100 % nicht überschreiten."
        ),
        "peak_shaving_max_discharge_w": (
            "Die Entladeobergrenze muss mindestens 0 W betragen."
        ),
        "peak_shaving_recharge_max_w": (
            "Die Netz-Nachladegrenze muss mindestens 0 W betragen."
        ),
        "peak_shaving_control_margin_w": (
            "Der Sicherheitsabstand muss mindestens 0 W betragen und "
            "mindestens 300 W Regelraum unter der Bezugsgrenze lassen."
        ),
        "peak_shaving_hysteresis_w": (
            "Die Leistungshysterese muss strikt größer als die kleinste "
            "300-W-Leistungsgrenze sein; empfohlen sind mindestens 600 W."
        ),
        "peak_shaving_soc_hysteresis_pct": (
            "Die SoC-Hysterese muss zwischen 0,1 und 5,0 Prozentpunkten liegen."
        ),
        "peak_shaving_max_sample_gap_s": (
            "Die größte Messlücke muss zwischen 2 und 60 Sekunden liegen."
        ),
        "peak_shaving_release_debounce_s": (
            "Die Freigabe-Entprellung muss zwischen 5 und 120 Sekunden liegen."
        ),
    }
    peak_labels = {
        "peak_shaving_grid_import_limit_w": ("15-Minuten-Netzbezugsgrenze", "W"),
        "peak_shaving_reserve_soc_pct": ("Lastspitzen-Speicherschwelle", "%"),
        "peak_shaving_max_discharge_w": ("Lastspitzen max. Entladung", "W"),
        "peak_shaving_recharge_max_w": ("Lastspitzen max. Netz-Nachladung", "W"),
        "peak_shaving_control_margin_w": ("Lastspitzen-Sicherheitsabstand", "W"),
        "peak_shaving_hysteresis_w": ("Lastspitzen-Leistungshysterese", "W"),
        "peak_shaving_soc_hysteresis_pct": ("Lastspitzen-SoC-Hysterese", "%"),
        "peak_shaving_max_sample_gap_s": ("Lastspitzen max. Messlücke", "s"),
        "peak_shaving_release_debounce_s": ("Lastspitzen Freigabe-Entprellung", "s"),
    }
    peak_contract_valid = all(
        valid
        for key, valid in peak_validation.items()
        if key != "peak_shaving_recharge_max_w"
        or peak_grid_recharge_enabled
    )
    for key, (value, typed) in peak_values.items():
        valid = bool(peak_validation.get(key))
        relevant = bool(
            peak_enabled
            and (
                key != "peak_shaving_recharge_max_w"
                or peak_grid_recharge_enabled
            )
        )
        label, unit = peak_labels[key]
        price[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=value if _has_user_value(cfg, key) and typed else None,
            live_value=None,
            live_key=None,
            effective=value if typed else None,
            source="user" if _has_user_value(cfg, key) else "default",
            severity="warning" if relevant and not valid else "ok",
            message=(
                peak_messages[key]
                if relevant and not valid
                else (
                    "Wert ist plausibel; die Regelung greift nur ein, wenn "
                    "der Netzbezug seit Beginn der aktuellen festen "
                    "Viertelstunde lückenlos belegt ist."
                    if peak_enabled
                    else "Lastspitzenbegrenzung ist aus; der Wert wirkt nicht."
                )
            ),
        )
    price["peak_shaving_enable"] = _entry(
        key="peak_shaving_enable",
        label="Lastspitzenbegrenzung",
        unit="-",
        configured=cfg.get("peak_shaving_enable")
        if _has_user_value(cfg, "peak_shaving_enable")
        else None,
        live_value=None,
        live_key=None,
        effective=peak_enabled,
        source="user"
        if _has_user_value(cfg, "peak_shaving_enable")
        else "default",
        severity=(
            "warning"
            if peak_enabled and not peak_contract_valid
            else "ok"
        ),
        message=(
            "Konfiguration ungültig: Die Regelung bleibt passiv und übernimmt "
            "keine Speicherentscheidung."
            if peak_enabled and not peak_contract_valid
            else (
                "Aktiv: Der Speicher begrenzt den 15-Minuten-Netzbezug nur "
                "mit E3/DC-AUTO und flüchtiger Entladeobergrenze; er fordert "
                "weder Entladung noch Einspeisung an."
                if peak_enabled
                else "Aus: keine Regelhoheit und keine Speicherwirkung."
            )
        ),
    )
    usable_peak_pct = max(0.0, peak_reserve_soc - reserve_effective)
    price["peak_shaving_reserve_contract"] = _entry(
        key="peak_shaving_reserve_contract",
        label="Nutzbarer Lastspitzenpuffer",
        unit="%",
        configured=peak_reserve_soc if peak_values["peak_shaving_reserve_soc_pct"][1] else None,
        live_value=reserve_effective,
        live_key=reserve_live_key,
        effective=usable_peak_pct,
        source="user_minus_physical_reserve",
        severity=(
            "warning"
            if peak_enabled
            and not peak_validation["peak_shaving_reserve_soc_pct"]
            else "ok"
        ),
        message=(
            f"Von der eingestellten {peak_reserve_soc:.1f}-%-Schwelle bis zur "
            f"wirksamen Notstromreserve von {reserve_effective:.1f} % sind "
            f"{usable_peak_pct:.1f} Prozentpunkte ausschließlich als "
            "Lastspitzenpuffer reserviert. Bei einer echten Lastspitze darf "
            "dieser Puffer bis zur physischen Reserve genutzt werden."
        ),
    )
    price["strompreis_cheap"] = _entry(
        key="strompreis_cheap",
        label="Günstigpreis",
        unit="ct/kWh",
        configured=cheap if _has_user_value(cfg, "strompreis_cheap") else None,
        live_value=None,
        live_key=None,
        effective=cheap,
        source="user" if _has_user_value(cfg, "strompreis_cheap") else "default",
        severity="warning" if octopus_bad_order else "ok",
        message=(
            "Octopus-Heat-Preise sollten günstig < normal < Hochpreis sein."
            if octopus_bad_order else "Preisfeld ist plausibel."
        ),
    )
    price["strompreis_basis"] = _entry(
        key="strompreis_basis",
        label="Normalpreis",
        unit="ct/kWh",
        configured=basis if _has_user_value(cfg, "strompreis_basis") else None,
        live_value=None,
        live_key=None,
        effective=basis,
        source="user" if _has_user_value(cfg, "strompreis_basis") else "default",
        severity="warning" if basis <= 0.0 or octopus_bad_order else "ok",
        message="Normalpreis wirkt unplausibel." if basis <= 0.0 else ("Octopus-Heat-Preise sollten günstig < normal < Hochpreis sein." if octopus_bad_order else "Preisfeld ist plausibel."),
    )
    price["strompreis_uht"] = _entry(
        key="strompreis_uht",
        label="Hochpreis",
        unit="ct/kWh",
        configured=uht if _has_user_value(cfg, "strompreis_uht") else None,
        live_value=None,
        live_key=None,
        effective=uht,
        source="user" if _has_user_value(cfg, "strompreis_uht") else "default",
        severity="warning" if octopus_bad_order else "ok",
        message=(
            "Octopus-Heat-Preise sollten günstig < normal < Hochpreis sein."
            if octopus_bad_order else "Preisfeld ist plausibel."
        ),
    )
    price_order_warning = price_limit > price_pause
    heat_price_pilot_ready = bool(
        heat_price_boost_requested
        and heat_price_scope_valid
        and cheap_grid_supported
        and _is_enabled(cfg, "luxtronik")
        and safe_float(cfg.get("wp_type"), -1.0) == 0
        and _is_enabled({"auto_mode": cfg.get("auto_mode", 1)}, "auto_mode")
        and _is_enabled(cfg, "heat_policy_runtime_enable")
        and cheap_grid_enabled
        and _is_enabled(cfg, "cheap_grid_heatpump_enable")
    )
    price["price_boost_enable"] = _entry(
        key="price_boost_enable",
        label="Experimenteller Wärmepumpen-Netzboost",
        unit="-",
        configured=(
            cfg.get("price_boost_enable")
            if _has_user_value(cfg, "price_boost_enable")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=heat_price_pilot_ready,
        source=("experimental" if heat_price_boost_requested else (
            "user" if _has_user_value(cfg, "price_boost_enable") else "default"
        )),
        severity="warning" if heat_price_boost_requested else "ok",
        message=(
            "Aus: Der experimentelle Wärmepumpen-Netzboost ist nicht freigegeben."
            if not heat_price_boost_requested else (
                "Experimenteller Testbetrieb freigegeben: Eine Luxtronik darf bei "
                "einem bestätigten Negativpreisfenster, Wärmebedarf und aktueller "
                "Speicherzusage boosten. Die Konfiguration bestätigt keinen laufenden "
                "Boost. Allgemeine günstige Preisfenster bleiben ohne Steuerwirkung."
                if heat_price_pilot_ready else
                "Experimenteller Testbetrieb gesperrt: Benötigt Luxtronik mit Automatik, "
                "gemeinsame Wärmeplanung, echten Börsentarif, beide Negativpreisfreigaben "
                "sowie eine aktuelle Speicherzusage. Allgemeine günstige "
                "Preisfenster bleiben ohne Steuerwirkung."
            )
        ),
    )
    price["heat_price_boost_scope"] = _entry(
        key="heat_price_boost_scope",
        label="Wärmepumpen-Preisverschiebung Ziel",
        unit="-",
        configured=(
            cfg.get("heat_price_boost_scope")
            if _has_user_value(cfg, "heat_price_boost_scope")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=heat_price_scope if heat_price_scope_valid else None,
        source=(
            "user"
            if _has_user_value(cfg, "heat_price_boost_scope")
            else "default"
        ),
        severity=(
            "warning"
            if heat_price_boost_requested and not heat_price_scope_valid
            else "ok"
        ),
        message=(
            "Ungültiges Wärmeziel; Tarif- und Negativpreis-Boost sind gesperrt."
            if not heat_price_scope_valid
            else (
                "Zielauswahl ist plausibel; sie erzeugt ohne vollständigen "
                "Intent-Vertrag keinen Aktorbefehl."
            )
        ),
    )
    price["heat_price_boost_windows"] = _entry(
        key="heat_price_boost_windows",
        label="Wärmepumpen-Preisverschiebung Zeitfenster",
        unit="-",
        configured=(
            cfg.get("heat_price_boost_windows")
            if _has_user_value(cfg, "heat_price_boost_windows")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=None,
        source=(
            "user"
            if _has_user_value(cfg, "heat_price_boost_windows")
            else "default"
        ),
        severity="ok",
        message="Gespeicherte Altfenster sind ohne Wirkung auf den Negativpreis-Boost.",
    )
    for key, label, value in (
        ("price_limit", "Boost-Preislimit", price_limit),
        ("price_pause_limit", "Sperr-Preislimit", price_pause),
        ("cheap_grid_price_limit_ct", "Speicher-Netzladen Preislimit", cheap_grid_limit),
    ):
        enabled = key != "cheap_grid_price_limit_ct" or cheap_grid_enabled
        severity = "ok"
        message = "Preislimit ist plausibel."
        if key != "cheap_grid_price_limit_ct" and enabled and value < 0.0:
            severity = "warning"
            message = "Preislimit sollte nicht negativ sein."
        elif key in {"price_limit", "price_pause_limit"} and price_order_warning:
            severity = "warning"
            message = "Das Boost-Preislimit liegt über dem Sperr-Preislimit; bitte die Reihenfolge prüfen."
        elif key == "cheap_grid_price_limit_ct" and cheap_grid_enabled and not cheap_grid_supported:
            severity = "warning"
            message = "Negativpreis-Freigaben sind nur bei echten Börsenpreistarifen wirksam."
        elif key == "cheap_grid_price_limit_ct" and enabled and value > 0.0:
            severity = "warning"
            message = "Positive Werte öffnen keinen Günstigpreis-Pfad; der Sonderpfad bleibt strikt auf negative Abrechnungspreise begrenzt."
        elif key == "cheap_grid_price_limit_ct" and enabled:
            message = "0 ct/kWh bedeutet: nur bei tatsächlich negativem Abrechnungspreis; ein negativer Wert verschärft die Grenze."
        price[key] = _entry(
            key=key,
            label=label,
            unit="ct/kWh",
            configured=value if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity=severity,
            message=message,
        )

    # Ladeprofil und Komfort-Preislimit bewerten (vor Marge/Sicherheit).
    price["market_charge_profile"] = _entry(
        key="market_charge_profile",
        label="Ladeprofil Speicher-Netzladen",
        unit="-",
        configured=cfg.get("market_charge_profile") if _has_user_value(cfg, "market_charge_profile") else None,
        live_value=None,
        live_key=None,
        effective=market_profile_name,
        source=(
            "user" if (_has_user_value(cfg, "market_charge_profile") and not market_profile_unknown)
            else ("fallback" if market_profile_unknown else "derived")
        ),
        severity="warning" if market_profile_unknown else "ok",
        message=(
            "Unbekanntes Ladeprofil, es gilt Wirtschaftlich."
            if market_profile_unknown
            else (
                "Profil %s: Marge %s %%, Sicherheitskorrektur %s ct/kWh%s."
                % (
                    _market_profile_label(market_profile_name),
                    _fmt_de(safe_float(market_profile.get("min_margin_pct"), 0.0), 1),
                    _fmt_de(safe_float(market_profile.get("safety_ct"), 0.0), 2),
                    (", Netzladen im Preisfenster bis zum Zielstand am Fensterende (PV zuerst)" if market_profile_comfort else ""),
                )
            )
        ),
    )
    if market_profile_comfort:
        limit_severity = "ok"
        if market_price_limit > 0.0:
            limit_message = "Preisfenster: Slots ≤ %s ct/kWh." % _fmt_de(market_price_limit, 1)
            if tariff in ("octopus_heat", "special", "spezial", "special_tariff"):
                tariff_prices = [cheap, basis, uht] if tariff == "octopus_heat" else [basis]
                if market_price_limit + 0.001 < min(tariff_prices):
                    limit_severity = "warning"
                    limit_message = "Komfort lädt nie: Limit %s ct liegt unter dem günstigsten Tarifpreis %s ct." % (_fmt_de(market_price_limit, 1), _fmt_de(min(tariff_prices), 2))
                elif market_price_limit + 0.001 >= basis:
                    limit_severity = "warning"
                    limit_message = "Komfort lädt auch zu Normalpreis-Stunden (Limit %s ≥ Basis %s ct)." % (_fmt_de(market_price_limit, 1), _fmt_de(basis, 2))
            else:
                limit_message = "Fester Wert %s ct/kWh; ohne Wert gilt der Mittelpreis des gebundenen Preishorizonts." % _fmt_de(market_price_limit, 1)
        else:
            limit_message = "Leer = Mittelpreis des Tarifs (Octopus Heat: 8 h günstig, 13 h Basis, 3 h teuer) bzw. des gebundenen Preishorizonts, je Plan bestimmt."
        price["market_price_limit_ct"] = _entry(
            key="market_price_limit_ct",
            label="Komfort-Preislimit",
            unit="ct/kWh",
            configured=cfg.get("market_price_limit_ct") if _has_user_value(cfg, "market_price_limit_ct") else None,
            live_value=None,
            live_key=None,
            effective=market_price_limit if market_price_limit > 0.0 else None,
            source="user" if market_price_limit > 0.0 else "tariff_mean",
            severity=limit_severity,
            message=limit_message,
        )
        if not _is_enabled(cfg, "market_battery_grid_charge_enable"):
            price["market_charge_profile"]["severity"] = "warning"
            price["market_charge_profile"]["message"] = "Komfort-Profil ohne Speicher-Netzladen-Freigabe bleibt wirkungslos (Speicher-Netzladen einschalten)."
    for key, label, value, unit, low, high in (
        ("market_min_margin_pct", "Preis-Mindestmarge", market_min_margin, "%", 0.0, None),
        ("market_safety_correction_ct_per_kwh", "Preis-Sicherheitskorrektur", market_safety_correction, "ct/kWh", -10.0, 50.0),
        ("market_autarky_low_soc_pct", "PV-autark Low-SOC-Ausnahme", market_autarky_low_soc, "%", 0.0, 100.0),
        ("market_autarky_horizon_buffer_wh", "PV-autark Horizontpuffer", market_autarky_buffer_wh, "Wh", 0.0, None),
    ):
        configured = cfg.get(key) if _has_user_value(cfg, key) else None
        source = (
            "user"
            if _has_user_value(cfg, key)
            else (
                "legacy_direct_marketing"
                if (
                    key == "market_min_margin_pct"
                    and _has_user_value(cfg, "direct_marketing_min_margin_pct")
                ) or (
                    key == "market_safety_correction_ct_per_kwh"
                    and _has_user_value(cfg, "direct_marketing_safety_margin_ct_per_kwh")
                )
                else "default"
            )
        )
        severity = "ok"
        message = "Wert ist plausibel."
        if low is not None and value < low:
            severity = "warning"
            message = "Wert liegt unter der erlaubten Untergrenze."
        elif high is not None and value > high:
            severity = "warning"
            message = "Wert liegt über der erlaubten Obergrenze."
        # Profilbewusst – bei economic/balanced/comfort setzt das Profil Marge und
        # Sicherheitskorrektur; abweichende Schlüsselwerte werden nicht verwendet (Warnung, kein Fehler).
        elif key == "market_min_margin_pct" and not market_profile_custom and abs(value - safe_float(market_profile.get("min_margin_pct"), 10.0)) > 0.05:
            severity = "warning"
            message = (
                "Mindestmarge %s %% wird im Profil %s nicht verwendet (das Profil setzt %s %%). "
                "Für eigene Werte „Eigene Einstellungen“ wählen."
                % (_fmt_de(value, 1), _market_profile_label(market_profile_name), _fmt_de(safe_float(market_profile.get("min_margin_pct"), 10.0), 0))
            )
        elif key == "market_safety_correction_ct_per_kwh" and not market_profile_custom and abs(value) > 0.005:
            severity = "warning"
            message = (
                "Sicherheitskorrektur %s ct wird im Profil %s nicht verwendet (das Profil setzt 0). "
                "Meintest Du Profil Ausgeglichen oder Komfort? Für eigene Werte „Eigene Einstellungen“ wählen."
                % (_fmt_de(value, 2), _market_profile_label(market_profile_name))
            )
        elif key == "market_min_margin_pct" and market_profile_custom and value < 10.0:
            severity = "warning"
            message = "Mindestmarge unter 10 Prozent; preisbasierte Speicherladung wird aggressiver."
        elif key == "market_safety_correction_ct_per_kwh" and market_profile_custom and value < 0.0:
            message = "Negative Korrektur macht die Preisregelung aggressiver; Mindestmarge bleibt als Schutz aktiv."
        elif key == "market_autarky_low_soc_pct" and value < 15.0:
            severity = "warning"
            message = "Low-SOC-Ausnahme unter 15 Prozent lässt Markt-Netzladen erst sehr spät wieder zu."
        elif key == "market_autarky_horizon_buffer_wh" and value > 5000.0:
            severity = "warning"
            message = "Sehr hoher Autarkie-Puffer kann Netzladen trotz eigentlich ausreichender Tagesprognose wieder erlauben."
        price[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=configured,
            live_value=None,
            live_key=None,
            effective=value,
            source=source,
            severity=severity,
            message=message,
        )

    price["market_autarky_first_enable"] = _entry(
        key="market_autarky_first_enable",
        label="PV-autark zuerst",
        unit="-",
        configured=(
            cfg.get("market_autarky_first_enable")
            if _has_user_value(cfg, "market_autarky_first_enable")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=market_autarky_first,
        source="system_invariant" if market_autarky_first else "profile_comfort",
        severity="ok",
        message=(
            "Schutzinvariante: Normales Markt-Netzladen und Speicher-Halten "
            "werden bei ausreichender Horizontprognose blockiert. Ein "
            "historischer Konfigurationswert kann diesen Schutz nicht abschalten."
            if market_autarky_first
            else "Im Komfort-Profil durch das Zeitziel ersetzt: PV zuerst, Netz nur für den Rest bis zum Fensterende. "
                 "Ein Konfigurationswert ändert daran nichts."
        ),
    )

    for key, label, default_enabled in (
        ("market_battery_grid_charge_enable", "Marktpfad Speicher-Netzladen", False),
        ("market_battery_hold_enable", "Marktpfad Speicher-Entladesperre", False),
        ("market_wallbox_enable", "Marktpfad Wallbox", False),
        ("market_heatpump_enable", "Marktpfad Wärmepumpe", False),
        ("market_heater_enable", "Marktpfad Heizstab", False),
    ):
        configured = cfg.get(key) if _has_user_value(cfg, key) else None
        if key == "market_heatpump_enable":
            user_enabled = _is_enabled(cfg, key) if _has_user_value(cfg, key) else False
            price[key] = _entry(
                key=key,
                label=label,
                unit="-",
                configured=configured,
                live_value=None,
                live_key=None,
                effective=False,
                source="legacy_ignored" if _has_user_value(cfg, key) else "default",
                severity="warning" if user_enabled else "ok",
                message=(
                    "Der normale Marktpfad steuert Wärmepumpen nicht mehr. "
                    "Wärmepumpen laufen über PV-/Forecast-Budget, Pre-Dump oder den separaten Negativpreis-Boost."
                    if user_enabled
                    else "Normale Marktpfad-Freigabe für Wärmepumpen ist aus."
                ),
            )
            continue
        if _has_user_value(cfg, key):
            effective = _is_enabled(cfg, key)
            source = "user"
        else:
            effective = bool(default_enabled)
            source = "default"
        hold_included = key == "market_battery_hold_enable" and _is_enabled(cfg, "market_battery_grid_charge_enable")
        if hold_included:
            effective = True
            source = "grid_charge_includes_hold"
        price[key] = _entry(
            key=key,
            label=label,
            unit="-",
            configured=configured,
            live_value=None,
            live_key=None,
            effective=effective,
            source=source,
            severity="ok",
            message=(
                "Speicher-Netzladen schließt Speicher-Halten ein; der tatsächliche Einsatz bleibt bedarfs- und preisabhängig."
                if hold_included else
                "Marktpfad-Freigabe ist aktiv; der tatsächliche Einsatz bleibt bedarfs- und preisabhängig."
                if effective and key.startswith("market_battery_")
                else ("Marktpfad-Freigabe ist aktiv." if effective else "Marktpfad-Freigabe ist aus.")
            ),
        )
    if _has_user_value(cfg, "market_battery_enable"):
        price["market_battery_enable"] = _entry(
            key="market_battery_enable",
            label="Marktpfad Speicher (Alt-Schalter)",
            unit="-",
            configured=cfg.get("market_battery_enable"),
            live_value=None,
            live_key=None,
            effective=False,
            source="legacy_ignored",
            severity="warning",
            message="Alt-Schalter wird für normales Prognose-Netzladen und Speicher-Halten nicht mehr ausgewertet.",
        )

    heat_policy_runtime = _is_enabled(cfg, "heat_policy_runtime_enable")
    ems_budget_runtime = _is_enabled(cfg, "ems_budget_runtime_enable")
    heater_grid_boost = _is_enabled(cfg, "heat_heater_grid_boost_enable")
    heater_grid_ack = _is_enabled(cfg, "heat_heater_grid_boost_ack")
    heater_requires_deficit = (
        _is_enabled(cfg, "heat_heater_grid_boost_requires_deficit")
        if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit")
        else True
    )
    heater_price_limit = safe_float(cfg.get("heat_heater_grid_boost_price_limit_ct"), 0.0)
    heater_grid_max_w = safe_float(cfg.get("heat_heater_grid_boost_max_w"), 3000.0)
    heater_min_temp = safe_float(cfg.get("heat_heater_min_temp_c"), 45.0)
    heater_max_temp = safe_float(cfg.get("heat_heater_max_temp_c"), 60.0)
    heat_daily_fallback = safe_float(cfg.get("heat_wp_daily_kwh"), 0.0)

    price["heat_policy_runtime_enable"] = _entry(
        key="heat_policy_runtime_enable",
        label="Central Heat Policy Runtime",
        unit="-",
        configured=cfg.get("heat_policy_runtime_enable") if _has_user_value(cfg, "heat_policy_runtime_enable") else None,
        live_value=None,
        live_key=None,
        effective=heat_policy_runtime,
        source="user" if _has_user_value(cfg, "heat_policy_runtime_enable") else "default",
        severity="warning" if heat_policy_runtime else "ok",
        message=(
            "Pilot aktiv: zentrale Wärme-Policy darf WP-Starts und Heizstab-Netzboost begrenzen. "
            "Dieser Schalter allein aktiviert keinen Wärmepumpen-Preis-Boost."
            if heat_policy_runtime
            else "Shadow-Betrieb: zentrale Wärme-Policy schreibt Diagnose, bestehende Logik bleibt führend; der Wärmepumpen-Preis-Boost bleibt effektiv aus."
        ),
    )
    price["ems_budget_runtime_enable"] = _entry(
        key="ems_budget_runtime_enable",
        label="EMS-Budget-Runtime",
        unit="-",
        configured=cfg.get("ems_budget_runtime_enable") if _has_user_value(cfg, "ems_budget_runtime_enable") else None,
        live_value=None,
        live_key=None,
        effective=ems_budget_runtime,
        source="user" if _has_user_value(cfg, "ems_budget_runtime_enable") else "default",
        severity="warning" if ems_budget_runtime else "ok",
        message=(
            "Pilot aktiv: produktive Verbraucherbudgets werden nur noch nach zentralem Budget-Latch und gültigem Ack freigegeben; bei Datenverlust fällt das System auf AUTO-Freilauf zurück."
            if ems_budget_runtime
            else "Shadow-Betrieb: Budget-Arbitration wird diagnostiziert, greift aber nicht produktiv in Verbraucherbudgets ein."
        ),
    )
    price["heat_heater_grid_boost_enable"] = _entry(
        key="heat_heater_grid_boost_enable",
        label="Heizstab-Netzboost",
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_enable") if _has_user_value(cfg, "heat_heater_grid_boost_enable") else None,
        live_value=None,
        live_key=None,
        effective=heater_grid_boost,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_enable") else "default",
        severity="warning" if heater_grid_boost else "ok",
        message=(
            "Heizstab-Netzboost ist freigegeben; COP=1 nur mit Preislimit, Defizit und Temperaturgrenzen sinnvoll."
            if heater_grid_boost
            else "Heizstab-Netzboost ist aus."
        ),
    )
    price["heat_heater_grid_boost_ack"] = _entry(
        key="heat_heater_grid_boost_ack",
        label="Heizstab COP=1 bestätigt",
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_ack") if _has_user_value(cfg, "heat_heater_grid_boost_ack") else None,
        live_value=None,
        live_key=None,
        effective=heater_grid_ack,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_ack") else "default",
        severity="warning" if heater_grid_boost and not heater_grid_ack else "ok",
        message=(
            "COP=1-Warnung nicht bestätigt; die Policy blockiert Heizstab-Netzboost."
            if heater_grid_boost and not heater_grid_ack
            else "COP=1-Warnung bestätigt." if heater_grid_ack else "Keine Bestätigung nötig, solange Heizstab-Netzboost aus ist."
        ),
    )
    price["heat_heater_grid_boost_requires_deficit"] = _entry(
        key="heat_heater_grid_boost_requires_deficit",
        label="Heizstab nur Prognose-Defizit",
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_requires_deficit") if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit") else None,
        live_value=None,
        live_key=None,
        effective=heater_requires_deficit,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit") else "default",
        severity="ok" if heater_requires_deficit else "warning",
        message=(
            "Heizstab-Netzboost wird auf die berechnete Wärme-Defizitdeckung begrenzt."
            if heater_requires_deficit
            else "Defizitbindung ist ausgeschaltet; das ist nur für bewusst freigegebene Sonderfälle sinnvoll."
        ),
    )
    for key, label, value, unit, low, high in (
        ("heat_heater_grid_boost_price_limit_ct", "Heizstab Preislimit", heater_price_limit, "ct/kWh", None, None),
        ("heat_heater_grid_boost_max_w", "Heizstab Netzlimit", heater_grid_max_w, "W", 0.0, None),
        ("heat_heater_min_temp_c", "Heizstab Mindesttemperatur", heater_min_temp, "°C", 0.0, 95.0),
        ("heat_heater_max_temp_c", "Heizstab Maximaltemperatur", heater_max_temp, "°C", 0.0, 95.0),
        ("heat_wp_daily_kwh", "Fallback Wärmebedarf", heat_daily_fallback, "kWh/24h", 0.0, 120.0),
    ):
        severity = "ok"
        message = "Wert ist plausibel."
        if low is not None and value < low:
            severity = "warning"
            message = "Wert liegt unter der erlaubten Untergrenze."
        elif high is not None and value > high:
            severity = "warning"
            message = "Wert liegt über der erwarteten Obergrenze."
        elif key == "heat_heater_grid_boost_price_limit_ct" and heater_grid_boost and value > 0.0:
            severity = "warning"
            message = "Positives Heizstab-Preislimit: COP=1 kann teurer sein als Wärmepumpe oder späterer PV-Betrieb."
        elif key == "heat_heater_grid_boost_max_w" and heater_grid_boost and value <= 0.0:
            severity = "warning"
            message = "Heizstab-Netzboost ist aktiv, aber das Netzlimit ist 0 W."
        elif key == "heat_heater_max_temp_c" and heater_max_temp <= heater_min_temp:
            severity = "warning"
            message = "Maximaltemperatur muss über der Mindesttemperatur liegen."
        price[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=cfg.get(key) if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity=severity,
            message=message,
        )

    price["price_hard_limit"] = _entry(
        key="price_hard_limit",
        label="Zwangs-Preislimit",
        unit="ct/kWh",
        configured=price_hard if _has_user_value(cfg, "price_hard_limit") else None,
        live_value=None,
        live_key=None,
        effective=price_hard,
        source="user" if _has_user_value(cfg, "price_hard_limit") else "default",
        severity="warning" if price_hard != -99.0 and price_hard > price_limit else "ok",
        message=(
            "Zwangs-Limit sollte unter oder gleich dem normalen Preislimit liegen; -99 deaktiviert es."
            if price_hard != -99.0 and price_hard > price_limit else "Zwangs-Preislimit ist plausibel oder deaktiviert."
        ),
    )

    dm_enabled = _is_enabled(cfg, "direct_marketing_enable")
    dm_settlement_basis = str(
        cfg.get("direct_marketing_settlement_basis", "day_ahead_15min") or "day_ahead_15min"
    ).strip().lower().replace("-", "_")
    dm_settlement_active_supported = dm_settlement_basis == "day_ahead_15min"
    dm_mode_raw = str(cfg.get("direct_marketing_mode", "safe")).strip().lower().replace("-", "_")
    if dm_mode_raw not in {"safe", "eco", "eco_plus", "arbitrage"}:
        dm_mode = "safe"
        dm_mode_warning = True
    else:
        dm_mode = dm_mode_raw
        dm_mode_warning = False
    dm_profit_profile_raw = str(cfg.get("direct_marketing_profit_profile", "standard")).strip().lower().replace("-", "_")
    if dm_profit_profile_raw not in {"standard", "aggressive", "expert"}:
        dm_profit_profile = "standard"
        dm_profit_profile_warning = True
    else:
        dm_profit_profile = dm_profit_profile_raw
        dm_profit_profile_warning = False
    dm_export_enabled = _is_enabled(cfg, "direct_marketing_export_enable")
    dm_grid_charge_enabled = _is_enabled(cfg, "direct_marketing_grid_charge_enable")
    dm_arbitrage_requested = bool(
        _is_enabled(cfg, "direct_marketing_arbitrage_enable")
        or _is_enabled(cfg, "direct_marketing_arbitrage_experimental_enable")
        or dm_mode == "arbitrage"
    )
    dm_pv_store_enabled = str(cfg.get("direct_marketing_pv_store_enable", "1")).strip().lower() in {"1", "true", "yes", "on", "ein"}
    dm_min_margin = safe_float(cfg.get("direct_marketing_min_margin_pct"), 10.0)
    dm_min_profit = safe_float(cfg.get("direct_marketing_min_profit_ct_per_kwh"), 0.0)
    dm_min_window_profit_eur = safe_float(cfg.get("direct_marketing_min_window_profit_eur"), 0.25)
    dm_min_export_energy_kwh = safe_float(cfg.get("direct_marketing_min_export_energy_kwh"), 1.5)
    dm_min_export_window_min = safe_float(cfg.get("direct_marketing_min_export_window_min"), 15.0)
    dm_preferred_export_plateau_min = safe_float(cfg.get("direct_marketing_preferred_export_plateau_min"), 60.0)
    dm_price_plateau_tolerance_ct = safe_float(cfg.get("direct_marketing_price_plateau_tolerance_ct"), 0.75)
    dm_max_daily_export_kwh = safe_float(cfg.get("direct_marketing_max_daily_export_kwh"), 0.0)
    dm_deep_cycle_threshold_pct = safe_float(cfg.get("direct_marketing_deep_cycle_threshold_pct"), 20.0)
    dm_deep_cycle_lcos_factor = safe_float(cfg.get("direct_marketing_deep_cycle_lcos_factor"), 0.5)
    dm_profit_hold = safe_float(cfg.get("direct_marketing_profit_hold_ct_per_kwh"), 0.5)
    dm_margin_hold = safe_float(cfg.get("direct_marketing_margin_hold_pct"), 5.0)
    dm_degradation = safe_float(cfg.get("direct_marketing_degradation_ct_per_kwh"), 4.0)
    dm_efficiency = safe_float(cfg.get("direct_marketing_roundtrip_efficiency_pct"), 85.0)
    dm_fee_ct = safe_float(cfg.get("direct_marketing_fee_ct_per_kwh"), 0.0)
    dm_fee_pct = safe_float(cfg.get("direct_marketing_fee_pct"), 0.0)
    dm_monthly_fee = safe_float(cfg.get("direct_marketing_monthly_fee_eur"), 0.0)
    dm_variable_fee_basis = str(cfg.get("direct_marketing_variable_fee_basis", "sell_revenue") or "sell_revenue").strip().lower().replace("-", "_")
    dm_variable_fee_basis_ct = safe_float(cfg.get("direct_marketing_variable_fee_basis_ct_per_kwh"), 0.0)
    dm_service_vat_pct = safe_float(cfg.get("direct_marketing_service_vat_pct"), 19.0)
    dm_input_vat_recoverable = _is_enabled(cfg, "direct_marketing_input_vat_recoverable")
    dm_installed_kwp = safe_float(cfg.get("direct_marketing_installed_kwp"), 0.0)
    dm_balancing_cost_estimate = safe_float(cfg.get("direct_marketing_balancing_cost_eur_per_kwp_month"), 0.0)
    dm_balancing_cost_actual = safe_float(cfg.get("direct_marketing_balancing_cost_actual_eur_per_kwp_month"), 0.0)
    dm_revenue_offset = safe_float(cfg.get("direct_marketing_revenue_offset_ct"), 0.0)
    dm_safety_margin = safe_float(cfg.get("direct_marketing_safety_margin_ct_per_kwh"), 0.0)
    dm_max_export_w = safe_float(cfg.get("direct_marketing_max_export_w"), 0.0)
    dm_min_grid_export_w = safe_float(cfg.get("direct_marketing_min_grid_export_w"), 100.0)
    dm_max_grid_charge_w = safe_float(cfg.get("direct_marketing_max_grid_charge_w"), 0.0)
    dm_pv_store_threshold = safe_float(cfg.get("direct_marketing_pv_store_threshold_ct"), 0.0)
    dm_pv_store_max_w = safe_float(cfg.get("direct_marketing_pv_store_max_w"), 0.0)
    dm_pv_store_min_surplus_w = safe_float(cfg.get("direct_marketing_pv_store_min_surplus_w"), 300.0)
    dm_pv_store_import_guard_w = safe_float(cfg.get("direct_marketing_pv_store_import_guard_w"), 80.0)
    dm_pv_store_min_hold_s = safe_float(cfg.get("direct_marketing_pv_store_min_hold_s"), 600.0)
    dm_pv_store_ramp_step_w = safe_float(cfg.get("direct_marketing_pv_store_ramp_step_w"), 300.0)
    dm_pv_store_legacy_dc_only_veto = _is_enabled(
        cfg,
        "direct_marketing_pv_store_dc_only_enable",
    )
    dm_aux_ac_mode, dm_aux_ac_mode_source = _direct_marketing_aux_ac_mode(cfg)
    dm_aux_ac_storage_requested = dm_aux_ac_mode != "off"
    dm_aux_ac_forecast_confidence_pct = safe_float(
        cfg.get("direct_marketing_aux_inverter_ac_forecast_confidence_pct"),
        80.0,
    )
    dm_aux_ac_deadband_wh = safe_float(
        cfg.get("direct_marketing_aux_inverter_ac_deadband_wh"),
        0.0,
    )
    dm_aux_ac_min_margin_ct = safe_float(
        cfg.get("direct_marketing_aux_inverter_ac_min_margin_ct_per_kwh"),
        dm_min_profit,
    )
    dm_aux_ac_protected_target_raw = (
        safe_float(
            cfg.get("direct_marketing_aux_inverter_ac_protected_target_soc_pct"),
            float("nan"),
        )
        if _has_user_value(cfg, "direct_marketing_aux_inverter_ac_protected_target_soc_pct")
        else None
    )
    dm_aux_ac_grid_import_guard_w = safe_float(
        cfg.get("direct_marketing_aux_inverter_ac_grid_import_guard_w"),
        0.0,
    )
    dm_pv_store_dc_charge_efficiency_pct = safe_float(
        cfg.get("direct_marketing_pv_store_dc_charge_efficiency_pct"),
        96.0,
    )
    dm_pv_store_aux_ac_charge_efficiency_pct = safe_float(
        cfg.get("direct_marketing_pv_store_aux_ac_charge_efficiency_pct"),
        90.0,
    )
    dm_pv_store_discharge_efficiency_pct = safe_float(
        cfg.get("direct_marketing_pv_store_discharge_efficiency_pct"),
        95.0,
    )
    dm_pv_topology_contract = build_pv_forecast_topology(cfg)
    dm_aux_ac_topology_ready = bool(
        dm_pv_topology_contract.get("status") == "bound"
        and dm_pv_topology_contract.get("e3dc_dc_bound")
        and dm_pv_topology_contract.get("external_ac_bound")
    )
    # Die Moduswahl ist nur eine gespeicherte Nutzerabsicht. Der produktive
    # Forecastpfad erzeugt noch keine kalibrierte gemeinsame Horizontverteilung,
    # und für deren Risikoentscheidung ist noch keine Schwelle beschlossen.
    # Deshalb darf die Konfigurationsprojektion weder eine AC-Wirksamkeit noch
    # einen OK-Status behaupten; der Backendvertrag bleibt separat fail-closed.
    dm_aux_ac_storage_enable = False
    dm_pv_store_dc_only = True
    dm_pv_store_external_ac_guard_w = safe_float(cfg.get("direct_marketing_pv_store_external_ac_guard_w"), 100.0)
    dm_pv_store_export_limit_guard_w = safe_float(cfg.get("direct_marketing_pv_store_export_limit_guard_w"), 100.0)
    dm_pv_store_export_limit_ramp_bypass_w = safe_float(cfg.get("direct_marketing_pv_store_export_limit_ramp_bypass_w"), 300.0)
    dm_price_max_age_s = safe_float(cfg.get("direct_marketing_price_max_age_s"), 0.0)
    dm_cycles = safe_float(cfg.get("direct_marketing_max_cycles_per_day"), 1.0)
    dm_headroom = safe_float(cfg.get("direct_marketing_keep_headroom_pct"), 20.0)
    dm_negative_headroom_enabled = _is_enabled(cfg, "direct_marketing_negative_headroom_enable")
    dm_negative_headroom_lookahead_min = safe_float(cfg.get("direct_marketing_negative_headroom_lookahead_min"), 240.0)
    dm_negative_headroom_min_window_min = safe_float(cfg.get("direct_marketing_negative_headroom_min_window_min"), 30.0)
    dm_negative_headroom_min_surplus_wh = safe_float(cfg.get("direct_marketing_negative_headroom_min_surplus_wh"), 1000.0)
    dm_negative_headroom_buffer_pct = safe_float(cfg.get("direct_marketing_negative_headroom_buffer_pct"), 3.0)
    dm_low_price_headroom_enabled = _is_enabled(cfg, "direct_marketing_low_price_headroom_enable")
    dm_curtail_enabled = _is_enabled(cfg, "direct_marketing_low_price_curtail_enable")
    dm_curtail_limit_w = safe_float(cfg.get("direct_marketing_low_price_curtail_limit_w"), 0.0)
    dm_market_value_solar_enabled = _is_enabled(cfg, "direct_marketing_market_value_solar_enable")
    dm_market_value_solar_source = str(cfg.get("direct_marketing_market_value_solar_source", "netztransparenz_hochrechnung_solar") or "netztransparenz_hochrechnung_solar").strip()
    nt_client_id = str(cfg.get("netztransparenz_client_id", "") or "").strip()
    nt_client_secret = str(cfg.get("netztransparenz_client_secret", "") or "").strip()
    dm_aux_shelly_override = aux_inverter_contract.get("override") == "central"
    dm_aux_shelly_ip = str(aux_inverter_contract.get("ip") or "").strip()
    dm_aux_shelly_ip_valid = bool(dm_aux_shelly_ip)
    dm_aux_shelly_invert = bool(aux_inverter_contract.get("invert"))
    dm_aux_shelly_dynamic_unblock = bool(aux_inverter_contract.get("dynamic_unblock_enable"))
    dm_aux_shelly_unblock_threshold_w = safe_float(
        aux_inverter_contract.get("unblock_threshold_w"),
        3000.0,
    )
    dm_aux_migration = aux_inverter_contract.get("migration") or {}
    dm_aux_contract_blocked = bool(aux_inverter_contract.get("commands_blocked"))
    dm_negative_no_export = _is_enabled(cfg, "direct_marketing_negative_price_no_export")
    dm_low_no_export = _is_enabled(cfg, "direct_marketing_low_price_no_export")
    dm_eeg_enabled = _is_enabled(cfg, "direct_marketing_eeg_enable")
    dm_eeg_commissioning = str(cfg.get("direct_marketing_eeg_commissioning_date", "") or "").strip()
    dm_eeg_support_years = safe_float(cfg.get("direct_marketing_eeg_support_years"), 20.0)
    dm_eeg_tiers = str(cfg.get("direct_marketing_eeg_tariff_tiers", "") or "").strip()
    dm_eeg_rate_source = str(cfg.get("direct_marketing_eeg_rate_source", "manual") or "manual").strip()
    dm_eeg_system_type = str(cfg.get("direct_marketing_eeg_system_type", "building") or "building").strip()
    dm_eeg_feed_type = str(cfg.get("direct_marketing_eeg_feed_type", "partial") or "partial").strip()
    dm_eeg_compensation_basis = str(cfg.get("direct_marketing_eeg_compensation_basis", "feed_in_tariff") or "feed_in_tariff").strip()
    dm_eeg_grid_export_ack = _is_enabled(cfg, "direct_marketing_eeg_grid_export_risk_ack")

    price["direct_marketing_enable"] = _entry(
        key="direct_marketing_enable",
        label="Direktvermarktung",
        unit="-",
        configured=cfg.get("direct_marketing_enable") if _has_user_value(cfg, "direct_marketing_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_enable") else "default",
        severity="ok",
        message="Direktvermarktung ist aktiv." if dm_enabled else "Direktvermarktung ist hart aus und beeinflusst die Regelung nicht.",
    )
    price["direct_marketing_mode"] = _entry(
        key="direct_marketing_mode",
        label="Direktvermarktungsmodus",
        unit="-",
        configured=cfg.get("direct_marketing_mode") if _has_user_value(cfg, "direct_marketing_mode") else None,
        live_value=None,
        live_key=None,
        effective=dm_mode,
        source="user" if _has_user_value(cfg, "direct_marketing_mode") else "default",
        severity="warning" if dm_enabled and dm_mode_warning else "ok",
        message="Unbekannter Modus; Safe wird als konservativer Fallback genutzt." if dm_enabled and dm_mode_warning else "Direktvermarktungsmodus ist plausibel.",
    )
    dm_profit_profile_severity = "ok"
    dm_profit_profile_message = "Standardprofil berücksichtigt Wirkungsgrad, LCOS und alle Mindestgates."
    if dm_profit_profile_warning:
        dm_profit_profile_severity = "warning"
        dm_profit_profile_message = "Unbekanntes Profitprofil; Standard wird als wirtschaftlicher Fallback genutzt."
    elif dm_profit_profile == "aggressive":
        dm_profit_profile_severity = "warning" if dm_enabled and dm_export_enabled else "ok"
        dm_profit_profile_message = "Aggressiv erlaubt positive Kleinstgewinne nach Wirkungsgrad und kann zusätzliche Batteriezyklen verursachen."
    elif dm_profit_profile == "expert":
        dm_profit_profile_severity = "warning" if dm_enabled else "ok"
        dm_profit_profile_message = "Experte umgeht Wirtschaftlichkeitsgates; Notstrom-, Reserve- und Datenqualitätsgrenzen bleiben hart."
    price["direct_marketing_profit_profile"] = _entry(
        key="direct_marketing_profit_profile",
        label="DV-Profitprofil",
        unit="-",
        configured=cfg.get("direct_marketing_profit_profile") if _has_user_value(cfg, "direct_marketing_profit_profile") else None,
        live_value=None,
        live_key=None,
        effective=dm_profit_profile,
        source="user" if _has_user_value(cfg, "direct_marketing_profit_profile") else "default",
        severity=dm_profit_profile_severity,
        message=dm_profit_profile_message,
    )
    price["direct_marketing_settlement_basis"] = _entry(
        key="direct_marketing_settlement_basis",
        label="DV-Abrechnungsbasis",
        unit="-",
        configured=(
            cfg.get("direct_marketing_settlement_basis")
            if _has_user_value(cfg, "direct_marketing_settlement_basis")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=dm_settlement_basis,
        source="user" if _has_user_value(cfg, "direct_marketing_settlement_basis") else "default",
        severity="warning" if dm_enabled and not dm_settlement_active_supported else "ok",
        message=(
            "Aktive DV-Regelung ist derzeit nur mit Day-Ahead-Viertelstundenpreisen freigegeben. "
            "Die gewählte Basis bleibt reine Analyse und erzeugt keine Steuerbefehle."
            if dm_enabled and not dm_settlement_active_supported
            else "Day-Ahead-Viertelstundenpreise sind für die aktive DV-Regelung freigegeben."
        ),
    )
    price["direct_marketing_pv_store_enable"] = _entry(
        key="direct_marketing_pv_store_enable",
        label="DV-PV-Speichern",
        unit="-",
        configured=cfg.get("direct_marketing_pv_store_enable") if _has_user_value(cfg, "direct_marketing_pv_store_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_pv_store_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_pv_store_enable") else "default",
        severity="ok",
        message=(
            "Eco+ darf PV-Überschuss in niedrigen Direktvermarktungsfenstern speichern; Netzladen und Batterieeinspeisung bleiben separat gesperrt."
            if dm_pv_store_enabled else
            "PV-Speichern im Direktvermarktungszweig ist aus; es gibt keinen aktiven PV-Ladeowner."
        ),
    )
    price["direct_marketing_eeg_enable"] = _entry(
        key="direct_marketing_eeg_enable",
        label="DV-EEG-Anlage",
        unit="-",
        configured=cfg.get("direct_marketing_eeg_enable") if _has_user_value(cfg, "direct_marketing_eeg_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_enable") else "default",
        severity="ok",
        message="EEG-/Marktprämienbewertung ist aktiv." if dm_eeg_enabled else "EEG-/Marktprämienbewertung ist aus.",
    )
    eeg_rate_sources = {"manual", "bnetza_archive", "bnetza_current_2026_02"}
    eeg_auto_source = dm_eeg_rate_source in {"bnetza_archive", "bnetza_current_2026_02"}
    eeg_source_invalid = dm_eeg_rate_source not in eeg_rate_sources
    eeg_auto_date_outside = bool(
        dm_eeg_rate_source == "bnetza_current_2026_02"
        and re.match(r"^\d{4}-\d{2}-\d{2}$", dm_eeg_commissioning)
        and (dm_eeg_commissioning < "2026-02-01" or dm_eeg_commissioning > "2026-07-31")
    )
    eeg_archive_date_missing = bool(dm_eeg_rate_source == "bnetza_archive" and not dm_eeg_commissioning)
    eeg_archive_date_outside = bool(
        dm_eeg_rate_source == "bnetza_archive"
        and re.match(r"^\d{4}-\d{2}-\d{2}$", dm_eeg_commissioning)
        and (dm_eeg_commissioning < "2018-01-01" or dm_eeg_commissioning > "2026-07-31")
    )
    price["direct_marketing_eeg_rate_source"] = _entry(
        key="direct_marketing_eeg_rate_source",
        label="DV-EEG-Vergütungsquelle",
        unit="-",
        configured=dm_eeg_rate_source if _has_user_value(cfg, "direct_marketing_eeg_rate_source") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_rate_source,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_rate_source") else "default",
        severity="warning" if dm_eeg_enabled and (eeg_source_invalid or eeg_auto_date_outside or eeg_archive_date_missing or eeg_archive_date_outside) else "ok",
        message=(
            "Unbekannte EEG-Vergütungsquelle; bitte manuelle Stufen verwenden."
            if eeg_source_invalid
            else (
                "Die aktuelle BNetzA-Tabelle gilt nur für Inbetriebnahmen vom 2026-02-01 bis 2026-07-31."
                if eeg_auto_date_outside
                else (
                    "BNetzA-Archiv automatisch braucht ein Inbetriebnahmedatum."
                    if eeg_archive_date_missing
                    else (
                        "Eingebettetes BNetzA-Archiv deckt aktuell nur 2018-01-01 bis 2026-07-31 ab."
                        if eeg_archive_date_outside
                        else "EEG-Vergütungsquelle ist plausibel."
                    )
                )
            )
        ),
    )
    price["direct_marketing_eeg_system_type"] = _entry(
        key="direct_marketing_eeg_system_type",
        label="DV-EEG-Anlagenart",
        unit="-",
        configured=dm_eeg_system_type if _has_user_value(cfg, "direct_marketing_eeg_system_type") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_system_type,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_system_type") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_system_type not in {"building", "other"} else "ok",
        message="Anlagenart ist plausibel." if dm_eeg_system_type in {"building", "other"} else "Unbekannte Anlagenart.",
    )
    price["direct_marketing_eeg_feed_type"] = _entry(
        key="direct_marketing_eeg_feed_type",
        label="DV-EEG-Einspeiseart",
        unit="-",
        configured=dm_eeg_feed_type if _has_user_value(cfg, "direct_marketing_eeg_feed_type") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_feed_type,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_feed_type") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_feed_type not in {"partial", "full"} else "ok",
        message="Einspeiseart ist plausibel." if dm_eeg_feed_type in {"partial", "full"} else "Unbekannte Einspeiseart.",
    )
    price["direct_marketing_eeg_compensation_basis"] = _entry(
        key="direct_marketing_eeg_compensation_basis",
        label="DV-EEG-Rechengrundlage",
        unit="-",
        configured=dm_eeg_compensation_basis if _has_user_value(cfg, "direct_marketing_eeg_compensation_basis") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_compensation_basis,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_compensation_basis") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_compensation_basis not in {"feed_in_tariff", "market_premium"} else "ok",
        message=(
            "Rechengrundlage ist plausibel."
            if dm_eeg_compensation_basis in {"feed_in_tariff", "market_premium"}
            else "Unbekannte Rechengrundlage."
        ),
    )
    eeg_date_invalid = bool(dm_eeg_commissioning and not re.match(r"^\d{4}-\d{2}-\d{2}$", dm_eeg_commissioning))
    price["direct_marketing_eeg_commissioning_date"] = _entry(
        key="direct_marketing_eeg_commissioning_date",
        label="DV-EEG-Inbetriebnahme",
        unit="-",
        configured=dm_eeg_commissioning if _has_user_value(cfg, "direct_marketing_eeg_commissioning_date") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_commissioning,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_commissioning_date") else "default",
        severity="warning" if dm_eeg_enabled and eeg_date_invalid else "ok",
        message="Datum bitte als YYYY-MM-DD hinterlegen." if dm_eeg_enabled and eeg_date_invalid else "Inbetriebnahmedatum ist plausibel oder leer.",
    )
    price["direct_marketing_eeg_tariff_tiers"] = _entry(
        key="direct_marketing_eeg_tariff_tiers",
        label="DV-EEG-Vergütungsstufen",
        unit="ct/kWh",
        configured=dm_eeg_tiers if _has_user_value(cfg, "direct_marketing_eeg_tariff_tiers") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_tiers,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_tariff_tiers") else "default",
        severity="warning" if dm_eeg_enabled and not eeg_auto_source and not dm_eeg_tiers else "ok",
        message=(
            "EEG-Anlage ist aktiv, aber es sind keine Vergütungsstufen hinterlegt."
            if dm_eeg_enabled and not eeg_auto_source and not dm_eeg_tiers else "Vergütungsstufen sind hinterlegt, automatisch ableitbar oder EEG ist aus."
        ),
    )
    eeg_grid_export_risk = bool(dm_enabled and dm_eeg_enabled and dm_grid_charge_enabled and dm_export_enabled)
    price["direct_marketing_eeg_grid_export_risk_ack"] = _entry(
        key="direct_marketing_eeg_grid_export_risk_ack",
        label="DV-EEG-Netzstrom-Risiko",
        unit="-",
        configured=cfg.get("direct_marketing_eeg_grid_export_risk_ack") if _has_user_value(cfg, "direct_marketing_eeg_grid_export_risk_ack") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_grid_export_ack,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_grid_export_risk_ack") else "default",
        severity="warning" if eeg_grid_export_risk and not dm_eeg_grid_export_ack else "ok",
        message=(
            "Eine EEG-Anlage mit Netzladen und Einspeisung benötigt ein bestätigtes Mess-/Vertragskonzept; sonst bleiben Arbitrage-Befehle blockiert."
            if eeg_grid_export_risk and not dm_eeg_grid_export_ack
            else "Kein unbestätigter Risikobetrieb mit EEG-Netzstromexport."
        ),
    )

    threshold_contract_warning = bool(
        dm_enabled
        and dm_pv_store_enabled
        and dm_eeg_enabled
        and dm_pv_store_threshold <= 0.0
        and not eeg_auto_source
        and not dm_eeg_tiers
    )
    price["direct_marketing_pv_store_contract_threshold"] = _entry(
        key="direct_marketing_pv_store_contract_threshold",
        label="DV-PV-Speicher-Vertragsschwelle",
        unit="-",
        configured=None,
        live_value=None,
        live_key=None,
        effective=not threshold_contract_warning,
        source="derived",
        severity="warning" if threshold_contract_warning else "ok",
        message=(
            "PV-Speichern nach EEG-/Direktvermarktungsschwelle braucht eine manuelle Schwelle oder gültige Vergütungsstufen; sonst bleibt nur die Preis-Score-Logik."
            if threshold_contract_warning else
            "Schwelle ist manuell, automatisch ableitbar oder PV-Speichern nutzt bewusst die Score-Logik."
        ),
    )
    price["direct_marketing_pv_store_dc_only_enable"] = _entry(
        key="direct_marketing_pv_store_dc_only_enable",
        label="DV-PV-Speichern E3DC-DC-Standard",
        unit="-",
        configured=cfg.get("direct_marketing_pv_store_dc_only_enable") if _has_user_value(cfg, "direct_marketing_pv_store_dc_only_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_pv_store_dc_only,
        source=(
            "evidence_limit"
            if dm_aux_ac_storage_requested
            else "derived"
        ),
        severity="warning" if dm_aux_ac_storage_requested else "ok",
        message=(
            "Wirksam bleibt E3DC_DC_ONLY: Die gewählte Zusatz-AC-Route ist "
            "EVIDENCE_LIMIT, bis eine kalibrierte gemeinsame "
            "Horizontverteilung erzeugt wird und die Risikoschwelle fachlich "
            "beschlossen ist."
            if dm_aux_ac_storage_requested
            else "E3DC-DC ist die einzige freigegebene PV-Speicherquelle."
        ),
    )
    aux_ac_validation_warning = bool(
        dm_aux_ac_mode_source.endswith("_invalid")
        or dm_aux_ac_storage_requested
    )
    configured_aux_ac_mode = (
        cfg.get("direct_marketing_aux_inverter_ac_storage_mode")
        if _has_user_value(cfg, "direct_marketing_aux_inverter_ac_storage_mode")
        else (
            cfg.get("direct_marketing_pv_store_aux_ac_mode")
            if _has_user_value(cfg, "direct_marketing_pv_store_aux_ac_mode")
            else None
        )
    )
    price["direct_marketing_aux_inverter_ac_storage_mode"] = _entry(
        key="direct_marketing_aux_inverter_ac_storage_mode",
        label="DV-Zusatz-WR-AC-Speichermodus",
        unit="-",
        configured=configured_aux_ac_mode,
        live_value=None,
        live_key=None,
        effective="off",
        source=(
            "evidence_limit"
            if dm_aux_ac_storage_requested
            else dm_aux_ac_mode_source
        ),
        severity=(
            "warning"
            if aux_ac_validation_warning
            else "info"
        ),
        message=(
            "Unbekannter AC-Speichermodus; sicherer Standard ist Aus."
            if dm_aux_ac_mode_source.endswith("_invalid")
            else (
                "Die AC-Freigabe wird vom gesetzten Legacy-DC-Veto überstimmt."
                if dm_aux_ac_storage_requested and dm_pv_store_legacy_dc_only_veto
                else (
                    "Auswahl gespeichert, wirksam bleibt E3DC_DC_ONLY: Zusätzlich "
                    "zur vollständigen E3DC-DC-/EXTERNAL-AC-Topologie fehlen der "
                    "Produzent einer historisch kalibrierten gemeinsamen "
                    "Horizontverteilung und die fachlich beschlossene "
                    "Risikoschwelle. Die Route bleibt EVIDENCE_LIMIT."
                    if dm_aux_ac_storage_requested and not dm_aux_ac_topology_ready
                    else (
                        "Auswahl gespeichert, aber nicht wirksam: Ohne Produzent "
                        "einer historisch kalibrierten gemeinsamen "
                        "Horizontverteilung und ohne fachlich beschlossene "
                        "Risikoschwelle bleibt die Zusatz-AC-Route EVIDENCE_LIMIT; "
                        "E3DC_DC_ONLY ist der steuernde Standard."
                        if dm_aux_ac_storage_requested
                        else "Aus: Zusatz-WR-AC ist keine Speicherquelle; E3DC-DC bleibt Standard."
                    )
                )
            )
        ),
    )
    price["direct_marketing_aux_inverter_ac_storage_enable"] = _entry(
        key="direct_marketing_aux_inverter_ac_storage_enable",
        label="DV-Zusatz-WR-AC Altfreigabe",
        unit="-",
        configured=(
            cfg.get("direct_marketing_aux_inverter_ac_storage_enable")
            if _has_user_value(cfg, "direct_marketing_aux_inverter_ac_storage_enable")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=dm_aux_ac_storage_enable,
        source=(
            "evidence_limit"
            if dm_aux_ac_storage_requested
            else dm_aux_ac_mode_source
        ),
        severity=(
            "warning"
            if aux_ac_validation_warning
            else "info"
        ),
        message=(
            "Explizites Legacy-Ein wird als gespeicherte Auswahl gelesen, bleibt "
            "aber bis zum gemeinsamen Horizontnachweis und zur beschlossenen "
            "Risikoschwelle EVIDENCE_LIMIT."
            if dm_aux_ac_mode_source == "legacy_bool_explicit_true"
            else (
                "Die Legacy-Freigabe wird vom gesetzten DC-Veto überstimmt."
                if dm_aux_ac_storage_requested and dm_pv_store_legacy_dc_only_veto
                else (
                    "Nur Kompatibilitätsanzeige; die kanonische Moduswahl ist "
                    "gespeichert, erteilt derzeit aber keine AC-Freigabe."
                    if dm_aux_ac_storage_requested
                    else "Nur Kompatibilitätsanzeige; Zusatz-AC ist aus."
                )
            )
        ),
    )
    market_value_source_ok = dm_market_value_solar_source in {"netztransparenz_hochrechnung_solar"}
    price["direct_marketing_market_value_solar_enable"] = _entry(
        key="direct_marketing_market_value_solar_enable",
        label="DV-Marktwert Solar Monitor",
        unit="-",
        configured=cfg.get("direct_marketing_market_value_solar_enable") if _has_user_value(cfg, "direct_marketing_market_value_solar_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_market_value_solar_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_market_value_solar_enable") else "default",
        severity="warning" if dm_market_value_solar_enabled and (not nt_client_id or not nt_client_secret or not market_value_source_ok) else "ok",
        message=(
            "Marktwert-Solar-Monitor ist aktiv, aber Netztransparenz-Zugangsdaten oder Quelle sind unvollständig."
            if dm_market_value_solar_enabled and (not nt_client_id or not nt_client_secret or not market_value_source_ok)
            else (
                "Marktwert-Solar-Monitor ist aktiv und bleibt read-only ohne Regelwirkung."
                if dm_market_value_solar_enabled else
                "Marktwert-Solar-Monitor ist aus."
            )
        ),
    )
    price["direct_marketing_market_value_solar_source"] = _entry(
        key="direct_marketing_market_value_solar_source",
        label="DV-Marktwert Solar Quelle",
        unit="-",
        configured=dm_market_value_solar_source if _has_user_value(cfg, "direct_marketing_market_value_solar_source") else None,
        live_value=None,
        live_key=None,
        effective=dm_market_value_solar_source,
        source="user" if _has_user_value(cfg, "direct_marketing_market_value_solar_source") else "default",
        severity="warning" if dm_market_value_solar_enabled and not market_value_source_ok else "ok",
        message="Quelle ist plausibel." if market_value_source_ok else "Unbekannte Marktwert-Solar-Quelle.",
    )
    price["netztransparenz_client_id"] = _entry(
        key="netztransparenz_client_id",
        label="Netztransparenz Client-ID",
        unit="-",
        configured="gesetzt" if _has_user_value(cfg, "netztransparenz_client_id") and nt_client_id else None,
        live_value=None,
        live_key=None,
        effective=bool(nt_client_id),
        source="user" if _has_user_value(cfg, "netztransparenz_client_id") else "default",
        severity="warning" if dm_market_value_solar_enabled and not nt_client_id else "ok",
        message="Client-ID ist gesetzt." if nt_client_id else "Client-ID fehlt für den Marktwert-Solar-Monitor.",
    )
    price["netztransparenz_client_secret"] = _entry(
        key="netztransparenz_client_secret",
        label="Netztransparenz Client-Secret",
        unit="-",
        configured="***" if _has_user_value(cfg, "netztransparenz_client_secret") and nt_client_secret else None,
        live_value=None,
        live_key=None,
        effective=bool(nt_client_secret),
        source="user" if _has_user_value(cfg, "netztransparenz_client_secret") else "default",
        severity="warning" if dm_market_value_solar_enabled and not nt_client_secret else "ok",
        message="Client-Secret ist gesetzt und wird nicht ausgegeben." if nt_client_secret else "Client-Secret fehlt für den Marktwert-Solar-Monitor.",
    )
    price["direct_marketing_aux_inverter_shelly_contract_status"] = _entry(
        key="direct_marketing_aux_inverter_shelly_contract_status",
        label="DV-Zusatz-WR Konfigurationsvertrag",
        unit="-",
        configured=None,
        live_value=None,
        live_key=None,
        effective="blocked" if dm_aux_contract_blocked else "ok",
        source=str(dm_aux_migration.get("source") or "safe_defaults"),
        severity="warning" if dm_aux_contract_blocked else "ok",
        message=(
            "Widersprüchliche oder unvollständige Zusatz-WR-Konfiguration: Zentralsteuerung bleibt sicher deaktiviert."
            if dm_aux_contract_blocked
            else "Kanonischer Zusatz-WR-Vertrag ist vollständig und konfliktfrei."
        ),
    )
    price["direct_marketing_aux_inverter_shelly_override"] = _entry(
        key="direct_marketing_aux_inverter_shelly_override",
        label="DV-Zusatz-WR Shelly-Steuerung",
        unit="-",
        configured=str(aux_inverter_contract.get("override") or "local"),
        live_value=None,
        live_key=None,
        effective="central" if dm_aux_shelly_override else "local",
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_contract_blocked else "ok",
        message=(
            "Konfigurationskonflikt; lokales Shelly-Skript bleibt als sicherer Fallback maßgeblich."
            if dm_aux_contract_blocked
            else (
                "E3DC-Control übernimmt die Shelly-Relaissteuerung für den ungeregelten Zusatzwechselrichter."
                if dm_aux_shelly_override
                else "Shelly-Zusatzwechselrichter bleibt lokal/fallback-gesteuert; E3DC-Control sendet keine Relaisbefehle."
            )
        ),
    )
    price["direct_marketing_aux_inverter_shelly_ip"] = _entry(
        key="direct_marketing_aux_inverter_shelly_ip",
        label="DV-Zusatz-WR Shelly-IP",
        unit="-",
        configured="konfiguriert" if dm_aux_shelly_ip else None,
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_shelly_ip),
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_override and not dm_aux_shelly_ip_valid else "ok",
        message=(
            "Zentrale Shelly-Steuerung ist aktiv, aber es ist keine gültige IPv4-Adresse hinterlegt."
            if dm_aux_shelly_override and not dm_aux_shelly_ip_valid
            else (
                "Shelly-IP ist plausibel."
                if dm_aux_shelly_ip
                else "Keine Shelly-IP hinterlegt; ohne zentrale Freigabe ist das in Ordnung."
            )
        ),
    )
    price["direct_marketing_aux_inverter_shelly_invert"] = _entry(
        key="direct_marketing_aux_inverter_shelly_invert",
        label="DV-Zusatz-WR Shelly-Schütz invertiert",
        unit="-",
        configured=bool(dm_aux_shelly_invert),
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_shelly_invert),
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_override and dm_aux_shelly_invert else "ok",
        message=(
            "Invertiert aktiv: Shelly-Relais EIN bedeutet Zusatzwechselrichter AUS (NC-Schütz/stromlos geschlossen)."
            if dm_aux_shelly_invert
            else "Normale Logik: Shelly-Relais EIN bedeutet Zusatzwechselrichter EIN."
        ),
    )
    price["direct_marketing_aux_inverter_shelly_dynamic_unblock_enable"] = _entry(
        key="direct_marketing_aux_inverter_shelly_dynamic_unblock_enable",
        label="DV-Zusatz-WR dynamische Last-Freigabe",
        unit="-",
        configured=bool(dm_aux_shelly_dynamic_unblock),
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_shelly_dynamic_unblock),
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_override and dm_aux_shelly_dynamic_unblock else "ok",
        message=(
            "Dynamische Freigabe ist bewusst aktiviert. Sie nutzt gültige Netz-/Lastwerte und eine 600-s-Schaltsperre; der gewählte Lastwert garantiert allein keinen exportfreien Betrieb."
            if dm_aux_shelly_dynamic_unblock
            else "Sicherer Standard: Bei Negativpreis bleibt der Zusatzwechselrichter statisch gesperrt."
        ),
    )
    price["direct_marketing_aux_inverter_shelly_unblock_threshold_w"] = _entry(
        key="direct_marketing_aux_inverter_shelly_unblock_threshold_w",
        label="DV-Zusatz-WR Last-Freigabeschwelle",
        unit="W",
        configured=dm_aux_shelly_unblock_threshold_w,
        live_value=None,
        live_key=None,
        effective=dm_aux_shelly_unblock_threshold_w,
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_dynamic_unblock and not (100.0 <= dm_aux_shelly_unblock_threshold_w <= 100000.0) else "ok",
        message=(
            "Last-Freigabeschwelle muss zwischen 100 W und 100.000 W liegen; wirksam werden mindestens 100 W angesetzt."
            if dm_aux_shelly_dynamic_unblock and not (100.0 <= dm_aux_shelly_unblock_threshold_w <= 100000.0)
            else "Last-Freigabeschwelle ist plausibel; sie muss zur Leistung des Zusatzwechselrichters und der schaltbaren Last passen."
        ),
    )

    variable_fee_bases = {"sell_revenue", "eeg_compensation", "manual"}
    variable_fee_basis_known = dm_variable_fee_basis in variable_fee_bases
    variable_fee_basis_ready = bool(
        dm_variable_fee_basis == "sell_revenue"
        or (dm_variable_fee_basis == "manual" and dm_variable_fee_basis_ct > 0.0)
        or (
            dm_variable_fee_basis == "eeg_compensation"
            and dm_eeg_enabled
            and bool(dm_eeg_tiers)
        )
    )
    price["direct_marketing_variable_fee_basis"] = _entry(
        key="direct_marketing_variable_fee_basis",
        label="DV-Basis variable Gebühr",
        unit="-",
        configured=dm_variable_fee_basis if _has_user_value(cfg, "direct_marketing_variable_fee_basis") else None,
        live_value=None,
        live_key=None,
        effective=dm_variable_fee_basis,
        source="user" if _has_user_value(cfg, "direct_marketing_variable_fee_basis") else "default",
        severity="warning" if dm_fee_pct > 0.0 and (not variable_fee_basis_known or not variable_fee_basis_ready) else "ok",
        message=(
            "Die variable Gebühr ist aktiv, aber ihre Abrechnungsbasis ist unbekannt oder unvollständig."
            if dm_fee_pct > 0.0 and (not variable_fee_basis_known or not variable_fee_basis_ready)
            else "Die variable Gebühr wird auf die konfigurierte Abrechnungsbasis gerechnet."
        ),
    )
    price["direct_marketing_input_vat_recoverable"] = _entry(
        key="direct_marketing_input_vat_recoverable",
        label="DV-Vorsteuerabzug",
        unit="-",
        configured=cfg.get("direct_marketing_input_vat_recoverable") if _has_user_value(cfg, "direct_marketing_input_vat_recoverable") else None,
        live_value=None,
        live_key=None,
        effective=dm_input_vat_recoverable,
        source="user" if _has_user_value(cfg, "direct_marketing_input_vat_recoverable") else "default",
        severity="warning" if dm_enabled and (dm_fee_pct > 0.0 or dm_monthly_fee > 0.0 or dm_balancing_cost_estimate > 0.0) and not _has_user_value(cfg, "direct_marketing_input_vat_recoverable") else "ok",
        message=(
            "Bitte steuerlich klären, ob die Umsatzsteuer auf Vermarktungskosten als Vorsteuer abziehbar ist."
            if dm_enabled and (dm_fee_pct > 0.0 or dm_monthly_fee > 0.0 or dm_balancing_cost_estimate > 0.0) and not _has_user_value(cfg, "direct_marketing_input_vat_recoverable")
            else ("Vorsteuer wird kostenmindernd berücksichtigt." if dm_input_vat_recoverable else "Umsatzsteuer wird als Kostenbestandteil berücksichtigt.")
        ),
    )

    protected_target_valid = bool(
        dm_aux_ac_protected_target_raw is None
        or (
            math.isfinite(dm_aux_ac_protected_target_raw)
            and 0.0 <= dm_aux_ac_protected_target_raw <= 100.0
        )
    )
    price["direct_marketing_aux_inverter_ac_protected_target_soc_pct"] = _entry(
        key="direct_marketing_aux_inverter_ac_protected_target_soc_pct",
        label="DV-AC geschütztes SoC-Ziel",
        unit="%",
        configured=(
            cfg.get("direct_marketing_aux_inverter_ac_protected_target_soc_pct")
            if _has_user_value(cfg, "direct_marketing_aux_inverter_ac_protected_target_soc_pct")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=(
            round(dm_aux_ac_protected_target_raw, 2)
            if dm_aux_ac_protected_target_raw is not None
            and math.isfinite(dm_aux_ac_protected_target_raw)
            else None
        ),
        source=(
            "user"
            if dm_aux_ac_protected_target_raw is not None
            else "reserve_contract_auto"
        ),
        severity="ok" if protected_target_valid else "warning",
        message=(
            "Leer bedeutet automatisch: wirksame Notstrom-/Haus-/Nachtreserve."
            if dm_aux_ac_protected_target_raw is None
            else (
                "Geschütztes SoC-Ziel ist plausibel."
                if protected_target_valid
                else "Geschütztes SoC-Ziel muss zwischen 0 und 100 Prozent liegen."
            )
        ),
    )
    dm_numeric_checks = (
        ("direct_marketing_fee_ct_per_kwh", "DV-Gebühr fix", dm_fee_ct, "ct/kWh", 0.0, None),
        ("direct_marketing_fee_pct", "DV-Gebühr variabel", dm_fee_pct, "%", 0.0, 100.0),
        ("direct_marketing_monthly_fee_eur", "DV-Grundgebühr", dm_monthly_fee, "EUR/Monat", 0.0, None),
        ("direct_marketing_variable_fee_basis_ct_per_kwh", "DV-Manuelle Gebührenbasis", dm_variable_fee_basis_ct, "ct/kWh", 0.0, None),
        ("direct_marketing_service_vat_pct", "DV-Umsatzsteuer Dienstleistung", dm_service_vat_pct, "%", 0.0, 100.0),
        ("direct_marketing_installed_kwp", "DV-Abrechnungsleistung", dm_installed_kwp, "kWp", 0.0, None),
        ("direct_marketing_balancing_cost_eur_per_kwp_month", "DV-Ausgleichskosten Abschlag", dm_balancing_cost_estimate, "EUR/kWp/Monat", 0.0, None),
        ("direct_marketing_balancing_cost_actual_eur_per_kwp_month", "DV-Ausgleichskosten tatsächlich", dm_balancing_cost_actual, "EUR/kWp/Monat", 0.0, None),
        ("direct_marketing_revenue_offset_ct", "DV-Erlös-Korrektur", dm_revenue_offset, "ct/kWh", None, None),
        ("direct_marketing_min_margin_pct", "DV-Mindestmarge", dm_min_margin, "%", 0.0, None),
        ("direct_marketing_min_profit_ct_per_kwh", "DV-Mindestgewinn", dm_min_profit, "ct/kWh", 0.0, None),
        ("direct_marketing_min_window_profit_eur", "DV-Mindestfenstergewinn", dm_min_window_profit_eur, "EUR", 0.0, None),
        ("direct_marketing_min_export_energy_kwh", "DV-Mindestexportenergie", dm_min_export_energy_kwh, "kWh", 0.0, None),
        ("direct_marketing_min_export_window_min", "DV-Mindestexportdauer", dm_min_export_window_min, "min", 15.0, 1440.0),
        ("direct_marketing_preferred_export_plateau_min", "DV-Bevorzugte Plateaudauer", dm_preferred_export_plateau_min, "min", 15.0, 1440.0),
        ("direct_marketing_price_plateau_tolerance_ct", "DV-Preisplateau-Toleranz", dm_price_plateau_tolerance_ct, "ct/kWh", 0.0, 20.0),
        ("direct_marketing_max_daily_export_kwh", "DV-Tagesexportlimit", dm_max_daily_export_kwh, "kWh/Tag", 0.0, None),
        ("direct_marketing_deep_cycle_threshold_pct", "DV-Tiefzyklus-Schwelle", dm_deep_cycle_threshold_pct, "%", 0.0, 100.0),
        ("direct_marketing_deep_cycle_lcos_factor", "DV-Tiefzyklus-LCOS-Faktor", dm_deep_cycle_lcos_factor, "Faktor", 0.0, 5.0),
        ("direct_marketing_profit_hold_ct_per_kwh", "DV-Gewinn-Halteband", dm_profit_hold, "ct/kWh", 0.0, None),
        ("direct_marketing_margin_hold_pct", "DV-Margen-Halteband", dm_margin_hold, "%", 0.0, 50.0),
        ("direct_marketing_degradation_ct_per_kwh", "DV-Degeneration", dm_degradation, "ct/kWh", 0.0, None),
        ("direct_marketing_roundtrip_efficiency_pct", "DV-Wirkungsgrad", dm_efficiency, "%", 50.0, 100.0),
        ("direct_marketing_safety_margin_ct_per_kwh", "DV-Sicherheitskorrektur", dm_safety_margin, "ct/kWh", -10.0, 50.0),
        ("direct_marketing_max_export_w", "DV-Basis-Entladung", dm_max_export_w, "W", 0.0, None),
        ("direct_marketing_min_grid_export_w", "DV-Mindest-Netzexport", dm_min_grid_export_w, "W", 0.0, None),
        ("direct_marketing_max_grid_charge_w", "DV-Max-Netzladen", dm_max_grid_charge_w, "W", 0.0, None),
        ("direct_marketing_pv_store_threshold_ct", "DV-PV-Speicher-Schwelle", dm_pv_store_threshold, "ct/kWh", -50.0, 200.0),
        ("direct_marketing_pv_store_max_w", "DV-Max-PV-Speichern", dm_pv_store_max_w, "W", 0.0, None),
        ("direct_marketing_pv_store_min_surplus_w", "DV-PV-Mindestüberschuss", dm_pv_store_min_surplus_w, "W", 0.0, None),
        ("direct_marketing_pv_store_import_guard_w", "DV-PV-Importwächter", dm_pv_store_import_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_min_hold_s", "DV-PV-Mindesthaltezeit", dm_pv_store_min_hold_s, "s", 0.0, 3600.0),
        ("direct_marketing_pv_store_ramp_step_w", "DV-PV-Laderampe", dm_pv_store_ramp_step_w, "W/Zyklus", 100.0, 5000.0),
        ("direct_marketing_aux_inverter_ac_deadband_wh", "DV-AC-Energietotband", dm_aux_ac_deadband_wh, "Wh", 0.0, None),
        ("direct_marketing_aux_inverter_ac_min_margin_ct_per_kwh", "DV-AC-Mindestmarge", dm_aux_ac_min_margin_ct, "ct/kWh", 0.0, None),
        ("direct_marketing_aux_inverter_ac_grid_import_guard_w", "DV-AC-Netzbezugsgrenze", dm_aux_ac_grid_import_guard_w, "W", 0.0, 300.0),
        ("direct_marketing_pv_store_dc_charge_efficiency_pct", "DV-DC-Ladewirkungsgrad", dm_pv_store_dc_charge_efficiency_pct, "%", 50.0, 100.0),
        ("direct_marketing_pv_store_aux_ac_charge_efficiency_pct", "DV-AC-Ladewirkungsgrad", dm_pv_store_aux_ac_charge_efficiency_pct, "%", 50.0, 100.0),
        ("direct_marketing_pv_store_discharge_efficiency_pct", "DV-Entladewirkungsgrad", dm_pv_store_discharge_efficiency_pct, "%", 50.0, 100.0),
        ("direct_marketing_pv_store_external_ac_guard_w", "DV-PV-AC-Zusatz-Wächter", dm_pv_store_external_ac_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_export_limit_guard_w", "DV-PV-Exportlimit-Toleranz", dm_pv_store_export_limit_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_export_limit_ramp_bypass_w", "DV-PV-Exportlimit-Rampenbypass", dm_pv_store_export_limit_ramp_bypass_w, "W", 0.0, 5000.0),
        ("direct_marketing_price_max_age_s", "DV-Preis-Maxalter", dm_price_max_age_s, "s", 0.0, None),
        ("direct_marketing_max_cycles_per_day", "DV-Zyklenlimit", dm_cycles, "Zyklen/Tag", 0.0, 3.0),
        ("direct_marketing_keep_headroom_pct", "DV-Headroom", dm_headroom, "%", 0.0, 100.0),
        ("direct_marketing_negative_headroom_lookahead_min", "DV-Preisfenster-Headroom-Vorlauf", dm_negative_headroom_lookahead_min, "min", 0.0, 1440.0),
        ("direct_marketing_negative_headroom_min_window_min", "DV-Preisfenster-Mindestfenster", dm_negative_headroom_min_window_min, "min", 0.0, 720.0),
        ("direct_marketing_negative_headroom_min_surplus_wh", "DV-Preisfenster-Mindestüberschuss", dm_negative_headroom_min_surplus_wh, "Wh", 0.0, None),
        ("direct_marketing_negative_headroom_buffer_pct", "DV-Preisfenster-Headroom-Puffer", dm_negative_headroom_buffer_pct, "%", 0.0, 50.0),
        ("direct_marketing_low_price_curtail_limit_w", "DV-Billigpreis-Restexport", dm_curtail_limit_w, "W", 0.0, None),
        ("direct_marketing_eeg_support_years", "DV-EEG-Förderjahre", dm_eeg_support_years, "Jahre", 0.0, 30.0),
    )
    for key, label, value, unit, minimum, maximum in dm_numeric_checks:
        severity = "ok"
        message = "Wert ist plausibel."
        check_active = dm_eeg_enabled if key == "direct_marketing_eeg_support_years" else dm_enabled
        if check_active:
            if minimum is not None and value < minimum:
                severity = "warning"
                message = "Wert darf nicht negativ oder unter der Mindestgrenze liegen."
            elif maximum is not None and value > maximum:
                severity = "warning"
                message = "Der Wert liegt über der plausiblen Obergrenze."
            elif key == "direct_marketing_safety_margin_ct_per_kwh" and value < 0.0:
                severity = "warning"
                message = "Negative Korrektur macht die Regelung aggressiver; Mindestmarge bleibt als Schutz aktiv."
            elif key == "direct_marketing_min_margin_pct" and value < 10.0:
                severity = "warning"
                message = "Mindestmarge unter 10 Prozent; Wirtschaftlichkeit wird zu aggressiv."
            elif key == "direct_marketing_max_export_w" and dm_export_enabled and value <= 0.0:
                severity = "warning"
                message = "Export ist freigegeben, aber die Basis-Entladung steht auf 0 W."
            elif key == "direct_marketing_max_grid_charge_w" and dm_grid_charge_enabled and value <= 0.0:
                severity = "warning"
                message = "Netzladen ist freigegeben, aber die maximale Netzladeleistung steht auf 0 W."
            elif key == "direct_marketing_pv_store_min_surplus_w" and dm_pv_store_enabled and value < 300.0:
                severity = "warning"
                message = "PV-Speichern unter 300 W kann takten und Messrauschen folgen."
            elif key == "direct_marketing_pv_store_import_guard_w" and dm_pv_store_enabled and value > 300.0:
                severity = "warning"
                message = "Hohe Importwächter-Grenze kann Netzbezug beim PV-Speichern zulassen."
            elif key == "direct_marketing_pv_store_max_w" and dm_pv_store_enabled and value <= 0.0:
                message = "0 W bedeutet Automatik: der Storage Manager nutzt das System-Ladelimit und den realen PV-Überschuss."
            elif key == "direct_marketing_pv_store_min_hold_s" and dm_pv_store_enabled and value < 300.0:
                severity = "warning"
                message = "Mindesthaltezeit unter 5 Minuten kann PV-Speichern nervös takten lassen."
            elif key == "direct_marketing_pv_store_ramp_step_w" and dm_pv_store_enabled and value > 3000.0:
                severity = "warning"
                message = "Sehr große Laderampen können die DV-PV-Ladeleistung trotz Preisfenster sprunghaft machen."
            elif key == "direct_marketing_aux_inverter_ac_deadband_wh" and value == 0.0:
                message = "0 bedeutet Automatik: mindestens 500 Wh, 2 Prozent Kapazität und eine Mindestleistungs-Slotenergie."
            elif key == "direct_marketing_aux_inverter_ac_min_margin_ct_per_kwh" and value == 0.0:
                message = "0 bedeutet: nur strikt positive Routenmarge; eine höhere Nutzergrenze bleibt zusätzlich bindend."
            elif key == "direct_marketing_aux_inverter_ac_grid_import_guard_w" and value > 0.0:
                severity = "warning"
                message = "Eine positive Grenze toleriert messbaren Netzbezug; 0 W hält Zusatz-WR-AC strikt PV-only."
            elif (
                key == "direct_marketing_pv_store_aux_ac_charge_efficiency_pct"
                and value >= dm_pv_store_dc_charge_efficiency_pct
            ):
                severity = "warning"
                message = "Der AC-Ladewirkungsgrad sollte technisch unter dem direkten DC-Ladewirkungsgrad liegen."
            elif key == "direct_marketing_pv_store_export_limit_guard_w" and dm_pv_store_enabled and value > 500.0:
                severity = "warning"
                message = "Hohe Exportlimit-Toleranz kann bei Negativpreisfenstern unnötigen Restexport zulassen."
            elif key == "direct_marketing_pv_store_export_limit_ramp_bypass_w" and dm_pv_store_enabled and value > 1000.0:
                severity = "warning"
                message = "Hoher Rampenbypass reagiert spät auf Exportlimit-0-Fenster."
            elif key == "direct_marketing_negative_headroom_lookahead_min" and dm_negative_headroom_enabled and value < 30.0:
                severity = "warning"
                message = "Sehr kurzer Vorlauf kann die Ladekurve vor Negativpreisfenstern zu spät beruhigen."
            elif key == "direct_marketing_negative_headroom_min_window_min" and dm_negative_headroom_enabled and value < 15.0:
                severity = "warning"
                message = "Sehr kurze Negativpreisfenster sind oft Prognose- und Rampenrauschen; mindestens 15 Minuten sind empfohlen."
            elif key == "direct_marketing_negative_headroom_min_surplus_wh" and dm_negative_headroom_enabled and value < 300.0:
                severity = "warning"
                message = "Sehr kleiner Mindestüberschuss kann Headroom-Holds aus Messrauschen erzeugen."
            elif key == "direct_marketing_price_max_age_s" and dm_enabled and value == 0.0:
                message = "0 bedeutet: Preisalter wird nicht pauschal begrenzt; explizite Stale-Flags blockieren weiterhin aktiv."
        price[key] = _entry(
            key=key,
            label=label,
            unit=unit,
            configured=value if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity=severity,
            message=message,
        )

    dm_point_confidence_valid = bool(
        0.0 <= dm_aux_ac_forecast_confidence_pct <= 100.0
    )
    price["direct_marketing_aux_inverter_ac_forecast_confidence_pct"] = _entry(
        key="direct_marketing_aux_inverter_ac_forecast_confidence_pct",
        label="DV-AC-Punktprognosewert (nur Diagnose)",
        unit="%",
        configured=(
            dm_aux_ac_forecast_confidence_pct
            if _has_user_value(
                cfg,
                "direct_marketing_aux_inverter_ac_forecast_confidence_pct",
            )
            else None
        ),
        live_value=None,
        live_key=None,
        effective=None,
        source="diagnostic_only",
        severity=(
            "warning"
            if dm_aux_ac_storage_requested or not dm_point_confidence_valid
            else "info"
        ),
        message=(
            "Der gespeicherte Punktwert ist ungültig, besitzt aber ohnehin "
            "keine Steuerwirkung."
            if not dm_point_confidence_valid
            else (
                "Nur Diagnose, keine Steuerwirkung: Der gespeicherte Punktwert "
                "ist weder ein kalibriertes Quantil noch die Risikoschwelle für "
                "eine Zusatz-AC-Freigabe. Die Route bleibt EVIDENCE_LIMIT."
                if dm_aux_ac_storage_requested
                else (
                    "Nur Diagnose, keine Steuerwirkung: Der gespeicherte "
                    "Punktwert ist weder ein kalibriertes Quantil noch eine "
                    "Risikoschwelle."
                )
            )
        ),
    )

    price["direct_marketing_arbitrage_enable"] = _entry(
        key="direct_marketing_arbitrage_enable",
        label="DV-Arbitrage",
        unit="-",
        configured=(
            cfg.get("direct_marketing_arbitrage_enable")
            if _has_user_value(cfg, "direct_marketing_arbitrage_enable")
            else cfg.get("direct_marketing_arbitrage_experimental_enable")
            if _has_user_value(cfg, "direct_marketing_arbitrage_experimental_enable")
            else None
        ),
        live_value=None,
        live_key=None,
        effective=False,
        source="user" if dm_arbitrage_requested else "default",
        severity="warning" if dm_arbitrage_requested else "ok",
        message=(
            "Arbitrage ist derzeit nicht freigegeben; vorhandene Altwerte bleiben erhalten, aber wirkungslos."
            if dm_arbitrage_requested
            else "Arbitrage ist derzeit nicht freigegeben."
        ),
    )
    price["direct_marketing_export_enable"] = _entry(
        key="direct_marketing_export_enable",
        label="DV-Batterieeinspeisung",
        unit="-",
        configured=cfg.get("direct_marketing_export_enable") if _has_user_value(cfg, "direct_marketing_export_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_export_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_export_enable") else "default",
        severity="ok",
        message="Batterieeinspeisung ist separat freigegeben." if dm_export_enabled else "Batterieeinspeisung ist gesperrt.",
    )
    price["direct_marketing_grid_charge_enable"] = _entry(
        key="direct_marketing_grid_charge_enable",
        label="DV-Netzladen",
        unit="-",
        configured=cfg.get("direct_marketing_grid_charge_enable") if _has_user_value(cfg, "direct_marketing_grid_charge_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_grid_charge_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_grid_charge_enable") else "default",
        severity="ok",
        message="Netzladen ist separat freigegeben." if dm_grid_charge_enabled else "Netzladen ist gesperrt.",
    )
    for key, label, value in (
        ("direct_marketing_negative_price_no_export", "DV-kein Verkauf bei Negativpreis", dm_negative_no_export),
        ("direct_marketing_negative_headroom_enable", "DV-Headroom vor Preisfenstern", dm_negative_headroom_enabled),
        ("direct_marketing_low_price_headroom_enable", "DV-Headroom vor Billigpreis", dm_low_price_headroom_enabled),
        ("direct_marketing_low_price_no_export", "DV-kein Verkauf bei Billigpreis", dm_low_no_export),
    ):
        price[key] = _entry(
            key=key,
            label=label,
            unit="-",
            configured=cfg.get(key) if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity="warning" if dm_enabled and not value else "ok",
            message="Schutz ist aktiv." if value else "Schutz ist aus; ein Verkauf in ungünstigen Preisfenstern wäre später möglich.",
        )
    price["direct_marketing_low_price_curtail_enable"] = _entry(
        key="direct_marketing_low_price_curtail_enable",
        label="DV-Negativpreis-Einspeisebegrenzung",
        unit="-",
        configured=cfg.get("direct_marketing_low_price_curtail_enable") if _has_user_value(cfg, "direct_marketing_low_price_curtail_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_curtail_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_low_price_curtail_enable") else "default",
        severity="ok",
        message=(
            "Harte Gesamt-Einspeisebegrenzung ist ausschließlich für Negativpreise freigegeben; positive EEG-/Billigpreisfenster bleiben weich."
            if dm_curtail_enabled else "Zusätzliche harte Negativpreis-Einspeisebegrenzung ist aus."
        ),
    )

    # Bluelink-Zugangsdaten – Benutzer und Passwort sind Pflicht, sobald Bluelink konfiguriert ist
    # (Benutzer, Passwort, PIN oder VIN gesetzt); PIN optional, nur Ziffern; Marke hyundai|kia (Standard hyundai).
    # Geheimnisse erscheinen nie als Wert in der Prüfung.
    bluelink_user_set = _has_user_value(cfg, "bluelink_user")
    bluelink_password_set = _has_user_value(cfg, "bluelink_password")
    bluelink_pin_raw = str(cfg.get("bluelink_pin") or "").strip()
    bluelink_brand_raw = str(cfg.get("bluelink_brand") or "").strip().lower()
    bluelink_active = bool(
        bluelink_user_set or bluelink_password_set or bluelink_pin_raw or _has_user_value(cfg, "bluelink_vin")
    )
    wallbox["bluelink_user"] = _entry(
        key="bluelink_user",
        label="Bluelink Benutzer",
        unit="-",
        configured="gesetzt" if bluelink_user_set else None,
        live_value=None,
        live_key=None,
        effective="gesetzt" if bluelink_user_set else "leer",
        source="user" if bluelink_user_set else "default",
        severity="warning" if (bluelink_active and not bluelink_user_set) else ("ok" if bluelink_user_set else "info"),
        message=(
            "Bluelink: Benutzer (E-Mail des Hyundai/Kia-Kontos) fehlt; der Fahrzeugdienst bleibt ohne Zugangsdaten deaktiviert."
            if (bluelink_active and not bluelink_user_set)
            else ("Benutzer ist gesetzt." if bluelink_user_set else "Bluelink ist nicht konfiguriert.")
        ),
    )
    wallbox["bluelink_password"] = _entry(
        key="bluelink_password",
        label="Bluelink Passwort",
        unit="-",
        configured="gesetzt" if bluelink_password_set else None,
        live_value=None,
        live_key=None,
        effective="gesetzt" if bluelink_password_set else "leer",
        source="user" if bluelink_password_set else "default",
        severity="warning" if (bluelink_active and not bluelink_password_set) else ("ok" if bluelink_password_set else "info"),
        message=(
            "Bluelink: Passwort fehlt; die Anmeldung am Hyundai/Kia-Konto ist nicht möglich."
            if (bluelink_active and not bluelink_password_set)
            else ("Passwort ist gesetzt." if bluelink_password_set else "Bluelink ist nicht konfiguriert.")
        ),
    )
    bluelink_pin_valid = bluelink_pin_raw == "" or bluelink_pin_raw.isdigit()
    wallbox["bluelink_pin"] = _entry(
        key="bluelink_pin",
        label="Bluelink PIN",
        unit="-",
        configured="gesetzt" if bluelink_pin_raw else None,
        live_value=None,
        live_key=None,
        effective="gesetzt" if (bluelink_pin_raw and bluelink_pin_valid) else "leer",
        source="user" if bluelink_pin_raw else "default",
        severity="warning" if not bluelink_pin_valid else "ok",
        message=(
            "Bluelink: Die PIN darf nur Ziffern enthalten; sie wird ignoriert."
            if not bluelink_pin_valid
            else ("PIN ist gesetzt." if bluelink_pin_raw else "PIN ist optional (nur für Konten mit PIN-Pflicht).")
        ),
    )
    bluelink_brand_valid = bluelink_brand_raw in {"", "hyundai", "kia"}
    wallbox["bluelink_brand"] = _entry(
        key="bluelink_brand",
        label="Bluelink Marke",
        unit="-",
        configured=bluelink_brand_raw if bluelink_brand_raw else None,
        live_value=None,
        live_key=None,
        effective=bluelink_brand_raw if (bluelink_brand_raw and bluelink_brand_valid) else "hyundai",
        source="user" if (bluelink_brand_raw and bluelink_brand_valid) else "default",
        severity="warning" if not bluelink_brand_valid else "ok",
        message=(
            "Bluelink: Marke muss hyundai oder kia sein; wirksam ist hyundai."
            if not bluelink_brand_valid
            else "Marke des Herstellerkontos (Hyundai Bluelink oder Kia Connect)."
        ),
    )

    warnings = _warning_count(storage, wallbox, consumer, price, forecast)
    return {
        "ts": now,
        "version": 2,
        "summary": {
            "warnings": warnings,
            "live_available": bool(live),
            "source_order": "user_then_rscp_then_default",
        },
        "storage": storage,
        "wallbox": wallbox,
        "consumer": consumer,
        "price": price,
        "forecast": forecast,
    }


def atomic_write_json(path: str, payload: Dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
            handle.write("\n")
        os.replace(tmp, path)
        try:
            if os.path.basename(path) == "e3dc_v4.json":
                apply_config_secret_permissions(path, data=payload if isinstance(payload, dict) else None)
            else:
                os.chmod(path, 0o664)
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(tmp):
                os.unlink(tmp)
        except Exception:
            pass


def write_config_validation(
    cfg: Optional[Dict[str, Any]],
    live: Optional[Dict[str, Any]],
    path: str = CONFIG_VALIDATION_F,
) -> Dict[str, Any]:
    payload = validate_storage_config(cfg, live)
    _atomic_write_on_change(
        path,
        payload,
        force_interval_s=60.0,
        noise_keys={"ts"},
        indent=2,
    )
    return payload
