"""Quellenbuchung und hypothetische Verbraucherprojektion, ausschließlich im Speicher."""
import json
import math
try:
    from Installer import consumer_priority as cp, control_time
    from Installer.heatpump_pv_contract import heatpump_start_reservation_duration_s
    from Installer.storage_dispatch_contract import revision_hash
    from Installer.Storage.kern import waerme_hauslast
except ModuleNotFoundError:
    import consumer_priority as cp
    import control_time
    from heatpump_pv_contract import heatpump_start_reservation_duration_s
    from storage_dispatch_contract import revision_hash
    from Storage.kern import waerme_hauslast


def _art(v):
    return "wallbox" if v.art.startswith("wallbox_") else {
        "waermepumpe": "heatpump", "heizstab": "heater"}.get(v.art)


def zuteilen(budget, *, order=None, now_s=0.0, previous_contract=None,
             wp_runon_s=600.0, reservations_w=None, priority_changed_at_s=-999999.0,
             wp_runon_active=None, wartend=(), gehalten=()):
    """Ein bestehender Zuteiler, zwei Quellen innerhalb seiner Verbrauchsbuchung.

    Ein Verbraucher in der Wartezeit nach einem nicht angenommenen Startangebot fragt in diesem Zyklus nichts an; sein
    Anteil geht an die übrigen (Rückforderung). Der Wärme-Nachlauf nach einem Prioritätswechsel kommt aus der
    gemeinsamen monotonen Wachuhr (wp_runon_active), nicht aus der Wandzeit. Ein gehaltener Verbraucher (offenes
    Startangebot, Phasenumschaltung) zählt wie ein laufender: Seine Reservierung wird vor der Reihenfolge gebucht,
    höchstens in Höhe der eigenen Anfrage."""
    previous = previous_contract or {}
    requests = dict.fromkeys(cp.CONSUMERS, 0)
    minimums = dict(cp.CONSUMER_MIN_W)
    enabled = dict.fromkeys(cp.CONSUMERS, False)
    active = dict(enabled)
    caps = dict(requests)
    for anteil in budget.verbraucher:
        v = anteil.verbraucher
        name = _art(v)
        if name is None or name in wartend:
            continue
        # Laufende Hauslast steht bereits im Hausmesspunkt des Budgets: kein zweiter Anteil.
        if v.anfrage_w is None or v.gesperrt or v.laeuft and waerme_hauslast(v):
            continue
        request = max(0, int(v.anfrage_w))
        if v.klasse in ("pv_only", "floor_closed") and v.pv_only_w is not None:
            request = min(request, max(0, int(v.pv_only_w)))
        requests[name] += request
        minimums[name] = max(minimums[name], int(v.mindest_w))
        enabled[name] = True
        active[name] = active[name] or v.laeuft
        caps[name] += max(0, int(anteil.battery_w.wert_w or 0))
    for name in gehalten:
        active[name] = active[name] or enabled.get(name, False)
    if budget.haltezone and budget.haltezone["active"]:
        requests["wallbox"] = min(requests["wallbox"], budget.haltezone["frame_w"])
    if reservations_w:
        # Eine Reservierung bucht einen Teil der eigenen Anfrage, nie mehr (kein zweiter Lastabzug).
        reservations_w = {name: min(max(0, int(w)), requests.get(name, 0)) for name, w in reservations_w.items()}
    pv = max(0, int(budget.pv_w.wert_w or 0))
    total = max(0, int(budget.rahmen_w or 0))
    pv = min(pv, total)
    contract = cp.allocate_consumer_budget_contract(total, requests, order,
        enabled=enabled, evidence_valid=budget.pv_w.gueltig and budget.rahmen_w is not None,
        minimums_w=minimums, maximums_w={name: max(requests[name], minimums[name]) for name in cp.CONSUMERS},
        active=active, reservations_w=reservations_w,
        previous_active=previous.get("active"), priority_changed_at_s=priority_changed_at_s,
        now_s=now_s, wp_runon_s=wp_runon_s, wp_runon_active_override=wp_runon_active, pv_pool_w=pv,
        battery_pool_w=total-pv, battery_caps_w=caps)
    contract.update(command_eligible=dict(enabled),
        command_allocations=dict(contract["allocations"]),
        accounting_allocations=dict(contract["allocations"]))
    accounting = cp.validate_consumer_budget_contract(contract)
    commands = cp.validate_consumer_command_allocations(contract)
    return dict(contract=contract, accounting=accounting, commands=commands,
        valid=accounting["valid"] and commands["valid"])


def betriebsrechte(budget, jetzt):
    """Betriebsrecht je Verbrauchergruppe, getrennt vom Zusatzbudget.

    Laufende Wärme ohne eigenen Akkuvertrag steckt im Hausmesspunkt: Sie darf weiterlaufen (Betriebsrecht), bekommt
    aber keinen zweiten Budgetanteil (Zusatzbudget 0). Gesperrte oder pausierte Verbraucher haben kein Betriebsrecht.
    Ohne laufenden Verbraucher entscheidet allein das Zusatzbudget über einen Start."""
    rechte = dict.fromkeys(cp.CONSUMERS, False)
    for anteil in budget.verbraucher:
        v = anteil.verbraucher
        name = _art(v)
        if name is not None and v.laeuft and not v.gesperrt:
            rechte[name] = True
    return rechte


def reservierungen(phasen):
    """Laufende Phasenumschaltungen reservieren ihren Bedarf in derselben Zuteilung (kein zweiter Abzug).

    Quelle ist der vor der Entscheidung gebundene Phasenvertrag des Produkts (Frische, Frist, Pause, Fahrzeug voll)."""
    w = (phasen or {}).get("reserviert_w")
    return dict(wallbox=int(w)) if (phasen or {}).get("aktiv") is True and type(w) in (int, float) and w > 0 else None


# Schlanker Lease-Zustand. Ein Startangebot an einen stehenden Verbraucher bucht seinen
# Anteil, bis die Messung den Start zeigt (Annahme), die Startgeneration wechselt oder die Frist abläuft; danach wartet
# er eine Wartezeit, und sein Anteil geht an die übrigen. Fristen laufen auf der gemeinsamen monotonen Wachuhr.
LEASE_ANGEBOT, LEASE_WARTEN = "angebot", "warten"


def _zahl_oder(wert, std):
    try:
        zahl = float(wert)
    except (TypeError, ValueError):
        return std
    return zahl if math.isfinite(zahl) else std


def lease_regeln(cfg):
    """Fristen aus derselben Konfiguration wie der Produkt-Zuteiler: WP nach Startsemantik, Box und Heizstab kurz."""
    kurz = min(60.0, max(5.0, _zahl_oder(cfg.get("consumer_start_lease_duration_s"), 25.0)))
    warte = min(120.0, max(1.0, _zahl_oder(cfg.get("consumer_start_lease_retry_s"), 15.0)))
    return dict(heatpump=heatpump_start_reservation_duration_s(cfg), wallbox=kurz, heater=kurz), warte


def _uhr(dauer_s, probe):
    return control_time.begin_guard(dauer_s, probe, minimum_s=0.0, epoch_mode=control_time.EPOCH_MODE_SAME_BOOT_MONOTONIC)


def _gruppe(budget, name):
    return [a.verbraucher for a in budget.verbraucher if _art(a.verbraucher) == name]


def leases_pruefen(budget, vorher, probe, generation, warte_s):
    """Offene Angebote gegen Messung, Generation und Frist. Rückgabe (gebucht {Name: W}, wartend {Name}, Zustand)."""
    gebucht, wartend, neu = {}, set(), {}
    for name, lease in (vorher if isinstance(vorher, dict) else {}).items():
        if name not in cp.CONSUMERS or not isinstance(lease, dict):
            continue
        uhr = control_time.evaluate_guard(lease.get("uhr"), probe, minimum_s=0.0)
        gruppe = _gruppe(budget, name)
        if lease.get("art") == LEASE_WARTEN:
            # Nur ein stehender Verbraucher wartet; startet er doch noch, trägt ihn die Messung (späte Annahme).
            if uhr["active"] and not any(v.laeuft for v in gruppe):
                wartend.add(name)
                neu[name] = dict(art=LEASE_WARTEN, uhr=uhr)
            continue
        if any(v.laeuft for v in gruppe):
            continue                       # Annahme: die Messung zeigt den Start, die Last trägt der Messpunkt.
        if not any((v.anfrage_w or 0) > 0 and not v.gesperrt for v in gruppe):
            continue                       # Bedarf zurückgezogen oder Nutzer-Aus: kein Angebot mehr.
        if name == "wallbox" and lease.get("generation") != generation:
            continue                       # Neue Startgeneration: ein altes Angebot erlaubt keinen Start.
        if uhr["active"]:
            gebucht[name] = int(lease.get("w") or 0)
            neu[name] = dict(lease, uhr=uhr)
        else:
            wartend.add(name)              # Frist ohne Annahme: Anteil an die übrigen, Wartezeit vor dem nächsten.
            neu[name] = dict(art=LEASE_WARTEN, uhr=_uhr(warte_s, probe))
    return gebucht, wartend, neu


def leases_anbieten(budget, zuteilung, leases, probe, generation, dauern):
    """Neues Angebot an stehende Verbraucher mit zugeteiltem Anteil; laufende tragen sich über die Messung."""
    neu = dict(leases)
    zuteilungen = zuteilung["commands"]["allocations"] if zuteilung["valid"] else {}
    for name in cp.CONSUMERS:
        w = int(zuteilungen.get(name) or 0)
        if name in neu or w <= 0 or any(v.laeuft for v in _gruppe(budget, name)):
            continue
        neu[name] = dict(art=LEASE_ANGEBOT, w=w, generation=generation if name == "wallbox" else None,
            uhr=_uhr(dauern[name], probe))
    return neu


def startfinanzierung(budget, zuteilung, leases, wartend):
    """WP-Startfinanzierung im gemeinsamen Vertragsformat aus der eigenen Zuteilung und dem eigenen Lease."""
    gruppe = _gruppe(budget, "heatpump")
    erforderlich = max([cp.CONSUMER_MIN_W["heatpump"]] + [int(v.mindest_w) for v in gruppe])
    gueltig = zuteilung["valid"]
    gebucht = int((zuteilung["accounting"].get("allocations") or {}).get("heatpump") or 0) if gueltig else 0
    befohlen = int(zuteilung["commands"]["allocations"].get("heatpump") or 0) if gueltig else 0
    lease = leases.get("heatpump") or {}
    if any(v.laeuft for v in gruppe):
        zustand, grund = "running", "heatpump_running_measured"
    elif "heatpump" in wartend:
        zustand, grund = "retry_wait", "start_lease_expired_unaccepted"
    elif lease.get("art") == LEASE_ANGEBOT:
        zustand, grund = "start_offered", "start_lease_open"
    else:
        zustand, grund = "idle", "no_start_funding" if befohlen < erforderlich else "start_funded"
    return cp.build_heatpump_start_funding_contract(required_start_w=erforderlich, accounting_allocated_w=gebucht,
        command_authorized_w=befohlen, state=zustand, reason_code=grund)


def projektion(budget, zuteilung, eingang, jetzt, *, haltezone=None, intent=None, leases=None, wartend=(),
               phasen=None):
    """Erzeugt einen eigenständigen Draht-Snapshot ohne Datei- oder MQTT-Schreibweg; kein Produktfeld wird
    übernommen (die frühere Durchreichung entfällt). Betriebsrecht, Startangebote, WP-Startfinanzierung und
    Phasenreservierung stammen aus der eigenen Zuteilung."""
    haltezone = budget.haltezone if haltezone is None else haltezone
    contract = zuteilung["contract"]
    valid = zuteilung["valid"]
    allocations = dict(zuteilung["commands"]["allocations"]) if valid else dict.fromkeys(cp.CONSUMERS, 0)
    accounting = dict(zuteilung["accounting"].get("allocations") or dict.fromkeys(cp.CONSUMERS, 0))
    wb = allocations["wallbox"]
    sources = (contract.get("source_allocations") or {}).get("wallbox") or dict(pv_w=0, battery_w=0)
    leases = leases or {}
    result = dict(consumer_operating_rights=betriebsrechte(budget, jetzt),
        consumer_start_leases={name: dict(art=lease.get("art"), w=lease.get("w"),
            remaining_s=round(float((lease.get("uhr") or {}).get("remaining_s") or 0.0), 1))
            for name, lease in sorted(leases.items())},
        heatpump_start_funding=startfinanzierung(budget, zuteilung, leases, wartend),
        wallbox_phase_transition_active=bool((phasen or {}).get("aktiv") is True),
        # Gebuchte Phasenreservierung: höchstens die eigene Anfrage der Box (kein zweiter Lastabzug).
        wallbox_phase_transition_reserved_w=int((contract.get("reservations_w") or {}).get("wallbox") or 0)
            if (phasen or {}).get("aktiv") is True else 0)
    result.update(schema_version="wb_pv_budget_control_v2", ts=int(jetzt),
        data_valid=valid, live_sample_invalid=not valid, budget_w=wb,
        raw_iAVal_w=wb, iAVal_w=wb,
        budget_amp_1ph=max(6, min(32, int(wb/230))) if wb >= 6*230 else 0,
        budget_amp_3ph=max(6, min(32, int(wb/690))) if wb >= 6*690 else 0,
        budget_pv_w=sources["pv_w"] if valid else None,
        budget_battery_w=sources["battery_w"] if valid else None,
        budget_pv_valid=valid, budget_pv_source="consumer_source_booking",
        consumer_allocations=allocations, consumer_accounting_allocations=accounting,
        consumer_budget_contract=contract, consumer_budget_contract_valid=valid,
        consumer_total_budget_w=contract["total_budget_w"], consumer_requests_w=contract["requests_w"],
        storage_charge_request_w=budget.akkuanspruch_w,
        wallbox_wbminsoc_hold_zone=haltezone,
        energy_score=dict(free_for_limbs_w=wb, free_for_consumers_raw_w=budget.rahmen_w,
            bat_charge_request_w=budget.akkuanspruch_w, pv_surplus_w=budget.pv_w.wert_w,
            consumer_allocations=allocations, consumer_accounting_allocations=accounting), state="speicherkern_schatten")
    boxen = [a for a in budget.verbraucher if _art(a.verbraucher) == "wallbox"]
    if haltezone and haltezone.get("active"):
        result.update(battery_support_authorized=False, battery_support_reason="wbminsoc_hold_zone")
    elif any(a.verbraucher.netzladen_gewollt for a in budget.verbraucher):
        result.update(battery_support_authorized=False,
            battery_support_reason="price_plan_storage_protection"
            if (intent or {}).get("price_plan_storage_protect") or
                (intent or {}).get("reason") == "price_plan_storage_protection"
            else "slot_grid_storage_protection")
    elif boxen and not any((a.battery_w.wert_w or 0) > 0 for a in boxen):
        result.update(battery_support_authorized=False,
            battery_support_reason=boxen[0].battery_w.grund.split(":", 1)[-1])
    if sources["battery_w"] > 0 and any(a.verbraucher.klasse == "Pre-Dump"
            and (a.battery_w.wert_w or 0) > 0 for a in boxen):
        result.update(battery_support_authorized=True, battery_support_reason="Pre-Dump")
    vorab = vorab_projektion(budget, eingang, jetzt)
    result.update(predump_consumer_plan=vorab, predump_active=vorab["active"],
        predump_allow=vorab["allow"],
        predump_allow_wallbox=vorab["allow"]["wallbox"],
        predump_allow_heatpump=vorab["allow"]["heatpump"],
        predump_allow_heater=vorab["allow"]["heater"],
        predump_target_soc=vorab["target_soc"], predump_floor_soc=vorab["floor_soc"],
        predump_bev_block_w=max((a.verbraucher.mindest_w for a in budget.verbraucher
            if a.verbraucher.art.startswith("wallbox_") and a.verbraucher.vorab_angemeldet), default=0)
            if vorab["active"] else 0)
    if vorab["active"]:
        result["state"] = vorab["state"]
    elif str(result["state"]).startswith(("pre_discharge", "predump")):
        result["state"] = "idle"
    identity = dict(schema_version="storage_consumer_budget_identity_v1", decision_ts=int(jetzt),
        decision_owner="speicherkern_schatten", decision_effect="hypothetical_consumer_power_budget",
        decision_state=result["state"], decision_mode=0, decision_value_w=0, decision_protected=False,
        authorized_wallbox_budget_w=wb, authorized_heatpump_budget_w=allocations["heatpump"],
        authorized_heater_budget_w=allocations["heater"], binding_status="not_applicable",
        plan_id="not_applicable", slot_id="not_applicable", action_id="not_applicable")
    identity["decision_generation"] = revision_hash(identity)
    result["producer_identity"] = identity
    result = json.loads(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False))
    result["budget_revision"] = revision_hash(result)
    return result


def vorab_projektion(budget, eingang, jetzt):
    """Bestehender Verbrauchervertrag als Wert, ohne Sender oder Quittung."""
    a = eingang.auftrag
    aktiv = (a is not None and a.modus == "VORAB" and a.beginn_s <= jetzt < a.ende_s
        and budget.vorab_rate_w is not None and budget.pv_w.gueltig)
    allow = {name: bool(aktiv and any(_art(x.verbraucher) == name
        and x.verbraucher.vorab_angemeldet and not x.verbraucher.gesperrt
        for x in budget.verbraucher)) for name in cp.CONSUMERS}
    last = sum(x.verbraucher.leistung.zahl(jetzt, 0, 1000000) or 0
        for x in budget.verbraucher if allow.get(_art(x.verbraucher))
        and x.verbraucher.vorab_angemeldet) if aktiv else 0
    return dict(enabled=bool(aktiv), active=bool(aktiv), ts=int(jetzt), expires_ts=int(jetzt)+20,
        allow=allow, budget_w=(budget.pv_w.wert_w + budget.battery_w.wert_w) if aktiv else 0,
        discharge_w=budget.vorab_rate_w if aktiv else 0, no_grid=True, grid_fallback=False,
        target_soc=a.ziel_pct if aktiv else None, floor_soc=budget.vorab_boden_pct if aktiv else None,
        state=("pre_discharge_consumer_auto" if last >= 250 else "pre_discharge_wait") if aktiv else "idle",
        reason="Pre-Dump" if aktiv else "Pre-Dump inaktiv",
        abweichungen=["A0-S5-"+str(i) for i in (1, 2, 3, 5, 6, 7, 8)] if aktiv else [])


def status_projektion(projektion):
    return json.loads(json.dumps(dict(state=projektion["state"], mode=0, val=0,
        auto_limit=projektion.get("auto_limit"), budget=projektion,
        energy_score=projektion["energy_score"],
        authorized_heatpump_budget_w=projektion.get("authorized_heatpump_budget_w")), allow_nan=False))


def ha_projektion(projektion):
    # Die reine Blattfunktion wird erst für die optionale HA-Ansicht geladen.
    try:
        from Installer.e3dc_mqtt_hub import mqtt_budget_state
    except ModuleNotFoundError:
        from e3dc_mqtt_hub import mqtt_budget_state
    return mqtt_budget_state({}, projektion)
