"""Quellenbuchung und hypothetische Verbraucherprojektion, ausschließlich im Speicher."""
import json
try:
    from Installer import consumer_priority as cp
    from Installer.storage_dispatch_contract import revision_hash
    from Installer.Storage.kern import waerme_hauslast
except ModuleNotFoundError:
    import consumer_priority as cp
    from storage_dispatch_contract import revision_hash
    from Storage.kern import waerme_hauslast


UEBERNOMMENE_FELDER = (
    "heatpump_pv_contract", "heatpump_start_funding", "authorized_heatpump_budget_w",
    "heatpump_pause_request", "wallbox_phase_transition", "wallbox_phase_transition_grants",
    "wallbox_phase_transition_reserved_w", "wallbox_start_hold_active", "wallbox_start_hold_w",
    "wallbox_start_hold_until_ts",
    "peak_shaving", "auto_limit", "force_wallbox_stop",
)


def _art(v):
    return "wallbox" if v.art.startswith("wallbox_") else {
        "waermepumpe": "heatpump", "heizstab": "heater"}.get(v.art)


def zuteilen(budget, *, order=None, now_s=0.0, previous_contract=None,
             wp_runon_s=600.0, reservations_w=None, priority_changed_at_s=-999999.0):
    """Ein bestehender Zuteiler, zwei Quellen innerhalb seiner Verbrauchsbuchung."""
    previous = previous_contract or {}
    requests = dict.fromkeys(cp.CONSUMERS, 0)
    minimums = dict(cp.CONSUMER_MIN_W)
    enabled = dict.fromkeys(cp.CONSUMERS, False)
    active = dict(enabled)
    caps = dict(requests)
    for anteil in budget.verbraucher:
        v = anteil.verbraucher
        name = _art(v)
        if name is None:
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
    if budget.haltezone and budget.haltezone["active"]:
        requests["wallbox"] = min(requests["wallbox"], budget.haltezone["frame_w"])
    pv = max(0, int(budget.pv_w.wert_w or 0))
    total = max(0, int(budget.rahmen_w or 0))
    pv = min(pv, total)
    contract = cp.allocate_consumer_budget_contract(total, requests, order,
        enabled=enabled, evidence_valid=budget.pv_w.gueltig and budget.rahmen_w is not None,
        minimums_w=minimums, maximums_w={name: max(requests[name], minimums[name]) for name in cp.CONSUMERS},
        active=active, reservations_w=reservations_w,
        previous_active=previous.get("active"), priority_changed_at_s=priority_changed_at_s,
        now_s=now_s, wp_runon_s=wp_runon_s, pv_pool_w=pv,
        battery_pool_w=total-pv, battery_caps_w=caps)
    contract.update(command_eligible=dict(enabled),
        command_allocations=dict(contract["allocations"]),
        accounting_allocations=dict(contract["allocations"]))
    accounting = cp.validate_consumer_budget_contract(contract)
    commands = cp.validate_consumer_command_allocations(contract)
    return dict(contract=contract, accounting=accounting, commands=commands,
        valid=accounting["valid"] and commands["valid"])


def projektion(budget, zuteilung, eingang, jetzt, *, produkt=None, haltezone=None, intent=None):
    """Erzeugt einen eigenständigen Draht-Snapshot ohne Datei- oder MQTT-Schreibweg."""
    product = produkt or {}
    haltezone = budget.haltezone if haltezone is None else haltezone
    contract = zuteilung["contract"]
    valid = zuteilung["valid"]
    allocations = dict(zuteilung["commands"]["allocations"]) if valid else dict.fromkeys(cp.CONSUMERS, 0)
    accounting = dict(zuteilung["accounting"].get("allocations") or dict.fromkeys(cp.CONSUMERS, 0))
    wb = allocations["wallbox"]
    sources = (contract.get("source_allocations") or {}).get("wallbox") or dict(pv_w=0, battery_w=0)
    result = {key: product[key] for key in UEBERNOMMENE_FELDER if key in product}
    result["uebernommen_aus_produkt"] = sorted(result)
    result["nicht_abgedeckt"] = ["G4b", "S5b", "G5", "G6"]
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
            consumer_allocations=allocations, consumer_accounting_allocations=accounting), state=product.get("state", "speicherkern_schatten"))
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
