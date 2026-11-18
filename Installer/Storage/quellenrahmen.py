"""Gemeinsame reine Druck- und Quellenrechnung, ohne Datei- oder Gerätezugriff.

Der Bestandsregler behält seine numerischen Fallbacks. Die belegte Schatten-
aufnahme kennzeichnet unbekannte Herkunft getrennt von dieser Bestandsdiagnose.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
from typing import Any, Dict
from .befehl_werte import EMS_POWER_SETTINGS_NONZERO_MIN_W
from .leistungsleser import safe_float as _safe_float, safe_int as _safe_int
try:
    from Installer.data_models import LIVE_SAMPLE_MAX_AGE_S
except ModuleNotFoundError:
    from data_models import LIVE_SAMPLE_MAX_AGE_S

# 50-W-Readback-Fenster: Ein frischer Readback innerhalb dieses Fensters
# bestätigt denselben früheren Cap; ab 50 W bleibt der Pfad fail-closed.
RSCP_POWER_SETTINGS_TOLERANCE_W = 50
PCC_LIMIT_SCHEMA = "pcc_headroom_limit_v1"
DC_QUELLENVERTRAG_VERSION = 2
RECHENREVISION = "quellenrahmen_v2"


def _zahl(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def _configured_kw_or_w(value: Any) -> int:
    raw = _safe_float(value, 0.0)
    if raw <= 0:
        return 0
    if raw < 250:
        return int(round(raw * 1000.0))
    return int(round(raw))


def _dc_tracker_limit_w(source: Dict[str, Any]) -> int:
    total = 0
    for idx in range(8):
        total += max(0, _safe_int(source.get(f"dc{idx}_max_w"), 0))
    return total if total >= 1000 else 0


def _inverter_ac_limit_w(
    cfg: Dict[str, Any],
    active_state: Dict[str, Any],
    live: Dict[str, Any],
) -> int:
    for key in (
        "wr_ac_limit_w",
        "wechselrichter_limit_w",
        "wechselrichterleistung_w",
        "inverter_ac_limit_w",
        "ac_power_limit_w",
    ):
        val = _configured_kw_or_w(cfg.get(key))
        if val >= 1000:
            return val
    for source in (live, active_state):
        for key in ("ac_power_limit_w", "wr_ac_limit_w", "inverter_ac_limit_w"):
            val = _safe_int(source.get(key), 0)
            if val >= 1000:
                return val
        val = _dc_tracker_limit_w(source)
        if val >= 1000:
            return val
    return 0


def _dc_coupled_ems_budget(
    pv_potential_w: Any,
    wr_max_ac_w: Any,
    export_limit_w: Any,
    home_w: Any,
    wp_w: Any,
    wallbox_actual_w: Any,
    battery_max_charge_w: Any,
    soc_pct: Any,
    external_ac_pv_w: Any = 0,
    external_ac_pv_valid: Any = False,
    external_ac_pv_source: Any = "",
) -> Dict[str, Any]:
    pv_total_w = max(0, _safe_int(pv_potential_w, 0))
    wr_limit_w = max(0, _safe_int(wr_max_ac_w, 0))
    feed_limit_w = max(0, _safe_int(export_limit_w, 0))
    fixed_load_w = max(0, _safe_int(home_w, 0)) + max(0, _safe_int(wp_w, 0))
    wb_w = max(0, _safe_int(wallbox_actual_w, 0))
    max_charge_w = max(0, _safe_int(battery_max_charge_w, 0))
    soc = _safe_float(soc_pct, 0.0)

    # PV_Power ist die physikalische Gesamt-PV am Netzpunkt. E3DC liefert den
    # externen AC-Erzeuger separat als EMS_POWER_ADD; e3dc_live hat ihn in
    # PV_Power bereits eingerechnet. Nur ein typgültiger Quellensplit darf ihn
    # vor der DC-Wechselrichtergrenze wieder abziehen. Fehlt dieser Nachweis,
    # bleibt die bisherige konservative Behandlung erhalten.
    external_source = str(external_ac_pv_source or "").strip()
    external_value = _safe_float(external_ac_pv_w, 0.0)
    external_trusted = bool(
        external_ac_pv_valid is True
        and external_source == "e3dc_add_power"
        and not isinstance(external_ac_pv_w, bool)
        and math.isfinite(external_value)
        and external_value >= 0.0
    )
    external_ac_w = (
        min(pv_total_w, max(0, int(round(external_value))))
        if external_trusted
        else 0
    )
    e3dc_dc_pv_w = max(0, pv_total_w - external_ac_w)

    if wr_limit_w >= 1000:
        battery_must_dc_w = max(0, e3dc_dc_pv_w - wr_limit_w)
        e3dc_ac_available_w = min(
            max(0, e3dc_dc_pv_w - battery_must_dc_w),
            wr_limit_w,
        )
    else:
        battery_must_dc_w = 0
        e3dc_ac_available_w = e3dc_dc_pv_w

    # Der externe AC-Erzeuger belastet nicht die E3DC-DC-/WR-Grenze, gehört
    # aber weiterhin vollständig in die PCC-/Einspeisebilanz.
    ac_available_w = e3dc_ac_available_w + external_ac_w

    total_ac_load_w = fixed_load_w + wb_w
    export_potential_w = max(0, ac_available_w - total_ac_load_w)
    battery_must_ac_w = (
        max(0, export_potential_w - feed_limit_w)
        if feed_limit_w > 0
        else 0
    )
    battery_must_total_w = battery_must_dc_w + battery_must_ac_w
    battery_room_w = 0 if soc >= 100.0 else max_charge_w
    battery_must_real_w = min(battery_must_total_w, battery_room_w)

    dc_real_w = min(battery_must_dc_w, battery_room_w)
    remaining_after_dc_w = max(0, battery_room_w - dc_real_w)
    ac_real_w = min(battery_must_ac_w, remaining_after_dc_w)
    loss_dc_w = max(0, battery_must_dc_w - dc_real_w)
    loss_ac_w = max(0, battery_must_ac_w - ac_real_w)

    wallbox_budget_total_w = max(0, ac_available_w - fixed_load_w - battery_must_ac_w)
    wallbox_budget_increase_w = max(0, wallbox_budget_total_w - wb_w)

    return {
        "pv_total_w": pv_total_w,
        "e3dc_dc_pv_w": e3dc_dc_pv_w,
        "e3dc_ac_available_w": e3dc_ac_available_w,
        "external_ac_pv_w": external_ac_w,
        "external_ac_pv_trusted": external_trusted,
        "external_ac_pv_source": external_source,
        "ac_available_w": ac_available_w,
        "battery_must_dc_w": battery_must_dc_w,
        "battery_must_ac_w": battery_must_ac_w,
        "battery_must_real_w": battery_must_real_w,
        "wallbox_budget_total_w": wallbox_budget_total_w,
        "wallbox_budget_increase_w": wallbox_budget_increase_w,
        "curtailment_loss_w": loss_dc_w + loss_ac_w,
        "export_limit_w": feed_limit_w,
        "wr_max_ac_w": wr_limit_w,
    }


def _meter_balance_home_w(pv_w: Any, grid_w: Any, bat_w: Any) -> int:
    return int(_safe_int(pv_w, 0) + _safe_int(grid_w, 0) - _safe_int(bat_w, 0))


def _meter_balance_plausible(
    pv_w: Any,
    grid_w: Any,
    bat_w: Any,
    home_w: Any,
) -> Dict[str, Any]:
    pv = max(0, _safe_int(pv_w, 0))
    grid = _safe_int(grid_w, 0)
    bat = _safe_int(bat_w, 0)
    home = max(0, _safe_int(home_w, 0))
    balance_home_w = _meter_balance_home_w(pv, grid, bat)
    if balance_home_w >= 0:
        balance_error_w = abs(balance_home_w - home)
    else:
        balance_error_w = abs(balance_home_w) + home
    error_limit_w = max(1800, int(round(pv * 0.25)))
    relevant = bool(pv > 1000 and (abs(grid) > 1000 or abs(bat) > 500))
    plausible = not (
        relevant
        and (
            balance_home_w < -500
            or balance_error_w > error_limit_w
        )
    )
    return {
        "home_balance_w": balance_home_w,
        "home_balance_error_w": balance_error_w,
        "home_balance_error_limit_w": error_limit_w,
        "home_balance_plausible": plausible,
    }


def resolve_buffered_pcc_limit(
    configured_limit_w: Any,
    live_readback_limit_w: Any,
    buffer_w: Any,
) -> Dict[str, Any]:
    """Materialisiert den PCC-Grenzvertrag des Storage-Reglers.

    Nur positive Konfigurations-/Readbackwerte sind aktive Quellen. Ein daraus
    resultierendes ``limit_w == 0`` bleibt durch ``active == True`` eindeutig
    von fehlenden Quellen unterschieden. Der Puffer wird exakt einmal nach der
    Auswahl des kleineren harten Limits abgezogen.
    """

    configured = max(0.0, _safe_float(configured_limit_w, 0.0))
    live = max(0.0, _safe_float(live_readback_limit_w, 0.0))
    buffer_value = max(0.0, _safe_float(buffer_w, 0.0))
    configured_active = configured > 0.0
    live_active = live > 0.0

    if configured_active and live_active:
        hard_limit = min(configured, live)
        source = "config_below_rscp_buffered" if configured < live - 50.0 else "rscp_buffered"
    elif live_active:
        hard_limit = live
        source = "rscp_buffered"
    elif configured_active:
        hard_limit = configured
        source = "config_buffered"
    else:
        hard_limit = 0.0
        source = "none"

    active = configured_active or live_active
    effective_limit = max(0.0, hard_limit - buffer_value) if active else 0.0
    return {
        "schema_version": PCC_LIMIT_SCHEMA,
        "active": active,
        "limit_w": round(effective_limit, 3) if active else None,
        "hard_limit_w": round(hard_limit, 3) if active else None,
        "buffer_w": round(buffer_value, 3),
        "source": source,
        "configured": {
            "active": configured_active,
            "limit_w": round(configured, 3) if configured_active else None,
        },
        "live_readback": {
            "active": live_active,
            "limit_w": round(live, 3) if live_active else None,
        },
        "zero_semantics": "ACTIVE_ZERO_ONLY_WHEN_SOURCE_PRESENT_AND_BUFFERED",
    }


def dc_quellenvertrag(
    cfg: Dict[str, Any],
    live: Dict[str, Any],
    now_s: float,
) -> Dict[str, Any]:
    """Bindet den sanften Laderahmen ausschließlich an einen frischen PV-Split."""

    live = live or {}
    now_value = float(now_s)
    max_age_s = LIVE_SAMPLE_MAX_AGE_S
    live_ts = _safe_float(live.get("_ts"), 0.0)
    live_age_s = now_value - live_ts if live_ts > 0.0 else float("inf")
    max_future_skew_s = 5.0
    total_raw = live.get("PV_Power")
    external_raw = live.get("Ext_PV_Power")
    grid_raw = live.get("Grid_Power")
    battery_raw = live.get("Battery_Power")
    values_typed = bool(
        isinstance(total_raw, (int, float))
        and not isinstance(total_raw, bool)
        and isinstance(external_raw, (int, float))
        and not isinstance(external_raw, bool)
        and math.isfinite(float(total_raw))
        and math.isfinite(float(external_raw))
    )
    grid_battery_typed = bool(
        isinstance(grid_raw, (int, float))
        and not isinstance(grid_raw, bool)
        and isinstance(battery_raw, (int, float))
        and not isinstance(battery_raw, bool)
        and math.isfinite(float(grid_raw))
        and math.isfinite(float(battery_raw))
    )
    values_nonnegative = bool(
        values_typed
        and float(total_raw) >= 0.0
        and float(external_raw) >= 0.0
    )
    total_pv_w = max(0, int(round(float(total_raw)))) if values_typed else 0
    external_ac_pv_w = max(0, int(round(float(external_raw)))) if values_typed else 0
    sample_valid = live.get("RSCP_Sample_Valid") is True
    decision_usable = live.get("Power_Decision_Usable") is True
    split_valid = live.get("Ext_PV_Power_Valid") is True
    grid_valid = live.get("Grid_Power_Valid", sample_valid) is True
    battery_valid = live.get("Battery_Power_Valid", sample_valid) is True
    source_fresh = bool(
        live_ts > 0.0
        and -max_future_skew_s <= live_age_s <= max_age_s
    )
    source_valid = bool(
        values_nonnegative
        and grid_battery_typed
        and sample_valid
        and decision_usable
        and split_valid
        and grid_valid
        and battery_valid
        and source_fresh
        and external_ac_pv_w <= total_pv_w
    )
    blocker = ""
    if not values_typed:
        blocker = "pv_split_untyped"
    elif float(total_raw) < 0.0:
        blocker = "pv_total_negative"
    elif float(external_raw) < 0.0:
        blocker = "external_pv_negative"
    elif not grid_battery_typed:
        blocker = "grid_or_battery_power_untyped"
    elif not sample_valid:
        blocker = "rscp_sample_invalid"
    elif not decision_usable:
        blocker = "power_decision_unusable"
    elif not split_valid:
        blocker = "external_pv_split_invalid"
    elif not grid_valid:
        blocker = "grid_power_invalid"
    elif not battery_valid:
        blocker = "battery_power_invalid"
    elif live_age_s < -max_future_skew_s:
        blocker = "pv_split_future"
    elif not source_fresh:
        blocker = "pv_split_stale"
    elif external_ac_pv_w > total_pv_w:
        blocker = "external_pv_exceeds_total"
    e3dc_pv_w = (
        max(0, total_pv_w - external_ac_pv_w)
        if source_valid
        else 0
    )
    grid_w = int(round(float(grid_raw))) if grid_battery_typed else 0
    battery_power_w = (
        int(round(float(battery_raw)))
        if grid_battery_typed
        else 0
    )
    no_import_margin_w = max(
        0,
        _safe_int(cfg.get("storage_dc_first_no_import_margin_w"), 100),
    )
    # Positiver Battery_Power bedeutet laufendes Laden, negativer Wert
    # Entladen. Die Differenz zum Netzpunkt ist damit die maximal zulässige
    # Gesamtladung, ohne aus dem Netz nachzuziehen. Eine kleine Exportmarge
    # fängt Messlatenz und Rundung ab.
    raw_no_import_cap_w = (
        max(0, battery_power_w - grid_w)
        if source_valid
        else 0
    )
    no_import_cap_w = max(0, raw_no_import_cap_w - no_import_margin_w)
    e3dc_dc_surplus_w = min(e3dc_pv_w, no_import_cap_w)
    return {
        "contract_version": DC_QUELLENVERTRAG_VERSION,
        "valid": source_valid,
        "blocker": blocker,
        "live_ts": live_ts if live_ts > 0.0 else None,
        "age_s": round(live_age_s, 3) if math.isfinite(live_age_s) else None,
        "max_age_s": max_age_s,
        "max_future_skew_s": max_future_skew_s,
        "sample_valid": sample_valid,
        "decision_usable": decision_usable,
        "external_split_valid": split_valid,
        "grid_valid": grid_valid,
        "battery_valid": battery_valid,
        "total_pv_w": total_pv_w,
        "external_ac_pv_w": external_ac_pv_w,
        "e3dc_pv_w": e3dc_pv_w,
        "grid_w": grid_w if grid_battery_typed else None,
        "battery_power_w": (
            battery_power_w
            if grid_battery_typed
            else None
        ),
        "no_import_margin_w": no_import_margin_w,
        "raw_no_import_cap_w": raw_no_import_cap_w,
        "no_import_cap_w": no_import_cap_w,
        "e3dc_dc_surplus_w": e3dc_dc_surplus_w,
        "source": str(live.get("Ext_PV_Power_Source") or "unavailable"),
    }


def druck_parameter(cfg, max_charge_w):
    """Bestehende Parameter, keine neuen Konfigurationsschlüssel."""
    parameter = {}
    parameter['grid_limit_w'] = max(50, _safe_int(cfg.get("tl_grid_limit_w"), 100))
    parameter['grid_relief_enter_w'] = max(300, _safe_int(
                cfg.get("storage_parallel_grid_relief_enter_w"),
                max(300, parameter['grid_limit_w'] * 3),
            ))
    parameter['curve_charge_enter_w'] = max(300, _safe_int(
                cfg.get("storage_parallel_curve_charge_enter_w"), 300
            ))
    parameter['abregel_min_charge_w'] = max(0, _safe_int(
                cfg.get("abregel_min_charge_w"), 300
            ))
    parameter['curve_cap_enter_margin_w'] = max(100, _safe_int(
                cfg.get("storage_parallel_curve_cap_enter_margin_w"), 600
            ))
    parameter['curve_cap_keep_margin_w'] = max(0, min(
                parameter['curve_cap_enter_margin_w'],
                _safe_int(cfg.get("storage_parallel_curve_cap_keep_margin_w"), 150),
            ))
    parameter['curve_cap_feed_buffer_w'] = max(0, _safe_int(
                cfg.get("abregel_puffer_w"),
                300,
            ))
    curve_cap_release_hysteresis_default_w = (
                _safe_int(cfg.get("abregel_auto_band_w"), 1800)
                if "abregel_auto_band_w" in cfg
                else 2000
            )
    parameter['curve_cap_release_hysteresis_w'] = max(
                parameter['curve_cap_feed_buffer_w'],
                _safe_int(
                    cfg.get("abregel_hysterese_w"),
                    curve_cap_release_hysteresis_default_w,
                ),
            )
    parameter['curve_cap_release_band_w'] = parameter['curve_cap_release_hysteresis_w']
    parameter['curve_cap_feedback_band_w'] = max(
                parameter['curve_cap_release_band_w'],
                _safe_int(
                    cfg.get("storage_parallel_curve_cap_feedback_band_w"),
                    parameter['curve_cap_release_band_w'],
                ),
            )
    parameter['curve_cap_step_w'] = max(100, _safe_int(
                cfg.get("storage_parallel_curve_cap_step_w"), 500
            ))
    parameter['curve_tolerance_pct'] = max(0.2, _safe_float(
            cfg.get("storage_parallel_curve_tolerance_pct"),
            _safe_float(cfg.get("tl_tolerance_pct"), 3.0),
        ))
    parameter['curve_auto_hold_exit_pct'] = max(0.5, min(
            parameter['curve_tolerance_pct'],
            _safe_float(
                cfg.get("storage_parallel_curve_auto_hold_exit_pct"),
                0.5,
            ),
        ))
    parameter['max_charge_w'] = max_charge_w
    return parameter

@dataclass(frozen=True)
class DruckErgebnis:
    werte: dict


def druck_rechnen(*, cfg, live, messsatz, parameter, vorzustand, jetzt_s):
    """Expliziter Vorzustand hinein, Diagnose heraus.

    Kein Zustand des Aufrufers wird verändert. Die spätere Reglerentscheidung
    bleibt beim Regler; der Schatten führt seinen eigenen ausgegebenen Cap fort.
    """
    active_state = dict(vorzustand)
    now_s = jetzt_s
    previous_parallel_state = str(active_state.get('previous_parallel_state') or '')
    previous_parallel_val = active_state.get('previous_parallel_val', 0)
    bat_w = messsatz['bat_w']
    curve_above_enter = messsatz['curve_above_enter']
    curve_above_keep = messsatz['curve_above_keep']
    curve_above_soft = messsatz['curve_above_soft']
    grid_ema_w = messsatz['grid_ema_w']
    grid_w = messsatz['grid_w']
    home_rule_w = messsatz['home_rule_w']
    home_w = messsatz['home_w']
    pv_w = messsatz['pv_w']
    soc = messsatz['soc']
    wallbox_home_includes = messsatz['wallbox_home_includes']
    wallbox_w = messsatz['wallbox_w']
    wp_w = messsatz['wp_w']
    curve_cap_target_w = 0
    curve_cap_margin_w = parameter['curve_cap_keep_margin_w'] if previous_parallel_state == "parallel_curve_charge_cap" else parameter['curve_cap_enter_margin_w']
    curve_cap_relevant = bool(
        curve_above_enter
        or curve_above_keep
        or (previous_parallel_state == "parallel_curve_charge_cap" and curve_above_soft)
    )
    previous_curve_cap_w = (
        max(0, _safe_int(previous_parallel_val, 0))
        if previous_parallel_state == "parallel_curve_charge_cap"
        else 0
    )
    curve_cap_release_pending = bool(active_state.get("curve_cap_release_pending"))
    curve_cap_release_requested = bool(
        active_state.get("curve_cap_release_requested")
    )
    curve_cap_release_confirmed_since_ts = max(
        0.0,
        _safe_float(active_state.get("curve_cap_release_confirmed_since_ts"), 0.0),
    )
    curve_cap_bounded_zero_w = max(
        EMS_POWER_SETTINGS_NONZERO_MIN_W,
        parameter['abregel_min_charge_w'],
    )
    curve_cap_tracking_active = bool(
        (
            previous_parallel_state == "parallel_curve_charge_cap"
            and previous_curve_cap_w > 0
        )
        or curve_cap_release_pending
        or curve_cap_release_requested
    )
    if curve_cap_relevant:
        curve_cap_target_w = 0
    curve_cap_keep_active = False
    curve_cap_grid_contract_valid = bool(
        _truthy(live.get("RSCP_Sample_Valid", True))
        and _truthy(live.get("Grid_Power_Valid", True))
    )
    grid_import_w = max(0, grid_w, grid_ema_w)
    grid_export_w = 0 if grid_import_w > 0 else max(0, -grid_w, -grid_ema_w)
    curve_cap_real_grid_import_active = bool(
        curve_cap_grid_contract_valid
        and grid_import_w >= parameter['grid_relief_enter_w']
    )
    curve_cap_release_below_since_ts = (
        max(0.0, _safe_float(active_state.get("curve_cap_release_below_since_ts"), 0.0))
        if (
            previous_parallel_state == "parallel_curve_charge_cap"
            and previous_curve_cap_w > 0
        )
        else 0.0
    )
    curve_cap_post_release_until_ts = max(
        0.0,
        _safe_float(active_state.get("curve_cap_post_release_until_ts"), 0.0),
    )
    if curve_cap_post_release_until_ts <= now_s:
        curve_cap_post_release_until_ts = 0.0
    curve_cap_post_release_guard_initial = bool(curve_cap_post_release_until_ts > now_s)
    settings_readback_valid = bool(
        live.get("ems_power_settings_read") is True
        and live.get("ems_power_settings_valid") is True
        and isinstance(live.get("power_limits_active"), bool)
        and isinstance(live.get("ems_max_charge_power_w"), int)
        and not isinstance(live.get("ems_max_charge_power_w"), bool)
        and int(live.get("ems_max_charge_power_w")) >= 0
        and isinstance(live.get("ems_max_discharge_power_w"), int)
        and not isinstance(live.get("ems_max_discharge_power_w"), bool)
        and int(live.get("ems_max_discharge_power_w")) >= 0
        and isinstance(live.get("ems_discharge_start_power_w"), int)
        and not isinstance(live.get("ems_discharge_start_power_w"), bool)
        and int(live.get("ems_discharge_start_power_w")) >= 0
    )
    settings_bounded_zero_confirmed = bool(
        settings_readback_valid
        and live.get("power_limits_active") is True
        and int(live.get("ems_max_charge_power_w")) <= curve_cap_bounded_zero_w
    )
    settings_previous_curve_cap_confirmed = bool(
        settings_readback_valid
        and live.get("power_limits_active") is True
        and previous_curve_cap_w > 0
        and abs(int(live.get("ems_max_charge_power_w")) - previous_curve_cap_w)
        < RSCP_POWER_SETTINGS_TOLERANCE_W
    )
    settings_release_confirmed = bool(
        settings_readback_valid
        and live.get("power_limits_active") is False
    )
    meter_home_w = home_rule_w + wp_w
    if wallbox_home_includes and wallbox_w > 250:
        meter_home_w += int(wallbox_w)
    meter_balance = _meter_balance_plausible(pv_w, grid_w, bat_w, meter_home_w)
    meter_balance_plausible = bool(meter_balance["home_balance_plausible"])
    # Harter Abregelschutz darf sich nicht an der aktuellen Batterieladung
    # hochziehen. Die Batterieladung kann bereits Ergebnis der vorherigen
    # Regelentscheidung sein. Entscheidend sind nur Netz-Einspeisung und
    # echte PV-Leistung oberhalb der WR-Grenze.
    curve_cap_measured_charge_w = 0
    inverter_limit_w = _inverter_ac_limit_w(cfg, active_state, live)
    configured_export_limit_w = _configured_kw_or_w(cfg.get("einspeiselimit", 0))
    live_derate_limit_w = _safe_int(live.get("derate_at_power_w", active_state.get("derate_at_power_w")), 0)
    pcc_limit_contract = resolve_buffered_pcc_limit(
        configured_export_limit_w,
        live_derate_limit_w,
        parameter['curve_cap_feed_buffer_w'],
    )
    derate_hard_limit_w = int(pcc_limit_contract.get("hard_limit_w") or 0)
    derate_limit_w = int(pcc_limit_contract.get("limit_w") or 0)
    derate_limit_source = str(pcc_limit_contract.get("source") or "none")
    derating_active = bool(
        _truthy(live.get("pv_derating_active", active_state.get("pv_derating_active", False)))
        or _truthy(live.get("ems_derating_active", active_state.get("ems_derating_active", False)))
        or _truthy(live.get("power_limits_active", active_state.get("power_limits_active", False)))
    )
    ems_budget = _dc_coupled_ems_budget(
        pv_w,
        inverter_limit_w,
        derate_limit_w,
        home_w,
        wp_w,
        wallbox_w,
        parameter['max_charge_w'],
        soc,
        external_ac_pv_w=live.get("Ext_PV_Power"),
        external_ac_pv_valid=live.get("Ext_PV_Power_Valid"),
        external_ac_pv_source=live.get("Ext_PV_Power_Source"),
    )
    inverter_pressure_w = int(ems_budget["battery_must_dc_w"])
    derating_pressure_w = int(ems_budget["battery_must_ac_w"])
    ems_mandatory_charge_w = int(ems_budget["battery_must_real_w"])
    feed_export_threshold_w = max(0, int(derate_limit_w))
    release_export_threshold_w = (
        max(0, int(feed_export_threshold_w) - int(parameter['curve_cap_release_band_w']))
        if feed_export_threshold_w > 0
        else 0
    )
    if feed_export_threshold_w > 0:
        grid_export_error_w = int(grid_export_w) - int(feed_export_threshold_w)
        grid_export_over_limit_w = max(0, grid_export_error_w)
        curve_cap_below_threshold_w = max(0, -grid_export_error_w)
    else:
        grid_export_error_w = int(grid_export_w) if curve_above_enter else 0
        grid_export_over_limit_w = max(0, grid_export_error_w)
        curve_cap_below_threshold_w = 0
    curve_cap_feedback_active = bool(
        curve_cap_tracking_active
        and curve_cap_grid_contract_valid
        and feed_export_threshold_w > 0
        and not curve_cap_real_grid_import_active
        and (
            grid_export_over_limit_w > 0
            or curve_cap_below_threshold_w <= parameter['curve_cap_feedback_band_w']
        )
    )
    if curve_cap_feedback_active:
        curve_cap_grid_pressure_w = max(0, int(previous_curve_cap_w) + int(grid_export_error_w))
        if curve_cap_grid_pressure_w < previous_curve_cap_w:
            curve_cap_grid_pressure_w = max(
                curve_cap_grid_pressure_w,
                max(0, int(previous_curve_cap_w) - int(parameter['curve_cap_step_w'])),
            )
    else:
        curve_cap_grid_pressure_w = int(grid_export_over_limit_w)
    curve_cap_dc_hold_margin_w = max(
        int(previous_curve_cap_w),
        int(inverter_pressure_w),
    )
    curve_cap_dc_hold_active = bool(
        previous_curve_cap_w >= parameter['curve_charge_enter_w']
        and curve_cap_grid_contract_valid
        and curve_above_soft
        and inverter_pressure_w >= max(parameter['abregel_min_charge_w'], parameter['curve_charge_enter_w'])
        and feed_export_threshold_w > 0
        and release_export_threshold_w > 0
        and not curve_cap_real_grid_import_active
        and (
            int(grid_export_w) + int(curve_cap_dc_hold_margin_w)
            >= max(0, int(release_export_threshold_w) - int(parameter['curve_cap_feedback_band_w']))
        )
    )
    # DC-/WR-Druck darf den harten Abregelschutz nur begleiten, wenn der
    # AC-Netzpunkt schon an der Abregelgrenze arbeitet oder der Pfad durch
    # die Hysterese gehalten wird. Ist der Abregelschutz bereits aktiv,
    # bleibt echter WR-Druck auch bei kurzen Haus-/Grid-Spruengen erhalten.
    # PV knapp oberhalb der WR-Leistung ist sonst nur Diagnose; der E3DC
    # kann diesen Anteil oft autonom aufnehmen.
    curve_cap_dc_pressure_active = bool(
        inverter_pressure_w > 0
        and curve_cap_grid_contract_valid
        and feed_export_threshold_w > 0
        and not curve_cap_real_grid_import_active
        and (
            grid_export_over_limit_w > 0
            or curve_cap_feedback_active
            or curve_cap_dc_hold_active
            or (
                release_export_threshold_w > 0
                and grid_export_w >= release_export_threshold_w
                and (
                    curve_above_soft
                    or curve_above_keep
                    or curve_above_enter
                    or previous_curve_cap_w >= parameter['curve_charge_enter_w']
                    or derating_active
                )
            )
        )
    )
    curve_cap_dc_pressure_w = int(inverter_pressure_w) if curve_cap_dc_pressure_active else 0
    curve_cap_model_pressure_w = (
        int(ems_mandatory_charge_w)
        if (
            meter_balance_plausible
            and (
                grid_export_over_limit_w > 0
                or curve_cap_feedback_active
                or curve_cap_dc_pressure_active
            )
        )
        else 0
    )
    curve_cap_direct_pressure_w = int(grid_export_over_limit_w) + int(curve_cap_dc_pressure_w)
    curve_cap_pressure_w = max(
        int(curve_cap_grid_pressure_w),
        int(curve_cap_direct_pressure_w),
    )
    curve_cap_physical_pressure_w = max(
        int(curve_cap_grid_pressure_w) + int(inverter_pressure_w),
        int(inverter_pressure_w),
    )
    werte = {
        'configured_export_limit_w': configured_export_limit_w,
        'curve_cap_below_threshold_w': curve_cap_below_threshold_w,
        'curve_cap_bounded_zero_w': curve_cap_bounded_zero_w,
        'curve_cap_dc_hold_active': curve_cap_dc_hold_active,
        'curve_cap_dc_hold_margin_w': curve_cap_dc_hold_margin_w,
        'curve_cap_dc_pressure_active': curve_cap_dc_pressure_active,
        'curve_cap_dc_pressure_w': curve_cap_dc_pressure_w,
        'curve_cap_direct_pressure_w': curve_cap_direct_pressure_w,
        'curve_cap_feedback_active': curve_cap_feedback_active,
        'curve_cap_grid_contract_valid': curve_cap_grid_contract_valid,
        'curve_cap_grid_pressure_w': curve_cap_grid_pressure_w,
        'curve_cap_keep_active': curve_cap_keep_active,
        'curve_cap_margin_w': curve_cap_margin_w,
        'curve_cap_measured_charge_w': curve_cap_measured_charge_w,
        'curve_cap_model_pressure_w': curve_cap_model_pressure_w,
        'curve_cap_physical_pressure_w': curve_cap_physical_pressure_w,
        'curve_cap_post_release_guard_initial': curve_cap_post_release_guard_initial,
        'curve_cap_post_release_until_ts': curve_cap_post_release_until_ts,
        'curve_cap_pressure_w': curve_cap_pressure_w,
        'curve_cap_real_grid_import_active': curve_cap_real_grid_import_active,
        'curve_cap_release_below_since_ts': curve_cap_release_below_since_ts,
        'curve_cap_release_confirmed_since_ts': curve_cap_release_confirmed_since_ts,
        'curve_cap_release_pending': curve_cap_release_pending,
        'curve_cap_release_requested': curve_cap_release_requested,
        'curve_cap_relevant': curve_cap_relevant,
        'curve_cap_target_w': curve_cap_target_w,
        'curve_cap_tracking_active': curve_cap_tracking_active,
        'derate_hard_limit_w': derate_hard_limit_w,
        'derate_limit_source': derate_limit_source,
        'derate_limit_w': derate_limit_w,
        'derating_active': derating_active,
        'derating_pressure_w': derating_pressure_w,
        'ems_budget': ems_budget,
        'ems_mandatory_charge_w': ems_mandatory_charge_w,
        'feed_export_threshold_w': feed_export_threshold_w,
        'grid_export_error_w': grid_export_error_w,
        'grid_export_over_limit_w': grid_export_over_limit_w,
        'grid_export_w': grid_export_w,
        'grid_import_w': grid_import_w,
        'inverter_limit_w': inverter_limit_w,
        'inverter_pressure_w': inverter_pressure_w,
        'live_derate_limit_w': live_derate_limit_w,
        'meter_balance': meter_balance,
        'meter_balance_plausible': meter_balance_plausible,
        'meter_home_w': meter_home_w,
        'pcc_limit_contract': pcc_limit_contract,
        'previous_curve_cap_w': previous_curve_cap_w,
        'release_export_threshold_w': release_export_threshold_w,
        'settings_bounded_zero_confirmed': settings_bounded_zero_confirmed,
        'settings_previous_curve_cap_confirmed': settings_previous_curve_cap_confirmed,
        'settings_readback_valid': settings_readback_valid,
        'settings_release_confirmed': settings_release_confirmed,
    }
    return DruckErgebnis(werte)


# Benannte Bestandsschlüssel der Druckrechnung; die Übergabe friert sie mit
# den übrigen Konfigurationsfeldern ein. Keine Anlagenkennungen.
DRUCK_CONFIG_FELDER = (
    'einspeiselimit', 'wr_ac_limit_w', 'wechselrichter_limit_w',
    'wechselrichterleistung_w', 'inverter_ac_limit_w', 'ac_power_limit_w',
    'abregel_puffer_w', 'abregel_auto_band_w', 'abregel_hysterese_w',
    'abregel_min_charge_w', 'tl_grid_limit_w', 'tl_tolerance_pct',
    'storage_parallel_curve_auto_hold_exit_pct',
    'storage_parallel_grid_relief_enter_w', 'storage_parallel_curve_charge_enter_w',
    'storage_parallel_curve_cap_enter_margin_w', 'storage_parallel_curve_cap_keep_margin_w',
    'storage_parallel_curve_cap_feedback_band_w', 'storage_parallel_curve_cap_step_w',
    'storage_dc_first_charge_limit_enable', 'storage_dc_first_no_import_margin_w',
)


@dataclass(frozen=True)
class SchattenQuellen:
    bedarf_w: float | None
    zeit_s: float | None
    grund: str
    rechnung: DruckErgebnis | None
    quellen: dict
    ladekappe_angefordert: bool
    ladekappe_w: float | None
    herkunft: tuple
    revision: str = RECHENREVISION


def schatten_quellen(cfg, live, *, haus_w, nativ_w, kurve, max_laden_w,
                     leistungsdaten_gueltig, vorzustand, jetzt_s):
    """Eingefrorene Rohwerte statt produktivem Endbedarf; eigenes Capgedächtnis.

    Fehlender Bedarf nimmt ausschließlich diesen Optimierungsanspruch zurück.
    Der DC-only-Rahmen ist wie im Produkt der E3DC-PV-Anteil des gültigen
    Splits. Frische prüft allein der Quellenvertrag hier und `Messwert.zahl`
    im Kern; die Messzeit ist die der Liveprobe, der ADD-Split trägt sein
    eigenes Alter aus demselben Poll.
    """
    quelle = dc_quellenvertrag(cfg, live, jetzt_s)
    split_ok = (leistungsdaten_gueltig is True and quelle['valid'] is True
                and live.get('Ext_PV_Power_Source') == 'e3dc_add_power')
    quellen = dict(
        gesamt_pv_w=live.get('PV_Power') if leistungsdaten_gueltig is True
                    and _zahl(live.get('PV_Power')) and live['PV_Power'] >= 0 else None)
    quellen.update(ac_w=quelle['external_ac_pv_w'] if split_ok else None,
        dc_w=quelle['e3dc_pv_w'] if split_ok else None,
        gueltig=split_ok, grund=('belegt' if split_ok else 'Leistungsmessung ungültig'
            if leistungsdaten_gueltig is not True else quelle['blocker'] or 'AC-Herkunft fehlt'))
    angefordert = _truthy(cfg.get('storage_dc_first_charge_limit_enable', False))
    kappe = quelle['e3dc_pv_w'] if split_ok else None
    stamp, age = live.get('_ts'), live.get('Ext_PV_Power_Age_S')
    zeiten = [stamp] + ([stamp - age] if _zahl(stamp) and _zahl(age) and age >= 0 else [])
    zeit = min(zeiten) if all(_zahl(v) and v > 0 for v in zeiten) else None
    fehler = []
    if leistungsdaten_gueltig is not True:
        fehler.append('Leistungsmessung ungültig, vorhandener Messwertvertrag')
    if not _zahl(live.get('SOC')):
        fehler.append('SOC fehlt')
    if not split_ok:
        fehler.append('Quellensplit unbekannt')
    if zeit is None:
        fehler.append('Messzeit fehlt')
    wr = _inverter_ac_limit_w(cfg, {}, live)
    wr_config = any(_configured_kw_or_w(cfg.get(key)) >= 1000 for key in (
        'wr_ac_limit_w','wechselrichter_limit_w','wechselrichterleistung_w','inverter_ac_limit_w','ac_power_limit_w'))
    # Ohne WR-Grenze entfällt wie im Produkt nur der DC-Anteil des Drucks.
    herkunft = ['WR:config' if wr_config else 'WR:live' if wr else 'WR:fehlt']
    parameter = druck_parameter(cfg, max_laden_w if _zahl(max_laden_w) else 0)
    pcc = resolve_buffered_pcc_limit(_configured_kw_or_w(cfg.get('einspeiselimit')),
        _safe_int(live.get('derate_at_power_w')), parameter['curve_cap_feed_buffer_w'])
    if not pcc['active']:
        fehler.append('PCC-Grenze fehlt')
    herkunft.append('PCC:' + pcc['source'])
    if fehler:
        return SchattenQuellen(None, zeit, '; '.join(fehler), None, quellen,
                               angefordert, kappe, tuple(herkunft))
    previous = dict(vorzustand or {})
    if previous and not (_zahl(previous.get('zeit_s'))
            and 0 <= jetzt_s - previous['zeit_s'] <= LIVE_SAMPLE_MAX_AGE_S):
        previous = {}  # Kein Kontinuitätsbeleg über eine Messlücke.
    gap = kurve.boden_pct - live['SOC'] if kurve is not None else None
    # Haus und native Box wirken nur auf die Bilanzdiagnose, nicht auf den Druck;
    # fehlend zählen sie dort wie im Regler als 0.
    messsatz = dict(pv_w=_safe_int(live['PV_Power']), grid_w=_safe_int(live['Grid_Power']),
        grid_ema_w=_safe_int(live['Grid_Power']), bat_w=_safe_int(live['Battery_Power']),
        home_w=_safe_int(haus_w), home_rule_w=_safe_int(haus_w), wp_w=0,
        wallbox_w=_safe_int(nativ_w), wallbox_home_includes=False, soc=live['SOC'],
        curve_above_enter=gap is not None and -gap >= parameter['curve_tolerance_pct'],
        curve_above_keep=False,  # Der Schatten kennt kein Kurven-Auto-Hold.
        curve_above_soft=gap is not None and -gap >= parameter['curve_auto_hold_exit_pct'])
    rechnung = druck_rechnen(cfg=cfg, live=live, messsatz=messsatz,
        parameter=parameter, vorzustand=previous, jetzt_s=jetzt_s)
    return SchattenQuellen(rechnung.werte['curve_cap_pressure_w'], zeit, 'belegt',
        rechnung, quellen, angefordert, kappe, tuple(herkunft))


def schatten_fortschreiben(quellen, kernergebnis, jetzt_s):
    """Nur die eigene hypothetische Ausgabe wird zum nächsten Vorzustand."""
    soll = kernergebnis.soll
    aktiv = (not soll.grenzen_frei and soll.modus in ('KURVE', 'HALTEN')
             and soll.ladegrenze_w is not None)
    return dict(previous_parallel_state='parallel_curve_charge_cap' if aktiv else '',
                previous_parallel_val=soll.ladegrenze_w if aktiv else 0, zeit_s=jetzt_s)
