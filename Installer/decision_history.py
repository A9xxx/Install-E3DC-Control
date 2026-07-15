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


def compact_history_record(record: Dict[str, Any], *, record_type: str, context_id: str) -> Dict[str, Any]:
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
    actions_changed = bool(actions) and action_signature != str(state.get("last_record_actions") or "")
    state["last_record_actions"] = action_signature
    failure = any(token in reason for token in ("fehler", "timeout", "stale", "schutz", "veto"))
    return bool(first or state_changed or mode_changed or protected or made_changes or actions_changed or failure)


def flush_history_buffer(state: Dict[str, Any], logger: Any = None) -> bool:
    lines = state.get("history_buffer") if isinstance(state.get("history_buffer"), list) else []
    path = str(state.get("history_buffer_path") or "")
    if not lines or not path:
        return False
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with gzip.open(path, "at", encoding="utf-8", compresslevel=6) as handle:
            handle.write("".join(lines))
        try:
            os.chmod(path, 0o664)
        except Exception:
            pass
        state["history_buffer"] = []
        state["history_buffer_bytes"] = 0
        state["history_buffer_started_monotonic"] = 0.0
        return True
    except Exception as exc:
        if logger is not None:
            try:
                logger.warning("Decision-History-Puffer konnte nicht geschrieben werden: %s", exc)
            except Exception:
                pass
        return False


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
    default_interval_s: int = 60,
    default_max_bytes: int = 8 * 1024 * 1024,
    default_retention_days: int = 2,
    logger: Any = None,
    config_get: Optional[Callable[[Dict[str, Any], str, Any], Any]] = None,
) -> bool:
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
    if state.get("history_buffer_path") not in (None, "", path):
        flush_history_buffer(state, logger)
    state["history_buffer_path"] = path

    interval_s = max(5, _safe_int(getter(config, interval_key, default_interval_s), default_interval_s))
    max_bytes = max(256 * 1024, _safe_int(getter(config, max_bytes_key, default_max_bytes), default_max_bytes))
    retention_days = max(1, _safe_int(getter(config, retention_key, default_retention_days), default_retention_days))
    legacy_profile = interval_s <= 30 and max_bytes >= 16 * 1024 * 1024 and retention_days >= 14
    effective_interval_s = max(interval_s, HISTORY_NORMAL_HEARTBEAT_S) if legacy_profile else interval_s
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
    record["_history_runtime"] = {
        "schema_version": HISTORY_SCHEMA_VERSION,
        "requested_interval_s": interval_s,
        "effective_interval_s": effective_interval_s,
        "batch_max_age_s": HISTORY_BATCH_MAX_AGE_S,
        "batch_max_bytes": HISTORY_BATCH_MAX_BYTES,
        "legacy_profile_damped": legacy_profile,
    }
    _atomic_write_json(latest_path, record)

    now_monotonic = time.monotonic()
    buffer_started = float(state.get("history_buffer_started_monotonic") or 0.0)
    buffer_bytes = int(state.get("history_buffer_bytes") or 0)
    if buffer_bytes and (
        buffer_bytes >= HISTORY_BATCH_MAX_BYTES
        or (buffer_started > 0.0 and now_monotonic - buffer_started >= HISTORY_BATCH_MAX_AGE_S)
    ):
        flush_history_buffer(state, logger)

    cleanup_day = datetime.datetime.now().date().isoformat()
    if state.get("cleanup_day") != cleanup_day:
        state["cleanup_day"] = cleanup_day
        _compress_legacy_jsonl(log_dir, prefix, today, logger)
        cleanup_decision_history(log_dir, prefix, retention_days, logger)

    signature = decision_signature(record, signature_paths)
    last_ts = float(state.get("last_write_ts") or 0.0)
    last_signature = state.get("last_signature")
    signature_changed = signature != last_signature
    should_write = signature_changed or ts - last_ts >= effective_interval_s
    if not should_write:
        return False

    current_size = os.path.getsize(path) if os.path.exists(path) else 0
    if current_size + int(state.get("history_buffer_bytes") or 0) >= max_bytes:
        return False

    context_id = _history_context_id(record)
    record_type = "transition" if signature_changed else "sample"
    history_record = compact_history_record(record, record_type=record_type, context_id=context_id)
    lines_to_append = []
    if state.get("last_context_id") != context_id:
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
        return False
    buffer = state.setdefault("history_buffer", [])
    if not isinstance(buffer, list):
        buffer = []
        state["history_buffer"] = buffer
    buffer.extend(lines_to_append)
    state["history_buffer_bytes"] = int(state.get("history_buffer_bytes") or 0) + added_bytes
    if not state.get("history_buffer_started_monotonic"):
        state["history_buffer_started_monotonic"] = now_monotonic
    critical = _critical_history_record(record, state)
    if critical or int(state.get("history_buffer_bytes") or 0) >= HISTORY_BATCH_MAX_BYTES:
        flush_history_buffer(state, logger)
    state["last_write_ts"] = ts
    state["last_signature"] = signature
    state["last_context_id"] = context_id
    return True
