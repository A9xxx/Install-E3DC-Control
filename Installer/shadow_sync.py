#!/usr/bin/env python3
"""Read-only Shadow sync and simulator entry point.

The shadow instance reads snapshots from one active E3DC-Control instance,
writes only local shadow files and runs the already existing read-only storage
and wallbox simulators. It must never talk to RSCP, Modbus, MQTT, Shelly or a
real wallbox driver.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import signal
import socket
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
MAX_FETCH_BYTES = 3 * 1024 * 1024

SNAPSHOT_TARGETS: Tuple[Tuple[str, str, str, str], ...] = (
    ("live_json", "get_live_json.php", RAMDISK, "shadow_master_live_json.json"),
    ("live_data", "ramdisk/live_data_py.json", RAMDISK, "shadow_master_live_data_py.json"),
    ("storage_state", "ramdisk/storage_manager_state.json", RAMDISK, "shadow_master_storage_manager_state.json"),
    ("storage_plan", "ramdisk/storage_plan.json", RAMDISK, "shadow_master_storage_plan.json"),
    ("wb_budget", "ramdisk/wb_pv_budget.json", RAMDISK, "shadow_master_wb_pv_budget.json"),
    ("wb_budget_diagnostics", "ramdisk/wb_pv_budget_diagnostics.json", RAMDISK, "shadow_master_wb_pv_budget_diagnostics.json"),
    ("wb_intent", "ramdisk/wallbox_storage_intent.json", RAMDISK, "shadow_master_wallbox_storage_intent.json"),
    ("wallbox_native", "ramdisk/wallbox_native.json", RAMDISK, "shadow_master_wallbox_native.json"),
)

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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - ShadowSync - %(levelname)s - %(message)s",
    datefmt="%d.%m %H:%M:%S",
)
log = logging.getLogger("ShadowSync")
_stop = False


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


def _append_history(path: str, record: Dict[str, Any], max_lines: int = MAX_HISTORY_LINES) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    lines: List[str] = []
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                lines = handle.readlines()[-max(1, max_lines - 1):]
    except Exception:
        lines = []
    lines.append(json.dumps(record, ensure_ascii=False, separators=(",", ":"), sort_keys=True) + "\n")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        handle.writelines(lines[-max_lines:])
    os.replace(tmp, path)
    try:
        os.chmod(path, 0o664)
    except Exception:
        pass


def load_config(config_path: str = CONFIG_FILE) -> Dict[str, Any]:
    return _read_json(config_path)


def _normalize_master_url(cfg: Dict[str, Any]) -> str:
    raw = str(cfg.get("shadow_master_url") or "").strip()
    if not raw:
        peer = str(cfg.get("shadow_master_ip") or cfg.get("ha_peer_ip") or "").strip()
        raw = peer
    if not raw:
        return ""
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlparse(raw)
    if not parsed.hostname:
        return ""
    scheme = parsed.scheme or "http"
    netloc = parsed.netloc or parsed.path
    return urllib.parse.urlunparse((scheme, netloc, "", "", "", "")).rstrip("/")


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


def _fetch_json(base_url: str, rel_path: str, timeout_s: float) -> Tuple[Any, Dict[str, Any]]:
    url = urllib.parse.urljoin(base_url.rstrip("/") + "/", rel_path.lstrip("/"))
    request = urllib.request.Request(url, headers={"User-Agent": "E3DC-Control-ShadowSync/1"})
    with urllib.request.urlopen(request, timeout=timeout_s) as response:
        raw = response.read(MAX_FETCH_BYTES + 1)
    if len(raw) > MAX_FETCH_BYTES:
        raise ValueError(f"Antwort zu gross: {rel_path}")
    data = json.loads(raw.decode("utf-8-sig"))
    meta = {
        "url": url,
        "bytes": len(raw),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }
    return data, meta


def fetch_master_snapshot(
    cfg: Dict[str, Any],
    *,
    ramdisk_dir: str = RAMDISK,
    data_dir: str = DATA_DIR,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    master_url = _normalize_master_url(cfg)
    timeout_s = max(0.5, min(15.0, _safe_float(cfg.get("shadow_fetch_timeout_s"), 2.5)))
    fetched: Dict[str, Any] = {}
    targets: Dict[str, Any] = {}
    errors: List[str] = []
    for key, rel_path, target_dir, filename in SNAPSHOT_TARGETS:
        out_dir = data_dir if target_dir == DATA_DIR else ramdisk_dir
        out_path = os.path.join(out_dir, filename)
        try:
            data, meta = _fetch_json(master_url, rel_path, timeout_s)
            if not isinstance(data, dict):
                raise ValueError(f"{rel_path} liefert kein JSON-Objekt")
            _write_json_atomic(out_path, data)
            fetched[key] = data
            targets[key] = {
                "ok": True,
                "path": out_path,
                "source": rel_path,
                "fetched_ts": int(time.time()),
                "bytes": meta.get("bytes"),
                "sha256": meta.get("sha256"),
            }
        except Exception as exc:
            errors.append(f"{key}: {exc}")
            targets[key] = {
                "ok": False,
                "path": out_path,
                "source": rel_path,
                "error": str(exc),
            }
    return fetched, {"targets": targets, "errors": errors}


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
    # Die Shadow-Instanz verwendet ausschließlich ihre lokale Konfiguration.
    # Die vollständige Konfiguration der aktiven Anlage kann Geheimnisse
    # enthalten und wird deshalb weder abgerufen noch übertragen.
    sim_cfg = dict(cfg or {})
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
    for key in ("ts", "timestamp", "timestamp_s", "last_update_ts", "last_update", "time"):
        if key not in data:
            continue
        value = _safe_float(data.get(key), 0.0)
        if value <= 0:
            continue
        if value > 10_000_000_000:
            value = value / 1000.0
        if value > 946_684_800:
            return value
    return None


def _snapshot_age_s(fetched: Dict[str, Any]) -> Optional[float]:
    for key in ("live_data", "live_json"):
        data = fetched.get(key)
        if not isinstance(data, dict):
            continue
        payload_ts = _payload_timestamp_s(data)
        if payload_ts is not None:
            return max(0.0, time.time() - payload_ts)
    return None


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
    # Vergleichsparameter stammen aus der lokalen Shadow-Konfiguration. Damit
    # verlassen Zugangsdaten der aktiven Anlage niemals deren System.
    sim_cfg = dict(cfg or {})
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
    if not master_url:
        payload = _status_payload(cfg=cfg, status="PAUSED", reason="missing_master_url", master_url="")
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
    snapshot_age = _snapshot_age_s(fetched)
    snapshot_max_age = max(5.0, min(3600.0, _safe_float(cfg.get("shadow_snapshot_max_age_s"), 30.0)))
    required_ok = bool(
        fetch_report.get("targets", {}).get("live_data", {}).get("ok")
        or fetch_report.get("targets", {}).get("live_json", {}).get("ok")
    )
    if required_ok and snapshot_age is not None and snapshot_age > snapshot_max_age:
        errors.append(f"snapshot_stale: {snapshot_age:.1f}s > {snapshot_max_age:.1f}s")
        payload = _status_payload(
            cfg=cfg,
            status="PAUSED",
            reason="snapshot_stale",
            master_url=master_url,
            targets=fetch_report.get("targets", {}),
            errors=errors,
            snapshot_age_s=snapshot_age,
            snapshot_max_age_s=snapshot_max_age,
        )
        _write_json_atomic(status_path, payload)
        _append_history(history_path, payload)
        return payload

    storage_payload: Dict[str, Any] = {}
    wallbox_payload: Dict[str, Any] = {}
    try:
        storage_payload = run_storage_shadow(
            fetched,
            cfg,
            output_path=os.path.join(ramdisk_dir, "shadow_storage_parallel_state.json"),
        )
    except Exception as exc:
        errors.append(f"storage_shadow: {exc}")
    try:
        wallbox_payload = run_wallbox_shadow(
            fetched,
            cfg,
            output_path=os.path.join(ramdisk_dir, "wallbox_parallel_shadow_state.json"),
            guard_status_path=os.path.join(ramdisk_dir, "shadow_control_status.json"),
            guard_history_path=os.path.join(ramdisk_dir, "shadow_control_history.jsonl"),
        )
    except Exception as exc:
        errors.append(f"wallbox_shadow: {exc}")

    status = "OK" if required_ok and not errors else ("WARN" if required_ok else "PAUSED")
    reason = "snapshot_synced" if status == "OK" else "snapshot_partial" if required_ok else "snapshot_missing_live"
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


def run_loop() -> None:
    log.info("Starte E3DC Shadow Sync.")
    while not _stop:
        cfg = load_config()
        interval_s = max(2, min(300, _safe_int(cfg.get("shadow_sync_interval_s"), 5)))
        try:
            payload = run_once()
            log.info("Shadow Sync: %s (%s)", payload.get("status"), payload.get("reason"))
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
            log.exception("Shadow Sync Fehler: %s", exc)
        slept = 0
        while not _stop and slept < interval_s:
            time.sleep(1)
            slept += 1
    log.info("Shadow Sync beendet.")


if __name__ == "__main__":
    run_loop()
