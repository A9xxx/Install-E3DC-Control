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
import re
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


def _reject_privileged_web_invocation() -> None:
    """Sperrt alte direkte sudoers-Freigaben vor produktiven Imports."""
    sudo_user = str(os.environ.get("SUDO_USER") or "").strip()
    if os.geteuid() == 0 and sudo_user == "www-data":
        print(
            json.dumps(
                {
                    "success": False,
                    "write_blocked": True,
                    "message": (
                        "Sicherheitssperre: web_installer.py darf nicht "
                        "privilegiert aus dem Webserverkontext gestartet werden."
                    ),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        raise SystemExit(126)


_reject_privileged_web_invocation()

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
        config_secret_dir_mode,
        config_secret_file_mode,
    )
    from .service_catalog import READ_ACTIONS, SERVICE_ACTIONS, get_module, iter_modules
    from .installer_config import get_install_path
    from .release_version import stable_update_check
    from .git_commit_reader import (
        read_commit_entries,
        repository_git_reader_user,
        run_isolated_git,
    )
    from .utils import MANAGER_LOCK_TMPFILES_CONFIG, ensure_manager_lock_namespace
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
        config_secret_dir_mode,
        config_secret_file_mode,
    )
    from service_catalog import READ_ACTIONS, SERVICE_ACTIONS, get_module, iter_modules
    # installer_config besitzt absichtlich Paketimporte. Beim direkten
    # Skriptaufruf wird deshalb nur für diesen Import der Produktroot als
    # Paketwurzel ergänzt; alle übrigen Legacy-Fallbacks bleiben unverändert.
    package_root = str(Path(__file__).resolve().parent.parent)
    if package_root not in sys.path:
        sys.path.insert(0, package_root)
    from Installer.installer_config import get_install_path
    from Installer.release_version import stable_update_check
    from Installer.git_commit_reader import (
        read_commit_entries,
        repository_git_reader_user,
        run_isolated_git,
    )
    from Installer.utils import MANAGER_LOCK_TMPFILES_CONFIG, ensure_manager_lock_namespace


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

# Der Web-Installer kann während eines commitgebundenen Release-Wechsels aus
# einem getrennten Ausführungssnapshot importiert werden. Wrapper, Backups,
# sudoers und Produktprüfungen müssen stets den explizit gebundenen
# Installationsbaum verwenden.
INSTALL_ROOT = Path(get_install_path()).resolve()
INSTALLER_DIR = INSTALL_ROOT / "Installer"
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

SERVICE_WRAPPER_SOURCE = INSTALLER_DIR / "service_wrapper.sh"
SERVICE_WRAPPER = Path("/usr/local/sbin/e3dc-service-control")
WEB_UPDATE_LAUNCHER_SOURCE = INSTALLER_DIR / "web_update_launcher.sh"
WEB_UPDATE_LAUNCHER = Path("/usr/local/sbin/e3dc-web-update-launcher")
WEB_UPDATE_DISPATCHER_CONTRACT = b'e3dc-download-bootstrap-v2'
SERVICE_WRAPPER_ACTIONS = (
    "start",
    "stop",
    "restart",
    "status",
    "enable",
    "disable",
)
SERVICE_WRAPPER_UNITS = (
    "e3dc-live.service",
    "energy_manager.service",
    "e3dc-wallbox-manager.service",
    "e3dc-epex-manager.service",
    "e3dc-weather-manager.service",
    "e3dc-storage-simulator.service",
    "e3dc-storage-manager.service",
    "e3dc-ha.service",
    "e3dc-matter-bridge.service",
    "e3dc-bluelink.service",
    "e3dc-lux-live.service",
    "e3dc-idm-live.service",
    "e3dc-stiebel-live.service",
    "e3dc-dimplex-live.service",
    "e3dc-heizstab.service",
    "e3dc-climate-live.service",
    "e3dc-climate-control.service",
    "e3dc-forecast-evidence.service",
    "e3dc-notifier.service",
    "e3dc-mqtt-hub.service",
    "e3dc-websocket.service",
    "e3dc-shadow-sync.service",
)
INSTALLER_WRAPPER = INSTALLER_DIR / "installer_wrapper.sh"
WRAPPER_RELATIVE_PATHS = (
    "Installer/service_wrapper.sh",
    "Installer/installer_wrapper.sh",
    "Installer/web_update_launcher.sh",
)
SUDOERS_FILE = Path("/etc/sudoers.d/020_e3dc_services")
SUDOERS_DIR = Path("/etc/sudoers.d")
VISUDO = Path("/usr/sbin/visudo")
MAX_BACKUP_FILE_BYTES = 10 * 1024 * 1024
EXPECTED_RELEASE_COMMIT_ENV = "E3DC_RELEASE_EXPECTED_COMMIT"


def _git_head_wrapper_bytes(repo_root: Path) -> tuple[str, dict[str, bytes]]:
    """Liest die freigegebenen Wrapperbytes direkt aus dem lokalen Git-HEAD."""
    root = Path(repo_root)
    reader_user = repository_git_reader_user(root)
    try:
        head_result = run_isolated_git(
            root,
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
            run_as_user=reader_user,
            timeout=10,
        )
    except Exception as exc:
        raise RuntimeError(f"Lokaler Git-HEAD konnte nicht gebunden werden: {exc}") from exc
    head = bytes(head_result.stdout or b"").decode("ascii", errors="replace").strip().lower()
    if head_result.returncode != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
        error = bytes(head_result.stderr or b"").decode("utf-8", errors="replace").strip() or "ungültige HEAD-Antwort"
        raise RuntimeError(f"Lokaler Git-HEAD konnte nicht gebunden werden: {error}")
    expected = str(os.environ.get(EXPECTED_RELEASE_COMMIT_ENV) or "").strip().lower()
    if expected:
        if not re.fullmatch(r"[0-9a-f]{40}", expected) or head != expected:
            raise RuntimeError("Lokaler Git-HEAD weicht vom gebundenen Release-Commit ab")
        commit = expected
    else:
        commit = head

    try:
        entries = read_commit_entries(
            root,
            commit,
            WRAPPER_RELATIVE_PATHS,
            required_paths=WRAPPER_RELATIVE_PATHS,
            run_as_user=reader_user,
            maximum_files=len(WRAPPER_RELATIVE_PATHS),
            maximum_file_bytes=1024 * 1024,
            maximum_total_bytes=3 * 1024 * 1024,
        )
    except Exception as exc:
        raise RuntimeError(f"HEAD-Wrapper konnten nicht gebunden werden: {exc}") from exc
    canonical: dict[str, bytes] = {}
    for relative_path in WRAPPER_RELATIVE_PATHS:
        payload, sealed_mode = entries[relative_path]
        if sealed_mode != 0o555 or not payload.startswith(b"#!/bin/bash\n") or b"\r" in payload:
            raise RuntimeError(f"HEAD-Blob ist kein LF-kodierter Bash-Wrapper: {relative_path}")
        canonical[relative_path] = payload
    return commit, canonical


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
        if stat.S_IMODE(metadata.st_mode) == 0o755:
            item.update({"status": "ok", "repairable": True, "needs_repair": False})
        else:
            item.update({"status": "mode_drift", "repairable": True})
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


def _atomic_write_wrapper(
    path: Path,
    payload: bytes,
    user: str | None,
    group: str | None,
    preimage: dict[str, Any] | None = None,
) -> None:
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
        if preimage is not None:
            # Das beim Snapshot gebundene Objekt erst am Commitpunkt erneut
            # prüfen. Ein Rebind vor dem Temp-Schreiben lässt sonst ein
            # Zeitfenster, in dem eine legitime Fremdänderung verloren geht.
            _assert_preimage_unchanged(preimage)
        os.replace(tmp_path, path)
        if preimage is not None:
            _bind_transaction_output(preimage)
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


def _set_verified_wrapper_permissions(
    path: Path,
    canonical: bytes,
    user: str | None,
    group: str | None,
    preimage: dict[str, Any] | None = None,
) -> None:
    if preimage is not None:
        _assert_preimage_unchanged(preimage)
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
        opened_payload = b"".join(chunks)
        if opened_payload != canonical:
            raise RuntimeError(f"Wrapperbytes änderten sich beim Rechte-Endgate: {path}")
        if preimage is not None and (
            not preimage.get("existed")
            or metadata.st_dev != int(preimage.get("dev", -1))
            or metadata.st_ino != int(preimage.get("ino", -1))
            or metadata.st_uid != int(preimage.get("uid", -1))
            or metadata.st_gid != int(preimage.get("gid", -1))
            or stat.S_IMODE(metadata.st_mode) != int(preimage.get("mode", -1))
            or opened_payload != bytes(preimage.get("payload") or b"")
        ):
            raise RuntimeError(f"Wrapper-Preimage driftete vor dem Rechte-Commit: {path}")
        uid, gid = _wrapper_owner_ids(user, group)
        if uid != -1 or gid != -1:
            os.fchown(fd, uid, gid)
        os.fchmod(fd, 0o755)
        os.fsync(fd)
        if preimage is not None:
            _bind_transaction_output(preimage)
    finally:
        os.close(fd)


def _capture_file_preimage(path: Path) -> dict[str, Any]:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return {"path": str(path), "existed": False}
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise RuntimeError(f"Preimage ist nicht regulär/nlink=1: {path}")
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            raise RuntimeError(f"Preimage wurde beim Öffnen ausgetauscht: {path}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        before_identity = (opened.st_dev, opened.st_ino, opened.st_mode, opened.st_size, opened.st_mtime_ns)
        after_identity = (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
        if before_identity != after_identity:
            raise RuntimeError(f"Preimage driftete während des Lesens: {path}")
    finally:
        os.close(descriptor)
    return {
        "path": str(path),
        "existed": True,
        "payload": b"".join(chunks),
        "dev": int(metadata.st_dev),
        "ino": int(metadata.st_ino),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": stat.S_IMODE(metadata.st_mode),
    }


def _bind_transaction_output(preimage: dict[str, Any]) -> None:
    """Bindet ein von dieser Transaktion neu erzeugtes Objekt für den Rücklauf."""
    current = _capture_file_preimage(Path(str(preimage["path"])))
    if not current.get("existed"):
        raise RuntimeError(f"Transaktionsausgabe fehlt nach dem Schreiben: {preimage['path']}")
    preimage["transaction_identity"] = {
        "dev": current.get("dev"),
        "ino": current.get("ino"),
        "uid": current.get("uid"),
        "gid": current.get("gid"),
        "mode": current.get("mode"),
        "sha256": hashlib.sha256(bytes(current.get("payload") or b"")).hexdigest(),
    }


def _assert_preimage_unchanged(preimage: dict[str, Any]) -> None:
    """Verhindert, dass ein zwischenzeitlich verändertes sudoers-Objekt überschrieben wird."""
    current = _capture_file_preimage(Path(str(preimage["path"])))
    if bool(current.get("existed")) != bool(preimage.get("existed")):
        raise RuntimeError(f"Transaktionspfad änderte seinen Existenzzustand: {preimage['path']}")
    if not preimage.get("existed"):
        return
    for key in ("dev", "ino", "uid", "gid", "mode", "payload"):
        if current.get(key) != preimage.get(key):
            raise RuntimeError(f"Transaktions-Preimage driftete vor dem Schreiben: {preimage['path']}")


def _assert_transaction_output_unchanged(preimage: dict[str, Any]) -> None:
    """Bindet den Rücklauf an genau das von der Transaktion erzeugte Objekt."""
    path = Path(str(preimage["path"]))
    current = _capture_file_preimage(path)
    identity = preimage.get("transaction_identity")
    if not identity or not current.get("existed"):
        raise RuntimeError(f"Transaktionsausgabe fehlt vor dem Rücklauf: {path}")
    current_identity = {
        "dev": current.get("dev"),
        "ino": current.get("ino"),
        "uid": current.get("uid"),
        "gid": current.get("gid"),
        "mode": current.get("mode"),
        "sha256": hashlib.sha256(bytes(current.get("payload") or b"")).hexdigest(),
    }
    if current_identity != identity:
        raise RuntimeError(f"Transaktionspfad driftete vor dem Rücklauf: {path}")


def _atomic_restore_preimage(preimage: dict[str, Any]) -> None:
    path = Path(str(preimage["path"]))
    if not preimage.get("existed"):
        current = _capture_file_preimage(path)
        if not current.get("existed"):
            return
        identity = preimage.get("transaction_identity")
        if not identity:
            raise RuntimeError(f"Neu erzeugter Pfad besitzt keine Transaktionsbindung: {path}")
        _assert_transaction_output_unchanged(preimage)
        path.unlink()
        directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return

    payload = bytes(preimage["payload"])
    identity = preimage.get("transaction_identity")
    if not identity:
        return
    _assert_transaction_output_unchanged(preimage)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.rollback-", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(fd, payload[offset:])
        os.fchown(fd, int(preimage["uid"]), int(preimage["gid"]))
        os.fchmod(fd, int(preimage["mode"]))
        os.fsync(fd)
        _assert_transaction_output_unchanged(preimage)
        os.replace(tmp_path, path)
        if preimage is not None:
            _bind_transaction_output(preimage)
        directory_fd = os.open(str(path.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(fd)
        tmp_path.unlink(missing_ok=True)

    restored = _capture_file_preimage(path)
    if (
        not restored.get("existed")
        or restored.get("payload") != payload
        or restored.get("uid") != int(preimage["uid"])
        or restored.get("gid") != int(preimage["gid"])
        or restored.get("mode") != int(preimage["mode"])
    ):
        raise RuntimeError(f"Preimage-Rücklauf konnte nicht verifiziert werden: {path}")


def _sudoers_owner_ids() -> tuple[int, int]:
    """Produktiver sudoers-Vertrag; als Funktion separat testbar."""
    return 0, 0


def _atomic_write_sudoers(
    path: Path,
    payload: bytes,
    preimage: dict[str, Any] | None = None,
) -> None:
    """Schreibt ein sudoers-Fragment same-FS, root:root/0440 und dauerhaft."""
    parent = path.parent
    parent_meta = os.lstat(parent)
    if stat.S_ISLNK(parent_meta.st_mode) or not stat.S_ISDIR(parent_meta.st_mode):
        raise RuntimeError(f"sudoers-Elternpfad ist kein echtes Verzeichnis: {parent}")
    descriptor, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.txn-", dir=str(parent))
    tmp_path = Path(tmp_name)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        owner_uid, owner_gid = _sudoers_owner_ids()
        os.fchown(descriptor, owner_uid, owner_gid)
        os.fchmod(descriptor, 0o440)
        os.fsync(descriptor)
        if preimage is not None:
            # Rebind direkt vor dem einzigen sichtbaren Commit. Der Aufrufer
            # darf damit keinen Stand überschreiben, der nach dem Snapshot
            # von einem anderen Prozess atomar publiziert wurde.
            _assert_preimage_unchanged(preimage)
        os.replace(tmp_path, path)
        if preimage is not None:
            _bind_transaction_output(preimage)
        directory_fd = os.open(str(parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(descriptor)
        tmp_path.unlink(missing_ok=True)


def _atomic_write_root_launcher(
    path: Path,
    payload: bytes,
    preimage: dict[str, Any] | None = None,
    *,
    label: str,
) -> None:
    """Installiert einen eng gebundenen Web-sudo-Aktor root-eigen und race-frei."""

    parent = path.parent
    for ancestor in (parent.parent, parent):
        try:
            ancestor_meta = os.lstat(ancestor)
        except OSError as exc:
            raise RuntimeError(
                f"{label}-Elternpfad ist nicht prüfbar: {ancestor}"
            ) from exc
        if (
            stat.S_ISLNK(ancestor_meta.st_mode)
            or not stat.S_ISDIR(ancestor_meta.st_mode)
            or ancestor_meta.st_uid != 0
            or ancestor_meta.st_gid != 0
            or ancestor_meta.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                f"{label}-Elternpfad ist nicht root-kontrolliert: {ancestor}"
            )
    if (
        not payload.startswith(b"#!/bin/bash\n")
        or b"\r" in payload
        or len(payload) > 64 * 1024
    ):
        raise RuntimeError(f"{label}-Quelle ist nicht zulässig")

    descriptor, tmp_name = tempfile.mkstemp(
        prefix=f".{path.name}.txn-",
        dir=str(parent),
    )
    tmp_path = Path(tmp_name)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o755)
        os.fsync(descriptor)
        if preimage is not None:
            _assert_preimage_unchanged(preimage)
        os.replace(tmp_path, path)
        if preimage is not None:
            _bind_transaction_output(preimage)
        directory_fd = os.open(
            str(parent),
            os.O_RDONLY | getattr(os, "O_DIRECTORY", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        os.close(descriptor)
        tmp_path.unlink(missing_ok=True)


def _atomic_write_service_launcher(
    payload: bytes,
    preimage: dict[str, Any] | None = None,
) -> None:
    _atomic_write_root_launcher(
        SERVICE_WRAPPER,
        payload,
        preimage,
        label="Service-Launcher",
    )


def _render_web_update_launcher(
    template: bytes,
    *,
    root: Path,
    user: str,
) -> bytes:
    """Bindet genau einen kanonischen Installationspfad und Benutzer ein."""

    root_text = str(root)
    user_text = str(user or "")
    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        raise RuntimeError(
            "Web-Update-Launcher besitzt keinen vorhandenen Installationspfad"
        ) from exc
    if (
        not root.is_absolute()
        or any(part in {".", ".."} for part in root.parts)
        or resolved_root != root
        or any(character in root_text for character in ("\x00", "\t", "\r", "\n"))
        or root_text.endswith("/")
        or user_text != user_text.strip()
        or not user_text
        or "/" in user_text
        or "\\" in user_text
        or not all(
            character.isalnum() or character in {"_", "-", "."}
            for character in user_text
        )
        or user_text in {"root", "www-data"}
    ):
        raise RuntimeError("Web-Update-Launcher besitzt keine kanonische Installationsbindung")
    root_marker = b"@E3DC_INSTALL_ROOT@"
    user_marker = b"@E3DC_INSTALL_USER@"
    if (
        template.count(root_marker) != 1
        or template.count(user_marker) != 1
    ):
        raise RuntimeError("Web-Update-Launcher-Vorlage besitzt keinen eindeutigen Platzhaltervertrag")
    def shell_literal(value: str) -> bytes:
        # Ausschließlich statische Single-Quote-Segmente erzeugen. Damit
        # bleiben Leerzeichen, Unicode, Dollar, Backticks und einfache
        # Anführungszeichen reine Daten und können beim Root-Start niemals
        # Shellsyntax werden.
        encoded = "'" + value.replace("'", "'\"'\"'") + "'"
        return encoded.encode("utf-8")

    rendered = template.replace(root_marker, shell_literal(root_text))
    rendered = rendered.replace(user_marker, shell_literal(user_text))
    if root_marker in rendered or user_marker in rendered:
        raise RuntimeError("Web-Update-Launcher-Vorlage blieb unvollständig")
    return rendered


def web_update_launcher_integrity_preview(
    *,
    expected_payload: bytes | None = None,
) -> dict[str, Any]:
    """Prüft Root-Vertrag und Dispatcherkennung ohne Produkt-Git-Abhängigkeit."""

    item: dict[str, Any] = {
        "path": str(WEB_UPDATE_LAUNCHER),
        "status": "unknown",
        "needs_repair": True,
    }
    try:
        metadata = os.lstat(WEB_UPDATE_LAUNCHER)
        item.update({
            "uid": int(metadata.st_uid),
            "gid": int(metadata.st_gid),
            "mode": f"{stat.S_IMODE(metadata.st_mode):04o}",
            "nlink": int(metadata.st_nlink),
        })
        if stat.S_ISLNK(metadata.st_mode):
            item["status"] = "symlink"
        elif not stat.S_ISREG(metadata.st_mode):
            item["status"] = "not_regular"
        elif metadata.st_nlink != 1:
            item["status"] = "hardlink"
        elif (
            metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or stat.S_IMODE(metadata.st_mode) & 0o500 != 0o500
        ):
            item["status"] = "unsafe_permissions"
        elif metadata.st_size < 512 or metadata.st_size > 64 * 1024:
            item["status"] = "size_invalid"
        else:
            flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
            descriptor = os.open(WEB_UPDATE_LAUNCHER, flags)
            try:
                bound = os.fstat(descriptor)
                payload = b""
                while len(payload) <= 64 * 1024:
                    chunk = os.read(descriptor, 64 * 1024 + 1 - len(payload))
                    if not chunk:
                        break
                    payload += chunk
                rebound = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            named_rebound = os.stat(WEB_UPDATE_LAUNCHER, follow_symlinks=False)
            item["sha256"] = hashlib.sha256(payload).hexdigest()
            stable = (
                bound.st_dev == metadata.st_dev
                and bound.st_ino == metadata.st_ino
                and bound.st_size == metadata.st_size
                and bound.st_mtime_ns == metadata.st_mtime_ns
                and rebound.st_dev == bound.st_dev
                and rebound.st_ino == bound.st_ino
                and rebound.st_size == bound.st_size
                and rebound.st_mtime_ns == bound.st_mtime_ns
                and named_rebound.st_dev == rebound.st_dev
                and named_rebound.st_ino == rebound.st_ino
                and named_rebound.st_size == rebound.st_size
                and named_rebound.st_mtime_ns == rebound.st_mtime_ns
            )
            content_ok = (
                stable
                and payload.startswith(b"#!/bin/bash\n")
                and b"\r" not in payload
                and payload.count(WEB_UPDATE_DISPATCHER_CONTRACT) == 1
                and b"@E3DC_INSTALL_ROOT@" not in payload
                and b"@E3DC_INSTALL_USER@" not in payload
                and (expected_payload is None or payload == expected_payload)
            )
            item["status"] = "ok" if content_ok else "content_invalid"
            item["needs_repair"] = not content_ok
    except FileNotFoundError:
        item["status"] = "missing"
    except Exception as exc:
        item.update({"status": "read_error", "error": str(exc)})

    parent_checks = []
    for parent in (WEB_UPDATE_LAUNCHER.parent.parent, WEB_UPDATE_LAUNCHER.parent):
        try:
            metadata = os.lstat(parent)
            ok = (
                stat.S_ISDIR(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
                and metadata.st_uid == 0
                and metadata.st_gid == 0
                and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
        except OSError:
            ok = False
        parent_checks.append({"path": str(parent), "ok": ok})
    success = bool(
        item.get("uid") == 0
        and item.get("nlink") == 1
        and item.get("status") == "ok"
        and all(check["ok"] for check in parent_checks)
    )
    return {
        "success": success,
        "path": str(WEB_UPDATE_LAUNCHER),
        "source": str(WEB_UPDATE_LAUNCHER_SOURCE),
        "status": "ok" if success else item.get("status", "invalid"),
        "item": item,
        "parent_checks": parent_checks,
    }


def service_launcher_integrity_preview() -> dict[str, Any]:
    """Prüft Quelle aus Git-HEAD und installierten root-eigenen Launcher."""

    try:
        head, canonical = _git_head_wrapper_bytes(INSTALL_ROOT)
        payload = canonical["Installer/service_wrapper.sh"]
    except Exception as exc:
        return {
            "success": False,
            "path": str(SERVICE_WRAPPER),
            "status": "head_error",
            "error": str(exc),
        }
    item = _classify_wrapper(SERVICE_WRAPPER, payload)
    parent_checks = []
    for parent in (SERVICE_WRAPPER.parent.parent, SERVICE_WRAPPER.parent):
        try:
            metadata = os.lstat(parent)
            ok = (
                stat.S_ISDIR(metadata.st_mode)
                and not stat.S_ISLNK(metadata.st_mode)
                and metadata.st_uid == 0
                and metadata.st_gid == 0
                and not metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
            )
        except OSError:
            ok = False
        parent_checks.append({"path": str(parent), "ok": ok})
    owner_ok = (
        item.get("uid") == 0
        and item.get("gid") == 0
        and item.get("status") == "ok"
    )
    parents_ok = all(check["ok"] for check in parent_checks)
    success = owner_ok and parents_ok
    return {
        "success": success,
        "path": str(SERVICE_WRAPPER),
        "source": str(SERVICE_WRAPPER_SOURCE),
        "head": head,
        "status": (
            "ok"
            if success
            else ("unsafe_parent" if not parents_ok else item.get("status", "invalid"))
        ),
        "item": item,
        "parent_checks": parent_checks,
    }


def _restore_preimages(preimages: list[dict[str, Any]]) -> dict[str, Any]:
    restored = []
    errors = []
    for preimage in reversed(preimages):
        try:
            _atomic_restore_preimage(preimage)
            restored.append(str(preimage.get("path")))
        except Exception as exc:
            errors.append({"path": str(preimage.get("path")), "error": str(exc)})
    return {"success": not errors, "restored": restored, "errors": errors}


def repair_wrapper_integrity(
    repo_root: Path | str | None = None,
    user: str | None = None,
    group: str | None = None,
    bound_preimages: list[dict[str, Any]] | None = None,
    rollback_on_failure: bool = True,
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
        expected_paths = {str(root / str(item["relative_path"])) for item in state["items"]}
        if bound_preimages is None:
            preimages = [
                _capture_file_preimage(root / str(item["relative_path"]))
                for item in state["items"]
            ]
        else:
            preimages = bound_preimages
            if {str(item.get("path") or "") for item in preimages} != expected_paths:
                raise RuntimeError("Gebundene Wrapper-Preimages stimmen nicht mit der Mutationsmenge überein")
            for preimage in preimages:
                _assert_preimage_unchanged(preimage)
        preimages_by_path = {str(item["path"]): item for item in preimages}
    except Exception as exc:
        public_state.update({
            "success": False,
            "message": f"Wrapper-Reparatur abgebrochen: Preimages konnten nicht gebunden werden: {exc}",
            "steps": [],
        })
        return public_state
    try:
        for item in state["items"]:
            relative_path = str(item["relative_path"])
            path = root / relative_path
            canonical = state["canonical"][relative_path]
            current = _classify_wrapper(path, canonical)
            if current.get("status") != item.get("status") or current.get("actual_sha256") != item.get("actual_sha256"):
                raise RuntimeError(f"Wrapperzustand änderte sich vor dem Schreiben: {path}")

            if item["status"] in {"missing", "crlf_only"}:
                _atomic_write_wrapper(
                    path,
                    canonical,
                    user,
                    group,
                    preimages_by_path[str(path)],
                )
                steps.append({
                    "step": "restore_wrapper_from_head",
                    "ok": True,
                    "path": str(path),
                    "source": f"{state['head']}:{relative_path}",
                    "reason": item["status"],
                    "sha256": hashlib.sha256(canonical).hexdigest(),
                })
            else:
                _set_verified_wrapper_permissions(
                    path,
                    canonical,
                    user,
                    group,
                    preimages_by_path[str(path)],
                )
                steps.append({
                    "step": "verify_wrapper_from_head",
                    "ok": True,
                    "path": str(path),
                    "source": f"{state['head']}:{relative_path}",
                    "sha256": hashlib.sha256(canonical).hexdigest(),
                })
            _bind_transaction_output(preimages_by_path[str(path)])

        final_state = _collect_wrapper_integrity(root)
        final_ok = (
            final_state["success"]
            and final_state["head"] == state["head"]
            and all(item.get("status") == "ok" for item in final_state["items"])
        )
        expected_uid, expected_gid = _wrapper_owner_ids(user, group)
        if expected_uid != -1:
            final_ok = final_ok and all(item.get("uid") == expected_uid for item in final_state["items"])
        if expected_gid != -1:
            final_ok = final_ok and all(item.get("gid") == expected_gid for item in final_state["items"])
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
        rollback = _restore_preimages(preimages) if rollback_on_failure else None
        if rollback is not None:
            steps.append({"step": "wrapper_rollback", "ok": rollback["success"], **rollback})
        return {
            "success": False,
            "message": (
                f"Wrapper-Reparatur abgebrochen und vollständig zurückgerollt: {exc}"
                if rollback is not None and rollback["success"]
                else (
                    f"Wrapper-Reparatur abgebrochen; Rücklauf unvollständig: {exc}"
                    if rollback is not None
                    else f"Wrapper-Reparatur abgebrochen; Rücklauf bleibt bei der äußeren Transaktion: {exc}"
                )
            ),
            "repo_root": str(root),
            "head": state["head"],
            "items": state["items"],
            "hard_blockers": [],
            "repair_needed": True,
            "steps": steps,
            "rollback": rollback,
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


def _write_snapshot_payload(path: Path, payload: bytes) -> None:
    """Schreibt ein privates Snapshot-Artefakt exklusiv und dauerhaft."""
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(
        str(path),
        os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
        0o600,
    )
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def create_bound_preimage_snapshot(
    action: str,
    preimages: list[dict[str, Any]],
    category_by_path: dict[str, str],
) -> dict[str, Any]:
    """Versiegelt exakt die bereits gebundene Mutationsmenge.

    Die Quelle wird bewusst nicht erneut gelesen. Damit sind persistenter
    Snapshot, In-Memory-Rücklauf und die spätere Commitprüfung dieselbe
    Dateigeneration. Fehlende Preimages werden als ``missing`` im Manifest
    festgehalten, damit auch ein ADD-Rücklauf selbsttragend beschrieben ist.
    """
    safe_action = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in action) or "job"
    collection_root = default_backup_root(INSTALL_ROOT) / "web_installer"
    copied: list[dict[str, Any]] = []
    mapped_entries: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    seen: set[str] = set()
    backup_root: Path | None = None

    try:
        collection_root.mkdir(parents=True, exist_ok=True)
        apply_config_backup_dir_permissions(collection_root, install_user=install_user())
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
        backup_root = Path(
            tempfile.mkdtemp(
                prefix=f"{timestamp}-{safe_action}-permissions-",
                dir=str(collection_root),
            )
        )
        apply_config_backup_dir_permissions(backup_root, install_user=install_user())

        for preimage in preimages:
            source = Path(str(preimage.get("path") or ""))
            source_text = str(source)
            if not source.is_absolute() or source_text in seen:
                raise RuntimeError(f"Ungültiger oder doppelter Snapshot-Pfad: {source}")
            seen.add(source_text)
            category = str(category_by_path.get(source_text) or "").strip().lower()
            if not category:
                raise RuntimeError(f"Snapshot-Kategorie fehlt: {source}")

            existed = bool(preimage.get("existed"))
            source_records.append({
                "category": category,
                "source": source_text,
                "present": existed,
                "files": 1 if existed else 0,
                "source_type": "file" if existed else "missing",
                "exclude_top_level": [],
                "exclude_anywhere": [],
                "directories": [],
            })
            if not existed:
                continue

            payload = bytes(preimage.get("payload") or b"")
            for field in ("uid", "gid", "mode"):
                if field not in preimage:
                    raise RuntimeError(f"Snapshot-Metadatum {field} fehlt: {source}")
            archive_relative = Path("recovery") / category / backup_relative_path(source)
            target = backup_root / archive_relative
            _write_snapshot_payload(target, payload)
            digest = hashlib.sha256(payload).hexdigest()
            mapped_entries.append({
                "backup_path": archive_relative.as_posix(),
                "restore_path": source_text,
                "category": category,
                "restore_mode": int(preimage["mode"]),
                "restore_uid": int(preimage["uid"]),
                "restore_gid": int(preimage["gid"]),
            })
            copied.append({
                "path": source_text,
                "backup": str(target),
                "size": len(payload),
                "sha256": digest,
                "uid": int(preimage["uid"]),
                "gid": int(preimage["gid"]),
                "mode": int(preimage["mode"]),
                "category": category,
            })

        if set(seen) != set(category_by_path):
            raise RuntimeError("Snapshot-Kategorien und Preimage-Menge weichen voneinander ab")
        if not copied:
            marker = backup_root / "snapshot.json"
            _write_snapshot_payload(
                marker,
                (
                    json.dumps(
                        {
                            "schema": "web_installer_preimage_snapshot_v1",
                            "action": safe_action,
                            "missing_paths": sorted(seen),
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    )
                    + "\n"
                ).encode("utf-8"),
            )

        secure_backup_tree(backup_root)
        manifest = finalize_backup(
            backup_root,
            mapped_entries,
            source_records,
            kind=WEB_SNAPSHOT_KIND,
            install_root=INSTALL_ROOT,
        )
        retention = prune_backup_dir(
            collection_root,
            keep_count=WEB_INSTALLER_BACKUP_KEEP_COUNT,
            expected_kind=WEB_SNAPSHOT_KIND,
        )
        return {
            "success": True,
            "root": str(backup_root),
            "copied_count": len(copied),
            "skipped_count": 0,
            "copied": copied,
            "manifest": manifest,
            "categories": dict(category_by_path),
            "retention": retention,
            "message": "Gebundener Transaktions-Snapshot angelegt.",
        }
    except Exception as exc:
        return {
            "success": False,
            "root": str(backup_root) if backup_root is not None else None,
            "copied_count": len(copied),
            "skipped_count": max(0, len(preimages) - len(copied)),
            "copied": copied,
            "message": f"Gebundener Transaktions-Snapshot fehlgeschlagen: {exc}",
        }


def validate_bound_preimage_snapshot(
    preimages: list[dict[str, Any]],
    snapshot: dict[str, Any],
) -> dict[str, Any]:
    """Bindet Manifest, Restore-Metadaten und Payload an jedes Preimage."""
    checks: list[dict[str, Any]] = []
    if not snapshot.get("success") or not isinstance(snapshot.get("manifest"), dict):
        return {"success": False, "checks": checks, "error": "Snapshot oder Manifest fehlt"}

    manifest = snapshot["manifest"]
    sources = {
        str(item.get("source") or ""): item
        for item in manifest.get("sources", [])
        if isinstance(item, dict) and str(item.get("source") or "")
    }
    files = {
        str(item.get("restore_path") or ""): item
        for item in manifest.get("files", [])
        if isinstance(item, dict) and str(item.get("restore_path") or "")
    }
    copied = {
        str(item.get("path") or ""): item
        for item in snapshot.get("copied", [])
        if str(item.get("path") or "")
    }
    expected_paths = {str(item.get("path") or "") for item in preimages}
    if set(sources) != expected_paths:
        return {
            "success": False,
            "checks": checks,
            "error": "Manifest-Quellmenge stimmt nicht mit den Preimages überein",
        }

    for preimage in preimages:
        path = str(preimage.get("path") or "")
        source = sources.get(path, {})
        existed = bool(preimage.get("existed"))
        check: dict[str, Any] = {"path": path, "ok": False, "existed": existed}
        if not existed:
            check["ok"] = source.get("source_type") == "missing" and not bool(source.get("present"))
            checks.append(check)
            continue

        payload = bytes(preimage.get("payload") or b"")
        digest = hashlib.sha256(payload).hexdigest()
        copied_item = copied.get(path, {})
        manifest_item = files.get(path, {})
        check["ok"] = (
            source.get("source_type") == "file"
            and bool(source.get("present"))
            and copied_item.get("sha256") == digest
            and int(copied_item.get("uid", -1)) == int(preimage.get("uid", -2))
            and int(copied_item.get("gid", -1)) == int(preimage.get("gid", -2))
            and int(copied_item.get("mode", -1)) == int(preimage.get("mode", -2))
            and manifest_item.get("sha256") == digest
            and int(manifest_item.get("uid", -1)) == int(preimage.get("uid", -2))
            and int(manifest_item.get("gid", -1)) == int(preimage.get("gid", -2))
            and int(manifest_item.get("mode", -1)) == int(preimage.get("mode", -2))
        )
        checks.append(check)

    return {
        "success": len(checks) == len(preimages) and all(item.get("ok") for item in checks),
        "checks": checks,
    }


def desired_sudoers_lines() -> list[str]:
    service_lines = [
        (
            f"www-data ALL=(root) NOPASSWD: "
            f"{SERVICE_WRAPPER} {action} {unit}"
        )
        for action in SERVICE_WRAPPER_ACTIONS
        for unit in SERVICE_WRAPPER_UNITS
    ]
    return [
        *service_lines,
        f'www-data ALL=(root) NOPASSWD: {WEB_UPDATE_LAUNCHER} ""',
        f'{install_user()} ALL=(root) NOPASSWD: {WEB_UPDATE_LAUNCHER} ""',
    ]


def desired_sudoers_content() -> str:
    lines = [
        "# E3DC-Control WebUI and unattended-update wrapper permissions",
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


def _classify_sudoers_line(path: Path | None, line: str) -> dict[str, bool]:
    """Trennt E3DC-eigene sudoers-Zeilen von fremden Dienstfreigaben."""

    normalized = str(line or "").strip()
    lowered = normalized.lower()
    allowed_wrapper = normalized in desired_sudoers_lines()
    direct_web = "www-data" in lowered and "nopasswd:" in lowered and not allowed_wrapper
    direct_systemctl = "systemctl" in lowered and not allowed_wrapper
    legacy = "e3dc.service" in lowered
    path_name = path.name.lower() if path is not None else ""
    e3dc_owned = bool(
        path is not None
        and (
            path == SUDOERS_FILE
            or re.fullmatch(r"(?:\d+[_-])?e3dc(?:[_-].*)?", path_name)
        )
    )
    e3dc_related = bool(
        e3dc_owned
        or "e3dc" in lowered
        or str(INSTALL_ROOT).lower() in lowered
        or str(SERVICE_WRAPPER).lower() in lowered
        or str(WEB_UPDATE_LAUNCHER).lower() in lowered
        or str(INSTALLER_WRAPPER).lower() in lowered
    )
    managed_direct = bool(e3dc_owned and (direct_web or direct_systemctl or legacy))
    external_direct = bool(not e3dc_owned and (direct_web or direct_systemctl))
    return {
        "allowed_wrapper": allowed_wrapper,
        "direct_web": direct_web,
        "direct_systemctl": direct_systemctl,
        "legacy": legacy,
        "e3dc_owned": e3dc_owned,
        "e3dc_related": e3dc_related,
        "managed_direct": managed_direct,
        "external_direct": external_direct,
    }


def _unique_sudoers_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    unique: list[dict[str, Any]] = []
    seen: set[tuple[str, int, str]] = set()
    for item in items:
        key = (
            str(item.get("file") or ""),
            int(item.get("line_no") or 0),
            str(item.get("line") or ""),
        )
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    return unique


def sudoers_file_findings() -> dict[str, Any]:
    """Scannt sudoers und bindet nur E3DC-eigene Zeilen an Reparaturen."""

    files: list[dict[str, Any]] = []
    direct_web_lines: list[dict[str, Any]] = []
    direct_systemctl_lines: list[dict[str, Any]] = []
    managed_direct_lines: list[dict[str, Any]] = []
    managed_direct_systemctl_lines: list[dict[str, Any]] = []
    e3dc_systemctl_lines: list[dict[str, Any]] = []
    external_systemctl_lines: list[dict[str, Any]] = []
    external_direct_web_lines: list[dict[str, Any]] = []
    legacy_lines: list[dict[str, Any]] = []
    try:
        candidates = [Path("/etc/sudoers"), *sorted(SUDOERS_DIR.glob("*"))]
    except Exception:
        candidates = [Path("/etc/sudoers")]

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
                "managed_direct_lines": [],
                "managed_direct_systemctl_lines": [],
                "e3dc_systemctl_lines": [],
                "external_systemctl_lines": [],
                "external_direct_web_lines": [],
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
        file_managed: list[dict[str, Any]] = []
        file_managed_systemctl: list[dict[str, Any]] = []
        file_e3dc_systemctl: list[dict[str, Any]] = []
        file_external_systemctl: list[dict[str, Any]] = []
        file_external_web: list[dict[str, Any]] = []
        file_legacy: list[dict[str, Any]] = []
        for line_no, raw_line in enumerate(text.splitlines(), start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            item = {"file": str(path), "line_no": line_no, "line": line}
            classification = _classify_sudoers_line(path, line)
            item["scope"] = (
                "e3dc"
                if classification["e3dc_owned"]
                else ("external-e3dc-related" if classification["e3dc_related"] else "external")
            )
            if classification["direct_web"]:
                file_direct.append(item)
                direct_web_lines.append(item)
            if classification["direct_systemctl"]:
                file_systemctl.append(item)
                direct_systemctl_lines.append(item)
                if classification["e3dc_related"]:
                    file_e3dc_systemctl.append(item)
                    e3dc_systemctl_lines.append(item)
                if classification["e3dc_owned"]:
                    file_managed_systemctl.append(item)
                    managed_direct_systemctl_lines.append(item)
                elif not classification["e3dc_related"] and not classification["direct_web"]:
                    file_external_systemctl.append(item)
                    external_systemctl_lines.append(item)
            if classification["direct_web"] and not classification["e3dc_owned"]:
                file_external_web.append(item)
                external_direct_web_lines.append(item)
            if classification["managed_direct"]:
                file_managed.append(item)
                managed_direct_lines.append(item)
            if classification["legacy"]:
                file_legacy.append(item)
                legacy_lines.append(item)

        files.append({
            "file": str(path),
            "readable": True,
            "direct_web_lines": file_direct,
            "direct_systemctl_lines": file_systemctl,
            "managed_direct_lines": file_managed,
            "managed_direct_systemctl_lines": file_managed_systemctl,
            "e3dc_systemctl_lines": file_e3dc_systemctl,
            "external_systemctl_lines": file_external_systemctl,
            "external_direct_web_lines": file_external_web,
            "legacy_lines": file_legacy,
        })

    repairable_lines = _unique_sudoers_items(managed_direct_lines)
    return {
        "files": files,
        "direct_web_lines": _unique_sudoers_items(direct_web_lines),
        "direct_systemctl_lines": _unique_sudoers_items(direct_systemctl_lines),
        "managed_direct_lines": _unique_sudoers_items(managed_direct_lines),
        "managed_direct_systemctl_lines": _unique_sudoers_items(managed_direct_systemctl_lines),
        "e3dc_systemctl_lines": _unique_sudoers_items(e3dc_systemctl_lines),
        "external_systemctl_lines": _unique_sudoers_items(external_systemctl_lines),
        "external_direct_web_lines": _unique_sudoers_items(external_direct_web_lines),
        "legacy_lines": _unique_sudoers_items(legacy_lines),
        "repairable_lines": repairable_lines,
        "affected_files": sorted({
            item["file"]
            for item in (
                direct_web_lines
                + direct_systemctl_lines
                + external_direct_web_lines
                + legacy_lines
            )
        }),
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
    """Vergleicht die installierte VERSION mit dem neuesten Stable-Release."""

    return stable_update_check(INSTALL_ROOT.resolve())

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


def desired_sudoers_command_specs() -> set[str]:
    """Liefert den exakten wirksamen NOPASSWD-Kommandosatz."""

    return {
        " ".join(line.split("NOPASSWD:", 1)[1].split())
        for line in desired_sudoers_lines()
    }


def parse_effective_www_data_sudoers(listing_text: str) -> dict[str, Any]:
    """Parst ausschließlich explizite `(root) NOPASSWD:`-Einträge."""

    specs: list[str] = []
    ambiguous_lines: list[str] = []
    continuation = False
    for raw_line in str(listing_text or "").splitlines():
        stripped = raw_line.strip()
        match = re.fullmatch(
            r"\(([^)]*)\)\s+NOPASSWD:\s*(.*)",
            stripped,
        )
        if match:
            runas = " ".join(match.group(1).split())
            if runas != "root":
                ambiguous_lines.append(stripped)
                continuation = False
                continue
            payload = match.group(2)
            continuation = True
        elif continuation and raw_line[:1].isspace() and stripped:
            if stripped.startswith(("(", "Matching ", "User ", "Sudoers ")):
                continuation = False
                continue
            payload = stripped
        else:
            continuation = False
            if "NOPASSWD:" in stripped:
                ambiguous_lines.append(stripped)
            continue

        for item in payload.split(","):
            normalized = " ".join(item.split())
            if normalized:
                specs.append(normalized)
            else:
                ambiguous_lines.append(stripped)

    effective_specs = sorted(set(specs))
    desired_specs = desired_sudoers_command_specs()
    return {
        "effective_specs": effective_specs,
        "missing_effective_specs": sorted(
            desired_specs.difference(effective_specs)
        ),
        "unexpected_effective_specs": sorted(
            set(effective_specs).difference(desired_specs)
        ),
        "ambiguous_lines": list(dict.fromkeys(ambiguous_lines)),
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

    www_data_listing = run_cmd(
        ["/usr/bin/sudo", "-n", "-l", "-U", "www-data"],
        timeout=5,
    )
    effective_output = (
        str(www_data_listing.get("stdout") or "")
        + "\n"
        + str(www_data_listing.get("stderr") or "")
    )
    effective = parse_effective_www_data_sudoers(effective_output)
    listing_ok = www_data_listing.get("ok") is True
    bound_install_user = install_user()
    install_user_listing = run_cmd(
        ["/usr/bin/sudo", "-n", "-l", "-U", bound_install_user],
        timeout=5,
    )
    install_user_output = (
        str(install_user_listing.get("stdout") or "")
        + "\n"
        + str(install_user_listing.get("stderr") or "")
    )
    install_user_effective = parse_effective_www_data_sudoers(install_user_output)
    update_launcher_spec = f'{WEB_UPDATE_LAUNCHER} ""'
    install_user_contract_proven = bool(
        install_user_listing.get("ok") is True
        and update_launcher_spec in install_user_effective["effective_specs"]
    )
    effective_contract_proven = bool(
        listing_ok
        and not effective["missing_effective_specs"]
        and not effective["unexpected_effective_specs"]
        and not effective["ambiguous_lines"]
        and install_user_contract_proven
    )
    effective_status = (
        "effective_sudoers_exact"
        if effective_contract_proven
        else "effective_sudoers_unproven"
        if not listing_ok or effective["ambiguous_lines"]
        else "effective_sudoers_mismatch"
    )

    sudoers_text = "\n".join(sudoers_chunks)
    sudoers_source = (
        "+".join(dict.fromkeys(sudoers_sources))
        if sudoers_sources
        else "sudoers_file_unavailable"
    )
    effective_www_data_lines = [
        f"(root) NOPASSWD: {spec}"
        for spec in effective["effective_specs"]
    ]
    effective_direct_web_lines = [
        f"(root) NOPASSWD: {spec}"
        for spec in effective["unexpected_effective_specs"]
    ] + list(effective["ambiguous_lines"])

    active_lines = [
        line.strip()
        for line in sudoers_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    effective_managed_systemctl: list[str] = []
    effective_external_systemctl: list[str] = []
    effective_legacy: list[str] = []
    for line in active_lines:
        classification = _classify_sudoers_line(None, line)
        if classification["direct_systemctl"]:
            if classification["e3dc_related"]:
                effective_managed_systemctl.append(line)
            elif not classification["direct_web"]:
                effective_external_systemctl.append(line)
        if classification["legacy"]:
            effective_legacy.append(line)

    managed_systemctl_lines = list(dict.fromkeys(
        [
            str(item.get("line") or "")
            for item in file_findings["e3dc_systemctl_lines"]
            if str(item.get("line") or "")
        ]
        + effective_managed_systemctl
    ))
    external_systemctl_lines = list(dict.fromkeys(
        [
            str(item.get("line") or "")
            for item in file_findings["external_systemctl_lines"]
            if str(item.get("line") or "")
        ]
        + effective_external_systemctl
    ))
    legacy_lines = list(dict.fromkeys(
        [
            str(item.get("line") or "")
            for item in file_findings["legacy_lines"]
            if str(item.get("line") or "")
        ]
        + effective_legacy
    ))

    return {
        "text": sudoers_text,
        "source": sudoers_source,
        "active_lines": active_lines,
        "direct_systemctl_lines": managed_systemctl_lines,
        "external_systemctl_lines": external_systemctl_lines,
        "legacy_lines": legacy_lines,
        "effective_www_data_lines": effective_www_data_lines,
        "effective_direct_web_lines": effective_direct_web_lines,
        "effective_listing_ok": listing_ok,
        "effective_listing": www_data_listing,
        "effective_status": effective_status,
        "effective_specs": effective["effective_specs"],
        "missing_effective_specs": effective["missing_effective_specs"],
        "unexpected_effective_specs": effective["unexpected_effective_specs"],
        "effective_ambiguous_lines": effective["ambiguous_lines"],
        "effective_contract_proven": effective_contract_proven,
        "install_user": bound_install_user,
        "install_user_listing_ok": install_user_listing.get("ok") is True,
        "install_user_update_launcher_proven": install_user_contract_proven,
        "install_user_effective_specs": install_user_effective["effective_specs"],
        "file_findings": file_findings,
    }


def write_readiness(ignore_active_lock: bool = False) -> dict[str, Any]:
    sudoers = sudoers_context()
    sudoers_text = sudoers["text"]
    sudoers_source = sudoers["source"]

    service_launcher = service_launcher_integrity_preview()
    service_wrapper = {
        "label": "Root-eigener Service-Launcher",
        "path": str(SERVICE_WRAPPER),
        "ok": bool(service_launcher.get("success")),
        "hard": True,
        "status": service_launcher.get("status", "unbekannt"),
        "issue": (
            None
            if service_launcher.get("success")
            else "Service-Launcher fehlt oder ist nicht root-eigen an Git-HEAD gebunden"
        ),
        "details": service_launcher,
    }
    web_update_launcher = web_update_launcher_integrity_preview()
    web_update_wrapper = {
        "label": "Root-eigener Web-Update-Launcher",
        "path": str(WEB_UPDATE_LAUNCHER),
        "ok": bool(web_update_launcher.get("success")),
        "hard": True,
        "status": web_update_launcher.get("status", "unbekannt"),
        "issue": (
            None
            if web_update_launcher.get("success")
            else "Web-Update-Launcher fehlt oder besitzt keinen sicheren Dispatcher-Vertrag"
        ),
        "details": web_update_launcher,
    }
    installer_wrapper = file_check(INSTALLER_WRAPPER, "Installer-Wrapper", executable=True)
    wrapper_integrity = wrapper_integrity_preview()
    wrapper_integrity_ok = bool(wrapper_integrity.get("success")) and not wrapper_integrity.get("repair_needed")
    sudoers_exists = SUDOERS_FILE.exists()
    sudoers_has_service = all(
        line in sudoers_text for line in desired_sudoers_lines()
    )
    sudoers_has_installer = str(INSTALLER_WRAPPER) in sudoers_text
    sudoers_direct_systemctl = bool(sudoers["direct_systemctl_lines"])
    sudoers_direct_web_commands = bool(
        sudoers["file_findings"]["direct_web_lines"]
        or sudoers["effective_direct_web_lines"]
    )
    external_systemctl_lines = sudoers["external_systemctl_lines"]
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
        web_update_wrapper,
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
            "label": "sudoers: effektive www-data-Rechte exakt gebunden",
            "ok": sudoers["effective_contract_proven"],
            "hard": True,
            "status": sudoers["effective_status"],
            "issue": (
                None
                if sudoers["effective_contract_proven"]
                else "effective_sudoers_unproven"
                if sudoers["effective_status"] == "effective_sudoers_unproven"
                else "Wirksame www-data-Rechte weichen vom festen Wrappervertrag ab"
            ),
            "details": {
                "missing": sudoers["missing_effective_specs"],
                "unexpected": sudoers["unexpected_effective_specs"],
                "ambiguous": sudoers["effective_ambiguous_lines"],
            },
        },
        {
            "label": "sudoers: kein breiter privilegierter Installer-Webzugang",
            "ok": not sudoers_has_installer,
            "hard": True,
            "issue": (
                "Die alte, zu breite www-data-Freigabe für installer_wrapper.sh muss entfernt werden"
                if sudoers_has_installer
                else None
            ),
        },
        {
            "label": "keine freien systemctl-Kommandos",
            "ok": not sudoers_direct_systemctl,
            "hard": True,
            "issue": "E3DC-sudoers enthält direkte systemctl-Freigaben" if sudoers_direct_systemctl else None,
        },
        {
            "label": "fremde systemctl-Freigaben bleiben fremdverwaltet",
            "ok": not external_systemctl_lines,
            "hard": False,
            "issue": (
                "sudoers enthält direkte systemctl-Freigaben außerhalb des E3DC-Besitzbereichs; "
                "sie werden nur gemeldet, nicht verändert und blockieren den Release-Wechsel nicht"
                if external_systemctl_lines
                else None
            ),
            "details": external_systemctl_lines,
        },
        {
            "label": "keine direkten www-data-Kommandos",
            "ok": not sudoers_direct_web_commands,
            "hard": True,
            "issue": (
                "sudoers enthält direkte www-data-Freigaben außerhalb des engen Service-Wrappers"
                if sudoers_direct_web_commands
                else None
            ),
            "details": {
                "files": sudoers["file_findings"]["direct_web_lines"],
                "effective": sudoers["effective_direct_web_lines"],
            },
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
    service_wrapper_ready = not hard_blockers and not is_docker()
    return {
        "success": True,
        "write_actions_enabled": WRITE_ACTIONS_ENABLED,
        "privileged_installer_web_enabled": False,
        "web_update_launcher_ready": bool(web_update_launcher.get("success") and service_wrapper_ready),
        "service_wrapper_ready": service_wrapper_ready,
        "ready_for_manual_enable": False,
        "can_write_now": False,
        "summary": (
            "Breite privilegierte Installer-Webaktionen bleiben fail-closed gesperrt. "
            "Dienststeuerung und Self-Update besitzen getrennte root-eigene Launcher."
        ),
        "checks": checks,
        "hard_blocker_count": len(hard_blockers),
        "hard_blockers": hard_blockers,
        "allowed_write_actions": [],
        "release_steps": [
            "Alte installer_wrapper.sh- und direkte installer_main.py-Freigaben aus sudoers entfernen",
            "Service-Wrapper und seine feste Aktions-/Unit-Allowlist getrennt prüfen",
            "Argumentlosen Update-Dispatcher root-eigen und pfadgebunden installieren",
            "Veröffentlichten Ziel-Bootstrap root-privat laden und im Systemjob starten",
            "Installation, Rechte-Reparatur und Rückfall weiterhin nur administrativ ausführen",
        ],
        "next_step": "Nur das Self-Update darf über den engen Launcher starten; alle anderen Installer-Webjobs bleiben gesperrt.",
    }


def write_permission_plan() -> dict[str, Any]:
    """Read-only-Plan für die spätere Bereinigung von sudoers und Wrappern."""
    sudoers = sudoers_context()
    desired_sudoers = desired_sudoers_lines()
    checks = write_readiness()
    wrapper_preview = wrapper_integrity_preview()
    wrapper_repair_needed = bool(
        wrapper_preview.get("success")
        and wrapper_preview.get("repair_needed")
    )
    repairable_items = sudoers["file_findings"]["repairable_lines"]
    remove_lines = list(dict.fromkeys(
        str(item.get("line") or "")
        for item in repairable_items
        if str(item.get("line") or "")
    ))
    missing_lines = [line for line in desired_sudoers if line not in sudoers["text"]]
    would_change = bool(
        remove_lines
        or missing_lines
        or not SUDOERS_FILE.exists()
        or wrapper_repair_needed
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
        "wrapper_repair_needed": wrapper_repair_needed,
        "wrapper_integrity": wrapper_preview,
        "current": {
            "active_lines": sudoers["active_lines"],
            "direct_systemctl_lines": sudoers["direct_systemctl_lines"],
            "external_systemctl_lines": sudoers["external_systemctl_lines"],
            "legacy_lines": sudoers["legacy_lines"],
            "file_findings": sudoers["file_findings"],
        },
        "target": {
            "allowed_lines": desired_sudoers,
            "missing_lines": missing_lines,
            "service_wrapper": str(SERVICE_WRAPPER),
            "web_update_launcher": str(WEB_UPDATE_LAUNCHER),
            "installer_wrapper_admin_only": str(INSTALLER_WRAPPER),
        },
        "file_preview": file_preview,
        "planned_steps": [
            "Bestehende sudoers-Datei sichern, bevor sie ersetzt wird.",
            "Nur E3DC-eigene direkte systemctl-/WebUI-Freigaben entfernen; fremde Fragmente bleiben byte- und metadatengleich.",
            "Nur die engen Service- und argumentlosen Update-Launcher für www-data setzen; installer_wrapper.sh bleibt administrativ.",
            "sudoers-Syntax mit visudo -cf prüfen.",
            "Freigabe-Check erneut ausführen; privilegierte Installer-Webjobs bleiben gesperrt.",
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
            "Keine direkten E3DC-systemctl-Freigaben für www-data.",
            "Keine privilegierte installer_wrapper.sh- oder installer_main.py-Freigabe für www-data; Update nur über den root-eigenen argumentlosen Launcher.",
            "Fremde sudoers-Fragmente werden ausschließlich gemeldet und niemals vom E3DC-Installer verändert.",
            "Der alte C++ Dienst e3dc.service bleibt kein erlaubtes WebUI-Startziel.",
            "Web-Updates starten ausschließlich als versiegelter Systemjob ohne freie Aktion, Pfade oder Releaseparameter.",
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
        WEB_UPDATE_LAUNCHER,
        INSTALLER_WRAPPER,
    ]
    sudoers_findings = sudoers_file_findings()
    system_files.extend(
        Path(str(item.get("file") or ""))
        for item in sudoers_findings.get("repairable_lines", [])
        if str(item.get("file") or "")
    )
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
    mapped_entries: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []

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
        category = str(item.get("category") or "files").strip().lower()
        if not item.get("exists"):
            skipped.append({"path": str(source), "reason": "Quelle fehlt"})
            source_records.append({
                "category": category,
                "source": str(source),
                "present": False,
                "files": 0,
                "source_type": "missing",
                "exclude_top_level": [],
                "exclude_anywhere": [],
                "directories": [],
            })
            continue
        try:
            metadata = os.lstat(source)
        except Exception as exc:
            skipped.append({"path": str(source), "reason": f"Quelle nicht lesbar: {exc}"})
            continue
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            skipped.append({"path": str(source), "reason": "Quelle ist nicht regulär/nlink=1 oder ist ein Symlink"})
            continue
        size = int(metadata.st_size)
        if size > MAX_BACKUP_FILE_BYTES:
            skipped.append({
                "path": str(source),
                "reason": f"Datei groesser als {MAX_BACKUP_FILE_BYTES // 1024 // 1024} MB",
                "size": size,
            })
            continue
        target = backup_root / category / backup_relative_path(source)
        try:
            preimage = _capture_file_preimage(source)
            payload = bytes(preimage.get("payload") or b"")
            if not preimage.get("existed") or len(payload) != size:
                raise RuntimeError("Quelle driftete vor der Sicherung")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(payload)
            os.chmod(target, int(preimage["mode"]))
            if str(item.get("category") or "") == "config":
                apply_config_backup_dir_permissions(target.parent, install_user=install_user())
                apply_config_secret_permissions(target, install_user=install_user())
            archive_relative = target.relative_to(backup_root).as_posix()
            mapped_entries.append({
                "backup_path": archive_relative,
                "restore_path": str(source),
                "category": category,
                "restore_mode": int(preimage["mode"]),
                "restore_uid": int(preimage["uid"]),
                "restore_gid": int(preimage["gid"]),
            })
            source_records.append({
                "category": category,
                "source": str(source),
                "present": True,
                "files": 1,
                "source_type": "file",
                "exclude_top_level": [],
                "exclude_anywhere": [],
                "directories": [],
            })
            copied.append({
                "path": str(source),
                "backup": str(target),
                "size": size,
                "sha256": hashlib.sha256(payload).hexdigest(),
                "uid": preimage.get("uid"),
                "gid": preimage.get("gid"),
                "mode": preimage.get("mode"),
            })
        except Exception as exc:
            skipped.append({"path": str(source), "reason": f"Kopie fehlgeschlagen: {exc}", "size": size})

    existing_count = int(plan.get("would_backup_count") or 0)
    success = len(copied) == existing_count
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
                mapped_entries,
                source_records,
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
    manager_lock_prestart = (
        "ExecStartPre=+/usr/bin/systemd-tmpfiles --create "
        f"{MANAGER_LOCK_TMPFILES_CONFIG}\n"
        if str(getattr(module, "key", "")) in {"heatpump", "heizstab"}
        else ""
    )
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
{manager_lock_prestart}ExecStart={python_executable()} -u {script_path}
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
    package_lock = workdir / "package-lock.json"
    if not package_lock.is_file():
        return {
            "step": "prepare_runner",
            "ok": False,
            "runner": runner,
            "workdir": str(workdir),
            "error": f"package-lock.json fehlt: {package_lock}",
        }
    npm = npm_executable()
    node_check = run_cmd(["node", "--version"], timeout=15)
    node_match = re.fullmatch(
        r"v([0-9]+)(?:\.[0-9]+){1,2}",
        str(node_check.get("stdout") or "").strip(),
    )
    if (
        not node_check.get("ok")
        or node_match is None
        or int(node_match.group(1)) < 18
    ):
        return {
            "step": "prepare_runner",
            "ok": False,
            "runner": runner,
            "workdir": str(workdir),
            "error": "Matter.js benötigt Node.js 18 oder neuer",
            "node_check": node_check,
        }
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
    install = run_cmd(
        [npm, "ci", "--omit=dev", "--ignore-scripts"],
        timeout=180,
        cwd=workdir,
    )
    return {
        "step": "prepare_runner",
        "ok": bool(install.get("ok")),
        "runner": runner,
        "workdir": str(workdir),
        "package_json": str(package_json),
        "package_lock": str(package_lock),
        "node_version": node_check.get("stdout"),
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

    if module.key in {"heatpump", "heizstab"} and ensure_manager_lock_namespace() is not True:
        return {
            "success": False,
            "module": module.public_dict(),
            "message": (
                "Installation abgebrochen: Der root-kontrollierte "
                "Wärme-Owner-Lockraum konnte nicht sicher eingerichtet werden."
            ),
        }

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


def check_path(
    path: Path,
    *,
    expected_owner: str | tuple[str, ...] | None = None,
    expected_group: str | None = "www-data",
    expected_mode: int | None = None,
    should_write: bool = False,
) -> dict[str, Any]:
    exists = path.exists()
    meta = owner_group(path) if exists else {"owner": None, "group": None, "mode": None}
    writable = os.access(path, os.W_OK) if exists else False
    problems: list[str] = []
    if not exists:
        problems.append("fehlt")
    elif path.is_symlink():
        problems.append("ist ein symbolischer Link")
    else:
        allowed_owners = (
            tuple(str(owner) for owner in expected_owner)
            if isinstance(expected_owner, tuple)
            else ((str(expected_owner),) if expected_owner else ())
        )
        if allowed_owners and meta.get("owner") not in allowed_owners:
            problems.append(
                f"Besitzer ist {meta.get('owner')}, erwartet {' oder '.join(allowed_owners)}"
            )
        if expected_group and meta.get("group") != expected_group:
            problems.append(
                f"Gruppe ist {meta.get('group')}, erwartet {expected_group}"
            )
        if expected_mode is not None and meta.get("mode") != oct(expected_mode):
            problems.append(
                f"Modus ist {meta.get('mode')}, erwartet {oct(expected_mode)}"
            )
        if should_write and not writable:
            problems.append("nicht schreibbar für aktuellen Web-Installer-Kontext")
    issue = "; ".join(problems) if problems else None
    return {
        "path": str(path),
        "exists": exists,
        **meta,
        "writable": writable,
        "ok": issue is None,
        "issue": issue,
    }


def permissions_check() -> dict[str, Any]:
    user = install_user()
    data_mode = config_secret_dir_mode()
    config_mode = config_secret_file_mode()
    config_owners: tuple[str, ...] = (user,)
    try:
        import grp as _grp
        import pwd as _pwd

        install_account = _pwd.getpwnam(user)
        web_group = _grp.getgrnam("www-data")
        if install_account.pw_gid == web_group.gr_gid or user in web_group.gr_mem:
            config_owners = (user, "www-data")
    except (ImportError, KeyError, OSError):
        pass
    paths = [
        (WEB_ROOT, "root", "www-data", 0o755, False),
        (TMP_DIR, user, "www-data", 0o2775, True),
        (RAMDISK_DIR, user, "www-data", 0o2775, True),
        (LOG_DIR, user, "www-data", 0o2775, True),
        (DATA_DIR, user, "www-data", data_mode, True),
        (INSTALL_ROOT, user, "www-data", 0o755, False),
        (INSTALLER_DIR, user, "www-data", 0o755, False),
        (INSTALLER_DIR / "service_wrapper.sh", user, "www-data", 0o755, False),
        (INSTALLER_DIR / "installer_wrapper.sh", user, "www-data", 0o755, False),
        (INSTALLER_DIR / "web_update_launcher.sh", user, "www-data", 0o755, False),
        (CONFIG_FILE, config_owners, "www-data", config_mode, True),
    ]
    checks = [
        check_path(
            path,
            expected_owner=owner,
            expected_group=group,
            expected_mode=mode,
            should_write=write,
        )
        for path, owner, group, mode, write in paths
    ]
    for session_file in SESSION_FILES:
        if session_file.exists():
            checks.append(
                check_path(
                    session_file,
                    expected_owner=user,
                    expected_group="www-data",
                    expected_mode=0o664,
                    should_write=True,
                )
            )
    issues = [item for item in checks if not item["ok"]]
    launcher_state = web_update_launcher_integrity_preview()
    repair_command = "/usr/bin/sudo -n -- /usr/local/sbin/e3dc-web-update-launcher"
    return {
        "success": True,
        "write_actions_enabled": WRITE_ACTIONS_ENABLED,
        "summary": "Rechteprüfung abgeschlossen. Es wurden keine Änderungen ausgeführt.",
        "checks": checks,
        "issue_count": len(issues),
        "issues": issues,
        "repair_available": bool(launcher_state.get("success")),
        "privileged_web_repair_enabled": False,
        "repair_via": "canonical_web_update_launcher",
        "repair_launcher_status": launcher_state.get("status", "unbekannt"),
        "repair_message": (
            "Die direkte Web-Installer-Reparatur bleibt gesperrt. Backup, "
            "Rechteprojektion, Releaseabgleich und Dienstneustart laufen über "
            "den root-eigenen argumentlosen Web-Update-Launcher."
        ),
        "detected_install_path": str(INSTALL_ROOT),
        "repair_command": repair_command,
        "repair_instruction": (
            "Per SSH am E3DC-Control-System anmelden und den folgenden Befehl "
            "ausführen. Fehlt der root-eigene Launcher, zuerst das für die "
            "installierte Version veröffentlichte Community-Bootstrap verwenden; "
            "dieses installiert den Launcher gebunden und startet danach das Update."
        ),
    }


def repair_permissions(
    *,
    repair_runtime: bool = False,
    bound_privileged_preimages: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ersetzt atomar nur die commitgebundenen Wrapper und sudoers-Fragmente."""
    readiness = write_readiness()
    if repair_runtime:
        return {
            "success": False,
            "message": (
                "Wrapper-/sudoers-Reparatur abgebrochen: Runtime- und Session-Rechte "
                "gehören in den separaten allgemeinen Rechte-Wizard."
            ),
            "readiness": readiness,
        }
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

    target_payload = desired_sudoers_content().encode("utf-8")
    steps: list[dict[str, Any]] = []
    findings: dict[str, Any] = {}
    wrapper_preimages: list[dict[str, Any]] = []
    launcher_preimages: list[dict[str, Any]] = []
    sudoers_preimages: list[dict[str, Any]] = []
    validation_tmp_path: Path | None = None
    mutation_started = False

    def rollback_permissions() -> dict[str, Any]:
        rollback = _restore_preimages(
            [*wrapper_preimages, *launcher_preimages, *sudoers_preimages]
        )
        syntax = run_cmd([str(VISUDO), "-cf", "/etc/sudoers"], timeout=10)
        rollback["visudo"] = syntax
        rollback["success"] = bool(rollback.get("success")) and bool(syntax.get("ok"))
        steps.append({"step": "permissions_rollback", "ok": rollback["success"], **rollback})
        return rollback

    try:
        try:
            # Die Mutationsmenge wird genau einmal gebildet. Derselbe Satz
            # liefert In-Memory-Preimages, persistenten Snapshot und alle
            # späteren Forward-Writer; ein zweiter Findings-Scan darf den
            # Schreibumfang nicht unbemerkt erweitern.
            findings = sudoers_file_findings()
            wrapper_preimages = [
                _capture_file_preimage(Path(str(item.get("path") or "")))
                for item in wrapper_preview.get("items", [])
            ]
            sudoers_paths = {SUDOERS_FILE}
            sudoers_paths.update(
                Path(str(item.get("file") or ""))
                for item in findings.get("repairable_lines", [])
                if str(item.get("file") or "")
            )
            privileged_paths = {
                SERVICE_WRAPPER,
                WEB_UPDATE_LAUNCHER,
                *sudoers_paths,
            }
            if bound_privileged_preimages is None:
                privileged_by_path = {
                    str(path): _capture_file_preimage(path)
                    for path in sorted(privileged_paths, key=lambda item: str(item))
                }
            else:
                privileged_by_path = {
                    str(item.get("path") or ""): item
                    for item in bound_privileged_preimages
                }
                if (
                    len(privileged_by_path) != len(bound_privileged_preimages)
                    or set(privileged_by_path)
                    != {str(path) for path in privileged_paths}
                ):
                    raise RuntimeError(
                        "Gebundene privilegierte Preimages sind unvollständig oder doppelt"
                    )
                for preimage in privileged_by_path.values():
                    _assert_preimage_unchanged(preimage)
            launcher_preimages = [
                privileged_by_path[str(SERVICE_WRAPPER)],
                privileged_by_path[str(WEB_UPDATE_LAUNCHER)],
            ]
            sudoers_preimages = [
                privileged_by_path[str(path)]
                for path in sorted(sudoers_paths, key=lambda item: str(item))
            ]
        except Exception as exc:
            return {
                "success": False,
                "message": f"Rechte-Reparatur abgebrochen: Transaktions-Preimages fehlen: {exc}",
                "steps": steps,
                "readiness": readiness,
            }

        all_preimages = [
            *wrapper_preimages,
            *launcher_preimages,
            *sudoers_preimages,
        ]
        category_by_path = {
            **{str(item["path"]): "wrapper" for item in wrapper_preimages},
            **{str(item["path"]): "service_launcher" for item in launcher_preimages},
            **{str(item["path"]): "sudoers" for item in sudoers_preimages},
        }
        backup_snapshot = create_bound_preimage_snapshot(
            "repair_permissions",
            all_preimages,
            category_by_path,
        )
        if not backup_snapshot.get("success"):
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Gebundener Transaktions-Snapshot fehlt.",
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

        backup_coverage = validate_bound_preimage_snapshot(all_preimages, backup_snapshot)
        steps.append({
            "step": "validate_bound_preimage_snapshot",
            "ok": bool(backup_coverage.get("success")),
            "checks": backup_coverage.get("checks", []),
        })
        if not backup_coverage.get("success"):
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Preimage-Snapshot ist nicht vollständig gebunden.",
                "steps": steps,
                "wrapper_integrity": wrapper_preview,
                "backup_snapshot": backup_snapshot,
                "backup_coverage": backup_coverage,
                "readiness": readiness,
            }

        try:
            for preimage in all_preimages:
                _assert_preimage_unchanged(preimage)
        except Exception as exc:
            return {
                "success": False,
                "message": f"Rechte-Reparatur abgebrochen: Preimage driftete nach dem Snapshot: {exc}",
                "steps": steps,
                "backup_snapshot": backup_snapshot,
                "backup_coverage": backup_coverage,
                "readiness": readiness,
            }

        user = install_user()
        mutation_started = True
        wrapper_repair = repair_wrapper_integrity(
            user=user,
            group="www-data",
            bound_preimages=wrapper_preimages,
            rollback_on_failure=False,
        )
        steps.extend(wrapper_repair.get("steps", []))
        if not wrapper_repair.get("success"):
            rollback = rollback_permissions()
            return {
                "success": False,
                "message": (
                    wrapper_repair.get("message") or "Wrapper-Reparatur fehlgeschlagen."
                ) + (" Rücklauf vollständig." if rollback["success"] else " Rücklauf unvollständig."),
                "steps": steps,
                "wrapper_integrity": wrapper_repair,
                "backup_snapshot": backup_snapshot,
                "readiness": readiness,
                "rollback": rollback,
            }
        for preimage in wrapper_preimages:
            _bind_transaction_output(preimage)

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
            rollback = rollback_permissions()
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Wrapper-Endgate vor sudoers ist nicht grün.",
                "steps": steps,
                "wrapper_integrity": wrapper_endgate,
                "backup_snapshot": backup_snapshot,
                "readiness": readiness,
                "rollback": rollback,
            }

        rebound_head, rebound_canonical = _git_head_wrapper_bytes(INSTALL_ROOT)
        if rebound_head != wrapper_endgate.get("head"):
            rollback = rollback_permissions()
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: HEAD driftete vor dem Launcher-Commit.",
                "steps": steps,
                "rollback": rollback,
            }
        launcher_by_path = {str(item["path"]): item for item in launcher_preimages}
        launcher_preimage = launcher_by_path[str(SERVICE_WRAPPER)]
        _atomic_write_service_launcher(
            rebound_canonical["Installer/service_wrapper.sh"],
            launcher_preimage,
        )
        installed_launcher = service_launcher_integrity_preview()
        steps.append({
            "step": "install_root_service_launcher",
            "ok": bool(installed_launcher.get("success")),
            "path": str(SERVICE_WRAPPER),
        })
        if (
            not installed_launcher.get("success")
            or installed_launcher.get("head") != rebound_head
        ):
            raise RuntimeError(
                "Root-eigener Service-Launcher bestand das Endgate nicht"
            )

        web_update_payload = _render_web_update_launcher(
            rebound_canonical["Installer/web_update_launcher.sh"],
            root=INSTALL_ROOT,
            user=user,
        )
        _atomic_write_root_launcher(
            WEB_UPDATE_LAUNCHER,
            web_update_payload,
            launcher_by_path[str(WEB_UPDATE_LAUNCHER)],
            label="Web-Update-Launcher",
        )
        installed_web_update_launcher = web_update_launcher_integrity_preview(
            expected_payload=web_update_payload,
        )
        steps.append({
            "step": "install_root_web_update_launcher",
            "ok": bool(installed_web_update_launcher.get("success")),
            "path": str(WEB_UPDATE_LAUNCHER),
        })
        if (
            not installed_web_update_launcher.get("success")
        ):
            raise RuntimeError(
                "Root-eigener Web-Update-Launcher bestand das Endgate nicht"
            )

        sudoers_by_path = {str(item["path"]): item for item in sudoers_preimages}
        target_preimage = sudoers_by_path[str(SUDOERS_FILE)]
        _assert_preimage_unchanged(target_preimage)

        validation_fd, validation_tmp_name = tempfile.mkstemp(
            prefix=".e3dc-wrapper-sudoers-validate-",
            dir=str(SUDOERS_DIR),
        )
        validation_tmp_path = Path(validation_tmp_name)
        try:
            offset = 0
            while offset < len(target_payload):
                offset += os.write(validation_fd, target_payload[offset:])
            os.fchown(validation_fd, 0, 0)
            os.fchmod(validation_fd, 0o440)
            os.fsync(validation_fd)
        finally:
            os.close(validation_fd)
        syntax_tmp = run_cmd([str(VISUDO), "-cf", str(validation_tmp_path)], timeout=10)
        steps.append({"step": "validate_target", **syntax_tmp})
        if not syntax_tmp.get("ok"):
            validation_tmp_path.unlink(missing_ok=True)
            validation_tmp_path = None
            rollback = rollback_permissions()
            return {
                "success": False,
                "message": "Rechte-Reparatur abgebrochen: Ziel-sudoers hat die visudo-Pruefung nicht bestanden.",
                "steps": steps,
                "backup_snapshot": backup_snapshot,
                "rollback": rollback,
            }
        validation_tmp_path.unlink(missing_ok=True)
        validation_tmp_path = None

        _atomic_write_sudoers(SUDOERS_FILE, target_payload, target_preimage)
        sudoers_uid, sudoers_gid = _sudoers_owner_ids()
        written_target = _capture_file_preimage(SUDOERS_FILE)
        if (
            written_target.get("payload") != target_payload
            or written_target.get("uid") != sudoers_uid
            or written_target.get("gid") != sudoers_gid
            or written_target.get("mode") != 0o440
        ):
            raise RuntimeError("Ziel-sudoers stimmt nach dem atomaren Schreiben nicht exakt")
        steps.append({"step": "write_sudoers", "ok": True, "path": str(SUDOERS_FILE)})

        target_file = str(SUDOERS_FILE)
        removable_by_file: dict[str, set[tuple[int, str]]] = {}
        for item in findings["repairable_lines"]:
            if item["file"] == target_file:
                continue
            removable_by_file.setdefault(item["file"], set()).add((int(item["line_no"]), str(item["line"])))

        for file_name, removable in sorted(removable_by_file.items()):
            path = Path(file_name)
            preimage = sudoers_by_path.get(file_name)
            if preimage is None:
                raise RuntimeError(f"Legacy-sudoers besitzt kein Transaktions-Preimage: {file_name}")
            _assert_preimage_unchanged(preimage)
            old_text = bytes(preimage.get("payload") or b"").decode("utf-8", errors="replace")
            kept_lines = []
            removed_count = 0
            for line_no, raw_line in enumerate(old_text.splitlines(), start=1):
                stripped = raw_line.strip()
                if (line_no, stripped) in removable:
                    removed_count += 1
                    continue
                kept_lines.append(raw_line)
            if removed_count != len(removable):
                raise RuntimeError(f"Legacy-sudoers-Zeilensatz driftete: {file_name}")
            new_text = "\n".join(kept_lines).rstrip() + "\n"
            new_payload = new_text.encode("utf-8")
            _atomic_write_sudoers(path, new_payload, preimage)
            written = _capture_file_preimage(path)
            if (
                written.get("payload") != new_payload
                or written.get("uid") != sudoers_uid
                or written.get("gid") != sudoers_gid
                or written.get("mode") != 0o440
            ):
                raise RuntimeError(f"Legacy-sudoers-Endgate ist nicht exakt: {file_name}")
            steps.append({
                "step": "clean_legacy_sudoers",
                "ok": True,
                "path": file_name,
                "removed_lines": removed_count,
            })

        syntax_final = run_cmd([str(VISUDO), "-cf", str(SUDOERS_FILE)], timeout=10)
        steps.append({"step": "validate_final", **syntax_final})
        syntax_all = run_cmd([str(VISUDO), "-cf", "/etc/sudoers"], timeout=10)
        steps.append({"step": "validate_all_sudoers", **syntax_all})

        # Dieser Abschlusscheck laeuft noch innerhalb des eigenen Job-Locks.
        # Der Lock ist hier kein Fehler, sondern der Schutzrahmen des gerade
        # erfolgreich ausgefuehrten Reparaturjobs.
        post = write_readiness(ignore_active_lock=True)
        success = bool(syntax_final.get("ok")) and bool(syntax_all.get("ok")) and post.get("hard_blocker_count", 1) == 0
        rollback = None
        if not success:
            rollback = rollback_permissions()
        return {
            "success": success,
            "message": (
                "Rechte-Reparatur abgeschlossen."
                if success
                else "Rechte-Reparatur validierte nicht und wurde automatisch zurückgerollt."
            ),
            "steps": steps,
            "backup": backup_snapshot.get("root"),
            "backup_snapshot": backup_snapshot,
            "wrapper_integrity": wrapper_endgate,
            "readiness": post,
            "rollback": rollback,
            "rollback_plan": [
                "Gebundene Transaktions-Preimages atomar zurückspielen",
                "visudo -cf /etc/sudoers ausführen",
                "Freigabe-Check erneut starten",
            ],
        }
    except Exception as exc:
        rollback = rollback_permissions() if mutation_started else None
        return {
            "success": False,
            "message": (
                f"Rechte-Reparatur abgebrochen und automatisch zurückgerollt: {exc}"
                if rollback and rollback.get("success")
                else f"Rechte-Reparatur abgebrochen; Rücklauf unvollständig oder nicht erforderlich: {exc}"
            ),
            "steps": steps,
            "backup_snapshot": locals().get("backup_snapshot"),
            "rollback": rollback,
            "readiness": readiness,
        }
    finally:
        if validation_tmp_path is not None:
            validation_tmp_path.unlink(missing_ok=True)


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

    if normalized_action == "repair_permissions":
        check = permissions_check()
        return {
            "success": False,
            "action": action,
            "write_blocked": True,
            "privileged_web_repair_enabled": False,
            "repair_available": check["repair_available"],
            "repair_via": check["repair_via"],
            "message": check["repair_message"],
            "detected_install_path": check["detected_install_path"],
            "repair_command": check["repair_command"],
            "repair_instruction": check["repair_instruction"],
        }

    if normalized_action in SERVICE_ACTIONS or normalized_action in {"run_update", "install_module", "remove_module", "install_missing_packages"}:
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
            "summary": "Dry-Run: Die Reparatur würde Wrapper prüfen und zu breite Web-sudoers-Freigaben entfernen.",
            "would_change": bool(plan.get("would_change")),
            "planned_steps": [
                "Commitgebundene Wrapper für administrative Nutzung prüfen",
                "Installer-Wrapper, installer_main.py und direkte systemctl-Freigaben für www-data entfernen",
                "Nur den festen Service-Wrapper und den argumentlosen Self-Update-Launcher behalten",
                "Ziel-sudoers mit visudo prüfen, bevor sie aktiv wird",
                "Breite Installer-Webjobs weiterhin nicht freischalten",
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
