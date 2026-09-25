"""Reiner PV-Wärmevertrag: Auftragsbindung, Schutzzeit und Quellenenergie.

Dieses Modul besitzt keinen Hardwareausgang. Der Storage Manager besitzt das
Energiekonto; der Energy Manager besitzt Kanalaufträge und Gerätebeobachtung.
Leistungswerte beziehen sich auf dieselbe elektrische Messgrenze der WP.
"""

from __future__ import annotations

import copy
import math


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
):
    """Entscheidet, ob eine Warmwasser-Boostphase die Zirkulation erzwingt.

    Die Einstellung „Zirkulationspumpe während Boost-Phasen dauerhaft ein“
    war bisher nur an den Legacy-Boostpfad (``force_ww``) gebunden. Die
    PV-Wärmefreigabe läuft über den Vertragspfad und setzt den WW-Sollwert,
    ohne ``force_ww`` zu setzen; die Pumpe folgte deshalb im Boost weiter dem
    Takt-Timer, obwohl die Leitungen mit aufgewärmt werden sollen.

    Der PV-Auftrag zählt jetzt ebenfalls als Boostphase: Der Vertrag muss den
    WW-Kanal aktiv tragen und der Auftrag muss am Gerät starten oder noch
    ausstehen. Eine angeforderte Rücknahme beendet die Phase nicht, solange
    die Wärmepumpe den Auftrag noch zu Ende führt; erst der bestätigte
    Abschluss, ein fremder Eigentümer des Kanals oder ein fehlender
    Vertragspfad geben die Pumpe dem Timer zurück. Zirkulationsfenster,
    Nutzerpause, Nutzer-Aus und Schreibtakt bleiben Sache des Aufrufers.
    """

    result = {
        "contract": WW_CIRCULATION_BOOST_SCHEMA,
        "force_on": False,
        "source": "",
        "reason": "circ_boost_disabled",
    }
    if not bool(circ_boost_enabled):
        return result
    if bool(legacy_force_ww):
        result["force_on"] = True
        result["source"] = "legacy_boost"
        result["reason"] = "legacy_force_ww"
        return result
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
    result["force_on"] = True
    result["source"] = "pv_contract"
    result["reason"] = (
        "pv_ww_cycle_finishing" if output.get("withdraw") else "pv_ww_channel_held"
    )
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
    for key in ("reservation_started_ts", "start_reservation_released_ts", "pv_unqualified_since_s"):
        if key in result and result[key] is not None:
            if _number(result[key]) is None:
                return {}
            result[key] = _number(result[key])
    if "start_reservation_released" in result and type(result["start_reservation_released"]) is not bool:
        return {}
    if "handoff_required" in result and type(result["handoff_required"]) is not bool:
        return {}
    return result


def _clear_start_reservation_release(state):
    """Startreservierung wieder scharf schalten (Verdichter läuft, Auftrag geschlossen, Sperre um)."""
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
        "start_reservation_released_ts": None, "pv_unqualified_since_s": None,
    }


def _record_energy(state, now, battery_wh, grid_wh, window_s):
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
    # Rechteckintegration mit dem vorigen belegten Quellenwert; Ausfälle sind
    # ausdrücklich keine kostenlose Energie und dürfen keinen neuen Topf öffnen.
    gap = bool(source.get("restart") is True or dt > ttl or not source_fresh or clock_uncertain)
    battery_delta = state["last_battery_w"] * dt / 3600.0
    grid_delta = state["last_grid_w"] * dt / 3600.0
    if gap and active_before:
        battery_bound = min(maximum or state["max_power_w"], _number(cfg.get("battery_max_w"), 0.0))
        grid_bound = min(maximum or state["max_power_w"], _number(cfg.get("grid_max_w"), 0.0))
        # Getrennte obere Schranken sind bei unbekannter Aufteilung erforderlich;
        # sie werden als unsichere Belastung und niemals als Messwert ausgegeben.
        battery_delta = max(battery_delta, battery_bound * dt / 3600.0)
        grid_delta = max(grid_delta, grid_bound * dt / 3600.0)
        if clock_uncertain:
            # Unbekannte Bootlücke kann mit Werten auf der Wanduhr nicht bewiesen
            # werden. Kontingente bis zur Klärung konservativ vollständig belasten.
            battery_delta = max(battery_delta, _number(cfg.get("battery_limit_wh"), 0.0))
            grid_delta = max(grid_delta, _number(cfg.get("grid_limit_wh"), 0.0))
        state["measurement_gap_charged"] = True
    _record_energy(state, max(now, state["last_ts"]), battery_delta, grid_delta,
                   _number(cfg.get("window_s"), 86400.0))
    if physical is True:
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
        if measured:
            state["withdrawal_reason"] = ""
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
        if any(not source_enabled[name] and (
            state[name + "_reserved_wh"] > 0 or state["last_" + name + "_w"] > 0
        ) for name in ("battery", "grid")):
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
    reserve_power = min(capability_w, reserve_battery_cap + reserve_grid_cap) if measured else capability_w
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
    # Befristete Startreservierung: Der Sollwert
    # steht für den Boost-Zeitraum, die Anlage startet nach eigener Hysterese.
    # Bleibt der Verdichter über wp_pv_start_wait_s aus, geht die reservierte
    # Startleistung an die nachrangigen Verbraucher; ab dem gemessenen Start
    # bindet wieder die Istaufnahme. Ein nie ausgespielter Auftrag wird erst
    # nach der Wiedereinschaltsperre erneut angeboten.
    start_wait_s = _number(cfg.get("start_wait_s"), 600.0)
    released = bool(state.get("start_reservation_released"))
    if measured and released and physical is True:
        _clear_start_reservation_release(state)
        released = False
    if measured and not released and physical is not True and not state.get("withdrawal_pending"):
        anchors = [value for value in (state["issued_ts"], _number(state.get("reservation_started_ts")))
                   if value is not None]
        anchor = min(anchors) if anchors else None
        if anchor is not None and now - anchor >= start_wait_s:
            state["start_reservation_released"] = True
            state["start_reservation_released_ts"] = now
            state["battery_reserved_wh"] = state["grid_reserved_wh"] = 0.0
            released = True
    if measured and released and not state["cycle_owned"]:
        released_ts = _number(state.get("start_reservation_released_ts"))
        if released_ts is not None and now - released_ts >= _number(cfg.get("restart_delay_s"), 1200.0):
            _clear_start_reservation_release(state)
            released = False
        else:
            blockers.append("start_reservation_released")
    start_candidate = bool(not blockers and qualified and not state["cycle_owned"]
                           and clock_ok and not state.get("withdrawal_pending"))
    if start_candidate:
        state["request_id"], state["revision"] = identity
        state["battery_reserved_wh"], state["grid_reserved_wh"] = reserve
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
    if measured:
        exhausted = [name for name, left in (("battery", battery_remaining), ("grid", grid_remaining))
                     if source_enabled[name] and left <= 1e-6 and (
                         state["last_" + name + "_w"] > 0 or state[name + "_reserved_wh"] > 0
                         or (battery_delta if name == "battery" else grid_delta) > 0)]
        if state["cycle_owned"] and exhausted:
            state["energy_guard_pending"] = True
            state["energy_guard_sources"] = sorted(set(state.get("energy_guard_sources", [])) | set(exhausted))
        # Nur der Wolkenrückzug darf durch erneut stabilen Überschuss entfallen.
        # Ein Kontingentende, Gerätestopp oder bereits ausgegebener Entzug bleibt bestehen.
        # Eine abgelaufene Startwartefrist entzieht den Sollwert nicht mehr; sie
        # gibt nur die Startreservierung frei (start_reservation_released).
        terminal_reason = protection or ("command_unconfirmed" if command_expired else "")
        terminal_reason = terminal_reason or ("request_withdrawn" if not requested else "")
        if state.get("energy_guard_pending"):
            terminal_reason = terminal_reason or "energy_guard_exhausted"
        if terminal_reason and state["active_command"]:
            state["withdrawal_pending"] = True
            state["withdrawal_reason"] = terminal_reason
        elif state["active_command"] and not qualified and not state["withdrawal_pending"]:
            # Wolkenüberbrückung – erst nach boost_release_s ohne
            # PV-Deckung wird der Sollwert zurückgenommen.
            unqualified_since = _number(state.get("pv_unqualified_since_s"))
            if unqualified_since is None:
                state["pv_unqualified_since_s"] = now
            elif now - unqualified_since >= _number(cfg.get("boost_release_s"), 300.0):
                state["withdrawal_pending"] = True
                state["withdrawal_reason"] = "pv_unqualified"
        elif (state["active_command"] and qualified and not command.get("withdrawal_requested")
              and state.get("withdrawal_reason") == "pv_unqualified"):
            state["withdrawal_pending"] = False
            state["withdrawal_reason"] = ""
        if qualified or not state["active_command"]:
            state["pv_unqualified_since_s"] = None
    else:
        if wait_expired or command_expired or not requested or protection:
            state["withdrawal_pending"] = bool(state["active_command"])
        if state["active_command"] and (not qualified or battery_remaining + grid_remaining <= 0):
            # Eine normale Optimierungsgrenze ist kein vorzeitiger Schutzabbruch.
            state["withdrawal_pending"] = True
    hold_required = bool(state["cycle_owned"] and protected > 0 and not protection)
    withdrawal_required = bool(state["withdrawal_pending"] and (protected <= 0 or protection))
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
            if source_enabled["battery"]:
                battery_energy = _number(source.get("battery_available_wh"), 0.0)
            if source_enabled["grid"]:
                grid_energy = grid_cap * energy_horizon / 3600.0
    battery_cap = min(battery_cap, battery_energy * 3600.0 / energy_horizon)
    grid_cap = min(grid_cap, grid_energy * 3600.0 / energy_horizon)
    if (measured and state["active_command"] and not protection
            and cfg.get("valid") and data_fresh and source_fresh
            and measured_observation_valid and clock_ok and not corrupt
            and not state.get("uncertain_state")):
        # Der kurze Antwortpuffer ist eine laufende Quellenbindung. Verbrauch
        # bleibt im 24-h-Konto; dieselbe belegte Reservehöhe ist kein neuer Topf.
        rolling_power = min(capability_w, battery_cap + grid_cap)
        rolling_reserve = _reserve_energy(
            rolling_power, reaction_s,
            battery_cap, battery_energy, grid_cap, grid_energy,
        )
        if rolling_power <= 0:
            rolling_reserve = (0.0, 0.0)
        if rolling_reserve is not None:
            state["battery_reserved_wh"], state["grid_reserved_wh"] = rolling_reserve
        else:
            state["battery_reserved_wh"] = min(state["battery_reserved_wh"], battery_energy)
            state["grid_reserved_wh"] = min(state["grid_reserved_wh"], grid_energy)
    # Im Messwertbetrieb folgt das Budget der Istaufnahme. Der Start wird
    # separat durch die qualifizierte PV-Startleistung gedeckt. Ein Profilwert
    # ist keine zusätzliche dauerhafte PV-Reservierung für einen möglichen Sprung.
    actual = observed_w if observed_w is not None else max(maximum, state["max_power_w"])
    battery_reaction_w = min(battery_cap, _number(source.get("battery_reaction_available_w"), 0.0))
    reaction_reserve = 0.0 if measured else max(0.0, max(maximum, state["max_power_w"]) - actual - battery_reaction_w - grid_cap)
    startup_shared_reserve = start_power if measured else 0.0
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
    else:
        state["last_battery_w"] = state["last_grid_w"] = 0.0
    state["last_ts"] = max(now, state["last_ts"])
    if current_clock is not None:
        state["clock"] = current_clock
    reason = protection or ("protected_minimum_runtime" if hold_required else
             "withdrawal_required" if withdrawal_required else
             "running" if physical is True and state["active_command"] else
             "start_reservation_released" if measured and released and state["active_command"] else
             "waiting_for_compressor" if state["active_command"] else
             "start_candidate" if start_candidate else (blockers[0] if blockers else "idle"))
    return {
        "schema": CONTRACT_SCHEMA, "sample_ts": wall,
        "control_mode": "measured" if measured else "reserved",
        "start_power_w": start_power,
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
        "withdrawal_reason": state.get("withdrawal_reason", ""),
        "battery_response_required_wh": battery_response_wh,
        "battery_response_required_w": battery_response_w,
        "quarantine_remaining_s": state.get("quarantine_remaining_s", 0.0),
        "window_battery_used_wh": window_battery, "window_grid_used_wh": window_grid,
        "energy_reservation_power_w": reserve_power,
        "energy_reservation_required_wh": reserve_power * reserve_duration / 3600.0,
        "measurement_uncertain": gap or not data_fresh or not source_fresh,
        "reaction_reserve_w": reaction_reserve, "max_power_w": maximum,
        "startup_shared_reserve_w": startup_shared_reserve,
        "handoff_timeout_s": _number(cfg.get("handoff_timeout_s"), 120.0),
        "command_timeout_s": _number(cfg.get("command_timeout_s"), 25.0),
        "start_reservation_released": bool(measured and released),
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
        # Ein PV-Start braucht die gesamte Startleistung aus dem zugeteilten
        # Überschuss. Überbrückung darf fehlende PV-Startleistung nicht ersetzen.
        funded = funded and shared >= _number(result.get("startup_shared_reserve_w"), 0.0)
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
        if _number(state.get("reservation_started_ts")) is None:
            state["reservation_started_ts"] = now
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
