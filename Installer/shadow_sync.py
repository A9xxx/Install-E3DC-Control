#!/usr/bin/env python3
"""Read-only Shadow sync and simulator entry point.

The shadow instance reads snapshots from one active E3DC-Control instance,
writes only local shadow files and runs the already existing read-only storage
and wallbox simulators. It must never talk to RSCP, Modbus, MQTT, Shelly or a
real wallbox driver.
"""

from __future__ import annotations

import base64
import dataclasses
import hashlib
import ipaddress
import json
import logging
import math
import os
import re
import signal
import socket
import stat
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple


SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

try:
    from storage_parallel_regulator import ParallelStorageRegulator
    from wallbox_parallel_simulator import ShadowWallboxSimulator, ShadowWallboxState
    from control_command_guard import evaluate_wallbox_command
except Exception:  # pragma: no cover - package import fallback
    from .storage_parallel_regulator import ParallelStorageRegulator  # type: ignore
    from .wallbox_parallel_simulator import ShadowWallboxSimulator, ShadowWallboxState  # type: ignore
    from .control_command_guard import evaluate_wallbox_command  # type: ignore


RAMDISK = "/var/www/html/ramdisk"
DATA_DIR = "/var/www/html/data"
LOG_DIR = "/var/www/html/logs"
CONFIG_FILE = os.path.join(DATA_DIR, "e3dc_v4.json")
STATUS_FILE = os.path.join(RAMDISK, "shadow_sync_status.json")
HISTORY_FILE = os.path.join(RAMDISK, "shadow_sync_history.jsonl")
STORAGE_SHADOW_FILE = os.path.join(RAMDISK, "shadow_storage_parallel_state.json")
WALLBOX_SHADOW_FILE = os.path.join(RAMDISK, "wallbox_parallel_shadow_state.json")
COMMAND_GUARD_STATUS_FILE = os.path.join(RAMDISK, "shadow_control_status.json")
COMMAND_GUARD_HISTORY_FILE = os.path.join(RAMDISK, "shadow_control_history.jsonl")
MAX_HISTORY_LINES = 720
HISTORY_TRIM_HEADROOM_MIN = 64
HISTORY_MAX_RECORD_BYTES = 256 * 1024
MAX_FETCH_BYTES = 3 * 1024 * 1024
MAX_BUNDLE_FETCH_BYTES = 8 * 1024 * 1024
STATUS_LOG_HEARTBEAT_S = 15 * 60
SHADOW_SNAPSHOT_ENDPOINT = "get_shadow_snapshot.php"
SHADOW_SNAPSHOT_SCHEMA = "e3dc_shadow_snapshot_v1"
SHADOW_BUNDLE_SCHEMA = "e3dc_shadow_snapshot_bundle_v1"
SHADOW_BUNDLE_RESOURCE = "bundle"
SHADOW_SNAPSHOT_CONTRACT = "e3dc-shadow-read-v1"
SHADOW_SNAPSHOT_TOKEN_RE = re.compile(r"\A[0-9a-fA-F]{64}\Z")
SHADOW_PRIMARY_FUTURE_TOLERANCE_S = 60.0

SNAPSHOT_TARGETS: Tuple[Tuple[str, str, str, str], ...] = (
    ("live_data", "live_data", RAMDISK, "shadow_master_live_data_py.json"),
    ("storage_state", "storage_state", RAMDISK, "shadow_master_storage_manager_state.json"),
    ("storage_plan", "storage_plan", RAMDISK, "shadow_master_storage_plan.json"),
    ("wb_budget", "wb_budget", RAMDISK, "shadow_master_wb_pv_budget.json"),
    ("wb_intent", "wb_intent", RAMDISK, "shadow_master_wallbox_storage_intent.json"),
    ("wallbox_native", "wallbox_native", RAMDISK, "shadow_master_wallbox_native.json"),
    ("config", "config", DATA_DIR, "shadow_master_e3dc_v4.json"),
)

STORAGE_REQUIRED_TARGETS = frozenset({
    "live_data",
    "storage_state",
    "storage_plan",
    "wb_budget",
    "wb_intent",
    "config",
})
WALLBOX_REQUIRED_TARGETS = frozenset({
    "live_data",
    "wb_budget",
    "wallbox_native",
    "config",
})

SHADOW_STANDBY_SERVICES: Tuple[str, ...] = (
    "e3dc",
    "e3dc-live",
    "e3dc-storage-manager",
    "e3dc-storage-simulator",
    "e3dc-epex-manager",
    "e3dc-weather-manager",
    "e3dc-wallbox-manager",
    "energy_manager",
    "e3dc-idm-live",
    "e3dc-lux-live",
    "e3dc-stiebel-live",
    "e3dc-dimplex-live",
    "e3dc-heizstab",
    "e3dc-climate-live",
    "e3dc-climate-control",
    "e3dc-mqtt-hub",
    "e3dc-bluelink",
    "e3dc-matter-bridge",
    "e3dc-websocket",
    "e3dc-ha",
    "e3dc-notifier",
)

SECRET_KEY_PARTS = (
    "password",
    "passwd",
    "token",
    "api_key",
    "apikey",
    "secret",
    "aes",
)


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - ShadowSync - %(levelname)s - %(message)s",
    datefmt="%d.%m %H:%M:%S",
)
log = logging.getLogger("ShadowSync")
_stop = False
_TMPFS_VERIFICATION_CACHE: Dict[Tuple[str, int], bool] = {}
_HISTORY_APPEND_STATE: Dict[Tuple[str, int], Dict[str, Any]] = {}


class _ShadowNoRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> Optional[urllib.request.Request]:
        raise urllib.error.HTTPError(
            req.full_url,
            int(code),
            "shadow_redirect_not_allowed",
            headers,
            fp,
        )


def _open_shadow_request(request: urllib.request.Request, timeout_s: float):
    # Kein Umgebungsproxy und kein Redirect: Der geprüfte Peer bleibt exakt
    # das Ziel, an das Contract- und Authentisierungsheader gesendet werden.
    opener = urllib.request.build_opener(
        urllib.request.ProxyHandler({}),
        _ShadowNoRedirectHandler(),
    )
    return opener.open(request, timeout=timeout_s)


def _sig(signum: int, _frame: Any) -> None:
    global _stop
    _stop = True
    log.info("Signal %d - beende.", signum)


signal.signal(signal.SIGTERM, _sig)
signal.signal(signal.SIGINT, _sig)


def _safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(round(float(str(value).strip().replace(",", "."))))
    except Exception:
        return int(default)


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        return float(str(value).strip().replace(",", "."))
    except Exception:
        return float(default)


def _cfg_bool(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text == "":
        return default
    return text in ("1", "true", "yes", "on", "ja", "ein")


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def _write_json_atomic(path: str, data: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o664)
    except Exception:
        pass


def _canonical_json_bytes(data: Dict[str, Any]) -> bytes:
    return json.dumps(
        data,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ).encode("utf-8")


def _write_json_atomic_if_changed(path: str, data: Dict[str, Any]) -> bool:
    """Persistiert JSON nur, wenn sich dessen wirksamer Inhalt geändert hat."""
    expected = _canonical_json_bytes(data)
    try:
        with open(path, "rb") as handle:
            current = handle.read()
        if current == expected:
            return False
        parsed = json.loads(current.decode("utf-8-sig"))
        if isinstance(parsed, dict) and _canonical_json_bytes(parsed) == expected:
            return False
    except (OSError, UnicodeError, ValueError, TypeError):
        # Fehlend oder beschädigt: Der atomare Schreibpfad stellt die Datei
        # sofort wieder her und meldet Schreibfehler weiterhin an den Aufrufer.
        pass
    _write_json_atomic(path, data)
    return True


def _mountinfo_path(value: str) -> str:
    return (
        value.replace("\\040", " ")
        .replace("\\011", "\t")
        .replace("\\012", "\n")
        .replace("\\134", "\\")
    )


def _path_is_verified_tmpfs(path: str) -> bool:
    parent = os.path.realpath(os.path.dirname(path) or ".")
    try:
        parent_stat = os.stat(parent)
    except OSError:
        return False
    cache_key = (parent, int(parent_stat.st_dev))
    cached = _TMPFS_VERIFICATION_CACHE.get(cache_key)
    if cached is not None:
        return cached

    best_mount = ""
    best_type = ""
    try:
        with open("/proc/self/mountinfo", "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if " - " not in line:
                    continue
                left, right = line.rstrip("\n").split(" - ", 1)
                left_fields = left.split()
                right_fields = right.split()
                if len(left_fields) < 5 or not right_fields:
                    continue
                mount_point = os.path.realpath(_mountinfo_path(left_fields[4]))
                if parent != mount_point and not parent.startswith(mount_point.rstrip("/") + "/"):
                    continue
                if len(mount_point) >= len(best_mount):
                    best_mount = mount_point
                    best_type = right_fields[0]
    except OSError:
        best_type = ""
    verified = best_type in {"tmpfs", "ramfs"}
    _TMPFS_VERIFICATION_CACHE[cache_key] = verified
    return verified


def _history_line_count_fd(fd: int) -> int:
    os.lseek(fd, 0, os.SEEK_SET)
    count = 0
    last = b""
    while True:
        chunk = os.read(fd, 128 * 1024)
        if not chunk:
            break
        count += chunk.count(b"\n")
        last = chunk[-1:]
    if last and last != b"\n":
        count += 1
    os.lseek(fd, 0, os.SEEK_END)
    return count


def _trim_history_tmpfs(path: str, max_lines: int) -> Optional[Tuple[os.stat_result, int]]:
    if not _path_is_verified_tmpfs(path):
        return None
    rows: List[bytes] = []
    source_stat: Optional[os.stat_result] = None
    try:
        flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
        source_fd = os.open(path, flags)
        try:
            source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(source_stat.st_mode):
                return None
            with os.fdopen(source_fd, "rb", closefd=False) as handle:
                for row in handle:
                    if row.strip():
                        rows.append(row if row.endswith(b"\n") else row + b"\n")
                        if len(rows) > max_lines:
                            del rows[0]
            after_read = os.fstat(source_fd)
            current = os.lstat(path)
            if (
                (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns)
                != (after_read.st_dev, after_read.st_ino, after_read.st_size, after_read.st_mtime_ns)
                or (source_stat.st_dev, source_stat.st_ino, source_stat.st_size, source_stat.st_mtime_ns)
                != (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns)
            ):
                return None
        finally:
            os.close(source_fd)
    except OSError:
        return None

    tmp = path + ".trim.tmp"
    tmp_fd: Optional[int] = None
    try:
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_TRUNC
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        tmp_fd = os.open(tmp, flags, 0o664)
        for row in rows[-max_lines:]:
            view = memoryview(row)
            while view:
                written = os.write(tmp_fd, view)
                if written <= 0:
                    raise OSError("history_trim_write_failed")
                view = view[written:]
        os.fchmod(tmp_fd, 0o664)
        os.close(tmp_fd)
        tmp_fd = None
        os.replace(tmp, path)
        return os.stat(path), len(rows[-max_lines:])
    except OSError:
        return None
    finally:
        if tmp_fd is not None:
            try:
                os.close(tmp_fd)
            except OSError:
                pass
        try:
            os.unlink(tmp)
        except OSError:
            pass


def _append_history(path: str, record: Dict[str, Any], max_lines: int = MAX_HISTORY_LINES) -> bool:
    max_lines = max(1, int(max_lines))
    directory = os.path.dirname(path) or "."
    try:
        os.makedirs(directory, exist_ok=True)
    except OSError:
        return False
    if not _path_is_verified_tmpfs(path):
        return False

    encoded = (
        json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n"
    ).encode("utf-8")
    if len(encoded) > HISTORY_MAX_RECORD_BYTES:
        return False

    state_key = (os.path.abspath(path), max_lines)
    headroom = max(HISTORY_TRIM_HEADROOM_MIN, max_lines // 4)
    default_trim_at = max_lines + headroom
    fd: Optional[int] = None
    try:
        flags = (
            os.O_RDWR
            | os.O_CREAT
            | os.O_APPEND
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0)
        )
        fd = os.open(path, flags, 0o664)
        before = os.fstat(fd)
        if not stat.S_ISREG(before.st_mode):
            return False
        parent_stat = os.stat(os.path.realpath(directory))
        if before.st_dev != parent_stat.st_dev:
            return False
        if stat.S_IMODE(before.st_mode) != 0o664:
            os.fchmod(fd, 0o664)

        state = _HISTORY_APPEND_STATE.get(state_key)
        identity = (int(before.st_dev), int(before.st_ino))
        if (
            not isinstance(state, dict)
            or state.get("identity") != identity
            or state.get("size") != int(before.st_size)
        ):
            state = {
                "identity": identity,
                "line_count": _history_line_count_fd(fd),
                "size": int(before.st_size),
                "next_trim_at": default_trim_at,
            }
            _HISTORY_APPEND_STATE[state_key] = state

        view = memoryview(encoded)
        while view:
            written = os.write(fd, view)
            if written <= 0:
                raise OSError("history_append_failed")
            view = view[written:]
        after = os.fstat(fd)
        state["line_count"] = int(state.get("line_count", 0)) + 1
        state["size"] = int(after.st_size)
        line_count = int(state["line_count"])
        next_trim_at = int(state.get("next_trim_at", default_trim_at))
    except (OSError, TypeError, ValueError):
        return False
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass

    if line_count > next_trim_at:
        trimmed = _trim_history_tmpfs(path, max_lines)
        if trimmed is not None:
            trimmed_stat, trimmed_count = trimmed
            _HISTORY_APPEND_STATE[state_key] = {
                "identity": (int(trimmed_stat.st_dev), int(trimmed_stat.st_ino)),
                "line_count": int(trimmed_count),
                "size": int(trimmed_stat.st_size),
                "next_trim_at": default_trim_at,
            }
        else:
            state["next_trim_at"] = line_count + headroom
    return True


def load_config(config_path: str = CONFIG_FILE) -> Dict[str, Any]:
    return _read_json(config_path)


def _redact_config(data: Dict[str, Any]) -> Dict[str, Any]:
    def redact(value: Any, depth: int = 0) -> Any:
        if depth > 16:
            return None
        if isinstance(value, dict):
            redacted: Dict[str, Any] = {}
            for key, child in value.items():
                key_text = str(key).lower()
                if any(part in key_text for part in SECRET_KEY_PARTS):
                    continue
                redacted[key] = redact(child, depth + 1)
            return redacted
        if isinstance(value, list):
            return [redact(child, depth + 1) for child in value]
        return value

    result = redact(data or {})
    return result if isinstance(result, dict) else {}


def _shadow_snapshot_token(cfg: Dict[str, Any]) -> str:
    raw = cfg.get("shadow_snapshot_token")
    if not isinstance(raw, str):
        return ""
    token = raw.strip()
    if SHADOW_SNAPSHOT_TOKEN_RE.fullmatch(token) is None:
        return ""
    return token.lower()


def _http_shadow_address_allowed(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if address.is_loopback:
        return True
    if isinstance(address, ipaddress.IPv4Address):
        return any(address in network for network in (
            ipaddress.ip_network("10.0.0.0/8"),
            ipaddress.ip_network("172.16.0.0/12"),
            ipaddress.ip_network("192.168.0.0/16"),
            ipaddress.ip_network("100.64.0.0/10"),
        ))
    return address in ipaddress.ip_network("fc00::/7")


def _normalize_master_url(cfg: Dict[str, Any]) -> str:
    raw = str(cfg.get("shadow_master_url") or "").strip()
    if not raw:
        peer = str(cfg.get("shadow_master_ip") or cfg.get("ha_peer_ip") or "").strip()
        raw = peer
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    try:
        parsed = urllib.parse.urlparse(raw)
        host = (parsed.hostname or "").strip()
        port = parsed.port
    except ValueError:
        return ""
    scheme = (parsed.scheme or "").lower()
    if (
        scheme not in {"http", "https"}
        or not host
        or parsed.username is not None
        or parsed.password is not None
        or parsed.path not in ("", "/")
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        return ""
    if scheme == "http" and not _http_shadow_address_allowed(host):
        return ""
    if any(char.isspace() for char in host):
        return ""

    host_part = f"[{host}]" if ":" in host else host
    netloc = host_part if port is None else f"{host_part}:{port}"
    return urllib.parse.urlunparse((scheme, netloc, "", "", "", ""))


def _local_identifiers() -> set[str]:
    values = {"localhost", "127.0.0.1", "::1"}
    try:
        host = socket.gethostname()
        if host:
            values.add(host.lower())
            values.add(socket.getfqdn(host).lower())
    except Exception:
        pass
    try:
        for ip in socket.gethostbyname_ex(socket.gethostname())[2]:
            values.add(str(ip).lower())
    except Exception:
        pass
    try:
        result = subprocess.run(
            ["hostname", "-I"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=3,
        )
        for item in (result.stdout or "").split():
            values.add(item.strip().lower())
    except Exception:
        pass
    return {item for item in values if item}


def master_points_to_self(master_url: str) -> bool:
    try:
        parsed = urllib.parse.urlparse(master_url)
        host = (parsed.hostname or "").strip().lower()
        if not host:
            return False
        local = _local_identifiers()
        if host in local:
            return True
        try:
            for info in socket.getaddrinfo(host, None):
                ip = str(info[4][0]).lower()
                if ip in local:
                    return True
        except Exception:
            return False
    except Exception:
        return False
    return False


def _service_active(service: str) -> bool:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", service],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
        )
        return result.returncode == 0 and (result.stdout or "").strip() == "active"
    except Exception:
        return False


def active_writer_services(checker: Optional[Callable[[str], bool]] = None) -> List[str]:
    is_active = checker or _service_active
    active: List[str] = []
    for service in SHADOW_STANDBY_SERVICES:
        if is_active(service):
            active.append(service)
    return active


def _fetch_json(
    base_url: str,
    resource: str,
    timeout_s: float,
    snapshot_token: str,
) -> Tuple[Any, Dict[str, Any]]:
    endpoint = urllib.parse.urljoin(
        base_url.rstrip("/") + "/",
        SHADOW_SNAPSHOT_ENDPOINT,
    )
    url = endpoint + "?" + urllib.parse.urlencode({"resource": resource})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "E3DC-Control-ShadowSync/2",
            "X-E3DC-Shadow-Contract": SHADOW_SNAPSHOT_CONTRACT,
            "X-E3DC-Shadow-Token": snapshot_token,
        },
    )
    with _open_shadow_request(request, timeout_s) as response:
        raw = response.read(MAX_FETCH_BYTES + 1)
    if len(raw) > MAX_FETCH_BYTES:
        raise ValueError(f"Antwort zu groß: {resource}")
    envelope = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(envelope, dict):
        raise ValueError(f"Shadow-Vertrag fehlt: {resource}")
    if envelope.get("schema_version") != SHADOW_SNAPSHOT_SCHEMA:
        raise ValueError(f"Shadow-Vertrag unbekannt: {resource}")
    if str(envelope.get("resource") or "") != resource:
        raise ValueError(f"Shadow-Ressource falsch gebunden: {resource}")
    if (
        envelope.get("shadow_only") is not True
        or envelope.get("commands_allowed") is not False
        or envelope.get("control_effect") is not False
    ):
        raise ValueError(f"Shadow-Sicherheitsvertrag unvollständig: {resource}")
    data = envelope.get("payload")
    if not isinstance(data, dict):
        raise ValueError(f"Shadow-Nutzlast ungültig: {resource}")
    meta = {
        "url": url,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": envelope.get("schema_version"),
        "source_mtime_ts": envelope.get("source_mtime_ts"),
        "payload_sha256": envelope.get("payload_sha256"),
    }
    return data, meta


def _fetch_bundle(
    base_url: str,
    timeout_s: float,
    snapshot_token: str,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    endpoint = urllib.parse.urljoin(
        base_url.rstrip("/") + "/",
        SHADOW_SNAPSHOT_ENDPOINT,
    )
    url = endpoint + "?" + urllib.parse.urlencode({"resource": SHADOW_BUNDLE_RESOURCE})
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "E3DC-Control-ShadowSync/3",
            "X-E3DC-Shadow-Contract": SHADOW_SNAPSHOT_CONTRACT,
            "X-E3DC-Shadow-Token": snapshot_token,
        },
    )
    with _open_shadow_request(request, timeout_s) as response:
        raw = response.read(MAX_BUNDLE_FETCH_BYTES + 1)
    if len(raw) > MAX_BUNDLE_FETCH_BYTES:
        raise ValueError("bundle_too_large")
    envelope = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(envelope, dict):
        raise ValueError("bundle_contract_missing")
    if (
        envelope.get("schema_version") != SHADOW_BUNDLE_SCHEMA
        or envelope.get("resource") != SHADOW_BUNDLE_RESOURCE
    ):
        raise ValueError("bundle_contract_unknown")
    if (
        envelope.get("shadow_only") is not True
        or envelope.get("commands_allowed") is not False
        or envelope.get("control_effect") is not False
    ):
        raise ValueError("bundle_safety_contract_invalid")

    resources = envelope.get("resources")
    if not isinstance(resources, dict):
        raise ValueError("bundle_resources_missing")
    expected = {resource for _key, resource, _target_dir, _filename in SNAPSHOT_TARGETS}
    if set(resources) != expected:
        raise ValueError("bundle_resources_unbound")
    if any(not isinstance(item, dict) or not isinstance(item.get("ok"), bool) for item in resources.values()):
        raise ValueError("bundle_resource_status_invalid")

    ok_count = sum(1 for item in resources.values() if item.get("ok") is True)
    error_count = len(resources) - ok_count
    declared_counts = (
        envelope.get("resource_count"),
        envelope.get("ok_count"),
        envelope.get("error_count"),
    )
    if any(isinstance(value, bool) or not isinstance(value, int) for value in declared_counts):
        raise ValueError("bundle_counts_invalid")
    if not isinstance(envelope.get("complete"), bool):
        raise ValueError("bundle_counts_invalid")
    generated_at = envelope.get("generated_at_ts")
    if isinstance(generated_at, bool) or not isinstance(generated_at, int) or generated_at <= 0:
        raise ValueError("bundle_generated_at_invalid")
    if (
        envelope.get("resource_count") != len(resources)
        or envelope.get("ok_count") != ok_count
        or envelope.get("error_count") != error_count
        or envelope.get("complete") is not (error_count == 0)
    ):
        raise ValueError("bundle_counts_invalid")
    return resources, {
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
        "schema_version": envelope.get("schema_version"),
        "generated_at_ts": generated_at,
    }


def _decode_bundle_resource(resource: str, item: Dict[str, Any]) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    if (
        item.get("schema_version") != SHADOW_SNAPSHOT_SCHEMA
        or item.get("resource") != resource
    ):
        raise ValueError("resource_contract_invalid")
    if item.get("ok") is not True:
        error_code = str(item.get("error_code") or "resource_unavailable")
        if (
            not error_code
            or len(error_code) > 96
            or any(char not in "abcdefghijklmnopqrstuvwxyz0123456789_" for char in error_code)
        ):
            error_code = "resource_unavailable"
        raise ValueError(f"server_{error_code}")
    if item.get("payload_encoding") != "base64-json-utf8":
        raise ValueError("payload_encoding_invalid")

    declared_bytes = item.get("payload_bytes")
    if isinstance(declared_bytes, bool) or not isinstance(declared_bytes, int):
        raise ValueError("payload_size_invalid")
    if declared_bytes < 2 or declared_bytes > MAX_FETCH_BYTES:
        raise ValueError("payload_size_invalid")
    encoded = item.get("payload_base64")
    if not isinstance(encoded, str):
        raise ValueError("payload_missing")
    max_encoded_chars = ((MAX_FETCH_BYTES + 2) // 3) * 4
    if len(encoded) > max_encoded_chars:
        raise ValueError("payload_too_large")
    try:
        raw = base64.b64decode(encoded.encode("ascii"), validate=True)
    except Exception as exc:
        raise ValueError("payload_base64_invalid") from exc
    if len(raw) != declared_bytes:
        raise ValueError("payload_size_mismatch")

    expected_sha256 = item.get("payload_sha256")
    actual_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    if expected_sha256 != actual_sha256:
        raise ValueError("payload_integrity_mismatch")
    try:
        payload = json.loads(raw.decode("utf-8"))
    except (UnicodeError, ValueError, TypeError) as exc:
        raise ValueError("payload_json_invalid") from exc
    if not isinstance(payload, dict):
        raise ValueError("payload_object_required")

    source_mtime = item.get("source_mtime_ts")
    if isinstance(source_mtime, bool) or not isinstance(source_mtime, int) or source_mtime < 0:
        raise ValueError("source_mtime_invalid")
    return payload, {
        "bytes": declared_bytes,
        "sha256": actual_sha256,
        "schema_version": item.get("schema_version"),
        "source_mtime_ts": source_mtime,
        "payload_sha256": actual_sha256,
    }


def _bundle_transport_error_code(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        return f"http_{int(exc.code)}"
    if isinstance(exc, (urllib.error.URLError, TimeoutError, socket.timeout)):
        return "transport_unavailable"
    message = str(exc)
    allowed = {
        "bundle_too_large",
        "bundle_contract_missing",
        "bundle_contract_unknown",
        "bundle_safety_contract_invalid",
        "bundle_resources_missing",
        "bundle_resources_unbound",
        "bundle_resource_status_invalid",
        "bundle_counts_invalid",
        "bundle_generated_at_invalid",
    }
    return message if message in allowed else "bundle_invalid"


def fetch_master_snapshot(
    cfg: Dict[str, Any],
    *,
    ramdisk_dir: str = RAMDISK,
    data_dir: str = DATA_DIR,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    master_url = _normalize_master_url(cfg)
    timeout_s = max(0.5, min(15.0, _safe_float(cfg.get("shadow_fetch_timeout_s"), 2.5)))
    snapshot_token = _shadow_snapshot_token(cfg)
    fetched: Dict[str, Any] = {}
    targets: Dict[str, Any] = {}
    errors: List[str] = []
    bundle_source = f"{SHADOW_SNAPSHOT_ENDPOINT}?resource={SHADOW_BUNDLE_RESOURCE}"
    if not snapshot_token:
        for key, _resource, target_dir, filename in SNAPSHOT_TARGETS:
            out_dir = data_dir if target_dir == DATA_DIR else ramdisk_dir
            out_path = os.path.join(out_dir, filename)
            targets[key] = {
                "ok": False,
                "fresh": False,
                "path": out_path,
                "source": bundle_source,
                "error": "snapshot_token_invalid",
            }
        return fetched, {
            "targets": targets,
            "errors": ["snapshot_token_invalid"],
            "transport": {
                "resource": SHADOW_BUNDLE_RESOURCE,
                "request_count": 0,
                "ok": False,
                "error": "snapshot_token_invalid",
            },
        }
    try:
        bundle, bundle_meta = _fetch_bundle(master_url, timeout_s, snapshot_token)
    except Exception as exc:
        error_code = _bundle_transport_error_code(exc)
        for key, resource, target_dir, filename in SNAPSHOT_TARGETS:
            out_dir = data_dir if target_dir == DATA_DIR else ramdisk_dir
            out_path = os.path.join(out_dir, filename)
            errors.append(f"{key}: {error_code}")
            targets[key] = {
                "ok": False,
                "fresh": False,
                "path": out_path,
                "source": bundle_source,
                "error": error_code,
            }
        return fetched, {
            "targets": targets,
            "errors": errors,
            "transport": {
                "resource": SHADOW_BUNDLE_RESOURCE,
                "request_count": 1,
                "ok": False,
                "error": error_code,
            },
        }

    fetched_ts = int(time.time())
    for key, resource, target_dir, filename in SNAPSHOT_TARGETS:
        out_dir = data_dir if target_dir == DATA_DIR else ramdisk_dir
        out_path = os.path.join(out_dir, filename)
        try:
            data, meta = _decode_bundle_resource(resource, bundle[resource])
            stored = _redact_config(data) if key == "config" else data
            if key == "config":
                _write_json_atomic_if_changed(out_path, stored)
            else:
                _write_json_atomic(out_path, stored)
            fetched[key] = stored
            targets[key] = {
                "ok": True,
                "fresh": True,
                "path": out_path,
                "source": bundle_source,
                "fetched_ts": fetched_ts,
                "bytes": meta.get("bytes"),
                "sha256": meta.get("sha256"),
                "schema_version": meta.get("schema_version"),
                "source_mtime_ts": meta.get("source_mtime_ts"),
                "payload_sha256": meta.get("payload_sha256"),
            }
        except Exception as exc:
            error_code = str(exc) or "resource_invalid"
            errors.append(f"{key}: {error_code}")
            targets[key] = {
                "ok": False,
                "fresh": False,
                "path": out_path,
                "source": bundle_source,
                "error": error_code,
            }
    return fetched, {
        "targets": targets,
        "errors": errors,
        "transport": {
            "resource": SHADOW_BUNDLE_RESOURCE,
            "request_count": 1,
            "ok": True,
            "bytes": bundle_meta.get("bytes"),
            "sha256": bundle_meta.get("sha256"),
            "schema_version": bundle_meta.get("schema_version"),
            "generated_at_ts": bundle_meta.get("generated_at_ts"),
        },
    }


def _shadow_active_state(master_state: Dict[str, Any], live: Dict[str, Any]) -> Dict[str, Any]:
    state = dict(master_state or {})
    state.setdefault("now_ts_s", time.time())
    state.setdefault("soc", live.get("SOC", live.get("soc")))
    state.setdefault("pv_w", live.get("PV_Power", live.get("pv")))
    state.setdefault("grid_w", live.get("Grid_Power", live.get("grid")))
    state.setdefault("bat_w", live.get("Battery_Power", live.get("bat")))
    state.setdefault("home_ema_w", live.get("Home_Power", live.get("home")))
    return state


def run_storage_shadow(fetched: Dict[str, Any], cfg: Dict[str, Any], *, output_path: str = STORAGE_SHADOW_FILE) -> Dict[str, Any]:
    live = fetched.get("live_data") if isinstance(fetched.get("live_data"), dict) else {}
    state = fetched.get("storage_state") if isinstance(fetched.get("storage_state"), dict) else {}
    plan = fetched.get("storage_plan") if isinstance(fetched.get("storage_plan"), dict) else {}
    wb_budget = fetched.get("wb_budget") if isinstance(fetched.get("wb_budget"), dict) else {}
    wb_intent = fetched.get("wb_intent") if isinstance(fetched.get("wb_intent"), dict) else {}
    master_cfg = fetched.get("config") if isinstance(fetched.get("config"), dict) else {}
    sim_cfg = dict(cfg or {})
    sim_cfg.update(master_cfg)
    payload = ParallelStorageRegulator(sim_cfg).decide(
        active_state=_shadow_active_state(state, live),
        live=live,
        plan=plan,
        wb_budget=wb_budget,
        wb_intent=wb_intent,
    )
    payload["source"] = "shadow_master"
    payload["shadow_only"] = True
    _write_json_atomic(output_path, payload)
    return payload


def _wallbox_state_from_dict(data: Dict[str, Any]) -> ShadowWallboxState:
    allowed = {field.name for field in dataclasses.fields(ShadowWallboxState)}
    kwargs = {key: value for key, value in (data or {}).items() if key in allowed}
    return ShadowWallboxState(**kwargs)


def _infer_wallbox_power(live: Dict[str, Any], native: Dict[str, Any]) -> float:
    for key in ("Wallbox_Power", "wallbox_power", "wb_power", "wb_total_power"):
        value = _safe_float(live.get(key), 0.0)
        if abs(value) > 1:
            return abs(value)
    for key in ("total_power_w", "power_w", "wb_total_w"):
        value = _safe_float(native.get(key), 0.0)
        if abs(value) > 1:
            return abs(value)
    return 0.0


def _infer_connected(native: Dict[str, Any], measured_w: float) -> bool:
    rows = native.get("wb_details") if isinstance(native.get("wb_details"), list) else []
    if rows:
        for row in rows:
            if isinstance(row, dict) and str(row.get("connected", row.get("car_connected", ""))).lower() in ("1", "true", "yes", "on"):
                return True
    return measured_w > 50.0


def _payload_timestamp_s(data: Dict[str, Any]) -> Optional[float]:
    for key in ("_ts", "ts", "timestamp", "timestamp_s", "last_update_ts", "last_update", "time"):
        if key not in data:
            continue
        raw = data.get(key)
        if isinstance(raw, bool):
            continue
        try:
            value = float(raw)
        except (TypeError, ValueError):
            continue
        if not math.isfinite(value) or value <= 0:
            continue
        if value > 10_000_000_000:
            value = value / 1000.0
        if value > 946_684_800:
            return value
    return None


def _primary_live_timestamp_contract(
    live_data: Any,
    *,
    max_age_s: float,
    now_s: Optional[float] = None,
) -> Dict[str, Any]:
    current_s = time.time() if now_s is None else float(now_s)
    if not isinstance(live_data, dict):
        return {"ok": False, "reason": "snapshot_missing_live"}
    if "_ts" not in live_data:
        return {"ok": False, "reason": "snapshot_timestamp_missing"}
    raw = live_data.get("_ts")
    if isinstance(raw, bool):
        return {"ok": False, "reason": "snapshot_timestamp_invalid"}
    try:
        payload_ts = float(raw)
    except (TypeError, ValueError):
        return {"ok": False, "reason": "snapshot_timestamp_invalid"}
    if not math.isfinite(payload_ts) or payload_ts <= 0:
        return {"ok": False, "reason": "snapshot_timestamp_invalid"}
    if payload_ts > 10_000_000_000:
        payload_ts /= 1000.0
    if payload_ts <= 946_684_800:
        return {"ok": False, "reason": "snapshot_timestamp_invalid"}
    if payload_ts > current_s + SHADOW_PRIMARY_FUTURE_TOLERANCE_S:
        return {
            "ok": False,
            "reason": "snapshot_timestamp_future",
            "timestamp_s": payload_ts,
            "age_s": current_s - payload_ts,
        }
    age_s = max(0.0, current_s - payload_ts)
    if age_s > float(max_age_s):
        return {
            "ok": False,
            "reason": "snapshot_stale",
            "timestamp_s": payload_ts,
            "age_s": age_s,
        }
    return {
        "ok": True,
        "reason": "snapshot_fresh",
        "timestamp_s": payload_ts,
        "age_s": age_s,
    }


def _snapshot_age_s(fetched: Dict[str, Any]) -> Optional[float]:
    data = fetched.get("live_data")
    if isinstance(data, dict):
        payload_ts = _payload_timestamp_s(data)
        if payload_ts is not None:
            return max(0.0, time.time() - payload_ts)
    return None


def _required_targets_ready(report: Dict[str, Any], required: Iterable[str]) -> bool:
    targets = report.get("targets")
    if not isinstance(targets, dict):
        return False
    return all(
        isinstance(targets.get(key), dict)
        and targets[key].get("ok") is True
        and targets[key].get("fresh") is True
        for key in required
    )


def run_wallbox_shadow(
    fetched: Dict[str, Any],
    cfg: Dict[str, Any],
    *,
    output_path: str = WALLBOX_SHADOW_FILE,
    guard_status_path: str = COMMAND_GUARD_STATUS_FILE,
    guard_history_path: str = COMMAND_GUARD_HISTORY_FILE,
) -> Dict[str, Any]:
    live = fetched.get("live_data") if isinstance(fetched.get("live_data"), dict) else {}
    wb_budget = fetched.get("wb_budget") if isinstance(fetched.get("wb_budget"), dict) else {}
    native = fetched.get("wallbox_native") if isinstance(fetched.get("wallbox_native"), dict) else {}
    master_cfg = fetched.get("config") if isinstance(fetched.get("config"), dict) else {}
    sim_cfg = dict(cfg or {})
    sim_cfg.update(master_cfg)
    simulator = ShadowWallboxSimulator(sim_cfg)
    previous = _read_json(output_path)
    state_data = previous.get("state") if isinstance(previous.get("state"), dict) else {}
    measured_w = _infer_wallbox_power(live, native)
    phases = 3 if measured_w >= 4000 else 1
    now_s = time.time()
    if state_data:
        state = _wallbox_state_from_dict(state_data)
    else:
        state = simulator.initial_state(phases=phases, amp=0, real_power_w=measured_w, ts=now_s)
    sample = {
        "ts_s": now_s,
        "mode": sim_cfg.get("wb1_mode", sim_cfg.get("wb_native_mode", 2)),
        "budget_w": max(0, _safe_int(wb_budget.get("budget_w", wb_budget.get("iAVal_w")), 0)),
        "grid_w": _safe_float(live.get("Grid_Power", live.get("grid")), 0.0),
        "car_connected": _infer_connected(native, measured_w),
        "grid_allowed": False,
        "storage_floor_reachable": True,
    }
    previous_amp = int(state.command_amp)
    previous_phases = int(state.command_phases)
    payload = simulator.step(state, sample)
    payload["source"] = "shadow_master"
    payload["shadow_only"] = True
    command_checks = []
    if previous_phases != int(state.command_phases):
        command_checks.append(evaluate_wallbox_command(
            {"kind": "set_phases", "target_phases": int(state.command_phases), "reason": payload["decision"]["reason"]},
            wb_id=1,
            reason=payload["decision"]["reason"],
            target_reachable=True,
            now_ts=now_s,
            status_path=guard_status_path,
            history_path=guard_history_path,
        ))
    if previous_amp != int(state.command_amp):
        kind = "set_current" if int(state.command_amp) > 0 else "stop"
        command_checks.append(evaluate_wallbox_command(
            {"kind": kind, "method": kind, "amp": int(state.command_amp), "reason": payload["decision"]["reason"]},
            wb_id=1,
            reason=payload["decision"]["reason"],
            target_reachable=True,
            now_ts=now_s,
            status_path=guard_status_path,
            history_path=guard_history_path,
        ))
    payload["command_guard"] = command_checks
    _write_json_atomic(output_path, payload)
    return payload


def _status_payload(
    *,
    cfg: Dict[str, Any],
    status: str,
    reason: str,
    master_url: str,
    targets: Optional[Dict[str, Any]] = None,
    errors: Optional[List[str]] = None,
    active_writers: Optional[List[str]] = None,
    storage_shadow: Optional[Dict[str, Any]] = None,
    wallbox_shadow: Optional[Dict[str, Any]] = None,
    snapshot_age_s: Optional[float] = None,
    snapshot_max_age_s: Optional[float] = None,
) -> Dict[str, Any]:
    now = int(time.time())
    payload = {
        "schema_version": 1,
        "service": "e3dc-shadow-sync",
        "mode": str(cfg.get("ha_mode", "off")).strip().lower(),
        "shadow_only": True,
        "status": status,
        "reason": reason,
        "ts": now,
        "master_url": master_url,
        "targets": targets or {},
        "errors": errors or [],
        "active_writers": active_writers or [],
        "storage_shadow": {
            "state": (storage_shadow or {}).get("parallel", {}).get("state") if isinstance((storage_shadow or {}).get("parallel"), dict) else None,
            "mode": (storage_shadow or {}).get("parallel", {}).get("mode") if isinstance((storage_shadow or {}).get("parallel"), dict) else None,
            "val": (storage_shadow or {}).get("parallel", {}).get("val") if isinstance((storage_shadow or {}).get("parallel"), dict) else None,
        },
        "wallbox_shadow": {
            "amp": (wallbox_shadow or {}).get("decision", {}).get("amp") if isinstance((wallbox_shadow or {}).get("decision"), dict) else None,
            "phases": (wallbox_shadow or {}).get("decision", {}).get("phases") if isinstance((wallbox_shadow or {}).get("decision"), dict) else None,
            "reason": (wallbox_shadow or {}).get("decision", {}).get("reason") if isinstance((wallbox_shadow or {}).get("decision"), dict) else None,
        },
    }
    if snapshot_age_s is not None:
        payload["snapshot_age_s"] = round(float(snapshot_age_s), 1)
    if snapshot_max_age_s is not None:
        payload["snapshot_max_age_s"] = round(float(snapshot_max_age_s), 1)
    return payload


def run_once(
    *,
    config_path: str = CONFIG_FILE,
    ramdisk_dir: str = RAMDISK,
    data_dir: str = DATA_DIR,
    service_checker: Optional[Callable[[str], bool]] = None,
) -> Dict[str, Any]:
    cfg = load_config(config_path)
    mode = str(cfg.get("ha_mode", "off")).strip().lower()
    master_url = _normalize_master_url(cfg)
    status_path = os.path.join(ramdisk_dir, "shadow_sync_status.json")
    history_path = os.path.join(ramdisk_dir, "shadow_sync_history.jsonl")
    if mode != "shadow":
        payload = _status_payload(cfg=cfg, status="DISABLED", reason="ha_mode_not_shadow", master_url=master_url)
        _write_json_atomic(status_path, payload)
        return payload
    if not _shadow_snapshot_token(cfg):
        payload = _status_payload(
            cfg=cfg,
            status="PAUSED",
            reason="snapshot_token_invalid",
            master_url=master_url,
        )
        _write_json_atomic(status_path, payload)
        _append_history(history_path, payload)
        return payload
    if not master_url:
        configured_master = bool(
            str(cfg.get("shadow_master_url") or "").strip()
            or str(cfg.get("shadow_master_ip") or cfg.get("ha_peer_ip") or "").strip()
        )
        payload = _status_payload(
            cfg=cfg,
            status="PAUSED",
            reason="invalid_master_url" if configured_master else "missing_master_url",
            master_url="",
        )
        _write_json_atomic(status_path, payload)
        _append_history(history_path, payload)
        return payload
    if master_points_to_self(master_url):
        payload = _status_payload(cfg=cfg, status="PAUSED", reason="master_points_to_self", master_url=master_url)
        _write_json_atomic(status_path, payload)
        _append_history(history_path, payload)
        return payload
    writers = active_writer_services(service_checker)
    if writers:
        payload = _status_payload(
            cfg=cfg,
            status="PAUSED",
            reason="active_writer_services",
            master_url=master_url,
            active_writers=writers,
        )
        _write_json_atomic(status_path, payload)
        _append_history(history_path, payload)
        return payload

    fetched, fetch_report = fetch_master_snapshot(cfg, ramdisk_dir=ramdisk_dir, data_dir=data_dir)
    errors = list(fetch_report.get("errors") or [])
    snapshot_max_age = max(5.0, min(3600.0, _safe_float(cfg.get("shadow_snapshot_max_age_s"), 30.0)))
    live_target_ready = _required_targets_ready(fetch_report, {"live_data"})
    if not live_target_ready:
        payload = _status_payload(
            cfg=cfg,
            status="PAUSED",
            reason="snapshot_missing_live",
            master_url=master_url,
            targets=fetch_report.get("targets", {}),
            errors=errors,
            snapshot_max_age_s=snapshot_max_age,
        )
        _write_json_atomic(status_path, payload)
        _append_history(history_path, payload)
        return payload

    timestamp_contract = _primary_live_timestamp_contract(
        fetched.get("live_data"),
        max_age_s=snapshot_max_age,
    )
    snapshot_age = timestamp_contract.get("age_s")
    if timestamp_contract.get("ok") is not True:
        timestamp_reason = str(timestamp_contract.get("reason") or "snapshot_timestamp_invalid")
        errors.append(timestamp_reason)
        payload = _status_payload(
            cfg=cfg,
            status="PAUSED",
            reason=timestamp_reason,
            master_url=master_url,
            targets=fetch_report.get("targets", {}),
            errors=errors,
            snapshot_age_s=float(snapshot_age) if snapshot_age is not None else None,
            snapshot_max_age_s=snapshot_max_age,
        )
        _write_json_atomic(status_path, payload)
        _append_history(history_path, payload)
        return payload

    storage_ready = _required_targets_ready(fetch_report, STORAGE_REQUIRED_TARGETS)
    wallbox_ready = _required_targets_ready(fetch_report, WALLBOX_REQUIRED_TARGETS)
    storage_payload: Dict[str, Any] = {}
    wallbox_payload: Dict[str, Any] = {}
    if storage_ready:
        try:
            storage_payload = run_storage_shadow(
                fetched,
                cfg,
                output_path=os.path.join(ramdisk_dir, "shadow_storage_parallel_state.json"),
            )
        except Exception as exc:
            storage_ready = False
            errors.append(f"storage_shadow: {exc}")
    else:
        errors.append("storage_shadow: required_resources_missing")
    if wallbox_ready:
        try:
            wallbox_payload = run_wallbox_shadow(
                fetched,
                cfg,
                output_path=os.path.join(ramdisk_dir, "wallbox_parallel_shadow_state.json"),
                guard_status_path=os.path.join(ramdisk_dir, "shadow_control_status.json"),
                guard_history_path=os.path.join(ramdisk_dir, "shadow_control_history.jsonl"),
            )
        except Exception as exc:
            wallbox_ready = False
            errors.append(f"wallbox_shadow: {exc}")
    else:
        errors.append("wallbox_shadow: required_resources_missing")

    both_ready = storage_ready and wallbox_ready
    either_ready = storage_ready or wallbox_ready
    status = "OK" if both_ready and not errors else ("WARN" if either_ready else "PAUSED")
    reason = (
        "snapshot_synced"
        if status == "OK"
        else "snapshot_partial"
        if either_ready
        else "snapshot_required_resources_missing"
    )
    payload = _status_payload(
        cfg=cfg,
        status=status,
        reason=reason,
        master_url=master_url,
        targets=fetch_report.get("targets", {}),
        errors=errors,
        storage_shadow=storage_payload,
        wallbox_shadow=wallbox_payload,
        snapshot_age_s=snapshot_age,
        snapshot_max_age_s=snapshot_max_age,
    )
    _write_json_atomic(status_path, payload)
    _append_history(history_path, payload)
    return payload


def _status_log_signature(payload: Dict[str, Any]) -> str:
    operational_state = {
        "status": str(payload.get("status") or ""),
        "reason": str(payload.get("reason") or ""),
        "errors": [str(item) for item in (payload.get("errors") or [])],
        "active_writers": sorted(str(item) for item in (payload.get("active_writers") or [])),
    }
    return json.dumps(operational_state, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _status_log_decision(
    payload: Dict[str, Any],
    *,
    last_signature: Optional[str],
    last_log_monotonic_s: Optional[float],
    now_monotonic_s: float,
) -> Tuple[bool, str, bool]:
    signature = _status_log_signature(payload)
    changed = last_signature is None or signature != last_signature
    heartbeat_due = (
        last_log_monotonic_s is None
        or now_monotonic_s - last_log_monotonic_s >= STATUS_LOG_HEARTBEAT_S
    )
    return changed or heartbeat_due, signature, changed


def run_loop() -> None:
    log.info("Starte E3DC Shadow Sync.")
    last_log_signature: Optional[str] = None
    last_log_monotonic_s: Optional[float] = None
    while not _stop:
        cfg = load_config()
        interval_s = max(2, min(300, _safe_int(cfg.get("shadow_sync_interval_s"), 5)))
        try:
            payload = run_once()
            now_monotonic_s = time.monotonic()
            should_log, signature, changed = _status_log_decision(
                payload,
                last_signature=last_log_signature,
                last_log_monotonic_s=last_log_monotonic_s,
                now_monotonic_s=now_monotonic_s,
            )
            if should_log:
                event = "Statuswechsel" if changed else "Heartbeat"
                logger = log.warning if payload.get("status") in ("WARN", "PAUSED") else log.info
                logger(
                    "Shadow Sync %s: %s (%s)",
                    event,
                    payload.get("status"),
                    payload.get("reason"),
                )
                last_log_signature = signature
                last_log_monotonic_s = now_monotonic_s
        except Exception as exc:
            error_payload = {
                "schema_version": 1,
                "service": "e3dc-shadow-sync",
                "shadow_only": True,
                "status": "ERROR",
                "reason": str(exc),
                "ts": int(time.time()),
            }
            try:
                _write_json_atomic(STATUS_FILE, error_payload)
                _append_history(HISTORY_FILE, error_payload)
            except Exception:
                pass
            now_monotonic_s = time.monotonic()
            should_log, signature, changed = _status_log_decision(
                error_payload,
                last_signature=last_log_signature,
                last_log_monotonic_s=last_log_monotonic_s,
                now_monotonic_s=now_monotonic_s,
            )
            if should_log:
                event = "Statuswechsel" if changed else "Heartbeat"
                log.exception("Shadow Sync %s: ERROR (%s)", event, exc)
                last_log_signature = signature
                last_log_monotonic_s = now_monotonic_s
        slept = 0
        while not _stop and slept < interval_s:
            time.sleep(1)
            slept += 1
    log.info("Shadow Sync beendet.")


if __name__ == "__main__":
    run_loop()
