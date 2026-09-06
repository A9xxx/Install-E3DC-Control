#!/usr/bin/env python3
"""Prüft Entscheidungsverläufe gegen gemeinsame Sicherheitsinvarianten."""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import math
import os
import re
import sys
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from Installer import control_safety  # noqa: E402


SERVICE_ALIASES = {
    "wallbox": "wallbox",
    "wb": "wallbox",
    "storage": "storage",
    "speicher": "storage",
    "ems": "ems",
    "surface": "ems",
    "decision_surface": "ems",
    "heatpump": "heatpump",
    "wp": "heatpump",
    "energy": "heatpump",
    "waerme": "heatpump",
}

SERVICE_FILE_MARKERS = {
    "wallbox": ("wallbox_decision_history", "wallbox_decision_latest"),
    "storage": ("storage_decision_history", "storage_decision_latest"),
    "ems": ("ems_decision_latest", "ems_decision_surface", "ems_decision"),
    "heatpump": ("energy_decision_history", "energy_decision_latest"),
}

WALLBOX_START_PATTERNS = (("START", "STOP", "START"), ("STOP", "START", "STOP"))
WALLBOX_PHASE_PATTERNS = (("1P", "3P", "1P"), ("3P", "1P", "3P"))
STORAGE_PATTERNS = (
    ("CHRG", "AUTO", "CHRG"),
    ("AUTO", "CHRG", "AUTO"),
    ("DISCH", "AUTO", "DISCH"),
    ("AUTO", "DISCH", "AUTO"),
    ("IDLE", "AUTO", "IDLE"),
    ("AUTO", "IDLE", "AUTO"),
)
HEATPUMP_PATTERNS = (
    ("BOOST", "OFF", "BOOST"),
    ("OFF", "BOOST", "OFF"),
    ("RUN", "OFF", "RUN"),
    ("OFF", "RUN", "OFF"),
)
EMS_DECISIONS = {
    "allow",
    "block",
    "command",
    "noop",
    "observe",
    "pause",
    "protect",
    "stop",
    "vehicle_finished",
}
OWNER_MIN_GAP_S = 600
LIVE_PLAUSIBILITY_REPEAT_COUNT = 3
LIVE_PLAUSIBILITY_PERSISTENT_S = 60
STORAGE_OWNER_CHATTER_ACTIONS = ("WALLBOX",)
STORAGE_STATE_CHATTER_ACTIONS = (
    "PARALLEL_WB_AUTO",
)
STORAGE_CONTRACT_CHATTER_ACTIONS = (
    "CURVE",
    "MARKET_DIRECT",
    "MARKET_PRICE",
    "PREDUMP",
    "PROTECTION",
    "STORAGE_ACTIVE",
)
STORAGE_DECISION_PATH_CHATTER_ACTIONS = (
    "CURVE",
    "DIRECT_MARKETING",
    "MARKET_PRICE",
    "PREDUMP",
    "PROTECTION",
    "WALLBOX_SUPPORT",
    "MANUAL",
)
STORAGE_DECISION_PATH_PROTECTION_MARKERS = (
    "live_plausibility",
    "emergency",
    "not_aus",
    "not-aus",
    "manual",
    "user",
    "hard",
    "house_fuse",
    "budget_timeout",
    "stale",
    "fault",
    "error",
    "no_vehicle",
    "kein fahrzeug",
    "disconnected",
    "vehicle_done",
    "charge_done",
    "target_reached",
    "target_unreachable",
    "owner_override",
    "slot_end",
    "planned_end",
)
LIVE_PLAUSIBILITY_REASON_KEYS = (
    "live_plausibility_preserved_auto_limit",
    "live_plausibility_preserved_wbminsoc_contract",
    "live_plausibility_preserved_discharge_owner",
    "live_plausibility_preserved_charge_owner",
    "live_plausibility_manual_override_kept",
)
BUDGET_EXECUTOR_ACK_SOURCES = (
    "storage_budget_executor",
    "storage_manager_budget_executor",
)

MAX_ARCHIVE_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_ENTRIES = 2048
MAX_ARCHIVE_UNCOMPRESSED_BYTES = 256 * 1024 * 1024
MAX_ARCHIVE_MEMBER_BYTES = 32 * 1024 * 1024
MAX_ARCHIVE_COMPRESSION_RATIO = 100
MAX_DECODED_RECORD_BYTES = 64 * 1024 * 1024

HISTORY_COVERAGE_COUNTS = (
    "context_records", "decision_records", "invalid_records", "aggregated_records",
    "aggregated_samples", "aggregated_transitions", "archive_gap_records",
)
EVIDENCE_REASON_CODES = (
    "malformed_record", "invalid_timestamp", "invalid_history_metadata",
    "unknown_record_format", "aggregated_interval", "archive_gap", "record_limit_reached",
    "legacy_output_contract_missing", "output_contract_invalid", "readback_stale",
    "output_unconfirmed", "transaction_binding_invalid", "live_contract_missing",
    "live_contract_invalid", "heatpump_observation_missing", "executor_binding_evidence_missing",
)
STORAGE_OUTPUT_COUNTS = (
    "confirmed_observations", "confirmed_output_changes", "confirmed_new_output_records",
    "retained_readback_records",
)


def _coverage_record() -> Dict[str, Any]:
    return {**{key: 0 for key in HISTORY_COVERAGE_COUNTS}, "reasons": {}}


def _coverage_reason(coverage: Dict[str, Any], reason: str) -> None:
    reasons = coverage.setdefault("reasons", {})
    reasons[reason] = reasons.get(reason, 0) + 1


def _is_history_context(record: Dict[str, Any]) -> bool:
    metadata = record.get("_history") if isinstance(record.get("_history"), dict) else {}
    context = record.get("context")
    return bool(
        metadata.get("schema_version") == "decision_history_v2"
        and metadata.get("record_type") == "context"
        and isinstance(metadata.get("context_id"), str) and metadata["context_id"].strip()
        and set(metadata) <= {"schema_version", "record_type", "context_id"}
        and isinstance(context, dict)
        and record.get("service") in ("storage_manager", "wallbox_manager", "energy_manager")
        and context.get("service") == record.get("service")
        and not any(key in record for key in ("decision", "inputs", "rscp_execution", "wallboxes", "heatpump"))
    )


def _history_observation(record: Dict[str, Any], service: str, coverage: Dict[str, Any]) -> bool:
    """Trennt Archivmetadaten von beobachteten Entscheidungen, ohne Lücken zu verdecken."""
    if record.get("_analysis_input_error") is True:
        coverage["invalid_records"] += 1
        _coverage_reason(coverage, "malformed_record")
        return False
    history = record.get("_history")
    metadata = history if isinstance(history, dict) else {}
    record_type = metadata.get("record_type")
    timestamp = _parse_ts_s(record.get("ts", record.get("time")), float("nan"))
    if isinstance(record.get("ts"), bool) or not math.isfinite(timestamp) or timestamp <= 0:
        coverage["invalid_records"] += 1
        _coverage_reason(coverage, "invalid_timestamp")
        # Zeitlose EMS-Datensätze können einen fachlichen Widerspruch belegen,
        # erhalten aber keinen erfundenen Zeitpunkt auf der Ereignisachse.
        return service == "ems" and record.get("schema") == "ems_decision_v1"
    expected_service = {"storage": "storage_manager", "wallbox": "wallbox_manager", "heatpump": "energy_manager"}.get(service)
    metadata_valid = bool(
        metadata.get("schema_version") == "decision_history_v2"
        and isinstance(record_type, str)
        and record_type in {"context", "transition", "sample", "summary"}
        and isinstance(metadata.get("context_id"), str)
        and metadata["context_id"].strip()
    )
    if _is_history_context(record) and record.get("service") == expected_service:
        coverage["context_records"] += 1
        return False
    invalid_metadata = history is not None and (not metadata_valid or record_type == "context")
    if invalid_metadata:
        coverage["invalid_records"] += 1
        _coverage_reason(coverage, "invalid_history_metadata")
    if record.get("service") not in (None, expected_service) and service != "ems":
        if not invalid_metadata:
            coverage["invalid_records"] += 1
        _coverage_reason(coverage, "unknown_record_format")
        return False
    observation = (
        isinstance(record.get("decision"), dict) and bool(record["decision"])
        or service == "wallbox" and isinstance(record.get("wallboxes"), list)
        or service == "heatpump" and isinstance(record.get("heatpump"), dict) and bool(record["heatpump"])
        or service == "ems" and record.get("schema") == "ems_decision_v1"
    )
    if not observation:
        if not invalid_metadata:
            coverage["invalid_records"] += 1
        _coverage_reason(coverage, "unknown_record_format")
        return False
    runtime = record.get("_history_runtime") if isinstance(record.get("_history_runtime"), dict) else {}
    markers = [source["evidence_limit"] for source in (metadata, runtime)
               if source.get("evidence_limit") is not None and source.get("evidence_limit") is not False]
    valid_gap = any(
        isinstance(marker, dict) and marker.get("schema_version") == "history_evidence_limit_v1"
        and marker.get("status") == "EVIDENCE_LIMIT" for marker in markers
    )
    if any(
        not isinstance(marker, dict) or marker.get("schema_version") != "history_evidence_limit_v1"
        or marker.get("status") != "EVIDENCE_LIMIT" for marker in markers
    ):
        if not invalid_metadata:
            coverage["invalid_records"] += 1
            _coverage_reason(coverage, "invalid_history_metadata")
            invalid_metadata = True
    if valid_gap:
        coverage["archive_gap_records"] += 1
        _coverage_reason(coverage, "archive_gap")
    if "coalesced" in metadata or record_type == "summary":
        summary = metadata.get("coalesced") if isinstance(metadata.get("coalesced"), dict) else {}
        coverage["aggregated_records"] += 1
        _coverage_reason(coverage, "aggregated_interval")
        # Min/Max und eine Übergangszahl ergeben keine zeitliche Befehlsfolge.
        summary_valid = bool(
            summary.get("schema_version") == "history_coalesced_summary_v1"
            and all(isinstance(summary.get(key), int) and not isinstance(summary.get(key), bool)
                    and summary[key] >= 0 for key in ("count", "transition_count", "first_ts", "last_ts"))
            and summary.get("count", 0) > 0
            and summary.get("transition_count", 0) <= summary.get("count", 0)
            and summary.get("first_ts", 0) <= summary.get("last_ts", 0)
        )
        if summary_valid:
            coverage["aggregated_samples"] += summary["count"]
            coverage["aggregated_transitions"] += summary["transition_count"]
        elif not invalid_metadata:
            coverage["invalid_records"] += 1
            _coverage_reason(coverage, "invalid_history_metadata")
    return True


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        text = str(value).strip().replace(",", ".")
        if text == "" or text.lower() in ("none", "null", "nan", "inf", "infinity"):
            return float(default)
        result = float(text)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    return int(round(_safe_float(value, default)))


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def _parse_ts_s(value: Any, fallback_s: float) -> float:
    if value is None:
        return fallback_s
    if isinstance(value, (int, float)):
        numeric = float(value)
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    text = str(value).strip()
    if not text:
        return fallback_s
    try:
        numeric = float(text)
        return numeric / 1000.0 if numeric > 10_000_000_000 else numeric
    except Exception:
        pass
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except Exception:
        return fallback_s


def _decode_jsonl_bytes(data: bytes) -> List[Dict[str, Any]]:
    try:
        with gzip.GzipFile(fileobj=io.BytesIO(data), mode="rb") as handle:
            decoded = handle.read(MAX_DECODED_RECORD_BYTES + 1)
        if len(decoded) > MAX_DECODED_RECORD_BYTES:
            raise ValueError("compressed decision history exceeds the decode limit")
        text = decoded.decode("utf-8", "replace")
    except (gzip.BadGzipFile, EOFError, OSError, zlib.error):
        if len(data) > MAX_DECODED_RECORD_BYTES:
            raise ValueError("decision history exceeds the decode limit")
        text = data.decode("utf-8", "replace")
    records: List[Dict[str, Any]] = []
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            value = json.loads(line)
        except Exception:
            records.append({"_analysis_input_error": True})
            continue
        if isinstance(value, dict):
            records.append(value)
        else:
            records.append({"_analysis_input_error": True})
    return records


def _json_records_from_value(value: Any, service: Optional[str]) -> List[Dict[str, Any]]:
    if service == "ems":
        if isinstance(value, dict):
            if value.get("schema") == "ems_decision_surface_v1":
                actors = value.get("actors") if isinstance(value.get("actors"), dict) else {}
                return [record if isinstance(record, dict) else {"_analysis_input_error": True}
                        for _, record in sorted(actors.items())]
            if value.get("schema") == "ems_decision_v1":
                return [value]
            records = value.get("records") if isinstance(value.get("records"), list) else value.get("items")
            if isinstance(records, list):
                return [record if isinstance(record, dict) else {"_analysis_input_error": True}
                        for record in records]
            return [{"_analysis_input_error": True}]
        if isinstance(value, list):
            return [record if isinstance(record, dict) else {"_analysis_input_error": True}
                    for record in value]
        return [{"_analysis_input_error": True}]
    if isinstance(value, dict):
        return [value]
    if isinstance(value, list):
        return [item if isinstance(item, dict) else {"_analysis_input_error": True} for item in value]
    return [{"_analysis_input_error": True}]


def _read_records_from_bytes(name: str, data: bytes, service: Optional[str]) -> List[Dict[str, Any]]:
    if name.lower().endswith(".json"):
        try:
            value = json.loads(data.decode("utf-8", "replace"))
        except Exception:
            return [{"_analysis_input_error": True}]
        return _json_records_from_value(value, service)
    return _decode_jsonl_bytes(data)


def _service_for_path(name: str) -> Optional[str]:
    lower = name.replace("\\", "/").lower()
    for service, markers in SERVICE_FILE_MARKERS.items():
        if any(marker in lower for marker in markers):
            return service
    return None


def _normalize_services(services: Optional[Iterable[str]]) -> Tuple[str, ...]:
    if not services:
        return ("wallbox", "storage", "heatpump")
    normalized = []
    for service in services:
        mapped = SERVICE_ALIASES.get(str(service).strip().lower())
        if mapped and mapped not in normalized:
            normalized.append(mapped)
    return tuple(normalized or ("wallbox", "storage", "heatpump"))


def _read_records_from_file(path: Path, service: Optional[str] = None) -> Dict[str, List[Dict[str, Any]]]:
    target_service = service or _service_for_path(path.name)
    if not target_service:
        return {}
    if path.stat().st_size > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError("decision history exceeds the input limit")
    data = path.read_bytes()
    records = _read_records_from_bytes(path.name, data, target_service)
    return {target_service: records}


def _validated_zip_infos(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    infos = archive.infolist()
    if len(infos) > MAX_ARCHIVE_ENTRIES:
        raise ValueError("diagnose archive contains too many entries")

    total_uncompressed = 0
    validated: List[zipfile.ZipInfo] = []
    for info in infos:
        normalized = info.filename.replace("\\", "/")
        parts = Path(normalized).parts
        unix_type = (info.external_attr >> 16) & 0o170000
        if (
            not normalized
            or normalized.startswith("/")
            or "\x00" in normalized
            or ".." in parts
            or unix_type == 0o120000
            or info.flag_bits & 0x1
        ):
            raise ValueError("diagnose archive contains an unsafe entry")
        if info.is_dir():
            continue
        if info.file_size < 0 or info.compress_size < 0:
            raise ValueError("diagnose archive contains invalid sizes")
        if info.file_size > MAX_ARCHIVE_MEMBER_BYTES:
            raise ValueError("diagnose archive entry exceeds the size limit")
        total_uncompressed += info.file_size
        if total_uncompressed > MAX_ARCHIVE_UNCOMPRESSED_BYTES:
            raise ValueError("diagnose archive exceeds the uncompressed size limit")
        if info.file_size and (
            info.compress_size == 0
            or info.file_size > info.compress_size * MAX_ARCHIVE_COMPRESSION_RATIO
        ):
            raise ValueError("diagnose archive exceeds the compression-ratio limit")
        validated.append(info)
    return validated


def _read_zip_member(archive: zipfile.ZipFile, info: zipfile.ZipInfo) -> bytes:
    with archive.open(info, "r") as handle:
        data = handle.read(MAX_ARCHIVE_MEMBER_BYTES + 1)
    if len(data) != info.file_size or len(data) > MAX_ARCHIVE_MEMBER_BYTES:
        raise ValueError("diagnose archive entry failed the bounded read")
    return data


def load_diagnose_records(
    input_path: str | os.PathLike[str],
    *,
    services: Optional[Iterable[str]] = None,
    limit_per_service: Optional[int] = None,
    history_coverage: Optional[Dict[str, Any]] = None,
) -> Dict[str, List[Dict[str, Any]]]:
    """Lädt Entscheidungsverläufe aus Diagnose-ZIP, Verzeichnis oder Einzeldatei."""

    selected = set(_normalize_services(services))
    path = Path(input_path)
    records: Dict[str, List[Dict[str, Any]]] = {service: [] for service in selected}
    coverage = history_coverage if history_coverage is not None else {}
    for service in selected:
        coverage[service] = _coverage_record()

    def add(service: Optional[str], source_records: Sequence[Dict[str, Any]]) -> None:
        if not service or service not in selected:
            return
        bucket = records.setdefault(service, [])
        for record in source_records:
            if not _history_observation(record, service, coverage[service]):
                continue
            if limit_per_service is not None and len(bucket) >= int(limit_per_service):
                _coverage_reason(coverage[service], "record_limit_reached")
                continue
            bucket.append(record)
            coverage[service]["decision_records"] += 1

    if path.is_file() and path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ValueError("diagnose archive exceeds the upload size limit")

    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            for info in sorted(_validated_zip_infos(archive), key=lambda value: value.filename):
                service = _service_for_path(info.filename)
                if service not in selected:
                    continue
                add(
                    service,
                    _read_records_from_bytes(
                        info.filename,
                        _read_zip_member(archive, info),
                        service,
                    ),
                )
        return records

    if path.is_dir():
        for child in sorted(path.rglob("*")):
            if not child.is_file():
                continue
            service = _service_for_path(str(child))
            if service not in selected:
                continue
            add(service, _read_records_from_file(child, service).get(service, []))
        return records

    loaded = _read_records_from_file(path)
    if not loaded and len(selected) == 1:
        loaded = _read_records_from_file(path, next(iter(selected)))
    for service, source_records in loaded.items():
        add(service, source_records)
    return records


def _decision(record: Dict[str, Any]) -> Dict[str, Any]:
    value = record.get("decision")
    return value if isinstance(value, dict) else {}


def _inputs(record: Dict[str, Any]) -> Dict[str, Any]:
    value = record.get("inputs")
    return value if isinstance(value, dict) else {}


def _reason_text(*parts: Any) -> str:
    return " ".join(str(part or "") for part in parts if part is not None).strip()


def _append_transition(
    events: List[Dict[str, Any]],
    last_actions: Dict[str, str],
    *,
    actor: str,
    ts: float,
    action: str,
    reason: str,
    source: str,
    target_reachable: bool = True,
    **extra: Any,
) -> None:
    action = str(action or "").strip().upper()
    if not action:
        return
    if last_actions.get(actor) == action:
        return
    last_actions[actor] = action
    event = {
        "actor": actor,
        "ts": float(ts),
        "action": action,
        "reason": str(reason or ""),
        "source": source,
        "target_reachable": bool(target_reachable),
    }
    for key, value in extra.items():
        if value is not None:
            event[str(key)] = value
    events.append(event)


def _ordered_history_records(records: Iterable[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], float]]:
    """Sortiert überlappende History-Dateien, bevor Übergänge entdoppelt werden."""

    ordered: List[Tuple[float, int, Dict[str, Any]]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        if _is_history_context(record):
            continue
        ts = _parse_ts_s(record.get("ts", record.get("time")), 1_800_000_000.0 + index * 10.0)
        ordered.append((float(ts), index, record))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return [(record, ts) for ts, _index, record in ordered]


def _wallbox_driver_command(wb: Dict[str, Any]) -> Dict[str, Any]:
    command = wb.get("driver_command")
    if isinstance(command, dict):
        return command
    payload = wb.get("decision_payload") if isinstance(wb.get("decision_payload"), dict) else {}
    command = payload.get("driver_command") if isinstance(payload.get("driver_command"), dict) else {}
    return command if isinstance(command, dict) else {}


def _wallbox_start_action_from_command(command: Dict[str, Any]) -> str:
    kind = str(command.get("kind", "") or "").strip().lower()
    method = str(command.get("method", "") or "").strip().lower()
    amp = _safe_int(command.get("amp", command.get("target_amp", 0)), 0)
    force_state = _safe_int(command.get("force_state", -1), -1)
    if kind == "stop" or method == "emergency_stop":
        return "STOP"
    if method in ("set_amp_and_state", "set_amp_sonnenmodus") and (amp <= 0 or force_state == 1):
        return "STOP"
    if method == "set_direct_current" and amp <= 0:
        return "STOP"
    if kind in ("set_current", "hold_current") and amp >= 6:
        return "START"
    if method in ("set_amp_and_state", "set_amp_sonnenmodus", "set_direct_current") and amp >= 6:
        return "START"
    return ""


def _wallbox_phase_action_from_command(command: Dict[str, Any]) -> str:
    kind = str(command.get("kind", "") or "").strip().lower()
    method = str(command.get("method", "") or "").strip().lower()
    phases = _safe_int(command.get("target_phases", command.get("phases", 0)), 0)
    if phases in (1, 3) and (kind == "set_phases" or method == "set_phases"):
        return f"{phases}P"
    return ""


def wallbox_records_to_events(records: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    start_events: List[Dict[str, Any]] = []
    phase_events: List[Dict[str, Any]] = []
    last_start: Dict[str, str] = {}
    last_phase: Dict[str, str] = {}
    for index, record in enumerate(records):
        decision = _decision(record)
        inputs = _inputs(record)
        ts = _parse_ts_s(record.get("ts", record.get("time")), 1_800_000_000.0 + index * 10.0)
        mode_reason = "mode0" if _safe_int(decision.get("mode_public"), -1) == 0 else ""
        global_reason = _reason_text(
            decision.get("reason"),
            decision.get("state"),
            inputs.get("budget_timeout") and "budget_timeout",
            inputs.get("budget_stale") and "stale",
            mode_reason,
        )
        wallboxes = record.get("wallboxes") if isinstance(record.get("wallboxes"), list) else []
        for wb in wallboxes:
            if not isinstance(wb, dict):
                continue
            wb_id = _safe_int(wb.get("id"), 1)
            actor = f"wallbox:{wb_id}"
            command = _wallbox_driver_command(wb)
            command_reason = _reason_text(command.get("reason"), command.get("kind"), command.get("method"))
            reason = _reason_text(global_reason, command_reason, wb.get("state_reason"), wb.get("state"), wb.get("physical_reason"))

            command_start = _wallbox_start_action_from_command(command)
            if command_start:
                _append_transition(
                    start_events,
                    last_start,
                    actor=actor,
                    ts=ts,
                    action=command_start,
                    reason=reason,
                    source="wallbox_driver_command",
                )
            elif not command:
                amp = _safe_int(wb.get("amp", wb.get("set_amp", inputs.get("set_amp", 0))), 0)
                power_w = abs(_safe_float(wb.get("power_w", wb.get("phase_power_sum_w")), 0.0))
                charging = bool(wb.get("charging")) or power_w > 250.0
                action = "START" if amp > 0 or charging else "STOP"
                _append_transition(
                    start_events,
                    last_start,
                    actor=actor,
                    ts=ts,
                    action=action,
                    reason=reason,
                    source="wallbox_decision_history",
                )

            command_phase = _wallbox_phase_action_from_command(command)
            if command_phase:
                _append_transition(
                    phase_events,
                    last_phase,
                    actor=f"{actor}:phase",
                    ts=ts,
                    action=command_phase,
                    reason=reason,
                    source="wallbox_driver_command",
                )
            elif not command:
                phases = _safe_int(wb.get("phases_in_use", wb.get("physical_phases", 0)), 0)
                if phases not in (1, 3):
                    l1 = abs(_safe_float(wb.get("phase_power_l1_w"), 0.0))
                    l2 = abs(_safe_float(wb.get("phase_power_l2_w"), 0.0))
                    l3 = abs(_safe_float(wb.get("phase_power_l3_w"), 0.0))
                    phase_count = sum(1 for value in (l1, l2, l3) if value > 250.0)
                    phases = 3 if phase_count >= 3 else 1 if phase_count == 1 else 0
                if phases in (1, 3):
                    _append_transition(
                        phase_events,
                        last_phase,
                        actor=f"{actor}:phase",
                        ts=ts,
                        action=f"{phases}P",
                        reason=reason,
                        source="wallbox_decision_history",
                    )
    return start_events, phase_events


def _storage_mode_name(decision: Dict[str, Any]) -> str:
    mode_name = str(decision.get("mode_name", "") or "").strip().upper()
    if mode_name:
        return mode_name
    return {
        0: "AUTO",
        1: "IDLE",
        2: "DISCH",
        3: "CHRG",
        4: "GRID",
    }.get(_safe_int(decision.get("mode"), 0), "AUTO")


def storage_records_to_events(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    seen: set[Tuple[float, str]] = set()
    previous_output_key = ""
    for record, ts in _ordered_history_records(records):
        decision = _decision(record)
        output = _storage_execution_output_info(record, decision)
        output_key = str(output.get("output_key") or "")
        if not output_key:
            continue
        exact_key = (float(ts), output_key)
        if exact_key in seen or (output_key and output_key == previous_output_key):
            continue
        seen.add(exact_key)
        previous_output_key = output_key
        reason = _reason_text(
            decision.get("reason"),
            decision.get("state"),
            decision.get("priority"),
            decision.get("protected") and "protection",
        )
        events.append({
            "actor": "storage",
            "ts": float(ts),
            "action": str(output.get("execution_class") or "UNKNOWN"),
            "reason": reason,
            "source": "storage_rscp_execution_history",
            "target_reachable": True,
            **output,
        })
    return events


def _record_r5(record: Dict[str, Any]) -> Dict[str, Any]:
    value = record.get("r5")
    return value if isinstance(value, dict) else {}


def _storage_target_history(record: Dict[str, Any]) -> Dict[str, Any]:
    value = record.get("storage_target")
    if not isinstance(value, dict):
        return {}
    if value.get("schema_version") != "storage_target_history_v1":
        return {}
    return value


def _storage_auto_limit(record: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    direct = decision.get("auto_limit")
    if isinstance(direct, dict):
        return direct
    limits = record.get("limits") if isinstance(record.get("limits"), dict) else {}
    nested = limits.get("auto_limit") if isinstance(limits.get("auto_limit"), dict) else {}
    if nested:
        return nested
    budget = record.get("storage_budget") if isinstance(record.get("storage_budget"), dict) else {}
    nested = budget.get("auto_limit") if isinstance(budget.get("auto_limit"), dict) else {}
    return nested if isinstance(nested, dict) else {}


def _storage_contract_owner(record: Dict[str, Any], decision: Dict[str, Any]) -> str:
    r5_contract = str(_record_r5(record).get("contract_owner") or "").strip().upper()
    if r5_contract:
        return r5_contract
    state = str(decision.get("state", record.get("state", "")) or "").strip()
    owner = str(
        decision.get("control_owner")
        or record.get("control_owner")
        or _record_r5(record).get("control_owner")
        or ""
    ).strip()
    owner_norm = owner.lower()
    mode_name = _storage_mode_name(decision)
    reason = _reason_text(
        decision.get("reason"),
        decision.get("state_label"),
        decision.get("label"),
        decision.get("priority"),
    ).lower()

    if state.startswith("direct_marketing_") or owner_norm == "direct_marketing":
        return "MARKET_DIRECT"
    if state.startswith("market_") or owner_norm == "market_economics":
        return "MARKET_PRICE"
    if state.startswith("pre_discharge") or owner_norm == "predump":
        return "PREDUMP"
    if (
        state in ("parallel_no_data", "parallel_passthrough", "parallel_emergency_auto")
        or bool(decision.get("protected"))
        or "notstrom" in reason
    ):
        return "PROTECTION"
    if mode_name in ("GRID",):
        return "MARKET_PRICE" if "price" in reason or "preis" in reason else "STORAGE_ACTIVE"
    if mode_name in ("CHRG", "DISCH", "IDLE"):
        if state.startswith("parallel_headroom") or "abregel" in reason:
            return "PROTECTION"
        if state.startswith("parallel_curve"):
            return "CURVE"
        return "STORAGE_ACTIVE"
    return "E3DC_AUTONOM"


def _storage_execution_class(record: Dict[str, Any], decision: Dict[str, Any]) -> str:
    target_execution = str(
        _storage_target_history(record).get("execution_class") or ""
    ).strip().upper()
    if target_execution:
        return target_execution
    r5_execution = str(_record_r5(record).get("execution_class") or "").strip().upper()
    if r5_execution:
        return r5_execution
    auto_limit = _storage_auto_limit(record, decision)
    mode_name = _storage_mode_name(decision)
    if mode_name == "AUTO":
        if bool(auto_limit.get("release")):
            return "AUTO_RELEASE"
        if bool(auto_limit.get("enabled")):
            return "AUTO_LIMITED"
        return "AUTO_FREE"
    return mode_name


def _storage_state_reason(record: Dict[str, Any], decision: Dict[str, Any]) -> str:
    r5_state_reason = str(_record_r5(record).get("state_reason") or "").strip().upper()
    if r5_state_reason:
        return r5_state_reason
    state = _normalize_storage_state(record, decision)
    if state:
        return state
    return "UNKNOWN"


def _storage_value_signature(record: Dict[str, Any], decision: Dict[str, Any]) -> str:
    target_signature = str(
        _storage_target_history(record).get("value_signature") or ""
    ).strip()
    if target_signature:
        return target_signature
    r5_signature = str(_record_r5(record).get("value_signature") or "").strip()
    if r5_signature:
        return r5_signature
    auto_limit = _storage_auto_limit(record, decision)
    if auto_limit:
        enabled = "1" if bool(auto_limit.get("enabled")) else "0"
        release = "1" if bool(auto_limit.get("release")) else "0"
        max_charge_w = _safe_int(auto_limit.get("max_charge_w"), 0)
        max_discharge_w = _safe_int(auto_limit.get("max_discharge_w"), 0)
        discharge_start_w = _safe_int(auto_limit.get("discharge_start_w"), 0)
        return f"auto_limit:{enabled}:{release}:{max_charge_w}:{max_discharge_w}:{discharge_start_w}"
    mode_name = _storage_mode_name(decision)
    value_w = _safe_int(
        decision.get("val", decision.get("val_w", decision.get("value", record.get("val", 0)))),
        0,
    )
    return f"mode:{mode_name}:{value_w}"


def _storage_execution_output_info(record: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    del decision  # Der fachliche Sollwert ist ausdrücklich keine Ausgangsevidenz.
    contract = (
        record.get("rscp_execution")
        if isinstance(record.get("rscp_execution"), dict)
        else {}
    )
    schema_valid = bool(
        contract.get("schema_version") == "storage_rscp_execution_history_v1"
        and _safe_int(contract.get("contract_version"), 0) == 1
    )
    execution_class = str(contract.get("execution_class") or "").strip().upper()
    output_signature = str(contract.get("value_signature") or "").strip()
    output_key = str(contract.get("output_key") or "").strip()
    readback = contract.get("readback") if isinstance(contract.get("readback"), dict) else {}
    typed_readback = bool(
        isinstance(readback.get("limits_used"), bool)
        and all(
            isinstance(readback.get(key), int)
            and not isinstance(readback.get(key), bool)
            and readback.get(key) >= 0
            for key in ("max_charge_w", "max_discharge_w", "discharge_start_w")
        )
    )
    derived_execution_class = (
        "AUTO_LIMITED" if readback.get("limits_used") else "AUTO_FREE"
    ) if typed_readback else ""
    derived_output_signature = (
        "power_settings:%d:%d:%d:%d"
        % (
            1 if readback["limits_used"] else 0,
            readback["max_charge_w"],
            readback["max_discharge_w"],
            readback["discharge_start_w"],
        )
        if typed_readback
        else ""
    )
    readback_contract_consistent = bool(
        typed_readback
        and execution_class == derived_execution_class
        and output_signature == derived_output_signature
    )
    expected_key = (
        f"{execution_class}|{output_signature}"
        if execution_class and output_signature
        else ""
    )
    proof = str(contract.get("evidence") or "").strip()
    proof_valid = proof in ("confirmed_new_output", "retained_readback")
    direct_confirmed_status = contract.get("readback_status") in (
        "confirmed",
        "confirmed_bounded_zero",
        "confirmed_nonoptimal",
        "confirmed_from_get_ack_unknown",
    )
    transaction_delta = contract.get("transaction_set_request_delta")
    transaction_delta_valid = bool(
        isinstance(transaction_delta, int)
        and not isinstance(transaction_delta, bool)
        and transaction_delta >= 0
    )
    retained_transaction_proof = bool(
        contract.get("retained_transaction_coherent") is True
        and contract.get("retained_transaction") is True
        and contract.get("requested_present") is True
        and contract.get("requested_matches_readback") is True
        and contract.get("transaction_send_called") is True
        and contract.get("transaction_confirmed") is True
        and contract.get("transaction_retained") is True
        and contract.get("output_complete") is True
        and contract.get("wire_write_confirmed") is True
        and contract.get("wire_write_retained") is True
        and contract.get("readback_status") == "confirmed_unchanged"
        and contract.get("readback_source") == "canonical_live"
    )
    retained_canonical_proof = bool(
        contract.get("retained_canonical_readback_coherent") is True
        and contract.get("retained_canonical_readback") is True
        and contract.get("readback_status") == "confirmed_from_live_readback"
        and contract.get("readback_source") == "canonical_live"
        and contract.get("canonical_requested_coherent") is True
        and (
            contract.get("requested_present") is False
            or contract.get("requested_matches_readback") is True
        )
    )
    proof_fields_valid = bool(
        (
            proof == "confirmed_new_output"
            and contract.get("confirmed_new_output") is True
            and direct_confirmed_status
            and contract.get("wire_write_attempted") is True
            and contract.get("wire_write_issued") is True
            and contract.get("wire_write_confirmed") is True
            and contract.get("wire_write_retained") is False
            and contract.get("output_complete") is True
            and contract.get("requested_matches_readback") is True
            and contract.get("transaction_coherent_new") is True
            and contract.get("transaction_send_called") is True
            and transaction_delta_valid
            and transaction_delta > 0
            and contract.get("transaction_wire_write_attempted") is True
            and contract.get("transaction_attempted") is True
            and contract.get("transaction_issued") is True
            and contract.get("transaction_confirmed") is True
            and contract.get("transaction_retained") is False
            and contract.get("transaction_partial") is False
        )
        or (
            proof == "retained_readback"
            and contract.get("retained_output") is True
            and contract.get("wire_write_attempted") is False
            and (retained_transaction_proof or retained_canonical_proof)
            and contract.get("transaction_wire_write_attempted") is False
            and transaction_delta_valid
            and transaction_delta == 0
            and contract.get("transaction_attempted") is False
            and contract.get("transaction_issued") is False
            and contract.get("transaction_partial") is False
        )
    )
    evidence_valid = bool(
        schema_valid
        and contract.get("evidence_valid") is True
        and proof_valid
        and proof_fields_valid
        and readback_contract_consistent
        and contract.get("unconfirmed_other_write") is False
        and contract.get("incoherent_other_step") is False
        and expected_key
        and output_key == expected_key
        and contract.get("readback_confirmed") is True
        and contract.get("readback_fresh") is True
    )
    if evidence_valid:
        evidence = "typed"
    elif contract:
        evidence = "invalid"
        output_key = ""
    else:
        # Alte History-Records enthalten nur den Sollvertrag in ``r5``. Sie
        # bleiben auswertbar, dürfen aber keinen Hardwareausgang vortäuschen.
        evidence = "missing"
        output_key = ""
    reason = None
    if not evidence_valid:
        if "rscp_execution" not in record:
            reason = "legacy_output_contract_missing"
        elif not schema_valid or not typed_readback:
            reason = "output_contract_invalid"
        elif contract.get("readback_fresh") is False:
            reason = "readback_stale"
        elif proof == "issued_unconfirmed" or contract.get("unconfirmed_other_write") is True:
            reason = "output_unconfirmed"
        elif contract.get("readback_confirmed") is not True:
            reason = "output_unconfirmed"
        else:
            reason = "transaction_binding_invalid"
    return {
        "execution_class": execution_class if evidence_valid else "",
        "output_signature": output_signature if evidence_valid else "",
        "output_key": output_key,
        "output_evidence": evidence,
        "output_proof": proof or None,
        "output_evidence_reason": reason,
    }


def _storage_window_has_output_change(events: Sequence[Dict[str, Any]]) -> bool:
    if len(events) != 3:
        return False
    output_keys = [str(event.get("output_key") or "") for event in events]
    return bool(
        all(output_keys)
        and output_keys[0] == output_keys[2]
        and output_keys[0] != output_keys[1]
    )


def detect_storage_transition_chatter(
    events: Iterable[Dict[str, Any]],
    *,
    min_gap_s: int,
    required_actions_any: Sequence[str],
) -> Dict[str, Any]:
    """Hält Owner-/State-Wechsel informativ, solange der Ausgang unverändert blieb."""

    timeline = [event for event in events if isinstance(event, dict)]
    chatter = control_safety.detect_state_chatter(
        timeline,
        min_gap_s=min_gap_s,
        required_actions_any=required_actions_any,
        reason_keys=("protection_reason", "reason"),
        protection_reason_markers=STORAGE_DECISION_PATH_PROTECTION_MARKERS,
    )
    raw = chatter.get("violations") if isinstance(chatter.get("violations"), list) else []
    violations = [
        violation
        for violation in raw
        if _storage_window_has_output_change(
            violation.get("events") if isinstance(violation.get("events"), list) else []
        )
    ]
    counts: Dict[str, int] = {"records": len(timeline)}
    for violation in violations:
        name = str(violation.get("type") or "storage_transition_pingpong")
        counts[name] = counts.get(name, 0) + 1
    decision_only_count = len(raw) - len(violations)
    if decision_only_count:
        counts["decision_only_aba"] = decision_only_count
    return {"ok": not violations, "violations": violations, "counts": counts}


def detect_storage_execution_chatter(
    events: Iterable[Dict[str, Any]],
    *,
    min_gap_s: int,
) -> Dict[str, Any]:
    """Bewertet nur belegtes A-B-A der tatsächlichen Execution-/Ausgangssignatur."""

    timeline = sorted(
        [event for event in events if isinstance(event, dict)],
        key=_event_ts_for_history_analysis,
    )
    recent: List[Dict[str, Any]] = []
    counts: Dict[str, int] = {"records": len(timeline)}
    violations: List[Dict[str, Any]] = []
    evidence_limit = 0
    for event in timeline:
        if str(event.get("output_evidence") or "") == "missing":
            evidence_limit += 1
        recent.append(event)
        del recent[:-3]
        if len(recent) != 3:
            continue
        first_key = str(recent[0].get("output_key") or "")
        middle_key = str(recent[1].get("output_key") or "")
        last_key = str(recent[2].get("output_key") or "")
        if (
            not all((first_key, middle_key, last_key))
            or not (first_key == last_key and first_key != middle_key)
        ):
            continue
        age_s = _event_ts_for_history_analysis(recent[-1]) - _event_ts_for_history_analysis(recent[0])
        if age_s >= min_gap_s:
            continue
        actions = [str(item.get("action") or "UNKNOWN").upper() for item in recent]
        executions = [str(item.get("execution_class") or "UNKNOWN").upper() for item in recent]
        pattern = executions if len(set(actions)) == 1 else actions
        name = "_".join(pattern).lower()
        counts[name] = counts.get(name, 0) + 1
        violations.append({
            "type": name,
            "actor": "storage",
            "age_s": int(round(age_s)),
            "events": list(recent),
        })
    if evidence_limit:
        counts["execution_evidence_limit"] = evidence_limit
    return {"ok": not violations, "violations": violations, "counts": counts}


def _storage_budget_record(record: Dict[str, Any]) -> Dict[str, Any]:
    budget = record.get("storage_budget")
    return budget if isinstance(budget, dict) else {}


def _budget_executor_nested(record: Dict[str, Any], key: str) -> Dict[str, Any]:
    budget = _storage_budget_record(record)
    value = budget.get(key)
    return value if isinstance(value, dict) else {}


def _first_non_empty(*values: Any) -> Any:
    for value in values:
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        return value
    return None


def _contract_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return bool(default)
    return _truthy(value)


def _typed_contract_bool(value: Any) -> Optional[bool]:
    """Fehlende oder anders typisierte Belege sind weder wahr noch falsch."""
    return value if isinstance(value, bool) else None


def _typed_contract_number(value: Any) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _budget_executor_output_flags(gate: Dict[str, Any], latch: Dict[str, Any], ack: Dict[str, Any]) -> Dict[str, bool]:
    return {
        "would_write_consumer_allocations": any(
            _contract_bool(contract.get("would_write_consumer_allocations"))
            for contract in (gate, latch, ack)
        ),
        "would_send_rscp": any(_contract_bool(contract.get("would_send_rscp")) for contract in (gate, latch, ack)),
        "would_command_wallbox": any(
            _contract_bool(contract.get("would_command_wallbox")) for contract in (gate, latch, ack)
        ),
        "would_command_heatpump": any(
            _contract_bool(contract.get("would_command_heatpump")) for contract in (gate, latch, ack)
        ),
    }


def _storage_budget_executor_shadow_info(record: Dict[str, Any]) -> Dict[str, Any]:
    r5 = _record_r5(record)
    budget = _storage_budget_record(record)
    gate = _budget_executor_nested(record, "executor_gate")
    latch = _budget_executor_nested(record, "executor_latch")
    ack = _budget_executor_nested(record, "executor_ack")
    present = bool(gate or latch or ack) or any(
        str(key).startswith("budget_executor_") for key in r5.keys()
    )
    if not present:
        return {"present": False}

    gate_class = str(_first_non_empty(r5.get("budget_executor_gate_class"), gate.get("gate_class")) or "")
    latch_class = str(_first_non_empty(r5.get("budget_executor_latch_class"), latch.get("latch_class")) or "")
    ack_class = str(_first_non_empty(r5.get("budget_executor_ack_class"), ack.get("ack_class")) or "")
    ack_source = str(_first_non_empty(r5.get("budget_executor_ack_source"), ack.get("ack_source")) or "").strip()
    ack_expected_source = str(
        _first_non_empty(r5.get("budget_executor_ack_expected_source"), ack.get("expected_ack_source"))
        or "storage_budget_executor"
    ).strip()
    source_allowed = ack_source in BUDGET_EXECUTOR_ACK_SOURCES and _contract_bool(
        _first_non_empty(ack.get("ack_source_allowed"), ack_source in BUDGET_EXECUTOR_ACK_SOURCES),
        ack_source in BUDGET_EXECUTOR_ACK_SOURCES,
    )
    gate_target_sink = str(
        _first_non_empty(r5.get("budget_executor_gate_target_sink"), gate.get("target_sink"), latch.get("gate_target_sink"))
        or "none"
    )
    gate_target_w = _safe_int(
        _first_non_empty(r5.get("budget_executor_gate_target_w"), gate.get("target_w"), latch.get("gate_target_w")),
        0,
    )
    latch_sink = str(_first_non_empty(r5.get("budget_executor_latch_sink"), latch.get("accepted_sink")) or "none")
    latch_target_w = _safe_int(
        _first_non_empty(r5.get("budget_executor_latch_target_w"), latch.get("accepted_target_w")),
        0,
    )
    ack_target_w = _safe_int(_first_non_empty(ack.get("ack_target_w"), r5.get("budget_executor_ack_target_w")), 0)
    output_flags = _budget_executor_output_flags(gate, latch, ack)

    return {
        "present": True,
        "gate_class": gate_class,
        "gate_data_valid": _typed_contract_bool(gate.get("data_valid")),
        "gate_open_shadow": _contract_bool(
            _first_non_empty(r5.get("budget_executor_gate_open_shadow"), gate.get("gate_open_shadow"))
        ),
        "gate_target_sink": gate_target_sink,
        "gate_target_w": gate_target_w,
        "latch_class": latch_class,
        "latch_active_shadow": _contract_bool(
            _first_non_empty(r5.get("budget_executor_latch_active_shadow"), latch.get("accepted_active_shadow"))
        ),
        "latch_sink": latch_sink,
        "latch_target_w": latch_target_w,
        "latch_hold_previous": _typed_contract_bool(latch.get("hold_previous_output_shadow")),
        "latch_release_allowed": _typed_contract_bool(latch.get("release_allowed_shadow")),
        "latch_safety_release": _typed_contract_bool(latch.get("safety_release")),
        "latch_hold_remaining_s": _typed_contract_number(latch.get("hold_remaining_s")),
        "latch_age_s": _typed_contract_number(latch.get("accepted_age_s")),
        "latch_min_runtime_s": _typed_contract_number(latch.get("min_runtime_s")),
        "ack_class": ack_class,
        "ack_required_shadow": _contract_bool(
            _first_non_empty(r5.get("budget_executor_ack_required_shadow"), ack.get("ack_required_shadow"))
        ),
        "ack_valid_shadow": _contract_bool(
            _first_non_empty(r5.get("budget_executor_ack_valid_shadow"), ack.get("ack_valid_shadow"))
        ),
        "ack_source": ack_source,
        "ack_expected_source": ack_expected_source,
        "ack_source_allowed": bool(source_allowed),
        "ack_sink": str(ack.get("ack_sink") or "none"),
        "ack_target_w": ack_target_w,
        "sink_matches": _typed_contract_bool(ack.get("sink_matches")),
        "target_matches": _typed_contract_bool(ack.get("target_matches")),
        "signature_matches": _typed_contract_bool(ack.get("signature_matches")),
        "ack_age_s": _typed_contract_number(ack.get("ack_age_s")),
        "ack_timeout_s": _typed_contract_number(ack.get("ack_timeout_s")),
        "productive_allowed_shadow": _contract_bool(
            _first_non_empty(
                r5.get("budget_executor_ack_productive_allowed_shadow"),
                ack.get("productive_allowed_shadow"),
            )
        ),
        "release_latch_shadow": _contract_bool(
            _first_non_empty(r5.get("budget_executor_ack_release_latch_shadow"), ack.get("release_latch_shadow"))
        ),
        "fallback_action": str(
            _first_non_empty(r5.get("budget_executor_ack_fallback_action"), ack.get("fallback_action"))
            or ""
        ),
        "runtime_enabled": _contract_bool(
            _first_non_empty(r5.get("ems_budget_runtime_enabled"), budget.get("runtime", {}).get("enabled") if isinstance(budget.get("runtime"), dict) else None)
        ),
        "runtime_active": _contract_bool(
            _first_non_empty(r5.get("ems_budget_runtime_active"), budget.get("runtime", {}).get("active") if isinstance(budget.get("runtime"), dict) else None)
        ),
        "runtime_class": str(
            _first_non_empty(r5.get("ems_budget_runtime_class"), budget.get("runtime", {}).get("runtime_class") if isinstance(budget.get("runtime"), dict) else "")
            or ""
        ),
        "runtime_safe_fallback": _contract_bool(
            _first_non_empty(r5.get("ems_budget_runtime_safe_fallback"), budget.get("runtime", {}).get("safe_fallback") if isinstance(budget.get("runtime"), dict) else None)
        ),
        "blockers": _as_reason_list(r5.get("budget_executor_ack_blockers") or ack.get("blockers")),
        "shadow_only": all(
            _contract_bool(contract.get("shadow_only"), True)
            for contract in (gate, latch, ack)
            if contract
        ),
        **output_flags,
    }


def storage_budget_executor_shadow_events(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    for index, record in enumerate(records):
        info = _storage_budget_executor_shadow_info(record)
        if not info.get("present"):
            continue
        decision = _decision(record)
        ts = _parse_ts_s(record.get("ts", record.get("time")), 1_800_000_000.0 + index * 10.0)
        action = "%s|%s|%s" % (
            info.get("gate_class") or "no_gate",
            info.get("latch_class") or "no_latch",
            info.get("ack_class") or "no_ack",
        )
        events.append({
            "actor": "storage_budget_executor_shadow",
            "ts": float(ts),
            "action": action.upper(),
            "reason": _reason_text(decision.get("reason"), decision.get("state"), ",".join(info.get("blockers", []))),
            "source": "storage_budget_executor_shadow",
            "target_reachable": True,
            **{key: value for key, value in info.items() if key != "present"},
        })
    return events


def _budget_executor_hold_evidence(event: Dict[str, Any]) -> Optional[bool]:
    """Ein geschlossenes Gate erlaubt nur den belegten, zeitbegrenzten Alt-Halt."""
    if event.get("latch_class") not in {"release_blocked_min_runtime", "switch_blocked_min_runtime"}:
        return False
    if event.get("gate_class") in {"blocked_import_guard", "blocked_data_quality"}:
        return False
    required_bools = {
        "gate_data_valid": True, "latch_hold_previous": True,
        "latch_release_allowed": False, "latch_safety_release": False,
    }
    if any(event.get(key) is not None and event.get(key) is not expected for key, expected in required_bools.items()):
        return False
    remaining = event.get("latch_hold_remaining_s")
    age = event.get("latch_age_s")
    minimum = event.get("latch_min_runtime_s")
    if remaining is not None and remaining <= 0:
        return False
    if age is not None and age < 0:
        return False
    if minimum is not None and minimum <= 0:
        return False
    if all(value is not None for value in (remaining, age, minimum)):
        if age >= minimum or abs(age + remaining - minimum) > 0.2:
            return False
    if any(event.get(key) is None for key in required_bools) or any(value is None for value in (remaining, age, minimum)):
        return None
    return bool(event.get("latch_sink") in {"wallbox", "heatpump"} and event.get("latch_target_w", 0) > 0)


def detect_storage_budget_executor_shadow(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    timeline = sorted(
        [event for event in events if isinstance(event, dict)],
        key=lambda event: _event_ts_for_history_analysis(event),
    )
    counts: Dict[str, int] = {"records": len(timeline)}
    violations: List[Dict[str, Any]] = []
    for event in timeline:
        evidence_missing = False
        gate_class = str(event.get("gate_class") or "none")
        latch_class = str(event.get("latch_class") or "none")
        ack_class = str(event.get("ack_class") or "none")
        counts[f"gate:{gate_class}"] = counts.get(f"gate:{gate_class}", 0) + 1
        counts[f"latch:{latch_class}"] = counts.get(f"latch:{latch_class}", 0) + 1
        counts[f"ack:{ack_class}"] = counts.get(f"ack:{ack_class}", 0) + 1

        ack_source = str(event.get("ack_source") or "").strip()
        ack_source_allowed = bool(event.get("ack_source_allowed"))
        ack_valid = bool(event.get("ack_valid_shadow"))
        productive_allowed = bool(event.get("productive_allowed_shadow"))
        runtime_enabled = bool(event.get("runtime_enabled"))
        runtime_active = bool(event.get("runtime_active"))
        runtime_safe_fallback = bool(event.get("runtime_safe_fallback"))
        gate_open = bool(event.get("gate_open_shadow"))
        latch_active = bool(event.get("latch_active_shadow"))
        if not bool(event.get("shadow_only", True)):
            violations.append({"type": "budget_executor_not_shadow_only", "event": event})
        for flag in (
            "would_write_consumer_allocations",
            "would_send_rscp",
            "would_command_wallbox",
            "would_command_heatpump",
        ):
            if bool(event.get(flag)):
                violations.append({"type": "budget_executor_would_command_runtime", "flag": flag, "event": event})
        if ack_source and not ack_source_allowed:
            violations.append({
                "type": "budget_executor_ack_from_noncentral_source",
                "source": ack_source,
                "expected_source": event.get("ack_expected_source"),
                "event": event,
            })
        if ack_valid and not ack_source_allowed:
            violations.append({"type": "budget_executor_ack_valid_from_invalid_source", "event": event})
        matches = [event.get(key) for key in ("sink_matches", "target_matches", "signature_matches")]
        if ack_valid:
            if any(value is False for value in matches):
                violations.append({"type": "budget_executor_ack_valid_without_exact_match", "event": event})
            if any(value is None for value in matches):
                evidence_missing = True
            age, timeout = event.get("ack_age_s"), event.get("ack_timeout_s")
            if age is None or timeout is None:
                evidence_missing = True
            elif age < 0 or timeout <= 0 or age > timeout:
                violations.append({"type": "budget_executor_ack_valid_but_stale", "event": event})
        gate_or_hold = True if gate_open else _budget_executor_hold_evidence(event)
        if (productive_allowed or runtime_active) and gate_or_hold is None:
            evidence_missing = True
        if productive_allowed and (not (latch_active and ack_valid and ack_source_allowed) or gate_or_hold is False):
            violations.append({"type": "budget_executor_productive_without_valid_ack", "event": event})
        if productive_allowed and ack_class != "ack_confirmed_shadow":
            violations.append({"type": "budget_executor_productive_without_confirmed_class", "event": event})
        if runtime_active and not runtime_enabled:
            violations.append({"type": "ems_budget_runtime_active_while_disabled", "event": event})
        if runtime_active and (not (latch_active and ack_valid and ack_source_allowed and productive_allowed) or gate_or_hold is False):
            violations.append({"type": "ems_budget_runtime_active_without_confirmed_executor", "event": event})
        if runtime_safe_fallback and runtime_active:
            violations.append({"type": "ems_budget_runtime_active_during_safe_fallback", "event": event})
        if evidence_missing:
            counts["executor_evidence_limit"] = counts.get("executor_evidence_limit", 0) + 1
    return {"ok": not violations, "violations": violations, "counts": counts}


def storage_records_to_contract_events(records: Iterable[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    contract_events: List[Dict[str, Any]] = []
    execution_events: List[Dict[str, Any]] = []
    state_reason_events: List[Dict[str, Any]] = []
    value_events: List[Dict[str, Any]] = []
    last_contract: Dict[str, str] = {}
    last_state_reason: Dict[str, str] = {}
    seen_execution_classes: set[Tuple[float, str]] = set()
    previous_signature = ""
    previous_contract = ""
    previous_execution = ""
    previous_output_key = ""
    previous_state_reason = ""

    for record, ts in _ordered_history_records(records):
        decision = _decision(record)
        contract = _storage_contract_owner(record, decision)
        execution = _storage_execution_class(record, decision)
        state_reason = _storage_state_reason(record, decision)
        signature = _storage_value_signature(record, decision)
        output = _storage_execution_output_info(record, decision)
        protection_class = _storage_protection_path_class(record)
        protection_reason = "live_plausibility" if protection_class == "live_plausibility" else ""
        reason = _reason_text(
            decision.get("reason"),
            decision.get("state_label"),
            decision.get("label"),
            decision.get("state"),
            decision.get("priority"),
        )

        _append_transition(
            contract_events,
            last_contract,
            actor="storage_contract_owner",
            ts=ts,
            action=contract,
            reason=reason,
            source="storage_contract_history",
            protection_path_class=protection_class,
            protection_reason=protection_reason,
            **output,
        )
        output_key = str(output.get("output_key") or "")
        exact_execution_key = (float(ts), output_key)
        if (
            output_key
            and exact_execution_key not in seen_execution_classes
            and output_key != previous_output_key
        ):
            seen_execution_classes.add(exact_execution_key)
            execution_events.append({
                "actor": "storage_execution_class",
                "ts": float(ts),
                "action": str(output.get("execution_class") or "UNKNOWN"),
                "reason": reason,
                "source": "storage_rscp_execution_history",
                "target_reachable": True,
                "protection_path_class": protection_class,
                "protection_reason": protection_reason,
                **output,
            })
        _append_transition(
            state_reason_events,
            last_state_reason,
            actor="storage_state_reason",
            ts=ts,
            action=state_reason,
            reason=reason,
            source="storage_contract_history",
            protection_path_class=protection_class,
            protection_reason=protection_reason,
            **output,
        )

        if (
            previous_signature
            and signature != previous_signature
            and contract == previous_contract
            and execution == previous_execution
            and state_reason == previous_state_reason
        ):
            value_events.append({
                "actor": "storage_value_update",
                "ts": float(ts),
                "action": "VALUE_UPDATE",
                "reason": reason,
                "source": "storage_contract_history",
                "from": previous_signature,
                "to": signature,
                "contract_owner": contract,
                "execution_class": execution,
                "state_reason": state_reason,
            })

        previous_signature = signature
        previous_contract = contract
        previous_execution = execution
        if output_key:
            previous_output_key = output_key
        previous_state_reason = state_reason

    return {
        "storage_contract_owner": contract_events,
        "storage_execution_class": execution_events,
        "storage_state_reason": state_reason_events,
        "storage_value_update": value_events,
    }


def _record_path(record: Dict[str, Any]) -> Dict[str, Any]:
    value = record.get("path")
    return value if isinstance(value, dict) else {}


def _storage_protection_path_class(record: Dict[str, Any]) -> str:
    r5 = _record_r5(record)
    path = _record_path(record)
    subcontracts = path.get("subcontracts") if isinstance(path.get("subcontracts"), dict) else {}
    protection = subcontracts.get("protection") if isinstance(subcontracts.get("protection"), dict) else {}
    value = _first_non_empty(
        r5.get("protection_path_class"),
        protection.get("protection_class"),
    )
    return str(value or "").strip().lower()


def _storage_decision_path_info(record: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    r5 = _record_r5(record)
    path = _record_path(record)
    primary_path = str(r5.get("decision_primary_path") or path.get("primary_path") or "").strip()
    active_paths = _as_reason_list(r5.get("decision_active_paths") or path.get("active_paths"))
    subordinate_paths = _as_reason_list(r5.get("decision_subordinate_paths") or path.get("subordinate_paths"))
    veto_reasons = _as_reason_list(r5.get("decision_veto_reasons") or path.get("veto_reasons"))
    veto_required = (
        _truthy(r5.get("decision_veto_required"))
        or _truthy(path.get("veto_required"))
        or bool(veto_reasons)
    )
    path_conflict = (
        _truthy(r5.get("decision_path_conflict"))
        or _truthy(path.get("path_conflict"))
        or veto_required
    )
    return {
        "primary_path": primary_path,
        "active_paths": active_paths,
        "subordinate_paths": subordinate_paths,
        "path_conflict": path_conflict,
        "veto_required": veto_required,
        "veto_reasons": veto_reasons,
        "contract_version": _safe_int(
            r5.get("storage_decision_path_contract_version", path.get("contract_version")),
            0,
        ),
    }


def storage_decision_path_events(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw_events: List[Dict[str, Any]] = []
    for record, ts in _ordered_history_records(records):
        decision = _decision(record)
        info = _storage_decision_path_info(record, decision)
        primary_path = str(info.get("primary_path") or "").strip()
        veto_reasons = info.get("veto_reasons") if isinstance(info.get("veto_reasons"), list) else []
        if not primary_path and not veto_reasons:
            continue
        output = _storage_execution_output_info(record, decision)
        protection_class = _storage_protection_path_class(record)
        reason = _reason_text(
            ",".join(str(reason) for reason in veto_reasons),
            decision.get("reason"),
            decision.get("state"),
            decision.get("priority"),
            decision.get("protected") and "protection",
        )
        raw_events.append({
            "actor": "storage_decision_path",
            "ts": float(ts),
            "action": primary_path.upper() if primary_path else "UNKNOWN_PATH",
            "reason": reason,
            "source": "storage_path_history",
            "primary_path": primary_path,
            "active_paths": info.get("active_paths"),
            "subordinate_paths": info.get("subordinate_paths"),
            "path_conflict": bool(info.get("path_conflict")),
            "veto_required": bool(info.get("veto_required")),
            "veto_reasons": veto_reasons,
            "contract_version": info.get("contract_version"),
            "target_reachable": True,
            "protection_path_class": protection_class,
            "protection_reason": "live_plausibility" if protection_class == "live_plausibility" else "",
            **output,
        })
    events: List[Dict[str, Any]] = []
    seen_exact: set[Tuple[Any, ...]] = set()
    previous_fingerprint: Optional[Tuple[Any, ...]] = None
    for event in sorted(raw_events, key=_event_ts_for_history_analysis):
        veto_reasons = tuple(str(item) for item in event.get("veto_reasons", []) if item)
        fingerprint = (
            event.get("action"),
            bool(event.get("path_conflict")),
            bool(event.get("veto_required")),
            veto_reasons,
        )
        exact = (event.get("ts"),) + fingerprint
        if exact in seen_exact or fingerprint == previous_fingerprint:
            continue
        seen_exact.add(exact)
        previous_fingerprint = fingerprint
        events.append(event)
    return events


def detect_storage_decision_path_conflicts(
    events: Iterable[Dict[str, Any]],
    *,
    min_gap_s: int,
) -> Dict[str, Any]:
    timeline = sorted(
        [event for event in events if isinstance(event, dict)],
        key=lambda event: _event_ts_for_history_analysis(event),
    )
    chatter = control_safety.detect_state_chatter(
        timeline,
        min_gap_s=min_gap_s,
        required_actions_any=STORAGE_DECISION_PATH_CHATTER_ACTIONS,
        reason_keys=("protection_reason", "reason"),
        protection_reason_markers=STORAGE_DECISION_PATH_PROTECTION_MARKERS,
    )
    chatter_violations = chatter.get("violations") if isinstance(chatter.get("violations"), list) else []
    retained_chatter = [
        violation
        for violation in chatter_violations
        if _storage_window_has_output_change(
            violation.get("events") if isinstance(violation.get("events"), list) else []
        )
    ]
    counts: Dict[str, int] = {}
    for violation in retained_chatter:
        name = str(violation.get("type") or "storage_path_pingpong")
        counts[name] = counts.get(name, 0) + 1
    decision_only_count = len(chatter_violations) - len(retained_chatter)
    if decision_only_count:
        counts["decision_only_aba"] = decision_only_count
    counts["records"] = len(timeline)
    violations = list(retained_chatter)
    for event in timeline:
        veto_required = bool(event.get("veto_required"))
        path_conflict = bool(event.get("path_conflict"))
        if not (veto_required or path_conflict):
            continue
        if veto_required:
            counts["veto_required"] = counts.get("veto_required", 0) + 1
        if path_conflict:
            counts["path_conflict"] = counts.get("path_conflict", 0) + 1
        for reason in event.get("veto_reasons") if isinstance(event.get("veto_reasons"), list) else []:
            key = "veto:%s" % str(reason).strip().lower()
            counts[key] = counts.get(key, 0) + 1
        violations.append({
            "type": "storage_path_veto_required" if veto_required else "storage_path_conflict",
            "actor": event.get("actor", "storage_decision_path"),
            "ts": event.get("ts"),
            "path": event.get("primary_path"),
            "veto_reasons": event.get("veto_reasons") if isinstance(event.get("veto_reasons"), list) else [],
            "events": [event],
            "event": event,
        })
    return {"ok": not violations, "violations": violations, "counts": counts}


def _as_reason_list(value: Any) -> List[str]:
    if isinstance(value, list):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, tuple):
        return [str(item) for item in value if str(item or "").strip()]
    if isinstance(value, str) and value.strip():
        return [value.strip()]
    return []


def _storage_live_plausibility_reasons(record: Dict[str, Any], decision: Dict[str, Any]) -> List[str]:
    live_plausibility = decision.get("live_plausibility") if isinstance(decision.get("live_plausibility"), dict) else {}
    reasons = _as_reason_list(live_plausibility.get("reasons"))
    reasons.extend(_as_reason_list(decision.get("RSCP_Glitch_Reasons")))

    diagnostics = record.get("diagnostics") if isinstance(record.get("diagnostics"), dict) else {}
    power = diagnostics.get("power_decision_stability") if isinstance(diagnostics.get("power_decision_stability"), dict) else {}
    power_invalid = (
        power.get("sample_valid") is False
        or power.get("usable_for_budget") is False
        or str(power.get("status", "")).strip().lower() == "invalid_sample_hold"
    )
    if power_invalid:
        reasons.append(str(power.get("status") or "power_decision_invalid_sample"))

    path = _record_path(record)
    subcontracts = path.get("subcontracts") if isinstance(path.get("subcontracts"), dict) else {}
    protection = subcontracts.get("protection") if isinstance(subcontracts.get("protection"), dict) else {}
    if _storage_protection_path_class(record) == "live_plausibility":
        protection_reason = str(protection.get("reason") or "").lower()
        for token in re.findall(r"[a-z][a-z0-9_]{2,80}", protection_reason):
            if "_" in token:
                reasons.append(token)

    seen: Dict[str, bool] = {}
    compact: List[str] = []
    for reason in reasons:
        key = str(reason).strip()
        if key and key not in seen:
            seen[key] = True
            compact.append(key)
    return compact


def _storage_critical_live_events(decision: Dict[str, Any]) -> List[Dict[str, Any]]:
    events = decision.get("critical_events") if isinstance(decision.get("critical_events"), list) else []
    return [
        event
        for event in events
        if isinstance(event, dict) and str(event.get("scope") or "").strip().lower() == "live_data"
    ]


def _storage_live_data_info(record: Dict[str, Any], decision: Dict[str, Any]) -> Dict[str, Any]:
    inputs = _inputs(record)
    critical_events = _storage_critical_live_events(decision)
    sample_values: List[bool] = []
    stale_values: List[bool] = []

    if "live_sample_valid" in inputs:
        sample_values.append(_contract_bool(inputs.get("live_sample_valid"), True))
    if "live_stale" in inputs:
        stale_values.append(_contract_bool(inputs.get("live_stale"), False))
    for event in critical_events:
        if "sample_valid" in event:
            sample_values.append(_contract_bool(event.get("sample_valid"), True))
        if "stale" in event:
            stale_values.append(_contract_bool(event.get("stale"), False))

    # Legacy-Diagnosepakete vor dem typisierten History-Vertrag bleiben auswertbar.
    if "live_sample_valid" in decision:
        sample_values.append(_contract_bool(decision.get("live_sample_valid"), True))
    if decision.get("live_sample_invalid") is not None:
        sample_values.append(not _contract_bool(decision.get("live_sample_invalid"), False))

    diagnostics = record.get("diagnostics") if isinstance(record.get("diagnostics"), dict) else {}
    power = diagnostics.get("power_decision_stability") if isinstance(diagnostics.get("power_decision_stability"), dict) else {}
    if "sample_valid" in power:
        sample_values.append(_contract_bool(power.get("sample_valid"), True))
    if "usable_for_budget" in power:
        sample_values.append(_contract_bool(power.get("usable_for_budget"), True))

    evidence_values = [
        value
        for source, keys in (
            (inputs, ("live_sample_valid", "live_stale", "home_power_valid", "grid_power_valid")),
            (decision, ("live_sample_valid", "live_sample_invalid")),
            (power, ("sample_valid", "usable_for_budget")),
            *((event, ("sample_valid", "stale")) for event in critical_events),
        )
        for key in keys if key in source
        for value in (source[key],)
    ]
    typed_live_evidence = bool(evidence_values) and all(isinstance(value, bool) for value in evidence_values)

    live_sample_valid: Optional[bool] = all(sample_values) if sample_values else None
    live_stale: Optional[bool] = any(stale_values) if stale_values else None
    home_power_valid = (
        _contract_bool(inputs.get("home_power_valid"), True)
        if "home_power_valid" in inputs
        else None
    )
    grid_power_valid = (
        _contract_bool(inputs.get("grid_power_valid"), True)
        if "grid_power_valid" in inputs
        else None
    )
    invalid = bool(
        live_sample_valid is False
        or live_stale is True
        or home_power_valid is False
        or grid_power_valid is False
    )
    return {
        "live_sample_valid": live_sample_valid,
        "live_stale": live_stale,
        "home_power_valid": home_power_valid,
        "grid_power_valid": grid_power_valid,
        "typed_live_evidence": typed_live_evidence,
        "live_evidence_reason": (
            None if typed_live_evidence else "live_contract_invalid" if evidence_values else "live_contract_missing"
        ),
        "invalid": invalid,
    }


def _storage_live_plausibility_action(decision: Dict[str, Any], protection_class: str = "") -> str:
    state = str(decision.get("state", "") or "").strip()
    if bool(decision.get("live_plausibility_preserved_discharge_owner")):
        return "DISCHARGE_OWNER_HOLD"
    if bool(decision.get("live_plausibility_preserved_charge_owner")):
        return "CHARGE_OWNER_HOLD"
    if bool(decision.get("live_plausibility_preserved_wbminsoc_contract")):
        return "WB_MINSOC_HOLD"
    if bool(decision.get("live_plausibility_preserved_auto_limit")):
        return "AUTO_LIMIT_HOLD"
    if bool(decision.get("live_plausibility_manual_override_kept")):
        return "MANUAL_OVERRIDE_HOLD"
    if state == "live_plausibility_auto" or protection_class == "live_plausibility":
        return "AUTO_GUARD"
    return "INVALID_SAMPLE"


def storage_live_plausibility_events(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    raw_events: List[Dict[str, Any]] = []
    guard_run_id = 0
    in_guard_run = False
    for record, ts in _ordered_history_records(records):
        decision = _decision(record)
        state = str(decision.get("state", record.get("state", "")) or "").strip()
        live_info = _storage_live_data_info(record, decision)
        protection_class = _storage_protection_path_class(record)
        live_sample_invalid = bool(live_info.get("invalid"))
        preserved = any(bool(decision.get(key)) for key in LIVE_PLAUSIBILITY_REASON_KEYS)
        reasons = _storage_live_plausibility_reasons(record, decision)
        if live_info.get("live_sample_valid") is False and "live_sample_invalid" not in reasons:
            reasons.append("live_sample_invalid")
        if live_info.get("live_stale") is True and "live_stale" not in reasons:
            reasons.append("live_stale")
        if live_info.get("home_power_valid") is False and "home_power_invalid" not in reasons:
            reasons.append("home_power_invalid")
        if live_info.get("grid_power_valid") is False and "grid_power_invalid" not in reasons:
            reasons.append("grid_power_invalid")
        if not (
            live_sample_invalid
            or preserved
            or state == "live_plausibility_auto"
            or protection_class == "live_plausibility"
            or reasons
        ):
            if in_guard_run:
                guard_run_id += 1
                in_guard_run = False
            continue
        in_guard_run = True
        action = _storage_live_plausibility_action(decision, protection_class)
        reason = _reason_text(
            ",".join(reasons),
            decision.get("reason"),
            state,
        )
        raw_events.append({
            "actor": "storage_live_plausibility",
            "ts": float(ts),
            "action": action,
            "reason": reason,
            "reasons": reasons,
            "source": "storage_decision_history",
            "target_reachable": True,
            "state": state,
            "protection_path_class": protection_class,
            "guard_run_id": guard_run_id,
            "evidence_limited": not bool(live_info.get("typed_live_evidence")),
            **{key: value for key, value in live_info.items() if key != "invalid"},
        })
    events: List[Dict[str, Any]] = []
    seen: set[Tuple[Any, ...]] = set()
    for event in sorted(raw_events, key=_event_ts_for_history_analysis):
        fingerprint = (
            event.get("ts"),
            event.get("action"),
            tuple(event.get("reasons", [])),
            event.get("live_sample_valid"),
            event.get("live_stale"),
            event.get("home_power_valid"),
            event.get("grid_power_valid"),
            event.get("protection_path_class"),
            event.get("guard_run_id"),
        )
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        events.append(event)
    return events


def detect_storage_live_plausibility_impact(events: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    timeline = sorted(
        [event for event in events if isinstance(event, dict)],
        key=lambda event: _event_ts_for_history_analysis(event),
    )
    counts: Dict[str, int] = {"records": len(timeline)}
    reason_counts: Dict[str, int] = {}
    for event in timeline:
        action = str(event.get("action", "") or "")
        if action:
            counts[action.lower()] = counts.get(action.lower(), 0) + 1
        reason_values = event.get("reasons") if isinstance(event.get("reasons"), list) else _reason_tokens(str(event.get("reason", "") or ""))
        for reason in reason_values:
            reason = str(reason).strip().lower()
            if not reason:
                continue
            reason_counts[reason] = reason_counts.get(reason, 0) + 1
    for reason, count in sorted(reason_counts.items()):
        counts[f"reason:{reason}"] = count

    if not timeline:
        return {"ok": True, "violations": [], "hints": [], "counts": counts}

    first_ts = _event_ts_for_history_analysis(timeline[0])
    last_ts = _event_ts_for_history_analysis(timeline[-1])
    samples = timeline[:3]
    if len(timeline) > 6:
        samples = timeline[:3] + timeline[-3:]
    else:
        samples = timeline
    guard_runs: Dict[int, List[Dict[str, Any]]] = {}
    for event in timeline:
        run_id = _safe_int(event.get("guard_run_id"), -1)
        guard_runs.setdefault(run_id, []).append(event)
    persistent_run = any(
        len(run) >= 2
        and _event_ts_for_history_analysis(run[-1]) - _event_ts_for_history_analysis(run[0])
        >= LIVE_PLAUSIBILITY_PERSISTENT_S
        for run in guard_runs.values()
    )
    repeated_or_persistent = bool(
        len(timeline) >= LIVE_PLAUSIBILITY_REPEAT_COUNT or persistent_run
    )
    counts["persistent_guard"] = 1 if repeated_or_persistent else 0
    counts["short_guard_hint"] = 0 if repeated_or_persistent else 1
    if any(bool(event.get("evidence_limited")) for event in timeline):
        counts["evidence_limit"] = sum(bool(event.get("evidence_limited")) for event in timeline)
    finding = {
        "type": "live_plausibility_guard",
        "actor": "storage",
        "severity": "warning" if repeated_or_persistent else "info",
        "age_s": int(round(max(0.0, last_ts - first_ts))),
        "count": len(timeline),
        "first_ts": first_ts,
        "last_ts": last_ts,
        "events": samples,
    }
    return {
        "ok": not repeated_or_persistent,
        "violations": [finding] if repeated_or_persistent else [],
        "hints": [] if repeated_or_persistent else [finding],
        "counts": counts,
    }


def _storage_evidence_contract(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """Zählt fehlende typisierte Evidenz ohne überlappende History-Records doppelt zu werten."""

    seen: set[str] = set()
    total = 0
    live_typed_missing = 0
    output_typed_missing = 0
    reasons: Dict[str, int] = {}
    output_counts = {key: 0 for key in STORAGE_OUTPUT_COUNTS}
    previous_output_key = ""
    for record, ts in _ordered_history_records(records):
        decision = _decision(record)
        inputs = _inputs(record)
        target = _storage_target_history(record)
        output = _storage_execution_output_info(record, decision)
        live_info = _storage_live_data_info(record, decision)
        fingerprint = json.dumps((
            float(ts),
            inputs.get("live_sample_valid"),
            inputs.get("live_stale"),
            inputs.get("home_power_valid"),
            inputs.get("grid_power_valid"),
            target.get("execution_class"),
            target.get("value_signature"),
            output.get("output_key"),
            output.get("output_evidence"),
            output.get("output_evidence_reason"),
            live_info.get("live_evidence_reason"),
        ), sort_keys=True, separators=(",", ":"))
        if fingerprint in seen:
            continue
        seen.add(fingerprint)
        total += 1
        if not live_info.get("typed_live_evidence"):
            live_typed_missing += 1
        if output.get("output_evidence") != "typed":
            output_typed_missing += 1
        for reason in (live_info.get("live_evidence_reason"), output.get("output_evidence_reason")):
            if reason:
                reasons[reason] = reasons.get(reason, 0) + 1
        if output.get("output_evidence") == "typed":
            output_counts["confirmed_observations"] += 1
            proof = output.get("output_proof")
            if proof == "confirmed_new_output":
                output_counts["confirmed_new_output_records"] += 1
            elif proof == "retained_readback":
                output_counts["retained_readback_records"] += 1
            output_key = str(output.get("output_key") or "")
            if previous_output_key and output_key != previous_output_key:
                output_counts["confirmed_output_changes"] += 1
            previous_output_key = output_key
    return {
        "records": total,
        "storage_live_typed_missing": live_typed_missing,
        "storage_output_typed_missing": output_typed_missing,
        "reasons": reasons,
        "output_counts": output_counts,
    }


def _event_ts_for_history_analysis(event: Dict[str, Any]) -> float:
    return _safe_float(event.get("ts", event.get("time")), 0.0)


def _reason_tokens(text: str) -> List[str]:
    tokens: List[str] = []
    for raw in str(text or "").replace(";", ",").split(","):
        token = raw.strip().lower()
        if not token:
            continue
        # Auf den Maschinengrund folgt Freitext; übernommen werden nur stabile Diagnoseschlüssel.
        if " " in token or ":" in token:
            continue
        if token not in tokens:
            tokens.append(token)
    return tokens


def _normalize_storage_owner(record: Dict[str, Any], decision: Dict[str, Any]) -> str:
    state = str(decision.get("state", record.get("state", "")) or "").strip()
    label = str(
        decision.get("state_label")
        or decision.get("label")
        or record.get("state_label")
        or record.get("storage_state_label")
        or ""
    ).strip()
    owner = str(
        decision.get("control_owner")
        or record.get("control_owner")
        or _record_r5(record).get("control_owner")
        or ""
    ).strip()
    owner_norm = owner.lower()
    text = " ".join([label, owner, state]).lower()
    if "wallbox manager" in text or state == "parallel_wb_auto":
        return "WALLBOX"
    if owner_norm == "market_economics" or state.startswith("market_"):
        return "MARKET"
    if "storage manager" in text:
        return "STORAGE"
    if "e3dc" in text or owner in ("e3dc_auto", "ems_limit_release") or state in ("parallel_auto", "parallel_evening_release"):
        return "E3DC"
    if owner in ("ems_auto_limit", "rscp_mode", "predump", "direct_marketing") or state:
        return "STORAGE"
    return ""


def _normalize_storage_state(record: Dict[str, Any], decision: Dict[str, Any]) -> str:
    state = str(decision.get("state", record.get("state", "")) or "").strip()
    if state:
        return state.upper()
    label = str(decision.get("state_label") or decision.get("label") or record.get("state_label") or "").strip()
    return label.upper()


def _storage_state_chatter_action(state: str) -> str:
    normalized = str(state or "").strip().upper()
    if normalized in ("PARALLEL_CURVE_CHARGE", "PARALLEL_CURVE_AUTO_HOLD"):
        return "PARALLEL_CURVE_GUIDANCE"
    return normalized


def storage_records_to_owner_state_events(records: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    owner_events: List[Dict[str, Any]] = []
    state_events: List[Dict[str, Any]] = []
    last_owner: Dict[str, str] = {}
    last_state: Dict[str, str] = {}
    for record, ts in _ordered_history_records(records):
        decision = _decision(record)
        state = _normalize_storage_state(record, decision)
        owner = _normalize_storage_owner(record, decision)
        output = _storage_execution_output_info(record, decision)
        protection_class = _storage_protection_path_class(record)
        protection_reason = "live_plausibility" if protection_class == "live_plausibility" else ""
        reason = _reason_text(
            decision.get("reason"),
            decision.get("state_label"),
            decision.get("label"),
            decision.get("state"),
            decision.get("priority"),
            decision.get("protected") and "protection",
        )
        if owner:
            _append_transition(
                owner_events,
                last_owner,
                actor="storage_owner",
                ts=ts,
                action=owner,
                reason=reason,
                source="storage_owner_history",
                protection_path_class=protection_class,
                protection_reason=protection_reason,
                **output,
            )
        if state:
            _append_transition(
                state_events,
                last_state,
                actor="storage_state",
                ts=ts,
                action=_storage_state_chatter_action(state),
                reason=reason,
                source="storage_state_history",
                raw_action=state,
                protection_path_class=protection_class,
                protection_reason=protection_reason,
                **output,
            )
    return owner_events, state_events


def _heatpump_action(record: Dict[str, Any]) -> str:
    decision = _decision(record)
    heatpump = record.get("heatpump") if isinstance(record.get("heatpump"), dict) else {}
    actions = " ".join(str(action or "") for action in decision.get("actions", []) if action is not None)
    owner = str(decision.get("heatpump_boost_owner", "") or "").lower()
    if (
        bool(decision.get("boost_active"))
        or bool(decision.get("price_boost_active"))
        or owner not in ("", "none", "null", "false")
        or "boost" in actions.lower()
    ):
        return "BOOST"
    if bool(decision.get("pv_pause_active")) or bool(decision.get("pre_pause_active")) or bool(heatpump.get("protect_block")):
        return "OFF"
    if any(heatpump.get(key) is False for key in ("power_known", "source_fresh", "status_valid")):
        return ""
    observed_running = bool(
        _safe_float(heatpump.get("wp_power_w"), 0.0) > 300.0
        or bool(heatpump.get("accepting_power"))
    )
    if bool(heatpump.get("budget_offered")) and observed_running:
        return "RUN"
    if observed_running:
        return "OBS_RUN"
    power = heatpump.get("wp_power_w")
    if (
        isinstance(power, (int, float)) and not isinstance(power, bool)
        and math.isfinite(float(power)) and power >= 0
        and heatpump.get("power_known") is not False
        and heatpump.get("source_fresh") is not False
        and heatpump.get("status_valid") is not False
    ):
        return "OBS_OFF"
    return ""


def heatpump_records_to_events(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    last: Dict[str, str] = {}
    for record, ts in _ordered_history_records(records):
        decision = _decision(record)
        inputs = _inputs(record)
        heatpump = record.get("heatpump") if isinstance(record.get("heatpump"), dict) else {}
        pause_request = inputs.get("heatpump_pause_request") if isinstance(inputs.get("heatpump_pause_request"), dict) else {}
        reason = _reason_text(
            decision.get("reason"),
            decision.get("state"),
            decision.get("price_action"),
            decision.get("market_plan_action"),
            decision.get("market_plan_reason"),
            decision.get("heatpump_boost_owner"),
            pause_request.get("reason"),
            heatpump.get("protect_block") and "protection",
            heatpump.get("targets_reached") and "target_reached",
        )
        _append_transition(
            events,
            last,
            actor="heatpump",
            ts=ts,
            action=_heatpump_action(record),
            reason=reason,
            source="energy_decision_history",
        )
    return events


def ems_decision_records_to_events(records: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    events: List[Dict[str, Any]] = []
    last: Dict[str, str] = {}
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            continue
        actor = str(record.get("actor") or f"ems:{index}").strip() or f"ems:{index}"
        decision = str(record.get("decision") or "observe").strip().lower()
        ts = _parse_ts_s(record.get("ts", record.get("time")), float("nan"))
        if isinstance(record.get("ts"), bool) or not math.isfinite(ts) or ts <= 0:
            continue
        reason = _reason_text(
            record.get("user_text"),
            ",".join(str(item) for item in record.get("blockers", []) if item) if isinstance(record.get("blockers"), list) else "",
            ",".join(str(item) for item in record.get("safety_guards", []) if item) if isinstance(record.get("safety_guards"), list) else "",
            record.get("noop_reason"),
        )
        _append_transition(
            events,
            last,
            actor=actor,
            ts=ts,
            action=decision.upper(),
            reason=reason,
            source="ems_decision_surface",
            domain=record.get("domain"),
            requested_power_w=record.get("requested_power_w"),
            granted_power_w=record.get("granted_power_w"),
            min_required_w=record.get("min_required_w"),
            missing_w=record.get("missing_w"),
            noop_reason=record.get("noop_reason"),
        )
    return events


def validate_ems_decision_records(records: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    counts: Dict[str, int] = {"records": 0}
    violations: List[Dict[str, Any]] = []
    for index, record in enumerate(records):
        if not isinstance(record, dict):
            violations.append({"type": "invalid_record", "index": index})
            continue
        counts["records"] += 1
        actor = str(record.get("actor") or "").strip()
        domain = str(record.get("domain") or "").strip()
        decision = str(record.get("decision") or "").strip().lower()
        if decision:
            counts[decision] = counts.get(decision, 0) + 1

        if record.get("schema") != "ems_decision_v1":
            violations.append({
                "type": "invalid_schema",
                "actor": actor or f"record:{index}",
                "schema": record.get("schema"),
            })
        if not actor:
            violations.append({"type": "missing_actor", "index": index})
        if not domain:
            violations.append({"type": "missing_domain", "actor": actor or f"record:{index}"})
        if decision not in EMS_DECISIONS:
            violations.append({
                "type": "invalid_decision",
                "actor": actor or f"record:{index}",
                "decision": decision,
            })

        missing_raw = record.get("missing_w")
        missing_w = _safe_float(missing_raw, 0.0) if missing_raw is not None else 0.0
        if decision == "allow" and missing_w > 0.1:
            violations.append({
                "type": "allow_with_missing_power",
                "actor": actor or f"record:{index}",
                "missing_w": missing_w,
            })
        if (
            decision in ("observe", "noop")
            and str(record.get("noop_reason") or "").strip().lower() in ("off_or_disabled", "unplugged", "observe_only")
            and missing_raw is not None
            and missing_w > 0.1
        ):
            violations.append({
                "type": "inactive_observe_with_missing_power",
                "actor": actor or f"record:{index}",
                "missing_w": missing_w,
                "noop_reason": record.get("noop_reason"),
            })
        hardware_command = record.get("hardware_command") if isinstance(record.get("hardware_command"), dict) else {}
        if decision in ("observe", "noop") and hardware_command.get("executed") is True:
            violations.append({
                "type": "inactive_decision_executed_command",
                "actor": actor or f"record:{index}",
                "decision": decision,
                "method": hardware_command.get("method"),
            })
    return {"ok": not violations, "violations": violations, "counts": counts}


def analyze_decision_history(
    input_path: str | os.PathLike[str],
    *,
    services: Optional[Iterable[str]] = None,
    min_gap_s: int = 180,
    limit_per_service: Optional[int] = None,
) -> Dict[str, Any]:
    selected = _normalize_services(services)
    history_coverage: Dict[str, Any] = {}
    records = load_diagnose_records(
        input_path, services=selected, limit_per_service=limit_per_service,
        history_coverage=history_coverage,
    )
    for service in selected:
        history_coverage.setdefault(service, _coverage_record())["decision_records"] = len(records.get(service, []))
    missing_services = [service for service in selected if len(records.get(service, [])) <= 0]
    analyzed_services = [service for service in selected if service not in missing_services]
    if not analyzed_services and not any(
        item.get("context_records") or item.get("invalid_records") for item in history_coverage.values()
    ):
        raise ValueError("missing supported decision records for requested services")
    events: Dict[str, List[Dict[str, Any]]] = {}
    checks: Dict[str, Dict[str, Any]] = {}
    effective_min_gap_s: Dict[str, int] = {}
    evidence_limits: Dict[str, int] = {}
    storage_output_counts: Dict[str, int] = {}

    if "wallbox" in analyzed_services:
        start_events, phase_events = wallbox_records_to_events(records.get("wallbox", []))
        events["wallbox_start_stop"] = start_events
        events["wallbox_phase"] = phase_events
        effective_min_gap_s["wallbox_start_stop"] = int(min_gap_s)
        effective_min_gap_s["wallbox_phase"] = int(min_gap_s)
        checks["wallbox_start_stop"] = control_safety.detect_command_chatter(
            start_events,
            unsafe_patterns=WALLBOX_START_PATTERNS,
            min_gap_s=min_gap_s,
        )
        checks["wallbox_phase"] = control_safety.detect_command_chatter(
            phase_events,
            unsafe_patterns=WALLBOX_PHASE_PATTERNS,
            min_gap_s=min_gap_s,
        )
    if "storage" in analyzed_services:
        storage_records = records.get("storage", [])
        storage_evidence = _storage_evidence_contract(storage_records)
        history_coverage["storage"]["reasons"].update(storage_evidence["reasons"])
        storage_output_counts = storage_evidence["output_counts"]
        evidence_limits["storage_live_typed_missing"] = storage_evidence["storage_live_typed_missing"]
        evidence_limits["storage_output_typed_missing"] = storage_evidence["storage_output_typed_missing"]
        storage_events = storage_records_to_events(storage_records)
        storage_owner_events, storage_state_events = storage_records_to_owner_state_events(storage_records)
        storage_contract_events = storage_records_to_contract_events(storage_records)
        storage_path_events = storage_decision_path_events(storage_records)
        storage_live_events = storage_live_plausibility_events(storage_records)
        storage_budget_executor_events = storage_budget_executor_shadow_events(storage_records)
        events["storage"] = storage_events
        events["storage_owner"] = storage_owner_events
        events["storage_state"] = storage_state_events
        events["storage_decision_path"] = storage_path_events
        events["storage_live_plausibility"] = storage_live_events
        events["storage_budget_executor_shadow"] = storage_budget_executor_events
        events.update(storage_contract_events)
        owner_gap_s = max(min_gap_s, OWNER_MIN_GAP_S)
        effective_min_gap_s["storage"] = int(min_gap_s)
        effective_min_gap_s["storage_owner"] = int(owner_gap_s)
        effective_min_gap_s["storage_state"] = int(owner_gap_s)
        effective_min_gap_s["storage_decision_path"] = int(owner_gap_s)
        effective_min_gap_s["storage_contract_owner"] = int(owner_gap_s)
        effective_min_gap_s["storage_live_plausibility"] = int(min_gap_s)
        effective_min_gap_s["storage_budget_executor_shadow"] = int(min_gap_s)
        checks["storage"] = detect_storage_execution_chatter(
            storage_events,
            min_gap_s=min_gap_s,
        )
        if storage_evidence["storage_output_typed_missing"]:
            checks["storage"]["counts"]["typed_output_evidence_limit"] = storage_evidence["storage_output_typed_missing"]
        checks["storage_owner"] = detect_storage_transition_chatter(
            storage_owner_events,
            min_gap_s=owner_gap_s,
            required_actions_any=STORAGE_OWNER_CHATTER_ACTIONS,
        )
        checks["storage_state"] = detect_storage_transition_chatter(
            storage_state_events,
            min_gap_s=owner_gap_s,
            required_actions_any=STORAGE_STATE_CHATTER_ACTIONS,
        )
        checks["storage_contract_owner"] = detect_storage_transition_chatter(
            storage_contract_events["storage_contract_owner"],
            min_gap_s=owner_gap_s,
            required_actions_any=STORAGE_CONTRACT_CHATTER_ACTIONS,
        )
        checks["storage_decision_path"] = detect_storage_decision_path_conflicts(
            storage_path_events,
            min_gap_s=owner_gap_s,
        )
        checks["storage_live_plausibility"] = detect_storage_live_plausibility_impact(storage_live_events)
        if storage_evidence["storage_live_typed_missing"]:
            checks["storage_live_plausibility"]["counts"]["typed_live_evidence_limit"] = storage_evidence["storage_live_typed_missing"]
        checks["storage_budget_executor_shadow"] = detect_storage_budget_executor_shadow(storage_budget_executor_events)
        executor_missing = checks["storage_budget_executor_shadow"]["counts"].get("executor_evidence_limit", 0)
        if executor_missing:
            evidence_limits["storage_budget_executor_missing"] = executor_missing
            history_coverage["storage"]["reasons"]["executor_binding_evidence_missing"] = executor_missing
    if "heatpump" in analyzed_services:
        heatpump_events = heatpump_records_to_events(records.get("heatpump", []))
        events["heatpump"] = heatpump_events
        effective_min_gap_s["heatpump"] = int(min_gap_s)
        checks["heatpump"] = control_safety.detect_command_chatter(
            heatpump_events,
            unsafe_patterns=HEATPUMP_PATTERNS,
            min_gap_s=min_gap_s,
        )
    if "ems" in analyzed_services:
        ems_events = ems_decision_records_to_events(records.get("ems", []))
        events["ems_decision"] = ems_events
        effective_min_gap_s["ems_decision"] = int(min_gap_s)
        checks["ems_decision"] = validate_ems_decision_records(records.get("ems", []))

    for record in records.get("heatpump", []):
        if not _heatpump_action(record):
            _coverage_reason(history_coverage["heatpump"], "heatpump_observation_missing")
    limited_history = any(
        any(reason not in {"legacy_output_contract_missing", "output_contract_invalid", "readback_stale", "output_unconfirmed", "transaction_binding_invalid", "live_contract_missing", "live_contract_invalid"} for reason in item["reasons"])
        for item in history_coverage.values()
    )
    storage_history_limited = bool(
        "storage" in history_coverage and any(
            reason in {"malformed_record", "invalid_timestamp", "invalid_history_metadata", "unknown_record_format", "aggregated_interval", "archive_gap", "record_limit_reached"}
            for reason in history_coverage["storage"]["reasons"]
        )
    )
    data_quality_check = checks.get("storage_live_plausibility")
    control_checks = {
        name: check
        for name, check in checks.items()
        if name != "storage_live_plausibility"
    }
    if not all(check.get("ok", True) for check in control_checks.values()):
        control_status = "FAIL"
    elif evidence_limits.get("storage_output_typed_missing", 0) > 0 or limited_history or not analyzed_services:
        control_status = "EVIDENCE_LIMIT"
    else:
        control_status = "PASS"
    if not isinstance(data_quality_check, dict):
        data_quality_status = "NOT_ANALYZED"
    elif data_quality_check.get("ok") is not True:
        data_quality_status = "FAIL"
    elif evidence_limits.get("storage_live_typed_missing", 0) > 0 or storage_history_limited:
        data_quality_status = "EVIDENCE_LIMIT"
    elif data_quality_check.get("hints"):
        data_quality_status = "HINT"
    else:
        data_quality_status = "PASS"
    status = "FAIL" if "FAIL" in {control_status, data_quality_status} or "EVIDENCE_LIMIT" in {control_status, data_quality_status} else "PASS"
    return {
        "status": status,
        "control_status": control_status,
        "data_quality_status": data_quality_status,
        "completeness": "PARTIAL" if missing_services else "COMPLETE",
        "services": list(selected),
        "analyzed_services": analyzed_services,
        "missing_services": missing_services,
        "records": {service: len(records.get(service, [])) for service in selected},
        "events": {name: len(value) for name, value in events.items()},
        "checks": checks,
        "event_samples": {name: value[:12] for name, value in events.items()},
        "effective_min_gap_s": effective_min_gap_s,
        "evidence_limits": evidence_limits,
        "history_coverage": history_coverage,
        "storage_output_counts": storage_output_counts,
    }


def _print_summary(summary: Dict[str, Any]) -> None:
    print(f"Regelruhe-Analyse: {summary['status']}")
    print(f"  records: {summary['records']}")
    print(f"  events: {summary['events']}")
    for name, check in summary.get("checks", {}).items():
        print(f"  {name}: {'PASS' if check.get('ok') else 'FAIL'} {check.get('counts', {})}")
        for violation in check.get("violations", [])[:5]:
            print(f"    {violation.get('type')} actor={violation.get('actor')} age={violation.get('age_s')}s")


CLI_SCHEMA = "e3dc_decision_history_analysis_cli_v1"
_PUBLIC_ACTIONS = {
    "START", "STOP", "ON", "OFF", "PAUSE", "RESUME", "IDLE", "HOLD",
    "RELEASE", "VALUE_UPDATE", "UNKNOWN", "UNKNOWN_PATH",
    "CURVE", "DIRECT_MARKETING", "MARKET_DIRECT", "MARKET_PRICE", "PREDUMP",
    "PROTECTION", "WALLBOX_SUPPORT", "MANUAL", "STORAGE_ACTIVE",
    "E3DC_AUTO", "E3DC_AUTONOM", "E3DC", "WALLBOX", "STORAGE", "MARKET",
    "AUTO", "AUTO_FREE", "AUTO_LIMITED", "AUTO_RELEASE", "CHRG", "DISCH", "GRID",
    "AUTO_GUARD", "INVALID_SAMPLE", "DISCHARGE_OWNER_HOLD", "CHARGE_OWNER_HOLD",
    "WB_MINSOC_HOLD", "AUTO_LIMIT_HOLD", "MANUAL_OVERRIDE_HOLD", "EVIDENCE_LIMIT",
    "PARALLEL_WB_AUTO", "1P", "2P", "3P",
    "BOOST", "RUN", "OBS_RUN", "OBS_OFF",
}

# Feste Zählerbezeichnungen; unbekannte Zustands- und Freitexte bleiben gehasht.
_PUBLIC_EXECUTOR_COUNT_LABELS = {
    "records": "Datensätze",
    "executor_evidence_limit": "Unvollständige Bestätigungsbelege",
    "gate:blocked_import_guard": "Freigabe: Netzbezugsschutz",
    "gate:blocked_data_quality": "Freigabe: Messwertschutz",
    "gate:blocked_stability_hold": "Freigabe: Stabilisierung",
    "gate:blocked_no_sink": "Freigabe: kein Verbraucher",
    "gate:export_observe_only": "Freigabe: Export nur beobachtet",
    "gate:storage_reserved_observe_only": "Freigabe: Speicher reserviert",
    "gate:blocked_no_budget": "Freigabe: kein Budget",
    "gate:blocked_minimum": "Freigabe: Mindestleistung fehlt",
    "gate:shadow_ready_wallbox": "Freigabe: Wallbox bereit",
    "gate:shadow_ready_heatpump": "Freigabe: Wärmepumpe bereit",
    "gate:blocked_unknown_sink": "Freigabe: unbekannter Verbraucher",
    "latch:safety_release": "Budgethaltung: Schutzfreigabe",
    "latch:accepted_new": "Budgethaltung: neu angenommen",
    "latch:blocked_not_controllable": "Budgethaltung: nicht steuerbar",
    "latch:idle_closed": "Budgethaltung: inaktiv",
    "latch:accepted_min_runtime": "Budgethaltung: Mindestlaufzeit",
    "latch:accepted_runtime_satisfied": "Budgethaltung: Laufzeit erfüllt",
    "latch:switch_blocked_min_runtime": "Budgethaltung: Wechsel zurückgestellt",
    "latch:accepted_switch": "Budgethaltung: Wechsel angenommen",
    "latch:release_blocked_min_runtime": "Budgethaltung: Freigabe zurückgestellt",
    "latch:release_after_min_runtime": "Budgethaltung: nach Laufzeit freigegeben",
    "ack:ack_not_required": "Bestätigung: nicht erforderlich",
    "ack:ack_missing_timeout": "Bestätigung: Zeitüberschreitung",
    "ack:ack_pending": "Bestätigung: ausstehend",
    "ack:ack_rejected_source": "Bestätigung: falsche Quelle",
    "ack:ack_rejected_not_accepted": "Bestätigung: nicht angenommen",
    "ack:ack_stale": "Bestätigung: veraltet",
    "ack:ack_rejected_mismatch": "Bestätigung: abweichender Auftrag",
    "ack:ack_confirmed_shadow": "Bestätigung: gültig",
}


def _public_count_name(value: Any) -> str:
    text = str(value or "").strip()
    if text in _PUBLIC_EXECUTOR_COUNT_LABELS:
        return _PUBLIC_EXECUTOR_COUNT_LABELS[text]
    if text in _PUBLIC_EXECUTOR_COUNT_LABELS.values():
        return text
    return _public_action(text)


def _public_hash(value: Any, prefix: str) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    return f"{prefix}-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:10]}"


def _public_action(value: Any) -> str:
    text = str(value or "").strip().upper()
    if text in _PUBLIC_ACTIONS or re.fullmatch(r"STATE-[0-9A-F]{10}", text):
        return text
    hashed = _public_hash(text, "state")
    return hashed.upper() if hashed else ""


def _public_actor(value: Any) -> str:
    text = str(value or "").strip()
    if text == "storage_decision_path" or re.fullmatch(r"actor-[0-9a-f]{10}", text):
        return text
    return _public_hash(text, "actor")


def _public_pattern(value: Any) -> str:
    text = str(value or "").strip().lower()
    if re.fullmatch(r"[a-z0-9_:-]{1,80}", text):
        return text
    return _public_hash(text, "pattern")


def _public_number(value: Any) -> Optional[float]:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return value
    return None


def _public_event(event: Any) -> Dict[str, Any]:
    if not isinstance(event, dict):
        return {}
    result: Dict[str, Any] = {
        "actor": _public_actor(event.get("actor")),
        "action": _public_action(event.get("action")),
    }
    ts = _public_number(event.get("ts", event.get("time")))
    if ts is not None:
        result["ts"] = ts
    if "target_reachable" in event:
        result["target_reachable"] = bool(event.get("target_reachable"))
    return result


def _public_violation(violation: Any) -> Dict[str, Any]:
    if not isinstance(violation, dict):
        return {}
    result: Dict[str, Any] = {
        "type": _public_pattern(violation.get("type") or "pattern"),
        "actor": _public_actor(violation.get("actor")),
        "events": [item for item in (_public_event(event) for event in violation.get("events", [])) if item][:6],
    }
    severity = str(violation.get("severity") or "").strip().lower()
    if severity in {"info", "warning"}:
        result["severity"] = severity
    for key in ("age_s", "count", "first_ts", "last_ts"):
        value = _public_number(violation.get(key))
        if value is not None:
            result[key] = value
    return result


def public_cli_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """Liefert die einzige Ausgabeform, die den lokalen CLI-Prozess verlassen darf."""
    status = str(summary.get("status") or "ERROR").upper()
    public: Dict[str, Any] = {
        "schema": CLI_SCHEMA,
        "status": status if status in {"PASS", "FAIL", "ERROR"} else "ERROR",
    }
    if public["status"] == "ERROR":
        public["error_code"] = str(summary.get("error_code") or "unsafe_or_unreadable_input")
        public["error"] = str(summary.get("error") or "Die lokale Analyse konnte nicht sicher ausgeführt werden.")
        return public

    control_status = str(summary.get("control_status") or public["status"]).upper()
    data_quality_status = str(summary.get("data_quality_status") or "NOT_ANALYZED").upper()
    public["control_status"] = control_status if control_status in {"PASS", "FAIL", "EVIDENCE_LIMIT"} else "EVIDENCE_LIMIT"
    public["data_quality_status"] = (
        data_quality_status
        if data_quality_status in {"PASS", "HINT", "FAIL", "EVIDENCE_LIMIT", "NOT_ANALYZED"}
        else "EVIDENCE_LIMIT"
    )

    services = [str(item) for item in summary.get("services", []) if str(item) in SERVICE_ALIASES.values()]
    public["services"] = list(dict.fromkeys(services))
    analyzed_services = [
        str(item) for item in summary.get("analyzed_services", public["services"])
        if str(item) in SERVICE_ALIASES.values()
    ]
    missing_services = [
        str(item) for item in summary.get("missing_services", [])
        if str(item) in SERVICE_ALIASES.values()
    ]
    public["analyzed_services"] = list(dict.fromkeys(analyzed_services))
    public["missing_services"] = list(dict.fromkeys(missing_services))
    completeness = str(summary.get("completeness") or "COMPLETE").upper()
    public["completeness"] = completeness if completeness in {"COMPLETE", "PARTIAL"} else "COMPLETE"
    public["records"] = {
        str(name): int(value)
        for name, value in summary.get("records", {}).items()
        if str(name) in SERVICE_ALIASES.values() and isinstance(value, (int, float))
    }
    public["events"] = {
        _public_pattern(name): int(value)
        for name, value in summary.get("events", {}).items()
        if isinstance(value, (int, float))
    }
    checks: Dict[str, Any] = {}
    for name, check in summary.get("checks", {}).items():
        if not isinstance(check, dict):
            continue
        counts = {
            _public_count_name(key): int(value)
            for key, value in check.get("counts", {}).items()
            if isinstance(value, (int, float)) and _public_count_name(key)
        }
        checks[_public_pattern(name)] = {
            "ok": check.get("ok") is True,
            "counts": counts,
            "violations": [
                item
                for item in (_public_violation(value) for value in check.get("violations", []))
                if item
            ][:30],
            "hints": [
                item
                for item in (_public_violation(value) for value in check.get("hints", []))
                if item
            ][:30],
        }
    public["checks"] = checks
    public["event_samples"] = {
        _public_pattern(name): [item for item in (_public_event(event) for event in values) if item][:12]
        for name, values in summary.get("event_samples", {}).items()
        if isinstance(values, list)
    }
    public["effective_min_gap_s"] = {
        _public_pattern(name): int(value)
        for name, value in summary.get("effective_min_gap_s", {}).items()
        if isinstance(value, (int, float))
    }
    public["evidence_limits"] = {
        _public_pattern(name): int(value)
        for name, value in summary.get("evidence_limits", {}).items()
        if isinstance(value, (int, float))
    }
    public["history_coverage"] = {}
    for service, coverage in summary.get("history_coverage", {}).items():
        if service not in public["services"] or not isinstance(coverage, dict):
            continue
        public["history_coverage"][service] = {
            key: max(0, int(coverage.get(key, 0)))
            for key in HISTORY_COVERAGE_COUNTS
            if isinstance(coverage.get(key, 0), int) and not isinstance(coverage.get(key, 0), bool)
        }
        public["history_coverage"][service]["reasons"] = {
            reason: count for reason, count in coverage.get("reasons", {}).items()
            if reason in EVIDENCE_REASON_CODES and isinstance(count, int)
            and not isinstance(count, bool) and count > 0
        }
    public["storage_output_counts"] = {
        key: value for key, value in summary.get("storage_output_counts", {}).items()
        if key in STORAGE_OUTPUT_COUNTS and isinstance(value, int) and not isinstance(value, bool) and value >= 0
    }
    return public


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", help="Diagnose-ZIP, Entscheidungsverlauf oder Verzeichnis")
    parser.add_argument("--service", action="append", choices=sorted(SERVICE_ALIASES), help="Service to inspect; may be repeated")
    parser.add_argument("--min-gap-s", type=int, default=180)
    parser.add_argument("--limit-per-service", type=int)
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON")
    args = parser.parse_args(argv)

    path = Path(args.input)
    try:
        summary = analyze_decision_history(
            path,
            services=args.service,
            min_gap_s=args.min_gap_s,
            limit_per_service=args.limit_per_service,
        )
    except (ValueError, OSError, zipfile.BadZipFile, json.JSONDecodeError):
        summary = {
            "status": "ERROR",
            "error_code": "unsafe_or_unreadable_input",
            "error": "Das lokale Diagnosepaket ist nicht lesbar oder verletzt ein Sicherheitslimit.",
        }
    public_summary = public_cli_summary(summary)
    if args.json:
        print(json.dumps(public_summary, ensure_ascii=False, indent=2))
    else:
        if public_summary.get("status") == "ERROR":
            print(f"Regelruhe-Analyse: ERROR\n  {public_summary['error']}")
        else:
            _print_summary(public_summary)
    if public_summary.get("status") == "PASS":
        return 0
    if public_summary.get("status") == "FAIL":
        return 1
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
