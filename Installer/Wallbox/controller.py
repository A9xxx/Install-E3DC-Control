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
from .modes import (
    MODE_BASE,
    MODE_OFF,
    normalize_wb_mode,
)

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
        if bool(status.get("charge_state", False) or status.get("charging", False)):
            return True
        if abs(float(status.get("power_w", status.get("power", 0)) or 0)) > 250.0:
            return True
        if abs(float(status.get("amp", status.get("offered_current", 0)) or 0)) >= 5.5:
            return True
    except Exception:
        return False
    return False


def allocate_power(available_watts, chargers_status, mode, max_amp,
                   wb_charge_mode, price_optimizing_active=False):
    """
    Verteilt 'available_watts' auf alle verbundenen Wallboxen.

    mode (Multi-WB Priorisierung aus wb_native_mode):
        0 = Ausgeglichen (Round-Robin, alle gleich)
        1 = WB1 hat Vorrang
        2 = WB2 hat Vorrang

    wb_charge_mode[id]: oeffentlicher WB-Modus 0/2/3/4/5/12 (Legacy-Werte werden normalisiert)
    price_optimizing_active: True oder Ladepunkt-IDs -> Sofortladen bevorzugt

    Gibt dict { wb_id: {'target_amp': int, 'state': int (1=Stop, 2=An)} } zurueck.
    """
    alloc = {}
    remaining_watts = available_watts
    active_chargers = []

    # --- Phase 1: Sofortladen / Ladekorridor Vorab-Zuteilung -----------------
    for c in chargers_status:
        c_id = c['id']
        if not (c['status'] and c['status'].get('car', 1) >= 2):
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
        pha       = c['status'].get('pha', 0)
        phases    = 3 if pha == 56 else (1 if pha in [8, 16, 32] else 3)
        w_per_amp = 230.0 * phases
        min_w     = 6 * w_per_amp
        max_w     = max_amp * w_per_amp

        # Zeitplan aktiv -> immer Sofortladen (ueberschreibt c_mode)
        if local_price_optimizing_active:
            behavior = 'instant'

        if behavior == 'instant':
            # Sofort mit Max-Strom laden (Netz-Bezug in Kauf nehmen)
            alloc[c_id] = {'target_amp': max_amp, 'state': 2}
            remaining_watts -= max_w
            logger.debug(f"WB{c_id} Sofortladen ({c_mode=}): {max_amp}A")

        elif behavior == 'corridor':
            # Mindest-Amp (6A) immer zuteilen, Rest aus Ueberschuss
            allocated_amp = 6
            remaining_watts -= min_w
            active_chargers.append({
                'id':            c_id,
                'charger':       c['charger'],
                'phases':        phases,
                'w_per_amp':     w_per_amp,
                'min_w':         min_w,
                'max_w':         max_w,
                'mode':          c_mode,
                'allocated_amp': allocated_amp,
                'state':         2,  # Immer AN (Mindest-Ladestrom sicherstellen)
                'running':       _status_running(c['status']),
            })
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
                'mode':          c_mode,
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

    if mode == 0:
        # Ausgeglichen: Erst alle auf Minimum, dann fair aufteilen
        for c in active_chargers:
            if c['allocated_amp'] < 6 and remaining_watts >= c['min_w']:
                remaining_watts -= c['min_w']
                c['allocated_amp'] = 6
                c['state'] = 2

        while remaining_watts > 0:
            allocated_any = False
            for c in active_chargers:
                if c['state'] == 2 and c['allocated_amp'] < max_amp and remaining_watts >= c['w_per_amp']:
                    c['allocated_amp'] += 1
                    remaining_watts    -= c['w_per_amp']
                    allocated_any = True
            if not allocated_any:
                break
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
                if c['state'] == 2 and c['allocated_amp'] < max_amp and remaining_watts >= c['w_per_amp']:
                    c['allocated_amp'] += 1
                    remaining_watts -= c['w_per_amp']
                    allocated_any = True
            if not allocated_any:
                break

    for c in active_chargers:
        alloc[c['id']] = {'target_amp': c['allocated_amp'], 'state': c['state']}

    return alloc


