"""Pure openWB Pro session contract.

openWB Pro connect.php is level-triggered: ``ampere`` and ``phasetarget`` are
absolute device state, not E3DC RSCP toggle edges.  This module keeps the
manager-owned start offer, phase settle window and real-charge confirmation
visible and testable without touching drivers, files or network state.
"""

import math

from typing import Any, Dict, Optional

try:
    from Installer import control_time
except ModuleNotFoundError:  # Native Ausführung mit Installer im sys.path
    import control_time  # type: ignore

from .decision import (
    status_charge_truth,
    status_connected,
    status_real_charging,
    status_real_power,
)


STATE_IDLE = "idle"
STATE_OFFERED = "offered"
STATE_STARTING = "starting"
STATE_WAKEUP = "wakeup"
STATE_CHARGING = "charging"
STATE_PHASE_WAIT = "phase_wait"
STATE_STOPPING = "stopping"
STATE_ENDED = "ended"
STATE_START_RETRY = "start_retry"

CONTRACT_NAME = "openwb_pro_connect_php_level_state"
MANAGER_WAKEUP_DISCONNECT_MAX_LAG_S = 30.0
RELEASABLE_ZERO_ANCHOR_REASONS = frozenset({
    "openwb_pro_curve_direct_zero",
    "openwb_pro_curve_direct_main_zero",
    # 0 A nach T_HOLD ohne PV-Budget im Startfenster ist ein
    # lösbarer Anker – Budget derselben Stecksession erlaubt das Wiederangebot.
    "openwb_pro_start_window_zero_budget_off",
    # Beide Gründe sind vorübergehende Policy-Ergebnisse. Ein später wieder
    # positives, ausführbares Budget derselben Stecksession muss den Start
    # erneut erlauben; Nutzer-Aus und Safety-Gates werden separat geprüft.
    "storage_charge_reserve",
    "zero_budget_stop",
    # Beide Stopps sind gruppenweite, vorübergehende Zuteilungsentscheidungen.
    # Sobald derselben Stecksession wieder ein frisches ausführbares Budget
    # gehört und alle Safety-Gates frei sind, dürfen sie keinen dauerhaften
    # 0-A-Anker hinterlassen.
    "min_current_import_integral",
    "priority_secondary_waiting",
    # Ein Prognose-/Prioritätsstopp ist ebenfalls nur eine vorübergehende
    # zentrale Zuteilung. Sobald diese Autorität entfällt und ein frisches,
    # ausführbares Budget derselben Stecksession vorliegt, darf der Nullanker
    # einen Wiederanlauf nicht wie Nutzer-Aus oder einen Safety-Stopp sperren.
    "priority_forced_stop",
    # Dieser Stop schützt ausschließlich den Hausakku unter wbminSoC. Sobald
    # dieselbe zentrale Policy wieder ein positives, ausführbares PV-Budget
    # für dieselbe Stecksession bindet, darf er den nächsten Start nicht als
    # dauerhaften Nutzer-/Safety-Stop behandeln.
    "wbminsoc_floor_pause",
    # Eine im laufenden Betrieb angehobene wbminSoC-Grenze entzieht der
    # Wallbox zunächst ausschließlich die Akku-Unterstützung. Der dadurch
    # gesetzte 0-A-Anker ist kein Nutzer-Aus: Er darf in derselben
    # Stecksession erst mit einem frischen, ausführbaren positiven Budget und
    # hinter allen übrigen Safety-Gates wieder gelöst werden.
    "wbminsoc_runtime_raise_no_pv_budget",
    # Die Pre-Dump-Startqualifizierung ist eine vorübergehende Policy-Kante.
    # Ein frisches positives Budget derselben Stecksession darf den dadurch
    # gesetzten Nullanker wieder lösen; Nutzer-Aus und Safety-Vetos bleiben
    # davon unberührt.
    "predump_wallbox_gate",
    # Der Manager setzt diesen Anker, wenn sein aktueller Sollstrom unter die
    # physische Mindestleistung fällt. Das ist kein Nutzer-Aus und kein
    # Hardwarefehler: Ein frisches positives Budget darf ihn ausschließlich
    # in derselben Stecksession und hinter allen übrigen Safety-Gates lösen.
    "hold_target_below_minimum",
})

PHASE_STAGE_TIMEBASE_KEY = "stage_timebase"


def _phase_stage_timer(
    stage: str,
    duration_s: float,
    previous: Any,
    current_sample: Any,
) -> Optional[Dict[str, Any]]:
    """Fortschreibung einer kurzen Sequenzdauer nur über monotonic Zeit."""

    if not isinstance(current_sample, dict):
        return None
    expected_existing = previous is not None
    old = previous if isinstance(previous, dict) else {}
    if old.get("phase_stage") == stage:
        timer = control_time.evaluate_guard(
            old,
            current_sample,
            minimum_s=0.0,
        )
    else:
        timer = control_time.begin_guard(
            duration_s,
            current_sample,
            minimum_s=0.0,
        )
        if expected_existing:
            timer["fail_closed"] = True
            timer["rearmed"] = True
            timer["reason"] = "phase_stage_timebase_incomplete"
    timer["phase_stage"] = str(stage)
    return timer

# Jeder temporär freigebbare Nullanker gehört exakt zu der Stecksession, in der
# er gesetzt wurde. Ohne beidseitig vorhandene und identische Session-ID bleibt
# auch bei positivem Folgebudget fail-closed; das gilt ausdrücklich ebenso für
# Direktkurven- und wbminSoC-Stopps.
SESSION_BOUND_RELEASABLE_ZERO_ANCHOR_REASONS = (
    RELEASABLE_ZERO_ANCHOR_REASONS
)

_STATE_LABELS = {
    STATE_IDLE: ("Idle", "secondary"),
    STATE_OFFERED: ("Startfreigabe", "warning"),
    STATE_WAKEUP: ("Wake-up", "warning"),
    STATE_STARTING: ("Start wartet", "warning"),
    STATE_CHARGING: ("Lade", "success"),
    STATE_PHASE_WAIT: ("Phasenwechsel", "warning"),
    STATE_STOPPING: ("Stoppt", "warning"),
    STATE_ENDED: ("Ladung beendet", "secondary"),
    STATE_START_RETRY: ("Start abgelehnt – Wiederholung", "warning"),
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip() if value is not None else ""
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, default)))
    except (TypeError, ValueError, OverflowError):
        return int(default)


def start_cp_retry_limit(config: Optional[Dict[str, Any]] = None) -> int:
    """Liefert ausschließlich einen endlichen ganzzahligen CP-Vertrag 1..3.

    Boolesche, nichtendliche, gebrochene oder außerhalb des freigegebenen
    Bereichs liegende Werte sind keine gültige Konfiguration. Sie fallen auf
    den konservativen Produktstandard von drei Wake-up-Versuchen zurück.
    """

    cfg = config if isinstance(config, dict) else {}
    raw_value = cfg.get("wb_openwb_start_cp_retries", 3)
    if isinstance(raw_value, bool):
        return 3
    try:
        numeric_value = float(raw_value)
    except (TypeError, ValueError, OverflowError):
        return 3
    if not math.isfinite(numeric_value) or not numeric_value.is_integer():
        return 3
    retry_limit = int(numeric_value)
    return retry_limit if 1 <= retry_limit <= 3 else 3


def _explicit_inactive(value: Any) -> bool:
    """Akzeptiert nur einen expliziten booleschen/numerischen Inaktivzustand, nie Missingness."""

    if value is False:
        return True
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value) == 0.0
    return False


def _explicit_numeric_bool(value: Any) -> Optional[bool]:
    """Normalisiert ausschließlich boolesche und endliche numerische Evidenz."""

    if isinstance(value, bool):
        return value
    if not isinstance(value, (int, float)):
        return None
    numeric = float(value)
    if not math.isfinite(numeric):
        return None
    return numeric != 0.0


def _current_step(value: Any, default: float = 1.0) -> float:
    step = _safe_float(value, default)
    if step <= 0.11:
        return 0.1
    if step <= 0.51:
        return 0.5
    return 1.0


def _round_to_step(value: float, step: float) -> float:
    step = _current_step(step)
    rounded = round(float(value or 0.0) / step) * step
    return float(int(round(rounded))) if step >= 0.99 else round(rounded, 1)


def _state_label(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[0]


def _state_level(state: str) -> str:
    return _STATE_LABELS.get(state, _STATE_LABELS[STATE_IDLE])[1]


def _valid_phase_count(value: Any, default: int = 0) -> int:
    try:
        phases = int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)
    return phases if phases in (1, 2, 3) else int(default)


def is_openwb_pro_charger(charger: Any) -> bool:
    """Return true for the official openWB Pro connect.php driver."""

    return bool(charger is not None and charger.__class__.__name__ == "OpenWBProCharger")


def start_hold_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return max(60.0, _safe_float(cfg.get("openwb_pro_start_hold_s", 180), 180.0))


def mark_start_offer(
    state: Dict[str, Any],
    amp: Any = 6,
    *,
    now_ts: Any = None,
    config: Optional[Dict[str, Any]] = None,
    charger_max_amp: Any = 16,
    refresh: bool = False,
) -> None:
    """Remember that an openWB Pro start offer is standing."""

    if not isinstance(state, dict):
        return
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    max_amp = max(6.0, _safe_float(charger_max_amp, 16.0))
    amp_value = round(max(0.0, min(max_amp, _safe_float(amp, 0.0))), 1)
    if amp_value < 6:
        return
    hold_s = start_hold_s(config)
    last_start_ts = _safe_float(state.get("last_start_ts", 0.0), 0.0)
    hold_until = _safe_float(state.get("_openwb_pro_start_hold_until", 0.0), 0.0)
    if refresh or last_start_ts <= 0.0 or hold_until <= now_value:
        state["last_start_ts"] = now_value
        hold_until = now_value + hold_s
    state["_openwb_pro_start_hold_until"] = hold_until
    state["_openwb_pro_start_hold_amp"] = max(
        _safe_float(state.get("_openwb_pro_start_hold_amp", 0), 0.0),
        amp_value,
    )
    if refresh and str(state.get("_bev_full_block_reason") or "") == "start_rejected_soft":
        state["_bev_full_block_reason"] = ""
        state["_openwb_start_reject_soft_until"] = 0.0


def start_hold_active(
    state: Dict[str, Any],
    now_ts: Any = None,
    *,
    hw_charging: bool = False,
    stable_hw_power_w: Any = 0.0,
) -> bool:
    if not isinstance(state, dict):
        return False
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    hold_until = _safe_float(state.get("_openwb_pro_start_hold_until", 0.0), 0.0)
    hold_amp = _safe_float(state.get("_openwb_pro_start_hold_amp", 0), 0.0)
    power_w = _safe_float(stable_hw_power_w, 0.0)
    return bool(
        hold_amp >= 6
        and now_value < hold_until
        and not hw_charging
        and power_w <= 500.0
    )


def phase_wait_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    value = _safe_float(cfg.get("openwb_pro_phase_wait_s", 480), 480.0)
    return max(480.0, value)


def phase_zero_settle_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    value = _safe_float(cfg.get("openwb_pro_phase_zero_settle_s", 3), 3.0)
    return min(30.0, max(2.0, value))


def phase_restart_delay_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return max(0.0, _safe_float(cfg.get("openwb_pro_phase_restart_delay_s", 0), 0.0))


def phase_cp_interrupt_duration_s(config: Optional[Dict[str, Any]] = None) -> float:
    """Liefert eine kurze, gerätegebundene CP-Unterbrechung in Sekunden.

    Die 480-s-Frist ist ausschließlich der Anti-Flatter-Puffer zwischen zwei
    Phasenwechseln und darf niemals als CP-Unterbrechungsdauer verwendet
    werden. Der aktive ``phasetarget``-Pfad überlässt die CP-Sequenz ohnehin
    der openWB; dieser Wert bleibt nur für kompatible Altzustände.
    """

    cfg = config or {}
    value = _safe_float(
        cfg.get(
            "openwb_pro_phase_cp_interrupt_duration_s",
            cfg.get("openwb_pro_start_cp_interrupt_s", 5),
        ),
        5.0,
    )
    # 480 s war in einem früheren Vertrag fälschlich als CP-Dauer vorbelegt.
    # Dieser Altwert wird als Migrationsmarker behandelt, damit ein Update
    # keine 30-s-Unterbrechung aus dem heutigen, kurzen Geräteimpuls macht.
    if value >= 480.0:
        value = 5.0
    return min(30.0, max(2.0, value))


def start_wakeup_delay_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return max(0.0, _safe_float(cfg.get("openwb_pro_start_wakeup_delay_s", 5), 5.0))


START_CP_GRACE_DEFAULT_S = 60.0

# Startfenster der openWB Pro – Konstanten.
START_WINDOW_SCHEMA = "openwb_pro_start_window_v1"
START_CP_GRACE_MIN_S = 60.0
START_WINDOW_HOLD_DEFAULT_S = 180.0
START_WINDOW_HOLD_MIN_S = 60.0
START_WINDOW_HOLD_MAX_S = 600.0
START_WINDOW_RETRY_CYCLE_DEFAULT_S = 300.0
START_WINDOW_RETRY_CYCLE_MIN_S = 180.0
START_WINDOW_RETRY_CYCLE_MAX_S = 1200.0
START_WINDOW_READBACK_TIMEOUT_S = 20.0
START_WINDOW_REEMIT_MAX = 2
START_WINDOW_UNRESPONSIVE_PAUSE_S = 60.0
START_WINDOW_RAISE_DELAY_S = 30.0
START_WINDOW_CONFIRM_FRAMES = 2
START_WINDOW_CONFIRM_POWER_W = 500.0
START_WINDOW_CONFIRM_ENERGY_WH = 5.0
START_WINDOW_CP_POWER_MAX_W = 100.0
START_WINDOW_CP_SETTLE_MIN_S = 15.0
START_WINDOW_PHASETARGET_QUIET_S = 60.0
START_WINDOW_OFFER_LOST_FRAMES = 2
START_WINDOW_STATUS_FRESH_S = 10.0
START_WINDOW_STALE_ABORT_FRAMES = 2
# Übernahme eines stehenden Angebots nur bis
# clamp (keine +2-A-Toleranz mehr) – darüber Startbefehl clamp bzw. regulating.
START_WINDOW_ADOPT_TOLERANCE_A = 0.0
# Ein eigener Befehl an eine laufende Ladung gilt bis zu dieser Toleranz über
# dem stehenden Angebot noch als „nicht darüber“ (kein Start); mehr als diese
# Toleranz darunter ist eine eigene Absenkung. Die Hälfte der kleinsten
# Stromstufe (0,1 A der openWB Pro) trennt Messrauschen von einer Stufe.
START_WINDOW_RUNNING_COMMAND_TOLERANCE_A = 0.05
START_WINDOW_CYCLES_MAX = 3
START_WINDOW_HLC_FACTOR = 2.0
START_WINDOW_BUDGET_COHERENCE_FRAMES = 2
START_WINDOW_DISCONNECT_FRAMES = 2
START_WINDOW_GAP_RESET_S = 300.0
START_WINDOW_BASIC_SIGNALING = ("basic", "basic iec61851")
START_WINDOW_STATE_INACTIVE = "inactive"
START_WINDOW_STATE_BUDGET_WAIT = "budget_wait"
START_WINDOW_STATE_OFFER_PENDING = "offer_pending"
START_WINDOW_STATE_BOX_UNRESPONSIVE = "box_unresponsive"
START_WINDOW_STATE_OFFER_FROZEN = "offer_frozen"
START_WINDOW_STATE_CHARGING_CONFIRMED = "charging_confirmed"
START_WINDOW_STATE_REGULATING = "regulating"
START_WINDOW_STATE_RETRY_WAIT = "retry_wait"
START_WINDOW_STATE_EXHAUSTED = "exhausted"
START_WINDOW_STATE_OFF_NO_BUDGET = "off_no_budget"
START_WINDOW_STATE_ABORTED_HARD = "aborted_hard"
START_WINDOW_ENFORCING_STATES = frozenset({
    START_WINDOW_STATE_BUDGET_WAIT,
    START_WINDOW_STATE_OFFER_PENDING,
    START_WINDOW_STATE_BOX_UNRESPONSIVE,
    START_WINDOW_STATE_OFFER_FROZEN,
    START_WINDOW_STATE_CHARGING_CONFIRMED,
    START_WINDOW_STATE_RETRY_WAIT,
    START_WINDOW_STATE_EXHAUSTED,
    START_WINDOW_STATE_OFF_NO_BUDGET,
})
START_WINDOW_FREEZE_STATES = frozenset({
    START_WINDOW_STATE_OFFER_PENDING,
    START_WINDOW_STATE_BOX_UNRESPONSIVE,
    START_WINDOW_STATE_OFFER_FROZEN,
    START_WINDOW_STATE_RETRY_WAIT,
    START_WINDOW_STATE_EXHAUSTED,
})
START_WINDOW_SETPOINT_STATES = frozenset({
    START_WINDOW_STATE_OFFER_PENDING,
    START_WINDOW_STATE_OFFER_FROZEN,
    START_WINDOW_STATE_CHARGING_CONFIRMED,
    START_WINDOW_STATE_RETRY_WAIT,
    START_WINDOW_STATE_EXHAUSTED,
})
START_WINDOW_ZERO_STATES = frozenset({
    START_WINDOW_STATE_OFF_NO_BUDGET,
    START_WINDOW_STATE_ABORTED_HARD,
})
START_WINDOW_NO_BUDGET_CLOCK_STATES = frozenset({
    START_WINDOW_STATE_BUDGET_WAIT,
    START_WINDOW_STATE_OFFER_FROZEN,
    START_WINDOW_STATE_RETRY_WAIT,
    START_WINDOW_STATE_EXHAUSTED,
})
# charging_confirmed führt eine eigene
# Nullbudget-Uhr (0 A nach 60 s ohne Budget, openWB-Abschaltverzögerung) und
# einen Wh-Deckel (100 Wh Netzbezug innerhalb derselben Nullbudget-Episode;
# Zuwachs nur in Frames ohne Budget, ein Budgetframe setzt
# Uhr und Wh zurück). Beides gibt 0 A nur frei (zero_allowed); gesendet wird
# es vom bestehenden Nullbudget-Pfad.
START_WINDOW_CONFIRMED_ZERO_BUDGET_S = 60.0
START_WINDOW_CONFIRMED_GRID_IMPORT_MAX_WH = 100.0
START_WINDOW_GRID_INTEGRATION_MAX_DT_S = 30.0
# Re-Arm aus regulating/charging_confirmed nur
# nach einem eigenen 0 A des Managers (Beleg: letzter 0-A-Strombefehl der
# Stecksession, Eingang last_zero_reason). Das 0 A der Phasensequenz bzw. der
# Phasenwartezeit gehört zu dieser Grundfamilie und zählt nicht; ohne Beleg
# (box-seitiger Abfall) bleibt der Zustand stehen.
START_WINDOW_SEQUENCE_ZERO_REASON_PREFIXES = (
    "openwb_pro_phase_",
)
# Typisierte Ausnahme – Absenkung auf den Mindeststrom unter
# Nullbudget bei bestätigter Ladung im Startfenster (nie Anhebung, nie 0 A).
MINIMUM_HOLD_REDUCTION_SCHEMA = "openwb_pro_minimum_hold_reduction_v1"
MINIMUM_HOLD_REDUCTION_REASON = "minimum_hold_reduction"
# Zustände vor dem eigenen bestätigten Angebot.
# Eine dort bereits laufende Ladung (2 Frames > 500 W bei stehendem Box-Angebot,
# z. B. nach Neustart/Force-Start/Lücke > 300 s) wird übernommen; Absenkungen ≥ 6 A
# sind sofort erlaubt (Netzbezug statt Deadlock), 0 A bleibt gesperrt.
START_WINDOW_PRE_OFFER_STATES = frozenset({
    START_WINDOW_STATE_BUDGET_WAIT,
    START_WINDOW_STATE_OFFER_PENDING,
    START_WINDOW_STATE_BOX_UNRESPONSIVE,
})
# Weiche EMS-Stoppgründe („halten“): Budget-, Prioritäts-, Pre-Dump-,
# wbminSoC- und Zuteilungsstopps schreiben im Startfenster kein 0 A. Jeder andere
# Grund (Pause, Sperre, Notaus, Nutzer-Aus, Trennung, Hausanschluss, Slot-Ende,
# Ladeende, start_rejected, unbekannt) gilt als hart – fail-closed.
START_WINDOW_SOFT_STOP_REASON_PREFIXES = (
    "openwb_pro_curve_direct",
    "openwb_pro_start_window_zero_budget_off",
    "zero_budget",
    "storage_charge_reserve",
    "min_current_import_integral",
    "priority_",
    "wbminsoc_",
    "predump_",
    "hold_target_below_minimum",
    "group_deficit",
    "native_battery_drain_zero_budget",
    "battery_departure_floor",
)


def start_window_stop_reason_is_soft(reason: Any) -> bool:
    """Weicher EMS-Stoppgrund (im Startfenster halten) – Präfixvergleich, sonst hart."""

    text = str(reason or "").strip().lower()
    if not text:
        return False
    return any(text.startswith(prefix) for prefix in START_WINDOW_SOFT_STOP_REASON_PREFIXES)


def start_cp_grace_s(
    config: Optional[Dict[str, Any]] = None,
    cp_capability: Optional[Dict[str, Any]] = None,
) -> float:
    """Karenz vor dem ersten Start-CP-Impuls einer Stecksession.

    Ein per Anlagenfreigabe (``on``) aktivierter Impuls folgt nicht sofort dem
    ersten Stromangebot: Ein normal startendes Fahrzeug zieht innerhalb von
    10-30 s Strom und wird nicht unterbrochen. Erst wenn nach der Karenz noch
    keine Ladung läuft, kommt der begrenzte Impuls (evcc-Verhalten; etwa ein
    Fahrzeug, das nach einem 0-A-Schnitt ein Angebot nicht annimmt und erst
    durch den Impuls aufwacht). Ein explizites Fahrzeugprofil, das den Impuls zum Start
    verlangt, behält den sofortigen Impuls (Karenz 0).
    """

    cfg = config if isinstance(config, dict) else {}
    capability = cp_capability if isinstance(cp_capability, dict) else {}
    # Ein CP-Impuls wenige Sekunden nach dem Start bringt die Ladung nicht sicher in Gang:
    # ein Fahrzeugprofil mit Impulsbedarf erlaubt den Impuls, verkürzt die
    # Karenz aber nie unter START_CP_GRACE_MIN_S (evcc 30 s, hier 60 s).
    capability.get("automatic_profile_capability")  # nur „Impuls erlaubt“, keine Karenz 0
    return max(
        START_CP_GRACE_MIN_S,
        min(600.0, _safe_float(cfg.get("openwb_pro_start_cp_grace_s"), START_CP_GRACE_DEFAULT_S)),
    )


def start_disconnect_reset_s(config: Optional[Dict[str, Any]] = None) -> float:
    """Mindestdauer einer sicher neuen physischen Stecksession.

    Ein CP-Wake-up kann den Pro-Status vorübergehend wie ein Abstecken aussehen
    lassen. Deshalb darf weder ein einzelner Statusframe noch die kurze
    Wake-up-Verzögerung den persistierten Versuchsbeleg zurücksetzen. Die
    konservative 120-Sekunden-Grenze liegt zusätzlich nie unter dem
    Retryabstand oder einer expliziten Start-CP-Dauer plus 30 Sekunden Puffer.
    """

    cfg = config if isinstance(config, dict) else {}
    retry_s = max(30.0, _safe_float(cfg.get("wb_openwb_start_retry_s"), 45.0))
    cp_duration_s = max(
        0.0,
        min(30.0, _safe_float(cfg.get("openwb_pro_start_cp_interrupt_s"), 0.0)),
    )
    return max(120.0, retry_s, cp_duration_s + 30.0)


def _explicit_connection_state(status: Optional[Dict[str, Any]] = None) -> Optional[bool]:
    """Liefert nur einen ausdrücklich beobachteten Steckzustand.

    ``plug_state`` ist beim normalisierten openWB-Pro-Status bereits der
    physische Gesamtzustand und deshalb autoritativ. Rohsignal, Schloss und
    Fahrzeugcode dienen nur als konservativer Fallback, wenn dieser
    normalisierte Zustand wirklich fehlt. Widersprüchliche oder fehlerhafte
    Fallback-Evidenz bleibt unbekannt.
    """

    st = status if isinstance(status, dict) else {}

    if "plug_state" in st:
        return _explicit_numeric_bool(st.get("plug_state"))

    observed = []
    for key in ("plug_state_raw", "locked", "plugged", "plug", "connected"):
        if key not in st:
            continue
        parsed = _explicit_numeric_bool(st.get(key))
        if parsed is None:
            return None
        observed.append(parsed)
    if "car" in st:
        car_value = st.get("car")
        if isinstance(car_value, bool) or not isinstance(car_value, (int, float)):
            return None
        car = float(car_value)
        if not math.isfinite(car) or car < 0.0:
            return None
        observed.append(car >= 2.0)
    if not observed:
        return None
    if any(observed) and not all(observed):
        return None
    return observed[0]


def _disconnect_evidence_uncontested(
    status: Optional[Dict[str, Any]] = None,
) -> bool:
    """Verhindert eine erfundene False-Flanke bei positiver Nebenevidenz.

    Der normalisierte ``plug_state`` bleibt für den Anschlusszustand
    autoritativ. Für das Erzeugen einer neuen Abstecksession gilt jedoch die
    strengere Beweisrichtung: Schloss, Rohstecker, ``plugged`` und Fahrzeugcode
    dürfen nicht gleichzeitig noch eine Verbindung anzeigen.
    """

    st = status if isinstance(status, dict) else {}
    if _explicit_connection_state(st) is not False:
        return False
    for key in ("plug_state_raw", "locked", "plugged"):
        if key not in st:
            continue
        parsed = _explicit_numeric_bool(st.get(key))
        if parsed is None or parsed is True:
            return False
    if "car" in st:
        car_value = st.get("car")
        if isinstance(car_value, bool) or not isinstance(car_value, (int, float)):
            return False
        car = float(car_value)
        if not math.isfinite(car) or car < 0.0 or car >= 2.0:
            return False
    return True


def _idle_zero_status(
    status: Optional[Dict[str, Any]] = None,
) -> bool:
    """Prüft den konservativen 0-A-/0-W-Ruhezustand."""

    st = status if isinstance(status, dict) else {}
    offered = max(
        abs(_safe_float(st.get("amp"), 0.0)),
        abs(_safe_float(st.get("offered_current_raw"), 0.0)),
        abs(_safe_float(st.get("evse_current"), 0.0)),
    )
    return bool(
        not bool(st.get("charging") or st.get("charge_state"))
        and offered <= 0.5
        and _phase_status_power_w(st) <= 50.0
    )


def _explicit_disconnected_physical_idle(
    status: Optional[Dict[str, Any]] = None,
) -> bool:
    """Bindet eine Absteckkante ausschließlich an reale Fahrzeugphysik.

    Die openWB Pro darf nach dem Abstecken ihren letzten EVSE-Sollwert in
    ``offered_current_raw`` oder verwandten Angebotsfeldern behalten. Bei
    einem frischen, explizit abgesteckten Status sind diese Sollwerte keine
    Aktivität. Maßgeblich bleiben Ladeflag, reale Leistung, gemessene
    Phasenströme und tatsächlich aktive Phasen. Konfigurierte Anzeige- oder
    Zielphasen sind dagegen kein Stromfluss.
    """

    st = status if isinstance(status, dict) else {}

    def _finite_number(key: str, default: float = 0.0) -> Optional[float]:
        if key not in st:
            return float(default)
        value = st.get(key)
        if isinstance(value, bool):
            return None
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            return None
        return numeric if math.isfinite(numeric) else None

    if bool(
        st.get("charging")
        or st.get("charge_state")
        or st.get("phase_power_verified")
    ):
        return False

    real_power_keys = (
        "phase_power_l1_w",
        "phase_power_l2_w",
        "phase_power_l3_w",
        "real_power_w",
        "phase_power_sum_w",
        "power_w",
        "apparent_power_va",
    )
    real_power = []
    for key in real_power_keys:
        value = _finite_number(key)
        if value is None:
            return False
        real_power.append(abs(value))
    measured_phase_power = sum(real_power[:3])
    if max(real_power + [measured_phase_power], default=0.0) > 50.0:
        return False

    phases_in_use = _finite_number("phases_in_use")
    if phases_in_use is None or phases_in_use < 0.0 or phases_in_use > 0.0:
        return False

    for key in (
        "phase_current_l1_a",
        "phase_current_l2_a",
        "phase_current_l3_a",
    ):
        current = _finite_number(key)
        if current is None or abs(current) > 0.2:
            return False
    return True


def _start_session_reset_barriers(
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Bindet Phasen- und CP-Sperren für eine neue physische Stecksession."""

    data = state_data if isinstance(state_data, dict) else {}
    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    now_value = _safe_float(now_ts, 0.0)

    reservation = data.get("_wallbox_phase_transition_reservation")
    reservation = reservation if isinstance(reservation, dict) else {}
    reservation_stage = str(reservation.get("stage") or "")
    reservation_lease = _safe_float(
        reservation.get("lease_until_ts", reservation.get("expires_ts")), 0.0
    )
    reservation_active = bool(
        reservation
        and reservation.get("active") is True
        and reservation_stage not in ("recovery_hold", "completed", "aborted", "expired", "fault")
        and (reservation_lease <= 0.0 or reservation_lease > now_value)
    )

    # Eine reine Vorab-Reservation ohne irgendeinen Phasen-/Hardwareausgang
    # gehört noch zur alten Stecksession. Sie darf eine durch mindestens drei
    # frische physische Disconnect-Frames belegte neue Session nicht selbst
    # blockieren. Sobald eine Sequenz, ein Ausgangsbeleg oder Recovery-Zustand
    # existiert, bleibt die Rotation dagegen unverändert fail-closed.
    preoutput_phase_disconnect_override = bool(
        reservation_active
        and reservation_stage == "await_budget"
        and _safe_int(reservation.get("committed_w"), 0) == 0
        and _safe_float(reservation.get("committed_ts"), 0.0) <= 0.0
        and _safe_int(reservation.get("valid_frames"), 0) == 0
        and not any(
            isinstance(data.get(key), dict) and bool(data.get(key))
            for key in (
                "_openwb_pro_phase_sequence",
                "_openwb_pro_phase_output_intent",
                "_openwb_pro_phase_output_ack",
                "_openwb_pro_phase_recovery_hold",
                "_openwb_pro_phase_restart_authorized",
            )
        )
    )

    sequence = data.get("_openwb_pro_phase_sequence")
    sequence = sequence if isinstance(sequence, dict) else {}
    sequence_stage = str(sequence.get("stage") or "")
    sequence_deadline = max(
        _safe_float(sequence.get("current_allowed_after"), 0.0),
        _safe_float(sequence.get("deadline_ts"), 0.0),
        _safe_float(sequence.get("lease_until_ts"), 0.0),
        _safe_float(data.get("_openwb_pro_phase_sequence_current_allowed_after"), 0.0),
    )
    sequence_active = bool(
        sequence
        and sequence_stage not in (
            "", "ready", "completed", "aborted", "expired", "fault", "recovery_hold"
        )
        and (sequence_deadline <= 0.0 or sequence_deadline > now_value)
    )

    phase_until = max(
        _safe_float(data.get("_openwb_pro_phase_wait_until"), 0.0),
        _safe_float(data.get("_openwb_pro_phase_change_block_until"), 0.0),
    )
    phase_active = bool(reservation_active or sequence_active or phase_until > now_value)

    cp_inactive = _explicit_inactive(st.get("cp_interrupt_isactive"))
    start_cp_duration = max(
        0.0,
        min(30.0, _safe_float(cfg.get("openwb_pro_start_cp_interrupt_s"), 0.0)),
    )
    recent_cp_ts = max(
        _safe_float(data.get("_openwb_pro_start_wakeup_cp_ts"), 0.0),
        _safe_float(data.get("_openwb_last_cp_start_ts"), 0.0),
    )
    wakeup_receipt = data.get("_openwb_pro_start_wakeup_receipt")
    wakeup_receipt = (
        wakeup_receipt if isinstance(wakeup_receipt, dict) else {}
    )
    if (
        str(wakeup_receipt.get("schema") or "")
        == "openwb_pro_start_wakeup_receipt_v1"
        and str(wakeup_receipt.get("plug_session_id") or "")
        == str(data.get("_openwb_pro_plug_session_id") or "")
    ):
        recent_cp_ts = max(
            recent_cp_ts,
            _safe_float(wakeup_receipt.get("last_sent_ts"), 0.0),
        )
    cp_aftermath_until = (
        recent_cp_ts + start_cp_duration + 30.0 if recent_cp_ts > 0.0 else 0.0
    )
    wakeup_until = max(
        _safe_float(data.get("_openwb_pro_start_wakeup_allowed_after"), 0.0),
        cp_aftermath_until,
    )
    wakeup_active = bool(wakeup_until > now_value or not cp_inactive)
    return {
        "cp_inactive": cp_inactive,
        "wakeup_active": wakeup_active,
        "phase_active": phase_active,
        "reservation_active": reservation_active,
        "preoutput_phase_disconnect_override": (
            preoutput_phase_disconnect_override
        ),
        "sequence_active": sequence_active,
        "phase_until": phase_until,
        "sequence_deadline": sequence_deadline,
        "wakeup_until": wakeup_until,
        "cp_aftermath_until": cp_aftermath_until,
    }


def disconnected_phase_terminalization_contract(
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Erlaubt das Ende eines abgelaufenen Phasenvertrags ohne Fahrzeug.

    Ein Manager-Neustart darf eine bereits vollständig abgelaufene
    Phasenpause nicht dauerhaft neu bewaffnen. Die Terminalisierung bleibt
    trotzdem fail-closed: Sie benötigt den gebundenen, bestätigten
    Phasenausgang, ein frisches explizites Abstecksignal, 0 A/0 W, inaktives
    CP und den bestätigten Zielphasen-Readback. Es wird kein Gerätebefehl
    erzeugt.
    """

    data = state_data if isinstance(state_data, dict) else {}
    st = status if isinstance(status, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    reservation = data.get("_wallbox_phase_transition_reservation")
    sequence = data.get("_openwb_pro_phase_sequence")
    intent = data.get("_openwb_pro_phase_output_intent")
    ack = data.get("_openwb_pro_phase_output_ack")
    reservation = reservation if isinstance(reservation, dict) else {}
    sequence = sequence if isinstance(sequence, dict) else {}
    intent = intent if isinstance(intent, dict) else {}
    ack = ack if isinstance(ack, dict) else {}

    reservation_id = str(
        reservation.get("transition_id")
        or reservation.get("reservation_id")
        or ""
    )
    target = _valid_phase_count(sequence.get("target"), 0)
    reservation_target = _valid_phase_count(
        reservation.get("target_phases"), 0
    )
    intent_target = _valid_phase_count(intent.get("target"), 0)
    allowed_after = _safe_float(sequence.get("current_allowed_after"), 0.0)
    phase_sent_ts = _safe_float(sequence.get("phase_sent_ts"), 0.0)
    intent_patch = intent.get("sequence_patch")
    intent_patch = intent_patch if isinstance(intent_patch, dict) else {}
    intent_phase_sent_ts = _safe_float(
        intent_patch.get("phase_sent_ts"), 0.0
    )
    reservation_deadline = _safe_float(
        reservation.get("stage_deadline_ts"), 0.0
    )
    passive_block_until = _safe_float(
        data.get("_openwb_pro_phase_change_block_until"), 0.0
    )
    sequence_block_until = _safe_float(
        sequence.get("phase_change_block_until"), 0.0
    )
    hard_wait_until = max(
        allowed_after,
        reservation_deadline,
        passive_block_until,
        sequence_block_until,
        phase_sent_ts + 480.0 if phase_sent_ts > 0.0 else 0.0,
    )
    requested_w = max(0, _safe_int(reservation.get("requested_w"), 0))
    committed_w = max(0, _safe_int(reservation.get("committed_w"), 0))
    connection_state = _explicit_connection_state(st)
    intent_id = str(intent.get("intent_id") or "")
    actual_phases = _phase_status_count(st)
    reported_target = _valid_phase_count(st.get("phases_target"), 0)
    target_readback_confirmed = bool(
        target in (1, 3)
        and reported_target == target
        and actual_phases in (0, target)
    )
    checks = {
        "status_fresh": _fresh_valid_status(st),
        "explicitly_disconnected": connection_state is False,
        "idle_zero": _idle_zero_status(st),
        "cp_inactive": _explicit_inactive(st.get("cp_interrupt_isactive")),
        "sequence_restart_delay": str(sequence.get("stage") or "")
        == "restart_delay",
        "target_valid": bool(
            target in (1, 3)
            and reservation_target == target
            and intent_target == target
        ),
        "hard_480s_wait_elapsed": bool(
            phase_sent_ts > 0.0
            and hard_wait_until >= phase_sent_ts + 480.0
            and now_value >= hard_wait_until
        ),
        "reservation_bound": bool(
            reservation
            and str(reservation.get("schema_version") or "")
            == "wallbox_phase_transition_v2"
            and reservation_id
            and reservation_target == target
            and str(reservation.get("stage") or "") == "restart_delay"
            and str(reservation.get("grant_state") or "") == "committed"
            and requested_w > 0
            and committed_w >= requested_w
            and _safe_float(reservation.get("committed_ts"), 0.0) > 0.0
            and abs(reservation_deadline - allowed_after) <= 0.1
        ),
        "output_intent_bound": bool(
            intent_id
            and str(intent.get("schema") or "")
            == "openwb_pro_phase_output_intent_v1"
            and intent_id.startswith(reservation_id + ":send_phase:")
            and str(intent.get("action") or "") == "send_phase"
            and str(intent.get("method") or "") == "set_phases"
            and intent_target == target
            and intent_phase_sent_ts > 0.0
            and abs(intent_phase_sent_ts - phase_sent_ts) <= 0.1
        ),
        "output_ack_bound": bool(
            intent_id
            and str(ack.get("schema") or "")
            == "openwb_pro_phase_output_ack_v1"
            and str(ack.get("intent_id") or "") == intent_id
            and ack.get("success") is True
            and _safe_float(ack.get("wall_ts"), 0.0)
            >= _safe_float(intent.get("wall_ts"), 0.0)
        ),
        # Im abgesteckten Zustand meldet die Pro regelmäßig 0 Istphasen.
        # Autoritativ ist dann ihr frischer, expliziter phasetarget-Readback.
        # Ein abweichender realer Istphasenwert bleibt ein harter Blocker.
        "target_readback_confirmed": target_readback_confirmed,
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "contract": "openwb_pro_disconnected_phase_terminalization_v1",
        "allow": not blockers,
        "action": "terminalize_without_output" if not blockers else "hold",
        "target": target,
        "allowed_after": allowed_after,
        "phase_sent_ts": phase_sent_ts,
        "hard_wait_until": hard_wait_until,
        "reservation_id": reservation_id,
        "connection_state": connection_state,
        "checks": checks,
        "blockers": blockers,
    }


def start_disconnect_reset_guard(
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Bewertet, ob eine echte Absteckdauer gezählt werden darf."""

    data = state_data if isinstance(state_data, dict) else {}
    st = status if isinstance(status, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    barriers = _start_session_reset_barriers(
        data,
        st,
        config,
        now_ts=now_value,
    )
    status_fresh = _fresh_valid_status(st)
    connection_state = _explicit_connection_state(st)
    explicitly_disconnected = connection_state is False
    disconnect_evidence_uncontested = bool(
        explicitly_disconnected
        and _disconnect_evidence_uncontested(st)
    )
    physical_idle = bool(
        disconnect_evidence_uncontested
        and _explicit_disconnected_physical_idle(st)
    )
    physical_disconnect_candidate = bool(
        status_fresh
        and explicitly_disconnected
        and disconnect_evidence_uncontested
        and physical_idle
        and barriers["cp_inactive"]
        and (
            not barriers["phase_active"]
            or barriers["preoutput_phase_disconnect_override"]
        )
    )
    eligible = bool(
        physical_disconnect_candidate
        and not barriers["wakeup_active"]
    )
    return {
        "contract": "openwb_pro_disconnect_reset_guard_v2",
        "eligible": eligible,
        # Ein einzelner Disconnect-Frame im CP-Nachlauf darf die Session nie
        # rotieren. Eine echte physische Trennung muss aber schon während des
        # konservativen 30-s-Nachlaufs gesammelt werden dürfen; sonst bleibt
        # ein 20-s-Ab-/Anstecken unsichtbar und übernimmt alte Startbelege.
        # Die Rotation folgt erst nach mindestens drei Frames und der
        # konfigurierten realen Absteckdauer am frischen Reconnect.
        "candidate_eligible": physical_disconnect_candidate,
        "status_fresh": status_fresh,
        "connection_state": connection_state,
        "explicitly_disconnected": explicitly_disconnected,
        "disconnect_evidence_uncontested": disconnect_evidence_uncontested,
        "idle_zero": physical_idle,
        "physical_idle": physical_idle,
        "retained_offer_ignored": bool(
            explicitly_disconnected
            and physical_idle
            and not _idle_zero_status(st)
        ),
        **barriers,
        "required_s": start_disconnect_reset_s(config),
    }


def start_disconnect_candidate_step(
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Fortschritt einer bestätigten physischen Absteckung, ohne Mutation."""

    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    guard = start_disconnect_reset_guard(
        data,
        status,
        config,
        now_ts=now_value,
    )
    existing = data.get("_openwb_pro_disconnect_candidate")
    candidate = dict(existing) if isinstance(existing, dict) else {}
    if guard.get("candidate_eligible", guard.get("eligible", False)):
        sample_ts = max(
            _safe_float((status or {}).get("driver_status_last_sample_ts"), 0.0),
            _safe_float((status or {}).get("driver_status_last_ok_ts"), 0.0),
        )
        if sample_ts <= 0.0:
            sample_ts = now_value
        if not candidate:
            candidate = {"since_ts": sample_ts, "frames": 0, "last_sample_ts": 0.0}
        previous_sample_ts = _safe_float(candidate.get("last_sample_ts"), 0.0)
        if previous_sample_ts > 0.0 and sample_ts < previous_sample_ts:
            candidate = {
                "since_ts": sample_ts,
                "frames": 0,
                "last_sample_ts": 0.0,
            }
            previous_sample_ts = 0.0
        if sample_ts > previous_sample_ts:
            candidate["frames"] = max(0, _safe_int(candidate.get("frames"), 0)) + 1
            candidate["last_ts"] = sample_ts
            candidate["last_sample_ts"] = sample_ts
    else:
        candidate = {}
    confirmed = bool(
        candidate
        and int(candidate.get("frames", 0) or 0) >= 3
        and _safe_float(candidate.get("last_sample_ts"), 0.0)
        - _safe_float(candidate.get("since_ts"), 0.0)
        >= _safe_float(guard.get("required_s"), 120.0)
    )
    return {
        "contract": "openwb_pro_disconnect_candidate_step_v1",
        "guard": guard,
        "candidate": candidate,
        "confirmed": confirmed,
    }


# Verhalten nach dem Abstecken (Geräteeigenschaft je openWB Pro).
# ``connect.php`` ist pegelgesteuert: Die Box behält nach dem Abstecken ihren
# letzten Sollstrom. Nach einer bestätigten Trennung setzt der Manager genau
# einmal ein definiertes Stehangebot: ``safe`` 0 A (nichts startet beim
# Anstecken), ``fast_start`` 6 A (das Fahrzeug startet sofort, danach übernimmt
# die normale Regelung das stehende Angebot).
UNPLUG_OFFER_CONFIG_KEY_TEMPLATE = "wb{wb_id}_openwb_pro_unplug_offer"
UNPLUG_OFFER_MODE_SAFE = "safe"
UNPLUG_OFFER_MODE_FAST_START = "fast_start"
UNPLUG_OFFER_TARGET_AMP = {
    UNPLUG_OFFER_MODE_SAFE: 0.0,
    UNPLUG_OFFER_MODE_FAST_START: 6.0,
}
UNPLUG_OFFER_CONTRACT = "openwb_pro_unplug_offer_v1"
# Ein Befehl und höchstens zwei Wiederholungen je bestätigtem Sessionende.
UNPLUG_OFFER_MAX_SENDS = 3
UNPLUG_OFFER_RETRY_AFTER_S = 20.0
UNPLUG_OFFER_READBACK_TOLERANCE_A = 0.25
# Höchstens etwa ein Regelzyklus: ältere Samples sind kein Beleg.
UNPLUG_OFFER_STATUS_MAX_AGE_S = 10.0
UNPLUG_OFFER_ACTIVE_STAGES = ("pending", "await_readback")


def unplug_offer_mode(
    config: Optional[Dict[str, Any]] = None,
    wb_id: Any = 0,
) -> str:
    """Konfigurierter Modus; fehlend oder ungültig gilt ``safe``."""

    cfg = config if isinstance(config, dict) else {}
    raw = str(
        cfg.get(UNPLUG_OFFER_CONFIG_KEY_TEMPLATE.format(wb_id=_safe_int(wb_id, 0)))
        or ""
    ).strip().lower()
    if raw in UNPLUG_OFFER_TARGET_AMP:
        return raw
    return UNPLUG_OFFER_MODE_SAFE


def new_unplug_offer(
    mode: Any,
    *,
    session_id: Any = "",
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Offener Auftrag nach bestätigter Trennung, ohne Geräte-I/O."""

    mode_value = str(mode or "").strip().lower()
    if mode_value not in UNPLUG_OFFER_TARGET_AMP:
        mode_value = UNPLUG_OFFER_MODE_SAFE
    return {
        "contract": UNPLUG_OFFER_CONTRACT,
        "mode": mode_value,
        "target_amp": UNPLUG_OFFER_TARGET_AMP[mode_value],
        "session_id": str(session_id or ""),
        "created_ts": _safe_float(now_ts, 0.0),
        "stage": "pending",
        "sends": 0,
        "sent": False,
        "sent_ts": 0.0,
        "confirmed": False,
        "confirmed_ts": 0.0,
        "reported_amp": None,
        "reason": "disconnect_confirmed",
    }


def unplug_offer_readback(
    status: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Frischer, ausdrücklicher Steck- und Angebotszustand der Box.

    Ein Angebot gilt nur als bekannt, wenn der Treiber es aus einem
    erfolgreichen ``connect.php``-GET bestätigt (``offered_current_confirmed``).
    Fehlende, veraltete oder ungültige Daten liefern ``fresh``/``offer_known``
    ``False`` und sind nie eine Freigabe.
    """

    st = status if isinstance(status, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    sample_ts = max(
        _safe_float(st.get("driver_status_last_sample_ts"), 0.0),
        _safe_float(st.get("driver_status_last_ok_ts"), 0.0),
    )
    age_s = now_value - sample_ts if sample_ts > 0.0 else float("inf")
    fresh = bool(
        _fresh_valid_status(st)
        and st.get("driver_status_stale") is False
        and sample_ts > 0.0
        and -2.0 <= age_s <= UNPLUG_OFFER_STATUS_MAX_AGE_S
    )
    offered = st.get("offered_current_raw")
    offer_known = bool(
        fresh
        and st.get("offered_current_confirmed") is True
        and isinstance(offered, (int, float))
        and not isinstance(offered, bool)
        and math.isfinite(float(offered))
        and float(offered) >= 0.0
    )
    connection = _explicit_connection_state(st) if fresh else None
    # Dieselben strengen Belege wie die Trennungsbestätigung: Schloss,
    # Rohstecker und Fahrzeugcode widersprechen nicht, und es fließt real
    # nichts (Ladeflag, Leistung, Phasenströme). Ein beibehaltenes
    # Sollangebot ist dabei keine Aktivität.
    disconnect_uncontested = bool(
        connection is False
        and _disconnect_evidence_uncontested(st)
        and _explicit_disconnected_physical_idle(st)
    )
    return {
        "fresh": fresh,
        "connection": connection,
        "disconnect_uncontested": disconnect_uncontested,
        "offer_known": offer_known,
        "offered_amp": float(offered) if offer_known else None,
        "sample_ts": sample_ts,
        "age_s": age_s if math.isfinite(age_s) else None,
    }


def unplug_offer_step_contract(
    offer: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    output_blocker: str = "",
) -> Dict[str, Any]:
    """Entscheidet den nächsten Schritt des Abstecken-Angebots, ohne I/O.

    Aktionen: ``none`` (kein offener Auftrag), ``wait`` (nichts senden),
    ``send`` (genau ein Strombefehl auf das Ziel), sowie die Endzustände
    ``confirm``, ``superseded`` und ``exhausted``. Gesendet wird nur bei
    frischem, ausdrücklich getrenntem Status, bekanntem Box-Angebot, das vom
    Ziel abweicht, ohne Ausgangssperre und höchstens ``UNPLUG_OFFER_MAX_SENDS``
    mal je Sessionende.
    """

    item = dict(offer) if isinstance(offer, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    result = {
        "contract": UNPLUG_OFFER_CONTRACT + ":step",
        "action": "none",
        "reason": "inactive",
        "offer": item,
    }
    stage = str(item.get("stage") or "")
    if stage not in UNPLUG_OFFER_ACTIVE_STAGES:
        return result
    target = _safe_float(item.get("target_amp"), -1.0)
    if target not in UNPLUG_OFFER_TARGET_AMP.values():
        item.update({"stage": "invalid", "reason": "target_invalid"})
        result.update({"action": "exhausted", "reason": "target_invalid"})
        return result
    readback = unplug_offer_readback(status, now_ts=now_value)
    item["last_check_ts"] = now_value
    result["readback"] = readback

    def _wait(reason: str) -> Dict[str, Any]:
        item["reason"] = reason
        result.update({"action": "wait", "reason": reason})
        return result

    if not readback["fresh"]:
        return _wait("status_not_fresh")
    if readback["connection"] is True:
        # Ein neues Anstecken beendet den Auftrag; die normale Regelung
        # übernimmt ab hier, ohne dass das Abstecken-Angebot noch wirkt.
        item.update({"stage": "superseded", "reason": "vehicle_plugged"})
        result.update({"action": "superseded", "reason": "vehicle_plugged"})
        return result
    if readback["connection"] is not False:
        return _wait("connection_state_unknown")
    if not readback["disconnect_uncontested"]:
        # Widerspruch (Leistung, Ladeflag, Phasenströme, Schloss oder
        # Fahrzeugcode): weder senden noch bestätigen, kein Versuch verbraucht.
        return _wait("disconnect_evidence_contested")
    if not readback["offer_known"]:
        return _wait("offer_readback_unknown")
    sends = max(0, _safe_int(item.get("sends"), 0))
    sent_ts = _safe_float(item.get("sent_ts"), 0.0)
    if sends > 0 and readback["sample_ts"] <= sent_ts:
        return _wait("await_fresh_readback")
    reported = float(readback["offered_amp"])
    item["reported_amp"] = reported
    if abs(reported - target) <= UNPLUG_OFFER_READBACK_TOLERANCE_A:
        reason = "readback_matches_target" if sends > 0 else "already_at_target"
        item.update({
            "stage": "confirmed",
            "confirmed": True,
            "confirmed_ts": now_value,
            "reason": reason,
        })
        result.update({"action": "confirm", "reason": reason})
        return result
    if sends >= UNPLUG_OFFER_MAX_SENDS:
        item.update({"stage": "unconfirmed", "reason": "readback_mismatch_after_retries"})
        result.update({
            "action": "exhausted",
            "reason": "readback_mismatch_after_retries",
        })
        return result
    if sends > 0 and now_value - sent_ts < UNPLUG_OFFER_RETRY_AFTER_S:
        return _wait("retry_interval")
    blocker = str(output_blocker or "")
    if blocker:
        return _wait(blocker)
    result.update({
        "action": "send",
        "reason": "readback_differs_from_target" if sends == 0 else "readback_mismatch_retry",
        "target_amp": target,
    })
    return result


def start_reconnect_confirmation_contract(
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Bestätigt ein kurzes, entprelltes Ab-/Anstecken ohne Geräte-I/O.

    Drei getrennte, frische 0-W-Frames bilden den Absteckkandidaten. Ein von
    der Pro unplugged beibehaltenes EVSE-Sollangebot ist dabei kein realer
    Stromfluss.
    Danach beweist ein frischer, explizit verbundener Status die Gegenkante,
    auch wenn das Fahrzeug bereits Leistung aufnimmt. Der lange
    120-Sekunden-Disconnect bleibt davon unabhängig als konservativer Fallback
    erhalten.
    """

    data = state_data if isinstance(state_data, dict) else {}
    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    candidate = data.get("_openwb_pro_disconnect_candidate")
    candidate = dict(candidate) if isinstance(candidate, dict) else {}
    since_ts = _safe_float(candidate.get("since_ts"), 0.0)
    last_ts = _safe_float(candidate.get("last_ts"), 0.0)
    frames = max(0, _safe_int(candidate.get("frames"), 0))
    minimum_s = min(
        60.0,
        max(10.0, _safe_float(cfg.get("openwb_pro_reconnect_reset_s"), 10.0)),
    )
    maximum_gap_s = min(
        60.0,
        max(10.0, _safe_float(cfg.get("openwb_pro_reconnect_max_gap_s"), 30.0)),
    )
    barriers = _start_session_reset_barriers(
        data,
        st,
        cfg,
        now_ts=now_value,
    )
    connection_state = _explicit_connection_state(st)
    status_fresh = _fresh_valid_status(st)
    idle_zero = _idle_zero_status(st)
    candidate_age_s = max(0.0, last_ts - since_ts) if since_ts > 0.0 else 0.0
    reconnect_gap_s = max(0.0, now_value - last_ts) if last_ts > 0.0 else 0.0
    candidate_ready = bool(
        candidate
        and frames >= 3
        and since_ts > 0.0
        and last_ts >= since_ts
        and candidate_age_s >= minimum_s
        and reconnect_gap_s <= maximum_gap_s
    )
    confirmed = bool(
        candidate_ready
        and status_fresh
        and connection_state is True
        and barriers["cp_inactive"]
        and (
            not barriers["phase_active"]
            or barriers["preoutput_phase_disconnect_override"]
        )
    )
    return {
        "contract": "openwb_pro_reconnect_confirmation_v1",
        "confirmed": confirmed,
        "physical_replug_overrides_cp_aftermath": bool(
            confirmed and barriers["wakeup_active"]
        ),
        "candidate_ready": candidate_ready,
        "candidate_frames": frames,
        "candidate_age_s": candidate_age_s,
        "reconnect_gap_s": reconnect_gap_s,
        "minimum_s": minimum_s,
        "maximum_gap_s": maximum_gap_s,
        "status_fresh": status_fresh,
        "connection_state": connection_state,
        "explicitly_connected": connection_state is True,
        "idle_zero": idle_zero,
        "connected_edge_authoritative": bool(
            candidate_ready
            and status_fresh
            and connection_state is True
        ),
        **barriers,
    }


def connected_session_meter_reset_contract(
    previous_status: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Bestätigt eine neue Stecksession trotz verpasster Disconnect-Frames.

    Die Sitzung wird nur aus zwei frischen, explizit verbundenen
    connect.php-Samples abgeleitet. Der Sitzungszähler muss deutlich auf null
    zurückfallen, während der kumulative Energiezähler monoton bleibt. Damit
    sind Rundung, fehlende Werte, Stale-Fallbacks und ein Geräte-/Zählerreset
    ausdrücklich keine gültige Reconnect-Evidenz.
    """

    previous = previous_status if isinstance(previous_status, dict) else {}
    current = status if isinstance(status, dict) else {}
    previous_sample_ts = max(
        _safe_float(previous.get("driver_status_last_sample_ts"), 0.0),
        _safe_float(previous.get("driver_status_last_ok_ts"), 0.0),
    )
    current_sample_ts = max(
        _safe_float(current.get("driver_status_last_sample_ts"), 0.0),
        _safe_float(current.get("driver_status_last_ok_ts"), 0.0),
    )
    previous_session_kwh = _safe_float(
        previous.get("session_kwh"),
        float("nan"),
    )
    current_session_kwh = _safe_float(
        current.get("session_kwh"),
        float("nan"),
    )
    previous_total_wh = _safe_float(
        previous.get("imported_total_wh"),
        float("nan"),
    )
    current_total_wh = _safe_float(
        current.get("imported_total_wh"),
        float("nan"),
    )
    previous_api = str(previous.get("api_surface") or "")
    current_api = str(current.get("api_surface") or "")
    previous_driver_instance = str(
        previous.get("driver_instance_token") or ""
    )
    current_driver_instance = str(
        current.get("driver_instance_token") or ""
    )
    checks = {
        "previous_fresh": _fresh_valid_status(previous),
        "current_fresh": _fresh_valid_status(current),
        "previous_connected": _fresh_connection_state(previous) is True,
        "current_connected": _fresh_connection_state(current) is True,
        "sample_time_advanced": bool(
            previous_sample_ts > 0.0
            and current_sample_ts > previous_sample_ts
        ),
        "same_openwb_pro_surface": bool(
            previous_api == "openwb_pro_connect_php"
            and current_api == previous_api
        ),
        "same_driver_instance": bool(
            previous_driver_instance
            and current_driver_instance == previous_driver_instance
        ),
        "cp_inactive": current.get("cp_interrupt_isactive") in (False, 0),
        "current_idle": bool(
            not status_real_charging(current)
            and status_real_power(current) <= 50.0
        ),
        "session_values_finite": bool(
            math.isfinite(previous_session_kwh)
            and math.isfinite(current_session_kwh)
        ),
        "previous_session_positive": bool(previous_session_kwh >= 0.05),
        "current_session_reset": bool(0.0 <= current_session_kwh <= 0.02),
        "session_drop_significant": bool(
            previous_session_kwh - current_session_kwh >= 0.05
        ),
        "total_values_finite": bool(
            math.isfinite(previous_total_wh)
            and math.isfinite(current_total_wh)
        ),
        "total_meter_monotonic": bool(
            current_total_wh + 1.0 >= previous_total_wh
        ),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    return {
        "contract": "openwb_pro_connected_session_meter_reset_v1",
        "confirmed": not blockers,
        "checks": checks,
        "blockers": blockers,
        "previous_sample_ts": previous_sample_ts,
        "current_sample_ts": current_sample_ts,
        "previous_session_kwh": previous_session_kwh,
        "current_session_kwh": current_session_kwh,
        "previous_total_wh": previous_total_wh,
        "current_total_wh": current_total_wh,
        "driver_instance_token": current_driver_instance,
    }


def _openwb_pro_api_version(status: Optional[Dict[str, Any]] = None) -> int:
    st = status if isinstance(status, dict) else {}
    raw = st.get("openwb_pro_api_version", st.get("api_version", st.get("version")))
    try:
        return max(0, int(float(raw)))
    except (TypeError, ValueError):
        return 0


def automatic_start_cp_contract(
    config: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    *,
    is_openwb_pro: bool = False,
) -> Dict[str, Any]:
    """Bindet die dreistufige Freigabe für den kurzen Pro-Startimpuls.

    Die offizielle ``connect.php``-Schnittstelle belegt den CP-Befehl, aber
    keine automatische Freigabe allein aus einer API-Versionsnummer. ``on``
    bleibt deshalb eine ausdrückliche Anlagenfreigabe auf einer frischen,
    offiziell gebundenen Schnittstelle. ``auto`` benötigt zusätzlich eine
    explizite Fahrzeug-/Herstellerfähigkeit aus dem Treiberprofil. Unbekannt
    blockiert nur den optionalen CP-Impuls, nie die normale Stromfreigabe.
    """

    cfg = config if isinstance(config, dict) else {}
    st = status if isinstance(status, dict) else {}
    raw = cfg.get("openwb_pro_automatic_start_cp_enable", "auto")
    text = str(raw if raw is not None else "auto").strip().lower()
    if text in ("0", "false", "no", "off", "nein"):
        mode = "off"
    elif text in ("1", "true", "yes", "on", "ja"):
        mode = "on"
    else:
        mode = "auto"

    fresh = _fresh_valid_status(st)
    surface = str(st.get("api_surface") or "").strip().lower()
    official_surface = surface in (
        "openwb_pro_connect_php",
        "official_connect_php",
    )
    api_version = _openwb_pro_api_version(st)
    cp_wire_capability = bool(
        fresh
        and official_surface
        and st.get("cp_interrupt_supported") is True
    )
    automatic_profile_capability = bool(
        st.get("automatic_start_cp_supported") is True
    )
    enabled = bool(
        is_openwb_pro
        and mode != "off"
        and (
            (mode == "on" and cp_wire_capability)
            or (
                mode == "auto"
                and cp_wire_capability
                and automatic_profile_capability
            )
        )
    )
    if not is_openwb_pro:
        reason = "not_openwb_pro"
    elif mode == "off":
        reason = "explicitly_disabled"
    elif not cp_wire_capability:
        reason = "requires_fresh_official_cp_surface"
    elif mode == "on":
        reason = "explicitly_enabled"
    elif not automatic_profile_capability:
        reason = "auto_requires_explicit_vehicle_profile"
    else:
        reason = "auto_explicit_vehicle_profile"
    return {
        "contract": "openwb_pro_automatic_start_cp_v2",
        "mode": mode,
        "enabled": enabled,
        "reason": reason,
        "is_openwb_pro": bool(is_openwb_pro),
        "status_fresh": bool(fresh),
        "api_surface": surface,
        "api_version": int(api_version),
        "cp_wire_capability": bool(cp_wire_capability),
        "automatic_profile_capability": bool(automatic_profile_capability),
        "capability": bool(
            cp_wire_capability and automatic_profile_capability
        ),
    }


def automatic_start_cp_enabled(
    config: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    *,
    is_openwb_pro: bool = False,
) -> bool:
    """Kompatibler boolescher Zugriff auf den typisierten Freigabevertrag."""

    return bool(
        automatic_start_cp_contract(
            config,
            status,
            is_openwb_pro=is_openwb_pro,
        ).get("enabled", False)
    )


def cp_interrupt_payload(
    config: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Return official connect.php CP-interrupt options.

    The driver translates this neutral payload to ``cp_interrupt=true`` plus
    optional duration/version fields.  Keeping the extraction here prevents the
    manager from owning protocol defaults while still allowing the manager to
    decide when a CP interrupt is safe.
    """

    cfg = config if isinstance(config, dict) else {}
    st = status if isinstance(status, dict) else {}
    payload: Dict[str, Any] = {}
    duration = cfg.get(
        "openwb_pro_cp_interrupt_s",
        cfg.get("openwb_pro_cp_interrupt_duration_s", cfg.get("openwb_pro_cp_interrupt_duration", None)),
    )
    if duration is None:
        duration = st.get("cp_interrupt_duration", None)
    try:
        if duration is not None and str(duration).strip() != "":
            duration_value = int(float(duration))
            if duration_value > 0:
                payload["duration"] = duration_value
    except (TypeError, ValueError):
        pass
    if "duration" not in payload:
        payload["duration"] = 10

    version = str(
        cfg.get(
            "openwb_pro_cp_interrupt_version",
            st.get("cp_interrupt_version", ""),
        )
        or ""
    ).strip()
    if version in ("0V", "-12V"):
        payload["version"] = version
    return payload


def start_cp_interrupt_payload(
    config: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Liefert einen begrenzten CP-Impuls zur Start-/Aufweckwiederherstellung.

    Der offizielle evcc-Pro-Wakeup sendet nur ``cp_interrupt=true``. Dauer und
    Signalvariante werden deshalb ausschließlich bei expliziten
    *Start*-Parametern ergänzt. Weder allgemeine Phasenparameter noch ein
    früherer Geräte-Readback dürfen in den Wake-up-Payload durchsickern.
    """

    cfg = config if isinstance(config, dict) else {}
    configured = cfg.get("openwb_pro_start_cp_interrupt_s")
    payload: Dict[str, Any] = {}
    try:
        if configured is not None and str(configured).strip() != "":
            candidate = int(float(configured))
            if candidate > 0:
                payload["duration"] = min(30, candidate)
    except (TypeError, ValueError):
        pass

    version = str(cfg.get("openwb_pro_start_cp_interrupt_version", "") or "").strip()
    if version in ("0V", "-12V"):
        payload["version"] = version
    return payload


def phase_cp_interrupt_payload(
    config: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Liefert den kurzen CP-Auftrag für den Phasenwechsel.

    Der getrennte 480-Sekunden-Cooldown sperrt ausschließlich einen weiteren
    Phasenwechsel und wird nicht in diesen Geräteimpuls übernommen.
    """

    payload = cp_interrupt_payload(config, status)
    payload["duration"] = int(round(phase_cp_interrupt_duration_s(config)))
    return payload


def _fresh_valid_status(status: Optional[Dict[str, Any]]) -> bool:
    st = status if isinstance(status, dict) else {}
    return bool(
        st
        and st.get("driver_status_valid") is True
        and st.get("driver_status_stale") is not True
        and st.get("driver_status_degraded") is not True
        and st.get("driver_status_glitch") is not True
    )


def _fresh_connection_state(status: Optional[Dict[str, Any]]) -> Optional[bool]:
    """Liefert den expliziten Anschlusszustand eines frischen Pro-Readbacks."""

    st = status if isinstance(status, dict) else {}
    if not _fresh_valid_status(st):
        return None
    plug_state = st.get("plug_state")
    car_state = _safe_int(st.get("car"), 0)
    if plug_state is True or car_state == 2:
        return True
    if plug_state is False or car_state == 1:
        return False
    return None


def _phase_status_power_w(status: Optional[Dict[str, Any]]) -> float:
    st = status if isinstance(status, dict) else {}
    phase_sum = sum(
        abs(_safe_float(st.get(key), 0.0))
        for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w")
    )
    return max(
        phase_sum,
        abs(_safe_float(st.get("real_power_w"), 0.0)),
        abs(_safe_float(st.get("phase_power_sum_w"), 0.0)),
        abs(_safe_float(st.get("power_w"), 0.0)),
    )


def _phase_status_count(status: Optional[Dict[str, Any]]) -> int:
    st = status if isinstance(status, dict) else {}
    for key in ("phase_actual_phases", "phases_actual"):
        phases = _valid_phase_count(st.get(key), 0)
        if phases:
            return phases
    if (
        ("phase_actual_phases" in st or "phases_actual" in st)
        and _phase_status_power_w(st) <= 100.0
        and not bool(st.get("charging") or st.get("charge_state"))
    ):
        # connect.php hält ``phases_in_use`` im Stillstand als letzte vom
        # Fahrzeug verwendete Phasenzahl fest, während ``phases_actual=0`` den
        # gegenwärtig stromlosen Zustand meldet. Für die Bestätigung eines
        # frisch gelesenen Phasenziels ist dieser historische Wert deshalb
        # nicht autoritativ; erst bei realer Ladung wird er wieder Istbeleg.
        return 0
    phases_in_use = _valid_phase_count(st.get("phases_in_use"), 0)
    if phases_in_use:
        return phases_in_use
    pha = _safe_int(st.get("pha"), 0)
    return 3 if pha == 56 else (1 if pha in (8, 16, 32) else 0)


def _zero_readback_confirmed(status: Optional[Dict[str, Any]]) -> bool:
    st = status if isinstance(status, dict) else {}
    if not _fresh_valid_status(st):
        return False
    offered = max(
        abs(_safe_float(st.get("offered_current_raw"), 0.0)),
        abs(_safe_float(st.get("evse_current"), 0.0)),
        abs(_safe_float(st.get("amp"), 0.0)),
    )
    return bool(
        offered <= 0.5
        and _phase_status_power_w(st) <= 100.0
        and not bool(st.get("charging") or st.get("charge_state"))
    )


def _phase_readback_confirmed(status: Optional[Dict[str, Any]], target: int) -> bool:
    st = status if isinstance(status, dict) else {}
    if not _fresh_valid_status(st):
        return False
    target_value = int(target)
    actual_count = _phase_status_count(st)
    if actual_count:
        # Ein vorhandener realer Istphasenwert ist autoritativ. Ein davon
        # abweichendes Sollziel darf niemals als bestätigter No-op gelten und
        # damit die echte 0-A-/480-s-Phasensequenz umgehen.
        return actual_count == target_value

    # Die Pro meldet ``phases_actual=0`` solange das Fahrzeug zwar steckt,
    # aber nach dem Phasenwechsel noch keinen Strom zieht. In diesem Zustand
    # ist der frische ``phases_target``-Readback die einzige bestätigbare
    # Geräteannahme. Er darf ausschließlich im physisch ruhigen, verbundenen
    # und CP-inaktiven Zustand die Wiederanlaufsperre lösen.
    reported_target = _valid_phase_count(st.get("phases_target"), 0)
    connected = bool(
        st.get("plug_state")
        or st.get("car") == 2
        or st.get("plug")
        or st.get("connected")
        or _explicit_connection_state(st) is True
    )

    idle = bool(
        not bool(st.get("charging") or st.get("charge_state"))
        and _phase_status_power_w(st) <= 100.0
    )
    cp_inactive = st.get("cp_interrupt_isactive") in (False, 0)
    return bool(
        reported_target == target_value
        and connected
        and idle
        and cp_inactive
    )


def _post_wire_readback_confirmed(
    status: Optional[Dict[str, Any]],
    target: int,
    wire_receipt_ts: Any,
) -> bool:
    """Akzeptiert nur einen echten frischen GET nach dem Phasen-POST."""

    st = status if isinstance(status, dict) else {}
    wire_ts = _safe_float(wire_receipt_ts, 0.0)
    readback_ts = _safe_float(st.get("driver_status_last_ok_ts"), 0.0)
    if readback_ts > 100_000_000_000.0:
        readback_ts /= 1000.0
    if wire_ts <= 0.0 or readback_ts <= wire_ts:
        return False
    return _phase_readback_confirmed(st, target)


def phase_zero_readback_confirmed(status: Optional[Dict[str, Any]]) -> bool:
    """Öffentliches Recovery-Prädikat für einen frischen, physisch ruhigen 0-A-Zustand."""

    return _zero_readback_confirmed(status)


def phase_target_readback_confirmed(
    status: Optional[Dict[str, Any]], target: int
) -> bool:
    """Öffentliches Recovery-Prädikat für einen frischen Zielphasen-Readback."""

    return _phase_readback_confirmed(status, target)


def start_offer_physics_confirmed(
    status: Optional[Dict[str, Any]],
) -> bool:
    """Bestätigt nur die aktuelle Ladeannahme eines stehenden Stromangebots.

    ``session_kwh`` und alte Manager-Marker sind innerhalb derselben
    Stecksession kumulativ. Sie dürfen einen späteren, erneut stromlosen
    Startversuch deshalb nicht als laufende Ladung ausgeben und damit das
    begrenzte Wake-up-Orakel entwaffnen.
    """

    st = status if isinstance(status, dict) else {}
    if not _fresh_valid_status(st):
        return False
    real_power_w = max(
        _safe_float(st.get("real_power_w"), 0.0),
        _safe_float(st.get("phase_power_sum_w"), 0.0),
        _safe_float(st.get("power_w"), 0.0),
    )
    return bool(
        st.get("charging")
        or st.get("charge_state")
        or real_power_w > 100.0
    )


def phase_sequence_step_contract(
    target_phases: Any,
    sequence: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    hold_s: Any = 480,
    restart_delay_s: Any = 0,
    current_set_amp: Any = 0,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    reason: str = "phase_switch",
    cp_payload: Optional[Dict[str, Any]] = None,
    clock_sample: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Liefert den nächsten sicheren Schritt der openWB-Pro-Phasensequenz.

    Der Aufrufer besitzt weiterhin Zustandsänderung und Treiber-I/O. Dieser Vertrag
    kodiert nur die harte Sequenz rund um connect.php: 0 A, kurze Beruhigung,
    ``phasetarget``, geschützte Wiederanlaufverzögerung und danach die Stromfreigabe.
    Nach dem openWB-Pro-Vertrag besitzt ``phasetarget`` die Fahrzeugsignalisierung
    und deren Pause. Eine zweite explizite CP-Unterbrechung an dieser Stelle kann
    dazu führen, dass die Pro den absichtlich unterbrochenen CP-Zustand als
    abgestecktes Fahrzeug meldet, und muss deshalb eine getrennte Start-/Wake-up-
    Aktion bleiben.

    ``cp_payload`` bleibt zur Kompatibilität mit älteren Aufrufern und persistierten
    Tests in der Signatur, wird aber bewusst nicht in Phasenwechsel-Hardware-I/O
    übersetzt.
    """

    target = _valid_phase_count(target_phases, 0)
    now_value = _safe_float(now_ts, 0.0)
    restart_seconds = max(0.0, _safe_float(restart_delay_s, 0.0))
    seq = sequence if isinstance(sequence, dict) else {}
    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    phase_clock_sample = (
        dict(clock_sample) if isinstance(clock_sample, dict) else None
    )
    phase_cooldown_seconds = max(30.0, _safe_float(hold_s, 480.0))
    zero_settle_seconds = phase_zero_settle_s(cfg)
    target_settle_seconds = max(zero_settle_seconds, phase_min_settle_s(cfg))
    base = {
        "contract": "openwb_pro_phase_sequence_step_v1",
        "target": int(target),
        "action": "invalid",
        "stage": "",
        "ready": False,
        "command": None,
        "sequence": None,
        "sequence_patch": {},
        "phase_wait_config": {},
        "phase_cooldown_s": float(phase_cooldown_seconds),
        "zero_settle_s": float(zero_settle_seconds),
        "target_settle_s": float(target_settle_seconds),
        "reason": str(reason or "phase_switch"),
        "monotonic_timebase_active": bool(phase_clock_sample),
    }
    if target not in (1, 3):
        base["reason"] = "invalid_phase_target"
        return base

    if not seq or _safe_int(seq.get("target", 0), 0) != target:
        connection_state = _fresh_connection_state(st)
        if connection_state is False:
            return {
                **base,
                "action": "abort_disconnected_before_phase_output",
                "stage": "",
                "reason": "vehicle_disconnected_before_phase_sequence",
            }
        if connection_state is not True:
            return {
                **base,
                "action": "wait_connection_readback",
                "stage": "",
                "reason": "phase_sequence_connection_stale_or_unknown",
            }
        amp_before = max(
            0.0,
            _safe_float(current_set_amp, 0.0),
            _safe_float(st.get("amp", 0), 0.0),
            _safe_float(st.get("offered_current_raw", 0), 0.0),
            _safe_float(st.get("evse_current", 0), 0.0),
        )
        hold_amp = round(max(6.0, amp_before), 1) if amp_before >= 6.0 else 0.0
        next_sequence = {
            "target": target,
            "stage": "zero_wait",
            "reason": str(reason or "phase_switch"),
            "hold_amp": hold_amp,
            "started_ts": now_value,
            "zero_until": now_value + zero_settle_seconds,
            "phase_cooldown_s": phase_cooldown_seconds,
            "phase_change_block_until": 0.0,
            "zero_settle_s": zero_settle_seconds,
            "phase_sent_ts": 0.0,
            "cp_sent_ts": 0.0,
            "current_allowed_after": 0.0,
        }
        stage_timer = _phase_stage_timer(
            "zero_wait",
            zero_settle_seconds,
            None,
            phase_clock_sample,
        )
        if stage_timer is not None:
            next_sequence[PHASE_STAGE_TIMEBASE_KEY] = stage_timer
        return {
            **base,
            "action": "send_zero",
            "stage": "zero_wait",
            "sequence": next_sequence,
            "phase_wait_config": {
                "openwb_pro_phase_wait_s": phase_cooldown_seconds,
            },
            "command": {
                "method": "set_amp_and_state",
                "amp": 0,
                "force_state": 1,
                "reason": "openwb_pro_phase_zero",
                "_openwb_pro_sequence_internal": True,
            },
        }

    stage = str(seq.get("stage") or "zero_wait")
    if stage == "zero_wait":
        connection_state = _fresh_connection_state(st)
        if connection_state is False:
            return {
                **base,
                "action": "abort_disconnected_before_phase_output",
                "stage": "zero_wait",
                "reason": "vehicle_disconnected_before_phasetarget",
            }
        if connection_state is not True:
            return {
                **base,
                "action": "wait_connection_readback",
                "stage": "zero_wait",
                "reason": "phasetarget_connection_stale_or_unknown",
            }
        zero_until = _safe_float(seq.get("zero_until"), now_value + zero_settle_seconds)
        stage_timer = _phase_stage_timer(
            "zero_wait",
            zero_settle_seconds,
            seq.get(PHASE_STAGE_TIMEBASE_KEY, {}),
            phase_clock_sample,
        )
        if stage_timer is not None:
            if stage_timer.get("active"):
                return {
                    **base,
                    "action": "wait_zero",
                    "stage": "zero_wait",
                    "sequence_patch": {
                        PHASE_STAGE_TIMEBASE_KEY: stage_timer,
                    },
                    "reason": str(
                        stage_timer.get("reason")
                        or "zero_wait_monotonic"
                    ),
                }
        elif now_value < zero_until:
            return {**base, "action": "wait_zero", "stage": "zero_wait"}
        if not _zero_readback_confirmed(st):
            return {
                **base,
                "action": "wait_zero_readback",
                "stage": "zero_wait",
                "reason": "zero_readback_not_fresh_or_not_zero",
                "sequence_patch": (
                    {PHASE_STAGE_TIMEBASE_KEY: stage_timer}
                    if stage_timer is not None
                    else {}
                ),
            }
        return {
            **base,
            "action": "send_phase",
            "stage": "restart_delay",
            "phase_wait_config": {
                "openwb_pro_phase_wait_s": _safe_float(
                    seq.get("phase_cooldown_s"),
                    phase_cooldown_seconds,
                ),
            },
            "sequence_patch": {
                "stage": "restart_delay",
                **(
                    {PHASE_STAGE_TIMEBASE_KEY: stage_timer}
                    if stage_timer is not None
                    else {}
                ),
                # Vor dem Treiber-I/O sind dies nur Dauerparameter. Die
                # wirksamen Zeitanker werden erst aus dem erfolgreichen
                # Wire-Receipt gebildet und anschließend persistiert.
                "phase_proposal_ts": now_value,
                "phase_sent_ts": 0.0,
                "wire_receipt_ts": 0.0,
                "phase_settle_s": target_settle_seconds,
                "phase_change_block_until": 0.0,
                "current_allowed_after": 0.0,
            },
            "command": {
                "method": "set_phases",
                "phases": target,
                # Diese Kante besitzt bereits einen persistierten Intent und
                # braucht deshalb zwingend einen neuen Wire-Beleg. Ein alter
                # identischer Soll-Readback oder Treiber-Dedup darf sie nicht
                # in einen unbehebbaren Recovery-Hold verwandeln.
                "_require_wire_receipt": True,
                "reason": "openwb_pro_phase_target",
                "_openwb_pro_sequence_internal": True,
            },
        }

    if stage == "cp_after_phase":
        # Kompatibilitätsaufnahme eines vor dem Fix persistierten Zustands:
        # keine zweite Wire-Kante erzeugen, sondern die noch offene
        # Wiederanlaufsperre ab dem bereits gesendeten phasetarget übernehmen.
        if not _phase_readback_confirmed(st, target):
            return {
                **base,
                "action": "wait_phase_readback",
                "stage": "cp_after_phase",
                "reason": "target_phase_not_freshly_confirmed",
            }
        phase_sent_ts = _safe_float(seq.get("phase_sent_ts"), now_value)
        settle_s = target_settle_seconds
        stage_timer = _phase_stage_timer(
            "restart_delay",
            settle_s + restart_seconds,
            None,
            phase_clock_sample,
        )
        return {
            **base,
            "action": "adopt_phase_settle",
            "stage": "restart_delay",
            "sequence_patch": {
                "stage": "restart_delay",
                "phase_sent_ts": phase_sent_ts,
                "phase_settle_s": settle_s,
                "phase_change_block_until": max(
                    _safe_float(seq.get("phase_change_block_until"), 0.0),
                    phase_sent_ts + phase_cooldown_seconds,
                ),
                "current_allowed_after": phase_sent_ts + settle_s + restart_seconds,
                **(
                    {PHASE_STAGE_TIMEBASE_KEY: stage_timer}
                    if stage_timer is not None
                    else {}
                ),
            },
            "command": None,
            "reason": "legacy_phase_state_adopted_without_extra_cp",
        }

    if stage == "restart_delay":
        phase_sent_ts = _safe_float(seq.get("phase_sent_ts"), now_value)
        if "wire_receipt_ts" not in seq:
            # Vor diesem Vertrag persistierte Sequenzen besitzen nur den
            # damaligen Vorschlagszeitpunkt. Er darf nicht rückwirkend als
            # erfolgreicher POST-Beleg gelten. Ein danach frisch gelesener,
            # verbundener und physisch ruhiger Zielzustand wird stattdessen
            # selbst zum konservativen Settle-Anker.
            readback_ts = _safe_float(st.get("driver_status_last_ok_ts"), 0.0)
            if readback_ts > 100_000_000_000.0:
                readback_ts /= 1000.0
            legacy_readback_confirmed = bool(
                readback_ts > max(0.0, phase_sent_ts)
                and _fresh_connection_state(st) is True
                and _zero_readback_confirmed(st)
                and _phase_readback_confirmed(st, target)
                and st.get("cp_interrupt_isactive") in (False, 0)
            )
            if not legacy_readback_confirmed:
                return {
                    **base,
                    "action": "wait_post_wire_readback",
                    "stage": "restart_delay",
                    "reason": "legacy_phase_sequence_requires_fresh_idle_target_readback",
                }
            stage_timer = _phase_stage_timer(
                "restart_delay",
                target_settle_seconds + restart_seconds,
                None,
                phase_clock_sample,
            )
            return {
                **base,
                "action": "adopt_phase_settle",
                "stage": "restart_delay",
                "sequence_patch": {
                    "stage": "restart_delay",
                    "phase_sent_ts": readback_ts,
                    "wire_receipt_ts": readback_ts,
                    "phase_settle_s": target_settle_seconds,
                    "phase_change_block_until": max(
                        _safe_float(seq.get("phase_change_block_until"), 0.0),
                        readback_ts + phase_cooldown_seconds,
                    ),
                    "current_allowed_after": (
                        readback_ts + target_settle_seconds + restart_seconds
                    ),
                    **(
                        {PHASE_STAGE_TIMEBASE_KEY: stage_timer}
                        if stage_timer is not None
                        else {}
                    ),
                },
                "command": None,
                "reason": "legacy_restart_delay_reanchored_from_fresh_readback",
            }
        wire_receipt_ts = _safe_float(
            seq.get("wire_receipt_ts"),
            phase_sent_ts,
        )
        if "wire_receipt_ts" in seq and wire_receipt_ts <= 0.0:
            return {
                **base,
                "action": "wait_wire_receipt",
                "stage": "restart_delay",
                "reason": "phase_wire_receipt_missing",
            }
        # openWB besitzt mit ``phasetarget`` die CP-/Umschaltpause. Die lange
        # 480-s-Frist schützt ausschließlich vor einem *weiteren*
        # Phasenwechsel; sie darf die Stromfreigabe nach bestätigtem Ziel nicht
        # blockieren. Auch vor dem Fix persistierte 480-s-Werte werden deshalb
        # auf den kurzen, bounded Ziel-Settle-Vertrag normalisiert.
        phase_settle_seconds = target_settle_seconds
        allowed_after = phase_sent_ts + phase_settle_seconds + restart_seconds
        stage_timer = _phase_stage_timer(
            "restart_delay",
            phase_settle_seconds + restart_seconds,
            seq.get(PHASE_STAGE_TIMEBASE_KEY, {}),
            phase_clock_sample,
        )
        if (
            stage_timer is not None
            and stage_timer.get("active")
        ) or (
            stage_timer is None
            and now_value < allowed_after
        ):
            return {
                **base,
                "action": "wait_restart",
                "stage": "restart_delay",
                "sequence_patch": {
                    "current_allowed_after": allowed_after,
                    **(
                        {PHASE_STAGE_TIMEBASE_KEY: stage_timer}
                        if stage_timer is not None
                        else {}
                    ),
                },
                "reason": (
                    str(stage_timer.get("reason") or "restart_delay_monotonic")
                    if stage_timer is not None
                    else str(base.get("reason") or "phase_switch")
                ),
            }
        if not _fresh_valid_status(st):
            return {
                **base,
                "action": "wait_fresh_readback",
                "stage": "restart_delay",
                "reason": "phase_release_status_stale_or_unknown",
            }
        if (
            "wire_receipt_ts" in seq
            and wire_receipt_ts > 0.0
            and not _post_wire_readback_confirmed(
                st,
                target,
                wire_receipt_ts,
            )
        ):
            return {
                **base,
                "action": "wait_post_wire_readback",
                "stage": "restart_delay",
                "reason": "target_phase_readback_not_after_wire_receipt",
            }
        if bool(st.get("cp_interrupt_isactive", True)):
            return {
                **base,
                "action": "wait_cp_inactive",
                "stage": "restart_delay",
                "reason": "cp_interrupt_still_active",
            }
        if not _phase_readback_confirmed(st, target):
            return {
                **base,
                "action": "wait_target_phase",
                "stage": "restart_delay",
                "reason": "target_phase_not_confirmed",
            }
        return {
            **base,
            "action": "ready",
            "stage": "ready",
            "ready": True,
            "sequence": dict(seq),
        }

    return {**base, "action": "unknown_stage", "stage": stage, "reason": "unknown_phase_sequence_stage"}


def start_wakeup_step_contract(
    method: Any,
    amp: Any,
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    cp_payload: Optional[Dict[str, Any]] = None,
    cp_source: str = "",
    window: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Liefert die begrenzten Wake-up-Schritte je Stecksession.

    Ein CP-Impuls ersetzt nie einen Strombefehl. Nur der
    Wake-up-Tick (``cp_source="wakeup_tick"``) darf ``send_cp_interrupt``
    erhalten, und nur wenn das Startfenster ihn als fällig meldet
    (``window["cp_due"]``); jeder andere Aufrufer bekommt ``allow_without_cp``.

    Der erste positive Sollstrom wird immer zuerst gesendet. Benötigt das
    wirksame Fahrzeugprofil eine CP-Unterbrechung, folgt der erste kurze
    ``cp_interrupt=true`` im unmittelbar nächsten freien Ausgangszyklus. Nur
    weitere erfolglose Wiederholungen warten den konfigurierten Retry-Abstand.
    Ein persistierter Receipt verhindert einen Neustart des Zählers nach
    Manager-Neustarts.
    """

    data = state_data if isinstance(state_data, dict) else {}
    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    method_text = str(method or "").strip()
    amp_value = _safe_float(amp, 0.0)
    delay_s = start_wakeup_delay_s(cfg)
    # Untergrenze 60 s; die Wiederholung bestimmt der Zyklus des Startfensters.
    retry_s = max(60.0, _safe_float(cfg.get("wb_openwb_start_retry_s"), 45.0))
    plug_session_id = str(data.get("_openwb_pro_plug_session_id") or "")
    issued_session_id = str(data.get("_openwb_pro_start_current_session_id") or "")
    issued_ts = _safe_float(data.get("_openwb_pro_start_current_issued_ts"), 0.0)
    receipt = (
        data.get("_openwb_pro_start_wakeup_receipt")
        if isinstance(data.get("_openwb_pro_start_wakeup_receipt"), dict)
        else {}
    )
    receipt_matches = bool(
        plug_session_id
        and str(receipt.get("plug_session_id") or "") == plug_session_id
    )
    sent_count = (
        max(0, _safe_int(receipt.get("count"), 0))
        if receipt_matches
        else 0
    )
    max_retries = start_cp_retry_limit(cfg)
    cp_capability = automatic_start_cp_contract(
        cfg,
        st,
        is_openwb_pro=True,
    )
    grace_s = start_cp_grace_s(cfg, cp_capability)
    base = {
        "contract": "openwb_pro_start_wakeup_step_v1",
        "action": "allow",
        "allow": True,
        "reason": "allow",
        "command": None,
        "command_patch": {},
        "state_patch": {},
        "success_state_patch": {},
        "delay_s": float(delay_s),
        "grace_s": float(grace_s),
        "retry_s": float(retry_s),
        "max_retries": int(max_retries),
        "sent_count": int(sent_count),
        "phase_wait_active": False,
        "plug_session_id": plug_session_id,
        "current_issued_ts": float(issued_ts),
        "cp_capability": cp_capability,
    }

    if method_text not in ("set_amp_and_state", "set_current", "set_direct_current"):
        return {**base, "action": "non_start_command", "reason": "not_current_command"}
    if amp_value < 6.0:
        return {
            **base,
            "action": "clear_below_min_current",
            "reason": "below_min_current",
            "state_patch": {
                "_openwb_pro_start_wakeup_allowed_after": 0.0,
                "_openwb_pro_start_wakeup_pending": False,
            },
        }
    if bool(data.get("_bev_full_blocked", False)):
        return {**base, "action": "blocked_vehicle_finished", "allow": False, "reason": "bev_full_blocked"}

    session = data.get("_openwb_pro_session") if isinstance(data.get("_openwb_pro_session"), dict) else {}
    if bool(
        data.get("_emergency_stop_active", False)
        or data.get("_notaus_active", False)
        or session.get("start_blocked", False)
        or session.get("state") in (STATE_STOPPING, STATE_ENDED)
    ):
        return {
            **base,
            "action": "blocked_session_or_safety_stop",
            "allow": False,
            "reason": "session_or_safety_stop",
        }

    sequence = data.get("_openwb_pro_phase_sequence")
    if isinstance(sequence, dict) and sequence and str(sequence.get("stage") or "") != "ready":
        return {**base, "action": "blocked_phase_sequence", "allow": False, "reason": "phase_sequence_active"}

    command_patch: Dict[str, Any] = {}
    last_sequence = data.get("_openwb_pro_phase_sequence_last")
    if isinstance(last_sequence, dict):
        allowed_after = _safe_float(last_sequence.get("current_allowed_after"), 0.0)
        phase_restart_grace_s = max(60.0, phase_restart_delay_s(cfg) + 30.0)
        if allowed_after > 0.0 and allowed_after <= now_value <= allowed_after + phase_restart_grace_s:
            command_patch["_guard_allow_restart_after_stop"] = True

    connected = bool(
        st.get("plug_state")
        or st.get("car") == 2
        or st.get("plug")
        or st.get("connected")
        or _explicit_connection_state(st) is True
    )

    real_power_w = max(
        _safe_float(st.get("real_power_w"), 0.0),
        _safe_float(st.get("phase_power_sum_w"), 0.0),
        _safe_float(st.get("power_w"), 0.0),
    )
    charging = bool(st.get("charging") or st.get("charge_state") or real_power_w > 500.0)
    if not connected:
        return {
            **base,
            "action": "wait_confirmed_disconnect",
            "allow": False,
            "reason": "single_disconnected_frame_does_not_reset_plug_session",
            "command_patch": command_patch,
        }

    allowed_after = _safe_float(data.get("_openwb_pro_start_wakeup_allowed_after"), 0.0)
    if allowed_after > now_value and real_power_w <= 500.0:
        return {
            **base,
            "action": "wait_wakeup_delay",
            "allow": False,
            "reason": "wakeup_delay_active",
            "command_patch": command_patch,
            "state_patch": {"_openwb_pro_start_wakeup_pending": True},
        }
    had_confirmed_charge = start_offer_physics_confirmed(st)
    if had_confirmed_charge:
        return {
            **base,
            "action": "clear_connected_or_charging",
            "reason": "not_connected_or_already_charging",
            "command_patch": command_patch,
            "state_patch": {
                "_openwb_pro_start_wakeup_allowed_after": 0.0,
                "_openwb_pro_start_wakeup_pending": False,
                "_openwb_pro_start_wakeup_count": 0,
                "_openwb_pro_unserved_since": 0.0,
                "_openwb_pro_unserved_pause_since": 0.0,
                "_openwb_pro_unserved_paused_s": 0.0,
            },
        }

    phase_sequence = data.get("_openwb_pro_phase_sequence")
    phase_sequence_active = bool(
        isinstance(phase_sequence, dict)
        and phase_sequence
        and str(phase_sequence.get("stage") or "") not in ("", "ready")
    )
    phase_wait_until = _safe_float(data.get("_openwb_pro_phase_wait_until"), 0.0)
    phase_wait_target = _valid_phase_count(data.get("_openwb_pro_phase_wait_target"), 0)
    phase_wait_active = bool(
        phase_sequence_active
        or (phase_wait_target in (1, 3) and phase_wait_until > now_value)
    )
    if phase_wait_active:
        return {
            **base,
            "action": "blocked_phase_sequence",
            "allow": False,
            "reason": "phase_sequence_no_cp",
            "command_patch": command_patch,
            "phase_wait_active": True,
        }

    if not _fresh_valid_status(st):
        return {
            **base,
            "action": "allow_without_cp",
            "allow": True,
            "reason": "status_not_fresh_cp_suppressed",
            "command_patch": command_patch,
        }

    if not plug_session_id or issued_session_id != plug_session_id or issued_ts <= 0.0:
        return {
            **base,
            "action": "allow_initial_current",
            "reason": "current_first",
            "command_patch": command_patch,
        }

    if bool(data.get("_openwb_pro_start_cp_persistence_blocked", False)):
        return {
            **base,
            "action": "allow_without_cp",
            "reason": "start_receipt_persistence_failed",
            "command_patch": command_patch,
        }

    if sent_count >= max_retries:
        return {
            **base,
            "action": "allow_after_bounded_cp",
            "reason": "bounded_cp_attempts_consumed",
            "command_patch": {**command_patch, "_guard_allow_restart_after_stop": True},
            "state_patch": {"_openwb_pro_start_wakeup_pending": False},
        }

    if not cp_capability.get("enabled", False):
        return {
            **base,
            "action": "allow_without_cp",
            "reason": str(cp_capability.get("reason") or "automatic_cp_disabled"),
            "command_patch": command_patch,
        }

    last_cp_ts = (
        _safe_float(receipt.get("last_sent_ts"), 0.0)
        if receipt_matches
        else 0.0
    )
    if sent_count > 0 and last_cp_ts <= 0.0:
        return {
            **base,
            "action": "allow_without_cp",
            "reason": "cp_receipt_timestamp_missing",
            "command_patch": {**command_patch, "_guard_allow_restart_after_stop": True},
        }
    if sent_count > 0 and now_value - last_cp_ts < retry_s:
        return {
            **base,
            "action": "allow_between_bounded_cp",
            "reason": "bounded_cp_retry_interval",
            "command_patch": {**command_patch, "_guard_allow_restart_after_stop": True},
        }
    if sent_count == 0 and grace_s > 0.0 and now_value - issued_ts < grace_s:
        # Karenz: das erste Angebot darf ohne Unterbrechung zum Start führen;
        # der Impuls kommt erst, wenn danach noch keine Ladung läuft.
        return {
            **base,
            "action": "allow_within_start_grace",
            "reason": "start_cp_grace_active",
            "grace_remaining_s": round(max(0.0, grace_s - (now_value - issued_ts)), 1),
            "command_patch": command_patch,
        }

    cp_state = st.get("cp_interrupt_isactive")
    cp_active = bool(
        cp_state is True
        or (
            isinstance(cp_state, (int, float))
            and not isinstance(cp_state, bool)
            and float(cp_state) != 0.0
        )
    )
    if cp_active:
        return {
            **base,
            "action": "wait_active_cp_interrupt",
            "allow": False,
            "reason": "cp_interrupt_active",
            "command_patch": command_patch,
            "state_patch": {
                "_openwb_pro_start_wakeup_allowed_after": now_value + delay_s,
                "_openwb_pro_start_wakeup_pending": True,
            },
        }
    if not _explicit_inactive(cp_state):
        return {
            **base,
            "action": "allow_without_cp",
            "allow": True,
            "reason": "cp_state_unknown_current_retry",
            "command_patch": command_patch,
            "state_patch": {
                "_openwb_pro_start_wakeup_pending": False,
            },
        }

    if str(cp_source or "") != "wakeup_tick":
        # Der Strombefehl geht immer raus; der Impuls
        # kommt ausschließlich aus dem Wake-up-Tick.
        return {
            **base,
            "action": "allow_without_cp",
            "allow": True,
            "reason": "cp_only_from_wakeup_tick",
            "command_patch": command_patch,
        }
    if window is not None and not bool((window or {}).get("cp_due", False)):
        return {
            **base,
            "action": "allow_without_cp",
            "allow": True,
            "reason": "start_window_cp_not_due",
            "command_patch": command_patch,
        }

    payload = cp_payload if isinstance(cp_payload, dict) else {}
    return {
        **base,
        "action": "send_cp_interrupt",
        "allow": False,
        "reason": "send_cp_interrupt",
        "command_patch": command_patch,
        "command": {
            "method": "trigger_cp_interrupt",
            "reason": "openwb_pro_start_wakeup",
            "_openwb_pro_sequence_internal": True,
            **payload,
        },
        "success_state_patch": {
            "_openwb_pro_start_wakeup_cp_ts": now_value,
            "_openwb_pro_start_wakeup_allowed_after": now_value + delay_s,
            "_openwb_pro_start_wakeup_pending": True,
            "_openwb_pro_start_wakeup_count": sent_count + 1,
            "_openwb_cp_start_sent": True,
            "_openwb_last_cp_start_ts": now_value,
            "_openwb_pro_start_wakeup_receipt": {
                "schema": "openwb_pro_start_wakeup_receipt_v1",
                "plug_session_id": plug_session_id,
                "count": sent_count + 1,
                "first_sent_ts": _safe_float(receipt.get("first_sent_ts"), now_value),
                "last_sent_ts": now_value,
                "current_issued_ts": issued_ts,
            },
        },
    }


def vehicle_finished_drop_contract(
    status: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    is_manager_charging: bool = False,
    current_set_amp: Any = 0,
    time_since_start_s: Any = 0,
    startup_grace_s: Any = 90,
    min_amp: Any = 6,
    had_confirmed_charge: Optional[bool] = None,
    observed_charge_in_run: bool = False,
    observe_only: bool = False,
    openwb_mode9_monitor: bool = False,
    stop_sent_active: bool = False,
    manager_zero_anchor_active: bool = False,
    recent_manager_stop: bool = False,
    recent_manager_start_retry: bool = False,
    phase_transition_active: bool = False,
    openwb_pro_phase_transition_active: bool = False,
    required_drop_s: Any = 45,
    required_drop_frames: Any = 3,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Classify an openWB Pro power drop before the central charge-end latch.

    This contract does not decide or send a stop.  It only says whether the
    live drop is a safe candidate for ``charge_end_latch_contract``.  Manager
    stops, phase pauses, wake-up retries and unconfirmed start attempts remain
    visible blockers instead of being mistaken for a finished vehicle. Ein
    einzelner Nullleistungs-Frame reicht ausdrücklich nicht: Das Ladeende muss
    über mehrere frische Gerätestatus und eine Mindestdauer bestätigt sein.
    """

    st = status if isinstance(status, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    min_current = max(1.0, _safe_float(min_amp, 6.0))
    current_amp = max(0.0, _safe_float(current_set_amp, 0.0))
    time_since_start = max(0.0, _safe_float(time_since_start_s, 0.0))
    grace_s = max(0.0, _safe_float(startup_grace_s, 90.0))
    confirm_s = max(15.0, _safe_float(required_drop_s, 45.0))
    confirm_frames = max(2, _safe_int(required_drop_frames, 3))
    connected = status_connected(st)
    real_power_w = max(
        status_real_power(st),
        _safe_float(st.get("real_power_w"), 0.0) if bool(st.get("charging") or st.get("charge_state")) else 0.0,
    )
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    confirmed = (
        bool(data.get("_aha_real_charge_confirmed", False))
        if had_confirmed_charge is None
        else bool(had_confirmed_charge)
    )
    hardware_amp = max(
        _safe_float(st.get("amp", 0), 0.0),
        _safe_float(st.get("offered_current_raw", 0), 0.0),
        _safe_float(st.get("evse_current", 0), 0.0),
    )
    plug_session_id = str(
        data.get("_openwb_pro_plug_session_id") or ""
    )
    previous = data.get("_openwb_pro_vehicle_finished_candidate")
    previous = dict(previous) if isinstance(previous, dict) else {}
    previous_session_id = str(previous.get("plug_session_id") or "")
    pending_same_session = bool(
        previous
        and previous_session_id
        and previous_session_id == plug_session_id
        and _safe_int(previous.get("frames"), 0) > 0
        and previous.get("confirmed") is not True
    )
    manager_session_active = bool(
        is_manager_charging or pending_same_session or observed_charge_in_run
    )
    sample_ts = max(
        _safe_float(st.get("driver_status_last_sample_ts"), 0.0),
        _safe_float(st.get("driver_status_last_ok_ts"), 0.0),
    )
    charge_truth = status_charge_truth(st)
    status_fresh = bool(
        _fresh_valid_status(st)
        and charge_truth == "not_charging"
        and sample_ts > 0.0
        and now_value >= sample_ts - 2.0
        and now_value - sample_ts <= 30.0
    )

    blockers = []
    if not manager_session_active:
        blockers.append("manager_not_in_charge_session")
    if not connected:
        blockers.append("vehicle_not_connected")
    if not plug_session_id:
        blockers.append("plug_session_missing")
    if not status_fresh:
        blockers.append("status_not_fresh")
    if real_charging:
        blockers.append("real_charge_still_active")
    # Nach tatsächlich gemessener Ladung kann die ruhige Drop-Prüfung schon
    # während der Startfrist beginnen. Die endgültige Bestätigung wartet
    # weiterhin sowohl diese Frist als auch die vollständige Drop-Dauer ab.
    if time_since_start <= grace_s and not observed_charge_in_run:
        blockers.append("startup_grace_active")
    if current_amp < min_current:
        blockers.append("no_minimum_manager_current_offer")
    if bool(observe_only):
        blockers.append("observe_only")
    if bool(openwb_mode9_monitor):
        blockers.append("openwb_mode9_monitor")
    if bool(stop_sent_active):
        blockers.append("manager_stop_already_sent")
    if bool(manager_zero_anchor_active):
        blockers.append("manager_zero_anchor_active")
    if bool(recent_manager_stop):
        blockers.append("recent_manager_stop")
    if bool(recent_manager_start_retry):
        blockers.append("recent_manager_start_retry")
    if bool(phase_transition_active):
        blockers.append("phase_transition_active")
    if bool(openwb_pro_phase_transition_active):
        blockers.append("openwb_pro_phase_transition_active")
    if not confirmed:
        blockers.append("no_confirmed_real_charge")

    candidate: Dict[str, Any] = {}
    drop_age_s = 0.0
    drop_frames = 0
    drop_confirmed = False
    if not blockers:
        since_ts = _safe_float(previous.get("since_ts"), 0.0)
        last_sample_ts = _safe_float(previous.get("last_sample_ts"), 0.0)
        drop_frames = max(0, _safe_int(previous.get("frames"), 0))
        if (
            since_ts <= 0.0
            or sample_ts < last_sample_ts
            or previous_session_id != plug_session_id
        ):
            since_ts = sample_ts
            last_sample_ts = 0.0
            drop_frames = 0
        if sample_ts > last_sample_ts + 1e-6:
            drop_frames += 1
            last_sample_ts = sample_ts
        drop_age_s = max(0.0, now_value - since_ts)
        drop_confirmed = bool(
            drop_frames >= confirm_frames
            and drop_age_s >= confirm_s
            and time_since_start > grace_s
        )
        candidate = {
            "since_ts": float(since_ts),
            "last_sample_ts": float(last_sample_ts),
            "frames": int(drop_frames),
            "confirmed": bool(drop_confirmed),
            "plug_session_id": plug_session_id,
        }
        if not drop_confirmed:
            blockers.append("drop_confirmation_pending")

    allow = bool(drop_confirmed and not blockers)
    action = "candidate" if allow else ("wait" if candidate else "ignore")
    reason = (
        "vehicle_finished_candidate"
        if allow
        else ("drop_confirmation_pending" if candidate else blockers[0])
    )
    return {
        "contract": "openwb_pro_vehicle_finished_drop_v2",
        "action": action,
        "allow_new_latch": bool(allow),
        "reason": reason,
        "blockers": blockers,
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "real_power_w": float(real_power_w if real_charging else 0.0),
        "manager_charging": bool(manager_session_active),
        "manager_session_continued_from_candidate": bool(
            pending_same_session and not is_manager_charging
        ),
        "current_set_amp": float(current_amp),
        "hardware_amp": float(hardware_amp),
        "status_fresh": bool(status_fresh),
        "charge_truth": str(charge_truth),
        "plug_session_id": plug_session_id,
        "min_amp": float(min_current),
        "time_since_start_s": float(time_since_start),
        "startup_grace_s": float(grace_s),
        "had_confirmed_charge": bool(confirmed),
        "observed_charge_in_run": bool(observed_charge_in_run),
        "drop_confirmed": bool(drop_confirmed),
        "drop_age_s": float(drop_age_s),
        "drop_frames": int(drop_frames),
        "required_drop_s": float(confirm_s),
        "required_drop_frames": int(confirm_frames),
        "remaining_drop_s": max(0.0, float(confirm_s - drop_age_s),
                                grace_s - time_since_start if observed_charge_in_run else 0.0),
        "candidate": candidate,
        "state_patch": {
            "_openwb_pro_vehicle_finished_candidate": candidate or None,
        },
        "ts": float(now_value),
    }


PARKED_FINISH_EVIDENCE_SCHEMA = "openwb_pro_parked_finish_evidence_v1"
PARKED_FINISH_RESTORE_SCHEMA = (
    "openwb_pro_parked_finish_evidence_restore_v1"
)


def parked_finish_evidence_restore_contract(
    parked: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    *,
    plug_session_id: Any = "",
    zero_anchor_active: bool = False,
    stop_sent_active: bool = False,
    manual_pause: bool = False,
    mode_off: bool = False,
    locked: bool = False,
    emergency_stop: bool = False,
    last_manager_stop_ts: Any = 0.0,
    now_ts: Any = 0.0,
    max_park_s: Any = 0.0,
    user_start_requested: bool = False,
) -> Dict[str, Any]:
    """Entscheidet, ob ein geparkter Ladeendebeleg wieder gelten darf.

    Der Beleg ist an die Stecksession gebunden: ``max_park_s`` <= 0 bedeutet
    keine Zeitgrenze. Ein ausdrücklicher Nutzerstart derselben Session
    verwirft ihn ebenso wie neues Abstecken.

    Ein temporärer Budget-Nullanker ist kein Beleg gegen ein bereits
    beobachtetes Fahrzeug-Ladeende. Er ist aber auch keine Bestätigung: Solange
    der Manager selbst 0 A anbietet, beweist ein ausbleibender Wiederstart
    nichts. Der Beleg wird deshalb geparkt und erst wieder gültig, wenn der
    Anker gelöst ist, dieselbe Stecksession weiterläuft und keine
    widersprechende Evidenz vorliegt.

    Der Vertrag verkürzt keine Frist. Er liefert ``resume_shift_s``, um die
    Drop-Uhr genau um die Parkdauer nach vorn zu schieben; die Bestätigung
    braucht danach unverändert ihre vollen Frames und Sekunden aus Proben mit
    tatsächlichem Mindestangebot. Nutzer-Aus, Nutzerpause, Sperre, Notaus, ein
    gesendeter Stopp, ein neuer Managerstopp nach dem Parken, eine fremde
    Stecksession, reale Ladung, eine CP-Unterbrechung sowie fehlende oder
    unfrische Statusevidenz verwerfen den Beleg.
    """

    item = parked if isinstance(parked, dict) else {}
    st = status if isinstance(status, dict) else {}
    session_id = str(plug_session_id or "")
    now_value = _safe_float(now_ts, 0.0)
    parked_since = _safe_float(item.get("parked_since_ts"), 0.0)
    park_limit_s = max(0.0, _safe_float(max_park_s, 0.0))
    result = {
        "contract": PARKED_FINISH_RESTORE_SCHEMA,
        "restore": False,
        "discard": False,
        "reason": "no_parked_evidence",
        "resume_shift_s": 0.0,
        "parked_s": 0.0,
        "plug_session_id": session_id,
    }
    if not item or item.get("contract") != PARKED_FINISH_EVIDENCE_SCHEMA:
        return result
    parked_s = max(0.0, now_value - parked_since)
    result["parked_s"] = parked_s

    def discard(reason: str) -> Dict[str, Any]:
        result["discard"] = True
        result["reason"] = reason
        return result

    if not session_id or str(item.get("plug_session_id") or "") != session_id:
        return discard("foreign_plug_session")
    if item.get("reason_code") not in RELEASABLE_ZERO_ANCHOR_REASONS:
        return discard("anchor_not_releasable")
    if parked_since <= 0.0 or now_value <= 0.0:
        return discard("parked_timebase_missing")
    if bool(user_start_requested):
        return discard("user_start_request")
    if park_limit_s > 0.0 and parked_s > park_limit_s:
        return discard("parked_too_long")
    if bool(manual_pause) or bool(mode_off) or bool(locked) or bool(emergency_stop):
        return discard("user_or_safety_state")
    if bool(stop_sent_active):
        return discard("manager_stop_active")
    if _safe_float(last_manager_stop_ts, 0.0) > parked_since + 1e-6:
        return discard("newer_manager_stop")
    if not status_connected(st):
        return discard("vehicle_not_connected")
    if status_real_charging(st) or status_real_power(st) > 500.0:
        return discard("real_charge_resumed")
    if bool(st.get("cp_interrupt_isactive", False)):
        return discard("cp_interrupt_active")
    if not _fresh_valid_status(st):
        result["reason"] = "status_not_fresh"
        return result
    if bool(zero_anchor_active):
        # Weiter geparkt: Der Anker haelt noch, es wird nichts bestaetigt.
        result["reason"] = "still_parked"
        return result
    result["restore"] = True
    result["reason"] = "resume_after_budget_pause"
    result["resume_shift_s"] = parked_s
    return result


VEHICLE_OFFER_HOLD_SCHEMA = "openwb_pro_vehicle_offer_hold_v1"


def vehicle_offer_hold_contract(
    *,
    plug_session_id: Any = "",
    reason_code: Any = "",
    status: Optional[Dict[str, Any]] = None,
    offered_amp: Any = 0.0,
    min_amp: Any = 6.0,
    finish_candidate: bool = False,
    charge_observation: bool = False,
    parked_evidence: bool = False,
    vehicle_idle: bool = False,
    real_charge_confirmed: bool = False,
    stop_sent_active: bool = False,
    manual_pause: bool = False,
    mode_off: bool = False,
    locked: bool = False,
    emergency_stop: bool = False,
    explicit_stop_blocked: bool = False,
    start_offer_age_s: Any = None,
    start_grace_s: Any = 0.0,
    window_active: bool = False,
) -> Dict[str, Any]:
    """Hält ein Mindestangebot ohne Abnahme statt eines vorübergehenden 0 A.

    ``window_active`` (Startfenster der openWB Pro hält das
    Angebot) erzwingt das Halten unabhängig von Ladeende-Evidenz;
    Nutzer-/Sicherheitszustände und harte Kanten bleiben vorrangig.

    Start-Schutzfenster: Ein frisch ausgegebenes
    Startangebot braucht die Annahmelatenz des Fahrzeugs (typisch 10–30 s).
    Solange ``start_offer_age_s`` unter ``start_grace_s`` liegt und das
    Fahrzeug noch nicht zieht, gilt das Angebot ebenfalls als zu haltender
    Bestand; ein Budget-Dip in diesen Sekunden erzeugt sonst Start/Stop.

    Hintergrund: Nach einem App-Stopp des Fahrzeugs setzte die
    Budgetkette den Rahmen des Ladepunkts auf 0 W, obwohl kein Watt mehr floss
    und PV eingespeist wurde. Der Manager gab 0 A aus, die Ladeende-
    Bestätigung brach ab, und das nächste Neuangebot ließ das Fahrzeug gegen
    den Nutzerstopp wieder anlaufen.

    Ein Angebot an ein nicht ziehendes Fahrzeug kostet nichts. Solange in
    derselben Stecksession ein Ladeende geprüft wird, ein geparkter Beleg
    vorliegt oder Bereitschaft besteht, ersetzt dieser Vertrag jeden
    vorübergehenden Policy-Nullausgang (lösbare Ankergründe) durch das Halten
    des bestehenden Mindestangebots. Nutzer-Aus, Nutzerpause, Sperre, Notaus,
    harte oder explizite Stoppkanten, ein bereits gesendeter Stopp, fehlende
    frische Statusevidenz, ein getrenntes oder real ladendes Fahrzeug und ein
    Angebot unter Mindeststrom lassen den bisherigen Pfad unverändert.
    """

    st = status if isinstance(status, dict) else {}
    code = str(reason_code or "")
    minimum = max(6.0, _safe_float(min_amp, 6.0))
    offered = _safe_float(offered_amp, 0.0)
    result = {
        "contract": VEHICLE_OFFER_HOLD_SCHEMA,
        "hold": False,
        "reason": "",
        "reason_code": code,
        "hold_amp": 0.0,
        "plug_session_id": str(plug_session_id or ""),
    }

    def no_hold(reason: str) -> Dict[str, Any]:
        result["reason"] = reason
        return result

    if not str(plug_session_id or ""):
        return no_hold("no_plug_session")
    if code not in RELEASABLE_ZERO_ANCHOR_REASONS:
        return no_hold("anchor_not_releasable")
    if bool(manual_pause) or bool(mode_off) or bool(locked) or bool(emergency_stop):
        return no_hold("user_or_safety_state")
    if bool(explicit_stop_blocked):
        return no_hold("explicit_stop_blocked")
    if bool(stop_sent_active):
        return no_hold("manager_stop_active")
    if not _fresh_valid_status(st):
        return no_hold("status_not_fresh")
    if not status_connected(st):
        return no_hold("vehicle_not_connected")
    if status_real_charging(st) or status_real_power(st) > 250.0:
        return no_hold("vehicle_drawing")
    if offered < minimum:
        return no_hold("no_minimum_offer_to_hold")
    vehicle_end = bool(
        finish_candidate or charge_observation or parked_evidence
        or vehicle_idle or real_charge_confirmed
    )
    grace = max(0.0, _safe_float(start_grace_s, 0.0))
    age = _safe_float(start_offer_age_s, -1.0) if start_offer_age_s is not None else -1.0
    start_grace = bool(grace > 0.0 and 0.0 <= age < grace)
    if not (vehicle_end or start_grace or bool(window_active)):
        return no_hold("no_vehicle_end_evidence")
    result["hold"] = True
    result["hold_amp"] = offered
    result["reason"] = (
        "vehicle_idle_offer_hold" if vehicle_idle
        else "finish_confirm_offer_hold" if vehicle_end
        else "start_window_offer_hold" if bool(window_active)
        else "start_grace_offer_hold"
    )
    result["start_grace_remaining_s"] = (
        round(grace - age, 1) if start_grace and not vehicle_end else 0.0
    )
    return result


def start_window_hlc_factor(evse_signaling: Any = None) -> float:
    """Faktor auf T_HOLD/T_CYCLE/GRACE – 1,0 nur bei reinem PWM."""

    text = str(evse_signaling or "").strip().lower()
    return 1.0 if text in START_WINDOW_BASIC_SIGNALING else float(START_WINDOW_HLC_FACTOR)


def _start_window_bounded_cfg(cfg: Dict[str, Any], key: str, default: float, low: float, high: float) -> float:
    raw = cfg.get(key)
    if raw is None or isinstance(raw, bool) or str(raw).strip() == "":
        return float(default)
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(value) or value < low or value > high:
        return float(default)
    return float(value)


def start_window_hold_s(config: Optional[Dict[str, Any]] = None, evse_signaling: Any = None) -> float:
    """T_HOLD: Fensterlänge ab Readback und Abschaltverzögerung ohne Budget (60–600 s, Standard 180)."""

    cfg = config if isinstance(config, dict) else {}
    return _start_window_bounded_cfg(
        cfg, "openwb_pro_start_hold_s", START_WINDOW_HOLD_DEFAULT_S,
        START_WINDOW_HOLD_MIN_S, START_WINDOW_HOLD_MAX_S,
    ) * start_window_hlc_factor(evse_signaling)


def start_retry_cycle_s(config: Optional[Dict[str, Any]] = None, evse_signaling: Any = None) -> float:
    """T_CYCLE: Wiederholzyklus des Startfensters (180–1200 s, Standard 300)."""

    cfg = config if isinstance(config, dict) else {}
    return _start_window_bounded_cfg(
        cfg, "openwb_pro_start_retry_cycle_s", START_WINDOW_RETRY_CYCLE_DEFAULT_S,
        START_WINDOW_RETRY_CYCLE_MIN_S, START_WINDOW_RETRY_CYCLE_MAX_S,
    ) * start_window_hlc_factor(evse_signaling)


def start_window_cp_grace_s(config: Optional[Dict[str, Any]] = None, evse_signaling: Any = None) -> float:
    """GRACE: Karenz ab dem bestätigten Angebot (≥ 60 s) mal HLC-Faktor."""

    return start_cp_grace_s(config) * start_window_hlc_factor(evse_signaling)


def start_offer_power_confirmed(
    status: Optional[Dict[str, Any]],
    prev_frame: Optional[Dict[str, Any]] = None,
    *,
    stable_hw_power_w: Any = 0.0,
) -> bool:
    """Ladeannahme nur mit Leistung, nicht mit ``charge_state`` allein.

    Hintergrund: ``charge_state True`` bei 0 W löschte die
    Startbelege und ließ 0-A-Schnitte zu. Ein Frame gilt erst mit
    ``charge_state``/``charging`` UND > 500 W; der Aufrufer zählt zwei Frames
    (oder ein Energiedelta > 5 Wh). ``prev_frame`` erlaubt die Zwei-Frame-
    Prüfung direkt im Vertrag.
    """

    def _frame_ok(frame: Optional[Dict[str, Any]], extra_w: float) -> bool:
        st = frame if isinstance(frame, dict) else {}
        if not _fresh_valid_status(st):
            return False
        power_w = max(
            _safe_float(st.get("real_power_w"), 0.0),
            _safe_float(st.get("phase_power_sum_w"), 0.0),
            _safe_float(st.get("power_w"), 0.0),
            extra_w,
        )
        return bool((st.get("charge_state") or st.get("charging")) and power_w > START_WINDOW_CONFIRM_POWER_W)

    current_ok = _frame_ok(status, _safe_float(stable_hw_power_w, 0.0))
    if prev_frame is None:
        return current_ok
    return bool(current_ok and _frame_ok(prev_frame, 0.0))


def _start_window_fresh_core(plug_session_id: Any) -> Dict[str, Any]:
    return {
        "schema": START_WINDOW_SCHEMA,
        "plug_session_id": str(plug_session_id or ""),
        "state": START_WINDOW_STATE_INACTIVE,
        "prev_state": "",
        "anchor_ts": 0.0,
        "offer_amp": 0.0,
        "box_offer_amp": 0.0,
        "command_ts": 0.0,
        "command_amp": 0.0,
        "command_seen_ts": 0.0,
        "reemit_count": 0,
        "unresponsive_until": 0.0,
        "cycle": 0,
        "cycle_anchor_ts": 0.0,
        "retry_due_ts": 0.0,
        "cp_in_cycle": 0,
        "cp_count_session": 0,
        "cp_settle_until": 0.0,
        "no_budget_since": 0.0,
        "confirmed_no_budget_since": 0.0,
        "grid_import_wh_in_window": 0.0,
        "budget_ok_frames": 0,
        "offer_lost_frames": 0,
        "confirm_frames": 0,
        "stale_frames": 0,
        "disconnect_frames": 0,
        "energy_wh_anchor": None,
        "last_change_ts": 0.0,
        "charging_since_ts": 0.0,
        "exhausted": False,
        "zero_sent": False,
        "slot_active": False,
        "hlc_factor": 1.0,
        "evse_signaling": "",
        "reason": "",
        "hard_edge_reason": "",
        "hold_events": {},
        "transition_ts": 0.0,
        "updated_ts": 0.0,
    }


START_WINDOW_CORE_KEYS = tuple(_start_window_fresh_core("").keys())


def start_window_contract(
    previous: Optional[Dict[str, Any]],
    *,
    now_ts: Any,
    config: Optional[Dict[str, Any]] = None,
    inputs: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Startfenster der openWB Pro – reiner Vertrag.

    Angebot stellen, einfrieren, wiederholen – nie 0 A aus Budgetgründen.
    Zustände: inactive → budget_wait → (Adoption eines stehenden Angebots
    ≤ clamp ohne Befehl | Startbefehl 6 A) offer_pending → (Readback ≥ 6 A
    = anchor_ts) offer_frozen [t_rb, t_rb+T_HOLD: kein 0 A, keine Änderung] →
    charging_confirmed (2 Frames > 500 W; danach nur Änderungen ≥ 6 A im
    30-s-Raster) → regulating. Fensterende ohne Ladung → retry_wait (Zyklen
    T_CYCLE, ≤ 1 CP je Zyklus frühestens Anker+GRACE, nur bei Readback ≥ 6 A ∧
    ¬charge_state ∧ ≤ 100 W ∧ ¬cp_active ∧ 60 s nach phasetarget) → nach 3 Zyklen
    exhausted (Angebot bleibt, kein CP). Nullbudget: Box-Angebot T_HOLD
    unverändert, dann 0 A lösbar (off_no_budget); Box 0 A → kein Write.
    offer_lost (Box nullt in 2 Frames) → Wiederangebot 6 A, neuer Anker. Ohne
    Readback nach 20 s identischer Befehl (max. 2 → box_unresponsive 60 s).
    Harte Kanten → aborted_hard (0 A). HLC-Faktor ×2 datengetrieben aus
    ``evse_signaling``. Keine I/O, keine Zeitabfrage; ``previous`` ist der
    persistierte Kern derselben Stecksession.
    """

    prev = previous if isinstance(previous, dict) else {}
    inp = inputs if isinstance(inputs, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    now = _safe_float(now_ts, 0.0)
    plug_session_id = str(inp.get("plug_session_id") or "")
    evse = str(inp.get("evse_signaling") or "")
    hlc = start_window_hlc_factor(evse)
    t_hold = start_window_hold_s(cfg, evse)
    t_cycle = start_retry_cycle_s(cfg, evse)
    grace_s = start_window_cp_grace_s(cfg, evse)
    cp_settle_s = max(start_wakeup_delay_s(cfg), START_WINDOW_CP_SETTLE_MIN_S)
    cp_max = start_cp_retry_limit(cfg)

    same_session = bool(
        plug_session_id
        and prev.get("schema") == START_WINDOW_SCHEMA
        and str(prev.get("plug_session_id") or "") == plug_session_id
    )
    w = _start_window_fresh_core(plug_session_id)
    if same_session:
        for key in START_WINDOW_CORE_KEYS:
            if key in prev:
                w[key] = prev[key]
        w["hold_events"] = dict(prev.get("hold_events") or {}) if isinstance(prev.get("hold_events"), dict) else {}
        w["plug_session_id"] = plug_session_id

    connected = bool(inp.get("connected", False))
    status_fresh = bool(inp.get("status_fresh", False))
    offered_rb = max(0.0, _safe_float(inp.get("offered_rb"), 0.0))
    charge_state = bool(inp.get("charge_state", False))
    power_rb = max(0.0, _safe_float(inp.get("power_rb"), 0.0))
    energy_raw = inp.get("energy_wh")
    energy_wh = _safe_float(energy_raw, 0.0) if energy_raw is not None else None
    cp_active = bool(inp.get("cp_interrupt_isactive", False))
    cp_glitch = bool(inp.get("cp_glitch", False))
    budget_valid = inp.get("budget_valid", True) is not False
    budget_w = _safe_float(inp.get("budget_w"), 0.0)
    cap_amp = max(0.0, _safe_float(inp.get("cap_amp"), 0.0))
    min_w = max(0.0, _safe_float(inp.get("min_w"), 0.0))
    deckel_amp = _safe_float(inp.get("deckel_amp"), 0.0)
    hard_edge_in = bool(inp.get("hard_edge", False))
    hard_edge_reason_in = str(inp.get("hard_edge_reason") or "")
    user_off = bool(inp.get("user_off", False))
    soc_known = bool(inp.get("soc_known", False))
    soc_reached = bool(inp.get("soc_reached", False))
    last_phasetarget_ts = _safe_float(inp.get("last_phasetarget_ts"), 0.0)
    sequence_active = bool(inp.get("phase_sequence_active", False))
    reservation_active = bool(inp.get("reservation_active", False))
    cp_enabled = bool(inp.get("cp_enabled", False))
    start_blocked_by_session = bool(inp.get("start_blocked_by_session", False))
    slot_active = bool(inp.get("slot_active", False))
    command_ts = _safe_float(inp.get("command_ts"), 0.0)
    command_amp = _safe_float(inp.get("command_amp"), 0.0)
    # Belege des Managers zum eigenen Befehl (Receipt): force_state des
    # Befehls, Laufbeleg (frischer Status vor dem Befehl zeigte echte Ladung)
    # und Absenkbeleg (zusätzlich lag der Befehl unter dem damaligen Angebot).
    command_force_state = inp.get("command_force_state")
    command_explicit_start = bool(
        command_force_state is not None and _safe_float(command_force_state, 0.0) == 2.0
    )
    command_running_before = bool(inp.get("command_running_before", False))
    command_running_reduction = bool(
        command_running_before and inp.get("command_running_reduction", False)
    )
    # Netzleistung (positiv = Bezug) für den Wh-Deckel; None = kein gültiger Wert.
    grid_raw = inp.get("grid_w")
    grid_w = _safe_float(grid_raw, 0.0) if grid_raw is not None else None
    # Grund des letzten eigenen 0-A-Strombefehls dieser
    # Stecksession (Manager-Beleg); leer = kein eigenes 0 A → kein Re-Arm.
    last_zero_reason = str(inp.get("last_zero_reason") or "").strip().lower()
    own_zero_sent = bool(
        last_zero_reason
        and not last_zero_reason.startswith(START_WINDOW_SEQUENCE_ZERO_REASON_PREFIXES)
    )

    transitions = []

    def goto(new_state: str, reason: str) -> None:
        if w["state"] != new_state:
            transitions.append((w["state"], new_state, reason))
            w["prev_state"] = w["state"]
            w["state"] = new_state
            w["transition_ts"] = now
            if new_state == START_WINDOW_STATE_CHARGING_CONFIRMED:
                # Eigene Nullbudget-Uhr und Wh-Deckel beginnen mit der Bestätigung.
                w["confirmed_no_budget_since"] = 0.0
                w["grid_import_wh_in_window"] = 0.0
        w["reason"] = reason

    def note_hold(reason: str) -> None:
        events = w["hold_events"]
        events[reason] = int(events.get(reason, 0) or 0) + 1
        w["reason"] = reason

    def reset_offer(keep_zero_sent: bool = False) -> None:
        w["anchor_ts"] = 0.0
        w["offer_amp"] = 0.0
        w["command_ts"] = 0.0
        w["command_amp"] = 0.0
        w["command_seen_ts"] = now
        w["reemit_count"] = 0
        w["confirm_frames"] = 0
        w["offer_lost_frames"] = 0
        w["energy_wh_anchor"] = None
        if not keep_zero_sent:
            w["zero_sent"] = False

    state = str(w["state"] or START_WINDOW_STATE_INACTIVE)

    # Lücke ohne Bewertung (Modus Aus/Pause ohne Markierung, Regelstillstand > 300 s):
    # Angebotsphasen beginnen neu ab budget_wait (Zähler bleiben), eine bestätigte
    # Ladung gilt als reguliert. Ein Manager-Neustart innerhalb der Frist setzt
    # den Fensterrest aus anchor_ts fort.
    updated_ts = _safe_float(w.get("updated_ts"), 0.0)
    if state in START_WINDOW_ENFORCING_STATES and updated_ts > 0.0 and now - updated_ts > START_WINDOW_GAP_RESET_S:
        if state == START_WINDOW_STATE_CHARGING_CONFIRMED:
            goto(START_WINDOW_STATE_REGULATING, "window_gap_regulating")
        else:
            goto(START_WINDOW_STATE_BUDGET_WAIT, "window_gap_reset")
            reset_offer()
        w["stale_frames"] = 0
        w["disconnect_frames"] = 0
        w["budget_ok_frames"] = 0
        w["no_budget_since"] = 0.0
        state = w["state"]

    # Frames
    w["stale_frames"] = 0 if status_fresh else int(w.get("stale_frames", 0) or 0) + 1
    cp_settle_until = _safe_float(w.get("cp_settle_until"), 0.0)
    if connected:
        w["disconnect_frames"] = 0
    elif now >= cp_settle_until and not cp_glitch and status_fresh:
        w["disconnect_frames"] = int(w.get("disconnect_frames", 0) or 0) + 1
    budget_ok_now = bool(budget_valid and min_w > 0.0 and budget_w + 1e-9 >= min_w)
    if budget_ok_now:
        w["budget_ok_frames"] = int(w.get("budget_ok_frames", 0) or 0) + 1
        w["no_budget_since"] = 0.0
    else:
        w["budget_ok_frames"] = 0
    confirm_now = bool(status_fresh and charge_state and power_rb > START_WINDOW_CONFIRM_POWER_W)
    w["confirm_frames"] = int(w.get("confirm_frames", 0) or 0) + 1 if confirm_now else 0
    energy_anchor = w.get("energy_wh_anchor")
    energy_confirmed = bool(
        energy_wh is not None
        and energy_anchor is not None
        and energy_wh - _safe_float(energy_anchor, 0.0) > START_WINDOW_CONFIRM_ENERGY_WH
    )
    charge_confirmed = bool(
        int(w["confirm_frames"]) >= START_WINDOW_CONFIRM_FRAMES or energy_confirmed
    )
    offer_seen = bool(status_fresh and offered_rb >= 6.0)
    offer_missing = bool(
        status_fresh
        and offered_rb < 6.0
        and now >= cp_settle_until
        and not cp_active
        and not cp_glitch
        and not bool(w.get("zero_sent", False))
    )
    if offer_seen:
        w["offer_lost_frames"] = 0
    elif offer_missing:
        w["offer_lost_frames"] = int(w.get("offer_lost_frames", 0) or 0) + 1
    if status_fresh:
        w["box_offer_amp"] = float(offered_rb)
    offer_lost_now = bool(int(w["offer_lost_frames"]) >= START_WINDOW_OFFER_LOST_FRAMES)
    # Eigenes 0 A nach bestätigter Ladung – Box-Angebot in 2 frischen
    # Frames < 6 A, Fahrzeug zieht nicht (¬charge_state ∧ ≤ 100 W).
    # Nur mit Beleg eines eigenen 0 A und außerhalb von
    # Phasensequenz/Reservierung – Sequenz-0-A oder box-seitiger Abfall sind keine Einladung.
    offer_ended_after_charge = bool(
        offer_lost_now
        and not charge_state
        and power_rb <= START_WINDOW_CP_POWER_MAX_W
        and own_zero_sent
        and not sequence_active
        and not reservation_active
    )
    new_command = bool(
        command_ts > _safe_float(w.get("command_seen_ts"), 0.0)
        and command_amp >= 6.0
    )
    # Eigener Befehl an eine laufende Ladung: Das Fahrzeug lud laut Laufbeleg
    # schon vor dem Befehl, lädt weiter (frischer Frame > 500 W) am stehenden
    # Box-Angebot, der Befehl ist kein ausdrücklicher Start (force_state 2)
    # und liegt ab 6 A nicht über diesem Angebot (Toleranz 0,05 A). Das ist
    # kein Start; ein Start aus dem Leerlauf mit schnellem Readback und jede
    # Anhebung bleiben Startbefehle. Liegt der Befehl darunter, oder belegt
    # der Manager beim Befehl eine Absenkung (Receipt, auch wenn das Gerät sie
    # bis zu diesem Frame schon zurückmeldet), ist es eine eigene Absenkung:
    # Die laufende Ladung liegt über dem Budget. Jede Stufe zählt, auch die
    # 0,1-A-Schritte der Defizitkaskade.
    running_charge_command = bool(
        new_command
        and offer_seen
        and confirm_now
        and command_running_before
        and not command_explicit_start
        and command_amp <= offered_rb + START_WINDOW_RUNNING_COMMAND_TOLERANCE_A
    )
    running_charge_reduced = bool(
        running_charge_command
        and (
            command_amp < offered_rb - START_WINDOW_RUNNING_COMMAND_TOLERANCE_A
            or command_running_reduction
        )
    )

    # Harte Kanten
    hard_edge = bool(hard_edge_in or user_off)
    hard_reason = hard_edge_reason_in or ("mode_off" if user_off else "")
    if state in START_WINDOW_ENFORCING_STATES:
        if int(w["stale_frames"]) >= START_WINDOW_STALE_ABORT_FRAMES:
            hard_edge = True
            hard_reason = hard_reason or "driver_status_invalid"
        if bool(w.get("slot_active", False)) and not slot_active:
            hard_edge = True
            hard_reason = hard_reason or "scheduled_slot_ended"
    w["slot_active"] = slot_active

    output = {"action": "none", "amp": None, "force_state": None, "reason": ""}
    start_due = False
    reemit_due = False
    start_amp = 0.0
    offer_lost_flag = False
    raise_allowed = False
    reduce_allowed = False
    confirmed_zero_release = False

    def do_zero_budget_off() -> None:
        nonlocal output
        goto(START_WINDOW_STATE_OFF_NO_BUDGET, "zero_budget_off")
        output = {
            "action": "zero_budget_off",
            "amp": 0.0,
            "force_state": None,
            "reason": "openwb_pro_start_window_zero_budget_off",
        }
        w["zero_sent"] = True
        w["anchor_ts"] = 0.0
        w["offer_amp"] = 0.0
        w["confirm_frames"] = 0
        w["offer_lost_frames"] = 0
        w["no_budget_since"] = 0.0
        w["command_seen_ts"] = now

    def do_rearm_after_charge(reason: str) -> None:
        # Das eigene 0 A hat die Ladeepisode beendet –
        # off_no_budget, damit die nächste Budgetfreigabe ein neues 6-A-Fenster
        # eröffnet (Anhebungen nur im 30-s-Raster). Eine bestätigte Ladung zählt
        # nicht als Ablehnung: Wiederholzyklen und exhausted beginnen neu;
        # cp_count_session bleibt je Stecksession, zero_sent hält offer_lost still.
        goto(START_WINDOW_STATE_OFF_NO_BUDGET, reason)
        w["zero_sent"] = True
        w["anchor_ts"] = 0.0
        w["offer_amp"] = 0.0
        w["command_ts"] = 0.0
        w["command_amp"] = 0.0
        w["command_seen_ts"] = now
        w["reemit_count"] = 0
        w["confirm_frames"] = 0
        w["offer_lost_frames"] = 0
        w["energy_wh_anchor"] = None
        w["no_budget_since"] = 0.0
        w["confirmed_no_budget_since"] = 0.0
        w["cycle"] = 0
        w["cycle_anchor_ts"] = 0.0
        w["retry_due_ts"] = 0.0
        w["cp_in_cycle"] = 0
        w["exhausted"] = False

    def do_reoffer(reason: str) -> None:
        nonlocal offer_lost_flag
        goto(START_WINDOW_STATE_OFFER_PENDING, reason)
        reset_offer()
        offer_lost_flag = True

    def do_freeze(offer: float, reason: str) -> None:
        goto(START_WINDOW_STATE_OFFER_FROZEN, reason)
        w["anchor_ts"] = now
        w["offer_amp"] = float(offer)
        w["confirm_frames"] = 0
        w["offer_lost_frames"] = 0
        w["energy_wh_anchor"] = energy_wh
        w["reemit_count"] = 0
        w["zero_sent"] = False
        if _safe_float(w.get("cycle_anchor_ts"), 0.0) <= 0.0:
            w["cycle_anchor_ts"] = now

    def do_start(amp: float, reason: str) -> None:
        nonlocal start_due, start_amp
        start_due = True
        start_amp = float(amp)
        w["reason"] = reason

    def adopt_command(reason: str, count_reemit: bool) -> None:
        if count_reemit and _safe_float(w.get("command_ts"), 0.0) > 0.0:
            w["reemit_count"] = int(w.get("reemit_count", 0) or 0) + 1
        else:
            w["reemit_count"] = 0
        w["command_ts"] = command_ts
        w["command_amp"] = command_amp
        w["command_seen_ts"] = command_ts
        w["offer_amp"] = float(command_amp)
        w["zero_sent"] = False
        goto(START_WINDOW_STATE_OFFER_PENDING, reason)

    def do_confirm_charging() -> None:
        goto(START_WINDOW_STATE_CHARGING_CONFIRMED, "charge_confirmed")
        w["charging_since_ts"] = now
        if _safe_float(w.get("anchor_ts"), 0.0) > 0.0 and now >= _safe_float(w.get("anchor_ts"), 0.0) + t_hold:
            goto(START_WINDOW_STATE_REGULATING, "window_complete")

    def adopt_running_charge(reason: str) -> None:
        # Laufende Ladung vor dem eigenen
        # Angebot (Neustart, Force-Start, Lücke > 300 s, erster Deploy ohne
        # persistierten Kern) – das stehende Box-Angebot wird übernommen.
        # Deckt das Budget das Angebot (≤ clamp+2 A), läuft das Fenster als
        # charging_confirmed (Anker jetzt, 30-s-Raster, kein 0 A bis
        # Anker+T_HOLD). Liegt es darüber, braucht die laufende Ladung kein
        # Startfenster: regulating, damit die bestehende Regelung sofort
        # absenken darf (kein start_due, das der Kaltstart bei > 500 W nie
        # ausführt; kein Gate-Veto reduction_frozen; kein unbegrenzter Bezug).
        w["anchor_ts"] = now
        w["offer_amp"] = float(offered_rb)
        w["offer_lost_frames"] = 0
        w["energy_wh_anchor"] = energy_wh
        w["reemit_count"] = 0
        w["zero_sent"] = False
        w["charging_since_ts"] = now
        w["last_change_ts"] = 0.0
        if _safe_float(w.get("cycle_anchor_ts"), 0.0) <= 0.0:
            w["cycle_anchor_ts"] = now
        if running_charge_command:
            # Der eigene Befehl an die laufende Ladung ist mit der Übernahme
            # verbraucht und zählt später nicht als neuer Befehl.
            w["command_seen_ts"] = command_ts
        if running_charge_reduced:
            # Die eigene Absenkung belegt, dass die laufende Ladung über dem
            # Budget liegt: regulating wie oben, der Befehl ist verbraucht.
            w["offer_amp"] = float(command_amp)
            w["command_seen_ts"] = command_ts
            goto(START_WINDOW_STATE_REGULATING, "running_charge_reduced")
            return
        covered = bool(
            clamp_amp >= 6.0
            and offered_rb <= min(float(deckel_amp), clamp_amp + START_WINDOW_ADOPT_TOLERANCE_A)
        )
        if covered:
            goto(START_WINDOW_STATE_CHARGING_CONFIRMED, reason)
        else:
            goto(START_WINDOW_STATE_REGULATING, reason + "_above_budget")

    def enter_retry_cycle() -> None:
        w["cycle"] = int(w.get("cycle", 0) or 0) + 1
        if _safe_float(w.get("cycle_anchor_ts"), 0.0) <= 0.0:
            w["cycle_anchor_ts"] = _safe_float(w.get("anchor_ts"), now) or now
        if int(w["cycle"]) == 1:
            # Zyklus 1 rastert ab dem ersten Anker; ein CP aus der Fensterphase zählt bereits dazu.
            w["retry_due_ts"] = _safe_float(w.get("cycle_anchor_ts"), now) + t_cycle
        else:
            w["cycle_anchor_ts"] = _safe_float(w.get("cycle_anchor_ts"), now) + t_cycle
            w["retry_due_ts"] = _safe_float(w.get("retry_due_ts"), now) + t_cycle
            w["cp_in_cycle"] = 0
        if int(w["cycle"]) > START_WINDOW_CYCLES_MAX:
            w["exhausted"] = True
            goto(START_WINDOW_STATE_EXHAUSTED, "cycles_exhausted")
        else:
            goto(START_WINDOW_STATE_RETRY_WAIT, "retry_cycle_%d" % int(w["cycle"]))

    if state == START_WINDOW_STATE_INACTIVE:
        if connected and status_fresh and plug_session_id and not hard_edge:
            goto(START_WINDOW_STATE_BUDGET_WAIT, "plug_session_started")
            reset_offer()
            w["budget_ok_frames"] = 1 if budget_ok_now else 0
    elif hard_edge and state != START_WINDOW_STATE_ABORTED_HARD:
        goto(START_WINDOW_STATE_ABORTED_HARD, hard_reason or "hard_edge")
        w["hard_edge_reason"] = hard_reason or "hard_edge"
        output = {"action": "hard_zero", "amp": 0.0, "force_state": 1, "reason": hard_reason or "hard_edge"}
        w["zero_sent"] = True
        w["command_seen_ts"] = now
    elif state != START_WINDOW_STATE_ABORTED_HARD and int(w["disconnect_frames"]) >= START_WINDOW_DISCONNECT_FRAMES:
        goto(START_WINDOW_STATE_INACTIVE, "vehicle_disconnected")
        reset_offer()
    state = str(w["state"])

    if state == START_WINDOW_STATE_ABORTED_HARD:
        if not hard_edge and connected and status_fresh:
            came_from_regulating = str(w.get("prev_state") or "") == START_WINDOW_STATE_REGULATING
            goto(START_WINDOW_STATE_BUDGET_WAIT, "hard_edge_released")
            w["hard_edge_reason"] = ""
            reset_offer()
            w["budget_ok_frames"] = 1 if budget_ok_now else 0
            if came_from_regulating:
                # Pause/Aus nach bestätigter Ladung – das neue
                # 6-A-Fenster zählt keine alten Ablehnungen (Zyklen neu, exhausted False).
                w["cycle"] = 0
                w["cycle_anchor_ts"] = 0.0
                w["retry_due_ts"] = 0.0
                w["cp_in_cycle"] = 0
                w["exhausted"] = False
        else:
            w["reason"] = w.get("hard_edge_reason") or hard_reason or "hard_edge"
    state = str(w["state"])

    if state in START_WINDOW_NO_BUDGET_CLOCK_STATES and not budget_ok_now:
        if _safe_float(w.get("no_budget_since"), 0.0) <= 0.0:
            w["no_budget_since"] = now
    no_budget_expired = bool(
        _safe_float(w.get("no_budget_since"), 0.0) > 0.0
        and now - _safe_float(w.get("no_budget_since"), 0.0) >= t_hold
    )
    can_start_now = bool(
        int(w["budget_ok_frames"]) >= START_WINDOW_BUDGET_COHERENCE_FRAMES
        and not start_blocked_by_session
        and not reservation_active
        and not sequence_active
        and connected
        and status_fresh
    )
    clamp_amp = 0.0
    if deckel_amp >= 6.0:
        clamp_amp = max(6.0, min(float(deckel_amp), float(math.floor(cap_amp))))
    anchor_ts = _safe_float(w.get("anchor_ts"), 0.0)
    # Laufende Ladung (2 Frames > 500 W) mit stehendem
    # Box-Angebot ≥ 6 A vor dem eigenen Angebot – übernehmen statt
    # einfrieren/Startbefehl (Deadlock nach Neustart/Force-Start/Gap-Reset).
    running_charge_adoptable = bool(charge_confirmed and offer_seen)

    if state == START_WINDOW_STATE_BUDGET_WAIT:
        if running_charge_adoptable:
            adopt_running_charge("running_charge_adopted")
        elif running_charge_command:
            # Ein eigener Befehl an die laufende Ladung, der nicht über ihrem
            # Angebot liegt, wird schon im ersten Frame übernommen; er ist kein
            # start_command_issued. Eine Absenkung geht dabei auf regulating.
            adopt_running_charge("running_charge_adopted")
        elif new_command:
            adopt_command("start_command_issued", count_reemit=False)
        elif offer_seen and confirm_now:
            # Erster Frame > 500 W bei stehendem Angebot: kein Startbefehl, kein
            # Einfrieren – der zweite Frame entscheidet (Übernahme).
            w["reason"] = "running_charge_pending"
        elif can_start_now:
            if deckel_amp < 6.0:
                note_hold("deckel_below_minimum")
            elif offer_seen and offered_rb <= min(float(deckel_amp), clamp_amp + START_WINDOW_ADOPT_TOLERANCE_A):
                do_freeze(offered_rb, "adopted_standing_offer")
            else:
                do_start(clamp_amp if offer_seen else 6.0, "start_due")
        elif not budget_ok_now:
            if offer_seen and no_budget_expired:
                do_zero_budget_off()
            elif offer_seen:
                note_hold("budget_wait_offer_standing")
            else:
                w["reason"] = "no_budget_no_offer" if budget_valid else "budget_unknown_hold"
        elif start_blocked_by_session:
            note_hold("start_blocked_by_session")
        elif reservation_active or sequence_active:
            note_hold("phase_sequence_or_reservation")
        else:
            w["reason"] = "budget_coherence_pending" if status_fresh else "status_glitch_hold"
    elif state == START_WINDOW_STATE_OFFER_PENDING:
        if new_command:
            adopt_command("start_command_issued", count_reemit=True)
        if offer_seen:
            do_freeze(offered_rb, "readback_confirmed")
        elif _safe_float(w.get("command_ts"), 0.0) <= 0.0:
            if can_start_now:
                do_start(6.0, "reoffer_due")
            elif not budget_ok_now:
                w["reason"] = "reoffer_waits_for_budget"
            else:
                w["reason"] = "reoffer_blocked"
        elif now - _safe_float(w.get("command_ts"), 0.0) >= START_WINDOW_READBACK_TIMEOUT_S:
            if int(w.get("reemit_count", 0) or 0) < START_WINDOW_REEMIT_MAX:
                reemit_due = True
                start_amp = max(6.0, _safe_float(w.get("command_amp"), 6.0))
                w["reason"] = "readback_timeout_reemit"
            else:
                goto(START_WINDOW_STATE_BOX_UNRESPONSIVE, "box_unresponsive")
                w["unresponsive_until"] = now + START_WINDOW_UNRESPONSIVE_PAUSE_S
        else:
            w["reason"] = "awaiting_readback"
    elif state == START_WINDOW_STATE_BOX_UNRESPONSIVE:
        if new_command:
            adopt_command("start_command_issued", count_reemit=False)
        elif offer_seen:
            do_freeze(offered_rb, "readback_confirmed_late")
        elif now >= _safe_float(w.get("unresponsive_until"), 0.0) and can_start_now:
            do_start(6.0, "unresponsive_retry")
        else:
            w["reason"] = "box_unresponsive"
    elif state == START_WINDOW_STATE_OFFER_FROZEN:
        if charge_confirmed:
            do_confirm_charging()
        elif offer_lost_now:
            do_reoffer("offer_lost")
        elif anchor_ts > 0.0 and now >= anchor_ts + t_hold:
            enter_retry_cycle()
        elif not budget_ok_now and no_budget_expired and offer_seen:
            do_zero_budget_off()
        else:
            if offer_seen and abs(offered_rb - _safe_float(w.get("offer_amp"), 0.0)) >= 0.5:
                w["offer_amp"] = float(offered_rb)
                w["reason"] = "late_readback_adopted"
            elif not budget_ok_now:
                note_hold("budget_dip_hold" if budget_valid else "budget_unknown_hold")
            elif not status_fresh:
                note_hold("status_glitch_hold")
            else:
                w["reason"] = "offer_frozen"
            if new_command:
                w["command_seen_ts"] = command_ts
    elif state == START_WINDOW_STATE_CHARGING_CONFIRMED:
        if offer_ended_after_charge:
            # Freigegebenes/eigenes 0 A hat die Ladung beendet.
            do_rearm_after_charge("confirmed_offer_ended")
        elif anchor_ts > 0.0 and now >= anchor_ts + t_hold:
            goto(START_WINDOW_STATE_REGULATING, "window_complete")
        else:
            if new_command:
                w["command_seen_ts"] = command_ts
                if abs(command_amp - _safe_float(w.get("offer_amp"), 0.0)) >= 0.5:
                    w["offer_amp"] = float(command_amp)
                    w["last_change_ts"] = command_ts
            # Das eingefrorene
            # Angebot ist das, was die Box wirklich stehen hat. Das gemerkte
            # offer_amp stammt aus einem Befehl, der abgesetzt werden sollte -
            # nicht aus einem, der die Box erreicht hat. Bleibt eine Schreibung
            # aus (z. B. zweiter logischer Hardwareausgang im selben
            # Managerzyklus, verworfen), laeuft das Gedaechtnis der Box davon
            # und jeder spaetere, reale Zielwert darunter gilt als "Absenkung"
            # und wird eingefroren (z. B. 20 A gegen ein Phantomangebot von 24 A,
            # während die Box 10 A stehen hatte). Nach Ablauf des 30-s-Rasters
            # - jede echte Schreibung ist dann laengst zurueckgelesen - wird das
            # Angebot auf den frischen Readback zurueckgeholt. last_change_ts
            # bleibt unberuehrt, damit das Raster allein die Freigabe steuert.
            # Nur hier, nur mit frischem Readback >= 6 A; sonst wie bisher.
            w["offer_resynced_to_readback"] = bool(
                status_fresh
                and offered_rb >= 6.0
                and _safe_float(w.get("offer_amp"), 0.0) > offered_rb + 0.5
                and now - _safe_float(w.get("last_change_ts"), 0.0) >= START_WINDOW_RAISE_DELAY_S
            )
            if w["offer_resynced_to_readback"]:
                w["offer_amp"] = float(offered_rb)
            last_change = _safe_float(w.get("last_change_ts"), 0.0)
            raise_allowed = reduce_allowed = bool(
                now >= anchor_ts + START_WINDOW_RAISE_DELAY_S
                and now - last_change >= START_WINDOW_RAISE_DELAY_S
            )
            # Eigene Nullbudget-Uhr und Wh-Deckel.
            # Netzbezug zählt nur innerhalb der laufenden Nullbudget-Episode (ab dem zweiten
            # Frame ohne Budget, dt ≤ 30 s); ein Budgetframe setzt Uhr und Wh zurück.
            frame_dt = min(START_WINDOW_GRID_INTEGRATION_MAX_DT_S, max(0.0, now - updated_ts)) if updated_ts > 0.0 else 0.0
            if budget_ok_now:
                w["confirmed_no_budget_since"] = 0.0
                w["grid_import_wh_in_window"] = 0.0
            elif _safe_float(w.get("confirmed_no_budget_since"), 0.0) <= 0.0:
                w["confirmed_no_budget_since"] = now
                w["grid_import_wh_in_window"] = 0.0
            elif grid_w is not None and grid_w > 0.0 and frame_dt > 0.0:
                w["grid_import_wh_in_window"] = round(
                    _safe_float(w.get("grid_import_wh_in_window"), 0.0) + grid_w * frame_dt / 3600.0, 3
                )
            confirmed_zero_release = bool(
                not budget_ok_now
                and (
                    now - _safe_float(w.get("confirmed_no_budget_since"), now) >= START_WINDOW_CONFIRMED_ZERO_BUDGET_S
                    or _safe_float(w.get("grid_import_wh_in_window"), 0.0) >= START_WINDOW_CONFIRMED_GRID_IMPORT_MAX_WH
                )
            )
            if confirmed_zero_release:
                w["reason"] = "charging_confirmed_zero_released"
            else:
                w["reason"] = "charging_confirmed" if raise_allowed else "charging_confirmed_raster_hold"
    elif state == START_WINDOW_STATE_RETRY_WAIT:
        if charge_confirmed:
            do_confirm_charging()
        elif offer_lost_now:
            do_reoffer("offer_lost")
        elif now >= _safe_float(w.get("retry_due_ts"), 0.0) > 0.0:
            enter_retry_cycle()
        elif not budget_ok_now and no_budget_expired and offer_seen:
            do_zero_budget_off()
        else:
            if new_command:
                w["command_seen_ts"] = command_ts
            if not budget_ok_now:
                note_hold("budget_dip_hold" if budget_valid else "budget_unknown_hold")
            else:
                w["reason"] = "retry_cycle_%d" % int(w.get("cycle", 1) or 1)
    elif state == START_WINDOW_STATE_EXHAUSTED:
        if charge_confirmed:
            do_confirm_charging()
        elif offer_lost_now:
            do_reoffer("offer_lost_exhausted")
        elif not budget_ok_now and no_budget_expired and offer_seen:
            do_zero_budget_off()
        else:
            if new_command:
                w["command_seen_ts"] = command_ts
            w["reason"] = "exhausted"
    elif state == START_WINDOW_STATE_OFF_NO_BUDGET:
        if new_command:
            adopt_command("reoffer_after_zero_budget", count_reemit=False)
        elif can_start_now:
            do_start(6.0, "reoffer_due")
        else:
            w["reason"] = "off_no_budget"
    elif state == START_WINDOW_STATE_REGULATING:
        if offer_ended_after_charge:
            # Der Zustand regulating ist nicht terminal – nach dem eigenen 0 A
            # (Wolke, Abend, Priorität) eröffnet die nächste Budgetfreigabe ein 6-A-Fenster.
            do_rearm_after_charge("regulating_offer_ended")
        else:
            w["reason"] = "regulating"
    state = str(w["state"])
    anchor_ts = _safe_float(w.get("anchor_ts"), 0.0)

    cycle_anchor_ts = _safe_float(w.get("cycle_anchor_ts"), 0.0)
    cp_due = bool(
        state in (START_WINDOW_STATE_OFFER_FROZEN, START_WINDOW_STATE_RETRY_WAIT)
        and connected
        and status_fresh
        and offered_rb >= 6.0
        and not charge_state
        and power_rb <= START_WINDOW_CP_POWER_MAX_W
        and not cp_active
        and (last_phasetarget_ts <= 0.0 or now - last_phasetarget_ts >= START_WINDOW_PHASETARGET_QUIET_S)
        and not sequence_active
        and not reservation_active
        and now >= max(anchor_ts, cycle_anchor_ts) + grace_s
        and int(w.get("cp_in_cycle", 0) or 0) == 0
        and int(w.get("cp_count_session", 0) or 0) < cp_max
        and cp_enabled
        and now >= _safe_float(w.get("cp_settle_until"), 0.0)
        and not bool(w.get("exhausted", False))
    )

    active = state in START_WINDOW_ENFORCING_STATES
    freeze_active = state in START_WINDOW_FREEZE_STATES
    zero_allowed = bool(
        state in START_WINDOW_ZERO_STATES
        or output["action"] in ("zero_budget_off", "hard_zero")
        or not active
        or confirmed_zero_release
    )
    if not active:
        raise_allowed = True
        reduce_allowed = True
    hold_required = bool(
        active
        and state not in (START_WINDOW_STATE_CHARGING_CONFIRMED, START_WINDOW_STATE_OFF_NO_BUDGET)
        and output["action"] == "none"
    )
    w["hlc_factor"] = float(hlc)
    w["evse_signaling"] = evse
    w["updated_ts"] = now
    if start_due or reemit_due:
        output = {
            "action": "reemit" if reemit_due else "start",
            "amp": float(start_amp),
            "force_state": 2,
            "reason": "openwb_pro_start_window_start",
        }
    elif output["action"] == "none" and active and hold_required:
        output = {"action": "hold", "amp": None, "force_state": None, "reason": w.get("reason") or state}

    result = dict(w)
    result.update({
        "active": bool(active),
        "freeze_active": bool(freeze_active),
        "zero_allowed": bool(zero_allowed),
        "raise_allowed": bool(raise_allowed),
        "reduce_allowed": bool(reduce_allowed),
        "start_due": bool(start_due),
        "start_amp": float(start_amp),
        "reemit_due": bool(reemit_due),
        "cp_due": bool(cp_due),
        "hard_edge": bool(hard_edge and state == START_WINDOW_STATE_ABORTED_HARD),
        "hard_edge_reason": str(w.get("hard_edge_reason") or (hard_reason if hard_edge else "")),
        "min_offer_standing": bool(offered_rb >= 6.0),
        "hold_required": bool(hold_required),
        "holds_setpoint": bool(state in START_WINDOW_SETPOINT_STATES),
        "offer_lost": bool(offer_lost_flag),
        "charge_confirmed": bool(charge_confirmed),
        "confirmed_zero_release": bool(confirmed_zero_release),
        # Beleg des eigenen 0 A (Diagnose, kein Kernschlüssel).
        "last_zero_reason": last_zero_reason,
        "own_zero_sent": bool(own_zero_sent),
        "confirmed_no_budget_s": (
            round(now - _safe_float(w.get("confirmed_no_budget_since"), 0.0), 1)
            if _safe_float(w.get("confirmed_no_budget_since"), 0.0) > 0.0
            else 0.0
        ),
        "budget_ok": bool(budget_ok_now),
        "budget_w": float(budget_w),
        "min_w": float(min_w),
        "cap_amp": float(cap_amp),
        "deckel_amp": float(deckel_amp),
        "clamp_amp": float(clamp_amp),
        "offered_rb": float(offered_rb),
        "power_rb": float(power_rb),
        "soc_known": bool(soc_known),
        "soc_reached": bool(soc_reached),
        "t_hold_s": float(t_hold),
        "t_cycle_s": float(t_cycle),
        "grace_s": float(grace_s),
        "cp_settle_s": float(cp_settle_s),
        "cp_max_session": int(cp_max),
        "offer_frozen_until": float(anchor_ts + t_hold) if anchor_ts > 0.0 else 0.0,
        "retry_next_ts": _safe_float(w.get("retry_due_ts"), 0.0),
        "retry_cycle": "%d/%d" % (min(int(w.get("cycle", 0) or 0), START_WINDOW_CYCLES_MAX), START_WINDOW_CYCLES_MAX),
        "output": output,
        "transitions": list(transitions),
    })
    return result


def minimum_hold_reduction_contract(
    window: Optional[Dict[str, Any]],
    *,
    min_amp: Any = 6,
    phases: Any = 0,
    wb_id: Any = 0,
    cycle_token: Any = "",
    plug_session_id: Any = "",
) -> Dict[str, Any]:
    """Typisierte Absenkung auf den Mindeststrom unter Nullbudget – reiner Vertrag.

    Aktiv nur im Zustand ``charging_confirmed`` (Startfenster/Mindesthalt) mit
    bestätigter Ladung (> 500 W), ohne Budget (``budget_ok`` False), solange das
    Fenster 0 A noch nicht freigibt, bei stehendem Angebot über dem Mindeststrom
    und im 30-s-Raster (``reduce_allowed``). Ergebnis: genau ein Zielwert
    ``target_amp`` = Mindeststrom (nie Anhebung, nie 0 A). Fehlt das Fenster oder
    eine Bedingung, ist der Vertrag inaktiv – die Gates verhalten sich wie heute.
    """

    w = window if isinstance(window, dict) else {}
    minimum = max(1.0, _safe_float(min_amp, 6.0))
    phase_count = int(_safe_float(phases, 0.0))
    if phase_count not in (1, 2, 3):
        phase_count = 0
    memo_offer = _safe_float(w.get("offer_amp"), 0.0)
    box_offer = _safe_float(w.get("box_offer_amp"), 0.0)
    # Beobachtung an das frische Box-Readback gebunden – nie
    # über dem Readback (sonst wäre die 'Absenkung' auf einen Mindeststrom > 6 A real
    # eine Anhebung); ohne Readback ≥ 6 A inaktiv (fail-closed).
    if box_offer >= 6.0:
        offer = min(memo_offer, box_offer) if memo_offer >= 6.0 else box_offer
    else:
        offer = 0.0
    result = {
        "schema": MINIMUM_HOLD_REDUCTION_SCHEMA,
        "active": False,
        "reason": "",
        "wb_id": int(_safe_float(wb_id, 0.0)),
        "plug_session_id": str(plug_session_id or ""),
        "cycle_token": str(cycle_token or ""),
        "state": str(w.get("state") or ""),
        "target_amp": float(minimum),
        "observed_amp": round(offer, 3),
        "box_offer_amp": round(box_offer, 3),
        "min_amp": float(minimum),
        "phases": phase_count,
        "target_w": round(minimum * 230.0 * phase_count, 1) if phase_count else None,
    }
    if not w or w.get("active") is not True:
        result["reason"] = "window_inactive"
    elif str(w.get("state") or "") != START_WINDOW_STATE_CHARGING_CONFIRMED:
        result["reason"] = "not_charging_confirmed"
    elif not result["plug_session_id"] or str(w.get("plug_session_id") or "") != result["plug_session_id"]:
        result["reason"] = "session_mismatch"
    elif not result["cycle_token"] or result["wb_id"] <= 0:
        result["reason"] = "binding_missing"
    elif bool(w.get("budget_ok", False)):
        result["reason"] = "budget_ok"
    elif bool(w.get("zero_allowed", False)):
        result["reason"] = "zero_released"
    elif not bool(w.get("charge_confirmed", False)) or _safe_float(w.get("power_rb"), 0.0) <= START_WINDOW_CONFIRM_POWER_W:
        result["reason"] = "charge_not_confirmed"
    elif box_offer < 6.0:
        result["reason"] = "offer_readback_missing"
    elif offer < minimum + 0.5:
        result["reason"] = "offer_at_minimum"
    elif not bool(w.get("reduce_allowed", False)):
        result["reason"] = "raster_hold"
    else:
        result["active"] = True
        result["reason"] = MINIMUM_HOLD_REDUCTION_REASON
    return result


def start_window_core(window: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Persistenter Kern (nur Schlüssel des Kerns) aus einem Vertragsergebnis/Memo."""

    src = window if isinstance(window, dict) else {}
    core = {key: src.get(key) for key in START_WINDOW_CORE_KEYS if key in src}
    if isinstance(core.get("hold_events"), dict):
        core["hold_events"] = dict(core["hold_events"])
    return core


def start_window_output_rule(
    window: Optional[Dict[str, Any]],
    cmd_amp: Any,
    force_state: Any = None,
    physical_offer_amp: Any = None,
) -> Dict[str, Any]:
    """Regel des Executor-Gates für einen Strombefehl im Startfenster.

    Rückgabe ``allowed``, ``reason`` (Vetogrund) und ``change`` ∈ {none, zero,
    same, raise, reduce}. Identische Werte (Keepalive/Re-Emit) passieren;
    Anhebung/Absenkung nur mit ``raise_allowed``/``reduce_allowed``; 0 A oder
    ``force_state 1`` nur mit ``zero_allowed``.

    ``physical_offer_amp`` (Beleg des Managers) ist das physisch stehende
    Angebot der Box aus einem frischen Readback, nach oben begrenzt durch einen
    noch nicht zurückgemeldeten eigenen Strombefehl. Liegt es bei mindestens
    6 A, passiert jeder Befehl ab 6 A darunter als Absenkung in jedem
    Fensterzustand, auch wenn die bisherige Regel ihn einfröre. Die Regel wirkt
    nur freigebend: Was bisher erlaubt war, bleibt erlaubt; 0 A,
    ``force_state 1`` und Anhebungen bleiben unverändert beim Fenster.
    """

    w = window if isinstance(window, dict) else {}
    state = str(w.get("state") or "")
    amp = _safe_float(cmd_amp, 0.0)
    try:
        fs = int(float(force_state)) if force_state is not None else None
    except (TypeError, ValueError):
        fs = None
    result = {"allowed": True, "reason": "window_not_enforcing", "change": "none", "state": state}
    if not bool(w.get("active", False)) or state not in START_WINDOW_ENFORCING_STATES:
        return result
    offer = _safe_float(w.get("offer_amp"), 0.0)
    if offer < 6.0:
        offer = _safe_float(w.get("box_offer_amp"), 0.0)
    if amp < 6.0 or fs == 1:
        result["change"] = "zero"
        if bool(w.get("zero_allowed", False)):
            result["reason"] = "zero_allowed"
        else:
            result["allowed"] = False
            result["reason"] = "budget_wait_zero" if state == START_WINDOW_STATE_BUDGET_WAIT else "offer_frozen_zero"
        return result
    physical = _safe_float(physical_offer_amp, 0.0) if physical_offer_amp is not None else 0.0
    if not math.isfinite(physical):
        physical = 0.0
    # Absenkung gegen das physisch stehende Angebot: die sichere Richtung, im
    # Fenster nie gesperrt (Netzbezug statt Festhalten).
    physical_reduction = bool(physical >= 6.0 and amp <= physical - 0.5)

    def _allow_physical_reduction() -> Dict[str, Any]:
        result["allowed"] = True
        result["change"] = "reduce"
        result["reason"] = "reduce_below_physical_offer"
        result["physical_offer_amp"] = float(physical)
        return result

    if offer >= 6.0 and abs(amp - offer) < 0.5:
        result["change"] = "same"
        result["reason"] = "identical_offer"
        return result
    if offer >= 6.0 and amp < offer:
        result["change"] = "reduce"
        if bool(w.get("reduce_allowed", False)):
            result["reason"] = "reduce_allowed"
        elif (
            state in START_WINDOW_PRE_OFFER_STATES
            and _safe_float(w.get("power_rb"), 0.0) > START_WINDOW_CONFIRM_POWER_W
        ):
            # Laufende Ladung vor dem eigenen Angebot –
            # Absenkung ≥ 6 A sofort erlaubt (Netzbezug), 0 A bleibt gesperrt.
            result["reason"] = "running_charge_reduce"
        elif physical_reduction:
            return _allow_physical_reduction()
        else:
            result["allowed"] = False
            result["reason"] = "reduction_frozen"
        return result
    result["change"] = "raise"
    if bool(w.get("raise_allowed", False)) or bool(w.get("start_due", False)) or bool(w.get("reemit_due", False)):
        result["reason"] = "raise_allowed" if bool(w.get("raise_allowed", False)) else "start_due"
    elif physical_reduction:
        # Gegen das gemerkte Angebot eine Anhebung, gegen das physisch stehende
        # (etwa nach einem Neustart der Box) eine Absenkung.
        return _allow_physical_reduction()
    else:
        result["allowed"] = False
        result["reason"] = "raise_not_due"
    return result


def _start_window_hhmm(ts: float, with_seconds: bool = False) -> str:
    try:
        import datetime as _dt

        stamp = _dt.datetime.fromtimestamp(float(ts))
        return stamp.strftime("%H:%M:%S" if with_seconds else "%H:%M")
    except (TypeError, ValueError, OverflowError, OSError):
        return "--:--"


def start_window_reason_text(window: Optional[Dict[str, Any]], now_ts: Any = 0) -> str:
    """Klartext des Startfensters für Session-Reason und Dashboard."""

    w = window if isinstance(window, dict) else {}
    state = str(w.get("state") or "")
    now = _safe_float(now_ts, 0.0)
    offer = _safe_float(w.get("offer_amp"), 0.0)
    cycle = min(int(_safe_int(w.get("cycle"), 0)), START_WINDOW_CYCLES_MAX)
    if state == START_WINDOW_STATE_OFFER_PENDING:
        amp = offer if offer >= 6.0 else _safe_float(w.get("command_amp"), 6.0)
        return "Startangebot %.0f A gesendet; warte auf Bestätigung der Box." % max(6.0, amp)
    if state == START_WINDOW_STATE_OFFER_FROZEN:
        remaining = max(0.0, _safe_float(w.get("offer_frozen_until"), 0.0) - now)
        return "Startfenster %d/%d: %.0f A angeboten, wartet auf Ladeannahme (noch %02d:%02d)." % (
            max(1, cycle),
            START_WINDOW_CYCLES_MAX,
            offer,
            int(remaining // 60),
            int(remaining % 60),
        )
    if state == START_WINDOW_STATE_CHARGING_CONFIRMED:
        return "Startfenster bis %s (nur Anhebungen/Absenkungen ≥ 6 A)." % _start_window_hhmm(
            _safe_float(w.get("offer_frozen_until"), 0.0), with_seconds=True
        )
    if state == START_WINDOW_STATE_RETRY_WAIT:
        return "Start abgelehnt – Wiederholung %s (Zyklus %d/%d); Angebot %.0f A bleibt stehen." % (
            _start_window_hhmm(_safe_float(w.get("retry_next_ts"), 0.0)),
            max(1, cycle),
            START_WINDOW_CYCLES_MAX,
            offer,
        )
    if state == START_WINDOW_STATE_EXHAUSTED:
        return (
            "Fahrzeug lädt trotz Freigabe nicht (%d/%d); Angebot %.0f A bleibt stehen, kein weiterer Weckimpuls."
            % (START_WINDOW_CYCLES_MAX, START_WINDOW_CYCLES_MAX, offer)
        )
    if state == START_WINDOW_STATE_OFF_NO_BUDGET:
        return "Wartet auf PV-Budget – Angebot nach %d s ohne Budget beendet (0 A, wird bei Budget wiederholt)." % int(
            round(_safe_float(w.get("t_hold_s"), START_WINDOW_HOLD_DEFAULT_S))
        )
    if state == START_WINDOW_STATE_BOX_UNRESPONSIVE:
        return "Box spiegelt den Startbefehl nicht; Wiederholung in %d s." % max(
            0, int(_safe_float(w.get("unresponsive_until"), 0.0) - now)
        )
    return str(w.get("reason") or state)


def apply_vehicle_finished_drop_to_status(
    status: Optional[Dict[str, Any]],
    contract: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    """Attach vehicle-finished pre-latch diagnostics to a status dict."""

    if status is None or not isinstance(contract, dict):
        return status
    status["openwb_pro_vehicle_finished_contract"] = contract
    status["openwb_pro_vehicle_finished_action"] = str(contract.get("action", "") or "")
    status["openwb_pro_vehicle_finished_reason"] = str(contract.get("reason", "") or "")
    status["openwb_pro_vehicle_finished_allow_new_latch"] = bool(contract.get("allow_new_latch", False))
    status["openwb_pro_vehicle_finished_blockers"] = list(contract.get("blockers") or [])
    status["openwb_pro_vehicle_finished_had_confirmed_charge"] = bool(
        contract.get("had_confirmed_charge", False)
    )
    status["openwb_pro_vehicle_finished_time_since_start_s"] = float(
        contract.get("time_since_start_s", 0.0) or 0.0
    )
    status["openwb_pro_vehicle_finished_drop_confirmed"] = bool(
        contract.get("drop_confirmed", False)
    )
    status["openwb_pro_vehicle_finished_drop_age_s"] = float(
        contract.get("drop_age_s", 0.0) or 0.0
    )
    status["openwb_pro_vehicle_finished_drop_frames"] = int(
        contract.get("drop_frames", 0) or 0
    )
    status["openwb_pro_vehicle_finished_remaining_s"] = float(
        contract.get("remaining_drop_s", 0.0) or 0.0
    )
    return status


def temporary_ems_stop_contract(
    status: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    current_set_amp: Any = 0,
    cap_amp: Any = 0,
    min_amp: Any = 6,
    mode_off: bool = False,
    priority_forced_stop: bool = False,
    stop_sent_active: bool = False,
    manager_stop_pending: bool = False,
    manager_stop_reason: str = "",
    manager_zero_anchor_active: bool = False,
    ended_latched: bool = False,
    now_ts: Any = 0,
    stable_hw_power_w: Any = 0.0,
) -> Dict[str, Any]:
    """Classify temporary EMS stops separately from vehicle-finished latches."""

    st = status if isinstance(status, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    min_current = max(1.0, _safe_float(min_amp, 6.0))
    current_amp = max(0.0, _safe_float(current_set_amp, 0.0))
    cap = max(0.0, _safe_float(cap_amp, 0.0))
    hw_amp = max(
        _safe_float(st.get("amp", 0), 0.0),
        _safe_float(st.get("offered_current_raw", 0), 0.0),
        _safe_float(st.get("evse_current", 0), 0.0),
    )
    connected = status_connected(st)
    real_power_w = max(status_real_power(st), _safe_float(stable_hw_power_w, 0.0))
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    zero_anchor = bool(manager_zero_anchor_active or data.get("_manager_zero_anchor_active", False))
    # Der Manager übergibt die aktuelle Stoppwahrheit ausdrücklich. Das
    # Statusobjekt wird zyklusübergreifend wiederverwendet und kann noch den
    # vorherigen Anzeigemarker enthalten; dieser darf einen aufgehobenen Stopp
    # niemals wieder aktivieren.
    stop_pending = bool(manager_stop_pending)
    last_stop_known = bool(
        _safe_float(data.get("_last_manager_stop_request_ts", 0.0), 0.0) > 0.0
        or _safe_float(data.get("_last_manager_zero_anchor_ts", 0.0), 0.0) > 0.0
    )
    # ``current_set_amp`` und ``cap_amp`` sind die neue fachliche Anforderung,
    # nicht der physische Nachweis, dass ein zuvor gesendeter Stopp noch wirkt.
    # Nach bestätigten 0 A / 0 W ist der Stopp abgeschlossen und ein frisches
    # positives Budget darf wieder starten. Andernfalls hält gerade das neue
    # Angebot den alten Stopp-Latch endlos aktiv.
    stop_has_effect = bool(
        real_charging
        or real_power_w > 50.0
        or hw_amp >= min_current
    )

    state_hint = "none"
    reason = ""
    active = False
    stopping = False
    temporary = False
    start_blocked = False

    if ended_latched:
        state_hint = "vehicle_finished"
        reason = "vehicle_finished_latched"
        start_blocked = True
    elif bool(mode_off):
        state_hint = "off"
        reason = "mode_off"
        active = True
        start_blocked = True
    elif stop_pending:
        state_hint = "stopping"
        reason = str(manager_stop_reason or st.get("manager_stop_reason") or "manager_stop_pending")
        active = True
        stopping = True
        temporary = True
        start_blocked = True
    elif bool(stop_sent_active) and stop_has_effect:
        state_hint = "stopping"
        reason = "stop_command_active"
        active = True
        stopping = True
        temporary = True
        start_blocked = True
    elif bool(stop_sent_active) and connected:
        state_hint = "waiting_start_release"
        reason = "stop_command_settled"
        active = True
        temporary = True
        start_blocked = False
    elif zero_anchor:
        state_hint = "waiting_start_release"
        reason = str(data.get("_last_manager_zero_anchor_reason") or "manager_zero_anchor_active")
        active = True
        temporary = True
        start_blocked = True
    elif bool(priority_forced_stop):
        state_hint = "waiting_start_release"
        reason = "priority_forced_stop"
        active = True
        temporary = True
        start_blocked = True
    elif last_stop_known and connected and current_amp <= 0 and cap <= 0 and hw_amp <= 0 and not real_charging:
        state_hint = "waiting_start_release"
        reason = "zero_current_policy_hold"
        active = True
        temporary = True
        start_blocked = False

    return {
        "contract": "openwb_pro_temporary_ems_stop_v1",
        "active": bool(active),
        "temporary": bool(temporary),
        "stopping": bool(stopping),
        "state_hint": state_hint,
        "reason": reason,
        "start_blocked": bool(start_blocked),
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "real_power_w": float(real_power_w if real_charging else 0.0),
        "current_set_amp": float(current_amp),
        "cap_amp": float(cap),
        "hardware_amp": float(hw_amp),
        "manager_stop_pending": bool(stop_pending),
        "manager_zero_anchor_active": bool(zero_anchor),
        "mode_off": bool(mode_off),
        "priority_forced_stop": bool(priority_forced_stop),
        "ended_latched": bool(ended_latched),
        "ts": float(now_value),
    }


def manager_owned_wakeup_disconnect_contract(
    status: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Bindet einen scheinbaren Unplug streng an den eigenen Start-Wake-up.

    openWB Pro signalisiert während eines kurzen CP-Interrupts vorübergehend
    keinen verbundenen Wagen. Das ist weder ein Nutzer-Unplug noch ein Grund,
    das bereits freigegebene Stromangebot dauerhaft auf 0 A zu verankern. Die
    Zuordnung gelingt ausschließlich über den persistierten CP-Receipt, den
    zugehörigen positiven Strombeleg und dieselbe Stecksession.

    ``synthetic_disconnect`` ist nur im kurzen Nachlauf des CP-Ausgangs wahr
    und kann einen neuen ``no_vehicle_connected``-Stop verhindern.
    ``anchor_releasable`` bleibt zeitlich unabhängig vom späteren Prüfmoment,
    bindet aber den Erstellzeitpunkt des bereits gesetzten Ankers an genau
    diesen kurzen Nachlauf. Alle Safety- und Budget-Gates werden weiterhin im
    aufrufenden Release-Vertrag geprüft.
    """

    st = status if isinstance(status, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    plug_session_id = str(data.get("_openwb_pro_plug_session_id") or "")
    issued_session_id = str(
        data.get("_openwb_pro_start_current_session_id") or ""
    )
    issued_ts = _safe_float(
        data.get("_openwb_pro_start_current_issued_ts"),
        0.0,
    )
    receipt = data.get("_openwb_pro_start_wakeup_receipt")
    receipt = receipt if isinstance(receipt, dict) else {}
    receipt_session_id = str(receipt.get("plug_session_id") or "")
    receipt_current_ts = _safe_float(receipt.get("current_issued_ts"), 0.0)
    first_sent_ts = _safe_float(receipt.get("first_sent_ts"), 0.0)
    last_sent_ts = _safe_float(receipt.get("last_sent_ts"), 0.0)
    try:
        raw_count = receipt.get("count", 0)
        if isinstance(raw_count, bool):
            raise ValueError("bool is not a retry count")
        numeric_count = float(raw_count)
        if not math.isfinite(numeric_count) or not numeric_count.is_integer():
            raise ValueError("retry count is not an integer")
        sent_count = int(numeric_count)
    except (TypeError, ValueError, OverflowError):
        sent_count = -1

    session_bound = bool(
        plug_session_id
        and issued_session_id == plug_session_id
        and receipt_session_id == plug_session_id
    )
    current_receipt_bound = bool(
        math.isfinite(issued_ts)
        and math.isfinite(receipt_current_ts)
        and issued_ts > 0.0
        and receipt_current_ts == issued_ts
    )
    timeline_valid = bool(
        current_receipt_bound
        and math.isfinite(first_sent_ts)
        and math.isfinite(last_sent_ts)
        and math.isfinite(now_value)
        and first_sent_ts >= issued_ts
        and last_sent_ts >= first_sent_ts
        and now_value >= last_sent_ts
    )
    count_valid = bool(1 <= sent_count <= 3)
    receipt_bound = bool(
        str(receipt.get("schema") or "")
        == "openwb_pro_start_wakeup_receipt_v1"
        and session_bound
        and timeline_valid
        and count_valid
    )
    wakeup_age_s = (
        max(0.0, now_value - last_sent_ts)
        if receipt_bound
        else 0.0
    )
    offered_amp = max(
        _safe_float(st.get("offered_current_raw"), 0.0),
        _safe_float(st.get("evse_current"), 0.0),
        _safe_float(st.get("amp"), 0.0),
        _safe_float(data.get("current_set_amp"), 0.0),
    )
    fresh_connection = _fresh_connection_state(st)
    synthetic_disconnect = bool(
        receipt_bound
        and wakeup_age_s <= MANAGER_WAKEUP_DISCONNECT_MAX_LAG_S
        and _fresh_valid_status(st)
        and fresh_connection is False
        and offered_amp >= 6.0
        and not start_offer_physics_confirmed(st)
    )

    typed_anchor = (
        data.get("_manager_zero_anchor_contract")
        if isinstance(data.get("_manager_zero_anchor_contract"), dict)
        else {}
    )
    anchor_created_ts = _safe_float(typed_anchor.get("created_ts"), 0.0)
    anchor_lag_s = (
        anchor_created_ts - last_sent_ts
        if receipt_bound
        and math.isfinite(anchor_created_ts)
        and anchor_created_ts > 0.0
        else -1.0
    )
    exact_anchor = bool(
        data.get("_manager_zero_anchor_active", False)
        and str(typed_anchor.get("contract") or "")
        == "wallbox_manager_zero_anchor_v1"
        and str(typed_anchor.get("owner") or "") == "wallbox_manager"
        and str(typed_anchor.get("class") or "") == "hard_stop"
        and str(typed_anchor.get("reason_code") or "")
        == "no_vehicle_connected"
        and str(typed_anchor.get("plug_session_id") or "")
        == plug_session_id
    )
    anchor_releasable = bool(
        receipt_bound
        and exact_anchor
        and anchor_created_ts <= now_value
        and 0.0 <= anchor_lag_s <= MANAGER_WAKEUP_DISCONNECT_MAX_LAG_S
    )
    return {
        "contract": "openwb_pro_manager_wakeup_disconnect_v1",
        "synthetic_disconnect": synthetic_disconnect,
        "anchor_releasable": anchor_releasable,
        "receipt_bound": receipt_bound,
        "session_bound": session_bound,
        "current_receipt_bound": current_receipt_bound,
        "timeline_valid": timeline_valid,
        "count_valid": count_valid,
        "sent_count": max(0, sent_count),
        "plug_session_id": plug_session_id,
        "receipt_plug_session_id": receipt_session_id,
        "fresh_connection": fresh_connection,
        "offered_amp": float(offered_amp),
        "wakeup_age_s": float(wakeup_age_s),
        "max_lag_s": float(MANAGER_WAKEUP_DISCONNECT_MAX_LAG_S),
        "exact_anchor": exact_anchor,
        "anchor_created_ts": float(anchor_created_ts),
        "anchor_lag_s": float(anchor_lag_s),
    }


def manager_zero_anchor_release_contract(
    status: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    cap_amp: Any = 0,
    min_amp: Any = 6,
    budget_ready: bool = False,
    fresh_budget_authorized: bool = False,
    mode_off: bool = False,
    priority_forced_stop: bool = False,
    ended_latched: bool = False,
    manual_pause: bool = False,
    locked: bool = False,
    emergency_stop: bool = False,
    manager_stop_pending: bool = False,
    phase_transition_active: bool = False,
    phase_recovery_ambiguous: bool = False,
    allow_soft_start_reject: bool = False,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Löst nur einen manager-eigenen temporären Nullanker ohne Geräte-I/O.

    Der alte Nullanker darf nicht selbst zur Bedingung werden, die den zum
    Verlassen nötigen positiven Befehl sperrt. Dieser Vertrag erkennt daher nur
    ausdrücklich freigegebene temporäre Policy-Gründe. Nutzer-Aus, Ladeende,
    Notfall, Priorität, Phase/CP und Stale-Data-Sperren bleiben maßgeblich.
    """

    st = status if isinstance(status, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    min_current = max(1.0, _safe_float(min_amp, 6.0))
    cap = max(0.0, _safe_float(cap_amp, 0.0))
    typed_anchor = (
        data.get("_manager_zero_anchor_contract")
        if isinstance(data.get("_manager_zero_anchor_contract"), dict)
        else {}
    )
    anchor_active = bool(data.get("_manager_zero_anchor_active", False))
    anchor_reason = str(typed_anchor.get("reason_code") or "")
    legacy_anchor_reason = str(
        data.get("_last_manager_zero_anchor_reason") or ""
    )
    typed_anchor_valid = bool(
        str(typed_anchor.get("contract") or "")
        == "wallbox_manager_zero_anchor_v1"
        and str(typed_anchor.get("owner") or "") == "wallbox_manager"
        and anchor_reason
    )
    anchor_class = str(
        typed_anchor.get("class")
        or (
            "temporary_policy_zero"
            if anchor_reason in RELEASABLE_ZERO_ANCHOR_REASONS
            else "hard_stop"
        )
    )
    anchor_vehicle_key = str(typed_anchor.get("session_vehicle_key") or "")
    anchor_plug_session_id = str(typed_anchor.get("plug_session_id") or "")
    session_vehicle_key = str(
        data.get("_session_vehicle_key")
        or data.get("session_vehicle_key")
        or ""
    )
    plug_session_id = str(data.get("_openwb_pro_plug_session_id") or "")
    wakeup_disconnect = manager_owned_wakeup_disconnect_contract(
        st,
        data,
        now_ts=now_value,
    )
    session_bound_reason = bool(
        anchor_reason in SESSION_BOUND_RELEASABLE_ZERO_ANCHOR_REASONS
        or anchor_reason == "no_vehicle_connected"
    )
    plug_session_matches = bool(
        anchor_plug_session_id
        and plug_session_id
        and anchor_plug_session_id == plug_session_id
    )
    soft_until = _safe_float(data.get("_openwb_start_reject_soft_until"), 0.0)
    soft_reject_due = bool(
        allow_soft_start_reject
        and anchor_reason == "vehicle_start_rejected_soft"
        and str(typed_anchor.get("owner") or "") == "wallbox_manager"
        and str(data.get("_bev_full_block_reason") or "") == "start_rejected_soft"
        and not bool(data.get("_bev_full_blocked", False))
        and soft_until > 0.0
        and now_value >= soft_until
    )
    legacy_releasable_anchor = bool(
        typed_anchor_valid
        and anchor_reason in RELEASABLE_ZERO_ANCHOR_REASONS
        and anchor_class == "hard_stop"
        and anchor_plug_session_id
        and anchor_plug_session_id == plug_session_id
        and (
            not anchor_vehicle_key
            or anchor_vehicle_key == session_vehicle_key
        )
    )
    legacy_floor_anchor = bool(
        legacy_releasable_anchor
        and anchor_reason == "wbminsoc_floor_pause"
    )
    regular_anchor = bool(
        anchor_reason in RELEASABLE_ZERO_ANCHOR_REASONS
        and (
            anchor_class == "temporary_policy_zero"
            or legacy_releasable_anchor
        )
        and (not session_bound_reason or plug_session_matches)
    )
    anchor_releasable = bool(
        typed_anchor_valid
        and (
            regular_anchor
            or soft_reject_due
            or wakeup_disconnect.get("anchor_releasable", False)
        )
    )
    status_valid = bool(
        st
        and st.get("driver_status_valid") is True
        and st.get("driver_status_stale") is not True
        and st.get("driver_status_degraded") is not True
        and st.get("driver_status_glitch") is not True
    )
    connected = bool(status_connected(st))
    cp_inactive = _explicit_inactive(st.get("cp_interrupt_isactive"))
    blockers = []
    if not anchor_active:
        blockers.append("zero_anchor_not_active")
    if not typed_anchor_valid:
        blockers.append("typed_zero_anchor_required")
    if not anchor_releasable:
        blockers.append("zero_anchor_reason_not_releasable")
    if session_bound_reason and not anchor_plug_session_id:
        blockers.append("anchor_plug_session_id_required")
    if session_bound_reason and not plug_session_id:
        blockers.append("current_plug_session_id_required")
    if anchor_vehicle_key and anchor_vehicle_key != session_vehicle_key:
        blockers.append("session_vehicle_key_mismatch")
    if anchor_plug_session_id and anchor_plug_session_id != plug_session_id:
        blockers.append("plug_session_id_mismatch")
    if not status_valid:
        blockers.append("fresh_valid_status_required")
    if not connected:
        blockers.append("vehicle_not_connected")
    if not cp_inactive:
        blockers.append("cp_not_explicitly_inactive")
    if cap < min_current or not bool(budget_ready):
        blockers.append("positive_executable_budget_required")
    if not bool(fresh_budget_authorized):
        blockers.append("fresh_budget_authorization_required")
    if mode_off:
        blockers.append("mode_off")
    if priority_forced_stop:
        blockers.append("priority_forced_stop")
    if ended_latched:
        blockers.append("vehicle_finished")
    if manual_pause:
        blockers.append("manual_pause")
    if locked:
        blockers.append("wallbox_locked")
    if emergency_stop:
        blockers.append("emergency_stop")
    if manager_stop_pending and not soft_reject_due:
        blockers.append("manager_stop_pending")
    if phase_transition_active:
        blockers.append("phase_transition_active")
    if phase_recovery_ambiguous:
        blockers.append("phase_recovery_ambiguous")

    allow = not blockers
    return {
        "contract": "openwb_pro_manager_zero_anchor_release_v1",
        "action": "release" if allow else "hold",
        "allow_release": bool(allow),
        "reason": "positive_policy_supersedes_own_zero" if allow else blockers[0],
        "blockers": blockers,
        "anchor_active": anchor_active,
        "anchor_reason": anchor_reason,
        "legacy_anchor_reason": legacy_anchor_reason,
        "typed_anchor_valid": typed_anchor_valid,
        "anchor_class": anchor_class,
        "legacy_releasable_anchor": legacy_releasable_anchor,
        "legacy_floor_anchor": legacy_floor_anchor,
        "soft_reject_due": bool(soft_reject_due),
        "manager_owned_wakeup_disconnect": wakeup_disconnect,
        "anchor_vehicle_key": anchor_vehicle_key,
        "session_vehicle_key": session_vehicle_key,
        "anchor_plug_session_id": anchor_plug_session_id,
        "plug_session_id": plug_session_id,
        "status_valid": status_valid,
        "connected": connected,
        "cp_inactive": cp_inactive,
        "cap_amp": float(cap),
        "min_amp": float(min_current),
        "budget_ready": bool(budget_ready),
        "fresh_budget_authorized": bool(fresh_budget_authorized),
        "phase_transition_active": bool(phase_transition_active),
        "phase_recovery_ambiguous": bool(phase_recovery_ambiguous),
        "state_patch": (
            {
                "_manager_zero_anchor_active": False,
                "_manager_zero_anchor_contract": None,
                "_wb_stop_sent_active": False,
                "_bev_full_block_reason": "",
                "_openwb_start_reject_soft_until": 0.0,
                "abort_count": 0,
                "abort_cooldown_ts": 0.0,
            }
            if allow
            else {}
        ),
        "ts": float(now_value),
    }


def confirmed_charge_soft_stop_release_contract(
    status: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    mode_off: bool = False,
    priority_forced_stop: bool = False,
    manual_pause: bool = False,
    locked: bool = False,
    emergency_stop: bool = False,
) -> Dict[str, Any]:
    """Beendet nur einen überholten manager-eigenen Soft-Stop.

    Ein frischer, real messbarer und phasenkohärenter Ladevorgang ist die
    autoritative Bestätigung, dass der frühere Startablehnungs-Soft-Stop nicht
    mehr aktiv sein kann. Nutzer-Aus und Schutzsperren bleiben unantastbar.
    """

    st = status if isinstance(status, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    sess = (
        data.get("_openwb_pro_session")
        if isinstance(data.get("_openwb_pro_session"), dict)
        else {}
    )
    temp = (
        sess.get("temporary_stop")
        if isinstance(sess.get("temporary_stop"), dict)
        else {}
    )
    typed_anchor = (
        data.get("_manager_zero_anchor_contract")
        if isinstance(data.get("_manager_zero_anchor_contract"), dict)
        else {}
    )
    anchor_reason = str(typed_anchor.get("reason_code") or "")
    legacy_anchor_reason = str(
        data.get("_last_manager_zero_anchor_reason") or ""
    )
    temp_reason = str(
        sess.get("temporary_stop_reason")
        or temp.get("reason")
        or ""
    )
    temp_hint = str(
        sess.get("temporary_stop_state_hint")
        or temp.get("state_hint")
        or ""
    )
    soft_until = _safe_float(data.get("_openwb_start_reject_soft_until"), 0.0)
    target = _valid_phase_count(st.get("phases_target"), 0)
    actual = _valid_phase_count(st.get("phases_actual"), 0)
    in_use = _valid_phase_count(st.get("phases_in_use"), 0)
    power_w = _phase_status_power_w(st)
    real_charging = bool(
        (st.get("charging") or st.get("charge_state"))
        and power_w > 500.0
    )

    reservation = (
        data.get("_wallbox_phase_transition_reservation")
        if isinstance(data.get("_wallbox_phase_transition_reservation"), dict)
        else {}
    )
    intent = (
        data.get("_openwb_pro_phase_output_intent")
        if isinstance(data.get("_openwb_pro_phase_output_intent"), dict)
        else {}
    )
    ack = (
        data.get("_openwb_pro_phase_output_ack")
        if isinstance(data.get("_openwb_pro_phase_output_ack"), dict)
        else {}
    )
    phase_artifacts_present = bool(reservation or intent or ack)
    reservation_target = _valid_phase_count(reservation.get("target_phases"), 0)
    phase_generation_bound = bool(
        not phase_artifacts_present
        or (
            reservation.get("active") is True
            and reservation_target == target
            and str(reservation.get("grant_state") or "") == "committed"
            and _safe_int(reservation.get("committed_w"), 0)
            >= _safe_int(reservation.get("requested_w"), 0)
            and str(intent.get("action") or "") == "send_phase"
            and _valid_phase_count(intent.get("target"), 0) == target
            and ack.get("success") is True
            and str(ack.get("intent_id") or "")
            == str(intent.get("intent_id") or "")
        )
    )

    checks = {
        "own_zero_anchor": bool(
            data.get("_manager_zero_anchor_active", False)
            and str(typed_anchor.get("contract") or "")
            == "wallbox_manager_zero_anchor_v1"
            and str(typed_anchor.get("owner") or "") == "wallbox_manager"
            and anchor_reason == "vehicle_start_rejected_soft"
        ),
        "own_soft_reason": bool(
            str(data.get("_bev_full_block_reason") or "")
            == "start_rejected_soft"
            and not bool(data.get("_bev_full_blocked", False))
        ),
        "soft_stop_elapsed": bool(soft_until > 0.0 and now_value >= soft_until),
        "session_soft_stop": bool(
            str(sess.get("state") or "") == "stopping"
            and (
                temp_reason == "vehicle_start_rejected_soft"
                or temp_hint in ("stopping", "waiting_start_release")
            )
        ),
        "fresh_status": _fresh_valid_status(st),
        "connected": bool(status_connected(st)),
        "cp_inactive": _explicit_inactive(st.get("cp_interrupt_isactive")),
        "real_charging": real_charging,
        "positive_matching_phases": bool(
            target in (1, 3)
            and (actual == target or in_use == target)
        ),
        "phase_generation_bound": phase_generation_bound,
        "mode_enabled": not bool(mode_off),
        "no_priority_stop": not bool(priority_forced_stop),
        "no_manual_pause": not bool(manual_pause),
        "not_locked": not bool(locked),
        "no_emergency_stop": not bool(emergency_stop),
    }
    blockers = [name for name, passed in checks.items() if not passed]
    allow = not blockers
    return {
        "contract": "openwb_pro_confirmed_charge_soft_stop_release_v1",
        "allow_release": allow,
        "reason": (
            "fresh_real_charge_supersedes_elapsed_soft_stop"
            if allow
            else blockers[0]
        ),
        "checks": checks,
        "blockers": blockers,
        "anchor_reason": anchor_reason,
        "legacy_anchor_reason": legacy_anchor_reason,
        "power_w": float(power_w),
        "target_phases": int(target),
        "actual_phases": int(actual),
        "phases_in_use": int(in_use),
        "state_patch": (
            {
                "_manager_zero_anchor_active": False,
                "_manager_zero_anchor_contract": None,
                "_wb_stop_sent_active": False,
                "_bev_full_block_reason": "",
                "_openwb_start_reject_soft_until": 0.0,
                "abort_count": 0,
                "abort_cooldown_ts": 0.0,
                "_last_manager_stop_request_ts": 0.0,
                "_last_manager_stop_request_reason": "",
                "_openwb_pro_session_state": "charging",
            }
            if allow
            else {}
        ),
        "session_patch": (
            {
                "state": "charging",
                "start_blocked": False,
                "stop_active": False,
                "temporary_stop_active": False,
                "temporary_stop_reason": "",
                "temporary_stop_state_hint": "",
                "temporary_stop": {},
                "can_send_start_command": True,
            }
            if allow
            else {}
        ),
        "ts": float(now_value),
    }


def start_liveness_contract(
    session: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    status: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    timeout_s: Any = 180,
) -> Dict[str, Any]:
    """Make a silently unserved, otherwise executable plug session visible."""

    sess = session if isinstance(session, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    st = status if isinstance(status, dict) else {}
    now_value = _safe_float(now_ts, 0.0)
    timeout = max(30.0, _safe_float(timeout_s, 180.0))
    plug_session_id = str(
        data.get("_openwb_pro_plug_session_id") or ""
    )
    timer_plug_session_id = str(
        data.get("_openwb_pro_unserved_plug_session_id") or ""
    )
    status_fresh = _fresh_valid_status(st)
    timer_session_matches = bool(
        plug_session_id
        and timer_plug_session_id == plug_session_id
    )
    phase_sequence = data.get("_openwb_pro_phase_sequence")
    phase_sequence_active = bool(
        isinstance(phase_sequence, dict)
        and phase_sequence
        and str(phase_sequence.get("stage") or "") not in ("", "ready")
    )
    phase_wait_active_now = bool(
        _safe_float(data.get("_openwb_pro_phase_wait_until"), 0.0) > now_value
    )
    wakeup_wait_active = bool(
        _safe_float(data.get("_openwb_pro_start_wakeup_allowed_after"), 0.0)
        > now_value
        or data.get("_openwb_pro_start_wakeup_pending", False)
        or not _explicit_inactive(st.get("cp_interrupt_isactive"))
    )
    expected_wait = bool(
        str(sess.get("state") or "") in (STATE_PHASE_WAIT, STATE_WAKEUP)
        or phase_sequence_active
        or phase_wait_active_now
        or wakeup_wait_active
    )
    had_confirmed_charge = bool(
        sess.get("had_confirmed_charge", False)
        or sess.get("real_charging", False)
        or _safe_float(st.get("session_kwh"), 0.0) > 0.005
        or _safe_float(st.get("power_w"), 0.0) > 100.0
        or _safe_float(st.get("real_power_w"), 0.0) > 100.0
        or _safe_float(st.get("phase_power_sum_w"), 0.0) > 100.0
        or bool(data.get("_charge_started", False))
    )
    start_scope = bool(
        plug_session_id
        and status_fresh
        and sess.get("connected", False)
        and sess.get("start_requested", False)
        and sess.get("budget_ready", False)
        and not sess.get("real_charging", False)
        and not had_confirmed_charge
    )
    issued_session_id = str(
        data.get("_openwb_pro_start_current_session_id") or ""
    )
    issued_ts = _safe_float(
        data.get("_openwb_pro_start_current_issued_ts"),
        0.0,
    )
    receipt_bound = bool(
        plug_session_id
        and issued_session_id == plug_session_id
        and issued_ts > 0.0
        and issued_ts <= now_value + 5.0
    )
    # Eine bloße fachliche Startbereitschaft ist noch keine reale
    # Fahrzeug-Antwortfrist. Erst der erfolgreiche positive Treiber-Receipt
    # derselben Stecksession darf den Timer scharf schalten.
    eligible = bool(start_scope and receipt_bound and not expected_wait)
    previous_since = (
        _safe_float(data.get("_openwb_pro_unserved_since"), 0.0)
        if timer_session_matches
        else 0.0
    )
    pause_since = (
        _safe_float(data.get("_openwb_pro_unserved_pause_since"), 0.0)
        if timer_session_matches
        else 0.0
    )
    paused_total = (
        max(
            0.0,
            _safe_float(data.get("_openwb_pro_unserved_paused_s"), 0.0),
        )
        if timer_session_matches
        else 0.0
    )
    state_patch: Dict[str, Any] = {}
    receipt_reanchors_timer = bool(
        receipt_bound
        and previous_since > 0.0
        and previous_since < issued_ts
    )
    if receipt_reanchors_timer:
        previous_since = issued_ts
        pause_since = 0.0
        paused_total = 0.0
    if start_scope and expected_wait and receipt_bound:
        since = max(previous_since, issued_ts)
        if pause_since <= 0.0:
            pause_since = now_value
        state_patch = {
            "_openwb_pro_unserved_since": since,
            "_openwb_pro_unserved_pause_since": pause_since,
            "_openwb_pro_unserved_paused_s": paused_total,
            "_openwb_pro_unserved_plug_session_id": plug_session_id,
        }
    elif eligible:
        since = max(previous_since, issued_ts)
        if pause_since > 0.0:
            paused_total += max(0.0, now_value - pause_since)
        pause_since = 0.0
        state_patch = {
            "_openwb_pro_unserved_since": since,
            "_openwb_pro_unserved_pause_since": 0.0,
            "_openwb_pro_unserved_paused_s": paused_total,
            "_openwb_pro_unserved_plug_session_id": plug_session_id,
        }
    else:
        since = 0.0
        pause_since = 0.0
        paused_total = 0.0
        state_patch = {
            "_openwb_pro_unserved_since": 0.0,
            "_openwb_pro_unserved_pause_since": 0.0,
            "_openwb_pro_unserved_paused_s": 0.0,
            "_openwb_pro_unserved_plug_session_id": None,
        }
    age_s = (
        max(0.0, now_value - since - paused_total)
        if eligible and since > 0.0
        else 0.0
    )
    issued_positive = bool(receipt_bound and since > 0.0)
    offered_amp = (
        max(
            _safe_float(st.get("amp"), 0.0),
            _safe_float(st.get("offered_current_raw"), 0.0),
            _safe_float(st.get("evse_current"), 0.0),
        )
        if status_fresh
        else 0.0
    )
    alert = bool(eligible and age_s >= timeout)
    awaiting_receipt = bool(
        start_scope
        and not expected_wait
        and not receipt_bound
    )
    if awaiting_receipt:
        state = "waiting"
        code = "START_PENDING_RECEIPT"
    elif not eligible:
        state = "inactive"
        code = ""
    elif not alert:
        state = "waiting"
        code = "START_PENDING"
    elif sess.get("start_blocked", False):
        state = "failed"
        code = "START_FAILED_BLOCKED"
    elif not issued_positive:
        state = "failed"
        code = "START_FAILED_NOT_ISSUED"
    elif offered_amp < 6.0:
        state = "failed"
        code = "START_FAILED_NO_READBACK"
    else:
        state = "failed"
        code = "START_FAILED_NO_CHARGE_CONFIRMATION"
    return {
        "contract": "openwb_pro_start_liveness_v2",
        "state": state,
        "code": code,
        "eligible": eligible,
        "alert": alert,
        "expected_wait": expected_wait,
        "paused": bool(start_scope and expected_wait),
        "paused_s": float(paused_total),
        "state_patch": state_patch,
        "start_blocked": bool(sess.get("start_blocked", False)),
        "blocker": str(
            sess.get("temporary_stop_reason")
            or sess.get("reason")
            or ""
        ),
        "since_ts": float(since),
        "age_s": float(age_s),
        "timeout_s": float(timeout),
        "issued_positive": issued_positive,
        "issued_ts": float(issued_ts),
        "receipt_bound": receipt_bound,
        "receipt_reanchors_timer": receipt_reanchors_timer,
        "awaiting_receipt": awaiting_receipt,
        "issued_plug_session_id": issued_session_id,
        "offered_amp": float(offered_amp),
        "plug_session_id": plug_session_id,
        "timer_plug_session_id": (
            plug_session_id if start_scope else ""
        ),
        "timer_session_reset": bool(
            timer_plug_session_id
            and timer_plug_session_id != plug_session_id
        ),
        "session_bound": bool(plug_session_id),
        "status_fresh": bool(status_fresh),
        "ts": float(now_value),
    }


def start_retry_guard_contract(
    command: Optional[Dict[str, Any]] = None,
    state_data: Optional[Dict[str, Any]] = None,
    *,
    session: Optional[Dict[str, Any]] = None,
    now_ts: Any = 0,
    reason: str = "",
    start_window: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Decide whether a start/retry command may bypass the chatter guard.

    The command guard is still the single executor-side protection.  This pure
    contract only identifies the narrow openWB-Pro retry cases that belong to
    the current session state, and blocks retries during EMS stop states or a
    latched vehicle-finished state.
    """

    cmd = command if isinstance(command, dict) else {}
    data = state_data if isinstance(state_data, dict) else {}
    sess = session if isinstance(session, dict) else (
        data.get("_openwb_pro_session") if isinstance(data.get("_openwb_pro_session"), dict) else {}
    )
    now_value = _safe_float(now_ts, 0.0)
    method = str(cmd.get("method") or cmd.get("kind") or "").strip().lower()
    kind = str(cmd.get("kind") or method or "").strip().lower()
    amp_value = _safe_float(cmd.get("amp"), 0.0)
    session_state = str(sess.get("state") or data.get("_openwb_pro_session_state") or "").strip().lower()
    hold_until = _safe_float(data.get("_openwb_pro_start_hold_until"), 0.0)
    hold_amp = _safe_float(data.get("_openwb_pro_start_hold_amp"), 0.0)
    hold_active = bool(sess.get("start_hold_active", False)) or (
        hold_until > now_value and hold_amp >= 6.0
    )
    phase_wait_until = _safe_float(data.get("_openwb_pro_phase_wait_until"), 0.0)
    phase_wait_target = _valid_phase_count(data.get("_openwb_pro_phase_wait_target"), 0)
    phase_wait_active_guard = bool(
        phase_wait_target in (1, 3)
        and phase_wait_until > now_value
    )
    start_verifying = bool(sess.get("start_verifying", False)) or session_state == STATE_STARTING
    budget_ready = bool(sess.get("budget_ready", False))
    can_send = bool(sess.get("can_send_start_command", False))
    stop_active = bool(sess.get("stop_active", False))
    start_blocked = bool(sess.get("start_blocked", False))
    temp_stop = sess.get("temporary_stop") if isinstance(sess.get("temporary_stop"), dict) else {}
    temporary_stop_active = bool(
        sess.get("temporary_stop_active", False)
        or temp_stop.get("active", False)
    )
    temporary_stop_hint = str(
        sess.get("temporary_stop_state_hint")
        or temp_stop.get("state_hint")
        or ""
    )
    temporary_stop_reason = str(
        sess.get("temporary_stop_reason")
        or temp_stop.get("reason")
        or ""
    )
    vehicle_finished = bool(
        data.get("_bev_full_blocked", False)
        or session_state == STATE_ENDED
        or temporary_stop_hint == "vehicle_finished"
    )
    reason_text = " ".join(
        str(part or "")
        for part in (cmd.get("reason"), cmd.get("source"), reason)
    ).lower()
    retry_command = bool(
        "openwb_start_retry" in reason_text
        or "openwb_pro_keepalive" in reason_text
        or "phase_decision_apply_current" in reason_text
        or "openwb_pro_curve_direct" in reason_text
        or "set_current" in reason_text
    )
    phase_restart_contract = (
        data.get("_openwb_pro_phase_restart_current_contract")
        if isinstance(
            data.get("_openwb_pro_phase_restart_current_contract"),
            dict,
        )
        else {}
    )
    phase_restart_command = bool(
        cmd.get("_openwb_pro_phase_restart_current") is True
        and phase_restart_contract.get("authorized") is True
    )
    soft_retry_due = bool(
        str(data.get("_bev_full_block_reason") or "") == "start_rejected_soft"
        and _safe_float(data.get("_openwb_start_reject_soft_until"), 0.0) > 0.0
        and now_value >= _safe_float(data.get("_openwb_start_reject_soft_until"), 0.0)
        and not bool(data.get("_bev_full_blocked", False))
    )
    soft_retry_releases_own_stop = bool(
        soft_retry_due
        and temporary_stop_active
        and temporary_stop_reason == "vehicle_start_rejected_soft"
        and temporary_stop_hint in ("stopping", "waiting_start_release")
        and budget_ready
    )
    window_memo = (
        start_window
        if isinstance(start_window, dict)
        else data.get("_openwb_pro_start_window_contract")
    )
    window_memo = window_memo if isinstance(window_memo, dict) else {}
    no_contactor_cycle = bool(
        temporary_stop_hint in ("stop_command_settled", "waiting_start_release")
        and not bool(data.get("_had_real_charge_in_session", False))
        and window_memo.get("active") is True
    )
    command_valid = bool(
        (
            method in ("set_amp_and_state", "set_current", "set_direct_current")
            or kind in ("set_current", "hold_current")
        )
        and amp_value >= 6.0
    )
    try:
        if int(float(cmd.get("force_state"))) == 1:
            command_valid = False
    except Exception:
        pass

    allow = False
    block_reason = ""
    if not command_valid:
        block_reason = "not_start_current_command"
    elif not retry_command:
        block_reason = "not_retry_command"
    elif vehicle_finished:
        block_reason = "vehicle_finished"
    elif phase_restart_command and budget_ready:
        allow = True
        block_reason = "confirmed_phase_restart"
    elif soft_retry_releases_own_stop:
        allow = True
        block_reason = "soft_reject_retry_due"
    elif temporary_stop_active and no_contactor_cycle:
        # Ein eigener Politik-Nullausgang ohne gemessene Ladung
        # ist kein Schützzyklus; das Startfenster darf erneut anbieten.
        allow = True
        block_reason = "start_window_retry_no_contactor"
    elif temporary_stop_active:
        block_reason = "temporary_stop_active"
    elif stop_active:
        block_reason = "stop_active"
    elif start_blocked:
        block_reason = "start_blocked"
    elif phase_wait_active_guard:
        allow = True
        block_reason = "phase_wait_no_cp"
    elif (
        start_verifying
        or hold_active
        or session_state in (STATE_OFFERED, STATE_PHASE_WAIT)
    ):
        if budget_ready and can_send:
            allow = True
            block_reason = "start_verification_retry"
        elif not budget_ready:
            block_reason = "budget_not_ready"
        else:
            block_reason = "cannot_send_start_command"
    elif soft_retry_due:
        allow = True
        block_reason = "soft_reject_retry_due"
    else:
        block_reason = "no_retry_scope"

    return {
        "contract": "openwb_pro_start_retry_guard_v1",
        "allow_override": bool(allow),
        "reason": block_reason,
        "command_valid": bool(command_valid),
        "retry_command": bool(retry_command),
        "soft_retry_due": bool(soft_retry_due),
        "phase_restart_command": bool(phase_restart_command),
        "session_state": session_state,
        "start_verifying": bool(start_verifying),
        "hold_active": bool(hold_active),
        "phase_wait_active": bool(phase_wait_active_guard),
        "budget_ready": bool(budget_ready),
        "can_send_start_command": bool(can_send),
        "stop_active": bool(stop_active),
        "temporary_stop_active": bool(temporary_stop_active),
        "temporary_stop_state_hint": temporary_stop_hint,
        "temporary_stop_reason": temporary_stop_reason,
        "soft_retry_releases_own_stop": bool(soft_retry_releases_own_stop),
        "vehicle_finished": bool(vehicle_finished),
        "start_blocked": bool(start_blocked),
        "amp": float(amp_value),
        "ts": float(now_value),
    }


def phase_min_settle_s(config: Optional[Dict[str, Any]] = None) -> float:
    cfg = config or {}
    return min(
        30.0,
        max(
            2.0,
            _safe_float(cfg.get("openwb_pro_phase_min_settle_s", 3), 3.0),
        ),
    )


def phase_target_adoption_settle_s(config: Optional[Dict[str, Any]] = None) -> float:
    """Kurze Stabilitätszeit für ein bereits passendes, frisches Phasenziel."""

    cfg = config or {}
    return min(
        10.0,
        max(
            1.0,
            _safe_float(cfg.get("openwb_pro_phase_target_adoption_settle_s", 2), 2.0),
        ),
    )


def _record_phase_wait_measurement(
    state: Dict[str, Any],
    *,
    now_ts: float,
    target: int,
    actual: int,
    in_use: int,
    measured_power_w: float,
    result: str,
) -> None:
    if not isinstance(state, dict):
        return
    since = _safe_float(state.get("_openwb_pro_phase_wait_since", 0.0), 0.0)
    duration_s = max(0.0, float(now_ts or 0.0) - since) if since > 0.0 and now_ts > 0.0 else 0.0
    samples = max(0, _safe_int(state.get("_openwb_pro_phase_wait_samples", 0), 0))
    previous_ema = _safe_float(state.get("_openwb_pro_phase_wait_ema_s", 0.0), 0.0)
    previous_max = _safe_float(state.get("_openwb_pro_phase_wait_max_s", 0.0), 0.0)
    if duration_s > 0.0:
        samples += 1
        ema = duration_s if previous_ema <= 0.0 else previous_ema * 0.75 + duration_s * 0.25
    else:
        ema = previous_ema
    state["_openwb_pro_phase_wait_last_duration_s"] = round(duration_s, 1)
    state["_openwb_pro_phase_wait_last_result"] = str(result or "")
    state["_openwb_pro_phase_wait_last_target"] = int(target or 0)
    state["_openwb_pro_phase_wait_last_actual"] = int(actual or 0)
    state["_openwb_pro_phase_wait_last_in_use"] = int(in_use or 0)
    state["_openwb_pro_phase_wait_last_power_w"] = round(max(0.0, float(measured_power_w or 0.0)), 1)
    state["_openwb_pro_phase_wait_last_ts"] = float(now_ts or 0.0)
    state["_openwb_pro_phase_wait_samples"] = samples
    state["_openwb_pro_phase_wait_ema_s"] = round(ema, 1)
    state["_openwb_pro_phase_wait_max_s"] = round(max(previous_max, duration_s), 1)


def mark_phase_wait(
    state: Dict[str, Any],
    phases: Any,
    *,
    current_amp: Any = 0,
    now_ts: Any = None,
    config: Optional[Dict[str, Any]] = None,
    charger_max_amp: Any = 16,
) -> None:
    """Remember that openWB Pro is settling after a phasetarget command."""

    if not isinstance(state, dict):
        return
    target = _valid_phase_count(phases, 0)
    if target not in (1, 3):
        return
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    max_amp = max(6.0, _safe_float(charger_max_amp, 16.0))
    amp_value = _safe_float(current_amp, 0.0)
    if amp_value >= 6.0:
        amp_value = round(max(6.0, min(max_amp, amp_value)), 1)
    else:
        amp_value = 0.0
    cooldown_s = phase_wait_s(config)
    min_settle_s = phase_min_settle_s(config)
    state["_openwb_pro_phase_wait_target"] = target
    state["_openwb_pro_phase_wait_until"] = now_value + min_settle_s
    state["_openwb_pro_phase_wait_min_until"] = now_value + min_settle_s
    state["_openwb_pro_phase_change_block_until"] = max(
        _safe_float(state.get("_openwb_pro_phase_change_block_until"), 0.0),
        now_value + cooldown_s,
    )
    state["_openwb_pro_phase_wait_amp"] = amp_value
    state["_openwb_pro_phase_wait_since"] = now_value


def clear_phase_wait(state: Dict[str, Any]) -> None:
    if not isinstance(state, dict):
        return
    state["_openwb_pro_phase_wait_target"] = 0
    state["_openwb_pro_phase_wait_until"] = 0.0
    state["_openwb_pro_phase_wait_min_until"] = 0.0
    state["_openwb_pro_phase_wait_amp"] = 0
    state["_openwb_pro_phase_wait_since"] = 0.0


def phase_wait_active(
    state: Dict[str, Any],
    status: Optional[Dict[str, Any]] = None,
    now_ts: Any = None,
    *,
    stable_hw_power_w: Any = 0.0,
) -> bool:
    if not isinstance(state, dict):
        return False
    now_value = _safe_float(now_ts, 0.0)
    if now_value <= 0.0:
        import time

        now_value = time.time()
    hold_until = _safe_float(state.get("_openwb_pro_phase_wait_until", 0.0), 0.0)
    if hold_until <= 0.0:
        return False
    min_until = _safe_float(state.get("_openwb_pro_phase_wait_min_until", 0.0), 0.0)
    min_settle_active = bool(min_until > 0.0 and now_value < min_until)
    target = _valid_phase_count(state.get("_openwb_pro_phase_wait_target"), 0)
    if target not in (1, 3):
        clear_phase_wait(state)
        return False
    st = status or {}
    status_target = _valid_phase_count(st.get("phases_target"), target)
    actual = _valid_phase_count(st.get("phases_actual"), 0)
    in_use = _valid_phase_count(st.get("phases_in_use"), 0)
    measured_power_w = max(
        _safe_float(st.get("real_power_w", st.get("power_w", 0.0)), 0.0),
        _safe_float(st.get("phase_power_sum_w", 0.0), 0.0),
        _safe_float(stable_hw_power_w, 0.0),
    )
    if now_value >= hold_until:
        sequence = state.get("_openwb_pro_phase_sequence")
        if (
            isinstance(sequence, dict)
            and sequence
            and str(sequence.get("stage") or "") not in ("", "ready")
        ):
            # Der Manager muss die Sequenz jetzt noch einmal ticken, damit
            # frisches Phasenziel und inaktiver CP bestätigt werden. Erst
            # ``ready`` räumt den kurzen Wait auf.
            return True
        confirmed_after_hold = bool(
            measured_power_w > 500.0
            and (
                actual == target
                or in_use == target
            )
        )
        _record_phase_wait_measurement(
            state,
            now_ts=now_value,
            target=target,
            actual=actual,
            in_use=in_use,
            measured_power_w=measured_power_w,
            result="confirmed_after_hold" if confirmed_after_hold else "timeout",
        )
        clear_phase_wait(state)
        state["_last_phase_switch_ts"] = now_value
        return False
    if status_target not in (0, target):
        return True
    # Dieses Fenster tickt nur die kurze Geräte-Settle-Sequenz. Die separate
    # ``_openwb_pro_phase_change_block_until``-Frist verhindert weiterhin für
    # 480 s einen erneuten Phasenwechsel, blockiert aber keinen Ladestrom.
    if actual == target or in_use == target or measured_power_w > 500.0 or min_settle_active:
        return True
    return True


def direct_bulk_ready(
    status: Optional[Dict[str, Any]] = None,
    *,
    hw_charging: bool = False,
    stable_hw_power_w: Any = 0.0,
) -> bool:
    """Return True once openWB Pro/BEV confirmed that a start or phase switch is real."""

    if hw_charging:
        return True
    st = status or {}
    measured_power_w = max(
        _safe_float(st.get("real_power_w", st.get("power_w", 0.0)), 0.0),
        _safe_float(st.get("phase_power_sum_w", 0.0), 0.0),
        _safe_float(stable_hw_power_w, 0.0),
    )
    return bool(measured_power_w > 500.0)


def direct_target_amp(
    current_amp: Any,
    direct_amp: Any,
    direct_direction: Any,
    *,
    bulk_ready: bool = False,
    start_amp: Any = 6,
    down_step_a: Any = 2,
    current_step_amp: Any = 1.0,
) -> float:
    """Calculate openWB Pro PV-curve setpoint without slow +1A ramps after confirmation."""

    step = _current_step(current_step_amp, 1.0)
    current_value = max(0.0, _safe_float(current_amp, 0.0))
    direct_value = max(0.0, _safe_float(direct_amp, 0.0))
    start_value = max(6.0, _safe_float(start_amp, 6.0))
    down_step = max(step, _safe_float(down_step_a, 2.0))

    if _safe_int(direct_direction, 0) < 0:
        if direct_value <= 0:
            return 0.0
        return _round_to_step(max(direct_value, current_value - down_step, 6.0), step)
    if _safe_int(direct_direction, 0) > 0:
        if direct_value <= 0:
            return 0.0
        if current_value <= 0:
            return _round_to_step(min(direct_value, start_value), step)
        if bulk_ready:
            return _round_to_step(direct_value, step)
        return _round_to_step(min(direct_value, current_value + max(1.0, step)), step)
    return _round_to_step(current_value, step)


def evaluate_session(
    status: Optional[Dict[str, Any]],
    *,
    state_data: Optional[Dict[str, Any]] = None,
    current_set_amp: Any = 0,
    cap_amp: Any = 0,
    min_amp: Any = 6,
    budget_ready: bool = False,
    switch_to_1p_ready: bool = False,
    grid_allowed: bool = False,
    price_active: bool = False,
    price_boost_active: bool = False,
    predump_active: bool = False,
    mode_off: bool = False,
    priority_forced_stop: bool = False,
    stop_sent_active: bool = False,
    manager_stop_pending: bool = False,
    manager_stop_reason: str = "",
    ended_latched: bool = False,
    end_reason: str = "",
    last_start_ts: Any = 0,
    now_ts: Any = 0,
    start_verify_s: Any = 180,
    stable_hw_power_w: Any = 0.0,
    start_window: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Classify one manager-owned openWB Pro session.

    ``start_window`` (Memo des Startfensters) liefert den
    Angebotswert ohne ``cap``, den Zustand ``start_retry`` und die Texte.
    """

    st = status or {}
    data = state_data if isinstance(state_data, dict) else {}
    now = _safe_float(now_ts, 0.0)
    min_current = max(1.0, _safe_float(min_amp, 6.0))
    current_amp = max(0.0, _safe_float(current_set_amp, 0.0))
    cap = max(0.0, _safe_float(cap_amp, 0.0))
    hw_amp = max(
        0.0,
        _safe_float(st.get("amp", 0), 0.0),
        _safe_float(st.get("offered_current_raw", 0), 0.0),
        _safe_float(st.get("evse_current", 0), 0.0),
    )
    window = start_window if isinstance(start_window, dict) else {}
    window_active = bool(window.get("active", False))
    window_state = str(window.get("state") or "") if window_active else ""
    window_offer_amp = max(0.0, _safe_float(window.get("offer_amp"), 0.0)) if window_active else 0.0
    if window_active and window_offer_amp >= min_current:
        # Das stehende Fensterangebot ist die Anzeige-/Manager-Wahrheit, nicht der Deckel
        # (kein „13.0 A freigegeben“ bei 6 A Angebot). Vor dem Angebot (budget_wait, off_no_budget)
        # bleibt der Deckel Teil der Startanforderung, sonst wäre kein Startbefehl mehr sendbar.
        offered_amp = max(current_amp, hw_amp if hw_amp >= min_current else 0, window_offer_amp)
    else:
        offered_amp = max(current_amp, cap, hw_amp if hw_amp >= min_current else 0)
    connected = status_connected(st)
    real_power_w = max(status_real_power(st), _safe_float(stable_hw_power_w, 0.0))
    real_charging = bool(status_real_charging(st) or real_power_w > 500.0)
    last_start = _safe_float(last_start_ts, 0.0)
    verify_s = max(0.0, _safe_float(start_verify_s, 180.0))
    last_start_age_s = max(0.0, now - last_start) if now > 0.0 and last_start > 0.0 else None
    start_hold = start_hold_active(
        data,
        now,
        hw_charging=real_charging,
        stable_hw_power_w=real_power_w,
    )
    wakeup_wait_until = _safe_float(data.get("_openwb_pro_start_wakeup_allowed_after", 0.0), 0.0)
    wakeup_pending = bool(wakeup_wait_until > 0.0 and now > 0.0 and now < wakeup_wait_until)
    phase_wait = phase_wait_active(
        data,
        st,
        now,
        stable_hw_power_w=real_power_w,
    )
    start_requested = bool(
        connected
        and not mode_off
        and offered_amp >= min_current
        and (current_amp >= min_current or cap >= min_current or start_hold)
    )
    physical_budget_ready = bool(
        budget_ready
        or switch_to_1p_ready
        or grid_allowed
        or price_active
        or price_boost_active
        or predump_active
    )
    start_verifying = bool(
        not real_charging
        and start_requested
        and (
            start_hold
            or (
                last_start_age_s is not None
                and last_start_age_s <= verify_s
            )
            or window_state in (
                START_WINDOW_STATE_OFFER_PENDING,
                START_WINDOW_STATE_OFFER_FROZEN,
            )
        )
    )
    temporary_stop = temporary_ems_stop_contract(
        st,
        data,
        current_set_amp=current_amp,
        cap_amp=cap,
        min_amp=min_current,
        mode_off=mode_off,
        priority_forced_stop=priority_forced_stop,
        stop_sent_active=stop_sent_active,
        manager_stop_pending=manager_stop_pending,
        manager_stop_reason=manager_stop_reason,
        manager_zero_anchor_active=bool(data.get("_manager_zero_anchor_active", False)),
        ended_latched=ended_latched,
        now_ts=now,
        stable_hw_power_w=real_power_w,
    )
    stop_active = bool(temporary_stop.get("stopping", False))
    stop_hint = str(temporary_stop.get("state_hint") or "none")
    temporary_waiting = bool(stop_hint == "waiting_start_release")

    if not connected:
        state = STATE_IDLE
        reason = "Kein Fahrzeug verbunden."
    elif real_charging:
        state = STATE_CHARGING
        reason = "Echte Ladung mit %.0f W bestätigt." % real_power_w
        if window_state == START_WINDOW_STATE_CHARGING_CONFIRMED:
            reason = "%s %s" % (reason, start_window_reason_text(window, now))
    elif ended_latched:
        state = STATE_ENDED
        reason = (
            "Ladeende ist gelatcht; Neustart erst nach Umstecken, Moduswechsel "
            "oder neuer Nutzerfreigabe."
        )
    elif stop_active:
        state = STATE_STOPPING
        stop_reason = str(temporary_stop.get("reason") or "stop")
        reason = "Temporärer EMS-Stopp (%s); es wird keine neue openWB-Pro-Freigabe gesendet." % stop_reason
    elif priority_forced_stop:
        state = STATE_IDLE
        reason = "Startfreigabe ist durch die Regelung blockiert; es wird auf neue Freigabe gewartet."
    elif mode_off:
        state = STATE_IDLE
        reason = "Wallbox-Regelung ist aus; openWB Pro wird nur beobachtet."
    elif phase_wait:
        state = STATE_PHASE_WAIT
        reason = "Phasenwechsel läuft; Stromrampe wartet auf plausiblen Status."
    elif wakeup_pending:
        state = STATE_WAKEUP
        reason = "CP-Wake-up gesendet; Stromfreigabe wartet auf Einschaltverzögerung."
    elif window_state in (START_WINDOW_STATE_RETRY_WAIT, START_WINDOW_STATE_EXHAUSTED) and connected:
        state = STATE_START_RETRY
        reason = start_window_reason_text(window, now)
    elif start_verifying and window_state in (
        START_WINDOW_STATE_OFFER_PENDING,
        START_WINDOW_STATE_OFFER_FROZEN,
    ):
        state = STATE_STARTING
        reason = start_window_reason_text(window, now)
    elif start_verifying:
        state = STATE_STARTING
        reason = "%.1f A freigegeben; openWB Pro wartet auf echte Ladeleistung." % offered_amp
    elif start_requested:
        state = STATE_OFFERED
        reason = "%.1f A freigegeben; noch keine echte Ladebestätigung." % offered_amp
    elif temporary_waiting:
        state = STATE_IDLE
        reason = "Temporärer EMS-Stopp; wartet auf neue Startfreigabe oder Mindestleistung."
    else:
        state = STATE_IDLE
        reason = "Fahrzeug verbunden; keine Startfreigabe aktiv."

    start_blocked = bool(
        state in (STATE_STOPPING, STATE_ENDED)
        or mode_off
        or priority_forced_stop
        or temporary_stop.get("start_blocked", False)
    )
    can_send_start_command = bool(
        state in (STATE_OFFERED, STATE_STARTING, STATE_PHASE_WAIT, STATE_START_RETRY)
        and physical_budget_ready
        and not start_blocked
        and offered_amp >= min_current
    )
    if end_reason:
        reason = "%s (%s)" % (reason, end_reason)

    return {
        "contract": CONTRACT_NAME,
        "state": state,
        "label": _state_label(state),
        "level": _state_level(state),
        "reason": reason,
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "real_power_w": float(real_power_w if real_charging else 0.0),
        "offered_amp": float(offered_amp),
        "current_set_amp": float(current_amp),
        "cap_amp": float(cap),
        "hardware_amp": float(hw_amp),
        "budget_ready": bool(physical_budget_ready),
        "start_requested": bool(start_requested),
        "start_verifying": bool(start_verifying),
        "wakeup_pending": bool(wakeup_pending),
        "wakeup_remaining_s": (
            max(0.0, wakeup_wait_until - now)
            if wakeup_pending and now > 0.0
            else 0.0
        ),
        "start_hold_active": bool(start_hold),
        "phase_wait_active": bool(phase_wait),
        "phase_wait_target": int(data.get("_openwb_pro_phase_wait_target", 0) or 0),
        "phase_wait_since_s": (
            max(0.0, now - _safe_float(data.get("_openwb_pro_phase_wait_since", 0.0), 0.0))
            if phase_wait and now > 0.0 and _safe_float(data.get("_openwb_pro_phase_wait_since", 0.0), 0.0) > 0.0
            else 0.0
        ),
        "phase_wait_last_duration_s": _safe_float(data.get("_openwb_pro_phase_wait_last_duration_s", 0.0), 0.0),
        "phase_wait_last_result": str(data.get("_openwb_pro_phase_wait_last_result", "") or ""),
        "phase_wait_last_target": int(data.get("_openwb_pro_phase_wait_last_target", 0) or 0),
        "phase_wait_samples": int(data.get("_openwb_pro_phase_wait_samples", 0) or 0),
        "phase_wait_ema_s": _safe_float(data.get("_openwb_pro_phase_wait_ema_s", 0.0), 0.0),
        "phase_wait_max_s": _safe_float(data.get("_openwb_pro_phase_wait_max_s", 0.0), 0.0),
        "stop_active": bool(stop_active),
        "temporary_stop": temporary_stop,
        "temporary_stop_active": bool(temporary_stop.get("active", False)),
        "temporary_stop_reason": str(temporary_stop.get("reason") or ""),
        "temporary_stop_state_hint": stop_hint,
        "temporary_stop_waiting": bool(temporary_waiting),
        "last_start_age_s": last_start_age_s,
        "start_blocked": bool(start_blocked),
        "can_send_start_command": bool(can_send_start_command),
        "counts_as_real_charge": bool(real_charging),
        "start_window": dict(window) if window_active else {},
    }


def apply_session_to_status(status: Optional[Dict[str, Any]], session: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """Attach openWB Pro session diagnostics to a status dict in-place."""

    if status is None:
        return None
    status["openwb_pro_contract"] = session.get("contract", CONTRACT_NAME)
    status["openwb_pro_runtime_path"] = "python_wallbox_manager"
    status["openwb_pro_session_guard_required"] = True
    status["openwb_pro_charge_verification_required"] = True
    status["openwb_pro_session_state"] = session.get("state", STATE_IDLE)
    status["openwb_pro_session_label"] = session.get("label", _state_label(STATE_IDLE))
    status["openwb_pro_session_level"] = session.get("level", _state_level(STATE_IDLE))
    status["openwb_pro_session_reason"] = session.get("reason", "")
    status["openwb_pro_session_offered_amp"] = float(session.get("offered_amp", 0) or 0)
    status["openwb_pro_session_budget_ready"] = bool(session.get("budget_ready", False))
    status["openwb_pro_session_start_requested"] = bool(session.get("start_requested", False))
    status["openwb_pro_session_start_verifying"] = bool(session.get("start_verifying", False))
    status["openwb_pro_session_wakeup_pending"] = bool(session.get("wakeup_pending", False))
    status["openwb_pro_session_wakeup_remaining_s"] = float(session.get("wakeup_remaining_s", 0.0) or 0.0)
    status["openwb_pro_session_start_hold_active"] = bool(session.get("start_hold_active", False))
    status["openwb_pro_session_phase_wait_active"] = bool(session.get("phase_wait_active", False))
    status["openwb_pro_session_phase_wait_target"] = int(session.get("phase_wait_target", 0) or 0)
    status["openwb_pro_session_phase_wait_since_s"] = float(session.get("phase_wait_since_s", 0.0) or 0.0)
    status["openwb_pro_session_phase_wait_last_duration_s"] = float(session.get("phase_wait_last_duration_s", 0.0) or 0.0)
    status["openwb_pro_session_phase_wait_last_result"] = str(session.get("phase_wait_last_result", "") or "")
    status["openwb_pro_session_phase_wait_last_target"] = int(session.get("phase_wait_last_target", 0) or 0)
    status["openwb_pro_session_phase_wait_samples"] = int(session.get("phase_wait_samples", 0) or 0)
    status["openwb_pro_session_phase_wait_ema_s"] = float(session.get("phase_wait_ema_s", 0.0) or 0.0)
    status["openwb_pro_session_phase_wait_max_s"] = float(session.get("phase_wait_max_s", 0.0) or 0.0)
    status["openwb_pro_session_stop_active"] = bool(session.get("stop_active", False))
    status["openwb_pro_temporary_stop_contract"] = session.get("temporary_stop", {})
    status["openwb_pro_temporary_stop_active"] = bool(session.get("temporary_stop_active", False))
    status["openwb_pro_temporary_stop_reason"] = str(session.get("temporary_stop_reason", "") or "")
    status["openwb_pro_temporary_stop_state_hint"] = str(session.get("temporary_stop_state_hint", "") or "")
    status["openwb_pro_temporary_stop_waiting"] = bool(session.get("temporary_stop_waiting", False))
    status["openwb_pro_session_start_blocked"] = bool(session.get("start_blocked", False))
    status["openwb_pro_session_can_send_start_command"] = bool(session.get("can_send_start_command", False))
    status["openwb_pro_session_counts_as_real_charge"] = bool(session.get("counts_as_real_charge", False))
    # Startfenster-Skalare für Snapshot/Dashboard (index.php liest nur den Snapshot).
    window = session.get("start_window") if isinstance(session.get("start_window"), dict) else {}
    status["openwb_pro_start_window_state"] = str(window.get("state") or "inactive")
    status["openwb_pro_start_window_anchor_ts"] = _safe_float(window.get("anchor_ts"), 0.0)
    status["openwb_pro_start_window_offer_frozen_until"] = _safe_float(window.get("offer_frozen_until"), 0.0)
    status["openwb_pro_start_window_retry_cycle"] = str(window.get("retry_cycle") or "")
    status["openwb_pro_start_window_retry_next_ts"] = _safe_float(window.get("retry_next_ts"), 0.0)
    status["openwb_pro_start_window_reason"] = str(window.get("reason") or "")
    status["openwb_pro_start_window_evse_signaling"] = str(window.get("evse_signaling") or "")
    status["openwb_pro_start_window_command_ts"] = _safe_float(window.get("command_ts"), 0.0)
    liveness = session.get("start_liveness") if isinstance(session.get("start_liveness"), dict) else {}
    status["openwb_pro_start_liveness_contract"] = liveness
    status["openwb_pro_start_liveness_state"] = str(liveness.get("state") or "inactive")
    status["openwb_pro_start_liveness_code"] = str(liveness.get("code") or "")
    status["openwb_pro_start_liveness_alert"] = bool(liveness.get("alert", False))
    status["openwb_pro_start_liveness_age_s"] = float(liveness.get("age_s", 0.0) or 0.0)
    return status
