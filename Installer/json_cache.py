#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mtime-basierter JSON-Cache ohne Veränderung der Frische-Semantik."""

from __future__ import annotations

import copy
import json
import os
import tempfile
import time
from typing import Any, Dict, Optional, Tuple


_READ_CACHE: Dict[str, Dict[str, Any]] = {}
_WRITE_CACHE: Dict[str, Dict[str, Any]] = {}


def file_signature(path: str) -> Optional[Tuple[int, int, int]]:
    try:
        stat = os.stat(path)
        return (int(stat.st_mtime_ns), int(stat.st_size), int(getattr(stat, "st_ino", 0)))
    except OSError:
        return None


def read_json_cached(
    path: str,
    *,
    max_age_s: Optional[float] = None,
    allow_last_good: bool = False,
    with_meta: bool = False,
    copy_data: bool = True,
) -> Any:
    now = time.time()
    signature = file_signature(path)
    cached = _READ_CACHE.get(path)
    from_cache = bool(cached is not None and signature is not None and cached.get("signature") == signature)
    error: Optional[str] = None

    if from_cache:
        data = cached.get("data") if isinstance(cached.get("data"), dict) else {}
        source_mtime = float(cached.get("source_mtime", 0.0) or 0.0)
        valid = True
        last_good = False
    elif signature is not None:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                loaded = json.load(handle)
            if not isinstance(loaded, dict):
                raise ValueError("JSON-Wurzel ist kein Objekt")
            source_mtime = os.path.getmtime(path)
            data = loaded
            valid = True
            last_good = False
            _READ_CACHE[path] = {
                "signature": signature,
                "source_mtime": source_mtime,
                "data": data,
            }
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            valid = False
            last_good = bool(allow_last_good and cached and isinstance(cached.get("data"), dict))
            data = cached.get("data", {}) if last_good else {}
            source_mtime = float(cached.get("source_mtime", 0.0) or 0.0) if last_good else 0.0
    else:
        error = "Datei fehlt"
        valid = False
        last_good = bool(allow_last_good and cached and isinstance(cached.get("data"), dict))
        data = cached.get("data", {}) if last_good else {}
        source_mtime = float(cached.get("source_mtime", 0.0) or 0.0) if last_good else 0.0

    age_s = max(0.0, now - source_mtime) if source_mtime > 0 else None
    stale = bool(max_age_s is not None and (age_s is None or age_s > float(max_age_s)))
    usable = bool(valid and not stale)
    result_data = copy.deepcopy(data) if copy_data else data
    meta = {
        "valid": valid,
        "usable": usable,
        "stale": stale,
        "last_good": last_good,
        "from_cache": from_cache,
        "source_ts": source_mtime or None,
        "source_age_s": round(age_s, 3) if age_s is not None else None,
        "error": error,
    }
    if with_meta:
        return result_data, meta
    return result_data if usable else {}


def atomic_write_json(path: str, data: Dict[str, Any], *, indent: Optional[int] = None, mode: int = 0o664) -> None:
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=os.path.basename(path) + ".", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, ensure_ascii=False, indent=indent, separators=None if indent else (",", ":"))
            if indent:
                handle.write("\n")
        try:
            os.chmod(tmp_path, mode)
        except OSError:
            pass
        os.replace(tmp_path, path)
        try:
            os.chmod(path, mode)
        except OSError:
            pass
        _READ_CACHE[path] = {
            "signature": file_signature(path),
            "source_mtime": os.path.getmtime(path),
            "data": copy.deepcopy(data),
        }
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def atomic_write_on_change(
    path: str,
    data: Dict[str, Any],
    *,
    force_interval_s: float = 60.0,
    noise_keys: Optional[set[str]] = None,
    indent: Optional[int] = None,
    mode: int = 0o664,
) -> bool:
    now = time.time()
    ignored = noise_keys or {"ts", "live_age_s", "source_age_s"}
    compare = {key: value for key, value in data.items() if key not in ignored}
    cached = _WRITE_CACHE.get(path)
    if cached and cached.get("compare") == compare and now - float(cached.get("write_ts", 0.0) or 0.0) < force_interval_s:
        return False
    atomic_write_json(path, data, indent=indent, mode=mode)
    _WRITE_CACHE[path] = {"compare": copy.deepcopy(compare), "write_ts": now}
    return True
