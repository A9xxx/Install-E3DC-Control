"""Typisierte, zunächst verhaltensneutrale Wallbox-Aktorverträge.

Die Verträge trennen Fähigkeiten, finalen Aktorauftrag und beobachtete
Ausführung. Sie entscheiden weder Budget noch Ladephilosophie und führen
keinen Treiberbefehl aus. Solange noch Legacy-Aufrufer existieren, dienen sie
als kanonische Projektion an der bestehenden Controller- und Ausgangskante.
"""

from dataclasses import dataclass, field
from typing import Any, Dict, Mapping, Optional, Tuple


_PHASE_CONTROL_MODES = frozenset({
    "unknown",
    "fixed_or_unknown",
    "current_only",
    "direct",
    "autonomous_vendor",
})

_NO_OUTPUT_KINDS = frozenset({
    "",
    "noop",
    "observe_only",
    "wait",
    "hold_state",
})

_CURRENT_METHODS = frozenset({
    "set_current",
    "hold_current",
    "set_direct_current",
    "set_amp_and_state",
    "set_amp_sonnenmodus",
    "set_amp_autonomous_solar",
})


def _safe_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if result != result or result in (float("inf"), float("-inf")):
        return None
    return result


def _positive_float(value: Any) -> Optional[float]:
    result = _safe_float(value)
    return result if result is not None and result > 0.0 else None


def _optional_bool(value: Any) -> Optional[bool]:
    """Normalisiere nur ausdrücklich belegte boolesche Werte.

    Fehlende Angaben bleiben ``None``. Dadurch wird aus einem nicht
    vorhandenen Fähigkeitsbeleg weder versehentlich eine Freigabe noch ein
    Veto.
    """

    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)) and value in (0, 1):
        return bool(value)
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on", "ja"}:
        return True
    if text in {"0", "false", "no", "off", "nein"}:
        return False
    return None


def _valid_phase(value: Any) -> int:
    try:
        phase = int(value or 0)
    except (TypeError, ValueError):
        return 0
    return phase if phase in (1, 2, 3) else 0


def _phase_tuple(value: Any) -> Tuple[int, ...]:
    raw = value if isinstance(value, (list, tuple, set, frozenset)) else ()
    return tuple(sorted({phase for phase in (_valid_phase(item) for item in raw) if phase}))


@dataclass(frozen=True)
class WallboxCapability:
    """Belegter Gerätevertrag ohne Regelentscheidung.

    Unbekannte Werte bleiben ``None`` beziehungsweise leer. Insbesondere wird
    ein Fahrzeug- oder Wallboxtyp nicht pauschal auf 6 A, 16 A oder drei
    Phasen geraten.
    """

    schema_version: str = "wallbox_capability_v1"
    driver_class: str = ""
    phase_control: str = "unknown"
    supported_phases: Tuple[int, ...] = field(default_factory=tuple)
    possible_phases: Tuple[int, ...] = field(default_factory=tuple)
    min_current_a: Optional[float] = None
    max_current_a: Optional[float] = None
    infrastructure_max_current_a: Optional[float] = None
    vehicle_max_current_a: Optional[float] = None
    effective_max_current_a: Optional[float] = None
    current_step_a: Optional[float] = None
    actual_phases: int = 0
    target_phases: int = 0
    phase_switch_capable: Optional[bool] = None
    phase_switch_veto: Optional[bool] = None
    phase_switch_veto_reason: str = ""
    phase_imbalance_limit_a: Optional[float] = None
    phase_imbalance_limit_confirmed: bool = False
    phase_imbalance_limit_source: str = ""
    can_read_current: bool = False
    can_read_power: bool = False
    can_read_phases: bool = False
    heartbeat_contract: str = "unknown"
    wakeup_contract: str = "unknown"
    source: str = "unknown"
    evidence_quality: str = "unknown"

    def __post_init__(self):
        phase_control = str(self.phase_control or "unknown").strip().lower()
        if phase_control not in _PHASE_CONTROL_MODES:
            phase_control = "unknown"
        object.__setattr__(self, "phase_control", phase_control)
        object.__setattr__(self, "driver_class", str(self.driver_class or ""))
        supported_phases = _phase_tuple(self.supported_phases)
        possible_phases = _phase_tuple(self.possible_phases) or supported_phases
        if not supported_phases:
            supported_phases = possible_phases
        object.__setattr__(self, "supported_phases", supported_phases)
        object.__setattr__(self, "possible_phases", possible_phases)
        object.__setattr__(self, "min_current_a", _positive_float(self.min_current_a))
        legacy_max_current = _positive_float(self.max_current_a)
        infrastructure_max_current = (
            _positive_float(self.infrastructure_max_current_a)
            or legacy_max_current
        )
        vehicle_max_current = _positive_float(self.vehicle_max_current_a)
        confirmed_limits = tuple(
            value
            for value in (infrastructure_max_current, vehicle_max_current)
            if value is not None
        )
        effective_max_current = _positive_float(self.effective_max_current_a)
        if confirmed_limits:
            confirmed_effective = min(confirmed_limits)
            effective_max_current = (
                min(effective_max_current, confirmed_effective)
                if effective_max_current is not None
                else confirmed_effective
            )
        object.__setattr__(self, "max_current_a", effective_max_current)
        object.__setattr__(
            self,
            "infrastructure_max_current_a",
            infrastructure_max_current,
        )
        object.__setattr__(self, "vehicle_max_current_a", vehicle_max_current)
        object.__setattr__(self, "effective_max_current_a", effective_max_current)
        object.__setattr__(self, "current_step_a", _positive_float(self.current_step_a))
        object.__setattr__(self, "actual_phases", _valid_phase(self.actual_phases))
        object.__setattr__(self, "target_phases", _valid_phase(self.target_phases))
        phase_switch_capable = _optional_bool(self.phase_switch_capable)
        if phase_switch_capable is None:
            if phase_control in {"direct", "autonomous_vendor"}:
                phase_switch_capable = bool(1 in possible_phases and 3 in possible_phases)
            elif phase_control in {"current_only", "fixed_or_unknown"}:
                phase_switch_capable = False
        object.__setattr__(self, "phase_switch_capable", phase_switch_capable)
        object.__setattr__(
            self,
            "phase_switch_veto",
            _optional_bool(self.phase_switch_veto),
        )
        object.__setattr__(
            self,
            "phase_switch_veto_reason",
            str(self.phase_switch_veto_reason or ""),
        )
        imbalance_source = str(self.phase_imbalance_limit_source or "").strip()
        imbalance_confirmed = bool(
            self.phase_imbalance_limit_confirmed and imbalance_source
        )
        imbalance_limit = (
            _positive_float(self.phase_imbalance_limit_a)
            if imbalance_confirmed
            else None
        )
        object.__setattr__(self, "phase_imbalance_limit_a", imbalance_limit)
        object.__setattr__(
            self,
            "phase_imbalance_limit_confirmed",
            bool(imbalance_confirmed and imbalance_limit is not None),
        )
        object.__setattr__(
            self,
            "phase_imbalance_limit_source",
            (
                imbalance_source
                if imbalance_confirmed and imbalance_limit is not None
                else ""
            ),
        )
        object.__setattr__(self, "heartbeat_contract", str(self.heartbeat_contract or "unknown"))
        object.__setattr__(self, "wakeup_contract", str(self.wakeup_contract or "unknown"))
        object.__setattr__(self, "source", str(self.source or "unknown"))
        object.__setattr__(self, "evidence_quality", str(self.evidence_quality or "unknown"))

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]] = None):
        data = value if isinstance(value, Mapping) else {}
        return cls(
            driver_class=data.get("driver_class", ""),
            phase_control=data.get("phase_control", "unknown"),
            supported_phases=data.get("supported_phases", ()),
            possible_phases=data.get("possible_phases", ()),
            min_current_a=data.get("min_current_a"),
            max_current_a=data.get("max_current_a"),
            infrastructure_max_current_a=data.get("infrastructure_max_current_a"),
            vehicle_max_current_a=data.get("vehicle_max_current_a"),
            effective_max_current_a=data.get("effective_max_current_a"),
            current_step_a=data.get("current_step_a"),
            actual_phases=data.get("actual_phases", 0),
            target_phases=data.get("target_phases", 0),
            phase_switch_capable=data.get("phase_switch_capable"),
            phase_switch_veto=data.get("phase_switch_veto"),
            phase_switch_veto_reason=data.get("phase_switch_veto_reason", ""),
            phase_imbalance_limit_a=data.get("phase_imbalance_limit_a"),
            phase_imbalance_limit_confirmed=(
                _optional_bool(data.get("phase_imbalance_limit_confirmed")) is True
            ),
            phase_imbalance_limit_source=data.get(
                "phase_imbalance_limit_source",
                "",
            ),
            can_read_current=bool(data.get("can_read_current", False)),
            can_read_power=bool(data.get("can_read_power", False)),
            can_read_phases=bool(data.get("can_read_phases", False)),
            heartbeat_contract=data.get("heartbeat_contract", "unknown"),
            wakeup_contract=data.get("wakeup_contract", "unknown"),
            source=data.get("source", "unknown"),
            evidence_quality=data.get("evidence_quality", "unknown"),
        )

    @classmethod
    def from_runtime(
        cls,
        *,
        driver: Optional[Mapping[str, Any]] = None,
        inputs: Optional[Mapping[str, Any]] = None,
        phase_capability: Optional[Mapping[str, Any]] = None,
        phase_contract: Optional[Mapping[str, Any]] = None,
        status: Optional[Mapping[str, Any]] = None,
        vehicle_current_capability: Optional[Mapping[str, Any]] = None,
        vehicle_phase_capability: Optional[Mapping[str, Any]] = None,
        phase_switch_veto: Optional[Mapping[str, Any]] = None,
        infrastructure: Optional[Mapping[str, Any]] = None,
    ):
        """Projiziert ausschließlich bereits belegte Laufzeitverträge.

        Der openWB-Pro-Vertrag stammt aus der offiziellen ``connect.php``-
        Schnittstelle. Eine efy wird nur dann als intern automatisch geführt,
        wenn der bestehende Phasenvertrag diese Fähigkeit frisch bestätigt.
        """

        driver_data = driver if isinstance(driver, Mapping) else {}
        input_data = inputs if isinstance(inputs, Mapping) else {}
        phase_cap = phase_capability if isinstance(phase_capability, Mapping) else {}
        phase_data = phase_contract if isinstance(phase_contract, Mapping) else {}
        status_data = status if isinstance(status, Mapping) else {}
        vehicle_current = (
            vehicle_current_capability
            if isinstance(vehicle_current_capability, Mapping)
            else {}
        )
        vehicle_phase = (
            vehicle_phase_capability
            if isinstance(vehicle_phase_capability, Mapping)
            else {}
        )
        phase_veto = (
            phase_switch_veto
            if isinstance(phase_switch_veto, Mapping)
            else {}
        )
        infrastructure_data = (
            infrastructure if isinstance(infrastructure, Mapping) else {}
        )
        driver_class = str(driver_data.get("class") or "")

        direct = bool(phase_cap.get("can_switch") is True)
        autonomous = bool(phase_cap.get("autonomous_can_switch") is True)
        phase_control = "unknown"
        supported_phases: Tuple[int, ...] = ()
        source = str(phase_cap.get("source") or "unknown")
        evidence_quality = "runtime_contract" if phase_cap else "unknown"
        min_current_a = None
        current_step_a = None
        heartbeat_contract = "unknown"
        wakeup_contract = "unknown"
        can_read_current = False
        can_read_power = False
        can_read_phases = False
        phase_switch_capable: Optional[bool] = None
        phase_switch_veto_value: Optional[bool] = None
        phase_switch_veto_reason = ""

        official_openwb_pro_status = bool(
            driver_class == "OpenWBProCharger"
            and status_data.get("connect_php_payload_valid") is True
            and str(status_data.get("api_surface") or "")
            in ("openwb_pro_connect_php", "official_connect_php")
        )
        official_openwb_pro_phase = bool(
            official_openwb_pro_status
            and direct
            and str(phase_cap.get("capability") or "")
            == "official_connect_php"
            and str(phase_cap.get("source") or "")
            == "openwb_pro_connect_php"
            and str(phase_cap.get("api_surface") or "")
            in ("openwb_pro_connect_php", "official_connect_php")
        )
        if official_openwb_pro_status:
            # Die offizielle Basisfläche belegt Strom, Leistung und
            # Heartbeat unabhängig von optionalen Phasen- oder CP-Feldern.
            phase_control = "direct" if official_openwb_pro_phase else "current_only"
            supported_phases = (1, 3) if official_openwb_pro_phase else ()
            min_current_a = 6.0
            current_step_a = 0.1
            heartbeat_contract = "periodic_api_read_20_30s_when_enabled"
            wakeup_contract = (
                "official_cp_wire_plus_vehicle_profile"
                if status_data.get("cp_interrupt_supported") is True
                else "cp_capability_unconfirmed"
            )
            can_read_current = True
            can_read_power = True
            can_read_phases = bool(official_openwb_pro_phase)
            source = "openwb_pro_connect_php"
            evidence_quality = "manufacturer_documented"
        elif driver_class == "OpenWBProCharger":
            # Die Treiberklasse allein belegt nicht, dass connect.php aktuell
            # erreichbar ist. Legacy-/Fallback-Status bleibt vollständig
            # unbekannt und kann keinen Phasenbefehl autorisieren.
            phase_control = "unknown"
            supported_phases = ()
            source = str(phase_cap.get("source") or "fail_closed")
            evidence_quality = "unknown"
        elif autonomous:
            # Belegt ist die interne efy-Phasenautomatik. Dass der vorhandene
            # WBchar6-Pfad sie aktiviert, bleibt ein feldverifizierter Vertrag.
            phase_control = "autonomous_vendor"
            supported_phases = (1, 3)
            source = str(phase_cap.get("autonomous_source") or source)
            evidence_quality = "field_verified_legacy"
        elif direct:
            phase_control = "direct"
            supported_phases = (1, 3)
        elif driver_class == "OpenWBCharger":
            phase_control = "current_only"
            source = source if source != "unknown" else "secondary_current_only"
        elif phase_data:
            phase_control = "fixed_or_unknown"

        if phase_cap:
            phase_switch_capable = bool(direct or autonomous)
        if phase_veto and "active" in phase_veto:
            phase_switch_veto_value = _optional_bool(phase_veto.get("active"))
            phase_switch_veto_reason = str(phase_veto.get("reason") or "")
        elif "phase_switch_allowed" in vehicle_phase:
            phase_switch_allowed = _optional_bool(
                vehicle_phase.get("phase_switch_allowed")
            )
            if phase_switch_allowed is not None:
                phase_switch_veto_value = not phase_switch_allowed
                phase_switch_veto_reason = str(
                    vehicle_phase.get("phase_switch_policy_source") or ""
                )

        infrastructure_max_current_a = _positive_float(
            infrastructure_data.get(
                "max_current_a",
                input_data.get("infrastructure_max_current_a", input_data.get("max_amp")),
            )
        )
        vehicle_max_current_a = None
        if vehicle_current.get("active") is True:
            vehicle_max_current_a = _positive_float(vehicle_current.get("cap_amp"))
        elif status_data.get("vehicle_current_cap_active") is True:
            vehicle_max_current_a = _positive_float(
                status_data.get("vehicle_current_cap_amp")
            )
        effective_candidates = tuple(
            value
            for value in (infrastructure_max_current_a, vehicle_max_current_a)
            if value is not None
        )
        effective_max_current_a = min(effective_candidates) if effective_candidates else None

        vehicle_max_phases = _valid_phase(vehicle_phase.get("phase_count", 0))
        if vehicle_phase.get("active") is True and vehicle_max_phases == 1:
            supported_phases = tuple(
                phase for phase in supported_phases if phase == 1
            ) or (1,)

        imbalance_limit = None
        imbalance_source = ""
        for evidence in (infrastructure_data, input_data, status_data):
            if evidence.get("phase_imbalance_limit_confirmed") is not True:
                continue
            candidate = _positive_float(evidence.get("phase_imbalance_limit_a"))
            if candidate is None:
                continue
            imbalance_limit = candidate
            imbalance_source = str(
                evidence.get("phase_imbalance_limit_source") or "confirmed_runtime"
            )
            break

        actual_phases = _valid_phase(
            phase_data.get("actual_phases", status_data.get("phases_in_use", 0))
        )
        target_phases = _valid_phase(
            phase_data.get("target_phases", status_data.get("phases_target", 0))
        )
        if actual_phases:
            can_read_phases = True
        if any(key in status_data for key in ("offered_current_raw", "evse_current", "amp")):
            can_read_current = True
        if any(
            key in status_data
            for key in (
                "power_all",
                "power",
                "power_w",
                "actual_power_w",
                "real_power_w",
                "phase_power_sum_w",
            )
        ):
            can_read_power = True

        return cls(
            driver_class=driver_class,
            phase_control=phase_control,
            supported_phases=supported_phases,
            possible_phases=supported_phases,
            min_current_a=min_current_a,
            max_current_a=effective_max_current_a,
            infrastructure_max_current_a=infrastructure_max_current_a,
            vehicle_max_current_a=vehicle_max_current_a,
            effective_max_current_a=effective_max_current_a,
            current_step_a=(
                _positive_float(input_data.get("current_step_a"))
                or _positive_float(driver_data.get("current_step_a"))
                or current_step_a
            ),
            actual_phases=actual_phases,
            target_phases=target_phases,
            phase_switch_capable=phase_switch_capable,
            phase_switch_veto=phase_switch_veto_value,
            phase_switch_veto_reason=phase_switch_veto_reason,
            phase_imbalance_limit_a=imbalance_limit,
            phase_imbalance_limit_confirmed=imbalance_limit is not None,
            phase_imbalance_limit_source=imbalance_source,
            can_read_current=can_read_current,
            can_read_power=can_read_power,
            can_read_phases=can_read_phases,
            heartbeat_contract=heartbeat_contract,
            wakeup_contract=wakeup_contract,
            source=source,
            evidence_quality=evidence_quality,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "driver_class": self.driver_class,
            "phase_control": self.phase_control,
            "supported_phases": list(self.supported_phases),
            "possible_phases": list(self.possible_phases),
            "min_current_a": self.min_current_a,
            "max_current_a": self.max_current_a,
            "infrastructure_max_current_a": self.infrastructure_max_current_a,
            "vehicle_max_current_a": self.vehicle_max_current_a,
            "effective_max_current_a": self.effective_max_current_a,
            "current_step_a": self.current_step_a,
            "actual_phases": self.actual_phases,
            "target_phases": self.target_phases,
            "phase_switch_capable": self.phase_switch_capable,
            "phase_switch_veto": self.phase_switch_veto,
            "phase_switch_veto_reason": self.phase_switch_veto_reason,
            "phase_imbalance_limit_a": self.phase_imbalance_limit_a,
            "phase_imbalance_limit_confirmed": self.phase_imbalance_limit_confirmed,
            "phase_imbalance_limit_source": self.phase_imbalance_limit_source,
            "can_read_current": self.can_read_current,
            "can_read_power": self.can_read_power,
            "can_read_phases": self.can_read_phases,
            "heartbeat_contract": self.heartbeat_contract,
            "wakeup_contract": self.wakeup_contract,
            "source": self.source,
            "evidence_quality": self.evidence_quality,
        }


@dataclass(frozen=True)
class ActuatorIntent:
    """Ein typisierter Aktorauftrag an einer benannten Verarbeitungsstufe.

    ``planned`` ist nur die Controller-Absicht. Erst ``admitted`` beschreibt
    den nach allen Gates belegten Ausgang; auch das ist noch kein physischer
    Ladebeleg.
    """

    schema_version: str = "wallbox_actuator_intent_v1"
    stage: str = "planned"
    wb_id: int = 0
    cycle_token: str = ""
    action: str = "no_output"
    method: str = ""
    decision_allowed_w: Optional[float] = None
    authorized_output_cap_w: Optional[float] = None
    target_current_a: Optional[float] = None
    target_phases: int = 0
    phase_control: str = "unknown"
    reason: str = ""
    source: str = "charger_controller"
    emergency: bool = False
    output_semantics: str = "unknown"
    wire_output_expected: Optional[bool] = None

    @classmethod
    def from_command(
        cls,
        command: Optional[Mapping[str, Any]],
        *,
        wb_id: Any = 0,
        cycle_token: Any = "",
        decision_allowed_w: Any = None,
        authorized_output_cap_w: Any = None,
        capability: Optional[WallboxCapability] = None,
        source: str = "charger_controller",
        stage: str = "planned",
    ):
        cmd = command if isinstance(command, Mapping) else {}
        kind = str(cmd.get("kind") or "").strip()
        method = str(cmd.get("method") or kind).strip()
        semantic = kind or method
        reason = str(cmd.get("reason") or "").strip()
        target_phases = _valid_phase(cmd.get("phases", cmd.get("target_phases", 0)))
        current = _safe_float(cmd.get("amp", cmd.get("max_amp")))
        canonical_guarded_stop = bool(
            kind == "stop"
            and cmd.get("_canonical_stop_command") is True
            and cmd.get("_control_guard_checked") is True
        )
        emergency = bool(
            (
                method == "emergency_stop"
                or canonical_guarded_stop
            )
            and (
                cmd.get("_emergency_override") is True
                or reason in ("emergency_stop", "not_aus", "webui_not_aus")
            )
        )

        if semantic in _NO_OUTPUT_KINDS:
            action = "no_output"
            wire_output_expected = False
            output_semantics = "no_output"
        elif semantic == "set_phases" or method == "set_phases":
            action = "phase_transition"
            wire_output_expected = True
            output_semantics = "hardware_command"
        elif semantic in _CURRENT_METHODS or method in _CURRENT_METHODS:
            action = (
                "stop"
                if cmd.get("force_state") == 1
                or (current is not None and current < 0.5)
                else "charge"
            )
            wire_output_expected = True
            output_semantics = "hardware_command"
        elif semantic in ("stop", "emergency_stop") or method in ("stop", "emergency_stop"):
            action = "stop"
            wire_output_expected = True
            output_semantics = "hardware_command"
        elif semantic in ("trigger_cp_interrupt", "cp_interrupt") or method in (
            "trigger_cp_interrupt",
            "cp_interrupt",
        ):
            action = "wakeup"
            wire_output_expected = True
            output_semantics = "hardware_command"
        elif semantic in ("release_to_e3dc", "release_to_default") or method in (
            "release_to_e3dc",
            "release_to_default",
        ):
            action = "release"
            if (
                capability is not None
                and capability.driver_class == "OpenWBProCharger"
                and method == "release_to_default"
            ):
                wire_output_expected = True
                output_semantics = "hardware_command"
            else:
                wire_output_expected = None
                output_semantics = "driver_dependent"
        elif semantic == "set_heartbeat" or method == "set_heartbeat":
            action = "control_acquire" if cmd.get("enabled") is True else "release"
            wire_output_expected = True
            output_semantics = "hardware_command"
        elif semantic == "set_pv_mode" or method == "set_pv_mode":
            action = "mode_transition"
            wire_output_expected = None
            output_semantics = "driver_dependent"
        elif semantic == "take_control" or method == "take_control":
            action = "preamble"
            wire_output_expected = None
            output_semantics = "driver_dependent"
        else:
            action = "unknown"
            wire_output_expected = None
            output_semantics = "unknown"

        phase_control = capability.phase_control if capability is not None else "unknown"
        try:
            normalized_wb_id = int(wb_id or 0)
        except (TypeError, ValueError):
            normalized_wb_id = 0
        return cls(
            stage=str(stage or "planned"),
            wb_id=normalized_wb_id,
            cycle_token=str(cycle_token or ""),
            action=action,
            method=method,
            decision_allowed_w=_safe_float(decision_allowed_w),
            authorized_output_cap_w=_safe_float(authorized_output_cap_w),
            target_current_a=current,
            target_phases=target_phases,
            phase_control=phase_control,
            reason=str(cmd.get("reason") or semantic or "no_output"),
            source=str(source or "charger_controller"),
            emergency=emergency,
            output_semantics=output_semantics,
            wire_output_expected=wire_output_expected,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "stage": self.stage,
            "wb_id": self.wb_id,
            "cycle_token": self.cycle_token,
            "action": self.action,
            "method": self.method,
            "decision_allowed_w": self.decision_allowed_w,
            "authorized_output_cap_w": self.authorized_output_cap_w,
            "target_current_a": self.target_current_a,
            "target_phases": self.target_phases,
            "phase_control": self.phase_control,
            "reason": self.reason,
            "source": self.source,
            "emergency": self.emergency,
            "output_semantics": self.output_semantics,
            "wire_output_expected": self.wire_output_expected,
        }


__all__ = ["ActuatorIntent", "WallboxCapability"]
