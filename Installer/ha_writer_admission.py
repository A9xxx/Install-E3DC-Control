#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fail-closed Schreiberfreigabe für lokale Hardware-Manager.

Der HA-Manager hält die exklusive Lease. Storage- und Wallbox-Manager prüfen
hier ausschließlich den kanonischen Rollen- und Lease-Beleg; sie erwerben oder
erneuern selbst niemals eine HA-Lease.
"""

from __future__ import annotations

import fcntl
import grp
import ipaddress
import json
import math
import os
import socket
import stat
import time
from typing import Any, Dict, Optional


CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
INSTANCE_ROLE_ANCHOR_PATH = "/etc/e3dc-control/instance_role.json"
LEASE_DIRECTORY = "/run/e3dc-control/ha"
LEASE_RECORD_NAME = "owner_lease.json"
LEASE_LOCK_NAME = "owner_lease.lock"
ROLE_RECORD_NAME = "instance_role.json"
LEASE_TTL_S = 180.0
MAX_JSON_BYTES = 64 * 1024


def _result(allowed: bool, reason: str, **values: Any) -> Dict[str, Any]:
    result: Dict[str, Any] = {
        "schema": "ha_writer_admission_v1",
        "allowed": bool(allowed),
        "reason": str(reason or "ha_writer_admission_unknown"),
    }
    result.update(values)
    return result


def _finite_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _normalized_ip(value: Any) -> str:
    try:
        text = str(value or "").strip()
        return str(ipaddress.ip_address(text)) if text else ""
    except ValueError:
        return ""


def _read_regular_json_at(
    directory_fd: int,
    name: str,
    *,
    expected_uid: int,
    expected_gid: int,
    expected_mode: int,
) -> Dict[str, Any]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(name, flags, dir_fd=directory_fd)
    try:
        before = os.fstat(fd)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != expected_uid
            or before.st_gid != expected_gid
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size > MAX_JSON_BYTES
        ):
            raise OSError("unsicherer HA-Lease-Datensatz")
        raw = bytearray()
        while len(raw) <= MAX_JSON_BYTES:
            chunk = os.read(fd, min(8192, MAX_JSON_BYTES + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(fd)
        named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            len(raw) > MAX_JSON_BYTES
            or (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
            )
            != (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
            )
            or (
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
            raise OSError("HA-Lease-Datensatz während des Lesens verändert")
        value = json.loads(bytes(raw).decode("utf-8"))
        if not isinstance(value, dict):
            raise ValueError("HA-Lease-Datensatz ist kein Objekt")
        return value
    finally:
        os.close(fd)


def _read_canonical_config(path: str) -> Dict[str, Any]:
    absolute = os.path.abspath(path)
    parent, name = os.path.split(absolute)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    directory_flags |= getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, directory_flags)
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(name, flags, dir_fd=directory_fd)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1 or before.st_size > MAX_JSON_BYTES:
                raise OSError("unsichere HA-Konfigurationsdatei")
            raw = bytearray()
            while len(raw) <= MAX_JSON_BYTES:
                chunk = os.read(fd, min(8192, MAX_JSON_BYTES + 1 - len(raw)))
                if not chunk:
                    break
                raw.extend(chunk)
            after = os.fstat(fd)
            named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)
            if (
                len(raw) > MAX_JSON_BYTES
                or (
                    before.st_dev,
                    before.st_ino,
                    before.st_size,
                    before.st_mtime_ns,
                    before.st_ctime_ns,
                )
                != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                    after.st_mtime_ns,
                    after.st_ctime_ns,
                )
                or (
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
                raise OSError("HA-Konfiguration während des Lesens verändert")
            value = json.loads(bytes(raw).decode("utf-8"))
            if not isinstance(value, dict):
                raise ValueError("HA-Konfiguration ist kein Objekt")
            return {str(key).strip().lower(): item for key, item in value.items()}
        finally:
            os.close(fd)
    finally:
        os.close(directory_fd)


def _read_role_anchor(path: str) -> Dict[str, Any]:
    parent, name = os.path.split(os.path.abspath(path))
    directory_fd, _info = _open_private_lease_directory(parent)
    try:
        return _read_regular_json_at(
            directory_fd,
            name,
            expected_uid=0,
            expected_gid=int(grp.getgrnam("www-data").gr_gid),
            expected_mode=0o640,
        )
    finally:
        os.close(directory_fd)


def instance_role_anchor_matches(
    mode: str,
    *,
    peer_ip: str = "",
    anchor_path: str = INSTANCE_ROLE_ANCHOR_PATH,
    hostname: Optional[str] = None,
) -> bool:
    """Prüft die privilegiert projizierte Rolle ohne sie zu verändern."""

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"off", "master", "slave", "shadow"}:
        return False
    normalized_peer = _normalized_ip(peer_ip) if normalized_mode in {"master", "slave"} else ""
    if normalized_mode in {"master", "slave"} and not normalized_peer:
        return False
    try:
        anchor = _read_role_anchor(anchor_path)
    except Exception:
        return False
    return bool(
        anchor.get("schema") == 1
        and anchor.get("node_id") == str(hostname or socket.gethostname()).strip()
        and str(anchor.get("mode") or "").strip().lower() == normalized_mode
        and _normalized_ip(anchor.get("peer_ip")) == normalized_peer
    )


def project_instance_role_anchor(
    mode: str,
    *,
    peer_ip: str = "",
    anchor_path: str = INSTANCE_ROLE_ANCHOR_PATH,
    hostname: Optional[str] = None,
    _expected_existing: Optional[tuple[str, str]] = None,
) -> bool:
    """Erzeugt den Rollenanker einmalig; bestehende Rollen werden nie ersetzt."""

    if os.geteuid() != 0:
        raise PermissionError("Instanzrollen-Anker benötigt root")
    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in {"off", "master", "slave", "shadow"}:
        raise ValueError("ungültige Instanzrolle")
    normalized_peer = _normalized_ip(peer_ip) if normalized_mode in {"master", "slave"} else ""
    if normalized_mode in {"master", "slave"} and not normalized_peer:
        raise ValueError("HA-Instanzrolle benötigt eine gültige Peer-IP")

    absolute = os.path.abspath(anchor_path)
    parent, name = os.path.split(absolute)
    grandparent, parent_name = os.path.split(parent)
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    grandparent_fd = os.open(grandparent, flags)
    parent_fd = -1
    try:
        grandparent_info = os.fstat(grandparent_fd)
        if (
            not stat.S_ISDIR(grandparent_info.st_mode)
            or grandparent_info.st_uid != 0
            or grandparent_info.st_gid != 0
            or stat.S_IMODE(grandparent_info.st_mode) != 0o755
        ):
            raise OSError("unsicherer Elternpfad des Instanzrollen-Ankers")
        try:
            os.mkdir(parent_name, 0o755, dir_fd=grandparent_fd)
        except FileExistsError:
            pass
        parent_fd = os.open(parent_name, flags, dir_fd=grandparent_fd)
    finally:
        os.close(grandparent_fd)
    try:
        parent_info = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != 0
            or parent_info.st_gid != 0
            or stat.S_IMODE(parent_info.st_mode) != 0o755
        ):
            raise OSError("unsicheres Verzeichnis des Instanzrollen-Ankers")
        lock_name = ".instance_role.lock"
        lock_flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_CLOEXEC", 0)
        lock_flags |= getattr(os, "O_NOFOLLOW", 0)
        lock_fd = os.open(lock_name, lock_flags, 0o600, dir_fd=parent_fd)
        try:
            os.fchown(lock_fd, 0, 0)
            os.fchmod(lock_fd, 0o600)
            lock_info = os.fstat(lock_fd)
            named_lock = os.stat(lock_name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                not stat.S_ISREG(lock_info.st_mode)
                or lock_info.st_nlink != 1
                or lock_info.st_uid != 0
                or lock_info.st_gid != 0
                or stat.S_IMODE(lock_info.st_mode) != 0o600
                or (lock_info.st_dev, lock_info.st_ino)
                != (named_lock.st_dev, named_lock.st_ino)
            ):
                raise OSError("unsichere Instanzrollen-Transaktionssperre")
            fcntl.flock(lock_fd, fcntl.LOCK_EX)

            existing_token = None
            try:
                existing = _read_regular_json_at(
                    parent_fd,
                    name,
                    expected_uid=0,
                    expected_gid=int(grp.getgrnam("www-data").gr_gid),
                    expected_mode=0o640,
                )
            except FileNotFoundError:
                existing = None
            else:
                existing_info = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
                existing_token = (
                    existing_info.st_dev,
                    existing_info.st_ino,
                    existing_info.st_size,
                    existing_info.st_mtime_ns,
                    existing_info.st_ctime_ns,
                    existing_info.st_uid,
                    existing_info.st_gid,
                    stat.S_IMODE(existing_info.st_mode),
                    existing_info.st_nlink,
                )
            if existing is not None:
                existing_matches_target = bool(
                    existing.get("schema") == 1
                    and existing.get("node_id")
                    == str(hostname or socket.gethostname()).strip()
                    and str(existing.get("mode") or "").strip().lower()
                    == normalized_mode
                    and _normalized_ip(existing.get("peer_ip")) == normalized_peer
                )
                if existing_matches_target:
                    return True
                if _expected_existing is None:
                    return False
                expected_mode, expected_peer = _expected_existing
                if not (
                    existing.get("schema") == 1
                    and existing.get("node_id")
                    == str(hostname or socket.gethostname()).strip()
                    and str(existing.get("mode") or "").strip().lower()
                    == expected_mode
                    and _normalized_ip(existing.get("peer_ip")) == expected_peer
                ):
                    return False
            elif _expected_existing is not None:
                return False

            payload = json.dumps(
                {
                    "schema": 1,
                    "node_id": str(hostname or socket.gethostname()).strip(),
                    "mode": normalized_mode,
                    "peer_ip": normalized_peer,
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            temp_name = ".%s.%d.%d.tmp" % (name, os.getpid(), time.monotonic_ns())
            create_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
            create_flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(temp_name, create_flags, 0o640, dir_fd=parent_fd)
            try:
                os.fchown(descriptor, 0, int(grp.getgrnam("www-data").gr_gid))
                os.fchmod(descriptor, 0o640)
                offset = 0
                while offset < len(payload):
                    offset += os.write(descriptor, payload[offset:])
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            try:
                if _expected_existing is None:
                    # Create-once: Der Name muss unter derselben Root-Sperre bis
                    # unmittelbar vor dem Commit nachweislich frei bleiben.
                    try:
                        os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                    except FileNotFoundError:
                        pass
                    else:
                        raise OSError("Instanzrollen-Anker erschien vor dem Erstcommit")
                else:
                    # Expliziter Rollenwechsel: exakt das zuvor gelesene alte
                    # Inode-/Byte-Semantik-Preimage muss noch am Namen liegen.
                    if existing_token is None:
                        raise OSError("Instanzrollen-Anker fehlt vor dem CAS-Commit")
                    rebound = _read_regular_json_at(
                        parent_fd,
                        name,
                        expected_uid=0,
                        expected_gid=int(grp.getgrnam("www-data").gr_gid),
                        expected_mode=0o640,
                    )
                    rebound_info = os.stat(
                        name,
                        dir_fd=parent_fd,
                        follow_symlinks=False,
                    )
                    rebound_token = (
                        rebound_info.st_dev,
                        rebound_info.st_ino,
                        rebound_info.st_size,
                        rebound_info.st_mtime_ns,
                        rebound_info.st_ctime_ns,
                        rebound_info.st_uid,
                        rebound_info.st_gid,
                        stat.S_IMODE(rebound_info.st_mode),
                        rebound_info.st_nlink,
                    )
                    expected_mode, expected_peer = _expected_existing
                    if (
                        rebound_token != existing_token
                        or rebound.get("schema") != 1
                        or rebound.get("node_id")
                        != str(hostname or socket.gethostname()).strip()
                        or str(rebound.get("mode") or "").strip().lower()
                        != expected_mode
                        or _normalized_ip(rebound.get("peer_ip")) != expected_peer
                    ):
                        raise OSError("Instanzrollen-Anker driftete vor dem CAS-Commit")
                os.replace(temp_name, name, src_dir_fd=parent_fd, dst_dir_fd=parent_fd)
                os.fsync(parent_fd)
            finally:
                try:
                    os.unlink(temp_name, dir_fd=parent_fd)
                except FileNotFoundError:
                    pass
        finally:
            os.close(lock_fd)
    finally:
        os.close(parent_fd)

    return instance_role_anchor_matches(
        normalized_mode,
        peer_ip=normalized_peer,
        anchor_path=absolute,
        hostname=hostname,
    )


def transition_instance_role_anchor(
    mode: str,
    *,
    peer_ip: str = "",
    expected_mode: str,
    expected_peer_ip: str = "",
    anchor_path: str = INSTANCE_ROLE_ANCHOR_PATH,
    hostname: Optional[str] = None,
) -> bool:
    """Ersetzt den Anker nur bei explizit gebundenem Rollen-Preimage."""

    old_mode = str(expected_mode or "").strip().lower()
    if old_mode not in {"off", "master", "slave", "shadow"}:
        return False
    old_peer = _normalized_ip(expected_peer_ip) if old_mode in {"master", "slave"} else ""
    if old_mode in {"master", "slave"} and not old_peer:
        return False
    return project_instance_role_anchor(
        mode,
        peer_ip=peer_ip,
        anchor_path=anchor_path,
        hostname=hostname,
        _expected_existing=(old_mode, old_peer),
    )


def _open_private_lease_directory(path: str) -> tuple[int, os.stat_result]:
    parent, name = os.path.split(os.path.abspath(path))
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_DIRECTORY", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    parent_fd = os.open(parent, flags)
    try:
        parent_info = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != 0
            or parent_info.st_gid != 0
            or stat.S_IMODE(parent_info.st_mode) != 0o755
        ):
            raise OSError("unsicherer HA-Lease-Namensraum")
        fd = os.open(name, flags, dir_fd=parent_fd)
        info = os.fstat(fd)
        if (
            not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o755
        ):
            os.close(fd)
            raise OSError("unsicheres HA-Lease-Verzeichnis")
        return fd, info
    finally:
        os.close(parent_fd)


def _open_lock_and_probe(
    directory_fd: int,
    expected_uid: int,
    expected_gid: int,
) -> tuple[int, bool]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(LEASE_LOCK_NAME, flags, dir_fd=directory_fd)
    info = os.fstat(fd)
    if (
        not stat.S_ISREG(info.st_mode)
        or info.st_nlink != 1
        or info.st_uid != expected_uid
        or info.st_gid != expected_gid
        or stat.S_IMODE(info.st_mode) != 0o640
    ):
        os.close(fd)
        raise OSError("unsichere HA-Lease-Sperrdatei")
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return fd, True
    except OSError:
        os.close(fd)
        raise
    fcntl.flock(fd, fcntl.LOCK_UN)
    return fd, False


def _lock_is_held_elsewhere(fd: int) -> bool:
    """Prüft denselben Lock-Inode unmittelbar vor der Freigabe erneut."""

    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        return True
    fcntl.flock(fd, fcntl.LOCK_UN)
    return False


def _lock_owner_id(fd: int) -> str:
    os.lseek(fd, 0, os.SEEK_SET)
    raw = os.read(fd, 512)
    if not raw or len(raw) >= 512:
        return ""
    try:
        return raw.decode("utf-8").strip()
    except UnicodeDecodeError:
        return ""


def evaluate_writer_admission(
    *,
    config_path: str = CONFIG_PATH,
    lease_directory: str = LEASE_DIRECTORY,
    now_s: Optional[float] = None,
    hostname: Optional[str] = None,
    allow_off_anchor_config_repair: bool = False,
) -> Dict[str, Any]:
    """Prüft Rolle, tatsächlichen Lockhalter und frischen Lease-Datensatz."""

    now = _finite_float(time.time() if now_s is None else now_s)
    if now is None or now < 0:
        return _result(False, "ha_time_invalid")
    local_node = str(hostname or socket.gethostname()).strip()
    config_repair_fallback = False
    try:
        config = _read_canonical_config(config_path)
    except FileNotFoundError:
        if not (
            allow_off_anchor_config_repair
            and instance_role_anchor_matches("off", hostname=local_node)
        ):
            return _result(False, "ha_config_missing")
        config = {"ha_mode": "off"}
        config_repair_fallback = True
    except Exception as exc:
        if not (
            allow_off_anchor_config_repair
            and instance_role_anchor_matches("off", hostname=local_node)
        ):
            return _result(False, "ha_config_invalid", error=type(exc).__name__)
        config = {"ha_mode": "off"}
        config_repair_fallback = True

    mode = str(config.get("ha_mode") or "").strip().lower()
    if mode not in {"off", "master", "slave", "shadow"}:
        return _result(False, "ha_mode_missing_or_invalid", mode=mode)
    peer_ip = _normalized_ip(config.get("ha_peer_ip"))
    if not instance_role_anchor_matches(
        mode,
        peer_ip=peer_ip,
        hostname=local_node,
    ):
        return _result(False, "ha_instance_role_anchor_mismatch", mode=mode)
    if mode == "shadow":
        return _result(False, "ha_shadow_writer_forbidden", mode=mode)

    try:
        directory_fd, directory_info = _open_private_lease_directory(lease_directory)
    except FileNotFoundError:
        if mode == "off":
            return _result(True, "ha_off_without_lease", mode=mode)
        return _result(False, "ha_lease_directory_missing", mode=mode)
    except Exception as exc:
        return _result(False, "ha_lease_directory_invalid", mode=mode, error=type(exc).__name__)

    lock_fd = -1
    try:
        try:
            www_data_gid = int(grp.getgrnam("www-data").gr_gid)
        except KeyError:
            return _result(False, "ha_service_group_missing", mode=mode)
        try:
            lock_fd, lock_held = _open_lock_and_probe(
                directory_fd,
                0,
                www_data_gid,
            )
        except FileNotFoundError:
            if mode == "off":
                lock_fd = -1
                lock_held = False
            else:
                return _result(False, "ha_lease_lock_missing", mode=mode)
        except Exception as exc:
            return _result(False, "ha_lease_lock_invalid", mode=mode, error=type(exc).__name__)

        if mode == "off":
            return _result(
                not lock_held,
                (
                    "ha_off_config_repair_clear"
                    if config_repair_fallback and not lock_held
                    else "ha_off_clear"
                    if not lock_held
                    else "ha_off_lease_still_held"
                ),
                mode=mode,
                lease_lock_held=lock_held,
                config_repair_fallback=config_repair_fallback,
            )

        try:
            role_record = _read_regular_json_at(
                directory_fd,
                ROLE_RECORD_NAME,
                expected_uid=0,
                expected_gid=www_data_gid,
                expected_mode=0o640,
            )
        except FileNotFoundError:
            return _result(False, "ha_role_anchor_missing", mode=mode)
        except Exception as exc:
            return _result(
                False,
                "ha_role_anchor_invalid",
                mode=mode,
                error=type(exc).__name__,
            )
        anchored_mode = str(role_record.get("mode") or "").strip().lower()
        role_anchor_valid = bool(
            role_record.get("schema") == 1
            and role_record.get("node_id") == local_node
            and anchored_mode == mode
        )
        if not role_anchor_valid:
            return _result(
                False,
                "ha_role_anchor_mismatch",
                mode=mode,
                anchored_mode=anchored_mode,
            )

        if not lock_held:
            return _result(False, "ha_lease_not_held", mode=mode, lease_lock_held=False)

        if not peer_ip:
            return _result(False, "ha_peer_invalid", mode=mode, lease_lock_held=True)
        try:
            record = _read_regular_json_at(
                directory_fd,
                LEASE_RECORD_NAME,
                expected_uid=0,
                expected_gid=www_data_gid,
                expected_mode=0o640,
            )
        except FileNotFoundError:
            return _result(False, "ha_lease_record_missing", mode=mode, lease_lock_held=True)
        except Exception as exc:
            return _result(
                False,
                "ha_lease_record_invalid",
                mode=mode,
                lease_lock_held=True,
                error=type(exc).__name__,
            )

        renewed_at = _finite_float(record.get("renewed_at"))
        expires_at = _finite_float(record.get("expires_at"))
        node_id = str(record.get("node_id") or "").strip()
        owner_id = str(record.get("owner_id") or "").strip()
        record_peer = _normalized_ip(record.get("peer_ip"))
        anchored_peer = _normalized_ip(role_record.get("peer_ip"))
        lock_owner_id = _lock_owner_id(lock_fd)
        lock_still_held = _lock_is_held_elsewhere(lock_fd)
        valid = bool(
            record.get("schema") == 1
            and record.get("context_valid") is True
            and record.get("released") is False
            and owner_id
            and lock_owner_id == owner_id
            and lock_still_held
            and node_id
            and node_id == local_node
            and str(record.get("mode") or "").strip().lower() == mode
            and record_peer == peer_ip
            and anchored_peer == peer_ip
            and renewed_at is not None
            and expires_at is not None
            and renewed_at <= now + 5.0
            and now - renewed_at <= LEASE_TTL_S
            and expires_at > now
            and expires_at <= renewed_at + LEASE_TTL_S + 5.0
        )
        return _result(
            valid,
            "ha_lease_valid" if valid else "ha_lease_context_invalid",
            mode=mode,
            owner_id=owner_id,
            node_id=node_id,
            peer_ip=peer_ip,
            renewed_at=renewed_at,
            expires_at=expires_at,
            lease_lock_held=lock_still_held,
            lock_owner_id=lock_owner_id,
        )
    finally:
        if lock_fd >= 0:
            os.close(lock_fd)
        os.close(directory_fd)


def writer_admission_allowed(**kwargs: Any) -> bool:
    return evaluate_writer_admission(**kwargs).get("allowed") is True
