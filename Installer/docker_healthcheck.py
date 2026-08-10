#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed Healthcheck für den vollständigen Docker-Dienstsatz."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import time


OPTIONAL_EXPECTATION_PATH = Path(
    "/run/e3dc-control/docker_health_optional_services"
)
LOGROTATE_HEALTH_PATH = Path(
    "/run/e3dc-control/docker_logrotate_health.json"
)
MAX_EXPECTATION_BYTES = 16 * 1024
MAX_CMDLINE_BYTES = 64 * 1024
# Der Taktgeber lässt bewusst 300 bis 3600 Sekunden zu. Prozesspräsenz und
# Ergebnisalter werden getrennt geprüft; 300 Sekunden Reserve verhindern einen
# falschen Health-Ausfall direkt vor dem nächsten zulässigen Lauf.
MAX_LOGROTATE_HEALTH_AGE_S = 3900.0

REQUIRED_PROCESSES = {
    "apache2": "Apache",
    "e3dc_live.py": "Live",
    "e3dc_websocket.py": "WebSocket",
    "epex_manager.py": "EPEX",
    "Forecast/pv_forecast_service.py": "Weather/PV-Forecast",
    "storage_simulator.py": "Storage Simulator",
    "storage_manager.py": "Storage Manager",
    "notification_manager.py": "Notifier",
    "e3dc-docker-logrotate": "Logrotation",
}

OPTIONAL_PROCESSES = {
    "e3dc-wallbox-manager": ("wallbox_manager.py", "Wallbox Manager"),
    "energy_manager": ("luxtronik/energy_manager.py", "Energy Manager"),
    "e3dc-lux-live": ("luxtronik/lux_live.py", "Luxtronik Live"),
    "e3dc-idm-live": ("idm/idm_live.py", "IDM Live"),
    "e3dc-stiebel-live": ("stiebel/stiebel_live.py", "Stiebel Live"),
    "e3dc-dimplex-live": ("dimplex/dimplex_live.py", "Dimplex Live"),
    "e3dc-heizstab": ("heizstab_manager.py", "Heizstab Manager"),
    "e3dc-climate-live": ("climate_live.py", "Klimaanlagen-Monitor"),
    "e3dc-climate-control": ("climate_control.py", "Klimaanlagen-Regelstatus"),
    "e3dc-forecast-evidence": (
        "forecast_evidence_sidecar.py",
        "PV-Prognosediagnose",
    ),
    "e3dc-matter-bridge": ("matter_bridge.js", "Matter Bridge"),
    "e3dc-bluelink": ("bluelink_client.py", "Bluelink Client"),
    "e3dc-mqtt-hub": ("e3dc_mqtt_hub.py", "MQTT Hub"),
}

OPTIONAL_DEPENDENCY_PROCESSES = {
    "e3dc-matter-bridge": (
        ("dbus-daemon", "D-Bus"),
        ("avahi-daemon", "Avahi"),
    ),
}
MULTI_PROCESS_MASTERS = {"apache2", "avahi-daemon"}
HEALTHY_PROCESS_STATES = {"R", "S", "D", "I"}


def _read_optional_expectation_nofollow(path: Path) -> tuple[str, ...]:
    """Liest die beim Boot root-gebundene optionale Dienstprojektion."""

    if not path.is_absolute():
        raise RuntimeError("Konfigurationspfad ist nicht absolut")
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.open(path.anchor, directory_flags)
    try:
        for component in path.parts[1:-1]:
            next_fd = os.open(component, directory_flags, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = next_fd
        before = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o444
            or before.st_size > MAX_EXPECTATION_BYTES
        ):
            raise RuntimeError("Optionale Boot-Projektion besitzt keinen sicheren Dateivertrag")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
    finally:
        os.close(directory_fd)

    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise RuntimeError("Optionale Boot-Projektion driftete beim Öffnen")
        chunks = []
        remaining = MAX_EXPECTATION_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        after = os.fstat(descriptor)
        if (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            opened.st_dev,
            opened.st_ino,
            opened.st_size,
            opened.st_mtime_ns,
            opened.st_ctime_ns,
        ):
            raise RuntimeError("Optionale Boot-Projektion driftete beim Lesen")
    finally:
        os.close(descriptor)

    raw = b"".join(chunks)
    if len(raw) > MAX_EXPECTATION_BYTES or b"\x00" in raw:
        raise RuntimeError("Optionale Boot-Projektion ist ungültig")
    try:
        services = tuple(
            line.strip()
            for line in raw.decode("utf-8").splitlines()
            if line.strip()
        )
    except UnicodeDecodeError as exc:
        raise RuntimeError("Optionale Boot-Projektion ist nicht UTF-8") from exc
    if len(services) != len(set(services)):
        raise RuntimeError("Optionale Boot-Projektion enthält Doppelnennungen")
    unknown = tuple(service for service in services if service not in OPTIONAL_PROCESSES)
    if unknown:
        raise RuntimeError(
            "Healthcheck-Abbildung fehlt für: " + ", ".join(unknown)
        )
    expected_order = tuple(
        service for service in OPTIONAL_PROCESSES if service in set(services)
    )
    if services != expected_order:
        raise RuntimeError("Optionale Boot-Projektion besitzt keine kanonische Reihenfolge")
    return services


def _read_process_stat(pid: str) -> tuple[str, int, int]:
    with open(
        os.path.join("/proc", pid, "stat"),
        "r",
        encoding="ascii",
        errors="replace",
    ) as handle:
        payload = handle.read(16 * 1024)
    closing = payload.rfind(")")
    if closing < 0:
        raise RuntimeError(f"Prozessstatus für PID {pid} ist ungültig")
    fields = payload[closing + 2 :].split()
    if len(fields) < 20:
        raise RuntimeError(f"Prozessstatus für PID {pid} ist unvollständig")
    return fields[0], int(fields[1]), int(fields[19])


def _require_recent_logrotate_health(path: Path) -> None:
    directory_flags = (
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    directory_fd = os.open(str(path.parent), directory_flags)
    try:
        before = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o444
            or before.st_size > 4096
        ):
            raise RuntimeError("Logrotate-Health besitzt keinen sicheren Dateivertrag")
        descriptor = os.open(
            path.name,
            os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0),
            dir_fd=directory_fd,
        )
        try:
            opened = os.fstat(descriptor)
            raw = os.read(descriptor, 4097)
            after = os.fstat(descriptor)
            named_after = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        finally:
            os.close(descriptor)
    finally:
        os.close(directory_fd)
    opened_contract = (
        opened.st_dev,
        opened.st_ino,
        opened.st_size,
        opened.st_mtime_ns,
        opened.st_ctime_ns,
        opened.st_uid,
        opened.st_gid,
        stat.S_IMODE(opened.st_mode),
        opened.st_nlink,
    )
    if (
        len(raw) > 4096
        or opened_contract
        != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
            after.st_uid,
            after.st_gid,
            stat.S_IMODE(after.st_mode),
            after.st_nlink,
        )
        or opened_contract
        != (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_size,
            named_after.st_mtime_ns,
            named_after.st_ctime_ns,
            named_after.st_uid,
            named_after.st_gid,
            stat.S_IMODE(named_after.st_mode),
            named_after.st_nlink,
        )
    ):
        raise RuntimeError("Logrotate-Health driftete beim Lesen")
    try:
        payload = json.loads(raw.decode("utf-8"))
        timestamp = float(payload.get("last_success_epoch_s"))
    except (AttributeError, TypeError, ValueError, UnicodeDecodeError) as exc:
        raise RuntimeError("Logrotate-Health ist strukturell ungültig") from exc
    age = time.time() - timestamp
    if (
        payload.get("schema") != "e3dc_docker_logrotate_health_v1"
        or not (0.0 <= age <= MAX_LOGROTATE_HEALTH_AGE_S)
    ):
        raise RuntimeError(f"Logrotate-Health ist veraltet oder unplausibel: {age:.1f}s")


def _process_snapshot() -> tuple[dict, ...]:
    """Liest einen begrenzten, atomnahen argv-Snapshot des PID-Namensraums."""

    processes = []
    with os.scandir("/proc") as entries:
        for entry in entries:
            if not entry.name.isdecimal():
                continue
            try:
                state_before, parent_pid, start_time = _read_process_stat(entry.name)
                with open(
                    os.path.join("/proc", entry.name, "cmdline"),
                    "rb",
                    buffering=0,
                ) as command_file:
                    payload = command_file.read(MAX_CMDLINE_BYTES + 1)
                state_after, parent_after, start_after = _read_process_stat(entry.name)
            except (FileNotFoundError, ProcessLookupError):
                continue
            except OSError as exc:
                raise RuntimeError(
                    f"Prozessinventar für PID {entry.name} ist nicht lesbar: {exc}"
                ) from exc
            if len(payload) > MAX_CMDLINE_BYTES:
                raise RuntimeError(f"Prozesskommando für PID {entry.name} ist zu groß")
            if (
                state_before,
                parent_pid,
                start_time,
            ) != (
                state_after,
                parent_after,
                start_after,
            ):
                raise RuntimeError(f"Prozess PID {entry.name} driftete im Snapshot")
            argv = tuple(
                token.decode("utf-8", errors="replace")
                for token in payload.split(b"\0")
                if token
            )
            if argv:
                processes.append(
                    {
                        "pid": int(entry.name),
                        "ppid": parent_pid,
                        "start_time": start_time,
                        "state": state_before,
                        "argv": argv,
                    }
                )
    return tuple(processes)


def _matching_processes(processes: tuple[dict, ...], expected: str) -> tuple[dict, ...]:
    """Bindet Prozesse an vollständige argv-Token statt Teilstrings."""

    if expected == "apache2":
        return tuple(
            process
            for process in processes
            if Path(process["argv"][0]).name == "apache2"
        )
    if expected.endswith(".py"):
        interpreter_prefix = "python"
    elif expected.endswith(".js"):
        interpreter_prefix = "node"
    else:
        interpreter_prefix = ""
    if interpreter_prefix:
        suffix = "/" + expected
        return tuple(
            process
            for process in processes
            if Path(process["argv"][0]).name.startswith(interpreter_prefix)
            and any(
                token == expected or token.endswith(suffix)
                for token in process["argv"][1:]
            )
        )
    if "/" not in expected:
        executable_matches = tuple(
            process
            for process in processes
            if Path(process["argv"][0]).name == expected
            or Path(process["argv"][0]).name.startswith(expected + ":")
        )
        if executable_matches:
            return executable_matches
    suffix = "/" + expected
    return tuple(
        process
        for process in processes
        if any(
            token == expected or token.endswith(suffix)
            for token in process["argv"][1:]
        )
    )


def _configured_processes(services: tuple[str, ...]) -> dict[str, str]:
    expected = {
        OPTIONAL_PROCESSES[service][0]: OPTIONAL_PROCESSES[service][1]
        for service in services
    }
    for service in services:
        for process, label in OPTIONAL_DEPENDENCY_PROCESSES.get(service, ()):
            expected[process] = label
    return expected


def _apache_config_valid() -> bool:
    try:
        result = subprocess.run(
            ["/usr/sbin/apache2ctl", "configtest"],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return result.returncode == 0


def main() -> int:
    try:
        optional_services = _read_optional_expectation_nofollow(
            OPTIONAL_EXPECTATION_PATH
        )
        expected = dict(REQUIRED_PROCESSES)
        expected.update(_configured_processes(optional_services))
        processes = _process_snapshot()
        missing = []
        unstable = []
        stable_snapshot = {}
        for process_name, label in expected.items():
            matches = tuple(
                process
                for process in _matching_processes(processes, process_name)
                if process["state"] in HEALTHY_PROCESS_STATES
            )
            if not matches:
                missing.append(label)
                continue
            if process_name in MULTI_PROCESS_MASTERS:
                master = min(
                    matches,
                    key=lambda item: (item["start_time"], item["pid"]),
                )
                stable_snapshot[process_name] = [
                    master["pid"],
                    master["start_time"],
                ]
                continue
            if len(matches) != 1:
                unstable.append(f"{label}={len(matches)} Prozesse")
                continue
            stable_snapshot[process_name] = [
                matches[0]["pid"],
                matches[0]["start_time"],
            ]
        if not _apache_config_valid():
            missing.append("Apache-Konfiguration")
        _require_recent_logrotate_health(LOGROTATE_HEALTH_PATH)
        if missing or unstable:
            details = []
            if missing:
                details.append("nicht bereit: " + ", ".join(missing))
            if unstable:
                details.append("nicht eindeutig: " + ", ".join(unstable))
            raise RuntimeError("; ".join(details))
    except Exception as exc:
        print(f"E3DC-Docker-Healthcheck fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(stable_snapshot, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
