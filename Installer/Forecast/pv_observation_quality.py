"""Messzuordnung, Begrenzungsfilter und zeitlich getrennte Ist-Kalibrierung.

Reine Diagnosefunktionen: keine Gerätezugriffe, Steuerbefehle oder Aktivierung
eines Prognosefaktors. Unbekannte Betriebszustände bleiben ausdrücklich unbekannt.
"""

from __future__ import annotations

import hashlib
import json
import math

SCHEMA = "pv_observation_quality_v1"
METER_KEY = "pv_external_ac_observation_mode"
METER_MODES = ("unverified", "generation_meter", "generation_meter_uncontrolled")
STATES = ("unrestricted", "protection_absorbing", "curtailed", "clipping_suspected",
          "shutdown", "unknown", "measurement_invalid")
SIGNALS = ("pv_e3dc_dc", "pv_external_ac")


def finite(value):
    if isinstance(value, bool) or value is None or value == "":
        return None
    try:
        value = float(value)
        return value if math.isfinite(value) else None
    except (TypeError, ValueError, OverflowError):
        return None


def positive(value):
    value = finite(value)
    return value if value is not None and value > 0 else None


def binding(config):
    """Eine Betreiberbestätigung gilt ab Erfassung, niemals rückwirkend."""
    mode = config.get(METER_KEY, "unverified")
    mode = mode if mode in METER_MODES else "unverified"
    ac_limits = []
    for key in ("wr_ac_limit_w", "wechselrichter_limit_w", "wechselrichterleistung_w",
                "inverter_ac_limit_w", "ac_power_limit_w"):
        limit = positive(config.get(key))
        if limit is not None:
            ac_limits.append(limit * 1000 if limit < 100 else limit)
    value = {"external_meter_mode": mode,
             "external_ac_limit_w": positive(config.get("pv_external_ac_inverter_limit_w")),
             "e3dc_ac_limit_w": min(ac_limits) if ac_limits else None}
    value["revision"] = "sha256:" + hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return value


def _flag(live, name):
    value = live.get(name)
    return value if live.get(name + "_valid") is True and type(value) is bool else None


def classify(live, config, now_s):
    """Bewertet frische Leistungen je Messgrenze; E3DC-Status gilt nur für DC.

Die Schutzschwelle allein ist kein Abregelungsnachweis. Eine AC-Grenze wird
nur gegen AC-Ausgangsleistung geprüft, nicht gegen PV einschließlich DC-Laden.
    """
    try:
        from pv_forecast_topology import resolve_buffered_pcc_limit
    except ImportError:
        from Installer.pv_forecast_topology import resolve_buffered_pcc_limit
    source = binding(config)
    ts = finite(live.get("_ts"))
    fresh = ts is not None and 0 <= now_s - ts <= 45 and live.get("RSCP_Sample_Valid") is True
    external = finite(live.get("Ext_PV_Power"))
    external_age = finite(live.get("Ext_PV_Power_Age_S"))
    external_valid = (fresh and external is not None and external >= 0
                      and live.get("Ext_PV_Power_Valid") is True
                      and live.get("Ext_PV_Power_Source") == "e3dc_add_power"
                      and external_age is not None and 0 <= external_age <= 45)
    total = finite(live.get("PV_Power"))
    dc_valid = external_valid and total is not None and total >= external
    dc = total - external if dc_valid else None
    battery = finite(live.get("Battery_Power"))
    home = finite(live.get("Home_Power"))
    grid = finite(live.get("Grid_Power"))
    # Diese Abschätzung beschreibt aufgenommenen Überschuss über der Grenze,
    # keine unabhängig gemessene verlorene oder gerettete Energie.
    configured_export = positive(config.get("einspeiselimit")) or 0.0
    if 0 < configured_export < 100:
        configured_export *= 1000.0
    pcc = resolve_buffered_pcc_limit(configured_export,
                                     live.get("derate_at_power_w"), config.get("abregel_puffer_w", 300))
    pcc_limit = pcc["limit_w"] if pcc["active"] else None
    pressure = (max(0.0, total - home - pcc_limit)
                if fresh and total is not None and home is not None and pcc_limit is not None else None)
    absorbing = bool(pressure is not None and pressure > 0 and battery is not None
                     and battery > 0 and grid is not None and -grid <= pcc_limit + 150)
    ac_ts = finite(live.get("pvi_ac_observed_at_s"))
    ac = (finite(live.get("pvi_ac_power_w")) if live.get("pvi_ac_power_valid") is True
          and ac_ts is not None and 0 <= now_s - ac_ts <= 45 else None)
    read_limit = (positive(live.get("ac_power_limit_w"))
                  if live.get("ac_power_limit_w_valid") is True else None)
    configured_limit = source["e3dc_ac_limit_w"]
    nominal_ts = finite(live.get("e3dc_ac_nominal_observed_at_s"))
    nominal = (positive(live.get("e3dc_ac_nominal_power_w"))
               if live.get("e3dc_ac_nominal_power_valid") is True and nominal_ts is not None
               and 0 <= now_s - nominal_ts <= 1800 else None)
    limits = [v for v in (read_limit, nominal, configured_limit) if v is not None]
    dc_limit = min(limits) if limits else None
    result = {"schema_version": SCHEMA, "binding": source, "signals": {},
              "protection_pressure_w": pressure, "protection_absorbing": absorbing,
              "protection_energy_basis": "counterfactual_estimate_not_measured_loss"}
    for signal, power, valid, limit, ac_power in (
        (SIGNALS[0], dc, dc_valid, dc_limit, ac),
        (SIGNALS[1], external, external_valid, source["external_ac_limit_w"], external),
    ):
        is_external = signal == SIGNALS[1]
        meter_confirmed = source["external_meter_mode"] != "unverified" if is_external else True
        curtailed = _flag(live, "external_pv_curtailment_active" if is_external else "pv_observation_derating_active")
        shutdown = _flag(live, "external_pv_shutdown_active" if is_external else "pv_shutdown_active")
        uncontrolled = is_external and source["external_meter_mode"] == "generation_meter_uncontrolled"
        if uncontrolled:
            # Explizite Anlagenangabe; keine Ableitung aus einem fehlenden Status.
            curtailed = False if curtailed is None else curtailed
            shutdown = False if shutdown is None else shutdown
        # Ein positiver Ertrag belegt, dass die Quelle nicht vollständig aus ist.
        if valid and power > 0 and shutdown is None:
            shutdown = False
        near_limit = (limit is not None and ac_power is not None
                      and ac_power >= limit - max(100.0, limit * .02))
        if not valid:
            state = "measurement_invalid"
        elif not meter_confirmed:
            state = "unknown"
        elif shutdown is True:
            state = "shutdown"
        elif curtailed is True:
            state = "curtailed"
        elif near_limit:
            state = "clipping_suspected"
        elif limit is None or ac_power is None or curtailed is None or shutdown is None:
            state = "unknown"
        else:
            state = "protection_absorbing" if absorbing else "unrestricted"
        result["signals"][signal] = {"state": state, "meter_confirmed": meter_confirmed,
            "measurement_basis": "operator_confirmed_generation_meter" if is_external and meter_confirmed else
                                 "e3dc_dc_generation" if not is_external else "unverified",
            "actual_power_w": power if valid else None, "ac_limit_w": limit,
            "curtailment": "observed" if curtailed is True else "not_observed" if curtailed is False else "unknown",
            "clipping": "suspected" if near_limit else "not_observed" if limit is not None and ac_power is not None else "unknown",
            "shutdown": "observed" if shutdown is True else "not_observed" if shutdown is False else "unknown",
            "calibration_eligible": state in ("unrestricted", "protection_absorbing")}
    return result


def calibration(pairs):
    """Faktor auf früheren UTC-Tagen fitten, auf späteren Tagen prüfen.

Paare: (Slotbeginn, Rohprognose Wh, Ist Wh). Keine Auswahl anhand der
Prognosefehler; auch bewölkte Tage zählen. Keine Anwendung auf die Regelung.
    """
    clean = []
    for ts, pred, actual in pairs:
        ts, pred, actual = finite(ts), finite(pred), finite(actual)
        if (ts is not None and pred is not None and actual is not None
                and ts >= 0 and pred >= 0 and actual >= 0 and max(pred, actual) >= 25):
            clean.append((int(ts), pred, actual))
    clean.sort()
    days = sorted({ts // 86400 for ts, _, _ in clean})
    result = {"status": "collecting", "compared_slots": len(clean), "compared_days": len(days),
              "factor": None, "training_slots": 0, "validation_slots": 0,
              "validation_raw_mae_wh": None, "validation_corrected_mae_wh": None,
              "decision_use_allowed": False, "reason": "insufficient_independent_days"}
    if len(days) < 7:
        return result
    split = days[-3]
    train = [(p, a) for ts, p, a in clean if ts // 86400 < split]
    validation = [(p, a) for ts, p, a in clean if ts // 86400 >= split]
    result.update(training_slots=len(train), validation_slots=len(validation))
    if len(train) < 64 or len(validation) < 32 or sum(p for p, _ in train) <= 0:
        return result
    factor = sum(a for _, a in train) / sum(p for p, _ in train)
    raw = sum(abs(a-p) for p, a in validation) / len(validation)
    corrected = sum(abs(a-p*factor) for p, a in validation) / len(validation)
    accepted = .75 <= factor <= 1.40 and corrected <= raw * .95 and abs(factor-1) >= .02
    result.update(status="validated" if accepted else "not_improved", factor=round(factor, 4),
                  validation_raw_mae_wh=round(raw, 3), validation_corrected_mae_wh=round(corrected, 3),
                  reason="later_days_improved" if accepted else "correction_not_validated")
    return result


def slot_quality(record, signal, expected_binding):
    """Altdaten, Mischintervalle und unvollständige Messungen nie aufwerten."""
    def obj(value):
        return value if isinstance(value, dict) else {}
    quality = obj(record.get("observation_quality"))
    bound = obj(quality.get("binding"))
    item = obj(obj(quality.get("signals")).get(signal))
    seconds = {key: finite(obj(item.get("state_seconds")).get(key)) for key in STATES}
    complete = (record.get("valid") is True and quality.get("schema_version") == SCHEMA
                and bound.get("revision") == expected_binding and expected_binding is not None
                and quality.get("binding_changed") is False
                and all(seconds[key] is not None and 0 <= seconds[key] <= 900 for key in STATES)
                and abs(sum(seconds.get(key, 0) for key in STATES) - 900) < 1e-6)
    confirmed = complete and item.get("meter_confirmed") is True
    if signal == "pv_external_ac":
        confirmed = confirmed and bound.get("external_meter_mode") in METER_MODES[1:]
    eligible_s = sum(seconds.get(key, 0) for key in ("unrestricted", "protection_absorbing")) if complete else 0
    return {"complete": complete, "meter_confirmed": confirmed,
            "eligible": confirmed and item.get("calibration_eligible") is True and abs(eligible_s-900) < 1e-6,
            "state_seconds": {key: float(seconds[key]) for key in STATES} if complete else {},
            "ac_limit_w": positive(item.get("ac_limit_w")) if complete else None}


def sanitize_source_summary(value):
    """Kleine öffentliche Zahlenprojektion ohne freie Texte oder Identitäten."""
    result = []
    for signal in SIGNALS:
        item = next((r for r in value if isinstance(r, dict) and r.get("signal") == signal), {}) if isinstance(value, list) else {}
        fit = item.get("calibration") or {}
        stats = {key: max(0, int(finite(item.get(key)) or 0)) for key in
                 ("observed_slots", "quality_slots", "meter_confirmed_slots", "eligible_slots", "excluded_slots")}
        result.append({"signal": signal, **stats,
            "ac_limit_w": positive(item.get("ac_limit_w")),
            "state_seconds": {key: max(0, finite((item.get("state_seconds") or {}).get(key)) or 0) for key in STATES},
            "calibration": {
                "status": fit.get("status") if fit.get("status") in ("collecting", "validated", "not_improved") else "collecting",
                "factor": positive(fit.get("factor")),
                **{key: max(0, finite(fit.get(key)) or 0) for key in
                   ("compared_slots", "compared_days", "training_slots", "validation_slots")},
                **{key: finite(fit.get(key)) for key in ("validation_raw_mae_wh", "validation_corrected_mae_wh")},
                "decision_use_allowed": False},
        })
    return result
