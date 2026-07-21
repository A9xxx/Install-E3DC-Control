"""Phasenscharfer Vertrag für mehrere Wallboxen.

Der Vertrag trennt drei Größen, die nicht skalar addiert werden dürfen:

* Strom je Netzphase,
* Wirkleistung je Fahrzeug,
* vom Fahrzeug tatsächlich angenommene Energie.

Die Funktionen sind rein und führen keine Geräte- oder Dateizugriffe aus.
"""

from __future__ import annotations

from itertools import permutations, product
import math


PHASE_COUNT = 3


def _finite(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def normalize_rotation(rotation):
    """Liefere die lokale-L1..L3-zu-PCC-Permutation oder ``None``."""

    if rotation is None:
        return None
    if isinstance(rotation, str):
        raw = rotation.replace(";", ",").replace(" ", ",").split(",")
        values = [item for item in raw if item != ""]
    else:
        try:
            values = list(rotation)
        except TypeError:
            return None
    try:
        integer_values = tuple(int(value) for value in values)
    except (TypeError, ValueError):
        return None
    if sorted(integer_values) == [0, 1, 2]:
        normalized = integer_values
    else:
        normalized = tuple(value - 1 for value in integer_values)
    if len(normalized) != PHASE_COUNT or sorted(normalized) != [0, 1, 2]:
        return None
    return normalized


def rotate_local_to_pcc(local_vector_a, rotation):
    """Projiziere einen lokalen Dreiphasenvektor auf die PCC-Phasen."""

    values = tuple(max(0.0, _finite(v)) for v in tuple(local_vector_a or ())[:3])
    values = values + (0.0,) * (PHASE_COUNT - len(values))
    normalized = normalize_rotation(rotation)
    if normalized is None:
        raise ValueError("phase_rotation_unbound")
    result = [0.0, 0.0, 0.0]
    for local_index, pcc_index in enumerate(normalized):
        result[pcc_index] = values[local_index]
    return tuple(result)


def vehicle_current_cap_a(
    power_kw,
    phases,
    *,
    evse_cap_a=32,
    cable_cap_a=None,
    explicit_cap_a=None,
    nominal_phase_voltage_v=230.0,
):
    """Binde OBC-, EVSE- und Kabelgrenze zu einem Stromdeckel.

    Das auf Zehntel-kW gerundete Fahrzeugprofil ``11 kW / 3p`` wird als
    nominelle 16-A-Klasse behandelt. Der Rundungsspielraum erhöht niemals
    eine explizite EVSE-, Kabel- oder Stromgrenze.
    """

    phase_count = max(1, min(3, int(_finite(phases, 1))))
    voltage = max(1.0, _finite(nominal_phase_voltage_v, 230.0))
    limits = [max(0.0, _finite(evse_cap_a, 0.0))]
    if cable_cap_a is not None:
        limits.append(max(0.0, _finite(cable_cap_a, 0.0)))
    if explicit_cap_a is not None:
        limits.append(max(0.0, _finite(explicit_cap_a, 0.0)))
    power = max(0.0, _finite(power_kw, 0.0))
    if power > 0.0:
        nominal_amp = power * 1000.0 / (voltage * phase_count)
        limits.append(float(max(0, int(math.floor(nominal_amp + 0.5)))))
    finite_limits = [limit for limit in limits if limit > 0.0]
    return int(math.floor(min(finite_limits))) if finite_limits else 0


def target_local_vector_a(target_amp, phases):
    """Erzeuge den lokalen Zielvektor für eine ein- oder dreiphasige Ladung."""

    amp = max(0.0, _finite(target_amp, 0.0))
    phase_count = max(1, min(3, int(_finite(phases, 1))))
    if phase_count >= 3:
        return (amp, amp, amp)
    if phase_count == 2:
        return (amp, amp, 0.0)
    return (amp, 0.0, 0.0)


def _mapping_options(spec):
    rotation = normalize_rotation(spec.get("phase_rotation"))
    phases = max(1, min(3, int(_finite(spec.get("phases"), 1))))
    if rotation is not None:
        return (rotation,)
    if phases >= 3:
        # Ein symmetrischer 3p-Vektor ist rotationsinvariant.
        return ((0, 1, 2),)
    if phases == 2:
        return tuple(permutations(range(3)))
    # Unbekannte einphasige Zuordnung: robuste Prüfung auf jeder PCC-Phase.
    return (
        (0, 1, 2),
        (1, 0, 2),
        (2, 1, 0),
    )


def _accepted_local_vector(spec):
    if spec.get("accepted_measurement_fresh") is False:
        # Der PCC-Messwert enthält die reale Last bereits. Einen stale
        # Wallbox-Anteil dürfen wir davon nicht abziehen, weil das die
        # berechnete Phasenlast künstlich verkleinern könnte.
        return (0.0, 0.0, 0.0)
    values = spec.get("accepted_local_phase_a")
    if isinstance(values, (list, tuple)) and values:
        compact = tuple(max(0.0, _finite(value)) for value in values[:3])
        return compact + (0.0,) * (3 - len(compact))
    return target_local_vector_a(spec.get("accepted_amp", 0.0), spec.get("phases", 1))


def project_pcc_phase_currents(measured_pcc_a, charger_specs, target_amp_by_id):
    """Projiziere Zielströme per Delta gegen den bereits gemessenen PCC-Wert.

    Für unbekannte einphasige Zuordnungen werden alle möglichen physischen
    Phasen ausgewertet. Das Ergebnis enthält den Worst-Case je Phase.
    """

    measured = tuple(_finite(value) for value in tuple(measured_pcc_a or ())[:3])
    measured = measured + (0.0,) * (3 - len(measured))
    specs = [spec for spec in (charger_specs or []) if int(spec.get("id", 0) or 0) > 0]
    option_sets = [_mapping_options(spec) for spec in specs]
    scenarios = []
    for rotations in product(*option_sets) if option_sets else [()]:
        projected = list(measured)
        for spec, rotation in zip(specs, rotations):
            accepted = rotate_local_to_pcc(_accepted_local_vector(spec), rotation)
            target = rotate_local_to_pcc(
                target_local_vector_a(
                    target_amp_by_id.get(int(spec["id"]), 0.0),
                    spec.get("phases", 1),
                ),
                rotation,
            )
            for phase in range(3):
                projected[phase] += target[phase] - accepted[phase]
        scenarios.append(tuple(projected))
    worst = tuple(max(scenario[phase] for scenario in scenarios) for phase in range(3))
    return {
        "worst_case_pcc_a": worst,
        "scenarios": tuple(scenarios),
        "scenario_count": len(scenarios),
        "mapping_complete": all(normalize_rotation(spec.get("phase_rotation")) is not None or int(spec.get("phases", 1) or 1) >= 3 for spec in specs),
    }


def clamp_targets_to_phase_limit(
    measured_pcc_a,
    charger_specs,
    proposed_amp_by_id,
    *,
    phase_limit_a,
    data_fresh=True,
    min_amp=6,
    fairness_weight_by_id=None,
):
    """Deckele gemeinsame Zielströme gegen jede PCC-Phase.

    Die Suche ist für die reale kleine Wallboxmenge absichtlich vollständig
    und deterministisch. Sie darf Zielwerte nur verringern, nie erhöhen.
    Stale PCC-Daten verbieten jeden Aufwärtsschritt.
    """

    specs = [spec for spec in (charger_specs or []) if int(spec.get("id", 0) or 0) > 0]
    weights = fairness_weight_by_id if isinstance(fairness_weight_by_id, dict) else {}
    if len(specs) > 2:
        # Das Produkt unterstützt aktuell WB1/WB2. Eine vollständige Suche
        # darf bei einer späteren Erweiterung nicht exponentiell in den
        # Regelzyklus wachsen; unbekannter größerer Scope stoppt fail-closed.
        ids = [int(spec["id"]) for spec in specs]
        measured = tuple(_finite(value) for value in tuple(measured_pcc_a or ())[:3])
        measured = measured + (0.0,) * (3 - len(measured))
        return {
            "targets_amp": {charger_id: 0 for charger_id in ids},
            "phase_limit_a": max(0.0, _finite(phase_limit_a, 0.0)),
            "worst_case_pcc_a": measured,
            "mapping_complete": False,
            "scenario_count": 0,
            "data_fresh": bool(data_fresh),
            "limited": True,
            "reason": "unsupported_more_than_two_chargepoints",
        }
    amp_options = []
    ids = []
    for spec in specs:
        charger_id = int(spec["id"])
        ids.append(charger_id)
        proposed = max(0, int(math.floor(_finite(proposed_amp_by_id.get(charger_id), 0.0))))
        accepted = max(
            0,
            int(
                math.floor(
                    _finite(
                        spec.get("no_increase_cap_amp", spec.get("accepted_amp")),
                        0.0,
                    )
                )
            ),
        )
        if not data_fresh:
            proposed = min(proposed, accepted)
        if proposed < int(min_amp):
            amp_options.append((0,))
        else:
            amp_options.append((0,) + tuple(range(int(min_amp), proposed + 1)))

    limit = max(0.0, _finite(phase_limit_a, 0.0))
    best = None
    best_projection = None
    best_score = None
    for candidate_values in product(*amp_options) if amp_options else [()]:
        candidate = dict(zip(ids, candidate_values))
        projection = project_pcc_phase_currents(measured_pcc_a, specs, candidate)
        if any(value > limit + 1e-9 for value in projection["worst_case_pcc_a"]):
            continue
        ratios = []
        weighted_power = 0.0
        total_power = 0.0
        for spec in specs:
            charger_id = int(spec["id"])
            phases = max(1, min(3, int(_finite(spec.get("phases"), 1))))
            target = candidate.get(charger_id, 0)
            proposed = max(1, int(_finite(proposed_amp_by_id.get(charger_id), 0)))
            weight = max(0.01, _finite(weights.get(charger_id), 1.0))
            power = target * phases * 230.0
            total_power += power
            weighted_power += power * weight
            ratios.append(target / proposed if proposed_amp_by_id.get(charger_id, 0) else 1.0)
        score = (
            min(ratios) if ratios else 1.0,
            weighted_power,
            total_power,
            tuple(candidate.get(charger_id, 0) for charger_id in sorted(ids)),
        )
        if best_score is None or score > best_score:
            best_score = score
            best = candidate
            best_projection = projection

    if best is None:
        best = {charger_id: 0 for charger_id in ids}
        best_projection = project_pcc_phase_currents(measured_pcc_a, specs, best)
    return {
        "targets_amp": best,
        "phase_limit_a": limit,
        "worst_case_pcc_a": best_projection["worst_case_pcc_a"],
        "mapping_complete": best_projection["mapping_complete"],
        "scenario_count": best_projection["scenario_count"],
        "data_fresh": bool(data_fresh),
        "limited": any(best.get(charger_id, 0) < max(0, int(_finite(proposed_amp_by_id.get(charger_id), 0))) for charger_id in ids),
        "reason": "phase_limit" if any(best.get(charger_id, 0) < max(0, int(_finite(proposed_amp_by_id.get(charger_id), 0))) for charger_id in ids) else "within_limit",
    }


def aggregate_target_display(charger_specs, target_amp_by_id):
    """Liefere kW und Phasenvektor; niemals eine skalare Ampere-Summe."""

    local_phase_sum = [0.0, 0.0, 0.0]
    total_power_w = 0.0
    mapping_complete = True
    for spec in charger_specs or []:
        charger_id = int(spec.get("id", 0) or 0)
        if charger_id <= 0:
            continue
        phases = max(1, min(3, int(_finite(spec.get("phases"), 1))))
        amp = max(0.0, _finite(target_amp_by_id.get(charger_id), 0.0))
        total_power_w += amp * phases * 230.0
        rotation = normalize_rotation(spec.get("phase_rotation"))
        if rotation is None and phases < 3:
            mapping_complete = False
            continue
        rotated = rotate_local_to_pcc(target_local_vector_a(amp, phases), rotation or (0, 1, 2))
        for phase in range(3):
            local_phase_sum[phase] += rotated[phase]
    return {
        "total_power_w": round(total_power_w, 1),
        "pcc_phase_target_a": tuple(round(value, 2) for value in local_phase_sum) if mapping_complete else None,
        "mapping_complete": mapping_complete,
    }


__all__ = [
    "aggregate_target_display",
    "clamp_targets_to_phase_limit",
    "normalize_rotation",
    "project_pcc_phase_currents",
    "rotate_local_to_pcc",
    "target_local_vector_a",
    "vehicle_current_cap_a",
]
