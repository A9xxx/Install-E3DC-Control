"""Grundadapter G2: dieselben Quellenleser, keine zweite Datei- oder Preiswahl."""
from math import floor, isfinite
from dataclasses import replace
import json
from . import backup_hold
try:
    from Installer.Storage import kern as k
    from Installer.Storage.kern import WALLBOX_QUELLENPRAEFIX
    from Installer.data_models import LiveDataSnapshot, control_home_power_w, PLAN_STALE_GRACE_MS
    from Installer.reserve import live_ep_reserve_details, effective_capacity_kwh, effective_ep_reserve_pct
except ModuleNotFoundError:
    from Storage import kern as k
    from Storage.kern import WALLBOX_QUELLENPRAEFIX
    from data_models import LiveDataSnapshot, control_home_power_w, PLAN_STALE_GRACE_MS
    from reserve import live_ep_reserve_details, effective_capacity_kwh, effective_ep_reserve_pct
from .kern_uebergabe import auftauen
try:
    from Installer.Heat.tariff_shift import tariff_window, grid_boost_active
    from Installer.consumer_priority import CONSUMER_MIN_W
except ModuleNotFoundError:
    from Heat.tariff_shift import tariff_window, grid_boost_active
    from consumer_priority import CONSUMER_MIN_W
from .vorab_leser import (cfg_bool, predump_time_window, predump_plan_is_hard,
    adaptive_gate, predump_trajectory_state, predump_wallbox_block_window)
from .quellenrahmen import schatten_quellen
from .leistungsleser import effective_power_limit_cfg, safe_float, safe_int
from .handauftrag_leser import (manual_override_deadline, manual_override_max_age_s,
                               manual_override_expired, storage_emergency_status)
from .kurvenleser import adaptive_curve_context
from .ladepunkt_leser import (storage_inputs_without_failed_wallboxes,
    wallbox_data_outage_context, wallbox_actual_power_snapshot, _wallbox_slot_types,
    wallbox_type_is_e3dc, runtime_input_age_s, AUSGESCHALTETE_BOX_TYPEN)


def _zahl(v):
    return type(v) in (int, float) and isfinite(v)


def _messwert(v, quelle, zeit, gueltig=True):
    ok = _zahl(v) and gueltig is True
    return k.Messwert(v if ok else None, ok, quelle, zeit if _zahl(zeit) else None,
                      "belegt" if ok else "fehlend oder ungültig")


def _konfigzahl(v):
    return safe_float(v, float("nan")) if type(v) in (str, int, float) else float("nan")


def _sekunden(v):
    return v / 1000.0 if _zahl(v) and v > 10000000000 else v if _zahl(v) else None


def _leistungsgrenze(cfg, live, richtung, jetzt):
    d = cfg["_power_limit_diagnostics"]
    device = "device_max_" + richtung + "_power_w"
    valid = d.get(device + "_valid") is True
    source = d.get(device + "_source", "unbekannt")
    zeit = (live.get(device + "_observed_at_s") if source != "rscp_get_power_settings_limits_off"
            else live.get("_ts"))
    # Eine bewusste Konfigurationskappe ist belegt; ein Legacy-Default nicht.
    key = "maximumladeleistung" if richtung == "charge" else "maximaleentladeleistung"
    configured = _konfigzahl(cfg.get("_power_limit_user_cfg", {}).get(key))
    if not valid and _zahl(configured) and configured >= 300:
        return _messwert(cfg[key], "config", jetzt)
    return _messwert(d.get("power_limit_" + richtung + "_effective_w"), source, zeit, valid)


def _kurve(plan, cfg, live, jetzt):
    raster = floor(jetzt / k.OPTIMIERUNGSTAKT_S) * k.OPTIMIERUNGSTAKT_S
    # A0: Raster ersetzt Interpolation je kurzem Dienstzyklus.
    context = adaptive_curve_context(plan, 0.0, raster,
        max(0.25, safe_float(cfg.get("tl_lookahead_h"), 1.0)),
        safe_float(live.get("PV_Power"), 0.0) > 250.0)
    boden, deckel = context["floor_soc"], context["ceiling_soc"]
    ziel, ende = context["target_soc"], context["target_ts"]
    kapazitaet, quelle = effective_capacity_kwh(cfg, live)
    if not all(_zahl(v) for v in (boden, ziel, ende)) or kapazitaet <= 0:
        return None
    if deckel is None:
        # Bestandskurve hat ein symmetrisches Band; keine zusätzliche Servo-Policy.
        toleranz = cfg.get("storage_parallel_curve_tolerance_pct", 0.5)
        toleranz = toleranz if _zahl(toleranz) and toleranz >= 0 else 0.5
        boden, deckel = max(0.0, boden - toleranz), min(100.0, boden + toleranz)
    gueltig = _sekunden(plan.get("valid_until_ts_ms"))
    if gueltig is None:
        gueltig = ende
    return k.Kurve(boden, deckel, ziel, ende, gueltig, kapazitaet * 1000.0,
                   "plan, Raster; Kapazität " + quelle)


def _bestand_auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig, plan_revision=None,
                     markt=None):
    if manual and not manual_override_expired(manual, jetzt, manual_override_max_age_s(cfg)):
        modus = str(manual.get("mode") or "").lower()      # wie im Produkt: Großschreibung ist kein anderer Modus
        ende = manual_override_deadline(manual, jetzt, manual_override_max_age_s(cfg))
        beginn = safe_float(manual.get("ts"), safe_float(manual.get("created_ts"), float("nan")))
        ziel = _konfigzahl(manual.get("target_soc"))
        if modus == "hold":
            return k.Auftrag("LADESP", beginn, ende, "manual"), None
        if modus == "charge":
            # Nutzervertrag: Die Oberfläche setzt immer Ziel und Richtung; ohne gültiges Ziel kein Auftrag (kein
            # Sonderfall). Zielabschluss unverändert bei target - 0,2; kein Kern-Halten.
            if not _zahl(ziel) or not 0 < ziel <= 100:
                return None, "manueller Auftrag ohne Ziel"
            if _zahl(soc) and soc >= ziel - 0.2:
                return None, None
            return k.Auftrag("NETZLADEN", beginn, ende, "manual", max_laden, ziel), None
        if modus == "discharge":
            # B08: endliche Entladung mit Gerätemaximum bis Ziel + 0,2 (Akkuenergie bzw. SoC-Ziel); die Reserve
            # bleibt Boden. Ohne gültiges Ziel kein Auftrag, nur Diagnose (kein Sonderfall).
            if not _zahl(ziel) or not 0 < ziel <= 100:
                return None, "manuelles Entladen ohne Ziel"
            if _zahl(soc) and soc <= ziel + k.ENTLADE_ABSTAND_PP:
                return None, None
            return k.Auftrag("ENTLADEN", beginn, ende, "manual", None, ziel, energiebezug="akku"), None
        return None, "unbekannter Handauftrag"
    storm = plan.get("storm_grid_charge") or {}
    if (storm.get("active") is True
            and str(cfg.get("storm_guard_mode", "warn")).strip().lower() not in ("off", "aus", "0")):
        beginn, ende = _sekunden(storm.get("window_start")), _sekunden(storm.get("window_end"))
        frist = _sekunden(plan.get("valid_until_ts_ms"))
        if not all(_zahl(v) for v in (beginn, ende, frist)):
            return None, "Unwetter: Fenster oder Planfrist fehlt oder ist ungültig"
        if beginn <= jetzt < min(ende, frist):
            ziel, rate = storm.get("target_soc"), storm.get("charge_w")
            if not all(_zahl(v) for v in (ziel, rate, max_laden)) or not 0 <= ziel <= 100 or rate <= 0:
                return None, "Unwetter: Ziel oder Ladeleistung fehlt oder ist ungültig"
            revision = plan_revision or plan.get("plan_id") or plan.get("revision")
            return k.Auftrag("NETZLADEN", beginn, min(ende, frist),
                "produkt, Unwetter; " + str(revision), min(rate, max_laden), ziel,
                konto="unwetter:" + str(storm["window_start"])), None
    # M05: Preis-Boost aus dem gebundenen Plan, in der Produktreihenfolge vor dem Markt (kein Dienstzustand).
    boost, boost_hinweis = _boost_auftrag(plan, soc, max_laden, jetzt)
    if boost is not None or boost_hinweis:
        return boost, boost_hinweis
    # Der Markt kommt aus dem vor der Entscheidung gebundenen Marktvertrag (Erzeuger), nicht aus dem
    # Produktergebnis; nur ohne gebundene Aufnahme (ältere Aufrufer) bleibt das Ergebnis die Quelle.
    quelle = ergebnis if markt is None else markt
    state = quelle.get("state", "")
    contract = quelle.get("market_economics_contract") or {}
    ende = _sekunden(contract.get("end_ts"))
    beginn = _sekunden(contract.get("start_ts"))
    forecast = quelle.get("market_economics_forecast") or contract.get("forecast") or {}
    if state in ("market_discharge_hold", "market_grid_done_hold", "market_grid_wait", "market_grid_pv_wait"):
        ende = _sekunden(quelle.get("market_late_fill_window_end_ts")) or ende
        vertrag_ende = _sekunden(contract.get("end_ts"))
        if (quelle.get("market_plan_grace") or {}).get("active") is True and vertrag_ende is not None:
            # Planübergangs-Karenz des Produkts: Es hält den Vertrag an der Slotgrenze bis zum Folgeplan,
            # höchstens PLAN_STALE_GRACE_MS über das Vertragsende; der Kern hält im selben Fenster.
            ende = max(ende, vertrag_ende + PLAN_STALE_GRACE_MS / 1000.0)
        if ende is not None and beginn is not None:
            return k.Auftrag("HALTEN", beginn, ende, "produkt, Marktvertrag"), None
        return None, "Markthalten ohne Vertragsfenster"
    if state in ("market_grid_charge", "market_negative_absorb_grid"):
        ziel = quelle.get("market_economics_target_soc_pct", forecast.get("grid_charge_target_soc_pct"))
        rate = quelle.get("market_economics_planned_charge_w", forecast.get("grid_charge_planned_charge_w"))
        if state == "market_negative_absorb_grid":
            # Die Negativpreis-Aufnahme fordert die freigegebene Geräteleistung;
            # val enthält bereits den eigenen Phasenrahmen und ist kein Auftrag.
            maximum = safe_int(cfg.get("market_battery_max_w"), 0)
            if maximum <= 0:
                maximum = safe_int(cfg.get("cheap_grid_battery_max_w"), 0)
            rate = min(max_laden, maximum) if maximum > 0 and _zahl(max_laden) else max_laden
        if all(_zahl(v) for v in (beginn, ende, ziel, rate)):
            return k.Auftrag("NETZLADEN", beginn, ende, "produkt, Marktvertrag", rate, ziel), None
        return None, "Marktladen ohne Ziel, Rate oder Fenster"
    return None, None


def _boost_auftrag(plan, soc, max_laden, jetzt):
    """M05: Preis-Boost (aWATTar-Modus 2) wie im Produkt aus dem Preisfenster des Plans: Netzladen bis Ziel minus
    Hysterese, Rate höchstens Gerätemaximum, mindestens 300 W; den Anschlussrahmen prüft der Kern. Ein aktives
    Fenster ohne Ziel oder Rate ist eine Planlücke (Hinweis), kein erfundener Wert."""
    cheap = plan.get("cheap_grid_charge") or {}
    if _konfigzahl(plan.get("awattar_mode")) != 2 or cheap.get("active") is not True:
        return None, None
    window = cheap.get("active_window") or {}
    beginn = _sekunden(window.get("start_timestamp"))
    ende = _sekunden(window.get("end_timestamp", cheap.get("window_end")))
    if not (_zahl(beginn) and _zahl(ende) and beginn <= jetzt < ende):
        return None, None
    ziel, rate = _konfigzahl(cheap.get("target_soc")), _konfigzahl(cheap.get("charge_w"))
    hysterese = max(0.1, _konfigzahl(cheap.get("hysteresis_pct")) if _zahl(_konfigzahl(cheap.get("hysteresis_pct"))) else 0.5)
    if not (_zahl(ziel) and 0 <= ziel <= 100 and _zahl(rate) and rate > 0 and _zahl(max_laden)):
        return None, "Preis-Boost: Ziel oder Ladeleistung fehlt im Preisfenster"
    if not (_zahl(soc) and soc < ziel - hysterese):
        return None, None
    return k.Auftrag("NETZLADEN", beginn, ende, "plan, Preis-Boost", max(300.0, min(rate, max_laden)), ziel), None


def _hand_wirksam(manual, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig):
    """K5: Nicht die Handdatei, sondern der daraus tatsächlich wirksame Handauftrag
    (Ladesperre oder Netzladen bis Ziel) setzt den Vorhalt aus. Eine Quelle für
    „wirksam“: derselbe Bestandsauftrag wie für den Kernauftrag."""
    if not manual or manual_override_expired(manual, jetzt, manual_override_max_age_s(cfg)):
        return False
    return _bestand_auftrag(manual, {}, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig)[0] is not None


def _vorhalt(aufnahme, cfg, live, plan, hand_wirksam, jetzt, monoton, zeitbasis,
             regelung_an, max_entladen, vorzustand):
    """Dieselbe Kontorechnung, eigener Schattenzustand, keine Dienst-Endreferenz."""
    quelle = 'config:speichergroesse'
    capacity = backup_hold.number(cfg.get('speichergroesse'))
    if capacity is None or capacity <= 0:
        capacity = backup_hold.number(live.get('bat_full_cap_kwh'))
        quelle = 'live:bat_full_cap_kwh'
    capacity = capacity*1000 if capacity is not None and capacity > 0 else None
    previous = json.loads(vorzustand.vorhalt_konto) if vorzustand and vorzustand.vorhalt_konto else {}
    if not previous:
        previous = dict(closed_account=aufnahme.get('startkonto') or {})
    if not vorzustand or vorzustand.vorhalt_zeitbasis != zeitbasis:
        previous = dict(closed_account=previous.get('closed_account') or {})
    eigene = effective_ep_reserve_pct(cfg, {}, default=8.0)
    geraet = live_ep_reserve_details(cfg, live)['effective_pct']
    alter_boden = max([a.pct for a in vorzustand.reserve_anteile if a.quelle != 'vorhalt'] or [eigene]) if vorzustand else eigene
    basis = max(eigene, geraet if geraet is not None else alter_boden)
    contract = aufnahme['contract']
    abweichungen = ('A0-G3-1', 'A0-G3-2', 'A0-G3-3', 'A0-G3-4')
    status, grund = 'aktiv', 'backup_hold'
    if aufnahme.get('fehler'):
        status, grund = 'Schattenfehler', 'backup_shadow_error'
        # Der Fehler lässt eine angeforderte Vorhaltung unbekannt; Aus bleibt Aus.
        if backup_hold.config_contract(cfg)['mode'] != 2:
            grund = 'backup_disabled'
    elif regelung_an is False or contract['mode'] != 2:
        status, grund = 'aus', 'backup_disabled'
    elif hand_wirksam:
        status, grund = 'manuell', 'backup_manual_suspended'
    elif storage_emergency_status(live) in (1, 4):
        status, grund = 'Insel', 'backup_island'
    stamp = backup_hold.number(live.get('_ts'))
    # Nur die 10-s-Kernfrist (A0-G3-3); Regelung, RSCP-Fehler, SoC, Uhr und
    # Probenvertrag prüfen der Kern (Schutzliste 2) und die Kontorechnung selbst.
    if status == 'aktiv' and not (stamp is not None and 0 <= jetzt-stamp <= k.LIVE_FRIST_S):
        status, grund = 'Live ungültig', 'backup_live_unknown_or_stale'
    context = backup_hold.konto_fortschreiben(cfg, plan, now_s=jetzt,
        monotonic_s=monoton, live=live, reserve_wh=capacity*basis/100 if capacity else None,
        capacity_wh=capacity, max_discharge_w=max_entladen, previous=previous,
        # Codeschalter (A0-G3-1): Ablauf mit dem Umschaltauftrag, dann eine Rechnung ohne Schalter.
        prognose_bestaetigen=False, contract=contract, preisvertrag=aufnahme['preise'])
    context = context or dict(closed_account=previous.get('closed_account') or {})
    if status == 'aktiv' and not context.get('valid'):
        status, grund = (('Prognosefallback', 'backup_capacity_unknown') if capacity is None
                         else ('Live ungültig', context.get('reason', 'backup_live_unknown_or_stale')))
    if status == 'aktiv':
        grund = context['reason']
        if not context.get('execution_binding'):
            status = 'Prognosefallback'
        if aufnahme.get('hard_owner') is not False:
            context.update(grid_job=None, grid_reason='backup_external_owner')
        job = context.get('grid_job') or {}
        if ((job.get('source_scope') == 'E3DC_DC_ONLY' or aufnahme.get('dc_only'))
                and not aufnahme.get('grid_charge_enable')):
            context.update(grid_job=None, grid_reason='backup_dc_only_grid_forbidden')
    else:
        context.update(active=False, grid_job=None, reason=grund, grid_reason=grund)
    # Auch die 10-s-Kernfrist beendet Kontinuität: keine Übernahmeuhr oder Wh
    # aus Proben, die der Kern bereits als ungültig zurückgenommen hat.
    if status == 'Live ungültig':
        context.update(valid=False, takeover_since_mono=None, pv_recharge_credit_wh=0)
    context['status'] = status
    floor = context.get('floor_wh') if status in ('aktiv', 'Prognosefallback') else None
    if floor is None:
        pct = None
    else:
        pct = min(100.0, floor/capacity*100) if capacity else None
    # Ausgangsprojektion und deren Latch gehören ausschließlich zum Dienst.
    # Das Schattenkonto übernimmt nur Rechnung, Quellen und Bestandsfortschritt.
    konto = {key: value for key, value in context.items() if key not in (
        'locked', 'coverage_since_mono', 'discharge_cap_w', 'export_cap_w',
        'requested_discharge_w', 'confirmed_discharge_w', 'release_confirmed',
        'reaction_s', 'entry_buffer_wh', 'hysteresis_wh')}
    v = k.Vorhalt(status, grund, pct, floor, capacity, context.get('current_energy_wh'),
        json.dumps(konto, sort_keys=True, ensure_ascii=False, separators=(',', ':')),
        zeitbasis, stamp+k.LIVE_FRIST_S if stamp is not None else None, quelle, abweichungen)
    return v, context


def _vorhalte_auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig,
             *, vorhalt=None, context=None, quelle=None, hand_wirksam=False, plan_revision=None):
    # Die unabhängige Marktentscheidung wurde vor der Produkt- und Reserveentscheidung gebunden (M01–M03).
    # Ohne Marktleser (nur Aufrufer außerhalb des Dienstes) bleibt das Ergebnis die Quelle.
    markt = (quelle or {}).get('markt') if isinstance(quelle, dict) else None
    markt_fehler = isinstance(markt, dict) and markt.get('fehler') is True
    a, marker = _bestand_auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig, plan_revision,
        markt={} if markt_fehler else markt)
    if markt_fehler and a is None and marker is None:
        marker = "Markt: Aufnahme fehlgeschlagen"
    if hand_wirksam:
        return a, marker
    if ((quelle or {}).get('hard_owner') is not False
            or (context or {}).get('grid_reason') == 'backup_dc_only_grid_forbidden'):
        return (None if a and a.modus == 'NETZLADEN' else a), marker
    job = (context or {}).get('grid_job')
    if not job or not vorhalt or vorhalt.status != 'aktiv' or not lade_daten_gueltig:
        return a, marker
    # Dieselbe endliche Netzenergie und technische Mindestleistung wie im
    # Bestandsvertrag; der Kern begrenzt sie je tatsächlichem Ausgabefenster.
    rest = min(job['remaining_storage_wh']/job['efficiency'], job['grid_wh'])
    if rest*3600/3 < 300 or max_laden is None or max_laden < 300:
        return a, marker
    ende = min(job['end_ts_ms'], job['deadline_ts_ms'],
        (plan['backup_hold_observation']['execution'])['valid_until_ts_ms'],
        (plan['backup_hold_observation']['execution'])['source_issue_ts_ms']+10800000,
        (plan['backup_hold_observation']['execution'])['load_issue_ts_ms']+10800000)/1000
    ziel = min(100.0, job['target_energy_wh']/vorhalt.kapazitaet_wh*100)
    if a and a.modus == 'NETZLADEN' and _zahl(a.ziel_pct):
        ziel = max(ziel, a.ziel_pct)
    konto = ((context.get('closed_account') or {}).get('key', '')+':'+str(job['deadline_ts_ms'])
             +':'+str(int(job['start_ts_ms']//backup_hold.SLOT_MS)))
    return k.Auftrag('NETZLADEN', job['start_ts_ms']/1000, ende, 'Reserve-Netzladen',
        max_laden, ziel, rest, konto, a, job["target_energy_wh"]/job["efficiency"]), marker


def _vorab(plan, cfg, live, jetzt, verbraucher):
    """Nur Planrechnung; G3b kontiert VORAB nicht als endliches Netzladen."""
    if not cfg_bool(cfg, "predump_enable", True):
        return None, "predump_enable aus"
    meta = plan.get("target_curve_meta") or {}
    value = lambda key: plan.get(key, meta.get(key))
    if value("predump_enabled") is False:
        return None, "Plan: Pre-Dump aus"
    if not value("predump_curve_active"):
        return None, "kein Pre-Dump im Plan"
    window = predump_time_window(cfg, plan)
    if not window["valid"]:
        return None, "Pflichtfeld: " + window["reason_code"]
    if not window["start_ts"] <= jetzt < window["deadline_ts"]:
        return None, "Trajektorie waiting/expired"
    hard = predump_plan_is_hard(plan)
    if hard and not cfg_bool(cfg, "hard_predump_enable", False):
        return None, "hard_predump_enable aus"
    if plan.get("weather_reserve_active") is True:
        return None, "Wetterreserve"
    target, dump = value("predump_curve_soc"), value("predump_dump_wh")
    capacity = plan.get("battery_capacity", _konfigzahl(cfg.get("speichergroesse"))*1000)
    soc = live.get("SOC")
    maximum = _konfigzahl(cfg.get("maximaleentladeleistung"))
    floor_pct = _konfigzahl(cfg.get("storage_predump_min_soc", cfg.get("eco_dump_min_soc", cfg.get("ep_reserve_pct", 8))))
    if (not all(_zahl(v) for v in (target, dump, capacity, soc, maximum, floor_pct))
            or not 0 <= target <= 100 or not 0 <= floor_pct <= 100
            or not 0 <= soc <= 100 or capacity <= 0 or dump < 0 or maximum < 300):
        return None, "Pflichtfeld: Ziel, Restenergie, Ladestand, Kapazität oder Gerätemaximum"
    gate = adaptive_gate(cfg, plan, jetzt, soc, target, dump, hard)
    if not gate.get("allowed"):
        return None, gate.get("reason", "Gates geschlossen")
    target = max(floor_pct, gate.get("target_soc", target))
    trajectory = predump_trajectory_state(cfg, live, plan, jetzt, target,
        gate.get("planned_dump_wh", dump), hysteresis_pct=0.0)
    if trajectory.get("phase") in ("waiting", "expired", "invalid") or not trajectory:
        return None, "Trajektorie " + str(trajectory.get("phase", "invalid"))
    allowed = [v for v in verbraucher if not v.gesperrt and cfg_bool(cfg,
        "predump_" + ("wallbox" if v.art.startswith("wallbox_") else
            {"waermepumpe": "heatpump", "heizstab": "heater"}.get(v.art, "unbekannt")) + "_enable")]
    minimum = max((v.mindest_w for v in allowed if v.art.startswith("wallbox_")), default=0.0)
    if minimum > maximum:
        return None, "Wallbox-Mindestblock über Gerätemaximum"
    block = predump_wallbox_block_window(cfg, live, plan, dict(target_soc=target),
        jetzt, maximum, minimum, start_buffer_s=120.0) if minimum and not any(
            v.laeuft for v in allowed) else {}
    begin = max(window["start_ts"], block.get("start_ts", window["start_ts"]))
    revision = plan.get("_vorab_revision") or plan.get("plan_id") or plan.get("revision")
    if not revision:
        return None, "Pflichtfeld: Planrevision"
    return k.Auftrag("VORAB", begin, window["deadline_ts"],
        ("plan, Hard-Pre-Dump; " if hard else "plan, Pre-Dump; ") + str(revision),
        min(maximum, max(300.0, minimum, trajectory["required_w"])), target,
        max(0.0, (soc-target)*capacity/100)), "Pre-Dump"


NETZPUNKT_FELDER = ("mindest_w", "totband_w", "importwaechter_w", "lastmarge_w", "auf_w", "ab_w", "marge_w",
    "schritt_w")


def _dv_auftrag(dv, cfg, jetzt):
    """Direktvermarktung als eigener Kernauftrag aus der vor der Entscheidung gebundenen Lage (D01–D09).

    Wirkungslose Lagen (beobachten, HOLD, NORMAL, gesperrte, ungebundene oder nicht freigegebene Aktion wie
    DV-Netzladen) geben keinen Auftrag und sind abgedeckt. Der Ladeblock (CHARGE_BLOCK_WAIT) wird ein befristeter
    LADEBLOCK am gebundenen Planslot; Hausversorgung bleibt reservebegrenzt. Noch nicht gebaute Wirkungen bleiben
    ein fester Hinweis. Rückgabe: (Auftrag oder None, Hinweis oder None, Hinweis gilt nur ohne DV-Aktion)."""
    if not cfg_bool(cfg, "direct_marketing_enable", False):
        return None, None, False
    if not dv or dv.get("aufnahmefehler") is True or dv.get("aktiv") not in (True, False):
        return None, "Direktvermarktung: Aufnahme fehlt", False
    if dv["aktiv"] is False:
        return None, None, False
    if dv.get("phase5_feld") is True:
        return None, "Direktvermarktung: Phase 5", False
    # Die Preisformung der normalen Kurve (D09) rechnet der Kern selbst (Eingang.kurvenformung).
    formung = dv.get("kurvenformung")
    passiv = None if formung is None or isinstance(formung, dict) else "Direktvermarktung: Kurvenformung unbekannt"
    if not (dv.get("befehle") is True and not dv.get("fehler") and dv.get("gate") is True):
        return None, passiv, True
    ziel = dv.get("ziel")
    # Headroom-Halten vor einem Exportfenster sperrt wie der Ladeblock nur das Laden (D04).
    if ziel == "CHARGE_BLOCK_WAIT" or ziel == "HEADROOM_EXPORT" and dv.get("headroom_halten") is True:
        beginn, ende = _sekunden(dv.get("beginn_ms")), _sekunden(dv.get("ende_ms"))
        if not (_zahl(beginn) and _zahl(ende) and beginn <= jetzt < ende):
            return None, "Direktvermarktung: Ladeblock ohne Fenster", False
        return k.Auftrag("LADEBLOCK", beginn, ende,
            "Direktvermarktung; " + str(dv.get("aktion_id") or dv.get("slot_id") or "")), None, False
    if ziel in ("FORCE_EXPORT", "HEADROOM_EXPORT"):
        # D05/D06: endliche Entladung am gebundenen Planslot. Rate ist das Exportbudget als Akkuleistung (Energiebezug
        # Akku wie Planer und Produkt), begrenzt durch den Einspeiserahmen; Boden ist der DV-Reserveboden.
        beginn, ende = _sekunden(dv.get("beginn_ms")), _sekunden(dv.get("ende_ms"))
        rate, grenze = dv.get("export_w"), dv.get("export_grenze_w")
        boden, netzpunkt = dv.get("reserve_boden_pct"), dv.get("netzpunkt") or {}
        if not (_zahl(beginn) and _zahl(ende) and beginn <= jetzt < ende and _zahl(rate) and _zahl(boden)
                and all(_zahl(netzpunkt.get(key)) for key in NETZPUNKT_FELDER)):
            return None, "Direktvermarktung: Export ohne Fenster, Budget oder Boden", False
        if dv.get("wallbox_laedt") is True:
            return None, None, False      # Wie im Produkt: keine Batterieeinspeisung, solange die E3DC-Box lädt.
        if _zahl(grenze):
            rate = min(rate, grenze)
        if rate < k.ENTLADE_MINDEST_W:
            return None, None, False      # Wie im Produkt: unter 300 W keine Batterieeinspeisung.
        return k.Auftrag("ENTLADEN", beginn, ende, "Direktvermarktung; " + str(dv.get("aktion_id") or ""),
            rate, boden, energiebezug="akku", netzpunkt=k.Netzpunkt(**{key: float(netzpunkt[key])
                for key in NETZPUNKT_FELDER})), None, False
    if ziel == "FORCE_CHARGE_PV":
        # D03: quellengebundenes PV-Laden am gebundenen Planslot; das Angebot rechnet der Kern aus eigenen Messwerten.
        pv = dv.get("pv_laden") if isinstance(dv.get("pv_laden"), dict) else {}
        if pv.get("dc_first") is True:
            return None, "Direktvermarktung: PV-Speicher mit DC-first", False
        beginn, ende = _sekunden(dv.get("beginn_ms")), _sekunden(dv.get("ende_ms"))
        zahlen = ("ziel_pct", "hysterese_pct", "importwaechter_w", "mindest_w", "export_waechter_w")
        if not (_zahl(beginn) and _zahl(ende) and beginn <= jetzt < ende and _zahl(dv.get("lade_w"))
                and all(_zahl(pv.get(key)) for key in zahlen) and type(pv.get("nur_dc")) is bool):
            return None, "Direktvermarktung: PV-Speicher ohne Fenster, Ziel oder Grenzen", False
        if pv.get("messung_vollstaendig") is not True:
            return None, None, False      # Wie im Produkt: ohne vollständige PV-/Hausmessung kein PV-Speichern.
        rate = min(dv["lade_w"], pv["max_w"]) if _zahl(pv.get("max_w")) else dv["lade_w"]
        if rate < k.ENTLADE_MINDEST_W:
            return None, None, False      # Kein Ladebudget: das Produkt speichert ebenfalls nicht.
        recht = k.PvLaderecht(float(pv["importwaechter_w"]), float(pv["mindest_w"]), float(pv["hysterese_pct"]),
            pv["nur_dc"], float(pv["export_grenze_w"]) if _zahl(pv.get("export_grenze_w")) else None,
            float(pv["export_waechter_w"]), float(pv["dc_ueberschuss_w"]) if _zahl(pv.get("dc_ueberschuss_w")) else None,
            **{key: float(pv[key]) for key in ("rampe_w", "halte_s", "schritt_w") if _zahl(pv.get(key))})
        return k.Auftrag("PVLADEN", beginn, ende, "Direktvermarktung; " + str(dv.get("aktion_id") or ""),
            float(rate), float(pv["ziel_pct"]), energiebezug="akku", pv_laderecht=recht), None, False
    return None, "Direktvermarktung: " + str(ziel)[:40], False


PLATZ_ZAHLEN = ("beginn_s", "frist_s", "boden_pct", "rest_wh", "min_w", "max_w", "min_pv_w", "luecke_s",
    "export_marge_w", "importwaechter_w")


def _platz_auftrag(p, live, verbraucher, jetzt, messung_gueltig=False):
    """N04: Platzschaffen als derselbe endliche Entladeauftrag (Restenergie Akku, eigenes Konto im Kern).

    Ausführungsvertrag aus dem Plan (Restenergie, Frist, harter Boden); einfache Bedingungen wie im Regler: genug PV,
    keine ladende Wallbox, Exportraum unter dem Einspeiseziel und kein Netzbezug über dem Wächter. Abregeldruck
    prüft der Kern (Veto). Ohne Aufnahme (Aufrufer außerhalb des Dienstes) kein Auftrag. Boden aus Kurve und Ziel,
    Slotkonto, Ein-/Haltehysterese, Kühlzeit, Plateau, Druckleistung mit Rampe und Ortstag rechnet der Kern aus der
    gebundenen Regel; fehlt sie, bleibt das eine Fehlstelle."""
    if not p:
        return None, None
    if p.get("aufnahmefehler") is True or p.get("aktiv") not in (True, False):
        return None, "Platzschaffen: Aufnahme fehlt"
    if p["aktiv"] is False:
        return None, None
    if not all(_zahl(p.get(key)) for key in PLATZ_ZAHLEN) or not isinstance(p.get("konto"), str):
        return None, "Platzschaffen: Aufnahme unvollständig"
    regel = _platzregel(p.get("regel"))
    offen = None if regel is not None else "Platzschaffen: Regel fehlt"
    pv, netz = live.get("PV_Power"), live.get("Grid_Power")
    if not (messung_gueltig and _zahl(pv) and _zahl(netz) and _zahl(p.get("export_ziel_w"))) or pv < p["min_pv_w"]:
        return None, offen
    if any(v.art.startswith("wallbox_") and v.laeuft for v in verbraucher):
        return None, offen
    raum = p["export_ziel_w"] - max(0.0, -netz) - p["export_marge_w"]
    if max(0.0, netz) > p["importwaechter_w"] or raum < p["min_w"]:
        return None, offen
    rate = min(p["max_w"], raum, p["rest_wh"] * 3600.0 / p["luecke_s"])
    if rate < p["min_w"] or not p["beginn_s"] <= jetzt < p["frist_s"]:
        return None, offen
    return k.Auftrag("ENTLADEN", p["beginn_s"], p["frist_s"], "Platzschaffen; " + p["konto"], rate, p["boden_pct"],
        rest_wh=p["rest_wh"], konto=p["konto"], energiebezug="akku", druckveto=True,
        tagesgrenze_wh=p.get("tagesgrenze_wh") if _zahl(p.get("tagesgrenze_wh")) else None, platz=regel), offen


PLATZREGEL_ZAHLEN = ("endziel_pct", "plateau_pct", "ein_pct", "halte_pct", "kuehl_s", "horizont_h", "schritt_w",
    "druck_wh", "min_druck_wh", "kapazitaet_wh", "min_w")


def _platzregel(r):
    """Regel des Platzschaffens aus der gebundenen Aufnahme; unvollständig → None (Fehlstelle, keine Ersatzwerte)."""
    if not isinstance(r, dict) or not all(_zahl(r.get(key)) for key in PLATZREGEL_ZAHLEN):
        return None
    if r["horizont_h"] <= 0 or r["kapazitaet_wh"] <= 0:
        return None
    wert = lambda key: float(r[key]) if _zahl(r.get(key)) else None
    return k.Platzregel(wert("kurven_boden_pct"), wert("ausfuehrungsziel_pct"), wert("zielkurve_pct"),
        *(float(r[key]) for key in PLATZREGEL_ZAHLEN), slot=str(r["slot"]) if r.get("slot") else None,
        slot_rest_wh=wert("slot_rest_wh"), tag=r["tag"] if type(r.get("tag")) is int else None)


def _netzrueckfall(n):
    """Netzrückfall des Pre-Dump aus der gebundenen Aufnahme; (Rückfall oder None, Hinweis oder None).

    Ohne Aufnahme (Aufrufer außerhalb des Dienstes) bleibt nur der Verbraucherauftrag; der Dienst bindet sie immer."""
    if not n:
        return None, None
    if n.get("aufnahmefehler") is True or n.get("aktiv") not in (True, False):
        return None, "Pre-Dump-Netzfallback: Aufnahme fehlt"
    if n["aktiv"] is False:
        return None, None
    zahlen = ("rate_w", "last_w", "schwelle_w", "warte_s", "schritt_w", "rampe_w")
    if not all(_zahl(n.get(key)) for key in zahlen) or not all(type(n.get(key)) is bool
            for key in ("erlaubt", "faellig", "frist_nah", "verfuegbar", "bev_fenster", "bev_faellig", "waechter")):
        return None, "Pre-Dump-Netzfallback: Aufnahme unvollständig"
    return k.Netzrueckfall(n["erlaubt"], n["faellig"], n["frist_nah"], float(n["rate_w"]), n["verfuegbar"],
        float(n["last_w"]), float(n["schwelle_w"]), float(n["warte_s"]),
        float(n["export_grenze_w"]) if _zahl(n.get("export_grenze_w")) else None,
        float(n["hard_grenze_w"]) if _zahl(n.get("hard_grenze_w")) else None, float(n["schritt_w"]),
        n["bev_fenster"], n["bev_faellig"], n["waechter"], float(n["rampe_w"])), None


def _auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig,
             *, live=None, verbraucher=(), intent=None, kern_vorzustand=None, dv=None, platz=None,
             platz_messung=False, **kwargs):
    a, marker = _vorhalte_auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan,
        lade_daten_gueltig, **kwargs)
    # Hand, Reserve, Unwetter und Preis-Boost bleiben vor dem Verbraucherpfad.
    if marker or a and a.quelle != "produkt, Marktvertrag":
        return a, marker
    # Direktvermarktung steht wie im Produkt vor Markt und Wallbox; ohne DV-Aktion folgt der normale Weg.
    dv_auftrag, dv_hinweis, passiv = _dv_auftrag(dv if isinstance(dv, dict) else {}, cfg, jetzt)
    if dv_auftrag is not None or dv_hinweis and not passiv:
        return dv_auftrag, dv_hinweis
    wallbox = _wallbox_auftrag(verbraucher, intent or {}, jetzt,
        kwargs.get("hand_wirksam", False), kern_vorzustand)
    if wallbox is not None:
        # Laufendes Akku-Netzladen aus dem Marktvertrag geht dem Haltewunsch einer
        # netzladenden Wallbox vor; Markthalten und Pre-Dump bleiben unverändert.
        markt = a is not None and a.modus == "NETZLADEN" and a.beginn_s <= jetzt < a.ende_s
        return (a if markt else wallbox), dv_hinweis
    vorab, grund = _vorab(dict(plan, _vorab_revision=kwargs.get("plan_revision")),
        cfg, live or {}, jetzt, verbraucher)
    if grund.startswith("Pflichtfeld:"):
        return None, "; ".join(filter(None, (dv_hinweis, "Pre-Dump: " + grund)))
    if vorab or a:
        return vorab or a, dv_hinweis
    # Platzschaffen ist wie im Produkt die nachrangige Entladung der normalen Regelung.
    platz_auftrag, platz_hinweis = _platz_auftrag(platz if isinstance(platz, dict) else {}, live or {}, verbraucher,
        jetzt, platz_messung is True)
    return platz_auftrag, "; ".join(filter(None, (dv_hinweis, platz_hinweis))) or None


def _verbraucher(live, native, cfg, intent, jetzt, energy_decision=None):
    intent_age = runtime_input_age_s(safe_float(intent.get("ts"), 0.0), jetzt)
    types = _wallbox_slot_types(cfg, intent if intent_age is not None and intent_age <= 60.0 else {})
    power = wallbox_actual_power_snapshot(live, native, now_s=jetzt,
        cfg=cfg, external_types=types, max_age_s=k.LIVE_FRIST_S, sample_max_age_s=k.UEBERBRUECKUNG_S, require_fresh_evidence=True)
    result = []
    frisch = intent_age is not None and intent_age <= 60.0
    request = intent.get("battery_request") if frisch else None
    netzladen = bool(request == "hold_discharge" and (intent.get("scheduled_slot_active")
        or intent.get("price_plan_storage_protect") or intent.get("reason") in
        ("slot_grid_storage_protection", "price_plan_storage_protection")))
    gesperrt = not frisch or intent.get("wb_mode_active") == 0 or any(intent.get(key) is True for key in
        ("manual_pause", "bev_full_blocked", "battery_departure_blocked"))
    def box(art, messwert):
        ist = messwert.zahl(jetzt, 0, k.LEISTUNG_MAX_W)
        anfrage = native.get("wb_possible_power_w")
        if not _zahl(anfrage):
            anfrage = ist
        # Die Gruppennachfrage wird anschließend nach Messanteilen aufgeteilt.
        v = k.Verbraucher(art, messwert, art != "wallbox_nativ",
            ist is not None and ist > k.LADEBELEG_W, anfrage, CONSUMER_MIN_W["wallbox"],
            request == "allow_discharge" and intent.get("battery_support_authorized") is not False if request is not None else None,
            intent.get("effective_wb_floor_soc", intent.get("wbminsoc", cfg.get("wbminsoc"))),
            _box_kappe(intent), intent.get("curve_support_class"),
            intent.get("pv_only_authorized_w", intent.get("pv_only_allowed_w")), netzladen, gesperrt)
        deckel_w = v.watt(jetzt)
        return replace(v, laeuft=deckel_w is not None and deckel_w > k.LADEBELEG_W,
            leistung=replace(messwert, wert=None, gueltig=False, grund="fehlend oder abgelaufen")
                if deckel_w is None else replace(messwert, grund="überbrückt") if ist is None else messwert)
    if any(wallbox_type_is_e3dc(v) for v in types.values()):
        # Der native E3DC-Messpunkt ist der separate Live-Wert. Der alte
        # native_w-Summenname umfasst auch externe Statusdetails.
        result.append(box("wallbox_nativ", _messwert(power.get("live_w"),
            "live, separater E3DC-Messpunkt", live.get("_ts"), power.get("live_evidence_valid"))))
    extern = [cid for cid, typ in types.items() if typ and not wallbox_type_is_e3dc(typ)
              and typ not in AUSGESCHALTETE_BOX_TYPEN]
    if extern:
        details = {d.get("id"): d for d in native.get("wb_details", []) if isinstance(d, dict)}
        zeiten = []
        for cid in extern:
            d = details.get(cid, {})
            observation = d.get("observation") or {}
            ts = _sekunden(observation.get("sample_ts", d.get("driver_status_last_sample_ts",
                                                     native.get("driver_status_last_sample_ts"))))
            if not _zahl(ts):
                zeiten = []
                break
            zeiten.append(ts)
        # Ältester belegter Punkt bindet die Summe. Keine künstliche Verjüngung.
        result.append(box("wallbox_extern", _messwert(power.get("external_w"),
            "wallbox_native, externe Einzelmessungen", min(zeiten) if zeiten else None,
            power.get("external_w") is not None)))
    if len(result) > 1:
        gesamt = sum(v.leistung.wert or 0 for v in result)
        result = [replace(v, anfrage_w=(v.anfrage_w * (v.leistung.wert or 0) / gesamt
            if gesamt > 0 and _zahl(v.anfrage_w) else v.anfrage_w if i == 0 else 0))
            for i, v in enumerate(result)]
    wp_contract = live.get("Heatpump_PV_Contract") or {}
    for art, prefix, feld, bekannt, vorhanden in (
        ("waermepumpe", "Heatpump", "Heatpump_Power", "Heatpump_Power_Known",
            str(cfg.get("luxtronik", "")).strip().lower() in ("1", "true", "yes", "on")
            or str(cfg.get("wp_type", "")).strip() in ("0", "1", "3", "4", "5", "6", "7")),
        ("heizstab", "Heater", "Heizstab_Power", "Heater_Power_Known",
            "Heizstab_Power" in live or cfg.get("heizstab"))):
        if not vorhanden:
            continue
        messung = _messwert(live.get(feld, live.get("WP_Power") if art == "waermepumpe" else None),
            "live:" + feld, live.get("_ts") if art == "waermepumpe"
                and str(cfg.get("wp_type", "")).strip() == "6" else live.get(prefix + "_Evidence_TS"),
            live.get(bekannt) is True)
        wert = messung.zahl(jetzt, 0, k.LEISTUNG_MAX_W)
        laeuft = (live.get("Heatpump_Compressor_Running") is True
            if art == "waermepumpe" and live.get("Heatpump_Compressor_Observation_Valid") is True
            else wert is not None and wert >= (500 if art == "waermepumpe" else 1))
        vertrag = wp_contract if art == "waermepumpe" else {}
        bruecke = vertrag.get("bridge_battery_w")
        start_w = live.get(prefix + "_Start_Request_W")
        # Ein gültiges Startangebot ist keine Leistungsmessung. Beide Verträge
        # bleiben getrennt: unbekannte Istleistung bleibt unverändert im Haus.
        anfrage_w = max(wert or 0, start_w) if _zahl(start_w) else wert
        v = k.Verbraucher(art, messung, True, laeuft, anfrage_w,
            CONSUMER_MIN_W["heatpump" if art == "waermepumpe" else "heater"],
            bruecke > 0 if _zahl(bruecke) else None, 0.0,
            bruecke if _zahl(bruecke) else None,
            gesperrt=art == "waermepumpe" and live.get("Heatpump_Pause_Active") is True)
        if art == "waermepumpe":
            deckel_w = v.watt(jetzt)
            v = replace(v, leistung=replace(messung, wert=None, gueltig=False,
                    grund="fehlend oder abgelaufen") if deckel_w is None else
                    replace(messung, grund="überbrückt") if wert is None else messung,
                laeuft=deckel_w is not None and (live.get("Heatpump_Compressor_Running") is True
                    if live.get("Heatpump_Compressor_Observation_Valid") is True else deckel_w >= 500))
        result.append(v)
    return tuple(_waerme_senke(v, cfg, energy_decision or {}, jetzt, live) if v.art == "waermepumpe"
        else v for v in result)


def _waerme_senke(v, cfg, record, jetzt, live):
    decision = record.get("decision") or {}
    if decision.get("predump_heatpump_active") is True:
        return v  # Pre-Dump bleibt im eigenen Wärmepfad.
    request = decision.get("heat_tariff_shift") or {}
    window = tariff_window(cfg, jetzt)
    ts, ende = request.get("ts"), request.get("valid_until_ts")
    frist, revision = None, request.get("intent_revision")
    if (cfg.get("heat_tariff_shift_mode") == "active" and request.get("mode") == "active"
            and (request.get("active") is True or request.get("would_start") is True)
            and _zahl(ts) and 0 <= jetzt-ts <= 30 and _zahl(ende)
            and isinstance(revision, str) and revision
            and window["valid"] and (request.get("window") or {}).get("revision") == window["revision"]):
        frist = min(ende, window["end_ts"])
    elif grid_boost_active(cfg, record, jetzt):
        frist, revision = float(record["ts"]) + 45, "heatpump_grid_boost"
    evidence_ts = live.get("Heatpump_Evidence_TS")
    if (frist is None or not jetzt < frist
            or not _zahl(evidence_ts) or not 0 <= jetzt-evidence_ts <= k.UEBERBRUECKUNG_S
            or v.watt(jetzt) is None):
        return v
    return replace(v, netzgedeckt=True, frist_s=frist, intent_revision=revision,
                   akku_freigabe=False)


def _box_kappe(intent):
    werte = [intent.get("wbminsoc_discharge_" + name + "_limit_w")
        for name in ("pull", "taper") if intent.get("wbminsoc_discharge_" + name + "_active") is True]
    werte = [v for v in werte if _zahl(v) and v >= 0]
    return max(werte) if werte else None


def _wallbox_auftrag(verbraucher, intent, jetzt, hand_wirksam, vorzustand):
    if hand_wirksam:
        return None
    if not any(v.netzladen_gewollt and v.laeuft and not v.gesperrt for v in verbraucher):
        return None
    ts = safe_float(intent.get("ts"), float("nan"))
    quelle = WALLBOX_QUELLENPRAEFIX + " gewolltes Netzladen"
    vorher = vorzustand.optimierung.vorgabe[1] if vorzustand and vorzustand.optimierung else None
    beginn = (vorher.beginn_s if isinstance(vorher, k.Auftrag) and vorher.modus == "HALTEN"
        and vorher.quelle == quelle and vorher.ende_s >= jetzt else ts)
    return k.Auftrag("HALTEN", beginn, ts + 60, quelle)


def eingang_bauen(uebergabe, quellen_vorzustand=None, kern_vorzustand=None):
    return eingang_und_quellen_bauen(uebergabe, quellen_vorzustand, kern_vorzustand)[0]


def eingang_und_quellen_bauen(uebergabe, quellen_vorzustand=None, kern_vorzustand=None):
    e = uebergabe.eingaenge
    live, plan, cfg, native, intent, manual, ergebnis, probe = map(auftauen,
        (e.live, e.plan, e.cfg, e.wb_native, e.wb_intent, e.manual, uebergabe.ergebnis, e.zeitprobe))
    jetzt = e.start
    # Anfrage und Rechte behalten die konfigurierten Verbraucher. Der bestehende
    # Ausfallfilter bereinigt nur die übrige Regelungssicht; der Messleser lehnt
    # abgelaufene Erzeugerwerte weiterhin selbst ab.
    verbraucher_cfg_basis, verbraucher_intent, verbraucher_native = cfg, intent, native
    # C09: Datenausfall aus der eigenen Aufnahme (gemeinsamer Leser, eigenes Gedächtnis), nicht aus dem Produkt;
    # ohne Aufnahme (Aufrufer außerhalb des Dienstes) derselbe Leser ohne Vorzustand.
    outage = auftauen(e.ausfall) or None
    if outage is None or outage.get("aufnahmefehler") is True:
        outage = wallbox_data_outage_context(cfg, intent, native, jetzt)
    cfg, intent, native = storage_inputs_without_failed_wallboxes(cfg, intent, native, outage)
    model = LiveDataSnapshot.from_dict(live, now_s=jetzt)
    cfg = effective_power_limit_cfg(cfg, model.raw, now_s=jetzt)
    ts = live.get("_ts")
    ts = ts if _zahl(ts) and ts > 0 else None
    reserve = live_ep_reserve_details(cfg, live)
    maximum = _leistungsgrenze(cfg, live, "charge", jetzt)
    entladen = _leistungsgrenze(cfg, live, "discharge", jetzt)
    zeit_ok = (probe.get("valid") is True and _zahl(probe.get("wall_ts"))
        and abs(probe["wall_ts"] - jetzt) <= k.UHR_ABWEICHUNG_S
        and _zahl(probe.get("monotonic_ts")) and probe.get("boot_id") and probe.get("process_epoch"))
    zeitbasis = (str(probe["boot_id"])+":"+str(probe["process_epoch"])) if zeit_ok else ""
    monoton = probe['monotonic_ts'] if zeit_ok else float('nan')
    quelle = auftauen(e.vorhalt)
    lade_ok = (model.soc_valid and model.grid_valid and model.battery_valid
        and model.plausibility.get("sample_valid") is True and live.get("Power_Decision_Usable") is not False)
    hand_wirksam = _hand_wirksam(manual, cfg, model.soc, maximum.wert, jetzt, plan, lade_ok)
    vorhalt, context = _vorhalt(quelle, cfg, live, plan, hand_wirksam, jetzt, monoton, zeitbasis,
        e.regelung_an, entladen.wert, kern_vorzustand)
    marker = None
    sonder = [marker] if marker else []
    if quelle.get("fehler"):
        sonder.append("Vorhaltung: Schattenaufnahme fehlgeschlagen")
    if quelle["contract"]["mode"] == 2 and vorhalt.kapazitaet_wh is None:
        sonder.append("Vorhaltung: Kapazität unbekannt")
    # Direktvermarktung bewertet der Kern selbst aus der gebundenen Lage (kein Produktzustand als Eingang).
    for name, status in auftauen(e.funktionsstatus).items():
        if name not in ("Unwetter", "Wärme-Tarif", "Pre-Dump", "Vorhaltung", "Preis-Boost", "Direktvermarktung",
                        "Lastspitzen") and (
                status.get("angefordert") is True or status.get("wirksam") is True):
            sonder.append(name)
    # Konfiguriert allein erzeugt keinen Marker. Lastspitzen rechnet der Kern aus der eigenen Aufnahme.
    spitze, spitzen_auftrag, spitzen_hinweis = _spitze(auftauen(e.spitze), jetzt)
    if spitzen_hinweis:
        sonder.append(spitzen_hinweis)
    lastkappe, last_hinweis = _lastkappe(auftauen(e.last))
    if last_hinweis:
        sonder.append(last_hinweis)
    if (cfg_bool(cfg, "storage_curve_sliding_horizon_enable", False)
            and ergebnis.get("sliding_horizon_active") is not False):
        # N03: Der gleitende Prognosehorizont (Option) ist im Kern nicht nachgebaut; sichtbar statt vorgetäuscht,
        # sobald er im Zyklus wirkt. Eine unbekannte Wirkung zählt als Wirkung.
        sonder.append("Kurve: gleitender Prognosehorizont")
    haus_quelle ="Bilanz" if model.plausibility.get("home_independent") is False or "balance" in model.plausibility.get("home_source", "").lower() else model.plausibility.get("home_source", "live")
    hauswert = model.home_w
    if live.get("Home_Includes_WP") is True:
        # Der gemeinsame Erzeuger kann die separat geführte WP aus Home
        # entfernt haben. Der Kern führt keine zweite WP-Senke: Gesamtmessung
        # erhalten, damit die physische Last weiterhin genau einmal zählt.
        hauswert = control_home_power_w(dict(Home_Power=live.get("Home_Power_Raw"),
                                            Home_Power_Valid=model.home_valid))
    reserve_ts = live.get("ep_reserve_observed_at_s", ts)
    house_fuse = auftauen(e.house_fuse)
    verbraucher_quelle = auftauen(e.verbraucher)
    verbraucher_cfg = dict(verbraucher_cfg_basis, **verbraucher_quelle["cfg"])
    verbraucher = _verbraucher(live, verbraucher_native, verbraucher_cfg, verbraucher_intent,
        jetzt, auftauen(e.energy_decision))
    # Ein vom Produkt projizierter Pre-Dump-Boden ist kein normales Recht.
    # Der Kern setzt seinen eigenen Zielboden erst aus dem eigenen Auftrag. Gelesen wird aus
    # derselben Sicht wie die Verbraucher, sonst gäbe ein Datenausfall den Pre-Dump-Boden frei.
    if (verbraucher_intent.get("predump_wallbox_active") is True
            or verbraucher_intent.get("reason") == "predump_wallbox_consumer"):
        normalboden = verbraucher_intent.get("wbminsoc")
        if normalboden is None:
            normalboden = verbraucher_cfg.get("wbminsoc")
        verbraucher = tuple(replace(v, boden_pct=normalboden)
            if v.art.startswith("wallbox_") else v for v in verbraucher)
    auftrag, marker = _auftrag(manual, ergebnis, cfg, model.soc, maximum.wert, jetzt, plan, lade_ok,
        vorhalt=vorhalt, context=context, quelle=quelle, hand_wirksam=hand_wirksam, plan_revision=e.plan_revision,
        live=live, verbraucher=verbraucher, intent=verbraucher_intent, kern_vorzustand=kern_vorzustand,
        dv=auftauen(e.dv), platz=auftauen(e.platz), platz_messung=bool(model.pv_valid and model.grid_valid))
    if marker:
        sonder.append(marker)
    # V03: Der Netzrückfall kommt aus der vor der Entscheidung gebundenen Aufnahme (dieselben Leser wie der
    # Produktpfad, kein Produktergebnis); Warteuhr und laufenden Rückfall führt der Kern selbst.
    if auftrag and auftrag.modus == "VORAB" and cfg_bool(cfg, "predump_enable", True):
        rueckfall, hinweis = _netzrueckfall(auftauen(e.vorab_netz))
        if hinweis:
            sonder.append(hinweis)
        if rueckfall is not None:
            auftrag = replace(auftrag, netzrueckfall=rueckfall)
    if auftrag is None and spitzen_auftrag is not None:
        auftrag = spitzen_auftrag          # L03: Nachladen des Puffers nur ohne anderen Auftrag (nachrangig).
    if auftrag and auftrag.modus == "VORAB":
        verbraucher = tuple(replace(v, vorab_angemeldet=bool(not v.gesperrt and cfg_bool(cfg,
            "predump_" + ("wallbox" if v.art.startswith("wallbox_") else
                {"waermepumpe": "heatpump", "heizstab": "heater"}.get(v.art, "unbekannt")) + "_enable")))
            for v in verbraucher)
    # Haltezone aus derselben Sicht wie die Verbraucher: Behält eine ausgefallene Box
    # Anfrage und Rechte, behält sie auch die Haltezone (sonst gäbe der Datenausfall
    # Akkubudget frei).
    zone_intent = verbraucher_intent
    alter = runtime_input_age_s(safe_float(zone_intent.get("ts"), 0.0), jetzt)
    zone = k.HaltezonenEingang(eligible=bool(alter is not None and alter <= 60
        and zone_intent.get("wb_control_mode") == 10 and not zone_intent.get("external_wallbox_manager")
        and not any(zone_intent.get(key) for key in ("manual_pause", "bev_full_blocked", "battery_departure_active",
            "wbminsoc_runtime_raise_active", "price_opt_active", "scheduled_slot_active",
            "price_boost_active"))
        # C03: Am eigenen Pre-Dump-Ziel (Bodenhalt) keine Haltezone, wie der Wallbox-Stopp des Produkts.
        and not (kern_vorzustand is not None and kern_vorzustand.vorab_latch is not None)),
        mode=zone_intent.get("wb_mode_active"),
        hysterese_pct=zone_intent.get("wb_soc_hysterese_pct", verbraucher_cfg.get("wb_soc_hysterese_pct",
            verbraucher_cfg.get("wb_hysterese_pct"))),
        neustart_pct=verbraucher_cfg.get("wb_target_restart_above_wbminsoc_pct", 2.0))
    nativ = [p.leistung.zahl(jetzt, 0, k.LEISTUNG_MAX_W) for p in verbraucher if p.art == 'wallbox_nativ']
    nativ_w = sum(nativ) if all(v is not None for v in nativ) else None
    # Dieselben vorhandenen Messwertverträge wie im Kern; keine zweite Wertregel.
    pv_messwert = _messwert(model.pv_w, "live:PV", ts, model.pv_valid)
    netz_messwert = _messwert(model.grid_w, "live:Netz", ts, model.grid_valid)
    akku_messwert = _messwert(model.battery_w, "live:Akku", ts, model.battery_valid)
    leistungsdaten_gueltig = all(w.zahl(jetzt, unten, k.LEISTUNG_MAX_W) is not None
        for w, unten in ((pv_messwert, 0), (netz_messwert, -k.LEISTUNG_MAX_W),
                         (akku_messwert, -k.LEISTUNG_MAX_W)))
    kurve = _kurve(plan, cfg, live, jetzt)
    # Dieselben eingefrorenen Eingänge wie der Kern; kein zweiter Rohsatz.
    quellen = schatten_quellen(cfg, live, haus_w=hauswert,
        nativ_w=nativ_w, kurve=kurve, max_laden_w=maximum.wert,
        leistungsdaten_gueltig=leistungsdaten_gueltig,
        vorzustand=quellen_vorzustand, jetzt_s=jetzt)
    fahrzeug = live.get("managed_vehicle_phases")
    fahrzeug = fahrzeug if isinstance(fahrzeug, dict) else {}
    fahrzeugwerte = fahrzeug.get("phase_w")
    fahrzeugwerte = fahrzeugwerte if isinstance(fahrzeugwerte, (tuple, list)) else ()
    return k.Eingang(jetzt_s=jetzt, monoton_s=probe["monotonic_ts"] if zeit_ok else float("nan"),
        zeitbasis=(str(probe["boot_id"]) + ":" + str(probe["process_epoch"])) if zeit_ok else "",
        messzeit_s=ts, soc=_messwert(model.soc, "live:SOC", ts, model.soc_valid),
        notstrom=_messwert(storage_emergency_status(live), "live:Notstrom", ts),
        pv=pv_messwert,
        haus=_messwert(hauswert, haus_quelle, ts, model.home_valid),
        netz=netz_messwert,
        akku=akku_messwert,
        max_laden=maximum, max_entladen=entladen,
        eigene_reserve=_messwert(effective_ep_reserve_pct(cfg, {}, default=8.0), "config", jetzt),
        geraetereserve=_messwert(reserve["effective_pct"], reserve["source"] or "Gerätereserve unbekannt", reserve_ts),
        anschluss_a=_messwert(_konfigzahl(house_fuse.get("effective")),
            "config:" + str(house_fuse.get("source", "unbekannt")), jetzt,
            house_fuse.get("source") not in (None, "invalid", "none")),
        anschluss_phasen=tuple(_messwert(live.get(key), "live:PM:" + key, ts,
            live.get("grid_pm_available") is not False and live.get("Grid_PM_Available") is not False)
            for key in ("grid_p1", "grid_p2", "grid_p3")),
        akku_phasen=(1 if live.get("pvi_ac_max_phase_count_valid") is True
            and type(live.get("pvi_ac_max_phase_count")) in (int, float)
            and live.get("pvi_ac_max_phase_count") == 1 else 3),
        fahrzeug_phasen=tuple(_messwert(w, "Wallbox-Manager:PM-Anteil", fahrzeug.get("ts"))
            for w in fahrzeugwerte),
        anschluss_abstand_a=max(0.0, safe_float(cfg.get("grid_max_reserve_amp"), 2.0)),
        regelung_an=e.regelung_an, rscp_fehler=e.rscp_fehler,
        verbraucher=verbraucher, haltezone=zone, kurve=kurve,
        vorhalt=vorhalt,
        auftrag=auftrag, sonderfunktion="; ".join(sonder) or None,
        einspeise_ladebedarf=k.Messwert(quellen.bedarf_w, quellen.bedarf_w is not None,
            'gemeinsame Druckrechnung:' + quellen.revision, quellen.zeit_s, quellen.grund),
        quellen_ladekappe=(_messwert(quellen.ladekappe_w, 'DC-only, gebundener Quellensplit',
            quellen.zeit_s, quellen.quellen['gueltig'])
            if quellen.ladekappe_angefordert else None),
        kurvenformung=_kurvenformung(auftauen(e.dv), cfg), spitze=spitze, last=lastkappe), quellen


def _spitze(s, jetzt):
    """L01–L03: Lastspitzen aus der gebundenen Aufnahme (gemeinsamer Intervallplaner, eigenes Gedächtnis).

    Rückgabe (Kappe/Puffer oder None, Netzladeauftrag oder None, Hinweis oder None). Ohne Aufnahme (Aufrufer
    außerhalb des Dienstes) keine Wirkung; ein Aufnahmefehler oder ein unbekannter Kandidat bleibt Fehlstelle."""
    if not s:
        return None, None, None
    if s.get("aufnahmefehler") is True or s.get("aktiv") not in (True, False):
        return None, None, "Lastspitzen: Aufnahme fehlt"
    klasse = s.get("klasse")
    if s["aktiv"] is False or klasse in (None, "release", "observe", "idle"):
        return None, None, None
    if klasse == "active_cap" and _zahl(s.get("ziel_w")):
        return k.Spitze("active_cap", float(s["ziel_w"])), None, None
    if klasse == "reserve_hold":
        return k.Spitze("reserve_hold"), None, None
    if klasse == "grid_recharge" and all(_zahl(s.get(key)) for key in ("lade_w", "puffer_pct", "ende_s")):
        # Nachladen des Puffers aus dem Netz innerhalb der Viertelstunde: derselbe Netzladeweg wie Markt und Unwetter.
        return None, k.Auftrag("NETZLADEN", jetzt, float(s["ende_s"]), k.SPITZE_NACHLADEN_QUELLE, float(s["lade_w"]),
            float(s["puffer_pct"])), None
    return None, None, "Lastspitzen: " + str(klasse)[:30]


def _lastkappe(l):
    """T06: geplante externe Last aus der gebundenen Aufnahme; (Kappe oder None, Hinweis oder None)."""
    if not l:
        return None, None
    if l.get("aufnahmefehler") is True or l.get("aktiv") not in (True, False):
        return None, "Geplante Last: Aufnahme fehlt"
    if l["aktiv"] is False:
        return None, None
    if l.get("klasse") == "halten":
        return k.Lastkappe("halten"), None
    if l.get("klasse") == "stuetzen" and _zahl(l.get("kappe_w")):
        return k.Lastkappe("stuetzen", float(l["kappe_w"])), None
    return None, "Geplante Last: " + str(l.get("klasse"))[:30]


def _kurvenformung(dv, cfg):
    """D09: Preiskurve und Ladereservierung der Direktvermarktung aus der gebundenen Lage, sonst keine Formung."""
    f = dv.get("kurvenformung") if dv.get("aktiv") is True and cfg_bool(cfg, "direct_marketing_enable", False) else None
    if not isinstance(f, dict) or not (_zahl(f.get("preis_w")) or _zahl(f.get("reservierung_w"))):
        return None
    return k.Kurvenformung(float(f["preis_w"]) if _zahl(f.get("preis_w")) else None,
        float(f["reservierung_w"]) if _zahl(f.get("reservierung_w")) else None,
        float(f["importwaechter_w"]) if _zahl(f.get("importwaechter_w")) else 80.0)
