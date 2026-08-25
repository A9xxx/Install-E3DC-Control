#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lokales systemd-Starttor für HA-verwaltete E3DC-Dienste.

Das Tor entscheidet ausschließlich aus der root-eigenen Instanzrolle und dem
lokalen HA-Lease. Es lädt keine Netzdaten und erzeugt selbst keine Lease.
"""

from __future__ import annotations

import sys

try:
    from Installer.ha_writer_admission import evaluate_writer_admission
except ImportError:
    from ha_writer_admission import evaluate_writer_admission  # type: ignore


def start_gate_result() -> tuple[bool, str]:
    """Erlaubt Standalone oder eine aktuell belegte HA-Owner-Lease."""

    # Bei einer noch fehlenden oder beschädigten V4-Konfiguration darf der
    # root-eigene Standalone-Anker als Reparaturautorität dienen. Die zentrale
    # Freigabe prüft dabei trotzdem, dass keine HA-Owner-Lease gehalten wird.
    result = evaluate_writer_admission(allow_off_anchor_config_repair=True)
    if result.get("allowed") is True:
        return True, str(result.get("reason") or "ha_writer_admission_allowed")
    return False, str(result.get("reason") or "ha_writer_admission_blocked")


def main() -> int:
    try:
        allowed, reason = start_gate_result()
    except Exception as exc:
        print(
            "E3DC HA-Starttor: interner Fehler, Dienststart gesperrt "
            f"({type(exc).__name__})",
            file=sys.stderr,
            flush=True,
        )
        return 255
    print(
        f"E3DC HA-Starttor: {'freigegeben' if allowed else 'gesperrt'} ({reason})",
        flush=True,
    )
    return 0 if allowed else 1


if __name__ == "__main__":
    raise SystemExit(main())
