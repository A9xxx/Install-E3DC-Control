#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Persistente RSCP-Lesesitzung mit sicheren Poll-Gruppen und Reconnect-Backoff."""

from __future__ import annotations

import atexit
import copy
import time
from typing import Any, Callable, Dict, Iterable, Optional, Tuple


Section = Tuple[str, Callable[[Any], Dict[str, Any]], float, bool]


class RscpAcquisitionSession:
    def __init__(
        self,
        connection_factory: Callable[[], Any],
        authenticate: Callable[[Any], None],
        *,
        now_fn: Callable[[], float] = time.monotonic,
        min_backoff_s: float = 2.0,
        max_backoff_s: float = 60.0,
    ):
        self.connection_factory = connection_factory
        self.authenticate = authenticate
        self.now_fn = now_fn
        self.min_backoff_s = max(1.0, float(min_backoff_s))
        self.max_backoff_s = max(self.min_backoff_s, float(max_backoff_s))
        self.connection: Any = None
        self.connected_since = 0.0
        self.next_retry_ts = 0.0
        self.backoff_s = self.min_backoff_s
        self.reconnect_count = 0
        self.section_cache: Dict[str, Dict[str, Any]] = {}
        atexit.register(self.close)

    def close(self) -> None:
        connection, self.connection = self.connection, None
        if connection is not None:
            try:
                connection.close()
            except Exception:
                pass

    def _connect(self, now_s: float) -> Optional[str]:
        if self.connection is not None:
            return None
        if now_s < self.next_retry_ts:
            return f"Reconnect-Backoff noch {self.next_retry_ts - now_s:.1f}s aktiv"
        connection = None
        try:
            connection = self.connection_factory()
            connection.connect()
            self.authenticate(connection)
            self.connection = connection
            self.connected_since = now_s
            self.next_retry_ts = 0.0
            self.backoff_s = self.min_backoff_s
            self.reconnect_count += 1
            return None
        except Exception as exc:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            self.next_retry_ts = now_s + self.backoff_s
            self.backoff_s = min(self.max_backoff_s, self.backoff_s * 2.0)
            return f"Verbindung: {exc}"

    def _cache_value(self, name: str, now_s: float, ttl_s: float, value: Dict[str, Any]) -> None:
        if ttl_s <= 0:
            return
        self.section_cache[name] = {
            "data": copy.deepcopy(value),
            "read_ts": now_s,
            "ttl_s": ttl_s,
        }

    def _cached_value(self, name: str, now_s: float, *, allow_stale_factor: float = 3.0) -> Dict[str, Any]:
        cached = self.section_cache.get(name)
        if not isinstance(cached, dict):
            return {}
        age_s = now_s - float(cached.get("read_ts", 0.0) or 0.0)
        ttl_s = max(1.0, float(cached.get("ttl_s", 0.0) or 0.0))
        if age_s > ttl_s * max(1.0, allow_stale_factor):
            return {}
        data = cached.get("data")
        return copy.deepcopy(data) if isinstance(data, dict) else {}

    def acquire(self, sections: Iterable[Section]) -> Tuple[Dict[str, Any], list[str], Dict[str, Any]]:
        now_s = self.now_fn()
        data: Dict[str, Any] = {}
        errors: list[str] = []
        section_meta: Dict[str, Any] = {}
        connect_error = self._connect(now_s)
        if connect_error:
            errors.append(connect_error)
            return data, errors, self.diagnostics(now_s, section_meta)

        for name, reader, ttl_s, critical in sections:
            cached = self.section_cache.get(name)
            age_s = now_s - float((cached or {}).get("read_ts", 0.0) or 0.0)
            due = ttl_s <= 0.0 or not cached or age_s >= ttl_s
            if not due:
                cached_data = self._cached_value(name, now_s)
                data.update(cached_data)
                section_meta[name] = {"source": "cache", "age_s": round(age_s, 3), "ttl_s": ttl_s}
                continue
            try:
                value = reader(self.connection)
                if not isinstance(value, dict):
                    raise ValueError("RSCP-Bereich lieferte kein Objekt")
                data.update(value)
                self._cache_value(name, now_s, ttl_s, value)
                section_meta[name] = {"source": "rscp", "age_s": 0.0, "ttl_s": ttl_s}
            except Exception as exc:
                errors.append(f"{name}: {exc}")
                cached_data = self._cached_value(name, now_s)
                if cached_data and ttl_s > 0:
                    data.update(cached_data)
                    section_meta[name] = {
                        "source": "last_good",
                        "age_s": round(age_s, 3),
                        "ttl_s": ttl_s,
                        "error": str(exc),
                    }
                else:
                    section_meta[name] = {"source": "invalid", "ttl_s": ttl_s, "error": str(exc)}
                if critical:
                    self.close()
                    self.next_retry_ts = now_s + self.backoff_s
                    self.backoff_s = min(self.max_backoff_s, self.backoff_s * 2.0)
                    break
        return data, errors, self.diagnostics(now_s, section_meta)

    def diagnostics(self, now_s: Optional[float] = None, sections: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        now_s = self.now_fn() if now_s is None else float(now_s)
        return {
            "schema_version": "rscp_acquisition_v1",
            "mode": "persistent",
            "connected": self.connection is not None,
            "connected_age_s": round(max(0.0, now_s - self.connected_since), 3) if self.connection is not None else None,
            "reconnect_count": self.reconnect_count,
            "retry_in_s": round(max(0.0, self.next_retry_ts - now_s), 3),
            "sections": sections or {},
        }


def normalize_snapshot_for_shadow(snapshot: Dict[str, Any]) -> Dict[str, Any]:
    ignored = {"_ts", "_elapsed", "RSCP_Acquisition"}
    return {key: value for key, value in snapshot.items() if key not in ignored}


def compare_acquisition_snapshots(reference: Dict[str, Any], candidate: Dict[str, Any]) -> Dict[str, Any]:
    left = normalize_snapshot_for_shadow(reference or {})
    right = normalize_snapshot_for_shadow(candidate or {})
    keys = sorted(set(left) | set(right))
    differing = [key for key in keys if left.get(key) != right.get(key)]
    return {
        "schema_version": "rscp_acquisition_shadow_v1",
        "equal": not differing,
        "differing_keys": differing,
        "reference_keys": len(left),
        "candidate_keys": len(right),
    }
