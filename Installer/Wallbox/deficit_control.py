"""Reiner Gruppen-Defizitregler für Wallboxen.

Das Modul besitzt bewusst weder Datei-, Netzwerk- noch Hardware-I/O. Pro
frischem Netzpunkt-Snapshot wird genau ein gemeinsames Energiekonto
fortgeschrieben. Eine daraus entstehende Aktion ist exakt an die marginale
Wallbox gebunden; weitere Wallboxen dürfen denselben Netzbezug weder erneut
integrieren noch daraus eine zweite Aktion ableiten.

Netzbezug und eine Überziehung des ausdrücklich autorisierten
Wallbox-Budgets bleiben getrennt sichtbar. Für die Schutzentscheidung gilt
jedoch eine eindeutige Priorität: Sobald echter Netzbezug vorliegt, zählt nur
der Netzbezug in das gemeinsame Energiekonto. Die Budgetüberziehung bleibt
dann reine Diagnose und darf den Netz-Wächter nicht beschleunigen. Ohne
Netzbezug zählt ausschließlich eine frisch und sitzungsgebunden belegte
Budgetüberziehung. Nicht doppelt gezählt wird außerdem derselbe PCC-Snapshot:
Er besitzt genau ein Konto und einen Aktionsbesitzer. Eine Batterie-, PV- oder
Hausverbrauchskomponente wird hier niemals geschätzt.
"""

from __future__ import annotations

from copy import deepcopy
import math
from typing import Any, Dict, Mapping, Optional


STATE_SCHEMA = "wallbox_group_deficit_control_v1"
LEDGER_SCHEMA = "wallbox_group_deficit_ledger_v1"
CASCADE_SCHEMA = "wallbox_deficit_cascade_v1"
ACTION_SCHEMA = "wallbox_deficit_action_v1"

ACTION_HOLD = "hold"
ACTION_CURRENT_DOWN = "current_down"
ACTION_PHASE_DOWN = "phase_down"
ACTION_STOP = "stop"

STAGE_THREE_PHASE_CURRENT = "three_phase_current"
STAGE_THREE_PHASE_MIN_WATCH = "three_phase_min_watch"
STAGE_PHASE_DOWN_PENDING = "phase_down_pending"
STAGE_ONE_PHASE_CURRENT = "one_phase_current"
STAGE_ONE_PHASE_MIN_WATCH = "one_phase_min_watch"
STAGE_STOP_PENDING = "stop_pending"

TOPOLOGY_SWITCHABLE = "switchable_1p_3p"
TOPOLOGY_FIXED_ONE = "fixed_1p"
TOPOLOGY_FIXED_THREE = "fixed_3p"


class DeficitControlInputError(ValueError):
    """Kennzeichnet einen ungültigen statischen Aufrufvertrag."""


def _finite(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _positive(value: Any, *, name: str, allow_zero: bool = False) -> float:
    number = _finite(value)
    if number is None or number < 0.0 or (not allow_zero and number == 0.0):
        qualifier = "nichtnegativ" if allow_zero else "positiv"
        raise DeficitControlInputError("%s muss endlich und %s sein" % (name, qualifier))
    return float(number)


def _wb_id(value: Any) -> int:
    if isinstance(value, bool):
        raise DeficitControlInputError("marginal_wb_id muss eine positive Ganzzahl sein")
    try:
        result = int(value)
    except (TypeError, ValueError):
        result = 0
    if result <= 0 or str(value).strip() not in (str(result), "%d.0" % result):
        raise DeficitControlInputError("marginal_wb_id muss eine positive Ganzzahl sein")
    return result


def _phase_count(value: Any) -> int:
    number = _finite(value)
    if number is None or int(round(number)) not in (1, 3):
        raise DeficitControlInputError("actual_phases muss 1 oder 3 sein")
    return int(round(number))


def _snapshot_id(value: Any) -> str:
    text = str(value or "").strip()
    if not text or len(text) > 160 or any(ord(char) < 33 for char in text):
        raise DeficitControlInputError("snapshot_id muss ein kompakter, nichtleerer Bezeichner sein")
    return text


def _round_down_to_step(value: float, step: float) -> float:
    units = math.floor(max(0.0, value) / step + 1e-9)
    return round(units * step, 3)


def _round_up_to_step(value: float, step: float) -> float:
    units = math.ceil(max(0.0, value) / step - 1e-9)
    return round(units * step, 3)


def _topology(*, supports_phase_switch: bool, prevent_phase_switch: bool, actual_phases: int) -> str:
    if bool(supports_phase_switch) and not bool(prevent_phase_switch):
        return TOPOLOGY_SWITCHABLE
    return TOPOLOGY_FIXED_ONE if actual_phases == 1 else TOPOLOGY_FIXED_THREE


def _stage_for_physics(actual_phases: int, current_amp: float, min_amp: float) -> str:
    at_minimum = current_amp <= min_amp + 1e-6
    if actual_phases == 3:
        return STAGE_THREE_PHASE_MIN_WATCH if at_minimum else STAGE_THREE_PHASE_CURRENT
    return STAGE_ONE_PHASE_MIN_WATCH if at_minimum else STAGE_ONE_PHASE_CURRENT


def _empty_ledger() -> Dict[str, Any]:
    return {
        "schema_version": LEDGER_SCHEMA,
        "last_snapshot_id": "",
        "last_sample_ts": None,
        "bucket_wh": 0.0,
        "grid_bucket_wh": 0.0,
        "authorized_budget_bucket_wh": 0.0,
        "bucket_component": "none",
        "stage_generation": 0,
        "grid_deficit_w": None,
        "authorized_budget_overrun_w": None,
        "counted_component": "none",
        "counted_total_w": None,
        "counted_total_complete": False,
        "authorized_budget_contract_valid": False,
        "sample_valid": False,
        "sample_fresh": False,
        "dt_s": 0.0,
        "dt_clamped": False,
        "threshold_reached": False,
        "reason": "uninitialized",
    }


def _empty_cascade(wb_id: int, topology: str, stage: str) -> Dict[str, Any]:
    return {
        "schema_version": CASCADE_SCHEMA,
        "marginal_wb_id": wb_id,
        "topology": topology,
        "stage": stage,
        "generation": 0,
        "phase_down_requested": False,
        "reason": "initialized",
    }


def _hold_action(wb_id: int, reason: str, *, stage: str, fail_closed: bool = False) -> Dict[str, Any]:
    return {
        "schema_version": ACTION_SCHEMA,
        "type": ACTION_HOLD,
        "marginal_wb_id": wb_id,
        "target_amp": None,
        "target_phases": None,
        "reason": str(reason),
        "stage": stage,
        "fail_closed": bool(fail_closed),
    }


def _action(
    action_type: str,
    wb_id: int,
    reason: str,
    *,
    stage: str,
    target_amp: Optional[float] = None,
    target_phases: Optional[int] = None,
) -> Dict[str, Any]:
    return {
        "schema_version": ACTION_SCHEMA,
        "type": action_type,
        "marginal_wb_id": wb_id,
        "target_amp": target_amp,
        "target_phases": target_phases,
        "reason": str(reason),
        "stage": stage,
        "fail_closed": False,
    }


def _validated_previous(previous_state: Any) -> Dict[str, Any]:
    if not isinstance(previous_state, Mapping):
        return {}
    if previous_state and previous_state.get("schema_version") != STATE_SCHEMA:
        raise DeficitControlInputError("previous_state besitzt ein unbekanntes Schema")
    return deepcopy(dict(previous_state))


def _reset_bucket(ledger: Dict[str, Any], *, reason: str) -> None:
    ledger["bucket_wh"] = 0.0
    ledger["grid_bucket_wh"] = 0.0
    ledger["authorized_budget_bucket_wh"] = 0.0
    ledger["bucket_component"] = "none"
    ledger["threshold_reached"] = False
    ledger["stage_generation"] = int(ledger.get("stage_generation", 0) or 0) + 1
    ledger["bucket_reset_reason"] = str(reason)


def _result(
    *,
    snapshot_id: str,
    sample_ts: Optional[float],
    wb_id: int,
    ledger: Dict[str, Any],
    cascade: Dict[str, Any],
    action: Dict[str, Any],
    duplicate_snapshot: bool = False,
) -> Dict[str, Any]:
    return {
        "schema_version": STATE_SCHEMA,
        "snapshot_id": snapshot_id,
        "sample_ts": sample_ts,
        "marginal_wb_id": wb_id,
        "single_action_owner": True,
        "duplicate_snapshot": bool(duplicate_snapshot),
        "ledger": ledger,
        "cascade": cascade,
        "action": action,
    }


def action_for_wb(state: Mapping[str, Any], wb_id: Any) -> Dict[str, Any]:
    """Projiziert die Gruppenaktion genau auf ihren marginalen Besitzer.

    Die Hilfsfunktion erzeugt für alle anderen Wallboxen ausschließlich einen
    Non-Output-Hold. Damit kann ein Manager die eine Gruppenentscheidung auf
    mehrere Wallbox-Kontexte abbilden, ohne den PCC-Wert erneut zu bilanzieren.
    """

    data = state if isinstance(state, Mapping) else {}
    owner = _wb_id(data.get("marginal_wb_id"))
    candidate = _wb_id(wb_id)
    action = data.get("action")
    if candidate == owner and isinstance(action, Mapping):
        return deepcopy(dict(action))
    stage = str((data.get("cascade") or {}).get("stage") or "unknown")
    return _hold_action(candidate, "not_marginal_action_owner", stage=stage)


def step_group_deficit(
    previous_state: Any,
    *,
    snapshot_id: Any,
    sample_ts: Any,
    sample_valid: bool,
    sample_fresh: bool,
    pcc_import_w: Any,
    marginal_wb_id: Any,
    current_amp: Any,
    actual_phases: Any,
    authorized_budget_overrun_w: Any = None,
    authorized_budget_contract_valid: bool = False,
    supports_phase_switch: bool = False,
    prevent_phase_switch: bool = False,
    phase_switch_confirmed: bool = False,
    phase_switch_sequence_active: bool = False,
    phase_switch_failed: bool = False,
    phase_switch_pending_timeout_s: Any = 240.0,
    phase_switch_cooldown_remaining_s: Any = 0.0,
    min_amp: Any = 6.0,
    current_step_amp: Any = 1.0,
    line_voltage_v: Any = 230.0,
    threshold_wh: Any = 200.0,
    leak_w: Any = 100.0,
    tolerance_w: Any = 100.0,
    max_dt_s: Any = 30.0,
) -> Dict[str, Any]:
    """Fortschreiben von genau einem PCC-Defizitkonto und einer Kaskade.

    ``pcc_import_w`` ist positiv für Netzbezug.
    ``authorized_budget_overrun_w`` darf ausschließlich aus einem frischen,
    sitzungsgebundenen und finalen Per-Wallbox-Cap-Vertrag stammen. Der
    Aufrufer bestätigt diesen Quellenvertrag explizit mit
    ``authorized_budget_contract_valid=True``; ein fehlender oder ungültiger
    Vertrag bleibt unbekannt und wird nicht als 0 W interpretiert.

    Netzbezug reduziert oberhalb des Mindeststroms sofort und proportional.
    Die Wh-Schwelle entscheidet über Budget-Abregelung sowie die nächste
    elektromechanische Stufe am Mindeststrom. Ein Phasen-Cooldown blockiert
    ausschließlich ``phase_down``; Stromabsenkung und Stop bleiben davon
    unabhängig.
    """

    previous = _validated_previous(previous_state)
    sid = _snapshot_id(snapshot_id)
    wb_id = _wb_id(marginal_wb_id)
    phases = _phase_count(actual_phases)
    current = _positive(current_amp, name="current_amp", allow_zero=True)
    minimum = _positive(min_amp, name="min_amp")
    step = _positive(current_step_amp, name="current_step_amp")
    voltage = _positive(line_voltage_v, name="line_voltage_v")
    threshold = _positive(threshold_wh, name="threshold_wh")
    leak = _positive(leak_w, name="leak_w", allow_zero=True)
    tolerance = _positive(tolerance_w, name="tolerance_w", allow_zero=True)
    max_dt = _positive(max_dt_s, name="max_dt_s")
    cooldown = _positive(
        phase_switch_cooldown_remaining_s,
        name="phase_switch_cooldown_remaining_s",
        allow_zero=True,
    )
    pending_timeout = _positive(
        phase_switch_pending_timeout_s,
        name="phase_switch_pending_timeout_s",
    )
    if current + 1e-6 < minimum:
        # 0 A ist als physischer Bereitschafts-/Stoppzustand erlaubt. Ein
        # positiver Unterstrom darf hingegen nicht als reguläre Ladestufe
        # interpretiert werden.
        if current > 1e-6:
            raise DeficitControlInputError("positiver current_amp liegt unter min_amp")

    topology = _topology(
        supports_phase_switch=bool(supports_phase_switch),
        prevent_phase_switch=bool(prevent_phase_switch),
        actual_phases=phases,
    )
    physical_stage = _stage_for_physics(phases, current, minimum)
    old_ledger = previous.get("ledger")
    ledger = deepcopy(old_ledger) if isinstance(old_ledger, Mapping) else _empty_ledger()
    old_cascade = previous.get("cascade")
    cascade = (
        deepcopy(old_cascade)
        if isinstance(old_cascade, Mapping)
        else _empty_cascade(wb_id, topology, physical_stage)
    )
    if ledger.get("schema_version") != LEDGER_SCHEMA:
        raise DeficitControlInputError("ledger besitzt ein unbekanntes Schema")
    if cascade.get("schema_version") != CASCADE_SCHEMA:
        raise DeficitControlInputError("cascade besitzt ein unbekanntes Schema")

    previous_sid = str(ledger.get("last_snapshot_id") or "")
    previous_owner = int(cascade.get("marginal_wb_id", wb_id) or 0)
    if sid == previous_sid:
        reason = (
            "duplicate_snapshot_owner_mismatch"
            if previous_owner != wb_id
            else "duplicate_snapshot"
        )
        action = _hold_action(
            wb_id,
            reason,
            stage=str(cascade.get("stage") or physical_stage),
            fail_closed=previous_owner != wb_id,
        )
        return _result(
            snapshot_id=sid,
            sample_ts=_finite(sample_ts),
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=action,
            duplicate_snapshot=True,
        )

    timestamp = _finite(sample_ts)
    last_timestamp = _finite(ledger.get("last_sample_ts"))
    timestamp_monotonic = bool(
        timestamp is not None
        and (last_timestamp is None or timestamp > last_timestamp)
    )
    pcc_value = _finite(pcc_import_w)
    valid = bool(sample_valid and sample_fresh and timestamp_monotonic and pcc_value is not None)
    if not valid:
        # Ungültige oder veraltete Messwerte werden nie in eine echte Null
        # umgedeutet. Konto und letzter gültiger Zeitstempel bleiben stehen.
        ledger.update({
            "last_snapshot_id": sid,
            "grid_deficit_w": None,
            "authorized_budget_overrun_w": None,
            "counted_total_w": None,
            "counted_total_complete": False,
            "authorized_budget_contract_valid": False,
            "sample_valid": bool(sample_valid and timestamp_monotonic and pcc_value is not None),
            "sample_fresh": bool(sample_fresh),
            "dt_s": 0.0,
            "dt_clamped": False,
            "threshold_reached": bool(float(ledger.get("bucket_wh", 0.0) or 0.0) >= threshold),
            "reason": (
                "sample_stale"
                if not sample_fresh
                else "sample_timestamp_not_monotonic"
                if not timestamp_monotonic
                else "sample_invalid"
            ),
        })
        cascade.update({
            "marginal_wb_id": wb_id,
            "topology": topology,
            "reason": "sample_invalid_fail_closed",
        })
        action = _hold_action(
            wb_id,
            str(ledger["reason"]),
            stage=str(cascade.get("stage") or physical_stage),
            fail_closed=True,
        )
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=action,
        )

    budget_overrun = _finite(authorized_budget_overrun_w)
    budget_contract_valid = bool(
        authorized_budget_contract_valid is True
        and budget_overrun is not None
        and budget_overrun >= 0.0
    )
    grid_deficit = max(0.0, float(pcc_value) - tolerance)
    authorized_budget_overrun: Optional[float] = None
    if budget_contract_valid:
        # Der Quellenvertrag liefert bereits exakt
        # ``Σ max(0, measured_i - final_cap_i)``. Eine zweite Toleranz würde
        # diesen versiegelten Wert verfälschen; ``tolerance_w`` gehört nur zur
        # PCC-Netzimportkante.
        authorized_budget_overrun = max(0.0, float(budget_overrun))
    # Netzbezug besitzt die höhere Schutzpriorität. Bei gleichzeitigem
    # Netzbezug und belegter Budgetüberziehung bleibt letztere diagnostisch
    # sichtbar, wird aber nicht zusätzlich in denselben Wh-Wächter gezählt.
    # Damit kann ein gemeinsamer physischer Mangel niemals zwei Wächter
    # beschleunigen. Erst ohne Netzbezug übernimmt der Budget-Wächter.
    if grid_deficit > 0.0:
        counted_component = "grid"
        counted_total = grid_deficit
    elif authorized_budget_overrun is not None and authorized_budget_overrun > 0.0:
        counted_component = "authorized_budget"
        counted_total = authorized_budget_overrun
    else:
        counted_component = "none"
        counted_total = 0.0

    raw_dt = 0.0 if last_timestamp is None else max(0.0, timestamp - last_timestamp)
    dt_s = min(raw_dt, max_dt)
    # Netz- und Budgetenergie besitzen getrennte Konten. Dadurch kann eine
    # beinahe volle Budgetüberziehung nicht durch einen kleinen späteren
    # Netzimpuls zur Netzabschaltung umgedeutet werden. Der Netzpunkt ist bei
    # jedem gültigen PCC-Snapshot vollständig bekannt und sein Konto darf bei
    # beendetem Netzbezug auch dann leaken, wenn noch keine explizite
    # Budgetzuordnung verfügbar ist. Das Budgetkonto bleibt dagegen bei
    # fehlendem Quellenvertrag unverändert, statt eine unbekannte Größe als Null
    # zu behandeln. Während Netzbezug aktiv ist, pausiert es: der Netz-Wächter
    # hat Vorrang und beide Ursachen werden niemals im selben Zyklus addiert.
    grid_bucket_before = max(
        0.0,
        float(ledger.get("grid_bucket_wh", ledger.get("bucket_wh", 0.0)) or 0.0),
    )
    budget_bucket_before = max(
        0.0,
        float(ledger.get("authorized_budget_bucket_wh", 0.0) or 0.0),
    )
    grid_bucket = max(
        0.0,
        grid_bucket_before + (grid_deficit - leak) * dt_s / 3600.0,
    )
    if grid_deficit > 0.0:
        budget_bucket = budget_bucket_before
        budget_leak_applied_w = 0.0
    elif budget_contract_valid:
        budget_bucket = max(
            0.0,
            budget_bucket_before
            + (float(authorized_budget_overrun or 0.0) - leak) * dt_s / 3600.0,
        )
        budget_leak_applied_w = leak
    else:
        budget_bucket = budget_bucket_before
        budget_leak_applied_w = 0.0

    active_bucket = (
        grid_bucket
        if counted_component == "grid"
        else budget_bucket
        if counted_component == "authorized_budget"
        else max(grid_bucket, budget_bucket)
    )
    active_threshold_bucket = (
        grid_bucket
        if counted_component == "grid"
        else budget_bucket
        if counted_component == "authorized_budget"
        else 0.0
    )
    threshold_reached = active_threshold_bucket + 1e-9 >= threshold
    grid_deficit_out = round(grid_deficit, 6)
    authorized_budget_overrun_out = (
        round(authorized_budget_overrun, 6)
        if authorized_budget_overrun is not None
        else None
    )
    counted_total_out = round(counted_total, 6)
    ledger.update({
        "last_snapshot_id": sid,
        "last_sample_ts": float(timestamp),
        "bucket_wh": round(active_bucket, 9),
        "grid_bucket_wh": round(grid_bucket, 9),
        "authorized_budget_bucket_wh": round(budget_bucket, 9),
        "bucket_component": counted_component,
        "grid_deficit_w": grid_deficit_out,
        "authorized_budget_overrun_w": authorized_budget_overrun_out,
        "counted_component": counted_component,
        "counted_total_w": counted_total_out,
        "counted_total_complete": bool(
            grid_deficit > 0.0 or budget_contract_valid
        ),
        "authorized_budget_contract_valid": bool(budget_contract_valid),
        "sample_valid": True,
        "sample_fresh": True,
        "dt_s": round(dt_s, 6),
        "dt_clamped": bool(raw_dt > max_dt + 1e-9),
        "leak_applied_w": round(
            leak if counted_component == "grid" else budget_leak_applied_w,
            6,
        ),
        "grid_leak_applied_w": round(leak, 6),
        "authorized_budget_leak_applied_w": round(
            budget_leak_applied_w,
            6,
        ),
        "threshold_reached": bool(threshold_reached),
        "reason": (
            "grid_priority"
            if grid_deficit > 0.0
            else "authorized_budget_overrun"
            if authorized_budget_overrun is not None
            and authorized_budget_overrun > 0.0
            else "authorized_budget_contract_missing"
            if not budget_contract_valid
            else "within_tolerance"
        ),
    })

    owner_changed = previous_owner != wb_id
    topology_changed = str(cascade.get("topology") or topology) != topology
    old_stage = str(cascade.get("stage") or physical_stage)
    pending_phase = bool(
        not owner_changed
        and not topology_changed
        and old_stage == STAGE_PHASE_DOWN_PENDING
        and cascade.get("phase_down_requested", False)
    )

    if pending_phase:
        pending_since = _finite(
            cascade.get("phase_down_requested_sample_ts")
        )
        if pending_since is None:
            pending_since = last_timestamp if last_timestamp is not None else timestamp
            cascade["phase_down_requested_sample_ts"] = pending_since
        pending_age_s = max(0.0, float(timestamp) - float(pending_since))
        if phases == 1 and phase_switch_confirmed:
            cascade.update({
                "marginal_wb_id": wb_id,
                "topology": topology,
                "stage": _stage_for_physics(1, current, minimum),
                "generation": int(cascade.get("generation", 0) or 0) + 1,
                "phase_down_requested": False,
                "phase_down_requested_sample_ts": None,
                "reason": "phase_down_confirmed",
            })
            _reset_bucket(ledger, reason="confirmed_phase_down")
            return _result(
                snapshot_id=sid,
                sample_ts=timestamp,
                wb_id=wb_id,
                ledger=ledger,
                cascade=cascade,
                action=_hold_action(
                    wb_id,
                    "phase_down_confirmed_stage_reset",
                    stage=str(cascade["stage"]),
                ),
            )
        if phase_switch_sequence_active:
            cascade.update({
                "marginal_wb_id": wb_id,
                "topology": topology,
                "stage": STAGE_PHASE_DOWN_PENDING,
                "phase_down_requested": True,
                "phase_pending_age_s": round(pending_age_s, 6),
                "reason": "phase_sequence_physically_active",
            })
            action = _hold_action(
                wb_id,
                "phase_sequence_physically_active",
                stage=STAGE_PHASE_DOWN_PENDING,
            )
            action["phase_pending_age_s"] = round(pending_age_s, 6)
            return _result(
                snapshot_id=sid,
                sample_ts=timestamp,
                wb_id=wb_id,
                ledger=ledger,
                cascade=cascade,
                action=action,
            )
        pending_terminal = bool(
            phase_switch_failed or pending_age_s + 1e-9 >= pending_timeout
        )
        if pending_terminal:
            if grid_deficit > 0.0 and current > 1e-6:
                cascade.update({
                    "marginal_wb_id": wb_id,
                    "topology": topology,
                    "stage": STAGE_STOP_PENDING,
                    "generation": int(cascade.get("generation", 0) or 0) + 1,
                    "phase_down_requested": False,
                    "phase_down_requested_sample_ts": None,
                    "phase_pending_age_s": round(pending_age_s, 6),
                    "reason": (
                        "phase_down_failed_grid_stop"
                        if phase_switch_failed
                        else "phase_down_timeout_grid_stop"
                    ),
                })
                _reset_bucket(ledger, reason="phase_pending_grid_stop")
                return _result(
                    snapshot_id=sid,
                    sample_ts=timestamp,
                    wb_id=wb_id,
                    ledger=ledger,
                    cascade=cascade,
                    action=_action(
                        ACTION_STOP,
                        wb_id,
                        str(cascade["reason"]),
                        stage=STAGE_STOP_PENDING,
                        target_amp=0.0,
                    ),
                )
            cascade.update({
                "marginal_wb_id": wb_id,
                "topology": topology,
                "stage": physical_stage,
                "generation": int(cascade.get("generation", 0) or 0) + 1,
                "phase_down_requested": False,
                "phase_down_requested_sample_ts": None,
                "phase_pending_age_s": round(pending_age_s, 6),
                "reason": "phase_pending_ended_without_grid_deficit",
            })
            return _result(
                snapshot_id=sid,
                sample_ts=timestamp,
                wb_id=wb_id,
                ledger=ledger,
                cascade=cascade,
                action=_hold_action(
                    wb_id,
                    "phase_pending_ended_without_grid_deficit",
                    stage=physical_stage,
                ),
            )
        cascade.update({
            "marginal_wb_id": wb_id,
            "topology": topology,
            "stage": STAGE_PHASE_DOWN_PENDING,
            "phase_down_requested": True,
            "phase_pending_age_s": round(pending_age_s, 6),
            "reason": "await_confirmed_one_phase",
        })
        action = _hold_action(
            wb_id,
            "await_confirmed_one_phase",
            stage=STAGE_PHASE_DOWN_PENDING,
            fail_closed=phases == 1 and not phase_switch_confirmed,
        )
        action["phase_pending_age_s"] = round(pending_age_s, 6)
        action["phase_pending_timeout_s"] = round(pending_timeout, 6)
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=action,
        )

    if (
        not owner_changed
        and not topology_changed
        and old_stage == STAGE_STOP_PENDING
    ):
        if current <= 1e-6:
            _reset_bucket(ledger, reason="stop_confirmed")
            cascade["reason"] = "stop_confirmed"
            reason = "stop_confirmed"
        else:
            cascade["reason"] = "await_stop_confirmation"
            reason = "await_stop_confirmation"
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=_hold_action(
                wb_id,
                reason,
                stage=STAGE_STOP_PENDING,
            ),
        )

    if owner_changed or topology_changed:
        cascade.update({
            "marginal_wb_id": wb_id,
            "topology": topology,
            "stage": physical_stage,
            "generation": int(cascade.get("generation", 0) or 0) + 1,
            "phase_down_requested": False,
            "reason": "physical_stage_rebound",
        })
    else:
        cascade.update({
            "marginal_wb_id": wb_id,
            "topology": topology,
            "stage": physical_stage,
            "phase_down_requested": False,
            "reason": "physical_stage_confirmed",
        })

    stage = str(cascade["stage"])
    at_minimum = current <= minimum + 1e-6

    if current <= 1e-6:
        # Eine stehende Wallbox kann nicht marginaler Leistungssteller sein.
        # Die Komponenten bleiben diagnostisch sichtbar, es entsteht aber
        # weder ein Strom- noch ein Schaltbefehl aus fremdem Hausdefizit.
        _reset_bucket(ledger, reason="marginal_not_offering_current")
        cascade["reason"] = "marginal_not_offering_current"
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=_hold_action(
                wb_id,
                "marginal_not_offering_current",
                stage=stage,
                fail_closed=True,
            ),
        )

    if grid_deficit > 0.0 and current > minimum + 1e-6:
        watts_per_amp = voltage * phases
        proportional_drop = _round_up_to_step(grid_deficit / watts_per_amp, step)
        target = max(minimum, _round_down_to_step(current - proportional_drop, step))
        if target >= current - 1e-6:
            target = max(minimum, _round_down_to_step(current - step, step))
        next_stage = _stage_for_physics(phases, target, minimum)
        cascade.update({
            "stage": next_stage,
            "generation": int(cascade.get("generation", 0) or 0) + 1,
            "reason": "grid_import_immediate_current_down",
        })
        _reset_bucket(ledger, reason="grid_current_down")
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=_action(
                ACTION_CURRENT_DOWN,
                wb_id,
                "grid_import_priority",
                stage=next_stage,
                target_amp=target,
            ),
        )

    if not at_minimum and threshold_reached:
        target = max(minimum, _round_down_to_step(current - step, step))
        next_stage = _stage_for_physics(phases, target, minimum)
        cascade.update({
            "stage": next_stage,
            "generation": int(cascade.get("generation", 0) or 0) + 1,
            "reason": "authorized_budget_energy_current_down",
        })
        _reset_bucket(ledger, reason="authorized_budget_current_down")
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=_action(
                ACTION_CURRENT_DOWN,
                wb_id,
                "authorized_budget_overrun_threshold",
                stage=next_stage,
                target_amp=target,
            ),
        )

    if at_minimum and threshold_reached:
        if phases == 3 and topology == TOPOLOGY_SWITCHABLE:
            if phase_switch_sequence_active:
                cascade.update({
                    "stage": STAGE_PHASE_DOWN_PENDING,
                    "generation": int(cascade.get("generation", 0) or 0) + 1,
                    "phase_down_requested": True,
                    "phase_down_requested_sample_ts": float(timestamp),
                    "phase_pending_age_s": 0.0,
                    "reason": "existing_phase_sequence_observed",
                })
                return _result(
                    snapshot_id=sid,
                    sample_ts=timestamp,
                    wb_id=wb_id,
                    ledger=ledger,
                    cascade=cascade,
                    action=_hold_action(
                        wb_id,
                        "existing_phase_sequence_observed",
                        stage=STAGE_PHASE_DOWN_PENDING,
                    ),
                )
            if cooldown > 0.0:
                # Der 480-s-Schutz gilt ausschließlich dem nächsten
                # Phasenkommando. Bei weiter bestätigtem Defizit am
                # 3p-Minimum bleibt nur der typisierte finale Stop; die
                # Cooldownzeit darf diese Safety-Kante nicht sperren.
                cascade.update({
                    "stage": STAGE_STOP_PENDING,
                    "generation": int(cascade.get("generation", 0) or 0) + 1,
                    "phase_down_requested": False,
                    "phase_down_requested_sample_ts": None,
                    "reason": "phase_cooldown_minimum_grid_stop",
                })
                _reset_bucket(ledger, reason="phase_cooldown_grid_stop")
                action = _action(
                    ACTION_STOP,
                    wb_id,
                    "phase_cooldown_minimum_grid_stop",
                    stage=STAGE_STOP_PENDING,
                    target_amp=0.0,
                )
                action["phase_switch_cooldown_remaining_s"] = round(
                    cooldown,
                    3,
                )
                return _result(
                    snapshot_id=sid,
                    sample_ts=timestamp,
                    wb_id=wb_id,
                    ledger=ledger,
                    cascade=cascade,
                    action=action,
                )
            cascade.update({
                "stage": STAGE_PHASE_DOWN_PENDING,
                "generation": int(cascade.get("generation", 0) or 0) + 1,
                "phase_down_requested": True,
                "phase_down_requested_sample_ts": float(timestamp),
                "phase_pending_age_s": 0.0,
                "reason": "three_phase_minimum_energy_reached",
            })
            return _result(
                snapshot_id=sid,
                sample_ts=timestamp,
                wb_id=wb_id,
                ledger=ledger,
                cascade=cascade,
                action=_action(
                    ACTION_PHASE_DOWN,
                    wb_id,
                    "three_phase_minimum_energy_reached",
                    stage=STAGE_PHASE_DOWN_PENDING,
                    target_phases=1,
                ),
            )

        # Bei 1p sowie bei fester oder gesperrter 3p-Hardware ist der Stop die
        # nächste und einzige verbleibende Kaskadenstufe. Ein Phasenkommando
        # wird in diesen Topologien niemals erzeugt.
        cascade.update({
            "stage": STAGE_STOP_PENDING,
            "generation": int(cascade.get("generation", 0) or 0) + 1,
            "phase_down_requested": False,
            "reason": "minimum_current_energy_reached",
        })
        _reset_bucket(ledger, reason="stop_requested")
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=_action(
                ACTION_STOP,
                wb_id,
                "minimum_current_energy_reached",
                stage=STAGE_STOP_PENDING,
                target_amp=0.0,
            ),
        )

    return _result(
        snapshot_id=sid,
        sample_ts=timestamp,
        wb_id=wb_id,
        ledger=ledger,
        cascade=cascade,
        action=_hold_action(
            wb_id,
            "deficit_energy_wait" if counted_total > 0.0 else str(ledger["reason"]),
            stage=stage,
        ),
    )


__all__ = [
    "ACTION_CURRENT_DOWN",
    "ACTION_HOLD",
    "ACTION_PHASE_DOWN",
    "ACTION_STOP",
    "DeficitControlInputError",
    "STATE_SCHEMA",
    "STAGE_ONE_PHASE_CURRENT",
    "STAGE_ONE_PHASE_MIN_WATCH",
    "STAGE_PHASE_DOWN_PENDING",
    "STAGE_STOP_PENDING",
    "STAGE_THREE_PHASE_CURRENT",
    "STAGE_THREE_PHASE_MIN_WATCH",
    "TOPOLOGY_FIXED_ONE",
    "TOPOLOGY_FIXED_THREE",
    "TOPOLOGY_SWITCHABLE",
    "action_for_wb",
    "step_group_deficit",
]
