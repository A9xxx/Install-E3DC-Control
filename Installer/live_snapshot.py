#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kompakter read-only Laufzeitvertrag für interne Live-Konsumenten.

Der Web-Sampler bleibt eine UI-Projektion und wird nicht von Diensten per HTTP
aufgerufen. Interne Leser verwenden den atomaren RSCP-Snapshot sowie optionale
frische Companion-Snapshots aus der RAM-Disk.
"""

from __future__ import annotations

import copy
import json
import math
import os
import stat
import sys
import threading
import time
from collections import OrderedDict
from typing import Any, Dict, Optional, Tuple


LIVE_DATA_PATH = "/var/www/html/ramdisk/live_data_py.json"
WALLBOX_DATA_PATH = "/var/www/html/ramdisk/wallbox_native.json"
WEB_SNAPSHOT_PATH = "/var/www/html/ramdisk/get_live_json_snapshot.json"
_BOUND_JSON_CACHE_LIMIT = 8
_BOUND_JSON_CACHE: "OrderedDict[Tuple[str, Tuple[int, int, int, int, int]], Any]" = OrderedDict()
_BOUND_JSON_CACHE_LOCK = threading.Lock()


def _generation(info: os.stat_result) -> Tuple[int, int, int, int, int]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def read_bound_json_value(
    path: str,
    *,
    max_age_s: Optional[float],
    max_bytes: int,
    copy_data: bool = True,
) -> Tuple[Any, Dict[str, Any]]:
    """Liest genau eine reguläre, unveränderte und ausreichend frische Datei."""

    normalized = os.path.abspath(os.fspath(path))
    try:
        current = os.stat(normalized, follow_symlinks=False)
    except OSError:
        return None, {"valid": False, "reason": "source_unreadable"}
    if (
        not stat.S_ISREG(current.st_mode)
        or int(current.st_nlink) != 1
        or current.st_size <= 1
        or current.st_size > int(max_bytes)
    ):
        return None, {"valid": False, "reason": "source_not_regular_or_bounded"}
    clock_delta_s = time.time() - float(current.st_mtime)
    if clock_delta_s < -5.0:
        return None, {
            "valid": False,
            "reason": "source_mtime_in_future",
            "clock_delta_s": clock_delta_s,
        }
    age_s = max(0.0, clock_delta_s)
    if max_age_s is not None and age_s > max(0.0, float(max_age_s)):
        return None, {
            "valid": False,
            "reason": "source_stale",
            "age_s": age_s,
        }
    expected_generation = _generation(current)
    cache_key = (normalized, expected_generation)
    with _BOUND_JSON_CACHE_LOCK:
        cached = _BOUND_JSON_CACHE.get(cache_key)
        if cached is not None:
            try:
                verified = os.stat(normalized, follow_symlinks=False)
            except OSError:
                verified = None
            if (
                verified is not None
                and stat.S_ISREG(verified.st_mode)
                and int(verified.st_nlink) == 1
                and _generation(verified) == expected_generation
            ):
                _BOUND_JSON_CACHE.move_to_end(cache_key)
                return (copy.deepcopy(cached) if copy_data else cached), {
                    "valid": True,
                    "reason": "ok",
                    "age_s": age_s,
                    "generation": expected_generation,
                    "from_cache": True,
                }
            _BOUND_JSON_CACHE.pop(cache_key, None)

    descriptor = -1
    try:
        descriptor = os.open(
            normalized,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or int(before.st_nlink) != 1
            or before.st_size <= 1
            or before.st_size > int(max_bytes)
        ):
            return {}, {"valid": False, "reason": "source_not_regular_or_bounded"}
        opened_clock_delta_s = time.time() - float(before.st_mtime)
        if opened_clock_delta_s < -5.0:
            return {}, {
                "valid": False,
                "reason": "source_mtime_in_future",
                "clock_delta_s": opened_clock_delta_s,
            }
        opened_age_s = max(0.0, opened_clock_delta_s)
        if (
            max_age_s is not None
            and opened_age_s > max(0.0, float(max_age_s))
        ):
            return {}, {
                "valid": False,
                "reason": "source_stale",
                "age_s": opened_age_s,
            }
        source = bytearray()
        while len(source) <= int(max_bytes):
            chunk = os.read(
                descriptor,
                min(256 * 1024, int(max_bytes) - len(source) + 1),
            )
            if not chunk:
                break
            source.extend(chunk)
        after = os.fstat(descriptor)
        source_generation = _generation(before)
        if (
            len(source) > int(max_bytes)
            or len(source) != int(after.st_size)
            or source_generation != _generation(after)
        ):
            return {}, {"valid": False, "reason": "source_generation_changed"}
        current = os.stat(normalized, follow_symlinks=False)
        if (
            not stat.S_ISREG(current.st_mode)
            or int(current.st_nlink) != 1
            or _generation(current) != source_generation
        ):
            return {}, {"valid": False, "reason": "source_path_changed"}
        payload = json.loads(
            source.decode("utf-8-sig"),
            parse_constant=lambda value: (_ for _ in ()).throw(
                ValueError(value)
            ),
        )
        cache_key = (normalized, source_generation)
        with _BOUND_JSON_CACHE_LOCK:
            for old_key in list(_BOUND_JSON_CACHE):
                if old_key[0] == normalized and old_key != cache_key:
                    _BOUND_JSON_CACHE.pop(old_key, None)
            _BOUND_JSON_CACHE[cache_key] = payload
            while len(_BOUND_JSON_CACHE) > _BOUND_JSON_CACHE_LIMIT:
                _BOUND_JSON_CACHE.popitem(last=False)
        return (copy.deepcopy(payload) if copy_data else payload), {
            "valid": True,
            "reason": "ok",
            "age_s": opened_age_s,
            "generation": source_generation,
            "from_cache": False,
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return {}, {"valid": False, "reason": "source_unreadable"}
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def read_bound_json_object(
    path: str,
    *,
    max_age_s: Optional[float],
    max_bytes: int,
    copy_data: bool = True,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    payload, metadata = read_bound_json_value(
        path,
        max_age_s=max_age_s,
        max_bytes=max_bytes,
        copy_data=copy_data,
    )
    if not isinstance(payload, dict):
        return {}, {
            **metadata,
            "valid": False,
            "reason": "json_root_not_object",
        }
    return payload, metadata


def _finite_number(value: Any) -> Optional[float]:
    try:
        parsed = float(value)
        return parsed if math.isfinite(parsed) else None
    except (TypeError, ValueError):
        return None


def _first_number(source: Dict[str, Any], *keys: str) -> Optional[float]:
    for key in keys:
        value = _finite_number(source.get(key))
        if value is not None:
            return value
    return None


def _first_value(source: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = source.get(key)
        if value is not None:
            return value
    return None


def _bool_value(value: Any) -> Optional[bool]:
    if isinstance(value, bool):
        return value
    if value is None:
        return None
    if isinstance(value, (int, float)):
        number = _finite_number(value)
        return None if number is None else bool(number)
    normalized = str(value).strip().lower()
    if normalized in {
        "1",
        "true",
        "yes",
        "ja",
        "on",
        "ein",
        "active",
        "aktiv",
        "charging",
    }:
        return True
    if normalized in {
        "",
        "0",
        "false",
        "no",
        "nein",
        "off",
        "aus",
        "inactive",
        "inaktiv",
        "idle",
    }:
        return False
    return None


def _any_bool(source: Dict[str, Any], *keys: str) -> bool:
    """Aggregiert alle belegten Aliase; ein frühes ``false`` ist kein Veto."""

    return any(_bool_value(source.get(key)) is True for key in keys)


def _bool_evidence(
    source: Dict[str, Any],
    *keys: str,
) -> Tuple[bool, bool]:
    values = [
        parsed
        for key in keys
        if key in source
        for parsed in (_bool_value(source.get(key)),)
        if parsed is not None
    ]
    return bool(values), any(values)


def _native_wallbox_status_contract(
    source: Dict[str, Any],
    *,
    prefix: str = "wb",
) -> Optional[Dict[str, Any]]:
    """Bindet positive E3DC-Wallboxbits an ihren frischen Statusvertrag."""

    if not isinstance(source, dict):
        return None
    status_prefix = "wb2_status" if prefix == "wb2" else "wb_status"
    valid_key = status_prefix + "_valid"
    source_key = status_prefix + "_source"
    reason_key = status_prefix + "_reason"
    native_connected_key = (
        "wb2_car_connected_rscp"
        if prefix == "wb2"
        else "car_connected_rscp"
    )
    declared = bool(
        valid_key in source
        or source_key in source
        or reason_key in source
        or native_connected_key in source
    )
    if not declared:
        return None
    valid = bool(
        source.get(valid_key) is True
        and (
            "driver_status_valid" not in source
            or source.get("driver_status_valid") is True
        )
        and not bool(source.get("driver_status_stale", False))
        and not bool(source.get("driver_status_degraded", False))
        and not bool(source.get("driver_status_glitch", False))
        and source.get("driver_status_plausible") is not False
        and source.get("valid") is not False
        and not bool(source.get("stale", False))
    )
    reason = str(source.get(reason_key) or "").strip()
    if not valid:
        for key in (
            "driver_status_glitch_reason",
            "driver_status_reason",
            reason_key,
        ):
            candidate = str(source.get(key) or "").strip()
            if candidate and candidate.lower() not in {"ok", "fresh"}:
                reason = candidate
                break
        else:
            reason = "native_status_not_fresh"
    elif not reason:
        reason = "fresh"
    return {
        "valid": valid,
        "source": str(source.get(source_key) or "native_status_contract"),
        "reason": reason,
    }


def _wallbox_projection(path: str, max_age_s: float) -> Dict[str, Any]:
    source, metadata = read_bound_json_object(
        path,
        max_age_s=max_age_s,
        max_bytes=4 * 1024 * 1024,
        copy_data=False,
    )
    if not metadata.get("valid"):
        return {
            "wallbox_snapshot_valid": False,
            "wallbox_snapshot_reason": metadata.get("reason"),
        }
    details = source.get("wb_details")
    if not isinstance(details, list):
        details = []
    projected: Dict[str, Any] = {
        "wb2": 0.0,
        "wb_locked": False,
        "wb2_locked": False,
        "wb_charging": False,
        "wb2_charging": False,
    }
    total_w = 0.0
    native_invalid_slots = set()
    for detail in details:
        if not isinstance(detail, dict):
            continue
        try:
            wallbox_id = int(float(detail.get("id") or 0))
        except (TypeError, ValueError):
            continue
        if wallbox_id not in (1, 2):
            continue
        prefix = "wb" if wallbox_id == 1 else "wb2"
        power_w = _first_number(
            detail,
            "charge_power_w",
            "power_w",
            "real_power_w",
        )
        normalized_power_w = max(0.0, power_w or 0.0)
        if power_w is not None:
            projected[prefix] = max(
                normalized_power_w,
                _finite_number(projected.get(prefix)) or 0.0,
            )
        native_status = _native_wallbox_status_contract(detail)
        if native_status is not None:
            native_status_invalid = native_status.get("valid") is not True
            if prefix not in native_invalid_slots or native_status_invalid:
                projected[prefix + "_status_valid"] = bool(
                    native_status["valid"]
                )
                projected[prefix + "_status_source"] = native_status["source"]
                projected[prefix + "_status_reason"] = native_status["reason"]
            if native_status_invalid:
                native_invalid_slots.add(prefix)
                projected[prefix + "_locked"] = False
                projected[prefix + "_charging"] = False
        positive_status_allowed = bool(
            prefix not in native_invalid_slots
            and (native_status is None or native_status.get("valid") is True)
        )
        connected = bool(positive_status_allowed and _any_bool(
            detail,
            "plug",
            "car_connected",
            "alg_connected",
            "plug_state",
        ))
        charging = bool(positive_status_allowed and _any_bool(
            detail,
            "charging",
            "charge_state",
            "alg_charging",
            "charge_is_charging",
        ))
        projected[prefix + "_locked"] = bool(
            projected.get(prefix + "_locked") is True
            or connected
            or charging
        )
        projected[prefix + "_charging"] = bool(
            projected.get(prefix + "_charging") is True or charging
        )
        session_kwh = _first_number(detail, "session_kwh")
        if session_kwh is not None:
            projected[prefix + "_session_kwh"] = max(
                max(0.0, session_kwh),
                _finite_number(
                    projected.get(prefix + "_session_kwh")
                ) or 0.0,
            )
    if not details:
        native_status = _native_wallbox_status_contract(source)
        if native_status is not None:
            projected["wb_status_valid"] = bool(native_status["valid"])
            projected["wb_status_source"] = native_status["source"]
            projected["wb_status_reason"] = native_status["reason"]
        positive_status_allowed = bool(
            native_status is None or native_status.get("valid") is True
        )
        projected["wb_locked"] = bool(positive_status_allowed and _any_bool(
            source,
            "connected",
            "plug_state",
            "car_connected",
        ))
        projected["wb_charging"] = bool(positive_status_allowed and _any_bool(
            source,
            "charging_active",
            "charge_state",
            "charging",
        ))
    total_w = sum(
        max(0.0, _finite_number(projected.get(prefix)) or 0.0)
        for prefix in ("wb", "wb2")
    )
    top_total = _first_number(source, "total_power_w", "power_w")
    if top_total is not None:
        total_w = max(0.0, top_total)
    projected["wallbox_total_w"] = total_w
    projected["wallbox_snapshot_valid"] = True
    projected["wallbox_snapshot_reason"] = "ok"
    projected["wallbox_snapshot_age_s"] = metadata.get("age_s")
    return projected


def project_native_live(
    source: Dict[str, Any],
    *,
    source_age_s: Optional[float] = None,
) -> Dict[str, Any]:
    """Projiziert Kernwerte und behält den nativen Vertrag zur Diagnose bei."""

    if not isinstance(source, dict):
        return {}
    pv_w = _first_number(source, "PV_Power", "pv")
    grid_raw_w = _first_number(source, "Grid_Power", "grid")
    grid_filtered_w = _first_number(source, "Grid_Power_Filtered")
    battery_w = _first_number(source, "Battery_Power", "bat")
    home_w = _first_number(source, "Home_Power", "home_raw", "home")
    soc = _first_number(source, "SOC", "soc")
    timestamp_s = _first_number(source, "_ts", "ts")
    if None in (pv_w, grid_raw_w, battery_w, home_w, soc):
        return {}

    sample_valid = source.get("RSCP_Sample_Valid") is True
    grid_valid = source.get("Grid_Power_Valid") is True
    grid_filtered_valid = source.get("Grid_Power_Filtered_Valid") is True
    projected = dict(source)
    projected.update({
        "pv": pv_w,
        "grid": grid_raw_w,
        "grid_raw": grid_raw_w,
        "grid_filtered": (
            grid_filtered_w
            if grid_filtered_valid and grid_filtered_w is not None
            else None
        ),
        "bat": battery_w,
        "home_raw": home_w,
        "home": home_w,
        "soc": soc,
        "wb": _first_number(source, "Wallbox_Power", "wb") or 0.0,
        "notstrom_status": _first_value(
            source,
            "notstrom_status",
            "Notstrom_Status",
            "ems_emergency_power_status",
        ),
        "rscp_sample_valid": sample_valid,
        "grid_power_valid": grid_valid,
        "grid_filtered_valid": grid_filtered_valid,
        "native_control_valid": bool(sample_valid and grid_valid),
        "valid_data": bool(sample_valid and grid_valid),
        "ts": int(timestamp_s) if timestamp_s is not None else 0,
        "time": (
            time.strftime("%H:%M:%S", time.localtime(timestamp_s))
            if timestamp_s is not None and timestamp_s > 0
            else "--:--"
        ),
        "native_source_age_s": source_age_s,
        "native_source": "live_data_py",
    })
    if "Home_Power_Valid" in source:
        projected["home_power_valid"] = source.get("Home_Power_Valid") is True
    if "Home_Power_Source" in source:
        projected["home_power_source"] = source.get("Home_Power_Source")
    if "Home_Power_Independent" in source:
        projected["home_power_independent"] = (
            source.get("Home_Power_Independent") is True
        )
    return projected


def _bound_web_clean_home(
    native_projection: Dict[str, Any],
    web_projection: Dict[str, Any],
    web_metadata: Dict[str, Any],
    *,
    max_age_s: float = 15.0,
) -> Tuple[Optional[float], Dict[str, Any]]:
    """Bindet den bereinigten Web-Hauswert an denselben nativen Live-Frame.

    ``home_raw`` bleibt immer der native E3/DC-Rohwert. Nur ein frischer
    Web-Snapshot mit passendem Rohwert und Zeitstempel darf ``home`` als bereits
    um separat gemessene Verbraucher bereinigte Anzeigeprojektion liefern.
    """

    if not web_metadata.get("valid"):
        return None, {"valid": False, "reason": "web_projection_invalid"}
    age_s = _finite_number(web_metadata.get("age_s"))
    if age_s is None or age_s > max(0.0, float(max_age_s)):
        return None, {"valid": False, "reason": "web_projection_stale"}
    if web_projection.get("home_power_valid") is not True:
        return None, {"valid": False, "reason": "web_home_invalid"}
    web_home_source = str(web_projection.get("home_source") or "").strip().lower()
    if (
        web_projection.get("home_held_zero_glitch") is True
        or web_home_source.startswith("held_")
        or "fallback" in web_home_source
    ):
        return None, {"valid": False, "reason": "web_home_held_projection"}

    native_raw = _finite_number(native_projection.get("home_raw"))
    web_raw = _finite_number(web_projection.get("home_raw"))
    clean_home = _finite_number(web_projection.get("home"))
    native_ts = _finite_number(native_projection.get("ts"))
    web_ts = _finite_number(web_projection.get("ts"))
    if None in (native_raw, web_raw, clean_home, native_ts, web_ts):
        return None, {"valid": False, "reason": "web_home_binding_incomplete"}
    now_s = time.time()
    if (
        native_ts <= 0.0
        or web_ts <= 0.0
        or native_ts > now_s + 5.0
        or web_ts > now_s + 5.0
        or now_s - native_ts > max(0.0, float(max_age_s))
        or now_s - web_ts > max(0.0, float(max_age_s))
        or abs(native_ts - web_ts) > 5.0
    ):
        return None, {"valid": False, "reason": "web_home_frame_mismatch"}

    tolerance_w = max(350.0, min(1500.0, abs(native_raw) * 0.15))
    if abs(native_raw - web_raw) > tolerance_w:
        return None, {"valid": False, "reason": "web_home_raw_mismatch"}
    if clean_home < 0.0 or clean_home > native_raw + tolerance_w:
        return None, {"valid": False, "reason": "web_home_value_implausible"}

    return clean_home, {
        "valid": True,
        "reason": "web_clean_home_bound",
        "age_s": age_s,
        "frame_delta_s": abs(native_ts - web_ts),
        "raw_delta_w": abs(native_raw - web_raw),
    }


def read_runtime_live_snapshot(
    *,
    live_path: str = LIVE_DATA_PATH,
    wallbox_path: str = WALLBOX_DATA_PATH,
    web_snapshot_path: str = WEB_SNAPSHOT_PATH,
    live_max_age_s: float = 15.0,
    wallbox_max_age_s: float = 30.0,
    web_snapshot_max_age_s: float = 180.0,
    require_control_valid: bool = False,
    include_web_projection: bool = False,
) -> Dict[str, Any]:
    """Liefert einen frischen RAM-Disk-Snapshot ohne HTTP-/PHP-Rückkopplung."""

    native, native_meta = read_bound_json_object(
        live_path,
        max_age_s=live_max_age_s,
        max_bytes=4 * 1024 * 1024,
        copy_data=False,
    )
    if not native_meta.get("valid"):
        return {}
    projected = project_native_live(
        native,
        source_age_s=native_meta.get("age_s"),
    )
    if not projected:
        return {}
    if require_control_valid and projected.get("native_control_valid") is not True:
        return {}

    if include_web_projection:
        web, web_meta = read_bound_json_object(
            web_snapshot_path,
            max_age_s=web_snapshot_max_age_s,
            max_bytes=16 * 1024 * 1024,
            copy_data=False,
        )
        merged = dict(web) if web_meta.get("valid") else {}
        merged.update(projected)
        clean_home, clean_home_binding = _bound_web_clean_home(
            projected,
            web,
            web_meta,
            max_age_s=min(15.0, max(0.0, float(web_snapshot_max_age_s))),
        )
        if clean_home is not None:
            merged["home"] = clean_home
        merged["home_projection_binding"] = clean_home_binding
        projected = merged

    wallbox = _wallbox_projection(wallbox_path, wallbox_max_age_s)
    if wallbox.get("wallbox_snapshot_valid") is True:
        projected.update(wallbox)
        wb_locked = bool(wallbox.get("wb_locked", False))
        wb2_locked = bool(wallbox.get("wb2_locked", False))
        wb_charging = bool(wallbox.get("wb_charging", False))
        wb2_charging = bool(wallbox.get("wb2_charging", False))
        projection_source = "wallbox_companion"
        projection_fallback = False
    else:
        projected.update({
            "wallbox_snapshot_valid": False,
            "wallbox_snapshot_reason": wallbox.get(
                "wallbox_snapshot_reason"
            ),
        })
        web_fallback = web if include_web_projection and web_meta.get("valid") else {}

        external_slots = {
            "wb": _bool_value(web_fallback.get("is_external_wb")) is True,
            "wb2": _bool_value(web_fallback.get("is_external_wb2")) is True,
        }
        native_status_contracts = {
            prefix: (
                None
                if external_slots[prefix]
                else _native_wallbox_status_contract(native, prefix=prefix)
            )
            for prefix in ("wb", "wb2")
        }
        web_status_contracts = {
            prefix: (
                None
                if external_slots[prefix]
                else _native_wallbox_status_contract(
                    web_fallback,
                    prefix=prefix,
                )
            )
            for prefix in ("wb", "wb2")
        }
        for prefix in ("wb", "wb2"):
            status_contract = (
                native_status_contracts[prefix]
                or web_status_contracts[prefix]
            )
            if status_contract is None:
                continue
            projected[prefix + "_status_valid"] = bool(
                status_contract["valid"]
            )
            projected[prefix + "_status_source"] = status_contract["source"]
            projected[prefix + "_status_reason"] = status_contract["reason"]

        def fallback_bool(prefix: str, *keys: str) -> Tuple[bool, str]:
            native_contract = native_status_contracts[prefix]
            if native_contract is not None:
                if native_contract.get("valid") is not True:
                    return False, "native_status_invalid"
                native_has_value, native_value = _bool_evidence(native, *keys)
                if native_has_value:
                    return native_value, "native_snapshot"
                return False, "native_status_without_boolean"

            if external_slots[prefix]:
                web_has_value, web_value = _bool_evidence(
                    web_fallback,
                    *keys,
                )
                if web_has_value:
                    return web_value, "external_web_projection"
                return False, "external_fail_closed_default"

            web_contract = web_status_contracts[prefix]
            if web_contract is not None:
                if web_contract.get("valid") is not True:
                    return False, "web_status_invalid"
                web_has_value, web_value = _bool_evidence(
                    web_fallback,
                    *keys,
                )
                if web_has_value:
                    return web_value, "web_projection"
                return False, "web_status_without_boolean"

            native_has_value, native_value = _bool_evidence(native, *keys)
            if native_has_value:
                return native_value, "native_legacy_snapshot"
            web_has_value, web_value = _bool_evidence(web_fallback, *keys)
            if web_has_value:
                return web_value, "web_legacy_projection"
            return False, "fail_closed_default"

        wb_locked, wb_locked_source = fallback_bool(
            "wb",
            "wb_locked",
            "wb_plugged",
            "wb_plug_state",
            "plug_state",
            "car_connected",
            "connected",
        )
        wb2_locked, wb2_locked_source = fallback_bool(
            "wb2",
            "wb2_locked",
            "wb2_plugged",
            "wb2_plug_state",
            "wb2_car_connected",
        )
        wb_charging, wb_charging_source = fallback_bool(
            "wb",
            "wb_charging",
            "wb_charge_state",
            "charge_state",
            "charging_active",
            "charging",
        )
        wb2_charging, wb2_charging_source = fallback_bool(
            "wb2",
            "wb2_charging",
            "wb2_charge_state",
        )
        native_wb = _first_number(native, "Wallbox_Power", "wb")
        web_wb = _first_number(web_fallback, "wb", "wb_power")
        native_wb2 = _first_number(native, "Wallbox2_Power", "wb2")
        web_wb2 = _first_number(web_fallback, "wb2", "wb2_power")
        projected["wb"] = max(
            0.0,
            native_wb if native_wb is not None else (web_wb or 0.0),
        )
        projected["wb2"] = max(
            0.0,
            native_wb2 if native_wb2 is not None else (web_wb2 or 0.0),
        )
        projected["wallbox_total_w"] = (
            projected["wb"] + projected["wb2"]
        )
        projected["wallbox_boolean_sources"] = {
            "wb_locked": wb_locked_source,
            "wb2_locked": wb2_locked_source,
            "wb_charging": wb_charging_source,
            "wb2_charging": wb2_charging_source,
        }
        projection_source = (
            "native_web_fallback"
            if web_fallback
            else "native_snapshot_fallback"
        )
        projection_fallback = True

    projected["wb_locked"] = wb_locked
    projected["wb2_locked"] = wb2_locked
    projected["wb_charging"] = wb_charging
    projected["wb2_charging"] = wb2_charging
    projected["wallbox_projection_source"] = projection_source
    projected["wallbox_projection_fallback"] = projection_fallback
    projected["wb_plug_state"] = wb_locked
    projected["wb2_plug_state"] = wb2_locked
    projected["wb_charge_state"] = wb_charging
    projected["wb2_charge_state"] = wb2_charging
    projected["wb_power"] = max(
        0.0,
        _finite_number(projected.get("wb")) or 0.0,
    )
    projected["wb2"] = max(
        0.0,
        _finite_number(projected.get("wb2")) or 0.0,
    )
    projected["wb2_power"] = projected["wb2"]
    return projected


def _watchdog_hash() -> Optional[str]:
    snapshot = read_runtime_live_snapshot(
        live_max_age_s=15.0,
        wallbox_max_age_s=30.0,
        require_control_valid=True,
        include_web_projection=False,
    )
    values = (
        _finite_number(snapshot.get("home_raw")),
        _finite_number(snapshot.get("pv")),
        _finite_number(snapshot.get("grid")),
    )
    if not snapshot or any(value is None for value in values):
        return None
    return "_".join(str(int(round(float(value)))) for value in values)


def _main(argv: Any = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args != ["--watchdog-hash"]:
        return 2
    current_hash = _watchdog_hash()
    if current_hash is None:
        return 3
    sys.stdout.write(current_hash + "\n")
    return 0


__all__ = [
    "LIVE_DATA_PATH",
    "WALLBOX_DATA_PATH",
    "WEB_SNAPSHOT_PATH",
    "project_native_live",
    "read_bound_json_object",
    "read_bound_json_value",
    "read_runtime_live_snapshot",
]


if __name__ == "__main__":
    raise SystemExit(_main())
