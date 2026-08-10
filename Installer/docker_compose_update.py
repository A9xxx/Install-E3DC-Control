#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Sicherer Host-Updater für den E3DC-Control-Compose-Container.

Der Helfer läuft auf dem Docker-Host. Er bindet das explizit gezogene Image
vor dem ersten Kandidatenstart an ID und Release-Version. Sobald ``compose up``
begonnen hat, führt jeder Fehler zu einem verifizierten Stopp des Kandidaten.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import stat
import subprocess
import sys
import time
from typing import Any


SERVICE_NAME = "e3dc-control"
OFFICIAL_IMAGE_REPOSITORY = "ghcr.io/a9xxx/install-e3dc-control"
LEGACY_NO_HEALTHCHECK_VERSION = "5.3.2b"
CONTAINER_HEALTHCHECK_COMMAND = (
    "/opt/venv/bin/python3",
    "-I",
    "-B",
    "/usr/local/bin/e3dc-docker-healthcheck",
)
DEFAULT_WAIT_TIMEOUT_S = 300
COMMAND_TIMEOUT_S = 30
START_TIMEOUT_GRACE_S = 60
COMPOSE_FILENAME = "docker-compose.yml"
MAX_COMPOSE_BYTES = 512 * 1024
MAX_ENV_BYTES = 128 * 1024
ROLE_VOLUME_NAME = "e3dc_instance_role"
ROLE_VOLUME_TARGET = "/etc/e3dc-control"
DATA_VOLUME_TARGET = "/var/www/html/data"
LOG_VOLUME_TARGET = "/var/www/html/logs"
KNOWN_TOP_LEVEL_VOLUMES = {
    "e3dc_data",
    "e3dc_logs",
    "e3dc_ml",
    "e3dc_forecast_evidence",
    ROLE_VOLUME_NAME,
}
VARIABLE_IMAGE_EXPRESSION = (
    f'"{OFFICIAL_IMAGE_REPOSITORY}:${{E3DC_IMAGE_TAG:-latest}}"'
)
WATCHTOWER_LABEL_LEGACY = "com.centurylinklabs.watchtower.enable=true"
WATCHTOWER_LABEL_CURRENT = (
    "com.centurylinklabs.watchtower.enable=${E3DC_WATCHTOWER_ENABLE:-false}"
)


def _active_yaml_lines(value: str) -> tuple[str, ...]:
    """Normalisiert nur Kommentare und Zeilenenden, niemals aktive YAML-Werte."""

    result: list[str] = []
    for raw_line in value.splitlines():
        if "\t" in raw_line:
            raise DockerUpdateError("Compose enthält nicht unterstützte Tab-Einrückungen.")
        quoted: str | None = None
        escaped = False
        visible: list[str] = []
        for char in raw_line.rstrip():
            if escaped:
                visible.append(char)
                escaped = False
                continue
            if char == "\\" and quoted == '"':
                visible.append(char)
                escaped = True
                continue
            if char in {"'", '"'}:
                quoted = None if quoted == char else (char if quoted is None else quoted)
                visible.append(char)
                continue
            if char == "#" and quoted is None:
                break
            visible.append(char)
        line = "".join(visible).rstrip()
        if line.strip():
            result.append(line)
    return tuple(result)


def _known_compose_template(*, bind_mounts: bool, current: bool) -> tuple[str, ...]:
    data_source = "./data" if bind_mounts else "e3dc_data"
    log_source = "./logs" if bind_mounts else "e3dc_logs"
    lines = [
        "services:",
        "  e3dc-control:",
        f"    image: {VARIABLE_IMAGE_EXPRESSION}",
        "    container_name: e3dc-control",
    ]
    if current:
        lines.append("    hostname: e3dc-control")
    lines.extend(
        [
            "    restart: unless-stopped",
            "    network_mode: host",
        ]
    )
    if current:
        lines.extend(
            [
                "    logging:",
                "      driver: json-file",
                "      options:",
                '        max-size: "10m"',
                '        max-file: "3"',
            ]
        )
    lines.extend(
        [
            "    labels:",
            "      - " + (WATCHTOWER_LABEL_CURRENT if current else WATCHTOWER_LABEL_LEGACY),
            "    volumes:",
            f"      - {data_source}:{DATA_VOLUME_TARGET}",
            f"      - {log_source}:{LOG_VOLUME_TARGET}",
            "      - e3dc_ml:/var/lib/e3dc-control/ml",
            "      - e3dc_forecast_evidence:/var/lib/e3dc-control/forecast-evidence",
        ]
    )
    if current:
        lines.append(f"      - {ROLE_VOLUME_NAME}:{ROLE_VOLUME_TARGET}")
    lines.extend(
        [
            "    tmpfs:",
            "      - /var/www/html/ramdisk:size=32M,uid=33,gid=33,mode=2775",
            "    environment:",
            "      - TZ=Europe/Berlin",
            "      - E3DC_CONTAINER_MODE=1",
            "  watchtower:",
            "    image: containrrr/watchtower",
            "    container_name: watchtower",
            "    restart: unless-stopped",
        ]
    )
    if current:
        lines.extend(
            [
                "    logging:",
                "      driver: json-file",
                "      options:",
                f'        max-size: "{"10m" if bind_mounts else "5m"}"',
                f'        max-file: "{"3" if bind_mounts else "2"}"',
            ]
        )
    lines.extend(
        [
            "    profiles:",
            "      - auto-update",
            "    volumes:",
            "      - /var/run/docker.sock:/var/run/docker.sock",
            "    environment:",
            "      - TZ=Europe/Berlin",
            "      - DOCKER_API_VERSION=1.40",
            "      - WATCHTOWER_CLEANUP=true",
            "      - WATCHTOWER_LABEL_ENABLE=true",
            "      - WATCHTOWER_POLL_INTERVAL=86400",
            "volumes:",
        ]
    )
    if not bind_mounts:
        lines.extend(["  e3dc_data:", "  e3dc_logs:"])
    lines.extend(["  e3dc_ml:", "  e3dc_forecast_evidence:"])
    if current:
        lines.append(f"  {ROLE_VOLUME_NAME}:")
    return tuple(lines)


KNOWN_CURRENT_TEMPLATES = {
    _known_compose_template(bind_mounts=False, current=True): "repo_named_volumes",
    _known_compose_template(bind_mounts=True, current=True): "installer_bind_mounts",
}
KNOWN_542_LEGACY_HASHES = {
    "4310fab1ceb4d354d2ca950f0e95a6696f5f68a17f986071b9fa8994a38e4412": "repo_named_volumes",
    "de9587d3f2552cac8200609b084773288610531ca79794a1458806ad97cee519": "repo_named_volumes",
    "be05563fef0cac58f0850829a4023ad40e3dcb479c5f838dff8dcbdfad6c914e": "repo_named_volumes",
    "0659f653757c87cc904b782ded5673b78e5c8f9489cfb1deb0caf697839e34c1": "repo_named_volumes",
    "f253d492f3dbece0ea388996466c2a9e7c1f1911e7595de129895ecf2e06401b": "repo_named_volumes",
    "b4e66cc98b005e34e0c900b91f762539e6bb7d513b33126e196050f520cf06a6": "installer_bind_mounts",
}


class DockerUpdateError(RuntimeError):
    """Der Kandidat erfüllt den gebundenen Updatevertrag nicht."""


class CandidateStopError(DockerUpdateError):
    """Der Kandidatenstillstand konnte nicht bewiesen werden."""


def _normalise_version(value: Any) -> str:
    return str(value or "").strip().lstrip("vV")


def _tag_version(image: str) -> str:
    reference = str(image or "").split("@", 1)[0]
    last_segment = reference.rsplit("/", 1)[-1]
    if ":" not in last_segment:
        return ""
    tag = last_segment.rsplit(":", 1)[-1].strip()
    if not re.fullmatch(r"v?\d+\.\d+\.\d+[A-Za-z0-9._-]*", tag):
        return ""
    return _normalise_version(tag)


def _require_official_image(image: str) -> str:
    pattern = re.compile(
        rf"^{re.escape(OFFICIAL_IMAGE_REPOSITORY)}:"
        r"(?:latest|v?\d+\.\d+\.\d+[A-Za-z0-9._-]*)$"
    )
    if not pattern.fullmatch(str(image or "").strip()):
        raise DockerUpdateError(
            "Der Produkt-Updater akzeptiert ausschließlich ein getaggtes "
            f"offizielles GHCR-Image aus {OFFICIAL_IMAGE_REPOSITORY}."
        )
    return str(image).strip()


class DockerCli:
    def __init__(self, compose_dir: Path, *, use_sudo: bool, environment: dict[str, str]):
        self.compose_dir = compose_dir
        self.compose_file = compose_dir / COMPOSE_FILENAME
        self.prefix = ["sudo"] if use_sudo and os.geteuid() != 0 else []
        self.environment = environment

    def run(
        self,
        arguments: list[str],
        *,
        timeout: int = COMMAND_TIMEOUT_S,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [*self.prefix, "docker", *arguments],
            cwd=str(self.compose_dir),
            env=self.environment,
            capture_output=capture,
            text=True,
            timeout=timeout,
            check=False,
        )

    def compose(
        self,
        arguments: list[str],
        *,
        timeout: int = COMMAND_TIMEOUT_S,
        capture: bool = True,
        compose_file: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        selected_file = compose_file or self.compose_file
        return self.run(
            [
                "compose",
                "--project-directory",
                str(self.compose_dir),
                "-f",
                str(selected_file),
                *arguments,
            ],
            timeout=timeout,
            capture=capture,
        )


def _require_success(result: subprocess.CompletedProcess[str], label: str) -> str:
    if result.returncode != 0:
        detail = (result.stderr or result.stdout or str(result.returncode)).strip()
        raise DockerUpdateError(f"{label} fehlgeschlagen: {detail}")
    return (result.stdout or "").strip()


def _normalised_lf_bytes(value: bytes) -> bytes:
    if b"\r" in value.replace(b"\r\n", b""):
        raise DockerUpdateError("Compose enthält nicht unterstützte einzelne CR-Zeichen.")
    return value.replace(b"\r\n", b"\n")


def _allowed_file_owners() -> set[int]:
    owners = {os.geteuid()}
    if os.geteuid() == 0:
        sudo_uid = str(os.environ.get("SUDO_UID") or "").strip()
        if sudo_uid.isdigit():
            owners.add(int(sudo_uid))
    return owners


def _validate_directory_chain(path: Path) -> None:
    absolute = Path(os.path.abspath(path))
    if absolute == Path("/"):
        raise DockerUpdateError("Das Dateisystemwurzelverzeichnis ist kein Compose-Pfad.")
    allowed_owners = _allowed_file_owners() | {0}
    current = Path("/")
    for component in absolute.parts[1:]:
        current /= component
        info = os.stat(current, follow_symlinks=False)
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise DockerUpdateError(
                f"Die Compose-Pfadkette enthält kein eindeutiges Verzeichnis: {current}"
            )
        mode = stat.S_IMODE(info.st_mode)
        sticky_root = info.st_uid == 0 and bool(mode & stat.S_ISVTX)
        if info.st_uid not in allowed_owners or ((mode & 0o022) and not sticky_root):
            raise DockerUpdateError(
                f"Die Compose-Pfadkette ist bei {current} nicht sicher gebunden."
            )


def _stat_identity(info: os.stat_result) -> tuple[int, ...]:
    return (
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_uid),
        int(info.st_gid),
        int(stat.S_IMODE(info.st_mode)),
        int(info.st_nlink),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _secure_snapshot(
    directory_fd: int,
    name: str,
    *,
    required: bool,
    max_size: int,
) -> dict[str, Any] | None:
    try:
        before = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        if required:
            raise DockerUpdateError(f"{name} fehlt.")
        return None
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise DockerUpdateError(f"{name} ist keine eindeutige reguläre Datei.")
    if before.st_nlink != 1:
        raise DockerUpdateError(f"{name} besitzt nicht genau einen Hardlink.")
    if before.st_uid not in _allowed_file_owners():
        raise DockerUpdateError(f"{name} besitzt einen unerwarteten Eigentümer.")
    mode = stat.S_IMODE(before.st_mode)
    if mode & 0o022 or mode & 0o7000 or not mode & 0o400:
        raise DockerUpdateError(f"{name} besitzt einen unsicheren Dateimodus {mode:04o}.")
    if before.st_size < (1 if required else 0) or before.st_size > max_size:
        raise DockerUpdateError(f"{name} besitzt eine unzulässige Größe.")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    if not nofollow:
        raise DockerUpdateError("Dieses System unterstützt kein O_NOFOLLOW.")
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC | nofollow, dir_fd=directory_fd)
    try:
        opened = os.fstat(fd)
        if _stat_identity(opened) != _stat_identity(before):
            raise DockerUpdateError(f"{name} wechselte beim sicheren Öffnen.")
        chunks: list[bytes] = []
        remaining = max_size + 1
        while remaining > 0:
            chunk = os.read(fd, min(64 * 1024, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        data = b"".join(chunks)
        after = os.fstat(fd)
    finally:
        os.close(fd)
    if len(data) != before.st_size or len(data) > max_size:
        raise DockerUpdateError(f"{name} änderte seine Größe beim Lesen.")
    if _stat_identity(after) != _stat_identity(before):
        raise DockerUpdateError(f"{name} driftete während des Lesens.")
    return {
        "name": name,
        "data": data,
        "sha256": hashlib.sha256(data).hexdigest(),
        "stat": _stat_identity(before),
        "uid": int(before.st_uid),
        "gid": int(before.st_gid),
        "mode": mode,
    }


def _require_same_snapshot(
    directory_fd: int,
    expected: dict[str, Any] | None,
    *,
    name: str,
    max_size: int,
) -> None:
    current = _secure_snapshot(
        directory_fd,
        name,
        required=expected is not None,
        max_size=max_size,
    )
    if expected is None:
        if current is not None:
            raise DockerUpdateError(f"{name} entstand während der Compose-Migration.")
        return
    if current is None or (
        current["stat"],
        current["sha256"],
    ) != (
        expected["stat"],
        expected["sha256"],
    ):
        raise DockerUpdateError(f"{name} driftete während der Compose-Migration.")


def _compose_projection(cli: DockerCli, compose_file: Path) -> dict[str, Any]:
    output = _require_success(
        cli.compose(
            ["--profile", "auto-update", "config", "--format", "json"],
            compose_file=compose_file,
        ),
        "Semantische Compose-Projektion",
    )
    try:
        value = json.loads(output)
    except (TypeError, ValueError) as exc:
        raise DockerUpdateError(f"Compose-Projektion ist kein gültiges JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise DockerUpdateError("Compose-Projektion ist nicht eindeutig.")
    services = value.get("services")
    if not isinstance(services, dict) or set(services) != {SERVICE_NAME, "watchtower"}:
        raise DockerUpdateError(
            "Compose muss exakt die bekannten Dienste e3dc-control und watchtower enthalten."
        )
    return value


def _without_migration_fields(value: dict[str, Any]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(value))
    services = cleaned.get("services") or {}
    e3dc = services.get(SERVICE_NAME) or {}
    watchtower = services.get("watchtower") or {}
    for key in ("hostname", "logging", "labels"):
        e3dc.pop(key, None)
    watchtower.pop("logging", None)
    volumes = e3dc.get("volumes")
    if isinstance(volumes, list):
        e3dc["volumes"] = [
            item
            for item in volumes
            if not (
                isinstance(item, dict)
                and str(item.get("target") or "") == ROLE_VOLUME_TARGET
            )
        ]
    top_volumes = cleaned.get("volumes")
    if isinstance(top_volumes, dict):
        top_volumes.pop(ROLE_VOLUME_NAME, None)
    return cleaned


def _volume_mapping(service: dict[str, Any], target: str) -> list[dict[str, Any]]:
    volumes = service.get("volumes")
    if not isinstance(volumes, list):
        return []
    return [
        item
        for item in volumes
        if isinstance(item, dict) and str(item.get("target") or "") == target
    ]


def _validate_projection_delta(
    before: dict[str, Any],
    after: dict[str, Any],
    *,
    topology: str,
) -> None:
    if _without_migration_fields(before) != _without_migration_fields(after):
        raise DockerUpdateError(
            "Die Compose-Projektion änderte mehr als die freigegebenen Pflichtfelder."
        )
    service = (after.get("services") or {}).get(SERVICE_NAME) or {}
    watchtower = (after.get("services") or {}).get("watchtower") or {}
    if service.get("hostname") != SERVICE_NAME:
        raise DockerUpdateError("Der projizierte Container-Hostname ist nicht gebunden.")
    labels = service.get("labels") or {}
    if str(labels.get("com.centurylinklabs.watchtower.enable", "")).lower() not in {
        "true",
        "false",
    }:
        raise DockerUpdateError("Das Watchtower-Opt-in-Label fehlt in der Projektion.")
    role_mounts = _volume_mapping(service, ROLE_VOLUME_TARGET)
    if len(role_mounts) != 1 or (
        role_mounts[0].get("type"),
        role_mounts[0].get("source"),
    ) != ("volume", ROLE_VOLUME_NAME):
        raise DockerUpdateError("Der persistente Instanzrollen-Mount ist nicht eindeutig.")
    if ROLE_VOLUME_NAME not in (after.get("volumes") or {}):
        raise DockerUpdateError("Das Top-Level-Volume für die Instanzrolle fehlt.")
    expected_watchtower = (
        {"max-size": "10m", "max-file": "3"}
        if topology == "installer_bind_mounts"
        else {"max-size": "5m", "max-file": "2"}
    )
    for label, projected, expected in (
        ("e3dc-control", service, {"max-size": "10m", "max-file": "3"}),
        ("watchtower", watchtower, expected_watchtower),
    ):
        logging = projected.get("logging") or {}
        options = logging.get("options") or {}
        if logging.get("driver") != "json-file" or {
            key: str(options.get(key) or "") for key in expected
        } != expected:
            raise DockerUpdateError(f"Die Logbegrenzung für {label} ist nicht gebunden.")


def _line_ending(data: bytes) -> str:
    _normalised_lf_bytes(data)
    if b"\r\n" in data:
        without_crlf = data.replace(b"\r\n", b"")
        if b"\n" in without_crlf:
            raise DockerUpdateError("Compose verwendet gemischte Zeilenenden.")
        return "\r\n"
    return "\n"


def _insert_after_sequence(
    lines: list[str],
    sequence: tuple[str, ...],
    inserted: tuple[str, ...],
) -> None:
    matches = [
        index
        for index in range(len(lines) - len(sequence) + 1)
        if tuple(
            (_active_yaml_lines(line)[0] if _active_yaml_lines(line) else "")
            for line in lines[index:index + len(sequence)]
        )
        == sequence
    ]
    if len(matches) != 1:
        raise DockerUpdateError("Die bekannte Compose-Einfügeposition ist nicht eindeutig.")
    index = matches[0] + len(sequence)
    ending = _line_ending("".join(lines).encode("utf-8"))
    lines[index:index] = [line + ending for line in inserted]


def _project_current_compose(data: bytes, *, topology: str) -> bytes:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DockerUpdateError("Compose ist nicht gültig UTF-8-kodiert.") from exc
    ending = _line_ending(data)
    lines = text.splitlines(keepends=True)
    if not lines or not lines[-1].endswith(("\n", "\r")):
        raise DockerUpdateError("Compose muss mit einem eindeutigen Zeilenende enden.")

    legacy_label = "      - " + WATCHTOWER_LABEL_LEGACY
    current_label = "      - " + WATCHTOWER_LABEL_CURRENT
    label_matches = [
        index
        for index, line in enumerate(lines)
        if line.rstrip("\r\n").rstrip() == legacy_label
    ]
    if len(label_matches) != 1:
        raise DockerUpdateError("Das historische Watchtower-Label ist nicht eindeutig.")
    label_index = label_matches[0]
    prefix = lines[label_index][: len(lines[label_index]) - len(lines[label_index].lstrip())]
    lines[label_index] = prefix + current_label.lstrip() + ending

    _insert_after_sequence(
        lines,
        ("    container_name: e3dc-control",),
        ("    hostname: e3dc-control",),
    )
    _insert_after_sequence(
        lines,
        ("    network_mode: host",),
        (
            "    logging:",
            "      driver: json-file",
            "      options:",
            '        max-size: "10m"',
            '        max-file: "3"',
        ),
    )
    _insert_after_sequence(
        lines,
        ("      - e3dc_forecast_evidence:/var/lib/e3dc-control/forecast-evidence",),
        (f"      - {ROLE_VOLUME_NAME}:{ROLE_VOLUME_TARGET}",),
    )
    watch_size, watch_files = (
        ("10m", "3") if topology == "installer_bind_mounts" else ("5m", "2")
    )
    _insert_after_sequence(
        lines,
        ("    container_name: watchtower", "    restart: unless-stopped"),
        (
            "    logging:",
            "      driver: json-file",
            "      options:",
            f'        max-size: "{watch_size}"',
            f'        max-file: "{watch_files}"',
        ),
    )
    _insert_after_sequence(
        lines,
        ("  e3dc_forecast_evidence:",),
        (f"  {ROLE_VOLUME_NAME}:",),
    )
    candidate = "".join(lines).encode("utf-8")
    try:
        active = _active_yaml_lines(candidate.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise DockerUpdateError("Compose-Kandidat ist nicht gültig UTF-8-kodiert.") from exc
    expected = _known_compose_template(
        bind_mounts=topology == "installer_bind_mounts",
        current=True,
    )
    if active != expected:
        raise DockerUpdateError("Der Compose-Kandidat entspricht nicht dem Zielvertrag.")
    return candidate


def _write_candidate(
    directory_fd: int,
    compose_dir: Path,
    data: bytes,
    source: dict[str, Any],
) -> tuple[str, Path]:
    for _attempt in range(16):
        name = f".docker-compose.yml.e3dc-migrate-{secrets.token_hex(8)}"
        try:
            fd = os.open(
                name,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
                0o600,
                dir_fd=directory_fd,
            )
        except FileExistsError:
            continue
        break
    else:
        raise DockerUpdateError("Kein eindeutiger Compose-Kandidatenpfad verfügbar.")
    try:
        offset = 0
        while offset < len(data):
            written = os.write(fd, data[offset:])
            if written <= 0:
                raise DockerUpdateError("Compose-Kandidat konnte nicht vollständig geschrieben werden.")
            offset += written
        os.fchmod(fd, int(source["mode"]))
        current = os.fstat(fd)
        if (current.st_uid, current.st_gid) != (source["uid"], source["gid"]):
            os.fchown(fd, int(source["uid"]), int(source["gid"]))
        os.fsync(fd)
    except BaseException:
        try:
            os.unlink(name, dir_fd=directory_fd)
        except OSError:
            pass
        raise
    finally:
        os.close(fd)
    return name, compose_dir / name


def _replace_candidate(
    directory_fd: int,
    candidate_name: str,
    expected_data: bytes,
    source: dict[str, Any],
) -> dict[str, Any]:
    os.replace(
        candidate_name,
        COMPOSE_FILENAME,
        src_dir_fd=directory_fd,
        dst_dir_fd=directory_fd,
    )
    os.fsync(directory_fd)
    readback = _secure_snapshot(
        directory_fd,
        COMPOSE_FILENAME,
        required=True,
        max_size=MAX_COMPOSE_BYTES,
    )
    if readback is None or (
        readback["data"],
        readback["uid"],
        readback["gid"],
        readback["mode"],
    ) != (
        expected_data,
        source["uid"],
        source["gid"],
        source["mode"],
    ):
        raise DockerUpdateError("Der atomare Compose-Readback ist nicht eindeutig.")
    return readback


def _restore_compose_preimage(
    directory_fd: int,
    compose_dir: Path,
    source: dict[str, Any],
    *,
    expected_current_data: bytes,
) -> None:
    current = _secure_snapshot(
        directory_fd,
        COMPOSE_FILENAME,
        required=True,
        max_size=MAX_COMPOSE_BYTES,
    )
    if current is None or current["data"] != expected_current_data:
        raise DockerUpdateError(
            "Compose driftete nach dem Replace; der gebundene Preimage darf nicht überschrieben werden."
        )
    rollback_name, _rollback_path = _write_candidate(
        directory_fd,
        compose_dir,
        source["data"],
        source,
    )
    try:
        _replace_candidate(directory_fd, rollback_name, source["data"], source)
    except BaseException:
        try:
            os.unlink(rollback_name, dir_fd=directory_fd)
        except OSError:
            pass
        raise


def _named_container_ids(cli: DockerCli, name: str) -> tuple[str, ...]:
    output = _require_success(
        cli.run(
            [
                "container",
                "ls",
                "-a",
                "--filter",
                f"name=^/{name}$",
                "--format",
                "{{.ID}}",
            ]
        ),
        f"Globales Containerinventar {name}",
    )
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _running_watchtower_image_ids(cli: DockerCli) -> tuple[str, ...]:
    output = _require_success(
        cli.run(["container", "ls", "--format", "{{.ID}}"]),
        "Globales laufendes Containerinventar",
    )
    found: list[str] = []
    for container_id in (line.strip() for line in output.splitlines() if line.strip()):
        info = _inspect_container(cli, container_id)
        config = info.get("Config") or {}
        references = [str(config.get("Image") or "")]
        provenance_references: list[str] = []
        image_id = str(info.get("Image") or "")
        image_metadata_ok = False
        if image_id:
            image_result = cli.run(["image", "inspect", image_id])
            if image_result.returncode == 0:
                try:
                    image_values = json.loads(image_result.stdout or "")
                    image_info = image_values[0] if len(image_values) == 1 else None
                    if not isinstance(image_info, dict):
                        raise ValueError("kein eindeutiges Image")
                    provenance_references.extend(
                        str(item) for item in (image_info.get("RepoTags") or ())
                    )
                    provenance_references.extend(
                        str(item) for item in (image_info.get("RepoDigests") or ())
                    )
                    references.extend(provenance_references)
                    image_metadata_ok = True
                except (KeyError, TypeError, ValueError):
                    image_metadata_ok = False

        repositories: set[str] = set()
        for raw_reference in references:
            reference = raw_reference.strip().split("@", 1)[0]
            prefix, separator, last = reference.rpartition("/")
            image_name = last
            if ":" in image_name:
                image_name = image_name.rsplit(":", 1)[0]
            repository = f"{prefix}/{image_name}" if separator else image_name
            if repository:
                repositories.add(repository.lower())
        official_repositories = {
            "containrrr/watchtower",
            "docker.io/containrrr/watchtower",
            "index.docker.io/containrrr/watchtower",
            "registry-1.docker.io/containrrr/watchtower",
        }
        if repositories & official_repositories:
            found.append(container_id)
            continue

        mounts = info.get("Mounts") or ()
        docker_socket = any(
            isinstance(mount, dict)
            and str(mount.get("Destination") or "").endswith("/docker.sock")
            for mount in mounts
        )
        docker_host = any(
            str(item).startswith("DOCKER_HOST=")
            for item in (config.get("Env") or ())
        )
        ambiguous_reference = (
            not image_metadata_ok
            or not provenance_references
            or not repositories
            or any("/" not in repo for repo in repositories)
            or any(repo.endswith("/watchtower") or repo == "watchtower" for repo in repositories)
        )
        if (docker_socket or docker_host) and ambiguous_reference:
            raise DockerUpdateError(
                "Ein laufender Container mit unklarer Image-Provenienz besitzt Docker-Daemon-Zugriff; "
                "ein paralleler Update-Supervisor kann nicht ausgeschlossen werden."
            )
    return tuple(found)


def _validate_watchtower_identity(
    cli: DockerCli,
    container_id: str,
    projection: dict[str, Any] | None = None,
) -> bool:
    info = _inspect_container(cli, container_id)
    config = info.get("Config") or {}
    labels = config.get("Labels") or {}
    image = str(config.get("Image") or "")
    config_files = [
        os.path.realpath(part.strip())
        for part in str(labels.get("com.docker.compose.project.config_files") or "").split(",")
        if part.strip()
    ]
    if image not in {"containrrr/watchtower", "containrrr/watchtower:latest"}:
        raise DockerUpdateError("Der gefundene Watchtower besitzt ein fremdes Image.")
    if labels.get("com.docker.compose.service") != "watchtower":
        raise DockerUpdateError("Der gefundene Watchtower gehört nicht zum Compose-Dienst.")
    if projection is not None and labels.get("com.docker.compose.project") != str(
        projection.get("name") or ""
    ):
        raise DockerUpdateError("Der gefundene Watchtower gehört zu einem anderen Compose-Projekt.")
    if os.path.realpath(str(labels.get("com.docker.compose.project.working_dir") or "")) != str(
        cli.compose_dir
    ):
        raise DockerUpdateError("Der gefundene Watchtower gehört zu einem anderen Projektpfad.")
    if config_files != [str(cli.compose_file)]:
        raise DockerUpdateError("Der gefundene Watchtower ist nicht an genau diese Compose-Datei gebunden.")
    return (info.get("State") or {}).get("Running") is True


def _validate_e3dc_container_binding(
    cli: DockerCli,
    projection: dict[str, Any],
    *,
    require_role: bool,
) -> None:
    ids = _named_container_ids(cli, SERVICE_NAME)
    if len(ids) > 1:
        raise DockerUpdateError("Mehrere globale e3dc-control-Container sind nicht eindeutig.")
    if not ids:
        return
    info = _inspect_container(cli, ids[0])
    labels = ((info.get("Config") or {}).get("Labels") or {})
    config_files = [
        os.path.realpath(part.strip())
        for part in str(labels.get("com.docker.compose.project.config_files") or "").split(",")
        if part.strip()
    ]
    if labels.get("com.docker.compose.service") != SERVICE_NAME:
        raise DockerUpdateError("Der vorhandene e3dc-control-Container ist projektfremd.")
    if labels.get("com.docker.compose.project") != str(projection.get("name") or ""):
        raise DockerUpdateError("Der vorhandene e3dc-control-Container nutzt ein anderes Compose-Projekt.")
    if os.path.realpath(str(labels.get("com.docker.compose.project.working_dir") or "")) != str(
        cli.compose_dir
    ):
        raise DockerUpdateError("Der vorhandene e3dc-control-Container nutzt einen anderen Projektpfad.")
    if config_files != [str(cli.compose_file)]:
        raise DockerUpdateError(
            "Der vorhandene e3dc-control-Container wurde nicht nur aus der gebundenen "
            "docker-compose.yml erzeugt; ein früheres Override muss manuell geprüft werden."
        )

    projected_service = ((projection.get("services") or {}).get(SERVICE_NAME) or {})
    projected_volumes = projected_service.get("volumes") or ()
    expected_targets = {
        DATA_VOLUME_TARGET,
        LOG_VOLUME_TARGET,
        "/var/lib/e3dc-control/ml",
        "/var/lib/e3dc-control/forecast-evidence",
    }
    if require_role:
        expected_targets.add(ROLE_VOLUME_TARGET)
    expected_items = [
        item
        for item in projected_volumes
        if isinstance(item, dict) and str(item.get("target") or "") in expected_targets
    ]
    if {str(item.get("target") or "") for item in expected_items} != expected_targets:
        raise DockerUpdateError("Die projizierten persistenten E3DC-Mounts sind unvollständig.")
    actual_mounts = info.get("Mounts") or ()
    actual_persistent = [
        mount
        for mount in actual_mounts
        if isinstance(mount, dict) and str(mount.get("Type") or "") in {"bind", "volume"}
    ]
    if len(actual_persistent) != len(expected_targets) or {
        str(mount.get("Destination") or "") for mount in actual_persistent
    } != expected_targets:
        raise DockerUpdateError(
            "Der reale Container besitzt zusätzliche oder fehlende persistente Mounts."
        )
    for expected in expected_items:
        target = str(expected.get("target") or "")
        matches = [
            mount
            for mount in actual_mounts
            if isinstance(mount, dict) and str(mount.get("Destination") or "") == target
        ]
        if len(matches) != 1:
            raise DockerUpdateError(f"Der reale Container-Mount {target} ist nicht eindeutig.")
        actual = matches[0]
        expected_type = str(expected.get("type") or "")
        if str(actual.get("Type") or "") != expected_type:
            raise DockerUpdateError(f"Der reale Container-Mount {target} wechselte seinen Typ.")
        if expected_type == "bind":
            expected_source = os.path.realpath(str(expected.get("source") or ""))
            if target == DATA_VOLUME_TARGET:
                required_source = os.path.realpath(cli.compose_dir / "data")
            elif target == LOG_VOLUME_TARGET:
                required_source = os.path.realpath(cli.compose_dir / "logs")
            else:
                raise DockerUpdateError("Ein privater E3DC-Mount darf kein Bind-Mount sein.")
            if expected_source != required_source or os.path.realpath(
                str(actual.get("Source") or "")
            ) != required_source:
                raise DockerUpdateError(f"Der Bind-Mount {target} zeigt auf einen fremden Pfad.")
        elif expected_type == "volume":
            logical_name = str(expected.get("source") or "")
            top_volume = (projection.get("volumes") or {}).get(logical_name) or {}
            expected_name = str(top_volume.get("name") or "")
            if not expected_name or str(actual.get("Name") or "") != expected_name:
                raise DockerUpdateError(f"Das reale Named Volume {target} ist nicht projektgebunden.")
        else:
            raise DockerUpdateError(f"Der persistente Mount {target} besitzt einen fremden Typ.")
        if bool(actual.get("RW")) == bool(expected.get("read_only")):
            raise DockerUpdateError(f"Der Schreibmodus des Mounts {target} widerspricht der Projektion.")


def _e3dc_stop_authority(cli: DockerCli, container_id: str) -> dict[str, Any]:
    info = _inspect_container(cli, container_id)
    labels = ((info.get("Config") or {}).get("Labels") or {})
    config_files = [
        os.path.realpath(part.strip())
        for part in str(labels.get("com.docker.compose.project.config_files") or "").split(",")
        if part.strip()
    ]
    if labels.get("com.docker.compose.service") != SERVICE_NAME:
        raise CandidateStopError("Der globale Containername gehört zu einem fremden Dienst.")
    if os.path.realpath(str(labels.get("com.docker.compose.project.working_dir") or "")) != str(
        cli.compose_dir
    ):
        raise CandidateStopError("Der globale Containername gehört zu einem fremden Projektpfad.")
    if config_files != [str(cli.compose_file)]:
        raise CandidateStopError("Der globale Containername gehört zu einem fremden Compose-Filesatz.")
    return info


def _stop_update_watchtower(cli: DockerCli, projection: dict[str, Any]) -> bool:
    previous: tuple[str, ...] | None = None
    stable = 0
    stopped = False
    for _attempt in range(10):
        current = _named_container_ids(cli, "watchtower")
        if len(current) > 1:
            raise DockerUpdateError("Watchtower-Inventar wurde während des Stopps mehrdeutig.")
        host_running = set(_running_watchtower_image_ids(cli))
        foreign_running = host_running - set(current)
        if foreign_running:
            raise DockerUpdateError(
                "Während des Watchtower-Stopps erschien ein fremder Update-Supervisor."
            )
        identity_running = bool(
            current and _validate_watchtower_identity(cli, current[0], projection)
        )
        running = bool(host_running & set(current)) or identity_running
        if running:
            result = cli.compose(
                ["--profile", "auto-update", "stop", "--timeout", "30", "watchtower"],
                timeout=60,
            )
            _require_success(result, "Watchtower-Stopp vor dem E3DC-Update")
            stopped = True
        if not running and current == previous:
            stable += 1
            if stable >= 2:
                return stopped
        else:
            stable = 0
        previous = current
        time.sleep(1)
    raise DockerUpdateError("Der Watchtower-Stillstand konnte nicht bestätigt werden.")


def _env_sets_compose_file(data: bytes) -> bool:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DockerUpdateError(".env ist nicht gültig UTF-8-kodiert.") from exc
    for line in text.splitlines():
        value = line.strip()
        if not value or value.startswith("#"):
            continue
        if value.startswith("export "):
            value = value[7:].lstrip()
        if re.match(r"COMPOSE_(?:FILE|PATH_SEPARATOR)\s*=", value):
            return True
    return False


def _classify_compose_source(data: bytes) -> tuple[str, str]:
    normalised = _normalised_lf_bytes(data)
    legacy_hash = hashlib.sha256(normalised).hexdigest()
    topology = KNOWN_542_LEGACY_HASHES.get(legacy_hash)
    try:
        active = _active_yaml_lines(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise DockerUpdateError("Compose ist nicht gültig UTF-8-kodiert.") from exc
    current_topology = KNOWN_CURRENT_TEMPLATES.get(active)
    if topology is None and current_topology is None:
        raise DockerUpdateError(
            "Automatisch migriert werden nur die unveränderten veröffentlichten "
            "5.4.2–5.4.2d-Compose-Dateien oder ihre Installer-Bind-Mount-Variante. "
            "Ältere beziehungsweise angepasste Dateien müssen manuell geprüft werden."
        )
    if topology is not None and current_topology is not None:
        raise DockerUpdateError("Der Compose-Stand ist strukturell mehrdeutig.")
    return topology or current_topology or "", "legacy" if topology is not None else "current"


def _prepare_compose_contract(cli: DockerCli) -> dict[str, Any]:
    compose_dir = cli.compose_dir
    _validate_directory_chain(compose_dir)
    if os.environ.get("COMPOSE_FILE") or os.environ.get("COMPOSE_PATH_SEPARATOR"):
        raise DockerUpdateError(
            "COMPOSE_FILE/COMPOSE_PATH_SEPARATOR darf den gebundenen E3DC-Pfad nicht überlagern."
        )
    for competing in (
        "compose.yml",
        "compose.yaml",
        "docker-compose.yaml",
        "docker-compose.override.yml",
        "docker-compose.override.yaml",
        "compose.override.yml",
        "compose.override.yaml",
    ):
        if os.path.lexists(compose_dir / competing):
            raise DockerUpdateError(
                f"Neben {COMPOSE_FILENAME} existiert die konkurrierende Datei {competing}."
            )

    directory_info = os.stat(compose_dir, follow_symlinks=False)
    if not stat.S_ISDIR(directory_info.st_mode) or stat.S_ISLNK(directory_info.st_mode):
        raise DockerUpdateError("Der Compose-Pfad ist kein eindeutiges reales Verzeichnis.")
    if directory_info.st_uid not in _allowed_file_owners():
        raise DockerUpdateError("Der Compose-Pfad besitzt einen unerwarteten Eigentümer.")
    directory_mode = stat.S_IMODE(directory_info.st_mode)
    if directory_mode & 0o022 or directory_mode & 0o7000:
        raise DockerUpdateError(
            f"Der Compose-Pfad besitzt einen unsicheren Modus {directory_mode:04o}."
        )
    directory_fd = os.open(
        compose_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    candidate_name = ""
    try:
        opened_dir = os.fstat(directory_fd)
        if _stat_identity(opened_dir) != _stat_identity(directory_info):
            raise DockerUpdateError("Der Compose-Pfad wechselte beim sicheren Öffnen.")
        source = _secure_snapshot(
            directory_fd,
            COMPOSE_FILENAME,
            required=True,
            max_size=MAX_COMPOSE_BYTES,
        )
        if source is None:
            raise DockerUpdateError(f"{COMPOSE_FILENAME} fehlt.")
        env_snapshot = _secure_snapshot(
            directory_fd,
            ".env",
            required=False,
            max_size=MAX_ENV_BYTES,
        )
        if env_snapshot is not None and _env_sets_compose_file(env_snapshot["data"]):
            raise DockerUpdateError(".env enthält eine mehrdeutige COMPOSE_FILE-Überlagerung.")

        topology, compose_state = _classify_compose_source(source["data"])
        if topology is None:
            raise DockerUpdateError("Die Compose-Topologie konnte nicht gebunden werden.")

        before_projection = _compose_projection(cli, cli.compose_file)
        selected = str(
            ((before_projection.get("services") or {}).get(SERVICE_NAME) or {}).get("image")
            or ""
        )
        _require_official_image(selected)
        _validate_e3dc_container_binding(
            cli,
            before_projection,
            require_role=compose_state == "current",
        )
        watchtower_stopped = _stop_update_watchtower(cli, before_projection)
        _require_same_snapshot(
            directory_fd,
            source,
            name=COMPOSE_FILENAME,
            max_size=MAX_COMPOSE_BYTES,
        )
        _require_same_snapshot(
            directory_fd,
            env_snapshot,
            name=".env",
            max_size=MAX_ENV_BYTES,
        )
        if _compose_projection(cli, cli.compose_file) != before_projection:
            raise DockerUpdateError("Die Compose-Projektion driftete nach dem Watchtower-Stopp.")

        if compose_state == "current":
            _validate_projection_delta(before_projection, before_projection, topology=topology)
            print("✓ Compose-Pflichtvertrag ist bereits aktuell und blieb bytegleich.", flush=True)
            if watchtower_stopped:
                print("✓ Watchtower ist für das gebundene Host-Update bestätigt gestoppt.", flush=True)
            return {
                "state": "current",
                "topology": topology,
                "compose": source,
                "env": env_snapshot,
                "projection": before_projection,
            }

        candidate_data = _project_current_compose(source["data"], topology=topology)
        candidate_name, candidate_path = _write_candidate(
            directory_fd,
            compose_dir,
            candidate_data,
            source,
        )
        candidate_projection = _compose_projection(cli, candidate_path)
        _validate_projection_delta(before_projection, candidate_projection, topology=topology)
        _stop_update_watchtower(cli, before_projection)
        _require_same_snapshot(
            directory_fd,
            source,
            name=COMPOSE_FILENAME,
            max_size=MAX_COMPOSE_BYTES,
        )
        _require_same_snapshot(
            directory_fd,
            env_snapshot,
            name=".env",
            max_size=MAX_ENV_BYTES,
        )
        if _compose_projection(cli, cli.compose_file) != before_projection:
            raise DockerUpdateError("Die Compose-Projektion driftete vor dem atomaren Replace.")
        bound_source = _replace_candidate(
            directory_fd,
            candidate_name,
            candidate_data,
            source,
        )
        candidate_name = ""
        try:
            final_projection = _compose_projection(cli, cli.compose_file)
            if final_projection != candidate_projection:
                raise DockerUpdateError(
                    "Der Compose-Readback projiziert nicht den geprüften Kandidaten."
                )
        except BaseException as exc:
            _restore_compose_preimage(
                directory_fd,
                compose_dir,
                source,
                expected_current_data=candidate_data,
            )
            raise DockerUpdateError(
                f"Compose-Endprüfung fehlgeschlagen; der gebundene Preimage wurde wiederhergestellt: {exc}"
            ) from exc
        print(
            "✓ Compose 5.4.2 wurde atomar auf den aktuellen Host-Vertrag migriert; "
            "Daten- und Logtopologie blieb unverändert.",
            flush=True,
        )
        if watchtower_stopped:
            print("✓ Watchtower bleibt nach der sicheren Migration bewusst gestoppt.", flush=True)
        return {
            "state": "migrated",
            "topology": topology,
            "compose": bound_source,
            "env": env_snapshot,
            "projection": final_projection,
        }
    finally:
        if candidate_name:
            try:
                os.unlink(candidate_name, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def _require_prepared_contract(
    cli: DockerCli,
    contract: dict[str, Any],
    *,
    require_role: bool = False,
) -> None:
    compose_dir = cli.compose_dir
    _validate_directory_chain(compose_dir)
    if os.environ.get("COMPOSE_FILE") or os.environ.get("COMPOSE_PATH_SEPARATOR"):
        raise DockerUpdateError("Die Compose-Pfadüberlagerung driftete nach dem Preflight.")
    for competing in (
        "compose.yml",
        "compose.yaml",
        "docker-compose.yaml",
        "docker-compose.override.yml",
        "docker-compose.override.yaml",
        "compose.override.yml",
        "compose.override.yaml",
    ):
        if os.path.lexists(compose_dir / competing):
            raise DockerUpdateError(f"Die konkurrierende Compose-Datei {competing} entstand neu.")
    directory_fd = os.open(
        compose_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        _require_same_snapshot(
            directory_fd,
            contract.get("compose"),
            name=COMPOSE_FILENAME,
            max_size=MAX_COMPOSE_BYTES,
        )
        _require_same_snapshot(
            directory_fd,
            contract.get("env"),
            name=".env",
            max_size=MAX_ENV_BYTES,
        )
    finally:
        os.close(directory_fd)
    if _compose_projection(cli, cli.compose_file) != contract.get("projection"):
        raise DockerUpdateError("Die vollständige Compose-Projektion driftete nach dem Preflight.")
    _stop_update_watchtower(cli, contract.get("projection") or {})
    _validate_e3dc_container_binding(
        cli,
        contract.get("projection") or {},
        require_role=require_role,
    )


def _selected_image(cli: DockerCli) -> str:
    output = _require_success(
        cli.compose(["config", "--images"]),
        "Compose-Imageprojektion",
    )
    images = tuple(line.strip() for line in output.splitlines() if line.strip())
    if len(images) != 1:
        raise DockerUpdateError(
            "Compose muss ohne aktiviertes Zusatzprofil genau ein Image projizieren; "
            f"gefunden: {len(images)}."
        )
    return images[0]


def _image_contract(
    cli: DockerCli,
    image: str,
    *,
    legacy_no_healthcheck_version: str = "",
) -> dict[str, str]:
    output = _require_success(
        cli.run(["image", "inspect", image]),
        "Inspektion des gezogenen Images",
    )
    try:
        values = json.loads(output)
        if len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError("kein eindeutiges Image")
        info = values[0]
        image_id = str(info["Id"])
        labels = (info.get("Config") or {}).get("Labels") or {}
        image_version = _normalise_version(labels.get("org.opencontainers.image.version"))
        health_test = tuple(
            ((info.get("Config") or {}).get("Healthcheck") or {}).get("Test") or ()
        )
    except (KeyError, TypeError, ValueError) as exc:
        raise DockerUpdateError(f"Image-Metadaten sind ungültig: {exc}") from exc

    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image_id):
        raise DockerUpdateError("Das gezogene Image besitzt keine gebundene sha256-ID.")
    tagged_version = _tag_version(image)
    legacy_version = _normalise_version(legacy_no_healthcheck_version)
    if legacy_version and (
        legacy_version != LEGACY_NO_HEALTHCHECK_VERSION
        or tagged_version != LEGACY_NO_HEALTHCHECK_VERSION
    ):
        raise DockerUpdateError(
            "Die Healthcheck-Ausnahme ist ausschließlich für den explizit "
            "getaggten historischen Rückfall v5.3.2b zulässig."
        )
    if not image_version or image_version == "unknown":
        if not legacy_version or tagged_version != legacy_version:
            raise DockerUpdateError("Das gezogene Image besitzt keine OCI-Release-Version.")
        image_version = legacy_version
    if not re.fullmatch(r"\d+\.\d+\.\d+[A-Za-z0-9._-]*", image_version):
        raise DockerUpdateError("Die OCI-Release-Version besitzt kein gültiges Format.")
    if tagged_version and image_version != tagged_version:
        raise DockerUpdateError(
            "Image-Tag und OCI-Release-Version widersprechen sich: "
            f"{tagged_version} gegenüber {image_version}."
        )

    expected_health = ("CMD", *CONTAINER_HEALTHCHECK_COMMAND)
    legacy_health = bool(
        legacy_version == LEGACY_NO_HEALTHCHECK_VERSION
        and tagged_version == LEGACY_NO_HEALTHCHECK_VERSION
        and image_version == LEGACY_NO_HEALTHCHECK_VERSION
    )
    if not legacy_health and health_test != expected_health:
        raise DockerUpdateError(
            "Das gezogene Image besitzt nicht den freigegebenen E3DC-Healthcheck."
        )
    return {
        "image": image,
        "image_id": image_id,
        "version": image_version,
        "legacy_without_healthcheck": "1" if legacy_health else "0",
    }


def _container_ids(cli: DockerCli, *, include_stopped: bool = False) -> tuple[str, ...]:
    arguments = ["ps", "-q"]
    if include_stopped:
        arguments.append("-a")
    arguments.append(SERVICE_NAME)
    output = _require_success(
        cli.compose(arguments),
        "Compose-Containerinventar",
    )
    return tuple(line.strip() for line in output.splitlines() if line.strip())


def _inspect_container(cli: DockerCli, container_id: str) -> dict[str, Any]:
    output = _require_success(
        cli.run(["inspect", container_id]),
        "Containerinspektion",
    )
    try:
        values = json.loads(output)
        if len(values) != 1 or not isinstance(values[0], dict):
            raise ValueError("kein eindeutiger Container")
        return values[0]
    except (TypeError, ValueError) as exc:
        raise DockerUpdateError(f"Container-Metadaten sind ungültig: {exc}") from exc


def _runtime_version(cli: DockerCli, container_id: str) -> str:
    output = _require_success(
        cli.run(
            ["exec", container_id, "cat", "/app/pi/Install/VERSION"],
            timeout=COMMAND_TIMEOUT_S,
        ),
        "Laufzeit-Versionsprüfung",
    )
    version = _normalise_version(output)
    if not version:
        raise DockerUpdateError("Die Laufzeit-VERSION ist leer.")
    return version


def _stable_runtime_snapshot(
    cli: DockerCli,
    contract: dict[str, str],
) -> tuple[str, dict[str, Any]]:
    container_ids = _container_ids(cli)
    if len(container_ids) != 1:
        raise DockerUpdateError(
            "Compose meldet nicht genau einen laufenden E3DC-Control-Container."
        )
    container_id = container_ids[0]
    before = _inspect_container(cli, container_id)
    state = before.get("State") or {}
    config = before.get("Config") or {}
    if state.get("Running") is not True:
        raise DockerUpdateError("Der Updatekandidat läuft nicht.")
    if str(before.get("Image") or "") != contract["image_id"]:
        raise DockerUpdateError("Der Updatekandidat verwendet nicht die gezogene Image-ID.")
    if str(config.get("Image") or "") != contract["image"]:
        raise DockerUpdateError("Der Updatekandidat verwendet eine andere Image-Referenz.")

    legacy_health = contract["legacy_without_healthcheck"] == "1"
    health_status = str((state.get("Health") or {}).get("Status") or "")
    if not legacy_health and health_status != "healthy":
        raise DockerUpdateError(
            f"Der Updatekandidat ist nicht gesund: {health_status or 'Health fehlt'}."
        )

    version = _runtime_version(cli, container_id)
    if version != contract["version"]:
        raise DockerUpdateError(
            "Gezogene Image-Version und Laufzeit-VERSION widersprechen sich: "
            f"{contract['version']} gegenüber {version}."
        )

    health_payload: dict[str, Any] = {"legacy_without_healthcheck": True}
    if not legacy_health:
        health_output = _require_success(
            cli.run(
                ["exec", container_id, *CONTAINER_HEALTHCHECK_COMMAND],
                timeout=COMMAND_TIMEOUT_S,
            ),
            "Image-Healthcheck",
        )
        try:
            health_payload = json.loads(health_output)
        except (TypeError, ValueError) as exc:
            raise DockerUpdateError(f"Health-Snapshot ist kein gültiges JSON: {exc}") from exc
        if not isinstance(health_payload, dict) or not health_payload:
            raise DockerUpdateError("Health-Snapshot enthält keinen Dienstsatz.")

    after = _inspect_container(cli, container_id)
    after_state = after.get("State") or {}
    signature = json.dumps(
        {
            "container_id": str(after.get("Id") or ""),
            "image_id": str(after.get("Image") or ""),
            "image_ref": str((after.get("Config") or {}).get("Image") or ""),
            "restart_count": int(after.get("RestartCount") or 0),
            "started_at": str(after_state.get("StartedAt") or ""),
            "pid": int(after_state.get("Pid") or 0),
            "running": after_state.get("Running") is True,
            "health": str((after_state.get("Health") or {}).get("Status") or ""),
            "version": version,
            "services": health_payload,
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    if (
        str(before.get("Id") or ""),
        str(before.get("Image") or ""),
        int(before.get("RestartCount") or 0),
        str(state.get("StartedAt") or ""),
        int(state.get("Pid") or 0),
    ) != (
        str(after.get("Id") or ""),
        str(after.get("Image") or ""),
        int(after.get("RestartCount") or 0),
        str(after_state.get("StartedAt") or ""),
        int(after_state.get("Pid") or 0),
    ):
        raise DockerUpdateError("Der Updatekandidat wechselte während des Snapshots.")
    return signature, health_payload


def _verify_candidate(cli: DockerCli, contract: dict[str, str]) -> dict[str, Any]:
    first_signature, first_payload = _stable_runtime_snapshot(cli, contract)
    time.sleep(2)
    second_signature, second_payload = _stable_runtime_snapshot(cli, contract)
    if first_signature != second_signature or first_payload != second_payload:
        raise DockerUpdateError(
            "Containeridentität, Version oder Dienstsatz blieb nicht über zwei Snapshots stabil."
        )
    return {
        "schema": "e3dc_docker_update_result_v1",
        "image": contract["image"],
        "image_id": contract["image_id"],
        "version": contract["version"],
        "stable_snapshots": 2,
        "services": second_payload,
    }


def _stop_candidate(
    cli: DockerCli,
    *,
    expected_image_id: str,
    expected_projection: dict[str, Any],
) -> bool:
    previous_ids: tuple[str, ...] | None = None
    stable = 0
    bound_seen = False
    contract_drift = False
    stop_error = ""
    last_running: list[str] = []
    for _attempt in range(10):
        try:
            ids = _named_container_ids(cli, SERVICE_NAME)
            if len(ids) > 1:
                raise CandidateStopError("Globales e3dc-control-Inventar ist mehrdeutig.")
            running = []
            for container_id in ids:
                info = _e3dc_stop_authority(cli, container_id)
                try:
                    _validate_e3dc_container_binding(
                        cli,
                        expected_projection,
                        require_role=False,
                    )
                except DockerUpdateError:
                    contract_drift = True
                if str(info.get("Image") or "") != expected_image_id:
                    contract_drift = True
                bound_seen = True
                if (info.get("State") or {}).get("Running") is True:
                    running.append(container_id)
                    stop_result = cli.run(
                        ["stop", "--time", "30", container_id],
                        timeout=60,
                    )
                    if stop_result.returncode != 0:
                        stop_error = (
                            stop_result.stderr
                            or stop_result.stdout
                            or str(stop_result.returncode)
                        ).strip()
        except Exception as exc:
            raise CandidateStopError(
                f"Kandidatenstillstand ist nicht inventarisierbar: {exc}"
            ) from exc
        last_running = running
        if bound_seen and not running and ids == previous_ids:
            stable += 1
            if stable >= 2:
                return contract_drift
        else:
            stable = 0
        previous_ids = ids
        time.sleep(1)
    detail = ", ".join(last_running) or stop_error or "unbekannter Zustand"
    if not bound_seen:
        detail = "nach begonnenem Start erschien keine eindeutig stoppbare Containeridentität"
    raise CandidateStopError(
        "Der fehlerhafte Updatekandidat ist nicht bestätigt gestoppt: " + detail
    )


def _diagnostics(cli: DockerCli) -> None:
    for arguments in (
        ["ps"],
        ["logs", "--tail=80", SERVICE_NAME],
    ):
        try:
            result = cli.compose(arguments, timeout=COMMAND_TIMEOUT_S)
        except Exception as exc:
            print(f"Docker-Diagnose fehlgeschlagen: {exc}", file=sys.stderr)
            continue
        output = (result.stdout or result.stderr or "").strip()
        if output:
            print(output, file=sys.stderr)


def update_container(args: argparse.Namespace) -> dict[str, Any]:
    compose_dir = Path(os.path.abspath(Path(args.compose_dir).expanduser()))
    if compose_dir == Path("/") or not compose_dir.is_dir():
        raise DockerUpdateError("Der Compose-Pfad ist kein eindeutiges Verzeichnis.")

    environment = os.environ.copy()
    if args.image_tag:
        if not re.fullmatch(r"v?\d+\.\d+\.\d+[A-Za-z0-9._-]*", args.image_tag):
            raise DockerUpdateError("Der angegebene Image-Tag ist ungültig.")
        environment["E3DC_IMAGE_TAG"] = args.image_tag
    cli = DockerCli(compose_dir, use_sudo=args.sudo, environment=environment)

    compose_contract = _prepare_compose_contract(cli)
    pre_up_role_required = compose_contract.get("state") == "current"
    _require_prepared_contract(
        cli,
        compose_contract,
        require_role=pre_up_role_required,
    )

    selected_before = _require_official_image(_selected_image(cli))
    if args.image_tag:
        expected = f"ghcr.io/a9xxx/install-e3dc-control:{args.image_tag}"
        if selected_before != expected:
            raise DockerUpdateError(
                f"Compose projiziert {selected_before} statt des angeforderten {expected}."
            )
    if args.recreate_current:
        print(f"→ Binde das bereits lokale Image für den reinen Neustart: {selected_before} …", flush=True)
    else:
        print(f"→ Ziehe explizit {selected_before} …", flush=True)
        _require_success(
            cli.compose(
                ["pull", SERVICE_NAME],
                timeout=max(args.wait_timeout, 300),
                capture=False,
            ),
            "Docker-Image-Pull",
        )
    _require_prepared_contract(
        cli,
        compose_contract,
        require_role=pre_up_role_required,
    )
    selected_after = _require_official_image(_selected_image(cli))
    if selected_after != selected_before:
        raise DockerUpdateError("Die Compose-Imageprojektion driftete während des Pulls.")
    contract = _image_contract(
        cli,
        selected_after,
        legacy_no_healthcheck_version=args.legacy_no_healthcheck_version,
    )
    print(
        f"✓ Gezogene Identität gebunden: {contract['image_id']} / Version {contract['version']}",
        flush=True,
    )

    candidate_started = False
    try:
        _require_prepared_contract(
            cli,
            compose_contract,
            require_role=pre_up_role_required,
        )
        pre_start_contract = _image_contract(
            cli,
            selected_after,
            legacy_no_healthcheck_version=args.legacy_no_healthcheck_version,
        )
        if pre_start_contract != contract:
            raise DockerUpdateError(
                "Die lokale Tag-, Image-ID-, OCI- oder Health-Bindung driftete vor dem Start."
            )
        candidate_started = True
        up_result = cli.compose(
            [
                "up",
                "-d",
                "--pull",
                "never",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                str(args.wait_timeout),
                SERVICE_NAME,
            ],
            timeout=args.wait_timeout + START_TIMEOUT_GRACE_S,
            capture=False,
        )
        _require_success(up_result, "Compose-Kandidatenstart")
        _require_prepared_contract(cli, compose_contract, require_role=True)
        if _require_official_image(_selected_image(cli)) != contract["image"]:
            raise DockerUpdateError("Compose-Imageprojektion driftete nach dem Start.")
        result = _verify_candidate(cli, contract)
        _require_prepared_contract(cli, compose_contract, require_role=True)
        return result
    except BaseException as exc:
        if candidate_started:
            try:
                stopped_with_contract_drift = _stop_candidate(
                    cli,
                    expected_image_id=contract["image_id"],
                    expected_projection=compose_contract["projection"],
                )
            except CandidateStopError as stop_exc:
                raise CandidateStopError(
                    f"{exc}; zusätzlich blieb der Kandidatenstopp unbestätigt: {stop_exc}"
                ) from exc
            print("✓ Fehlerhafter Updatekandidat ist bestätigt gestoppt.", file=sys.stderr)
            _diagnostics(cli)
            if stopped_with_contract_drift:
                raise DockerUpdateError(
                    f"{exc}; der per Stop-Autorität gebundene Container wurde gestoppt, "
                    "wich aber bei Image, Projekt oder persistenten Mounts vom Kandidatenvertrag ab."
                ) from exc
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="E3DC-Control-Compose-Image sicher ziehen, starten und verifizieren"
    )
    parser.add_argument("--compose-dir", default=".", help="Verzeichnis der docker-compose.yml")
    parser.add_argument("--sudo", action="store_true", help="Docker über sudo aufrufen")
    parser.add_argument("--image-tag", default="", help="Expliziter offizieller Release-Tag")
    parser.add_argument(
        "--recreate-current",
        action="store_true",
        help="Bereits lokales Image ohne Pull neu starten und vollständig prüfen",
    )
    parser.add_argument(
        "--legacy-no-healthcheck-version",
        default="",
        help="Nur für den dokumentierten historischen Rückfall ohne Image-Healthcheck",
    )
    parser.add_argument(
        "--wait-timeout",
        type=int,
        default=DEFAULT_WAIT_TIMEOUT_S,
        help="Compose-Health-Wartezeit in Sekunden",
    )
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.recreate_current and args.image_tag:
        print("--recreate-current und --image-tag dürfen nicht kombiniert werden.", file=sys.stderr)
        return 2
    if args.wait_timeout < 60 or args.wait_timeout > 1800:
        print("Docker-Wartezeit muss zwischen 60 und 1800 Sekunden liegen.", file=sys.stderr)
        return 2
    try:
        result = update_container(args)
    except CandidateStopError as exc:
        print(f"SICHERHEITSSTOPP: {exc}", file=sys.stderr)
        return 70
    except (DockerUpdateError, OSError, subprocess.SubprocessError) as exc:
        print(f"Docker-Update fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
