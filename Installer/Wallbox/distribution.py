"""Gemeinsame Phasenwahl; Energieanteile und elektrische Kürzung bleiben getrennt.

Keine Ein-/Ausgabe, keine eigene Uhr. Der Manager liefert einen Schnappschuss;
die vorhandenen Qualifizierungszeiten bleiben im Entscheider je Ladepunkt.
"""
from . import simple_core as core
from .controller import allocate_power
from .phase_balancing import normalize_rotation
from .phase_transition import planned_reservation_power_w


def order_key(box, distribution):
    return (box['id'] != distribution if distribution in (1, 2) else False,
            core.energy_source(box) != 'grid' and not box.get('instant', False),
            not box.get('charging'),
            core.number(box.get('last_start_ts')) if core.number(box.get('last_start_ts')) is not None else float('inf'),
            box['id'])


def phase_vector(amp, phases, rotation):
    rotation = normalize_rotation(rotation)
    indexes = rotation[:phases] if rotation is not None and phases in (1, 2) else range(3)
    return tuple(max(0.0, amp) if index in indexes else 0.0 for index in range(3))


def fresh(box):
    # Dieselbe Messgültigkeit wie im Hausrahmen: unbekannte Boxleistung
    # steckt bereits in held/U und darf nicht nochmals als Box zählen.
    return core.measured_grid_power([box]) is not None


def measured_phase_currents(box):
    """Dieselbe RMS-Quellenwahl wie im lokalen Messvektor des Managers."""
    status = box.get('status') or {}
    local = tuple(core.number(status.get('phase_current_l%d_a' % p)) for p in (1, 2, 3))
    watts = core.measured_grid_power([box])
    return (all(value is not None and value >= 0 for value in local)
           and (sum(local) > 0 or watts is None or watts <= 0))


def reservation_scale(box, power_factor_margin, target_phases=None):
    """Ziele mit derselben oberen Schätzung belegen, die nach dem Start gilt."""
    unknown = (normalize_rotation(box.get('rotation')) is None
               and not box.get('autonomous_can_switch')
               and (box.get('phases') in (1, 2) or target_phases in (1, 2)))
    if not measured_phase_currents(box):
        # Das vorhandene gemessene W/A-Verhältnis umfasst die Ist-Phasen.
        # Ohne Messung bleibt die bisherige nominale Schätzung erhalten.
        measured = core.number(box.get('measured_watts_per_amp'))
        phases = box.get('phases')
        ratio = measured/(230*phases) if measured is not None and phases in (1, 2, 3) else 1.0
        return max(1.0, ratio)/power_factor_margin
    return 1.0/power_factor_margin if unknown else 1.0


def occupied_amp(box, scale=1.0, target_phases=None):
    """Gemessene Belegung vor der Zielskalierung; Schätzung enthält f bereits."""
    measured = core.number(box.get('measured_amp')) or 0.0
    sent = core.number(box.get('sent_amp')) or 0.0
    unknown = (normalize_rotation(box.get('rotation')) is None
               and not box.get('autonomous_can_switch')
               and (box.get('phases') in (1, 2) or target_phases in (1, 2)))
    if measured_phase_currents(box) and unknown:
        measured *= scale
    if unknown:
        sent *= scale
    # Bekannte Rücklesung bleibt max(Schätzung, Rücklesung), ohne weitere Marge.
    return max(measured, sent)/scale


def distribute(boxes, budget_w, budget_pv_w, phase_limits, distribution=0, power_factor_margin=0.9):
    """Aktuelle Kombination und höchstens ein neues Ziel je Kombination.

    Veraltete Boxen stecken bereits im Phasenrahmen. Frische Boxen ohne
    Auftrag bleiben mit gemessenem bzw. gesendetem Strom elektrisch belegt.
    """
    boxes = sorted(boxes, key=lambda box: order_key(box, distribution))
    by_id = {box['id']: box for box in boxes}
    busy = any(box.get('phase_in_progress') or box.get('phase_resume_pending') for box in boxes)
    current, choices = {}, {}
    for box in boxes:
        cid = box['id']
        phases = box.get('phases')
        following = box.get('phase_in_progress') or box.get('phase_resume_pending')
        current[cid] = box.get('sequence_target') if following else phases
        options = [current[cid]]
        if (not following and box.get('eligible') and box.get('phase_ready') is True
                and box.get('phase_capable') is True and phases in (1, 3)):
            if phases == 3:
                options.append(1)
            elif box.get('charging') and not busy:
                options.append(3)
        choices[cid] = options
    combinations = [current]
    for box in boxes:
        for target in choices[box['id']][1:]:
            combinations.append(dict(current, **{}) | {box['id']: target})

    def evaluate(combination):
        energy, counts, floors, scales = [], {}, {}, {}
        observer = [0.0, 0.0, 0.0]
        statuses, modes, maxima, minima, steps, rotations = [], {}, {}, {}, {}, {}
        for box in boxes:
            cid, target = box['id'], combination[box['id']]
            maximum = (box.get('max_amp_by_phase') or {}).get(target)
            minimum = core.number(box.get('min_amp'))
            following = box.get('phase_in_progress') or box.get('phase_resume_pending')
            row = dict(box, phases=target, max_amp=maximum)
            energy.append(row)
            actual = box.get('phases')
            # Die vorhandene Pro-Folge verlässt zero_wait erst nach frischer
            # Nullquittung und erfolgreichem Phasenbefehl. Andere Geräte ohne
            # Nullquittung behalten die Vereinigung bis zur Phasenquittung.
            zero_confirmed = (box.get('_openwb_pro_phase_sequence') or {}).get('stage') in (
                'restart_delay', 'cp_after_phase')
            counts[cid] = 3 if box.get('autonomous_can_switch') else (
                target if following and zero_confirmed else max(actual or 3, target or 3))
            rotations[cid] = box.get('rotation')
            # Ohne Lage belegt eine ein-/zweiphasige Box alle möglichen
            # Phasen einschließlich des vorhandenen Sicherheitsabstands.
            scales[cid] = reservation_scale(box, power_factor_margin, target)
            occupied = occupied_amp(box, scales[cid], target)
            is_fresh = fresh(box)
            if (not box.get('eligible') or core.energy_source(box) is None) and is_fresh and not box.get('unplug_offer'):
                vector = phase_vector(occupied*scales[cid], counts[cid], rotations[cid])
                observer = [a+b for a, b in zip(observer, vector)]
            floors[cid] = ((minimum or 0.0) if zero_confirmed else max(minimum or 0.0, occupied)) if following else occupied if target != actual else (minimum or 0.0) if box.get('charging') and is_fresh else 0.0
            statuses.append(dict(id=cid, status=dict(box.get('status') or {})))
            modes[cid] = box.get('mode', 0) if box.get('eligible') and is_fresh else 0
            maxima[cid], minima[cid], steps[cid] = maximum or 0.0, minimum or 6.0, box.get('step_amp', 1.0)
            # Mindeststrom während der Folge ist elektrische Belegung.
            # Der Energieanteil bleibt der Box zugeordnet; die andere Box
            # erhält ihn nicht vorübergehend und der Wiederanlauf behält Deckung.
            if (box.get('phase_in_progress') or (following and target != box.get('phases'))) and minimum is not None:
                maxima[cid] = min(maxima[cid], minimum)
        shares = core.energy_shares(energy, budget_w, budget_pv_w, distribution)
        # Mindesthalt bleibt eine Entscheidung des Kerns. Der Zuteiler hält
        # dafür den Mindeststrom nur bei bereits laufenden Sitzungen frei.
        electrical_w = {}
        for row in energy:
            cid, phases = row['id'], row['phases']
            share = core.number(shares[cid]['share_w'])
            hold = row.get('charging') and row.get('eligible') and core.support_authorized(row)
            electrical_w[cid] = max(share or 0.0, minima[cid]*230*(phases or 0) if hold else 0.0)
            maxima[cid] = min(maxima[cid], electrical_w[cid]/(230*phases)) if phases else 0.0
        allocation = allocate_power(sum(electrical_w.values()), statuses, distribution,
            max([32.0] + list(maxima.values())), modes,
            {box['id'] for box in boxes if core.energy_source(box) == 'grid'},
            max_amp_by_id=maxima, min_amp_by_id=minima,
            current_step_amp_by_id=steps,
            phase_count_by_id=combination,
            electrical_reservation_phase_count_by_id=counts,
            grid_phase_limit_vector=tuple(max(0.0, a-b) for a, b in zip(phase_limits, observer)),
            phase_rotation_by_id=rotations,
            order_by_id={box['id']: index for index, box in enumerate(boxes)},
            electrical_floor_amp_by_id=floors,
            electrical_reservation_scale_by_id=scales,
            externally_reserved_ids=set(by_id))
        useful, lost = 0.0, 0
        result = {}
        for row in energy:
            cid, target = row['id'], row['phases']
            box = by_id[cid]
            slot = allocation.get(cid) or {}
            amp = slot.get('target_amp', 0.0)
            share = core.number(shares[cid]['share_w'])
            unrounded_cap = slot.get('unrounded_electrical_cap_amp', 0.0)
            maximum = (box.get('max_amp_by_phase') or {}).get(target) or 0.0
            watts = min(share or 0.0, unrounded_cap*230*(target or 0),
                        maximum*230*(target or 0))
            need = minima[cid]*230*(target or 0)
            if target == 1 and target != box.get('phases'):
                need = planned_reservation_power_w(restart_amp=minima[cid], target_phases=1,
                    current_step_amp=steps[cid])
            if watts+1e-6 >= need:
                useful += watts
            elif box.get('charging') and box.get('eligible'):
                lost += 1
            occupied = [0.0, 0.0, 0.0]
            for other in boxes:
                if other['id'] == cid or not fresh(other):
                    continue
                accepted = occupied_amp(other, scales[other['id']], combination[other['id']])
                if other.get('phase_in_progress') or other.get('phase_resume_pending'):
                    accepted = max(accepted, floors[other['id']])
                vector = phase_vector(accepted*scales[other['id']], counts[other['id']], other.get('rotation'))
                occupied = [a+b for a, b in zip(occupied, vector)]
            following = box.get('phase_in_progress') or box.get('phase_resume_pending')
            own_phases = counts[cid] if following or box.get('autonomous_can_switch') else box.get('phases')
            indexes = [p for p, value in enumerate(phase_vector(1, own_phases, box.get('rotation'))) if value]
            physical = max(0.0, min(phase_limits[p]-occupied[p] for p in indexes))/scales[cid]
            # Bis zum Nullsetzen gilt der Anteil auf den Ist-Phasen als
            # Strombefehl. Die Kandidatenbelegung bleibt davon unberührt.
            command_amp = amp
            command_phases = target
            command_unrounded_cap = unrounded_cap
            actual = box.get('phases')
            following = box.get('phase_in_progress') or box.get('phase_resume_pending')
            if (target != actual and not following and actual in (1, 3)
                    and (box.get('charging') or (box.get('core_state') or {}).get('session_active'))):
                command_phases = actual
                reserved = list(observer)
                for other in boxes:
                    oid = other['id']
                    if oid == cid or not other.get('eligible') or not fresh(other) or core.energy_source(other) is None:
                        continue
                    other_amp = max(floors[oid], (allocation.get(oid) or {}).get('target_amp', 0.0))
                    vector = phase_vector(other_amp*scales[oid], counts[oid], other.get('rotation'))
                    reserved = [a+b for a,b in zip(reserved,vector)]
                actual_count = 3 if box.get('autonomous_can_switch') else actual
                indexes = [p for p,value in enumerate(phase_vector(1,actual_count,box.get('rotation'))) if value]
                room = max(0.0,min(phase_limits[p]-reserved[p] for p in indexes))/scales[cid]
                command_unrounded_cap = room
                actual_max = (box.get('max_amp_by_phase') or {}).get(actual) or 0.0
                command_amp = core._floor(min(actual_max, room,
                    max(minima[cid] if box.get('charging') else 0.0, (share or 0.0)/(230*actual))), steps[cid])
            result[cid] = dict(shares[cid], target_phases=target, cap_amp=amp,
                house_fuse_cap_amp=physical, reservation_scale=scales[cid],
                phase_grant=bool(amp >= minima[cid] and watts+1e-6 >= need),
                sentence='Gemeinsame Verteilung: %dp, Anteil %.0f W, Zuteilung %g A' % (target or 0, share or 0.0, amp),
                allocation=dict(slot, target_amp=command_amp, target_amp_per_phase=command_amp,
                    unrounded_electrical_cap_amp=command_unrounded_cap,
                    target_phases=command_phases, energy_projection_phases=command_phases,
                    target_power_w=command_amp*230*(command_phases or 0),
                    reserved_phase_vector_a=phase_vector(max(floors[cid], amp, command_amp)*scales[cid], counts[cid], box.get('rotation')),
                    electrical_cap_amp=physical))
        return lost, useful, result

    evaluated = [(combination, evaluate(combination)) for combination in combinations]
    best_combo, best = evaluated[0]
    tolerance = 230*min(core.number(box.get('step_amp')) or 1.0 for box in boxes)
    for combo, value in evaluated[1:]:
        if value[0] < best[0] or (value[0] == best[0] and (
                value[1] >= best[1]+tolerance or (abs(value[1]-best[1]) < tolerance
                and sum(p or 0 for p in combo.values()) > sum(p or 0 for p in best_combo.values())))):
            best_combo, best = combo, value
    # Dieselbe gemeinsame Zielbelegung für Fahrstrom und stehende Angebote.
    # Die leere Box erhält weiterhin 0 W Energieanteil. Bereits vergebene
    # Fahrstromziele und Angebote werden elektrisch genau einmal gebucht.
    occupied = [0.0, 0.0, 0.0]
    for box in boxes:
        if box.get('unplug_offer') or not fresh(box):
            continue
        row = best[2][box['id']]
        vector = (row.get('allocation') or {}).get('reserved_phase_vector_a')
        if not box.get('eligible') or core.energy_source(box) is None:
            scale = reservation_scale(box, power_factor_margin)
            vector = phase_vector(occupied_amp(box, scale)*scale,
                3 if box.get('autonomous_can_switch') else box.get('phases'), box.get('rotation'))
        occupied = [a+b for a,b in zip(occupied, vector or (0.0,0.0,0.0))]
    shares = best[2]
    for box in boxes:
        offer = box.get('unplug_offer')
        if not isinstance(offer, dict):
            continue
        cid = box['id']
        source = shares[cid].get('source')
        remaining = core.number(budget_w if source == 'battery' else budget_pv_w)
        if source == 'battery' and remaining is not None:
            measured = core.measured_grid_power([other for other in boxes
                if shares[other['id']].get('source') == 'grid'])
            pv = core.number(budget_pv_w)
            remaining = (max(0.0, remaining-measured) if measured is not None
                else min(remaining,pv) if pv is not None else None)
        if remaining is not None:
            remaining = max(0.0,remaining-sum(core.number(shares[other['id']].get('share_w')) or 0.0
                for other in boxes if other.get('eligible') and shares[other['id']].get('source') != 'grid'))
        phases = box.get('offer_phases')
        count = 3 if box.get('autonomous_can_switch') else phases
        scale = reservation_scale(dict(box,phases=phases),power_factor_margin)
        indexes = [p for p,value in enumerate(phase_vector(1.0,count,box.get('rotation'))) if value]
        cap = max(0.0,min(phase_limits[p]-occupied[p] for p in indexes))/scale
        minimum = box.get('offer_min',6.0)
        target = core.unplug_offer_target(box['core_state'],box['offer_now'],
            safe=bool(box.get('offer_safe') and box.get('offer_fast') and cap >= minimum),
            grid=source == 'grid',budget_w=remaining,phases=phases,minimum=minimum,
            budget_valid=box.get('offer_budget_valid') is True,
            previous_target=core.number(offer.get('target_amp')) or 0.0)
        shares[cid]['unplug_offer_target_amp'] = target
        vector = phase_vector(target*scale,count,box.get('rotation'))
        occupied = [a+b for a,b in zip(occupied,vector)]
    one_phase = [box for box in boxes if fresh(box) and box.get('charging')
                 and box.get('phases') == 1]
    warning = None
    if len(one_phase) > 1:
        rotations = [normalize_rotation(box.get('rotation')) for box in one_phase]
        if any(rotation is None for rotation in rotations) or len({r[0] for r in rotations if r is not None}) < len(rotations):
            warning = ('Netzphase der Ladepunkte im Config-Editor eintragen; die Leistung wird geteilt.'
                       if any(rotation is None for rotation in rotations) else
                       'WB1 und WB2 laden einphasig auf derselben Netzphase; die Leistung wird aufgeteilt.')
    for box in boxes:
        hint = warning
        if (fresh(box) and box.get('charging') and box.get('phases') in (1, 2)
                and normalize_rotation(box.get('rotation')) is None):
            hint = ('Netzphase von WB%d nicht eingetragen: E3DC-Control hält mehr Abstand zur Anschlussgrenze. '
                    'Netzphase im Config-Editor unter „WB%d lokale L1 liegt auf Netzphase“ eintragen, '
                    'um Sicherung und Hausanschluss voll zu nutzen.') % (box['id'], box['id'])
        best[2][box['id']]['phase_mapping_hint'] = hint
    return best[2]
