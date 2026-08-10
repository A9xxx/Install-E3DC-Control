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

logger = logging.getLogger("WallboxManager.Controller")


def _classify_mode(c_mode):
    """
    Vereinfacht den oeffentlichen WB-Modus auf drei interne Controller-Klassen:
      'instant'   - Sofortladen mit max Strom (Netz erlaubt, durch Config/Hauslimit gedeckelt)
      'corridor'  - Ladekorridor (Mindest-Amp = 6A immer gesichert, dann Rest aufteilen)
      'pv'        - PV-Ueberschuss (nur was uebrig ist nach Bat-Prio)
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
                   phase_count_by_id=None, watts_per_amp_by_id=None):
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
    kein zusätzliches Gruppenbudget. Ohne diese neuen Maps bleibt der
    historische Rückgabevertrag für bestehende Aufrufer unverändert.

    Der Wattvertrag ergänzt je Ladepunkt ``target_power_w``,
    ``target_phases`` und ``target_amp_per_phase``; ``target_amp`` bleibt der
    rückwärtskompatible Alias für den Strom je aktiver Phase.
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

    def _watts_per_amp(c_id, phases):
        nominal = 230.0 * float(phases)
        # ``watts_per_amp_by_id`` bleibt als Eingangs-/Diagnosevertrag
        # kompatibel, ist aber niemals Autorität für die harte Zuteilung.
        # Maßgeblich ist die Leistung, die das angebotene Stromlimit bei
        # nominaler Netzspannung ermöglichen würde.
        return nominal

    def _allocation_result(target_amp, state, phases, w_per_amp):
        target = max(0, int(target_amp or 0))
        result = {
            'target_amp': target,
            'state': int(state),
        }
        if strict_watt_contract:
            result.update({
                'target_power_w': float(target) * max(0.0, float(w_per_amp or 0.0)),
                'target_phases': int(phases or 0),
                'target_amp_per_phase': target,
            })
        return result

    # --- Phase 1: Sofortladen / Ladekorridor Vorab-Zuteilung -----------------
    for c in chargers_status:
        c_id = c['id']
        # Alle Treiber werden bereits auf ein echtes Steckersignal
        # normalisiert. openWB Pro liefert dabei nicht zwingend das alte
        # numerische ``car``-Feld; eine Prüfung ausschließlich darauf ließ
        # einen nachweislich verbundenen Ladepunkt trotz positivem Wattbudget
        # aus der Verteilung fallen.
        if not (c['status'] and status_connected(c['status'])):
            continue
        if c['status'].get('locked', False):
            continue

        c_mode    = normalize_wb_mode(wb_charge_mode.get(c_id, MODE_OFF))
        
        # E3DC Autonom/Python aus: immer aus der Python-Verteilung ignorieren.
        if c_mode == 0:
            continue

        # Zeitplan aktiv -> Sofortladen, aber nur fuer aktiv von Python
        # verwaltete Ladepunkte. Mode 0 bleibt auch bei Preisfenstern stumm.
        local_price_optimizing_active = _price_active_for(price_optimizing_active, c_id)

        if local_price_optimizing_active:
            behavior = 'instant'
        else:
            behavior = _classify_mode(c_mode)
        phases    = _phase_count(c['status'], c_id)
        if strict_watt_contract and phases == 0:
            alloc[c_id] = _allocation_result(0, 1, 0, 0.0)
            logger.debug(f"WB{c_id} keine frische/plausible Phasenzahl: Zuteilung bleibt 0A")
            continue
        w_per_amp = _watts_per_amp(c_id, phases)
        min_w     = 6 * w_per_amp
        try:
            charger_max_amp = int(per_charger_limits.get(c_id, max_amp) or max_amp)
        except (TypeError, ValueError):
            charger_max_amp = int(max_amp)
        charger_max_amp = max(0, min(int(max_amp), charger_max_amp))
        if strict_watt_contract and charger_max_amp < 6:
            alloc[c_id] = _allocation_result(0, 1, phases, w_per_amp)
            logger.debug(f"WB{c_id} Stromlimit unter 6A: Zuteilung bleibt 0A")
            continue
        max_w     = charger_max_amp * w_per_amp
        try:
            fairness_weight = max(0.01, float(fairness_weights.get(c_id, 1.0) or 1.0))
        except (TypeError, ValueError):
            fairness_weight = 1.0

        # Zeitplan aktiv -> immer Sofortladen (ueberschreibt c_mode)
        if local_price_optimizing_active:
            behavior = 'instant'

        if behavior == 'instant':
            if strict_watt_contract:
                active_chargers.append({
                    'id': c_id,
                    'charger': c['charger'],
                    'phases': phases,
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
                logger.debug(
                    f"WB{c_id} Sofortladen im Wattvertrag ({c_mode=}): "
                    f"Zuteilung nach gruppenweitem Lauf-/Prioritätsvertrag bei {phases}p"
                )
            else:
                # Historischer Vertrag: Die explizite Netzfreigabe fordert Maximalstrom an.
                alloc[c_id] = {
                    'target_amp': charger_max_amp,
                    'state': 2,
                }
                remaining_watts -= max_w
                logger.debug(f"WB{c_id} Sofortladen ({c_mode=}): {charger_max_amp}A")

        elif behavior == 'corridor':
            # Im Wattvertrag muss auch der 6-A-Boden durch das gemeinsame
            # Leistungsbudget gedeckt sein. Der Legacy-Aufruf behält seine
            # bisherige Grundladefreigabe.
            allocated_amp = 0 if strict_watt_contract else 6
            if not strict_watt_contract:
                remaining_watts -= min_w
            active_chargers.append({
                'id':            c_id,
                'charger':       c['charger'],
                'phases':        phases,
                'w_per_amp':     w_per_amp,
                'min_w':         min_w,
                'max_w':         max_w,
                'max_amp':       charger_max_amp,
                'fairness_weight': fairness_weight,
                'mode':          c_mode,
                'behavior':      behavior,
                'allocated_amp': allocated_amp,
                'state':         1 if strict_watt_contract else 2,
                'running':       _status_running(c['status']),
            })
            if strict_watt_contract:
                logger.debug(
                    f"WB{c_id} Ladekorridor im Wattvertrag ({c_mode=}): "
                    "Mindeststrom nur bei gedecktem Leistungsbudget"
                )
            else:
                logger.debug(f"WB{c_id} Ladekorridor ({c_mode=}): 6A garantiert + Ueberschuss")

        else:
            # PV-Ueberschuss: Nur laden wenn genug da ist (kommt aus Phase 2)
            active_chargers.append({
                'id':            c_id,
                'charger':       c['charger'],
                'phases':        phases,
                'w_per_amp':     w_per_amp,
                'min_w':         min_w,
                'max_w':         max_w,
                'max_amp':       charger_max_amp,
                'fairness_weight': fairness_weight,
                'mode':          c_mode,
                'behavior':      behavior,
                'allocated_amp': 0,
                'state':         1,  # Default: Stop, wird in Phase 2 gesetzt
                'running':       _status_running(c['status']),
            })

    if not active_chargers:
        return alloc

    # --- Phase 2: Ueberschuss auf verbleibende Boxen verteilen ---------------
    # Sortierung nach Prioritaet (wb_native_mode)
    if mode == 1:
        active_chargers.sort(key=lambda x: 0 if x['id'] == 1 else 1)
    elif mode == 2:
        active_chargers.sort(key=lambda x: 0 if x['id'] == 2 else 1)

    if strict_watt_contract:
        priority_id = int(mode) if int(mode) in (1, 2) else 0

        def _grant_strict_minimum(c):
            nonlocal remaining_watts
            if c['allocated_amp'] < 6 and remaining_watts >= c['min_w']:
                remaining_watts -= c['min_w']
                c['allocated_amp'] = 6
                c['state'] = 2
                return True
            return False

        # Elektromechanisch zuerst die bereits physisch laufenden Ladungen
        # halten. Erst danach dürfen Preis-/Prioritätswünsche eine nur
        # verbundene Wallbox neu starten; die Eingangsreihenfolge ist bedeutungslos.
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

        priority_started = any(
            int(c.get('id', 0)) == priority_id and c.get('state') == 2
            for c in active_chargers
        )
        priority_present = any(
            int(c.get('id', 0)) == priority_id for c in active_chargers
        )
        if priority_id and priority_present and not priority_started:
            for c in active_chargers:
                if int(c.get('id', 0)) == priority_id:
                    priority_started = _grant_strict_minimum(c)
                    break

        idle_order = sorted(
            (c for c in active_chargers if not c.get('running')),
            key=lambda c: (
                0 if c.get('behavior') == 'instant' else 1,
                int(c.get('id', 0)),
            ),
        )
        if priority_id == 0:
            for c in idle_order:
                _grant_strict_minimum(c)
        elif not priority_present or not priority_started:
            for c in idle_order:
                if _grant_strict_minimum(c):
                    break

        if priority_id == 0:
            while remaining_watts > 0:
                candidates = [
                    c for c in active_chargers
                    if c['state'] == 2
                    and c['allocated_amp'] < c['max_amp']
                    and remaining_watts >= c['w_per_amp']
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
                chosen['allocated_amp'] += 1
                remaining_watts -= chosen['w_per_amp']
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
                    if (
                        c['state'] == 2
                        and c['allocated_amp'] < c['max_amp']
                        and remaining_watts >= c['w_per_amp']
                    ):
                        c['allocated_amp'] += 1
                        remaining_watts -= c['w_per_amp']
                        allocated_any = True
                if not allocated_any:
                    break

    elif mode == 0:
        # Ausgeglichen: Eine bereits physisch laufende Wallbox darf nicht nur
        # wegen der Eingangsreihenfolge ihr Mindestbudget an eine stehende
        # Wallbox verlieren. Innerhalb beider Gruppen bleibt die ID-Reihenfolge
        # stabil; angebotene Ampere gelten ausdrücklich nicht als Ladebeleg.
        minimum_order = sorted(
            active_chargers,
            key=lambda c: (0 if c.get('running') else 1, int(c.get('id', 0))),
        )
        for c in minimum_order:
            if c['allocated_amp'] < 6 and remaining_watts >= c['min_w']:
                remaining_watts -= c['min_w']
                c['allocated_amp'] = 6
                c['state'] = 2

        # Progressive Füllung nach zugeteilter Wirkleistung. Damit sind z. B.
        # 16 A einphasig und 6 A dreiphasig annähernd energiefair, während
        # 16 A + 16 A dies nicht wären. Ein optionales Gewicht bildet später
        # Deadline-/Energieschuld ab, ohne die Safety-Caps zu verändern.
        while remaining_watts > 0:
            candidates = [
                c for c in active_chargers
                if c['state'] == 2
                and c['allocated_amp'] < c['max_amp']
                and remaining_watts >= c['w_per_amp']
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
            chosen['allocated_amp'] += 1
            remaining_watts -= chosen['w_per_amp']
    else:
        # Prioritaet: die gewaehlte Box bekommt zuerst Mindeststrom und den
        # ersten Extra-Ampere je Runde. Sobald das Budget fuer weitere
        # Mindeststroeme reicht, werden diese aber gehalten oder gestartet.
        # So wird Prioritaet nicht zur Monopol-Freigabe.
        priority_id = int(mode) if int(mode) in (1, 2) else 0
        priority_present = any(int(c.get('id', 0)) == priority_id for c in active_chargers)

        def _grant_minimum(c):
            nonlocal remaining_watts
            if c['allocated_amp'] < 6 and remaining_watts >= c['min_w']:
                remaining_watts -= c['min_w']
                c['allocated_amp'] = 6
                c['state'] = 2
                return True
            return False

        priority_started = False
        if priority_present:
            for c in active_chargers:
                if int(c.get('id', 0)) == priority_id and _grant_minimum(c):
                    priority_started = True
                    break

            # Laufende Neben-WB halten, aber keine schlafende Neben-WB neu
            # starten, solange die priorisierte WB nutzbares Budget bekommt.
            for c in active_chargers:
                if int(c.get('id', 0)) == priority_id:
                    continue
                if c.get('running'):
                    _grant_minimum(c)

        # Falls die priorisierte Box wegen Phasen/Mindestleistung nicht starten
        # kann oder kein expliziter Prioritaetstreffer existiert, darf nutzbares
        # Budget trotzdem eine andere verbundene Box starten.
        if not priority_present or not priority_started:
            for c in active_chargers:
                _grant_minimum(c)

        # Extra-Budget wird rundlaufend verteilt. Die sortierte Reihenfolge
        # sorgt dafuer, dass die priorisierte WB je Runde den ersten Ampere
        # bekommt, ohne eine zweite sinnvolle Ladung zu verhungern.
        while remaining_watts > 0:
            allocated_any = False
            for c in active_chargers:
                if c['state'] == 2 and c['allocated_amp'] < c['max_amp'] and remaining_watts >= c['w_per_amp']:
                    c['allocated_amp'] += 1
                    remaining_watts -= c['w_per_amp']
                    allocated_any = True
            if not allocated_any:
                break

    for c in active_chargers:
        alloc[c['id']] = _allocation_result(
            c['allocated_amp'],
            c['state'],
            c['phases'],
            c['w_per_amp'],
        )

    return alloc


