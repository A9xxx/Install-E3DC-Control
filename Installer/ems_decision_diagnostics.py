#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Canonical EMS decision surface.

This module is diagnostic-only.  It normalizes already-made decisions from the
domain managers and writes a shared latest-state file.  It must not derive new
policy decisions or send hardware commands.
"""

from __future__ import annotations

import contextlib
import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional


RECORD_SCHEMA = "ems_decision_v1"
SURFACE_SCHEMA = "ems_decision_surface_v1"
CONTRACT_VERSION = "ems-decision-contract-v1"


def default_surface_path(ramdisk_dir: str = "/var/www/html/ramdisk") -> str:
    return os.path.join(str(ramdisk_dir or "/var/www/html/ramdisk"), "ems_decision_latest.json")


def _safe_int(value: Any, default: Optional[int] = None) -> Optional[int]:
    try:
        if value is None or value == "":
            return default
        return int(round(float(value)))
    except Exception:
        return default


def _safe_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except Exception:
        return default


def _compact_text(value: Any, limit: int = 240) -> str:
    text = str(value or "").replace("\r", " ").replace("\n", " ").strip()
    return text[:limit]


def _reason_list(*values: Any) -> List[str]:
    result: List[str] = []
    for value in values:
        if value is None or value is False:
            continue
        if isinstance(value, dict):
            for key, item in value.items():
                if item:
                    result.append(_compact_text(key))
            continue
        if isinstance(value, (list, tuple, set)):
            for item in value:
                if item:
                    result.append(_compact_text(item))
            continue
        text = _compact_text(value)
        if text:
            result.append(text)
    deduped: List[str] = []
    seen = set()
    for item in result:
        key = item.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(item)
    return deduped


def _numeric_or_none(value: Any) -> Optional[int]:
    return _safe_int(value, None)


def build_record(
    *,
    actor: str,
    domain: str,
    mode: Any = None,
    decision: str = "observe",
    owner: str = "",
    requested_power_w: Any = None,
    granted_power_w: Any = None,
    min_required_w: Any = None,
    missing_w: Any = None,
    blockers: Optional[Iterable[Any]] = None,
    safety_guards: Optional[Iterable[Any]] = None,
    data_quality: Optional[Dict[str, Any]] = None,
    next_retry_s: Any = None,
    hardware_command: Optional[Dict[str, Any]] = None,
    noop_reason: Any = None,
    user_text: Any = None,
    ts: Any = None,
) -> Dict[str, Any]:
    """Build one canonical record from an already existing domain decision."""

    record_ts = _safe_int(ts, int(time.time()))
    requested = _numeric_or_none(requested_power_w)
    granted = _numeric_or_none(granted_power_w)
    minimum = _numeric_or_none(min_required_w)
    missing = _numeric_or_none(missing_w)
    if missing is None and minimum is not None and granted is not None:
        missing = max(0, minimum - max(0, granted))

    return {
        "schema": RECORD_SCHEMA,
        "contract": CONTRACT_VERSION,
        "ts": record_ts,
        "actor": _compact_text(actor, 80),
        "domain": _compact_text(domain, 40),
        "mode": mode,
        "decision": _compact_text(decision or "observe", 80),
        "owner": _compact_text(owner or domain, 120),
        "requested_power_w": requested,
        "granted_power_w": granted,
        "min_required_w": minimum,
        "missing_w": missing,
        "blockers": _reason_list(blockers or []),
        "safety_guards": _reason_list(safety_guards or []),
        "data_quality": data_quality if isinstance(data_quality, dict) else {},
        "next_retry_s": _numeric_or_none(next_retry_s),
        "hardware_command": hardware_command if isinstance(hardware_command, dict) else None,
        "noop_reason": _compact_text(noop_reason, 160) or None,
        "user_text": _compact_text(user_text, 320),
    }


def build_storage_decision_record(payload: Dict[str, Any]) -> Dict[str, Any]:
    payload = payload if isinstance(payload, dict) else {}
    budget = payload.get("budget") if isinstance(payload.get("budget"), dict) else {}
    auto_limit = payload.get("auto_limit") if isinstance(payload.get("auto_limit"), dict) else {}
    mode_name = str(payload.get("mode_name") or payload.get("mode") or "AUTO").strip()
    val_w = _safe_int(payload.get("val"), 0) or 0
    safe_start = bool(payload.get("safe_start"))
    protected = bool(payload.get("protected"))

    if safe_start:
        decision = "block"
    elif protected:
        decision = "protect"
    elif mode_name.upper() == "AUTO" and val_w == 0:
        decision = "observe"
    else:
        decision = "command"

    blockers = []
    if safe_start:
        blockers.append(payload.get("state") or "safe_start")
    if payload.get("live_sample_valid") is False:
        blockers.append("live_sample_invalid")
    blockers.extend(_reason_list(payload.get("blockers")))

    safety = []
    if protected:
        safety.append("protected_owner")
    if auto_limit.get("enabled"):
        safety.append("auto_limit")
    if payload.get("predump_active"):
        safety.append("predump")
    if payload.get("direct_marketing_active"):
        safety.append("direct_marketing")
    if safe_start:
        safety.append("safe_start")

    hardware_command = None
    if not safe_start:
        hardware_command = {
            "method": "rscp_send",
            "mode": mode_name,
            "mode_value": _safe_int(payload.get("mode"), 0),
            "value_w": val_w,
        }
        if auto_limit:
            hardware_command["auto_limit"] = auto_limit

    record = build_record(
        actor="storage:e3dc",
        domain="storage",
        mode=mode_name,
        decision=decision,
        owner=payload.get("control_owner") or payload.get("owner") or payload.get("state") or "storage_manager",
        requested_power_w=payload.get("storage_req_w", payload.get("storage_request_w", val_w)),
        granted_power_w=val_w,
        min_required_w=payload.get("min_required_w"),
        missing_w=payload.get("missing_w"),
        blockers=blockers,
        safety_guards=safety,
        data_quality={
            "live": "invalid" if payload.get("live_sample_valid") is False else ("missing" if safe_start else "fresh"),
            "budget": "fresh" if budget else "missing",
            "source": "storage_manager_state",
        },
        hardware_command=hardware_command,
        noop_reason="safe_start" if safe_start else None,
        user_text=payload.get("display_reason") or payload.get("reason") or payload.get("state_label") or payload.get("state"),
        ts=payload.get("ts"),
    )
    dispatch_runtime = payload.get("storage_dispatch_runtime")
    if isinstance(dispatch_runtime, dict):
        # Reine Durchleitung der bereits im Storage Manager gebundenen Fläche;
        # die gemeinsame Diagnose erfindet keine zweite Dispatchentscheidung.
        record["storage_dispatch_runtime"] = dispatch_runtime
        record["plan_id"] = dispatch_runtime.get("plan_id")
        record["slot_id"] = dispatch_runtime.get("slot_id")
        record["commands_allowed"] = bool(dispatch_runtime.get("commands_allowed"))
        record["block_reason_code"] = dispatch_runtime.get("block_reason_code")
    return record


def _wallbox_command_from_detail(detail: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    command = detail.get("driver_command")
    if not isinstance(command, dict):
        payload = detail.get("decision_payload") if isinstance(detail.get("decision_payload"), dict) else {}
        command = payload.get("driver_command")
    return command if isinstance(command, dict) else None


def build_wallbox_decision_records(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    decision_meta = snapshot.get("decision") if isinstance(snapshot.get("decision"), dict) else {}
    inputs = snapshot.get("inputs") if isinstance(snapshot.get("inputs"), dict) else {}
    storage_context = snapshot.get("storage_context") if isinstance(snapshot.get("storage_context"), dict) else {}
    details = snapshot.get("wallboxes") if isinstance(snapshot.get("wallboxes"), list) else []
    records: List[Dict[str, Any]] = []
    made_changes = bool(decision_meta.get("made_changes"))
    budget_w = _safe_int(inputs.get("effective_budget_w", inputs.get("allowed_w")), 0) or 0

    for detail in details:
        if not isinstance(detail, dict):
            continue
        cid = _safe_int(detail.get("id"), len(records) + 1) or (len(records) + 1)
        command = _wallbox_command_from_detail(detail)
        guard = detail.get("command_guard") if isinstance(detail.get("command_guard"), dict) else {}
        payload = detail.get("decision_payload") if isinstance(detail.get("decision_payload"), dict) else {}
        decisions = payload.get("decisions") if isinstance(payload.get("decisions"), dict) else {}
        start_stop = decisions.get("start_stop") if isinstance(decisions.get("start_stop"), dict) else {}
        current = decisions.get("current") if isinstance(decisions.get("current"), dict) else {}

        target_amp = _safe_float(detail.get("target_amp", current.get("target_amp")), 0.0) or 0.0
        phases = _safe_int(
            detail.get("phase_effective_phases", detail.get("detected_phases", detail.get("phases_in_use"))),
            1,
        ) or 1
        min_required_w = _safe_int(detail.get("min_power_w"), None)
        if min_required_w is None and bool(detail.get("plug")) and target_amp <= 0:
            min_required_w = 6 * 230 * max(1, phases)

        if guard.get("decision") == "observe_only_noop":
            decision = "noop"
            noop_reason = "observe_only_noop"
        elif decision_meta.get("mode_public") == 0 or detail.get("enabled") is False:
            decision = "observe"
            noop_reason = "off_or_disabled"
        elif detail.get("charge_end_latched"):
            decision = "vehicle_finished"
            noop_reason = "vehicle_finished"
        elif target_amp > 0:
            decision = "allow"
            noop_reason = None
        elif bool(detail.get("plug")):
            decision = "block"
            noop_reason = start_stop.get("reason") or detail.get("state_reason")
        else:
            decision = "observe"
            noop_reason = "unplugged"
        if decision == "observe" and noop_reason in {"off_or_disabled", "unplugged"}:
            min_required_w = None

        blockers = []
        if inputs.get("budget_timeout"):
            blockers.append("budget_timeout")
        elif inputs.get("budget_stale"):
            blockers.append("budget_stale")
        if detail.get("manual_pause"):
            blockers.append("manual_pause")
        if detail.get("charge_end_latched"):
            blockers.append("vehicle_finished")
        if guard.get("decision") == "blocked":
            blockers.append(guard.get("block_reason") or "command_guard")
        if decision in {"block", "vehicle_finished"}:
            blockers.append(detail.get("state_reason") or start_stop.get("reason") or decision_meta.get("reason"))

        requested_power_w = int(round(target_amp * 230 * max(1, phases))) if target_amp > 0 else 0
        granted_power_w = requested_power_w if target_amp > 0 else budget_w
        safety = []
        if guard:
            safety.append("command_guard")
        if detail.get("phase_wait_active") or detail.get("phase_contract"):
            safety.append("phase_guard")
        if storage_context.get("price_plan_storage_protect"):
            safety.append("price_plan_storage_protect")
        if not storage_context.get("wbminsoc_gate_open", True):
            safety.append("wbminsoc")

        command_diag = None
        if command:
            command_diag = dict(command)
            command_diag["executed"] = made_changes

        records.append(
            build_record(
                actor=f"wallbox:{cid}",
                domain="wallbox",
                mode=decision_meta.get("mode_public"),
                decision=decision,
                owner="wallbox_manager",
                requested_power_w=requested_power_w,
                granted_power_w=granted_power_w,
                min_required_w=min_required_w,
                blockers=blockers,
                safety_guards=safety,
                data_quality={
                    "live": "fresh",
                    "budget": "stale" if inputs.get("budget_stale") else ("timeout" if inputs.get("budget_timeout") else "fresh"),
                    "vehicle_soc": "fresh" if detail.get("car_soc_rule_confirmed") else "unknown",
                    "source": "wallbox_decision_snapshot",
                },
                next_retry_s=detail.get("next_retry_s"),
                hardware_command=command_diag,
                noop_reason=noop_reason,
                user_text=detail.get("state_reason") or decision_meta.get("reason") or detail.get("state"),
                ts=snapshot.get("ts"),
            )
        )
    return records


def build_energy_surface_record(record: Dict[str, Any]) -> Dict[str, Any]:
    record = record if isinstance(record, dict) else {}
    decision_meta = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    inputs = record.get("inputs") if isinstance(record.get("inputs"), dict) else {}
    heatpump = record.get("heatpump") if isinstance(record.get("heatpump"), dict) else {}

    if heatpump.get("protect_block") or heatpump.get("targets_reached"):
        decision = "block"
    elif decision_meta.get("pv_pause_active"):
        decision = "pause"
    elif heatpump.get("budget_offered") or decision_meta.get("boost_active") or decision_meta.get("price_boost_active"):
        decision = "allow"
    else:
        decision = "observe"

    blockers = []
    blockers.extend(_reason_list(decision_meta.get("local_autonomy_blocked")))
    if heatpump.get("protect_block"):
        blockers.append("heatpump_protect_block")
    if heatpump.get("targets_reached"):
        blockers.append("targets_reached")

    safety = []
    if heatpump.get("predump_active"):
        safety.append("predump_min_runtime")
    if decision_meta.get("source_recovery_pause_latched"):
        safety.append("source_recovery_pause")
    if decision_meta.get("storage_manager_owns_energy"):
        safety.append("storage_manager_owns_energy")

    actions = decision_meta.get("actions") if isinstance(decision_meta.get("actions"), list) else []
    hardware_command = {"actions": actions[-8:]} if actions else None

    return build_record(
        actor="heat:heatpump",
        domain="heat",
        mode=decision_meta.get("state"),
        decision=decision,
        owner=decision_meta.get("heatpump_boost_owner") or "energy_manager",
        requested_power_w=inputs.get("heatpump_budget_w"),
        granted_power_w=heatpump.get("wp_power_w"),
        blockers=blockers,
        safety_guards=safety,
        data_quality={
            "live": "fresh" if heatpump.get("connected") else "missing",
            "heatpump_power": "fresh" if heatpump.get("power_known") else "unknown",
            "source": "energy_decision_latest",
        },
        next_retry_s=heatpump.get("predump_hold_remaining_s"),
        hardware_command=hardware_command,
        noop_reason=None if decision != "observe" else "no_heat_request",
        user_text=decision_meta.get("reason") or decision_meta.get("state"),
        ts=record.get("ts"),
    )


@contextlib.contextmanager
def _surface_lock(path: str):
    lock_path = f"{path}.lock"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    handle = open(lock_path, "a+", encoding="utf-8")
    try:
        try:
            import fcntl  # type: ignore

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        except Exception:
            pass
        yield
    finally:
        try:
            try:
                import fcntl  # type: ignore

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
            except Exception:
                pass
        finally:
            handle.close()


def _read_surface(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _atomic_write(path: str, payload: Dict[str, Any], mode: int = 0o664) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2, sort_keys=True)
    try:
        os.chmod(tmp, mode)
    except Exception:
        pass
    os.replace(tmp, path)
    try:
        os.chmod(path, mode)
    except Exception:
        pass


def write_decision_surface_records(
    records: Iterable[Dict[str, Any]],
    *,
    path: Optional[str] = None,
    ramdisk_dir: str = "/var/www/html/ramdisk",
    stale_after_s: int = 1800,
) -> bool:
    path = path or default_surface_path(ramdisk_dir)
    clean_records = [
        dict(record)
        for record in records
        if isinstance(record, dict) and record.get("schema") == RECORD_SCHEMA and record.get("actor")
    ]
    if not clean_records:
        return False

    with _surface_lock(path):
        previous = _read_surface(path)
        actors = previous.get("actors") if isinstance(previous.get("actors"), dict) else {}
        merged: Dict[str, Dict[str, Any]] = {
            str(actor): dict(record)
            for actor, record in actors.items()
            if isinstance(record, dict)
        }
        now_ts = max(_safe_int(record.get("ts"), int(time.time())) or int(time.time()) for record in clean_records)
        for record in clean_records:
            merged[str(record["actor"])] = record
        keep: Dict[str, Dict[str, Any]] = {}
        for actor, record in merged.items():
            record_ts = _safe_int(record.get("ts"), 0) or 0
            if record_ts <= 0 or now_ts - record_ts <= max(60, int(stale_after_s)):
                keep[actor] = record
        payload = {
            "schema": SURFACE_SCHEMA,
            "record_schema": RECORD_SCHEMA,
            "contract": CONTRACT_VERSION,
            "ts": now_ts,
            "stale_after_s": max(60, int(stale_after_s)),
            "actors": keep,
        }
        _atomic_write(path, payload)
    return True


def write_decision_surface_record(
    record: Dict[str, Any],
    *,
    path: Optional[str] = None,
    ramdisk_dir: str = "/var/www/html/ramdisk",
    stale_after_s: int = 1800,
) -> bool:
    return write_decision_surface_records(
        [record],
        path=path,
        ramdisk_dir=ramdisk_dir,
        stale_after_s=stale_after_s,
    )
