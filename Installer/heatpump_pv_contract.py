"""Reiner PV-Wärmevertrag: Auftragsbindung, Schutzzeit und Quellenenergie.

Dieses Modul besitzt keinen Hardwareausgang. Der Storage Manager besitzt das
Energiekonto; der Energy Manager besitzt Kanalaufträge und Gerätebeobachtung.
Leistungswerte beziehen sich auf dieselbe elektrische Messgrenze der WP.
"""

from __future__ import annotations

import copy
import math


# Aus der bestehenden PV-Startlogik im Energy Manager: 500 W Schaltabstand.
# Gemeinsame Startkante für Qualifikation und Freigabe, kein Dauerabzug.
PV_START_MARGIN_W = 500.0
GRID_MEASUREMENT_BAND_W = 200.0
REQUEST_WITHDRAWAL_DEBOUNCE_S = 45.0
# Bestehende WP-Reaktionszeit (wp_pv_reaction_s, Standard 30 s).
# Die unabhängige Netzschranke erhält diese feste, zusammenhängende Frist.
GRID_STOP_DEBOUNCE_S = 30.0
GRID_STOP_THRESHOLD_W = 2500.0

DEMAND_SCHEMA = "heatpump_pv_demand_v1"
STATE_SCHEMA = "heatpump_pv_state_v1"
CONTRACT_SCHEMA = "heatpump_pv_contract_v1"
HARD_PROTECTIONS = frozenset({
    "user_off", "hardware_fault", "emergency_reserve", "bms_limit", "heat_source_limit",
    "house_connection_limit", "invalid_control_data", "electrical_profile_exceeded",
    "hard_battery_energy_limit", "hard_grid_energy_limit",
    "bridge_source_disabled",
})


def _number(value, default=None, *, minimum=0.0):
    if isinstance(value, bool):
        return default
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return default
    return result if math.isfinite(result) and result >= minimum else default


def _dict(value):
    return value if isinstance(value, dict) else {}


def _clock(value, state):
    """Schutzzeit läuft monoton; ein Bootwechsel verbraucht keine unbekannte Restzeit."""
    if not isinstance(value, dict):
        wall = _number(value, 0.0)
        return wall, wall, None, False
    wall = _number(value.get("wall_s", value.get("wall_ts")), 0.0)
    mono = _number(value.get("monotonic_s", value.get("monotonic_ts")))
    boot = value.get("boot_id")
    previous = _dict(state.get("clock"))
    current = {"wall_s": wall, "monotonic_s": mono, "boot_id": boot}
    if mono is None or not isinstance(boot, str) or not boot or boot == "unknown":
        return wall, state.get("last_ts", wall), current, True
    if not previous:
        return wall, state.get("last_ts", wall), current, bool(state.get("active_command"))
    old_mono = _number(previous.get("monotonic_s"))
    if previous.get("boot_id") != boot or old_mono is None or mono < old_mono:
        return wall, state.get("last_ts", wall), current, True
    return wall, state.get("last_ts", wall) + mono - old_mono, current, False


def heatpump_grid_stop(previous, observation, source, *, clock_sample, config):
    """Frischer Netzbezug mit Entprellung; Hausanschluss wirkt sofort.

    Der Zustand ist flüchtig. Lücke, Zeitrücksprung oder Neustart verdienen die
    Frist neu. Wiederlesen derselben Proben erzeugt keine zusätzlichen Sekunden.
    Ohne frische WP-Einzelmessung zählt der Gesamtbezug abzüglich Messband.
    Ein Wechsel dieser Grundlage beginnt die Frist neu.
    """
    prev, obs, src, cfg = map(_dict, (previous, observation, source, config))
    clock = _dict(clock_sample)
    wall = _number(clock.get("wall_ts"))
    mono = _number(clock.get("monotonic_ts"))
    grid = _number(src.get("grid_w"), minimum=-math.inf)
    power = _number(obs.get("power_w"))
    grid_ts, wp_ts = _number(src.get("sample_ts")), _number(obs.get("sample_ts"))
    grid_fresh = bool(wall is not None and src.get("fresh") is True
        and grid_ts is not None and 0 <= wall - grid_ts <= 15.0 and grid is not None)
    wp_fresh = bool(wall is not None and obs.get("fresh") is True and power is not None
        and wp_ts is not None and 0 <= wall - wp_ts <= 45.0)
    house = False
    if grid_fresh:
        amps = _number(cfg.get("grid_max_amps"))
        reserve = _number(cfg.get("grid_max_reserve_amp"), 2.0)
        if amps is not None and amps > 0:
            house = grid >= max(0.0, amps - reserve) * 230.0 * 3.0
        # Dieselben vorhandenen Netzphasen und konfigurierten Grenzen.
        # Fehlende Phasen bleiben unbekannt; sie werden nicht als 0 eingesetzt.
        for phase in (1, 2, 3):
            limit = _number(cfg.get("grid_max_amps_l" + str(phase)), amps)
            phase_w = _number(src.get("grid_p" + str(phase)), minimum=-math.inf)
            if limit is not None and limit > 0 and phase_w is not None:
                house = house or phase_w >= max(0.0, limit - reserve) * 230.0
    basis = "wp_share" if wp_fresh else "grid_total"
    counted = max(0.0, grid - GRID_MEASUREMENT_BAND_W) if grid_fresh else None
    if grid_fresh and wp_fresh:
        counted = min(power, counted)
    clock_ok = bool(clock.get("valid") is True and mono is not None and clock.get("boot_id"))
    high = bool(grid_fresh and counted > GRID_STOP_THRESHOLD_W and clock_ok)
    continuous = bool(high and prev.get("high") is True
        and prev.get("basis") == basis
        and prev.get("boot_id") == clock.get("boot_id")
        and 0 <= mono - prev.get("last_mono", mono - 16.0) <= 15.0
        and grid_ts >= prev.get("grid_ts", grid_ts)
        and (not wp_fresh or wp_ts >= prev.get("wp_ts", wp_ts)))
    since = prev["since_mono"] if continuous else mono
    grid_since = prev["grid_since"] if continuous else grid_ts
    wp_since = (prev["wp_since"] if continuous else wp_ts) if wp_fresh else None
    elapsed = max(0.0, min(mono - since, grid_ts - grid_since)) if high else 0.0
    if high and wp_fresh:
        elapsed = min(elapsed, max(0.0, wp_ts - wp_since))
    debounced = bool(high and elapsed >= GRID_STOP_DEBOUNCE_S)
    return dict(stop=bool(house or debounced), house_connection_limit=bool(house),
        grid_stop=debounced, valid=grid_fresh, basis=basis, counted_w=counted, elapsed_s=elapsed,
        reason="house_connection_limit" if house
            else ("wp_grid_import_sustained" if wp_fresh
                  else "grid_import_sustained_without_wp_measurement") if debounced
            else "grid_measurement_missing_or_stale" if not grid_fresh else "grid_import_debounce" if high else "below_grid_threshold",
        high=high, since_mono=since, last_mono=mono, grid_since=grid_since,
        wp_since=wp_since, grid_ts=grid_ts, wp_ts=wp_ts, boot_id=clock.get("boot_id"))


def heatpump_ww_pv_takeover(observation, source, *, now_s):
    """Übernahme einer bereits PV-gedeckten WW-Bereitung ohne neuen Start."""
    obs, src = _dict(observation), _dict(source)
    power = _number(obs.get("power_w"))
    grid = _number(src.get("balance_grid_w"), minimum=-math.inf)
    battery = _number(src.get("balance_battery_w"), minimum=-math.inf)
    fresh = bool(obs.get("fresh") is True and _fresh(obs, now_s, 45.0)
        and src.get("fresh") is True and src.get("balance_fresh") is True
        and _fresh(src, now_s, 10.0))
    covered = bool(fresh and power is not None and power > 0
        and obs.get("compressor_running") is True and obs.get("ww_running") is True
        and grid is not None and grid <= GRID_MEASUREMENT_BAND_W
        and battery is not None and battery >= 0.0)
    # Die WP steckt bereits im Netzsaldo: genau einmal zurücknehmen. Kein
    # Sollwert, Altwert oder zweiter Verbraucherabzug ersetzt diese Messung.
    budget = max(0.0, -grid) + power if covered else None
    return dict(eligible=covered, start_budget_w=budget,
        power_w=power if fresh else None,
        reason="running_ww_pv_covered" if covered else "normal_start_required")


WW_WITHDRAWAL_SCOPE_SCHEMA = "heatpump_ww_release_withdrawal_scope_v1"


def ww_release_withdrawal_scope_contract(
    *,
    independent_safety_stop=False,
    timer_enabled=False,
    timer_target_c=None,
    live_ww_mode=None,
    live_ww_setpoint=None,
    status_valid=False,
    tolerance_c=0.5,
):
    """Begrenzt eine Freigaberücknahme auf das eigene Angebot.

    Die Rücknahme der positiven Wärmefreigabe hat den WW-Kanal bisher
    unabhängig davon abgeschaltet, wem sein aktueller Auftrag gehört. Trug der
    Kanal den Auftrag des normalen WW-Timers, stellte die Mismatch-Korrektur
    ihn Sekunden später wieder her. Jeder dieser Schreibvorgänge mit Sollwert
    löst am Gerät eine kurze Warmwasserbereitung aus.

    Gehört der Kanal erkennbar dem normalen Timer, fasst die Rücknahme ihn
    deshalb nicht an. Sie erzeugt damit keine neue Anforderung: Sie lässt
    ausschließlich unverändert, was ohnehin schon gilt.

    Eine unabhängige Safety- oder Herstellerschranke behält den harten
    Nullausgang. Ohne gültige Statusevidenz bleibt es fail-closed ebenfalls
    beim bisherigen Verhalten.
    """

    result = {
        "contract": WW_WITHDRAWAL_SCOPE_SCHEMA,
        "touch_ww": True,
        "ww_mode": 0,
        "reason": "release_owns_channel",
        "timer_target_c": _number(timer_target_c, None),
        "live_ww_mode": live_ww_mode,
        "live_ww_setpoint": _number(live_ww_setpoint, None),
    }
    if bool(independent_safety_stop):
        result["reason"] = "independent_safety_stop"
        return result
    if not bool(status_valid):
        result["reason"] = "status_not_valid"
        return result
    if not bool(timer_enabled):
        result["reason"] = "timer_disabled"
        return result
    target = _number(timer_target_c, None)
    setpoint = _number(live_ww_setpoint, None)
    if target is None or setpoint is None:
        result["reason"] = "timer_target_or_setpoint_missing"
        return result
    try:
        mode_value = int(live_ww_mode)
    except (TypeError, ValueError):
        result["reason"] = "live_mode_unreadable"
        return result
    if mode_value != 1:
        result["reason"] = "channel_not_active"
        return result
    tolerance = _number(tolerance_c, 0.5)
    if tolerance is None:
        tolerance = 0.5
    if abs(setpoint - target) > tolerance:
        result["reason"] = "setpoint_not_timer_owned"
        return result
    result["touch_ww"] = False
    result["ww_mode"] = None
    result["reason"] = "normal_timer_owns_channel"
    return result


WW_CIRCULATION_BOOST_SCHEMA = "heatpump_ww_circulation_boost_v1"


def ww_circulation_boost_contract(
    *,
    circ_boost_enabled=False,
    legacy_force_ww=False,
    pv_direct=False,
    pv_output=None,
    observation=None,
    previous=None,
    now_s=None,
):
    """Zirkulationsboost nur bei frischer WW-Bereitung, mit 10 s Einschaltentprellung.

    Ein bestätigtes Ende gibt sofort an den normalen Zeitplan zurück. Die
    erneute Einschaltentprellung verhindert Flattern. Eine fehlende frische
    Rücklesung wird bei bereits laufender Zirkulation höchstens 30 s überbrückt.
    """
    result = {
        "contract": WW_CIRCULATION_BOOST_SCHEMA,
        "force_on": False,
        "source": "",
        "reason": "circ_boost_disabled",
        "running_since_s": None,
        "missing_since_s": None,
        "sample_ts": now_s,
    }
    if not bool(circ_boost_enabled):
        return result
    if bool(legacy_force_ww):
        result["source"] = "legacy_boost"
    else:
        if not bool(pv_direct):
            result["reason"] = "no_pv_direct_path"
            return result
        output = _dict(pv_output)
        channel = _dict((output.get("channels") or {}).get("ww"))
        if not channel.get("active"):
            result["reason"] = "pv_ww_channel_inactive"
            return result
        if output.get("competing_owner"):
            result["reason"] = "competing_owner"
            return result
        if not (output.get("start") or output.get("command_outstanding")):
            result["reason"] = "pv_command_not_outstanding"
            return result
        result["source"] = "pv_contract"
    obs, prev = _dict(observation), _dict(previous)
    now = _number(now_s)
    fresh = bool(now is not None and obs.get("fresh") is True and _fresh(obs, now, 45.0))
    if not (fresh and obs.get("compressor_running") is True and obs.get("ww_running") is True):
        # Nur eine bereits laufende Boost-Zirkulation überbrückt eine Leselücke.
        # Frisch bestätigtes Ende und entzogene Freigaben haben keinen Nachlauf.
        missing_since = _number(prev.get("missing_since_s"))
        if missing_since is None:
            missing_since = now
        if (not fresh and now is not None and prev.get("force_on") is True
                and _fresh(prev, now, 45.0) and missing_since is not None
                and 0.0 <= now - missing_since < 30.0):
            result.update(force_on=True, reason="ww_readback_grace",
                          running_since_s=prev.get("running_since_s"),
                          missing_since_s=missing_since)
            return result
        result["reason"] = "fresh_ww_run_missing"
        return result
    since = _number(prev.get("running_since_s"))
    if since is None or since > now or not _fresh(prev, now, 45.0):
        since = now
    result["running_since_s"] = since
    if now - since < 10.0:
        result["reason"] = "ww_run_debounce"
        return result
    result["force_on"] = True
    result["reason"] = ("legacy_force_ww" if legacy_force_ww else
        "pv_ww_cycle_finishing" if output.get("withdraw") else "pv_ww_channel_held")
    return result


def heatpump_pv_config(cfg):
    """Alte absolute Zusage erhalten; Messwertbetrieb benötigt eine ausdrückliche Auswahl."""
    cfg = _dict(cfg)
    maximum = _number(cfg.get("wp_pv_max_power_w"), 0.0)
    mode = "measured" if str(cfg.get("wp_pv_control_mode", "")).strip().lower() == "measured" else "reserved"
    mode_valid = "wp_pv_control_mode" not in cfg or (
        isinstance(cfg["wp_pv_control_mode"], str)
        and cfg["wp_pv_control_mode"].strip().lower() in ("measured", "reserved")
    )
    start = _number(cfg.get("wp_pv_start_power_w"), 0.0)
    if start <= 0:
        try:
            threshold = abs(float(cfg.get("grid_start_limit", 0)))
        except (ValueError, TypeError, OverflowError):
            threshold = 0.0
        start = threshold if math.isfinite(threshold) and threshold > 0 else 3500.0
    if maximum > 0:
        start = min(start, maximum)
    runtime = _number(cfg.get("wp_min_runtime_min"), 30.0) * 60.0
    restart = _number(cfg.get("wp_restart_block_min"), 20.0) * 60.0
    result = {
        "max_power_w": maximum,
        "control_mode": mode,
        "start_power_w": start,
        "profile_tolerance_w": max(100.0, maximum * 0.05) if mode == "measured" else 1.0,
        "min_runtime_s": runtime,
        "restart_delay_s": restart,
        "reaction_s": _number(cfg.get("wp_pv_reaction_s"), 30.0),
        "qualification_s": _number(cfg.get("pv_boost_delay"), 30.0),
        "signal_hold_s": 600.0,
        # Dies ist eine EMS-Wartefrist, keine behauptete Herstellergrenze.
        "start_wait_s": _number(cfg.get("wp_pv_start_wait_s"), 600.0),
        "handoff_timeout_s": _number(cfg.get("wp_pv_handoff_timeout_s"), 120.0),
        "command_timeout_s": 25.0,
        # Sollwerthysterese der Anlage (HZ/WW): Die Wärmepumpe entscheidet den
        # Verdichterstart selbst. E3DC-Control spiegelt nur ihre Schwellen, um
        # Bedarf und Startreservierung vorherzusagen (Standard:
        # Heizung 3,5 K, Warmwasser 8 K).
        "hz_hysteresis_k": _number(cfg.get("wp_pv_hz_hysteresis_k"), 3.5),
        "ww_hysteresis_k": _number(cfg.get("wp_pv_ww_hysteresis_k"), 8.0),
        # Der Boost steht wie eine SG-Ready-Freigabe, bis die PV-Deckung
        # so lange fehlt. Kurze Wolken entziehen den Sollwert nicht mehr.
        "boost_release_s": max(30.0, _number(cfg.get("wp_pv_boost_release_s"), 300.0)),
        "freshness_s": 45.0,
        "window_s": 86400.0,
        "wallbox_grid_stop_wh": max(5.0, min(40.0, _number(cfg.get("wb_min_current_import_stop_wh"), 40.0))),
        "wallbox_grid_band_w": max(0.0, _number(cfg.get("wb_min_current_import_tolerance_w"), 200.0, minimum=-math.inf)),
        "wallbox_grid_leak_w": max(0.0, _number(cfg.get("wb_min_current_import_release_w"), 80.0, minimum=-math.inf)),
        "wallbox_confirm_s": max(30.0, _number(cfg.get("wb_phase_confirm_timeout_s"), 240.0)),
        "battery_limit_wh": _number(cfg.get("wp_pv_battery_limit_wh"), 0.0),
        "grid_limit_wh": _number(cfg.get("wp_pv_grid_limit_wh"), 0.0),
        "battery_max_w": _number(cfg.get("wp_pv_battery_max_w"), 0.0),
        "grid_max_w": _number(cfg.get("wp_pv_grid_max_w"), 0.0),
    }
    result["valid"] = bool(
        mode_valid and (maximum > 0 or mode == "measured") and start > 0
        and runtime > 0 and result["reaction_s"] > 0
        and result["start_wait_s"] >= result["signal_hold_s"]
        and result["handoff_timeout_s"] > 0
    )
    for name in ("wp_pv_max_power_w", "wp_min_runtime_min", "wp_restart_block_min",
                 "wp_pv_reaction_s", "wp_pv_start_wait_s", "wp_pv_handoff_timeout_s",
                 "wp_pv_battery_limit_wh", "wp_pv_grid_limit_wh",
                 "wp_pv_battery_max_w", "wp_pv_grid_max_w", "wp_pv_start_power_w",
                 "wp_pv_hz_hysteresis_k", "wp_pv_ww_hysteresis_k", "wp_pv_boost_release_s"):
        if name in cfg and _number(cfg[name]) is None:
            result["valid"] = False
    result["reserve_duration_s"] = (
        result["reaction_s"] if mode == "measured"
        else runtime + result["start_wait_s"] + result["reaction_s"]
    )
    return result


def _identity(demand):
    demand = _dict(demand)
    identity = demand.get("request_id")
    revision = demand.get("revision")
    if not isinstance(identity, str) or not 1 <= len(identity) <= 160:
        return None
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return None
    return identity, revision


def _fresh(sample, now_s, ttl):
    timestamp = _number(_dict(sample).get("sample_ts"))
    return bool(timestamp is not None and 0 <= now_s - timestamp <= ttl)


def _channels_valid(demand):
    channels = demand.get("channels")
    if not isinstance(channels, dict) or set(channels) != {"hz", "ww"}:
        return False
    active = False
    for channel in channels.values():
        if not isinstance(channel, dict) or type(channel.get("active")) is not bool:
            return False
        if channel["active"]:
            if _number(channel.get("target_c")) is None:
                return False
            active = True
    return type(demand.get("requested")) is bool and (not demand["requested"] or active)


def qualify_heatpump_pv_demand(previous, demand, prospective_capacity_w, *, now_s, delay_s):
    """PV vor dem verbindlichen Startangebot qualifizieren, ohne dessen Uhr zu verbrauchen."""
    demand, previous = _dict(demand), _dict(previous)
    now = _number(now_s)
    identity = _identity(demand)
    required = _number(demand.get("request_w"), 0.0)
    capacity = _number(prospective_capacity_w, 0.0)
    valid = bool(now is not None and identity and demand.get("requested") is True
                 and required > 0 and capacity >= required)
    since = _number(previous.get("qualified_since_s"))
    previous_ts = _number(previous.get("sample_ts"))
    same = identity is not None and identity == _identity(previous)
    if not valid or not same or since is None or since > now or (
        previous_ts is not None and (now < previous_ts or now - previous_ts > 45.0)
    ):
        since = now if valid else None
    return {
        "request_id": identity[0] if identity else "",
        "revision": identity[1] if identity else 0,
        "sample_ts": now,
        "qualified_since_s": since,
        "qualified": bool(valid and since is not None
                          and now - since >= _number(delay_s, 30.0)),
    }


def validate_heatpump_pv_state(state):
    """Ungültige Konten niemals in einen leeren, erneut verfügbaren Topf umwandeln."""
    if not isinstance(state, dict) or state.get("schema") != STATE_SCHEMA:
        return {}
    result = copy.deepcopy(state)
    for key in (
        "last_ts", "battery_used_wh", "grid_used_wh", "battery_reserved_wh",
        "grid_reserved_wh", "last_battery_w", "last_grid_w", "max_power_w",
    ):
        if _number(result.get(key)) is None:
            return {}
        result[key] = _number(result[key])
    for key in ("running_since_s", "last_stop_s", "issued_ts", "offered_ts"):
        if key not in result:
            return {}
        if result.get(key) is not None and _number(result[key]) is None:
            return {}
        if result[key] is not None:
            result[key] = _number(result[key])
    for key in ("active_command", "cycle_owned", "withdrawal_pending", "signal_withdrawn"):
        if type(result.get(key)) is not bool:
            return {}
    if not isinstance(result.get("request_id"), str) or type(result.get("revision")) is not int:
        return {}
    entries = result.get("energy_window")
    if not isinstance(entries, list) or len(entries) > 1500:
        return {}
    last = -1.0
    for entry in entries:
        if not isinstance(entry, dict):
            return {}
        if any(_number(entry.get(key)) is None for key in ("until_s", "battery_wh", "grid_wh")):
            return {}
        for key in ("until_s", "battery_wh", "grid_wh"):
            entry[key] = _number(entry[key])
        for key in ("battery_measured_wh", "grid_measured_wh"):
            if key in entry:
                value = _number(entry[key])
                if value is None or value > entry[key.replace("_measured", "")] + 1e-6:
                    return {}
                entry[key] = value
        if entry["until_s"] < last:
            return {}
        last = entry["until_s"]
    if result.get("compressor_running") is not None and type(result.get("compressor_running")) is not bool:
        return {}
    if "quarantine_remaining_s" in result:
        remaining = _number(result["quarantine_remaining_s"])
        if remaining is None:
            return {}
        result["quarantine_remaining_s"] = remaining
    if "energy_guard_pending" in result and type(result["energy_guard_pending"]) is not bool:
        return {}
    if "energy_guard_sources" in result and (
        not isinstance(result["energy_guard_sources"], list)
        or any(source not in ("battery", "grid") for source in result["energy_guard_sources"])
    ):
        return {}
    if "withdrawal_reason" in result and not isinstance(result["withdrawal_reason"], str):
        return {}
    for key in ("request_withdrawn_since_s", "request_returned_since_s", "first_withdrawal_ts", "withdrawal_healed_ts", "wallbox_grid_defer_since_s", "reservation_started_ts", "start_reservation_released_ts", "pv_unqualified_since_s", "pv_covered_since_s"):
        if key in result and result[key] is not None:
            if _number(result[key]) is None:
                return {}
            result[key] = _number(result[key])
    if "start_reservation_released" in result and type(result["start_reservation_released"]) is not bool:
        return {}
    for key in ("last_grid_measured_w", "last_battery_measured_w", "wallbox_grid_defer_wh", "last_valid_power_w", "last_valid_power_ts", "last_allocated_w"):
        if key in result and result[key] is not None and _number(result[key]) is None:
            return {}
    if "handoff_required" in result and type(result["handoff_required"]) is not bool:
        return {}
    if "withdrawal_identity" in result and _identity(result["withdrawal_identity"]) is None:
        return {}
    return result


def _clear_start_reservation_release(state):
    """Reservierungsstatus nach abgeschlossenem Auftrag zurücksetzen."""
    state["start_reservation_released"] = False
    state["start_reservation_released_ts"] = None
    state["reservation_started_ts"] = None


def _empty_state(now, maximum):
    return {
        "schema": STATE_SCHEMA, "last_ts": now, "max_power_w": maximum,
        "request_id": "", "revision": 0, "running_since_s": None,
        "last_stop_s": None, "compressor_running": None,
        "issued_ts": None, "offered_ts": None, "active_command": False,
        "cycle_owned": False, "signal_withdrawn": False,
        "battery_used_wh": 0.0, "grid_used_wh": 0.0,
        "battery_reserved_wh": 0.0, "grid_reserved_wh": 0.0,
        "last_battery_w": 0.0, "last_grid_w": 0.0,
        "energy_window": [], "withdrawal_pending": False,
        "quarantine_remaining_s": 0.0,
        "reservation_started_ts": None, "start_reservation_released": False,
        "start_reservation_released_ts": None, "pv_unqualified_since_s": None, "pv_covered_since_s": None,
    }


def _record_energy(state, now, battery_wh, grid_wh, window_s, *,
                   battery_measured_wh=0.0, grid_measured_wh=0.0):
    for source, increment in (("battery", battery_wh), ("grid", grid_wh)):
        state[source + "_used_wh"] += increment
        state[source + "_reserved_wh"] = max(0.0, state[source + "_reserved_wh"] - increment)
    # Minutenweise konservativ am Intervallende ausbuchen; kein Reset um Mitternacht.
    entries = [entry for entry in state["energy_window"] if entry["until_s"] > now - window_s]
    until = math.ceil(now / 60.0) * 60.0
    if battery_wh or grid_wh:
        if entries and entries[-1]["until_s"] == until:
            entries[-1]["battery_wh"] += battery_wh
            entries[-1]["grid_wh"] += grid_wh
        else:
            entries.append({"until_s": until, "battery_wh": battery_wh, "grid_wh": grid_wh})
        for name, increment in (("battery", battery_measured_wh), ("grid", grid_measured_wh)):
            key = name + "_measured_wh"
            entries[-1][key] = entries[-1].get(key, 0.0) + min(increment, battery_wh if name == "battery" else grid_wh)
    state["energy_window"] = entries


def _reserve_energy(power, duration, battery_w, battery_wh, grid_w, grid_wh):
    """Deckung eines konstanten ungünstigsten Leistungsbedarfs über die gesamte Frist."""
    hours = duration / 3600.0
    if hours <= 0 or power <= 0 or battery_w + grid_w < power:
        return None
    battery = min(battery_wh, battery_w * hours, power * hours)
    grid = max(0.0, power * hours - battery)
    if grid > min(grid_wh, grid_w * hours) + 1e-6:
        return None
    return battery, grid


def curve_forecast_hold_contract(plan, *, now_s, horizon_end_s, soc, capacity_kwh,
                                 power_w, config, target_soc=None, max_charge_w=None):
    """Vorsichtige Restenergie aus derselben Quelle wie das Tariffenster.

    Nur Halteevidenz, keine Speicher- oder Startfreigabe. Haus und Wallbox
    werden ausschließlich im gemeinsamen Prognosevertrag abgezogen.
    """
    try:
        from .Heat.tariff_shift import forecast_contract, number, TARIFF_TIMEZONE
    except ImportError:
        from Heat.tariff_shift import forecast_contract, number, TARIFF_TIMEZONE
    from datetime import datetime

    result = dict(valid=False, covers=False, reason="curve_forecast_missing",
                  pv_cover_kwh=None, battery_need_kwh=None, boost_need_kwh=None,
                  heat_need_kwh=None, reserve_kwh=None, pv_method=None, load_method=None,
                  horizon_end_ts=horizon_end_s)
    plan = _dict(plan)
    # Nicht endliche Werte dürfen weder Revisionen hashen noch Deckung liefern.
    def finite_tree(value):
        if isinstance(value, dict):
            return all(finite_tree(v) for v in value.values())
        if isinstance(value, (list, tuple)):
            return all(finite_tree(v) for v in value)
        if isinstance(value, (int, float, str)) and not isinstance(value, bool):
            try:
                return math.isfinite(float(value))
            except ValueError:
                return True  # Textuelle Plan- und Revisionskennungen sind erlaubt.
            except OverflowError:
                return False
        return True
    if not finite_tree((plan, now_s, horizon_end_s, soc, capacity_kwh, power_w, config, target_soc)):
        return {**result, "horizon_end_ts": None, "reason": "curve_forecast_nonfinite"}
    target = number(target_soc)
    if target_soc is None:
        targets = [number(plan.get(k)) for k in
                   ("effective_target_soc", "planning_target_soc", "target_soc")]
        target = max((v for v in targets if v is not None), default=None)
    end, current_soc, capacity = map(number, (horizon_end_s, soc, capacity_kwh))
    power = number(power_w)
    runtime = number(config.get("min_runtime_s"))
    if (end is None or not now_s < end <= now_s + 86400
            or datetime.fromtimestamp(end, TARIFF_TIMEZONE).date() != datetime.fromtimestamp(now_s, TARIFF_TIMEZONE).date()
            or target is None or not 0 <= target <= 100
            or current_soc is None or not 0 <= current_soc <= 100
            or capacity is None or capacity <= 0
            or power is None or power <= 0 or runtime is None or runtime <= 0):
        return {**result, "reason": "curve_or_boost_evidence_invalid"}
    try:
        from .Heat.plan_forecast import project_plan_forecast
    except ImportError:
        from Heat.plan_forecast import project_plan_forecast
    charge_limit = number(max_charge_w)
    if charge_limit is None or charge_limit < 0:
        return {**result, "reason": "curve_charge_limit_missing"}
    forecast = forecast_contract(project_plan_forecast(plan), now_s, end,
                                 max_absorption_w=charge_limit + power)
    if not forecast["valid"]:
        return {**result, "reason": forecast["reason"]}
    battery = max(0., target - current_soc) * capacity / 100.
    # Ohne temperaturgebundene Restlaufschätzung gilt eine volle Mindestlaufzeit
    # mit mindestens der konfigurierten Start-/Maximalleistung. Die kanonische
    # Wärmeprognose enthält dieselbe WP: niemals beide Energiemengen addieren.
    boost = power * runtime / 3600000.
    heat = max(boost, forecast["need_kwh"])
    need = battery + heat
    # Die Wolkenüberbrückung darf die Sicherheitsreserve nicht aufbrauchen.
    reserve = max(.5, .10 * need) + power * min(300.0, _number(config.get("boost_release_s"), 300.0)) / 3600000.
    covers = forecast["pv_cover_kwh"] >= need + reserve
    return {**result, "valid": True, "covers": covers,
            "reason": "forecast_covers_curve" if covers else "forecast_insufficient",
            "pv_cover_kwh": forecast["pv_cover_kwh"], "battery_need_kwh": battery,
            "boost_need_kwh": boost, "boost_method": "power_over_full_minimum_runtime",
            "heat_need_kwh": heat, "reserve_kwh": reserve,
            "pv_method": forecast["pv_method"], "load_method": forecast["load_method"],
            "revision": forecast["revision"],
            "target_soc": target, "soc": current_soc, "capacity_kwh": capacity}


def _wallbox_grid_wait(state, evidence, *, wall, now, cfg, enabled, grid_onset=False):
    """Begrenztes Warten je Ladepunkt; nur ein bestätigtes Ende erneuert die Episode.

    Der Anzeigevertrag exportiert keine interne Kontogeneration. Die lokale
    Generation zählt deshalb ausschließlich frisch belegte Ladeenden. Weder
    ein neuer WP-Auftrag noch eine Beleglücke oder ein Phasenwechsel setzt sie zurück.
    """
    episodes = _dict(state.get("wallbox_grid_episodes"))
    state["wallbox_grid_episodes"] = episodes
    fresh = _fresh(evidence, wall, _number(cfg.get("freshness_s"), 45.0))
    if fresh:
        for item in evidence.get("ended_owners") or []:
            if not isinstance(item, dict):
                continue
            key = str(item.get("owner_wb_id"))
            episode = _dict(episodes.get(key))
            stamp = _number(item.get("sample_ts"))
            if (episode and not episode.get("ended") and stamp is not None
                    and stamp > _number(episode.get("last_positive_wall_s"), math.inf)):
                episode["ended"] = True
                episode["end_wall_s"] = stamp
    owner = evidence.get("owner_wb_id") if fresh else None
    key = str(owner) if owner is not None else str(state.get("wallbox_grid_last_owner"))
    episode = _dict(episodes.get(key))
    positive = bool(fresh and owner is not None and (
        evidence.get("active") is True or grid_onset and evidence.get("onset_ready") is True))
    if positive:
        if not episode or episode.get("ended"):
            generation = int(_number(episode.get("generation"), 0.0)) + 1
            episode = {"owner_wb_id": owner, "generation": generation, "ended": False,
                       "since_s": now, "account_stage": "", "last_used_wh": 0.0,
                       "progress_wh": 0.0, "last_progress_s": now}
            episodes[key] = episode
        state["wallbox_grid_last_owner"] = owner
        episode["last_positive_wall_s"] = _number(evidence.get("sample_ts"), wall)
        stage = str(evidence.get("stage") or "")
        used = _number(evidence.get("used_wh"))
        threshold = _number(cfg.get("wallbox_grid_stop_wh"), 40.0)
        # Fortschritt folgt dem echten Konto; keine zweite Netzenergiebuchung.
        account_stage = "three_phase" if stage in ("three_phase_min_watch", "phase_down_pending") else "one_phase"
        changed_stage = account_stage != episode.get("account_stage")
        if changed_stage or used is not None and used > _number(episode.get("last_used_wh"), 0.0):
            if used is not None:
                delta = used if changed_stage else max(0.0, used - _number(episode.get("last_used_wh"), 0.0))
                episode["progress_wh"] = min(2.0 * threshold,
                    _number(episode.get("progress_wh"), 0.0) + delta)
            episode["last_progress_s"] = now
        episode["account_stage"] = account_stage
        episode["last_used_wh"] = used
        pending = stage in ("phase_down_pending", "stop_pending")
        confirm_s = _number(cfg.get("wallbox_confirm_s"), 240.0)
        rate = _number(evidence.get("deficit_w"))
        rate = max(0.0, rate - _number(cfg.get("wallbox_grid_leak_w"), 80.0)) if rate is not None else None
        left = max(0.0, threshold - used) if used is not None else threshold
        if pending:
            duration = confirm_s
        elif rate is not None and rate > 0:
            duration = left * 3600.0 / rate + confirm_s
        else:
            duration = 60.0
        episode["progress_deadline_s"] = _number(episode.get("last_progress_s"), now) + duration
    since = _number(episode.get("since_s"))
    last = _number(episode.get("last_positive_wall_s"))
    # Ein frischer Ausschluss oder eine Stufe über Mindeststrom beendet den
    # Nachlauf; reine Lücken belegen weder Ende noch eine neue Generation.
    excluded = bool(fresh and evidence.get("eligible") is False)
    recent = bool(last is not None and 0 <= wall - last <= 60.0)
    bounded = bool(since is not None and 0 <= now - since < 1800.0
                   and now <= _number(episode.get("progress_deadline_s"), now))
    waiting = bool(enabled and episode and not episode.get("ended")
                   and not excluded and recent and bounded)
    state["wallbox_grid_defer_since_s"] = since
    state["wallbox_grid_defer_wh"] = _number(episode.get("progress_wh"), 0.0)
    reason = ("Wallbox im Netzhalt" if waiting else
              "Wallbox-Rückstellungsgrenze erreicht" if episode and not bounded else "")
    return waiting, reason


def evaluate_heatpump_pv_request(demand, observation, previous, source, *, clock_sample, config):
    """Vor der Verteilung: einen WP-Verbraucher und getrennte Energieansprüche bilden.

    ``battery_actual_w`` und ``grid_actual_w`` sind bereits zentral der WP
    zugeordnete Messanteile, keine Summen vom Hausanschluss. Während Messlücken
    wird eine konservative, als unsicher gekennzeichnete Belastung verwendet.
    """
    demand, observation, source = _dict(demand), _dict(observation), _dict(source)
    cfg = _dict(config)
    if "max_power_w" not in cfg:
        cfg = heatpump_pv_config(cfg)
    maximum = _number(cfg.get("max_power_w"), 0.0)
    measured = cfg.get("control_mode") == "measured"
    start_power = _number(cfg.get("start_power_w"), maximum or 3500.0)
    restored = validate_heatpump_pv_state(previous)
    corrupt = bool(previous) and not restored
    wall, now, current_clock, clock_uncertain = _clock(clock_sample, restored)
    state = restored or _empty_state(now, maximum)
    # Alte Checkpoints haben noch keinen eigenen Identitätsanker der Vormerkung.
    # Vor dem unten erlaubten Sollwertwechsel an den bisherigen Auftrag binden.
    if (state.get("withdrawal_pending") and state.get("withdrawal_reason") == "request_withdrawn"
            and "withdrawal_identity" not in state):
        state["withdrawal_identity"] = {"request_id": state["request_id"], "revision": state["revision"]}
    if measured:
        # Übernommene Reservierungen sind keine tatsächlich verbrauchte Energie.
        state["battery_reserved_wh"] = state["grid_reserved_wh"] = 0.0
    # Alte Zuteilungen sind keine Messung und werden nicht wiederverwendet.
    state.pop("last_allocated_w", None)
    blockers = []
    identity = _identity(demand)
    ttl = _number(cfg.get("freshness_s"), 45.0)
    data_fresh = bool(observation.get("fresh") is True and _fresh(observation, wall, ttl))
    source_fresh = bool(source.get("fresh") is True and _fresh(source, wall, ttl))
    demand_fresh = bool(demand.get("schema") == DEMAND_SCHEMA and _fresh(demand, wall, ttl))
    backwards = now < state["last_ts"]
    dt = max(0.0, now - state["last_ts"])
    clock_ok = not backwards and not clock_uncertain and now > 0
    if backwards:
        blockers.append("clock_regression")
    if clock_uncertain:
        blockers.append("clock_epoch_unconfirmed")
    if corrupt:
        blockers.append("energy_state_invalid")
        state["uncertain_state"] = True
        state["quarantine_remaining_s"] = _number(cfg.get("window_s"), 86400.0)
    elif state.get("uncertain_state") and clock_ok:
        state["quarantine_remaining_s"] = max(0.0, _number(state.get("quarantine_remaining_s"), 86400.0) - dt)
        if (state["quarantine_remaining_s"] <= 0 and data_fresh and source_fresh
                and observation.get("compressor_running") is False
                and not state.get("cycle_owned") and not state.get("active_command")):
            state["uncertain_state"] = False
    if state.get("uncertain_state"):
        blockers.append("energy_state_requires_reconciliation")
    if cfg.get("valid") is not True:
        blockers.append("electrical_profile_missing_or_invalid")
    if not demand_fresh or not identity or not _channels_valid(demand):
        blockers.append("demand_invalid_or_stale")
    if not data_fresh or not source_fresh:
        blockers.append("measurement_invalid_or_stale")
    command = _dict(demand.get("command"))
    command_identity = _identity(command)
    bound_command = identity is not None and command_identity == identity
    issued = (_number(command.get("issued_ts"), _number(command.get("prepared_ts")))
              if bound_command else None)
    closed_command = _dict(state.get("closed_command"))
    already_closed = bool(bound_command and closed_command.get("request_id") == command.get("request_id")
                          and closed_command.get("revision") == command.get("revision")
                          and closed_command.get("issued_ts") == issued)
    if issued is not None and issued > 0 and issued <= wall and not already_closed:
        if not restored and command.get("withdrawal_confirmed") is not True:
            state["uncertain_state"] = True
            state["quarantine_remaining_s"] = _number(cfg.get("window_s"), 86400.0)
            blockers.append("active_command_without_energy_checkpoint")
            # Die fehlende Historie darf kein neues 24-h-Kontingent erzeugen.
            _record_energy(state, now, _number(cfg.get("battery_limit_wh"), 0.0),
                           _number(cfg.get("grid_limit_wh"), 0.0),
                           _number(cfg.get("window_s"), 86400.0))
        if not state["cycle_owned"]:
            state["issued_ts"] = now - min(ttl, max(0.0, wall - issued))
            state["issued_wall_s"] = issued
            state["cycle_owned"] = True
            state["signal_withdrawn"] = False
        if not state["signal_withdrawn"]:
            state["active_command"] = True
    withdrawn = bool(bound_command and command.get("withdrawal_confirmed") is True and data_fresh)
    active_before = state["cycle_owned"]
    physical = observation.get("compressor_running") if data_fresh else None
    if physical is not True and physical is not False:
        physical = None
    observed_w = _number(observation.get("power_w")) if data_fresh else None
    measured_observation_valid = bool(observed_w is not None and physical is not None)
    if measured and not measured_observation_valid:
        blockers.append("power_or_compressor_observation_missing")
    # Der Originalzeitstempel überlebt einen Neustart. Weder Wiederlesen noch
    # Halten verjüngt die Messung; ein ungültiger Wert ersetzt keinen gültigen.
    if measured and measured_observation_valid:
        state["last_valid_power_w"] = observed_w
        state["last_valid_power_ts"] = _number(observation.get("sample_ts"))
    last_power = _number(state.get("last_valid_power_w"))
    last_power_ts = _number(state.get("last_valid_power_ts"))
    power_age = wall - last_power_ts if last_power_ts is not None and wall >= last_power_ts else None
    # Nicht länger als die bestehende Defizitfrist, absolut höchstens 300 s.
    power_hold_s = min(300.0, _number(cfg.get("boost_release_s"), 300.0))
    power_source = "measured" if measured_observation_valid else (
        "held_last_valid" if last_power is not None and power_age is not None
        and power_age <= power_hold_s and not clock_uncertain else "unknown")
    accounted_power = (observed_w if power_source == "measured" else
                       last_power if power_source == "held_last_valid" else None)
    balance_grid = _number(source.get("balance_grid_w"), minimum=-math.inf)
    balance_battery = _number(source.get("balance_battery_w"), minimum=-math.inf)
    balance_fresh = bool(source.get("balance_fresh") is True and _fresh(source, wall, 10.0)
                         and balance_grid is not None and balance_battery is not None)
    takeover = heatpump_ww_pv_takeover(observation, source, now_s=wall)
    takeover_active = bool(measured and takeover["eligible"] and not state["cycle_owned"]
        and _dict(_dict(demand.get("channels")).get("ww")).get("active") is True
        and not _dict(_dict(demand.get("channels")).get("hz")).get("active"))
    if takeover_active:
        start_power = observed_w
    # Rechteckintegration mit dem vorigen belegten Quellenwert; Ausfälle sind
    # ausdrücklich keine kostenlose Energie und dürfen keinen neuen Topf öffnen.
    gap = bool(source.get("restart") is True or dt > ttl or not source_fresh or clock_uncertain)
    short_source_gap = bool(not source_fresh and dt <= ttl
                            and source.get("restart") is not True and not clock_uncertain)
    measured_interval = bool(measured and active_before and not gap and clock_ok
                             and measured_observation_valid and balance_fresh)
    wb = _dict(demand.get("wallbox_grid_hold"))
    wb_fresh = _fresh(wb, wall, _number(cfg.get("freshness_s"), 45.0))
    episode = _dict(_dict(state.get("wallbox_grid_episodes")).get(
        str(state.get("wallbox_grid_last_owner"))))
    last_positive = _number(episode.get("last_positive_wall_s"))
    # Das Leistungsband für die Wallbox bleibt 200 W. Nur die Netzbuchung
    # übernimmt während ihres belegten Mindesthalts Toleranz plus Leck.
    wb_minimum_hold = bool(
        wb_fresh and (wb.get("active") is True or wb.get("onset_ready") is True)
        or state.get("wallbox_waiting_previous") and not episode.get("ended")
        and not (wb_fresh and wb.get("eligible") is False)
        and last_positive is not None and 0 <= wall - last_positive <= 60.0)
    grid_band_w = (max(GRID_MEASUREMENT_BAND_W,
                       _number(cfg.get("wallbox_grid_band_w"), 200.0)
                       + _number(cfg.get("wallbox_grid_leak_w"), 80.0))
                   if wb_minimum_hold else GRID_MEASUREMENT_BAND_W)
    grid_counted_w = (min(_number(source.get("grid_actual_w"), 0.0), observed_w,
                         max(0.0, balance_grid - grid_band_w))
                      if measured_interval else None)
    battery_counted_w = (min(_number(source.get("battery_actual_w"), 0.0), observed_w,
                            max(0.0, -balance_battery)) if measured_interval else None)
    grid_measured_delta = (_number(state.get("last_grid_measured_w"), 0.0) * dt / 3600.0
                           if measured_interval else 0.0)
    battery_measured_delta = (_number(state.get("last_battery_measured_w"), 0.0) * dt / 3600.0
                              if measured_interval else 0.0)
    battery_delta = state["last_battery_w"] * dt / 3600.0
    grid_delta = state["last_grid_w"] * dt / 3600.0
    state["measurement_gap_charged"] = False
    state["measurement_gap_battery_wh"] = 0.0
    state["measurement_gap_grid_wh"] = 0.0
    state["measurement_gap_reason"] = "no_gap"
    if gap and active_before:
        battery_bound = min(maximum or state["max_power_w"], _number(cfg.get("battery_max_w"), 0.0))
        grid_bound = min(maximum or state["max_power_w"], _number(cfg.get("grid_max_w"), 0.0))
        if measured and not measured_observation_valid and balance_fresh:
            # Die gemessene Gesamtentnahme begrenzt auch einen unbekannten
            # WP-Anteil. Bei Überschuss kein fiktiver Kontingentverbrauch.
            battery_bound = min(battery_bound, max(0.0, -balance_battery))
            grid_bound = min(grid_bound, max(0.0, balance_grid))
        # Getrennte obere Schranken sind bei unbekannter Aufteilung erforderlich;
        # sie werden als unsichere Belastung und niemals als Messwert ausgegeben.
        battery_delta = max(battery_delta, battery_bound * dt / 3600.0)
        grid_delta = max(grid_delta, grid_bound * dt / 3600.0)
        if clock_uncertain:
            # Unbekannte Bootlücke kann mit Werten auf der Wanduhr nicht bewiesen
            # werden. Kontingente bis zur Klärung konservativ vollständig belasten.
            battery_delta = max(battery_delta, _number(cfg.get("battery_limit_wh"), 0.0))
            grid_delta = max(grid_delta, _number(cfg.get("grid_limit_wh"), 0.0))
        gap_reason = "unknown_wp_power_conservative"
        if measured and measured_observation_valid and short_source_gap:
            # Eine gültige WP-Messung begrenzt auch bei fehlendem Quellenanteil
            # die Buchung. Die WP steckt bereits in der gemessenen Bilanz.
            battery_bound = min(battery_bound, observed_w)
            grid_bound = min(grid_bound, observed_w)
            if balance_fresh:
                battery_bound = min(battery_bound, max(0.0, -balance_battery))
                grid_bound = min(grid_bound, max(0.0, balance_grid))
                gap_reason = "measured_wp_and_balance"
            else:
                gap_reason = "measured_wp_balance_missing"
            # Beide Quellen zusammen dürfen dieselbe WP-Aufnahme nur einmal buchen.
            grid_bound = min(grid_bound, max(0.0, observed_w - battery_bound))
            battery_delta = battery_bound * dt / 3600.0
            grid_delta = grid_bound * dt / 3600.0
            if (balance_fresh and not source.get("restart") and dt <= ttl
                    and not source_fresh):
                gap_reason = "source_gap_wp_and_balance_valid"
        elif clock_uncertain:
            gap_reason = "clock_epoch_unconfirmed"
        elif source.get("restart") is True:
            gap_reason = "restart_interval_unmeasured"
        elif dt > ttl:
            gap_reason = "long_interval_unmeasured"
        state["measurement_gap_charged"] = bool(battery_delta or grid_delta)
        state["measurement_gap_battery_wh"] = battery_delta
        state["measurement_gap_grid_wh"] = grid_delta
        state["measurement_gap_reason"] = gap_reason
    _record_energy(state, max(now, state["last_ts"]), battery_delta, grid_delta,
                   _number(cfg.get("window_s"), 86400.0),
                   battery_measured_wh=battery_measured_delta, grid_measured_wh=grid_measured_delta)
    compressor_stopped = bool(physical is False and state["compressor_running"] is True)
    if measured and compressor_stopped and state["active_command"]:
        state["stop_recheck_since_s"] = now
        state["stop_recheck_wall_s"] = wall
    if physical is True:
        state.pop("stop_recheck_since_s", None)
        state.pop("stop_recheck_wall_s", None)
        if state["running_since_s"] is None:
            state["running_since_s"] = now
    elif physical is False:
        if state["compressor_running"] is True:
            state["last_stop_s"] = now
            # Im Messwertbetrieb ist der Verdichterstopp kein Ende des
            # Boosts. Der Sollwert bleibt stehen, die Anlage startet nach
            # eigener Hysterese erneut; nur neue Startreservierungen warten die
            # Wiedereinschaltsperre ab. Der Reservierungsbetrieb entzieht weiter.
            if state["cycle_owned"] and not measured:
                state["withdrawal_pending"] = True
        state["running_since_s"] = None
    if physical is not None:
        state["compressor_running"] = physical
    if withdrawn:
        state["active_command"] = False
        state["signal_withdrawn"] = True
        state["withdrawal_pending"] = False
        state["issued_ts"] = None
        state["offered_ts"] = None
        if physical is False:
            state["battery_reserved_wh"] = state["grid_reserved_wh"] = 0.0
            state["cycle_owned"] = False
    if state["signal_withdrawn"] and physical is False:
        state["cycle_owned"] = False
        state["battery_reserved_wh"] = state["grid_reserved_wh"] = 0.0
        if measured:
            state["energy_guard_pending"] = False
            state["energy_guard_sources"] = []
        if bound_command:
            state["closed_command"] = {
                "request_id": command["request_id"], "revision": command["revision"],
                "issued_ts": issued,
            }
        _clear_start_reservation_release(state)
    runtime = _number(cfg.get("min_runtime_s"), 1800.0)
    running_since = state["running_since_s"]
    remaining = max(0.0, running_since + runtime - now) if running_since is not None else 0.0
    # Unbekannt ist kein bestätigter Stopp. Ein bestehender Zeitanker bleibt stehen.
    if not data_fresh and state["compressor_running"] is True and remaining <= 0:
        blockers.append("running_observation_unconfirmed")
    signal_remaining = max(0.0, state["issued_ts"] + _number(cfg.get("signal_hold_s"), 600.0) - now) if state["issued_ts"] is not None else 0.0
    protected = max(remaining, signal_remaining)
    protection = source.get("protection_reason", "")
    if protection not in HARD_PROTECTIONS:
        protection = demand.get("protection_reason", "")
    protection = protection if protection in HARD_PROTECTIONS else ""
    # Die Luxtronik-Aufnahme wird in 100-W-Schritten gemeldet. Im expliziten
    # Messwertbetrieb vermeidet ein Projektband von mindestens 100 W bzw. 5 %
    # Scheingenauigkeit des Profilwerts; reale Quellenlimits bleiben unverändert.
    profile_tolerance = max(100.0, maximum * 0.05) if measured else 1.0
    profile_excess = max(0.0, observed_w - maximum) if observed_w is not None and maximum > 0 else None
    if observed_w is not None and maximum > 0 and observed_w > maximum + profile_tolerance:
        protection = "electrical_profile_exceeded"
    source_enabled = {
        name: _number(cfg.get(name + "_max_w"), 0.0) > 0
        and _number(cfg.get(name + "_limit_wh"), 0.0) > 0
        for name in ("battery", "grid")
    }
    if measured and state["cycle_owned"] and not protection:
        # Die Akkusperre bleibt unverändert. Für das Netz zählt nur der
        # aktuelle gültige WP-Anteil oberhalb desselben Messbandes.
        battery_disabled = bool(not source_enabled["battery"] and (
            state["battery_reserved_wh"] > 0 or state["last_battery_w"] > 0))
        grid_disabled = bool(not source_enabled["grid"] and grid_counted_w is not None
                             and grid_counted_w > 0)
        if battery_disabled or grid_disabled:
            protection = "bridge_source_disabled"
    if protection:
        blockers.append(protection)
    state["max_power_w"] = max(maximum, state["max_power_w"]) if state["active_command"] else maximum
    if measured and maximum <= 0:
        # Ohne belegtes Gerätemaximum bleiben Startschätzung und beobachtete
        # Aufnahme getrennt von einer behaupteten elektrischen Gerätegrenze.
        state["max_power_w"] = max(state["max_power_w"], start_power, observed_w or 0.0)
    requested = demand.get("requested") is True and demand_fresh and identity is not None and _channels_valid(demand)
    qualified = requested and demand.get("qualified") is True
    reclaim_w = _number(source.get("deficit_wallbox_reclaim_w")) if balance_fresh else None
    balance_without_wallbox = None
    if reclaim_w is not None and reclaim_w > 0.0:
        # Dieselbe gemessene Leistung nur einmal zurücknehmen: zuerst aus
        # dem Netzbezug, der Rest aus der Akkuentladung. Autorisierte
        # Wallbox-Stützung ist damit kein Defizit der vorrangigen WP.
        grid_import_w = max(0.0, balance_grid)
        grid_without_wallbox = max(0.0, grid_import_w - reclaim_w)
        battery_without_wallbox = max(
            0.0, -balance_battery - max(0.0, reclaim_w - grid_import_w))
        balance_without_wallbox = bool(grid_without_wallbox <= 200.0
                                       and battery_without_wallbox <= 200.0)
    if measured and not measured_observation_valid:
        # Bestehendes 200-W-Defizitband; die WP ist bereits im Hausverbrauch.
        # Ohne gültige Bilanz übernimmt weiterhin der Kommunikationswächter.
        qualified = bool(requested and balance_fresh and (
            balance_without_wallbox if balance_without_wallbox is not None
            else balance_grid <= 200.0 and balance_battery >= -200.0))
    elif measured and state["active_command"] and balance_without_wallbox is not None:
        # Ein bestehender Boost darf den nachrangigen Verbraucher verdrängen.
        # Neue Starts behalten ihre bisherige Qualifikation und Schutzschranken.
        qualified = bool(requested and balance_without_wallbox)
    forecast_hold = False
    forecast = _dict(source.get("curve_forecast"))
    measured_offer = _number(source.get("measured_pv_offer_w")) if balance_fresh else None
    if measured and measured_observation_valid and state["active_command"] and measured_offer is not None:
        # Nach dem Versand zählt die aktuelle Bilanz, nicht die Startverzögerung
        # eines neuen thermischen Bedarfs. Laufende nachrangige Last ist verfügbar.
        hold_deficit = _number(source.get("measured_pv_deficit_w"),
                               max(0.0, balance_grid) + max(0.0, -balance_battery))
        hold_capacity = measured_offer - hold_deficit + (observed_w or 0.0) + (reclaim_w or 0.0)
        required = observed_w if physical is True and observed_w is not None and observed_w > 0 else start_power
        qualified = bool(requested and hold_capacity >= required)
        # Nur das Kurvendefizit darf entfallen. Tatsächliche Quellenverluste
        # bleiben auch bei guter Zukunftsprognose der Wolkenlogik unterworfen.
        physical_surplus = (balance_without_wallbox if balance_without_wallbox is not None
                            else balance_grid <= 200.0 and balance_battery >= -200.0)
        uncurved_capacity = (-balance_grid + balance_battery
                             + (observed_w or 0.0) + (reclaim_w or 0.0))
        forecast_hold = bool(not qualified and requested and not protection
            and physical_surplus and uncurved_capacity >= required
            and forecast.get("valid") is True and forecast.get("covers") is True)
        qualified = qualified or forecast_hold
    standing_qualified = bool(measured and qualified and physical is False and measured_observation_valid)
    stop_recheck = False
    stop_since = _number(state.get("stop_recheck_since_s"))
    if measured and stop_since is not None and physical is False:
        # Erst die folgende frische Bilanz kann die entfallene WP-Last belegen.
        # Die kurze Wartefrist endet absolut nach 30 s; Schutz bleibt sofort.
        post_stop_sample = bool(balance_fresh and measured_observation_valid
            and _number(source.get("sample_ts"), 0.0) > _number(state.get("stop_recheck_wall_s"), wall))
        stop_recheck = bool(not standing_qualified and not post_stop_sample and now - stop_since < 30.0)
        if standing_qualified or post_stop_sample or now - stop_since >= 30.0:
            state.pop("stop_recheck_since_s", None)
            state.pop("stop_recheck_wall_s", None)
    if measured and state["active_command"]:
        # Ein kurzer deckender Messpunkt verjüngt keine laufende Defizitfrist.
        if qualified:
            if state.get("pv_covered_since_s") is None:
                state["pv_covered_since_s"] = now
            if state.get("pv_unqualified_since_s") is not None:
                qualified = now - state["pv_covered_since_s"] >= 60.0
                forecast_hold = forecast_hold and qualified
        else:
            state["pv_covered_since_s"] = None
    else:
        state["pv_covered_since_s"] = None
    if standing_qualified and (compressor_stopped or stop_since is not None):
        qualified = True
    if not qualified:
        blockers.append("pv_not_qualified")
    restart_remaining = max(0.0, state["last_stop_s"] + _number(cfg.get("restart_delay_s"), 1200.0) - now) if state["last_stop_s"] is not None else 0.0
    if restart_remaining > 0 and physical is not True:
        blockers.append("compressor_restart_delay")
    if (state["offered_ts"] is not None and not state["cycle_owned"]
            and state.get("handoff_required", True) is not False):
        handoff_age = now - state["offered_ts"]
        handoff_timeout = _number(cfg.get("handoff_timeout_s"), 120.0)
        if handoff_age > handoff_timeout:
            if handoff_age < handoff_timeout + 60.0:
                blockers.append("handoff_retry_delay")
            else:
                # Ein nicht ausgegebener Auftrag darf nach begrenzter Pause
                # neu zugeteilt werden. Verbrauchte Energie bleibt unverändert.
                state["offered_ts"] = None
                state["battery_reserved_wh"] = state["grid_reserved_wh"] = 0.0
    window_battery = sum(e["battery_wh"] for e in state["energy_window"])
    window_grid = sum(e["grid_wh"] for e in state["energy_window"])
    window_grid_measured = sum(e.get("grid_measured_wh", 0.0) for e in state["energy_window"])
    window_battery_measured = sum(e.get("battery_measured_wh", 0.0) for e in state["energy_window"])
    battery_remaining = max(0.0, _number(cfg.get("battery_limit_wh"), 0.0) - window_battery)
    grid_remaining = max(0.0, _number(cfg.get("grid_limit_wh"), 0.0) - window_grid)
    battery_cap = min(_number(cfg.get("battery_max_w"), 0.0), _number(source.get("battery_available_w"), 0.0)) if source_fresh else 0.0
    grid_cap = min(_number(cfg.get("grid_max_w"), 0.0), _number(source.get("grid_available_w"), 0.0)) if source_fresh else 0.0
    battery_energy = min(battery_remaining, _number(source.get("battery_available_wh"), 0.0))
    grid_energy = grid_remaining
    duration = runtime + _number(cfg.get("start_wait_s"), 600.0) + _number(cfg.get("reaction_s"), 30.0)
    reaction_s = _number(cfg.get("reaction_s"), 30.0)
    reserve_duration = reaction_s if measured else duration
    capability_w = maximum if maximum > 0 or not measured else max(start_power, state["max_power_w"])
    if measured:
        capability_w = max(capability_w, observed_w or 0.0)
    if measured:
        battery_cap = battery_cap if source_enabled["battery"] else 0.0
        grid_cap = grid_cap if source_enabled["grid"] else 0.0
    # Im Messwertbetrieb begrenzen Quellenleistung und Restenergie die
    # verfügbare Hilfe. Sie erzeugen keine zusätzliche PV-Startschwelle.
    reserve_battery_cap = min(battery_cap, battery_energy * 3600.0 / reaction_s) if measured else battery_cap
    reserve_grid_cap = min(grid_cap, grid_energy * 3600.0 / reaction_s) if measured else grid_cap
    reserve_power = 0.0 if measured else capability_w
    reserve = _reserve_energy(reserve_power, reserve_duration,
                              reserve_battery_cap, battery_energy, reserve_grid_cap, grid_energy)
    if measured and reserve_power <= 0:
        reserve = (0.0, 0.0)
    battery_response_wh = _number(source.get("battery_response_required_wh"), 0.0)
    battery_response_w = _number(source.get("battery_response_required_w"), 0.0)
    response_funded = bool(battery_cap >= battery_response_w and battery_energy >= battery_response_wh
                           and (reserve is None or reserve[0] >= battery_response_wh))
    if not response_funded and not state["cycle_owned"]:
        blockers.append("battery_response_reserve_unfunded")
    if reserve is None and not state["active_command"]:
        blockers.append("minimum_runtime_energy_unfunded")
    # Ein verbrauchtes gemessenes Netzkontingent sperrt keinen neuen PV-Start.
    # Es verlangt zusätzlich das vorhandene 500-W-Startband, ohne Budgetabzug.
    start_margin_w = PV_START_MARGIN_W * (2.0 if measured and _number(cfg.get("grid_limit_wh"), 0.0) > 0
        and window_grid_measured >= _number(cfg.get("grid_limit_wh"), 0.0) else 1.0)
    if takeover_active:
        start_margin_w = 0.0
    if (measured and not state["cycle_owned"] and start_margin_w > PV_START_MARGIN_W
            and _number(source.get("prospective_capacity_w"), 0.0) < start_power + start_margin_w):
        blockers.append("pv_start_margin_after_grid_exhaustion")
    # Ein PV-Sollwertangebot reserviert weder Leistung noch Energie.
    # Alte Reservierungen werden abgeräumt; die Verbrauchskonten bleiben erhalten.
    released = bool(measured and state["active_command"])
    if measured:
        state["battery_reserved_wh"] = state["grid_reserved_wh"] = 0.0
        state["reservation_started_ts"] = None
        state["start_reservation_released"] = released
        state["start_reservation_released_ts"] = now if released else None
    start_candidate = bool(not blockers and qualified and not state["cycle_owned"]
                           and clock_ok and not state.get("withdrawal_pending"))
    if start_candidate:
        state["request_id"], state["revision"] = identity
        state["battery_reserved_wh"], state["grid_reserved_wh"] = reserve
        state.pop("first_withdrawal_reason", None)
        state.pop("first_withdrawal_sources", None)
        state["withdrawal_reason"] = ""
        state["request_withdrawn_since_s"] = None
        for key in ("withdrawal_identity", "request_returned_since_s", "first_withdrawal_ts",
                    "first_withdrawal_class", "first_withdrawal_cause", "withdrawal_healed_ts"):
            state.pop(key, None)
        if measured:
            state["energy_guard_pending"] = False
            state["energy_guard_sources"] = []
            state["withdrawal_reason"] = ""
    if state["active_command"] and identity and (state["request_id"], state["revision"]) != identity:
        # Ein Sollwertwechsel besitzt weder einen frischen Energietopf noch neue Laufzeit.
        state["request_id"], state["revision"] = identity
    wait_expired = bool(state["issued_ts"] is not None and physical is not True
                        and now - state["issued_ts"] >= _number(cfg.get("start_wait_s"), 600.0))
    command_expired = bool(state["issued_ts"] is not None and command.get("confirmed") is not True
                           and not state["signal_withdrawn"]
                           and now - state["issued_ts"] >= _number(cfg.get("command_timeout_s"), 25.0))
    wallbox_deferred = False
    wallbox_defer_reason = ""
    request_debounced = False
    if measured:
        # Ein einzelner Wert im Rauschband löscht keine Vormerkung. Das
        # schützt Checkpoints vor Schreibfluten; ein belegter Stopp löscht sofort.
        exhausted = [name for name, used, counted in (
            ("battery", window_battery_measured, battery_counted_w),
            ("grid", window_grid_measured, grid_counted_w))
            if source_enabled[name] and used >= _number(cfg.get(name + "_limit_wh"), 0.0)
            and counted is not None and counted > 0]
        clear_since = _dict(state.get("energy_guard_clear_since_s"))
        held_sources = set(state.get("energy_guard_sources") or [])
        for name in ("battery", "grid"):
            if not state["cycle_owned"] or physical is False or not source_enabled[name]:
                held_sources.discard(name)
                clear_since.pop(name, None)
            elif name in exhausted:
                held_sources.add(name)
                clear_since.pop(name, None)
            elif measured_observation_valid and measured_interval:
                since = _number(clear_since.get(name), now)
                clear_since[name] = since
                if now - since >= 60.0:
                    held_sources.discard(name)
            else:
                # Ungültige Proben belegen keine durchgehende Erholung.
                clear_since.pop(name, None)
        exhausted = [name for name in ("battery", "grid") if name in held_sources]
        state["energy_guard_clear_since_s"] = clear_since
        state["energy_guard_pending"] = bool(exhausted)
        state["energy_guard_sources"] = exhausted
        # Der gehaltene Marker beruhigt die Persistenz, ist aber kein
        # Nachweis für Netzbezug im aktuellen gültigen Messintervall.
        grid_exhausted = bool(state["active_command"] and physical is True
                              and "grid" in exhausted and grid_counted_w is not None
                              and grid_counted_w > 0)
        grid_only_disabled = bool(protection == "bridge_source_disabled"
            and not source_enabled["grid"]
            and (not source_fresh or _number(source.get("battery_actual_w"), 0.0) <= 0)
            and state["last_battery_w"] <= 0 and state["battery_reserved_wh"] <= 0)
        # Erlaubte Akkustützung ändert den Mindestlaufhalt bei gesperrter
        # Netzbrücke nicht. Wallbox-Warten gilt weiter nur bei reinem Netzanteil.
        grid_disabled_reason = bool(protection == "bridge_source_disabled"
            and not source_enabled["grid"]
            and (source_enabled["battery"] or grid_only_disabled))
        wait_protection = bool(protection and not grid_only_disabled)
        # Eine kurze Quellenlücke eröffnet keine Episode. Ein bereits
        # belegtes Warten behält den vorhandenen, begrenzten Nachlauf.
        wait_source = bool(source_fresh and balance_fresh
                           or state.get("wallbox_waiting_previous") and dt <= ttl
                           and not source.get("restart") and not clock_uncertain)
        wallbox_deferred, wallbox_defer_reason = _wallbox_grid_wait(
            state, wb if source_fresh and balance_fresh else dict(wb, active=False, onset_ready=False),
            wall=wall, now=now, cfg=cfg,
            enabled=bool(state["active_command"] and demand_fresh and clock_ok
                and wait_source and not wait_protection),
            grid_onset=bool(measured_interval and balance_grid > grid_band_w))
        if wallbox_deferred and state.get("wallbox_waiting_previous"):
            if state.get("pv_unqualified_since_s") is not None:
                state["pv_unqualified_since_s"] += dt
        state["wallbox_waiting_previous"] = wallbox_deferred
        # Ein fremder Besitzer erhält keine Entprellung. Fehlende Klassifikation
        # alter Bedarfsdateien gilt als gewöhnliches Anforderungsende.
        withdrawal_class = demand.get("withdrawal_class", "debounced")
        if not requested and withdrawal_class == "debounced" and state["active_command"]:
            if state.get("request_withdrawn_since_s") is None:
                state["request_withdrawn_since_s"] = now
            request_debounced = now - state["request_withdrawn_since_s"] < REQUEST_WITHDRAWAL_DEBOUNCE_S
        else:
            state["request_withdrawn_since_s"] = None
        terminal_reason = protection or ("command_unconfirmed" if command_expired else "")
        if not requested and not request_debounced:
            terminal_reason = terminal_reason or "request_withdrawn"
        if grid_exhausted and not wallbox_deferred:
            terminal_reason = terminal_reason or "wp_grid_energy_exhausted"
        pending_reason = state.get("withdrawal_reason", "")
        # Eine kurze Besitzer- oder Schutzlücke heilt erst nach durchgehend
        # gültiger Rückkehr desselben Auftrags. Ein bereits begonnener Entzug
        # bleibt endgültig; Schutz und unbekannte Daten verdienen keine Zeit.
        returning_request = bool(state["withdrawal_pending"]
            and pending_reason == "request_withdrawn"
            and state.get("withdrawal_class") in ("owner_change", "protection")
            and requested and withdrawal_class == "debounced"
            and identity == _identity(state.get("withdrawal_identity"))
            and not protection and demand.get("protection_reason") not in HARD_PROTECTIONS
            and not command.get("withdrawal_requested") and not command.get("withdrawal_confirmed")
            and not state["signal_withdrawn"] and state["active_command"]
            and data_fresh and measured_observation_valid and source_fresh and clock_ok and not corrupt
            and cfg.get("valid") is True and bound_command
            and not state.get("uncertain_state"))
        if returning_request:
            if state.get("request_returned_since_s") is None or dt > ttl:
                state["request_returned_since_s"] = now
        else:
            state["request_returned_since_s"] = None
        request_healed = bool(returning_request
            and now - state["request_returned_since_s"] >= REQUEST_WITHDRAWAL_DEBOUNCE_S)
        cancel_pending = bool(not state["signal_withdrawn"]
            and not command.get("withdrawal_requested")
            and not command.get("withdrawal_confirmed") and (
            (pending_reason == "request_withdrawn" and requested
             and (state.get("withdrawal_class", "debounced") == "debounced" or request_healed))
            or (pending_reason == "command_unconfirmed" and command.get("confirmed") is True)
            # Alte Vormerkungen dürfen bei gültiger Erholung entfallen,
            # auch wenn nach einer Pause noch kein Messintervall vorliegt.
            or (pending_reason == "energy_guard_exhausted"
                and measured_observation_valid and not grid_exhausted)
            or (pending_reason == "wp_grid_energy_exhausted"
                and measured_interval and "grid" not in exhausted)
            or (pending_reason == "pv_unqualified" and (qualified or compressor_stopped and standing_qualified))
            or (pending_reason == "wp_grid_energy_exhausted" and wallbox_deferred)
            or (pending_reason == "bridge_source_disabled" and measured_interval
                and not protection)))
        if cancel_pending:
            if request_healed:
                state["withdrawal_healed_ts"] = wall
            state["request_returned_since_s"] = None
            state.pop("withdrawal_identity", None)
            state["withdrawal_pending"] = False
            state["withdrawal_reason"] = ""
            state.pop("first_withdrawal_reason", None)
            state.pop("first_withdrawal_sources", None)
        if terminal_reason and state["active_command"]:
            if terminal_reason == "request_withdrawn" and "withdrawal_identity" not in state:
                state["withdrawal_identity"] = {"request_id": state["request_id"], "revision": state["revision"]}
            if not requested and withdrawal_class != "debounced" and state.get("first_withdrawal_ts") is None:
                state["first_withdrawal_ts"] = wall
                state["first_withdrawal_class"] = withdrawal_class
                state["first_withdrawal_cause"] = demand.get("withdrawal_cause") or terminal_reason
            state["withdrawal_pending"] = True
            state["withdrawal_reason"] = terminal_reason
            if (terminal_reason != "request_withdrawn" or pending_reason != "request_withdrawn"
                    or state.get("withdrawal_class") not in ("owner_change", "protection")):
                state["withdrawal_class"] = withdrawal_class
        # Die Wolkenfrist läuft auch unter einer Vormerkung weiter und wird
        # beim Verdichterstopp nicht verjüngt. Ein Messglitch erzeugt kein Ende.
        if state["active_command"] and not qualified and not request_debounced and not wallbox_deferred:
            unqualified_since = _number(state.get("pv_unqualified_since_s"))
            if unqualified_since is None:
                state["pv_unqualified_since_s"] = now
            elif (now - unqualified_since >= _number(cfg.get("boost_release_s"), 300.0)
                  and not state["withdrawal_pending"]):
                state["withdrawal_pending"] = True
                state["withdrawal_reason"] = "pv_unqualified"
        if qualified or not state["active_command"]:
            state["pv_unqualified_since_s"] = None
    else:
        if wait_expired or command_expired or not requested or protection:
            state["withdrawal_pending"] = bool(state["active_command"])
        if state["active_command"] and (not qualified or battery_remaining + grid_remaining <= 0):
            # Eine normale Optimierungsgrenze ist kein vorzeitiger Schutzabbruch.
            state["withdrawal_pending"] = True
    hold_required = bool(state["cycle_owned"] and protected > 0
                         and (not protection or measured and grid_disabled_reason))
    withdrawal_required = bool(state["withdrawal_pending"] and (protected <= 0 or protection)
        and not (stop_recheck and not protection and state.get("withdrawal_reason") == "pv_unqualified")
        and not (measured and state.get("withdrawal_reason") == "wp_grid_energy_exhausted"
                 and not grid_exhausted)
        and not (measured and state.get("withdrawal_reason") == "bridge_source_disabled"
                 and (not protection or grid_disabled_reason
                      and (protected > 0 or grid_counted_w is None or grid_counted_w <= 0)))
        and not (wallbox_deferred and not wait_protection and state.get("withdrawal_reason") in (
            "pv_unqualified", "wp_grid_energy_exhausted", "bridge_source_disabled")))
    if withdrawal_required and not state.get("first_withdrawal_reason"):
        state["first_withdrawal_reason"] = state.get("withdrawal_reason") or protection or "withdrawal_required"
        state["first_withdrawal_sources"] = (list(state.get("energy_guard_sources") or [])
            if state["first_withdrawal_reason"] in ("energy_guard_exhausted", "wp_grid_energy_exhausted") else [])
    # Ein einzelner Quellentopf darf nicht vorzeitig leergefahren werden, während
    # nur die Summenenergie ausreichend wäre. Die mögliche Leistung folgt auch
    # aus seiner Energie für die noch zugesagte Schutz- und Reaktionsdauer.
    energy_horizon = max(protected, 0.0) + _number(cfg.get("reaction_s"), 30.0)
    if not state["cycle_owned"]:
        energy_horizon = duration
    if measured:
        energy_horizon = reaction_s
        if state["cycle_owned"] and physical is True and remaining > 0 and not protection:
            # Ein normales Kontingent ist im Messwertbetrieb kein harter
            # Verdichterabbruch. Die Quelle muss ausdrücklich erlaubt bleiben;
            # reale Akkuenergie und sämtliche W-Grenzen gelten unverändert.
            if source_enabled["battery"] and (not measured_observation_valid or battery_remaining > 1e-6):
                battery_energy = _number(source.get("battery_available_wh"), 0.0)
            if source_enabled["grid"] and (not measured_observation_valid or grid_remaining > 1e-6):
                grid_energy = grid_cap * energy_horizon / 3600.0
    battery_cap = min(battery_cap, battery_energy * 3600.0 / energy_horizon)
    grid_cap = min(grid_cap, grid_energy * 3600.0 / energy_horizon)
    # Im Messwertbetrieb folgt das Budget der Istaufnahme. Der Start wird
    # separat durch die qualifizierte PV-Startleistung gedeckt. Ein Profilwert
    # ist keine zusätzliche dauerhafte PV-Reservierung für einen möglichen Sprung.
    actual = observed_w if observed_w is not None else max(maximum, state["max_power_w"])
    if measured and not protection:
        # Unbekannt bleibt null in der Diagnose. Im gemessenen Hausverbrauch
        # enthaltene WP-Leistung bekommt keinen zweiten Budgetabzug.
        actual = observed_w if observed_w is not None else 0.0
    battery_reaction_w = min(battery_cap, _number(source.get("battery_reaction_available_w"), 0.0))
    reaction_reserve = 0.0 if measured else max(0.0, max(maximum, state["max_power_w"]) - actual - battery_reaction_w - grid_cap)
    startup_shared_reserve = 0.0
    if state["cycle_owned"]:
        request_w = actual + reaction_reserve
        if physical is not True and command.get("confirmed") is not True and not (measured and released):
            request_w = max(request_w, start_power if measured else maximum)
        if (measured and state["active_command"] and not state["signal_withdrawn"]
                and not wait_expired and not released
                and state.get("withdrawal_reason") != "compressor_stopped"
                and (physical is not True or actual <= 0)):
            # Ein bestätigter Sollwert ist noch kein gemessener Verdichterstart.
            request_w = max(request_w, start_power)
    else:
        request_w = (start_power if measured else maximum) if start_candidate else (actual if physical is True else 0.0)
    if measured:
        # Die Istaufnahme wird in der zentralen Bilanz genau einmal gezählt.
        reserving = False
        request_w = actual
    if protection:
        request_w = actual if observed_w is not None else max(maximum, state["max_power_w"])
    # Die Energiequellen gleichen nur den WP-Anteil aus, niemals allgemeine Last.
    shared = _number(source.get("shared_capacity_w"), 0.0)
    deficit = max(0.0, request_w - shared)
    battery_bridge = min(deficit, battery_cap)
    grid_bridge = min(max(0.0, deficit - battery_bridge), grid_cap)
    if state["cycle_owned"] and source_fresh:
        state["last_battery_w"] = min(_number(source.get("battery_actual_w"), 0.0), actual)
        state["last_grid_w"] = min(_number(source.get("grid_actual_w"), 0.0), max(0.0, actual - state["last_battery_w"]))
    elif measured and state["cycle_owned"] and short_source_gap and measured_observation_valid:
        state["last_battery_w"] = battery_bound
        state["last_grid_w"] = grid_bound
    else:
        state["last_battery_w"] = state["last_grid_w"] = 0.0
    state["last_grid_measured_w"] = grid_counted_w or 0.0
    state["last_battery_measured_w"] = battery_counted_w or 0.0
    state["last_ts"] = max(now, state["last_ts"])
    if current_clock is not None:
        state["clock"] = current_clock
    reason = protection or ("protected_minimum_runtime" if hold_required else
             "withdrawal_required" if withdrawal_required else
             "measured_pv_offer_held_forecast_covers_curve" if forecast_hold else
             "running" if physical is True and state["active_command"] else
             "start_reservation_released" if measured and released and state["active_command"] else
             "waiting_for_compressor" if state["active_command"] else
             "start_candidate" if start_candidate else (blockers[0] if blockers else "idle"))
    return {
        "schema": CONTRACT_SCHEMA, "sample_ts": wall,
        "control_mode": "measured" if measured else "reserved",
        "curve_forecast_hold": forecast_hold, "curve_forecast": copy.deepcopy(forecast),
        "start_power_w": start_power, "start_margin_w": start_margin_w,
        "ww_takeover": takeover_active,
        "ww_takeover_budget_w": takeover["start_budget_w"] if takeover_active else None,
        "grid_limit_wh": cfg.get("grid_limit_wh"), "battery_limit_wh": cfg.get("battery_limit_wh"),
        "stop_recheck_pending": stop_recheck,
        "power_accounting_w": accounted_power,
        "power_accounting_source": power_source,
        "power_accounting_age_s": power_age,
        "power_hold_s": power_hold_s,
        # Nur die WP-Leistung fehlt: kein neuer Start, aber auch kein Entzug
        # des vorhandenen Sollwerts durch den Kommunikationswächter.
        "measurement_gap_control_valid": bool(measured and (not measured_observation_valid or not source_fresh)
            and cfg.get("valid") and demand_fresh and balance_fresh and clock_ok
            and not corrupt and not state.get("uncertain_state") and not protection),
        "reserve_duration_s": reserve_duration,
        "profile_tolerance_w": profile_tolerance,
        "profile_excess_w": profile_excess,
        "profile_within_tolerance": profile_excess <= profile_tolerance if profile_excess is not None else None,
        "reaction_capability_w": capability_w,
        "valid": bool(cfg.get("valid") and data_fresh and source_fresh and clock_ok and not corrupt
                      and (not measured or measured_observation_valid)),
        "request_id": identity[0] if identity else "", "revision": identity[1] if identity else 0,
        "state": state, "request_w": int(math.ceil(request_w)),
        "minimum_w": int(math.ceil(request_w)) if state["cycle_owned"] or start_candidate else 0,
        "start_candidate": start_candidate, "protected_remaining_s": protected,
        "compressor_protected_remaining_s": remaining, "restart_remaining_s": restart_remaining,
        "hold_required": hold_required, "withdrawal_required": withdrawal_required,
        "command_outstanding": state["active_command"], "cycle_owned": state["cycle_owned"],
        "signal_withdrawn": state["signal_withdrawn"],
        "protection_reason": protection, "blockers": list(dict.fromkeys(blockers)), "reason": reason,
        "prospective_capacity_w": _number(source.get("prospective_capacity_w"), shared),
        "bridge_battery_w": battery_bridge, "bridge_grid_w": grid_bridge,
        "battery_available_w": battery_cap, "grid_available_w": grid_cap,
        "battery_remaining_wh": battery_remaining, "grid_remaining_wh": grid_remaining,
        "battery_overrun_wh": max(0.0, window_battery - _number(cfg.get("battery_limit_wh"), 0.0)),
        "grid_overrun_wh": max(0.0, window_grid - _number(cfg.get("grid_limit_wh"), 0.0)),
        "energy_guard_pending": bool(state.get("energy_guard_pending")),
        "window_grid_measured_wh": window_grid_measured,
        "window_grid_unmeasured_wh": max(0.0, window_grid - window_grid_measured),
        "window_battery_measured_wh": window_battery_measured,
        "window_battery_unmeasured_wh": max(0.0, window_battery - window_battery_measured),
        "grid_counted_w": grid_counted_w,
        "battery_counted_w": battery_counted_w,
        "grid_measurement_band_w": grid_band_w,
        "grid_withdrawal_deferred": wallbox_deferred,
        "grid_withdrawal_defer_reason": wallbox_defer_reason,
        "wallbox_grid_defer_wh": _number(state.get("wallbox_grid_defer_wh"), 0.0),
        "request_withdrawal_debounced": request_debounced,
        "minimum_runtime_reason": "Mindestlaufzeit schützt den Verdichter" if remaining > 0 else "",
        "withdrawal_reason": state.get("withdrawal_reason", ""),
        "first_withdrawal_ts": state.get("first_withdrawal_ts"),
        "first_withdrawal_class": state.get("first_withdrawal_class", ""),
        "first_withdrawal_cause": state.get("first_withdrawal_cause", ""),
        "withdrawal_healed_ts": state.get("withdrawal_healed_ts"),
        "first_withdrawal_reason": state.get("first_withdrawal_reason", ""),
        "first_withdrawal_sources": list(state.get("first_withdrawal_sources") or []),
        "energy_guard_sources": list(state.get("energy_guard_sources") or []),
        "measurement_gap_charged": state["measurement_gap_charged"],
        "measurement_gap_battery_wh": state["measurement_gap_battery_wh"],
        "measurement_gap_grid_wh": state["measurement_gap_grid_wh"],
        "measurement_gap_reason": state["measurement_gap_reason"],
        "battery_response_required_wh": battery_response_wh,
        "battery_response_required_w": battery_response_w,
        "quarantine_remaining_s": state.get("quarantine_remaining_s", 0.0),
        "window_battery_used_wh": window_battery, "window_grid_used_wh": window_grid,
        "energy_reservation_power_w": reserve_power,
        "energy_reservation_required_wh": reserve_power * reserve_duration / 3600.0,
        "measurement_uncertain": gap or not data_fresh or not source_fresh or (measured and not measured_observation_valid),
        "reaction_reserve_w": reaction_reserve, "max_power_w": maximum,
        "startup_shared_reserve_w": startup_shared_reserve,
        "handoff_timeout_s": _number(cfg.get("handoff_timeout_s"), 120.0),
        "command_timeout_s": _number(cfg.get("command_timeout_s"), 25.0),
        "start_reservation_released": bool(measured and released),
        "start_reservation_active": bool(measured and reserving),
        "reservation_started_ts": state.get("reservation_started_ts"),
        "pv_unqualified_since_s": state.get("pv_unqualified_since_s"),
        "boost_release_s": _number(cfg.get("boost_release_s"), 300.0),
    }


def bind_heatpump_pv_grant(request_contract, *, allocated_w, shared_funded_w,
                           battery_funded_w=0, grid_funded_w=0,
                           wallbox_actual_w=0, wallbox_target_w=0,
                           phase_transition_active=False, clock_sample,
                           handoff_required=True):
    """Nach der Verteilung: Quelle und tatsächliche WB-Absenkung vor Ausgang binden."""
    result = copy.deepcopy(_dict(request_contract))
    state = validate_heatpump_pv_state(result.get("state"))
    wall, now, _, clock_uncertain = _clock(clock_sample, state)
    allocation = _number(allocated_w, 0.0)
    shared = _number(shared_funded_w, 0.0)
    battery = min(_number(battery_funded_w, 0.0), _number(result.get("battery_available_w"), 0.0))
    grid = min(_number(grid_funded_w, 0.0), _number(result.get("grid_available_w"), 0.0))
    required = _number(result.get("request_w"), 0.0)
    funded = allocation >= required and shared + battery + grid >= required
    if result.get("control_mode") == "measured" and result.get("start_candidate"):
        # Freigabe aus dem vorrangigen PV-Rahmen, ohne Vorabreservierung.
        # Das bestehende 500-W-PV-Schaltband schützt die neue Startkante;
        # Überbrückungsquellen ersetzen die PV-Startdeckung nicht.
        start_capacity = (result.get("ww_takeover_budget_w") if result.get("ww_takeover")
                          else result.get("prospective_capacity_w"))
        funded = funded and _number(start_capacity, 0.0) >= _number(result.get("start_power_w"), 0.0) + _number(result.get("start_margin_w"), PV_START_MARGIN_W)
    # Bei Wärmepumpen-Vorrang ist die Wallbox-Absenkung keine Vorbedingung des
    # Sollwerts: Die Reservierung senkt das Wallbox-Ziel, die Wallbox regelt parallel
    # nach, die Anlage startet nach eigener Hysterese.
    handoff_required = bool(handoff_required)
    waiting = bool(handoff_required
                   and _number(wallbox_actual_w, math.inf) > _number(wallbox_target_w, 0.0) + 100.0)
    transition = phase_transition_active is True
    if state:
        state["handoff_required"] = handoff_required
    if state and result.get("start_candidate") and funded:
        if state.get("offered_ts") is None:
            state["offered_ts"] = now
        # Die Reservierungsuhr beginnt erst mit dem Versandbeleg aus dem EM.
    handoff_expired = bool(handoff_required and state and state.get("offered_ts") is not None
                           and now - state["offered_ts"] > result.get("handoff_timeout_s", 120.0))
    command_deadline = (state["issued_ts"] + result.get("command_timeout_s", 25.0)) if state and state.get("issued_ts") is not None else None
    fresh = _fresh(result, wall, 45.0)
    identity_bound = bool(_identity(result) and _identity(result) == _identity(state))
    allowed = bool(state and result.get("valid") and fresh and identity_bound and now >= state["last_ts"]
                   and not clock_uncertain and _identity(result) and result.get("start_candidate")
                   and funded and not waiting and not transition and not handoff_expired
                   and not result.get("protection_reason"))
    result.update({
        "state": state, "command_authorized": allowed,
        "grant_id": f"{result.get('request_id', '')}:{result.get('revision', 0)}:{state.get('offered_ts')}" if state else "",
        "allocated_w": allocation, "shared_funded_w": shared,
        "battery_funded_w": battery, "grid_funded_w": grid,
        "waiting_for_wallbox_reduction": waiting,
        "handoff_required": handoff_required,
        "phase_transition_active": transition,
        "handoff_expired": handoff_expired, "command_deadline_s": command_deadline,
    })
    if state and not funded and not state.get("active_command"):
        state["battery_reserved_wh"] = state["grid_reserved_wh"] = 0.0
    if result.get("start_candidate") and not allowed:
        result["reason"] = ("handoff_timeout" if handoff_expired else
                            "wallbox_phase_transition" if transition else
                            "waiting_for_wallbox_reduction" if waiting else "power_not_funded")
    return result


def stiebel_sg_ready_output_contract(config):
    """Entscheidet rein lesend, ob der experimentelle ISG-SG-Ready-Ausgang aktiv ist.

    Er entsteht nur bei Wärmepumpen-Typ Stiebel, aktivem WP-/Verbrauchslogging,
    gesetzter ISG-Adresse und ausdrücklichem Schreib-Opt-in. Ein konfigurierter
    Shelly-SG-Ready- oder EVU-Kontakt behält Vorrang, damit nie zwei Ausgänge
    dieselbe SG-Ready-Schnittstelle ansteuern.
    """

    cfg = {str(key).strip().lower(): value for key, value in (config or {}).items()}

    def flag(key):
        return str(cfg.get(key, "0")).strip().lower() in ("1", "1.0", "true", "yes", "on", "ja", "ein")

    def address(key):
        return str(cfg.get(key, "") or "").strip().lower() not in ("", "0", "0.0.0.0", "none", "null")

    if not flag("stiebel_isg_sg_ready_write"):
        reason = "opt_in_off"
    elif int(_number(cfg.get("wp_type"), -1, minimum=-1)) != 4:
        reason = "wp_type_not_stiebel"
    elif not flag("luxtronik"):
        reason = "heatpump_logging_off"
    elif not address("stiebel_isg_ip"):
        reason = "isg_ip_missing"
    elif address("shelly_sg_ip") or address("shelly_pause_ip"):
        reason = "shelly_sg_ready_configured"
    else:
        reason = "active"
    return {"active": reason == "active", "reason": reason}


# Startart und Zeitfenster gelten für beide Manager; die Treiber führen nur aus.
HEATPUMP_START_SEMANTICS_SG_READY = "sg_ready_delayed_start"
HEATPUMP_START_SEMANTICS_DIRECT = "direct_setpoint"
HEATPUMP_SG_READY_START_RESERVATION_S = 150.0
HEATPUMP_DIRECT_START_RESERVATION_S = 25.0


def heatpump_start_semantics(config, *, sg_ready_output=False):
    """Klassifiziert die Ausgangsfähigkeit einschließlich des ISG-Schreibpfads."""
    cfg = _dict(config)
    wp_type = _number(cfg.get("wp_type"), -1, minimum=-1)
    shelly = str(cfg.get("shelly_sg_ip", "") or "").strip()
    if (wp_type in (3, 5) or shelly not in ("", "0.0.0.0")
            or sg_ready_output or stiebel_sg_ready_output_contract(cfg)["active"]):
        return HEATPUMP_START_SEMANTICS_SG_READY
    return HEATPUMP_START_SEMANTICS_DIRECT


def heatpump_start_reservation_duration_s(config, *, sg_ready_output=False):
    """SG Ready: mindestens 150 s und 120 s nach der Einschaltverzögerung.

    Die Marge umfasst die 65-s-Anlaufsperre des EM sowie Lesen, Schreiben
    und Verdichterannahme. Direkte
    Sollwertangebote behalten ihre bisherige kurze Budgetreservierung.
    """
    cfg = _dict(config)
    if heatpump_start_semantics(cfg, sg_ready_output=sg_ready_output) == HEATPUMP_START_SEMANTICS_SG_READY:
        delay = _number(cfg.get("pv_boost_delay"), 30.0)
        return max(HEATPUMP_SG_READY_START_RESERVATION_S, delay + 120.0)
    return min(60.0, max(5.0, _number(cfg.get("consumer_start_lease_duration_s"),
                                   HEATPUMP_DIRECT_START_RESERVATION_S)))


def heatpump_idle_threshold_w(config):
    """Frischen Leerlauf von Pumpenvorlauf trennen, ohne Annahme zu behaupten.

    Der vorhandene ISG-Standby-Wert erhält 25 W Messspielraum. 100 W begrenzen
    die Erkennung unterhalb des bekannten 150-W-Pumpenvorlaufs. Andere Geräte
    behalten 50 W; ein fehlender Messwert ist niemals Leerlauf.
    """
    cfg = _dict(config)
    if _number(cfg.get("wp_type"), -1, minimum=-1) == 4:
        standby = _number(cfg.get("stiebel_isg_standby_w"), 35.0)
        return max(50.0, min(100.0, standby + 25.0))
    return 50.0


