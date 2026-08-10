"""Gemeinsame Verbraucherpriorität für PV- und Ladekurvenüberschüsse.

Der Storage Manager besitzt die Gesamtentscheidung. Dieser Helfer teilt nur den
bereits freigegebenen Rahmen zwischen Wallbox, Wärmepumpe und Heizstab auf,
damit UI-Einstellungen keine konkurrierenden Wattbudgets erzeugen.
"""

import math

CONSUMERS = ("heatpump", "wallbox", "heater")
DEFAULT_CONSUMER_PRIORITY_ORDER = ("heatpump", "wallbox", "heater")
CONSUMER_MIN_W = {
    "heatpump": 1500,
    "wallbox": 6 * 230,
    "heater": 500,
}
CONSUMER_ALIASES = {
    "wp": "heatpump",
    "waermepumpe": "heatpump",
    "warmepumpe": "heatpump",
    "heatpump": "heatpump",
    "wallbox": "wallbox",
    "wb": "wallbox",
    "auto": "wallbox",
    "bev": "wallbox",
    "heizstab": "heater",
    "heater": "heater",
    "shelly": "heater",
}

CONSUMER_BUDGET_SCHEMA = "flexible_consumer_budget_contract_v1"


def _strict_nonnegative_int(value):
    if type(value) is not int or value < 0:
        return None
    return value


def _strict_bool(value):
    return value if type(value) is bool else None


def _strict_power_map(value, *, defaults=None):
    if value is None and defaults is not None:
        value = defaults
    if not isinstance(value, dict):
        return None
    if any(key not in CONSUMERS for key in value):
        return None
    result = {}
    for consumer in CONSUMERS:
        raw = value.get(consumer, (defaults or {}).get(consumer, 0))
        parsed = _strict_nonnegative_int(raw)
        if parsed is None:
            return None
        result[consumer] = parsed
    return result


def _strict_bool_map(value, *, default=False):
    if value is None:
        value = {}
    if not isinstance(value, dict) or any(key not in CONSUMERS for key in value):
        return None
    result = {}
    for consumer in CONSUMERS:
        parsed = _strict_bool(value.get(consumer, default))
        if parsed is None:
            return None
        result[consumer] = parsed
    return result


def _strict_time(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    parsed = float(value)
    return parsed if math.isfinite(parsed) else None


def _fail_closed_contract(reason, *, requested_total_w=0, order=None):
    parsed_order = parse_priority_order(order)
    return {
        "schema_version": CONSUMER_BUDGET_SCHEMA,
        "valid": False,
        "authorization_status": "FAIL_CLOSED",
        "reason_code": str(reason or "consumer_budget_invalid"),
        "total_budget_w": 0,
        "requested_total_budget_w": max(0, int(requested_total_w or 0)),
        "requests_w": {consumer: 0 for consumer in CONSUMERS},
        "device_limits_w": {consumer: 0 for consumer in CONSUMERS},
        "minimums_w": dict(CONSUMER_MIN_W),
        "enabled": {consumer: False for consumer in CONSUMERS},
        "active": {consumer: False for consumer in CONSUMERS},
        "reservations_w": {consumer: 0 for consumer in CONSUMERS},
        "allocations": {consumer: 0 for consumer in CONSUMERS},
        "allocation_sum_w": 0,
        "remaining_w": 0,
        "consumer_priority_order": parsed_order,
        "consumer_priority_effective_order": parsed_order,
        "consumer_priority_wp_runon_s": 0.0,
        "consumer_priority_wp_runon_active": False,
        "priority_front": [],
        "invariant_conserved": True,
        "blockers": [str(reason or "consumer_budget_invalid")],
    }


def allocate_consumer_budget_contract(
    total_budget_w,
    requests_w,
    order=None,
    *,
    enabled,
    evidence_valid,
    minimums_w=None,
    maximums_w=None,
    active=None,
    reservations_w=None,
    previous_active=None,
    priority_changed_at_s=-999999.0,
    now_s=0.0,
    wp_runon_s=600.0,
    wp_runon_active_override=None,
    priority_front=(),
):
    """Teilt genau einen versiegelbaren Verbraucherrahmen deterministisch auf.

    Die Funktion besitzt weder Datei- noch Hardware-I/O. Unvollständige oder
    untypisierte Evidenz erzeugt ausschließlich einen 0-W-Vertrag. Bereits
    aktive Mindestlasten werden vor neuen Starts berücksichtigt; darüber
    hinaus entscheidet die konfigurierte Reihenfolge strikt und reproduzierbar.
    """

    total = _strict_nonnegative_int(total_budget_w)
    requested = _strict_power_map(requests_w)
    enabled_map = _strict_bool_map(enabled)
    active_map = _strict_bool_map(active, default=False)
    previous_map = _strict_bool_map(previous_active, default=False)
    minimums = _strict_power_map(minimums_w, defaults=CONSUMER_MIN_W)
    limits = _strict_power_map(maximums_w, defaults=requested or {})
    reservations = _strict_power_map(
        reservations_w,
        defaults={consumer: 0 for consumer in CONSUMERS},
    )
    evidence = _strict_bool(evidence_valid)
    now_value = _strict_time(now_s)
    changed_at = _strict_time(priority_changed_at_s)
    runon = _strict_time(wp_runon_s)
    runon_override = (
        None
        if wp_runon_active_override is None
        else _strict_bool(wp_runon_active_override)
    )
    parsed_order = parse_priority_order(order)
    front = list(priority_front or ())
    front_valid = bool(
        len(front) == len(set(front))
        and all(consumer in CONSUMERS for consumer in front)
    )
    reason = None
    if total is None:
        reason = "total_budget_type_invalid"
    elif requested is None:
        reason = "consumer_requests_invalid"
    elif enabled_map is None:
        reason = "consumer_enabled_evidence_invalid"
    elif active_map is None or previous_map is None:
        reason = "consumer_activity_evidence_invalid"
    elif minimums is None or limits is None or reservations is None:
        reason = "consumer_power_contract_invalid"
    elif evidence is not True:
        reason = "source_evidence_invalid"
    elif now_value is None or changed_at is None or runon is None or runon < 0.0:
        reason = "consumer_time_contract_invalid"
    elif wp_runon_active_override is not None and runon_override is None:
        reason = "consumer_priority_runon_override_invalid"
    elif not front_valid:
        reason = "consumer_priority_override_invalid"
    elif any(
        enabled_map[name] and minimums[name] > limits[name]
        for name in CONSUMERS
    ):
        reason = "consumer_minimum_exceeds_device_limit"
    elif any(reservations[name] > limits[name] for name in CONSUMERS):
        reason = "consumer_reservation_exceeds_device_limit"
    elif any(
        enabled_map[name] and reservations[name] > requested[name]
        for name in CONSUMERS
    ):
        reason = "consumer_reservation_exceeds_request"
    if reason:
        return _fail_closed_contract(
            reason,
            requested_total_w=total if total is not None else 0,
            order=parsed_order,
        )

    requests = {
        consumer: min(requested[consumer], limits[consumer])
        if enabled_map[consumer]
        else 0
        for consumer in CONSUMERS
    }
    runon_time_active = bool(
        now_value - changed_at < runon
        if runon_override is None
        else runon_override
    )
    wp_runon_active = bool(
        previous_map["heatpump"]
        and enabled_map["heatpump"]
        and requests["heatpump"] >= minimums["heatpump"]
        and runon_time_active
    )
    effective_order = list(parsed_order)
    if wp_runon_active and effective_order[0] != "heatpump":
        effective_order = ["heatpump"] + [
            item for item in effective_order if item != "heatpump"
        ]
    if front:
        effective_order = front + [
            item for item in effective_order if item not in front
        ]

    remaining = total
    allocations = {consumer: 0 for consumer in CONSUMERS}
    blockers = []

    # Laufende Verbraucher erhalten nur dann ihre vollständige Mindest- oder
    # Reservierungsleistung, wenn der gemeinsame Rahmen sie physikalisch trägt.
    for consumer in effective_order:
        if not active_map[consumer] or requests[consumer] <= 0:
            continue
        floor_w = max(minimums[consumer], reservations[consumer])
        floor_w = min(floor_w, requests[consumer])
        if requests[consumer] < minimums[consumer]:
            blockers.append(consumer + "_request_below_minimum")
            continue
        if remaining < floor_w:
            blockers.append(consumer + "_active_minimum_unfunded")
            continue
        allocations[consumer] = floor_w
        remaining -= floor_w

    # Oberhalb der aktiven Mindestlasten gilt die konfigurierte Reihenfolge
    # strikt. Dadurch bleibt auch bei identischen Eingaben jede Permutation
    # deterministisch und es entsteht kein verstecktes Zweitbudget.
    for consumer in effective_order:
        request_w = requests[consumer]
        if request_w <= allocations[consumer]:
            continue
        if allocations[consumer] == 0 and request_w < minimums[consumer]:
            blockers.append(consumer + "_request_below_minimum")
            continue
        missing_w = request_w - allocations[consumer]
        if allocations[consumer] == 0 and remaining < minimums[consumer]:
            blockers.append(consumer + "_minimum_unfunded")
            continue
        grant_w = min(missing_w, remaining)
        if allocations[consumer] == 0 and grant_w < minimums[consumer]:
            blockers.append(consumer + "_partial_start_blocked")
            continue
        allocations[consumer] += grant_w
        remaining -= grant_w

    allocation_sum = sum(allocations.values())
    conserved = bool(allocation_sum <= total and allocation_sum + remaining == total)
    if not conserved:
        return _fail_closed_contract(
            "consumer_budget_conservation_failed",
            requested_total_w=total,
            order=parsed_order,
        )
    return {
        "schema_version": CONSUMER_BUDGET_SCHEMA,
        "valid": True,
        "authorization_status": "AUTHORIZED",
        "reason_code": "consumer_budget_authorized",
        "total_budget_w": total,
        "requested_total_budget_w": total,
        "requests_w": requests,
        "device_limits_w": limits,
        "minimums_w": minimums,
        "enabled": enabled_map,
        "active": active_map,
        "reservations_w": reservations,
        "allocations": allocations,
        "allocation_sum_w": allocation_sum,
        "remaining_w": remaining,
        "consumer_priority_order": parsed_order,
        "consumer_priority_effective_order": effective_order,
        "consumer_priority_wp_runon_s": float(runon),
        "consumer_priority_wp_runon_active": wp_runon_active,
        "priority_front": front,
        "invariant_conserved": conserved,
        "blockers": sorted(set(blockers)),
    }


def validate_consumer_budget_contract(contract):
    """Prüft den vollständigen Summen- und Typvertrag ohne Imputation."""

    data = contract if isinstance(contract, dict) else {}
    result = {
        "valid": False,
        "reason_code": "consumer_budget_contract_missing",
        "total_budget_w": 0,
        "allocations": {consumer: 0 for consumer in CONSUMERS},
    }
    if data.get("schema_version") != CONSUMER_BUDGET_SCHEMA:
        return {**result, "reason_code": "consumer_budget_schema_invalid"}
    if data.get("valid") is not True or data.get("authorization_status") != "AUTHORIZED":
        return {**result, "reason_code": "consumer_budget_not_authorized"}
    total = _strict_nonnegative_int(data.get("total_budget_w"))
    requests = _strict_power_map(data.get("requests_w"))
    limits = _strict_power_map(data.get("device_limits_w"))
    minimums = _strict_power_map(data.get("minimums_w"))
    allocations = _strict_power_map(data.get("allocations"))
    enabled_map = _strict_bool_map(data.get("enabled"))
    active_map = _strict_bool_map(data.get("active"))
    reservations = _strict_power_map(data.get("reservations_w"))
    remaining = _strict_nonnegative_int(data.get("remaining_w"))
    allocation_sum = _strict_nonnegative_int(data.get("allocation_sum_w"))
    priority_order = data.get("consumer_priority_order")
    effective_order = data.get("consumer_priority_effective_order")
    orders_valid = bool(
        isinstance(priority_order, list)
        and isinstance(effective_order, list)
        and len(priority_order) == len(CONSUMERS)
        and len(effective_order) == len(CONSUMERS)
        and set(priority_order) == set(CONSUMERS)
        and set(effective_order) == set(CONSUMERS)
    )
    if any(value is None for value in (
        total, requests, limits, minimums, allocations, enabled_map,
        active_map, reservations, remaining, allocation_sum,
    )):
        return {**result, "reason_code": "consumer_budget_types_invalid"}
    if not orders_valid:
        return {**result, "reason_code": "consumer_budget_priority_invalid"}
    if data.get("invariant_conserved") is not True:
        return {**result, "reason_code": "consumer_budget_invariant_unconfirmed"}
    if allocation_sum != sum(allocations.values()):
        return {**result, "reason_code": "consumer_budget_sum_mismatch"}
    if allocation_sum > total or allocation_sum + remaining != total:
        return {**result, "reason_code": "consumer_budget_overallocated"}
    for consumer in CONSUMERS:
        allocation = allocations[consumer]
        if enabled_map[consumer] and minimums[consumer] > limits[consumer]:
            return {**result, "reason_code": "consumer_budget_limit_invalid"}
        if allocation > requests[consumer] or allocation > limits[consumer]:
            return {**result, "reason_code": "consumer_budget_allocation_exceeds_claim"}
        if allocation > 0 and (
            not enabled_map[consumer] or allocation < minimums[consumer]
        ):
            return {**result, "reason_code": "consumer_budget_minimum_or_off_invalid"}
    return {
        "valid": True,
        "reason_code": "consumer_budget_contract_valid",
        "total_budget_w": total,
        "allocations": allocations,
        "remaining_w": remaining,
        "allocation_sum_w": allocation_sum,
    }


def validate_consumer_command_allocations(contract):
    """Bindet Aktorbudgets strikt an Eligibility, ohne Accounting zu lösen."""

    validation = validate_consumer_budget_contract(contract)
    result = {
        "valid": False,
        "reason_code": "consumer_command_allocations_invalid",
        "allocations": {consumer: 0 for consumer in CONSUMERS},
        "accounting_allocations": {consumer: 0 for consumer in CONSUMERS},
    }
    if validation.get("valid") is not True:
        return {
            **result,
            "reason_code": str(
                validation.get("reason_code") or "consumer_budget_not_authorized"
            ),
        }
    data = contract if isinstance(contract, dict) else {}
    command_eligible = _strict_bool_map(data.get("command_eligible"))
    command_allocations = _strict_power_map(data.get("command_allocations"))
    accounting = dict(validation.get("allocations") or {})
    minimums = _strict_power_map(data.get("minimums_w"))
    raw_caps = data.get("runtime_caps_w", {})
    if (
        command_eligible is None
        or command_allocations is None
        or minimums is None
        or not isinstance(raw_caps, dict)
        or any(consumer not in CONSUMERS for consumer in raw_caps)
    ):
        return result
    for consumer in CONSUMERS:
        cap_w = accounting[consumer]
        if consumer in raw_caps:
            cap_w = _strict_nonnegative_int(raw_caps.get(consumer))
            if cap_w is None:
                return {
                    **result,
                    "reason_code": "consumer_runtime_cap_invalid",
                }
        expected_w = (
            min(accounting[consumer], cap_w)
            if command_eligible[consumer]
            else 0
        )
        if 0 < expected_w < minimums[consumer]:
            expected_w = 0
        if command_allocations[consumer] != expected_w:
            return {
                **result,
                "reason_code": "consumer_command_eligibility_mismatch",
            }
    return {
        "valid": True,
        "reason_code": "consumer_command_allocations_valid",
        "allocations": command_allocations,
        "accounting_allocations": accounting,
        "command_eligible": command_eligible,
    }


def cap_consumer_budget_contract(contract, caps_w):
    """Senkt autorisierte Teilbudgets, ohne frei werdende Watt neu zu verteilen."""

    validation = validate_consumer_budget_contract(contract)
    command_validation = validate_consumer_command_allocations(contract)
    if validation.get("valid") is not True:
        return _fail_closed_contract(validation.get("reason_code"))
    if command_validation.get("valid") is not True:
        return _fail_closed_contract(command_validation.get("reason_code"))
    caps = caps_w if isinstance(caps_w, dict) else None
    if caps is None or any(key not in CONSUMERS for key in caps):
        return _fail_closed_contract("consumer_budget_caps_invalid")
    result = dict(contract)
    allocations = dict(validation["allocations"])
    minimums = dict(result["minimums_w"])
    applied_caps = dict(result.get("runtime_caps_w") or {})
    for consumer, raw_cap in caps.items():
        cap_w = _strict_nonnegative_int(raw_cap)
        if cap_w is None:
            return _fail_closed_contract("consumer_budget_cap_type_invalid")
        applied_caps[consumer] = cap_w
    command_eligible = dict(result.get("command_eligible") or {})
    command_allocations = {}
    for consumer in CONSUMERS:
        capped = allocations[consumer]
        if consumer in applied_caps:
            capped = min(capped, applied_caps[consumer])
        if 0 < capped < minimums[consumer]:
            capped = 0
        command_allocations[consumer] = (
            capped if command_eligible.get(consumer) is True else 0
        )
    result.update({
        "allocations": allocations,
        "accounting_allocations": dict(allocations),
        "command_allocations": command_allocations,
        "command_allocation_sum_w": sum(command_allocations.values()),
        "runtime_caps_w": applied_caps,
        "invariant_conserved": True,
    })
    return result


def parse_priority_order(value):
    """Liefert eine vollständige Verbraucherreihenfolge ohne Duplikate."""
    if value is None or value == "":
        items = DEFAULT_CONSUMER_PRIORITY_ORDER
    elif isinstance(value, str):
        items = value.replace(">", ",").replace("|", ",").replace(";", ",").split(",")
    else:
        try:
            items = list(value)
        except TypeError:
            items = DEFAULT_CONSUMER_PRIORITY_ORDER

    seen = set()
    order = []
    for raw in items:
        name = CONSUMER_ALIASES.get(str(raw).strip().lower())
        if name in CONSUMER_MIN_W and name not in seen:
            seen.add(name)
            order.append(name)
    for name in DEFAULT_CONSUMER_PRIORITY_ORDER:
        if name not in seen:
            order.append(name)
    return order


def priority_order_key(order):
    return ",".join(parse_priority_order(order))


def priority_order_from_config(config):
    return parse_priority_order((config or {}).get("consumer_priority_order"))


def priority_runon_s_from_config(config, default=600.0):
    try:
        raw = float((config or {}).get("consumer_priority_wp_runon_s", default) or default)
        return max(0.0, min(7200.0, raw))
    except (TypeError, ValueError):
        return float(default)


def allocate_consumer_budget(
    available_w,
    requests_w,
    order=None,
    *,
    previous_active=None,
    priority_changed_at_s=-999999.0,
    now_s=0.0,
    wp_runon_s=600.0,
):
    """Teilt einen freigegebenen Rahmen gemäß Verbraucherpriorität auf."""
    parsed_order = parse_priority_order(order)
    previous_active = previous_active or {}
    requests = {name: max(0, int(float((requests_w or {}).get(name, 0) or 0))) for name in CONSUMERS}
    wp_runon_active = bool(
        previous_active.get("heatpump")
        and requests.get("heatpump", 0) > 0
        and (float(now_s or 0.0) - float(priority_changed_at_s or 0.0)) < float(wp_runon_s or 0.0)
    )

    effective_order = list(parsed_order)
    if wp_runon_active and effective_order[0] != "heatpump":
        effective_order = ["heatpump"] + [item for item in effective_order if item != "heatpump"]

    remaining = max(0, int(float(available_w or 0)))
    allocations = {name: 0 for name in CONSUMERS}
    for name in effective_order:
        request = requests.get(name, 0)
        if request <= 0:
            continue
        minimum = CONSUMER_MIN_W[name]
        if remaining >= minimum:
            grant = min(request, remaining)
            allocations[name] = grant
            remaining -= grant

    return {
        "available_w": max(0, int(float(available_w or 0))),
        "requests_w": requests,
        "allocations": allocations,
        "consumer_priority_order": parsed_order,
        "consumer_priority_effective_order": effective_order,
        "consumer_priority_wp_runon_s": float(wp_runon_s or 0.0),
        "consumer_priority_wp_runon_active": wp_runon_active,
        "remaining_w": remaining,
    }
