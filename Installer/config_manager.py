import os
import json
import logging
import shutil
import datetime
import sys

# Standard-Ausgabe auf UTF-8 erzwingen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')\
    
except Exception:
    pass

from .utils import run_command
from .installer_config import (
    WEB_CONFIG_START_DEFAULTS,
    apply_web_config_start_defaults,
    get_install_path,
    get_install_user,
    load_config,
)
from .config_secret_permissions import apply_config_secret_permissions, config_secret_dir_mode_text, config_secret_file_mode_text
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))
V4_CONFIG_FILE = '/var/www/html/data/e3dc_v4.json'

GREEN = '\033[92m'
RED   = '\033[91m'
RESET = '\033[0m'

# =============================================================================
# V4-SCHLUESSEL-INVENTAR (kanonische Liste aller gueltigen Konfigurations-Keys)
# Strukturiert in logische Bloecke. NUR diese Keys werden in e3dc_v4.json
# zugelassen – alles andere (Laufzeit-Zustaende, Eba-M Altlasten) wird entfernt.
# =============================================================================

# --- Block 1: E3DC Verbindung ---
V4_BLOCK_CONNECTION = {
    'server_ip', 'server_port',
    'e3dc_user', 'e3dc_password', 'aes_password',
    'wurzelzaehler', 'wurzelzaehler_invertiert',
    'live_grid_pm_delta_debounce_enable', 'live_grid_pm_delta_soft_threshold_w',
    'live_grid_pm_delta_hard_threshold_w', 'live_grid_pm_delta_persist_count',
    'live_grid_pm_delta_persist_window_s',
}

# --- Block 2: PV-Anlage & Standort ---
V4_BLOCK_PV = {
    'hoehe', 'laenge',
    'openmeteo',
    'forecast1', 'forecast2', 'forecast3',  # Dachflaechen (Neigung/Azimuth/kWp)
    'solcast_api_key', 'solcast_api_key_2',
    'solcast_calls_per_day', 'solcast_calls_per_day_2',
    'solcast_resource_id', 'solcast_resource_id_2', 'solcast_resource_id_3',
    'ml_home_cap_kw',   # Max. Hausverbrauch im ML-Training (kW) – verhindert WB-Artefakte
}

# --- Block 3: Energiepreise & Tarife ---
V4_BLOCK_TARIFF = {
    'awattar', 'awmwst', 'awnebenkosten', 'awreserve', 'awsimulation',
    'strompreis_basis', 'strompreis_cheap', 'strompreis_uht', 'strompreis_spezial',
    'stromtarif_typ', 'tariff_provider', 'tibber_api_token', 'tibber_home_id',
    'entsoe_api_token',
    'grid_friendly_mode',
    # Negativpreis-Boost/Kompatibilität: bisherige Preisfenster erkennen, Verbraucher optional freigeben.
    'cheap_grid_boost_enable', 'cheap_grid_price_limit_ct',
    'cheap_grid_min_duration_min',
    'cheap_grid_battery_enable', 'cheap_grid_wallbox_enable',
    'cheap_grid_heatpump_enable', 'cheap_grid_heater_enable',
    'cheap_grid_battery_max_soc', 'cheap_grid_battery_max_w',
    'cheap_grid_pv_buffer_pct', 'cheap_grid_soc_hysteresis_pct',
    'heat_policy_runtime_enable', 'ems_budget_runtime_enable',
    'heat_heater_grid_boost_enable', 'heat_heater_grid_boost_ack',
    'heat_heater_grid_boost_requires_deficit',
    'heat_heater_grid_boost_price_limit_ct', 'heat_heater_grid_boost_max_w',
    'heat_heater_min_temp_c', 'heat_heater_max_temp_c',
    'heat_wp_daily_kwh',
    # Preisbasierte Speicherregelung: gemeinsame Marktökonomie ohne Direktvermarktungszwang.
    'market_min_margin_pct', 'market_safety_correction_ct_per_kwh',
    'market_autarky_first_enable', 'market_autarky_low_soc_pct',
    'market_autarky_horizon_buffer_wh',
    'market_target_hysteresis_pct', 'market_owner_dwell_s',
    'market_late_fill_enable', 'market_late_fill_buffer_pct',
    'market_late_fill_safety_min', 'market_late_fill_min_delay_min',
    'market_battery_grid_charge_enable', 'market_battery_hold_enable',
    'market_wallbox_enable',
    'market_heatpump_enable', 'market_heater_enable',
    # Direktvermarktung: Der Storage Manager bleibt alleiniger Owner.
    'direct_marketing_enable', 'direct_marketing_mode', 'direct_marketing_profit_profile', 'direct_marketing_provider_name',
    'direct_marketing_settlement_basis', 'direct_marketing_revenue_offset_ct',
    'direct_marketing_fee_ct_per_kwh', 'direct_marketing_fee_pct',
    'direct_marketing_min_margin_pct', 'direct_marketing_min_profit_ct_per_kwh',
    'direct_marketing_min_window_profit_eur', 'direct_marketing_min_export_energy_kwh',
    'direct_marketing_min_export_window_min',
    'direct_marketing_profit_hold_ct_per_kwh', 'direct_marketing_margin_hold_pct',
    'direct_marketing_degradation_ct_per_kwh', 'direct_marketing_roundtrip_efficiency_pct',
    'direct_marketing_safety_margin_ct_per_kwh', 'direct_marketing_export_enable',
    'direct_marketing_grid_charge_enable', 'direct_marketing_arbitrage_enable',
    'direct_marketing_pv_store_enable', 'direct_marketing_pv_store_threshold_ct',
    'direct_marketing_pv_store_max_w', 'direct_marketing_pv_store_min_surplus_w',
    'direct_marketing_pv_store_import_guard_w', 'direct_marketing_pv_store_min_hold_s',
    'direct_marketing_pv_store_ramp_step_w', 'direct_marketing_pv_store_dc_only_enable',
    'direct_marketing_pv_store_external_ac_guard_w',
    'direct_marketing_pv_store_export_limit_guard_w', 'direct_marketing_pv_store_export_limit_ramp_bypass_w',
    'direct_marketing_price_max_age_s',
    'direct_marketing_max_export_w',
    'direct_marketing_min_grid_export_w', 'direct_marketing_max_grid_charge_w', 'direct_marketing_max_cycles_per_day',
    'direct_marketing_home_reserve_soc_pct', 'direct_marketing_night_reserve_soc_pct',
    'direct_marketing_morning_export_target_soc_pct', 'direct_marketing_negative_price_no_export',
    'direct_marketing_negative_headroom_enable', 'direct_marketing_negative_headroom_lookahead_min',
    'direct_marketing_negative_headroom_min_window_min', 'direct_marketing_negative_headroom_min_surplus_wh',
    'direct_marketing_negative_headroom_buffer_pct',
    'direct_marketing_low_price_headroom_enable',
    'direct_marketing_low_price_no_export', 'direct_marketing_keep_headroom_pct',
    'direct_marketing_negative_price_charge_target_soc_pct',
    'direct_marketing_low_price_curtail_enable', 'direct_marketing_low_price_curtail_limit_w',
    'direct_marketing_market_value_solar_enable', 'direct_marketing_market_value_solar_source',
    'direct_marketing_aux_inverter_shelly_override', 'direct_marketing_aux_inverter_shelly_ip',
    'direct_marketing_aux_inverter_shelly_invert', 'direct_marketing_aux_inverter_shelly_dynamic_unblock_enable',
    'direct_marketing_aux_inverter_shelly_unblock_threshold_w',
    'netztransparenz_client_id', 'netztransparenz_client_secret',
    'direct_marketing_eeg_enable', 'direct_marketing_eeg_commissioning_date',
    'direct_marketing_eeg_support_years', 'direct_marketing_eeg_tariff_tiers',
    'direct_marketing_eeg_rate_source', 'direct_marketing_eeg_system_type',
    'direct_marketing_eeg_feed_type', 'direct_marketing_eeg_compensation_basis',
    'direct_marketing_eeg_grid_export_risk_ack',
}

# --- Block 4: Speicher-Manager (Fallback-Werte) ---
V4_BLOCK_STORAGE = {
    'speichergroesse',           # Batteriegröße kWh (Fallback wenn RSCP nicht liefert)
    'maximumladeleistung',       # Max Ladeleistung W (Lade-Limit)
    'maximaleentladeleistung',   # Max Entladeleistung W
    'ems_budget_runtime_enable',
    'einspeiselimit',            # Einspeise-Limit W (für PV-Derating)
    'storage_curve_target_mode', 'storage_curve_sliding_horizon_enable',
    'storage_curve_charge_servo_mode', 'storage_curve_charge_servo_min_w',
    'storage_curve_charge_servo_deadband_w', 'storage_curve_charge_servo_step_up_w',
    'storage_curve_charge_servo_step_down_w', 'storage_curve_charge_servo_max_age_s',
    'storage_target_soc', 'storage_morning_soc',
    'storage_morning_hour', 'storage_predump_min_soc',
    'predump_enable', 'storage_headroom_discharge_enable',
    'storage_headroom_discharge_daily_limit_pct', 'storage_headroom_discharge_cooldown_min',
    'storage_headroom_discharge_target_plateau_margin_pct',
    'hard_predump_enable', 'hard_predump_target_soc',
    'hard_predump_grid_enable', 'hard_predump_grid_max_w',
    'predump_wallbox_enable', 'predump_heatpump_enable', 'predump_heater_enable',
    'predump_grid_guard_w', 'predump_pause_grid_guard_w',
    'storage_absorb_pct', 'storage_release_pct',
    'storage_hysteresis_cycles', 'ep_reserve_pct',
    # Einstellbare Zwischenziele fuer mehrteilige Ladekurve
    'storage_mid_target_soc',   # Fruehes Zwischenziel (0=aus, z.B. 60)
    'storage_mid_hour',         # Ziel-Uhrzeit fuer fruehes Zwischenziel
    'storage_noon_target_soc',  # Soll-SoC bis noon_hour (0=aus, z.B. 85)
    'storage_noon_hour',        # Ziel-Uhrzeit fuer Zwischenziel (z.B. 14 = 14:00 Uhr)
    'storage_emergency_noon_target_soc',
    'storage_emergency_forecast_factor',
    'storage_manual_override_max_age_h',
    # Unwetterwaechter: Wetterwarnungen koennen nur warnen oder die Ladekurve anheben.
    'storm_guard_mode', 'storm_guard_min_level',
    'storm_guard_precharge_lead_min', 'storm_guard_min_precharge_kwh',
    'storm_guard_max_soc', 'storm_guard_grid_enable', 'storm_guard_grid_min_level',
    'storm_guard_grid_morning_soc',
    # Netz-Laden / Preis-Override Schwellen (storage_manager.py price_override())
    'netz_laden_enable',          # 1 = Netz-Laden bei guenstigen Preisen aktiv
    'grid_discharge_enable',      # 1 = Netz-Entladen bei teuren Preisen aktiv
    'netz_laden_price_limit',     # Max. Endkundenpreis ct/kWh fuer Guenstig-Trigger (default 20.0)
    'netz_entladen_price_limit',  # Min. Endkundenpreis ct/kWh fuer Teuer-Trigger  (default 35.0)
    'grid_max_reserve_amp',       # Reserve am Hausanschluss fuer echtes Preis-Netzladen
    # PV-Ueberschuss-Floor (Ladekurve Hysterese)
    'storage_pv_floor_ratio',       # 0.0=aus, 0.15=15% des Ueberschusses
    'storage_pv_floor_threshold_w', # Erst ab diesem Ueberschuss aktiv (W)
    'storage_pv_floor_max_w',       # Obere Grenze des Floors (W)
    # Dokumentierter Firmware-Kompatibilitaetsschalter fuer alternative Ladebefehle.
    'storage_ems_charge_quirk',     # 1 = EMS_CHARGE statt set_limit() bei Ueber-Kurve
    # Trajectory-Clamping (Ladekurve aus target_timeline aktiv verfolgen)
    'pd_eco_min', 'pd_eco_max', 'pd_max_hours', 'eco_dump_regelbuffer_pct',
    'eco_dump_min_soc',           # Legacy-Alias; storage_predump_min_soc bleibt fuehrend
    'abregel_puffer_w', 'abregel_hysterese_w', 'abregel_min_charge_w',
    'abregel_auto_band_w', 'abregel_auto_grace_s',
    'tl_autodump_enable', 'tl_autodump_start_pct',
    'tl_autodump_release_pct', 'tl_autodump_horizon_h',
    'tl_autodump_min_w',
    'tl_enable',           # 1 = Clamping aktiv (default), 0 = reines Eba-Verhalten
    'tl_tolerance_pct',    # % SOC Toleranzband oberhalb der Kurve bevor Bremse greift (default 3.0)
    'tl_emergency_tolerance_pct', # Mindest-Toleranz fuer harte TL-Notbremse (default 30.0)
    'tl_lookahead_h',      # Stunden Vorausschau fuer iFc-Zwischenziel (default 2.0)
    'tl_grid_limit_w',     # Max. Netzbezug (W) bevor Bremse aufgehoben wird (Grid-Waechter, default 100)
    'storage_parallel_curve_tolerance_pct',
    'storage_parallel_wb_hold_s', 'storage_parallel_auto_hold_s',
    'storage_parallel_wb_auto_grid_abort_w', 'storage_parallel_grid_relief_enter_w',
    'storage_curve_mode_wallbox_discharge_protect',
    'storage_live_stale_guard_s', 'storage_parallel_curve_charge_reenter_w',
    'storage_parallel_curve_guard_enter_below_pct',
    'storage_auto_limit_heartbeat_enable', 'storage_auto_limit_heartbeat_s',
    'storage_curve_sliding_horizon_min_open_s',
    'storage_curve_latest_charge_freeze_s', 'storage_curve_latest_charge_replan_margin_s',
    'storage_curve_shortfall_late_catchup_enter_w',
    'storage_decision_history_enable', 'storage_decision_history_max_bytes',
    'storage_decision_history_retention_days', 'storage_decision_history_interval_s',
    'wallbox_decision_history_enable', 'wallbox_decision_history_max_bytes',
    'wallbox_decision_history_retention_days', 'wallbox_decision_history_interval_s',
    'energy_decision_history_enable', 'energy_decision_history_max_bytes',
    'energy_decision_history_retention_days', 'energy_decision_history_interval_s',
    'e3dc_live_persistent_connection', 'e3dc_live_static_poll_s',
    'e3dc_live_history_poll_s', 'e3dc_live_system_poll_s',
    # Explizite Zeitfenster fuer grosse, statische Fremdlasten.
    'planned_load_enable', 'planned_load_min_power_w',
    'planned_load_min_duration_min', 'planned_load_confirm_grace_min',
    'planned_load_late_grace_min', 'planned_load_early_grace_min',
    'planned_load_support_min_price_ct', 'planned_load_support_min_soc',
    'planned_load_support_max_kwh', 'planned_load_support_pv_recovery_factor',
    'planned_load_windows',
}

# --- Block 5: Preis-/KI-Logik ---
V4_BLOCK_INTELLIGENCE = {
    'price_boost_enable', 'price_limit', 'price_hard_limit',
    'price_pause_limit', 'price_min_duration', 'price_max_daily',
    'consumer_priority_order', 'consumer_priority_wp_runon_s',
    'show_forecast', 'frontend_variant', 'frontend_detail_mode',
    'check_updates', 'auto_update_enable', 'auto_update_time',
}

# --- Block 6: Wallbox ---
V4_BLOCK_WALLBOX = {
    'wb_native_enable', 'wb_native_type', 'wb_native_ip',
    'wb_native_type2', 'wb_native_ip2',
    'wb_native_mode', 'wb_native_eco', 'wbmaxladestrom',
    'wb1_max_amp', 'wb2_max_amp',
    'wb1_current_step_amp', 'wb2_current_step_amp',
    'grid_max_amps', 'dvcarlimit',
    'wb_restart_delay_s', 'wb_min_charge_time_s',
    'wb_cloud_stop_delay_s', 'wb_phase_change_hold_s',
    'wb1_restart_delay_s', 'wb1_min_charge_time_s',
    'wb1_cloud_stop_delay_s', 'wb1_phase_change_hold_s',
    'wb2_restart_delay_s', 'wb2_min_charge_time_s',
    'wb2_cloud_stop_delay_s', 'wb2_phase_change_hold_s',
    'wb_openwb_zero_budget_hold_s',
    'wb_shadow_start_delay_s', 'wb_shadow_power_ramp_s',
    'wb_shadow_meter_delay_s', 'wb_shadow_meter_ramp_s',
    'wb_shadow_phase_pause_s', 'wb_shadow_zero_budget_stop_s',
    'wb_shadow_zero_budget_grid_stop_s',
    'wb_openwb_modbus_secondary_enable', 'wb_openwb_modbus_port',
    'wb_openwb_modbus_unit', 'wb_openwb_modbus_connector',
    'wb_openwb_modbus_offset',
    'wb_openwb_primary_enable', 'wb_openwb_auto_discovery',
    'wb_openwb_auto_role_enable', 'wb_openwb_command_fail_limit',
    'wb_openwb_command_block_s',
    'wbhour', 'wbvon', 'wbbis', 'wb_sofort', 'wb_no_time_limit',
    'wb_battery_departure_window_h',
    'wb1_plan_hours', 'wb1_wbvon', 'wb1_wbbis',
    'wb1_battery_departure_time', 'wb1_battery_departure_window_h',
    'wb1_smart_wbhour_enable', 'wb1_native_eco',
    'wb2_plan_hours', 'wb2_wbvon', 'wb2_wbbis',
    'wb2_battery_departure_time', 'wb2_battery_departure_window_h',
    'wb2_smart_wbhour_enable', 'wb2_native_eco',
    'wb_native_cp_id',
    'wb1_topic_prefix', 'wb2_topic_prefix',
    'wb1_mode', 'wb1_observe_storage_policy',
    'wb1_car_id', 'wb1_capacity', 'wb1_target_unit', 'wb1_target_kwh', 'wb1_target_soc',
    'wb1_charge_power', 'wb1_max_soc_si',
    'wb2_mode', 'wb2_observe_storage_policy',
    'wb2_car_id', 'wb2_capacity', 'wb2_target_unit', 'wb2_target_kwh', 'wb2_target_soc',
    'wb2_charge_power', 'wb2_max_soc_si',
    'smart_wbhour_enable', 'wbcostpowers',
    'shelly_wb_ip', 'shelly_wb2_ip',
    'wbminsoc',              # Mindest-SoC Batterie fuer Wallbox-Betrieb (Haus-Prio)
}

# --- Block 7: Waermepumpe / Energie-Manager ---
V4_BLOCK_HEATPUMP = {
    'wp_type', 'wp_source_type', 'luxtronik', 'luxtronik_ip',
    'luxtronik_pause_setpoint_c',
    'idm_ip', 'idm_port', 'idm_e_total', 'idm_cooling_boost_min_at',
    'idm_pv_surplus_enable', 'idm_pv_surplus_max_kw', 'idm_pv_surplus_min_kw',
    'idm_pv_surplus_ramp_kw', 'idm_pv_surplus_deadband_kw',
    'idm_pv_surplus_heartbeat_s', 'idm_pv_surplus_min_write_interval_s',
    'shelly_sg_ip', 'shelly_pause_ip',
    'auto_mode', 'grid_start_limit',
    'pv_boost_delay', 'stop_delay_minutes', 'wp_min_runtime_min',
    'wp_restart_block_min', 'min_soc',
    'heizgrenze_temp', 'wws', 'www', 'hz', 'khl',
    'pv_pause_enable', 'pv_pause_soc', 'pv_pause_watt',
    'pv_pause_timeout_minutes', 'pv_pause_min_at', 'pv_pause_max_temp_drop',
    'manual_boost_min_soc', 'manual_boost_max_duration',
    'wq_min_temp', 'rl_source',
    'ww_timer_enable', 'wwvon', 'wwbis', 'ww_normal', 'ww_eco',
    'ww_circ_von', 'ww_circ_bis', 'ww_circ_on', 'ww_circ_off', 'ww_circ_boost',
    # Heizstab / Shelly / myPV
    'heizstab', 'heizstab_ip', 'heizstab_port', 'heizstab_type',  # Modbus-IP, Port, Typ (generic | mypv_elwa)
    'heizstab_max_w', 'shelly_heiz_ip', 'shelly_heiz_w',
    'hs_auto_mode', 'hs_min_surplus_w', 'hs_min_soc',
    # Shelly Pro3EM WP-Integration (wp_type=3)
    'shelly_3em_ip',       # IP des Shelly Pro3EM (Energiemessung + optionales Relais)
    'shelly_3em_relay_id', # Relais-ID 0-2 (-1 = nur messen, nicht schalten)
    'shelly_3em_wp_min_w', # WP Mindestleistung als Einschaltschwelle (W)
    'shelly_3em_wp_max_w', # WP Nennleistung fuer Budget-Berechnung (W)
    'shelly_3em_enable',   # 1=PV-Auto-Schalten, 0=nur Messen
    # Klimaanlage / gemessener Zusatzverbraucher
    'climate_enable',
    'climate_name',
    'climate_meter_ip',
    'climate_meter_type',
    'climate_meter_phase',
    'climate_min_power_w',
    'climate_poll_s',
    'climate_history_enable',
    'climate_history_interval_s',
    'climate_forecast_enable',
    'climate_control_enable',
    'climate_control_poll_s',
    'climate_toshiba_cloud_enable',
    'climate_toshiba_username',
    'climate_toshiba_password',
    'climate_toshiba_device_ids',
    # Stiebel Eltron ISG / WPM (wp_type=4)
    'stiebel_isg_ip',
    'stiebel_isg_port',
    'stiebel_isg_device_id',
    'stiebel_isg_power_heating_w',
    'stiebel_isg_power_dhw_w',
    'stiebel_isg_cop_estimate',
    'stiebel_isg_standby_w',
    'stiebel_isg_max_hz',
    'stiebel_isg_hz_power_map',
    'stiebel_isg_scrape_hz_enable',
    'stiebel_isg_web_user',
    'stiebel_isg_web_password',
    'stiebel_isg_power_meter_enable',
    'stiebel_isg_power_meter_ip',
    'stiebel_isg_power_meter_type',
    # Dimplex WPM Touch / NWPM (wp_type=5)
    'dimplex_ip',
    'dimplex_port',
    'dimplex_unit_id',
    'dimplex_wpm_software',
    'dimplex_sg_register',
    'dimplex_modbus_zero_based',
    'dimplex_outdoor_register',
    'dimplex_dhw_register',
    'dimplex_return_register',
    'dimplex_flow_register',
    'dimplex_return_setpoint_register',
    'dimplex_dhw_setpoint_register',
    'dimplex_heat_source_in_register',
    'dimplex_heat_source_out_register',
    'dimplex_cooling_flow_register',
    'dimplex_cooling_return_register',
    'dimplex_cooling_primary_return_register',
    'dimplex_operating_mode_register',
    'dimplex_heat_power_register',
    'dimplex_electric_power_register',
    'dimplex_heartbeat_out_register',
    'dimplex_cop_estimate',
    'dimplex_temp_scale',
    'dimplex_sg_heartbeat_s',
    'dimplex_allow_dark_green',
}

# --- Block 8: Fahrzeuge & Bluelink ---
V4_BLOCK_VEHICLE = {
    'bluelink_refresh_token', 'bluelink_vin', 'bluelink_car_name',
    'bluelink_interval', 'bluelink_ignore_plug_status',
}

# --- Block 9: Benachrichtigungen ---
V4_BLOCK_NOTIFY = {
    'telegram_token', 'telegram_chat_id', 'telegram_device_name',
    'telegram_status_enable', 'telegram_status_time',
    'telegram_stats_enable', 'telegram_stats_time',
    'telegram_weekly_enable', 'telegram_weekly_time', 'telegram_weekly_day',
    'push_notify_soc', 'push_notify_plugged', 'push_notify_unplugged',
    'push_notify_autostart', 'push_notify_warnings',
    'push_notify_notstrom', 'push_notify_updates',
}

# --- Block 10: HA & Netzwerk ---
V4_BLOCK_HA = {
    'ha_mode', 'ha_peer_ip', 'ha_sync_interval',
    'ha_fail_timeout', 'ha_auto_recover', 'ha_auto_failover',
    'shadow_master_url', 'shadow_master_ip', 'shadow_sync_interval_s',
    'shadow_fetch_timeout_s', 'shadow_snapshot_max_age_s',
    'mqtt_hub_ip', 'mqtt_hub_port', 'mqtt_hub_user', 'mqtt_hub_pass',
    'mqtt_hub_topic',
    'mqtt_hub_sub_soc_topic', 'mqtt_hub_sub_soc_name',
    'mqtt_hub_sub_soc_topic_2', 'mqtt_hub_sub_soc_name_2',
    # Direkter externer Wallbox-MQTT-Messwert, z.B. evcc/loadpoints/1/chargePower.
    'wb_ip', 'wb_topic', 'wb_user', 'wb_pass',
    'wb2_ip', 'wb2_topic', 'wb2_user', 'wb2_pass',
    'mqtt_ha_inbound_enable', 'mqtt_ha_inbound_history_enable',
}

# --- Block 11: Web-UI & System ---
V4_BLOCK_UI = {
    'darkmode', 'web_pin', 'matter_bridge',
    'pvatmosphere', 'show_forecast', 'frontend_variant', 'frontend_detail_mode',
    'config_secret_protection_mode',
    'check_updates', 'auto_update_enable', 'auto_update_time',
    'logfile', 'ui_energy_flow',
}

# --- Block 8: System und Installationspfade ---
V4_BLOCK_SYSTEM = {
    'e3dcwallboxtxt',
    # WICHTIG: Pfade muessen in der v4 JSON bleiben, da PHP sie braucht!
    'install_user', 'home_dir', 'install_path', 'venv_name', 'venv_path', '_comment'
}

# Gesamtes gueltiges Inventar
V4_ALL_KEYS = (
    V4_BLOCK_CONNECTION | V4_BLOCK_PV | V4_BLOCK_TARIFF |
    V4_BLOCK_STORAGE | V4_BLOCK_INTELLIGENCE | V4_BLOCK_WALLBOX |
    V4_BLOCK_HEATPUMP | V4_BLOCK_VEHICLE | V4_BLOCK_NOTIFY |
    V4_BLOCK_HA | V4_BLOCK_UI | V4_BLOCK_SYSTEM
)

V4_LOCAL_PATH_KEYS = {'install_user', 'home_dir', 'install_path', 'venv_name', 'venv_path'}

# Keys die NIEMALS in e3dc_v4.json gespeichert werden duerfen
V4_BLACKLIST = {
    # Laufzeit-Zustaende (gehoeren in Ramdisk)
    'wbhour', 'wb_sofort', 'wb_locked', 'wb2_locked', 'wbbis', 'wbvon', 'wbmode',
    # Lokale Installationspfade sind gueltige Metadaten. Import/Rollback schuetzt sie
    # separat gegen Fremdwerte; Cleanup darf sie nicht zyklisch entfernen.
    # Dopplung
    'config',
    # ===== C++ LEGACY (entfernt mit V4-Pure Migration) =====
    # Speicher-Parameter (nur C++ awattar.cpp / Eba-M relevant)
    'speicherev',       # Ladewirkungsgrad (C++ intern)
    'speichereta',      # Gesamtwirkungsgrad (C++ intern)
    'unload',           # Entladen bis % (C++ intern)
    'ladeschwelle',     # Ladestart SoC (C++ intern)
    'ladeende',         # Ladeende SoC (C++ intern)
    'ladeende2',        # Zweites Ladeende (C++ intern)
    'ladeende2rampe',   # Rampe (C++ intern)
    'winterminimum',    # Winter SoC Minimum (C++ intern)
    'sommermaximum',    # Sommer SoC Maximum (C++ intern)
    'sommerladeende',   # Sommer Ladeende (C++ intern)
    # Prognose-Parameter (nur C++ awattar.cpp relevant)
    'forecastsoc',        # Forecast SoC Ziel (C++ awattar.cpp)
    'forecastconsumption', # Forecast Verbrauch (C++ awattar.cpp)
    'forecastreserve',    # Forecast Reserve (C++ awattar.cpp)
    'forecast4', 'forecast5',  # Zusatz-Arrays (C++ intern, nicht in Python genutzt)
    # Atmosphäre (Installer-Setup nur; nicht im laufenden Betrieb)
    'pvatmosphere',
    # Logfile Pfad (C++ intern; Python nutzt journald/systemd)
    'logfile',
    # Veralteter Legacy-Schalter
    'openwb',
    # Stillgelegte Test-, Vergleichs- und Zukunftspfade. Vorhandene Altwerte
    # werden beim Cleanup entfernt, damit sie nicht versehentlich reaktiviert werden.
    'storage_parallel_enable', 'storage_parallel_history_enable',
    'storage_parallel_history_max_lines', 'storage_parallel_diff_enable',
    'storage_parallel_diff_min_w', 'storage_parallel_diff_log_interval_s',
    'direct_marketing_arbitrage_experimental_enable',
    'super_intelligence_enable', 'super_intelligence_deadline',
    'morning_boost_enable', 'morning_boost_prio', 'morning_boost_wb_power',
    'morning_boost_deadline', 'morning_boost_target_soc',
    'morning_boost_min_hours', 'morning_boost_min_pv_pct',
    'v2h_enable', 'v2h_min_soc', 'v2h_bat_soc_limit',
    'shelly0v10v', 'shelly0v10v_ip', 'shelly0v10vmin', 'shelly0v10vmax',
    'shelly0v10vezh1', 'shelly0v10vezh2', 'shelly0v10vezh3', 'shelly0v10vezh4',
    'tasmota',
    'bwwpein', 'bwwpaus', 'bwwpon', 'bwwpoff', 'bwwpmax', 'bwwpsupport',
    'bwwptasmotadauer', 'bwwptasmota', 'bwwp_power', 'bwwp_port', 'bwwp_ip',
    'mqtt_ip', 'mqtt2_ip', 'mqtt3_ip', 'mqtt4_ip', 'mqtt4_topic', 'mqttavl',
    'forecast4', 'forecast5',
    'wpheizlast', 'wpleistung', 'wpheizgrenze', 'wpnat', 'wpmin', 'wpmax',
    'wppvon', 'wppvoff', 'wphk1', 'wphk1max', 'wphk2on', 'wphk2off',
    'wpehz', 'wpzwe', 'wpzwepvon', 'wpoffset', 'wpdyncop',
    # Redundanz: car_* ist jetzt wb1_*
    'car_capacity', 'car_target_unit', 'car_target_kwh', 'car_target_soc', 'car_max_soc_si', 'car_charge_power',
    # C++ Legacy: Wallbox-Wirkungsgrad-Keys (leere Strings -> float() Crash)
    # Grossbuchstaben-Duplikate die config_editor.php faelschlicherweise schreibt
    # (Korrekte Kleinbuchstaben-Keys existieren bereits)
    'GRID_START_LIMIT', 'MIN_SOC', 'HEIZGRENZE_TEMP', 'WWS', 'WWW', 'HZ', 'KHL',
}

# Reihenfolge der Bloecke fuer strukturierte JSON-Ausgabe
V4_BLOCK_ORDER = [
    ('E3DC Verbindung',             V4_BLOCK_CONNECTION),
    ('PV-Anlage & Standort',        V4_BLOCK_PV),
    ('Energiepreise & Tarife',      V4_BLOCK_TARIFF),
    ('Speicher-Manager (Gehirn)',   V4_BLOCK_STORAGE),
    ('Preis-/KI-Logik',            V4_BLOCK_INTELLIGENCE),
    ('Wallbox',                     V4_BLOCK_WALLBOX),
    ('Waermepumpe / Energie-Mgr',  V4_BLOCK_HEATPUMP),
    ('Fahrzeuge & Bluelink',        V4_BLOCK_VEHICLE),
    ('Benachrichtigungen',          V4_BLOCK_NOTIFY),
    ('HA & Netzwerk',               V4_BLOCK_HA),
    ('Web-UI & System',             V4_BLOCK_UI),
]

WALLBOX_TYPE_ALIASES = {
    '': '',
    'none': 'none',
    'off': 'none',
    'disabled': 'none',
    'dummy': 'none',
    'goe': 'go-e',
    'go-echarger': 'go-e',
    'openwb-pro': 'openwb_pro',
    'openwbpro': 'openwb_pro',
    'e3dc_multi_connect': 'e3dc_multi',
    'e3dc-multi': 'e3dc_multi',
    'e3dc multi': 'e3dc_multi',
    'multiconnect': 'e3dc_multi',
    'multi_connect': 'e3dc_multi',
    'e3dc_easy': 'e3dc',
    'e3dc_legacy': 'e3dc',
    'native': 'e3dc',
}


# =============================================================================
# HILFSFUNKTIONEN
# =============================================================================

def _normalise_wallbox_type(value) -> str:
    """Normalisiert UI-/Alt-Aliase auf die kanonischen V4-Wallbox-Typen."""
    typ = str(value or '').strip().lower()
    return WALLBOX_TYPE_ALIASES.get(typ, typ)


def _normalise_percent(value, fallback: float = 50.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().replace(',', '.')
        parsed = float(value)
    except (TypeError, ValueError):
        parsed = fallback
    return max(4.0, min(96.0, round(parsed, 2)))


def _normalise_flow_color(value, fallback: str) -> str:
    raw = str(value or '').strip().lower()
    if len(raw) == 7 and raw.startswith('#'):
        try:
            int(raw[1:], 16)
            return raw
        except ValueError:
            pass
    if len(raw) == 4 and raw.startswith('#'):
        try:
            int(raw[1:], 16)
            return '#' + raw[1] * 2 + raw[2] * 2 + raw[3] * 2
        except ValueError:
            pass
    return fallback


def _normalise_ui_energy_flow(value) -> dict:
    """Keep the Energy-Flow layout as structured UI config during cleanup."""
    if not isinstance(value, dict):
        return {}
    allowed_nodes = ('pv', 'external_pv', 'grid', 'battery', 'home', 'wallbox', 'wallbox2', 'heatpump', 'heater', 'climate')
    default_colors = {
        'pv': '#ffc107',
        'external_pv': '#22c55e',
        'grid': '#6c757d',
        'grid_import': '#ef4444',
        'grid_export': '#2ecc71',
        'home': '#0dcaf0',
        'battery': '#198754',
        'battery_charge': '#2ecc71',
        'wallbox': '#2ecc71',
        'wallbox2': '#34d399',
        'heatpump': '#f97316',
        'heater': '#fd7e14',
        'climate': '#38bdf8',
        'center': '#0d6efd',
    }
    clean: dict = {}
    for layout in ('desktop', 'mobile'):
        nodes = {}
        raw_layout = value.get(layout, {})
        raw_nodes = raw_layout.get('nodes', {}) if isinstance(raw_layout, dict) else {}
        if isinstance(raw_nodes, dict):
            for key in allowed_nodes:
                node = raw_nodes.get(key)
                if not isinstance(node, dict):
                    continue
                nodes[key] = {
                    'x': _normalise_percent(node.get('x'), 50.0),
                    'y': _normalise_percent(node.get('y'), 50.0),
                }
        clean[layout] = {'nodes': nodes}

    raw_colors = value.get('colors', {})
    colors = {}
    if isinstance(raw_colors, dict):
        for key, fallback in default_colors.items():
            colors[key] = _normalise_flow_color(raw_colors.get(key, fallback), fallback)
    if colors:
        clean['colors'] = colors
    return clean


def _normalise_v4_values(data: dict) -> dict:
    """Bereinigt Werte, ohne unbekannte Keys zu entfernen."""
    result = dict(data or {})
    for key in ('wb_native_type', 'wb_native_type2'):
        if key in result:
            result[key] = _normalise_wallbox_type(result.get(key))
    if 'ui_energy_flow' in result:
        result['ui_energy_flow'] = _normalise_ui_energy_flow(result.get('ui_energy_flow'))
    return result

def _load_v4() -> dict:
    """Laedt e3dc_v4.json. Gibt {} zurueck bei Fehler."""
    if not os.path.exists(V4_CONFIG_FILE):
        return {}
    try:
        with open(V4_CONFIG_FILE, 'r', encoding='utf-8') as f:
            return json.load(f)
    except Exception as e:
        logging.getLogger('config_manager').error(f'Fehler beim Lesen von e3dc_v4.json: {e}')
        return {}


def _save_v4(data: dict) -> bool:
    """Schreibt e3dc_v4.json atomar (temp-Datei, dann rename)."""
    tmp = V4_CONFIG_FILE + '.tmp'
    try:
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=4, ensure_ascii=False)
        os.replace(tmp, V4_CONFIG_FILE)
        try:
            install_user = get_install_user()
            run_command(f'sudo chown {install_user}:www-data {V4_CONFIG_FILE}')
            run_command(f'sudo chmod {config_secret_file_mode_text(data)} {V4_CONFIG_FILE}')
        except Exception:
            apply_config_secret_permissions(V4_CONFIG_FILE, data=data)
            pass
        return True
    except Exception as e:
        logging.getLogger('config_manager').error(f'Fehler beim Schreiben von e3dc_v4.json: {e}')
        return False


def _sort_by_blocks(data: dict) -> dict:
    """Sortiert ein Dict nach der kanonischen Block-Reihenfolge."""
    data = _normalise_v4_values(data)
    result = {}
    seen = set()
    for block_name, block_keys in V4_BLOCK_ORDER:
        for key in sorted(block_keys):
            if key in data and key not in seen:
                result[key] = data[key]
                seen.add(key)
    # Verbleibende (unbekannte, aber nicht blacklisted) ans Ende
    for key in sorted(data.keys()):
        if key not in seen:
            result[key] = data[key]
    return result


# =============================================================================
# HAUPT-FUNKTIONEN
# =============================================================================

def cleanup_v4_config(dry_run: bool = False) -> bool:
    """
    Bereinigt e3dc_v4.json:
    1. Entfernt blacklisted & unbekannte Keys (Laufzeit-Zustaende, Eba-M Altlasten)
    2. Sortiert nach kanonischer Block-Struktur
    3. Erstellt vorher ein Backup

    dry_run=True: zeigt nur an, was geloescht wuerden haette, schreibt nicht.
    """
    log = logging.getLogger('config_manager')
    print('\n=== V4 Konfigurations-Bereinigung ===\n')

    if not os.path.exists(V4_CONFIG_FILE):
        print(f'  [!] {V4_CONFIG_FILE} nicht gefunden. Nichts zu bereinigen.')
        return True

    # Backup
    if not dry_run:
        ts = datetime.datetime.now().strftime('%Y%m%d_%H%M%S')
        backup_dir = os.path.join(os.path.dirname(V4_CONFIG_FILE), 'config_backups')
        os.makedirs(backup_dir, exist_ok=True)
        try:
            run_command(f'sudo chgrp www-data {backup_dir} && sudo chmod {config_secret_dir_mode_text()} {backup_dir}')
        except: pass
        
        bak = os.path.join(backup_dir, f'e3dc_v4_{ts}.json.bak')
        try:
            shutil.copy2(V4_CONFIG_FILE, bak)
            apply_config_secret_permissions(bak, install_user=get_install_user())
            print(f'  [OK] Backup: {os.path.basename(bak)}')
        except Exception as e:
            print(f'  [!] Backup fehlgeschlagen: {e}')
            log.error(f'V4 Backup fehlgeschlagen: {e}')
            return False

    data = _load_v4()
    if not data:
        print('  [!] e3dc_v4.json ist leer oder unlesbar.')
        return False

    before_defaults = dict(data)
    data = apply_web_config_start_defaults(data)
    filled_defaults = [
        key for key in WEB_CONFIG_START_DEFAULTS
        if before_defaults.get(key) != data.get(key)
    ]

    removed_blacklist = []
    removed_unknown  = []
    kept = {}

    for key, val in data.items():
        k = key.lower()
        if k in V4_BLACKLIST:
            removed_blacklist.append(key)
        elif k not in V4_ALL_KEYS:
            removed_unknown.append(key)
        else:
            kept[k] = val

    if removed_blacklist:
        print(f'  Entferne {len(removed_blacklist)} Altlast-Keys (Blacklist):')
        for k in removed_blacklist:
            print(f'    - {k}')
    if removed_unknown:
        print(f'  Entferne {len(removed_unknown)} unbekannte Keys:')
        for k in removed_unknown:
            print(f'    - {k}')
    if filled_defaults:
        print(f'  Ergaenze {len(filled_defaults)} Start-Defaults:')
        for k in filled_defaults:
            print(f'    + {k}')

    if not removed_blacklist and not removed_unknown and not filled_defaults:
        print(f'  [OK] Keine Altlasten gefunden. Datei ist sauber.')
    else:
        total = len(removed_blacklist) + len(removed_unknown)
        if dry_run:
            print(f'\n  [Dry-Run] {total} Keys WUERDEN entfernt, {len(filled_defaults)} Defaults WUERDEN ergaenzt.')
        else:
            sorted_kept = _sort_by_blocks(kept)
            if _save_v4(sorted_kept):
                print(f'\n  [OK] {total} Keys entfernt, {len(filled_defaults)} Defaults ergaenzt. Datei nach Block-Schema sortiert.')
            else:
                print(f'  [!] Fehler beim Schreiben der bereinigten Datei.')
                return False

    log.info(f'V4 Cleanup: {len(removed_blacklist)} blacklisted, {len(removed_unknown)} unbekannt entfernt, {len(filled_defaults)} defaults ergaenzt.')
    return True


def reorder_v4_config() -> bool:
    """Sortiert e3dc_v4.json nach Block-Schema ohne Inhalte zu aendern."""
    data = _load_v4()
    if not data:
        return False
    # Blacklisted Keys trotzdem rausfiltern
    clean = {k: v for k, v in data.items() if k.lower() not in V4_BLACKLIST}
    sorted_data = _sort_by_blocks(clean)
    if _save_v4(sorted_data):
        print('  [OK] e3dc_v4.json nach Block-Schema sortiert.')
        return True
    return False


def check_config_duplicates():
    """Prueft e3dc.config.txt auf doppelte Eintraege und entfernt Duplikate."""
    log = logging.getLogger('config_manager')
    print('\n=== Konfigurations-Duplikat-Pruefung (e3dc.config.txt) ===\n')

    config_candidates = [
        os.path.join('/var/www/html/data', 'e3dc.config.txt'),
        os.path.join(INSTALL_PATH, 'e3dc.config.txt'),
    ]
    config_file = next((p for p in config_candidates if os.path.exists(p)), None)

    if not config_file:
        print(f'  [OK] e3dc.config.txt nicht gefunden – kein Check noetig (V4 Pure).')
        return True

    try:
        with open(config_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        seen = set()
        new_lines = []
        removed = 0
        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#'):
                new_lines.append(line)
                continue
            if '=' in stripped:
                key = stripped.split('=', 1)[0].strip().lower()
                if key in seen:
                    print(f'  [!] Duplikat entfernt: {stripped}')
                    removed += 1
                    continue
                seen.add(key)
            new_lines.append(line)

        if removed:
            with open(config_file, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            print(f'  [OK] {removed} Duplikate in e3dc.config.txt entfernt.')
            log.info(f'e3dc.config.txt: {removed} Duplikate entfernt.')
        else:
            print(f'  [OK] Keine Duplikate gefunden.')
        return True
    except Exception as e:
        print(f'  [!] Fehler: {e}')
        log.error(f'Duplikat-Pruefung fehlgeschlagen: {e}')
        return False


def _migrate_luxtronik_config():
    """Prueft auf alte config.lux.json und migriert die Werte in e3dc_v4.json."""
    log = logging.getLogger('config_manager')
    lux_path = os.path.join(INSTALLER_DIR, 'luxtronik', 'config.lux.json')
    if not os.path.exists(lux_path):
        return

    print('\n=== Veraltete Luxtronik-Konfiguration gefunden: Migriere in V4 ===\n')
    log.info('Veraltete config.lux.json gefunden – starte V4-Migration.')

    try:
        with open(lux_path, 'r', encoding='utf-8') as f:
            lux_conf = json.load(f)

        v4 = _load_v4()
        for k, v in lux_conf.items():
            key_lower = k.lower()
            if key_lower in V4_ALL_KEYS:
                v4[key_lower] = str(v)
                print(f'  [OK] Migriert: {key_lower}')

        _save_v4(_sort_by_blocks(v4))
        os.rename(lux_path, lux_path + '.migrated')
        print(f'  [OK] Migration abgeschlossen. Alte Datei umbenannt zu .migrated\n')
        log.info('Luxtronik-Migration abgeschlossen.')
    except Exception as e:
        print(f'  [!] Fehler bei Migration: {e}')
        log.error(f'Luxtronik-Migration fehlgeschlagen: {e}')


def _migrate_txt_config():
    """Prueft e3dc.config.txt und migriert fehlende Werte in e3dc_v4.json."""
    log = logging.getLogger('config_manager')
    config_candidates = [
        os.path.join('/var/www/html/data', 'e3dc.config.txt'),
        os.path.join(INSTALL_PATH, 'e3dc.config.txt'),
    ]
    txt_file = next((p for p in config_candidates if os.path.exists(p)), None)

    if not txt_file:
        return

    try:
        with open(txt_file, 'r', encoding='utf-8') as f:
            lines = f.readlines()

        v4 = _load_v4()
        migrated_keys = 0

        for line in lines:
            stripped = line.strip()
            if not stripped or stripped.startswith('#') or '=' not in stripped:
                continue
            
            key, val = stripped.split('=', 1)
            key = key.strip().lower()
            val = val.strip()

            # Nur gueltige Keys migrieren und nur, wenn sie im JSON noch nicht existieren
            if key in V4_ALL_KEYS and key not in v4 and val != '':
                v4[key] = val
                migrated_keys += 1
                print(f'  [OK] V3-Migration: {key} in e3dc_v4.json uebernommen.')

        if migrated_keys > 0:
            _save_v4(_sort_by_blocks(v4))
            print(f'  [OK] {migrated_keys} Konfigurationswerte aus e3dc.config.txt nach e3dc_v4.json migriert.\n')
            log.info(f'V3 Text-Config Migration abgeschlossen. {migrated_keys} Keys migriert.')
    except Exception as e:
        print(f'  [!] Fehler bei V3 Text-Migration: {e}')
        log.error(f'V3 Text-Migration fehlgeschlagen: {e}')


def run_config_wizard():
    """Fuehrt alle Konfigurations-Checks und Migrationen aus."""
    log_task_completed('Konfigurations-Management gestartet', details='V4 Pruefe & migriere')

    _migrate_luxtronik_config()
    _migrate_txt_config()
    check_config_duplicates()
    cleanup_v4_config(dry_run=False)

    log_task_completed('Konfigurations-Management beendet')
