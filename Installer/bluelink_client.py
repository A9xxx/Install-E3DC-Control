#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import math
import hashlib
import hmac
import secrets
import stat
import logging
import re
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # pragma: no cover - Nur nicht-POSIX Testumgebungen
    fcntl = None

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover - Python < 3.9 bleibt fail-closed
    ZoneInfo = None

try:
    from hyundai_kia_connect_api import VehicleManager
    IMPORT_ERROR = None
except Exception as e:
    VehicleManager = None
    IMPORT_ERROR = str(e)

# Typisierte Fehler der Bibliothek (optional; ohne Bibliothek bleiben die Textregeln).
try:
    from hyundai_kia_connect_api import exceptions as _bluelink_exceptions
except Exception:  # pragma: no cover - Bibliothek fehlt oder zu alt
    _bluelink_exceptions = None

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("BluelinkClient")

def get_install_path():
    root = Path(__file__).resolve().parent.parent
    markers = (root / "VERSION", root / "installer_main.py", root / "Installer")
    if not all(marker.exists() for marker in markers):
        raise RuntimeError("Bluelink: Release-Root ist nicht eindeutig auflösbar")
    return str(root)

CONFIG_FILE = os.path.join(get_install_path(), "e3dc.config.txt")
V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
VEHICLES_JSON_FILE = "/var/www/html/ramdisk/vehicles.json"
FORCE_FLAG_FILE = "/var/www/html/ramdisk/force_bluelink.flag"
BLUELINK_REFRESH_SCHEMA = "bluelink_refresh_status_v1"
BLUELINK_SOC_SOURCES = {"bluelink", "hyundai", "kia", "cloud", "vehicle_cloud"}
# Anmeldung mit Benutzer/Passwort (Region 1 = Europa; Marke hyundai → 2, kia → 1 laut Bibliothek).
BLUELINK_REGION_EUROPE = 1
BLUELINK_BRAND_CODES = {"hyundai": 2, "kia": 1}
BLUELINK_DEFAULT_BRAND = "hyundai"


class BluelinkVehicleDataMissing(RuntimeError):
    """Die Cloudantwort enthält kein zum konfigurierten Fahrzeug nutzbares Ergebnis."""

    refresh_error_code = "vehicle_data_missing"


def _read_force_refresh_request(path=None):
    """Erhält alte manuelle Flags und trennt typisierte Steckaufträge davon."""
    with open(path or FORCE_FLAG_FILE, "r", encoding="utf-8") as handle:
        raw = handle.read(4096).strip()
    # Der bestehende Webweg schreibt einen Zeitstempel; ältere Clients
    # verwendeten eine leere Datei. Beide bleiben manuelle Gesamtanfragen.
    if not raw or raw.isdigit():
        return None
    try:
        request = json.loads(raw)
    except (ValueError, TypeError) as error:
        raise BluelinkVehicleDataMissing("Ungültige Fahrzeug-Anforderung") from error
    if (
        not isinstance(request, dict)
        or request.get("schema") != "vehicle_soc_refresh_request_v1"
        or request.get("source") != "openwb_plug"
        or not str(request.get("vehicle_id") or "").strip()
        or not str(request.get("plug_session_id") or "").strip()
        or type(request.get("wb")) is not int or request["wb"] not in (1, 2)
    ):
        raise BluelinkVehicleDataMissing("Ungültige automatische Fahrzeug-Anforderung")
    requested_at = _safe_timestamp(request.get("requested_at"))
    now = time.time()
    if (isinstance(request.get("requested_at"), bool) or requested_at is None
            or requested_at > now + 300 or now - requested_at > 300):
        # Dieselben fünf Minuten wie bei der bestehenden Web-Flag-Bereinigung;
        # ein abgeschlossener Steckvorgang darf später kein Fahrzeug wecken.
        raise BluelinkVehicleDataMissing("Die automatische Fahrzeug-Anforderung ist abgelaufen")
    return request


def _apply_force_refresh_request(manager, request, vin=None):
    """Automatische Aufträge wecken ausschließlich das gebundene Fahrzeug."""
    if request is None:
        manager.force_refresh_all_vehicles_states()
        return None
    vehicle_id = str(request.get("vehicle_id") or "").strip()
    vehicles = getattr(manager, "vehicles", {})
    vehicle = vehicles.get(vehicle_id) if isinstance(vehicles, dict) else None
    refresh_one = getattr(manager, "force_refresh_vehicle_state", None)
    if vehicle is None or (vin and getattr(vehicle, "vin", None) != vin):
        raise BluelinkVehicleDataMissing("Das angeforderte Fahrzeug ist nicht im freigegebenen Konto verfügbar")
    if not callable(refresh_one):
        raise BluelinkVehicleDataMissing("Diese Cloud-API unterstützt keinen einzelnen Fahrzeugabruf")
    _validate_automatic_refresh_context(request)
    # Öffentliche SDK-Methode; kein Ersatz durch einen Konto-Gesamtabruf.
    refresh_one(vehicle_id)
    return vehicle_id


def _validate_automatic_refresh_context(request):
    """Prüfe unmittelbar vor dem Wakeup die weiterhin gültige Steckfreigabe."""
    try:
        from Wallbox.modes import MODE_OFF, normalize_wb_mode
    except ModuleNotFoundError:
        from Installer.Wallbox.modes import MODE_OFF, normalize_wb_mode

    def read(path):
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)

    def compact(value):
        return re.sub(r"[^a-z0-9]", "", str(value or "").lower())

    def active(value):
        return str(value or "").strip().lower() in ("1", "true", "yes", "ja", "on", "active")

    try:
        wb = request["wb"]
        if type(wb) is not int or wb not in (1, 2):
            raise ValueError("Wallbox fehlt")
        v4 = read(V4_CONFIG_FILE)
        if not isinstance(v4, dict):
            raise ValueError("Konfiguration fehlt")
        cfg = dict(v4.get("config") or {})
        cfg.update(v4)  # Wie im bestehenden V4-Leser gewinnt die Wurzelebene.
        mode = cfg.get(f"wb{wb}_mode")
        if mode in (None, ""):
            legacy_mode = float(cfg.get("wb_native_mode") or 0)
            mode = legacy_mode if legacy_mode > 2 else MODE_OFF
        if (not active(cfg.get("wb_native_enable"))
                or normalize_wb_mode(mode) == MODE_OFF
                or active(cfg.get(f"wb{wb}_locked"))
                or active(cfg.get(f"wb{wb}_manual_pause"))):
            raise ValueError("Wallbox ist aus oder gesperrt")

        data_dir = os.path.dirname(V4_CONFIG_FILE)
        profiles = read(os.path.join(data_dir, "saved_cars.json"))
        profiles = list(profiles.values()) if isinstance(profiles, dict) else profiles
        selected = compact(cfg.get(f"wb{wb}_car_id"))
        alias_keys = ("id", "profile_id", "cloud_vehicle_id", "vehicle_id",
                      "vehicle_mac", "mac", "rfid", "rfid_tag")
        matches = [profile for profile in profiles if isinstance(profile, dict)
                   and selected and selected in {compact(profile.get(key)) for key in alias_keys}]
        if (len(matches) != 1
                or str(matches[0].get("cloud_vehicle_id") or "").strip() != request["vehicle_id"]
                or sum(compact(cfg.get(f"wb{slot}_car_id")) == selected for slot in (1, 2)) != 1):
            raise ValueError("Fahrzeugzuordnung wurde geändert oder ist nicht eindeutig")

        # Der Pro-Treiber schreibt diesen Status vor der SoC-Anforderung.
        # Nicht auf erst später publizierte Manager-/Phasenpersistenz warten.
        status = read(os.path.join(os.path.dirname(FORCE_FLAG_FILE), f"openwb_data_wb{wb}.json"))
        now = time.time()
        sample_ts = _safe_timestamp(status.get("offered_current_readback_ts"))
        requested_at = _safe_timestamp(request.get("requested_at"))
        session_start = _safe_timestamp(status.get("session_start_ts"))
        requested_session_start = _safe_timestamp(request.get("driver_session_start_ts"))
        if (status.get("source") != "openwb_pro" or status.get("wb_id") != wb
                or status.get("plug_state_raw") is not True
                or status.get("connect_php_payload_valid") is not True
                or session_start is None or requested_session_start is None
                or session_start != requested_session_start
                or sample_ts is None or sample_ts > now + 300 or now - sample_ts > 60
                or requested_at is None or requested_at > now + 300 or now - requested_at > 300):
            raise ValueError("Keine aktuelle Steckbestätigung für den SoC-Auftrag")
        aliases = {compact(matches[0].get(key)) for key in alias_keys}
        if any(compact(status.get(key)) and compact(status.get(key)) not in aliases
               for key in ("car_id", "vehicle_id", "rfid_tag")):
            raise ValueError("Das aktuell erkannte Fahrzeug widerspricht dem Auftrag")
    except (OSError, ValueError, TypeError, KeyError, AttributeError, OverflowError) as error:
        # Nur der automatische Cloud-Auftrag entfällt; kein Eingriff in Laden,
        # Strom-/Phasenregelung und kein Ersatz durch einen Konto-Gesamtabruf.
        raise BluelinkVehicleDataMissing("Automatischer SoC-Abruf nicht mehr freigegeben: " + str(error)) from error


def _safe_timestamp(value):
    """Normalisiert ausschließlich einen echten Fahrzeug-Quellzeitpunkt."""
    if value in (None, ""):
        return None
    if hasattr(value, "timestamp"):
        try:
            value = value.timestamp()
        except Exception:
            return None
    try:
        timestamp = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(timestamp) or timestamp <= 0.0:
        return None
    if timestamp > 100000000000.0:
        timestamp /= 1000.0
    return int(timestamp)


def _safe_persisted_soc_timestamp(value, now=None):
    """Verwirft persistierte Zukunftsanker, die gültige Antworten blockieren."""
    timestamp = _safe_timestamp(value)
    if timestamp is None:
        return None
    now_value = int(time.time()) if now is None else int(now)
    if timestamp > now_value + 300:
        return None
    return timestamp


def _normalize_soc_percent(value):
    """Akzeptiert nur endliche, physikalisch mögliche Fahrzeug-Prozentwerte."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        soc = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(soc) or soc < 0.0 or soc > 100.0:
        return None
    return soc


def _parse_raw_vehicle_timestamp(value, source_timezone):
    """Parst nur einen tatsächlich im Hyundai/Kia-Rohstatus vorhandenen Zeitwert."""
    if value in (None, ""):
        return None
    if hasattr(value, "timestamp"):
        timestamp = _safe_timestamp(value)
        return timestamp if timestamp is not None and timestamp <= int(time.time()) + 300 else None

    text = str(value).strip()
    if not text:
        return None
    parsed = None
    try:
        parsed = datetime.strptime(text, "%a, %d %b %Y %H:%M:%S GMT").replace(
            tzinfo=timezone.utc
        )
    except ValueError:
        pass

    if parsed is None:
        compact = text.replace("-", "").replace("T", "").replace(":", "").replace("Z", "")
        if len(compact) >= 14 and compact[:14].isdigit():
            try:
                parsed = datetime.strptime(compact[:14], "%Y%m%d%H%M%S")
            except ValueError:
                parsed = None

    if parsed is None:
        return None
    if parsed.tzinfo is None:
        if source_timezone == "UTC":
            parsed = parsed.replace(tzinfo=timezone.utc)
        elif ZoneInfo is not None:
            parsed = parsed.replace(tzinfo=ZoneInfo("Europe/Berlin"))
        else:
            return None
    timestamp = _safe_timestamp(parsed)
    if timestamp is None or timestamp > int(time.time()) + 300:
        return None
    return timestamp


def _raw_vehicle_soc_timestamp(target_vehicle):
    """Liest ausschließlich die beiden von der EU-API belegten Rohanker."""
    raw = getattr(target_vehicle, "data", None)
    if not isinstance(raw, dict):
        return None

    vehicle_status = raw.get("vehicleStatus")
    if isinstance(vehicle_status, dict) and vehicle_status.get("time") not in (None, ""):
        return _parse_raw_vehicle_timestamp(vehicle_status.get("time"), "Europe/Berlin")
    if raw.get("Date") not in (None, ""):
        return _parse_raw_vehicle_timestamp(raw.get("Date"), "UTC")
    return None


def _preserve_vehicles_fail_closed(old_data):
    """Erhält fremde Quellen und entwertet nur ungebundene Legacywerte."""
    old_vehicles = old_data.get("vehicles") if isinstance(old_data, dict) else None
    preserved = []
    for vehicle in old_vehicles if isinstance(old_vehicles, list) else []:
        if not isinstance(vehicle, dict):
            continue
        item = dict(vehicle)
        source = str(item.get("soc_source") or item.get("source") or "").strip().lower()
        if source in BLUELINK_SOC_SOURCES:
            source_ts = (
                _safe_persisted_soc_timestamp(item.get("soc_source_ts"))
                if "soc_source_ts" in item else None
            )
            rule_confirmed = (
                item.get("soc_rule_confirmed") is True
                and item.get("soc_stale") is not True
                and item.get("soc_rule_usable") is not False
                and source_ts is not None
            )
            item["soc_source"] = "bluelink"
            item["soc_source_ts"] = source_ts
            item["last_updated_at"] = source_ts
            item["soc_rule_confirmed"] = rule_confirmed
            if not rule_confirmed:
                item["soc_stale"] = True
        elif not source:
            # 4f schrieb Cloud- und MQTT-Werte noch ohne Quellvertrag in
            # dieselbe Datei. Diese Migration ist absichtlich unbekannt statt
            # eine der beiden Quellen zu erfinden.
            item["soc_source"] = "legacy_vehicle_unknown"
            item["soc_source_ts"] = None
            item["last_updated_at"] = None
            item["soc_rule_confirmed"] = False
            item["soc_stale"] = True
        preserved.append(item)
    return preserved


def _is_bluelink_vehicle_record(vehicle):
    if not isinstance(vehicle, dict):
        return False
    source = str(vehicle.get("soc_source") or vehicle.get("source") or "").strip().lower()
    return source in BLUELINK_SOC_SOURCES


def _vehicle_source_timestamp(vehicle):
    if not isinstance(vehicle, dict):
        return None
    if not _is_bluelink_vehicle_record(vehicle):
        return None
    # Der Abruf-/Dateizeitpunkt ist kein Fahrzeugmesswert. Sobald das neue
    # Feld vorhanden ist, darf ein fehlender Rohanker nicht auf einen anderen
    # Zeitstempel zurückfallen.
    if "soc_source_ts" in vehicle:
        return _safe_persisted_soc_timestamp(vehicle.get("soc_source_ts"))
    return None


def _latest_vehicle_source_timestamp(vehicles):
    source_timestamps = [
        timestamp
        for timestamp in (
            _vehicle_source_timestamp(vehicle)
            for vehicle in (vehicles if isinstance(vehicles, list) else [])
        )
        if timestamp is not None
    ]
    return max(source_timestamps) if source_timestamps else None


def _typed_refresh_error_code(error):
    """Typisierte Bibliotheksfehler (hyundai_kia_connect_api.exceptions) → Statuscode."""
    library = _bluelink_exceptions
    if library is None or error is None:
        return None
    for name, code in (
        ("AuthenticationOTPRequired", "otp_required"),
        ("ConsentRequiredError", "consent_required"),
        ("PINMissingError", "pin_missing"),
        ("AuthenticationError", "authentication_failed"),
        ("RateLimitingError", "rate_limited"),
        ("RequestTimeoutError", "timeout"),
    ):
        cls = getattr(library, name, None)
        if isinstance(cls, type) and isinstance(error, cls):
            return code
    return None


def _refresh_error_code(error):
    typed_code = getattr(error, "refresh_error_code", None)
    if typed_code == "vehicle_data_missing":
        return typed_code
    # Die Bibliothek kennt für diese Serverantwort keinen eigenen Fehlertyp.
    # Vor AuthenticationError prüfen, sonst verdeckt dessen Oberklasse den Grund.
    text = str(error or "").strip().lower()
    if "unusual activity" in text or "logged out" in text:
        return "account_logged_out"
    library_code = _typed_refresh_error_code(error)
    if library_code is not None:
        return library_code
    text = str(error or "").strip().lower()
    if "timed out" in text or "timeout" in text:
        return "timeout"
    if "too many requests" in text or "rate limit" in text or "429" in text:
        return "rate_limited"
    if "otp" in text or "one-time" in text:
        return "otp_required"
    if "consent" in text:
        return "consent_required"
    if "unauthorized" in text or "authentication" in text or "invalid token" in text or "signin failed" in text:
        return "authentication_failed"
    return "api_error"


BLUELINK_ERROR_HINTS = {
    "account_logged_out": (
        "Hyundai/Kia hat die Sitzung wegen ungewöhnlicher Aktivität abgemeldet. "
        "Einmal in der Hersteller-App anmelden."
    ),
    "otp_required": (
        "Das Hyundai/Kia-Konto verlangt einen Einmalcode (Zwei-Faktor-Anmeldung/OTP); das wird nicht unterstützt. "
        "Bitte die Zwei-Faktor-Anmeldung für dieses Konto deaktivieren oder ein Konto ohne Zwei-Faktor-Anmeldung verwenden."
    ),
    "consent_required": (
        "Das Hyundai/Kia-Konto verlangt eine Bestätigung der Nutzungsbedingungen. "
        "Bitte einmal in der Hersteller-App oder auf der Hersteller-Website anmelden und bestätigen."
    ),
    "pin_missing": "Das Hyundai/Kia-Konto verlangt eine PIN; bitte bluelink_pin im Konfigurations-Editor eintragen.",
    "authentication_failed": (
        "Anmeldung abgewiesen: Bitte Benutzer (E-Mail) und Passwort des Hyundai/Kia-Kontos im Konfigurations-Editor "
        "prüfen; eine aktive Zwei-Faktor-Anmeldung wird nicht unterstützt."
    ),
}


def _refresh_error_message(code, retry_at=None):
    if code == "account_logged_out":
        local_time = datetime.fromtimestamp(retry_at, ZoneInfo("Europe/Berlin")).strftime("%H:%M") if retry_at else "--:--"
        return (BLUELINK_ERROR_HINTS[code] + " E3DC-Control versucht es automatisch ab "
                + local_time + " erneut oder sofort über ‚Fahrzeug aufwecken‘.")
    return {
        "timeout": "Hyundai/Kia-Cloud antwortet nicht rechtzeitig.",
        "rate_limited": "Hyundai/Kia-Cloud begrenzt weitere Abfragen.",
        "authentication_failed": "Hyundai/Kia-Anmeldung wurde abgewiesen.",
        "otp_required": "Hyundai/Kia-Anmeldung verlangt einen Einmalcode (Zwei-Faktor); nicht unterstützt.",
        "consent_required": "Hyundai/Kia-Konto verlangt eine Bestätigung in der Hersteller-App.",
        "pin_missing": "Hyundai/Kia-Konto verlangt eine PIN.",
        "vehicle_data_missing": "Hyundai/Kia-Cloud lieferte keine verwertbaren Fahrzeugdaten.",
        "api_error": "Hyundai/Kia-Fahrzeugdaten konnten nicht aktualisiert werden.",
    }.get(str(code or ""), "Hyundai/Kia-Fahrzeugdaten konnten nicht aktualisiert werden.")


def _mask_account(user):
    """Kontoname nur verkürzt ins Log (erstes Zeichen + Domain)."""
    text = str(user or "").strip()
    if not text:
        return "(leer)"
    local, _, domain = text.partition("@")
    head = local[:1] + "***"
    return head + "@" + domain if domain else head


def _bluelink_secrets(config):
    """Werte, die niemals in Log oder Statusdatei erscheinen dürfen."""
    config = config if isinstance(config, dict) else {}
    return tuple(
        str(config.get(key) or "")
        for key in ("password", "pin", "user")
        if str(config.get(key) or "").strip()
    )


def _describe_bluelink_error(error, secrets=()):
    """Exception-Typ, HTTP-Status und (gekürzte, redigierte) Server-Antwort für das Log."""
    parts = [type(error).__name__]
    response = getattr(error, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        parts.append("HTTP %d" % status)
    text = str(error or "").strip()
    if not text and response is not None:
        text = str(getattr(response, "text", "") or "")
    for secret in sorted(secrets, key=len, reverse=True):
        if secret:
            text = re.sub(re.escape(secret), lambda match: "***", text, flags=re.IGNORECASE)
    # Erst schwärzen, dann kürzen: auch ein Geheimnis über der Kürzungsgrenze.
    text = " ".join(text.split())[:300]
    if text:
        parts.append(text)
    return " | ".join(parts)


def _bluelink_brand_code(value):
    """hyundai|kia → Markencode der Bibliothek; unbekannt/leer → Hyundai."""
    text = str(value or "").strip().lower()
    return BLUELINK_BRAND_CODES.get(text, BLUELINK_BRAND_CODES[BLUELINK_DEFAULT_BRAND])


def _credentials_complete(config):
    config = config if isinstance(config, dict) else {}
    return bool(str(config.get("user") or "").strip()) and bool(str(config.get("password") or ""))


def _credentials_fingerprint(config):
    """Vergleichswert (ohne Klartext im Log) – geänderte Zugangsdaten erzwingen eine neue Anmeldung."""
    config = config if isinstance(config, dict) else {}
    return (
        str(config.get("user") or "").strip(),
        str(config.get("password") or ""),
        str(config.get("pin") or "").strip(),
        _bluelink_brand_code(config.get("brand")),
    )


def _ensure_vehicle_manager(session, config):
    """Hält genau einen VehicleManager je Zugangsdaten im Prozess (Token-Cache; kein Re-Login je Intervall)."""
    fingerprint = _credentials_fingerprint(config)
    manager = session.get("vm")
    if manager is not None and session.get("fingerprint") != fingerprint:
        logger.info("Bluelink-Zugangsdaten geändert; Anmeldung wird erneuert.")
        manager = None
    if manager is None:
        user, password, pin, brand_code = fingerprint
        manager = VehicleManager(
            region=BLUELINK_REGION_EUROPE,
            brand=brand_code,
            username=user,
            password=password,
            pin=pin,
        )
        session["vm"] = manager
        session["fingerprint"] = fingerprint
        session["logged_in"] = False
        logger.info(
            "Bluelink-Anmeldung vorbereitet (Konto %s, Marke %s, PIN %s).",
            "gesetzt" if user else "leer",
            "Kia" if brand_code == BLUELINK_BRAND_CODES["kia"] else "Hyundai",
            "gesetzt" if pin else "leer",
        )
    return manager


def _handle_refresh_failure(session, error, retry_at=None):
    """Anmeldefehler verwerfen den Token-Cache (nächster Zyklus meldet sich neu an) und loggen den Handlungshinweis."""
    code = _refresh_error_code(error)
    if code in ("account_logged_out", "otp_required", "consent_required", "pin_missing", "authentication_failed"):
        session["vm"] = None
        session["fingerprint"] = None
        session["logged_in"] = False
        logger.error("Bluelink: %s", _refresh_error_message(code, retry_at) if code == "account_logged_out" else BLUELINK_ERROR_HINTS.get(code, _refresh_error_message(code)))
    return code


def build_refresh_status(
    old_data,
    vehicles,
    mode,
    attempt_ts,
    completed_ts,
    error=None,
    response_vehicles=None,
):
    """Erzeugt Diagnose ohne einen SoC-Quellanker künstlich zu verjüngen."""
    old_data = old_data if isinstance(old_data, dict) else {}
    old_vehicles = old_data.get("vehicles") if isinstance(old_data.get("vehicles"), list) else []
    old_source_ts = _latest_vehicle_source_timestamp(old_vehicles)
    source_ts = _latest_vehicle_source_timestamp(vehicles)
    response_items = (
        response_vehicles
        if isinstance(response_vehicles, list)
        else vehicles if isinstance(vehicles, list) else []
    )
    response_source_ts = _latest_vehicle_source_timestamp(response_items)
    response_vehicle_count = len([
        item for item in response_items if isinstance(item, dict)
    ])
    response_missing_source_count = len([
        item
        for item in response_items
        if isinstance(item, dict) and _vehicle_source_timestamp(item) is None
    ])
    response_source_complete = bool(
        response_vehicle_count > 0 and response_missing_source_count == 0
    )
    previous = old_data.get("refresh") if isinstance(old_data.get("refresh"), dict) else {}
    last_error = previous.get("last_error") if isinstance(previous.get("last_error"), dict) else None

    error_code = _refresh_error_code(error) if error is not None else None
    if error_code is not None:
        last_error = {
            "ts": int(completed_ts),
            "mode": str(mode),
            "code": error_code,
            "message": _refresh_error_message(error_code),
        }
        status = "failed"
        source_advanced = False
    else:
        source_advanced = response_source_ts is not None and (
            old_source_ts is None or response_source_ts > old_source_ts
        )
        if response_source_ts is None:
            status = "success_source_unknown"
        elif not response_source_complete:
            status = "success_source_partial"
        elif source_advanced:
            status = "success_source_advanced"
        else:
            status = "success_source_unchanged"

    last_error_ts = _safe_timestamp((last_error or {}).get("ts"))
    last_error_active = bool(
        last_error_ts is not None
        and (
            not response_source_complete
            or response_source_ts is None
            or response_source_ts <= last_error_ts
        )
    )
    return {
        "schema": BLUELINK_REFRESH_SCHEMA,
        "status": status,
        "mode": str(mode),
        "attempt_ts": int(attempt_ts),
        "completed_ts": int(completed_ts),
        "success": error is None,
        "source_ts": source_ts,
        "source_age_s": max(0, int(completed_ts) - source_ts) if source_ts is not None else None,
        "source_advanced": source_advanced,
        "response_source_ts": response_source_ts,
        "response_source_complete": response_source_complete,
        "response_vehicle_count": response_vehicle_count,
        "response_missing_source_count": response_missing_source_count,
        "error_code": error_code,
        "message": _refresh_error_message(error_code) if error_code is not None else None,
        "last_error": last_error,
        "last_error_active": last_error_active,
    }


def _read_existing_vehicle_data():
    try:
        if os.path.exists(VEHICLES_JSON_FILE):
            with open(VEHICLES_JSON_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
                return data if isinstance(data, dict) else {}
    except Exception:
        pass
    return {}


@contextmanager
def _vehicle_store_lock():
    """Serialisiert alle read-modify-write-Zyklen der gemeinsamen Fahrzeugdatei."""
    lock_path = f"{VEHICLES_JSON_FILE}.lock"
    os.makedirs(os.path.dirname(lock_path) or ".", exist_ok=True)
    descriptor = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o664)
    try:
        try:
            os.chmod(lock_path, 0o664)
        except OSError:
            pass
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        if fcntl is not None:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


_SOC_RUNTIME_FIELDS = frozenset({
    "soc", "source", "soc_source", "mqtt_vehicle_origin", "mqtt_vehicle_id",
    "soc_source_ts", "last_updated_at", "soc_rule_confirmed",
    "soc_rule_usable", "soc_stale", "soc_age_s", "soc_meta",
    "soc_source_previous", "soc_source_original", "soc_source_class",
    "soc_cache_ts", "raw_soc_ts", "is_interpolated",
})


def _soc_truth_rank(vehicle):
    """Ordnet konkurrierende Producer nach Regelwahrheit, Anker und Quelle."""
    if not isinstance(vehicle, dict):
        return (0, -1, 0)
    source = str(
        vehicle.get("soc_source") or vehicle.get("source") or ""
    ).strip().lower()
    source_ts = (
        _safe_persisted_soc_timestamp(vehicle.get("soc_source_ts"))
        if "soc_source_ts" in vehicle else None
    )
    confirmed = bool(
        vehicle.get("soc_rule_confirmed") is True
        and vehicle.get("soc_stale") is not True
        and vehicle.get("soc_rule_usable") is not False
        and source_ts is not None
    )
    if source in BLUELINK_SOC_SOURCES:
        source_priority = 300
    elif source == "mqtt":
        source_priority = 250
    elif source.startswith("openwb") or source.startswith("wallbox"):
        source_priority = 200
    elif source:
        source_priority = 100
    else:
        source_priority = 0
    return (
        1 if confirmed else 0,
        int(source_ts) if source_ts is not None else -1,
        source_priority,
    )


def _merge_cloud_vehicles_with_current(
    current_data,
    cloud_vehicles,
    preserve_missing_cloud=False,
):
    """Erhält statische Felder und fremde Producer ohne alte SoC-Wahrheit."""
    merged = [dict(vehicle) for vehicle in cloud_vehicles if isinstance(vehicle, dict)]
    current_vehicles = (
        current_data.get("vehicles")
        if isinstance(current_data, dict) else None
    )
    current_vehicles = current_vehicles if isinstance(current_vehicles, list) else []

    by_id = {str(vehicle.get("id")): vehicle for vehicle in merged}
    for current_vehicle in current_vehicles:
        if not isinstance(current_vehicle, dict):
            continue
        target = by_id.get(str(current_vehicle.get("id")))
        if target is None:
            continue
        current_rank = _soc_truth_rank(current_vehicle)
        incoming_rank = _soc_truth_rank(target)
        if current_rank[0] > incoming_rank[0] or (
            current_rank[0] == incoming_rank[0] == 1
            and current_rank >= incoming_rank
        ):
            # Eine schwächere, ankerlose oder ältere Cloudantwort darf weder
            # einen neueren Cloudwert noch einen parallel empfangenen,
            # bestätigten MQTT-Wert derselben Fahrzeug-ID zurückrollen.
            # Nicht-SoC-Stammdaten des aktuellen API-Objekts bleiben nutzbar.
            for key in _SOC_RUNTIME_FIELDS:
                if key in current_vehicle:
                    target[key] = current_vehicle[key]
                else:
                    target.pop(key, None)
        for key, value in current_vehicle.items():
            if key not in target and key not in _SOC_RUNTIME_FIELDS:
                target[key] = value

    known_ids = set(by_id)
    for current_vehicle in _preserve_vehicles_fail_closed(current_data):
        current_id = str(current_vehicle.get("id"))
        if ((not _is_bluelink_vehicle_record(current_vehicle)
             or preserve_missing_cloud)
            and current_id not in known_ids):
            merged.append(current_vehicle)
            known_ids.add(current_id)
    return merged


def _commit_bluelink_result(
    cloud_vehicles,
    mode,
    attempt_ts,
    completed_ts,
    error=None,
    preserve_missing_cloud=False,
):
    """Liest unter derselben Sperre neu ein und schreibt genau einen Merge."""
    with _vehicle_store_lock():
        current_data = _read_existing_vehicle_data()
        if error is None:
            vehicles = _merge_cloud_vehicles_with_current(
                current_data,
                cloud_vehicles,
                preserve_missing_cloud=preserve_missing_cloud,
            )
        else:
            vehicles = _preserve_vehicles_fail_closed(current_data)
        refresh = build_refresh_status(
            current_data,
            vehicles,
            mode,
            attempt_ts,
            completed_ts,
            error=error,
            response_vehicles=cloud_vehicles if error is None else None,
        )
        data = {
            "ts": int(completed_ts),
            "source": "bluelink",
            "refresh": refresh,
            "vehicles": vehicles,
        }
        if error is not None:
            data["error"] = refresh["message"]
        write_json_atomic(VEHICLES_JSON_FILE, data)
        return data

def write_json_atomic(path, payload, indent=None):
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    stamp = getattr(time, "time_ns", lambda: int(time.time() * 1000000000))()
    tmp = f"{path}.tmp.{os.getpid()}.{stamp}.{id(payload)}"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=indent)
        os.replace(tmp, path)
    finally:
        try:
            if os.path.exists(tmp):
                os.remove(tmp)
        except OSError:
            pass

def load_bluelink_config():
    """Lädt Zugangsdaten (Benutzer/Passwort/PIN/Marke), VIN und Heimat-Koordinaten aus der V4-Konfig mit TXT-Fallback.

    Anmeldung mit Benutzer/Passwort statt Refresh-Token. Die Zugangsdaten kommen ausschließlich aus der
    zugriffsgeschützten V4-Konfiguration; ein noch vorhandener alter Schlüssel bluelink_refresh_token wird nicht mehr
    verwendet und nur als Hinweis gemeldet (legacy_token_present).
    """
    config = {
        'user': None, 'password': None, 'pin': '', 'brand': BLUELINK_DEFAULT_BRAND,
        'vin': None, 'car_name': None, 'bluelink_interval': '15', 'hoehe': '0', 'laenge': '0',
        'legacy_token_present': False,
    }
    v4_bound_keys = set()
    if os.path.exists(V4_CONFIG_FILE):
        try:
            with open(V4_CONFIG_FILE, 'r', encoding='utf-8') as f:
                v4 = json.load(f)
            if isinstance(v4, dict):
                sub_cfg = v4.get('config') if isinstance(v4.get('config'), dict) else {}
                for legacy_source in (v4, sub_cfg):
                    if str(legacy_source.get('bluelink_refresh_token') or '').strip():
                        config['legacy_token_present'] = True
                for source_key, target_key, stringify in (
                    ('bluelink_user', 'user', False),
                    ('bluelink_password', 'password', False),
                    ('bluelink_pin', 'pin', True),
                    ('bluelink_brand', 'brand', True),
                    ('bluelink_vin', 'vin', False),
                    ('bluelink_car_name', 'car_name', False),
                    ('bluelink_interval', 'bluelink_interval', True),
                    ('hoehe', 'hoehe', True),
                    ('laenge', 'laenge', True),
                ):
                    if source_key in v4:
                        value = v4.get(source_key)
                        v4_bound_keys.add(source_key)
                        config[target_key] = (
                            '' if value is None else str(value)
                        ) if stringify else value
                    elif source_key in sub_cfg:
                        value = sub_cfg.get(source_key)
                        if value is not None and str(value).strip() != '':
                            config[target_key] = str(value) if stringify else value
                    else:
                        continue
        except Exception as e:
            logger.warning(f"V4-Konfig konnte nicht gelesen werden: {e}")

    if not os.path.exists(CONFIG_FILE):
        return config

    with open(CONFIG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
        for line in f:
            if '=' in line and not line.strip().startswith('#'):
                key, value = [x.strip() for x in line.split('=', 1)]
                key = key.lower()
                if key == 'bluelink_refresh_token' and value:
                    config['legacy_token_present'] = True
                elif key == 'bluelink_vin' and key not in v4_bound_keys and not config['vin']:
                    config['vin'] = value
                elif key == 'bluelink_car_name' and key not in v4_bound_keys and not config['car_name']:
                    config['car_name'] = value
                elif key == 'bluelink_interval' and key not in v4_bound_keys and config['bluelink_interval'] == '15':
                    config['bluelink_interval'] = value
                elif key == 'hoehe' and key not in v4_bound_keys and config['hoehe'] == '0':
                    config['hoehe'] = value
                elif key == 'laenge' and key not in v4_bound_keys and config['laenge'] == '0':
                    config['laenge'] = value
    return config

BLUELINK_RETRY_SCHEMA = "bluelink_retry_v1"
BLUELINK_BACKOFF_CODES = {"authentication_failed", "otp_required", "consent_required", "pin_missing", "rate_limited"}


def plan_bluelink_refresh(state, *, now, interval_minutes, fingerprint, manual=False, outcome=None):
    """Reine Abrufplanung; alle Zeitentscheidungen hängen an der übergebenen Uhr."""
    previous = state if isinstance(state, dict) else {}
    interval = max(5, int(interval_minutes)) * 60
    changed = previous.get("credential_hmac") != fingerprint
    result = dict(previous) if previous.get("schema") == BLUELINK_RETRY_SCHEMA else {}
    if changed or not result:
        result = {"schema": BLUELINK_RETRY_SCHEMA, "credential_hmac": fingerprint,
                  "failures": 0, "next_attempt_ts": float(now), "pause_until": 0, "error_code": None}
    if manual:
        # Nur den Termin aufheben; die Fehlerstufe endet erst mit einem Erfolg.
        result.update(next_attempt_ts=float(now), pause_until=0, error_code=None)
    if outcome is not None:
        result["error_code"] = None if str(outcome).startswith("success_") else outcome
        result["pause_until"] = 0
        delay = interval
        if str(outcome).startswith("success_"):
            result["failures"] = 0
        elif outcome == "account_logged_out":
            result["pause_until"] = float(now) + 6 * 3600
            delay = max(interval, 6 * 3600)
        elif outcome in BLUELINK_BACKOFF_CODES:
            stage = min(6, max(0, int(result.get("failures", 0))) + 1)
            result["failures"] = stage
            delay = max(interval, (30, 60, 120, 240, 360, 360)[stage - 1] * 60)
        result["next_attempt_ts"] = float(now) + delay
    return result


def _retry_paths():
    directory = os.path.dirname(V4_CONFIG_FILE)
    return os.path.join(directory, "bluelink_retry.json"), os.path.join(directory, ".bluelink_retry.key")


def _retry_fingerprint(config):
    """Persistenter HMAC mit lokalem Zufallsschlüssel; keine Wörterbuch-Hashes."""
    _, key_path = _retry_paths()
    os.makedirs(os.path.dirname(key_path), exist_ok=True)
    try:
        fd = os.open(key_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
    except FileExistsError:
        pass
    else:
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(32))
    fd = os.open(key_path, os.O_RDONLY | os.O_NOFOLLOW)
    with os.fdopen(fd, "rb") as handle:
        key_stat = os.fstat(handle.fileno())
        if not stat.S_ISREG(key_stat.st_mode):
            raise OSError("Ungültiger Bluelink-Pausenschlüssel")
        if stat.S_IMODE(key_stat.st_mode) != 0o600:
            os.fchmod(handle.fileno(), 0o600)
        key = handle.read(33)
    if len(key) != 32:
        raise OSError("Ungültiger Bluelink-Pausenschlüssel")
    material = json.dumps([*_credentials_fingerprint(config),
                           config.get("region", BLUELINK_REGION_EUROPE)], ensure_ascii=False).encode("utf-8")
    return hmac.new(key, material, hashlib.sha256).hexdigest()


def _load_retry_state():
    try:
        with open(_retry_paths()[0], encoding="utf-8") as handle:
            state = json.load(handle)
        return state if isinstance(state, dict) else {}
    except FileNotFoundError:
        return {}


def _save_retry_state(state):
    # Gleicher Dienst als einziger Writer; temporäre Datei von Anfang an 0600.
    path, _ = _retry_paths()
    temporary = path + ".tmp." + str(os.getpid())
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            json.dump(state, handle)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _manual_force_refresh_requested(now):
    try:
        with open(FORCE_FLAG_FILE, encoding="utf-8") as handle:
            raw = handle.read(4096).strip()
        # Nur force_soc aus dem Web schreibt einen echten, frischen Zeitstempel.
        # Das automatische „1“ und alte leere Flags beenden keine Pause.
        return raw.isdigit() and int(raw) > 1 and -300 <= now - int(raw) <= 300
    except OSError:
        return False


def _discard_paused_force_request():
    try:
        with open(FORCE_FLAG_FILE, encoding="utf-8") as handle:
            raw = handle.read(4096)
        try:
            request = json.loads(raw)
        except ValueError:
            request = None
        if (isinstance(request, dict) and request.get("schema") == "vehicle_soc_refresh_request_v1"
                and request.get("source") == "openwb_plug" and type(request.get("wb")) is int
                and request["wb"] in (1, 2) and request.get("plug_session_id")):
            path = os.path.join(os.path.dirname(FORCE_FLAG_FILE), "vehicle_soc_cloud_refresh.json")
            try:
                with open(path, encoding="utf-8") as handle:
                    state = json.load(handle)
                sessions = state.get("sessions", {})
                if sessions.get(str(request["wb"])) == request["plug_session_id"]:
                    sessions.pop(str(request["wb"]), None)
                    write_json_atomic(path, state)
            except (OSError, ValueError, AttributeError):
                pass
        # Einen zwischenzeitlich neu eingegangenen manuellen Auftrag erhalten.
        with open(FORCE_FLAG_FILE, encoding="utf-8") as handle:
            if handle.read(4096) == raw:
                os.unlink(FORCE_FLAG_FILE)
    except FileNotFoundError:
        pass


def _publish_refresh_pause(state, now):
    """Lebenszeichen ohne Anmeldung; SoC-Quellanker und Bestandsdaten erhalten."""
    with _vehicle_store_lock():
        data = _read_existing_vehicle_data()
        refresh = dict(data.get("refresh") or {})
        code = state.get("error_code")
        message = _refresh_error_message(code, state.get("next_attempt_ts"))
        refresh.update({"schema": BLUELINK_REFRESH_SCHEMA, "status": "paused",
                        "mode": refresh.get("mode", "cached"), "success": False,
                        "pause_reason": code, "pause_until": state.get("pause_until", 0),
                        "next_attempt_ts": state["next_attempt_ts"], "error_code": code,
                        "message": message, "last_error_active": bool(code)})
        if isinstance(refresh.get("last_error"), dict) and code == "account_logged_out":
            refresh["last_error"]["message"] = message
        data.update({"ts": int(now), "source": "bluelink", "refresh": refresh, "error": message})
        write_json_atomic(VEHICLES_JSON_FILE, data)


def main():
    if VehicleManager is None:
        logger.error(f"Fehler beim Laden der Bluelink-API: {IMPORT_ERROR}")
        logger.error("Bitte prüfe die Installation manuell auf der Konsole.")
        return
        
    last_heartbeat = 0
    missing_credentials_logged = False
    legacy_token_logged = False
    # VehicleManager samt Token im Prozess halten; Re-Login nur nach Anmeldefehler oder Änderung der Zugangsdaten.
    session = {"vm": None, "fingerprint": None, "logged_in": False}

    while True:
        config = load_bluelink_config()
        vin = config.get('vin')
        try: interval = int(config.get('bluelink_interval', 15))
        except ValueError: interval = 15
        
        if interval < 5: interval = 5 # Absicherung für die API

        if not _credentials_complete(config):
            if config.get('legacy_token_present') and not legacy_token_logged:
                logger.warning(
                    "Der bisherige Bluelink-Refresh-Token wird nicht mehr verwendet; bitte Benutzer (E-Mail) und "
                    "Passwort des Hyundai/Kia-Kontos im Konfigurations-Editor hinterlegen."
                )
                legacy_token_logged = True
            if not missing_credentials_logged:
                logger.info("Bluelink ist ohne Zugangsdaten deaktiviert; warte auf Konfiguration.")
                missing_credentials_logged = True
            session["vm"] = None
            session["fingerprint"] = None
            session["logged_in"] = False
            time.sleep(60)
            continue
        missing_credentials_logged = False
            
        now = time.time()
        force_requested = os.path.exists(FORCE_FLAG_FILE)
        try:
            fingerprint = _retry_fingerprint(config)
            stored = _load_retry_state()
            retry = plan_bluelink_refresh(stored, now=now, interval_minutes=interval,
                                          fingerprint=fingerprint,
                                          manual=force_requested and _manual_force_refresh_requested(now))
            if retry != stored:
                _save_retry_state(retry)
            login_paused = (retry.get("error_code") == "account_logged_out"
                            or retry.get("error_code") in BLUELINK_BACKOFF_CODES)
            if now < retry["next_attempt_ts"] and not (force_requested and not login_paused):
                if force_requested:
                    _discard_paused_force_request()
                if login_paused and now - last_heartbeat >= min(interval * 60, 900):
                    _publish_refresh_pause(retry, now)
                    last_heartbeat = now
                time.sleep(5)
                continue
        except (OSError, ValueError, TypeError):
            # Ohne sichere Persistenz keine Anmeldung, die eine Pause umgehen könnte.
            logger.error("Bluelink-Pausenzustand nicht lesbar oder schreibbar; Anmeldung ausgesetzt.")
            time.sleep(60)
            continue

        if force_requested or now >= retry["next_attempt_ts"]:
            attempt_ts = int(time.time())
            refresh_mode = "force" if force_requested else "cached"
            # Jeder Cloudversuch setzt den nächsten regulären Termin. Ein
            # fehlgeschlagener Cached-Abruf darf sonst alle fünf Sekunden neu
            # anlaufen und API-Limit sowie Fahrzeugbatterie belasten.
            try:
                force_request = _read_force_refresh_request() if force_requested else None
                requested_vehicle_id = None
                # Benutzer/Passwort-Login (Region 1 = Europa, Marke aus bluelink_brand); der Manager bleibt
                # im Prozess, check_and_refresh_token() meldet nur beim ersten Mal an und erneuert sonst den Token bei Ablauf.
                vm = _ensure_vehicle_manager(session, config)
                vm.check_and_refresh_token()
                if not session.get("logged_in"):
                    session["logged_in"] = True
                    logger.info("Bluelink-Anmeldung erfolgreich (%d Fahrzeug(e) im Konto).", len(getattr(vm, "vehicles", None) or {}))
                if force_requested:
                    logger.info(
                        "Automatischer SoC-Abruf für das angesteckte Fahrzeug angefordert."
                        if force_request is not None
                        else "Manueller Force-Refresh angefordert. Wecke Fahrzeug auf..."
                    )
                    requested_vehicle_id = _apply_force_refresh_request(vm, force_request, vin=vin)
                    try: os.remove(FORCE_FLAG_FILE)
                    except: pass
                else:
                    vm.update_all_vehicles_with_cached_state()

                if not vm.vehicles:
                    raise BluelinkVehicleDataMissing("Kein Fahrzeug im Konto")
                else:
                    vehicles_out = []
                    for v_id, target_vehicle in vm.vehicles.items():
                        if requested_vehicle_id is not None and str(v_id) != requested_vehicle_id:
                            continue
                        if vin and target_vehicle.vin != vin:
                            continue
                            
                        soc = _normalize_soc_percent(
                            target_vehicle.ev_battery_percentage
                        )
                        if soc is None:
                            continue
                        
                        v_data = {
                            "id": v_id,
                            "soc": soc,
                            "soc_source": "bluelink",
                        }
                        
                        name = getattr(target_vehicle, 'nickname', None)
                        if not name: name = getattr(target_vehicle, 'model_name', None)
                        if not name: name = getattr(target_vehicle, 'name', f"Fahrzeug {len(vehicles_out)+1}")
                        
                        custom_name = config.get('car_name')
                        if custom_name and (len(vm.vehicles) == 1 or (vin and target_vehicle.vin == vin)):
                            name = custom_name
                            
                        v_data["name"] = name
                        
                        is_plugged_raw = getattr(target_vehicle, 'ev_battery_is_plugged_in', None)
                        if is_plugged_raw is None: v_data["is_plugged_in"] = True
                        elif isinstance(is_plugged_raw, bool): v_data["is_plugged_in"] = is_plugged_raw
                        else:
                            try: v_data["is_plugged_in"] = int(is_plugged_raw) > 0
                            except: v_data["is_plugged_in"] = True
                            
                        source_ts = _raw_vehicle_soc_timestamp(target_vehicle)
                        v_data["soc_source_ts"] = source_ts
                        v_data["last_updated_at"] = source_ts
                        v_data["soc_rule_confirmed"] = source_ts is not None
                        
                        try: v_data["range_km"] = getattr(target_vehicle, 'ev_driving_range', None)
                        except: pass
                        try: 
                            bat = getattr(target_vehicle, 'car_battery_percentage', None)
                            if bat is None: bat = getattr(target_vehicle, 'ev_battery_twelve_volt_percentage', None)
                            if bat is None and hasattr(target_vehicle, 'data'):
                                try: bat = target_vehicle.data['vehicleStatus']['battery']['batSoc']
                                except: pass
                            v_data["bat_12v"] = bat
                        except: pass
                        try: 
                            odo = getattr(target_vehicle, 'odometer', None)
                            if odo is None and hasattr(target_vehicle, 'data'):
                                try: odo = target_vehicle.data['vehicleStatus']['odometer']
                                except: pass
                            if odo is not None: v_data["odometer"] = odo
                        except: pass
                        try: 
                            tsoc = getattr(target_vehicle, 'ev_target_soc_ac', None)
                            if tsoc is None: tsoc = getattr(target_vehicle, 'ev_target_soc', None)
                            if isinstance(tsoc, list) and len(tsoc) > 0: tsoc = tsoc[0]
                            if tsoc is None and hasattr(target_vehicle, 'data'):
                                try: 
                                    tsoc_list = target_vehicle.data['vehicleStatus']['evStatus']['reservChargeInfos']['targetSOClist']
                                    for t in tsoc_list:
                                        if t.get('plugType') == 1:
                                            tsoc = t.get('targetSOClevel')
                                            break
                                    if tsoc is None: tsoc = tsoc_list[0]['targetSOClevel']
                                except: pass
                                if tsoc is None:
                                    try: 
                                        tsoc_list = target_vehicle.data['vehicleStatus']['evStatus']['reservChargeStInfo']['targetSocList']
                                        for t in tsoc_list:
                                            if t.get('plugType') == 1:
                                                tsoc = t.get('targetSocLevel')
                                                break
                                        if tsoc is None: tsoc = tsoc_list[0]['targetSocLevel']
                                    except: pass
                            if tsoc is not None: v_data["target_soc"] = tsoc
                        except: pass
                        try: v_data["is_locked"] = getattr(target_vehicle, 'is_locked', None)
                        except: pass
                        try: v_data["air_ctrl"] = getattr(target_vehicle, 'air_control_is_on', None)
                        except: pass
                        
                        try: 
                            v_stat = target_vehicle.data.get('vehicleStatus') or {}
                            tire = v_stat.get('tirePressureLamp') or {}
                            v_data["tire_warning"] = bool(int(tire.get('tirePressureLampAll', 0)) > 0)
                        except Exception as e: pass
                        
                        try: 
                            v_stat = target_vehicle.data.get('vehicleStatus') or {}
                            doors = v_stat.get('doorOpen') or {}
                            any_door = False
                            for v in doors.values():
                                try:
                                    if int(v) > 0: any_door = True
                                except: pass
                            trunk = bool(v_stat.get('trunkOpen', False))
                            hood = bool(v_stat.get('hoodOpen', False))
                            v_data["doors_open"] = bool(any_door or trunk or hood)
                        except Exception as e: pass
                        
                        loc = getattr(target_vehicle, 'location', None)
                        car_lat = getattr(target_vehicle, 'location_latitude', None)
                        car_lon = getattr(target_vehicle, 'location_longitude', None)
                        if loc and not car_lat:
                            if hasattr(loc, 'latitude'):
                                car_lat = loc.latitude
                                car_lon = loc.longitude
                            elif isinstance(loc, dict):
                                car_lat = loc.get('latitude')
                                car_lon = loc.get('longitude')
                        if not car_lat and hasattr(target_vehicle, 'data'):
                            try:
                                car_lat = target_vehicle.data['vehicleLocation']['coord']['lat']
                                car_lon = target_vehicle.data['vehicleLocation']['coord']['lon']
                            except: pass
                        if car_lat and car_lon:
                            v_data["car_lat"] = car_lat
                            v_data["car_lon"] = car_lon
                            try:
                                home_lat = float(config.get('hoehe', 0))
                                home_lon = float(config.get('laenge', 0))
                                if home_lat and home_lon:
                                    dLat = math.radians(car_lat - home_lat); dLon = math.radians(car_lon - home_lon)
                                    a = math.sin(dLat/2)**2 + math.cos(math.radians(home_lat)) * math.cos(math.radians(car_lat)) * math.sin(dLon/2)**2
                                    c = 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))
                                    dist = 6371.0 * c
                                    v_data["is_at_home"] = bool(dist <= 0.5)
                            except Exception as e: pass
                            
                        vehicles_out.append(v_data)

                    if not vehicles_out:
                        raise BluelinkVehicleDataMissing(
                            "Konfiguriertes Fahrzeug oder dessen SoC fehlt"
                        )

                    completed_ts = int(time.time())
                    data = _commit_bluelink_result(
                        vehicles_out,
                        refresh_mode,
                        attempt_ts,
                        completed_ts,
                        preserve_missing_cloud=requested_vehicle_id is not None or not bool(vin),
                    )
                    retry = plan_bluelink_refresh(retry, now=completed_ts, interval_minutes=interval,
                                                  fingerprint=fingerprint, outcome=data["refresh"]["status"])
                    _save_retry_state(retry)
                    logger.info(f"{len(data['vehicles'])} Fahrzeuge erfolgreich aktualisiert (Force={force_requested}).")
                    
                    # --- DEBUG: Alle rohen Fahrzeugdaten für den Nutzer speichern ---
            except Exception as e:
                # Typ, HTTP-Status und Server-Antwort ohne Geheimnisse; Anmeldefehler verwerfen den Token-Cache.
                logger.error("Bluelink-Abruf fehlgeschlagen: %s", _describe_bluelink_error(e, _bluelink_secrets(config)))
                retry = plan_bluelink_refresh(retry, now=time.time(), interval_minutes=interval,
                                              fingerprint=fingerprint, outcome=_refresh_error_code(e))
                _save_retry_state(retry)
                _handle_refresh_failure(session, e, retry["next_attempt_ts"])
                
                # Letzten bestätigten SoC erhalten, aber Versuch und Fehler
                # getrennt davon publizieren. Der neue Datei-Zeitpunkt ist
                # ausdrücklich kein neuer Fahrzeugstand.
                completed_ts = int(time.time())
                failed = _commit_bluelink_result(
                    [],
                    refresh_mode,
                    attempt_ts,
                    completed_ts,
                    error=e,
                )
                if retry.get("error_code") == "account_logged_out":
                    _publish_refresh_pause(retry, completed_ts)
                last_heartbeat = completed_ts

                if force_requested:
                    try: os.remove(FORCE_FLAG_FILE)
                    except: pass

        time.sleep(5)

if __name__ == "__main__":
    main()
