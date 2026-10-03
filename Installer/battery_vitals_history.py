#!/usr/bin/env python3
"""Monatliche, rein diagnostische Batterie-Vitalhistorie."""

from __future__ import annotations

import datetime as dt
import grp
import json
import math
import os
import sqlite3
import tempfile
from typing import Any

try:
    from .ha_writer_admission import evaluate_writer_admission
except ImportError:
    from ha_writer_admission import evaluate_writer_admission

HISTORY_PATH = "/var/www/html/data/battery_vitals_history.json"
DB_PATH = "/var/www/html/data/e3dc_stats.db"
MAX_MONTHS = 240


def _number(value: Any, *, positive: bool = False) -> Any:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or (positive and number <= 0):
        return None
    return value


def _previous_month(now: dt.datetime) -> tuple[str, str, str]:
    first = now.date().replace(day=1)
    previous_last = first - dt.timedelta(days=1)
    start = previous_last.replace(day=1)
    return previous_last.strftime("%Y-%m"), start.isoformat(), previous_last.isoformat()


def _monthly_energy(db_path: str, now: dt.datetime) -> dict[str, Any]:
    month, start, end = _previous_month(now)
    result = {"month": month, "bat_in": None, "bat_out": None, "valid": False, "reason": "DAILY_HISTORY_UNAVAILABLE"}
    try:
        db = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
        try:
            rows = db.execute(
                "SELECT date, bat_in, bat_out FROM daily_stats WHERE date BETWEEN ? AND ? ORDER BY date",
                (start, end),
            ).fetchall()
        finally:
            db.close()
    except (OSError, sqlite3.Error):
        return result
    expected = (dt.date.fromisoformat(end) - dt.date.fromisoformat(start)).days + 1
    if len(rows) != expected or [row[0] for row in rows] != [
        (dt.date.fromisoformat(start) + dt.timedelta(days=i)).isoformat() for i in range(expected)
    ]:
        result["reason"] = "DAILY_HISTORY_INCOMPLETE"
        return result
    values = [(row[1], row[2]) for row in rows]
    if any(_number(value) is None or float(value) < 0 for pair in values for value in pair):
        result["reason"] = "DAILY_HISTORY_INVALID"
        return result
    result.update({
        "bat_in": round(sum(float(pair[0]) for pair in values), 6),
        "bat_out": round(sum(float(pair[1]) for pair in values), 6),
        "valid": True,
        "reason": None,
    })
    return result


def _pack_record(cabinet: dict[str, Any], pack: dict[str, Any]) -> dict[str, Any]:
    fields = {
        "soh": _number(pack.get("soh"), positive=True),
        "cycles": _number(pack.get("cycles")),
        "fcc_wh": _number(cabinet.get("fcc_wh"), positive=True),
        "usable_wh": _number(cabinet.get("usable_wh"), positive=True),
        "specified_wh": _number(cabinet.get("specified_wh"), positive=True),
        "temperature_min_c": _number(pack.get("temp_min")),
        "temperature_max_c": _number(pack.get("temp_max")),
        "cell_voltage_spread_v": _number(pack.get("voltage_spread")),
    }
    missing = [name for name, value in fields.items() if value is None]
    previous_cycles = pack.get("_previous_cycles")
    previous_capacity = pack.get("_previous_specified_wh")
    replacement = bool(
        (_number(previous_cycles) is not None and fields["cycles"] is not None and fields["cycles"] < previous_cycles)
        or (_number(previous_capacity, positive=True) is not None and fields["specified_wh"] is not None
            and abs(fields["specified_wh"] - previous_capacity) > previous_capacity * 0.2)
    )
    return {
        "cabinet": int(cabinet["index"]),
        "module": int(pack["index"]),
        **fields,
        "valid": not missing,
        "reason": None if not missing else "MISSING_OR_INVALID:" + ",".join(missing),
        "module_replacement_suspected": replacement,
    }


def build_month_record(vitals: dict[str, Any], now: dt.datetime, db_path: str,
                       previous_packs: dict[tuple[Any, Any], dict[str, Any]] | None = None) -> dict[str, Any]:
    packs = []
    cabinets = []
    for cabinet in vitals.get("cabinets", []) if isinstance(vitals, dict) else []:
        if not isinstance(cabinet, dict):
            continue
        if isinstance(cabinet.get("index"), int):
            soh = _number(cabinet.get("soh_avg"), positive=True)
            if soh is not None and soh > 110:
                soh = None
            approximate = bool(cabinet.get("soh_approx")) and soh is not None
            cabinets.append({
                "cabinet": cabinet["index"],
                "soh_avg": soh,
                "soh_source": ("BAT_ASOC" if approximate else "BAT_DCB_SOH") if soh is not None else None,
                "soh_approx": approximate,
                "soh_quality": ("approximate" if approximate else "measured") if soh is not None else "unavailable",
                "soh_reason": ("BAT_ASOC_APPROX" if approximate else "BAT_DCB_SOH") if soh is not None else "SOH_NOT_AVAILABLE",
            })
        for pack in cabinet.get("packs", []):
            if isinstance(pack, dict) and isinstance(cabinet.get("index"), int) and isinstance(pack.get("index"), int):
                sample = dict(pack)
                old = (previous_packs or {}).get((cabinet.get("index"), pack.get("index")), {})
                sample["_previous_cycles"] = old.get("cycles")
                sample["_previous_specified_wh"] = old.get("specified_wh")
                packs.append(_pack_record(cabinet, sample))
    return {
        "month": now.strftime("%Y-%m"),
        "timestamp": now.astimezone(dt.timezone.utc).isoformat().replace("+00:00", "Z"),
        "valid": bool(packs) and all(pack["valid"] for pack in packs),
        "reason": None if packs and all(pack["valid"] for pack in packs) else ("NO_PACKS" if not packs else "PACK_DATA_INVALID"),
        "previous_month_energy": _monthly_energy(db_path, now),
        "packs": packs,
        "cabinets": cabinets,
    }


def _atomic_write(path: str, payload: dict[str, Any]) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".battery_vitals_history.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o664)
        try:
            os.chown(temporary, -1, grp.getgrnam("www-data").gr_gid)
        except (KeyError, OSError):
            pass
        os.replace(temporary, path)
        directory_fd = os.open(directory, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def record_monthly(vitals: dict[str, Any], *, now: dt.datetime | None = None,
                   history_path: str = HISTORY_PATH, db_path: str = DB_PATH,
                   admission=None) -> dict[str, Any]:
    now = now or dt.datetime.now().astimezone()
    decision = admission if admission is not None else evaluate_writer_admission()
    if not decision.get("allowed"):
        return {"written": False, "reason": decision.get("reason", "ROLE_NOT_ALLOWED")}
    history = {"schema": "battery_vitals_history_v1", "months": []}
    try:
        with open(history_path, "r", encoding="utf-8") as stream:
            loaded = json.load(stream)
        if isinstance(loaded, dict) and isinstance(loaded.get("months"), list):
            history = loaded
    except FileNotFoundError:
        pass
    except (OSError, ValueError):
        return {"written": False, "reason": "HISTORY_INVALID"}
    month = now.strftime("%Y-%m")
    if any(item.get("month") == month for item in history["months"] if isinstance(item, dict)):
        return {"written": False, "reason": "MONTH_ALREADY_RECORDED"}
    previous = history["months"][-1] if history["months"] else None
    lookup = {}
    if isinstance(previous, dict):
        lookup = {(p.get("cabinet"), p.get("module")): p for p in previous.get("packs", []) if isinstance(p, dict)}
    history["months"].append(build_month_record(vitals, now, db_path, lookup))
    history["months"] = history["months"][-MAX_MONTHS:]
    _atomic_write(history_path, history)
    return {"written": True, "reason": None, "month": month}
