#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Nebenwirkungsfreie Startprüfung für den ausdrücklich gewählten Bridge-Betrieb.

Die Prüfung ersetzt weder einen Erreichbarkeitstest noch eine laufende
Konfigurationskontrolle. Der unveränderte Host-Betrieb nutzt diesen Vertrag
nicht. Fehlermeldungen enthalten ausschließlich bekannte Schlüsselnamen.
"""

from __future__ import annotations

from collections.abc import Mapping
import argparse
import ipaddress
import json
import os
from pathlib import Path
import sys
from urllib.parse import urlsplit


# Bekannte Gerätezieladressen, keine beliebigen Konfigurationswerte oder URLs.
DEVICE_ADDRESS_KEYS = (
    "server_ip", "wb_native_ip", "wb_native_ip2", "wb_ip", "wb2_ip",
    "luxtronik_ip", "idm_ip", "stiebel_isg_ip", "dimplex_ip",
    "shelly_sg_ip", "shelly_pause_ip", "heizstab_ip", "shelly_heiz_ip",
    "shelly_3em_ip", "shelly_wb_ip", "shelly_wb2_ip", "climate_meter_ip",
    "shelly0v10v_ip", "direct_marketing_aux_inverter_shelly_ip",
)
EMPTY_ADDRESSES = {"", "0", "0.0.0.0", "none", "null", "false"}


def _text(config: Mapping[str, object], key: str, default: str = "") -> str:
    value = config.get(key, default)
    return "" if value is None else str(value).strip()


def _enabled(value: object) -> bool:
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _incompatible_address(value: str) -> bool:
    """Erkennt feste lokale Adressen ohne DNS-Abfragen oder Netzwerkzugriff."""
    raw = value.strip()
    try:
        address = ipaddress.ip_address(raw.strip("[]"))
    except ValueError:
        try:
            host = urlsplit(raw if "://" in raw else "//" + raw).hostname or ""
        except ValueError:
            return True
        host = host.rstrip(".").lower()
        if not host or host == "localhost" or host.endswith(".localhost"):
            return True
        # mDNS und schnittstellengebundene IPv6-Ziele sind keine gerouteten
        # LAN-Ziele des unterstützten Bridge-Pfads.
        if host.endswith(".local") or "%" in host:
            return True
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            return False  # Gewöhnliche DNS-Namen müssen extern geprüft werden.
    mapped = getattr(address, "ipv4_mapped", None)
    if mapped is not None:
        address = mapped
    return bool(
        address.is_loopback or address.is_link_local
        or address.is_unspecified or address.is_multicast
    )


def bridge_start_blockers(
    config: Mapping[str, object],
    *,
    environment: Mapping[str, str],
    optional_services: tuple[str, ...],
) -> tuple[str, ...]:
    """Prüft ausschließlich den bekannten, optionalen Bridge-Startvertrag."""
    mode = _text(environment, "E3DC_CONTAINER_NETWORK_MODE", "host").lower()
    if mode == "host":
        return ()
    if mode != "bridge":
        return ("E3DC_CONTAINER_NETWORK_MODE muss host oder bridge sein.",)

    blockers = []
    if _text(environment, "E3DC_WEB_BIND"):
        blockers.append("Bridge: E3DC_WEB_BIND muss leer bleiben; die Hostbindung erfolgt über E3DC_PUBLISH_BIND.")
    if _text(environment, "E3DC_WEB_PORT", "80") != "80":
        blockers.append("Bridge: E3DC_WEB_PORT muss 80 bleiben; den äußeren Port bestimmt E3DC_PUBLISH_PORT.")
    if "e3dc-matter-bridge" in optional_services:
        blockers.append("Bridge: matter_bridge benötigt den dokumentierten Host-Betrieb für Matter/mDNS.")

    for key in DEVICE_ADDRESS_KEYS:
        value = _text(config, key)
        if value.lower() not in EMPTY_ADDRESSES and _incompatible_address(value):
            blockers.append(f"Bridge: {key} muss ein geroutetes LAN-Ziel ohne Loopback, Link-Local oder mDNS sein.")
    if "e3dc-mqtt-hub" in optional_services:
        broker = _text(config, "mqtt_hub_ip", "127.0.0.1")
        if broker and broker != "0.0.0.0" and _incompatible_address(broker):
            blockers.append("Bridge: mqtt_hub_ip darf nicht auf Host-Loopback oder mDNS angewiesen sein.")

    if "e3dc-wallbox-manager" in optional_services:
        for number, key in ((1, "wb_native_type"), (2, "wb_native_type2")):
            if _text(config, key).lower() != "openwb":
                continue
            # Der HTTP-Secondary-Pfad darf per getsockname keine private
            # Container-IP melden. Explizite Primary-/Modbus-Rollen bleiben
            # laut Treiber auch bei automatischer Rollenerkennung verbindlich.
            primary = _enabled(config.get(f"wb{number}_openwb_primary_enable", config.get("wb_openwb_primary_enable", "0")))
            modbus = _enabled(config.get(f"wb{number}_openwb_modbus_secondary_enable", config.get("wb_openwb_modbus_secondary_enable", "0")))
            if modbus or primary:
                continue
            parent = _text(config, "wb_openwb_parent_ip", _text(config, "openwb_parent_ip"))
            try:
                parent_ip = ipaddress.IPv4Address(parent)
                valid_parent = not _incompatible_address(str(parent_ip))
            except ValueError:
                valid_parent = False
            if not valid_parent:
                blockers.append(f"Bridge: openWB {number} benötigt für HTTP-Secondary eine ausdrücklich konfigurierte, aus dem LAN erreichbare wb_openwb_parent_ip.")
    return tuple(blockers)


def main() -> int:
    parser = argparse.ArgumentParser(description="Docker-Bridge-Konfiguration ohne Netzwerkzugriff prüfen")
    parser.add_argument("--config", type=Path, required=True, help="Pfad zur e3dc_v4.json")
    args = parser.parse_args()
    try:
        try:
            from Installer.optional_service_contract import configured_optional_services
        except ModuleNotFoundError:
            from optional_service_contract import configured_optional_services
        config = json.loads(args.config.read_text(encoding="utf-8"))
        if not isinstance(config, dict):
            raise ValueError("Kein Konfigurationsobjekt")
    except (OSError, ValueError, ImportError):
        print("Bridge-Prüfung: Konfiguration oder Dienstvertrag konnte nicht gelesen werden.", file=sys.stderr)
        return 1
    environment = dict(os.environ, E3DC_CONTAINER_NETWORK_MODE="bridge")
    blockers = bridge_start_blockers(
        config, environment=environment, optional_services=configured_optional_services(config),
    )
    if blockers:
        print("\n".join(blockers), file=sys.stderr)
        return 1
    print("Bridge-Startprüfung bestanden; LAN-Erreichbarkeit und DNS separat prüfen.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
