"""Phasenscharfer Vertrag für mehrere Wallboxen.

Der Vertrag trennt drei Größen, die nicht skalar addiert werden dürfen:

* Strom je Netzphase,
* Wirkleistung je Fahrzeug,
* vom Fahrzeug tatsächlich angenommene Energie.

Die Funktionen sind rein und führen keine Geräte- oder Dateizugriffe aus.
"""

from __future__ import annotations

import copy
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


PHASE_MAPPING_PROOF_SCHEMA = "wallbox_phase_mapping_proof_v1"
PHASE_MAPPING_PROOF_STATES = ("pending", "verified", "unverified")
_PHASE_MAPPING_PROOF_VOLATILE_KEYS = (
    "samples",
    "current_plateau",
    "last_plateau",
    "_cycle_token",
)


def pcc_phase_import_current_vector(
    grid_phase_w,
    phase_voltage_v=None,
    *,
    pvi_age_s=None,
    pvi_max_age_s=10.0,
    nominal_voltage_v=230.0,
    voltage_min_v=180.0,
    voltage_max_v=260.0,
    pm_available=True,
):
    """Bezugsstrom je Netzphase aus der Wurzelzähler-Wirkleistung.

    ``P_imp_j = max(0, grid_p_j)`` – Einspeisung zählt nie als Headroom.
    ``I_imp_j = P_imp_j / U_j`` mit der vom Wechselrichter gemessenen
    Phasenspannung, wenn sie endlich, plausibel (``voltage_min_v`` bis
    ``voltage_max_v``) und nicht älter als ``pvi_max_age_s`` ist; sonst gilt
    die Nennspannung (kein Fail-closed: nachts oder im WR-Standby liefert der
    PVI 0 V). ``pm_available=False`` oder ein nicht endlicher Leistungsvektor
    ergeben ``valid=False``. Reine Funktion ohne Geräte- oder Dateizugriff.
    """

    nominal = max(1.0, _finite(nominal_voltage_v, 230.0))
    voltage_min = _finite(voltage_min_v, 180.0)
    voltage_max = _finite(voltage_max_v, 260.0)
    max_age = _finite(pvi_max_age_s, 10.0)
    try:
        age = float(pvi_age_s) if pvi_age_s is not None else None
    except (TypeError, ValueError):
        age = None
    pvi_fresh = bool(age is not None and math.isfinite(age) and age <= max_age)

    raw_voltages = (
        tuple(phase_voltage_v)
        if isinstance(phase_voltage_v, (list, tuple))
        and len(phase_voltage_v) == PHASE_COUNT
        else (None, None, None)
    )
    voltages = []
    sources = []
    for raw in raw_voltages:
        value = None
        if (
            pvi_fresh
            and raw is not None
            and not isinstance(raw, bool)
            and isinstance(raw, Real)
        ):
            number = float(raw)
            if math.isfinite(number) and voltage_min <= number <= voltage_max:
                value = number
        if value is None:
            voltages.append(nominal)
            sources.append("nominal")
        else:
            voltages.append(value)
            sources.append("pvi")

    result = {
        "valid": False,
        "reason": "ok",
        "import_w": None,
        "import_a": None,
        "voltage_v": tuple(voltages),
        "voltage_source": tuple(sources),
        "raw_grid_w": None,
        "pvi_fresh": pvi_fresh,
    }
    if pm_available is not None and not bool(pm_available):
        result["reason"] = "pm_unavailable"
        return result
    grid = _strict_numeric_vector(grid_phase_w)
    if grid is None:
        result["reason"] = "grid_phase_vector_invalid"
        return result
    import_w = tuple(max(0.0, value) for value in grid)
    import_a = tuple(
        power / voltage for power, voltage in zip(import_w, voltages)
    )
    result.update(
        {
            "valid": True,
            "reason": "ok",
            "import_w": import_w,
            "import_a": import_a,
            "raw_grid_w": grid,
        }
    )
    return result


def one_phase_cap_ramp(
    previous,
    raw_cap_amp,
    *,
    now_ts,
    dynamic=True,
    headroom_a=None,
    fallback_cap_amp=20.0,
    step_a=1.0,
    hold_s=4.0,
    reduce_hold_factor=3.0,
    deadband_a=1.5,
    min_amp=6.0,
    reset=False,
    reset_reason=None,
):
    """Asymmetrische Rampe des 1p-Deckels.

    Anhebung höchstens ``step_a`` je ``hold_s`` und nur, wenn der Headroom
    mindestens ``deadband_a`` beträgt; Absenkung sofort, danach Nachlauf
    ``reduce_hold_factor * hold_s`` ohne Anhebung. Ein Reset (neuer Zustand,
    ausdrücklich, oder nach einem Fail-closed-Zyklus) startet bei
    ``fallback_cap_amp``. Unterhalb ``min_amp`` gilt 0 A; eine Anhebung aus 0 A
    springt direkt auf ``min_amp`` (ein Deckel unter dem Mindeststrom hält den
    Ausgang ohnehin auf 0 A). Ganze Ampere; reine Funktion.
    """

    now = _finite(now_ts, 0.0)
    minimum = max(0.0, _finite(min_amp, 6.0))
    raw = max(0.0, _finite(raw_cap_amp, 0.0))
    if raw + 1e-9 < minimum:
        raw = 0.0
    fallback = max(0.0, _finite(fallback_cap_amp, 20.0))
    step = max(0.0, _finite(step_a, 1.0))
    hold = max(0.0, _finite(hold_s, 4.0))
    reduce_hold = hold * max(0.0, _finite(reduce_hold_factor, 3.0))
    deadband = max(0.0, _finite(deadband_a, 1.5))
    prev = previous if isinstance(previous, dict) else None
    prev_cap = _finite(prev.get("cap_amp"), fallback) if prev else None

    if dynamic is not True:
        return {
            "cap_amp": raw,
            "previous_cap_amp": prev_cap,
            "raw_cap_amp": raw,
            "direction": "fail_closed",
            "limited_by_ramp": False,
            "last_raise_ts": 0.0,
            "last_reduce_ts": 0.0,
            "hold_until_ts": None,
            "hold_s": hold,
            "dynamic_prev": False,
            "reset_reason": reset_reason,
        }

    reason = reset_reason
    if prev is None:
        do_reset = True
        reason = reason or "no_state"
    elif reset:
        do_reset = True
        reason = reason or "reset"
    elif prev.get("dynamic_prev") is not True:
        do_reset = True
        reason = reason or "fail_closed_recovery"
    else:
        do_reset = False
        reason = None

    if do_reset:
        base = fallback
        last_raise = 0.0
        last_reduce = 0.0
    else:
        base = max(0.0, _finite(prev_cap, fallback))
        last_raise = _finite(prev.get("last_raise_ts"), 0.0)
        last_reduce = _finite(prev.get("last_reduce_ts"), 0.0)

    hold_until = None
    limited = False
    if raw + 1e-9 < base:
        cap = raw
        last_reduce = now
        direction = "reduce"
    elif abs(raw - base) <= 1e-9:
        cap = base
        direction = "hold"
    else:
        raise_ready = (now - last_raise) >= hold
        reduce_ready = (now - last_reduce) >= reduce_hold
        headroom_ok = headroom_a is None or _finite(headroom_a, 0.0) >= deadband
        if raise_ready and reduce_ready and headroom_ok:
            cap = min(raw, max(base + step, minimum))
            last_raise = now
            direction = "raise"
            limited = cap + 1e-9 < raw
        else:
            cap = base
            direction = "hold"
            limited = True
            if not (raise_ready and reduce_ready):
                hold_until = max(last_raise + hold, last_reduce + reduce_hold)
    if do_reset:
        direction = "reset"
    return {
        "cap_amp": float(cap),
        "previous_cap_amp": prev_cap,
        "raw_cap_amp": raw,
        "direction": direction,
        "limited_by_ramp": bool(limited),
        "last_raise_ts": float(last_raise),
        "last_reduce_ts": float(last_reduce),
        "hold_until_ts": hold_until,
        "hold_s": hold,
        "dynamic_prev": True,
        "reset_reason": reason,
    }


def phase_mapping_proof_persistent(state):
    """Persistenter Teil des Zuordnungs-Nachweises (ohne Fenster/Plateaus)."""

    if not isinstance(state, dict):
        return None
    return {
        key: copy.deepcopy(value)
        for key, value in state.items()
        if key not in _PHASE_MAPPING_PROOF_VOLATILE_KEYS
    }


def _proof_int(value):
    if value is None:
        return None
    text = str(value).strip()
    if text == "":
        return None
    try:
        number = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or not number.is_integer():
        return None
    return int(number)


def _proof_text(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _proof_fresh_state(now_ts, grid_phase, rotation, pcc_phase_index, reason):
    return {
        "state": "pending",
        "reason": reason,
        "configured_grid_phase": grid_phase,
        "configured_rotation": rotation,
        "pcc_phase_index": pcc_phase_index,
        "observed_phase": None,
        "hint": None,
        "static": {
            "state": "none",
            "ratio": None,
            "ts": None,
            "mismatch_since": None,
            "carrier_phase": None,
        },
        "steps": [],
        "evidence": {
            "consistent_weight": 0,
            "contradicting_weight": 0,
            "ambiguous_count": 0,
        },
        "verified_ts": None,
        "unverified_ts": None,
        "plug_session_id": None,
        "updated_ts": now_ts,
        "changed": True,
        "persist_now": False,
        "samples": [],
        "current_plateau": None,
        "last_plateau": None,
    }


def _proof_normalize_sample(sample):
    if not isinstance(sample, dict):
        return None
    ts = _finite(sample.get("ts"), float("nan"))
    amp = _finite(sample.get("wallbox_amp"), float("nan"))
    if not math.isfinite(ts) or not math.isfinite(amp) or amp < 0.0:
        return None
    load = _strict_numeric_vector(sample.get("load_w"))
    volt = _strict_numeric_vector(sample.get("voltage_v"))
    if load is None or volt is None or any(value <= 0.0 for value in volt):
        return None
    try:
        active = int(sample.get("active_phases") or 0)
    except (TypeError, ValueError):
        active = 0
    return {
        "ts": ts,
        "wallbox_amp": amp,
        "active_phases": max(0, min(3, active)),
        "load_w": tuple(max(0.0, value) for value in load),
        "voltage_v": volt,
        "plug_session_id": _proof_text(sample.get("plug_session_id")),
    }


def _proof_plateau_means(plateau, window_s):
    recent = plateau.get("recent") or []
    if not recent:
        return None
    t_end = _finite(plateau.get("t_end"), 0.0)
    used = [item for item in recent if t_end - float(item["ts"]) <= window_s] or recent
    count = len(used)
    return {
        "t_start": _finite(plateau.get("t_start"), 0.0),
        "t_end": t_end,
        "n": int(plateau.get("n", count) or count),
        "amp": sum(float(item["wallbox_amp"]) for item in used) / count,
        "load_w": tuple(
            sum(float(item["load_w"][j]) for item in used) / count
            for j in range(PHASE_COUNT)
        ),
        "voltage_v": tuple(
            sum(float(item["voltage_v"][j]) for item in used) / count
            for j in range(PHASE_COUNT)
        ),
    }


def _proof_close_current(state, window_s):
    current = state.get("current_plateau")
    state["current_plateau"] = None
    if isinstance(current, dict) and current.get("complete"):
        means = _proof_plateau_means(current, window_s)
        if means is not None:
            means["consumed"] = False
            state["last_plateau"] = means


def _proof_classify_step(
    plateau_a,
    plateau_b,
    pcc_phase_index,
    *,
    step_ratio_min,
    step_ratio_max,
    contradict_ratio_max,
    foreign_ratio,
):
    delta = float(plateau_b["amp"]) - float(plateau_a["amp"])
    ratios = []
    for j in range(PHASE_COUNT):
        denominator = delta * float(plateau_b["voltage_v"][j])
        if abs(denominator) <= 1e-9:
            ratios.append(0.0)
        else:
            ratios.append(
                (float(plateau_b["load_w"][j]) - float(plateau_a["load_w"][j]))
                / denominator
            )
    others = [j for j in range(PHASE_COUNT) if j != pcc_phase_index]
    ratio_k = ratios[pcc_phase_index]
    if step_ratio_min <= ratio_k <= step_ratio_max and all(
        abs(ratios[j]) < foreign_ratio for j in others
    ):
        return "consistent", ratios, None
    carriers = [
        j for j in others if step_ratio_min <= ratios[j] <= step_ratio_max
    ]
    if (
        ratio_k < contradict_ratio_max
        and len(carriers) == 1
        and all(
            abs(ratios[j]) < foreign_ratio
            for j in others
            if j != carriers[0]
        )
    ):
        return "contradicting", ratios, carriers[0] + 1
    return "ambiguous", ratios, None


def phase_mapping_proof_update(
    state,
    sample,
    *,
    now_ts,
    configured_grid_phase,
    configured_rotation,
    pcc_phase_index,
    plug_session_id=None,
    charger_id=None,
    min_static_current_a=8.0,
    idle_current_a=0.5,
    plateau_samples=3,
    plateau_span_s=10.0,
    plateau_tolerance_a=0.5,
    window_s=45.0,
    step_min_a=3.0,
    strong_step_a=10.0,
    step_gap_s=120.0,
    static_ok_ratio=0.8,
    static_fail_ratio=0.5,
    static_fail_hold_s=60.0,
    step_ratio_min=0.6,
    step_ratio_max=1.4,
    contradict_ratio_max=0.3,
    foreign_ratio=0.5,
    evidence_required=2,
    max_steps=5,
):
    """Software-Nachweis der Phasenzuordnung (Zustandsmaschine).

    Stichprobe je Zyklus: Wallbox-Strom, aktive Treiberphasen, Phasenlast
    ``P_load_j = max(0, grid_p_j + ac_j_w)`` (signierte Bilanz am Netzknoten)
    und Phasenspannung. Plateaus (≥ ``plateau_samples`` Stichproben über
    ≥ ``plateau_span_s`` mit Spannweite ≤ ``plateau_tolerance_a``) liefern den
    Statiktest ``P_k / (I·U_k)`` und – zwischen zwei aufeinanderfolgenden
    Plateaus – den Sprungtest ``ΔP_j / (ΔI·U_j)``. Sprünge ≥ ``strong_step_a``
    zählen doppelt. ``verified`` verlangt Statik ok und konsistente Sprünge mit
    Gewicht ≥ ``evidence_required`` sowie mehr als das Doppelte der
    Widerspruchsgewichte; ``unverified`` (sticky bis Konfigänderung) entsteht
    nur aus Widersprüchen auf ein und derselben Fremdphase. Ein reiner
    Statik-Fehlschlag bleibt ``pending`` (externer AC-Zusatz-WR). Reine
    Zustandsmaschine ohne I/O und ohne Zeitabfrage.
    """

    now = float(now_ts)
    grid_phase = _proof_int(configured_grid_phase)
    rotation = _proof_text(configured_rotation)
    phase_index = _proof_int(pcc_phase_index)
    if phase_index is not None and phase_index not in (0, 1, 2):
        phase_index = None

    reset_now = False
    fresh_now = False
    if not isinstance(state, dict):
        st = _proof_fresh_state(now, grid_phase, rotation, phase_index, "no_sample")
        fresh_now = True
    else:
        st = copy.deepcopy(state)
        if (
            _proof_int(st.get("configured_grid_phase")) != grid_phase
            or _proof_text(st.get("configured_rotation")) != rotation
            or not isinstance(st.get("static"), dict)
            or not isinstance(st.get("evidence"), dict)
            or not isinstance(st.get("steps"), list)
            or st.get("state") not in PHASE_MAPPING_PROOF_STATES
        ):
            st = _proof_fresh_state(now, grid_phase, rotation, phase_index, "config_changed")
            reset_now = True
    st["pcc_phase_index"] = phase_index
    st.setdefault("samples", [])
    st.setdefault("current_plateau", None)
    st.setdefault("last_plateau", None)
    if plug_session_id is not None:
        st["plug_session_id"] = _proof_text(plug_session_id)
    before = phase_mapping_proof_persistent(st)
    for key in ("updated_ts", "changed", "persist_now"):
        before.pop(key, None)
    prev_state = st["state"]
    static = st["static"]
    steps = st["steps"]
    if len(steps) > int(max_steps):
        del steps[:-int(max_steps)]
    evidence = st["evidence"]

    normalized = _proof_normalize_sample(sample)
    sample_kind = "ok"
    new_step = False
    ambiguous_now = False
    current_means = None
    if normalized is None or phase_index is None:
        _proof_close_current(st, window_s)
        st["samples"] = []
        static["mismatch_since"] = None
        sample_kind = "none"
    elif normalized["active_phases"] >= 2:
        _proof_close_current(st, window_s)
        st["samples"] = []
        static["mismatch_since"] = None
        sample_kind = "three_phase"
    else:
        samples = [
            item
            for item in (st.get("samples") or [])
            if isinstance(item, dict) and now - _finite(item.get("ts"), now) <= window_s
        ]
        samples.append(normalized)
        st["samples"] = samples
        current = st.get("current_plateau")
        amp = normalized["wallbox_amp"]
        if (
            isinstance(current, dict)
            and max(_finite(current.get("amp_max"), amp), amp)
            - min(_finite(current.get("amp_min"), amp), amp)
            <= plateau_tolerance_a
        ):
            current["t_end"] = normalized["ts"]
            current["n"] = int(current.get("n", 0) or 0) + 1
            current["amp_min"] = min(_finite(current.get("amp_min"), amp), amp)
            current["amp_max"] = max(_finite(current.get("amp_max"), amp), amp)
            recent = [
                item
                for item in (current.get("recent") or [])
                if normalized["ts"] - _finite(item.get("ts"), normalized["ts"]) <= window_s
            ]
            recent.append(normalized)
            current["recent"] = recent
            current["all_one_phase"] = bool(
                current.get("all_one_phase", True) and normalized["active_phases"] == 1
            )
        else:
            _proof_close_current(st, window_s)
            current = {
                "t_start": normalized["ts"],
                "t_end": normalized["ts"],
                "n": 1,
                "amp_min": amp,
                "amp_max": amp,
                "recent": [normalized],
                "all_one_phase": normalized["active_phases"] == 1,
                "complete": False,
                "step_checked": False,
            }
            st["current_plateau"] = current
        current_means = _proof_plateau_means(current, window_s)
        span_ok = bool(
            int(current.get("n", 0) or 0) >= int(plateau_samples)
            and (current["t_end"] - current["t_start"]) >= plateau_span_s
        )
        kind_ok = bool(
            current_means["amp"] <= idle_current_a or current.get("all_one_phase", False)
        )
        current["complete"] = bool(span_ok and kind_ok)
        if current["complete"] and not current.get("step_checked"):
            last = st.get("last_plateau")
            if isinstance(last, dict) and not last.get("consumed"):
                delta = current_means["amp"] - _finite(last.get("amp"), 0.0)
                gap = current["t_start"] - _finite(last.get("t_end"), 0.0)
                if abs(delta) >= step_min_a and gap <= step_gap_s:
                    kind, ratios, observed = _proof_classify_step(
                        last,
                        current_means,
                        phase_index,
                        step_ratio_min=step_ratio_min,
                        step_ratio_max=step_ratio_max,
                        contradict_ratio_max=contradict_ratio_max,
                        foreign_ratio=foreign_ratio,
                    )
                    if kind == "ambiguous":
                        evidence["ambiguous_count"] = int(evidence.get("ambiguous_count", 0) or 0) + 1
                        ambiguous_now = True
                    else:
                        steps.append(
                            {
                                "ts": current["t_start"],
                                "delta_a": round(delta, 3),
                                "ratio": [round(value, 3) for value in ratios],
                                "kind": kind,
                                "weight": 2 if abs(delta) >= strong_step_a else 1,
                                "observed_phase": observed if kind == "contradicting" else phase_index + 1,
                                "plug_session_id": normalized.get("plug_session_id"),
                            }
                        )
                        del steps[:-int(max_steps)]
                        new_step = True
                    last["consumed"] = True
            current["step_checked"] = True
        if current["complete"] and current_means["amp"] >= min_static_current_a:
            voltage_k = float(current_means["voltage_v"][phase_index])
            denominator = current_means["amp"] * voltage_k
            ratio = (
                float(current_means["load_w"][phase_index]) / denominator
                if denominator > 0.0
                else 0.0
            )
            static["ratio"] = round(ratio, 3)
            static["ts"] = now
            if ratio >= static_ok_ratio:
                static["state"] = "ok"
                static["mismatch_since"] = None
                static["carrier_phase"] = None
            elif ratio < static_fail_ratio:
                if static.get("mismatch_since") is None:
                    static["mismatch_since"] = now
                if now - _finite(static.get("mismatch_since"), now) >= static_fail_hold_s:
                    static["state"] = "mismatch"
                    others = [j for j in range(PHASE_COUNT) if j != phase_index]
                    carrier = max(others, key=lambda j: float(current_means["load_w"][j]))
                    carrier_denominator = current_means["amp"] * float(current_means["voltage_v"][carrier])
                    carrier_ratio = (
                        float(current_means["load_w"][carrier]) / carrier_denominator
                        if carrier_denominator > 0.0
                        else 0.0
                    )
                    static["carrier_phase"] = carrier + 1 if carrier_ratio >= static_ok_ratio else None
            else:
                static["mismatch_since"] = None
        elif current["complete"]:
            static["mismatch_since"] = None

    consistent_weight = sum(
        int(item.get("weight", 1) or 1) for item in steps if item.get("kind") == "consistent"
    )
    contradicting_weight = sum(
        int(item.get("weight", 1) or 1) for item in steps if item.get("kind") == "contradicting"
    )
    contradicting_phases = {
        int(item.get("observed_phase") or 0)
        for item in steps
        if item.get("kind") == "contradicting" and item.get("observed_phase")
    }
    evidence["consistent_weight"] = int(consistent_weight)
    evidence["contradicting_weight"] = int(contradicting_weight)
    evidence.setdefault("ambiguous_count", 0)

    charger_text = "" if charger_id is None else str(charger_id)
    verified_ok = bool(
        static.get("state") == "ok"
        and consistent_weight >= int(evidence_required)
        and consistent_weight > 2 * contradicting_weight
    )
    unverified_ok = bool(
        consistent_weight == 0
        and len(contradicting_phases) == 1
        and (
            contradicting_weight >= int(evidence_required)
            or (
                contradicting_weight >= 1
                and static.get("state") == "mismatch"
                and static.get("carrier_phase") in contradicting_phases
            )
        )
    )
    if prev_state == "unverified":
        new_state = "unverified"
    elif unverified_ok:
        new_state = "unverified"
    elif verified_ok:
        new_state = "verified"
    elif prev_state == "verified":
        if contradicting_weight > 0 and consistent_weight <= 2 * contradicting_weight:
            new_state = "pending"
            st["reason"] = "mixed_evidence"
        elif static.get("state") == "mismatch":
            new_state = "pending"
            st["reason"] = "static_mismatch"
        else:
            new_state = "verified"
    else:
        new_state = "pending"

    if new_state == "unverified":
        if prev_state != "unverified":
            observed = next(iter(contradicting_phases)) if contradicting_phases else None
            st["observed_phase"] = observed
            st["hint"] = (
                "Phasenzuordnung WB%s stimmt nicht: Last auf L%s statt L%d"
                % (charger_text, observed if observed else "?", phase_index + 1 if phase_index is not None else 0)
            )
            st["unverified_ts"] = now
        st["reason"] = "contradicting_evidence"
    elif new_state == "verified":
        if prev_state != "verified":
            st["verified_ts"] = now
        st["observed_phase"] = phase_index + 1 if phase_index is not None else None
        st["hint"] = None
        st["reason"] = "verified"
    else:
        st["hint"] = None
        st["observed_phase"] = None
        if prev_state == "verified":
            pass
        elif reset_now:
            pass
        elif sample_kind == "none":
            st["reason"] = "no_sample"
        elif sample_kind == "three_phase":
            st["reason"] = "three_phase"
        elif ambiguous_now:
            st["reason"] = "ambiguous_step"
        elif consistent_weight > 0 and contradicting_weight > 0:
            st["reason"] = "mixed_evidence"
        elif static.get("state") == "mismatch":
            st["reason"] = "static_mismatch"
        elif not (isinstance(st.get("current_plateau"), dict) and st["current_plateau"].get("complete")):
            st["reason"] = "no_plateau"
        elif current_means is not None and idle_current_a < current_means["amp"] < min_static_current_a:
            st["reason"] = "current_below_8a"
        else:
            st["reason"] = "insufficient_steps"
    st["state"] = new_state
    st["updated_ts"] = now

    after = phase_mapping_proof_persistent(st)
    for key in ("updated_ts", "changed", "persist_now"):
        after.pop(key, None)
    st["changed"] = bool(reset_now or fresh_now or before != after)
    st["persist_now"] = bool(reset_now or new_state != prev_state or new_step)
    return st


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
    measurement_basis=None,
    grid_limit_explicit=False,
    power_factor_margin=0.9,
    imbalance_max_a=20.0,
    mapping_state=None,
    phase_voltage_v=None,
    voltage_source=None,
    meter_mismatch=False,
    legal_cap_epsilon_a=1e-6,
):
    """Bestimme den sicheren 1p-Stromdeckel für eine openWB Pro.

    Messbasis ist der Bezugsstrom je
    Netzphase aus dem E3DC-Wurzelzähler (Wirkleistung je Phase ÷ vom
    Wechselrichter gemessene Phasenspannung, sonst Nennspannung), siehe
    ``pcc_phase_import_current_vector``. Der PCC-Vektor enthält ausschließlich
    nichtnegative Strombeträge; Einspeisung zählt nie als Headroom.

    Formeln (k = zugeordnete PCC-Phase, m = Bezugsvektor, I_wb = gemessener
    Wallbox-Strom, PF = Leistungsfaktor-Reserve nur auf den Fremdanteil):

    * ``I_fremd = max(0, m_k − I_wb)``, ``I_pcc_eff = min(m_k, I_wb) + I_fremd / PF``
    * ``H_k = (L_k − R_k) − I_pcc_eff`` (vorzeichenbehaftet: bei Überlast fällt
      der Deckel sofort unter den Ist-Strom), ``cap_sich = I_wb + H_k``
    * Schieflast-Wächter ``cap_imb = max(fallback, I_wb + (I_imb_max − I_imb))``
      mit ``I_imb = m_k − min_{j≠k} m_j``; der Floor entspricht dem heutigen
      statischen Deckel ohne Wächter.
    * ``cap = floor(min(Nutzer, Wallbox, L_k − R_k, cap_sich, cap_imb) + ε)``,
      unter ``min_amp`` → 0.

    Eine dynamische Freigabe über den konservativen Fallback erfordert
    gleichzeitig: Messbasis nicht ``off``, eine explizite, widerspruchsfreie
    Phasenzuordnung, ein ausdrücklich eingetragenes Hausanschlusslimit (Skalar
    oder Phase; der historische Standard 35 A zählt nicht), einen bestätigten
    Zuordnungs-Nachweis (``mapping_state`` ``verified`` oder nicht erzwungen),
    keinen Wurzelzähler-Widerspruch sowie gültige und frische Netzphasen- und
    Wallbox-Messwerte. Fehlt eine dieser Bindungen, bleibt der Deckel beim
    Fallback (fail-closed). Blindstrom und Spannungsabweichungen deckt die
    PF-Reserve auf den Fremdanteil ab; der Wallbox-Anteil ist ein echter
    Phasenstrom der openWB Pro. ``hard_rms_current_measurement=True`` wählt die
    Basis ``pcc_rms_current`` (PF 1,0). Die Funktion ist eine reine
    Entscheidung und sendet keine Hardwarebefehle.
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

    if grid_max_amps is None or str(grid_max_amps).strip() == "":
        grid_limit = 35.0
    else:
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
    # Ein ausdrücklich eingetragener Skalar (grid_max_amps) erfüllt die
    # Explizit-Bedingung ebenso wie ein Phasenlimit; nur der stille Standard 35 A
    # bleibt ausgeschlossen (grid_limit_not_explicit).
    grid_limit_explicit_ok = bool(
        phase_specific_grid_limit or grid_limit_explicit is True
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
    grid_values_valid = bool(
        grid_limits is not None
        and reserves is not None
        and all(
            reserve_value < limit_value
            for limit_value, reserve_value in zip(grid_limits, reserves)
        )
    )
    grid_contract_valid = bool(
        grid_values_valid
        and (
            not require_phase_specific_grid_limit
            or grid_limit_explicit_ok
        )
    )

    if hard_rms_current_measurement is True:
        basis = "pcc_rms_current"
    elif measurement_basis is None:
        basis = None
    else:
        basis = str(measurement_basis).strip().lower() or None
        if basis not in ("e3dc_pm_active_power", "pcc_rms_current", "off"):
            basis = "off"
    if basis == "pcc_rms_current":
        power_factor = 1.0
    else:
        power_factor = min(1.0, max(0.8, _finite(power_factor_margin, 0.9)))
    imbalance_limit = min(32.0, max(10.0, _finite(imbalance_max_a, 20.0)))
    mapping_state_text = (
        None
        if mapping_state is None
        else (str(mapping_state).strip().lower() or "pending")
    )
    if mapping_state_text not in (None, "verified", "pending", "unverified"):
        mapping_state_text = "pending"
    voltage_vector = _strict_numeric_vector(phase_voltage_v)
    voltage_source_vector = (
        tuple(str(item) for item in voltage_source)
        if isinstance(voltage_source, (list, tuple))
        and len(voltage_source) == PHASE_COUNT
        else None
    )

    epsilon = _finite(legal_cap_epsilon_a, 1e-6)
    if epsilon < 0.0:
        epsilon = 1e-6

    def _legal_cap(value):
        cap = max(0.0, float(math.floor(max(0.0, _finite(value, 0.0)) + epsilon)))
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
        "raw_cap_amp": fallback_cap,
        "dynamic": False,
        "reason": mapping["reason"],
        "binding_limit": None,
        "basis": basis,
        "fallback_cap_amp": fallback_cap,
        "user_limit_a": user_limit,
        "user_limit_source": user_limit_source,
        "charger_limit_a": charger_limit,
        "grid_max_amps": selected_grid_limit,
        "grid_max_phase_amps": grid_limits if grid_contract_valid else None,
        "grid_limit_explicit": grid_limit_explicit_ok,
        "reserve_a": selected_reserve,
        "reserve_phase_amps": reserves if grid_contract_valid else None,
        "operating_limit_a": operating_limit,
        "pcc_phase": mapping["pcc_phase"],
        "pcc_phase_index": mapping["pcc_phase_index"],
        "mapping_source": mapping["source"],
        "mapping_state": mapping_state_text,
        "phase_specific_grid_limit": phase_specific_grid_limit,
        "phase_specific_grid_limit_required": bool(
            require_phase_specific_grid_limit
        ),
        "measurement_valid": data_valid is True,
        "measurement_fresh": data_fresh is True,
        "meter_mismatch": meter_mismatch is True,
        "hard_rms_current_measurement": (
            hard_rms_current_measurement is True
        ),
        "accepted_measurement_fresh": accepted_measurement_fresh is True,
        "measurement_kind": (
            "pcc_import_active_power_over_voltage"
            if basis == "e3dc_pm_active_power"
            else "nonnegative_rms_magnitude"
        ),
        "measured_pcc_a": None,
        "measured_import_w": None,
        "measured_phase_a": None,
        "phase_voltage_v": voltage_vector,
        "voltage_source": voltage_source_vector,
        "accepted_amp": None,
        "wallbox_pcc_share_a": None,
        "foreign_load_a": None,
        "foreign_load_margin_a": None,
        "pcc_effective_a": None,
        "headroom_a": None,
        "imbalance_a": None,
        "imbalance_max_a": imbalance_limit,
        "imbalance_excess_a": None,
        "power_factor_margin": power_factor,
        "base_without_wallbox_a": None,
        "available_target_amp": None,
    }

    # Prüfkette (jeder Abbruch: cap = Fallback, dynamic False).
    if basis == "off":
        result["reason"] = "pcc_basis_off"
        return result
    if not mapping["valid"]:
        return result
    if not grid_values_valid:
        result["reason"] = "grid_contract_invalid"
        return result
    if not grid_contract_valid:
        result["reason"] = "grid_limit_not_explicit"
        return result
    if mapping_state_text == "pending":
        result["reason"] = "mapping_pending"
        return result
    if mapping_state_text == "unverified":
        result["reason"] = "mapping_unverified"
        return result
    if meter_mismatch is True:
        result["reason"] = "pcc_meter_mismatch"
        return result
    if basis is None:
        result["reason"] = "pcc_measurement_basis_missing"
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
    wallbox_share = min(measured_phase, accepted)
    foreign_load = max(0.0, measured_phase - accepted)
    foreign_margin = foreign_load / power_factor
    pcc_effective = wallbox_share + foreign_margin
    headroom = operating_limit - pcc_effective
    cap_safety = accepted + headroom
    other_min = min(
        value for index, value in enumerate(measured) if index != phase_index
    )
    imbalance = measured_phase - other_min
    imbalance_excess = max(0.0, imbalance - imbalance_limit)
    cap_imbalance = max(fallback_cap, accepted + (imbalance_limit - imbalance))
    cap = _legal_cap(min(requested_limit, cap_safety, cap_imbalance))
    legal_requested = _legal_cap(requested_limit)

    binding = None
    if cap + 1e-9 >= legal_requested:
        reason = "within_phase_headroom"
        for name, value in (
            ("user_limit", user_limit),
            ("charger_limit", charger_limit),
            ("operating_limit", max(0.0, operating_limit)),
        ):
            if abs(value - requested_limit) <= 1e-9:
                binding = name
                break
    elif cap <= 0.0:
        reason = "phase_headroom_exhausted"
        binding = "phase_headroom"
    elif cap_imbalance + 1e-9 < cap_safety:
        reason = "imbalance_limit"
        binding = "imbalance"
    else:
        reason = "phase_headroom_limit"
        binding = "phase_headroom"

    result.update(
        {
            "cap_amp": cap,
            "raw_cap_amp": cap,
            "dynamic": True,
            "reason": reason,
            "binding_limit": binding,
            "measured_pcc_a": measured,
            "measured_import_w": tuple(
                value * (
                    voltage_vector[index]
                    if voltage_vector is not None
                    else 230.0
                )
                for index, value in enumerate(measured)
            ),
            "measured_phase_a": measured_phase,
            "accepted_amp": accepted,
            "wallbox_pcc_share_a": wallbox_share,
            "foreign_load_a": foreign_load,
            "foreign_load_margin_a": foreign_margin,
            "pcc_effective_a": pcc_effective,
            "headroom_a": headroom,
            "imbalance_a": imbalance,
            "imbalance_excess_a": imbalance_excess,
            "base_without_wallbox_a": foreign_load,
            "available_target_amp": cap_safety,
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
