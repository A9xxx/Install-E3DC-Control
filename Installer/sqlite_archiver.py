#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import sqlite3
import os
import datetime
import sys
from pathlib import Path

# Standard-Ausgabe auf UTF-8 erzwingen (verhindert UnicodeEncodeError z.B. bei cron oder SSH ohne Locale)
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

DB_DIR = "/var/www/html/data"
DB_PATH = os.path.join(DB_DIR, "e3dc_stats.db")
JSON_PATH = "/var/www/html/ramdisk/daily_stats.json"
HISTORY_PATH = "/var/www/html/ramdisk/live_history.txt"
HISTORY_BACKUP_DIR = "/var/www/html/data/history_backups"

def export_backed_pv_total(pv_yield, grid_out, bat_out, exact_counter_present=False, source=""):
    pv = max(0.0, float(pv_yield or 0.0))
    export = max(0.0, float(grid_out or 0.0))
    battery = max(0.0, float(bat_out or 0.0))
    source = str(source or "")
    if exact_counter_present or source in {"exact_e3dc_counter", "integrated_total_with_external_ac"}:
        return pv
    if pv > 0.0001 and export > 0.5 and export > pv + 0.5 and export > max(1.0, battery * 1.5):
        return round(pv + export, 3)
    return pv

def keep_integrated_pv_total_for_external_ac(integrated_pv_kwh, integrated_dc_kwh, exact_e3dc_kwh):
    integrated = max(0.0, float(integrated_pv_kwh or 0.0))
    integrated_dc = max(0.0, float(integrated_dc_kwh or 0.0))
    exact = max(0.0, float(exact_e3dc_kwh or 0.0))
    if integrated <= 0.5 or exact <= 0.5 or integrated_dc <= 0.5:
        return False
    if integrated <= exact + max(2.0, exact * 0.08):
        return False
    return abs(integrated_dc - exact) <= max(5.0, exact * 0.20)

def get_install_path():
    root = Path(__file__).resolve().parent.parent
    markers = (root / "VERSION", root / "installer_main.py", root / "Installer")
    if not all(marker.exists() for marker in markers):
        raise RuntimeError("SQLite-Archiv: Release-Root ist nicht eindeutig aufloesbar")
    return str(root)

def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    cursor = conn.cursor()
    # Erstelle die Tabelle für die Tageswerte, falls sie nicht existiert
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS daily_stats (
            date TEXT PRIMARY KEY,
            pv_yield REAL,
            home_consumption REAL,
            grid_in REAL,
            grid_out REAL,
            bat_in REAL,
            bat_out REAL,
            wb_consumption REAL,
            wp_consumption REAL,
            climate_consumption REAL,
            autarky REAL,
            self_con REAL
        )
    ''')

    # Automatische Migration: Neue Spalten hinzufügen
    for col in ['cost_total', 'cost_home', 'cost_bat', 'cost_wb', 'cost_wp', 'wb2_consumption', 'cost_wb2', 'climate_consumption', 'cost_climate', 'pv_balance_rest', 'bat_balance_rest', 'balance_unknown_rest', 'saved_u', 'saved_td', 'saved_wb', 'pv_e3dc', 'pv_external', 'pv_source_rest', 'pv_grid', 'bat_grid']:
        try:
            cursor.execute(f"ALTER TABLE daily_stats ADD COLUMN {col} REAL DEFAULT 0")
        except sqlite3.OperationalError:
            pass # Spalte existiert bereits, alles gut!

    # Tabelle für Machine Learning Trainingsdaten (15-Minuten Raster aus Ertrag.X.txt)
    cursor.execute('''
        CREATE TABLE IF NOT EXISTS ml_training_data (
            id TEXT PRIMARY KEY,
            date TEXT,
            time_gmt REAL,
            pv_prog_pct REAL,
            pv_real_pct REAL,
            home_kwh_cum REAL,
            wp_kwh_cum REAL,
            temp_c REAL,
            grid_kwh_cum REAL
        )
    ''')
    conn.commit()
    return conn

def archive_today():
    if not os.path.exists(JSON_PATH):
        return

    try:
        with open(JSON_PATH, 'r') as f:
            data = json.load(f)
    except Exception:
        return

    stats = data.get('stats', {})
    costs = data.get('costs', {})
    today = datetime.date.today().isoformat() # Format: YYYY-MM-DD

    pv_yield = float(data.get('pv_today_kwh', 0.0) or 0.0)
    autarky = float(data.get('autarky_day', 0.0) or 0.0)
    self_con = float(data.get('selfcon_day', 0.0) or 0.0)

    # Detaillierte Energieverteilung aufaddieren.
    # WICHTIG: total_home_kwh muss grid_home_kwh BEREITS ENTHALTEN (pv+bat+grid -> Home).
    # Aeltere Systeme oder C++-Quellen koennen hier nur den selbstversorgten Anteil liefern.
    # Plausibilitaetspruefung: Wenn die Energiefluss-Summe groesser ist, verwenden wir sie.
    _pv_h   = float(stats.get('pv_home_kwh',   0.0) or 0.0)
    _grid_h = float(stats.get('grid_home_kwh', 0.0) or 0.0)
    _bat_h  = float(stats.get('bat_home_kwh',  0.0) or 0.0)
    _total_h = float(stats.get('total_home_kwh') or 0.0)
    _flow_h  = _pv_h + _grid_h + _bat_h  # Summe der Einzelfluesse

    if _flow_h > 0.1:
        # Einzelfluesse vorhanden -> pruefe ob total schon grid enthaelt
        if (_total_h + 0.5) < _flow_h and _grid_h > 0.5:
            # total ist deutlich kleiner als Summe -> grid_home fehlt im total
            home_consumption = round(_flow_h, 3)
            print(f"  [FIX] home_consumption: total={_total_h:.2f} < flow={_flow_h:.2f} -> grid_home={_grid_h:.2f} fehlte, verwende Summe.")
        else:
            home_consumption = round(max(_total_h, _flow_h), 3)
    else:
        # Keine Einzelfluesse (Python RSCP): total_home_kwh direkt verwenden
        home_consumption = round(_total_h, 3)

    wb_consumption  = float(stats.get('total_wb_kwh')  or (float(stats.get('pv_wb_kwh',  0.0) or 0.0) + float(stats.get('grid_wb_kwh',  0.0) or 0.0) + float(stats.get('bat_wb_kwh',  0.0) or 0.0)))
    wb2_consumption = float(stats.get('total_wb2_kwh') or (float(stats.get('pv_wb2_kwh', 0.0) or 0.0) + float(stats.get('grid_wb2_kwh', 0.0) or 0.0) + float(stats.get('bat_wb2_kwh', 0.0) or 0.0)))
    wp_consumption  = float(stats.get('total_wp_kwh')  or (float(stats.get('pv_wp_kwh',  0.0) or 0.0) + float(stats.get('grid_wp_kwh',  0.0) or 0.0) + float(stats.get('bat_wp_kwh',  0.0) or 0.0)))
    climate_consumption = float(stats.get('total_climate_kwh') or (float(stats.get('pv_climate_kwh', 0.0) or 0.0) + float(stats.get('grid_climate_kwh', 0.0) or 0.0) + float(stats.get('bat_climate_kwh', 0.0) or 0.0)))

    grid_in  = float(stats.get('total_grid_in_kwh', 0.0) or 0.0)
    grid_out = float(stats.get('total_grid_out_kwh') or stats.get('pv_grid_kwh', 0.0) or 0.0)
    bat_in   = float(stats.get('pv_bat_kwh', 0.0) or 0.0) + float(stats.get('grid_bat_kwh', 0.0) or 0.0)
    bat_out  = float(stats.get('total_bat_out_kwh', 0.0) or 0.0)
    pv_e3dc = float(stats.get('pv_e3dc_kwh', 0.0) or 0.0)
    pv_external = float(stats.get('pv_external_kwh', 0.0) or 0.0)
    pv_source_rest = float(stats.get('pv_source_rest_kwh', 0.0) or 0.0)
    pv_grid = float(stats.get('pv_grid_kwh', 0.0) or 0.0)
    bat_grid = float(stats.get('bat_grid_kwh', 0.0) or 0.0)
    sources = data.get('sources', {}) if isinstance(data.get('sources', {}), dict) else {}
    pv_yield = export_backed_pv_total(
        pv_yield,
        grid_out,
        bat_out,
        exact_counter_present=bool(sources.get('pv_e3dc_exact_kwh')),
        source=sources.get('pv_total_source', ''),
    )
    if pv_yield > 0.0:
        self_con = round(max(0.0, min(100.0, ((pv_yield - grid_out) / max(0.001, pv_yield)) * 100.0)), 1)

    def _stats_sum(keys):
        total = 0.0
        for key in keys:
            total += float(stats.get(key, 0.0) or 0.0)
        return total

    pv_balance_rest = max(0.0, pv_yield - _stats_sum([
        'pv_home_kwh', 'pv_wb_kwh', 'pv_wb2_kwh', 'pv_wp_kwh',
        'pv_climate_kwh', 'pv_bat_kwh', 'pv_grid_kwh',
    ]))
    bat_balance_rest = max(0.0, bat_out - _stats_sum([
        'bat_home_kwh', 'bat_wb_kwh', 'bat_wb2_kwh', 'bat_wp_kwh', 'bat_climate_kwh', 'bat_grid_kwh',
    ]))
    known_use_for_balance = (
        home_consumption + wb_consumption + wb2_consumption + wp_consumption
        + climate_consumption + grid_out + bat_in
    )
    total_balance_rest = max(0.0, (pv_yield + grid_in + bat_out) - known_use_for_balance)
    balance_unknown_rest = max(0.0, total_balance_rest - pv_balance_rest - bat_balance_rest)

    # Autarkie-Fallback: Falls E3DC keinen Wert meldet, selbst berechnen
    if autarky == 0.0 and home_consumption > 0.1:
        autarky = round(max(0.0, min(100.0, (1 - grid_in / home_consumption) * 100)), 1)

    # Reale Kosten erfassen
    cost_total = float(costs.get('total', 0.0) or 0.0)
    cost_home = float(costs.get('home', 0.0) or 0.0)
    cost_bat = float(costs.get('bat', 0.0) or 0.0)
    cost_wb = float(costs.get('wb', 0.0) or 0.0)
    cost_wb2 = float(costs.get('wb2', 0.0) or 0.0)
    cost_wp = float(costs.get('wp', 0.0) or 0.0)
    cost_climate = float(costs.get('climate', 0.0) or 0.0)

    # Peak-Shaving Daten auslesen.
    # Neue Quelle ist daily_stats.json/live_data_py.json. Die alte peak_live.json
    # darf nur noch als frischer Legacy-Fallback dienen, sonst bleiben dort alte
    # Nullwerte kleben und die Langzeitgrafik zeigt "Peak Gerettet 0 kWh".
    saved = data.get('saved', {}) if isinstance(data.get('saved', {}), dict) else {}
    saved_td = float(saved.get('derating_today_kwh', data.get('saved_td', 0.0)) or 0.0)
    saved_wb = float(saved.get('inverter_today_kwh', data.get('saved_wb', 0.0)) or 0.0)
    saved_u = float(saved.get('total_today_kwh', data.get('saved_u', 0.0)) or 0.0)
    if saved_u <= 0.0001:
        saved_u = saved_td + saved_wb

    live_file = "/var/www/html/ramdisk/live_data_py.json"
    if saved_u <= 0.0001 and os.path.exists(live_file):
        try:
            with open(live_file, 'r') as f:
                live_data = json.load(f)
                saved_td = float(live_data.get('saved_derating_today_kwh', 0.0) or 0.0)
                saved_wb = float(live_data.get('saved_inverter_today_kwh', 0.0) or 0.0)
                saved_u = saved_td + saved_wb
        except Exception:
            pass

    peak_file = "/var/www/html/ramdisk/peak_live.json"
    if saved_u <= 0.0001 and os.path.exists(peak_file):
        try:
            with open(peak_file, 'r') as f:
                peak_data = json.load(f)
                peak_ts = float(peak_data.get('ts', 0) or 0)
                peak_day = datetime.date.fromtimestamp(peak_ts).isoformat() if peak_ts > 0 else ''
                if peak_day == today:
                    saved_td = float(peak_data.get('saved_today', 0.0) or 0.0)
                    saved_wb = float(peak_data.get('saved_wb', 0.0) or 0.0)
                    saved_u = float(peak_data.get('saved_u', 0.0) or 0.0)
                    if saved_u <= 0.0001:
                        saved_u = saved_td + saved_wb
        except Exception:
            pass

    # Sanity check: Nur speichern, wenn E3DC heute wirklich Werte geliefert hat
    if pv_yield == 0 and home_consumption == 0:
        return

    conn = init_db()
    cursor = conn.cursor()
    # INSERT OR REPLACE sorgt dafür, dass die Zeile für heute stündlich überschrieben/geupdatet wird
    cursor.execute('''
        INSERT OR REPLACE INTO daily_stats
        (date, pv_yield, home_consumption, grid_in, grid_out, bat_in, bat_out, wb_consumption, wb2_consumption, wp_consumption, climate_consumption, autarky, self_con, cost_total, cost_home, cost_bat, cost_wb, cost_wb2, cost_wp, cost_climate, pv_balance_rest, bat_balance_rest, balance_unknown_rest, saved_u, saved_td, saved_wb, pv_e3dc, pv_external, pv_source_rest, pv_grid, bat_grid)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (today, pv_yield, home_consumption, grid_in, grid_out, bat_in, bat_out, wb_consumption, wb2_consumption, wp_consumption, climate_consumption, autarky, self_con, cost_total, cost_home, cost_bat, cost_wb, cost_wb2, cost_wp, cost_climate, pv_balance_rest, bat_balance_rest, balance_unknown_rest, saved_u, saved_td, saved_wb, pv_e3dc, pv_external, pv_source_rest, pv_grid, bat_grid))
    conn.commit()
    conn.close()

    # Rechte setzen, damit PHP/Webserver die DB später lesen können
    try:
        os.chmod(DB_PATH, 0o664)
        os.chmod(DB_DIR, 0o775)
    except:
        pass

def _float_value(value, default=0.0):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def _history_lines_for_date(day):
    day_s = day.isoformat() if hasattr(day, "isoformat") else str(day)
    paths = [
        os.path.join(HISTORY_BACKUP_DIR, f"history_{day_s}.txt"),
        HISTORY_PATH,
    ]
    seen = set()
    rows = []
    for path in paths:
        if not os.path.exists(path):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    if day_s not in line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    ts = str(row.get("ts", ""))
                    if not ts.startswith(day_s):
                        continue
                    key = ts + "|" + str(row.get("e_home", "")) + "|" + str(row.get("home", ""))
                    if key in seen:
                        continue
                    seen.add(key)
                    rows.append(row)
        except Exception:
            continue
    rows.sort(key=lambda r: str(r.get("ts", "")))
    return rows

def _parse_history_ts(row):
    try:
        return datetime.datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00"))
    except Exception:
        return None

def _row_has_stiebel_wp_counter(row):
    wp_type = str(row.get("wp_type", "")).strip()
    e_wp_source = str(row.get("e_wp_source", "")).strip().lower()
    wp_source = str(row.get("wp_source", "")).strip().lower()
    return wp_type == "4" or e_wp_source.startswith("stiebel") or wp_source.startswith("stiebel")

def _normalise_wb_type(value):
    raw = str(value or "").strip().lower()
    aliases = {
        "goe": "go-e",
        "openwb-pro": "openwb_pro",
        "openwbpro": "openwb_pro",
        "e3dc_multi_connect": "e3dc_multi",
        "e3dc-multi": "e3dc_multi",
        "e3dc multi": "e3dc_multi",
        "e3dc_easy": "e3dc",
        "e3dc_legacy": "e3dc",
        "native": "e3dc",
        "off": "none",
        "disabled": "none",
        "deaktiviert": "none",
        "keine": "none",
        "false": "none",
        "no": "none",
        "0": "none",
        "-1": "none",
    }
    return aliases.get(raw, raw)

def _history_wallbox_home_relation(row, slot="wb"):
    is_wb2 = str(slot) == "wb2"
    relation_key = "wb2_home_relation" if is_wb2 else "wb_home_relation"
    relation = str(row.get(relation_key, "") or "").strip().lower()
    if relation:
        return relation

    type_key = "wb2_native_type" if is_wb2 else "wb_native_type"
    wb_type = _normalise_wb_type(row.get(type_key, ""))
    source_keys = (
        ("wb2_source", "e_wb2_source", "wb2_daily_source")
        if is_wb2
        else ("wb_source", "e_wb_source", "wb_daily_source")
    )
    is_e3dc_multi = wb_type in ("e3dc_multi", "e3dc_multi_connect")
    for key in source_keys:
        source = str(row.get(key, "") or "").strip().lower()
        if "e3dc_multi" in source:
            is_e3dc_multi = True
    if not is_e3dc_multi or "home_raw" not in row:
        return ""

    power_key = "wb2" if is_wb2 else "wb"
    wb = max(0.0, _float_value(row.get(power_key), 0.0))
    if wb <= 50.0:
        return ""
    pv = _float_value(row.get("pv"), 0.0)
    grid = _float_value(row.get("grid"), 0.0)
    bat = _float_value(row.get("bat"), 0.0)
    home_raw = max(0.0, _float_value(row.get("home_raw"), 0.0))
    total_load = pv + grid - bat
    if total_load < -500.0 or total_load > 60000.0:
        return ""

    tolerance = max(250.0, min(1200.0, wb * 0.18))
    net_gap = total_load - home_raw
    if abs(net_gap - wb) <= tolerance:
        return "home_excludes_wb"
    if abs(total_load - home_raw) <= tolerance and home_raw + tolerance >= wb:
        return "home_includes_wb"
    if abs(total_load - home_raw) <= tolerance and home_raw + tolerance < wb:
        return "stale_balance_reject"
    return ""

def _row_marks_external_consumer(row, slot="wb"):
    is_wb2 = str(slot) == "wb2"
    relation = _history_wallbox_home_relation(row, slot)
    if relation in ("home_includes_wb", "home_includes_wallbox"):
        return True
    if relation in ("home_excludes_wb", "home_excludes_wallbox", "stale_balance_reject"):
        return False

    flag_key = "is_external_wb2" if is_wb2 else "is_external_wb"
    if bool(row.get(flag_key)):
        return True
    type_key = "wb2_native_type" if is_wb2 else "wb_native_type"
    wb_type = _normalise_wb_type(row.get(type_key, ""))
    if wb_type and wb_type != "none" and not wb_type.startswith("e3dc"):
        return True
    source_keys = (
        ("wb2_source", "e_wb2_source", "wb2_daily_source")
        if is_wb2
        else ("wb_source", "e_wb_source", "wb_daily_source")
    )
    for key in source_keys:
        source = str(row.get(key, "") or "").strip().lower()
        if not source:
            continue
        if any(token in source for token in ("openwb", "mqtt", "external", "evcc", "go-e", "goe", "shelly", "e3dc_multi")):
            return True
    return False

def _clean_history_home_power(row):
    home = _float_value(row.get("home"), _float_value(row.get("home_raw"), 0.0))
    home = max(0.0, home)
    if "home_raw" not in row:
        return home
    home_raw = _float_value(row.get("home_raw"), home)
    for slot in ("wb", "wb2"):
        relation = _history_wallbox_home_relation(row, slot)
        if relation not in ("home_excludes_wb", "home_excludes_wallbox", "stale_balance_reject"):
            continue
        wb_power = max(0.0, _float_value(row.get(slot), 0.0))
        threshold = 50.0 if relation == "stale_balance_reject" else max(250.0, min(1200.0, wb_power * 0.18))
        if home_raw > 50.0 and home < home_raw - threshold:
            home = home_raw
    external_load = 0.0
    if _row_marks_external_consumer(row, "wb"):
        external_load += max(0.0, _float_value(row.get("wb"), 0.0))
    if _row_marks_external_consumer(row, "wb2"):
        external_load += max(0.0, _float_value(row.get("wb2"), 0.0))
    external_load += max(
        max(0.0, _float_value(row.get("wp"), 0.0)),
        max(0.0, _float_value(row.get("hs"), 0.0)),
    )
    external_load += max(0.0, _float_value(row.get("climate"), 0.0))
    if external_load <= 50.0:
        return home
    clean_from_raw = max(0.0, home_raw - external_load)
    threshold = max(250.0, min(1500.0, external_load * 0.15))
    if home > clean_from_raw + threshold:
        return clean_from_raw
    return home

def _clean_exact_home_energy(exact_home, wb=0.0, wb2=0.0, wp=0.0, climate=0.0):
    """Return house-only daily energy from the E3DC gross home counter."""
    home = max(0.0, _float_value(exact_home, 0.0))
    if home <= 0.0:
        return home
    consumer_total = (
        max(0.0, _float_value(wb, 0.0))
        + max(0.0, _float_value(wb2, 0.0))
        + max(0.0, _float_value(wp, 0.0))
        + max(0.0, _float_value(climate, 0.0))
    )
    if consumer_total <= 0.05:
        return home
    if home + 0.05 < consumer_total:
        return home
    return max(0.0, home - consumer_total)

def _wallbox_exact_counter_needs_integral_guard(source):
    source = str(source or "").strip().lower()
    if not source:
        return False
    return any(
        token in source
        for token in ("native_session_integrated", "live_wallbox_energy", "wallbox_native_detail")
    )

def _sanitize_wallbox_exact_counter(exact_kwh, integrated_kwh, source):
    exact = _float_value(exact_kwh, -1.0)
    if exact < 0.0 or exact >= 2000.0:
        return None, None
    integrated = max(0.0, _float_value(integrated_kwh, 0.0))
    if not _wallbox_exact_counter_needs_integral_guard(source):
        if integrated > 0.5:
            max_extreme = max(integrated * 3.0, integrated + 25.0)
            if exact > max_extreme:
                return integrated, {
                    "action": "extreme_integral_cap",
                    "raw_kwh": exact,
                    "integral_kwh": integrated,
                    "source": str(source or ""),
                }
        return exact, None
    if integrated <= 0.05:
        if exact > 1.0:
            return 0.0, {
                "action": "reject_without_integral",
                "raw_kwh": exact,
                "integral_kwh": integrated,
                "source": str(source or ""),
            }
        return exact, None
    max_plausible = max(integrated * 1.35, integrated + 2.0)
    if exact > max_plausible:
        return integrated, {
            "action": "integral_cap",
            "raw_kwh": exact,
            "integral_kwh": integrated,
            "source": str(source or ""),
        }
    return exact, None

def _history_exact_source(row, key):
    if key == "e_wb2":
        keys = ("e_wb2_source", "wb2_daily_source", "wb2_source")
    elif key == "e_wb":
        keys = ("e_wb_source", "wb_daily_source", "wb_source")
    else:
        return ""
    for source_key in keys:
        source = str(row.get(source_key, "") or "").strip()
        if source:
            return source
    return ""

def _exact_baselines_from_history(rows):
    keys = [
        "e_pv", "e_grid_in", "e_grid_out", "e_bat_in", "e_bat_out",
        "e_home", "e_wb", "e_wb2", "e_wp", "e_climate",
    ]
    baselines = {key: 0.0 for key in keys}
    skip_baseline = {"e_wp"} if any(_row_has_stiebel_wp_counter(row) for row in rows) else set()
    first = {}
    early_min = {}
    midnight = None
    for row in rows:
        dt = _parse_history_ts(row)
        if dt is not None:
            midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0)
            break
    if midnight is None:
        return baselines

    for row in rows:
        dt = _parse_history_ts(row)
        if dt is None:
            continue
        age = (dt - midnight).total_seconds()
        if age < 0.0 or age > 3600.0:
            continue
        for key in keys:
            if key in skip_baseline:
                continue
            if key not in row:
                continue
            val = _float_value(row.get(key), -1.0)
            if val < 0.0 or val >= 2000.0:
                continue
            if key not in first:
                if val <= 0.0:
                    continue
                first[key] = (age, val)
                early_min[key] = val
            else:
                early_min[key] = min(early_min[key], val)

    for key, (age, val) in first.items():
        min_val = early_min.get(key, val)
        # E3DC DB history can expose the previous day's closing bucket as the
        # first value after midnight. If it does not promptly reset, use it as
        # the day baseline instead of counting it as today's energy.
        if age <= 300.0 and val > 0.05 and (min_val + 0.05) >= val:
            baselines[key] = val
    return baselines

def _final_exact_and_sources_from_history(rows):
    final = {
        "e_pv": 0.0, "e_grid_in": 0.0, "e_grid_out": 0.0,
        "e_bat_in": 0.0, "e_bat_out": 0.0, "e_home": 0.0,
        "e_wb": 0.0, "e_wb2": 0.0, "e_wp": 0.0, "e_climate": 0.0,
    }
    sources = {key: "" for key in final}
    baselines = _exact_baselines_from_history(rows)
    for row in rows:
        for key in final:
            if key not in row:
                continue
            val = _float_value(row.get(key), -1.0)
            if val < 0.0 or val >= 2000.0:
                continue
            baseline = baselines.get(key, 0.0)
            if baseline > 0.0:
                val = max(0.0, val - baseline)
            if final[key] == 0.0 or val > final[key] or (final[key] - val > 5.0):
                final[key] = val
                sources[key] = _history_exact_source(row, key)
    return final, sources

def _final_exact_from_history(rows):
    final, _sources = _final_exact_and_sources_from_history(rows)
    return final

def _integrated_energy_from_history(rows):
    totals = {"pv": 0.0, "pv_dc": 0.0, "home": 0.0, "wb": 0.0, "wb2": 0.0, "wp": 0.0, "climate": 0.0}
    last = None
    for row in rows:
        if last is not None:
            try:
                ts = datetime.datetime.fromisoformat(str(row.get("ts", "")).replace("Z", "+00:00")).timestamp()
                last_ts = datetime.datetime.fromisoformat(str(last.get("ts", "")).replace("Z", "+00:00")).timestamp()
            except Exception:
                ts = last_ts = 0.0
            dt = ts - last_ts
            if 0.0 < dt < 3600.0:
                hours = dt / 3600.0
                for key in totals:
                    if key == "home":
                        p = max(0.0, (_clean_history_home_power(row) + _clean_history_home_power(last)) / 2.0)
                    elif key == "pv_dc":
                        now_dc = max(0.0, _float_value(row.get("dc0_w"), 0.0) + _float_value(row.get("dc1_w"), 0.0))
                        last_dc = max(0.0, _float_value(last.get("dc0_w"), 0.0) + _float_value(last.get("dc1_w"), 0.0))
                        p = max(0.0, (now_dc + last_dc) / 2.0)
                    else:
                        p = max(0.0, (_float_value(row.get(key), 0.0) + _float_value(last.get(key), 0.0)) / 2.0)
                    totals[key] += p * hours / 1000.0
        last = row
    return totals

def repair_recent_daily_stats(days=2, today=None):
    """Repair recent SQLite day rows from exact counters in live_history/backups.

    This catches stale rows written before the current longterm calculation was
    fixed. It intentionally only touches the latest days where raw history is
    still available.
    """
    today = today or datetime.date.today()
    conn = init_db()
    cursor = conn.cursor()
    repaired = 0
    for offset in range(max(1, int(days))):
        day = today - datetime.timedelta(days=offset)
        day_s = day.isoformat()
        rows = _history_lines_for_date(day)
        if len(rows) < 2:
            continue
        exact, exact_sources = _final_exact_and_sources_from_history(rows)
        integrated = _integrated_energy_from_history(rows)
        if keep_integrated_pv_total_for_external_ac(integrated.get("pv", 0.0), integrated.get("pv_dc", 0.0), exact["e_pv"]):
            pv = integrated.get("pv", 0.0)
            pv_source = "integrated_total_with_external_ac"
        else:
            pv = exact["e_pv"] if exact["e_pv"] > 0 else integrated.get("pv", 0.0)
            pv_source = "exact_e3dc_counter" if exact["e_pv"] > 0 else "power_integral"
        grid_in = exact["e_grid_in"]
        grid_out = exact["e_grid_out"]
        bat_in = exact["e_bat_in"]
        bat_out = exact["e_bat_out"]
        wb = exact["e_wb"] if exact["e_wb"] > 0 else integrated["wb"]
        wb2 = exact["e_wb2"] if exact["e_wb2"] > 0 else integrated["wb2"]
        if exact["e_wb"] > 0:
            wb, _wb_sanity = _sanitize_wallbox_exact_counter(exact["e_wb"], integrated["wb"], exact_sources.get("e_wb", ""))
            if wb is None:
                wb = integrated["wb"]
        if exact["e_wb2"] > 0:
            wb2, _wb2_sanity = _sanitize_wallbox_exact_counter(exact["e_wb2"], integrated["wb2"], exact_sources.get("e_wb2", ""))
            if wb2 is None:
                wb2 = integrated["wb2"]
        wp = exact["e_wp"] if exact["e_wp"] > 0 else integrated["wp"]
        climate = exact["e_climate"] if exact["e_climate"] > 0 else integrated["climate"]
        home = exact["e_home"] if exact["e_home"] > 0 else integrated["home"]
        if exact["e_home"] > 0:
            home = _clean_exact_home_energy(exact["e_home"], wb, wb2, wp, climate)
        if pv <= 0 and grid_in <= 0 and grid_out <= 0 and home <= 0:
            continue
        old = cursor.execute(
            "SELECT home_consumption, wb_consumption, wb2_consumption, wp_consumption, climate_consumption FROM daily_stats WHERE date=?",
            (day_s,),
        ).fetchone()
        if old is None:
            continue
        old_home = _float_value(old[0], 0.0)
        old_wb = _float_value(old[1], 0.0)
        old_wb2 = _float_value(old[2], 0.0)
        old_wp = _float_value(old[3], 0.0)
        old_climate = _float_value(old[4], 0.0)
        if (
            abs(old_home - home) < 0.05
            and abs(old_wb - wb) < 0.05
            and abs(old_wb2 - wb2) < 0.05
            and abs(old_wp - wp) < 0.05
            and abs(old_climate - climate) < 0.05
        ):
            continue
        pv = export_backed_pv_total(
            pv,
            grid_out,
            bat_out,
            exact_counter_present=exact["e_pv"] > 0,
            source=pv_source,
        )
        total_consumption = max(0.001, home + wb + wb2 + wp + climate)
        autarky = round(max(0.0, min(100.0, ((total_consumption - grid_in) / total_consumption) * 100.0)), 1)
        self_con = round(max(0.0, min(100.0, ((max(0.001, pv) - grid_out) / max(0.001, pv)) * 100.0)), 1)
        cursor.execute(
            """
            UPDATE daily_stats
               SET pv_yield=?,
                   home_consumption=?,
                   grid_in=?,
                   grid_out=?,
                   bat_in=?,
                   bat_out=?,
	                   wb_consumption=?,
	                   wb2_consumption=?,
	                   wp_consumption=?,
	                   climate_consumption=?,
	                   autarky=?,
	                   self_con=?
             WHERE date=?
            """,
            (
                round(pv, 3), round(home, 3), round(grid_in, 3), round(grid_out, 3),
                round(bat_in, 3), round(bat_out, 3), round(wb, 3), round(wb2, 3),
                round(wp, 3), round(climate, 3), autarky, self_con, day_s,
            ),
        )
        repaired += 1
        print(f"  [FIX] daily_stats {day_s}: Hausverbrauch {old_home:.2f} -> {home:.2f} kWh aus History/RSCP korrigiert.")
    conn.commit()
    conn.close()
    return repaired

def repair_wallbox_exact_counter_rollout_once(days=30, today=None):
    """Run the broader wallbox counter repair once after this rollout."""
    marker = os.path.join(DB_DIR, ".wallbox_exact_counter_sanity_20260623.done")
    if os.path.exists(marker):
        return 0
    repaired = repair_recent_daily_stats(days=days, today=today)
    try:
        os.makedirs(DB_DIR, exist_ok=True)
        with open(marker, "w", encoding="utf-8") as f:
            f.write(datetime.datetime.now().isoformat() + f"\nrepaired={repaired}\n")
        os.chmod(marker, 0o664)
    except Exception as exc:
        print(f"  [WARN] Wallbox-Zähler-Reparaturmarker konnte nicht geschrieben werden: {exc}")
    return repaired

def import_ertrag_file(filepath, file_date, cursor):
    """Hilfsfunktion zum Einlesen einer einzelnen Ertrag.txt Datei."""
    count = 0
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()

        for line in lines:
            if "Day Prognose" in line or "30Tage" in line or "Zeit Stat" in line:
                continue

            parts = line.split()
            if len(parts) >= 14 and parts[4] == '/' and parts[8] == '/':
                try:
                    time_gmt = float(parts[0])
                    pv_prog_pct = float(parts[5].replace('%', ''))
                    pv_real_pct = float(parts[6].replace('%', ''))
                    home_kwh_cum = float(parts[9])
                    wp_kwh_cum = float(parts[10])
                    temp_c = float(parts[11].replace('°', ''))
                    grid_kwh_cum = float(parts[13])

                    record_id = f"{file_date.isoformat()}_{time_gmt:.2f}"

                    cursor.execute('''
                        INSERT OR REPLACE INTO ml_training_data
                        (id, date, time_gmt, pv_prog_pct, pv_real_pct, home_kwh_cum, wp_kwh_cum, temp_c, grid_kwh_cum)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ''', (record_id, file_date.isoformat(), time_gmt, pv_prog_pct, pv_real_pct, home_kwh_cum, wp_kwh_cum, temp_c, grid_kwh_cum))
                    count += 1
                except ValueError: pass
    except Exception: pass
    return count

def archive_ml_data():
    """Liest die Ertrag.X.txt Datei des heutigen Tages."""
    today = datetime.date.today()
    ertrag_file = os.path.join(get_install_path(), f"Ertrag.{today.day}.txt")

    if not os.path.exists(ertrag_file): return

    conn = init_db()
    cursor = conn.cursor()
    import_ertrag_file(ertrag_file, today, cursor)
    conn.commit()
    conn.close()

def backfill_ml_data():
    """Liest ALLE vergangenen Ertrag.X.txt Dateien für das ML-Training ein."""
    install_path = get_install_path()
    conn = init_db()
    cursor = conn.cursor()
    today = datetime.date.today()
    total_count = 0

    for i in range(1, 32):
        ertrag_file = os.path.join(install_path, f"Ertrag.{i}.txt")
        if os.path.exists(ertrag_file):
            month = today.month
            year = today.year
            if i > today.day:
                month -= 1
                if month == 0:
                    month = 12; year -= 1
            try:
                file_date = datetime.date(year, month, i)
            except ValueError:
                mtime = os.path.getmtime(ertrag_file)
                file_date = datetime.date.fromtimestamp(mtime)

            added = import_ertrag_file(ertrag_file, file_date, cursor)
            if added > 0:
                print(f"✓ {os.path.basename(ertrag_file)} importiert ({file_date.isoformat()}) -> {added} Einträge")
            total_count += added

    conn.commit()
    conn.close()
    print(f"\nBackfill erfolgreich! Insgesamt {total_count} 15-Minuten-Datensätze geladen.")

if __name__ == "__main__":
    if "--backfill" in sys.argv:
        print("Starte Historien-Import (Backfill) für Machine Learning...")
        backfill_ml_data()
    else:
        archive_today()
        repair_wallbox_exact_counter_rollout_once(days=30)
        repair_recent_daily_stats(days=2)
        archive_ml_data()
