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
from .leistungsleser import effective_power_limit_cfg, safe_float
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


def _bestand_auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig, plan_revision=None):
    if manual and not manual_override_expired(manual, jetzt, manual_override_max_age_s(cfg)):
        modus = manual.get("mode")
        ende = manual_override_deadline(manual, jetzt, manual_override_max_age_s(cfg))
        beginn = safe_float(manual.get("ts"), safe_float(manual.get("created_ts"), float("nan")))
        ziel = _konfigzahl(manual.get("target_soc"))
        if modus == "hold":
            return k.Auftrag("LADESP", beginn, ende, "manual"), None
        if modus == "charge":
            # Nutzervertrag: Zielabschluss unverändert bei target - 0,2; kein Kern-Halten.
            if _zahl(ziel) and ziel > 0 and _zahl(soc) and soc >= ziel - 0.2:
                return None, None
            if not _zahl(ziel):
                return None, "manueller Auftrag ohne Ziel"
            return k.Auftrag("NETZLADEN", beginn, ende, "manual", max_laden,
                             100.0 if ziel <= 0 else ziel), None
        if modus == "discharge":
            return None, "manuelles Entladen, Welle 2"
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
    # Markt und Preis-Boost behalten ihre aufgelösten Dienstverträge.
    state = ergebnis.get("state", "")
    contract = ergebnis.get("market_economics_contract") or {}
    ende = _sekunden(contract.get("end_ts"))
    beginn = _sekunden(contract.get("start_ts"))
    forecast = ergebnis.get("market_economics_forecast") or contract.get("forecast") or {}
    if state in ("market_discharge_hold", "market_grid_done_hold", "market_grid_wait", "market_grid_pv_wait"):
        ende = _sekunden(ergebnis.get("market_late_fill_window_end_ts")) or ende
        vertrag_ende = _sekunden(contract.get("end_ts"))
        if (ergebnis.get("market_plan_grace") or {}).get("active") is True and vertrag_ende is not None:
            # Planübergangs-Karenz des Produkts: Es hält den Vertrag an der Slotgrenze bis zum Folgeplan,
            # höchstens PLAN_STALE_GRACE_MS über das Vertragsende; der Kern hält im selben Fenster.
            ende = max(ende, vertrag_ende + PLAN_STALE_GRACE_MS / 1000.0)
        if ende is not None and beginn is not None:
            return k.Auftrag("HALTEN", beginn, ende, "produkt, Marktvertrag"), None
        return None, "Markthalten ohne Vertragsfenster"
    if state in ("market_grid_charge", "market_negative_absorb_grid"):
        ziel = ergebnis.get("market_economics_target_soc_pct", forecast.get("grid_charge_target_soc_pct"))
        rate = (ergebnis.get("market_grid_charge_control") or {}).get("rate_w")
        if rate is None:
            rate = ergebnis.get("val") if state == "market_negative_absorb_grid" else ergebnis.get("market_economics_planned_charge_w", forecast.get("grid_charge_planned_charge_w"))
        if all(_zahl(v) for v in (beginn, ende, ziel, rate)):
            return k.Auftrag("NETZLADEN", beginn, ende, "produkt, Marktvertrag", rate, ziel), None
        return None, "Marktladen ohne Ziel, Rate oder Fenster"
    if state in ("price_boost_grid", "price_boost_grid_wait"):
        cheap = plan.get("cheap_grid_charge") or {}
        window = cheap.get("active_window") or {}
        beginn = _sekunden(window.get("start_timestamp"))
        ende = _sekunden(window.get("end_timestamp", cheap.get("window_end")))
        ziel, rate = cheap.get("target_soc"), cheap.get("charge_w")
        if (cheap.get("active") is True and all(_zahl(v) for v in (beginn, ende, ziel, rate, max_laden))
                and beginn <= jetzt < ende and 0 <= ziel <= 100 and rate > 0):
            rate = min(rate, max_laden)
            return k.Auftrag("NETZLADEN", beginn, ende, "produkt, Preis-Boost", rate, ziel), None
    if state.startswith("price_boost"):
        return None, "Preis-Boost ohne aufgelösten vollständigen Auftrag"
    return None, None


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
    # Die unabhängige Marktentscheidung wurde vor der Reserveentscheidung gebunden.
    source = ergebnis
    contract = ergebnis.get('market_economics_contract') or {}
    if (contract.get('economics') or {}).get('charge_profile') == 'reserve':
        source = (quelle or {}).get('markt') or {}
    a, marker = _bestand_auftrag(manual, source, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig, plan_revision)
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


def _auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan, lade_daten_gueltig,
             *, live=None, verbraucher=(), intent=None, kern_vorzustand=None, **kwargs):
    a, marker = _vorhalte_auftrag(manual, ergebnis, cfg, soc, max_laden, jetzt, plan,
        lade_daten_gueltig, **kwargs)
    # Hand, Reserve, Unwetter und Preis-Boost bleiben vor dem Verbraucherpfad.
    if marker or a and a.quelle != "produkt, Marktvertrag":
        return a, marker
    wallbox = _wallbox_auftrag(None, verbraucher, intent or {}, jetzt,
        kwargs.get("hand_wirksam", False), kern_vorzustand)
    if wallbox is not None:
        return wallbox, None
    vorab, grund = _vorab(dict(plan, _vorab_revision=kwargs.get("plan_revision")),
        cfg, live or {}, jetzt, verbraucher)
    if grund.startswith("Pflichtfeld:"):
        return None, "Pre-Dump: " + grund
    return vorab or a, None


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


def _wallbox_auftrag(a, verbraucher, intent, jetzt, hand_wirksam, vorzustand):
    if hand_wirksam or a and a.modus != "HALTEN":
        return a
    if not any(v.netzladen_gewollt and v.laeuft and not v.gesperrt for v in verbraucher):
        return a
    ts = safe_float(intent.get("ts"), float("nan"))
    quelle = WALLBOX_QUELLENPRAEFIX + " gewolltes Netzladen" + ("; " + a.quelle if a else "")
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
    outage = ergebnis.get("wallbox_data_outage")
    if outage is None:
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
    state = str(ergebnis.get("state") or "")
    sonder = [marker] if marker else []
    if (state.startswith("storm_guard") or (auftauen(e.funktionsstatus).get("Unwetter") or {}).get("angefordert")) and not (
            (plan.get("storm_grid_charge") or {}).get("active") is True
            or (plan.get("storm_guard") or {}).get("control_active") is True):
        sonder.append("Unwetter: Plan fehlt")
    if quelle.get("fehler"):
        sonder.append("Vorhaltung: Schattenaufnahme fehlgeschlagen")
    if quelle["contract"]["mode"] == 2 and vorhalt.kapazitaet_wh is None:
        sonder.append("Vorhaltung: Kapazität unbekannt")
    for name, status in auftauen(e.funktionsstatus).items():
        if name not in ("Unwetter", "Wärme-Tarif", "Pre-Dump", "Vorhaltung", "Preis-Boost") and (status.get("angefordert") is True or status.get("wirksam") is True):
            sonder.append(name)
    # Konfiguriert allein erzeugt keinen Marker. Die Dienstentscheidung hat
    # die vorhandenen Pre-Dump-, Unwetter- und Wärme-Leser bereits ausgewertet.
    for name, aktiv in (
        ("Lastspitzen", state.startswith("peak_shaving") or (ergebnis.get("peak_shaving") or {}).get("applied") is True),
        ("Direktvermarktung", state.startswith("direct_marketing") or (ergebnis.get("storage_dispatch_phase5") or {}).get("applied") is True),
    ):
        if aktiv:
            sonder.append(name)
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
        live=live, verbraucher=verbraucher, intent=verbraucher_intent, kern_vorzustand=kern_vorzustand)
    if marker:
        sonder.append(marker)
    window = predump_time_window(cfg, plan)
    if (cfg_bool(cfg, "predump_enable", True) and (cfg_bool(cfg, "hard_predump_grid_enable")
            or ergebnis.get("predump_grid_fallback") is True)
            and window["valid"] and window["start_ts"] <= jetzt < window["deadline_ts"]
            and (plan.get("predump_curve_active") or (plan.get("target_curve_meta") or {}).get("predump_curve_active"))):
        sonder.append("Pre-Dump-Netzfallback")
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
        and not ergebnis.get("force_wallbox_stop")),
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
    last = quelle.get('regelbare_last_w')
    wb = [p.leistung.zahl(jetzt, 0, k.LEISTUNG_MAX_W) for p in verbraucher if p.art.startswith("wallbox_")]
    netz = netz_messwert.zahl(jetzt, -k.LEISTUNG_MAX_W, k.LEISTUNG_MAX_W)
    akku = akku_messwert.zahl(jetzt, -k.LEISTUNG_MAX_W, k.LEISTUNG_MAX_W)
    maximum_a = _konfigzahl(house_fuse.get('effective'))
    harte_restlast = (max(0.0, netz-akku-sum(wb)-last
        - max(0.0, maximum_a-max(0.0, safe_float(cfg.get('grid_max_reserve_amp'), 2.0)))*690)
        if netz is not None and akku is not None and last is not None
        and None not in wb and _zahl(maximum_a) and maximum_a > 0 else None)
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
            house_fuse.get("source") not in (None, "invalid", "none")
            and live.get("Grid_PM_Delta_Rule_Effective") is not True),
        anschluss_abstand_a=max(0.0, safe_float(cfg.get("grid_max_reserve_amp"), 2.0)),
        regelung_an=e.regelung_an, rscp_fehler=e.rscp_fehler,
        verbraucher=verbraucher, haltezone=zone, kurve=kurve,
        vorhalt=vorhalt, harte_restlast_w=harte_restlast,
        auftrag=auftrag, sonderfunktion="; ".join(sonder) or None,
        einspeise_ladebedarf=k.Messwert(quellen.bedarf_w, quellen.bedarf_w is not None,
            'gemeinsame Druckrechnung:' + quellen.revision, quellen.zeit_s, quellen.grund),
        quellen_ladekappe=(_messwert(quellen.ladekappe_w, 'DC-only, gebundener Quellensplit',
            quellen.zeit_s, quellen.quellen['gueltig'])
            if quellen.ladekappe_angefordert else None)), quellen
