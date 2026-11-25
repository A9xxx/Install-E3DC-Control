"""Sitzungsadapter im Arbeitsspeicher, ohne Datei- oder Hardwarezugriffe."""
import math
from .fixed_phase_session import update_fixed_phase_session
from .decision import e3dc_autonomous_solar_provenance

def _sample_id(status):
    seq = status.get("native_status_sample_seq")
    ts = status.get("native_status_sample_ts")
    if type(seq) in (int, float) and math.isfinite(seq) and seq > 0 and type(ts) in (int, float) and math.isfinite(ts) and ts > 0:
        return "native:%s:%s" % (seq, ts)
    ts = status.get("driver_status_last_sample_ts")
    if type(ts) in (int, float) and math.isfinite(ts) and ts > 0:
        return "driver:%s" % ts
    return None

def _eligible(box, status):
    charger = box.get("charger")
    name = charger.__class__.__name__ if charger is not None else str(box.get("_charger_class_name") or "")
    family = str(status.get("e3dc_device_family") or getattr(charger, "device_family", "") or "").lower()
    return bool(name in {"E3DCCharger", "E3DCMultiConnectCharger"}
                and family != "efy" and status.get("can_switch_phases") is not True
                and status.get("autonomous_phase_switch_capable") is not True
                and not e3dc_autonomous_solar_provenance(status)
                # Lesender ALG-/Phasenstatus ist keine Python-Stromfreigabe.
                # Statusbeleg und tatsächlich gebundener Treiber müssen den
                # ausdrücklich gewählten flüchtigen WBchar6-Pfad bestätigen.
                and status.get("e3dc_control_backend") == "wbchar6_compat"
                and status.get("e3dc_wbchar6_compat_explicit") is True
                and getattr(charger, "control_backend", None) == "wbchar6_compat"
                and getattr(charger, "wbchar6_compat_explicit", None) is True)


class FixedPhaseRuntime:
    """Beobachtet feste Boxen mit der vorhandenen entprellten Steckepisode."""

    def __init__(self):
        self.sessions = {}

    def observe(self, boxes, statuslist, config, now_s):
        statuses = {int(item.get("id", 0) or 0): item.get("status") or {}
                    for item in statuslist or [] if isinstance(item, dict)}
        contracts = {}
        for box in boxes or []:
            cid = int(box.get("id", 0) or 0)
            status = statuses.get(cid, {})
            box.pop("_fixed_phase_session_contract", None)
            if cid <= 0 or not _eligible(box, status):
                continue
            sid = str(box.get("_wallbox_plug_episode_id") or "")
            if not sid:
                continue
            result = update_fixed_phase_session(
                self.sessions.get(cid), status=status, now_s=now_s,
                session_id=sid,
                sample_id=_sample_id(status),
                vehicle_max_phases=box.get("_fixed_phase_vehicle_max_phases", 0),
            )
            self.sessions[cid] = result["state"]
            contract = dict(result["phase_contract"], cycle_token=box.get("_wallbox_cycle_token"))
            box["_fixed_phase_session_contract"] = contract
            contracts[cid] = contract
        return contracts
