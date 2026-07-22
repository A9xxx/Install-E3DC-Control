#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Backend-Grundgerüst für den künftigen autonomen WebUI-Installer.

Das Modul ist noch nicht aus PHP verknüpft. Standardmäßig führt es nur
Read-only-Diagnoseaufträge aus. Schreibaktionen bleiben gesperrt, bis WebUI-
Ablauf, sudoers-Wrapper und Testplan bewusst freigegeben werden.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

try:
    from .backup_retention import WEB_INSTALLER_BACKUP_KEEP_COUNT, prune_backup_dir
    from .backup_integrity import (
        WEB_SNAPSHOT_KIND,
        default_backup_root,
        finalize_backup,
        secure_backup_tree,
    )
    from .config_secret_permissions import (
        apply_config_backup_dir_permissions,
        apply_config_secret_permissions,
    )
    from .service_catalog import READ_ACTIONS, SERVICE_ACTIONS, get_module, iter_modules
except ImportError:  # pragma: no cover - direct script execution fallback
    from backup_retention import WEB_INSTALLER_BACKUP_KEEP_COUNT, prune_backup_dir
    from backup_integrity import (
        WEB_SNAPSHOT_KIND,
        default_backup_root,
        finalize_backup,
        secure_backup_tree,
    )
    from config_secret_permissions import (
        apply_config_backup_dir_permissions,
        apply_config_secret_permissions,
    )
    from service_catalog import READ_ACTIONS, SERVICE_ACTIONS, get_module, iter_modules


RAMDISK_DIR = Path("/var/www/html/ramdisk")
DATA_DIR = Path("/var/www/html/data")
LOG_DIR = Path("/var/www/html/logs")
TMP_DIR = Path("/var/www/html/tmp")
JOB_FILE = RAMDISK_DIR / "web_install_jobs.json"
STATUS_FILE = RAMDISK_DIR / "web_install_status.json"
LOCK_FILE = RAMDISK_DIR / "web_install.lock"
LOG_FILE = LOG_DIR / "web_installer.log"
CONFIG_FILE = DATA_DIR / "e3dc_v4.json"
SESSION_FILES = (
    TMP_DIR / "car_charge_session.json",
    TMP_DIR / "car_charge_session_wb2.json",
)

PYTHON_IMPORT_PACKAGES = {
    "mqtt": (("paho.mqtt.client", "python3-paho-mqtt"),),
}

CONFIG_FIELD_LABELS = {
    "luxtronik": "WP-/Verbrauchslogging aktiv",
    "wp_type": "Wärmepumpen-Typ",
    "wp_type=Luxtronik": "Wärmepumpen-Typ = Luxtronik",
    "wp_type=IDM": "Wärmepumpen-Typ = IDM",
    "wp_type=Stiebel": "Wärmepumpen-Typ = Stiebel",
    "wp_type=Dimplex": "Wärmepumpen-Typ = Dimplex",
    "wp_type=Luxtronik, IDM, Stiebel oder Dimplex": "Wärmepumpen-Typ = Luxtronik, IDM, Stiebel oder Dimplex",
    "shelly_sg_ip oder shelly_pause_ip": "SG-Ready Shelly IP",
    "climate_enable": "Klimaanlage aktiv",
    "climate_meter_ip": "Klimaanlagen-Zähler IP",
    "climate_control_enable": "Klimaanlage Regel-Vorbereitung aktiv",
    "climate_control_provider": "Klimaanlagen-Regelprovider",
    "climate_control_mode": "Klimaanlagen-Regelmodus",
}


def config_field_label(item: str) -> str:
    text = str(item or "").strip()
    if text in CONFIG_FIELD_LABELS:
        return CONFIG_FIELD_LABELS[text]
    if text.startswith("wp_type="):
        return "Wärmepumpen-Typ = " + text.split("=", 1)[1]
    return text


def config_field_labels(items: list[str] | tuple[str, ...]) -> list[str]:
    return [config_field_label(item) for item in items if str(item or "").strip()]

WRITE_ACTIONS_ENABLED = os.environ.get("E3DC_WEB_INSTALLER_ENABLE_WRITES") == "1"
ALLOWED_JOB_TYPES = set(READ_ACTIONS) | set(SERVICE_ACTIONS) | {
    "catalog",
    "installer_status",
    "job_status",
    "write_readiness",
    "write_permission_plan",
    "backup_plan",
    "run_diagnosis",
    "dry_run",
    "install_module_dry_run",
    "permissions_check",
    "repair_permissions_dry_run",
    "update_check",
    "start_service",
    "stop_service",
    "restart_service",
    "enable_service",
    "disable_service",
    "repair_permissions",
    "run_update",
    "install_module",
    "remove_module",
    "install_missing_packages",
}

ACTION_ALIASES = {
    "start_service": "start",
    "stop_service": "stop",
    "restart_service": "restart",
    "enable_service": "enable",
    "disable_service": "disable",
}

INSTALLER_DIR = Path(__file__).resolve().parent
INSTALL_ROOT = INSTALLER_DIR.parent
WEB_ROOT = Path("/var/www/html")

READ_ONLY_ACTIONS = {
    "catalog",
    "installer_status",
    "job_status",
    "write_readiness",
    "write_permission_plan",
    "backup_plan",
    "status",
    "diagnose",
    "run_diagnosis",
    "validate_config",
    "dry_run",
    "install_module_dry_run",
    "permissions_check",
    "repair_permissions_dry_run",
    "update_check",
}


def configure_utf8_stdio() -> None:
    """Hält die JSON-Ausgabe auch unter einer alten latin-1-Locale gültig."""

    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (AttributeError, OSError, ValueError):
            pass

PASSIVE_STATUS_ACTIONS = {
    "catalog",
    "installer_status",
    "job_status",
}

WRITE_ACTION_NAMES = sorted(
    set(SERVICE_ACTIONS)
    | {
        "start_service",
        "stop_service",
        "restart_service",
        "enable_service",
        "disable_service",
        "repair_permissions",
        "run_update",
        "install_module",
        "remove_module",
        "install_missing_packages",
    }
)

SERVICE_WRAPPER = INSTALLER_DIR / "service_wrapper.sh"
INSTALLER_WRAPPER = INSTALLER_DIR / "installer_wrapper.sh"
WRAPPER_RELATIVE_PATHS = (
    "Installer/service_wrapper.sh",
    "Installer/installer_wrapper.sh",
)
SUDOERS_FILE = Path("/etc/sudoers.d/020_e3dc_services")
SUDOERS_DIR = Path("/etc/sudoers.d")
MAX_BACKUP_FILE_BYTES = 10 * 1024 * 1024


def _git_head_wrapper_bytes(repo_root: Path) -> tuple[str, dict[str, bytes]]:
    """Liest die freigegebenen Wrapperbytes direkt aus dem lokalen Git-HEAD."""
    root = Path(repo_root)
    try:
        head_result = subprocess.run(
            ["git", "-c", f"safe.directory={root}", "-C", str(root), "rev-parse", "--verify", "HEAD^{commit}"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        raise RuntimeError(f"Lokaler Git-HEAD konnte nicht gebunden werden: {exc}") from exc
    head = head_result.stdout.strip().lower()
    if head_result.returncode != 0 or len(head) != 40 or any(ch not in "0123456789abcdef" for ch in head):
        error = head_result.stderr.strip() or "ungültige HEAD-Antwort"
        raise RuntimeError(f"Lokaler Git-HEAD konnte nicht gebunden werden: {error}")

    canonical: dict[str, bytes] = {}
    for relative_path in WRAPPER_RELATIVE_PATHS:
        try:
            blob_result = subprocess.run(
                ["git", "-c", f"safe.directory={root}", "-C", str(root), "cat-file", "blob", f"{head}:{relative_path}"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=10,
                check=False,
            )
        except Exception as exc:
            raise RuntimeError(f"HEAD-Blob fehlt für {relative_path}: {exc}") from exc
        if blob_result.returncode != 0:
            error = blob_result.stderr.decode("utf-8", errors="replace").strip() or "Blob nicht lesbar"
            raise RuntimeError(f"HEAD-Blob fehlt für {relative_path}: {error}")
        payload = bytes(blob_result.stdout)
        if not payload.startswith(b"#!/bin/bash\n") or b"\r" in payload:
            raise RuntimeError(f"HEAD-Blob ist kein LF-kodierter Bash-Wrapper: {relative_path}")
        canonical[relative_path] = payload
    return head, canonical


def _classify_wrapper(path: Path, canonical: bytes) -> dict[str, Any]:
    expected_sha256 = hashlib.sha256(canonical).hexdigest()
    item: dict[str, Any] = {
        "path": str(path),
        "expected_sha256": expected_sha256,
        "status": "unknown",
        "repairable": False,
        "needs_repair": True,
    }
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        item.update({"status": "missing", "repairable": True})
        return item
    except Exception as exc:
        item.update({"status": "read_error", "error": str(exc)})
        return item

    item.update({
        "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "nlink": int(metadata.st_nlink),
    })
    if stat.S_ISLNK(metadata.st_mode):
        item["status"] = "symlink"
        return item
    if not stat.S_ISREG(metadata.st_mode):
        item["status"] = "not_regular"
        return item
    if metadata.st_nlink != 1:
        item["status"] = "hardlink"
        return item
    try:
        actual = path.read_bytes()
    except Exception as exc:
        item.update({"status": "read_error", "error": str(exc)})
        return item

    actual_sha256 = hashlib.sha256(actual).hexdigest()
    item["actual_sha256"] = actual_sha256
    if actual == canonical:
        item.update({"status": "ok", "repairable": True, "needs_repair": False})
    elif b"\r\n" in actual and actual.replace(b"\r\n", b"\n") == canonical:
        item.update({"status": "crlf_only", "repairable": True})
    else:
        item["status"] = "content_drift"
    return item


def _collect_wrapper_integrity(repo_root: Path) -> dict[str, Any]:
    root = Path(repo_root)
    try:
        head, canonical = _git_head_wrapper_bytes(root)
    except Exception as exc:
        return {
            "success": False,
            "repo_root": str(root),
            "head": None,
            "items": [],
            "hard_blockers": [{"status": "head_error", "error": str(exc)}],
            "canonical": {},
        }

    items: list[dict[str, Any]] = []
    for relative_path in WRAPPER_RELATIVE_PATHS:
        item = _classify_wrapper(root / relative_path, canonical[relative_path])
        item["relative_path"] = relative_path
        items.append(item)
    hard_blockers = [item for item in items if not item.get("repairable")]
    return {
        "success": not hard_blockers,
        "repo_root": str(root),
        "head": head,
        "items": items,
        "hard_blockers": hard_blockers,
        "canonical": canonical,
    }


def wrapper_integrity_preview(repo_root: Path | str | None = None) -> dict[str, Any]:
    """Prüft Wrapper gegen HEAD, ohne Dateien oder Rechte zu verändern."""
    state = _collect_wrapper_integrity(Path(repo_root) if repo_root is not None else INSTALL_ROOT)
    return {
        "success": state["success"],
        "repo_root": state["repo_root"],
        "head": state["head"],
        "items": state["items"],
        "hard_blockers": state["hard_blockers"],
        "repair_needed": any(item.get("needs_repair") for item in state["items"]),
    }


def _wrapper_owner_ids(user: str | None, group: str | None) -> tuple[int, int]:
    uid = -1
    gid = -1
    if user is not None:
        import pwd

        uid = int(pwd.getpwnam(user).pw_uid)
    if group is not None:
        import grp

        gid = int(grp.getgrnam(group).gr_gid)
    return uid, gid


def _atomic_write_wrapper(path: Path, payload: bytes, user: str | None, group: str | None) -> None:
    parent_meta = os.lstat(path.parent)
    if stat.S_ISLNK(parent_meta.st_mode) or not stat.S_ISDIR(parent_meta.st_mode):
        raise RuntimeError(f"Wrapper-Elternpfad ist kein echtes Verzeichnis: {path.parent}")

    uid, gid = _wrapper_owner_ids(user, group)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.repair-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        if uid != -1 or gid != -1:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o755)
        os.fsync(fd)
        os.replace(tmp_path, path)
        directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(fd)
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def _set_verified_wrapper_permissions(path: Path, canonical: bytes, user: str | None, group: str | None) -> None:
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(str(path), flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise RuntimeError(f"Wrapper ist beim Rechte-Endgate nicht mehr regulär/nlink=1: {path}")
        chunks: list[bytes] = []
        while True:
            chunk = os.read(fd, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        if b"".join(chunks) != canonical:
            raise RuntimeError(f"Wrapperbytes änderten sich beim Rechte-Endgate: {path}")
        uid, gid = _wrapper_owner_ids(user, group)
        if uid != -1 or gid != -1:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o755)
        os.fsync(fd)
    finally:
        os.close(fd)


def repair_wrapper_integrity(
    repo_root: Path | str | None = None,
    user: str | None = None,
    group: str | None = None,
) -> dict[str, Any]:
    """Repariert nur fehlende oder reine CRLF-Wrapper aus dem gebundenen HEAD."""
    root = Path(repo_root) if repo_root is not None else INSTALL_ROOT
    state = _collect_wrapper_integrity(root)
    public_state = {
        "success": state["success"],
        "repo_root": state["repo_root"],
        "head": state["head"],
        "items": state["items"],
        "hard_blockers": state["hard_blockers"],
        "repair_needed": any(item.get("needs_repair") for item in state["items"]),
    }
    if not state["success"]:
        public_state["message"] = "Wrapper-Reparatur fail-closed abgebrochen: unsicherer Wrapperzustand."
        public_state["steps"] = []
        return public_state

    # Zweites reines Lesegate unmittelbar vor der ersten Mutation schützt vor
    # einem zwischenzeitlichen HEAD- oder Dateigenerationswechsel.
    rebound = _collect_wrapper_integrity(root)
    if (
        not rebound["success"]
        or rebound["head"] != state["head"]
        or [(item.get("relative_path"), item.get("status"), item.get("actual_sha256")) for item in rebound["items"]]
        != [(item.get("relative_path"), item.get("status"), item.get("actual_sha256")) for item in state["items"]]
    ):
        public_state.update({
            "success": False,
            "message": "Wrapper-Reparatur abgebrochen: HEAD oder Wrapperzustand hat sich während des Gates geändert.",
            "steps": [],
        })
        return public_state

    steps: list[dict[str, Any]] = []
    try:
        for item in state["items"]:
            relative_path = str(item["relative_path"])
            path = root / relative_path
            canonical = state["canonical"][relative_path]
            current = _classify_wrapper(path, canonical)
            if current.get("status") != item.get("status") or current.get("actual_sha256") != item.get("actual_sha256"):
                raise RuntimeError(f"Wrapperzustand änderte sich vor dem Schreiben: {path}")

            if item["status"] in {"missing", "crlf_only"}:
                _atomic_write_wrapper(path, canonical, user, group)
                steps.append({
                    "step": "restore_wrapper_from_head",
                    "ok": True,
                    "path": str(path),
                    "source": f"{state['head']}:{relative_path}",
                    "reason": item["status"],
                    "sha256": hashlib.sha256(canonical).hexdigest(),
                })
            else:
                _set_verified_wrapper_permissions(path, canonical, user, group)
                steps.append({
                    "step": "verify_wrapper_from_head",
                    "ok": True,
                    "path": str(path),
                    "source": f"{state['head']}:{relative_path}",
                    "sha256": hashlib.sha256(canonical).hexdigest(),
                })

        final_state = _collect_wrapper_integrity(root)
        final_ok = (
            final_state["success"]
            and final_state["head"] == state["head"]
            and all(item.get("status") == "ok" for item in final_state["items"])
        )
        if not final_ok:
            raise RuntimeError("Wrapper-Endgate stimmt nicht vollständig mit dem gebundenen Git-HEAD überein")
        return {
            "success": True,
            "message": "Wrapperintegrität ist gegen den lokalen Git-HEAD gebunden.",
            "repo_root": str(root),
            "head": state["head"],
            "items": final_state["items"],
            "hard_blockers": [],
            "repair_needed": False,
            "steps": steps,
        }
    except Exception as exc:
        steps.append({"step": "wrapper_integrity", "ok": False, "error": str(exc)})
        return {
            "success": False,
            "message": f"Wrapper-Reparatur abgebrochen: {exc}",
            "repo_root": str(root),
            "head": state["head"],
            "items": state["items"],
            "hard_blockers": [],
            "repair_needed": True,
            "steps": steps,
        }


def validate_wrapper_backup_coverage(wrapper_preview: dict[str, Any], backup_snapshot: dict[str, Any]) -> dict[str, Any]:
    """Beweist vor Mutation das Snapshot-Preimage jedes vorhandenen Wrappers."""
    copied_by_source = {
        str(item.get("path") or ""): item
        for item in backup_snapshot.get("copied", [])
        if str(item.get("path") or "")
    }
    checks: list[dict[str, Any]] = []
    for wrapper in wrapper_preview.get("items", []):
        if wrapper.get("status") == "missing":
            checks.append({
                "path": wrapper.get("path"),
                "ok": True,
                "skipped": True,
                "reason": "kein Preimage vorhanden",
            })
            continue

        source = str(wrapper.get("path") or "")
        copied = copied_by_source.get(source)
        check: dict[str, Any] = {"path": source, "ok": False}
        if not copied:
            check["error"] = "Wrapper fehlt in backup_snapshot.copied"
            checks.append(check)
            continue
        backup_path = Path(str(copied.get("backup") or ""))
        try:
            metadata = os.lstat(backup_path)
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise RuntimeError("Backup ist nicht regulär/nlink=1")
            backup_sha256 = hashlib.sha256(backup_path.read_bytes()).hexdigest()
            expected_sha256 = str(wrapper.get("actual_sha256") or "")
            if not expected_sha256 or backup_sha256 != expected_sha256:
                raise RuntimeError("Backup-Hash stimmt nicht mit dem gebundenen Wrapper-Preimage überein")
            check.update({"ok": True, "backup": str(backup_path), "sha256": backup_sha256})
        except Exception as exc:
            check.update({"backup": str(backup_path), "error": str(exc)})
        checks.append(check)

    return {
        "success": bool(wrapper_preview.get("success")) and all(item.get("ok") for item in checks),
        "checks": checks,
    }


def backup_relative_path(path: Path) -> str:
    """Map absolute Linux/Windows paths into a safe relative backup path."""
    raw = path.as_posix()
    if len(raw) >= 2 and raw[1] == ":":
        raw = f"{raw[0]}_drive{raw[2:]}"
    return raw.lstrip("/")


def desired_sudoers_lines() -> list[str]:
    return [
        f"www-data ALL=(root) NOPASSWD: {SERVICE_WRAPPER}",
        f"www-data ALL=(root) NOPASSWD: {INSTALLER_WRAPPER}",
    ]


def desired_sudoers_content() -> str:
    lines = [
        "# E3DC-Control WebUI wrapper permissions",
        "# Managed by the Web-Installer. Do not add direct systemctl commands here.",
        *desired_sudoers_lines(),
        "",
    ]
    return "\n".join(lines)


def sudoers_file_preview() -> dict[str, Any]:
    desired_lines = desired_sudoers_lines()
    try:
        existing_text = SUDOERS_FILE.read_text(encoding="utf-8", errors="replace")
    except FileNotFoundError:
        existing_text = ""
    except Exception as exc:
        return {
            "ok": False,
            "error": str(exc),
            "file": str(SUDOERS_FILE),
            "desired_lines": desired_lines,
            "target_content": desired_sudoers_content(),
        }

    existing_active = [
        line.strip()
        for line in existing_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    return {
        "ok": True,
        "file": str(SUDOERS_FILE),
        "exists": SUDOERS_FILE.exists(),
        "existing_active_lines": existing_active,
        "desired_lines": desired_lines,
        "missing_lines": [line for line in desired_lines if line not in existing_text],
        "removed_lines": [line for line in existing_active if line not in desired_lines],
        "target_content": desired_sudoers_content(),
        "would_replace_file": existing_text != desired_sudoers_content(),
    }


def sudoers_file_findings() -> dict[str, Any]:
    """Scan sudoers fragments for direct www-data commands outside the wrappers."""
    files: list[dict[str, Any]] = []
    direct_web_lines: list[dict[str, Any]] = []
    direct_systemctl_lines: list[dict[str, Any]] = []
    legacy_lines: list[dict[str, Any]] = []
    try:
        candidates = sorted(SUDOERS_DIR.glob("*"))
    except Exception:
        candidates = []

    for path in candidates:
        if not path.is_file():
            continue
        if ".bak-webinstaller-" in path.name or ".tmp-webinstaller-" in path.name:
            files.append({
                "file": str(path),
                "readable": True,
                "ignored": True,
                "reason": "Web-Installer Backup/Temp-Datei, keine aktive sudoers-Freigabe",
                "direct_web_lines": [],
                "direct_systemctl_lines": [],
                "legacy_lines": [],
            })
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception as exc:
            files.append({"file": str(path), "readable": False, "error": str(exc)})
            continue

        file_direct: list[dict[str, Any]] = []
        file_systemctl: list[dict[str, Any]] = []
        file_legacy: list[dict[str, Any]] = []
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            item = {"file": str(path), "line_no": line_no, "line": line}
            is_wrapper = str(SERVICE_WRAPPER) in line or str(INSTALLER_WRAPPER) in line
            if "www-data" in line and "NOPASSWD:" in line and not is_wrapper:
                file_direct.append(item)
                direct_web_lines.append(item)
            if "systemctl" in line and not is_wrapper:
                file_systemctl.append(item)
                direct_systemctl_lines.append(item)
            if "e3dc.service" in line:
                file_legacy.append(item)
                legacy_lines.append(item)

        files.append({
            "file": str(path),
            "readable": True,
            "direct_web_lines": file_direct,
            "direct_systemctl_lines": file_systemctl,
            "legacy_lines": file_legacy,
        })

    return {
        "files": files,
        "direct_web_lines": direct_web_lines,
        "direct_systemctl_lines": direct_systemctl_lines,
        "legacy_lines": legacy_lines,
        "affected_files": sorted({item["file"] for item in direct_web_lines + direct_systemctl_lines + legacy_lines}),
    }


def owner_group(path: Path) -> dict[str, Any]:
    try:
        import grp
        import pwd

        stat = path.stat()
        return {
            "owner": pwd.getpwuid(stat.st_uid).pw_name,
            "group": grp.getgrgid(stat.st_gid).gr_name,
            "mode": oct(stat.st_mode & 0o7777),
        }
    except Exception:
        try:
            stat = path.stat()
            return {"owner": str(stat.st_uid), "group": str(stat.st_gid), "mode": oct(stat.st_mode & 0o7777)}
        except Exception as exc:
            return {"owner": None, "group": None, "mode": None, "error": str(exc)}


def utc_now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def ensure_dirs() -> None:
    for path in (RAMDISK_DIR, LOG_DIR):
        try:
            path.mkdir(parents=True, exist_ok=True)
        except PermissionError:
            if path == RAMDISK_DIR:
                raise


def log(message: str) -> None:
    ensure_dirs()
    line = f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} - {message}\n"
    try:
        with LOG_FILE.open("a", encoding="utf-8") as handle:
            handle.write(line)
    except PermissionError:
        # Passive WebUI-Diagnose darf nicht scheitern, nur weil das Logfile noch
        # nicht für www-data schreibbar ist. Der Rechte-Check meldet das separat.
        pass


def write_status(payload: dict[str, Any]) -> None:
    ensure_dirs()
    payload = {
        "updated_at": utc_now(),
        **payload,
    }
    tmp = STATUS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, STATUS_FILE)
    try:
        os.chmod(STATUS_FILE, 0o664)
    except OSError:
        pass


@contextmanager
def job_lock():
    ensure_dirs()
    try:
        fd = os.open(str(LOCK_FILE), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise RuntimeError("Es läuft bereits ein Web-Installer-Job.") from exc
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
        os.close(fd)
        yield
    finally:
        try:
            LOCK_FILE.unlink()
        except FileNotFoundError:
            pass


def run_cmd(args: list[str], timeout: int = 8, cwd: Path | str | None = None) -> dict[str, Any]:
    try:
        result = subprocess.run(
            args,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
            check=False,
            cwd=str(cwd) if cwd else None,
        )
        return {
            "ok": result.returncode == 0,
            "returncode": result.returncode,
            "stdout": result.stdout.strip(),
            "stderr": result.stderr.strip(),
        }
    except Exception as exc:
        return {"ok": False, "returncode": None, "stdout": "", "stderr": str(exc)}


def normalize_missing_unit_step(step: dict[str, Any]) -> dict[str, Any]:
    """Treat already-missing optional units as a successful target state."""
    if step.get("ok"):
        return step
    text = " ".join(str(step.get(part, "")) for part in ("stdout", "stderr", "error")).lower()
    missing_markers = (
        "not loaded",
        "not-found",
        "not found",
        "could not be found",
        "unit file does not exist",
        "does not exist",
        "no such file",
    )
    if any(marker in text for marker in missing_markers):
        normalized = dict(step)
        normalized["ok"] = True
        normalized["noop"] = True
        normalized["message"] = "Dienst war systemd nicht bekannt; Zielzustand bereits erreicht."
        return normalized
    return step


def python_executable() -> str:
    for candidate in (Path("/opt/venv/bin/python3"), Path("/usr/bin/python3")):
        if candidate.exists():
            return str(candidate)
    return "python3"


def sanitize_system_user(value: Any) -> str | None:
    raw = str(value or "").strip()
    if not raw or "/" in raw or "\\" in raw:
        return None
    if not all(ch.isalnum() or ch in {"_", "-", "."} for ch in raw):
        return None
    return raw


def install_user() -> str:
    """Return the user that should own and run generated module services."""
    for paths_file in (CONFIG_FILE, WEB_ROOT / "e3dc_paths.json"):
        try:
            paths = json.loads(paths_file.read_text(encoding="utf-8"))
            user = sanitize_system_user(paths.get("install_user") or paths.get("user"))
            if user:
                return user
        except Exception:
            pass

    if os.name == "posix":
        try:
            import pwd

            return pwd.getpwuid(INSTALL_ROOT.stat().st_uid).pw_name
        except Exception:
            pass
    return "pi"


def load_config() -> dict[str, Any]:
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}


def cfg_text(cfg: dict[str, Any], key: str, default: str = "") -> str:
    return str(cfg.get(key, default)).strip()


def cfg_enabled(cfg: dict[str, Any], key: str) -> bool:
    return cfg_text(cfg, key).lower() in {"1", "true", "yes", "on"}


def cfg_has_address(cfg: dict[str, Any], key: str) -> bool:
    return cfg_text(cfg, key).lower() not in {"", "0", "0.0.0.0", "none", "null"}


def module_config_required_keys(module_key: str, cfg: dict[str, Any], fallback_keys: tuple[str, ...]) -> tuple[str, ...]:
    wp_type = cfg_text(cfg, "wp_type", "0")
    if module_key == "wallbox":
        return ("wb_native_enable", "Legacy-C++ Wallboxsteuerung aus")
    if module_key == "heatpump":
        if has_shelly_sgready_config(cfg):
            return ("luxtronik", "shelly_sg_ip oder shelly_pause_ip")
        if wp_type == "0":
            return ("luxtronik", "wp_type", "luxtronik_ip")
        if wp_type == "1":
            return ("luxtronik", "wp_type", "idm_ip")
        if wp_type == "4":
            return ("luxtronik", "wp_type", "stiebel_isg_ip")
        if wp_type == "5":
            return ("luxtronik", "wp_type", "dimplex_ip")
        return ("luxtronik", "wp_type=Luxtronik, IDM, Stiebel oder Dimplex")
    if module_key == "lux_live":
        return ("luxtronik", "wp_type=Luxtronik", "luxtronik_ip")
    if module_key == "idm_live":
        return ("luxtronik", "wp_type=IDM", "idm_ip")
    if module_key == "stiebel_live":
        return ("luxtronik", "wp_type=Stiebel", "stiebel_isg_ip")
    if module_key == "dimplex_live":
        return ("luxtronik", "wp_type=Dimplex", "dimplex_ip")
    if module_key == "heizstab":
        if cfg_has_address(cfg, "heizstab_ip") or cfg_has_address(cfg, "shelly_heiz_ip"):
            return ("heizstab_ip oder shelly_heiz_ip",)
        if wp_type == "3":
            return ("wp_type=Shelly Pro3EM", "shelly_3em_ip")
        return ("heizstab_ip oder shelly_heiz_ip",)
    if module_key == "climate_live":
        return ("climate_enable", "climate_meter_ip")
    return fallback_keys


def module_config_missing(module_key: str, cfg: dict[str, Any], required_keys: tuple[str, ...]) -> list[str]:
    special_modules = {"wallbox", "heatpump", "lux_live", "idm_live", "stiebel_live", "dimplex_live", "heizstab", "climate_live"}
    missing = [] if module_key in special_modules else [
        key for key in required_keys if cfg.get(key) in (None, "", [])
    ]

    if module_key == "wallbox":
        if not cfg_enabled(cfg, "wb_native_enable"):
            missing.append("wb_native_enable")
        if cfg_enabled(cfg, "wallbox"):
            missing.append("wallbox=0 (Legacy-C++ Wallbox aus)")
        if cfg_text(cfg, "wbmode", "0") not in {"", "0"}:
            missing.append("wbmode=0 (Legacy-C++ Wallbox aus)")

    elif module_key == "heatpump":
        if not cfg_enabled(cfg, "luxtronik"):
            missing.append("luxtronik")
        wp_type = cfg_text(cfg, "wp_type", "0")
        if has_shelly_sgready_config(cfg):
            pass
        elif wp_type == "0" and not cfg_has_address(cfg, "luxtronik_ip"):
            missing.append("luxtronik_ip")
        elif wp_type == "1" and not cfg_has_address(cfg, "idm_ip"):
            missing.append("idm_ip")
        elif wp_type == "4" and not cfg_has_address(cfg, "stiebel_isg_ip"):
            missing.append("stiebel_isg_ip")
        elif wp_type == "5" and not cfg_has_address(cfg, "dimplex_ip"):
            missing.append("dimplex_ip")
        elif wp_type not in {"0", "1", "4", "5"}:
            missing.append("wp_type=Luxtronik, IDM, Stiebel oder Dimplex")

    elif module_key == "lux_live":
        if not cfg_enabled(cfg, "luxtronik"):
            missing.append("luxtronik")
        if cfg_text(cfg, "wp_type", "0") != "0":
            missing.append("wp_type=Luxtronik")
        if not cfg_has_address(cfg, "luxtronik_ip"):
            missing.append("luxtronik_ip")

    elif module_key == "idm_live":
        if not cfg_enabled(cfg, "luxtronik"):
            missing.append("luxtronik")
        if cfg_text(cfg, "wp_type", "0") != "1":
            missing.append("wp_type=IDM")
        if not cfg_has_address(cfg, "idm_ip"):
            missing.append("idm_ip")

    elif module_key == "stiebel_live":
        if not cfg_enabled(cfg, "luxtronik"):
            missing.append("luxtronik")
        if cfg_text(cfg, "wp_type", "0") != "4":
            missing.append("wp_type=Stiebel")
        if not cfg_has_address(cfg, "stiebel_isg_ip"):
            missing.append("stiebel_isg_ip")

    elif module_key == "dimplex_live":
        if not cfg_enabled(cfg, "luxtronik"):
            missing.append("luxtronik")
        if cfg_text(cfg, "wp_type", "0") != "5":
            missing.append("wp_type=Dimplex")
        if not cfg_has_address(cfg, "dimplex_ip"):
            missing.append("dimplex_ip")

    elif module_key == "heizstab":
        wp_type = cfg_text(cfg, "wp_type", "0")
        has_aux_heater = cfg_has_address(cfg, "heizstab_ip") or cfg_has_address(cfg, "shelly_heiz_ip")
        if has_aux_heater:
            pass
        elif wp_type == "3":
            if not cfg_has_address(cfg, "shelly_3em_ip"):
                missing.append("shelly_3em_ip")
        else:
            missing.append("heizstab_ip oder shelly_heiz_ip")

    elif module_key == "climate_live":
        if not cfg_enabled(cfg, "climate_enable"):
            missing.append("climate_enable")
        if not cfg_has_address(cfg, "climate_meter_ip"):
            missing.append("climate_meter_ip")

    return list(dict.fromkeys(missing))


def module_dependency_keys(module_key: str, cfg: dict[str, Any], base_dependencies: tuple[str, ...]) -> tuple[str, ...]:
    dependencies = list(base_dependencies)
    if module_key == "heatpump":
        wp_type = cfg_text(cfg, "wp_type", "0")
        if wp_type == "0":
            dependencies.append("lux_live")
        elif wp_type == "1":
            dependencies.append("idm_live")
        elif wp_type == "4":
            dependencies.append("stiebel_live")
        elif wp_type == "5":
            dependencies.append("dimplex_live")
    return tuple(dict.fromkeys(dependencies))


HEAT_SOURCE_MODULE_KEYS = ("heatpump", "lux_live", "idm_live", "stiebel_live", "dimplex_live", "heizstab")


def has_aux_heater_config(cfg: dict[str, Any]) -> bool:
    return (
        cfg_has_address(cfg, "heizstab_ip")
        or cfg_has_address(cfg, "shelly_heiz_ip")
        or cfg_enabled(cfg, "heizstab")
    )


def has_shelly_sgready_config(cfg: dict[str, Any]) -> bool:
    return cfg_has_address(cfg, "shelly_sg_ip") or cfg_has_address(cfg, "shelly_pause_ip")


def heat_source_allowed_companions(module_key: str, cfg: dict[str, Any]) -> set[str]:
    """Modules that may coexist for the currently selected heat-source type."""
    wp_type = cfg_text(cfg, "wp_type", "0")
    aux_heater = has_aux_heater_config(cfg)
    if module_key == "heatpump":
        if wp_type == "0":
            allowed = {"heatpump", "lux_live"}
        if wp_type == "1":
            allowed = {"heatpump", "idm_live"}
        if wp_type == "4":
            allowed = {"heatpump", "stiebel_live"}
        if wp_type == "5":
            allowed = {"heatpump", "dimplex_live"}
        if wp_type not in {"0", "1", "4", "5"}:
            allowed = {"heatpump"}
        if aux_heater:
            allowed.add("heizstab")
        return allowed
    if module_key == "lux_live":
        allowed = {"lux_live", "heatpump"} if wp_type == "0" else {"lux_live"}
        if aux_heater:
            allowed.add("heizstab")
        return allowed
    if module_key == "idm_live":
        allowed = {"idm_live", "heatpump"} if wp_type == "1" else {"idm_live"}
        if aux_heater:
            allowed.add("heizstab")
        return allowed
    if module_key == "stiebel_live":
        allowed = {"stiebel_live", "heatpump"} if wp_type == "4" else {"stiebel_live"}
        if aux_heater:
            allowed.add("heizstab")
        return allowed
    if module_key == "dimplex_live":
        allowed = {"dimplex_live", "heatpump"} if wp_type == "5" else {"dimplex_live"}
        if aux_heater:
            allowed.add("heizstab")
        return allowed
    if module_key == "heizstab":
        if wp_type == "3" and not aux_heater:
            return {"heizstab"}
        return set(HEAT_SOURCE_MODULE_KEYS)
    return {module_key}


def module_present_for_conflict(module: Any, docker: bool) -> tuple[bool, dict[str, Any]]:
    """Return whether another heat-source module is installed/active enough to be a conflict."""
    if docker:
        alive = file_fresh(module.alive_file, module.alive_max_age_s)
        return bool(alive.get("exists") or alive.get("fresh")), {
            "exists": alive.get("exists", False),
            "active": alive.get("fresh", False),
            "enabled": alive.get("fresh", False),
            "raw": "docker-alive-check",
        }
    status = service_status(module.service_unit)
    return bool(status.get("exists") or status.get("active") or status.get("enabled")), status


def module_matches_current_heat_config(module_key: str, cfg: dict[str, Any]) -> bool:
    """Return whether a heat-source module belongs to the currently selected WP setup."""
    if module_key not in HEAT_SOURCE_MODULE_KEYS:
        return True
    module = get_module(module_key)
    fallback_keys = module.config_keys if module is not None else ()
    required = module_config_required_keys(module_key, cfg, fallback_keys)
    return not module_config_missing(module_key, cfg, required)


def module_conflict_reasons(module_key: str, cfg: dict[str, Any], docker: bool | None = None) -> list[str]:
    """Block mutually exclusive heat-source modules before write actions can run."""
    if module_key not in HEAT_SOURCE_MODULE_KEYS:
        return []
    docker_mode = is_docker() if docker is None else docker
    allowed = heat_source_allowed_companions(module_key, cfg)
    current = get_module(module_key)
    reasons: list[str] = []

    for other_key in HEAT_SOURCE_MODULE_KEYS:
        if other_key == module_key or other_key in allowed:
            continue
        other = get_module(other_key)
        if other is None:
            continue
        if docker_mode and not module_matches_current_heat_config(other_key, cfg):
            continue
        present, state = module_present_for_conflict(other, docker_mode)
        if not present:
            continue
        other_state = "aktiv" if state.get("active") else ("installiert" if state.get("exists") else "bekannt")
        current_name = current.display_name if current is not None else module_key
        reasons.append(
            f"Konflikt: {current_name} kann nicht parallel zu {other.display_name} installiert werden "
            f"({other_state}; gemeinsamer WP-/Heizstab-Konfigurationspfad). "
            "Bitte zuerst das andere Wärme-/Heizstab-Modul zurückbauen oder den WP-Typ sauber umstellen."
        )
    return reasons


def load_last_status() -> dict[str, Any] | None:
    try:
        return json.loads(STATUS_FILE.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except Exception as exc:
        return {"error": str(exc)}


def is_docker() -> bool:
    if Path("/.dockerenv").exists():
        return True
    return str(os.environ.get("E3DC_CONTAINER_MODE") or "").strip().lower() in {"1", "true", "yes"}


def installer_status() -> dict[str, Any]:
    last_job = load_last_status()
    lock_active = LOCK_FILE.exists()
    return {
        "success": True,
        "write_actions_enabled": WRITE_ACTIONS_ENABLED,
        "write_mode": "freigeschaltet" if WRITE_ACTIONS_ENABLED else "gesperrt",
        "docker": is_docker(),
        "install_root": str(INSTALL_ROOT),
        "web_root": str(WEB_ROOT),
        "status_file": str(STATUS_FILE),
        "job_file": str(JOB_FILE),
        "lock_file": str(LOCK_FILE),
        "lock_active": lock_active,
        "last_job": last_job,
        "read_actions": sorted(READ_ONLY_ACTIONS),
        "write_actions": WRITE_ACTION_NAMES,
        "message": (
            "Web-Installer läuft im sicheren Lesemodus."
            if not WRITE_ACTIONS_ENABLED
            else "Web-Installer-Schreibaktionen sind explizit freigeschaltet."
        ),
    }


def update_check() -> dict[str, Any]:
    """Check Git updates through the sudo-approved installer wrapper path."""
    repo_dir = INSTALL_ROOT.resolve()
    if not (repo_dir / ".git").is_dir():
        return {
            "success": False,
            "missing": 0,
            "repo": str(repo_dir),
            "error": "Git-Repository nicht gefunden.",
        }

    user = install_user()
    run_cmd(["sudo", "-H", "-u", user, "git", "config", "--global", "--add", "safe.directory", str(repo_dir)], timeout=5)

    fetch = run_cmd(["sudo", "-H", "-u", user, "git", "-C", str(repo_dir), "fetch", "origin"], timeout=20)
    if not fetch.get("ok"):
        return {
            "success": False,
            "missing": 0,
            "repo": str(repo_dir),
            "error": "\n".join(part for part in [fetch.get("stdout", ""), fetch.get("stderr", "")] if part),
        }

    upstream = run_cmd(
        ["sudo", "-H", "-u", user, "git", "-C", str(repo_dir), "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"],
        timeout=5,
    )
    target = upstream.get("stdout", "").strip() if upstream.get("ok") else ""
    if not target:
        target = "origin/main"

    count = run_cmd(["sudo", "-H", "-u", user, "git", "-C", str(repo_dir), "rev-list", "--count", f"HEAD..{target}"], timeout=5)
    if not count.get("ok"):
        return {
            "success": False,
            "missing": 0,
            "repo": str(repo_dir),
            "upstream": target,
            "error": "\n".join(part for part in [count.get("stdout", ""), count.get("stderr", "")] if part),
        }

    try:
        missing = int(count.get("stdout", "0").strip())
    except ValueError:
        missing = 0

    return {
        "success": True,
        "missing": missing,
        "repo": str(repo_dir),
        "upstream": target,
    }


def job_status() -> dict[str, Any]:
    return {
        "success": True,
        "lock_active": LOCK_FILE.exists(),
        "lock_file": str(LOCK_FILE),
        "status_file": str(STATUS_FILE),
        "job_file": str(JOB_FILE),
        "last_job": load_last_status(),
    }


def file_check(path: Path, label: str, executable: bool = False, hard: bool = True) -> dict[str, Any]:
    exists = path.exists()
    ok = exists and (not executable or os.access(path, os.X_OK))
    issue = None
    if not exists:
        issue = "fehlt"
    elif executable and not os.access(path, os.X_OK):
        issue = "nicht ausführbar"
    return {
        "label": label,
        "path": str(path),
        "ok": ok,
        "hard": hard,
        "issue": issue,
    }


def sudoers_context() -> dict[str, Any]:
    sudoers_chunks = []
    sudoers_sources = []
    file_findings = sudoers_file_findings()
    try:
        sudoers_chunks.append(SUDOERS_FILE.read_text(encoding="utf-8", errors="replace"))
        sudoers_sources.append("file")
    except Exception:
        pass

    sudoers_listing = run_cmd(["sudo", "-n", "-l"], timeout=5)
    www_data_listing = run_cmd(["sudo", "-u", "www-data", "sudo", "-n", "-l"], timeout=5)
    for listing in (sudoers_listing, www_data_listing):
        if listing.get("ok"):
            sudoers_chunks.append(listing.get("stdout", ""))
            sudoers_chunks.append(listing.get("stderr", ""))
            sudoers_sources.append("sudo-list")

    sudoers_text = "\n".join(sudoers_chunks)
    sudoers_source = "+".join(dict.fromkeys(sudoers_sources)) if sudoers_sources else "sudo-list-unavailable"

    active_lines = [
        line.strip()
        for line in sudoers_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    direct_systemctl_lines = [
        line
        for line in active_lines
        if "systemctl" in line and "service_wrapper.sh" not in line
    ]
    legacy_lines = [line for line in active_lines if "e3dc.service" in line]

    return {
        "text": sudoers_text,
        "source": sudoers_source,
        "active_lines": active_lines,
        "direct_systemctl_lines": direct_systemctl_lines,
        "legacy_lines": legacy_lines,
        "file_findings": file_findings,
    }


def write_readiness(ignore_active_lock: bool = False) -> dict[str, Any]:
    sudoers = sudoers_context()
    sudoers_text = sudoers["text"]
    sudoers_source = sudoers["source"]

    service_wrapper = file_check(SERVICE_WRAPPER, "Service-Wrapper", executable=True)
    installer_wrapper = file_check(INSTALLER_WRAPPER, "Installer-Wrapper", executable=True)
    wrapper_integrity = wrapper_integrity_preview()
    wrapper_integrity_ok = bool(wrapper_integrity.get("success")) and not wrapper_integrity.get("repair_needed")
    sudoers_exists = SUDOERS_FILE.exists()
    sudoers_has_service = str(SERVICE_WRAPPER) in sudoers_text
    sudoers_has_installer = str(INSTALLER_WRAPPER) in sudoers_text
    sudoers_direct_systemctl = bool(sudoers["direct_systemctl_lines"])
    sudoers_direct_web_commands = bool(sudoers["file_findings"]["direct_web_lines"])
    legacy_service_allowed = bool(sudoers["legacy_lines"])
    catalog_legacy = any((module.service_unit or "") == "e3dc.service" for module in iter_modules())

    checks = [
        {
            "label": "Schreibmodus",
            "ok": not WRITE_ACTIONS_ENABLED,
            "hard": False,
            "status": "gesperrt" if not WRITE_ACTIONS_ENABLED else "freigeschaltet",
            "issue": None if not WRITE_ACTIONS_ENABLED else "nur für bewusst freigegebene Tests aktiv lassen",
        },
        {
            "label": "Systemtyp",
            "ok": not is_docker(),
            "hard": False,
            "status": "Docker" if is_docker() else "Bare-Metal",
            "issue": "Docker benötigt später einen eigenen Container-Ablauf" if is_docker() else None,
        },
        service_wrapper,
        installer_wrapper,
        {
            "label": "Wrapperintegrität gegen lokalen Git-HEAD",
            "ok": wrapper_integrity_ok,
            "hard": True,
            "status": wrapper_integrity.get("head") or "nicht gebunden",
            "issue": None if wrapper_integrity_ok else "Wrapper fehlen, haben CRLF-Drift oder weichen unsicher von Git-HEAD ab",
            "details": wrapper_integrity,
        },
        {
            "label": "sudoers-Datei",
            "path": str(SUDOERS_FILE),
            "ok": sudoers_exists,
            "hard": True,
            "status": sudoers_source,
            "issue": None if sudoers_exists else "fehlt",
        },
        {
            "label": "sudoers: Service-Wrapper erlaubt",
            "ok": sudoers_has_service,
            "hard": True,
            "issue": None if sudoers_has_service else "Service-Wrapper ist noch nicht in sudoers eingetragen",
        },
        {
            "label": "sudoers: Installer-Wrapper erlaubt",
            "ok": sudoers_has_installer,
            "hard": True,
            "issue": None if sudoers_has_installer else "Installer-Wrapper ist noch nicht in sudoers eingetragen",
        },
        {
            "label": "keine freien systemctl-Kommandos",
            "ok": not sudoers_direct_systemctl,
            "hard": True,
            "issue": "sudoers enthält direkte systemctl-Freigaben" if sudoers_direct_systemctl else None,
        },
        {
            "label": "keine direkten www-data-Kommandos",
            "ok": not sudoers_direct_web_commands,
            "hard": True,
            "issue": "sudoers.d enthält alte direkte www-data-Freigaben außerhalb der Wrapper" if sudoers_direct_web_commands else None,
        },
        {
            "label": "alter C++ Dienst bleibt gesperrt",
            "ok": not legacy_service_allowed and not catalog_legacy,
            "hard": True,
            "issue": "e3dc.service darf nur erkannt/deaktiviert, aber nicht als Startziel freigegeben werden"
            if legacy_service_allowed or catalog_legacy
            else None,
        },
        {
            "label": "kein aktiver Job-Lock",
            "ok": ignore_active_lock or not LOCK_FILE.exists(),
            "hard": True,
            "issue": None if ignore_active_lock or not LOCK_FILE.exists() else "ein Web-Installer-Job läuft oder hängt noch",
        },
    ]
    hard_blockers = [item for item in checks if item.get("hard") and not item.get("ok")]
    ready_for_enable = not hard_blockers and not is_docker()
    return {
        "success": True,
        "write_actions_enabled": WRITE_ACTIONS_ENABLED,
        "ready_for_manual_enable": ready_for_enable,
        "can_write_now": WRITE_ACTIONS_ENABLED and ready_for_enable,
        "summary": (
            "Schreibaktionen bleiben gesperrt. Die Sicherheitsvoraussetzungen wirken vorbereitet."
            if ready_for_enable and not WRITE_ACTIONS_ENABLED
            else "Freigabe-Check abgeschlossen."
        ),
        "checks": checks,
        "hard_blocker_count": len(hard_blockers),
        "hard_blockers": hard_blockers,
        "allowed_write_actions": WRITE_ACTION_NAMES,
        "release_steps": [
            "Wrapper und sudoers auf dem Zielsystem prüfen",
            "Job-Test für mindestens ein optionales Modul erfolgreich ausführen",
            "Schreibmodus nur bewusst und zeitlich begrenzt freischalten",
            "Erste echte Installation nur auf einem Testsystem mit Backup ausführen",
            "Nach jedem Schreibjob Alive-Datei, Log und Dienststatus prüfen",
        ],
        "next_step": (
            "Echte Jobs erst nach bewusster Freigabe über Wrapper und Testplan aktivieren."
            if ready_for_enable
            else "Zuerst die harten Blocker beheben, danach erneut prüfen."
        ),
    }


def write_permission_plan() -> dict[str, Any]:
    """Read-only-Plan für die spätere Bereinigung von sudoers und Wrappern."""
    sudoers = sudoers_context()
    desired_sudoers = desired_sudoers_lines()
    checks = write_readiness()
    remove_lines = list(dict.fromkeys(sudoers["direct_systemctl_lines"] + sudoers["legacy_lines"]))
    missing_lines = [line for line in desired_sudoers if line not in sudoers["text"]]
    legacy_file_findings = sudoers["file_findings"]
    would_change = bool(
        remove_lines
        or missing_lines
        or not SUDOERS_FILE.exists()
        or legacy_file_findings["direct_web_lines"]
        or legacy_file_findings["legacy_lines"]
    )
    file_preview = sudoers_file_preview()

    return {
        "success": True,
        "read_only": True,
        "write_actions_enabled": WRITE_ACTIONS_ENABLED,
        "summary": "Freigabe-Plan: keine Änderung am System. Der Plan beschreibt nur die spätere sichere sudoers-Bereinigung.",
        "sudoers_file": str(SUDOERS_FILE),
        "sudoers_source": sudoers["source"],
        "would_change": would_change,
        "current": {
            "active_lines": sudoers["active_lines"],
            "direct_systemctl_lines": sudoers["direct_systemctl_lines"],
            "legacy_lines": sudoers["legacy_lines"],
            "file_findings": sudoers["file_findings"],
        },
        "target": {
            "allowed_lines": desired_sudoers,
            "missing_lines": missing_lines,
            "service_wrapper": str(SERVICE_WRAPPER),
            "installer_wrapper": str(INSTALLER_WRAPPER),
        },
        "file_preview": file_preview,
        "planned_steps": [
            "Bestehende sudoers-Datei sichern, bevor sie ersetzt wird.",
            "Direkte systemctl-Freigaben entfernen; die WebUI darf systemd nur über Wrapper erreichen.",
            "Nur die zwei erlaubten Wrapper-Zeilen für service_wrapper.sh und installer_wrapper.sh setzen.",
            "sudoers-Syntax mit visudo -cf prüfen.",
            "Freigabe-Check erneut ausführen und erst danach Schreibmodus zeitlich begrenzt testen.",
        ],
        "rollback_plan": [
            "Backup der sudoers-Datei wiederherstellen.",
            "visudo -cf erneut prüfen.",
            "Web-Installer-Status aktualisieren und Schreibmodus gesperrt lassen.",
        ],
        "validation_commands": [
            f"visudo -cf {SUDOERS_FILE}",
            "visudo -cf /etc/sudoers",
            "sudo -u www-data sudo -n -l",
        ],
        "safety_rules": [
            "Keine freien Shell-Kommandos aus PHP.",
            "Keine direkten systemctl-Freigaben für www-data.",
            "Der alte C++ Dienst e3dc.service bleibt kein erlaubtes WebUI-Startziel.",
            "Echte Reparatur erst nach erfolgreichem Job-Test auf einem Testsystem.",
        ],
        "readiness": checks,
    }


def backup_plan(module_key: str | None = None) -> dict[str, Any]:
    """Read-only-Backup- und Rückfallplan vor späteren Schreibaufträgen."""
    modules = [get_module(module_key)] if module_key else list(iter_modules())
    modules = [module for module in modules if module is not None]
    if module_key and not modules:
        return {
            "success": False,
            "read_only": True,
            "error": f"Unbekanntes Modul: {module_key}",
        }

    base_backup_dir = INSTALL_ROOT / "backups" / "web_installer"
    timestamp_hint = "YYYYmmdd-HHMMSS"
    config_files = [
        DATA_DIR / "e3dc_v4.json",
        WEB_ROOT / "e3dc_paths.json",
    ]
    history_files = [
        WEB_ROOT / "live_history.txt",
        WEB_ROOT / "ramdisk" / "live_history.txt",
        WEB_ROOT / "ramdisk" / "storage_plan.json",
        WEB_ROOT / "ramdisk" / "storage_manager_state.json",
    ]
    system_files = [
        SUDOERS_FILE,
        SERVICE_WRAPPER,
        INSTALLER_WRAPPER,
    ]
    for module in modules:
        if module.service_unit:
            system_files.append(Path("/etc/systemd/system") / module.service_unit)
        if module.script:
            system_files.append(INSTALLER_DIR / module.script)
        if module.log_file:
            history_files.append(Path(module.log_file))
        if module.alive_file:
            history_files.append(Path(module.alive_file))

    def describe(path: Path, category: str) -> dict[str, Any]:
        exists = path.exists()
        return {
            "path": str(path),
            "category": category,
            "exists": exists,
            "size": path.stat().st_size if exists and path.is_file() else None,
            "owner": owner_group(path) if exists else None,
            "backup_target": str(base_backup_dir / timestamp_hint / category / backup_relative_path(path)),
        }

    seen: set[str] = set()
    items: list[dict[str, Any]] = []
    for category, paths in (
        ("config", config_files),
        ("history", history_files),
        ("system", system_files),
    ):
        for path in paths:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            items.append(describe(path, category))

    existing = [item for item in items if item["exists"]]
    missing = [item for item in items if not item["exists"]]
    module_names = [module.key for module in modules]
    return {
        "success": True,
        "read_only": True,
        "write_actions_enabled": WRITE_ACTIONS_ENABLED,
        "summary": "Backup-Plan: keine Änderung am System. Der Plan zeigt, was vor späteren Schreibjobs gesichert würde.",
        "scope": "module" if module_key else "system",
        "module": module_key,
        "modules": module_names,
        "backup_root": str(base_backup_dir),
        "timestamp_hint": timestamp_hint,
        "would_backup_count": len(existing),
        "missing_count": len(missing),
        "items": items,
        "planned_steps": [
            "Zeitgestempeltes Backup-Verzeichnis anlegen.",
            "Config, History/Ramdisk-Snapshots, Wrapper, sudoers und betroffene systemd-Units sichern.",
            "Schreibjob erst starten, wenn das Backup vollständig ist.",
            "Nach dem Schreibjob Dienststatus, Logdatei und Alive-Datei prüfen.",
        ],
        "rollback_plan": [
            "Betroffene systemd-Units aus dem Backup wiederherstellen.",
            "Config- und History-Dateien nur bei Bedarf gezielt zurückspielen.",
            "systemctl daemon-reload ausführen.",
            "Dienste aus dem Katalog neu prüfen und Web-Installer-Status aktualisieren.",
        ],
        "safety_rules": [
            "Dieser Plan liest nur; er erstellt noch kein Backup.",
            "Echte Backups dürfen nur über den Installer-Wrapper entstehen.",
            "Backup-Snapshots liegen außerhalb des Webroots.",
            "Freie Pfade aus der WebUI bleiben verboten.",
            "Historische Daten und Konfiguration werden bei Tests auf Feldsystemen nicht verändert.",
        ],
    }


def create_backup_snapshot(action: str, module_key: str | None = None) -> dict[str, Any]:
    """Create the concrete backup bundle that backup_plan previews."""
    safe_action = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in action) or "job"
    safe_module = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in (module_key or "system"))
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    collection_root = default_backup_root(INSTALL_ROOT) / "web_installer"
    backup_root = collection_root / f"{timestamp}-{safe_action}-{safe_module}"
    plan = backup_plan(module_key)
    copied: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    if not plan.get("success"):
        return {
            "success": False,
            "root": str(backup_root),
            "message": plan.get("error") or "Backup-Plan konnte nicht berechnet werden.",
            "copied": copied,
            "skipped": skipped,
            "plan": plan,
        }

    try:
        backup_root.mkdir(parents=True, exist_ok=True)
        apply_config_backup_dir_permissions(backup_root, install_user=install_user())
    except Exception as exc:
        return {
            "success": False,
            "root": str(backup_root),
            "message": f"Backup-Verzeichnis konnte nicht angelegt werden: {exc}",
            "copied": copied,
            "skipped": skipped,
            "plan": plan,
        }
    for item in plan.get("items", []):
        source = Path(str(item.get("path") or ""))
        if not item.get("exists"):
            skipped.append({"path": str(source), "reason": "Quelle fehlt"})
            continue
        if not source.is_file():
            skipped.append({"path": str(source), "reason": "Quelle ist keine Datei"})
            continue
        try:
            size = source.stat().st_size
        except Exception as exc:
            skipped.append({"path": str(source), "reason": f"Quelle nicht lesbar: {exc}"})
            continue
        if size > MAX_BACKUP_FILE_BYTES:
            skipped.append({
                "path": str(source),
                "reason": f"Datei groesser als {MAX_BACKUP_FILE_BYTES // 1024 // 1024} MB",
                "size": size,
            })
            continue
        target = backup_root / str(item.get("category") or "files") / backup_relative_path(source)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            if str(item.get("category") or "") == "config":
                apply_config_backup_dir_permissions(target.parent, install_user=install_user())
                apply_config_secret_permissions(target, install_user=install_user())
            copied.append({"path": str(source), "backup": str(target), "size": size})
        except Exception as exc:
            skipped.append({"path": str(source), "reason": f"Kopie fehlgeschlagen: {exc}", "size": size})

    existing_count = int(plan.get("would_backup_count") or 0)
    success = bool(copied) or existing_count == 0
    if success:
        try:
            if not copied:
                marker = backup_root / "snapshot.json"
                marker.write_text(
                    json.dumps(
                        {
                            "schema": "web_installer_snapshot_v1",
                            "action": safe_action,
                            "module": safe_module,
                            "copied_count": 0,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ) + "\n",
                    encoding="utf-8",
                )
            secure_backup_tree(backup_root)
            finalize_backup(
                backup_root,
                [],
                [],
                kind=WEB_SNAPSHOT_KIND,
                install_root=INSTALL_ROOT,
            )
        except Exception as exc:
            success = False
            skipped.append({"path": str(backup_root), "reason": f"Snapshot-Manifest fehlgeschlagen: {exc}"})
    retention = prune_backup_dir(
        collection_root,
        keep_count=WEB_INSTALLER_BACKUP_KEEP_COUNT,
        expected_kind=WEB_SNAPSHOT_KIND,
    )

    return {
        "success": success,
        "root": str(backup_root),
        "copied_count": len(copied),
        "skipped_count": len(skipped),
        "copied": copied,
        "skipped": skipped,
        "message": "Backup-Snapshot angelegt." if success else "Backup-Snapshot konnte keine vorhandene Datei sichern.",
        "plan_summary": plan.get("summary"),
        "retention": retention,
    }


def file_fresh(path: str | None, max_age_s: int) -> dict[str, Any]:
    if not path:
        return {"path": None, "exists": False, "fresh": False, "age_s": None}
    fpath = Path(path)
    if not fpath.exists():
        return {"path": path, "exists": False, "fresh": False, "age_s": None}
    age = max(0, int(time.time() - fpath.stat().st_mtime))
    return {"path": path, "exists": True, "fresh": age <= max_age_s, "age_s": age}


def read_log_preview(path: str | None, max_lines: int = 12) -> dict[str, Any]:
    if not path:
        return {"path": None, "exists": False, "lines": [], "error": None}
    log_path = Path(path)
    try:
        resolved = log_path.resolve()
        logs_root = LOG_DIR.resolve()
        if logs_root not in (resolved, *resolved.parents):
            return {"path": path, "exists": False, "lines": [], "error": "Logpfad ist nicht im erlaubten Log-Verzeichnis"}
        if not resolved.exists():
            return {"path": path, "exists": False, "lines": [], "error": None}
        with resolved.open("rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            handle.seek(max(0, size - 20000))
            raw = handle.read().decode("utf-8", errors="replace")
        lines = [line for line in raw.splitlines() if line.strip()]
        return {
            "path": path,
            "exists": True,
            "lines": lines[-max_lines:],
            "line_count": min(len(lines), max_lines),
            "error": None,
        }
    except Exception as exc:
        return {"path": path, "exists": log_path.exists(), "lines": [], "error": str(exc)}


def read_journal_preview(service_unit: str | None, max_lines: int = 12) -> dict[str, Any]:
    if not service_unit:
        return {"unit": None, "available": False, "lines": [], "error": None}
    result = run_cmd(
        ["journalctl", "-u", service_unit, "-n", str(max_lines), "--no-pager", "-o", "short-iso"],
        timeout=5,
    )
    if not result.get("ok"):
        return {
            "unit": service_unit,
            "available": False,
            "lines": [],
            "error": result.get("stderr") or result.get("stdout") or "journalctl nicht verfügbar",
        }
    lines = [line for line in result.get("stdout", "").splitlines() if line.strip()]
    return {
        "unit": service_unit,
        "available": True,
        "lines": lines[-max_lines:],
        "error": None,
    }


JOURNAL_UNHEALTHY_PATTERNS = (
    "Unvollständige E3DC-RSCP-Konfiguration",
    "E3DC-RSCP-Ziel oder Zugangsdaten fehlen",
    "Kein e3dc_user",
    "server_ip oder e3dc_user fehlen",
    "Keine E3DC Credentials",
    "Permission denied",
    "Berechtigung verweigert",
)


def journal_unhealthy_reason(journal: dict[str, Any]) -> str | None:
    lines = journal.get("lines") if isinstance(journal, dict) else None
    if not isinstance(lines, list):
        return None
    for line in reversed(lines):
        text = str(line)
        for pattern in JOURNAL_UNHEALTHY_PATTERNS:
            if pattern in text:
                return pattern
    return None


def service_status(service_unit: str | None) -> dict[str, Any]:
    if not service_unit:
        return {"exists": False, "active": False, "enabled": False, "raw": "no-service"}
    unit_path = Path("/etc/systemd/system") / service_unit
    lib_path = Path("/lib/systemd/system") / service_unit
    usr_lib_path = Path("/usr/lib/systemd/system") / service_unit
    exists = unit_path.exists() or lib_path.exists() or usr_lib_path.exists()
    active = run_cmd(["systemctl", "is-active", service_unit])
    enabled = run_cmd(["systemctl", "is-enabled", service_unit])
    return {
        "exists": exists,
        "active": active["stdout"] == "active",
        "enabled": enabled["stdout"] in {"enabled", "static"},
        "raw": active["stdout"] or active["stderr"] or "unknown",
    }


def service_unit_path(service_unit: str | None) -> str | None:
    if not service_unit:
        return None
    for base in (Path("/etc/systemd/system"), Path("/lib/systemd/system"), Path("/usr/lib/systemd/system")):
        candidate = base / service_unit
        if candidate.exists():
            return str(candidate)
    return None


def validate_config(module_key: str | None = None) -> dict[str, Any]:
    cfg = load_config()
    modules = [get_module(module_key)] if module_key else list(iter_modules())
    result = {}
    for module in modules:
        if module is None:
            continue
        fallback_keys = module.config_keys if module.required_config_keys is None else module.required_config_keys
        required_keys = module_config_required_keys(module.key, cfg, fallback_keys)
        missing = module_config_missing(module.key, cfg, required_keys)
        result[module.key] = {
            "display_name": module.display_name,
            "ok": not missing,
            "missing_keys": missing,
            "missing_labels": config_field_labels(missing),
            "config_keys": module.config_keys,
            "config_key_labels": config_field_labels(module.config_keys),
            "required_config_keys": required_keys,
            "required_config_labels": config_field_labels(required_keys),
        }
    return result


def diagnose_module(module_key: str | None = None) -> dict[str, Any]:
    modules = [get_module(module_key)] if module_key else list(iter_modules())
    docker = is_docker()
    cfg = load_config()
    result = {}
    for module in modules:
        if module is None:
            continue
        alive = file_fresh(module.alive_file, module.alive_max_age_s)
        config_matches = True
        if docker and module.key in HEAT_SOURCE_MODULE_KEYS and not module_matches_current_heat_config(module.key, cfg):
            config_matches = False
            alive = {**alive, "fresh": False, "ignored_by_config": True, "config_match": False}
        systemd = service_status(module.service_unit) if not docker else {
            "exists": alive["exists"],
            "active": alive["fresh"] and config_matches,
            "enabled": alive["fresh"] and config_matches,
            "raw": "docker-file-check" if config_matches else "docker-file-check-config-mismatch",
        }
        config = validate_config(module.key).get(module.key, {})
        journal = read_journal_preview(module.service_unit)
        journal_unhealthy = journal_unhealthy_reason(journal)
        healthy = bool(systemd["active"] and (alive["fresh"] or not module.alive_file) and config.get("ok", True))
        if journal_unhealthy:
            healthy = False
        result[module.key] = {
            "module": module.public_dict(),
            "docker": docker,
            "systemd": systemd,
            "alive": alive,
            "config": config,
            "log": read_log_preview(module.log_file),
            "journal": journal,
            "journal_unhealthy": journal_unhealthy,
            "healthy": healthy,
        }
    return result


def module_dry_run(module_key: str | None = None) -> dict[str, Any]:
    modules = [get_module(module_key)] if module_key else list(iter_modules())
    docker = is_docker()
    cfg = load_config()
    result = {}
    full_diag = diagnose_module(module_key)
    for module in modules:
        if module is None:
            continue
        diag = full_diag.get(module.key, {})
        script_path = str(INSTALLER_DIR / module.script) if module.script else None
        script_exists = bool(script_path and Path(script_path).exists())
        unit_path = service_unit_path(module.service_unit)
        config = diag.get("config", {})
        alive = diag.get("alive", {})
        systemd = diag.get("systemd", {})
        dependency_states = {}
        for dep in module_dependency_keys(module.key, cfg, tuple(module.dependencies)):
            dep_diag = diagnose_module(dep).get(dep, {})
            dependency_states[dep] = {
                "healthy": dep_diag.get("healthy", False),
                "active": (dep_diag.get("systemd") or {}).get("active", False),
                "config_ok": (dep_diag.get("config") or {}).get("ok", True),
            }

        planned_steps = [
            "Modul-Katalog laden und Modul gegen Allowlist prüfen",
            "Konfiguration auf Pflichtfelder prüfen",
            "Script- und Service-Dateien prüfen",
            "Daten-/Alive-Datei prüfen",
        ]
        if not docker:
            planned_steps.append("systemd-Status prüfen")
            if not systemd.get("exists"):
                planned_steps.append("Service-Datei wäre zu erzeugen oder neu zu schreiben")
            if systemd.get("exists") and not systemd.get("enabled"):
                planned_steps.append("Dienst wäre für Autostart zu aktivieren")
            if systemd.get("exists") and not systemd.get("active"):
                planned_steps.append("Dienst könnte gestartet werden")
        else:
            planned_steps.append("Docker-Alive-Dateien statt systemd prüfen")

        blocked_reasons = []
        if module.optional and config.get("ok") is False:
            blocked_reasons.append("Konfiguration unvollständig")
        if module.script and not script_exists:
            blocked_reasons.append("Python-/Node-Script fehlt")
        for dep, state in dependency_states.items():
            if not state["healthy"]:
                blocked_reasons.append(f"Abhängigkeit nicht gesund: {dep}")

        blocked_reasons.extend(module_conflict_reasons(module.key, cfg, docker))

        result[module.key] = {
            "module": module.public_dict(),
            "docker": docker,
            "script": {"path": script_path, "exists": script_exists},
            "service": {"unit": module.service_unit, "unit_path": unit_path, **systemd},
            "alive": alive,
            "config": config,
            "dependencies": dependency_states,
            "planned_steps": planned_steps,
            "would_change": False,
            "write_actions_enabled": WRITE_ACTIONS_ENABLED,
            "blocked_reasons": blocked_reasons,
            "summary": "Dry-Run: keine Änderungen am System ausgeführt.",
        }
    return result


def module_install_dry_run(module_key: str | None = None) -> dict[str, Any]:
    modules = [get_module(module_key)] if module_key else list(iter_modules())
    docker = is_docker()
    cfg = load_config()
    result = {}
    diagnosis = diagnose_module(module_key)
    for module in modules:
        if module is None:
            continue
        diag = diagnosis.get(module.key, {})
        systemd = diag.get("systemd", {})
        alive = diag.get("alive", {})
        config = diag.get("config", {})
        script_path = safe_script_path(module.script) if module.script else None
        runner = module_runner_label(module)
        workdir = safe_working_directory(module)
        package_json = workdir / "package.json" if runner == "npm" else None
        service_target = f"/etc/systemd/system/{module.service_unit}" if module.service_unit else None
        log_path = module.log_file
        alive_path = module.alive_file
        service_user = install_user()

        dependency_states = {}
        blocked_reasons = []
        for dep in module_dependency_keys(module.key, cfg, tuple(module.dependencies)):
            dep_diag = diagnose_module(dep).get(dep, {})
            dep_state = {
                "healthy": dep_diag.get("healthy", False),
                "active": (dep_diag.get("systemd") or {}).get("active", False),
                "config_ok": (dep_diag.get("config") or {}).get("ok", True),
            }
            dependency_states[dep] = dep_state
            if not dep_state["healthy"]:
                blocked_reasons.append(f"Abhängigkeit nicht gesund: {dep}")

        if config.get("ok") is False:
            missing = ", ".join(config.get("missing_labels") or config.get("missing_keys", []))
            blocked_reasons.append(f"Konfiguration unvollständig: {missing}")
        if script_path and not script_path.exists():
            blocked_reasons.append(f"Script fehlt: {script_path}")
        if package_json and not package_json.exists():
            blocked_reasons.append(f"package.json fehlt: {package_json}")
        if not module.service_unit:
            blocked_reasons.append("Kein systemd-Dienst im Katalog hinterlegt")

        blocked_reasons.extend(module_conflict_reasons(module.key, cfg, docker))

        missing_or_inactive = not systemd.get("exists") or not systemd.get("enabled") or not systemd.get("active")
        if missing_or_inactive and not module.optional:
            blocked_reasons.append("Kernmodule sind in dieser Stufe nicht über die WebUI installierbar.")
        would_change = bool(missing_or_inactive and not blocked_reasons)
        python_dependency_packages = tuple(pkg for _, pkg in PYTHON_IMPORT_PACKAGES.get(module.key, ()))

        if docker:
            planned_steps = [
                "Docker-Umgebung erkannt: keine systemd-Unit auf dem Host installieren",
                "Config-Schalter prüfen",
                "Container-Startlogik/entrypoint würde das Modul beim nächsten Containerstart aktivieren",
                "Alive-Datei nach Container-Neustart prüfen",
            ]
        else:
            planned_steps = [
                "Modul gegen zentralen Service-Katalog und Allowlist prüfen",
                "Pflicht-Konfiguration prüfen",
                "Abhängigkeiten prüfen",
                "Script-Datei und Ausführbarkeit prüfen",
                *([f"Python-Abhängigkeiten prüfen/installieren: {', '.join(python_dependency_packages)}"] if python_dependency_packages else []),
                *([f"npm-Abhängigkeiten in {workdir} vorbereiten"] if runner == "npm" else []),
                f"systemd-Unit vorbereiten: {service_target}",
                "systemctl daemon-reload ausführen",
                "Dienst für Autostart aktivieren",
                "Dienst starten oder neu starten",
                "Alive-Datei und Log nach dem Start prüfen",
            ]

        if not missing_or_inactive and not blocked_reasons:
            planned_steps = [
                "Modul ist bereits installiert und aktiv",
                "Keine Installation nötig",
                "Optional wäre nur ein Neustart über die erlaubte Dienststeuerung sinnvoll",
            ]

        if blocked_reasons:
            readiness = {
                "state": "blocked",
                "label": "Blockiert",
                "message": "Dieses Modul ist noch nicht installierbar. Bitte zuerst die Blocker beheben.",
                "can_install_when_writes_enabled": False,
                "reasons": blocked_reasons,
            }
        elif not missing_or_inactive:
            readiness = {
                "state": "installed",
                "label": "Bereits installiert",
                "message": "Dieses Modul ist installiert, aktiv und braucht keine Installation.",
                "can_install_when_writes_enabled": False,
                "reasons": ["Keine Installation nötig"],
            }
        elif docker:
            readiness = {
                "state": "docker_pending",
                "label": "Docker-Ablauf nötig",
                "message": "Dieses Modul wird in Docker über Config speichern und Container-Neustart aktiviert, nicht über systemd.",
                "can_install_when_writes_enabled": False,
                "reasons": ["Docker nutzt keine systemd-Installation: Config speichern, Container neu starten, Log/Alive prüfen"],
            }
        else:
            readiness = {
                "state": "ready",
                "label": "Bereit für Installation",
                "message": "Dieses Modul wäre installierbar, sobald der Schreibmodus bewusst freigeschaltet wird.",
                "can_install_when_writes_enabled": True,
                "reasons": [],
            }

        install_plan = {
            "before": [
                f"Dienst: {'vorhanden' if systemd.get('exists') else 'fehlt'}, "
                f"{'aktiv' if systemd.get('active') else 'inaktiv'}, "
                f"{'Autostart aktiv' if systemd.get('enabled') else 'Autostart aus'}",
                f"Konfiguration: {'vollständig' if config.get('ok') is not False else 'unvollständig'}",
                f"Daten/Alive: {'frisch' if alive.get('fresh') else ('veraltet' if alive.get('exists') else 'kein Signal')}",
            ],
            "would_do": planned_steps,
            "expected_after": (
                [
                    "Keine Änderung nötig: Modul ist bereits installiert und aktiv.",
                    "Optionaler Neustart bleibt eine separate, erlaubte Dienstaktion.",
                ]
                if not missing_or_inactive and not blocked_reasons
                else (
                    [
                        "Docker: keine systemd-Unit im Container erzeugen.",
                        "Modul wird nach Config-Speichern und Container-Neustart über die entrypoint-Logik aktiviert.",
                    ]
                    if docker
                    else [
                        f"Service-Datei liegt unter {service_target}.",
                        f"systemd kennt den Dienst nach daemon-reload und führt ihn als Benutzer {service_user} aus.",
                        "Autostart ist aktiv.",
                        "Dienst läuft und schreibt Log/Alive-Datei.",
                    ]
                )
            ),
            "rollback": (
                [
                    "Bei Fehler: keine Änderung am Host; Docker-Ablauf prüfen.",
                    "Container/Compose-Konfiguration zurücknehmen und Alive-Datei erneut prüfen.",
                ]
                if docker
                else [
                    f"Dienst stoppen: systemctl stop {module.service_unit}",
                    f"Autostart deaktivieren: systemctl disable {module.service_unit}",
                    "Falls eine alte Unit ersetzt wurde: Backup-Snapshot aus <Installationspfad>/backups/web_installer wiederherstellen.",
                    "systemctl daemon-reload ausführen.",
                    "Log, Alive-Datei und Dienststatus erneut prüfen.",
                ]
            ),
            "safety_checks": [
                "Modul-Key muss im zentralen Service-Katalog vorhanden sein.",
                "Service-Name und Script-Pfad kommen nur aus dem Katalog.",
                "Freie Shell-Befehle und freie Dateipfade bleiben verboten.",
                "Der alte C++ Dienst e3dc.service ist kein erlaubtes WebUI-Installationsziel.",
                "Schreibmodus muss explizit freigeschaltet sein; dieser Dry-Run schreibt nichts.",
            ],
            "affected": {
                "service": module.service_unit,
                "script": str(script_path) if script_path else None,
                "service_target": service_target,
                "log_file": log_path,
                "alive_file": alive_path,
                "config_keys": module.config_keys,
                "runner": runner,
                "working_directory": str(workdir),
                "service_user": service_user,
            },
        }

        result[module.key] = {
            "module": module.public_dict(),
            "docker": docker,
            "write_actions_enabled": WRITE_ACTIONS_ENABLED,
            "would_change": would_change,
            "summary": "Installations-Dry-Run: keine Änderungen am System ausgeführt.",
            "service": {
                "unit": module.service_unit,
                "target_path": service_target,
                "unit_path": service_unit_path(module.service_unit),
                **systemd,
            },
            "script": {
                "path": str(script_path) if script_path else None,
                "exists": bool(script_path and script_path.exists()),
            },
            "log_file": log_path,
            "alive": alive,
            "config": config,
            "dependencies": dependency_states,
            "required_files": [
                item for item in [
                    str(script_path) if script_path else None,
                    str(package_json) if package_json else None,
                    service_target,
                    log_path,
                    alive_path,
                ] if item
            ],
            "required_sudoers": [
                "www-data darf nur Installer/Service-Wrapper ausführen",
                "keine freien Shell-Befehle",
                "alter C++ Dienst e3dc.service bleibt gesperrt",
            ],
            "planned_steps": planned_steps,
            "install_plan": install_plan,
            "readiness": readiness,
            "blocked_reasons": blocked_reasons,
        }
    return result


def safe_script_path(script: str | None) -> Path | None:
    if not script:
        return None
    script_path = (INSTALLER_DIR / script).resolve()
    try:
        script_path.relative_to(INSTALLER_DIR.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Script liegt außerhalb des Installer-Verzeichnisses: {script}") from exc
    return script_path


def safe_working_directory(module: Any) -> Path:
    workdir = getattr(module, "working_directory", None)
    work_path = (INSTALLER_DIR / workdir).resolve() if workdir else INSTALLER_DIR.resolve()
    try:
        work_path.relative_to(INSTALLER_DIR.resolve())
    except ValueError as exc:
        raise RuntimeError(f"Arbeitsverzeichnis liegt außerhalb des Installer-Verzeichnisses: {workdir}") from exc
    return work_path


def npm_executable() -> str:
    for candidate in (Path("/usr/bin/npm"), Path("/usr/local/bin/npm")):
        if candidate.exists():
            return str(candidate)
    return "npm"


def module_runner_label(module: Any) -> str:
    return str(getattr(module, "runner", "python") or "python").strip().lower()


def systemd_output_directives(module: Any, log_path: str) -> str:
    # Diese Dienste besitzen ihr rotierendes Dateilog selbst. systemd darf
    # nicht gleichzeitig mit einem zweiten Dateideskriptor dieselbe Datei halten.
    owns_rotating_log = {
        "live",
        "epex",
        "weather",
        "wallbox",
        "heatpump",
        "mqtt",
        "storage_manager",
        "storage_simulator",
        "idm_live",
        "dimplex_live",
    }
    if getattr(module, "key", "") in owns_rotating_log:
        return "StandardOutput=journal\nStandardError=journal"
    return f"StandardOutput=append:{log_path}\nStandardError=append:{log_path}"


def render_systemd_unit(module: Any, script_path: Path) -> str:
    log_path = module.log_file or f"/var/www/html/logs/{module.key}.log"
    workdir = safe_working_directory(module)
    user = install_user()
    output_directives = systemd_output_directives(module, log_path)
    if module_runner_label(module) == "npm":
        return f"""[Unit]
Description=E3DC-Control {module.display_name}
After=network-online.target avahi-daemon.service
Wants=network-online.target avahi-daemon.service

[Service]
Type=simple
User={user}
Group=www-data
WorkingDirectory={workdir}
ExecStart={npm_executable()} run start
Restart=always
RestartSec=10
LogRateLimitIntervalSec=60s
LogRateLimitBurst=240
{output_directives}

[Install]
WantedBy=multi-user.target
"""
    return f"""[Unit]
Description=E3DC-Control {module.display_name}
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User={user}
Group=www-data
WorkingDirectory={workdir}
ExecStart={python_executable()} -u {script_path}
Restart=always
RestartSec=10
{output_directives}

[Install]
WantedBy=multi-user.target
"""


def ensure_module_runtime_files(module: Any) -> dict[str, Any]:
    """Create runtime directories/log files with service-writable permissions."""
    steps: list[dict[str, Any]] = []
    ok = True
    user = install_user()

    def safe_chown(path: Path) -> dict[str, Any]:
        if os.name != "posix":
            return {"ok": True, "skipped": True, "reason": "nicht-posix"}
        try:
            shutil.chown(path, user=user, group="www-data")
            return {"ok": True}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}

    def ensure_dir(path: Path, mode: int = 0o775) -> dict[str, Any]:
        try:
            path.mkdir(parents=True, exist_ok=True)
            os.chmod(path, mode)
            owner = safe_chown(path)
            return {"path": str(path), "ok": bool(owner.get("ok")), "owner": owner}
        except Exception as exc:
            return {"path": str(path), "ok": False, "error": str(exc)}

    log_path = Path(module.log_file) if module.log_file else LOG_DIR / f"{module.key}.log"
    try:
        resolved = log_path.resolve()
        logs_root = LOG_DIR.resolve()
        if logs_root not in (resolved, *resolved.parents):
            step = {"step": "prepare_log", "ok": False, "path": str(log_path), "error": "Logpfad liegt außerhalb des erlaubten Log-Verzeichnisses"}
        else:
            dir_step = ensure_dir(log_path.parent)
            log_path.touch(exist_ok=True)
            os.chmod(log_path, 0o664)
            owner = safe_chown(log_path)
            step = {
                "step": "prepare_log",
                "ok": bool(dir_step.get("ok") and owner.get("ok")),
                "path": str(log_path),
                "directory": dir_step,
                "owner": owner,
            }
    except Exception as exc:
        step = {"step": "prepare_log", "ok": False, "path": str(log_path), "error": str(exc)}
    steps.append(step)
    ok = ok and bool(step.get("ok"))

    if module.alive_file:
        alive_parent = Path(module.alive_file).parent
        alive_step = ensure_dir(alive_parent, 0o775)
        alive_step["step"] = "prepare_alive_dir"
        steps.append(alive_step)
        ok = ok and bool(alive_step.get("ok"))

    return {"step": "prepare_runtime_files", "ok": ok, "steps": steps}


def prepare_module_runner(module: Any) -> dict[str, Any]:
    """Bereitet Runner für Nicht-Python-Module vor, bevor systemd sie startet."""
    runner = module_runner_label(module)
    if runner != "npm":
        return {"step": "prepare_runner", "ok": True, "runner": runner, "skipped": True}

    workdir = safe_working_directory(module)
    package_json = workdir / "package.json"
    if not package_json.exists():
        return {
            "step": "prepare_runner",
            "ok": False,
            "runner": runner,
            "workdir": str(workdir),
            "error": f"package.json fehlt: {package_json}",
        }
    npm = npm_executable()
    npm_check = run_cmd([npm, "--version"], timeout=15)
    if not npm_check.get("ok"):
        return {
            "step": "prepare_runner",
            "ok": False,
            "runner": runner,
            "workdir": str(workdir),
            "error": "npm ist nicht verfügbar",
            "npm_check": npm_check,
        }
    install = run_cmd([npm, "install"], timeout=180, cwd=workdir)
    return {
        "step": "prepare_runner",
        "ok": bool(install.get("ok")),
        "runner": runner,
        "workdir": str(workdir),
        "package_json": str(package_json),
        "npm_version": npm_check.get("stdout"),
        "install": install,
    }


def prepare_python_module_dependencies(module: Any) -> dict[str, Any]:
    """Install small apt-backed Python dependencies needed by optional modules."""
    deps = PYTHON_IMPORT_PACKAGES.get(str(module.key), ())
    if not deps:
        return {"step": "prepare_python_dependencies", "ok": True, "skipped": True, "module": module.key}

    steps: list[dict[str, Any]] = []
    ok = True
    python = python_executable()
    for import_name, apt_package in deps:
        check = run_cmd([python, "-c", f"import {import_name}"], timeout=15)
        if check.get("ok"):
            steps.append({
                "import": import_name,
                "apt_package": apt_package,
                "ok": True,
                "already_available": True,
            })
            continue

        install = run_cmd(["apt-get", "install", "-y", apt_package], timeout=180)
        if not install.get("ok"):
            update = run_cmd(["apt-get", "update"], timeout=180)
            install_retry = run_cmd(["apt-get", "install", "-y", apt_package], timeout=180)
            install = {
                **install_retry,
                "first_attempt": install,
                "apt_update": update,
            }
        recheck = run_cmd([python, "-c", f"import {import_name}"], timeout=15)
        step_ok = bool(install.get("ok") and recheck.get("ok"))
        ok = ok and step_ok
        steps.append({
            "import": import_name,
            "apt_package": apt_package,
            "ok": step_ok,
            "install": install,
            "recheck": recheck,
        })

    return {
        "step": "prepare_python_dependencies",
        "ok": ok,
        "module": module.key,
        "python": python,
        "steps": steps,
    }


def verify_service_stable(module: Any, timeout_s: int = 12, min_runtime_s: int = 3) -> dict[str, Any]:
    """Wait until the service is active long enough to catch immediate crashes."""
    deadline = time.time() + timeout_s
    last_status: dict[str, Any] = {}
    while time.time() < deadline:
        last_status = service_status(module.service_unit)
        if last_status.get("active"):
            time.sleep(min_runtime_s)
            stable_status = service_status(module.service_unit)
            return {
                "step": "verify_service_stable",
                "ok": bool(stable_status.get("active")),
                "status": stable_status,
                "waited_s": min_runtime_s,
            }
        if str(last_status.get("raw", "")).strip() in {"failed", "inactive"}:
            time.sleep(1)
        else:
            time.sleep(1)
    return {
        "step": "verify_service_stable",
        "ok": False,
        "status": last_status,
        "waited_s": timeout_s,
    }


def control_service(module_key: str | None, action: str) -> dict[str, Any]:
    if not module_key:
        raise RuntimeError("Kein Modul angegeben.")
    module = get_module(str(module_key))
    if module is None:
        raise RuntimeError(f"Unbekanntes Modul: {module_key}")
    if action not in SERVICE_ACTIONS:
        raise RuntimeError(f"Nicht erlaubte Service-Aktion: {action}")
    if not module.service_unit:
        raise RuntimeError("Modul hat keinen erlaubten systemd-Dienst im Katalog.")
    if is_docker():
        return {
            "success": False,
            "action": action,
            "module": module.public_dict(),
            "message": "Docker-Dienste werden über Config/Container-Ablauf gesteuert, nicht per systemd-Webaktion.",
        }

    readiness = write_readiness(ignore_active_lock=True)
    if readiness.get("hard_blocker_count", 1) != 0:
        return {
            "success": False,
            "action": action,
            "module": module.public_dict(),
            "readiness": readiness,
            "blocked_reasons": [
                item.get("issue") or item.get("label") or "Freigabe-Check blockiert"
                for item in readiness.get("hard_blockers", [])
            ],
            "message": "Service-Aktion abgebrochen: Freigabe-Check meldet noch harte Blocker.",
        }

    status_before = service_status(module.service_unit)
    if not status_before.get("exists"):
        return {
            "success": False,
            "action": action,
            "module": module.public_dict(),
            "status_before": status_before,
            "message": f"Service-Aktion abgebrochen: Unit {module.service_unit} ist nicht installiert.",
        }

    command_map = {
        "start": ["systemctl", "start", module.service_unit],
        "stop": ["systemctl", "stop", module.service_unit],
        "restart": ["systemctl", "restart", module.service_unit],
        "enable": ["systemctl", "enable", module.service_unit],
        "disable": ["systemctl", "disable", module.service_unit],
    }
    steps: list[dict[str, Any]] = [
        {"step": "pre_status", "ok": True, "status": status_before},
        {"step": action, **run_cmd(command_map[action], timeout=30)},
    ]
    if action in {"start", "restart"}:
        steps.append(verify_service_stable(module))
    else:
        steps.append({"step": "post_status", "ok": True, "status": service_status(module.service_unit)})
    status_after = service_status(module.service_unit)
    ok = all(step.get("ok", False) for step in steps[1:])
    if action in {"start", "restart"}:
        ok = ok and bool(status_after.get("active"))
    if action == "enable":
        ok = ok and bool(status_after.get("enabled"))
    if action == "disable":
        ok = ok and not bool(status_after.get("enabled"))
    if action == "stop":
        ok = ok and not bool(status_after.get("active"))

    return {
        "success": ok,
        "action": action,
        "module": module.public_dict(),
        "steps": steps,
        "status_before": status_before,
        "status_after": status_after,
        "diagnosis": diagnose_module(module.key).get(module.key, {}),
        "message": f"Service-Aktion {action} für {module.display_name} abgeschlossen." if ok else f"Service-Aktion {action} für {module.display_name} meldet Fehler.",
    }


def install_module(module_key: str | None = None) -> dict[str, Any]:
    if not module_key:
        raise RuntimeError("Kein Modul angegeben.")
    module = get_module(str(module_key))
    if module is None:
        raise RuntimeError(f"Unbekanntes Modul: {module_key}")
    if not module.optional:
        return {
            "success": False,
            "module": module.public_dict(),
            "blocked_reasons": ["Kernmodule sind in dieser Stufe nicht über die WebUI installierbar."],
            "message": "Installation abgebrochen: Core-Module bleiben für echte WebUI-Jobs gesperrt.",
        }
    if is_docker():
        return {
            "success": False,
            "module": module.public_dict(),
            "message": "Docker-Installation ist vorbereitet, aber noch nicht als WebUI-Schreibaktion freigeschaltet.",
        }
    if not module.service_unit:
        raise RuntimeError("Modul hat keinen erlaubten systemd-Dienst im Katalog.")

    readiness = write_readiness(ignore_active_lock=True)
    if readiness.get("hard_blocker_count", 1) != 0:
        return {
            "success": False,
            "module": module.public_dict(),
            "readiness": readiness,
            "blocked_reasons": [
                item.get("issue") or item.get("label") or "Freigabe-Check blockiert"
                for item in readiness.get("hard_blockers", [])
            ],
            "message": "Installation abgebrochen: Freigabe-Check meldet noch harte Blocker.",
        }

    dry = module_install_dry_run(module.key).get(module.key, {})
    blockers = dry.get("blocked_reasons", [])
    if blockers:
        return {
            "success": False,
            "module": module.public_dict(),
            "blocked_reasons": blockers,
            "message": "Installation abgebrochen: zuerst die Blocker beseitigen.",
        }

    script_path = safe_script_path(module.script)
    if script_path is None or not script_path.exists():
        raise RuntimeError(f"Script fehlt: {module.script}")

    target = Path("/etc/systemd/system") / module.service_unit
    unit_content = render_systemd_unit(module, script_path)
    backup_snapshot = create_backup_snapshot("install_module", module.key)
    if not backup_snapshot.get("success"):
        return {
            "success": False,
            "module": module.public_dict(),
            "message": "Installation abgebrochen: Backup-Snapshot konnte nicht angelegt werden.",
            "backup_snapshot": backup_snapshot,
        }
    backup_path = None
    if target.exists():
        existing = target.read_text(encoding="utf-8", errors="replace")
        if existing != unit_content:
            backup_path = target.with_name(f"{target.name}.bak-webinstaller-{int(time.time())}")
            backup_path.write_text(existing, encoding="utf-8")

    target.write_text(unit_content, encoding="utf-8")
    os.chmod(target, 0o644)

    steps = [
        {"step": "write_unit", "ok": True, "target": str(target), "backup": str(backup_path) if backup_path else None},
        ensure_module_runtime_files(module),
        prepare_python_module_dependencies(module),
        prepare_module_runner(module),
        {"step": "daemon_reload", **run_cmd(["systemctl", "daemon-reload"], timeout=20)},
        {"step": "enable", **run_cmd(["systemctl", "enable", module.service_unit], timeout=20)},
        {"step": "restart", **run_cmd(["systemctl", "restart", module.service_unit], timeout=20)},
    ]
    verify_step = verify_service_stable(module)
    steps.append(verify_step)
    ok = all(step.get("ok", False) for step in steps[1:])
    post_diag = diagnose_module(module.key).get(module.key, {})
    return {
        "success": ok,
        "module": module.public_dict(),
        "steps": steps,
        "diagnosis": post_diag,
        "backup_snapshot": backup_snapshot,
        "message": "Modulinstallation abgeschlossen und Dienst stabil." if ok else "Modulinstallation wurde ausgeführt, aber Start/Nachlaufprüfung meldet Fehler.",
    }


def remove_module(module_key: str | None = None) -> dict[str, Any]:
    if not module_key:
        raise RuntimeError("Kein Modul angegeben.")
    module = get_module(str(module_key))
    if module is None:
        raise RuntimeError(f"Unbekanntes Modul: {module_key}")
    if not module.optional:
        return {
            "success": False,
            "module": module.public_dict(),
            "blocked_reasons": ["Core-Module dürfen nicht deinstalliert werden."],
            "message": "Rückbau abgebrochen: Core-Module sind nur für Diagnose, Neustart und spätere Reparatur vorgesehen.",
        }
    if is_docker():
        return {
            "success": False,
            "module": module.public_dict(),
            "message": "Docker-Module werden über Config/Container-Ablauf deaktiviert, nicht per systemd-Rückbau.",
        }
    if not module.service_unit:
        raise RuntimeError("Modul hat keinen erlaubten systemd-Dienst im Katalog.")

    readiness = write_readiness(ignore_active_lock=True)
    if readiness.get("hard_blocker_count", 1) != 0:
        return {
            "success": False,
            "module": module.public_dict(),
            "readiness": readiness,
            "message": "Rückbau abgebrochen: Freigabe-Check meldet noch harte Blocker.",
        }

    unit_path = Path("/etc/systemd/system") / module.service_unit
    pre_status = service_status(module.service_unit)
    unit_known = bool(pre_status.get("exists")) or unit_path.exists()
    backup_snapshot = create_backup_snapshot("remove_module", module.key)
    if not backup_snapshot.get("success"):
        return {
            "success": False,
            "module": module.public_dict(),
            "message": "Rückbau abgebrochen: Backup-Snapshot konnte nicht angelegt werden.",
            "backup_snapshot": backup_snapshot,
        }

    steps = [
        {"step": "backup_snapshot", "ok": True, "path": backup_snapshot.get("root"), "copied_count": backup_snapshot.get("copied_count", 0)},
        {"step": "pre_status", "ok": True, "status": pre_status},
    ]
    if unit_known:
        steps.append(normalize_missing_unit_step({"step": "stop", **run_cmd(["systemctl", "stop", module.service_unit], timeout=20)}))
        steps.append(normalize_missing_unit_step({"step": "disable", **run_cmd(["systemctl", "disable", module.service_unit], timeout=20)}))
    else:
        steps.append({"step": "stop", "ok": True, "noop": True, "message": "Unit war nicht vorhanden; kein Stop noetig."})
        steps.append({"step": "disable", "ok": True, "noop": True, "message": "Unit war nicht vorhanden; kein Disable noetig."})
    if unit_path.exists():
        try:
            unit_path.unlink()
            steps.append({"step": "remove_unit", "ok": True, "target": str(unit_path)})
        except Exception as exc:
            steps.append({"step": "remove_unit", "ok": False, "target": str(unit_path), "error": str(exc)})
    else:
        steps.append({"step": "remove_unit", "ok": True, "target": str(unit_path), "message": "Unit war nicht vorhanden"})
    if unit_known:
        steps.append({"step": "daemon_reload", **run_cmd(["systemctl", "daemon-reload"], timeout=20)})
    else:
        steps.append({"step": "daemon_reload", "ok": True, "noop": True, "message": "Kein daemon-reload noetig; es wurde keine Unit entfernt."})
    post_diag = diagnose_module(module.key).get(module.key, {})
    ok = all(step.get("ok", False) for step in steps[1:]) and not (post_diag.get("systemd") or {}).get("exists", False)
    already_absent = not unit_known and not unit_path.exists()
    return {
        "success": ok,
        "module": module.public_dict(),
        "steps": steps,
        "diagnosis": post_diag,
        "backup_snapshot": backup_snapshot,
        "rollback_plan": [
            f"Unit aus Backup-Snapshot wiederherstellen: {backup_snapshot.get('root')}/system/etc/systemd/system/{module.service_unit}",
            "systemctl daemon-reload ausführen",
            f"systemctl enable --now {module.service_unit} ausführen",
            "Dienststatus, Log und Alive-Datei erneut prüfen",
        ],
        "message": (
            "Optionales Modul war bereits zurueckgebaut; keine Aenderung noetig."
            if ok and already_absent
            else ("Optionales Modul wurde kontrolliert zurückgebaut." if ok else "Rückbau wurde ausgeführt, aber Nachprüfung meldet Fehler.")
        ),
    }


def check_path(path: Path, expected_group: str | None = "www-data", should_write: bool = False) -> dict[str, Any]:
    exists = path.exists()
    meta = owner_group(path) if exists else {"owner": None, "group": None, "mode": None}
    writable = os.access(path, os.W_OK) if exists else False
    issue = None
    if not exists:
        issue = "fehlt"
    elif expected_group and meta.get("group") != expected_group:
        issue = f"Gruppe ist {meta.get('group')}, erwartet {expected_group}"
    elif should_write and not writable:
        issue = "nicht schreibbar für aktuellen Web-Installer-Kontext"
    return {
        "path": str(path),
        "exists": exists,
        **meta,
        "writable": writable,
        "ok": issue is None,
        "issue": issue,
    }


def permissions_check() -> dict[str, Any]:
    paths = [
        (WEB_ROOT, "www-data", True),
        (TMP_DIR, "www-data", True),
        (RAMDISK_DIR, "www-data", True),
        (LOG_DIR, "www-data", True),
        (DATA_DIR, "www-data", True),
        (INSTALL_ROOT, "www-data", False),
        (INSTALLER_DIR, "www-data", False),
        (INSTALLER_DIR / "service_wrapper.sh", "www-data", False),
        (INSTALLER_DIR / "installer_wrapper.sh", "www-data", False),
        (CONFIG_FILE, "www-data", True),
    ]
    checks = [check_path(path, group, write) for path, group, write in paths]
    for session_file in SESSION_FILES:
        if session_file.exists():
            checks.append(check_path(session_file, "www-data", True))
    issues = [item for item in checks if not item["ok"]]
    return {
        "success": True,
        "write_actions_enabled": WRITE_ACTIONS_ENABLED,
        "summary": "Rechteprüfung abgeschlossen. Es wurden keine Änderungen ausgeführt.",
        "checks": checks,
        "issue_count": len(issues),
        "issues": issues,
        "repair_available": False,
        "repair_message": "Rechte-Reparatur ist als WebUI-Aktion vorbereitet, aber noch nicht freigeschaltet.",
    }


def repair_runtime_permissions(user: str) -> list[dict[str, Any]]:
    """Repair shared WebUI/runtime paths used by PHP and Python services."""
    steps: list[dict[str, Any]] = []
    runtime_dirs = [
        (WEB_ROOT, 0o775),
        (TMP_DIR, 0o775),
        (RAMDISK_DIR, 0o2775),
        (LOG_DIR, 0o775),
        (DATA_DIR, 0o775),
    ]
    for path, mode in runtime_dirs:
        try:
            path.mkdir(parents=True, exist_ok=True)
            shutil.chown(path, user=user, group="www-data")
            os.chmod(path, mode)
            steps.append({
                "step": "repair_runtime_dir",
                "ok": True,
                "path": str(path),
                "owner": f"{user}:www-data",
                "mode": oct(mode)[2:],
            })
        except Exception as exc:
            steps.append({
                "step": "repair_runtime_dir",
                "ok": False,
                "path": str(path),
                "error": str(exc),
            })

    for path in SESSION_FILES:
        if not path.exists():
            continue
        try:
            shutil.chown(path, user=user, group="www-data")
            os.chmod(path, 0o664)
            steps.append({
                "step": "repair_session_file",
                "ok": True,
                "path": str(path),
                "owner": f"{user}:www-data",
                "mode": "664",
            })
        except Exception as exc:
            steps.append({
                "step": "repair_session_file",
                "ok": False,
                "path": str(path),
                "error": str(exc),
            })
    return steps


def repair_permissions(*, repair_runtime: bool = True) -> dict[str, Any]:
    """Write-mode only: replace sudoers with the wrapper-only target."""
    readiness = write_readiness()
    if is_docker():
        return {
            "success": False,
            "message": "Rechte-Reparatur abgebrochen: Docker-Systeme nutzen keinen systemd/sudoers-Bare-Metal-Pfad.",
            "readiness": readiness,
        }

    wrapper_preview = wrapper_integrity_preview()
    if not wrapper_preview.get("success"):
        return {
            "success": False,
            "message": "Rechte-Reparatur fail-closed abgebrochen: Wrapper stimmen nicht sicher mit dem lokalen Git-HEAD überein.",
            "wrapper_integrity": wrapper_preview,
            "readiness": readiness,
        }

    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = SUDOERS_FILE.with_name(f"{SUDOERS_FILE.name}.bak-webinstaller-{timestamp}")
    target_content = desired_sudoers_content()
    tmp_path = SUDOERS_FILE.with_name(f"{SUDOERS_FILE.name}.tmp-webinstaller-{timestamp}")
    steps: list[dict[str, Any]] = []
    findings = sudoers_file_findings()

    try:
        backup_snapshot = create_backup_snapshot("repair_permissions", None)
        if not backup_snapshot.get("success"):
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Backup-Snapshot konnte nicht angelegt werden.",
                "steps": steps,
                "backup_snapshot": backup_snapshot,
                "readiness": readiness,
            }
        steps.append({
            "step": "backup_snapshot",
            "ok": True,
            "path": backup_snapshot.get("root"),
            "copied_count": backup_snapshot.get("copied_count", 0),
        })

        backup_coverage = validate_wrapper_backup_coverage(wrapper_preview, backup_snapshot)
        steps.append({
            "step": "validate_wrapper_backup_coverage",
            "ok": bool(backup_coverage.get("success")),
            "checks": backup_coverage.get("checks", []),
        })
        if not backup_coverage.get("success"):
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Wrapper-Preimages sind im Snapshot nicht vollständig gebunden.",
                "steps": steps,
                "wrapper_integrity": wrapper_preview,
                "backup_snapshot": backup_snapshot,
                "backup_coverage": backup_coverage,
                "readiness": readiness,
            }

        user = install_user()
        wrapper_repair = repair_wrapper_integrity(user=user, group="www-data")
        steps.extend(wrapper_repair.get("steps", []))
        if not wrapper_repair.get("success"):
            return {
                "success": False,
                "message": wrapper_repair.get("message") or "Wrapper-Reparatur fehlgeschlagen.",
                "steps": steps,
                "wrapper_integrity": wrapper_repair,
                "backup_snapshot": backup_snapshot,
                "readiness": readiness,
            }

        if repair_runtime:
            steps.extend(repair_runtime_permissions(user))
        wrapper_endgate = wrapper_integrity_preview()
        wrapper_endgate_ok = (
            wrapper_endgate.get("success")
            and wrapper_endgate.get("head") == wrapper_repair.get("head")
            and not wrapper_endgate.get("repair_needed")
        )
        steps.append({
            "step": "validate_wrapper_integrity_before_sudoers",
            "ok": bool(wrapper_endgate_ok),
            "head": wrapper_endgate.get("head"),
        })
        if not wrapper_endgate_ok:
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Wrapper-Endgate vor sudoers ist nicht grün.",
                "steps": steps,
                "wrapper_integrity": wrapper_endgate,
                "backup_snapshot": backup_snapshot,
                "readiness": readiness,
            }

        if SUDOERS_FILE.exists():
            backup_path.write_text(SUDOERS_FILE.read_text(encoding="utf-8", errors="replace"), encoding="utf-8")
            os.chmod(backup_path, 0o440)
            steps.append({"step": "backup_sudoers", "ok": True, "path": str(backup_path)})
        else:
            steps.append({"step": "backup_sudoers", "ok": True, "path": None, "message": "sudoers-Datei war noch nicht vorhanden"})

        tmp_path.write_text(target_content, encoding="utf-8")
        os.chmod(tmp_path, 0o440)
        syntax_tmp = run_cmd(["visudo", "-cf", str(tmp_path)], timeout=10)
        steps.append({"step": "validate_target", **syntax_tmp})
        if not syntax_tmp.get("ok"):
            tmp_path.unlink(missing_ok=True)
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Ziel-sudoers hat die visudo-Pruefung nicht bestanden.",
                "steps": steps,
                "backup": str(backup_path) if backup_path.exists() else None,
            }

        os.replace(tmp_path, SUDOERS_FILE)
        os.chmod(SUDOERS_FILE, 0o440)
        steps.append({"step": "write_sudoers", "ok": True, "path": str(SUDOERS_FILE)})

        target_file = str(SUDOERS_FILE)
        removable_by_file: dict[str, set[tuple[int, str]]] = {}
        for item in findings["direct_web_lines"] + findings["legacy_lines"]:
            if item["file"] == target_file:
                continue
            removable_by_file.setdefault(item["file"], set()).add((int(item["line_no"]), str(item["line"])))

        for file_name, removable in sorted(removable_by_file.items()):
            path = Path(file_name)
            if not path.exists() or not path.is_file():
                steps.append({"step": "clean_legacy_sudoers", "ok": False, "path": file_name, "message": "Datei fehlt"})
                continue
            old_text = path.read_text(encoding="utf-8", errors="replace")
            backup_other = path.with_name(f"{path.name}.bak-webinstaller-{timestamp}")
            backup_other.write_text(old_text, encoding="utf-8")
            os.chmod(backup_other, 0o440)
            kept_lines = []
            removed_count = 0
            for line_no, raw_line in enumerate(old_text.splitlines(), start=1):
                stripped = raw_line.strip()
                if (line_no, stripped) in removable:
                    removed_count += 1
                    continue
                kept_lines.append(raw_line)
            new_text = "\n".join(kept_lines).rstrip() + "\n"
            path.write_text(new_text, encoding="utf-8")
            os.chmod(path, 0o440)
            steps.append({
                "step": "clean_legacy_sudoers",
                "ok": True,
                "path": file_name,
                "backup": str(backup_other),
                "removed_lines": removed_count,
            })

        syntax_final = run_cmd(["visudo", "-cf", str(SUDOERS_FILE)], timeout=10)
        steps.append({"step": "validate_final", **syntax_final})
        syntax_all = run_cmd(["visudo", "-cf", "/etc/sudoers"], timeout=10)
        steps.append({"step": "validate_all_sudoers", **syntax_all})

        # Dieser Abschlusscheck laeuft noch innerhalb des eigenen Job-Locks.
        # Der Lock ist hier kein Fehler, sondern der Schutzrahmen des gerade
        # erfolgreich ausgefuehrten Reparaturjobs.
        post = write_readiness(ignore_active_lock=True)
        return {
            "success": bool(syntax_final.get("ok")) and bool(syntax_all.get("ok")) and post.get("hard_blocker_count", 1) == 0,
            "message": "Rechte-Reparatur abgeschlossen." if syntax_final.get("ok") and syntax_all.get("ok") else "Rechte-Reparatur geschrieben, aber Validierung meldet Fehler.",
            "steps": steps,
            "backup": str(backup_path) if backup_path.exists() else None,
            "backup_snapshot": backup_snapshot,
            "wrapper_integrity": wrapper_endgate,
            "readiness": post,
            "rollback_plan": [
                f"Backup zurueckspielen: {backup_path}",
                f"visudo -cf {SUDOERS_FILE} ausfuehren",
                "Freigabe-Check erneut starten",
            ],
        }
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


def load_job_file() -> dict[str, Any]:
    try:
        return json.loads(JOB_FILE.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise RuntimeError(f"Jobdatei nicht lesbar: {JOB_FILE} ({exc})") from exc


def job_progress_steps(job: dict[str, Any], phase: str = "running") -> list[dict[str, Any]]:
    """Creates a compact, user-facing progress model for WebUI job status."""
    action = str(job.get("action", "")).strip()
    normalized_action = ACTION_ALIASES.get(action, action)
    module_key = job.get("module")
    is_read_only = normalized_action in READ_ONLY_ACTIONS
    is_write = normalized_action in WRITE_ACTION_NAMES or normalized_action in SERVICE_ACTIONS

    labels = [
        "Jobdatei aus der Ramdisk lesen",
        "Aktion und Modul gegen Allowlist prüfen",
        "Schreibschutz und Sicherheitsmodus prüfen",
    ]
    if module_key:
        labels.append(f"Modulkontext laden: {module_key}")
    if normalized_action in {"install_module_dry_run", "install_module", "remove_module"}:
        labels.extend([
            "Konfiguration und Abhängigkeiten prüfen",
            "Service-Datei, Script, Log und Alive-Datei prüfen",
        ])
    elif normalized_action in {"permissions_check", "repair_permissions_dry_run", "repair_permissions"}:
        labels.append("Projektpfade und Wrapper-Rechte prüfen")
    elif normalized_action in {"dry_run", "diagnose", "run_diagnosis", "status"}:
        labels.append("Diagnosedaten und Alive-Dateien prüfen")
    elif normalized_action == "write_readiness":
        labels.append("Wrapper, sudoers und Freigabeblocker prüfen")

    if normalized_action == "write_permission_plan":
        labels.append("sudoers-Zielbild und sicheren Rückweg berechnen")
    elif normalized_action == "backup_plan":
        labels.append("Backup-Umfang und Rückweg berechnen")

    if is_write:
        labels.append("Schreibjob nur bei expliziter Freigabe ausführen")
    else:
        labels.append("Read-only Ergebnis berechnen")
    labels.append("Statusdatei für die WebUI aktualisieren")

    if phase == "running":
        done_until = 2
        if is_read_only:
            done_until = min(3, len(labels) - 1)
        return [
            {
                "label": label,
                "state": "done" if idx < done_until else ("running" if idx == done_until else "pending"),
                "read_only": is_read_only,
            }
            for idx, label in enumerate(labels)
        ]

    final_state = "blocked" if phase == "blocked" else ("error" if phase == "error" else "done")
    return [
        {
            "label": label,
            "state": final_state if idx == len(labels) - 1 and final_state != "done" else "done",
            "read_only": is_read_only,
        }
        for idx, label in enumerate(labels)
    ]


def execute_job(job: dict[str, Any]) -> dict[str, Any]:
    action = str(job.get("action", "")).strip()
    normalized_action = ACTION_ALIASES.get(action, action)
    module_key = job.get("module")

    if action not in ALLOWED_JOB_TYPES:
        raise RuntimeError(f"Nicht erlaubter Job-Typ: {action}")

    if normalized_action in SERVICE_ACTIONS or normalized_action in {"repair_permissions", "run_update", "install_module", "remove_module", "install_missing_packages"}:
        if not WRITE_ACTIONS_ENABLED:
            return {
                "success": False,
                "action": action,
                "write_blocked": True,
                "message": "Schreibende Web-Installer-Aktionen sind noch nicht freigeschaltet.",
            }

    if normalized_action == "catalog":
        return {"success": True, "modules": {m.key: m.public_dict() for m in iter_modules()}}
    if normalized_action == "installer_status":
        return installer_status()
    if normalized_action == "job_status":
        return job_status()
    if normalized_action == "write_readiness":
        return write_readiness()
    if normalized_action == "write_permission_plan":
        return write_permission_plan()
    if normalized_action == "backup_plan":
        return backup_plan(module_key)
    if normalized_action in {"status", "diagnose", "run_diagnosis"}:
        return {"success": True, "diagnosis": diagnose_module(module_key)}
    if normalized_action == "dry_run":
        return {"success": True, "dry_run": module_dry_run(module_key)}
    if normalized_action == "install_module_dry_run":
        return {"success": True, "install_dry_run": module_install_dry_run(module_key)}
    if normalized_action == "permissions_check":
        return permissions_check()
    if normalized_action == "update_check":
        return update_check()
    if normalized_action == "repair_permissions_dry_run":
        plan = write_permission_plan()
        return {
            "success": True,
            "write_actions_enabled": WRITE_ACTIONS_ENABLED,
            "summary": "Dry-Run: Rechte-Reparatur würde Projektpfade prüfen und sudoers auf Wrapper-only bereinigen.",
            "would_change": bool(plan.get("would_change")),
            "planned_steps": [
                "Webroot, ramdisk, logs und data auf Besitzer/Gruppe/Rechte prüfen",
                "Installationsordner und Installer-Skripte auf www-data-Gruppe prüfen",
                "sudoers-Wrapper für service_wrapper.sh und installer_wrapper.sh prüfen",
                "Direkte systemctl-Freigaben aus der WebUI-sudoers entfernen",
                "Ziel-sudoers mit visudo prüfen, bevor sie aktiv wird",
                "Keine Änderung ohne explizite Freischaltung und separaten Reparatur-Job",
            ],
            "permissions": permissions_check(),
            "sudoers_plan": plan,
        }
    if normalized_action == "validate_config":
        return {"success": True, "validation": validate_config(module_key)}
    if normalized_action == "repair_permissions":
        return repair_permissions()
    if normalized_action in SERVICE_ACTIONS:
        return control_service(module_key, normalized_action)
    if normalized_action == "install_module":
        return install_module(module_key)
    if normalized_action == "remove_module":
        return remove_module(module_key)

    return {
        "success": False,
        "action": action,
        "message": "Aktion ist bekannt, aber im Scaffold noch nicht implementiert.",
    }


def run_once(job: dict[str, Any] | None = None, record_status: bool = True) -> dict[str, Any]:
    job = job or load_job_file()
    action = str(job.get("action", "")).strip()
    normalized_action = ACTION_ALIASES.get(action, action)
    if normalized_action in PASSIVE_STATUS_ACTIONS or not record_status:
        return execute_job(job)
    if normalized_action in READ_ONLY_ACTIONS:
        write_status({"state": "running", "job": job, "read_only": True, "progress_steps": job_progress_steps(job, "running")})
        try:
            result = execute_job(job)
            state = "done" if result.get("success") else "error"
            write_status({
                "state": state,
                "job": job,
                "read_only": True,
                "progress_steps": job_progress_steps(job, state),
                "result": result,
            })
            return result
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
            write_status({
                "state": "error",
                "job": job,
                "read_only": True,
                "progress_steps": job_progress_steps(job, "error"),
                "result": result,
            })
            return result

    with job_lock():
        write_status({"state": "running", "job": job, "progress_steps": job_progress_steps(job, "running")})
        log(f"Starte Job: {job}")
        try:
            result = execute_job(job)
            state = "done" if result.get("success") else "blocked"
            write_status({"state": state, "job": job, "progress_steps": job_progress_steps(job, state), "result": result})
            log(f"Job abgeschlossen: {result}")
            return result
        except Exception as exc:
            result = {"success": False, "error": str(exc)}
            write_status({"state": "error", "job": job, "progress_steps": job_progress_steps(job, "error"), "result": result})
            log(f"Job fehlgeschlagen: {exc}")
            return result


def main() -> int:
    configure_utf8_stdio()
    parser = argparse.ArgumentParser(description="E3DC-Control Web Installer scaffold")
    parser.add_argument("--job-file", action="store_true", help="Job aus der Ramdisk lesen")
    parser.add_argument("--action", default="run_diagnosis", help="Aktion für direkten Testlauf")
    parser.add_argument("--module", default=None, help="Optionaler Modul-Key")
    args = parser.parse_args()

    job = None if args.job_file else {"action": args.action, "module": args.module}
    result = run_once(job, record_status=args.job_file)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
