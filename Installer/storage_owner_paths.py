#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gemeinsamer Ownervertrag der Speicherpfade.

Der kanonische Manager, Phase 5 und ihre Fallbacks dürfen bekannte, stärkere
und kompatible Primärpfade nicht getrennt fortschreiben. Ein neuer Pfad bleibt
dadurch bis zur ausdrücklichen Einordnung fail-closed.
"""

from __future__ import annotations


STORAGE_DECISION_PRIMARY_PATH_PRIORITY = (
    "protection",
    "manual",
    "direct_marketing",
    "market_price",
    "predump",
    "wallbox_support",
    "curve",
    "storage_active",
    "e3dc_auto",
)

STORAGE_DECISION_PRIMARY_PATHS = frozenset(
    STORAGE_DECISION_PRIMARY_PATH_PRIORITY
)

PHASE5_COMPATIBLE_PRIMARY_PATHS = frozenset({
    "direct_marketing",
    "curve",
    "e3dc_auto",
})

PHASE5_STRONGER_PRIMARY_PATHS = frozenset({
    "protection",
    "manual",
    "wallbox_support",
    "predump",
})

PHASE5_COMPETING_PRIMARY_PATHS = frozenset(
    STORAGE_DECISION_PRIMARY_PATHS
    - PHASE5_COMPATIBLE_PRIMARY_PATHS
    - PHASE5_STRONGER_PRIMARY_PATHS
)

PHASE5_NONCOMPATIBLE_PRIMARY_PATHS = frozenset(
    STORAGE_DECISION_PRIMARY_PATHS - PHASE5_COMPATIBLE_PRIMARY_PATHS
)

# ``peak_shaving`` ist kein regulärer Rückgabewert des Pfadvertrags, kann aber
# als stärkerer historischer State-/Priority-Marker auftreten.
PHASE5_STRONGER_OWNER_MARKERS = frozenset({
    *PHASE5_STRONGER_PRIMARY_PATHS,
    "peak_shaving",
})
