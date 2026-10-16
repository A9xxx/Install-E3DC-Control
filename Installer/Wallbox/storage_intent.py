"""Erstellt und publiziert den Wallbox-zu-Speicher-Intent-Vertrag."""

import json
import math
import os
import time


def build_storage_intent_payload(data, *, now_ts=None, source="wallbox_manager"):
    payload = dict(data) if isinstance(data, dict) else {}
    payload.setdefault("schema_version", "wallbox_storage_intent_v2")
    payload["ts"] = int(time.time() if now_ts is None else float(now_ts))
    payload["source"] = str(source or "wallbox_manager")
    return payload


class StorageIntentPublisher:
    def __init__(self, path, logger=None):
        self.path = path
        self.logger = logger

    def publish(self, data, *, now_ts=None, source="wallbox_manager"):
        payload = build_storage_intent_payload(data, now_ts=now_ts, source=source)
        tmp = self.path + ".tmp"
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            with open(tmp, "w", encoding="utf-8") as handle:
                json.dump(payload, handle)
            os.replace(tmp, self.path)
            return True
        except Exception as exc:
            try:
                if os.path.exists(tmp):
                    os.remove(tmp)
            except Exception:
                pass
            if self.logger is not None:
                self.logger.debug("Wallbox-Storage-Intent konnte nicht geschrieben werden: %s", exc)
            return False


def storage_curve_d1_active(plan):
    """Das abgesenkte Abendziel gilt nur mit übereinstimmendem Zielvertrag."""
    target = plan.get("wallbox_curve_target_state")
    return (plan.get("wallbox_target_soc_active") is True and isinstance(target, dict)
            and target.get("decision") == "wbminsoc")


def storage_curve_can_reach_target(plan, *, default=True):
    """Reichweite zum wirksamen Kurvenziel, mit Rückfall für ältere Pläne."""
    value = (plan.get("can_reach_target", default) if storage_curve_d1_active(plan)
             else plan.get("normal_target_can_reach", plan.get("can_reach_target", default)))
    # Strikte Altleser unterscheiden fehlend, wahr und falsch.
    return None if value is None and default is None else bool(value)


E3DC_WALLBOX_TYPES = frozenset((
    "native", "e3dc", "e3dc_easy", "e3dc_easy_connect", "e3dc_legacy",
    "e3dc_efy", "e3dc_auto", "e3dc_multi", "e3dc_multi_connect",
    "e3dc_multi_connect_ii",
))


def wallbox_type_is_e3dc(value):
    """Gemeinsame Messpunktzuordnung einschließlich der Konfigurationsaliase."""
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_") in E3DC_WALLBOX_TYPES


def measured_charge_state(previous, *, power_w, now_s, disconnected=False, gap_hold_s=600.0,
                          reset_on_gap=False):
    """Gemeinsamer Messbeleg; externe Ausfälle löschen den Beleg ohne Nachhaltezeit."""
    previous = previous if isinstance(previous, dict) else {}
    valid = type(power_w) in (int, float) and math.isfinite(power_w) and power_w >= 0
    power = float(power_w) if valid else None
    active = previous.get("active") is True
    last_valid = previous.get("last_valid_ts")
    idle_since = previous.get("idle_since")
    last_ts = previous.get("ts")
    # Ein übersprungener Messzeitraum darf den Aus-Timer nicht weiterzählen.
    if last_ts is not None and not 0 <= now_s-last_ts <= gap_hold_s:
        idle_since = None
        active = False
    if disconnected:
        active, idle_since, last_valid = False, None, None
    elif power is None:
        idle_since = None
        active = bool(not reset_on_gap and active and last_valid is not None
                      and 0 <= now_s-last_valid <= gap_hold_s)
        if reset_on_gap:
            last_valid = None
    else:
        last_valid = now_s
        if power >= 500.0:
            active, idle_since = True, None
        elif power < 300.0:
            idle_since = now_s if idle_since is None or idle_since > now_s else idle_since
            if now_s-idle_since >= 45.0:
                active = False
        else:
            idle_since = None
    return {"active": active, "idle_since": idle_since, "last_valid_ts": last_valid,
            "power_w": power, "valid": valid and not disconnected, "ts": now_s}
