"""Einzige hardwarenahe Ausgangskante für Wallboxtreibermethoden.

Policy, Zeitwächter und Phasensequenzierung bleiben außerhalb dieses Moduls.
Der Executor erhält einen bereits freigegebenen Befehl und führt genau eine
Treiberoperation aus.
"""


def _amp_limit(value, maximum=32):
    try:
        return max(0, min(int(maximum), int(round(float(value or 0)))))
    except (TypeError, ValueError):
        return 0


class CommandExecutor:
    def __init__(self, logger):
        self.logger = logger

    @staticmethod
    def stop_command(charger, *, hard_stop_allowed=False, reason="stop"):
        if hasattr(charger, "set_amp_sonnenmodus"):
            return {"kind": "stop", "method": "set_amp_sonnenmodus", "amp": 6,
                    "force_state": 1 if hard_stop_allowed else None, "reason": reason}
        if hasattr(charger, "set_amp_and_state"):
            return {"kind": "stop", "method": "set_amp_and_state", "amp": 0,
                    "force_state": 1, "reason": reason}
        if hasattr(charger, "set_direct_current"):
            return {"kind": "stop", "method": "set_direct_current", "amp": 0, "reason": reason}
        if hasattr(charger, "release_to_e3dc"):
            return {"kind": "stop", "method": "release_to_e3dc", "max_amp": 6, "reason": reason}
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

        try:
            if method == "take_control":
                return bool(charger.take_control())
            if method == "set_amp_and_state":
                return bool(charger.set_amp_and_state(cmd_amp(), force_state=cmd.get("force_state")))
            if method == "set_amp_sonnenmodus":
                return bool(charger.set_amp_sonnenmodus(cmd_amp(), force_state=cmd.get("force_state")))
            if method == "set_direct_current":
                return bool(charger.set_direct_current(cmd_amp()))
            if method == "set_pv_mode":
                return bool(charger.set_pv_mode())
            if method == "set_phases":
                return bool(charger.set_phases(cmd_int("phases", cmd_int("target_phases", 0))))
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
                return bool(charger.release_to_e3dc(max_amp=_amp_limit(cmd.get("max_amp", cmd.get("amp", 6)), 32)))
            if method == "release_to_default":
                return bool(charger.release_to_default(max_amp=_amp_limit(cmd.get("max_amp", cmd.get("amp", 32)), 32)))
            if method == "set_current":
                amp = cmd_amp()
                if hasattr(charger, "set_amp_sonnenmodus"):
                    return bool(charger.set_amp_sonnenmodus(amp, force_state=cmd.get("force_state")))
                if hasattr(charger, "set_direct_current"):
                    return bool(charger.set_direct_current(amp))
                if hasattr(charger, "set_amp_and_state"):
                    return bool(charger.set_amp_and_state(amp, force_state=cmd.get("force_state")))
                if hasattr(charger, "release_to_e3dc"):
                    return bool(charger.release_to_e3dc(max_amp=_amp_limit(amp, 32)))
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
