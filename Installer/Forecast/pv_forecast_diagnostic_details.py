"""Additive PV-Diagnose: unveränderliche Stufen, Zeitbezug und Messabdeckung.

Keine Regelungsimporte, Konfigurationsänderungen oder Hardwarezugriffe.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import Counter


DETAIL_SCHEMA = "pv_forecast_diagnostic_details_v1"
STAGE_SCHEMA = "pv_forecast_diagnostic_stages_v1"
EXTERNAL_SOURCE = "e3dc_add_power"
SIGNALS = ("pv_e3dc_dc", "pv_external_ac")
STAGES = ("provider_m1_raw", "provider_m2_raw", "provider_m3_raw",
          "ensemble_before_bias", "bias_corrected_before_caps", "displayed_postprocessed")
MAX_SAMPLE_GAP_S = 45.0


def number(value):
    if isinstance(value, bool) or value is None:
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) and result >= 0 else None
    except (ValueError, TypeError):
        return None


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def revision(value):
    return "sha256:" + hashlib.sha256(canonical(value).encode()).hexdigest()


def provider_resource_samples(models, keys, timestamp, adjacent_timestamp, adjacent_weight):
    """Archivwerte in Watt mit derselben Viertelstundeninterpolation bilden."""
    result = {}
    for model in ("m1", "m2", "m3"):
        values = {}
        for key in keys:
            series = (models.get(model) or {}).get(key) or {}
            current, adjacent = number(series.get(timestamp)), number(series.get(adjacent_timestamp))
            if current is not None and adjacent is not None:
                values[key] = (current * (1 - adjacent_weight) + adjacent * adjacent_weight) * 1000
        result[model] = values
    return result


def build_stage_metadata(slot, topology, raw_resources, provider_resources, parameters):
    """Bindet beobachtete Rechenstufen an DC/AC, ohne Ausgangswerte zu ändern.

    Roh- und Biasstufe sind vor den späteren Leistungs-/Tageslimits. Provider-
    Rohwerte werden aus ihren eigenen Ressourcen gebildet, nie aus einem
    nachträglichen Split der endgültigen Gesamtprognose.
    """
    resources = topology.get("resources", [])
    couplings = {r.get("resource_key"): r.get("coupling") for r in resources if isinstance(r, dict)}
    usable = topology.get("split_usable") is True and bool(couplings)
    usable = usable and all(c in {"E3DC_DC", "EXTERNAL_AC"} for c in couplings.values())
    raw_kw, bias_kw = number(slot.get("raw_predicted_kwh")), number(slot.get("bias_corrected_kwh"))
    raw_total = sum(number(v) or 0 for v in raw_resources.values())
    coherent = usable and all(number(raw_resources.get(k)) is not None for k in couplings)
    coherent = coherent and raw_kw is not None and abs(raw_total - raw_kw * 1000) <= max(5.0, raw_total * .001)
    bound_parameters = {"topology_revision": topology.get("revision"), **parameters}
    for key in ("bias_applied", "bias_nominal", "bias_guard_applied", "bias_guard_reason",
                "daily_cap_kwh", "daily_cap_scale", "physical_cap_kw", "physical_cap_applied"):
        if key in slot:
            bound_parameters[key] = slot[key]
    rows = []
    for stage in STAGES:
        values = None
        if stage.startswith("provider_"):
            values = provider_resources.get(stage[9:11]) if usable else None
        elif stage == "ensemble_before_bias" and coherent:
            values = raw_resources
        elif stage == "bias_corrected_before_caps" and coherent and bias_kw is not None:
            if raw_total > 0:
                values = {k: v * bias_kw * 1000 / raw_total for k, v in raw_resources.items()}
            elif bias_kw == 0:
                values = dict(raw_resources)
        for signal, coupling in zip(SIGNALS, ("E3DC_DC", "EXTERNAL_AC")):
            energy = None
            if stage == "displayed_postprocessed" and usable and slot.get("pv_topology_status") == "bound":
                watts = number(slot.get("e3dc_dc_pv_w" if signal == SIGNALS[0] else "external_ac_pv_w"))
                energy = watts * .25 if watts is not None else None
            elif isinstance(values, dict) and all(number(values.get(k)) is not None for k in couplings):
                energy = sum(float(values[k]) for k, c in couplings.items() if c == coupling) * .25
            rows.append({"stage": stage, "signal": signal,
                         "energy_wh": round(energy, 6) if energy is not None else None,
                         "status": "complete" if energy is not None else "EVIDENCE_LIMIT",
                         "source_fresh": (
                             (parameters.get("provider_freshness") or {}).get(stage[9:11]) is True
                             if stage.startswith("provider_") else slot.get("pv_forecast_fresh") is True
                         )})
    return {"schema_version": STAGE_SCHEMA, "unit": "Wh", "interval_s": 900,
            "legacy_slot_value_unit": "kW", "parameters": bound_parameters,
            "parameter_revision": revision(bound_parameters), "stages": rows,
            "decision_use_allowed": False}


def initialize_tables(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS forecast_diagnostic_slots (
            issue_id TEXT NOT NULL REFERENCES forecast_issues(issue_id) ON DELETE CASCADE,
            slot_start_utc_s INTEGER NOT NULL,
            daylight_expected INTEGER,
            source_fresh INTEGER NOT NULL,
            source_status TEXT NOT NULL,
            stages_json TEXT,
            parameter_revision TEXT,
            PRIMARY KEY(issue_id, slot_start_utc_s)
        );
        CREATE TABLE IF NOT EXISTS forecast_external_observations (
            observation_id TEXT PRIMARY KEY,
            topology_revision TEXT NOT NULL,
            slot_start_utc_s INTEGER NOT NULL,
            observed_at_utc_s INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS forecast_diagnostic_parameters (
            parameter_revision TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS idx_forecast_diagnostic_slot_time
            ON forecast_diagnostic_slots(slot_start_utc_s);
        CREATE INDEX IF NOT EXISTS idx_forecast_external_time
            ON forecast_external_observations(topology_revision, slot_start_utc_s);
        CREATE TRIGGER IF NOT EXISTS forecast_diagnostic_slots_no_update
            BEFORE UPDATE ON forecast_diagnostic_slots
            BEGIN SELECT RAISE(ABORT, 'forecast diagnostic slots are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_external_observations_no_update
            BEFORE UPDATE ON forecast_external_observations
            BEGIN SELECT RAISE(ABORT, 'forecast external observations are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_diagnostic_parameters_no_update
            BEFORE UPDATE ON forecast_diagnostic_parameters
            BEGIN SELECT RAISE(ABORT, 'forecast diagnostic parameters are immutable'); END;
    """)


def tables_ready(connection):
    expected = {"forecast_diagnostic_slots", "forecast_external_observations",
                "forecast_diagnostic_slots_no_update", "forecast_external_observations_no_update",
                "forecast_diagnostic_parameters", "forecast_diagnostic_parameters_no_update"}
    return expected.issubset({r[0] for r in connection.execute("SELECT name FROM sqlite_master")})


def archive_details(connection, issue_id, slots):
    for slot in slots:
        start, end = number(slot.get("start_timestamp")), number(slot.get("end_timestamp"))
        if start is None or end is None or start % 900000 or end - start != 900000:
            continue
        daylight = slot.get("pv_diagnostic_daylight_expected")
        metadata = slot.get("pv_diagnostic_stages")
        if not isinstance(metadata, dict) or metadata.get("schema_version") != STAGE_SCHEMA:
            metadata = None
        if metadata is not None and (metadata.get("unit") != "Wh" or metadata.get("interval_s") != 900
                                     or metadata.get("decision_use_allowed") is not False
                                     or metadata.get("parameter_revision") != revision(metadata.get("parameters"))):
            metadata = None
        if metadata is not None:
            connection.execute("INSERT OR IGNORE INTO forecast_diagnostic_parameters VALUES (?, ?)",
                               (metadata["parameter_revision"], canonical(metadata["parameters"])))
            values = {(item.get("stage"), item.get("signal")): number(item.get("energy_wh"))
                      for item in metadata.get("stages", [])
                      if isinstance(item, dict) and item.get("status") == "complete"}
            fresh = {(item.get("stage"), item.get("signal")) for item in metadata.get("stages", [])
                     if isinstance(item, dict) and item.get("source_fresh") is True}
            # Feste Reihenfolge statt zwölfmal wiederholter Signal-/Stufennamen.
            # Die zugehörigen Parameter stehen nur einmal pro Revision in der DB.
            metadata = {"schema_version": "pv_forecast_stage_values_v1",
                        "parameter_revision": metadata["parameter_revision"], "unit": "Wh",
                        "fresh_mask": sum(1 << i for i, key in enumerate(
                            (stage, signal) for stage in STAGES for signal in SIGNALS) if key in fresh),
                        "energy_wh": [values.get((stage, signal)) for stage in STAGES for signal in SIGNALS]}
        connection.execute("""INSERT OR IGNORE INTO forecast_diagnostic_slots
            VALUES (?, ?, ?, ?, ?, ?, ?)""", (issue_id, int(start / 1000),
            int(daylight) if isinstance(daylight, bool) else None,
            int(slot.get("pv_forecast_fresh") is True),
            "bound" if slot.get("pv_topology_status") == "bound" else "unbound",
            canonical(metadata) if metadata else None,
            metadata["parameter_revision"] if metadata else None))


def expanded_stages(metadata):
    """Dekodiert das dokumentierte kompakte Format und ältere Diagnosezeilen."""
    if metadata.get("schema_version") == "pv_forecast_stage_values_v1" and metadata.get("unit") == "Wh":
        values = metadata.get("energy_wh")
        if not isinstance(values, list) or len(values) != len(STAGES) * len(SIGNALS):
            return []
        return [{"stage": stage, "signal": signal, "energy_wh": number(value),
                 "source_fresh": bool(int(metadata.get("fresh_mask") or 0) & (1 << i)),
                 "status": "complete" if number(value) is not None else "EVIDENCE_LIMIT"}
                for i, ((stage, signal), value) in enumerate(zip(
                    ((stage, signal) for stage in STAGES for signal in SIGNALS), values))]
    return metadata.get("stages", []) if metadata.get("schema_version") == STAGE_SCHEMA else []


class ExternalEnergyAccumulator:
    """RAM-Integration gültiger Live-Proben; keine Extrapolation über Lücken.

    Trapezregel zwischen verschiedenen Quellzeitpunkten, maximal 45 Sekunden.
    Ein Neustart beginnt mit leerem RAM und kann keine volle Abdeckung erfinden.
    ADD_POWER ist die vom Live-Produzenten ausgewiesene externe Erzeugung;
    dies beweist keine von weiteren Verbrauchern unabhängige Bruttomessung.
    """
    def __init__(self):
        self.previous = None
        self.latest_timestamp = None
        self.slots = {}

    def observe(self, live, topology_revision, now_s):
        live = live if isinstance(live, dict) else {}
        ts, watts, age = number(live.get("_ts")), number(live.get("Ext_PV_Power")), number(live.get("Ext_PV_Power_Age_S"))
        valid = (ts is not None and 0 <= now_s - ts <= MAX_SAMPLE_GAP_S
                 and age is not None and age <= MAX_SAMPLE_GAP_S and watts is not None
                 and live.get("Ext_PV_Power_Valid") is True and live.get("RSCP_Sample_Valid") is True
                 and live.get("Ext_PV_Power_Source") == EXTERNAL_SOURCE
                 and isinstance(topology_revision, str) and topology_revision.startswith("sha256:"))
        if not valid:
            self.previous = None
            return
        if self.latest_timestamp is not None and ts <= self.latest_timestamp:
            if ts < self.latest_timestamp:
                self.previous = None
            return
        self.latest_timestamp = ts
        derating = (live.get("pv_derating_active") if live.get("pv_derating_active_valid") is True
                    and isinstance(live.get("pv_derating_active"), bool) else None)
        current = (ts, watts, topology_revision, derating)
        previous = self.previous
        if previous and ts <= previous[0]:
            if ts < previous[0]:
                self.previous = None
            return
        self.previous = current
        start = int(ts // 900) * 900
        self.slots.setdefault((topology_revision, start), self._empty())
        if not previous or previous[2] != topology_revision:
            return
        gap = ts - previous[0]
        if gap > MAX_SAMPLE_GAP_S:
            return
        cursor = previous[0]
        while cursor < ts:
            slot_start = int(cursor // 900) * 900
            stop = min(ts, slot_start + 900)
            state = self.slots.setdefault((topology_revision, slot_start), self._empty())
            p0 = previous[1] + (watts - previous[1]) * (cursor - previous[0]) / gap
            p1 = previous[1] + (watts - previous[1]) * (stop - previous[0]) / gap
            state["energy"] += (p0 + p1) * .5 * (stop - cursor) / 3600
            state["covered"] += stop - cursor
            state["max_gap"] = max(state["max_gap"], gap)
            state["segments"] += 1
            if previous[3] is True or derating is True:
                state["derating_observed"] = True
            if previous[3] is None or derating is None:
                state["derating_unknown"] = True
            cursor = stop

    @staticmethod
    def _empty():
        return {"energy": 0.0, "covered": 0.0, "max_gap": 0.0, "segments": 0,
                "derating_observed": False, "derating_unknown": False}

    def closed(self, now_s):
        result = []
        for (topology, start), state in list(self.slots.items()):
            # Eine frische Folgeprobe darf die letzte Teilstrecke noch schließen.
            if start + 900 + MAX_SAMPLE_GAP_S > now_s:
                continue
            complete = abs(state["covered"] - 900) < 1e-6
            result.append({"schema_version": "pv_external_ac_observation_v1",
                "topology_revision": topology, "slot_start_utc_s": start,
                "slot_end_utc_s": start + 900, "observed_at_utc_s": int(now_s),
                "actual_energy_wh": round(state["energy"], 6) if complete else None,
                "observed_partial_energy_wh": round(state["energy"], 6),
                "covered_seconds": round(state["covered"], 6),
                "coverage_pct": round(state["covered"] / 9, 3),
                "uncovered_seconds": round(max(0, 900 - state["covered"]), 6),
                "max_integrated_sample_gap_s": state["max_gap"], "integration_segments": state["segments"],
                "valid": complete, "reason": "ok" if complete else "measurement_gaps",
                "source": EXTERNAL_SOURCE, "method": "time_weighted_trapezoid_no_extrapolation_v1",
                "gross_generation_independently_proven": False,
                "curtailment": "observed" if state["derating_observed"] else
                    "unknown" if state["derating_unknown"] or not complete else "not_observed",
                "clipping": "unknown", "external_shutdown": "unknown",
                "decision_use_allowed": False})
            del self.slots[(topology, start)]
        return result


def store_external(connection, records):
    inserted = 0
    for record in records:
        energy, covered = number(record.get("actual_energy_wh")), number(record.get("covered_seconds"))
        if (record.get("schema_version") != "pv_external_ac_observation_v1"
                or record.get("decision_use_allowed") is not False or record.get("source") != EXTERNAL_SOURCE
                or covered is None or covered > 900
                or record.get("slot_end_utc_s", 0) - record.get("slot_start_utc_s", 0) != 900
                or record.get("slot_start_utc_s", 1) % 900
                or (record.get("valid") is not True and record.get("actual_energy_wh") is not None)
                or (record.get("valid") is True and (energy is None or covered < 900 - 1e-6))):
            continue
        payload = canonical(record)
        cursor = connection.execute("""INSERT OR IGNORE INTO forecast_external_observations
            VALUES (?, ?, ?, ?, ?)""", (revision(record), record["topology_revision"],
            record["slot_start_utc_s"], record["observed_at_utc_s"], payload))
        inserted += max(0, cursor.rowcount)
    return inserted


def error_metrics(pairs):
    if not pairs:
        return {"compared_slots": 0, "mae_wh": None, "bias_wh": None, "rmse_wh": None, "wape_pct": None}
    errors = [actual - forecast for forecast, actual in pairs]
    actual_sum = sum(actual for _, actual in pairs)
    return {"compared_slots": len(pairs), "mae_wh": round(sum(map(abs, errors)) / len(errors), 3),
            "bias_wh": round(sum(errors) / len(errors), 3),
            "rmse_wh": round(math.sqrt(sum(e * e for e in errors) / len(errors)), 3),
            "wape_pct": round(sum(map(abs, errors)) / actual_sum * 100, 3) if actual_sum > 0 else None}


def calculate_details(connection, topology_revision, method_revision, now_s):
    """Wertet ausschließlich aktuelle Methode/Topologie und vorherige Ausgaben aus."""
    cutoff, end = now_s - 90 * 86400, now_s - 3600
    rows = connection.execute("""
        SELECT d.issue_id, d.slot_start_utc_s, d.daylight_expected,
               d.source_fresh, d.source_status, c.producer_issued_at_utc_s AS issued,
               p.captured_at_utc_s AS captured, f.predicted_e3dc_dc_energy_wh AS predicted
        FROM forecast_diagnostic_slots d
        JOIN forecast_issue_contracts c USING(issue_id)
        JOIN forecast_issue_provenance p USING(issue_id)
        LEFT JOIN forecast_slots f USING(issue_id,slot_start_utc_s)
        WHERE c.topology_revision=? AND c.method_revision=?
          AND c.producer_issue_time_status='complete'
          AND c.method_revision_status='complete'
          AND c.postprocessing_revision_status='complete'
          AND c.source_composition_status='complete'
          AND c.target_slots_status='complete'
          AND d.slot_start_utc_s>=? AND d.slot_start_utc_s+900<=?
          AND c.producer_issued_at_utc_s<=d.slot_start_utc_s
          AND p.captured_at_utc_s<=d.slot_start_utc_s
          AND d.slot_start_utc_s-c.producer_issued_at_utc_s<259200
        ORDER BY c.producer_issued_at_utc_s, d.issue_id
    """, (topology_revision, method_revision, cutoff, end)).fetchall()
    latest = {r["slot_start_utc_s"]: r for r in rows}
    actual = {}
    for row in connection.execute("""SELECT * FROM observed_slots
            WHERE topology_revision=? AND source_contract='e3dc_db_history_day_15m_v1'
            AND slot_start_utc_s>=? AND slot_end_utc_s<=?
            ORDER BY valid, observed_at_utc_s, observation_id""", (topology_revision, cutoff, end)):
        actual[row["slot_start_utc_s"]] = row["actual_e3dc_dc_energy_wh"] if row["valid"] else None
    excluded = Counter()
    expected = matched = 0
    for start, row in latest.items():
        if row["daylight_expected"] == 1:
            expected += 1
            matched += int(actual.get(start) is not None and row["predicted"] is not None and row["source_fresh"] == 1)
        if row["source_status"] != "bound":
            excluded["topology_unbound"] += 1
        elif not row["source_fresh"]:
            excluded["forecast_not_fresh"] += 1
        elif row["predicted"] is None:
            excluded["forecast_value_missing"] += 1
        elif actual.get(start) is None:
            excluded["observation_missing_or_invalid"] += 1
        elif max(row["predicted"], actual[start]) < 25:
            excluded["below_25_wh"] += 1
    first, last = (min(latest), max(latest) + 900) if latest else (None, None)
    grid_count = (last - first) // 900 if first is not None else 0
    unknown = sum(r["daylight_expected"] is None for r in latest.values()) + grid_count - len(latest)
    external = {}
    for row in connection.execute("""SELECT * FROM forecast_external_observations
            WHERE topology_revision=? AND slot_start_utc_s>=? AND slot_start_utc_s+900<=?
            ORDER BY observed_at_utc_s, observation_id""", (topology_revision, cutoff, end)):
        item = json.loads(row["payload_json"])
        key = row["slot_start_utc_s"]
        if item.get("valid") is True or key not in external:
            external[key] = item
    stages = {}
    stage_archive_count = 0
    for start, row in latest.items():
        if row["source_status"] != "bound" or not row["source_fresh"]:
            continue
        stored = connection.execute("""SELECT stages_json FROM forecast_diagnostic_slots
            WHERE issue_id=? AND slot_start_utc_s=?""", (row["issue_id"], start)).fetchone()
        if not stored or not stored[0]:
            continue
        metadata = json.loads(stored[0])
        stage_archive_count += 1
        for item in expanded_stages(metadata):
            key = (item.get("signal"), item.get("stage"))
            if key[0] not in SIGNALS or key[1] not in STAGES:
                continue
            predicted = number(item.get("energy_wh")) if item.get("status") == "complete" and item.get("source_fresh") is True else None
            measured = actual.get(start) if key[0] == SIGNALS[0] else (external.get(start) or {}).get("actual_energy_wh")
            if predicted is not None and measured is not None and max(predicted, measured) >= 25:
                stages.setdefault(key, []).append((predicted, measured))
    # Ein vollständiger UTC-Tag stammt aus genau einer Ausgabe, die bereits
    # vor Tagesbeginn erzeugt UND archiviert war. Keine Slot-Mosaike.
    daily_candidates = {}
    for row in rows:
        start = row["slot_start_utc_s"]
        day = start // 86400 * 86400
        if row["issued"] <= day and row["captured"] <= day:
            daily_candidates.setdefault((day, row["issue_id"]), []).append(row)
    daily = {}
    for (day, issue), items in daily_candidates.items():
        if len(items) != 96 or len({r["slot_start_utc_s"] for r in items}) != 96:
            continue
        if any(r["predicted"] is None or r["source_fresh"] != 1 or r["source_status"] != "bound" for r in items):
            continue
        if day not in daily or items[0]["issued"] > daily[day][0]["issued"]:
            daily[day] = items
    day_pairs = []
    for day, items in daily.items():
        if all(actual.get(r["slot_start_utc_s"]) is not None for r in items):
            day_pairs.append((sum(r["predicted"] for r in items), sum(actual[r["slot_start_utc_s"]] for r in items)))
    return {"schema_version": DETAIL_SCHEMA, "decision_use_allowed": False,
        "period_start_utc_s": first, "period_end_utc_s": last,
        "expected_grid_slots": grid_count, "archived_slots": len(latest),
        "unarchived_slots": grid_count - len(latest), "exclusion_counts": dict(excluded),
        "expected_daylight_slots": expected, "compared_daylight_slots": matched,
        "daylight_unknown_slots": unknown,
        "daylight_coverage_pct": round(matched / expected * 100, 3) if expected else None,
        "daylight_basis": "producer_solar_window_estimate_with_unknown_gaps",
        "stage_archived_slots": stage_archive_count,
        "stage_metrics": [{"signal": signal, "stage": stage, **error_metrics(stages.get((signal, stage), []))}
                          for signal in SIGNALS for stage in STAGES],
        "frozen_daily": {"basis": "latest_complete_issue_captured_before_utc_day_start",
            "forecast_days": len(daily), "compared_days": len(day_pairs),
            "incomplete_observation_days": len(daily) - len(day_pairs),
            **{k: v for k, v in error_metrics(day_pairs).items() if k != "compared_slots"}},
        "external_observation": {"source": EXTERNAL_SOURCE, "observed_slots": len(external),
            "complete_slots": sum(r.get("valid") is True for r in external.values()),
            "incomplete_slots": sum(r.get("valid") is not True for r in external.values()),
            "covered_seconds": round(sum(r.get("covered_seconds", 0) for r in external.values()), 3),
            "curtailment_observed_slots": sum(r.get("curtailment") == "observed" for r in external.values()),
            "quality_filtered_comparison_allowed": False,
            "gross_generation_independently_proven": False,
            "clipping_status": "unknown", "shutdown_status": "unknown"}}


def sanitize_details(value):
    """Nur endliche Kennzahlen und feste Kategorien verlassen die private DB."""
    if not isinstance(value, dict) or value.get("schema_version") != DETAIL_SCHEMA or value.get("decision_use_allowed") is not False:
        return None
    result = {"schema_version": DETAIL_SCHEMA, "decision_use_allowed": False}
    keys = ("period_start_utc_s", "period_end_utc_s", "expected_grid_slots", "archived_slots",
            "unarchived_slots", "expected_daylight_slots", "compared_daylight_slots", "daylight_unknown_slots",
            "daylight_coverage_pct", "stage_archived_slots")
    result.update({key: number(value.get(key)) for key in keys})
    result["exclusion_counts"] = {k: int(number((value.get("exclusion_counts") or {}).get(k)) or 0)
        for k in ("topology_unbound", "forecast_not_fresh", "forecast_value_missing", "observation_missing_or_invalid", "below_25_wh")}
    result["stage_metrics"] = []
    for item in (value.get("stage_metrics") or [])[:len(SIGNALS) * len(STAGES)]:
        if isinstance(item, dict) and item.get("signal") in SIGNALS and item.get("stage") in STAGES:
            result["stage_metrics"].append({"signal": item["signal"], "stage": item["stage"], **_metrics_projection(item),
                                             "compared_slots": int(number(item.get("compared_slots")) or 0)})
    daily = value.get("frozen_daily") or {}
    result["frozen_daily"] = {"basis": "latest_complete_issue_captured_before_utc_day_start", **_metrics_projection(daily),
        **{k: int(number(daily.get(k)) or 0) for k in ("forecast_days", "compared_days", "incomplete_observation_days")}}
    external = value.get("external_observation") or {}
    result["external_observation"] = {"source": EXTERNAL_SOURCE,
        **{k: number(external.get(k)) for k in ("observed_slots", "complete_slots", "incomplete_slots", "covered_seconds", "curtailment_observed_slots")},
        "quality_filtered_comparison_allowed": False, "gross_generation_independently_proven": False,
        "clipping_status": "unknown", "shutdown_status": "unknown"}
    return result


def _metrics_projection(value):
    result = {k: number(value.get(k)) for k in ("mae_wh", "rmse_wh", "wape_pct")}
    bias = value.get("bias_wh")
    try:
        bias = float(bias) if not isinstance(bias, bool) else None
        result["bias_wh"] = bias if bias is not None and math.isfinite(bias) else None
    except (TypeError, ValueError):
        result["bias_wh"] = None
    return result
