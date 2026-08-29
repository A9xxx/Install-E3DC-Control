#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Kompatibilitätsmodul für alte ``Wallbox.scheduler``-Imports.

Die Wallbox-Ladeplanung gehoert fachlich zu ``Installer/wallbox_planer.py``.
Dieses Modul bleibt absichtlich eine duenne Fassade, damit alte Tests,
Diagnose-Skripte und Fremdaufrufe nicht brechen und keine zweite Planung
entsteht. Neue Aufrufer können dieselbe Delegation über ``ScheduleService``
verwenden.
"""

try:
    from .. import wallbox_planer as _planer
except Exception:  # Aufruf ohne Paketkontext direkt aus dem Installer-Verzeichnis
    import wallbox_planer as _planer  # type: ignore


# Alte Tests und Hotfix-Skripte patchen teilweise diese Modulvariablen. Die
# Wrapper uebertragen sie vor jedem Aufruf in den echten Planer.
os = _planer.os
json = _planer.json
math = _planer.math
time = _planer.time
datetime = _planer.datetime
logging = _planer.logging
logger = _planer.logger

CONFIG_FILE = _planer.CONFIG_FILE
V4_CONFIG_FILE = _planer.V4_CONFIG_FILE
RAMDISK_DIR = _planer.RAMDISK_DIR
INSTALL_DIR = _planer.INSTALL_DIR
PRE_CONDITION_PERCENT = _planer.PRE_CONDITION_PERCENT
PRE_CONDITION_WINDOW_H = _planer.PRE_CONDITION_WINDOW_H
JIT_BONUS_CT_PER_H = _planer.JIT_BONUS_CT_PER_H


def _sync_mutable_globals(planer=None):
    target = planer or _planer
    for name in (
        "CONFIG_FILE",
        "V4_CONFIG_FILE",
        "RAMDISK_DIR",
        "INSTALL_DIR",
        "PRE_CONDITION_PERCENT",
        "PRE_CONDITION_WINDOW_H",
        "JIT_BONUS_CT_PER_H",
    ):
        setattr(target, name, globals()[name])


class ScheduleService:
    """Verhaltensgleiche Fassade um den kanonischen Wallbox-Planer.

    Der Service besitzt keine eigene Planungs-, Cache- oder Dateilogik. Seine
    Aufrufe gehen direkt an ``wallbox_planer.py`` und entsprechen damit dem
    bisherigen Manager-Pfad. Den historischen Global-Sync behalten nur die
    alten modulweiten Kompatibilitaetsfunktionen. ``now_ts`` ist fuer die
    spaetere zeitinjizierbare Service-Grenze reserviert; solange der kanonische
    Planer seine Uhr selbst besitzt, bleibt dessen Zeitsemantik unveraendert.
    """

    def __init__(self, planer=None):
        self._planer = planer or _planer
        self._runtime_config = None

    def generate_native_charging_schedule(self, config, wb_id=None):
        """Delegiere die Planerzeugung und liefere unveraendert dessen Slots."""
        return self._planer.generate_native_charging_schedule(config, wb_id=wb_id)

    def get_planned_charging_status(self, wb_id=None, config=None):
        """Delegiere die Pruefung, ob fuer eine Wallbox ein Slot aktiv ist."""
        return self._planer.get_planned_charging_status(
            wb_id=wb_id,
            config=config,
        )

    def refresh(self, config, now_ts=None, wb_id=None):
        """Aktualisiere den kanonischen Plan und gib seine Slotliste zurueck."""
        del now_ts  # Reserviert; die kanonische Planner-Uhr bleibt autoritativ.
        self._runtime_config = dict(config) if isinstance(config, dict) else None
        return self.generate_native_charging_schedule(config, wb_id=wb_id)

    def evaluate(self, wb_id=None, now_ts=None):
        """Liefere das bestehende boolesche Aktivsignal des Planers."""
        del now_ts  # Reserviert; die kanonische Planner-Uhr bleibt autoritativ.
        return self.get_planned_charging_status(
            wb_id=wb_id,
            config=self._runtime_config,
        )

    def active_charger_ids(self, charger_ids, now_ts=None):
        """Liefere die IDs mit aktuell aktivem kanonischem Ladeplan."""
        active_ids = set()
        for charger_id in charger_ids:
            try:
                wb_id = int(charger_id)
            except (TypeError, ValueError):
                continue
            if self.evaluate(wb_id=wb_id, now_ts=now_ts):
                active_ids.add(wb_id)
        return active_ids


def generate_native_charging_schedule(config, wb_id=None):
    _sync_mutable_globals()
    return _planer.generate_native_charging_schedule(config, wb_id=wb_id)


def get_planned_charging_status(wb_id=None):
    _sync_mutable_globals()
    return _planer.get_planned_charging_status(wb_id=wb_id)


def __getattr__(name):
    return getattr(_planer, name)


__all__ = [
    "ScheduleService",
    "generate_native_charging_schedule",
    "get_planned_charging_status",
]
