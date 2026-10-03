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
        "battery_bucket_wh": 0.0,
        "bucket_component": "none",
        "stage_generation": 0,
        "grid_deficit_w": None,
        "authorized_budget_overrun_w": None,
        "battery_support_w": None,
        "battery_threshold_wh": None,
        "battery_contract_valid": False,
        "counted_component": "none",
        "counted_total_w": None,
        "counted_total_complete": False,
        "authorized_budget_contract_valid": False,
        # Ruhezustand des Budgetkontos bei Export.
        "export_rest_since_ts": None,
        "export_rest_active": False,
        "export_rest_contract_valid": False,
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
    # Stand vor der Aktion bewahren
    # (Journalzeile '%.0f/%.0f Wh'); das Akku-Konto wird mit zurueckgesetzt.
    ledger["last_action_bucket_wh"] = round(
        float(ledger.get("bucket_wh", 0.0) or 0.0),
        3,
    )
    ledger["bucket_wh"] = 0.0
    ledger["grid_bucket_wh"] = 0.0
    ledger["battery_bucket_wh"] = 0.0
    ledger["authorized_budget_bucket_wh"] = 0.0
    ledger["bucket_component"] = "none"
    ledger["threshold_reached"] = False
    ledger["stage_generation"] = int(ledger.get("stage_generation", 0) or 0) + 1
    ledger["bucket_reset_reason"] = str(reason)


# Die Untergrenzen-Episode wird je Wallbox geführt, unabhängig davon, welche
# Wallbox gerade Aktionsbesitzer ist: Beginn der Unterdeckung (Eintritts-
# bestätigung), bestätigte Episode und Anforderungszeitpunkt der Absenkung (je
# Wallbox und Topologie). Die übrigen Schlüssel spiegeln nur den aktuellen
# Aktionsbesitzer für Diagnose und Journal.
_FLOOR_DIRECT_SINCE_KEY = "floor_direct_minimum_pending_since_by_wb"
_FLOOR_DIRECT_CONFIRMED_KEY = "floor_direct_minimum_confirmed_by_wb"
_FLOOR_DIRECT_REQUESTS_KEY = "floor_direct_minimum_requested_by_wb"
_FLOOR_DIRECT_BOX_KEYS = (
    _FLOOR_DIRECT_SINCE_KEY,
    _FLOOR_DIRECT_CONFIRMED_KEY,
    _FLOOR_DIRECT_REQUESTS_KEY,
)
_FLOOR_DIRECT_OWNER_KEYS = (
    "floor_direct_minimum",
    "floor_direct_minimum_pending",
    "floor_direct_minimum_pending_since_ts",
    "floor_direct_minimum_requested_ts",
)


def _floor_direct_map(cascade: Mapping[str, Any], key: str) -> Dict[str, Any]:
    values = cascade.get(key)
    return dict(values) if isinstance(values, Mapping) else {}


def _store_floor_direct_map(
    cascade: Dict[str, Any],
    key: str,
    values: Mapping[str, Any],
) -> None:
    if values:
        cascade[key] = dict(values)
    else:
        cascade.pop(key, None)


def _forget_floor_direct_box(cascade: Dict[str, Any], wb_id: int) -> None:
    """Beendet die Untergrenzen-Episode genau dieser Wallbox."""

    for key in _FLOOR_DIRECT_BOX_KEYS:
        values = _floor_direct_map(cascade, key)
        values.pop(str(wb_id), None)
        _store_floor_direct_map(cascade, key, values)
    for key in _FLOOR_DIRECT_OWNER_KEYS:
        cascade.pop(key, None)


def _sync_floor_direct_candidates(
    cascade: Dict[str, Any],
    immediate_by_wb: Any,
    timestamp: float,
) -> None:
    """Führt die Episoden aller Wallboxen mit Direktabsenkung in diesem Zyklus.

    ``immediate_by_wb`` nennt jede Wallbox, deren Untergrenzen-Vertrag in
    diesem Zyklus gilt, mit ihrem Eintritt ohne Bestätigung. Eine Wallbox, die
    fehlt, verlässt ihre Episode samt Zeitstempeln; eine neue beginnt ihre
    Eintrittsbestätigung jetzt.
    """

    active: Dict[str, bool] = {}
    reported = immediate_by_wb if isinstance(immediate_by_wb, Mapping) else {}
    for key, immediate in reported.items():
        try:
            candidate_id = _wb_id(key)
        except DeficitControlInputError:
            continue
        active[str(candidate_id)] = bool(immediate is True)
    since = {
        key: value
        for key, value in _floor_direct_map(cascade, _FLOOR_DIRECT_SINCE_KEY).items()
        if key in active and _finite(value) is not None and float(value) <= timestamp
    }
    confirmed = {
        key: True
        for key, value in _floor_direct_map(cascade, _FLOOR_DIRECT_CONFIRMED_KEY).items()
        if key in active and value is True
    }
    requests = {
        key: value
        for key, value in _floor_direct_map(cascade, _FLOOR_DIRECT_REQUESTS_KEY).items()
        if key in active
    }
    for key, immediate in active.items():
        since.setdefault(key, float(timestamp))
        if immediate:
            confirmed[key] = True
    _store_floor_direct_map(cascade, _FLOOR_DIRECT_SINCE_KEY, since)
    _store_floor_direct_map(cascade, _FLOOR_DIRECT_CONFIRMED_KEY, confirmed)
    _store_floor_direct_map(cascade, _FLOOR_DIRECT_REQUESTS_KEY, requests)


def _floor_direct_request_ts(
    cascade: Mapping[str, Any],
    wb_id: int,
    topology: str,
) -> Optional[float]:
    """Anforderungszeitpunkt der Absenkung genau dieser Wallbox und Topologie."""

    entry = _floor_direct_map(cascade, _FLOOR_DIRECT_REQUESTS_KEY).get(str(wb_id))
    if not isinstance(entry, Mapping) or str(entry.get("topology") or "") != topology:
        return None
    return _finite(entry.get("ts"))


def _set_floor_direct_request(
    cascade: Dict[str, Any],
    wb_id: int,
    topology: str,
    timestamp: float,
) -> None:
    requests = _floor_direct_map(cascade, _FLOOR_DIRECT_REQUESTS_KEY)
    requests[str(wb_id)] = {"ts": float(timestamp), "topology": str(topology)}
    _store_floor_direct_map(cascade, _FLOOR_DIRECT_REQUESTS_KEY, requests)
    cascade["floor_direct_minimum_requested_ts"] = float(timestamp)


def _forget_floor_direct_request(cascade: Dict[str, Any], wb_id: int) -> None:
    requests = _floor_direct_map(cascade, _FLOOR_DIRECT_REQUESTS_KEY)
    requests.pop(str(wb_id), None)
    _store_floor_direct_map(cascade, _FLOOR_DIRECT_REQUESTS_KEY, requests)
    cascade.pop("floor_direct_minimum_requested_ts", None)


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
    if str(action.get("type") or "") == ACTION_STOP:
        # Ein Stop beendet die Untergrenzen-Episode der gestoppten Wallbox;
        # ihre Bestätigung und Anforderung gelten nie für eine spätere Ladung.
        _forget_floor_direct_box(cascade, wb_id)
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
    measured_subminimum_charge: bool = False,
    authorized_budget_overrun_w: Any = None,
    authorized_budget_contract_valid: bool = False,
    battery_support_w: Any = None,
    battery_contract_valid: bool = False,
    battery_threshold_wh: Any = None,
    curve_floor_end: Optional[Mapping[str, Any]] = None,
    export_rest_contract_valid: bool = False,
    battery_not_discharging: bool = False,
    export_margin_w: Any = 200.0,
    export_rest_min_s: Any = 30.0,
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
    floor_direct_minimum: bool = False,
    floor_direct_minimum_confirm_s: Any = 30.0,
    floor_direct_minimum_immediate: bool = True,
    floor_direct_minimum_entry_confirm_s: Any = 20.0,
    floor_direct_minimum_phase_down: bool = False,
    floor_direct_minimum_immediate_by_wb: Optional[Mapping[Any, Any]] = None,
    grid_import_settle_s: Any = 0.0,
    grid_import_settle_max_w: Any = None,
    grid_import_settle_bypass_reason: str = "",
    grid_import_episode: Optional[Mapping[str, Any]] = None,
) -> Dict[str, Any]:
    """Fortschreiben von genau einem PCC-Defizitkonto und einer Kaskade.

    ``pcc_import_w`` ist positiv für Netzbezug.
    ``authorized_budget_overrun_w`` darf ausschließlich aus einem frischen,
    sitzungsgebundenen und finalen Per-Wallbox-Cap-Vertrag stammen. Der
    Aufrufer bestätigt diesen Quellenvertrag explizit mit
    ``authorized_budget_contract_valid=True``; ein fehlender oder ungültiger
    Vertrag bleibt unbekannt und wird nicht als 0 W interpretiert.

    Eine explizit bestätigte reale Ladung mit einer nur aus Leistung
    abgeleiteten Stromuntergrenze unter dem Mindeststrom bleibt als
    ``measured_subminimum_charge`` im Wh-Konto. Sie erlaubt ausschließlich
    Beobachten und den energiebasierten Stop, keinen positiven Ausgang.

    Netzbezug reduziert oberhalb des Mindeststroms sofort und proportional.
    Die Wh-Schwelle entscheidet über Budget-Abregelung sowie die nächste
    elektromechanische Stufe am Mindeststrom. Ein Phasen-Cooldown blockiert
    ausschließlich ``phase_down``; Stromabsenkung und Stop bleiben davon
    unabhängig.

    Das Budgetkonto ruht bei Export. Seine Zählgröße hängt an der
    Zuteilung, nicht am Netzpunkt – bei sinkender Zuteilung und nachlaufender
    Ist-Leistung zählt es ohne physikalisches Defizit. Es zählt daher nur, wenn
    NICHT gilt: Rohnetz <= −``export_margin_w`` UND Akku lädt oder ruht
    (``battery_not_discharging``), beides stabil >= ``export_rest_min_s``. Der
    Aufrufer bestätigt die Quelle explizit mit ``export_rest_contract_valid``;
    ohne Bestätigung ruht nichts und es wird gezählt wie ohne diese Regel.
    Die Rangfolge Netz > Akku > Budget bleibt unverändert.

    wbminSoC-Untergrenze (``floor_direct_minimum``): Der Aufrufer bestätigt,
    dass die marginale Wallbox in einem Modus mit Akkuladen bis zur
    Untergrenze an der geschlossenen wbminSoC-Untergrenze lädt und ihr
    batterieneutrales PV-Budget die Mindestleistung der aktuellen Phasenzahl
    nicht trägt. Liegt dabei noch PV an (``floor_direct_minimum_immediate``
    False), muss diese Unterdeckung erst
    ``floor_direct_minimum_entry_confirm_s`` ununterbrochen anstehen
    (Anti-Flattern bei Wolkenkanten); bis dahin gilt die bisherige Kaskade,
    nur die vom Budgetkonto ausgelöste Strom- und Phasenstufe wartet, das
    Konto zählt weiter. Ohne PV oder ohne bekanntes PV-Budget gilt die
    Episode sofort. In
    der Episode senkt die Kaskade oberhalb des Mindeststroms in einem Schritt
    auf den Mindeststrom der aktuellen Phasenzahl ab – ohne Kontoreset, auch
    bei Netzbezug, unabhängig vom Kontostand. Am Mindeststrom läuft das
    vorhandene Konto genau einmal bis zur Schwelle weiter (kein Neustart).
    Danach wechselt eine dreiphasig ladende, schaltbare Wallbox auf 1p, wenn
    der Aufrufer ``floor_direct_minimum_phase_down`` bestätigt (ihr PV-Budget
    trägt das 1p-Minimum und der Phasenausgang ist vorhanden); sonst folgt
    der Stop ohne Phasenabstieg. Bleibt die Bestätigung eines solchen
    Phasenwechsels aus, endet die Episode im Stop statt in einem erneuten
    Phasenauftrag. Ein Stop oberhalb des Mindeststroms ist nur die
    fail-closed-Kante für eine Wallbox, die der Absenkung nicht folgt: Die
    Absenkung wurde für genau diese Wallbox in ihrer Topologie vor mindestens
    ``floor_direct_minimum_confirm_s`` angefordert, der Strom liegt weiter
    darüber und das Konto hat die Schwelle erreicht. Ohne gezählte Komponente
    bleibt es am Mindeststrom beim Halten; fehlende Daten lösen keinen Stop
    aus.

    Die Episode – Eintrittsbestätigung, bestätigte Episode und
    Anforderungszeitpunkt – wird je Wallbox geführt und hängt nicht daran,
    welche Wallbox gerade Aktionsbesitzer ist; ein Besitzerwechsel startet
    nichts neu und überträgt nichts. ``floor_direct_minimum_immediate_by_wb``
    nennt dazu in jedem Zyklus alle Wallboxen mit geltender Direktabsenkung
    und ihren Eintritt ohne Bestätigung; eine dort fehlende Wallbox verlässt
    ihre Episode. Ohne diese Angabe (``None``) wird nur die Episode des
    aktuellen Besitzers fortgeschrieben. Ein Besitzer ohne Direktabsenkung,
    ein Stop und eine stehende Wallbox beenden die Episode dieser Wallbox
    samt Zeitstempeln.

    Einschwingfrist (``grid_import_settle_s``, 0 = aus): Neuer Netzbezug –
    nach einer eigenen Anhebung oder bei einer kurzen Lastspitze – wird vom
    Speicher erst nach seiner Reaktionszeit ausgeglichen. Die sofortige
    proportionale Absenkung wartet deshalb, bis der Bezug seit Beginn der
    Bezugsepisode ``grid_import_settle_s`` ansteht. Die Episode endet erst,
    wenn ebenso lange kein Bezug mehr gemessen wurde; kurze Pausen verlängern
    die Frist nicht. Das Netzkonto zählt währenddessen weiter; erreicht es
    seine Schwelle, endet die Frist sofort. Ohne bekannte Speicherreichweite
    (``grid_import_settle_max_w`` fehlt), bei Bezug darüber oder an einer
    harten Grenze (``grid_import_settle_bypass_reason``, etwa Hausanschluss)
    gilt keine Frist. Die Frist wirkt nur oberhalb des Mindeststroms; Stufen
    am Mindeststrom folgen wie bisher dem Wh-Konto. Die Bezugsepisode ist eine
    Gruppengröße am Netzpunkt: Beginnt die Kaskade neu (leeres Konto), führt
    ``grid_import_episode`` (``since_ts``, ``quiet_since_ts``, ``updated_ts``)
    eine laufende Episode fort, sofern zwischen ihrer letzten Fortschreibung
    und diesem Sample höchstens ``max(max_dt_s, grid_import_settle_s)``
    liegen; ein anhaltender Bezug erhält so keine neue Frist.
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
    direct_candidate = bool(floor_direct_minimum is True)
    direct_confirm_s = _positive(
        floor_direct_minimum_confirm_s,
        name="floor_direct_minimum_confirm_s",
        allow_zero=True,
    )
    direct_entry_confirm_s = _positive(
        floor_direct_minimum_entry_confirm_s,
        name="floor_direct_minimum_entry_confirm_s",
        allow_zero=True,
    )
    subminimum_watch = bool(
        measured_subminimum_charge is True and 0.0 < current < minimum
    )
    if current + 1e-6 < minimum and not subminimum_watch:
        # 0 A ist als physischer Bereitschafts-/Stoppzustand erlaubt. Ein
        # positiver Unterstrom darf hingegen nicht als reguläre Ladestufe
        # interpretiert werden.
        if current > 1e-6:
            raise DeficitControlInputError("positiver current_amp liegt unter min_amp")

    topology = _topology(
        supports_phase_switch=bool(supports_phase_switch) and not subminimum_watch,
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
            "battery_support_w": None,
            "battery_contract_valid": False,
            "counted_total_w": None,
            "counted_total_complete": False,
            "authorized_budget_contract_valid": False,
            # Die 30-s-Stabilität des Ruhezustands darf eine
            # Datenlücke nicht überleben. Bliebe der Zeitstempel über die Lücke
            # stehen, wäre "stabil >= 30 s" beim ersten wieder gültigen
            # Exportframe sofort erfüllt – ohne frisch belegte Stabilität. Der
            # Ruhezustand wird nach jeder Lücke neu verdient (fail-closed wie
            # die übrigen Felder in diesem Zweig).
            "export_rest_since_ts": None,
            "export_rest_active": False,
            "export_rest_contract_valid": False,
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
    # Einschwingfrist der sofortigen Absenkung: Beginn der Bezugsepisode und
    # Beginn der Ruhe danach. Die Episode endet erst nach einer vollen Frist
    # ohne Bezug, damit wiederholte kurze Spitzen die Frist nicht neu starten.
    settle_s = _positive(
        grid_import_settle_s,
        name="grid_import_settle_s",
        allow_zero=True,
    )
    settle_max = _finite(grid_import_settle_max_w)
    import_since = _finite(ledger.get("grid_import_since_ts"))
    import_quiet_since = _finite(ledger.get("grid_import_quiet_since_ts"))
    carried = grid_import_episode if isinstance(grid_import_episode, Mapping) else {}
    carried_updated = _finite(carried.get("updated_ts"))
    if (
        "grid_import_since_ts" not in ledger
        and carried_updated is not None
        and 0.0 <= float(timestamp) - carried_updated <= max(max_dt, settle_s) + 1e-9
    ):
        # Neu begonnene Kaskade: laufende Bezugsepisode fortführen.
        import_since = _finite(carried.get("since_ts"))
        import_quiet_since = _finite(carried.get("quiet_since_ts"))
    if grid_deficit > 0.0:
        import_quiet_since = None
        if import_since is None or import_since > float(timestamp):
            import_since = float(timestamp)
    elif import_since is not None:
        if import_quiet_since is None or import_quiet_since > float(timestamp):
            import_quiet_since = float(timestamp)
        if float(timestamp) - import_quiet_since + 1e-9 >= settle_s:
            import_since = None
            import_quiet_since = None
    import_age_s = (
        float(timestamp) - import_since if import_since is not None else None
    )
    if settle_s <= 0.0:
        settle_blocker = "disabled"
    elif str(grid_import_settle_bypass_reason or "").strip():
        settle_blocker = str(grid_import_settle_bypass_reason).strip()
    elif settle_max is None or settle_max <= 0.0:
        settle_blocker = "storage_reach_unknown"
    elif float(pcc_value) > settle_max:
        settle_blocker = "import_above_storage_reach"
    else:
        settle_blocker = ""
    settle_wait = bool(
        not settle_blocker
        and grid_deficit > 0.0
        and import_age_s is not None
        and import_age_s + 1e-9 < settle_s
    )
    # Ruhezustand des Budgetkontos.
    # Ein Export am Netzpunkt bei ladendem oder ruhendem Speicher ist der
    # physikalische Gegenbeleg zu einer Budgetüberziehung: es fehlt nichts.
    # Die Stabilität liegt im Ledger (``export_rest_since_ts``); ein einzelner
    # Frame ohne Bedingung löscht sie wieder. Ohne bestätigte Quelle
    # (``export_rest_contract_valid``) ruht nie etwas.
    export_margin = _positive(export_margin_w, name="export_margin_w", allow_zero=True)
    export_rest_min = _positive(
        export_rest_min_s,
        name="export_rest_min_s",
        allow_zero=True,
    )
    export_rest_eligible = bool(
        export_rest_contract_valid is True
        and battery_not_discharging is True
        and float(pcc_value) <= -export_margin
    )
    export_rest_since = _finite(ledger.get("export_rest_since_ts"))
    if not export_rest_eligible:
        export_rest_since = None
    elif export_rest_since is None:
        export_rest_since = float(timestamp)
    budget_rest = bool(
        export_rest_since is not None
        and float(timestamp) - float(export_rest_since) >= export_rest_min
    )
    # Drittes Konto – Akku-Stuetzung der
    # marginalen Wallbox ausserhalb der Floor-Modi. Der Aufrufer liefert die
    # Nettogroesse min(Entladung, WB-Ist - PV-Rest) - Toleranz und bestaetigt
    # Live-/PV-Daten explizit; ohne Vertrag bleibt das Konto unbekannt und
    # eingefroren, nie 0 W. Schwelle ist das Kurven-Kontingent, nicht die
    # Netz-Wh-Schwelle.
    floor_end = curve_floor_end if isinstance(curve_floor_end, Mapping) else {}
    floor_applicable = floor_end.get("applicable") is True
    floor_exhausted = bool(floor_applicable and floor_end.get("exhausted") is True)
    end_ts = _finite(floor_end.get("end_ts"))
    floor_event = bool(floor_exhausted and floor_end.get("handled") is not True
                       and end_ts is not None
                       and cascade.get("curve_floor_end_handled_ts") != end_ts)
    battery_value = _finite(battery_support_w)
    battery_threshold_value = _finite(battery_threshold_wh)
    battery_contract = bool(
        battery_contract_valid is True
        and not (floor_event or (floor_applicable and floor_end.get("bridge_active") is True))
        and battery_value is not None
        and battery_value >= 0.0
        and battery_threshold_value is not None
        and battery_threshold_value > 0.0
    )
    battery_support: Optional[float] = (
        max(0.0, float(battery_value)) if battery_contract else None
    )
    battery_threshold = (
        float(battery_threshold_value) if battery_contract else threshold
    )
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
    # Totzone geschlossen –
    # Netz hat nur Vorrang, wenn sein Konto wirklich akkumuliert
    # (Defizit > Leck). Im Band Toleranz < Import <= Toleranz + Leck wuerde
    # 'grid' sonst das Akku-Konto pausieren, ohne selbst zu wachsen
    # (Akku 1000 W + 250 W Import -> nie Stop). Ohne angebotene
    # Akku-Stuetzung bleibt das Netzverhalten unveraendert.
    grid_accumulating = bool(grid_deficit > leak)
    battery_offered = bool(battery_support is not None and battery_support > 0.0)
    # Rang nach erwarteter Zeit bis
    # zur Schwelle statt fester Kante Toleranz + Leck. Netz zaehlt nur, wenn
    # sein Konto die Netzschwelle mindestens so schnell erreicht wie das
    # Akku-Konto sein Kontingent: (Defizit - Leck) / Schwelle >=
    # (Stuetzung - Leck) / Kontingent. Sonst zaehlt der Akku (Netzkonto
    # pausiert, s. u.). Ohne angebotene Akku-Stuetzung bleibt das
    # Netzverhalten unveraendert (Netz zaehlt bei jedem Defizit > 0).
    grid_rate = (grid_deficit - leak) / threshold
    battery_rate = (
        (float(battery_support or 0.0) - leak) / battery_threshold
        if battery_offered
        else None
    )
    grid_outranks = bool(
        grid_deficit > 0.0
        and (battery_rate is None or grid_rate >= battery_rate)
    )
    if grid_outranks:
        counted_component = "grid"
        counted_total = grid_deficit
    elif battery_support is not None and battery_support > 0.0:
        # Akku vor Budgetueberziehung, nach Netz.
        counted_component = "battery"
        counted_total = battery_support
    elif (
        authorized_budget_overrun is not None
        and authorized_budget_overrun > 0.0
        # Ruht das Konto, wird es nicht zur gezählten Komponente;
        # die Kachel folgt automatisch (counted_component 'none').
        and not budget_rest
    ):
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
    # Das Netzkonto pausiert (eingefroren), solange der Akku als
    # schnellere Komponente gezaehlt wird und das Netzkonto sonst wachsen
    # wuerde - nie zwei wachsende Konten je Zyklus. Im Band (Defizit <= Leck)
    # leakt es weiter wie bisher.
    grid_paused = bool(counted_component == "battery" and grid_accumulating)
    grid_bucket = (
        grid_bucket_before
        if grid_paused
        else max(
            0.0,
            grid_bucket_before + (grid_deficit - leak) * dt_s / 3600.0,
        )
    )
    # Akku-Konto mit Netz-Leck und Netz-max_dt (dt_s ist geclamped).
    # Netz hat Vorrang (pausiert); ohne Vertrag bleibt das Konto stehen.
    battery_bucket_before = max(
        0.0,
        float(ledger.get("battery_bucket_wh", 0.0) or 0.0),
    )
    # Pausiert nur, solange das Netzkonto akkumuliert
    # (grid_deficit > leak); im Toleranzband zaehlt bzw. leakt es normal.
    # Pausiert nur, wenn das Netz gezaehlt wird UND akkumuliert;
    # zaehlt der Akku (schneller an seiner Schwelle), laeuft sein Konto weiter.
    if (
        grid_accumulating and counted_component == "grid"
    ) or not battery_contract:
        battery_bucket = battery_bucket_before
        battery_leak_applied_w = 0.0
    else:
        battery_bucket = max(
            0.0,
            battery_bucket_before
            + (float(battery_support or 0.0) - leak) * dt_s / 3600.0,
        )
        battery_leak_applied_w = leak
    # Bei Export haelt das Konto seinen Stand (kein Fuellen, kein
    # Leeren) - wie bei fehlendem Quellenvertrag.
    if grid_deficit > 0.0 or counted_component == "battery" or budget_rest:
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
        else battery_bucket
        if counted_component == "battery"
        else budget_bucket
        if counted_component == "authorized_budget"
        else max(grid_bucket, battery_bucket, budget_bucket)
    )
    active_threshold_bucket = (
        grid_bucket
        if counted_component == "grid"
        else battery_bucket
        if counted_component == "battery"
        else budget_bucket
        if counted_component == "authorized_budget"
        else 0.0
    )
    # Das Akku-Konto hat seine eigene Schwelle (Kurven-Kontingent).
    active_threshold = (
        battery_threshold if counted_component == "battery" else threshold
    )
    threshold_reached = active_threshold_bucket + 1e-9 >= active_threshold
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
        "battery_bucket_wh": round(battery_bucket, 9),
        "battery_support_w": (
            round(battery_support, 6) if battery_support is not None else None
        ),
        "battery_threshold_wh": (
            round(battery_threshold, 6) if battery_contract else None
        ),
        "battery_contract_valid": bool(battery_contract),
        "battery_leak_applied_w": round(battery_leak_applied_w, 6),
        "bucket_component": counted_component,
        "grid_deficit_w": grid_deficit_out,
        "authorized_budget_overrun_w": authorized_budget_overrun_out,
        "counted_component": counted_component,
        "counted_total_w": counted_total_out,
        "counted_total_complete": bool(
            grid_deficit > 0.0 or battery_contract or budget_contract_valid
        ),
        "authorized_budget_contract_valid": bool(budget_contract_valid),
        # Ruhezustand des Budgetkontos bei Export (Diagnose).
        "export_rest_since_ts": export_rest_since,
        "export_rest_active": bool(budget_rest),
        "export_rest_contract_valid": bool(export_rest_contract_valid is True),
        # Einschwingfrist der sofortigen Absenkung (Diagnose).
        "grid_import_since_ts": import_since,
        "grid_import_quiet_since_ts": import_quiet_since,
        "grid_import_settle_s": round(settle_s, 3),
        "grid_import_settle_max_w": (
            round(settle_max, 1) if settle_max is not None else None
        ),
        "grid_import_settle_wait": settle_wait,
        "grid_import_settle_blocker": settle_blocker,
        "grid_import_settle_remaining_s": (
            round(max(0.0, settle_s - import_age_s), 3)
            if settle_wait and import_age_s is not None
            else 0.0
        ),
        "sample_valid": True,
        "sample_fresh": True,
        "dt_s": round(dt_s, 6),
        "dt_clamped": bool(raw_dt > max_dt + 1e-9),
        "leak_applied_w": round(
            leak
            if counted_component == "grid"
            else battery_leak_applied_w
            if counted_component == "battery"
            else budget_leak_applied_w,
            6,
        ),
        # 0 W, solange das Netzkonto hinter dem Akku pausiert.
        "grid_leak_applied_w": round(0.0 if grid_paused else leak, 6),
        "authorized_budget_leak_applied_w": round(
            budget_leak_applied_w,
            6,
        ),
        "threshold_reached": bool(threshold_reached),
        "reason": (
            # Grund folgt der gewaehlten Komponente.
            "grid_priority"
            if counted_component == "grid"
            else "battery_support"
            if counted_component == "battery"
            else "authorized_budget_overrun"
            if authorized_budget_overrun is not None
            and authorized_budget_overrun > 0.0
            else "authorized_budget_contract_missing"
            if not budget_contract_valid
            else "within_tolerance"
        ),
    })

    if floor_applicable and not floor_exhausted:
        # Vor dem Endereignis gehört der Stand zum gemeinsamen Kontingent.
        floor_used = _finite(floor_end.get("used_wh"))
        floor_limit = _finite(floor_end.get("limit_wh"))
        if floor_used is not None and floor_limit is not None and floor_limit > 0:
            ledger.update(battery_bucket_wh=floor_used, battery_threshold_wh=floor_limit)
            if floor_event:
                ledger.update(counted_component="battery", bucket_component="battery", bucket_wh=floor_used,
                              threshold_reached=True, reason="curve_floor_contingent_end")

    if floor_event:
        ledger.update(battery_bucket_wh=0.0,
                      battery_threshold_wh=_finite(floor_end.get("limit_wh")) or battery_threshold,
                      last_action_bucket_wh=_finite(floor_end.get("used_wh")) or 0.0,
                      counted_component="battery",
                      bucket_component="battery", bucket_wh=0.0,
                      threshold_reached=True, reason="curve_floor_contingent_end")

    if floor_direct_minimum_immediate_by_wb is not None:
        # Untergrenzen-Episoden aller Wallboxen in jedem gültigen Zyklus
        # fortschreiben, auch während Phasen- oder Stopbestätigung.
        _sync_floor_direct_candidates(
            cascade,
            floor_direct_minimum_immediate_by_wb,
            float(timestamp),
        )

    owner_changed = previous_owner != wb_id
    topology_changed = str(cascade.get("topology") or topology) != topology
    old_stage = str(cascade.get("stage") or physical_stage)
    pending_phase = bool(
        not owner_changed
        and not topology_changed
        and old_stage == STAGE_PHASE_DOWN_PENDING
        and cascade.get("phase_down_requested", False)
    )

    if pending_phase and floor_event and not phase_switch_sequence_active:
        pending_phase = False

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
            cascade["phase_output_started"] = True
        pending_pv = _finite(floor_end.get("pv_budget_w"))
        deficit_persists = bool(
            grid_deficit > 0.0
            or (counted_component == "battery" and threshold_reached)
            or (float(pcc_value) >= 0.0 and (
                direct_candidate
                or (floor_exhausted and (pending_pv is None
                    or pending_pv < minimum * voltage * phases))
            ))
        )
        no_output_timeout = bool(
            not cascade.get("phase_output_started") and pending_age_s >= 30.0
            and deficit_persists
        )
        if phase_switch_sequence_active and pending_age_s < pending_timeout:
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
        if budget_rest and not phase_switch_failed:
            # Die Defizitepisode ist abgeschlossen: Einspeisung bei ladendem
            # oder ruhendem Akku, stabil über ``export_rest_min_s``. Ein noch
            # nicht angelaufener Phasenabstieg verfällt, statt bis zum
            # Pending-Timeout (oder nach einem Startfenster) bei Einspeisung
            # nachzulaufen. Das Konto beginnt neu; eine zugesagte, aber nicht
            # begonnene Reservierung gibt der Aufrufer frei.
            cascade.update({
                "marginal_wb_id": wb_id,
                "topology": topology,
                "stage": physical_stage,
                "generation": int(cascade.get("generation", 0) or 0) + 1,
                "phase_down_requested": False,
                "phase_down_requested_sample_ts": None,
                "phase_pending_age_s": round(pending_age_s, 6),
                "reason": "phase_pending_ended_export_rest",
            })
            _reset_bucket(ledger, reason="phase_pending_export_rest")
            return _result(
                snapshot_id=sid,
                sample_ts=timestamp,
                wb_id=wb_id,
                ledger=ledger,
                cascade=cascade,
                action=_hold_action(
                    wb_id,
                    "phase_pending_ended_export_rest",
                    stage=physical_stage,
                ),
            )
        if (
            not cascade.get("phase_output_started")
            and not phase_switch_failed
            and pending_age_s >= 30.0
            and not deficit_persists
            and counted_component == "authorized_budget"
        ):
            # Reine Budgetüberziehung rechtfertigt ohne Netzbezug und ohne
            # erschöpftes Akkukonto keinen Stop. Die unbegonnene Reservierung
            # verfällt; vor dem nächsten Wunsch muss das Konto neu durchlaufen.
            reason = "phase_down_no_output_budget_expired"
            cascade.update({
                "marginal_wb_id": wb_id,
                "topology": topology,
                "stage": physical_stage,
                "generation": int(cascade.get("generation", 0) or 0) + 1,
                "phase_down_requested": False,
                "phase_down_requested_sample_ts": None,
                "phase_pending_age_s": round(pending_age_s, 6),
                "reason": reason,
            })
            _reset_bucket(ledger, reason=reason)
            return _result(
                snapshot_id=sid, sample_ts=timestamp, wb_id=wb_id,
                ledger=ledger, cascade=cascade,
                action=_hold_action(wb_id, reason, stage=physical_stage),
            )
        pending_terminal = bool(
            no_output_timeout or phase_switch_failed or pending_age_s + 1e-9 >= pending_timeout
        )
        if pending_terminal:
            # wbminSoC-Untergrenze: Bleibt der Phasenwechsel der Episode
            # unbestätigt, endet sie fail-closed im Stop statt in einem
            # erneuten Phasenauftrag am 3p-Minimum aus dem Akku.
            floor_pending_stop = bool(
                direct_candidate
                and _floor_direct_map(cascade, _FLOOR_DIRECT_CONFIRMED_KEY).get(
                    str(wb_id)
                ) is True
            )
            # Nach gescheitertem Phasenwechsel stoppt auch die
            # Akku-Stuetzung wie der Netzbezug (naechste Kaskadenstufe).
            if (
                grid_deficit > 0.0
                or counted_component == "battery"
                or floor_pending_stop
                or no_output_timeout
                or (floor_applicable and deficit_persists)
            ) and current > 1e-6:
                # Art folgt der gewaehlten Komponente.
                if counted_component == "grid":
                    _pending_kind = "grid"
                elif floor_pending_stop:
                    _pending_kind = "wbminsoc_floor"
                elif no_output_timeout:
                    _pending_kind = (
                        "budget" if counted_component == "authorized_budget" else
                        "battery" if counted_component == "battery" or floor_applicable else "budget"
                    )
                elif counted_component == "battery" or not floor_pending_stop:
                    _pending_kind = "battery"
                else:
                    _pending_kind = "wbminsoc_floor"
                cascade.update({
                    "marginal_wb_id": wb_id,
                    "topology": topology,
                    "stage": STAGE_STOP_PENDING,
                    "generation": int(cascade.get("generation", 0) or 0) + 1,
                    "phase_down_requested": False,
                    "phase_down_requested_sample_ts": None,
                    "phase_pending_age_s": round(pending_age_s, 6),
                    "reason": (
                        "phase_down_no_output_%s_stop" % _pending_kind
                        if no_output_timeout
                        else "phase_down_failed_%s_stop" % _pending_kind
                        if phase_switch_failed
                        else "phase_down_timeout_%s_stop" % _pending_kind
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
            _forget_floor_direct_box(cascade, wb_id)
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

    if floor_event and current > 0.0:
        pv = _finite(floor_end.get("pv_budget_w"))
        carried = bool(pv is not None and pv >= minimum * voltage * phases)
        down_w = _finite(floor_end.get("phase_down_required_w"))
        down = bool(not carried and phases == 3 and topology == TOPOLOGY_SWITCHABLE
                    and floor_end.get("phase_down_surface") is True
                    and cooldown <= 0.0 and pv is not None and down_w is not None
                    and pv >= down_w)
        if carried:
            target = max(minimum, _round_down_to_step(pv / (voltage * phases), step))
            kind = ACTION_CURRENT_DOWN if target < current - 1e-6 else ACTION_HOLD
            next_stage = _stage_for_physics(phases, min(current, target), minimum)
        else:
            target = 0.0
            kind = ACTION_PHASE_DOWN if down else ACTION_STOP
            next_stage = STAGE_PHASE_DOWN_PENDING if down else STAGE_STOP_PENDING
        cascade["curve_floor_end_handled_ts"] = end_ts
        cascade.update(marginal_wb_id=wb_id, topology=topology, stage=next_stage,
                       generation=int(cascade.get("generation", 0) or 0) + 1,
                       phase_down_requested=down,
                       phase_down_requested_sample_ts=float(timestamp) if down else None,
                       phase_output_started=False, reason="curve_floor_contingent_end")
        action = _action(kind, wb_id, "curve_floor_contingent_end", stage=next_stage,
                         **({"target_phases": 1} if down else {"target_amp": target}))
        return _result(snapshot_id=sid, sample_ts=timestamp, wb_id=wb_id,
                       ledger=ledger, cascade=cascade, action=action)

    if owner_changed or topology_changed:
        # Die Untergrenzen-Episoden laufen je Wallbox weiter; ein je Zyklus
        # wechselnder Besitzer hielte sonst die Eintrittsbestätigung endlos
        # offen. Nur die Spiegelwerte des bisherigen Besitzers entfallen; der
        # Anforderungszeitpunkt bleibt an Wallbox und Topologie gebunden und
        # wird nie auf eine andere Wallbox übertragen.
        for key in _FLOOR_DIRECT_OWNER_KEYS:
            cascade.pop(key, None)
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
        _forget_floor_direct_box(cascade, wb_id)
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

    # wbminSoC-Untergrenze ohne tragfähiges PV-Budget: Mindeststrom, danach
    # genau ein Durchlauf des vorhandenen Kontos, dann Stop. Die Absenkung
    # setzt das Konto nicht zurück. Liegt noch PV an, bestätigt eine Haltezeit
    # die Unterdeckung, bevor die Episode gilt; ohne PV gilt sie sofort. Die
    # Haltezeit läuft je Wallbox, auch während eine andere Wallbox
    # Aktionsbesitzer ist.
    direct_floor = False
    owner_key = str(wb_id)
    if not direct_candidate:
        _forget_floor_direct_box(cascade, wb_id)
    else:
        since_by_wb = _floor_direct_map(cascade, _FLOOR_DIRECT_SINCE_KEY)
        pending_since = _finite(since_by_wb.get(owner_key))
        if pending_since is None or pending_since > float(timestamp):
            pending_since = float(timestamp)
            since_by_wb[owner_key] = pending_since
            _store_floor_direct_map(cascade, _FLOOR_DIRECT_SINCE_KEY, since_by_wb)
        confirmed_by_wb = _floor_direct_map(cascade, _FLOOR_DIRECT_CONFIRMED_KEY)
        direct_floor = bool(
            confirmed_by_wb.get(owner_key) is True
            or floor_direct_minimum_immediate is True
            or float(timestamp) - pending_since + 1e-9 >= direct_entry_confirm_s
        )
        if direct_floor:
            confirmed_by_wb[owner_key] = True
            _store_floor_direct_map(cascade, _FLOOR_DIRECT_CONFIRMED_KEY, confirmed_by_wb)
        cascade["floor_direct_minimum"] = direct_floor
        cascade["floor_direct_minimum_pending"] = not direct_floor
        cascade["floor_direct_minimum_pending_since_ts"] = pending_since
    if at_minimum:
        # Die Absenkung ist befolgt; ein späterer Anstieg wäre eine neue
        # Anforderung mit eigener Bestätigungszeit.
        _forget_floor_direct_request(cascade, wb_id)
    energy_stage_step = bool(
        not at_minimum
        or (
            phases == 3
            and topology == TOPOLOGY_SWITCHABLE
            and not phase_switch_sequence_active
        )
    )
    if (
        direct_candidate
        and not direct_floor
        and energy_stage_step
        and grid_deficit <= 0.0
        and threshold_reached
    ):
        # Eintritt noch in Bestätigung: weder Budgetstufe mit Kontoreset noch
        # Phasenabstieg am 3p-Minimum; die gleich darauf geltende Episode
        # übernimmt den Kontostand und entscheidet zwischen 1p und Stop.
        cascade["reason"] = "wbminsoc_floor_direct_minimum_confirming"
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=_hold_action(
                wb_id,
                "wbminsoc_floor_direct_minimum_confirming",
                stage=stage,
            ),
        )
    if direct_floor and not at_minimum:
        # Der Anforderungszeitpunkt gehört genau dieser Wallbox in ihrer
        # Topologie; ein zwischenzeitlicher Besitzerwechsel setzt ihn nicht
        # zurück, damit auch bei wechselndem Besitzer eine nicht folgende
        # Wallbox fail-closed stoppt.
        requested_ts = _floor_direct_request_ts(cascade, wb_id, topology)
        not_following = bool(
            requested_ts is not None
            and float(timestamp) - float(requested_ts) + 1e-9 >= direct_confirm_s
        )
        if not_following and threshold_reached:
            cascade.update({
                "stage": STAGE_STOP_PENDING,
                "generation": int(cascade.get("generation", 0) or 0) + 1,
                "phase_down_requested": False,
                "reason": "wbminsoc_floor_direct_minimum_not_following_stop",
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
        if requested_ts is None:
            _set_floor_direct_request(cascade, wb_id, topology, float(timestamp))
        else:
            cascade["floor_direct_minimum_requested_ts"] = float(requested_ts)
        next_stage = _stage_for_physics(phases, minimum, minimum)
        cascade.update({
            "stage": next_stage,
            "generation": int(cascade.get("generation", 0) or 0) + 1,
            "reason": "wbminsoc_floor_direct_minimum",
        })
        return _result(
            snapshot_id=sid,
            sample_ts=timestamp,
            wb_id=wb_id,
            ledger=ledger,
            cascade=cascade,
            action=_action(
                ACTION_CURRENT_DOWN,
                wb_id,
                "wbminsoc_floor_direct_minimum",
                stage=next_stage,
                target_amp=minimum,
            ),
        )

    if grid_deficit > 0.0 and current > minimum + 1e-6:
        if settle_wait and not threshold_reached:
            # Der Speicher gleicht den neuen Bezug innerhalb seiner
            # Reaktionszeit aus; eine Absenkung jetzt würde nach dem Ausgleich
            # Einspeisung und die nächste Anhebung auslösen (Pendeln).
            cascade["reason"] = "grid_import_settle"
            action = _hold_action(
                wb_id,
                "grid_import_settle",
                stage=stage,
            )
            action["grid_import_settle_remaining_s"] = ledger.get(
                "grid_import_settle_remaining_s"
            )
            return _result(
                snapshot_id=sid,
                sample_ts=timestamp,
                wb_id=wb_id,
                ledger=ledger,
                cascade=cascade,
                action=action,
            )
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
                (
                    "battery_support_threshold"
                    if counted_component == "battery"
                    else "authorized_budget_overrun_threshold"
                ),
                stage=next_stage,
                target_amp=target,
            ),
        )

    if at_minimum and threshold_reached:
        # An der wbminSoC-Untergrenze folgt nach dem Kontodurchlauf ein
        # Phasenabstieg nur, wenn der Aufrufer ihn bestätigt (PV trägt das
        # 1p-Minimum, Phasenausgang vorhanden); sonst direkt Stop. Eine bereits
        # laufende Phasensequenz wird wie bisher abgewartet.
        floor_phase_down = bool(
            direct_floor and floor_direct_minimum_phase_down is True
        )
        if (
            phases == 3
            and topology == TOPOLOGY_SWITCHABLE
            and (
                phase_switch_sequence_active
                or not direct_floor
                or floor_phase_down
            )
        ):
            if phase_switch_sequence_active:
                cascade.update({
                    "stage": STAGE_PHASE_DOWN_PENDING,
                    "generation": int(cascade.get("generation", 0) or 0) + 1,
                    "phase_down_requested": True,
                    "phase_down_requested_sample_ts": float(timestamp),
                    "phase_output_started": bool(phase_switch_sequence_active),
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
                "phase_output_started": bool(phase_switch_sequence_active),
                "phase_pending_age_s": 0.0,
                "reason": (
                    "wbminsoc_floor_direct_minimum_phase_down"
                    if floor_phase_down
                    else "three_phase_minimum_energy_reached"
                ),
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
                    (
                        "battery_support_threshold"
                        if counted_component == "battery"
                        else "three_phase_minimum_energy_reached"
                    ),
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
            "reason": (
                "wbminsoc_floor_direct_minimum_stop"
                if direct_floor
                else "minimum_current_energy_reached"
            ),
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
                (
                    "battery_support_threshold"
                    if counted_component == "battery"
                    else "minimum_current_energy_reached"
                ),
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


def raise_settle_contract(
    *,
    target_amp: Any,
    base_amp: Any,
    running: bool,
    now_ts: Any,
    last_raise_ts: Any,
    settle_s: Any,
    max_step_a: Any = 2.0,
    current_step_amp: Any = 1.0,
    grid_import_w: Any = None,
    import_tolerance_w: Any = 200.0,
    import_episode_active: bool = False,
    live_valid: bool = False,
    bypass: bool = False,
) -> Dict[str, Any]:
    """Anhebung einer laufenden Ladung erst nach Beruhigung, in kleinen Stufen.

    Gegenstück zur Einschwingfrist des Defizitreglers: Eine Anhebung zieht
    zusätzliche Leistung, die der Speicher erst nach seiner Reaktionszeit
    ausgleicht. Die nächste Anhebung folgt deshalb erst, wenn seit der letzten
    ausgeführten Anhebung ``settle_s`` vergangen sind, kein Netzbezug über der
    Toleranz gemessen wird und keine Bezugsepisode des Defizitreglers offen
    ist; sie beträgt höchstens ``max_step_a`` über dem zuletzt gesetzten
    Strom. Fehlen gültige Live-Daten, wird nicht angehoben. Absenkungen, ein
    Start aus dem Stillstand und freigegebene Netz-/Preisfenster (``bypass``)
    bleiben unberührt.
    """

    def _num(value: Any, default: float) -> float:
        number = _finite(value)
        return float(default) if number is None else float(number)

    step = max(0.1, min(16.0, _num(current_step_amp, 1.0)))
    target = max(0.0, _num(target_amp, 0.0))
    base = max(0.0, _num(base_amp, 0.0))
    settle = max(0.0, _num(settle_s, 0.0))
    max_step = max(step, _num(max_step_a, 2.0))
    now = _num(now_ts, 0.0)
    last = _finite(last_raise_ts)
    since_raise_s = (now - float(last)) if last is not None and last > 0.0 else None
    grid = _finite(grid_import_w)
    tolerance = max(0.0, _num(import_tolerance_w, 200.0))
    result: Dict[str, Any] = {
        "contract": "wallbox_raise_settle_v1",
        "target_amp": round(target, 3),
        "base_amp": round(base, 3),
        "applied_amp": round(target, 3),
        "limited": False,
        "reason": "",
        "settle_s": round(settle, 3),
        "max_step_a": round(max_step, 3),
        "since_last_raise_s": (
            round(since_raise_s, 3) if since_raise_s is not None else None
        ),
    }
    if target <= base + step * 0.5:
        result["reason"] = "no_raise"
        return result
    if bypass:
        result["reason"] = "bypass"
        return result
    if settle <= 0.0:
        result["reason"] = "disabled"
        return result
    if not running or base <= 0.0:
        result["reason"] = "not_running"
        return result
    hold_reason = ""
    if not live_valid or grid is None:
        hold_reason = "live_invalid"
    elif grid > tolerance:
        hold_reason = "grid_import"
    elif import_episode_active:
        hold_reason = "import_episode_open"
    elif since_raise_s is not None and since_raise_s + 1e-9 < settle:
        hold_reason = "settle_wait"
    if hold_reason:
        result.update({
            "applied_amp": round(base, 3),
            "limited": True,
            "reason": hold_reason,
        })
        return result
    ceiling = _round_down_to_step(base + max_step, step)
    applied = min(target, ceiling)
    if applied <= base + 1e-9:
        applied = min(target, base + step)
    result.update({
        "applied_amp": round(applied, 3),
        "limited": bool(applied + 1e-9 < target),
        "reason": "raise_step" if applied + 1e-9 < target else "raise",
    })
    return result


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
    "raise_settle_contract",
    "step_group_deficit",
]
