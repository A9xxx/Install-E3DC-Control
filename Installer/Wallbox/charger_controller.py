"""Autoritativer, nebenwirkungsfreier Befehlsplaner für einen Wallboxzyklus."""

from dataclasses import dataclass, field
from typing import Any, Dict

from .actuator_contracts import ActuatorIntent, WallboxCapability
from .cycle_context import ChargerCycleContext, CycleContext
from . import decision


@dataclass(frozen=True)
class ControllerResult:
    authoritative: bool
    valid: bool
    command: Dict[str, Any]
    payload: Dict[str, Any]
    reason: str
    error: str = ""
    actuator_intent: Dict[str, Any] = field(default_factory=dict)


class ChargerController:
    """Erstellt ohne Hardwarezugriff den einzigen kanonischen Befehlsplan."""

    DRIVER_METHODS = frozenset({
        "take_control",
        "set_amp_and_state",
        "set_amp_sonnenmodus",
        "set_amp_autonomous_solar",
        "set_direct_current",
        "set_current",
        "set_pv_mode",
        "set_phases",
        "set_heartbeat",
        "stop",
        "emergency_stop",
        "trigger_cp_interrupt",
        "cp_interrupt",
        "release_to_e3dc",
        "release_to_default",
        # Kanonischer, garantiert ausgangsloser Plannervertrag. Er wird im
        # Manager vor allen Hardware-/Budgetgates als NOOP abgeschlossen.
        "observe_only",
    })

    @staticmethod
    def context_from_payload(payload):
        data = payload if isinstance(payload, dict) else {}
        mode = data.get("mode") if isinstance(data.get("mode"), dict) else {}
        driver = data.get("driver") if isinstance(data.get("driver"), dict) else {}
        decisions = data.get("decisions") if isinstance(data.get("decisions"), dict) else {}
        inputs = data.get("inputs") if isinstance(data.get("inputs"), dict) else {}
        capability_data = data.get("capability") if isinstance(data.get("capability"), dict) else {}
        capability = (
            WallboxCapability.from_mapping(capability_data)
            if capability_data
            else WallboxCapability.from_runtime(driver=driver, inputs=inputs)
        )
        return ChargerCycleContext(
            wb_id=int(data.get("wb_id", 0) or 0),
            public_mode=int(mode.get("public", 0) or 0),
            control_mode=int(mode.get("control", 0) or 0),
            allowed_w=float(inputs.get("allowed_w", 0.0) or 0.0),
            cap_amp=float(inputs.get("cap_amp", 0.0) or 0.0),
            detected_phases=int(inputs.get("detected_phases", 1) or 1),
            max_amp=int(inputs.get("max_amp", 0) or 0),
            connected=bool(inputs.get("charger_connected", False)),
            current_amp=float(inputs.get("current_amp", 0.0) or 0.0),
            current_set_amp=float(inputs.get("current_set_amp", 0.0) or 0.0),
            hw_charging=bool(inputs.get("hw_charging", False)),
            hw_power_w=float(inputs.get("hw_power_w", 0.0) or 0.0),
            grid_power_w=float(inputs.get("grid_power_w", 0.0) or 0.0),
            mode_label=str(mode.get("label", "") or ""),
            storage_state=str(inputs.get("storage_state", "") or ""),
            driver_class_name=str(driver.get("class", "") or ""),
            openwb_like=bool(driver.get("openwb_like", False)),
            openwb_pro=bool(driver.get("openwb_pro", False)),
            e3dc_native_toggle=bool(driver.get("e3dc_native_toggle", False)),
            observe_only=bool(driver.get("observe_only", False)),
            priority_forced_stop=bool(inputs.get("priority_forced_stop", False)),
            budget_timeout=bool(inputs.get("budget_timeout", False)),
            cycle_token=str(data.get("cycle_token") or ""),
            capability=capability,
            current_decision=decisions.get("current") or {},
            start_stop_decision=decisions.get("start_stop") or {},
            phase_recommendation=decisions.get("phase") or {},
        )

    @staticmethod
    def build_payload(context: ChargerCycleContext):
        payload = decision.build_wallbox_decision_payload(
            wb_id=context.wb_id, public_mode=context.public_mode, control_mode=context.control_mode,
            current_decision=dict(context.current_decision),
            start_stop_decision=dict(context.start_stop_decision),
            phase_recommendation=dict(context.phase_recommendation),
            allowed_w=context.allowed_w, detected_phases=context.detected_phases,
            current_amp=context.current_amp, current_set_amp=context.current_set_amp,
            cap_amp=context.cap_amp, max_amp=context.max_amp, charger_connected=context.connected,
            hw_charging=context.hw_charging, hw_power_w=context.hw_power_w,
            grid_power_w=context.grid_power_w, mode_label=context.mode_label,
            storage_state=context.storage_state, driver_class_name=context.driver_class_name,
            openwb_like_charger=context.openwb_like, openwb_pro=context.openwb_pro,
            e3dc_native_toggle=context.e3dc_native_toggle, observe_only=context.observe_only,
            priority_forced_stop=context.priority_forced_stop, budget_timeout=context.budget_timeout,
        )
        payload["cycle_token"] = str(context.cycle_token or "")
        payload["capability"] = context.capability.as_dict()
        return payload

    def plan(self, cycle: CycleContext):
        if not isinstance(cycle, CycleContext) or len(cycle.chargers) != 1:
            raise ValueError("exactly_one_charger_context_required")
        context = cycle.chargers[0]
        payload = self.build_payload(context)
        command = decision.driver_command_from_decision_payload(payload)
        planned_intent = ActuatorIntent.from_command(
            command,
            wb_id=context.wb_id,
            cycle_token=context.cycle_token,
            decision_allowed_w=context.allowed_w,
            capability=context.capability,
            source="charger_controller",
            stage="planned",
        ).as_dict()
        payload["planned_actuator_intent"] = planned_intent
        return ControllerResult(
            authoritative=True,
            valid=True,
            command=command,
            payload=payload,
            reason="planned",
            actuator_intent=planned_intent,
        )

    def plan_payload(self, payload, *, now_ts=0.0):
        """Plant aus kanonischen Daten und stoppt ohne Legacy-Fallback sicher."""
        try:
            context = self.context_from_payload(payload)
            cycle = CycleContext(now_ts=float(now_ts or 0.0), chargers=(context,))
            return self.plan(cycle)
        except Exception as exc:
            command = {
                "schema_version": "wallbox_driver_command_v1",
                "kind": "noop",
                "amp": 0,
                "target_phases": 0,
                "reason": "controller_error",
                "source": "charger_controller",
            }
            planned_intent = ActuatorIntent.from_command(
                command,
                source="charger_controller_error",
                stage="planned",
            ).as_dict()
            return ControllerResult(
                authoritative=True,
                valid=False,
                command=command,
                payload=dict(payload) if isinstance(payload, dict) else {},
                reason="controller_error",
                error=str(exc),
                actuator_intent=planned_intent,
            )

    def authorize_driver_command(self, command):
        """Gibt den finalen Hardwarebefehl frei; unbekannte Methoden bleiben gesperrt."""
        cmd = dict(command) if isinstance(command, dict) else {}
        method = str(cmd.get("method") or cmd.get("kind") or "").strip()
        if method in self.DRIVER_METHODS:
            planned_intent = ActuatorIntent.from_command(
                cmd,
                source="charger_controller_output_admission",
                stage="admission_candidate",
            ).as_dict()
            return ControllerResult(
                authoritative=True,
                valid=True,
                command=cmd,
                payload={},
                reason="driver_command_approved",
                actuator_intent=planned_intent,
            )
        command = {
            "schema_version": "wallbox_driver_command_v1",
            "kind": "noop",
            "amp": 0,
            "target_phases": 0,
            "reason": "unsupported_driver_command",
            "source": "charger_controller",
        }
        return ControllerResult(
            authoritative=True,
            valid=False,
            command=command,
            payload={},
            reason="unsupported_driver_command",
            error=method or "missing_driver_method",
            actuator_intent=ActuatorIntent.from_command(
                command,
                source="charger_controller_output_admission",
                stage="rejected",
            ).as_dict(),
        )
