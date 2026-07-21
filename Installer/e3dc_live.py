#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
e3dc_live.py  --  Nativer Python-RSCP-Live-Dienst für E3DC-Stromspeicher

Liest Live- und Tageswerte direkt via RSCP (TCP 5033) vom E3DC-System.
Im Daemon-Modus (--write --loops 0) schreibt er live_data_py.json atomar
in die RAM-Disk. get_live_json.php liest diese Datei.

Verzeichnisstruktur:
  <Release-Root>/Installer/  <- dieses Skript + rscp_client.py

Betrieb:
  Test (Einzel):    python3 e3dc_live.py
  Daemon (nonstop): python3 e3dc_live.py --write --loops 0 --interval 3
  N-mal wiederholen: python3 e3dc_live.py --loops 5 --interval 3
"""

import os
import sys
import json
import time
import argparse
import contextlib
import io
import logging
import tempfile
import math

LIVE_DATA_PATH = "/var/www/html/ramdisk/live_data_py.json"
LIVE_LAST_VALID_PATH = "/var/www/html/ramdisk/live_data_last_valid.json"
LIVE_DECISION_STABILITY_PATH = "/var/www/html/ramdisk/live_decision_stability.json"
LIVE_GRID_POWER_FILTER_STATE_PATH = "/var/www/html/ramdisk/live_grid_power_filter_state.json"
LIVE_GRID_PM_DELTA_STATE_PATH = "/var/www/html/ramdisk/live_grid_pm_delta_state.json"
LIVE_PLAUSIBILITY_LOG_DIR = "/var/www/html/logs"
LIVE_PLAUSIBILITY_LOG_PREFIX = "live_plausibility_glitches"
LIVE_PLAUSIBILITY_LOG_MAX_RECORDS = 480
LIVE_PLAUSIBILITY_LOG_MAX_BYTES = 1024 * 1024
LIVE_LAST_VALID_HEARTBEAT_S = 30.0
LIVE_GRID_POWER_FILTER_ALPHA = 0.03
PM_AUTO_PROBE_LAST_INDEX = 7
POWER_DECISION_SIGNALS = (
    "PV_Power",
    "Grid_Power",
    "Battery_Power",
    "Home_Power",
    "Wallbox_Power",
)
_LIVE_LAST_VALID_MEMORY = {}
_LIVE_LAST_VALID_WRITE_TS = {}

# rscp_client.py liegt im selben Verzeichnis
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

from rscp_client import (
    RscpConnection, RscpType,
    RscpTag, decode_wb_extern_data_alg,
    find_tag, find_tag_value, find_all_values,
)
from reserve import normalise_live_ep_reserve
try:
    from runtime_logging import EventLogLimiter, configure_service_logger
except ImportError:  # pragma: no cover - Paketimport
    from Installer.runtime_logging import EventLogLimiter, configure_service_logger
try:
    from rscp_acquisition import RscpAcquisitionSession
except ImportError:  # pragma: no cover - Paketimport
    from Installer.rscp_acquisition import RscpAcquisitionSession
try:
    from external_pv_topology import ExternalPvTopologyEvidenceTracker, read_external_pv_topology
except ImportError:  # pragma: no cover - Paketimport
    from Installer.external_pv_topology import ExternalPvTopologyEvidenceTracker, read_external_pv_topology

logger = configure_service_logger(
    "E3DCLive",
    log_path=os.path.join(LIVE_PLAUSIBILITY_LOG_DIR, "e3dc_live.log"),
    max_bytes=2 * 1024 * 1024,
    backup_count=3,
    quiet_interval_s=900.0,
)
_live_log_limiter = EventLogLimiter(min_interval_s=30.0, max_interval_s=3600.0)


def _safe_float_config_value(value, default):
    try:
        return float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return float(default)


def persistent_acquisition_sections(cfg):
    """Definiert Poll-Gruppen; regelrelevante Werte haben immer TTL 0."""

    history_poll_s = max(30.0, _safe_float_config_value(cfg.get("e3dc_live_history_poll_s", 60), 60))
    system_poll_s = max(
        300.0,
        _safe_float_config_value(
            cfg.get("e3dc_live_system_poll_s", cfg.get("e3dc_live_static_poll_s", 900)),
            900,
        ),
    )
    return [
        ("Power Snapshot", lambda conn: get_power_snapshot(conn, cfg), 0.0, True),
        ("PVI DC/AC", lambda conn: get_pvi(conn), 0.0, False),
        ("Batterie", lambda conn: get_bat(conn, cfg), 0.0, False),
        ("Wallbox", lambda conn: get_wb(conn, cfg), 0.0, False),
        # Dieser Block enthält neben Stammdaten auch aktive EMS-Leistungsgrenzen.
        # Er darf deshalb nicht als statischer Bereich gecacht werden.
        ("EMS Anlagendata", lambda conn: get_ems_config(conn), 0.0, False),
        ("DB-History (kWh)", lambda conn: get_db_history(conn), history_poll_s, False),
        ("System-Info", lambda conn: get_system_info(conn), system_poll_s, False),
    ]

# ---------------------------------------------------------------------------
# Konfiguration lesen
# ---------------------------------------------------------------------------

def _find_config():
    """
    Liest Konfiguration aus e3dc_v4.json (Single Source of Truth).
    """
    cfg = {}

    v4_path = "/var/www/html/data/e3dc_v4.json"
    if os.path.exists(v4_path):
        try:
            with open(v4_path, "r", encoding="utf-8") as f:
                v4_data = json.load(f)
                for k, v in v4_data.items():
                    if isinstance(v, dict):
                        for sub_k, sub_v in v.items():
                            cfg[sub_k.lower()] = str(sub_v)
                    else:
                        cfg[k.lower()] = str(v)
            print("  [Config] e3dc_v4.json geladen")
        except Exception as e:
            print(f"  [!] e3dc_v4.json Fehler: {e}")
    else:
        print("  [!] e3dc_v4.json nicht gefunden!")
    return cfg


def sanitize_for_json(value):
    """Liefert eine JSON-strikte Kopie, in der NaN/Infinity durch null ersetzt ist."""
    if isinstance(value, dict):
        return {k: sanitize_for_json(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize_for_json(v) for v in value]
    if isinstance(value, tuple):
        return [sanitize_for_json(v) for v in value]
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value


_ENERGY_BALANCE_COMPONENTS = (
    ("PV_Energy_kWh", "PV_Energy_Valid", "PV_Energy_Source", "supply", True),
    ("Grid_In_Energy_kWh", "Grid_In_Energy_Valid", "Grid_In_Energy_Source", "supply", True),
    ("Bat_Out_Energy_kWh", "Bat_Out_Energy_Valid", "Bat_Out_Energy_Source", "supply", True),
    ("Home_Energy_kWh", "Home_Energy_Valid", "Home_Energy_Source", "demand", True),
    ("Wallbox_Energy_kWh", "Wallbox_Energy_Valid", "Wallbox_Energy_Source", "demand", False),
    ("Grid_Out_Energy_kWh", "Grid_Out_Energy_Valid", "Grid_Out_Energy_Source", "demand", True),
    ("Bat_In_Energy_kWh", "Bat_In_Energy_Valid", "Bat_In_Energy_Source", "demand", True),
)


def calculate_energy_balance_metrics(data):
    """Berechnet reine Tagesenergiediagnose ohne fehlende Werte als Messnull zu erfinden."""

    components = {}
    invalid_required = []
    unavailable_optional = []
    for key, valid_key, source_key, side, required in _ENERGY_BALANCE_COMPONENTS:
        raw = data.get(key)
        valid_flag_present = valid_key in data
        reported_valid = bool(data.get(valid_key)) if valid_flag_present else True
        reason = ""
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            value = None
            reason = "missing" if raw is None else "invalid_type"
        else:
            value = float(raw)
            if not math.isfinite(value):
                value = None
                reason = "non_finite"
            elif value < 0.0:
                value = None
                reason = "negative_counter"
        valid = value is not None and reported_valid
        if value is not None and not reported_valid:
            reason = "reported_invalid"
        source = str(data.get(source_key) or ("numeric_legacy" if valid else reason or "invalid"))
        components[key] = {
            "value": value if valid else None,
            "valid": valid,
            "source": source,
            "reason": "valid" if valid else reason or "reported_invalid",
            "required": required,
            "side": side,
        }
        if not valid:
            if required:
                invalid_required.append(key)
            else:
                unavailable_optional.append(key)

    required_valid = not invalid_required
    optional_complete = not unavailable_optional
    partial_supply = None
    partial_demand = None
    partial_valid = False
    if required_valid:
        calculated_supply = sum(
            item["value"] for item in components.values()
            if item["side"] == "supply" and item["valid"]
        )
        calculated_demand = sum(
            item["value"] for item in components.values()
            if item["side"] == "demand" and item["valid"]
        )
        if optional_complete:
            supply = calculated_supply
            demand = calculated_demand
            loss_kwh = round(supply - demand, 3)
            loss_pct = round(loss_kwh / supply * 100, 1) if supply > 0 else 0.0
            completeness = "complete"
            source = "db_history_complete"
        else:
            # Die Teilsummen bleiben rein diagnostisch. Insbesondere darf eine
            # fehlende WB-Tagesenergie nicht als vermeintlicher Systemverlust
            # in die kanonische Bilanz gelangen.
            supply = None
            demand = None
            loss_kwh = None
            loss_pct = None
            partial_supply = calculated_supply
            partial_demand = calculated_demand
            partial_valid = True
            completeness = "partial_optional"
            source = "db_history_partial_without_optional_wallbox"
    else:
        supply = None
        demand = None
        loss_kwh = None
        loss_pct = None
        completeness = "degraded_required"
        source = "degraded_required_input"

    bat_in = components["Bat_In_Energy_kWh"]
    bat_out = components["Bat_Out_Energy_kWh"]
    bat_valid = bool(bat_in["valid"] and bat_out["valid"])
    bat_net_kwh = None
    bat_rte_pct = None
    if bat_valid:
        bi_e = bat_in["value"]
        bo_e = bat_out["value"]
        bat_net_kwh = round(bo_e - bi_e, 3)
        if bi_e > 0.5 and bo_e > 0.1 and bo_e <= bi_e * 1.05:
            bat_rte_pct = round(bo_e / bi_e * 100, 1)

    return {
        "sys_energy_supply_kwh": supply,
        "sys_energy_demand_kwh": demand,
        "sys_loss_kwh": loss_kwh,
        "sys_loss_pct": loss_pct,
        "sys_energy_balance_valid": bool(required_valid and optional_complete),
        "sys_energy_balance_completeness": completeness,
        "sys_energy_balance_source": source,
        "sys_energy_partial_supply_kwh": partial_supply,
        "sys_energy_partial_demand_kwh": partial_demand,
        "sys_energy_partial_valid": partial_valid,
        "sys_energy_balance_invalid_fields": invalid_required,
        "sys_energy_balance_optional_unavailable_fields": unavailable_optional,
        "sys_energy_balance_components": components,
        "bat_net_discharge_kwh": bat_net_kwh,
        "bat_rte_pct": bat_rte_pct,
        "bat_energy_metrics_valid": bat_valid,
        "bat_energy_metrics_source": "db_history_energy_components" if bat_valid else "degraded_required_input",
    }



# ---------------------------------------------------------------------------
# RSCP Tag-Konstanten (aus _rscpTags.py - korrekte Werte!)
# ---------------------------------------------------------------------------



# ---------------------------------------------------------------------------
# TLV-Hilfsfunktionen
# ---------------------------------------------------------------------------

def _nil(tag):
    return {'tag': tag, 'type': RscpType.Nil, 'value': None}

def _uint16(tag, val):
    return {'tag': tag, 'type': RscpType.Uint16, 'value': int(val)}

def _uint64(tag, val):
    return {'tag': tag, 'type': RscpType.Uint64, 'value': int(val)}

def _container(tag, children):
    return {'tag': tag, 'type': RscpType.Container, 'value': children}

def _fv(resp, tag, default=0.0):
    """Float-Wert aus Antwort lesen, None-sicher. Container werden ignoriert."""
    v = find_tag_value(resp, tag)
    if v is None or isinstance(v, (list, dict)):
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default

def _optional_float(resp, tag):
    """Liest einen Float-Wert mit Verfügbarkeitsstatus aus der Antwort."""
    item = find_tag(resp, tag)
    if item is None:
        return None, "rscp_missing"
    v = item.get('value')
    if v is None or isinstance(v, (list, dict)):
        return None, "rscp_invalid"
    try:
        return float(v), "rscp"
    except (TypeError, ValueError):
        return None, "rscp_invalid"

def _iv(resp, tag, default=0):
    """Int-Wert aus Antwort lesen, None-sicher. Container werden ignoriert."""
    v = find_tag_value(resp, tag)
    if v is None or isinstance(v, (list, dict)):
        return default
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _typed_int_tag(resp, tag):
    """Liefert RSCP-Integer mit expliziter Missing-/0-Unterscheidung."""

    item = find_tag(resp, tag)
    value = item.get("value") if isinstance(item, dict) else None
    valid = bool(
        isinstance(item, dict)
        and item.get("type") not in {RscpType.Nil, RscpType.Container, RscpType.Error}
        and isinstance(value, int)
        and not isinstance(value, bool)
    )
    return (int(value) if valid else None, valid)

def _float_in_container(item):
    """Extrahiert Float-Wert aus einem Container-Item."""
    if item and isinstance(item.get('value'), list):
        for sub in item['value']:
            if sub.get('type') in (RscpType.Float32, RscpType.Double64):
                return float(sub.get('value') or 0)
    elif item and item.get('type') in (RscpType.Float32, RscpType.Double64):
        return float(item.get('value') or 0)
    return 0.0

def _today_midnight_ts():
    from datetime import datetime
    now = datetime.now()
    return int(now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp())


# ---------------------------------------------------------------------------
# Datenabruf-Abschnitte
# ---------------------------------------------------------------------------

def _ems_live_request_items():
    return [
        _nil(RscpTag.EMS_REQ_POWER_PV),
        _nil(RscpTag.EMS_REQ_POWER_BAT),
        _nil(RscpTag.EMS_REQ_POWER_HOME),
        _nil(RscpTag.EMS_REQ_POWER_GRID),
        _nil(RscpTag.EMS_REQ_POWER_ADD),
        _nil(RscpTag.EMS_REQ_AUTARKY),
        _nil(RscpTag.EMS_REQ_SELF_CONSUMPTION),
        _nil(RscpTag.EMS_REQ_BAT_SOC),
        _nil(RscpTag.EMS_REQ_POWER_WB_ALL),
        _nil(RscpTag.EMS_REQ_STATUS),
        _nil(RscpTag.EMS_REQ_REMAINING_BAT_CHARGE_POWER),
        _nil(RscpTag.EMS_REQ_REMAINING_BAT_DISCHARGE_POWER),
        _nil(RscpTag.EMS_REQ_EMERGENCY_POWER_STATUS),  # 0=NOT_POSSIBLE, 1=ACTIVE, 2=NOT_ACTIVE, 3=NOT_AVAILABLE
    ]


def _decode_ems_live_response(resp):
    pv   = _iv(resp, RscpTag.EMS_POWER_PV)
    bat  = _iv(resp, RscpTag.EMS_POWER_BAT)
    home = _iv(resp, RscpTag.EMS_POWER_HOME)
    grid = _fv(resp, RscpTag.EMS_POWER_GRID)
    add_item = find_tag(resp, RscpTag.EMS_POWER_ADD)
    add_value = add_item.get("value") if isinstance(add_item, dict) else None
    add_valid = bool(
        isinstance(add_item, dict)
        and add_item.get("type") not in {RscpType.Nil, RscpType.Container, RscpType.Error}
        and not isinstance(add_value, bool)
        and isinstance(add_value, int)
    )
    add_raw = int(add_value) if add_valid else 0
    wb   = _iv(resp, RscpTag.EMS_POWER_WB_ALL)
    soc  = _fv(resp, RscpTag.EMS_BAT_SOC)
    remaining_charge_w, remaining_charge_valid = _typed_int_tag(
        resp,
        RscpTag.EMS_REMAINING_BAT_CHARGE_POWER,
    )
    remaining_discharge_w, remaining_discharge_valid = _typed_int_tag(
        resp,
        RscpTag.EMS_REMAINING_BAT_DISCHARGE_POWER,
    )

    # ADD_POWER: negativ = ext. PV-Erzeuger, positiv = Zusatzlast (Heizstab)
    ext_pv = 0 if add_valid else None
    heizstab = 0
    if add_valid and add_raw < 0:
        ext_pv = abs(add_raw)
        pv += ext_pv   # Gesamte PV-Leistung analog zu E3DC-Control "PV + Ext = Total"
    elif add_valid and add_raw > 0:
        heizstab = add_raw

    print(f"     PV={pv}W (davon Ext={ext_pv}W)  Bat={bat:+d}W  Home={home}W  Grid={grid:+.0f}W  WB={wb}W  Heizstab={heizstab}W  SOC={soc:.0f}%")
    print(f"     Autarkie={_fv(resp,RscpTag.EMS_AUTARKY):.1f}%  Eigenverbrauch={_fv(resp,RscpTag.EMS_SELF_CONSUMPTION):.1f}%  "
          "PV-Drosselung=nicht belegt  AC-Limit=nicht belegt")
    remaining_charge_text = f"{remaining_charge_w}W" if remaining_charge_valid else "nicht verfügbar"
    remaining_discharge_text = f"{remaining_discharge_w}W" if remaining_discharge_valid else "nicht verfügbar"
    print(f"     Verbl.Lade={remaining_charge_text}  Verbl.Entlade={remaining_discharge_text}")
    # EMS_STATUS Bitfeld-Dekodierung (aus E3DC RSCP-Doku)
    status_raw = find_tag_value(resp, RscpTag.EMS_STATUS)
    status_val = None if isinstance(status_raw, (list, dict)) else status_raw
    status_bits = {}
    if isinstance(status_val, int):
        sv = status_val
        status_bits = {
            "charge_locked":    bool(sv & (1 << 0)),  # Bit 0: Laden gesperrt
            "discharge_locked": bool(sv & (1 << 1)),  # Bit 1: Entladen gesperrt
            "emergency_ready":  bool(sv & (1 << 2)),  # Bit 2: Notstrom MÖGLICH (nicht aktiv!)
            "weather_charging": bool(sv & (1 << 3)),  # Bit 3: Wetterbas. Laden aktiv
            "derating_active":  bool(sv & (1 << 4)),  # Bit 4: Abregelung aktiv
            "charge_lock_time": bool(sv & (1 << 5)),  # Bit 5: Ladesperrzeit aktiv
            "discharge_lock_time": bool(sv & (1 << 6)), # Bit 6: Entladesperrzeit aktiv
        }
        active = [k for k, v in status_bits.items() if v]
        print(f"     EMS-Status={sv} ({', '.join(active) if active else 'Normalbetrieb'})")
    ep_status_raw = find_tag_value(resp, RscpTag.EMS_EMERGENCY_POWER_STATUS)
    ep_status = None if isinstance(ep_status_raw, (list, dict)) else ep_status_raw
    ep_names = {0: 'NOT_POSSIBLE', 1: 'ACTIVE', 2: 'NOT_ACTIVE', 3: 'NOT_AVAILABLE'}
    if ep_status is not None:
        print(f"     Notstrom-Status={ep_status} ({ep_names.get(ep_status, '?')})")
    return {
        "PV_Power": pv, "Ext_PV_Power": ext_pv,
        "Ext_PV_Power_Valid": add_valid,
        "Ext_PV_Power_Source": "e3dc_add_power" if add_valid else "rscp_missing_or_invalid",
        "Ext_PV_Power_Age_S": 0.0 if add_valid else None,
        "Battery_Power": bat, "Home_Power": home,
        "Grid_Power": grid, "Wallbox_Power": wb, "heizstab_power": heizstab,
        "SOC": soc,
        "autarky_pct": round(_fv(resp, RscpTag.EMS_AUTARKY), 1),
        "self_consumption_pct": round(_fv(resp, RscpTag.EMS_SELF_CONSUMPTION), 1),
        "pv_derating_active": None,
        "pv_derating_active_valid": False,
        "pv_derating_active_source": "unsupported_unverified",
        "ac_power_limit_w": None,
        "ac_power_limit_w_valid": False,
        "ac_power_limit_w_source": "unsupported_unverified",
        "remaining_charge_w": remaining_charge_w,
        "remaining_charge_w_valid": remaining_charge_valid,
        "remaining_charge_w_source": "rscp_ems_remaining_bat_charge_power" if remaining_charge_valid else "rscp_missing_or_invalid",
        "remaining_charge_w_age_s": 0.0 if remaining_charge_valid else None,
        "remaining_discharge_w": remaining_discharge_w,
        "remaining_discharge_w_valid": remaining_discharge_valid,
        "remaining_discharge_w_source": "rscp_ems_remaining_bat_discharge_power" if remaining_discharge_valid else "rscp_missing_or_invalid",
        "remaining_discharge_w_age_s": 0.0 if remaining_discharge_valid else None,
        "ems_status": status_val,
        "ems_emergency_power_status": ep_status,  # 0=NOT_POSSIBLE,1=ACTIVE,2=NOT_ACTIVE,3=N/A
        **{f"ems_{k}": v for k, v in status_bits.items()},
    }


def get_ems_live(conn):
    """EMS Live-Leistungen, SOC, Autarkie, Status."""
    print("  -> EMS Live-Leistungen ...")
    return _decode_ems_live_response(conn.request(_ems_live_request_items()))


def get_ems_config(conn):
    """EMS Anlagenparameter: kWp, Abregelung, Lade-Limits."""
    print("  -> EMS Anlagenparameter ...")
    resp = conn.request([
        _nil(RscpTag.EMS_REQ_INSTALLED_PEAK_POWER),
        _nil(RscpTag.EMS_REQ_DERATE_AT_PERCENT_VALUE),
        _nil(RscpTag.EMS_REQ_DERATE_AT_POWER_VALUE),
        # Korrekte GET-Tags! 0x01000101/102 sind SET-Tags -> liefern Fehlercode 7
        _nil(RscpTag.EMS_REQ_USED_CHARGE_LIMIT),
        _nil(RscpTag.EMS_REQ_USER_CHARGE_LIMIT),
        _nil(RscpTag.EMS_REQ_USED_DISCHARGE_LIMIT),
        _nil(RscpTag.EMS_REQ_BAT_CHARGE_LIMIT),
        _nil(RscpTag.EMS_REQ_GET_POWER_SETTINGS),
        _nil(RscpTag.EMS_REQ_BATTERY_TO_CAR_MODE),
        _nil(RscpTag.EMS_REQ_BATTERY_BEFORE_CAR_MODE),
        _nil(RscpTag.EMS_REQ_GET_WB_DISCHARGE_BAT_UNTIL),
        _nil(RscpTag.EMS_MANUAL_CHARGE_ACTIVE),
        _nil(RscpTag.SE_REQ_EP_RESERVE),
    ])
    kwp, peak_source = _optional_float(resp, RscpTag.EMS_INSTALLED_PEAK_POWER)
    peak_valid = kwp is not None and kwp > 0
    if not peak_valid:
        if kwp is not None and kwp <= 0:
            peak_source = "rscp_zero"
        kwp = None
    derat_frac = _fv(resp, RscpTag.EMS_DERATE_AT_PERCENT_VALUE)
    derat_pct  = round(derat_frac * 100, 1)
    derat_w    = _iv(resp, RscpTag.EMS_DERATE_AT_POWER_VALUE)
    used_ch    = _iv(resp, RscpTag.EMS_USED_CHARGE_LIMIT)
    user_ch    = _iv(resp, RscpTag.EMS_USER_CHARGE_LIMIT)
    used_dis   = abs(_iv(resp, RscpTag.EMS_USED_DISCHARGE_LIMIT))  # E3DC liefert negativ
    bat_ch     = _iv(resp, RscpTag.EMS_BAT_CHARGE_LIMIT)
    settings_read = find_tag(resp, RscpTag.EMS_GET_POWER_SETTINGS) is not None
    settings_limits_item = find_tag(resp, RscpTag.EMS_POWER_LIMITS_USED)
    settings_max_ch_item = find_tag(resp, RscpTag.EMS_MAX_CHARGE_POWER)
    settings_max_dis_item = find_tag(resp, RscpTag.EMS_MAX_DISCHARGE_POWER)
    settings_dis_start_item = find_tag(resp, RscpTag.EMS_DISCHARGE_START_POWER)
    settings_valid = bool(
        settings_read
        and isinstance(settings_limits_item, dict)
        and settings_limits_item.get("type") != RscpType.Error
        and isinstance(settings_limits_item.get("value"), bool)
        and all(
            isinstance(item, dict)
            and item.get("type") != RscpType.Error
            and isinstance(item.get("value"), int)
            and not isinstance(item.get("value"), bool)
            and int(item.get("value")) >= 0
            for item in (
                settings_max_ch_item,
                settings_max_dis_item,
                settings_dis_start_item,
            )
        )
    )
    settings_max_ch = _iv(resp, RscpTag.EMS_MAX_CHARGE_POWER)
    settings_max_dis = _iv(resp, RscpTag.EMS_MAX_DISCHARGE_POWER)
    settings_dis_start = _iv(resp, RscpTag.EMS_DISCHARGE_START_POWER)
    limits_on  = bool(find_tag_value(resp, RscpTag.EMS_POWER_LIMITS_USED))
    weather_ch = bool(find_tag_value(resp, RscpTag.EMS_WEATHER_REGULATED_CHARGE_ENABLED))
    manual_ch  = bool(find_tag_value(resp, RscpTag.EMS_MANUAL_CHARGE_ACTIVE))
    battery_to_car_mode = bool(find_tag_value(resp, RscpTag.EMS_BATTERY_TO_CAR_MODE))
    battery_before_car_mode = bool(find_tag_value(resp, RscpTag.EMS_BATTERY_BEFORE_CAR_MODE))
    wb_floor_tag = find_tag(resp, RscpTag.EMS_GET_WB_DISCHARGE_BAT_UNTIL)
    wb_floor_raw = wb_floor_tag.get("value") if wb_floor_tag else None
    wb_floor_type = wb_floor_tag.get("type") if wb_floor_tag else None
    wb_floor_valid = (
        wb_floor_type != RscpType.Error
        and isinstance(wb_floor_raw, int)
        and not isinstance(wb_floor_raw, bool)
        and 0 <= wb_floor_raw <= 100
    )
    wb_discharge_bat_until_soc = int(wb_floor_raw) if wb_floor_valid else None
    wb_floor_reason = "ok" if wb_floor_valid else (
        "missing" if wb_floor_tag is None else "invalid_type_or_range"
    )
    ep_reserve_raw, ep_reserve_source = _optional_float(resp, RscpTag.SE_PARAM_EP_RESERVE)
    ep_reserve_valid = ep_reserve_raw is not None and 0 <= ep_reserve_raw <= 100
    ep_reserve_pct = ep_reserve_raw if ep_reserve_valid else None
    ep_reserve_energy_wh = _fv(resp, RscpTag.SE_PARAM_EP_RESERVE_W, 0.0)
    ep_reserve_max_energy_wh = _fv(resp, RscpTag.SE_PARAM_EP_RESERVE_MAX_W, 0.0)

    peak_label = (
        f"{kwp:.0f}Wp ({kwp/1000:.1f}kWp)"
        if peak_valid
        else f"nicht verfügbar ({peak_source})"
    )
    ep_label = f"{ep_reserve_pct:.2f}%" if ep_reserve_valid else "nicht verfügbar"
    print(f"     Installiert={peak_label}  Abregelung={derat_pct}% ({derat_w}W)  Notstrom={ep_label}")
    if ep_reserve_energy_wh > 0 or ep_reserve_max_energy_wh > 0:
        print(
            f"     EP-Reserve: raw={ep_reserve_pct:.2f}%  "
            f"Energie={ep_reserve_energy_wh:.0f}Wh  Max={ep_reserve_max_energy_wh:.0f}Wh"
        )
    print(f"     Lade-Limit: genutzt={used_ch}W  Nutzer={user_ch}W  Bat={bat_ch}W")
    print(f"     Entlade-Limit={used_dis}W  ExtLimits={limits_on}  Wetter={weather_ch}  Manuell={manual_ch}")
    print(
        f"     E3DC-Wallbox: Batterie->Auto={battery_to_car_mode}  "
        f"Batterie-vor-Auto={battery_before_car_mode}  "
        "WB-Zuordnung-erzwingen=nicht belegt  "
        f"Akku-bis-SoC={wb_discharge_bat_until_soc if wb_floor_valid else 'unbekannt'}"
    )
    if settings_read:
        print(
            f"     Power-Settings: MaxLaden={settings_max_ch}W  "
            f"MaxEntladen={settings_max_dis}W  EntladeStart={settings_dis_start}W  Aktiv={limits_on}"
        )

    return {
        "installed_peak_power_w": int(kwp) if peak_valid else None,
        "installed_peak_power_kwp": round(kwp / 1000, 2) if peak_valid else None,
        "installed_peak_power_valid": bool(peak_valid),
        "installed_peak_power_source": peak_source,
        "derate_at_percent": derat_pct,
        "derate_at_power_w": derat_w,
        "ep_reserve_pct": round(ep_reserve_pct, 2) if ep_reserve_valid else None,
        "ep_reserve_raw_pct": round(ep_reserve_pct, 2) if ep_reserve_valid else None,
        "ep_reserve_valid": bool(ep_reserve_valid),
        "ep_reserve_source": ep_reserve_source,
        "ep_reserve_energy_wh": round(ep_reserve_energy_wh, 1),
        "ep_reserve_max_energy_wh": round(ep_reserve_max_energy_wh, 1),
        "e3dc_battery_to_car_mode": bool(battery_to_car_mode),
        "e3dc_battery_before_car_mode": bool(battery_before_car_mode),
        "e3dc_wb_enforce_power_assignment": None,
        "e3dc_wb_enforce_power_assignment_valid": False,
        "e3dc_wb_enforce_power_assignment_source": "unsupported_unverified",
        "e3dc_wb_discharge_bat_until_soc": wb_discharge_bat_until_soc,
        "e3dc_wb_discharge_bat_until_soc_valid": bool(wb_floor_valid),
        "e3dc_wb_discharge_bat_until_soc_source": "rscp_provisional_read_only",
        "e3dc_wb_discharge_bat_until_soc_reason": wb_floor_reason,
        "used_charge_limit_w": used_ch,
        "user_charge_limit_w": user_ch,
        "bat_charge_limit_w": bat_ch,
        "used_discharge_limit_w": used_dis,
        "power_limits_active": limits_on,
        "ems_power_settings_read": settings_read,
        "ems_power_settings_valid": settings_valid,
        "ems_max_charge_power_w": settings_max_ch,
        "ems_max_discharge_power_w": settings_max_dis,
        "ems_discharge_start_power_w": settings_dis_start,
        "weather_regulated_charge": weather_ch,
        "manual_charge_active": manual_ch,
    }


def get_db_history(conn):
    """Tageserträge über DB-History (Uint64-Zeitstempel, KEIN Timestamp-Typ!)."""
    print("  -> DB-History Tagesbilanz ...")
    midnight = _today_midnight_ts()
    print(f"     Mitternacht-TS={midnight}  ({time.strftime('%Y-%m-%d %H:%M', time.localtime(midnight))})")

    # Versuch 1: INTERVAL=86400 (Tages-Bucket)
    resp = conn.request([_container(RscpTag.DB_REQ_HISTORY_DATA_DAY, [
        _uint64(RscpTag.DB_REQ_HISTORY_TIME_START,    midnight),
        _uint64(RscpTag.DB_REQ_HISTORY_TIME_INTERVAL, 86400),
        _uint64(RscpTag.DB_REQ_HISTORY_TIME_SPAN,     86400),
    ])])

    hist = find_tag(resp, RscpTag.DB_HISTORY_DATA_DAY)
    summ = None
    if hist and isinstance(hist.get('value'), list):
        tags_found = [f"0x{item.get('tag',0):08X}" for item in hist['value']]
        print(f"     DB-Antwort-Tags: {tags_found}")
        summ = find_tag(hist['value'], RscpTag.DB_SUM_CONTAINER)
        if summ is None:
            print("     [Hinweis] Kein SUM-Container - summiere alle Buckets")
            agg = {}
            for item in hist['value']:
                if isinstance(item.get('value'), list):
                    for sub in item['value']:
                        t = sub.get('tag', 0)
                        v = sub.get('value')
                        if v is not None and not isinstance(v, list):
                            try:
                                agg[t] = agg.get(t, 0) + int(v)
                            except (TypeError, ValueError):
                                pass
            if agg:
                summ = {'tag': RscpTag.DB_SUM_CONTAINER, 'type': 0x0E,
                        'value': [{'tag': t, 'type': 0x09, 'value': v}
                                  for t, v in agg.items()]}
    else:
        # Debug: zeige was stattdessen in der Antwort ist
        top_tags = [f"0x{item.get('tag',0):08X}" for item in resp] if resp else []
        print(f"     [!] DB_HISTORY_DATA_DAY nicht gefunden. Antwort-Tags: {top_tags}")

    def _read_kwh(tag):
        # DB-Werte sind in Wh (nicht Ws)! Daher /1000 für kWh.
        # Beleg: vi=7200 Wh = 7.2 kWh (plausibel) vs 7200 Ws = 2 Wh (unrealistisch)
        if summ and isinstance(summ.get('value'), list):
            item = find_tag(summ['value'], tag)
            if not isinstance(item, dict) or item.get('value') is None:
                return None, False, "rscp_db_history_missing"
            try:
                vf = float(item.get('value'))
            except (TypeError, ValueError):
                return None, False, "rscp_db_history_invalid_type"
            if not math.isfinite(vf):
                return None, False, "rscp_db_history_non_finite"
            if int(vf) == 0xFFFFFFFF:
                return None, False, "rscp_db_history_invalid_sentinel"
            if vf < 0.0:
                return None, False, "rscp_db_history_negative_counter"
            return round(vf / 1000.0, 3), True, "rscp_db_history_day"
        return None, False, "rscp_db_history_unavailable"

    def _legacy_optional_kwh(tag):
        value, valid, _source = _read_kwh(tag)
        return value if valid else 0.0

    energy_values = {}
    for key, tag in (
        ("PV_Energy", RscpTag.DB_DC_POWER),
        ("Grid_In_Energy", RscpTag.DB_GRID_POWER_OUT),
        ("Grid_Out_Energy", RscpTag.DB_GRID_POWER_IN),
        ("Bat_In_Energy", RscpTag.DB_BAT_POWER_IN),
        ("Bat_Out_Energy", RscpTag.DB_BAT_POWER_OUT),
        ("Home_Energy", RscpTag.DB_CONSUMPTION),
    ):
        energy_values[key] = _read_kwh(tag)

    pv_kwh, pv_valid, pv_source = energy_values["PV_Energy"]
    # ACHTUNG: E3DC benennt Tags aus Haus-Perspektive:
    # DB_GRID_POWER_IN  = Energie ins Haus = Netzbezug (Grid_In)
    # DB_GRID_POWER_OUT = Energie ins Grid = Einspeisung (Grid_Out)
    gi_kwh, gi_valid, gi_source = energy_values["Grid_In_Energy"]
    go_kwh, go_valid, go_source = energy_values["Grid_Out_Energy"]
    bi_kwh, bi_valid, bi_source = energy_values["Bat_In_Energy"]
    bo_kwh, bo_valid, bo_source = energy_values["Bat_Out_Energy"]
    hm_kwh, hm_valid, hm_source = energy_values["Home_Energy"]
    wb_kwh = None
    pm0_kwh = _legacy_optional_kwh(RscpTag.DB_PM_0_POWER)
    pm1_kwh = _legacy_optional_kwh(RscpTag.DB_PM_1_POWER)

    # Externe Erzeuger zum PV-Feld addieren
    ext_pv_kwh = round(pm0_kwh + pm1_kwh, 3)
    if ext_pv_kwh > 0 and pv_valid:
        pv_kwh = round(pv_kwh + ext_pv_kwh, 3)

    ok = "[OK]" if summ else "[!] Leer"
    def _display(value):
        return f"{value}kWh" if value is not None else "n/a"

    print(f"     PV={_display(pv_kwh)} (inkl. Ext={ext_pv_kwh}kWh)  "
          f"GridIn={_display(gi_kwh)}  GridOut={_display(go_kwh)}  "
          f"BatIn={_display(bi_kwh)}  BatOut={_display(bo_kwh)}  {ok}")
    print(f"     Home={_display(hm_kwh)}  Wallbox=nicht belegt")

    # -------------------------------------------------------------------------
    # PLAUSIBILITÄTSPRÜFUNGEN: DB-Historie prüfen (erkennt Tag-Tausch und Vorzeichenfehler)
    # Konventionen (Live-Werte aus EMS_POWER_*):
    #   Grid_Power  < 0 = Einspeisung    -> go_kwh sollte > gi_kwh sein
    #   Grid_Power  > 0 = Netzbezug      -> gi_kwh sollte > go_kwh sein
    #   Battery_Power > 0 = Entladung    -> bo_kwh sollte > bi_kwh sein
    #   Battery_Power < 0 = Ladung       -> bi_kwh sollte > bo_kwh sein
    # -------------------------------------------------------------------------
    try:
        # Aktuelle Live-Werte für den Kontextvergleich lesen (aus der Ramdisk)
        import os, json as _json
        _live_path = "/var/www/html/ramdisk/live_data_py.json"
        _live = {}
        if os.path.exists(_live_path):
            try:
                with open(_live_path) as _f:
                    _live = _json.load(_f)
            except Exception:
                pass

        _grid_live = float(_live.get("Grid_Power", 0) or 0)
        _bat_live  = float(_live.get("Battery_Power", 0) or 0)
        _home_live = float(_live.get("Home_Power", 0) or 0)

        # --- GRID: Tag-Tausch-Erkennung ---
        # Wenn den ganzen Tag stark eingespeist wird, muss go_kwh deutlich größer sein
        if gi_valid and go_valid and _grid_live < -500 and gi_kwh > 0.5 and gi_kwh > go_kwh * 2:
            print(f"[!] PLAUSIBEL-CHECK Grid: Einspeisung aktiv ({_grid_live:.0f}W) aber "
                  f"gi_kwh={gi_kwh:.3f} > go_kwh={go_kwh:.3f} -- möglicher Tag-Tausch!")
        # Umgekehrt: Netzbezug aktiv aber go_kwh wächst stärker
        if gi_valid and go_valid and _grid_live > 500 and go_kwh > 0.5 and go_kwh > gi_kwh * 2:
            print(f"[!] PLAUSIBEL-CHECK Grid: Netzbezug aktiv ({_grid_live:.0f}W) aber "
                  f"go_kwh={go_kwh:.3f} > gi_kwh={gi_kwh:.3f} -- möglicher Tag-Tausch!")

        # --- BATTERIE: Tag-Tausch-Erkennung ---
        # Battery_Power > 0 = Entladung -> bo_kwh sollte größer sein
        if bi_valid and bo_valid and _bat_live > 500 and bi_kwh > 0.5 and bi_kwh > bo_kwh * 2:
            print(f"[!] PLAUSIBEL-CHECK Bat: Entladung aktiv ({_bat_live:.0f}W) aber "
                  f"bi_kwh={bi_kwh:.3f} > bo_kwh={bo_kwh:.3f} -- möglicher Tag-Tausch!")
        # Battery_Power < 0 = Ladung -> bi_kwh sollte größer sein
        if bi_valid and bo_valid and _bat_live < -500 and bo_kwh > 0.5 and bo_kwh > bi_kwh * 2:
            print(f"[!] PLAUSIBEL-CHECK Bat: Ladung aktiv ({_bat_live:.0f}W) aber "
                  f"bo_kwh={bo_kwh:.3f} > bi_kwh={bi_kwh:.3f} -- möglicher Tag-Tausch!")

        # --- HAUSVERBRAUCH: Negativer Wert (z.B. Balkonkraftwerk) ---
        # DB_CONSUMPTION kann negativ werden wenn ein Balkonkraftwerk mehr erzeugt
        # als der Haushalt verbraucht UND E3DC das als "Rückspeisung ins Haus" sieht.
        if hm_valid and hm_kwh < -0.1:
            print(f"[!] PLAUSIBEL-CHECK Home: hm_kwh={hm_kwh:.3f}kWh negativ! "
                  f"Balkonkraftwerk oder externer Erzeuger könnte DB_CONSUMPTION verfälschen.")

        # --- PV: Niemals negativ ---
        if pv_valid and pv_kwh < -0.05:
            print(f"[!] PLAUSIBEL-CHECK PV: pv_kwh={pv_kwh:.3f}kWh negativ -- DB-Fehler oder falscher Tag!")

        # --- ENERGIEBILANZ: Grobe Konsistenzprüfung ---
        # PV + gi_kwh + bo_kwh ~ hm_kwh + go_kwh + bi_kwh + wb_kwh (Energieerhaltung)
        _balance_preview = calculate_energy_balance_metrics({
            "PV_Energy_kWh": pv_kwh,
            "PV_Energy_Valid": pv_valid,
            "PV_Energy_Source": pv_source,
            "Grid_In_Energy_kWh": gi_kwh,
            "Grid_In_Energy_Valid": gi_valid,
            "Grid_In_Energy_Source": gi_source,
            "Grid_Out_Energy_kWh": go_kwh,
            "Grid_Out_Energy_Valid": go_valid,
            "Grid_Out_Energy_Source": go_source,
            "Bat_In_Energy_kWh": bi_kwh,
            "Bat_In_Energy_Valid": bi_valid,
            "Bat_In_Energy_Source": bi_source,
            "Bat_Out_Energy_kWh": bo_kwh,
            "Bat_Out_Energy_Valid": bo_valid,
            "Bat_Out_Energy_Source": bo_source,
            "Home_Energy_kWh": hm_kwh,
            "Home_Energy_Valid": hm_valid,
            "Home_Energy_Source": hm_source,
            "Wallbox_Energy_kWh": wb_kwh,
            "Wallbox_Energy_Valid": False,
            "Wallbox_Energy_Source": "unsupported_unverified",
        })
        if _balance_preview["sys_energy_balance_valid"] and pv_kwh > 0.5 and hm_kwh > 0:
            _erzeugung = _balance_preview["sys_energy_supply_kwh"]
            _verbrauch = _balance_preview["sys_energy_demand_kwh"]
            _bilanz_err = abs(_erzeugung - _verbrauch)
            if _bilanz_err > max(3.0, _erzeugung * 0.30):  # >30% Abweichung oder >3kWh
                print(f"[!] PLAUSIBEL-CHECK Bilanz: Erzeugung={_erzeugung:.2f}kWh "
                      f"!= Verbrauch={_verbrauch:.2f}kWh (Delta={_bilanz_err:.2f}kWh) "
                      f"-- DB-Daten inkonsistent (normal bei Tagesanfang).")

    except Exception as _e:
        print(f"[!] PLAUSIBEL-CHECK Fehler (nicht kritisch): {_e}")
    # -------------------------------------------------------------------------

    return {
        "PV_Energy_kWh": pv_kwh,
        "PV_Energy_Valid": pv_valid,
        "PV_Energy_Source": pv_source,
        "Ext_PV_Energy_kWh": ext_pv_kwh,
        "Grid_In_Energy_kWh": gi_kwh, "Grid_Out_Energy_kWh": go_kwh,
        "Grid_In_Energy_Valid": gi_valid, "Grid_Out_Energy_Valid": go_valid,
        "Grid_In_Energy_Source": gi_source, "Grid_Out_Energy_Source": go_source,
        "Bat_In_Energy_kWh": bi_kwh, "Bat_Out_Energy_kWh": bo_kwh,
        "Bat_In_Energy_Valid": bi_valid, "Bat_Out_Energy_Valid": bo_valid,
        "Bat_In_Energy_Source": bi_source, "Bat_Out_Energy_Source": bo_source,
        "Home_Energy_kWh": hm_kwh,
        "Home_Energy_Valid": hm_valid,
        "Home_Energy_Source": hm_source,
        "Wallbox_Energy_kWh": wb_kwh,
        "Wallbox_Energy_Valid": False,
        "Wallbox_Energy_Source": "unsupported_unverified",
        "_db_midnight_ts": midnight,
        "_db_sum_ok": summ is not None,
    }



def get_pvi(conn):
    """
    PVI Wechselrichter: DC-Strings + AC-Phasen.
    Korrekte Tags: 0x020DC... (REQ) / 0x028DC... (RESP)
                   0x020AC... (REQ) / 0x028AC... (RESP)

    Messrauschen: Der E3DC-PVI-Wechselrichter hat eine Leistungsauflösung
    von ca. 6W. Leistungswerte innerhalb +/-PVI_NOISE_THRESHOLD_W werden
    auf Null geclamprt, da sie physikalisch bedeutungslos sind (inaktive Phasen
    oder Ruhestand liefern -1W bis +2W Rauschen).
    """
    PVI_NOISE_THRESHOLD_W = 5  # W - Rauschschwelle (PVI-Auflösung ~6 W)

    print("  -> PVI DC-Strings + AC-Phasen ...")
    result = {}

    # Zuerst: Anzahl DC-Strings und AC-Phasen ermitteln
    info_req = [_container(RscpTag.PVI_REQ_DATA, [
        _uint16(RscpTag.PVI_INDEX, 0),
        _nil(RscpTag.PVI_REQ_DC_MAX_STRING_COUNT),
        _nil(RscpTag.PVI_REQ_AC_MAX_PHASE_COUNT),
        _nil(RscpTag.PVI_REQ_TEMPERATURE),
    ])]
    info_resp = conn.request(info_req)
    pvi_data = find_tag(info_resp, RscpTag.PVI_DATA)
    pd = pvi_data['value'] if pvi_data and isinstance(pvi_data.get('value'), list) else []

    dc_count = _iv({'items': pd}, 0) or 2  # Fallback 2 Strings
    dc_count_tag = find_tag(pd, RscpTag.PVI_DC_MAX_STRING_COUNT)
    if dc_count_tag:
        dc_count = int(dc_count_tag.get('value') or 2)

    ac_count_tag = find_tag(pd, RscpTag.PVI_AC_MAX_PHASE_COUNT)
    ac_count = int(ac_count_tag.get('value') or 3) if ac_count_tag else 3

    temp_tag = find_tag(pd, RscpTag.PVI_TEMPERATURE)
    result["pvi_temperature_c"] = round(_float_in_container(temp_tag), 1)
    result["pvi_frequency_hz"] = None
    result["pvi_frequency_valid"] = False
    result["pvi_frequency_source"] = "unsupported_unverified"

    print(f"     PVI: {dc_count} DC-Strings, {ac_count} AC-Phasen, T={result['pvi_temperature_c']:.1f}C, f=nicht belegt")

    # DC-Strings einzeln abfragen
    for s in range(dc_count):
        dc_req = [_container(RscpTag.PVI_REQ_DATA, [
            _uint16(RscpTag.PVI_INDEX, 0),
            _uint16(RscpTag.PVI_REQ_DC_POWER,   s),
            _uint16(RscpTag.PVI_REQ_DC_VOLTAGE,  s),
            _uint16(RscpTag.PVI_REQ_DC_CURRENT,  s),
            _uint16(RscpTag.PVI_REQ_DC_MAX_POWER, s),
        ])]
        dc_resp = conn.request(dc_req)
        dpd = find_tag(dc_resp, RscpTag.PVI_DATA)
        dv = dpd['value'] if dpd and isinstance(dpd.get('value'), list) else []

        def _dc_float(tag):
            item = find_tag(dv, tag)
            if item and isinstance(item.get('value'), list):
                for sub in item['value']:
                    if sub.get('type') in (RscpType.Float32, RscpType.Double64):
                        return round(float(sub.get('value') or 0), 2)
            return 0.0

        w = _dc_float(RscpTag.PVI_DC_POWER)
        v = _dc_float(RscpTag.PVI_DC_VOLTAGE)
        a = _dc_float(RscpTag.PVI_DC_CURRENT)
        mp = _dc_float(RscpTag.PVI_DC_MAX_POWER)
        # Rausch-Clamping: DC-Leistung unter Schwelle -> 0 (inaktiver String)
        w = 0.0 if abs(w) < PVI_NOISE_THRESHOLD_W else w
        print(f"     DC{s}: {w}W / {v}V / {a}A  (Max={mp}W)")
        result[f"dc{s}_w"] = w
        result[f"dc{s}_v"] = v
        result[f"dc{s}_a"] = a
        result[f"dc{s}_max_w"] = mp

    # AC-Phasen einzeln abfragen
    for p in range(ac_count):
        ac_req = [_container(RscpTag.PVI_REQ_DATA, [
            _uint16(RscpTag.PVI_INDEX, 0),
            _uint16(RscpTag.PVI_REQ_AC_POWER,   p),
            _uint16(RscpTag.PVI_REQ_AC_VOLTAGE,  p),
            _uint16(RscpTag.PVI_REQ_AC_CURRENT,  p),
        ])]
        ac_resp = conn.request(ac_req)
        apd = find_tag(ac_resp, RscpTag.PVI_DATA)
        av = apd['value'] if apd and isinstance(apd.get('value'), list) else []

        def _ac_float(tag):
            item = find_tag(av, tag)
            if item and isinstance(item.get('value'), list):
                for sub in item['value']:
                    if sub.get('type') in (RscpType.Float32, RscpType.Double64):
                        return round(float(sub.get('value') or 0), 2)
            return 0.0

        w = _ac_float(RscpTag.PVI_AC_POWER)
        v = _ac_float(RscpTag.PVI_AC_VOLTAGE)
        a = _ac_float(RscpTag.PVI_AC_CURRENT)
        # Rausch-Clamping: AC-Leistung unter Schwelle -> 0 (inaktive Phase / Messrauschen)
        # Hintergrund: Eine inaktive Phase (DC-Kopplung -> nur Phase 0 aktiv)
        # liefert typisch -1W bis +2W. Schwelle 5W sicher unter echter Last.
        w = 0.0 if abs(w) < PVI_NOISE_THRESHOLD_W else w
        a = 0.0 if abs(w) < PVI_NOISE_THRESHOLD_W else a  # Strom nur wenn Leistung real
        print(f"     AC{p}: {w}W / {v}V / {a}A")
        result[f"ac{p}_w"] = w
        result[f"ac{p}_v"] = v
        result[f"ac{p}_a"] = a

    return result


def _cfg_int(cfg, key, default=0):
    try:
        return int(float(str((cfg or {}).get(key, default) or default).replace(",", ".")))
    except Exception:
        return int(default)


def _pm_candidates(cfg=None):
    raw_configured_index = _cfg_int(cfg, "wurzelzaehler", 0)
    configured_index = raw_configured_index if 0 <= raw_configured_index <= 65535 else 0
    candidates = []
    if configured_index > 0:
        candidates.append(configured_index)
    for idx in range(0, PM_AUTO_PROBE_LAST_INDEX + 1):
        if idx not in candidates:
            candidates.append(idx)
    return raw_configured_index, configured_index, candidates


def _pm_request_item(idx):
    return _container(RscpTag.PM_REQ_DATA, [
        _uint16(RscpTag.PM_INDEX, idx),
        _nil(RscpTag.PM_REQ_POWER_L1),
        _nil(RscpTag.PM_REQ_POWER_L2),
        _nil(RscpTag.PM_REQ_POWER_L3),
    ])


def _decode_pm_response(resp, idx, configured_index):
    pm_data = find_tag(resp, RscpTag.PM_DATA)
    if not (pm_data and isinstance(pm_data.get('value'), list)):
        return {}
    pd = pm_data['value']
    p1 = round(float(find_tag_value(pd, RscpTag.PM_POWER_L1) or 0), 1)
    p2 = round(float(find_tag_value(pd, RscpTag.PM_POWER_L2) or 0), 1)
    p3 = round(float(find_tag_value(pd, RscpTag.PM_POWER_L3) or 0), 1)
    available = abs(p1) + abs(p2) + abs(p3) > 0.1
    phase_values = (p1, p2, p3) if available else (None, None, None)
    return {
        "grid_p1": phase_values[0],
        "grid_p2": phase_values[1],
        "grid_p3": phase_values[2],
        "grid_pm_index": idx,
        "grid_pm_configured_index": configured_index,
        "grid_pm_sum_w": round(p1 + p2 + p3, 1) if available else None,
        "grid_pm_available": available,
        "grid_pm_source": "configured_wurzelzaehler" if idx == configured_index and configured_index > 0 else f"pm_index_{idx}",
    }


def _print_pm_summary(best):
    if not best:
        return
    if not best.get("grid_pm_available"):
        print(
            f"     PM-Index {best['grid_pm_index']} ohne plausible Phasenwerte "
            "(Diagnose inaktiv; Regelung nutzt EMS Grid_Power)"
        )
        return
    print(
        f"     PM-Index {best['grid_pm_index']} "
        f"L1={best['grid_p1']:+.0f}W  L2={best['grid_p2']:+.0f}W  L3={best['grid_p3']:+.0f}W "
        "(Diagnose; Regelung nutzt EMS Grid_Power)"
    )


def get_pm(conn, cfg=None):
    """Netzphasen L1/L2/L3 als Diagnose; die Regelung nutzt EMS Grid_Power."""
    print("  -> PM Netzphasen ...")
    raw_configured_index, configured_index, candidates = _pm_candidates(cfg)
    if raw_configured_index > 65535:
        print(
            f"     Wurzelzähler {raw_configured_index} ist eine Seriennummer, kein PM-Index; "
            f"nutze Auto-Suche 0..{PM_AUTO_PROBE_LAST_INDEX}."
        )
    best = {}
    for idx in candidates:
        try:
            resp = conn.request([_pm_request_item(idx)])
        except Exception as exc:
            print(f"     PM-Index {idx} fehlgeschlagen: {exc}")
            continue
        candidate = _decode_pm_response(resp, idx, configured_index)
        if not candidate:
            continue
        best = candidate
        if best["grid_pm_available"] or idx == candidates[-1]:
            break
    _print_pm_summary(best)
    return best


def get_power_snapshot(conn, cfg=None):
    """Liest EMS-Liveleistungen und Wurzelzählerphasen möglichst in einer RSCP-Anfrage."""
    print("  -> EMS Live-Leistungen + PM Netzphasen ...")
    raw_configured_index, configured_index, candidates = _pm_candidates(cfg)
    if raw_configured_index > 65535:
        print(
            f"     Wurzelzähler {raw_configured_index} ist eine Seriennummer, kein PM-Index; "
            f"nutze Auto-Suche 0..{PM_AUTO_PROBE_LAST_INDEX}."
        )
    last_response = None
    best_pm = {}
    best_response = None
    for idx in candidates:
        try:
            resp = conn.request(_ems_live_request_items() + [_pm_request_item(idx)])
        except Exception as exc:
            print(f"     Gemeinsamer Snapshot PM-Index {idx} fehlgeschlagen: {exc}")
            continue
        last_response = resp
        pm = _decode_pm_response(resp, idx, configured_index)
        if pm:
            best_pm = pm
            best_response = resp
        if pm and (pm["grid_pm_available"] or idx == candidates[-1]):
            data = _decode_ems_live_response(resp)
            _print_pm_summary(pm)
            data.update(pm)
            data["power_snapshot_source"] = "joint_ems_pm"
            return data

    if best_pm and best_response is not None:
        data = _decode_ems_live_response(best_response)
        _print_pm_summary(best_pm)
        data.update(best_pm)
        data["power_snapshot_source"] = "joint_ems_pm_unavailable"
        return data

    if last_response is not None:
        data = _decode_ems_live_response(last_response)
        try:
            pm = get_pm(conn, cfg)
        except Exception as exc:
            print(f"     Separater PM-Fallback fehlgeschlagen: {exc}")
            pm = {}
        data.update(pm)
        data["power_snapshot_source"] = "joint_ems_separate_pm" if pm else "joint_ems_only"
        return data

    data = get_ems_live(conn)
    data.update(get_pm(conn, cfg))
    data["power_snapshot_source"] = "fallback_separate"
    return data


def _normalise_bat_capacity(cap_kwh, fcc_kwh, dcb_count=1, specified_wh=None):
    """Liefert systemweite Batteriekapazitäten aus gemischten E3DC-BAT-Antworten."""
    cap = round(float(cap_kwh or 0.0), 2)
    fcc = round(float(fcc_kwh or 0.0), 2)
    packs = max(1, int(dcb_count or 1))
    source = "bat_capacity"
    specified_kwh = 0.0
    try:
        specified_kwh = round(float(specified_wh or 0.0) / 1000.0, 2)
    except Exception:
        specified_kwh = 0.0

    if packs > 1 and 0.1 < cap < 5.0:
        cap = round(cap * packs, 2)
        source = "pack_capacity_scaled"
    if packs > 1 and 0.1 < fcc < 5.0:
        fcc = round(fcc * packs, 2)

    if specified_kwh > 0.0 and cap > specified_kwh * 1.15:
        cap = round(specified_kwh * 0.9, 2)
        source = "specification_clamped"
    if specified_kwh > 0.0 and fcc > specified_kwh * 1.15:
        fcc = specified_kwh

    if specified_kwh > 0.0 and (cap <= 0.0 or cap < specified_kwh * 0.5):
        cap = round(specified_kwh * 0.9, 2)
        source = "specification_fallback"
    if specified_kwh > 0.0 and (fcc <= 0.0 or fcc < specified_kwh * 0.5):
        fcc = specified_kwh
    return cap, fcc, source, specified_kwh


def _normalise_bat_capacity_from_rscp(cap_raw, fcc_raw, dcb_count=1, specified_wh=None):
    """Normalisiert BAT_USABLE_CAPACITY/BAT_FCC aus E3DC-Schrankantworten.

    Neuere H20-/S10X-Systeme melden diese Tags in Ah; ältere Antworten wurden
    als 0,1-kWh-Packwerte beobachtet. Die Ah-Deutung hat Vorrang, wenn sie
    gegenüber BAT_SPECIFICATION plausibel ist; andernfalls greift der Altpfad.
    """
    packs = max(1, int(dcb_count or 1))
    specified_kwh = 0.0
    try:
        specified_kwh = round(float(specified_wh or 0.0) / 1000.0, 2)
    except Exception:
        specified_kwh = 0.0

    def _sf(value):
        try:
            value = float(value or 0.0)
            return value if math.isfinite(value) else 0.0
        except Exception:
            return 0.0

    def _plausible(value_kwh, raw_value, low=0.45, high=1.15):
        if value_kwh <= 0.1:
            return False
        if specified_kwh <= 0.1:
            return _sf(raw_value) >= 100.0
        return specified_kwh * low <= value_kwh <= specified_kwh * high

    nominal_voltage_v = 51.8
    cap_ah_kwh = _sf(cap_raw) * nominal_voltage_v / 1000.0
    fcc_ah_kwh = _sf(fcc_raw) * nominal_voltage_v / 1000.0
    if _plausible(cap_ah_kwh, cap_raw) or _plausible(fcc_ah_kwh, fcc_raw):
        cap = round(cap_ah_kwh, 2) if _plausible(cap_ah_kwh, cap_raw) else 0.0
        fcc = round(fcc_ah_kwh, 2) if _plausible(fcc_ah_kwh, fcc_raw) else 0.0
        if specified_kwh > 0.0 and (cap <= 0.0 or cap < specified_kwh * 0.45):
            cap = round(specified_kwh * 0.9, 2)
        if specified_kwh > 0.0 and (fcc <= 0.0 or fcc < specified_kwh * 0.45):
            fcc = specified_kwh
        return cap, fcc, "ah_capacity", specified_kwh

    return _normalise_bat_capacity(
        round(_sf(cap_raw) / 10.0, 2),
        round(_sf(fcc_raw) / 10.0, 2),
        packs,
        specified_wh,
    )


def _add_bat_total_fields(result):
    """Ergänzt explizite systemweite Kapazitätssummen, ohne Batterieschränke auszublenden."""
    usable = 0.0
    full = 0.0
    specified = 0.0
    dcb_count = 0
    sources = []
    active = 0
    for prefix in ("bat", "bat1", "bat2", "bat3"):
        cab_dcb = int(result.get(f"{prefix}_dcb_count") or 0)
        cab_usable = float(result.get(f"{prefix}_usable_kwh") or 0.0)
        cab_full = float(result.get(f"{prefix}_full_cap_kwh") or 0.0)
        cab_specified = float(result.get(f"{prefix}_specified_kwh") or 0.0)
        cab_v = float(result.get(f"{prefix}_v") or 0.0)
        if cab_dcb <= 0 and cab_usable <= 0.1 and cab_full <= 0.1 and cab_specified <= 0.1 and cab_v <= 5.0:
            continue
        active += 1
        dcb_count += max(0, cab_dcb)
        usable += max(0.0, cab_usable)
        full += max(0.0, cab_full)
        specified += max(0.0, cab_specified)
        source = str(result.get(f"{prefix}_capacity_source") or "").strip()
        if source:
            sources.append(f"{prefix}:{source}")

    if active <= 0:
        return result

    result["bat_total_cabinet_count"] = active
    if dcb_count > 0:
        result["bat_total_dcb_count"] = dcb_count
    if usable > 0.1:
        result["bat_total_usable_kwh"] = round(usable, 2)
    if full > 0.1:
        result["bat_total_full_cap_kwh"] = round(full, 2)
    if specified > 0.1:
        result["bat_total_specified_kwh"] = round(specified, 2)
    if sources:
        result["bat_total_capacity_source"] = "+".join(sources)
    return result


def get_bat(conn, cfg=None):
    """Batterie: Spannung, Strom, maximaler Ladestrom und Kapazität (bis 4 Schränke)."""
    print("  -> Batterie Spannung/Strom ...")
    result = {}
    for cab_idx, pfx in [(0, "bat"), (1, "bat1"), (2, "bat2"), (3, "bat3")]:
        resp = conn.request([_container(RscpTag.BAT_REQ_DATA, [
            _uint16(RscpTag.BAT_INDEX, cab_idx),
            _nil(RscpTag.BAT_REQ_RSOC),              # 0x03000001 -> liefert SOC
            _nil(RscpTag.BAT_REQ_MODULE_VOLTAGE),              # 0x03000002 -> liefert Spannung V
            _nil(RscpTag.BAT_REQ_CURRENT),         # 0x03000003 -> liefert Strom A
            _nil(RscpTag.BAT_REQ_MAX_CHARGE_CURRENT),
            _nil(RscpTag.BAT_REQ_DCB_COUNT),
            _nil(RscpTag.BAT_REQ_USABLE_CAPACITY),
            _nil(RscpTag.BAT_REQ_FCC),
            _nil(RscpTag.BAT_REQ_CHARGE_CYCLES),        # Ladezyklen
            _nil(RscpTag.BAT_REQ_EOD_VOLTAGE),          # Entladeschlussspannung
            _nil(RscpTag.BAT_REQ_SPECIFICATION),
        ])])
        bat_data = find_tag(resp, RscpTag.BAT_DATA)
        if bat_data and isinstance(bat_data.get('value'), list):
            bd = bat_data['value']
            # BAT_VOLTAGE = 0x03800002 (Spannung in V, korrekt!)
            # BAT_CURRENT = 0x03800003 (Strom in A, korrekt - war vorher 0x03800002!)
            # BAT_SOC_OWN = 0x03800001 (eigener SOC des Batterie-BMS in %)
            soc_own = round(float(find_tag_value(bd, RscpTag.BAT_RSOC) or 0), 1)
            v     = round(float(find_tag_value(bd, RscpTag.BAT_MODULE_VOLTAGE) or 0), 2)
            a     = round(float(find_tag_value(bd, RscpTag.BAT_CURRENT) or 0), 2)
            max_a = round(float(find_tag_value(bd, RscpTag.BAT_MAX_CHARGE_CURRENT) or 0), 1)
            dcb_count = int(find_tag_value(bd, RscpTag.BAT_DCB_COUNT) or 0)
            # BAT_USABLE_CAPACITY/BAT_FCC können je nach Firmware Ah oder
            # alte 0,1-kWh-Packwerte liefern; die Normalisierung prüft beides.
            cap_value = find_tag_value(bd, RscpTag.BAT_USABLE_CAPACITY)
            fcc_value = find_tag_value(bd, RscpTag.BAT_FCC)
            cap_raw = float(cap_value) if isinstance(cap_value, (int, float)) and not isinstance(cap_value, bool) else None
            fcc_raw = float(fcc_value) if isinstance(fcc_value, (int, float)) and not isinstance(fcc_value, bool) else None
            specified_wh = None
            bat_spec = find_tag(bd, RscpTag.BAT_SPECIFICATION)
            if bat_spec and isinstance(bat_spec.get('value'), list):
                specified_wh = find_tag_value(bat_spec['value'], RscpTag.BAT_SPECIFIED_CAPACITY)
            cap_kwh, fcc_kwh, cap_source, specified_kwh = _normalise_bat_capacity_from_rscp(
                cap_raw, fcc_raw, dcb_count, specified_wh
            )
            local_capacity = None
            try:
                candidate = float((cfg or {}).get("speichergroesse"))
                if 0.1 <= candidate <= 1000.0:
                    local_capacity = candidate
            except (TypeError, ValueError):
                pass
            telemetry_valid = any(value > 0.1 for value in (cap_kwh, fcc_kwh, specified_kwh))
            if not telemetry_valid and local_capacity is not None and cab_idx == 0:
                cap_kwh = local_capacity
                fcc_kwh = local_capacity
                cap_source = "validated_local_config_fallback"
            cycles = _iv(bd, RscpTag.BAT_CHARGE_CYCLES)
            result[f"{pfx}_soc_own"]      = soc_own      # BMS-eigener SOC
            result[f"{pfx}_v"]            = v
            result[f"{pfx}_a"]            = a
            result[f"{pfx}_max_charge_a"] = max_a
            result[f"{pfx}_dcb_count"]    = dcb_count
            result[f"{pfx}_usable_kwh"]   = cap_kwh
            result[f"{pfx}_full_cap_kwh"] = fcc_kwh
            result[f"{pfx}_capacity_source"] = cap_source
            result[f"{pfx}_capacity_valid"] = bool(telemetry_valid)
            if specified_kwh > 0:
                result[f"{pfx}_specified_kwh"] = specified_kwh
            result[f"{pfx}_charge_cycles"] = cycles
            if v > 0 or soc_own > 0:
                print(f"     Cabinet {cab_idx}: SOC={soc_own}%  {v}V / {a}A  "
                      f"Packs={dcb_count or '?'}  MaxLade={max_a}A  "
                      f"Kap={cap_kwh}kWh / FCC={fcc_kwh}kWh ({cap_source})")
        else:
            result[f"{pfx}_v"] = 0.0; result[f"{pfx}_a"] = 0.0
    _add_bat_total_fields(result)
    if result.get("bat_total_usable_kwh"):
        print(
            f"     Batterie gesamt: {result.get('bat_total_usable_kwh')}kWh nutzbar / "
            f"{result.get('bat_total_full_cap_kwh', 0)}kWh FCC "
            f"({result.get('bat_total_dcb_count', '?')} DCB)"
        )
    return result


def get_wb(conn, cfg):
    """Wallbox Phasen + Verbindungsstatus."""
    print("  -> Wallbox Phasen ...")
    resp = conn.request([_container(RscpTag.WB_REQ_DATA, [
        _uint16(RscpTag.WB_INDEX, 0),
        _nil(RscpTag.WB_REQ_PM_POWER_L1), _nil(RscpTag.WB_REQ_PM_POWER_L2), _nil(RscpTag.WB_REQ_PM_POWER_L3),
        _nil(RscpTag.WB_REQ_EXTERN_DATA_ALG),
    ])])
    wb_data = find_tag(resp, RscpTag.WB_DATA)
    result  = {"wb_p1": 0.0, "wb_p2": 0.0, "wb_p3": 0.0,
               "wb_plugged": None, "wb_locked": None, "wb_charging": None,
               "wb_status_valid": False, "wb_status_source": "rscp_wb_extern_data_alg",
               "wb_status_reason": "missing", "wb_status_age_s": 0.0,
               "wb_mode": int(cfg.get("wbmode", "4"))}
    if wb_data and isinstance(wb_data.get('value'), list):
        wd = wb_data['value']
        result["wb_p1"]      = round(float(find_tag_value(wd, RscpTag.WB_PM_POWER_L1) or 0), 1)
        result["wb_p2"]      = round(float(find_tag_value(wd, RscpTag.WB_PM_POWER_L2) or 0), 1)
        result["wb_p3"]      = round(float(find_tag_value(wd, RscpTag.WB_PM_POWER_L3) or 0), 1)
        alg = find_tag(wd, RscpTag.WB_EXTERN_DATA_ALG)
        decoded = decode_wb_extern_data_alg(alg, age_s=0.0)
        result.update({
            "wb_plugged": decoded["plugged"],
            "wb_locked": decoded["plug_locked"],
            "wb_charging": decoded["charging"],
            "wb_status_valid": decoded["valid"],
            "wb_status_source": decoded["source"],
            "wb_status_reason": decoded["reason"],
            "wb_status_age_s": decoded["age_s"],
        })
    print(f"     L1={result['wb_p1']}W  L2={result['wb_p2']}W  L3={result['wb_p3']}W  Gesteckt={result['wb_plugged']}  Verriegelt={result['wb_locked']}  Lädt={result['wb_charging']}")
    return result


def get_system_info(conn):
    """Seriennummer und SW-Version."""
    print("  -> System-Info ...")
    resp = conn.request([
        _nil(RscpTag.INFO_REQ_SERIAL_NUMBER),
        _nil(RscpTag.INFO_REQ_SW_RELEASE),
    ])
    sn  = find_tag_value(resp, RscpTag.INFO_SERIAL_NUMBER) or "N/A"
    rel = find_tag_value(resp, RscpTag.INFO_SW_RELEASE)    or "N/A"
    print(f"     Seriennummer={sn}  SW={rel}")
    return {"serial_number": sn, "sw_release": rel}


# ---------------------------------------------------------------------------
# kWh - Retter (Tracking von Wechselrichter- und Abregelungsverlust-Rettungen)
# ---------------------------------------------------------------------------

class KwhRetterState:
    FILE_PATH = "/var/www/html/data/kwh_retter.json"
    def __init__(self):
        self.total_derating_wsec = 0.0
        self.total_inverter_wsec = 0.0
        self.today_derating_wsec = 0.0
        self.today_inverter_wsec = 0.0
        import datetime
        self.current_date = datetime.datetime.now().strftime("%Y-%m-%d")
        self.start_date = self.current_date
        self.last_ts = 0
        self.load()

    def load(self):
        import os, json
        if os.path.exists(self.FILE_PATH):
            try:
                with open(self.FILE_PATH, 'r') as f:
                    d = json.load(f)
                    self.total_derating_wsec = d.get('total_derating_wsec', 0.0)
                    self.total_inverter_wsec = d.get('total_inverter_wsec', 0.0)
                    self.today_derating_wsec = d.get('today_derating_wsec', 0.0)
                    self.today_inverter_wsec = d.get('today_inverter_wsec', 0.0)
                    self.current_date = d.get('current_date', self.current_date)
                    self.start_date = d.get('start_date', self.current_date)
            except: pass

    def save(self):
        import os, json
        tmp = self.FILE_PATH + ".tmp"
        try:
            with open(tmp, 'w') as f:
                json.dump({
                    "total_derating_wsec": self.total_derating_wsec,
                    "total_inverter_wsec": self.total_inverter_wsec,
                    "today_derating_wsec": self.today_derating_wsec,
                    "today_inverter_wsec": self.today_inverter_wsec,
                    "current_date": self.current_date,
                    "start_date": self.start_date
                }, f)
            os.replace(tmp, self.FILE_PATH)
            os.chmod(self.FILE_PATH, 0o664)
        except: pass

    @staticmethod
    def split_saved_power(pv_power, derate_limit_w, inverter_limit_w):
        """
        Teilt die PV-Leistung oberhalb der Anlagenlimits in disjunkte Anteile.

        - Abregelung: Bereich oberhalb der Einspeise-/Derate-Grenze.
        - AC-Limit: Bereich oberhalb einer vom E3DC gemeldeten WR-Grenze.

        Beide Werte dürfen sich nicht überlappen, sonst zählt der Gesamtwert
        die gleiche Energie doppelt. Ein AC-Limit von 0 bedeutet "unbekannt"
        und wird deshalb nicht geraten.
        """
        try:
            pv = max(0.0, float(pv_power or 0))
            derate = max(0.0, float(derate_limit_w or 0))
            inverter = max(0.0, float(inverter_limit_w or 0))
        except (TypeError, ValueError):
            return 0.0, 0.0

        derating_w = 0.0
        inverter_w = 0.0

        if inverter > 0 and pv > inverter:
            inverter_w = pv - inverter

        if derate > 0 and pv > derate:
            if inverter > derate:
                derating_w = max(0.0, min(pv, inverter) - derate)
            elif inverter > 0:
                derating_w = 0.0
            else:
                derating_w = pv - derate

        return derating_w, inverter_w

    def add_sample(self, pv_power, derate_limit_w, inverter_limit_w):
        import time, datetime
        now_ts = time.monotonic()
        if self.last_ts == 0:
            self.last_ts = now_ts
            return

        dt_sec = now_ts - self.last_ts
        self.last_ts = now_ts
        if dt_sec > 300 or dt_sec <= 0:
            return

        today = datetime.datetime.now().strftime("%Y-%m-%d")
        if today != self.current_date:
            self.today_derating_wsec = 0.0
            self.today_inverter_wsec = 0.0
            self.current_date = today

        changed = False
        derating_w, inverter_w = self.split_saved_power(pv_power, derate_limit_w, inverter_limit_w)
        if derating_w > 0:
            self.today_derating_wsec += derating_w * dt_sec
            self.total_derating_wsec += derating_w * dt_sec
            changed = True
        if inverter_w > 0:
            self.today_inverter_wsec += inverter_w * dt_sec
            self.total_inverter_wsec += inverter_w * dt_sec
            changed = True

        if changed or dt_sec > 60: # periodisch speichern
            self.save()

    def get_stats(self):
        return {
            "saved_derating_today_kwh": round(self.today_derating_wsec / 3600000.0, 3),
            "saved_inverter_today_kwh": round(self.today_inverter_wsec / 3600000.0, 3),
            "saved_derating_total_kwh": round(self.total_derating_wsec / 3600000.0, 3),
            "saved_inverter_total_kwh": round(self.total_inverter_wsec / 3600000.0, 3),
            "retter_start_date": self.start_date
        }


def apply_pv_zero_glitch_filter(clean, live_path="/var/www/html/ramdisk/live_data_py.json"):
    """
    Kurze RSCP/PVI-Aussetzer abfangen.

    Einige E3DC melden für wenige Sekunden PV=0 W und PVI AC/DC=0 W, obwohl
    die Stringspannung weiter hoch ist. Ohne Filter interpretiert die Regelung
    das als echten PV-Einbruch und verlässt die Ladekurve. Wir halten nur den
    letzten plausiblen PV-Wert und nur für ein enges Zeitfenster.
    """
    try:
        raw_pv = float(clean.get("PV_Power", 0) or 0)
        if raw_pv > 50:
            return clean

        dc_v_high = any(float(clean.get(f"dc{i}_v", 0) or 0) > 150 for i in range(4))
        ac_power_sum = sum(abs(float(clean.get(f"ac{i}_w", 0) or 0)) for i in range(3))
        grid_w = float(clean.get("Grid_Power", 0) or 0)
        home_w = float(clean.get("Home_Power", 0) or 0)

        if not (dc_v_high and ac_power_sum < 80 and grid_w > 150 and home_w > 150):
            return clean
        if not os.path.exists(live_path):
            return clean

        with open(live_path, "r", encoding="utf-8") as f:
            prev = json.load(f)

        prev_ts = float(prev.get("_ts", 0) or 0)
        prev_pv = float(prev.get("PV_Power", 0) or 0)
        if time.time() - prev_ts > 120 or prev_pv < 500:
            return clean

        clean["PV_Power"] = int(prev_pv)
        clean["_pv_glitch_filtered"] = True
        clean["_pv_glitch_raw_w"] = int(raw_pv)
        clean["_pv_glitch_reason"] = "PV=0 bei hoher Stringspannung, Vorwert kurz gehalten"
        print(f"  [PV-Filter] PV=0W verworfen, halte kurz {prev_pv:.0f}W (DC-Spannung hoch)")
    except Exception as e:
        print(f"  [PV-Filter] Fehler: {e}")
    return clean


def _finite_float(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    if not math.isfinite(result):
        return float(default)
    return result


def _finite_optional_float(value):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(result):
        return None
    return result


def _rscp_error_class(errors):
    if not errors:
        return ""
    text = " | ".join(str(item) for item in errors if str(item or "").strip()).lower()
    if not text:
        return ""
    if (
        "errno 113" in text
        or "no route to host" in text
        or "errno 101" in text
        or "network is unreachable" in text
    ):
        return "rscp_network_unreachable"
    if "connection refused" in text or "errno 111" in text:
        return "rscp_connection_refused"
    if "timed out" in text or "timeout" in text:
        return "rscp_timeout"
    return "rscp_partial_errors"


def _finite_bool(value, default=True):
    if isinstance(value, bool):
        return value
    if value is None:
        return bool(default)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on", "ok", "valid", "available"):
        return True
    if text in ("0", "false", "no", "off", "invalid", "unavailable"):
        return False
    return bool(default)


def _cfg_lookup(cfg, key, default):
    if not isinstance(cfg, dict):
        return default
    return cfg.get(str(key).lower(), cfg.get(str(key), default))


def _cfg_number(cfg, key, default):
    return _finite_float(_cfg_lookup(cfg, key, default), default)


def _cfg_flag(cfg, key, default=False):
    return _finite_bool(_cfg_lookup(cfg, key, default), default)


def _grid_pm_delta_status(delta_w, cfg=None, *, state_path=None, now_s=None):
    """Klassifiziert die EMS-/PM-Abweichung als rein diagnostisch oder regelwirksam."""
    cfg_present = isinstance(cfg, dict)
    soft_threshold_w = max(0.0, _cfg_number(cfg, "live_grid_pm_delta_soft_threshold_w", 250.0))
    hard_threshold_w = max(
        soft_threshold_w,
        _cfg_number(cfg, "live_grid_pm_delta_hard_threshold_w", 2000.0),
    )
    persist_count = max(1, int(round(_cfg_number(cfg, "live_grid_pm_delta_persist_count", 2.0))))
    persist_window_s = max(0.0, _cfg_number(cfg, "live_grid_pm_delta_persist_window_s", 20.0))
    debounce_enabled = bool(cfg_present and _cfg_flag(cfg, "live_grid_pm_delta_debounce_enable", True))
    now = _finite_float(now_s if now_s is not None else time.time(), time.time())
    delta_abs_w = abs(float(delta_w))
    diagnostic_high = delta_abs_w > soft_threshold_w

    observations = []
    if debounce_enabled and state_path:
        state = _read_json_file(state_path)
        raw_observations = state.get("observations", []) if isinstance(state, dict) else []
        if isinstance(raw_observations, list):
            for item in raw_observations:
                if not isinstance(item, dict):
                    continue
                ts = _finite_optional_float(item.get("ts"))
                if ts is None:
                    continue
                if persist_window_s > 0.0 and now - ts > persist_window_s:
                    continue
                observations.append({
                    "ts": int(round(ts)),
                    "delta_w": int(round(_finite_float(item.get("delta_w"), 0.0))),
                    "delta_abs_w": int(round(abs(_finite_float(item.get("delta_abs_w"), 0.0)))),
                })

    if diagnostic_high:
        observations.append({
            "ts": int(round(now)),
            "delta_w": int(round(delta_w)),
            "delta_abs_w": int(round(delta_abs_w)),
        })
        if persist_window_s <= 0.0:
            observations = observations[-persist_count:]
    else:
        observations = []

    if diagnostic_high and not debounce_enabled:
        effective = True
        mode = "legacy_immediate" if not cfg_present else "debounce_disabled"
    elif diagnostic_high and delta_abs_w >= hard_threshold_w:
        effective = True
        mode = "hard_threshold"
    elif diagnostic_high and len(observations) >= persist_count:
        effective = True
        mode = "persistent"
    elif diagnostic_high:
        effective = False
        mode = "diagnostic_hold"
    else:
        effective = False
        mode = "within_threshold"

    status = {
        "diagnostic_high": bool(diagnostic_high),
        "rule_effective": bool(effective),
        "mode": mode,
        "debounce_enabled": bool(debounce_enabled),
        "soft_threshold_w": int(round(soft_threshold_w)),
        "hard_threshold_w": int(round(hard_threshold_w)),
        "persist_count": int(persist_count),
        "persist_window_s": int(round(persist_window_s)),
        "observed_count": int(len(observations)),
    }

    if debounce_enabled and state_path:
        try:
            _atomic_write_json(state_path, {
                "schema_version": "live_grid_pm_delta_state_v1",
                "ts": int(round(now)),
                "observations": observations,
                "latest": {
                    "delta_w": int(round(delta_w)),
                    "delta_abs_w": int(round(delta_abs_w)),
                    "diagnostic_high": bool(diagnostic_high),
                    "rule_effective": bool(effective),
                    "mode": mode,
                },
                "config": {
                    "soft_threshold_w": status["soft_threshold_w"],
                    "hard_threshold_w": status["hard_threshold_w"],
                    "persist_count": status["persist_count"],
                    "persist_window_s": status["persist_window_s"],
                },
            })
        except Exception as exc:
            status["state_write_error"] = str(exc)

    return status


def apply_power_plausibility(
    clean,
    errors=None,
    cfg=None,
    *,
    grid_pm_state_path=None,
    now_s=None,
):
    """Ergänzt Echtzeit-Plausibilitätsdaten, ohne Liveleistungswerte zu verändern."""
    if not isinstance(clean, dict):
        return clean
    errors = errors or []
    reasons = []
    diagnostic_reasons = []
    effective_reasons = reasons

    pv_w = _finite_float(clean.get("PV_Power"), 0.0)
    grid_w = _finite_float(clean.get("Grid_Power"), 0.0)
    bat_w = _finite_float(clean.get("Battery_Power"), 0.0)
    home_w = _finite_float(clean.get("Home_Power"), 0.0)
    wb_w = abs(_finite_float(clean.get("Wallbox_Power"), 0.0))
    heizstab_w = max(0.0, _finite_float(clean.get("heizstab_power"), 0.0))

    raw_sources = {
        "PV_Power_Raw": _finite_float(clean.get("_pv_glitch_raw_w", pv_w), pv_w),
        "Grid_Power_Raw": grid_w,
        "Battery_Power_Raw": bat_w,
        "Home_Power_Raw": home_w,
        "Wallbox_Power_Raw": wb_w,
    }
    for key, value in raw_sources.items():
        clean.setdefault(key, int(round(value)) if abs(value - round(value)) < 0.001 else round(value, 3))

    # Von diesem Dienst verwendete E3DC-EMS-Vorzeichen:
    # Grid > 0 Bezug, Grid < 0 Einspeisung; Battery > 0 Laden, Battery < 0 Entladen.
    home_balance_w = max(0.0, pv_w + grid_w - bat_w - wb_w - heizstab_w)
    home_delta_w = home_w - home_balance_w
    clean["Home_Power_Balance"] = int(round(home_balance_w))
    clean["Home_Power_Delta"] = int(round(home_delta_w))

    source = "rscp_direct"
    home_valid = True
    if abs(home_delta_w) <= 80.0:
        source = "ems_balance_like"
    if home_w <= 1.0 and home_balance_w >= 300.0:
        source = "invalid_zero_glitch"
        home_valid = False
        reasons.append("home_zero_but_balance_nonzero")
        diagnostic_reasons.append("home_zero_but_balance_nonzero")
    if home_w < -100.0:
        source = "invalid_negative_home"
        home_valid = False
        reasons.append("home_negative")
        diagnostic_reasons.append("home_negative")
    clean["Home_Power_Source"] = source
    clean["Home_Power_Valid"] = bool(home_valid)
    clean["Home_Power_Independent"] = bool(source == "rscp_direct")

    if "grid_pm_sum_w" in clean:
        pm_available = _finite_bool(clean.get("grid_pm_available"), True)
        clean["Grid_PM_Available"] = bool(pm_available)
        pm_sum_w = _finite_float(clean.get("grid_pm_sum_w"), 0.0)
        grid_pm_delta_w = grid_w - pm_sum_w
        clean["Grid_PM_Delta"] = int(round(grid_pm_delta_w))
        clean["Grid_PM_Delta_Abs"] = int(round(abs(grid_pm_delta_w)))
        if pm_available:
            grid_pm_state_path = (
                grid_pm_state_path
                if grid_pm_state_path is not None
                else (LIVE_GRID_PM_DELTA_STATE_PATH if isinstance(cfg, dict) else None)
            )
            grid_pm_status = _grid_pm_delta_status(
                grid_pm_delta_w,
                cfg,
                state_path=grid_pm_state_path,
                now_s=now_s if now_s is not None else clean.get("_ts"),
            )
        else:
            grid_pm_status = {
                "diagnostic_high": False,
                "rule_effective": False,
                "mode": "pm_unavailable",
                "debounce_enabled": bool(isinstance(cfg, dict) and _cfg_flag(cfg, "live_grid_pm_delta_debounce_enable", True)),
                "soft_threshold_w": int(round(_cfg_number(cfg, "live_grid_pm_delta_soft_threshold_w", 250.0))),
                "hard_threshold_w": int(round(_cfg_number(cfg, "live_grid_pm_delta_hard_threshold_w", 2000.0))),
                "persist_count": max(1, int(round(_cfg_number(cfg, "live_grid_pm_delta_persist_count", 2.0)))),
                "persist_window_s": int(round(max(0.0, _cfg_number(cfg, "live_grid_pm_delta_persist_window_s", 20.0)))),
                "observed_count": 0,
            }
        grid_diagnostic_valid = (not pm_available) or not grid_pm_status["diagnostic_high"]
        grid_valid = (not pm_available) or not grid_pm_status["rule_effective"]
        clean["Grid_Power_Diagnostic_Valid"] = bool(grid_diagnostic_valid)
        clean["Grid_Power_Valid"] = bool(grid_valid)
        clean["Grid_PM_Delta_Rule_Effective"] = bool(pm_available and grid_pm_status["rule_effective"])
        clean["Grid_PM_Delta_Status"] = grid_pm_status
        if pm_available and grid_pm_status["diagnostic_high"]:
            diagnostic_reasons.append("grid_pm_delta_high")
        if pm_available and grid_pm_status["rule_effective"]:
            reasons.append("grid_pm_delta_high")
    else:
        clean.setdefault("Grid_Power_Valid", True)
        clean.setdefault("Grid_Power_Diagnostic_Valid", clean["Grid_Power_Valid"])

    error_class = _rscp_error_class(errors)
    if error_class:
        reasons.append(error_class)
        diagnostic_reasons.append(error_class)
    core_values = {
        key: _finite_optional_float(clean.get(key))
        for key in ("PV_Power", "Grid_Power", "Battery_Power", "Home_Power")
    }
    core_values_complete = all(value is not None for value in core_values.values())
    core_values_zero = bool(
        core_values_complete
        and all(abs(float(value)) <= 1.0 for value in core_values.values())
    )
    if core_values_zero and errors:
        reasons.append("all_core_powers_zero_with_errors")
        diagnostic_reasons.append("all_core_powers_zero_with_errors")

    effective_reasons = sorted(set(effective_reasons))
    diagnostic_reasons = sorted(set(diagnostic_reasons or effective_reasons))
    clean["RSCP_Glitch_Reasons"] = effective_reasons
    clean["RSCP_Diagnostic_Reasons"] = diagnostic_reasons
    clean["RSCP_Sample_Valid"] = bool(not effective_reasons)
    clean["RSCP_Diagnostic_Only"] = bool(diagnostic_reasons and not effective_reasons)
    clean["Power_Plausibility"] = {
        "home_source": clean["Home_Power_Source"],
        "home_valid": clean["Home_Power_Valid"],
        "home_independent": clean["Home_Power_Independent"],
        "home_balance_w": clean["Home_Power_Balance"],
        "home_delta_w": clean["Home_Power_Delta"],
        "grid_pm_available": clean.get("Grid_PM_Available"),
        "grid_pm_delta_w": clean.get("Grid_PM_Delta"),
        "grid_pm_delta_abs_w": clean.get("Grid_PM_Delta_Abs"),
        "grid_pm_delta_diagnostic": not bool(clean.get("Grid_Power_Diagnostic_Valid", True)),
        "grid_pm_delta_rule_effective": bool(clean.get("Grid_PM_Delta_Rule_Effective", False)),
        "grid_pm_delta_status": clean.get("Grid_PM_Delta_Status"),
        "grid_pm_sum_w": clean.get("grid_pm_sum_w"),
        "grid_pm_index": clean.get("grid_pm_index"),
        "grid_pm_source": clean.get("grid_pm_source"),
        "grid_valid": clean.get("Grid_Power_Valid", True),
        "grid_diagnostic_valid": clean.get("Grid_Power_Diagnostic_Valid", True),
        "rscp_error_class": error_class or None,
        "rscp_core_values_complete": bool(core_values_complete),
        "rscp_core_values_zero": bool(core_values_zero),
        "grid_phase_w": [
            clean.get("grid_p1"),
            clean.get("grid_p2"),
            clean.get("grid_p3"),
        ],
        "power_snapshot_source": clean.get("power_snapshot_source"),
        "sample_valid": clean["RSCP_Sample_Valid"],
        "reasons": clean["RSCP_Glitch_Reasons"],
        "effective_reasons": clean["RSCP_Glitch_Reasons"],
        "diagnostic_reasons": clean["RSCP_Diagnostic_Reasons"],
        "diagnostic_only": clean["RSCP_Diagnostic_Only"],
    }
    return clean


def _cfg_float(cfg, key, default):
    if not isinstance(cfg, dict):
        return float(default)
    return _finite_float(cfg.get(str(key).lower(), cfg.get(str(key), default)), default)


def _power_round(value):
    number = _finite_float(value, 0.0)
    if abs(number - round(number)) < 0.001:
        return int(round(number))
    return round(number, 3)


def apply_grid_power_filtered(
    clean,
    cfg=None,
    *,
    state_path=LIVE_GRID_POWER_FILTER_STATE_PATH,
    now_s=None,
):
    """Erzeuge ein gedämpftes Netzpunkt-Signal für langsame Budgetentscheidungen."""
    if not isinstance(clean, dict):
        return clean
    cfg = cfg or {}
    now = _finite_float(now_s if now_s is not None else clean.get("_ts", time.time()), time.time())
    alpha = max(
        0.001,
        min(1.0, _cfg_float(cfg, "live_grid_power_filter_alpha", LIVE_GRID_POWER_FILTER_ALPHA)),
    )
    max_age_s = max(5.0, _cfg_float(cfg, "live_grid_power_filter_state_max_age_s", 120.0))
    raw = _finite_float(clean.get("Grid_Power"), 0.0)
    sample_valid = bool(clean.get("RSCP_Sample_Valid", True) and clean.get("Grid_Power_Valid", True))

    previous = _read_json_file(state_path) if state_path else None
    previous_ts = _finite_float((previous or {}).get("ts"), 0.0) if isinstance(previous, dict) else 0.0
    previous_filtered = _finite_optional_float((previous or {}).get("filtered_w")) if isinstance(previous, dict) else None
    previous_age_s = max(0.0, now - previous_ts) if previous_ts > 0.0 else None
    previous_stale = previous_age_s is None or previous_age_s > max_age_s

    if sample_valid and previous_filtered is not None and not previous_stale:
        filtered = alpha * raw + (1.0 - alpha) * float(previous_filtered)
        status = "filtered"
    elif sample_valid:
        filtered = raw
        status = "reset"
    elif previous_filtered is not None and not previous_stale:
        filtered = float(previous_filtered)
        status = "invalid_sample_hold"
    else:
        filtered = raw
        status = "invalid_sample_raw"

    clean["Grid_Power_Filtered"] = _power_round(filtered)
    clean["Grid_Power_Filtered_Valid"] = bool(sample_valid or (previous_filtered is not None and not previous_stale))
    clean["Grid_Power_Filter"] = {
        "schema_version": "grid_power_filter_v1",
        "status": status,
        "raw_w": _power_round(raw),
        "filtered_w": _power_round(filtered),
        "alpha": round(alpha, 3),
        "previous_age_s": _power_round(previous_age_s) if previous_age_s is not None else None,
        "previous_stale": bool(previous_stale),
        "sample_valid": bool(sample_valid),
        "source": "ewma_30s",
    }

    if state_path and sample_valid:
        try:
            _atomic_write_json(state_path, {
                "schema_version": "grid_power_filter_state_v1",
                "ts": int(round(now)),
                "raw_w": _power_round(raw),
                "filtered_w": _power_round(filtered),
                "alpha": round(alpha, 6),
            })
        except Exception as exc:
            clean["Grid_Power_Filter"]["state_write_error"] = str(exc)
    return clean


def apply_power_decision_stability(
    clean,
    cfg=None,
    *,
    state_path=LIVE_DECISION_STABILITY_PATH,
    now_s=None,
):
    """Exportiert diagnostische EWMA-/Totbandwerte, ohne Live-Rohwerte zu verändern."""
    if not isinstance(clean, dict):
        return clean
    cfg = cfg or {}
    now = _finite_float(now_s if now_s is not None else clean.get("_ts", time.time()), time.time())
    alpha = max(0.05, min(1.0, _cfg_float(cfg, "live_power_decision_ewma_alpha", 0.35)))
    deadband_w = max(0.0, _cfg_float(cfg, "live_power_decision_deadband_w", 120.0))
    max_age_s = max(5.0, _cfg_float(cfg, "live_power_decision_state_max_age_s", 90.0))
    sample_valid = bool(clean.get("RSCP_Sample_Valid", True))

    previous = _read_json_file(state_path) if state_path else None
    previous_ts = _finite_float((previous or {}).get("ts"), 0.0) if isinstance(previous, dict) else 0.0
    previous_age_s = max(0.0, now - previous_ts) if previous_ts > 0.0 else None
    previous_stale = previous_age_s is None or previous_age_s > max_age_s
    previous_signals = (previous or {}).get("signals") if isinstance(previous, dict) else {}
    if not isinstance(previous_signals, dict):
        previous_signals = {}

    signals = {}
    state_signals = {}
    any_reset = False
    any_deadband_hold = False
    any_invalid_hold = False

    for signal in POWER_DECISION_SIGNALS:
        raw = _finite_float(clean.get(signal), 0.0)
        previous_signal = previous_signals.get(signal) if isinstance(previous_signals.get(signal), dict) else {}
        has_previous_signal = bool(previous_signal) and not previous_stale
        prev_ewma = _finite_float(previous_signal.get("ewma_w"), raw)
        prev_decision = _finite_float(previous_signal.get("decision_w"), raw)

        reset = bool(not has_previous_signal)
        held_by_deadband = False
        held_previous_invalid = False

        if sample_valid and has_previous_signal:
            ewma = alpha * raw + (1.0 - alpha) * prev_ewma
            if abs(ewma - prev_decision) < deadband_w:
                decision = prev_decision
                held_by_deadband = True
                any_deadband_hold = True
            else:
                decision = ewma
        elif sample_valid:
            ewma = raw
            decision = raw
            any_reset = True
        elif has_previous_signal:
            ewma = prev_ewma
            decision = prev_decision
            held_previous_invalid = True
            any_invalid_hold = True
        else:
            ewma = raw
            decision = raw
            reset = True
            any_reset = True

        clean[f"{signal}_EWMA"] = _power_round(ewma)
        clean[f"{signal}_Decision"] = _power_round(decision)
        clean[f"{signal}_Decision_Valid"] = bool(sample_valid)

        signals[signal] = {
            "raw_w": _power_round(raw),
            "ewma_w": _power_round(ewma),
            "decision_w": _power_round(decision),
            "previous_decision_w": _power_round(prev_decision) if has_previous_signal else None,
            "reset": bool(reset),
            "held_by_deadband": bool(held_by_deadband),
            "held_previous_invalid": bool(held_previous_invalid),
            "valid": bool(sample_valid),
        }
        state_signals[signal] = {
            "raw_w": _power_round(raw),
            "ewma_w": _power_round(ewma),
            "decision_w": _power_round(decision),
            "valid": bool(sample_valid),
        }

    if not sample_valid and any_invalid_hold:
        status = "invalid_sample_hold"
    elif not sample_valid:
        status = "invalid_sample_raw"
    elif any_reset:
        status = "reset"
    elif any_deadband_hold:
        status = "valid_deadband_hold"
    else:
        status = "valid"

    clean["Power_Decision_Usable"] = bool(sample_valid)
    clean["Power_Decision_Stability"] = {
        "schema_version": "power_decision_stability_v1",
        "status": status,
        "diagnostic_only": True,
        "usable_for_budget": bool(sample_valid),
        "hard_stop_bypass": True,
        "raw_values_preserved": True,
        "sample_valid": bool(sample_valid),
        "alpha": round(alpha, 3),
        "deadband_w": _power_round(deadband_w),
        "max_age_s": _power_round(max_age_s),
        "previous_age_s": _power_round(previous_age_s) if previous_age_s is not None else None,
        "previous_stale": bool(previous_stale),
        "signals": signals,
    }

    if state_path and sample_valid:
        try:
            _atomic_write_json(state_path, {
                "schema_version": "power_decision_stability_state_v1",
                "ts": int(round(now)),
                "signals": state_signals,
            })
        except Exception as exc:
            clean["Power_Decision_Stability"]["state_write_error"] = str(exc)

    return clean


def _round_or_none(value, digits=3):
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number):
        return None
    if abs(number - round(number)) < 0.001:
        return int(round(number))
    return round(number, digits)


def compact_live_plausibility_frame(clean):
    """Liefert einen kompakten forensischen Rahmen für die Live-Daten-Plausibilitätsdiagnose."""
    if not isinstance(clean, dict):
        return {}
    plausibility = clean.get("Power_Plausibility") if isinstance(clean.get("Power_Plausibility"), dict) else {}
    effective_reasons = clean.get("RSCP_Glitch_Reasons", plausibility.get("effective_reasons", plausibility.get("reasons", [])))
    reasons = clean.get("RSCP_Diagnostic_Reasons", plausibility.get("diagnostic_reasons", effective_reasons))
    if not isinstance(effective_reasons, list):
        effective_reasons = [str(effective_reasons)]
    if not isinstance(reasons, list):
        reasons = [str(reasons)]
    valid = bool(clean.get("RSCP_Sample_Valid", plausibility.get("sample_valid", True)))
    diagnostic_only = bool(valid and reasons)
    ts = _round_or_none(clean.get("_ts", clean.get("ts")), 0)
    try:
        iso_ts = time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(float(ts))) if ts else None
    except Exception:
        iso_ts = None
    return {
        "ts": ts,
        "iso_ts": iso_ts,
        "valid": valid,
        "reasons": reasons,
        "effective_reasons": effective_reasons,
        "diagnostic_only": diagnostic_only,
        "source": clean.get("_source"),
        "elapsed_s": _round_or_none(clean.get("_elapsed")),
        "power_snapshot_source": clean.get("power_snapshot_source", plausibility.get("power_snapshot_source")),
        "grid_w": _round_or_none(clean.get("Grid_Power")),
        "grid_power_valid": bool(clean.get("Grid_Power_Valid", plausibility.get("grid_valid", True))),
        "grid_power_diagnostic_valid": bool(clean.get("Grid_Power_Diagnostic_Valid", plausibility.get("grid_diagnostic_valid", True))),
        "grid_pm_sum_w": _round_or_none(clean.get("grid_pm_sum_w", plausibility.get("grid_pm_sum_w"))),
        "grid_pm_delta_w": _round_or_none(clean.get("Grid_PM_Delta", plausibility.get("grid_pm_delta_w"))),
        "grid_pm_delta_abs_w": _round_or_none(clean.get("Grid_PM_Delta_Abs", plausibility.get("grid_pm_delta_abs_w"))),
        "grid_pm_delta_rule_effective": bool(clean.get("Grid_PM_Delta_Rule_Effective", plausibility.get("grid_pm_delta_rule_effective", False))),
        "grid_pm_delta_status": clean.get("Grid_PM_Delta_Status", plausibility.get("grid_pm_delta_status")),
        "grid_pm_available": clean.get("Grid_PM_Available", plausibility.get("grid_pm_available")),
        "grid_pm_index": clean.get("grid_pm_index", plausibility.get("grid_pm_index")),
        "grid_pm_source": clean.get("grid_pm_source", plausibility.get("grid_pm_source")),
        "grid_phase_w": [
            _round_or_none(clean.get("grid_p1")),
            _round_or_none(clean.get("grid_p2")),
            _round_or_none(clean.get("grid_p3")),
        ],
        "pv_w": _round_or_none(clean.get("PV_Power")),
        "battery_w": _round_or_none(clean.get("Battery_Power")),
        "home_w": _round_or_none(clean.get("Home_Power")),
        "home_balance_w": _round_or_none(clean.get("Home_Power_Balance", plausibility.get("home_balance_w"))),
        "home_delta_w": _round_or_none(clean.get("Home_Power_Delta", plausibility.get("home_delta_w"))),
        "home_source": clean.get("Home_Power_Source", plausibility.get("home_source")),
        "wallbox_w": _round_or_none(clean.get("Wallbox_Power")),
        "heizstab_w": _round_or_none(clean.get("heizstab_power")),
        "soc": _round_or_none(clean.get("SOC")),
        "errors": clean.get("_errors", []),
    }


def _atomic_write_json(path, payload):
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(sanitize_for_json(payload), handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
        os.replace(tmp_path, path)
        try:
            os.chmod(path, 0o664)
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass


def _read_json_file(path):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _daily_plausibility_log_path(now_s=None, log_dir=LIVE_PLAUSIBILITY_LOG_DIR):
    now_s = time.time() if now_s is None else float(now_s)
    day = time.strftime("%Y%m%d", time.localtime(now_s))
    return os.path.join(log_dir, f"{LIVE_PLAUSIBILITY_LOG_PREFIX}_{day}.jsonl")


def _trim_jsonl_tail(path, max_records=LIVE_PLAUSIBILITY_LOG_MAX_RECORDS):
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if len(lines) <= max_records:
            return
        with open(path, "w", encoding="utf-8") as handle:
            handle.writelines(lines[-max_records:])
    except Exception:
        return


def record_live_plausibility_state(
    clean,
    *,
    last_valid_path=LIVE_LAST_VALID_PATH,
    log_path=None,
    now_s=None,
):
    """Speichert kompakte Live-Daten-Diagnosen, ohne Regelwerte zu verändern."""
    if not isinstance(clean, dict):
        return None
    frame = compact_live_plausibility_frame(clean)
    if not frame:
        return None
    now_s = time.time() if now_s is None else float(now_s)
    reasons = frame.get("reasons") or []
    if frame.get("valid") and not reasons:
        _LIVE_LAST_VALID_MEMORY[last_valid_path] = frame
        last_write_ts = float(_LIVE_LAST_VALID_WRITE_TS.get(last_valid_path, 0.0) or 0.0)
        if last_write_ts <= 0.0 or now_s - last_write_ts >= LIVE_LAST_VALID_HEARTBEAT_S:
            _atomic_write_json(last_valid_path, frame)
            _LIVE_LAST_VALID_WRITE_TS[last_valid_path] = now_s
            return {"action": "last_valid_updated", "frame": frame}
        return {"action": "last_valid_cached", "frame": frame}

    if not reasons:
        return None
    log_path = log_path or _daily_plausibility_log_path(now_s)
    previous_valid = _LIVE_LAST_VALID_MEMORY.get(last_valid_path)
    if not isinstance(previous_valid, dict):
        previous_valid = _read_json_file(last_valid_path)
    event = {
        "ts": int(now_s),
        "iso_ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now_s)),
        "schema_version": "live_plausibility_glitch_v1",
        "current": frame,
        "previous_valid": previous_valid if isinstance(previous_valid, dict) else None,
    }
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(sanitize_for_json(event), ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n")
    try:
        os.chmod(log_path, 0o664)
    except Exception:
        pass
    try:
        if os.path.getsize(log_path) > LIVE_PLAUSIBILITY_LOG_MAX_BYTES:
            _trim_jsonl_tail(log_path)
    except Exception:
        pass
    action = "diagnostic_glitch_recorded" if frame.get("valid") else "glitch_recorded"
    return {"action": action, "path": log_path, "event": event}

# ---------------------------------------------------------------------------
# Haupt-Schleife (Daemon + Einzel-Test)
# ---------------------------------------------------------------------------

def run_test(host, port, user, pw, aes_pw, cfg, loops=1, interval=3, write=False):
    """
    Haupt-Schleife.
    loops=0  -> Endlosschleife (Daemon-Modus)
    loops>0  -> N Wiederholungen
    write=True -> schreibt live_data_py.json atomar (--write Flag)
    """
    run = 0
    kwh_retter = KwhRetterState()
    external_pv_topology = ExternalPvTopologyEvidenceTracker()
    daemon_quiet = bool(write and loops == 0)
    persistent_enabled = str(cfg.get("e3dc_live_persistent_connection", "0")).strip().lower() in {
        "1", "true", "yes", "on", "ein",
    }
    persistent_session = None
    if persistent_enabled:
        persistent_session = RscpAcquisitionSession(
            lambda: RscpConnection(host, port, aes_pw),
            lambda connection: connection.authenticate(user, pw),
        )
    while True:
        run += 1
        if loops > 0 and run > loops:
            if persistent_session is not None:
                persistent_session.close()
            break
        if loops != 1 and not daemon_quiet:  # Nicht bei Einzel-Run
            print(f"\n{'='*55}\nDurchlauf {run}{(' / ' + str(loops)) if loops > 0 else ' (Daemon)'}\n{'='*55}")

        t_start = time.monotonic()
        if not daemon_quiet:
            print(f"\nVerbinde mit E3DC {host}:{port} ...")

        data = {}
        errors = []
        acquisition_diagnostics = {"schema_version": "rscp_acquisition_v1", "mode": "legacy_per_cycle"}
        if persistent_session is not None:
            sections = persistent_acquisition_sections(cfg)
            output_context = contextlib.redirect_stdout(io.StringIO()) if daemon_quiet else contextlib.nullcontext()
            with output_context:
                data, errors, acquisition_diagnostics = persistent_session.acquire(sections)
            if acquisition_diagnostics.get("connected"):
                _live_log_limiter.recovery(logger, "rscp_connection", "Persistente RSCP-Verbindung wiederhergestellt")
            for error in errors:
                key = "rscp_connection" if error.startswith(("Verbindung:", "Reconnect-Backoff")) else "persistent_section"
                _live_log_limiter.failure(logger, key, "RSCP-Akquise: %s", error)
        else:
            conn = RscpConnection(host, port, aes_pw)
            try:
                output_context = contextlib.redirect_stdout(io.StringIO()) if daemon_quiet else contextlib.nullcontext()
                with output_context:
                    conn.connect()
                    conn.authenticate(user, pw)
                _live_log_limiter.recovery(logger, "rscp_connection", "RSCP-Verbindung wiederhergestellt")
                if not daemon_quiet:
                    print("[OK] Authentifiziert\n")

                sections = [
                    ("Power Snapshot",   lambda: get_power_snapshot(conn, cfg)),
                    ("EMS Anlagendata",  lambda: get_ems_config(conn)),
                    ("DB-History (kWh)", lambda: get_db_history(conn)),
                    ("PVI DC/AC",        lambda: get_pvi(conn)),
                    ("Batterie",         lambda: get_bat(conn, cfg)),
                    ("Wallbox",          lambda: get_wb(conn, cfg)),
                    ("System-Info",      lambda: get_system_info(conn)),
                ]

                for name, fn in sections:
                    try:
                        output_context = contextlib.redirect_stdout(io.StringIO()) if daemon_quiet else contextlib.nullcontext()
                        with output_context:
                            data.update(fn())
                        _live_log_limiter.recovery(logger, f"section:{name}", "RSCP-Bereich %s wieder verfügbar", name)
                    except Exception as e:
                        if daemon_quiet:
                            _live_log_limiter.failure(logger, f"section:{name}", "RSCP-Bereich %s fehlgeschlagen: %s", name, e)
                        else:
                            print(f"  [!] {name} fehlgeschlagen: {e}")
                        errors.append(f"{name}: {e}")
                    if not daemon_quiet:
                        print()

            except Exception as e:
                if daemon_quiet:
                    _live_log_limiter.failure(logger, "rscp_connection", "RSCP-Verbindungsfehler zu %s:%s: %s", host, port, e)
                else:
                    print(f"[!] Verbindungsfehler: {e}")
                errors.append(str(e))
            finally:
                conn.close()

        data = normalise_live_ep_reserve(data, cfg)

        # Effizienz & Umwandlungsverluste sind reine Diagnose. Fehlende zwingende
        # DB-Zähler degradieren die Bilanz; die optionale, unbelegte WB-Energie
        # wird nicht als echte 0 publiziert und beendet den Liveprozess nicht.
        energy_metrics = calculate_energy_balance_metrics(data)
        data.update(energy_metrics)

        supply = energy_metrics["sys_energy_supply_kwh"]
        demand = energy_metrics["sys_energy_demand_kwh"]
        loss_kwh = energy_metrics["sys_loss_kwh"]
        loss_pct = energy_metrics["sys_loss_pct"]
        bat_net_kwh = energy_metrics["bat_net_discharge_kwh"]
        bat_rte_pct = energy_metrics["bat_rte_pct"]
        if energy_metrics["sys_energy_balance_valid"] and supply > 0 and not daemon_quiet:
            rte_str = (
                f"  Bat-RTE={bat_rte_pct}%"
                if bat_rte_pct is not None
                else f"  Bat-Netto-Entladen={bat_net_kwh}kWh"
            )
            print(f"  -> Effizienz: Eingang={supply:.3f}kWh  Ausgang={demand:.3f}kWh  "
                  f"Verluste={loss_kwh:.3f}kWh ({loss_pct}%){rte_str}")

        # kWh-Retter ausführen
        pv_p = data.get("PV_Power", 0)
        derate_lim = data.get("derate_at_power_w", 0)
        inv_lim = data.get("ac_power_limit_w", 0)

        kwh_retter.add_sample(pv_p, derate_lim, inv_lim)
        retter_stats = kwh_retter.get_stats()
        data.update(retter_stats)

        # Gebe die Retter-Stats noch im Print aus
        if pv_p > 0 and not daemon_quiet:
            print(f"  -> kWh-Retter: Heute {retter_stats['saved_derating_today_kwh']:.2f} kWh (Abregelung) / {retter_stats['saved_inverter_today_kwh']:.2f} kWh (AC-Limit) gerettet")

        elapsed = time.monotonic() - t_start

        # Sauberes JSON (interne _ Keys rausfiltern)
        clean = {k: v for k, v in data.items() if not k.startswith("_")}
        clean["_source"]  = "python_rscp"   # PHP prüft auf diesen Wert
        clean["_ts"]      = int(time.time())
        clean["_elapsed"] = round(elapsed, 3)
        clean["RSCP_Acquisition"] = acquisition_diagnostics
        if errors:
            clean["_errors"] = errors

        clean = apply_pv_zero_glitch_filter(clean)
        clean = apply_power_plausibility(clean, errors, cfg)
        clean = apply_grid_power_filtered(clean, cfg)
        clean = apply_power_decision_stability(clean, cfg)
        clean = sanitize_for_json(clean)
        try:
            topology = (
                external_pv_topology.observe(clean, now_s=clean.get("_ts"))
                if write
                else {**read_external_pv_topology(external_pv_topology.state_path), "write_performed": False}
            )
        except Exception as topology_exc:
            topology = {
                "topology_present": False,
                "valid": False,
                "source": "none",
                "evidence_state": "invalid",
                "reason": "persistence_error",
                "write_performed": False,
            }
            _live_log_limiter.failure(
                logger,
                "external_pv_topology",
                "Zusatz-WR-Topologienachweis ist fail-closed: %s",
                topology_exc,
                level=logging.ERROR,
            )
        clean["External_PV_Topology_Present"] = bool(topology.get("topology_present"))
        clean["External_PV_Topology_Valid"] = bool(topology.get("valid"))
        clean["External_PV_Topology_Source"] = str(topology.get("source") or "none")
        clean["External_PV_Topology_Evidence_State"] = str(topology.get("evidence_state") or "invalid")
        clean["External_PV_Topology_Reason"] = str(topology.get("reason") or "")
        try:
            record_live_plausibility_state(clean)
        except Exception as diag_exc:
            if daemon_quiet:
                _live_log_limiter.failure(logger, "plausibility_write", "Plausibilitätsdiagnose konnte nicht geschrieben werden: %s", diag_exc)
            else:
                print(f"  [Plausibilität] Diagnose schreiben fehlgeschlagen: {diag_exc}")

        if daemon_quiet:
            logger.info("RSCP-Live: Abruf %.2fs, %d Felder, %d Fehler", elapsed, len(clean), len(errors))
        else:
            print(f"[Datenabruf in {elapsed:.2f}s | {len(clean)} Felder | {len(errors)} Fehler]\n")

        # --write: live_data_py.json ist die permanente Zieldatei
        if write:
            out_path = LIVE_DATA_PATH
            tmp_path = out_path + '.tmp'
            ramdisk_dir = os.path.dirname(out_path)
            try:
                # Ramdisk-Verzeichnis sicherstellen (z.B. nach Neustart nicht gemountet)
                if not os.path.isdir(ramdisk_dir):
                    os.makedirs(ramdisk_dir, exist_ok=True)
                    logger.warning("Ramdisk-Verzeichnis %s fehlte und wurde angelegt; tmpfs-Mount prüfen", ramdisk_dir)
                with open(tmp_path, 'w', encoding='utf-8') as f:
                    json.dump(clean, f, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
                os.replace(tmp_path, out_path)  # Atomares Schreiben (kein halb-geschriebenes JSON)
                os.chmod(out_path, 0o664)       # www-data muss lesend zugreifen können
                _live_log_limiter.recovery(logger, "live_write", "Live-Daten können wieder geschrieben werden")
                if not daemon_quiet:
                    print(f"[OK] Geschrieben: {out_path}")
            except PermissionError as we:
                _user = os.environ.get('USER', 'pi')
                if daemon_quiet:
                    _live_log_limiter.failure(
                        logger,
                        "live_write",
                        "Schreiben nach %s verweigert (%s, Nutzer %s)",
                        ramdisk_dir,
                        we,
                        _user,
                        level=logging.ERROR,
                    )
                else:
                    print(f"[!] SCHREIBEN VERWEIGERT ({ramdisk_dir}): {we}")
            except Exception as we:
                if daemon_quiet:
                    _live_log_limiter.failure(logger, "live_write", "Live-Daten konnten nicht geschrieben werden: %s", we, level=logging.ERROR)
                else:
                    print(f"[!] Schreiben fehlgeschlagen: {we}")
        else:
            print("--- JSON-Ausgabe (würde in live_data_py.json geschrieben) ---")
            print(json.dumps(clean, indent=2, ensure_ascii=False, allow_nan=False))


        # Letzte Wartezeit vor dem nächsten Durchlauf
        if loops == 0 or run < loops:
            t_elapsed = time.monotonic() - t_start
            sleep_s = max(0.1, interval - t_elapsed)
            if not write:  # Im Daemon-Modus still schlafen
                print(f"  Warte {sleep_s:.1f}s ...")
            time.sleep(sleep_s)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _rscp_runtime_config_complete(host, port, user, password, rscp_password):
    try:
        port_value = int(port)
    except (TypeError, ValueError):
        return False
    return bool(
        str(host or "").strip()
        and 1 <= port_value <= 65535
        and str(user or "").strip()
        and str(password or "").strip()
        and str(rscp_password or "").strip()
    )

if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="e3dc_live.py - E3DC Live-Daten Service via RSCP"
    )
    parser.add_argument("--host",     default=None)
    parser.add_argument("--port",     type=int, default=None)
    parser.add_argument("--user",     default=None)
    parser.add_argument("--pw",       default=None)
    parser.add_argument("--aes",      default=None)
    parser.add_argument("--loops",    type=int, default=1,
                        help="Anzahl Wiederholungen. 0 = Endlosschleife (Daemon)")
    parser.add_argument("--interval", type=int, default=3,
                        help="Sekunden zwischen Abfragen (Standard: 3)")
    parser.add_argument(
        "--write", action="store_true",
        help="Schreibt Ergebnis als live_data_py.json in RAM-Disk (Daemon-Modus)"
    )
    args = parser.parse_args()

    print("E3DC-Live-Dienst - lädt Konfiguration ...\n")
    cfg    = _find_config()
    host   = args.host   or cfg.get("server_ip") or ""
    port   = args.port if args.port is not None else (cfg.get("server_port") or 5033)
    user   = args.user   or cfg.get("e3dc_user") or ""
    pw     = args.pw     or cfg.get("e3dc_password") or ""
    aes_pw = args.aes    or cfg.get("aes_password") or pw

    if not _rscp_runtime_config_complete(host, port, user, pw, aes_pw):
        if args.write and args.loops == 0 and not (args.host or args.user or args.pw or args.aes):
            print("[!] E3DC-RSCP-Ziel oder Zugangsdaten fehlen - warte auf gespeicherte Konfiguration ...")
            while True:
                time.sleep(30)
                cfg    = _find_config()
                host   = cfg.get("server_ip") or ""
                port   = cfg.get("server_port") or 5033
                user   = cfg.get("e3dc_user") or ""
                pw     = cfg.get("e3dc_password") or ""
                aes_pw = cfg.get("aes_password") or pw
                if _rscp_runtime_config_complete(host, port, user, pw, aes_pw):
                    print("[OK] Konfiguration gefunden, starte RSCP Live-Dienst.")
                    break
                print("[i] Warte weiter auf vollständige E3DC-RSCP-Konfiguration in /var/www/html/data/e3dc_v4.json ...")
        else:
            print("[!] E3DC-RSCP-Ziel oder Zugangsdaten fehlen!")
            sys.exit(1)

    port = int(port)
    print(f"\nHost: {host}:{port}  User: {user}")
    if args.loops == 0:
        print(f"[Daemon] Endlosschleife, Intervall {args.interval}s")
    elif args.loops > 1:
        print(f"[N-Shot] {args.loops}x alle {args.interval}s")
    if args.write:
        print("[Write] -> /var/www/html/ramdisk/live_data_py.json")
        # Ramdisk-Vorabprüfung: Schreiben möglich?
        _rdir = '/var/www/html/ramdisk'
        if not os.path.isdir(_rdir):
            print(f"[!] Ramdisk-Verzeichnis fehlt: {_rdir}")
            print(f"[!] Erstelle Verzeichnis (kein tmpfs, aber besser als Absturz)...")
            try:
                os.makedirs(_rdir, mode=0o775, exist_ok=True)
            except Exception as _e:
                print(f"[!] Fehler beim Erstellen: {_e} – Berechtigungen prüfen!")
        elif not os.access(_rdir, os.W_OK):
            print(f"[!] Kein Schreibrecht auf {_rdir}!")
            print(f"[!] Fix: sudo chown -R pi:www-data {_rdir} && sudo chmod -R 775 {_rdir}")
    print()

    run_test(host, port, user, pw, aes_pw, cfg,
             loops=args.loops, interval=args.interval, write=args.write)
