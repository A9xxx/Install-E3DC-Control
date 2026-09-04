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
import signal
import stat
import subprocess
import sys
import threading
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
DEFAULT_PULL_TIMEOUT_S = 900
COMMAND_TIMEOUT_S = 30
START_TIMEOUT_GRACE_S = 60
PROCESS_GROUP_TERM_GRACE_S = 5.0
PROCESS_GROUP_KILL_GRACE_S = 5.0
PROCESS_GROUP_POLL_S = 0.1
PROCESS_SIGNAL_POLL_S = 0.5
TIMEOUT_OUTPUT_MAX_CHARS = 4000
COMPOSE_FILENAME = "docker-compose.yml"
MAX_COMPOSE_BYTES = 512 * 1024
MAX_ENV_BYTES = 128 * 1024
MIN_DOCKER_FREE_BYTES = 2 * 1024 * 1024 * 1024
DOCKER_STORAGE_FULL_CODE = "DOCKER_STORAGE_FULL"
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
PUBLISHED_532B_NORMALISED_HASHES = {
    # v5.3.2b enthält historisch gemischte LF-/CRLF-Zeilenenden. Gebunden wird
    # deshalb der inhaltlich identische, ausschließlich auf LF normalisierte Blob.
    "cb30967959cac6df8476ecf8cb1dfc5258a92a327afd51a6e2c79c8de27939ef",
}


class DockerUpdateError(RuntimeError):
    """Der Kandidat erfüllt den gebundenen Updatevertrag nicht."""


class CandidateStopError(DockerUpdateError):
    """Der Kandidatenstillstand konnte nicht bewiesen werden."""


class DockerStorageFullError(DockerUpdateError):
    """Der Docker-Datenträger besitzt nicht genug nachgewiesenen Freiraum."""


class _DeferredDockerSignal(BaseException):
    """Verschiebt einen Terminalabbruch bis nach dem sicheren Prozessgruppenstopp."""

    def __init__(self, signum: int):
        self.signum = int(signum)
        super().__init__(f"Elternprozess erhielt Signal {self.signum}")


class _DockerSignalGuard:
    """Bindet SIGINT/SIGTERM, solange genau ein Docker-Aufruf läuft."""

    def __init__(self) -> None:
        self.requested_signum: int | None = None
        self._previous_handlers: dict[int, object] = {}
        self._previous_mask = None
        self._installed = False
        self._armed = False

    def install(self) -> None:
        if (
            os.name != "posix"
            or threading.current_thread() is not threading.main_thread()
        ):
            return
        for signum in (signal.SIGINT, signal.SIGTERM):
            self._previous_handlers[signum] = signal.getsignal(signum)
            signal.signal(signum, self._handle)
        self._installed = True

    def _handle(self, signum, _frame) -> None:
        if self.requested_signum is not None:
            return
        self.requested_signum = int(signum)
        if self._armed and hasattr(signal, "pthread_sigmask"):
            self._previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGINT, signal.SIGTERM},
            )

    def arm(self) -> None:
        self._armed = True
        if (
            self.requested_signum is not None
            and self._previous_mask is None
            and hasattr(signal, "pthread_sigmask")
        ):
            self._previous_mask = signal.pthread_sigmask(
                signal.SIG_BLOCK,
                {signal.SIGINT, signal.SIGTERM},
            )
        self.raise_if_requested()

    def raise_if_requested(self) -> None:
        if self.requested_signum is not None:
            raise _DeferredDockerSignal(self.requested_signum)

    def restore(self) -> None:
        if not self._installed:
            return
        self._installed = False
        for signum, previous in self._previous_handlers.items():
            signal.signal(signum, previous)
        if self._previous_mask is not None:
            signal.pthread_sigmask(signal.SIG_SETMASK, self._previous_mask)


def _process_group_has_live_members(process_group: int) -> bool:
    """Behandelt beendete, noch nicht von PID 1 geerntete Zombies als inaktiv."""

    proc_root = Path("/proc")
    if os.name == "posix" and proc_root.is_dir():
        for entry in proc_root.iterdir():
            if not entry.name.isdecimal():
                continue
            try:
                value = (entry / "stat").read_text(encoding="ascii")
                closing = value.rfind(")")
                fields = value[closing + 2 :].split()
                state = fields[0]
                group = int(fields[2])
            except (IndexError, OSError, UnicodeError, ValueError):
                continue
            if group == process_group and state != "Z":
                return True
        return False
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _signal_bound_process_group(
    process: subprocess.Popen[str],
    process_group: int,
    signum: int,
) -> None:
    """Sendet ein Signal ausschließlich an die für diesen Aufruf erzeugte Gruppe."""

    try:
        os.killpg(process_group, signum)
    except ProcessLookupError:
        return
    except PermissionError as exc:
        # Der dokumentierte sudo-Aufruf startet den gesamten Updater als Root.
        # Ein erst hier verschachteltes sudo kann wegen sudo-use_pty eine andere
        # Prozessgruppe erzeugen und ist deshalb bereits im Konstruktor gesperrt.
        if process.poll() is not None:
            return
        raise DockerUpdateError(
            "Die gebundene Docker-Prozessgruppe konnte nicht signalisiert werden."
        ) from exc
    except OSError:
        if process.poll() is not None:
            return
        try:
            process.kill() if signum == signal.SIGKILL else process.terminate()
        except ProcessLookupError:
            pass


def _communicate_until(
    process: subprocess.Popen[str],
    *,
    timeout: float,
    signal_guard: _DockerSignalGuard,
) -> tuple[str | None, str | None]:
    """Pollt blockierend, damit Terminalsignale nur außerhalb des Handlers wirken."""

    started = time.monotonic()
    last_stdout = None
    last_stderr = None
    while True:
        signal_guard.raise_if_requested()
        remaining = float(timeout) - (time.monotonic() - started)
        if remaining <= 0:
            raise subprocess.TimeoutExpired(
                process.args,
                timeout,
                output=last_stdout,
                stderr=last_stderr,
            )
        try:
            result = process.communicate(timeout=min(PROCESS_SIGNAL_POLL_S, remaining))
            signal_guard.raise_if_requested()
            return result
        except subprocess.TimeoutExpired as exc:
            if exc.output is not None:
                last_stdout = exc.output
            if exc.stderr is not None:
                last_stderr = exc.stderr


def _wait_for_bound_group_stop(
    process: subprocess.Popen[str],
    process_group: int,
    *,
    timeout: float,
) -> tuple[str | None, str | None, bool]:
    """Erntet das direkte Kind und bestätigt, dass kein lebendes Gruppenmitglied bleibt."""

    deadline = time.monotonic() + max(0.0, float(timeout))
    stdout = None
    stderr = None
    communication_done = False
    while True:
        remaining = deadline - time.monotonic()
        if not communication_done:
            try:
                stdout, stderr = process.communicate(
                    timeout=max(0.01, min(PROCESS_GROUP_POLL_S, remaining))
                )
                communication_done = True
            except subprocess.TimeoutExpired:
                pass
        else:
            process.poll()
        group_live = _process_group_has_live_members(process_group)
        if communication_done and not group_live:
            return stdout, stderr, True
        if remaining <= 0:
            return stdout, stderr, False
        if communication_done:
            time.sleep(min(PROCESS_GROUP_POLL_S, remaining))


def _terminate_bound_process_group(
    process: subprocess.Popen[str],
    process_group: int,
) -> tuple[str | None, str | None]:
    """TERM, kurze Bestätigung, dann KILL und vollständiges Reaping."""

    _signal_bound_process_group(
        process,
        process_group,
        signal.SIGTERM,
    )
    stdout, stderr, stopped = _wait_for_bound_group_stop(
        process,
        process_group,
        timeout=PROCESS_GROUP_TERM_GRACE_S,
    )
    if not stopped:
        _signal_bound_process_group(
            process,
            process_group,
            signal.SIGKILL,
        )
        stdout, stderr, stopped = _wait_for_bound_group_stop(
            process,
            process_group,
            timeout=PROCESS_GROUP_KILL_GRACE_S,
        )
    if not stopped:
        raise DockerUpdateError(
            "Die ausschließlich diesem Docker-Aufruf zugeordnete Prozessgruppe "
            "konnte nach TERM und KILL nicht vollständig beendet werden."
        )
    return stdout, stderr


def _raise_deferred_docker_signal(signum: int) -> None:
    if signum == signal.SIGINT:
        raise KeyboardInterrupt
    raise SystemExit(128 + int(signum))


def _run_bound_process_group(
    command: list[str],
    *,
    cwd: str,
    environment: dict[str, str],
    timeout: float,
    capture: bool,
) -> subprocess.CompletedProcess[str]:
    """Führt genau einen Befehl in einer eigenen, sicher stoppbaren Gruppe aus."""

    signal_guard = _DockerSignalGuard()
    signal_guard.install()
    process: subprocess.Popen[str] | None = None
    deferred_signum: int | None = None
    pending_exception: BaseException | None = None
    try:
        process = subprocess.Popen(
            command,
            cwd=cwd,
            env=environment,
            stdout=subprocess.PIPE if capture else None,
            stderr=subprocess.PIPE if capture else None,
            text=True,
            start_new_session=os.name == "posix",
        )
        process_group = process.pid
        try:
            signal_guard.arm()
            stdout, stderr = _communicate_until(
                process,
                timeout=timeout,
                signal_guard=signal_guard,
            )
        except subprocess.TimeoutExpired as exc:
            stdout, stderr = _terminate_bound_process_group(
                process,
                process_group,
            )
            exc.output = stdout if stdout is not None else exc.output
            exc.stderr = stderr if stderr is not None else exc.stderr
            exc.e3dc_process_group_stopped = True
            pending_exception = exc
        except _DeferredDockerSignal as exc:
            _terminate_bound_process_group(
                process,
                process_group,
            )
            deferred_signum = exc.signum
            stdout = stderr = None
        except BaseException as exc:
            _terminate_bound_process_group(
                process,
                process_group,
            )
            pending_exception = exc
    finally:
        signal_guard.restore()
    if signal_guard.requested_signum is not None:
        deferred_signum = signal_guard.requested_signum
    if deferred_signum is not None:
        _raise_deferred_docker_signal(deferred_signum)
    if pending_exception is not None:
        raise pending_exception
    if process is None:
        raise DockerUpdateError("Docker-Prozess konnte nicht eindeutig gestartet werden.")
    return subprocess.CompletedProcess(
        process.args,
        int(process.returncode),
        stdout,
        stderr,
    )


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
        if use_sudo and os.geteuid() != 0:
            raise DockerUpdateError(
                "Für einen sicher gebundenen Docker-Prozessstopp muss der gesamte "
                "Updater mit sudo gestartet werden, zum Beispiel: sudo python3 "
                "./Installer/docker_compose_update.py --compose-dir . --sudo"
            )
        self.prefix: list[str] = []
        self.environment = environment

    def run(
        self,
        arguments: list[str],
        *,
        timeout: int = COMMAND_TIMEOUT_S,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        return _run_bound_process_group(
            [*self.prefix, "docker", *arguments],
            cwd=str(self.compose_dir),
            environment=self.environment,
            timeout=timeout,
            capture=capture,
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
        if re.search(
            r"(?:no space left on device|disk quota exceeded|not enough space)",
            detail,
            flags=re.IGNORECASE,
        ):
            raise DockerStorageFullError(_docker_storage_full_message(detail))
        raise DockerUpdateError(f"{label} fehlgeschlagen: {detail}")
    return (result.stdout or "").strip()


def _docker_storage_full_message(detail: str) -> str:
    technical = str(detail or "").strip()
    suffix = f"\nDocker meldet: {technical}" if technical else ""
    return (
        f"[{DOCKER_STORAGE_FULL_CODE}] Im Docker-Speicher ist nicht genug freier "
        "Platz für Download und sichere Image-Entpackung vorhanden. Der bisherige "
        "E3DC-Control-Container und seine Volumes werden nicht gelöscht.\n"
        "Diagnose:\n"
        "  sudo docker system df -v\n"
        "  sudo docker info --format '{{.DockerRootDir}}'\n"
        "  sudo df -h \"$(sudo docker info --format '{{.DockerRootDir}}')\"\n"
        "Danach gezielt nicht mehr benötigte Images oder andere Hostdateien prüfen; "
        "dieser Updater führt weder docker prune noch eine Volume-Löschung aus."
        + suffix
    )


def _docker_storage_preflight(cli: DockerCli) -> dict[str, Any]:
    """Bindet DockerRootDir und beweist einen konservativen lokalen Freiraum."""

    root_output = _require_success(
        cli.run(["info", "--format", "{{json .DockerRootDir}}"]),
        "Docker-Speicherpfad-Prüfung",
    )
    try:
        docker_root = str(json.loads(root_output) or "").strip()
    except (TypeError, ValueError) as exc:
        raise DockerUpdateError(
            f"DockerRootDir ist nicht eindeutig auswertbar: {exc}"
        ) from exc
    if not docker_root.startswith("/") or docker_root == "/":
        raise DockerUpdateError("DockerRootDir ist kein sicherer absoluter Hostpfad.")

    df_result = subprocess.run(
        [*cli.prefix, "/bin/df", "-Pk", "--", docker_root],
        cwd=str(cli.compose_dir),
        env=cli.environment,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT_S,
        check=False,
    )
    output = _require_success(df_result, "Docker-Freiraum-Prüfung")
    rows = [line.split() for line in output.splitlines() if line.strip()]
    if len(rows) < 2 or len(rows[-1]) < 4 or not rows[-1][3].isdigit():
        raise DockerUpdateError("Docker-Freiraum konnte nicht eindeutig ausgewertet werden.")
    available_bytes = int(rows[-1][3]) * 1024
    if available_bytes < MIN_DOCKER_FREE_BYTES:
        raise DockerStorageFullError(
            _docker_storage_full_message(
                f"DockerRootDir {docker_root}: {available_bytes // (1024 * 1024)} MiB frei; "
                f"mindestens {MIN_DOCKER_FREE_BYTES // (1024 * 1024)} MiB erforderlich"
            )
        )
    return {
        "docker_root": docker_root,
        "available_bytes": available_bytes,
        "minimum_bytes": MIN_DOCKER_FREE_BYTES,
    }


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
    for key in ("hostname", "image", "logging"):
        e3dc.pop(key, None)
    labels = e3dc.get("labels")
    if isinstance(labels, dict):
        labels.pop("com.centurylinklabs.watchtower.enable", None)
        if not labels:
            e3dc.pop("labels", None)
    environment = e3dc.get("environment")
    if isinstance(environment, dict):
        environment.pop("E3DC_CONTAINER_MODE", None)
        if not environment:
            e3dc.pop("environment", None)
    watchtower.pop("logging", None)
    profiles = watchtower.get("profiles")
    if isinstance(profiles, list):
        watchtower["profiles"] = [item for item in profiles if item != "auto-update"]
        if not watchtower["profiles"]:
            watchtower.pop("profiles", None)
    watch_environment = watchtower.get("environment")
    if isinstance(watch_environment, dict):
        watch_environment.pop("WATCHTOWER_LABEL_ENABLE", None)
        if not watch_environment:
            watchtower.pop("environment", None)
    volumes = e3dc.get("volumes")
    if isinstance(volumes, list):
        e3dc["volumes"] = [
            item
            for item in volumes
            if not (
                isinstance(item, dict)
                and str(item.get("target") or "")
                in {
                    "/var/lib/e3dc-control/ml",
                    "/var/lib/e3dc-control/forecast-evidence",
                    ROLE_VOLUME_TARGET,
                }
            )
        ]
    top_volumes = cleaned.get("volumes")
    if isinstance(top_volumes, dict):
        for name in ("e3dc_ml", "e3dc_forecast_evidence", ROLE_VOLUME_NAME):
            top_volumes.pop(name, None)
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
    environment = service.get("environment") or {}
    if str(environment.get("E3DC_CONTAINER_MODE") or "") != "1":
        raise DockerUpdateError("Die Docker-Laufzeitrolle E3DC_CONTAINER_MODE=1 fehlt.")
    watch_environment = watchtower.get("environment") or {}
    if str(watch_environment.get("WATCHTOWER_LABEL_ENABLE") or "").lower() != "true":
        raise DockerUpdateError("Watchtower ist nicht auf explizit markierte Container begrenzt.")
    if "auto-update" not in tuple(watchtower.get("profiles") or ()):
        raise DockerUpdateError("Watchtower besitzt nicht das explizite auto-update-Profil.")
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
    projected_volumes = [
        item
        for item in (projected_service.get("volumes") or ())
        if isinstance(item, dict) and str(item.get("type") or "") in {"bind", "volume"}
    ]
    expected_targets = {str(item.get("target") or "") for item in projected_volumes}
    mandatory_targets = {DATA_VOLUME_TARGET, LOG_VOLUME_TARGET}
    if require_role:
        mandatory_targets.add(ROLE_VOLUME_TARGET)
    if not mandatory_targets.issubset(expected_targets):
        raise DockerUpdateError("Die projizierten persistenten E3DC-Mounts sind unvollständig.")
    if len(expected_targets) != len(projected_volumes):
        raise DockerUpdateError("Die projizierten persistenten E3DC-Mountziele sind mehrdeutig.")
    private_targets = {
        "/var/lib/e3dc-control/ml",
        "/var/lib/e3dc-control/forecast-evidence",
        ROLE_VOLUME_TARGET,
    }
    for item in projected_volumes:
        target = str(item.get("target") or "")
        if target not in mandatory_targets | private_targets and not _safe_custom_mount(item):
            raise DockerUpdateError(
                f"Der projizierte Fremdmount {target or '<leer>'} ist nicht sicher read-only gebunden."
            )
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
    for expected in projected_volumes:
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
            if target in private_targets:
                raise DockerUpdateError("Ein privater E3DC-Mount darf kein Bind-Mount sein.")
            if not expected_source.startswith("/") or expected_source == "/":
                raise DockerUpdateError(f"Der Bind-Mount {target} besitzt keinen sicheren Quellpfad.")
            if os.path.realpath(str(actual.get("Source") or "")) != expected_source:
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


def _safe_custom_mount(item: dict[str, Any]) -> bool:
    target = str(item.get("target") or "")
    if not target.startswith("/") or target == "/":
        return False
    protected = (
        "/app",
        "/opt",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/etc/e3dc-control",
        "/var/www/html",
        "/var/lib/e3dc-control",
        "/var/run/docker.sock",
    )
    if any(target == root or target.startswith(root + "/") for root in protected):
        return False
    return bool(item.get("read_only")) and str(item.get("type") or "") in {
        "bind",
        "volume",
    }


def _legacy_532b_projection_topology(projection: dict[str, Any]) -> str:
    services = projection.get("services") or {}
    e3dc = services.get(SERVICE_NAME) or {}
    watchtower = services.get("watchtower") or {}
    image = _require_official_image(str(e3dc.get("image") or ""))
    if _tag_version(image) != LEGACY_NO_HEALTHCHECK_VERSION:
        raise DockerUpdateError("Die semantische Altmigration gilt nur für v5.3.2b.")
    if (
        str(e3dc.get("container_name") or "") != SERVICE_NAME
        or str(e3dc.get("network_mode") or "") != "host"
        or str(e3dc.get("restart") or "") != "unless-stopped"
    ):
        raise DockerUpdateError("Der v5.3.2b-Dienst besitzt keine sichere Standardidentität.")
    forbidden = {
        "build",
        "command",
        "entrypoint",
        "privileged",
        "devices",
        "cap_add",
        "pid",
        "ipc",
        "uts",
        "userns_mode",
        "security_opt",
        "sysctls",
        "develop",
    }
    if forbidden & set(e3dc):
        raise DockerUpdateError(
            "Die angepasste v5.3.2b-Compose-Datei enthält nicht automatisch "
            "migrierbare Prozess- oder Hardwareprivilegien."
        )

    volume_items = [item for item in (e3dc.get("volumes") or ()) if isinstance(item, dict)]
    mappings = {
        target: _volume_mapping(e3dc, target)
        for target in (DATA_VOLUME_TARGET, LOG_VOLUME_TARGET)
    }
    if any(len(items) != 1 for items in mappings.values()):
        raise DockerUpdateError("Die v5.3.2b-Daten- und Logpersistenz ist nicht eindeutig.")
    data_type = str(mappings[DATA_VOLUME_TARGET][0].get("type") or "")
    log_type = str(mappings[LOG_VOLUME_TARGET][0].get("type") or "")
    if data_type != log_type or data_type not in {"bind", "volume"}:
        raise DockerUpdateError("Daten und Logs verwenden keine einheitliche sichere Topologie.")
    for item in volume_items:
        target = str(item.get("target") or "")
        if target in {DATA_VOLUME_TARGET, LOG_VOLUME_TARGET}:
            if bool(item.get("read_only")):
                raise DockerUpdateError(f"Der persistente Mount {target} ist schreibgeschützt.")
            continue
        if not _safe_custom_mount(item):
            raise DockerUpdateError(
                f"Der benutzerdefinierte Mount {target or '<leer>'} ist nicht "
                "nachweislich read-only und außerhalb der Produktpfade."
            )

    watch_image = str(watchtower.get("image") or "").strip()
    if watch_image not in {"containrrr/watchtower", "containrrr/watchtower:latest"}:
        raise DockerUpdateError("Die v5.3.2b-Watchtower-Quelle ist nicht offiziell gebunden.")
    socket_mounts = _volume_mapping(watchtower, "/var/run/docker.sock")
    if len(socket_mounts) != 1 or str(socket_mounts[0].get("source") or "") != "/var/run/docker.sock":
        raise DockerUpdateError("Der v5.3.2b-Watchtower besitzt keinen eindeutigen Docker-Socket.")
    watch_persistent = [
        item
        for item in (watchtower.get("volumes") or ())
        if isinstance(item, dict) and str(item.get("type") or "") in {"bind", "volume"}
    ]
    if len(watch_persistent) != 1:
        raise DockerUpdateError("Der v5.3.2b-Watchtower besitzt zusätzliche Hostmounts.")
    watch_environment = watchtower.get("environment") or {}
    docker_host_override = (
        "DOCKER_HOST" in watch_environment
        if isinstance(watch_environment, dict)
        else any(str(item).startswith("DOCKER_HOST=") for item in watch_environment)
    )
    if forbidden & set(watchtower) or docker_host_override:
        raise DockerUpdateError(
            "Der v5.3.2b-Watchtower besitzt eine nicht migrierbare Prozess- oder Docker-Host-Anpassung."
        )
    if str(watchtower.get("container_name") or "") != "watchtower":
        raise DockerUpdateError("Der v5.3.2b-Watchtower besitzt einen fremden Containername.")
    return "installer_bind_mounts" if data_type == "bind" else "repo_named_volumes"


def _yaml_lines_for_semantic_migration(data: bytes) -> list[str]:
    normalised = _normalised_lf_bytes(data)
    try:
        text = normalised.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise DockerUpdateError("Compose ist nicht gültig UTF-8-kodiert.") from exc
    if not text.endswith("\n"):
        raise DockerUpdateError("Compose muss mit einem eindeutigen Zeilenende enden.")
    _active_yaml_lines(text)
    return text.splitlines()


def _mapping_span(lines: list[str], *, indent: int, key: str) -> tuple[int, int]:
    prefix = " " * indent + key + ":"
    matches = [
        index
        for index, line in enumerate(lines)
        if (_active_yaml_lines(line)[0] if _active_yaml_lines(line) else "") == prefix
    ]
    if len(matches) != 1:
        raise DockerUpdateError(f"Der YAML-Block {key} ist nicht eindeutig.")
    start = matches[0]
    end = len(lines)
    for index in range(start + 1, len(lines)):
        active = _active_yaml_lines(lines[index])
        if not active:
            continue
        content = active[0]
        child_indent = len(content) - len(content.lstrip(" "))
        if child_indent <= indent:
            end = index
            break
    return start, end


def _service_span(lines: list[str], service: str) -> tuple[int, int]:
    services_start, services_end = _mapping_span(lines, indent=0, key="services")
    prefix = "  " + service + ":"
    matches = [
        index
        for index in range(services_start + 1, services_end)
        if (_active_yaml_lines(lines[index])[0] if _active_yaml_lines(lines[index]) else "")
        == prefix
    ]
    if len(matches) != 1:
        raise DockerUpdateError(f"Der Compose-Dienst {service} ist nicht eindeutig.")
    start = matches[0]
    end = services_end
    for index in range(start + 1, services_end):
        active = _active_yaml_lines(lines[index])
        if not active:
            continue
        content = active[0]
        child_indent = len(content) - len(content.lstrip(" "))
        if child_indent <= 2:
            end = index
            break
    return start, end


def _direct_field_indices(
    lines: list[str],
    *,
    start: int,
    end: int,
    indent: int,
    key: str,
) -> list[int]:
    pattern = re.compile(rf"^{{}}{re.escape(key)}\s*:".format(" " * indent))
    return [
        index
        for index in range(start + 1, end)
        if (
            (active := _active_yaml_lines(lines[index]))
            and pattern.match(active[0])
            and len(active[0]) - len(active[0].lstrip(" ")) == indent
        )
    ]


def _set_service_scalar(
    lines: list[str],
    service: str,
    key: str,
    value: str,
    *,
    replace: bool,
) -> None:
    start, end = _service_span(lines, service)
    matches = _direct_field_indices(lines, start=start, end=end, indent=4, key=key)
    if len(matches) > 1:
        raise DockerUpdateError(f"{service}.{key} ist mehrfach vorhanden.")
    if matches:
        active = _active_yaml_lines(lines[matches[0]])[0]
        current = active.split(":", 1)[1].strip()
        if not replace and current.strip("'\"") != value.strip("'\""):
            raise DockerUpdateError(f"{service}.{key} widerspricht dem Zielvertrag.")
        if replace:
            lines[matches[0]] = f"    {key}: {value}"
        return
    lines.insert(start + 1, f"    {key}: {value}")


def _service_field_span(lines: list[str], service: str, field: str) -> tuple[int, int] | None:
    start, end = _service_span(lines, service)
    matches = _direct_field_indices(lines, start=start, end=end, indent=4, key=field)
    if len(matches) > 1:
        raise DockerUpdateError(f"{service}.{field} ist mehrfach vorhanden.")
    if not matches:
        return None
    field_start = matches[0]
    field_end = end
    for index in range(field_start + 1, end):
        active = _active_yaml_lines(lines[index])
        if not active:
            continue
        content = active[0]
        child_indent = len(content) - len(content.lstrip(" "))
        if child_indent <= 4:
            field_end = index
            break
    return field_start, field_end


def _ensure_service_sequence_entry(
    lines: list[str], service: str, field: str, value: str
) -> None:
    span = _service_field_span(lines, service, field)
    if span is None:
        _start, service_end = _service_span(lines, service)
        lines[service_end:service_end] = [f"    {field}:", f"      - {value}"]
        return
    field_start, field_end = span
    entries: list[str] = []
    for index in range(field_start + 1, field_end):
        active = _active_yaml_lines(lines[index])
        if not active:
            continue
        content = active[0]
        child_indent = len(content) - len(content.lstrip(" "))
        if child_indent != 6 or not content.strip().startswith("- "):
            raise DockerUpdateError(
                f"{service}.{field} verwendet keine sicher erweiterbare Kurzlistenform."
            )
        entries.append(content.strip()[2:].strip())
    if value not in entries:
        lines.insert(field_end, f"      - {value}")


def _ensure_service_assignment(
    lines: list[str],
    service: str,
    field: str,
    key: str,
    value: str,
) -> None:
    span = _service_field_span(lines, service, field)
    if span is None:
        _start, service_end = _service_span(lines, service)
        lines[service_end:service_end] = [f"    {field}:", f"      - {key}={value}"]
        return
    field_start, field_end = span
    style = ""
    matches: list[int] = []
    for index in range(field_start + 1, field_end):
        active = _active_yaml_lines(lines[index])
        if not active:
            continue
        content = active[0]
        child_indent = len(content) - len(content.lstrip(" "))
        stripped = content.strip()
        if child_indent != 6:
            raise DockerUpdateError(
                f"{service}.{field} verwendet eine verschachtelte, nicht sicher erweiterbare Form."
            )
        current_style = "sequence" if stripped.startswith("- ") else "mapping"
        if style and current_style != style:
            raise DockerUpdateError(f"{service}.{field} mischt YAML-Darstellungsformen.")
        style = current_style
        material = stripped[2:].strip() if style == "sequence" else stripped
        assignment_key = material.split("=", 1)[0] if style == "sequence" else material.split(":", 1)[0]
        if assignment_key.strip("'\"") == key:
            matches.append(index)
    if len(matches) > 1:
        raise DockerUpdateError(f"{service}.{field}.{key} ist mehrfach vorhanden.")
    if matches:
        lines[matches[0]] = (
            f"      - {key}={value}"
            if style == "sequence"
            else f'      {key}: "{value}"'
        )
        return
    lines.insert(
        field_end,
        f"      - {key}={value}" if style != "mapping" else f'      {key}: "{value}"',
    )


def _ensure_service_mapping(
    lines: list[str], service: str, field: str, body: tuple[str, ...]
) -> None:
    if _service_field_span(lines, service, field) is not None:
        return
    _start, service_end = _service_span(lines, service)
    lines[service_end:service_end] = [f"    {field}:", *body]


def _ensure_top_volume(lines: list[str], name: str) -> None:
    start, end = _mapping_span(lines, indent=0, key="volumes")
    matches = _direct_field_indices(lines, start=start, end=end, indent=2, key=name)
    if len(matches) > 1:
        raise DockerUpdateError(f"Das Top-Level-Volume {name} ist mehrfach vorhanden.")
    if not matches:
        lines.insert(end, f"  {name}:")


def _project_legacy_532b_compose(data: bytes, *, topology: str) -> bytes:
    lines = _yaml_lines_for_semantic_migration(data)
    _set_service_scalar(
        lines,
        SERVICE_NAME,
        "image",
        VARIABLE_IMAGE_EXPRESSION,
        replace=True,
    )
    _set_service_scalar(lines, SERVICE_NAME, "hostname", SERVICE_NAME, replace=False)
    _ensure_service_mapping(
        lines,
        SERVICE_NAME,
        "logging",
        (
            "      driver: json-file",
            "      options:",
            '        max-size: "10m"',
            '        max-file: "3"',
        ),
    )
    _ensure_service_assignment(
        lines,
        SERVICE_NAME,
        "labels",
        "com.centurylinklabs.watchtower.enable",
        "${E3DC_WATCHTOWER_ENABLE:-false}",
    )
    for value in (
        "e3dc_ml:/var/lib/e3dc-control/ml",
        "e3dc_forecast_evidence:/var/lib/e3dc-control/forecast-evidence",
        f"{ROLE_VOLUME_NAME}:{ROLE_VOLUME_TARGET}",
    ):
        _ensure_service_sequence_entry(lines, SERVICE_NAME, "volumes", value)
    _ensure_service_assignment(
        lines,
        SERVICE_NAME,
        "environment",
        "E3DC_CONTAINER_MODE",
        "1",
    )
    watch_size, watch_files = (
        ("10m", "3") if topology == "installer_bind_mounts" else ("5m", "2")
    )
    _ensure_service_mapping(
        lines,
        "watchtower",
        "logging",
        (
            "      driver: json-file",
            "      options:",
            f'        max-size: "{watch_size}"',
            f'        max-file: "{watch_files}"',
        ),
    )
    _ensure_service_sequence_entry(lines, "watchtower", "profiles", "auto-update")
    _ensure_service_assignment(
        lines,
        "watchtower",
        "environment",
        "WATCHTOWER_LABEL_ENABLE",
        "true",
    )
    for name in ("e3dc_ml", "e3dc_forecast_evidence", ROLE_VOLUME_NAME):
        _ensure_top_volume(lines, name)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _classify_compose_source(
    data: bytes,
    projection: dict[str, Any] | None = None,
) -> tuple[str, str]:
    normalised = _normalised_lf_bytes(data)
    legacy_hash = hashlib.sha256(normalised).hexdigest()
    topology = KNOWN_542_LEGACY_HASHES.get(legacy_hash)
    try:
        active = _active_yaml_lines(data.decode("utf-8"))
    except UnicodeDecodeError as exc:
        raise DockerUpdateError("Compose ist nicht gültig UTF-8-kodiert.") from exc
    current_topology = KNOWN_CURRENT_TEMPLATES.get(active)
    published_532b = legacy_hash in PUBLISHED_532B_NORMALISED_HASHES
    legacy_532b_topology = "repo_named_volumes" if published_532b else ""
    if projection is not None:
        projected_image = str(
            (((projection.get("services") or {}).get(SERVICE_NAME) or {}).get("image"))
            or ""
        )
        if _tag_version(projected_image) == LEGACY_NO_HEALTHCHECK_VERSION:
            semantic_topology = _legacy_532b_projection_topology(projection)
            if published_532b and semantic_topology != legacy_532b_topology:
                raise DockerUpdateError(
                    "Die veröffentlichte v5.3.2b-Datei widerspricht ihrer gebundenen Topologie."
                )
            legacy_532b_topology = semantic_topology
        elif published_532b:
            raise DockerUpdateError(
                "Der veröffentlichte v5.3.2b-Blob projiziert nicht sein gebundenes Altimage."
            )
    if topology is None and current_topology is None and not legacy_532b_topology:
        raise DockerUpdateError(
            "Automatisch migriert werden die semantisch sichere v5.3.2b-Compose-Datei, "
            "die unveränderten veröffentlichten 5.4.2–5.4.2d-Dateien und der "
            "aktuelle Pflichtvertrag. Prozess-, Hardware- oder schreibbare "
            "Fremdmount-Anpassungen müssen manuell geprüft werden."
        )
    found = sum(bool(item) for item in (topology, current_topology, legacy_532b_topology))
    if found != 1:
        raise DockerUpdateError("Der Compose-Stand ist strukturell mehrdeutig.")
    if legacy_532b_topology:
        return legacy_532b_topology, "legacy_532b"
    return topology or current_topology or "", "legacy_542" if topology is not None else "current"


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

        before_projection = _compose_projection(cli, cli.compose_file)
        topology, compose_state = _classify_compose_source(
            source["data"],
            before_projection,
        )
        if not topology:
            raise DockerUpdateError("Die Compose-Topologie konnte nicht gebunden werden.")
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
                "preimage": source,
                "env": env_snapshot,
                "projection": before_projection,
                "pre_projection": before_projection,
            }

        candidate_data = (
            _project_legacy_532b_compose(source["data"], topology=topology)
            if compose_state == "legacy_532b"
            else _project_current_compose(source["data"], topology=topology)
        )
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
            f"✓ Compose {('5.3.2b' if compose_state == 'legacy_532b' else '5.4.2')} "
            "wurde atomar auf den aktuellen Host-Vertrag migriert; "
            "Daten- und Logtopologie blieb unverändert.",
            flush=True,
        )
        if watchtower_stopped:
            print("✓ Watchtower bleibt nach der sicheren Migration bewusst gestoppt.", flush=True)
        return {
            "state": "migrated",
            "source_state": compose_state,
            "topology": topology,
            "compose": bound_source,
            "preimage": source,
            "env": env_snapshot,
            "projection": final_projection,
            "pre_projection": before_projection,
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
    runtime_projection: dict[str, Any] | None = None,
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
        runtime_projection or contract.get("projection") or {},
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


def _projection_has_role(projection: dict[str, Any]) -> bool:
    service = ((projection.get("services") or {}).get(SERVICE_NAME) or {})
    return bool(_volume_mapping(service, ROLE_VOLUME_TARGET))


def _capture_previous_runtime(
    cli: DockerCli,
    projection: dict[str, Any],
) -> dict[str, Any]:
    ids = _named_container_ids(cli, SERVICE_NAME)
    if len(ids) > 1:
        raise DockerUpdateError("Das Altcontainer-Inventar ist mehrdeutig.")
    if not ids:
        return {"present": False, "running": False}
    container_id = ids[0]
    before = _e3dc_stop_authority(cli, container_id)
    _validate_e3dc_container_binding(
        cli,
        projection,
        require_role=_projection_has_role(projection),
    )
    image_ref = _require_official_image(
        str((before.get("Config") or {}).get("Image") or "")
    )
    legacy_version = (
        LEGACY_NO_HEALTHCHECK_VERSION
        if _tag_version(image_ref) == LEGACY_NO_HEALTHCHECK_VERSION
        else ""
    )
    contract = _image_contract(
        cli,
        image_ref,
        legacy_no_healthcheck_version=legacy_version,
    )
    if str(before.get("Image") or "") != contract["image_id"]:
        raise DockerUpdateError("Die lokale Altimage-ID widerspricht dem laufenden Container.")
    running = (before.get("State") or {}).get("Running") is True
    if running and _runtime_version(cli, container_id) != contract["version"]:
        raise DockerUpdateError("Altimage und laufende Altversion widersprechen sich.")
    after = _e3dc_stop_authority(cli, container_id)
    stable_keys = lambda info: (
        str(info.get("Id") or ""),
        str(info.get("Image") or ""),
        bool((info.get("State") or {}).get("Running")),
        int(info.get("RestartCount") or 0),
        str((info.get("State") or {}).get("StartedAt") or ""),
    )
    if stable_keys(before) != stable_keys(after):
        raise DockerUpdateError("Der Altcontainer driftete während seiner Rückfallbindung.")
    return {
        "present": True,
        "running": running,
        "container_id": container_id,
        "contract": contract,
    }


def _replace_active_compose_data(
    cli: DockerCli,
    *,
    expected_data: bytes,
    replacement_data: bytes,
    metadata_source: dict[str, Any],
) -> dict[str, Any]:
    directory_fd = os.open(
        cli.compose_dir,
        os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC | getattr(os, "O_NOFOLLOW", 0),
    )
    candidate_name = ""
    try:
        current = _secure_snapshot(
            directory_fd,
            COMPOSE_FILENAME,
            required=True,
            max_size=MAX_COMPOSE_BYTES,
        )
        if current is None or current["data"] != expected_data:
            raise DockerUpdateError(
                "Compose driftete; der gebundene Stand darf beim Rückfall nicht überschrieben werden."
            )
        candidate_name, _candidate_path = _write_candidate(
            directory_fd,
            cli.compose_dir,
            replacement_data,
            metadata_source,
        )
        replaced = _replace_candidate(
            directory_fd,
            candidate_name,
            replacement_data,
            metadata_source,
        )
        candidate_name = ""
        return replaced
    finally:
        if candidate_name:
            try:
                os.unlink(candidate_name, dir_fd=directory_fd)
            except OSError:
                pass
        os.close(directory_fd)


def _restore_compose_contract_preimage(
    cli: DockerCli,
    contract: dict[str, Any],
) -> dict[str, Any]:
    current = contract.get("compose") or {}
    preimage = contract.get("preimage") or current
    if current.get("data") == preimage.get("data"):
        return current
    return _replace_active_compose_data(
        cli,
        expected_data=current["data"],
        replacement_data=preimage["data"],
        metadata_source=preimage,
    )


def _compose_with_image(data: bytes, image: str) -> bytes:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", str(image or "")):
        raise DockerUpdateError("Das Rückfallimage besitzt keine unveränderliche Image-ID.")
    lines = _yaml_lines_for_semantic_migration(data)
    _set_service_scalar(lines, SERVICE_NAME, "image", image, replace=True)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _restore_old_image_reference(cli: DockerCli, contract: dict[str, str]) -> None:
    image_ref = _require_official_image(str(contract.get("image") or ""))
    image_id = str(contract.get("image_id") or "")
    _require_success(
        cli.run(["image", "tag", image_id, image_ref]),
        "Wiederherstellung der Altimage-Referenz",
    )
    rebound = _image_contract(
        cli,
        image_ref,
        legacy_no_healthcheck_version=(
            LEGACY_NO_HEALTHCHECK_VERSION
            if contract.get("legacy_without_healthcheck") == "1"
            else ""
        ),
    )
    if rebound != contract:
        raise DockerUpdateError("Die wiederhergestellte Altimage-Referenz ist nicht identisch.")


def _rollback_previous_runtime(
    cli: DockerCli,
    compose_contract: dict[str, Any],
    previous: dict[str, Any],
    *,
    wait_timeout: int,
) -> dict[str, Any]:
    restored = _restore_compose_contract_preimage(cli, compose_contract)
    if not previous.get("present") or not previous.get("running"):
        return {
            "restored": True,
            "previous_running": False,
            "version": "",
        }

    old_contract = dict(previous.get("contract") or {})
    old_image_id = str(old_contract.get("image_id") or "")
    pinned_data = _compose_with_image(restored["data"], old_image_id)
    pinned = _replace_active_compose_data(
        cli,
        expected_data=restored["data"],
        replacement_data=pinned_data,
        metadata_source=restored,
    )
    pinned_contract = dict(old_contract)
    pinned_contract["image"] = old_image_id
    start_error: BaseException | None = None
    verification: dict[str, Any] | None = None
    try:
        up_result = cli.compose(
            [
                "up",
                "-d",
                "--pull",
                "never",
                "--force-recreate",
                "--wait",
                "--wait-timeout",
                str(wait_timeout),
                SERVICE_NAME,
            ],
            timeout=wait_timeout + START_TIMEOUT_GRACE_S,
            capture=True,
        )
        _require_success(up_result, "Start des gebundenen Altcontainers")
        verification = _verify_candidate(cli, pinned_contract)
        _restore_old_image_reference(cli, old_contract)
    except BaseException as exc:
        start_error = exc
    finally:
        try:
            _replace_active_compose_data(
                cli,
                expected_data=pinned["data"],
                replacement_data=restored["data"],
                metadata_source=restored,
            )
        except BaseException as restore_exc:
            raise CandidateStopError(
                "Der Altcontainer-Rückfall konnte die originale Compose-Datei nicht "
                f"wiederherstellen: {restore_exc}"
            ) from restore_exc
    if start_error is not None:
        raise CandidateStopError(
            f"Der gebundene Altcontainer konnte nicht wieder gestartet werden: {start_error}"
        ) from start_error
    second = _verify_candidate(cli, pinned_contract)
    return {
        "restored": True,
        "previous_running": True,
        "version": old_contract.get("version") or "",
        "verification": second or verification or {},
    }


def _restore_prestart_state(
    cli: DockerCli,
    compose_contract: dict[str, Any],
    previous: dict[str, Any],
) -> None:
    _restore_compose_contract_preimage(cli, compose_contract)
    _require_previous_runtime_unchanged(
        cli,
        previous,
        compose_contract.get("pre_projection") or {},
    )


def _require_previous_runtime_unchanged(
    cli: DockerCli,
    previous: dict[str, Any],
    projection: dict[str, Any],
) -> None:
    ids = _named_container_ids(cli, SERVICE_NAME)
    if not previous.get("present"):
        if ids:
            raise CandidateStopError("Vor dem Kandidatenstart entstand ein fremder Altcontainer.")
        return
    if ids != (str(previous.get("container_id") or ""),):
        raise CandidateStopError(
            "Der Altcontainerzustand driftete vor dem Kandidatenstart und konnte nicht "
            "unverändert bestätigt werden."
        )
    info = _e3dc_stop_authority(cli, ids[0])
    _validate_e3dc_container_binding(
        cli,
        projection,
        require_role=_projection_has_role(projection),
    )
    if (
        str(info.get("Image") or "")
        != str((previous.get("contract") or {}).get("image_id") or "")
        or ((info.get("State") or {}).get("Running") is True)
        != bool(previous.get("running"))
    ):
        raise CandidateStopError("Altimage-ID oder Laufzustand driftete vor dem Kandidatenstart.")


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
        if not running and ids == previous_ids:
            stable += 1
            if stable >= 2:
                return contract_drift
        else:
            stable = 0
        previous_ids = ids
        time.sleep(1)
    detail = ", ".join(last_running) or stop_error or "unbekannter Zustand"
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
    previous: dict[str, Any] | None = None
    contract: dict[str, str] | None = None
    candidate_started = False
    try:
        pre_projection = compose_contract.get("pre_projection") or {}
        previous = _capture_previous_runtime(cli, pre_projection)
        pre_up_role_required = _projection_has_role(pre_projection)
        _require_prepared_contract(
            cli,
            compose_contract,
            require_role=pre_up_role_required,
            runtime_projection=pre_projection,
        )

        selected_before = _require_official_image(_selected_image(cli))
        if args.image_tag:
            expected = f"ghcr.io/a9xxx/install-e3dc-control:{args.image_tag}"
            if selected_before != expected:
                raise DockerUpdateError(
                    f"Compose projiziert {selected_before} statt des angeforderten {expected}."
                )
        if args.recreate_current:
            print(
                f"→ Binde das bereits lokale Image für den reinen Neustart: {selected_before} …",
                flush=True,
            )
        else:
            storage = _docker_storage_preflight(cli)
            print(
                "✓ Docker-Speicher vor dem Pull: "
                f"{storage['available_bytes'] // (1024 * 1024)} MiB frei.",
                flush=True,
            )
            print(f"→ Ziehe explizit {selected_before} …", flush=True)
            _require_success(
                cli.compose(
                    ["pull", SERVICE_NAME],
                    timeout=max(args.wait_timeout, DEFAULT_PULL_TIMEOUT_S),
                    capture=True,
                ),
                "Docker-Image-Pull",
            )
        _require_prepared_contract(
            cli,
            compose_contract,
            require_role=pre_up_role_required,
            runtime_projection=pre_projection,
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
        _require_previous_runtime_unchanged(cli, previous, pre_projection)
        _require_prepared_contract(
            cli,
            compose_contract,
            require_role=pre_up_role_required,
            runtime_projection=pre_projection,
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
            capture=True,
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
            if contract is None:
                raise CandidateStopError(
                    f"{exc}; der Kandidatenstart begann ohne gebundenen Imagevertrag."
                ) from exc
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
            try:
                rollback = _rollback_previous_runtime(
                    cli,
                    compose_contract,
                    previous or {"present": False, "running": False},
                    wait_timeout=args.wait_timeout,
                )
            except BaseException as rollback_exc:
                raise CandidateStopError(
                    f"{exc}; der Kandidat ist gestoppt, aber der automatische "
                    f"Altcontainer-Rückfall blieb unbestätigt: {rollback_exc}"
                ) from exc
            version = str(rollback.get("version") or "unbekannt")
            drift_note = (
                " Der gestoppte Kandidat zeigte zuvor Vertragsdrift."
                if stopped_with_contract_drift
                else ""
            )
            raise DockerUpdateError(
                f"{exc}; [ROLLBACK_OK] die vorherige Compose-Datei und "
                f"Altversion {version} laufen wieder verifiziert.{drift_note}"
            ) from exc
        try:
            if previous is None:
                _restore_compose_contract_preimage(cli, compose_contract)
            else:
                _restore_prestart_state(cli, compose_contract, previous)
        except BaseException as restore_exc:
            raise CandidateStopError(
                f"{exc}; der Kandidat wurde noch nicht gestartet, aber der unveränderte "
                f"Altzustand konnte nicht bestätigt werden: {restore_exc}"
            ) from exc
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="E3DC-Control-Compose-Image sicher ziehen, starten und verifizieren"
    )
    parser.add_argument("--compose-dir", default=".", help="Verzeichnis der docker-compose.yml")
    parser.add_argument(
        "--sudo",
        action="store_true",
        help=(
            "Bestätigt den dokumentierten Root-Aufruf `sudo python3 ... --sudo`; "
            "verschachteltes sudo wird nicht verwendet"
        ),
    )
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
        help=(
            "Compose-Health-Wartezeit in Sekunden; der Image-Pull erhält "
            f"mindestens {DEFAULT_PULL_TIMEOUT_S} Sekunden"
        ),
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
    except subprocess.TimeoutExpired as exc:
        if not getattr(exc, "e3dc_process_group_stopped", False):
            print(f"Docker-Update fehlgeschlagen: {exc}", file=sys.stderr)
            return 1
        detail = exc.stderr or exc.output or ""
        if isinstance(detail, bytes):
            detail = detail.decode("utf-8", errors="replace")
        detail = str(detail).strip()[-TIMEOUT_OUTPUT_MAX_CHARS:]
        detail_suffix = f"\nLetzte Docker-Ausgabe:\n{detail}" if detail else ""
        command_value = exc.cmd or ()
        command = (
            [command_value]
            if isinstance(command_value, str)
            else [str(item) for item in command_value]
        )
        long_wait_relevant = "compose" in command and bool(
            {"pull", "up"} & set(command)
        )
        retry_hint = (
            " Wenn der Timeout beim Pull oder Kandidatenstart auftrat, kann "
            "--wait-timeout bis 1800 erhöht werden."
            if long_wait_relevant
            else (
                " Dieser Docker-Kurzcheck verwendet ein festes Zeitlimit; "
                "--wait-timeout verändert ihn nicht."
            )
        )
        print(
            "Docker-Update fehlgeschlagen: Docker/Compose hat das Zeitlimit von "
            f"{exc.timeout:g} Sekunden überschritten. Die nur diesem Aufruf "
            "zugeordnete Prozessgruppe wurde beendet; die daran gebundene "
            "Docker-/Compose-Instanz läuft nicht im Hintergrund weiter."
            + retry_hint
            + detail_suffix,
            file=sys.stderr,
        )
        return 1
    except (DockerUpdateError, OSError, subprocess.SubprocessError) as exc:
        print(f"Docker-Update fehlgeschlagen: {exc}", file=sys.stderr)
        return 1
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
