"""Reiner Offer-Demand-Rückmeldevertrag der Wallbox-Regelung.

Dieses Modul bewertet ausschließlich frische Mess- und Fähigkeitsbelege. Es
verteilt kein Budget, berechnet keinen Aktorauftrag und sendet niemals einen
Hardwarebefehl. Ein ungültiger oder zeitlich nicht gebundener Messstand bleibt
``valid=False`` und darf insbesondere keinen erfundenen Mehrbedarf erzeugen.
"""

from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Any, Dict, Mapping, Optional, Tuple


DEFAULT_STATUS_MAX_AGE_S = 15.0
DEFAULT_UNDER_ACCEPTANCE_CONFIRM_S = 12.0
DEFAULT_UNUSED_BUDGET_DECAY_S = 12.0


class DemandState(str, Enum):
    WANTS_MORE = "WANTS_MORE"
    SATISFIED = "SATISFIED"
    IDLE = "IDLE"


def _safe_float(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
    except (TypeError, ValueError):
        return None
    return result if math.isfinite(result) else None


def _non_negative_float(value: Any) -> Optional[float]:
    result = _safe_float(value)
    return result if result is not None and result >= 0.0 else None


def _positive_float(value: Any) -> Optional[float]:
    result = _safe_float(value)
    return result if result is not None and result > 0.0 else None


def _bounded_seconds(value: Any, default: float) -> float:
    seconds = _safe_float(value)
    if seconds is None:
        seconds = float(default)
    return min(15.0, max(10.0, seconds))


def _mapping(value: Any) -> Mapping[str, Any]:
    if isinstance(value, Mapping):
        return value
    as_dict = getattr(value, "as_dict", None)
    if callable(as_dict):
        projected = as_dict()
        if isinstance(projected, Mapping):
            return projected
    return {}


def _status_age_s(status: Mapping[str, Any], now_ts: float) -> Optional[float]:
    direct_age = _non_negative_float(status.get("driver_status_age_s"))
    if direct_age is not None:
        return direct_age
    for key in ("driver_status_last_ok_ts", "driver_status_last_sample_ts"):
        observed_ts = _positive_float(status.get(key))
        if observed_ts is not None and now_ts >= observed_ts:
            return now_ts - observed_ts
    return None


def _freshness_blocker(
    status: Mapping[str, Any],
    *,
    now_ts: float,
    max_age_s: float,
) -> Tuple[str, Optional[float]]:
    if status.get("driver_status_valid") is not True:
        return "driver_status_invalid", _status_age_s(status, now_ts)
    if status.get("driver_status_stale") is True:
        return "driver_status_stale", _status_age_s(status, now_ts)
    if status.get("driver_status_degraded") is True:
        return "driver_status_degraded", _status_age_s(status, now_ts)
    if status.get("driver_status_glitch") is True:
        return "driver_status_glitch", _status_age_s(status, now_ts)
    if status.get("driver_status_plausible") is False:
        return "driver_status_implausible", _status_age_s(status, now_ts)
    age_s = _status_age_s(status, now_ts)
    if age_s is None:
        return "driver_status_age_unproven", None
    if age_s > max_age_s:
        return "driver_status_too_old", age_s
    return "", age_s


def _connected(status: Mapping[str, Any]) -> bool:
    try:
        car_state = int(status.get("car", 0) or 0)
    except (TypeError, ValueError):
        car_state = 0
    return bool(
        status.get("plug_state") is True
        or status.get("plug") is True
        or status.get("connected") is True
        or car_state == 2
    )


def _charging(status: Mapping[str, Any]) -> bool:
    charge_contract = status.get("charge_contract")
    if isinstance(charge_contract, Mapping):
        truth = str(charge_contract.get("truth") or "").strip().lower()
        if truth:
            return truth == "charging"
    truth = str(status.get("charge_truth") or "").strip().lower()
    if truth:
        return truth == "charging"
    return bool(status.get("charging") is True or status.get("charge_state") is True)


def _offered_current_a(status: Mapping[str, Any]) -> Optional[float]:
    values = [
        _non_negative_float(status.get(key))
        for key in ("offered_current_raw", "offered_current", "evse_current", "amp")
    ]
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _accepted_current_a(status: Mapping[str, Any]) -> Optional[float]:
    explicit = _non_negative_float(status.get("accepted_current_a"))
    if explicit is not None:
        return explicit
    phase_values = [
        _non_negative_float(status.get(f"phase_current_l{phase}_a"))
        for phase in (1, 2, 3)
    ]
    present = [value for value in phase_values if value is not None]
    return max(present) if present else None


def _session_key(status: Mapping[str, Any]) -> str:
    return str(
        status.get("plug_session_id")
        or status.get("session_id")
        or status.get("charge_session_id")
        or ""
    ).strip()


@dataclass(frozen=True)
class WallboxDemandContract:
    """Typisierte, rein lesende Rückmeldung an einen späteren Budgetverteiler."""

    schema_version: str = "wallbox_demand_contract_v1"
    state: str = DemandState.IDLE.value
    valid: bool = False
    blocker: str = "not_evaluated"
    reason: str = "not_evaluated"
    connected: bool = False
    charging: bool = False
    session_key: str = ""
    status_age_s: Optional[float] = None
    offered_current_a: Optional[float] = None
    accepted_current_a: Optional[float] = None
    min_current_a: Optional[float] = None
    effective_max_current_a: Optional[float] = None
    current_step_a: Optional[float] = None
    start_offer_active: bool = False
    start_offer_bound: bool = False
    start_offer_source: str = ""
    start_offer_session_key: str = ""
    at_confirmed_limit: bool = False
    confirmed_limit_source: str = ""
    under_acceptance_candidate_since_ts: Optional[float] = None
    under_acceptance_stable: bool = False
    under_acceptance_confirm_s: float = DEFAULT_UNDER_ACCEPTANCE_CONFIRM_S
    satisfied_since_ts: Optional[float] = None
    unused_budget_decay_s: float = DEFAULT_UNUSED_BUDGET_DECAY_S
    unused_budget_release_at_ts: Optional[float] = None
    unused_budget_releasable: bool = False
    observed_ts: float = 0.0
    output_semantics: str = "feedback_only"
    hardware_output_allowed: bool = False

    def __post_init__(self):
        state = str(self.state or "").strip().upper()
        valid_states = {item.value for item in DemandState}
        if state not in valid_states:
            state = DemandState.IDLE.value
            object.__setattr__(self, "valid", False)
            object.__setattr__(self, "blocker", "demand_state_invalid")
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "session_key", str(self.session_key or ""))
        object.__setattr__(self, "status_age_s", _non_negative_float(self.status_age_s))
        object.__setattr__(
            self,
            "offered_current_a",
            _non_negative_float(self.offered_current_a),
        )
        object.__setattr__(
            self,
            "accepted_current_a",
            _non_negative_float(self.accepted_current_a),
        )
        object.__setattr__(self, "min_current_a", _positive_float(self.min_current_a))
        object.__setattr__(
            self,
            "effective_max_current_a",
            _positive_float(self.effective_max_current_a),
        )
        object.__setattr__(self, "current_step_a", _positive_float(self.current_step_a))
        object.__setattr__(
            self,
            "start_offer_source",
            str(self.start_offer_source or "").strip(),
        )
        object.__setattr__(
            self,
            "start_offer_session_key",
            str(self.start_offer_session_key or "").strip(),
        )
        object.__setattr__(
            self,
            "under_acceptance_candidate_since_ts",
            _positive_float(self.under_acceptance_candidate_since_ts),
        )
        object.__setattr__(
            self,
            "satisfied_since_ts",
            _positive_float(self.satisfied_since_ts),
        )
        object.__setattr__(
            self,
            "under_acceptance_confirm_s",
            _bounded_seconds(
                self.under_acceptance_confirm_s,
                DEFAULT_UNDER_ACCEPTANCE_CONFIRM_S,
            ),
        )
        object.__setattr__(
            self,
            "unused_budget_decay_s",
            _bounded_seconds(
                self.unused_budget_decay_s,
                DEFAULT_UNUSED_BUDGET_DECAY_S,
            ),
        )
        object.__setattr__(
            self,
            "unused_budget_release_at_ts",
            _positive_float(self.unused_budget_release_at_ts),
        )
        object.__setattr__(
            self,
            "observed_ts",
            _non_negative_float(self.observed_ts) or 0.0,
        )
        object.__setattr__(self, "output_semantics", "feedback_only")
        object.__setattr__(self, "hardware_output_allowed", False)

    @classmethod
    def from_mapping(cls, value: Optional[Mapping[str, Any]] = None):
        data = value if isinstance(value, Mapping) else {}
        return cls(
            state=data.get("state", DemandState.IDLE.value),
            valid=bool(data.get("valid", False)),
            blocker=data.get("blocker", "not_evaluated"),
            reason=data.get("reason", "not_evaluated"),
            connected=bool(data.get("connected", False)),
            charging=bool(data.get("charging", False)),
            session_key=data.get("session_key", ""),
            status_age_s=data.get("status_age_s"),
            offered_current_a=data.get("offered_current_a"),
            accepted_current_a=data.get("accepted_current_a"),
            min_current_a=data.get("min_current_a"),
            effective_max_current_a=data.get("effective_max_current_a"),
            current_step_a=data.get("current_step_a"),
            start_offer_active=bool(data.get("start_offer_active", False)),
            start_offer_bound=bool(data.get("start_offer_bound", False)),
            start_offer_source=data.get("start_offer_source", ""),
            start_offer_session_key=data.get("start_offer_session_key", ""),
            at_confirmed_limit=bool(data.get("at_confirmed_limit", False)),
            confirmed_limit_source=data.get("confirmed_limit_source", ""),
            under_acceptance_candidate_since_ts=data.get(
                "under_acceptance_candidate_since_ts"
            ),
            under_acceptance_stable=bool(
                data.get("under_acceptance_stable", False)
            ),
            under_acceptance_confirm_s=data.get(
                "under_acceptance_confirm_s",
                DEFAULT_UNDER_ACCEPTANCE_CONFIRM_S,
            ),
            satisfied_since_ts=data.get("satisfied_since_ts"),
            unused_budget_decay_s=data.get(
                "unused_budget_decay_s",
                DEFAULT_UNUSED_BUDGET_DECAY_S,
            ),
            unused_budget_release_at_ts=data.get("unused_budget_release_at_ts"),
            unused_budget_releasable=bool(
                data.get("unused_budget_releasable", False)
            ),
            observed_ts=_non_negative_float(data.get("observed_ts")) or 0.0,
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "state": self.state,
            "valid": self.valid,
            "blocker": self.blocker,
            "reason": self.reason,
            "connected": self.connected,
            "charging": self.charging,
            "session_key": self.session_key,
            "status_age_s": self.status_age_s,
            "offered_current_a": self.offered_current_a,
            "accepted_current_a": self.accepted_current_a,
            "min_current_a": self.min_current_a,
            "effective_max_current_a": self.effective_max_current_a,
            "current_step_a": self.current_step_a,
            "start_offer_active": self.start_offer_active,
            "start_offer_bound": self.start_offer_bound,
            "start_offer_source": self.start_offer_source,
            "start_offer_session_key": self.start_offer_session_key,
            "at_confirmed_limit": self.at_confirmed_limit,
            "confirmed_limit_source": self.confirmed_limit_source,
            "under_acceptance_candidate_since_ts": self.under_acceptance_candidate_since_ts,
            "under_acceptance_stable": self.under_acceptance_stable,
            "under_acceptance_confirm_s": self.under_acceptance_confirm_s,
            "satisfied_since_ts": self.satisfied_since_ts,
            "unused_budget_decay_s": self.unused_budget_decay_s,
            "unused_budget_release_at_ts": self.unused_budget_release_at_ts,
            "unused_budget_releasable": self.unused_budget_releasable,
            "observed_ts": self.observed_ts,
            "output_semantics": self.output_semantics,
            "hardware_output_allowed": self.hardware_output_allowed,
        }


def evaluate_wallbox_demand(
    status: Optional[Mapping[str, Any]],
    capability: Any = None,
    *,
    previous: Optional[Mapping[str, Any]] = None,
    now_ts: Optional[float] = None,
    max_status_age_s: float = DEFAULT_STATUS_MAX_AGE_S,
    under_acceptance_confirm_s: float = DEFAULT_UNDER_ACCEPTANCE_CONFIRM_S,
    unused_budget_decay_s: float = DEFAULT_UNUSED_BUDGET_DECAY_S,
    confirmed_limit_reached: bool = False,
    confirmed_limit_source: str = "",
    start_offer_active: bool = False,
    start_offer_source: str = "",
    start_offer_session_key: str = "",
) -> WallboxDemandContract:
    """Bewerte Bedarf, Begrenzung und Decay ausschließlich aus Evidenz.

    ``previous`` ist der vorherige Vertrag derselben Stecksession und dient
    nur der zeitlichen Bestätigung. Die Funktion mutiert weder diesen Zustand
    noch eine Wallbox.
    """

    st = status if isinstance(status, Mapping) else {}
    cap = _mapping(capability)
    prev = previous if isinstance(previous, Mapping) else {}
    now_value = time.time() if now_ts is None else _safe_float(now_ts)
    now = now_value if now_value is not None and now_value >= 0.0 else time.time()
    status_limit_value = _positive_float(max_status_age_s)
    status_limit_s = max(1.0, status_limit_value or DEFAULT_STATUS_MAX_AGE_S)
    confirm_s = _bounded_seconds(
        under_acceptance_confirm_s,
        DEFAULT_UNDER_ACCEPTANCE_CONFIRM_S,
    )
    decay_s = _bounded_seconds(
        unused_budget_decay_s,
        DEFAULT_UNUSED_BUDGET_DECAY_S,
    )
    blocker, age_s = _freshness_blocker(
        st,
        now_ts=now,
        max_age_s=status_limit_s,
    )
    session = _session_key(st)
    connected = _connected(st)
    charging = _charging(st)
    offered = _offered_current_a(st)
    accepted = _accepted_current_a(st)
    min_current = _positive_float(cap.get("min_current_a"))
    effective_max = _positive_float(
        cap.get("effective_max_current_a", cap.get("max_current_a"))
    )
    current_step = _positive_float(cap.get("current_step_a"))

    def contract(**overrides: Any) -> WallboxDemandContract:
        values = {
            "state": DemandState.IDLE.value,
            "valid": not bool(blocker),
            "blocker": blocker,
            "reason": blocker or "idle",
            "connected": connected,
            "charging": charging,
            "session_key": session,
            "status_age_s": age_s,
            "offered_current_a": offered,
            "accepted_current_a": accepted,
            "min_current_a": min_current,
            "effective_max_current_a": effective_max,
            "current_step_a": current_step,
            "start_offer_active": bool(start_offer_active),
            "start_offer_source": str(start_offer_source or "").strip(),
            "start_offer_session_key": str(start_offer_session_key or "").strip(),
            "under_acceptance_confirm_s": confirm_s,
            "unused_budget_decay_s": decay_s,
            "observed_ts": now,
        }
        values.update(overrides)
        return WallboxDemandContract(**values)

    if blocker:
        return contract(valid=False)
    if not connected:
        return contract(reason="vehicle_disconnected")
    if not charging:
        if not start_offer_active:
            return contract(reason="connected_standby")
        bound_source = str(start_offer_source or "").strip()
        bound_session = str(start_offer_session_key or "").strip()
        if not bound_source:
            return contract(
                valid=False,
                blocker="start_offer_source_missing",
                reason="start_offer_source_missing",
            )
        if not session or not bound_session or bound_session != session:
            return contract(
                valid=False,
                blocker="start_offer_session_mismatch",
                reason="start_offer_session_mismatch",
            )
        if min_current is None:
            return contract(
                valid=False,
                blocker="start_offer_min_current_unproven",
                reason="start_offer_min_current_unproven",
            )
        if offered is None or offered + 1e-9 < min_current:
            return contract(
                valid=False,
                blocker="start_offer_below_confirmed_minimum",
                reason="start_offer_below_confirmed_minimum",
            )
        return contract(
            state=DemandState.WANTS_MORE.value,
            valid=True,
            blocker="",
            reason="bound_start_offer_active",
            start_offer_bound=True,
        )
    if confirmed_limit_reached and not str(confirmed_limit_source or "").strip():
        return contract(
            valid=False,
            blocker="confirmed_limit_source_missing",
            reason="confirmed_limit_source_missing",
        )
    if offered is None:
        return contract(
            valid=False,
            blocker="offered_current_unproven",
            reason="offered_current_unproven",
        )

    previous_is_contract = bool(
        str(prev.get("schema_version") or "") == "wallbox_demand_contract_v1"
        and prev.get("valid") is True
        and str(prev.get("state") or "")
        in {
            DemandState.WANTS_MORE.value,
            DemandState.SATISFIED.value,
            DemandState.IDLE.value,
        }
    )
    same_session = bool(
        previous_is_contract
        and session
        and str(prev.get("session_key") or "") == session
        and _non_negative_float(prev.get("observed_ts")) is not None
        and now >= float(prev.get("observed_ts", 0.0) or 0.0)
    )
    tolerance_a = max(0.2, (current_step or 1.0) * 0.5)
    at_capability_limit = bool(
        effective_max is not None
        and offered >= effective_max - tolerance_a
    )
    target_limit = bool(confirmed_limit_reached)
    at_confirmed_limit = bool(target_limit or at_capability_limit)
    limit_source = ""
    if target_limit:
        limit_source = str(confirmed_limit_source).strip()
    elif at_capability_limit:
        limit_source = (
            "vehicle_max_current"
            if _positive_float(cap.get("vehicle_max_current_a")) is not None
            and effective_max == _positive_float(cap.get("vehicle_max_current_a"))
            else "infrastructure_max_current"
        )

    under_candidate = False
    candidate_since = None
    under_stable = False
    if accepted is not None and accepted > 0.2:
        under_gap_a = offered - accepted
        under_candidate = bool(
            offered >= 5.5
            and under_gap_a >= max(1.0, current_step or 1.0)
        )
        if under_candidate and session:
            previous_since = _positive_float(
                prev.get("under_acceptance_candidate_since_ts")
            )
            if same_session and previous_since is not None:
                candidate_since = previous_since
            else:
                candidate_since = now
            under_stable = bool(now - candidate_since >= confirm_s)

    if accepted is None and not at_confirmed_limit:
        return contract(
            valid=False,
            blocker="accepted_current_unproven",
            reason="accepted_current_unproven",
        )
    if accepted is not None and accepted <= 0.2 and not at_confirmed_limit:
        return contract(
            valid=False,
            blocker="accepted_current_inconsistent",
            reason="accepted_current_inconsistent",
        )

    satisfied = bool(at_confirmed_limit or under_stable)
    if not satisfied:
        return contract(
            state=DemandState.WANTS_MORE.value,
            valid=True,
            blocker="",
            reason=(
                "under_acceptance_confirmation_pending"
                if under_candidate
                else "charging_accepts_more"
            ),
            under_acceptance_candidate_since_ts=candidate_since,
            under_acceptance_stable=False,
        )

    previous_satisfied_since = _positive_float(prev.get("satisfied_since_ts"))
    if (
        same_session
        and str(prev.get("state") or "") == DemandState.SATISFIED.value
        and prev.get("valid") is True
        and previous_satisfied_since is not None
    ):
        satisfied_since = previous_satisfied_since
    else:
        satisfied_since = now
    release_at = satisfied_since + decay_s
    releasable = bool(now >= release_at)
    reason = (
        "confirmed_limit_reached"
        if at_confirmed_limit
        else "stable_vehicle_under_acceptance"
    )
    return contract(
        state=DemandState.SATISFIED.value,
        valid=True,
        blocker="",
        reason=reason,
        at_confirmed_limit=at_confirmed_limit,
        confirmed_limit_source=limit_source,
        under_acceptance_candidate_since_ts=candidate_since,
        under_acceptance_stable=under_stable,
        satisfied_since_ts=satisfied_since,
        unused_budget_release_at_ts=release_at,
        unused_budget_releasable=releasable,
    )


__all__ = [
    "DEFAULT_STATUS_MAX_AGE_S",
    "DEFAULT_UNDER_ACCEPTANCE_CONFIRM_S",
    "DEFAULT_UNUSED_BUDGET_DECAY_S",
    "DemandState",
    "WallboxDemandContract",
    "evaluate_wallbox_demand",
]
