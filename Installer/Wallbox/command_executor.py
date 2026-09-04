"""Einzige hardwarenahe Ausgangskante für Wallboxtreibermethoden.

Policy, Zeitwächter und Phasensequenzierung bleiben außerhalb dieses Moduls.
Der Executor erhält einen bereits freigegebenen Befehl und führt genau eine
Treiberoperation aus.
"""


def _amp_limit(value, maximum=16):
    try:
        return max(0, min(int(maximum), int(round(float(value or 0)))))
    except (TypeError, ValueError):
        return 0


def _driver_max_amp(charger):
    """Liefere die am Treiber gebundene Infrastrukturgrenze.

    Ein fehlender Beleg bleibt konservativ bei 16 A. 32 A entstehen nur aus
    einer beim Erzeugen des Treibers ausdrücklich gebundenen Konfiguration.
    """

    try:
        maximum = float(getattr(charger, "max_amp", 16.0) or 16.0)
    except (TypeError, ValueError):
        maximum = 16.0
    if maximum != maximum or maximum in (float("inf"), float("-inf")):
        maximum = 16.0
    return max(6.0, min(32.0, maximum))


def _driver_current_amp(charger, value):
    try:
        amp = float(value or 0.0)
    except (TypeError, ValueError):
        amp = 0.0
    if amp < 0.5:
        return 0.0
    return max(6.0, min(_driver_max_amp(charger), amp))


class CommandExecutor:
    def __init__(self, logger):
        self.logger = logger
        # Prozesslokales, nicht serialisierbares Siegel. Nur die letzte
        # Manager-I/O-Kante darf damit einen genau einmal verwendbaren
        # autonomen efy-Ausgang autorisieren.
        self._autonomous_solar_dispatch_seal = object()

    def seal_autonomous_solar_dispatch(
        self,
        charger,
        output_contract,
        *,
        cycle_token,
        amp,
        force_state=None,
    ):
        contract = (
            dict(output_contract)
            if isinstance(output_contract, dict)
            else {}
        )
        token = str(cycle_token or "")
        try:
            sealed_amp = float(amp)
        except (TypeError, ValueError):
            return {}
        if not (
            token
            and contract.get("contract")
            == "wallbox_autonomous_solar_output_v1"
            and contract.get("active") is True
            and str(contract.get("cycle_token") or "") == token
            and str(contract.get("method") or "")
            == "set_amp_autonomous_solar"
            and str(contract.get("protocol_mode") or "")
            == "wbchar6_solar_mode"
            and str(contract.get("watt_budget_semantics") or "")
            == "autonomous_pv_sink"
            and contract.get("strict_watt_cap") is False
        ):
            return {}
        return {
            "contract": "wallbox_autonomous_solar_dispatch_authority_v1",
            "method": "set_amp_autonomous_solar",
            "cycle_token": token,
            "amp": sealed_amp,
            "force_state": force_state,
            "charger_identity": id(charger),
            "output_contract": contract,
            "_executor_seal": self._autonomous_solar_dispatch_seal,
            "_consumed": False,
        }

    def _consume_autonomous_solar_dispatch(self, charger, command):
        cmd = command if isinstance(command, dict) else {}
        authority = cmd.get("_autonomous_solar_dispatch_authority")
        if not isinstance(authority, dict):
            return False
        try:
            command_amp = float(cmd.get("amp", 0.0) or 0.0)
            sealed_amp = float(authority.get("amp", 0.0) or 0.0)
        except (TypeError, ValueError):
            return False
        output = authority.get("output_contract")
        valid = bool(
            authority.get("_executor_seal")
            is self._autonomous_solar_dispatch_seal
            and authority.get("_consumed") is False
            and authority.get("contract")
            == "wallbox_autonomous_solar_dispatch_authority_v1"
            and authority.get("method") == "set_amp_autonomous_solar"
            and int(authority.get("charger_identity", -1)) == id(charger)
            and str(authority.get("cycle_token") or "")
            and command_amp == sealed_amp
            and authority.get("force_state") == cmd.get("force_state")
            and isinstance(output, dict)
            and output.get("active") is True
            and output.get("method") == "set_amp_autonomous_solar"
            and str(output.get("cycle_token") or "")
            == str(authority.get("cycle_token") or "")
        )
        if valid:
            authority["_consumed"] = True
        return valid

    @staticmethod
    def stop_command(
        charger,
        *,
        hard_stop_allowed=False,
        stop_authority=None,
        reason="stop",
    ):
        authority = dict(stop_authority) if isinstance(stop_authority, dict) else {}
        if hasattr(charger, "set_amp_sonnenmodus"):
            return {
                "kind": "stop",
                "method": "set_amp_sonnenmodus",
                "amp": 6,
                "force_state": 1 if hard_stop_allowed else None,
                "stop_authority": authority,
                "reason": reason,
            }
        if hasattr(charger, "set_amp_and_state"):
            return {"kind": "stop", "method": "set_amp_and_state", "amp": 0,
                    "force_state": 1, "stop_authority": authority, "reason": reason}
        if hasattr(charger, "set_direct_current"):
            return {"kind": "stop", "method": "set_direct_current", "amp": 0,
                    "stop_authority": authority, "reason": reason}
        if hasattr(charger, "release_to_e3dc"):
            return {"kind": "stop", "method": "release_to_e3dc", "max_amp": 6,
                    "stop_authority": authority, "reason": reason}
        return {}

    def execute(self, charger, command, *, c_id=None):
        cmd = command if isinstance(command, dict) else {}
        method = str(cmd.get("method") or cmd.get("kind") or "").strip()
        kind = str(cmd.get("kind") or method or "driver_command").strip()
        reason = str(cmd.get("reason") or kind)

        def cmd_int(name, default=0):
            try:
                return int(round(float(cmd.get(name, default) or 0)))
            except (TypeError, ValueError):
                return int(default)

        def cmd_amp(name="amp", default=0.0):
            try:
                return float(cmd.get(name, default) or 0.0)
            except (TypeError, ValueError):
                return float(default)

        def driver_amp(name="amp", default=0.0):
            return _driver_current_amp(charger, cmd_amp(name, default))

        try:
            if method == "take_control":
                return bool(charger.take_control())
            if method == "set_amp_and_state":
                return bool(charger.set_amp_and_state(driver_amp(), force_state=cmd.get("force_state")))
            if method == "set_amp_sonnenmodus":
                return bool(charger.set_amp_sonnenmodus(driver_amp(), force_state=cmd.get("force_state")))
            if method == "set_amp_autonomous_solar":
                if not self._consume_autonomous_solar_dispatch(charger, cmd):
                    self.logger.warning(
                        "Autonomer efy-Solarbefehl ohne gültige "
                        "Manager-Versiegelung blockiert (%s).",
                        reason,
                    )
                    return False
                return bool(charger.set_amp_autonomous_solar(
                    driver_amp(),
                    force_state=cmd.get("force_state"),
                ))
            if method == "set_direct_current":
                return bool(charger.set_direct_current(driver_amp()))
            if method == "set_pv_mode":
                return bool(charger.set_pv_mode())
            if method == "set_phases":
                phases = cmd_int("phases", cmd_int("target_phases", 0))
                if cmd.get("_require_wire_receipt") is True:
                    # Dieses private Flag entsteht ausschließlich im
                    # openWB-Pro-Sequenzvertrag. Signatur- oder Treiberfehler
                    # müssen fail-closed bleiben; ein Fallback könnte denselben
                    # Phasenbefehl nach bereits erfolgtem POST doppelt senden.
                    return bool(charger.set_phases(phases, require_wire_receipt=True))
                return bool(charger.set_phases(phases))
            if method == "set_heartbeat":
                return bool(charger.set_heartbeat(enabled=bool(cmd.get("enabled", False))))
            if method == "emergency_stop":
                return bool(charger.emergency_stop())
            if method == "trigger_cp_interrupt":
                duration = cmd.get("duration")
                version = cmd.get("version")
                if duration is None and version is None:
                    return bool(charger.trigger_cp_interrupt())
                try:
                    return bool(charger.trigger_cp_interrupt(duration=duration, version=version))
                except TypeError:
                    return bool(charger.trigger_cp_interrupt())
            if method == "release_to_e3dc":
                return bool(charger.release_to_e3dc(max_amp=_amp_limit(
                    cmd.get("max_amp", cmd.get("amp", 6)),
                    int(_driver_max_amp(charger)),
                )))
            if method == "release_to_default":
                return bool(charger.release_to_default(max_amp=_amp_limit(
                    cmd.get("max_amp", cmd.get("amp", 16)),
                    int(_driver_max_amp(charger)),
                )))
            if method == "set_current":
                amp = driver_amp()
                if hasattr(charger, "set_amp_sonnenmodus"):
                    return bool(charger.set_amp_sonnenmodus(amp, force_state=cmd.get("force_state")))
                if hasattr(charger, "set_direct_current"):
                    return bool(charger.set_direct_current(amp))
                if hasattr(charger, "set_amp_and_state"):
                    return bool(charger.set_amp_and_state(amp, force_state=cmd.get("force_state")))
                if hasattr(charger, "release_to_e3dc"):
                    return bool(charger.release_to_e3dc(max_amp=_amp_limit(
                        amp,
                        int(_driver_max_amp(charger)),
                    )))
        except Exception as exc:
            if c_id is not None:
                self.logger.warning(
                    "WB%d Treiberbefehl fehlgeschlagen (%s/%s): %s",
                    int(c_id), method or kind, reason, exc,
                )
            else:
                self.logger.warning(
                    "Wallbox-Treiberbefehl fehlgeschlagen (%s/%s): %s",
                    method or kind, reason, exc,
                )
        return False
