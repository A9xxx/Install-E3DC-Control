#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RSCP-Verbindung und Befehlsvertrag für den Storage Manager."""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, Optional, Tuple

try:
    from Installer.rscp_client import RscpConnection, RscpTag, RscpType, find_tag
except ModuleNotFoundError:
    from rscp_client import RscpConnection, RscpTag, RscpType, find_tag  # type: ignore

try:
    from Installer.storage_parallel_regulator import (
        MODE_AUTO,
        MODE_CHRG,
        MODE_DISCH,
        MODE_GRID,
        MODE_IDLE,
    )
except ModuleNotFoundError:
    from storage_parallel_regulator import (  # type: ignore
        MODE_AUTO,
        MODE_CHRG,
        MODE_DISCH,
        MODE_GRID,
        MODE_IDLE,
    )

try:
    from Installer.Storage.common import mode_label, safe_int
except ModuleNotFoundError:
    from Storage.common import mode_label, safe_int  # type: ignore


ACTIVE_REFRESH_MODES = {MODE_DISCH, MODE_CHRG, MODE_GRID}
ACTIVE_RELEASE_MODES = {MODE_IDLE, MODE_DISCH, MODE_CHRG, MODE_GRID}
RSCP_COMMAND_CONTRACT_VERSION = 2
RSCP_POWER_SETTINGS_CONTRACT_VERSION = 1
RSCP_POWER_SETTINGS_RETRY_S = 10.0
RSCP_POWER_SETTINGS_TOLERANCE_W = 50

log = logging.getLogger("StorageManager")


def rscp_settings_from_cfg(cfg: Dict[str, Any]) -> Tuple[str, int, str, str, str]:
    host = str(
        cfg.get("server_ip", cfg.get("rscpip", cfg.get("e3dc_ip", "")))
        or ""
    ).strip()
    port = safe_int(cfg.get("server_port", cfg.get("rscpport", cfg.get("e3dc_port"))), 5033)
    user = str(cfg.get("e3dc_user", cfg.get("username", "")) or "").strip()
    pw = str(cfg.get("e3dc_password", cfg.get("password", "")) or "").strip()
    aes = cfg.get("aes_password", cfg.get("rscp_password", cfg.get("rscp_pw", "")))
    rscp_pw = str(aes or pw or "").strip()
    return host, port, user, pw, rscp_pw


class BattCtrl:
    def __init__(self, host: str, port: int, user: str, pw: str, rscp_pw: str):
        self.host = host
        self.port = int(port)
        self.user = user
        self.pw = pw
        self.rscp_pw = rscp_pw
        self._c = None
        self._mode = -1
        self._val = -1
        self._charge_cap = -1
        self._discharge_cap = -1
        self._auto_discharge_cap = 12000
        self._settings_charge_cap = -1
        self._settings_discharge_cap = -1
        self._settings_discharge_start = -1
        self._settings_limits_used: Optional[bool] = None
        self._settings_retry_after_monotonic = 0.0
        self._settings_set_requests = 0
        self._settings_get_requests = 0
        self._settings_suppressed = 0
        self._power_settings_diag: Dict[str, Any] = {
            "schema": "rscp_power_settings_v1",
            "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
            "status": "not_requested",
            "confirmed": False,
        }

    def set_auto_discharge_cap(self, val: int) -> None:
        val = max(300, int(val or 0))
        self._auto_discharge_cap = val

    def _conn(self) -> bool:
        try:
            self._c = RscpConnection(self.host, self.port, self.rscp_pw)
            self._c.connect()
            self._c.authenticate(self.user, self.pw)
            log.info("RSCP: %s:%d", self.host, self.port)
            return True
        except Exception as exc:
            log.error("RSCP conn: %s", exc)
            self._c = None
            return False

    def _clear_confirmed_power_settings(self) -> None:
        self._settings_charge_cap = -1
        self._settings_discharge_cap = -1
        self._settings_discharge_start = -1
        self._settings_limits_used = None

    def _power_settings_target_matches(
        self,
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
    ) -> bool:
        if self._settings_limits_used is False and limits_used is False:
            return True
        return bool(
            self._settings_limits_used is limits_used
            and abs(charge_w - self._settings_charge_cap) < RSCP_POWER_SETTINGS_TOLERANCE_W
            and abs(discharge_w - self._settings_discharge_cap) < RSCP_POWER_SETTINGS_TOLERANCE_W
            and abs(discharge_start_w - self._settings_discharge_start) < RSCP_POWER_SETTINGS_TOLERANCE_W
        )

    @staticmethod
    def _power_settings_values(
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
    ) -> list:
        return [
            {"tag": RscpTag.EMS_POWER_LIMITS_USED, "type": RscpType.Bool, "value": limits_used},
            {"tag": RscpTag.EMS_MAX_DISCHARGE_POWER, "type": RscpType.Uint32, "value": discharge_w},
            {"tag": RscpTag.EMS_MAX_CHARGE_POWER, "type": RscpType.Uint32, "value": charge_w},
            {"tag": RscpTag.EMS_DISCHARGE_START_POWER, "type": RscpType.Uint32, "value": discharge_start_w},
        ]

    @staticmethod
    def _power_settings_response_codes(response: Any) -> Optional[list]:
        container = find_tag(response, RscpTag.EMS_SET_POWER_SETTINGS)
        if not isinstance(container, dict) or container.get("type") == RscpType.Error:
            return None
        values = container.get("value")
        if not isinstance(values, list) or not values:
            return None
        codes = []
        for item in values:
            if not isinstance(item, dict) or item.get("type") == RscpType.Error:
                return None
            try:
                code = int(item.get("value"))
            except (TypeError, ValueError):
                return None
            if code not in (0, 1):
                return None
            codes.append(code)
        return codes

    def _read_power_settings(self) -> Optional[Dict[str, Any]]:
        self._settings_get_requests += 1
        response = self._c.request([{
            "tag": RscpTag.EMS_REQ_GET_POWER_SETTINGS,
            "type": RscpType.Nil,
            "value": None,
        }])
        container = find_tag(response, RscpTag.EMS_GET_POWER_SETTINGS)
        if not isinstance(container, dict) or container.get("type") == RscpType.Error:
            return None
        values = container.get("value")
        if not isinstance(values, list):
            return None

        def value_for(tag: int) -> Any:
            item = find_tag(values, tag)
            return item.get("value") if isinstance(item, dict) else None

        raw_limits_used = value_for(RscpTag.EMS_POWER_LIMITS_USED)
        raw_charge_w = value_for(RscpTag.EMS_MAX_CHARGE_POWER)
        raw_discharge_w = value_for(RscpTag.EMS_MAX_DISCHARGE_POWER)
        raw_discharge_start_w = value_for(RscpTag.EMS_DISCHARGE_START_POWER)
        if any(value is None for value in (
            raw_limits_used,
            raw_charge_w,
            raw_discharge_w,
            raw_discharge_start_w,
        )):
            return None
        try:
            return {
                "limits_used": bool(raw_limits_used),
                "max_charge_w": max(0, int(raw_charge_w)),
                "max_discharge_w": max(0, int(raw_discharge_w)),
                "discharge_start_w": max(0, int(raw_discharge_start_w)),
            }
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _power_settings_readback_matches(
        readback: Optional[Dict[str, Any]],
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
    ) -> bool:
        if not isinstance(readback, dict) or readback.get("limits_used") is not limits_used:
            return False
        if not limits_used:
            return True
        return bool(
            abs(int(readback.get("max_charge_w", -1)) - charge_w) < RSCP_POWER_SETTINGS_TOLERANCE_W
            and abs(int(readback.get("max_discharge_w", -1)) - discharge_w) < RSCP_POWER_SETTINGS_TOLERANCE_W
            and abs(int(readback.get("discharge_start_w", -1)) - discharge_start_w) < RSCP_POWER_SETTINGS_TOLERANCE_W
        )

    def _write_and_verify_power_settings(
        self,
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
        *,
        stage: str,
    ) -> bool:
        requested = {
            "limits_used": limits_used,
            "max_charge_w": charge_w,
            "max_discharge_w": discharge_w,
            "discharge_start_w": discharge_start_w,
        }
        self._settings_set_requests += 1
        response = self._c.request([{
            "tag": RscpTag.EMS_REQ_SET_POWER_SETTINGS,
            "type": RscpType.Container,
            "value": self._power_settings_values(
                charge_w,
                discharge_w,
                discharge_start_w,
                limits_used,
            ),
        }])
        response_codes = self._power_settings_response_codes(response)
        if response_codes is None or len(response_codes) < 4:
            self._power_settings_diag = {
                "schema": "rscp_power_settings_v1",
                "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
                "status": "set_response_invalid",
                "stage": stage,
                "confirmed": False,
                "requested": requested,
                "response_codes": response_codes,
                "ts": int(time.time()),
            }
            return False

        readback = None
        readback_matches = False
        for attempt in range(2):
            readback = self._read_power_settings()
            readback_matches = self._power_settings_readback_matches(
                readback,
                charge_w,
                discharge_w,
                discharge_start_w,
                limits_used,
            )
            if readback_matches:
                break
            if attempt == 0:
                time.sleep(0.05)
        if not readback_matches:
            self._power_settings_diag = {
                "schema": "rscp_power_settings_v1",
                "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
                "status": "readback_mismatch" if readback is not None else "readback_missing",
                "stage": stage,
                "confirmed": False,
                "requested": requested,
                "response_codes": response_codes,
                "readback": readback,
                "ts": int(time.time()),
            }
            return False

        self._settings_charge_cap = charge_w
        self._settings_discharge_cap = discharge_w
        self._settings_discharge_start = discharge_start_w
        self._settings_limits_used = limits_used
        self._charge_cap = charge_w
        self._discharge_cap = discharge_w
        self._settings_retry_after_monotonic = 0.0
        self._power_settings_diag = {
            "schema": "rscp_power_settings_v1",
            "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
            "status": "confirmed_nonoptimal" if any(code == 1 for code in response_codes) else "confirmed",
            "stage": stage,
            "confirmed": True,
            "requested": requested,
            "response_codes": response_codes,
            "readback": readback,
            "ts": int(time.time()),
        }
        return True

    def power_settings_diagnostics(self) -> Dict[str, Any]:
        diag = dict(self._power_settings_diag)
        diag["set_requests"] = self._settings_set_requests
        diag["get_requests"] = self._settings_get_requests
        diag["suppressed_unchanged"] = self._settings_suppressed
        diag["retry_remaining_s"] = round(
            max(0.0, self._settings_retry_after_monotonic - time.monotonic()),
            1,
        )
        return diag

    def set_power_limit_settings(
        self,
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int = 0,
        limits_used: bool = True,
        force: bool = False,
    ) -> bool:
        charge_w = max(0, int(charge_w or 0))
        discharge_w = max(0, int(discharge_w or 0))
        discharge_start_w = max(0, int(discharge_start_w or 0))
        limits_used = bool(limits_used)
        if not force and self._power_settings_target_matches(
            charge_w,
            discharge_w,
            discharge_start_w,
            limits_used,
        ):
            self._settings_suppressed += 1
            self._power_settings_diag = {
                **self._power_settings_diag,
                "status": "confirmed_unchanged",
                "confirmed": True,
                "ts": int(time.time()),
            }
            return True
        if time.monotonic() < self._settings_retry_after_monotonic:
            self._power_settings_diag = {
                **self._power_settings_diag,
                "status": "retry_backoff",
                "confirmed": False,
                "ts": int(time.time()),
            }
            return False
        if not self._c and not self._conn():
            self._power_settings_diag = {
                "schema": "rscp_power_settings_v1",
                "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
                "status": "connection_failed",
                "confirmed": False,
                "ts": int(time.time()),
            }
            return False
        try:
            if (
                limits_used
                and charge_w > 0
                and self._settings_limits_used is True
                and 0 <= self._settings_charge_cap < 50
            ):
                if not self._write_and_verify_power_settings(
                    charge_w,
                    discharge_w,
                    discharge_start_w,
                    False,
                    stage="rearm_release",
                ):
                    raise RuntimeError("POWER_SETTINGS-Rearm konnte nicht bestaetigt werden")
                log.info("RSCP POWER_SETTINGS REARM: Ladegrenze 0W -> %dW", charge_w)
            if not self._write_and_verify_power_settings(
                charge_w,
                discharge_w,
                discharge_start_w,
                limits_used,
                stage="target",
            ):
                raise RuntimeError("POWER_SETTINGS-Readback stimmt nicht mit der Vorgabe ueberein")
            log.info(
                "RSCP POWER_SETTINGS: limits=%s max_charge=%dW max_discharge=%dW discharge_start=%dW",
                "on" if limits_used else "off",
                charge_w,
                discharge_w,
                discharge_start_w,
            )
            return True
        except Exception as exc:
            log.error("RSCP power_settings: %s", exc)
            self._clear_confirmed_power_settings()
            self._settings_retry_after_monotonic = time.monotonic() + RSCP_POWER_SETTINGS_RETRY_S
            self.close()
            return False

    def set_max_charge_power(self, val: int, force: bool = False) -> None:
        val = int(val)
        if not self._c and not self._conn():
            return
        if not force and abs(val - self._charge_cap) < 50:
            return
        actual = max(50, val) if 0 < val < 200 else val
        try:
            self._c.request([{"tag": RscpTag.EMS_REQ_SET_MAX_CHARGE_POWER, "type": RscpType.Int32, "value": actual}])
            self._charge_cap = val
            log.info("RSCP MAX_CHARGE_POWER: %dW (Echt: %dW)", val, actual)
        except Exception as exc:
            log.error("RSCP set_max_charge: %s", exc)
            self._c = None

    def set_max_discharge_power(self, val: int, force: bool = False) -> None:
        val = int(val)
        if not self._c and not self._conn():
            return
        if not force and abs(val - self._discharge_cap) < 50:
            return
        try:
            self._c.request([{"tag": RscpTag.EMS_REQ_SET_MAX_DISCHARGE_POWER, "type": RscpType.Int32, "value": val}])
            self._discharge_cap = val
            log.info("RSCP MAX_DISCHARGE_POWER: %dW", val)
        except Exception as exc:
            log.error("RSCP set_max_discharge: %s", exc)
            self._c = None

    def set_power_auto(self, value_w: int = 0, force: bool = False, reason: str = "") -> bool:
        value_w = max(0, int(value_w or 0))
        if not force and self._mode == MODE_AUTO and abs(value_w - self._val) < 50:
            return True
        if not self._c and not self._conn():
            return False
        try:
            self._c.request([{"tag": RscpTag.EMS_REQ_SET_POWER, "type": RscpType.Container, "value": [
                {"tag": RscpTag.EMS_REQ_SET_POWER_MODE, "type": RscpType.UChar8, "value": MODE_AUTO},
                {"tag": RscpTag.EMS_REQ_SET_POWER_VALUE, "type": RscpType.Int32, "value": value_w},
            ]}])
            self._mode = MODE_AUTO
            self._val = value_w
            suffix = f" ({reason})" if reason else ""
            log.info("RSCP SET: AUTO(%d) %dW%s", MODE_AUTO, value_w, suffix)
            return True
        except Exception as exc:
            log.error("RSCP release auto: %s", exc)
            self._c = None
            return False

    def send(
        self,
        mode: int,
        val: int,
        force: bool = False,
        discharge_cap_w: Optional[int] = None,
        auto_limit: Optional[Dict[str, Any]] = None,
    ) -> None:
        mode = int(mode)
        val = max(0, int(val))
        def _auto_limit_int(key: str, default: int) -> int:
            if not isinstance(auto_limit, dict) or key not in auto_limit:
                return int(default)
            return safe_int(auto_limit.get(key), default)

        command_contract = rscp_command_contract(
            mode,
            val,
            current_mode=self._mode,
            auto_limit=auto_limit,
            discharge_cap_w=discharge_cap_w,
            auto_discharge_cap_w=self._auto_discharge_cap,
        )

        if command_contract.get("auto_limit_release"):
            auto_discharge_cap = max(
                300,
                _auto_limit_int("max_discharge_w", int(discharge_cap_w or self._auto_discharge_cap or val or 0)),
            )
            auto_charge_cap = max(
                300,
                _auto_limit_int("max_charge_w", int(auto_discharge_cap or val or 0)),
                auto_discharge_cap,
            )
            if not self.set_power_limit_settings(
                auto_charge_cap,
                auto_discharge_cap,
                _auto_limit_int("discharge_start_w", 0),
                limits_used=False,
                force=False,
            ):
                return
            # AUTO-Freilauf entsteht bei E3DC durch das Ende aktiver
            # SET_POWER-Refreshes. AUTO mit 0 W wäre dagegen ein echter
            # Nullleistungsbefehl und würde Laden sowie Entladen sperren.
            self._mode = MODE_AUTO
            self._val = auto_discharge_cap
            return
        elif command_contract.get("auto_limit_enabled"):
            auto_discharge_cap = max(0, _auto_limit_int(
                "max_discharge_w",
                int(discharge_cap_w or self._auto_discharge_cap or 0),
            ))
            auto_charge_cap = max(0, _auto_limit_int("max_charge_w", 0))
            if not self.set_power_limit_settings(
                auto_charge_cap,
                auto_discharge_cap,
                _auto_limit_int("discharge_start_w", 0),
                limits_used=True,
                force=False,
            ):
                return
            should_set_power_auto = bool(command_contract.get("set_power_auto"))
            if should_set_power_auto:
                if not self.set_power_auto(0, force=True, reason="aktive Vorgabe vor EMS-Grenze gestoppt"):
                    return
            self._mode = MODE_AUTO
            self._val = max(300, int(discharge_cap_w or self._auto_discharge_cap or val or auto_charge_cap or 0))
            return
        elif mode == MODE_AUTO:
            auto_discharge_cap = max(
                300,
                int(discharge_cap_w or self._auto_discharge_cap or val or 0),
            )
            auto_charge_cap = max(300, int(val or 0), auto_discharge_cap)
            if not self.set_power_limit_settings(
                auto_charge_cap,
                auto_discharge_cap,
                0,
                limits_used=False,
                force=False,
            ):
                return
            self._mode = MODE_AUTO
            self._val = auto_discharge_cap
            return
        elif mode == MODE_DISCH:
            self.set_max_charge_power(0)
        elif mode in (MODE_CHRG, MODE_GRID):
            self.set_max_charge_power(val)
        if not self._c and not self._conn():
            return
        same_command = mode == self._mode and abs(val - self._val) < 50
        if not force and same_command:
            return
        try:
            self._c.request([{"tag": RscpTag.EMS_REQ_SET_POWER, "type": RscpType.Container, "value": [
                {"tag": RscpTag.EMS_REQ_SET_POWER_MODE, "type": RscpType.UChar8, "value": mode},
                {"tag": RscpTag.EMS_REQ_SET_POWER_VALUE, "type": RscpType.Int32, "value": val},
            ]}])
            self._mode = mode
            self._val = val
            if same_command and force:
                log.debug("RSCP REFRESH: %s(%d) %dW", mode_label(mode), mode, val)
            else:
                log.info("RSCP SET: %s(%d) %dW", mode_label(mode), mode, val)
        except Exception as exc:
            log.error("RSCP send: %s", exc)
            self._c = None

    def release(self) -> None:
        self.send(
            MODE_AUTO,
            self._auto_discharge_cap,
            force=True,
            discharge_cap_w=self._auto_discharge_cap,
            auto_limit={
                "release": True,
                "set_power_auto": False,
                "set_power_value": 0,
                "max_charge_w": self._auto_discharge_cap,
                "max_discharge_w": self._auto_discharge_cap,
                "discharge_start_w": 0,
            },
        )

    def close(self) -> None:
        if self._c:
            try:
                self._c.close()
            except Exception:
                pass
            self._c = None


def rscp_command_contract(
    mode: int,
    val: int,
    *,
    current_mode: int,
    auto_limit: Optional[Dict[str, Any]] = None,
    discharge_cap_w: Optional[int] = None,
    auto_discharge_cap_w: int = 0,
) -> Dict[str, Any]:
    """Describe the RSCP command path without opening a connection or sending."""
    mode_i = safe_int(mode, MODE_AUTO)
    val_i = max(0, safe_int(val, 0))
    current_mode_i = safe_int(current_mode, -1)
    auto = auto_limit if isinstance(auto_limit, dict) else {}
    auto_release = bool(auto.get("release"))
    limit_refresh = bool(auto.get("enabled"))
    auto_enabled = limit_refresh and not auto_release
    release_active_mode = current_mode_i in ACTIVE_RELEASE_MODES
    active_refresh = mode_i in ACTIVE_REFRESH_MODES
    set_power_auto_requested = bool(auto.get("set_power_auto"))
    should_set_power_auto = False
    set_power_auto_suppressed = False
    release_strategy = "none"
    path = "mode:%s" % mode_label(mode_i)
    if auto_release:
        path = "auto_limit_release"
        release_strategy = "stop_set_power_refresh"
        set_power_auto_suppressed = bool(set_power_auto_requested or release_active_mode)
    elif auto_enabled:
        path = "auto_limit_enabled"
        # Eine aktive DISCH/CHRG/GRID-Vorgabe muss vor einer schützenden
        # EMS-Grenze gezielt auf 0 W gestoppt werden. Das ist kein AUTO-Freilauf.
        should_set_power_auto = bool(
            release_active_mode
            or (set_power_auto_requested and current_mode_i != MODE_AUTO)
        )
    elif mode_i == MODE_AUTO:
        path = "auto_release"
        release_strategy = "stop_set_power_refresh"
        set_power_auto_suppressed = bool(release_active_mode)
    return {
        "contract_version": RSCP_COMMAND_CONTRACT_VERSION,
        "path": path,
        "mode": mode_i,
        "mode_name": mode_label(mode_i),
        "value_w": val_i,
        "current_mode": current_mode_i,
        "current_mode_name": mode_label(current_mode_i),
        "auto_limit_release": auto_release,
        "auto_limit_enabled": auto_enabled,
        "active_refresh": active_refresh,
        "limit_refresh": limit_refresh,
        "release_active_mode": release_active_mode,
        "release_strategy": release_strategy,
        "set_power_auto_requested": set_power_auto_requested,
        "set_power_auto": should_set_power_auto,
        "set_power_auto_suppressed": set_power_auto_suppressed,
        "force_recommended": bool(active_refresh or limit_refresh),
        "discharge_cap_w": max(0, safe_int(discharge_cap_w, 0)) if discharge_cap_w is not None else None,
        "auto_discharge_cap_w": max(0, safe_int(auto_discharge_cap_w, 0)),
    }
