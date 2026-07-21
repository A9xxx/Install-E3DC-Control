#!/usr/bin/env python3
"""Neutraler, ausschließlich lesender Topologienachweis für externe PV-Erzeuger."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import stat
import tempfile
import time
from typing import Any, Dict, Optional


SCHEMA_VERSION = "external_pv_topology_v1"
TOPOLOGY_SOURCE = "e3dc_add_power"
DEFAULT_STATE_PATH = Path("/var/www/html/data/external_pv_topology.json")
STATE_MODE = 0o664
MIN_CONFIRMATION_SAMPLES = 3
MIN_CONFIRMATION_POWER_W = 100.0
MAX_SAMPLE_AGE_S = 15.0
_STATE_KEYS = frozenset({
    "schema_version",
    "topology_present",
    "valid",
    "source",
    "evidence_state",
    "confirmation_samples",
    "minimum_power_w",
    "confirmed_at",
})


class ExternalPvTopologyError(RuntimeError):
    """Der persistente Topologievertrag ist nicht vertrauenswürdig oder nicht schreibbar."""


def _pairs_without_duplicates(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _unknown(reason: str = "not_confirmed") -> Dict[str, Any]:
    return {
        "topology_present": False,
        "valid": False,
        "source": "none",
        "evidence_state": "unknown",
        "reason": reason,
    }


def _invalid(reason: str) -> Dict[str, Any]:
    return {
        "topology_present": False,
        "valid": False,
        "source": "none",
        "evidence_state": "invalid",
        "reason": reason,
    }


def _confirmed(payload: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "topology_present": True,
        "valid": True,
        "source": TOPOLOGY_SOURCE,
        "evidence_state": "confirmed",
        "reason": "multi_sample_confirmed",
        "confirmation_samples": int(payload["confirmation_samples"]),
        "confirmed_at": int(payload["confirmed_at"]),
    }


def _validate_payload(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict) or frozenset(payload) != _STATE_KEYS:
        return "schema_fields_invalid"
    if payload.get("schema_version") != SCHEMA_VERSION:
        return "schema_version_invalid"
    if payload.get("topology_present") is not True or payload.get("valid") is not True:
        return "topology_state_invalid"
    if payload.get("source") != TOPOLOGY_SOURCE or payload.get("evidence_state") != "confirmed":
        return "topology_source_invalid"
    samples = payload.get("confirmation_samples")
    minimum = payload.get("minimum_power_w")
    confirmed_at = payload.get("confirmed_at")
    if isinstance(samples, bool) or not isinstance(samples, int) or samples < MIN_CONFIRMATION_SAMPLES:
        return "confirmation_samples_invalid"
    if isinstance(minimum, bool) or not isinstance(minimum, (int, float)) or not math.isfinite(float(minimum)):
        return "minimum_power_invalid"
    if float(minimum) < MIN_CONFIRMATION_POWER_W:
        return "minimum_power_invalid"
    if isinstance(confirmed_at, bool) or not isinstance(confirmed_at, int) or confirmed_at <= 0:
        return "confirmed_at_invalid"
    return None


def read_external_pv_topology(path: Path | str = DEFAULT_STATE_PATH) -> Dict[str, Any]:
    """Liest einen regulären, nicht verlinkten und schemaexakten Marker fail-closed."""

    target = Path(path)
    try:
        metadata = target.lstat()
    except FileNotFoundError:
        return _unknown()
    except OSError:
        return _invalid("state_metadata_unavailable")
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
        return _invalid("state_not_regular")
    if stat.S_IMODE(metadata.st_mode) != STATE_MODE:
        return _invalid("state_mode_invalid")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(target, flags)
        try:
            opened = os.fstat(descriptor)
            if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
                return _invalid("state_changed_during_read")
            with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
                descriptor = -1
                payload = json.load(handle, object_pairs_hook=_pairs_without_duplicates)
        finally:
            if descriptor >= 0:
                os.close(descriptor)
    except (OSError, UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        return _invalid("state_untrusted")
    reason = _validate_payload(payload)
    return _invalid(reason) if reason else _confirmed(payload)


def _assert_safe_parent(parent: Path) -> None:
    try:
        metadata = parent.lstat()
    except FileNotFoundError:
        parent.mkdir(parents=True, mode=0o2775, exist_ok=True)
        metadata = parent.lstat()
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ExternalPvTopologyError("topology state directory is not a real directory")


def _write_confirmed_state(path: Path, payload: Dict[str, Any]) -> bool:
    """Erzeugt den Marker einmalig und überschreibt niemals vorhandenen Zustand."""

    _assert_safe_parent(path.parent)
    if os.path.lexists(path):
        existing = read_external_pv_topology(path)
        if existing.get("topology_present") is True and existing.get("valid") is True:
            return False
        raise ExternalPvTopologyError("existing topology state is not trusted")
    fd, tmp_name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp_path, STATE_MODE)
        try:
            os.link(tmp_path, path, follow_symlinks=False)
        except FileExistsError:
            existing = read_external_pv_topology(path)
            if existing.get("topology_present") is True and existing.get("valid") is True:
                return False
            raise ExternalPvTopologyError("topology state appeared concurrently and is not trusted")
        finally:
            try:
                tmp_path.unlink()
            except FileNotFoundError:
                pass
        verified = read_external_pv_topology(path)
        if verified.get("topology_present") is not True or verified.get("valid") is not True:
            raise ExternalPvTopologyError("topology state verification failed")
        directory_descriptor = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
        return True
    finally:
        try:
            tmp_path.unlink()
        except FileNotFoundError:
            pass


class ExternalPvTopologyEvidenceTracker:
    """Bestätigt Topologie im RAM und persistiert ausschließlich unknown -> present."""

    def __init__(
        self,
        state_path: Path | str = DEFAULT_STATE_PATH,
        *,
        required_samples: int = MIN_CONFIRMATION_SAMPLES,
        minimum_power_w: float = MIN_CONFIRMATION_POWER_W,
        max_sample_age_s: float = MAX_SAMPLE_AGE_S,
    ) -> None:
        self.state_path = Path(state_path)
        self.required_samples = max(MIN_CONFIRMATION_SAMPLES, int(required_samples))
        self.minimum_power_w = max(MIN_CONFIRMATION_POWER_W, float(minimum_power_w))
        self.max_sample_age_s = max(1.0, float(max_sample_age_s))
        self._confirmation_count = 0
        self._last_confirmation_ts: Optional[float] = None
        self.write_count = 0

    def _sample_is_confirming(self, sample: Any, now_s: float) -> bool:
        if not isinstance(sample, dict):
            return False
        if sample.get("RSCP_Sample_Valid") is not True:
            return False
        if sample.get("Ext_PV_Power_Valid") is not True:
            return False
        if sample.get("Ext_PV_Power_Source") != TOPOLOGY_SOURCE:
            return False
        acquisition = sample.get("RSCP_Acquisition")
        if not isinstance(acquisition, dict):
            return False
        acquisition_mode = acquisition.get("mode")
        if acquisition_mode == "persistent":
            sections = acquisition.get("sections")
            power_section = sections.get("Power Snapshot") if isinstance(sections, dict) else None
            if not isinstance(power_section, dict):
                return False
            if power_section.get("source") != "rscp" or float(power_section.get("age_s", -1.0)) != 0.0:
                return False
        elif acquisition_mode != "legacy_per_cycle":
            return False
        timestamp = sample.get("_ts")
        if isinstance(timestamp, bool) or not isinstance(timestamp, (int, float)):
            return False
        age = float(now_s) - float(timestamp)
        if not math.isfinite(age) or age < -1.0 or age > self.max_sample_age_s:
            return False
        external = sample.get("Ext_PV_Power")
        total = sample.get("PV_Power")
        if isinstance(external, bool) or not isinstance(external, (int, float)):
            return False
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            return False
        external_f = float(external)
        total_f = float(total)
        return bool(
            math.isfinite(external_f)
            and math.isfinite(total_f)
            and external_f >= self.minimum_power_w
            and total_f >= external_f
        )

    def observe(self, sample: Any, *, now_s: Optional[float] = None) -> Dict[str, Any]:
        current = read_external_pv_topology(self.state_path)
        if current.get("topology_present") is True and current.get("valid") is True:
            self._confirmation_count = 0
            self._last_confirmation_ts = None
            return {**current, "write_performed": False}
        if current.get("evidence_state") == "invalid":
            self._confirmation_count = 0
            self._last_confirmation_ts = None
            return {**current, "write_performed": False}
        now_s = time.time() if now_s is None else float(now_s)
        if not self._sample_is_confirming(sample, now_s):
            self._confirmation_count = 0
            self._last_confirmation_ts = None
            return {**current, "write_performed": False}
        sample_ts = float(sample["_ts"])
        if self._last_confirmation_ts is not None:
            gap_s = sample_ts - self._last_confirmation_ts
            if gap_s <= 0.0 or gap_s > self.max_sample_age_s:
                self._confirmation_count = 0
                self._last_confirmation_ts = None
        self._last_confirmation_ts = sample_ts
        self._confirmation_count += 1
        if self._confirmation_count < self.required_samples:
            return {
                **current,
                "evidence_state": "candidate",
                "reason": "awaiting_confirmation",
                "confirmation_samples": self._confirmation_count,
                "write_performed": False,
            }
        payload = {
            "schema_version": SCHEMA_VERSION,
            "topology_present": True,
            "valid": True,
            "source": TOPOLOGY_SOURCE,
            "evidence_state": "confirmed",
            "confirmation_samples": self.required_samples,
            "minimum_power_w": int(self.minimum_power_w),
            "confirmed_at": int(now_s),
        }
        written = _write_confirmed_state(self.state_path, payload)
        self._confirmation_count = 0
        self._last_confirmation_ts = None
        if written:
            self.write_count += 1
        return {**read_external_pv_topology(self.state_path), "write_performed": written}
