#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nebenwirkungsfreier Aktivierungsvertrag für optionale systemd-Dienste."""

from __future__ import annotations

from collections.abc import Mapping


FALSE_VALUES = {"", "0", "false", "off", "no", "nein", "none", "null", "disabled"}
MISSING_ADDRESS_VALUES = FALSE_VALUES | {"0.0.0.0"}
LOCAL_MQTT_ADDRESSES = {"127.0.0.1", "localhost", "::1"}


def _text(config: Mapping[str, object], key: str, default: str = "") -> str:
    value = config.get(key, default)
    if value is None:
        return ""
    return str(value).strip()


def _enabled(config: Mapping[str, object], key: str) -> bool:
    return _text(config, key).lower() in {"1", "true", "yes", "on"}


def _has_address(config: Mapping[str, object], key: str) -> bool:
    return _text(config, key).lower() not in MISSING_ADDRESS_VALUES


def _nonempty(config: Mapping[str, object], key: str) -> bool:
    return _text(config, key) != ""


def optional_service_configured(service: str, config: Mapping[str, object]) -> bool:
    """Prüft fachlich, ob ein optionaler Dienst laut Konfiguration gewollt ist."""

    name = str(service or "").strip().removesuffix(".service")
    if not isinstance(config, Mapping):
        return False

    wp_type = _text(config, "wp_type", "0")
    luxtronik_enabled = _enabled(config, "luxtronik")
    live_source_configured = (
        (wp_type == "0" and _has_address(config, "luxtronik_ip"))
        or (wp_type == "1" and _has_address(config, "idm_ip"))
        or (wp_type == "4" and _has_address(config, "stiebel_isg_ip"))
        or (wp_type == "5" and _has_address(config, "dimplex_ip"))
    )
    sg_ready_configured = _has_address(config, "shelly_sg_ip") or _has_address(
        config, "shelly_pause_ip"
    )

    if name == "e3dc-wallbox-manager":
        return (
            _enabled(config, "wb_native_enable")
            and not _enabled(config, "wallbox")
            and _text(config, "wbmode", "0") in {"", "0"}
        )
    if name == "energy_manager":
        return luxtronik_enabled and (live_source_configured or sg_ready_configured)
    if name == "e3dc-lux-live":
        return luxtronik_enabled and wp_type == "0" and _has_address(config, "luxtronik_ip")
    if name == "e3dc-idm-live":
        return luxtronik_enabled and wp_type == "1" and _has_address(config, "idm_ip")
    if name == "e3dc-stiebel-live":
        return luxtronik_enabled and wp_type == "4" and _has_address(config, "stiebel_isg_ip")
    if name == "e3dc-dimplex-live":
        return luxtronik_enabled and wp_type == "5" and _has_address(config, "dimplex_ip")
    if name == "e3dc-heizstab":
        return (
            _enabled(config, "heizstab")
            or _has_address(config, "heizstab_ip")
            or _has_address(config, "shelly_heiz_ip")
            or (
                luxtronik_enabled
                and wp_type == "3"
                and _has_address(config, "shelly_3em_ip")
            )
        )
    if name == "e3dc-climate-live":
        return _enabled(config, "climate_enable") and _has_address(config, "climate_meter_ip")
    if name == "e3dc-climate-control":
        return _enabled(config, "climate_control_enable")
    if name == "e3dc-matter-bridge":
        return _enabled(config, "matter_bridge")
    if name == "e3dc-bluelink":
        return _nonempty(config, "bluelink_refresh_token") or _nonempty(config, "bluelink_vin")
    if name == "e3dc-mqtt-hub":
        mqtt_ip = _text(config, "mqtt_hub_ip").lower()
        return (
            (_has_address(config, "mqtt_hub_ip") and mqtt_ip not in LOCAL_MQTT_ADDRESSES)
            or _nonempty(config, "mqtt_hub_sub_soc_topic")
            or _nonempty(config, "mqtt_hub_sub_soc_topic_2")
            or _nonempty(config, "wb_topic")
            or _nonempty(config, "wb2_topic")
        )
    return False


def preinstalled_optional_service_expected(
    service: str,
    config: Mapping[str, object],
) -> bool:
    """Bewahrt installierte Dienste, ohne reine Installationsdefaults zu aktivieren."""

    name = str(service or "").strip().removesuffix(".service")
    if not isinstance(config, Mapping):
        return False
    if name == "e3dc-mqtt-hub":
        return (
            _has_address(config, "mqtt_hub_ip")
            or _nonempty(config, "mqtt_hub_sub_soc_topic")
            or _nonempty(config, "mqtt_hub_sub_soc_topic_2")
            or _nonempty(config, "wb_topic")
            or _nonempty(config, "wb2_topic")
        )
    return optional_service_configured(name, config)


def configured_optional_services(config: Mapping[str, object]) -> tuple[str, ...]:
    """Liefert die kanonische, stabile Reihenfolge konfigurierter Zusatzdienste."""

    services = (
        "e3dc-wallbox-manager",
        "energy_manager",
        "e3dc-lux-live",
        "e3dc-idm-live",
        "e3dc-stiebel-live",
        "e3dc-dimplex-live",
        "e3dc-heizstab",
        "e3dc-climate-live",
        "e3dc-climate-control",
        "e3dc-matter-bridge",
        "e3dc-bluelink",
        "e3dc-mqtt-hub",
    )
    return tuple(service for service in services if optional_service_configured(service, config))
