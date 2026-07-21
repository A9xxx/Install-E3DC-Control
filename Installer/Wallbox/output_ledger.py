"""Fail-closed-Ledger für einen logischen Wallboxausgang pro Managerzyklus.

Das Ledger bleibt bewusst im Arbeitsspeicher. Es entscheidet nicht, *was*
gesendet wird, sondern schützt nur die finale CommandExecutor-Kante, nachdem
Policy, Sequenzierung und Befehls-Gates zugestimmt haben. Eine Freigabe wird
vor dem Treiber-I/O verbraucht, weil ein Gerät auch bei Timeout oder Exception
reagiert haben kann.
"""

from copy import deepcopy
from typing import Any, Dict


LEDGER_KEY = "_wallbox_output_ledger"
AUDIT_KEY = "_wallbox_output_audit"


def begin_cycle(state: Dict[str, Any], token: Any, *, wb_id: Any = 0) -> Dict[str, Any]:
    """Öffnet einen unabhängigen Ausgangsslot für eine Wallbox im äußeren Zyklus."""

    data = state if isinstance(state, dict) else {}
    token_text = str(token or "").strip()
    current = data.get(LEDGER_KEY)
    if (
        isinstance(current, dict)
        and str(current.get("cycle_token") or "") == token_text
        and current.get("token_valid") is True
    ):
        return deepcopy(current)
    ledger = {
        "schema": "wallbox_output_cycle_v1",
        "cycle_token": token_text,
        "wb_id": int(wb_id or data.get("id", 0) or 0),
        "normal_claimed": False,
        "emergency_claimed": False,
        "normal_locked": False,
        "last_method": "",
        "last_reason": "",
        "blocked_count": 0,
        "token_valid": bool(token_text),
    }
    data[LEDGER_KEY] = ledger
    return deepcopy(ledger)


def claim(
    state: Dict[str, Any],
    *,
    method: Any,
    reason: Any = "",
    emergency: bool = False,
) -> Dict[str, Any]:
    """Belegt den logischen Ausgangsslot unmittelbar vor dem Treiberaufruf."""

    data = state if isinstance(state, dict) else {}
    ledger = data.get(LEDGER_KEY)
    method_text = str(method or "driver_command")
    reason_text = str(reason or method_text)
    if (
        not isinstance(ledger, dict)
        or not str(ledger.get("cycle_token") or "").strip()
        or ledger.get("token_valid") is not True
    ):
        audit = data.get(AUDIT_KEY)
        audit = list(audit) if isinstance(audit, list) else []
        audit.append({
            "cycle_token": "",
            "wb_id": int(data.get("id", 0) or 0),
            "method": method_text,
            "reason": reason_text,
            "emergency": bool(emergency),
            "allowed": False,
            "blocker": "missing_cycle_token",
        })
        data[AUDIT_KEY] = audit[-32:]
        return {
            "allowed": False,
            "blocker": "missing_cycle_token",
            "ledger": deepcopy(ledger) if isinstance(ledger, dict) else {},
        }
    allowed = False
    blocker = ""
    if emergency:
        if bool(ledger.get("emergency_claimed")):
            blocker = "emergency_already_claimed"
        else:
            allowed = True
            ledger["emergency_claimed"] = True
            ledger["normal_locked"] = True
    elif bool(ledger.get("normal_locked")) or bool(ledger.get("emergency_claimed")):
        blocker = "normal_blocked_after_emergency"
    elif bool(ledger.get("normal_claimed")):
        blocker = "normal_already_claimed"
    else:
        allowed = True
        ledger["normal_claimed"] = True

    if not allowed:
        ledger["blocked_count"] = int(ledger.get("blocked_count", 0) or 0) + 1
    ledger["last_method"] = method_text
    ledger["last_reason"] = reason_text
    data[LEDGER_KEY] = ledger
    audit = data.get(AUDIT_KEY)
    audit = list(audit) if isinstance(audit, list) else []
    audit.append({
        "cycle_token": str(ledger.get("cycle_token") or ""),
        "wb_id": int(ledger.get("wb_id", 0) or 0),
        "method": method_text,
        "reason": reason_text,
        "emergency": bool(emergency),
        "allowed": bool(allowed),
        "blocker": blocker,
    })
    data[AUDIT_KEY] = audit[-32:]
    return {
        "allowed": bool(allowed),
        "blocker": blocker,
        "ledger": deepcopy(ledger),
    }


__all__ = ["AUDIT_KEY", "LEDGER_KEY", "begin_cycle", "claim"]
