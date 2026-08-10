#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemeinsame Aktionsmatrix der kanonischen Direktvermarktung.

Producer, Planprojektion, Arbitrierung und Executor dürfen die Zuordnung nicht
jeweils separat fortschreiben. Eine aktive Policy ist nur dann ausführbar,
wenn alle Stufen denselben Eintrag aus dieser Matrix verwenden.
"""

from __future__ import annotations

import copy
import hashlib
import json
import math
from typing import Any, Dict, Optional


DIRECT_MARKETING_EXPORT_START_GATE_SCHEMA = "export_window_start_gate_v1"
DIRECT_MARKETING_POLICY_SCHEMA = "direct_marketing_policy_v1"
DIRECT_MARKETING_EXPORT_GATE_LINEAGE_SCHEMA = "export_window_gate_lineage_v1"
DIRECT_MARKETING_EXPORT_GATE_LINEAGE_STATES = frozenset(
    {"ACTIVE", "SUSPENDED", "REVOKED"}
)
DIRECT_MARKETING_EXPORT_GATE_LINEAGE_EFFECT_CONTRACT = (
    "STATUS_ONLY_NO_EXECUTION_AUTHORITY"
)
DIRECT_MARKETING_EXPORT_PROFIT_PROFILES = frozenset(
    {"standard", "aggressive", "expert"}
)


# Ein gemeinsamer Wirkungsvertrag für alle kanonischen Speicheraktionen. Die
# Stufen dürfen daraus Teilmengen für Validierung und Übersetzung ableiten, aber
# weder Richtung noch Hardwarewirkung erneut erraten. Insbesondere ist
# HOUSE_SUPPLY kein Soll-Entladebefehl: E3/DC-AUTO versorgt das Haus innerhalb
# der Reserve selbst; E3DC-Control setzt dafür ausschließlich den Laderahmen
# auf 0 W.
STORAGE_ACTION_CONTRACTS: Dict[str, Dict[str, Any]] = {
    "HOLD": {
        "direction": "hold",
        "effect": "NO_HARDWARE_EFFECT",
        "manager_action": "HOLD",
        "canonical_active": False,
        "phase5_command": False,
        "field_released": True,
    },
    "CHARGE_BLOCK_WAIT": {
        "direction": "hold",
        "effect": "AUTO_CHARGE_CAP",
        "manager_action": "DIRECT_MARKETING_CHARGE_BLOCK_WAIT",
        "canonical_active": True,
        "phase5_command": True,
        "field_released": True,
    },
    "PV_STORE": {
        "direction": "charge",
        "effect": "AUTO_CHARGE_CAP",
        "manager_action": "PV_STORE",
        "canonical_active": True,
        "phase5_command": True,
        "field_released": True,
    },
    "GRID_CHARGE": {
        "direction": "charge",
        "effect": "GRID_CHARGE",
        "manager_action": "GRID_CHARGE",
        "canonical_active": True,
        "phase5_command": True,
        "field_released": False,
    },
    "HOUSE_SUPPLY": {
        "direction": "hold",
        "effect": "AUTO_CHARGE_CAP",
        "manager_action": "HOUSE_SUPPLY",
        "canonical_active": True,
        "phase5_command": False,
        "field_released": True,
        "requested_power_w": 0,
        "max_charge_w": 0,
        "discharge_contract": "RESERVE_AND_HARDWARE_BOUND_AUTO",
    },
    "ECONOMIC_EXPORT": {
        "direction": "discharge",
        "effect": "SET_POWER_DISCHARGE",
        "manager_action": "ECONOMIC_EXPORT",
        "canonical_active": True,
        "phase5_command": True,
        "field_released": True,
    },
    "HEADROOM_EXPORT": {
        "direction": "discharge",
        "effect": "SET_POWER_DISCHARGE",
        "manager_action": "HEADROOM_EXPORT",
        "canonical_active": True,
        "phase5_command": True,
        "field_released": False,
    },
}


def storage_action_contract(action: Any) -> Optional[Dict[str, Any]]:
    """Liefert eine defensive Kopie des gemeinsamen Speichervertrags."""

    contract = STORAGE_ACTION_CONTRACTS.get(
        str(action or "HOLD").strip().upper()
    )
    return copy.deepcopy(contract) if contract is not None else None


def storage_action_transition_contract(
    previous_action: Any,
    next_action: Any,
) -> Dict[str, Any]:
    """Typisiert jedes geordnete Aktionspaar ohne Regelungsneuberechnung."""

    previous = str(previous_action or "HOLD").strip().upper()
    next_value = str(next_action or "HOLD").strip().upper()
    previous_contract = STORAGE_ACTION_CONTRACTS.get(previous)
    next_contract = STORAGE_ACTION_CONTRACTS.get(next_value)
    covered = bool(previous_contract is not None and next_contract is not None)
    released = bool(
        covered
        and previous_contract.get("field_released") is True
        and next_contract.get("field_released") is True
    )
    return {
        "schema": "storage_action_transition_v1",
        "covered": covered,
        "allowed": released,
        "previous_action": previous,
        "next_action": next_value,
        "previous_effect": (
            previous_contract.get("effect") if previous_contract else None
        ),
        "next_effect": next_contract.get("effect") if next_contract else None,
        "requires_fresh_slot_identity": previous != next_value,
        "reason": (
            "released_pair"
            if released
            else "unreleased_action"
            if covered
            else "unknown_action"
        ),
    }


DIRECT_MARKETING_CANONICAL_ACTIONS: Dict[str, Dict[str, Any]] = {
    "FORCE_EXPORT": {
        "plan_action": "ECONOMIC_EXPORT",
        # Beide Aktionen sind ein gemeinsamer diagnostischer Vertrag. Nur die
        # Eco+-Kante ist aktuell bis zum Hardwareausgang freigegeben.
        "source_actions": frozenset({
            "eco_plus_export_candidate",
            "arbitrage_export_candidate",
        }),
        "source_action_execution_release": {
            "eco_plus_export_candidate": True,
            "arbitrage_export_candidate": False,
        },
        "source_action_modes": {
            "eco_plus_export_candidate": frozenset({"eco_plus"}),
            "arbitrage_export_candidate": frozenset({"arbitrage"}),
        },
        "source_modes": frozenset({"eco_plus", "arbitrage"}),
        "budget_key": "export_budget_w",
        "direction": "discharge",
        "canonical_execution_released": True,
    },
    "HEADROOM_EXPORT": {
        "plan_action": "HEADROOM_EXPORT",
        "source_actions": frozenset({"eco_plus_negative_headroom_hold"}),
        "source_action_execution_release": {
            "eco_plus_negative_headroom_hold": False,
        },
        "source_action_modes": {
            "eco_plus_negative_headroom_hold": frozenset({"eco_plus"}),
        },
        "source_modes": frozenset({"eco_plus"}),
        "budget_key": "export_budget_w",
        "direction": "discharge",
        # Die Zuordnung ist bereits typisiert, der vollständige Headroom-Gate-
        # und Executorvertrag wird jedoch separat freigegeben.
        "canonical_execution_released": False,
    },
    "FORCE_CHARGE_PV": {
        "plan_action": "PV_STORE",
        "source_actions": frozenset({"eco_plus_store_pv_candidate"}),
        "source_action_execution_release": {
            "eco_plus_store_pv_candidate": True,
        },
        "source_action_modes": {
            "eco_plus_store_pv_candidate": frozenset({"eco", "eco_plus"}),
        },
        "source_modes": frozenset({"eco", "eco_plus"}),
        "budget_key": "charge_budget_w",
        "direction": "charge",
        "canonical_execution_released": True,
    },
    "CHARGE_BLOCK_WAIT": {
        "plan_action": "CHARGE_BLOCK_WAIT",
        "source_actions": frozenset({"direct_marketing_charge_block_wait"}),
        "source_action_execution_release": {
            "direct_marketing_charge_block_wait": True,
        },
        "source_action_modes": {
            "direct_marketing_charge_block_wait": frozenset({"eco_plus"}),
        },
        "source_modes": frozenset({"eco_plus"}),
        "budget_key": None,
        "direction": "hold",
        "canonical_execution_released": True,
    },
}

DIRECT_MARKETING_ACTIVE_TARGETS = frozenset(
    DIRECT_MARKETING_CANONICAL_ACTIONS
)
DIRECT_MARKETING_CANONICAL_PLAN_ACTIONS = frozenset(
    str(contract["plan_action"])
    for contract in DIRECT_MARKETING_CANONICAL_ACTIONS.values()
)
DIRECT_MARKETING_RELEASED_PLAN_ACTIONS = frozenset(
    str(contract["plan_action"])
    for contract in DIRECT_MARKETING_CANONICAL_ACTIONS.values()
    if contract.get("canonical_execution_released") is True
)
# GRID_CHARGE bleibt als bisheriger Runtime-Claim ausdrücklich fail-closed,
# obwohl dafür noch kein freigegebener Producervertrag existiert.
DIRECT_MARKETING_RUNTIME_PLAN_ACTIONS = frozenset(
    set(DIRECT_MARKETING_CANONICAL_PLAN_ACTIONS) | {"GRID_CHARGE"}
)


def direct_marketing_action_contract(
    target_state: Any,
) -> Optional[Dict[str, Any]]:
    """Liefert eine defensive Kopie des gemeinsamen Aktionsvertrags."""

    target = str(target_state or "").strip().upper()
    contract = DIRECT_MARKETING_CANONICAL_ACTIONS.get(target)
    return copy.deepcopy(contract) if contract is not None else None


def direct_marketing_target_for_plan_action(
    plan_action: Any,
) -> Optional[str]:
    action = str(plan_action or "").strip().upper()
    matches = [
        target
        for target, contract in DIRECT_MARKETING_CANONICAL_ACTIONS.items()
        if contract["plan_action"] == action
    ]
    return matches[0] if len(matches) == 1 else None


def direct_marketing_plan_action_released(plan_action: Any) -> bool:
    """Prüft die Freigabe dynamisch gegen genau einen Matrixeintrag.

    Die abgeleiteten Mengen oben dienen Diagnose und statischen Prüfungen. Die
    Laufzeit darf ihre Freigabe nicht aus einer beim Import eingefrorenen Kopie
    ableiten: So kann das vollständige Durchstich-Gate die noch geschlossene
    ``HEADROOM_EXPORT``-Kante im Testprozess gezielt öffnen, ohne den
    Produktvertrag umzuschalten.
    """

    target = direct_marketing_target_for_plan_action(plan_action)
    contract = direct_marketing_action_contract(target)
    return bool(
        contract
        and contract.get("canonical_execution_released") is True
    )


def direct_marketing_source_action_known(
    target_state: Any,
    source_action: Any,
) -> bool:
    """Bestätigt eine diagnostisch bekannte Quellaktion der Zielmatrix."""

    contract = direct_marketing_action_contract(target_state)
    source = str(source_action or "").strip()
    return bool(
        contract
        and source
        and source in set(contract.get("source_actions") or ())
    )


def direct_marketing_source_action_released(
    target_state: Any,
    source_action: Any,
) -> bool:
    """Prüft die aktionsgenaue Freigabe bis zum Hardwareausgang."""

    contract = direct_marketing_action_contract(target_state)
    source = str(source_action or "").strip()
    releases = (
        contract.get("source_action_execution_release")
        if isinstance(contract, dict)
        and isinstance(contract.get("source_action_execution_release"), dict)
        else {}
    )
    return bool(
        contract
        and contract.get("canonical_execution_released") is True
        and source in set(contract.get("source_actions") or ())
        and releases.get(source) is True
    )


def direct_marketing_source_action_mode_valid(
    target_state: Any,
    source_action: Any,
    source_mode: Any,
) -> bool:
    """Bindet eine Quellaktion fail-closed an ihren Producer-Modus.

    Getrennte Mengen aus bekannten Aktionen und Modi würden sonst ein
    kartesisches Produkt erlauben, etwa eine Eco+-Exportaktion unter dem
    Arbitrage-Modus. Die Paarbindung ist deshalb Teil des zentralen
    Aktionsvertrags und darf nicht in den Verbrauchern neu erraten werden.
    """

    contract = direct_marketing_action_contract(target_state)
    source = str(source_action or "").strip()
    mode = str(source_mode or "").strip().lower()
    action_modes = (
        contract.get("source_action_modes")
        if isinstance(contract, dict)
        and isinstance(contract.get("source_action_modes"), dict)
        else {}
    )
    allowed_modes = action_modes.get(source)
    return bool(
        contract
        and source in set(contract.get("source_actions") or ())
        and mode in set(allowed_modes or ())
    )


def direct_marketing_typed_int_equals(value: Any, expected: int) -> bool:
    """Prüft versionierte Integerfelder ohne Python-Bool-Alias.

    ``bool`` ist in Python eine Unterklasse von ``int``. Ein aus JSON
    eingelesenes ``true`` darf deshalb weder als Vertragsversion ``1`` noch
    als Eindeutigkeitsnachweis ``1`` akzeptiert werden.
    """

    return bool(
        isinstance(expected, int)
        and not isinstance(expected, bool)
        and isinstance(value, int)
        and not isinstance(value, bool)
        and value == expected
    )


def _direct_marketing_finite_contract_number(value: Any) -> bool:
    return bool(
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(float(value))
    )


def _direct_marketing_contract_ts_ms(value: Any) -> int:
    if not _direct_marketing_finite_contract_number(value):
        return 0
    parsed = int(round(float(value)))
    if 0 < parsed < 100_000_000_000:
        parsed *= 1000
    return parsed


def direct_marketing_contract_sha256(material: Any) -> str:
    """Hasht nur vollständig JSON-serialisierbare Vertragsdaten."""

    try:
        payload = json.dumps(
            material,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    except (TypeError, ValueError):
        return ""
    return "sha256:" + hashlib.sha256(payload).hexdigest()


def direct_marketing_export_gate_sha256(gate: Any) -> str:
    return direct_marketing_contract_sha256(gate) if isinstance(gate, dict) else ""


def direct_marketing_export_gate_lineage_id(gate: Any) -> str:
    if not isinstance(gate, dict):
        return ""
    gate_sha256 = direct_marketing_export_gate_sha256(gate)
    if not gate_sha256:
        return ""
    return direct_marketing_contract_sha256({
        "schema": DIRECT_MARKETING_EXPORT_GATE_LINEAGE_SCHEMA,
        "gate_sha256": gate_sha256,
        "action": gate.get("action"),
        "window_id": gate.get("window_id"),
        "origin_start_ts": gate.get("origin_start_ts"),
        "end_ts": gate.get("end_ts"),
    })


def direct_marketing_export_gate_generation_id(
    gate_lineage_id: Any,
    generation: Any,
) -> str:
    if not bool(
        isinstance(gate_lineage_id, str)
        and gate_lineage_id.startswith("sha256:")
        and len(gate_lineage_id) == 71
        and isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation >= 1
    ):
        return ""
    return direct_marketing_contract_sha256({
        "gate_lineage_id": gate_lineage_id,
        "generation": generation,
    })


def direct_marketing_export_gate_lineage_shape_valid(
    lineage: Any,
    allowed_statuses: Any = None,
) -> bool:
    """Gemeinsame strukturelle Prüfung eines wirkungslosen Lineage-Status."""

    if not isinstance(lineage, dict):
        return False
    status = lineage.get("status")
    if status not in DIRECT_MARKETING_EXPORT_GATE_LINEAGE_STATES:
        return False
    if allowed_statuses is not None and status not in set(allowed_statuses):
        return False
    generation = lineage.get("current_generation")
    if not bool(
        isinstance(generation, int)
        and not isinstance(generation, bool)
        and generation >= 1
    ):
        return False
    gate_sha256 = str(lineage.get("gate_sha256") or "")
    gate_lineage_id = str(lineage.get("gate_lineage_id") or "")
    expected_lineage_id = direct_marketing_contract_sha256({
        "schema": DIRECT_MARKETING_EXPORT_GATE_LINEAGE_SCHEMA,
        "gate_sha256": gate_sha256,
        "action": lineage.get("action"),
        "window_id": lineage.get("window_id"),
        "origin_start_ts": lineage.get("origin_start_ts"),
        "end_ts": lineage.get("end_ts"),
    })
    reasons = lineage.get("transition_reason_codes")
    return bool(
        lineage.get("schema")
        == DIRECT_MARKETING_EXPORT_GATE_LINEAGE_SCHEMA
        and lineage.get("effect_contract")
        == DIRECT_MARKETING_EXPORT_GATE_LINEAGE_EFFECT_CONTRACT
        and len(gate_sha256) == 71
        and gate_sha256.startswith("sha256:")
        and all(character in "0123456789abcdef" for character in gate_sha256[7:])
        and gate_lineage_id == expected_lineage_id
        and lineage.get("current_generation_id")
        == direct_marketing_export_gate_generation_id(
            gate_lineage_id,
            generation,
        )
        and lineage.get("previous_generation_id")
        == (
            direct_marketing_export_gate_generation_id(
                gate_lineage_id,
                generation - 1,
            )
            if generation > 1
            else None
        )
        and isinstance(reasons, list)
        and bool(reasons)
        and reasons == sorted(set(reasons))
        and all(isinstance(reason, str) and bool(reason) for reason in reasons)
        and isinstance(lineage.get("action"), str)
        and bool(lineage.get("action"))
        and isinstance(lineage.get("window_id"), str)
        and bool(lineage.get("window_id"))
        and isinstance(lineage.get("origin_start_ts"), int)
        and not isinstance(lineage.get("origin_start_ts"), bool)
        and isinstance(lineage.get("end_ts"), int)
        and not isinstance(lineage.get("end_ts"), bool)
        and lineage.get("origin_start_ts") > 0
        and lineage.get("end_ts") > lineage.get("origin_start_ts")
    )


def direct_marketing_export_gate_lineage_valid(
    lineage: Any,
    gate: Any,
    allowed_statuses: Any = None,
) -> bool:
    return bool(
        direct_marketing_export_gate_lineage_shape_valid(
            lineage,
            allowed_statuses,
        )
        and isinstance(gate, dict)
        and lineage.get("gate_sha256")
        == direct_marketing_export_gate_sha256(gate)
        and lineage.get("gate_lineage_id")
        == direct_marketing_export_gate_lineage_id(gate)
        and lineage.get("action") == gate.get("action")
        and lineage.get("window_id") == gate.get("window_id")
        and lineage.get("origin_start_ts") == gate.get("origin_start_ts")
        and lineage.get("end_ts") == gate.get("end_ts")
    )


def direct_marketing_export_gate_contract_valid(
    decision: Any,
    economics: Any = None,
    allowed_lineage_statuses: Any = ("ACTIVE",),
    current_window_id: Any = None,
    current_window_end_ts_ms: Any = None,
) -> bool:
    """Einheitlicher Strukturvertrag für Producer und alle Consumer.

    Der Predicate erteilt keine Hardwarefreigabe. Diese bleibt eine separate,
    stufenspezifische Prüfung der Aktionsmatrix und der aktuellen Generation.
    """

    if not isinstance(decision, dict):
        return False
    gate = decision.get("export_window_start_gate")
    lineage = decision.get("export_window_gate_lineage")
    selected = decision.get("selected_window")
    execution = decision.get("execution_window")
    budget = (
        decision.get("storage_budget")
        if isinstance(decision.get("storage_budget"), dict)
        else {}
    )
    economics_contract = (
        economics
        if isinstance(economics, dict)
        else decision.get("economics")
        if isinstance(decision.get("economics"), dict)
        else {}
    )
    if not bool(
        isinstance(gate, dict)
        and isinstance(lineage, dict)
        and isinstance(selected, dict)
    ):
        return False
    statuses = set(allowed_lineage_statuses or ())
    if not statuses or not direct_marketing_export_gate_lineage_valid(
        lineage,
        gate,
        statuses,
    ):
        return False

    numeric_keys = (
        "initial_expected_profit_eur",
        "min_window_profit_eur",
        "initial_export_energy_kwh",
        "min_export_energy_kwh",
        "initial_duration_min",
        "min_export_window_min",
    )
    if not all(
        _direct_marketing_finite_contract_number(gate.get(key))
        for key in numeric_keys
    ):
        return False
    action = str(gate.get("action") or "")
    window_id = str(gate.get("window_id") or "")
    profile = str(gate.get("profile") or "")
    origin_ms = _direct_marketing_contract_ts_ms(gate.get("origin_start_ts"))
    end_ms = _direct_marketing_contract_ts_ms(gate.get("end_ts"))
    policy_start_ms = _direct_marketing_contract_ts_ms(
        decision.get("start_ts")
    )
    policy_end_ms = _direct_marketing_contract_ts_ms(decision.get("end_ts"))
    current_thresholds = {
        key: economics_contract.get(key)
        for key in (
            "min_window_profit_eur",
            "min_export_energy_kwh",
            "min_export_window_min",
        )
    }
    if not all(
        _direct_marketing_finite_contract_number(value)
        and float(value) >= 0.0
        for value in current_thresholds.values()
    ):
        return False
    if current_window_id is not None and str(current_window_id or "") != window_id:
        return False
    if current_window_end_ts_ms is not None and (
        _direct_marketing_contract_ts_ms(current_window_end_ts_ms) != end_ms
    ):
        return False
    if not bool(
        decision.get("schema") == DIRECT_MARKETING_POLICY_SCHEMA
        and gate.get("schema") == DIRECT_MARKETING_EXPORT_START_GATE_SCHEMA
        and gate.get("passed") is True
        and gate.get("accounting_contract")
        == "START_ONLY_NO_REMAINING_WINDOW_REAPPLICATION"
        and profile in DIRECT_MARKETING_EXPORT_PROFIT_PROFILES
        and str(decision.get("profit_profile") or "") == profile
        and direct_marketing_source_action_known("FORCE_EXPORT", action)
        and window_id
        and decision.get("window_id") == window_id
        and selected.get("window_id") == window_id
        and selected.get("action") == action
        and decision.get("source_action") == action
        and origin_ms > 0
        and end_ms > origin_ms
        and policy_start_ms > 0
        and policy_end_ms > policy_start_ms
        and _direct_marketing_contract_ts_ms(
            decision.get("window_origin_start_ts")
        ) == origin_ms
        and _direct_marketing_contract_ts_ms(selected.get("start_ts"))
        == origin_ms
        and _direct_marketing_contract_ts_ms(selected.get("end_ts")) == end_ms
        and all(
            abs(float(gate[key]) - float(current_thresholds[key]))
            <= 0.000001
            for key in current_thresholds
        )
        and float(gate["min_window_profit_eur"]) >= 0.0
        and float(gate["min_export_energy_kwh"]) >= 0.0
        and float(gate["min_export_window_min"]) >= 15.0
        and (
            profile != "standard"
            or (
                float(gate["initial_expected_profit_eur"]) + 0.000001
                >= float(gate["min_window_profit_eur"])
                and float(gate["initial_export_energy_kwh"]) + 0.000001
                >= float(gate["min_export_energy_kwh"])
                and float(gate["initial_duration_min"]) + 0.000001
                >= float(gate["min_export_window_min"])
            )
        )
    ):
        return False

    status = lineage.get("status")
    if status == "ACTIVE":
        if not isinstance(execution, dict):
            return False
        active_economics = {
            key: economics_contract.get(key)
            for key in (
                "margin_ct_kwh",
                "user_min_margin_ct",
                "expected_profit_eur",
                "min_window_profit_eur",
            )
        }
        if not all(
            _direct_marketing_finite_contract_number(value)
            for value in active_economics.values()
        ):
            return False
        continuation = bool(
            decision.get("continuation_active") is True
            and decision.get("continuation_reason_code")
            == "WINDOW_START_GATES_ALREADY_SATISFIED"
        )
        profile_economics_valid = bool(
            profile == "expert"
            or (
                profile == "aggressive"
                and float(active_economics["margin_ct_kwh"]) > 0.0
            )
            or (
                profile == "standard"
                and float(active_economics["margin_ct_kwh"]) + 0.000001
                >= float(active_economics["user_min_margin_ct"])
                and (
                    float(active_economics["expected_profit_eur"])
                    + 0.000001
                    >= float(active_economics["min_window_profit_eur"])
                    or continuation
                )
            )
        )
        plan_start_ms = _direct_marketing_contract_ts_ms(
            execution.get("plan_window_start_ts")
        )
        execution_start_ms = _direct_marketing_contract_ts_ms(
            execution.get("start_ts")
        )
        execution_end_ms = _direct_marketing_contract_ts_ms(
            execution.get("end_ts")
        )
        plan_end_ms = _direct_marketing_contract_ts_ms(
            execution.get("plan_window_end_ts")
        )
        return bool(
            decision.get("commands_allowed") is True
            and decision.get("blocked") is False
            and str(decision.get("dv_target_state") or "").upper()
            == "FORCE_EXPORT"
            and decision.get("executable_action") == action
            and profile_economics_valid
            and _direct_marketing_finite_contract_number(
                budget.get("export_budget_w")
            )
            and float(budget.get("export_budget_w")) > 0.0
            and direct_marketing_typed_int_equals(
                execution.get("contract_version"),
                1,
            )
            and execution.get("source") == "active_plan_window"
            and direct_marketing_typed_int_equals(
                decision.get("execution_window_match_count"),
                1,
            )
            and execution.get("window_id") == window_id
            and execution.get("action") == action
            and _direct_marketing_contract_ts_ms(
                execution.get("origin_start_ts")
            ) == origin_ms
            and origin_ms <= policy_start_ms
            <= plan_start_ms <= execution_start_ms < execution_end_ms
            <= policy_end_ms
            <= plan_end_ms == end_ms
        )
    if status != "SUSPENDED":
        return False
    if not bool(
        decision.get("commands_allowed") is False
        and decision.get("blocked") is True
        and str(decision.get("dv_target_state") or "").upper() == "HOLD"
        and decision.get("executable_action") is None
        and _direct_marketing_finite_contract_number(
            budget.get("export_budget_w")
        )
        and float(budget.get("export_budget_w")) == 0.0
        and _direct_marketing_finite_contract_number(
            budget.get("charge_budget_w")
        )
        and float(budget.get("charge_budget_w")) == 0.0
    ):
        return False
    match_count = decision.get("execution_window_match_count")
    if direct_marketing_typed_int_equals(match_count, 0):
        return execution is None
    if not bool(
        direct_marketing_typed_int_equals(match_count, 1)
        and isinstance(execution, dict)
    ):
        return False
    plan_start_ms = _direct_marketing_contract_ts_ms(
        execution.get("plan_window_start_ts")
    )
    execution_start_ms = _direct_marketing_contract_ts_ms(
        execution.get("start_ts")
    )
    execution_end_ms = _direct_marketing_contract_ts_ms(
        execution.get("end_ts")
    )
    plan_end_ms = _direct_marketing_contract_ts_ms(
        execution.get("plan_window_end_ts")
    )
    return bool(
        direct_marketing_typed_int_equals(
            execution.get("contract_version"),
            1,
        )
        and execution.get("source") == "active_plan_window"
        and execution.get("window_id") == window_id
        and execution.get("action") == action
        and _direct_marketing_contract_ts_ms(execution.get("origin_start_ts"))
        == origin_ms
        and origin_ms <= policy_start_ms
        <= plan_start_ms <= execution_start_ms < execution_end_ms
        <= policy_end_ms
        <= plan_end_ms == end_ms
    )
