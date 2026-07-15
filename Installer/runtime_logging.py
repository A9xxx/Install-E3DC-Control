#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Einheitliche, begrenzte Laufzeitprotokollierung für E3DC-Dienste."""

from __future__ import annotations

import logging
import os
import time
from logging.handlers import RotatingFileHandler
from typing import Any, Dict, Iterable, Optional

try:
    from .quiet_logging import install_quiet_info_filter
except ImportError:  # pragma: no cover - direkter Skriptstart
    from quiet_logging import install_quiet_info_filter


def configure_service_logger(
    name: str,
    *,
    log_path: Optional[str] = None,
    level: int = logging.INFO,
    max_bytes: int = 2 * 1024 * 1024,
    backup_count: int = 3,
    stream: bool = True,
    quiet_interval_s: float = 300.0,
    warning_min_interval_s: float = 30.0,
    warning_max_interval_s: float = 3600.0,
    always_keywords: Optional[Iterable[str]] = None,
) -> logging.Logger:
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        try:
            handler.close()
        except Exception:
            pass
    logger.handlers.clear()
    logger.filters.clear()
    formatter = logging.Formatter(
        "%(asctime)s - %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_error: Optional[Exception] = None
    if log_path and os.environ.get("E3DC_LOG_TO_FILE", "1") != "0":
        try:
            os.makedirs(os.path.dirname(log_path) or ".", exist_ok=True)
            file_handler = RotatingFileHandler(
                log_path,
                maxBytes=max(256 * 1024, int(max_bytes)),
                backupCount=max(1, int(backup_count)),
                encoding="utf-8",
            )
            file_handler.setFormatter(formatter)
            logger.addHandler(file_handler)
        except OSError as exc:
            # Ein defektes Logverzeichnis darf keinen Regeldienst blockieren.
            file_error = exc
    if stream:
        stream_handler = logging.StreamHandler()
        stream_handler.setFormatter(formatter)
        logger.addHandler(stream_handler)
    install_quiet_info_filter(
        logger,
        min_interval_s=quiet_interval_s,
        warning_min_interval_s=warning_min_interval_s,
        warning_max_interval_s=warning_max_interval_s,
        always_keywords=always_keywords,
    )
    if file_error is not None:
        logger.warning(
            "Dateilog %s nicht verfügbar; Ausgabe läuft nur über stderr: %s",
            log_path,
            file_error,
            extra={"e3dc_no_throttle": True},
        )
    return logger


class EventLogLimiter:
    """Dämpft bekannte Fehlerpfade und meldet ihre Erholung sofort."""

    def __init__(self, *, min_interval_s: float = 30.0, max_interval_s: float = 3600.0):
        self.min_interval_s = max(1.0, float(min_interval_s))
        self.max_interval_s = max(self.min_interval_s, float(max_interval_s))
        self._state: Dict[str, Dict[str, Any]] = {}

    def failure(
        self,
        logger: logging.Logger,
        key: str,
        message: str,
        *args: Any,
        level: int = logging.WARNING,
        now_s: Optional[float] = None,
    ) -> bool:
        now_s = time.monotonic() if now_s is None else float(now_s)
        state = self._state.get(key)
        if state is None:
            state = {
                "first_ts": now_s,
                "last_emit_ts": 0.0,
                "interval_s": self.min_interval_s,
                "suppressed": 0,
            }
            self._state[key] = state
        elapsed = now_s - float(state.get("last_emit_ts", 0.0) or 0.0)
        if state["last_emit_ts"] and elapsed < float(state["interval_s"]):
            state["suppressed"] = int(state.get("suppressed", 0) or 0) + 1
            return False
        suppressed = int(state.get("suppressed", 0) or 0)
        suffix = f" [seit letzter Meldung {suppressed} Wiederholungen unterdrückt]" if suppressed else ""
        logger.log(level, message + suffix, *args, extra={"e3dc_no_throttle": True})
        state["last_emit_ts"] = now_s
        state["suppressed"] = 0
        state["interval_s"] = min(self.max_interval_s, max(self.min_interval_s, float(state["interval_s"]) * 2.0))
        return True

    def recovery(
        self,
        logger: logging.Logger,
        key: str,
        message: str,
        *args: Any,
        now_s: Optional[float] = None,
    ) -> bool:
        state = self._state.pop(key, None)
        if state is None:
            return False
        now_s = time.monotonic() if now_s is None else float(now_s)
        duration_s = max(0.0, now_s - float(state.get("first_ts", now_s) or now_s))
        suppressed = int(state.get("suppressed", 0) or 0)
        logger.info(
            message + " [Störung nach %.1fs beendet, %d Wiederholungen unterdrückt]",
            *args,
            duration_s,
            suppressed,
            extra={"e3dc_no_throttle": True},
        )
        return True
