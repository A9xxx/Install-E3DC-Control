"""Storage-only observation mode; an off startup never releases another owner."""
from __future__ import annotations

import json
from typing import Any, Dict, Optional


def storage_regulation_enabled(cfg: Dict[str, Any]) -> bool:
    # Missing key preserves the existing installation behaviour. A malformed
    # explicit value must not silently enable a hardware writer.
    return str(cfg.get("storage_regulation_enabled", "1")).strip().lower() in (
        "1", "true", "yes", "on", "ja", "ein", "aktiv",
    )


def read_storage_regulation_config(path: str) -> Optional[Dict[str, Any]]:
    """Bind the requested switch and its generation to one canonical read."""
    try:
        with open(path, encoding="utf-8") as handle:
            raw = json.load(handle)
        if not isinstance(raw, dict):
            return None
        flat = {}
        for key, value in raw.items():
            if isinstance(value, dict):
                flat.update({str(k).lower(): v for k, v in value.items()})
            else:
                flat[str(key).lower()] = value
        return flat
    except (OSError, ValueError, TypeError):
        return None


def read_storage_regulation_enabled(path: str) -> Optional[bool]:
    """Read the canonical file again at the output boundary, without a cache."""
    cfg = read_storage_regulation_config(path)
    return storage_regulation_enabled(cfg) if cfg is not None else None


class StorageRegulationSwitch:
    """One release opportunity per observed on -> off edge, never on restart.

    A failed/uncertain release is reported and not replayed: a new external
    controller may already have taken over. The existing RSCP receipt proves
    the POWER_SETTINGS release, not the timeout of an earlier SET_POWER.
    """

    def __init__(self, enabled: Optional[bool], prior: Dict[str, Any], request_ts: float):
        self.enabled = enabled
        self.resumed_at = 0.0
        self.status: Dict[str, Any] = {}
        if enabled is False:
            if prior.get("requested_enabled") is False and prior.get("request_ts") == request_ts:
                self.status = dict(prior)
                if self.status.get("release_status") == "pending":
                    self.status.update(release_status="unconfirmed", release_confirmed=False,
                                       release_reason="release_interrupted_by_restart")
            else:
                self.status = {"release_status": "not_released_on_start", "release_confirmed": False}

    def advance(self, requested: Optional[bool], *, request_ts: float, now_s: float) -> bool:
        """Return whether this cycle owns a single explicit release opportunity."""
        before = self.enabled
        self.enabled = requested
        release = before is True and requested is False
        if before is not True and requested is True:
            self.resumed_at = now_s
            self.status = {}
        if release:
            self.status = {"release_status": "pending", "release_confirmed": False}
        elif requested is None:
            self.status = {"release_status": "config_unreadable", "release_confirmed": False}
        elif requested is False and (not self.status or self.status.get("request_ts", request_ts) != request_ts):
            self.status = {"release_status": "not_released_on_start", "release_confirmed": False}
        self.status.update({
            "requested_enabled": requested,
            "request_ts": request_ts,
            "observed_ts": now_s,
            "commands_allowed": requested is True,
        })
        return release

    def complete_release(self, receipt: Optional[Dict[str, Any]], *, reason: str = "") -> None:
        receipt = receipt if isinstance(receipt, dict) else {}
        confirmed = receipt.get("confirmed") is True and receipt.get("output_complete") is True
        self.status.update({
            "release_status": "confirmed" if confirmed else "unconfirmed",
            "release_confirmed": confirmed,
            "release_attempted": bool(receipt.get("attempted")),
            "release_issued": bool(receipt.get("issued")),
            "release_reason": reason or str(receipt.get("reason") or "no_receipt"),
        })

    def manual_allowed(self, manual: Dict[str, Any], request_ts: float) -> bool:
        try:
            return self.enabled is True and float(manual.get("ts", 0)) > max(request_ts, self.resumed_at)
        except (ValueError, TypeError):
            return False
