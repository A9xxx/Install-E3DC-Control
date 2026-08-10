"""Treiberstatusprüfung und ausfallsichere Behandlung veralteter Daten."""

import time


STATUS_DIAGNOSTIC_KEYS = (
    "driver_status_valid", "driver_status_stale", "driver_status_degraded",
    "driver_status_age_s", "driver_status_reason", "driver_status_last_ok_ts",
    "driver_status_last_sample_ts", "driver_status_source", "driver_status_plausible",
    "driver_status_glitch", "driver_status_glitch_reason", "driver_status_last_good_ts",
)

NATIVE_STATUS_CONTRACT_KEYS = (
    "wb_status_source", "wb_status_reason",
)

NATIVE_STATUS_PHYSICAL_KEYS = (
    "plug_locked", "car_connected_rscp",
)

NATIVE_STATUS_POSITIVE_PROJECTION_KEYS = (
    "plug", "connected", "car_connected", "plug_locked",
    "car_connected_rscp", "alg_connected", "charging", "alg_charging",
)

_NATIVE_STATUS_OK_REASONS = {"", "ok", "fresh"}


def _native_status_present(status):
    """Erkennt nur den E3DC-Statusvertrag, nicht gleichnamige Fremd-WB-Felder."""

    if not isinstance(status, dict):
        return False
    return bool(
        "wb_status_valid" in status
        or any(key in status for key in NATIVE_STATUS_CONTRACT_KEYS)
        or "car_connected_rscp" in status
    )


def _native_status_fresh(status):
    if not _native_status_present(status):
        return False
    return bool(
        status.get("wb_status_valid") is True
        and (
            "driver_status_valid" not in status
            or status.get("driver_status_valid") is True
        )
        and not bool(status.get("driver_status_stale", False))
        and not bool(status.get("driver_status_degraded", False))
        and not bool(status.get("driver_status_glitch", False))
        and status.get("driver_status_plausible") is not False
        and status.get("valid") is not False
        and not bool(status.get("stale", False))
    )


def _native_status_failure_reason(*candidates, fallback="native_status_not_fresh"):
    for candidate in candidates:
        if not isinstance(candidate, dict):
            continue
        for key in (
            "driver_status_glitch_reason",
            "driver_status_reason",
            "wb_status_reason",
        ):
            reason = str(candidate.get(key) or "").strip()
            if reason.lower() not in _NATIVE_STATUS_OK_REASONS:
                return reason
    reason = str(fallback or "native_status_not_fresh").strip()
    if reason.lower() in _NATIVE_STATUS_OK_REASONS:
        return "native_status_not_fresh"
    return reason


def _invalidate_native_status(status, *reason_candidates, fallback="native_status_not_fresh"):
    if not isinstance(status, dict) or not any(
        _native_status_present(candidate)
        for candidate in (status,) + reason_candidates
    ):
        return status
    for candidate in reason_candidates:
        if not isinstance(candidate, dict):
            continue
        source = str(candidate.get("wb_status_source") or "").strip()
        if source:
            status["wb_status_source"] = source
            break
    if not str(status.get("wb_status_source") or "").strip():
        status["wb_status_source"] = "native_status_contract"
    status["wb_status_valid"] = False
    status["wb_status_reason"] = _native_status_failure_reason(
        *reason_candidates,
        status,
        fallback=fallback,
    )
    for key in NATIVE_STATUS_PHYSICAL_KEYS + ("alg_connected", "alg_charging"):
        if key in status:
            status[key] = False
    return status


class StatusService:
    """Normalisiert frische, tolerierte und veraltete Wallbox-Samples."""

    def __init__(self, logger, diagnostic_keys=STATUS_DIAGNOSTIC_KEYS):
        self.logger = logger
        self.diagnostic_keys = tuple(diagnostic_keys)

    def copy_diagnostics(self, detail, status):
        if not isinstance(detail, dict) or not isinstance(status, dict):
            return detail
        for key in self.diagnostic_keys:
            if key in status and key not in detail:
                detail[key] = status[key]

        native_status_present = _native_status_present(status)
        native_status_fresh = _native_status_fresh(status)
        if native_status_present:
            detail["wb_status_valid"] = native_status_fresh
            detail["wb_status_source"] = str(
                status.get("wb_status_source") or "native_status_contract"
            )
            detail["wb_status_reason"] = (
                str(status.get("wb_status_reason") or "fresh")
                if native_status_fresh
                else _native_status_failure_reason(status)
            )
        if native_status_fresh:
            for key in NATIVE_STATUS_PHYSICAL_KEYS:
                if key in status:
                    detail[key] = status[key]
        elif native_status_present:
            for key in NATIVE_STATUS_PHYSICAL_KEYS:
                detail.pop(key, None)
            for key in NATIVE_STATUS_POSITIVE_PROJECTION_KEYS:
                if key in detail:
                    detail[key] = False
        return detail

    def safe_stale_status(self, c_data, *, now_ts=None, age_s=999999.0, reason="status_unavailable"):
        now_value = time.time() if now_ts is None else float(now_ts)
        box = c_data if isinstance(c_data, dict) else {}
        last_status = box.get("last_valid")
        seed_status = last_status if isinstance(last_status, dict) else box.get("_last_invalid_status")
        status = dict(seed_status) if isinstance(seed_status, dict) else {}
        status.update({
            "car": 1, "amp": 0, "pha": 0, "charging": False, "charge_state": False,
            "plug_state": False, "locked": False, "real_power_w": 0.0, "power_w": 0.0,
            "evse_current": 0.0, "phase_power_l1_w": 0.0, "phase_power_l2_w": 0.0,
            "phase_power_l3_w": 0.0, "phase_power_sum_w": 0.0, "phase_power_verified": False,
            "phase_apparent_l1_va": 0.0, "phase_apparent_l2_va": 0.0,
            "phase_apparent_l3_va": 0.0, "apparent_power_va": 0.0, "power_factor": 0.0,
            "phases_in_use": 0, "phases_actual": 0, "driver_status_valid": False,
            "driver_status_stale": True, "driver_status_degraded": True,
            "driver_status_age_s": round(float(age_s), 1),
            "driver_status_reason": str(reason or "status_unavailable"),
            "driver_status_last_sample_ts": int(now_value),
            "driver_status_last_ok_ts": int(float(box.get("_status_last_ok_ts", 0.0) or 0.0)),
            "charge_contract": {}, "charge_truth": "unknown",
            "charge_source": "driver_status_stale",
        })
        _invalidate_native_status(
            status,
            seed_status,
            fallback=str(reason or "status_unavailable"),
        )
        if status.get("phases_target") is None:
            status["phases_target"] = 0
        return status

    def status_or_stale(self, c_data, raw_status, *, now_ts=None, stale_guard_s=45.0):
        now_value = time.time() if now_ts is None else float(now_ts)
        box = c_data if isinstance(c_data, dict) else {}
        raw_invalid = bool(isinstance(raw_status, dict) and (
            raw_status.get("driver_status_valid") is False
            or raw_status.get("driver_status_stale") is True
            or raw_status.get("driver_status_degraded") is True
            or raw_status.get("driver_status_plausible") is False
            or raw_status.get("driver_status_glitch") is True
            or (
                _native_status_present(raw_status)
                and raw_status.get("wb_status_valid") is not True
            )
        ))
        if isinstance(raw_status, dict) and raw_status and not raw_invalid:
            raw_status.update({
                "driver_status_valid": True, "driver_status_stale": False,
                "driver_status_degraded": False, "driver_status_age_s": 0.0,
                "driver_status_reason": str(raw_status.get("driver_status_reason") or "fresh"),
                "driver_status_last_sample_ts": int(now_value),
                "driver_status_last_ok_ts": int(now_value),
            })
            box["_status_last_ok_ts"] = now_value
            box["_status_last_fail_ts"] = 0.0
            box["_status_stale_logged"] = False
            box["last_valid"] = dict(raw_status)
            return raw_status
        if isinstance(raw_status, dict):
            box["_last_invalid_status"] = dict(raw_status)

        last_ok_ts = float(box.get("_status_last_ok_ts", 0.0) or 0.0)
        last_status = box.get("last_valid")
        age_s = now_value - last_ok_ts if last_ok_ts > 0.0 else 999999.0
        box["_status_last_fail_ts"] = now_value
        if isinstance(last_status, dict) and age_s <= float(stale_guard_s):
            status = dict(last_status)
            status.update({
                "driver_status_valid": False, "driver_status_degraded": True,
                "driver_status_stale": False, "driver_status_age_s": round(float(age_s), 1),
                "driver_status_reason": str(
                    (raw_status or {}).get("driver_status_glitch_reason")
                    or (raw_status or {}).get("driver_status_reason")
                    or "last_good_within_grace"
                ),
                "driver_status_last_sample_ts": int(now_value),
                "driver_status_last_ok_ts": int(last_ok_ts),
            })
            for key in ("driver_status_plausible", "driver_status_glitch", "driver_status_glitch_reason", "driver_status_last_good_ts"):
                if isinstance(raw_status, dict) and key in raw_status:
                    status[key] = raw_status[key]
            _invalidate_native_status(
                status,
                raw_status,
                last_status,
                fallback=str(status.get("driver_status_reason") or "last_good_within_grace"),
            )
            return status

        if not box.get("_status_stale_logged", False):
            try:
                self.logger.warning(
                    "WB%d Status stale: keine frischen Treiberdaten seit %.0fs, Messwerte werden entwertet."
                    % (int(box.get("id", 0) or 0), float(age_s))
                )
            except Exception:
                pass
            box["_status_stale_logged"] = True
        stale_reason = (
            (raw_status or {}).get("driver_status_glitch_reason")
            or (raw_status or {}).get("driver_status_reason")
            or "status_timeout"
        )
        return self.safe_stale_status(box, now_ts=now_value, age_s=age_s, reason=str(stale_reason))
