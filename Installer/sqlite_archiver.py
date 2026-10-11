#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
from contextlib import closing
import argparse
import hashlib
import shlex
import subprocess
import tempfile
import time
from zoneinfo import ZoneInfo
import math
import sqlite3
import os
import stat
import datetime
import sys
from pathlib import Path

try:
    from .config_secret_permissions import config_secret_dir_mode
except ImportError:  # Direkter Aufruf durch Scheduler oder Konsole.
    from config_secret_permissions import config_secret_dir_mode

# Standard-Ausgabe auf UTF-8 erzwingen (verhindert UnicodeEncodeError z.B. bei cron oder SSH ohne Locale)
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

try:
    from . import cost_accounting
except ImportError:
    import cost_accounting

DB_DIR = "/var/www/html/data"
DB_PATH = os.path.join(DB_DIR, "e3dc_stats.db")
JSON_PATH = "/var/www/html/ramdisk/daily_stats.json"
HISTORY_PATH = "/var/www/html/ramdisk/live_history.txt"
HISTORY_BACKUP_DIR = "/var/www/html/data/history_backups"

DAILY_STATS_MIGRATION_COLUMNS = (
    'cost_total', 'cost_home', 'cost_bat', 'cost_wb', 'cost_wp',
    'wb2_consumption', 'cost_wb2', 'climate_consumption', 'cost_climate',
    'pv_balance_rest', 'bat_balance_rest', 'balance_unknown_rest',
    'saved_u', 'saved_td', 'saved_wb', 'pv_e3dc', 'pv_external',
    'pv_source_rest', 'pv_grid', 'bat_grid',
)


def _ensure_mode_if_needed(path, desired_mode):
    """Ändert Dateirechte nur, wenn der bestehende Modus wirklich abweicht."""
    try:
        current_mode = stat.S_IMODE(os.stat(path).st_mode)
        if current_mode != int(desired_mode):
            os.chmod(path, int(desired_mode))
            return True
    except OSError:
        return False
    return False


def _upsert_changed(cursor, table, key_column, columns, values):
    """UPSERT ohne SQLite-Schreibvorgang bei identischem Zeileninhalt."""
    identifiers = (table, key_column, *columns)
    if not all(str(value).replace('_', '').isalnum() for value in identifiers):
        raise ValueError("Ungültiger SQLite-Bezeichner")
    update_columns = tuple(column for column in columns if column != key_column)
    placeholders = ', '.join('?' for _ in columns)
    assignments = ', '.join(
        f"{column}=excluded.{column}" for column in update_columns
    )
    changed_where = ' OR '.join(
        f"{table}.{column} IS NOT excluded.{column}" for column in update_columns
    )
    cursor.execute(
        f"""
        INSERT INTO {table} ({', '.join(columns)})
        VALUES ({placeholders})
        ON CONFLICT({key_column}) DO UPDATE SET {assignments}
        WHERE {changed_where}
        """,
        tuple(values),
    )
    return cursor.rowcount > 0

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
    conn = sqlite3.connect(DB_PATH, timeout=30)
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

    # Automatische Migration nur für tatsächlich fehlende Spalten.
    existing_columns = {
        str(row[1]) for row in cursor.execute("PRAGMA table_info(daily_stats)")
    }
    for col in DAILY_STATS_MIGRATION_COLUMNS:
        if col not in existing_columns:
            cursor.execute(f"ALTER TABLE daily_stats ADD COLUMN {col} REAL DEFAULT 0")

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

# Die Berechnung bleibt in helpers.php; Python übernimmt nur Speicherung und Wartung.
DAILY_STATS_HELPER = "/var/www/html/daily_stats_cli.php"
MAINTENANCE_COLUMNS = (
    'home_consumption', 'pv_yield', 'grid_in', 'grid_out', 'bat_in', 'bat_out',
    'wb_consumption', 'wb2_consumption', 'wp_consumption', 'climate_consumption',
    'pv_e3dc', 'pv_external', 'pv_source_rest', 'pv_grid', 'bat_grid',
    'pv_balance_rest', 'bat_balance_rest', 'balance_unknown_rest',
    'cost_total', 'cost_home', 'cost_bat', 'cost_wb', 'cost_wb2', 'cost_wp',
    'cost_climate', 'autarky', 'self_con',
)


def calculate_history_stats(rows):
    """Dieselbe Berechnung wie die historische Tagesansicht, ohne HTTP-Zugriff."""
    result = subprocess.run(
        ['php', DAILY_STATS_HELPER],
        input=''.join(json.dumps(row, allow_nan=False) + '\n' for row in rows),
        capture_output=True, text=True, timeout=60, check=True,
    )
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or data.get('sources', {}).get('home_energy_source') is None:
        raise ValueError('Web-Bilanzberechnung fehlt oder liefert ungültige Daten')
    return data


def _nullable_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _daily_values(data):
    """Feldzuordnung ohne Ersatz fehlender Messungen oder Kosten durch Null."""
    stats, costs = data['stats'], data.get('costs', {})
    values = {column: _nullable_number(stats.get(key)) for column, key in {
        'home_consumption': 'total_home_kwh', 'grid_in': 'total_grid_in_kwh',
        'grid_out': 'total_grid_out_kwh', 'bat_in': 'total_bat_in_kwh',
        'bat_out': 'total_bat_out_kwh', 'wb_consumption': 'total_wb_kwh',
        'wb2_consumption': 'total_wb2_kwh', 'wp_consumption': 'total_wp_kwh',
        'climate_consumption': 'total_climate_kwh', 'pv_e3dc': 'pv_e3dc_kwh',
        'pv_external': 'pv_external_kwh', 'pv_source_rest': 'pv_source_rest_kwh',
        'pv_grid': 'pv_grid_kwh', 'bat_grid': 'bat_grid_kwh',
    }.items()}
    values.update(pv_yield=_nullable_number(data.get('pv_today_kwh')),
                  autarky=_nullable_number(data.get('autarky_day')),
                  self_con=_nullable_number(data.get('selfcon_day')))
    for key in ('total', 'home', 'bat', 'wb', 'wb2', 'wp', 'climate'):
        values['cost_' + key] = _nullable_number(costs.get(key))
    # Bilanzreste nur aus vollständig bekannten Flüssen bestimmen.
    def rest(total, keys):
        parts = [_nullable_number(stats.get(key)) for key in keys]
        return (round(max(0.0, total - sum(parts)), 3)
                if total is not None and all(part is not None for part in parts) else None)
    values['pv_balance_rest'] = rest(values['pv_yield'],
        ['pv_' + key + '_kwh' for key in ('home', 'wb', 'wb2', 'wp', 'climate', 'bat', 'grid')])
    values['bat_balance_rest'] = rest(values['bat_out'],
        ['bat_' + key + '_kwh' for key in ('home', 'wb', 'wb2', 'wp', 'climate', 'grid')])
    known = [values[key] for key in ('pv_yield', 'grid_in', 'bat_out', 'home_consumption',
        'wb_consumption', 'wb2_consumption', 'wp_consumption', 'climate_consumption',
        'grid_out', 'bat_in', 'pv_balance_rest', 'bat_balance_rest')]
    values['balance_unknown_rest'] = (round(max(0.0, sum(known[:3]) - sum(known[3:])), 3)
                                      if all(v is not None for v in known) else None)
    saved = data.get('saved', {})
    for col, key in (('saved_u', 'total_today_kwh'), ('saved_td', 'derating_today_kwh'),
                     ('saved_wb', 'inverter_today_kwh')):
        if key in saved or col in data:
            values[col] = _nullable_number(saved.get(key, data.get(col)))
    return values


def archive_today(conn=None):
    """Archiviert ausschließlich den laufenden Tag mit der gemeinsamen Webbilanz."""
    today = datetime.date.today()
    rows = _history_lines_for_date(today)
    cached = {}
    try:
        if datetime.date.fromtimestamp(os.path.getmtime(JSON_PATH)) == today:
            with open(JSON_PATH, encoding='utf-8') as stream:
                cached = json.load(stream)
    except (OSError, ValueError):
        pass
    if not isinstance(cached, dict):
        cached = {}
    if len(rows) >= 2:
        data = calculate_history_stats(rows)
        # Zusatzstatistik stammt weiterhin aus dem Tageserzeuger.
        for key in ('saved', 'saved_u', 'saved_td', 'saved_wb'):
            if key in cached:
                data[key] = cached[key]
    elif cached.get('sources', {}).get('home_energy_source'):
        data = cached
    else:
        print('Archiv unverändert: keine aktuelle Webbilanz oder Tageshistorie.')
        return 0
    values = _daily_values(data)
    if values['home_consumption'] is None:
        print('Archiv: Hausverbrauch unbekannt: ' + str(data['sources']['home_energy_source']))
    owns_connection = conn is None
    db_conn = init_db() if owns_connection else conn
    try:
        if owns_connection:
            db_conn.execute('BEGIN IMMEDIATE')
        columns = ('date', *values)
        changed = _upsert_changed(db_conn.cursor(), 'daily_stats', 'date', columns,
                                  (today.isoformat(), *values.values()))
        if owns_connection:
            db_conn.commit()
    finally:
        if owns_connection:
            db_conn.close()
    _ensure_mode_if_needed(DB_PATH, 0o664)
    _ensure_mode_if_needed(DB_DIR, config_secret_dir_mode())
    return int(changed)


def repair_recent_daily_stats(days=1, today=None, conn=None):
    """Kompatibler Einstieg: vergangene Tage nur über die explizite Wartung ändern."""
    if today is not None and today != datetime.date.today():
        return 0
    return archive_today(conn=conn)


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
    # Doppelte Ortsstunden nach ihrem tatsächlichen Zeitpunkt ordnen.
    rows.sort(key=lambda r: (_parse_history_ts(r).timestamp()
                            if _parse_history_ts(r) is not None else float("-inf")))
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

def _valid_daily_energy(value):
    """Fehlende, nicht endliche oder negative Tagesenergie bleibt unbekannt."""
    if isinstance(value, bool):
        return None
    value = _float_value(value, float("nan"))
    return value if math.isfinite(value) and 0.0 <= value < 2000.0 else None


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
            val = _valid_daily_energy(row.get(key))
            if val is None:
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
        "e_pv": None, "e_grid_in": None, "e_grid_out": None,
        "e_bat_in": None, "e_bat_out": None, "e_home": None,
        "e_wb": None, "e_wb2": None, "e_wp": None, "e_climate": None,
    }
    sources = {key: "exact_counter_missing_or_invalid" for key in final}
    baselines = _exact_baselines_from_history(rows)
    last_seen = {}
    latest = max((dt.timestamp() for row in rows
                  if (dt := _parse_history_ts(row)) is not None), default=None)
    for row in rows:
        for key in final:
            if key not in row:
                continue
            val = _valid_daily_energy(row.get(key))
            if val is None:
                continue
            dt = _parse_history_ts(row)
            if dt is not None:
                last_seen[key] = dt
            baseline = baselines.get(key, 0.0)
            if baseline > 0.0:
                # Ein unveränderter Mitternachtsrest bestätigt keine heutige Null.
                if val <= baseline:
                    if final[key] is None:
                        sources[key] = "midnight_counter_unconfirmed"
                    continue
                val -= baseline
            if final[key] is None or val > final[key] or (final[key] - val > 5.0):
                final[key] = val
                sources[key] = _history_exact_source(row, key)
    for key, dt in last_seen.items():
        midnight = dt.replace(hour=0, minute=0, second=0, microsecond=0).timestamp()
        if dt.timestamp() - midnight <= 300.0 and latest is not None and latest > midnight + 300.0:
            final[key] = None
            sources[key] = "midnight_counter_unconfirmed"
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
        _ensure_mode_if_needed(marker, 0o664)
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

                    columns = (
                        'id', 'date', 'time_gmt', 'pv_prog_pct', 'pv_real_pct',
                        'home_kwh_cum', 'wp_kwh_cum', 'temp_c', 'grid_kwh_cum',
                    )
                    values = (
                        record_id, file_date.isoformat(), time_gmt, pv_prog_pct,
                        pv_real_pct, home_kwh_cum, wp_kwh_cum, temp_c,
                        grid_kwh_cum,
                    )
                    if _upsert_changed(
                        cursor,
                        'ml_training_data',
                        'id',
                        columns,
                        values,
                    ):
                        count += 1
                except ValueError: pass
    except Exception: pass
    return count

def archive_ml_data(conn=None):
    """Liest die Ertrag.X.txt Datei des heutigen Tages."""
    today = datetime.date.today()
    ertrag_file = os.path.join(get_install_path(), f"Ertrag.{today.day}.txt")

    if not os.path.exists(ertrag_file):
        return 0

    owns_connection = conn is None
    db_conn = init_db() if owns_connection else conn
    changed = import_ertrag_file(ertrag_file, today, db_conn.cursor())
    if owns_connection:
        db_conn.commit()
        db_conn.close()
    return changed


def run_hourly_archive():
    """Bündelt reguläre Stundenwerte in einer gemeinsamen SQLite-Transaktion."""
    conn = init_db()
    try:
        conn.execute('BEGIN IMMEDIATE')
        archive_today(conn=conn)
        archive_ml_data(conn=conn)
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    else:
        # Optionale Kostenfehler dürfen das festgeschriebene Kernarchiv nicht berühren.
        try:
            cost_accounting.archive_costs(conn, Path(DB_DIR).parent)
        except Exception as exc:
            print(f"  [WARN] Kostenhistorie übersprungen ({type(exc).__name__}): {exc}")
        try:
            cost_accounting.automatic_backfill(sys.modules[__name__], conn)
        except Exception as exc:
            print(f"  [WARN] Nachberechnung übersprungen ({type(exc).__name__}): {exc}")
    finally:
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

def _read_complete_backup(day):
    """Vollständigkeit anhand Erzeugerformat, Tagesrändern und Intervallabdeckung."""
    path = Path(HISTORY_BACKUP_DIR) / ('history_' + day.isoformat() + '.txt')
    if not path.is_file():
        return None, 'Sicherung fehlt'
    try:
        raw = path.read_bytes()
        rows = [json.loads(line) for line in raw.decode('utf-8').splitlines() if line.strip()]
        if len(rows) < 2:
            return None, 'Sicherung unvollständig: weniger als zwei Messpunkte'
        zone = ZoneInfo('Europe/Berlin')
        start = datetime.datetime.combine(day, datetime.time(), zone).timestamp()
        end = datetime.datetime.combine(day + datetime.timedelta(days=1), datetime.time(), zone).timestamp()
        stamps = []
        for row in rows:
            stamp = _parse_history_ts(row)
            if stamp is None:
                raise ValueError('ungültiger Zeitstempel')
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=zone)
            if stamp.astimezone(zone).date() != day:
                raise ValueError('fremder Kalendertag')
            stamps.append(stamp.timestamp())
        if stamps[0] - start > 300 or end - stamps[-1] > 300:
            return None, 'Sicherung unvollständig: Tagesanfang oder Tagesende fehlt'
        if any(not 0 < b - a < 3600 for a, b in zip(stamps, stamps[1:])):
            return None, 'Sicherung unvollständig: Zeitlücke oder ungeordnete Messpunkte'
        return rows, None
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        return None, 'Sicherung ungültig: ' + str(exc)


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, 'rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def _readonly_database(path):
    return sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro', uri=True, timeout=30)


def _verified_statistics_backup(changes, *, cost_only=False, deadline=None):
    """Konsistentes SQLite-Backup, vor der ersten Änderung dauerhaft geprüft."""
    if cost_only:
        return cost_accounting._verified_cost_backup(sys.modules[__name__], deadline=deadline)
    directory = Path(DB_DIR) / 'statistics_backups'
    directory.mkdir(mode=0o770, exist_ok=True)
    backup = Path(tempfile.mkdtemp(prefix='tagesbilanz-', dir=directory))
    os.chmod(backup, 0o770)
    snapshot = backup / 'e3dc_stats.db'
    deadline = time.monotonic() + 30
    def progress(_status, _remaining, _total):
        if time.monotonic() > deadline:
            raise TimeoutError('SQLite-Backup überschreitet 30 Sekunden')
    # Die Wartungsverbindung hält BEGIN IMMEDIATE; die zweite Verbindung liest
    # den unveränderten, konsistenten Stand einschließlich eines vorhandenen WAL.
    with closing(_readonly_database(DB_PATH)) as source, closing(sqlite3.connect(snapshot)) as destination:
        source.backup(destination, pages=256, progress=progress, sleep=0.05)
        if destination.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
            raise RuntimeError('SQLite-Backup ist nicht konsistent')
        for change in changes:
            columns = tuple(change['vorher'])
            row = destination.execute('SELECT ' + ', '.join(columns) +
                ' FROM daily_stats WHERE date=?', (change['tag'],)).fetchone()
            if row != tuple(change['vorher'].values()):
                raise RuntimeError('SQLite-Backup stimmt nicht mit dem Ausgangsstand überein')
    os.chmod(snapshot, 0o660)
    with snapshot.open('rb') as stream:
        os.fsync(stream.fileno())
    manifest = {'format': 'tagesbilanz_backup_v1', 'sha256': _sha256_file(snapshot),
                'datenbank': str(Path(DB_PATH).resolve()), 'tage': changes}
    manifest_path = backup / 'backup.json'
    with manifest_path.open('x', encoding='utf-8') as stream:
        json.dump(manifest, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(manifest_path, 0o660)
    for folder in (backup, directory):
        descriptor = os.open(folder, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    if _sha256_file(snapshot) != manifest['sha256']:
        raise RuntimeError('Prüfsumme des SQLite-Backups stimmt nicht')
    return backup


def recalculate_statistics(*, apply=False, days=30, today=None):
    """Explizite Wartung für höchstens 30 abgeschlossene Tage, niemals beim Start."""
    if not 1 <= days <= 30:
        raise ValueError('Erlaubt sind 1 bis 30 Tage')
    today = today or datetime.datetime.now(ZoneInfo('Europe/Berlin')).date()
    report = {'modus': 'anwenden' if apply else 'trockenlauf', 'tage': [],
              'geändert': 0, 'backup': None, 'rückweg': None}
    if not Path(DB_PATH).is_file():
        raise FileNotFoundError('Statistikdatenbank fehlt: ' + DB_PATH)
    conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + '?mode=rw', uri=True, timeout=30) if apply else _readonly_database(DB_PATH)
    try:
        # Vorhandene SQLite-Sperre statt einer unabhängigen Sperrdatei.
        conn.execute('BEGIN IMMEDIATE' if apply else 'BEGIN')
        columns = ', '.join(MAINTENANCE_COLUMNS)
        changes = []
        for offset in range(1, days + 1):
            day = today - datetime.timedelta(days=offset)
            entry = {'tag': day.isoformat()}
            report['tage'].append(entry)
            old = conn.execute('SELECT ' + columns + ' FROM daily_stats WHERE date=?',
                               (day.isoformat(),)).fetchone()
            rows, reason = _read_complete_backup(day)
            if reason or old is None:
                entry.update(status='übersprungen', grund=reason or 'Statistikzeile fehlt')
                continue
            data = calculate_history_stats(rows)
            values = _daily_values(data)
            if values['home_consumption'] is None:
                entry.update(status='übersprungen', grund=data['sources']['home_energy_source'])
                continue
            before = dict(zip(MAINTENANCE_COLUMNS, old))
            after = {key: values[key] for key in MAINTENANCE_COLUMNS}
            entry.update(vorher=before, nachher=after,
                         differenz={key: round(after[key] - before[key], 6)
                             if before[key] is not None and after[key] is not None else None
                             for key in MAINTENANCE_COLUMNS},
                         quellen=data['sources'])
            if before == after:
                entry['status'] = 'unverändert'
                continue
            entry['status'] = 'geplant'
            changes.append(entry)
        if apply and changes:
            backup = _verified_statistics_backup(changes)
            report['backup'] = str(backup)
            report['rückweg'] = shlex.join([sys.executable, '-B', str(Path(__file__).resolve()),
                '--wiederherstellen', str(backup), '--web-root', str(Path(DB_DIR).parent)])
            for change in changes:
                assignments = ', '.join(key + '=?' for key in MAINTENANCE_COLUMNS)
                conn.execute('UPDATE daily_stats SET ' + assignments + ' WHERE date=?',
                    (*change['nachher'].values(), change['tag']))
            conn.commit()
            for change in changes:
                change['status'] = 'geändert'
            report['geändert'] = len(changes)
        else:
            conn.rollback()
    finally:
        conn.close()
    return report


def restore_statistics(backup):
    """Setzt betroffene Spalten transaktional zurück; spätere andere Daten bleiben."""
    backup = Path(backup)
    manifest = json.loads((backup / 'backup.json').read_text(encoding='utf-8'))
    snapshot = backup / 'e3dc_stats.db'
    if manifest.get('format') != 'tagesbilanz_backup_v1' or _sha256_file(snapshot) != manifest.get('sha256'):
        raise ValueError('Backupformat oder Prüfsumme ungültig')
    if manifest.get('datenbank') != str(Path(DB_PATH).resolve()):
        raise ValueError('Backup gehört zu einer anderen Statistikdatenbank')
    conn = sqlite3.connect(Path(DB_PATH).resolve().as_uri() + '?mode=rw', uri=True, timeout=30)
    try:
        conn.execute('BEGIN IMMEDIATE')
        pending = []
        with closing(_readonly_database(snapshot)) as source:
            if source.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                raise ValueError('Backup ist nicht konsistent')
            for change in manifest['tage']:
                columns = tuple(change['vorher'])
                if columns != MAINTENANCE_COLUMNS:
                    raise ValueError('Backup enthält unerwartete Spalten')
                query = 'SELECT ' + ', '.join(columns) + ' FROM daily_stats WHERE date=?'
                before = source.execute(query, (change['tag'],)).fetchone()
                if before != tuple(change['vorher'].values()):
                    raise ValueError('Backupwerte und Beschreibung weichen ab')
                current = conn.execute(query, (change['tag'],)).fetchone()
                if current == before:
                    continue
                if current != tuple(change['nachher'].values()):
                    raise ValueError('Tageswerte inzwischen verändert: ' + change['tag'])
                pending.append((change['tag'], before))
        for day, before in pending:
            conn.execute('UPDATE daily_stats SET ' + ', '.join(key + '=?' for key in MAINTENANCE_COLUMNS)
                         + ' WHERE date=?', (*before, day))
        conn.commit()
        return {'wiederhergestellt': len(pending), 'backup': str(backup)}
    finally:
        conn.close()


def main():
    parser = argparse.ArgumentParser(description='Tagesarchiv und ausdrücklich aufgerufene Statistikwartung')
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--kosten-nachberechnen', action='store_true', help='Kosten der letzten 30 Tage ergänzen')
    action.add_argument('--kosten-wiederherstellen', metavar='BACKUP')
    action.add_argument('--nachberechnen', action='store_true', help='Letzte 30 abgeschlossene Tage prüfen')
    action.add_argument('--wiederherstellen', metavar='BACKUP')
    action.add_argument('--backfill', action='store_true', help='Bestehender ML-Historienimport')
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument('--trockenlauf', action='store_true', help='Standard bei --nachberechnen; schreibt nichts')
    mode.add_argument('--anwenden', action='store_true', help='Nach verifiziertem Backup anwenden')
    parser.add_argument('--web-root', default='/var/www/html', help='Web-Verzeichnis der laufenden Instanz')
    args = parser.parse_args()
    if (args.anwenden or args.trockenlauf) and not (args.nachberechnen or args.kosten_nachberechnen):
        parser.error('--anwenden/--trockenlauf benötigen --nachberechnen oder --kosten-nachberechnen')
    global DB_DIR, DB_PATH, JSON_PATH, HISTORY_PATH, HISTORY_BACKUP_DIR, DAILY_STATS_HELPER
    root = Path(args.web_root).resolve()
    DB_DIR, DB_PATH = str(root / 'data'), str(root / 'data/e3dc_stats.db')
    JSON_PATH, HISTORY_PATH = str(root / 'ramdisk/daily_stats.json'), str(root / 'ramdisk/live_history.txt')
    HISTORY_BACKUP_DIR, DAILY_STATS_HELPER = str(root / 'data/history_backups'), str(root / 'daily_stats_cli.php')
    try:
        if args.kosten_nachberechnen:
            result = cost_accounting.maintain(sys.modules[__name__], apply=args.anwenden)
        elif args.kosten_wiederherstellen:
            result = cost_accounting.restore(sys.modules[__name__], args.kosten_wiederherstellen)
        elif args.nachberechnen:
            result = recalculate_statistics(apply=args.anwenden)
        elif args.wiederherstellen:
            result = restore_statistics(args.wiederherstellen)
        elif args.backfill:
            backfill_ml_data()
            return 0
        else:
            run_hourly_archive()
            return 0
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (OSError, ValueError, RuntimeError, sqlite3.Error, subprocess.SubprocessError) as exc:
        print('Statistik unverändert oder Transaktion zurückgerollt: ' + str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
