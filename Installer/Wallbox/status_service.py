"""Treiberstatusprüfung und ausfallsichere Behandlung veralteter Daten."""

import time


STATUS_DIAGNOSTIC_KEYS = (
    "driver_status_valid", "driver_status_stale", "driver_status_degraded",
    "driver_status_age_s", "driver_status_reason", "driver_status_last_ok_ts",
    "driver_status_last_sample_ts", "driver_status_source", "driver_status_plausible",
    "driver_status_glitch", "driver_status_glitch_reason", "driver_status_last_good_ts",
)


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
        if status.get("phases_target") is None:
            status["phases_target"] = 0
        return status

    def status_or_stale(self, c_data, raw_status, *, now_ts=None, stale_guard_s=45.0):
        now_value = time.time() if now_ts is None else float(now_ts)
        box = c_data if isinstance(c_data, dict) else {}
        raw_invalid = bool(isinstance(raw_status, dict) and (
            raw_status.get("driver_status_valid") is False
            or raw_status.get("driver_status_stale") is True
            or raw_status.get("driver_status_plausible") is False
            or raw_status.get("driver_status_glitch") is True
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
