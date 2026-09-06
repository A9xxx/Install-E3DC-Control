"""Diagnose und Persistenz der Wallboxentscheidungen.

Dieses Modul besitzt ausschließlich den Dateiausgang. Es trifft keine
Regelentscheidung und kommuniziert nie mit einem Wallboxtreiber.
"""

import json
import math
import os
import time

from decision_history import HISTORY_NORMAL_HEARTBEAT_S, write_history_record
from ems_decision_diagnostics import build_wallbox_decision_records, write_decision_surface_records


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


def _mapping_subset(value, keys):
    source = value if isinstance(value, dict) else {}
    return {key: source.get(key) for key in keys if key in source}


def _finite_diagnostic_number(value):
    """Erhält den Quellwert; fehlende oder unendliche Werte sind keine Null."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(float(value)) else None
    except (ValueError, OverflowError):
        return None


def _budget_balance_diagnostic(value):
    """Begrenzte Quellenbeschreibung ohne neue Budget- oder Freigabeautorität."""
    if not isinstance(value, dict):
        return None
    sources = ("existing_filtered_balance", "raw_ems_after_confirmed_idle")
    reasons = (
        "idle_frame_coherent", "charging_or_status_unknown",
        "live_sample_invalid", "live_sample_stale", "live_before_wallbox_sample",
        "producer_budget_not_authorized", "grid_import_present", "non_finite_input",
    )
    result = {
        "source": value.get("source") if value.get("source") in sources else None,
        "reason": value.get("reason") if value.get("reason") in reasons else None,
    }
    for key in (
        "live_sample_ts", "wallbox_sample_ts", "grid_raw_w", "grid_filtered_w",
        "wallbox_power_w", "coherent_available_w",
    ):
        result[key] = _finite_diagnostic_number(value.get(key))
    return result


def transient_grid_pm_hold_diagnostic(value):
    """Begrenzt die letzte PM-Halteprüfung ohne IDs oder neue Regelautorität."""
    source = value if isinstance(value, dict) else {}
    if source.get("contract") != "wallbox_transient_grid_pm_output_hold_v1":
        return None

    def token(item):
        if (
            isinstance(item, str)
            and 0 < len(item) <= 64
            and item.isascii()
            and all(char.islower() or char.isdigit() or char == "_" for char in item)
        ):
            return item
        return None

    result = {
        "contract": "wallbox_transient_grid_pm_output_hold_v1",
        "observation_scope": "last_evaluation",
    }
    for key in ("active", "output_hold_only", "new_output_authorized"):
        result[key] = source.get(key) is True
    for key in ("family", "reason"):
        result[key] = token(source.get(key))
    for key in (
        "observed_ts", "hold_amp", "max_hold_s", "anchor_age_s",
        "episode_age_s", "grid_w", "grid_pm_w",
    ):
        raw = source.get(key)
        try:
            number = float(raw) if isinstance(raw, (int, float)) and not isinstance(raw, bool) else None
        except (TypeError, ValueError, OverflowError):
            number = None
        result[key] = round(number, 3) if number is not None and math.isfinite(number) else None
    blockers = source.get("blockers")
    blockers = blockers if isinstance(blockers, list) else []
    result["blockers"] = [entry for entry in (token(item) for item in blockers[:8]) if entry]
    result["blocker_count"] = len(blockers)
    result["blockers_truncated"] = len(blockers) > 8
    return result


def _wallboxes_with_pm_hold_diagnostics(wallboxes, context):
    """Kopiert nur die Diagnoseansicht; der Befehlsplan bleibt unangetastet."""
    holds = context.get("transient_grid_pm_output_hold_by_id")
    holds = holds if isinstance(holds, dict) else {}
    result = []
    for item in wallboxes if isinstance(wallboxes, list) else []:
        if not isinstance(item, dict):
            continue
        detail = dict(item)
        diagnostic = transient_grid_pm_hold_diagnostic(holds.get(str(item.get("id"))))
        if diagnostic is not None:
            original_payload = item.get("decision_payload")
            payload = dict(original_payload) if isinstance(original_payload, dict) else {}
            original_diagnostics = payload.get("diagnostics")
            diagnostics = dict(original_diagnostics) if isinstance(original_diagnostics, dict) else {}
            diagnostics["transient_grid_pm_output_hold"] = diagnostic
            payload["diagnostics"] = diagnostics
            detail["decision_payload"] = payload
        result.append(detail)
    return result


def _blocked_gate_event(name, value):
    gate = value if isinstance(value, dict) else {}
    blocked = bool(
        gate.get("blocked") is True
        or gate.get("hard_block") is True
        or gate.get("veto") is True
        or gate.get("valid") is False
        or gate.get("allowed") is False
        or gate.get("execute") is False
    )
    if not blocked:
        return None
    event = {"gate": name, "blocked": True}
    event.update(
        _mapping_subset(
            gate,
            (
                "reason",
                "blocker",
                "code",
                "state",
                "source",
            ),
        )
    )
    return event


def wallbox_critical_events(wallboxes, *, budget_stale=False, budget_timeout=False):
    """Verdichtet ausschließlich sofort zu sichernde Wallbox-Kanten.

    Strom- und Leistungsziele gehören bewusst nicht in diese Signatur. Ein
    neuer Hardware-Receipt, Stop, Phasenwechsel, Veto oder Datenfehler bleibt
    dagegen auch dann sofort erhalten, wenn der normale Archivheartbeat erst
    später fällig wäre.
    """

    events = [{
        "scope": "budget",
        "stale": bool(budget_stale),
        "timeout": bool(budget_timeout),
    }]
    for raw in wallboxes or []:
        if not isinstance(raw, dict):
            continue
        event = {"scope": "wallbox", "id": raw.get("id")}
        for key in (
            "plug",
            "connected",
            "charging",
            "manager_stop_pending",
            "driver_status_valid",
            "driver_status_stale",
            "driver_status_glitch",
            "rscp_error_active",
            "command_blocked",
            "last_command_ok",
            "phase_actual_phases",
            "physical_phases",
            "phases_in_use",
            "e3dc_session_stop_active",
            "openwb_pro_session_stop_active",
            "openwb_secondary_session_stop_active",
            "goe_session_stop_active",
            "fault_state",
        ):
            if key in raw:
                event[key] = raw.get(key)
        if raw.get("manager_stop_pending"):
            event["manager_stop_reason"] = raw.get("manager_stop_reason")
        if raw.get("driver_status_stale") or raw.get("driver_status_glitch"):
            event["driver_status_reason"] = raw.get("driver_status_reason")

        receipt = raw.get("last_executed_command")
        if isinstance(receipt, dict):
            # Das Zyklustoken bindet einen echten neuen Hardware-Receipt. Der
            # restliche Receipt-Inhalt bleibt im vollständigen Datensatz. Für
            # die Persistenzkante zählt allein die Identität; eine erneute
            # Projektion desselben Receipts darf keinen SD-Schreibzyklus
            # erzeugen.
            cycle_token = receipt.get("cycle_token")
            if cycle_token not in (None, ""):
                event["receipt_token"] = str(cycle_token)
            else:
                # Rückwärtskompatibler Fallback für ältere Treiber ohne Token.
                event["receipt"] = _mapping_subset(
                    receipt,
                    (
                        "method",
                        "amp",
                        "force_state",
                        "forced_zero",
                        "target_phases",
                    ),
                )

        payload = raw.get("decision_payload")
        payload = payload if isinstance(payload, dict) else {}
        # START/STOP, Strom- und Phasenabsichten sind noch keine physische
        # Hardwarekante. Diese wird eindeutig über Receipt, Ladezustand und
        # Ist-Phasen gebunden. Dadurch erzeugen START→SET_CURRENT und
        # SET_CURRENT↔NOOP bei demselben Receipt keine Doppelspur im Archiv.
        payload_inputs = payload.get("inputs") if isinstance(payload.get("inputs"), dict) else {}
        if payload_inputs.get("priority_forced_stop"):
            event["priority_forced_stop"] = True
        if payload_inputs.get("budget_timeout"):
            event["budget_timeout"] = True

        blocked_gates = []
        for gate_name in (
            "command_guard",
            "e3dc_edge_guard",
            "charger_controller_output_gate",
            "effective_current_output_gate",
            "vehicle_current_output_gate",
            "multi_allocation_output_gate",
            "peak_shaving_output_gate",
            "openwb_pro_one_phase_output_gate",
        ):
            blocked = _blocked_gate_event(gate_name, raw.get(gate_name))
            if blocked is not None:
                blocked_gates.append(blocked)
        if blocked_gates:
            event["vetoes"] = blocked_gates
        events.append(event)
    return sorted(
        events,
        key=lambda item: (str(item.get("scope") or ""), str(item.get("id") or "")),
    )


class WallboxDiagnostics:
    """Persistiert Wallbox-Snapshots unabhängig vom Regelkreis."""

    def __init__(self, *, log_dir, latest_path, history_prefix, ems_path, logger):
        self.log_dir = log_dir
        self.latest_path = latest_path
        self.history_prefix = history_prefix
        self.ems_path = ems_path
        self.logger = logger
        self.history_state = {}

    def write_json_atomic(self, path, payload, mode=0o664):
        tmp = "%s.tmp.%s" % (path, os.getpid())
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False)
            try:
                os.chmod(tmp, mode)
            except Exception:
                pass
            os.replace(tmp, path)
            try:
                os.chmod(path, mode)
            except Exception:
                pass
            return True
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            self.logger.debug("JSON-Schreibvorgang übersprungen (%s): %s", path, exc)
            return False

    def cleanup_history(self, retention_days=14):
        today = time.strftime("%Y-%m-%d")
        if self.history_state.get("legacy_cleanup_day") == today:
            return
        self.history_state["legacy_cleanup_day"] = today
        cutoff = time.time() - max(1, int(retention_days or 14)) * 86400
        try:
            for name in os.listdir(self.log_dir):
                if not name.startswith(self.history_prefix):
                    continue
                if not (name.endswith(".jsonl") or name.endswith(".jsonl.gz")):
                    continue
                path = os.path.join(self.log_dir, name)
                if os.path.isfile(path) and os.path.getmtime(path) < cutoff:
                    os.remove(path)
        except Exception as exc:
            self.logger.debug("Wallbox-Decision-Cleanup übersprungen: %s", exc)

    def write_history(self, record, config):
        enabled = str((config or {}).get("wallbox_decision_history_enable", 1)).strip().lower()
        if enabled in ("0", "false", "no", "off", "nein", "aus"):
            return
        try:
            write_history_record(
                record,
                config=config or {},
                log_dir=self.log_dir,
                latest_path=self.latest_path,
                prefix=self.history_prefix,
                enable_key="wallbox_decision_history_enable",
                max_bytes_key="wallbox_decision_history_max_bytes",
                retention_key="wallbox_decision_history_retention_days",
                interval_key="wallbox_decision_history_interval_s",
                state=self.history_state,
                signature_paths=(
                    "decision.state",
                    "decision.mode_public",
                    "decision.mode_control",
                    "decision.battery_request",
                    "decision.reason",
                    "decision.made_changes",
                    "decision.scheduled_slot_active",
                    "decision.price_boost_active",
                    "decision.predump_wallbox_active",
                    "storage_context.storage_state",
                    "storage_context.curve_wb_relief_active",
                    "storage_context.forecast_auto_relief_active",
                    "storage_context.curve_forecast_wallbox_stop_active",
                ),
                critical_signature_paths=(
                    "decision.critical_events",
                ),
                summary_paths=(
                    "inputs.grid_w",
                    "inputs.bat_w",
                    "inputs.budget_raw_w",
                    "inputs.effective_budget_w",
                    "inputs.producer_budget_w",
                    "inputs.budget_effective_w",
                    "inputs.allowed_w",
                    "inputs.cap_amp",
                    "inputs.set_amp",
                ),
                summary_state_path="decision.state",
                default_interval_s=HISTORY_NORMAL_HEARTBEAT_S,
                minimum_interval_s=HISTORY_NORMAL_HEARTBEAT_S,
                default_max_bytes=8 * 1024 * 1024,
                default_retention_days=2,
                logger=self.logger,
            )
        except Exception as exc:
            self.logger.debug("Wallbox-Decision History konnte nicht geschrieben werden: %s", exc)

    def write_snapshot(self, ui_state, config, context=None):
        state = ui_state if isinstance(ui_state, dict) else {}
        context = context if isinstance(context, dict) else {}
        budget_stale = bool(context.get("budget_stale", False))
        budget_timeout = bool(context.get("budget_timeout", False))
        wallboxes = _wallboxes_with_pm_hold_diagnostics(
            state.get("wb_details", []), context,
        )
        record = {
            "ts": int(time.time()),
            "time": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "service": "wallbox_manager",
            "decision": {
                "state": state.get("status_msg", "unknown"),
                "mode_public": state.get("wb_mode_active"),
                "mode_control": state.get("wb_control_mode"),
                "battery_request": state.get("battery_request", context.get("battery_request")),
                "reason": context.get("intent_reason", state.get("operator_hint_text", "")),
                "made_changes": bool(context.get("made_changes", False)),
                "scheduled_slot_active": bool(state.get("scheduled_slot_active", False)),
                "price_boost_active": bool(state.get("price_boost_active", False)),
                "predump_wallbox_active": bool(state.get("predump_wallbox_active", False)),
                "predump_wallbox_gate_open": bool(state.get("predump_wallbox_gate_open", False)),
                "critical_events": wallbox_critical_events(
                    wallboxes,
                    budget_stale=budget_stale,
                    budget_timeout=budget_timeout,
                ),
            },
            "inputs": {
                "grid_w": int(round(_safe_float(state.get("grid_w_raw"), 0.0))),
                "bat_w": int(round(_safe_float(state.get("bat_w_raw"), 0.0))),
                "budget_raw_w": int(round(_safe_float(state.get("wb_budget_raw_w"), 0.0))),
                "effective_budget_w": int(round(_safe_float(state.get("wb_effective_budget_w"), 0.0))),
                # Legacy-Rohbudget ist bereits im Empfänger bearbeitet. Die
                # Originalzuteilung wird separat und ohne Ersatzwert erhalten.
                "producer_budget_w": _finite_diagnostic_number(context.get("producer_budget_w")),
                "budget_effective_w": _finite_diagnostic_number(state.get("wb_effective_budget_w")),
                "budget_balance": _budget_balance_diagnostic(context.get("budget_balance_diagnostic")),
                "allowed_w": int(round(_safe_float(state.get("avail_wb_w"), 0.0))),
                "cap_amp": round(_safe_float(state.get("cap_amp"), 0.0), 3),
                "set_amp": round(_safe_float(state.get("set_amp"), 0.0), 3),
                "detected_phases": int(round(_safe_float(state.get("detected_phases"), 0.0))),
                "budget_stale": budget_stale,
                "budget_timeout": budget_timeout,
            },
            "wallboxes": wallboxes,
            "storage_context": {
                "storage_state": context.get("storage_state"),
                "wbminsoc_gate_open": bool(state.get("wbminsoc_gate_open", False)),
                "curve_wb_relief_active": bool(state.get("curve_wb_relief_active", False)),
                "forecast_auto_relief_active": bool(state.get("forecast_auto_relief_active", False)),
                "curve_forecast_wallbox_stop_active": bool(state.get("curve_forecast_wallbox_stop_active", False)),
                "curve_forecast_wallbox_reason": state.get("curve_forecast_wallbox_reason"),
                "price_plan_storage_protect": bool(state.get("price_plan_storage_protect", False)),
            },
        }
        self.write_history(record, config)
        try:
            write_decision_surface_records(build_wallbox_decision_records(record), path=self.ems_path)
        except Exception as exc:
            self.logger.debug("EMS-Decision-Surface für Wallbox konnte nicht geschrieben werden: %s", exc)
