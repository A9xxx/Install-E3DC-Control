"""Bestehende reine Einzelmessverträge; keine Wallbox-Steuerung."""
import math
from typing import Any, Dict, Optional
try:
    from Installer.data_models import (live_power_plausibility, published_measurement_valid,
        wallbox_type_is_e3dc, E3DC_WALLBOX_TYPES, STATUS_STALE_DEFAULT_S)
    from Installer.Wallbox.modes import MODE_OFF, normalize_wb_mode, controller_mode
except ModuleNotFoundError:
    from data_models import (live_power_plausibility, published_measurement_valid,
        wallbox_type_is_e3dc, E3DC_WALLBOX_TYPES, STATUS_STALE_DEFAULT_S)
    from Wallbox.modes import MODE_OFF, normalize_wb_mode, controller_mode
from .leistungsleser import safe_float, safe_int

WALLBOX_STATUS_SAMPLE_MAX_AGE_S = 30.0
AUSGESCHALTETE_BOX_TYPEN = frozenset(("none", "off", "aus", "disabled", "0", "false", "no", "nein",
    "deaktiviert", "keine", "keine_wallbox", "no_wallbox", "-1"))


def native_total_power_w(wb_native: Optional[Dict[str, Any]]) -> Optional[float]:
    """Gemessene Gesamtleistung; unbekannt bleibt unbekannt, auch bei bool."""
    if not isinstance(wb_native, dict) or wb_native.get("total_power_valid") is False:
        return None
    value = wb_native.get("total_power_w")
    if type(value) not in (int, float) or not math.isfinite(value):
        return None
    return abs(float(value))


def cfg_bool(cfg: Dict[str, Any], key: str, default: bool = False) -> bool:
    raw = cfg.get(key, default)
    if isinstance(raw, bool):
        return raw
    return str(raw).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")


def runtime_input_age_s(timestamp: Any, now_s: float) -> Optional[float]:
    """Alter gelesener Laufzeitdaten; bis 2 s Vorlauf nach ms-Normalisierung.

    Ein Erzeuger kann zwischen Zyklusbeginn und Dateilesen publizieren.
    Größerer Vorlauf, fehlende und nicht endliche Zeitstempel sind ungültig.
    Frischeobergrenzen und interne Regleruhren bestimmt weiterhin der Aufrufer.
    """
    if type(timestamp) not in (int, float) or type(now_s) not in (int, float):
        return None
    stamp, now = float(timestamp), float(now_s)
    if not math.isfinite(stamp) or not math.isfinite(now) or stamp <= 0.0:
        return None
    if stamp > 100_000_000_000.0:
        stamp /= 1000.0
    age_s = now - stamp
    return max(0.0, age_s) if age_s >= -2.0 else None


def live_with_managed_vehicle_phases(live, intent, now_s):
    """Derselbe frische Fahrzeuganteil für Netzladeregler und Vorschau."""
    age = runtime_input_age_s(safe_float(intent.get("ts"), 0.0), now_s)
    return dict(live, managed_vehicle_phases=(intent.get("managed_vehicle_phases")
        if age is not None and age <= 60.0 else None))


def _wallbox_status_sample_fresh(
    status: Dict[str, Any],
    *,
    now_s: float,
    max_age_s: float,
) -> bool:
    """Prüft native Leistungsbelege ohne Legacy-Default auf "gültig"."""

    if not isinstance(status, dict):
        return False
    published = published_measurement_valid(status, now_s, max_age_s)
    if published is not None:
        return published
    sample_ts = status.get("driver_status_last_sample_ts")
    if type(sample_ts) not in (int, float) or not math.isfinite(float(sample_ts)):
        return False
    sample_value = float(sample_ts)
    # Die zentrale Altersprüfung normalisiert genau einmal.
    return bool(
        status.get("driver_status_valid") is True
        and status.get("driver_status_stale") is False
        and status.get("driver_status_degraded") is False
        and status.get("driver_status_glitch") is False
        and status.get("driver_status_plausible") is True
        and sample_value > 0.0
        and math.isfinite(float(now_s))
        and math.isfinite(float(max_age_s))
        and float(max_age_s) > 0.0
        and ((_input_age_s := runtime_input_age_s(sample_value, float(now_s))) is not None and _input_age_s <= float(max_age_s))
    )


def wallbox_data_outage_context(cfg, intent, native, now_s, previous=None):
    """Bewertet jeden externen Messpunkt, ohne Last oder Freigaben zu erfinden."""
    previous = previous or {}
    rows = {safe_int(r.get("wb_id"), 0): r for r in intent.get("car_energy_needs", []) if isinstance(r, dict)}
    details = {safe_int(d.get("id"), 0): d for d in native.get("wb_details", []) if isinstance(d, dict)}
    prior = {p.get("id"): p for p in previous.get("points", []) if isinstance(p, dict)}
    points = []
    vehicle_present = dict(previous.get("vehicle_present") or {})
    if "wb_native_enable" in cfg and not cfg_bool(cfg, "wb_native_enable", False):
        return {"active": False, "points": []}
    for cid, key in ((1, "wb_native_type"), (2, "wb_native_type2")):
        kind = str(cfg.get(key) or "").strip().lower()
        if key not in cfg:
            # Altverträge können die Typbindung am einzelnen Ladepunkt tragen.
            kind = next((str(p.get("type") or "").strip().lower()
                         for p in intent.get("wallbox_storage_points", [])
                         if isinstance(p, dict) and safe_int(p.get("id"), 0) == cid), "")
            kind = kind or str(details.get(cid, {}).get("type") or "").strip().lower()
            if not kind and cid == 1 and cid in (intent.get("openwb_primary_pv_mode_ids") or [1]) and (
                    intent.get("openwb_primary_pv_mode_active") or intent.get("openwb_primary_observe_only")):
                kind = "openwb"
        if kind in ("", "none", "off", "disabled", "0", "false", "aus", "no", "nein", "deaktiviert", "keine", "keine_wallbox", "no_wallbox", "-1") or wallbox_type_is_e3dc(kind):
            continue
        row, detail = rows.get(cid), details.get(cid, {})
        observation = detail.get("observation") or {}
        published = published_measurement_valid(detail, now_s, WALLBOX_STATUS_SAMPLE_MAX_AGE_S)
        if published is not None:
            power = (observation.get("values") or {}).get("power_w")
            valid = bool(published and type(power) in (int, float) and math.isfinite(power) and power >= 0)
        else:
            # Bestandsformat ohne veröffentlichten Ablauf: eine Gültigkeit aus Boxdetail und
            # Absichtsalter. Die Absichtszeile ist eine Kopie derselben Probe, keine zweite Messung.
            # Ein fehlendes Boxdetail zählt wie ein ungültiges: dieselbe Antwort wie der Messleser.
            power = (observation.get("values") or {}).get("power_w") if observation else detail.get("power_w")
            valid = bool(((_input_age_s := runtime_input_age_s(safe_float(intent.get("ts"), 0), now_s)) is not None and _input_age_s <= 60)
                         and _wallbox_status_sample_fresh(detail, now_s=now_s, max_age_s=WALLBOX_STATUS_SAMPLE_MAX_AGE_S)
                         and (not observation or observation.get("valid") is True)
                         and type(power) in (int, float) and math.isfinite(power) and power >= 0)
        if valid:
            values = observation.get("values") or {}
            vehicle_present[str(cid)] = bool(
                (row or {}).get("connected") is True or values.get("plug") is True
                or detail.get("plug") is True or detail.get("charging") is True
                or (type(power) in (int, float) and power > 500))
        if (row or {}).get("connected") is False:
            vehicle_present[str(cid)] = False
        if valid:
            continue
        # Diagnose enthält nur bekannte neutrale Codes, niemals URLs oder Fahrzeugdaten.
        raw_reason = str(observation.get("reason") or detail.get("driver_status_reason") or "")
        allowed = {"openwb_tls_required", "openwb_pin_changed", "openwb_target_changed",
                   "openwb_tls_pin_mismatch", "openwb_certificate_expired", "openwb_auth_rejected",
                   "openwb_acl_missing", "openwb_unavailable", "connection_refused", "status_timeout",
                   "http_error", "partial_measurement", "last_good_within_grace", "openwb_redirect_rejected",
                   "openwb_address_invalid", "openwb_mqtt_unavailable", "openwb_mqtt_waiting",
                   "openwb_cleartext_credentials"}
        reason = raw_reason if raw_reason in allowed else "wallbox_measurement_missing_or_stale"
        since = prior.get(cid, {}).get("since_ts")
        since = since if type(since) in (int, float) and 0 < since <= now_s else now_s
        points.append({"id": cid, "since_ts": since, "reason": reason,
                       "vehicle_present": vehicle_present.get(str(cid), False),
                       "regulation": "Unterhalb der Hausakku-Untergrenze bleibt die Speicherentladung für die Wallbox begrenzt"})
    return {"active": bool(points), "points": points, "vehicle_present": vehicle_present}


def storage_inputs_without_failed_wallboxes(cfg, intent, native, outage):
    """Entfernt ausgefallene Punkte ausschließlich aus der internen Regelungssicht.

    Der gemessene Hausverbrauch bleibt vollständig erhalten. Ein separater
    E3DC-Messpunkt und alle anderen Schutzkonfigurationen bleiben maßgeblich.
    """
    failed = {p["id"] for p in outage.get("points", [])}
    if not failed:
        return cfg, intent, native
    cfg, native = dict(cfg), dict(native)
    for cid in failed:
        cfg["wb_native_type" if cid == 1 else "wb_native_type2"] = "none"
        cfg[f"wb{cid}_mode"] = MODE_OFF
        cfg[f"wb{cid}_observe_storage_policy"] = ""
        cfg[f"wb{cid}_storage_observe_policy"] = ""
    points = [p for p in intent.get("wallbox_storage_points", [])
              if isinstance(p, dict) and safe_int(p.get("id"), 0) not in failed]
    rows = [r for r in intent.get("car_energy_needs", [])
            if isinstance(r, dict) and safe_int(r.get("wb_id"), 0) not in failed]
    details = [d for d in native.get("wb_details", [])
               if isinstance(d, dict) and safe_int(d.get("id"), 0) not in failed]
    for key in ("wb_possible_power_w", "wb_max_amp", "wb_global_max_amp"):
        native.pop(key, None)
    native["wb_details"] = details
    native["total_power_w"] = sum(max(0, safe_float(d.get("power_w"), 0)) for d in details)
    native["charging_active"] = any(d.get("charging") for d in details)
    if not points and not rows and details and intent:
        # Beim Altschema bleiben einzeln belegte andere Ladepunkte erhalten.
        primary_ids = intent.get("openwb_primary_pv_mode_ids") or [1]
        for detail in details:
            cid = safe_int(detail.get("id"), 0)
            if cid <= 0:
                continue
            primary = cid in primary_ids and bool(intent.get("openwb_primary_pv_mode_active")
                                                  or intent.get("openwb_primary_observe_only"))
            points.append(dict(id=cid, configured=True, enabled=True, type=detail.get("type"),
                mode=normalize_wb_mode(cfg.get(f"wb{cid}_mode", intent.get("wb_mode_active", 0))),
                role="primary" if primary else "controlled",
                observe_storage_policy=cfg.get(f"wb{cid}_observe_storage_policy", intent.get("observe_storage_policy")),
                active=bool(detail.get("plug") or detail.get("charging")),
                car_active=bool(detail.get("plug") or detail.get("charging")),
                charging_active=bool(detail.get("charging")), power_w=detail.get("power_w")))
    if not points and not rows:
        return cfg, {}, native
    filtered = dict(intent, wallbox_storage_points=points, car_energy_needs=rows)
    # Aufträge einer ausgefallenen Box dürfen auch in Mischanlagen keinen
    # Start- oder Phasenrahmen reservieren. Gebundene Gruppen mit einem
    # ausgefallenen Mitglied müssen neu qualifiziert werden.
    allowed = {safe_int(p.get("id"), 0) for p in points} | {safe_int(r.get("wb_id"), 0) for r in rows}
    phases = [item for item in intent.get("phase_transition_reservations", [])
              if isinstance(item, dict) and safe_int(item.get("wb_id"), 0) in allowed]
    filtered["phase_transition_reservations"] = phases
    filtered["phase_transition_active"] = any(item.get("active") for item in phases)
    filtered["phase_transition_reserved_w"] = sum(max(0, safe_int(item.get("requested_w"), 0)) for item in phases)
    filtered["phase_transition_charger_ids"] = [safe_int(item.get("wb_id"), 0) for item in phases]
    filtered.pop("phase_transition_reservation", None)
    for key in ("active", "car_active", "charging_active", "grid_allowed", "price_opt_active",
                "scheduled_slot_active", "price_boost_active", "predump_wallbox_active"):
        filtered[key] = any(p.get(key) for p in points)
    filtered["connected"] = any(r.get("connected") is True for r in rows)
    filtered["plugged"] = filtered["connected"]
    filtered["wb_power_w"] = (sum(max(0, safe_float(r.get("car_power_w"), 0)) for r in rows)
                                if rows else native["total_power_w"])
    filtered["wb_mode_active"] = max((normalize_wb_mode(p.get("mode", 0)) for p in points), default=0)
    filtered["wb_control_mode"] = controller_mode(filtered["wb_mode_active"])
    primary = [p["id"] for p in points if p.get("role") == "primary"]
    filtered["openwb_primary_pv_mode_ids"] = primary
    filtered["openwb_primary_pv_mode_active"] = bool(primary)
    filtered["openwb_primary_observe_only"] = bool(primary)
    filtered["external_wallbox_manager"] = bool(primary) and len(primary) == len(points)
    return cfg, filtered, native


def _wallbox_detail_power_w(detail: Dict[str, Any]) -> float:
    for key in ("power_w", "phase_power_sum_w", "meter_power_w", "charge_power_w"):
        power = abs(safe_float(detail.get(key), 0.0))
        if power > 0:
            return power
    return 0.0


def _wallbox_slot_types(cfg, wb_intent):
    """Explizite Konfiguration vor der slotgebundenen Manager-Veröffentlichung."""
    cfg = cfg or {}
    types = {}
    for point in (wb_intent or {}).get("wallbox_storage_points") or []:
        if isinstance(point, dict) and type(point.get("id")) is int and point["id"] > 0:
            types[point["id"]] = str(point.get("type") or "").strip().lower()
    for cid, key in ((1, "wb_native_type"), (2, "wb_native_type2")):
        kind = str(cfg.get(key) or "").strip().lower()
        if kind:
            types[cid] = kind
    return types


def _wallbox_detail_measurement(detail, *, now_s, sample_age_limit_s,
                                require_fresh_evidence, external_slot):
    """Ein Messvertrag für Gesamtleistung und externen Hausanteil."""
    power = _wallbox_detail_power_w(detail)
    detail_status_usable = (
        _wallbox_status_sample_fresh(
            detail,
            now_s=now_s,
            max_age_s=sample_age_limit_s,
        )
        if require_fresh_evidence
        else bool(
            detail.get("driver_status_valid") is not False
            and detail.get("driver_status_stale") is not True
            and detail.get("driver_status_degraded") is not True
            and detail.get("driver_status_glitch") is not True
        )
    )
    published = published_measurement_valid(detail, now_s, sample_age_limit_s)
    if published is not None:
        detail_status_usable = bool(detail_status_usable and published)
    if external_slot:
        detail_status_usable = bool(detail_status_usable and _wallbox_status_sample_fresh(
            detail, now_s=now_s, max_age_s=sample_age_limit_s))
    observation = detail.get("observation")
    if isinstance(observation, dict):
        measured_power = (observation.get("values") or {}).get("power_w")
        detail_status_usable = bool(detail_status_usable and observation.get("valid") is True
            and type(measured_power) in (int, float) and math.isfinite(measured_power))
    if not detail_status_usable:
        return dict(usable=False, known=False, power_w=power, running=False)
    known = any(
        isinstance(detail.get(key), (int, float))
        and not isinstance(detail.get(key), bool)
        and math.isfinite(float(detail.get(key)))
        for key in (
            "power_w",
            "phase_power_sum_w",
            "meter_power_w",
            "charge_power_w",
        )
    )
    if require_fresh_evidence and power > 250.0 and not (
        detail.get("plug") is True
        and detail.get("charging") is True
    ):
        return dict(usable=False, known=known, power_w=power, running=False)
    return dict(usable=True, known=known, power_w=power,
                running=bool(detail.get("charging")) or power > 250.0)


def _external_wallbox_power_snapshot(wb_native, types, now_s, max_age_s, *, require_fresh_evidence=False):
    """Hausanteil aus dem gemeinsamen Messvertrag aller externen Ladepunkte."""
    types = types or {}
    details = wb_native.get("wb_details")
    off_types = AUSGESCHALTETE_BOX_TYPEN
    expected = {cid for cid, kind in types.items()
                if kind and kind not in off_types and not wallbox_type_is_e3dc(kind)}
    external_w, seen = 0.0, set()
    separate_box_present = any(wallbox_type_is_e3dc(kind) for kind in types.values())
    if not isinstance(details, list):
        details = []
    for detail in details:
        if not isinstance(detail, dict):
            return dict(external_w=None, external_reason="wallbox_type_unknown")
        cid = detail.get("id")
        if type(cid) is not int or cid <= 0 or cid in seen:
            return dict(external_w=None, external_reason="wallbox_type_unknown")
        seen.add(cid)
        kind = types.get(cid, "")
        if kind in off_types or wallbox_type_is_e3dc(kind):
            continue
        measurement = _wallbox_detail_measurement(
            detail, now_s=now_s, sample_age_limit_s=max_age_s,
            require_fresh_evidence=require_fresh_evidence, external_slot=True)
        if not kind:
            if (measurement["usable"] and measurement["known"]
                    and measurement["power_w"] <= 250.0 and not bool(detail.get("charging"))):
                continue
            return dict(external_w=None, external_reason="wallbox_type_unknown")
        if (not measurement["usable"] or not measurement["known"]
                or wb_native.get("driver_status_valid") is False
                or any(wb_native.get(k) is True for k in
                       ("driver_status_stale", "driver_status_degraded", "driver_status_glitch"))):
            return dict(external_w=None, external_reason="external_measurement_invalid")
        external_w += measurement["power_w"]
    if expected - seen or not (seen or types):
        return dict(external_w=None, external_reason="wallbox_details_incomplete")
    return dict(external_w=external_w, external_reason="measured_external_slots",
                home_includes_wallbox=bool(external_w > 0 and not separate_box_present))


def wallbox_actual_power_snapshot(
    live: Dict[str, Any],
    wb_native: Optional[Dict[str, Any]] = None,
    *,
    now_s: Optional[float] = None,
    max_age_s: float = 45.0,
    require_fresh_evidence: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
    sample_max_age_s: Optional[float] = None,
    external_types: Optional[Dict[int, str]] = None,
) -> Dict[str, Any]:
    """Summiert belegte Ladepunkte; ein separater E3DC-Wert ersetzt keinen externen Slot."""
    wb_native = wb_native or {}
    now_value = float(now_s)
    # max_age_s gilt für die E3DC-Livedaten, sample_max_age_s für Wallbox-Statusproben (ohne Angabe wie max_age_s).
    sample_age_limit_s = min(WALLBOX_STATUS_SAMPLE_MAX_AGE_S,
                             float(max_age_s if sample_max_age_s is None else sample_max_age_s))
    # Ein angereicherter Rahmen trägt die Summe in Wallbox_Power. Der
    # separate E3DC-Messpunkt bleibt im Ursprungsfeld erhalten; erneutes Lesen
    # darf den externen Anteil nicht wieder dem Haus zuschlagen.
    live_raw = (live.get("Wallbox_Live_Power") if "Wallbox_Live_Power" in live
                and live.get("Wallbox_Power_Source") in ("wallbox_native", "live_data", "unknown")
                else live.get("Wallbox_Power"))
    live_power_known = bool(
        isinstance(live_raw, (int, float))
        and not isinstance(live_raw, bool)
        and math.isfinite(float(live_raw))
    )
    live_w = abs(float(live_raw)) if live_power_known else 0.0
    live_sample_ts = live.get("_ts")
    live_sample_fresh = bool(
        type(live_sample_ts) in (int, float)
        and math.isfinite(float(live_sample_ts))
        and float(live_sample_ts) > 0.0
        and ((_input_age_s := runtime_input_age_s(float(live_sample_ts), now_value)) is not None and _input_age_s <= float(max_age_s))
        and live_power_plausibility(live).get("sample_valid") is True
        and live.get("Power_Decision_Usable", True) is not False
    )
    live_status_usable = bool(
        live_power_known
        and (live_sample_fresh if require_fresh_evidence else True)
    )
    native_status_usable = (
        _wallbox_status_sample_fresh(
            wb_native,
            now_s=now_value,
            max_age_s=sample_age_limit_s,
        )
        if require_fresh_evidence
        else bool(
            wb_native.get("driver_status_valid") is not False
            and wb_native.get("driver_status_stale") is not True
            and wb_native.get("driver_status_degraded") is not True
            and wb_native.get("driver_status_glitch") is not True
        )
    )
    native_root_status_marked = any(
        key in wb_native
        for key in (
            "driver_status_valid",
            "driver_status_stale",
            "driver_status_degraded",
            "driver_status_glitch",
            "driver_status_plausible",
            "driver_status_last_sample_ts",
        )
    )
    native_root_explicitly_invalid = bool(
        require_fresh_evidence
        and native_root_status_marked
        and not native_status_usable
    )
    # Der Typkontext für den Hausanteil verändert keine bestehende Quellenvalidierung.
    external = _external_wallbox_power_snapshot(
        wb_native, _wallbox_slot_types(cfg, {}) if external_types is None else external_types,
        now_value, sample_age_limit_s, require_fresh_evidence=require_fresh_evidence)
    native_details_w = 0.0
    native_details_known = False
    native_details_complete = True
    native_running_details = 0
    details = wb_native.get("wb_details") or []
    expected_external_ids = set()
    if cfg and ("wb_native_enable" not in cfg or cfg_bool(cfg, "wb_native_enable", False)):
        for cid, key in ((1, "wb_native_type"), (2, "wb_native_type2")):
            kind = str(cfg.get(key) or "").strip().lower()
            if kind not in ("", "none", "off", "aus", "disabled", "0", "false", "no", "nein", "deaktiviert", "keine", "keine_wallbox", "no_wallbox", "-1") and not wallbox_type_is_e3dc(kind):
                expected_external_ids.add(cid)
    present_ids = {safe_int(d.get("id"), 0) for d in details if isinstance(d, dict)} if isinstance(details, list) else set()
    missing_external_ids = expected_external_ids - present_ids
    if require_fresh_evidence and not isinstance(details, list):
        native_details_complete = False
    if native_root_explicitly_invalid:
        native_details_complete = False
    if isinstance(details, list) and not native_root_explicitly_invalid and (
        require_fresh_evidence or native_status_usable
    ):
        for detail in details:
            if not isinstance(detail, dict):
                native_details_complete = False
                continue
            measurement = _wallbox_detail_measurement(
                detail, now_s=now_value, sample_age_limit_s=sample_age_limit_s,
                require_fresh_evidence=require_fresh_evidence,
                external_slot=safe_int(detail.get("id"), 0) in expected_external_ids)
            if measurement["known"]:
                native_details_known = True
            if not measurement["usable"]:
                native_details_complete = False
                continue
            if measurement["running"]:
                native_details_w += measurement["power_w"]
                if measurement["power_w"] > 250.0:
                    native_running_details += 1

    external_measurement_missing = bool(missing_external_ids or (details and not native_details_complete))
    if external_measurement_missing:
        native_details_w = 0.0
        native_details_known = False
        native_running_details = 0

    native_total_raw = native_total_power_w(wb_native)
    native_total_known = bool(
        native_status_usable
        and not external_measurement_missing
        and isinstance(native_total_raw, (int, float))
        and not isinstance(native_total_raw, bool)
        and math.isfinite(float(native_total_raw))
    )
    native_total_w = abs(float(native_total_raw)) if native_total_known else 0.0
    native_power_known = bool(
        (native_details_known and native_details_complete)
        or native_total_known
    )
    has_native_charge_flag = "charging_active" in wb_native
    native_charging = bool(wb_native.get("charging_active", False))
    native_total_valid = native_total_w > 250.0 and (
        native_charging
        or native_details_w > 250.0
        or not has_native_charge_flag
    )
    native_w = native_details_w if native_details_w > 50.0 else (native_total_w if native_total_valid else 0.0)
    if native_w > max(250.0, live_w):
        return {
            "power_w": native_w,
            "source": "wallbox_native",
            "power_known": True,
            "live_w": live_w,
            "native_w": native_w,
            "home_includes_wallbox": bool(external.get("home_includes_wallbox", False)),
            **external,
            "measurement_valid": True,
            "live_evidence_valid": bool(live_status_usable),
            "native_evidence_valid": True,
            "native_details_complete": bool(native_details_complete),
            "running_detail_count": int(native_running_details),
        }
    if live_status_usable and not external_measurement_missing:
        return {
            "power_w": live_w,
            "source": "live_data",
            "power_known": True,
            "live_w": live_w,
            "native_w": native_w,
            "home_includes_wallbox": bool(external.get("home_includes_wallbox", False)),
            **external,
            "measurement_valid": True,
            "live_evidence_valid": True,
            "native_evidence_valid": bool(native_power_known),
            "native_details_complete": bool(native_details_complete),
            "running_detail_count": int(native_running_details),
        }
    if native_power_known:
        return {
            "power_w": native_w,
            "source": "wallbox_native",
            "power_known": True,
            "live_w": 0.0,
            "native_w": native_w,
            "home_includes_wallbox": bool(external.get("home_includes_wallbox", False)),
            **external,
            "measurement_valid": True,
            "live_evidence_valid": bool(live_status_usable),
            "native_evidence_valid": True,
            "native_details_complete": bool(native_details_complete),
            "running_detail_count": int(native_running_details),
        }
    return {
        "power_w": None,
        "source": "unknown",
        "reason": "wallbox_measurement_invalid",
        "power_known": False,
        "live_w": live_w if live_status_usable else 0.0,
        "native_w": 0.0,
        "home_includes_wallbox": bool(external.get("home_includes_wallbox", False)),
        **external,
        "measurement_valid": False,
        "live_evidence_valid": bool(live_status_usable),
        "native_evidence_valid": False,
        "native_details_complete": bool(native_details_complete),
        "running_detail_count": 0,
    }

