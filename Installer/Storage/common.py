#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemeinsame, zustandsfreie Helfer der Storage-Module."""

from __future__ import annotations

import math
from typing import Any

try:
    from Installer.storage_parallel_regulator import (
        MODE_AUTO,
        MODE_CHRG,
        MODE_DISCH,
        MODE_GRID,
        MODE_IDLE,
    )
except ModuleNotFoundError:
    from storage_parallel_regulator import (  # type: ignore
        MODE_AUTO,
        MODE_CHRG,
        MODE_DISCH,
        MODE_GRID,
        MODE_IDLE,
    )


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return float(default)
        if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
            return float(default)
        text = str(value).strip().replace(",", ".")
        if text == "" or text.lower() in ("none", "null", "nan", "inf", "infinity"):
            return float(default)
        result = float(text)
        return result if math.isfinite(result) else float(default)
    except Exception:
        return float(default)


def safe_int(value: Any, default: int = 0) -> int:
    return int(round(safe_float(value, default)))


def mode_label(mode: int) -> str:
    return {
        MODE_AUTO: "AUTO",
        MODE_IDLE: "IDLE",
        MODE_DISCH: "DISCH",
        MODE_CHRG: "CHRG",
        MODE_GRID: "GRID",
    }.get(int(mode), "UNKNOWN")
