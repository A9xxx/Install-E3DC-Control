#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Reine Konfigurationsverträge für den Wärmepumpen-Preis-Boost.

Dieses Modul plant keine Wärme und sendet keine Aktorbefehle. Es begrenzt nur,
wann ein anderweitig fachlich freigegebener Preis-Boost zulässig ist und welche
getrennten Wärmeanforderungen ein geeigneter Treiber erhalten darf.
"""

from __future__ import annotations

import math
import re
import time
from datetime import datetime

try:
    from ..tariff_schedule import (
        TARIFF_TIMEZONE,
        supports_heat_price_boost,
        supports_spot_market_prices,
    )
except ImportError:  # pragma: no cover - direkter Skriptstart
    from tariff_schedule import (  # type: ignore
        TARIFF_TIMEZONE,
        supports_heat_price_boost,
        supports_spot_market_prices,
    )


VALID_SCOPES = frozenset({"heating", "dhw", "both"})
SEPARATE_TARGET_WP_TYPES = frozenset({0, 1})
COMBINED_TARGET_WP_TYPES = frozenset({5})
_WINDOW_PATTERN = re.compile(
    r"^\s*([01]?\d|2[0-3]):([0-5]\d)\s*-\s*"
    r"((?:[01]?\d|2[0-3]):[0-5]\d|24:00)\s*$"
)


def _enabled(value, default=False):
    if value is None or str(value).strip() == "":
        return bool(default)
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ja", "ein"}


def _nonnegative_number(value):
    if value is None or str(value).strip() == "":
        return None
    try:
        parsed = float(str(value).replace(",", "."))
    except (TypeError, ValueError):
        return None
    return max(0.0, parsed) if math.isfinite(parsed) else None


def normalize_scope(value):
    raw = str(value or "both").strip().lower()
    aliases = {
        "heat": "heating",
        "heizen": "heating",
        "heizung": "heating",
        "warmwasser": "dhw",
        "ww": "dhw",
        "beide": "both",
    }
    normalized = aliases.get(raw, raw)
    return normalized if normalized in VALID_SCOPES else "both"


def parse_allowed_windows(value):
    """Parst lokale Uhrzeitfenster; leere Eingabe bedeutet ganztägig.

    Über-Mitternacht-Fenster wie ``22:00-06:00`` sind zulässig. Ungültige
    nichtleere Einträge werden separat zurückgegeben und öffnen kein Fenster.
    """
    raw = str(value or "").strip()
    if not raw:
        return [], []
    tokens = [
        token.strip()
        for token in re.split(r"[\n,;]+", raw)
        if token.strip()
    ]
    windows = []
    invalid = []
    for token in tokens:
        match = _WINDOW_PATTERN.match(token)
        if not match:
            invalid.append(token)
            continue
        start_min = int(match.group(1)) * 60 + int(match.group(2))
        end_text = match.group(3)
        if end_text == "24:00":
            end_min = 24 * 60
        else:
            end_hour, end_minute = end_text.split(":", 1)
            end_min = int(end_hour) * 60 + int(end_minute)
        if start_min == end_min:
            invalid.append(token)
            continue
        windows.append((start_min, end_min))
    return windows, invalid


def allowed_window_active(value, now_ts=None):
    """Liefert die aktuelle Zeitfensterfreigabe; ungültige Eingabe ist fail-closed."""
    windows, invalid = parse_allowed_windows(value)
    if invalid:
        return False
    if not str(value or "").strip():
        return True
    if not windows:
        return False
    now_value = time.time() if now_ts is None else float(now_ts)
    local = datetime.fromtimestamp(now_value, tz=TARIFF_TIMEZONE)
    minute = local.hour * 60 + local.minute
    for start_min, end_min in windows:
        if end_min > start_min and start_min <= minute < end_min:
            return True
        if end_min < start_min and (minute >= start_min or minute < end_min):
            return True
    return False


def driver_capability(wp_type, has_shelly_heatpump=False):
    try:
        normalized_type = int(wp_type)
    except (TypeError, ValueError):
        normalized_type = -1
    separate = normalized_type in SEPARATE_TARGET_WP_TYPES
    combined = normalized_type in COMBINED_TARGET_WP_TYPES or bool(has_shelly_heatpump)
    return {
        "controllable": bool(separate or combined),
        "separate_targets": bool(separate),
        "combined_sg_ready": bool(combined and not separate),
    }


def configured_contract(
    config,
    wp_type,
    has_shelly_heatpump=False,
    now_ts=None,
    heat_forecast_valid=False,
    heat_forecast_need_kwh=None,
    pv_coverage_valid=False,
    pv_coverage_kwh=None,
    conservative_evidence_valid=False,
):
    """Bindet Konfiguration und Evidence für einen wirkungslosen Kandidaten.

    `general_enabled` bleibt in diesem Migrationsslice bewusst falsch: Erst
    ein bestätigter `heat_intent_v1` mit aktuellem Storage-Budget darf später
    eine aktive Ausführung autorisieren. P50 kann einen Kandidaten sichtbar
    machen, ist aber keine konservative Freigabeevidence.
    """
    config = config or {}
    capability = driver_capability(wp_type, has_shelly_heatpump)
    tariff_allowed = supports_heat_price_boost(config)
    spot_market = supports_spot_market_prices(config)
    window_allowed = allowed_window_active(
        config.get("heat_price_boost_windows", ""),
        now_ts=now_ts,
    )
    general_configured = bool(
        capability["controllable"]
        and tariff_allowed
        and window_allowed
        and _enabled(config.get("price_boost_enable"), False)
    )
    heat_need_kwh = _nonnegative_number(heat_forecast_need_kwh)
    pv_cover_kwh = _nonnegative_number(pv_coverage_kwh)
    coverage_contract_valid = bool(
        heat_forecast_valid
        and pv_coverage_valid
        and heat_need_kwh is not None
        and pv_cover_kwh is not None
    )
    remaining_heat_need_kwh = (
        max(0.0, heat_need_kwh - pv_cover_kwh)
        if coverage_contract_valid
        else None
    )
    # Der allgemeine Preis-Boost darf ausschließlich den nach prognostizierter
    # späterer PV-Deckung verbleibenden Wärmebedarf vorziehen. P50 markiert
    # dabei nur einen Diagnosekandidaten. Erst explizit konservativ gebundene
    # Quantile dürfen ihn als fachlich geeignet einstufen; die aktive
    # Ausführung bleibt bis zum bestätigten Intent-/Budgetvertrag gesperrt.
    general_candidate = bool(
        general_configured
        and coverage_contract_valid
        and remaining_heat_need_kwh > 0.001
    )
    general_eligible = bool(general_candidate and conservative_evidence_valid)
    general_enabled = False
    negative_price_enabled = bool(
        capability["controllable"]
        and spot_market
        and window_allowed
        and _enabled(config.get("cheap_grid_boost_enable"), False)
        and _enabled(config.get("cheap_grid_heatpump_enable"), False)
    )
    scope = (
        normalize_scope(config.get("heat_price_boost_scope", "both"))
        if capability["separate_targets"]
        else "both"
    )
    return {
        **capability,
        "tariff_allowed": bool(tariff_allowed),
        "spot_market": bool(spot_market),
        "window_allowed": bool(window_allowed),
        "general_configured": general_configured,
        "general_candidate": general_candidate,
        "general_eligible": general_eligible,
        "general_enabled": general_enabled,
        "activation_contract_complete": False,
        "evidence_status": (
            "COMPLETE"
            if general_eligible
            else "EVIDENCE_LIMIT" if general_candidate else "NOT_APPLICABLE"
        ),
        "shadow_only": True,
        "commands_allowed": False,
        "coverage_contract_valid": coverage_contract_valid,
        "heat_forecast_need_kwh": heat_need_kwh if coverage_contract_valid else None,
        "pv_coverage_kwh": pv_cover_kwh if coverage_contract_valid else None,
        "remaining_heat_need_kwh": remaining_heat_need_kwh,
        "negative_price_enabled": negative_price_enabled,
        "scope": scope,
    }


def boost_target_modes(scope, separate_targets, summer_mode):
    """Liefert HZ-/WW-Modi für einen bereits freigegebenen Preis-Boost."""
    if not separate_targets:
        return {"heating_mode": 1, "dhw_mode": 1, "scope": "both"}
    normalized = normalize_scope(scope)
    heating_mode = int(normalized in {"heating", "both"} and not bool(summer_mode))
    dhw_mode = int(normalized in {"dhw", "both"})
    return {
        "heating_mode": heating_mode,
        "dhw_mode": dhw_mode,
        "scope": normalized,
    }


def negative_price_runtime_allowed(
    contract,
    current_price_ct,
    *,
    heat_policy_runtime_enabled,
    requested,
):
    """Öffnet den Sonderpfad nur bei echtem Negativpreis und zentraler Policy."""
    try:
        price_ct = float(str(current_price_ct).replace(",", "."))
    except (TypeError, ValueError):
        return False
    return bool(
        isinstance(contract, dict)
        and contract.get("negative_price_enabled")
        and heat_policy_runtime_enabled
        and requested
        and math.isfinite(price_ct)
        and price_ct < 0.0
    )
