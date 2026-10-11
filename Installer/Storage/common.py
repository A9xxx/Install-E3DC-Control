#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemeinsame, zustandsfreie Helfer der Storage-Module."""

from __future__ import annotations

from .power_limits import safe_float, safe_int

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






def mode_label(mode: int) -> str:
    return {
        MODE_AUTO: "AUTO",
        MODE_IDLE: "IDLE",
        MODE_DISCH: "DISCH",
        MODE_CHRG: "CHRG",
        MODE_GRID: "GRID",
    }.get(int(mode), "UNKNOWN")
