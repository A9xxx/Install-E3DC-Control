"""
Canonical wallbox modes for the Python managers.

The UI/config deliberately exposes only a small public mode set. Historic C++
mode numbers are still accepted as aliases, but new decisions should use these
helpers instead of comparing raw integers all over the codebase.
"""

MODE_OFF = 0
MODE_CURVE = 2
MODE_BASE = 3
MODE_TARGET = 4
MODE_PRICE = 5
MODE_BATTERY_DEPARTURE = 12

PUBLIC_MODES = (MODE_OFF, MODE_CURVE, MODE_BASE, MODE_TARGET, MODE_PRICE, MODE_BATTERY_DEPARTURE)
STORAGE_FLOOR_MODES = (MODE_TARGET, MODE_PRICE, MODE_BATTERY_DEPARTURE)

# Internal controller paths. These are not UI/config values.
CONTROL_OFF = 0
CONTROL_CURVE = 3
CONTROL_BASE = 6
CONTROL_TARGET = 10  # wbminSoC curve target, no grid, storage support to the floor
CONTROL_PRICE = 11   # same storage floor path, plus grid when the price gate is open

LEGACY_MODE_ALIASES = {
    0: MODE_OFF,
    1: MODE_CURVE,
    2: MODE_CURVE,
    3: MODE_BASE,
    4: MODE_TARGET,
    5: MODE_PRICE,
    6: MODE_BASE,
    7: MODE_CURVE,
    8: MODE_CURVE,
    9: MODE_TARGET,
    10: MODE_TARGET,
    11: MODE_PRICE,
    12: MODE_BATTERY_DEPARTURE,
}

MODE_LABELS = {
    MODE_OFF: "Aus / autonom",
    MODE_CURVE: "PV-Kurve ruhig",
    MODE_BASE: "Grundladung stabil",
    MODE_TARGET: "PV + Akku bis Untergrenze",
    MODE_PRICE: "Sofort bis Preislimit",
    MODE_BATTERY_DEPARTURE: "Akku bis Abfahrt",
}


def _safe_int(value, default=0):
    try:
        if value is None:
            return default
        text = str(value).strip()
        if text == "":
            return default
        return int(float(text))
    except (TypeError, ValueError):
        return default


def normalize_wb_mode(value, default=MODE_OFF):
    """Return one of the public wallbox modes.

    Unknown positive legacy values fall back to MODE_CURVE: PV-only behavior is
    safer than silently opening grid charging, while explicit 0 remains hard off.
    """
    raw = _safe_int(value, default)
    if raw == MODE_OFF:
        return MODE_OFF
    return LEGACY_MODE_ALIASES.get(raw, MODE_CURVE)


def normalize_distribution_mode(value):
    """wb_native_mode is only the multi-wallbox distribution priority: 0/1/2."""
    raw = _safe_int(value, 0)
    return raw if raw in (0, 1, 2) else 0


def mode_label(mode):
    return MODE_LABELS.get(normalize_wb_mode(mode), "PV-Kurve ruhig")


def storage_floor_mode(mode):
    return normalize_wb_mode(mode) in STORAGE_FLOOR_MODES


def price_limit_ct(config):
    """Wallbox grid-price limit in ct/kWh.

    dvcarlimit is the established wallbox limit. wallbox_price_limit_ct is kept
    as a clearer forward-compatible key. If neither is set, grid charging from
    mode 5 stays closed.
    """
    for key in ("wallbox_price_limit_ct", "dvcarlimit"):
        try:
            value = float(config.get(key, -1) or -1)
            if value > 0:
                return value
        except Exception:
            pass
    return -1.0


def price_allows_grid(current_price_ct, config):
    try:
        price = float(current_price_ct)
    except (TypeError, ValueError):
        return False
    limit = price_limit_ct(config)
    return limit > 0 and price <= limit


def controller_mode(public_mode, grid_allowed=False):
    """Map public modes to the existing low-level controller behavior.

    This lets us simplify the user-facing modes without rewriting every driver
    branch at once. Mode 4 deliberately keeps the wbminSoC support path: it
    has no 6A base guarantee, but the storage manager may support the car up
    to the configured floor. Mode 5 only opens the grid when the current price
    gate explicitly allows it.
    """
    mode = normalize_wb_mode(public_mode)
    if mode == MODE_OFF:
        return CONTROL_OFF
    if mode == MODE_CURVE:
        return CONTROL_CURVE
    if mode == MODE_BASE:
        return CONTROL_BASE
    if mode == MODE_TARGET:
        return CONTROL_TARGET
    if mode == MODE_PRICE:
        return CONTROL_PRICE if grid_allowed else CONTROL_TARGET
    if mode == MODE_BATTERY_DEPARTURE:
        return CONTROL_TARGET
    return CONTROL_CURVE
