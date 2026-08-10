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
DEFAULT_STATE_DIR = Path(
    os.environ.get("E3DC_EMERGENCY_STATE_DIR", "/var/lib/e3dc-control/emergency-quiesce")
)
LATCH_FILENAME = "active-incident.json"
HISTORY_DIRNAME = "history"
STORAGE_SERVICE = "e3dc-storage-manager.service"

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
    if "ActiveState" not in values or "MainPID" not in values:
        raise RuntimeError("Systemd-Status ist unvollstaendig.")
    try:
        main_pid = int(values.get("MainPID") or 0)
        control_pid = int(values.get("ControlPID") or 0)
        exec_status = int(values.get("ExecMainStatus") or 0)
    except ValueError as exc:
        raise RuntimeError("Systemd-Status enthält ungültige Prozesswerte.") from exc
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
        rscp_audit = audit_emergency_rscp_tags()
        private_dir = _ensure_private_dir(Path(state_dir))
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
        writer_stopped = not _service_is_running(after) and original_pid_gone

        release_confirmed = not was_running or bool(
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
            if _service_is_running(verify):
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
        private_dir = _ensure_private_dir(Path(state_dir))
        latch_path = private_dir / LATCH_FILENAME
        payload = _read_latch(latch_path)
        if str(payload.get("incident_id") or "") != incident_id:
            raise RuntimeError("Incident-ID stimmt nicht mit dem aktiven Latch ueberein.")
        service = _systemd_show(runner, STORAGE_SERVICE)
        if _service_is_running(service):
            raise RuntimeError("Storage-Writer ist noch aktiv; Latch bleibt gesetzt.")
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
