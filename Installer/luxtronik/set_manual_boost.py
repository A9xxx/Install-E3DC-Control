#!/usr/bin/env python3
"""Übergibt eine manuelle Wärmepumpenanforderung an den alleinigen Aktor-Owner.

Dieser Befehl führt bewusst weder Gerätesuche, Modbus-/HTTP-Verbindung noch
einen Wärmepumpen-Schreibzug aus. ``energy_manager.py`` übernimmt die atomare
Anforderung und bleibt der einzige Prozess, der den Treiber-Lease besitzen und
auf Hardware zugreifen darf.
"""

from __future__ import annotations

import grp
import json
import os
import stat
import sys
import time


FLAG_FILE = "/var/www/html/ramdisk/manual_boost.flag"
COMMAND_SCHEMA = "manual_heatpump_command_v1"


def _lock_command_directory(parent):
    """Serialisiert Veröffentlichung und Übernahme des Befehls am Verzeichnis-Inode."""
    import fcntl

    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(parent, flags)
    metadata = os.fstat(fd)
    if not stat.S_ISDIR(metadata.st_mode):
        os.close(fd)
        raise OSError("manual heat-pump command parent is not a directory")
    fcntl.flock(fd, fcntl.LOCK_EX)
    return fd


def _unlock_command_directory(fd):
    import fcntl

    try:
        fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


def _write_all(fd, payload):
    view = memoryview(payload)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("short write while storing manual heat-pump command")
        view = view[written:]


def write_manual_boost_command(action, path=FLAG_FILE, now_ts=None):
    """Speichert eine ``on``-/``off``-Anforderung atomar mit gruppenprivatem Modus."""
    requested_action = str(action or "").strip().lower()
    if requested_action not in ("on", "off"):
        raise ValueError("action must be 'on' or 'off'")

    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    os.makedirs(parent, mode=0o2770, exist_ok=True)
    if not stat.S_ISDIR(os.stat(parent, follow_symlinks=False).st_mode):
        raise OSError("manual heat-pump command parent is not a directory")

    payload = json.dumps(
        {
            "schema": COMMAND_SCHEMA,
            "action": requested_action,
            "requested_ts": int(time.time() if now_ts is None else float(now_ts)),
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    tmp = f"{target}.tmp.{os.getpid()}.{time.time_ns()}"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    fd = None
    lock_fd = None
    try:
        fd = os.open(tmp, flags, 0o640)
        os.fchmod(fd, 0o640)
        try:
            os.fchown(fd, -1, grp.getgrnam("www-data").gr_gid)
        except (KeyError, PermissionError, OSError):
            # Der aufrufende Dienstnutzer hat normalerweise bereits www-data als
            # Primärgruppe. Ist chgrp nicht verfügbar, bleibt die tatsächliche
            # Gruppe maßgeblich; Modus 0640 verhindert weiterhin öffentlichen Zugriff.
            pass
        _write_all(fd, payload)
        os.fsync(fd)
        os.close(fd)
        fd = None
        lock_fd = _lock_command_directory(parent)
        os.replace(tmp, target)
        os.fsync(lock_fd)
        _unlock_command_directory(lock_fd)
        lock_fd = None
        return target
    except Exception:
        if lock_fd is not None:
            try:
                _unlock_command_directory(lock_fd)
            except OSError:
                pass
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def main(argv=None):
    args = list(sys.argv[1:] if argv is None else argv)
    if len(args) != 1 or args[0].strip().lower() not in ("on", "off"):
        print("Verwendung: set_manual_boost.py on|off", file=sys.stderr)
        return 2
    action = args[0].strip().lower()
    try:
        write_manual_boost_command(action, path=FLAG_FILE)
    except Exception as exc:
        print(f"Fehler: Manueller Wärmepumpenauftrag konnte nicht gespeichert werden: {exc}", file=sys.stderr)
        return 1
    if action == "on":
        print("Manueller Wärmepumpen-Boost wurde angefordert.")
    else:
        print("Deaktivierung des manuellen Wärmepumpen-Boosts wurde angefordert.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
