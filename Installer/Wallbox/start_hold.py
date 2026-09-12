"""Energie- und zeitgebundener Start-Haltevertrag für Wallboxen.

Das Modul besitzt weder Hardware- noch Datei-I/O. Der Wallbox Manager meldet
eine sitzungsgebundene Mindestleistungsreservierung an; ausschließlich der
Storage Manager darf daraus ein vollständig finanziertes Wattbudget freigeben.
Die gemeinsame 40-Wh-Grenze begrenzt die zusätzliche Energie und wird deshalb
bei mehreren Wallboxen nicht vervielfacht. Nur ein ausdrücklich typisierter
Erkennungsstart kann eine zusätzliche begrenzte Netzfreigabe tragen.
"""

from __future__ import annotations

from copy import deepcopy
import math
import uuid

try:
    from Installer import control_time
except ModuleNotFoundError:  # Native Ausführung mit Installer im sys.path
    import control_time  # type: ignore


REQUEST_SCHEMA = "wallbox_start_hold_request_v1"
GRANT_SCHEMA = "wallbox_start_hold_grants_v1"
STATE_KEY = "_wallbox_start_hold_request"
GRANT_KEY = "_wallbox_start_hold_grant"
SESSION_KEY = "_wallbox_start_hold_session_id"
CONSUMED_SESSION_KEY = "_wallbox_start_hold_consumed_session_id"
DISCONNECT_SEEN_KEY = "_wallbox_start_hold_disconnect_seen"
DEFAULT_ARM_S = 30.0
DEFAULT_HOLD_S = 180.0
DEFAULT_ENERGY_WH = 40.0
DEFAULT_HARD_IMPORT_W = 2500.0
ACTIVE_STAGES = frozenset({"await_receipt", "committed"})
FIXED_DETECTION_KIND = "fixed_phase_detection"


def _fixed_detection_metadata(item):
    """Validiert ausschließlich die private, bereits reservierte Probeenergie."""
    if not isinstance(item, dict) or item.get("request_kind") != FIXED_DETECTION_KIND:
        return None
    numbers = {}
    for key in ("bridge_remaining_wh", "bridge_remaining_s", "minimum_real_pv_w"):
        value = item.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
            return None
        numbers[key] = float(value)
    used = item.get("bridge_used_wh", 0.0)
    if isinstance(used, bool) or not isinstance(used, (int, float)) or not math.isfinite(float(used)):
        return None
    if (not str(item.get("bridge_reservation_id") or "")
        or item.get("grid_support_allowed") is not True
        or not 0.0 < numbers["bridge_remaining_s"] <= 30.0
        or not 0.0 < numbers["bridge_remaining_wh"] <= DEFAULT_ENERGY_WH
        or not 0.0 <= float(used) <= DEFAULT_ENERGY_WH
        or numbers["bridge_remaining_wh"] + float(used) > DEFAULT_ENERGY_WH + 1e-6
        or numbers["minimum_real_pv_w"] != 1380.0):
        return None
    return {"request_kind": FIXED_DETECTION_KIND,
            "bridge_reservation_id": str(item["bridge_reservation_id"]),
            "grid_support_allowed": True, "bridge_used_wh": float(used), **numbers}


def _fixed_grant_matches(request, grant):
    if request.get("request_kind") != FIXED_DETECTION_KIND:
        return grant.get("request_kind") != FIXED_DETECTION_KIND
    meta = _fixed_detection_metadata(request)
    return bool(meta and grant.get("request_kind") == FIXED_DETECTION_KIND
                and grant.get("bridge_reservation_id") == meta["bridge_reservation_id"]
                and 0.0 <= _float(grant.get("grid_support_w"), -1.0)
                and 0.0 < _float(grant.get("bridge_remaining_s"), 0.0) <= 30.0
                and 0.0 < _float(grant.get("bridge_remaining_wh"), 0.0) <= DEFAULT_ENERGY_WH)



def _float(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return float(default)
    return result if math.isfinite(result) else float(default)


def _int(value, default=0):
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return int(default)


def _current_session_id(data):
    box = data if isinstance(data, dict) else {}
    return str(
        box.get(SESSION_KEY)
        or box.get("_openwb_pro_plug_session_id")
        or ""
    )


def _normalize_group_members(items, *, allow_two_phase=False, allow_running_current=False):
    """Normalisiert die beim Start belegte Wallbox-/Stecksession-Gruppe.

    Eine typisierte Probe darf die bereits laufenden Ströme anderer Mitglieder
    binden. Der Probeowner selbst bleibt durch Request und Intent exakt 6 A.
    """

    result = []
    seen = set()
    for raw in items if isinstance(items, list) else []:
        if not isinstance(raw, dict):
            return []
        wb_id = max(0, _int(raw.get("wb_id"), 0))
        session_id = str(raw.get("plug_session_id") or "")
        phases = _int(raw.get("target_phases"), 0)
        minimum_amp = _int(raw.get("minimum_amp"), 0)
        minimum_power_w = max(0, _int(raw.get("minimum_power_w"), 0))
        nominal_minimum_power_w = minimum_amp * 230 * phases
        if (
            wb_id <= 0
            or wb_id in seen
            or not session_id
            or phases not in ((1, 2, 3) if allow_two_phase else (1, 3))
            or not (6 <= minimum_amp <= 32 if allow_running_current else minimum_amp == 6)
            or minimum_power_w < nominal_minimum_power_w
        ):
            return []
        seen.add(wb_id)
        result.append({
            "wb_id": wb_id,
            "plug_session_id": session_id,
            "target_phases": phases,
            "minimum_amp": minimum_amp,
            "minimum_power_w": minimum_power_w,
        })
    return sorted(result, key=lambda item: item["wb_id"])


def _active_request(request, now_ts):
    item = request if isinstance(request, dict) else {}
    return bool(
        item.get("schema_version") == REQUEST_SCHEMA
        and str(item.get("stage") or "") in ACTIVE_STAGES
        and str(item.get("reservation_id") or "")
        and str(item.get("plug_session_id") or "")
        and _float(item.get("expires_ts"), 0.0) > float(now_ts)
    )


def begin_request(
    box,
    *,
    wb_id,
    plug_session_id,
    request_cycle_token,
    group_id,
    group_minimum_w,
    minimum_quantum_w,
    group_members=None,
    target_phases=1,
    minimum_amp=6,
    now_ts=0.0,
    arm_s=DEFAULT_ARM_S,
    request_kind="",
):
    """Erzeugt höchstens einen Startversuch je physischer Stecksession.

    Ein abgelaufener Versuch derselben Session wird absichtlich nicht erneuert.
    Erst eine neue, vom Manager bestätigte Stecksession darf neu anfragen.
    """

    data = box if isinstance(box, dict) else {}
    now_value = _float(now_ts, 0.0)
    session_id = str(plug_session_id or "")
    existing = data.get(STATE_KEY)
    if isinstance(existing, dict):
        if str(existing.get("plug_session_id") or "") == session_id:
            return deepcopy(existing)
        data.pop(STATE_KEY, None)
        data.pop(GRANT_KEY, None)

    phases = _int(target_phases, 0)
    amp = _int(minimum_amp, 0)
    quantum_w = max(0, _int(minimum_quantum_w, 0))
    required_w = max(0, _int(group_minimum_w, 0))
    charger_id = max(0, _int(wb_id, 0))
    normalized_members = _normalize_group_members(group_members, allow_two_phase=request_kind == FIXED_DETECTION_KIND, allow_running_current=request_kind == FIXED_DETECTION_KIND)
    provided_group_members_invalid = bool(
        group_members is not None and not normalized_members
    )
    if not normalized_members:
        normalized_members = [{
            "wb_id": charger_id,
            "plug_session_id": session_id,
            "target_phases": phases,
            "minimum_amp": amp,
            "minimum_power_w": quantum_w,
        }]
    selected_member = next(
        (
            item
            for item in normalized_members
            if item.get("wb_id") == charger_id
        ),
        None,
    )
    if (
        charger_id <= 0
        or not session_id
        or not str(request_cycle_token or "")
        or phases not in ((1, 2, 3) if request_kind == FIXED_DETECTION_KIND else (1, 3))
        or amp != 6
        or quantum_w < amp * 230 * phases
        or required_w < quantum_w
        or now_value <= 0.0
        or provided_group_members_invalid
        or not isinstance(selected_member, dict)
        or str(selected_member.get("plug_session_id") or "") != session_id
        or _int(selected_member.get("target_phases"), 0) != phases
        or _int(selected_member.get("minimum_amp"), 0) != amp
        or _int(selected_member.get("minimum_power_w"), 0) != quantum_w
    ):
        return {}

    request = {
        "schema_version": REQUEST_SCHEMA,
        "active": True,
        "stage": "await_receipt",
        "reservation_id": uuid.uuid4().hex,
        "group_id": str(group_id or request_cycle_token),
        "wb_id": charger_id,
        "plug_session_id": session_id,
        "request_cycle_token": str(request_cycle_token),
        "target_phases": phases,
        "minimum_amp": amp,
        "minimum_quantum_w": quantum_w,
        "group_minimum_w": required_w,
        "group_members": normalized_members,
        "created_ts": now_value,
        "started_ts": 0.0,
        "expires_ts": now_value + max(5.0, min(60.0, _float(arm_s, DEFAULT_ARM_S))),
        "receipt_reservation_id": "",
        "receipt_cycle_token": "",
        "receipt_method": "",
        "receipt_amp": 0.0,
        "receipt_force_state": None,
        "receipt_ts": 0.0,
    }
    if request_kind == FIXED_DETECTION_KIND:
        request["request_kind"] = FIXED_DETECTION_KIND
    data[STATE_KEY] = request
    data.pop(GRANT_KEY, None)
    return deepcopy(request)


def commit_receipt(box, receipt, *, now_ts=0.0, hold_s=DEFAULT_HOLD_S):
    """Bindet den Haltebeginn einmalig an einen echten Ausgangsbeleg."""

    data = box if isinstance(box, dict) else {}
    request = data.get(STATE_KEY)
    proof = receipt if isinstance(receipt, dict) else {}
    now_value = _float(now_ts, 0.0)
    if not isinstance(request, dict) or not _active_request(request, now_value):
        return {}
    if str(request.get("stage") or "") == "committed":
        return deepcopy(request)
    reservation_id = str(request.get("reservation_id") or "")
    session_id = str(request.get("plug_session_id") or "")
    request_cycle_token = str(request.get("request_cycle_token") or "")
    receipt_cycle_token = str(proof.get("cycle_token") or "")
    current_cycle_token = str(data.get("_wallbox_cycle_token") or "")
    method = str(proof.get("method") or "")
    amp = _float(proof.get("amp"), -1.0)
    receipt_ts = _float(proof.get("ts"), 0.0)
    if (
        not reservation_id
        or str(proof.get("start_hold_reservation_id") or "") != reservation_id
        or str(proof.get("start_hold_plug_session_id") or "") != session_id
        or _current_session_id(data) != session_id
        or not request_cycle_token
        or receipt_cycle_token != request_cycle_token
        or current_cycle_token != request_cycle_token
        or method not in {
            "set_amp_and_state",
            "set_current",
            "set_direct_current",
            "set_amp_sonnenmodus",
            "set_amp_autonomous_solar",
        }
        or amp < 6.0
        or proof.get("forced_zero") is True
        or receipt_ts <= 0.0
        or receipt_ts > now_value + 1.0
    ):
        return {}
    if method in {
        "set_amp_sonnenmodus",
        "set_amp_autonomous_solar",
    } and _int(proof.get("force_state"), 0) != 2:
        return {}

    committed = dict(request)
    committed.update({
        "stage": "committed",
        "started_ts": receipt_ts,
        "expires_ts": receipt_ts
        + max(20.0, min(DEFAULT_HOLD_S, _float(hold_s, DEFAULT_HOLD_S))),
        "receipt_reservation_id": reservation_id,
        "receipt_cycle_token": str(proof.get("cycle_token") or ""),
        "receipt_method": method,
        "receipt_amp": amp,
        "receipt_force_state": proof.get("force_state"),
        "receipt_ts": receipt_ts,
    })
    if request.get("request_kind") == FIXED_DETECTION_KIND:
        meta = _fixed_detection_metadata(request)
        if meta is None:
            return {}
        committed["expires_ts"] = min(request["expires_ts"], receipt_ts + meta["bridge_remaining_s"])
    data[STATE_KEY] = committed
    return deepcopy(committed)


def request_for_intent(box, *, now_ts, connected, status_fresh, enabled, blocked):
    """Projiziert nur eine aktuell sichere RAM-Reservierung in das Intent."""

    data = box if isinstance(box, dict) else {}
    request = data.get(STATE_KEY)
    now_value = _float(now_ts, 0.0)
    if not isinstance(request, dict):
        return {}
    result = deepcopy(request)
    result.update({
        "connected": bool(connected),
        "status_fresh": bool(status_fresh),
        "enabled": bool(enabled),
        "blocked": bool(blocked),
    })
    if not _active_request(result, now_value):
        result["active"] = False
        result["stage"] = "expired"
        data[STATE_KEY] = dict(result)
        data.pop(GRANT_KEY, None)
        return {}
    if not connected or not status_fresh or not enabled or blocked:
        result["active"] = False
        result["stage"] = "rejected"
        data[STATE_KEY] = dict(result)
        data.pop(GRANT_KEY, None)
        return {}
    data[STATE_KEY] = dict(result)
    return result


def aggregate_requests(items):
    requests = [deepcopy(item) for item in (items or []) if isinstance(item, dict)]
    return {
        "schema_version": REQUEST_SCHEMA,
        "active": bool(requests),
        "requests": requests,
        "requested_group_minimum_w": max(
            (max(0, _int(item.get("group_minimum_w"), 0)) for item in requests),
            default=0,
        ),
        "reservation_ids": [str(item.get("reservation_id") or "") for item in requests],
    }


def intent_contract(wb_intent, *, now_ts):
    """Validiert das vom Wallbox Manager gelieferte Start-Halte-Intent."""

    intent = wb_intent if isinstance(wb_intent, dict) else {}
    now_value = _float(now_ts, 0.0)
    intent_ts = _float(intent.get("ts"), 0.0)
    intent_fresh = bool(
        intent
        and intent_ts > 0.0
        and -2.0 <= now_value - intent_ts <= 60.0
    )
    raw = intent.get("start_hold_requests")
    accepted = []
    rejected = []
    for candidate in raw if isinstance(raw, list) else []:
        if not isinstance(candidate, dict):
            continue
        item = deepcopy(candidate)
        blockers = []
        reservation_id = str(item.get("reservation_id") or "")
        session_id = str(item.get("plug_session_id") or "")
        stage = str(item.get("stage") or "")
        created_ts = _float(item.get("created_ts"), 0.0)
        started_ts = _float(item.get("started_ts"), 0.0)
        expires_ts = _float(item.get("expires_ts"), 0.0)
        minimum_w = max(0, _int(item.get("minimum_quantum_w"), 0))
        group_w = max(0, _int(item.get("group_minimum_w"), 0))
        raw_group_members = item.get("group_members")
        group_members = _normalize_group_members(raw_group_members, allow_two_phase=item.get("request_kind") == FIXED_DETECTION_KIND, allow_running_current=item.get("request_kind") == FIXED_DETECTION_KIND)
        selected_group_member = next(
            (
                member
                for member in group_members
                if member.get("wb_id") == _int(item.get("wb_id"), 0)
            ),
            None,
        )
        if item.get("request_kind") == FIXED_DETECTION_KIND and _fixed_detection_metadata(item) is None:
            blockers.append("fixed_detection_energy_unbound")
        if not intent_fresh:
            blockers.append("stale_intent")
        if item.get("schema_version") != REQUEST_SCHEMA:
            blockers.append("schema_mismatch")
        if not item.get("active") or stage not in ACTIVE_STAGES:
            blockers.append("request_inactive")
        if not reservation_id or not session_id or not str(item.get("request_cycle_token") or ""):
            blockers.append("identity_missing")
        if _int(item.get("wb_id"), 0) <= 0:
            blockers.append("wb_id_invalid")
        target_phases = _int(item.get("target_phases"), 0)
        minimum_amp = _int(item.get("minimum_amp"), 0)
        allowed_phases = (1, 2, 3) if item.get("request_kind") == FIXED_DETECTION_KIND else (1, 3)
        if target_phases not in allowed_phases or minimum_amp != 6:
            blockers.append("not_supported_phase_6a")
        if (
            target_phases not in allowed_phases
            or minimum_w < minimum_amp * 230 * target_phases
            or group_w < minimum_w
        ):
            blockers.append("minimum_power_invalid")
        if (
            not isinstance(raw_group_members, list)
            or not group_members
            or len(group_members) != len(raw_group_members)
            or not isinstance(selected_group_member, dict)
            or str(selected_group_member.get("plug_session_id") or "")
            != session_id
            or _int(selected_group_member.get("target_phases"), 0)
            != target_phases
            or _int(selected_group_member.get("minimum_amp"), 0)
            != minimum_amp
            or _int(selected_group_member.get("minimum_power_w"), 0)
            != minimum_w
            or sum(
                _int(member.get("minimum_power_w"), 0)
                for member in group_members
            )
            > group_w
        ):
            blockers.append("group_members_invalid")
        if created_ts <= 0.0 or expires_ts <= now_value:
            blockers.append("request_expired")
        if not item.get("connected") or not item.get("status_fresh") or not item.get("enabled"):
            blockers.append("wallbox_not_controllable")
        if item.get("blocked"):
            blockers.append("wallbox_blocked")
        if stage == "await_receipt" and expires_ts - created_ts > DEFAULT_ARM_S + 1.0:
            blockers.append("arm_lease_invalid")
        if stage == "committed":
            if (
                started_ts <= 0.0
                or expires_ts - started_ts > DEFAULT_HOLD_S + 1.0
                or str(item.get("receipt_reservation_id") or "") != reservation_id
                or str(item.get("receipt_method") or "")
                not in {
                    "set_amp_and_state",
                    "set_current",
                    "set_direct_current",
                    "set_amp_sonnenmodus",
                    "set_amp_autonomous_solar",
                }
                or _float(item.get("receipt_amp"), 0.0) < 6.0
                or _float(item.get("receipt_ts"), 0.0) != started_ts
                or str(item.get("receipt_cycle_token") or "")
                != str(item.get("request_cycle_token") or "")
                or (
                    str(item.get("receipt_method") or "")
                    in {"set_amp_sonnenmodus", "set_amp_autonomous_solar"}
                    and _int(item.get("receipt_force_state"), 0) != 2
                )
            ):
                blockers.append("receipt_not_bound")
        if blockers:
            rejected.append({
                "reservation_id": reservation_id,
                "wb_id": max(0, _int(item.get("wb_id"), 0)),
                "blockers": blockers,
            })
            continue
        accepted.append(item)

    return {
        "schema_version": REQUEST_SCHEMA,
        "active": bool(accepted),
        "fresh": intent_fresh,
        "requests": accepted,
        "rejected": rejected,
        "requested_group_minimum_w": max(
            (max(0, _int(item.get("group_minimum_w"), 0)) for item in accepted),
            default=0,
        ),
    }


def arbitrate_grants(
    requests,
    *,
    base_budget_w,
    grid_import_w,
    previous=None,
    now_ts,
    hard_blockers=None,
    battery_support_allowed=True,
    storage_charge_request_w=0,
    max_hold_s=DEFAULT_HOLD_S,
    max_energy_wh=DEFAULT_ENERGY_WH,
    hard_import_w=DEFAULT_HARD_IMPORT_W,
    clock_sample=None,
    max_known_step_s=15.0,
    fixed_grid_available_w=0.0,
    fixed_battery_available_w=0.0,
    fixed_real_pv_w=None,
):
    """Erteilt genau ein globales, zeit- und energiegebundenes Wattbudget."""

    if any(isinstance(item, dict) and item.get("request_kind") == FIXED_DETECTION_KIND for item in (requests or [])):
        return _arbitrate_fixed_detection(
            requests, base_budget_w=base_budget_w, grid_import_w=grid_import_w,
            previous=previous, now_ts=now_ts, hard_blockers=hard_blockers,
            battery_support_allowed=battery_support_allowed,
            storage_charge_request_w=storage_charge_request_w,
            grid_available_w=fixed_grid_available_w, battery_available_w=fixed_battery_available_w,
            real_pv_w=fixed_real_pv_w, hard_import_w=hard_import_w,
        )
    now_value = _float(now_ts, 0.0)
    base_w = max(0, _int(base_budget_w, 0))
    grid_w = max(0, _int(grid_import_w, 0))
    valid = [deepcopy(item) for item in (requests or []) if isinstance(item, dict)]
    required_w = max(
        (max(0, _int(item.get("group_minimum_w"), 0)) for item in valid),
        default=0,
    )
    committed = [item for item in valid if str(item.get("stage") or "") == "committed"]
    reservation_ids = sorted(str(item.get("reservation_id") or "") for item in valid)
    group_key = ":".join(reservation_ids)
    previous_state = previous if isinstance(previous, dict) else {}
    current_clock_sample = (
        deepcopy(clock_sample) if isinstance(clock_sample, dict) else None
    )
    previous_clock_sample = (
        previous_state.get("clock_sample")
        if isinstance(previous_state.get("clock_sample"), dict)
        else None
    )
    previous_reservation_ids = {
        str(item)
        for item in (previous_state.get("reservation_ids") or [])
        if str(item)
    }
    current_reservation_ids = {item for item in reservation_ids if item}
    reservation_overlap = bool(
        previous_reservation_ids.intersection(current_reservation_ids)
    )
    previous_episode_id = str(previous_state.get("episode_id") or "")
    previous_episode_committed = bool(
        previous_state.get(
            "episode_committed",
            previous_state.get("committed", False),
        )
    )
    current_committed = bool(committed)
    carry_episode = bool(
        previous_episode_id
        and valid
        # Mindestens eine unveränderte Reservierung muss die physische
        # Startsession über den Membership-Wechsel hinweg belegen. Zwei
        # vollständig disjunkte committed Mengen sind ein neuer Start und
        # dürfen weder Episode-ID noch bereits verbrauchte Wh übernehmen.
        and reservation_overlap
    )
    episode_anchor_ts = min(
        (_float(item.get("created_ts"), 0.0) for item in valid),
        default=0.0,
    )
    episode_id = (
        previous_episode_id
        if carry_episode
        else (
            "start:%d:%s" % (
                int(max(0.0, episode_anchor_ts) * 1000.0),
                reservation_ids[0],
            )
            if reservation_ids
            else ""
        )
    )
    blockers = [str(item) for item in (hard_blockers or []) if str(item)]
    if not valid:
        blockers.append("no_valid_request")
    if required_w <= 0:
        blockers.append("no_minimum_budget")
    if grid_w >= max(300.0, _float(hard_import_w, DEFAULT_HARD_IMPORT_W)):
        blockers.append("hard_grid_import")

    current_started_ts = min(
        (_float(item.get("started_ts"), 0.0) for item in committed),
        default=0.0,
    )
    previous_started_ts = _float(
        previous_state.get("episode_started_ts"),
        _float(previous_state.get("started_ts"), 0.0),
    )
    previous_sample_ts = _float(previous_state.get("last_sample_ts"), 0.0)
    started_ts = (
        min(
            value
            for value in (previous_started_ts, current_started_ts)
            if value > 0.0
        )
        if carry_episode
        and (previous_started_ts > 0.0 or current_started_ts > 0.0)
        else current_started_ts
    )
    request_expires_ts = min(
        (_float(item.get("expires_ts"), 0.0) for item in valid),
        default=0.0,
    )
    committed_active = bool(committed and started_ts > 0.0)
    elapsed_s = max(0.0, now_value - started_ts) if committed_active else 0.0
    timebase_contract = None
    shortfall_w = max(max(0, required_w - base_w), grid_w)
    used_wh = (
        max(0.0, _float(previous_state.get("used_deficit_wh"), 0.0))
        if carry_episode
        else 0.0
    )
    initial_clock_invalid = bool(
        committed_active
        and not carry_episode
        and current_started_ts > now_value + 1.0
    )
    episode_state_missing = bool(
        committed_active
        and not carry_episode
        # Eine neue, disjunkte Reservierung darf nach einem zuvor gesehenen
        # armed/committed Zustand eine eigene Episode eröffnen. Fehlt dagegen
        # jeder vorherige Storage-Beleg und ist der Receipt bereits älter als
        # ein Zyklus, bleibt ein Replay nach Prozessneustart fail-closed.
        and not previous_episode_id
        and now_value - current_started_ts > 1.0
    )
    previous_episode_clock_invalid = bool(
        committed_active
        and not carry_episode
        and previous_episode_id
        and (
            previous_sample_ts <= 0.0
            or previous_sample_ts > now_value + 1.0
        )
    )
    disjoint_receipt_replayed = bool(
        committed_active
        and not carry_episode
        and previous_episode_id
        and not previous_episode_clock_invalid
        and current_started_ts < previous_sample_ts - 1.0
    )
    if initial_clock_invalid:
        blockers.append("episode_clock_invalid")
    if episode_state_missing:
        blockers.append("episode_state_missing")
    if previous_episode_clock_invalid:
        blockers.append("previous_episode_clock_invalid")
    if disjoint_receipt_replayed:
        blockers.append("episode_receipt_not_newer_than_previous_state")
    if (
        committed_active
        and not initial_clock_invalid
        and not episode_state_missing
        and not previous_episode_clock_invalid
        and not disjoint_receipt_replayed
    ):
        if carry_episode and previous_episode_committed:
            if current_clock_sample is not None:
                timebase_contract = control_time.elapsed_contract(
                    previous_clock_sample,
                    current_clock_sample,
                    max_step_s=max_known_step_s,
                )
                if timebase_contract.get("known"):
                    dt_s = max(
                        0.0,
                        _float(timebase_contract.get("elapsed_s"), 0.0),
                    )
                else:
                    # Der bekannte Wh-Stand bleibt exakt erhalten. Eine
                    # Prozesslücke, neue Epoche oder unvollständige Probe darf
                    # weder als Ladeenergie imputiert noch aktiv fortgesetzt
                    # werden.
                    blockers.append("episode_timebase_unbound")
                    dt_s = 0.0
            else:
                previous_ts = _float(previous_state.get("last_sample_ts"), 0.0)
                if previous_ts <= 0.0 or previous_ts > now_value:
                    blockers.append("episode_clock_invalid")
                    dt_s = 0.0
                else:
                    # Kompatibilität für explizite historische now_ts-Tests.
                    dt_s = now_value - previous_ts
        else:
            # Der erste Storage-Zyklus darf wegen des asynchronen Manager-
            # Takts einige Sekunden nach dem echten Ausgangsreceipt liegen.
            # Bei einer disjunkten, zuvor im Storage gesehenen Folgesession ist
            # der gebundene Receipt-Zeitpunkt die konservative Unterkante: Die
            # bis jetzt verstrichene Zeit wird vollständig mit dem aktuellen
            # Shortfall nachberechnet, statt das neue Konto auf null zu setzen.
            dt_s = (
                0.0
                if current_clock_sample is not None
                else max(0.0, now_value - current_started_ts)
            )
        used_wh += shortfall_w * dt_s / 3600.0
        if current_clock_sample is not None:
            elapsed_s = max(
                0.0,
                _float(previous_state.get("episode_elapsed_s"), 0.0)
                + dt_s,
            )

    hold_limit_s = max(20.0, min(DEFAULT_HOLD_S, _float(max_hold_s, DEFAULT_HOLD_S)))
    energy_limit_wh = max(1.0, min(DEFAULT_ENERGY_WH, _float(max_energy_wh, DEFAULT_ENERGY_WH)))
    if request_expires_ts > 0.0 and now_value >= request_expires_ts:
        blockers.append("request_expired")
    if committed_active and elapsed_s >= hold_limit_s:
        blockers.append("time_limit_reached")
    if committed_active and used_wh >= energy_limit_wh:
        blockers.append("energy_limit_reached")
    previous_terminal = bool(
        carry_episode and previous_state.get("episode_terminal")
    )
    previous_terminal_reason = str(
        previous_state.get("episode_terminal_reason") or ""
    )
    if previous_terminal:
        blockers.append("episode_terminated")

    requested_extension_w = max(0, required_w - base_w)
    available_charge_reduction_w = min(
        max(0, _int(storage_charge_request_w, 0)),
        requested_extension_w,
    )
    uncovered_extension_w = max(
        0,
        requested_extension_w - available_charge_reduction_w,
    )
    if (
        uncovered_extension_w > 0
        and not battery_support_allowed
        and "extension_unfunded" not in blockers
    ):
        blockers.append("extension_unfunded")

    active = not blockers
    extension_w = requested_extension_w if active else 0
    effective_budget_w = max(base_w, required_w) if active else base_w
    charge_reduction_w = available_charge_reduction_w if active else 0
    discharge_support_w = (
        uncovered_extension_w
        if active and battery_support_allowed
        else 0
    )
    episode_terminal = bool(
        committed_active and (previous_terminal or bool(blockers))
    )
    episode_terminal_reason = (
        previous_terminal_reason
        if previous_terminal_reason
        else str(blockers[0] if episode_terminal and blockers else "")
    )
    episode_expires_ts = (
        started_ts + hold_limit_s
        if committed_active
        else request_expires_ts
    )
    status = (
        "committed"
        if active and committed_active
        else "armed"
        if active
        else str(blockers[0] if blockers else "inactive")
    )
    grants = []
    if active:
        for item in valid:
            grants.append({
                "schema_version": GRANT_SCHEMA,
                "active": True,
                "grant_state": status,
                "reservation_id": str(item.get("reservation_id") or ""),
                "wb_id": max(0, _int(item.get("wb_id"), 0)),
                "plug_session_id": str(item.get("plug_session_id") or ""),
                "request_cycle_token": str(item.get("request_cycle_token") or ""),
                "group_members": deepcopy(
                    _normalize_group_members(item.get("group_members"))
                ),
                "episode_id": episode_id,
                "base_budget_w": base_w,
                # Der sitzungsgebundene Beleg ist ausschließlich eine
                # Mindestleistungs-Unterkante. Ein bei Start zufällig höheres
                # normales Storage-Budget darf niemals für 180 s konserviert
                # oder nach einem Drop wiederhergestellt werden.
                "granted_total_budget_w": required_w,
                "storage_effective_budget_w": effective_budget_w,
                "granted_deficit_ceiling_w": extension_w,
                "used_deficit_wh": round(used_wh, 6),
                "started_ts": started_ts,
                "expires_ts": min(
                    _float(item.get("expires_ts"), request_expires_ts),
                    episode_expires_ts,
                ),
            })

    return {
        "schema_version": GRANT_SCHEMA,
        "active": bool(active),
        "status": status,
        "committed": committed_active,
        "episode_id": episode_id,
        "episode_open": bool(valid),
        "episode_committed": committed_active,
        "episode_started_ts": started_ts,
        "episode_expires_ts": episode_expires_ts,
        "episode_terminal": episode_terminal,
        "episode_terminal_reason": episode_terminal_reason,
        "group_key": group_key,
        "reservation_ids": reservation_ids,
        "grants": grants,
        "base_budget_w": base_w,
        "required_group_minimum_w": required_w,
        "effective_budget_w": effective_budget_w,
        "extension_w": extension_w,
        "actual_shortfall_w": shortfall_w if committed_active else 0,
        "grid_import_w": grid_w,
        "used_deficit_wh": round(used_wh, 6),
        "remaining_deficit_wh": round(max(0.0, energy_limit_wh - used_wh), 6),
        "elapsed_s": round(elapsed_s, 3),
        "remaining_s": round(max(0.0, hold_limit_s - elapsed_s), 3),
        "max_energy_wh": energy_limit_wh,
        "max_hold_s": hold_limit_s,
        "battery_support_allowed": bool(battery_support_allowed),
        "charge_reduction_w": charge_reduction_w,
        "discharge_support_w": discharge_support_w,
        "funding_required_w": requested_extension_w,
        "funding_covered_w": (
            available_charge_reduction_w
            + (
                uncovered_extension_w
                if battery_support_allowed
                else 0
            )
        ),
        "funding_applied_w": charge_reduction_w + discharge_support_w,
        "funding_uncovered_w": max(
            0,
            requested_extension_w
            - available_charge_reduction_w
            - (
                uncovered_extension_w
                if battery_support_allowed
                else 0
            ),
        ),
        "source_mode": (
            "storage_charge_reduction_and_battery_support"
            if charge_reduction_w > 0 and discharge_support_w > 0
            else "storage_charge_reduction"
            if charge_reduction_w > 0
            else "authorized_battery_support"
            if discharge_support_w > 0
            else "base_budget_only"
            if active and requested_extension_w <= 0
            else "unfunded"
        ),
        "blockers": blockers,
        "last_sample_ts": now_value,
        "clock_sample": deepcopy(current_clock_sample),
        "episode_elapsed_s": round(elapsed_s, 3),
        "timebase_contract": deepcopy(timebase_contract),
    }


def _arbitrate_fixed_detection(
    requests, *, base_budget_w, grid_import_w, previous, now_ts, hard_blockers,
    battery_support_allowed, storage_charge_request_w, grid_available_w,
    battery_available_w, real_pv_w, hard_import_w,
):
    """Finanziert nur die benannte Probe; das private Gruppenkonto besitzt Wh."""
    items = [deepcopy(x) for x in (requests or []) if isinstance(x, dict)]
    blockers = [str(x) for x in (hard_blockers or []) if str(x)]
    previous = previous if isinstance(previous, dict) else {}
    item = items[0] if len(items) == 1 else {}
    meta = _fixed_detection_metadata(item)
    now = _float(now_ts, 0.0)
    base = max(0, _int(base_budget_w, 0))
    required = max(0, _int(item.get("group_minimum_w"), 0))
    if len(items) != 1 or not meta:
        blockers.append("fixed_probe_group_or_energy_invalid")
    if not _active_request(item, now):
        blockers.append("fixed_probe_request_inactive")
    if (previous.get("active") is True
        and previous.get("request_kind") != FIXED_DETECTION_KIND):
        blockers.append("other_start_hold_active")
    phases = _int(item.get("target_phases"), 0)
    if (phases not in (1, 2, 3) or required <= 0
        or _int(item.get("minimum_amp"), 0) != 6
        or _int(item.get("minimum_quantum_w"), 0) != 6 * 230 * phases):
        blockers.append("fixed_probe_minimum_invalid")
    remaining_s = min(_float(item.get("expires_ts"), 0.0) - now,
                      (meta or {}).get("bridge_remaining_s", 0.0))
    if remaining_s <= 0:
        blockers.append("fixed_probe_expired")
    if item.get("stage") == "await_receipt" and (
        real_pv_w is None or _float(real_pv_w, -1.0) < 1380.0
    ):
        blockers.append("fixed_probe_real_pv_missing")
    if max(0.0, _float(grid_import_w)) >= max(300.0, _float(hard_import_w, DEFAULT_HARD_IMPORT_W)):
        blockers.append("hard_grid_import")
    extension = max(0, required - base)
    charge = min(extension, max(0, _int(storage_charge_request_w, 0)))
    battery = min(extension - charge, max(0, _int(battery_available_w, 0))) if battery_support_allowed else 0
    grid = min(extension - charge - battery, max(0, _int(grid_available_w, 0)))
    uncovered = extension - charge - battery - grid
    # Reservierung bleibt global und zeitlich abnehmend. Sie wird weder hier
    # erneut integriert noch durch eine neue Storage-Episode aufgefüllt.
    energy = (meta or {}).get("bridge_remaining_wh", 0.0)
    if (battery + grid) * max(0.0, remaining_s) / 3600.0 > energy + 1e-6:
        blockers.append("fixed_probe_energy_insufficient")
    if uncovered > 0:
        blockers.append("extension_unfunded")
    active = not blockers
    effective = max(base, required) if active else base
    episode = "fixed:" + str((meta or {}).get("bridge_reservation_id") or "")
    grants = []
    if active:
        grants.append({
            "schema_version": GRANT_SCHEMA, "active": True,
            "grant_state": "committed" if item.get("stage") == "committed" else "armed",
            "reservation_id": item["reservation_id"], "wb_id": item["wb_id"],
            "plug_session_id": item["plug_session_id"], "request_cycle_token": item["request_cycle_token"],
            "group_members": deepcopy(item.get("group_members") or []),
            "episode_id": episode, "base_budget_w": base,
            "granted_total_budget_w": required, "storage_effective_budget_w": effective,
            "granted_deficit_ceiling_w": extension, "used_deficit_wh": meta["bridge_used_wh"],
            "started_ts": _float(item.get("started_ts"), 0.0), "expires_ts": now + remaining_s,
            **meta, "bridge_remaining_s": remaining_s,
            "grid_support_w": grid, "discharge_support_w": battery,
        })
    return {
        "schema_version": GRANT_SCHEMA, "request_kind": FIXED_DETECTION_KIND,
        "active": active, "status": (grants[0]["grant_state"] if active else blockers[0]),
        "committed": item.get("stage") == "committed", "episode_id": episode,
        "episode_open": bool(items), "episode_committed": item.get("stage") == "committed",
        "episode_terminal": bool(not active and item.get("stage") == "committed"),
        "episode_terminal_reason": blockers[0] if blockers else "",
        "reservation_ids": [str(item.get("reservation_id") or "")],
        "grants": grants, "base_budget_w": base, "required_group_minimum_w": required,
        "effective_budget_w": effective, "extension_w": extension if active else 0,
        "charge_reduction_w": charge if active else 0,
        "discharge_support_w": battery if active else 0, "grid_support_w": grid if active else 0,
        "funding_required_w": extension, "funding_covered_w": charge + battery + grid,
        "funding_applied_w": charge + battery + grid if active else 0,
        "funding_uncovered_w": uncovered, "battery_support_allowed": bool(battery_support_allowed),
        "grid_support_allowed": bool(active and meta and meta["grid_support_allowed"]),
        "used_deficit_wh": (meta or {}).get("bridge_used_wh", 0.0),
        "remaining_deficit_wh": energy, "remaining_s": max(0.0, remaining_s),
        "bridge_remaining_wh": energy, "bridge_remaining_s": max(0.0, remaining_s),
        "bridge_reservation_id": str((meta or {}).get("bridge_reservation_id") or ""),
        "energy_account_owner": "wallbox_fixed_start_energy",
        "source_mode": "fixed_detection_bound_sources" if active else "unfunded",
        "blockers": blockers, "last_sample_ts": now,
    }


def apply_grant(box, grant, *, now_ts):
    """Bindet eine Storage-Freigabe exakt an Request und Stecksession."""

    data = box if isinstance(box, dict) else {}
    request = data.get(STATE_KEY)
    item = grant if isinstance(grant, dict) else {}
    now_value = _float(now_ts, 0.0)
    request_group_members = _normalize_group_members(
        request.get("group_members") if isinstance(request, dict) else None,
        allow_two_phase=isinstance(request, dict) and request.get("request_kind") == FIXED_DETECTION_KIND,
        allow_running_current=isinstance(request, dict) and request.get("request_kind") == FIXED_DETECTION_KIND,
    )
    grant_group_members = _normalize_group_members(item.get("group_members"), allow_two_phase=item.get("request_kind") == FIXED_DETECTION_KIND, allow_running_current=item.get("request_kind") == FIXED_DETECTION_KIND)
    valid = bool(
        isinstance(request, dict)
        and request.get("schema_version") == REQUEST_SCHEMA
        and item.get("schema_version") == GRANT_SCHEMA
        and item.get("active") is True
        and str(item.get("reservation_id") or "")
        == str(request.get("reservation_id") or "")
        and str(item.get("plug_session_id") or "")
        == str(request.get("plug_session_id") or "")
        == _current_session_id(data)
        and _int(item.get("wb_id"), 0) == _int(request.get("wb_id"), -1)
        and str(item.get("request_cycle_token") or "")
        == str(request.get("request_cycle_token") or "")
        and _fixed_grant_matches(request, item)
        and request_group_members
        and grant_group_members == request_group_members
        and str(item.get("episode_id") or "")
        and _float(item.get("expires_ts"), 0.0) > now_value
    )
    if not valid:
        data.pop(GRANT_KEY, None)
        return {}
    data[GRANT_KEY] = deepcopy(item)
    return deepcopy(item)


def committed_grant_budget(box, *, base_budget_w, now_ts):
    """Liefert nur für einen belegten Start das zusätzliche Storage-Budget."""

    data = box if isinstance(box, dict) else {}
    request = data.get(STATE_KEY)
    grant = data.get(GRANT_KEY)
    base_w = max(0, _int(base_budget_w, 0))
    now_value = _float(now_ts, 0.0)
    request_group_members = _normalize_group_members(
        request.get("group_members") if isinstance(request, dict) else None,
        allow_two_phase=isinstance(request, dict) and request.get("request_kind") == FIXED_DETECTION_KIND,
        allow_running_current=isinstance(request, dict) and request.get("request_kind") == FIXED_DETECTION_KIND,
    )
    grant_group_members = _normalize_group_members(
        grant.get("group_members") if isinstance(grant, dict) else None,
        allow_two_phase=isinstance(grant, dict) and grant.get("request_kind") == FIXED_DETECTION_KIND,
        allow_running_current=isinstance(grant, dict) and grant.get("request_kind") == FIXED_DETECTION_KIND,
    )
    valid = bool(
        isinstance(request, dict)
        and isinstance(grant, dict)
        and str(request.get("stage") or "") == "committed"
        and str(request.get("receipt_reservation_id") or "")
        == str(request.get("reservation_id") or "")
        and _float(request.get("receipt_amp"), 0.0) >= 6.0
        and str(request.get("receipt_cycle_token") or "")
        == str(request.get("request_cycle_token") or "")
        and _active_request(request, now_value)
        and str(grant.get("reservation_id") or "")
        == str(request.get("reservation_id") or "")
        and str(grant.get("plug_session_id") or "")
        == str(request.get("plug_session_id") or "")
        and _int(grant.get("wb_id"), 0) == _int(request.get("wb_id"), -1)
        and str(grant.get("request_cycle_token") or "")
        == str(request.get("request_cycle_token") or "")
        and _fixed_grant_matches(request, grant)
        and request_group_members
        and grant_group_members == request_group_members
        and grant.get("active") is True
        and str(grant.get("episode_id") or "")
        and _float(grant.get("expires_ts"), 0.0) > now_value
    )
    effective_w = max(
        base_w,
        max(0, _int(grant.get("granted_total_budget_w"), 0)),
    ) if valid else base_w
    authorized_extension_w = (
        max(0, _int(grant.get("granted_deficit_ceiling_w"), 0))
        if valid
        else 0
    )
    return {
        "schema_version": "wallbox_start_hold_budget_v1",
        "active": bool(valid and effective_w > base_w),
        "bound": valid,
        "extension_active": bool(valid and authorized_extension_w > 0),
        "authorized_extension_w": authorized_extension_w,
        "reservation_id": str(request.get("reservation_id") or "")
        if isinstance(request, dict)
        else "",
        "wb_id": max(0, _int(request.get("wb_id"), 0))
        if isinstance(request, dict)
        else 0,
        "group_members": deepcopy(grant_group_members) if valid else [],
        "base_budget_w": base_w,
        "effective_budget_w": effective_w,
        "extension_w": max(0, effective_w - base_w),
        "reason": "storage_grant_bound" if valid else "no_committed_matching_grant",
    }


def floor_override_contract(
    grant_contract,
    *,
    allocated_amp,
    authorized_amp=None,
    hard_grid_stop_due=False,
    battery_hard_stop_due=False,
    immediate_battery_stop_due=False,
    battery_cooldown_active=False,
):
    """Begrenzt den wbminSoC-Startschutz auf exakt 6 A der Vertragsphasen.

    Ein gebundener Start darf ausschließlich lokale Batterie-Floor-Stopps
    kurz unterdrücken. Ein harter Netzstopp bleibt unverändert wirksam. Ohne
    aktuelle 6-A-Allokation oder nach Grant-Ende ist der Sonderpfad aus.
    """

    grant = grant_contract if isinstance(grant_contract, dict) else {}
    allocated = _float(allocated_amp, 0.0)
    authorized = _float(
        allocated if authorized_amp is None else authorized_amp,
        0.0,
    )
    active = bool(
        grant.get("bound") is True
        and grant.get("active") is True
        and abs(allocated - 6.0) <= 0.001
        and authorized >= 6.0
    )
    hard_grid = bool(hard_grid_stop_due)
    battery_stop = bool(battery_hard_stop_due)
    immediate_stop = bool(immediate_battery_stop_due)
    cooldown = bool(battery_cooldown_active)
    if active:
        battery_stop = False
        immediate_stop = False
        cooldown = False
    return {
        "schema_version": "wallbox_start_hold_floor_override_v1",
        "active": active,
        "hold_amp": 6 if active and not hard_grid else 0,
        "authorized_amp": authorized,
        "hard_grid_stop_due": hard_grid,
        "battery_hard_stop_due": battery_stop,
        "immediate_battery_stop_due": immediate_stop,
        "battery_cooldown_active": cooldown,
        "stop_due": bool(hard_grid or battery_stop or immediate_stop or cooldown),
        "reason": (
            "hard_grid_stop"
            if hard_grid
            else "storage_grant_exact_contract_phases_6a"
            if active
            else "no_exact_active_floor_grant"
        ),
    }
