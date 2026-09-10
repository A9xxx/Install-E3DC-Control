#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Feste Docker-Laufzeitidentität, getrennt vom administrativen Installationsbesitz.

Umgebungsvariablen und Webmetadaten verleihen keine Rolle. Die eng begrenzte
Laufzeit wird an tatsächliche Prozess-IDs, das Systemkonto und unveränderliche
Produktpfade gebunden. Dieser Vertrag führt keine Schreiboperation aus.
"""

from __future__ import annotations

import grp
import os
from pathlib import Path
import pwd
import stat


RUNTIME_USER = "e3dc-runtime"
RUNTIME_GROUP = "e3dc-runtime"
RUNTIME_UID = 991
RUNTIME_GID = 991
WEB_GID = 33
RUNTIME_HOME = "/nonexistent"
PRODUCT_ROOT = "/app/pi/Install"
VENV_ROOT = "/opt/venv"
IDENTITY_PATH = PRODUCT_ROOT + "/Installer/docker_runtime_identity.py"
BUNDLED_IDENTITY_PATH = "/usr/local/lib/e3dc-control/docker_runtime_identity.py"
CONTROL_STATE_ROOT = "/var/lib/e3dc-control/runtime"
CONTROL_STATE_DIRECTORY = CONTROL_STATE_ROOT + "/private-control"
RUNTIME_PYTHON_SCRIPTS = (
    "e3dc_live.py", "e3dc_websocket.py", "climate_live.py", "climate_control.py",
    "luxtronik/energy_manager.py", "luxtronik/lux_live.py", "idm/idm_live.py",
    "stiebel/stiebel_live.py", "dimplex/dimplex_live.py", "heizstab_manager.py",
    "wallbox_manager.py", "e3dc_mqtt_hub.py", "bluelink_client.py", "epex_manager.py",
    "Forecast/pv_forecast_service.py", "forecast_evidence_sidecar.py",
    "storage_simulator.py", "storage_manager.py", "notification_manager.py",
    "ml_predictor.py",
)


def validate_runtime_account():
    """Prüft das feste Systemkonto ohne Rückschluss aus Prozessumgebungswerten."""
    try:
        account = pwd.getpwnam(RUNTIME_USER)
        numeric_account = pwd.getpwuid(RUNTIME_UID)
        group = grp.getgrnam(RUNTIME_GROUP)
        numeric_group = grp.getgrgid(RUNTIME_GID)
        web_group = grp.getgrnam("www-data")
        account_groups = set(os.getgrouplist(RUNTIME_USER, RUNTIME_GID))
    except (KeyError, OSError) as exc:
        raise RuntimeError("Das feste Docker-Laufzeitkonto fehlt oder ist mehrdeutig.") from exc
    if (
        account.pw_name != RUNTIME_USER or numeric_account.pw_name != RUNTIME_USER
        or account.pw_uid != RUNTIME_UID or account.pw_gid != RUNTIME_GID
        or account.pw_dir != RUNTIME_HOME or account.pw_shell != "/usr/sbin/nologin"
        or group.gr_gid != RUNTIME_GID or numeric_group.gr_name != RUNTIME_GROUP
        or web_group.gr_gid != WEB_GID or account_groups != {RUNTIME_GID, WEB_GID}
    ):
        raise RuntimeError("Das Docker-Laufzeitkonto entspricht nicht dem festen Identitätsvertrag.")
    return account


def _validate_root_path(path: str, *, directory: bool) -> None:
    """Bindet jede Pfadkomponente des festen Codes mit O_NOFOLLOW an root."""
    candidate = Path(path)
    if not candidate.is_absolute() or str(candidate) != os.path.abspath(path):
        raise RuntimeError("Docker-Produktpfad ist nicht kanonisch.")
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW
    descriptor = os.open("/", flags | os.O_DIRECTORY)
    try:
        for index, component in enumerate(candidate.parts[1:]):
            is_directory = directory or index < len(candidate.parts) - 2
            child = os.open(
                component, flags | (os.O_DIRECTORY if is_directory else 0),
                dir_fd=descriptor,
            )
            os.close(descriptor)
            descriptor = child
            info = os.fstat(descriptor)
            expected_type = stat.S_ISDIR if is_directory else stat.S_ISREG
            if (
                not expected_type(info.st_mode) or info.st_uid != 0
                or info.st_gid != 0 or stat.S_IMODE(info.st_mode) & 0o022
                or (not is_directory and info.st_nlink != 1)
            ):
                raise RuntimeError("Docker-Produktpfad ist nicht ausschließlich root-kontrolliert.")
    except OSError as exc:
        raise RuntimeError("Docker-Produktpfad ist nicht sicher lesbar.") from exc
    finally:
        os.close(descriptor)


def validate_docker_product_binding() -> None:
    """Prüft festen Produktstamm, Venv-Stamm und die eigene geladene Moduldatei."""
    module_file = os.path.abspath(__file__)
    if module_file not in {IDENTITY_PATH, BUNDLED_IDENTITY_PATH}:
        raise RuntimeError("Docker-Laufzeitidentität stammt nicht aus einem freigegebenen Produktpfad.")
    _validate_root_path(PRODUCT_ROOT, directory=True)
    _validate_root_path(VENV_ROOT, directory=True)
    for marker in (
        PRODUCT_ROOT + "/VERSION",
        PRODUCT_ROOT + "/installer_main.py",
        PRODUCT_ROOT + "/Installer/installer_config.py",
        IDENTITY_PATH,
        module_file,
    ):
        _validate_root_path(marker, directory=False)


def _runtime_process_has_no_privileges() -> bool:
    """Prüft die vom Kernel ausgewiesenen Privilegien des aufrufenden Prozesses."""
    try:
        values = {}
        with open("/proc/self/status", "r", encoding="ascii") as handle:
            for line in handle:
                key, _, value = line.partition(":")
                values[key] = value.strip()
        return bool(
            values.get("Uid", "").split() == [str(RUNTIME_UID)] * 4
            and values.get("Gid", "").split() == [str(RUNTIME_GID)] * 4
            and
            values.get("NoNewPrivs") == "1"
            and all(
                key in values and int(values[key], 16) == 0
                for key in ("CapInh", "CapPrm", "CapEff", "CapBnd", "CapAmb")
            )
        )
    except (OSError, ValueError):
        return False


def is_docker_runtime_process() -> bool:
    """Erkennt ausschließlich den vollständig gebundenen, privilegienfreien Worker."""
    try:
        if (
            os.getresuid() != (RUNTIME_UID,) * 3
            or os.getresgid() != (RUNTIME_GID,) * 3
        ):
            return False
        groups = set(os.getgroups())
        if WEB_GID not in groups or not groups.issubset({RUNTIME_GID, WEB_GID}):
            return False
        if not _runtime_process_has_no_privileges():
            return False
        validate_runtime_account()
        validate_docker_product_binding()
        return True
    except (AttributeError, RuntimeError, OSError):
        return False


def runtime_control_state_directory() -> str:
    """Liest ausschließlich den vor Workerstart privilegiert vorbereiteten Leasepfad."""
    if not is_docker_runtime_process():
        raise RuntimeError("Privater Docker-Steuerzustand erfordert die gebundene Laufzeit.")
    _validate_root_path(CONTROL_STATE_ROOT, directory=True)
    flags = os.O_RDONLY | os.O_CLOEXEC | os.O_NOFOLLOW | os.O_DIRECTORY
    descriptor = os.open(CONTROL_STATE_DIRECTORY, flags)
    try:
        info = os.fstat(descriptor)
        if (
            info.st_uid != RUNTIME_UID or info.st_gid != RUNTIME_GID
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise RuntimeError("Privater Docker-Steuerzustand besitzt einen ungültigen Rechtevertrag.")
    finally:
        os.close(descriptor)
    return CONTROL_STATE_DIRECTORY
