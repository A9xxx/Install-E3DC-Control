#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Neutrale Zeitachse für vollständig lokal konfigurierte Stromtarife.

Das Modul enthält ausschließlich Tarifauflösung. Es trifft keine Geräte-,
Markt- oder Freigabeentscheidung und erzeugt insbesondere keine erfundenen
Börsenpreise.
"""

import re
import time
from datetime import datetime
from zoneinfo import ZoneInfo


RECURRING_TARIFF_TYPES = frozenset({
    "static",
    "fix",
    "fixed",
    "flat",
    "octopus_heat",
    "special",
    "spezial",
    "special_tariff",
})
SPOT_MARKET_TARIFF_TYPES = frozenset({
    "awattar",
    "dynamic",
    "epex",
    "tibber",
})
STATIC_TARIFF_TYPES = frozenset({
    "static",
    "fix",
    "fixed",
    "flat",
})
HEAT_PRICE_BOOST_TARIFF_TYPES = frozenset(
    set(SPOT_MARKET_TARIFF_TYPES)
    | {"octopus_heat", "special", "spezial", "special_tariff"}
)
TARIFF_TIMEZONE_NAME = "Europe/Berlin"
TARIFF_TIMEZONE = ZoneInfo(TARIFF_TIMEZONE_NAME)


def _safe_float(value, default):
    try:
        if value is None or str(value).strip() == "":
            return float(default)
        if isinstance(value, str):
            value = value.replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def tariff_type(config):
    return str((config or {}).get("stromtarif_typ", "static") or "static").strip().lower()


def uses_recurring_tariff_axis(config):
    """True, wenn die vollständige Tagespreisachse lokal definiert ist."""
    return tariff_type(config) in RECURRING_TARIFF_TYPES


def supports_spot_market_prices(config):
    """True nur für Tarife mit echten Börsenpreis-Slots.

    Lokale Zeitfenster wie Octopus Heat und frei konfigurierte Sondertarife
    können günstige Preise abbilden, liefern aber keinen belastbaren
    Negativpreisvertrag.
    """
    return tariff_type(config) in SPOT_MARKET_TARIFF_TYPES


def supports_heat_price_boost(config):
    """True für alle zeitvariablen Tarife, nicht für statische Arbeitspreise."""
    return tariff_type(config) in HEAT_PRICE_BOOST_TARIFF_TYPES


def parse_special_tariff_schedule(raw):
    """Liefert sortierte Paare aus Tagesminute und Preis in ct/kWh."""
    text = str(raw or "").strip()
    if not text:
        return []

    entries = {}
    pattern = re.compile(r"(?<!\d)([0-2]?\d)(?:[:.](\d{1,2}))?\s+(-?\d+(?:[,.]\d+)?)")
    for match in pattern.finditer(text):
        try:
            hour = int(match.group(1))
            minute = int(match.group(2) or 0)
            price = float(match.group(3).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if hour < 0 or hour > 23 or minute < 0 or minute > 59:
            continue
        entries[hour * 60 + minute] = price

    return sorted(entries.items(), key=lambda item: item[0])


def special_tariff_price_for_datetime(raw, value, default_price):
    """Löst den aktiven Sondertarifpreis für einen lokalen Zeitpunkt auf."""
    schedule = parse_special_tariff_schedule(raw)
    if not schedule:
        return float(default_price)

    minute_of_day = value.hour * 60 + value.minute
    active_price = schedule[-1][1]
    for start_minute, price in schedule:
        if minute_of_day < start_minute:
            break
        active_price = price
    return active_price


def configured_billing_price_for_timestamp(config, now_ts=None):
    """Liefert den lokal definierten Abrechnungspreis in ct/kWh."""
    config = config or {}
    tariff = tariff_type(config)
    if tariff not in RECURRING_TARIFF_TYPES:
        return None

    basis = _safe_float(config.get("strompreis_basis", 25.0), 25.0)
    if tariff in ("static", "fix", "fixed", "flat"):
        return basis

    try:
        value = datetime.fromtimestamp(
            float(time.time() if now_ts is None else now_ts),
            tz=TARIFF_TIMEZONE,
        )
    except (OSError, OverflowError, TypeError, ValueError):
        return basis

    if tariff == "octopus_heat":
        cheap = _safe_float(config.get("strompreis_cheap", basis), basis)
        uht = _safe_float(config.get("strompreis_uht", basis), basis)
        if (2 <= value.hour < 6) or (12 <= value.hour < 16):
            return cheap
        if 18 <= value.hour < 21:
            return uht
        return basis

    return special_tariff_price_for_datetime(
        config.get("strompreis_spezial", ""),
        value,
        basis,
    )


def recurring_tariff_slots(
    config,
    *,
    now_ms=None,
    lookback_ms=60 * 60 * 1000,
    horizon_ms=48 * 60 * 60 * 1000,
    slot_ms=15 * 60 * 1000,
):
    """Materialisiert eine lokale Tarifachse ohne Börsenpreis-Projektion."""
    if not uses_recurring_tariff_axis(config):
        return []

    now_ms = int(time.time() * 1000) if now_ms is None else int(now_ms)
    slot_ms = max(60 * 1000, int(slot_ms))
    first_ms = ((now_ms - max(0, int(lookback_ms))) // slot_ms) * slot_ms
    last_ms = now_ms + max(slot_ms, int(horizon_ms))
    slots = []
    start_ms = first_ms
    while start_ms < last_ms:
        billing_price = configured_billing_price_for_timestamp(
            config,
            now_ts=start_ms / 1000.0,
        )
        if billing_price is not None:
            slots.append({
                "start_timestamp": start_ms,
                "end_timestamp": start_ms + slot_ms,
                "billing_price_ct": round(float(billing_price), 4),
                "price_source": "configured_tariff",
                "timezone": TARIFF_TIMEZONE_NAME,
                "price_resolution_min": int(round(slot_ms / 60000.0)),
                "source_resolution_min": int(round(slot_ms / 60000.0)),
            })
        start_ms += slot_ms
    return slots
