#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Central service and module catalog for installer and WebUI.

This module is intentionally side-effect free: it does not call systemctl,
write files or import heavy project modules. It is the shared source of truth
for WebUI, installer, status checks and wrappers.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Iterable


CORE = "core"
CONSUMERS = "consumers"
INTEGRATIONS = "integrations"
SYSTEM = "system"

LOAD_ESSENTIAL = "essential"
LOAD_ACTIVE_CONTROL = "active_control"
LOAD_COMFORT = "comfort"
LOAD_INTEGRATION = "integration"
LOAD_OBSERVE = "observe"

READ_ACTIONS = ("status", "diagnose", "validate_config")
SERVICE_ACTIONS = ("start", "stop", "restart", "enable", "disable")
ACTIVE_CONTROL_MODULES = {"storage_manager", "wallbox", "heatpump", "heizstab"}
ESSENTIAL_LOAD_MODULES = {"live", "epex", "weather", "storage_simulator", "notifier"}
OBSERVE_LOAD_MODULES = {
    "lux_live",
    "idm_live",
    "stiebel_live",
    "dimplex_live",
    "climate_live",
    "climate_control",
    "shadow",
}


@dataclass(frozen=True)
class ServiceModule:
    key: str
    display_name: str
    group: str
    description: str
    service: str | None = None
    script: str | None = None
    runner: str = "python"
    working_directory: str | None = None
    log_file: str | None = None
    alive_file: str | None = None
    alive_max_age_s: int = 120
    config_keys: tuple[str, ...] = ()
    required_config_keys: tuple[str, ...] | None = None
    dependencies: tuple[str, ...] = ()
    docker_process: str | None = None
    install_hint: str | None = None
    install_warning: str | None = None
    install_notes: tuple[str, ...] = ()
    optional: bool = True
    load_profile: str = LOAD_COMFORT
    actions: tuple[str, ...] = field(default_factory=lambda: READ_ACTIONS)

    @property
    def service_unit(self) -> str | None:
        if not self.service:
            return None
        return self.service if self.service.endswith(".service") else f"{self.service}.service"

    def public_dict(self) -> dict:
        data = asdict(self)
        data["service_unit"] = self.service_unit
        data["load_profile"] = service_load_profile(self)
        return data


def service_load_profile(module: ServiceModule) -> str:
    if module.key in ACTIVE_CONTROL_MODULES:
        return LOAD_ACTIVE_CONTROL
    if module.key in ESSENTIAL_LOAD_MODULES or not module.optional:
        return LOAD_ESSENTIAL
    if module.key in OBSERVE_LOAD_MODULES:
        return LOAD_OBSERVE
    if module.group == INTEGRATIONS:
        return LOAD_INTEGRATION
    return module.load_profile or LOAD_COMFORT


MODULES: dict[str, ServiceModule] = {
    "live": ServiceModule(
        key="live",
        display_name="E3DC Live Data (RSCP)",
        group=CORE,
        description="Liest die Live-Daten vom E3DC-Speicher über RSCP.",
        service="e3dc-live",
        script="e3dc_live.py",
        log_file="/var/www/html/logs/e3dc_live.log",
        alive_file="/var/www/html/ramdisk/live_data_py.json",
        alive_max_age_s=30,
        config_keys=("server_ip", "server_port", "e3dc_user", "e3dc_password"),
        optional=False,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "epex": ServiceModule(
        key="epex",
        display_name="Markt- und Strompreise",
        group=CORE,
        description="Aktualisiert Strompreise, Eco-Score und Preisfenster.",
        service="e3dc-epex-manager",
        script="epex_manager.py",
        log_file="/var/www/html/logs/epex_manager.log",
        alive_file="/var/www/html/ramdisk/epex_daten.json",
        alive_max_age_s=3700,
        optional=False,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "weather": ServiceModule(
        key="weather",
        display_name="PV-Prognose und ML",
        group=CORE,
        description="Holt PV-Prognosen und erzeugt ML-/Ensemble-Vorhersagen.",
        service="e3dc-weather-manager",
        script="Forecast/pv_forecast_service.py",
        log_file="/var/www/html/logs/weather_manager.log",
        alive_file="/var/www/html/ramdisk/pv_forecast.json",
        alive_max_age_s=7200,
        config_keys=("forecast1",),
        optional=False,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "storage_simulator": ServiceModule(
        key="storage_simulator",
        display_name="Storage Simulator",
        group=CORE,
        description="Erzeugt Speicherplan, Ladekurve und Pre-Dump-Ziele.",
        service="e3dc-storage-simulator",
        script="storage_simulator.py",
        log_file="/var/www/html/logs/storage_simulator.log",
        alive_file="/var/www/html/ramdisk/storage_plan.json",
        alive_max_age_s=900,
        dependencies=("live", "weather", "epex"),
        optional=False,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "storage_manager": ServiceModule(
        key="storage_manager",
        display_name="Storage Manager",
        group=CORE,
        description="Regelt Speicherladung und -entladung entlang der Ladekurve.",
        service="e3dc-storage-manager",
        script="storage_manager.py",
        log_file="/var/www/html/logs/storage_manager.log",
        alive_file="/var/www/html/ramdisk/storage_manager_state.json",
        alive_max_age_s=300,
        dependencies=("live", "storage_simulator"),
        optional=False,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "websocket": ServiceModule(
        key="websocket",
        display_name="WebUI Live-Animationen",
        group=CORE,
        description="Liefert WebSocket-Daten für flüssige Live-Ansichten.",
        service="e3dc-websocket",
        script="e3dc_websocket.py",
        log_file="/var/www/html/logs/e3dc_websocket.log",
        alive_file="/var/www/html/ramdisk/live_data_py.json",
        alive_max_age_s=30,
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "wallbox": ServiceModule(
        key="wallbox",
        display_name="Wallbox Manager",
        group=CONSUMERS,
        description="Regelt Wallboxen inklusive PV, Speicher, Budget und Phasen.",
        service="e3dc-wallbox-manager",
        script="wallbox_manager.py",
        log_file="/var/www/html/logs/wallbox_manager.log",
        alive_file="/var/www/html/ramdisk/wallbox_native.json",
        alive_max_age_s=120,
        config_keys=("wb_native_enable",),
        dependencies=("live", "storage_manager"),
        install_warning=(
            "Der Wallbox Manager greift aktiv in Ladefreigabe, Phasen und Stromvorgaben ein. "
            "Installieren nur, wenn die native Wallbox-Regelung bewusst aktiviert ist und keine alte C++-Wallboxsteuerung parallel läuft."
        ),
        install_notes=(
            "Nach der Installation zuerst Read-only-Prüfung und Diagnose ausführen.",
            "Bei E3DC Multi Connect, Easy Connect und openWB sind echte Messwerte wichtiger als Soll-/Phantomwerte.",
        ),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "heatpump": ServiceModule(
        key="heatpump",
        display_name="Wärmepumpe Manager",
        group=CONSUMERS,
        description="Regelt PV-Boost, SG-Ready und Modbus-Wärmepumpenlogik.",
        service="energy_manager",
        script="luxtronik/energy_manager.py",
        log_file="/var/www/html/logs/energy_manager.log",
        alive_file="/var/www/html/ramdisk/waermepumpe.json",
        alive_max_age_s=120,
        config_keys=("luxtronik", "wp_type", "luxtronik_ip", "idm_ip", "stiebel_isg_ip", "dimplex_ip", "shelly_sg_ip", "shelly_pause_ip", "auto_mode"),
        dependencies=("live", "epex"),
        install_warning=(
            "Der direkte Wärmepumpen Manager darf nur für Luxtronik, IDM, Stiebel, Dimplex oder SG-Ready per Shelly installiert werden. "
            "Heizstab/Shelly-Heizlüfter und reine Shelly-Pro3EM-Messung laufen über das Heizstab-Modul."
        ),
        install_notes=(
            "Keine Doppelsteuerung: Luxtronik Live, IDM Live und direkter Manager müssen zum gewählten WP-Typ passen.",
            "Luxtronik Live, IDM Live, Stiebel ISG Live oder Dimplex WPM Live passend zum WP-Typ zuerst installieren und prüfen.",
            "Bei nur messender Shelly-Pro3EM-Konfiguration nicht den direkten Wärmepumpen Manager starten.",
            "Bei SG-Ready per Shelly 1 reicht wp_type=-1 plus shelly_sg_ip oder shelly_pause_ip.",
        ),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "lux_live": ServiceModule(
        key="lux_live",
        display_name="Luxtronik Live",
        group=CONSUMERS,
        description="Liest Luxtronik-/Alpha-Innotec-/Novelan-Livewerte.",
        service="e3dc-lux-live",
        script="luxtronik/lux_live.py",
        log_file="/var/www/html/logs/lux_live.log",
        alive_file="/var/www/html/ramdisk/luxtronik.json",
        alive_max_age_s=120,
        config_keys=("luxtronik", "wp_type", "luxtronik_ip"),
        install_notes=("Reines Live-/Monitoring-Modul für Luxtronik, keine direkte Schaltlogik.",),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "idm_live": ServiceModule(
        key="idm_live",
        display_name="IDM Live",
        group=CONSUMERS,
        description="Liest IDM-Wärmepumpe über Modbus.",
        service="e3dc-idm-live",
        script="idm/idm_live.py",
        log_file="/var/www/html/logs/idm_live.log",
        alive_file="/var/www/html/ramdisk/waermepumpe.json",
        alive_max_age_s=120,
        config_keys=("luxtronik", "wp_type", "idm_ip"),
        install_notes=("Reines Live-/Monitoring-Modul für IDM, keine direkte Schaltlogik.",),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "stiebel_live": ServiceModule(
        key="stiebel_live",
        display_name="Stiebel ISG Live",
        group=CONSUMERS,
        description="Liest Stiebel-Eltron ISG/WPM read-only via Modbus und Prozessdaten.",
        service="e3dc-stiebel-live",
        script="stiebel/stiebel_live.py",
        log_file="/var/www/html/logs/stiebel_live.log",
        alive_file="/var/www/html/ramdisk/stiebel_isg.json",
        alive_max_age_s=120,
        config_keys=("luxtronik", "wp_type", "stiebel_isg_ip", "stiebel_isg_port"),
        install_notes=("Reines Live-/Monitoring-Modul für Stiebel ISG, keine SG-Ready-Schreiblogik.",),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "dimplex_live": ServiceModule(
        key="dimplex_live",
        display_name="Dimplex WPM Live",
        group=CONSUMERS,
        description="Liest Dimplex WPM Touch / NWPM read-only via Modbus.",
        service="e3dc-dimplex-live",
        script="dimplex/dimplex_live.py",
        log_file="/var/www/html/logs/dimplex_live.log",
        alive_file="/var/www/html/ramdisk/dimplex_wpm.json",
        alive_max_age_s=120,
        config_keys=(
            "luxtronik", "wp_type", "dimplex_ip", "dimplex_port", "dimplex_unit_id",
            "dimplex_wpm_software", "dimplex_sg_register", "dimplex_modbus_zero_based", "dimplex_outdoor_register",
            "dimplex_dhw_register", "dimplex_return_register", "dimplex_flow_register", "dimplex_return_setpoint_register",
            "dimplex_dhw_setpoint_register", "dimplex_heat_source_in_register", "dimplex_heat_source_out_register",
            "dimplex_cooling_flow_register", "dimplex_cooling_return_register", "dimplex_cooling_primary_return_register",
            "dimplex_operating_mode_register", "dimplex_heat_power_register", "dimplex_electric_power_register",
            "dimplex_heartbeat_out_register", "dimplex_cop_estimate",
            "dimplex_temp_scale", "dimplex_sg_heartbeat_s", "dimplex_allow_dark_green",
        ),
        install_notes=("Reines Live-/Monitoring-Modul für Dimplex WPM Touch/NWPM; SG-Ready-Schreiblogik bleibt im Energy Manager.",),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "heizstab": ServiceModule(
        key="heizstab",
        display_name="Heizstab / Shelly",
        group=CONSUMERS,
        description="Liest und bilanziert Heizstab-/Shelly-Verbrauch.",
        service="e3dc-heizstab",
        script="heizstab_manager.py",
        log_file="/var/www/html/logs/heizstab_manager.log",
        alive_file="/var/www/html/ramdisk/heizstab_data.json",
        alive_max_age_s=120,
        config_keys=("heizstab", "heizstab_ip", "shelly_heiz_ip", "shelly_3em_ip", "wp_type"),
        dependencies=("live",),
        install_warning=(
            "Das Heizstab/Shelly-Modul kann Verbraucher aktiv schalten oder Modbus-Sollwerte setzen. "
            "Als Heizstab/BWWP darf es parallel zu Luxtronik oder IDM laufen; Shelly Pro3EM als Ersatz-WP bleibt ein eigener WP-Typ."
        ),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "climate_live": ServiceModule(
        key="climate_live",
        display_name="Klimaanlage Live",
        group=CONSUMERS,
        description="Liest den Klimaanlagenverbrauch über einen eigenen Shelly-Zähler read-only.",
        service="e3dc-climate-live",
        script="climate_live.py",
        log_file="/var/www/html/logs/climate_live.log",
        alive_file="/var/www/html/ramdisk/climate_load.json",
        alive_max_age_s=120,
        config_keys=(
            "climate_enable", "climate_name", "climate_meter_ip", "climate_meter_type",
            "climate_meter_phase", "climate_min_power_w", "climate_poll_s",
            "climate_history_enable", "climate_history_interval_s", "climate_forecast_enable",
        ),
        install_notes=(
            "Reines Messmodul für Klimaanlagen oder ähnliche Zusatzverbraucher mit eigenem Energiezähler.",
            "Keine Cloud-Anbindung und keine Schaltbefehle; Toshiba oder Shelly werden nur ausgelesen.",
        ),
        optional=True,
        load_profile=LOAD_OBSERVE,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "climate_control": ServiceModule(
        key="climate_control",
        display_name="Klimaanlage Status",
        group=CONSUMERS,
        description="Liest den Toshiba-Cloud-Status read-only und sendet keine Klimaanlagen-Kommandos.",
        service="e3dc-climate-control",
        script="climate_control.py",
        log_file="/var/www/html/logs/climate_control.log",
        alive_file="/var/www/html/ramdisk/climate_control.json",
        alive_max_age_s=180,
        config_keys=(
            "climate_control_enable", "climate_control_poll_s",
            "climate_toshiba_cloud_enable", "climate_toshiba_username",
            "climate_toshiba_password", "climate_toshiba_device_ids",
        ),
        required_config_keys=(),
        dependencies=("climate_live",),
        install_notes=(
            "Der Dienst liest Toshiba read-only und schreibt climate_control.json.",
            "Leistung und Bilanz kommen weiterhin aus Klimaanlage Live/Shelly.",
        ),
        optional=True,
        load_profile=LOAD_OBSERVE,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "ha": ServiceModule(
        key="ha",
        display_name="HA Master/Slave",
        group=SYSTEM,
        description="Synchronisiert Master/Slave und hält den Slave im Standby ruhig.",
        service="e3dc-ha",
        script="ha_manager.py",
        log_file="/var/www/html/logs/ha_manager.log",
        alive_file="/var/www/html/ramdisk/ha_status.json",
        alive_max_age_s=120,
        config_keys=("ha_mode",),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "shadow": ServiceModule(
        key="shadow",
        display_name="Shadow-Vergleichsinstanz",
        group=SYSTEM,
        description="Liest die aktive Instanz read-only und berechnet lokale Vergleichsentscheidungen ohne Steuerbefehle oder Failover.",
        service="e3dc-shadow-sync",
        script="shadow_sync.py",
        log_file="/var/www/html/logs/shadow_sync.log",
        alive_file="/var/www/html/ramdisk/shadow_sync_status.json",
        alive_max_age_s=30,
        config_keys=(
            "ha_mode", "ha_peer_ip", "shadow_master_url", "shadow_master_ip",
            "shadow_sync_interval_s", "shadow_fetch_timeout_s", "shadow_snapshot_max_age_s",
        ),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "matter": ServiceModule(
        key="matter",
        display_name="Matter Bridge",
        group=INTEGRATIONS,
        description="Stellt drei lokale read-only Statusschalter für Apple Home, Google Home und andere Matter-Systeme bereit.",
        service="e3dc-matter-bridge",
        script="matter/matter_bridge.js",
        runner="npm",
        working_directory="matter",
        log_file="/var/www/html/logs/matter_bridge.log",
        alive_file="/var/www/html/ramdisk/matter_pairing.json",
        alive_max_age_s=300,
        config_keys=("matter_bridge",),
        required_config_keys=(),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "bluelink": ServiceModule(
        key="bluelink",
        display_name="Hyundai/Kia Bluelink",
        group=INTEGRATIONS,
        description="Liest Fahrzeug-SoC für Ladeplanung und Fahrzeugkacheln.",
        service="e3dc-bluelink",
        script="bluelink_client.py",
        log_file="/var/www/html/logs/bluelink_client.log",
        alive_file="/var/www/html/ramdisk/vehicles.json",
        alive_max_age_s=600,
        config_keys=("bluelink_refresh_token", "bluelink_vin", "bluelink_car_name", "bluelink_interval"),
        required_config_keys=(),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "notifier": ServiceModule(
        key="notifier",
        display_name="Zeitplanung und Langzeit-Archiv",
        group=CORE,
        description="Schreibt Langzeitdaten und sendet Ereignisse, Warnungen und Tagesmeldungen.",
        service="e3dc-notifier",
        script="notification_manager.py",
        log_file="/var/www/html/logs/notification_manager.log",
        alive_file="/var/www/html/logs/notification_manager.log",
        alive_max_age_s=3700,
        config_keys=("telegram_token", "telegram_chat_id", "telegram_device_name"),
        required_config_keys=(),
        optional=False,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
    "mqtt": ServiceModule(
        key="mqtt",
        display_name="MQTT Hub",
        group=INTEGRATIONS,
        description="Verteilt E3DC-Control Werte per MQTT.",
        service="e3dc-mqtt-hub",
        script="e3dc_mqtt_hub.py",
        log_file="/var/www/html/logs/e3dc_mqtt_hub.log",
        config_keys=("mqtt_hub_ip", "mqtt_hub_port", "mqtt_hub_topic"),
        required_config_keys=(),
        install_notes=("Installiert python3-paho-mqtt bei Bedarf, damit der Dienst direkt starten kann.",),
        optional=True,
        actions=READ_ACTIONS + SERVICE_ACTIONS,
    ),
}


def iter_modules(include_optional: bool = True) -> Iterable[ServiceModule]:
    for module in MODULES.values():
        if include_optional or not module.optional:
            yield module


def get_module(key: str) -> ServiceModule | None:
    return MODULES.get(str(key).strip())


def get_module_by_service(service: str) -> ServiceModule | None:
    normalized = str(service).strip()
    if normalized and not normalized.endswith(".service"):
        normalized = f"{normalized}.service"
    for module in MODULES.values():
        if module.service_unit == normalized:
            return module
    return None


def allowed_services() -> tuple[str, ...]:
    return tuple(
        module.service_unit
        for module in MODULES.values()
        if module.service_unit
    )


def catalog_as_dict() -> dict[str, dict]:
    return {key: module.public_dict() for key, module in MODULES.items()}


if __name__ == "__main__":
    import json

    print(json.dumps(catalog_as_dict(), indent=2, ensure_ascii=False))
