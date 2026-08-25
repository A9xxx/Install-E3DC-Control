#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
E3DC-Control High Availability (HA) Manager
Überwacht den Master/Slave Status, synchronisiert Daten und übernimmt im Notfall.
"""

import os
import time
import subprocess
import json
import glob
import ipaddress
from datetime import datetime
import logging
import pwd
import grp
import socket
import stat
import uuid
import sys
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - HA owner leases require POSIX flock
    fcntl = None

try:
    from Installer.config_secret_permissions import (
        config_secret_dir_mode,
        config_secret_dir_mode_text,
        config_secret_file_mode,
        config_secret_file_mode_text,
    )
    from Installer.quiet_logging import install_quiet_info_filter
    from Installer.ha_writer_admission import instance_role_anchor_matches
except ImportError:
    from config_secret_permissions import (
        config_secret_dir_mode,
        config_secret_dir_mode_text,
        config_secret_file_mode,
        config_secret_file_mode_text,
    )
    from quiet_logging import install_quiet_info_filter
    from ha_writer_admission import instance_role_anchor_matches

PATHS_FILE = "/var/www/html/e3dc_paths.json"
_paths_warning_logged = False


def read_paths_config():
    """Read e3dc_paths.json defensively; updates can briefly leave it empty."""
    global _paths_warning_logged
    if not os.path.exists(PATHS_FILE):
        return {}
    try:
        with open(PATHS_FILE, "r", encoding="utf-8-sig") as f:
            raw = f.read().strip()
        if not raw:
            return {}
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except Exception as exc:
        if "logger" in globals() and not _paths_warning_logged:
            logger.warning(f"{PATHS_FILE} nicht lesbar, nutze HA-Fallbackwerte: {exc}")
            _paths_warning_logged = True
        return {}


def _validated_install_path(raw_path):
    """Normalisiere den Installationspfad aus der Web-Config defensiv."""
    candidate = Path(str(raw_path or "").strip())
    if not candidate.is_absolute():
        raise ValueError("install_path ist nicht absolut")
    resolved = candidate.resolve(strict=True)
    markers = (
        resolved / "VERSION",
        resolved / "installer_main.py",
        resolved / "Installer" / "installer_config.py",
    )
    if not all(marker.is_file() for marker in markers):
        raise ValueError("install_path besitzt nicht alle Release-Marker")
    return str(resolved).rstrip("/")


def validate_peer_ip(peer_ip):
    """Akzeptiert nur numerische IPv4/IPv6-Adressen für HA-Remote-Ziele."""
    try:
        value = str(peer_ip or "").strip()
        if not value:
            return ""
        return str(ipaddress.ip_address(value))
    except ValueError:
        return ""


def _rsync_remote(user, host_ip, path):
    host = f"[{host_ip}]" if ":" in str(host_ip) else str(host_ip)
    return f"{user}@{host}:{path}"


def _runtime_binding(argv=None):
    """Bindet den privilegierten HA-Prozess an Root-Unit-Argumente.

    Der Fallback hält reine Modulimporte und unprivilegierte Diagnoseaufrufe
    kompatibel. Der eigentliche Root-Daemon verlangt weiter unten zwingend die
    explizite Bindung aus seiner root-eigenen systemd-Unit.
    """

    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] == "--install-root":
        if len(args) != 4 or args[2] != "--install-user":
            raise RuntimeError("HA-Root-Laufzeitargumente sind unvollständig")
        install_path = _validated_install_path(args[1])
        try:
            account = pwd.getpwnam(str(args[3]))
        except KeyError as exc:
            raise RuntimeError("HA-Installationsbenutzer existiert nicht") from exc
        if account.pw_uid == 0 or account.pw_name in {"root", "www-data"}:
            raise RuntimeError("HA-Installationsbenutzer ist unzulässig")
        return install_path, account.pw_name, True
    fallback = _validated_install_path(Path(__file__).resolve().parent.parent)
    try:
        fallback_user = pwd.getpwuid(Path(fallback).stat().st_uid).pw_name
    except (KeyError, OSError):
        fallback_user = ""
    return fallback, fallback_user, False


# Die root-eigene HA-Unit übergibt Produktpfad und Installationsbenutzer als
# feste Argumente. Der Produktbaum wird damit nur noch als Datenziel benutzt.
INSTALL_PATH, INSTALL_USER, PRIVILEGED_RUNTIME_BOUND = _runtime_binding()
p_data = read_paths_config()
if p_data.get('install_path'):
    configured_install_path = _validated_install_path(p_data['install_path'])
    if configured_install_path != INSTALL_PATH:
        raise RuntimeError("HA-Pfadmetadaten zeigen auf einen anderen Release-Baum")

CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
WEB_DATA_DIR = "/var/www/html/data"
WALLBOX_MODE5_USER_START_REQUEST_FILE = os.path.join(
    WEB_DATA_DIR,
    "wallbox_mode5_user_start_request.json",
)
LOG_DIR = "/var/www/html/logs"
NOTIFY_SCRIPT = "/usr/local/bin/boot_notify.sh"
# Der HA-Owner ist Hardwareautorität und darf deshalb nicht unter einem vom
# Webdienst umbenennbaren Verzeichnis leben. Der HA-Dienst läuft als root;
# Storage/Wallbox lesen den festen Beleg über ihre www-data-Gruppe.
HA_LEASE_DIR = "/run/e3dc-control/ha"
HA_LEASE_FILE = os.path.join(HA_LEASE_DIR, "owner_lease.json")
HA_LEASE_LOCK_FILE = os.path.join(HA_LEASE_DIR, "owner_lease.lock")
HA_ROLE_FILE = os.path.join(HA_LEASE_DIR, "instance_role.json")
HA_LEASE_TTL_S = 180.0

LEGACY_E3DC_SERVICE = "e3dc.service"
NATIVE_LIVE_SERVICE = "e3dc-live.service"

HA_LOCAL_CONFIG_KEYS = {
    "ha_mode",
    "ha_peer_ip",
    "telegram_device_name",
    "install_path",
    "install_user",
    "home_dir",
    "venv_name",
    "venv_path",
}
SECRET_CONFIG_KEY_PARTS = (
    "password",
    "passwd",
    "passwort",
    "token",
    "secret",
    "api_key",
    "apikey",
    "aes",
    "private",
)
SECRET_CONFIG_EXACT_KEYS = {
    "rscp_pw",
    "rscp_password",
    "telegram_chat_id",
    "web_pin",
}


def catalog_managed_services():
    """Liefert alle katalogisierten Dienste, die im HA-Standby exklusiv bleiben."""
    fallback = [
        "e3dc-live.service",
        "e3dc-storage-manager.service",
        "e3dc-storage-simulator.service",
        "e3dc-epex-manager.service",
        "e3dc-weather-manager.service",
        "e3dc-wallbox-manager.service",
        "energy_manager.service",
        "e3dc-idm-live.service",
        "e3dc-lux-live.service",
        "e3dc-stiebel-live.service",
        "e3dc-dimplex-live.service",
        "e3dc-heizstab.service",
        "e3dc-climate-live.service",
        "e3dc-climate-control.service",
        "e3dc-forecast-evidence.service",
        "e3dc-bluelink.service",
        "e3dc-mqtt-hub.service",
        "e3dc-matter-bridge.service",
        "e3dc-notifier.service",
        "e3dc-websocket.service",
        "e3dc-shadow-sync.service",
    ]
    try:
        import sys as _sys
        installer_dir = os.path.dirname(os.path.abspath(__file__))
        if installer_dir not in _sys.path:
            _sys.path.insert(0, installer_dir)
        from service_catalog import allowed_services
        units = list(allowed_services())
    except Exception:
        units = fallback

    services = [LEGACY_E3DC_SERVICE]
    for unit in units:
        unit_name = str(unit or "").strip()
        if not unit_name:
            continue
        if not unit_name.endswith(".service"):
            unit_name += ".service"
        if unit_name in {
            LEGACY_E3DC_SERVICE,
            "e3dc-ha.service",
            "e3dc-shadow-sync.service",
        }:
            continue
        if unit_name not in services:
            services.append(unit_name)
    return services


# Dienste, die im HA-Standby nicht laufen duerfen.
#
# Ein Slave im Standby muss alle Steuer-, Schreib-, Integrations- und
# Simulationsdienste aus dem zentralen Katalog hart gestoppt halten. Der HA-
# Manager selbst bleibt ausgenommen, damit Failover/Fallback weiter arbeitet.
MANAGED_SERVICES = catalog_managed_services()

def setup_logging():
    """Schreibt den privilegierten HA-Prozess ausschließlich ins Journal."""

    logger = logging.getLogger("HAManager")
    logger.setLevel(logging.INFO)
    handler = logging.StreamHandler()
    formatter = logging.Formatter('%(asctime)s - [%(levelname)s] - %(message)s', datefmt='%d.%m %H:%M:%S')
    handler.setFormatter(formatter)
    if not logger.handlers:
        logger.addHandler(handler)
    install_quiet_info_filter(
        logger,
        min_interval_s=900.0,
        warning_min_interval_s=30.0,
        warning_max_interval_s=3600.0,
    )
    return logger

logger = setup_logging()


def _ensure_private_runtime_dir(path):
    try:
        if os.geteuid() != 0:
            return False
        namespace_root = os.path.dirname(path)
        for directory in (namespace_root, path):
            os.makedirs(directory, mode=0o755, exist_ok=True)
            info = os.lstat(directory)
            if (
                stat.S_ISLNK(info.st_mode)
                or not stat.S_ISDIR(info.st_mode)
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o755
            ):
                return False
    except OSError:
        return False
    return True


def _read_private_json(path, max_bytes=65536):
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        www_data_gid = grp.getgrnam("www-data").gr_gid
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_nlink != 1
            or info.st_uid != 0
            or info.st_gid != www_data_gid
        ):
            raise OSError("owner lease is not a single regular file")
        if stat.S_IMODE(info.st_mode) != 0o640:
            raise OSError("owner lease permissions are not private")
        if info.st_size > max_bytes:
            raise OSError("owner lease is too large")
        raw = b""
        while len(raw) <= max_bytes:
            chunk = os.read(fd, min(8192, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw += chunk
        if len(raw) > max_bytes:
            raise OSError("owner lease is too large")
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("owner lease is not an object")
        return payload
    finally:
        os.close(fd)


def _write_private_json(path, payload):
    directory = os.path.dirname(path)
    if not _ensure_private_runtime_dir(directory):
        raise OSError("owner lease directory is not private")
    temp_path = "%s.tmp.%d.%s" % (path, os.getpid(), uuid.uuid4().hex)
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(temp_path, flags, 0o640)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise OSError("owner lease is not a single regular file")
        os.fchown(fd, 0, grp.getgrnam("www-data").gr_gid)
        os.fchmod(fd, 0o640)
        raw = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
        offset = 0
        while offset < len(raw):
            offset += os.write(fd, raw[offset:])
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.replace(temp_path, path)
        directory_fd = os.open(
            directory,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        try:
            if os.path.lexists(temp_path):
                os.unlink(temp_path)
        except OSError:
            pass


def _write_role_anchor(mode, peer_ip=""):
    """Projiziert nur die bereits privilegiert bestätigte Rolle zur Laufzeit."""

    normalized_mode = str(mode or "").strip().lower()
    if normalized_mode not in ("off", "master", "slave", "shadow"):
        raise ValueError("ungültige HA-/Shadow-Rolle")
    normalized_peer = validate_peer_ip(peer_ip) if normalized_mode in ("master", "slave") else ""
    if normalized_mode in ("master", "slave") and not normalized_peer:
        raise ValueError("HA-Rolle ohne gültigen Peer")
    if not instance_role_anchor_matches(
        normalized_mode,
        peer_ip=normalized_peer,
    ):
        raise OSError("persistenter Instanzrollen-Anker stimmt nicht überein")
    _write_private_json(
        HA_ROLE_FILE,
        {
            "schema": 1,
            "node_id": socket.gethostname(),
            "mode": normalized_mode,
            "peer_ip": normalized_peer,
            "written_at": time.time(),
        },
    )
    return True


class OwnerLease:
    """Prozesslokale exklusive Schreiber-Lease mit prüfbarem TTL-Datensatz."""

    def __init__(
        self,
        owner_id=None,
        node_id=None,
        ttl_s=HA_LEASE_TTL_S,
        lease_path=HA_LEASE_FILE,
        lock_path=HA_LEASE_LOCK_FILE,
        clock=time.time,
    ):
        self.node_id = str(node_id or socket.gethostname())
        self.owner_id = str(owner_id or "%s:%d:%s" % (self.node_id, os.getpid(), uuid.uuid4().hex))
        self.ttl_s = max(30.0, float(ttl_s))
        self.lease_path = str(lease_path)
        self.lock_path = str(lock_path)
        self.clock = clock
        self._lock_file = None
        self._mode = ""
        self._peer_ip = ""
        self.resumed_same_context = False
        self.last_error = ""

    def _open_lock(self):
        if fcntl is None:
            self.last_error = "posix_flock_unavailable"
            return False
        if not _ensure_private_runtime_dir(os.path.dirname(self.lock_path)):
            self.last_error = "lease_directory_insecure"
            return False
        flags = os.O_RDWR | os.O_CREAT
        if hasattr(os, "O_CLOEXEC"):
            flags |= os.O_CLOEXEC
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            fd = os.open(self.lock_path, flags, 0o640)
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise OSError("owner lock is not a single regular file")
            os.fchown(fd, 0, grp.getgrnam("www-data").gr_gid)
            os.fchmod(fd, 0o640)
            info = os.fstat(fd)
            if (
                info.st_uid != 0
                or info.st_gid != grp.getgrnam("www-data").gr_gid
                or stat.S_IMODE(info.st_mode) != 0o640
            ):
                raise OSError("owner lock permissions are not private")
            lock_file = os.fdopen(fd, "r+", encoding="utf-8")
            fd = -1
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            lock_file.seek(0)
            lock_file.truncate(0)
            lock_file.write(self.owner_id + "\n")
            lock_file.flush()
            os.fsync(lock_file.fileno())
        except (OSError, BlockingIOError) as exc:
            try:
                if "lock_file" in locals():
                    lock_file.close()
                elif "fd" in locals() and fd >= 0:
                    os.close(fd)
            except OSError:
                pass
            self.last_error = "lease_lock_busy_or_invalid:%s" % type(exc).__name__
            return False
        self._lock_file = lock_file
        return True

    def _read_record(self):
        try:
            return _read_private_json(self.lease_path)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            self.last_error = "lease_record_invalid:%s" % type(exc).__name__
            return None

    def _write_record(self, context_valid=True, released=False, reason=""):
        now = float(self.clock())
        payload = {
            "schema": 1,
            "owner_id": self.owner_id,
            "node_id": self.node_id,
            "mode": self._mode,
            "peer_ip": self._peer_ip,
            "renewed_at": now,
            "expires_at": now if released else now + self.ttl_s,
            "context_valid": bool(context_valid and not released),
            "released": bool(released),
            "reason": str(reason or ""),
        }
        _write_private_json(self.lease_path, payload)
        return payload

    def _same_context_predecessor(self, prior, mode, peer_ip, now=None):
        """Erkennt die aufgegebene Lease desselben lokalen HA-Prozesses."""

        if not isinstance(prior, dict):
            return False
        try:
            renewed_at = float(prior.get("renewed_at"))
            expires_at = float(prior.get("expires_at"))
            current_time = float(self.clock() if now is None else now)
        except (TypeError, ValueError):
            return False
        return bool(
            prior.get("schema") == 1
            and prior.get("context_valid") is True
            and prior.get("released") is False
            and str(prior.get("owner_id") or "")
            and str(prior.get("owner_id") or "") != self.owner_id
            and str(prior.get("node_id") or "") == self.node_id
            and str(prior.get("mode") or "") == str(mode)
            and validate_peer_ip(prior.get("peer_ip")) == peer_ip
            and renewed_at <= current_time + 5.0
            and current_time - renewed_at <= self.ttl_s
            and expires_at > current_time
            and expires_at <= renewed_at + self.ttl_s + 5.0
        )

    def resume_same_context_only(self, mode, peer_ip, context_valid=True):
        """Übernimmt ausschließlich den frischen eigenen HA-Vorzustand.

        Diese Vorstufe darf einen bestätigten lokalen Neustart fortsetzen,
        bevor der Peer erneut erreichbar ist. Ein neuer, abgelaufener oder
        fremder Owner erhält hier ausdrücklich keine Schreiberfreigabe.
        """

        self.resumed_same_context = False
        normalized_peer = validate_peer_ip(peer_ip)
        if not context_valid or mode not in ("master", "slave") or not normalized_peer:
            self.last_error = "lease_context_invalid"
            return False
        if self._lock_file is not None:
            return False
        if not self._open_lock():
            return False
        self._mode = str(mode)
        self._peer_ip = normalized_peer
        prior = self._read_record()
        now = float(self.clock())
        if not self._same_context_predecessor(prior, self._mode, normalized_peer, now=now):
            self.last_error = self.last_error or "same_context_predecessor_missing_or_invalid"
            self._unlock()
            return False
        try:
            self._write_record(context_valid=True)
        except Exception as exc:
            self.last_error = "lease_record_write_failed:%s" % type(exc).__name__
            self._unlock()
            return False
        self.resumed_same_context = True
        self.last_error = ""
        return True

    def acquire(self, mode, peer_ip, context_valid=True):
        self.resumed_same_context = False
        normalized_peer = validate_peer_ip(peer_ip)
        if not context_valid or mode not in ("master", "slave") or not normalized_peer:
            self.last_error = "lease_context_invalid"
            return False
        if self._lock_file is not None:
            return self.renew(mode, normalized_peer, context_valid=context_valid)
        if not self._open_lock():
            return False
        self._mode = str(mode)
        self._peer_ip = normalized_peer
        prior = self._read_record()
        now = float(self.clock())
        unexpired_foreign_owner = bool(
            prior
            and not prior.get("released")
            and float(prior.get("expires_at", 0.0) or 0.0) > now
            and str(prior.get("owner_id") or "") != self.owner_id
        )
        # Die erfolgreich übernommene flock-Sperre belegt, dass der frühere
        # lokale HA-Prozess nicht mehr Schreiber sein kann. Nur sein exakt
        # gleicher Knoten-/Rollen-/Peer-Kontext darf deshalb ohne die alte
        # TTL-Wartezeit fortgesetzt werden; jede fremde Lease bleibt gesperrt.
        same_context_restart = unexpired_foreign_owner and self._same_context_predecessor(
            prior,
            self._mode,
            normalized_peer,
            now=now,
        )
        if prior is None or (unexpired_foreign_owner and not same_context_restart):
            self.last_error = self.last_error or "unexpired_foreign_owner"
            self._unlock()
            return False
        self.resumed_same_context = bool(same_context_restart)
        try:
            self._write_record(context_valid=True)
        except Exception as exc:
            self.last_error = "lease_record_write_failed:%s" % type(exc).__name__
            self._unlock()
            return False
        self.last_error = ""
        return True

    def renew(self, mode, peer_ip, context_valid=True):
        normalized_peer = validate_peer_ip(peer_ip)
        if self._lock_file is None or not context_valid:
            self.last_error = "lease_not_held_or_context_invalid"
            return False
        if str(mode) != self._mode or normalized_peer != self._peer_ip:
            self.last_error = "lease_context_changed"
            return False
        record = self._read_record()
        now = float(self.clock())
        if not record or str(record.get("owner_id") or "") != self.owner_id:
            self.last_error = "lease_owner_mismatch"
            return False
        if not record.get("context_valid") or float(record.get("expires_at", 0.0) or 0.0) <= now:
            self.last_error = "lease_expired_or_invalid"
            return False
        try:
            self._write_record(context_valid=True)
        except Exception as exc:
            self.last_error = "lease_renew_failed:%s" % type(exc).__name__
            return False
        self.last_error = ""
        return True

    def acquire_or_renew(self, mode, peer_ip, context_valid=True):
        if self._lock_file is None:
            return self.acquire(mode, peer_ip, context_valid=context_valid)
        return self.renew(mode, peer_ip, context_valid=context_valid)

    def valid(self, mode=None, peer_ip=None):
        if self._lock_file is None:
            return False
        record = self._read_record()
        if not record or str(record.get("owner_id") or "") != self.owner_id:
            return False
        if not record.get("context_valid") or record.get("released"):
            return False
        if float(record.get("expires_at", 0.0) or 0.0) <= float(self.clock()):
            return False
        if mode is not None and str(mode) != str(record.get("mode") or ""):
            return False
        if peer_ip is not None and validate_peer_ip(peer_ip) != str(record.get("peer_ip") or ""):
            return False
        return True

    def invalidate(self, reason="context_lost"):
        if self._lock_file is None:
            return
        try:
            self._write_record(context_valid=False, reason=reason)
        except Exception:
            pass

    def release(self, reason="released"):
        if self._lock_file is None:
            return True
        try:
            self._write_record(context_valid=False, released=True, reason=reason)
        except Exception as exc:
            self.last_error = "lease_release_record_failed:%s" % type(exc).__name__
            return False
        self._unlock()
        return True

    def snapshot(self):
        record = self._read_record()
        return record if isinstance(record, dict) else {}

    @property
    def held(self):
        return self._lock_file is not None

    def _unlock(self):
        lock_file = self._lock_file
        self._lock_file = None
        if lock_file is None:
            return
        try:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)
        except Exception:
            pass
        try:
            lock_file.close()
        except Exception:
            pass

def set_web_permissions(filepath, data=None):
    """Setzt Webrechte über den geöffneten Inode, niemals über einen Symlink."""

    descriptor = -1
    try:
        base = os.path.basename(str(filepath))
        mode = config_secret_file_mode(data) if "e3dc_v4.json" in base else 0o664
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(filepath, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("Webdatei ist keine einzelne reguläre Datei")
        os.fchown(
            descriptor,
            pwd.getpwnam(INSTALL_USER).pw_uid,
            grp.getgrnam("www-data").gr_gid,
        )
        os.fchmod(descriptor, mode)
    except Exception as e:
        logger.error(f"Konnte Rechte für {filepath} nicht setzen: {e}")
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _read_web_json(path, max_bytes=4 * 1024 * 1024):
    """Liest eine Web-/Ramdisk-JSON ohne Symlink- oder Wechselrennen."""

    absolute = os.path.abspath(path)
    parent, name = os.path.split(absolute)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, directory_flags)
    descriptor = -1
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
        flags |= getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(name, flags, dir_fd=directory_fd)
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size < 1
            or before.st_size > max_bytes
            or stat.S_IMODE(before.st_mode) & 0o002
        ):
            raise OSError("Web-JSON besitzt unsichere Metadaten")
        raw = bytearray()
        while len(raw) <= max_bytes:
            chunk = os.read(descriptor, min(64 * 1024, max_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=directory_fd, follow_symlinks=False)

        def token(item):
            return (
                item.st_dev,
                item.st_ino,
                item.st_size,
                item.st_mtime_ns,
                item.st_ctime_ns,
                item.st_nlink,
            )

        if len(raw) > max_bytes or token(before) != token(after) or token(after) != token(named_after):
            raise OSError("Web-JSON wechselte beim Lesen")
        value = json.loads(bytes(raw).decode("utf-8-sig"))
        if not isinstance(value, dict):
            raise ValueError("Web-JSON ist kein Objekt")
        return value
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(directory_fd)


def _atomic_write_web_json(path, payload, *, mode=0o664):
    """Ersetzt eine Web-/Ramdisk-JSON atomar über einen gebundenen Parent-FD."""

    absolute = os.path.abspath(path)
    parent, name = os.path.split(absolute)
    directory_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    directory_flags |= getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    directory_fd = os.open(parent, directory_flags)
    temporary = ".%s.%d.%s.tmp" % (name, os.getpid(), uuid.uuid4().hex)
    descriptor = -1
    try:
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
            raise OSError("Temporäre Web-JSON ist keine einzelne reguläre Datei")
        os.fchown(
            descriptor,
            pwd.getpwnam(INSTALL_USER).pw_uid,
            grp.getgrnam("www-data").gr_gid,
        )
        os.fchmod(descriptor, int(mode))
        raw = (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
        offset = 0
        while offset < len(raw):
            offset += os.write(descriptor, raw[offset:])
        os.fsync(descriptor)
        os.close(descriptor)
        descriptor = -1
        os.replace(temporary, name, src_dir_fd=directory_fd, dst_dir_fd=directory_fd)
        try:
            os.fsync(directory_fd)
        except OSError:
            pass
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _mode5_user_start_nodes_safe(
    request_path=WALLBOX_MODE5_USER_START_REQUEST_FILE,
    *,
    legacy_parent=False,
):
    """Bindet Parent, Request und Lock read-only vor und nach HA-Sync."""

    try:
        account = pwd.getpwnam("www-data")
        group = grp.getgrnam("www-data")
        parent = os.lstat(os.path.dirname(request_path))
        parent_mode = stat.S_IMODE(parent.st_mode)
        expected_parent_mode = int(config_secret_dir_mode())
        required_parent_mode = (
            expected_parent_mode & 0o777
            if legacy_parent
            else expected_parent_mode
        )
        if (
            stat.S_ISLNK(parent.st_mode)
            or not stat.S_ISDIR(parent.st_mode)
            or bool(parent_mode & 0o002)
            or parent.st_gid != int(group.gr_gid)
            or parent_mode != required_parent_mode
        ):
            return False
        manager_uid = int(pwd.getpwnam(INSTALL_USER).pw_uid)
        allowed_parent_uids = {int(account.pw_uid), manager_uid}
        if parent.st_uid not in allowed_parent_uids:
            return False
        target_contracts = (
            (request_path, {int(account.pw_uid)}, True),
            (
                request_path + ".lock",
                {0, int(account.pw_uid), manager_uid},
                False,
            ),
        )
        for target, allowed_uids, payload_file in target_contracts:
            if not os.path.lexists(target):
                continue
            metadata = os.lstat(target)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or stat.S_IMODE(metadata.st_mode) != 0o660
                or metadata.st_uid not in allowed_uids
                or metadata.st_gid != int(group.gr_gid)
                or metadata.st_size > 65536
                or (payload_file and metadata.st_size < 1)
            ):
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _mode5_user_start_surfaces_safe(
    request_path=WALLBOX_MODE5_USER_START_REQUEST_FILE,
):
    return _mode5_user_start_nodes_safe(
        request_path,
        legacy_parent=False,
    )


def _repair_mode5_user_start_legacy_parent(
    request_path=WALLBOX_MODE5_USER_START_REQUEST_FILE,
):
    """Ergänzt ausschließlich dem konfigurierten Datenmodus das Setgid-Bit."""

    if _mode5_user_start_surfaces_safe(request_path):
        return True
    if not _mode5_user_start_nodes_safe(request_path, legacy_parent=True):
        return False
    directory = os.path.dirname(request_path)
    descriptor = None
    try:
        before = os.lstat(directory)
        group = grp.getgrnam("www-data")
        account = pwd.getpwnam("www-data")
        manager_uid = int(pwd.getpwnam(INSTALL_USER).pw_uid)
        allowed_parent_uids = {int(account.pw_uid), manager_uid}
        expected_parent_mode = int(config_secret_dir_mode())
        legacy_parent_mode = expected_parent_mode & 0o777
        flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
        flags |= getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        descriptor = os.open(directory, flags)
        current = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(current.st_mode)
            or (current.st_dev, current.st_ino) != (before.st_dev, before.st_ino)
            or current.st_uid not in allowed_parent_uids
            or current.st_gid != int(group.gr_gid)
            or stat.S_IMODE(current.st_mode) != legacy_parent_mode
        ):
            return False
        os.fchmod(descriptor, expected_parent_mode)
        changed = os.fstat(descriptor)
        named = os.lstat(directory)
        if (
            stat.S_IMODE(changed.st_mode) != expected_parent_mode
            or (named.st_dev, named.st_ino) != (changed.st_dev, changed.st_ino)
            or named.st_uid not in allowed_parent_uids
            or stat.S_IMODE(named.st_mode) != expected_parent_mode
        ):
            return False
    except (KeyError, OSError, TypeError, ValueError):
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return _mode5_user_start_surfaces_safe(request_path)

def is_secret_config_key(key):
    """Erkennt Config-Schlüssel, deren Werte nicht zwischen HA-Knoten wandern."""
    normalized = str(key or "").strip().lower()
    if not normalized:
        return False
    if normalized in SECRET_CONFIG_EXACT_KEYS:
        return True
    if normalized.endswith("_pass") or normalized == "pass":
        return True
    return any(part in normalized for part in SECRET_CONFIG_KEY_PARTS)

def ha_sync_config_payload(config_data):
    """Liefert die Master-Config ohne lokale Zugangsdaten für HA-Sync-Artefakte."""
    if not isinstance(config_data, dict):
        return {}
    return {
        key: value
        for key, value in config_data.items()
        if not is_secret_config_key(key)
    }

def load_config():
    """Liest die HA-Parameter aus der e3dc_v4.json (Single Source of Truth)."""
    config = {}
    if os.path.exists(CONFIG_PATH):
        try:
            data = _read_web_json(CONFIG_PATH)
            for k, v in data.items():
                config[str(k).strip().lower()] = str(v).strip()
        except Exception as e:
            logger.error(f"Fehler beim Laden der Config ({CONFIG_PATH}): {e}")
    return config

def get_local_ips():
    """Liefert lokale IPs, damit HA niemals auf sich selbst synchronisiert."""
    ips = {"127.0.0.1", "::1", "localhost"}
    try:
        hostname = socket.gethostname()
        ips.add(hostname)
        for info in socket.getaddrinfo(hostname, None):
            ips.add(info[4][0])
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["/usr/bin/hostname", "-I"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        for ip in (result.stdout or "").split():
            ips.add(ip.strip())
    except Exception:
        pass
    return {ip for ip in ips if ip}

def peer_points_to_self(peer_ip):
    """True, wenn die HA-Peer-IP auf diesen Pi zeigt."""
    normalized = validate_peer_ip(peer_ip)
    return bool(normalized) and normalized in get_local_ips()

def send_telegram(msg):
    """Sendet Telegram-Nachrichten via boot_notify.sh (Watchdog)."""
    if _trusted_root_executable(NOTIFY_SCRIPT):
        try:
            subprocess.run([NOTIFY_SCRIPT, msg], timeout=10)
        except Exception:
            pass
    elif os.path.lexists(NOTIFY_SCRIPT):
        logger.warning("Unsicherer Root-Benachrichtigungshelfer wurde nicht ausgeführt.")
    logger.info(f"Benachrichtigung: {msg}")


def _trusted_root_executable(path):
    """Akzeptiert nur Root-Code unter vollständig geschützten Elternpfaden."""

    candidate = Path(path)
    try:
        metadata = candidate.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
            or not stat.S_IMODE(metadata.st_mode) & 0o111
        ):
            return False
        for parent in (candidate.parent, *candidate.parents):
            parent_metadata = parent.lstat()
            if (
                not stat.S_ISDIR(parent_metadata.st_mode)
                or stat.S_ISLNK(parent_metadata.st_mode)
                or parent_metadata.st_uid != 0
                or stat.S_IMODE(parent_metadata.st_mode) & 0o022
            ):
                return False
            if parent == Path("/"):
                break
        return True
    except OSError:
        return False

def is_host_online(ip):
    """Prüft per Ping, ob der Partner erreichbar ist."""
    ip = validate_peer_ip(ip)
    if not ip:
        return False
    res = subprocess.run(["/usr/bin/ping", "-c", "1", "-W", "2", ip], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return res.returncode == 0


def query_peer_writer_state(peer_ip, runner=subprocess.run):
    """Read-only-Peerprüfung: aktiv, stillgelegt oder unbekannt, was immer sperrt."""
    peer_ip = validate_peer_ip(peer_ip)
    if not peer_ip or peer_points_to_self(peer_ip):
        return "unknown"
    services = get_managed_services("stop")
    command = [
        "/usr/bin/sudo", "-u", INSTALL_USER, "/usr/bin/ssh",
        "-o", "StrictHostKeyChecking=accept-new",
        "-o", "BatchMode=yes",
        "-o", "ConnectTimeout=5",
        f"{INSTALL_USER}@{peer_ip}",
        "systemctl", "is-active",
    ] + services
    try:
        result = runner(
            command,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
        )
    except Exception:
        return "unknown"
    if result.returncode not in {0, 3, 4}:
        return "unknown"
    states = [line.strip().lower() for line in (result.stdout or "").splitlines() if line.strip()]
    if len(states) != len(services):
        return "unknown"
    if any(state in {"active", "activating", "reloading", "deactivating"} for state in states):
        return "active"
    if all(state in {"inactive", "failed", "unknown"} for state in states):
        return "quiesced"
    return "unknown"


def local_writers_quiesced():
    return all(not service_is_active(service) for service in get_managed_services("stop") if service_exists(service))


def side_effect_context_is_clear(peer_ip, owner_lease=None, peer_state_getter=query_peer_writer_state):
    """Erlaubt Sync nur mit belegtem lokalem Owner oder vollständig gestopptem Knoten."""
    peer_state = peer_state_getter(peer_ip)
    if peer_state != "quiesced":
        return False, "peer_writer_%s" % peer_state
    if owner_lease is not None and owner_lease.valid(peer_ip=peer_ip):
        return True, ""
    if local_writers_quiesced():
        return True, ""
    return False, "local_writer_owner_unknown"


def owner_admission_allowed(role, already_owner, peer_writer_state):
    """Ein neuer Owner benötigt immer einen bestätigt stillgelegten Peer; ein bestehender darf dessen Ausfall überbrücken."""
    if role not in ("master", "slave"):
        return False
    if already_owner:
        return peer_writer_state != "active"
    return peer_writer_state == "quiesced"

def write_status(mode, state, peer_online, last_sync=0, owner_lease=None, safety_reason=""):
    """Schreibt den aktuellen HA-Status für die Web-Oberfläche in die Ramdisk."""
    try:
        status_data = {
            "mode": mode,
            "state": state,
            "peer_online": peer_online,
            "last_sync": last_sync,
            "ts": int(time.time()),
            "owner_lease_valid": bool(owner_lease and owner_lease.valid()),
            "owner_id": owner_lease.owner_id if owner_lease and owner_lease.valid() else "",
            "owner_lease_expires_at": (owner_lease.snapshot().get("expires_at", 0) if owner_lease else 0),
            "safety_reason": str(safety_reason or ""),
        }
        _atomic_write_web_json(
            "/var/www/html/ramdisk/ha_status.json",
            status_data,
            mode=0o664,
        )
    except Exception as e:
        logger.error(f"Fehler beim Schreiben des HA-Status: {e}")

def merge_config(src, dest):
    """
    Übernimmt die Konfiguration vom Master (src) auf den Slave (dest),
    behält aber Cluster-Parameter und Zugangsdaten des Slaves lokal.
    """
    slave_data = {}

    # 1. Lokale HA-Parameter und Secrets des Slaves retten
    if os.path.exists(dest):
        try:
            slave_data = _read_web_json(dest)
        except Exception as e:
            logger.error(f"Fehler beim Lesen der Slave-Config: {e}")

    # 2. Master-Config einlesen
    try:
        master_data = _read_web_json(src)
    except Exception as e:
        logger.error(f"Fehler beim Lesen der Master-Config: {e}")
        return

    # 3. Master-Daten ohne Secrets übernehmen und lokale Werte wieder injizieren
    merged_data = ha_sync_config_payload(master_data)
    protected_count = 0
    for k, v in slave_data.items():
        if k in HA_LOCAL_CONFIG_KEYS or is_secret_config_key(k):
            merged_data[k] = v
            protected_count += 1

    # 4. Sicher schreiben
    try:
        _atomic_write_web_json(
            dest,
            merged_data,
            mode=config_secret_file_mode(merged_data),
        )
        logger.info(f"Master-Config gemergt, {protected_count} lokale Slave-Werte geschützt.")
    except Exception as e:
        logger.error(f"Fehler beim Speichern der gemergten Config: {e}")

def rsync_data(target_ip, push=True, owner_lease=None, peer_state_getter=query_peer_writer_state):
    """
    Synchronisiert die Daten zwischen den PIs via rsync.
    push=True:  Dieser Pi sendet an den anderen Pi.
    push=False: Dieser Pi holt sich Daten vom anderen Pi.
    """
    target_ip = validate_peer_ip(target_ip)
    if not target_ip:
        logger.error("Rsync abgebrochen: ungültige HA-Peer-IP.")
        return False

    context_clear, context_reason = side_effect_context_is_clear(
        target_ip,
        owner_lease=owner_lease,
        peer_state_getter=peer_state_getter,
    )
    if not context_clear:
        logger.error("Rsync blocked: %s", context_reason)
        return False
    if not push and not local_writers_quiesced():
        logger.error("Rsync pull blocked: local writers are not confirmed stopped.")
        return False
    if not _repair_mode5_user_start_legacy_parent():
        logger.error(
            "Rsync blocked: persistente Modus-5-Anforderungsfläche ist unsicher."
        )
        return False

    user = INSTALL_USER
    ssh_transport = "ssh -o StrictHostKeyChecking=accept-new -o BatchMode=yes"
    base_args = [
        "/usr/bin/sudo", "-u", user, "/usr/bin/rsync", "-au",
        "--exclude", "*.tmp",
        "--exclude", "*.flag",
        "--exclude", "*_status.json",
        "--exclude", "*_cache.json",
        "--exclude", "*_history.json",
        "--exclude", "live_history.txt",
        "--exclude", "diagnose_ack.json",
        "--exclude", "watchdog.heartbeat",
        "-e", ssh_transport,
    ]
    ramdisk_args = base_args + [
        "--exclude", "rule_calm_analysis.json",
        "--exclude", "watchdog.update_pause",
        "--exclude", "watchdog.update_grace",
        "--exclude", "matter_pairing.json",
        "--exclude", "matter_pairing.json.*.tmp",
        "--exclude", ".e3dc_config_cache.*",
        "--exclude", ".wb_sessions_aggregate_*",
        "--exclude", ".get_live_json_*",
    ]
    data_args = base_args + [
        "--exclude", "e3dc_v4.json",
        "--exclude", "e3dc_v4.json.tmp",
        "--exclude", "e3dc_v4.json.bak*",
        "--exclude", ".e3dc_v4_*",
        "--exclude", "e3dc.config.txt",
        "--exclude", "config_backups",
        "--exclude", "config_backups/",
        "--exclude", "matter-storage",
        "--exclude", "matter-storage/",
        "--exclude", ".wallbox_plan_jobs",
        "--exclude", ".wallbox_plan_jobs/",
        "--exclude", "wallbox_mode5_user_start_request.json",
        "--exclude", "wallbox_mode5_user_start_request.json.lock",
    ]
    optional_args = base_args + ["--ignore-missing-args"]

    # Quelle und Ziel für Ramdisk (Historie)
    rd_src = "/var/www/html/ramdisk/" if push else _rsync_remote(user, target_ip, "/var/www/html/ramdisk/")
    rd_dst = _rsync_remote(user, target_ip, "/var/www/html/ramdisk/") if push else "/var/www/html/ramdisk/"

    # NEU: Quelle und Ziel für dauerhafte Daten (Datenbank, Wallbox-Logs, Archive)
    data_src = "/var/www/html/data/" if push else _rsync_remote(user, target_ip, "/var/www/html/data/")
    data_dst = _rsync_remote(user, target_ip, "/var/www/html/data/") if push else "/var/www/html/data/"

    # Quelle und Ziel für E3DC Daten (.dat Dateien)
    dat_src = sorted(glob.glob(os.path.join(INSTALL_PATH, "*.dat"))) if push else [_rsync_remote(user, target_ip, f"{INSTALL_PATH}/*.dat")]
    dat_dst = _rsync_remote(user, target_ip, f"{INSTALL_PATH}/") if push else f"{INSTALL_PATH}/"

    # NEU: Quelle und Ziel für E3DC-Strompreise (Ausschluss der wallbox.txt oder anderer lokaler Logs)
    local_txt_src = os.path.join(INSTALL_PATH, "e3dc.strompreise.txt")
    txt_src = [local_txt_src] if push and os.path.exists(local_txt_src) else []
    if not push:
        txt_src = [_rsync_remote(user, target_ip, f"{INSTALL_PATH}/e3dc.strompreise.txt")]
    txt_dst = _rsync_remote(user, target_ip, f"{INSTALL_PATH}/") if push else f"{INSTALL_PATH}/"

    try:
        subprocess.run(ramdisk_args + [rd_src, rd_dst], timeout=45, check=True)
        subprocess.run(data_args + [data_src, data_dst], timeout=45, check=True)
        if dat_src:
            subprocess.run(optional_args + dat_src + [dat_dst], timeout=45, check=True)
        if txt_src:
            subprocess.run(optional_args + txt_src + [txt_dst], timeout=45, check=True)

        # Rechte der geholten Daten in der Ramdisk und Data anpassen (bei Pull)
        if not push:
            protected_ramdisk = [
                "(",
                "-path", "/var/www/html/ramdisk/rule_calm_analysis.json", "-o",
                "-path", "/var/www/html/ramdisk/watchdog.update_pause", "-o",
                "-path", "/var/www/html/ramdisk/watchdog.update_grace",
                "-o", "-path", "/var/www/html/ramdisk/matter_pairing.json",
                "-o", "-path", "/var/www/html/ramdisk/matter_pairing.json.*.tmp",
                "-o", "-path", "/var/www/html/ramdisk/e3dc_config_cache.json",
                "-o", "-path", "/var/www/html/ramdisk/.e3dc_config_cache.*",
                "-o", "-path", "/var/www/html/ramdisk/.wb_sessions_aggregate_*",
                "-o", "-path", "/var/www/html/ramdisk/.get_live_json_*",
                ")", "-prune", "-o",
            ]
            protected_data = [
                "(",
                "-path", "/var/www/html/data/e3dc_v4.json", "-o",
                "-path", "/var/www/html/data/e3dc_v4.json.tmp", "-o",
                "-path", "/var/www/html/data/e3dc_v4.json.bak*", "-o",
                "-path", "/var/www/html/data/.e3dc_v4_*", "-o",
                "-path", "/var/www/html/data/e3dc.config.txt", "-o",
                "-path", "/var/www/html/data/config_backups", "-o",
                "-path", "/var/www/html/data/matter-storage", "-o",
                "-path", "/var/www/html/data/.wallbox_plan_jobs", "-o",
                "-path", "/var/www/html/data/wallbox_mode5_user_start_request.json", "-o",
                "-path", "/var/www/html/data/wallbox_mode5_user_start_request.json.lock",
                ")", "-prune", "-o",
            ]
            subprocess.run([
                "/usr/bin/sudo", "/usr/bin/find", "-P", "/var/www/html/ramdisk", "-xdev",
                *protected_ramdisk, "-type", "l", "-prune", "-o",
                "-exec", "/usr/bin/chown", f"{INSTALL_USER}:www-data", "{}", "+",
            ], check=True)
            subprocess.run([
                "/usr/bin/sudo", "/usr/bin/find", "-P", "/var/www/html/data", "-xdev",
                "-mindepth", "1", *protected_data,
                "-type", "l", "-prune", "-o",
                "-exec", "/usr/bin/chown", f"{INSTALL_USER}:www-data", "{}", "+",
            ], check=True)
            subprocess.run([
                "/usr/bin/sudo", "/usr/bin/find", "-P", "/var/www/html/ramdisk", "-xdev",
                *protected_ramdisk, "-type", "f", "-exec", "/usr/bin/chmod", "664", "{}", "+",
            ], check=True)
            subprocess.run(
                ["/usr/bin/sudo", "/usr/bin/chown", f"{INSTALL_USER}:www-data", "/var/www/html/data"],
                check=True,
            )
            subprocess.run(
                [
                    "/usr/bin/sudo",
                    "/usr/bin/chmod",
                    config_secret_dir_mode_text(),
                    "/var/www/html/data",
                ],
                check=True,
            )
            subprocess.run([
                "/usr/bin/sudo", "/usr/bin/find", "-P", "/var/www/html/data", "-xdev",
                "-mindepth", "1", *protected_data,
                "-type", "d", "-exec", "/usr/bin/chmod", "775", "{}", "+",
            ])
            subprocess.run([
                "/usr/bin/sudo", "/usr/bin/find", "-P", "/var/www/html/data", "-xdev",
                "-mindepth", "1", *protected_data,
                "-type", "f", "-exec", "/usr/bin/chmod", "664", "{}", "+",
            ])
            subprocess.run(["/usr/bin/sudo", "/usr/bin/chmod", config_secret_file_mode_text(), "/var/www/html/data/e3dc_v4.json"], stderr=subprocess.DEVNULL)
            subprocess.run(["/usr/bin/sudo", "/usr/bin/chmod", config_secret_dir_mode_text(), "/var/www/html/data/config_backups"], stderr=subprocess.DEVNULL)
            migration_backup_dir = "/var/www/html/data/config_backups/aux_inverter_migration"
            protected_backup = ["-path", migration_backup_dir, "-prune", "-o"]
            subprocess.run(["/usr/bin/sudo", "/usr/bin/find", "-P", "/var/www/html/data/config_backups", *protected_backup, "-type", "d", "-exec", "/usr/bin/chmod", config_secret_dir_mode_text(), "{}", "+"], stderr=subprocess.DEVNULL)
            subprocess.run(["/usr/bin/sudo", "/usr/bin/find", "-P", "/var/www/html/data/config_backups", *protected_backup, "-type", "f", "-exec", "/usr/bin/chmod", config_secret_file_mode_text(), "{}", "+"], stderr=subprocess.DEVNULL)
            if not _harden_aux_inverter_migration_backups(migration_backup_dir):
                logger.error("Zusatz-WR-Migrationsbackups konnten nicht sicher gehärtet werden.")
                return False
            if not _mode5_user_start_surfaces_safe():
                logger.error(
                    "Rsync blocked: Modus-5-Anforderungsfläche wechselte beim HA-Pull."
                )
                return False

        return True
    except Exception as e:
        logger.error(f"Rsync Fehler: {e}")
        return False


def _aux_inverter_migration_backup_structure_safe(path):
    if not os.path.lexists(path):
        return True
    try:
        root_stat = os.lstat(path)
        if stat.S_ISLNK(root_stat.st_mode) or not stat.S_ISDIR(root_stat.st_mode):
            return False
        for directory, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
            for name in dirnames:
                metadata = os.lstat(os.path.join(directory, name))
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
                    return False
            for name in filenames:
                metadata = os.lstat(os.path.join(directory, name))
                if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                    return False
        return True
    except OSError:
        return False


def _verify_aux_inverter_migration_backup_modes(path):
    if not _aux_inverter_migration_backup_structure_safe(path):
        return False
    if not os.path.lexists(path):
        return True
    if stat.S_IMODE(os.lstat(path).st_mode) != 0o700:
        return False
    for directory, dirnames, filenames in os.walk(path, topdown=True, followlinks=False):
        if stat.S_IMODE(os.lstat(directory).st_mode) != 0o700:
            return False
        for name in dirnames:
            if stat.S_IMODE(os.lstat(os.path.join(directory, name)).st_mode) != 0o700:
                return False
        for name in filenames:
            if stat.S_IMODE(os.lstat(os.path.join(directory, name)).st_mode) != 0o600:
                return False
    return True


def _harden_aux_inverter_migration_backups(path):
    if not os.path.lexists(path):
        return True
    if not _aux_inverter_migration_backup_structure_safe(path):
        return False
    try:
        subprocess.run(["/usr/bin/sudo", "/usr/bin/chmod", "00700", path], check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["/usr/bin/sudo", "/usr/bin/find", "-P", path, "-type", "d", "-exec", "/usr/bin/chmod", "00700", "{}", "+"], check=True, stderr=subprocess.DEVNULL)
        subprocess.run(["/usr/bin/sudo", "/usr/bin/find", "-P", path, "-type", "f", "-exec", "/usr/bin/chmod", "00600", "{}", "+"], check=True, stderr=subprocess.DEVNULL)
    except (OSError, subprocess.SubprocessError):
        return False
    return _verify_aux_inverter_migration_backup_modes(path)

def service_exists(service):
    """Prueft, ob eine systemd-Unit lokal existiert."""
    if not service.endswith(".service"):
        service += ".service"
    return (
        os.path.exists(f"/etc/systemd/system/{service}")
        or os.path.exists(f"/lib/systemd/system/{service}")
        or os.path.exists(f"/usr/lib/systemd/system/{service}")
    )

def service_is_active(service):
    """True, wenn systemd die Unit als aktiv meldet."""
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-active", "--quiet", service],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return result.returncode == 0

def service_is_enabled(service):
    """True, wenn die Unit beim HA-Aktivstart mitgestartet werden darf."""
    result = subprocess.run(
        ["/usr/bin/systemctl", "is-enabled", service],
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
    )
    state = (result.stdout or "").strip().lower()
    return state in {"enabled", "static", "generated", "linked", "indirect", "alias"}

def get_managed_services(action="start"):
    """
    Liefert die HA-verwalteten Dienste.

    Bei Start wird der alte C++-Kern nur dann gestartet, wenn kein V4
    `e3dc-live` vorhanden ist. Beim Stop werden beide gestoppt, damit ein
    Standby-Slave sicher stumm bleibt.
    """
    services = list(MANAGED_SERVICES)
    if action == "start" and service_exists(NATIVE_LIVE_SERVICE):
        services = [s for s in services if s != LEGACY_E3DC_SERVICE]
    return services

def _legacy_manage_services_unverified(action="start"):
    """Startet oder stoppt alle Dienste, die im HA-Verbund exklusiv sein muessen."""
    if action not in ("start", "stop"):
        logger.error(f"Ungueltige Service-Aktion: {action}")
        return

    for srv in get_managed_services(action):
        if not service_exists(srv):
            continue

        try:
            active = service_is_active(srv)
            if action == "stop" and not active:
                continue
            if action == "start" and active:
                continue
            if action == "start" and not service_is_enabled(srv):
                logger.info(f"HA start uebersprungen (deaktiviert): {srv}")
                continue

            result = subprocess.run(
                ["/usr/bin/sudo", "/usr/bin/systemctl", action, srv],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
            if result.returncode == 0:
                logger.info(f"HA {action}: {srv}")
            else:
                logger.warning(f"HA {action} fehlgeschlagen: {srv} (rc={result.returncode})")
        except Exception as e:
            logger.error(f"HA {action} Fehler bei {srv}: {e}")


def _systemctl_service_action(action, service):
    try:
        result = subprocess.run(
            ["/usr/bin/sudo", "/usr/bin/systemctl", action, service],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=20,
        )
    except Exception as exc:
        logger.error("HA %s error for %s: %s", action, service, exc)
        return False
    if result.returncode != 0:
        logger.warning("HA %s failed: %s (rc=%s)", action, service, result.returncode)
        return False
    expected_active = action == "start"
    actual_active = service_is_active(service)
    if actual_active != expected_active:
        logger.warning("HA %s unconfirmed: %s (active=%s)", action, service, actual_active)
        return False
    logger.info("HA %s: %s", action, service)
    return True


def _rollback_started_services(services):
    rollback_ok = True
    for service in reversed(list(services)):
        if service_exists(service) and service_is_active(service):
            rollback_ok = _systemctl_service_action("stop", service) and rollback_ok
    return rollback_ok and local_writers_quiesced()


def manage_services(action="start", owner_lease=None, mode=None, peer_ip=None, release_lease=True):
    """Startet oder stoppt die exklusiven HA-Schreiber als eine geprüfte Transaktion."""
    if action not in ("start", "stop"):
        logger.error("Invalid HA service action: %s", action)
        return False
    if action == "start" and (
        owner_lease is None
        or not owner_lease.valid(mode=mode, peer_ip=peer_ip)
    ):
        logger.error("HA start blocked: no fresh owner lease.")
        return False

    services = get_managed_services(action)
    for srv in services:
        if not service_exists(srv):
            continue
        if action == "start" and not owner_lease.valid(mode=mode, peer_ip=peer_ip):
            logger.error("HA start aborted: owner lease lost before %s.", srv)
            rollback_ok = _rollback_started_services(services)
            if rollback_ok:
                owner_lease.release("start_context_lost")
            else:
                owner_lease.invalidate("start_rollback_failed")
            return False

        active = service_is_active(srv)
        if action == "stop" and not active:
            continue
        if action == "start" and active:
            continue
        if action == "start" and not service_is_enabled(srv):
            logger.info("HA start skipped (disabled): %s", srv)
            continue
        if not _systemctl_service_action(action, srv):
            if action == "start":
                rollback_ok = _rollback_started_services(services)
                if rollback_ok:
                    owner_lease.release("partial_start_rolled_back")
                else:
                    owner_lease.invalidate("partial_start_rollback_failed")
            return False

    if action == "stop":
        stopped = local_writers_quiesced()
        if not stopped:
            if owner_lease is not None:
                owner_lease.invalidate("writer_stop_unconfirmed")
            return False
        if owner_lease is not None and release_lease:
            return owner_lease.release("writers_stopped")
    return True


def _legacy_main_loop_unsafe():
    logger.info("E3DC-Control High Availability Manager gestartet.")

    fail_counter = 0
    last_sync = 0
    was_active_as_slave = False
    master_auto_recovered = False
    last_config_mtime = 0
    last_master_cfg_mtime = 0

    while True:
        config = load_config()
        mode = config.get("ha_mode", "off").lower()
        raw_peer_ip = config.get("ha_peer_ip", "")
        peer_ip = validate_peer_ip(raw_peer_ip)

        try: fail_timeout = int(config.get("ha_fail_timeout", "15"))
        except ValueError: fail_timeout = 15

        try: sync_interval = int(config.get("ha_sync_interval", "60")) * 60
        except ValueError: sync_interval = 3600

        auto_recover = config.get("ha_auto_recover", "1") in ["1", "true"]
        auto_failover = config.get("ha_auto_failover", "1") in ["1", "true"]

        if mode == "off" or not raw_peer_ip:
            write_status("off", "inactive", False)
            time.sleep(60)
            continue

        if not peer_ip:
            logger.critical(f"HA-Konfiguration ungueltig: ha_peer_ip={raw_peer_ip!r} ist keine IP-Adresse.")
            if mode == "slave":
                manage_services("stop")
            write_status(mode, "config_error_invalid_peer", False, last_sync)
            time.sleep(60)
            continue

        if peer_points_to_self(peer_ip):
            logger.critical(
                f"HA-Konfiguration ungueltig: peer_ip={peer_ip} zeigt auf diesen Pi. "
                "HA-Aktionen werden blockiert."
            )
            if mode == "slave":
                manage_services("stop")
            write_status(mode, "config_error_self_peer", False, last_sync)
            time.sleep(60)
            continue

        # ==========================================
        # MASTER LOGIK (Der Haupt-Raspberry Pi)
        # ==========================================
        if mode == "master":
            peer_online = is_host_online(peer_ip)
            # 1. Auto-Recover beim Boot (Einmalig)
            if auto_recover and not master_auto_recovered:
                logger.info("Auto-Recover aktiv: Prüfe ob Slave erreichbar ist für Daten-Rücksicherung...")
                if peer_online:
                    manage_services("stop") # Sicherstellen, dass hier nichts schreibt
                    logger.info("Ziehe letzte Historie & Ramdisk-Daten vom Slave (Pull)...")
                    rsync_data(peer_ip, push=False)
                    logger.info("Auto-Recover abgeschlossen. Fahre System hoch.")
                master_auto_recovered = True

            # 2. Stelle sicher, dass die Dienste laufen
            manage_services("start")

            # 3. Config-Sync-Artefakt vor dem Datensync frisch und ohne Secrets schreiben
            if os.path.exists(CONFIG_PATH):
                current_config_mtime = os.path.getmtime(CONFIG_PATH)
                if current_config_mtime != last_config_mtime:
                    try:
                        master_data = _read_web_json(CONFIG_PATH)
                        sync_payload = ha_sync_config_payload(master_data)
                        _atomic_write_web_json(
                            "/var/www/html/ramdisk/master_e3dc_v4.json",
                            sync_payload,
                            mode=0o664,
                        )
                        last_config_mtime = current_config_mtime
                        logger.info("Konfiguration ohne lokale Secrets für automatischen Sync zum Slave bereitgestellt.")
                    except Exception as e:
                        logger.error(f"Fehler beim Bereitstellen der Config für Sync: {e}")

            # 4. Synchronisiere Daten regelmäßig zum Backup-Pi
            if time.time() - last_sync > sync_interval:
                if peer_online:
                    rsync_data(peer_ip, push=True)
                    last_sync = time.time()
                    logger.debug(f"Sync zu Slave ({peer_ip}) erfolgreich.")
                else:
                    logger.warning(f"Slave ({peer_ip}) ist nicht erreichbar. Backup-Sync übersprungen.")

            write_status("master", "active", peer_online, last_sync)

        # ==========================================
        # SLAVE LOGIK (Der Backup-Raspberry Pi)
        # ==========================================
        elif mode == "slave":
            master_online = is_host_online(peer_ip)

            if master_online:
                # Master ist DA -> Alles entspannt
                if fail_counter > 0:
                    logger.info(f"Master ({peer_ip}) ist wieder erreichbar.")
                fail_counter = 0

                # Waren wir aktiv? -> FAILBACK einleiten!
                if was_active_as_slave:
                    logger.info("Starte Failback: Synchronisiere gesammelte Daten zurück zum Master...")
                    manage_services("stop") # Unsere Dienste stoppen!
                    rsync_data(peer_ip, push=True) # Daten zum Master pushen
                    logger.info("Starte Dienste auf dem Master neu...")
                    subprocess.run(
                        [
                            "/usr/bin/sudo", "-u", INSTALL_USER,
                            "/usr/bin/ssh",
                            "-o", "StrictHostKeyChecking=accept-new",
                            "-o", "BatchMode=yes",
                            f"{INSTALL_USER}@{peer_ip}",
                            "sudo", "systemctl", "restart", "e3dc-live",
                        ],
                        timeout=30,
                    )
                    send_telegram(f"✅ E3DC FAILBACK: Master ({peer_ip}) ist wieder da! Daten wurden synchronisiert. Backup-Pi geht zurück in Standby.")
                    was_active_as_slave = False

                manage_services("stop") # Im Normalbetrieb bleiben wir gestoppt

                # Verhindere Endlos-Spinner im Web-UI, falls jemand auf dem Slave auf Update drückt
                force_flag = "/var/www/html/ramdisk/force_bluelink.flag"
                if os.path.exists(force_flag):
                    try: os.remove(force_flag)
                    except: pass

            else:
                # Master antwortet NICHT
                fail_counter += 1
                logger.warning(f"Master offline! Fehler-Zähler: {fail_counter}/{fail_timeout} Minuten")

                if fail_counter == fail_timeout:
                    if auto_failover:
                        logger.critical(f"FAILOVER: Master seit {fail_timeout} Minuten offline. ÜBERNEHME KONTROLLE!")
                        send_telegram(f"🚨 E3DC FAILOVER: Master ({peer_ip}) ist offline! Backup-Pi übernimmt ab sofort die Steuerung.")
                        manage_services("start")
                        was_active_as_slave = True
                    else:
                        logger.warning(f"Master offline, aber Auto-Failover ist DEAKTIVIERT. Slave bleibt im Standby.")
                        send_telegram(f"⚠️ E3DC Master ({peer_ip}) ist offline! (Manuelles Eingreifen erforderlich, Auto-Failover ist aus)")

                elif fail_counter > fail_timeout:
                    if fail_counter % 60 == 0:
                        if auto_failover:
                            logger.info("System läuft weiterhin im Failover-Modus.")
                            send_telegram(f"⚠️ E3DC FAILOVER: Backup-Pi läuft aktiv. Master weiterhin offline (seit {fail_counter} Min).")

            # Config-Sync vom Master empfangen und sicher mergen
            master_cfg_path = "/var/www/html/ramdisk/master_e3dc_v4.json"
            if os.path.exists(master_cfg_path):
                current_master_cfg_mtime = os.path.getmtime(master_cfg_path)
                if current_master_cfg_mtime != last_master_cfg_mtime:
                    try:
                        merge_config(master_cfg_path, CONFIG_PATH)
                        last_master_cfg_mtime = current_master_cfg_mtime
                        logger.info("Konfiguration vom Master synchronisiert (HA-Parameter geschützt).")
                    except Exception as e:
                        logger.error(f"Fehler beim Mergen der Master-Config: {e}")

            current_state = "failover" if was_active_as_slave else "standby"
            write_status("slave", current_state, master_online)

        time.sleep(60) # Loop läuft einmal pro Minute

def main_loop():
    """Betreibt HA mit genau einer geprüften Writer-Lease und fehlersicherer Peerfreigabe."""
    logger.info("E3DC-Control HA manager started with owner lease.")

    owner_lease = OwnerLease()
    fail_counter = 0
    last_sync = 0
    was_active_as_slave = False
    master_auto_recovered = False
    last_config_mtime = 0
    last_master_cfg_mtime = 0

    while True:
        config = load_config()
        mode = config.get("ha_mode", "off").lower()
        raw_peer_ip = config.get("ha_peer_ip", "")
        peer_ip = validate_peer_ip(raw_peer_ip)
        try:
            fail_timeout = int(config.get("ha_fail_timeout", "15"))
        except ValueError:
            fail_timeout = 15
        try:
            sync_interval = int(config.get("ha_sync_interval", "60")) * 60
        except ValueError:
            sync_interval = 3600
        auto_recover = config.get("ha_auto_recover", "1") in ["1", "true"]
        auto_failover = config.get("ha_auto_failover", "1") in ["1", "true"]

        try:
            _write_role_anchor(mode, peer_ip or raw_peer_ip)
        except Exception as exc:
            manage_services("stop", owner_lease=owner_lease)
            write_status(
                mode or "invalid",
                "role_anchor_blocked",
                False,
                last_sync,
                owner_lease=owner_lease,
                safety_reason="role_anchor_failed:%s" % type(exc).__name__,
            )
            time.sleep(60)
            continue

        if mode not in ("off", "master", "slave", "shadow"):
            manage_services("stop", owner_lease=owner_lease)
            write_status(mode, "config_error_invalid_mode", False, last_sync, safety_reason="invalid_mode")
            time.sleep(60)
            continue
        if mode == "off":
            if owner_lease.held:
                manage_services("stop", owner_lease=owner_lease)
            write_status("off", "inactive", False, owner_lease=owner_lease)
            time.sleep(60)
            continue
        if mode == "shadow":
            manage_services("stop", owner_lease=owner_lease)
            write_status("shadow", "observe_only", False, last_sync, safety_reason="shadow_has_no_writer_lease")
            time.sleep(60)
            continue
        if not raw_peer_ip:
            manage_services("stop", owner_lease=owner_lease)
            write_status(mode, "config_error_missing_peer", False, last_sync, safety_reason="missing_peer")
            time.sleep(60)
            continue
        if not peer_ip:
            manage_services("stop", owner_lease=owner_lease)
            write_status(mode, "config_error_invalid_peer", False, last_sync, safety_reason="invalid_peer")
            time.sleep(60)
            continue
        if owner_lease.held and not owner_lease.valid(mode=mode, peer_ip=peer_ip):
            if not manage_services("stop", owner_lease=owner_lease):
                write_status(
                    mode, "owner_context_lost", False, last_sync,
                    owner_lease=owner_lease, safety_reason="writer_stop_unconfirmed",
                )
                time.sleep(60)
                continue
        if peer_points_to_self(peer_ip):
            manage_services("stop", owner_lease=owner_lease)
            write_status(mode, "config_error_self_peer", False, last_sync, safety_reason="self_peer")
            time.sleep(60)
            continue

        if mode == "master":
            peer_online = is_host_online(peer_ip)
            peer_writer_state = query_peer_writer_state(peer_ip) if peer_online else "unknown"
            resumed_master_continuity = False
            already_owner = owner_lease.valid(mode="master", peer_ip=peer_ip)
            if not already_owner and peer_writer_state != "active":
                resumed_master_continuity = owner_lease.resume_same_context_only(
                    "master",
                    peer_ip,
                    context_valid=True,
                )
                already_owner = resumed_master_continuity
            if not owner_admission_allowed("master", already_owner, peer_writer_state):
                if owner_lease.held:
                    manage_services("stop", owner_lease=owner_lease)
                write_status(
                    "master", "peer_confirmation_required", peer_online, last_sync,
                    owner_lease=owner_lease,
                    safety_reason="initial_owner_blocked_peer_writer_%s" % peer_writer_state,
                )
                time.sleep(60)
                continue
            if not owner_lease.acquire_or_renew("master", peer_ip, context_valid=True):
                if owner_lease.held:
                    manage_services("stop", owner_lease=owner_lease)
                write_status(
                    "master", "owner_lease_blocked", peer_online, last_sync,
                    owner_lease=owner_lease, safety_reason=owner_lease.last_error,
                )
                time.sleep(60)
                continue
            if resumed_master_continuity:
                # Kontrollierter Neustart desselben lokalen Owners: Der Peer
                # war vor der Übernahme bereits als stillstehend bestätigt.
                # Ein Rücklesen vom Slave würde nur die Downtime verlängern.
                master_auto_recovered = True
                logger.info("HA same-context owner lease resumed after local restart.")
            if peer_writer_state == "active":
                stopped = manage_services("stop", owner_lease=owner_lease)
                write_status(
                    "master", "peer_writer_conflict", peer_online, last_sync,
                    owner_lease=owner_lease,
                    safety_reason="peer_writer_active" if stopped else "local_writer_stop_unconfirmed",
                )
                time.sleep(60)
                continue

            if auto_recover and not master_auto_recovered:
                if peer_online and peer_writer_state == "quiesced":
                    local_stop_ok = manage_services("stop", owner_lease=owner_lease, release_lease=False)
                    if local_stop_ok and rsync_data(peer_ip, push=False, owner_lease=owner_lease):
                        logger.info("HA auto-recovery completed.")
                    else:
                        logger.warning("HA auto-recovery blocked; no unverified restore was applied.")
                elif peer_online:
                    logger.warning("HA auto-recovery blocked by peer writer state %s.", peer_writer_state)
                master_auto_recovered = True

            if not manage_services("start", owner_lease=owner_lease, mode="master", peer_ip=peer_ip):
                write_status(
                    "master", "writer_start_failed", peer_online, last_sync,
                    owner_lease=owner_lease, safety_reason=owner_lease.last_error,
                )
                time.sleep(60)
                continue

            if os.path.exists(CONFIG_PATH):
                current_config_mtime = os.path.getmtime(CONFIG_PATH)
                if current_config_mtime != last_config_mtime:
                    try:
                        master_data = _read_web_json(CONFIG_PATH)
                        sync_payload = ha_sync_config_payload(master_data)
                        _atomic_write_web_json(
                            "/var/www/html/ramdisk/master_e3dc_v4.json",
                            sync_payload,
                            mode=0o664,
                        )
                        last_config_mtime = current_config_mtime
                    except Exception as exc:
                        logger.error("HA config sync preparation failed: %s", exc)
            if time.time() - last_sync > sync_interval:
                if peer_online:
                    if rsync_data(peer_ip, push=True, owner_lease=owner_lease):
                        last_sync = time.time()
                    else:
                        logger.warning("HA backup sync blocked by ambiguous peer/owner state.")
                else:
                    logger.warning("HA peer offline; backup sync skipped.")
            write_status("master", "active", peer_online, last_sync, owner_lease=owner_lease)

        elif mode == "slave":
            master_online = is_host_online(peer_ip)
            peer_writer_state = query_peer_writer_state(peer_ip)
            safety_reason = ""
            resumed_slave_continuity = False
            if master_online:
                fail_counter = 0
                if was_active_as_slave:
                    if manage_services("stop", owner_lease=owner_lease):
                        was_active_as_slave = False
                        if peer_writer_state == "quiesced" and rsync_data(peer_ip, push=True):
                            send_telegram("E3DC failback completed; backup node returned to standby.")
                        else:
                            safety_reason = "failback_sync_blocked_peer_%s" % peer_writer_state
                    else:
                        safety_reason = "failback_local_stop_unconfirmed"
                standby_stopped = manage_services("stop")
                if standby_stopped and owner_lease.held:
                    owner_lease.release("slave_standby")
                elif owner_lease.held:
                    owner_lease.invalidate("slave_standby_stop_unconfirmed")
                force_flag = "/var/www/html/ramdisk/force_bluelink.flag"
                if os.path.exists(force_flag):
                    try:
                        os.remove(force_flag)
                    except OSError:
                        pass
            else:
                fail_counter += 1
                logger.warning("HA master offline: %s/%s minutes", fail_counter, fail_timeout)
                if (
                    peer_writer_state != "active"
                    and not owner_lease.valid(mode="slave", peer_ip=peer_ip)
                ):
                    resumed_slave_continuity = owner_lease.resume_same_context_only(
                        "slave",
                        peer_ip,
                        context_valid=True,
                    )
                    if resumed_slave_continuity:
                        was_active_as_slave = True
                        logger.info("HA slave failover lease resumed after local restart.")
                if resumed_slave_continuity:
                    if not manage_services(
                        "start",
                        owner_lease=owner_lease,
                        mode="slave",
                        peer_ip=peer_ip,
                    ):
                        safety_reason = "failover_resume_writer_start_failed"
                        was_active_as_slave = False
                elif fail_counter == fail_timeout:
                    if auto_failover:
                        if not owner_admission_allowed("slave", False, peer_writer_state):
                            safety_reason = "failover_blocked_peer_writer_%s" % peer_writer_state
                        elif owner_lease.acquire("slave", peer_ip, context_valid=True) and manage_services(
                            "start", owner_lease=owner_lease, mode="slave", peer_ip=peer_ip
                        ):
                            send_telegram("E3DC failover: peer confirmed quiesced; backup node owns control.")
                            was_active_as_slave = True
                        else:
                            safety_reason = "failover_owner_lease_failed"
                    else:
                        send_telegram("E3DC master offline; automatic failover is disabled.")
                elif fail_counter > fail_timeout and fail_counter % 60 == 0 and auto_failover and was_active_as_slave:
                    logger.info("HA remains in verified failover mode.")
                if was_active_as_slave and not owner_lease.renew("slave", peer_ip, context_valid=True):
                    safety_reason = "failover_owner_lease_expired"
                    manage_services("stop", owner_lease=owner_lease)
                    was_active_as_slave = False

            master_cfg_path = "/var/www/html/ramdisk/master_e3dc_v4.json"
            if master_online and peer_writer_state != "unknown" and not was_active_as_slave and os.path.exists(master_cfg_path):
                current_master_cfg_mtime = os.path.getmtime(master_cfg_path)
                config_artifact_fresh = (time.time() - current_master_cfg_mtime) <= max(180.0, float(sync_interval) * 2.0)
                if current_master_cfg_mtime != last_master_cfg_mtime and config_artifact_fresh:
                    try:
                        merge_config(master_cfg_path, CONFIG_PATH)
                        last_master_cfg_mtime = current_master_cfg_mtime
                    except Exception as exc:
                        logger.error("HA config merge failed: %s", exc)
            current_state = "failover" if was_active_as_slave else "standby"
            if safety_reason:
                current_state = "blocked"
            write_status(
                "slave", current_state, master_online,
                owner_lease=owner_lease, safety_reason=safety_reason,
            )

        time.sleep(60)


if __name__ == "__main__":
    if os.geteuid() == 0 and not PRIVILEGED_RUNTIME_BOUND:
        raise SystemExit(
            "HA-Manager verweigert Root-Start ohne root-eigene Laufzeitbindung."
        )
    main_loop()
