#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only E3/DC-Historienslots für die Prognosediagnose.

Das Modul enthält ausschließlich den belegten DB-History-Lesevertrag. Es
sendet keine EMS-, Speicher- oder Konfigurationsbefehle und schreibt selbst
keine Dateien.
"""

from __future__ import annotations

import math
import time
from typing import Any

try:
    from rscp_client import RscpTag, RscpType, find_tag
except ImportError:  # pragma: no cover - Paketimport
    from Installer.rscp_client import RscpTag, RscpType, find_tag


HISTORY_SLOT_SCHEMA = "e3dc_history_slot_v1"
HISTORY_SOURCE_CONTRACT = "e3dc_db_history_day_15m_v1"
HISTORY_INTERVAL_S = 15 * 60
HISTORY_SETTLING_DELAY_S = 60 * 60
HISTORY_DEFAULT_SLOT_COUNT = 16
HISTORY_MAX_SLOT_COUNT = 16
GRAPH_INDEX_TOLERANCE = 0.01
INVALID_NUMERIC_SENTINEL_FLOOR = float(0xFFFFFF00)
ENERGY_SUM_ABS_TOLERANCE_WH = 0.05
ENERGY_SUM_REL_TOLERANCE = 1e-6


def _uint64(tag: int, value: int) -> dict[str, Any]:
    return {"tag": tag, "type": RscpType.Uint64, "value": int(value)}


def _container(tag: int, children: list[dict[str, Any]]) -> dict[str, Any]:
    return {"tag": tag, "type": RscpType.Container, "value": children}


def build_history_request(time_start_utc_s: int, slot_count: int) -> list[dict[str, Any]]:
    """Baut genau eine read-only Tageshistorienanfrage für 15-Minuten-Slots."""

    start = int(time_start_utc_s)
    count = int(slot_count)
    if start < 0 or start % HISTORY_INTERVAL_S != 0:
        raise ValueError("time_start_utc_s muss ein ausgerichteter positiver UTC-Slotstart sein")
    if count < 1 or count > HISTORY_MAX_SLOT_COUNT:
        raise ValueError(f"slot_count muss zwischen 1 und {HISTORY_MAX_SLOT_COUNT} liegen")
    return [
        _container(
            RscpTag.DB_REQ_HISTORY_DATA_DAY,
            [
                _uint64(RscpTag.DB_REQ_HISTORY_TIME_START, start),
                _uint64(RscpTag.DB_REQ_HISTORY_TIME_INTERVAL, HISTORY_INTERVAL_S),
                _uint64(RscpTag.DB_REQ_HISTORY_TIME_SPAN, count * HISTORY_INTERVAL_S),
            ],
        )
    ]


def _finite_history_value(
    item: Any,
    *,
    expected_type: int | None = None,
) -> tuple[float | None, str]:
    if not isinstance(item, dict):
        return None, "value_missing"
    if item.get("type") == RscpType.Error:
        return None, "value_error"
    if expected_type is not None and item.get("type") != expected_type:
        return None, "value_unexpected_rscp_type"
    value = item.get("value")
    if isinstance(value, bool):
        return None, "value_invalid_type"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, "value_invalid_type"
    if not math.isfinite(number):
        return None, "value_non_finite"
    # Float32 kann 0xFFFFFFFF auf 4294967296 runden. Der enge obere
    # Sentinelbereich liegt weit außerhalb jeder physikalischen PV-Leistung.
    if number >= INVALID_NUMERIC_SENTINEL_FLOOR:
        return None, "value_invalid_sentinel"
    if number < 0.0:
        return None, "value_negative"
    return number, "ok"


def _container_index(children: list[dict[str, Any]]) -> tuple[int | None, str]:
    item = find_tag(children, RscpTag.DB_GRAPH_INDEX)
    if not isinstance(item, dict) or item.get("type") == RscpType.Error:
        return None, "graph_index_missing"
    if item.get("type") != RscpType.Float32:
        return None, "graph_index_not_float32"
    raw = item.get("value")
    if isinstance(raw, bool):
        return None, "graph_index_invalid_type"
    try:
        number = float(raw)
    except (TypeError, ValueError):
        return None, "graph_index_invalid_type"
    if not math.isfinite(number):
        return None, "graph_index_non_finite"
    rounded = int(round(number))
    if abs(number - rounded) > GRAPH_INDEX_TOLERANCE:
        return None, "graph_index_not_integral"
    return rounded, "ok"


def _missing_slot(start_utc_s: int, reason: str) -> dict[str, Any]:
    return {
        "schema_version": HISTORY_SLOT_SCHEMA,
        "source_contract": HISTORY_SOURCE_CONTRACT,
        "slot_start_utc_s": int(start_utc_s),
        "slot_end_utc_s": int(start_utc_s) + HISTORY_INTERVAL_S,
        "interval_s": HISTORY_INTERVAL_S,
        "e3dc_dc_mean_w": None,
        "e3dc_dc_energy_wh": None,
        "valid": False,
        "reason": str(reason or "value_missing"),
        "history_contract_valid": False,
    }


def _sum_dc_energy_wh(root_children: list[dict[str, Any]]) -> tuple[float | None, str]:
    summary = find_tag(root_children, RscpTag.DB_SUM_CONTAINER)
    if not isinstance(summary, dict):
        return None, "sum_container_missing"
    if summary.get("type") != RscpType.Container:
        return None, "sum_container_not_container"
    if not isinstance(summary.get("value"), list):
        return None, "sum_container_invalid"
    return _finite_history_value(
        find_tag(summary["value"], RscpTag.DB_DC_POWER),
        expected_type=RscpType.Float32,
    )


def parse_history_response(
    response: list[dict[str, Any]],
    *,
    time_start_utc_s: int,
    slot_count: int,
) -> list[dict[str, Any]]:
    """Dekodiert die belegten Indizes 1..N in lückenlose UTC-Slots.

    Index 0 ist der vom Gerät gelieferte Null-Platzhalter. Die Werte der
    gültigen Container sind mittlere Watt; die Slotenergie ergibt sich daher
    aus ``mean_w * 0.25 h``.
    """

    start = int(time_start_utc_s)
    count = int(slot_count)
    build_history_request(start, count)  # gemeinsame Eingabevalidierung
    root = find_tag(response or [], RscpTag.DB_HISTORY_DATA_DAY)
    if not isinstance(root, dict):
        return [
            _missing_slot(start + offset * HISTORY_INTERVAL_S, "history_root_missing")
            for offset in range(count)
        ]
    if root.get("type") != RscpType.Container:
        return [
            _missing_slot(start + offset * HISTORY_INTERVAL_S, "history_root_not_container")
            for offset in range(count)
        ]
    if not isinstance(root.get("value"), list):
        return [
            _missing_slot(start + offset * HISTORY_INTERVAL_S, "history_root_invalid")
            for offset in range(count)
        ]

    value_containers = [
        item
        for item in root["value"]
        if isinstance(item, dict) and item.get("tag") == RscpTag.DB_VALUE_CONTAINER
    ]
    if len(value_containers) != count + 1:
        return [
            _missing_slot(
                start + offset * HISTORY_INTERVAL_S,
                "value_container_count_mismatch",
            )
            for offset in range(count)
        ]

    indexed: dict[int, dict[str, Any]] = {}
    duplicates: set[int] = set()
    index_errors: list[str] = []
    for item in value_containers:
        if item.get("type") != RscpType.Container:
            index_errors.append("value_container_not_container")
            continue
        children = item.get("value")
        if not isinstance(children, list):
            index_errors.append("value_container_invalid")
            continue
        graph_index, index_reason = _container_index(children)
        if graph_index is None:
            index_errors.append(index_reason)
            continue
        if graph_index < 0 or graph_index > count:
            index_errors.append("graph_index_out_of_range")
            continue
        if graph_index in indexed:
            duplicates.add(graph_index)
            continue
        indexed[graph_index] = item

    if index_errors:
        reason = sorted(index_errors)[0]
        return [
            _missing_slot(start + offset * HISTORY_INTERVAL_S, reason)
            for offset in range(count)
        ]
    if duplicates:
        return [
            _missing_slot(start + offset * HISTORY_INTERVAL_S, "graph_index_duplicate")
            for offset in range(count)
        ]
    if set(indexed) != set(range(0, count + 1)):
        return [
            _missing_slot(
                start + offset * HISTORY_INTERVAL_S,
                "graph_index_sequence_incomplete",
            )
            for offset in range(count)
        ]

    slots: list[dict[str, Any]] = []
    for graph_index in range(1, count + 1):
        slot_start = start + (graph_index - 1) * HISTORY_INTERVAL_S
        item = indexed.get(graph_index)
        children = item.get("value") if isinstance(item, dict) else None
        if not isinstance(children, list):
            slots.append(_missing_slot(slot_start, "value_container_missing"))
            continue
        dc_item = find_tag(children, RscpTag.DB_DC_POWER)
        mean_w, reason = _finite_history_value(
            dc_item,
            expected_type=RscpType.Float32,
        )
        if mean_w is None:
            slots.append(_missing_slot(slot_start, reason))
            continue
        slots.append(
            {
                "schema_version": HISTORY_SLOT_SCHEMA,
                "source_contract": HISTORY_SOURCE_CONTRACT,
                "slot_start_utc_s": slot_start,
                "slot_end_utc_s": slot_start + HISTORY_INTERVAL_S,
                "interval_s": HISTORY_INTERVAL_S,
                "e3dc_dc_mean_w": round(mean_w, 6),
                "e3dc_dc_energy_wh": round(mean_w * HISTORY_INTERVAL_S / 3600.0, 6),
                "valid": True,
                "reason": "ok",
                "history_contract_valid": True,
            }
        )

    if not all(slot["valid"] for slot in slots):
        return slots

    summed_energy_wh, sum_reason = _sum_dc_energy_wh(root["value"])
    if summed_energy_wh is None:
        return [_missing_slot(slot["slot_start_utc_s"], sum_reason) for slot in slots]
    calculated_energy_wh = sum(float(slot["e3dc_dc_energy_wh"]) for slot in slots)
    if not math.isclose(
        summed_energy_wh,
        calculated_energy_wh,
        rel_tol=ENERGY_SUM_REL_TOLERANCE,
        abs_tol=ENERGY_SUM_ABS_TOLERANCE_WH,
    ):
        return [
            _missing_slot(slot["slot_start_utc_s"], "sum_container_energy_mismatch")
            for slot in slots
        ]
    return slots


def read_recent_closed_slots(
    connection: Any,
    *,
    now_utc_s: int | float | None = None,
    slot_count: int = HISTORY_DEFAULT_SLOT_COUNT,
    settling_delay_s: int = HISTORY_SETTLING_DELAY_S,
) -> list[dict[str, Any]]:
    """Liest abgeschlossene, mindestens ``settling_delay_s`` alte Slots."""

    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    delay_s = max(HISTORY_SETTLING_DELAY_S, int(settling_delay_s))
    eligible_end = ((now_s - delay_s) // HISTORY_INTERVAL_S) * HISTORY_INTERVAL_S
    count = int(slot_count)
    start = eligible_end - count * HISTORY_INTERVAL_S
    request = build_history_request(start, count)
    response = connection.request(request)
    return parse_history_response(
        response,
        time_start_utc_s=start,
        slot_count=count,
    )
