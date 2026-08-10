#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""RSCP-Verbindung und Befehlsvertrag für den Storage Manager."""

from __future__ import annotations

import logging
import math
import time
from typing import Any, Dict, Optional, Tuple

try:
    from Installer.rscp_client import RscpConnection, RscpTag, RscpType, find_tag
except ModuleNotFoundError:
    from rscp_client import RscpConnection, RscpTag, RscpType, find_tag  # type: ignore

try:
    from Installer.storage_parallel_regulator import (
        EMS_POWER_SETTINGS_NONZERO_MIN_W,
        MODE_AUTO,
        MODE_CHRG,
        MODE_DISCH,
        MODE_GRID,
        MODE_IDLE,
    )
except ModuleNotFoundError:
    from storage_parallel_regulator import (  # type: ignore
        EMS_POWER_SETTINGS_NONZERO_MIN_W,
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
RSCP_COMMAND_CONTRACT_VERSION = 3
RSCP_POWER_SETTINGS_CONTRACT_VERSION = 2
RSCP_POWER_SETTINGS_RETRY_S = 10.0
RSCP_POWER_SETTINGS_READBACK_GRACE_S = 10.0
RSCP_POWER_SETTINGS_TOLERANCE_W = 50
RSCP_SEND_RECEIPT_CONTRACT_VERSION = 1

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
        self._settings_pending_target: Optional[Dict[str, Any]] = None
        self._settings_pending_bounded_zero_w = 0
        self._settings_pending_started_monotonic = 0.0
        self._settings_pending_deadline_monotonic = 0.0
        self._settings_pending_started_ts = 0
        self._settings_pending_response_codes: Optional[list] = None
        self._settings_last_reconcile_fresh = False
        self._settings_set_requests = 0
        self._settings_get_requests = 0
        self._settings_suppressed = 0
        self._last_power_settings_wire_receipt: Dict[str, Any] = {}
        self._last_set_power_receipt: Dict[str, Any] = {}
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
        bounded_zero_w: int = 0,
    ) -> bool:
        if self._settings_limits_used is False and limits_used is False:
            return True
        charge_matches = abs(charge_w - self._settings_charge_cap) < RSCP_POWER_SETTINGS_TOLERANCE_W
        if limits_used and charge_w == 0 and bounded_zero_w > 0:
            charge_matches = 0 <= self._settings_charge_cap <= bounded_zero_w
        return bool(
            self._settings_limits_used is limits_used
            and charge_matches
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
        if not isinstance(raw_limits_used, bool):
            return None
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (raw_charge_w, raw_discharge_w, raw_discharge_start_w)
        ):
            return None
        return {
            "limits_used": raw_limits_used,
            "max_charge_w": raw_charge_w,
            "max_discharge_w": raw_discharge_w,
            "discharge_start_w": raw_discharge_start_w,
        }

    def _accept_power_settings_readback(self, readback: Dict[str, Any]) -> None:
        self._settings_charge_cap = int(readback["max_charge_w"])
        self._settings_discharge_cap = int(readback["max_discharge_w"])
        self._settings_discharge_start = int(readback["discharge_start_w"])
        self._settings_limits_used = bool(readback["limits_used"])
        self._charge_cap = int(readback["max_charge_w"])
        self._discharge_cap = int(readback["max_discharge_w"])
        self._clear_pending_power_settings()
        self._settings_retry_after_monotonic = 0.0

    @staticmethod
    def _power_settings_readback_matches(
        readback: Optional[Dict[str, Any]],
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
        bounded_zero_w: int = 0,
    ) -> bool:
        if not isinstance(readback, dict) or readback.get("limits_used") is not limits_used:
            return False
        if not limits_used:
            return True
        readback_charge_w = int(readback.get("max_charge_w", -1))
        charge_matches = abs(readback_charge_w - charge_w) < RSCP_POWER_SETTINGS_TOLERANCE_W
        if charge_w == 0 and bounded_zero_w > 0:
            charge_matches = 0 <= readback_charge_w <= bounded_zero_w
        return bool(
            charge_matches
            and abs(int(readback.get("max_discharge_w", -1)) - discharge_w) < RSCP_POWER_SETTINGS_TOLERANCE_W
            and abs(int(readback.get("discharge_start_w", -1)) - discharge_start_w) < RSCP_POWER_SETTINGS_TOLERANCE_W
        )

    @staticmethod
    def _power_settings_readback_matches_exact(
        readback: Optional[Dict[str, Any]],
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
        bounded_zero_w: int = 0,
    ) -> bool:
        if not isinstance(readback, dict) or readback.get("limits_used") is not limits_used:
            return False
        if not limits_used:
            return True
        readback_charge_w = int(readback.get("max_charge_w", -1))
        charge_matches = readback_charge_w == charge_w
        if charge_w == 0 and bounded_zero_w > 0:
            charge_matches = 0 <= readback_charge_w <= bounded_zero_w
        return bool(
            charge_matches
            and int(readback.get("max_discharge_w", -1)) == discharge_w
            and int(readback.get("discharge_start_w", -1)) == discharge_start_w
        )

    def _write_and_verify_power_settings(
        self,
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
        *,
        stage: str,
        bounded_zero_w: int = 0,
    ) -> bool:
        requested = {
            "limits_used": limits_used,
            "max_charge_w": charge_w,
            "max_discharge_w": discharge_w,
            "discharge_start_w": discharge_start_w,
        }
        self._last_power_settings_wire_receipt = {
            "kind": "power_settings",
            "target": dict(requested),
            "attempted": True,
            "issued": False,
            "acknowledged": None,
            "confirmed": False,
            "retained": False,
            "reason": "request_started",
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
        self._last_power_settings_wire_receipt.update({
            "issued": True,
            "response_returned": True,
            "issued_at": time.time(),
            "reason": "request_returned",
        })
        response_codes = self._power_settings_response_codes(response)
        self._last_power_settings_wire_receipt["acknowledged"] = (
            True
            if isinstance(response_codes, list) and len(response_codes) >= 4
            else None
        )
        if response_codes is None or len(response_codes) < 4:
            # Manche E3DC-Generationen übernehmen eine sichere Begrenzung,
            # liefern aber keine für diesen Vertrag auswertbare SET-Antwort.
            # Genau ein kanonischer GET darf die Wirkung bestätigen; ohne
            # exakten, typisierten Match bleibt der Ausgang fail-closed.
            readback = self._read_power_settings()
            readback_matches = self._power_settings_readback_matches_exact(
                readback,
                charge_w,
                discharge_w,
                discharge_start_w,
                limits_used,
                bounded_zero_w,
            )
            if readback_matches:
                assert isinstance(readback, dict)
                self._accept_power_settings_readback(readback)
                self._power_settings_diag = {
                    "schema": "rscp_power_settings_v1",
                    "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
                    "status": "confirmed_from_get_ack_unknown",
                    "stage": stage,
                    "confirmed": True,
                    "acknowledged": None,
                    "acknowledgement_status": "unknown_invalid_set_response",
                    "requested": requested,
                    "response_codes": response_codes,
                    "readback": readback,
                    "readback_source": "command_get_after_invalid_set_response",
                    "bounded_zero_w": max(0, int(bounded_zero_w)),
                    "ts": int(time.time()),
                }
                self._last_power_settings_wire_receipt.update({
                    "confirmed": True,
                    "reason": "confirmed_from_get_ack_unknown",
                })
                return True
            self._power_settings_diag = {
                "schema": "rscp_power_settings_v1",
                "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
                "status": (
                    "set_response_invalid_readback_mismatch"
                    if readback is not None
                    else "set_response_invalid_readback_missing"
                ),
                "stage": stage,
                "confirmed": False,
                "acknowledged": None,
                "acknowledgement_status": "unknown_invalid_set_response",
                "requested": requested,
                "response_codes": response_codes,
                "readback": readback,
                "readback_source": "command_get_after_invalid_set_response",
                "bounded_zero_w": max(0, int(bounded_zero_w)),
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
                bounded_zero_w,
            )
            if readback_matches:
                break
            if attempt == 0:
                time.sleep(0.05)
        if not readback_matches:
            now_monotonic = time.monotonic()
            self._settings_pending_target = dict(requested)
            self._settings_pending_bounded_zero_w = max(0, int(bounded_zero_w))
            self._settings_pending_started_monotonic = now_monotonic
            self._settings_pending_deadline_monotonic = (
                now_monotonic + RSCP_POWER_SETTINGS_READBACK_GRACE_S
            )
            self._settings_pending_started_ts = int(time.time())
            self._settings_pending_response_codes = list(response_codes)
            self._settings_retry_after_monotonic = 0.0
            self._power_settings_diag = {
                "schema": "rscp_power_settings_v1",
                "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
                "status": "pending_readback" if readback is not None else "pending_readback_missing",
                "stage": stage,
                "confirmed": False,
                "acknowledged": True,
                "requested": requested,
                "response_codes": response_codes,
                "readback": readback,
                "readback_source": "command_verification",
                "bounded_zero_w": max(0, int(bounded_zero_w)),
                "ts": self._settings_pending_started_ts,
            }
            return False

        self._accept_power_settings_readback(readback)
        bounded_zero_equivalent = bool(
            limits_used
            and charge_w == 0
            and 0 < int(readback["max_charge_w"]) <= max(0, int(bounded_zero_w))
        )
        self._power_settings_diag = {
            "schema": "rscp_power_settings_v1",
            "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
            "status": (
                "confirmed_bounded_zero"
                if bounded_zero_equivalent
                else ("confirmed_nonoptimal" if any(code == 1 for code in response_codes) else "confirmed")
            ),
            "stage": stage,
            "confirmed": True,
            "requested": requested,
            "response_codes": response_codes,
            "readback": readback,
            "bounded_zero_w": max(0, int(bounded_zero_w)),
            "bounded_zero_equivalent": bounded_zero_equivalent,
            "ts": int(time.time()),
        }
        self._last_power_settings_wire_receipt.update({
            "confirmed": True,
            "reason": self._power_settings_diag["status"],
        })
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
        diag["pending_remaining_s"] = round(
            max(0.0, self._settings_pending_deadline_monotonic - time.monotonic()),
            1,
        )
        return diag

    def _clear_pending_power_settings(self) -> None:
        self._settings_pending_target = None
        self._settings_pending_bounded_zero_w = 0
        self._settings_pending_started_monotonic = 0.0
        self._settings_pending_deadline_monotonic = 0.0
        self._settings_pending_started_ts = 0
        self._settings_pending_response_codes = None

    @staticmethod
    def _power_settings_target_dict(
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int,
        limits_used: bool,
    ) -> Dict[str, Any]:
        return {
            "limits_used": bool(limits_used),
            "max_charge_w": int(charge_w),
            "max_discharge_w": int(discharge_w),
            "discharge_start_w": int(discharge_start_w),
        }

    def _pending_target_matches(
        self,
        target: Dict[str, Any],
        bounded_zero_w: int,
    ) -> bool:
        pending = self._settings_pending_target
        if not isinstance(pending, dict):
            return False
        if pending.get("limits_used") is not target.get("limits_used"):
            return False
        if target.get("limits_used") is False:
            return True
        pending_charge_w = int(pending.get("max_charge_w", -1))
        target_charge_w = int(target.get("max_charge_w", -1))
        charge_matches = abs(pending_charge_w - target_charge_w) < RSCP_POWER_SETTINGS_TOLERANCE_W
        if target_charge_w == 0 and bounded_zero_w > 0:
            charge_matches = 0 <= pending_charge_w <= bounded_zero_w
        return bool(
            charge_matches
            and abs(int(pending.get("max_discharge_w", -1)) - int(target.get("max_discharge_w", -1)))
            < RSCP_POWER_SETTINGS_TOLERANCE_W
            and abs(int(pending.get("discharge_start_w", -1)) - int(target.get("discharge_start_w", -1)))
            < RSCP_POWER_SETTINGS_TOLERANCE_W
        )

    def _pending_target_may_be_superseded(self, target: Dict[str, Any]) -> bool:
        pending = self._settings_pending_target
        if not isinstance(pending, dict):
            return True
        if target.get("limits_used") is False:
            return True
        if pending.get("limits_used") is False:
            return True
        return bool(
            int(target.get("max_charge_w", 0)) <= int(pending.get("max_charge_w", 0))
            and int(target.get("max_discharge_w", 0)) <= int(pending.get("max_discharge_w", 0))
        )

    def reconcile_power_settings(self, snapshot: Dict[str, Any], *, fresh: bool) -> bool:
        """Übernimmt einen frischen kanonischen GET_POWER_SETTINGS-Readback ohne Write."""
        self._settings_last_reconcile_fresh = False
        if (
            not fresh
            or snapshot.get("ems_power_settings_read") is not True
            or snapshot.get("ems_power_settings_valid") is not True
        ):
            return False
        limits_used = snapshot.get("ems_power_limits_active")
        charge_w = snapshot.get("ems_max_charge_power_w")
        discharge_w = snapshot.get("ems_max_discharge_power_w")
        discharge_start_w = snapshot.get("ems_discharge_start_power_w")
        if not isinstance(limits_used, bool):
            return False
        if any(
            not isinstance(value, int) or isinstance(value, bool) or value < 0
            for value in (charge_w, discharge_w, discharge_start_w)
        ):
            return False
        reconcile_time_s = time.time()
        raw_readback_cycle_ts = snapshot.get("_ts")
        if (
            not isinstance(raw_readback_cycle_ts, (int, float))
            or isinstance(raw_readback_cycle_ts, bool)
            or not math.isfinite(float(raw_readback_cycle_ts))
            or float(raw_readback_cycle_ts) <= 0.0
        ):
            return False
        readback_cycle_ts = float(raw_readback_cycle_ts)
        if readback_cycle_ts > 100_000_000_000.0:
            readback_cycle_ts /= 1000.0
        readback_age_s = reconcile_time_s - readback_cycle_ts
        if not math.isfinite(readback_age_s) or not 0.0 <= readback_age_s <= 5.0:
            return False
        readback = {
            "limits_used": limits_used,
            "max_charge_w": int(charge_w),
            "max_discharge_w": int(discharge_w),
            "discharge_start_w": int(discharge_start_w),
        }
        self._settings_last_reconcile_fresh = True
        if isinstance(self._settings_pending_target, dict):
            pending = dict(self._settings_pending_target)
            bounded_zero_w = self._settings_pending_bounded_zero_w
            if self._power_settings_readback_matches(
                readback,
                int(pending["max_charge_w"]),
                int(pending["max_discharge_w"]),
                int(pending["discharge_start_w"]),
                bool(pending["limits_used"]),
                bounded_zero_w,
            ):
                response_codes = list(self._settings_pending_response_codes or [])
                self._settings_limits_used = bool(readback["limits_used"])
                self._settings_charge_cap = int(readback["max_charge_w"])
                self._settings_discharge_cap = int(readback["max_discharge_w"])
                self._settings_discharge_start = int(readback["discharge_start_w"])
                self._charge_cap = int(readback["max_charge_w"])
                self._discharge_cap = int(readback["max_discharge_w"])
                self._settings_retry_after_monotonic = 0.0
                self._clear_pending_power_settings()
                self._power_settings_diag = {
                    "schema": "rscp_power_settings_v1",
                    "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
                    "status": "confirmed_from_live_readback",
                    "stage": "live_reconciliation",
                    "confirmed": True,
                    "acknowledged": True,
                    "requested": pending,
                    "response_codes": response_codes,
                    "readback": readback,
                    "readback_source": "canonical_live",
                    "readback_cycle_ts": readback_cycle_ts,
                    "bounded_zero_w": bounded_zero_w,
                    "bounded_zero_equivalent": bool(
                        pending["limits_used"]
                        and int(pending["max_charge_w"]) == 0
                        and 0 < int(readback["max_charge_w"]) <= bounded_zero_w
                    ),
                    "ts": int(reconcile_time_s),
                }
                return True
            now_monotonic = time.monotonic()
            if now_monotonic < self._settings_pending_deadline_monotonic:
                self._power_settings_diag = {
                    **self._power_settings_diag,
                    "status": "pending_readback",
                    "confirmed": False,
                    "acknowledged": True,
                    "requested": pending,
                    "readback": readback,
                    "readback_source": "canonical_live",
                    "readback_cycle_ts": readback_cycle_ts,
                    "bounded_zero_w": bounded_zero_w,
                    "ts": int(reconcile_time_s),
                }
                return False
            if self._settings_retry_after_monotonic <= 0.0:
                self._settings_retry_after_monotonic = now_monotonic + RSCP_POWER_SETTINGS_RETRY_S
            self._power_settings_diag = {
                **self._power_settings_diag,
                "status": "readback_mismatch",
                "confirmed": False,
                "acknowledged": True,
                "requested": pending,
                "readback": readback,
                "readback_source": "canonical_live",
                "readback_cycle_ts": readback_cycle_ts,
                "bounded_zero_w": bounded_zero_w,
                "pending_expired": True,
                "ts": int(reconcile_time_s),
            }
            return False
        self._settings_limits_used = limits_used
        self._settings_charge_cap = int(charge_w)
        self._settings_discharge_cap = int(discharge_w)
        self._settings_discharge_start = int(discharge_start_w)
        self._charge_cap = int(charge_w)
        self._discharge_cap = int(discharge_w)
        self._settings_retry_after_monotonic = 0.0
        self._power_settings_diag = {
            "schema": "rscp_power_settings_v1",
            "contract_version": RSCP_POWER_SETTINGS_CONTRACT_VERSION,
            "status": "confirmed_from_live_readback",
            "stage": "live_reconciliation",
            "confirmed": True,
            "readback": readback,
            "readback_source": "canonical_live",
            "readback_cycle_ts": readback_cycle_ts,
            "ts": int(reconcile_time_s),
        }
        return True

    def set_power_limit_settings(
        self,
        charge_w: int,
        discharge_w: int,
        discharge_start_w: int = 0,
        limits_used: bool = True,
        force: bool = False,
        bounded_zero_w: int = 0,
    ) -> bool:
        charge_w = max(0, int(charge_w or 0))
        discharge_w = max(0, int(discharge_w or 0))
        discharge_start_w = max(0, int(discharge_start_w or 0))
        limits_used = bool(limits_used)
        bounded_zero_w = max(0, int(bounded_zero_w or 0)) if limits_used and charge_w == 0 else 0
        target = self._power_settings_target_dict(
            charge_w,
            discharge_w,
            discharge_start_w,
            limits_used,
        )
        if isinstance(self._settings_pending_target, dict):
            now_monotonic = time.monotonic()
            if self._pending_target_matches(target, bounded_zero_w):
                self._settings_suppressed += 1
                if now_monotonic < self._settings_pending_deadline_monotonic:
                    pending_status = (
                        "pending_readback"
                        if isinstance(self._power_settings_diag.get("readback"), dict)
                        else "pending_readback_missing"
                    )
                    self._power_settings_diag = {
                        **self._power_settings_diag,
                        "status": pending_status,
                        "confirmed": False,
                        "ts": int(time.time()),
                    }
                    return False
                if now_monotonic < self._settings_retry_after_monotonic:
                    self._power_settings_diag = {
                        **self._power_settings_diag,
                        "status": "retry_backoff",
                        "confirmed": False,
                        "ts": int(time.time()),
                    }
                    return False
                if not self._settings_last_reconcile_fresh:
                    self._power_settings_diag = {
                        **self._power_settings_diag,
                        "status": "readback_stale",
                        "confirmed": False,
                        "ts": int(time.time()),
                    }
                    return False
                self._clear_pending_power_settings()
            elif not self._pending_target_may_be_superseded(target):
                self._power_settings_diag = {
                    **self._power_settings_diag,
                    "status": "pending_target_conflict",
                    "confirmed": False,
                    "next_requested": target,
                    "ts": int(time.time()),
                }
                return False
            else:
                self._clear_pending_power_settings()
                self._settings_retry_after_monotonic = 0.0
        if not force and self._power_settings_target_matches(
            charge_w,
            discharge_w,
            discharge_start_w,
            limits_used,
            bounded_zero_w,
        ):
            # Ein gecachter Soll-/Readbackwert ist kein aktueller Beweis. Der
            # Manager ruft vor jedem Ausgang reconcile_power_settings() auf;
            # nur dessen frischer, typisierter Live-Readback darf einen
            # unveränderten POWER_SETTINGS-Vertrag bestätigen.
            if not self._settings_last_reconcile_fresh:
                self._power_settings_diag = {
                    **self._power_settings_diag,
                    "status": "readback_stale",
                    "confirmed": False,
                    "requested": target,
                    "ts": int(time.time()),
                }
                return False
            self._settings_suppressed += 1
            self._power_settings_diag = {
                **self._power_settings_diag,
                "status": "confirmed_unchanged",
                "confirmed": True,
                "requested": target,
                "bounded_zero_w": bounded_zero_w,
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
            if not self._write_and_verify_power_settings(
                charge_w,
                discharge_w,
                discharge_start_w,
                limits_used,
                stage="target",
                bounded_zero_w=bounded_zero_w,
            ):
                if str(self._power_settings_diag.get("status") or "").startswith("pending_readback"):
                    return False
                status = str(self._power_settings_diag.get("status") or "")
                if status == "set_response_invalid_readback_missing":
                    raise RuntimeError(
                        "POWER_SETTINGS-SET-Antwort ungültig; kanonischer Readback fehlt"
                    )
                if status == "set_response_invalid_readback_mismatch":
                    raise RuntimeError(
                        "POWER_SETTINGS-SET-Antwort ungültig; kanonischer Readback weicht von der Vorgabe ab"
                    )
                raise RuntimeError("POWER_SETTINGS-Readback stimmt nicht mit der Vorgabe überein")
            if self._power_settings_diag.get("status") == "confirmed_from_get_ack_unknown":
                log.warning(
                    "RSCP POWER_SETTINGS: SET-Antwort unbekannt; kanonischer GET bestätigt "
                    "limits=%s max_charge=%dW max_discharge=%dW discharge_start=%dW",
                    "on" if limits_used else "off",
                    charge_w,
                    discharge_w,
                    discharge_start_w,
                )
            else:
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

    def set_max_charge_power(self, val: int, force: bool = False) -> Dict[str, Any]:
        val = int(val)
        receipt: Dict[str, Any] = {
            "kind": "max_charge_power",
            "target_w": val,
            "attempted": False,
            "issued": False,
            "acknowledged": None,
            "confirmed": False,
            "retained": False,
            "reason": "not_attempted",
        }
        if not self._c and not self._conn():
            receipt["reason"] = "connection_failed"
            return receipt
        if not force and self._charge_cap >= 0 and abs(val - self._charge_cap) < 50:
            receipt.update({
                "retained": True,
                "reason": "same_target_cache_only",
            })
            return receipt
        actual = max(50, val) if 0 < val < 200 else val
        receipt.update({
            "attempted": True,
            "actual_target_w": actual,
            "reason": "request_started",
        })
        try:
            self._c.request([{"tag": RscpTag.EMS_REQ_SET_MAX_CHARGE_POWER, "type": RscpType.Int32, "value": actual}])
            self._charge_cap = val
            receipt.update({
                "issued": True,
                "response_returned": True,
                "issued_at": time.time(),
                "reason": "request_returned_ack_unknown",
            })
            log.info("RSCP MAX_CHARGE_POWER: %dW (Echt: %dW)", val, actual)
        except Exception as exc:
            log.error("RSCP set_max_charge: %s", exc)
            self._c = None
            receipt["reason"] = "request_failed"
        return receipt

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

    def _send_set_power_receipt(
        self,
        mode: int,
        value_w: int,
        *,
        force: bool,
        reason: str = "",
    ) -> Dict[str, Any]:
        mode = int(mode)
        value_w = max(0, int(value_w or 0))
        same_command = mode == self._mode and abs(value_w - self._val) < 50
        previous = (
            dict(self._last_set_power_receipt)
            if (
                self._last_set_power_receipt.get("issued") is True
                and safe_int(self._last_set_power_receipt.get("mode"), -1) == mode
                and abs(safe_int(self._last_set_power_receipt.get("value_w"), -1) - value_w) < 50
            )
            else {}
        )
        receipt: Dict[str, Any] = {
            "kind": "set_power",
            "mode": mode,
            "mode_name": mode_label(mode),
            "value_w": value_w,
            "attempted": False,
            "issued": False,
            "acknowledged": None,
            "confirmed": False,
            "retained": False,
            "prior_same_target_issued_at": previous.get("issued_at"),
            "reason": "not_attempted",
        }
        if not force and same_command:
            receipt.update({
                "retained": bool(previous),
                "reason": (
                    "same_command_retained"
                    if previous
                    else "same_command_cache_without_receipt"
                ),
            })
            return receipt
        if not self._c and not self._conn():
            receipt["reason"] = "connection_failed"
            return receipt
        receipt.update({
            "attempted": True,
            "reason": "request_started",
        })
        try:
            self._c.request([{"tag": RscpTag.EMS_REQ_SET_POWER, "type": RscpType.Container, "value": [
                {"tag": RscpTag.EMS_REQ_SET_POWER_MODE, "type": RscpType.UChar8, "value": mode},
                {"tag": RscpTag.EMS_REQ_SET_POWER_VALUE, "type": RscpType.Int32, "value": value_w},
            ]}])
            issued_at = time.time()
            self._mode = mode
            self._val = value_w
            receipt.update({
                "issued": True,
                "response_returned": True,
                "issued_at": issued_at,
                "reason": "request_returned_ack_unknown",
            })
            self._last_set_power_receipt = dict(receipt)
            suffix = f" ({reason})" if reason else ""
            if same_command and force:
                log.debug("RSCP REFRESH: %s(%d) %dW%s", mode_label(mode), mode, value_w, suffix)
            else:
                log.info("RSCP SET: %s(%d) %dW%s", mode_label(mode), mode, value_w, suffix)
        except Exception as exc:
            log.error("RSCP send: %s", exc)
            self._c = None
            receipt["reason"] = "request_failed"
        return receipt

    def set_power_auto(self, value_w: int = 0, force: bool = False, reason: str = "") -> bool:
        receipt = self._send_set_power_receipt(
            MODE_AUTO,
            value_w,
            force=force,
            reason=reason,
        )
        return bool(receipt.get("issued") or receipt.get("retained"))

    def send(
        self,
        mode: int,
        val: int,
        force: bool = False,
        discharge_cap_w: Optional[int] = None,
        auto_limit: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
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

        receipt: Dict[str, Any] = {
            "schema": "rscp_send_receipt_v1",
            "contract_version": RSCP_SEND_RECEIPT_CONTRACT_VERSION,
            "path": command_contract.get("path"),
            "target": {"mode": mode, "value_w": val},
            "attempted": False,
            "issued": False,
            "acknowledged": None,
            "confirmed": False,
            "retained": False,
            "partial": False,
            "output_complete": False,
            "substeps": {},
            "reason": "not_attempted",
        }

        def _record_primary(step: Dict[str, Any]) -> None:
            receipt["attempted"] = bool(step.get("attempted"))
            receipt["issued"] = bool(step.get("issued"))
            receipt["acknowledged"] = step.get("acknowledged")
            receipt["confirmed"] = bool(step.get("confirmed"))
            receipt["retained"] = bool(step.get("retained"))
            receipt["reason"] = step.get("reason") or "not_attempted"

        def _power_settings_step(confirmed: bool) -> Dict[str, Any]:
            diag = self.power_settings_diagnostics()
            wire = dict(self._last_power_settings_wire_receipt)
            attempted = bool(wire.get("attempted"))
            step = {
                "kind": "power_settings",
                "target": dict(diag.get("requested") or wire.get("target") or {}),
                "attempted": attempted,
                "issued": bool(wire.get("issued")) if attempted else False,
                "acknowledged": wire.get("acknowledged") if attempted else None,
                "confirmed": bool(confirmed and diag.get("confirmed") is True),
                "retained": bool(not attempted and confirmed and diag.get("confirmed") is True),
                "status": diag.get("status"),
                "reason": (
                    str(diag.get("status") or "confirmed")
                    if confirmed
                    else str(diag.get("status") or wire.get("reason") or "unconfirmed")
                ),
            }
            if wire.get("issued_at") is not None:
                step["issued_at"] = wire.get("issued_at")
            return step

        self._last_power_settings_wire_receipt = {}

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
            confirmed = self.set_power_limit_settings(
                auto_charge_cap,
                auto_discharge_cap,
                _auto_limit_int("discharge_start_w", 0),
                limits_used=False,
                force=False,
            )
            step = _power_settings_step(confirmed)
            receipt["substeps"]["power_settings"] = step
            _record_primary(step)
            if not confirmed:
                receipt["partial"] = bool(step.get("attempted") or step.get("issued"))
                return receipt
            # AUTO-Freilauf entsteht bei E3DC durch das Ende aktiver
            # SET_POWER-Refreshes. AUTO mit 0 W wäre dagegen ein echter
            # Nullleistungsbefehl und würde Laden sowie Entladen sperren.
            self._mode = MODE_AUTO
            self._val = auto_discharge_cap
            receipt["output_complete"] = True
            return receipt
        elif command_contract.get("auto_limit_enabled"):
            auto_discharge_cap = max(0, _auto_limit_int(
                "max_discharge_w",
                int(discharge_cap_w or self._auto_discharge_cap or 0),
            ))
            auto_charge_cap = max(0, _auto_limit_int("max_charge_w", 0))
            confirmed = self.set_power_limit_settings(
                auto_charge_cap,
                auto_discharge_cap,
                _auto_limit_int("discharge_start_w", 0),
                limits_used=True,
                force=False,
                bounded_zero_w=(
                    EMS_POWER_SETTINGS_NONZERO_MIN_W if auto_charge_cap == 0 else 0
                ),
            )
            step = _power_settings_step(confirmed)
            receipt["substeps"]["power_settings"] = step
            _record_primary(step)
            if not confirmed:
                receipt["partial"] = bool(step.get("attempted") or step.get("issued"))
                return receipt
            should_set_power_auto = bool(command_contract.get("set_power_auto"))
            if should_set_power_auto:
                transition = self._send_set_power_receipt(
                    MODE_AUTO,
                    0,
                    force=True,
                    reason="aktive Vorgabe vor EMS-Grenze gestoppt",
                )
                receipt["substeps"]["set_power_auto"] = transition
                receipt["attempted"] = bool(receipt["attempted"] or transition.get("attempted"))
                if not bool(transition.get("issued") or transition.get("retained")):
                    receipt["partial"] = True
                    receipt["reason"] = "power_settings_confirmed_auto_transition_failed"
                    return receipt
            self._mode = MODE_AUTO
            self._val = max(300, int(discharge_cap_w or self._auto_discharge_cap or val or auto_charge_cap or 0))
            receipt["output_complete"] = True
            return receipt
        elif mode == MODE_AUTO:
            auto_discharge_cap = max(
                300,
                int(discharge_cap_w or self._auto_discharge_cap or val or 0),
            )
            auto_charge_cap = max(300, int(val or 0), auto_discharge_cap)
            confirmed = self.set_power_limit_settings(
                auto_charge_cap,
                auto_discharge_cap,
                0,
                limits_used=False,
                force=False,
            )
            step = _power_settings_step(confirmed)
            receipt["substeps"]["power_settings"] = step
            _record_primary(step)
            if not confirmed:
                receipt["partial"] = bool(step.get("attempted") or step.get("issued"))
                return receipt
            self._mode = MODE_AUTO
            self._val = auto_discharge_cap
            receipt["output_complete"] = True
            return receipt
        elif mode == MODE_DISCH:
            receipt["substeps"]["max_charge_power"] = self.set_max_charge_power(0)
        elif mode in (MODE_CHRG, MODE_GRID):
            receipt["substeps"]["max_charge_power"] = self.set_max_charge_power(val)
        set_power = self._send_set_power_receipt(mode, val, force=force)
        receipt["substeps"]["set_power"] = set_power
        _record_primary(set_power)
        auxiliary = receipt["substeps"].get("max_charge_power")
        auxiliary_progress = bool(
            isinstance(auxiliary, dict)
            and (auxiliary.get("attempted") or auxiliary.get("issued") or auxiliary.get("retained"))
        )
        auxiliary_complete = bool(
            not isinstance(auxiliary, dict)
            or auxiliary.get("issued")
            or auxiliary.get("retained")
        )
        receipt["output_complete"] = bool(
            (set_power.get("issued") or set_power.get("retained"))
            and auxiliary_complete
        )
        receipt["attempted"] = bool(receipt["attempted"] or (auxiliary or {}).get("attempted"))
        receipt["partial"] = bool(
            not receipt["output_complete"]
            and (receipt["attempted"] or receipt["issued"] or auxiliary_progress)
        )
        return receipt

    def release_power_limits_explicit(self) -> None:
        """Gibt POWER_SETTINGS ausdrücklich frei; kein Prozess-Cleanup."""

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

    def close_for_handover(self) -> None:
        """Schließt nur die RSCP-Sitzung und lässt flüchtige Limits unangetastet.

        ``rscp_client.py`` definiert die verwendeten GET-/SET-Tags, aber keine
        Haltedauer über Geräte- oder Hostneustarts. Deshalb wird hier weder
        AUTO noch ``POWER_LIMITS_USED=false`` geschrieben. Der Nachfolger muss
        den kanonischen GET-Readback vor seinem ersten Schreibintent bestätigen.
        """

        self.close()

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
    release_active_mode = current_mode_i in ACTIVE_RELEASE_MODES
    active_refresh = mode_i in ACTIVE_REFRESH_MODES
    auto_enabled = limit_refresh and not auto_release and not active_refresh
    set_power_auto_requested = bool(auto.get("set_power_auto"))
    set_power_execution_owner = str(
        auto.get("set_power_execution_owner") or ""
    ).strip()
    external_owner_suppression = bool(
        auto.get("suppress_set_power_auto")
        and set_power_execution_owner == "external_e3dc_luox"
    )
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
        if external_owner_suppression:
            release_strategy = "stop_set_power_refresh_external_owner"
            set_power_auto_suppressed = bool(
                release_active_mode or set_power_auto_requested
            )
        else:
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
        "set_power_execution_owner": set_power_execution_owner or None,
        "external_owner_suppression": external_owner_suppression,
        "force_recommended": bool(active_refresh or limit_refresh),
        "discharge_cap_w": max(0, safe_int(discharge_cap_w, 0)) if discharge_cap_w is not None else None,
        "auto_discharge_cap_w": max(0, safe_int(auto_discharge_cap_w, 0)),
    }
