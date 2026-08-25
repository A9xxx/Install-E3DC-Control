#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Projiziert den privilegierten HA-Manager in einen root-eigenen Laufzeitbaum.

Der Produktbaum gehört bewusst dem Installationsbenutzer. Ein Dienst mit
``User=root`` darf daraus deshalb weder Python-Code noch den Python-Interpreter
laden. Dieses Modul kopiert ausschließlich die kleine, vollständige
Standardbibliothek-Closure des HA-Managers in einen unveränderlichen,
inhaltlich adressierten Root-Baum.
"""

from __future__ import annotations

import hashlib
import os
import pwd
import shutil
import stat
import uuid
from pathlib import Path


HA_ROOT_RUNTIME_BASE = Path("/usr/local/lib/e3dc-control-ha")
HA_ROOT_RUNTIME_FILES = (
    "ha_manager.py",
    "config_secret_permissions.py",
    "quiet_logging.py",
    "ha_writer_admission.py",
    "service_catalog.py",
)
MAX_RUNTIME_FILE_BYTES = 2 * 1024 * 1024


def _read_regular_file(path: Path) -> bytes:
    """Liest eine Release-Datei ohne Symlink- oder Wechselrennen."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size < 1
            or before.st_size > MAX_RUNTIME_FILE_BYTES
        ):
            raise RuntimeError(f"Ungültige HA-Laufzeitquelle: {path}")
        payload = bytearray()
        while len(payload) <= MAX_RUNTIME_FILE_BYTES:
            chunk = os.read(
                descriptor,
                min(64 * 1024, MAX_RUNTIME_FILE_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
        named_after = path.stat(follow_symlinks=False)
        identity = lambda item: (
            item.st_dev,
            item.st_ino,
            item.st_size,
            item.st_mtime_ns,
            item.st_ctime_ns,
        )
        if (
            len(payload) > MAX_RUNTIME_FILE_BYTES
            or identity(before) != identity(after)
            or identity(after) != identity(named_after)
        ):
            raise RuntimeError(f"HA-Laufzeitquelle wechselte beim Lesen: {path}")
        return bytes(payload)
    finally:
        os.close(descriptor)


def _validate_system_parent(path: Path) -> None:
    metadata = path.lstat()
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError(f"HA-Laufzeit-Elternpfad ist nicht root-geschützt: {path}")


def _prepare_runtime_base() -> None:
    for parent in reversed(HA_ROOT_RUNTIME_BASE.parents):
        _validate_system_parent(parent)
    if os.path.lexists(HA_ROOT_RUNTIME_BASE):
        metadata = HA_ROOT_RUNTIME_BASE.lstat()
        if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(
                f"HA-Laufzeitpfad ist kein echtes Verzeichnis: {HA_ROOT_RUNTIME_BASE}"
            )
    else:
        HA_ROOT_RUNTIME_BASE.mkdir(mode=0o755)
    os.chown(HA_ROOT_RUNTIME_BASE, 0, 0)
    os.chmod(HA_ROOT_RUNTIME_BASE, 0o755)
    _validate_system_parent(HA_ROOT_RUNTIME_BASE)


def _write_root_file(directory: Path, name: str, payload: bytes) -> None:
    temporary = directory / f".{name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_CLOEXEC", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    descriptor = os.open(temporary, flags, 0o600)
    try:
        offset = 0
        while offset < len(payload):
            offset += os.write(descriptor, payload[offset:])
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, directory / name)


def _bundle_matches(directory: Path, payloads: dict[str, bytes]) -> bool:
    try:
        metadata = directory.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o755
            or set(os.listdir(directory)) != set(payloads)
        ):
            return False
        for name, payload in payloads.items():
            path = directory / name
            file_metadata = path.lstat()
            if (
                not stat.S_ISREG(file_metadata.st_mode)
                or stat.S_ISLNK(file_metadata.st_mode)
                or file_metadata.st_nlink != 1
                or file_metadata.st_uid != 0
                or file_metadata.st_gid != 0
                or stat.S_IMODE(file_metadata.st_mode) != 0o644
                or path.read_bytes() != payload
            ):
                return False
        return True
    except (OSError, UnicodeError):
        return False


def _validate_install_binding(install_root: Path, install_user: str) -> tuple[Path, str]:
    if os.geteuid() != 0:
        raise PermissionError("HA-Root-Laufzeit darf nur als root projiziert werden")
    root = Path(install_root).resolve(strict=True)
    if not root.is_absolute() or root in {Path("/"), Path("/home"), Path("/usr"), Path("/var")}:
        raise RuntimeError("HA-Produktpfad ist zu weit gefasst")
    markers = (
        root / "VERSION",
        root / "installer_main.py",
        root / "Installer" / "installer_config.py",
    )
    if not all(marker.is_file() and not marker.is_symlink() for marker in markers):
        raise RuntimeError("HA-Produktpfad besitzt nicht alle Release-Marker")
    try:
        account = pwd.getpwnam(str(install_user))
    except KeyError as exc:
        raise RuntimeError("HA-Installationsbenutzer existiert nicht") from exc
    if account.pw_uid == 0 or account.pw_name in {"root", "www-data"}:
        raise RuntimeError("HA-Installationsbenutzer ist unzulässig")
    return root, account.pw_name


def project_ha_root_runtime(
    source_installer: Path,
    *,
    install_root: Path,
    install_user: str,
) -> tuple[Path, Path, str]:
    """Erzeugt/verwendet ein root-eigenes HA-Bundle und bindet Pfad/Benutzer."""

    product_root, bound_user = _validate_install_binding(install_root, install_user)
    source = Path(source_installer).resolve(strict=True)
    payloads = {
        name: _read_regular_file(source / name)
        for name in HA_ROOT_RUNTIME_FILES
    }
    digest = hashlib.sha256()
    for name in HA_ROOT_RUNTIME_FILES:
        digest.update(name.encode("utf-8") + b"\0" + payloads[name] + b"\0")
    bundle_name = "bundle-" + digest.hexdigest()

    _prepare_runtime_base()
    bundle = HA_ROOT_RUNTIME_BASE / bundle_name
    if not _bundle_matches(bundle, payloads):
        stage = HA_ROOT_RUNTIME_BASE / f".stage-{os.getpid()}-{uuid.uuid4().hex}"
        stage.mkdir(mode=0o700)
        try:
            os.chown(stage, 0, 0)
            for name, payload in payloads.items():
                _write_root_file(stage, name, payload)
            os.chmod(stage, 0o755)
            if os.path.lexists(bundle):
                # Ein gleichnamiger, aber abweichender Altpfad wird nicht benutzt
                # und auch nicht rekursiv gelöscht. Das neue Bundle erhält einen
                # eindeutigen root-eigenen Namen.
                bundle = HA_ROOT_RUNTIME_BASE / (
                    bundle_name + "-repair-" + uuid.uuid4().hex
                )
            os.rename(stage, bundle)
        finally:
            if os.path.lexists(stage):
                shutil.rmtree(stage)
    if not _bundle_matches(bundle, payloads):
        raise RuntimeError("HA-Root-Laufzeit stimmt nach der Projektion nicht")
    return bundle, product_root, bound_user
