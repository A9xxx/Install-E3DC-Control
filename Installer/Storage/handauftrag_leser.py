"""Gemeinsame Zeit- und Statusleser für Speicheraufträge."""
import math
from typing import Any, Dict, List, Optional
from .leistungsleser import safe_float

def manual_override_max_age_s(cfg: Dict[str, Any]) -> int:
    hours = safe_float(cfg.get("storage_manual_override_max_age_h"), 12.0)
    return int(max(1.0, min(24.0, hours)) * 3600)


def manual_override_expired(
    manual_override: Dict[str, Any],
    now_s: Optional[float] = None,
    max_age_s: Optional[float] = None,
) -> bool:
    if not manual_override:
        return False
    now = float(now_s)
    deadline = manual_override_deadline(manual_override, now, max_age_s)
    return deadline is not None and now >= deadline


def manual_override_deadline(manual_override, now, max_age_s):
    deadlines: List[float] = []
    expires_ts = safe_float(manual_override.get("expires_ts"), 0.0)
    if expires_ts > 0:
        deadlines.append(expires_ts)
    if max_age_s is not None:
        ts = safe_float(manual_override.get("ts"), safe_float(manual_override.get("created_ts"), 0.0))
        if ts <= 0 or ts > now + 300.0:
            return now
        deadlines.append(ts + max(1.0, float(max_age_s)))
    if not deadlines:
        return None
    return min(deadlines)


def storage_emergency_status(live: Dict[str, Any]) -> Optional[int]:
    """Gemeldeter Status; 0 und 3 sind gültig, fehlend ist kein Normalbetrieb."""
    if live.get("ems_emergency_power_status_valid") is False:
        return None
    value = live.get("ems_emergency_power_status", live.get("Notstrom_Status"))
    if isinstance(value, bool):
        return None
    number = safe_float(value, float("nan"))
    return int(number) if math.isfinite(number) and number.is_integer() and 0 <= number <= 4 else None

