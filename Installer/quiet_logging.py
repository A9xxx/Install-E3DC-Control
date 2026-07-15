#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small logging helper for quiet manager loops."""

from __future__ import annotations

import logging
import re
import time
from typing import Dict, Iterable, Optional


class QuietInfoFilter(logging.Filter):
    """Dämpft wiederholte INFO- und Fehlermeldungen ohne Erstereignisse zu verlieren."""

    def __init__(
        self,
        name: str = "",
        *,
        min_interval_s: float = 300.0,
        normalize_numbers: bool = True,
        always_keywords: Optional[Iterable[str]] = None,
        warning_min_interval_s: float = 30.0,
        warning_max_interval_s: float = 3600.0,
    ):
        super().__init__(name)
        self.min_interval_s = max(0.0, float(min_interval_s or 0.0))
        self.normalize_numbers = bool(normalize_numbers)
        self.always_keywords = tuple(str(item).lower() for item in (always_keywords or ()))
        self.warning_min_interval_s = max(0.0, float(warning_min_interval_s or 0.0))
        self.warning_max_interval_s = max(self.warning_min_interval_s, float(warning_max_interval_s or 0.0))
        self._last_seen: Dict[str, float] = {}
        self._warning_state: Dict[str, Dict[str, float]] = {}
        self._record_attribute = f"_e3dc_quiet_filter_{id(self)}"

    def _signature(self, message: str) -> str:
        text = re.sub(r"\s+", " ", str(message or "")).strip()
        if self.normalize_numbers:
            text = re.sub(r"[-+]?\d+(?:[.,]\d+)?", "#", text)
        return text[:240]

    def filter(self, record: logging.LogRecord) -> bool:
        cached_decision = getattr(record, self._record_attribute, None)
        if cached_decision is not None:
            return bool(cached_decision)
        decision = self._filter_once(record)
        setattr(record, self._record_attribute, bool(decision))
        return bool(decision)

    def _filter_once(self, record: logging.LogRecord) -> bool:
        if bool(getattr(record, "e3dc_no_throttle", False)):
            return True
        if record.levelno != logging.INFO or self.min_interval_s <= 0:
            if record.levelno >= logging.WARNING:
                return self._filter_warning(record)
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        lower = str(message).lower()
        if any(keyword in lower for keyword in self.always_keywords):
            return True
        signature = self._signature(message)
        now_ts = time.monotonic()
        last_ts = float(self._last_seen.get(signature, 0.0) or 0.0)
        if last_ts > 0.0 and now_ts - last_ts < self.min_interval_s:
            return False
        self._last_seen[signature] = now_ts
        if len(self._last_seen) > 512:
            cutoff = now_ts - max(self.min_interval_s * 2.0, 600.0)
            self._last_seen = {
                key: ts for key, ts in self._last_seen.items() if float(ts or 0.0) >= cutoff
            }
        return True

    def _filter_warning(self, record: logging.LogRecord) -> bool:
        if self.warning_min_interval_s <= 0:
            return True
        try:
            message = record.getMessage()
        except Exception:
            return True
        signature = f"{record.levelno}:{self._signature(message)}"
        now_ts = time.monotonic()
        state = self._warning_state.get(signature)
        if state is None:
            self._warning_state[signature] = {
                "last_emit": now_ts,
                "interval": self.warning_min_interval_s,
                "suppressed": 0.0,
            }
            return True
        interval = float(state.get("interval", self.warning_min_interval_s) or self.warning_min_interval_s)
        if now_ts - float(state.get("last_emit", 0.0) or 0.0) < interval:
            state["suppressed"] = float(state.get("suppressed", 0.0) or 0.0) + 1.0
            return False
        suppressed = int(state.get("suppressed", 0.0) or 0.0)
        if suppressed > 0:
            record.msg = f"{message} [seit letzter Meldung {suppressed} Wiederholungen unterdrückt]"
            record.args = ()
        state["last_emit"] = now_ts
        state["suppressed"] = 0.0
        state["interval"] = min(self.warning_max_interval_s, max(self.warning_min_interval_s, interval * 2.0))
        if len(self._warning_state) > 256:
            cutoff = now_ts - max(self.warning_max_interval_s * 2.0, 7200.0)
            self._warning_state = {
                key: value
                for key, value in self._warning_state.items()
                if float(value.get("last_emit", 0.0) or 0.0) >= cutoff
            }
        return True


def install_quiet_info_filter(
    logger: logging.Logger,
    *,
    min_interval_s: float = 300.0,
    normalize_numbers: bool = True,
    always_keywords: Optional[Iterable[str]] = None,
    warning_min_interval_s: float = 30.0,
    warning_max_interval_s: float = 3600.0,
) -> QuietInfoFilter:
    """Attach one idempotent filter to the logger and all current handlers."""

    for existing in logger.filters:
        if isinstance(existing, QuietInfoFilter):
            quiet_filter = existing
            break
    else:
        quiet_filter = QuietInfoFilter(
            min_interval_s=min_interval_s,
            normalize_numbers=normalize_numbers,
            always_keywords=always_keywords,
            warning_min_interval_s=warning_min_interval_s,
            warning_max_interval_s=warning_max_interval_s,
        )
        logger.addFilter(quiet_filter)
    for handler in logger.handlers:
        if quiet_filter not in handler.filters:
            handler.addFilter(quiet_filter)
    return quiet_filter
