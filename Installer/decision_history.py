#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Shared compact decision-history writer for manager diagnostics."""

from __future__ import annotations

import atexit
import copy
import datetime
import gzip
import hashlib
import json
import os
import stat
import time
from typing import Any, Callable, Dict, Iterable, Optional

try:
    from .json_cache import read_json_cached
except ImportError:  # pragma: no cover - direkter Skriptstart
    from json_cache import read_json_cached


HISTORY_SCHEMA_VERSION = "decision_history_v2"
HISTORY_BATCH_MAX_BYTES = 64 * 1024
HISTORY_BATCH_MAX_AGE_S = 30.0
HISTORY_NORMAL_HEARTBEAT_S = 300
HISTORY_LAST_OBSERVED_MAX_BYTES = 256 * 1024
_BUFFER_STATES: Dict[int, Dict[str, Any]] = {}


POWER_DECISION_FIELDS = (
    "Grid_Power",
    "Home_Power",
    "PV_Power",
    "Battery_Power",
    "Wallbox_Power",
)


def _safe_float(value: Any, default: Optional[float] = 0.0) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(value)))
    except Exception:
        return int(default)


def _cfg_bool(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() not in {"0", "false", "no", "off", "nein", "aus"}


def _atomic_write_json(path: str, payload: Dict[str, Any], mode: int = 0o664) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    try:
        os.chmod(tmp, mode)
    except Exception:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def _round_w(value: Any) -> Optional[float]:
    number = _safe_float(value, None)
    if number is None:
        return None
    return round(number, 3)


def _compact_power_signal(live: Dict[str, Any], stability_signals: Dict[str, Any], field: str) -> Optional[Dict[str, Any]]:
    signal_meta = stability_signals.get(field) if isinstance(stability_signals.get(field), dict) else {}
    raw_w = _round_w(signal_meta.get("raw_w", live.get(field)))
    ewma_w = _round_w(signal_meta.get("ewma_w", live.get(f"{field}_EWMA")))
    decision_w = _round_w(signal_meta.get("decision_w", live.get(f"{field}_Decision")))
    valid_value = signal_meta.get("valid", live.get(f"{field}_Decision_Valid"))
    if raw_w is None and ewma_w is None and decision_w is None and valid_value is None:
        return None
    result: Dict[str, Any] = {}
    if raw_w is not None:
        result["raw_w"] = raw_w
    if ewma_w is not None:
        result["ewma_w"] = ewma_w
    if decision_w is not None:
        result["decision_w"] = decision_w
    if valid_value is not None:
        result["valid"] = bool(valid_value)
    for key in ("held_by_deadband", "held_previous_invalid", "reset"):
        if key in signal_meta:
            result[key] = bool(signal_meta.get(key))
    return result


def build_power_decision_history_diagnostics(live: Dict[str, Any]) -> Dict[str, Any]:
    stability = live.get("Power_Decision_Stability") if isinstance(live.get("Power_Decision_Stability"), dict) else {}
    stability_signals = stability.get("signals") if isinstance(stability.get("signals"), dict) else {}
    signals: Dict[str, Any] = {}
    for field in POWER_DECISION_FIELDS:
        compact = _compact_power_signal(live, stability_signals, field)
        if compact:
            signals[field] = compact
    if not signals and not stability:
        return {}
    result: Dict[str, Any] = {
        "schema_version": "power_decision_history_v1",
        "signals": signals,
    }
    for key in (
        "status",
        "diagnostic_only",
        "hard_stop_bypass",
        "raw_values_preserved",
        "sample_valid",
        "usable_for_budget",
        "previous_age_s",
        "previous_stale",
        "deadband_w",
        "max_age_s",
    ):
        if key in stability:
            result[key] = stability.get(key)
    if "Power_Decision_Usable" in live and "usable_for_budget" not in result:
        result["usable_for_budget"] = bool(live.get("Power_Decision_Usable"))
    return result


def _decision_history_live_path(
    config: Dict[str, Any],
    log_dir: str,
    getter: Callable[[Dict[str, Any], str, Any], Any],
) -> str:
    configured = (
        getter(config, "decision_history_live_data_path", None)
        or getter(config, "live_data_py_path", None)
        or os.environ.get("E3DC_DECISION_HISTORY_LIVE_DATA")
    )
    if configured:
        return str(configured)
    sibling = os.path.join(os.path.dirname(os.path.abspath(log_dir)), "ramdisk", "live_data_py.json")
    if os.path.exists(sibling):
        return sibling
    return "/var/www/html/ramdisk/live_data_py.json"


def add_power_decision_history_diagnostics(
    record: Dict[str, Any],
    *,
    config: Dict[str, Any],
    log_dir: str,
    getter: Callable[[Dict[str, Any], str, Any], Any],
    logger: Any = None,
) -> None:
    path = _decision_history_live_path(config, log_dir, getter)
    live, meta = read_json_cached(path, with_meta=True)
    if not meta.get("valid"):
        if logger is not None:
            try:
                logger.debug("Power-Decision-Diagnose nicht in History übernommen: %s", meta.get("error"))
            except Exception:
                pass
        return
    if not isinstance(live, dict):
        return
    diagnostics = build_power_decision_history_diagnostics(live)
    if not diagnostics:
        return
    record.setdefault("diagnostics", {})["power_decision_stability"] = diagnostics


def _nested_value(record: Dict[str, Any], dotted_path: str) -> Any:
    current: Any = record
    for part in dotted_path.split("."):
        if not isinstance(current, dict):
            return None
        current = current.get(part)
    return current


def decision_signature(record: Dict[str, Any], paths: Iterable[str]) -> str:
    parts = []
    for path in paths:
        value = _nested_value(record, path)
        if isinstance(value, (dict, list)):
            value = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
        parts.append(f"{path}={value}")
    return "|".join(parts)


def _mapping_subset(value: Any, keys: Iterable[str]) -> Dict[str, Any]:
    source = value if isinstance(value, dict) else {}
    return {key: copy.deepcopy(source[key]) for key in keys if key in source}


def compact_history_record(
    record: Dict[str, Any],
    *,
    record_type: str,
    context_id: str,
    coalesced_summary: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Verdichtet nur das Archiv; die aktuelle Diagnosefläche bleibt vollständig."""

    compact = copy.deepcopy(record)
    service = str(compact.get("service") or "")
    if service == "storage_manager" and isinstance(compact.get("decision"), dict):
        budget = compact.get("storage_budget") if isinstance(compact.get("storage_budget"), dict) else {}
        compact["storage_budget"] = _mapping_subset(
            budget,
            (
                "contract_version", "readiness_class", "balance_class", "data_valid", "blockers",
                "iFc_w", "iMinLade_w", "abregel_charge_req_w", "abregel_target_w",
                "abregel_release_w", "storage_req_w", "free_for_consumers_w", "min_consumer_w",
                "consumer_shortfall_w", "grid_export_w", "grid_import_w", "storage_reserved_w",
            ),
        )
        # Bestätigungs- und Haltebelege nicht aus den r5-Summen rekonstruieren.
        # Nur vorhandene, begrenzte Vertragsfelder erhalten; keine Folgezustände.
        output_flags = (
            "shadow_only", "would_write_consumer_allocations", "would_send_rscp",
            "would_command_wallbox", "would_command_heatpump",
        )
        executor_fields = {
            "executor_gate": (
                "contract_version", "gate_class", "gate_open_shadow", "data_valid",
                "target_sink", "target_w",
            ),
            "executor_latch": (
                "contract_version", "latch_class", "accepted_active_shadow",
                "accepted_sink", "accepted_target_w", "accepted_age_s", "min_runtime_s",
                "hold_remaining_s", "release_allowed_shadow", "hold_previous_output_shadow",
                "safety_release",
            ),
            "executor_ack": (
                "contract_version", "ack_class", "ack_required_shadow", "ack_valid_shadow",
                "ack_source", "expected_ack_source", "ack_source_allowed", "ack_sink",
                "ack_target_w", "ack_age_s", "ack_timeout_s", "sink_matches",
                "target_matches", "signature_matches", "productive_allowed_shadow",
                "release_latch_shadow", "fallback_action",
            ),
            "runtime": ("enabled", "active", "runtime_class", "safe_fallback"),
        }
        for contract_name, fields in executor_fields.items():
            evidence = _mapping_subset(budget.get(contract_name), fields + output_flags)
            if evidence:
                compact["storage_budget"][contract_name] = evidence
        path = compact.get("path") if isinstance(compact.get("path"), dict) else {}
        compact["path"] = _mapping_subset(
            path,
            (
                "contract_version", "primary_path", "active_paths", "subordinate_paths",
                "path_conflict", "veto_required", "veto_reasons",
            ),
        )
        direct = compact.get("direct_marketing") if isinstance(compact.get("direct_marketing"), dict) else {}
        compact["direct_marketing"] = _mapping_subset(
            direct,
            ("state", "action", "owner", "commands_allowed", "shadow", "blocked_reasons", "reason"),
        ) or None
        if isinstance(compact.get("trace"), list):
            compact["trace"] = compact["trace"][-6:]
    elif service == "wallbox_manager":
        allowed = (
            "id", "state", "state_reason", "physical_reason", "driver", "plug", "connected",
            "charging", "amp", "set_amp", "cap_amp", "power_w", "phase_power_sum_w",
            "phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w", "phases_in_use",
            "physical_phases", "target_phases", "manager_stop_pending", "rscp_error_active",
            "rscp_last_error", "driver_command", "decision_payload", "last_executed_command",
        )
        compact["wallboxes"] = [
            _mapping_subset(item, allowed)
            for item in (compact.get("wallboxes") or [])
            if isinstance(item, dict)
        ]
    elif service == "energy_manager":
        decision = compact.get("decision") if isinstance(compact.get("decision"), dict) else {}
        if isinstance(decision.get("actions"), list):
            decision["actions"] = decision["actions"][-4:]
        inputs = compact.get("inputs") if isinstance(compact.get("inputs"), dict) else {}
        inputs.pop("consumer_allocations", None)
        inputs.pop("heatpump_pause_request", None)

    compact["_history"] = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "record_type": record_type,
        "context_id": context_id,
    }
    if isinstance(coalesced_summary, dict) and coalesced_summary:
        compact["_history"]["coalesced"] = copy.deepcopy(coalesced_summary)
    return compact


def _history_context(record: Dict[str, Any]) -> Dict[str, Any]:
    service = str(record.get("service") or "unknown")
    context: Dict[str, Any] = {"service": service}
    if service == "storage_manager":
        context["contracts"] = {
            "path": _nested_value(record, "path.contract_version"),
            "budget": _nested_value(record, "storage_budget.contract_version"),
            "owner": _nested_value(record, "r5.storage_owner_contract_version"),
        }
    elif service == "wallbox_manager":
        context["wallboxes"] = [
            {"id": item.get("id"), "driver": item.get("driver")}
            for item in (record.get("wallboxes") or [])
            if isinstance(item, dict)
        ]
    elif service == "energy_manager":
        context["heatpump"] = _mapping_subset(record.get("heatpump"), ("configured", "type"))
    return context


def _history_context_id(record: Dict[str, Any]) -> str:
    context = _history_context(record)
    payload = json.dumps(context, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _history_context_record(record: Dict[str, Any], context_id: str) -> Dict[str, Any]:
    return {
        "ts": record.get("ts"),
        "time": record.get("time"),
        "service": record.get("service"),
        "context": _history_context(record),
        "_history": {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "record_type": "context",
            "context_id": context_id,
        },
    }


def _critical_history_record(record: Dict[str, Any], state: Dict[str, Any]) -> bool:
    decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    current_state = str(decision.get("state") or "")
    current_mode = str(decision.get("mode_name", decision.get("mode", "")) or "")
    first = "last_record_state" not in state
    state_changed = current_state != str(state.get("last_record_state") or "")
    mode_changed = current_mode != str(state.get("last_record_mode") or "")
    state["last_record_state"] = current_state
    state["last_record_mode"] = current_mode
    reason = str(decision.get("reason") or "").lower()
    protected = bool(decision.get("protected"))
    made_changes = bool(decision.get("made_changes"))
    actions = decision.get("actions") if isinstance(decision.get("actions"), list) else []
    action_signature = json.dumps(actions, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    actions_seen = "last_record_actions" in state
    actions_changed = actions_seen and action_signature != str(state.get("last_record_actions") or "")
    state["last_record_actions"] = action_signature
    critical_events = (
        decision.get("critical_events")
        if isinstance(decision.get("critical_events"), list)
        else []
    )
    critical_event_signature = json.dumps(
        critical_events,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    )
    critical_events_seen = "last_record_critical_events" in state
    critical_events_changed = bool(
        critical_events_seen
        and critical_event_signature
        != str(state.get("last_record_critical_events") or "")
    )
    state["last_record_critical_events"] = critical_event_signature
    failure = any(token in reason for token in ("fehler", "timeout", "stale", "schutz", "veto"))
    return bool(
        first
        or state_changed
        or mode_changed
        or protected
        or made_changes
        or actions_changed
        or critical_events_changed
        or failure
    )


_COALESCED_STATE_KEYS = (
    "coalesced_count",
    "coalesced_transition_count",
    "coalesced_first_ts",
    "coalesced_last_ts",
    "coalesced_min",
    "coalesced_max",
    "coalesced_end_state",
)


def _reset_coalesced_history(state: Dict[str, Any]) -> None:
    for key in _COALESCED_STATE_KEYS:
        state.pop(key, None)


_LAST_OBSERVED_STATE_KEYS = (
    "last_observed_compact_record",
    "last_observed_context_id",
    "last_observed_history_path",
)


_PENDING_HISTORY_TRANSACTION_KEYS = (
    "history_buffer",
    "history_buffer_bytes",
    "history_buffer_started_monotonic",
    "history_buffer_path",
    "context_write_required",
    "history_pending_evidence_limit",
    "last_observed_signature",
    "last_observed_critical_signature",
    *_COALESCED_STATE_KEYS,
    *_LAST_OBSERVED_STATE_KEYS,
)


def _snapshot_pending_history_transaction(state: Dict[str, Any]) -> Dict[str, Any]:
    return {
        key: copy.deepcopy(state[key])
        for key in _PENDING_HISTORY_TRANSACTION_KEYS
        if key in state
    }


def _restore_pending_history_transaction(
    state: Dict[str, Any],
    snapshot: Dict[str, Any],
) -> None:
    for key in _PENDING_HISTORY_TRANSACTION_KEYS:
        state.pop(key, None)
    state.update(copy.deepcopy(snapshot))


def _clear_last_observed_history(state: Dict[str, Any]) -> None:
    for key in _LAST_OBSERVED_STATE_KEYS:
        state.pop(key, None)


def _discard_history_buffer(state: Dict[str, Any]) -> None:
    state["history_buffer"] = []
    state["history_buffer_bytes"] = 0
    state["history_buffer_started_monotonic"] = 0.0


def _bounded_last_observed_record(record: Dict[str, Any], context_id: str) -> Dict[str, Any]:
    compact = compact_history_record(record, record_type="sample", context_id=context_id)
    compact.pop("_history_runtime", None)
    encoded = json.dumps(compact, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(encoded) <= HISTORY_LAST_OBSERVED_MAX_BYTES:
        return compact

    decision = compact.get("decision") if isinstance(compact.get("decision"), dict) else {}
    return {
        "ts": compact.get("ts"),
        "time": compact.get("time"),
        "service": compact.get("service"),
        "decision": {
            "state": decision.get("state"),
            "mode": decision.get("mode"),
            "mode_name": decision.get("mode_name"),
            "protected": bool(decision.get("protected")),
            "reason": str(decision.get("reason") or "")[:512],
        },
        "_history": {
            "schema_version": HISTORY_SCHEMA_VERSION,
            "record_type": "sample",
            "context_id": context_id,
            "last_observed_truncated": True,
        },
    }


def _remember_last_observed_history(
    state: Dict[str, Any],
    record: Dict[str, Any],
    *,
    context_id: str,
    path: str,
) -> None:
    state["last_observed_compact_record"] = _bounded_last_observed_record(record, context_id)
    state["last_observed_context_id"] = context_id
    state["last_observed_history_path"] = path


def _observe_coalesced_history(
    state: Dict[str, Any],
    record: Dict[str, Any],
    *,
    summary_paths: Iterable[str],
    state_path: str,
    transition: bool,
) -> None:
    ts = _safe_int(record.get("ts"), int(time.time()))
    count = int(state.get("coalesced_count") or 0)
    if count <= 0:
        state["coalesced_first_ts"] = ts
    state["coalesced_count"] = count + 1
    state["coalesced_last_ts"] = ts
    if transition:
        state["coalesced_transition_count"] = int(state.get("coalesced_transition_count") or 0) + 1

    end_state = _nested_value(record, state_path)
    if end_state is not None:
        state["coalesced_end_state"] = copy.deepcopy(end_state)

    minimums = state.setdefault("coalesced_min", {})
    maximums = state.setdefault("coalesced_max", {})
    if not isinstance(minimums, dict):
        minimums = {}
        state["coalesced_min"] = minimums
    if not isinstance(maximums, dict):
        maximums = {}
        state["coalesced_max"] = maximums
    for path in summary_paths:
        value = _safe_float(_nested_value(record, path), None)
        if value is None:
            continue
        if path not in minimums or value < float(minimums[path]):
            minimums[path] = value
        if path not in maximums or value > float(maximums[path]):
            maximums[path] = value


def _coalesced_history_summary(state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    count = int(state.get("coalesced_count") or 0)
    if count <= 0:
        return None
    summary: Dict[str, Any] = {
        "schema_version": "history_coalesced_summary_v1",
        "count": count,
        "transition_count": int(state.get("coalesced_transition_count") or 0),
        "first_ts": _safe_int(state.get("coalesced_first_ts"), 0),
        "last_ts": _safe_int(state.get("coalesced_last_ts"), 0),
        "min": copy.deepcopy(state.get("coalesced_min") or {}),
        "max": copy.deepcopy(state.get("coalesced_max") or {}),
        "end_state": copy.deepcopy(state.get("coalesced_end_state")),
    }
    return summary


def _history_rollover_evidence_limit(
    state: Dict[str, Any],
    *,
    source_context_id: str,
    target_day: str,
) -> Dict[str, Any]:
    summary = _coalesced_history_summary(state) or {}
    last_record = (
        state.get("last_observed_compact_record")
        if isinstance(state.get("last_observed_compact_record"), dict)
        else {}
    )
    source_ts = _safe_int(summary.get("last_ts"), _safe_int(last_record.get("ts"), 0))
    source_day = (
        datetime.datetime.fromtimestamp(source_ts).strftime("%Y%m%d")
        if source_ts > 0
        else "unknown"
    )
    return {
        "schema_version": "history_evidence_limit_v1",
        "status": "EVIDENCE_LIMIT",
        "scope": "previous_day_summary",
        "reason": "unverified_append_rollback",
        "source_day": source_day,
        "target_day": target_day,
        "source_context_id": source_context_id or None,
    }


def _history_append_path_is_blocked(state: Dict[str, Any], path: str) -> bool:
    bound_path = os.path.abspath(path)
    blocked_paths = state.get("history_append_blocked_paths")
    if isinstance(blocked_paths, dict):
        return bound_path in blocked_paths
    return bool(state.get("history_append_blocked"))


def _history_append_rollback_verified(state: Dict[str, Any], path: str) -> bool:
    failure = (
        state.get("history_append_last_failure")
        if isinstance(state.get("history_append_last_failure"), dict)
        else {}
    )
    return bool(
        failure.get("rollback_verified")
        and str(failure.get("path") or "") == os.path.abspath(path)
    )


def _block_history_append_path(
    state: Dict[str, Any],
    path: str,
    *,
    reason: str,
    preimage_size: Optional[int],
) -> None:
    bound_path = os.path.abspath(path)
    blocked_paths = state.get("history_append_blocked_paths")
    if not isinstance(blocked_paths, dict):
        blocked_paths = {}
        state["history_append_blocked_paths"] = blocked_paths
    blocked_paths[bound_path] = {
        "reason": str(reason or "append_unverifiable")[:512],
        "preimage_size": preimage_size,
        "locked_at": int(time.time()),
    }
    state["history_append_blocked"] = True
    state["history_append_blocked_path"] = bound_path
    state["history_append_blocked_reason"] = str(reason or "append_unverifiable")[:512]


def _history_path_matches_fstat(path: str, descriptor_stat: os.stat_result) -> bool:
    try:
        path_stat = os.stat(path, follow_symlinks=False)
    except Exception:
        return False
    return bool(
        stat.S_ISREG(descriptor_stat.st_mode)
        and stat.S_ISREG(path_stat.st_mode)
        and descriptor_stat.st_dev == path_stat.st_dev
        and descriptor_stat.st_ino == path_stat.st_ino
    )


def _append_gzip_member_preimage_bound(
    state: Dict[str, Any],
    path: str,
    payload: str,
    *,
    max_bytes: Optional[int] = None,
    accounted_append_bytes: Optional[int] = None,
    require_nonempty_preimage: bool = False,
    logger: Any = None,
) -> bool:
    """Hängt einen vollständig erzeugten Gzip-Member mit beweisbarem Rollback an."""

    bound_path = os.path.abspath(path)
    if _history_append_path_is_blocked(state, bound_path):
        state["history_append_last_failure"] = {
            "path": bound_path,
            "kind": "path_blocked",
            "rollback_verified": False,
        }
        return False

    try:
        member = gzip.compress(payload.encode("utf-8"), compresslevel=6)
    except Exception as exc:
        state["history_append_last_failure"] = {
            "path": bound_path,
            "kind": "compression_failed",
            "reason": str(exc)[:512],
            "rollback_verified": True,
        }
        if logger is not None:
            try:
                logger.warning("Decision-History-Gzip-Member konnte nicht erzeugt werden: %s", exc)
            except Exception:
                pass
        return False

    descriptor: Optional[int] = None
    preimage_size: Optional[int] = None
    binding_proven = False
    failure_kind = "binding_failed"
    try:
        os.makedirs(os.path.dirname(bound_path), exist_ok=True)
        flags = os.O_RDWR
        flags |= getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(bound_path, flags)
        except FileNotFoundError:
            try:
                descriptor = os.open(bound_path, flags | os.O_CREAT | os.O_EXCL, 0o664)
            except FileExistsError:
                descriptor = os.open(bound_path, flags)

        descriptor_stat = os.fstat(descriptor)
        if not _history_path_matches_fstat(bound_path, descriptor_stat):
            raise OSError("Pfad und geöffneter Archivdeskriptor sind nicht identisch")
        preimage_size = int(descriptor_stat.st_size)
        if os.lseek(descriptor, 0, os.SEEK_END) != preimage_size:
            raise OSError("Archivgröße änderte sich während der Preimage-Bindung")
        binding_proven = True

        if require_nonempty_preimage and preimage_size <= 0:
            failure_kind = "missing_preimage"
            raise OSError("Archivkontext fehlt vor dem Summary-Append")
        accounted_bytes = int(
            len(member) if accounted_append_bytes is None else accounted_append_bytes
        )
        if max_bytes is not None and preimage_size + accounted_bytes > int(max_bytes):
            failure_kind = "size_limit"
            raise OSError("Archivgrößenlimit erreicht")

        failure_kind = "append_failed"
        written = 0
        member_view = memoryview(member)
        while written < len(member):
            count = os.write(descriptor, member_view[written:])
            if count <= 0:
                raise OSError("Gzip-Append lieferte keinen Fortschritt")
            written += int(count)

        failure_kind = "verification_failed"
        appended_stat = os.fstat(descriptor)
        if int(appended_stat.st_size) != preimage_size + len(member):
            raise OSError("Archivgröße nach Gzip-Append ist nicht exakt")
        if not _history_path_matches_fstat(bound_path, appended_stat):
            raise OSError("Archivpfad verlor nach Gzip-Append seine Deskriptorbindung")
        try:
            os.fchmod(descriptor, 0o664)
        except Exception:
            pass
        os.close(descriptor)
        descriptor = None
        state.pop("history_append_last_failure", None)
        return True
    except Exception as exc:
        rollback_verified = False
        rollback_error = ""
        if descriptor is not None and binding_proven and preimage_size is not None:
            try:
                os.ftruncate(descriptor, preimage_size)
                rolled_back_stat = os.fstat(descriptor)
                rollback_verified = bool(
                    int(rolled_back_stat.st_size) == preimage_size
                    and _history_path_matches_fstat(bound_path, rolled_back_stat)
                )
            except Exception as rollback_exc:
                rollback_error = str(rollback_exc)[:512]
                rollback_verified = False
        if descriptor is not None:
            try:
                os.close(descriptor)
            except Exception as close_exc:
                rollback_error = str(close_exc)[:512]
                rollback_verified = False
        if not rollback_verified:
            _block_history_append_path(
                state,
                bound_path,
                reason=rollback_error or str(exc),
                preimage_size=preimage_size,
            )
        state["history_append_last_failure"] = {
            "path": bound_path,
            "kind": failure_kind,
            "reason": str(exc)[:512],
            "preimage_size": preimage_size,
            "rollback_verified": rollback_verified,
        }
        if logger is not None:
            try:
                logger.warning(
                    "Decision-History-Append fehlgeschlagen (Rollback=%s): %s",
                    "bestätigt" if rollback_verified else "nicht beweisbar; Pfad gesperrt",
                    exc,
                )
            except Exception:
                pass
        return False


def flush_history_buffer(state: Dict[str, Any], logger: Any = None) -> bool:
    lines = state.get("history_buffer") if isinstance(state.get("history_buffer"), list) else []
    path = str(state.get("history_buffer_path") or "")
    if not lines or not path:
        return False
    if _append_gzip_member_preimage_bound(
        state,
        path,
        "".join(lines),
        logger=logger,
    ):
        state["history_buffer"] = []
        state["history_buffer_bytes"] = 0
        state["history_buffer_started_monotonic"] = 0.0
        return True
    return False


def _materialize_pending_history_summary(
    state: Dict[str, Any],
    *,
    max_bytes: int,
    logger: Any = None,
) -> bool:
    """Schreibt eine offene Zusammenfassung noch in ihren gebundenen Kontext/Tag."""

    summary = _coalesced_history_summary(state)
    old_record = state.get("last_observed_compact_record")
    old_context_id = str(state.get("last_observed_context_id") or "")
    old_path = str(state.get("last_observed_history_path") or "")
    buffer_path = str(state.get("history_buffer_path") or "")
    buffer_lines = (
        state.get("history_buffer") if isinstance(state.get("history_buffer"), list) else []
    )
    buffer_bytes = int(state.get("history_buffer_bytes") or 0)

    if buffer_lines or buffer_bytes:
        expected_path = old_path or buffer_path
        if not expected_path or buffer_path != expected_path:
            blocked_path = expected_path or buffer_path
            if blocked_path:
                _block_history_append_path(
                    state,
                    blocked_path,
                    reason="Puffer und Summary besitzen keine eindeutige gemeinsame Pfadbindung",
                    preimage_size=None,
                )
            state["history_append_last_failure"] = {
                "path": os.path.abspath(blocked_path) if blocked_path else "",
                "kind": "buffer_binding_mismatch",
                "rollback_verified": False,
            }
            if logger is not None:
                try:
                    logger.warning(
                        "Decision-History: alter Puffer ohne eindeutige Pfadbindung verworfen"
                    )
                except Exception:
                    pass
            return False
        if not flush_history_buffer(state, logger):
            return False

    if not summary:
        _reset_coalesced_history(state)
        _clear_last_observed_history(state)
        return True

    if not isinstance(old_record, dict) or not old_context_id or not old_path:
        if old_path:
            _block_history_append_path(
                state,
                old_path,
                reason="Offene Zusammenfassung besitzt keine vollständige Kontextbindung",
                preimage_size=None,
            )
        state["history_append_last_failure"] = {
            "path": os.path.abspath(old_path) if old_path else "",
            "kind": "summary_binding_missing",
            "rollback_verified": False,
        }
        if logger is not None:
            try:
                logger.warning(
                    "Decision-History: offene Zusammenfassung ohne vollständige Kontextbindung verworfen"
                )
            except Exception:
                pass
        return False

    history_record = copy.deepcopy(old_record)
    history_record.pop("_history_runtime", None)
    history_record["_history"] = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "record_type": "summary",
        "context_id": old_context_id,
        "coalesced": summary,
    }
    line = json.dumps(history_record, ensure_ascii=False, separators=(",", ":")) + "\n"
    added_bytes = len(line.encode("utf-8"))
    if not _append_gzip_member_preimage_bound(
        state,
        old_path,
        line,
        max_bytes=max_bytes,
        accounted_append_bytes=added_bytes,
        require_nonempty_preimage=True,
        logger=logger,
    ):
        return False

    _reset_coalesced_history(state)
    _clear_last_observed_history(state)
    return True


def _flush_all_history_buffers() -> None:
    for state in list(_BUFFER_STATES.values()):
        flush_history_buffer(state)


atexit.register(_flush_all_history_buffers)


def _compress_legacy_jsonl(log_dir: str, prefix: str, day: str, logger: Any = None) -> None:
    try:
        for name in os.listdir(log_dir):
            if not (name.startswith(prefix) and name.endswith(".jsonl")):
                continue
            path = os.path.join(log_dir, name)
            if not os.path.isfile(path):
                continue
            gz_path = path + ".gz"
            with open(path, "rt", encoding="utf-8", errors="replace") as src, gzip.open(
                gz_path, "at", encoding="utf-8", compresslevel=5
            ) as dst:
                for line in src:
                    if line.strip():
                        dst.write(line if line.endswith("\n") else line + "\n")
            try:
                os.chmod(gz_path, 0o664)
            except Exception:
                pass
            os.remove(path)
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug("Decision-History Kompression uebersprungen: %s", exc)
            except Exception:
                pass


def cleanup_decision_history(log_dir: str, prefix: str, retention_days: int, logger: Any = None) -> None:
    cutoff = time.time() - max(1, int(retention_days or 7)) * 86400
    try:
        for name in os.listdir(log_dir):
            if not name.startswith(prefix):
                continue
            if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                continue
            path = os.path.join(log_dir, name)
            if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                os.remove(path)
    except Exception as exc:
        if logger is not None:
            try:
                logger.debug("Decision-History Cleanup uebersprungen: %s", exc)
            except Exception:
                pass


def write_history_record(
    record: Dict[str, Any],
    *,
    config: Dict[str, Any],
    log_dir: str,
    latest_path: str,
    prefix: str,
    enable_key: str,
    max_bytes_key: str,
    retention_key: str,
    interval_key: str,
    state: Dict[str, Any],
    signature_paths: Iterable[str],
    critical_signature_paths: Optional[Iterable[str]] = None,
    summary_paths: Iterable[str] = (),
    summary_state_path: str = "decision.state",
    default_interval_s: int = 60,
    minimum_interval_s: int = 5,
    default_max_bytes: int = 8 * 1024 * 1024,
    default_retention_days: int = 2,
    logger: Any = None,
    config_get: Optional[Callable[[Dict[str, Any], str, Any], Any]] = None,
) -> bool:
    signature_paths = tuple(signature_paths)
    summary_paths = tuple(summary_paths)
    explicit_critical_contract = critical_signature_paths is not None
    critical_signature_paths = tuple(critical_signature_paths or ())
    getter = config_get or (lambda cfg, key, default=None: (cfg or {}).get(key, default))
    enabled = _cfg_bool(getter(config, enable_key, 1), True)
    if not enabled:
        return False

    os.makedirs(log_dir, exist_ok=True)
    _BUFFER_STATES[id(state)] = state
    add_power_decision_history_diagnostics(record, config=config, log_dir=log_dir, getter=getter, logger=logger)
    ts = _safe_int(record.get("ts"), int(time.time()))
    day = datetime.datetime.fromtimestamp(ts).strftime("%Y%m%d")
    today = datetime.datetime.now().strftime("%Y%m%d")
    path = os.path.join(log_dir, f"{prefix}{day}.jsonl.gz")

    interval_s = max(5, _safe_int(getter(config, interval_key, default_interval_s), default_interval_s))
    max_bytes = max(256 * 1024, _safe_int(getter(config, max_bytes_key, default_max_bytes), default_max_bytes))
    retention_days = max(1, _safe_int(getter(config, retention_key, default_retention_days), default_retention_days))
    legacy_profile = interval_s <= 30 and max_bytes >= 16 * 1024 * 1024 and retention_days >= 14
    minimum_interval_s = max(5, _safe_int(minimum_interval_s, 5))
    effective_interval_s = max(interval_s, minimum_interval_s)
    if legacy_profile:
        effective_interval_s = max(effective_interval_s, HISTORY_NORMAL_HEARTBEAT_S)
    if legacy_profile and not state.get("legacy_profile_notice_logged"):
        state["legacy_profile_notice_logged"] = True
        if logger is not None:
            try:
                logger.info(
                    "Decision-History: altes 30s/16MiB-Profil erkannt; unveränderte Samples werden auf %ds gedämpft",
                    effective_interval_s,
                )
            except Exception:
                pass
    context_id = _history_context_id(record)
    previous_context_id = str(
        state.get("last_observed_context_id") or state.get("last_context_id") or ""
    )
    previous_path = str(
        state.get("last_observed_history_path") or state.get("history_buffer_path") or ""
    )
    context_boundary = bool(previous_context_id and previous_context_id != context_id)
    path_boundary = bool(previous_path and previous_path != path)
    boundary_transaction_snapshot = (
        _snapshot_pending_history_transaction(state)
        if explicit_critical_contract and (context_boundary or path_boundary)
        else {}
    )
    blocked_old_day_rollover = bool(
        explicit_critical_contract
        and path_boundary
        and previous_path
        and _history_append_path_is_blocked(state, previous_path)
        and not _history_append_path_is_blocked(state, path)
    )
    if blocked_old_day_rollover:
        state["history_pending_evidence_limit"] = _history_rollover_evidence_limit(
            state,
            source_context_id=previous_context_id,
            target_day=day,
        )
        _discard_history_buffer(state)
        _reset_coalesced_history(state)
        _clear_last_observed_history(state)
        state["context_write_required"] = True
    elif explicit_critical_contract and (context_boundary or path_boundary):
        if not _materialize_pending_history_summary(
            state,
            max_bytes=max_bytes,
            logger=logger,
        ):
            if previous_path and _history_append_rollback_verified(state, previous_path):
                _restore_pending_history_transaction(
                    state,
                    boundary_transaction_snapshot,
                )
            return False
        _discard_history_buffer(state)
        _reset_coalesced_history(state)
        _clear_last_observed_history(state)
        state["context_write_required"] = True
    elif not explicit_critical_contract and state.get("history_buffer_path") not in (None, "", path):
        if not flush_history_buffer(state, logger):
            _discard_history_buffer(state)
        state["context_write_required"] = True
    state["history_buffer_path"] = path
    if explicit_critical_contract and (
        not os.path.exists(path) or os.path.getsize(path) <= 0
    ):
        state["context_write_required"] = True
    if explicit_critical_contract and _history_append_path_is_blocked(state, path):
        return False
    write_transaction_snapshot: Dict[str, Any] = {}

    record["_history_runtime"] = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "requested_interval_s": interval_s,
        "effective_interval_s": effective_interval_s,
        "minimum_interval_s": minimum_interval_s,
        "batch_max_age_s": HISTORY_BATCH_MAX_AGE_S,
        "batch_max_bytes": HISTORY_BATCH_MAX_BYTES,
        "legacy_profile_damped": legacy_profile,
        "persistence_policy": (
            "critical_edges_300s_summary" if explicit_critical_contract else "legacy_transition"
        ),
    }
    pending_evidence_limit = (
        state.get("history_pending_evidence_limit")
        if isinstance(state.get("history_pending_evidence_limit"), dict)
        else None
    )
    if pending_evidence_limit:
        record["_history_runtime"]["evidence_limit"] = copy.deepcopy(
            pending_evidence_limit
        )
    _atomic_write_json(latest_path, record)

    now_monotonic = time.monotonic()
    buffer_started = float(state.get("history_buffer_started_monotonic") or 0.0)
    buffer_bytes = int(state.get("history_buffer_bytes") or 0)
    if buffer_bytes and (
        buffer_bytes >= HISTORY_BATCH_MAX_BYTES
        or (buffer_started > 0.0 and now_monotonic - buffer_started >= HISTORY_BATCH_MAX_AGE_S)
    ):
        periodic_flush_snapshot = (
            _snapshot_pending_history_transaction(state)
            if explicit_critical_contract
            else {}
        )
        if not flush_history_buffer(state, logger) and explicit_critical_contract:
            if _history_append_rollback_verified(state, path):
                _restore_pending_history_transaction(
                    state,
                    periodic_flush_snapshot,
                )
            return False

    cleanup_day = datetime.datetime.now().date().isoformat()
    if state.get("cleanup_day") != cleanup_day:
        state["cleanup_day"] = cleanup_day
        _compress_legacy_jsonl(log_dir, prefix, today, logger)
        cleanup_decision_history(log_dir, prefix, retention_days, logger)

    signature = decision_signature(record, signature_paths)
    last_ts = float(state.get("last_write_ts") or 0.0)
    last_signature = state.get("last_signature")
    signature_changed = signature != last_signature
    context_changed = bool(
        state.get("context_write_required")
        or state.get("last_context_id") not in (None, "", context_id)
    )
    first_record = "last_write_ts" not in state
    heartbeat_due = bool(first_record or ts - last_ts >= effective_interval_s)
    coalesced_summary: Optional[Dict[str, Any]] = None

    if explicit_critical_contract:
        critical_signature = decision_signature(record, critical_signature_paths)
        observed_signature = state.get("last_observed_signature")
        observed_critical_signature = state.get("last_observed_critical_signature")
        normal_transition = bool(
            observed_signature is not None and signature != observed_signature
        )
        critical_transition = bool(
            observed_critical_signature is not None
            and critical_signature != observed_critical_signature
        )
        should_write = bool(first_record or context_changed or critical_transition or heartbeat_due)
        if should_write:
            write_transaction_snapshot = _snapshot_pending_history_transaction(state)

        # Normale Reglerbewegungen bleiben im RAM und werden höchstens alle 300 s
        # als verdichtete Zusammenfassung persistent. Eine echte Schutz-/Hardwarekante
        # trägt eine bereits aufgelaufene Zusammenfassung im selben Datensatz mit.
        if not first_record and not critical_transition and not context_changed:
            _observe_coalesced_history(
                state,
                record,
                summary_paths=summary_paths,
                state_path=summary_state_path,
                transition=normal_transition,
            )
        if not should_write:
            state["last_observed_signature"] = signature
            state["last_observed_critical_signature"] = critical_signature
            _remember_last_observed_history(
                state,
                record,
                context_id=context_id,
                path=path,
            )
            return False
        coalesced_summary = _coalesced_history_summary(state)
        critical = bool(first_record or context_changed or critical_transition)
        if critical:
            record_type = "transition"
        elif coalesced_summary:
            record_type = "summary"
        else:
            record_type = "sample"
    else:
        should_write = bool(signature_changed or heartbeat_due or context_changed)
        if not should_write:
            return False
        critical = False
        record_type = "transition" if signature_changed or context_changed else "sample"

    current_size = os.path.getsize(path) if os.path.exists(path) else 0
    if current_size + int(state.get("history_buffer_bytes") or 0) >= max_bytes:
        if explicit_critical_contract:
            _discard_history_buffer(state)
            _reset_coalesced_history(state)
            _clear_last_observed_history(state)
        return False

    history_record = compact_history_record(
        record,
        record_type=record_type,
        context_id=context_id,
        coalesced_summary=coalesced_summary,
    )
    if pending_evidence_limit:
        history_record["_history"]["evidence_limit"] = copy.deepcopy(
            pending_evidence_limit
        )
    lines_to_append = []
    context_write_required = bool(state.get("context_write_required"))
    if context_write_required or state.get("last_context_id") != context_id:
        lines_to_append.append(
            json.dumps(
                _history_context_record(record, context_id),
                ensure_ascii=False,
                separators=(",", ":"),
            ) + "\n"
        )
    lines_to_append.append(json.dumps(history_record, ensure_ascii=False, separators=(",", ":")) + "\n")
    added_bytes = sum(len(line.encode("utf-8")) for line in lines_to_append)
    if current_size + int(state.get("history_buffer_bytes") or 0) + added_bytes > max_bytes:
        if explicit_critical_contract:
            _discard_history_buffer(state)
            _reset_coalesced_history(state)
            _clear_last_observed_history(state)
        return False
    buffer = state.setdefault("history_buffer", [])
    if not isinstance(buffer, list):
        buffer = []
        state["history_buffer"] = buffer
    buffer.extend(lines_to_append)
    state["history_buffer_bytes"] = int(state.get("history_buffer_bytes") or 0) + added_bytes
    if not state.get("history_buffer_started_monotonic"):
        state["history_buffer_started_monotonic"] = now_monotonic
    if not explicit_critical_contract:
        critical = _critical_history_record(record, state)
    must_flush = bool(
        critical
        or context_write_required
        or explicit_critical_contract
        or int(state.get("history_buffer_bytes") or 0) >= HISTORY_BATCH_MAX_BYTES
    )
    if must_flush and not flush_history_buffer(state, logger) and explicit_critical_contract:
        if _history_append_rollback_verified(state, path):
            _restore_pending_history_transaction(
                state,
                write_transaction_snapshot,
            )
        return False
    state["last_write_ts"] = ts
    state["last_signature"] = signature
    state["last_context_id"] = context_id
    state.pop("context_write_required", None)
    state.pop("history_pending_evidence_limit", None)
    if explicit_critical_contract:
        state["last_observed_signature"] = signature
        state["last_observed_critical_signature"] = critical_signature
        state["last_critical_signature"] = critical_signature
        _reset_coalesced_history(state)
        _clear_last_observed_history(state)
        _remember_last_observed_history(
            state,
            record,
            context_id=context_id,
            path=path,
        )
    return True
