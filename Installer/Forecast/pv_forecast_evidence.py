#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Private, rein diagnostische Evidenzablage für PV-Prognosen.

Das Modul gehört ausschließlich dem niedrig priorisierten Diagnose-Sidecar.
Es importiert weder Regelungs- noch Konfigurationsmodule und besitzt keinerlei
Schnittstelle für Hardwarebefehle oder automatische Modellauswahl.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterable
from urllib.parse import quote


EVIDENCE_STATE_DIR = "/var/lib/e3dc-control/forecast-evidence"
EVIDENCE_DB_PATH = os.path.join(EVIDENCE_STATE_DIR, "pv_forecast_evidence.db")
EVIDENCE_LOCK_PATH = os.path.join(EVIDENCE_STATE_DIR, "writer.lock")
SUMMARY_JSON_PATH = "/var/www/html/ramdisk/pv_forecast_diagnostic_summary.json"

EVIDENCE_SCHEMA = "pv_forecast_evidence_v4"
PREVIOUS_EVIDENCE_SCHEMA = "pv_forecast_evidence_v3"
LEGACY_EVIDENCE_SCHEMAS = {"pv_forecast_evidence_v2", PREVIOUS_EVIDENCE_SCHEMA}
SUMMARY_SCHEMA = "pv_forecast_diagnostic_summary_v4"
LEGACY_SUMMARY_SCHEMAS = {
    "pv_forecast_diagnostic_summary_v2",
    "pv_forecast_diagnostic_summary_v3",
}
CONTINUITY_SCHEMA = "pv_forecast_evidence_continuity_v1"
OPERATION_MODE = "read_only_diagnostic"
HISTORY_SOURCE_CONTRACT = "e3dc_db_history_day_15m_v1"
FORECAST_SOURCE_CONTRACT = "resource_forecast_ensemble_v1"
FORECAST_SIGNAL_CONTRACT = "pv_e3dc_dc"
FORECAST_VALUE_STAGE = "displayed_postprocessed"
FORECAST_DISTRIBUTION_TYPE = "deterministic_point"
FORECAST_ISSUE_SCHEMA = "pv_forecast_issue_v1"
FORECAST_SOURCE_COMPOSITION_SCHEMA = "pv_forecast_source_composition_v1"
FORECAST_PRODUCER = "pv_forecast_service"
FORECAST_PRODUCER_TIME_BASIS = "producer_output_generation_utc_v1"
FORECAST_FILE_TIME_BASIS = "forecast_file_mtime_v1"
SIDECAR_CAPTURE_TIME_BASIS = "sidecar_capture_v1"
DETERMINISTIC_REFERENCE_METHOD = (
    "previous_day_same_utc_slot_observed_before_issue_v1"
)

LEAD_TIME_BUCKETS = (
    {
        "bucket_id": "lead_0_2h",
        "label": "0–2 h",
        "min_minutes": 0,
        "max_minutes": 120,
    },
    {
        "bucket_id": "lead_2_6h",
        "label": "2–6 h",
        "min_minutes": 120,
        "max_minutes": 360,
    },
    {
        "bucket_id": "lead_6_24h",
        "label": "6–24 h",
        "min_minutes": 360,
        "max_minutes": 1440,
    },
    {
        "bucket_id": "lead_24_48h",
        "label": "24–48 h",
        "min_minutes": 1440,
        "max_minutes": 2880,
    },
    {
        "bucket_id": "lead_48_72h",
        "label": "48–72 h",
        "min_minutes": 2880,
        "max_minutes": 4320,
    },
)

ARCHIVE_MIN_INTERVAL_S = 6 * 60 * 60
SUMMARY_MIN_INTERVAL_S = 24 * 60 * 60
RAW_RETENTION_S = 90 * 24 * 60 * 60
EVALUATION_WINDOW_S = RAW_RETENTION_S
MIN_EVALUATION_DELAY_S = 60 * 60
MIN_RELEVANT_SLOTS = 96
MIN_RELEVANT_DAYS = 7
MIN_COMPARISON_COVERAGE_PCT = 100.0
YIELD_RELEVANT_ENERGY_WH = 25.0
MAX_DATABASE_BYTES = 256 * 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 750
SQLITE_BEGIN_DELAYS_S = (0.05, 0.15, 0.30)
MAX_SUMMARY_BYTES = 64 * 1024

DIAGNOSTIC_LABELS = {
    "trefferabweichung_wh": "Trefferabweichung",
    "richtungsversatz_wh": "Richtungsversatz",
    "quadratische_fehlerwurzel_wh": "Quadratische Fehlerwurzel (RMSE)",
    "persistenz_skill_score_pct": "Skill gegenüber Tagespersistenz",
    "energiegewichtete_gesamtabweichung_pct": "Energiegewichtete Gesamtabweichung",
    "vergleichsabdeckung_pct": "Vergleichsabdeckung",
}


class EvidenceLimitError(RuntimeError):
    """Die Evidenzgrenze wurde erreicht; weitere Schreibvorgänge bleiben aus."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_record(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _sha256_revision(value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def _finite_optional(value: Any, *, nonnegative: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0.0):
        return None
    return number


def _valid_revision(value: Any) -> str | None:
    revision = str(value or "").strip()
    if not revision.startswith("sha256:") or len(revision) != 71:
        return None
    digest = revision[7:]
    if any(char not in "0123456789abcdef" for char in digest.lower()):
        return None
    return "sha256:" + digest.lower()


def _read_local_forecast_method_revision() -> str | None:
    path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "pv_forecast_service.py")
    try:
        with open(path, "rb") as handle:
            return "sha256:" + hashlib.sha256(handle.read()).hexdigest()
    except OSError:
        return None


LOCAL_FORECAST_METHOD_REVISION = _read_local_forecast_method_revision()


def _local_forecast_method_revision() -> str | None:
    """Liefert den beim Modulimport gebundenen Methodenstand."""

    return LOCAL_FORECAST_METHOD_REVISION


def _contract_status(value: Any) -> str | None:
    status = str(value or "")
    return status if status in {"complete", "EVIDENCE_LIMIT"} else None


def _status_bound_revision(
    contract: dict[str, Any],
    field: str,
    status_field: str | None = None,
) -> str | None:
    status = _contract_status(contract.get(status_field or f"{field}_status"))
    revision = _valid_revision(contract.get(field))
    if status == "complete" and revision is not None:
        return revision
    if status == "EVIDENCE_LIMIT" and contract.get(field) is None:
        return None
    raise EvidenceLimitError(f"forecast_issue_{field}_invalid")


def _forecast_target_slot_material(slots: Iterable[dict[str, Any]]) -> list[dict[str, int]]:
    material: list[dict[str, int]] = []
    for slot in slots:
        if not isinstance(slot, dict):
            raise EvidenceLimitError("forecast_issue_slot_invalid")
        start_ms = _finite_optional(slot.get("start_timestamp"), nonnegative=True)
        end_ms = _finite_optional(slot.get("end_timestamp"), nonnegative=True)
        if start_ms is None or end_ms is None:
            raise EvidenceLimitError("forecast_issue_target_slot_invalid")
        start_s = int(round(start_ms / 1000.0))
        end_s = int(round(end_ms / 1000.0))
        if start_s <= 0 or start_s % 900 != 0 or end_s - start_s != 900:
            raise EvidenceLimitError("forecast_issue_target_slot_invalid")
        material.append({
            "slot_start_utc_s": start_s,
            "slot_end_utc_s": end_s,
        })
    material.sort(key=lambda item: (item["slot_start_utc_s"], item["slot_end_utc_s"]))
    if not material or any(
        later["slot_start_utc_s"] <= earlier["slot_start_utc_s"]
        for earlier, later in zip(material, material[1:])
    ):
        raise EvidenceLimitError("forecast_issue_target_slots_missing_or_duplicate")
    return material


def validate_forecast_issue_contract(
    slots: Iterable[dict[str, Any]],
    contract: dict[str, Any],
) -> dict[str, Any]:
    """Validiert den Producer-Vertrag ohne Zeit- oder Revisionsimputation."""

    slot_list = list(slots or ())
    if not isinstance(contract, dict):
        raise EvidenceLimitError("forecast_issue_contract_missing")
    expected_keys = {
        "schema_version", "status", "producer", "producer_issued_at_utc_s",
        "producer_issue_time_basis", "producer_issue_time_status",
        "model_revision", "model_revision_status", "method_revision",
        "method_revision_status", "postprocessing_revision",
        "postprocessing_revision_status", "topology_revision",
        "topology_revision_status", "source_composition",
        "source_composition_revision", "source_composition_status",
        "value_stage", "distribution_type", "declared_quantile",
        "quantile_convention", "target_slot_count",
        "target_slot_start_utc_s", "target_slot_end_utc_s",
        "target_slots_revision", "target_slots_status", "control_effect",
        "configuration_writes", "automatic_model_selection",
        "decision_use_allowed", "issue_id",
    }
    if set(contract) != expected_keys:
        raise EvidenceLimitError("forecast_issue_contract_shape_invalid")
    contract_issue_id = _valid_revision(contract.get("issue_id"))
    if contract_issue_id is None:
        raise EvidenceLimitError("forecast_issue_id_invalid")
    if (
        contract.get("schema_version") != FORECAST_ISSUE_SCHEMA
        or contract.get("producer") != FORECAST_PRODUCER
        or contract.get("producer_issue_time_basis") != FORECAST_PRODUCER_TIME_BASIS
        or contract.get("value_stage") != FORECAST_VALUE_STAGE
        or contract.get("distribution_type") != FORECAST_DISTRIBUTION_TYPE
        or contract.get("declared_quantile") is not None
        or contract.get("quantile_convention") is not None
        or contract.get("control_effect") is not False
        or contract.get("configuration_writes") is not False
        or contract.get("automatic_model_selection") is not False
        or contract.get("decision_use_allowed") is not False
    ):
        raise EvidenceLimitError("forecast_issue_contract_semantics_invalid")

    issue_status = _contract_status(contract.get("status"))
    producer_time_status = _contract_status(contract.get("producer_issue_time_status"))
    issued_value = contract.get("producer_issued_at_utc_s")
    issued = (
        int(issued_value)
        if isinstance(issued_value, int) and not isinstance(issued_value, bool)
        else 0
    )
    if not (
        (producer_time_status == "complete" and issued > 0)
        or (producer_time_status == "EVIDENCE_LIMIT" and issued_value is None)
    ):
        raise EvidenceLimitError("forecast_issue_producer_time_invalid")

    model_revision = _status_bound_revision(contract, "model_revision")
    method_revision = _status_bound_revision(contract, "method_revision")
    postprocessing_revision = _status_bound_revision(
        contract,
        "postprocessing_revision",
    )
    topology_revision = _status_bound_revision(contract, "topology_revision")
    source_revision = _status_bound_revision(
        contract,
        "source_composition_revision",
        "source_composition_status",
    )
    target_revision = _status_bound_revision(
        contract,
        "target_slots_revision",
        "target_slots_status",
    )
    local_method_revision = _local_forecast_method_revision()
    if method_revision is not None and (
        local_method_revision is None or method_revision != local_method_revision
    ):
        raise EvidenceLimitError("forecast_issue_method_revision_mismatch")

    source_composition = contract.get("source_composition")
    if not isinstance(source_composition, dict) or set(source_composition) != {
        "schema_version",
        "sources",
    }:
        raise EvidenceLimitError("forecast_issue_source_composition_invalid")
    if source_composition.get("schema_version") != FORECAST_SOURCE_COMPOSITION_SCHEMA:
        raise EvidenceLimitError("forecast_issue_source_composition_schema_invalid")
    sources = source_composition.get("sources")
    if not isinstance(sources, list) or len(sources) != 3:
        raise EvidenceLimitError("forecast_issue_sources_invalid")
    expected_providers = {
        "m1": "forecast_solar",
        "m2": "open_meteo_icon_ecmwf_ensemble",
        "m3": "solcast",
    }
    for expected_model_id, source in zip(("m1", "m2", "m3"), sources):
        if not isinstance(source, dict) or set(source) != {
            "model_id", "provider", "configured", "available", "fresh",
            "freshness_source", "model_input_revision", "resource_input_revision",
        }:
            raise EvidenceLimitError("forecast_issue_source_shape_invalid")
        if (
            source.get("model_id") != expected_model_id
            or source.get("provider") != expected_providers[expected_model_id]
            or not isinstance(source.get("configured"), bool)
            or not isinstance(source.get("available"), bool)
            or not isinstance(source.get("fresh"), bool)
            or not isinstance(source.get("freshness_source"), str)
            or not str(source.get("freshness_source"))
            or len(str(source.get("freshness_source"))) > 96
            or _valid_revision(source.get("model_input_revision")) is None
            or _valid_revision(source.get("resource_input_revision")) is None
        ):
            raise EvidenceLimitError("forecast_issue_source_invalid")
    if source_revision != _sha256_revision(source_composition):
        raise EvidenceLimitError("forecast_issue_source_revision_mismatch")
    if model_revision is not None:
        raise EvidenceLimitError("forecast_issue_model_revision_unproven")

    target_slots = _forecast_target_slot_material(slot_list)
    if target_revision != _sha256_revision(target_slots):
        raise EvidenceLimitError("forecast_issue_target_revision_mismatch")
    target_count = contract.get("target_slot_count")
    target_start = contract.get("target_slot_start_utc_s")
    target_end = contract.get("target_slot_end_utc_s")
    if (
        not isinstance(target_count, int)
        or isinstance(target_count, bool)
        or target_count != len(target_slots)
        or target_start != target_slots[0]["slot_start_utc_s"]
        or target_end != target_slots[-1]["slot_end_utc_s"]
    ):
        raise EvidenceLimitError("forecast_issue_target_range_mismatch")

    embedded_contracts = [
        slot.get("forecast_issue_contract")
        for slot in slot_list
        if slot.get("forecast_issue_contract") is not None
    ]
    if (
        len(embedded_contracts) != 1
        or _canonical_json(embedded_contracts[0]) != _canonical_json(contract)
    ):
        raise EvidenceLimitError("forecast_issue_contract_cardinality_invalid")
    clean_slots: list[dict[str, Any]] = []
    slot_topology_revisions: set[str] = set()
    for slot in slot_list:
        if slot.get("forecast_issue_id") != contract_issue_id:
            raise EvidenceLimitError("forecast_issue_id_mixed")
        clean_slot = dict(slot)
        clean_slot.pop("forecast_issue_contract", None)
        clean_slot.pop("forecast_issue_id", None)
        if (
            clean_slot.get("forecast_value_stage") != FORECAST_VALUE_STAGE
            or clean_slot.get("forecast_distribution_type") != FORECAST_DISTRIBUTION_TYPE
            or clean_slot.get("forecast_quantile_level") is not None
            or clean_slot.get("forecast_quantile_convention") is not None
            or clean_slot.get("pv_topology_status") != "bound"
            or clean_slot.get("pv_topology_source") != FORECAST_SOURCE_CONTRACT
        ):
            raise EvidenceLimitError("forecast_issue_slot_contract_invalid")
        slot_revision = _valid_revision(clean_slot.get("pv_topology_revision"))
        if slot_revision is None:
            raise EvidenceLimitError("forecast_issue_slot_topology_invalid")
        slot_topology_revisions.add(slot_revision)
        clean_slots.append(clean_slot)
    if len(slot_topology_revisions) != 1 or topology_revision not in slot_topology_revisions:
        raise EvidenceLimitError("forecast_issue_topology_revision_mismatch")
    expected_postprocessing_revision = _sha256_revision({
        "schema_version": "pv_forecast_postprocessed_payload_v1",
        "value_stage": FORECAST_VALUE_STAGE,
        "slots": clean_slots,
    })
    if postprocessing_revision != expected_postprocessing_revision:
        raise EvidenceLimitError("forecast_issue_postprocessing_revision_mismatch")

    component_statuses = (
        producer_time_status,
        contract.get("topology_revision_status"),
        contract.get("model_revision_status"),
        contract.get("method_revision_status"),
        contract.get("postprocessing_revision_status"),
        contract.get("source_composition_status"),
        contract.get("target_slots_status"),
    )
    expected_issue_status = (
        "complete"
        if all(status == "complete" for status in component_statuses)
        else "EVIDENCE_LIMIT"
    )
    if issue_status != expected_issue_status:
        raise EvidenceLimitError("forecast_issue_status_mismatch")
    material = dict(contract)
    issue_id = _valid_revision(material.pop("issue_id", None))
    if issue_id != _sha256_revision(material):
        raise EvidenceLimitError("forecast_issue_id_mismatch")
    return json.loads(_canonical_json(contract))


def extract_forecast_issue_contract(
    slots: Iterable[dict[str, Any]],
) -> dict[str, Any] | None:
    """Liest genau einen wiederholten Vertrag; alte Ausgaben bleiben explizit leer."""

    slot_list = list(slots or ())
    raw_contracts = [
        slot.get("forecast_issue_contract")
        for slot in slot_list
        if isinstance(slot, dict)
    ]
    present = [item for item in raw_contracts if item is not None]
    if not present:
        return None
    if len(present) != 1:
        raise EvidenceLimitError("forecast_issue_contract_cardinality_invalid")
    first = present[0]
    issue_id = _valid_revision(first.get("issue_id") if isinstance(first, dict) else None)
    if issue_id is None or any(
        slot.get("forecast_issue_id") != issue_id
        for slot in slot_list
    ):
        raise EvidenceLimitError("forecast_issue_id_mixed")
    return validate_forecast_issue_contract(slot_list, first)


def _ensure_private_location(database_path: str, *, create: bool = True) -> None:
    path = os.path.abspath(database_path)
    directory = os.path.dirname(path)
    if os.path.lexists(directory) and os.path.islink(directory):
        raise EvidenceLimitError("private_state_directory_symlink")
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    elif not os.path.isdir(directory):
        raise EvidenceLimitError("private_state_directory_missing")
    if os.path.lexists(path) and os.path.islink(path):
        raise EvidenceLimitError("private_database_symlink")
    if not create and not os.path.exists(path):
        raise EvidenceLimitError("private_database_missing")
    if os.path.exists(path) and not os.path.isfile(path):
        raise EvidenceLimitError("private_database_not_regular")
    if os.path.exists(path) and os.path.getsize(path) > MAX_DATABASE_BYTES:
        raise EvidenceLimitError("private_database_size_limit")


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS evidence_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forecast_issues (
            issue_id TEXT PRIMARY KEY,
            issued_at_utc_s INTEGER NOT NULL,
            topology_revision TEXT NOT NULL,
            operation_mode TEXT NOT NULL CHECK (operation_mode = 'read_only_diagnostic'),
            control_effect INTEGER NOT NULL CHECK (control_effect = 0),
            configuration_writes INTEGER NOT NULL CHECK (configuration_writes = 0),
            automatic_model_selection INTEGER NOT NULL CHECK (automatic_model_selection = 0),
            created_at_utc_s INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forecast_slots (
            issue_id TEXT NOT NULL REFERENCES forecast_issues(issue_id) ON DELETE CASCADE,
            slot_start_utc_s INTEGER NOT NULL,
            slot_end_utc_s INTEGER NOT NULL,
            predicted_e3dc_dc_energy_wh REAL,
            source_fresh INTEGER NOT NULL,
            source_quality TEXT NOT NULL,
            PRIMARY KEY (issue_id, slot_start_utc_s)
        );

        CREATE TABLE IF NOT EXISTS forecast_issue_provenance (
            issue_id TEXT PRIMARY KEY
                REFERENCES forecast_issues(issue_id) ON DELETE CASCADE,
            published_at_utc_s INTEGER NOT NULL,
            captured_at_utc_s INTEGER NOT NULL,
            issue_time_basis TEXT NOT NULL
                CHECK (
                    issue_time_basis IN (
                        'forecast_file_mtime_v1',
                        'sidecar_capture_v1'
                    )
                ),
            forecast_source_contract TEXT NOT NULL,
            forecast_signal_contract TEXT NOT NULL,
            value_stage TEXT NOT NULL,
            distribution_type TEXT NOT NULL
                CHECK (distribution_type = 'deterministic_point'),
            quantile_level REAL,
            quantile_convention TEXT,
            CHECK (
                quantile_level IS NULL
                AND quantile_convention IS NULL
            )
        );

        CREATE TABLE IF NOT EXISTS forecast_issue_contracts (
            issue_id TEXT PRIMARY KEY
                REFERENCES forecast_issues(issue_id) ON DELETE CASCADE,
            contract_status TEXT NOT NULL
                CHECK (contract_status IN ('complete', 'EVIDENCE_LIMIT')),
            producer_issued_at_utc_s INTEGER,
            producer_issue_time_status TEXT NOT NULL
                CHECK (producer_issue_time_status IN ('complete', 'EVIDENCE_LIMIT')),
            model_revision TEXT,
            model_revision_status TEXT NOT NULL,
            method_revision TEXT,
            method_revision_status TEXT NOT NULL,
            postprocessing_revision TEXT,
            postprocessing_revision_status TEXT NOT NULL,
            topology_revision TEXT,
            topology_revision_status TEXT NOT NULL,
            source_composition_revision TEXT,
            source_composition_status TEXT NOT NULL,
            source_composition_json TEXT NOT NULL,
            target_slot_count INTEGER NOT NULL,
            target_slot_start_utc_s INTEGER,
            target_slot_end_utc_s INTEGER,
            target_slots_revision TEXT,
            target_slots_status TEXT NOT NULL,
            contract_json TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS observed_slots (
            observation_id TEXT PRIMARY KEY,
            slot_start_utc_s INTEGER NOT NULL,
            slot_end_utc_s INTEGER NOT NULL,
            topology_revision TEXT NOT NULL,
            source_contract TEXT NOT NULL,
            actual_e3dc_dc_energy_wh REAL,
            valid INTEGER NOT NULL,
            reason TEXT NOT NULL,
            observed_at_utc_s INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS diagnostic_summaries (
            summary_id TEXT PRIMARY KEY,
            topology_revision TEXT NOT NULL,
            calculated_at_utc_s INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_forecast_issue_revision_time
            ON forecast_issues(topology_revision, issued_at_utc_s);
        CREATE INDEX IF NOT EXISTS idx_forecast_slot_time
            ON forecast_slots(slot_start_utc_s, slot_end_utc_s);
        CREATE INDEX IF NOT EXISTS idx_forecast_provenance_time
            ON forecast_issue_provenance(
                issue_time_basis,
                published_at_utc_s
            );
        CREATE INDEX IF NOT EXISTS idx_forecast_contract_revision_time
            ON forecast_issue_contracts(
                topology_revision,
                producer_issued_at_utc_s
            );
        CREATE INDEX IF NOT EXISTS idx_observed_revision_time
            ON observed_slots(topology_revision, slot_start_utc_s, observed_at_utc_s);
        CREATE INDEX IF NOT EXISTS idx_observed_summary_rank
            ON observed_slots(
                topology_revision,
                source_contract,
                slot_start_utc_s,
                valid DESC,
                observed_at_utc_s DESC,
                observation_id DESC
            );
        CREATE INDEX IF NOT EXISTS idx_summary_revision_time
            ON diagnostic_summaries(topology_revision, calculated_at_utc_s);

        CREATE TRIGGER IF NOT EXISTS forecast_issues_no_update
        BEFORE UPDATE ON forecast_issues
        BEGIN SELECT RAISE(ABORT, 'forecast_issues are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_slots_no_update
        BEFORE UPDATE ON forecast_slots
        BEGIN SELECT RAISE(ABORT, 'forecast_slots are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_issue_provenance_no_update
        BEFORE UPDATE ON forecast_issue_provenance
        BEGIN SELECT RAISE(ABORT, 'forecast issue provenance is immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_issue_contracts_no_update
        BEFORE UPDATE ON forecast_issue_contracts
        BEGIN SELECT RAISE(ABORT, 'forecast issue contracts are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS observed_slots_no_update
        BEFORE UPDATE ON observed_slots
        BEGIN SELECT RAISE(ABORT, 'observed_slots are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS diagnostic_summaries_no_update
        BEFORE UPDATE ON diagnostic_summaries
        BEGIN SELECT RAISE(ABORT, 'diagnostic_summaries are immutable'); END;
        """
    )
    schema_row = connection.execute(
        "SELECT value FROM evidence_meta WHERE key = 'schema_version'"
    ).fetchone()
    if schema_row is None:
        connection.execute(
            "INSERT INTO evidence_meta(key, value) VALUES('schema_version', ?)",
            (EVIDENCE_SCHEMA,),
        )
    elif str(schema_row[0]) in LEGACY_EVIDENCE_SCHEMAS:
        connection.execute(
            "UPDATE evidence_meta SET value = ? WHERE key = 'schema_version'",
            (EVIDENCE_SCHEMA,),
        )
    elif str(schema_row[0]) != EVIDENCE_SCHEMA:
        raise EvidenceLimitError("private_database_schema_mismatch")


def _schema_is_ready(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT value FROM evidence_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise
    if row is None:
        return False
    if str(row[0]) in LEGACY_EVIDENCE_SCHEMAS:
        return False
    if str(row[0]) != EVIDENCE_SCHEMA:
        raise EvidenceLimitError("private_database_schema_mismatch")
    required_objects = {
        "forecast_issues",
        "forecast_slots",
        "forecast_issue_provenance",
        "forecast_issue_contracts",
        "observed_slots",
        "diagnostic_summaries",
        "idx_forecast_provenance_time",
        "idx_forecast_contract_revision_time",
        "idx_observed_summary_rank",
        "forecast_issues_no_update",
        "forecast_slots_no_update",
        "forecast_issue_provenance_no_update",
        "forecast_issue_contracts_no_update",
        "observed_slots_no_update",
        "diagnostic_summaries_no_update",
    }
    present = {
        str(item[0])
        for item in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE name IN (
                'forecast_issues',
                'forecast_slots',
                'forecast_issue_provenance',
                'forecast_issue_contracts',
                'observed_slots',
                'diagnostic_summaries',
                'idx_forecast_provenance_time',
                'idx_forecast_contract_revision_time',
                'idx_observed_summary_rank',
                'forecast_issues_no_update',
                'forecast_slots_no_update',
                'forecast_issue_provenance_no_update',
                'forecast_issue_contracts_no_update',
                'observed_slots_no_update',
                'diagnostic_summaries_no_update'
            )
            """
        ).fetchall()
    }
    return required_objects == present


def _database_is_busy(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _new_database_connection(path: str, *, write: bool) -> sqlite3.Connection:
    if write:
        database_target = path
        use_uri = False
    else:
        database_target = (
            f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro&immutable=0"
        )
        use_uri = True
    connection = sqlite3.connect(
        database_target,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
        uri=use_uri,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return connection


def _prepare_writer_database(path: str) -> sqlite3.Connection:
    """Bindet die gesamte Writer-Initialisierung an einen begrenzten Retry.

    Auch ein rein lesender, bereits geöffneter SQLite-Client kann
    Initialisierungs-PRAGMAs oder den Schemaabgleich kurz blockieren. Deshalb
    gilt der Retry nicht erst für ``BEGIN IMMEDIATE``, sondern für die gesamte
    Writer-Vorbereitung. Jeder Fehlversuch wird vollständig geschlossen.
    """

    last_error: sqlite3.OperationalError | None = None
    attempt_delays = (0.0, *SQLITE_BEGIN_DELAYS_S)
    for attempt, delay_s in enumerate(attempt_delays):
        if delay_s > 0.0:
            time.sleep(delay_s)
        connection = _new_database_connection(path, write=True)
        if os.path.exists(path):
            os.chmod(path, 0o600)
        try:
            connection.execute("PRAGMA synchronous = NORMAL")
            # ``executescript`` beendet eine bestehende Transaktion implizit.
            # Der Schemaabgleich muss deshalb vor dem abschließenden
            # BEGIN IMMEDIATE liegen. Ein fertiges Schema wird nur gelesen und
            # erzeugt auch bei parallelen Diagnose-Lesern keinen DDL-Lock.
            if not _schema_is_ready(connection):
                _initialize_schema(connection)
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            max_pages = max(1, MAX_DATABASE_BYTES // page_size)
            connection.execute(f"PRAGMA max_page_count = {max_pages}")
            connection.execute("BEGIN IMMEDIATE")
            if not connection.in_transaction:
                raise sqlite3.OperationalError("writer_transaction_not_active")
            return connection
        except sqlite3.OperationalError as exc:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
            connection.close()
            if not _database_is_busy(exc):
                raise
            last_error = exc
            if attempt + 1 >= len(attempt_delays):
                break
        except sqlite3.DatabaseError:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
            connection.close()
            raise
    raise EvidenceLimitError("database_writer_busy") from last_error


def _open_database(path: str, *, write: bool) -> sqlite3.Connection:
    _ensure_private_location(path, create=write)
    if write:
        try:
            return _prepare_writer_database(path)
        except sqlite3.DatabaseError as exc:
            if "full" in str(exc).lower():
                raise EvidenceLimitError("private_database_size_limit") from exc
            raise
    connection = _new_database_connection(path, write=False)
    connection.execute("PRAGMA query_only = ON")
    return connection


@contextmanager
def database(path: str = EVIDENCE_DB_PATH, *, write: bool = False):
    connection = _open_database(path, write=write)
    try:
        yield connection
        if write:
            connection.commit()
    except sqlite3.DatabaseError as exc:
        if write:
            connection.rollback()
        if "full" in str(exc).lower():
            raise EvidenceLimitError("private_database_size_limit") from exc
        raise
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()
        if write and os.path.exists(path):
            os.chmod(path, 0o600)


def initialize_database(path: str = EVIDENCE_DB_PATH) -> None:
    with database(path, write=True):
        pass


@contextmanager
def single_writer_lock(lock_path: str = EVIDENCE_LOCK_PATH):
    """Erlaubt genau einen Diagnose-Writer pro privatem Zustandspfad."""

    directory = os.path.dirname(os.path.abspath(lock_path))
    if os.path.lexists(directory) and os.path.islink(directory):
        raise EvidenceLimitError("private_state_directory_symlink")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvidenceLimitError("single_writer_already_active") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _normalized_forecast_slot(slot: dict[str, Any]) -> dict[str, Any] | None:
    start_ms = _finite_optional(slot.get("start_timestamp"), nonnegative=True)
    end_ms = _finite_optional(slot.get("end_timestamp"), nonnegative=True)
    if start_ms is None or end_ms is None:
        return None
    start_s = int(round(start_ms / 1000.0))
    end_s = int(round(end_ms / 1000.0))
    if start_s <= 0 or start_s % 900 != 0 or end_s - start_s != 900:
        return None
    if str(slot.get("pv_topology_status") or "") != "bound":
        return None
    source_contract = str(slot.get("pv_topology_source") or "")
    if source_contract != FORECAST_SOURCE_CONTRACT:
        return None
    distribution_type = str(
        slot.get("forecast_distribution_type") or FORECAST_DISTRIBUTION_TYPE
    )
    if distribution_type != FORECAST_DISTRIBUTION_TYPE:
        return None
    if (
        slot.get("forecast_quantile_level") is not None
        or slot.get("forecast_quantile_convention") is not None
    ):
        return None
    dc_w = _finite_optional(slot.get("e3dc_dc_pv_w"), nonnegative=True)
    if dc_w is None:
        return None
    return {
        "slot_start_utc_s": start_s,
        "slot_end_utc_s": end_s,
        "predicted_e3dc_dc_energy_wh": round(dc_w * 0.25, 6),
        "source_fresh": 1 if slot.get("pv_forecast_fresh") is True else 0,
        "source_quality": str(slot.get("pv_topology_quality") or "unknown")[:80],
    }


def archive_forecast_snapshot(
    slots: Iterable[dict[str, Any]],
    *,
    forecast_issue_contract: dict[str, Any] | None = None,
    issued_at_utc_s: int | None = None,
    published_at_utc_s: int | None = None,
    captured_at_utc_s: int | None = None,
    issue_time_basis: str | None = None,
    database_path: str = EVIDENCE_DB_PATH,
    min_interval_s: int = ARCHIVE_MIN_INTERVAL_S,
) -> dict[str, Any]:
    """Archiviert höchstens alle sechs Stunden eine topologiegebundene Ausgabe.

    Nur ein vollständig validierter Producer-Vertrag darf die Vorlaufzeit
    begründen. Dateizeit und Sidecar-Erfassungszeit bleiben getrennte Legacy-
    Provenienz und werden nie als Producer-Ausgabezeit umgedeutet.
    """

    slot_list = [slot for slot in (slots or ()) if isinstance(slot, dict)]
    validated_contract: dict[str, Any] | None = None
    if forecast_issue_contract is not None:
        try:
            validated_contract = validate_forecast_issue_contract(
                slot_list,
                forecast_issue_contract,
            )
        except EvidenceLimitError as exc:
            return {"archived": False, "reason": str(exc)}

    explicitly_published = published_at_utc_s is not None
    if explicitly_published:
        published = int(published_at_utc_s)
    elif issued_at_utc_s is not None:
        published = int(issued_at_utc_s)
    else:
        published = int(time.time())
    captured = int(
        published
        if captured_at_utc_s is None
        else captured_at_utc_s
    )
    time_basis = str(
        issue_time_basis
        or (
            FORECAST_FILE_TIME_BASIS
            if explicitly_published
            else SIDECAR_CAPTURE_TIME_BASIS
        )
    )
    if time_basis not in {FORECAST_FILE_TIME_BASIS, SIDECAR_CAPTURE_TIME_BASIS}:
        return {"archived": False, "reason": "issue_time_basis_invalid"}
    if published <= 0 or captured <= 0 or published > captured + 300:
        return {"archived": False, "reason": "forecast_publication_time_invalid"}
    producer_issued = (
        int(validated_contract.get("producer_issued_at_utc_s") or 0)
        if validated_contract is not None
        else 0
    )
    if producer_issued > captured + 300:
        return {"archived": False, "reason": "forecast_producer_time_invalid"}
    archive_time = producer_issued if producer_issued > 0 else published

    normalized: list[dict[str, Any]] = []
    revisions: set[str] = set()
    for slot in slot_list:
        revision = _valid_revision(slot.get("pv_topology_revision"))
        item = _normalized_forecast_slot(slot)
        if revision is None or item is None:
            continue
        revisions.add(revision)
        normalized.append(item)
    if not normalized:
        return {"archived": False, "reason": "no_bound_forecast_slots"}
    if len(revisions) != 1:
        return {"archived": False, "reason": "topology_revision_missing_or_mixed"}
    revision = next(iter(revisions))
    normalized.sort(key=lambda item: item["slot_start_utc_s"])

    with database(database_path, write=True) as connection:
        if validated_contract is not None:
            last_issue = connection.execute(
                """
                SELECT contract.producer_issued_at_utc_s
                FROM forecast_issue_contracts AS contract
                WHERE contract.topology_revision = ?
                  AND contract.method_revision IS ?
                  AND contract.method_revision_status = ?
                  AND contract.producer_issued_at_utc_s IS NOT NULL
                ORDER BY contract.producer_issued_at_utc_s DESC, contract.issue_id DESC
                LIMIT 1
                """,
                (
                    revision,
                    validated_contract.get("method_revision"),
                    validated_contract.get("method_revision_status"),
                ),
            ).fetchone()
        else:
            last_issue = connection.execute(
                """
                SELECT issue.issued_at_utc_s
                FROM forecast_issues AS issue
                WHERE issue.topology_revision = ?
                  AND NOT EXISTS(
                      SELECT 1
                      FROM forecast_issue_contracts AS contract
                      WHERE contract.issue_id = issue.issue_id
                  )
                ORDER BY issue.issued_at_utc_s DESC, issue.issue_id DESC
                LIMIT 1
                """,
                (revision,),
            ).fetchone()
        if last_issue and archive_time - int(last_issue[0]) < max(
            ARCHIVE_MIN_INTERVAL_S,
            int(min_interval_s),
        ):
            return {
                "archived": False,
                "reason": "archive_cadence",
                "topology_revision": revision,
            }
        issue_material = {
            "schema_version": EVIDENCE_SCHEMA,
            "published_at_utc_s": published,
            "captured_at_utc_s": captured,
            "issue_time_basis": time_basis,
            "topology_revision": revision,
            "forecast_source_contract": FORECAST_SOURCE_CONTRACT,
            "forecast_signal_contract": FORECAST_SIGNAL_CONTRACT,
            "value_stage": FORECAST_VALUE_STAGE,
            "distribution_type": FORECAST_DISTRIBUTION_TYPE,
            "quantile_level": None,
            "quantile_convention": None,
            "slots": normalized,
        }
        issue_id = (
            str(validated_contract["issue_id"])
            if validated_contract is not None
            else _sha256_record("forecast", issue_material)
        )
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO forecast_issues(
                issue_id, issued_at_utc_s, topology_revision, operation_mode,
                control_effect, configuration_writes, automatic_model_selection,
                created_at_utc_s
            ) VALUES(?, ?, ?, ?, 0, 0, 0, ?)
            """,
            (issue_id, archive_time, revision, OPERATION_MODE, captured),
        )
        if cursor.rowcount:
            connection.execute(
                """
                INSERT INTO forecast_issue_provenance(
                    issue_id, published_at_utc_s, captured_at_utc_s,
                    issue_time_basis, forecast_source_contract,
                    forecast_signal_contract, value_stage, distribution_type,
                    quantile_level, quantile_convention
                ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, NULL, NULL)
                """,
                (
                    issue_id,
                    published,
                    captured,
                    time_basis,
                    FORECAST_SOURCE_CONTRACT,
                    FORECAST_SIGNAL_CONTRACT,
                    FORECAST_VALUE_STAGE,
                    FORECAST_DISTRIBUTION_TYPE,
                ),
            )
            if validated_contract is not None:
                connection.execute(
                    """
                    INSERT INTO forecast_issue_contracts(
                        issue_id, contract_status, producer_issued_at_utc_s,
                        producer_issue_time_status, model_revision,
                        model_revision_status, method_revision,
                        method_revision_status, postprocessing_revision,
                        postprocessing_revision_status, topology_revision,
                        topology_revision_status, source_composition_revision,
                        source_composition_status, source_composition_json,
                        target_slot_count, target_slot_start_utc_s,
                        target_slot_end_utc_s, target_slots_revision,
                        target_slots_status, contract_json
                    ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        issue_id,
                        validated_contract["status"],
                        validated_contract["producer_issued_at_utc_s"],
                        validated_contract["producer_issue_time_status"],
                        validated_contract["model_revision"],
                        validated_contract["model_revision_status"],
                        validated_contract["method_revision"],
                        validated_contract["method_revision_status"],
                        validated_contract["postprocessing_revision"],
                        validated_contract["postprocessing_revision_status"],
                        validated_contract["topology_revision"],
                        validated_contract["topology_revision_status"],
                        validated_contract["source_composition_revision"],
                        validated_contract["source_composition_status"],
                        _canonical_json(validated_contract["source_composition"]),
                        validated_contract["target_slot_count"],
                        validated_contract["target_slot_start_utc_s"],
                        validated_contract["target_slot_end_utc_s"],
                        validated_contract["target_slots_revision"],
                        validated_contract["target_slots_status"],
                        _canonical_json(validated_contract),
                    ),
                )
            connection.executemany(
                """
                INSERT INTO forecast_slots(
                    issue_id, slot_start_utc_s, slot_end_utc_s,
                    predicted_e3dc_dc_energy_wh, source_fresh, source_quality
                ) VALUES(
                    :issue_id, :slot_start_utc_s, :slot_end_utc_s,
                    :predicted_e3dc_dc_energy_wh, :source_fresh, :source_quality
                )
                """,
                [{"issue_id": issue_id, **item} for item in normalized],
            )
    return {
        "archived": True,
        "inserted": bool(cursor.rowcount),
        "issue_id": issue_id,
        "topology_revision": revision,
        "slot_count": len(normalized),
        "published_at_utc_s": published,
        "captured_at_utc_s": captured,
        "issue_time_basis": time_basis,
        "producer_issued_at_utc_s": producer_issued or None,
        "producer_issue_time_status": (
            validated_contract.get("producer_issue_time_status")
            if validated_contract is not None
            else "EVIDENCE_LIMIT"
        ),
        "forecast_issue_contract_status": (
            validated_contract.get("status")
            if validated_contract is not None
            else "EVIDENCE_LIMIT"
        ),
    }


def store_history_observations(
    history_slots: Iterable[dict[str, Any]],
    *,
    topology_revision: str,
    observed_at_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, Any]:
    """Speichert History nur für die ausdrücklich aktuelle Topologierevision."""

    revision = _valid_revision(topology_revision)
    if revision is None:
        return {"inserted": 0, "reason": "topology_revision_invalid"}
    observed = int(time.time() if observed_at_utc_s is None else observed_at_utc_s)
    records: list[tuple[Any, ...]] = []
    for slot in history_slots or ():
        if not isinstance(slot, dict):
            continue
        start_s = int(slot.get("slot_start_utc_s") or 0)
        end_s = int(slot.get("slot_end_utc_s") or 0)
        if start_s <= 0 or start_s % 900 != 0 or end_s - start_s != 900:
            continue
        source_contract = str(slot.get("source_contract") or "")
        valid = bool(
            slot.get("valid") is True
            and slot.get("history_contract_valid") is True
            and source_contract == HISTORY_SOURCE_CONTRACT
        )
        energy_wh = (
            _finite_optional(slot.get("e3dc_dc_energy_wh"), nonnegative=True)
            if valid
            else None
        )
        if energy_wh is None:
            valid = False
        reason = "ok" if valid else str(slot.get("reason") or "history_contract_invalid")[:80]
        material = {
            "slot_start_utc_s": start_s,
            "slot_end_utc_s": end_s,
            "topology_revision": revision,
            "source_contract": source_contract,
            "actual_e3dc_dc_energy_wh": energy_wh,
            "valid": valid,
            "reason": reason,
        }
        records.append(
            (
                _sha256_record("observation", material),
                start_s,
                end_s,
                revision,
                source_contract,
                energy_wh,
                1 if valid else 0,
                reason,
                observed,
            )
        )
    inserted = 0
    with database(database_path, write=True) as connection:
        for record in records:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO observed_slots(
                    observation_id, slot_start_utc_s, slot_end_utc_s,
                    topology_revision, source_contract,
                    actual_e3dc_dc_energy_wh, valid, reason, observed_at_utc_s
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS(
                    SELECT 1
                    FROM forecast_slots AS slot
                    JOIN forecast_issues AS issue ON issue.issue_id = slot.issue_id
                    WHERE slot.slot_start_utc_s = ?
                      AND issue.topology_revision = ?
                )
                """,
                (*record, record[1], revision),
            )
            inserted += max(0, int(cursor.rowcount))
    return {"inserted": inserted, "topology_revision": revision}


def enforce_retention(
    *,
    now_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, int]:
    """Löscht ausschließlich abgeschlossene Rohdaten außerhalb von 90 UTC-Tagen."""

    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    cutoff = now_s - RAW_RETENTION_S
    deleted = {"observations": 0, "forecasts": 0, "summaries": 0}
    with database(database_path, write=True) as connection:
        cursor = connection.execute(
            "DELETE FROM observed_slots WHERE slot_end_utc_s < ?",
            (cutoff,),
        )
        deleted["observations"] = max(0, int(cursor.rowcount))
        cursor = connection.execute(
            """
            DELETE FROM forecast_issues
            WHERE issue_id IN (
                SELECT issue.issue_id
                FROM forecast_issues AS issue
                LEFT JOIN forecast_slots AS slot ON slot.issue_id = issue.issue_id
                GROUP BY issue.issue_id
                HAVING MAX(COALESCE(slot.slot_end_utc_s, 0)) < ?
            )
            """,
            (cutoff,),
        )
        deleted["forecasts"] = max(0, int(cursor.rowcount))
        cursor = connection.execute(
            "DELETE FROM diagnostic_summaries WHERE calculated_at_utc_s < ?",
            (cutoff,),
        )
        deleted["summaries"] = max(0, int(cursor.rowcount))
    return deleted


def _provisional_reasons(
    relevant_slots: int,
    relevant_days: int,
    eligible_slots: int,
    compared_slots: int,
) -> list[str]:
    reasons: list[str] = []
    if relevant_slots < MIN_RELEVANT_SLOTS:
        reasons.append("zu_wenige_ertragsrelevante_slots")
    if relevant_days < MIN_RELEVANT_DAYS:
        reasons.append("zu_wenige_vergleichstage")
    coverage_pct = (
        compared_slots / eligible_slots * 100.0
        if eligible_slots > 0
        else 0.0
    )
    if coverage_pct < MIN_COMPARISON_COVERAGE_PCT:
        reasons.append("unvollstaendige_vergleichsabdeckung")
    return reasons


def _lead_time_case_sql() -> str:
    clauses = [
        (
            f"WHEN lead_minutes >= {int(bucket['min_minutes'])} "
            f"AND lead_minutes < {int(bucket['max_minutes'])} "
            f"THEN '{bucket['bucket_id']}'"
        )
        for bucket in LEAD_TIME_BUCKETS
    ]
    return "CASE " + " ".join(clauses) + " ELSE NULL END"


def _unavailable_issue_contract(
    topology_revision: str | None = None,
    reason: str = "producer_issue_contract_missing",
) -> dict[str, Any]:
    return {
        "schema_version": FORECAST_ISSUE_SCHEMA,
        "status": "EVIDENCE_LIMIT",
        "issue_id": None,
        "producer": FORECAST_PRODUCER,
        "producer_issued_at_utc_s": None,
        "producer_issue_time_basis": FORECAST_PRODUCER_TIME_BASIS,
        "producer_issue_time_status": "EVIDENCE_LIMIT",
        "model_revision": None,
        "model_revision_status": "EVIDENCE_LIMIT",
        "method_revision": None,
        "method_revision_status": "EVIDENCE_LIMIT",
        "postprocessing_revision": None,
        "postprocessing_revision_status": "EVIDENCE_LIMIT",
        "topology_revision": None,
        "topology_revision_status": "EVIDENCE_LIMIT",
        "source_composition": {
            "schema_version": FORECAST_SOURCE_COMPOSITION_SCHEMA,
            "sources": [],
        },
        "source_composition_revision": None,
        "source_composition_status": "EVIDENCE_LIMIT",
        "value_stage": FORECAST_VALUE_STAGE,
        "distribution_type": FORECAST_DISTRIBUTION_TYPE,
        "declared_quantile": None,
        "quantile_convention": None,
        "target_slot_count": 0,
        "target_slot_start_utc_s": None,
        "target_slot_end_utc_s": None,
        "target_slots_revision": None,
        "target_slots_status": "EVIDENCE_LIMIT",
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "decision_use_allowed": False,
        "reason": str(reason or "producer_issue_contract_missing")[:80],
    }


def _public_issue_contract(
    contract: dict[str, Any] | None,
    topology_revision: str | None = None,
) -> dict[str, Any]:
    if not isinstance(contract, dict):
        return _unavailable_issue_contract(topology_revision)
    raw_composition = (
        contract.get("source_composition")
        if isinstance(contract.get("source_composition"), dict)
        else {}
    )
    sources: list[dict[str, Any]] = []
    for source in raw_composition.get("sources") or []:
        if not isinstance(source, dict):
            continue
        model_id = str(source.get("model_id") or "")
        provider = str(source.get("provider") or "")
        if model_id not in {"m1", "m2", "m3"} or not provider:
            continue
        sources.append({
            "model_id": model_id,
            "provider": provider[:64],
            "configured": source.get("configured") is True,
            "available": source.get("available") is True,
            "fresh": source.get("fresh") is True,
            "freshness_source": str(
                source.get("freshness_source") or "model_provenance_unknown"
            )[:96],
            "model_input_revision": _valid_revision(
                source.get("model_input_revision")
            ),
            "resource_input_revision": _valid_revision(
                source.get("resource_input_revision")
            ),
        })

    def safe_status(field: str) -> str:
        return _contract_status(contract.get(field)) or "EVIDENCE_LIMIT"

    producer_time = contract.get("producer_issued_at_utc_s")
    if not isinstance(producer_time, int) or isinstance(producer_time, bool) or producer_time <= 0:
        producer_time = None
    target_count = contract.get("target_slot_count")
    if not isinstance(target_count, int) or isinstance(target_count, bool) or target_count < 0:
        target_count = 0
    return {
        "schema_version": FORECAST_ISSUE_SCHEMA,
        "status": safe_status("status"),
        "issue_id": _valid_revision(contract.get("issue_id")),
        "producer": FORECAST_PRODUCER,
        "producer_issued_at_utc_s": producer_time,
        "producer_issue_time_basis": FORECAST_PRODUCER_TIME_BASIS,
        "producer_issue_time_status": safe_status("producer_issue_time_status"),
        "model_revision": _valid_revision(contract.get("model_revision")),
        "model_revision_status": safe_status("model_revision_status"),
        "method_revision": _valid_revision(contract.get("method_revision")),
        "method_revision_status": safe_status("method_revision_status"),
        "postprocessing_revision": _valid_revision(
            contract.get("postprocessing_revision")
        ),
        "postprocessing_revision_status": safe_status(
            "postprocessing_revision_status"
        ),
        "topology_revision": _valid_revision(contract.get("topology_revision")),
        "topology_revision_status": safe_status("topology_revision_status"),
        "source_composition": {
            "schema_version": FORECAST_SOURCE_COMPOSITION_SCHEMA,
            "sources": sources,
        },
        "source_composition_revision": _valid_revision(
            contract.get("source_composition_revision")
        ),
        "source_composition_status": safe_status("source_composition_status"),
        "value_stage": FORECAST_VALUE_STAGE,
        "distribution_type": FORECAST_DISTRIBUTION_TYPE,
        "declared_quantile": None,
        "quantile_convention": None,
        "target_slot_count": target_count,
        "target_slot_start_utc_s": (
            int(contract["target_slot_start_utc_s"])
            if isinstance(contract.get("target_slot_start_utc_s"), int)
            and not isinstance(contract.get("target_slot_start_utc_s"), bool)
            else None
        ),
        "target_slot_end_utc_s": (
            int(contract["target_slot_end_utc_s"])
            if isinstance(contract.get("target_slot_end_utc_s"), int)
            and not isinstance(contract.get("target_slot_end_utc_s"), bool)
            else None
        ),
        "target_slots_revision": _valid_revision(
            contract.get("target_slots_revision")
        ),
        "target_slots_status": safe_status("target_slots_status"),
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "decision_use_allowed": False,
    }


def _latest_issue_contract(
    connection: sqlite3.Connection,
    topology_revision: str,
) -> dict[str, Any]:
    row = connection.execute(
        """
        SELECT contract_json
        FROM forecast_issue_contracts
        WHERE topology_revision = ?
        ORDER BY
            COALESCE(producer_issued_at_utc_s, 0) DESC,
            issue_id DESC
        LIMIT 1
        """,
        (topology_revision,),
    ).fetchone()
    if not row:
        return _unavailable_issue_contract(topology_revision)
    try:
        contract = json.loads(str(row["contract_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return _unavailable_issue_contract(
            topology_revision,
            "producer_issue_contract_unreadable",
        )
    return _public_issue_contract(contract, topology_revision)


def _nonnegative_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return number if number >= 0 else None


def _evidence_continuity(
    connection: sqlite3.Connection,
    *,
    topology_revision: str,
    method_revision: str | None,
    current_compared_slots: int,
    current_yield_relevant_days: int,
) -> dict[str, Any]:
    """Hält frühere Kohorten sichtbar, ohne deren Kennzahlen zu vermischen."""

    current_topology = _valid_revision(topology_revision)
    current_method = _valid_revision(method_revision)
    result: dict[str, Any] = {
        "schema_version": CONTINUITY_SCHEMA,
        "status": "continuous" if current_method else "EVIDENCE_LIMIT",
        "reason": "no_prior_cohort" if current_method else "current_cohort_unavailable",
        "retained": False,
        "merged_into_current_metrics": False,
        "previous_summary_schema": None,
        "previous_topology_revision": None,
        "previous_method_revision": None,
        "previous_compared_slots": 0,
        "previous_yield_relevant_days": 0,
        "current_topology_revision": current_topology,
        "current_method_revision": current_method,
        "current_compared_slots": max(0, int(current_compared_slots or 0)),
        "current_yield_relevant_days": max(
            0,
            int(current_yield_relevant_days or 0),
        ),
    }
    rows = connection.execute(
        """
        SELECT topology_revision, calculated_at_utc_s, payload_json
        FROM diagnostic_summaries
        ORDER BY calculated_at_utc_s DESC, summary_id DESC
        LIMIT 256
        """
    ).fetchall()
    for row in rows:
        try:
            payload = json.loads(str(row["payload_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict) or payload.get("available") is not True:
            continue
        schema = str(payload.get("schema_version") or "")
        if schema not in LEGACY_SUMMARY_SCHEMAS | {SUMMARY_SCHEMA}:
            continue
        previous_topology = _valid_revision(
            payload.get("topology_revision") or row["topology_revision"]
        )
        previous_contract = (
            payload.get("forecast_issue_contract")
            if isinstance(payload.get("forecast_issue_contract"), dict)
            else {}
        )
        previous_method = _valid_revision(previous_contract.get("method_revision"))
        if (
            schema == SUMMARY_SCHEMA
            and previous_topology == current_topology
            and previous_method == current_method
        ):
            continue
        previous_compared = _nonnegative_int(payload.get("compared_slots"))
        previous_days = _nonnegative_int(payload.get("yield_relevant_days"))
        if previous_compared is None or previous_days is None:
            continue
        if previous_days > previous_compared:
            continue
        if previous_topology is not None and previous_topology != current_topology:
            reason = "topology_revision_changed"
        elif schema in LEGACY_SUMMARY_SCHEMAS:
            reason = "producer_contract_upgrade"
        elif previous_method != current_method:
            reason = "method_revision_changed"
        else:
            continue
        return {
            **result,
            "status": "cohort_transition",
            "reason": reason,
            "retained": True,
            "previous_summary_schema": schema,
            "previous_topology_revision": previous_topology,
            "previous_method_revision": previous_method,
            "previous_compared_slots": previous_compared,
            "previous_yield_relevant_days": previous_days,
        }
    return result


def _sanitized_evidence_continuity(
    value: Any,
    *,
    topology_revision: str | None,
    method_revision: str | None,
    current_compared_slots: int,
    current_yield_relevant_days: int,
) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    allowed_reasons = {
        "no_prior_cohort",
        "producer_contract_upgrade",
        "topology_revision_changed",
        "method_revision_changed",
        "current_contract_missing",
        "current_cohort_unavailable",
    }
    current_method = _valid_revision(method_revision)
    retained = raw.get("retained") is True
    previous_schema = str(raw.get("previous_summary_schema") or "")
    previous_compared = _nonnegative_int(raw.get("previous_compared_slots"))
    previous_days = _nonnegative_int(raw.get("previous_yield_relevant_days"))
    if (
        previous_schema not in LEGACY_SUMMARY_SCHEMAS | {SUMMARY_SCHEMA}
        or previous_compared is None
        or previous_days is None
        or previous_days > previous_compared
    ):
        retained = False
    reason = str(raw.get("reason") or "")
    if reason not in allowed_reasons:
        reason = "current_cohort_unavailable" if current_method is None else "no_prior_cohort"
    if retained and reason not in {
        "producer_contract_upgrade",
        "topology_revision_changed",
        "method_revision_changed",
        "current_contract_missing",
    }:
        retained = False
    if not retained:
        previous_schema = ""
        previous_compared = 0
        previous_days = 0
    return {
        "schema_version": CONTINUITY_SCHEMA,
        "status": (
            "cohort_transition"
            if retained
            else "EVIDENCE_LIMIT"
            if current_method is None
            else "continuous"
        ),
        "reason": reason,
        "retained": retained,
        "merged_into_current_metrics": False,
        "previous_summary_schema": previous_schema or None,
        "previous_topology_revision": (
            _valid_revision(raw.get("previous_topology_revision"))
            if retained
            else None
        ),
        "previous_method_revision": (
            _valid_revision(raw.get("previous_method_revision"))
            if retained
            else None
        ),
        "previous_compared_slots": previous_compared,
        "previous_yield_relevant_days": previous_days,
        "current_topology_revision": _valid_revision(topology_revision),
        "current_method_revision": current_method,
        "current_compared_slots": max(0, int(current_compared_slots or 0)),
        "current_yield_relevant_days": max(
            0,
            int(current_yield_relevant_days or 0),
        ),
    }


def _evidence_limits(issue_contract: dict[str, Any]) -> list[str]:
    status_fields = {
        "producer_issue_time_status": "producer_issue_time",
        "model_revision_status": "external_model_revision",
        "method_revision_status": "producer_method_revision",
        "postprocessing_revision_status": "postprocessing_revision",
        "topology_revision_status": "topology_revision",
        "source_composition_status": "source_composition",
        "target_slots_status": "utc_target_slots",
    }
    limits = [
        label
        for field, label in status_fields.items()
        if issue_contract.get(field) != "complete"
    ]
    limits.extend([
        "probabilistic_quantiles",
        "external_ac_observation_history",
        "curtailment_exclusion",
        "inverter_clipping_exclusion",
        "external_shutdown_exclusion",
    ])
    return limits


def _forecast_value_contract() -> dict[str, Any]:
    return {
        "signal": FORECAST_SIGNAL_CONTRACT,
        "source_contract": FORECAST_SOURCE_CONTRACT,
        "value_stage": FORECAST_VALUE_STAGE,
        "distribution_type": FORECAST_DISTRIBUTION_TYPE,
        "declared_quantile": None,
        "quantile_convention": None,
        "p50_claim": "not_proven",
        "bias_sign_convention": "actual_minus_forecast_positive_underforecast",
        "decision_use_allowed": False,
    }


def _probabilistic_evidence_contract() -> dict[str, Any]:
    return {
        "status": "EVIDENCE_LIMIT",
        "reason": "explicit_quantiles_and_convention_missing",
        "required_quantile_convention": "cdf_or_exceedance_explicit",
        "empirical_quantile_coverage": {},
        "interval_coverage_pct": None,
        "mean_pinball_loss_wh": None,
        "crps_wh": None,
        "decision_use_allowed": False,
    }


def _deterministic_reference_contract(compared_slots: int) -> dict[str, Any]:
    count = max(0, int(compared_slots or 0))
    return {
        "status": "diagnostisch" if count > 0 else "EVIDENCE_LIMIT",
        "method": DETERMINISTIC_REFERENCE_METHOD,
        "reference": "previous_day_same_utc_slot_actual",
        "lookahead_guard": "reference_observed_at_or_before_producer_issue",
        "skill_score_definition": "1_minus_rmse_forecast_div_rmse_reference",
        "compared_slots": count,
        "decision_use_allowed": False,
    }


def _observation_quality_contract() -> dict[str, Any]:
    return {
        "observation_source_contract": HISTORY_SOURCE_CONTRACT,
        "curtailment_exclusion_status": "EVIDENCE_LIMIT",
        "inverter_clipping_exclusion_status": "EVIDENCE_LIMIT",
        "external_shutdown_exclusion_status": "EVIDENCE_LIMIT",
        "availability_forecast_claim_allowed": False,
        "decision_use_allowed": False,
    }


def _source_diagnostics(compared_slots: int) -> list[dict[str, Any]]:
    return [
        {
            "signal": FORECAST_SIGNAL_CONTRACT,
            "status": "diagnostisch" if compared_slots > 0 else "sammelt_evidenz",
            "forecast_source_contract": FORECAST_SOURCE_CONTRACT,
            "observation_source_contract": HISTORY_SOURCE_CONTRACT,
            "reason": "ok" if compared_slots > 0 else "noch_keine_vergleichspaare",
        },
        {
            "signal": "pv_external_ac",
            "status": "EVIDENCE_LIMIT",
            "forecast_source_contract": FORECAST_SOURCE_CONTRACT,
            "observation_source_contract": None,
            "reason": "validated_external_ac_history_missing",
        },
        {
            "signal": "house_base_load",
            "status": "EVIDENCE_LIMIT",
            "forecast_source_contract": None,
            "observation_source_contract": None,
            "reason": "forecast_observation_pair_missing",
        },
        {
            "signal": "heat_load",
            "status": "EVIDENCE_LIMIT",
            "forecast_source_contract": None,
            "observation_source_contract": None,
            "reason": "forecast_observation_pair_missing",
        },
        {
            "signal": "wallbox_load",
            "status": "EVIDENCE_LIMIT",
            "forecast_source_contract": None,
            "observation_source_contract": None,
            "reason": "forecast_observation_pair_missing",
        },
    ]


def calculate_diagnostic_summary(
    connection: sqlite3.Connection,
    topology_revision: str,
    *,
    now_utc_s: int | None = None,
) -> dict[str, Any]:
    """Berechnet Gesamt- und Vorlaufgüte in mengenbasierten Abfragen."""

    revision = _valid_revision(topology_revision)
    if revision is None:
        raise ValueError("topology_revision_invalid")
    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    evaluation_cutoff = now_s - MIN_EVALUATION_DELAY_S
    window_cutoff = now_s - EVALUATION_WINDOW_S
    issue_contract = _latest_issue_contract(connection, revision)
    current_method_revision = _valid_revision(
        issue_contract.get("method_revision")
    )
    row = connection.execute(
        """
        WITH ranked_forecasts AS (
            SELECT
                slot.slot_start_utc_s,
                slot.predicted_e3dc_dc_energy_wh AS predicted_wh,
                contract.producer_issued_at_utc_s AS published_at_utc_s,
                ROW_NUMBER() OVER (
                    PARTITION BY slot.slot_start_utc_s
                    ORDER BY
                        contract.producer_issued_at_utc_s DESC,
                        issue.issue_id DESC
                ) AS rank_no
            FROM forecast_slots AS slot
            JOIN forecast_issues AS issue ON issue.issue_id = slot.issue_id
            JOIN forecast_issue_provenance AS provenance
                ON provenance.issue_id = issue.issue_id
            JOIN forecast_issue_contracts AS contract
                ON contract.issue_id = issue.issue_id
            WHERE issue.topology_revision = :revision
              AND contract.producer_issue_time_status = 'complete'
              AND contract.method_revision_status = 'complete'
              AND contract.method_revision = :method_revision
              AND contract.postprocessing_revision_status = 'complete'
              AND contract.topology_revision_status = 'complete'
              AND contract.source_composition_status = 'complete'
              AND contract.target_slots_status = 'complete'
              AND contract.topology_revision = :revision
              AND provenance.forecast_source_contract = :forecast_source
              AND provenance.forecast_signal_contract = :forecast_signal
              AND provenance.value_stage = :value_stage
              AND provenance.distribution_type = :distribution_type
              AND provenance.quantile_level IS NULL
              AND provenance.quantile_convention IS NULL
              AND contract.producer_issued_at_utc_s <= slot.slot_start_utc_s
              AND (
                    slot.slot_start_utc_s
                    - contract.producer_issued_at_utc_s
                  ) < :maximum_lead_s
              AND slot.slot_start_utc_s >= :window_cutoff
              AND slot.slot_end_utc_s <= :evaluation_cutoff
              AND slot.predicted_e3dc_dc_energy_wh IS NOT NULL
              AND slot.source_fresh = 1
        ),
        compared AS (
            SELECT
                forecast.slot_start_utc_s,
                forecast.predicted_wh,
                forecast.published_at_utc_s,
                (
                    SELECT
                        CASE
                            WHEN observation.valid = 1
                            THEN observation.actual_e3dc_dc_energy_wh
                            ELSE NULL
                        END
                    FROM observed_slots AS observation
                        INDEXED BY idx_observed_summary_rank
                    WHERE observation.topology_revision = :revision
                      AND observation.source_contract = :source_contract
                      AND observation.slot_start_utc_s = forecast.slot_start_utc_s
                    ORDER BY
                        observation.valid DESC,
                        observation.observed_at_utc_s DESC,
                        observation.observation_id DESC
                    LIMIT 1
                ) AS actual_wh,
                (
                    SELECT
                        CASE
                            WHEN observation.valid = 1
                             AND observation.observed_at_utc_s
                                 <= forecast.published_at_utc_s
                            THEN observation.actual_e3dc_dc_energy_wh
                            ELSE NULL
                        END
                    FROM observed_slots AS observation
                        INDEXED BY idx_observed_summary_rank
                    WHERE observation.topology_revision = :revision
                      AND observation.source_contract = :source_contract
                      AND observation.slot_start_utc_s
                          = forecast.slot_start_utc_s - 86400
                      AND observation.observed_at_utc_s
                          <= forecast.published_at_utc_s
                    ORDER BY
                        observation.valid DESC,
                        observation.observed_at_utc_s DESC,
                        observation.observation_id DESC
                    LIMIT 1
                ) AS persistence_wh
            FROM ranked_forecasts AS forecast
            WHERE forecast.rank_no = 1
        ),
        relevant AS (
            SELECT *
            FROM compared
            WHERE actual_wh IS NOT NULL
              AND MAX(predicted_wh, actual_wh) >= :relevant_wh
        )
        SELECT
            (SELECT COUNT(*) FROM compared) AS eligible_slots,
            (SELECT COUNT(*) FROM compared WHERE actual_wh IS NOT NULL) AS compared_slots,
            (SELECT COUNT(*) FROM relevant) AS relevant_slots,
            (SELECT COUNT(DISTINCT date(slot_start_utc_s, 'unixepoch')) FROM relevant)
                AS relevant_days,
            (SELECT AVG(ABS(actual_wh - predicted_wh)) FROM relevant)
                AS trefferabweichung_wh,
            (SELECT AVG(actual_wh - predicted_wh) FROM relevant)
                AS richtungsversatz_wh,
            (SELECT AVG(
                (actual_wh - predicted_wh) * (actual_wh - predicted_wh)
             ) FROM relevant) AS forecast_mse_wh2,
            (SELECT COUNT(*) FROM relevant WHERE persistence_wh IS NOT NULL)
                AS persistence_compared_slots,
            (SELECT AVG(
                (actual_wh - predicted_wh) * (actual_wh - predicted_wh)
             ) FROM relevant WHERE persistence_wh IS NOT NULL)
                AS forecast_persistence_subset_mse_wh2,
            (SELECT AVG(
                (actual_wh - persistence_wh) * (actual_wh - persistence_wh)
             ) FROM relevant WHERE persistence_wh IS NOT NULL)
                AS persistence_mse_wh2,
            (SELECT
                CASE
                    WHEN SUM(actual_wh) > 0
                    THEN SUM(ABS(actual_wh - predicted_wh)) / SUM(actual_wh) * 100.0
                    ELSE NULL
                END
             FROM relevant) AS energiegewichtete_gesamtabweichung_pct
        """,
        {
            "revision": revision,
            "method_revision": current_method_revision,
            "forecast_source": FORECAST_SOURCE_CONTRACT,
            "forecast_signal": FORECAST_SIGNAL_CONTRACT,
            "value_stage": FORECAST_VALUE_STAGE,
            "distribution_type": FORECAST_DISTRIBUTION_TYPE,
            "source_contract": HISTORY_SOURCE_CONTRACT,
            "maximum_lead_s": int(LEAD_TIME_BUCKETS[-1]["max_minutes"]) * 60,
            "window_cutoff": window_cutoff,
            "evaluation_cutoff": evaluation_cutoff,
            "relevant_wh": YIELD_RELEVANT_ENERGY_WH,
        },
    ).fetchone()

    lead_rows = connection.execute(
        f"""
        WITH forecast_candidates AS (
            SELECT
                slot.slot_start_utc_s,
                slot.predicted_e3dc_dc_energy_wh AS predicted_wh,
                issue.issue_id,
                contract.producer_issued_at_utc_s AS published_at_utc_s,
                (
                    slot.slot_start_utc_s
                    - contract.producer_issued_at_utc_s
                ) / 60.0 AS lead_minutes
            FROM forecast_slots AS slot
            JOIN forecast_issues AS issue ON issue.issue_id = slot.issue_id
            JOIN forecast_issue_provenance AS provenance
                ON provenance.issue_id = issue.issue_id
            JOIN forecast_issue_contracts AS contract
                ON contract.issue_id = issue.issue_id
            WHERE issue.topology_revision = :revision
              AND contract.producer_issue_time_status = 'complete'
              AND contract.method_revision_status = 'complete'
              AND contract.method_revision = :method_revision
              AND contract.postprocessing_revision_status = 'complete'
              AND contract.topology_revision_status = 'complete'
              AND contract.source_composition_status = 'complete'
              AND contract.target_slots_status = 'complete'
              AND contract.topology_revision = :revision
              AND provenance.forecast_source_contract = :forecast_source
              AND provenance.forecast_signal_contract = :forecast_signal
              AND provenance.value_stage = :value_stage
              AND provenance.distribution_type = :distribution_type
              AND provenance.quantile_level IS NULL
              AND provenance.quantile_convention IS NULL
              AND contract.producer_issued_at_utc_s <= slot.slot_start_utc_s
              AND slot.slot_start_utc_s >= :window_cutoff
              AND slot.slot_end_utc_s <= :evaluation_cutoff
              AND slot.predicted_e3dc_dc_energy_wh IS NOT NULL
              AND slot.source_fresh = 1
        ),
        bucketed_forecasts AS (
            SELECT
                *,
                {_lead_time_case_sql()} AS bucket_id
            FROM forecast_candidates
        ),
        ranked_forecasts AS (
            SELECT
                *,
                ROW_NUMBER() OVER (
                    PARTITION BY slot_start_utc_s, bucket_id
                    ORDER BY published_at_utc_s DESC, issue_id DESC
                ) AS rank_no
            FROM bucketed_forecasts
            WHERE bucket_id IS NOT NULL
        ),
        compared AS (
            SELECT
                forecast.bucket_id,
                forecast.slot_start_utc_s,
                forecast.predicted_wh,
                forecast.published_at_utc_s,
                forecast.lead_minutes,
                (
                    SELECT
                        CASE
                            WHEN observation.valid = 1
                            THEN observation.actual_e3dc_dc_energy_wh
                            ELSE NULL
                        END
                    FROM observed_slots AS observation
                        INDEXED BY idx_observed_summary_rank
                    WHERE observation.topology_revision = :revision
                      AND observation.source_contract = :source_contract
                      AND observation.slot_start_utc_s = forecast.slot_start_utc_s
                    ORDER BY
                        observation.valid DESC,
                        observation.observed_at_utc_s DESC,
                        observation.observation_id DESC
                    LIMIT 1
                ) AS actual_wh,
                (
                    SELECT
                        CASE
                            WHEN observation.valid = 1
                             AND observation.observed_at_utc_s
                                 <= forecast.published_at_utc_s
                            THEN observation.actual_e3dc_dc_energy_wh
                            ELSE NULL
                        END
                    FROM observed_slots AS observation
                        INDEXED BY idx_observed_summary_rank
                    WHERE observation.topology_revision = :revision
                      AND observation.source_contract = :source_contract
                      AND observation.slot_start_utc_s
                          = forecast.slot_start_utc_s - 86400
                      AND observation.observed_at_utc_s
                          <= forecast.published_at_utc_s
                    ORDER BY
                        observation.valid DESC,
                        observation.observed_at_utc_s DESC,
                        observation.observation_id DESC
                    LIMIT 1
                ) AS persistence_wh
            FROM ranked_forecasts AS forecast
            WHERE forecast.rank_no = 1
        )
        SELECT
            bucket_id,
            COUNT(*) AS eligible_slots,
            SUM(CASE WHEN actual_wh IS NOT NULL THEN 1 ELSE 0 END)
                AS compared_slots,
            SUM(
                CASE
                    WHEN actual_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN 1 ELSE 0
                END
            ) AS relevant_slots,
            COUNT(
                DISTINCT CASE
                    WHEN actual_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN date(slot_start_utc_s, 'unixepoch')
                    ELSE NULL
                END
            ) AS relevant_days,
            MIN(lead_minutes) AS observed_lead_min_minutes,
            MAX(lead_minutes) AS observed_lead_max_minutes,
            AVG(lead_minutes) AS observed_lead_mean_minutes,
            AVG(
                CASE
                    WHEN actual_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN ABS(actual_wh - predicted_wh)
                    ELSE NULL
                END
            ) AS trefferabweichung_wh,
            AVG(
                CASE
                    WHEN actual_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN actual_wh - predicted_wh
                    ELSE NULL
                END
            ) AS richtungsversatz_wh,
            AVG(
                CASE
                    WHEN actual_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN (actual_wh - predicted_wh) * (actual_wh - predicted_wh)
                    ELSE NULL
                END
            ) AS forecast_mse_wh2,
            SUM(
                CASE
                    WHEN actual_wh IS NOT NULL
                     AND persistence_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN 1 ELSE 0
                END
            ) AS persistence_compared_slots,
            AVG(
                CASE
                    WHEN actual_wh IS NOT NULL
                     AND persistence_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN (actual_wh - predicted_wh) * (actual_wh - predicted_wh)
                    ELSE NULL
                END
            ) AS forecast_persistence_subset_mse_wh2,
            AVG(
                CASE
                    WHEN actual_wh IS NOT NULL
                     AND persistence_wh IS NOT NULL
                     AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                    THEN (actual_wh - persistence_wh) * (actual_wh - persistence_wh)
                    ELSE NULL
                END
            ) AS persistence_mse_wh2,
            CASE
                WHEN SUM(
                    CASE
                        WHEN actual_wh IS NOT NULL
                         AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                        THEN actual_wh
                        ELSE 0
                    END
                ) > 0
                THEN
                    SUM(
                        CASE
                            WHEN actual_wh IS NOT NULL
                             AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                            THEN ABS(actual_wh - predicted_wh)
                            ELSE 0
                        END
                    )
                    /
                    SUM(
                        CASE
                            WHEN actual_wh IS NOT NULL
                             AND MAX(predicted_wh, actual_wh) >= :relevant_wh
                            THEN actual_wh
                            ELSE 0
                        END
                    )
                    * 100.0
                ELSE NULL
            END AS energiegewichtete_gesamtabweichung_pct
        FROM compared
        GROUP BY bucket_id
        """,
        {
            "revision": revision,
            "method_revision": current_method_revision,
            "forecast_source": FORECAST_SOURCE_CONTRACT,
            "forecast_signal": FORECAST_SIGNAL_CONTRACT,
            "value_stage": FORECAST_VALUE_STAGE,
            "distribution_type": FORECAST_DISTRIBUTION_TYPE,
            "source_contract": HISTORY_SOURCE_CONTRACT,
            "window_cutoff": window_cutoff,
            "evaluation_cutoff": evaluation_cutoff,
            "relevant_wh": YIELD_RELEVANT_ENERGY_WH,
        },
    ).fetchall()

    eligible = int(row["eligible_slots"] or 0)
    compared = int(row["compared_slots"] or 0)
    relevant = int(row["relevant_slots"] or 0)
    relevant_days = int(row["relevant_days"] or 0)
    continuity = _evidence_continuity(
        connection,
        topology_revision=revision,
        method_revision=current_method_revision,
        current_compared_slots=compared,
        current_yield_relevant_days=relevant_days,
    )
    provisional_reasons = _provisional_reasons(
        relevant,
        relevant_days,
        eligible,
        compared,
    )
    sufficient = not provisional_reasons

    def rounded(name: str) -> float | None:
        value = _finite_optional(row[name])
        return None if value is None else round(value, 3)

    def rmse(source: sqlite3.Row, name: str) -> float | None:
        value = _finite_optional(source[name], nonnegative=True)
        return None if value is None else round(math.sqrt(value), 3)

    def persistence_skill(source: sqlite3.Row) -> float | None:
        forecast_rmse = rmse(source, "forecast_persistence_subset_mse_wh2")
        reference_rmse = rmse(source, "persistence_mse_wh2")
        if forecast_rmse is None or reference_rmse is None or reference_rmse <= 0.0:
            return None
        return round((1.0 - forecast_rmse / reference_rmse) * 100.0, 3)

    persistence_compared = int(row["persistence_compared_slots"] or 0)

    lead_rows_by_id = {
        str(item["bucket_id"]): item
        for item in lead_rows
        if str(item["bucket_id"] or "")
    }
    lead_time_buckets: list[dict[str, Any]] = []
    for bucket in LEAD_TIME_BUCKETS:
        bucket_row = lead_rows_by_id.get(str(bucket["bucket_id"]))
        bucket_eligible = int(bucket_row["eligible_slots"] or 0) if bucket_row else 0
        bucket_compared = int(bucket_row["compared_slots"] or 0) if bucket_row else 0
        bucket_relevant = int(bucket_row["relevant_slots"] or 0) if bucket_row else 0
        bucket_days = int(bucket_row["relevant_days"] or 0) if bucket_row else 0
        bucket_persistence_compared = (
            int(bucket_row["persistence_compared_slots"] or 0)
            if bucket_row
            else 0
        )
        bucket_reasons = _provisional_reasons(
            bucket_relevant,
            bucket_days,
            bucket_eligible,
            bucket_compared,
        )

        def bucket_number(name: str) -> float | None:
            if bucket_row is None:
                return None
            value = _finite_optional(bucket_row[name])
            return None if value is None else round(value, 3)

        lead_time_buckets.append(
            {
                **bucket,
                "status": (
                    "diagnostisch"
                    if not bucket_reasons
                    else "vorläufig"
                    if bucket_eligible > 0
                    else "EVIDENCE_LIMIT"
                ),
                "provisional": bool(bucket_reasons),
                "provisional_reasons": bucket_reasons,
                "eligible_forecast_slots": bucket_eligible,
                "compared_slots": bucket_compared,
                "yield_relevant_slots": bucket_relevant,
                "yield_relevant_days": bucket_days,
                "persistence_compared_slots": bucket_persistence_compared,
                "observed_lead_min_minutes": bucket_number(
                    "observed_lead_min_minutes"
                ),
                "observed_lead_max_minutes": bucket_number(
                    "observed_lead_max_minutes"
                ),
                "observed_lead_mean_minutes": bucket_number(
                    "observed_lead_mean_minutes"
                ),
                "metrics": {
                    "trefferabweichung_wh": bucket_number(
                        "trefferabweichung_wh"
                    ),
                    "richtungsversatz_wh": bucket_number(
                        "richtungsversatz_wh"
                    ),
                    "quadratische_fehlerwurzel_wh": (
                        rmse(bucket_row, "forecast_mse_wh2")
                        if bucket_row is not None
                        else None
                    ),
                    "persistenz_skill_score_pct": (
                        persistence_skill(bucket_row)
                        if bucket_row is not None
                        else None
                    ),
                    "energiegewichtete_gesamtabweichung_pct": bucket_number(
                        "energiegewichtete_gesamtabweichung_pct"
                    ),
                    "vergleichsabdeckung_pct": (
                        round(bucket_compared / bucket_eligible * 100.0, 3)
                        if bucket_eligible
                        else None
                    ),
                },
            }
        )

    lead_time_evidence_status = (
        "diagnostisch"
        if any(item["status"] == "diagnostisch" for item in lead_time_buckets)
        else "vorläufig"
        if any(item["eligible_forecast_slots"] > 0 for item in lead_time_buckets)
        else "EVIDENCE_LIMIT"
    )

    return {
        "schema_version": SUMMARY_SCHEMA,
        "calculated_at_utc_s": now_s,
        "topology_revision": revision,
        "status": "diagnostisch" if sufficient else "vorläufig",
        "available": True,
        "provisional": not sufficient,
        "provisional_reasons": provisional_reasons,
        "operation_mode": OPERATION_MODE,
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "decision_use_allowed": False,
        "evaluation_window_days": RAW_RETENTION_S // 86400,
        "evaluation_delay_minutes": MIN_EVALUATION_DELAY_S // 60,
        "minimum_relevant_slots": MIN_RELEVANT_SLOTS,
        "minimum_relevant_days": MIN_RELEVANT_DAYS,
        "minimum_comparison_coverage_pct": MIN_COMPARISON_COVERAGE_PCT,
        "eligible_forecast_slots": eligible,
        "compared_slots": compared,
        "yield_relevant_slots": relevant,
        "yield_relevant_days": relevant_days,
        "persistence_compared_slots": persistence_compared,
        "lead_time_basis": "producer_output_generation_to_slot_start_v1",
        "lead_time_evidence_status": lead_time_evidence_status,
        "producer_issue_time_status": issue_contract["producer_issue_time_status"],
        "model_revision_status": issue_contract["model_revision_status"],
        "method_revision_status": issue_contract["method_revision_status"],
        "postprocessing_revision_status": issue_contract[
            "postprocessing_revision_status"
        ],
        "forecast_issue_contract": issue_contract,
        "evidence_continuity": continuity,
        "evidence_limits": _evidence_limits(issue_contract),
        "lead_time_buckets": lead_time_buckets,
        "forecast_value_contract": _forecast_value_contract(),
        "deterministic_reference": _deterministic_reference_contract(
            persistence_compared
        ),
        "probabilistic_evidence": _probabilistic_evidence_contract(),
        "observation_quality": _observation_quality_contract(),
        "source_diagnostics": _source_diagnostics(compared),
        "metrics": {
            "trefferabweichung_wh": rounded("trefferabweichung_wh"),
            "richtungsversatz_wh": rounded("richtungsversatz_wh"),
            "quadratische_fehlerwurzel_wh": rmse(row, "forecast_mse_wh2"),
            "persistenz_skill_score_pct": persistence_skill(row),
            "energiegewichtete_gesamtabweichung_pct": rounded(
                "energiegewichtete_gesamtabweichung_pct"
            ),
            "vergleichsabdeckung_pct": (
                round(compared / eligible * 100.0, 3) if eligible else None
            ),
        },
        "labels": dict(DIAGNOSTIC_LABELS),
    }


def append_summary_if_due(
    *,
    topology_revision: str,
    now_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, Any]:
    """Materialisiert pro Topologie höchstens einmal täglich eine Zusammenfassung."""

    revision = _valid_revision(topology_revision)
    if revision is None:
        raise ValueError("topology_revision_invalid")
    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    with database(database_path, write=True) as connection:
        current_issue_contract = _latest_issue_contract(connection, revision)
        current_issue_id = current_issue_contract.get("issue_id")
        latest = connection.execute(
            """
            SELECT payload_json, calculated_at_utc_s
            FROM diagnostic_summaries
            WHERE topology_revision = ?
            ORDER BY calculated_at_utc_s DESC, summary_id DESC
            LIMIT 1
            """,
            (revision,),
        ).fetchone()
        if latest and now_s - int(latest["calculated_at_utc_s"]) < SUMMARY_MIN_INTERVAL_S:
            latest_payload = json.loads(str(latest["payload_json"]))
            if (
                isinstance(latest_payload, dict)
                and latest_payload.get("schema_version") == SUMMARY_SCHEMA
                and isinstance(latest_payload.get("forecast_issue_contract"), dict)
                and latest_payload["forecast_issue_contract"].get("issue_id")
                    == current_issue_id
            ):
                return latest_payload
        payload = calculate_diagnostic_summary(
            connection,
            revision,
            now_utc_s=now_s,
        )
        summary_id = _sha256_record(
            "summary",
            {"topology_revision": revision, "payload": payload},
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO diagnostic_summaries(
                summary_id, topology_revision, calculated_at_utc_s, payload_json
            ) VALUES(?, ?, ?, ?)
            """,
            (summary_id, revision, now_s, _canonical_json(payload)),
        )
        return payload


def latest_summary_for_topology(
    topology_revision: str,
    *,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, Any] | None:
    revision = _valid_revision(topology_revision)
    if revision is None or not os.path.exists(database_path):
        return None
    with database(database_path, write=False) as connection:
        row = connection.execute(
            """
            SELECT payload_json
            FROM diagnostic_summaries
            WHERE topology_revision = ?
            ORDER BY calculated_at_utc_s DESC, summary_id DESC
            LIMIT 1
            """,
            (revision,),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def unavailable_summary(
    topology_revision: str | None,
    reason: str,
    *,
    now_utc_s: int | None = None,
) -> dict[str, Any]:
    issue_contract = _unavailable_issue_contract(topology_revision, reason)
    return {
        "schema_version": SUMMARY_SCHEMA,
        "calculated_at_utc_s": int(time.time() if now_utc_s is None else now_utc_s),
        "topology_revision": _valid_revision(topology_revision),
        "status": "nicht_verfügbar",
        "available": False,
        "reason": str(reason or "evidence_unavailable")[:80],
        "operation_mode": OPERATION_MODE,
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "decision_use_allowed": False,
        "lead_time_basis": "producer_output_generation_to_slot_start_v1",
        "lead_time_evidence_status": "EVIDENCE_LIMIT",
        "producer_issue_time_status": "EVIDENCE_LIMIT",
        "model_revision_status": "EVIDENCE_LIMIT",
        "method_revision_status": "EVIDENCE_LIMIT",
        "postprocessing_revision_status": "EVIDENCE_LIMIT",
        "minimum_comparison_coverage_pct": MIN_COMPARISON_COVERAGE_PCT,
        "lead_time_buckets": [],
        "forecast_value_contract": _forecast_value_contract(),
        "forecast_issue_contract": issue_contract,
        "evidence_continuity": _sanitized_evidence_continuity(
            None,
            topology_revision=topology_revision,
            method_revision=None,
            current_compared_slots=0,
            current_yield_relevant_days=0,
        ),
        "evidence_limits": _evidence_limits(issue_contract),
        "persistence_compared_slots": 0,
        "deterministic_reference": _deterministic_reference_contract(0),
        "probabilistic_evidence": _probabilistic_evidence_contract(),
        "observation_quality": _observation_quality_contract(),
        "source_diagnostics": _source_diagnostics(0),
        "metrics": {},
        "labels": dict(DIAGNOSTIC_LABELS),
    }


def unavailable_summary_with_retained_continuity(
    topology_revision: str | None,
    reason: str,
    *,
    now_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, Any]:
    """Ergänzt ausschließlich bereits gespeicherte Altserien per Read-only-Zugriff."""

    payload = unavailable_summary(
        topology_revision,
        reason,
        now_utc_s=now_utc_s,
    )
    revision = _valid_revision(topology_revision)
    if revision is None:
        return payload
    try:
        with database(database_path, write=False) as connection:
            payload["evidence_continuity"] = _evidence_continuity(
                connection,
                topology_revision=revision,
                method_revision=None,
                current_compared_slots=0,
                current_yield_relevant_days=0,
            )
            if payload["evidence_continuity"].get("retained") is True:
                payload["evidence_continuity"]["reason"] = (
                    "current_contract_missing"
                )
    except (OSError, sqlite3.Error, EvidenceLimitError):
        pass
    return payload


def _sanitized_summary(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    sanitized_metrics: dict[str, float | None] = {}
    for key in DIAGNOSTIC_LABELS:
        value = _finite_optional(metrics.get(key))
        sanitized_metrics[key] = None if value is None else round(value, 3)
    raw_buckets = (
        payload.get("lead_time_buckets")
        if isinstance(payload.get("lead_time_buckets"), list)
        else []
    )
    raw_buckets_by_id = {
        str(item.get("bucket_id") or ""): item
        for item in raw_buckets
        if isinstance(item, dict)
    }
    sanitized_buckets: list[dict[str, Any]] = []
    allowed_statuses = {"diagnostisch", "vorläufig", "EVIDENCE_LIMIT"}
    for bucket in LEAD_TIME_BUCKETS:
        raw_bucket = raw_buckets_by_id.get(str(bucket["bucket_id"]), {})
        raw_metrics = (
            raw_bucket.get("metrics")
            if isinstance(raw_bucket.get("metrics"), dict)
            else {}
        )
        bucket_metrics: dict[str, float | None] = {}
        for key in DIAGNOSTIC_LABELS:
            value = _finite_optional(raw_metrics.get(key))
            bucket_metrics[key] = None if value is None else round(value, 3)
        status = str(raw_bucket.get("status") or "EVIDENCE_LIMIT")
        if status not in allowed_statuses:
            status = "EVIDENCE_LIMIT"

        def bucket_number(name: str) -> float | None:
            value = _finite_optional(raw_bucket.get(name), nonnegative=True)
            return None if value is None else round(value, 3)

        sanitized_buckets.append(
            {
                **bucket,
                "status": status,
                "provisional": raw_bucket.get("provisional") is True,
                "provisional_reasons": [
                    str(item)[:80]
                    for item in (raw_bucket.get("provisional_reasons") or [])
                    if isinstance(item, str)
                ][:8],
                "eligible_forecast_slots": max(
                    0, int(raw_bucket.get("eligible_forecast_slots") or 0)
                ),
                "compared_slots": max(
                    0, int(raw_bucket.get("compared_slots") or 0)
                ),
                "yield_relevant_slots": max(
                    0, int(raw_bucket.get("yield_relevant_slots") or 0)
                ),
                "yield_relevant_days": max(
                    0, int(raw_bucket.get("yield_relevant_days") or 0)
                ),
                "persistence_compared_slots": max(
                    0, int(raw_bucket.get("persistence_compared_slots") or 0)
                ),
                "observed_lead_min_minutes": bucket_number(
                    "observed_lead_min_minutes"
                ),
                "observed_lead_max_minutes": bucket_number(
                    "observed_lead_max_minutes"
                ),
                "observed_lead_mean_minutes": bucket_number(
                    "observed_lead_mean_minutes"
                ),
                "metrics": bucket_metrics,
            }
        )
    lead_time_status = str(
        payload.get("lead_time_evidence_status") or "EVIDENCE_LIMIT"
    )
    if lead_time_status not in allowed_statuses:
        lead_time_status = "EVIDENCE_LIMIT"
    compared_slots = max(0, int(payload.get("compared_slots") or 0))
    persistence_compared_slots = max(
        0, int(payload.get("persistence_compared_slots") or 0)
    )
    topology_revision = _valid_revision(payload.get("topology_revision"))
    issue_contract = _public_issue_contract(
        payload.get("forecast_issue_contract"),
        topology_revision,
    )
    current_yield_relevant_days = max(
        0,
        int(payload.get("yield_relevant_days") or 0),
    )
    continuity = _sanitized_evidence_continuity(
        payload.get("evidence_continuity"),
        topology_revision=topology_revision,
        method_revision=issue_contract.get("method_revision"),
        current_compared_slots=compared_slots,
        current_yield_relevant_days=current_yield_relevant_days,
    )
    return {
        "schema_version": SUMMARY_SCHEMA,
        "calculated_at_utc_s": int(payload.get("calculated_at_utc_s") or 0),
        "topology_revision": topology_revision,
        "status": str(payload.get("status") or "nicht_verfügbar")[:32],
        "available": payload.get("available") is True,
        "reason": str(payload.get("reason") or "")[:80],
        "provisional": payload.get("provisional") is True,
        "provisional_reasons": [
            str(item)[:80]
            for item in (payload.get("provisional_reasons") or [])
            if isinstance(item, str)
        ][:8],
        "operation_mode": OPERATION_MODE,
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "decision_use_allowed": False,
        "evaluation_window_days": int(payload.get("evaluation_window_days") or 0),
        "evaluation_delay_minutes": int(payload.get("evaluation_delay_minutes") or 0),
        "minimum_relevant_slots": int(payload.get("minimum_relevant_slots") or 0),
        "minimum_relevant_days": int(payload.get("minimum_relevant_days") or 0),
        "minimum_comparison_coverage_pct": MIN_COMPARISON_COVERAGE_PCT,
        "eligible_forecast_slots": int(payload.get("eligible_forecast_slots") or 0),
        "compared_slots": compared_slots,
        "yield_relevant_slots": int(payload.get("yield_relevant_slots") or 0),
        "yield_relevant_days": current_yield_relevant_days,
        "persistence_compared_slots": persistence_compared_slots,
        "lead_time_basis": "producer_output_generation_to_slot_start_v1",
        "lead_time_evidence_status": lead_time_status,
        "producer_issue_time_status": issue_contract["producer_issue_time_status"],
        "model_revision_status": issue_contract["model_revision_status"],
        "method_revision_status": issue_contract["method_revision_status"],
        "postprocessing_revision_status": issue_contract[
            "postprocessing_revision_status"
        ],
        "lead_time_buckets": sanitized_buckets,
        "forecast_value_contract": _forecast_value_contract(),
        "forecast_issue_contract": issue_contract,
        "evidence_continuity": continuity,
        "evidence_limits": _evidence_limits(issue_contract),
        "deterministic_reference": _deterministic_reference_contract(
            persistence_compared_slots
        ),
        "probabilistic_evidence": _probabilistic_evidence_contract(),
        "observation_quality": _observation_quality_contract(),
        "source_diagnostics": _source_diagnostics(compared_slots),
        "metrics": sanitized_metrics,
        "labels": dict(DIAGNOSTIC_LABELS),
    }


def publish_summary_json(
    payload: dict[str, Any],
    *,
    summary_path: str = SUMMARY_JSON_PATH,
) -> None:
    """Veröffentlicht ausschließlich eine sanitierte Summary atomar."""

    directory = os.path.dirname(os.path.abspath(summary_path))
    if os.path.lexists(directory) and os.path.islink(directory):
        raise EvidenceLimitError("summary_directory_symlink")
    os.makedirs(directory, exist_ok=True)
    if os.path.lexists(summary_path) and os.path.islink(summary_path):
        raise EvidenceLimitError("summary_target_symlink")
    encoded = (_canonical_json(_sanitized_summary(payload)) + "\n").encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise EvidenceLimitError("summary_size_limit")
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".pv_forecast_diagnostic_summary.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, summary_path)
        os.chmod(summary_path, 0o644)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def run_write_step(
    operation: Callable[[], Any],
    *,
    database_path: str = EVIDENCE_DB_PATH,
    lock_path: str | None = None,
) -> Any:
    """Kapselt einen Sidecar-Schreibschritt hinter dem Ein-Writer-Vertrag."""

    effective_lock_path = lock_path or os.path.join(
        os.path.dirname(os.path.abspath(database_path)),
        "writer.lock",
    )
    with single_writer_lock(effective_lock_path):
        return operation()


# Kompatible Funktionsnamen für lokale Diagnosewerkzeuge; produktiv importiert
# ausschließlich der Sidecar dieses Modul.
archive_forecast_issue = archive_forecast_snapshot
