"""Gemeinsame Auflösung der Hausabsicherung ohne Änderung der Rohkonfiguration."""
import math
import json
import os
import stat
import tempfile

HOUSE_FUSE_PATH = "/var/www/html/data/e3dc_house_fuse.json"

# Ein gültiger E3DC-Eintrag zählt wie ein ausdrücklich eingetragener Nutzerwert.
E3DC_FUSE_IS_EXPLICIT = True


def _present(value):
    return value is not None and str(value).strip() != ""


def _positive(value):
    if isinstance(value, bool):
        return None
    try:
        number = float(str(value).strip().replace(",", "."))
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def resolve_house_fuse(config, live):
    """Liefert Basis, Phasen und Herkunft; fehlende Werte bleiben unbekannt."""
    cfg, data = config or {}, live or {}
    raw = cfg.get("grid_max_amps")
    user = _positive(raw)
    invalid = _present(raw) and user is None
    e3dc = _positive(data.get("e3dc_fuse_amps")) if data.get("e3dc_fuse_valid") is True else None
    if e3dc is not None and not 1 <= e3dc < 999:
        e3dc = None
    confirmed = user is not None and _positive(cfg.get("grid_max_amps_confirmed")) == user
    hint = ""
    if invalid:
        effective, source = None, "invalid"
    elif user is not None and e3dc is not None:
        if user <= e3dc:
            effective, source = user, "user"
        elif confirmed:
            effective, source = user, "user_confirmed"
        else:
            effective, source = e3dc, "e3dc_unconfirmed_user"
    elif user is not None:
        effective, source = user, "user"
    elif e3dc is not None:
        effective, source = e3dc, "e3dc"
    else:
        effective, source = None, "none"
    if user is not None and e3dc is not None and user != e3dc:
        hint = f"Nutzereingabe {user:g} A weicht von der Hausabsicherung im E3DC ({e3dc:g} A) ab."
        if source == "e3dc_unconfirmed_user":
            hint += " Der höhere Nutzerwert gilt erst nach Bestätigung; bis dahin gilt der E3DC-Wert."
    phases = {}
    explicit_phases = {}
    base_explicit = bool(user is not None or (e3dc is not None and E3DC_FUSE_IS_EXPLICIT))
    phase_explicit = False
    for phase in (1, 2, 3):
        raw_phase = cfg.get(f"grid_max_amps_l{phase}")
        value = _positive(raw_phase) if _present(raw_phase) else effective
        phase_explicit = phase_explicit or _positive(raw_phase) is not None
        explicit_phases[f"l{phase}"] = bool(not invalid and (base_explicit or _positive(raw_phase) is not None))
        if invalid or (_present(raw_phase) and value is None):
            value = 0.0
        elif e3dc is not None and value is not None and not confirmed:
            value = min(value, e3dc)
        phases[f"l{phase}"] = value
    return dict(effective=effective, source=source, user_amps=user, e3dc_amps=e3dc,
        phase_limits_effective=phases, phase_limits_explicit=explicit_phases,
        explicit=bool(not invalid and (user is not None or phase_explicit
            or (e3dc is not None and E3DC_FUSE_IS_EXPLICIT))),
        hint=hint)


def house_fuse_config(config, resolution):
    """Erzeugt ausschließlich eine Zykluskopie; 35 A bleiben PV-Ersatzrahmen."""
    result = dict(config or {})
    result["grid_max_amps"] = 0.0 if resolution["source"] == "invalid" else resolution["effective"]
    for phase, value in resolution["phase_limits_effective"].items():
        result[f"grid_max_amps_{phase}"] = value
    result["_house_fuse"] = resolution
    return result


def _empty(source="rscp_missing"):
    return dict(e3dc_fuse_amps=None, e3dc_fuse_valid=False,
        e3dc_fuse_source=source, e3dc_fuse_raw_w=None, e3dc_fuse_read_ts=None)


def _checked(data):
    """Gespeicherter oder veröffentlichter Systemwert; jede andere Form ist unbekannt."""
    try:
        amps, raw, stamp = data.get("e3dc_fuse_amps"), data.get("e3dc_fuse_raw_w"), data.get("e3dc_fuse_read_ts")
        if (type(amps) is not int or not 1 <= amps < 999 or type(raw) is not int
                or not 0 <= raw <= 0xffffffff or raw // 690 != amps
                or isinstance(stamp, bool) or not isinstance(stamp, (int, float))
                or not math.isfinite(stamp) or stamp <= 0 or data.get("e3dc_fuse_source") != "rscp"):
            return None
    except (AttributeError, OverflowError):
        return None
    return dict(e3dc_fuse_amps=amps, e3dc_fuse_valid=True,
        e3dc_fuse_source="rscp", e3dc_fuse_raw_w=raw, e3dc_fuse_read_ts=stamp)


def read_house_fuse(path=None):
    """Liest den gespeicherten Systemwert, ohne die reine Auflösung zu verändern."""
    descriptor = -1
    try:
        descriptor = os.open(path or HOUSE_FUSE_PATH, os.O_RDONLY
            | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
        if not stat.S_ISREG(os.fstat(descriptor).st_mode):
            return _empty()
        payload = os.read(descriptor, 16385)
        if len(payload) > 16384:
            return _empty()
        return _checked(json.loads(payload)) or _empty()
    except (OSError, ValueError, TypeError, RecursionError):
        return _empty()
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def system_house_fuse(live=None, path=None):
    """Systemwert der Dienste: die jüngste Lesung aus den Live-Daten vor der Datei.

    Scheitert das Speichern, steht ein neu gelesener Wert nur in den Live-Daten;
    meldet der E3DC „nicht eingetragen“, gilt eine nicht entfernte Datei nicht mehr.
    """
    data = live if isinstance(live, dict) else {}
    if data.get("e3dc_fuse_source") == "rscp_not_set" and data.get("e3dc_fuse_valid") is False:
        return _empty("rscp_not_set")
    return _checked(data) or read_house_fuse(path)


def store_house_fuse(value, path=None):
    """Nur der Live-Leser schreibt: atomar und ausschließlich bei Inhaltsänderung."""
    path = os.fspath(path or HOUSE_FUSE_PATH)
    previous = read_house_fuse(path)
    if value.get("e3dc_fuse_source") == "rscp_not_set":
        try:
            os.unlink(path)
        except FileNotFoundError:
            return ""
        return "Hausabsicherung im E3DC nicht mehr hinterlegt; gespeicherter Systemwert entfernt."
    if not value.get("e3dc_fuse_valid"):
        return ""
    if previous["e3dc_fuse_valid"] and all(previous[k] == value[k]
            for k in ("e3dc_fuse_amps", "e3dc_fuse_raw_w", "e3dc_fuse_source")):
        return ""
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".e3dc_house_fuse.", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o664)
            json.dump({key: value[key] for key in ("e3dc_fuse_amps", "e3dc_fuse_raw_w",
                "e3dc_fuse_source", "e3dc_fuse_read_ts")}, handle, allow_nan=False)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if previous["e3dc_fuse_valid"]:
        return (f"Hausabsicherung im E3DC geändert: {previous['e3dc_fuse_amps']} A"
            f" → {value['e3dc_fuse_amps']} A. Gespeicherter Systemwert aktualisiert.")
    return f"Hausabsicherung aus dem E3DC gespeichert: {value['e3dc_fuse_amps']} A."
