"""Shared consumer-priority helper for PV/curve surplus budgets.

The storage manager owns the total surplus decision. This helper only splits
that already approved budget between flexible consumers so UI settings cannot
make wallbox, heat pump and heater fight over the same watts.
"""

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


def parse_priority_order(value):
    """Return a complete, duplicate-free consumer order."""
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
    """Allocate an approved flexible-consumer budget by configured priority."""
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
