"""Pure wallbox policy decisions.

The wallbox manager gathers live state and the drivers translate commands to
hardware APIs.  This module owns the shared charging policy: mode profile,
grid permission, battery support and the allowed wallbox power budget.
"""

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

from .modes import CONTROL_CURVE, MODE_CURVE, MODE_OFF, MODE_PRICE


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip() if value is not None else ""
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        text = str(value).strip() if value is not None else ""
        return int(float(text)) if text else int(default)
    except (TypeError, ValueError):
        return int(default)


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() in ("1", "true", "yes", "on", "ja")
    return bool(value)


# Internal controller paths, not public UI modes.  Historic values are kept
# here so the manager has one source of truth while the policy is extracted.
CONTROL_MODE_PARAMS: Dict[int, Dict[str, Any]] = {
    0: {"bat": False, "band_up": 0.0, "band_dn": 0.0},
    1: {"bat": False, "band_up": 0.0, "band_dn": 0.0},
    2: {"bat": True, "band_up": 2.0, "band_dn": 2.0},
    # PV-Kurve nutzt im stationären Sollwert nur das PV-/Ladekurvenbudget.
    # Kurze Batteriestützung gegen Wolken- und Messwerttransienten wird
    # getrennt, zeit- und energiebegrenzt im PV-Hybrid-Gate behandelt.
    3: {"bat": False, "band_up": 3.0, "band_dn": 4.0},
    4: {"bat": True, "band_up": 3.0, "band_dn": 0.0},
    5: {"bat": True, "band_up": 3.0, "band_dn": 4.0, "eco": True},
    6: {"bat": True, "band_up": 3.0, "band_dn": 4.0, "base_6a": True},
}


def fuzzy_factor(delta_pct: float, band_up_pct: float, band_down_pct: float) -> float:
    """Return the soft start/stop factor for curve-guided wallbox modes."""

    delta = _safe_float(delta_pct, 0.0)
    band_up = max(0.0, _safe_float(band_up_pct, 0.0))
    band_down = max(0.0, _safe_float(band_down_pct, 0.0))
    if band_up <= 0.0 and band_down <= 0.0:
        return 1.0 if delta >= 0.0 else 0.0
    if delta >= band_up:
        return 1.0
    if delta <= -band_down:
        return 0.0
    span = max(0.1, band_up + band_down)
    return max(0.0, min(1.0, (delta + band_down) / span))


def resolve_mode_policy(
    *,
    effective_wb_mode: int,
    delta_pct: float,
    pv_power_w: float,
    eco_score_now: float,
    eco_grid_score: float,
    eco_pv_score: float,
    battery_soc: Optional[float],
    wb_soc_hyst_pct: float,
    curve_wb_relief_active: bool,
    hysteresis_gate: Callable[[str, float, float, float, bool], bool],
) -> Dict[str, Any]:
    """Resolve the shared policy profile for one controller mode.

    ``hysteresis_gate`` is supplied by the runtime state object so this helper
    stays free of global state while preserving the existing hysteresis memory.
    """

    mode = _safe_int(effective_wb_mode, 0)
    delta = _safe_float(delta_pct, 0.0)
    params: Dict[str, Any]
    effective_allow_grid = False
    fuzzy_delta = delta
    band_dn_eff = 0.0
    band_up = 0.0
    fz = 0.0
    wbmin_mode4_gate_open = True
    gate_open = True

    if mode >= 10:
        params = {"bat": True, "band_up": 0.0, "band_dn": 0.0}
        effective_allow_grid = mode == 11
        fz = 1.0
    elif mode == 9:
        if _safe_float(pv_power_w, 0.0) > 100.0:
            params = {"bat": True, "band_up": 0.0, "band_dn": 0.0}
            fz = 1.0
        else:
            params = {"bat": False, "band_up": 0.0, "band_dn": 0.0}
            fz = 0.0
    else:
        params = dict(CONTROL_MODE_PARAMS.get(mode, CONTROL_MODE_PARAMS[3]))
        if params.get("eco"):
            if _safe_float(eco_score_now, 0.0) >= _safe_float(eco_grid_score, 0.0):
                effective_allow_grid = True
            elif _safe_float(eco_score_now, 0.0) < _safe_float(eco_pv_score, 0.0):
                params = dict(CONTROL_MODE_PARAMS[1])

        if mode == 4:
            band_dn_eff = 0.0
            fz = 1.0
        else:
            band_dn_eff = _safe_float(params.get("band_dn"), 0.0)
            fz = 0.0 if mode == MODE_OFF else fuzzy_factor(
                fuzzy_delta,
                params.get("band_up", 0.0),
                band_dn_eff,
            )
        band_up = _safe_float(params.get("band_up"), 0.0)

        if mode == 4:
            wbmin_mode4_gate_open = not (
                battery_soc is not None and _safe_float(battery_soc, 0.0) < 5.0
            )
            if not wbmin_mode4_gate_open:
                fz = 0.0
        elif mode in (1, 2, 3, 5, 6):
            hyst = _safe_float(wb_soc_hyst_pct, 0.0)
            if band_up <= 0.0 and band_dn_eff <= 0.0:
                gate_start = hyst
                gate_stop = -hyst
            else:
                gate_start = -band_dn_eff + hyst
                gate_stop = -band_dn_eff
            gate_open = bool(
                hysteresis_gate(
                    "curve_mode%d" % mode,
                    fuzzy_delta,
                    gate_start,
                    gate_stop,
                    False,
                )
            )
            if not gate_open:
                fz = 0.0

    if curve_wb_relief_active:
        fz = max(fz, 1.0)

    return {
        "params": params,
        "effective_allow_grid": bool(effective_allow_grid),
        "fuzzy_delta": float(fuzzy_delta),
        "band_dn_eff": float(band_dn_eff),
        "band_up": float(band_up),
        "fz": float(fz),
        "wbmin_mode4_gate_open": bool(wbmin_mode4_gate_open),
        "gate_open": bool(gate_open),
    }


def base_charge_active(
    *,
    params: Dict[str, Any],
    base_floor_reachable: bool,
    soll_soc_now_available: bool,
    storage_grid_hold_active: bool,
    curve_wb_relief_active: bool,
) -> bool:
    """Shared 6A floor rule for ``Grundladung stabil``."""

    return bool(
        params.get("base_6a")
        and base_floor_reachable
        and soll_soc_now_available
        and not (storage_grid_hold_active and not curve_wb_relief_active)
    )


@dataclass
class EnergyPolicyInput:
    effective_wb_mode: int
    effective_public_wb_mode: int
    params: Dict[str, Any]
    fz: float
    wb_max_amp: float
    detected_phases: int
    wb_actual_power: float
    free_for_limbs_w: float
    phys_surplus_w: float
    raw_iaval_w: float
    eba_iaval_w: float
    wb_storage_cap_w: float
    wb_storage_extra_w: float
    pv_power_raw: float
    pv_only_allowed_w: float
    pv_surplus_ex_wb_w: float
    wbminsoc_gate_open: bool
    price_boost_wallbox_active: bool
    predump_wallbox_active: bool
    predump_wallbox_gate_open: bool
    price_optimizing_active: bool
    effective_allow_grid: bool
    base_6a_active: bool
    curve_wb_relief_active: bool
    forecast_auto_relief_active: bool
    storage_charge_priority_active: bool
    grid_unlocked_all_controllable: bool
    controlled_wallbox_wbminsoc_pause: bool
    controlled_wallbox_wbminsoc_pv_only_active: bool
    wbminsoc_discharge_taper_active: bool
    target_wbminsoc_discharge_pull_active: bool
    target_wbminsoc_low_power_recovery_active: bool
    target_wbminsoc_low_power_start_ready: bool
    forecast_auto_battery_assist: bool
    predump_wallbox_floor_block: bool
    target_wbminsoc_discharge_taper_limit_w: float
    target_wbminsoc_low_power_min_w: float
    target_wbminsoc_discharge_pull_limit_w: float
    target_wbminsoc_low_power_bootstrap_w: float
    forecast_auto_limit_w: float
    forecast_auto_min_w: float
    storage_curve_budget_active: bool
    wr_limit_config_w: Any
    ac_power_limit_live_w: float
    ext_pv_power_raw: float
    home_w: float
    grid_reserve_w: float
    battery_power_raw: float
    floor_discharge_threshold_w: Any
    floor_down_step_w: Any
    budget_ok: bool
    openwb_pro_curve_direct_possible: bool
    openwb_pro_curve_direct_storage_block: bool
    wallbox_curve_reserve_w: float
    grid_export_room_w: float
    grid_import_for_budget_w: float
    grid_import_budget_down_active: bool
    grid_import_w: float
    native_sun_capable: bool
    authorized_wallbox_budget_w: float = 32.0 * 230.0 * 3.0
    consumer_budget_contract: Optional[Dict[str, Any]] = None
    consumer_budget_identity_valid: bool = False
    direct_marketing_active: bool = False
    direct_marketing_policy_target_state: Optional[str] = None
    openwb_pro_curve_direct_start_min_w: float = 6.0 * 230.0
    peak_shaving_enabled: bool = False
    peak_shaving_budget_valid: bool = False
    peak_shaving_allowed_remaining_import_w: Optional[float] = None
    peak_shaving_base_import_w: Optional[float] = None
    predump_discharge_add_w: Optional[float] = None
    predump_discharge_contract_valid: bool = True
    grid_funded_wallbox_authorized: bool = False


def _identity_bound_wallbox_source_budget(
    ctx: EnergyPolicyInput,
    total_authorized_w: float,
) -> Dict[str, Any]:
    """Projiziert normale und Pre-Dump-Quelle aus einem Budget-Snapshot.

    ``authorized_wallbox_budget_w`` ist bereits der absolute Commandrahmen und
    enthält gegebenenfalls auch den Pre-Dump-Anteil. Deshalb darf der
    Pre-Dump-Zusatz niemals erneut auf diesen Gesamtwert addiert werden. Nur der
    identity-validierte, verschachtelte Consumervertrag darf die zwei
    Quellenkomponenten belegen.
    """

    result = {
        "valid": False,
        "normal_support_w": 0.0,
        "predump_add_w": 0.0,
        "predump_contract_valid": False,
        "reason": "consumer_budget_identity_invalid",
    }
    if getattr(ctx, "consumer_budget_identity_valid", False) is not True:
        return result

    contract = getattr(ctx, "consumer_budget_contract", None)
    if not isinstance(contract, dict):
        result["reason"] = "consumer_budget_contract_missing"
        return result
    if contract.get("schema_version") != "flexible_consumer_budget_contract_v1":
        result["reason"] = "consumer_budget_contract_schema_invalid"
        return result

    command_allocations = contract.get("command_allocations")
    command_wallbox_w = (
        command_allocations.get("wallbox")
        if isinstance(command_allocations, dict)
        else None
    )
    if (
        isinstance(command_wallbox_w, bool)
        or not isinstance(command_wallbox_w, int)
        or command_wallbox_w < 0
        or abs(float(command_wallbox_w) - float(total_authorized_w)) > 0.001
    ):
        result["reason"] = "consumer_budget_wallbox_projection_mismatch"
        return result

    normal_values = []
    for key in (
        "wallbox_exclusive_start_support_w",
        "wallbox_running_hold_support_w",
    ):
        value = contract.get(key, 0)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            result["reason"] = key + "_invalid"
            return result
        normal_values.append(value)

    total_w = max(0.0, float(total_authorized_w))
    result.update({
        "valid": True,
        "normal_support_w": min(total_w, float(max(normal_values))),
        "reason": "consumer_budget_sources_valid",
    })

    predump = contract.get("predump_discharge_add_contract")
    if not isinstance(predump, dict) or predump.get("valid") is not True:
        return result
    allocations = predump.get("allocations_w")
    requests = predump.get("requests_w")
    eligible = predump.get("eligible")
    if not all(isinstance(value, dict) for value in (allocations, requests, eligible)):
        result["reason"] = "predump_source_maps_invalid"
        return result
    consumers = ("heatpump", "wallbox", "heater")
    allocation_values = []
    for consumer in consumers:
        allocation = allocations.get(consumer)
        request = requests.get(consumer)
        if (
            isinstance(allocation, bool)
            or not isinstance(allocation, int)
            or allocation < 0
            or isinstance(request, bool)
            or not isinstance(request, int)
            or request < allocation
            or not isinstance(eligible.get(consumer), bool)
            or (allocation > 0 and eligible.get(consumer) is not True)
        ):
            result["reason"] = "predump_source_allocation_invalid"
            return result
        allocation_values.append(allocation)

    allocation_sum = sum(allocation_values)
    pool_w = predump.get("available_discharge_pool_w")
    if (
        predump.get("schema_version") != "predump_discharge_add_contract_v1"
        or predump.get("owner") != "storage_manager"
        or predump.get("effect") != "additional_battery_discharge_only"
        or predump.get("authorization_status") != "authorized"
        or predump.get("no_grid") is not True
        or predump.get("grid_fallback") is not False
        or predump.get("invariant_conserved") is not True
        or isinstance(pool_w, bool)
        or not isinstance(pool_w, int)
        or pool_w < allocation_sum
        or predump.get("allocation_sum_w") != allocation_sum
        or allocations.get("wallbox", 0) > command_wallbox_w
    ):
        result["reason"] = "predump_source_contract_invalid"
        return result

    result.update({
        "predump_add_w": float(allocations.get("wallbox", 0)),
        "predump_contract_valid": True,
        "reason": "consumer_budget_sources_valid",
    })
    return result



def peak_shaving_wallbox_power_cap(
    *,
    enabled: bool,
    budget_valid: bool,
    allowed_remaining_import_w: Any,
    base_import_w: Any,
    wallbox_actual_w: Any,
) -> Dict[str, Any]:
    """Begrenzt flexible Wallboxlast auf den verbleibenden 15-Minuten-Rahmen."""

    result = {
        "active": False,
        "budget_valid": bool(budget_valid),
        "cap_w": None,
        "non_wallbox_base_import_w": None,
        "reason": "disabled" if not enabled else "evidence_limit",
    }
    if not enabled or not budget_valid:
        return result
    if allowed_remaining_import_w is None or base_import_w is None:
        return result

    allowed_import_w = max(0.0, _safe_float(allowed_remaining_import_w, 0.0))
    current_wallbox_w = max(0.0, _safe_float(wallbox_actual_w, 0.0))
    non_wallbox_base_import_w = (
        _safe_float(base_import_w, 0.0) - current_wallbox_w
    )
    cap_w = max(0.0, allowed_import_w - non_wallbox_base_import_w)
    result.update({
        "active": True,
        "cap_w": float(cap_w),
        "non_wallbox_base_import_w": float(non_wallbox_base_import_w),
        "reason": "remaining_interval_import_budget",
    })
    return result


def build_group_source_envelope(
    *,
    pv_only_cap_w: Any,
    storage_cap_w: Optional[Any] = None,
    allowed_w: Optional[Any] = None,
    data_fresh: bool,
    safety_binding_valid: bool,
    binding_key: Any,
    grid_cap_w: Optional[Any] = None,
    grid_authorized: bool = False,
) -> Dict[str, Any]:
    """Versiegelt bereits berechnete Gruppen-Quellendeckel fail-closed.

    Die Funktion bewertet keine PV-, Speicher- oder Netzleistung neu. Sie
    nimmt ausschließlich zuvor berechnete und bereits sicherheitsbegrenzte
    Kandidaten entgegen. ``storage_cap_w`` und ``allowed_w`` sind zwei Namen
    für die inklusive Speicherhülle; wenn beide belegt sind, gilt der kleinere
    Wert. Eine Netzstufe kann diesen Deckel nur mit einer expliziten
    Autorisierung *und* einem eigenen, bereits sicherheitsbegrenzten Deckel
    erweitern.

    Die drei Ausgänge sind inklusive Hüllen und deshalb immer monoton:
    ``PV <= Speicher <= Netz``. Widersprüchliche Eingänge werden nur nach
    unten geschnitten; die Funktion erhöht keinen der gelieferten Kandidaten.
    Fehlende Frische, Safety-Bindung oder numerische Evidenz setzt alle
    Ausgänge auf 0 W.
    """

    binding = str(binding_key or "").strip()
    grid_permission = grid_authorized is True
    result: Dict[str, Any] = {
        "contract": "wallbox_group_source_envelope_v1",
        "valid": False,
        "reason": "unbound",
        "binding_key": binding,
        "data_fresh": data_fresh is True,
        "safety_binding_valid": safety_binding_valid is True,
        "grid_authorized": grid_permission,
        "pv_total_cap_w": 0.0,
        "storage_total_cap_w": 0.0,
        "grid_total_cap_w": 0.0,
        "storage_increment_cap_w": 0.0,
        "grid_increment_cap_w": 0.0,
        "storage_input_cap_w": 0.0,
    }

    if data_fresh is not True:
        result["reason"] = "stale_source_caps"
        return result
    if safety_binding_valid is not True:
        result["reason"] = "invalid_safety_binding"
        return result
    if not binding or len(binding) > 256:
        result["reason"] = "missing_or_invalid_binding_key"
        return result

    def _strict_cap(value: Any) -> Optional[float]:
        if value is None or isinstance(value, bool):
            return None
        try:
            cap = float(value)
        except (TypeError, ValueError, OverflowError):
            return None
        if not math.isfinite(cap) or cap < 0.0:
            return None
        return cap

    pv_candidate = _strict_cap(pv_only_cap_w)
    storage_candidates = []
    if storage_cap_w is not None:
        parsed_storage_cap = _strict_cap(storage_cap_w)
        if parsed_storage_cap is None:
            result["reason"] = "invalid_cap_value"
            return result
        storage_candidates.append(parsed_storage_cap)
    if allowed_w is not None:
        parsed_allowed_w = _strict_cap(allowed_w)
        if parsed_allowed_w is None:
            result["reason"] = "invalid_cap_value"
            return result
        storage_candidates.append(parsed_allowed_w)
    if not storage_candidates:
        result["reason"] = "storage_cap_missing"
        return result
    storage_candidate = min(storage_candidates)
    grid_candidate = (
        _strict_cap(grid_cap_w) if grid_cap_w is not None else None
    )
    if (
        pv_candidate is None
        or (grid_cap_w is not None and grid_candidate is None)
    ):
        result["reason"] = "invalid_cap_value"
        return result
    if grid_permission and grid_candidate is None:
        result["reason"] = "authorized_grid_cap_missing"
        return result

    # Ohne explizite Netzfreigabe endet die oberste Hülle exakt an der
    # Speicherhülle. Ein eventuell mitgelieferter Netzdeckel bleibt rein
    # diagnostisch und kann keine Leistung freigeben.
    if grid_permission:
        grid_total = grid_candidate
        storage_total = min(storage_candidate, grid_total)
        reason = "bound_grid_envelope"
    else:
        storage_total = storage_candidate
        grid_total = storage_total
        reason = (
            "grid_cap_ignored_without_authority"
            if grid_cap_w is not None
            else "bound_storage_envelope_only"
        )
    pv_total = min(pv_candidate, storage_total)

    result.update({
        "valid": True,
        "reason": reason,
        "pv_total_cap_w": float(pv_total),
        "storage_total_cap_w": float(storage_total),
        "grid_total_cap_w": float(grid_total),
        "storage_increment_cap_w": float(storage_total - pv_total),
        "grid_increment_cap_w": float(grid_total - storage_total),
        "storage_input_cap_w": float(storage_candidate),
    })
    return result


def decide_energy_policy(ctx: EnergyPolicyInput) -> Dict[str, Any]:
    """Compute the shared wallbox power decision before driver execution."""

    mode = _safe_int(ctx.effective_wb_mode, 0)
    phases = max(1, _safe_int(ctx.detected_phases, 1))
    fz = max(0.0, _safe_float(ctx.fz, 0.0))
    wb_actual = _safe_float(ctx.wb_actual_power, 0.0)
    free_w = _safe_float(ctx.free_for_limbs_w, 0.0)
    phys_surplus = max(0.0, _safe_float(ctx.phys_surplus_w, 0.0))
    allowed_w = 0.0
    native_mode9_batt_start = False
    mode5_pv_surplus_active = False
    curve_wb_relief_active = bool(ctx.curve_wb_relief_active)
    forecast_auto_relief_active = bool(ctx.forecast_auto_relief_active)
    controlled_floor_battery_guard_active = False
    controlled_floor_battery_discharge_w = 0.0
    controlled_floor_netpoint_limit_w = 0.0
    storage_curve_overcharge_relief_active = False
    storage_curve_overcharge_relief_w = 0.0
    storage_auth_budget = max(

        0.0,
        _safe_float(
            getattr(ctx, "authorized_wallbox_budget_w", 32.0 * 230.0 * 3.0),
            32.0 * 230.0 * 3.0,
        ),
    )
    source_budget_contract = _identity_bound_wallbox_source_budget(
        ctx,
        storage_auth_budget,
    )
    normal_wallbox_support_w = max(
        0.0,
        _safe_float(source_budget_contract.get("normal_support_w"), 0.0),
    )
    predump_discharge_add_w = max(
        0.0,
        _safe_float(source_budget_contract.get("predump_add_w"), 0.0),
    )
    display_wb_budget_curve_w = max(0.0, free_w * fz)

    raw_grid_funded_mode_active = bool(
        ctx.price_boost_wallbox_active
        or ctx.effective_allow_grid
        or ctx.grid_unlocked_all_controllable
    )
    grid_funded_budget_authority_active = bool(
        mode != MODE_OFF
        and ctx.effective_public_wb_mode != MODE_OFF
        and raw_grid_funded_mode_active
        and ctx.grid_funded_wallbox_authorized
    )

    if ctx.price_boost_wallbox_active:
        allowed_w = _safe_float(ctx.wb_max_amp, 0.0) * 230.0 * phases
    elif ctx.price_optimizing_active or ctx.effective_allow_grid:
        allowed_w = 32.0 * 230.0 * phases
    elif mode == 0:
        allowed_w = 0.0
    elif ctx.base_6a_active:
        base_w = 6.0 * 230.0 * phases
        base_surplus_budget_w = max(0.0, free_w * fz)
        if free_w > 0.0 and phys_surplus > base_w:
            base_surplus_budget_w = max(
                base_surplus_budget_w,
                min(free_w, phys_surplus),
            )
        display_wb_budget_curve_w = base_surplus_budget_w
        budget_w = min(wb_actual + base_surplus_budget_w, phys_surplus)
        allowed_w = max(base_w, budget_w)
    else:
        if fz <= 0.0:
            allowed_w = 0.0
        else:
            budget_w = free_w * fz
            allowed_w = min(wb_actual + budget_w, phys_surplus)

        # ``mode`` ist hier bereits der interne Controllerpfad (PV-Kurve = 3).
        # Die öffentliche PV-Kurve darf belegten physischen PV-Überschuss vor
        # der Fuzzy-Skalierung projizieren. Das bestehende interne Mode-2-
        # Verhalten bleibt getrennt; Storage- und Schutzdeckel folgen weiter.
        public_curve_pv_projection = bool(
            mode == CONTROL_CURVE
            and ctx.effective_public_wb_mode == MODE_CURVE
            and ctx.budget_ok
            and _safe_float(ctx.pv_power_raw, 0.0) > 100.0
        )
        if mode == 2:
            pv_only_w = max(
                0.0,
                _safe_float(ctx.pv_only_allowed_w, 0.0),
                _safe_float(ctx.pv_surplus_ex_wb_w, 0.0),
            )
            if pv_only_w > 0.0:
                allowed_w = max(allowed_w, min(phys_surplus, pv_only_w) if phys_surplus > 0.0 else pv_only_w)
        elif public_curve_pv_projection:
            # ``phys_surplus`` ist im Manager für diesen Pfad noch der vom
            # Netzpunkt abgeleitete Altwert. Im Stillstand kann der Storage
            # Manager den PV-Überschuss bereits aufnehmen und diesen Wert
            # dadurch unter die 6-A-Startschwelle drücken. Die öffentliche
            # PV-Kurve projiziert deshalb hier den separat belegten,
            # batterieneutralen PV-Kandidaten. Nutzer-Aus, physikalisches
            # WR-Limit, Peak-Shaving und der finale Storage-Vertrag bleiben
            # als nachgelagerte harte Deckel erhalten.
            public_curve_pv_w = max(
                0.0,
                _safe_float(ctx.pv_only_allowed_w, 0.0),
                _safe_float(ctx.pv_surplus_ex_wb_w, 0.0),
            )
            if public_curve_pv_w > 0.0:
                allowed_w = max(allowed_w, public_curve_pv_w)

        if free_w > 0.0:
            direct_pv_surplus_w = min(max(0.0, wb_actual + free_w), max(0.0, phys_surplus))
            if direct_pv_surplus_w > 0.0:
                allowed_w = max(allowed_w, direct_pv_surplus_w)
                display_wb_budget_curve_w = max(display_wb_budget_curve_w, direct_pv_surplus_w)

        if mode == 4:

            allowed_w = max(0.0, wb_actual + _safe_float(ctx.eba_iaval_w, 0.0))
        if mode == 9:
            if ctx.native_sun_capable:
                native_mode9_batt_start = bool(
                    ctx.wbminsoc_gate_open
                    and _safe_float(ctx.pv_power_raw, 0.0) > 100.0
                    and _safe_float(ctx.eba_iaval_w, 0.0) > 0.0
                )
            allowed_w = max(0.0, wb_actual + _safe_float(ctx.eba_iaval_w, 0.0))
            if _safe_float(ctx.wb_storage_cap_w, 0.0) > 0.0:
                allowed_w = max(
                    allowed_w,
                    min(
                        _safe_float(ctx.wb_storage_cap_w, 0.0),
                        wb_actual + max(0.0, _safe_float(ctx.wb_storage_extra_w, 0.0)),
                    ),
                )
        elif mode in (10, 11):
            if ctx.native_sun_capable:
                native_mode9_batt_start = bool(
                    ctx.wbminsoc_gate_open and _safe_float(ctx.eba_iaval_w, 0.0) > 0.0
                )
            allowed_w = max(0.0, wb_actual + _safe_float(ctx.eba_iaval_w, 0.0))
            if not ctx.wbminsoc_gate_open and not ctx.effective_allow_grid:
                direct_pv_limit_w = min(
                    max(0.0, wb_actual + max(0.0, free_w * fz)),
                    max(0.0, phys_surplus),
                )
                allowed_w = max(allowed_w, direct_pv_limit_w)
            if _safe_float(ctx.wb_storage_cap_w, 0.0) > 0.0:
                native_mode9_batt_start = bool(
                    native_mode9_batt_start or _safe_float(ctx.wb_storage_extra_w, 0.0) > 0.0
                )
                allowed_w = max(
                    allowed_w,
                    min(
                        _safe_float(ctx.wb_storage_cap_w, 0.0),
                        wb_actual + max(0.0, _safe_float(ctx.wb_storage_extra_w, 0.0)),
                    ),
                )
            if (
                ctx.effective_public_wb_mode == MODE_PRICE
                and not ctx.effective_allow_grid
                and not ctx.price_boost_wallbox_active
                and not ctx.price_optimizing_active
            ):
                price_mode_pv_limit_w = max(0.0, _safe_float(ctx.pv_only_allowed_w, 0.0))
                if price_mode_pv_limit_w > 0.0:
                    allowed_w = max(allowed_w, price_mode_pv_limit_w)
                    mode5_pv_surplus_active = True
        elif free_w >= 99000.0:
            allowed_w = max(0.0, phys_surplus + wb_actual)


    # Pre-Dump ist eine additive Quelle. Wenn sein eigener Floor oder Gate die
    # Zusatzentladung schließt, darf das weder ein unabhängig freigegebenes
    # PV-/wbminSoC-Budget verkleinern noch eine laufende Ladung stoppen.
    if mode != 0 and ctx.predump_wallbox_active:
        pv_only_w = max(0.0, _safe_float(ctx.pv_only_allowed_w, 0.0))
        if (
            ctx.predump_wallbox_gate_open
            and source_budget_contract.get("predump_contract_valid") is True
            and predump_discharge_add_w > 0.0
        ):
            allowed_w = max(
                allowed_w,
                min(
                    storage_auth_budget,
                    pv_only_w + predump_discharge_add_w,
                ),
            )







    if curve_wb_relief_active and not ctx.price_boost_wallbox_active:
        curve_relief_limit_w = max(0.0, wb_actual + max(0.0, free_w))
        curve_relief_limit_w = max(curve_relief_limit_w, 6.0 * 230.0 * phases)
        if curve_relief_limit_w > 0.0:
            allowed_w = max(allowed_w, curve_relief_limit_w)

    if (
        ctx.forecast_auto_relief_active
        and not ctx.price_boost_wallbox_active
        and not ctx.effective_allow_grid
        and _safe_float(ctx.forecast_auto_limit_w, 0.0) > 0.0
    ):
        # Prognose-AUTO ist eine Haltefreigabe, keine blinde Maximalfreigabe.
        forecast_auto_extra_w = max(0.0, free_w * fz)
        forecast_min_w = max(0.0, _safe_float(ctx.forecast_auto_min_w, 0.0))
        if (
            ctx.storage_curve_budget_active
            and free_w > 0.0
            and phys_surplus > forecast_min_w
        ):
            forecast_auto_extra_w = max(
                forecast_auto_extra_w,
                min(free_w, max(0.0, phys_surplus - forecast_min_w)),
            )
        forecast_auto_hold_w = forecast_min_w + forecast_auto_extra_w
        allowed_w = min(
            max(allowed_w, forecast_auto_hold_w),
            _safe_float(ctx.forecast_auto_limit_w, 0.0),
        )
        display_wb_budget_curve_w = max(
            display_wb_budget_curve_w,
            max(0.0, allowed_w - 6.0 * 230.0 * phases),
        )

    if ctx.storage_charge_priority_active and not (
        grid_funded_budget_authority_active
        or ctx.predump_wallbox_active
        or curve_wb_relief_active
        or forecast_auto_relief_active
    ):
        allowed_w = 0.0
    elif (
        not (
            grid_funded_budget_authority_active
            or ctx.price_boost_wallbox_active
            or ctx.grid_unlocked_all_controllable
            or ctx.predump_wallbox_active
        )
        and not native_mode9_batt_start
        and not curve_wb_relief_active
        and (not forecast_auto_relief_active or ctx.storage_curve_budget_active)
        and mode != 4
        and not (
            mode in (9, 10)
            and ctx.wbminsoc_gate_open
            and _safe_float(ctx.wb_storage_cap_w, 0.0) > 0.0
        )
        and _safe_float(ctx.raw_iaval_w, 0.0) < 0.0
    ):
        allowed_w = min(allowed_w, max(0.0, wb_actual + _safe_float(ctx.raw_iaval_w, 0.0)))

    wr_limit = _safe_float(ctx.wr_limit_config_w, 11900.0)
    if wr_limit < 5000.0:
        wr_limit = 11900.0
    if _safe_float(ctx.ac_power_limit_live_w, 0.0) > 1000.0:
        wr_limit = _safe_float(ctx.ac_power_limit_live_w, wr_limit)
    max_phys_wb_w = max(
        0.0,
        wr_limit
        + _safe_float(ctx.ext_pv_power_raw, 0.0)
        - _safe_float(ctx.home_w, 0.0)
        - _safe_float(ctx.grid_reserve_w, 0.0),
    )
    if not (
        ctx.price_boost_wallbox_active
        or ctx.price_optimizing_active
        or ctx.effective_allow_grid
        or ctx.grid_unlocked_all_controllable
    ):
        allowed_w = min(allowed_w, max_phys_wb_w)

    if (
        ctx.storage_curve_budget_active
        and _safe_float(ctx.wallbox_curve_reserve_w, 0.0) > 0.0
        and wb_actual > 500.0
        and not (
            ctx.price_boost_wallbox_active
            or ctx.price_optimizing_active
            or ctx.effective_allow_grid
            or ctx.predump_wallbox_active
            or ctx.storage_charge_priority_active
            or ctx.grid_unlocked_all_controllable
        )
    ):
        reserve_w = max(0.0, _safe_float(ctx.wallbox_curve_reserve_w, 0.0))
        battery_charge_w = max(0.0, _safe_float(ctx.battery_power_raw, 0.0))
        excess_charge_w = max(0.0, battery_charge_w - reserve_w)
        relief_deadband_w = max(250.0, 0.5 * 230.0 * phases)
        if excess_charge_w > relief_deadband_w:
            storage_curve_overcharge_relief_w = excess_charge_w
            storage_curve_overcharge_relief_active = True
            allowed_w = max(
                allowed_w,
                min(max_phys_wb_w, wb_actual + excess_charge_w),
            )

    controlled_floor_pv_only_active = bool(ctx.controlled_wallbox_wbminsoc_pv_only_active)
    controlled_floor_guard_mode_active = bool(
        ctx.controlled_wallbox_wbminsoc_pause
        or controlled_floor_pv_only_active
    )
    if controlled_floor_guard_mode_active:
        floor_phase_count = phases
        floor_min_w = 6.0 * 230.0 * floor_phase_count
        controlled_floor_netpoint_limit_w = max(0.0, allowed_w)
        if controlled_floor_pv_only_active:
            pv_only_limit_w = max(0.0, _safe_float(ctx.pv_surplus_ex_wb_w, 0.0))
            controlled_floor_netpoint_limit_w = min(
                controlled_floor_netpoint_limit_w,
                pv_only_limit_w,
            )
        controlled_floor_battery_discharge_w = max(0.0, -_safe_float(ctx.battery_power_raw, 0.0))
        floor_discharge_threshold_w = max(
            500.0,
            _safe_float(ctx.floor_discharge_threshold_w, 700.0),
        )
        if controlled_floor_battery_discharge_w > floor_discharge_threshold_w and wb_actual > 250.0:
            floor_down_step_w = max(
                230.0 * floor_phase_count,
                _safe_float(ctx.floor_down_step_w, 230.0 * floor_phase_count),
            )
            floor_excess_w = max(0.0, controlled_floor_battery_discharge_w - floor_discharge_threshold_w)
            floor_down_step_w = max(
                floor_down_step_w,
                min(3.0 * 230.0 * floor_phase_count, floor_excess_w * 0.5),
            )
            controlled_floor_netpoint_limit_w = min(
                controlled_floor_netpoint_limit_w,
                max(0.0, wb_actual - floor_down_step_w),
            )
            controlled_floor_battery_guard_active = True
        allowed_w = max(0.0, controlled_floor_netpoint_limit_w)
        display_wb_budget_curve_w = min(
            display_wb_budget_curve_w,
            max(0.0, allowed_w - floor_min_w),
        )
        if allowed_w < max(0.0, floor_min_w - 250.0):
            curve_wb_relief_active = False
            forecast_auto_relief_active = False
    elif ctx.wbminsoc_discharge_taper_active:
        allowed_w = min(allowed_w, _safe_float(ctx.target_wbminsoc_discharge_taper_limit_w, 0.0))
        display_wb_budget_curve_w = min(
            display_wb_budget_curve_w,
            max(
                0.0,
                _safe_float(ctx.target_wbminsoc_discharge_taper_limit_w, 0.0)
                - _safe_float(ctx.target_wbminsoc_low_power_min_w, 0.0),
            ),
        )
    elif (
        ctx.target_wbminsoc_discharge_pull_active
        and _safe_float(ctx.target_wbminsoc_discharge_pull_limit_w, 0.0) > 0.0
    ):
        pull_limit_w = _safe_float(ctx.target_wbminsoc_discharge_pull_limit_w, 0.0)
        if ctx.forecast_auto_battery_assist and _safe_float(ctx.forecast_auto_limit_w, 0.0) > 0.0:
            pull_limit_w = min(pull_limit_w, _safe_float(ctx.forecast_auto_limit_w, 0.0))
        allowed_w = max(allowed_w, pull_limit_w)
        display_wb_budget_curve_w = max(
            display_wb_budget_curve_w,
            max(0.0, pull_limit_w - _safe_float(ctx.target_wbminsoc_low_power_min_w, 0.0)),
        )
    elif ctx.target_wbminsoc_low_power_recovery_active:
        if ctx.target_wbminsoc_low_power_start_ready:
            bootstrap_w = _safe_float(ctx.target_wbminsoc_low_power_bootstrap_w, 0.0)
            low_power_min_w = _safe_float(ctx.target_wbminsoc_low_power_min_w, 0.0)
            allowed_w = min(max(allowed_w, bootstrap_w), bootstrap_w)
            display_wb_budget_curve_w = min(
                max(display_wb_budget_curve_w, low_power_min_w),
                low_power_min_w,
            )
        else:
            allowed_w = 0.0
            display_wb_budget_curve_w = 0.0

    openwb_pro_curve_direct_start_min_w = max(
        6.0 * 230.0,
        _safe_float(ctx.openwb_pro_curve_direct_start_min_w, 6.0 * 230.0),
    )
    openwb_pro_curve_direct_grid_pv_w = 0.0
    if _safe_float(ctx.pv_power_raw, 0.0) > 100.0:
        openwb_pro_curve_direct_grid_pv_w = max(
            0.0,
            _safe_float(ctx.grid_export_room_w, 0.0)
            - _safe_float(ctx.grid_import_for_budget_w, 0.0),
        )
    openwb_pro_curve_direct_storage_soft_release = bool(
        ctx.storage_curve_budget_active
        and ctx.wbminsoc_gate_open
        and _safe_float(ctx.pv_power_raw, 0.0) > 100.0
        and _safe_float(ctx.grid_export_room_w, 0.0) > 0.0
        and _safe_float(ctx.raw_iaval_w, 0.0) >= openwb_pro_curve_direct_start_min_w
    )
    openwb_pro_curve_direct_storage_pv_w = 0.0
    if (
        ctx.storage_curve_budget_active
        and (
            (
                not ctx.storage_charge_priority_active
                and not ctx.openwb_pro_curve_direct_storage_block
            )
            or openwb_pro_curve_direct_storage_soft_release
        )
        and _safe_float(ctx.pv_power_raw, 0.0) > 100.0
    ):
        openwb_pro_curve_direct_storage_pv_w = max(
            0.0,
            _safe_float(ctx.raw_iaval_w, 0.0),
            _safe_float(ctx.free_for_limbs_w, 0.0),
        )
    openwb_pro_curve_direct_candidate_w = max(
        0.0,
        wb_actual
        + _safe_float(ctx.grid_export_room_w, 0.0)
        - _safe_float(ctx.grid_import_for_budget_w, 0.0),
        _safe_float(ctx.pv_surplus_ex_wb_w, 0.0),
        openwb_pro_curve_direct_grid_pv_w,
        openwb_pro_curve_direct_storage_pv_w,
    )
    # Netzpunkt und Storage-Budget können eine bereits laufende, vom Akku
    # gestützte Wallbox wie PV-Überschuss aussehen lassen. Der stationäre
    # openWB-Pro-Sollwert darf deshalb höchstens das batterieneutral belegte
    # Gesamtbudget halten: aktuelle WB-Leistung nach Abzug der gemessenen
    # Batterieentladung oder PV abzüglich Hauslast und Netzreserve. Ein kurzer
    # Übergangspuffer bleibt ausschließlich dem PV-Hybrid-Gate vorbehalten.
    openwb_pro_curve_direct_battery_neutral_w = max(
        0.0,
        _safe_float(ctx.pv_only_allowed_w, 0.0),
        _safe_float(ctx.pv_surplus_ex_wb_w, 0.0),
    )
    openwb_pro_curve_direct_real_pv_w = min(
        openwb_pro_curve_direct_candidate_w,
        openwb_pro_curve_direct_battery_neutral_w,
    )
    openwb_pro_curve_direct_battery_clamp_active = bool(
        openwb_pro_curve_direct_candidate_w
        > openwb_pro_curve_direct_real_pv_w + 1.0
    )
    openwb_pro_curve_direct_pv_start_ready = bool(
        openwb_pro_curve_direct_real_pv_w >= openwb_pro_curve_direct_start_min_w
    )
    openwb_pro_curve_direct_storage_priority_block = bool(
        (
            ctx.storage_charge_priority_active
            or ctx.openwb_pro_curve_direct_storage_block
        )
        and not openwb_pro_curve_direct_pv_start_ready
    )
    dm_blocks_direct = False
    if ctx.direct_marketing_active:
        dm_target = str(ctx.direct_marketing_policy_target_state or "").upper().strip()
        dm_blocks_direct = dm_target != "FORCE_CHARGE_PV"
    openwb_pro_curve_direct_active = bool(
        ctx.budget_ok
        and ctx.effective_public_wb_mode == MODE_CURVE
        and not (
            ctx.price_boost_wallbox_active
            or ctx.price_optimizing_active
            or ctx.effective_allow_grid
            or ctx.predump_wallbox_active
            or curve_wb_relief_active
            or ctx.forecast_auto_battery_assist
            or openwb_pro_curve_direct_storage_priority_block
            or dm_blocks_direct
        )
        and _safe_float(ctx.wallbox_curve_reserve_w, 0.0) <= 0.0
        and ctx.openwb_pro_curve_direct_possible
        and openwb_pro_curve_direct_pv_start_ready
    )
    openwb_pro_curve_direct_w = 0.0
    if openwb_pro_curve_direct_active:
        openwb_pro_curve_direct_w = openwb_pro_curve_direct_real_pv_w
        allowed_w = min(max_phys_wb_w, openwb_pro_curve_direct_w)

    grid_import_budget_clamp_active = bool(
        ctx.budget_ok
        and free_w <= max(120.0, _safe_float(ctx.grid_reserve_w, 0.0))
        and _safe_float(ctx.raw_iaval_w, 0.0) <= max(120.0, _safe_float(ctx.grid_reserve_w, 0.0))
        and ctx.grid_import_budget_down_active
        and wb_actual > 500.0
        and not (
            ctx.price_boost_wallbox_active
            or ctx.price_optimizing_active
            or ctx.effective_allow_grid
            or ctx.predump_wallbox_active
            or curve_wb_relief_active
            or ctx.forecast_auto_battery_assist
        )
    )
    if grid_import_budget_clamp_active:
        allowed_w = min(
            allowed_w,
            max(
                0.0,
                wb_actual
                + _safe_float(ctx.grid_export_room_w, 0.0)
                - _safe_float(ctx.grid_import_w, 0.0),
            ),
        )

    # Nutzer-Aus ist unabhängig von Preis-, Plan- oder Flexbudgetsignalen eine
    # harte Endentscheidung und wird vor allen Ausgangsdeckeln erneut gebunden.
    if mode == MODE_OFF or ctx.effective_public_wb_mode == MODE_OFF:
        allowed_w = 0.0

    peak_shaving_wallbox = peak_shaving_wallbox_power_cap(
        enabled=bool(ctx.peak_shaving_enabled),
        budget_valid=bool(ctx.peak_shaving_budget_valid),
        allowed_remaining_import_w=ctx.peak_shaving_allowed_remaining_import_w,
        base_import_w=ctx.peak_shaving_base_import_w,
        wallbox_actual_w=wb_actual,
    )
    peak_shaving_wallbox_limited = False
    peak_shaving_wallbox_minimum_blocked = False
    peak_shaving_wallbox_minimum_power_w = 6.0 * 230.0 * phases
    if peak_shaving_wallbox.get("active"):
        peak_cap_w = max(
            0.0,
            _safe_float(peak_shaving_wallbox.get("cap_w"), 0.0),
        )
        peak_shaving_wallbox_limited = bool(allowed_w > peak_cap_w + 1.0)
        allowed_w = min(allowed_w, peak_cap_w)
        peak_shaving_wallbox_minimum_blocked = bool(
            allowed_w > 0.0
            and allowed_w < peak_shaving_wallbox_minimum_power_w
        )
    pre_auth_cap = max(0.0, float(allowed_w))
    storage_auth_budget = max(
        0.0,
        _safe_float(
            getattr(ctx, "authorized_wallbox_budget_w", 32.0 * 230.0 * 3.0),
            32.0 * 230.0 * 3.0,
        ),
    )
    curve_pv_allowed_w = max(
        0.0, _safe_float(getattr(ctx, "pv_only_allowed_w", 0.0), 0.0)
    )
    openwb_pro_curve_direct_pv_authority_w = 0.0
    if openwb_pro_curve_direct_active:
        # Der Storage Manager bleibt auch bei real belegtem PV-Export der
        # einzige Budgetgeber. Die batterieneutrale PV-Evidenz ist hier nur
        # eine zusätzliche physische Obergrenze; sie darf einen kleineren oder
        # auf 0 W gesetzten Storage-Vertrag niemals nachträglich vergrößern.
        openwb_pro_curve_direct_pv_authority_w = min(
            pre_auth_cap,
            curve_pv_allowed_w,
            max(0.0, float(openwb_pro_curve_direct_real_pv_w)),
        )
    predump_active = bool(getattr(ctx, "predump_wallbox_active", False))
    predump_gate_open = bool(getattr(ctx, "predump_wallbox_gate_open", False))
    predump_contract_valid = bool(
        predump_active
        and predump_gate_open
        and predump_discharge_add_w > 0.0
        and source_budget_contract.get("predump_contract_valid") is True
    )

    if mode == MODE_OFF or ctx.effective_public_wb_mode == MODE_OFF:
        final_allowed = 0.0
        effective_auth_w = 0.0
    elif grid_funded_budget_authority_active:
        effective_auth_w = pre_auth_cap
        final_allowed = pre_auth_cap

    elif predump_active:
        # Der normale, vom Storage Manager versiegelte Wallboxrahmen bleibt
        # auch während eines Pre-Dump-Kandidaten gültig. Der typisierte
        # Pre-Dump-Vertrag darf ausschließlich zusätzliche Batterieentladung
        # autorisieren; ein geschlossenes/abgelaufenes Gate kann deshalb nie
        # PV-, wbminSoC- oder Startbudget auf einen kleineren Wert schneiden.
        predump_base_auth_w = min(
            storage_auth_budget,
            max(curve_pv_allowed_w, normal_wallbox_support_w),
        )
        if predump_contract_valid:
            effective_auth_w = min(
                storage_auth_budget,
                max(
                    predump_base_auth_w,
                    curve_pv_allowed_w + predump_discharge_add_w,
                ),
            )
        else:
            effective_auth_w = predump_base_auth_w
        final_allowed = min(pre_auth_cap, effective_auth_w)

    elif openwb_pro_curve_direct_active:
        effective_auth_w = min(
            storage_auth_budget,
            openwb_pro_curve_direct_pv_authority_w,
        )
        final_allowed = min(pre_auth_cap, effective_auth_w)

    elif mode == 2:
        effective_auth_w = max(storage_auth_budget, curve_pv_allowed_w)
        final_allowed = min(pre_auth_cap, effective_auth_w)
    else:
        effective_auth_w = storage_auth_budget
        final_allowed = min(pre_auth_cap, storage_auth_budget)



    if peak_shaving_wallbox.get("active"):
        peak_cap_w = max(
            0.0,
            _safe_float(peak_shaving_wallbox.get("cap_w"), 0.0),
        )
        final_allowed = min(final_allowed, peak_cap_w)
        if peak_shaving_wallbox_minimum_blocked:
            final_allowed = 0.0

    auth_budget_limited = bool(final_allowed < pre_auth_cap)

    return {
        "allowed_w": final_allowed,
        "pre_authorized_cap_allowed_w": pre_auth_cap,
        "authorized_wallbox_budget_w": storage_auth_budget,
        "effective_authorized_wallbox_budget_w": effective_auth_w,
        "storage_authorized_wallbox_budget_w": storage_auth_budget,
        "normal_wallbox_support_w": normal_wallbox_support_w,
        "predump_discharge_add_w": predump_discharge_add_w,
        "predump_discharge_contract_valid": bool(predump_contract_valid),
        "wallbox_source_budget_contract_reason": str(
            source_budget_contract.get("reason") or ""
        ),
        "openwb_pro_curve_direct_pv_authority_w": max(
            0.0,
            float(openwb_pro_curve_direct_pv_authority_w),
        ),
        "grid_funded_budget_authority_active": bool(

            grid_funded_budget_authority_active
        ),
        "raw_grid_funded_mode_active": bool(raw_grid_funded_mode_active),
        "authorized_wallbox_budget_limited": auth_budget_limited,
        "display_wb_budget_curve_w": max(0.0, float(display_wb_budget_curve_w)),
        "native_mode9_batt_start": bool(native_mode9_batt_start),
        "mode5_pv_surplus_active": bool(mode5_pv_surplus_active),
        "max_phys_wb_w": max(0.0, float(max_phys_wb_w)),
        "controlled_floor_battery_guard_active": bool(controlled_floor_battery_guard_active),
        "controlled_floor_battery_discharge_w": max(0.0, float(controlled_floor_battery_discharge_w)),
        "controlled_floor_netpoint_limit_w": max(0.0, float(controlled_floor_netpoint_limit_w)),
        "curve_wb_relief_active": bool(curve_wb_relief_active),
        "forecast_auto_relief_active": bool(forecast_auto_relief_active),
        "openwb_pro_curve_direct_active": bool(openwb_pro_curve_direct_active),
        "openwb_pro_curve_direct_w": min(
            final_allowed,
            max(0.0, float(openwb_pro_curve_direct_w)),
        ),
        "openwb_pro_curve_direct_pv_start_ready": bool(openwb_pro_curve_direct_pv_start_ready),
        "openwb_pro_curve_direct_real_pv_w": max(0.0, float(openwb_pro_curve_direct_real_pv_w)),
        "openwb_pro_curve_direct_candidate_w": max(
            0.0,
            float(openwb_pro_curve_direct_candidate_w),
        ),
        "openwb_pro_curve_direct_battery_neutral_w": max(
            0.0,
            float(openwb_pro_curve_direct_battery_neutral_w),
        ),
        "openwb_pro_curve_direct_battery_clamp_active": bool(
            openwb_pro_curve_direct_battery_clamp_active
        ),
        "openwb_pro_curve_direct_start_min_w": max(0.0, float(openwb_pro_curve_direct_start_min_w)),
        "openwb_pro_curve_direct_storage_soft_release": bool(openwb_pro_curve_direct_storage_soft_release),
        "openwb_pro_curve_direct_direct_marketing_block": bool(dm_blocks_direct),
        "grid_import_budget_clamp_active": bool(grid_import_budget_clamp_active),
        "peak_shaving_wallbox_budget_active": bool(
            peak_shaving_wallbox.get("active")
        ),
        "peak_shaving_wallbox_limited": bool(peak_shaving_wallbox_limited),
        "peak_shaving_wallbox_minimum_blocked": bool(
            peak_shaving_wallbox_minimum_blocked
        ),
        "peak_shaving_wallbox_minimum_power_w": float(
            peak_shaving_wallbox_minimum_power_w
        ),
        "peak_shaving_wallbox_cap_w": peak_shaving_wallbox.get("cap_w"),
        "peak_shaving_wallbox_non_wb_base_import_w": peak_shaving_wallbox.get(
            "non_wallbox_base_import_w"
        ),
        "peak_shaving_wallbox_reason": str(
            peak_shaving_wallbox.get("reason") or ""
        ),
        "storage_curve_overcharge_relief_active": bool(storage_curve_overcharge_relief_active),
        "storage_curve_overcharge_relief_w": max(0.0, float(storage_curve_overcharge_relief_w)),
    }
