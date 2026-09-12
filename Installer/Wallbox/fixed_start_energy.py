"""Reines Energiekonto für einen begrenzten Erkennungsstart fester Wallboxen.

Dieses Konto ist eine konservative Obergrenze der PV-Deckungslücke, keine
Batterieenergiemessung. Es darf nicht zum bestehenden Gruppen-PCC-Konto addiert
werden. Der Manager bindet die Eingangswerte an seine gültige Quellenzusage,
persistiert Reservierung und Stoppsperre vor einem positiven Ausgang und bleibt
alleiniger Hardwareausgang. Dieses Modul besitzt keinerlei I/O.
"""
from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Mapping

SCHEMA = "wallbox_fixed_start_energy_v1"


class FixedStartEnergyInputError(ValueError):
    """Ein ungebundener Vertrag darf keinen Erkennungsstart freigeben."""


def _number(value: Any, *, minimum: float = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise FixedStartEnergyInputError("number_invalid")
    result = float(value)
    if not math.isfinite(result) or result < minimum:
        raise FixedStartEnergyInputError("number_invalid")
    return result


def _text(value: Any, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > 200 or any(ord(c) < 32 for c in value):
        raise FixedStartEnergyInputError("binding_invalid")
    if not empty and not value.strip():
        raise FixedStartEnergyInputError("binding_missing")
    return value


def _clock(value: Any) -> dict:
    if not isinstance(value, Mapping):
        raise FixedStartEnergyInputError("clock_invalid")
    if "schema_version" in value and (value.get("schema_version") != "ems_control_time_sample_v1" or value.get("valid") is not True):
        raise FixedStartEnergyInputError("clock_invalid")
    return {"wall_s": _number(value.get("wall_s", value.get("wall_ts"))),
            "monotonic_s": _number(value.get("monotonic_s", value.get("monotonic_ts"))),
            "boot_id": _text(value.get("boot_id")),
            "process_epoch": _text(value.get("process_epoch", ""), empty=True)}


def validate_fixed_start_energy_state(value: Any) -> dict:
    """Validiert einen bestehenden Stand, ohne Fehler als leeres Konto zu heilen."""
    if not isinstance(value, Mapping) or value.get("schema") != SCHEMA:
        raise FixedStartEnergyInputError("state_schema_invalid")
    state = deepcopy(dict(value))
    bindings = state.get("device_bindings")
    stopped = state.get("blocked_sessions")
    if not isinstance(bindings, dict) or not isinstance(stopped, dict) or len(bindings) > 64 or len(stopped) > 64:
        raise FixedStartEnergyInputError("group_bindings_invalid")
    for key, binding in bindings.items():
        if not str(key).isdigit() or not 1 <= int(key) <= 64:
            raise FixedStartEnergyInputError("group_owner_invalid")
        _text(binding)
    for key, session in stopped.items():
        if key not in bindings:
            raise FixedStartEnergyInputError("stop_owner_unbound")
        _text(session)
    wb_id = _number(state.get("wb_id"), minimum=1)
    if wb_id != int(wb_id) or wb_id > 64:
        raise FixedStartEnergyInputError("wallbox_id_invalid")
    for key in ("debt_wh", "consumed_wh", "reserved_wh", "remaining_s",
                "last_deficit_w", "worst_case_power_w", "generation"):
        _number(state.get(key))
    if state["generation"] != int(state["generation"]):
        raise FixedStartEnergyInputError("generation_invalid")
    if state["debt_wh"] > state["consumed_wh"] + 1e-6:
        raise FixedStartEnergyInputError("debt_exceeds_consumption")
    if not isinstance(state.get("probe_active"), bool):
        raise FixedStartEnergyInputError("probe_flag_invalid")
    for key in ("plug_session_id", "last_sample_id", "probe_id"):
        _text(state.get(key), empty=True)
    state["clock"] = _clock(state.get("clock"))
    state["energy_clock"] = _clock(state.get("energy_clock"))
    if state["probe_active"] and (
        not state["plug_session_id"] or not state["probe_id"]
        or state["remaining_s"] <= 0 or state["worst_case_power_w"] <= 0
        or state["reserved_wh"] + 1e-6
        < state["worst_case_power_w"] * state["remaining_s"] / 3600.0
    ):
        raise FixedStartEnergyInputError("reservation_invalid")
    return state


def retire_fixed_start_device_binding(
    previous_state: Any, *, wb_id: int, device_binding: str,
    plug_session_id: str, clock_sample: Mapping[str, Any],
) -> dict:
    """Rechnet eine entfernte Gerätebindung ab, ohne Stopautorität zu vererben.

    Nur der Manager darf einen belegten Konfigurationswechsel übergeben und
    muss den Rückgabestand vor jeder neuen Freigabe dauerhaft speichern.
    Defizitschuld bleibt gruppenweit erhalten; es entsteht kein neues Konto.
    """
    state = validate_fixed_start_energy_state(previous_state)
    identifier = _number(wb_id, minimum=1)
    if identifier != int(identifier) or identifier > 64:
        raise FixedStartEnergyInputError("wallbox_id_invalid")
    key = str(int(identifier))
    device = _text(device_binding)
    plug = _text(plug_session_id, empty=True)
    clock = _clock(clock_sample)
    if key not in state["device_bindings"]:
        raise FixedStartEnergyInputError("retired_device_unbound")
    if state["device_bindings"][key] == device:
        return state
    if state["wb_id"] == int(identifier):
        if state["probe_active"]:
            # Der entfernte Ausgang kann nicht mehr beobachtet werden.
            state["debt_wh"] += state["reserved_wh"]
            state["consumed_wh"] += state["reserved_wh"]
        state.update(probe_active=False, reserved_wh=0.0, remaining_s=0.0,
                     probe_id="", last_sample_id="", last_repayment_valid=False,
                     last_deficit_w=0.0, worst_case_power_w=0.0,
                     plug_session_id=plug, clock=clock, energy_clock=clock)
    state["device_bindings"][key] = device
    state["blocked_sessions"].pop(key, None)
    return validate_fixed_start_energy_state(state)


def evaluate_fixed_start_energy(
    previous_state: Any,
    evidence: Mapping[str, Any],
    *,
    clock_sample: Mapping[str, Any],
    threshold_wh: float = 40.0,
    probe_duration_s: float = 30.0,
    worst_case_power_w: float = 4140.0,
    release_w: float = 80.0,
) -> dict:
    """Bindet genau einen Probeversuch und bewahrt seine Defizitschuld.

    ``actual_w`` und ``pv_assigned_w`` sind frische, numerische, diesem
    Ladepunkt zugeordnete Größen. ``source_binding_valid`` bestätigt dieselbe
    Quellenentscheidung; weder Hausnetzbezug noch bloßes Sollbudget genügt.
    ``normal_pv_funded`` darf nur die bereits vorhandene reguläre PV-Freigabe
    abbilden. Sie ist keine neue Startfreigabe dieses Moduls.

    Neue Reservierungen, Abschlüsse und Sperren müssen vom Aufrufer dauerhaft
    gespeichert werden. Bei einem Wiederanlauf mit noch aktiver Reservierung
    wird deren unbekannter Rest konservativ verbraucht; es gibt keinen Replay.
    Ein Messintervall wird höchstens einmal anhand seiner sample_id gebucht.
    """
    if not isinstance(evidence, Mapping):
        raise FixedStartEnergyInputError("evidence_invalid")
    clock = _clock(clock_sample)
    threshold = _number(threshold_wh, minimum=0.000001)
    duration = _number(probe_duration_s, minimum=0.000001)
    maximum = _number(worst_case_power_w, minimum=0.000001)
    release = _number(release_w)
    wb_id = _number(evidence.get("wb_id"), minimum=1)
    if wb_id != int(wb_id) or wb_id > 64:
        raise FixedStartEnergyInputError("wallbox_id_invalid")
    device = _text(evidence.get("device_binding"))
    plug = _text(evidence.get("plug_session_id", ""), empty=True)
    sid = _text(evidence.get("sample_id", ""), empty=True)
    if previous_state in (None, {}):
        state = {"schema": SCHEMA, "wb_id": int(wb_id), "device_bindings": {str(int(wb_id)): device},
                 "plug_session_id": plug, "blocked_sessions": {},
                 "debt_wh": 0.0, "consumed_wh": 0.0, "reserved_wh": 0.0,
                 "remaining_s": 0.0, "last_deficit_w": 0.0,
                 "worst_case_power_w": 0.0, "generation": 0,
                 "probe_active": False, "probe_id": "", "last_sample_id": "",
                 "last_repayment_valid": False,
                 "clock": clock, "energy_clock": clock}
    else:
        state = validate_fixed_start_energy_state(previous_state)
    owner_key = str(int(wb_id))
    if owner_key in state["device_bindings"] and state["device_bindings"][owner_key] != device:
        raise FixedStartEnergyInputError("state_binding_mismatch")
    if state["probe_active"] and state["wb_id"] != wb_id:
        return {"schema": SCHEMA, "valid": True, "state": state,
                "allow_probe": False, "probe_active": True, "stop_required": False,
                "normal_pv_restart_allowed": False, "persist_before_output": False,
                "state_changed": False, "duplicate_sample": False,
                "reason": "group_probe_owner_busy", "blockers": ["group_probe_owner_busy"],
                "owner_wb_id": state["wb_id"], "reservation_id": state["probe_id"],
                "bridge_remaining_wh": state["reserved_wh"], "bridge_remaining_s": state["remaining_s"],
                "energy_semantics": "conservative_pv_cover_deficit_not_battery_measurement"}
    state["device_bindings"][owner_key] = device
    before = deepcopy(state)
    was_active = bool(state["probe_active"])
    same_boot = state["clock"]["boot_id"] == clock["boot_id"]
    dt = clock["monotonic_s"] - state["clock"]["monotonic_s"] if same_boot else 0.0
    clock_valid = same_boot and dt >= 0.0
    energy_dt = max(0.0, clock["monotonic_s"] - state["energy_clock"]["monotonic_s"]) if same_boot else 0.0
    process_changed = bool(state["clock"].get("process_epoch") and clock.get("process_epoch")
                           and state["clock"]["process_epoch"] != clock["process_epoch"])
    fresh = bool(evidence.get("sample_valid") is True
                 and evidence.get("sample_fresh") is True
                 and evidence.get("source_binding_valid") is True and sid)
    try:
        actual = _number(evidence.get("actual_w"))
        pv = _number(evidence.get("pv_assigned_w"))
        # Optional für bestehende Einzel-Owner-Aufrufer. Ein übergebenes,
        # unbekanntes Gruppenfeld ist hingegen keine belegte Nullleistung.
        group_deficit = (_number(evidence["group_pv_deficit_w"])
                         if "group_pv_deficit_w" in evidence else 0.0)
    except FixedStartEnergyInputError:
        actual = pv = group_deficit = None
        fresh = False
    duplicate = bool(sid and sid == state["last_sample_id"])
    changed_plug = state["wb_id"] != wb_id or state["plug_session_id"] != plug
    safety = evidence.get("safety_blocked") is True
    normal_pv = bool(fresh and clock_valid and not safety
                     and evidence.get("connected") is True and plug
                     and evidence.get("normal_pv_funded") is True
                     and group_deficit <= 0.0)
    blockers = []
    stop = False
    reason = "idle"
    must_persist = False

    def consume(wh: float) -> None:
        state["debt_wh"] += max(0.0, wh)
        state["consumed_wh"] += max(0.0, wh)

    def finish(reason_code: str, *, block: bool) -> None:
        nonlocal reason, must_persist
        if block and state["plug_session_id"]:
            state["blocked_sessions"][str(state["wb_id"])] = state["plug_session_id"]
        state["probe_active"] = False
        state["remaining_s"] = 0.0
        state["reserved_wh"] = 0.0
        reason = reason_code
        must_persist = True

    uncertain = bool(evidence.get("restart") is True or process_changed or not clock_valid
                     or changed_plug or not fresh or energy_dt > 15.0)
    if was_active and uncertain:
        # Ein verlorenes oder unbekanntes Intervall wird nicht auf 0 gekürzt.
        consume(state["reserved_wh"])
        stop = True
        finish("probe_observation_lost", block=True)
    elif was_active:
        if not duplicate:
            # Die Gruppenlücke enthält den Owner bereits. Beide Belege
            # werden deshalb über max gebunden und niemals addiert.
            deficit = max(0.0, actual - pv, group_deficit)
            # Zwischen zwei Messpunkten ist der höhere Randwert die konservative
            # Deckungslücke; sie wird ausdrücklich nicht Batterie-Wh genannt.
            booked_wh = max(state["last_deficit_w"], deficit) * energy_dt / 3600.0
            consume(booked_wh)
            state["reserved_wh"] = max(0.0, state["reserved_wh"] - booked_wh)
            state["last_deficit_w"] = deficit
        state["remaining_s"] = max(0.0, state["remaining_s"] - dt)
        # Nicht beobachtete Zeitanteile bleiben reserviert, auch wenn derselbe
        # Messpunkt mehrfach gelesen wird. Ein Neustart kann sie nicht löschen.
        state["reserved_wh"] = max(state["reserved_wh"], state["worst_case_power_w"] * state["remaining_s"] / 3600.0)
        if safety or evidence.get("deficit_stop") is True or state["debt_wh"] >= threshold:
            stop = True
            finish("probe_deficit_stop", block=True)
        elif actual > state["worst_case_power_w"] + 1.0:
            stop = True
            finish("probe_power_exceeds_reservation", block=True)
        elif group_deficit > state["worst_case_power_w"]:
            stop = True
            finish("group_deficit_exceeds_reservation", block=True)
        elif evidence.get("completed") is True and group_deficit <= 0.0:
            finish("probe_completed", block=False)
        elif state["remaining_s"] <= 0.0:
            stop = True
            finish("probe_timeout", block=True)
        else:
            reason = "probe_running"
    elif (normal_pv and not duplicate and actual > 0.0 and pv >= actual
          and before.get("last_repayment_valid") is True
          and not changed_plug and not process_changed
          and evidence.get("restart") is not True and 0.0 < energy_dt <= 15.0):
        # Das kumulative Verbrauchsprotokoll bleibt erhalten. Nur eine reale,
        # quellengebundene PV-Ladung zahlt die Defizitschuld langsam zurück.
        state["debt_wh"] = max(0.0, state["debt_wh"] - min(release, actual) * energy_dt / 3600.0)
        reason = "normal_pv_repayment"

    if not state["probe_active"] and not was_active and evidence.get("probe_requested") is True:
        if not fresh:
            blockers.append("fresh_source_evidence_missing")
        if not clock_valid:
            blockers.append("clock_discontinuity")
        if safety:
            blockers.append("safety_blocked")
        if evidence.get("connected") is not True or not plug:
            blockers.append("plug_binding_missing")
        if plug and plug == state["blocked_sessions"].get(owner_key):
            blockers.append("same_plug_deficit_stop")
        try:
            if _number(evidence.get("actual_pv_surplus_w")) < 1380.0:
                blockers.append("real_pv_below_detection_minimum")
        except FixedStartEnergyInputError:
            blockers.append("real_pv_evidence_missing")
        if duration > 30.0 or maximum > 4554.0:
            blockers.append("detection_profile_exceeds_limit")
        reserve = maximum * duration / 3600.0
        if state["debt_wh"] + reserve > threshold + 1e-9:
            blockers.append("energy_reservation_unfunded")
        if not blockers:
            state["generation"] += 1
            state["probe_id"] = "%d:%d" % (wb_id, state["generation"])
            state["probe_active"] = True
            state["remaining_s"] = duration
            state["reserved_wh"] = reserve
            state["worst_case_power_w"] = maximum
            state["last_deficit_w"] = 0.0
            reason = "probe_reserved"
            must_persist = True
        else:
            reason = blockers[0]
    if not was_active or not changed_plug:
        state["wb_id"] = int(wb_id)
        state["plug_session_id"] = plug
    if fresh and not duplicate:
        state["last_sample_id"] = sid
        state["energy_clock"] = clock
    state["last_repayment_valid"] = bool(
        not was_active and not state["probe_active"] and normal_pv
        and actual > 0.0 and pv >= actual
        and not process_changed and evidence.get("restart") is not True
    )
    if (plug and state["blocked_sessions"].get(owner_key) == plug
        and evidence.get("connected") is True and not normal_pv):
        stop = True
        if reason == "idle":
            reason = "same_plug_deficit_stop"
    state["clock"] = clock
    return {"schema": SCHEMA, "valid": True, "state": state,
            "allow_probe": bool(state["probe_active"] and not stop),
            "probe_active": bool(state["probe_active"]),
            "stop_required": stop, "normal_pv_restart_allowed": normal_pv,
            "persist_before_output": must_persist,
            "state_changed": state != before, "duplicate_sample": duplicate,
            "reason": reason, "blockers": blockers,
            "owner_wb_id": state["wb_id"], "reservation_id": state["probe_id"],
            "bridge_remaining_wh": state["reserved_wh"], "bridge_remaining_s": state["remaining_s"],
            "energy_semantics": "conservative_pv_cover_deficit_not_battery_measurement"}
