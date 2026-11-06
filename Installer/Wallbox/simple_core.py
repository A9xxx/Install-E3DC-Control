"""Ein Wallbox-Regler auf dem zugeteilten Speicherbudget, ohne Ein-/Ausgabe.

Alle Zeiten sind monotone Sekunden aus dem Aufrufer. ``decide`` pflegt nur
Qualifizierungszeiten. Erst ``acknowledge`` übernimmt quittierte Ausgänge.
Das Budget ist absolut: die eigene Ladeleistung wird niemals abgezogen.
"""
import math

from .phase_transition import planned_reservation_power_w


def number(value):
    """Unbekannte Werte bleiben unbekannt, insbesondere bool und NaN."""
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return result if math.isfinite(result) else None


def _floor(value, step):
    return round(math.floor((value + 1e-9) / step) * step, 6)


def _since(state, key, now, active):
    if not active:
        state.pop(key, None)
        return 0.0
    if number(state.get(key)) is None:
        state[key] = now
    return max(0.0, now - state[key])


def support_authorized(snapshot):
    """Speicher-Veto vor Kurvenklasse; keine Freigabe aus fehlendem SoC."""
    if snapshot.get("battery_support_authorized") is False:
        return False
    mode = snapshot.get("mode")
    curve = snapshot.get("curve_support") or {}
    if mode in (4, 5, 12):
        return snapshot.get("soc_valid") is True and snapshot.get("floor_open") is True
    if mode == 3:
        return snapshot.get("explicit_support") is True
    if mode == 2:
        if curve.get("contingent_exhausted") is True:
            return False
        return bool(curve.get("authorized") is True and (
            curve.get("relation") != "below_floor" or (
                curve.get("contingent_applicable") is True
                and curve.get("contingent_exhausted") is not True
                and (number(curve.get("support_wh_remaining")) or 0) > 0)))
    return False


def energy_source(snapshot):
    """Anteilsquelle und Haltestützung sind getrennte Wahrheiten."""
    if snapshot.get("observe_only") is True or snapshot.get("mode") == 0:
        return None
    if snapshot.get("local_grid_allowed") is True:
        return "grid"
    if (snapshot.get("mode") in (4, 5, 12)
            and snapshot.get("soc_valid") is True
            and snapshot.get("floor_open") is True
            and snapshot.get("battery_support_authorized") is True):
        return "battery"
    return "pv"


def energy_shares(boxes, budget_w, budget_pv_w, distribution=0):
    """Absolute Anteile: Netz ohne Budget, Akku zuerst, danach verbleibende PV.

    Die elektrische Prüfung mit Phasenlage erfolgt anschließend im Zuteiler.
    Der Speicher zählt Netzboxen im Gesamtbudget mit. Ihre frische
    Messleistung wird dort genau einmal abgezogen, niemals vom PV-Rahmen.
    """
    total = number(budget_w)
    pv = number(budget_pv_w)
    result = {box["id"]: dict(source=energy_source(box), share_w=0.0) for box in boxes}
    grid_power = measured_grid_power([box for box in boxes
        if result[box["id"]]["source"] == "grid"])
    if total is not None:
        total = max(0.0, total-grid_power) if grid_power is not None else (
            min(total, pv) if pv is not None else None)

    def limits(box):
        phases = box.get("phases")
        lo, hi = number(box.get("min_amp")), number(box.get("max_amp"))
        if phases not in (1, 2, 3) or lo is None or hi is None:
            return None
        return lo*230*phases, max(0.0, hi)*230*phases

    def distribute(group, available):
        if available is None:
            for box in group:
                result[box["id"]]["share_w"] = None
            return 0.0
        available = max(0.0, available)
        for box in group:
            if limits(box) is None:
                result[box["id"]]["share_w"] = None
        group = [box for box in group if limits(box) is not None
                 and box.get("eligible", True) is True]
        from .distribution import order_key
        group.sort(key=lambda box: order_key(box, distribution))
        if distribution not in (1, 2) and group and available >= sum(limits(box)[0] for box in group):
            remaining = list(group)
            # Gleiche Wattanteile, soweit die unterschiedlichen Minima und Maxima es erlauben.
            while remaining:
                equal = available/len(remaining)
                bound = next((box for box in remaining if equal < limits(box)[0]
                              or equal >= limits(box)[1]), None)
                if bound is None:
                    for box in remaining:
                        result[box["id"]]["share_w"] = equal
                    break
                minimum, maximum = limits(bound)
                value = minimum if equal < minimum else maximum
                result[bound["id"]]["share_w"] = value
                available -= value
                remaining.remove(bound)
        else:
            for box in group:
                minimum, maximum = limits(box)
                # Ohne weiteren Empfänger bleibt das Restangebot sichtbar:
                # Der vorhandene Phasenautomat kann damit den Abstieg qualifizieren.
                alone = sum(box.get("eligible", True) is True and energy_source(box) is not None for box in boxes) == 1
                value = min(maximum, available) if available >= minimum or alone else 0.0
                result[box["id"]]["share_w"] = value
                available -= value
        return sum(result[box["id"]]["share_w"] or 0 for box in group)

    for box in boxes:
        if result[box["id"]]["source"] == "grid" and box.get("eligible", True) is True:
            result[box["id"]]["share_w"] = limits(box)[1] if limits(box) is not None else None
    used = distribute([box for box in boxes if result[box["id"]]["source"] == "battery"], total)
    distribute([box for box in boxes if result[box["id"]]["source"] == "pv"],
               max(0.0, pv-used) if pv is not None else None)
    return result


def unplug_offer_target(state, now, *, safe, grid, budget_w, phases, minimum,
                        budget_valid, previous_target):
    """Stehendes Angebot mit den vorhandenen Start- und Unterdeckungsuhren."""
    if not safe or (not grid and not budget_valid) or phases not in (1, 2, 3):
        state.pop("start_since", None)
        state.pop("under_since", None)
        return 0.0
    available = number(budget_w)
    covered = bool(grid or (available is not None and available >= minimum*230*phases))
    qualified = _since(state, "start_since", now, covered)
    under = _since(state, "under_since", now, not covered)
    if grid or (covered and qualified >= 60):
        return minimum
    if previous_target > 0 and (covered or under < 180):
        return minimum
    return 0.0


def measured_grid_power(grid_boxes):
    """Eindeutige frische Messsumme; unbekannt bleibt unbekannt."""
    power = 0.0
    seen = set()
    for box in grid_boxes:
        if box["id"] in seen:
            continue
        seen.add(box["id"])
        age, watts = number(box.get("status_age_s")), number(box.get("power_w"))
        if box.get("status_valid") is not True or age is None or not 0 <= age <= 15 or watts is None or watts < 0:
            return None
        power += watts
    return power


def permitted_grid_import(grid_w, grid_boxes, now):
    """Nur frische gemessene Netzboxleistung abziehen; sonst roher Netzwert."""
    grid = number(grid_w)
    settling = any(box.get("current_acknowledged", True) is not True for box in grid_boxes)
    power = measured_grid_power(grid_boxes)
    return (grid-power if grid is not None and power is not None else grid), settling


def stop_display(reason):
    """Anzeigetext und Abzeichen; der technische Stoppgrund bleibt intern."""
    labels = {
        "user_off": ("Nutzer-Aus: Ladung angehalten", "protection"),
        "manual_pause": ("Ladung manuell pausiert", "protection"),
        "emergency_stop": ("Not-Aus: Ladung bleibt gesperrt", "protection"),
        "electrical_limit": ("Hausanschluss oder Gerätegrenze: Ladung beenden", "protection"),
        "data_timeout": ("Messdaten oder Ladebudget fehlen: Ladung beenden", "protection"),
        "budget_timeout": ("Gültiges Ladebudget fehlt: Ladung beenden", "protection"),
        "soc_unknown": ("Ladezustand nicht lesbar: Ladung beenden", "protection"),
        "no_vehicle": ("Kein Fahrzeug verbunden", "idle"),
        "no_vehicle_connected": ("Kein Fahrzeug verbunden", "idle"),
        "observe": ("Nur beobachten", "idle"),
        "mode0": ("Nur beobachten", "idle"),
        "window_end": ("Ladefenster beendet", "stopped"),
        "grid_window_end": ("Ladefenster beendet", "stopped"),
        "departure_window": ("Außerhalb des Abfahrtsfensters", "idle"),
        "target_reached": ("Ladeziel erreicht", "idle"),
        "target_kwh_reached": ("Ladeziel erreicht", "idle"),
        "target_soc_reached": ("Ladeziel erreicht", "idle"),
        "support_ended": ("Speicherunterstützung beendet", "stopped"),
        "simple_core_stop": ("Zu wenig Ladeleistung: Ladung beenden", "stopped"),
    }
    return labels.get(reason, ("Ladung beendet", "idle"))


def decide(snapshot, state, now, previous_snapshot=None):
    """Liefert höchstens eine Aktion; elektrische und Nutzergrenzen gelten sofort.

    Pflichtwerte tragen ihre Gültigkeit im Schnappschuss. ``share_w`` ist der
    Anteil des vorhandenen Zuteilers in den beobachteten Phasen. ``cap_amp``
    ist dessen elektrischer Deckel, kein zweites Leistungsbudget.
    """
    now = float(now)
    s = snapshot
    if (s.get("connected") is False or (previous_snapshot
            and s.get("plug_session_id")
            and s.get("plug_session_id") != previous_snapshot.get("plug_session_id"))):
        state.pop("charge_seen_ts", None)
        if snapshot.get("unplug_offer_active") is not True:
            state.pop("under_since", None)
    phases = s.get("phases") if s.get("phases") in (1, 2, 3) else None
    minimum = number(s.get("min_amp"))
    maximum = number(s.get("max_amp"))
    step = number(s.get("step_amp"))
    cap = number(s.get("cap_amp"))
    house = number(s.get("house_fuse_cap_amp"))
    share = number(s.get("share_w"))
    if s.get("source") == "grid" and maximum is not None and phases:
        # Netzladen nutzt den gültigen elektrischen Rahmen unabhängig vom PV-Budget.
        share = maximum * 230 * phases
    offered = number(s.get("offered_amp"))
    power = number(s.get("power_w"))
    if (s.get('native') is True and s.get('abort_restart_seen')
            and state.get('stop_reason') == 'window_end'
            and s.get('local_grid_allowed') is not True):
        # Das beendete Fenster bleibt beim beobachteten Fremdstart beendet,
        # auch wenn die physische Netzsitzung inzwischen abgeschlossen ist.
        s = dict(s, window_ended=True)
    # Nicht-native Ausgänge sind idempotent. Native Flanken und ihr Rückfall
    # teilen das vom Gerätezustand geführte Tor, unabhängig von der Leistung.
    last_stop = number(state.get("last_stop_ts"))
    proof_ts = number(s.get("charge_proof_ts"))
    retry_gap = number(s.get("stop_retry_gap_s"))
    retry_due = bool(s.get('native') is not True and state.get("stop_acknowledged") and last_stop is not None
        and retry_gap is not None and retry_gap > 0 and now-last_stop >= retry_gap
        and proof_ts is not None and proof_ts >= last_stop+retry_gap
        and proof_ts <= now and s.get("fresh_charge_proof") is True
        and s.get("status_valid") is True and s.get("charging") is True)
    takeover = bool(s.get('native') is True and s.get('abort_restart_seen')
                    and state.get('stop_reason') in ('simple_core_stop', 'support_ended'))
    if takeover and s.get('charging') is True:
        # Der beobachtete Fremdstart wird zur normalen Sitzung. Die vorhandenen
        # Qualifizierungen beginnen am beobachteten Ladebeginn, nicht an der
        # früheren EMS-Sitzung. Die Übernahme weiter unten bleibt unverändert.
        state.update(stop_acknowledged=False, session_active=True)
        if offered is not None:
            state['output_amp'] = offered
        if not state.get('external_takeover'):
            state['last_start_ts'] = now
            state['external_takeover'] = True
        state.get('native_edges', {}).update(event={}, own_stop_confirmed=False, restart_seen=False)
    active = bool((s.get("charging") is True)
                  or state.get("session_active") or s.get("phase_in_progress") is True)
    if "session_active" not in state and s.get("status_valid") is True:
        # Nur frische Beobachtung übernehmen, nie den alten Manager-Sollwert.
        state["session_active"] = active
        if active:
            state["last_start_ts"] = now
    base = number(state.get("output_amp"))
    if (state.get("phase_resume_pending") and s.get("status_valid") is True
            and s.get("charging") is not True and offered is not None):
        base = offered
        state["output_amp"] = base
    if base is None:
        base = offered if s.get("status_valid") is True else None
        if base is None and phases and power is not None and s.get("status_valid") is True:
            base = round(power / (230 * phases))
        if base is not None:
            state["output_amp"] = base

    def result(action="none", target=None, reason="budget", sentence=None, status="idle", target_phases=None):
        if (s.get('native') is True and s.get('abort_start_retry_allowed') is True
                and action in ('none', 'set_current') and reason in ('budget', 'cycle_wait')
                and not s.get('phase_in_progress') and not state.get('phase_resume_pending')
                and share is not None and phases and share >= minimum * 230 * phases):
            # Der vorhandene Easy-Connect-Vertrag besitzt Anzahl und Abstand.
            # Erst nach allen normalen Budget- und Schutzentscheidungen wird
            # daraus wieder ein expliziter Startauftrag durch denselben Ausgang.
            action, reason = 'start', 'native_start_retry'
            target = min(ceiling, max(minimum, _floor(share / (230 * phases), step)))
            sentence, status = 'Freigabe der Box erneut anfordern', 'start_wait'
        started = number(state.get("last_start_ts"))
        if (active and s.get("charging") is not True and s.get("connected") is True
                and not s.get("phase_in_progress") and not state.get("phase_resume_pending")
                and started is not None and now-started >= 30
                and action in ("none", "set_current") and reason in ("budget", "cycle_wait")):
            reason, sentence, status = "vehicle_wait", "Fahrzeug folgt dem Ladeangebot nicht; kein Ladebeginn gemeldet", "hold"
        if reason == "cycle_wait":
            sentence = state.get("last_real_sentence") or sentence
        else:
            state["last_real_sentence"] = sentence or reason
        return {"action": action, "target_amp": target,
                "target_phases": target_phases if target_phases is not None else phases,
                "reason": reason, "sentence": sentence or reason, "state": status}

    def stop(reason, protection=True):
        text, display_state = stop_display(reason)
        state.pop("start_since", None)
        if not active:
            for key in ("reserve_phases", "phase_target", "phase_since"):
                state.pop(key, None)
        if s.get('native') is True:
            if s.get('abort_stop_confirmed'):
                return result('stop_confirmed', 0, reason, text, display_state)
            if (state.get('external_takeover') and not protection and active
                    and (_since(state, 'under_since', now, True) < 180
                         or now - state.get('last_start_ts', now) < 300)):
                return result('set_current', minimum, reason, 'Übernommene Ladung: Mindestladezeit und Unterdeckung abwarten', 'hold')
            if (s.get('abort_stop_allowed') is not True
                    and s.get('abort_error') != 'native_stop_ineffective'):
                return result(reason=reason,
                              sentence=('Stopp wartet auf frischen Zustand der Box'
                                  if s.get('status_valid') is not True or s.get('abort_fresh') is not True else text),
                              status='protection' if display_state == 'protection' else 'stop_wait')
            return result('stop', 0, reason, text, display_state)
        if (not active and not retry_due) or (state.get("stop_acknowledged") and not retry_due):
            return result(reason=reason, sentence=text, status=display_state)
        return result("stop", 0, reason, text, display_state)

    # Nutzer-Aus wird für beobachtete Rollen vom vorgeschalteten Pausenpfad
    # quittiert. Der Kern eröffnet dafür keinen zweiten Stopppfad.
    if s.get("mode") == 0 or s.get("observe_only") is True:
        return result(reason="observe", sentence="Nur beobachten")
    # Sicherheitsnetz für Schranke 1; der Pausenpfad quittiert Nutzer-Aus zuerst.
    if s.get("user_off") is True:
        return stop("user_off")
    if s.get("departure_blocked") is True:
        return stop("departure_window")
    if s.get("window_ended") is True or s.get("target_reached") is True:
        return stop("window_end" if s.get("window_ended") else "target_reached")
    if s.get("budget_timeout") is True and s.get("source") != "grid":
        return stop("budget_timeout")
    if (s.get("unplug_offer_active") is True and s.get("connected") is False
            and s.get("charging") is not True):
        # Der Kern führt im Leerlauf schon das stehende Angebot mit genau
        # diesen Uhren. Die spätere Einzelbox-Beobachtung qualifiziert es
        # nicht nochmals und löscht es auch nicht als Phasenfehler.
        state["session_active"] = False
        for key in ("reserve_phases", "phase_target", "phase_since"):
            state.pop(key, None)
        return result(reason="no_vehicle", sentence="Kein Fahrzeug verbunden")
    limits_unknown = (minimum is None or maximum is None or step is None or step <= 0
        or minimum <= 0 or maximum < 0 or phases is None or cap is None or house is None)
    invalid = s.get("live_valid") is not True or s.get("status_valid") is not True
    # Alle fehlenden Kerneingänge teilen dieselbe Datenfrist. Ein Wechsel
    # der Fehlerart darf weder verlängern noch einen alten Anteil freigeben.
    data_invalid = (invalid or share is None or limits_unknown
        or (s.get("source") != "grid" and s.get("budget_valid") is not True))
    # Eine kurze gültige Probe beendet die bestehende Datenfrist nicht.
    # Die Erholung nutzt dieselben zehn Sekunden wie der kurze Datenhalt.
    if data_invalid:
        state.pop("data_valid_since", None)
    elif number(state.get("invalid_since")) is not None:
        data_invalid = _since(state, "data_valid_since", now, True) < 10
        if not data_invalid:
            state.pop("data_valid_since", None)
    invalid_age = _since(state, "invalid_since", now, data_invalid)
    soc_invalid_age = _since(state, "soc_invalid_since", now,
                             s.get("mode") == 4 and s.get("soc_valid") is not True)
    # Bekannte elektrische Grenzen haben Vorrang vor der Datenfrist.
    # Slot 0 ist Unterdeckung, kein elektrischer Schutzstopp.
    if not limits_unknown:
        hard_ceiling = _floor(min(maximum, house), step)
        ceiling = min(hard_ceiling, _floor(max(minimum, cap) if active else cap, step))
        if hard_ceiling < minimum:
            return stop("electrical_limit")
    if data_invalid and invalid_age >= 300:
        return stop("data_timeout")
    if limits_unknown:
        state.pop("start_since", None)
        state.pop("phase_since", None)
        state.pop("phase_target", None)
        state.pop("reserve_phases", None)
        return result(reason="limits_unknown", sentence="Gerätegrenzen oder Phasen unbekannt", status="protection")
    if active and base is not None and base > hard_ceiling + 1e-6:
        return result("set_current", ceiling, "electrical_limit", "Hausanschluss oder Gerätegrenze: Strom absenken", "protection")
    if soc_invalid_age >= 10 and s.get("mode") == 4 and s.get("soc_valid") is not True:
        return stop("soc_unknown")
    if s.get("connected") is not True:
        state.pop("start_since", None)
        state["session_active"] = False
        for key in ("reserve_phases", "phase_target", "phase_since"):
            state.pop(key, None)
        return result(reason="no_vehicle", sentence="Kein Fahrzeug verbunden")
    if data_invalid:
        phase_missing = s.get("house_phase_source") == "phases_missing"
        phase_sentence = "Keine Phasenwerte vom E3DC-Leistungsmesser – Zuordnung des Wurzelzählers prüfen"
        state.pop("rest_since", None)
        state.pop("start_since", None)
        state.pop("phase_since", None)
        state.pop("phase_target", None)
        state.pop("reserve_phases", None)
        if (s.get("phase_in_progress") is True or state.get("phase_resume_pending")) and phase_missing:
            return result(reason="data_stale", sentence=phase_sentence, status="hold")
        if number(state.get("data_valid_since")) is not None or (not phase_missing and invalid and share is not None and invalid_age < 10):
            return result(reason="data_hold", sentence=phase_sentence if phase_missing else "Kurzer Datenhalt ohne Änderung", status="hold" if active else "idle")
        action = "set_current" if active and base is not None and base > minimum + 1e-6 else "none"
        return result(action, minimum if active else None, "data_stale", phase_sentence if phase_missing else "Veraltete Daten: Mindeststrom halten", "hold" if active else "idle")

    support = support_authorized(s)
    one_phase_need = planned_reservation_power_w(
        restart_amp=minimum, target_phases=1, current_step_amp=step)
    target_phases = s.get("phase_target") if s.get("phase_target") in (1, 3) else phases
    target_need = one_phase_need if target_phases == 1 and target_phases != phases else minimum * 230 * target_phases
    # Mindesthaltezeit nach einem Phasenwechsel ist die vorhandene Phasensperre
    # der Box (höchstens 480 s): keine Unterdeckungsstopps, nur Schutzstopps.
    phase_hold = s.get("phase_hold") is True
    # Endet die Akkustützung, gilt die normale Unterdeckung: 180 s und
    # Mindestladezeit wie die Ausschaltverzögerung bei evcc, kein Sofortstopp.
    if (state.get("stop_acknowledged") and share < target_need
            and (state.get("stop_reason") == "simple_core_stop" or not support)):
        # Ein weiter anstehender Unterdeckungsstopp bleibt dieselbe Episode,
        # auch wenn die erste Quittung die Qualifizierungsuhr geleert hat.
        return stop("simple_core_stop" if support else "support_ended", False)
    if (state.get("stop_acknowledged") and s.get("status_valid") is True
            and s.get("charging") is True):
        # Der Stoppgrund ist entfallen: laufende Sitzung übernehmen, ohne
        # daraus einen Startbefehl oder eine neue Wiederanlaufsperre abzuleiten.
        state.update(stop_acknowledged=False, session_active=True)
        state.pop("stop_reason", None)
        if offered is not None:
            base = offered
            state["output_amp"] = offered

    # Optimierung genau im 10-s-Takt; Schutzstopps und Deckel stehen davor.
    last_tick = number(state.get("last_tick"))
    if (last_tick is not None and now - last_tick < 10 - 1e-6
            and not (not active and s.get("source") == "grid")):
        return result(reason="cycle_wait", sentence="Nächsten Regeltakt abwarten", status=state.get("state", "idle"))
    if last_tick is not None and now - last_tick > 15:
        state.pop("start_since", None)
        state.pop("phase_since", None)
        state["import_count"] = 0
    state["last_tick"] = now
    watts_per_amp = 230 * phases
    need = minimum * watts_per_amp
    minimum_time = max(300.0, number(s.get("min_charge_time_s")) or 300.0)
    rest_age = _since(state, "rest_since", now,
        s.get("connected") is True and s.get("charging") is not True and not s.get("phase_in_progress")
        and not state.get("phase_resume_pending") and not state.get("stop_acknowledged"))
    if rest_age >= minimum_time:
        state.pop("charge_seen_ts", None)
        state.pop("under_since", None)
    if not active:
        if number(state.get("charge_seen_ts")) is None:
            state.pop("under_since", None)
        # Ein neuer Start benötigt die vorhandenen 60 s Budgetqualifizierung;
        # eine zusätzliche Wiederanlaufsperre entfällt.
    if active:
        # Die bestehenden Schutz- und Sequenzpfade bleiben vor der Phasenwahl.
        if s.get("phase_in_progress") is True:
            return result("set_phases", minimum, "phase_sequence", "Phasensequenz fortsetzen", "hold", s.get("phase_target"))
        if s.get("charging") is True:
            state.pop("phase_resume_pending", None)
            state.pop("phase_resume_target", None)
    if not active:
        qualified = _since(state, "start_since", now, share >= target_need
            and (ceiling >= minimum or target_phases != phases)
            and (s.get("mode") != 4 or s.get("soc_valid") is True))
    phase_target = target_phases if target_phases != phases else None
    if s.get("phase_capable") is True and phase_target and not invalid and s.get("phase_ready") is True:
        if state.get("phase_target") != phase_target:
            state.pop("phase_since", None)
        state["phase_target"] = phase_target
        age = _since(state, "phase_since", now, True)
        # Reservierung ab der ersten Qualifizierungsprobe, keine zweite Uhr.
        state["reserve_phases"] = phase_target
        if (qualified >= 60 if not active else age >= 60) and s.get("phase_ready") is True and (phase_target == 1 or s.get("phase_grant") is True):
            return result("set_phases", minimum, "phase_qualified", "Phasenwechsel qualifiziert", "charging", phase_target)
    else:
        state.pop("phase_since", None)
        state.pop("phase_target", None)
        state.pop("reserve_phases", None)
    if not active:
        if phases != target_phases:
            return result(reason="start_wait", sentence="Startqualifizierung und Phasenwahl laufen gemeinsam", status="start_wait")
        if share < need or ceiling < minimum or (s.get("mode") == 4 and s.get("soc_valid") is not True):
            return result(reason="start_budget", sentence="Budget reicht noch nicht zum Start")
        if qualified < 60 and s.get("source") != "grid":
            return result(reason="start_wait", sentence="Start in %d s, Budget %.0f W ≥ %.0f W" % (math.ceil(60-qualified), share, need), status="start_wait")
        return result("start", min(ceiling, max(minimum, _floor(share/watts_per_amp, step))),
                      "start_qualified", "Start mit Netzfreigabe" if s.get("source") == "grid" else "Start nach 60 s Budgetfreigabe", "start_wait")

    # Kurze Fahrzeugpausen gehören zur selben Ladeepisode.
    # Ein ausstehender Stopp eröffnet keine neue Mindestladezeit.
    if (s.get("charging") is True and not state.get("stop_acknowledged")
            and not state.get("stop_reason")):
        state.setdefault("charge_seen_ts", now)
    # Eine Fahrzeugpause unterbricht eine laufende Unterdeckung nicht.
    under_age = _since(state, "under_since", now, share < need or (
        s.get("charging") is not True and number(state.get("under_since")) is not None))
    if share < need:
        started = number(state.get("charge_seen_ts"))
        if started is None:
            started = number(state.get("last_start_ts"))
        if (under_age >= 60 and phases == 3 and target_phases == 1
                and s.get("phase_capable") is True and s.get("phase_ready") is True
                and not invalid and share >= one_phase_need):
            # Die Unterdeckung bleibt auch nach einem kurzen Datenhalt gültig.
            # Das Phasenziel stammt weiterhin aus der gemeinsamen Verteilung.
            state["phase_target"] = state["reserve_phases"] = 1
            return result("set_phases", minimum, "phase_qualified", "Phasenwechsel qualifiziert", "charging", 1)
        if under_age >= 180 and started is not None and now-started >= minimum_time and not phase_hold:
            return stop("simple_core_stop", False)
        target = minimum
        state_name = "hold" if support or phase_hold else "stop_wait"
        reason = "support_hold" if support else "phase_hold" if phase_hold else "support_ended"
        sentence = ("Halten am Mindeststrom: Akkustützung freigegeben" if support
            else "Mindesthaltezeit nach Phasenwechsel: Mindeststrom halten" if phase_hold
            else "Mindeststrom bis zum qualifizierten Abstieg oder Unterdeckungsstopp halten")
    else:
        target = min(ceiling, max(minimum, _floor(share/watts_per_amp, step)))
        state_name, reason = "charging", "budget"
        sentence = "Budget %.0f W → %g A (%dp)" % (share, target, phases)

    grid = number(s.get("grid_w"))
    settling = bool(s.get("current_acknowledged", True) is not True
                    or s.get("grid_box_settling") is True)
    high = grid is not None and grid > 300 and not settling and s.get("source") != "grid"
    if s.get("source") == "grid":
        state.pop("import_cap_amp", None)
    state["import_count"] = state.get("import_count", 0) + 1 if high else 0
    low_age = _since(state, "grid_low_since", now, grid is not None and grid < 300)
    if "import_cap_amp" in state and low_age >= 30:
        state.pop("import_cap_amp", None)
    if state["import_count"] >= 2 and "import_cap_amp" not in state and power is not None:
        state["import_cap_amp"] = max(minimum, _floor((power-grid)/watts_per_amp, step))
    import_cap = number(state.get("import_cap_amp"))
    if import_cap is not None:
        target = min(target, import_cap)
        reason, sentence = "grid_correction", "Netzbezug: Strom begrenzen bis 30 s Beruhigung"

    if base is None:
        return result(reason="offer_unknown", sentence="Frisches Stromangebot fehlt", status="hold")
    unrounded_cap = number(s.get("unrounded_cap_amp"))
    unrounded_ceiling = ceiling if unrounded_cap is None else min(hard_ceiling, max(minimum, unrounded_cap))
    unrounded = min(unrounded_ceiling, max(minimum, share/watts_per_amp))
    if import_cap is not None or share < need:
        unrounded = target
    # Die Netzkorrektur erfüllt ihre eigene 300-W-Schwelle auch dann,
    # wenn das feinere Geräteraster weniger als 1 A absenkt.
    correction = import_cap is not None and target < base
    resume = bool(state.get("phase_resume_pending") and s.get("charging") is not True
                  and phases == state.get("phase_resume_target"))
    if resume:
        reason, sentence = "phase_resume", "Phasenwechsel bestätigt: Ladung fortsetzen"
    action = "set_current" if resume or (target != base and (correction or abs(unrounded-base) >= 1-1e-6)) else "none"
    return result(action, target, reason, sentence, state_name)


def acknowledge(state, decision, now, executed_amp=None):
    """Nur nach neuem Ausgangsbeleg aufrufen, niemals nach einem Versuch."""
    action = decision["action"]
    if action in ("start", "set_current"):
        amp = number(executed_amp)
        if amp is None or amp <= 0:
            return
        previous = number(state.get("output_amp"))
        if action == "start" or previous != amp or decision.get("reason") == "phase_resume":
            phases = decision.get("target_phases")
            old_phases = (state.get("current_ack") or {}).get("phases", phases)
            prior_ack = state.get("current_ack") or {}
            # Ein unquittierter Vorbefehl ist kein physischer Stand.
            # Bis zur Quittung bleibt der zuletzt quittierte Vektor maßgeblich.
            previous_local = (_current_vector(0.0 if action == "start" else previous, old_phases)
                if action == "start" or prior_ack.get("acknowledged") is not False
                else prior_ack.get("previous_local_a"))
            state["current_ack"] = dict(sent_ts=float(now), phases=phases,
                previous_local_a=previous_local, acknowledged=False)
        state["output_amp"] = amp
        if decision.get("reason") == "electrical_limit":
            # Der Schutz senkt sofort ab; die nächste Optimierung beginnt
            # danach frühestens im nächsten vorhandenen 10-s-Takt.
            state["last_tick"] = float(now)
        state.pop("phase_resume_pending", None)
        state.pop("phase_resume_target", None)
        if action == "start":
            state.update(session_active=True, last_start_ts=float(now), stop_acknowledged=False)
            state.pop("start_since", None)
            state.pop("charge_seen_ts", None)
            state.pop("under_since", None)
            state.pop("stop_reason", None)
    elif action == "stop":
        phases = decision.get("target_phases") or (state.get("current_ack") or {}).get("phases")
        state["current_ack"] = dict(sent_ts=float(now), phases=phases,
            previous_local_a=_current_vector(number(state.get("output_amp")), phases), acknowledged=False)
        state.update(session_active=False, output_amp=0.0, last_stop_ts=float(now),
                     stop_acknowledged=True, stop_reason=decision.get("reason"))
        state.pop("phase_resume_pending", None)
        state.pop("phase_resume_target", None)
        state.pop("under_since", None)
        state.pop("charge_seen_ts", None)
        for key in ("reserve_phases", "phase_target", "phase_since"):
            state.pop(key, None)
    elif action == "set_phases":
        state.pop("phase_since", None)
        state.pop("phase_target", None)
        state.pop("reserve_phases", None)
        state["phase_resume_pending"] = True
        state["phase_resume_target"] = decision.get("target_phases")
        target = decision.get("target_phases")
        state["current_ack"] = dict(sent_ts=float(now), phases=target,
            previous_local_a=_current_vector(number(state.get("output_amp")), 3 if target == 1 else 1),
            acknowledged=False)


def _current_vector(amp, phases):
    if amp == 0:
        return (0.0, 0.0, 0.0)
    if amp is None or phases not in (1, 2, 3):
        return None
    return tuple(max(0.0, amp) if index < phases else 0.0 for index in range(3))


def acknowledge_measurement(state, measured_local_a, sample_ts, now):
    """Abruf nach dem Ausgang: näher am neuen Vektor oder 30 s eingeschwungen."""
    mark = state.get("current_ack")
    if not isinstance(mark, dict):
        return True
    if mark.get("acknowledged") is True:
        return True
    sent = number(mark.get("sent_ts"))
    sample = number(sample_ts)
    target = _current_vector(number(state.get("output_amp")), mark.get("phases"))
    previous = mark.get("previous_local_a")
    if sent is None or sample is None or not sent < sample <= now:
        return False
    if now-sent >= 30:
        mark["acknowledged"] = True
        mark["sample_ts"] = sample
        return True
    if target is None or previous is None:
        return False
    closer = (sum(abs(value-goal) for value, goal in zip(measured_local_a, target))
        < sum(abs(value-goal) for value, goal in zip(measured_local_a, previous)))
    mark["acknowledged"] = bool(closer)
    if closer:
        mark["sample_ts"] = sample
    return mark["acknowledged"]


def checkpoint(state, now, wall_now):
    """Kleiner Zusatz zur vorhandenen Zustandsdatei, ohne Uhrabhängigkeit."""
    result = {"saved_wall_ts": float(wall_now), "saved_monotonic_ts": float(now)}
    for key in ("last_stop_ts", "last_start_ts", "invalid_since", "soc_invalid_since"):
        value = number(state.get(key))
        if value is not None:
            result[key + "_age_s"] = max(0.0, now-value)
    return result


def restore(saved, now, wall_now):
    """Alte Dateien ohne Kernfelder bleiben gültig; Uhrsprung öffnet kein Tor."""
    if not isinstance(saved, dict):
        return {}
    elapsed = number(wall_now)
    stamp = number(saved.get("saved_wall_ts"))
    elapsed = elapsed-stamp if elapsed is not None and stamp is not None else 0
    if elapsed < 0 or elapsed > 86400:
        elapsed = 0
    result = {}
    for key in ("last_stop_ts", "last_start_ts", "invalid_since", "soc_invalid_since"):
        age = number(saved.get(key + "_age_s"))
        if age is not None and age >= 0:
            result[key] = float(now) - age - elapsed
    return result
