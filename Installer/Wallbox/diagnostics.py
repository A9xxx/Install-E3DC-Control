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


def _core_gate_event(wb_id, now_ts, since_ts=None):
    """Letzter Auditbeleg dieser Box; keine Freigabe und kein Gerätezugriff."""
    from .command_gate import AUDIT_LOG

    latest = None
    # Auch nach einer Rotation bleibt der letzte vorhandene Beleg lesbar.
    for path in (AUDIT_LOG + ".1", AUDIT_LOG):
        try:
            with open(path, "rb") as handle:
                handle.seek(0, os.SEEK_END)
                size = handle.tell()
                handle.seek(max(0, size - 262144))
                if size > 262144:
                    handle.readline()
                lines = handle.read().splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            try:
                event = json.loads(line)
                stamp = float(event.get("ts"))
            except (ValueError, TypeError, AttributeError):
                continue
            if (str(event.get("wb")) != str(wb_id)
                    or event.get("decision") not in ("allowed", "blocked", "emergency_allowed")
                    or not math.isfinite(stamp) or stamp > now_ts or stamp <= 0
                    or (since_ts is not None and stamp < since_ts)):
                continue
            if latest is None or stamp > latest[0]:
                latest = (stamp, event["decision"])
            break
    return latest


def core_status_sentence(status, box, *, now_ts, box_stopped=False):
    """Lesbarer Status mit getrenntem Treiberergebnis, Boxzustand und Gatebeleg."""
    sentence = str(status.get("sentence") or "Regelkern wartet auf gültigen Zyklus")
    # Die bestehenden Suffixe bleiben für die Diagnosehistorie abtrennbar.
    suffixes = ("; Hauslast je Phase", "; Hauslast (ungünstigste Lage) je Phase",
                "; Haus inkl. Verbraucher je Phase", "; Boxquittung")
    end = min((sentence.find(key) for key in suffixes if key in sentence), default=len(sentence))
    head, tail = sentence[:end], sentence[end:]
    tail = tail.replace("; Boxquittung ", "; Sollstrom-Boxquittung ")
    states = {"idle": "bereit", "start_wait": "Start abwarten", "charging": "lädt",
              "hold": "halten", "stop_wait": "Stopp abwarten", "stopped": "gestoppt",
              "protection": "Schutzstopp"}
    actions = {"none": "kein neuer Befehl", "stop": "Stopp senden",
               "stop_confirmed": "Stopp nachlesen", "start": "Start senden",
               "set_current": "Sollstrom senden", "set_phases": "Phasen wechseln"}
    parts = [head, "Zustand " + states.get(status.get("state"), "unbekannt"),
             "Aktion " + actions.get(status.get("action"), "unbekannt")]
    stopping = (status.get("action") in ("stop", "stop_confirmed")
                or status.get("state") in ("stopped", "stop_wait"))
    if status.get("action") in ("stop", "start", "set_current", "set_phases"):
        result = status.get("command_confirmed")
        parts.append("Treiberbefehl " + ("bestätigt" if result is True else
                     "nicht bestätigt" if result is False else "Ergebnis unbekannt"))
    if stopping:
        parts.append("Box hat den Stopp bestätigt" if box_stopped else
                     "Box hat den Stopp noch nicht bestätigt")
    since = _finite_diagnostic_number((box.get("_simple_core_diagnostics") or {}).get("stop_since_ts")) if stopping else None
    event = _core_gate_event(box.get("id"), now_ts, since)
    if event:
        stamp, decision = event
        parts.append("Befehl am Gate: ja (" + time.strftime("%d.%m. %H:%M:%S", time.localtime(stamp))
                     + (", abgewiesen)" if decision == "blocked" else ", zugelassen)"))
    else:
        parts.append("Befehl am Gate: nein (kein Auditbeleg" + (" seit Stoppbeginn)" if since is not None else ")"))
        if (stopping and status.get("action") != "stop_confirmed" and not box_stopped
                and since is not None and now_ts - since > 10):
            parts.append("Hinweis: Kern fordert seit mehr als einem Zyklus Stopp; kein Befehl am Gate belegt")
    return " · ".join(parts) + tail


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
        cores = (record.get("decision") or {}).get("core_wallboxes") or {}
        core_paths = tuple(
            "decision.core_wallboxes.%s.%s" % (cid, key)
            for cid in sorted(cores) for key in ("state", "reason")
        )
        # Echte Kernwechsel sofort sichern, Messwertbewegungen nur im Grundtakt.
        # Bei Mischbetrieb bleiben ausschließlich die kernlosen Boxen im Altpfad.
        legacy_paths = tuple(
            "decision.legacy_wallboxes.%s.%s" % (cid, key)
            for cid in sorted((record.get("decision") or {}).get("legacy_wallboxes") or {})
            for key in ("state", "reason")
        )
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
                signature_paths=(core_paths + legacy_paths) if cores else (
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
                ) + core_paths,
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
        # Nur Diagnose: den Anzeigegrund über cycle_wait hinweg erhalten,
        # passend zum bereits im Kern gehaltenen letzten echten Satz.
        previous = self.history_state.get("core_display_reasons", {})
        cores = {}
        legacy = {}
        for box in wallboxes:
            if not isinstance(box, dict):
                continue
            cid = str(box.get("id"))
            if not box.get("simple_core_active"):
                legacy[cid] = {"state": box.get("state"), "reason": box.get("state_reason")}
                continue
            reason = box.get("simple_core_reason")
            if reason == "cycle_wait":
                reason = previous.get(cid)
            sentence = str(box.get("simple_core_sentence") or "")
            # Die verdichtete Historie behält den Beschlusssatz ohne die
            # wechselnden Anzeigeangaben zu Aktion, Quittung und Gatezeit.
            sentence = sentence.split(" · Zustand ", 1)[0]
            for suffix in ("; Hauslast je Phase", "; Hauslast (ungünstigste Lage) je Phase", "; Haus inkl. Verbraucher je Phase", "; Boxquittung", "; Sollstrom-Boxquittung"):
                sentence = sentence.split(suffix, 1)[0]
            data = box.get("simple_core_data_diagnostics")
            data = data if isinstance(data, dict) else {}
            budget = _finite_diagnostic_number(data.get("share_w"))
            # Erkannte Phasen und ein reservierter Phasenwechsel erklären Stopp oder Abstieg.
            phases = box.get("phases_in_use")
            transition = box.get("transition_state")
            transition = transition if isinstance(transition, dict) else {}
            cores[cid] = {
                "state": box.get("simple_core_state"), "reason": reason,
                "sentence": sentence.strip(), "budget_w": budget,
                "budget_reason": None if budget is not None else "share_unavailable",
                "source": data.get("source"),
                "phases": phases if phases in (1, 2, 3) else None,
                "phase_change": ({"state": transition.get("state"), "target_phases": transition.get("target_phases")}
                                 if transition.get("active") is True else None),
            }
        self.history_state["core_display_reasons"] = {cid: box["reason"] for cid, box in cores.items()}
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
                "grid_w": (int(round(state["grid_w_raw"]))
                    if _finite_diagnostic_number(state.get("grid_w_raw")) is not None else None),
                "bat_w": (int(round(state["bat_w_raw"]))
                    if _finite_diagnostic_number(state.get("bat_w_raw")) is not None else None),
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
        if cores:
            # decision bleibt auch in der verdichteten JSONL-Historie erhalten.
            record["decision"]["core_wallboxes"] = cores
            record["decision"]["legacy_wallboxes"] = legacy
        self.write_history(record, config)
        try:
            write_decision_surface_records(build_wallbox_decision_records(record), path=self.ems_path)
        except Exception as exc:
            self.logger.debug("EMS-Decision-Surface für Wallbox konnte nicht geschrieben werden: %s", exc)
