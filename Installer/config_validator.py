#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Plausibilitätsprüfung der Konfiguration gegen aktuelle E3DC-/RSCP-Werte.

Die Prüfung ist bewusst beratend: Nutzereingaben behalten Vorrang, wo der
Manager sie bereits unterstützt; starke Abweichungen von aktuellen E3DC-Werten
werden jedoch in der WebUI deutlich angezeigt.
"""

from __future__ import annotations

try:
    from .house_fuse import resolve_house_fuse, read_house_fuse
except ImportError:
    from house_fuse import resolve_house_fuse, read_house_fuse

import ipaddress
try:
    from .location_defaults import DEFAULT_LATITUDE, DEFAULT_LONGITUDE
except ImportError:
    from location_defaults import DEFAULT_LATITUDE, DEFAULT_LONGITUDE
try:
    from data_models import parse_mqtt_vehicle_allowlist, mqtt_vehicle_record_allowed
except ImportError:
    from Installer.data_models import parse_mqtt_vehicle_allowlist, mqtt_vehicle_record_allowed

try:
    from .shelly_relay_owner import shelly_relay_conflict, SHELLY_OWNER_WARNING
except ImportError:
    from shelly_relay_owner import shelly_relay_conflict, SHELLY_OWNER_WARNING
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
        recurring_tariff_stages,
        supports_heat_price_boost,
        supports_spot_market_prices,
    )
except Exception:  # pragma: no cover - direct script execution fallback
    from tariff_schedule import (  # type: ignore
        recurring_tariff_stages,
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
        external_ac_source_configured,
        external_ac_limit_w,
        has_explicit_topology_config,
        legacy_provider_resource_duplicate_keys,
    )
except Exception:  # pragma: no cover - direct script execution fallback
    from pv_forecast_topology import (  # type: ignore
        build_pv_forecast_topology,
        external_ac_source_configured,
        external_ac_limit_w,
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
        message = f"Die Solcast-Fläche {label} nutzt Konto {effective_slot} für die Prognoseabfrage."
        if not resource_configured:
            severity = "info"
            message = f"Die Fläche {label} ist nicht eingerichtet und liefert keine Solcast-Prognose."
        elif label in duplicate_labels:
            severity = "warning"
            message = (
                f'Die Solcast-Fläche {label} ist mehrfach eingetragen; die erweiterte Prognose bleibt aus. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Resource IDs FC1 bis FC4“ prüfen.'
            )
        elif not valid_slot:
            severity = "warning"
            message = f'Die Kontoauswahl für {label} ist ungültig; die erweiterte Prognose bleibt aus. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Konto je Site“ prüfen.'
        elif not key_available[effective_slot]:
            severity = "warning"
            message = f'Dem gewählten Solcast-Konto {effective_slot} fehlt der Zugangsschlüssel; die erweiterte Prognose bleibt aus. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „API Key Konto 1“ beziehungsweise „API Key Konto 2“ prüfen.'
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


def validate_pv_forecast_topology_config(cfg: Optional[Dict[str, Any]], live: Optional[Dict[str, Any]] = None) -> Dict[str, Dict[str, Any]]:
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
            'Die PV-Prognosediagnose ist an und wertet die Prognose aus; sie ändert weder Regelung noch Prognosemodell.' if diagnostics_enabled else 'Die PV-Prognosediagnose ist aus und führt keine zusätzlichen Auswertungen durch.' if diagnostics_valid else 'Der Schalterwert ist ungültig; die Diagnose bleibt aus. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „PV-Prognosediagnose“ prüfen.'
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
            label='Zuordnung der PV-Flächen',
            unit="-",
            configured=group_count if _has_user_value(cfg, "pv_forecast_topology_config") else None,
            live_value=None,
            live_key=None,
            effective={"generator_groups": group_count, "provider_resources": binding_count},
            source="user",
            severity="ok" if topology_ok and provider_ok else "warning",
            message=(
                f'Die {group_count} PV-Flächen sind erfasst, aber ihre Zuordnung zum Prognoseanbieter ist unvollständig; die Anbieterprognose bleibt aus. Im Config-Editor unter „Standort & PV-Prognose“ den Kasten „PV-Flächen und Wechselrichtergruppen“ prüfen.' if topology_ok and not provider_ok else f"Die Prognose verwendet {group_count} PV-Flächen und {binding_count} Zuordnungen zum Prognoseanbieter." if topology_ok else 'Die PV-Flächen sind unvollständig zugeordnet; ihre Erträge können nicht nach Wechselrichter getrennt werden. Im Config-Editor unter „Standort & PV-Prognose“ den Kasten „PV-Flächen und Wechselrichtergruppen“ prüfen.'
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
            message = f"Die Fläche {_label} ist nicht für die PV-Prognose eingerichtet."
            effective = None
        elif not item.get("has_geometry"):
            severity = "warning"
            effective = None
            message = (
                f'Der Fläche {_label} fehlen Angaben zu Größe und Ausrichtung; ihre Prognose kann nicht getrennt berechnet werden. Im Config-Editor unter „Standort & PV-Prognose“ den Kasten „PV-Flächen und Wechselrichtergruppen“ prüfen.'
            )
        elif item.get("coupling") in {"E3DC_DC", "EXTERNAL_AC"}:
            severity = "ok"
            effective = item.get("coupling")
            message = f"Die Fläche {_label} ist eindeutig dem {'E3DC-Wechselrichter' if effective == 'E3DC_DC' else 'Zusatzwechselrichter'} zugeordnet."
        else:
            severity = "warning"
            effective = None
            if not configured and contract.get("reason") == "RESOURCE_COUPLING_MISSING":
                external_ac_known = contract["external_ac_source_configured"]
                message = (
                    f'{_label}: Ohne vollständige Flächenzuordnung bleiben zusätzliche Speicherladung und Entladung für freie Speicherkapazität eingeschränkt; bei bekanntem Zusatzwechselrichter bleibt die volle Notstrom-Vorhaltung bestehen. Im Config-Editor unter „Standort & PV-Prognose“ die Kopplungsauswahl und den Kasten „PV-Flächen und Wechselrichtergruppen“ vervollständigen.' if external_ac_known else f'{_label}: Ohne vollständige Flächenzuordnung bleiben zusätzliche Speicherladung und Entladung für freie Speicherkapazität eingeschränkt; die Notstrom-Vorhaltung nutzt ohne bekannten Zusatzwechselrichter die Gesamtprognose, bei ausschließlicher E3DC-DC-Ladung bleibt sie vollständig bestehen. Im Config-Editor unter „Standort & PV-Prognose“ die Kopplungsauswahl und den Kasten „PV-Flächen und Wechselrichtergruppen“ vervollständigen.'
                )
            else:
                message = f'Die Fläche {_label} ist keinem Wechselrichter eindeutig zugeordnet; zusätzliche freie Speicherkapazität wird nicht eingeplant. Im Config-Editor unter „Standort & PV-Prognose“ den Kasten „PV-Flächen und Wechselrichtergruppen“ prüfen.'
        result[coupling_key] = _entry(
            key=coupling_key,
            label=f"Zuordnung der PV-Fläche {_label}",
            unit="-",
            configured=configured,
            live_value=None,
            live_key=None,
            effective=effective,
            source="user" if configured is not None else "missing",
            severity=severity,
            message=message,
        )

    external_raw = cfg.get("pv_external_ac_inverter_limit_w")
    external_value = external_ac_limit_w(external_raw)
    external_unreadable = _has_user_value(cfg, "pv_external_ac_inverter_limit_w") and external_value is None
    external_limit = external_value or 0.0
    external_required = bool(contract.get("external_ac_bound"))
    result["pv_external_ac_inverter_limit_w"] = _entry(
        key="pv_external_ac_inverter_limit_w",
        label='Externe AC-WR-Leistung (W)',
        unit="W",
        configured=external_value if _has_user_value(cfg, "pv_external_ac_inverter_limit_w") else None,
        live_value=None,
        live_key=None,
        effective=external_limit if external_limit > 0.0 else None,
        source="user" if external_limit > 0.0 else "missing",
        severity="warning" if external_unreadable or (external_required and external_limit <= 0.0) else "ok",
        message=(
            'Die Leistung des Zusatzwechselrichters ist unlesbar; seine Leistung bleibt unbekannt und die Notstrom-Vorhaltung berücksichtigt ihn vorsorglich. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Externe AC-WR-Leistung (W)“ prüfen.' if external_unreadable else 'Für den Zusatzwechselrichter fehlt die Leistungsgrenze; die Prognose kann nicht nach Wechselrichter getrennt werden. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Externe AC-WR-Leistung (W)“ ergänzen.' if external_required and external_limit <= 0.0 else 'Die Leistungsgrenze des Zusatzwechselrichters ist eingetragen und den PV-Flächen zugeordnet.' if external_limit > 0.0 and external_required else 'Die Leistung des Zusatzwechselrichters ist eingetragen, aber keiner PV-Fläche zugeordnet; solange eine Fläche offen ist, bleibt die volle Notstrom-Vorhaltung bestehen.' if external_limit > 0.0 else 'Es ist kein Zusatzwechselrichter für die PV-Prognose eingetragen.'
        ),
    )
    # Vorhandene RSCP-Livewerte wie bei den übrigen beratenden Abweichungshinweisen.
    additional, additional_key = None, None
    for key in ("EMS_POWER_ADD", "Power_ADD", "ADD_POWER", "add_power"):
        raw = (live or {}).get(key)
        if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
            continue
        value = safe_float(raw, float("nan"))
        if math.isfinite(value) and int(round(value)) < 0:
            additional, additional_key = int(round(value)), key
            break
    guard_w = max(0, int(round(safe_float(cfg.get("direct_marketing_pv_store_external_ac_guard_w"), 100))))
    if additional is not None and abs(additional) > guard_w and not external_ac_source_configured(cfg):
        result["pv_external_ac_inverter_limit_w"].update(
            severity="warning", rscp=additional, rscp_key=additional_key,
            message=f'Der E3DC meldet Zusatzleistung, obwohl kein Zusatzwechselrichter eingetragen ist; die Zuordnung ist unvollständig. Im Config-Editor unter „Standort & PV-Prognose“ die Kopplung „Externer AC-WR“, den Kasten „PV-Flächen und Wechselrichtergruppen“ oder „Externe AC-WR-Leistung (W)“ prüfen.',
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
        message='Die angegebene Messzuordnung gilt ab ihrer Erfassung; Leistungsbegrenzung und Leistungsgrenze des Zusatzwechselrichters werden getrennt geprüft.' if meter_mode in known_meter_modes[1:] else 'Die Messzuordnung ist noch nicht bestätigt; aus diesen Messungen wird keine neue Prognosekorrektur berechnet.' if meter_mode in known_meter_modes else 'Die Messzuordnung ist unbekannt; aus diesen Messungen wird keine neue Prognosekorrektur ermittelt. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Ist-Messung des Zusatzwechselrichters“ prüfen.',
    )
    result["pv_e3dc_dc_inverter_limit_w"] = _entry(
        key="pv_e3dc_dc_inverter_limit_w",
        label='Leistungsgrenze des E3DC-Wechselrichters',
        unit="W",
        configured=e3dc_limit if _has_user_value(cfg, "pv_e3dc_dc_inverter_limit_w") else None,
        live_value=None,
        live_key=None,
        effective=e3dc_limit if e3dc_limit > 0.0 else None,
        source="user" if e3dc_limit > 0.0 else "rscp_runtime",
        severity="ok" if e3dc_limit > 0.0 else "info",
        message=(
            'Die eingetragene Leistungsgrenze des E3DC-Wechselrichters gilt als Ersatz für einen fehlenden aktuellen Gerätewert.'
            if e3dc_limit > 0.0
            else 'Es ist keine Leistungsgrenze des E3DC-Wechselrichters eingetragen; dafür wird ein aktueller Gerätewert benötigt.'
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
        ("wp_pv_max_power_w", 'Elektrischer Profilwert (W, optional bei Ist-Regelung)', "W", "max_power_w", 0.0),
        ("wp_pv_battery_limit_wh", 'Energiekontingent innerhalb von 24 Stunden (Wh)', "Wh", "battery_limit_wh", 0.0),
        ("wp_pv_grid_limit_wh", 'Energiekontingent innerhalb von 24 Stunden (Wh)', "Wh", "grid_limit_wh", 0.0),
        ("wp_pv_battery_max_w", 'Maximale Leistung (W)', "W", "battery_max_w", 0.0),
        ("wp_pv_grid_max_w", 'Maximale Leistung (W)', "W", "grid_max_w", 0.0),
        ("wp_pv_reaction_s", 'Berücksichtigte Reaktionszeit (s)', "s", "reaction_s", 30.0),
        ("wp_pv_start_wait_s", 'Wartezeit auf den Verdichterstart (s)', "s", "start_wait_s", 600.0),
        ("wp_pv_handoff_timeout_s", 'Wartezeit auf die Leistungsreduzierung der Wallbox (s)', "s", "handoff_timeout_s", 120.0),
    )
    if measured or "wp_pv_start_power_w" in cfg:
        rules += (("wp_pv_start_power_w", 'Startleistung (W), 0 = bestehende Start-Grenze', "W", "start_power_w", 0.0),)
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
        message = f"Der eingestellte Wert von {effective:g} {unit} kann für die zusätzliche PV-Heizung verwendet werden; aktuelle Energie- und Leistungsgrenzen werden vor dem Start geprüft."
        if not valid:
            invalid_fields.append(key)
            message = f'Der Wert für „{label}“ fehlt oder ist ungültig; zusätzliche PV-Starts warten, normale Heizung und Warmwasser bleiben unabhängig. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „{label}“ prüfen.'
        elif key == "wp_pv_max_power_w" and effective == 0:
            message = "Keine elektrische Obergrenze eingetragen. Die Automatik verwendet die Startleistung; aktuelle Quellen- und Hardwaregrenzen bleiben wirksam."
        elif key == "wp_pv_start_power_w":
            message = 'Die eingestellte Leistung bestimmt den benötigten PV-Überschuss vor dem Start; 0 übernimmt die vorhandene Startgrenze. Nach dem Start zählt die gemessene Aufnahme.'
        elif effective == 0:
            message = 'Diese Energiequelle wird nicht zur Überbrückung genutzt, weil Leistung oder Energiemenge auf 0 stehen.'
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
        'Die eingestellten Energiequellen decken rechnerisch Start und Mindestlaufzeit. Vor jedem Start werden verfügbare Energie, Speicherreserve und aktuelle Leistungsgrenzen zusätzlich geprüft.' if funded else f'Die Überbrückungsenergie reicht mit {available_wh:.0f} Wh für die benötigten {required_wh:.0f} Wh während Start und Mindestlaufzeit nicht aus; zusätzliche PV-Starts warten. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ die Angaben unter „Luxtronik: PV-Automatik und Wolkenüberbrückung“ prüfen.'
    )
    if measured:
        message = (
            'Die eingestellten Energiequellen decken rechnerisch die Überbrückung während der Reaktionszeit; gestartet wird nach PV-Startleistung und Startverzögerung. Im Betrieb zählt die gemessene Aufnahme, und ausgeschöpfte Energiemengen beenden die zusätzliche Heizung nach der Mindestlaufzeit.' if funded else f'Die {available_wh:.0f} Wh Überbrückungsenergie begrenzen die mögliche Leistung während der Reaktionsfrist; ein PV-Start wird dadurch nicht zusätzlich gesperrt. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ die Angaben unter „Luxtronik: PV-Automatik und Wolkenüberbrückung“ prüfen.'
        )
    if not profile["valid"]:
        message = f'Das Leistungsprofil ist unvollständig oder ungültig; normale Heizung und Warmwasser bleiben unabhängig. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ den Abschnitt „Luxtronik: PV-Automatik und Wolkenüberbrückung“ prüfen.'
    entries["wp_pv_energy_reservation"] = _entry(
        key="wp_pv_energy_reservation", label='Energie für den zusätzlichen Wärmepumpenstart', unit="Wh",
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
        message = f'Der Verbindungswert liegt außerhalb {minimum} bis {maximum}; verwendet wird {default}. Im Config-Editor unter „Zusatzwechselrichter“ den angezeigten Verbindungswert prüfen.'
    elif not active:
        severity = "ok"
        message = 'Der Wert wird nur bei direkter Abfrage eines Sungrow-Zusatzwechselrichters verwendet.'
    else:
        severity = "ok"
        message = "Für die direkte Abfrage gilt: %s, wirksam %d (Standard %d)." % (label, effective, default)
    return _entry(
        key=key,
        label={'ext_inverter_unit_id': 'Modbus-Gerätenummer'}.get(key, label),
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
        kind_message = f'Der Wechselrichtertyp ist unbekannt; die direkte Abfrage bleibt aus. Im Config-Editor unter „Zusatzwechselrichter“ die Typauswahl prüfen.'
    elif active and settings["enabled"]:
        kind_severity = "info"
        kind_message = (
            "Der Sungrow-Zusatzwechselrichter wird alle %d s über Modbus TCP abgefragt und liefert Phasen-, PV-Strang- und Diagnosedaten. Die Regelung verwendet weiterhin den E3DC-Messwert." % settings["poll_s"]
        )
    elif active:
        kind_severity = "warning"
        kind_message = f'Der Zusatzwechselrichter kann wegen unvollständiger Verbindungseinstellungen nicht direkt abgefragt werden. Im Config-Editor unter „Zusatzwechselrichter“ die Typauswahl prüfen.'
    else:
        kind_severity = "ok"
        kind_message = 'Die direkte Abfrage eines Zusatzwechselrichters ist aus; es werden keine zusätzlichen Messwerte über das Netzwerk abgefragt.'
    entries["ext_inverter_type"] = _entry(
        key="ext_inverter_type",
        label='Zusatzwechselrichtertyp',
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
        ip_message = f'Die Adresse des Zusatzwechselrichters fehlt oder ist ungültig; die direkte Abfrage bleibt aus. Im Config-Editor unter „Zusatzwechselrichter“ die Adresse prüfen.'
    elif ip_user and not ip_valid:
        ip_severity = "warning"
        ip_message = f'Die Adresse des Zusatzwechselrichters ist keine gültige IPv4-Adresse; sie kann nicht verwendet werden. Im Config-Editor unter „Zusatzwechselrichter“ die Adresse prüfen.'
    elif not active:
        ip_severity = "ok"
        ip_message = 'Der Wert wird nur bei direkter Abfrage eines Sungrow-Zusatzwechselrichters verwendet.'
    else:
        ip_severity = "ok"
        ip_message = 'Die eingetragene Netzwerkadresse wird für die direkte Abfrage des Wechselrichters oder seines Kommunikationsmoduls über Modbus TCP verwendet.'
    entries["ext_inverter_ip"] = _entry(
        key="ext_inverter_ip",
        label='Adresse des Zusatzwechselrichters',
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
        cfg, "ext_inverter_unit_id", 'Modbus-Gerätenummer', "", _EXT_INVERTER_DEFAULT_UNIT_ID, 0, 247, active
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
            message = f'Der Pufferfühler ist unbekannt; die Ansicht zeigt keinen Pufferspeicher. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Pufferfühler (neue Ansicht)“ prüfen.'
        elif _WP_BUFFER_SENSORS[raw] is not None and _WP_BUFFER_SENSORS[raw] != wp_type:
            severity = "warning"
            effective = raw
            message = f'Der Pufferfühler passt nicht zum Wärmepumpentyp; die Ansicht zeigt keinen Messwert. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Pufferfühler (neue Ansicht)“ prüfen.'
        else:
            severity = "ok"
            effective = raw
            message = f"Der gewählte Pufferfühler wird in der Wärmepumpenansicht angezeigt; die Regelung bleibt unverändert."
        entries.update({
            "wp_buffer_sensor": _entry(
                key="wp_buffer_sensor", label='Pufferfühler (neue Ansicht)', unit="",
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
            message = f'Das Heizkreispumpensignal ist unbekannt; der Heizkreiszustand bleibt unbekannt. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Heizkreispumpe (neue Ansicht)“ prüfen.'
        elif wp_type != 0 and raw != "none":
            severity = "warning"
            effective = "none"
            message = f'Das Heizkreispumpensignal ist nur für Luxtronik verfügbar; die bisherige Anzeige bleibt erhalten. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Heizkreispumpe (neue Ansicht)“ prüfen.'
        else:
            severity = "ok"
            effective = raw
            message = "Das gewählte Heizkreispumpensignal wird in der Wärmepumpenansicht angezeigt; die Regelung bleibt unverändert."
        entries["wp_heating_circuit_pump"] = _entry(
            key="wp_heating_circuit_pump", label='Heizkreispumpe (neue Ansicht)', unit="",
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
        message = f'Der experimentelle SG-Ready-Ausgang ist für diesen Wärmepumpentyp nicht verfügbar; es wird nichts geschrieben. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „SG Ready schreiben“ prüfen.'
    elif not _is_enabled(cfg, "luxtronik"):
        message = f'Die Wärmepumpenerfassung ist aus; der experimentelle SG-Ready-Ausgang bleibt inaktiv. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „SG Ready schreiben“ prüfen.'
    elif not _address_configured(cfg, "stiebel_isg_ip"):
        message = f'Die ISG-Adresse fehlt; der experimentelle SG-Ready-Ausgang bleibt inaktiv. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „SG Ready schreiben“ prüfen.'
    elif _address_configured(cfg, "shelly_sg_ip") or _address_configured(cfg, "shelly_pause_ip"):
        message = (
            f'Ein eingetragener Shelly-Kontakt hat Vorrang; der experimentelle ISG-Ausgang schreibt nichts. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „SG Ready schreiben“ prüfen.'
        )
    elif str(cfg.get("auto_mode", "1")).strip().lower() in {"0", "false", "off", "aus", "nein"}:
        message = f'Die Gerätesteuerung ist aus; der experimentelle SG-Ready-Ausgang schreibt nichts. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Automatik darf Geräte steuern“ prüfen.'
    else:
        severity = "ok"
        effective = 1
        message = (
            "Der experimentelle Ausgang steuert nach einem Neustart bei Zustandswechseln nur den ersten SG-Ready-Eingang. Im Wärmepumpenregler SG Ready über Modbus mit Sicherheitstemperaturbegrenzer im Heizungsvorlauf einrichten und nur eine SG-Ready-Steuerung verwenden."
        )
    return {
        "stiebel_isg_sg_ready_write": _entry(
            key="stiebel_isg_sg_ready_write", label='SG Ready schreiben', unit="",
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
        ("grid_start_limit", 'Start-Grenze (Betrag in Watt)', "W", start, start < minimum,
         (f'Die Startgrenze von {start:g} W liegt unter der benötigten Mindestleistung von {minimum} W; ein kleinerer Wert ermöglicht keinen Start. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Start-Grenze (Betrag in Watt)“ prüfen.') if start < minimum else (f"Die Startgrenze von {start:g} W bezieht sich auf die verfügbare Verbraucherleistung nach der Speicherladung, nicht auf die Netzeinspeisung. Zum Start sind mindestens {minimum} W nötig.")),
        ("pv_boost_delay", 'Start-Verzögerung (Sek)', "s", delay, delay >= duration,
         (f'Die Startverzögerung von {delay:g} s hält bei SG Ready die Leistung zum Start mindestens {delay + 120:g} s bereit; andere Verbraucher können sie so lange nicht nutzen. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Start-Verzögerung (Sek)“ nur für bewusst längeres Warten so belassen.' if sg_ready else f'Die Startverzögerung von {delay:g} s ist mindestens so lang wie die {duration:g} s, für die Leistung zum Start bereitgehalten wird; diese Zeit wird bei direkter Sollwertvorgabe nicht verlängert. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Start-Verzögerung (Sek)“ prüfen.') if delay >= duration else (f"Die Startverzögerung von {delay:g} s liegt unter den {duration:g} s, für die diese Startart Leistung bereithält. " + ('Bei SG Ready bleibt die Leistung mindestens für die Verzögerung plus 120 s bereit; andere Verbraucher können sie währenddessen nicht nutzen.' if sg_ready else 'Bei direkter Sollwertvorgabe bleibt die Leistung nur für die kurze Startzeit bereit.'))),
    ):
        entries[key] = _entry(key=key, label=label, unit=unit, configured=cfg.get(key),
            live_value=None, live_key=None, effective=value, source="user",
            severity="warning" if warning else "ok", message=message)
    if str(cfg.get("wp_type", "")).strip() == "4":
        standby = safe_float(cfg.get("stiebel_isg_standby_w"), 35.0)
        entries["stiebel_isg_standby_w"] = _entry(
            key="stiebel_isg_standby_w", label='Bereitschaftsleistung der Stiebel-Wärmepumpe', unit="W",
            configured=cfg.get("stiebel_isg_standby_w"), live_value=None, live_key=None,
            effective=heatpump_idle_threshold_w(cfg), source="user",
            severity="warning" if not 0 <= standby <= 75 else "ok",
            message=(f'Die eingestellte Bereitschaftsleistung liegt außerhalb 0 bis 75 W; wirksam ist eine Leerlaufgrenze von {heatpump_idle_threshold_w(cfg):g} W. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Standby (W)“ prüfen.') if not 0 <= standby <= 75 else ('Die Leerlaufgrenze beträgt die Bereitschaftsleistung plus 25 W, mindestens 50 und höchstens 100 W; bei 35 W sind das 60 W. Eine erneute Freigabe erfordert aktuelle Leistungsdaten, einen bestätigt stillstehenden Verdichter und eine zurückgenommene Startfreigabe.'))
    return entries


def validate_climate_meter_config(cfg: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    """Bei überlappenden Messzielen warnen; keine Aktivierung oder Sperre."""
    if not (_is_enabled(cfg, "climate_enable") or str(cfg.get("climate_enable", "")).strip().lower() == "ja") or not _address_configured(cfg, "climate_meter2_ip"):
        return {}
    ip1 = str(cfg.get("climate_meter_ip", "")).strip().lower()
    ip2 = str(cfg.get("climate_meter2_ip", "")).strip().lower()
    if not ip1 or ip1 != ip2:
        return {}
    try:
        from .climate_live import normalize_meter_type, normalize_phase, normalize_shelly_em_gen1_channel
    except ImportError:
        from climate_live import normalize_meter_type, normalize_phase, normalize_shelly_em_gen1_channel
    def targets(prefix):
        kind = normalize_meter_type(cfg.get(prefix + "type", "shelly_pro3em"))
        phase = cfg.get(prefix + "phase", "c")
        if kind == "shelly_em_mini_gen4":
            return {"em1:0"}
        if kind == "shelly_pm_mini":
            return {"pm1:0"}
        if kind == "shelly_pro3em":
            phase = normalize_phase(phase)
            return {"em:0:" + p for p in (("a", "b", "c") if phase == "total" else (phase,))}
        if kind == "shelly_em_gen1":
            try:
                channel = normalize_shelly_em_gen1_channel(phase)
            except ValueError:
                return {"unknown"}
            return {"emeter:" + p for p in (("channel0", "channel1") if channel == "total" else (channel,))}
        return {"unknown"}
    a, b = targets("climate_meter_"), targets("climate_meter2_")
    if not (a & b) and "unknown" not in a | b:
        return {}
    return {"climate_meter2_ip": _entry(
        key="climate_meter2_ip", label='Zweiter Shelly-Zähler IP', unit="",
        configured=cfg.get("climate_meter2_ip"), live_value=None, live_key=None,
        effective=None, source="user", severity="warning",
        message=f'Beide Klima-Zähler verwenden dieselbe Adresse mit überlappenden oder unklaren Kanälen; Verbrauch kann doppelt gezählt werden. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Zweiter Shelly-Zähler IP“ prüfen.',
    )}


try:
    from .Storage import backup_hold
except ImportError:
    from Storage import backup_hold

def validate_forecast_location_config(cfg):
    """Beratender Standortcheck ohne Koordinaten im Diagnosepaket."""
    values = (cfg.get('hoehe'), cfg.get('laenge'))
    missing = any(value is None or str(value).strip() == '' for value in values)
    readable = False
    default_pair = False
    if not missing:
        try:
            lat, lon = (float(value) for value in values)
            readable = bool(not any(isinstance(value, bool) for value in values)
                            and math.isfinite(lat) and math.isfinite(lon)
                            and -90 <= lat <= 90 and -180 <= lon <= 180)
            if readable:
                default_pair = (lat, lon) in {(DEFAULT_LATITUDE, DEFAULT_LONGITUDE), (51.163375, 10.447683)}
        except (TypeError, ValueError, OverflowError):
            pass
    if missing:
        message = f'Der Standort fehlt; die PV-Prognose kann für den falschen Ort rechnen. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Breitengrad (Latitude)“ und „Längengrad (Longitude)“ prüfen.'
    elif not readable:
        message = f'Der Standort ist nicht lesbar; die PV-Prognose kann für den falschen Ort rechnen. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Breitengrad (Latitude)“ und „Längengrad (Longitude)“ prüfen.'
    elif default_pair:
        message = f'Es ist noch ein vorgegebener Standort eingetragen; die PV-Prognose passt möglicherweise nicht zur Anlage. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Breitengrad (Latitude)“ und „Längengrad (Longitude)“ prüfen.'
    else:
        message = 'Die PV-Prognose verwendet den eingetragenen eigenen Standort.'
    warning = missing or not readable or default_pair
    return {'hoehe': _entry(
        key='hoehe', label='Standort für die PV-Prognose', unit='',
        configured='[privat]', live_value=None, live_key=None,
        effective='Standort prüfen' if warning else 'Eigener Standort',
        source='default' if missing else 'user', severity='warning' if warning else 'ok',
        message=message,
    )}


def validate_storage_config(
    cfg: Optional[Dict[str, Any]], live: Optional[Dict[str, Any]],
    *, reserve_sample_fresh: Optional[bool] = None,
) -> Dict[str, Any]:
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
    consumer.update(validate_climate_meter_config(cfg))
    consumer.update(validate_heatpump_pv_config(cfg))
    consumer.update(validate_heatpump_display_config(cfg))
    consumer.update(validate_stiebel_sg_ready_config(cfg))
    if shelly_relay_conflict(cfg):
        consumer["shelly_3em_relay_id"] = _entry(
            key="shelly_3em_relay_id", label='Doppelt zugeordnetes Shelly-Relais', unit="",
            configured=cfg.get("shelly_3em_relay_id"), live_value=None, live_key=None,
            effective=None, source="user", severity="warning", message='Zwei Verbraucher verwenden dasselbe Shelly-Relais; die Relaissteuerung ist widersprüchlich. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Relais-ID“ beim Heizstab sowie „SG-Ready Shelly IP (Boost)“ und „EVU-Pause Shelly IP (Sperre)“ bei der Wärmepumpe prüfen.',
        )
    consumer.update(validate_heatpump_start_config(cfg))
    price: Dict[str, Dict[str, Any]] = {}
    cap = safe_float(cfg.get("ml_home_daily_cap_kwh"), 24.0)
    cap_warning = cap < 8 or cap > 80
    forecast = {"ml_home_daily_cap_kwh": _entry(
        key="ml_home_daily_cap_kwh", label='Haus-Tagesreferenz für ML (kWh)', unit="kWh",
        configured=cfg.get("ml_home_daily_cap_kwh"), live_value=None, live_key=None,
        effective=cap, source="user" if _has_user_value(cfg, "ml_home_daily_cap_kwh") else "default",
        severity="warning" if cap_warning else "info",
        message=f'Die Haus-Tagesreferenz von {cap:g} kWh liegt außerhalb 8 bis 80 kWh; sie begrenzt die Vergleichsbasis der Verbrauchsprüfung, nicht die Prognose. Im Config-Editor unter „Standort & PV-Prognose“ das Feld „Haus-Tagesreferenz für ML (kWh)“ prüfen.' if cap_warning else 'Der eingestellte Tagesverbrauch begrenzt die Vergleichsbasis der Verbrauchsprüfung; die ausgegebene Verbrauchsprognose wird dadurch nicht begrenzt.' + '',
    )}
    forecast.update(validate_forecast_location_config(cfg))
    forecast.update(validate_solcast_config(cfg))
    forecast.update(validate_pv_forecast_topology_config(cfg, live))
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
        message = "Ohne eigenen Wert und verfügbaren E3DC-Leistungswert gilt der Standardwert; zusätzlich gelten die ermittelten Gerätegrenzen."
        if configured is not None and math.isfinite(configured) and configured >= 300.0:
            source = "user"
            effective = configured
            severity = "ok"
            message = "Die eingetragene Leistungsgrenze wird unter Berücksichtigung der ermittelten Gerätegrenzen verwendet."
            if live_value is not None:
                delta, rel = _deviation(configured, live_value)
                if delta >= 2000.0 and rel >= 0.30:
                    severity = "warning"
                    message = (
                        f'Die Eingabe von {effective:g} W weicht deutlich von den Leistungsangaben des E3DC ab und bleibt wirksam. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „{dict(maximumladeleistung="Max. Laden (W)", maximaleentladeleistung="Max. Entladen (W)")[key]}“ prüfen.'
                    )
        elif live_value is not None:
            source = "rscp"
            effective = live_value
            severity = "ok"
            message = "Die Regelung verwendet die vom E3DC gemeldete Leistungsgrenze unter Berücksichtigung der ermittelten Gerätegrenzen."
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

    # Gemeinsame Laufzeitgrenze; der Import erfolgt erst nach Modulinitialisierung.
    try:
        from .Storage.power_limits import effective_power_limit_cfg
    except ImportError:
        from Storage.power_limits import effective_power_limit_cfg
    limits = effective_power_limit_cfg(cfg, live)["_power_limit_diagnostics"]
    for direction, key, label in (("charge", "maximumladeleistung", "Max. Laden (W)"),
                                  ("discharge", "maximaleentladeleistung", "Max. Entladen (W)")):
        prefix = "power_limit_" + direction
        device_key = "device_max_" + direction + "_power_w"
        entry = storage[key]
        entry["label"] = label
        if limits[device_key + "_valid"]:
            entry["effective"] = limits[prefix + "_effective_w"]
            entry["source"] = limits[prefix + "_source"]
            entry["rscp"] = limits[device_key]
            entry["rscp_key"] = device_key
        hint = limits[prefix + "_hint"]
        if hint and hint != "device_limit_unknown":
            entry["severity"] = "warning"
            entry["message"] = f'Die Leistungseingabe passt nicht zu den Angaben oder Empfehlungen des Speichers; wirksam sind {entry["effective"]:g} W. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „{label}“ prüfen.'
        elif hint == "device_limit_unknown":
            entry["device_limit_status"] = hint

    capacity_cfg = safe_float(cfg.get("speichergroesse"), float("nan")) if _has_user_value(cfg, "speichergroesse") else None
    capacity_live, capacity_live_key = _capacity_live_value(live)
    capacity_effective = 15.0
    capacity_source = "default"
    capacity_severity = "info"
    capacity_message = f"Der E3DC liefert keine Speicherkapazität; wirksam ist der Ersatzwert von {capacity_effective:g} kWh."
    if capacity_cfg is not None and math.isfinite(capacity_cfg) and capacity_cfg > 0.1:
        capacity_effective = capacity_cfg
        capacity_source = "user"
        capacity_severity = "ok"
        capacity_message = f"Die Regelung verwendet die eingetragene nutzbare Speicherkapazität von {capacity_effective:g} kWh."
        if capacity_live is not None:
            delta, rel = _deviation(capacity_cfg, capacity_live)
            if rel >= 0.05:
                capacity_severity = "warning"
                suggested_capacity = f"{capacity_live:.1f}".replace(".", ",")
                capacity_message = (
                    'Die eingetragene Speicherkapazität weicht mindestens 5 % vom nutzbaren Wert laut Speicher ab '
                    f'({suggested_capacity} kWh) und bleibt wirksam. Vorschlag: {suggested_capacity} kWh eintragen. '
                    'Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Kapazität (kWh)“ prüfen.'
                )
    elif capacity_live is not None:
        capacity_effective = capacity_live
        capacity_source = "rscp"
        capacity_severity = "ok"
        capacity_message = f"Die Regelung verwendet die vom E3DC gemeldete nutzbare Speicherkapazität von {capacity_effective:g} kWh."
    storage["speichergroesse"] = _entry(
        key="speichergroesse",
        label='Kapazität (kWh)',
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
    reserve_message = f"Die Notstromreserve des E3DC ist maßgeblich; wirksam sind {reserve_effective:g} %. Der eingestellte Ersatzwert greift nur ohne aktuellen Gerätewert."
    if reserve_details.get("normalised"):
        reserve_message = (
            f"Die Notstromreserve des E3DC wurde aus der Energiemenge auf den Gesamtspeicher umgerechnet; wirksam sind {reserve_effective:g} %. Die Prozentangabe des Geräts gilt nur für den reservierten Teil."
        )
    if reserve_live is None:
        reserve_message = f"Die Notstromreserve des E3DC ist nicht verfügbar; wirksam ist der Ersatzwert von {reserve_effective:g} %."
    elif reserve_cfg is not None and math.isfinite(reserve_cfg):
        delta, _rel = _deviation(reserve_cfg, safe_float(reserve_live, 0.0))
        # Der aufrufende Dienst liefert die Frische aus seinem bestehenden Vertrag.
        # Ohne bestätigte Frische bleibt die Gerätereserve für Hinweise unsicher.
        if delta >= 2.0 and reserve_sample_fresh is True:
            if reserve_cfg < reserve_live:
                reserve_severity = "warning"
                reserve_message = 'Die eigene Notstromreserve %.2f %% liegt unter der Gerätereserve %.2f %%; der höhere Wert bleibt wirksam. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Fallback-Reserve (%%)“ prüfen.' % (reserve_cfg, reserve_live)
            else:
                reserve_severity = "info"
                reserve_message = "Die eigene Notstromreserve von %.2f %% liegt über den %.2f %% des E3DC und bleibt wirksam; bei Bedarf die Reserve im Gerät angleichen." % (reserve_cfg, reserve_live)
            if reserve_details.get("normalised"):
                reserve_message += '' if reserve_severity == 'warning' else ' Die Gerätereserve wurde auf die Gesamtspeicherkapazität normalisiert.'
    storage["ep_reserve_pct"] = _entry(
        key="ep_reserve_pct",
        label='Notstromreserve als Ersatzwert',
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
    dc_first_message = 'Die Begrenzung der Speicherladung auf direkte E3DC-PV-Leistung ist aus; die bisherige Ladeführung bleibt erhalten.'
    if dc_first_enabled and dc_first_source_valid:
        dc_first_severity = "ok"
        dc_first_message = (
            "Die Speicherladung nach Ladekurve und das PV-Speichern der Direktvermarktung nutzen nur aktuell gemessene E3DC-PV-Leistung; die Entladung bleibt unverändert."
        )
    elif dc_first_enabled:
        dc_first_severity = "warning"
        dc_first_message = (
            f'Die Begrenzung auf E3DC-PV ist an, aber die aktuelle Aufteilung zwischen E3DC und Zusatzwechselrichter fehlt; Kurvenladung und PV-Speichern der Direktvermarktung bleiben auf 0 W begrenzt. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Speicherladung auf E3DC-DC begrenzen“ prüfen.'
        )
    storage["storage_dc_first_charge_limit_enable"] = _entry(
        key="storage_dc_first_charge_limit_enable",
        label='Speicherladung auf E3DC-DC begrenzen',
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
            'Die Speicherladung aus dem Zusatzwechselrichter ist aus und nicht freigegeben; auch bei zu wenig vorhergesagter PV-Energie wird sie nicht eingeplant.'
        )
    else:
        shortfall_aux_ac_severity = "warning"
        shortfall_aux_ac_message = (
            f'Die angeforderte Speicherladung aus dem Zusatzwechselrichter ist nicht freigegeben, weil ein belastbarer gemeinsamer Prognosenachweis fehlt; nur E3DC-PV bleibt zulässig. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Nicht freigegeben: Zusatz-AC bei probabilistisch belegtem DC-Defizit“ prüfen.'
        )
    storage["storage_forecast_shortfall_aux_ac_charge_enable"] = _entry(
        key="storage_forecast_shortfall_aux_ac_charge_enable",
        label=(
            'Zusatzwechselrichterladung bei Prognoseminderertrag (nicht freigegeben)'
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
            f'Ein leerer Wert wirkt wie ausgeschaltet; ein spätes Erreichen von 100 % wird nie geplant. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Später Vollstand nur mit Grund“ prüfen.'
        )
    elif not late_full_forecast_mode:
        late_full_severity = "info"
        late_full_source = "user" if late_full_present else "default"
        late_full_message = (
            'Die Einstellung wirkt nur im Zielkurven-Modus „Prognose auf 100%“; die derzeitige Ankerkurve bleibt unverändert. Die Auswahl bleibt für einen späteren Moduswechsel gespeichert.'
        )
    elif late_full_enabled:
        late_full_severity = "ok"
        late_full_source = "user" if late_full_present else "default"
        late_full_message = (
            "Ein später Vollstand kurz vor PV-Ende wird nur bei Einspeiselimit, drohender Abregelung, Direktvermarktung oder vorheriger gezielter Entladung geplant. Sonst endet die Kurve um den eingestellten Zeitpuffer vor dem letzten nutzbaren PV-Überschuss."
        )
    else:
        late_full_severity = "info"
        late_full_source = "user"
        late_full_message = (
            "Die Ladekurve endet immer um den eingestellten Zeitpuffer vor dem letzten nutzbaren PV-Überschuss. Maßnahmen gegen Abregelung und die vorherige gezielte Entladung bleiben aktiv."
        )
    storage["storage_forecast100_late_full_guard_enable"] = _entry(
        key="storage_forecast100_late_full_guard_enable",
        label='Später Vollstand nur mit Grund',
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
            f'Der Zeitpuffer ist unlesbar; verwendet werden 45 min. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Kurvenende-Puffer (min)“ prüfen.'
        )
    elif guard_min_raw < 30.0 or guard_min_raw > 120.0:
        guard_min_effective = max(30.0, min(120.0, guard_min_raw))
        guard_min_source, guard_min_severity = "user", "warning"
        guard_min_message = (
            f'Der Zeitpuffer von {guard_min_raw:g} min liegt außerhalb 30 bis 120 min; wirksam sind {guard_min_effective:g} min. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Kurvenende-Puffer (min)“ prüfen.'
        )
    else:
        guard_min_effective, guard_min_source, guard_min_severity = guard_min_raw, "user", "ok"
        guard_min_message = (
            "Die Ladekurve endet %.0f min vor dem letzten nutzbaren PV-Überschuss. Größer = früher voll "
            "und mehr Reserve gegen Wolken, kleiner = später voll." % guard_min_raw
        )
    storage["storage_curve_end_guard_min"] = _entry(
        key="storage_curve_end_guard_min",
        label='Kurvenende-Puffer (min)',
        unit="min",
        configured=cfg.get("storage_curve_end_guard_min") if guard_min_user else None,
        live_value=None,
        live_key=None,
        effective=round(float(guard_min_effective), 1),
        source=guard_min_source,
        severity=guard_min_severity,
        message=guard_min_message,
    )

    house_fuse = resolve_house_fuse(cfg, read_house_fuse())
    grid_user_present = _has_user_value(cfg, "grid_max_amps")
    grid_source = house_fuse["source"]
    grid_invalid = grid_source == "invalid"
    # Nur Folgeprüfungen nutzen ohne Eintrag den bisherigen PV-Rahmen.
    grid_amps = house_fuse["effective"] if house_fuse["effective"] is not None else (0.0 if grid_invalid else 35.0)
    grid_severity = "ok"
    grid_location = ' Im Config-Editor unter „Wallbox & Fahrzeug“, Feld „Hausabsicherung/SLS (A je Phase)“ prüfen.'
    if grid_invalid:
        grid_severity = "warning"
        grid_message = "Die Hausabsicherung ist ungültig; gesteuertes Wallboxladen und Netzladen bleiben gesperrt." + grid_location
    elif grid_source == "none":
        grid_severity = "warning"
        grid_message = "Keine Hausabsicherung hinterlegt – Netzladen nicht möglich." + grid_location
    elif grid_source == "e3dc_unconfirmed_user":
        grid_severity = "warning"
        grid_message = house_fuse["hint"] + grid_location
    else:
        grid_message = f"Die wirksame Hausabsicherung von {grid_amps:g} A je Phase begrenzt das gesteuerte Laden."
        if house_fuse["hint"]:
            grid_severity = "info"
            grid_message += " " + house_fuse["hint"]
        if grid_amps < 16.0 or grid_amps > 100.0:
            grid_severity = "warning"
            grid_message += " Der Wert liegt außerhalb des üblichen Bereichs von 16 bis 100 A je Phase." + grid_location
    wallbox["grid_max_amps"] = _entry(
        key="grid_max_amps", label='Hausabsicherung/SLS (A je Phase)', unit="A",
        configured=cfg.get("grid_max_amps") if grid_user_present else None,
        live_value=house_fuse["e3dc_amps"], live_key="e3dc_fuse_amps",
        effective=house_fuse["effective"], source=grid_source,
        severity=grid_severity, message=grid_message)
    confirmed_raw = cfg.get("grid_max_amps_confirmed")
    confirmed_value = safe_float(confirmed_raw, float("nan")) if _has_user_value(cfg, "grid_max_amps_confirmed") else None
    confirmed_valid = confirmed_value is not None and math.isfinite(confirmed_value) and confirmed_value > 0
    wallbox["grid_max_amps_confirmed"] = _entry(
        key="grid_max_amps_confirmed", label="Bestätigte Hausabsicherung", unit="A",
        configured=confirmed_raw, live_value=None, live_key=None,
        effective=confirmed_value if confirmed_valid else None,
        source="user" if confirmed_valid else "none" if confirmed_raw in (None, "") else "invalid",
        severity="ok" if confirmed_valid or confirmed_raw in (None, "") else "warning",
        message="Die Bestätigung gilt ausschließlich für den numerisch gleichen Nutzerwert.")

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
        f"Für übrige Hausverbraucher bleiben auf jeder Netzphase {grid_reserve:g} A bei der Wallboxverteilung frei."
    )
    if grid_reserve_invalid:
        grid_reserve_severity = "warning"
        grid_reserve_message = (
            f'Die Wallbox-Reserve ist ungültig; verwendet werden 2 A. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Reserve für übrige Hausverbraucher (A)“ prüfen.'
        )
    elif grid_reserve >= grid_amps:
        grid_reserve_severity = "warning"
        grid_reserve_message = (
            f'Die Wallbox-Reserve von {grid_reserve:g} A erreicht die Hausabsicherung von {grid_amps:g} A; es bleibt kein Spielraum zum Laden. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Reserve für übrige Hausverbraucher (A)“ prüfen.'
        )
    wallbox["grid_wallbox_reserve_amps"] = _entry(
        key="grid_wallbox_reserve_amps",
        label='Reserve für übrige Hausverbraucher (A)',
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
        resolved_phase = house_fuse["phase_limits_effective"][f"l{phase}"]
        limit_effective = None if limit_invalid else (resolved_phase if resolved_phase is not None else grid_amps)
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
        phase_message = f"Auf Netzphase L{phase} gilt die eingestellte Stromgrenze; die Reserve bleibt für übrige Hausverbraucher frei."
        if limit_invalid or reserve_invalid:
            phase_severity = "warning"
            phase_message = (
                f'Ein Wert für Netzphase L{phase} ist unlesbar; diese Phase bleibt für zusätzliche Leistung gesperrt. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Netz L{phase} Limit (A)“ und „Netz L{phase} Reserve (A)“ prüfen.'
            )
        elif limit_effective <= 0.0 or phase_reserve_effective < 0.0:
            phase_severity = "warning"
            phase_message = f'Die Stromgrenze von {limit_effective:g} A oder die Reserve von {phase_reserve_effective:g} A für Netzphase L{phase} ist ungültig; die Grenze muss größer als null und die Reserve mindestens null sein. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Netz L{phase} Limit (A)“ und „Netz L{phase} Reserve (A)“ prüfen.'
        elif phase_reserve_effective >= limit_effective:
            phase_severity = "warning"
            phase_message = f'Die Reserve von {phase_reserve_effective:g} A erreicht die Stromgrenze von {limit_effective:g} A für Netzphase L{phase}; es bleibt kein Spielraum zum Laden. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Netz L{phase} Limit (A)“ und „Netz L{phase} Reserve (A)“ prüfen.'
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
        ("wbmaxladestrom", 'Standard-Maximalstrom je Wallbox (6–32 A)', 16.0),
        ("wb1_max_amp", 'WB1 Max. Ladestrom (A)', global_wb_max),
        ("wb2_max_amp", 'WB2 Max. Ladestrom (A)', global_wb_max),
    ):
        configured = safe_float(cfg.get(key), float("nan")) if _has_user_value(cfg, key) else None
        effective = configured if configured is not None and math.isfinite(configured) else float(fallback)
        severity = "ok"
        message = (
            f"Der maximale Ladestrom beträgt {effective:g} A und liegt innerhalb der geprüften Grenzen."
        )
        if effective < 6.0 or effective > 32.0:
            severity = "warning"
            message = (
                f'Der Ladestrom von {effective:g} A liegt außerhalb 6 bis 32 A. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „{label}“ prüfen.'
            )
        elif effective > grid_amps:
            severity = "warning"
            message = (
                f'Der Ladestrom von {effective:g} A liegt über der Hausabsicherung von {grid_amps:g} A je Phase. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „{label}“ prüfen.'
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
            f'Die Messbasis ist unbekannt; einphasiges Laden bleibt auf 20 A begrenzt. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Messbasis 1p-Deckel openWB Pro“ prüfen.'
        )
    elif pcc_basis_effective == "off":
        pcc_basis_severity = "ok"
        pcc_basis_message = (
            'Die zusätzliche Phasenmessung ist aus; einphasiges Laden an der openWB Pro bleibt auf 20 A begrenzt.'
        )
    else:
        pcc_basis_severity = "ok"
        pcc_basis_message = (
            'Für einphasiges Laden wird die Belastung der Netzphase aus Netzleistung, Phasenspannung und gemessenem Wallboxstrom ermittelt. Mehr als 20 A sind nur mit eingetragener Hausabsicherung, bestätigter Phasenzuordnung und bestätigter Softwareprüfung möglich.'
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
            f'Die Reserve für die Stromberechnung liegt außerhalb 0,8 bis 1,0; verwendet wird 0,9. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Leistungsfaktor-Reserve Fremdlast“ prüfen.'
        )
    else:
        pf_severity = "ok"
        pf_message = (
            f"Der Netzstrom anderer Verbraucher auf der Wallboxphase wird mit dem Faktor {pf_effective:g} vorsichtig höher angesetzt, weil die Leistungsmessung Blindstrom nicht erfasst. Der Faktor 1,0 eignet sich nur für rein ohmsche Lasten."
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
            f'Die Schieflastgrenze liegt außerhalb 10 bis 32 A; verwendet werden 20 A. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Schieflast-Wächter am Netzpunkt (A)“ prüfen.'
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
            'Der eingestellte Strom begrenzt den Belastungsunterschied zwischen der Wallboxphase und der am wenigsten belasteten Netzphase. Die daraus berechnete Ladestromgrenze fällt nie unter 20 A.'
        )
    wallbox["grid_pcc_imbalance_max_a"] = _entry(
        key="grid_pcc_imbalance_max_a",
        label='Schieflast-Wächter am Netzpunkt (A)',
        unit="A",
        configured=imbalance_candidate if imbalance_valid else (cfg.get("grid_pcc_imbalance_max_a") if imbalance_user else None),
        live_value=None,
        live_key=None,
        effective=imbalance_effective,
        source="user" if imbalance_valid else ("invalid" if imbalance_user else "default"),
        severity=imbalance_severity,
        message=imbalance_message,
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
            f'Die experimentelle direkte Phasensteuerung ist für {", ".join("WB%d" % i for i in _rm_direct_on_ids)} an; bei efy oder Multi Connect ändert sie bei Phasenwechseln Geräteeinstellungen mit ungeklärter Dauerspeicherung und setzt die Übergabe an die E3DC-Sonnenautomatik aus. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „E3DC-Direktvertrag (experimentell)“ prüfen; die Funktion nur zum Testen und nicht im Dauerbetrieb nutzen.'
        )
    else:
        _rm_direct_severity = "ok"
        _rm_direct_message = (
            'Die experimentelle direkte Phasensteuerung ist aus; E3DC-Control ändert keine Phaseneinstellungen der E3/DC-Wallbox.'
        )
    wallbox["wb_e3dc_direct_phase_control_enable"] = _entry(
        key="wb_e3dc_direct_phase_control_enable",
        label='Direkte Phasensteuerung (experimentell)',
        unit="",
        configured=cfg.get("wb_e3dc_direct_phase_control_enable") if _rm_direct_global_user else None,
        live_value=None,
        live_key=None,
        effective=1 if _rm_direct_on_ids else 0,
        source="user" if _rm_direct_any_user else "default",
        severity=_rm_direct_severity,
        message=_rm_direct_message,
    )

    _observe_mode = "mixed" if cfg.get("wb_e3dc_observe_mode") == "mixed" else "sun"
    wallbox["wb_e3dc_observe_mode"] = _entry(
        key="wb_e3dc_observe_mode", label="Betriebsart für Beobachten wählen", unit="",
        configured=cfg.get("wb_e3dc_observe_mode"), live_value=None, live_key=None,
        effective=_observe_mode,
        source="user" if cfg.get("wb_e3dc_observe_mode") in ("sun", "mixed") else "default",
        severity="ok",
        message="Beim nächsten bewussten Wechsel auf Beobachten gilt "
            + ("Mischmodus." if _observe_mode == "mixed" else "Sonnenmodus (Werkseinstellung)."),
    )

    # Dieselbe Normalisierung wie im Treiber, einschließlich seiner Aliase.
    _family_rows = []
    _family_severity = 'ok'
    _family_configured = {}
    _family_effective = {}
    for _family_id in (1, 2):
        _family_key = 'wb%d_e3dc_device_family' % _family_id
        _family_type_key = 'wb_native_type' + ('2' if _family_id == 2 else '')
        _family_type = str(cfg.get(_family_type_key) or '').strip().lower()
        _family_multi = _family_type in {'e3dc_multi', 'e3dc_multi_connect', 'e3dc-multi', 'e3dc multi'}
        if not _family_multi and _family_key not in cfg:
            continue
        try:
            from .Wallbox.drivers import _normalize_e3dc_device_family
        except ImportError:  # pragma: no cover - direkter Installer-Import
            from Wallbox.drivers import _normalize_e3dc_device_family
        _family_choice = cfg.get(_family_key)
        _family_raw = _family_choice
        if _family_raw in (None, ''):
            _family_raw = cfg.get('e3dc_device_family')
        _family_default = {
            'e3dc_efy': 'efy', 'e3dc_easy': 'easy_connect',
            'e3dc_easy_connect': 'easy_connect', 'e3dc_multi_connect_ii': 'multi_connect_ii',
        }.get(_family_type, 'multi_connect' if _family_multi else 'unknown')
        _family_value = _normalize_e3dc_device_family(_family_raw, _family_default)
        _family_known = _normalize_e3dc_device_family(_family_raw, '__invalid__') != '__invalid__'
        _family_item_severity = 'info' if _family_choice in (None, '') else ('ok' if _family_known else 'warning')
        if {'ok': 0, 'info': 1, 'warning': 2}[_family_item_severity] > {'ok': 0, 'info': 1, 'warning': 2}[_family_severity]:
            _family_severity = _family_item_severity
        _family_configured['WB%d' % _family_id] = cfg.get(_family_key)
        _family_effective['WB%d' % _family_id] = _family_value
        _family_note = 'nicht festgelegt' if _family_item_severity == 'info' else ('unbekannter Wert' if _family_item_severity == 'warning' else 'festgelegt')
        _family_rows.append("WB%d: Auswahl %s, wirksames Modell %s" % (_family_id, _family_note, {"efy":"efy", "easy_connect":"Easy Connect", "multi_connect":"Multi Connect", "multi_connect_ii":"Multi Connect II", "unknown":"unbekannt"}.get(_family_value,_family_value)))
    if _family_rows:
        wallbox['e3dc_device_family'] = _entry(
            key='e3dc_device_family', label='E3/DC-Modell', unit='',
            configured=_family_configured, live_value=None, live_key=None,
            effective=_family_effective, source='user', severity=_family_severity,
            message=(f'Die Modellauswahl ist unbekannt; es gelten die geräteabhängigen Ersatzmodelle. Im Config-Editor unter „Wallbox & Fahrzeug“ das Nachfolgefeld „Modell“ am jeweiligen Ladepunkt prüfen.') if _family_severity == 'warning' else (' '.join(_family_rows)),
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
                f'Der Zeitwert liegt außerhalb {int(low)} bis {int(high)} {unit}; verwendet werden {int(default)} {unit}. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „{label}“ prüfen.'
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
        'Pro Startfenster (s)',
        180.0,
        60.0,
        600.0,
        'Das von der Box bestätigte Stromangebot bleibt bis zur Leistungsaufnahme während des Startfensters bestehen; es wird nicht erhöht oder auf 0 A gesetzt, eine Absenkung bis 6 A erfolgt sofort. Dieselbe Zeit gilt als Abschaltverzögerung bei fehlender PV-Leistung.',
    )
    _openwb_pro_start_window_entry(
        "openwb_pro_start_retry_cycle_s",
        'Pro Start-Wiederholzyklus (s)',
        300.0,
        180.0,
        1200.0,
        'Ohne Leistungsaufnahme beginnt nach dieser Zeit ein neuer Startversuch mit höchstens einem Weckimpuls. Nach höchstens drei Versuchen je Anstecken bleibt das Stromangebot mit einem Hinweis bestehen.',
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
        grace_message = f'Die Wartezeit vor dem ersten Weckimpuls ist unlesbar; verwendet werden 60 s. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Pro Start-CP-Karenz (s)“ prüfen.'
    elif grace_numeric and grace_candidate < 60.0:
        grace_severity = "warning"
        grace_message = (
            f'Die Wartezeit vor dem ersten Weckimpuls liegt unter 60 s und wird auf 60 s angehoben, damit ein normal startendes Auto nicht unterbrochen wird. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Pro Start-CP-Karenz (s)“ prüfen.'
        )
    elif grace_numeric and grace_candidate > 600.0:
        grace_severity = "warning"
        grace_message = f'Die Wartezeit vor dem ersten Weckimpuls liegt über 600 s und wird auf 600 s begrenzt. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Pro Start-CP-Karenz (s)“ prüfen.'
    else:
        grace_severity = "ok"
        grace_message = (
            f"Nach einem von der openWB Pro bestätigten Stromangebot ab 6 A wartet die Regelung {grace_effective:g} s vor dem ersten Weckimpuls. Ein nur angenommenes Stromangebot löst keinen Impuls aus."
        )
    wallbox["openwb_pro_start_cp_grace_s"] = _entry(
        key="openwb_pro_start_cp_grace_s",
        label='Pro Start-CP-Karenz (s)',
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
            'Die eingestellte Anzahl begrenzt die Weckimpulse auf ein bis drei je Anstecken, auch bei wiederholten Startversuchen.' if cp_valid or not cp_user else 'Die Anzahl der Weckimpulse liegt außerhalb 1 bis 3; verwendet werden 3 je Anstecken. Kein eigenes Feld im Config-Editor; die Konfiguration durch den Fachbetrieb prüfen lassen.'
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
        # ausdrücklich eingetragen (Nutzereingabe oder gültiger E3DC-Wert)
        # und das Betriebslimit ≥ 6 A ist; ohne frische Messwerte bleibt es bei 20 A.
        effective_limit = min(requested_limit, 20.0)
        limit_source = "safe_fallback" if configured_limit is None else "user"
        severity = "ok"
        message = 'Für einphasiges Laden an der openWB Pro gilt eine Obergrenze von 20 A.'
        if configured_limit_invalid:
            limit_source = "invalid"
            severity = "warning"
            message = (
                f'Die einphasige Stromgrenze ist unlesbar; es gelten höchstens 20 A. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro 1p Max. (A)“ prüfen.'
            )
        elif requested_limit < 6.0 or requested_limit > 32.0:
            severity = "warning"
            message = f'Die einphasige Stromgrenze von {effective_limit:g} A liegt außerhalb 6 bis 32 A. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro 1p Max. (A)“ prüfen.'
        elif requested_limit > 20.0:
            limit_source = "user_capped_fail_closed"
            phase_limit_explicit = bool(phase_valid and house_fuse["explicit"]
                and house_fuse["phase_limits_explicit"].get(f"l{mapped_phase}", False))
            if pcc_basis_effective == "off":
                severity = "warning"
                message = (
                    f'Mehr als 20 A bleiben gesperrt, weil die Messbasis ausgeschaltet ist. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro 1p Max. (A)“ prüfen.'
                )
            elif not phase_valid:
                severity = "warning"
                message = (
                    f'Mehr als 20 A bleiben gesperrt, weil die Netzphase der Wallbox nicht eindeutig zugeordnet ist. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro 1p Max. (A)“ prüfen.'
                )
            elif not phase_limit_explicit:
                severity = "warning"
                message = (
                    f'Mehr als 20 A bleiben gesperrt, weil eine ausdrücklich eingetragene Hausabsicherung fehlt. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro 1p Max. (A)“ prüfen.'
                )
            elif not phase_grid_contract_valid.get(mapped_phase, False):
                severity = "warning"
                message = (
                    f'Mehr als 20 A bleiben gesperrt, weil Stromgrenze oder Reserve der Netzphase ungültig sind. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro 1p Max. (A)“ prüfen.'
                )
            else:
                operating_limit = (
                    phase_grid_limits[mapped_phase]
                    - phase_grid_reserves[mapped_phase]
                )
                if operating_limit < 6.0:
                    severity = "warning"
                    message = f'Die Stromgrenze der Netzphase lässt mit {effective_limit:g} A keinen ausreichenden Ladestrom zu. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro 1p Max. (A)“ prüfen.'
                else:
                    effective_limit = min(requested_limit, operating_limit)
                    limit_source = "user_dynamic_pcc_pm"
                    severity = "ok"
                    message = (
                        f"Einphasiges Laden bis {effective_limit:g} A wird erst mit aktuellen Messwerten der zugeordneten Netzphase erlaubt; ohne diese gelten höchstens 20 A."
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
                'Die Netzphase der Wallbox ist eingetragen; E3DC-Control prüft und bestätigt die Zuordnung beim einphasigen Laden automatisch im Dashboard.' if phase_valid else f'Die Netzphase der Wallbox ist nicht zugeordnet; eine einphasige Freigabe über 20 A bleibt gesperrt. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} lokale L1 liegt auf Netzphase“ prüfen.' if requested_limit > 20.0 else 'Die Netzphase der Wallbox ist nicht eingetragen; einphasiges Laden über 20 A bleibt ausgeschlossen.'
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
                'Nach dem Abstecken einmalig 6 A: das Fahrzeug startet beim Anstecken sofort, danach regelt die normale Logik.' if unplug_effective == 'fast_start' else f'Die Auswahl ist ungültig; nach dem Abstecken gelten 0 A. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „WB{charger_id} openWB Pro nach dem Abstecken“ prüfen.' if unplug_user and (not unplug_valid) else 'Nach dem Abstecken einmalig 0 A: beim Anstecken startet nichts, bevor die Regelung entscheidet.'
            ),
        )

    native_wallbox_active = _is_enabled(cfg, "wb_native_enable")
    wallbox["wallbox_phase_budget"] = _entry(
        key="wallbox_phase_budget",
        label='Verteilung der Wallboxleistung auf die Netzphasen',
        unit="",
        configured=None,
        live_value=None,
        live_key=None,
        effective="runtime_phase_vector" if native_wallbox_active else "status_only",
        source="phase_vector_contract",
        severity="ok",
        message=(
            'Die Wallboxregelung verteilt die Ladeleistung und berücksichtigt für jeden Ladepunkt die belasteten Netzphasen. Einphasige und dreiphasige Ströme werden dabei je Phase getrennt bewertet.'
            if native_wallbox_active
            else
            'Ohne zentrale Wallboxregelung werden die maximalen Ladeströme einzeln geprüft. Eine gemeinsame Stromsumme wird nicht gebildet, weil die Ladepunkte unterschiedliche Netzphasen belasten können.'
        ),
    )

    wbminsoc_val = safe_float(cfg.get("wbminsoc"), 70.0)
    if wbminsoc_val < reserve_effective:
        raise ValueError(
            f"Der Wallbox-Mindestladestand von {wbminsoc_val:g} % liegt unter der Notstromreserve von {reserve_effective:g} %; diese Einstellung ist unzulässig. "
            "Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Haus-Priorität (Batterie-Reserve SoC %)“ mindestens auf die Notstromreserve anheben."
        )
    wbminsoc_severity = "ok"
    wbminsoc_message = f"Unter {wbminsoc_val:g} % Speicherladestand erhält die Hausversorgung Vorrang vor dem Fahrzeugladen."
    if wbminsoc_val < 20.0:
        wbminsoc_severity = "warning"
        wbminsoc_message = (
            f'Der Mindestladestand von {wbminsoc_val:g} % ist sehr niedrig; Fahrzeugladen kann den Hausspeicher nachts stärker entladen. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Haus-Priorität (Batterie-Reserve SoC %)“ prüfen.'
        )
    elif wbminsoc_val > 90.0:
        wbminsoc_severity = "warning"
        wbminsoc_message = f'Der Mindestladestand von {wbminsoc_val:g} % ist sehr hoch; die Wallbox wird selten freigegeben. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Haus-Priorität (Batterie-Reserve SoC %)“ prüfen.'
    wallbox["wbminsoc"] = _entry(
        key="wbminsoc",
        label='Haus-Priorität (Batterie-Reserve SoC %)',
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
    wb_bat_target_soc_message = f"Der abendliche Speicherzielstand von {wb_bat_target_soc_val:g} % liegt mindestens auf Höhe der Reserve für das Fahrzeugladen."
    if wb_bat_target_soc_val < wbminsoc_val:
        wb_bat_target_soc_severity = "warning"
        wb_bat_target_soc_message = f'Der abendliche Zielladestand von {wb_bat_target_soc_val:g} % liegt unter dem Wallbox-Mindestladestand. Kein eigenes Feld im Config-Editor; die Konfiguration durch den Fachbetrieb prüfen lassen.'
    wallbox["wb_bat_target_soc"] = _entry(
        key="wb_bat_target_soc",
        label='Speicherzielstand am Abend für das Fahrzeugladen',
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
        f"Unter der Ladekurve ohne Überbrückungsenergie deckt der Speicher Haus und Wärmepumpe sowie {pv_only_reserve_effective_w:g} W Reserve; die Wallbox erhält nur PV-Überschuss."
    )
    if pv_only_reserve_user and not pv_only_reserve_finite:
        pv_only_reserve_severity = "warning"
        pv_only_reserve_message = (
            f'Die Reserve unter der Ladekurve ist unlesbar; verwendet werden 300 W. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Reserve unter der Kurve (W)“ prüfen.'
        )
    elif pv_only_reserve_user and pv_only_reserve_configured < pv_only_reserve_default_w:
        pv_only_reserve_severity = "warning"
        pv_only_reserve_message = (
            f'Die Reserve unter der Ladekurve liegt unter 300 W und wird auf 300 W angehoben, um Netzbezug bei Lastsprüngen zu vermeiden. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Reserve unter der Kurve (W)“ prüfen.'
        )
    elif pv_only_reserve_user and pv_only_reserve_configured > 3000.0:
        pv_only_reserve_severity = "warning"
        pv_only_reserve_message = (
            f'Die Reserve unter der Ladekurve von {pv_only_reserve_effective_w:g} W ist sehr hoch; der Akku kann dadurch zusätzlich zu Haus und Wärmepumpe auch Wallboxstrom liefern. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Reserve unter der Kurve (W)“ prüfen.'
        )
    wallbox["wb_curve_pv_only_house_reserve_w"] = _entry(
        key="wb_curve_pv_only_house_reserve_w",
        label='Reserve unter der Kurve (W)',
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
            'Die Wärmepumpe ist im E3DC-Hausverbrauch enthalten; ihre gemeldete Leistung wird herausgerechnet und getrennt geführt. Haus und Wärmepumpe zählen dadurch insgesamt nur einmal.'
        )
    elif home_wp_split_mode == "separate":
        home_wp_split_message = (
            "Die Wärmepumpe wird getrennt gemessen und zählt zusätzlich zum E3DC-Hauswert; "
            "der Hauswert bleibt unverändert."
        )
    else:
        home_wp_split_message = (
            'Die automatische Zuordnung zählt die Wärmepumpe im Hauswert, sobald dieser mindestens 500 W und 55 % der Wärmepumpenleistung erreicht, darunter zusätzlich; bei kleinem Hausverbrauch kann die Zuordnung wechseln.'
        )
    if home_wp_split_unknown:
        home_wp_split_severity = "warning"
        home_wp_split_message = (
            f'Die Zuordnung der Wärmepumpe zum Hausverbrauch ist unbekannt; die automatische Schätzung kann zwischen enthaltener und zusätzlicher Messung wechseln. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „Wärmepumpe im Hauswert“ prüfen.'
        )
    elif (
        home_wp_split_mode == "auto"
        and _has_user_value(cfg, "wurzelzaehler")
        and safe_float(cfg.get("wurzelzaehler"), -1.0) == 0.0
    ):
        home_wp_split_severity = "info"
        home_wp_split_message += (
            ' Im Config-Editor unter „Speicher & Konfiguration“ bei „Wärmepumpe im Hauswert“ die Option „Wärmepumpe steckt im Hauswert“ wählen, wenn der Hausverbrauch die Wärmepumpe bereits enthält und kein separater Wurzelzähler vorhanden ist.'
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

    backup = backup_hold.config_contract(cfg)
    backup_labels = ('Notstrom-Vorhaltung', 'Notstrom-Vorhalt am Abend (kWh Speicherinhalt)',
        'Bezugszeit (Ortszeit)', 'Versorgung mindestens bis (bestätigte Ortszeit)', 'Reserve-Netzladen (bei Halten)', 'Höchster Bezugspreis (ct/kWh)')
    for key, label in zip(backup_hold.KEYS, backup_labels):
        effective = backup['values'][key]
        if key == 'storage_backup_hold_enable':
            effective = (backup['mode'] if backup['mode'] == 2 else backup['enabled']) if backup['valid'] else False
        storage[key] = _entry(key=key, label=label, unit='kWh' if key.endswith('energy_kwh') else ('ct/kWh' if key.endswith('limit_ct') else '-'),
            configured=cfg.get(key), live_value=None, live_key=None, effective=effective,
            source='user' if _has_user_value(cfg, key) else 'default',
            severity='info' if backup['valid'] else 'warning',
            message=('Halten; nachts mehr Netzbezug möglich. Reserve-Netzladen kostet den Bezugspreis.' if backup['mode'] == 2 else 'Nur Beobachtung; Verluste und Notstromfähigkeit der Anlage prüft E3DC-Control nicht.') if backup['valid'] else f'Die Notstrom-Vorhaltung kann nicht verwendet werden: {backup_hold.reason_text(";".join(backup["reasons"]))}. Im Config-Editor unter „Speicher & Konfiguration“ das Feld „{label}“ und die übrigen Angaben zur Notstrom-Vorhaltung prüfen.')

    storage["storage_central_pv_split_enable"] = _entry(
        key="storage_central_pv_split_enable",
        label="Zentrale PV-Aufteilung Akku/Wallbox (experimentell)", unit="-",
        configured=cfg.get("storage_central_pv_split_enable"), live_value=None, live_key=None,
        effective=_is_enabled(cfg, "storage_central_pv_split_enable"), source="user" if _has_user_value(cfg, "storage_central_pv_split_enable") else "default",
        severity="info", message='Die experimentelle gemeinsame PV-Aufteilung richtet den Speicheranteil nach der Ladekurve aus. Ladestrom, Phasenwechsel und Anhalten steuert weiterhin die Wallboxregelung.',
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
        f"Die Signalunterbrechung für den Phasenwechsel dauert {phase_cp_effective:g} s; der nächste Wechsel ist weiterhin frühestens nach 480 s möglich."
    )
    if phase_cp_legacy:
        phase_cp_severity = "warning"
        phase_cp_message = (
            f'Der alte Unterbrechungswert von 480 s wird auf 5 s umgestellt; zwischen Phasenwechseln gelten weiterhin mindestens 480 s. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Kurzer CP-Impuls (s)“ prüfen.'
        )
    elif phase_cp_configured is not None and not 2.0 <= phase_cp_configured <= 30.0:
        phase_cp_severity = "warning"
        phase_cp_message = (
            f'Die Signalunterbrechung liegt außerhalb 2 bis 30 s; wirksam sind {phase_cp_effective:g} s. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Kurzer CP-Impuls (s)“ prüfen.'
        )
    wallbox["openwb_pro_phase_cp_interrupt_duration_s"] = _entry(
        key="openwb_pro_phase_cp_interrupt_duration_s",
        label='Kurzer CP-Impuls (s)',
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
        f"Mehr PV-Ladestrom oder ein Wiederanlauf nach einer Leistungsreduzierung wird erst nach {pv_only_release_hold_effective:g} s durchgehend ausreichender PV-Leistung freigegeben. Bei fehlender Leistungszuteilung wird sofort wieder angehalten."
    )
    if pv_only_release_hold_configured is not None and pv_only_release_hold_configured < 30.0:
        pv_only_release_hold_severity = "warning"
        pv_only_release_hold_message = (
            f'Die Wartezeit vor erneuter PV-Ladefreigabe liegt unter 30 s und wird auf 30 s angehoben, um häufiges Anhalten und Anlaufen zu vermeiden. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „PV-only Halt-Freigabe (s)“ prüfen.'
        )
    wallbox["wb_pv_only_release_hold_s"] = _entry(
        key="wb_pv_only_release_hold_s",
        label='Wartezeit vor erneuter PV-Ladefreigabe',
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
        f"Kurze Datenlücken werden für {pv_only_stale_guard_effective:g} s überbrückt; die bisherige PV-Ladefreigabe bleibt währenddessen erhalten."
    )
    if pv_only_stale_guard_configured is not None and not 10.0 <= pv_only_stale_guard_configured <= 300.0:
        pv_only_stale_guard_severity = "warning"
        pv_only_stale_guard_message = (
            f'Die Überbrückungszeit bei kurz fehlenden Daten liegt außerhalb 10 bis 300 s; wirksam sind {pv_only_stale_guard_effective:g} s. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „PV-only Halt-Gnadenfrist (s)“ prüfen.'
        )
    wallbox["wb_pv_only_hold_stale_guard_s"] = _entry(
        key="wb_pv_only_hold_stale_guard_s",
        label='Überbrückung kurzer Datenlücken bei PV-Ladefreigabe',
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
        f"Neuer Netzbezug innerhalb der Speicherleistung wird {settle_effective:g} s abgewartet, damit der Speicher Lastsprünge ausgleichen kann. Hausanschlussgrenzen, ausgeschaltetes Laden und höherer Netzbezug wirken sofort."
    )
    if settle_configured is not None and not 0.0 <= settle_configured <= 30.0:
        settle_severity = "warning"
        settle_message = (
            f'Die Wartezeit bei neuem Netzbezug liegt außerhalb 0 bis 30 s; wirksam sind {settle_effective:g} s. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Einschwingfrist Netzbezug (s)“ prüfen.'
        )
    elif settle_configured is not None and settle_effective <= 0.0:
        settle_message = (
            'Die Wartezeit bei neuem Netzbezug ist aus; die Wallbox reduziert den Ladestrom sofort. Kurze Lastspitzen können dadurch wiederholte Stromänderungen auslösen.'
        )
    elif settle_configured is not None and settle_effective < 5.0:
        settle_severity = "warning"
        settle_message = (
            f'Die Wartezeit von {settle_effective:g} s ist kürzer als die Speicherreaktion von etwa 5 bis 8 s; die Wallbox kann dadurch kurzzeitige Lastspitzen unnötig ausregeln. Im Config-Editor unter „Wallbox & Fahrzeug“ das Feld „Einschwingfrist Netzbezug (s)“ prüfen.'
        )
    wallbox["wb_grid_import_settle_s"] = _entry(
        key="wb_grid_import_settle_s",
        label='Einschwingfrist Netzbezug (s)',
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
    if _is_enabled(cfg, "luxtronik") or (safe_float(cfg.get("wp_type"), -1.0) >= 0 and safe_float(cfg.get("wp_type"), -1.0) != 7):
        active_consumers.append("heatpump")
    if _is_enabled(cfg, "wb_native_enable"):
        active_consumers.append("wallbox")
    if _is_enabled(cfg, "heizstab") or _has_user_value(cfg, "heizstab_ip") or _has_user_value(cfg, "shelly_heiz_ip"):
        active_consumers.append("heater")
    priority_severity = "ok"
    priority_message = "Wärmepumpe, Wallbox und Heizstab sind jeweils einmal in der Reihenfolge für die Leistungsverteilung enthalten."
    if sorted(order) != sorted(valid_priority) or len(set(order)) != len(order):
        priority_severity = "warning"
        priority_message = f'Die Verbraucherreihenfolge ist unvollständig oder enthält doppelte Einträge; Wärmepumpe, Wallbox und Heizstab müssen jeweils einmal vorkommen. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „PV-/Kurven-Budget zuerst an“ prüfen.'
    elif "heatpump" in active_consumers and order[0] != "heatpump":
        priority_severity = "warning"
        priority_message = f'Die aktive Wärmepumpe steht nicht an erster Stelle; ein Prioritätswechsel kann ihren Betrieb beeinflussen. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „PV-/Kurven-Budget zuerst an“ prüfen.'
    consumer["consumer_priority_order"] = _entry(
        key="consumer_priority_order",
        label='PV-/Kurven-Budget zuerst an',
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
        label='WP-Nachlauf bei Prioritätswechsel (s)',
        unit="s",
        configured=wp_runon if _has_user_value(cfg, "consumer_priority_wp_runon_s") else None,
        live_value=None,
        live_key=None,
        effective=wp_runon,
        source="user" if _has_user_value(cfg, "consumer_priority_wp_runon_s") else "default",
        severity="warning" if wp_runon < 60.0 or wp_runon > 3600.0 else "ok",
        message=(
            f'Die Nachlaufzeit von {wp_runon:g} s liegt außerhalb der empfohlenen 60 bis 3600 s und beeinflusst den Prioritätswechsel. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „WP-Nachlauf bei Prioritätswechsel (s)“ prüfen.' if wp_runon < 60.0 or wp_runon > 3600.0 else 'Die eingestellte Nachlaufzeit hält die Wärmepumpe bei einem Wechsel der Verbraucherpriorität vorübergehend weiter aktiv.'
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
        label='Luxtronik Absenk-Soll (°C)',
        unit="°C",
        configured=luxtronik_pause_configured,
        live_value=None,
        live_key=None,
        effective=luxtronik_pause_effective,
        source="user" if luxtronik_pause_configured is not None else "default",
        severity="ok" if luxtronik_pause_in_range else "warning",
        message=(
            'Der Absenk-Sollwert liegt zwischen 15 und 22 °C und begrenzt die zusätzliche Wärmeanforderung über den externen Sollwert.' if luxtronik_pause_in_range else f'Der Absenk-Sollwert liegt außerhalb 15 bis 22 °C; wirksam sind {luxtronik_pause_effective:g} °C. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Luxtronik Absenk-Soll (°C)“ prüfen.'
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
            'Die 15-Minuten-Grenze muss mindestens 1.000 W betragen.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Maximaler 15-Minuten-Netzbezug“ prüfen.'
        ),
        "peak_shaving_reserve_soc_pct": (
            f'Die Lastspitzen-Schwelle muss über der wirksamen Notstromreserve von {reserve_effective:.1f} % liegen und darf 100 % nicht überschreiten.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Speicherschwelle für Lastspitzen“ prüfen.'
        ),
        "peak_shaving_max_discharge_w": (
            'Die Entladeobergrenze muss mindestens 0 W betragen.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Maximale Entladeleistung“ prüfen.'
        ),
        "peak_shaving_recharge_max_w": (
            'Die Netz-Nachladegrenze muss mindestens 0 W betragen.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Maximale Netz-Nachladung“ prüfen.'
        ),
        "peak_shaving_control_margin_w": (
            'Der Sicherheitsabstand muss mindestens 0 W betragen und mindestens 300 W Regelraum unter der Bezugsgrenze lassen.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Sicherheitsabstand“ prüfen.'
        ),
        "peak_shaving_hysteresis_w": (
            'Die Leistungshysterese muss strikt größer als die kleinste 300-W-Leistungsgrenze sein; empfohlen sind mindestens 600 W.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Leistungshysterese“ prüfen.'
        ),
        "peak_shaving_soc_hysteresis_pct": (
            'Die SoC-Hysterese muss zwischen 0,1 und 5,0 Prozentpunkten liegen.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „SoC-Hysterese“ prüfen.'
        ),
        "peak_shaving_max_sample_gap_s": (
            'Die größte Messlücke muss zwischen 2 und 60 Sekunden liegen.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Maximale Messlücke“ prüfen.'
        ),
        "peak_shaving_release_debounce_s": (
            'Die Freigabe-Entprellung muss zwischen 5 und 120 Sekunden liegen.' + ' Im Config-Editor unter „Tarif & Strompreise“ das Feld „Freigabe-Entprellung“ prüfen.'
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
                ('Die Eingabe ist keine gültige Zahl; die Lastspitzenbegrenzung bleibt aus. ' + peak_messages[key].split('. ', 1)[1] if not typed else f'Bei {value:g} {unit} bleibt die Lastspitzenbegrenzung aus; ' + peak_messages[key][0].lower() + peak_messages[key][1:]) if relevant and not valid else 'Die Einstellung ist verwendbar; die Lastspitzenbegrenzung greift nur mit lückenlosen Netzbezugsdaten seit Beginn der aktuellen Viertelstunde ein.' if peak_enabled else 'Lastspitzenbegrenzung ist aus; der Wert wirkt nicht.'
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
            'Die Lastspitzenbegrenzung ist eingeschaltet, aber ihre Einstellungen sind ungültig; sie übernimmt keine Speicherentscheidung. Im Config-Editor unter „Tarif & Strompreise“ die Angaben unter „Lastspitzenbegrenzung am Netzanschluss“ prüfen.' if peak_enabled and not peak_contract_valid else 'Die Lastspitzenbegrenzung ist an und begrenzt den Viertelstunden-Netzbezug im automatischen Speicherbetrieb über eine vorübergehende Entladegrenze. Sie fordert weder Entladung noch Einspeisung an.' if peak_enabled else 'Die Lastspitzenbegrenzung ist aus und beeinflusst den Speicher nicht.'
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
            ('Die Speicherschwelle ist nicht als Zahl verwendbar; die Lastspitzenbegrenzung bleibt aus. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Speicherschwelle für Lastspitzen“ prüfen.' if not peak_values['peak_shaving_reserve_soc_pct'][1] else f'Die Speicherschwelle von {peak_reserve_soc:g} % muss über der Notstromreserve von {reserve_effective:g} % und höchstens bei 100 % liegen; die Lastspitzenbegrenzung bleibt aus. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Speicherschwelle für Lastspitzen“ prüfen.') if peak_enabled and not peak_validation['peak_shaving_reserve_soc_pct'] else (f'Von der eingestellten {peak_reserve_soc:.1f}-%-Schwelle bis zur wirksamen Notstromreserve von {reserve_effective:.1f} % sind {usable_peak_pct:.1f} Prozentpunkte ausschließlich als Lastspitzenpuffer reserviert. Bei einer echten Lastspitze darf dieser Puffer bis zur physischen Reserve genutzt werden.')
        ),
    )
    price["strompreis_cheap"] = _entry(
        key="strompreis_cheap",
        label='Günstig (LT)',
        unit="ct/kWh",
        configured=cheap if _has_user_value(cfg, "strompreis_cheap") else None,
        live_value=None,
        live_key=None,
        effective=cheap,
        source="user" if _has_user_value(cfg, "strompreis_cheap") else "default",
        severity="warning" if octopus_bad_order else "ok",
        message=(
            f'Die Tarifpreise günstig {cheap:g}, normal {basis:g} und teuer {uht:g} ct/kWh sind nicht aufsteigend; die Zuordnung günstiger und teurer Zeiten passt dadurch nicht. Im Config-Editor unter „Tarif & Strompreise“ die Felder „Günstig (LT)“, „Normal (HT)“ und „Teuer (UHT)“ prüfen.' if octopus_bad_order else 'Der eingestellte Tarifpreis ist für die Preisbewertung verwendbar.'
        ),
    )
    price["strompreis_basis"] = _entry(
        key="strompreis_basis",
        label='Normalpreis oder Arbeitspreis',
        unit="ct/kWh",
        configured=basis if _has_user_value(cfg, "strompreis_basis") else None,
        live_value=None,
        live_key=None,
        effective=basis,
        source="user" if _has_user_value(cfg, "strompreis_basis") else "default",
        severity="warning" if basis <= 0.0 or octopus_bad_order else "ok",
        message=f'Der Normalpreis von {basis:g} ct/kWh ist nicht positiv; die Preisbewertung kann dadurch unplausibel werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Normal (HT)“ beziehungsweise „Arbeitspreis“ prüfen.' if basis <= 0.0 else f'Die Tarifpreise günstig {cheap:g}, normal {basis:g} und teuer {uht:g} ct/kWh sind nicht aufsteigend; die Preisbewertung passt dadurch nicht. Im Config-Editor unter „Tarif & Strompreise“ die Felder „Günstig (LT)“, „Normal (HT)“ und „Teuer (UHT)“ prüfen.' if octopus_bad_order else 'Der eingestellte Tarifpreis ist für die Preisbewertung verwendbar.',
    )
    price["strompreis_uht"] = _entry(
        key="strompreis_uht",
        label='Teuer (UHT)',
        unit="ct/kWh",
        configured=uht if _has_user_value(cfg, "strompreis_uht") else None,
        live_value=None,
        live_key=None,
        effective=uht,
        source="user" if _has_user_value(cfg, "strompreis_uht") else "default",
        severity="warning" if octopus_bad_order else "ok",
        message=(
            f'Die Tarifpreise günstig {cheap:g}, normal {basis:g} und teuer {uht:g} ct/kWh sind nicht aufsteigend; die Zuordnung günstiger und teurer Zeiten passt dadurch nicht. Im Config-Editor unter „Tarif & Strompreise“ die Felder „Günstig (LT)“, „Normal (HT)“ und „Teuer (UHT)“ prüfen.' if octopus_bad_order else 'Der eingestellte Tarifpreis ist für die Preisbewertung verwendbar.'
        ),
    )
    fixed_stages = recurring_tariff_stages(cfg)
    if fixed_stages is not None and not fixed_stages["valid"]:
        if tariff == "octopus_heat":
            field_labels = {
                "strompreis_basis": "Normal (HT)",
                "strompreis_cheap": "Günstig (LT)",
                "strompreis_uht": "Teuer (UHT)",
            }
            for key, label in field_labels.items():
                raw = cfg.get(key)
                try:
                    value = float(str(raw).strip().replace(",", "."))
                except (TypeError, ValueError):
                    value = float("nan")
                if math.isfinite(value):
                    continue
                reason = "Pflichtfeld nicht gesetzt" if raw is None or str(raw).strip() == "" else "Preis ist keine endliche Zahl"
                price[key] = _entry(
                    key=key, label=label, unit="ct/kWh", configured=raw,
                    live_value=None, live_key=None, effective=None, source="invalid",
                    severity="warning",
                    message=f'Die Octopus-Tarifstufen sind unvollständig oder ungültig; preisabhängiges Netzladen erhält keine Freigabe. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.',
                )
        else:
            reason = {
                "fixed_tariff_stages_missing": "Tagesplan nicht gesetzt",
                "fixed_tariff_stages_conflicting": "widersprüchliche Preise für dieselbe Startzeit",
            }.get(fixed_stages["reason"], "ungültige Startzeit oder Preisangabe; nur Startzeit und Zahl ohne Freitext oder ct-Suffix zulässig")
            price["strompreis_spezial"] = _entry(
                key="strompreis_spezial", label="Spezialtarif-Zeitpreise", unit="ct/kWh",
                configured=cfg.get("strompreis_spezial"), live_value=None, live_key=None,
                effective=None, source="invalid", severity="warning",
                message=f'Die Zeitpreise des Spezialtarifs sind unvollständig oder ungültig; preisabhängiges Netzladen erhält keine Freigabe. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Spezialtarif-Zeitpreise“ prüfen.',
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
        label='Zusätzliches Wärmepumpenheizen bei Negativpreisen (experimentell)',
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
            'Aus: Der experimentelle Wärmepumpen-Netzboost ist nicht freigegeben.' if not heat_price_boost_requested else 'Der experimentelle Testbetrieb ist freigegeben; eine Luxtronik darf bei negativem Abrechnungspreis, Wärmebedarf und aktueller Speicherzusage zusätzlich heizen, eine laufende Heizung ist damit nicht bestätigt. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ den Block „Negativpreis-Netzboost“ nur für Tests eingeschaltet lassen.' if heat_price_pilot_ready else 'Der experimentelle Testbetrieb ist angefordert, aber Voraussetzungen fehlen; zusätzliche Wärmepumpenheizung bei Negativpreis bleibt aus. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ den Block „Negativpreis-Netzboost“ sowie Luxtronik, Automatik, gemeinsame Wärmeplanung, Börsentarif und beide Negativpreisfreigaben prüfen; zusätzlich ist eine aktuelle Speicherzusage nötig.'
        ),
    )
    price["heat_price_boost_scope"] = _entry(
        key="heat_price_boost_scope",
        label='Wärmeziel',
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
            'Das Wärmeziel ist ungültig; zusätzliches Heizen nach Tarif oder Negativpreis bleibt aus. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Wärmeziel“ prüfen.' if not heat_price_scope_valid else 'Das gewählte Wärmeziel ist verwendbar; ohne vollständige Freigabe wird kein Heizbefehl ausgegeben.'
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
        message='Die früher gespeicherten Zeitfenster beeinflussen zusätzliches Wärmepumpenheizen bei Negativpreisen nicht.',
    )
    for key, label, value in (
        ("price_limit", 'Preis-Limit', price_limit),
        ("price_pause_limit", 'Sperr-Limit', price_pause),
        ("cheap_grid_price_limit_ct", 'Preisgrenze (Legacy)', cheap_grid_limit),
    ):
        enabled = key != "cheap_grid_price_limit_ct" or cheap_grid_enabled
        severity = "ok"
        message = f"Das eingestellte Preislimit von {value:g} ct/kWh wird bei der Wärmeplanung berücksichtigt."
        if key != "cheap_grid_price_limit_ct" and enabled and value < 0.0:
            severity = "warning"
            message = f'Das Preislimit von {value:g} ct/kWh ist negativ; es liegt unter dem erwarteten Bereich. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „{label}“ prüfen.'
        elif key in {"price_limit", "price_pause_limit"} and price_order_warning:
            severity = "warning"
            message = f'Das Heizpreislimit von {price_limit:g} ct/kWh liegt über dem Sperrpreis von {price_pause:g} ct/kWh; die Preisgrenzen widersprechen einander. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „{label}“ prüfen.'
        elif key == "cheap_grid_price_limit_ct" and cheap_grid_enabled and not cheap_grid_supported:
            severity = "warning"
            message = f'Der gewählte Tarif liefert keine echten Börsenpreise; die Negativpreisfreigabe bleibt ohne Wirkung. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "cheap_grid_price_limit_ct" and enabled and value > 0.0:
            severity = "warning"
            message = f'Das positive Preislimit von {value:g} ct/kWh erlaubt keine Ladung zu günstigen positiven Preisen; zusätzliche Ladung bleibt auf negative Abrechnungspreise begrenzt. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
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

    # Speicherparameter sind profilunabhängig und verändern keine Nutzereingaben.
    for key, label, unit, default, fallback, low, high in (
        ("market_degradation_ct_per_kwh", "Akkuverschleiß", "ct/kWh", 4.0,
         "direct_marketing_degradation_ct_per_kwh", 0.0, None),
        ("market_roundtrip_efficiency_pct", "Speicher-Wirkungsgrad", "%", 85.0,
         "direct_marketing_roundtrip_efficiency_pct", 1.0, 100.0),
        ("market_grid_charge_min_job_wh", "Mindestmenge je Netzladeauftrag", "Wh", 0.0,
         None, 0.0, None),
        ("market_grid_charge_min_job_pct", "Kapazitätsanteil der Mindestmenge", "%", 5.0,
         None, 0.0, 100.0),
    ):
        selected = key if _has_user_value(cfg, key) else (
            fallback if fallback and _has_user_value(cfg, fallback) else None
        )
        value, finite = _configured_finite_number(cfg, selected, default) if selected else (default, True)
        effective = max(low, safe_float(value, default))
        if high is not None:
            effective = min(high, effective)
        source = "user" if selected == key else ("legacy_direct_marketing" if selected else "default")
        origin = "Eigener Wert" if selected == key else ("Direktvermarktung" if selected else "Standard")
        severity = "ok"
        message = "Die Planung verwendet %s mit %s %s; dieser Wert bleibt bei einem Ladeprofilwechsel erhalten." % (origin, _fmt_de(effective, 2), unit)
        if not finite:
            severity = "warning"
            if not math.isfinite(value):
                effective = None
                message = f'Die Eingabe ist keine endliche Zahl; ein wirksamer Wert steht nicht zur Verfügung. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            else:
                message = f'Die Eingabe ist unlesbar; die Planung verwendet {default:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif value < low or (high is not None and value > high):
            severity = "warning"
            message = f'Die Eingabe liegt außerhalb des gültigen Bereichs; die Planung verwendet {effective:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "market_roundtrip_efficiency_pct" and value < 50.0:
            severity = "warning"
            message = f'Der Speicherwirkungsgrad von {value:g} % ist sehr niedrig; Netzladen lohnt erst bei großem späterem Preisabstand. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "market_degradation_ct_per_kwh" and value > 20.0:
            severity = "warning"
            message = f'Der Verschleißaufschlag von {value:g} ct/kWh ist hoch; wirtschaftliches Netzladen lohnt sich selten. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "market_grid_charge_min_job_wh" and value > capacity_effective * 1000.0:
            severity = "warning"
            message = f'Die Mindestlademenge von {value:g} Wh ist größer als die Speicherkapazität; kleinere Netzladungen werden voraussichtlich ganz unterdrückt. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        if key in ("market_grid_charge_min_job_wh", "market_grid_charge_min_job_pct"):
            message += '' if severity == 'warning' else ' Die Mindestnetzenergie je Ladevorgang ist der größere Betrag aus fester Energiemenge und Kapazitätsanteil (standardmäßig 5 %); nur zwei Nullwerte heben die Mindestmenge auf.'
        price[key] = _entry(
            key=key, label=label, unit=unit,
            configured=cfg.get(key) if _has_user_value(cfg, key) else None,
            live_value=None, live_key=None, effective=effective, source=source,
            severity=severity, message=message,
        )

    # Ladeprofil und Komfort-Preislimit bewerten (vor Marge/Sicherheit).
    price["market_charge_profile"] = _entry(
        key="market_charge_profile",
        label='Ladeprofil',
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
            'Das Ladeprofil ist unbekannt; verwendet wird „Wirtschaftlich“. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Ladeprofil“ prüfen.' if market_profile_unknown else 'Profil %s: Marge %s %%, Sicherheitskorrektur %s ct/kWh%s.' % (_market_profile_label(market_profile_name), _fmt_de(safe_float(market_profile.get('min_margin_pct'), 0.0), 1), _fmt_de(safe_float(market_profile.get('safety_ct'), 0.0), 2), ', Netzladen im Preisfenster bis zum Zielstand am Fensterende (PV zuerst)' if market_profile_comfort else '')
        ),
    )
    if market_profile_comfort:
        limit_severity = "ok"
        if market_price_limit > 0.0:
            limit_message = 'Das Komfortprofil erlaubt Netzladen in Zeitabschnitten bis %s ct/kWh.' % _fmt_de(market_price_limit, 1)
            if tariff in ("octopus_heat", "special", "spezial", "special_tariff"):
                # Spezialtarif: Vergleich mit den Preisen des Tagesplans; der Arbeitspreis ist dort
                # ausgeblendet. Ein ungültiger Plan hat oben schon eine eigene Warnung.
                if tariff == "octopus_heat":
                    tariff_prices = [cheap, basis, uht]
                elif fixed_stages is not None and fixed_stages.get("valid"):
                    tariff_prices = list(fixed_stages.get("prices") or [])
                else:
                    tariff_prices = []
                    limit_severity = "info"
                    limit_message = "Preisfenster gilt erst mit gültigem Tagesplan (Spezialtarif-Zeitpreise)."
                if tariff_prices and market_price_limit + 0.001 < min(tariff_prices):
                    limit_severity = "warning"
                    limit_message = f'Das Komfortprofil lädt nie, weil das Limit von {market_price_limit:g} ct/kWh unter dem günstigsten Tarifpreis liegt. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Komfort-Preislimit“ prüfen.'
                elif tariff == "octopus_heat" and market_price_limit + 0.001 >= basis:
                    limit_severity = "warning"
                    limit_message = f'Das Limit von {market_price_limit:g} ct/kWh erlaubt im Komfortprofil auch Laden zum Normalpreis. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Komfort-Preislimit“ prüfen.'
                elif tariff != "octopus_heat" and tariff_prices and market_price_limit + 0.001 >= max(tariff_prices):
                    limit_severity = "warning"
                    limit_message = f'Das Limit von {market_price_limit:g} ct/kWh erlaubt im Komfortprofil Laden zu allen Tarifzeiten. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Komfort-Preislimit“ prüfen.'
            else:
                limit_message = 'Das Preislimit beträgt %s ct/kWh; ohne eigenen Wert gilt der Mittelpreis der verfügbaren Planpreise.' % _fmt_de(market_price_limit, 1)
        else:
            limit_message = 'Ohne eigenen Wert wird das Preislimit je Plan aus dem mittleren Tarifpreis oder den verfügbaren Planpreisen bestimmt. Bei Octopus Heat zählen 8 günstige, 13 normale und 3 teure Stunden.'
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
            price["market_charge_profile"]["message"] = f'Das Komfortprofil ist gewählt, aber Speicher-Netzladen ist aus; das Profil bewirkt keine Netzladung. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Speicher-Netzladen“ prüfen.'
    for key, label, value, unit, low, high in (
        ("market_min_margin_pct", "Preis-Mindestmarge", market_min_margin, "%", 0.0, None),
        ("market_safety_correction_ct_per_kwh", "Preis-Sicherheitskorrektur", market_safety_correction, "ct/kWh", -10.0, 50.0),
        ("market_autarky_low_soc_pct", 'Low-SOC-Ausnahme', market_autarky_low_soc, "%", 0.0, 100.0),
        ("market_autarky_horizon_buffer_wh", 'Autarkie-Puffer', market_autarky_buffer_wh, "Wh", 0.0, None),
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
        message = f"Die Preisplanung verwendet für diese Einstellung {value:g} {unit}."
        if low is not None and value < low:
            severity = "warning"
            message = f'Der Wert von {value:g} {unit} liegt unter der Untergrenze von {low:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif high is not None and value > high:
            severity = "warning"
            message = f'Der Wert von {value:g} {unit} liegt über der Obergrenze von {high:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        # Profilbewusst – bei economic/balanced/comfort setzt das Profil Marge und
        # Sicherheitskorrektur; abweichende Schlüsselwerte werden nicht verwendet (Warnung, kein Fehler).
        elif key == "market_min_margin_pct" and not market_profile_custom and abs(value - safe_float(market_profile.get("min_margin_pct"), 10.0)) > 0.05:
            severity = "warning"
            message = (
                f'Die eingetragene Mindestmarge von {value:g} % wird durch das Ladeprofil ersetzt; wirksam sind {safe_float(market_profile.get("min_margin_pct"),10):g} %. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            )
        elif key == "market_safety_correction_ct_per_kwh" and not market_profile_custom and abs(value) > 0.005:
            severity = "warning"
            message = (
                f'Die eingetragene Preiskorrektur von {value:g} ct/kWh wird durch das Ladeprofil ersetzt; wirksam ist 0 ct/kWh. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            )
        elif key == "market_min_margin_pct" and market_profile_custom and value < 10.0:
            severity = "warning"
            message = f'Die Mindestmarge von {value:g} % liegt unter 10 %; der Speicher lädt schon bei kleineren erwarteten Preisvorteilen aus dem Netz. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "market_safety_correction_ct_per_kwh" and market_profile_custom and value < 0.0:
            message = 'Die negative Preiskorrektur lässt kleinere Preisvorteile für Netzladen genügen; die eingestellte Mindestmarge gilt weiterhin.'
        elif key == "market_autarky_low_soc_pct" and value < 15.0:
            severity = "warning"
            message = f'Die Ausnahmegrenze von {value:g} % liegt unter 15 %; Netzladen wird bei niedrigem Speicherstand erst sehr spät wieder zugelassen. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "market_autarky_horizon_buffer_wh" and value > 5000.0:
            severity = "warning"
            message = f'Der zusätzliche Energiepuffer von {value:g} Wh ist sehr hoch; Netzladen kann trotz ausreichender Tagesprognose erlaubt werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        price[key] = _entry(
            key=key,
            label={'market_autarky_low_soc_pct': 'Ausnahme bei niedrigem Speicherstand'}.get(key, label),
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
            'Bei ausreichender PV-Prognose entfallen normales preisabhängiges Netzladen und Speicherhalten. Eine ältere Einstellung kann diese feste Regel nicht abschalten.'
            if market_autarky_first
            else 'Das Komfortprofil nutzt zuerst PV-Energie und lädt nur die bis zum Fensterende fehlende Energie aus dem Netz. Eine ältere Einstellung ändert dieses feste Verhalten nicht.'
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
                label={'market_heatpump_enable': 'Alte Freigabe für preisabhängige Wärmepumpenheizung'}.get(key, label),
                unit="-",
                configured=configured,
                live_value=None,
                live_key=None,
                effective=False,
                source="legacy_ignored" if _has_user_value(cfg, key) else "default",
                severity="warning" if user_enabled else "ok",
                message=(
                    'Die alte Wärmepumpenfreigabe wird nicht mehr ausgewertet und bewirkt keine preisabhängige Wärmepumpensteuerung. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ den Abschnitt „Luxtronik: PV-Automatik und Wolkenüberbrückung“ oder für Negativpreise das Feld „Experimentellen Netzboost aktivieren“ verwenden.' if user_enabled else 'Die alte Freigabe für preisabhängiges Wärmepumpenheizen ist aus und wird nicht mehr ausgewertet.'
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
            label={'market_heatpump_enable': 'Alte Freigabe für preisabhängige Wärmepumpenheizung'}.get(key, label),
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
                'Die Funktion ist eingeschaltet; der tatsächliche Einsatz hängt von Bedarf und Strompreis ab.'
                if effective and key.startswith("market_battery_")
                else ('Die ausgewählte preisabhängige Funktion ist eingeschaltet.' if effective else 'Die ausgewählte preisabhängige Funktion ist ausgeschaltet.')
            ),
        )
    if _has_user_value(cfg, "market_battery_enable"):
        price["market_battery_enable"] = _entry(
            key="market_battery_enable",
            label='Alte Freigabe für preisabhängiges Speicherladen',
            unit="-",
            configured=cfg.get("market_battery_enable"),
            live_value=None,
            live_key=None,
            effective=False,
            source="legacy_ignored",
            severity="warning",
            message='Die alte Einstellung wird nicht mehr ausgewertet; sie schaltet weder Netzladen noch Speicherhalten ein. Im Config-Editor unter „Tarif & Strompreise“ die Nachfolgefelder „Speicher-Netzladen“ und „Speicher halten“ verwenden.',
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
        label='Gemeinsame Wärmeplanung (experimentell)',
        unit="-",
        configured=cfg.get("heat_policy_runtime_enable") if _has_user_value(cfg, "heat_policy_runtime_enable") else None,
        live_value=None,
        live_key=None,
        effective=heat_policy_runtime,
        source="user" if _has_user_value(cfg, "heat_policy_runtime_enable") else "default",
        severity="warning" if heat_policy_runtime else "ok",
        message=(
            'Die experimentelle gemeinsame Wärmeplanung ist eingeschaltet und darf Wärmepumpenstarts und zusätzliches Heizstabheizen mit Netzstrom begrenzen; sie aktiviert allein kein preisabhängiges Wärmepumpenheizen. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ im Block „Wärme in die Gesamtplanung einbeziehen“ das Feld „Aktiv nutzen“ nur zum Testen eingeschaltet lassen.' if heat_policy_runtime else 'Die experimentelle gemeinsame Wärmeplanung wertet nur zur Diagnose aus; die bisherige Regelung steuert weiter. Zusätzliches Wärmepumpenheizen nach Strompreis bleibt damit aus.'
        ),
    )
    price["ems_budget_runtime_enable"] = _entry(
        key="ems_budget_runtime_enable",
        label='Gemeinsame Verbraucherleistung (experimentell)',
        unit="-",
        configured=cfg.get("ems_budget_runtime_enable") if _has_user_value(cfg, "ems_budget_runtime_enable") else None,
        live_value=None,
        live_key=None,
        effective=ems_budget_runtime,
        source="user" if _has_user_value(cfg, "ems_budget_runtime_enable") else "default",
        severity="warning" if ems_budget_runtime else "ok",
        message=(
            'Die experimentelle gemeinsame Leistungsverteilung ist eingeschaltet; zusätzliche Verbraucherleistung benötigt eine aktuelle bestätigte Gesamtplanung, bei Datenverlust entfällt diese zusätzliche Freigabe. Im Config-Editor unter „Tarif & Strompreise“ im Block „Gemeinsame Verbraucherbudgets“ das Feld „Aktiv nutzen“ nur zum Testen eingeschaltet lassen.' if ems_budget_runtime else 'Die experimentelle gemeinsame Leistungsverteilung wertet nur zur Diagnose aus; die bisherige Leistungsverteilung bleibt unverändert.'
        ),
    )
    price["heat_heater_grid_boost_enable"] = _entry(
        key="heat_heater_grid_boost_enable",
        label='Zusätzliches Heizstabheizen mit Netzstrom',
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_enable") if _has_user_value(cfg, "heat_heater_grid_boost_enable") else None,
        live_value=None,
        live_key=None,
        effective=heater_grid_boost,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_enable") else "default",
        severity="warning" if heater_grid_boost else "ok",
        message=(
            'Zusätzliches Heizstabheizen mit Netzstrom ist freigegeben; eine Kilowattstunde Strom liefert dabei nur etwa eine Kilowattstunde Wärme. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Heizstab-Netzboost prüfen“ nur bei bewusst gewünschten Kosten und passenden Preis-, Bedarfs- und Temperaturgrenzen eingeschaltet lassen.' if heater_grid_boost else 'Zusätzliches Heizstabheizen mit Netzstrom ist ausgeschaltet.'
        ),
    )
    price["heat_heater_grid_boost_ack"] = _entry(
        key="heat_heater_grid_boost_ack",
        label='COP=1-Warnung verstanden',
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_ack") if _has_user_value(cfg, "heat_heater_grid_boost_ack") else None,
        live_value=None,
        live_key=None,
        effective=heater_grid_ack,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_ack") else "default",
        severity="warning" if heater_grid_boost and not heater_grid_ack else "ok",
        message=(
            'Die Kostenwarnung ist nicht bestätigt; zusätzliches Heizstabheizen mit Netzstrom bleibt gesperrt. Im Config-Editor unter „Tarif & Strompreise“ das Feld „COP=1-Warnung verstanden“ nur nach Prüfung der Kosten bestätigen.' if heater_grid_boost and (not heater_grid_ack) else 'Die Kostenwarnung ist bestätigt; eine Kilowattstunde Heizstabstrom liefert etwa eine Kilowattstunde Wärme.' if heater_grid_ack else 'Zusätzliches Heizstabheizen mit Netzstrom ist aus; die Bestätigung der Kostenwarnung ist deshalb nicht erforderlich.'
        ),
    )
    price["heat_heater_grid_boost_requires_deficit"] = _entry(
        key="heat_heater_grid_boost_requires_deficit",
        label='Heizstab auf fehlenden Wärmebedarf begrenzen',
        unit="-",
        configured=cfg.get("heat_heater_grid_boost_requires_deficit") if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit") else None,
        live_value=None,
        live_key=None,
        effective=heater_requires_deficit,
        source="user" if _has_user_value(cfg, "heat_heater_grid_boost_requires_deficit") else "default",
        severity="ok" if heater_requires_deficit else "warning",
        message=(
            'Zusätzliches Heizstabheizen mit Netzstrom ist auf den vorhergesagten fehlenden Wärmebedarf begrenzt.' if heater_requires_deficit else 'Die Begrenzung auf den vorhergesagten fehlenden Wärmebedarf ist ausgeschaltet; der Heizstab kann dadurch mehr Netzstrom verbrauchen. Im Config-Editor unter „Tarif & Strompreise“ das Feld „nur Prognose-Defizit“ einschalten oder nur für bewusst gewünschte Sonderfälle ausgeschaltet lassen.'
        ),
    )
    for key, label, value, unit, low, high in (
        ("heat_heater_grid_boost_price_limit_ct", "Heizstab Preislimit", heater_price_limit, "ct/kWh", None, None),
        ("heat_heater_grid_boost_max_w", "Heizstab Netzlimit", heater_grid_max_w, "W", 0.0, None),
        ("heat_heater_min_temp_c", 'Boost ab unter', heater_min_temp, "°C", 0.0, 95.0),
        ("heat_heater_max_temp_c", 'Sperre ab', heater_max_temp, "°C", 0.0, 95.0),
        ("heat_wp_daily_kwh", "Fallback Wärmebedarf", heat_daily_fallback, "kWh/24h", 0.0, 120.0),
    ):
        severity = "ok"
        message = f"Die Wärmeplanung verwendet für diese Einstellung {value:g} {unit}."
        if low is not None and value < low:
            severity = "warning"
            message = f'Der Wert von {value:g} {unit} liegt unter der Untergrenze von {low:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif high is not None and value > high:
            severity = "warning"
            message = f'Der Wert von {value:g} {unit} liegt über der Obergrenze von {high:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "heat_heater_grid_boost_price_limit_ct" and heater_grid_boost and value > 0.0:
            severity = "warning"
            message = f'Das Heizstabpreislimit von {value:g} ct/kWh ist positiv; Netzstromwärme kann teurer als Wärmepumpe oder spätere PV-Wärme werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "heat_heater_grid_boost_max_w" and heater_grid_boost and value <= 0.0:
            severity = "warning"
            message = f'Zusätzliches Heizstabheizen mit Netzstrom ist an, aber 0 W Leistungsgrenze lassen keine Netzleistung zu. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        elif key == "heat_heater_max_temp_c" and heater_max_temp <= heater_min_temp:
            severity = "warning"
            message = f'Die Höchsttemperatur von {value:g} °C liegt nicht über der Mindesttemperatur; die Temperaturgrenzen widersprechen einander. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
        price[key] = _entry(
            key=key,
            label={'heat_wp_daily_kwh': 'Ersatzwert für den täglichen Wärmebedarf'}.get(key, label),
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
        label='Sehr günstig',
        unit="ct/kWh",
        configured=price_hard if _has_user_value(cfg, "price_hard_limit") else None,
        live_value=None,
        live_key=None,
        effective=price_hard,
        source="user" if _has_user_value(cfg, "price_hard_limit") else "default",
        severity="warning" if price_hard != -99.0 and price_hard > price_limit else "ok",
        message=(
            f'Das Zwangspreislimit von {price_hard:g} ct/kWh liegt über dem normalen Preislimit von {price_limit:g} ct/kWh; die Preisgrenzen widersprechen einander. Im Config-Editor unter „Smart Home & Verbrauchsprognose“ das Feld „Sehr günstig“ auf höchstens das normale Preislimit setzen oder mit -99 deaktivieren.' if price_hard != -99.0 and price_hard > price_limit else 'Die Preisgrenze für besonders günstiges Heizen passt zur normalen Preisgrenze oder ist mit -99 ausgeschaltet.'
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
        message="Direktvermarktung ist aktiv." if dm_enabled else 'Die Direktvermarktung ist ausgeschaltet und beeinflusst die Regelung nicht.',
    )
    price["direct_marketing_mode"] = _entry(
        key="direct_marketing_mode",
        label='Direktvermarktungsstrategie',
        unit="-",
        configured=cfg.get("direct_marketing_mode") if _has_user_value(cfg, "direct_marketing_mode") else None,
        live_value=None,
        live_key=None,
        effective=dm_mode,
        source="user" if _has_user_value(cfg, "direct_marketing_mode") else "default",
        severity="warning" if dm_enabled and dm_mode_warning else "ok",
        message='Der Direktvermarktungsmodus ist unbekannt; es gilt der vorsichtige Standardmodus. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Strategie“ prüfen.' if dm_enabled and dm_mode_warning else 'Die gewählte Strategie ist für die Direktvermarktung verwendbar.',
    )
    dm_profit_profile_severity = "ok"
    dm_profit_profile_message = "Das Standardprofil berücksichtigt Ladeverluste, Speicherverschleiß und die eingestellten Mindestgewinne."
    if dm_profit_profile_warning:
        dm_profit_profile_severity = "warning"
        dm_profit_profile_message = f'Das Wirtschaftlichkeitsprofil ist unbekannt; verwendet wird das Standardprofil. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Profitprofil“ prüfen.'
    elif dm_profit_profile == "aggressive":
        dm_profit_profile_severity = "warning" if dm_enabled and dm_export_enabled else "ok"
        dm_profit_profile_message = f'Das aggressive Profil akzeptiert kleinste Gewinne nach Ladeverlusten und kann zusätzliche Batteriezyklen verursachen. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Profitprofil“ prüfen.'
    elif dm_profit_profile == "expert":
        dm_profit_profile_severity = "warning" if dm_enabled else "ok"
        dm_profit_profile_message = f'Das Expertenprofil überspringt Wirtschaftlichkeitsprüfungen; Reserve- und Datengrenzen bleiben wirksam. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Profitprofil“ prüfen.'
    price["direct_marketing_profit_profile"] = _entry(
        key="direct_marketing_profit_profile",
        label='Profitprofil',
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
        label='Abrechnung',
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
            'Die gewählte Preisbasis ist nur zur Auswertung verfügbar; aktive Direktvermarktung benötigt die viertelstündlichen Preise des Vortagsmarkts. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Abrechnung“ prüfen.' if dm_enabled and (not dm_settlement_active_supported) else 'Die Direktvermarktung kann die viertelstündlichen Preise des Vortagsmarkts zur Regelung verwenden.'
        ),
    )
    price["direct_marketing_pv_store_enable"] = _entry(
        key="direct_marketing_pv_store_enable",
        label='PV-Speichern bei Direktvermarktung',
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
            'PV-Speichern bei Direktvermarktung ist aus und übernimmt keine Speicherladung.'
        ),
    )
    price["direct_marketing_eeg_enable"] = _entry(
        key="direct_marketing_eeg_enable",
        label='EEG-Anlage in der Direktvermarktung',
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
        label='Vergütungsquelle',
        unit="-",
        configured=dm_eeg_rate_source if _has_user_value(cfg, "direct_marketing_eeg_rate_source") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_rate_source,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_rate_source") else "default",
        severity="warning" if dm_eeg_enabled and (eeg_source_invalid or eeg_auto_date_outside or eeg_archive_date_missing or eeg_archive_date_outside) else "ok",
        message=(
            'Die Vergütungsquelle ist unbekannt; die EEG-Vergütung kann nicht zuverlässig bestimmt werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Vergütungsquelle“ und das Inbetriebnahmedatum prüfen.' if eeg_source_invalid else 'Das Inbetriebnahmedatum liegt außerhalb der aktuellen Vergütungstabelle vom 1. Februar bis 31. Juli 2026; diese Tabelle passt nicht zur Anlage. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Vergütungsquelle“ und das Inbetriebnahmedatum prüfen.' if eeg_auto_date_outside else 'Für die automatische Vergütung aus dem Archiv fehlt das Inbetriebnahmedatum; der passende Satz kann nicht bestimmt werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Vergütungsquelle“ und das Inbetriebnahmedatum prüfen.' if eeg_archive_date_missing else 'Das Inbetriebnahmedatum liegt außerhalb des eingebauten Archivs vom 1. Januar 2018 bis 31. Juli 2026; ein passender Vergütungssatz fehlt. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Vergütungsquelle“ und das Inbetriebnahmedatum prüfen.' if eeg_archive_date_outside else 'Die gewählte Vergütungsquelle ist für die EEG-Berechnung verwendbar.'
        ),
    )
    price["direct_marketing_eeg_system_type"] = _entry(
        key="direct_marketing_eeg_system_type",
        label='Anlagenart',
        unit="-",
        configured=dm_eeg_system_type if _has_user_value(cfg, "direct_marketing_eeg_system_type") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_system_type,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_system_type") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_system_type not in {"building", "other"} else "ok",
        message='Die gewählte Anlagenart wird bei der EEG-Vergütung berücksichtigt.' if dm_eeg_system_type in {'building', 'other'} else 'Die Anlagenart ist unbekannt; die Vergütungsberechnung kann nicht passend zugeordnet werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Anlagenart“ prüfen.',
    )
    price["direct_marketing_eeg_feed_type"] = _entry(
        key="direct_marketing_eeg_feed_type",
        label='Einspeiseart',
        unit="-",
        configured=dm_eeg_feed_type if _has_user_value(cfg, "direct_marketing_eeg_feed_type") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_feed_type,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_feed_type") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_feed_type not in {"partial", "full"} else "ok",
        message='Die gewählte Teil- oder Volleinspeisung wird bei der EEG-Vergütung berücksichtigt.' if dm_eeg_feed_type in {'partial', 'full'} else 'Die Einspeiseart ist unbekannt; die Vergütungsberechnung kann nicht passend zugeordnet werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Einspeiseart“ prüfen.',
    )
    price["direct_marketing_eeg_compensation_basis"] = _entry(
        key="direct_marketing_eeg_compensation_basis",
        label='Rechengrundlage',
        unit="-",
        configured=dm_eeg_compensation_basis if _has_user_value(cfg, "direct_marketing_eeg_compensation_basis") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_compensation_basis,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_compensation_basis") else "default",
        severity="warning" if dm_eeg_enabled and dm_eeg_compensation_basis not in {"feed_in_tariff", "market_premium"} else "ok",
        message=(
            'Die gewählte Einspeisevergütung oder Marktprämie wird als Rechengrundlage verwendet.' if dm_eeg_compensation_basis in {'feed_in_tariff', 'market_premium'} else 'Die Rechengrundlage ist unbekannt; die Vergütungsberechnung ist nicht eindeutig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Rechengrundlage“ prüfen.'
        ),
    )
    eeg_date_invalid = bool(dm_eeg_commissioning and not re.match(r"^\d{4}-\d{2}-\d{2}$", dm_eeg_commissioning))
    price["direct_marketing_eeg_commissioning_date"] = _entry(
        key="direct_marketing_eeg_commissioning_date",
        label='Inbetriebnahme',
        unit="-",
        configured=dm_eeg_commissioning if _has_user_value(cfg, "direct_marketing_eeg_commissioning_date") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_commissioning,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_commissioning_date") else "default",
        severity="warning" if dm_eeg_enabled and eeg_date_invalid else "ok",
        message='Das Inbetriebnahmedatum ist ungültig; ein passender Vergütungssatz kann nicht bestimmt werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Inbetriebnahme“ prüfen.' if dm_eeg_enabled and eeg_date_invalid else 'Das Inbetriebnahmedatum ist verwendbar oder nicht eingetragen.',
    )
    price["direct_marketing_eeg_tariff_tiers"] = _entry(
        key="direct_marketing_eeg_tariff_tiers",
        label='Vergütungsstufen',
        unit="ct/kWh",
        configured=dm_eeg_tiers if _has_user_value(cfg, "direct_marketing_eeg_tariff_tiers") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_tiers,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_tariff_tiers") else "default",
        severity="warning" if dm_eeg_enabled and not eeg_auto_source and not dm_eeg_tiers else "ok",
        message=(
            'Die EEG-Bewertung ist an, aber Vergütungsstufen fehlen; die Vergütung kann nicht berechnet werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Vergütungsstufen“ prüfen.' if dm_eeg_enabled and (not eeg_auto_source) and (not dm_eeg_tiers) else 'Vergütungsstufen sind hinterlegt, automatisch ableitbar oder EEG ist aus.'
        ),
    )
    eeg_grid_export_risk = bool(dm_enabled and dm_eeg_enabled and dm_grid_charge_enabled and dm_export_enabled)
    price["direct_marketing_eeg_grid_export_risk_ack"] = _entry(
        key="direct_marketing_eeg_grid_export_risk_ack",
        label='Ich bestätige, dass Messkonzept, Direktvermarktervertrag und Netzbetreiberfreigabe diese Betriebsweise erlauben.',
        unit="-",
        configured=cfg.get("direct_marketing_eeg_grid_export_risk_ack") if _has_user_value(cfg, "direct_marketing_eeg_grid_export_risk_ack") else None,
        live_value=None,
        live_key=None,
        effective=dm_eeg_grid_export_ack,
        source="user" if _has_user_value(cfg, "direct_marketing_eeg_grid_export_risk_ack") else "default",
        severity="warning" if eeg_grid_export_risk and not dm_eeg_grid_export_ack else "ok",
        message=(
            'Für Netzladen und spätere Einspeisung der EEG-Anlage fehlt das bestätigte Mess- und Vertragskonzept; entsprechende Speicherbefehle bleiben gesperrt. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Ich bestätige, dass Messkonzept, Direktvermarktervertrag und Netzbetreiberfreigabe diese Betriebsweise erlauben.“ prüfen.' if eeg_grid_export_risk and (not dm_eeg_grid_export_ack) else 'Für die EEG-Anlage ist kein unbestätigter Betrieb mit Netzladen und späterer Einspeisung vorgesehen.'
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
        label='Vergütungsschwelle für PV-Speichern',
        unit="-",
        configured=None,
        live_value=None,
        live_key=None,
        effective=not threshold_contract_warning,
        source="derived",
        severity="warning" if threshold_contract_warning else "ok",
        message=(
            'Für PV-Speichern fehlt eine manuelle Vergütungsschwelle oder eine gültige Vergütungstabelle; es bleibt nur die Preisbewertung. Im Config-Editor unter „Tarif & Strompreise“ das Feld „PV-Speicher-Schwelle“ prüfen.' if threshold_contract_warning else 'Die Vergütungsschwelle ist eingetragen oder automatisch bestimmbar; andernfalls bewertet das PV-Speichern die verfügbaren Preise.'
        ),
    )
    price["direct_marketing_pv_store_dc_only_enable"] = _entry(
        key="direct_marketing_pv_store_dc_only_enable",
        label='Nur direkte E3DC-PV als Speicherquelle',
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
            'Die gewählte Zusatzwechselrichterladung ist mangels belastbarem Prognosenachweis nicht freigegeben; nur E3DC-PV bleibt zulässig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „AC-Speicherroute“ prüfen.' if dm_aux_ac_storage_requested else 'Nur direkte PV-Leistung des E3DC-Wechselrichters ist als Quelle für PV-Speichern freigegeben.'
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
        label='AC-Speicherroute',
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
            'Die Speicherroute des Zusatzwechselrichters ist unbekannt; verwendet wird „Aus“. Im Config-Editor unter „Tarif & Strompreise“ das Feld „AC-Speicherroute“ prüfen; eine Auswahl ersetzt den fehlenden Nachweis nicht.' if dm_aux_ac_mode_source.endswith('_invalid') else 'Die gewählte Zusatzwechselrichterladung wird durch die Einstellung für ausschließliche E3DC-PV-Ladung überstimmt; sie bleibt aus. Im Config-Editor unter „Tarif & Strompreise“ das Feld „AC-Speicherroute“ prüfen; eine Auswahl ersetzt den fehlenden Nachweis nicht.' if dm_aux_ac_storage_requested and dm_pv_store_legacy_dc_only_veto else 'Die Zusatzwechselrichterladung ist gespeichert, aber Flächenzuordnung und belastbarer gemeinsamer Prognosenachweis fehlen; nur E3DC-PV bleibt zulässig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „AC-Speicherroute“ prüfen; eine Auswahl ersetzt den fehlenden Nachweis nicht.' if dm_aux_ac_storage_requested and (not dm_aux_ac_topology_ready) else 'Die Zusatzwechselrichterladung ist gespeichert, aber mangels belastbarem gemeinsamen Prognosenachweis nicht freigegeben; nur E3DC-PV bleibt zulässig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „AC-Speicherroute“ prüfen; eine Auswahl ersetzt den fehlenden Nachweis nicht.' if dm_aux_ac_storage_requested else 'Die Speicherladung aus dem Zusatzwechselrichter ist aus; direkte E3DC-PV-Leistung bleibt die vorgesehene Quelle.'
        ),
    )
    price["direct_marketing_aux_inverter_ac_storage_enable"] = _entry(
        key="direct_marketing_aux_inverter_ac_storage_enable",
        label='Alte Freigabe für Zusatzwechselrichterladung',
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
            'Die alte Freigabe wird als Auswahl übernommen, erlaubt mangels belastbarem Prognosenachweis aber keine Zusatzwechselrichterladung. Im Config-Editor unter „Tarif & Strompreise“ das Nachfolgefeld „AC-Speicherroute“ verwenden.' if dm_aux_ac_mode_source == 'legacy_bool_explicit_true' else 'Die alte Freigabe wird durch die Beschränkung auf E3DC-PV überstimmt; Zusatzwechselrichterladung bleibt aus. Im Config-Editor unter „Tarif & Strompreise“ das Nachfolgefeld „AC-Speicherroute“ verwenden.' if dm_aux_ac_storage_requested and dm_pv_store_legacy_dc_only_veto else 'Die alte Freigabe zeigt nur die gespeicherte Auswahl an; sie erlaubt derzeit keine Zusatzwechselrichterladung. Im Config-Editor unter „Tarif & Strompreise“ das Nachfolgefeld „AC-Speicherroute“ verwenden.' if dm_aux_ac_storage_requested else 'Die alte Freigabe wird nur angezeigt; Speicherladung aus dem Zusatzwechselrichter ist aus.'
        ),
    )
    market_value_source_ok = dm_market_value_solar_source in {"netztransparenz_hochrechnung_solar"}
    price["direct_marketing_market_value_solar_enable"] = _entry(
        key="direct_marketing_market_value_solar_enable",
        label='Monatswert-Trend berechnen',
        unit="-",
        configured=cfg.get("direct_marketing_market_value_solar_enable") if _has_user_value(cfg, "direct_marketing_market_value_solar_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_market_value_solar_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_market_value_solar_enable") else "default",
        severity="warning" if dm_market_value_solar_enabled and (not nt_client_id or not nt_client_secret or not market_value_source_ok) else "ok",
        message=(
            'Die Marktwert-Solar-Auswertung ist an, aber Zugangsdaten oder Quelle fehlen; die Auswertung kann nicht vollständig arbeiten. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Monatswert-Trend berechnen“ prüfen.' if dm_market_value_solar_enabled and (not nt_client_id or not nt_client_secret or (not market_value_source_ok)) else 'Die Marktwert-Solar-Auswertung ist an; sie liest Daten und ändert die Regelung nicht.' if dm_market_value_solar_enabled else 'Die Marktwert-Solar-Auswertung ist ausgeschaltet.'
        ),
    )
    price["direct_marketing_market_value_solar_source"] = _entry(
        key="direct_marketing_market_value_solar_source",
        label='Quelle',
        unit="-",
        configured=dm_market_value_solar_source if _has_user_value(cfg, "direct_marketing_market_value_solar_source") else None,
        live_value=None,
        live_key=None,
        effective=dm_market_value_solar_source,
        source="user" if _has_user_value(cfg, "direct_marketing_market_value_solar_source") else "default",
        severity="warning" if dm_market_value_solar_enabled and not market_value_source_ok else "ok",
        message='Die gewählte Datenquelle ist für die Marktwert-Solar-Auswertung verwendbar.' if market_value_source_ok else 'Die Marktwert-Solar-Quelle ist unbekannt; ihre Daten können nicht zugeordnet werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Quelle“ prüfen.',
    )
    price["netztransparenz_client_id"] = _entry(
        key="netztransparenz_client_id",
        label='Zugangskennung für Netztransparenz',
        unit="-",
        configured="gesetzt" if _has_user_value(cfg, "netztransparenz_client_id") and nt_client_id else None,
        live_value=None,
        live_key=None,
        effective=bool(nt_client_id),
        source="user" if _has_user_value(cfg, "netztransparenz_client_id") else "default",
        severity="warning" if dm_market_value_solar_enabled and not nt_client_id else "ok",
        message='Die Zugangskennung für die Marktwert-Solar-Auswertung ist eingetragen.' if nt_client_id else 'Die Zugangskennung für Netztransparenz fehlt; die Marktwert-Solar-Auswertung kann sich nicht anmelden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Netztransparenz Client-ID“ prüfen.',
    )
    price["netztransparenz_client_secret"] = _entry(
        key="netztransparenz_client_secret",
        label='Zugangsschlüssel für Netztransparenz',
        unit="-",
        configured="***" if _has_user_value(cfg, "netztransparenz_client_secret") and nt_client_secret else None,
        live_value=None,
        live_key=None,
        effective=bool(nt_client_secret),
        source="user" if _has_user_value(cfg, "netztransparenz_client_secret") else "default",
        severity="warning" if dm_market_value_solar_enabled and not nt_client_secret else "ok",
        message='Der Zugangsschlüssel für die Marktwert-Solar-Auswertung ist eingetragen und wird nicht angezeigt.' if nt_client_secret else 'Der Zugangsschlüssel für Netztransparenz fehlt; die Marktwert-Solar-Auswertung kann sich nicht anmelden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Netztransparenz Client-Secret“ prüfen.',
    )
    price["direct_marketing_aux_inverter_shelly_contract_status"] = _entry(
        key="direct_marketing_aux_inverter_shelly_contract_status",
        label='Konflikt in der Zusatzwechselrichtersteuerung',
        unit="-",
        configured=None,
        live_value=None,
        live_key=None,
        effective="blocked" if dm_aux_contract_blocked else "ok",
        source=str(dm_aux_migration.get("source") or "safe_defaults"),
        severity="warning" if dm_aux_contract_blocked else "ok",
        message=(
            'Die Angaben zum Zusatzwechselrichter sind widersprüchlich oder unvollständig; seine zentrale Steuerung bleibt aus. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Steuerung“ prüfen.' if dm_aux_contract_blocked else 'Die Steuerungsangaben des Zusatzwechselrichters sind vollständig und widersprechen einander nicht.'
        ),
    )
    price["direct_marketing_aux_inverter_shelly_override"] = _entry(
        key="direct_marketing_aux_inverter_shelly_override",
        label='Steuerung',
        unit="-",
        configured=str(aux_inverter_contract.get("override") or "local"),
        live_value=None,
        live_key=None,
        effective="central" if dm_aux_shelly_override else "local",
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_contract_blocked else "ok",
        message=(
            'Die Steuerungsangaben widersprechen einander; das lokale Shelly-Skript bleibt zuständig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Steuerung“ prüfen.' if dm_aux_contract_blocked else 'E3DC-Control übernimmt die Shelly-Relaissteuerung für den ungeregelten Zusatzwechselrichter.' if dm_aux_shelly_override else 'Die lokale Shelly-Steuerung bleibt für den Zusatzwechselrichter zuständig; E3DC-Control sendet keine Relaisbefehle.'
        ),
    )
    price["direct_marketing_aux_inverter_shelly_ip"] = _entry(
        key="direct_marketing_aux_inverter_shelly_ip",
        label='Shelly-IP',
        unit="-",
        configured="konfiguriert" if dm_aux_shelly_ip else None,
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_shelly_ip),
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_override and not dm_aux_shelly_ip_valid else "ok",
        message=(
            'Die zentrale Shelly-Steuerung ist an, aber ihre IPv4-Adresse fehlt oder ist ungültig; das Relais kann nicht angesprochen werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Shelly-IP“ prüfen.' if dm_aux_shelly_override and (not dm_aux_shelly_ip_valid) else 'Die eingetragene Shelly-Adresse ist für die Relaissteuerung verwendbar.' if dm_aux_shelly_ip else 'Es ist keine Shelly-Adresse eingetragen; solange die zentrale Relaissteuerung aus ist, wird sie nicht benötigt.'
        ),
    )
    price["direct_marketing_aux_inverter_shelly_invert"] = _entry(
        key="direct_marketing_aux_inverter_shelly_invert",
        label='Invertiert / NC-Schütz',
        unit="-",
        configured=bool(dm_aux_shelly_invert),
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_shelly_invert),
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_override and dm_aux_shelly_invert else "ok",
        message=(
            'Die umgekehrte Relaissteuerung ist an; ein eingeschaltetes Relais schaltet den Zusatzwechselrichter aus. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Invertiert / NC-Schütz“ nur für ein passendes, stromlos geschlossenes Schütz eingeschaltet lassen.' if dm_aux_shelly_override and dm_aux_shelly_invert else 'Invertiert aktiv: Shelly-Relais EIN bedeutet Zusatzwechselrichter AUS (NC-Schütz/stromlos geschlossen).' if dm_aux_shelly_invert else 'Normale Logik: Shelly-Relais EIN bedeutet Zusatzwechselrichter EIN.'
        ),
    )
    price["direct_marketing_aux_inverter_shelly_dynamic_unblock_enable"] = _entry(
        key="direct_marketing_aux_inverter_shelly_dynamic_unblock_enable",
        label='Dynamische Last-Freigabe',
        unit="-",
        configured=bool(dm_aux_shelly_dynamic_unblock),
        live_value=None,
        live_key=None,
        effective=bool(dm_aux_shelly_dynamic_unblock),
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_override and dm_aux_shelly_dynamic_unblock else "ok",
        message=(
            'Die dynamische Freigabe ist an und verwendet aktuelle Netz- und Lastwerte mit 600 s Mindestabstand zwischen Schaltungen; die Lastschwelle allein garantiert keine vollständige Vermeidung von Einspeisung. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Dynamische Last-Freigabe“ nur für diesen bewusst gewünschten Betrieb eingeschaltet lassen.' if dm_aux_shelly_dynamic_unblock else 'Die dynamische Lastfreigabe ist aus; bei Negativpreisen bleibt der Zusatzwechselrichter abgeschaltet.'
        ),
    )
    price["direct_marketing_aux_inverter_shelly_unblock_threshold_w"] = _entry(
        key="direct_marketing_aux_inverter_shelly_unblock_threshold_w",
        label='Last-Freigabe ab',
        unit="W",
        configured=dm_aux_shelly_unblock_threshold_w,
        live_value=None,
        live_key=None,
        effective=dm_aux_shelly_unblock_threshold_w,
        source=str(dm_aux_migration.get("source") or "default"),
        severity="warning" if dm_aux_shelly_dynamic_unblock and not (100.0 <= dm_aux_shelly_unblock_threshold_w <= 100000.0) else "ok",
        message=(
            f'Die Lastschwelle von {dm_aux_shelly_unblock_threshold_w:g} W liegt außerhalb 100 bis 100000 W; verwendet werden mindestens 100 W. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Last-Freigabe ab“ prüfen.' if dm_aux_shelly_dynamic_unblock and (not 100.0 <= dm_aux_shelly_unblock_threshold_w <= 100000.0) else 'Last-Freigabeschwelle ist plausibel; sie muss zur Leistung des Zusatzwechselrichters und der schaltbaren Last passen.'
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
        label='Basis variable Gebühr',
        unit="-",
        configured=dm_variable_fee_basis if _has_user_value(cfg, "direct_marketing_variable_fee_basis") else None,
        live_value=None,
        live_key=None,
        effective=dm_variable_fee_basis,
        source="user" if _has_user_value(cfg, "direct_marketing_variable_fee_basis") else "default",
        severity="warning" if dm_fee_pct > 0.0 and (not variable_fee_basis_known or not variable_fee_basis_ready) else "ok",
        message=(
            'Die variable Gebühr ist an, aber ihre Abrechnungsbasis fehlt oder ist unbekannt; die Kostenbewertung ist unvollständig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Basis variable Gebühr“ prüfen.' if dm_fee_pct > 0.0 and (not variable_fee_basis_known or not variable_fee_basis_ready) else 'Die variable Gebühr wird auf die konfigurierte Abrechnungsbasis gerechnet.'
        ),
    )
    price["direct_marketing_input_vat_recoverable"] = _entry(
        key="direct_marketing_input_vat_recoverable",
        label='Vorsteuer abziehbar',
        unit="-",
        configured=cfg.get("direct_marketing_input_vat_recoverable") if _has_user_value(cfg, "direct_marketing_input_vat_recoverable") else None,
        live_value=None,
        live_key=None,
        effective=dm_input_vat_recoverable,
        source="user" if _has_user_value(cfg, "direct_marketing_input_vat_recoverable") else "default",
        severity="warning" if dm_enabled and (dm_fee_pct > 0.0 or dm_monthly_fee > 0.0 or dm_balancing_cost_estimate > 0.0) and not _has_user_value(cfg, "direct_marketing_input_vat_recoverable") else "ok",
        message=(
            'Die Vorsteuerbehandlung ist nicht festgelegt; dadurch ist die Kostenbewertung steuerlich ungeklärt. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Vorsteuer abziehbar“ prüfen.' if dm_enabled and (dm_fee_pct > 0.0 or dm_monthly_fee > 0.0 or dm_balancing_cost_estimate > 0.0) and (not _has_user_value(cfg, 'direct_marketing_input_vat_recoverable')) else 'Vorsteuer wird kostenmindernd berücksichtigt.' if dm_input_vat_recoverable else 'Umsatzsteuer wird als Kostenbestandteil berücksichtigt.'
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
        label='Geschütztes Reserve-Ziel',
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
            'Leer bedeutet automatisch: wirksame Notstrom-/Haus-/Nachtreserve.' if dm_aux_ac_protected_target_raw is None else 'Der eingestellte Reserve-Zielstand liegt im gültigen Bereich von 0 bis 100 %.' if protected_target_valid else 'Das geschützte Speicherziel liegt außerhalb 0 bis 100 % und ist ungültig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Geschütztes Reserve-Ziel“ prüfen.'
        ),
    )
    dm_numeric_checks = (
        ("direct_marketing_fee_ct_per_kwh", 'Gebühr fix', dm_fee_ct, "ct/kWh", 0.0, None),
        ("direct_marketing_fee_pct", 'Gebühr variabel', dm_fee_pct, "%", 0.0, 100.0),
        ("direct_marketing_monthly_fee_eur", 'Grundgebühr', dm_monthly_fee, "EUR/Monat", 0.0, None),
        ("direct_marketing_variable_fee_basis_ct_per_kwh", 'Manuelle Gebührenbasis', dm_variable_fee_basis_ct, "ct/kWh", 0.0, None),
        ("direct_marketing_service_vat_pct", 'USt. auf Kosten', dm_service_vat_pct, "%", 0.0, 100.0),
        ("direct_marketing_installed_kwp", 'Abrechnungsleistung', dm_installed_kwp, "kWp", 0.0, None),
        ("direct_marketing_balancing_cost_eur_per_kwp_month", 'Ausgleichskosten Abschlag', dm_balancing_cost_estimate, "EUR/kWp/Monat", 0.0, None),
        ("direct_marketing_balancing_cost_actual_eur_per_kwp_month", 'Ausgleichskosten tatsächlich', dm_balancing_cost_actual, "EUR/kWp/Monat", 0.0, None),
        ("direct_marketing_revenue_offset_ct", 'Erlös-Korrektur', dm_revenue_offset, "ct/kWh", None, None),
        ("direct_marketing_min_margin_pct", 'Mindestmarge', dm_min_margin, "%", 0.0, None),
        ("direct_marketing_min_profit_ct_per_kwh", 'Mindestgewinn', dm_min_profit, "ct/kWh", 0.0, None),
        ("direct_marketing_min_window_profit_eur", 'Mindestgewinn/Fenster', dm_min_window_profit_eur, "EUR", 0.0, None),
        ("direct_marketing_min_export_energy_kwh", 'Mindestenergie/Fenster', dm_min_export_energy_kwh, "kWh", 0.0, None),
        ("direct_marketing_min_export_window_min", 'Mindestdauer/Fenster', dm_min_export_window_min, "min", 15.0, 1440.0),
        ("direct_marketing_preferred_export_plateau_min", 'Bevorzugtes Plateau', dm_preferred_export_plateau_min, "min", 15.0, 1440.0),
        ("direct_marketing_price_plateau_tolerance_ct", 'Plateau-Toleranz', dm_price_plateau_tolerance_ct, "ct/kWh", 0.0, 20.0),
        ("direct_marketing_max_daily_export_kwh", 'Tagesexportlimit', dm_max_daily_export_kwh, "kWh/Tag", 0.0, None),
        ("direct_marketing_deep_cycle_threshold_pct", 'Tiefzyklus ab', dm_deep_cycle_threshold_pct, "%", 0.0, 100.0),
        ("direct_marketing_deep_cycle_lcos_factor", 'Tiefzyklus-LCOS', dm_deep_cycle_lcos_factor, "Faktor", 0.0, 5.0),
        ("direct_marketing_profit_hold_ct_per_kwh", 'Gewinn-Halteband', dm_profit_hold, "ct/kWh", 0.0, None),
        ("direct_marketing_margin_hold_pct", 'Margen-Halteband', dm_margin_hold, "%", 0.0, 50.0),
        ("direct_marketing_degradation_ct_per_kwh", 'Degeneration', dm_degradation, "ct/kWh", 0.0, None),
        ("direct_marketing_roundtrip_efficiency_pct", 'Wirkungsgrad', dm_efficiency, "%", 50.0, 100.0),
        ("direct_marketing_safety_margin_ct_per_kwh", 'Sicherheitskorrektur', dm_safety_margin, "ct/kWh", -10.0, 50.0),
        ("direct_marketing_max_export_w", 'Basis-Entladung', dm_max_export_w, "W", 0.0, None),
        ("direct_marketing_min_grid_export_w", 'Mindest-Netzexport', dm_min_grid_export_w, "W", 0.0, None),
        ("direct_marketing_max_grid_charge_w", 'Max. Netzladen', dm_max_grid_charge_w, "W", 0.0, None),
        ("direct_marketing_pv_store_threshold_ct", 'PV-Speicher-Schwelle', dm_pv_store_threshold, "ct/kWh", -50.0, 200.0),
        ("direct_marketing_pv_store_max_w", 'Max. PV-Speichern', dm_pv_store_max_w, "W", 0.0, None),
        ("direct_marketing_pv_store_min_surplus_w", 'PV-Mindestüberschuss', dm_pv_store_min_surplus_w, "W", 0.0, None),
        ("direct_marketing_pv_store_import_guard_w", 'PV-Importwächter', dm_pv_store_import_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_min_hold_s", 'PV-Mindesthaltezeit', dm_pv_store_min_hold_s, "s", 0.0, 3600.0),
        ("direct_marketing_pv_store_ramp_step_w", 'PV-Laderampe', dm_pv_store_ramp_step_w, "W/Zyklus", 100.0, 5000.0),
        ("direct_marketing_aux_inverter_ac_deadband_wh", 'Defizit-Totbereich', dm_aux_ac_deadband_wh, "Wh", 0.0, None),
        ("direct_marketing_aux_inverter_ac_min_margin_ct_per_kwh", 'Zusätzliche Mindestmarge', dm_aux_ac_min_margin_ct, "ct/kWh", 0.0, None),
        ("direct_marketing_aux_inverter_ac_grid_import_guard_w", "DV-AC-Netzbezugsgrenze", dm_aux_ac_grid_import_guard_w, "W", 0.0, 300.0),
        ("direct_marketing_pv_store_dc_charge_efficiency_pct", 'E3DC-DC Laden', dm_pv_store_dc_charge_efficiency_pct, "%", 50.0, 100.0),
        ("direct_marketing_pv_store_aux_ac_charge_efficiency_pct", 'Zusatz-WR-AC Laden', dm_pv_store_aux_ac_charge_efficiency_pct, "%", 50.0, 100.0),
        ("direct_marketing_pv_store_discharge_efficiency_pct", 'Speicher entladen', dm_pv_store_discharge_efficiency_pct, "%", 50.0, 100.0),
        ("direct_marketing_pv_store_external_ac_guard_w", 'AC-Zusatz-Wächter', dm_pv_store_external_ac_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_export_limit_guard_w", 'Exportlimit-Toleranz', dm_pv_store_export_limit_guard_w, "W", 0.0, None),
        ("direct_marketing_pv_store_export_limit_ramp_bypass_w", 'Exportlimit-Bypass', dm_pv_store_export_limit_ramp_bypass_w, "W", 0.0, 5000.0),
        ("direct_marketing_price_max_age_s", 'Preis-Maxalter', dm_price_max_age_s, "s", 0.0, None),
        ("direct_marketing_max_cycles_per_day", 'Zyklen/Tag', dm_cycles, "Zyklen/Tag", 0.0, 3.0),
        ("direct_marketing_keep_headroom_pct", 'Headroom', dm_headroom, "%", 0.0, 100.0),
        ("direct_marketing_negative_headroom_lookahead_min", 'Preisfenster-Vorlauf', dm_negative_headroom_lookahead_min, "min", 0.0, 1440.0),
        ("direct_marketing_negative_headroom_min_window_min", 'Preisfenster-Mindestfenster', dm_negative_headroom_min_window_min, "min", 0.0, 720.0),
        ("direct_marketing_negative_headroom_min_surplus_wh", 'Preisfenster-Mindestüberschuss', dm_negative_headroom_min_surplus_wh, "Wh", 0.0, None),
        ("direct_marketing_negative_headroom_buffer_pct", 'Preisfenster-Puffer', dm_negative_headroom_buffer_pct, "%", 0.0, 50.0),
        ("direct_marketing_low_price_curtail_limit_w", 'Restexport-Limit', dm_curtail_limit_w, "W", 0.0, None),
        ("direct_marketing_eeg_support_years", 'Förderjahre', dm_eeg_support_years, "Jahre", 0.0, 30.0),
    )
    for key, label, value, unit, minimum, maximum in dm_numeric_checks:
        severity = "ok"
        message = f"Die Direktvermarktung verwendet für diese Einstellung {value:g} {unit}."
        check_active = dm_eeg_enabled if key == "direct_marketing_eeg_support_years" else dm_enabled
        if check_active:
            if minimum is not None and value < minimum:
                severity = "warning"
                message = (f'Der Wert von {value:g} {unit} liegt unter der Mindestgrenze von {minimum:g} {unit}. Kein eigenes Feld im Config-Editor; die Konfiguration durch den Fachbetrieb prüfen lassen.') if key == "direct_marketing_aux_inverter_ac_grid_import_guard_w" else (f'Der Wert von {value:g} {unit} liegt unter der Mindestgrenze von {minimum:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.')
            elif maximum is not None and value > maximum:
                severity = "warning"
                message = (f'Der Wert von {value:g} {unit} liegt über der Obergrenze von {maximum:g} {unit}. Kein eigenes Feld im Config-Editor; die Konfiguration durch den Fachbetrieb prüfen lassen.') if key == "direct_marketing_aux_inverter_ac_grid_import_guard_w" else (f'Der Wert von {value:g} {unit} liegt über der Obergrenze von {maximum:g} {unit}. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.')
            elif key == "direct_marketing_safety_margin_ct_per_kwh" and value < 0.0:
                severity = "warning"
                message = f'Die negative Preiskorrektur von {value:g} ct/kWh lässt kleinere Preisvorteile genügen; die Mindestmarge bleibt wirksam. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_min_margin_pct" and value < 10.0:
                severity = "warning"
                message = f'Die Mindestmarge von {value:g} % liegt unter 10 %; kleinere erwartete Gewinne reichen für eine Speicheraktion. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_max_export_w" and dm_export_enabled and value <= 0.0:
                severity = "warning"
                message = f'Batterieeinspeisung ist freigegeben, aber 0 W Basisentladung erlauben keine Entladeleistung. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_max_grid_charge_w" and dm_grid_charge_enabled and value <= 0.0:
                severity = "warning"
                message = f'Netzladen ist freigegeben, aber 0 W maximale Netzladeleistung erlauben keine Ladung. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_pv_store_min_surplus_w" and dm_pv_store_enabled and value < 300.0:
                severity = "warning"
                message = f'Der Mindestüberschuss von {value:g} W liegt unter 300 W; PV-Speichern kann dadurch häufig ein- und ausschalten oder Messrauschen folgen. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_pv_store_import_guard_w" and dm_pv_store_enabled and value > 300.0:
                severity = "warning"
                message = f'Die zulässige Bezugsgrenze von {value:g} W ist hoch; beim PV-Speichern kann Netzstrom bezogen werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_pv_store_max_w" and dm_pv_store_enabled and value <= 0.0:
                message = 'Bei 0 W bestimmt die Speicherregelung die Ladeleistung aus der Systemgrenze und dem tatsächlich verfügbaren PV-Überschuss.'
            elif key == "direct_marketing_pv_store_min_hold_s" and dm_pv_store_enabled and value < 300.0:
                severity = "warning"
                message = f'Die Mindesthaltezeit von {value:g} s liegt unter 5 Minuten; PV-Speichern kann häufig ein- und ausschalten. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_pv_store_ramp_step_w" and dm_pv_store_enabled and value > 3000.0:
                severity = "warning"
                message = f'Die Leistungsänderung von {value:g} W je Zyklus ist groß; die PV-Ladeleistung kann sprunghaft wechseln. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_aux_inverter_ac_deadband_wh" and value == 0.0:
                message = 'Bei 0 wird die Mindestenergiemenge automatisch bestimmt; sie beträgt mindestens 500 Wh, 2 % der Speicherkapazität und die Energie für einen Zeitabschnitt bei Mindestleistung.'
            elif key == "direct_marketing_aux_inverter_ac_min_margin_ct_per_kwh" and value == 0.0:
                message = 'Bei 0 muss Laden aus dem Zusatzwechselrichter nach Verlusten einen positiven Preisvorteil haben; ein höherer eingestellter Mindestvorteil gilt zusätzlich.'
            elif key == "direct_marketing_aux_inverter_ac_grid_import_guard_w" and value > 0.0:
                severity = "warning"
                message = (f'Die Bezugsgrenze von {value:g} W erlaubt messbaren Netzbezug beim Laden aus dem Zusatzwechselrichter. Kein eigenes Feld im Config-Editor; die Konfiguration durch den Fachbetrieb prüfen lassen.') if key == "direct_marketing_aux_inverter_ac_grid_import_guard_w" else (f'Die Bezugsgrenze von {value:g} W erlaubt messbaren Netzbezug beim Laden aus dem Zusatzwechselrichter. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.')
            elif (
                key == "direct_marketing_pv_store_aux_ac_charge_efficiency_pct"
                and value >= dm_pv_store_dc_charge_efficiency_pct
            ):
                severity = "warning"
                message = f'Der Ladewirkungsgrad aus dem Zusatzwechselrichter von {value:g} % liegt nicht unter den {dm_pv_store_dc_charge_efficiency_pct:g} % bei direkter PV-Ladung und ist damit technisch fragwürdig. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_pv_store_export_limit_guard_w" and dm_pv_store_enabled and value > 500.0:
                severity = "warning"
                message = f'Die Einspeisetoleranz von {value:g} W ist hoch; bei Negativpreisen kann unnötige Resteinspeisung bleiben. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_pv_store_export_limit_ramp_bypass_w" and dm_pv_store_enabled and value > 1000.0:
                severity = "warning"
                message = f'Die Schwelle von {value:g} W für sofortige Leistungsänderungen ist hoch; auf ein Einspeiselimit von 0 W wird spät reagiert. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_negative_headroom_lookahead_min" and dm_negative_headroom_enabled and value < 30.0:
                severity = "warning"
                message = f'Der Vorlauf von {value:g} min ist kurz; die Ladekurve kann vor Negativpreisen zu spät beruhigt werden. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_negative_headroom_min_window_min" and dm_negative_headroom_enabled and value < 15.0:
                severity = "warning"
                message = f'Die Preisfensterdauer von {value:g} min liegt unter 15 Minuten; kurze Prognoseschwankungen können als nutzbares Fenster gelten. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_negative_headroom_min_surplus_wh" and dm_negative_headroom_enabled and value < 300.0:
                severity = "warning"
                message = f'Der Mindestüberschuss von {value:g} Wh ist klein; Messrauschen kann unnötig freie Speicherkapazität auslösen. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ prüfen.'
            elif key == "direct_marketing_price_max_age_s" and dm_enabled and value == 0.0:
                message = 'Bei 0 gibt es keine feste Altersgrenze für Preise; ausdrücklich als veraltet gekennzeichnete Daten werden weiterhin nicht verwendet.'
        price[key] = _entry(
            key=key,
            label={'direct_marketing_aux_inverter_ac_grid_import_guard_w': 'Netzbezugsgrenze der Zusatzwechselrichterladung', 'direct_marketing_keep_headroom_pct': 'Freie Speicherkapazität', 'direct_marketing_pv_store_export_limit_ramp_bypass_w': 'Sofortige Anpassung an das Einspeiselimit', 'direct_marketing_deep_cycle_lcos_factor': 'Verschleißkosten bei tiefer Entladung'}.get(key, label),
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
        label='Gespeicherter Prognosepunktwert',
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
            'Der gespeicherte Prognosepunktwert liegt außerhalb 0 bis 100 %; er besitzt keine Steuerwirkung und ersetzt keinen belastbaren Prognosenachweis. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Legacy-Punktwert nur Diagnose“ prüfen.' if not dm_point_confidence_valid else 'Der gespeicherte Prognosepunktwert ist nur eine Diagnoseangabe und erlaubt keine Ladung aus dem Zusatzwechselrichter; der belastbare Prognosenachweis fehlt weiterhin. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Legacy-Punktwert nur Diagnose“ nur als Diagnoseangabe verwenden.' if dm_aux_ac_storage_requested else 'Der gespeicherte Prognosepunktwert dient nur zur Diagnose und steuert nichts; er ist weder eine statistisch geprüfte Prognosegrenze noch eine Risikoschwelle.'
        ),
    )

    price["direct_marketing_arbitrage_enable"] = _entry(
        key="direct_marketing_arbitrage_enable",
        label='Alte Freigabe für Stromhandel mit der Batterie',
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
            'Der gespeicherte Stromhandel zwischen Netz und Batterie ist nicht freigegeben; die alten Werte bleiben ohne Wirkung. Keine Aktion nötig; diese Einstellung wird nicht mehr verwendet.' if dm_arbitrage_requested else 'Stromhandel zwischen Netz und Batterie ist derzeit nicht freigegeben.'
        ),
    )
    price["direct_marketing_export_enable"] = _entry(
        key="direct_marketing_export_enable",
        label='Batterieeinspeisung bei Direktvermarktung',
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
        label='Netzladen bei Direktvermarktung',
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
        ("direct_marketing_negative_price_no_export", 'Kein Verkauf bei Negativpreis', dm_negative_no_export),
        ("direct_marketing_negative_headroom_enable", 'Headroom vor Negativpreis', dm_negative_headroom_enabled),
        ("direct_marketing_low_price_headroom_enable", 'Headroom vor Billigpreis', dm_low_price_headroom_enabled),
        ("direct_marketing_low_price_no_export", 'Kein Verkauf bei Billigpreis', dm_low_no_export),
    ):
        price[key] = _entry(
            key=key,
            label={'direct_marketing_negative_headroom_enable': 'Freie Speicherkapazität vor Negativpreisen', 'direct_marketing_low_price_headroom_enable': 'Freie Speicherkapazität vor günstigen Preisen'}.get(key, label),
            unit="-",
            configured=cfg.get(key) if _has_user_value(cfg, key) else None,
            live_value=None,
            live_key=None,
            effective=value,
            source="user" if _has_user_value(cfg, key) else "default",
            severity="warning" if dm_enabled and not value else "ok",
            message='Die gewählte Funktion zur Vermeidung von Einspeisung bei ungünstigen Preisen ist eingeschaltet.' if value else f'Die Vermeidung von Einspeisung bei ungünstigen Preisen ist aus; ein späterer Verkauf zu solchen Preisen wäre möglich. Im Config-Editor unter „Tarif & Strompreise“ das Feld „{label}“ nur bei bewusst gewünschtem Betrieb ausgeschaltet lassen.',
        )
    price["direct_marketing_low_price_curtail_enable"] = _entry(
        key="direct_marketing_low_price_curtail_enable",
        label='Einspeisebegrenzung bei Negativpreisen',
        unit="-",
        configured=cfg.get("direct_marketing_low_price_curtail_enable") if _has_user_value(cfg, "direct_marketing_low_price_curtail_enable") else None,
        live_value=None,
        live_key=None,
        effective=dm_curtail_enabled,
        source="user" if _has_user_value(cfg, "direct_marketing_low_price_curtail_enable") else "default",
        severity="ok",
        message=(
            'Die zusätzliche Einspeisebegrenzung ist an und wirkt nur bei Negativpreisen; günstige positive Preise lösen keine harte Begrenzung aus. Im Config-Editor unter „Tarif & Strompreise“ das Feld „Negativpreis-Export begrenzen“ nur für diesen gewünschten Betrieb eingeschaltet lassen.' if dm_curtail_enabled else 'Zusätzliche harte Negativpreis-Einspeisebegrenzung ist aus.'
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
        label='Benutzer (E-Mail)',
        unit="-",
        configured="gesetzt" if bluelink_user_set else None,
        live_value=None,
        live_key=None,
        effective="gesetzt" if bluelink_user_set else "leer",
        source="user" if bluelink_user_set else "default",
        severity="warning" if (bluelink_active and not bluelink_user_set) else ("ok" if bluelink_user_set else "info"),
        message=(
            'Die E-Mail-Adresse des Herstellerkontos fehlt; der Fahrzeugdienst bleibt ohne Zugangsdaten aus. Im Config-Editor unter „Wallbox & Fahrzeug“ im Abschnitt „Fahrzeug Integration (Bluelink)“ das Feld „Benutzer (E-Mail)“ prüfen.' if bluelink_active and (not bluelink_user_set) else 'Benutzer ist gesetzt.' if bluelink_user_set else 'Bluelink ist nicht konfiguriert.'
        ),
    )
    wallbox["bluelink_password"] = _entry(
        key="bluelink_password",
        label='Passwort',
        unit="-",
        configured="gesetzt" if bluelink_password_set else None,
        live_value=None,
        live_key=None,
        effective="gesetzt" if bluelink_password_set else "leer",
        source="user" if bluelink_password_set else "default",
        severity="warning" if (bluelink_active and not bluelink_password_set) else ("ok" if bluelink_password_set else "info"),
        message=(
            'Das Passwort des Herstellerkontos fehlt; der Fahrzeugdienst kann sich nicht anmelden. Im Config-Editor unter „Wallbox & Fahrzeug“ im Abschnitt „Fahrzeug Integration (Bluelink)“ das Feld „Passwort“ prüfen.' if bluelink_active and (not bluelink_password_set) else 'Passwort ist gesetzt.' if bluelink_password_set else 'Bluelink ist nicht konfiguriert.'
        ),
    )
    bluelink_pin_valid = bluelink_pin_raw == "" or bluelink_pin_raw.isdigit()
    wallbox["bluelink_pin"] = _entry(
        key="bluelink_pin",
        label='PIN',
        unit="-",
        configured="gesetzt" if bluelink_pin_raw else None,
        live_value=None,
        live_key=None,
        effective="gesetzt" if (bluelink_pin_raw and bluelink_pin_valid) else "leer",
        source="user" if bluelink_pin_raw else "default",
        severity="warning" if not bluelink_pin_valid else "ok",
        message=(
            'Die PIN enthält andere Zeichen als Ziffern und wird ignoriert. Im Config-Editor unter „Wallbox & Fahrzeug“ im Abschnitt „Fahrzeug Integration (Bluelink)“ das Feld „PIN“ prüfen.' if not bluelink_pin_valid else 'PIN ist gesetzt.' if bluelink_pin_raw else 'PIN ist optional (nur für Konten mit PIN-Pflicht).'
        ),
    )
    bluelink_brand_valid = bluelink_brand_raw in {"", "hyundai", "kia"}
    wallbox["bluelink_brand"] = _entry(
        key="bluelink_brand",
        label='Marke',
        unit="-",
        configured=bluelink_brand_raw if bluelink_brand_raw else None,
        live_value=None,
        live_key=None,
        effective=bluelink_brand_raw if (bluelink_brand_raw and bluelink_brand_valid) else "hyundai",
        source="user" if (bluelink_brand_raw and bluelink_brand_valid) else "default",
        severity="warning" if not bluelink_brand_valid else "ok",
        message=(
            'Die Fahrzeugmarke ist unbekannt; verwendet wird Hyundai. Im Config-Editor unter „Wallbox & Fahrzeug“ im Abschnitt „Fahrzeug Integration (Bluelink)“ das Feld „Marke“ prüfen.' if not bluelink_brand_valid else 'Marke des Herstellerkontos (Hyundai Bluelink oder Kia Connect).'
        ),
    )

    mqtt_configured = (
        str(cfg.get("mqtt_hub_ip") or "").strip().lower() not in ("", "127.0.0.1", "localhost", "::1")
        or any(_has_user_value(cfg, key) for key in (
            "mqtt_hub_sub_soc_topic", "mqtt_hub_sub_soc_topic_2", "wp_power_topic", "wb_topic", "wb2_topic",
        ))
    )
    if mqtt_configured:
        repeat_raw = cfg.get("mqtt_hub_repeat_interval_s", "0")
        try:
            repeat_value = int(str(repeat_raw).strip())
            repeat_valid = repeat_value >= 0
        except (TypeError, ValueError):
            repeat_value, repeat_valid = 0, False
        repeat_effective = min(3600, max(10, repeat_value)) if repeat_value > 0 else 0
        repeat_warning = not repeat_valid or 0 < repeat_value < 10 or repeat_value > 3600
        repeat_message = f"Gültige aktuelle Messwerte werden alle {repeat_effective:g} s erneut gesendet; bei 0 s ist die Wiederholung aus."
        if not repeat_valid:
            repeat_message = f'Der Wiederholabstand ist keine nichtnegative ganze Sekundenzahl; die Wiederholung bleibt aus. Im Config-Editor unter „Schnittstellen & MQTT“ das Feld „Werte zyklisch wiederholen (s)“ prüfen.'
        elif 0 < repeat_value < 10:
            repeat_message = f'Der Wiederholabstand liegt unter 10 s; verwendet werden 10 s. Im Config-Editor unter „Schnittstellen & MQTT“ das Feld „Werte zyklisch wiederholen (s)“ prüfen.'
        elif repeat_value > 3600:
            repeat_message = f'Der Wiederholabstand liegt über 3600 s; verwendet werden 3600 s. Im Config-Editor unter „Schnittstellen & MQTT“ das Feld „Werte zyklisch wiederholen (s)“ prüfen.'
        consumer["mqtt_hub_repeat_interval_s"] = _entry(
            key="mqtt_hub_repeat_interval_s", label='Werte zyklisch wiederholen (s)', unit="s",
            configured=repeat_raw, live_value=None, live_key=None, effective=repeat_effective,
            source="user" if _has_user_value(cfg, "mqtt_hub_repeat_interval_s") else "default",
            severity="warning" if repeat_warning else "ok", message=repeat_message,
        )

    names, valid = parse_mqtt_vehicle_allowlist(cfg.get("mqtt_vehicle_allowlist"))
    consumer["mqtt_vehicle_allowlist"] = _entry(
        key="mqtt_vehicle_allowlist", label='MQTT-Fahrzeug-Freigabeliste (optional)', unit="-",
        configured="[maskiert]" if names is not None else "", live_value=None, live_key=None,
        effective="offen" if names is None else "begrenzt" if valid else "gesperrt",
        source="default" if names is None else "user", severity="ok" if valid else "warning",
        message=('Die MQTT-Fahrzeugfreigabe verwendet genaue, kommagetrennte Namen ohne Platzhalter; sie gilt nur für allgemeine MQTT-Fahrzeuge.' if valid else 'Die Fahrzeugfreigabeliste ist ungültig; allgemeine MQTT-Fahrzeuge bleiben gesperrt. Im Config-Editor unter „Schnittstellen & MQTT“ das Feld „MQTT-Fahrzeug-Freigabeliste (optional)“ auf exakte, kommagetrennte Namen ohne Platzhalter prüfen.'),
    )
    wp_topic = str(cfg.get("wp_power_topic") or "").strip()
    base = str(cfg.get("mqtt_hub_topic") or "e3dc").strip().strip('/') or 'e3dc'
    fixed_power = {f"{base}/in/heatpump/power_w", f"{base}/in/heatpump/electric_w"}
    other_topics = {str(cfg.get(key) or "").strip() for key in (
        "mqtt_hub_sub_soc_topic", "mqtt_hub_sub_soc_topic_2", "wb_topic", "wb2_topic")}
    inbound_keys = {
        "heatpump": {"power_w", "electric_w", "state", "mode", "ww_temp", "ww_target_temp", "flow_temp", "return_temp", "outside_temp", "heat_kw", "boost_active"},
        "heater": {"power_w", "water_temp", "target_temp", "state", "mode"},
        "house": {"extra_power_w"},
    }
    for device in ("wallbox", "wallbox1", "wallbox2", "wb", "wb1", "wb2", "wallbox_1", "wallbox_2"):
        inbound_keys[device] = {"power_w", "plugged", "charging", "soc", "range_km"}
    parts = wp_topic[len(base) + 4:].strip('/').split('/') if wp_topic.startswith(f"{base}/in/") else []
    inbound_conflict = len(parts) >= 2 and parts[-1] in inbound_keys.get(parts[0], set()) and wp_topic not in fixed_power
    conflict = bool(wp_topic and (wp_topic in other_topics or inbound_conflict
        or wp_topic.startswith((f"{base}/vehicle/", f"{base}/ha/", f"{base}/status/"))
        or wp_topic == f"{base}/live"))
    invalid = any(char in wp_topic for char in ('#', '+', '\x00')) or len(wp_topic.encode('utf-8')) > 65535
    consumer["wp_power_topic"] = _entry(
        key="wp_power_topic", label='MQTT-Leistungsthema der Wärmepumpe', unit="-", configured=wp_topic,
        live_value=None, live_key=None, effective=wp_topic or "Feste WP-Leistungseingänge",
        source="user" if wp_topic else "default", severity="warning" if invalid or conflict else "ok",
        message=('Das MQTT-Leistungsthema enthält Platzhalter oder ist ungültig; es wird keine automatische Ersatzquelle verwendet. Im Config-Editor unter „Schnittstellen & MQTT“ das Feld „WP-Leistungs-Topic (optional)“ auf ein exaktes Thema prüfen.' if invalid else 'Das MQTT-Leistungsthema wird schon für eine andere Funktion verwendet; die Zuordnung ist widersprüchlich. Im Config-Editor unter „Schnittstellen & MQTT“ im Feld „WP-Leistungs-Topic (optional)“ ein eigenes Thema wählen.' if conflict else 'Das MQTT-Thema liefert die Wärmepumpenleistung als gültige Zahl in Watt an den zentralen Nachrichtenserver. Zum Übernehmen im Einrichtungsplan den Dienst neu starten; eine Gerätekennung aus Home Assistant ist kein MQTT-Thema.'),
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
    *, reserve_sample_fresh: Optional[bool] = None,
) -> Dict[str, Any]:
    payload = validate_storage_config(cfg, live, reserve_sample_fresh=reserve_sample_fresh)
    _atomic_write_on_change(
        path,
        payload,
        force_interval_s=60.0,
        noise_keys={"ts"},
        indent=2,
    )
    return payload
