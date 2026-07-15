#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Read-only preliminary market-value-solar monitor.

The monitor estimates the current monthly Marktwert Solar trend from
15-minute solar extrapolation data and 15-minute spot prices. It writes
diagnostics only; it must never create a control owner or storage command.
"""

import csv
import io
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except Exception:  # pragma: no cover - Python < 3.9 fallback
    ZoneInfo = None


SCHEMA_VERSION = "market_value_solar_v1"
SLOT_MS = 15 * 60 * 1000
NETZTRANSPARENZ_TOKEN_URL = "https://identity.netztransparenz.de/users/connect/token"
NETZTRANSPARENZ_DATA_URL = "https://ds.netztransparenz.de/api/v1/data/hochrechnung/Solar/{date_from}/{date_to}"
NETZTRANSPARENZ_SOURCE = "netztransparenz_hochrechnung_solar"


def _safe_float(value, default=None):
    if value is None:
        return default
    if isinstance(value, bool):
        return default
    try:
        text = str(value).strip()
        if text == "":
            return default
        text = text.replace("\u00a0", "").replace(" ", "")
        if "," in text and "." in text:
            text = text.replace(".", "").replace(",", ".")
        else:
            text = text.replace(",", ".")
        return float(text)
    except Exception:
        return default


def _safe_int(value, default=None):
    number = _safe_float(value, None)
    if number is None:
        return default
    try:
        return int(round(number))
    except Exception:
        return default


def _config_bool(config, key, default=False):
    raw = (config or {}).get(key, default)
    if isinstance(raw, bool):
        return raw
    text = str(raw).strip().lower()
    if text == "":
        return bool(default)
    return text in ("1", "true", "yes", "on", "ja", "ein", "aktiv")


def _berlin_tz():
    if ZoneInfo is None:
        return timezone(timedelta(hours=1))
    try:
        return ZoneInfo("Europe/Berlin")
    except Exception:
        return timezone(timedelta(hours=1))


def _iso_from_ms(ms):
    if not ms:
        return None
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat()
    except Exception:
        return None


def _parse_datetime_ms(value, date_hint=None):
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 10_000_000_000:
            return int(round(number))
        if number > 100_000_000:
            return int(round(number * 1000.0))
        return None

    text = str(value).strip()
    if not text:
        return None
    number = _safe_float(text, None)
    if number is not None and re.fullmatch(r"-?\d+(?:[.,]\d+)?", text):
        return _parse_datetime_ms(number)

    if date_hint and re.fullmatch(r"\d{1,2}:\d{2}(?::\d{2})?", text):
        text = f"{date_hint} {text}"

    cleaned = text.replace("Z", "+00:00")
    cleaned = re.sub(r"\s+", " ", cleaned)
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M%z",
        "%Y-%m-%d %H:%M:%S%z",
        "%Y-%m-%d %H:%M%z",
        "%d.%m.%Y %H:%M:%S%z",
        "%d.%m.%Y %H:%M%z",
    ):
        try:
            return int(datetime.strptime(cleaned, fmt).timestamp() * 1000)
        except Exception:
            pass

    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=_berlin_tz())
        return int(dt.timestamp() * 1000)
    except Exception:
        pass

    for fmt in (
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%d.%m.%Y %H:%M:%S",
        "%d.%m.%Y %H:%M",
        "%d.%m.%y %H:%M:%S",
        "%d.%m.%y %H:%M",
    ):
        try:
            return int(datetime.strptime(cleaned, fmt).replace(tzinfo=_berlin_tz()).timestamp() * 1000)
        except Exception:
            pass
    return None


def _month_bounds_ms(now_ms):
    now_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=_berlin_tz())
    start = now_dt.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    if start.month == 12:
        end = start.replace(year=start.year + 1, month=1)
    else:
        end = start.replace(month=start.month + 1)
    return int(start.timestamp() * 1000), int(end.timestamp() * 1000), start.strftime("%Y-%m")


def _normalise_key(key):
    text = str(key or "").strip().lower()
    replacements = {
        "ä": "ae",
        "ö": "oe",
        "ü": "ue",
        "ß": "ss",
    }
    for src, dst in replacements.items():
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", "", text)


def _row_value(row, keys):
    if not isinstance(row, dict):
        return None
    wanted = {_normalise_key(k) for k in keys}
    for key, value in row.items():
        if _normalise_key(key) in wanted:
            return value
    return None


def _extract_start_end_ms(row):
    date_hint = _row_value(row, ("date", "datum", "tag"))
    start = (
        _row_value(row, ("start_timestamp", "startts", "timestamp", "zeitstempel", "start", "beginn", "von"))
        or _row_value(row, ("datefrom", "gueltigvon", "zeit"))
    )
    if start is None and date_hint is not None:
        time_part = _row_value(row, ("uhrzeit", "zeit", "von", "beginn"))
        if time_part is not None:
            start = f"{date_hint} {time_part}"
    start_ms = _parse_datetime_ms(start, date_hint=date_hint)

    end = _row_value(row, ("end_timestamp", "endts", "end", "ende", "bis", "dateto", "gueltigbis"))
    if end is None and date_hint is not None:
        time_part = _row_value(row, ("bis", "ende"))
        if time_part is not None:
            end = f"{date_hint} {time_part}"
    end_ms = _parse_datetime_ms(end, date_hint=date_hint)
    if start_ms is not None and (end_ms is None or end_ms <= start_ms):
        end_ms = start_ms + SLOT_MS
    return start_ms, end_ms


def _header_unit_scale(header):
    key = str(header or "").lower()
    if "gw" in key:
        return 1000.0
    if "kw" in key and "kwh" not in key:
        return 0.001
    return 1.0


def _extract_solar_power_mw(row):
    if not isinstance(row, dict):
        return None
    tso_sum = 0.0
    tso_count = 0
    for key, value in row.items():
        norm = _normalise_key(key)
        if norm.startswith(("50hertz", "amprion", "tennet", "transnetbw")):
            number = _safe_float(value, None)
            if number is not None:
                tso_sum += number * _header_unit_scale(key)
                tso_count += 1
    if tso_count:
        return tso_sum

    candidates = (
        "solar_mw",
        "solarmw",
        "solar",
        "hochrechnungsurplus",
        "hochrechnung",
        "leistungmw",
        "powermw",
        "value",
        "wert",
    )
    for key, value in row.items():
        norm = _normalise_key(key)
        if norm in {_normalise_key(c) for c in candidates} or ("solar" in norm and "mw" in norm):
            number = _safe_float(value, None)
            if number is not None:
                return number * _header_unit_scale(key)
    return None


def _extract_energy_mwh(row):
    if not isinstance(row, dict):
        return None
    for key, value in row.items():
        norm = _normalise_key(key)
        if norm in ("solarenergymwh", "energymwh", "mwh") or ("energy" in norm and "mwh" in norm):
            return _safe_float(value, None)
        if norm in ("solarenergykwh", "energykwh", "kwh") or ("energy" in norm and "kwh" in norm):
            number = _safe_float(value, None)
            return None if number is None else number / 1000.0
    return None


def parse_netztransparenz_solar_csv(text):
    """Parse common Netztransparenz CSV variants into row dictionaries."""
    if not isinstance(text, str):
        return []
    clean = text.lstrip("\ufeff").strip()
    if not clean:
        return []
    first_line = clean.splitlines()[0] if clean.splitlines() else ""
    delimiter = ";" if first_line.count(";") >= first_line.count(",") else ","
    rows = []
    try:
        reader = csv.DictReader(io.StringIO(clean), delimiter=delimiter)
        for row in reader:
            if isinstance(row, dict):
                rows.append(row)
    except Exception:
        return []
    return rows


def normalise_solar_slots(solar_data):
    if isinstance(solar_data, str):
        rows = parse_netztransparenz_solar_csv(solar_data)
    elif isinstance(solar_data, dict):
        for key in ("slots", "data", "values", "items"):
            if isinstance(solar_data.get(key), list):
                rows = solar_data.get(key)
                break
        else:
            rows = [solar_data]
    elif isinstance(solar_data, list):
        rows = solar_data
    else:
        rows = []

    by_start = {}
    for raw in rows:
        if not isinstance(raw, dict):
            continue
        start_ms, end_ms = _extract_start_end_ms(raw)
        if start_ms is None or end_ms is None or end_ms <= start_ms:
            continue
        energy_mwh = _extract_energy_mwh(raw)
        power_mw = _extract_solar_power_mw(raw)
        if energy_mwh is None and power_mw is None:
            continue
        if energy_mwh is None:
            energy_mwh = max(0.0, power_mw) * ((end_ms - start_ms) / 3_600_000.0)
        by_start[int(start_ms)] = {
            "start_timestamp": int(start_ms),
            "end_timestamp": int(end_ms),
            "power_mw": None if power_mw is None else round(power_mw, 6),
            "energy_mwh": max(0.0, float(energy_mwh)),
        }
    return [by_start[key] for key in sorted(by_start)]


def _price_ct_from_slot(slot):
    if not isinstance(slot, dict):
        return None, "missing"
    for key in ("direct_marketing_market_price_ct", "spot_market_price_ct", "market_price_ct", "price_ct", "market_ct"):
        number = _safe_float(slot.get(key), None)
        if number is not None:
            return number, key
    for key in ("direct_marketing_marketprice", "spot_marketprice", "marketprice"):
        number = _safe_float(slot.get(key), None)
        if number is not None:
            return number / 10.0, key
    return None, "missing"


def normalise_price_slots(price_data):
    if not isinstance(price_data, list):
        return {}
    slots = {}
    for raw in price_data:
        if not isinstance(raw, dict):
            continue
        start = _safe_int(raw.get("start_timestamp"), None)
        end = _safe_int(raw.get("end_timestamp"), None)
        if start is None or start <= 0:
            continue
        if end is None or end <= start:
            end = start + SLOT_MS
        price_ct, price_key = _price_ct_from_slot(raw)
        if price_ct is None:
            continue
        source = str(raw.get("direct_marketing_price_source") or raw.get("price_source") or raw.get("tariff_provider") or "unknown")
        slots[int(start)] = {
            "start_timestamp": int(start),
            "end_timestamp": int(end),
            "price_ct_per_kwh": float(price_ct),
            "price_key": price_key,
            "source": source,
            "may_be_billing_price": source.lower() == "tibber" and price_key == "marketprice",
        }
    return slots


def _base_report(enabled, status, now_ms=None, warnings=None, error=None):
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    return {
        "schema_version": SCHEMA_VERSION,
        "created_at": _iso_from_ms(now_ms),
        "enabled": bool(enabled),
        "active": False,
        "status": status,
        "read_only": True,
        "control_effect": "none",
        "actionable_for_control": False,
        "owner": "none",
        "source": NETZTRANSPARENZ_SOURCE,
        "unit": "ct/kWh",
        "warnings": list(warnings or []),
        "error": str(error) if error else None,
        "method": "sum(solar_energy_mwh * spot_price_ct_per_kwh) / sum(solar_energy_mwh)",
        "preliminary": True,
    }


def build_market_value_solar_report(price_data, solar_data, now_ms=None, enabled=True, source=NETZTRANSPARENZ_SOURCE, warnings=None):
    now_ms = int(now_ms if now_ms is not None else time.time() * 1000)
    month_start_ms, month_end_ms, month_label = _month_bounds_ms(now_ms)
    report = _base_report(enabled, "preliminary", now_ms=now_ms, warnings=warnings)
    report["source"] = source
    report["month"] = month_label

    price_slots = normalise_price_slots(price_data)
    solar_slots = [
        slot for slot in normalise_solar_slots(solar_data)
        if month_start_ms <= int(slot["start_timestamp"]) < month_end_ms
        and int(slot["start_timestamp"]) <= now_ms
    ]

    if not solar_slots:
        report.update({
            "status": "no_solar_data",
            "solar_weighted_market_value_ct": None,
            "matched_solar_energy_mwh": 0.0,
            "slots": {
                "solar": 0,
                "price": len(price_slots),
                "matched": 0,
                "missing_price": 0,
            },
        })
        return report
    if not price_slots:
        report.update({
            "status": "no_price_data",
            "solar_weighted_market_value_ct": None,
            "matched_solar_energy_mwh": 0.0,
            "slots": {
                "solar": len(solar_slots),
                "price": 0,
                "matched": 0,
                "missing_price": len(solar_slots),
            },
        })
        return report

    matched = 0
    missing_price = 0
    solar_energy_total = 0.0
    matched_energy = 0.0
    weighted_sum = 0.0
    simple_price_sum = 0.0
    billing_warning = False
    first_ms = None
    last_ms = None

    for solar in solar_slots:
        start = int(solar["start_timestamp"])
        energy_mwh = max(0.0, float(solar.get("energy_mwh") or 0.0))
        solar_energy_total += energy_mwh
        price = price_slots.get(start)
        if not price:
            missing_price += 1
            continue
        price_ct = float(price["price_ct_per_kwh"])
        matched += 1
        matched_energy += energy_mwh
        weighted_sum += energy_mwh * price_ct
        simple_price_sum += price_ct
        billing_warning = billing_warning or bool(price.get("may_be_billing_price"))
        first_ms = start if first_ms is None else min(first_ms, start)
        last_ms = start if last_ms is None else max(last_ms, start)

    if matched <= 0 or matched_energy <= 0.0:
        report.update({
            "status": "no_price_matches",
            "solar_weighted_market_value_ct": None,
            "matched_solar_energy_mwh": round(matched_energy, 6),
            "solar_energy_mwh": round(solar_energy_total, 6),
            "slots": {
                "solar": len(solar_slots),
                "price": len(price_slots),
                "matched": matched,
                "missing_price": missing_price,
            },
        })
        return report

    weighted_value = weighted_sum / matched_energy
    simple_avg = simple_price_sum / matched
    completeness_pct = (matched / len(solar_slots)) * 100.0 if solar_slots else 0.0
    if billing_warning:
        report["warnings"].append("price_source_may_be_customer_billing_price")
    if completeness_pct < 95.0:
        report["warnings"].append("incomplete_month_to_date_matching")

    report.update({
        "status": "preliminary",
        "solar_weighted_market_value_ct": round(weighted_value, 5),
        "simple_avg_price_ct": round(simple_avg, 5),
        "solar_capture_delta_ct": round(weighted_value - simple_avg, 5),
        "matched_solar_energy_mwh": round(matched_energy, 6),
        "solar_energy_mwh": round(solar_energy_total, 6),
        "slots": {
            "solar": len(solar_slots),
            "price": len(price_slots),
            "matched": matched,
            "missing_price": missing_price,
        },
        "quality": {
            "completeness_pct": round(completeness_pct, 2),
            "preliminary": True,
            "official_month_value": False,
            "first_matched_slot": _iso_from_ms(first_ms),
            "last_matched_slot": _iso_from_ms(last_ms),
            "solar_slots_until": _iso_from_ms(max(int(slot["start_timestamp"]) for slot in solar_slots)),
        },
    })
    return report


def fetch_netztransparenz_access_token(client_id, client_secret, timeout=15):
    body = urllib.parse.urlencode({
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
    }).encode("utf-8")
    request = urllib.request.Request(
        NETZTRANSPARENZ_TOKEN_URL,
        data=body,
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
            "User-Agent": "E3DC-Control/market-value-solar",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    token = str(payload.get("access_token") or "").strip()
    if not token:
        raise RuntimeError("Netztransparenz OAuth lieferte keinen access_token")
    return token


def fetch_netztransparenz_solar_data(client_id, client_secret, date_from, date_to, timeout=20):
    token = fetch_netztransparenz_access_token(client_id, client_secret, timeout=timeout)
    url = NETZTRANSPARENZ_DATA_URL.format(
        date_from=urllib.parse.quote(str(date_from)),
        date_to=urllib.parse.quote(str(date_to)),
    )
    request = urllib.request.Request(
        url,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "text/csv,application/json",
            "User-Agent": "E3DC-Control/market-value-solar",
        },
        method="GET",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        content_type = str(response.headers.get("Content-Type") or "").lower()
        data = response.read().decode("utf-8-sig")
    if "json" in content_type:
        return json.loads(data)
    return data


def _read_json_file(path):
    if not path or not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:
        return None


def _write_json(path, payload, writer=None, indent=2):
    if not path:
        return
    if writer is not None:
        writer(path, payload, indent=indent)
        return
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=indent, ensure_ascii=False)
    os.replace(tmp, path)


def _cache_payload(solar_slots, now_ms):
    return {
        "schema_version": "market_value_solar_cache_v1",
        "created_at": _iso_from_ms(now_ms),
        "source": NETZTRANSPARENZ_SOURCE,
        "slots": solar_slots,
    }


def _fetch_month_solar_slots(config, now_ms, fetcher=None):
    month_start_ms, _month_end_ms, _month_label = _month_bounds_ms(now_ms)
    start_dt = datetime.fromtimestamp(month_start_ms / 1000.0, tz=_berlin_tz()).date()
    end_dt = datetime.fromtimestamp(now_ms / 1000.0, tz=_berlin_tz()).date() + timedelta(days=1)
    client_id = str((config or {}).get("netztransparenz_client_id") or "").strip()
    client_secret = str((config or {}).get("netztransparenz_client_secret") or "").strip()
    if not client_id or not client_secret:
        raise ValueError("missing_netztransparenz_credentials")
    if fetcher is None:
        fetcher = fetch_netztransparenz_solar_data
    raw = fetcher(client_id, client_secret, start_dt.isoformat(), end_dt.isoformat())
    slots = normalise_solar_slots(raw)
    if not slots:
        raise RuntimeError("no_parseable_solar_slots")
    return slots


def update_market_value_solar_report(config, price_data, output_file, cache_file, write_json_atomic=None, logger=None, now_s=None, fetcher=None):
    now_ms = int((now_s if now_s is not None else time.time()) * 1000)
    enabled = _config_bool(config, "direct_marketing_market_value_solar_enable", False)

    def log_warning(message):
        if logger is not None:
            try:
                logger.warning(message)
            except Exception:
                pass

    if not enabled:
        report = _base_report(False, "disabled", now_ms=now_ms)
        _write_json(output_file, report, writer=write_json_atomic, indent=2)
        return report

    warnings = ["preliminary_trend_not_official_monthly_market_value"]
    cache = _read_json_file(cache_file)
    solar_slots = None
    cache_used = False
    fetch_error = None
    try:
        solar_slots = _fetch_month_solar_slots(config, now_ms, fetcher=fetcher)
        _write_json(cache_file, _cache_payload(solar_slots, now_ms), writer=write_json_atomic, indent=2)
    except Exception as exc:
        fetch_error = str(exc)
        log_warning(f"Marktwert Solar: Netztransparenz-Abruf nicht nutzbar: {exc}")
        if isinstance(cache, dict) and isinstance(cache.get("slots"), list):
            solar_slots = cache.get("slots")
            cache_used = True
            warnings.append("using_cached_solar_extrapolation")
        else:
            status = "missing_credentials" if fetch_error == "missing_netztransparenz_credentials" else "unavailable"
            report = _base_report(True, status, now_ms=now_ms, warnings=warnings, error=fetch_error)
            report["quality"] = {
                "preliminary": True,
                "official_month_value": False,
                "cache_used": False,
            }
            _write_json(output_file, report, writer=write_json_atomic, indent=2)
            return report

    report = build_market_value_solar_report(
        price_data,
        solar_slots,
        now_ms=now_ms,
        enabled=True,
        source=NETZTRANSPARENZ_SOURCE,
        warnings=warnings,
    )
    if cache_used:
        report["status"] = "cached_" + str(report.get("status") or "preliminary")
        report["error"] = fetch_error
    report.setdefault("quality", {})
    if isinstance(report["quality"], dict):
        report["quality"]["cache_used"] = bool(cache_used)
        report["quality"]["official_month_value"] = False
    _write_json(output_file, report, writer=write_json_atomic, indent=2)
    return report
