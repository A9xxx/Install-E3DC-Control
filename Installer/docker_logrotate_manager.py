#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed Logrotate-Taktgeber für den Docker-Dienstsatz."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


CONFIG_PATH = Path("/etc/logrotate.d/e3dc-control")
STATE_PATH = Path("/run/e3dc-control/docker-logrotate.state")
HEALTH_PATH = Path("/run/e3dc-control/docker_logrotate_health.json")
LOGROTATE_BIN = "/usr/sbin/logrotate"


def _require_root_regular(path: Path, mode: int) -> None:
    info = path.lstat()
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != 0
        or info.st_gid != 0
        or stat.S_IMODE(info.st_mode) != mode
    ):
        raise RuntimeError(f"Unsicherer Logrotate-Vertrag: {path}")


def _write_health(timestamp: float) -> None:
    directory = HEALTH_PATH.parent
    directory_info = directory.lstat()
    if (
        not stat.S_ISDIR(directory_info.st_mode)
        or directory_info.st_uid != 0
        or directory_info.st_gid != 0
        or stat.S_IMODE(directory_info.st_mode) != 0o755
    ):
        raise RuntimeError("Unsicherer Docker-Laufzeitnamespace")
    payload = json.dumps(
        {
            "schema": "e3dc_docker_logrotate_health_v1",
            "last_success_epoch_s": timestamp,
        },
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(str(directory), directory_flags)
    temporary_name = f".{HEALTH_PATH.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o444,
            dir_fd=directory_fd,
        )
        try:
            os.fchown(descriptor, 0, 0)
            os.fchmod(descriptor, 0o444)
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        os.replace(
            temporary_name,
            HEALTH_PATH.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)
    _require_root_regular(HEALTH_PATH, 0o444)


def _interval_seconds() -> int:
    raw = str(os.environ.get("E3DC_DOCKER_LOGROTATE_INTERVAL_S") or "900").strip()
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError("Ungültiger Docker-Logrotate-Takt") from exc
    if value < 300 or value > 3600:
        raise RuntimeError("Docker-Logrotate-Takt muss zwischen 300 und 3600 Sekunden liegen")
    return value


def main() -> int:
    if os.geteuid() != 0:
        print("Docker-Logrotate benötigt root.", file=sys.stderr)
        return 1
    if not Path(LOGROTATE_BIN).is_file():
        print("/usr/sbin/logrotate fehlt.", file=sys.stderr)
        return 1
    try:
        _require_root_regular(CONFIG_PATH, 0o644)
        interval = _interval_seconds()
    except Exception as exc:
        print(f"Docker-Logrotate-Vertrag ungültig: {exc}", file=sys.stderr)
        return 1

    while True:
        result = subprocess.run(
            [LOGROTATE_BIN, "--state", str(STATE_PATH), str(CONFIG_PATH)],
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        if result.returncode != 0:
            detail = (result.stderr or result.stdout or str(result.returncode)).strip()
            print(f"Docker-Logrotate fehlgeschlagen: {detail}", file=sys.stderr, flush=True)
            return 1
        try:
            _write_health(time.time())
        except Exception as exc:
            print(f"Docker-Logrotate-Health konnte nicht gebunden werden: {exc}", file=sys.stderr, flush=True)
            return 1
        time.sleep(interval)


if __name__ == "__main__":
    raise SystemExit(main())
