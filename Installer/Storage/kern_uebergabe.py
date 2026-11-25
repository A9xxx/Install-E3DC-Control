"""Unveränderliche, schlüsselbeschränkte Eingangs- und Ergebnissätze für G1.

Kein Dienstobjekt wird gehalten. Ein- und Ausgang werden zu zwei verschiedenen
Zeitpunkten gebunden; die Ausgangsbindung ersetzt niemals die Eingangsaufnahme.
"""
from dataclasses import dataclass
from .zyklus_leser import vorhalte_aufnahme_sicher, verbraucher_aufnahme
from .quellenrahmen import DRUCK_CONFIG_FELDER


CONFIG_FELDER = (
    "storage_curve_sliding_horizon_enable",
    "maximumladeleistung", "maximaleentladeleistung", "storage_live_stale_guard_s",
    "ep_reserve_pct", "speichergroesse", "grid_max_amps", "grid_max_reserve_amp",
    "storage_manual_override_max_age_h", "storage_regulation_changed_ts",
    "tl_lookahead_h",
    "storage_parallel_curve_tolerance_pct", "storage_curve_target_mode",
    "wb_native_enable", "wb_native_type", "wb_native_type2", "wb1_mode", "wb2_mode",
    "wb1_observe_storage_policy", "wb2_observe_storage_policy",
    "wb1_storage_observe_policy", "wb2_storage_observe_policy",
    "predump_enable", "hard_predump_enable", "storm_guard_mode",
    "storage_predump_min_soc", "eco_dump_min_soc", "pd_max_hours",
    "predump_wallbox_enable", "predump_heatpump_enable", "predump_heater_enable",
    "hard_predump_target_soc", "hard_predump_grid_enable", "hard_predump_grid_max_w",
    "price_boost_enable", "wp_pv_max_power_w", "stromtarif_typ",
    "strompreis_cheap", "strompreis_basis", "strompreis_uht", "heat_tariff_shift_windows",
    "heat_tariff_shift_mode", "peak_shaving_enable", "direct_marketing_enable",
    "storage_backup_hold_enable", "cheap_grid_battery_enable", "awattar_mode",
    "market_battery_max_w", "cheap_grid_battery_max_w",
    "storage_backup_energy_kwh", "storage_backup_reference_time", "storage_backup_until_time",
    "storage_backup_grid_charge_enable", "storage_backup_grid_price_limit_ct",
) + DRUCK_CONFIG_FELDER  # Druck- und Quellenschlüssel, gleicher Eingang.
# Keine Kennungen oder Zugangsdaten aus beliebigen Zusatzfeldern übernehmen.
_VERBOTEN = frozenset(("password", "pass", "pin", "token", "aes_key", "e3dc_user",
    "mqtt_hub_user", "wb_user", "wb2_user", "stiebel_isg_web_user", "car_lat",
    "car_lon", "odometer", "vehicle_id", "vin", "car_id", "profile_id"))


INTENT_FELDER = (
    "ts", "battery_request", "reason", "wb_mode_active", "wb_control_mode", "wbminsoc",
    "effective_wb_floor_soc", "wbminsoc_gate_open", "wb_power_w", "house_baseline_w",
    "scheduled_slot_active", "price_plan_storage_protect", "price_opt_active", "price_boost_active",
    "battery_departure_active", "battery_departure_blocked", "predump_wallbox_active",
    "manual_pause", "bev_full_blocked", "battery_support_authorized", "battery_support_reason",
    "curve_support_class", "curve_support_relation", "curve_floor_support", "pv_only_authorized_w",
    "pv_only_allowed_w", "wbminsoc_discharge_pull_active", "wbminsoc_discharge_pull_limit_w",
    "wbminsoc_discharge_taper_active", "wbminsoc_discharge_taper_limit_w", "wb_soc_hysterese_pct",
    "wbminsoc_runtime_raise_active", "phase_transition_active", "phase_transition_reserved_w",
    "phase_transition_reservation", "start_requested", "start_request_authorized", "start_generation",
    "start_request_contract_version", "connected", "plugged", "active", "charging_active",
    "external_wallbox_manager", "wallbox_storage_points", "car_energy_needs",
    "openwb_primary_pv_mode_ids", "openwb_primary_pv_mode_active", "openwb_primary_observe_only",
    "observe_storage_policy", "phase_transition_reservations",
)


@dataclass(frozen=True)
class Felder:
    werte: tuple


def waerme_aufnahme(record):
    """Nur die fachlichen Wärmefelder des bereits gelesenen Datensatzes binden."""
    record = record if isinstance(record, dict) else {}
    decision = record.get("decision") if isinstance(record.get("decision"), dict) else {}
    request = decision.get("heat_tariff_shift") if isinstance(decision.get("heat_tariff_shift"), dict) else {}
    policy = decision.get("heat_policy") if isinstance(decision.get("heat_policy"), dict) else {}
    window = request.get("window") if isinstance(request.get("window"), dict) else {}
    return dict(ts=record.get("ts"), decision={
        **{key: decision.get(key) for key in ("price_action", "price_boost_active",
            "market_plan_heatpump_active", "heatpump_boost_owner", "predump_heatpump_active")},
        "heat_tariff_shift": {**{key: request.get(key) for key in (
            "mode", "ts", "active", "would_start", "intent_revision", "valid_until_ts")},
            "window": {key: window.get(key) for key in ("revision", "start_ts", "end_ts")}},
        "heat_policy": {key: policy.get(key) for key in ("owner", "target_state", "runtime_enabled")}})


# Positivlisten der reinen Leser. Signierte Beobachtungen bleiben als Vertrag vollständig.
LIVE_FELDER = (
    'Battery', 'Battery_SoC', 'Ext_PV_Power', 'Ext_PV_Power_Age_S', 'Ext_PV_Power_Source', 'Ext_PV_Power_Valid',
    'Grid', 'Grid_PM_Delta', 'Heatpump_Compressor_Observation_Valid', 'Heatpump_Compressor_Running', 'Heatpump_Evidence_TS',
    'Heatpump_PV_Contract', 'Heatpump_Pause_Active', 'Home', 'Home_Includes_WP', 'Home_Power_Balance', 'Home_Power_Delta',
    'Home_Power_Independent', 'Home_Power_Raw', 'Home_Power_Source', 'Notstrom_Status', 'PV', 'Power_Decision_Usable',
    'Power_Plausibility', 'RSCP_Glitch_Reasons', 'RSCP_Sample_Valid', 'WB', 'WP_Power', 'Wallbox_Live_Power',
    'Heatpump_Power', 'Heatpump_Power_Known', 'Heizstab_Power', 'Heater_Power_Known',
    'Heatpump_Start_Request_W', 'Heater_Start_Request_W', 'Heater_Evidence_TS', 'Heatpump_Provider_Command_Eligible',
    'Wallbox_Power_Source', '_ts', 'bat_capacity_kwh', 'bat_charge_limit_w', 'bat_dcb_count', 'bat_discharge_limit_w',
    'bat_total_full_cap_kwh', 'bat_total_specified_kwh', 'bat_total_usable_kwh', 'battery_capacity_wh', 'derate_at_power_w', 'emergency_reserve_pct', 'ac_power_limit_w', 'wr_ac_limit_w', 'inverter_ac_limit_w',
    'ems_derating_active', 'ems_discharge_start_power_w', 'ems_emergency_power_status', 'ems_emergency_power_status_valid', 'ems_max_charge_power_w', 'ems_max_discharge_power_w',
    'ems_power_limits_active', 'ems_power_settings_read', 'ems_power_settings_valid', 'ep_reserve_energy_wh', 'ep_reserve_max_energy_wh', 'ep_reserve_observed_at_s',
    'ep_reserve_pct', 'ep_reserve_raw_pct', 'installed_capacity_wh', 'notstrom_reserve', 'power_limits_active', 'pv_derating_active',
    'real_usable_capacity_kwh', 'real_usable_capacity_wh', 'reserve_energy', 'reserve_energy_wh', 'reserve_max', 'reserve_max_energy',
    'reserve_max_energy_wh', 'reserve_percent', 'usable_capacity_kwh', 'usable_capacity_wh', 'user_charge_limit_w',
) + tuple(key+suffix for key in ('SOC', 'PV_Power', 'Grid_Power', 'Home_Power', 'Battery_Power', 'Wallbox_Power') for suffix in ("", "_Valid"))\
    + tuple(prefix+suffix for prefix in ("bat", "bat1", "bat2", "bat3") for suffix in ("_usable_kwh", "_full_cap_kwh", "_specified_kwh", "_v"))\
    + tuple("device_max_"+d+s for d in ("charge", "discharge") for s in ("_power_w", "_power_w_valid", "_power_w_source", "_power_w_observed_at_s"))
LIVE_FELDER += tuple("dc"+str(index)+"_max_w" for index in range(8))
LIVE_FELDER += ("ems_charge_locked", "ems_weather_charging", "weather_regulated_charge")
LIVE_FELDER += ("grid_p1", "grid_p2", "grid_p3", "grid_pm_available", "Grid_PM_Available",
    "pvi_ac_max_phase_count", "pvi_ac_max_phase_count_valid", "managed_vehicle_phases")
PLAN_FELDER = (
    '_vorab_revision', 'awattar_mode', 'adaptive_headroom_available_wh', 'adaptive_headroom_required_wh', 'adaptive_headroom_target_soc', 'backup_hold_observation', 'battery_capacity',
    'cheap_grid_charge', 'curtailment_pressure_wh', 'curtailment_unavoidable_wh', 'direct_marketing', 'evening_shortfall_wh', 'hard_predump_enabled',
    'headroom_reserve_active', 'headroom_reserve_pressure_wh', 'headroom_reserve_source', 'ladeende_ts', 'ladestart_ts', 'latest_charge_start_ts',
    'plan_id', 'predump_bev_start_buffer_s', 'predump_curve_active', 'predump_curve_soc', 'predump_dump_wh', 'predump_enabled',
    'predump_end_ts', 'predump_grid_fallback', 'predump_heatpump_active', 'predump_min_soc', 'predump_preventable_clipping_wh', 'predump_reason',
    'predump_request', 'predump_start_ts', 'predump_time_window_valid', 'predump_wallbox_active', 'revision', 'soc_min_curve', 'soc_ceiling_curve', 'storage_target_curve',
    'storm_grid_charge', 'storm_guard', 'target_curve_meta', 'target_timeline', 'timeline', 'timezone',
    'valid_until_ts_ms', 'weather_reserve_active',
)


def lese_aufnahme(wert, felder):
    return {key: wert[key] for key in felder if key in wert}


def plan_aufnahme(plan):
    neu = lese_aufnahme(plan, PLAN_FELDER)
    for key in ("target_timeline", "soc_min_curve", "soc_ceiling_curve", "storage_target_curve", "timeline"):
        if key in neu:
            neu[key] = [lese_aufnahme(row, ("ts", "grid_dump_w") if key == "timeline" else ("ts", "soc", "target_soc"))
                for row in neu[key] if isinstance(row, dict)]
    if isinstance(neu.get("backup_hold_observation"), dict):
        neu["backup_hold_observation"] = lese_aufnahme(neu["backup_hold_observation"], ("execution",))
    if isinstance(neu.get("target_curve_meta"), dict):
        neu["target_curve_meta"] = lese_aufnahme(neu["target_curve_meta"], PLAN_FELDER+("curve_end_ts",))
    return neu


def einfrieren(wert):
    if isinstance(wert, dict):
        return Felder(tuple((k, einfrieren(v)) for k, v in wert.items()
            if isinstance(k, (str, int)) and not (isinstance(k, str)
            and (k.lower() in _VERBOTEN or k.lower().endswith(("_pass", "_password", "_token", "_pin"))))))
    if isinstance(wert, (tuple, list)):
        return tuple(einfrieren(v) for v in wert)
    if type(wert) not in (str, float, int, bool, type(None)):
        raise TypeError("Übergabe erlaubt nur Datenwerte")
    return wert


def auftauen(wert):
    """Jeder Aufruf liefert eine eigene bearbeitbare Lesekopie."""
    if isinstance(wert, Felder):
        return {k: auftauen(v) for k, v in wert.werte}
    if isinstance(wert, tuple):
        return [auftauen(v) for v in wert]
    return wert


@dataclass(frozen=True)
class ZyklusEingaenge:
    start: float
    live: Felder
    plan: Felder
    wb_native: Felder
    wb_intent: Felder
    manual: Felder
    cfg: Felder
    regelung_an: bool | None
    rscp_fehler: bool | None
    zeitprobe: Felder
    plan_revision: str | None
    funktionsstatus: Felder
    house_fuse: Felder
    vorhalt: Felder
    verbraucher: Felder
    energy_decision: Felder = Felder(())
    dv: Felder = Felder(())
    vorab_netz: Felder = Felder(())
    platz: Felder = Felder(())
    phasen: Felder = Felder(())
    ausfall: Felder = Felder(())
    spitze: Felder = Felder(())
    last: Felder = Felder(())


@dataclass(frozen=True)
class Uebergabe:
    eingaenge: ZyklusEingaenge
    ergebnis: Felder
    rscp_request_transaction: Felder
    power_settings: Felder


DV_FELDER = ("aktiv", "aufnahmefehler", "ziel", "befehle", "fehler", "gate", "beginn_ms", "ende_ms",
    "aktion_id", "slot_id", "export_w", "lade_w", "headroom_halten", "phase5_feld", "kurvenformung",
    "reserve_boden_pct", "export_grenze_w", "netzpunkt", "wallbox_laedt", "pv_laden")
VORAB_NETZ_FELDER = ("aktiv", "aufnahmefehler", "erlaubt", "faellig", "frist_nah", "rate_w", "verfuegbar", "last_w",
    "schwelle_w", "warte_s", "export_grenze_w", "hard_grenze_w", "schritt_w", "bev_fenster", "bev_faellig", "waechter",
    "rampe_w")
LAST_FELDER = ("aktiv", "aufnahmefehler", "klasse", "kappe_w", "erwartet_w", "erkannt_w")
SPITZE_FELDER = ("aktiv", "aufnahmefehler", "klasse", "ziel_w", "lade_w", "puffer_pct", "ende_s",
    "kontext")
AUSFALL_FELDER = ("active", "points", "vehicle_present", "aufnahmefehler")
PHASEN_FELDER = ("aktiv", "aufnahmefehler", "reserviert_w", "ziel_phasen", "frist_s")
PLATZ_FELDER = ("aktiv", "aufnahmefehler", "konto", "beginn_s", "frist_s", "boden_pct", "rest_wh", "min_w", "max_w",
    "min_pv_w", "luecke_s", "export_marge_w", "importwaechter_w", "export_ziel_w", "tagesgrenze_wh", "regel")


def eingaenge_binden(*, live, plan, wb_native, wb_intent, manual, cycle_cfg,
                     start, zeitprobe, regelung_an, rscp_fehler, funktionsstatus, house_fuse=None, energy_decision=None,
                     dv=None, vorab_netz=None, platz=None, phasen=None, spitze=None, ausfall=None, last=None):
    # Ein Plan, vor der Entscheidung eingefroren; der Kernmodus ergänzt
    # den Wirksamkeitsstatus erst in der hypothetischen Projektion.
    storm = plan.get("storm_grid_charge") or {}
    lo, hi = storm.get("window_start"), storm.get("window_end")
    im_fenster = (type(lo) in (int, float) and type(hi) in (int, float)
        and lo / 1000 <= start < hi / 1000)
    funktionsstatus = dict(funktionsstatus, **{"Unwetter": dict(
        konfiguriert=str(cycle_cfg.get("storm_guard_mode", "warn")).strip().lower() not in ("off", "aus", "0"),
        angefordert=bool(storm.get("active") is True and im_fenster
            or (plan.get("storm_guard") or {}).get("control_active") is True),
        wirksam=None)})
    for row in funktionsstatus.values():
        if not isinstance(row, dict) or any(row.get(key) is not None and type(row.get(key)) is not bool
                for key in ("konfiguriert", "angefordert", "wirksam")):
            raise TypeError("Funktionsstatus benötigt boolesche Werte oder None")
    cfg = {k: cycle_cfg[k] for k in CONFIG_FELDER if k in cycle_cfg}
    if any(type(v) not in (str, int, float, bool, type(None)) for v in cfg.values()):
        raise TypeError("Konfigurationsgrößen müssen skalar sein")
    if regelung_an is not None and type(regelung_an) is not bool:
        raise TypeError("Regelung benötigt True, False oder None")
    if rscp_fehler is not None and type(rscp_fehler) is not bool:
        raise TypeError("RSCP-Fehlerstatus benötigt True, False oder None")
    revision = getattr(plan, "source_sha256", None) or plan.get("plan_id") or plan.get("revision")
    intent = {key: wb_intent[key] for key in INTENT_FELDER if key in wb_intent}
    # Nur die vom Ladepunktleser benötigten Betriebsfelder, keine Fahrzeugkennungen.
    for key, felder in (("car_energy_needs", ("wb_id", "connected", "car_power_w",
            "car_power_valid", "car_charging_last_valid_ts")),
            ("phase_transition_reservations", ("wb_id", "active", "requested_w"))):
        if key in intent:
            intent[key] = [{feld: row[feld] for feld in felder if feld in row}
                for row in (intent[key] or []) if isinstance(row, dict)]
    return ZyklusEingaenge(start, einfrieren(lese_aufnahme(live, LIVE_FELDER)),
        einfrieren(plan_aufnahme(plan)), einfrieren(lese_aufnahme(wb_native, (
            "ts", "total_power_w", "total_power_valid", "wb_details", "charging_active", "observation", "wb_possible_power_w",
            "driver_status_valid", "driver_status_stale", "driver_status_degraded", "driver_status_glitch",
            "driver_status_plausible", "driver_status_last_sample_ts"))),
        einfrieren(intent),
        einfrieren({key: manual[key] for key in ("mode", "ts", "created_ts", "expires_ts", "target_soc") if key in manual}),
        einfrieren(cfg), regelung_an,
        rscp_fehler, einfrieren(zeitprobe), str(revision) if revision is not None else None,
        einfrieren({name: {key: row.get(key) for key in ("konfiguriert", "angefordert", "wirksam")}
            for name, row in funktionsstatus.items()
            if name in ("Pre-Dump", "Unwetter", "Wärme-Tarif", "Lastspitzen", "Direktvermarktung", "Preis-Boost", "Vorhaltung")
            and isinstance(row, dict)}),
        einfrieren({key: (house_fuse or {}).get(key) for key in ("effective", "source")}),
        einfrieren(cycle_cfg.get("_kern_vorhalt") or vorhalte_aufnahme_sicher(cycle_cfg, plan, start, live=live)),
        einfrieren(cycle_cfg.get("_kern_verbraucher") or verbraucher_aufnahme(cycle_cfg)),
        energy_decision=einfrieren(waerme_aufnahme(energy_decision if energy_decision is not None
            else cycle_cfg.get("_kern_energy_decision"))),
        # Direktvermarktung vor der Entscheidung; ohne Aufnahme unbekannt, nie „aus“.
        dv=einfrieren(lese_aufnahme(dv, DV_FELDER) if isinstance(dv, dict) else {}),
        vorab_netz=einfrieren(lese_aufnahme(vorab_netz, VORAB_NETZ_FELDER) if isinstance(vorab_netz, dict) else {}),
        platz=einfrieren(lese_aufnahme(platz, PLATZ_FELDER) if isinstance(platz, dict) else {}),
        phasen=einfrieren(lese_aufnahme(phasen, PHASEN_FELDER) if isinstance(phasen, dict) else {}),
        spitze=einfrieren(lese_aufnahme(spitze, SPITZE_FELDER) if isinstance(spitze, dict) else {}),
        ausfall=einfrieren(lese_aufnahme(ausfall, AUSFALL_FELDER) if isinstance(ausfall, dict) else {}),
        last=einfrieren(lese_aufnahme(last, LAST_FELDER) if isinstance(last, dict) else {}))


# Nur benötigte fachliche Ergebnisse, kein Rohdump der Controllerdiagnose.
ERGEBNIS_FELDER = (
    "state", "mode", "val", "auto_limit", "wallbox_data_outage", "abregel_charge_req_w", "smartcharge_external_veto",
    "market_economics_action", "market_economics_contract", "market_economics_forecast",
    "market_economics_target_soc_pct", "market_economics_planned_charge_w",
    "market_grid_charge_control", "market_grid_job_done", "market_late_fill_window_end_ts", "market_plan_grace",
    "cheap_grid_charge",
    "predump_target_soc", "predump_floor_soc", "predump_grid_fallback",
    "predump_no_grid", "predump_consumer_load_w",
    "predump_active", "pre_discharge", "storm_guard_active", "storm_grid_charge_active",
    "heat_tariff_shift", "peak_shaving", "backup_hold_control", "storage_dispatch_phase5",
    "force_wallbox_stop", "heatpump_pv_contract", "heatpump_start_funding",
    "authorized_heatpump_budget_w", "heatpump_pause_request", "consumer_budget_contract",
    "wallbox_phase_transition", "wallbox_phase_transition_grants",
    "wallbox_phase_transition_reserved_w",
    "predump_consumer_plan", "predump_discharge_add_contract", "sliding_horizon_active",
)


def ergebnisse_binden(eingaenge, payload, power_settings):
    return Uebergabe(eingaenge,
        einfrieren({k: payload[k] for k in ERGEBNIS_FELDER if k in payload}),
        einfrieren(payload.get("rscp_request_transaction") or {}),
        einfrieren({k: power_settings[k] for k in ("confirmed", "readback", "requested", "ts")
                   if k in power_settings}))
