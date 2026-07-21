"""Diagnose und Persistenz der Wallboxentscheidungen.

Dieses Modul besitzt ausschließlich den Dateiausgang. Es trifft keine
Regelentscheidung und kommuniziert nie mit einem Wallboxtreiber.
"""

import json
import os
import time

from decision_history import write_history_record
from ems_decision_diagnostics import build_wallbox_decision_records, write_decision_surface_records


def _safe_float(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return float(default)


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
                    "inputs.cap_amp",
                    "inputs.set_amp",
                    "inputs.budget_stale",
                    "inputs.budget_timeout",
                    "storage_context.storage_state",
                    "storage_context.curve_wb_relief_active",
                    "storage_context.forecast_auto_relief_active",
                    "storage_context.curve_forecast_wallbox_stop_active",
                ),
                default_interval_s=60,
                default_max_bytes=8 * 1024 * 1024,
                default_retention_days=2,
                logger=self.logger,
            )
        except Exception as exc:
            self.logger.debug("Wallbox-Decision History konnte nicht geschrieben werden: %s", exc)

    def write_snapshot(self, ui_state, config, context=None):
        state = ui_state if isinstance(ui_state, dict) else {}
        context = context if isinstance(context, dict) else {}
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
            },
            "inputs": {
                "grid_w": int(round(_safe_float(state.get("grid_w_raw"), 0.0))),
                "bat_w": int(round(_safe_float(state.get("bat_w_raw"), 0.0))),
                "budget_raw_w": int(round(_safe_float(state.get("wb_budget_raw_w"), 0.0))),
                "effective_budget_w": int(round(_safe_float(state.get("wb_effective_budget_w"), 0.0))),
                "allowed_w": int(round(_safe_float(state.get("avail_wb_w"), 0.0))),
                "cap_amp": round(_safe_float(state.get("cap_amp"), 0.0), 3),
                "set_amp": round(_safe_float(state.get("set_amp"), 0.0), 3),
                "detected_phases": int(round(_safe_float(state.get("detected_phases"), 0.0))),
                "budget_stale": bool(context.get("budget_stale", False)),
                "budget_timeout": bool(context.get("budget_timeout", False)),
            },
            "wallboxes": state.get("wb_details", []),
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
