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
from numbers import Real


PHASE_COUNT = 3


def _finite(value, default=0.0):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return float(default)
    return number if math.isfinite(number) else float(default)


def _strict_numeric_vector(raw, *, allow_none=False):
    """Normalisiere genau drei echte numerische List-/Tuple-Werte.

    Sicherheitsrelevante Phasenvektoren dürfen nicht implizit aus Text,
    Bytes, Mappings oder anderen Iterables entstehen. Insbesondere darf
    ``"123"`` nicht still als drei Stromwerte interpretiert werden.
    ``None`` ist nur für rohe Konfigurationsvektoren als ausdrücklicher
    Scalar-Fallback zulässig.
    """

    if not isinstance(raw, (list, tuple)) or len(raw) != PHASE_COUNT:
        return None
    normalized = []
    for value in raw:
        if value is None and allow_none:
            normalized.append(None)
            continue
        if isinstance(value, bool) or not isinstance(value, Real):
            return None
        number = float(value)
        if not math.isfinite(number):
            return None
        normalized.append(number)
    return tuple(normalized)


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

    values = _strict_numeric_vector(local_vector_a)
    if values is None or not all(value >= 0.0 for value in values):
        raise ValueError("phase_vector_invalid")
    normalized = normalize_rotation(rotation)
    if normalized is None:
        raise ValueError("phase_rotation_unbound")
    result = [0.0, 0.0, 0.0]
    for local_index, pcc_index in enumerate(normalized):
        result[pcc_index] = values[local_index]
    return tuple(result)


def resolve_single_phase_pcc_mapping(*, grid_phase=None, phase_rotation=None):
    """Binde lokale L1 einer einphasigen Wallbox eindeutig an eine PCC-Phase.

    ``grid_phase`` verwendet die für Nutzer sichtbaren Werte 1..3.
    ``phase_rotation`` beschreibt wie im übrigen Phasenvertrag die vollständige
    lokale-L1..L3-zu-PCC-Permutation. Sind beide Angaben vorhanden, müssen sie
    dieselbe physische Phase benennen. Eine fehlende, ungültige oder
    widersprüchliche Zuordnung bleibt ausdrücklich ungebunden.
    """

    phase_supplied = grid_phase is not None and str(grid_phase).strip() != ""
    rotation_supplied = (
        phase_rotation is not None and str(phase_rotation).strip() != ""
    )

    phase_index = None
    if phase_supplied:
        try:
            phase_value = float(grid_phase)
        except (TypeError, ValueError):
            return {
                "valid": False,
                "pcc_phase": None,
                "pcc_phase_index": None,
                "rotation": None,
                "source": "grid_phase",
                "reason": "grid_phase_invalid",
            }
        if not math.isfinite(phase_value) or not phase_value.is_integer():
            return {
                "valid": False,
                "pcc_phase": None,
                "pcc_phase_index": None,
                "rotation": None,
                "source": "grid_phase",
                "reason": "grid_phase_invalid",
            }
        phase_number = int(phase_value)
        if phase_number not in (1, 2, 3):
            return {
                "valid": False,
                "pcc_phase": None,
                "pcc_phase_index": None,
                "rotation": None,
                "source": "grid_phase",
                "reason": "grid_phase_invalid",
            }
        phase_index = phase_number - 1

    rotation = None
    if rotation_supplied:
        rotation = normalize_rotation(phase_rotation)
        if rotation is None:
            return {
                "valid": False,
                "pcc_phase": None,
                "pcc_phase_index": None,
                "rotation": None,
                "source": "phase_rotation",
                "reason": "phase_rotation_invalid",
            }

    rotation_phase_index = rotation[0] if rotation is not None else None
    if (
        phase_index is not None
        and rotation_phase_index is not None
        and phase_index != rotation_phase_index
    ):
        return {
            "valid": False,
            "pcc_phase": None,
            "pcc_phase_index": None,
            "rotation": rotation,
            "source": "grid_phase+phase_rotation",
            "reason": "phase_mapping_conflict",
        }

    resolved_index = (
        phase_index if phase_index is not None else rotation_phase_index
    )
    if resolved_index is None:
        return {
            "valid": False,
            "pcc_phase": None,
            "pcc_phase_index": None,
            "rotation": None,
            "source": "none",
            "reason": "phase_mapping_missing",
        }

    source = (
        "grid_phase+phase_rotation"
        if phase_index is not None and rotation is not None
        else ("grid_phase" if phase_index is not None else "phase_rotation")
    )
    return {
        "valid": True,
        "pcc_phase": resolved_index + 1,
        "pcc_phase_index": resolved_index,
        "rotation": rotation,
        "source": source,
        "reason": "phase_mapping_bound",
    }


def openwb_pro_one_phase_current_cap(
    *,
    user_limit_a=None,
    charger_limit_a=32.0,
    grid_max_amps=None,
    grid_max_phase_amps=None,
    reserve_a=2.0,
    reserve_phase_amps=None,
    measured_pcc_a=None,
    data_valid=False,
    data_fresh=False,
    hard_rms_current_measurement=False,
    grid_phase=None,
    phase_rotation=None,
    accepted_amp=0.0,
    accepted_measurement_fresh=False,
    fallback_cap_a=20.0,
    min_amp=6.0,
    require_phase_specific_grid_limit=False,
):
    """Bestimme den sicheren 1p-Stromdeckel für eine openWB Pro.

    Der PCC-Vektor enthält ausschließlich nichtnegative RMS-Strombeträge. Eine
    Stromerhöhung wird konservativ zum gemessenen Betrag addiert; eine
    angeforderte Absenkung wird erst nach einem neuen Messwert als Reserve
    gutgeschrieben. Damit erzeugen weder Einspeiserichtung noch Blindstrom
    künstlichen Headroom.

    Eine dynamische Anhebung über den bisherigen konservativen 20-A-Deckel
    erfordert gleichzeitig:

    * eine explizite, widerspruchsfreie Phasenzuordnung,
    * drei frische und endliche PCC-RMS-Stromwerte,
    * eine frische Wallbox-Strommessung,
    * ein gültiges Hausanschlusslimit und eine nichtnegative Reserve.

    Der aufrufende Hardwarevertrag kann zusätzlich ein ausdrücklich gesetztes
    Limit genau der zugeordneten PCC-Phase verlangen. So darf ein historischer
    globaler Installationsstandard keine Freigabe über 20 A legitimieren.

    Wirkleistung geteilt durch eine Nennspannung ist keine RMS-Strommessung:
    Leistungsfaktor und reale Spannung können den Leiterstrom deutlich erhöhen.
    Fehlt eine dieser Bindungen, bleibt der Deckel beim konservativen Fallback.
    Die Funktion ist eine reine Entscheidung und sendet keine Hardwarebefehle.
    """

    minimum = max(0.0, _finite(min_amp, 6.0))
    fallback = max(0.0, _finite(fallback_cap_a, 20.0))

    try:
        charger_limit = float(charger_limit_a)
    except (TypeError, ValueError):
        charger_limit = 0.0
    if not math.isfinite(charger_limit) or charger_limit <= 0.0:
        charger_limit = 0.0

    if user_limit_a is None or str(user_limit_a).strip() == "":
        user_limit = fallback
        user_limit_source = "fallback"
    else:
        try:
            user_limit = float(user_limit_a)
        except (TypeError, ValueError):
            user_limit = fallback
            user_limit_source = "fallback_invalid"
        else:
            if not math.isfinite(user_limit):
                user_limit = fallback
                user_limit_source = "fallback_invalid"
            else:
                user_limit = max(0.0, user_limit)
                user_limit_source = "user"

    static_limit = min(fallback, user_limit, charger_limit)
    requested_limit = min(user_limit, charger_limit)

    try:
        grid_limit = float(grid_max_amps)
    except (TypeError, ValueError):
        grid_limit = 0.0
    scalar_grid_valid = bool(
        math.isfinite(grid_limit) and grid_limit > 0.0
    )
    try:
        reserve = float(reserve_a)
    except (TypeError, ValueError):
        reserve = 0.0
    scalar_reserve_valid = bool(
        math.isfinite(reserve) and reserve >= 0.0
    )

    def _phase_values(raw, scalar, scalar_valid, *, positive):
        if raw is None:
            return (scalar, scalar, scalar) if scalar_valid else None
        values = _strict_numeric_vector(raw, allow_none=True)
        if values is None:
            return None
        normalized = []
        for value in values:
            if value is None:
                if not scalar_valid:
                    return None
                number = scalar
            else:
                number = value
            if positive and number <= 0.0:
                return None
            if not positive and number < 0.0:
                return None
            normalized.append(number)
        return tuple(normalized)

    mapping = resolve_single_phase_pcc_mapping(
        grid_phase=grid_phase,
        phase_rotation=phase_rotation,
    )
    phase_index = mapping["pcc_phase_index"]
    raw_phase_grid_limits = _strict_numeric_vector(
        grid_max_phase_amps,
        allow_none=True,
    )
    phase_specific_grid_limit = bool(
        phase_index is not None
        and raw_phase_grid_limits is not None
        and raw_phase_grid_limits[phase_index] is not None
    )

    grid_limits = _phase_values(
        grid_max_phase_amps,
        grid_limit,
        scalar_grid_valid,
        positive=True,
    )
    reserves = _phase_values(
        reserve_phase_amps,
        reserve,
        scalar_reserve_valid,
        positive=False,
    )
    grid_contract_valid = bool(
        grid_limits is not None
        and reserves is not None
        and (
            not require_phase_specific_grid_limit
            or phase_specific_grid_limit
        )
        and all(
            reserve_value < limit_value
            for limit_value, reserve_value in zip(grid_limits, reserves)
        )
    )

    def _legal_cap(value):
        cap = max(0.0, math.floor(max(0.0, _finite(value, 0.0))))
        return 0.0 if cap + 1e-9 < minimum else cap

    fallback_cap = _legal_cap(static_limit)
    selected_grid_limit = (
        grid_limits[phase_index]
        if grid_contract_valid and phase_index is not None
        else None
    )
    selected_reserve = (
        reserves[phase_index]
        if grid_contract_valid and phase_index is not None
        else None
    )
    operating_limit = (
        selected_grid_limit - selected_reserve
        if selected_grid_limit is not None and selected_reserve is not None
        else None
    )
    if operating_limit is not None:
        fallback_cap = _legal_cap(min(fallback_cap, operating_limit))
        requested_limit = min(requested_limit, max(0.0, operating_limit))

    result = {
        "cap_amp": fallback_cap,
        "dynamic": False,
        "reason": mapping["reason"],
        "fallback_cap_amp": fallback_cap,
        "user_limit_a": user_limit,
        "user_limit_source": user_limit_source,
        "charger_limit_a": charger_limit,
        "grid_max_amps": selected_grid_limit,
        "grid_max_phase_amps": grid_limits if grid_contract_valid else None,
        "reserve_a": selected_reserve,
        "reserve_phase_amps": reserves if grid_contract_valid else None,
        "operating_limit_a": operating_limit,
        "pcc_phase": mapping["pcc_phase"],
        "pcc_phase_index": mapping["pcc_phase_index"],
        "mapping_source": mapping["source"],
        "phase_specific_grid_limit": phase_specific_grid_limit,
        "phase_specific_grid_limit_required": bool(
            require_phase_specific_grid_limit
        ),
        "measurement_valid": data_valid is True,
        "measurement_fresh": data_fresh is True,
        "hard_rms_current_measurement": (
            hard_rms_current_measurement is True
        ),
        "accepted_measurement_fresh": accepted_measurement_fresh is True,
        "measurement_kind": "nonnegative_rms_magnitude",
        "measured_pcc_a": None,
        "measured_phase_a": None,
        "accepted_amp": None,
        "base_without_wallbox_a": None,
        "available_target_amp": None,
    }

    if not mapping["valid"]:
        return result
    if not grid_contract_valid:
        result["reason"] = "grid_contract_invalid"
        return result
    if data_valid is not True:
        result["reason"] = "pcc_measurement_invalid"
        return result
    if data_fresh is not True:
        result["reason"] = "pcc_measurement_stale"
        return result
    if accepted_measurement_fresh is not True:
        result["reason"] = "wallbox_measurement_stale"
        return result
    if hard_rms_current_measurement is not True:
        result["reason"] = "pcc_rms_current_missing"
        return result

    measured = _strict_numeric_vector(measured_pcc_a)
    if measured is None:
        result["reason"] = "pcc_phase_vector_invalid"
        return result
    if not all(
        math.isfinite(value) and value >= 0.0
        for value in measured
    ):
        result["reason"] = "pcc_phase_vector_invalid"
        return result

    try:
        accepted = float(accepted_amp)
    except (TypeError, ValueError):
        result["reason"] = "wallbox_measurement_invalid"
        return result
    if not math.isfinite(accepted) or accepted < 0.0:
        result["reason"] = "wallbox_measurement_invalid"
        return result

    measured_phase = measured[phase_index]
    additional_headroom = max(0.0, operating_limit - measured_phase)
    available_target = accepted + additional_headroom
    cap = _legal_cap(min(requested_limit, available_target))

    result.update(
        {
            "cap_amp": cap,
            "dynamic": True,
            "reason": (
                "phase_headroom_limit"
                if cap + 1e-9 < _legal_cap(requested_limit)
                else "within_phase_headroom"
            ),
            "measured_pcc_a": measured,
            "measured_phase_a": measured_phase,
            "accepted_amp": accepted,
            "base_without_wallbox_a": None,
            "available_target_amp": available_target,
        }
    )
    return result


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
    compact = _strict_numeric_vector(values)
    if compact is not None and all(value >= 0.0 for value in compact):
        return compact
    if values is not None:
        # Ein vorhandener, aber formal ungültiger Messvektor darf keinen
        # bereits im PCC-Wert enthaltenen Wallboxstrom abziehen.
        return (0.0, 0.0, 0.0)
    return target_local_vector_a(spec.get("accepted_amp", 0.0), spec.get("phases", 1))


def project_pcc_phase_currents(measured_pcc_a, charger_specs, target_amp_by_id):
    """Projiziere Zielströme per Delta gegen den bereits gemessenen PCC-Wert.

    Für unbekannte einphasige Zuordnungen werden alle möglichen physischen
    Phasen ausgewertet. Das Ergebnis enthält den Worst-Case je Phase.
    """

    measured = _strict_numeric_vector(measured_pcc_a)
    if measured is None:
        raise ValueError("pcc_phase_vector_invalid")
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


def project_pcc_phase_rms_upper_bound(
    measured_pcc_rms_a,
    charger_specs,
    target_amp_by_id,
):
    """Projiziere eine konservative RMS-Obergrenze je PCC-Phase.

    RMS-Ströme sind Beträge und werden deshalb niemals als signierte
    Wirkleistungsströme behandelt. Eine Erhöhung des Wallboxstroms wird per
    Dreiecksungleichung vollständig zum gemessenen Leiterstrom addiert. Eine
    angeforderte Absenkung wird erst nach einem neuen Messwert gutgeschrieben.
    So kann Blindstrom oder ein abweichender Phasenwinkel keine künstliche
    Anschlussreserve erzeugen.
    """

    measured = _strict_numeric_vector(measured_pcc_rms_a)
    if measured is None or not all(value >= 0.0 for value in measured):
        raise ValueError("pcc_rms_measurement_invalid")
    specs = [
        spec
        for spec in (charger_specs or [])
        if int(spec.get("id", 0) or 0) > 0
    ]
    option_sets = [_mapping_options(spec) for spec in specs]
    scenarios = []
    for rotations in product(*option_sets) if option_sets else [()]:
        projected = list(measured)
        for spec, rotation in zip(specs, rotations):
            accepted = rotate_local_to_pcc(
                _accepted_local_vector(spec),
                rotation,
            )
            target = rotate_local_to_pcc(
                target_local_vector_a(
                    target_amp_by_id.get(int(spec["id"]), 0.0),
                    spec.get("phases", 1),
                ),
                rotation,
            )
            for phase in range(PHASE_COUNT):
                projected[phase] += max(
                    0.0,
                    target[phase] - accepted[phase],
                )
        scenarios.append(tuple(projected))
    worst = tuple(
        max(scenario[phase] for scenario in scenarios)
        for phase in range(PHASE_COUNT)
    )
    return {
        "worst_case_pcc_a": worst,
        "scenarios": tuple(scenarios),
        "scenario_count": len(scenarios),
        "mapping_complete": all(
            normalize_rotation(spec.get("phase_rotation")) is not None
            or int(spec.get("phases", 1) or 1) >= 3
            for spec in specs
        ),
    }


def clamp_targets_to_phase_limit(
    measured_pcc_a,
    charger_specs,
    proposed_amp_by_id,
    *,
    phase_limit_a,
    phase_limits_a=None,
    data_valid=False,
    data_fresh=True,
    hard_rms_current_measurement=False,
    min_amp=6,
    fairness_weight_by_id=None,
):
    """Deckele gemeinsame Zielströme gegen echte PCC-RMS-Ströme.

    Die Suche ist für die reale kleine Wallboxmenge absichtlich vollständig
    und deterministisch. Sie darf Zielwerte nur verringern, nie erhöhen.
    Fehlende, stale oder nur aus Wirkleistung geschätzte Leiterströme sowie
    ein explizit ungültiger Phasenlimit-Vektor sperren die Ausgabe
    fail-closed.
    """

    specs = [spec for spec in (charger_specs or []) if int(spec.get("id", 0) or 0) > 0]
    ids = [int(spec["id"]) for spec in specs]
    scalar_limit = max(0.0, _finite(phase_limit_a, 0.0))
    phase_limit_vector_invalid = False
    if phase_limits_a is None:
        phase_limits = (scalar_limit, scalar_limit, scalar_limit)
    else:
        raw_phase_limits = _strict_numeric_vector(phase_limits_a)
        if raw_phase_limits is None:
            phase_limits = None
            phase_limit_vector_invalid = True
        else:
            if not all(value >= 0.0 for value in raw_phase_limits):
                phase_limits = None
                phase_limit_vector_invalid = True
            else:
                phase_limits = raw_phase_limits

    measured_rms = _strict_numeric_vector(measured_pcc_a)
    measurement_valid = bool(
        measured_rms is not None
        and all(
            value >= 0.0
            for value in measured_rms
        )
    )

    def _fail_closed(reason):
        return {
            "targets_amp": {charger_id: 0 for charger_id in ids},
            "phase_limit_a": scalar_limit,
            "phase_limits_a": phase_limits,
            "worst_case_pcc_a": (
                measured_rms if measurement_valid else None
            ),
            "mapping_complete": False,
            "scenario_count": 0,
            "data_valid": bool(data_valid),
            "data_fresh": bool(data_fresh),
            "hard_rms_current_measurement": bool(
                hard_rms_current_measurement
            ),
            "limited": any(
                max(
                    0,
                    int(
                        math.floor(
                            _finite(
                                proposed_amp_by_id.get(charger_id),
                                0.0,
                            )
                        )
                    ),
                ) > 0
                for charger_id in ids
            ),
            "reason": reason,
        }

    if phase_limit_vector_invalid:
        return _fail_closed("phase_limit_vector_invalid")
    if data_valid is not True or not measurement_valid:
        return _fail_closed("pcc_rms_measurement_invalid")
    if data_fresh is not True:
        return _fail_closed("pcc_rms_measurement_stale")
    if hard_rms_current_measurement is not True:
        return _fail_closed("pcc_rms_measurement_missing")
    if any(
        spec.get("accepted_measurement_fresh") is not True
        for spec in specs
    ):
        return _fail_closed("wallbox_measurement_stale")

    weights = fairness_weight_by_id if isinstance(fairness_weight_by_id, dict) else {}
    if len(specs) > 2:
        # Das Produkt unterstützt aktuell WB1/WB2. Eine vollständige Suche
        # darf bei einer späteren Erweiterung nicht exponentiell in den
        # Regelzyklus wachsen; unbekannter größerer Scope stoppt fail-closed.
        return {
            "targets_amp": {charger_id: 0 for charger_id in ids},
            "phase_limit_a": scalar_limit,
            "phase_limits_a": phase_limits,
            "worst_case_pcc_a": measured_rms,
            "mapping_complete": False,
            "scenario_count": 0,
            "data_valid": True,
            "data_fresh": True,
            "hard_rms_current_measurement": True,
            "limited": True,
            "reason": "unsupported_more_than_two_chargepoints",
        }
    amp_options = []
    for spec in specs:
        charger_id = int(spec["id"])
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
        if proposed < int(min_amp):
            amp_options.append((0,))
        else:
            amp_options.append((0,) + tuple(range(int(min_amp), proposed + 1)))

    best = None
    best_projection = None
    best_score = None
    for candidate_values in product(*amp_options) if amp_options else [()]:
        candidate = dict(zip(ids, candidate_values))
        projection = project_pcc_phase_rms_upper_bound(
            measured_rms,
            specs,
            candidate,
        )
        if any(
            value > phase_limits[phase] + 1e-9
            for phase, value in enumerate(projection["worst_case_pcc_a"])
        ):
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
        best_projection = project_pcc_phase_rms_upper_bound(
            measured_rms,
            specs,
            best,
        )
    return {
        "targets_amp": best,
        "phase_limit_a": scalar_limit,
        "phase_limits_a": phase_limits,
        "worst_case_pcc_a": best_projection["worst_case_pcc_a"],
        "mapping_complete": best_projection["mapping_complete"],
        "scenario_count": best_projection["scenario_count"],
        "data_valid": True,
        "data_fresh": True,
        "hard_rms_current_measurement": True,
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
    "openwb_pro_one_phase_current_cap",
    "project_pcc_phase_currents",
    "project_pcc_phase_rms_upper_bound",
    "resolve_single_phase_pcc_mapping",
    "rotate_local_to_pcc",
    "target_local_vector_a",
    "vehicle_current_cap_a",
]
