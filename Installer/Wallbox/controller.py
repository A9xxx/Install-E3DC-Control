"""
E3DC-Control Wallbox Manager - Leistungsverteilung.
allocate_power(): Verteilt verfuegbare Watt fair auf alle aktiven Wallboxen
unter Beruecksichtigung von PV-Ueberschuss, Modus und Sperren.

wb_charge_mode (pro Wallbox, gespeichert als wb1_mode/wb2_mode):
  0 = Aus/autonom: Python sendet keine Ladebefehle
  2 = PV-Kurve ruhig: Ladekurve + Hysterese, kein geplanter Netzbezug
  3 = Grundladung stabil: 6A-Boden gegen Takten, solange wbminSoC erreichbar bleibt
  4 = PV + Akku bis Untergrenze: Hausakku-Untergrenze bleibt fix, kein Netz
  5 = Sofort bis Preislimit: wie PV + Akku bis Untergrenze, Netz nur bei freigegebenem Preisfenster
 12 = Akku bis Abfahrt: wie PV + Akku bis Untergrenze, aber zeitlich bis zur Abfahrt begrenzt
"""
import logging
import math
from .modes import (
    MODE_BASE,
    MODE_OFF,
    normalize_wb_mode,
)
from .decision import status_connected
from .phase_balancing import (
    normalize_rotation,
    _strict_numeric_vector,
)

logger = logging.getLogger("WallboxManager.Controller")


def _classify_mode(c_mode):
    """
    Vereinfacht den öffentlichen WB-Modus auf interne Controller-Klassen:
      'instant'   - autorisiertes Budget bevorzugt bis zum Stromdeckel zuteilen
      'corridor'  - Mindeststrom innerhalb des autorisierten Budgets priorisieren
      'pv'        - ausschließlich das autorisierte PV-/Speicherbudget verteilen

    Keine Klasse erzeugt selbst Leistung; ``available_watts`` bleibt bindend.
    """
    c_mode = normalize_wb_mode(c_mode)
    if c_mode == MODE_BASE:
        return 'corridor'
    return 'pv'


def _price_active_for(price_optimizing_active, c_id):
    if isinstance(price_optimizing_active, dict):
        return bool(price_optimizing_active.get(c_id, False))
    if isinstance(price_optimizing_active, (set, list, tuple)):
        return c_id in price_optimizing_active
    return bool(price_optimizing_active)


def _status_running(status):
    if not isinstance(status, dict):
        return False
    try:
        if (
            status.get("driver_status_valid") is False
            or status.get("driver_status_stale") is True
            or status.get("driver_status_glitch") is True
            or status.get("stale") is True
        ):
            return False
        contract = status.get("charge_contract")
        if isinstance(contract, dict):
            if (
                contract.get("counts_as_real_charge") is True
                or contract.get("is_charging") is True
                or contract.get("truth") in ("charging", "real_charge")
            ):
                return True
        if status.get("charge_counts_as_real") is True:
            return True
        if bool(status.get("charge_state", False) or status.get("charging", False)):
            return True
        power_w = max(
            abs(float(status.get("real_power_w", 0) or 0)),
            abs(float(status.get("phase_power_sum_w", 0) or 0)),
            abs(float(status.get("power_w", status.get("power", 0)) or 0)),
        )
        if power_w > 250.0:
            return True
    except Exception:
        return False
    return False


def allocate_power(available_watts, chargers_status, mode, max_amp,
                   wb_charge_mode, price_optimizing_active=False,
                   max_amp_by_id=None, fairness_weight_by_id=None,
                   phase_count_by_id=None, watts_per_amp_by_id=None,
                   grid_phase_limit_vector=None, phase_rotation_by_id=None,
                   unmanaged_phase_amps=None,
                   electrical_reservation_phase_count_by_id=None):
    """
    Verteilt 'available_watts' auf alle verbundenen Wallboxen.

    mode (Multi-WB Priorisierung aus wb_native_mode):
        0 = Ausgeglichen (Round-Robin, alle gleich)
        1 = WB1 hat Vorrang
        2 = WB2 hat Vorrang

    wb_charge_mode[id]: oeffentlicher WB-Modus 0/2/3/4/5/12 (Legacy-Werte werden normalisiert)
    price_optimizing_active: True oder Ladepunkt-IDs -> Sofortladen bevorzugt

    ``max_amp_by_id`` bindet EVSE-, Kabel- und Fahrzeuggrenzen je Ladepunkt.
    Im ausgeglichenen Modus wird nach Leistung statt nach einer physikalisch
    bedeutungslosen Summe aus ein- und dreiphasigen Amperewerten verteilt.

    Sobald ``phase_count_by_id`` oder ``watts_per_amp_by_id`` übergeben wird,
    gilt der Wattvertrag: Eine fehlende/ungültige Phasenzahl ist 0 A, und auch
    Preis- oder Grundlademodi dürfen zusammen nicht mehr Leistung zuteilen als
    ``available_watts``. Gemessene Watt-pro-Ampere-Werte dürfen den nominalen
    Leistungsansatz von 230 V je aktiver Phase nicht absenken: Ein Fahrzeug,
    das sein Stromangebot gerade nicht vollständig abnimmt, öffnet dadurch
    kein zusätzliches Gruppenbudget. Auch ohne diese Maps bleibt
    ``available_watts`` die einzige Leistungsautorität; Modus-, Preis- und
    Phaseninformationen dürfen kein eigenes Startbudget erzeugen.

    ``electrical_reservation_phase_count_by_id`` trennt die energetische
    Wattprojektion von der elektrischen Anschlussbelegung. Das ist für eine
    autonom umschaltende efy nötig: Ihr Start darf energetisch als 1p gelten,
    solange die Firmware aber auch 3p wählen kann, reserviert die Verteilung
    denselben Strom auf L1/L2/L3. Ist die explizite Reservierungsmap
    unvollständig oder ungültig, bleibt der betroffene Ladepunkt bei 0 A.

    ``grid_phase_limit_vector``: Optionaler Phasenvektor [L1, L2, L3] in Ampere
    (z. B. Hausabsicherung minus Reserve). Startzuteilung und Stromerhöhung
    werden gemeinsam gegen diesen Phasenvektor verteilt, ohne pauschale Halbierung.
    Autonome oder unmanaged Verbraucher werden konservativ gegen den verfügbaren
    Rest reserviert und erhalten keine Stromkommandos.

    Der Wattvertrag ergänzt je Ladepunkt ``target_power_w``,
    ``target_phases`` und ``target_amp_per_phase``; ``target_amp`` bleibt der
    rückwärtskompatible Alias für den Strom je aktiver Phase. Zusätzlich
    beschreiben ``energy_projection_phases``,
    ``electrical_reservation_phases`` und ``reserved_phase_vector_a`` den
    getrennten Kapazitätsvertrag.
    """
    alloc = {}
    strict_watt_contract = bool(
        isinstance(phase_count_by_id, dict)
        or isinstance(watts_per_amp_by_id, dict)
    )
    try:
        remaining_watts = max(0.0, float(available_watts or 0.0))
    except (TypeError, ValueError):
        remaining_watts = 0.0
    if strict_watt_contract and not math.isfinite(remaining_watts):
        remaining_watts = 0.0
    active_chargers = []
    per_charger_limits = max_amp_by_id if isinstance(max_amp_by_id, dict) else {}
    fairness_weights = fairness_weight_by_id if isinstance(fairness_weight_by_id, dict) else {}
    phase_counts = phase_count_by_id if isinstance(phase_count_by_id, dict) else None
    electrical_phase_counts = (
        electrical_reservation_phase_count_by_id
        if isinstance(electrical_reservation_phase_count_by_id, dict)
        else None
    )
    strict_electrical_reservation = electrical_phase_counts is not None
    rotations_by_id = phase_rotation_by_id if isinstance(phase_rotation_by_id, dict) else {}
    phase_limits = None
    phase_limits_valid = True
    if grid_phase_limit_vector is not None:
        raw_p_limits = _strict_numeric_vector(grid_phase_limit_vector)
        if raw_p_limits is None or any(v < 0.0 for v in raw_p_limits):
            phase_limits = [0.0, 0.0, 0.0]
            phase_limits_valid = False
        else:
            phase_limits = [float(v) for v in raw_p_limits]

    if phase_limits is not None and phase_limits_valid and unmanaged_phase_amps is not None:
        raw_unm = _strict_numeric_vector(unmanaged_phase_amps)
        if raw_unm is not None:
            for p in range(3):
                phase_limits[p] = max(0.0, phase_limits[p] - max(0.0, float(raw_unm[p])))

    def _mapped_value(values, c_id, default=None):
        if not isinstance(values, dict):
            return default
        if c_id in values:
            return values.get(c_id)
        return values.get(str(c_id), default)

    def _phase_count(status, c_id):
        if phase_counts is not None:
            try:
                mapped = int(float(_mapped_value(phase_counts, c_id, 0) or 0))
            except (TypeError, ValueError):
                mapped = 0
            return mapped if mapped in (1, 2, 3) else 0
        pha = (status or {}).get('pha', 0)
        if pha == 56:
            return 3
        if pha in [8, 16, 32]:
            return 1
        return 0 if strict_watt_contract else 3

    def _electrical_phase_count(status, c_id, energy_phases):
        if electrical_phase_counts is not None:
            try:
                mapped = int(float(
                    _mapped_value(electrical_phase_counts, c_id, 0) or 0
                ))
            except (TypeError, ValueError):
                mapped = 0
            return mapped if mapped in (1, 2, 3) else 0
        return int(energy_phases or _phase_count(status, c_id) or 0)

    def _watts_per_amp(c_id, phases):
        nominal = 230.0 * float(phases)
        return nominal

    def _reserved_phase_vector(target_amp, reservation_phases, rotation):
        target = max(0.0, float(target_amp or 0.0))
        phases = int(reservation_phases or 0)
        if target <= 0.0 or phases <= 0:
            return (0.0, 0.0, 0.0)
        if phases >= 3:
            return (target, target, target)
        rot = normalize_rotation(rotation)
        if rot is None:
            # Ohne bestätigte PCC-Zuordnung darf keine Phase freigegeben werden.
            return (target, target, target)
        vector = [0.0, 0.0, 0.0]
        for local_index in range(min(phases, 3)):
            vector[rot[local_index]] = target
        return tuple(vector)

    def _allocation_result(
        target_amp,
        state,
        phases,
        w_per_amp,
        *,
        electrical_phases=None,
        rotation=None,
    ):
        target = max(0, int(target_amp or 0))
        reservation_phases = int(
            electrical_phases if electrical_phases is not None else phases or 0
        )
        result = {
            'target_amp': target,
            'state': int(state),
        }
        if strict_watt_contract:
            result.update({
                'target_power_w': float(target) * max(0.0, float(w_per_amp or 0.0)),
                'target_phases': int(phases or 0),
                'target_amp_per_phase': target,
                'energy_projection_phases': int(phases or 0),
                'electrical_reservation_phases': reservation_phases,
                'reserved_phase_vector_a': _reserved_phase_vector(
                    target,
                    reservation_phases,
                    rotation,
                ),
            })
        return result

    # --- Phase 1: Vorab-Prüfung und Autonome Verbraucher ---------------------
    for c in chargers_status:
        c_id = c['id']
        status = c.get('status') if isinstance(c.get('status'), dict) else {}
        c_mode = normalize_wb_mode(wb_charge_mode.get(c_id, MODE_OFF))

        # E3DC Autonom / MODE_OFF: keine Steuerbefehle, aber konservative
        # Reservierung gegen das verbleibende Phasenbudget.
        if c_mode == 0:
            if phase_limits is not None and phase_limits_valid:
                c_energy_phases = _phase_count(status, c_id)
                c_phases = _electrical_phase_count(
                    status,
                    c_id,
                    c_energy_phases,
                )
                c_rot = normalize_rotation(_mapped_value(rotations_by_id, c_id))

                # Evidenzreihenfolge:
                # 1. Frische gemessene Phasenströme
                # 2. Frischer bestätigter tatsächlicher Ladestrom
                # 3. Bei laufender Ladung ohne gültigen Iststrom sowie bei
                #    stale/unklarem Status: konfiguriertes Hardwaremaximum
                # 4. Frisch und widerspruchsfrei nicht ladend: 0 A
                c_accepted = 0.0
                phase_amps = None

                try:
                    fallback_max = float(max_amp)
                except (TypeError, ValueError):
                    fallback_max = 32.0
                if not math.isfinite(fallback_max) or fallback_max <= 0.0:
                    fallback_max = 32.0
                try:
                    hw_max = float(
                        per_charger_limits.get(c_id, fallback_max)
                        or fallback_max
                    )
                except (TypeError, ValueError):
                    hw_max = fallback_max
                if not math.isfinite(hw_max) or hw_max <= 0.0:
                    hw_max = fallback_max
                hw_max = max(0.0, hw_max)

                status_fresh = bool(
                    status.get('driver_status_valid') is True
                    and status.get('driver_status_stale') is not True
                    and status.get('driver_status_glitch') is not True
                    and status.get('stale') is not True
                )
                if not status_fresh:
                    # Ein autonomer Ladepunkt kann bei Kommunikationsverlust
                    # physisch weiterladen. Da wir ihn in MODE_OFF nicht
                    # kommandieren dürfen, muss sein Hardwaremaximum den
                    # verbleibenden Phasenraum konservativ belegen.
                    c_accepted = hw_max
                else:
                    raw_phase_amps = []
                    complete_phase_vector = True
                    for p in (1, 2, 3):
                        p_val = status.get(f"phase_current_l{p}_a")
                        try:
                            p_amp = float(p_val)
                        except (TypeError, ValueError):
                            complete_phase_vector = False
                            break
                        if not math.isfinite(p_amp) or p_amp < 0.0:
                            complete_phase_vector = False
                            break
                        raw_phase_amps.append(p_amp)

                    if (
                        complete_phase_vector
                        and len(raw_phase_amps) == 3
                        and any(value > 0.5 for value in raw_phase_amps)
                    ):
                        if c_rot is not None:
                            phase_amps = [0.0, 0.0, 0.0]
                            for local_index, pcc_index in enumerate(c_rot):
                                phase_amps[pcc_index] = raw_phase_amps[local_index]
                        else:
                            # Ohne bestätigte Zuordnung darf eine lokale
                            # Phasenmessung keine PCC-Phase freigeben.
                            c_accepted = max(raw_phase_amps)
                        if phase_amps is not None:
                            c_accepted = max(phase_amps)

                    if c_accepted <= 0.0:
                        actual_amp = (
                            status.get("charging_current")
                            or status.get("actual_charging_current")
                            or status.get("real_current_a")
                            or status.get("current_a")
                        )
                        try:
                            actual_amp = float(actual_amp or 0.0)
                        except (TypeError, ValueError):
                            actual_amp = 0.0
                        if math.isfinite(actual_amp) and actual_amp > 0.0:
                            c_accepted = actual_amp

                    if c_accepted <= 0.0 and _status_running(status):
                        # Wirkleistung ist wegen Spannung und Leistungsfaktor
                        # keine RMS-Stromautorität. Läuft die autonome Box ohne
                        # gültigen Stromwert, bleibt nur das Hardwaremaximum.
                        c_accepted = hw_max

                if c_accepted > 0.0:
                    if phase_amps is not None:
                        for p in range(3):
                            phase_limits[p] = max(0.0, phase_limits[p] - phase_amps[p])
                    elif c_phases >= 3:
                        for p in range(3):
                            phase_limits[p] = max(0.0, phase_limits[p] - c_accepted)
                    elif c_phases == 1 and c_rot is not None:
                        idx = c_rot[0]
                        phase_limits[idx] = max(0.0, phase_limits[idx] - c_accepted)
                    else:
                        for p in range(3):
                            phase_limits[p] = max(0.0, phase_limits[p] - c_accepted)
            continue

        if not (status and status_connected(status)):
            continue
        if status.get('locked', False):
            continue
        if (
            status.get('driver_status_valid') is False
            or status.get('driver_status_stale') is True
            or status.get('stale') is True
        ):
            continue

        local_price_optimizing_active = (
            _price_active_for(price_optimizing_active, c_id)
            and c_mode == 5
        )

        if local_price_optimizing_active:
            behavior = 'instant'
        else:
            behavior = _classify_mode(c_mode)
        phases = _phase_count(c['status'], c_id)
        electrical_phases = _electrical_phase_count(
            c['status'],
            c_id,
            phases,
        )
        if strict_watt_contract and phases == 0:
            alloc[c_id] = _allocation_result(0, 1, 0, 0.0)
            logger.debug(f"WB{c_id} keine frische/plausible Phasenzahl: Zuteilung bleibt 0A")
            continue
        if strict_electrical_reservation and electrical_phases == 0:
            alloc[c_id] = _allocation_result(
                0,
                1,
                phases,
                _watts_per_amp(c_id, phases),
                electrical_phases=0,
                rotation=_mapped_value(rotations_by_id, c_id),
            )
            logger.debug(
                f"WB{c_id} ohne gültigen elektrischen Phasenvertrag: "
                "Zuteilung bleibt 0A"
            )
            continue
        w_per_amp = _watts_per_amp(c_id, phases)
        min_w = 6 * w_per_amp
        try:
            charger_max_amp = int(per_charger_limits.get(c_id, max_amp) or max_amp)
        except (TypeError, ValueError):
            charger_max_amp = int(max_amp)
        charger_max_amp = max(0, min(int(max_amp), charger_max_amp))
        if strict_watt_contract and charger_max_amp < 6:
            alloc[c_id] = _allocation_result(0, 1, phases, w_per_amp)
            logger.debug(f"WB{c_id} Stromlimit unter 6A: Zuteilung bleibt 0A")
            continue
        max_w = charger_max_amp * w_per_amp
        try:
            fairness_weight = max(0.01, float(fairness_weights.get(c_id, 1.0) or 1.0))
        except (TypeError, ValueError):
            fairness_weight = 1.0

        rotation = normalize_rotation(_mapped_value(rotations_by_id, c_id))

        if behavior == 'instant':
            active_chargers.append({
                'id': c_id,
                'charger': c.get('charger'),
                'phases': phases,
                'electrical_phases': electrical_phases,
                'rotation': rotation,
                'w_per_amp': w_per_amp,
                'min_w': min_w,
                'max_w': max_w,
                'max_amp': charger_max_amp,
                'fairness_weight': fairness_weight,
                'mode': c_mode,
                'behavior': behavior,
                'allocated_amp': 0,
                'state': 1,
                'running': _status_running(c['status']),
            })

        elif behavior == 'corridor':
            active_chargers.append({
                'id':            c_id,
                'charger':       c.get('charger'),
                'phases':        phases,
                'electrical_phases': electrical_phases,
                'rotation':      rotation,
                'w_per_amp':     w_per_amp,
                'min_w':         min_w,
                'max_w':         max_w,
                'max_amp':       charger_max_amp,
                'fairness_weight': fairness_weight,
                'mode':          c_mode,
                'behavior':      behavior,
                'allocated_amp': 0,
                'state':         1,
                'running':       _status_running(c['status']),
            })

        else:
            active_chargers.append({
                'id':            c_id,
                'charger':       c.get('charger'),
                'phases':        phases,
                'electrical_phases': electrical_phases,
                'rotation':      rotation,
                'w_per_amp':     w_per_amp,
                'min_w':         min_w,
                'max_w':         max_w,
                'max_amp':       charger_max_amp,
                'fairness_weight': fairness_weight,
                'mode':          c_mode,
                'behavior':      behavior,
                'allocated_amp': 0,
                'state':         1,
                'running':       _status_running(c['status']),
            })

    if not active_chargers:
        return alloc

    # --- Phase 2: Zuteilung gegen Wattbudget und Phasenvektor ----------------
    allocated_pcc_phase_a = [0.0, 0.0, 0.0]

    def _can_allocate_delta(c, delta_a):
        if c['allocated_amp'] + delta_a > c['max_amp']:
            return False
        # Das Gruppen-Wattbudget bleibt in jedem Aufruf bindend. Der optionale
        # Phasenvektor ist nur eine zusätzliche Hausanschlussgrenze und darf
        # weder einen 6-A-Start noch eine Erhöhung aus 0 W erzeugen.
        if math.isfinite(remaining_watts):
            if remaining_watts + 1e-9 < delta_a * c['w_per_amp']:
                return False
        if phase_limits is not None:
            if not phase_limits_valid:
                return False
            rot = c.get('rotation')
            if c['electrical_phases'] >= 3:
                if any(allocated_pcc_phase_a[p] + delta_a > phase_limits[p] + 1e-9 for p in range(3)):
                    return False
            elif c['electrical_phases'] in (1, 2) and rot is not None:
                phase_indexes = rot[:c['electrical_phases']]
                if any(
                    allocated_pcc_phase_a[idx] + delta_a
                    > phase_limits[idx] + 1e-9
                    for idx in phase_indexes
                ):
                    return False
            else:
                if any(allocated_pcc_phase_a[p] + delta_a > phase_limits[p] + 1e-9 for p in range(3)):
                    return False
        return True

    def _apply_delta(c, delta_a):
        nonlocal remaining_watts
        c['allocated_amp'] += delta_a
        if math.isfinite(remaining_watts):
            remaining_watts -= delta_a * c['w_per_amp']
        if phase_limits is not None:
            rot = c.get('rotation')
            if c['electrical_phases'] >= 3:
                for p in range(3):
                    allocated_pcc_phase_a[p] += delta_a
            elif c['electrical_phases'] in (1, 2) and rot is not None:
                for idx in rot[:c['electrical_phases']]:
                    allocated_pcc_phase_a[idx] += delta_a
            else:
                for p in range(3):
                    allocated_pcc_phase_a[p] += delta_a

    def _grant_strict_minimum(c):
        if c['allocated_amp'] < 6 and _can_allocate_delta(c, 6):
            _apply_delta(c, 6)
            c['state'] = 2
            return True
        return False

    def _grant_minimum(c):
        return _grant_strict_minimum(c)

    priority_id = int(mode) if int(mode) in (1, 2) else 0

    if strict_watt_contract or phase_limits is not None:
        running_order = sorted(
            (c for c in active_chargers if c.get('running')),
            key=lambda c: (
                0 if int(c.get('id', 0)) == priority_id else 1,
                0 if c.get('behavior') == 'instant' else 1,
                int(c.get('id', 0)),
            ),
        )
        for c in running_order:
            _grant_strict_minimum(c)

        idle_order = sorted(
            (c for c in active_chargers if not c.get('running')),
            key=lambda c: (
                0 if int(c.get('id', 0)) == priority_id else 1,
                0 if c.get('behavior') == 'instant' else 1,
                int(c.get('id', 0)),
            ),
        )
        for c in idle_order:
            if c['state'] != 2:
                _grant_strict_minimum(c)

        if priority_id == 0:
            while remaining_watts > 0:
                candidates = [
                    c for c in active_chargers
                    if c['state'] == 2
                    and _can_allocate_delta(c, 1)
                ]
                if not candidates:
                    break
                instant_candidates = [
                    c for c in candidates if c.get('behavior') == 'instant'
                ]
                pool = instant_candidates or candidates
                chosen = min(
                    pool,
                    key=lambda c: (
                        (c['allocated_amp'] * c['w_per_amp']) / c['fairness_weight'],
                        c['id'],
                    ),
                )
                _apply_delta(chosen, 1)
        else:
            extra_order = sorted(
                active_chargers,
                key=lambda c: (
                    0 if int(c.get('id', 0)) == priority_id else 1,
                    0 if c.get('behavior') == 'instant' else 1,
                    int(c.get('id', 0)),
                ),
            )
            while remaining_watts > 0:
                allocated_any = False
                for c in extra_order:
                    if c['state'] == 2 and _can_allocate_delta(c, 1):
                        _apply_delta(c, 1)
                        allocated_any = True
                if not allocated_any:
                    break

    elif mode == 0:
        minimum_order = sorted(
            active_chargers,
            key=lambda c: (0 if c.get('running') else 1, int(c.get('id', 0))),
        )
        for c in minimum_order:
            _grant_minimum(c)

        while remaining_watts > 0:
            candidates = [
                c for c in active_chargers
                if c['state'] == 2
                and _can_allocate_delta(c, 1)
            ]
            if not candidates:
                break
            chosen = min(
                candidates,
                key=lambda c: (
                    (c['allocated_amp'] * c['w_per_amp']) / c['fairness_weight'],
                    c['id'],
                ),
            )
            _apply_delta(chosen, 1)
    else:
        if priority_id:
            for c in active_chargers:
                if int(c.get('id', 0)) == priority_id:
                    _grant_minimum(c)
                    break

        for c in active_chargers:
            if int(c.get('id', 0)) != priority_id and c.get('running'):
                _grant_minimum(c)

        for c in active_chargers:
            if c['state'] != 2:
                _grant_minimum(c)

        while remaining_watts > 0:
            allocated_any = False
            for c in active_chargers:
                if c['state'] == 2 and _can_allocate_delta(c, 1):
                    _apply_delta(c, 1)
                    allocated_any = True
            if not allocated_any:
                break

    for c in active_chargers:
        alloc[c['id']] = _allocation_result(
            c['allocated_amp'],
            c['state'],
            c['phases'],
            c['w_per_amp'],
            electrical_phases=c['electrical_phases'],
            rotation=c.get('rotation'),
        )

    return alloc
