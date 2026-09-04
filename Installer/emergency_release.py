#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Einmaliges Notfall-Stillsetzen des Speicherschreibers.

Dieses Programm sendet bewusst keinen RSCP-, Wallbox-, Wärmepumpen-, Phasen-
oder CP-Befehl. Es fordert systemd auf, den Speicherschreiber anzuhalten. Dessen
SIGTERM-Pfad beendet ausschließlich den Prozessbesitz und lässt den zuletzt
bestätigten flüchtigen POWER_SETTINGS-Rahmen unverändert. Wenn Prozessende und
Besitzfreigabe nicht bestätigt werden können, bleibt der Vorfall verriegelt und
das Programm endet ungleich null. Es gibt weder einen rohen Hardware-Fallback
noch einen Wiederholungsversuch.
"""

from __future__ import annotations

import argparse
import ast
from dataclasses import dataclass
import datetime as _datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import time
from typing import Any, Callable, Dict, Iterable, Mapping, Optional, Sequence


SCRIPT_DIR = Path(__file__).resolve().parent
RSCP_CLIENT_PATH = SCRIPT_DIR / "rscp_client.py"
PERSISTENT_EMERGENCY_STATE_DIR = Path("/var/lib/e3dc-control/emergency-quiesce")
LATCH_FILENAME = "active-incident.json"
PERSISTENT_EMERGENCY_LATCH_PATH = PERSISTENT_EMERGENCY_STATE_DIR / LATCH_FILENAME
SYSTEMD_ADMIN_UNIT_DIR = Path("/etc/systemd/system")
SYSTEMD_GENERATOR_DIR = Path("/etc/systemd/system-generators")
PERSISTENT_EMERGENCY_DROPIN_NAME = "05-e3dc-emergency-quiesce.conf"
DEFAULT_STATE_DIR = PERSISTENT_EMERGENCY_STATE_DIR
HISTORY_DIRNAME = "history"
STORAGE_SERVICE = "e3dc-storage-manager.service"
PERSISTENT_EMERGENCY_DROPIN_PATH = (
    SYSTEMD_ADMIN_UNIT_DIR
    / f"{STORAGE_SERVICE}.d"
    / PERSISTENT_EMERGENCY_DROPIN_NAME
)
PERSISTENT_EMERGENCY_GENERATOR_PATH = (
    SYSTEMD_GENERATOR_DIR / "e3dc-emergency-quiesce-generator"
)

# Das Notfall-Stillsetzen wirkt ausschließlich auf Prozesse. Die aktuelle
# Allowlist ist deshalb bewusst leer. Ein künftiges Read-only-Tag muss explizit
# ergänzt werden und bleibt gesperrt, sofern es nicht in der exakten lokalen
# rscp_client.py definiert ist.
EMERGENCY_RSCP_TAG_ALLOWLIST = frozenset()
_RSCP_REFERENCE_RE = re.compile(r"\bRscpTag\.([A-Z][A-Z0-9_]*)\b")
_FORBIDDEN_RSCP_EXACT = frozenset(
    {
        "EMS_REQ_SET_POWER",
        "EMS_REQ_SET_POWER_MODE",
        "EMS_REQ_SET_POWER_VALUE",
    }
)
_FORBIDDEN_RSCP_FRAGMENTS = ("SET_MAX", "SET_POWER_SETTINGS")
_FORBIDDEN_RSCP_PREFIXES = ("WB_",)

EXIT_OK = 0
EXIT_USAGE_OR_AUDIT = 2
EXIT_LATCHED_INCOMPLETE = 3
EXIT_STOP_FAILED = 4
EXIT_CONFIRMATION_FAILED = 5
EXIT_RESET_FAILED = 6
EXIT_INTERNAL = 7

_INCIDENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,127}$")
_RUNNING_STATES = frozenset({"active", "activating", "reloading", "deactivating"})

log = logging.getLogger("EmergencyQuiesce")

CommandRunner = Callable[[Sequence[str], float], subprocess.CompletedProcess[str]]
ProcessAlive = Callable[[int], bool]


@dataclass(frozen=True)
class QuiesceOutcome:
    exit_code: int
    status: str
    incident_id: str
    changed: bool


def _utc_now() -> str:
    return _datetime.datetime.now(_datetime.timezone.utc).isoformat(timespec="seconds")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _rscp_tag_definitions(rscp_client_path: Path) -> frozenset[str]:
    source = rscp_client_path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(rscp_client_path))
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == "RscpTag":
            names = set()
            for item in node.body:
                if isinstance(item, (ast.Assign, ast.AnnAssign)):
                    targets: Iterable[ast.expr]
                    if isinstance(item, ast.Assign):
                        targets = item.targets
                    else:
                        targets = (item.target,)
                    for target in targets:
                        if isinstance(target, ast.Name):
                            names.add(target.id)
            return frozenset(names)
    raise RuntimeError("Die lokale rscp_client.py enthaelt keine RscpTag-Klasse.")


def audit_emergency_rscp_tags(
    *,
    source_text: Optional[str] = None,
    source_path: Path = Path(__file__),
    rscp_client_path: Path = RSCP_CLIENT_PATH,
    allowlist: Iterable[str] = EMERGENCY_RSCP_TAG_ALLOWLIST,
) -> Dict[str, Any]:
    """Bindet jede Notfall-Tagreferenz an den exakten lokalen RSCP-Client.

    Direkte Aktor-Tags und möglicherweise persistente Einstellungen sind auch
    dann verboten, wenn ein Aufrufer versucht, sie in eine Allowlist aufzunehmen.
    """

    if source_text is None:
        source_text = source_path.read_text(encoding="utf-8")
    referenced = frozenset(_RSCP_REFERENCE_RE.findall(source_text))
    allowed = frozenset(str(item) for item in allowlist)
    defined = _rscp_tag_definitions(rscp_client_path)

    forbidden = sorted(
        tag
        for tag in referenced
        if tag in _FORBIDDEN_RSCP_EXACT
        or tag.startswith(_FORBIDDEN_RSCP_PREFIXES)
        or any(fragment in tag for fragment in _FORBIDDEN_RSCP_FRAGMENTS)
    )
    undefined = sorted(referenced - defined)
    unapproved = sorted(referenced - allowed)
    if forbidden or undefined or unapproved:
        details = []
        if forbidden:
            details.append("verbotene Aktor-/Persistenz-Tags")
        if undefined:
            details.append("nicht lokal definierte Tags")
        if unapproved:
            details.append("nicht freigegebene Tags")
        raise RuntimeError("Emergency-RSCP-Audit fehlgeschlagen: " + ", ".join(details))

    return {
        "status": "passed",
        "referenced_tags": sorted(referenced),
        "allowlist": sorted(allowed),
        "rscp_client_sha256": _sha256(rscp_client_path),
    }


def _validate_incident_id(incident_id: str) -> str:
    value = str(incident_id or "").strip()
    if not _INCIDENT_RE.fullmatch(value):
        raise ValueError("Incident-ID hat ein ungültiges Format.")
    return value


def _ensure_private_dir(path: Path) -> Path:
    path = path.expanduser()
    if path.is_symlink():
        raise RuntimeError("Emergency-Statusverzeichnis darf kein Symlink sein.")
    if not path.exists():
        path.mkdir(parents=True, mode=0o700, exist_ok=False)
        os.chmod(path, 0o700)
    if not path.is_dir():
        raise RuntimeError("Emergency-Statuspfad ist kein Verzeichnis.")
    info = path.stat()
    if info.st_mode & 0o077:
        raise RuntimeError("Emergency-Statusverzeichnis ist nicht privat (erwartet 0700).")
    geteuid = getattr(os, "geteuid", None)
    if callable(geteuid) and info.st_uid != geteuid():
        raise RuntimeError("Emergency-Statusverzeichnis gehoert nicht dem aufrufenden Benutzer.")
    return path


def _fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def emergency_latch_present(path: Path = PERSISTENT_EMERGENCY_LATCH_PATH) -> bool:
    """Behandelt jeden benannten Latchknoten absichtlich als aktives Startveto."""

    try:
        os.lstat(Path(path))
    except FileNotFoundError:
        return False
    except OSError as exc:
        # EACCES, EIO, ELOOP und vergleichbare Fehler dürfen den Writer nicht
        # freigeben. Der konkrete Fehler bleibt für die Diagnose im Journal.
        log.error("Emergency-Latchpfad ist nicht sicher lesbar; Veto bleibt aktiv: %s", exc)
        return True
    return True


def _bound_emergency_state_dir(state_dir: Path, *, require_root: bool) -> Path:
    """Trennt den festen Produktpfad von expliziten Testverzeichnissen."""

    candidate = Path(state_dir)
    if not candidate.is_absolute() or any(
        character in str(candidate) for character in "\r\n\x00"
    ):
        raise RuntimeError("Emergency-State-Verzeichnis ist kein sicherer absoluter Pfad.")
    if require_root and candidate != PERSISTENT_EMERGENCY_STATE_DIR:
        raise RuntimeError(
            "Privilegierter Emergency-Quiesce darf nur den festen Produkt-Latch verwenden."
        )
    return candidate


def _bound_emergency_latch_path(latch_path: Path, *, require_root: bool) -> Path:
    """Bindet produktive Startsperren an genau den festen Incident-Latch."""

    candidate = Path(latch_path)
    if not candidate.is_absolute() or any(
        character in str(candidate) for character in "\r\n\x00"
    ):
        raise RuntimeError("Emergency-Latchpfad ist kein sicherer absoluter Pfad.")
    if require_root and candidate != PERSISTENT_EMERGENCY_LATCH_PATH:
        raise RuntimeError(
            "Privilegierter Emergency-Startschutz darf nur den festen Produkt-Latch verwenden."
        )
    return candidate


def render_persistent_emergency_start_veto(
    latch_path: Path = PERSISTENT_EMERGENCY_LATCH_PATH,
) -> bytes:
    """Rendert den versionsunabhängigen systemd-Startschutz des Writers."""

    target = Path(latch_path)
    if not target.is_absolute() or any(character in str(target) for character in "\r\n\x00"):
        raise ValueError("Emergency-Latchpfad ist kein sicherer absoluter Pfad.")
    return (
        "# E3DC-Control: versionsunabhängiger Emergency-Quiesce-Startschutz\n"
        "[Unit]\n"
        f"ConditionPathExists=!{target}\n"
        f"ConditionPathIsSymbolicLink=!{target}\n"
    ).encode("utf-8")


def render_persistent_emergency_generator(
    latch_path: Path = PERSISTENT_EMERGENCY_LATCH_PATH,
) -> bytes:
    """Rendert den rollbackfesten Generator für den flüchtigen Unit-Drop-in."""

    target = Path(latch_path)
    if not target.is_absolute() or any(character in str(target) for character in "\r\n\x00'"):
        raise ValueError("Emergency-Latchpfad ist für den Generator ungültig.")
    return (
        "#!/bin/sh\n"
        "set -eu\n"
        "[ \"$#\" -ge 1 ] || exit 0\n"
        "umask 022\n"
        # Der Early-Generatorpfad schlägt auch restaurierte /etc-Drop-ins.
        # Beim dokumentierten Ein-Argument-Testaufruf bleibt $1 der Fallback.
        "output_dir=${2:-$1}\n"
        "unit_dir=$output_dir/e3dc-storage-manager.service.d\n"
        "/bin/mkdir -p -- \"$unit_dir\"\n"
        "/usr/bin/printf '%s\\n' "
        "'# Generated by e3dc-emergency-quiesce-generator' '[Unit]' "
        f"'ConditionPathExists=!{target}' "
        f"'ConditionPathIsSymbolicLink=!{target}' "
        f"> \"$unit_dir/{PERSISTENT_EMERGENCY_DROPIN_NAME}\"\n"
    ).encode("utf-8")


def ensure_persistent_emergency_start_veto(
    *,
    systemd_root: Path = SYSTEMD_ADMIN_UNIT_DIR,
    generator_root: Path = SYSTEMD_GENERATOR_DIR,
    latch_path: Path = PERSISTENT_EMERGENCY_LATCH_PATH,
    require_root: bool = True,
) -> Dict[str, Any]:
    """Projiziert einen root-eigenen Drop-in außerhalb des Releasebaums.

    Der Drop-in bleibt auch dann wirksam, wenn ein Updatefehler die eigentliche
    Storage-Unit auf einen älteren, noch nicht gegateten Stand zurücksetzt.
    """

    latch_path = _bound_emergency_latch_path(
        latch_path,
        require_root=require_root,
    )
    effective_uid = int(os.geteuid()) if callable(getattr(os, "geteuid", None)) else 0
    effective_gid = int(os.getegid()) if callable(getattr(os, "getegid", None)) else 0
    if require_root and effective_uid != 0:
        raise PermissionError("Persistenter Emergency-Startschutz benötigt root.")
    expected_uid = 0 if require_root else effective_uid
    expected_gid = 0 if require_root else effective_gid

    root = Path(systemd_root)
    if not root.is_absolute():
        raise ValueError("Systemd-Adminwurzel ist nicht absolut.")
    root_info = root.lstat()
    if (
        not stat.S_ISDIR(root_info.st_mode)
        or stat.S_ISLNK(root_info.st_mode)
        or root_info.st_uid != expected_uid
        or root_info.st_gid != expected_gid
        or root_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("Systemd-Adminwurzel ist nicht eindeutig administrativ gebunden.")

    directory = root / f"{STORAGE_SERVICE}.d"
    if os.path.lexists(directory):
        directory_info = directory.lstat()
        if (
            not stat.S_ISDIR(directory_info.st_mode)
            or stat.S_ISLNK(directory_info.st_mode)
            or directory_info.st_uid != expected_uid
            or directory_info.st_gid != expected_gid
            or directory_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Emergency-Drop-in-Verzeichnis ist nicht sicher gebunden.")
    else:
        directory.mkdir(mode=0o755)
        os.chown(directory, expected_uid, expected_gid)
        os.chmod(directory, 0o755)
        _fsync_directory(root)

    destination = directory / PERSISTENT_EMERGENCY_DROPIN_NAME
    if os.path.lexists(destination):
        existing = destination.lstat()
        if (
            not stat.S_ISREG(existing.st_mode)
            or stat.S_ISLNK(existing.st_mode)
            or existing.st_nlink != 1
            or existing.st_uid != expected_uid
            or existing.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Vorhandener Emergency-Drop-in ist nicht sicher gebunden.")

    payload = render_persistent_emergency_start_veto(latch_path)
    current = destination.read_bytes() if destination.is_file() else b""
    content_changed = current != payload
    if content_changed:
        temporary = directory / f".{PERSISTENT_EMERGENCY_DROPIN_NAME}.{os.getpid()}.{time.time_ns()}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o644)
        try:
            os.fchmod(descriptor, 0o644)
            os.fchown(descriptor, expected_uid, expected_gid)
            offset = 0
            while offset < len(payload):
                offset += os.write(descriptor, payload[offset:])
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            os.replace(temporary, destination)
            _fsync_directory(directory)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    before_metadata = destination.lstat()
    metadata_changed = bool(
        before_metadata.st_uid != expected_uid
        or before_metadata.st_gid != expected_gid
        or stat.S_IMODE(before_metadata.st_mode) != 0o644
    )
    if metadata_changed:
        os.chown(destination, expected_uid, expected_gid, follow_symlinks=False)
        os.chmod(destination, 0o644, follow_symlinks=False)
        _fsync_directory(directory)

    rebound = destination.lstat()
    if (
        not stat.S_ISREG(rebound.st_mode)
        or rebound.st_nlink != 1
        or rebound.st_uid != expected_uid
        or rebound.st_gid != expected_gid
        or stat.S_IMODE(rebound.st_mode) != 0o644
        or destination.read_bytes() != payload
    ):
        raise RuntimeError("Persistenter Emergency-Drop-in blieb nach Projektion abweichend.")
    generator_directory = Path(generator_root)
    if not generator_directory.is_absolute():
        raise ValueError("Systemd-Generatorwurzel ist nicht absolut.")
    generator_parent = generator_directory.parent
    generator_parent_info = generator_parent.lstat()
    if (
        not stat.S_ISDIR(generator_parent_info.st_mode)
        or stat.S_ISLNK(generator_parent_info.st_mode)
        or generator_parent_info.st_uid != expected_uid
        or generator_parent_info.st_gid != expected_gid
        or generator_parent_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("Systemd-Generator-Elternpfad ist nicht sicher gebunden.")
    if os.path.lexists(generator_directory):
        generator_directory_info = generator_directory.lstat()
        if (
            not stat.S_ISDIR(generator_directory_info.st_mode)
            or stat.S_ISLNK(generator_directory_info.st_mode)
            or generator_directory_info.st_uid != expected_uid
            or generator_directory_info.st_gid != expected_gid
            or generator_directory_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Systemd-Generatorverzeichnis ist nicht sicher gebunden.")
    else:
        generator_directory.mkdir(mode=0o755)
        os.chown(generator_directory, expected_uid, expected_gid)
        os.chmod(generator_directory, 0o755)
        _fsync_directory(generator_parent)

    generator = generator_directory / PERSISTENT_EMERGENCY_GENERATOR_PATH.name
    if os.path.lexists(generator):
        generator_info = generator.lstat()
        if (
            not stat.S_ISREG(generator_info.st_mode)
            or stat.S_ISLNK(generator_info.st_mode)
            or generator_info.st_nlink != 1
            or generator_info.st_uid != expected_uid
            or generator_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError("Vorhandener Emergency-Generator ist nicht sicher gebunden.")
    generator_payload = render_persistent_emergency_generator(latch_path)
    generator_current = generator.read_bytes() if generator.is_file() else b""
    generator_content_changed = generator_current != generator_payload
    if generator_content_changed:
        generator_temporary = generator_directory / (
            f".{generator.name}.{os.getpid()}.{time.time_ns()}.tmp"
        )
        generator_flags = (
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        )
        generator_descriptor = os.open(generator_temporary, generator_flags, 0o755)
        try:
            os.fchmod(generator_descriptor, 0o755)
            os.fchown(generator_descriptor, expected_uid, expected_gid)
            offset = 0
            while offset < len(generator_payload):
                offset += os.write(generator_descriptor, generator_payload[offset:])
            os.fsync(generator_descriptor)
        finally:
            os.close(generator_descriptor)
        try:
            os.replace(generator_temporary, generator)
            _fsync_directory(generator_directory)
        finally:
            try:
                generator_temporary.unlink()
            except FileNotFoundError:
                pass
    generator_before_metadata = generator.lstat()
    generator_metadata_changed = bool(
        generator_before_metadata.st_uid != expected_uid
        or generator_before_metadata.st_gid != expected_gid
        or stat.S_IMODE(generator_before_metadata.st_mode) != 0o755
    )
    if generator_metadata_changed:
        os.chown(generator, expected_uid, expected_gid, follow_symlinks=False)
        os.chmod(generator, 0o755, follow_symlinks=False)
        _fsync_directory(generator_directory)
    generator_rebound = generator.lstat()
    if (
        not stat.S_ISREG(generator_rebound.st_mode)
        or generator_rebound.st_nlink != 1
        or generator_rebound.st_uid != expected_uid
        or generator_rebound.st_gid != expected_gid
        or stat.S_IMODE(generator_rebound.st_mode) != 0o755
        or generator.read_bytes() != generator_payload
    ):
        raise RuntimeError("Persistenter Emergency-Generator blieb nach Projektion abweichend.")

    return {
        "path": str(destination),
        "generator_path": str(generator),
        "changed": bool(
            content_changed
            or metadata_changed
            or generator_content_changed
            or generator_metadata_changed
        ),
        "latch_present": emergency_latch_present(latch_path),
        "condition": f"ConditionPathExists=!{latch_path}",
    }


def _json_bytes(payload: Mapping[str, Any]) -> bytes:
    return (json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2) + "\n").encode("utf-8")


def _create_latch(path: Path, payload: Mapping[str, Any]) -> None:
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        data = _json_bytes(payload)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
    finally:
        os.close(descriptor)
    _fsync_directory(path.parent)


def _replace_latch(path: Path, payload: Mapping[str, Any]) -> None:
    temp = path.parent / f".{path.name}.{os.getpid()}.{time.time_ns()}.tmp"
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(temp, flags, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        data = _json_bytes(payload)
        with os.fdopen(descriptor, "wb", closefd=False) as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp, path)
        os.chmod(path, 0o600)
        _fsync_directory(path.parent)
    finally:
        os.close(descriptor)
        try:
            temp.unlink()
        except FileNotFoundError:
            pass


def _read_latch(path: Path) -> Dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_mode & 0o077:
            raise RuntimeError("Emergency-Latch ist kein privates regulaeres Einzel-Link-File.")
        with os.fdopen(descriptor, "r", encoding="utf-8", closefd=False) as handle:
            payload = json.load(handle)
    finally:
        os.close(descriptor)
    if not isinstance(payload, dict):
        raise RuntimeError("Emergency-Latch ist ungültig.")
    return payload


def _default_runner(command: Sequence[str], timeout: float) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(command),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _systemd_show(runner: CommandRunner, service: str) -> Dict[str, Any]:
    properties = (
        "ActiveState",
        "SubState",
        "MainPID",
        "ControlPID",
        "ExecMainCode",
        "ExecMainStatus",
        "Result",
    )
    command = ["systemctl", "show", service, "--no-pager"]
    command.extend(f"--property={item}" for item in properties)
    result = runner(command, 10.0)
    if result.returncode != 0:
        raise RuntimeError("Storage-Writer-Status konnte nicht bestätigt werden.")
    values: Dict[str, str] = {}
    for line in str(result.stdout or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key] = value.strip()
    required = {"ActiveState", "SubState", "MainPID", "ControlPID"}
    if not required.issubset(values) or any(not values[key] for key in required):
        raise RuntimeError("Systemd-Status ist unvollständig.")
    try:
        main_pid = int(values["MainPID"])
        control_pid = int(values["ControlPID"])
        exec_status = int(values.get("ExecMainStatus") or 0)
    except ValueError as exc:
        raise RuntimeError("Systemd-Status enthält ungültige Prozesswerte.") from exc
    if main_pid < 0 or control_pid < 0:
        raise RuntimeError("Systemd-Status enthält negative Prozesswerte.")
    return {
        "active_state": values.get("ActiveState", "unknown"),
        "sub_state": values.get("SubState", "unknown"),
        "main_pid": main_pid,
        "control_pid": control_pid,
        "exec_main_code": values.get("ExecMainCode", ""),
        "exec_main_status": exec_status,
        "result": values.get("Result", ""),
    }


def _service_is_running(snapshot: Mapping[str, Any]) -> bool:
    return bool(
        str(snapshot.get("active_state") or "") in _RUNNING_STATES
        or int(snapshot.get("main_pid") or 0) > 0
        or int(snapshot.get("control_pid") or 0) > 0
    )


def _service_is_proven_inactive(snapshot: Mapping[str, Any]) -> bool:
    """Nur der vollständige systemd-Stillstandsvertrag gilt als Beweis."""

    return bool(
        str(snapshot.get("active_state") or "").lower() == "inactive"
        and str(snapshot.get("sub_state") or "").lower() in {"dead", "exited"}
        and int(snapshot.get("main_pid") or 0) == 0
        and int(snapshot.get("control_pid") or 0) == 0
    )


def _actor_statuses() -> Dict[str, Dict[str, Any]]:
    untouched = {"status": "not_touched", "hardware_write": False}
    return {
        "storage_writer": {"status": "pending", "hardware_write": False},
        "battery": {
            "status": "no_direct_write",
            "hardware_write": False,
            "release_path": "storage_writer_sigterm_only",
        },
        "wallbox": dict(untouched),
        "heatpump": dict(untouched),
        "phase_switch": dict(untouched),
        "control_pilot": dict(untouched),
        "emergency_reserve": dict(untouched),
        "device_limits": dict(untouched),
        "permanent_settings": dict(untouched),
    }


def _initial_latch(incident_id: str, rscp_audit: Mapping[str, Any]) -> Dict[str, Any]:
    return {
        "schema": "e3dc_emergency_quiesce_v1",
        "incident_id": incident_id,
        "one_shot": True,
        "automatic_retry_allowed": False,
        "status": "quiescing",
        "started_at": _utc_now(),
        "completed_at": None,
        "service": STORAGE_SERVICE,
        "owner_lease": {
            "holder": "emergency_quiesce",
            "status": "pending_process_termination",
        },
        "normal_sigterm_release": {"status": "pending"},
        "rscp_audit": dict(rscp_audit),
        "actors": _actor_statuses(),
        "hardware_commands_sent": 0,
    }


def _existing_outcome(path: Path, incident_id: str) -> QuiesceOutcome:
    payload = _read_latch(path)
    existing_id = str(payload.get("incident_id") or "")
    status_value = str(payload.get("status") or "incomplete")
    if existing_id == incident_id and status_value == "complete":
        return QuiesceOutcome(EXIT_OK, "already_complete", incident_id, False)
    return QuiesceOutcome(EXIT_LATCHED_INCOMPLETE, "latched", incident_id, False)


def quiesce(
    incident_id: str,
    *,
    state_dir: Path = DEFAULT_STATE_DIR,
    runner: CommandRunner = _default_runner,
    process_alive: ProcessAlive = _pid_alive,
    stop_timeout_s: float = 45.0,
    require_root: bool = True,
) -> QuiesceOutcome:
    """Stoppt und prüft den Speicherschreiber pro Vorfall exakt einmal."""

    incident_id = _validate_incident_id(incident_id)
    if require_root and callable(getattr(os, "geteuid", None)) and os.geteuid() != 0:
        return QuiesceOutcome(EXIT_USAGE_OR_AUDIT, "root_required", incident_id, False)

    try:
        bound_state_dir = _bound_emergency_state_dir(
            state_dir,
            require_root=require_root,
        )
        rscp_audit = audit_emergency_rscp_tags()
        private_dir = _ensure_private_dir(bound_state_dir)
    except Exception as exc:
        log.error("Emergency-Quiesce-Vorpruefung fehlgeschlagen: %s", exc)
        return QuiesceOutcome(EXIT_USAGE_OR_AUDIT, "preflight_failed", incident_id, False)

    latch_path = private_dir / LATCH_FILENAME
    payload = _initial_latch(incident_id, rscp_audit)
    try:
        _create_latch(latch_path, payload)
    except FileExistsError:
        try:
            return _existing_outcome(latch_path, incident_id)
        except Exception as exc:
            log.error("Vorhandener Emergency-Latch ist nicht vertrauenswuerdig: %s", exc)
            return QuiesceOutcome(EXIT_LATCHED_INCOMPLETE, "untrusted_latch", incident_id, False)
    except Exception as exc:
        log.error("Emergency-Latch konnte nicht atomar beansprucht werden: %s", exc)
        return QuiesceOutcome(EXIT_INTERNAL, "latch_claim_failed", incident_id, False)

    exit_code = EXIT_INTERNAL
    status_value = "incomplete"
    failure_reason = "internal_error"
    try:
        before = _systemd_show(runner, STORAGE_SERVICE)
        payload["writer_before"] = before
        was_running = _service_is_running(before)
        was_proven_inactive = _service_is_proven_inactive(before)
        if not was_running and not was_proven_inactive:
            raise RuntimeError(
                "Storage-Writer-Zustand ist weder aktiv noch nachweislich inaktiv."
            )
        original_pid = int(before.get("main_pid") or 0)
        stop_returncode: Optional[int] = None

        if was_running:
            try:
                stopped = runner(["systemctl", "stop", STORAGE_SERVICE], float(stop_timeout_s))
                stop_returncode = int(stopped.returncode)
            except subprocess.TimeoutExpired:
                stop_returncode = 124
            except Exception:
                stop_returncode = 125
            payload["systemd_stop"] = {
                "requested": True,
                "returncode": stop_returncode,
                "signal_path": "systemd_sigterm",
                "raw_signal_fallback": False,
            }
        else:
            payload["systemd_stop"] = {
                "requested": False,
                "returncode": 0,
                "reason": "writer_already_stopped",
                "raw_signal_fallback": False,
            }

        after = _systemd_show(runner, STORAGE_SERVICE)
        payload["writer_after_stop"] = after
        original_pid_gone = original_pid <= 0 or not process_alive(original_pid)
        writer_stopped = _service_is_proven_inactive(after) and original_pid_gone

        release_confirmed = was_proven_inactive or bool(
            stop_returncode == 0
            # systemctl show exposes siginfo's CLD_EXITED as numeric "1";
            # einige Test-/Fassadenimplementierungen stellen denselben Zustand
            # als "exited" dar. Beides bedeutet ein normales Prozessende, anders
            # als 2/"killed".
            and str(after.get("exec_main_code") or "") in {"1", "exited"}
            and int(after.get("exec_main_status", -1)) == 0
        )
        payload["normal_sigterm_release"] = {
            "status": "confirmed" if release_confirmed else "unconfirmed",
            "evidence": "already_stopped" if not was_running else "systemd_clean_exit",
            "raw_rscp_fallback": False,
        }

        if stop_returncode not in (None, 0):
            failure_reason = "systemd_stop_failed"
            exit_code = EXIT_STOP_FAILED
        elif not writer_stopped:
            failure_reason = "writer_process_or_service_still_active"
            exit_code = EXIT_CONFIRMATION_FAILED
        elif not release_confirmed:
            failure_reason = "normal_sigterm_release_unconfirmed"
            exit_code = EXIT_CONFIRMATION_FAILED
        else:
            # Der exklusive Vorfall-Latch ist die Notfall-Owner-Lease. Er wird
            # erst nach dem Prozessende bestätigt; danach wird systemd erneut
            # gelesen, um das Übernahmerennen zu schließen. Der Watchdog beachtet
            # diese Datei und startet oder ruft den Schreiber nicht erneut auf.
            payload["owner_lease"] = {
                "holder": "emergency_quiesce",
                "status": "confirming",
                "writer_process_gone": True,
            }
            verify = _systemd_show(runner, STORAGE_SERVICE)
            payload["writer_after_lease"] = verify
            if not _service_is_proven_inactive(verify):
                failure_reason = "writer_reappeared_during_lease_confirmation"
                exit_code = EXIT_CONFIRMATION_FAILED
            else:
                payload["owner_lease"] = {
                    "holder": "emergency_quiesce",
                    "status": "confirmed",
                    "writer_process_gone": True,
                    "watchdog_restart_blocked_by_latch": True,
                }
                payload["actors"]["storage_writer"] = {
                    "status": "quiesced",
                    "hardware_write": False,
                    "normal_sigterm_release": "confirmed",
                }
                payload["status"] = "complete"
                payload["completed_at"] = _utc_now()
                payload["hardware_commands_sent"] = 0
                _replace_latch(latch_path, payload)
                log.critical("Emergency-Quiesce für Incident %s vollständig bestätigt.", incident_id)
                return QuiesceOutcome(EXIT_OK, "complete", incident_id, True)
    except Exception as exc:
        failure_reason = "verification_exception"
        exit_code = EXIT_CONFIRMATION_FAILED
        log.error("Emergency-Quiesce konnte nicht bestätigt werden: %s", exc)

    payload["status"] = status_value
    payload["completed_at"] = _utc_now()
    payload["failure_reason"] = failure_reason
    payload["hardware_commands_sent"] = 0
    payload["automatic_retry_allowed"] = False
    payload["actors"]["storage_writer"] = {
        "status": "unconfirmed",
        "hardware_write": False,
        "automatic_retry": False,
    }
    payload["owner_lease"] = {
        "holder": "emergency_quiesce",
        "status": "unconfirmed",
    }
    try:
        _replace_latch(latch_path, payload)
    except Exception as exc:
        log.error("Unvollstaendiger Emergency-Status konnte nicht persistiert werden: %s", exc)
        exit_code = EXIT_INTERNAL
    log.critical(
        "Emergency-Quiesce für Incident %s unvollständig (%s); keine Wiederholung, kein Hardware-Fallback.",
        incident_id,
        failure_reason,
    )
    return QuiesceOutcome(exit_code, "incomplete", incident_id, True)


def reset_latch(
    incident_id: str,
    *,
    state_dir: Path = DEFAULT_STATE_DIR,
    runner: CommandRunner = _default_runner,
    require_root: bool = True,
) -> QuiesceOutcome:
    """Controlled reset: exact incident ID and a stopped writer are mandatory."""

    incident_id = _validate_incident_id(incident_id)
    if require_root and callable(getattr(os, "geteuid", None)) and os.geteuid() != 0:
        return QuiesceOutcome(EXIT_RESET_FAILED, "root_required", incident_id, False)
    try:
        private_dir = _ensure_private_dir(
            _bound_emergency_state_dir(state_dir, require_root=require_root)
        )
        latch_path = private_dir / LATCH_FILENAME
        payload = _read_latch(latch_path)
        if str(payload.get("incident_id") or "") != incident_id:
            raise RuntimeError("Incident-ID stimmt nicht mit dem aktiven Latch ueberein.")
        service = _systemd_show(runner, STORAGE_SERVICE)
        if not _service_is_proven_inactive(service):
            raise RuntimeError(
                "Storage-Writer ist nicht nachweislich inaktiv; Latch bleibt gesetzt."
            )
        history_dir = _ensure_private_dir(private_dir / HISTORY_DIRNAME)
        archive = history_dir / f"{incident_id}-{time.time_ns()}.json"
        os.replace(latch_path, archive)
        os.chmod(archive, 0o600)
        _fsync_directory(history_dir)
        _fsync_directory(private_dir)
    except Exception as exc:
        log.error("Kontrolliertes Rücksetzen des Emergency-Latch abgelehnt: %s", exc)
        return QuiesceOutcome(EXIT_RESET_FAILED, "reset_refused", incident_id, False)
    log.warning("Emergency-Latch für Incident %s kontrolliert archiviert.", incident_id)
    return QuiesceOutcome(EXIT_OK, "reset", incident_id, True)


def _configure_logging() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - EMERGENCY-QUIESCE - %(levelname)s - %(message)s",
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="E3DC storage-writer emergency quiesce")
    parser.add_argument("--incident-id", required=True)
    parser.add_argument("--state-dir", default=str(DEFAULT_STATE_DIR))
    parser.add_argument("--stop-timeout", type=float, default=45.0)
    parser.add_argument("--reset-latch", action="store_true")
    args = parser.parse_args(argv)
    _configure_logging()
    try:
        if args.reset_latch:
            outcome = reset_latch(args.incident_id, state_dir=Path(args.state_dir))
        else:
            outcome = quiesce(
                args.incident_id,
                state_dir=Path(args.state_dir),
                stop_timeout_s=max(1.0, min(90.0, float(args.stop_timeout))),
            )
    except (ValueError, RuntimeError) as exc:
        log.error("Emergency-Quiesce abgelehnt: %s", exc)
        return EXIT_USAGE_OR_AUDIT
    log.info(
        "Emergency-Quiesce Ergebnis: status=%s incident=%s changed=%s",
        outcome.status,
        outcome.incident_id,
        outcome.changed,
    )
    return outcome.exit_code


if __name__ == "__main__":
    sys.exit(main())
