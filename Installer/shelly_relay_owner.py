"""Reine Konflikterkennung für Pro3EM und Shelly-Kontakte der Wärmepumpe."""
from urllib.parse import urlsplit

SHELLY_OWNER_WARNING = (
    "Beide Dienste können dasselbe Shelly-Relais schalten. "
    "Eine der beiden Zuordnungen sollte entfernt werden."
)


def normalized_shelly_address(value):
    """Vergleicht Hostnamen und Ports; ein fehlender HTTP-Port bedeutet 80."""
    value = "".join(str(value or "").split()).lower()
    if value in {"", "0", "0.0.0.0", "none", "null"}:
        return None
    try:
        parsed = urlsplit(value if "://" in value else "//" + value)
        host = (parsed.hostname or "").rstrip(".")
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        if not host or host == "0.0.0.0":
            return None
        return (host, port)
    except ValueError:
        # Ein unbestimmbarer Port darf denselben Host nicht freigeben.
        return (value.split(":", 1)[0].rstrip("."), None)


def same_shelly_address(first, second):
    a, b = normalized_shelly_address(first), normalized_shelly_address(second)
    return bool(a and b and a[0] == b[0] and (a[1] == b[1] or None in (a[1], b[1])))


def relay_id(value):
    """Liest die Relais-ID wie cfg_int; unlesbare Werte bedeuten nur Messung."""
    try:
        text = str(value).strip()
        return int(float(text if text != "" else -1))
    except Exception:
        return -1


def shelly_relay_conflict(cfg):
    """Die bestehenden SG-/EVU-Ausgänge schreiben beide fest Relais 0.

    Es gibt dort keine Kanaloption. Unlesbare Pro3EM-Relais-IDs bedeuten
    wie im Heizstab-Dienst -1 (nur Messung); ein unbekannter Port bei
    gleichem Host bleibt dagegen ein möglicher Konflikt.
    """
    if str(cfg.get("wp_type", "")).strip() != "3":
        return False
    if str(cfg.get("shelly_3em_enable", "0")).strip().lower() not in {"1", "true", "yes", "on"}:
        return False
    channel = relay_id(cfg.get("shelly_3em_relay_id", -1))
    if channel != 0:
        return False
    return any(same_shelly_address(cfg.get("shelly_3em_ip"), cfg.get(key))
               for key in ("shelly_sg_ip", "shelly_pause_ip"))

