"""Gemeinsame Wärmeprognose aus dem kanonischen Speicherplan, ohne Freigaben."""
import json
import copy

_PLAN_CACHE = {}
from pathlib import Path


def project_plan_forecast(plan):
    """Projiziert Slotquantile und explizite Punkte; fehlende Werte bleiben None."""
    from .tariff_shift import number

    def mapping(value):
        return value if isinstance(value, dict) else {}

    def invalid(reason):
        return {"storage_plan_meta": {}, "heat_price_boost_forecast": {},
                "heat_forecast_source_reason": reason}

    if not isinstance(plan, dict) or plan.get("schema_version") != "storage_dispatch_plan_v1":
        return invalid("forecast_plan_not_canonical")
    if not isinstance(plan.get("slots"), list):
        return invalid("forecast_slots_invalid")
    rows = []
    for slot in plan["slots"]:
        if not isinstance(slot, dict):
            return invalid("forecast_slot_invalid")
        f = mapping(slot.get("forecast_w"))
        pv = mapping(f.get("pv"))
        p10, p50 = number(pv.get("p10")), number(pv.get("p50"))
        if ((pv.get("p10") is not None and (p10 is None or p10 < 0))
                or (pv.get("p50") is not None and (p50 is None or p50 < 0))
                or (p10 is not None and p50 is not None and p10 > p50)):
            return invalid("pv_forecast_implausible")
        evidence = mapping(f.get("evidence"))
        projection = mapping(slot.get("projection"))
        row = {key: slot.get(key) for key in ("start_ts_ms", "end_ts_ms", "slot_id")}
        for component, quantiles in (("pv", ("p10", "p50", "p90")),
                                     ("load", ("p10", "p50", "p90")),
                                     ("house", ("p50",)), ("heat", ("p50",)),
                                     ("wallbox", ("p50",)), ("external_ac_pv", ("p50",))):
            for quantile in quantiles:
                row[component + "_" + quantile + "_w"] = mapping(f.get(component)).get(quantile)
        for component in ("pv", "house", "heat", "wallbox"):
            if "point" in mapping(f.get(component)):
                row[component + "_point_w"] = f[component]["point"]
        for key in ("home_source", "home_quality", "climate_source", "climate_quality"):
            row[key] = projection.get(key)
        row.update(pv_forecast_fresh=evidence.get("pv_fresh") is True,
                   load_valid=evidence.get("load_valid") is True,
                   forecast_fresh=evidence.get("pv_fresh") is True and evidence.get("load_valid") is True)
        rows.append(row)
    return {"storage_plan_meta": {key: plan.get(key) for key in (
                "schema_version", "plan_id", "generated_at", "valid_from", "valid_until",
                "horizon_end", "input_revisions")},
            "heat_price_boost_forecast": {
                "schema_version": "heat_price_boost_forecast_v1", "status": "canonical_projection",
                "shadow_only": True, "commands_allowed": False,
                "plan_id": plan.get("plan_id"), "input_revisions": plan.get("input_revisions"),
                "candidate": plan.get("heat_intent_candidate"), "slots": rows}}


def read_plan_forecast(path):
    """Liest einen Snapshot; Dateifehler sperren nur die Prognose, nicht den WP-Zyklus."""
    try:
        path = Path(path).resolve()
        stat = path.stat()
        key = (str(path), stat.st_dev, stat.st_ino, stat.st_mtime_ns, stat.st_size)
        cached = _PLAN_CACHE.get(key)
        if cached is not None:
            return copy.deepcopy(cached)
        plan = json.loads(path.read_text(encoding="utf-8"), parse_constant=lambda value: (_ for _ in ()).throw(ValueError(value)))
    except FileNotFoundError:
        reason = "forecast_plan_missing"
    except (OSError, ValueError, UnicodeError):
        reason = "forecast_plan_unreadable"
    else:
        projection = project_plan_forecast(plan)
        # Ein Snapshot genügt für beide Verbraucher; keine wachsende Plansammlung.
        _PLAN_CACHE.clear()
        _PLAN_CACHE[key] = projection
        return copy.deepcopy(projection)
    _PLAN_CACHE.clear()
    return {"storage_plan_meta": {}, "heat_price_boost_forecast": {},
            "heat_forecast_source_reason": reason}


def slot_forecast_values(row):
    """Bewertet Quantile, sonst belegte Punkte; Lasten bleiben unskaliert."""
    from .tariff_shift import number

    def value(component, quantile):
        return number(row.get(component + "_" + quantile + "_w"))

    result = {"valid": False, "reason": None, "pv_method": None, "load_method": None}
    for quantile, factor, method in (("p10", 1., "p10"), ("p50", .7, "p50x0.7"),
                                     ("point", .7, "pointx0.7")):
        raw = row.get("pv_" + quantile + "_w")
        if raw is None:
            continue
        pv = value("pv", quantile)
        if pv is None or pv < 0:
            return {**result, "reason": "pv_forecast_implausible"}
        if quantile == "point" and row.get("pv_forecast_fresh") is not True:
            return {**result, "reason": "pv_point_evidence_missing"}
        result.update(pv=pv * factor, pv_method=method)
        break
    else:
        return {**result, "reason": "pv_point_missing"}
    methods = set()
    for component in ("house", "wallbox", "heat"):
        quantile = "p50" if row.get(component + "_p50_w") is not None else "point"
        if quantile == "point" and row.get("load_valid") is not True:
            return {**result, "reason": "load_point_evidence_missing"}
        load = value(component, quantile)
        if load is None or load < 0:
            return {**result, "reason": component + "_" + quantile + "_missing_or_invalid"}
        result[component] = load
        methods.add(quantile)
    return {**result, "valid": True, "load_method": " + ".join(sorted(methods))}
