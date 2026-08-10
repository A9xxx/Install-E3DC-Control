#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unveränderliche, rein diagnostische Historie für den DV-Shadow.

Das Modul persistiert ausschließlich den bereits erzeugten, vollständigen
Shadow-Vertrag. Es kennt weder Konfiguration noch Treiber, Netzwerk, Dienste
oder RSCP und besitzt daher keinen Hardwareausgang. Pro Viertelstunde wird
mindestens ein Snapshot geschrieben; innerhalb derselben Viertelstunde entsteht
ein weiterer Snapshot nur bei einer fachlichen Plan- oder
Begründungsänderung beziehungsweise einer irreversiblen Archiv-Beweislücke.
Eingangsänderungen allein werden im nächsten festen Viertelstunden-Snapshot
übernommen. Dadurch bleiben auch A-B-A-Wechsel nachvollziehbar.

Die SQLite-Zeilen sind bis zur begrenzten Retention unveränderlich. Retention
entfernt ausschließlich vollständige, alte Snapshots; ``UPDATE`` ist per
Datenbank-Trigger verboten.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
import os
import re
import sqlite3
import time
import zlib
from typing import Any, Dict, Iterable, List, Optional
from urllib.parse import quote


HISTORY_SCHEMA = "dv_shadow_history_v1"
HISTORY_RECORD_SCHEMA = "dv_shadow_history_record_v1"
ARCHIVE_QUEUE_STATUS_SCHEMA = "dv_shadow_history_queue_status_v1"
OPERATION_MODE = "read_only_diagnostic"
COMPRESSION = "zlib_json_v1"

SLOT_DURATION_MS = 15 * 60 * 1000
MAX_SHADOW_SLOTS = 400
DEFAULT_RETENTION_DAYS = 14
MAX_RETENTION_DAYS = 31
DEFAULT_MAX_RECORDS = 4096
MAX_RECORDS_LIMIT = 8192
MAX_DATABASE_BYTES = 128 * 1024 * 1024
MAX_RECORD_BYTES = 4 * 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 750

STATE_DIR = "/var/www/html/data/dv_shadow_history"
DATABASE_PATH = os.path.join(STATE_DIR, "dv_shadow_history.db")

_TOKEN_RE = re.compile(r"^[A-Za-z0-9_.:/+\-]{1,192}$")
_REVISION_RE = re.compile(r"^sha256:[0-9a-fA-F]{64}$")

_INPUT_REVISION_KEYS = (
    "price",
    "pv_forecast",
    "load_forecast",
    "topology",
    "storage_state",
    "hardware_limits",
    "permissions",
    "planning_goals",
)
_STORAGE_NUMBER_KEYS = (
    "initial_soc_pct",
    "capacity_wh",
    "hard_reserve_soc_pct",
    "ceiling_soc_pct",
)
_HARDWARE_NUMBER_KEYS = (
    "max_charge_w",
    "max_discharge_w",
    "max_grid_import_w",
    "max_grid_export_w",
    "max_grid_charge_w",
    "max_economic_export_w",
)
_PERMISSION_KEYS = (
    "direct_marketing_enabled",
    "pv_store_enabled",
    "economic_export_enabled",
    "grid_charge_enabled",
    "external_ac_storage_enabled",
    "external_ac_fallback_supported",
    "dc_first_required",
)
_INPUT_SLOT_NUMBER_KEYS = (
    "buy_ct_kwh",
    "net_sell_ct_kwh",
    "pv_total_w",
    "e3dc_dc_pv_w",
    "external_ac_pv_w",
    "load_w",
)
_EXECUTION_NUMBER_KEYS = (
    "requested_power_w",
    "max_charge_w",
    "max_discharge_w",
)
_VALIDATION_NUMBER_KEYS = (
    "requested_power_w",
    "effective_charge_cap_w",
    "effective_discharge_w",
    "projected_battery_w",
    "projected_grid_w",
    "source_budget_w",
    "soc_start_pct",
    "soc_end_pct",
)


class ShadowHistoryContractError(RuntimeError):
    """Der übergebene Shadow oder die private Historie ist nicht sicher nutzbar."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _revision(value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, bool):
        return int(default)
    try:
        number = float(value)
    except (TypeError, ValueError):
        return int(default)
    if not math.isfinite(number):
        return int(default)
    return int(number)


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    return round(number, 6)


def _token(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not _TOKEN_RE.fullmatch(text):
        return None
    return text


def _valid_revision(value: Any) -> Optional[str]:
    text = str(value or "").strip()
    if not _REVISION_RE.fullmatch(text):
        return None
    return text.lower()


def _codes(values: Any, *, limit: int = 64) -> List[str]:
    if not isinstance(values, (list, tuple)):
        return []
    result: List[str] = []
    for value in values:
        code = _token(value)
        if code and code not in result:
            result.append(code)
        if len(result) >= limit:
            break
    return result


def _number_projection(source: Any, keys: Iterable[str]) -> Dict[str, Any]:
    mapping = source if isinstance(source, dict) else {}
    return {key: _finite(mapping.get(key)) for key in keys}


def _boolean_projection(source: Any, keys: Iterable[str]) -> Dict[str, bool]:
    mapping = source if isinstance(source, dict) else {}
    return {key: mapping.get(key) is True for key in keys}


def _normalize_archive_queue_status(value: Any) -> Dict[str, Any]:
    """Whitelistet ausschließlich beweisrelevante Queue-Telemetrie.

    Die Telemetrie beschreibt nur die diagnostische Archivpipeline. Sie darf
    weder Produktiventscheidungen beeinflussen noch beliebige Prozess- oder
    Anlagenkonfiguration in die private Historie übernehmen.
    """

    status = value if isinstance(value, dict) else {}
    dropped_total = max(0, _safe_int(status.get("dropped_total")))
    write_failures_total = max(
        0,
        _safe_int(status.get("write_failures_total")),
    )
    worker_start_failures_total = max(
        0,
        _safe_int(status.get("worker_start_failures_total")),
    )
    evidence_complete = (
        dropped_total == 0
        and write_failures_total == 0
        and worker_start_failures_total == 0
    )
    return {
        "schema_version": ARCHIVE_QUEUE_STATUS_SCHEMA,
        "status": "COMPLETE" if evidence_complete else "EVIDENCE_LIMIT",
        "capacity": max(0, _safe_int(status.get("capacity"))),
        "queue_depth": max(0, _safe_int(status.get("queue_depth"))),
        "accepted_total": max(0, _safe_int(status.get("accepted_total"))),
        "processed_total": max(0, _safe_int(status.get("processed_total"))),
        "dropped_total": dropped_total,
        "write_failures_total": write_failures_total,
        "worker_start_failures_total": worker_start_failures_total,
        "last_drop_at_ms": max(0, _safe_int(status.get("last_drop_at_ms"))),
        "last_dropped_snapshot_at_ms": max(
            0,
            _safe_int(status.get("last_dropped_snapshot_at_ms")),
        ),
        "last_write_failure_at_ms": max(
            0,
            _safe_int(status.get("last_write_failure_at_ms")),
        ),
        "last_worker_start_failure_at_ms": max(
            0,
            _safe_int(status.get("last_worker_start_failure_at_ms")),
        ),
        "worker_alive": status.get("worker_alive") is True,
        "evidence_complete": evidence_complete,
        "control_effect": False,
    }


def _archive_evidence_material(value: Dict[str, Any]) -> Dict[str, Any]:
    """Bindet nur irreversible Beweislücken, nicht normale Queue-Bewegungen."""

    return {
        key: copy.deepcopy(value.get(key))
        for key in (
            "dropped_total",
            "write_failures_total",
            "worker_start_failures_total",
            "last_drop_at_ms",
            "last_dropped_snapshot_at_ms",
            "last_write_failure_at_ms",
            "last_worker_start_failure_at_ms",
            "evidence_complete",
        )
    }


def _normalize_input_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
    normalized = {
        "input_slot_id": _valid_revision(slot.get("input_slot_id")),
        "start_ts_ms": _safe_int(slot.get("start_ts_ms")),
        "end_ts_ms": _safe_int(slot.get("end_ts_ms")),
        **_number_projection(slot, _INPUT_SLOT_NUMBER_KEYS),
        "price_fresh": slot.get("price_fresh") is True,
        "price_status": _token(slot.get("price_status")),
        "topology_status": _token(slot.get("topology_status")),
        "topology_complete": slot.get("topology_complete") is True,
        "topology_revision": _valid_revision(slot.get("topology_revision")),
        "pv_forecast_fresh": slot.get("pv_forecast_fresh") is True,
        "pv_forecast_freshness_source": _token(
            slot.get("pv_forecast_freshness_source")
        ),
        "load_forecast_valid": slot.get("load_forecast_valid") is True,
        "load_forecast_validity_source": _token(
            slot.get("load_forecast_validity_source")
        ),
    }
    return normalized


def _normalize_planning_input(payload: Dict[str, Any]) -> Dict[str, Any]:
    slots = [
        _normalize_input_slot(item)
        for item in (payload.get("slots") or [])
        if isinstance(item, dict)
    ]
    if not slots or len(slots) > MAX_SHADOW_SLOTS:
        raise ShadowHistoryContractError("planning_input_slot_count_invalid")

    storage = payload.get("storage") if isinstance(payload.get("storage"), dict) else {}
    goals = (
        payload.get("planning_goals")
        if isinstance(payload.get("planning_goals"), dict)
        else {}
    )
    adequacy = (
        payload.get("forecast_charge_adequacy")
        if isinstance(payload.get("forecast_charge_adequacy"), dict)
        else {}
    )
    limits = (
        payload.get("hardware_limits")
        if isinstance(payload.get("hardware_limits"), dict)
        else {}
    )
    permissions = (
        payload.get("permissions")
        if isinstance(payload.get("permissions"), dict)
        else {}
    )
    efficiency = (
        payload.get("efficiency")
        if isinstance(payload.get("efficiency"), dict)
        else {}
    )
    revisions = (
        payload.get("input_revisions")
        if isinstance(payload.get("input_revisions"), dict)
        else {}
    )
    return {
        "schema_version": _token(payload.get("schema_version")),
        "generated_at_ts_ms": _safe_int(payload.get("generated_at_ts_ms")),
        "valid_from_ts_ms": _safe_int(payload.get("valid_from_ts_ms")),
        "horizon_end_ts_ms": _safe_int(payload.get("horizon_end_ts_ms")),
        "timezone": _token(payload.get("timezone")),
        "slot_duration_s": _safe_int(payload.get("slot_duration_s")),
        "shadow_only": payload.get("shadow_only") is True,
        "commands_allowed": payload.get("commands_allowed") is True,
        "source_revisions": {
            key: _valid_revision(revisions.get(key))
            for key in _INPUT_REVISION_KEYS
        },
        "storage": {
            **_number_projection(storage, _STORAGE_NUMBER_KEYS),
            "state_fresh": storage.get("state_fresh") is True,
        },
        "planning_goals": {
            "target_soc_pct": _finite(goals.get("target_soc_pct")),
            "grid_charge_requires_forecast_deficit": (
                goals.get("grid_charge_requires_forecast_deficit") is True
            ),
        },
        "forecast_charge_adequacy": {
            "status": _token(adequacy.get("status")),
            "required_source_wh": _finite(adequacy.get("required_source_wh")),
            "forecast_dc_charge_potential_wh": _finite(
                adequacy.get("forecast_dc_charge_potential_wh")
            ),
            "forecast_charge_deficit_wh": _finite(
                adequacy.get("forecast_charge_deficit_wh")
            ),
        },
        "hardware_limits": _number_projection(limits, _HARDWARE_NUMBER_KEYS),
        "permissions": _boolean_projection(permissions, _PERMISSION_KEYS),
        "efficiency": {
            "charge": _finite(efficiency.get("charge")),
            "discharge": _finite(efficiency.get("discharge")),
        },
        "slots": slots,
    }


def _normalize_execution(value: Any) -> Dict[str, Any]:
    execution = value if isinstance(value, dict) else {}
    return {
        "class": _token(execution.get("class")),
        "mode": _token(execution.get("mode")),
        **_number_projection(execution, _EXECUTION_NUMBER_KEYS),
        "release_existing_dv_limits": (
            execution.get("release_existing_dv_limits") is True
        ),
        "would_require_runtime_command": (
            execution.get("would_require_runtime_command") is True
        ),
        "runtime_command_condition": _token(
            execution.get("runtime_command_condition")
        ),
        "steady_state_command_required": (
            execution.get("steady_state_command_required") is True
        ),
        "commands_allowed": execution.get("commands_allowed") is True,
    }


def _normalize_plan_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slot_id": _valid_revision(slot.get("slot_id")),
        "input_slot_id": _valid_revision(slot.get("input_slot_id")),
        "start_ts_ms": _safe_int(slot.get("start_ts_ms")),
        "end_ts_ms": _safe_int(slot.get("end_ts_ms")),
        "applies": slot.get("applies") is True,
        "action": _token(slot.get("action")),
        "purpose": _token(slot.get("purpose")),
        "reason_code": _token(slot.get("reason_code")),
        "mapping_blocker_code": _token(slot.get("mapping_blocker_code")),
        "source_action": _token(slot.get("source_action")),
        "source_window_revision": _valid_revision(
            slot.get("source_window_revision")
        ),
        "headroom_reservation_revision": _valid_revision(
            slot.get("headroom_reservation_revision")
        ),
        "protected_reserve_wh": _finite(slot.get("protected_reserve_wh")),
        "sellable_wh": _finite(slot.get("sellable_wh")),
        "charge_source_contract": _token(slot.get("charge_source_contract")),
        "execution": _normalize_execution(slot.get("execution")),
    }


def _normalize_plan(payload: Dict[str, Any]) -> Dict[str, Any]:
    slots = [
        _normalize_plan_slot(item)
        for item in (payload.get("slots") or [])
        if isinstance(item, dict)
    ]
    if not slots or len(slots) > MAX_SHADOW_SLOTS:
        raise ShadowHistoryContractError("dv_plan_slot_count_invalid")
    owner = (
        payload.get("owner_contract")
        if isinstance(payload.get("owner_contract"), dict)
        else {}
    )
    return {
        "schema_version": _token(payload.get("schema_version")),
        "algorithm": _token(payload.get("algorithm")),
        "upstream_plan_revision": _valid_revision(payload.get("plan_id")),
        "planning_input_revision": _valid_revision(
            payload.get("planning_input_id")
        ),
        "migration_action_source_revision": _valid_revision(
            payload.get("migration_action_source_revision")
        ),
        "generated_at_ts_ms": _safe_int(payload.get("generated_at_ts_ms")),
        "valid_from_ts_ms": _safe_int(payload.get("valid_from_ts_ms")),
        "horizon_end_ts_ms": _safe_int(payload.get("horizon_end_ts_ms")),
        "slot_duration_s": _safe_int(payload.get("slot_duration_s")),
        "shadow_only": payload.get("shadow_only") is True,
        "commands_allowed": payload.get("commands_allowed") is True,
        "complete": payload.get("complete") is True,
        "blockers": _codes(payload.get("blockers")),
        "owner_contract": {
            "planner_has_hardware_effect": (
                owner.get("planner_has_hardware_effect") is True
            ),
            "hardware_executor": _token(owner.get("hardware_executor")),
            "rscp_output_count": _safe_int(owner.get("rscp_output_count")),
        },
        "slots": slots,
    }


def _normalize_validation_slot(slot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "slot_id": _valid_revision(slot.get("slot_id")),
        "action": _token(slot.get("action")),
        "effective_action": _token(slot.get("effective_action")),
        "fallback_effect": _token(slot.get("fallback_effect")),
        "status": _token(slot.get("status")),
        "eligible": slot.get("eligible") is True,
        **_number_projection(slot, _VALIDATION_NUMBER_KEYS),
        "reject_codes": _codes(slot.get("reject_codes"), limit=32),
        "tighten_codes": _codes(slot.get("tighten_codes"), limit=32),
    }


def _normalize_reasons(
    plan: Dict[str, Any],
    validation: Dict[str, Any],
) -> Dict[str, Any]:
    summary = (
        validation.get("summary")
        if isinstance(validation.get("summary"), dict)
        else {}
    )
    validation_slots = []
    plan_slots = plan.get("slots") or []
    for index, item in enumerate(validation.get("slots") or []):
        if not isinstance(item, dict):
            continue
        normalized = _normalize_validation_slot(item)
        plan_slot = (
            plan_slots[index]
            if index < len(plan_slots) and isinstance(plan_slots[index], dict)
            else {}
        )
        normalized["start_ts_ms"] = plan_slot.get("start_ts_ms")
        normalized["end_ts_ms"] = plan_slot.get("end_ts_ms")
        validation_slots.append(normalized)
        if len(validation_slots) >= MAX_SHADOW_SLOTS:
            break
    return {
        "plan_blockers": _codes(plan.get("blockers")),
        "plan_slots": [
            {
                "slot_id": slot.get("slot_id"),
                "start_ts_ms": slot.get("start_ts_ms"),
                "end_ts_ms": slot.get("end_ts_ms"),
                "reason_code": slot.get("reason_code"),
                "mapping_blocker_code": slot.get("mapping_blocker_code"),
                "source_action": slot.get("source_action"),
                "source_window_revision": slot.get("source_window_revision"),
            }
            for slot in plan.get("slots") or []
        ],
        "validation": {
            "schema_version": _token(validation.get("schema_version")),
            "validator": _token(validation.get("validator")),
            "upstream_validation_revision": _valid_revision(
                validation.get("validation_id")
            ),
            "status": _token(validation.get("status")),
            "shadow_only": validation.get("shadow_only") is True,
            "commands_allowed": validation.get("commands_allowed") is True,
            "field_activation_ready": (
                validation.get("field_activation_ready") is True
            ),
            "reject_codes": _codes(validation.get("reject_codes")),
            "tighten_codes": _codes(validation.get("tighten_codes")),
            "summary": {
                key: _safe_int(summary.get(key))
                for key in ("slot_count", "valid", "tightened", "rejected")
            },
            "slots": validation_slots,
        },
    }


def _plan_revision_material(plan: Dict[str, Any]) -> Dict[str, Any]:
    """Projiziert ausschließlich die fachlich ausführbare Plansemantik.

    Die Herkunftsrevision des migrierten Produktivplans darf hier nicht
    einfließen. Sie ändert sich bei einer neuen Quellgeneration zusammen mit
    den upstream Plan-/Slot-IDs, obwohl Aktion, Leistung, Energie und
    Reserven unverändert sein können. Solche reinen Bindungswechsel sind kein
    fachlicher ``PLAN_CHANGED``-Beleg.
    """

    material = copy.deepcopy(plan)
    for key in (
        "upstream_plan_revision",
        "planning_input_revision",
        "migration_action_source_revision",
        "generated_at_ts_ms",
        "blockers",
    ):
        material.pop(key, None)
    for slot in material.get("slots") or []:
        for key in (
            "slot_id",
            "input_slot_id",
            "reason_code",
            "mapping_blocker_code",
            "source_action",
            "source_window_revision",
        ):
            slot.pop(key, None)
    return material


def _input_revision_material(planning_input: Dict[str, Any]) -> Dict[str, Any]:
    material = copy.deepcopy(planning_input)
    material.pop("generated_at_ts_ms", None)
    material.pop("source_revisions", None)
    for slot in material.get("slots") or []:
        slot.pop("input_slot_id", None)
    return material


def _reason_revision_material(reasons: Dict[str, Any]) -> Dict[str, Any]:
    material = copy.deepcopy(reasons)
    for slot in material.get("plan_slots") or []:
        # Die Fensterrevision ist wie die Slot-ID nur eine Herkunftsbindung.
        # Fachliche Gründe, Blocker und die konkrete Quellaktion bleiben
        # dagegen revisionswirksam.
        slot.pop("slot_id", None)
        slot.pop("source_window_revision", None)
    validation = (
        material.get("validation")
        if isinstance(material.get("validation"), dict)
        else {}
    )
    validation.pop("upstream_validation_revision", None)
    for slot in validation.get("slots") or []:
        # Identitäten und reine Physikprojektionen dürfen einen unveränderten
        # fachlichen Grund nicht in einen scheinbaren Grundwechsel verwandeln.
        for key in (
            "slot_id",
            "requested_power_w",
            "effective_charge_cap_w",
            "effective_discharge_w",
            "projected_battery_w",
            "projected_grid_w",
            "source_budget_w",
            "soc_start_pct",
            "soc_end_pct",
        ):
            slot.pop(key, None)
    return material


def _normalize_productive_context(value: Any) -> Dict[str, Any]:
    """Übernimmt nur die drei revisionssicheren Korrelationsfelder."""

    context = value if isinstance(value, dict) else {}
    valid_until = context.get("valid_until_ts_ms")
    if valid_until is None:
        valid_until = context.get("horizon_end_ts_ms")
    return {
        "plan_id": _valid_revision(context.get("plan_id")),
        "valid_from_ts_ms": _safe_int(context.get("valid_from_ts_ms")),
        "valid_until_ts_ms": _safe_int(valid_until),
    }


def build_shadow_history_record(
    shadow: Dict[str, Any],
    *,
    captured_at_ms: Optional[int] = None,
    productive_context: Optional[Dict[str, Any]] = None,
    archive_queue_status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verdichtet den vollständigen Shadow über eine feste Whitelist.

    Unbekannte Felder werden nie übernommen. Dadurch verändern Zugangsdaten,
    Hosts, Pfade oder sonstige private Konfiguration weder Payload noch
    Revisionen.
    """

    if not isinstance(shadow, dict):
        raise ShadowHistoryContractError("shadow_payload_invalid")
    if shadow.get("shadow_only") is not True:
        raise ShadowHistoryContractError("shadow_not_diagnostic")
    if shadow.get("commands_allowed") is not False:
        raise ShadowHistoryContractError("shadow_commands_must_be_disabled")

    input_payload = shadow.get("planning_input")
    plan_payload = shadow.get("dv_plan")
    validation_payload = shadow.get("physics_validation")
    if not all(
        isinstance(value, dict)
        for value in (input_payload, plan_payload, validation_payload)
    ):
        raise ShadowHistoryContractError("full_shadow_payload_required")

    planning_input = _normalize_planning_input(input_payload)
    plan = _normalize_plan(plan_payload)
    reasons = _normalize_reasons(plan, validation_payload)
    if (
        planning_input.get("schema_version") != "planning_input_v1"
        or plan.get("schema_version") != "dv_plan_v1"
        or planning_input.get("slot_duration_s") != 900
        or plan.get("slot_duration_s") != 900
        or planning_input.get("shadow_only") is not True
        or planning_input.get("commands_allowed") is not False
        or plan.get("shadow_only") is not True
        or plan.get("commands_allowed") is not False
        or plan.get("owner_contract", {}).get("planner_has_hardware_effect")
        is not False
        or plan.get("owner_contract", {}).get("hardware_executor")
        != "storage_manager"
        or plan.get("owner_contract", {}).get("rscp_output_count") != 1
        or shadow.get("runtime_owner") != "storage_manager"
        or validation_payload.get("shadow_only") is not True
        or validation_payload.get("commands_allowed") is not False
        or validation_payload.get("field_activation_ready") is not False
    ):
        raise ShadowHistoryContractError("shadow_safety_contract_invalid")

    capture_ms = _safe_int(
        captured_at_ms,
        _safe_int(planning_input.get("generated_at_ts_ms"), int(time.time() * 1000)),
    )
    if capture_ms <= 0:
        raise ShadowHistoryContractError("captured_at_invalid")
    interval_start_ms = (capture_ms // SLOT_DURATION_MS) * SLOT_DURATION_MS

    plan_revision = _revision(_plan_revision_material(plan))
    input_revision = _revision(_input_revision_material(planning_input))
    reason_revision = _revision(_reason_revision_material(reasons))
    archive_queue = _normalize_archive_queue_status(archive_queue_status)
    archive_evidence_revision = _revision(
        _archive_evidence_material(archive_queue)
    )
    identity = {
        "schema_version": HISTORY_RECORD_SCHEMA,
        "captured_at_ms": capture_ms,
        "interval_start_ms": interval_start_ms,
        "plan_revision": plan_revision,
        "input_revision": input_revision,
        "reason_revision": reason_revision,
        "archive_evidence_revision": archive_evidence_revision,
    }
    record = {
        **identity,
        "snapshot_id": _revision(identity),
        "interval_end_ms": interval_start_ms + SLOT_DURATION_MS,
        "operation_mode": OPERATION_MODE,
        "control_effect": False,
        "configuration_writes": False,
        "hardware_writes": False,
        "network_access": False,
        "runtime_owner": "storage_manager",
        "shadow_contract": {
            "schema_version": _token(shadow.get("schema_version")),
            "algorithm": _token(shadow.get("algorithm")),
            "status": _token(shadow.get("status")),
            "commands_allowed": False,
        },
        "upstream_revisions": {
            "planning_input": _valid_revision(
                shadow.get("planning_input_revision")
            ),
            "plan": _valid_revision(shadow.get("dv_plan_revision")),
            "physics_validation": _valid_revision(
                shadow.get("physics_validation_revision")
            ),
        },
        "productive_context": _normalize_productive_context(
            productive_context
        ),
        "archive_queue": archive_queue,
        "planning_input": planning_input,
        "plan": plan,
        "reasons": reasons,
    }
    record["record_revision"] = _revision(record)
    encoded = _canonical_json(record).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ShadowHistoryContractError("shadow_history_record_size_limit")
    return record


def _ensure_private_database_path(path: str, *, create: bool) -> None:
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute)
    if os.path.lexists(directory) and os.path.islink(directory):
        raise ShadowHistoryContractError("history_directory_symlink")
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    elif not os.path.isdir(directory):
        raise ShadowHistoryContractError("history_directory_missing")
    if os.path.lexists(absolute) and os.path.islink(absolute):
        raise ShadowHistoryContractError("history_database_symlink")
    if not create and not os.path.isfile(absolute):
        raise ShadowHistoryContractError("history_database_missing")
    if os.path.exists(absolute):
        if not os.path.isfile(absolute):
            raise ShadowHistoryContractError("history_database_not_regular")
        if os.path.getsize(absolute) > MAX_DATABASE_BYTES:
            raise ShadowHistoryContractError("history_database_size_limit")


def _connect(path: str, *, write: bool) -> sqlite3.Connection:
    _ensure_private_database_path(path, create=write)
    if write and not os.path.exists(path):
        flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags, 0o600)
        except FileExistsError:
            # Ein paralleler Diagnose-Writer darf die Datenbank zwischen
            # Prüfung und Anlage erzeugt haben; SQLite serialisiert danach.
            pass
        else:
            os.close(descriptor)
    if write:
        os.chmod(path, 0o600)
    if write:
        target = path
        uri = False
    else:
        target = f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro"
        uri = True
    connection = sqlite3.connect(
        target,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
        uri=uri,
    )
    connection.row_factory = sqlite3.Row
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    connection.execute("PRAGMA foreign_keys = ON")
    if write:
        connection.execute("PRAGMA synchronous = FULL")
    else:
        connection.execute("PRAGMA query_only = ON")
    return connection


def _initialize_schema(connection: sqlite3.Connection) -> None:
    statements = (
        """
        CREATE TABLE IF NOT EXISTS dv_shadow_history_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """,
        """
        CREATE TABLE IF NOT EXISTS dv_shadow_snapshots (
            snapshot_id TEXT PRIMARY KEY,
            captured_at_ms INTEGER NOT NULL,
            interval_start_ms INTEGER NOT NULL,
            interval_end_ms INTEGER NOT NULL,
            plan_revision TEXT NOT NULL,
            input_revision TEXT NOT NULL,
            reason_revision TEXT NOT NULL,
            change_kind TEXT NOT NULL,
            record_blob BLOB NOT NULL,
            record_bytes INTEGER NOT NULL,
            compression TEXT NOT NULL CHECK (compression = 'zlib_json_v1'),
            operation_mode TEXT NOT NULL
                CHECK (operation_mode = 'read_only_diagnostic'),
            control_effect INTEGER NOT NULL CHECK (control_effect = 0),
            configuration_writes INTEGER NOT NULL CHECK (configuration_writes = 0),
            hardware_writes INTEGER NOT NULL CHECK (hardware_writes = 0)
        )
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_dv_shadow_capture
        ON dv_shadow_snapshots(captured_at_ms, snapshot_id)
        """,
        """
        CREATE INDEX IF NOT EXISTS idx_dv_shadow_interval
        ON dv_shadow_snapshots(interval_start_ms, captured_at_ms)
        """,
        """
        CREATE TRIGGER IF NOT EXISTS dv_shadow_snapshots_no_update
        BEFORE UPDATE ON dv_shadow_snapshots
        BEGIN SELECT RAISE(ABORT, 'dv shadow snapshots are immutable'); END
        """,
    )
    for statement in statements:
        connection.execute(statement)
    connection.execute(
        """
        INSERT OR IGNORE INTO dv_shadow_history_meta(key, value)
        VALUES('schema_version', ?)
        """,
        (HISTORY_SCHEMA,),
    )
    row = connection.execute(
        """
        SELECT value FROM dv_shadow_history_meta
        WHERE key = 'schema_version'
        """
    ).fetchone()
    if row is None or str(row[0]) != HISTORY_SCHEMA:
        raise ShadowHistoryContractError("history_database_schema_mismatch")
    page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
    max_pages = max(1, MAX_DATABASE_BYTES // page_size)
    connection.execute(f"PRAGMA max_page_count = {max_pages}")


def _encode_record(record: Dict[str, Any]) -> tuple[bytes, int]:
    encoded = _canonical_json(record).encode("utf-8")
    if len(encoded) > MAX_RECORD_BYTES:
        raise ShadowHistoryContractError("shadow_history_record_size_limit")
    return zlib.compress(encoded, level=6), len(encoded)


def _decode_record(blob: bytes) -> Dict[str, Any]:
    try:
        encoded = zlib.decompress(blob)
        if len(encoded) > MAX_RECORD_BYTES:
            raise ShadowHistoryContractError("shadow_history_record_size_limit")
        value = json.loads(encoded.decode("utf-8"))
    except ShadowHistoryContractError:
        raise
    except Exception as exc:
        raise ShadowHistoryContractError("shadow_history_record_invalid") from exc
    if not isinstance(value, dict):
        raise ShadowHistoryContractError("shadow_history_record_invalid")
    return value


def _latest_record(connection: sqlite3.Connection) -> Optional[Dict[str, Any]]:
    row = connection.execute(
        """
        SELECT record_blob
        FROM dv_shadow_snapshots
        ORDER BY captured_at_ms DESC, snapshot_id DESC
        LIMIT 1
        """
    ).fetchone()
    return _decode_record(row[0]) if row is not None else None


def _slot_semantics(slot: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: copy.deepcopy(slot.get(key))
        for key in (
            "action",
            "purpose",
            "protected_reserve_wh",
            "sellable_wh",
            "charge_source_contract",
            "headroom_reservation_revision",
            "execution",
        )
    }


def _changed_slots(
    previous_plan: Dict[str, Any],
    current_plan: Dict[str, Any],
) -> List[Dict[str, Any]]:
    def indexed(plan: Dict[str, Any]) -> Dict[tuple[int, int], Dict[str, Any]]:
        return {
            (
                _safe_int(slot.get("start_ts_ms")),
                _safe_int(slot.get("end_ts_ms")),
            ): slot
            for slot in (plan.get("slots") or [])
            if isinstance(slot, dict)
        }

    before = indexed(previous_plan)
    after = indexed(current_plan)
    changes: List[Dict[str, Any]] = []
    for interval in sorted(set(before) | set(after)):
        old = before.get(interval)
        new = after.get(interval)
        if old is None:
            kind = "ADDED"
        elif new is None:
            kind = "REMOVED"
        elif _slot_semantics(old) != _slot_semantics(new):
            kind = "CHANGED"
        else:
            continue
        changes.append(
            {
                "start_ts_ms": interval[0],
                "end_ts_ms": interval[1],
                "kind": kind,
                "previous_action": old.get("action") if old else None,
                "current_action": new.get("action") if new else None,
                "previous_reason_code": (
                    old.get("reason_code") if old else None
                ),
                "current_reason_code": (
                    new.get("reason_code") if new else None
                ),
                "previous_headroom_reservation_revision": (
                    old.get("headroom_reservation_revision")
                    if old
                    else None
                ),
                "current_headroom_reservation_revision": (
                    new.get("headroom_reservation_revision")
                    if new
                    else None
                ),
            }
        )
        if len(changes) >= MAX_SHADOW_SLOTS:
            break
    return changes


def _change_contract(
    previous: Optional[Dict[str, Any]],
    current: Dict[str, Any],
) -> Dict[str, Any]:
    if previous is None:
        return {
            "kind": "INITIAL",
            "previous_snapshot_id": None,
            "plan_changed": False,
            "input_changed": False,
            "reason_changed": False,
            "archive_evidence_changed": False,
            "changed_slots": [],
        }
    plan_changed = previous.get("plan_revision") != current.get("plan_revision")
    input_changed = previous.get("input_revision") != current.get("input_revision")
    reason_changed = (
        previous.get("reason_revision") != current.get("reason_revision")
    )
    archive_evidence_changed = (
        previous.get("archive_evidence_revision")
        != current.get("archive_evidence_revision")
    )
    if plan_changed:
        kind = "PLAN_CHANGED"
    elif reason_changed:
        kind = "REASONS_CHANGED"
    elif archive_evidence_changed:
        kind = "ARCHIVE_EVIDENCE_GAP"
    elif previous.get("interval_start_ms") != current.get("interval_start_ms"):
        kind = "QUARTER_SNAPSHOT"
    else:
        kind = "INPUT_CHANGED"
    return {
        "kind": kind,
        "previous_snapshot_id": previous.get("snapshot_id"),
        "previous_plan_revision": previous.get("plan_revision"),
        "previous_input_revision": previous.get("input_revision"),
        "previous_reason_revision": previous.get("reason_revision"),
        "plan_changed": plan_changed,
        "input_changed": input_changed,
        "reason_changed": reason_changed,
        "archive_evidence_changed": archive_evidence_changed,
        "changed_slots": (
            _changed_slots(previous.get("plan") or {}, current.get("plan") or {})
            if plan_changed
            else []
        ),
    }


def _bounded_retention_days(value: Any) -> int:
    return max(1, min(MAX_RETENTION_DAYS, _safe_int(value, DEFAULT_RETENTION_DAYS)))


def _bounded_max_records(value: Any) -> int:
    return max(1, min(MAX_RECORDS_LIMIT, _safe_int(value, DEFAULT_MAX_RECORDS)))


def _enforce_retention_in_transaction(
    connection: sqlite3.Connection,
    *,
    now_ms: int,
    retention_days: int,
    max_records: int,
) -> int:
    cutoff_ms = int(now_ms) - int(retention_days) * 24 * 60 * 60 * 1000
    deleted = 0
    cursor = connection.execute(
        "DELETE FROM dv_shadow_snapshots WHERE captured_at_ms < ?",
        (cutoff_ms,),
    )
    deleted += max(0, int(cursor.rowcount))
    cursor = connection.execute(
        """
        DELETE FROM dv_shadow_snapshots
        WHERE snapshot_id IN (
            SELECT snapshot_id
            FROM dv_shadow_snapshots
            ORDER BY captured_at_ms DESC, snapshot_id DESC
            LIMIT -1 OFFSET ?
        )
        """,
        (int(max_records),),
    )
    deleted += max(0, int(cursor.rowcount))
    return deleted


def append_shadow_history(
    shadow: Dict[str, Any],
    *,
    database_path: str = DATABASE_PATH,
    captured_at_ms: Optional[int] = None,
    productive_context: Optional[Dict[str, Any]] = None,
    archive_queue_status: Optional[Dict[str, Any]] = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> Dict[str, Any]:
    """Schreibt atomar einen Viertelstunden- oder Änderungs-Snapshot.

    Fachliche Plan- und Begründungsänderungen sowie irreversible
    Archiv-Beweislücken werden innerhalb derselben Viertelstunde sofort
    protokolliert. Reine Eingangs-, Herkunfts- und Generationsänderungen werden
    bis zum nächsten festen Viertelstunden-Snapshot dedupliziert; ein
    fachlicher Wechsel zurück auf einen früheren Plan bleibt dagegen als
    A-B-A-Folge sichtbar.
    """

    record = build_shadow_history_record(
        shadow,
        captured_at_ms=captured_at_ms,
        productive_context=productive_context,
        archive_queue_status=archive_queue_status,
    )
    keep_days = _bounded_retention_days(retention_days)
    keep_records = _bounded_max_records(max_records)
    connection = _connect(database_path, write=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _initialize_schema(connection)
        deleted = _enforce_retention_in_transaction(
            connection,
            now_ms=record["captured_at_ms"],
            retention_days=keep_days,
            max_records=keep_records,
        )
        previous = _latest_record(connection)
        if (
            previous is not None
            and _safe_int(record.get("captured_at_ms"))
            < _safe_int(previous.get("captured_at_ms"))
        ):
            raise ShadowHistoryContractError("captured_at_before_latest_snapshot")
        same_interval = bool(
            previous is not None
            and previous.get("interval_start_ms") == record.get("interval_start_ms")
        )
        same_plan_and_reasons = bool(
            previous is not None
            and previous.get("plan_revision") == record.get("plan_revision")
            and previous.get("reason_revision") == record.get("reason_revision")
            and previous.get("archive_evidence_revision")
            == record.get("archive_evidence_revision")
        )
        if same_interval and same_plan_and_reasons:
            connection.commit()
            return {
                "inserted": False,
                "snapshot_id": previous.get("snapshot_id"),
                "reason": "quarter_snapshot_already_present",
                "input_revision_changed": (
                    previous.get("input_revision") != record.get("input_revision")
                ),
                "deleted_by_retention": deleted,
                "control_effect": False,
            }

        change = _change_contract(previous, record)
        record["change"] = change
        record["record_revision"] = _revision(
            {
                key: value
                for key, value in record.items()
                if key != "record_revision"
            }
        )
        blob, record_bytes = _encode_record(record)
        connection.execute(
            """
            INSERT INTO dv_shadow_snapshots(
                snapshot_id, captured_at_ms, interval_start_ms, interval_end_ms,
                plan_revision, input_revision, reason_revision, change_kind,
                record_blob, record_bytes, compression, operation_mode,
                control_effect, configuration_writes, hardware_writes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0, 0)
            """,
            (
                record["snapshot_id"],
                record["captured_at_ms"],
                record["interval_start_ms"],
                record["interval_end_ms"],
                record["plan_revision"],
                record["input_revision"],
                record["reason_revision"],
                change["kind"],
                blob,
                record_bytes,
                COMPRESSION,
                OPERATION_MODE,
            ),
        )
        deleted += _enforce_retention_in_transaction(
            connection,
            now_ms=record["captured_at_ms"],
            retention_days=keep_days,
            max_records=keep_records,
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
        if os.path.exists(database_path):
            os.chmod(database_path, 0o600)
    return {
        "inserted": True,
        "snapshot_id": record["snapshot_id"],
        "change_kind": change["kind"],
        "deleted_by_retention": deleted,
        "control_effect": False,
    }


def enforce_shadow_history_retention(
    *,
    database_path: str = DATABASE_PATH,
    now_ms: Optional[int] = None,
    retention_days: int = DEFAULT_RETENTION_DAYS,
    max_records: int = DEFAULT_MAX_RECORDS,
) -> Dict[str, Any]:
    current_ms = _safe_int(now_ms, int(time.time() * 1000))
    connection = _connect(database_path, write=True)
    try:
        connection.execute("BEGIN IMMEDIATE")
        _initialize_schema(connection)
        deleted = _enforce_retention_in_transaction(
            connection,
            now_ms=current_ms,
            retention_days=_bounded_retention_days(retention_days),
            max_records=_bounded_max_records(max_records),
        )
        connection.commit()
    except Exception:
        if connection.in_transaction:
            connection.rollback()
        raise
    finally:
        connection.close()
        if os.path.exists(database_path):
            os.chmod(database_path, 0o600)
    return {"deleted": deleted, "control_effect": False}


def read_shadow_history(
    *,
    database_path: str = DATABASE_PATH,
    since_ms: Optional[int] = None,
    limit: int = 256,
) -> List[Dict[str, Any]]:
    """Liest die gespeicherten Snapshots ohne Migration oder Reparatur."""

    bounded_limit = max(1, min(2048, _safe_int(limit, 256)))
    connection = _connect(database_path, write=False)
    try:
        if since_ms is None:
            rows = connection.execute(
                """
                SELECT record_blob
                FROM (
                    SELECT record_blob, captured_at_ms, snapshot_id
                    FROM dv_shadow_snapshots
                    ORDER BY captured_at_ms DESC, snapshot_id DESC
                    LIMIT ?
                )
                ORDER BY captured_at_ms ASC, snapshot_id ASC
                """,
                (bounded_limit,),
            ).fetchall()
        else:
            rows = connection.execute(
                """
                SELECT record_blob
                FROM (
                    SELECT record_blob, captured_at_ms, snapshot_id
                    FROM dv_shadow_snapshots
                    WHERE captured_at_ms >= ?
                    ORDER BY captured_at_ms DESC, snapshot_id DESC
                    LIMIT ?
                )
                ORDER BY captured_at_ms ASC, snapshot_id ASC
                """,
                (_safe_int(since_ms), bounded_limit),
            ).fetchall()
        return [_decode_record(row[0]) for row in rows]
    finally:
        connection.close()
