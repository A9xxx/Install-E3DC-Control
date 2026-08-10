"""Injizierbare Zeitbasis für hardwarekritische EMS-Schutzverträge.

Wallclock-Zeit bleibt ausschließlich Diagnose- und Korrelationszeit. Laufende
Schutzdauern werden innerhalb einer Prozessepoche aus ``monotonic``
fortgeschrieben. Ein Neustart, Bootwechsel oder unvollständiger persistierter
Zustand darf eine Schutzfrist niemals verkürzen; ein zeitgebundener Wächter
entscheidet dann konservativ und imputiert keine unbekannte Energie.

Das Modul besitzt kein Aktor- oder Dateischreib-I/O. Boot-ID und alternative
Zeitquellen sind vollständig injizierbar; ohne Injektion wird die Linux-Boot-ID
einmalig rein lesend gebunden.
"""

from __future__ import annotations

import math
import os
import time
import uuid
from copy import deepcopy
from typing import Any, Dict, Optional


SAMPLE_SCHEMA = "ems_control_time_sample_v1"
GUARD_SCHEMA = "ems_monotonic_guard_v1"
DEFAULT_PHASE_GUARD_S = 480.0
EPOCH_MODE_STRICT_PROCESS = "strict_process"
EPOCH_MODE_SAME_BOOT_MONOTONIC = "same_boot_monotonic"
EPOCH_MODES = frozenset({
    EPOCH_MODE_STRICT_PROCESS,
    EPOCH_MODE_SAME_BOOT_MONOTONIC,
})
_PROCESS_EPOCH = "%d:%s" % (os.getpid(), uuid.uuid4().hex)
_BOOT_ID: Optional[str] = None


def _finite_float(value: Any, default: Optional[float] = None) -> Optional[float]:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def process_epoch() -> str:
    """Liefert die für genau diesen Interpreter gültige Prozessepoche."""

    return _PROCESS_EPOCH


def system_boot_id(injected: Any = None) -> str:
    """Liefert injiziert oder einmalig rein lesend die aktuelle Boot-ID."""

    if injected is not None:
        return str(injected or "unknown")
    global _BOOT_ID
    if _BOOT_ID is None:
        try:
            with open(
                "/proc/sys/kernel/random/boot_id",
                "r",
                encoding="ascii",
            ) as handle:
                _BOOT_ID = handle.read().strip() or "unknown"
        except Exception:
            _BOOT_ID = "unknown"
    return _BOOT_ID


def sample(
    *,
    wall_ts: Any = None,
    monotonic_ts: Any = None,
    boot_id: Any = None,
    process_epoch_id: Any = None,
) -> Dict[str, Any]:
    """Erzeugt eine validierbare, in Tests vollständig injizierbare Probe."""

    wall = time.time() if wall_ts is None else _finite_float(wall_ts)
    monotonic = (
        time.monotonic()
        if monotonic_ts is None
        else _finite_float(monotonic_ts)
    )
    epoch = process_epoch() if process_epoch_id is None else str(process_epoch_id or "")
    boot = system_boot_id(boot_id)
    valid = bool(
        wall is not None
        and monotonic is not None
        and wall >= 0.0
        and monotonic >= 0.0
        and epoch
        and boot
    )
    return {
        "schema_version": SAMPLE_SCHEMA,
        "valid": valid,
        "wall_ts": float(wall or 0.0),
        "monotonic_ts": float(monotonic or 0.0),
        "boot_id": boot,
        "process_epoch": epoch,
    }


def elapsed_contract(
    previous: Any,
    current: Any,
    *,
    max_step_s: Any = None,
    epoch_mode: str = EPOCH_MODE_STRICT_PROCESS,
) -> Dict[str, Any]:
    """Bindet verstrichene Zeit ausschließlich an monotonic-Fortschritt.

    Ein unbekannter Prozessstillstand wird nicht als Energiezeit interpretiert.
    ``max_step_s`` dient genau dieser Unterscheidung; bei Überschreitung bleibt
    der bekannte Zustand erhalten und der Aufrufer muss fail-closed handeln.
    """

    before = previous if isinstance(previous, dict) else {}
    after = current if isinstance(current, dict) else {}
    blockers = []
    if before.get("schema_version") != SAMPLE_SCHEMA or before.get("valid") is not True:
        blockers.append("previous_time_sample_invalid")
    if after.get("schema_version") != SAMPLE_SCHEMA or after.get("valid") is not True:
        blockers.append("current_time_sample_invalid")
    if before.get("boot_id") != after.get("boot_id"):
        blockers.append("boot_epoch_changed")
    mode = str(epoch_mode or EPOCH_MODE_STRICT_PROCESS)
    if mode not in EPOCH_MODES:
        blockers.append("epoch_mode_invalid")
        mode = EPOCH_MODE_STRICT_PROCESS
    if (
        mode == EPOCH_MODE_STRICT_PROCESS
        and before.get("process_epoch") != after.get("process_epoch")
    ):
        blockers.append("process_epoch_changed")

    before_mono = _finite_float(before.get("monotonic_ts"))
    after_mono = _finite_float(after.get("monotonic_ts"))
    before_wall = _finite_float(before.get("wall_ts"))
    after_wall = _finite_float(after.get("wall_ts"))
    monotonic_delta = (
        after_mono - before_mono
        if before_mono is not None and after_mono is not None
        else None
    )
    wall_delta = (
        after_wall - before_wall
        if before_wall is not None and after_wall is not None
        else None
    )
    if monotonic_delta is None or monotonic_delta < 0.0:
        blockers.append("monotonic_regression")
    limit = _finite_float(max_step_s)
    if (
        limit is not None
        and limit >= 0.0
        and monotonic_delta is not None
        and monotonic_delta > limit
    ):
        blockers.append("unknown_process_gap")

    known = not blockers
    return {
        "schema_version": "ems_control_elapsed_v1",
        "known": known,
        "fail_closed": not known,
        "elapsed_s": float(max(0.0, monotonic_delta or 0.0)) if known else 0.0,
        "monotonic_delta_s": monotonic_delta,
        "wall_delta_s": wall_delta,
        "wallclock_jump_s": (
            wall_delta - monotonic_delta
            if wall_delta is not None and monotonic_delta is not None
            else None
        ),
        "blockers": blockers,
        "epoch_mode": mode,
    }


def begin_guard(
    duration_s: Any,
    current_sample: Any,
    *,
    minimum_s: Any = DEFAULT_PHASE_GUARD_S,
    epoch_mode: str = EPOCH_MODE_STRICT_PROCESS,
) -> Dict[str, Any]:
    """Bewaffnet eine monotonic gebundene Schutzfrist."""

    minimum_value = _finite_float(minimum_s, DEFAULT_PHASE_GUARD_S)
    duration_value = _finite_float(duration_s, DEFAULT_PHASE_GUARD_S)
    duration = max(
        0.0,
        float(
            DEFAULT_PHASE_GUARD_S
            if minimum_value is None
            else minimum_value
        ),
        float(
            DEFAULT_PHASE_GUARD_S
            if duration_value is None
            else duration_value
        ),
    )
    current = deepcopy(current_sample) if isinstance(current_sample, dict) else {}
    mode = str(epoch_mode or EPOCH_MODE_STRICT_PROCESS)
    mode_valid = mode in EPOCH_MODES
    if not mode_valid:
        mode = EPOCH_MODE_STRICT_PROCESS
    valid = bool(
        current.get("schema_version") == SAMPLE_SCHEMA
        and current.get("valid") is True
        and mode_valid
    )
    return {
        "schema_version": GUARD_SCHEMA,
        "active": bool(duration > 0.0 or not valid),
        "duration_s": duration,
        "remaining_s": duration,
        "sample": current,
        "valid": bool(valid),
        "fail_closed": not valid,
        "reason": "guard_armed" if valid else "guard_time_sample_invalid",
        "epoch_mode": mode,
    }


def evaluate_guard(
    guard: Any,
    current_sample: Any,
    *,
    minimum_s: Any = DEFAULT_PHASE_GUARD_S,
) -> Dict[str, Any]:
    """Fortschreibt oder konservativ neu bewaffnet eine Schutzfrist.

    Bei einer neuen Prozessepoche ist die Stillstandszeit nicht eindeutig an
    den alten Aktorzustand bindbar. Daher beginnt die volle Schutzdauer neu.
    Wallclock-Sprünge innerhalb derselben Prozessepoche beeinflussen die
    Restdauer dagegen nicht.
    """

    item = guard if isinstance(guard, dict) else {}
    current = deepcopy(current_sample) if isinstance(current_sample, dict) else {}
    minimum_value = _finite_float(minimum_s, DEFAULT_PHASE_GUARD_S)
    mode = str(item.get("epoch_mode") or EPOCH_MODE_STRICT_PROCESS)
    mode_valid = mode in EPOCH_MODES
    if not mode_valid:
        mode = EPOCH_MODE_STRICT_PROCESS
    duration_value = _finite_float(
        item.get("duration_s"),
        DEFAULT_PHASE_GUARD_S,
    )
    duration = max(
        0.0,
        float(
            DEFAULT_PHASE_GUARD_S
            if minimum_value is None
            else minimum_value
        ),
        float(
            DEFAULT_PHASE_GUARD_S
            if duration_value is None
            else duration_value
        ),
    )
    remaining = min(
        duration,
        max(0.0, float(_finite_float(item.get("remaining_s"), duration) or 0.0)),
    )
    previous_sample = item.get("sample") if isinstance(item.get("sample"), dict) else {}
    structurally_valid = bool(
        item.get("schema_version") == GUARD_SCHEMA
        and item.get("active") in (True, False)
        and previous_sample
        and mode_valid
    )
    elapsed = elapsed_contract(
        previous_sample,
        current,
        epoch_mode=mode,
    )

    if not structurally_valid or not elapsed.get("known"):
        reason = (
            "guard_state_incomplete"
            if not structurally_valid
            else str((elapsed.get("blockers") or ["guard_timebase_unbound"])[0])
        )
        return {
            "schema_version": GUARD_SCHEMA,
            "active": True,
            "duration_s": duration,
            "remaining_s": duration,
            "sample": current,
            "valid": bool(current.get("valid") is True),
            "fail_closed": True,
            "rearmed": True,
            "reason": reason,
            "elapsed": elapsed,
            "epoch_mode": mode,
        }

    remaining = max(0.0, remaining - float(elapsed.get("elapsed_s", 0.0) or 0.0))
    return {
        "schema_version": GUARD_SCHEMA,
        "active": remaining > 0.0,
        "duration_s": duration,
        "remaining_s": remaining,
        "sample": current,
        "valid": True,
        "fail_closed": False,
        "rearmed": False,
        "reason": "guard_active" if remaining > 0.0 else "guard_elapsed",
        "elapsed": elapsed,
        "epoch_mode": mode,
    }


__all__ = [
    "DEFAULT_PHASE_GUARD_S",
    "EPOCH_MODE_SAME_BOOT_MONOTONIC",
    "EPOCH_MODE_STRICT_PROCESS",
    "EPOCH_MODES",
    "GUARD_SCHEMA",
    "SAMPLE_SCHEMA",
    "begin_guard",
    "elapsed_contract",
    "evaluate_guard",
    "process_epoch",
    "sample",
    "system_boot_id",
]
