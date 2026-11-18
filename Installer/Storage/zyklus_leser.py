"""Lesereihenfolge des Speicherdienstes mit explizit übergebenen I/O-Lesern.

Die Funktionen besitzen keinen eigenen Datei- oder Gerätezugriff. Der Dienst
übergibt seine bisherigen Leser; der Schattenadapter verwendet nur die Werte.
Frühe Abbruchzweige bleiben zwischen den drei Leseabschnitten in main().
"""

from . import backup_hold
from .ladepunkt_leser import live_with_managed_vehicle_phases
try:
    from Installer.pv_forecast_topology import external_ac_source_configured
except ModuleNotFoundError:
    from pv_forecast_topology import external_ac_source_configured


def _an(cfg, key):
    return str(cfg.get(key, '')).strip().lower() in ('1', 'true', 'yes', 'on')


def _adresse(cfg, key):
    return str(cfg.get(key, '')).strip() not in ('', '0.0.0.0')


def regelbare_lasten(cfg, live):
    """Regelbare Verbraucherlast je Quelle aus den Erzeugerfeldern des Dienstes (K4).

    Wärmepumpe: regelbar sind die nativen Treiber (Luxtronik, iDM, Stiebel, Dimplex; Erkennung
    wie im Dienst über `luxtronik`, `wp_type`, `idm_ip`, `luxtronik_ip`) und der Shelly-3EM-Aktor
    mit Befehlsfreigabe (`Heatpump_Provider_Command_Eligible`); der E3DC-Leistungsmesser
    (`wp_type` 6) misst nur und zählt als harte Last. Leistung nur aus dem Vertrag
    `Heatpump_Power_Known`/`WP_Power`. Heizstab: `Heater_Power_Known`/`Heizstab_Power`.
    Klima (`climate_enable`): die Leistung liegt im Speicherdienst nicht vor; sie bleibt
    unbekannt (None), damit K4 nicht lockert. Unbekannt heißt nie 0.
    """
    live = live or {}
    wp_type = str(cfg.get('wp_type', '')).strip()
    lasten = {}
    if wp_type == '3':
        wp_regelbar = _adresse(cfg, 'shelly_3em_ip') and live.get('Heatpump_Provider_Command_Eligible') is True
    elif wp_type == '6':
        wp_regelbar = False
    else:
        wp_regelbar = (_an(cfg, 'luxtronik') or wp_type in ('0', '1')
                       or _adresse(cfg, 'idm_ip') or _adresse(cfg, 'luxtronik_ip'))
    if wp_regelbar:
        lasten['wp'] = (backup_hold.number(live.get('WP_Power'))
                        if live.get('Heatpump_Power_Known') is True else None)
    if _an(cfg, 'heizstab') or _adresse(cfg, 'heizstab_ip') or _adresse(cfg, 'shelly_heiz_ip'):
        lasten['heizstab'] = (backup_hold.number(live.get('Heizstab_Power'))
                              if live.get('Heater_Power_Known') is True else None)
    if _an(cfg, 'climate_enable'):
        lasten['klima'] = None
    summe = sum(lasten.values()) if lasten and None not in lasten.values() else (0.0 if not lasten else None)
    return summe, lasten


def vorhalte_aufnahme(cfg, plan, start, previous_state=None, owner_binding=None, live=None, markt_leser=None):
    """Nicht geheimer, vor der Entscheidung aufgelöster Tarif-/Quellensatz.

    Ohne angeforderte Vorhaltung (Modus ungleich 2) werden nur die billigen
    Konfigurations- und Planflags gelesen; Tarif, Konto, Lasten und Markt entfallen.
    """
    contract = backup_hold.config_contract(cfg)
    flags = (plan.get('direct_marketing') or {}).get('flags') or {}
    hard_owner = bool(owner_binding(plan, start)) if owner_binding else (
        None if plan.get('direct_marketing') else False)
    aufnahme = dict(contract=contract, markt=None, regelbare_last_w=None, regelbare_lasten={},
        preise=dict(reason="backup_disabled", prices=[]), startkonto={},
        external_ac_known=external_ac_source_configured(cfg), hard_owner=hard_owner,
        dc_only=(_an(cfg, 'storage_dc_first_charge_limit_enable')
            or (_an(cfg, 'direct_marketing_enable')
                and (flags.get('pv_store_dc_only_enable') is True
                    or _an(cfg, 'direct_marketing_pv_store_dc_only_enable')))),
        grid_charge_enable=flags.get('grid_charge_enable') is True)
    if contract["mode"] != 2:
        return aufnahme
    live = live or {}
    previous_state = previous_state or {}
    execution = backup_hold.read_execution(plan, cfg, start * 1000, contract=contract)
    deadline = execution['deadline_ts_ms'] if execution else start * 1000
    control = previous_state.get('backup_hold_control') or {}
    account = control.get('closed_account') or previous_state.get('backup_hold_closed_account') or {}
    regelbare_last, lasten = regelbare_lasten(cfg, live)
    aufnahme.update(markt=markt_leser(cfg, live, plan, start, previous_state) if markt_leser else None,
        regelbare_last_w=regelbare_last, regelbare_lasten=lasten,
        preise=(backup_hold.tarif_preise(cfg, plan, start * 1000, deadline)
            if contract["values"][backup_hold.KEYS[4]] else dict(reason="grid_charge_disabled", prices=[])),
        startkonto=dict(account))
    return aufnahme


def vorhalte_aufnahme_sicher(cfg, plan, start, previous_state=None, owner_binding=None, live=None,
                             markt_leser=None, log=None):
    """Die Schattenaufnahme stört den Dienst nie: Ausnahmen werden hier gefangen und als
    Schattenfehler weitergegeben (Feld `fehler`); der Kern weist den Zyklus dann als nicht
    abgedeckt aus. Kein Abbruch von main(), die Freigabe der Grenzen bleibt erreichbar."""
    try:
        return vorhalte_aufnahme(cfg, plan, start, previous_state, owner_binding, live, markt_leser)
    except Exception as exc:  # Schattenfehler, kein Dienstfehler
        if log is not None:
            log.warning("Vorhalte-Schattenaufnahme fehlgeschlagen: %s: %s", type(exc).__name__, exc)
        return dict(contract=dict(mode=None, valid=False, values={}, reasons=['backup_shadow_error']),
            fehler=(type(exc).__name__ + ": " + str(exc))[:200], markt=None, regelbare_last_w=None,
            regelbare_lasten={}, preise=dict(reason="backup_shadow_error", prices=[]), startkonto={},
            external_ac_known=None, hard_owner=None, dc_only=False, grid_charge_enable=False)


# Bestehende Verbrauchereinstellungen separat: G3-Quellen-/Reservebindung bleibt gleich.
VERBRAUCHER_CONFIG_FELDER = (
    "consumer_priority_order", "consumer_priority_wp_runon_s",
    "wb_soc_hysterese_pct", "wb_hysterese_pct", "wb_target_restart_above_wbminsoc_pct",
    "wp_type", "luxtronik", "heizstab", "wbminsoc",
)


def verbraucher_aufnahme(cfg, previous_state=None):
    return dict(cfg={key: cfg[key] for key in VERBRAUCHER_CONFIG_FELDER if key in cfg})


def lese_zyklus_konfiguration(cfg, *, read_storage_regulation_config,
                              storage_regulation_enabled, V4_CFG):
    current_cfg = read_storage_regulation_config(V4_CFG)
    requested_regulation = storage_regulation_enabled(current_cfg) if current_cfg is not None else None
    if current_cfg is not None:
        cfg = current_cfg
    return cfg, requested_regulation


def lese_zyklus_live(cfg, *, read_json_file, house_fuse_config,
                    resolve_house_fuse, system_house_fuse, LIVE_F):
    live = read_json_file(LIVE_F, max_age_s=None)
    cycle_cfg = house_fuse_config(cfg, resolve_house_fuse(cfg, system_house_fuse(live)))
    return live, cycle_cfg


def lese_zyklus_eingaenge(cycle_cfg, live, start, market_plan_grace_source_memory, regulation_switch, *,
    load_validated_canonical_plan_snapshot, read_json_file, augment_consumer_live,
    manual_override_max_age_s, manual_override_expired, safe_float,
    remove_manual, getmtime, log, PLAN_F, WB_INTENT_F, WB_NATIVE_F,
    MANUAL_OVERRIDE_F, ENERGY_DECISION_F, HEATER_STATUS_F, previous_state=None, owner_binding=None, markt_leser=None):
    # Pro Zyklus genau eine stabile, statisch validierte Planrevision.
    # Bei Replace, Hash- oder Generationsabweichung gibt es keinen Altplan-Fallback.
    plan_read_diagnostic: Dict[str, Any] = {}
    plan = load_validated_canonical_plan_snapshot(PLAN_F, max_age_s=1800, diagnostic=plan_read_diagnostic) or {}
    cycle_cfg = dict(cycle_cfg, _market_plan_read_reason=plan_read_diagnostic.get("reason"),
                     _market_plan_grace_source=market_plan_grace_source_memory)
    wb_intent = read_json_file(WB_INTENT_F, max_age_s=90)
    wb_native = read_json_file(WB_NATIVE_F, max_age_s=90)
    manual_max_age_s = manual_override_max_age_s(cycle_cfg)
    manual = read_json_file(MANUAL_OVERRIDE_F)
    if manual and not regulation_switch.manual_allowed(
        manual, safe_float(cycle_cfg.get("storage_regulation_changed_ts"), 0),
    ):
        manual = {}
    if manual_override_expired(manual, start, manual_max_age_s):
        log.info("Manueller Batterie-Override abgelaufen; Automatik übernimmt.")
        try:
            remove_manual(MANUAL_OVERRIDE_F)
        except FileNotFoundError:
            pass
        except Exception as exc:
            log.debug("Abgelaufener Batterie-Override konnte nicht entfernt werden: %s", exc)
        manual = {}
    energy_decision = read_json_file(ENERGY_DECISION_F, max_age_s=45)
    heater_status = read_json_file(HEATER_STATUS_F, max_age_s=45)
    if heater_status:
        try:
            heater_status["_source_mtime_s"] = getmtime(
                HEATER_STATUS_F
            )
        except OSError:
            heater_status = {}
    live = augment_consumer_live(
        live,
        energy_decision,
        cycle_cfg,
        heater_status,
    )
    live = live_with_managed_vehicle_phases(live, wb_intent, start)
    # Schattenaufnahme außerhalb des schützenden try um decide_next_cycle: eigener Fehlerfang.
    cycle_cfg = dict(cycle_cfg, _kern_vorhalt=vorhalte_aufnahme_sicher(
        cycle_cfg, plan, start, previous_state, owner_binding, live, markt_leser, log=log))
    cycle_cfg = dict(cycle_cfg, _kern_verbraucher=verbraucher_aufnahme(cycle_cfg, previous_state))
    # Vor der Entscheidung gelesen; der Schatten öffnet keine zweite Datei.
    from .kern_uebergabe import waerme_aufnahme
    cycle_cfg = dict(cycle_cfg, _kern_energy_decision=waerme_aufnahme(energy_decision))
    return cycle_cfg, live, plan, plan_read_diagnostic, wb_intent, wb_native, manual, energy_decision
