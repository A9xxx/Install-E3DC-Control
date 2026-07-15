#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E3DC-Control Failsafe / Emergency Release
Schaltet das System in den reinen Hardware-Modus von E3DC und openWB/go-e zurück.
Wird vom Watchdog im ha_manager.py bei Skript-Abstürzen / Hängern aufgerufen.
"""

import sys
import os
import json
import logging
from datetime import datetime

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, SCRIPT_DIR)
from rscp_client import RscpConnection, RscpTag, RscpType

LOG_DIR = "/var/www/html/logs"
os.makedirs(LOG_DIR, exist_ok=True)
logging.basicConfig(
    filename=f"{LOG_DIR}/failsafe.log",
    level=logging.INFO,
    format='%(asctime)s - FAILSAFE - %(levelname)s - %(message)s',
    datefmt='%d.%m %H:%M:%S'
)
log = logging.getLogger('Failsafe')

# Zusätzliches Logging in die Shell
sh = logging.StreamHandler(sys.stdout)
sh.setFormatter(logging.Formatter('%(asctime)s - WAIT - %(levelname)s - %(message)s'))
log.addHandler(sh)

def load_config():
    config = {}
    config_path = "/home/pi/E3DC-Control/e3dc.config.txt"
    try:
        if os.path.exists('/var/www/html/e3dc_paths.json'):
            with open('/var/www/html/e3dc_paths.json', 'r') as f:
                p_data = json.load(f)
                if 'install_path' in p_data:
                    config_path = os.path.join(p_data['install_path'], 'e3dc.config.txt')
        
        # Priority 1: V4 JSON
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.exists(v4_path):
            with open(v4_path, 'r') as f:
                data = json.load(f)
                config.update({k.lower(): v for k, v in data.items()})

        # Priority 2: Text Config (Fallback)
        if os.path.exists(config_path):
            with open(config_path, 'r', encoding='utf-8') as f:
                for line in f:
                    if line.strip().startswith('#') or '=' not in line: continue
                    k, v = line.split('=', 1)
                    config.setdefault(k.strip().lower(), v.strip())
        return config
    except Exception as e:
        log.error(f"Fehler beim Config laden: {e}")
        return config

def execute():
    log.info("=== EMERGENCY RELEASE GETRIGGERT ===")
    cfg = load_config()
    server_ip = str(cfg.get('server_ip') or '').strip()
    raw_server_port = cfg.get('server_port', 5033)
    e3dc_user = str(cfg.get('e3dc_user') or '')
    e3dc_password = str(cfg.get('e3dc_password') or '')
    aes_password = str(cfg.get('aes_password') or '')
    wb_native_type = str(cfg.get('wb_native_type', '')).lower().strip()
    conn = None

    try:
        server_port = int(raw_server_port)
    except (TypeError, ValueError):
        server_port = 0

    incomplete_fields = []
    if not server_ip:
        incomplete_fields.append('server_ip')
    if not 1 <= server_port <= 65535:
        incomplete_fields.append('server_port')
    if not e3dc_user:
        incomplete_fields.append('e3dc_user')
    if not e3dc_password:
        incomplete_fields.append('e3dc_password')
    if not aes_password:
        incomplete_fields.append('aes_password')

    if 'e3dc' in wb_native_type:
        log.error(
            "INCOMPLETE: Native E3DC-Wallbox ohne kanonisch definierten Python-RSCP-Vertrag; "
            "aus Hardwareschutzgründen wird kein Wallboxbefehl gesendet."
        )

    if incomplete_fields:
        log.error(
            "INCOMPLETE: RSCP-Notfallfreigabe ohne gültigen Verbindungs- oder Credentialkontext "
            f"({', '.join(incomplete_fields)}); es wird kein RSCP-Befehl gesendet."
        )
    else:
        try:
            conn = RscpConnection(server_ip, server_port, aes_password)
            conn.connect()
            conn.authenticate(e3dc_user, e3dc_password)
            log.info(f"RSCP Verbindung zu {server_ip} hergestellt.")

            # 1. E3DC Batterie Regelung lösen (AUTO-Modus) & Freigabe auf 20kW (mechanisches Limit E3DC)
            conn.request([
                {'tag': RscpTag.EMS_REQ_SET_POWER, 'type': RscpType.Container, 'value': [
                    {'tag': RscpTag.EMS_REQ_SET_POWER_MODE, 'type': RscpType.UChar8, 'value': 0},
                    {'tag': RscpTag.EMS_REQ_SET_POWER_VALUE, 'type': RscpType.Int32, 'value': 0},
                ]},
                {'tag': RscpTag.EMS_REQ_SET_MAX_CHARGE_POWER, 'type': RscpType.Int32, 'value': 20000},
                {'tag': RscpTag.EMS_REQ_SET_MAX_DISCHARGE_POWER, 'type': RscpType.Int32, 'value': 20000},
            ])
            log.info("SUCCESS: Batterie auf EMS_AUTO freigegeben! Lade-/Entladelimit gelöscht.")
        except Exception as e:
            log.error(f"FEHLER beim RSCP Notfall-Befehl: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception as close_error:
                    log.error(f"FEHLER beim RSCP Close: {close_error}")
        
    # Externe Wallboxen / openWB per openwb_cmd.php setzen (falls zutreffend)
    if 'openwb' in wb_native_type:
        try:
            import urllib.request
            # Schaltet openWB in Modus "pv_charging"
            url = "http://127.0.0.1/openwb_cmd.php?mode=pv_charging&cp=both"
            urllib.request.urlopen(url, timeout=3)
            log.info("SUCCESS: openWB auf PV-Laden umschalten (Fallback).")
        except Exception as e:
            log.error(f"Fehler beim Umschalten von openWB: {e}")

    log.info("=== EMERGENCY RELEASE BEENDET ===")

if __name__ == '__main__':
    execute()
