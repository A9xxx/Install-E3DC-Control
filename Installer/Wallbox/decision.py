"""Reine Entscheidungshilfen für Wallboxen.

Dieses Modul greift bewusst weder auf Dateisystem, Netzwerk, Ramdisk noch
Treiber zu. Der Wallbox-Manager darf Zustände am Rand erfassen; Entscheidungen
über Phasen und physikalische Budgets liegen hier, damit sie isoliert testbar
bleiben.
"""

import math
import re
from typing import Any, Dict, Iterable, Optional

try:
    from .modes import MODE_BATTERY_DEPARTURE, MODE_OFF, MODE_PRICE, mode_label, normalize_wb_mode, storage_floor_mode
except ImportError:  # pragma: no cover - fallback for direct Installer-path imports
    from Wallbox.modes import MODE_BATTERY_DEPARTURE, MODE_OFF, MODE_PRICE, mode_label, normalize_wb_mode, storage_floor_mode

try:
    from .ramps import running_charge_ramp_contract
except ImportError:  # pragma: no cover - fallback for direct Installer-path imports
    from Wallbox.ramps import running_charge_ramp_contract


EFY_AUTONOMOUS_PRODUCT_CAPABILITY = "e3dc_efy_autonomous_solar_product"
EFY_WBCHAR6_AUTONOMOUS_SOURCE = (
    "configured_family_plus_field_verified_wbchar6"
)
EFY_WBCHAR6_PROVENANCE = "field_verified_legacy"


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value).strip() if value is not None else ""
        return float(text) if text else float(default)
    except (TypeError, ValueError):
        return float(default)


CURRENT_OUTPUT_HOLD_ACTIONS = frozenset({
    "HOLD_MIN_CHARGE",
    "HOLD_GRID_WINDOW",
    "HOLD_MULTI_ZERO",
    "HOLD_OPENWB_ZERO",
    "HOLD_NATIVE_RUNNING_CHARGE",
    "HOLD_NATIVE_CURRENT_DOWN",
    "HOLD_CONTROLLABLE_EXPORT_CLOUD",
    "HOLD_NATIVE_NO_STOP_WAIT",
    "HOLD_NATIVE_START_GRACE",
    "HOLD_NATIVE_START_CAP",
    "HOLD_WBMINSOC_FLOOR",
    "HOLD_OPENWB_FINISH_CONFIRM",
    "HOLD_STOP_PV_HYBRID",
})

CURRENT_OUTPUT_EXACT_MINIMUM_HOLD_ACTIONS = frozenset({
    "HOLD_OPENWB_ZERO",
    "HOLD_NATIVE_CURRENT_DOWN",
    "HOLD_NATIVE_NO_STOP_WAIT",
    "HOLD_NATIVE_START_GRACE",
    "HOLD_NATIVE_START_CAP",
    "HOLD_WBMINSOC_FLOOR",
    "HOLD_STOP_PV_HYBRID",
})

CURRENT_OUTPUT_NO_INCREASE_HOLD_ACTIONS = frozenset({
    "HOLD_MIN_CHARGE",
    "HOLD_GRID_WINDOW",
    "HOLD_MULTI_ZERO",
    "HOLD_NATIVE_RUNNING_CHARGE",
    "HOLD_CONTROLLABLE_EXPORT_CLOUD",
    "HOLD_OPENWB_FINISH_CONFIRM",
})


FINAL_STOP_AUTHORITY_SCHEMA = "wallbox_stop_authority_v1"


def final_stop_authority_contract() -> Dict[str, Any]:
    """Bindet einen finalen STOP typisiert an die native Abort-Kante.

    Ob die Kante wirklich gesendet werden darf, bleibt zusätzlich an einen
    frischen, realen Ladebeleg im hardwarenahen E3DC-Guard gebunden.
    """

    return {
        "schema_version": FINAL_STOP_AUTHORITY_SCHEMA,
        "action": "STOP",
        "stop_type": "hard_stop",
        "native_abort_authorized": True,
        "requires_verified_active_charge": True,
    }


def final_output_authority_contract(
    *,
    cap_amp: Any,
    allowed_w: Any,
    priority_forced_stop: bool,
) -> Dict[str, Any]:
    """Versiegelt den finalen Leistungsdeckel vor allen Hardwarepfaden.

    Ein bereits finalisierter Prioritätsstopp darf weder durch einen alten
    Stromdeckel noch durch den Istzustand ``real_charging`` wieder geöffnet
    werden. Gewöhnliche Defizit- und Wh-Haltepfade setzen dieses Signal nicht
    und behalten deshalb ihren berechneten Deckel unverändert.
    """

    authorized_amp = max(0.0, _safe_float(cap_amp, 0.0))
    authorized_w = max(0.0, _safe_float(allowed_w, 0.0))
    forced_zero = bool(priority_forced_stop)
    if forced_zero:
        authorized_amp = 0.0
        authorized_w = 0.0
    return {
        "schema_version": "wallbox_final_output_authority_v1",
        "cap_amp": float(authorized_amp),
        "allowed_w": float(authorized_w),
        "forced_zero": forced_zero,
        "reason": "priority_forced_stop" if forced_zero else "authorized",
    }


def priority_forced_stop_edge_contract(
    *,
    priority_forced_stop: bool,
    charger_connected: bool,
    hw_charging: bool,
    hw_power_w: Any,
    current_amp: Any,
    current_set_amp: Any,
    is_charging_memory: bool,
    stop_already_sent: bool,
    stop_retry_due: bool,
    e3dc_native_toggle: bool,
) -> Dict[str, Any]:
    """Bindet den Prioritätsstopp vor Strom- und Phasen-Hardwarepfaden.

    Ein nicht verbundenes oder bereits stromloses Gerät benötigt keine neue
    Stopkante. Dadurch entsteht insbesondere bei einer openWB Pro kein
    sessionloser temporärer Nullanker. Beim nativen E3DC-Toggle bleibt die
    Kante zusätzlich strikt an frisch belegte reale Ladung beziehungsweise
    einen ausdrücklich fälligen Retry gebunden.
    """

    active = bool(priority_forced_stop)
    connected = bool(charger_connected)
    real_charge = bool(
        connected
        and hw_charging
        and max(0.0, _safe_float(hw_power_w, 0.0)) > 500.0
    )
    offered_or_running = bool(
        connected
        and (
            real_charge
            or is_charging_memory
            or max(0.0, _safe_float(current_amp, 0.0)) > 0.5
            or max(0.0, _safe_float(current_set_amp, 0.0)) > 0.5
        )
    )
    if not active:
        stop_edge_due = False
    elif e3dc_native_toggle:
        stop_edge_due = bool(
            (real_charge and not stop_already_sent)
            or stop_retry_due
        )
    else:
        stop_edge_due = bool(
            (offered_or_running and not stop_already_sent)
            or stop_retry_due
        )
    return {
        "schema_version": "wallbox_priority_forced_stop_edge_v1",
        "active": active,
        "stop_edge_due": bool(stop_edge_due),
        "phase_outputs_allowed": not active,
        "connected": connected,
        "real_charge": real_charge,
        "offered_or_running": offered_or_running,
        "reason": (
            "priority_forced_stop"
            if active
            else "priority_clear"
        ),
    }


def current_output_hold_target_amp(
    action: Any,
    *,
    hold_amp: Any = 0,
    target_amp: Any = 0,
    current_amp: Any = 0,
    current_set_amp: Any = 0,
    min_amp: Any = 6,
    max_amp: Any = 32,
    authorized_target_amp: Any = 0,
) -> float:
    """Liefert nur für bekannte Halteklassen einen positiven Stromdeckel.

    Mindeststrom-Holds dürfen exakt den physikalischen Mindeststrom halten.
    Alle übrigen Holds dürfen einen bereits angebotenen Strom nur beibehalten
    oder absenken. Keine Halteklasse darf einen fehlenden per-WB-Zielstrom
    ersetzen oder über ihn hinaus erhöhen.
    """

    name = str(action or "")
    minimum = max(0.0, _safe_float(min_amp, 6.0))
    maximum = min(
        max(0.0, _safe_float(max_amp, 0.0)),
        max(0.0, _safe_float(authorized_target_amp, 0.0)),
    )
    if name not in CURRENT_OUTPUT_HOLD_ACTIONS or maximum < minimum:
        return 0.0
    if name in CURRENT_OUTPUT_EXACT_MINIMUM_HOLD_ACTIONS:
        return float(minimum)
    if name not in CURRENT_OUTPUT_NO_INCREASE_HOLD_ACTIONS:
        return 0.0

    existing = max(
        0.0,
        _safe_float(current_amp, 0.0),
        _safe_float(current_set_amp, 0.0),
    )
    requested = max(
        0.0,
        _safe_float(hold_amp, 0.0),
        _safe_float(target_amp, 0.0),
    )
    applied = min(existing, requested, maximum)
    return float(applied if applied >= minimum else 0.0)


def hold_current_enforcement_contract(
    action: Any,
    *,
    authorized_amp: Any,
    reported_amp: Any = None,
    current_amp: Any = 0,
    current_set_amp: Any = 0,
    min_amp: Any = 6,
) -> Dict[str, Any]:
    """Entscheidet, ob ein Hold den Hardwarestrom aktiv absenken muss.

    Ein Hold darf eine laufende Ladung zeitlich bewahren, aber niemals ein
    älteres höheres Stromangebot gegen einen inzwischen kleineren
    Zyklusvertrag stehen lassen. Unterhalb des physikalischen Mindeststroms
    wird der Hold zu einem typisierten Stopp; ein höheres bekanntes Angebot
    wird im selben Zyklus auf den autorisierten Wert abgesenkt. Ein Hold
    erhöht den Strom niemals.
    """

    name = str(action or "")
    minimum = max(0.0, _safe_float(min_amp, 6.0))
    target = max(0.0, _safe_float(authorized_amp, 0.0))
    reported_known = reported_amp is not None
    # Ein frischer Hardware-Readback ist die maßgebliche Ist-Evidenz. Ein
    # älterer Manager-Sollwert darf insbesondere ein physisches 4-A-Angebot
    # nicht in einen vermeintlichen 16→6-A-Absenkfall umdeuten und dadurch den
    # Strom tatsächlich erhöhen. Nur ohne frischen Readback bleibt der höchste
    # bekannte Managerwert der konservative Absenkbeleg.
    observed = (
        max(0.0, _safe_float(reported_amp, 0.0))
        if reported_known
        else max(
            0.0,
            _safe_float(current_amp, 0.0),
            _safe_float(current_set_amp, 0.0),
        )
    )
    result = {
        "schema_version": "wallbox_hold_current_enforcement_v1",
        "hold_action": name,
        "authorized_amp": float(target),
        "observed_offer_amp": float(observed),
        "reported_offer_known": bool(reported_known),
        "action": "none",
        "reason": "not_a_current_hold",
    }
    if name not in CURRENT_OUTPUT_HOLD_ACTIONS:
        return result
    if target < minimum:
        if observed >= minimum:
            result.update({
                "action": "stop",
                "target_amp": 0.0,
                "reason": "hold_target_below_minimum",
            })
            return result
        result.update({
            "action": "none",
            "target_amp": 0.0,
            "reason": "standby_hold_below_minimum",
        })
        return result
    if observed > target + 1e-6:
        result.update({
            "action": "set_current",
            "target_amp": float(target),
            "reason": "existing_offer_exceeds_hold_cap",
        })
        return result
    result.update({
        "action": "hold",
        "target_amp": float(target),
        "reason": "existing_offer_within_hold_cap",
    })
    return result


def _format_price_ct(value: Any) -> str:
    try:
        number = float(value)
        if not math.isfinite(number):
            return "--"
        return "%.1f" % number
    except (TypeError, ValueError):
        return "--"


def wallbox_operator_hint_contract(
    public_mode: Any,
    current_price_ct: Any,
    price_limit: Any,
    *,
    mode5_grid_allowed: bool = False,
    price_boost_active: bool = False,
    market_plan_active: bool = False,
    market_plan_action: Any = None,
    scheduled_slot_active: bool = False,
    predump_wallbox_active: bool = False,
    budget_stale: bool = False,
    budget_timeout: bool = False,
    budget_age_s: Any = 0.0,
    house_fuse_limited: bool = False,
    house_fuse_cap_amp: Any = 0,
    connected: bool = True,
    cap_amp: Any = 0,
    battery_departure_active: bool = False,
    battery_departure_blocked: bool = False,
    battery_departure_label: str = "",
    battery_departure_start_label: str = "",
    battery_departure_reason: str = "",
) -> Dict[str, str]:
    """Erzeugt den nutzerseitigen Wallbox-Hinweis ohne Nebenwirkungen."""

    mode = normalize_wb_mode(public_mode)
    price_txt = _format_price_ct(current_price_ct)
    limit_txt = _format_price_ct(price_limit)

    if not connected:
        return {
            "operator_hint": "Kein Fahrzeug verbunden: Einstellungen sind gespeichert, es wird nicht gestartet.",
            "operator_hint_level": "secondary",
            "operator_hint_code": "no_vehicle",
        }
    if mode == MODE_OFF:
        return {
            "operator_hint": "Aus: E3DC-Control sendet keine Start- oder Strombefehle; geplante Ladefenster bleiben blockiert.",
            "operator_hint_level": "secondary",
            "operator_hint_code": "mode_off_blocks_control",
        }
    if scheduled_slot_active:
        return {
            "operator_hint": "Geplanter Lade-Slot aktiv: Netzladen ist gewollt; das Wallbox-Preislimit wird ignoriert.",
            "operator_hint_level": "success",
            "operator_hint_code": "planned_slot_ignores_price_limit",
        }
    if predump_wallbox_active:
        return {
            "operator_hint": "Pre-Dump aktiv: Wallbox nutzt lokalen Überschuss/Speicher, Netzladen bleibt gesperrt.",
            "operator_hint_level": "success",
            "operator_hint_code": "predump_wallbox",
        }
    if market_plan_active:
        action_text = "Negativpreis" if str(market_plan_action or "") == "negative_price_absorb" else "Marktfenster"
        return {
            "operator_hint": "%s aktiv: Wallbox ist durch den Storage-Marktvertrag freigegeben." % action_text,
            "operator_hint_level": "success",
            "operator_hint_code": "market_plan_wallbox_active",
        }
    if price_boost_active:
        return {
            "operator_hint": "Preisfenster aktiv: Wallbox ist für günstigen Netzstrom freigegeben.",
            "operator_hint_level": "success",
            "operator_hint_code": "price_boost_active",
        }

    if mode == MODE_BATTERY_DEPARTURE:
        if battery_departure_blocked:
            if battery_departure_reason == "departure_reached":
                hint = "Akku bis Abfahrt wartet: Abfahrtszeit %s ist erreicht; es wird nicht weiter geladen." % (
                    battery_departure_label or "--:--"
                )
            else:
                hint = "Akku bis Abfahrt wartet: Freigabe ab %s bis %s. Kein Netzladen." % (
                    battery_departure_start_label or "--:--",
                    battery_departure_label or "--:--",
                )
            return {
                "operator_hint": hint,
                "operator_hint_level": "warning",
                "operator_hint_code": "battery_departure_blocked",
            }
        suffix = f" bis {battery_departure_label}" if battery_departure_label else ""
        return {
            "operator_hint": "Akku bis Abfahrt aktiv%s: PV und Hausakku sind bis wbminSoC freigegeben, Netzladen bleibt gesperrt." % suffix,
            "operator_hint_level": "success" if battery_departure_active else "info",
            "operator_hint_code": "battery_departure_active" if battery_departure_active else "battery_departure_ready",
        }

    if mode == MODE_PRICE:
        limit_value = _safe_float(price_limit, -1.0)
        if not math.isfinite(limit_value):
            limit_value = -1.0
        try:
            price_value = float(current_price_ct)
        except (TypeError, ValueError):
            price_value = None
        if price_value is not None and not math.isfinite(price_value):
            price_value = None
        if limit_value <= 0:
            return {
                "operator_hint": "Sofort bis Preislimit wartet: bitte ein Wallbox-Preislimit größer 0 ct/kWh setzen.",
                "operator_hint_level": "warning",
                "operator_hint_code": "price_limit_missing",
            }
        if price_value is None:
            return {
                "operator_hint": "Sofort bis Preislimit wartet: aktueller Strompreis fehlt.",
                "operator_hint_level": "warning",
                "operator_hint_code": "price_missing",
            }
        if not mode5_grid_allowed:
            if int(_safe_float(cap_amp, 0.0)) > 0:
                hint = "Netzladen wartet: aktueller Preis %s ct/kWh > Limit %s ct/kWh; PV/Speicher bleibt erlaubt." % (price_txt, limit_txt)
            else:
                hint = "Sofort wartet: aktueller Preis %s ct/kWh > Limit %s ct/kWh." % (price_txt, limit_txt)
            return {
                "operator_hint": hint,
                "operator_hint_level": "warning",
                "operator_hint_code": "price_limit_wait",
            }
        if limit_value >= 80:
            return {
                "operator_hint": "Sofortladung freigegeben: Preis %s ct/kWh <= Limit %s ct/kWh; Limit ist sehr hoch." % (price_txt, limit_txt),
                "operator_hint_level": "warning",
                "operator_hint_code": "price_limit_high_open",
            }
        return {
            "operator_hint": "Sofortladung freigegeben: aktueller Preis %s ct/kWh <= Limit %s ct/kWh." % (price_txt, limit_txt),
            "operator_hint_level": "success",
            "operator_hint_code": "price_limit_open",
        }

    if budget_timeout:
        return {
            "operator_hint": "Wallbox-Budget fehlt seit %.0f s: Regelung stoppt, bis der Storage Manager wieder frische Daten liefert." % _safe_float(budget_age_s, 0.0),
            "operator_hint_level": "danger",
            "operator_hint_code": "budget_timeout",
        }
    if budget_stale:
        return {
            "operator_hint": "Wallbox-Budget ist %.0f s alt: Regelung drosselt vorsichtig auf Mindeststrom." % _safe_float(budget_age_s, 0.0),
            "operator_hint_level": "warning",
            "operator_hint_code": "budget_stale",
        }
    if house_fuse_limited:
        return {
            "operator_hint": "Hausanschluss-Schutz aktiv: Wallbox wird auf %d A begrenzt." % int(_safe_float(house_fuse_cap_amp, 0.0)),
            "operator_hint_level": "warning",
            "operator_hint_code": "house_fuse_limit",
        }

    return {
        "operator_hint": mode_label(mode),
        "operator_hint_level": "info",
        "operator_hint_code": "mode_status",
    }


def wallbox_detail_status_contract(
    status: Optional[Dict[str, Any]],
    c_data: Optional[Dict[str, Any]] = None,
    *,
    public_mode: Any = 0,
    cap_amp: Any = 0,
    allowed_w: Any = 0,
    budget_stale: bool = False,
    budget_timeout: bool = False,
    mode5_grid_allowed: bool = False,
    scheduled_slot_active: bool = False,
    price_boost_active: bool = False,
    predump_wallbox_active: bool = False,
    wbminsoc_gate_open: bool = True,
    house_fuse_limited: bool = False,
    detected_phases: Any = 1,
    min_amp: Any = 6,
    physical_budget: Optional[Dict[str, Any]] = None,
    battery_departure_state: Optional[Dict[str, Any]] = None,
    primary_phase_warning: Optional[Dict[str, Any]] = None,
    under_acceptance_warning: Optional[Dict[str, Any]] = None,
    car_soc_rule_confirmed: bool = False,
    connected: Optional[bool] = None,
    real_charging: Optional[bool] = None,
    real_power_w: Any = None,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Liefert den kompakten Diagnosezustand eines Wallbox-Slots."""

    st = status if isinstance(status, dict) else {}
    box = c_data if isinstance(c_data, dict) else {}
    physical = physical_budget if isinstance(physical_budget, dict) else {}
    mode = normalize_wb_mode(public_mode)
    connected_value = status_connected(st) if connected is None else bool(connected)
    real_charging_value = status_real_charging(st) if real_charging is None else bool(real_charging)
    real_power_value = status_real_power(st) if real_power_w is None else _safe_float(real_power_w, 0.0)

    def _phase_value() -> int:
        physical_phases = valid_phase_count(physical.get("phases"), 0)
        if physical_phases:
            return physical_phases
        for value in (st.get("phases_target"), st.get("phases_in_use"), detected_phases):
            phases = valid_phase_count(value, 0)
            if phases:
                return phases
        return 1

    phases = _phase_value()
    manager_set_amp = _safe_int(box.get("current_set_amp", 0), 0)
    hardware_offered_amp = _safe_int(st.get("amp", 0), 0)
    cap_value = _safe_int(cap_amp, 0)
    allowed_value_w = _safe_int(allowed_w, 0)
    min_power_w = int(
        _safe_float(
            physical.get("min_power_w"),
            max(1, phases) * max(1, _safe_int(min_amp, 6)) * 230,
        )
    )

    if st and bool(st.get("rscp_error_active", False)):
        err = str(st.get("rscp_last_error") or "RSCP-Fehler ohne Detail")
        return {
            "state": "RSCP Fehler",
            "state_level": "danger",
            "state_reason": "Letzter RSCP-Zugriff fehlgeschlagen: %s" % err,
            "min_power_w": min_power_w,
        }

    if isinstance(primary_phase_warning, dict) and primary_phase_warning:
        result = dict(primary_phase_warning)
        result["min_power_w"] = min_power_w
        return result

    if isinstance(under_acceptance_warning, dict) and under_acceptance_warning:
        result = dict(under_acceptance_warning)
        result["min_power_w"] = min_power_w
        return result

    if real_charging_value:
        return {
            "state": "Lade",
            "state_level": "success",
            "state_reason": "Lädt real mit %.0f W." % real_power_value,
            "min_power_w": min_power_w,
        }

    e3dc_state = str(st.get("e3dc_session_state") or "")
    if e3dc_state in ("starting", "offered", "stopping", "ended", "rscp_error"):
        return {
            "state": str(st.get("e3dc_session_label") or "E3DC"),
            "state_level": str(st.get("e3dc_session_level") or "info"),
            "state_reason": str(st.get("e3dc_session_reason") or ""),
            "min_power_w": min_power_w,
        }

    openwb_pro_state = str(st.get("openwb_pro_session_state") or "")
    if openwb_pro_state in ("starting", "offered", "phase_wait", "stopping", "ended"):
        return {
            "state": str(st.get("openwb_pro_session_label") or "openWB Pro"),
            "state_level": str(st.get("openwb_pro_session_level") or "info"),
            "state_reason": str(st.get("openwb_pro_session_reason") or ""),
            "min_power_w": min_power_w,
        }

    openwb_secondary_state = str(st.get("openwb_secondary_session_state") or "")
    if openwb_secondary_state in ("starting", "offered", "stopping", "ended"):
        return {
            "state": str(st.get("openwb_secondary_session_label") or "openWB"),
            "state_level": str(st.get("openwb_secondary_session_level") or "info"),
            "state_reason": str(st.get("openwb_secondary_session_reason") or ""),
            "min_power_w": min_power_w,
        }

    goe_state = str(st.get("goe_session_state") or "")
    if goe_state in ("starting", "offered", "stopping", "ended"):
        return {
            "state": str(st.get("goe_session_label") or "go-e"),
            "state_level": str(st.get("goe_session_level") or "info"),
            "state_reason": str(st.get("goe_session_reason") or ""),
            "min_power_w": min_power_w,
        }

    if not connected_value:
        return {
            "state": "Idle",
            "state_level": "secondary",
            "state_reason": "Kein Fahrzeug verbunden.",
            "min_power_w": min_power_w,
        }

    if mode == MODE_BATTERY_DEPARTURE and isinstance(battery_departure_state, dict) and battery_departure_state.get("blocked"):
        if battery_departure_state.get("expired"):
            return {
                "state": "Abfahrt erreicht",
                "state_level": "secondary",
                "state_reason": "Abfahrtszeit %s ist erreicht; es wird nicht weiter geladen." % (
                    battery_departure_state.get("departure_time") or "--:--"
                ),
                "min_power_w": min_power_w,
            }
        return {
            "state": "Wartet Startfenster",
            "state_level": "warning",
            "state_reason": "Freigabe ab %s bis %s; kein Netzladen." % (
                battery_departure_state.get("start_time") or "--:--",
                battery_departure_state.get("departure_time") or "--:--",
            ),
            "min_power_w": min_power_w,
        }

    if mode == MODE_OFF:
        return {
            "state": "Aus",
            "state_level": "secondary",
            "state_reason": "Wallbox-Regelung ist aus; E3DC-Control sendet keine Ladebefehle.",
            "min_power_w": min_power_w,
        }

    now_value = _safe_float(now_ts, 0.0)
    if (
        str(box.get("_bev_full_block_reason") or "") == "start_rejected_soft"
        and _safe_float(box.get("_openwb_start_reject_soft_until", 0.0), 0.0) > now_value
    ):
        return {
            "state": "Start wartet",
            "state_level": "warning",
            "state_reason": (
                "Fahrzeug/Wallbox hat die Startfreigabe noch nicht angenommen; "
                "E3DC-Control versucht es nach kurzem Cooldown erneut, sobald Budget da ist."
            ),
            "min_power_w": min_power_w,
        }

    if bool(box.get("_bev_full_blocked", False)):
        block_reason = str(box.get("_bev_full_block_reason") or "")
        if block_reason == "start_rejected":
            return {
                "state": "Start abgelehnt",
                "state_level": "secondary",
                "state_reason": (
                    "Fahrzeug/Wallbox nimmt die Startfreigabe nicht an; "
                    "E3DC-Control startet bis zum nächsten Steckvorgang nicht neu."
                ),
                "min_power_w": min_power_w,
            }
        return {
            "state": "Ladung beendet",
            "state_level": "secondary",
            "state_reason": (
                "Wallbox/Fahrzeug hat die Ladung beendet; E3DC-Control startet "
                "bis zum nächsten Steckvorgang nicht neu."
            ),
            "min_power_w": min_power_w,
        }

    car_soc = _safe_float(st.get("car_soc"), -1.0)
    if car_soc >= 99.5 and bool(car_soc_rule_confirmed) and manager_set_amp <= 0 and hardware_offered_amp <= 0:
        return {
            "state": "Auto voll",
            "state_level": "secondary",
            "state_reason": "Fahrzeug-SoC liegt bei %.0f%%; es wird kein Start erzwungen." % car_soc,
            "min_power_w": min_power_w,
        }

    if budget_timeout:
        return {
            "state": "Kein Budget",
            "state_level": "danger",
            "state_reason": "Kein frisches Wallbox-Budget vom Storage Manager.",
            "min_power_w": min_power_w,
        }

    if budget_stale:
        return {
            "state": "Budget alt",
            "state_level": "warning",
            "state_reason": "Wallbox-Budget ist alt; Regelung wartet vorsichtig.",
            "min_power_w": min_power_w,
        }

    if (
        mode == MODE_PRICE
        and not mode5_grid_allowed
        and not scheduled_slot_active
        and not price_boost_active
        and cap_value <= 0
        and allowed_value_w < min_power_w
    ):
        return {
            "state": "Wartet Preis",
            "state_level": "warning",
            "state_reason": "Preislimit gibt Netzladen aktuell nicht frei; PV-Überschuss liegt unter Mindestleistung.",
            "min_power_w": min_power_w,
        }

    if (
        storage_floor_mode(mode)
        and not mode5_grid_allowed
        and not wbminsoc_gate_open
        and not bool(physical.get("budget_ready", False) or physical.get("can_start_or_hold", False))
    ):
        return {
            "state": "Wartet wbminSoC",
            "state_level": "warning",
            "state_reason": "Hausakku-Floor ist erreicht; Wallbox wartet auf echte PV-Mindestleistung, Preisfenster oder Speicherfreigabe.",
            "min_power_w": min_power_w,
        }

    if house_fuse_limited and cap_value <= 0:
        return {
            "state": "Hauslimit",
            "state_level": "warning",
            "state_reason": "Hausanschluss-Schutz lässt aktuell keine Wallbox-Leistung frei.",
            "min_power_w": min_power_w,
        }

    if bool(physical.get("switch_to_1p_ready")):
        return {
            "state": "Phasenwechsel",
            "state_level": "warning",
            "state_reason": physical.get("reason") or "3p ist zu schwer; 1p-Start wird angefordert.",
            "min_power_w": min_power_w,
        }

    start_permission_active = bool(
        manager_set_amp > 0
        or cap_value > 0
        or scheduled_slot_active
        or price_boost_active
        or predump_wallbox_active
    )
    if start_permission_active:
        amp = max(manager_set_amp, cap_value, hardware_offered_amp)
        return {
            "state": "Startfreigabe",
            "state_level": "warning",
            "state_reason": "%d A freigegeben; Fahrzeug/Wallbox nimmt noch keine Leistung ab." % max(0, amp),
            "min_power_w": min_power_w,
        }

    if not bool(physical.get("budget_ready", allowed_value_w >= min_power_w)):
        return {
            "state": "Wartet Mindestleistung",
            "state_level": "warning",
            "state_reason": physical.get("reason") or "Budget %d W < Mindestleistung ca. %d W (%d ph)." % (
                max(0, allowed_value_w),
                min_power_w,
                phases,
            ),
            "min_power_w": min_power_w,
        }

    return {
        "state": "Angesteckt",
        "state_level": "info",
        "state_reason": "Fahrzeug ist verbunden; Regelung wartet auf Startbedingung.",
        "min_power_w": min_power_w,
    }


def _current_step(value: Any, default: float = 1.0) -> float:
    step = _safe_float(value, default)
    if step <= 0.11:
        return 0.1
    if step <= 0.51:
        return 0.5
    return 1.0


def _amp_value(value: float, step: float) -> float:
    if step >= 0.99:
        return int(round(value))
    return round(float(value), 1)


def compact_vehicle_identifier(value: Any) -> str:
    return "".join(ch for ch in str(value or "").lower() if ch.isalnum())


VEHICLE_PROFILE_ID_KEYS = (
    "id",
    "key",
    "profile_id",
    "cloud_vehicle_id",
    "vehicle_id",
    "vehicle_mac",
    "mac",
    "rfid",
    "rfid_tag",
)



def confirmed_session_vehicle_identity(
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Liefere ausschließlich eine belastbar sitzungsgebundene Fahrzeug-ID.

    Fahrzeugname, SoC-Profil, Cloud-SoC und die statische ``wbX_car_id``-
    Zuordnung sind keine Identität der aktuellen Stecksession. Ein Treiber darf
    eine aktuelle ID/RFID mit ``stable_vehicle_identity_current`` bestätigen.
    Für eine explizite UI-Zuordnung steht ein fail-closed Hook bereit: Schlüssel
    und Stecksession müssen bestätigt sein und exakt zusammenpassen.
    """

    st = status if isinstance(status, dict) else {}
    live_key = compact_vehicle_identifier(
        st.get("vehicle_id")
        or st.get("rfid_tag")
        or st.get("car_id")
    )
    if st.get("stable_vehicle_identity_current") is True and live_key:
        return {
            "confirmed": True,
            "key": live_key,
            "source": "stable_vehicle_identity_current",
            "session_bound": True,
        }

    explicit_key = compact_vehicle_identifier(
        st.get("session_vehicle_identity_key")
        or st.get("session_vehicle_id")
    )
    binding_id = str(
        st.get("session_vehicle_identity_plug_session_id")
        or st.get("session_vehicle_identity_session_id")
        or ""
    ).strip()
    plug_session_id = str(st.get("plug_session_id") or "").strip()
    if (
        st.get("session_vehicle_identity_confirmed") is True
        and explicit_key
        and (
            not (binding_id or plug_session_id)
            or (binding_id and plug_session_id and binding_id == plug_session_id)
        )
    ):
        return {
            "confirmed": True,
            "key": explicit_key,
            "source": "confirmed_session_binding",
            "session_bound": True,
        }


    return {
        "confirmed": False,
        "key": "",
        "source": "vehicle_identity_unconfirmed",
        "session_bound": False,
    }


def _vehicle_profile_for_identity(
    profiles: Optional[Iterable[Dict[str, Any]]],
    identity_key: Any,
) -> Optional[Dict[str, Any]]:
    compact_identity = compact_vehicle_identifier(identity_key)
    if not compact_identity:
        return None
    matches = []
    for profile in profiles or []:
        if not isinstance(profile, dict):
            continue
        aliases = {
            compact_vehicle_identifier(profile.get(key))
            for key in VEHICLE_PROFILE_ID_KEYS
            if str(profile.get(key) or "").strip()
        }
        if compact_identity in aliases:
            matches.append(profile)
    return matches[0] if len(matches) == 1 else None


def _vehicle_profile_phase_switch_policy(profile: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Liefert nur ein ausdrücklich konfiguriertes Fahrzeug-Veto.

    openWB behandelt ein Fahrzeugprofil mit ``prevent_phase_switch`` als
    Hardware-Schutz. Ältere E3DC-Control-Profile besitzen dieses optionale
    Feld noch nicht; fehlende Angaben werden deshalb nicht als Freigabe und
    auch nicht als Verbot erfunden.
    """

    data = profile if isinstance(profile, dict) else {}

    def _bool_value(value: Any) -> Optional[bool]:
        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        text = str(value or "").strip().lower()
        if text in {"1", "true", "yes", "on", "ja"}:
            return True
        if text in {"0", "false", "no", "off", "nein"}:
            return False
        return None

    if "prevent_phase_switch" in data:
        prevented = _bool_value(data.get("prevent_phase_switch"))
        if prevented is not None:
            return {
                "phase_switch_allowed": not prevented,
                "phase_switch_policy_source": "vehicle_profile_prevent_phase_switch",
            }
    for key in (
        "phase_switch_allowed",
        "allow_phase_switch",
        "phase_switch_after_start_allowed",
    ):
        if key not in data:
            continue
        allowed = _bool_value(data.get(key))
        if allowed is not None:
            return {
                "phase_switch_allowed": allowed,
                "phase_switch_policy_source": f"vehicle_profile_{key}",
            }
    return {
        "phase_switch_allowed": None,
        "phase_switch_policy_source": "vehicle_profile_policy_missing",
    }


def vehicle_phase_capability_from_profiles(
    profiles: Optional[Iterable[Dict[str, Any]]],
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    charger_id: int = 1,
) -> Dict[str, Any]:
    """Belegt Fahrzeugphasen aus Session, Fahrerprofil oder Wallbox-Fahrzeugkonfiguration."""

    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    try:
        cid = int(charger_id or 1)
    except (TypeError, ValueError):
        cid = 1

    identity = confirmed_session_vehicle_identity(st)
    result = {
        "contract": "vehicle_phase_capability_v1",
        "active": False,
        "identity_confirmed": bool(identity.get("confirmed", False)),
        "identity_source": str(identity.get("source") or ""),
        "session_bound": bool(identity.get("session_bound", False)),
        "profile_match": False,
        "phase_count": 0,
        "phase_source": "none",
        "phase_switch_allowed": None,
        "phase_switch_policy_source": "vehicle_profile_unbound",
        "reason": "vehicle_identity_unconfirmed",
    }

    # Evidenzstufe 2: Aktuell bestätigtes Sitzungs-/Fahrerprofil
    if identity.get("confirmed", False):
        profile = _vehicle_profile_for_identity(profiles, identity.get("key"))
        if profile:
            result["profile_match"] = True
            result.update(_vehicle_profile_phase_switch_policy(profile))
            for key in (
                "max_phases",
                "obc_max_phases",
                "phases",
                "ac_phases",
                "charge_phases",
                "charging_phases",
            ):
                phases = valid_phase_count(profile.get(key), 0)
                if phases:
                    result.update({
                        "active": True,
                        "phase_count": int(phases),
                        "phase_source": "confirmed_session_vehicle_profile",
                        "reason": "confirmed_session_vehicle_phase_profile",
                    })
                    return result
            result["reason"] = "confirmed_vehicle_explicit_phase_missing"
            return result

    # Evidenzstufe 3: Ausdrücklich ausgewähltes und WB-zugeordnetes Fahrzeugprofil
    configured_key = (
        cfg.get(f"wb{cid}_car_id")
        or cfg.get(f"wb{cid}_vehicle_id")
        or cfg.get(f"wb{cid}_selected_car_id")
        or cfg.get(f"wb{cid}_car_profile")
        or ""
    )
    if configured_key:
        profile = _vehicle_profile_for_identity(profiles, configured_key)
        if not profile and isinstance(profiles, (list, tuple)):
            norm_key = str(configured_key).strip().lower()
            for p in profiles:
                if not isinstance(p, dict):
                    continue
                p_id = str(p.get("id") or "").strip().lower()
                p_name = str(p.get("name") or "").strip().lower()
                if norm_key in (p_id, p_name):
                    profile = p
                    break
        if profile:
            result["profile_match"] = True
            result["identity_confirmed"] = False
            result["identity_source"] = "configured_selected_vehicle_profile"
            result.update(_vehicle_profile_phase_switch_policy(profile))
            for key in (
                "max_phases",
                "obc_max_phases",
                "phases",
                "ac_phases",
                "charge_phases",
                "charging_phases",
            ):
                phases = valid_phase_count(profile.get(key), 0)
                if phases:
                    result.update({
                        "active": True,
                        "phase_count": int(phases),
                        "phase_source": "configured_selected_vehicle_profile",
                        "reason": "configured_selected_vehicle_phase_profile",
                    })
                    return result
            result["reason"] = "configured_vehicle_explicit_phase_missing"
            return result

    for key in (f"wb{cid}_obc_max_phases",):
        phases = valid_phase_count(cfg.get(key), 0)
        if phases:
            result.update({
                "active": True,
                "identity_confirmed": True,
                "identity_source": "configured_wallbox_obc_max_phases",
                "phase_count": int(phases),
                "phase_source": "configured_wallbox_obc_max_phases",
                "reason": "configured_wallbox_obc_max_phases",
            })
            return result

    return result


def vehicle_control_pilot_interruption_capability_from_profiles(
    profiles: Optional[Iterable[Dict[str, Any]]],
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    charger_id: int = 1,
) -> Dict[str, Any]:
    """Binde automatisches CP-Wakeup an das wirksame Fahrzeugprofil.

    Die openWB-Pro-Hardwarefläche und die Fahrzeuganforderung sind getrennte
    Verträge. Eine bestätigte Live-ID hat Vorrang. Fehlt die optionale
    Fahrzeugerkennung, darf wie im openWB-Core das eindeutig dem Ladepunkt
    zugeordnete Profil gelten, jedoch nur bei frischem Steckstatus und ohne
    widersprechende Live-ID.
    """

    st = status if isinstance(status, dict) else {}
    cfg = config if isinstance(config, dict) else {}
    try:
        cid = int(charger_id or 1)
    except (TypeError, ValueError):
        cid = 1
    identity = confirmed_session_vehicle_identity(st)
    result = {
        "contract": "vehicle_control_pilot_interruption_capability_v1",
        "active": False,
        "identity_confirmed": bool(identity.get("confirmed", False)),
        "identity_source": str(identity.get("source") or ""),
        "session_bound": bool(identity.get("session_bound", False)),
        "profile_match": False,
        "assignment_bound": False,
        "plug_bound": False,
        "source": "vehicle_profile_unbound",
        "reason": "vehicle_identity_unconfirmed",
    }
    profile = None
    if (
        identity.get("confirmed") is True
        and identity.get("session_bound") is True
    ):
        profile = _vehicle_profile_for_identity(profiles, identity.get("key"))
        if not profile:
            result["reason"] = "confirmed_vehicle_profile_not_unique"
            return result
        result["source"] = "confirmed_session_vehicle_profile"
    else:
        connected = bool(
            st.get("plug_state")
            or st.get("plug")
            or st.get("connected")
            or st.get("car") == 2
        )
        fresh = bool(
            st.get("driver_status_valid") is True
            and st.get("driver_status_stale") is False
            and st.get("driver_status_degraded") is not True
            and st.get("driver_status_glitch") is not True
        )
        if not connected or not fresh:
            result["reason"] = (
                "configured_vehicle_requires_fresh_plug_status"
            )
            return result
        configured_key = (
            cfg.get(f"wb{cid}_car_id")
            or cfg.get(f"wb{cid}_vehicle_id")
            or cfg.get(f"wb{cid}_selected_car_id")
            or cfg.get(f"wb{cid}_car_profile")
            or ""
        )
        if not str(configured_key).strip():
            result["reason"] = "configured_vehicle_profile_missing"
            return result
        profile = _vehicle_profile_for_identity(profiles, configured_key)
        if not profile and isinstance(profiles, (list, tuple)):
            normalized_key = str(configured_key).strip().lower()
            matches = [
                candidate
                for candidate in profiles
                if isinstance(candidate, dict)
                and normalized_key
                in {
                    str(candidate.get("id") or "").strip().lower(),
                    str(candidate.get("name") or "").strip().lower(),
                }
            ]
            profile = matches[0] if len(matches) == 1 else None
        if not profile:
            result["reason"] = "configured_vehicle_profile_not_unique"
            return result
        live_key = compact_vehicle_identifier(
            st.get("vehicle_id") or st.get("rfid_tag") or st.get("car_id")
        )
        profile_aliases = {
            compact_vehicle_identifier(profile.get(key))
            for key in VEHICLE_PROFILE_ID_KEYS
            if str(profile.get(key) or "").strip()
        }
        if live_key and live_key not in profile_aliases:
            result["reason"] = "configured_vehicle_conflicts_with_live_identity"
            return result
        result.update({
            "identity_source": "configured_selected_vehicle_profile",
            "assignment_bound": True,
            "plug_bound": True,
            "source": "configured_wallbox_vehicle_profile",
        })
    result["profile_match"] = True
    raw = profile.get("control_pilot_interruption")
    if isinstance(raw, bool):
        enabled = raw
    elif isinstance(raw, (int, float)):
        enabled = bool(raw)
    else:
        text = str(raw or "").strip().lower()
        if text in {"1", "true", "yes", "on", "ja"}:
            enabled = True
        elif text in {"0", "false", "no", "off", "nein"}:
            enabled = False
        else:
            result["reason"] = "vehicle_profile_cp_requirement_missing"
            return result
    result.update({
        "active": bool(enabled),
        "reason": (
            "vehicle_profile_requires_cp_interruption"
            if enabled
            else "vehicle_profile_does_not_require_cp_interruption"
        ),
    })
    return result


def vehicle_current_capability_from_profiles(
    profiles: Optional[Iterable[Dict[str, Any]]],
    status: Optional[Dict[str, Any]] = None,
    *,
    phase_count: Any = 0,
) -> Dict[str, Any]:
    """Liefere den phasenabhängigen OBC-Stromdeckel der aktuellen Session.

    Nur ein eindeutig getroffenes Profil mit ausdrücklich bestätigten
    Stromgrenzen ist autoritativ. Alte Gesamtleistungs-/``max_phases``-Felder
    bleiben Planungsinformationen und werden hier bewusst nicht in Ampere
    umgerechnet.
    """

    phases = valid_phase_count(phase_count, 0)
    identity = confirmed_session_vehicle_identity(status)
    result = {
        "contract": "vehicle_phase_current_cap_v1",
        "active": False,
        "identity_confirmed": bool(identity.get("confirmed", False)),
        "identity_source": str(identity.get("source") or ""),
        "profile_match": False,
        "limits_confirmed": False,
        "phase_count": int(phases),
        "cap_amp": None,
        "cap_source": "none",
        "reason": "vehicle_identity_unconfirmed",
    }
    if not identity.get("confirmed", False):
        return result
    if phases not in (1, 2, 3):
        result["reason"] = "effective_phase_unknown"
        return result

    profile = _vehicle_profile_for_identity(profiles, identity.get("key"))
    if not profile:
        result["reason"] = "confirmed_vehicle_profile_not_unique"
        return result
    result["profile_match"] = True

    limits_confirmed = profile.get("obc_current_limits_confirmed") is True
    result["limits_confirmed"] = limits_confirmed
    if not limits_confirmed:
        result["reason"] = "vehicle_current_limits_unconfirmed"
        return result

    field = "obc_max_current_%dp_a" % phases
    try:
        cap_amp = float(str(profile.get(field, "")).replace(",", "."))
    except (TypeError, ValueError):
        cap_amp = 0.0
    if not math.isfinite(cap_amp) or cap_amp < 6.0:
        result["reason"] = "phase_current_limit_missing"
        return result

    result.update({
        "active": True,
        "cap_amp": float(cap_amp),
        "cap_source": field,
        "reason": "confirmed_session_vehicle_phase_cap",
    })
    return result


def valid_phase_count(value: Any, default: int = 0) -> int:
    try:
        phases = int(float(value))
        if phases in (1, 2, 3):
            return phases
    except (TypeError, ValueError):
        pass
    return default


def status_connected(status: Optional[Dict[str, Any]]) -> bool:
    """Ist nur bei einem realen Stecker-/Fahrzeugsignal wahr, nicht bei Sollstrom."""

    if not status:
        return False
    try:
        if bool(status.get("plug_state", False)):
            return True
        if bool(status.get("car_connected_rscp", False)):
            return True
        return int(status.get("car", 1) or 1) >= 2
    except Exception:
        return False


def _alg_flags(status: Optional[Dict[str, Any]]) -> Optional[int]:
    if not status:
        return None
    for key in ("alg_flags", "extern_alg_flags"):
        try:
            if status.get(key) is not None:
                return int(float(status.get(key)))
        except (TypeError, ValueError):
            pass
    text = str(status.get("extern_alg_hex", "") or "").strip()
    if not text:
        return None
    try:
        raw = bytes.fromhex(text)
    except ValueError:
        return None
    if len(raw) < 3:
        return None
    return int(raw[2])


def _charge_energy_sample(status: Optional[Dict[str, Any]], now_ts: float) -> Dict[str, Any]:
    st = status or {}
    source = ""
    value_wh = None
    for key, scale, name in (
        ("session_kwh", 1000.0, "session_kwh"),
        ("total_kwh", 1000.0, "total_kwh"),
        ("imported_total_wh", 1.0, "imported_total_wh"),
        ("daily_imported_wh", 1.0, "daily_imported_wh"),
    ):
        raw = st.get(key)
        if raw is None or str(raw).strip() == "":
            continue
        try:
            value_wh = _safe_float(raw, 0.0) * scale
            source = name
            break
        except Exception:
            continue
    if value_wh is None:
        return {"source": "", "value_wh": None, "ts": float(now_ts or 0.0)}
    return {"source": source, "value_wh": float(value_wh), "ts": float(now_ts or 0.0)}


def charge_observation_contract(
    status: Optional[Dict[str, Any]],
    previous: Optional[Dict[str, Any]] = None,
    *,
    now_ts: Any = 0,
    min_power_w: Any = 500.0,
) -> Dict[str, Any]:
    """Normalisiert belastbare Ladebelege in einen Wahrheitsvertrag.

    ``charging`` ist nur wahr, wenn die Wallbox ein vertrauenswürdiges
    Hardwaresignal zusammen mit einer aussagekräftigen Messleistung liefert
    oder der Treiber die Phasenleistung bereits bestätigt hat. Angebotener
    Strom und veraltete PM-Werte bleiben sichtbar, zählen aber nicht als reales
    Laden.
    """

    st = status or {}
    now = _safe_float(now_ts, 0.0)
    threshold_w = max(50.0, _safe_float(min_power_w, 500.0))
    status_valid = bool(
        isinstance(status, dict)
        and status
        and status.get("driver_status_valid") is not False
        and status.get("driver_status_stale") is not True
        and status.get("driver_status_plausible") is not False
        and status.get("driver_status_glitch") is not True
        and status.get("valid") is not False
        and status.get("stale") is not True
    )
    connected = status_connected(st)
    rscp_error = bool(st.get("rscp_error_active", False))
    enabled = st.get("enabled")
    disabled = enabled is False

    phase_values = []
    for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w"):
        try:
            phase_values.append(max(0.0, float(st.get(key, 0.0) or 0.0)))
        except (TypeError, ValueError):
            phase_values.append(0.0)
    phase_sum_w = sum(phase_values)
    try:
        reported_phase_sum_w = max(0.0, float(st.get("phase_power_sum_w", 0.0) or 0.0))
    except (TypeError, ValueError):
        reported_phase_sum_w = 0.0
    phase_sum_w = max(phase_sum_w, reported_phase_sum_w)
    measured_phases = sum(1 for value in phase_values if value > 250.0)
    if measured_phases <= 0 and bool(st.get("phase_power_verified", False)) and phase_sum_w > threshold_w:
        measured_phases = 1
    phase_power_verified = bool(st.get("phase_power_verified", False) and phase_sum_w > threshold_w and measured_phases >= 1)

    reported_power_w = 0.0
    for key in ("real_power_w", "power_w"):
        try:
            reported_power_w = max(reported_power_w, float(st.get(key, 0.0) or 0.0))
        except (TypeError, ValueError):
            pass
    raw_power_w = max(reported_power_w, phase_sum_w)

    flags = _alg_flags(st)
    alg_seen = flags is not None or bool(st.get("alg_seen", False))
    alg_charging = bool(st.get("alg_charging", False))
    alg_connected = bool(st.get("alg_connected", False))
    if flags is not None:
        alg_charging = bool(flags & 0b00100000)
        alg_connected = bool(flags & 0b00001000)
    device_working = bool(st.get("device_working", False))
    charge_state = bool(st.get("charge_state", False))
    generic_charging = bool(st.get("charging", False))
    driver_variant = str(st.get("driver_variant", "") or "")
    is_e3dc = driver_variant.startswith("e3dc") or alg_seen or "rscp_wb_index" in st
    if alg_seen:
        hardware_charging = bool(alg_charging or device_working)
    elif is_e3dc:
        hardware_charging = bool(device_working)
    else:
        hardware_charging = bool(generic_charging or charge_state)

    sample = _charge_energy_sample(st, now)
    prev = previous if isinstance(previous, dict) else {}
    energy_delta_wh = 0.0
    energy_delta_s = 0.0
    energy_increasing = False
    if (
        sample.get("source")
        and prev.get("source") == sample.get("source")
        and sample.get("value_wh") is not None
        and prev.get("value_wh") is not None
    ):
        energy_delta_wh = max(0.0, _safe_float(sample.get("value_wh"), 0.0) - _safe_float(prev.get("value_wh"), 0.0))
        energy_delta_s = max(0.0, _safe_float(sample.get("ts"), 0.0) - _safe_float(prev.get("ts"), 0.0))
        energy_increasing = bool(energy_delta_s <= 300.0 and energy_delta_wh >= 2.0)

    truth = "not_charging"
    confidence = "verified"
    source = "no_verified_power"
    power_w = 0.0
    phantom_power_w = 0.0

    if not status_valid:
        truth = "unknown"
        confidence = "unknown"
        source = str(st.get("driver_status_reason") or "driver_status_unknown")
    elif rscp_error:
        truth = "unknown"
        confidence = "unknown"
        source = "rscp_error"
    elif not connected and not alg_connected and raw_power_w <= threshold_w:
        source = "disconnected"
    elif disabled and not phase_power_verified:
        source = "disabled_abort"
        phantom_power_w = raw_power_w if raw_power_w > 50.0 else 0.0
    elif phase_power_verified:
        truth = "charging"
        source = "alg_phase_power" if alg_charging else ("device_working_phase_power" if device_working else "phase_power")
        power_w = phase_sum_w
    elif hardware_charging and raw_power_w > threshold_w:
        truth = "charging"
        source = "alg_power" if alg_charging else ("device_working_power" if device_working else "hardware_power")
        power_w = raw_power_w
    elif energy_increasing and hardware_charging:
        truth = "charging"
        confidence = "meter_delta"
        source = "energy_delta_hardware"
        power_w = raw_power_w if raw_power_w > 50.0 else 0.0
    elif raw_power_w > 50.0:
        source = "phantom_power_rejected"
        phantom_power_w = raw_power_w
    elif connected or alg_connected:
        source = "connected_no_power"

    is_charging = truth == "charging"
    return {
        "truth": truth,
        "is_charging": bool(is_charging),
        "counts_as_real_charge": bool(is_charging),
        "confidence": confidence,
        "source": source,
        "connected": bool(connected or alg_connected),
        "hardware_charging": bool(hardware_charging),
        "alg_seen": bool(alg_seen),
        "alg_charging": bool(alg_charging),
        "alg_connected": bool(alg_connected),
        "device_working": bool(device_working),
        "charge_state": bool(charge_state),
        "generic_charging": bool(generic_charging),
        "phase_power_verified": bool(phase_power_verified),
        "measured_phases": int(measured_phases),
        "power_w": float(power_w if is_charging else 0.0),
        "raw_power_w": float(raw_power_w),
        "phase_power_sum_w": float(phase_sum_w),
        "phantom_power_w": float(phantom_power_w if not is_charging else 0.0),
        "energy_increasing": bool(energy_increasing),
        "energy_delta_wh": float(energy_delta_wh),
        "energy_delta_s": float(energy_delta_s),
        "energy_source": str(sample.get("source") or ""),
        "energy_sample": sample,
        "rscp_error": bool(rscp_error),
        "status_valid": bool(status_valid),
    }


def status_real_power(status: Optional[Dict[str, Any]]) -> float:
    """Liefert gemessene Wallbox-Leistung, niemals aus dem Sollstrom abgeleitet."""

    if not status:
        return 0.0
    contract = status.get("charge_contract") if isinstance(status.get("charge_contract"), dict) else None
    if contract is None:
        contract = charge_observation_contract(status)
    if str(contract.get("truth") or "") != "charging":
        return 0.0
    return max(0.0, _safe_float(contract.get("power_w"), 0.0))


def status_real_charging(status: Optional[Dict[str, Any]]) -> bool:
    """Laden gilt nur, wenn der Beobachtungsvertrag es belegt."""

    if not status:
        return False
    contract = status.get("charge_contract") if isinstance(status.get("charge_contract"), dict) else None
    if contract is None:
        contract = charge_observation_contract(status)
    return bool(contract.get("is_charging", False))


def status_charge_truth(status: Optional[Dict[str, Any]]) -> str:
    """Liefert charging/not_charging/unknown aus dem normalisierten Vertrag."""

    if not status:
        return "not_charging"
    contract = status.get("charge_contract") if isinstance(status.get("charge_contract"), dict) else None
    if contract is None:
        contract = charge_observation_contract(status)
    return str(contract.get("truth") or "not_charging")


def transient_hold_contract(
    status: Optional[Dict[str, Any]] = None,
    *,
    charger_connected: Optional[bool] = None,
    hw_charging: bool = False,
    hw_power_w: Any = 0.0,
    current_amp: Any = 0,
    current_set_amp: Any = 0,
    offered_amp: Any = None,
    phase_capable: bool = False,
    phase_target: Any = 0,
    phase_actual: Any = 0,
    phases_in_use: Any = 0,
    phase_command_age_s: Any = None,
    phase_command_grace_s: Any = 120.0,
    phase_wait_active: bool = False,
    start_hold_active: bool = False,
    native_start_grace_active: bool = False,
    vehicle_finished_drop_pending: bool = False,
    priority_forced_stop: bool = False,
    mode_off: bool = False,
    budget_timeout: bool = False,
) -> Dict[str, Any]:
    """Zentraler Vertrag für Übergangsfenster beim Start und Phasenwechsel.

    Zeitstempel und Treiberaufrufe bleiben beim Manager. Diese Hilfe entscheidet,
    ob ein vorübergehender 0-W-Messwert zu einem befohlenen Übergang gehört und
    daher nicht als normale Stoppbedingung ausgelegt werden darf.
    """

    st = status or {}
    connected = status_connected(st) if charger_connected is None else bool(charger_connected)
    real_charging = bool(hw_charging or status_real_charging(st))
    real_power = max(_safe_float(hw_power_w, 0.0), status_real_power(st))
    current = max(0.0, _safe_float(current_amp, 0.0))
    set_amp = max(0.0, _safe_float(current_set_amp, 0.0))
    if offered_amp is None:
        offered = max(
            _safe_float(st.get("amp"), 0.0),
            _safe_float(st.get("offered_current_raw"), 0.0),
            _safe_float(st.get("offered_current"), 0.0),
        )
    else:
        offered = max(0.0, _safe_float(offered_amp, 0.0))

    target = valid_phase_count(phase_target, 0) or valid_phase_count(st.get("phases_target"), 0)
    actual = valid_phase_count(phase_actual, 0) or valid_phase_count(st.get("phases_actual"), 0)
    in_use = valid_phase_count(phases_in_use, 0) or valid_phase_count(st.get("phases_in_use"), 0)
    if not in_use:
        in_use = valid_phase_count(st.get("number_phases"), 0)

    command_age = _safe_float(phase_command_age_s, 999999.0)
    grace_s = max(30.0, _safe_float(phase_command_grace_s, 120.0))
    recent_phase_command = bool(command_age >= 0.0 and command_age <= grace_s)
    active_or_offered = bool(
        real_charging
        or real_power > 500.0
        or current >= 5.5
        or set_amp >= 5.5
        or offered >= 5.5
    )
    phase_status_unsettled = bool(
        target in (1, 3)
        and (
            not real_charging
            or (in_use in (1, 2, 3) and in_use != target)
            or (actual in (1, 2, 3) and actual != target)
        )
    )
    hard_abort = bool(priority_forced_stop or mode_off or budget_timeout or not connected)
    phase_transition_active = bool(
        not hard_abort
        and phase_capable
        and target in (1, 3)
        and (phase_wait_active or recent_phase_command)
        and (phase_wait_active or phase_status_unsettled or active_or_offered)
    )
    start_transition_active = bool(
        not hard_abort
        and connected
        and (start_hold_active or native_start_grace_active)
    )
    vehicle_finished_drop_active = bool(
        not hard_abort
        and connected
        and vehicle_finished_drop_pending
        and active_or_offered
    )
    active = bool(
        phase_transition_active
        or start_transition_active
        or vehicle_finished_drop_active
    )

    reason = "inactive"
    if hard_abort:
        reason = "hard_abort"
    elif phase_transition_active and phase_wait_active:
        reason = "phase_wait"
    elif phase_transition_active:
        reason = "phase_command"
    elif start_hold_active:
        reason = "start_hold"
    elif native_start_grace_active:
        reason = "native_start_grace"
    elif vehicle_finished_drop_active:
        reason = "vehicle_finished_drop_confirmation"

    return {
        "schema_version": "wallbox_transient_hold_v1",
        "active": bool(active),
        "reason": reason,
        "zero_power_neutral": bool(active),
        "phase_transition_grace_active": bool(phase_transition_active),
        "phase_transition_offer_active": bool(phase_transition_active and active_or_offered),
        "start_hold_active": bool(start_hold_active and not hard_abort and connected),
        "native_start_grace_active": bool(native_start_grace_active and not hard_abort and connected),
        "vehicle_finished_drop_pending_active": bool(
            vehicle_finished_drop_active
        ),
        "recent_phase_command": bool(recent_phase_command),
        "phase_wait_active": bool(phase_wait_active),
        "phase_status_unsettled": bool(phase_status_unsettled),
        "active_or_offered": bool(active_or_offered),
        "hard_abort": bool(hard_abort),
        "charger_connected": bool(connected),
        "target_phases": int(target or 0),
        "actual_phases": int(actual or 0),
        "phases_in_use": int(in_use or 0),
        "phase_command_age_s": float(command_age),
        "phase_command_grace_s": float(grace_s),
        "offered_amp": float(offered),
        "current_amp": float(current),
        "current_set_amp": float(set_amp),
        "hw_charging": bool(real_charging),
        "hw_power_w": float(real_power),
    }


def e3dc_native_production_contract(
    charger_class_name: str = "",
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
    *,
    driver_variant: str = "",
    has_sonnenmodus_surface: bool = False,
) -> Dict[str, Any]:
    """Dokumentiert den einzigen erlaubten Laufzeitpfad nativer E3DC-Wallboxen."""

    _ = config
    st = status or {}
    class_name = str(charger_class_name or "").strip()
    variant = str(driver_variant or st.get("driver_variant", "") or "").strip()
    native_variants = {"e3dc_native", "e3dc_multi_connect", "e3dc_rscp"}
    native_classes = {"E3DCCharger", "E3DCMultiConnectCharger"}
    is_native = bool(
        class_name in native_classes
        or variant in native_variants
        or (bool(has_sonnenmodus_surface) and class_name not in {"OpenWBCharger", "OpenWBProCharger"})
    )
    runtime_path = "hardened_native" if is_native else "not_e3dc"
    return {
        "schema_version": "e3dc_native_production_v1",
        "enabled": bool(is_native),
        "runtime_path": runtime_path,
        "canonical_path": runtime_path,
        "driver_class_name": class_name,
        "driver_variant": variant,
        "transport": str(st.get("e3dc_transport", "") or ""),
        "device_family": str(st.get("e3dc_device_family", "unknown") or "unknown"),
        "device_family_source": str(st.get("e3dc_device_family_source", "unknown") or "unknown"),
        "control_backend": str(st.get("e3dc_control_backend", "status_only") or "status_only"),
        "direct_readback_complete": bool(st.get("e3dc_direct_readback_complete", False)),
        "direct_transition_write_allowed": False,
        "legacy_cpp_runtime_allowed": False,
        "legacy_cpp_reference_only": bool(is_native),
        "fallback_role": "reference_only" if is_native else "not_applicable",
        "current_update_policy": "stromdeckel_only",
        "current_updates_are_toggles": False,
        "start_toggle_policy": "edge_only_after_session_offer",
        "stop_toggle_policy": "hard_reason_after_verified_charge",
        "session_guard_required": bool(is_native),
        "charge_verification_required": bool(is_native),
        "phase_power_verification_required": bool(is_native),
        "phantom_power_zero_required": bool(is_native),
    }


def charge_end_latch_contract(
    status: Optional[Dict[str, Any]] = None,
    *,
    previous_latched: bool = False,
    previous_reason: str = "",
    had_confirmed_charge: bool = False,
    allow_new_latch: bool = True,
    user_release_exception: str = "",
    vehicle_changed: bool = False,
    disconnected_release: bool = False,
    mode_off: bool = False,
    start_verifying: bool = False,
    manager_stop_active: bool = False,
    grace_active: bool = False,
    target_soc_reached: bool = False,
    target_reached_reason: str = "",
    external_restart_confirmed: bool = False,
    now_ts: Any = 0,
) -> Dict[str, Any]:
    """Entscheidet, ob ein beendeter Ladevorgang verriegelt oder freigegeben wird.

    Dadurch bleibt „Fahrzeug zieht keinen Strom mehr“ von RSCP-Lücken,
    managerseitigen Stopps und bloßen Startangeboten getrennt. Eine neue
    Verriegelung erfordert einen zuvor bestätigten Ladevorgang. Explizite
    Nutzer-/Konfigurationsänderungen und ein echter Selbstneustart lösen eine
    bestehende Verriegelung, ohne den angebotenen Strom als Beleg zu werten.
    """

    st = status or {}
    now = _safe_float(now_ts, 0.0)
    charge_contract = st.get("charge_contract") if isinstance(st.get("charge_contract"), dict) else None
    if charge_contract is None:
        charge_contract = charge_observation_contract(st, now_ts=now)
    truth = str(charge_contract.get("truth") or "not_charging")
    status_valid = bool(charge_contract.get("status_valid", truth != "unknown"))
    connected = bool(charge_contract.get("connected", status_connected(st)))
    rscp_error = bool(charge_contract.get("rscp_error", st.get("rscp_error_active", False)))
    real_charging = bool(charge_contract.get("is_charging", False) or truth == "charging")
    prev_latched = bool(previous_latched)
    prev_reason = str(previous_reason or "")
    release_exception = str(user_release_exception or "").strip()
    target_reason = str(target_reached_reason or "").strip()
    terminal_reasons = frozenset({
        "vehicle_charge_ended",
        "start_rejected",
        "target_soc_reached",
        "target_kwh_reached",
        "battery_departure_target_reached",
    })
    terminal_latch = bool(prev_latched and prev_reason in terminal_reasons)
    protected_latch = bool(prev_latched and not terminal_latch)

    action = "hold" if prev_latched else "none"
    latched = prev_latched
    reason = prev_reason
    exception = ""
    start_blocked = prev_latched

    def clear(exc: str, why: str = "") -> Dict[str, Any]:
        return {
            "schema_version": "wallbox_charge_end_latch_v1",
            "action": "clear",
            "latched": False,
            "start_blocked": False,
            "reason": "",
            "previous_reason": prev_reason,
            "exception": str(exc or ""),
            "detail": str(why or exc or ""),
            "truth": truth,
            "connected": bool(connected),
            "real_charging": bool(real_charging),
            "had_confirmed_charge": bool(had_confirmed_charge),
            "allow_new_latch": bool(allow_new_latch),
            "start_verifying": bool(start_verifying),
            "manager_stop_active": bool(manager_stop_active),
            "grace_active": bool(grace_active),
            "rscp_error": bool(rscp_error),
            "target_soc_reached": bool(target_soc_reached),
            "target_reached_reason": target_reason,
            "status_valid": bool(status_valid),
            "external_restart_confirmed": bool(external_restart_confirmed),
            "ts": float(now),
        }

    if release_exception and (not prev_latched or terminal_latch):
        return clear(release_exception, "user_or_config_release")
    if not status_valid or rscp_error or truth == "unknown":
        action = "hold_unknown" if prev_latched else "observe_unknown"
        reason = prev_reason or "driver_status_unknown"
        latched = prev_latched
        start_blocked = prev_latched
    elif protected_latch:
        # Recovery-/Safety-Latches dürfen weder eine Ziel-/Profiländerung
        # noch Fahrzeugwechsel, Modus-Aus oder reale Leistung aufheben. Ihre
        # Freigabe gehört ausschließlich zum jeweiligen Schutzvertrag.
        action = "hold_protected"
        reason = prev_reason or "protected_charge_end_hold"
        latched = True
        start_blocked = True
    elif target_reason:
        action = (
            "hold"
            if prev_latched and prev_reason == target_reason
            else "latch"
        )
        latched = True
        start_blocked = True
        reason = target_reason
    elif real_charging:
        if (
            prev_latched
            and prev_reason == "vehicle_charge_ended"
            and external_restart_confirmed
        ):
            return clear("vehicle_self_restart", "verified_charge_seen_after_latch")
        if prev_latched:
            action = "hold"
            reason = prev_reason or "vehicle_charge_ended"
            latched = True
            start_blocked = True
        else:
            action = "observe"
            reason = "charging"
            latched = False
            start_blocked = False
    elif disconnected_release and terminal_latch and not rscp_error:
        return clear("unplugged", "vehicle_disconnected")
    elif mode_off and prev_latched:
        action = "hold"
        reason = prev_reason or "wallbox_mode_off"
        latched = True
        start_blocked = True
    elif manager_stop_active:
        action = "hold" if prev_latched else "ignore"
        reason = prev_reason or "manager_stop_active"
        latched = prev_latched
        start_blocked = prev_latched
    elif start_verifying or grace_active:
        action = "hold" if prev_latched else "ignore"
        reason = prev_reason or "start_verification_active"
        latched = prev_latched
        start_blocked = prev_latched
    elif (
        allow_new_latch
        and connected
        and bool(had_confirmed_charge)
        and truth == "not_charging"
    ):
        action = "latch"
        latched = True
        start_blocked = True
        reason = "target_soc_reached" if target_soc_reached else "vehicle_charge_ended"
    elif prev_latched:
        action = "hold"
        latched = True
        start_blocked = True
        reason = prev_reason or "vehicle_charge_ended"
    else:
        action = "none"
        latched = False
        start_blocked = False
        reason = ""

    return {
        "schema_version": "wallbox_charge_end_latch_v1",
        "action": action,
        "latched": bool(latched),
        "start_blocked": bool(start_blocked),
        "reason": str(reason or ""),
        "previous_reason": prev_reason,
        "exception": exception,
        "detail": str(reason or action or ""),
        "truth": truth,
        "connected": bool(connected),
        "real_charging": bool(real_charging),
        "had_confirmed_charge": bool(had_confirmed_charge),
        "allow_new_latch": bool(allow_new_latch),
        "start_verifying": bool(start_verifying),
        "manager_stop_active": bool(manager_stop_active),
        "grace_active": bool(grace_active),
        "rscp_error": bool(rscp_error),
        "target_soc_reached": bool(target_soc_reached),
        "target_reached_reason": target_reason,
        "status_valid": bool(status_valid),
        "external_restart_confirmed": bool(external_restart_confirmed),
        "ts": float(now),
    }


def vehicle_finished_candidate_step(
    previous: Optional[Dict[str, Any]],
    status: Optional[Dict[str, Any]],
    *,
    session_key: str,
    had_confirmed_charge: bool,
    offered_amp: Any,
    min_amp: Any = 6,
    manager_stop_active: bool = False,
    phase_transition_active: bool = False,
    start_verifying: bool = False,
    observe_only: bool = False,
    external_controller: bool = False,
    now_ts: Any = 0,
    min_frames: Any = 3,
    min_duration_s: Any = 45.0,
) -> Dict[str, Any]:
    """Entprellt ein mögliches Fahrzeug-Ladeende ohne Hardwareausgang.

    Der Kandidat ist absichtlich nur RAM-Zustand. Erst der aufrufende Manager
    darf ihn nach Prüfung des aktuellen Wattbudgets terminalisieren und
    anschließend crashfest persistieren. Manager-Stopps, Phasenwechsel,
    Fremd-Owner und unbekannte Treiberframes sind niemals Fahrzeug-Ladeenden.
    """

    st = status or {}
    now = _safe_float(now_ts, 0.0)
    required_frames = max(2, int(_safe_float(min_frames, 3)))
    required_s = max(10.0, _safe_float(min_duration_s, 45.0))
    minimum_amp = max(1.0, _safe_float(min_amp, 6.0))
    offered = max(0.0, _safe_float(offered_amp, 0.0))
    session = str(session_key or "").strip()
    charge_contract = (
        st.get("charge_contract")
        if isinstance(st.get("charge_contract"), dict)
        else charge_observation_contract(st, now_ts=now)
    )
    truth = str(charge_contract.get("truth") or "unknown")
    fresh = bool(charge_contract.get("status_valid", truth != "unknown"))
    connected = bool(charge_contract.get("connected", status_connected(st)))
    sample_ts = _safe_float(st.get("driver_status_last_sample_ts"), now)
    candidate = dict(previous) if isinstance(previous, dict) else {}

    blocker = ""
    if not fresh or truth == "unknown":
        blocker = "driver_status_unknown"
    elif not connected:
        blocker = "vehicle_not_connected"
    elif external_controller:
        blocker = "external_controller_owner"
    elif observe_only:
        blocker = "observe_only"
    elif manager_stop_active:
        blocker = "manager_stop_active"
    elif phase_transition_active:
        blocker = "phase_transition_active"
    elif start_verifying:
        blocker = "start_verification_active"
    elif not had_confirmed_charge:
        blocker = "confirmed_charge_missing"
    elif offered < minimum_amp:
        blocker = "minimum_offer_missing"
    elif not session:
        blocker = "session_key_missing"
    elif truth == "charging":
        blocker = "charging"
    elif truth != "not_charging":
        blocker = "charge_truth_unknown"

    if blocker:
        return {
            "contract": "wallbox_vehicle_finished_candidate_v1",
            "action": "hold_unknown" if blocker == "driver_status_unknown" else "reset",
            "confirmed": False,
            "blocker": blocker,
            "candidate": {},
            "frames": 0,
            "elapsed_s": 0.0,
            "session_key": session,
            "ts": now,
        }

    same_candidate = bool(
        candidate.get("session_key") == session
        and _safe_float(candidate.get("first_ts"), 0.0) > 0.0
    )
    if not same_candidate:
        candidate = {
            "session_key": session,
            "first_ts": now,
            "last_sample_ts": sample_ts,
            "frames": 1,
        }
    elif sample_ts > _safe_float(candidate.get("last_sample_ts"), 0.0):
        candidate["last_sample_ts"] = sample_ts
        candidate["frames"] = max(1, int(candidate.get("frames", 1) or 1)) + 1

    elapsed_s = max(0.0, now - _safe_float(candidate.get("first_ts"), now))
    frames = max(1, int(candidate.get("frames", 1) or 1))
    confirmed = bool(frames >= required_frames and elapsed_s >= required_s)
    return {
        "contract": "wallbox_vehicle_finished_candidate_v1",
        "action": "confirmed" if confirmed else "candidate",
        "confirmed": confirmed,
        "blocker": "",
        "candidate": candidate,
        "frames": frames,
        "elapsed_s": elapsed_s,
        "session_key": session,
        "ts": now,
    }


def openwb_phase_switch_capability(
    charger_class_name: str,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Liefert, ob Python 1p-/3p-Befehle an diese Wallbox senden darf."""

    _ = config
    st = status or {}
    if charger_class_name == "OpenWBProCharger":
        status_fresh = bool(
            st.get("driver_status_valid") is True
            and st.get("driver_status_stale") is not True
            and st.get("driver_status_degraded") is not True
            and st.get("driver_status_glitch") is not True
        )
        official_surface = bool(
            str(st.get("api_surface") or "").strip().lower()
            in ("openwb_pro_connect_php", "official_connect_php")
            and str(st.get("phase_switch_capability") or "")
            == "official_connect_php"
            and str(st.get("phase_switch_source") or "")
            == "openwb_pro_connect_php"
        )
        signaling = " ".join(
            str(st.get("evse_signaling") or "").strip().lower().split()
        )
        # Die offizielle openWB-Pro-Dokumentation nennt ``basic iec61851``;
        # aktuelle connect.php-Versionen liefern für denselben reinen
        # IEC-61851-PWM-Pfad auch den Kurzwert ``basic``. Nur diese beiden
        # exakten Werte öffnen die Phasenumschaltung. HLC-, ISO-15118- und
        # kombinierte Diagnosewerte bleiben weiterhin fail-closed.
        signaling_basic = signaling in ("basic", "basic iec61851")
        signaling_hlc = bool("hlc" in signaling or "iso15118" in signaling)
        can_switch = bool(
            status_fresh
            and official_surface
            and signaling_basic
            and not signaling_hlc
            and st.get("can_switch_phases") is True
        )
        if signaling_hlc:
            blocker = "hlc_iso15118_phase_switch_blocked"
        elif not signaling_basic:
            blocker = "evse_signaling_unknown"
        elif not status_fresh:
            blocker = "driver_status_not_fresh"
        elif not official_surface:
            blocker = "official_connect_php_not_confirmed"
        elif st.get("can_switch_phases") is not True:
            blocker = "driver_phase_capability_missing"
        else:
            blocker = ""
        return {
            "can_switch": can_switch,
            "capability": (
                "official_connect_php"
                if can_switch
                else "unknown_or_stale_connect_php"
            ),
            "source": (
                "openwb_pro_connect_php"
                if can_switch
                else "fail_closed"
            ),
            "api_surface": str(st.get("api_surface") or ""),
            "status_fresh": status_fresh,
            "evse_signaling": signaling,
            "blocker": blocker,
        }
    if charger_class_name != "OpenWBCharger":
        return {
            "can_switch": False,
            "capability": "not_openwb",
            "source": "driver",
            "api_surface": "",
        }
    return {
        "can_switch": False,
        "capability": st.get("phase_switch_capability", "secondary_current_only"),
        "source": "disabled_by_design",
        "api_surface": "openwb_secondary_set_current_heartbeat",
    }


def wallbox_phase_switch_capability(
    charger_class_name: str,
    status: Optional[Dict[str, Any]] = None,
    config: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Liefert die Phasen-Befehlsfläche, ohne eine Policy-Freigabe zu erteilen.

    Eine normale openWB bleibt auf sekundäre Stromvorgaben beschränkt. E3DC
    Multi Connect bleibt für direkte Phasenbefehle im reinen Beobachtungsmodus,
    bis ein kanonischer Vertrag für CP-Unterbrechung und Rücklesung die
    480-Sekunden-Sicherheitsfolge durchgängig belegt. Die davon unabhängige,
    herstellereigene efy-Automatik wird nur bei explizit gebundener Familie,
    frischem ALG-/Treiberstatus und dem vorhandenen WBchar6-Sonnenmodus
    getrennt ausgewiesen. Der optionale Auto-Phase-Mirror fehlt bei der efy
    teilweise und ist deshalb Diagnose, aber keine Startvoraussetzung.
    """

    st = status or {}
    if charger_class_name in ("OpenWBCharger", "OpenWBProCharger"):
        return openwb_phase_switch_capability(charger_class_name, st, config)
    driver_variant = str(st.get("driver_variant", "") or "")
    if charger_class_name == "E3DCMultiConnectCharger" or driver_variant in {"e3dc_multi_connect", "e3dc_rscp"}:
        sample_ts = _safe_float(st.get("driver_status_last_sample_ts"), 0.0)
        sample_ok_ts = _safe_float(st.get("driver_status_last_ok_ts"), 0.0)
        sample_age_s = _safe_float(st.get("driver_status_age_s"), -1.0)
        status_fresh = bool(
            st.get("driver_status_valid") is True
            and st.get("driver_status_stale") is False
            and st.get("driver_status_degraded") is False
            and st.get("driver_status_glitch") is False
            and st.get("driver_status_plausible") is True
            and st.get("driver_status_source") == "rscp_same_response"
            and st.get("wb_status_source") == "rscp_wb_extern_data_alg"
            and st.get("wb_status_valid") is True
            and st.get("alg_seen") is True
            and math.isfinite(sample_ts)
            and math.isfinite(sample_ok_ts)
            and math.isfinite(sample_age_s)
            and sample_ts > 0.0
            and sample_ok_ts == sample_ts
            and 0.0 <= sample_age_s <= 15.0
        )
        autonomous_efy = bool(
            str(st.get("e3dc_device_family", "") or "").strip().lower()
            == "efy"
            and str(st.get("e3dc_device_family_source", "") or "")
            in {"configured", "configured_type"}
            and str(st.get("e3dc_control_backend", "") or "")
            == "wbchar6_compat"
            and st.get("e3dc_efy_autonomous_wbchar6_verified") is True
            and str(
                st.get("e3dc_efy_autonomous_wbchar6_provenance") or ""
            )
            == EFY_WBCHAR6_PROVENANCE
            and status_fresh
        )
        return {
            "can_switch": False,
            "capability": "e3dc_multi_connect_cp_480_unverified",
            "source": "disabled_by_hardware_protection",
            "api_surface": "",
            "autonomous_can_switch": autonomous_efy,
            "autonomous_capability": (
                EFY_AUTONOMOUS_PRODUCT_CAPABILITY
                if autonomous_efy
                else "not_freshly_confirmed"
            ),
            "autonomous_source": (
                EFY_WBCHAR6_AUTONOMOUS_SOURCE
                if autonomous_efy
                else "fail_closed"
            ),
            "autonomous_provenance": (
                EFY_WBCHAR6_PROVENANCE
                if autonomous_efy
                else "unknown"
            ),
            "autonomous_handoff_method": (
                "set_amp_autonomous_solar"
                if autonomous_efy
                else ""
            ),
            "autonomous_protocol_mode": (
                "wbchar6_solar_mode"
                if autonomous_efy
                else ""
            ),
        }
    return {
        "can_switch": False,
        "capability": st.get("phase_switch_capability", "fixed_or_unknown"),
        "source": st.get("phase_switch_source", "driver"),
        "api_surface": st.get("api_surface", ""),
    }


def phase_observation_contract(
    status: Optional[Dict[str, Any]] = None,
    c_data: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    charger_id: int = 1,
    detected_phases: int = 1,
    vehicle_max_phases: int = 0,
    phase_cap_phases: int = 0,
    phase_switch_phases: int = 0,
    phase_target: int = 0,
    phase_capability: Optional[Dict[str, Any]] = None,
    vehicle_phase_capability: Optional[Dict[str, Any]] = None,
    charger_class_name: str = "",
    driver_variant: str = "",
) -> Dict[str, Any]:
    """Normalisiert Phasenbelege in einen typisierten Phasenvertrag."""

    st = status or {}
    cd = c_data or {}
    cfg = config or {}
    cap = phase_capability if isinstance(phase_capability, dict) else {}
    vehicle_cap = (
        vehicle_phase_capability
        if isinstance(vehicle_phase_capability, dict)
        else {}
    )
    detected = valid_phase_count(detected_phases, 1) or 1
    target = valid_phase_count(phase_target, valid_phase_count(st.get("phases_target"), 0))
    reported_target = valid_phase_count(st.get("phases_target"), 0)
    in_use = valid_phase_count(st.get("phases_in_use"), 0)
    actual = valid_phase_count(st.get("phases_actual"), 0)
    switch_phases = valid_phase_count(phase_switch_phases, 0)
    cap_phases = valid_phase_count(phase_cap_phases, 0)
    vehicle_phases = valid_phase_count(
        vehicle_max_phases or vehicle_cap.get("phase_count"),
        0,
    )
    vehicle_profile_phase_bound = bool(
        vehicle_cap.get("contract") == "vehicle_phase_capability_v1"
        and vehicle_cap.get("active") is True
        and valid_phase_count(vehicle_cap.get("phase_count"), 0) == vehicle_phases
        and (
            vehicle_cap.get("identity_confirmed") is True
            or str(vehicle_cap.get("identity_source") or "")
            in {
                "configured_selected_vehicle_profile",
                "configured_wallbox_obc_max_phases",
            }
        )
    )
    cable_phases = valid_phase_count(
        st.get("cable_phases", st.get("connected_phases", st.get("number_phases"))),
        0,
    )
    wallbox_phases = valid_phase_count(
        st.get("wallbox_phases", st.get("wallbox_max_phases")),
        0,
    ) or cable_phases or 3
    evse_supply_phases = max(1, min(3, int(wallbox_phases or cable_phases or 3)))

    measured_phases = 0
    measured_phase_power_w = 0.0
    if bool(st.get("phase_power_verified", False)):
        phase_values = []
        for key in ("phase_power_l1_w", "phase_power_l2_w", "phase_power_l3_w"):
            try:
                phase_values.append(abs(float(st.get(key, 0.0) or 0.0)))
            except (TypeError, ValueError):
                phase_values.append(0.0)
        measured_phase_power_w = sum(phase_values)
        measured_phases = sum(1 for value in phase_values if value > 250.0)
        measured_phases = valid_phase_count(measured_phases, 0)

    real_charging = status_real_charging(st)
    actual_phases = 0
    actual_source = "none"
    if measured_phases:
        actual_phases = measured_phases
        actual_source = "phase_power"
    elif real_charging and in_use:
        actual_phases = in_use
        actual_source = "phases_in_use"
    elif real_charging and actual:
        actual_phases = actual
        actual_source = "phases_actual"

    session_1p_only = bool(cd.get("_session_1p_only", False))
    can_switch = bool(cap.get("can_switch", st.get("can_switch_phases", False)))
    autonomous_can_switch = bool(cap.get("autonomous_can_switch", False))
    if charger_class_name == "OpenWBCharger":
        can_switch = False
    normalized_driver = str(driver_variant or st.get("driver_variant", "") or "")
    if charger_class_name == "E3DCMultiConnectCharger" or normalized_driver in {"e3dc_multi_connect", "e3dc_rscp"}:
        can_switch = False
        autonomous_can_switch = bool(
            autonomous_can_switch
            and str(st.get("e3dc_device_family", "") or "").strip().lower()
            == "efy"
            and str(st.get("e3dc_device_family_source", "") or "")
            in {"configured", "configured_type"}
            and str(st.get("e3dc_control_backend", "") or "")
            == "wbchar6_compat"
            and st.get("e3dc_efy_autonomous_wbchar6_verified") is True
            and str(
                st.get("e3dc_efy_autonomous_wbchar6_provenance") or ""
            )
            == EFY_WBCHAR6_PROVENANCE
        )
    if charger_class_name == "E3DCCharger" and normalized_driver == "e3dc_native":
        can_switch = False
        autonomous_can_switch = False
    evse_phase_switch_capable = bool(can_switch)

    phase_power_target_transition = bool(
        evse_phase_switch_capable
        and target == 3
        and reported_target == 3
        and in_use == 3
        and vehicle_profile_phase_bound
        and vehicle_phases >= 3
        and measured_phases in (1, 2)
        and measured_phase_power_w < 6.0 * 230.0 * 3.0
        and st.get("driver_status_valid") is True
        and st.get("driver_status_stale") is not True
        and st.get("driver_status_degraded") is not True
        and st.get("driver_status_glitch") is not True
        and st.get("driver_status_plausible") is not False
    )
    if phase_power_target_transition:
        # Beim Anlauf/Abwurf kann der phasenweise Leistungszähler einen Zyklus
        # hinter dem frischen EVSE-Readback liegen. Ein bekannter 3p-Wagen mit
        # übereinstimmendem target+in_use bleibt unterhalb der 3p-Mindestlast
        # deshalb 3p; andernfalls würde derselbe Übergang als 1p budgetiert und
        # einen gefährlich hohen Stromsollwert erzeugen.
        actual_phases = 3
        actual_source = "phases_in_use_target_transition"

    idle_confirmed_target = bool(
        evse_phase_switch_capable
        and target in (1, 3)
        and reported_target == target
        and st.get("driver_status_valid") is True
        and st.get("driver_status_stale") is not True
        and st.get("driver_status_degraded") is not True
        and st.get("driver_status_glitch") is not True
        and st.get("driver_status_plausible") is not False
        and bool(
            st.get("plug_state")
            or st.get("car") == 2
            or st.get("plug")
            or st.get("connected")
        )
        and not real_charging
        and status_real_power(st) <= 100.0
        and st.get("cp_interrupt_isactive") in (False, 0)
    )

    native_fixed_three_phase = bool(
        (charger_class_name in ("E3DCCharger", "E3DCMultiConnectCharger") or normalized_driver in ("e3dc_native", "e3dc_multi_connect", "e3dc_rscp"))
        and not can_switch
        and not autonomous_can_switch
        and not session_1p_only
        and cable_phases >= 3
        and wallbox_phases >= 3
    )

    phase_evidence_valid = False
    reason_code = "none"
    vehicle_phase_source = "none"

    if actual_phases:
        effective = actual_phases
        basis = actual_source
        phase_evidence_valid = True
        reason_code = f"measured_live_charging_{actual_phases}p"
        vehicle_phase_source = "measured_power"
    elif session_1p_only or (
        vehicle_profile_phase_bound and vehicle_phases == 1
    ):
        effective = 1
        basis = "vehicle_1p"
        phase_evidence_valid = True
        vehicle_phase_source = str(
            vehicle_cap.get("phase_source") or "configured_selected_vehicle_profile"
        )
        if evse_supply_phases >= 3 and not evse_phase_switch_capable:
            reason_code = "vehicle_profile_1p_on_fixed_3p_evse"
        else:
            reason_code = "vehicle_profile_1p"
    elif idle_confirmed_target:
        # Bei einer phasenumschaltbaren EVSE ist das Fahrzeugprofil die
        # maximale AC-Fähigkeit, nicht die gegenwärtige Verdrahtung. Während
        # des physisch stromlosen Wiederanlaufs gibt es naturgemäß noch keine
        # Istphasen. Ein frischer, ruhiger und zum Managerziel passender
        # Geräte-Readback ist dann der maßgebliche Topologiebeleg. Andernfalls
        # entstünde ein Zirkelschluss: 3p-Mindestbudget trotz bestätigtem 1p,
        # aber keine Istphasen vor dem ersten Ladestrom.
        effective = target
        basis = "idle_confirmed_target"
        phase_evidence_valid = True
        vehicle_phase_source = "evse_idle_target_readback"
        reason_code = f"evse_idle_confirmed_target_{target}p"
    elif vehicle_profile_phase_bound and vehicle_phases >= 3:
        effective = min(evse_supply_phases, vehicle_phases)
        basis = "vehicle_profile"
        phase_evidence_valid = True
        vehicle_phase_source = str(
            vehicle_cap.get("phase_source") or "configured_selected_vehicle_profile"
        )
        reason_code = f"vehicle_profile_{effective}p"
    elif switch_phases:
        effective = switch_phases
        basis = "switch"
        phase_evidence_valid = True
        vehicle_phase_source = "evse_switch_target"
        reason_code = f"evse_switch_target_{switch_phases}p"
    elif target:
        effective = target
        basis = "target"
        phase_evidence_valid = True
        vehicle_phase_source = "evse_target"
        reason_code = f"evse_target_{target}p"
    elif cap_phases:
        effective = cap_phases
        basis = "phase_cap"
        phase_evidence_valid = True
        vehicle_phase_source = "evse_cap"
        reason_code = f"evse_cap_{cap_phases}p"
    elif native_fixed_three_phase:
        effective = 3
        basis = "fixed_wallbox_3p"
        phase_evidence_valid = False
        vehicle_phase_source = "evse_topology_fallback"
        reason_code = "fixed_wallbox_3p_evse_topology"
    elif wallbox_phases and charger_class_name != "OpenWBCharger":
        effective = wallbox_phases
        basis = "wallbox"
        phase_evidence_valid = False
        vehicle_phase_source = "evse_topology_fallback"
        reason_code = "wallbox_topology_fallback"
    else:
        effective = detected
        basis = "detected"
        phase_evidence_valid = False
        vehicle_phase_source = "evse_topology_fallback"
        reason_code = "detected_topology_fallback"

    effective = max(1, min(3, int(effective or 1)))
    return {
        "actual_phases": int(actual_phases),
        "actual_source": actual_source,
        "effective_phases": int(effective),
        "effective_source": basis,
        "effective_load_phases": int(effective),
        "evse_supply_phases": int(evse_supply_phases),
        "evse_phase_switch_capable": bool(evse_phase_switch_capable),
        "vehicle_ac_max_phases": int(vehicle_phases),
        "vehicle_phase_source": str(vehicle_phase_source),
        "phase_evidence_valid": bool(phase_evidence_valid),
        "reason_code": str(reason_code),
        "detected_phases": int(detected),
        "target_phases": int(target),
        "reported_target_phases": int(reported_target),
        "idle_confirmed_target": bool(idle_confirmed_target),
        "switch_phases": int(switch_phases),
        "cap_phases": int(cap_phases),
        "cable_phases": int(cable_phases),
        "vehicle_max_phases": int(vehicle_phases),
        "vehicle_profile_phase_bound": bool(vehicle_profile_phase_bound),
        "vehicle_profile_phase_source": str(
            vehicle_cap.get("phase_source") or "none"
        ),
        "vehicle_identity_source": str(
            vehicle_cap.get("identity_source") or ""
        ),
        "wallbox_phases": int(wallbox_phases),
        "measured_phases": int(measured_phases),
        "measured_phase_power_w": float(measured_phase_power_w),
        "phase_power_target_transition": bool(
            phase_power_target_transition
        ),
        "phase_power_verified": bool(st.get("phase_power_verified", False)),
        "can_switch_phases": bool(can_switch),
        "autonomous_phase_switch_capable": bool(autonomous_can_switch),
        "autonomous_phase_switch_capability": str(
            cap.get("autonomous_capability", "") or ""
        ),
        "autonomous_phase_switch_source": str(
            cap.get("autonomous_source", "") or ""
        ),
        "autonomous_phase_switch_provenance": str(
            cap.get("autonomous_provenance", "") or ""
        ),
        "autonomous_phase_handoff_method": str(
            cap.get("autonomous_handoff_method", "") or ""
        ),
        "autonomous_phase_protocol_mode": str(
            cap.get("autonomous_protocol_mode", "") or ""
        ),
        "phase_switch_capability": str(cap.get("capability", st.get("phase_switch_capability", "")) or ""),
        "phase_switch_source": str(cap.get("source", st.get("phase_switch_source", "")) or ""),
        "api_surface": str(cap.get("api_surface", st.get("api_surface", "")) or ""),
        "charger_class": str(charger_class_name or ""),
        "driver_variant": normalized_driver,
    }


def idle_start_phase_budget_count(phase_contract: Optional[Dict[str, Any]]) -> int:
    """Trennt eine mögliche 1p-Startzuteilung von der physischen Topologie.

    Nur eine bestätigt umschaltbare EVSE darf ohne Fahrzeug-/Istphasenbeleg
    zunächst für einen späteren 1p-Start budgetiert werden. Fehlende
    Umschaltfähigkeit und ein neutraler KEEP_PHASES-Auftrag sind kein
    einphasiger Lastbeleg. Der separate Ausgangsvertrag prüft weiterhin die
    tatsächliche Phasenlage vor jedem Stromangebot.
    """

    contract = phase_contract if isinstance(phase_contract, dict) else {}
    phases = valid_phase_count(contract.get("effective_phases"), 3) or 3
    if (
        contract.get("evse_phase_switch_capable") is True
        and not valid_phase_count(contract.get("actual_phases"), 0)
        and contract.get("vehicle_profile_phase_bound") is not True
    ):
        return 1
    return phases


def vehicle_max_ac_phases_from_profiles(
    config: Optional[Dict[str, Any]],
    charger_id: int,
    profiles: Optional[Iterable[Dict[str, Any]]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> int:
    """Liefert die Planungsphasen eines bestätigten oder konfigurierten Fahrzeugs."""
    cap = vehicle_phase_capability_from_profiles(
        profiles,
        status=status,
        config=config,
        charger_id=charger_id,
    )
    if cap.get("active") and valid_phase_count(cap.get("phase_count"), 0):
        return int(cap["phase_count"])
    return 0





def vehicle_max_ac_power_kw_from_profiles(
    config: Optional[Dict[str, Any]],
    charger_id: int,
    profiles: Optional[Iterable[Dict[str, Any]]] = None,
    status: Optional[Dict[str, Any]] = None,
) -> float:
    """Liefere die AC-Leistungsannahme für die Fahrzeugplanung.

    Eine in der Ladeplanung angenommene Leistung ist keine Strom-Hardwaregrenze.
    Profilwerte werden nur für ein bestätigt sitzungsgebundenes Fahrzeug
    ausgewertet. Explizite Ladepunktwerte bleiben als Planungsinformation
    kompatibel, werden aber nicht durch den Hardwareausgang in Ampere
    umgerechnet.
    """

    cfg = config or {}
    st = status or {}
    try:
        cid = int(charger_id or 1)
    except (TypeError, ValueError):
        cid = 1

    for key in (f"wb{cid}_obc_max_power_kw", f"wb{cid}_max_ac_power_kw"):
        try:
            explicit_kw = float(str(cfg.get(key, "")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if explicit_kw > 0.0:
            return explicit_kw

    identity = confirmed_session_vehicle_identity(st)
    profile = _vehicle_profile_for_identity(
        profiles,
        identity.get("key") if identity.get("confirmed", False) else "",
    )
    if not profile:
        return 0.0

    for key in ("max_ac_power_kw", "ac_power_kw", "charge_power_kw", "charge_power", "power"):
        try:
            power_kw = float(str(profile.get(key, "")).replace(",", "."))
        except (TypeError, ValueError):
            continue
        if power_kw > 0.0:
            return power_kw
    return 0.0


def wallbox_executable_budget(
    status: Optional[Dict[str, Any]] = None,
    c_data: Optional[Dict[str, Any]] = None,
    *,
    config: Optional[Dict[str, Any]] = None,
    charger_id: int = 1,
    allowed_w: float = 0.0,
    detected_phases: int = 1,
    min_amp: int = 6,
    vehicle_max_phases: int = 0,
    phase_cap_phases: int = 0,
    phase_switch_phases: int = 0,
    phase_target: int = 0,
    openwb_phase_capable: bool = False,
    can_switch_to_1p: bool = False,
    require_one_phase: bool = False,
    grid_unlocked: bool = False,
    autonomous_pv_only: bool = False,
    phase_capability: Optional[Dict[str, Any]] = None,
    vehicle_phase_capability: Optional[Dict[str, Any]] = None,
    charger_class_name: str = "",
    driver_variant: str = "",
) -> Dict[str, Any]:
    """Liefert, ob das aktuelle Budget einen Ladestart oder das Weiterladen physikalisch erlaubt."""

    st = status or {}
    cd = c_data or {}
    budget_w = max(0.0, _safe_float(allowed_w, 0.0))
    min_amp_int = max(1, int(round(_safe_float(min_amp, 6))))
    detected = valid_phase_count(detected_phases, 1) or 1
    target = valid_phase_count(phase_target, valid_phase_count(st.get("phases_target"), 0))
    switch_phases = valid_phase_count(phase_switch_phases, 0)
    cap_phases = valid_phase_count(phase_cap_phases, 0)
    vehicle_phases = valid_phase_count(vehicle_max_phases, 0)
    real_charging = status_real_charging(st)
    phase_contract = phase_observation_contract(
        st,
        cd,
        config=config,
        charger_id=charger_id,
        detected_phases=detected,
        vehicle_max_phases=vehicle_phases,
        phase_cap_phases=cap_phases,
        phase_switch_phases=switch_phases,
        phase_target=target,
        phase_capability=phase_capability or {
            "can_switch": bool(openwb_phase_capable),
            "capability": st.get("phase_switch_capability", ""),
            "source": st.get("phase_switch_source", ""),
            "api_surface": st.get("api_surface", ""),
        },
        vehicle_phase_capability=vehicle_phase_capability,
        charger_class_name=str(charger_class_name or cd.get("charger_class_name", "") or cd.get("_charger_class_name", "") or ""),
        driver_variant=str(driver_variant or st.get("driver_variant", "") or ""),
    )
    phases = max(1, min(3, int(phase_contract.get("effective_phases", detected) or detected)))
    basis = str(phase_contract.get("effective_source", "detected") or "detected")
    one_phase_min_w = float(min_amp_int * 230)
    nominal_min_power_w = float(min_amp_int * 230 * phases)
    autonomous_1p_ready = bool(
        phase_contract.get("autonomous_phase_switch_capable") is True
        and autonomous_pv_only
        and not grid_unlocked
        and str(phase_contract.get("autonomous_phase_handoff_method") or "")
        == "set_amp_autonomous_solar"
        and str(phase_contract.get("autonomous_phase_protocol_mode") or "")
        == "wbchar6_solar_mode"
        and budget_w >= one_phase_min_w
        and (
            require_one_phase
            or budget_w < nominal_min_power_w
        )
    )
    if autonomous_1p_ready:
        # Die efy erhält keinen Phasenbefehl. E3DC-Control übergibt ausschließlich
        # den bereits kanonischen WBchar6-Sonnenmodus mit Stromdeckel; die im
        # explizit konfigurierte efy besitzt die herstellereigene 1p-/3p-
        # Produktfähigkeit. Ihre Aktivierung über WBchar6 bleibt dagegen ein
        # feldverifizierter Legacy-Vertrag; der frische ALG-Status bindet die
        # konkrete Steck-/Ladesitzung.
        # Für diesen eng gebundenen Übergabepfad darf die Watt-/Ampere-Projektion
        # deshalb das 1p-Minimum verwenden.
        phases = 1
        basis = "e3dc_efy_autonomous_solar_handoff"
    energy_projection_phases = int(phases)
    electrical_reservation_phases = int(
        3 if autonomous_1p_ready else energy_projection_phases
    )
    min_power_w = float(min_amp_int * 230 * phases)
    budget_ready = bool(grid_unlocked or budget_w >= min_power_w)
    switch_to_1p_ready = bool(
        openwb_phase_capable
        and not real_charging
        and phases >= 3
        and target == 3
        and can_switch_to_1p
        and budget_w >= one_phase_min_w
    )
    one_phase_ready = bool(
        int(phase_contract.get("actual_phases", 0) or 0) == 1
        or (
            phase_contract.get("vehicle_profile_phase_bound") is True
            and int(phase_contract.get("vehicle_max_phases", 0) or 0) == 1
        )
        or int(phase_contract.get("target_phases", 0) or 0) == 1
        or int(phase_contract.get("switch_phases", 0) or 0) == 1
        or (
            int(phase_contract.get("cap_phases", 0) or 0) == 1
            and bool(phase_contract.get("can_switch_phases", False))
        )
        or switch_to_1p_ready
        or autonomous_1p_ready
    )
    can_start_or_hold = bool(
        real_charging
        or budget_ready
        or switch_to_1p_ready
        or autonomous_1p_ready
    )

    if grid_unlocked and budget_w < min_power_w:
        reason = "Netz-/Preisfenster gibt Laden frei; Mindestleistung %.0f W (%dp) ist erlaubt." % (
            min_power_w,
            phases,
        )
    elif autonomous_1p_ready:
        reason = (
            "Budget %.0f W reicht für die efy-Übergabe an den autonomen "
            "Solarmodus ab %.0f W (1p); E3DC-Control sendet keinen Phasenbefehl."
        ) % (budget_w, one_phase_min_w)
    elif budget_ready:
        reason = "Budget %.0f W deckt Mindestleistung %.0f W (%dp)." % (budget_w, min_power_w, phases)
    elif switch_to_1p_ready:
        reason = "Budget %.0f W reicht für den einphasigen Start, aber nicht für die dreiphasige Mindestleistung %.0f W." % (
            budget_w,
            min_power_w,
        )
    else:
        reason = "Budget %.0f W < Mindestleistung %.0f W (%dp)." % (budget_w, min_power_w, phases)

    if require_one_phase and not one_phase_ready:
        budget_ready = False
        switch_to_1p_ready = False
        can_start_or_hold = False
        reason = (
            "1p-Betrieb erforderlich; aktuelle/konfigurierbare Phasenlage ist nicht sicher einphasig."
        )

    return {
        "allowed_w": int(round(budget_w)),
        "phases": int(phases),
        # ``phases`` bleibt der kompatible Alias für die energetische
        # Wattprojektion. Eine autonome efy darf energetisch mit 1p starten,
        # muss bis zur rückgelesenen Eigenwahl aber elektrisch 3p reservieren.
        "energy_projection_phases": energy_projection_phases,
        "electrical_reservation_phases": electrical_reservation_phases,
        "phase_basis": basis,
        "phase_contract": phase_contract,
        "min_amp": int(min_amp_int),
        "min_power_w": int(round(min_power_w)),
        "one_phase_min_power_w": int(round(one_phase_min_w)),
        "budget_ready": bool(budget_ready),
        "switch_to_1p_ready": bool(switch_to_1p_ready),
        "autonomous_1p_ready": bool(autonomous_1p_ready),
        "autonomous_phase_handoff": bool(autonomous_1p_ready),
        "autonomous_phase_handoff_method": (
            "set_amp_autonomous_solar" if autonomous_1p_ready else ""
        ),
        "autonomous_phase_protocol_mode": (
            "wbchar6_solar_mode" if autonomous_1p_ready else ""
        ),
        # Der Ampere-Deckel stammt aus der zentralen Wattentscheidung. Weil
        # die efy ihre Phase in diesem Sonderpfad selbst wählt, ist daraus
        # jedoch kein strikter Ladepunkt-Wattdeckel ableitbar: derselbe Strom
        # kann ein- oder dreiphasig fließen. Der Handoff ist deshalb bewusst
        # als PV-Senke und nicht als harte Wattzusage typisiert.
        "watt_budget_semantics": (
            "autonomous_pv_sink" if autonomous_1p_ready else "strict_phase_projection"
        ),
        "strict_watt_cap": not autonomous_1p_ready,
        "worst_case_phase_multiplier": 3 if autonomous_1p_ready else 1,
        "grid_import_protection": (
            "manager_pcc_energy_guard_plus_wbchar6_envelope"
            if autonomous_1p_ready
            else "manager_budget"
        ),
        "one_phase_required": bool(require_one_phase),
        "one_phase_ready": bool(one_phase_ready),
        "can_start_or_hold": bool(can_start_or_hold),
        "real_charging": bool(real_charging),
        "grid_unlocked": bool(grid_unlocked),
        "reason": reason,
    }


def budget_to_target_current(
    *,
    allowed_w: float,
    detected_phases: int,
    min_amp: int = 6,
    max_amp: int = 16,
    current_step_amp: Any = 1.0,
    house_fuse_cap_amp: Optional[int] = None,
    apply_house_fuse: bool = False,
    base_6a_active: bool = False,
    watts_per_amp: float = 0.0,
) -> Dict[str, Any]:
    """Übersetzt ein absolutes Wallbox-Budget in einen Zielstrom.

    ``allowed_w`` ist die gesamte Leistung, welche die Wallbox beziehen darf,
    kein Aufschlag auf ihre aktuelle Last. Der Aufrufer berücksichtigt die
    aktuell gemessene Wallbox-Leistung beim Aufbau dieses Budgets. Für die
    harte Übersetzung in ein Stromangebot zählt stets die nominal mögliche
    Leistung von 230 V je aktiver Phase. Eine aktuell geringere Abnahme des
    Fahrzeugs bleibt Diagnose und darf das Stromangebot nicht über das
    Leistungsbudget hinaus öffnen.
    """

    budget_w = max(0.0, _safe_float(allowed_w, 0.0))
    phases = valid_phase_count(detected_phases, 1) or 1
    min_amp_int = max(1, int(round(_safe_float(min_amp, 6))))
    max_amp_int = max(0, int(round(_safe_float(max_amp, 16))))
    step_amp = _current_step(current_step_amp, 1.0)
    nominal_w_per_amp = float(230 * phases)
    measured_w_per_amp = _safe_float(watts_per_amp, 0.0)
    measured_w_per_amp = (
        measured_w_per_amp
        if math.isfinite(measured_w_per_amp) and measured_w_per_amp > 0.0
        else 0.0
    )
    w_per_amp = nominal_w_per_amp
    min_power_w = float(min_amp_int * w_per_amp)

    if budget_w >= min_power_w:
        raw_amp = min(float(max_amp_int), budget_w / w_per_amp)
        stepped_amp = int(raw_amp / step_amp + 1e-9) * step_amp
        target_amp = max(
            float(min_amp_int),
            min(float(max_amp_int), stepped_amp),
        )
        target_amp = _amp_value(target_amp, step_amp)
        if measured_w_per_amp > 0.0:
            reason = (
                "Budget %.0f W -> %s A bei %d Phase(n), nominal %.0f W/A; "
                "gemessene Abnahme %.0f W/A bleibt Diagnose."
            ) % (
                budget_w,
                target_amp,
                phases,
                nominal_w_per_amp,
                measured_w_per_amp,
            )
        else:
            reason = "Budget %.0f W -> %s A bei %d Phase(n)." % (budget_w, target_amp, phases)
    else:
        target_amp = 0
        if base_6a_active:
            reason = (
                "6A-Boden angefordert, aber Budget %.0f W < Mindestleistung "
                "%.0f W (%dp); keine Leistungsautorität."
            ) % (budget_w, min_power_w, phases)
        else:
            reason = "Budget %.0f W < Mindestleistung %.0f W (%dp)." % (
                budget_w,
                min_power_w,
                phases,
            )

    house_fuse_limited = False
    if apply_house_fuse and house_fuse_cap_amp is not None and target_amp > 0:
        cap_amp = max(0, int(round(_safe_float(house_fuse_cap_amp, max_amp_int))))
        if cap_amp < target_amp:
            target_amp = _amp_value(float(cap_amp), step_amp)
            house_fuse_limited = True
            reason = "Hausabsicherung begrenzt Zielstrom auf %s A." % target_amp

    return {
        "target_amp": _amp_value(target_amp, step_amp),
        "allowed_w": int(round(budget_w)),
        "phases": int(phases),
        "min_amp": int(min_amp_int),
        "max_amp": int(max_amp_int),
        "current_step_amp": _amp_value(step_amp, step_amp),
        "min_power_w": int(round(min_power_w)),
        "physically_chargeable": bool(target_amp >= min_amp_int),
        "house_fuse_limited": bool(house_fuse_limited),
        "limiting_reason": reason,
        "watts_per_amp": round(float(w_per_amp), 1),
        "watts_per_amp_measured": bool(measured_w_per_amp > 0.0),
        "measured_watts_per_amp": round(float(measured_w_per_amp), 1),
        "budget_basis": "nominal_offer",
        "target_power_w": int(round(float(target_amp) * nominal_w_per_amp)),
    }


def physical_current_phase_count(
    physical_budget: Optional[Dict[str, Any]],
    *,
    allocation_target_phases: Any = 0,
    detected_phases: Any = 1,
) -> int:
    """Bindet die Stromübersetzung an die bereits ausführbare Wattzuteilung.

    Bei einem stehenden, umschaltbaren Ladepunkt kann die zentrale Zuteilung
    einen einphasigen Start freigeben, obwohl der alte Geräte-Sollwert noch 3p
    meldet. Genau in diesem geprüften Fall darf eine spätere physikalische
    Projektion die 1p-Zuteilung nicht erneut auf 3p und damit auf 0 A
    verteuern. In allen anderen Fällen bleibt die höhere Phasenzahl
    konservativ maßgeblich.
    """

    budget = physical_budget if isinstance(physical_budget, dict) else {}
    policy_phases = valid_phase_count(
        budget.get("phases"),
        valid_phase_count(detected_phases, 1),
    ) or 1
    allocation_phases = valid_phase_count(allocation_target_phases, 0)
    if budget.get("autonomous_1p_ready") is True:
        # Die Zuteilung kann aus dem konservativen gemeinsamen RSCP-Vertrag
        # noch 3p tragen. Der versiegelte efy-Übergabevertrag ist enger: Er
        # gilt nur bei expliziter efy-Konfiguration und frischem ALG-Status und
        # lässt ausschließlich den herstellereigenen Sonnenmodus entscheiden.
        return 1
    if (
        budget.get("switch_to_1p_ready") is True
        and allocation_phases == 1
    ):
        return 1
    return max(1, min(3, max(policy_phases, allocation_phases)))


def wallbox_floor_phase_contract(
    physical_budget: Optional[Dict[str, Any]],
    *,
    allocated_w: Any = 0.0,
    real_surplus_w: Any = 0.0,
    min_amp: Any = 6,
    fallback_phases: Any = 3,
) -> Dict[str, Any]:
    """Bindet Floor-Mindestleistung und Cooldown an die ausführbare Physik.

    Der Vertrag öffnet keine Akkuhilfe. Er darf ausschließlich einen alten
    3p-Floor-Cooldown aufheben, wenn der bereits versiegelte autonome efy-
    Sonnenmodus mindestens sein 1p-PV-Budget real zur Verfügung hat.
    """

    budget = physical_budget if isinstance(physical_budget, dict) else {}
    phases = valid_phase_count(
        budget.get("phases"),
        valid_phase_count(fallback_phases, 3),
    ) or 3
    minimum_amp = max(1, _safe_int(min_amp, 6))
    minimum_w = float(minimum_amp * 230 * phases)
    threshold_w = max(750.0, minimum_w - 250.0)
    allocated = max(0.0, _safe_float(allocated_w, 0.0))
    surplus = max(0.0, _safe_float(real_surplus_w, 0.0))
    autonomous_ready = bool(budget.get("autonomous_1p_ready") is True)
    release_cooldown = bool(
        autonomous_ready
        and allocated >= threshold_w
        and surplus >= threshold_w
    )
    return {
        "contract": "wallbox_floor_phase_contract_v1",
        "phases": int(phases),
        "min_amp": int(minimum_amp),
        "min_power_w": int(round(minimum_w)),
        "threshold_w": int(round(threshold_w)),
        "allocated_w": int(round(allocated)),
        "real_surplus_w": int(round(surplus)),
        "autonomous_1p_ready": autonomous_ready,
        "release_battery_cooldown": release_cooldown,
    }


def autonomous_solar_output_contract(
    physical_budget: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Bindet den expliziten efy-Solarauftrag an den Physical-Budget-Vertrag.

    Die Policy hat Wattbudget, Mindeststrom und die zulässige autonome
    Übergabe bereits entschieden. Dieser Vertrag wählt nur die dazugehörige
    Treibermethode. Er fällt bei einem unvollständigen oder veränderten
    Physical-Budget-Vertrag auf den normalen Sonnenmodus zurück und behauptet
    ausdrücklich keinen strikten Ladepunkt-Wattdeckel.
    """

    budget = physical_budget if isinstance(physical_budget, dict) else {}
    phase_contract = (
        budget.get("phase_contract")
        if isinstance(budget.get("phase_contract"), dict)
        else {}
    )
    active = bool(
        budget.get("autonomous_phase_handoff") is True
        and budget.get("autonomous_1p_ready") is True
        and budget.get("strict_watt_cap") is False
        and str(budget.get("watt_budget_semantics") or "")
        == "autonomous_pv_sink"
        and str(budget.get("autonomous_phase_handoff_method") or "")
        == "set_amp_autonomous_solar"
        and str(budget.get("autonomous_phase_protocol_mode") or "")
        == "wbchar6_solar_mode"
        and phase_contract.get("autonomous_phase_switch_capable") is True
        and str(phase_contract.get("autonomous_phase_switch_source") or "")
        == EFY_WBCHAR6_AUTONOMOUS_SOURCE
        and str(
            phase_contract.get("autonomous_phase_switch_provenance") or ""
        )
        == EFY_WBCHAR6_PROVENANCE
        and str(phase_contract.get("autonomous_phase_handoff_method") or "")
        == "set_amp_autonomous_solar"
        and str(phase_contract.get("autonomous_phase_protocol_mode") or "")
        == "wbchar6_solar_mode"
    )
    return {
        "contract": "wallbox_autonomous_solar_output_v1",
        "active": active,
        "method": (
            "set_amp_autonomous_solar" if active else "set_amp_sonnenmodus"
        ),
        "protocol_mode": "wbchar6_solar_mode" if active else "",
        "watt_budget_semantics": (
            "autonomous_pv_sink" if active else "strict_phase_projection"
        ),
        "strict_watt_cap": not active,
        "worst_case_phase_multiplier": 3 if active else 1,
        "grid_import_protection": (
            "manager_pcc_energy_guard_plus_wbchar6_envelope"
            if active
            else "manager_budget"
        ),
        "reason": (
            "explicit_efy_autonomous_solar_handoff"
            if active
            else "autonomous_handoff_not_bound"
        ),
    }


def physical_start_diagnostic_projection(
    physical_budget: Optional[Dict[str, Any]],
    *,
    allocation_target_phases: Any = 0,
    connected: bool = False,
    charger_class_name: str = "",
    phase_capable: bool = False,
) -> Dict[str, Any]:
    """Projiziert die ausführbare Startphysik getrennt vom Hardwareziel.

    Ein stehender openWB-Pro-Ladepunkt darf zentral einen einphasigen Start
    erhalten, obwohl ``connect.php`` noch das letzte 3p-Geräteziel meldet. Der
    konservative Hardware-/Transitionsvertrag bleibt unverändert; nur die
    Diagnose benennt die bereits gebundene Startprojektion und ihre echte
    Mindestleistung.
    """

    budget = physical_budget if isinstance(physical_budget, dict) else {}
    control_phases = valid_phase_count(budget.get("phases"), 1) or 1
    diagnostic_phases = control_phases
    source = "physical_control_contract"
    allocation_phases = valid_phase_count(allocation_target_phases, 0)
    phase_contract = (
        budget.get("phase_contract")
        if isinstance(budget.get("phase_contract"), dict)
        else {}
    )
    can_switch = bool(
        phase_capable
        or phase_contract.get("can_switch_phases", False)
    )
    real_charging = bool(budget.get("real_charging", False))
    if (
        connected
        and not real_charging
        and str(charger_class_name or phase_contract.get("charger_class") or "")
        == "OpenWBProCharger"
        and can_switch
        and allocation_phases == 1
    ):
        diagnostic_phases = 1
        source = "cycle_allocation_1p_start"

    allowed_w = max(0.0, _safe_float(budget.get("allowed_w"), 0.0))
    min_amp = max(1, int(round(_safe_float(budget.get("min_amp"), 6))))
    min_power_w = float(min_amp * 230 * diagnostic_phases)
    grid_unlocked = bool(budget.get("grid_unlocked", False))
    budget_ready = bool(grid_unlocked or allowed_w >= min_power_w)
    if grid_unlocked and allowed_w < min_power_w:
        reason = (
            "Netz-/Preisfenster gibt den %dp-Start frei; Mindestleistung %.0f W ist erlaubt."
            % (diagnostic_phases, min_power_w)
        )
    elif budget_ready:
        reason = "Budget %.0f W deckt Start-Mindestleistung %.0f W (%dp)." % (
            allowed_w,
            min_power_w,
            diagnostic_phases,
        )
    else:
        reason = "Budget %.0f W < Start-Mindestleistung %.0f W (%dp)." % (
            allowed_w,
            min_power_w,
            diagnostic_phases,
        )

    return {
        "schema_version": "wallbox_physical_start_diagnostic_v1",
        "source": source,
        "control_phases": int(control_phases),
        "allocation_target_phases": int(allocation_phases),
        "phases": int(diagnostic_phases),
        "min_amp": int(min_amp),
        "min_power_w": int(round(min_power_w)),
        "allowed_w": int(round(allowed_w)),
        "budget_ready": bool(budget_ready),
        "real_charging": bool(real_charging),
        "reason": reason,
    }


def stable_wallbox_amp_contract(
    *,
    proposed_amp: Any,
    current_amp: Any,
    real_power_w: Any = 0.0,
    real_charging: bool = False,
    status_fresh: bool = True,
    now_ts: Any = 0.0,
    charger_class_name: str = "",
    grid_power_w: Any = 0.0,
    fast_grid_threshold_w: Any = 150.0,
    budget_timeout: bool = False,
    storage_floor_mode_active: bool = False,
    grid_allowed: bool = False,
    price_active: bool = False,
    price_boost_active: bool = False,
    predump_active: bool = False,
    physical_amp_down_active: bool = False,
    stable_budget_jump_done: bool = False,
    last_storage_guided_amp_up_ts: Any = 0.0,
    last_storage_guided_amp_down_ts: Any = 0.0,
    fast_block_until: Any = 0.0,
    stable_budget_jump_ts: Any = 0.0,
    last_openwb_grid_window_amp_up_ts: Any = 0.0,
    stable_follow_hold_s: Any = 4.0,
    openwb_budget_jump_hold_s: Any = 4.0,
    stable_start_confirm_w: Any = 700.0,
    openwb_grid_window_ramp_a: Any = 5,
    openwb_grid_window_ramp_hold_s: Any = 15.0,
    stable_budget_jump_max_a: Any = 5,
    confirmed_start_direct_target: bool = True,
    storage_floor_amp_up_export_w: Any = 500.0,
    storage_floor_amp_up_hold_s: Any = 15.0,
    stable_budget_jump_deadband_a: Any = 3,
    stable_budget_jump_hold_s: Any = 6.0,
) -> Dict[str, Any]:
    """Dämpft Stromänderungen der Wallbox während eines aktiv geregelten Ladevorgangs."""

    def amp_int(value: Any, default: int = 0) -> int:
        try:
            return int(_safe_float(value, float(default)))
        except Exception:
            return int(default)

    proposed_i = amp_int(proposed_amp, 0)
    current_i = amp_int(current_amp, 0)
    now = _safe_float(now_ts, 0.0)
    grid_w = _safe_float(grid_power_w, 0.0)
    threshold_w = _safe_float(fast_grid_threshold_w, 150.0)
    openwb_like = charger_class_name in ("OpenWBCharger", "OpenWBProCharger")
    follow_s = max(2.0, _safe_float(stable_follow_hold_s, 4.0))
    if openwb_like:
        follow_s = min(
            follow_s,
            max(2.0, _safe_float(openwb_budget_jump_hold_s, 4.0)),
        )
    confirm_w = max(400.0, _safe_float(stable_start_confirm_w, 700.0))
    real_confirmed = bool(
        status_fresh
        and (real_charging or _safe_float(real_power_w, 0.0) >= confirm_w)
    )
    state_updates: Dict[str, Any] = {}

    def result(applied: int, reason: str) -> Dict[str, Any]:
        return {
            "schema_version": "wallbox_stable_amp_v1",
            "applied_amp": int(applied),
            "proposed_amp": int(proposed_i),
            "current_amp": int(current_i),
            "limited": bool(int(applied) != proposed_i),
            "direction": "up" if int(applied) > current_i else ("down" if int(applied) < current_i else "flat"),
            "reason": reason,
            "real_confirmed": bool(real_confirmed),
            "status_fresh": bool(status_fresh),
            "openwb_like": bool(openwb_like),
            "follow_s": float(follow_s),
            "state_updates": dict(state_updates),
        }

    if proposed_i <= 0:
        state_updates["_wb_stable_budget_jump_done"] = False
        return result(proposed_i, "target_zero")

    if current_i > 0 and real_confirmed and proposed_i > current_i:
        # Der sichere Startdeckel bleibt bis zum frischen Lade-/Leistungsbeleg
        # bestehen. Danach ist der vorgeschlagene Wert bereits der final
        # autorisierte Sollstrom. Eine zweite +1-A-/+5-A-Rampe erzeugt keinen
        # Hardwareschutz, sondern lässt nutzbare Leistung liegen.
        state_updates["_wb_stable_budget_jump_done"] = True
        state_updates["_wb_stable_budget_jump_ts"] = now
        state_updates["last_storage_guided_amp_up_ts"] = now
        if openwb_like:
            state_updates["_last_openwb_grid_window_amp_up_ts"] = now
        return result(proposed_i, "confirmed_direct_up")

    grid_window_active = bool(grid_allowed or price_active or price_boost_active)
    if grid_window_active:
        state_updates["_wb_stable_budget_jump_done"] = True
        state_updates["_wb_stable_budget_jump_ts"] = now
        if openwb_like:
            grid_ramp_a = max(1, min(5, amp_int(openwb_grid_window_ramp_a, 5)))
            grid_ramp_hold_s = max(5.0, _safe_float(openwb_grid_window_ramp_hold_s, 15.0))
            last_grid_up = _safe_float(last_openwb_grid_window_amp_up_ts, 0.0)
            if current_i <= 0:
                state_updates["_last_openwb_grid_window_amp_up_ts"] = now
                state_updates["_wb_stable_budget_jump_done"] = False
                return result(min(proposed_i, 6), "grid_window_start_deck")
            if not real_confirmed and proposed_i > current_i:
                return result(current_i, "grid_window_wait_real_power")
            if proposed_i > current_i + grid_ramp_a:
                if now - last_grid_up < grid_ramp_hold_s:
                    return result(current_i, "grid_window_ramp_hold")
                state_updates["_last_openwb_grid_window_amp_up_ts"] = now
                return result(current_i + grid_ramp_a, "grid_window_ramp_step")
            if proposed_i > current_i:
                if now - last_grid_up < grid_ramp_hold_s:
                    return result(current_i, "grid_window_ramp_hold")
                state_updates["_last_openwb_grid_window_amp_up_ts"] = now
            elif proposed_i < current_i:
                state_updates["_last_openwb_grid_window_amp_up_ts"] = now
        return result(proposed_i, "grid_window_direct")

    budget_jump_max_a = max(1, min(8, amp_int(stable_budget_jump_max_a, 5)))
    last_wb_up = _safe_float(last_storage_guided_amp_up_ts, 0.0)
    fast_until = _safe_float(fast_block_until, 0.0)
    if current_i <= 0:
        state_updates["_wb_stable_budget_jump_done"] = False
        return result(min(proposed_i, 6), "start_deck")
    if not real_confirmed:
        state_updates["_wb_stable_budget_jump_done"] = False
        if proposed_i > current_i:
            return result(current_i, "wait_real_power")
        return result(proposed_i, "down_without_real_power")

    storage_floor_cautious = bool(
        openwb_like
        and storage_floor_mode_active
        and not (grid_allowed or price_active or price_boost_active or predump_active)
        and grid_w > -max(0.0, _safe_float(storage_floor_amp_up_export_w, 500.0))
    )
    if proposed_i > current_i and storage_floor_cautious:
        amp_up_hold_s = max(7.0, _safe_float(storage_floor_amp_up_hold_s, 15.0))
        if now - last_wb_up < amp_up_hold_s:
            return result(current_i, "storage_floor_amp_up_hold")
        state_updates["last_storage_guided_amp_up_ts"] = now
        state_updates["_wb_stable_budget_jump_done"] = True
        state_updates["_wb_stable_budget_jump_ts"] = now
        return result(min(proposed_i, current_i + 1), "storage_floor_amp_up_step")

    if not stable_budget_jump_done:
        if now < fast_until:
            return result(current_i, "fast_block_initial")
        if predump_active:
            if now - last_wb_up < follow_s:
                return result(current_i, "predump_follow_hold")
            state_updates["_wb_stable_budget_jump_ts"] = now
            return result(min(proposed_i, current_i + 1), "predump_follow_step")
        state_updates["_wb_stable_budget_jump_done"] = True
        state_updates["_wb_stable_budget_jump_ts"] = now
        if confirmed_start_direct_target:
            return result(proposed_i, "confirmed_start_direct")
        return result(min(proposed_i, current_i + budget_jump_max_a), "confirmed_start_budget_jump")

    if proposed_i > current_i:
        deadband_a = max(2, amp_int(stable_budget_jump_deadband_a, 3))
        budget_jump_hold_s = max(follow_s, _safe_float(stable_budget_jump_hold_s, 45.0))
        last_jump = _safe_float(stable_budget_jump_ts, 0.0)
        if (
            not predump_active
            and proposed_i - current_i >= deadband_a
            and now >= fast_until
            and now - last_jump >= budget_jump_hold_s
            and grid_w <= threshold_w
        ):
            state_updates["_wb_stable_budget_jump_ts"] = now
            if confirmed_start_direct_target:
                return result(proposed_i, "budget_jump_direct")
            return result(min(proposed_i, current_i + budget_jump_max_a), "budget_jump_step")
        if now < fast_until or now - last_wb_up < follow_s:
            return result(current_i, "amp_up_follow_hold")
        return result(min(proposed_i, current_i + 1), "amp_up_step")

    if proposed_i < current_i:
        if budget_timeout:
            return result(proposed_i, "budget_timeout_down")
        if physical_amp_down_active:
            state_updates["_wb_stable_budget_jump_ts"] = now
            state_updates["last_storage_guided_amp_down_ts"] = now
            return result(proposed_i, "physical_amp_down")
        if grid_w <= threshold_w:
            return result(current_i, "hold_load_without_import")
        last_wb_down = _safe_float(last_storage_guided_amp_down_ts, 0.0)
        if now - last_wb_down < follow_s:
            return result(current_i, "amp_down_follow_hold")
        return result(max(proposed_i, current_i - 1), "amp_down_step")

    return result(proposed_i, "stable")


def pv_hybrid_energy_gate(
    *,
    previous: Optional[Dict[str, Any]] = None,
    now_ts: Any = 0.0,
    budget_w: Any = 0.0,
    min_power_w: Any = 1380.0,
    cap_amp: Any = 0,
    min_amp: Any = 6,
    current_amp: Any = 0,
    current_set_amp: Any = 0,
    hw_charging: bool = False,
    hw_power_w: Any = 0.0,
    grid_power_w: Any = 0.0,
    charger_connected: bool = False,
    start_hold_s: Any = 60.0,
    start_energy_wh: Any = 35.0,
    strong_surplus_w: Any = 1500.0,
    stop_hold_s: Any = 180.0,
    stop_energy_wh: Any = 75.0,
    hard_import_w: Any = 2500.0,
    ordinary_grid_import_sequence_active: bool = False,
    enabled: bool = True,
) -> Dict[str, Any]:
    """Sammelt PV-Energiebelege für ruhige Start-/Stoppentscheidungen der Wallbox.

    Bei starkem Überschuss darf die Regelung schnell starten, ein nur knapp
    ausreichendes 6-A-Fenster muss jedoch zeitlich oder energetisch Bestand
    haben. Eine laufende Ladung wird dagegen am physikalischen Mindeststrom
    überbrückt, bis das konfigurierte Energiedefizit wirklich verbraucht ist.
    Die verstrichene Zeit ist dabei nur Diagnose und niemals eine eigenständige
    Stopfreigabe. Gewöhnlicher Netzbezug einer laufenden PV-Ladung wird vom
    zentralen Mindeststrom-/Importintegral behandelt und darf dieses ältere
    Energiegate nicht als Sofort-Stopp umgehen. Das negative Integral nutzt die
    gemessene oder konservativ abgeleitete laufende Leistung. Deshalb verbraucht
    ein großes, batteriegestütztes Defizit den Übergangspuffer früher als eine
    kurze kleine Wolkendelle.
    """

    prev = previous if isinstance(previous, dict) else {}
    now = max(0.0, _safe_float(now_ts, 0.0))
    last = max(0.0, _safe_float(prev.get("ts", 0.0), 0.0))
    if now > 0.0 and last > 0.0:
        dt_s = max(0.0, min(120.0, now - last))
    else:
        dt_s = 0.0

    budget = max(0.0, _safe_float(budget_w, 0.0))
    min_power = max(1.0, _safe_float(min_power_w, 1380.0))
    cap = max(0, _safe_int(cap_amp, 0))
    minimum = max(1, _safe_int(min_amp, 6))
    current = max(0, _safe_int(current_amp, 0))
    set_amp = max(0, _safe_int(current_set_amp, 0))
    hw_power = max(0.0, _safe_float(hw_power_w, 0.0))
    grid_w = _safe_float(grid_power_w, 0.0)
    start_hold = max(0.0, _safe_float(start_hold_s, 60.0))
    start_wh = max(0.0, _safe_float(start_energy_wh, 35.0))
    strong_w = max(0.0, _safe_float(strong_surplus_w, 1500.0))
    stop_hold = max(0.0, _safe_float(stop_hold_s, 180.0))
    stop_wh = max(0.0, _safe_float(stop_energy_wh, 75.0))
    hard_import = max(0.0, _safe_float(hard_import_w, 2500.0))

    running = bool(
        charger_connected
        and (
            hw_charging
            or hw_power > 500.0
            or (current >= minimum and hw_power > 100.0)
        )
    )
    inferred_running_power_w = max(current, set_amp) * min_power / float(minimum)
    running_power_w = (
        hw_power
        if hw_power > 500.0
        else max(0.0, inferred_running_power_w)
    )
    uncovered_hold_w = max(0.0, running_power_w - budget, grid_w) if running else 0.0
    hard_stop = bool(
        grid_w >= hard_import
        and not ordinary_grid_import_sequence_active
    )
    start_signal = bool(
        charger_connected
        and not running
        and (cap >= minimum or budget >= max(1.0, min_power - 120.0))
        and budget >= max(1.0, min_power - 120.0)
        and not hard_stop
    )
    stop_signal = bool(
        running
        and (cap < minimum or budget < min_power or hard_stop)
    )
    strong_start = bool(start_signal and (budget >= min_power + strong_w or cap >= minimum + 2))

    positive_wh = max(0.0, _safe_float(prev.get("positive_wh", 0.0), 0.0))
    negative_wh = max(0.0, _safe_float(prev.get("negative_wh", 0.0), 0.0))
    positive_age = max(0.0, _safe_float(prev.get("positive_age_s", 0.0), 0.0))
    negative_age = max(0.0, _safe_float(prev.get("negative_age_s", 0.0), 0.0))

    if not enabled:
        positive_wh = 0.0
        negative_wh = 0.0
        positive_age = 0.0
        negative_age = 0.0
    elif start_signal:
        positive_age += dt_s
        positive_wh += budget * dt_s / 3600.0
        negative_age = 0.0
        negative_wh = 0.0
    elif stop_signal:
        positive_age = 0.0
        positive_wh = 0.0
        if ordinary_grid_import_sequence_active:
            negative_age = 0.0
            negative_wh = 0.0
        else:
            negative_age += dt_s
            negative_wh += uncovered_hold_w * dt_s / 3600.0
    else:
        positive_age = 0.0
        positive_wh = 0.0
        negative_age = 0.0
        negative_wh = 0.0

    start_allowed = bool(
        not enabled
        or running
        or not start_signal
        or strong_start
        or positive_age >= start_hold
        or positive_wh >= start_wh
    )
    stop_energy_reached = bool(stop_wh <= 0.0 or negative_wh >= stop_wh)
    stop_allowed = bool(
        not enabled
        or not stop_signal
        or (
            not ordinary_grid_import_sequence_active
            and (
                hard_stop
                or stop_energy_reached
            )
        )
    )

    reason = "disabled"
    if enabled:
        if hard_stop:
            reason = "hard_import"
        elif start_signal and not running:
            reason = "start_allowed" if start_allowed else "start_integral_wait"
        elif stop_signal and ordinary_grid_import_sequence_active:
            reason = "minimum_current_import_sequence"
        elif stop_signal:
            reason = "stop_allowed" if stop_allowed else "stop_integral_hold"
        else:
            reason = "neutral"

    return {
        "schema_version": "wallbox_pv_hybrid_energy_gate_v1",
        "enabled": bool(enabled),
        "ts": float(now),
        "dt_s": float(dt_s),
        "budget_w": float(budget),
        "min_power_w": float(min_power),
        "cap_amp": int(cap),
        "min_amp": int(minimum),
        "running": bool(running),
        "running_power_w": float(running_power_w),
        "uncovered_hold_w": float(uncovered_hold_w),
        "start_signal": bool(start_signal),
        "stop_signal": bool(stop_signal),
        "strong_start": bool(strong_start),
        "hard_stop": bool(hard_stop),
        "ordinary_grid_import_sequence_active": bool(
            ordinary_grid_import_sequence_active
        ),
        "positive_wh": float(positive_wh),
        "negative_wh": float(negative_wh),
        "positive_age_s": float(positive_age),
        "negative_age_s": float(negative_age),
        "stop_hold_s": float(stop_hold),
        "stop_energy_wh": float(stop_wh),
        "stop_energy_reached": bool(stop_energy_reached),
        "start_allowed": bool(start_allowed),
        "stop_allowed": bool(stop_allowed),
        "hold_allowed": bool(stop_signal and not stop_allowed),
        "reason": reason,
    }


def pv_hybrid_hold_action(
    *,
    gate: Optional[Dict[str, Any]] = None,
    enabled: bool = True,
    cap_amp: Any = 0,
    current_amp: Any = 0,
    current_set_amp: Any = 0,
    min_amp: Any = 6,
    max_amp: Any = 32,
    allowed_w: Any = 0.0,
    min_power_w: Any = 1380.0,
    gate_running: bool = False,
    mode_switch_quiet_active: bool = False,
    mode_switch_quiet_remaining_s: Any = 0.0,
    floor_battery_guard_active: bool = False,
) -> Dict[str, Any]:
    """Übersetzt das PV-Hybrid-Energiegate in eine explizite Halte-/Freigabeaktion."""

    st = gate if isinstance(gate, dict) else {}
    def amp_int(value: Any, default: int = 0) -> int:
        try:
            return int(_safe_float(value, float(default)))
        except Exception:
            return int(default)

    cap = max(0, amp_int(cap_amp, 0))
    minimum = max(1, amp_int(min_amp, 6))
    maximum = max(minimum, amp_int(max_amp, 32))
    current = max(0, amp_int(current_amp, 0))
    set_amp = max(0, amp_int(current_set_amp, 0))
    min_power = max(0.0, _safe_float(min_power_w, 1380.0))
    budget_w = max(0.0, _safe_float(allowed_w, 0.0))
    running = bool(gate_running or st.get("running", False))
    start_allowed = bool(st.get("start_allowed", True))
    hold_allowed = bool(st.get("hold_allowed", False))
    import_sequence_active = bool(
        st.get("ordinary_grid_import_sequence_active", False)
    )
    quiet = bool(mode_switch_quiet_active)

    if enabled and cap > 0 and not running and (not start_allowed or quiet):
        if quiet:
            return {
                "action": "HOLD_START_PV_HYBRID",
                "target_amp": 0,
                "allowed_w": budget_w,
                "min_power_w": min_power,
                "log_key": "pv_curve_mode_switch_quiet_wait",
                "reason": "mode_switch_quiet",
                "quiet_remaining_s": max(0.0, _safe_float(mode_switch_quiet_remaining_s, 0.0)),
                "positive_age_s": _safe_float(st.get("positive_age_s", 0.0), 0.0),
                "positive_wh": _safe_float(st.get("positive_wh", 0.0), 0.0),
            }
        return {
            "action": "HOLD_START_PV_HYBRID",
            "target_amp": 0,
            "allowed_w": budget_w,
            "min_power_w": min_power,
            "log_key": "pv_hybrid_start_integral_wait",
            "reason": "start_integral_wait",
            "quiet_remaining_s": 0.0,
            "positive_age_s": _safe_float(st.get("positive_age_s", 0.0), 0.0),
            "positive_wh": _safe_float(st.get("positive_wh", 0.0), 0.0),
        }

    if (
        enabled
        and cap <= 0
        and running
        and hold_allowed
        and not floor_battery_guard_active
    ):
        if import_sequence_active:
            # Typisierter Non-Output-Hold: Zwischen zwei Fast-Grid-Takten darf
            # kein älterer Nullbudgetpfad stoppen oder einen Strom setzen. Die
            # bestätigte Hardwareausgabe bleibt unangetastet; nur Fast-Grid
            # darf sie reduzieren.
            return {
                "action": "HOLD_GRID_IMPORT_SEQUENCE",
                "target_amp": 0.0,
                "allowed_w": budget_w,
                "min_power_w": min_power,
                "log_key": "minimum_current_import_sequence_hold",
                "reason": "minimum_current_import_sequence",
                "ordinary_grid_import_sequence_active": True,
                "negative_age_s": 0.0,
                "negative_wh": 0.0,
            }

        # Kein Vollstrom aus dem Akku: Bei fehlendem nachhaltigem Budget wird
        # zuerst auf den fahrzeugseitigen Mindeststrom abgesenkt. So überbrückt
        # die Hysterese kurze PV-Dellen ohne unnötiges Schalten, bleibt aber ein
        # kleiner Übergangspuffer und kein verdeckter Modus "PV + Akku".
        hold_amp = minimum
        return {
            "action": "HOLD_STOP_PV_HYBRID",
            "target_amp": float(hold_amp),
            "allowed_w": max(budget_w, min_power),
            "min_power_w": min_power,
            "log_key": "pv_hybrid_stop_integral_hold",
            "reason": "stop_integral_hold",
            "negative_age_s": _safe_float(st.get("negative_age_s", 0.0), 0.0),
            "negative_wh": _safe_float(st.get("negative_wh", 0.0), 0.0),
        }

    return {
        "action": "ALLOW_PV_HYBRID",
        "target_amp": int(cap),
        "allowed_w": budget_w,
        "min_power_w": min_power,
        "log_key": "",
        "reason": str(st.get("reason", "allow") or "allow"),
    }


def zero_budget_contract_from_pv_hybrid_gate(
    gate: Optional[Dict[str, Any]],
) -> Dict[str, Any]:
    """Normalisiert das gemeinsame PV-Hybrid-Gate für die Start-/Stopp-Policy.

    Die Zeit- und Energiebilanz ist für alle regelbaren Wallboxtypen gleich.
    Treiberspezifische Alterszähler bleiben nur noch ein Legacy-Fallback, wenn
    kein gültiger Vertrag aus dem gemeinsamen Gate vorliegt.
    """

    st = gate if isinstance(gate, dict) else {}
    valid = str(st.get("schema_version") or "") == "wallbox_pv_hybrid_energy_gate_v1"
    enabled = bool(st.get("enabled", False))
    active = bool(
        valid
        and enabled
        and st.get("running", False)
        and st.get("stop_signal", False)
    )
    hard_stop = bool(active and st.get("hard_stop", False))
    hold_allowed = bool(active and st.get("hold_allowed", False) and not hard_stop)
    import_sequence_active = bool(
        active
        and hold_allowed
        and st.get("ordinary_grid_import_sequence_active", False)
    )
    stop_allowed = bool(active and (st.get("stop_allowed", False) or hard_stop))
    released = bool(active and stop_allowed and not hold_allowed)
    return {
        "schema_version": "wallbox_zero_budget_contract_v1",
        "source": "pv_hybrid_energy_gate",
        "source_valid": bool(valid),
        "enabled": bool(enabled),
        "active": bool(active),
        "age_s": max(0.0, _safe_float(st.get("negative_age_s", 0.0), 0.0)),
        "deficit_wh": max(0.0, _safe_float(st.get("negative_wh", 0.0), 0.0)),
        "hold_allowed": bool(hold_allowed),
        "ordinary_grid_import_sequence_active": bool(
            import_sequence_active
        ),
        "stop_allowed": bool(stop_allowed),
        "released": bool(released),
        "hard_stop": bool(hard_stop),
        "reason": str(st.get("reason", "invalid") or "invalid"),
    }


def native_verified_pv_sink_hold_contract(
    *,
    e3dc_native_toggle: bool,
    control_mode: int,
    charger_connected: bool,
    hw_charging: bool,
    hw_power_w: float,
    cap_amp: float,
    budget_ok: bool,
    live_sample_invalid: bool,
    status_valid: bool,
    status_stale: bool,
    phase_power_verified: bool,
    priority_forced_stop: bool,
    budget_timeout: bool,
    local_price_optimizing_active: bool,
    local_grid_allowed: bool,
    price_boost_wallbox_active: bool,
    predump_wallbox_active: bool,
    native_battery_drain_zero_budget_active: bool,
    grid_power_w: float,
    battery_power_w: float,
    pv_power_w: float,
) -> Dict[str, Any]:
    """Schützt eine bereits bestätigte native PV-Senke vor Speicherverdrängung.

    Dies startet niemals ein stehendes Fahrzeug und lockert nie die reguläre
    Startschwelle. Es hält ausschließlich einen physikalisch bestätigten
    Ladevorgang, während frische PV-Leistung zugleich den Hausspeicher lädt und
    am Netzpunkt kein wesentlicher Bezug vorliegt. Alle expliziten Schutz- und
    Netzladepfade bleiben harte Blocker.
    """

    hw_power = max(0.0, _safe_float(hw_power_w, 0.0))
    grid_w = _safe_float(grid_power_w, 0.0)
    battery_w = _safe_float(battery_power_w, 0.0)
    pv_w = max(0.0, _safe_float(pv_power_w, 0.0))
    active = bool(
        e3dc_native_toggle
        and int(round(_safe_float(control_mode, 0))) in (2, 3, 6)
        and charger_connected
        and hw_charging
        and hw_power > 500.0
        and _safe_float(cap_amp, 0.0) <= 0.0
        and budget_ok
        and not live_sample_invalid
        and status_valid
        and not status_stale
        and phase_power_verified
        and not priority_forced_stop
        and not budget_timeout
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
        and not predump_wallbox_active
        and not native_battery_drain_zero_budget_active
        and grid_w <= 250.0
        and battery_w > 500.0
        and pv_w > hw_power + 500.0
    )
    return {
        "active": active,
        "reason": "verified_native_pv_sink" if active else "not_eligible",
        "hw_power_w": hw_power,
        "grid_power_w": grid_w,
        "battery_power_w": battery_w,
        "pv_power_w": pv_w,
    }


def start_stop_hold_action(
    *,
    cap_amp: float,
    current_amp: float,
    current_set_amp: float,
    charger_connected: bool,
    hw_charging: bool,
    hw_power_w: float,
    control_mode: int,
    last_start_age_s: float,
    min_charge_time_s: float,
    priority_forced_stop: bool,
    local_price_optimizing_active: bool,
    local_grid_allowed: bool,
    price_boost_wallbox_active: bool,
    budget_timeout: bool,
    grid_power_w: float,
    is_multi_direct_toggle: bool,
    wbminsoc_gate_open: bool,
    multi_phase_verified: bool,
    native_multi_zero_budget_age_s: float,
    openwb_like_charger: bool,
    openwb_pro: bool,
    abort_cooldown_age_s: float,
    budget_ok: bool,
    budget_storage_state: str,
    openwb_zero_budget_age_s: float,
    cloud_stop_delay_s: float,
    predump_wallbox_active: bool,
    phase_forecast_hold_for_wb: bool,
    phase_down_grid_w: float,
    phase_forecast_zero_hold_s: float,
    stop_already_sent: bool,
    stop_retry_due: bool,
    e3dc_native_toggle: bool,
    native_start_grace_active: bool,
    is_charging_memory: bool,
    effective_budget_w: float = 0.0,
    openwb_zero_export_hold_allowed: bool = True,
    openwb_phase_transition_grace_active: bool = False,
    transient_contract: Optional[Dict[str, Any]] = None,
    native_battery_drain_zero_budget_active: bool = False,
    native_verified_pv_sink_hold_active: bool = False,
    openwb_floor_zero_budget_stop_active: bool = False,
    zero_budget_contract: Optional[Dict[str, Any]] = None,
    non_native_idle_zero_confirmed: bool = False,
) -> Dict[str, Any]:
    """Wählt die übergeordnete Wallbox-Aktion vor jedem Treiberbefehl.

    Timer, Protokollierung und echte Treiberaufrufe bleiben beim Manager. Diese
    Hilfe übersetzt lediglich bereits erfasste Fakten in eine reine
    START/SET/HOLD/STOP-Entscheidung, damit Grenzfälle ohne Wallbox testbar sind.
    """
    cap = max(0.0, _safe_float(cap_amp, 0.0))
    current = max(0.0, _safe_float(current_amp, 0.0))
    set_amp = max(0.0, _safe_float(current_set_amp, 0.0))
    hw_power = max(0.0, _safe_float(hw_power_w, 0.0))
    grid_w = _safe_float(grid_power_w, 0.0)
    mode = int(round(_safe_float(control_mode, 0)))
    start_age = _safe_float(last_start_age_s, 999999.0)
    min_charge_s = max(0.0, _safe_float(min_charge_time_s, 0.0))
    cloud_hold_s = max(0.0, _safe_float(cloud_stop_delay_s, 0.0))
    native_zero_age = max(0.0, _safe_float(native_multi_zero_budget_age_s, 0.0))
    openwb_zero_age = max(0.0, _safe_float(openwb_zero_budget_age_s, 0.0))
    phase_down_w = max(0.0, _safe_float(phase_down_grid_w, 0.0))
    phase_zero_s = max(0.0, _safe_float(phase_forecast_zero_hold_s, 0.0))
    storage_state = str(budget_storage_state or "")
    native_battery_drain_zero_budget_active = bool(native_battery_drain_zero_budget_active)
    native_verified_pv_sink_hold_active = bool(native_verified_pv_sink_hold_active)
    openwb_floor_zero_budget_stop_active = bool(openwb_floor_zero_budget_stop_active)
    if native_battery_drain_zero_budget_active:
        native_zero_age = max(native_zero_age, cloud_hold_s + 1.0)
    transient = transient_contract if isinstance(transient_contract, dict) else {}
    openwb_phase_transition_grace_active = bool(
        openwb_phase_transition_grace_active
        or transient.get("phase_transition_grace_active", False)
    )
    native_start_grace_active = bool(
        native_start_grace_active
        or transient.get("native_start_grace_active", False)
    )
    transient_hold_active = bool(transient.get("active", False))
    transient_offer_active = bool(
        transient_hold_active
        and transient.get("active_or_offered", False)
    )
    transient_hold_reason = str(transient.get("reason", "") or "")

    zero_budget = zero_budget_contract if isinstance(zero_budget_contract, dict) else {}
    zero_budget_valid = bool(
        str(zero_budget.get("schema_version") or "") == "wallbox_zero_budget_contract_v1"
        and zero_budget.get("source_valid", False)
    )
    zero_budget_active = bool(zero_budget_valid and zero_budget.get("active", False))
    zero_budget_hold_allowed = bool(
        zero_budget_active
        and zero_budget.get("hold_allowed", False)
        and not zero_budget.get("hard_stop", False)
    )
    zero_budget_import_sequence_active = bool(
        zero_budget_hold_allowed
        and zero_budget.get(
            "ordinary_grid_import_sequence_active",
            False,
        )
    )
    zero_budget_stop_allowed = bool(
        zero_budget_active
        and zero_budget.get("stop_allowed", False)
        and not zero_budget_hold_allowed
    )
    zero_budget_hard_stop = bool(
        zero_budget_active and zero_budget.get("hard_stop", False)
    )
    zero_budget_age = (
        max(0.0, _safe_float(zero_budget.get("age_s", 0.0), 0.0))
        if zero_budget_active
        else (openwb_zero_age if openwb_like_charger else native_zero_age)
    )
    zero_budget_deficit_wh = (
        max(0.0, _safe_float(zero_budget.get("deficit_wh", 0.0), 0.0))
        if zero_budget_active
        else 0.0
    )
    timed_zero_budget_hold_allowed = bool(
        not zero_budget_active or zero_budget_hold_allowed
    )
    non_native_output_active_or_offered = bool(
        hw_charging
        or hw_power > 500.0
        or (
            charger_connected
            and (
                is_charging_memory
                or current > 0.5
                or set_amp > 0.5
            )
        )
    )
    non_native_stop_edge_due = bool(
        stop_retry_due
        or (
            not stop_already_sent
            and (
                non_native_output_active_or_offered
                or not bool(non_native_idle_zero_confirmed)
            )
        )
    )

    # Ein freigegebener gemeinsamer Gate-Stopp und der native Akkuentladungs-
    # Schutz stehen vor sämtlichen Haltepfaden. Auch ein inkonsistenter positiver
    # Upstream-Cap darf diese Stop-Autorität nicht wieder öffnen.
    zero_budget_stop_requested = bool(
        priority_forced_stop
        or zero_budget_hard_stop
        or zero_budget_stop_allowed
        or native_battery_drain_zero_budget_active
    )
    if zero_budget_stop_requested:
        cap = 0.0

        if priority_forced_stop:
            priority_stop_edge = priority_forced_stop_edge_contract(
                priority_forced_stop=True,
                charger_connected=charger_connected,
                hw_charging=hw_charging,
                hw_power_w=hw_power,
                current_amp=current,
                current_set_amp=set_amp,
                is_charging_memory=is_charging_memory,
                stop_already_sent=stop_already_sent,
                stop_retry_due=stop_retry_due,
                e3dc_native_toggle=e3dc_native_toggle,
            )
            stop_edge_due = bool(
                priority_stop_edge.get("stop_edge_due", False)
            )
        else:
            native_real_charge = bool(hw_charging and hw_power > 500.0)
            if e3dc_native_toggle:
                stop_edge_due = bool(
                    (native_real_charge and not stop_already_sent)
                    or stop_retry_due
                )
            else:
                stop_edge_due = non_native_stop_edge_due
        if priority_forced_stop:
            stop_reason = "priority_forced_stop"
        elif zero_budget_hard_stop:
            stop_reason = "zero_budget_hard_stop"
        elif native_battery_drain_zero_budget_active:
            stop_reason = "native_battery_drain_zero_budget"
        else:
            stop_reason = "zero_budget_stop_allowed"
        return {
            "action": "STOP",
            "target_amp": 0.0,
            "hold_amp": 0.0,
            "is_new_start": False,
            "min_charge_hold_active": False,
            "multi_zero_budget_hold": False,
            "openwb_zero_budget_hold": False,
            "native_running_charge_hold": False,
            "native_current_down_hold": False,
            "native_mode_no_stop_wait": False,
            "native_start_grace_active": False,
            "native_battery_drain_zero_budget_active": bool(
                native_battery_drain_zero_budget_active
            ),
            "native_verified_pv_sink_hold_active": False,
            "openwb_phase_transition_grace_active": False,
            "openwb_phase_transition_offer_active": False,
            "transient_hold_active": False,
            "transient_offer_active": False,
            "transient_hold_reason": "",
            "zero_budget_contract_valid": bool(zero_budget_valid),
            "zero_budget_contract_active": bool(zero_budget_active),
            "zero_budget_hold_allowed": False,
            "zero_budget_stop_allowed": bool(zero_budget_stop_allowed),
            "zero_budget_hard_stop": bool(zero_budget_hard_stop),
            "zero_budget_age_s": float(zero_budget_age),
            "zero_budget_deficit_wh": float(zero_budget_deficit_wh),
            "zero_budget_contract_source": str(
                zero_budget.get("source", "legacy_timer") or "legacy_timer"
            ),
            "need_stop_toggle": bool(stop_edge_due),
            "reason": stop_reason,
        }

    vehicle_finished_drop_pending = bool(
        openwb_pro
        and transient.get("vehicle_finished_drop_pending_active", False)
    )
    pending_offer_amp = max(current, set_amp)
    if (
        vehicle_finished_drop_pending
        and cap >= 6.0
        and pending_offer_amp >= 6.0
    ):
        pending_target_amp = min(cap, pending_offer_amp)
        return {
            "action": "HOLD_OPENWB_FINISH_CONFIRM",
            "target_amp": float(pending_target_amp),
            "hold_amp": float(pending_target_amp),
            "is_new_start": False,
            "min_charge_hold_active": False,
            "multi_zero_budget_hold": False,
            "openwb_zero_budget_hold": False,
            "native_mode_no_stop_wait": False,
            "native_start_grace_active": bool(native_start_grace_active),
            "native_battery_drain_zero_budget_active": False,
            "native_verified_pv_sink_hold_active": False,
            "openwb_phase_transition_grace_active": bool(
                openwb_phase_transition_grace_active
            ),
            "openwb_phase_transition_offer_active": bool(
                transient.get("phase_transition_offer_active", False)
            ),
            "transient_hold_active": True,
            "transient_offer_active": True,
            "transient_hold_reason": "vehicle_finished_drop_confirmation",
            "vehicle_finished_drop_pending": True,
            "need_stop_toggle": False,
            "zero_budget_contract_valid": bool(zero_budget_valid),
            "zero_budget_contract_active": bool(zero_budget_active),
            "zero_budget_hold_allowed": bool(zero_budget_hold_allowed),
            "zero_budget_stop_allowed": bool(zero_budget_stop_allowed),
            "zero_budget_hard_stop": bool(zero_budget_hard_stop),
            "zero_budget_age_s": float(zero_budget_age),
            "zero_budget_deficit_wh": float(zero_budget_deficit_wh),
            "zero_budget_contract_source": str(
                zero_budget.get("source", "legacy_timer")
                or "legacy_timer"
            ),
            "reason": "vehicle_finished_drop_confirmation",
        }

    if cap > 0:
        return {
            "action": "START" if current <= 0 else "SET_CURRENT",
            "target_amp": cap,
            "hold_amp": cap,
            "is_new_start": bool(current <= 0),
            "min_charge_hold_active": False,
            "multi_zero_budget_hold": False,
            "openwb_zero_budget_hold": False,
            "native_mode_no_stop_wait": False,
            "native_start_grace_active": bool(native_start_grace_active),
            "native_battery_drain_zero_budget_active": bool(native_battery_drain_zero_budget_active),
            "native_verified_pv_sink_hold_active": bool(native_verified_pv_sink_hold_active),
            "openwb_phase_transition_grace_active": bool(openwb_phase_transition_grace_active),
            "openwb_phase_transition_offer_active": bool(transient.get("phase_transition_offer_active", False)),
            "transient_hold_active": bool(transient_hold_active),
            "transient_offer_active": bool(transient_offer_active),
            "transient_hold_reason": transient_hold_reason,
            "need_stop_toggle": False,
            "reason": "target_current_available",
        }

    grid_window_active = bool(
        local_price_optimizing_active
        or local_grid_allowed
        or price_boost_wallbox_active
    )
    if (
        grid_window_active
        and charger_connected
        and not priority_forced_stop
        and not budget_timeout
        and (current > 0 or set_amp > 0 or hw_charging or hw_power > 500.0)
    ):
        return {
            "action": "HOLD_GRID_WINDOW",
            "target_amp": float(max(6, current, set_amp)),
            "hold_amp": float(max(6, current, set_amp)),
            "is_new_start": False,
            "min_charge_hold_active": False,
            "multi_zero_budget_hold": False,
            "openwb_zero_budget_hold": False,
            "native_mode_no_stop_wait": False,
            "native_start_grace_active": bool(native_start_grace_active),
            "native_battery_drain_zero_budget_active": bool(native_battery_drain_zero_budget_active),
            "native_verified_pv_sink_hold_active": bool(native_verified_pv_sink_hold_active),
            "openwb_phase_transition_grace_active": bool(openwb_phase_transition_grace_active),
            "openwb_phase_transition_offer_active": bool(transient.get("phase_transition_offer_active", False)),
            "transient_hold_active": bool(transient_hold_active),
            "transient_offer_active": bool(transient_offer_active),
            "transient_hold_reason": transient_hold_reason,
            "need_stop_toggle": False,
            "reason": "grid_window_zero_budget_hold",
        }

    wbminsoc_floor_grid_stop = bool(
        not wbminsoc_gate_open
        and grid_w > 250.0
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
    )

    if (
        zero_budget_import_sequence_active
        and charger_connected
        and mode > 0
        and wbminsoc_gate_open
        and not priority_forced_stop
        and not budget_timeout
        and not openwb_floor_zero_budget_stop_active
        and not native_battery_drain_zero_budget_active
        and not wbminsoc_floor_grid_stop
        and not (
            local_price_optimizing_active
            or local_grid_allowed
            or price_boost_wallbox_active
            or predump_wallbox_active
        )
    ):
        return {
            "action": "HOLD_GRID_IMPORT_SEQUENCE",
            "target_amp": 0.0,
            "hold_amp": 0.0,
            "is_new_start": False,
            "min_charge_hold_active": False,
            "multi_zero_budget_hold": False,
            "openwb_zero_budget_hold": False,
            "native_running_charge_hold": False,
            "native_current_down_hold": False,
            "native_mode_no_stop_wait": False,
            "native_start_grace_active": False,
            "native_battery_drain_zero_budget_active": False,
            "native_verified_pv_sink_hold_active": False,
            "openwb_phase_transition_grace_active": False,
            "openwb_phase_transition_offer_active": False,
            "transient_hold_active": False,
            "transient_offer_active": False,
            "transient_hold_reason": "",
            "zero_budget_contract_valid": bool(zero_budget_valid),
            "zero_budget_contract_active": bool(zero_budget_active),
            "zero_budget_hold_allowed": True,
            "zero_budget_stop_allowed": False,
            "zero_budget_hard_stop": False,
            "zero_budget_age_s": 0.0,
            "zero_budget_deficit_wh": 0.0,
            "ordinary_grid_import_sequence_active": True,
            "zero_budget_contract_source": str(
                zero_budget.get("source", "pv_hybrid_energy_gate")
                or "pv_hybrid_energy_gate"
            ),
            "need_stop_toggle": False,
            "reason": "minimum_current_import_sequence",
        }

    min_charge_hold_active = bool(
        min_charge_s > 0.0
        and 0.0 <= start_age < min_charge_s
        and charger_connected
        and not priority_forced_stop
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
        and not budget_timeout
        and not wbminsoc_floor_grid_stop
        and grid_w < 2500.0
        and (
            hw_charging
            or hw_power > 500.0
            or current > 0
            or set_amp > 0
        )
    )
    native_recent_or_confirmed_start = bool(
        hw_charging
        or hw_power > 500.0
        or is_charging_memory
        or native_start_grace_active
        or 0.0 <= start_age < 300.0
    )

    multi_zero_budget_hold = False
    if (
        is_multi_direct_toggle
        and not priority_forced_stop
        and mode in (9, 10)
        and wbminsoc_gate_open
        and (hw_charging or multi_phase_verified)
        and not local_price_optimizing_active
        and not local_grid_allowed
    ):
        multi_zero_budget_hold = bool(
            min_charge_hold_active
            or (
                timed_zero_budget_hold_allowed
                and (
                    (grid_w < 800.0 and zero_budget_age < 180.0)
                    or zero_budget_age < 45.0
                )
            )
        )

    openwb_zero_budget_hard_stop = bool(
        openwb_floor_zero_budget_stop_active
        or wbminsoc_floor_grid_stop
        or (
            budget_ok
            and storage_state in (
                "morning_autonomy",
                "wbmin_charge_recovery",
                "wb9_wbminsoc_hold",
            )
        )
    )
    openwb_pro_zero_budget_can_hold = False
    openwb_zero_budget_hold = False
    openwb_phase_transition_offer_active = bool(
        openwb_phase_transition_grace_active
        and (
            transient.get("phase_transition_offer_active", False)
            or hw_charging
            or hw_power > 500.0
            or current > 0
            or set_amp > 0
        )
    )
    if (
        openwb_like_charger
        and not priority_forced_stop
        and mode in (1, 2, 3, 4, 5, 6, 9, 10)
        and charger_connected
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
        and _safe_float(abort_cooldown_age_s, 999999.0) >= 60.0
    ):
        if openwb_pro:
            openwb_pro_zero_budget_can_hold = bool(
                ((hw_charging or hw_power > 500.0) and openwb_zero_export_hold_allowed)
                or openwb_phase_transition_offer_active
                or (transient.get("start_hold_active", False) and transient_offer_active)
                or (grid_w < -800.0 and openwb_zero_export_hold_allowed)
                or local_grid_allowed
                or local_price_optimizing_active
                or price_boost_wallbox_active
                or predump_wallbox_active
            )
            if openwb_zero_budget_hard_stop or not openwb_pro_zero_budget_can_hold:
                openwb_zero_budget_hold = False
            else:
                clean_hold_s = max(0.0, cloud_hold_s)
                openwb_zero_budget_hold = bool(
                    min_charge_hold_active
                    or (
                        timed_zero_budget_hold_allowed
                        and (
                            (grid_w < 700.0 and zero_budget_age < clean_hold_s)
                            or (zero_budget_age < 20.0 and grid_w < 1500.0)
                            or (
                                hw_charging
                                and hw_power > 500.0
                                and grid_w < 1500.0
                                and zero_budget_age < min(180.0, clean_hold_s)
                            )
                        )
                    )
                    or openwb_phase_transition_offer_active
                    or (transient.get("start_hold_active", False) and transient_offer_active)
                    or (
                        timed_zero_budget_hold_allowed
                        and phase_forecast_hold_for_wb
                        and grid_w < max(phase_down_w * 1.5, phase_down_w + 1200.0)
                        and zero_budget_age < phase_zero_s
                    )
                )
        else:
            openwb_zero_budget_hold = bool(
                min_charge_hold_active
                or (
                    timed_zero_budget_hold_allowed
                    and (
                        zero_budget_age < min(45.0, cloud_hold_s)
                        or (grid_w < 5000.0 and zero_budget_age < cloud_hold_s)
                    )
                )
            )

    controllable_export_cloud_hold = bool(
        (e3dc_native_toggle or openwb_like_charger)
        and not priority_forced_stop
        and not openwb_zero_budget_hard_stop
        and mode in (3, 6, 9, 10, 11)
        and charger_connected
        and not budget_timeout
        and not (stop_already_sent and not (hw_charging or hw_power > 500.0))
        and (
            not wbminsoc_floor_grid_stop
            or (
                mode in (9, 10, 11)
                and grid_w < -800.0
            )
        )
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
        and not native_battery_drain_zero_budget_active
        and (
            (
                e3dc_native_toggle
                and native_recent_or_confirmed_start
            )
            or (
                not e3dc_native_toggle
                and (hw_charging or hw_power > 500.0 or current > 0 or set_amp > 0)
            )
        )
        and (
            grid_w < -800.0
            or min_charge_hold_active
            or (
                timed_zero_budget_hold_allowed
                and zero_budget_age < cloud_hold_s
                and grid_w < 2500.0
            )
        )
    )

    native_running_charge_hold = bool(
        e3dc_native_toggle
        and not priority_forced_stop
        and mode in (2, 3, 4, 5, 6, 9, 10, 11, 12)
        and charger_connected
        and not budget_timeout
        and hw_power > 500.0
        and (
            not wbminsoc_floor_grid_stop
            or grid_w < -800.0
        )
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
        and not native_battery_drain_zero_budget_active
        and (
            grid_w < -800.0
            or min_charge_hold_active
            or native_verified_pv_sink_hold_active
            or (
                timed_zero_budget_hold_allowed
                and zero_budget_age < cloud_hold_s
                and grid_w < 2500.0
            )
        )
    )
    native_current_down_hold = bool(
        e3dc_native_toggle
        and not priority_forced_stop
        and mode in (2, 3, 4, 5, 6, 9, 10, 11, 12)
        and charger_connected
        and not budget_timeout
        and hw_power > 500.0
        and max(current, set_amp) > 6
        and not wbminsoc_floor_grid_stop
        and not local_price_optimizing_active
        and not local_grid_allowed
        and not price_boost_wallbox_active
    )

    native_has_pending_start = bool(
        native_recent_or_confirmed_start
    )

    native_mode_no_stop_wait = bool(
        e3dc_native_toggle
        and not priority_forced_stop
        and mode in (3, 6, 9, 10)
        and charger_connected
        and wbminsoc_gate_open
        and hw_power <= 500.0
        and (mode in (9, 10) or native_has_pending_start)
        and not stop_already_sent
        and not local_price_optimizing_active
        and not local_grid_allowed
    )
    if e3dc_native_toggle:
        need_stop_toggle = bool(
            (hw_charging and hw_power > 500.0 and not stop_already_sent)
            or stop_retry_due
        )
    else:
        need_stop_toggle = non_native_stop_edge_due
    if native_running_charge_hold or native_current_down_hold:
        need_stop_toggle = False

    hold_amp = float(max(6, current, set_amp))
    native_down_hold_amp = float(max(6, set_amp if set_amp > 0 else current))
    if min_charge_hold_active and not multi_zero_budget_hold and not openwb_zero_budget_hold:
        action = "HOLD_MIN_CHARGE"
        target_amp = hold_amp
    elif multi_zero_budget_hold:
        action = "HOLD_MULTI_ZERO"
        target_amp = hold_amp
    elif openwb_zero_budget_hold:
        action = "HOLD_OPENWB_ZERO"
        target_amp = 6
    elif native_current_down_hold and not native_running_charge_hold:
        action = "HOLD_NATIVE_CURRENT_DOWN"
        target_amp = native_down_hold_amp
    elif native_running_charge_hold:
        action = "HOLD_NATIVE_RUNNING_CHARGE"
        target_amp = hold_amp
    elif controllable_export_cloud_hold:
        action = "HOLD_CONTROLLABLE_EXPORT_CLOUD"
        target_amp = hold_amp
    elif native_mode_no_stop_wait:
        action = "HOLD_NATIVE_NO_STOP_WAIT"
        target_amp = 6
    elif native_start_grace_active and not priority_forced_stop and not hw_charging and not stop_already_sent:
        action = "HOLD_NATIVE_START_GRACE"
        target_amp = max(6, current, set_amp)
    elif (
        e3dc_native_toggle
        and not priority_forced_stop
        and mode in (3, 6, 9, 10)
        and native_recent_or_confirmed_start
        and not hw_charging
        and hw_power <= 500.0
        and not stop_already_sent
    ):
        action = "HOLD_NATIVE_START_CAP"
        target_amp = max(6, current, set_amp)
    elif need_stop_toggle:
        action = "STOP"
        target_amp = 0
    elif e3dc_native_toggle:
        action = "SUPPRESS_NATIVE_STOP"
        target_amp = 0
    else:
        action = "NOOP"
        target_amp = 0

    return {
        "action": action,
        "target_amp": float(target_amp),
        "hold_amp": float(target_amp if action == "HOLD_NATIVE_CURRENT_DOWN" else hold_amp),
        "is_new_start": False,
        "min_charge_hold_active": bool(min_charge_hold_active),
        "multi_zero_budget_hold": bool(multi_zero_budget_hold),
        "openwb_zero_budget_hold": bool(openwb_zero_budget_hold),
        "openwb_zero_budget_hard_stop": bool(openwb_zero_budget_hard_stop),
        "openwb_floor_zero_budget_stop_active": bool(openwb_floor_zero_budget_stop_active),
        "openwb_pro_zero_budget_can_hold": bool(openwb_pro_zero_budget_can_hold),
        "openwb_phase_transition_grace_active": bool(openwb_phase_transition_grace_active),
        "openwb_phase_transition_offer_active": bool(openwb_phase_transition_offer_active),
        "transient_hold_active": bool(transient_hold_active),
        "transient_offer_active": bool(transient_offer_active),
        "transient_hold_reason": transient_hold_reason,
        "wbminsoc_floor_grid_stop": bool(wbminsoc_floor_grid_stop),
        "controllable_export_cloud_hold": bool(controllable_export_cloud_hold),
        "native_running_charge_hold": bool(native_running_charge_hold),
        "native_current_down_hold": bool(native_current_down_hold),
        "native_mode_no_stop_wait": bool(native_mode_no_stop_wait),
        "native_start_grace_active": bool(native_start_grace_active),
        "native_battery_drain_zero_budget_active": bool(native_battery_drain_zero_budget_active),
        "native_verified_pv_sink_hold_active": bool(native_verified_pv_sink_hold_active),
        "zero_budget_contract_valid": bool(zero_budget_valid),
        "zero_budget_contract_active": bool(zero_budget_active),
        "zero_budget_hold_allowed": bool(zero_budget_hold_allowed),
        "zero_budget_stop_allowed": bool(zero_budget_stop_allowed),
        "zero_budget_hard_stop": bool(zero_budget_hard_stop),
        "zero_budget_age_s": float(zero_budget_age),
        "zero_budget_deficit_wh": float(zero_budget_deficit_wh),
        "zero_budget_contract_source": str(zero_budget.get("source", "legacy_timer") or "legacy_timer"),
        "need_stop_toggle": bool(need_stop_toggle),
        "reason": "zero_budget_stop" if action == "STOP" else action.lower(),
    }


def start_stop_effective_action_contract(
    start_stop_decision: Optional[Dict[str, Any]],
    *,
    floor_pv_only_guard_for_wb: bool = False,
    controlled_floor_battery_guard_active: bool = False,
    current_amp: Any = 0,
    current_set_amp: Any = 0,
    detected_phases: Any = 1,
    low_power_one_phase_required_for_wb: bool = False,
    physical_budget: Optional[Dict[str, Any]] = None,
    authorized_target_amp: Any = 0,
    min_amp: Any = 6,
    native_stop_edge_due: bool = False,
) -> Dict[str, Any]:
    """Wendet nachgelagerte Start-/Stopp-Policy-Korrekturen ohne Hardwarezugriff an."""

    decision = dict(start_stop_decision) if isinstance(start_stop_decision, dict) else {}
    action = str(decision.get("action", "NOOP") or "NOOP")
    effective_action = action
    decision_changed = False
    minimum = max(1.0, _safe_float(min_amp, 6.0))
    authorized = max(0.0, _safe_float(authorized_target_amp, 0.0))
    floor_min_reached = bool(
        _safe_int(current_amp, 0) <= 6
        and _safe_int(current_set_amp, 0) <= 6
        and max(1, valid_phase_count(detected_phases, 1)) <= 1
    )

    floor_battery_guard = bool(
        floor_pv_only_guard_for_wb
        and controlled_floor_battery_guard_active
    )
    if floor_battery_guard and effective_action == "HOLD_NATIVE_RUNNING_CHARGE":
        effective_action = "HOLD_NATIVE_CURRENT_DOWN"

    if effective_action == "HOLD_NATIVE_CURRENT_DOWN":
        if authorized < minimum:
            effective_action = "STOP"
            decision.update({
                "action": "STOP",
                "target_amp": 0.0,
                "hold_amp": 0.0,
                "need_stop_toggle": bool(native_stop_edge_due),
                "native_current_down_hold": False,
                "native_running_charge_hold": False,
                "reason": (
                    "wbminsoc_floor_zero_authority_stop"
                    if floor_battery_guard
                    else "native_current_down_zero_authority_stop"
                ),
            })
            decision_changed = True
        else:
            hold_amp = min(minimum, authorized)
            decision.update({
                "action": "HOLD_NATIVE_CURRENT_DOWN",
                "target_amp": float(hold_amp),
                "hold_amp": float(hold_amp),
                "need_stop_toggle": False,
                "native_current_down_hold": True,
                "native_running_charge_hold": False,
                "reason": "native_current_down_hold",
            })
            decision_changed = True

    physical = physical_budget if isinstance(physical_budget, dict) else {}
    openwb_zero_budget_hold = bool(decision.get("openwb_zero_budget_hold", False))
    low_power_one_phase_stop = bool(
        effective_action == "HOLD_OPENWB_ZERO"
        and low_power_one_phase_required_for_wb
        and not bool(physical.get("one_phase_ready", False))
        and not bool(decision.get("openwb_phase_transition_grace_active", False))
    )
    if low_power_one_phase_stop:
        effective_action = "STOP"
        openwb_zero_budget_hold = False
        decision["action"] = "STOP"
        decision["reason"] = "low_power_requires_1p"
        decision_changed = True

    if effective_action == "STOP":
        decision["stop_authority"] = final_stop_authority_contract()
    else:
        # Eine von einem früheren Kandidaten mitgebrachte Autorität darf nie
        # eine nachgelagert finalisierte Halte-/Startentscheidung überleben.
        decision.pop("stop_authority", None)

    return {
        "action": effective_action,
        "decision": decision,
        "action_changed": bool(effective_action != action),
        "decision_changed": bool(decision_changed),
        "openwb_zero_budget_hold": bool(openwb_zero_budget_hold),
        "floor_pv_only_guard_active": bool(floor_pv_only_guard_for_wb and controlled_floor_battery_guard_active),
        "floor_pv_only_min_reached": bool(floor_min_reached),
        "authorized_target_amp": float(authorized),
        "low_power_one_phase_stop": bool(low_power_one_phase_stop),
    }


def ordinary_grid_import_sequence_required(
    *,
    grid_power_w: Any,
    threshold_w: Any,
    grid_import_down_active: bool,
    charger_connected: bool,
    charging_running: bool,
    control_mode: Any,
    public_mode: Any,
    safety_blocked: bool,
    priority_forced_stop: bool,
    budget_timeout: bool,
    grid_allowed: bool,
    price_optimizing_active: bool,
    price_boost_active: bool,
    predump_active: bool,
    scheduled_slot_active: bool,
) -> bool:
    """Bindet gewöhnlichen Netzbezug an die Mindeststrom-/Wh-Sequenz.

    Die Funktion gibt keine Hardwareaktion frei. Sie verhindert lediglich,
    dass ältere Phasen- oder PV-Hybrid-Pfade den zentralen Ablauf zwischen zwei
    Fast-Grid-Takten überholen. Explizite Netzlade- und Schutzmodi bleiben
    außerhalb dieses Vertrags. Pre-Dump ist bewusst keine Ausnahme: realer
    Netzbezug wird immer zuerst über den gemeinsamen Netz-Wh-Wächter abgebaut.
    """

    return bool(
        (
            _safe_float(grid_power_w, 0.0)
            > max(0.0, _safe_float(threshold_w, 0.0))
            or grid_import_down_active
        )
        and charger_connected
        and charging_running
        and _safe_int(control_mode, 0) > 0
        and _safe_int(public_mode, 0) > 0
        and not safety_blocked
        and not priority_forced_stop
        and not budget_timeout
        and not (
            grid_allowed
            or price_optimizing_active
            or price_boost_active
            or scheduled_slot_active
        )
    )


def curve_direct_shared_minimum_hold_required(
    *,
    charging_running: bool,
    current_amp: Any,
    direct_target_amp: Any,
    min_amp: Any,
    explicit_stop_or_safety_blocked: bool,
) -> bool:
    """Übergibt einen Direct-0A-Rand an die gemeinsame Halte-/Stopppolicy.

    Der Direct-Regler bleibt für physisch ladbare Sollströme zuständig. Fällt
    sein finales, bereits allokiertes Ziel bei laufender Ladung unter den
    Mindeststrom, darf er daraus jedoch keinen eigenen Stop ableiten. Ohne
    harte oder explizite Stopkante entscheiden dann Fast-Min beziehungsweise
    das PV-Hybrid-Energiegate über Halten, Phasenwechsel oder Stop.
    """

    minimum = max(6.0, _safe_float(min_amp, 6.0))
    # Die bestätigte Charge-Truth ist hier maßgeblich. Eine fehlende oder
    # unterminimierte Stromtelemetrie darf keinen eigenen Direct-0A-Ausgang
    # wieder freigeben; sie wird erst im gemeinsamen Folgepfad bewertet.
    return bool(
        charging_running
        and not explicit_stop_or_safety_blocked
        and _safe_float(direct_target_amp, 0.0) < minimum
    )


def openwb_mode9_pv_phase_down_required(
    *,
    openwb_phase_capable: bool,
    current_phases: Any,
    pv_power_w: Any,
    grid_power_w: Any,
    ordinary_grid_import_sequence_active: bool,
) -> bool:
    """Schützt den alten Modus-9-Phasenpfad vor einer zweiten Netzregelung."""

    return bool(
        openwb_phase_capable
        and _safe_int(current_phases, 0) != 1
        and not ordinary_grid_import_sequence_active
        and (
            _safe_float(pv_power_w, 0.0) < 4500.0
            or _safe_float(grid_power_w, 0.0) > 200.0
        )
    )


def phase_3p_budget_support_contract(
    *,
    phase_budget_w: float,
    authorized_budget_w: float,
    phase_3p_min_w: float,
    phase_up_buffer_w: float,
    grid_power_w: float,
    phase_up_grid_allow_w: float,
    ordinary_override_allowed: bool,
    openwb_pro: bool,
    phase_target: int,
    strong_export_threshold_w: float,
    fresh_budget_authorized: bool,
    strong_export_safety_clear: bool,
) -> Dict[str, Any]:
    """Trennt das physische 3p-Minimum vom 1→3-Hochwechselpuffer.

    Der zusätzliche Up-Puffer bleibt dem gewöhnlichen 1→3-Wechsel
    vorbehalten. Ein noch ungesetztes oder bereits bestätigtes 3p-Ziel darf
    bei starkem Export schon mit dem frischen, autorisierten physischen
    Mindestbudget als tragfähig gelten. Safety- und Freshness-Gates werden
    dabei nicht ersetzt, sondern als explizite Voraussetzung gebunden.
    """

    budget_w = max(0.0, _safe_float(phase_budget_w, 0.0))
    authorized_w = max(0.0, _safe_float(authorized_budget_w, 0.0))
    minimum_w = max(0.0, _safe_float(phase_3p_min_w, 0.0))
    buffer_w = max(0.0, _safe_float(phase_up_buffer_w, 0.0))
    grid_w = _safe_float(grid_power_w, 0.0)
    up_grid_allow_w = _safe_float(phase_up_grid_allow_w, 0.0)
    export_threshold_w = max(
        0.0,
        _safe_float(strong_export_threshold_w, 0.0),
    )
    target = valid_phase_count(phase_target, 0)

    ordinary_up_supported = bool(
        budget_w >= minimum_w + buffer_w
        and (grid_w < up_grid_allow_w or ordinary_override_allowed)
    )
    strong_export_supported = bool(
        openwb_pro
        and target in (0, 3)
        and budget_w >= minimum_w
        and authorized_w >= minimum_w
        and grid_w <= -export_threshold_w
        and fresh_budget_authorized
        and strong_export_safety_clear
    )
    return {
        "contract": "phase_3p_budget_support_v1",
        "supported": bool(
            ordinary_up_supported or strong_export_supported
        ),
        "ordinary_up_supported": ordinary_up_supported,
        "strong_export_supported": strong_export_supported,
        "phase_budget_w": budget_w,
        "authorized_budget_w": authorized_w,
        "minimum_3p_w": minimum_w,
        "up_buffer_w": buffer_w,
        "strong_export_threshold_w": export_threshold_w,
        "phase_target": target,
    }


def phase_up_counterfactual_contract(
    *,
    authorized_budget_w: Any,
    current_1p_amp: Any,
    effective_1p_max_amp: Any,
    current_step_amp: Any = 1.0,
    voltage_v: Any = 230.0,
    min_current_amp: Any = 6.0,
    measurement_noise_w: Any = 100.0,
    fresh_budget_authorized: bool = False,
    driver_phase_switch_allowed: bool = False,
    vehicle_phase_switch_allowed: bool = True,
    phase_change_blocked: bool = False,
    current_phases: Any = 1,
) -> Dict[str, Any]:
    """Bewertet 1p→3p gegen die Leistung nach dem Schaltvorgang.

    Gemessener Export allein ist keine Freigabe: Nach 3p→1p entsteht durch
    den eigenen Schaltvorgang ein Scheinüberschuss. Der Hochwechsel verwendet
    deshalb das frische autorisierte Budget und verlangt zusätzlich, dass der
    einphasige Pfad seine belegte wirksame Grenze erreicht hat.
    """

    budget_w = max(0.0, _safe_float(authorized_budget_w, 0.0))
    voltage = max(200.0, min(260.0, _safe_float(voltage_v, 230.0)))
    minimum_a = max(6.0, _safe_float(min_current_amp, 6.0))
    step_a = max(0.1, min(16.0, _safe_float(current_step_amp, 1.0)))
    one_phase_max_a = max(minimum_a, _safe_float(effective_1p_max_amp, 0.0))
    one_phase_current_a = max(0.0, _safe_float(current_1p_amp, 0.0))
    phases = valid_phase_count(current_phases, 0)
    noise_w = max(0.0, _safe_float(measurement_noise_w, 0.0))

    one_phase_min_w = minimum_a * voltage
    one_phase_max_w = one_phase_max_a * voltage
    three_phase_min_w = 3.0 * minimum_a * voltage
    current_quantum_w = step_a * voltage
    # Die Ein-/Aus-Hysterese muss größer als die kleinste physische
    # einphasige Ladeleistung sein. Ein zusätzliches Stromquantum macht die
    # strikte Ungleichung auch bei gerundetem 230-V-Modell eindeutig.
    hysteresis_w = max(
        one_phase_min_w + current_quantum_w,
        2.0 * noise_w + current_quantum_w,
    )
    required_budget_w = max(three_phase_min_w, one_phase_max_w) + hysteresis_w
    one_phase_saturated = bool(
        phases == 1
        and one_phase_current_a + step_a >= one_phase_max_a - 1e-9
    )

    blockers = []
    if phases != 1:
        blockers.append("current_phase_not_one")
    if not one_phase_saturated:
        blockers.append("one_phase_not_saturated")
    if not fresh_budget_authorized:
        blockers.append("authorized_budget_not_fresh")
    if budget_w + 1e-9 < required_budget_w:
        blockers.append("counterfactual_budget_insufficient")
    if not driver_phase_switch_allowed:
        blockers.append("driver_phase_switch_not_allowed")
    if not vehicle_phase_switch_allowed:
        blockers.append("vehicle_phase_switch_veto")
    if phase_change_blocked:
        blockers.append("phase_change_cooldown")

    return {
        "contract": "phase_up_counterfactual_v1",
        "supported": not blockers,
        "blockers": blockers,
        "authorized_budget_w": round(budget_w, 3),
        "required_budget_w": round(required_budget_w, 3),
        "one_phase_saturated": one_phase_saturated,
        "one_phase_current_a": round(one_phase_current_a, 3),
        "effective_1p_max_amp": round(one_phase_max_a, 3),
        "one_phase_max_w": round(one_phase_max_w, 3),
        "three_phase_min_w": round(three_phase_min_w, 3),
        "hysteresis_w": round(hysteresis_w, 3),
        "current_quantum_w": round(current_quantum_w, 3),
        "measurement_noise_w": round(noise_w, 3),
        "phase_change_blocked": bool(phase_change_blocked),
    }


def phase_switch_recommendation(
    *,
    openwb_phase_capable: bool,
    charger_connected: bool,
    control_mode: int,
    effective_wb_mode: int,
    hw_charging: bool,
    cap_amp: int,
    openwb_pro: bool,
    vehicle_1p_only: bool,
    vehicle_phase_unknown: bool,
    phase_target: int,
    phase_switch_phases: int,
    phase_cap_phases: int,
    phase_configured_3p: bool,
    phase_3p_supported: bool,
    phase_3p_keep_supported: bool,
    phase_3p_pending_hold_active: bool,
    phase_start_1p_possible: bool,
    phase_1p_start_hold_active: bool,
    phase_forecast_hold_for_wb: bool,
    phase_block_active: bool,
    last_phase_switch_age_s: float,
    phase_effective_hold_s: float,
    phase_down_since_age_s: float,
    phase_down_delay_s: float,
    phase_down_fast_delay_s: float,
    phase_down_forecast_hold_s: float,
    phase_up_since_age_s: float,
    phase_up_forecast_hold_s: float,
    predump_wallbox_active: bool,
    local_price_optimizing_active: bool,
    local_grid_allowed: bool,
    wbminsoc_gate_open: bool,
    grid_power_w: float,
    phase_down_grid_w: float,
    one_phase_confirmed: bool,
    phase_pending_age_s: float,
    phase_confirm_timeout_s: float,
    prefer_current_first_before_phase_up: bool = False,
    phase_up_min_runtime_s: float = 0.0,
    ordinary_grid_import_sequence_active: bool = False,
) -> Dict[str, Any]:
    """Empfiehlt eine Phasenaktion, ohne einen Wallbox-Befehl zu senden."""

    if not openwb_phase_capable:
        return {
            "action": "KEEP_PHASES",
            "target_phases": 0,
            "reason": "phase_switch_not_capable",
            "wait_s": 0,
            "remaining_s": 0,
        }

    mode = int(round(_safe_float(control_mode, 0)))
    public_mode = int(round(_safe_float(effective_wb_mode, 0)))
    target = valid_phase_count(phase_target, 0)
    switch_phases = valid_phase_count(phase_switch_phases, 0)
    cap_phases = valid_phase_count(phase_cap_phases, 0)
    cap = max(0, int(round(_safe_float(cap_amp, 0))))
    grid_w = _safe_float(grid_power_w, 0.0)
    hold_s = max(0.0, _safe_float(phase_effective_hold_s, 0.0))
    last_age = max(0.0, _safe_float(last_phase_switch_age_s, 999999.0))
    hold_elapsed = bool(last_age >= hold_s)

    def wait(action: str, target_phases: int, reason: str, wait_s: float, age_s: float) -> Dict[str, Any]:
        wait_val = max(0.0, _safe_float(wait_s, 0.0))
        age_val = max(0.0, _safe_float(age_s, 0.0))
        return {
            "action": action,
            "target_phases": int(target_phases),
            "reason": reason,
            "wait_s": int(round(wait_val)),
            "remaining_s": int(round(max(0.0, wait_val - age_val))),
        }

    if (
        prefer_current_first_before_phase_up
        and openwb_pro
        and charger_connected
        and mode > 0
        and public_mode > 0
        and not hw_charging
        and cap >= 6
        and (
            (target == 1 and switch_phases in (0, 1))
            or (
                target == 0
                and switch_phases == 0
                and cap_phases >= 3
            )
        )
    ):
        return {
            "action": "KEEP_PHASES",
            "target_phases": 0,
            "reason": "openwb_pro_current_first_before_phase_up",
            "wait_s": 0,
            "remaining_s": 0,
        }

    if not hold_elapsed:
        openwb_pro_running_on_current_target_allowed = bool(
            openwb_pro
            and charger_connected
            and mode > 0
            and public_mode > 0
            and hw_charging
            and target in (1, 3)
            and switch_phases == target
        )
        if openwb_pro_running_on_current_target_allowed:
            return {
                "action": "KEEP_PHASES",
                "target_phases": 0,
                "reason": "phase_change_cooldown_current_allowed",
                "wait_s": int(round(hold_s)),
                "remaining_s": int(round(max(0.0, hold_s - last_age))),
            }
        openwb_pro_start_on_current_target_allowed = bool(
            openwb_pro
            and charger_connected
            and mode > 0
            and public_mode > 0
            and not hw_charging
            and cap > 0
            and target in (1, 3)
            and switch_phases == target
        )
        if openwb_pro_start_on_current_target_allowed:
            return {
                "action": "KEEP_PHASES",
                "target_phases": 0,
                "reason": "phase_change_hold_start_allowed",
                "wait_s": int(round(hold_s)),
                "remaining_s": int(round(max(0.0, hold_s - last_age))),
            }
        return wait("WAIT", 0, "phase_change_hold", hold_s, last_age)

    # Die persistierte openWB-Pro-Hardwarefrist sperrt jede neue Phasenkante
    # symmetrisch. Sie darf den Strom am bereits bestätigten Phasenziel nicht
    # sperren; deshalb bleibt die Phasenentscheidung neutral und die
    # nachgelagerte Stromentscheidung kann weiter START/SET_CURRENT liefern.
    if phase_block_active:
        return {
            "action": "KEEP_PHASES",
            "target_phases": 0,
            "reason": "phase_change_deadline_current_allowed",
            "wait_s": 0,
            "remaining_s": 0,
        }

    if vehicle_1p_only and target == 3:
        return wait("SWITCH_1P", 1, "vehicle_1p_only", 0, 0)

    start_1p_needed = bool(
        switch_phases != 3
        and (
            phase_start_1p_possible
            or (
                charger_connected
                and not hw_charging
                and public_mode > 0
                and cap > 0
                and cap_phases == 1
                and phase_configured_3p
                and target != 1
                and not phase_forecast_hold_for_wb
                and not phase_3p_supported
                and not phase_3p_pending_hold_active
            )
        )
    )
    if start_1p_needed:
        return wait("SWITCH_1P", 1, "start_1p", 0, 0)

    budget_phase_down_needed = bool(
        not ordinary_grid_import_sequence_active
        and (
            cap == 0
            or (openwb_pro and cap <= 6)
            or grid_w > _safe_float(phase_down_grid_w, 0.0)
        )
    )
    phase_down_needed = bool(
        mode > 0
        and not local_price_optimizing_active
        and not local_grid_allowed
        and phase_configured_3p
        and switch_phases >= 3
        and not phase_3p_keep_supported
        and (budget_phase_down_needed or not wbminsoc_gate_open)
    )
    if phase_down_needed:
        down_reason = "effective_cap"
        if (
            not ordinary_grid_import_sequence_active
            and grid_w > _safe_float(phase_down_grid_w, 0.0)
        ):
            down_reason = "grid_import"
        elif not ordinary_grid_import_sequence_active and cap == 0:
            down_reason = "no_3p_budget"
        elif (
            not ordinary_grid_import_sequence_active
            and openwb_pro
            and cap <= 6
        ):
            down_reason = "3p_minimum"
        elif not wbminsoc_gate_open:
            down_reason = "wbminsoc_floor"

        down_wait_s = max(0.0, _safe_float(phase_down_delay_s, 0.0))
        if (
            phase_forecast_hold_for_wb
            and grid_w < max(
                _safe_float(phase_down_grid_w, 0.0) * 1.5,
                _safe_float(phase_down_grid_w, 0.0) + 1200.0,
            )
        ):
            down_wait_s = max(down_wait_s, _safe_float(phase_down_forecast_hold_s, down_wait_s))
        elif openwb_pro and (
            cap == 0
            or cap <= 6
            or grid_w > _safe_float(phase_down_grid_w, 0.0)
            or not wbminsoc_gate_open
        ):
            down_wait_s = _safe_float(phase_down_fast_delay_s, down_wait_s)
        down_age_s = max(0.0, _safe_float(phase_down_since_age_s, 0.0))
        return wait(
            "SWITCH_1P" if down_age_s >= down_wait_s else "WAIT_1P",
            1,
            down_reason,
            down_wait_s,
            down_age_s,
        )

    phase_up_possible = bool(
        mode > 0
        and not ordinary_grid_import_sequence_active
        and cap > 0
        and phase_3p_supported
        and not vehicle_1p_only
        and not phase_1p_start_hold_active
        and switch_phases in (0, 1)
        and target != 3
        and not phase_block_active
    )
    if phase_up_possible:
        up_wait_s = (
            max(0.0, _safe_float(phase_up_min_runtime_s, 0.0))
            if openwb_pro
            else (0.0 if predump_wallbox_active else 45.0)
        )
        if (
            phase_forecast_hold_for_wb
            and not phase_configured_3p
            and not (openwb_pro and phase_3p_supported)
        ):
            up_wait_s = max(up_wait_s, _safe_float(phase_up_forecast_hold_s, up_wait_s))
        up_age_s = max(0.0, _safe_float(phase_up_since_age_s, 0.0))
        return wait(
            "SWITCH_3P" if up_age_s >= up_wait_s else "WAIT_3P",
            3,
            "phase_up",
            up_wait_s,
            up_age_s,
        )

    if target == 3 and switch_phases in (0, 1):
        if (
            one_phase_confirmed
            and vehicle_phase_unknown
            and _safe_float(phase_pending_age_s, 0.0) >= _safe_float(phase_confirm_timeout_s, 0.0)
        ):
            return wait("SWITCH_1P", 1, "unknown_vehicle_1p", 0, 0)
        return wait(
            "WAIT_CONFIRM_3P",
            3,
            "pending_3p_confirmation",
            _safe_float(phase_confirm_timeout_s, 0.0),
            _safe_float(phase_pending_age_s, 0.0),
        )

    return {
        "action": "KEEP_PHASES",
        "target_phases": 0,
        "reason": (
            "minimum_current_import_sequence"
            if ordinary_grid_import_sequence_active
            else "stable"
        ),
        "wait_s": 0,
        "remaining_s": 0,
    }


def phase_start_1p_dispatch_required(
    *,
    action: Any,
    reason: Any,
    effective_current_amp: Any,
) -> bool:
    """Übersetzt die bereits geprüfte 1p-Policy ohne zweite Fachprüfung."""

    return bool(
        str(action or "") == "SWITCH_1P"
        and str(reason or "") in (
            "start_1p",
            "openwb_pro_cold_start_1p",
        )
        and _safe_float(effective_current_amp, 0.0) >= 6.0
    )


def minimum_current_import_action(
    *,
    current_amp: Any,
    min_amp: Any = 6,
    grid_power_w: Any = 0.0,
    import_status: Optional[Dict[str, Any]] = None,
    stop_wh: Any = 40.0,
    openwb_like_charger: bool = False,
    phase_switch_supported: bool = False,
    phase_count: Any = 1,
    phase_target: Any = 0,
    phase_actual: Any = 0,
    phase_forecast_hold_active: bool = False,
    phase_down_reup_block_s: Any = 180.0,
    phase_down_forecast_hold_s: Any = 600.0,
) -> Dict[str, Any]:
    """Wählt bei Netzbezug und Mindeststrom HOLD, 3p->1p oder STOP.

    Das Laufzeitmodul führt das Energieintegral. Diese Hilfe interpretiert
    dessen Zustand nur zusammen mit der aktuellen Phasensituation, damit die
    Stopp-/Halte-Policy ohne Treibernebenwirkungen testbar bleibt.
    """

    status = import_status if isinstance(import_status, dict) else {}
    minimum = max(6.0, _safe_float(min_amp, 6.0))
    current = max(0.0, _safe_float(current_amp, minimum))
    hold_amp = max(minimum, current or minimum)
    grid_w = _safe_float(grid_power_w, 0.0)
    wh = max(0.0, _safe_float(status.get("wh", 0.0), 0.0))
    stable_s = max(0.0, _safe_float(status.get("stable_s", 0.0), 0.0))
    stop_limit_wh = max(0.0, _safe_float(stop_wh, 40.0))
    should_stop = bool(status.get("stop", False))
    phases = max(
        1,
        valid_phase_count(phase_count, 1) or 1,
        valid_phase_count(phase_target, 0),
        valid_phase_count(phase_actual, 0),
    )
    phase_down_possible = bool(
        should_stop
        and openwb_like_charger
        and phase_switch_supported
        and phases >= 3
    )
    if not should_stop:
        forecast_hold = bool(openwb_like_charger and phase_forecast_hold_active and phases >= 3)
        return {
            "action": "HOLD_MIN_CURRENT_IMPORT",
            "target_amp": float(hold_amp),
            "target_phases": 0,
            "grid_power_w": int(round(grid_w)),
            "import_wh": float(wh),
            "stop_wh": float(stop_limit_wh),
            "stable_s": float(stable_s),
            "phase_count": int(phases),
            "forecast_hold": bool(forecast_hold),
            "log_key": "forecast_phase_grid_hold" if forecast_hold else "min_current_import_integral_hold",
            "fast_block_s": 10.0,
            "reset_integral": False,
            "reason": "integral_not_full",
        }
    if phase_down_possible:
        reup_block_s = max(
            300.0,
            _safe_float(
                phase_down_forecast_hold_s if phase_forecast_hold_active else phase_down_reup_block_s,
                600.0 if phase_forecast_hold_active else 180.0,
            ),
        )
        return {
            "action": "SWITCH_1P_MIN_CURRENT_IMPORT",
            "target_amp": int(minimum),
            "target_phases": 1,
            "grid_power_w": int(round(grid_w)),
            "import_wh": float(wh),
            "stop_wh": float(stop_limit_wh),
            "stable_s": float(stable_s),
            "phase_count": int(phases),
            "forecast_hold": bool(phase_forecast_hold_active),
            "reup_block_s": float(reup_block_s),
            "fast_block_s": 60.0,
            "reset_integral": True,
            "reason": "phase_down_before_stop",
        }
    return {
        "action": "STOP_MIN_CURRENT_IMPORT",
        "target_amp": 0,
        "target_phases": 0,
        "grid_power_w": int(round(grid_w)),
        "import_wh": float(wh),
        "stop_wh": float(stop_limit_wh),
        "stable_s": float(stable_s),
        "phase_count": int(phases),
        "forecast_hold": False,
        "fast_block_s": 60.0,
        "reset_integral": True,
        "reason": "integral_full_stop",
    }


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(_safe_float(value, default)))
    except Exception:
        return int(default)


def fast_grid_current_reduction_action(
    *,
    current_amp: Any,
    cap_amp: Any = 0,
    max_amp: Any = 32,
    grid_power_w: Any = 0.0,
    phase_count: Any = 1,
    current_step_amp: Any = 1.0,
    physical_amp_clamp: bool = False,
    openwb_like_charger: bool = False,
    direct_current_capable: bool = False,
    sonnenmodus_capable: bool = False,
    set_amp_and_state_capable: bool = False,
    public_mode: Any = 0,
    min_amp: Any = 6,
    grid_margin_w: Any = 120.0,
    stable_after_fast_hold_s: Any = 60.0,
) -> Dict[str, Any]:
    """Wählt reduzierten Strom und Treibermethode für einen schnellen Netzwächter.

    Treiberausführung und Laufzeitstempel bleiben beim Aufrufer. So wird die
    Kernentscheidung über Wallbox-Typen hinweg geteilt, während der Manager die
    Grenze für Nebenwirkungen bleibt.
    """

    step = _current_step(current_step_amp, 1.0)
    current = _safe_float(current_amp, 6.0)
    cap = _safe_float(cap_amp, 0.0)
    maximum = max(0.0, _safe_float(max_amp, 32.0))
    minimum = max(6.0, _safe_float(min_amp, 6.0))
    phases = max(1, valid_phase_count(phase_count, 1) or 1)
    grid_w = _safe_float(grid_power_w, 0.0)
    margin_w = _safe_float(grid_margin_w, 120.0)
    watts_per_amp = 230.0 * float(phases)
    reason = "fast_grid_physical_clamp" if physical_amp_clamp else "fast_grid_import"
    drop_amp: float

    if physical_amp_clamp:
        if openwb_like_charger and step < 0.99:
            drop_amp = max(step, (grid_w + margin_w) / watts_per_amp)
            raw_target = max(minimum, int((current - drop_amp) / step + 1e-9) * step)
        else:
            drop_amp = 1.0
            raw_target = max(minimum, min(_safe_float(cap or minimum, minimum), float(int(current or minimum) - 1)))
    elif direct_current_capable or openwb_like_charger:
        if step < 0.99:
            drop_amp = max(step, (grid_w + margin_w) / watts_per_amp)
            raw_target = max(minimum, int((current - drop_amp) / step + 1e-9) * step)
        else:
            drop_amp = float(max(1, int(math.ceil((grid_w + margin_w) / watts_per_amp))))
            raw_target = max(minimum, current - drop_amp)
    else:
        drop_amp = 1.0
        raw_target = current - 1.0

    target = min(maximum, raw_target)
    target_amp = _amp_value(target, step)
    if sonnenmodus_capable:
        method = "set_amp_sonnenmodus"
    elif direct_current_capable:
        method = "set_direct_current"
    elif set_amp_and_state_capable:
        method = "set_amp_and_state"
    else:
        method = ""

    stable_hold_s = max(30.0, _safe_float(stable_after_fast_hold_s, 60.0))
    return {
        "action": "REDUCE_CURRENT_FAST_GRID" if method else "REDUCE_CURRENT_FAST_GRID_NO_DRIVER",
        "target_amp": target_amp,
        "previous_amp": _amp_value(current, step),
        "drop_amp": _amp_value(drop_amp, step),
        "target_phases": 0,
        "phase_count": int(phases),
        "grid_power_w": int(round(grid_w)),
        "current_step_amp": _amp_value(step, step),
        "method": method,
        "force_state": None if method in ("set_amp_sonnenmodus", "set_amp_and_state") else "",
        "reason": reason,
        "fast_block_s": 25.0,
        "stable_after_fast_hold_s": float(stable_hold_s),
        "hold_up_after_fast": bool(_safe_int(public_mode, 0) != 0),
        "update_last_change": bool(openwb_like_charger),
        "mark_charge_anchor": bool(_safe_float(target_amp, 0.0) > 0.0),
    }


def _allocation_for_id(allocations: Optional[Dict[Any, Any]], wb_id: int) -> Dict[str, Any]:
    if not isinstance(allocations, dict):
        return {}
    for key in (wb_id, str(wb_id)):
        item = allocations.get(key)
        if isinstance(item, dict):
            return item
    return {}


def _mode_for_id(charge_modes: Optional[Dict[Any, Any]], wb_id: int) -> Optional[int]:
    if not isinstance(charge_modes, dict):
        return None
    for key in (wb_id, str(wb_id)):
        if key in charge_modes:
            return _safe_int(charge_modes.get(key), 0)
    return None


def _grid_allowed_for_id(grid_allowed_charger_ids: Any, wb_id: int) -> bool:
    if isinstance(grid_allowed_charger_ids, bool):
        return bool(grid_allowed_charger_ids)
    if isinstance(grid_allowed_charger_ids, dict):
        return bool(grid_allowed_charger_ids.get(wb_id) or grid_allowed_charger_ids.get(str(wb_id)))
    if isinstance(grid_allowed_charger_ids, (set, list, tuple)):
        return wb_id in grid_allowed_charger_ids or str(wb_id) in grid_allowed_charger_ids
    return False


def vehicle_phase_switch_veto_contract(
    vehicle_capability: Optional[Dict[str, Any]],
    status: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Wende die openWB-Semantik für Fahrzeug-Phasenverbote an.

    ``prevent_phase_switch`` erlaubt vor dem ersten Laden genau die notwendige
    Initialwahl. Explizite lokale ``phase_switch_allowed=false``-Felder
    bleiben dagegen absolute Vetos.
    """

    capability = (
        vehicle_capability if isinstance(vehicle_capability, dict) else {}
    )
    st = status if isinstance(status, dict) else {}
    source = str(capability.get("phase_switch_policy_source") or "")
    requested_veto = capability.get("phase_switch_allowed") is False
    session_kwh = max(0.0, _safe_float(st.get("session_kwh"), 0.0))
    session_started = bool(session_kwh > 0.0 or status_real_charging(st))
    prevent_profile = source == "vehicle_profile_prevent_phase_switch"
    active = bool(
        requested_veto
        and (
            not prevent_profile
            or session_started
        )
    )
    if not requested_veto:
        reason = "no_vehicle_phase_veto"
    elif prevent_profile and not session_started:
        reason = "initial_phase_selection_allowed_before_charge"
    elif prevent_profile:
        reason = "prevent_phase_switch_after_charge_start"
    else:
        reason = "absolute_vehicle_phase_switch_veto"
    return {
        "contract": "vehicle_phase_switch_veto_v1",
        "active": active,
        "requested_veto": requested_veto,
        "policy_source": source,
        "session_started": session_started,
        "session_kwh": session_kwh,
        "reason": reason,
    }


def _status_running_for_allocation(status: Optional[Dict[str, Any]]) -> bool:
    if not isinstance(status, dict):
        return False
    st = status
    try:
        if (
            st.get("driver_status_valid") is False
            or st.get("driver_status_stale") is True
            or st.get("driver_status_glitch") is True
            or st.get("stale") is True
        ):
            return False
        charge_contract = st.get("charge_contract")
        if (
            isinstance(charge_contract, dict)
            and charge_contract.get("counts_as_real_charge") is True
        ):
            return True
        if st.get("charge_counts_as_real") is True:
            return True
        if bool(st.get("charge_state", False) or st.get("charging", False)):
            return True
        power_w = max(
            abs(float(st.get("real_power_w", 0) or 0)),
            abs(float(st.get("phase_power_sum_w", 0) or 0)),
            abs(float(st.get("power_w", st.get("power", 0)) or 0)),
        )
        if power_w > 250.0:
            return True
    except Exception:
        return False
    return False


def multi_wallbox_allocation_contract(
    charger_statuses: Iterable[Dict[str, Any]],
    *,
    priority_mode: Any = 0,
    allocations: Optional[Dict[Any, Any]] = None,
    charge_modes: Optional[Dict[Any, Any]] = None,
    grid_allowed_charger_ids: Any = None,
    min_amp: Any = 6,
) -> Dict[str, Any]:
    """Beschreibt den gemeinsamen Zuteilungsvertrag mehrerer Wallboxen.

    Der untergeordnete Zuteiler darf weiterhin Stromziele berechnen. Dieser
    Vertrag benennt die wallboxübergreifenden Entscheidungen: ob das
    konfigurierte Prioritätsziel tatsächlich nutzbar ist, welche nachrangige
    Wallbox einen bestehenden Ladevorgang nur halten darf und warum eine
    schlafende nachrangige Wallbox warten muss.
    """

    try:
        priority_id = _safe_int(priority_mode, 0)
    except Exception:
        priority_id = 0
    if priority_id not in (1, 2):
        priority_id = 0

    slots: Dict[int, Dict[str, Any]] = {}
    for entry in charger_statuses or []:
        if not isinstance(entry, dict):
            continue
        wb_id = _safe_int(entry.get("id", entry.get("wb_id", 0)), 0)
        if wb_id <= 0:
            continue
        status = entry.get("status") if isinstance(entry.get("status"), dict) else entry
        mode_value = _mode_for_id(charge_modes, wb_id)
        active_mode = bool(mode_value != 0) if mode_value is not None else True
        connected = status_connected(status)
        running = bool(connected and _status_running_for_allocation(status))
        allocation = _allocation_for_id(allocations, wb_id)
        allocated_amp = max(0, _safe_int(allocation.get("target_amp", 0), 0))
        allocated_state = _safe_int(allocation.get("state", 1), 1)
        grid_allowed = _grid_allowed_for_id(grid_allowed_charger_ids, wb_id)
        slots[wb_id] = {
            "id": wb_id,
            "connected": bool(connected),
            "running": bool(running),
            "active_mode": bool(active_mode),
            "mode": mode_value,
            "grid_allowed": bool(grid_allowed),
            "allocated_amp": int(allocated_amp),
            "allocated_state": int(allocated_state),
            "allocated": bool(allocated_state == 2 and allocated_amp >= max(1, _safe_int(min_amp, 6))),
        }

    priority_slot = slots.get(priority_id, {}) if priority_id else {}
    priority_target_connected = bool(priority_slot.get("connected", False))
    priority_target_active = bool(priority_target_connected and priority_slot.get("active_mode", True))
    priority_target_running = bool(priority_slot.get("running", False))

    for wb_id, slot in slots.items():
        is_priority = bool(priority_id and wb_id == priority_id)
        priority_waiting = bool(
            priority_target_active
            and not is_priority
            and slot.get("connected", False)
            and slot.get("active_mode", True)
        )
        must_wait = bool(
            priority_waiting
            and not slot.get("running", False)
            and not slot.get("grid_allowed", False)
            and not slot.get("allocated", False)
        )
        slot["priority_active"] = bool(is_priority and priority_target_active)
        slot["priority_waiting"] = bool(priority_waiting)
        slot["must_wait_for_priority"] = bool(must_wait)
        slot["may_hold_secondary"] = bool(priority_waiting and slot.get("running", False))
        slot["effective_amp"] = 0 if must_wait else int(slot.get("allocated_amp", 0) or 0)
        if slot["priority_active"]:
            reason = "priority_target"
        elif must_wait:
            reason = "wait_for_priority_target"
        elif priority_waiting and slot.get("running", False):
            reason = "secondary_running_hold"
        elif not slot.get("connected", False):
            reason = "no_vehicle"
        elif not slot.get("active_mode", True):
            reason = "mode_off"
        elif slot.get("allocated", False):
            reason = "allocated"
        else:
            reason = "not_allocated"
        slot["reason"] = reason

    return {
        "mode": int(priority_id),
        "priority_target_id": int(priority_id),
        "priority_target_connected": bool(priority_target_connected),
        "priority_target_active": bool(priority_target_active),
        "priority_target_running": bool(priority_target_running),
        "slots": slots,
    }


def _decision_map(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def build_wallbox_decision_payload(
    *,
    wb_id: int,
    public_mode: int,
    control_mode: int,
    current_decision: Optional[Dict[str, Any]] = None,
    start_stop_decision: Optional[Dict[str, Any]] = None,
    phase_recommendation: Optional[Dict[str, Any]] = None,
    allowed_w: float = 0.0,
    detected_phases: int = 1,
    current_amp: float = 0.0,
    current_set_amp: float = 0.0,
    cap_amp: float = 0.0,
    max_amp: int = 0,
    charger_connected: bool = False,
    hw_charging: bool = False,
    hw_power_w: float = 0.0,
    grid_power_w: float = 0.0,
    mode_label: str = "",
    storage_state: str = "",
    driver_class_name: str = "",
    openwb_like_charger: bool = False,
    openwb_pro: bool = False,
    e3dc_native_toggle: bool = False,
    observe_only: bool = False,
    priority_forced_stop: bool = False,
    budget_timeout: bool = False,
) -> Dict[str, Any]:
    """Erzeugt die kanonische Entscheidungslast einer Wallbox für einen Zyklus."""

    current = _decision_map(current_decision)
    start_stop = _decision_map(start_stop_decision)
    phase = _decision_map(phase_recommendation)
    if priority_forced_stop:
        # Auch ein vorfinales Diagnose-/Phasen-Payload darf aus einem alten
        # positiven Strom- oder Phasenwert keinen Hardwarewunsch erzeugen.
        # Der Manager materialisiert die eigentliche typisierte Stopkante;
        # ein bereits finales STOP-Payload wird hier unverändert erhalten.
        current = dict(current)
        current.update({
            "target_amp": 0.0,
            "physically_chargeable": False,
            "limiting_reason": "priority_forced_stop",
        })
        start_stop = dict(start_stop)
        if str(start_stop.get("action", "NOOP") or "NOOP") != "STOP":
            start_stop.update({
                "action": "NOOP",
                "target_amp": 0.0,
                "hold_amp": 0.0,
                "is_new_start": False,
                "reason": "priority_forced_stop",
            })
            start_stop.pop("stop_authority", None)
        phase = {
            "action": "KEEP_PHASES",
            "target_phases": 0,
            "wait_s": 0,
            "remaining_s": 0,
            "reason": "priority_forced_stop",
        }
    start_action = str(start_stop.get("action", "NOOP") or "NOOP")
    phase_action = str(phase.get("action", "KEEP_PHASES") or "KEEP_PHASES")
    current_reason = str(current.get("limiting_reason", "") or "")
    start_reason = str(start_stop.get("reason", start_action.lower()) or "")
    stop_authority = _decision_map(start_stop.get("stop_authority"))
    phase_reason = str(phase.get("reason", "stable") or "stable")
    target_amp = max(0.0, _safe_float(start_stop.get("target_amp", current.get("target_amp", cap_amp)), 0.0))
    hold_amp = max(0.0, _safe_float(start_stop.get("hold_amp", 0), 0.0))
    phase_target = valid_phase_count(phase.get("target_phases", 0), 0)

    return {
        "schema_version": "wallbox_decision_payload_v1",
        "wb_id": _safe_int(wb_id, 0),
        "mode": {
            "public": _safe_int(public_mode, 0),
            "control": _safe_int(control_mode, 0),
            "label": str(mode_label or ""),
        },
        "driver": {
            "class": str(driver_class_name or ""),
            "openwb_like": bool(openwb_like_charger),
            "openwb_pro": bool(openwb_pro),
            "e3dc_native_toggle": bool(e3dc_native_toggle),
            "observe_only": bool(observe_only),
        },
        "decisions": {
            "current": {
                "target_amp": max(0.0, _safe_float(current.get("target_amp", cap_amp), 0.0)),
                "raw_amp": max(0.0, _safe_float(current.get("raw_amp", current.get("target_amp", cap_amp)), 0.0)),
                "physically_chargeable": bool(current.get("physically_chargeable", False)),
                "house_fuse_limited": bool(current.get("house_fuse_limited", False)),
                "reason": current_reason,
            },
            "start_stop": {
                "action": start_action,
                "target_amp": target_amp,
                "hold_amp": hold_amp,
                "is_new_start": bool(start_stop.get("is_new_start", False)),
                "reason": start_reason,
                "stop_authority": dict(stop_authority),
            },
            "phase": {
                "action": phase_action,
                "target_phases": phase_target,
                "wait_s": max(0, _safe_int(phase.get("wait_s", 0), 0)),
                "remaining_s": max(0, _safe_int(phase.get("remaining_s", 0), 0)),
                "reason": phase_reason,
            },
        },
        "inputs": {
            "allowed_w": int(round(_safe_float(allowed_w, 0.0))),
            "detected_phases": valid_phase_count(detected_phases, 1),
            "current_amp": max(0.0, _safe_float(current_amp, 0.0)),
            "current_set_amp": max(0.0, _safe_float(current_set_amp, 0.0)),
            "cap_amp": max(0.0, _safe_float(cap_amp, 0.0)),
            "max_amp": max(0, _safe_int(max_amp, 0)),
            "charger_connected": bool(charger_connected),
            "hw_charging": bool(hw_charging),
            "hw_power_w": int(round(_safe_float(hw_power_w, 0.0))),
            "grid_power_w": int(round(_safe_float(grid_power_w, 0.0))),
            "storage_state": str(storage_state or ""),
            "priority_forced_stop": bool(priority_forced_stop),
            "budget_timeout": bool(budget_timeout),
        },
        "reasons": {
            "current": current_reason,
            "start_stop": start_reason,
            "phase": phase_reason,
        },
        "command_intent": {
            "start_stop_action": start_action,
            "phase_action": phase_action,
            "target_amp": target_amp,
            "hold_amp": hold_amp,
            "target_phases": phase_target,
        },
    }


def driver_command_from_decision_payload(payload: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Bildet eine kanonische Entscheidungslast auf einen abstrakten Treiberbefehl ab."""

    data = payload if isinstance(payload, dict) else {}
    decisions = _decision_map(data.get("decisions"))
    driver = _decision_map(data.get("driver"))
    inputs = _decision_map(data.get("inputs"))
    current = _decision_map(decisions.get("current"))
    start_stop = _decision_map(decisions.get("start_stop"))
    phase = _decision_map(decisions.get("phase"))
    stop_authority = _decision_map(start_stop.get("stop_authority"))

    start_action = str(start_stop.get("action", "NOOP") or "NOOP")
    phase_action = str(phase.get("action", "KEEP_PHASES") or "KEEP_PHASES")
    phase_target = valid_phase_count(phase.get("target_phases", 0), 0)
    target_amp = max(0.0, _safe_float(start_stop.get("target_amp", inputs.get("cap_amp", 0)), 0.0))
    hold_amp = max(0.0, _safe_float(start_stop.get("hold_amp", 0), 0.0))
    current_set_amp = max(0.0, _safe_float(inputs.get("current_set_amp", 0), 0.0))
    effective_current_amp = max(0.0, _safe_float(current.get("target_amp", 0), 0.0))
    hold_target_amp = current_output_hold_target_amp(
        start_action,
        hold_amp=hold_amp,
        target_amp=target_amp,
        current_amp=inputs.get("current_amp", 0),
        current_set_amp=current_set_amp,
        min_amp=6,
        max_amp=effective_current_amp,
        authorized_target_amp=effective_current_amp,
    )
    authorized_start_amp = 0.0
    if target_amp >= 6.0 and effective_current_amp >= 6.0:
        authorized_start_amp = min(target_amp, effective_current_amp)

    kind = "noop"
    amp = 0
    target_phases = 0
    reason = str(start_stop.get("reason", phase.get("reason", "")) or "")

    if bool(driver.get("observe_only", False)):
        kind = "observe_only"
        reason = reason or "observe_only"
    elif start_action == "STOP":
        kind = "stop"
        reason = reason or "stop"
    elif start_action == "SUPPRESS_NATIVE_STOP":
        kind = "noop"
        reason = reason or "suppress_native_stop"
    elif start_action == "HOLD_GRID_IMPORT_SEQUENCE":
        # Dieser Hold besitzt bewusst keinen eigenen Stromausgang. Die
        # Fast-Grid-Regelung senkt den bestätigten Strom schrittweise; dieser
        # Vertrag verhindert nur, dass ein nachgelagerter Nullbudgetpfad
        # parallel stoppt oder einen Phasenwechsel auslöst.
        kind = "noop"
        amp = 0
        reason = reason or "minimum_current_import_sequence"
    elif start_action.startswith("HOLD_") and start_action not in CURRENT_OUTPUT_HOLD_ACTIONS:
        kind = "noop"
        amp = 0
        reason = "unknown_hold_action"
    elif phase_action in ("SWITCH_1P", "SWITCH_3P") and phase_target in (1, 3):
        kind = "set_phases"
        target_phases = phase_target
        reason = str(phase.get("reason", "phase_switch") or "phase_switch")
    elif phase_action.startswith("WAIT"):
        # Eine noch laufende 30-/60-s-Umschalthysterese ist kein Ladeverbot.
        # Auf den vorhandenen Phasen folgt die Wallbox weiter der Stromkurve;
        # nur der spätere Schütz-/phasetarget-Befehl wartet.
        hw_charging = bool(inputs.get("hw_charging", False))
        if (
            hw_charging
            and start_action in ("START", "SET_CURRENT")
            and authorized_start_amp >= 6.0
        ):
            kind = "set_current"
            amp = authorized_start_amp
            reason = (
                "current_target_clamped"
                if target_amp > effective_current_amp + 1e-6
                else "phase_hysteresis_hold_current"
            )
        elif hw_charging and start_action in CURRENT_OUTPUT_HOLD_ACTIONS and hold_target_amp >= 6.0:
            kind = "hold_current"
            amp = hold_target_amp
            reason = "phase_hysteresis_hold_current"
        else:
            kind = "wait"
            target_phases = phase_target
            reason = str(phase.get("reason", "phase_wait") or "phase_wait")
    elif start_action in CURRENT_OUTPUT_HOLD_ACTIONS:
        kind = "hold_current" if hold_target_amp >= 6.0 else "hold_state"
        amp = hold_target_amp
        reason = reason or start_action.lower()
    elif start_action in ("START", "SET_CURRENT"):
        if target_amp < 6.0 or effective_current_amp < 6.0:
            amp = 0
            kind = "noop"
            reason = (
                "current_target_mismatch"
                if target_amp >= 6.0 and effective_current_amp < 6.0
                else "current_target_below_minimum"
            )
        else:
            amp = authorized_start_amp
            kind = "set_current"
            reason = (
                "current_target_clamped"
                if target_amp > effective_current_amp + 1e-6
                else (reason or start_action.lower())
            )

    command = {
        "schema_version": "wallbox_driver_command_v1",
        "kind": kind,
        "amp": float(amp),
        "target_phases": int(target_phases),
        "reason": reason,
        "source": "decision_payload",
    }
    if kind == "stop" and stop_authority:
        command["stop_authority"] = dict(stop_authority)
    return command


CONTACTOR_PROTECTION_REASON_MARKERS = (
    "emergency",
    "not_aus",
    "not-aus",
    "mode0",
    "wallbox_aus",
    "user_aus",
    "off",
    "ngna",
    "user_stop",
    "manual_stop",
    "grid",
    "netz",
    "import",
    "house_fuse",
    "sicherung",
    "budget_timeout",
    "stale",
    "predump_floor",
    "wbminsoc_floor",
    "protection",
    "schutz",
    "fault",
    "error",
    "vehicle_done",
    "charge_done",
    "unplug",
    "disconnected",
    "planned_end",
    "schedule_end",
)


def _event_text(event: Dict[str, Any], key: str, default: str = "") -> str:
    return str(event.get(key, default) or default).strip().lower()


def _event_ts(event: Dict[str, Any]) -> float:
    return _safe_float(event.get("ts", event.get("time", event.get("timestamp", 0.0))), 0.0)


def _event_wb_id(event: Dict[str, Any]) -> int:
    return _safe_int(event.get("wb_id", event.get("wallbox_id", event.get("id", 0))), 0)


def _event_target_reachable(event: Dict[str, Any]) -> bool:
    if "target_reachable" not in event:
        return True
    return str(event.get("target_reachable", "")).strip().lower() not in ("0", "false", "no", "nein")


def _event_has_protection_reason(
    event: Dict[str, Any],
    markers: Optional[Iterable[str]] = None,
) -> bool:
    reason = " ".join(
        str(event.get(key, "") or "").lower()
        for key in ("reason", "driver_reason", "protection_reason")
    )
    reason_words = re.sub(r"\s+", " ", reason).strip()
    for marker in markers or CONTACTOR_PROTECTION_REASON_MARKERS:
        marker_text = str(marker).strip().lower()
        if not marker_text:
            continue
        if " " in marker_text:
            if marker_text in reason_words:
                return True
            continue
        if re.search(rf"(?<![a-z0-9]){re.escape(marker_text)}(?![a-z0-9])", reason):
            return True
    return False


def _event_start_stop_action(event: Dict[str, Any]) -> str:
    action = _event_text(event, "action")
    if action in ("start", "stop"):
        return action

    kind = _event_text(event, "kind")
    method = _event_text(event, "method")
    amp = _safe_int(event.get("amp", event.get("target_amp", event.get("current_amp", 0))), 0)
    force_state = _safe_int(event.get("force_state", -1), -1)

    if kind == "stop" or method == "emergency_stop":
        return "stop"
    if method in (
        "set_amp_and_state",
        "set_amp_sonnenmodus",
        "set_amp_autonomous_solar",
    ) and (amp <= 0 or force_state == 1):
        return "stop"
    if method == "set_direct_current" and amp <= 0:
        return "stop"
    if kind in ("set_current", "hold_current") and amp >= 6:
        return "start"
    if method in (
        "set_amp_and_state",
        "set_amp_sonnenmodus",
        "set_amp_autonomous_solar",
        "set_direct_current",
    ) and amp >= 6:
        return "start"
    return ""


def _event_phase_action(event: Dict[str, Any]) -> str:
    action = _event_text(event, "action")
    if action in ("1p", "3p", "phase_1p", "phase_3p"):
        return action[-2:]

    kind = _event_text(event, "kind")
    method = _event_text(event, "method")
    phases = valid_phase_count(event.get("phases", event.get("target_phases", 0)), 0)
    if phases in (1, 3) and (kind == "set_phases" or method == "set_phases"):
        return f"{phases}p"
    return ""


def detect_wallbox_command_chatter(
    events: Iterable[Dict[str, Any]],
    *,
    min_start_stop_gap_s: int = 180,
    min_phase_gap_s: int = 180,
    protection_reason_markers: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Erkennt unsicheres Schütz- und Phasenbefehlsflattern in einem Befehlsstrom."""

    markers = protection_reason_markers or CONTACTOR_PROTECTION_REASON_MARKERS
    timeline = sorted(
        [event for event in events if isinstance(event, dict)],
        key=lambda event: (_event_wb_id(event), _event_ts(event)),
    )
    recent_start_stop: Dict[int, list] = {}
    recent_phases: Dict[int, list] = {}
    violations = []
    counts = {"start_stop_start": 0, "stop_start_stop": 0, "phase_flip": 0}

    def protected_or_unreachable(window: Iterable[Dict[str, Any]]) -> bool:
        return any(_event_has_protection_reason(event, markers) for event in window) or not all(
            _event_target_reachable(event) for event in window
        )

    for event in timeline:
        wb_id = _event_wb_id(event)
        ts = _event_ts(event)

        start_stop = _event_start_stop_action(event)
        if start_stop:
            history = recent_start_stop.setdefault(wb_id, [])
            history.append({"event": event, "action": start_stop, "ts": ts})
            del history[:-3]
            if len(history) == 3:
                pattern = tuple(item["action"] for item in history)
                age_s = history[-1]["ts"] - history[0]["ts"]
                window = [item["event"] for item in history]
                violation_type = {
                    ("start", "stop", "start"): "start_stop_start",
                    ("stop", "start", "stop"): "stop_start_stop",
                }.get(pattern)
                if violation_type and age_s < min_start_stop_gap_s and not protected_or_unreachable(window):
                    counts[violation_type] += 1
                    violations.append({
                        "type": violation_type,
                        "wb_id": wb_id,
                        "age_s": int(round(age_s)),
                        "events": window,
                    })

        phase = _event_phase_action(event)
        if phase:
            history = recent_phases.setdefault(wb_id, [])
            history.append({"event": event, "phase": phase, "ts": ts})
            del history[:-3]
            if len(history) == 3:
                pattern = tuple(item["phase"] for item in history)
                age_s = history[-1]["ts"] - history[0]["ts"]
                window = [item["event"] for item in history]
                if pattern in (("1p", "3p", "1p"), ("3p", "1p", "3p")):
                    if age_s < min_phase_gap_s and not protected_or_unreachable(window):
                        counts["phase_flip"] += 1
                        violations.append({
                            "type": "phase_flip",
                            "wb_id": wb_id,
                            "age_s": int(round(age_s)),
                            "events": window,
                        })

    return {"ok": not violations, "violations": violations, "counts": counts}
