"""Zustandsbehaftete Fassade für openWB-Pro-Phasenwechsel.

Die fachliche Sequenz bleibt vollständig in den reinen Verträgen aus
``openwb_pro_session``. Diese Fassade besitzt ausschließlich den bereits im
Wallbox-Manager verwendeten Sequenzzustand und trennt Vorschlag und bestätigte
Treiber-Ausführung. Sie führt selbst weder Datei- noch Netzwerkzugriffe aus.
"""

from copy import deepcopy
from typing import Any, Dict, Optional

from . import openwb_pro_session
from . import phase_transition


_SEQUENCE_STATE_KEYS = (
    "_openwb_pro_phase_sequence",
    "_openwb_pro_phase_sequence_stage",
    "_openwb_pro_phase_sequence_target",
    "_openwb_pro_phase_sequence_current_allowed_after",
    "_openwb_pro_phase_sequence_phase_sent_ts",
    "_openwb_pro_phase_sequence_cp_sent_ts",
    "_openwb_pro_phase_sequence_last",
    "_openwb_pro_phase_sequence_contract",
    "_openwb_pro_phase_zero_sent_ts",
    "_openwb_pro_phase_wait_target",
    "_openwb_pro_phase_wait_until",
    "_openwb_pro_phase_wait_min_until",
    "_openwb_pro_phase_wait_amp",
    "_openwb_pro_phase_wait_since",
    "_openwb_pro_start_wakeup_cp_ts",
    "_openwb_pro_start_wakeup_allowed_after",
    "_openwb_cp_start_sent",
    "_openwb_last_cp_start_ts",
    "_last_phase_switch_ts",
    "_openwb_pro_phase_change_guard",
    "_openwb_pro_phase_cooldown_remaining_s",
    "_wallbox_phase_transition_reservation",
    "_openwb_pro_phase_output_intent",
    "_openwb_pro_phase_output_ack",
    "_openwb_pro_phase_recovery_hold",
)

_NOMINAL_PHASE_VOLTAGE_V = 230.0
_MIN_CHARGE_CURRENT_A = 6


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip() if value is not None else ""
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, default)))
    except (TypeError, ValueError):
        return int(default)


def _status_power_w(status: Optional[Dict[str, Any]]) -> float:
    st = status if isinstance(status, dict) else {}
    phase_sum_w = sum(
        abs(_safe_float(st.get(key), 0.0))
        for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w")
    )
    return max(
        0.0,
        abs(_safe_float(st.get("real_power_w"), 0.0)),
        abs(_safe_float(st.get("phase_power_sum_w"), 0.0)),
        abs(_safe_float(st.get("power_w"), 0.0)),
        phase_sum_w,
    )


def _status_phase_count(status: Optional[Dict[str, Any]]) -> int:
    st = status if isinstance(status, dict) else {}
    phase_powers = [
        abs(_safe_float(st.get(key), 0.0))
        for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w")
    ]
    measured_phases = sum(1 for value in phase_powers if value > 100.0)
    if measured_phases in (1, 2, 3):
        return measured_phases
    for key in ("phase_actual_phases", "phases_actual", "phases_in_use"):
        phases = _safe_int(st.get(key), 0)
        if phases in (1, 2, 3):
            return phases
    return 0


def begin_phase_transition_reservation(
    state: Optional[Dict[str, Any]],
    target_phases: Any,
    *,
    status: Optional[Dict[str, Any]] = None,
    now_ts: Any = 0,
    started_ts: Any = None,
    zero_settle_s: Any = 0,
    restart_delay_s: Any = 0,
    charger_max_amp: Any = 32,
    source: str = "manager_phase_command",
    reason: str = "phase_transition",
    wb_id: Any = None,
    from_phases: Any = None,
    restart_amp: Any = None,
    current_step_amp: Any = None,
    effective_w_per_amp: Any = None,
    lease_s: Any = None,
    transition_id: Any = None,
    clock_sample: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Erstellt den allgemeinen Auftrag, bevor ein Gerätebefehl gesendet wird."""

    data = state if isinstance(state, dict) else {}
    st = status if isinstance(status, dict) else {}
    target = _safe_int(target_phases, 0)
    if target not in (1, 3):
        return {}
    now_value = _safe_float(now_ts, 0.0) if started_ts is None else _safe_float(started_ts, now_ts)
    observed_before_w = _status_power_w(st)
    real_charge_confirmed = bool(
        observed_before_w > 500.0
        or st.get("charging") is True
        or st.get("charge_state") is True
    )
    # Eine nur softwareseitig angebotene Stromstärke ist bei einem noch
    # stromlosen Fahrzeug keine reale Wiederanlauflast. Würden wir sie für
    # die 1p->3p-Reservierung übernehmen, könnte ein früheres 11-A-Angebot
    # 7,7 kW verlangen, obwohl für den sicheren 3p-Start nur 6 A nötig sind.
    # Der Storage Manager verweigert dann den überhöhten Grant und derselbe
    # Phasenvertrag unterdrückt gleichzeitig den möglichen 1p-/3p-Start.
    # Erst bestätigte Fahrzeugleistung darf deshalb oberhalb des normativen
    # Mindeststroms reserviert werden; die weitere Rampe folgt dem Budget.
    current = (
        max(
            _safe_float(restart_amp, 0.0),
            _safe_float(data.get("current_set_amp"), 0.0),
            _safe_float(st.get("offered_current_raw"), 0.0),
            _safe_float(st.get("evse_current"), 0.0),
            _safe_float(st.get("amp"), 0.0),
            float(_MIN_CHARGE_CURRENT_A),
        )
        if real_charge_confirmed
        else float(_MIN_CHARGE_CURRENT_A)
    )
    actual_phases = _safe_int(from_phases, 0) or _status_phase_count(st)
    # ``restart_amp`` ist wie der spätere openWB-Befehl ein Strom je Phase.
    # Eine Umrechnung mit dem Verhältnis alter/neuer Phasen würde deshalb bei
    # 1p->3p weniger Leistung reservieren, als der bestätigte Wiederanlauf
    # tatsächlich ausgibt. Die Phasenzahl gehört ausschließlich in W/A.
    current = min(max(float(_MIN_CHARGE_CURRENT_A), current), max(float(_MIN_CHARGE_CURRENT_A), _safe_float(charger_max_amp, 32.0)))
    step = (
        _safe_float(current_step_amp, 0.0)
        or _safe_float(st.get("current_step_amp"), 0.0)
        or _safe_float(getattr(data.get("charger"), "current_step_amp", 0.0), 0.0)
        or 1.0
    )
    w_per_amp = _safe_float(effective_w_per_amp, 0.0) or (_NOMINAL_PHASE_VOLTAGE_V * target)
    duration_s = max(
        phase_transition.DEFAULT_LEASE_S,
        max(0.0, _safe_float(zero_settle_s, 0.0))
        + max(0.0, _safe_float(restart_delay_s, 0.0))
        + 120.0,
        max(0.0, _safe_float(lease_s, 0.0)),
    )
    return phase_transition.begin_reservation(
        data,
        wb_id=_safe_int(wb_id, _safe_int(data.get("id"), 0)),
        from_phases=actual_phases,
        target_phases=target,
        restart_amp=current,
        current_step_amp=step,
        effective_w_per_amp=w_per_amp,
        observed_before_w=observed_before_w,
        now_ts=now_value,
        lease_s=duration_s,
        owner="wallbox_manager",
        source=source,
        reason_code=reason,
        max_power_w=max(float(_MIN_CHARGE_CURRENT_A), _safe_float(charger_max_amp, 32.0)) * w_per_amp,
        transition_id=transition_id,
        clock_sample=clock_sample,
    )


def phase_transition_reservation(
    state: Optional[Dict[str, Any]],
    *,
    status: Optional[Dict[str, Any]] = None,
    now_ts: Any = 0,
    connected: Optional[bool] = None,
    clock_sample: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Aktualisiert und veröffentlicht die allgemeine Reservierung je Wallbox."""

    return phase_transition.update_reservation(
        state,
        status=status,
        now_ts=now_ts,
        connected=connected,
        clock_sample=clock_sample,
    )


class PhaseSwitchSequencer:
    """Führe den bestehenden openWB-Pro-Sequenzvertrag zustandsbehaftet aus.

    ``state`` darf das bestehende ``c_data`` des Managers oder ein zuvor mit
    :meth:`snapshot` erzeugter Zustand sein. Die Fassade hält die übergebene
    Dictionary-Instanz bewusst als Referenz, damit eine spätere Integration
    keine zweite, abweichende Zustandsquelle erzeugt.
    """

    def __init__(self, state: Optional[Dict[str, Any]] = None) -> None:
        self._state = state if isinstance(state, dict) else {}
        self._pending: Optional[Dict[str, Any]] = None
        self._pending_config: Dict[str, Any] = {}
        self._pending_charger_max_amp: Any = 32
        self._pending_status: Dict[str, Any] = {}
        self._pending_restart_delay_s: float = 0.0
        self._pending_clock_sample: Optional[Dict[str, Any]] = None

    def propose(
        self,
        target_phases: Any,
        *,
        now_ts: Any = 0,
        current_set_amp: Any = 0,
        status: Optional[Dict[str, Any]] = None,
        config: Optional[Dict[str, Any]] = None,
        reason: str = "phase_switch",
        cp_payload: Optional[Dict[str, Any]] = None,
        hold_s: Any = None,
        restart_delay_s: Any = None,
        charger_max_amp: Any = 32,
        clock_sample: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Schlage den nächsten Schritt ohne Treiber-I/O vor.

        Der Rückgabewert stammt unverändert aus
        :func:`openwb_pro_session.phase_sequence_step_contract`. Insbesondere
        bleiben Methoden, Parameter und Reason-Codes der Command-Dictionaries
        identisch zum bisherigen Manager-Pfad.
        """

        cfg = config if isinstance(config, dict) else {}
        st = status if isinstance(status, dict) else {}
        sequence = self._state.get("_openwb_pro_phase_sequence")
        effective_hold_s = (
            openwb_pro_session.phase_wait_s(cfg)
            if hold_s is None
            else hold_s
        )
        effective_restart_delay_s = (
            openwb_pro_session.phase_restart_delay_s(cfg)
            if restart_delay_s is None
            else restart_delay_s
        )
        effective_cp_payload = (
            openwb_pro_session.phase_cp_interrupt_payload(cfg, st)
            if cp_payload is None
            else cp_payload
        )
        contract = openwb_pro_session.phase_sequence_step_contract(
            target_phases,
            sequence if isinstance(sequence, dict) else None,
            now_ts=now_ts,
            hold_s=effective_hold_s,
            restart_delay_s=effective_restart_delay_s,
            current_set_amp=current_set_amp,
            status=st,
            config=cfg,
            reason=reason,
            cp_payload=effective_cp_payload,
            clock_sample=clock_sample,
        )
        self._state["_openwb_pro_phase_sequence_contract"] = deepcopy(contract)
        self._pending = deepcopy(contract)
        self._pending_config = deepcopy(cfg)
        self._pending_charger_max_amp = charger_max_amp
        self._pending_status = deepcopy(st)
        self._pending_restart_delay_s = max(
            0.0,
            _safe_float(effective_restart_delay_s, 0.0),
        )
        self._pending_clock_sample = (
            deepcopy(clock_sample) if isinstance(clock_sample, dict) else None
        )
        return contract

    def acknowledge(
        self,
        proposal: Optional[Dict[str, Any]] = None,
        success: bool = True,
        *,
        config: Optional[Dict[str, Any]] = None,
        charger_max_amp: Any = None,
    ) -> Dict[str, Any]:
        """Übernehme einen vorgeschlagenen Schritt nach dessen Ausführung.

        ``success`` bezeichnet ausschließlich den Erfolg eines vorhandenen
        Treiberkommandos. ``send_zero`` und ``send_phase`` schreiten nur nach
        erfolgreicher Treiberbestätigung fort. Damit kann
        insbesondere ein abgelehntes 0-A-Kommando niemals nach Ablauf des
        Settle-Timers unbemerkt in ``phasetarget`` weiterlaufen.
        """

        contract = proposal if isinstance(proposal, dict) else self._pending
        if not isinstance(contract, dict):
            return self.snapshot()

        cfg = (
            config
            if isinstance(config, dict)
            else self._pending_config
        )
        max_amp = (
            self._pending_charger_max_amp
            if charger_max_amp is None
            else charger_max_amp
        )
        action = str(contract.get("action") or "invalid")
        target = _safe_int(contract.get("target"), 0)

        if action == "send_zero" and success:
            sequence = contract.get("sequence")
            sequence = deepcopy(sequence) if isinstance(sequence, dict) else {}
            self._state["_openwb_pro_phase_sequence"] = sequence
            self._state["_openwb_pro_phase_sequence_stage"] = "zero_wait"
            self._state["_openwb_pro_phase_sequence_target"] = target
            self._state["_openwb_pro_phase_sequence_current_allowed_after"] = 0.0
            started_ts = _safe_float(sequence.get("started_ts"), 0.0)
            self._state["current_set_amp"] = 0
            self._state["_last_openwb_hold_amp"] = 0
            self._state["_wb_stop_sent_active"] = False
            self._state["_openwb_pro_phase_zero_sent_ts"] = started_ts
            self._begin_transition_reservation(
                target,
                sequence,
                status=self._pending_status,
                restart_delay_s=self._pending_restart_delay_s,
                charger_max_amp=max_amp,
            )
            phase_transition.mark_committed(
                self._state,
                stage="ramp_to_zero",
                now_ts=started_ts,
            )
            phase_transition.set_stage(
                self._state,
                "zero_settle",
                now_ts=started_ts,
                deadline_ts=sequence.get("zero_until", 0.0),
            )

        elif action in ("wait_zero", "wait_zero_readback"):
            sequence = self._active_sequence()
            patch = contract.get("sequence_patch")
            if isinstance(patch, dict):
                sequence.update(deepcopy(patch))
                self._state["_openwb_pro_phase_sequence"] = sequence
            self._state["_openwb_pro_phase_sequence_stage"] = "zero_wait"
            phase_transition.set_stage(
                self._state,
                "zero_settle",
                deadline_ts=self._active_sequence().get("zero_until", 0.0),
            )

        elif action == "send_phase" and success:
            sequence = self._active_sequence()
            patch = contract.get("sequence_patch")
            wire_receipt_ts = (
                _safe_float(patch.get("wire_receipt_ts"), 0.0)
                if isinstance(patch, dict)
                else 0.0
            )
            phase_sent_ts = (
                _safe_float(patch.get("phase_sent_ts"), 0.0)
                if isinstance(patch, dict)
                else 0.0
            )
            # Ein erfolgreicher Treiber-Rückgabewert allein ist kein
            # Phasenwechselbeleg. Die Fassade darf den Zustand nur mit dem vom
            # Manager nach dem echten POST gebundenen Wire-Receipt fortsetzen.
            if wire_receipt_ts <= 0.0 or phase_sent_ts != wire_receipt_ts:
                self._pending = None
                self._pending_config = {}
                self._pending_charger_max_amp = 32
                self._pending_status = {}
                self._pending_restart_delay_s = 0.0
                self._pending_clock_sample = None
                return self.snapshot()
            if isinstance(patch, dict):
                sequence.update(deepcopy(patch))
            self._state["_openwb_pro_phase_sequence"] = sequence
            phase_sent_ts = _safe_float(sequence.get("phase_sent_ts"), 0.0)
            phase_wait_config = dict(cfg)
            phase_wait_patch = contract.get("phase_wait_config")
            if isinstance(phase_wait_patch, dict):
                phase_wait_config.update(phase_wait_patch)
            openwb_pro_session.mark_phase_wait(
                self._state,
                target,
                current_amp=sequence.get("hold_amp", 0),
                now_ts=phase_sent_ts,
                config=phase_wait_config,
                charger_max_amp=max_amp,
            )
            current_allowed_after = _safe_float(
                sequence.get("current_allowed_after"),
                phase_sent_ts + openwb_pro_session.phase_wait_s(cfg),
            )
            self._state["_openwb_pro_phase_sequence_stage"] = "restart_delay"
            self._state["_openwb_pro_phase_sequence_phase_sent_ts"] = phase_sent_ts
            self._state["_openwb_pro_phase_sequence_cp_sent_ts"] = 0.0
            self._state[
                "_openwb_pro_phase_sequence_current_allowed_after"
            ] = current_allowed_after
            self._state["_last_phase_switch_ts"] = phase_sent_ts
            self._state["current_set_amp"] = 0
            phase_transition.set_stage(
                self._state,
                "restart_delay",
                now_ts=phase_sent_ts,
                deadline_ts=current_allowed_after,
            )

        elif action == "adopt_phase_settle":
            sequence = self._active_sequence()
            patch = contract.get("sequence_patch")
            if isinstance(patch, dict):
                sequence.update(deepcopy(patch))
            self._state["_openwb_pro_phase_sequence"] = sequence
            phase_sent_ts = _safe_float(sequence.get("phase_sent_ts"), 0.0)
            phase_wait_config = dict(cfg)
            phase_wait_patch = contract.get("phase_wait_config")
            if isinstance(phase_wait_patch, dict):
                phase_wait_config.update(phase_wait_patch)
            openwb_pro_session.mark_phase_wait(
                self._state,
                target,
                current_amp=sequence.get("hold_amp", 0),
                now_ts=phase_sent_ts,
                config=phase_wait_config,
                charger_max_amp=max_amp,
            )
            self._state["_openwb_pro_phase_change_block_until"] = max(
                _safe_float(
                    self._state.get("_openwb_pro_phase_change_block_until"),
                    0.0,
                ),
                _safe_float(sequence.get("phase_change_block_until"), 0.0),
            )
            current_allowed_after = _safe_float(
                sequence.get("current_allowed_after"),
                phase_sent_ts + openwb_pro_session.phase_wait_s(cfg),
            )
            self._state["_openwb_pro_phase_sequence_stage"] = "restart_delay"
            self._state["_openwb_pro_phase_sequence_phase_sent_ts"] = phase_sent_ts
            self._state["_openwb_pro_phase_sequence_cp_sent_ts"] = 0.0
            self._state[
                "_openwb_pro_phase_sequence_current_allowed_after"
            ] = current_allowed_after
            self._state["_last_phase_switch_ts"] = phase_sent_ts
            self._state["current_set_amp"] = 0
            phase_transition.set_stage(
                self._state,
                "restart_delay",
                now_ts=phase_sent_ts,
                deadline_ts=current_allowed_after,
            )

        elif action == "send_cp" and success:
            sequence = self._active_sequence()
            patch = contract.get("sequence_patch")
            if isinstance(patch, dict):
                sequence.update(deepcopy(patch))
            self._state["_openwb_pro_phase_sequence"] = sequence
            cp_sent_ts = _safe_float(sequence.get("cp_sent_ts"), 0.0)
            current_allowed_after = _safe_float(
                sequence.get("current_allowed_after"),
                cp_sent_ts
                + openwb_pro_session.phase_cp_interrupt_duration_s(cfg)
                + openwb_pro_session.phase_restart_delay_s(cfg),
            )
            self._state["_openwb_pro_phase_sequence_stage"] = "restart_delay"
            self._state["_openwb_pro_phase_sequence_cp_sent_ts"] = cp_sent_ts
            self._state[
                "_openwb_pro_phase_sequence_current_allowed_after"
            ] = current_allowed_after
            self._state["_openwb_pro_start_wakeup_cp_ts"] = cp_sent_ts
            self._state[
                "_openwb_pro_start_wakeup_allowed_after"
            ] = current_allowed_after
            self._state["_openwb_cp_start_sent"] = True
            self._state["_openwb_last_cp_start_ts"] = cp_sent_ts
            phase_transition.set_stage(
                self._state,
                "restart_delay",
                now_ts=cp_sent_ts,
                deadline_ts=current_allowed_after,
            )

        elif action == "wait_restart":
            sequence = self._active_sequence()
            patch = contract.get("sequence_patch")
            if isinstance(patch, dict):
                sequence.update(deepcopy(patch))
                self._state["_openwb_pro_phase_sequence"] = sequence
            self._state["_openwb_pro_phase_sequence_stage"] = "restart_delay"
            phase_transition.set_stage(
                self._state,
                "restart_delay",
                deadline_ts=self._active_sequence().get("current_allowed_after", 0.0),
            )

        elif action == "ready" and success:
            last_sequence = contract.get("sequence")
            if not isinstance(last_sequence, dict):
                last_sequence = self._active_sequence()
            self._state["_openwb_pro_phase_sequence_last"] = deepcopy(last_sequence)
            self._state["_openwb_pro_phase_sequence"] = {}
            self._state["_openwb_pro_phase_sequence_stage"] = "ready"
            self._state["_openwb_pro_phase_sequence_target"] = 0
            self._state["_openwb_pro_phase_sequence_current_allowed_after"] = 0.0
            # Die kurze Geräte-Settle-Sperre endet mit dem bestätigten Ziel.
            # Die separat persistierte 480-s-Phasenwechsel-Sperre bleibt
            # unangetastet und verhindert nur die nächste Umschaltung.
            openwb_pro_session.clear_phase_wait(self._state)
            phase_transition.set_stage(self._state, "confirm_target")

        self._pending = None
        self._pending_config = {}
        self._pending_charger_max_amp = 32
        self._pending_status = {}
        self._pending_restart_delay_s = 0.0
        self._pending_clock_sample = None
        return self.snapshot()

    def transition_reservation(
        self,
        *,
        status: Optional[Dict[str, Any]] = None,
        now_ts: Any = 0,
        connected: Optional[bool] = None,
        clock_sample: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        """Liefere die temporäre Leistungsreservierung während des Wechsels.

        Die Reservierung endet erst nach zehn Sekunden bestätigter Ladeleistung
        auf der Zielphasenzahl. Ein harter Timeout verhindert, dass ein nicht
        wieder anlaufendes Fahrzeug andere flexible Verbraucher dauerhaft sperrt.
        """

        return phase_transition_reservation(
            self._state,
            status=status,
            now_ts=now_ts,
            connected=connected,
            clock_sample=clock_sample,
        )

    def reset(self, *, clear_phase_wait: bool = False) -> Dict[str, Any]:
        """Breche die aktive Sequenz ohne Treiberkommando ab.

        Der standardmäßige Reset lässt die hardwarekritische Nachwechsel-Sperre
        bewusst unangetastet. Nur ein ausdrücklich angeforderter
        ``clear_phase_wait`` nutzt den bereits vorhandenen Reset-Vertrag.
        """

        self._state["_openwb_pro_phase_sequence"] = {}
        self._state["_openwb_pro_phase_sequence_stage"] = ""
        self._state["_openwb_pro_phase_sequence_target"] = 0
        self._state["_openwb_pro_phase_sequence_current_allowed_after"] = 0.0
        self._state["_openwb_pro_phase_sequence_phase_sent_ts"] = 0.0
        self._state["_openwb_pro_phase_sequence_cp_sent_ts"] = 0.0
        reservation = self._state.get("_wallbox_phase_transition_reservation")
        if isinstance(reservation, dict) and reservation.get("active"):
            phase_transition.set_stage(
                self._state,
                "aborted" if not reservation.get("committed_w") else "recovery_hold",
                reason_code="sequence_reset",
            )
        if clear_phase_wait:
            openwb_pro_session.clear_phase_wait(self._state)
        self._pending = None
        self._pending_config = {}
        self._pending_charger_max_amp = 32
        self._pending_status = {}
        self._pending_restart_delay_s = 0.0
        self._pending_clock_sample = None
        return self.snapshot()

    def snapshot(self) -> Dict[str, Any]:
        """Liefere eine entkoppelte Kopie der bisherigen Legacy-State-Keys."""

        return {
            key: deepcopy(self._state[key])
            for key in _SEQUENCE_STATE_KEYS
            if key in self._state
        }

    def _active_sequence(self) -> Dict[str, Any]:
        sequence = self._state.get("_openwb_pro_phase_sequence")
        return deepcopy(sequence) if isinstance(sequence, dict) else {}

    def _begin_transition_reservation(
        self,
        target_phases: Any,
        sequence: Dict[str, Any],
        *,
        status: Optional[Dict[str, Any]],
        restart_delay_s: Any,
        charger_max_amp: Any,
    ) -> None:
        existing = self._state.get("_wallbox_phase_transition_reservation")
        if (
            isinstance(existing, dict)
            and existing.get("active")
            and _safe_int(existing.get("target_phases"), 0) == _safe_int(target_phases, 0)
        ):
            return
        begin_phase_transition_reservation(
            self._state,
            target_phases,
            status=status,
            now_ts=sequence.get("started_ts", 0.0),
            started_ts=sequence.get("started_ts", 0.0),
            zero_settle_s=sequence.get("zero_settle_s", 3.0),
            restart_delay_s=restart_delay_s,
            charger_max_amp=charger_max_amp,
            source="openwb_pro_phase_sequence",
            reason=str(sequence.get("reason") or "phase_transition"),
            clock_sample=self._pending_clock_sample,
        )


__all__ = [
    "PhaseSwitchSequencer",
    "begin_phase_transition_reservation",
    "phase_transition_reservation",
]
