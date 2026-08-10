#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Vorbereiteter Klimaanlagen-Regelstatus.

Dieser Dienst plant nur den späteren Toshiba-Regelpfad und schreibt Diagnose in
die Ramdisk. Er sendet bewusst keine Cloud-, IR-, Shelly- oder lokalen
Steuerbefehle. Echte Toshiba-Kommandos dürfen erst in einem Adapter ergänzt
werden, wenn Zugangsdaten, Geräte-IDs und Sicherheitsgrenzen bewusst gesetzt
sind.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import signal
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable


CONFIG_FILE = Path("/var/www/html/data/e3dc_v4.json")
CLIMATE_LOAD_FILE = Path("/var/www/html/ramdisk/climate_load.json")
RAMDISK_FILE = Path("/var/www/html/ramdisk/climate_control.json")
TOSHIBA_BASE_URL = "https://mobileapi.toshibahomeaccontrols.com"
TOSHIBA_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
TOSHIBA_FALLBACK_BACKOFF_S = (300, 600, 1200, 2400, 3600)
TOSHIBA_MAX_RETRY_AFTER_S = 7 * 24 * 3600

RUNNING = True

DEFAULT_CONFIG: dict[str, Any] = {
    "climate_control_enable": "0",
    "climate_control_provider": "toshiba_cloud",
    "climate_control_mode": "off",
    "climate_control_poll_s": "60",
    "climate_toshiba_cloud_enable": "0",
    "climate_toshiba_username": "",
    "climate_toshiba_password": "",
    "climate_toshiba_device_ids": "",
    "climate_day_temp_c": "24.0",
    "climate_night_temp_c": "26.0",
    "climate_night_start": "22:00",
    "climate_night_end": "06:00",
    "climate_night_eco_enable": "1",
    "climate_night_quiet_enable": "1",
    "climate_high_power_enable": "0",
}


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_int(value: Any, default: int = 0, min_value: int | None = None, max_value: int | None = None) -> int:
    try:
        if isinstance(value, str):
            value = value.strip().replace(",", ".")
        parsed = int(round(float(value)))
    except (TypeError, ValueError):
        parsed = default
    if min_value is not None:
        parsed = max(min_value, parsed)
    if max_value is not None:
        parsed = min(max_value, parsed)
    return parsed


def _truthy(value: Any) -> bool:
    return str(value).strip().lower() in {"1", "true", "yes", "on", "ja", "ein"}


def cfg_text(cfg: dict[str, Any], key: str, default: str = "") -> str:
    value = cfg.get(key, default)
    if isinstance(value, dict):
        for nested_key in ("value", "Value", "val"):
            if nested_key in value:
                value = value[nested_key]
                break
    if value is None:
        return default
    return str(value).strip()


def load_config(path: Path = CONFIG_FILE) -> dict[str, Any]:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                for key in DEFAULT_CONFIG:
                    if key in data:
                        cfg[key] = data[key]
    except Exception as exc:
        cfg["_config_error"] = str(exc)
    return cfg


def read_json(path: Path) -> dict[str, Any]:
    try:
        if path.exists():
            with path.open("r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, dict):
                return data
    except Exception:
        pass
    return {}


def normalize_provider(value: Any) -> str:
    raw = str(value or "toshiba_cloud").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "toshiba": "toshiba_cloud",
        "toshiba_ac": "toshiba_cloud",
        "cloud": "toshiba_cloud",
        "local": "local_only",
        "none": "local_only",
    }
    return aliases.get(raw, raw if raw in {"toshiba_cloud", "local_only"} else "toshiba_cloud")


def normalize_mode(value: Any) -> str:
    raw = str(value or "off").strip().lower().replace("-", "_").replace(" ", "_")
    aliases = {
        "0": "off",
        "aus": "off",
        "manual": "manual",
        "manuell": "manual",
        "1": "schedule",
        "auto": "schedule",
        "zeitplan": "schedule",
        "schedule": "schedule",
    }
    return aliases.get(raw, "off")


def parse_device_ids(value: Any) -> list[str]:
    if isinstance(value, list):
        raw_items = value
    else:
        raw_items = str(value or "").replace(";", ",").split(",")
    ids: list[str] = []
    for item in raw_items:
        text = str(item or "").strip()
        if text:
            ids.append(text)
    return ids


def _parse_hhmm(value: Any, default: str) -> tuple[int, int]:
    raw = str(value or default).strip()
    try:
        hour_s, minute_s = raw.split(":", 1)
        hour = max(0, min(23, int(hour_s)))
        minute = max(0, min(59, int(minute_s)))
        return hour, minute
    except (TypeError, ValueError):
        hour_s, minute_s = default.split(":", 1)
        return int(hour_s), int(minute_s)


def _minute_of_day(hour_minute: tuple[int, int]) -> int:
    return hour_minute[0] * 60 + hour_minute[1]


def _in_window(now_min: int, start_min: int, end_min: int) -> bool:
    if start_min == end_min:
        return False
    if start_min < end_min:
        return start_min <= now_min < end_min
    return now_min >= start_min or now_min < end_min


def current_schedule(cfg: dict[str, Any], now_ts: float | None = None) -> dict[str, Any]:
    now = datetime.fromtimestamp(float(now_ts if now_ts is not None else time.time()))
    start = _parse_hhmm(cfg.get("climate_night_start"), "22:00")
    end = _parse_hhmm(cfg.get("climate_night_end"), "06:00")
    start_min = _minute_of_day(start)
    end_min = _minute_of_day(end)
    now_min = now.hour * 60 + now.minute
    night_active = _in_window(now_min, start_min, end_min)
    profile = "night" if night_active else "day"
    target_temp_c = _safe_float(
        cfg.get("climate_night_temp_c" if night_active else "climate_day_temp_c"),
        26.0 if night_active else 24.0,
    )
    return {
        "profile": profile,
        "night_active": night_active,
        "target_temp_c": round(target_temp_c, 1),
        "night_start": "%02d:%02d" % start,
        "night_end": "%02d:%02d" % end,
        "eco": bool(night_active and _truthy(cfg.get("climate_night_eco_enable", "1"))),
        "quiet": bool(night_active and _truthy(cfg.get("climate_night_quiet_enable", "1"))),
        "high_power": bool((not night_active) and _truthy(cfg.get("climate_high_power_enable", "0"))),
    }


class ToshibaHttpError(RuntimeError):
    def __init__(self, status: int, path: str, retry_after_s: int | None = None):
        self.status = int(status)
        self.path = str(path)
        self.retry_after_s = retry_after_s
        super().__init__(f"HTTP {self.status} bei {self.path}")


def _parse_retry_after_seconds(value: Any, now_ts: float | None = None) -> int | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    if raw.isdigit():
        return min(TOSHIBA_MAX_RETRY_AFTER_S, max(1, int(raw)))
    try:
        retry_at = parsedate_to_datetime(raw)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        now = float(now_ts if now_ts is not None else time.time())
        return min(TOSHIBA_MAX_RETRY_AFTER_S, max(1, int(math.ceil(retry_at.timestamp() - now))))
    except (TypeError, ValueError, OverflowError):
        return None


@dataclass
class ToshibaCloudSession:
    access_token: str = field(default="", repr=False)
    token_type: str = field(default="", repr=False)
    consumer_id: str = field(default="", repr=False)
    account_signature: str = field(default="", repr=False)
    rate_limit_until_ts: float = 0.0
    rate_limit_started_ts: float = 0.0
    rate_limit_failures: int = 0
    rate_limit_backoff_s: int = 0
    rate_limit_http_status: int | None = None

    def clear_auth(self) -> None:
        self.access_token = ""
        self.token_type = ""
        self.consumer_id = ""
        self.account_signature = ""

    def has_auth(self, account_signature: str) -> bool:
        return bool(
            self.account_signature == account_signature
            and self.access_token
            and self.token_type
            and self.consumer_id
        )

    def set_auth(self, login: dict[str, Any], account_signature: str) -> None:
        access_token = str(login.get("access_token") or "")
        token_type = str(login.get("token_type") or "")
        consumer_id = str(login.get("consumerId") or "")
        if not access_token or not token_type or not consumer_id:
            raise RuntimeError("Toshiba Login ohne Token oder Consumer-ID")
        self.access_token = access_token
        self.token_type = token_type
        self.consumer_id = consumer_id
        self.account_signature = account_signature

    def in_rate_limit(self, now_ts: float) -> bool:
        return self.rate_limit_until_ts > float(now_ts)

    def mark_rate_limited(self, now_ts: float, retry_after_s: int | None) -> None:
        failure_index = min(self.rate_limit_failures, len(TOSHIBA_FALLBACK_BACKOFF_S) - 1)
        backoff_s = retry_after_s if retry_after_s is not None else TOSHIBA_FALLBACK_BACKOFF_S[failure_index]
        self.rate_limit_failures = min(self.rate_limit_failures + 1, len(TOSHIBA_FALLBACK_BACKOFF_S))
        self.rate_limit_started_ts = float(now_ts)
        self.rate_limit_backoff_s = max(1, min(TOSHIBA_MAX_RETRY_AFTER_S, int(backoff_s)))
        self.rate_limit_until_ts = self.rate_limit_started_ts + self.rate_limit_backoff_s
        self.rate_limit_http_status = 429

    def clear_rate_limit(self) -> None:
        self.rate_limit_until_ts = 0.0
        self.rate_limit_started_ts = 0.0
        self.rate_limit_failures = 0
        self.rate_limit_backoff_s = 0
        self.rate_limit_http_status = None

    def restore_rate_limit(self, previous_status: dict[str, Any], now_ts: float) -> None:
        if not isinstance(previous_status, dict):
            return
        http_status = _safe_int(
            previous_status.get("cloud_http_status"),
            0,
            min_value=0,
            max_value=999,
        )
        retry_at = _safe_float(previous_status.get("cloud_retry_at_ts"), 0.0)
        now = float(now_ts)
        if http_status != 429 or retry_at <= now:
            return
        failures = _safe_int(
            previous_status.get("cloud_rate_limit_failures"),
            0,
            min_value=0,
            max_value=len(TOSHIBA_FALLBACK_BACKOFF_S),
        )
        started_at = min(
            now,
            max(0.0, _safe_float(previous_status.get("cloud_rate_limit_started_ts"), 0.0)),
        )
        backoff_s = _safe_int(
            previous_status.get("cloud_backoff_s"),
            0,
            min_value=0,
            max_value=TOSHIBA_MAX_RETRY_AFTER_S,
        )
        self.rate_limit_failures = failures
        self.rate_limit_started_ts = started_at
        self.rate_limit_backoff_s = backoff_s
        self.rate_limit_http_status = 429
        self.rate_limit_until_ts = min(retry_at, now + TOSHIBA_MAX_RETRY_AFTER_S)

    def public_backoff_fields(self, now_ts: float) -> dict[str, Any]:
        limited = self.in_rate_limit(now_ts)
        retry_at = int(math.ceil(self.rate_limit_until_ts)) if limited else None
        retry_in_s = max(0, int(math.ceil(self.rate_limit_until_ts - float(now_ts)))) if limited else 0
        return {
            "rate_limited": limited,
            "http_status": self.rate_limit_http_status,
            "rate_limit_started_ts": int(self.rate_limit_started_ts) if self.rate_limit_started_ts > 0 else None,
            "retry_at_ts": retry_at,
            "retry_at_iso": datetime.fromtimestamp(retry_at).isoformat(timespec="seconds") if retry_at is not None else "",
            "retry_in_s": retry_in_s,
            "backoff_s": self.rate_limit_backoff_s,
            "rate_limit_failures": self.rate_limit_failures,
        }


def _toshiba_api_request(
    path: str,
    *,
    token_type: str | None = None,
    token: str | None = None,
    get: dict[str, str] | None = None,
    post: dict[str, Any] | None = None,
    timeout: float = 20.0,
) -> Any:
    url = TOSHIBA_BASE_URL + path
    if get:
        url += "?" + urllib.parse.urlencode(get)
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": TOSHIBA_USER_AGENT,
    }
    if token_type and token:
        headers["Authorization"] = f"{token_type} {token}"
    body = json.dumps(post).encode("utf-8") if post is not None else None
    request = urllib.request.Request(url, data=body, headers=headers, method="POST" if post is not None else "GET")
    context = ssl.create_default_context()
    try:
        with urllib.request.urlopen(request, timeout=timeout, context=context) as response:
            raw_payload = response.read()
    except urllib.error.HTTPError as exc:
        retry_after = exc.headers.get("Retry-After") if exc.headers is not None else None
        raise ToshibaHttpError(
            int(exc.code),
            path,
            _parse_retry_after_seconds(retry_after),
        ) from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Netzwerkfehler bei {path}: {exc.reason}") from exc

    try:
        decoded = json.loads(raw_payload.decode("utf-8"))
    except Exception as exc:
        raise RuntimeError(f"Ungültige JSON-Antwort bei {path}") from exc

    if not isinstance(decoded, dict):
        raise RuntimeError(f"Unerwartete Antwort bei {path}")
    if decoded.get("IsSuccess") is True:
        return decoded.get("ResObj")
    status = decoded.get("StatusCode", "unknown")
    message = decoded.get("Message", "unknown error")
    try:
        http_like_status = int(status)
    except (TypeError, ValueError):
        http_like_status = 0
    if http_like_status in {401, 429}:
        raise ToshibaHttpError(http_like_status, path)
    raise RuntimeError(f"Toshiba API-Fehler bei {path}: {status}: {message}")


def _signed_byte(value: int) -> int:
    return struct.unpack("b", bytes([value & 0xFF]))[0]


def _enum_value(raw: int, mapping: dict[int, str]) -> str:
    return mapping.get(raw, f"0x{raw:02x}")


def _temp_value(raw: int) -> int | None:
    signed = _signed_byte(raw)
    if signed in (-128, -1) or raw == 0x7F:
        return None
    if raw == 126:
        return -1
    return signed


def _decode_toshiba_state(hex_state: str) -> dict[str, Any]:
    if not isinstance(hex_state, str) or len(hex_state) < 38:
        return {"raw_len": len(hex_state) if isinstance(hex_state, str) else 0, "decode_error": "state too short"}
    extended = hex_state[:12] + "0" + hex_state[12] + "0" + hex_state[13:38]
    try:
        data = struct.unpack("BBbBBBBBBbbBBBBBBBBB", bytes.fromhex(extended))
    except Exception as exc:
        return {"raw_len": len(hex_state), "decode_error": str(exc)}
    return {
        "status": _enum_value(data[0], {0x30: "ON", 0x31: "OFF", 0x02: "NONE", 0xFF: "NONE"}),
        "mode": _enum_value(data[1], {0x41: "AUTO", 0x42: "COOL", 0x43: "HEAT", 0x44: "DRY", 0x45: "FAN", 0x00: "NONE", 0xFF: "NONE"}),
        "target_temp_c": _temp_value(data[2] & 0xFF),
        "fan": _enum_value(data[3], {0x41: "AUTO", 0x31: "QUIET", 0x32: "LOW", 0x33: "MEDIUM_LOW", 0x34: "MEDIUM", 0x35: "MEDIUM_HIGH", 0x36: "HIGH", 0x00: "NONE", 0xFF: "NONE"}),
        "swing": _enum_value(data[4], {0x31: "OFF", 0x41: "SWING_VERTICAL", 0x42: "SWING_HORIZONTAL", 0x43: "SWING_BOTH", 0x50: "FIXED_1", 0x51: "FIXED_2", 0x52: "FIXED_3", 0x53: "FIXED_4", 0x54: "FIXED_5", 0x00: "NONE", 0xFF: "NONE"}),
        "power_selection": _enum_value(data[5], {0x32: "POWER_50", 0x4B: "POWER_75", 0x64: "POWER_100", 0xFF: "NONE"}),
        "indoor_temp_c": _temp_value(data[9] & 0xFF),
        "outdoor_temp_c": _temp_value(data[10] & 0xFF),
        "raw_len": len(hex_state),
    }


def _toshiba_device_summary(device: dict[str, Any], state_obj: dict[str, Any] | None) -> dict[str, Any]:
    current_state = ""
    if state_obj and isinstance(state_obj.get("ACStateData"), str):
        current_state = state_obj["ACStateData"]
    elif isinstance(device.get("ACStateData"), str):
        current_state = device["ACStateData"]
    decoded = _decode_toshiba_state(current_state) if current_state else {}
    cdu = (state_obj or {}).get("Cdu")
    fcu = (state_obj or {}).get("Fcu")
    summary = {
        "name": str(device.get("Name", "") or ""),
        "ac_id_tail": str(device.get("Id", "") or "")[-6:],
        "unique_id_tail": str(device.get("DeviceUniqueId", "") or "")[-6:],
        "firmware": str(device.get("FirmwareVersion", "") or ""),
        "model_id": str(device.get("ACModelId", "") or ""),
        "cdu_model": cdu.get("model_name") if isinstance(cdu, dict) else None,
        "fcu_model": fcu.get("model_name") if isinstance(fcu, dict) else None,
        "state": decoded,
    }
    if state_obj and "state_error" in state_obj:
        summary["state_error"] = str(state_obj["state_error"])
    return summary


def read_toshiba_cloud_status(
    cfg: dict[str, Any],
    now_ts: float | None = None,
    *,
    session: ToshibaCloudSession | None = None,
) -> dict[str, Any]:
    started = time.time()
    now = float(now_ts if now_ts is not None else started)
    username = cfg_text(cfg, "climate_toshiba_username")
    password = cfg_text(cfg, "climate_toshiba_password")
    cloud_session = session if session is not None else ToshibaCloudSession()
    account_signature = hashlib.sha256(f"{username}\0{password}".encode("utf-8")).hexdigest()
    request_performed = False
    login_performed = False
    session_reused = False
    reauthenticated = False

    def response_duration() -> float:
        return round(max(0.0, time.time() - started), 3)

    try:
        if not username or not password:
            raise RuntimeError("Toshiba-Zugangsdaten fehlen")

        if cloud_session.in_rate_limit(now):
            backoff = cloud_session.public_backoff_fields(now)
            retry_at_iso = str(backoff.get("retry_at_iso") or "")
            attempt_ts = cloud_session.rate_limit_started_ts or now
            return {
                "success": False,
                "ts": int(attempt_ts),
                "ts_iso": datetime.fromtimestamp(attempt_ts).isoformat(timespec="seconds"),
                "duration_s": response_duration(),
                "error": f"Toshiba Cloud ratenbegrenzt bis {retry_at_iso}",
                "request_performed": False,
                "login_performed": False,
                "session_reused": False,
                "reauthenticated": False,
                **backoff,
            }

        if cloud_session.account_signature and cloud_session.account_signature != account_signature:
            cloud_session.clear_auth()

        def login() -> None:
            nonlocal request_performed, login_performed
            request_performed = True
            login_result = _toshiba_api_request(
                "/api/Consumer/Login",
                post={"Username": username, "Password": password},
            )
            if not isinstance(login_result, dict):
                raise RuntimeError("Toshiba Login ohne gültiges Objekt")
            cloud_session.set_auth(login_result, account_signature)
            login_performed = True

        session_reused = cloud_session.has_auth(account_signature)
        if not session_reused:
            cloud_session.clear_auth()
            login()

        reauth_used = False

        def authorized_request(path: str, get_factory: Callable[[ToshibaCloudSession], dict[str, str]]) -> Any:
            nonlocal request_performed, login_performed, reauthenticated, reauth_used

            def perform() -> Any:
                nonlocal request_performed
                request_performed = True
                return _toshiba_api_request(
                    path,
                    get=get_factory(cloud_session),
                    token_type=cloud_session.token_type,
                    token=cloud_session.access_token,
                )

            try:
                return perform()
            except ToshibaHttpError as exc:
                if exc.status != 401 or reauth_used:
                    raise
                reauth_used = True
                reauthenticated = True
                cloud_session.clear_auth()
                login()
                return perform()

        mapping = authorized_request(
            "/api/AC/GetConsumerACMapping",
            lambda current: {"consumerId": current.consumer_id},
        )
        devices: list[dict[str, Any]] = []
        for group in mapping if isinstance(mapping, list) else []:
            if not isinstance(group, dict):
                continue
            ac_list = group.get("ACList")
            for item in ac_list if isinstance(ac_list, list) else []:
                if not isinstance(item, dict):
                    continue
                state_obj: dict[str, Any] | None = None
                ac_id = str(item.get("Id") or "")
                if ac_id:
                    try:
                        state_candidate = authorized_request(
                            "/api/AC/GetCurrentACState",
                            lambda current, current_ac_id=ac_id: {
                                "consumerId": current.consumer_id,
                                "ACId": current_ac_id,
                            },
                        )
                        state_obj = state_candidate if isinstance(state_candidate, dict) else None
                    except ToshibaHttpError as exc:
                        if exc.status in {401, 429}:
                            raise
                        state_obj = {"state_error": str(exc)}
                    except Exception as exc:
                        state_obj = {"state_error": str(exc)}
                devices.append(_toshiba_device_summary(item, state_obj))

        cloud_session.clear_rate_limit()
        return {
            "success": True,
            "ts": int(now),
            "ts_iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "duration_s": response_duration(),
            "device_count": len(devices),
            "devices": devices,
            "request_performed": request_performed,
            "login_performed": login_performed,
            "session_reused": session_reused,
            "reauthenticated": reauthenticated,
            **cloud_session.public_backoff_fields(now),
        }
    except ToshibaHttpError as exc:
        if exc.status == 429:
            cloud_session.mark_rate_limited(now, exc.retry_after_s)
        elif exc.status == 401:
            cloud_session.clear_auth()
        backoff = cloud_session.public_backoff_fields(now)
        return {
            "success": False,
            "ts": int(now),
            "ts_iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "duration_s": response_duration(),
            "error": str(exc),
            "request_performed": request_performed,
            "login_performed": login_performed,
            "session_reused": session_reused,
            "reauthenticated": reauthenticated,
            **backoff,
            "http_status": exc.status,
        }
    except Exception as exc:
        return {
            "success": False,
            "ts": int(now),
            "ts_iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
            "duration_s": response_duration(),
            "error": str(exc),
            "request_performed": request_performed,
            "login_performed": login_performed,
            "session_reused": session_reused,
            "reauthenticated": reauthenticated,
            **cloud_session.public_backoff_fields(now),
        }


def _device_state_value(devices: list[dict[str, Any]], key: str) -> Any:
    for device in devices:
        state = device.get("state")
        if isinstance(state, dict) and state.get(key) not in (None, ""):
            return state[key]
    return None


def _clean_cloud_devices(devices: Any) -> list[dict[str, Any]]:
    if not isinstance(devices, list):
        return []
    clean: list[dict[str, Any]] = []
    allowed_device_keys = {"name", "ac_id_tail", "unique_id_tail", "firmware", "model_id", "cdu_model", "fcu_model", "state_error"}
    allowed_state_keys = {
        "status", "mode", "target_temp_c", "fan", "swing", "power_selection",
        "indoor_temp_c", "outdoor_temp_c", "raw_len", "decode_error",
    }
    for item in devices:
        if not isinstance(item, dict):
            continue
        device = {key: item.get(key) for key in allowed_device_keys if item.get(key) not in (None, "")}
        state = item.get("state")
        if isinstance(state, dict):
            device["state"] = {key: state.get(key) for key in allowed_state_keys if state.get(key) not in (None, "")}
        clean.append(device)
    return clean


def _normalize_selector(value: Any) -> str:
    return str(value or "").strip().casefold()


def _selector_matches_device(selector: str, device: dict[str, Any]) -> bool:
    normalized = _normalize_selector(selector)
    if not normalized:
        return False
    candidates = [
        _normalize_selector(device.get("name")),
        _normalize_selector(device.get("ac_id_tail")),
        _normalize_selector(device.get("unique_id_tail")),
    ]
    if normalized in candidates:
        return True
    for candidate in candidates[1:]:
        if candidate and len(candidate) >= 4 and normalized.endswith(candidate):
            return True
    return False


def _annotate_configured_devices(devices: list[dict[str, Any]], selectors: list[str]) -> tuple[int, list[str]]:
    matched: set[int] = set()
    for selector in selectors:
        for device in devices:
            if _selector_matches_device(selector, device):
                device["config_selector"] = selector
                device["configured"] = True
                matched.add(id(device))
                break
    for device in devices:
        if "configured" not in device:
            device["configured"] = False
    unmatched = [
        selector
        for selector in selectors
        if not any(_selector_matches_device(selector, device) for device in devices)
    ]
    return len(matched), unmatched


def build_control_status(
    cfg: dict[str, Any],
    climate_load: dict[str, Any] | None = None,
    now_ts: float | None = None,
    *,
    read_cloud: bool = False,
    cloud_reader: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    cloud_session: ToshibaCloudSession | None = None,
) -> dict[str, Any]:
    now = float(now_ts if now_ts is not None else time.time())
    climate_load = climate_load if isinstance(climate_load, dict) else {}
    provider = normalize_provider(cfg.get("climate_control_provider"))
    mode = normalize_mode(cfg.get("climate_control_mode"))
    device_ids = parse_device_ids(cfg.get("climate_toshiba_device_ids"))
    schedule = current_schedule(cfg, now)

    enabled = _truthy(cfg.get("climate_control_enable", "0"))
    cloud_enabled = _truthy(cfg.get("climate_toshiba_cloud_enable", "0"))
    has_account = bool(cfg_text(cfg, "climate_toshiba_username") and cfg_text(cfg, "climate_toshiba_password"))
    has_device = bool(device_ids)

    reason = "disabled"
    control_ready = False
    if enabled:
        if mode == "off":
            reason = "mode_off"
        elif provider == "local_only":
            reason = "local_adapter_not_available"
        elif not cloud_enabled:
            reason = "toshiba_cloud_disabled"
        elif not has_account:
            reason = "toshiba_cloud_config_incomplete"
        else:
            reason = "toshiba_adapter_not_implemented"

    power_w = _safe_float(climate_load.get("power_w", climate_load.get("climate_power_w")), 0.0)
    load_age_s = max(0.0, now - _safe_float(climate_load.get("ts"), now))

    devices: list[dict[str, Any]] = []
    cloud_connected = False
    cloud_error = ""
    cloud_duration_s: float | None = None
    cloud_read_ts: int | None = None
    cloud_read_iso = ""
    cloud_rate_limited = False
    cloud_http_status: int | None = None
    cloud_retry_at_ts: int | None = None
    cloud_retry_at_iso = ""
    cloud_retry_in_s = 0
    cloud_backoff_s = 0
    cloud_rate_limit_started_ts: int | None = None
    cloud_rate_limit_failures = 0
    cloud_request_performed = False
    cloud_login_performed = False
    cloud_session_reused = False
    cloud_reauthenticated = False
    should_read_cloud = bool(read_cloud and enabled and provider == "toshiba_cloud" and cloud_enabled and has_account)
    if should_read_cloud:
        try:
            cloud_status = cloud_reader(cfg) if cloud_reader is not None else read_toshiba_cloud_status(
                cfg,
                now_ts=now,
                session=cloud_session,
            )
        except Exception as exc:
            cloud_status = {"success": False, "error": str(exc), "ts": int(now), "duration_s": 0.0}
        cloud_connected = bool(cloud_status.get("success"))
        cloud_error = "" if cloud_connected else str(cloud_status.get("error", "Toshiba Cloud konnte nicht gelesen werden"))
        cloud_duration_s = _safe_float(cloud_status.get("duration_s"), 0.0)
        cloud_read_ts = _safe_int(cloud_status.get("ts"), int(now), min_value=0)
        cloud_read_iso = str(cloud_status.get("ts_iso") or datetime.fromtimestamp(cloud_read_ts).isoformat(timespec="seconds"))
        cloud_rate_limited = bool(cloud_status.get("rate_limited"))
        cloud_http_status = _safe_int(cloud_status.get("http_status"), 0, min_value=0, max_value=999) or None
        cloud_retry_at_ts = _safe_int(cloud_status.get("retry_at_ts"), 0, min_value=0) or None
        cloud_retry_at_iso = str(cloud_status.get("retry_at_iso") or "")
        cloud_retry_in_s = _safe_int(cloud_status.get("retry_in_s"), 0, min_value=0, max_value=TOSHIBA_MAX_RETRY_AFTER_S)
        cloud_backoff_s = _safe_int(cloud_status.get("backoff_s"), 0, min_value=0, max_value=TOSHIBA_MAX_RETRY_AFTER_S)
        cloud_rate_limit_started_ts = _safe_int(cloud_status.get("rate_limit_started_ts"), 0, min_value=0) or None
        cloud_rate_limit_failures = _safe_int(
            cloud_status.get("rate_limit_failures"),
            0,
            min_value=0,
            max_value=len(TOSHIBA_FALLBACK_BACKOFF_S),
        )
        cloud_request_performed = bool(cloud_status.get("request_performed"))
        cloud_login_performed = bool(cloud_status.get("login_performed"))
        cloud_session_reused = bool(cloud_status.get("session_reused"))
        cloud_reauthenticated = bool(cloud_status.get("reauthenticated"))
        devices = _clean_cloud_devices(cloud_status.get("devices"))
        if cloud_connected:
            reason = "mode_off" if mode == "off" else "toshiba_cloud_readonly"
        elif cloud_rate_limited:
            reason = "toshiba_cloud_rate_limited"
        else:
            reason = "toshiba_cloud_read_failed"
    configured_device_count, unmatched_device_ids = _annotate_configured_devices(devices, device_ids)

    room_temp_c = _device_state_value(devices, "indoor_temp_c")
    outside_temp_c = _device_state_value(devices, "outdoor_temp_c")
    target_temp_c = _device_state_value(devices, "target_temp_c")
    ac_status = _device_state_value(devices, "status")
    ac_mode = _device_state_value(devices, "mode")
    fan = _device_state_value(devices, "fan")
    primary_device_name = str(devices[0].get("name") or "") if devices else ""

    status = {
        "success": True,
        "enabled": enabled,
        "read_only": True,
        "prepared": True,
        "active": False,
        "commands_allowed": False,
        "control_ready": control_ready,
        "reason": reason,
        "provider": provider,
        "mode": mode,
        "adapter": "planned",
        "cloud_enabled": cloud_enabled,
        "cloud_connected": cloud_connected,
        "cloud_error": cloud_error,
        "cloud_duration_s": round(cloud_duration_s, 3) if cloud_duration_s is not None else None,
        "cloud_last_read_ts": cloud_read_ts,
        "cloud_last_read_iso": cloud_read_iso,
        "cloud_rate_limited": cloud_rate_limited,
        "cloud_http_status": cloud_http_status,
        "cloud_rate_limit_started_ts": cloud_rate_limit_started_ts,
        "cloud_retry_at_ts": cloud_retry_at_ts,
        "cloud_retry_at_iso": cloud_retry_at_iso,
        "cloud_retry_in_s": cloud_retry_in_s,
        "cloud_backoff_s": cloud_backoff_s,
        "cloud_rate_limit_failures": cloud_rate_limit_failures,
        "cloud_request_performed": cloud_request_performed,
        "cloud_login_performed": cloud_login_performed,
        "cloud_session_reused": cloud_session_reused,
        "cloud_reauthenticated": cloud_reauthenticated,
        "cloud_device_count": len(devices),
        "credentials_configured": has_account,
        "device_ids_configured": has_device,
        "configured_device_count": configured_device_count,
        "unmatched_device_ids": unmatched_device_ids,
        "device_count": len(devices) if devices else len(device_ids),
        "device_ids": device_ids,
        "devices": devices,
        "primary_device_name": primary_device_name,
        "room_temp_c": room_temp_c,
        "indoor_temp_c": room_temp_c,
        "outside_temp_c": outside_temp_c,
        "outdoor_temp_c": outside_temp_c,
        "target_temp_c": target_temp_c,
        "ac_status": ac_status,
        "ac_mode": ac_mode,
        "fan": fan,
        "schedule": schedule,
        "climate_power_w": round(max(0.0, power_w), 1),
        "climate_active": bool(climate_load.get("active", power_w > 50.0)),
        "climate_meter_online": bool(climate_load.get("online", False)),
        "climate_load_age_s": round(load_age_s, 1),
        "ts": int(now),
        "ts_iso": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "note": "Toshiba Cloud read-only; Kommandos bleiben gesperrt." if cloud_connected else "Vorbereitung ohne aktive Toshiba-Kommandos",
    }
    return status


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, path)


def _signal_handler(signum, frame):  # noqa: ARG001
    global RUNNING
    RUNNING = False


def _restored_cloud_session(output_path: Path, now_ts: float | None = None) -> ToshibaCloudSession:
    now = float(now_ts if now_ts is not None else time.time())
    session = ToshibaCloudSession()
    session.restore_rate_limit(read_json(output_path), now)
    return session


def run_once(
    config_path: Path,
    climate_path: Path,
    output_path: Path,
    *,
    cloud_session: ToshibaCloudSession | None = None,
) -> dict[str, Any]:
    cfg = load_config(config_path)
    session = cloud_session if cloud_session is not None else _restored_cloud_session(output_path)
    status = build_control_status(
        cfg,
        read_json(climate_path),
        read_cloud=True,
        cloud_session=session,
    )
    atomic_write_json(output_path, status)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Klimaanlagen-Regelstatus vorbereiten.")
    parser.add_argument("--config", default=str(CONFIG_FILE))
    parser.add_argument("--climate-load", default=str(CLIMATE_LOAD_FILE))
    parser.add_argument("--output", default=str(RAMDISK_FILE))
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args(argv)

    config_path = Path(args.config)
    climate_path = Path(args.climate_load)
    output_path = Path(args.output)

    signal.signal(signal.SIGTERM, _signal_handler)
    signal.signal(signal.SIGINT, _signal_handler)
    cloud_session = _restored_cloud_session(output_path)

    if args.once:
        run_once(config_path, climate_path, output_path, cloud_session=cloud_session)
        return 0

    while RUNNING:
        cfg = load_config(config_path)
        status = build_control_status(
            cfg,
            read_json(climate_path),
            read_cloud=True,
            cloud_session=cloud_session,
        )
        atomic_write_json(output_path, status)
        poll_s = _safe_int(cfg.get("climate_control_poll_s"), 60, min_value=15, max_value=900)
        deadline = time.time() + poll_s
        while RUNNING and time.time() < deadline:
            time.sleep(min(1.0, max(0.0, deadline - time.time())))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
