#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import copy
import hashlib
import time
import math
import logging
import queue
import stat
import threading
from datetime import datetime, timedelta

try:
    from .planned_loads import apply_planned_loads_to_timeline
except Exception:
    from planned_loads import apply_planned_loads_to_timeline

try:
    from .direct_marketing import build_direct_marketing_shadow_plan
except Exception:
    from direct_marketing import build_direct_marketing_shadow_plan

try:
    from .storage_dispatch_contract import (
        build_canonical_dispatch_plan,
        build_storage_plan_action_projection_artifact,
        FORECAST_SHORTFALL_AUX_AC_RELEASED,
        revision_hash,
    )
except Exception:
    from storage_dispatch_contract import (
        build_canonical_dispatch_plan,
        build_storage_plan_action_projection_artifact,
        FORECAST_SHORTFALL_AUX_AC_RELEASED,
        revision_hash,
    )

try:
    from .pv_forecast_topology import (
        build_pv_forecast_topology,
        resolve_buffered_pcc_limit,
        slot_headroom_pressure,
    )
except Exception:
    from pv_forecast_topology import (
        build_pv_forecast_topology,
        resolve_buffered_pcc_limit,
        slot_headroom_pressure,
    )

try:
    from .market_economics import (
        HORIZON_MS as MARKET_HORIZON_MS,
        build_market_economics_plan,
    )
except Exception:
    from market_economics import (
        HORIZON_MS as MARKET_HORIZON_MS,
        build_market_economics_plan,
    )

try:
    from .reserve import effective_ep_reserve_pct
except Exception:
    from reserve import effective_ep_reserve_pct

try:
    from Wallbox.modes import MODE_OFF, MODE_TARGET, normalize_wb_mode
except Exception:
    MODE_OFF = 0
    MODE_TARGET = 4
    def normalize_wb_mode(value, default=0):
        try:
            raw = int(float(value))
        except Exception:
            raw = default
        if raw in (4, 9, 10):
            return MODE_TARGET
        if raw == 0:
            return 0
        return raw

try:
    from runtime_logging import configure_service_logger
except ImportError:  # pragma: no cover - Paketimport
    from Installer.runtime_logging import configure_service_logger

# Pfade
RAMDISK_DIR = "/var/www/html/ramdisk"
V4_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
LIVE_DATA_FILE = os.path.join(RAMDISK_DIR, "live_data_py.json")
PV_ENV_FILE = os.path.join(RAMDISK_DIR, "pv_forecast.json")
PV_META_FILE = os.path.join(RAMDISK_DIR, "pv_forecast_meta.json")
ML_PRED_FILE = os.path.join(RAMDISK_DIR, "ml_prediction.json")
EPEX_DATA_FILE = os.path.join(RAMDISK_DIR, "epex_daten.json")
ECO_SCORE_FILE = os.path.join(RAMDISK_DIR, "eco_score.json")
PRICE_BOOST_PLAN_FILE = os.path.join(RAMDISK_DIR, "price_boost_plan.json")
WEATHER_ALERTS_FILE = os.path.join(RAMDISK_DIR, "weather_alerts.json")
WEATHER_FORECAST_FILE = os.path.join(RAMDISK_DIR, "weather_forecast.json")
LIVE_HISTORY_FILE = os.path.join(RAMDISK_DIR, "live_history.txt")
MANUAL_ANCHOR_FILE = os.path.join(RAMDISK_DIR, "manual_bat_anchor.json")
OUTPUT_FILE = os.path.join(RAMDISK_DIR, "storage_plan.json")
ACTION_PROJECTION_FILE = os.path.join(
    RAMDISK_DIR,
    "storage_plan_action_projection.json",
)
DIRECT_MARKETING_REPORT_FILE = os.path.join(RAMDISK_DIR, "direct_marketing_daily_report.json")
WB_INTENT_FILE = os.path.join(RAMDISK_DIR, "wallbox_storage_intent.json")
HISTORY_DIR = "/var/www/html/data/history_backups"
EMERGENCY_CURVE_FILE = os.path.join(RAMDISK_DIR, "storage_emergency_curve.json")


def _stable_json_object(path, max_bytes=2 * 1024 * 1024):
    """Liest genau eine reguläre, unveränderte JSON-Dateigeneration."""

    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | getattr(os, "O_CLOEXEC", 0)
            | getattr(os, "O_NOFOLLOW", 0),
        )
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_size <= 0
            or before.st_size > int(max_bytes)
        ):
            return None, None
        source = bytearray()
        while len(source) <= int(max_bytes):
            chunk = os.read(
                descriptor,
                min(256 * 1024, int(max_bytes) - len(source) + 1),
            )
            if not chunk:
                break
            source.extend(chunk)
        after = os.fstat(descriptor)
        generation = (
            int(before.st_dev),
            int(before.st_ino),
            int(before.st_size),
            int(before.st_mtime_ns),
            int(before.st_ctime_ns),
        )
        after_generation = (
            int(after.st_dev),
            int(after.st_ino),
            int(after.st_size),
            int(after.st_mtime_ns),
            int(after.st_ctime_ns),
        )
        if (
            len(source) > int(max_bytes)
            or len(source) != int(after.st_size)
            or generation != after_generation
        ):
            return None, None
        current = os.stat(path, follow_symlinks=False)
        current_generation = (
            int(current.st_dev),
            int(current.st_ino),
            int(current.st_size),
            int(current.st_mtime_ns),
            int(current.st_ctime_ns),
        )
        if not stat.S_ISREG(current.st_mode) or current_generation != generation:
            return None, None
        payload = json.loads(source.decode("utf-8-sig"))
        if not isinstance(payload, dict):
            return None, None
        return payload, {
            "generation": generation,
            "mtime": float(before.st_mtime),
        }
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError, ValueError):
        return None, None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def direct_marketing_runtime_config(config, daily_report, today=None):
    """Liefert eine Konfigurationskopie mit gemessenem DV-Tagesdurchsatz."""
    result = dict(config or {})
    if not isinstance(daily_report, dict):
        return result
    today = str(today or datetime.now().strftime("%Y-%m-%d"))
    if str(daily_report.get("date") or "") != today:
        return result
    try:
        export_kwh = max(0.0, float(daily_report.get("real_export_kwh") or 0.0))
    except (TypeError, ValueError):
        export_kwh = 0.0
    result["_runtime_direct_marketing_daily_export_used_wh"] = export_kwh * 1000.0
    return result
SIM_INTERVAL_S = 900
SIM_INPUT_POLL_S = 30
SIM_REPLAN_INPUT_FILES = (
    V4_CONFIG_FILE,
    PV_ENV_FILE,
    PV_META_FILE,
    ML_PRED_FILE,
    EPEX_DATA_FILE,
    ECO_SCORE_FILE,
    WEATHER_ALERTS_FILE,
    WEATHER_FORECAST_FILE,
    MANUAL_ANCHOR_FILE,
)


logger = configure_service_logger(
    "StorageSimulator",
    log_path="/var/www/html/logs/storage_simulator.log",
    max_bytes=2 * 1024 * 1024,
    backup_count=3,
    quiet_interval_s=900.0,
)

_DV_SHADOW_HISTORY_QUEUE_CAPACITY = 32
_DV_SHADOW_HISTORY_QUEUE = queue.Queue(
    maxsize=_DV_SHADOW_HISTORY_QUEUE_CAPACITY
)
_DV_SHADOW_HISTORY_WORKER_LOCK = threading.Lock()
_DV_SHADOW_HISTORY_WORKER = None
_DV_SHADOW_HISTORY_STATUS_LOCK = threading.Lock()
_DV_SHADOW_HISTORY_STATUS = {
    "accepted_total": 0,
    "processed_total": 0,
    "dropped_total": 0,
    "write_failures_total": 0,
    "worker_start_failures_total": 0,
    "last_drop_at_ms": 0,
    "last_dropped_snapshot_at_ms": 0,
    "last_write_failure_at_ms": 0,
    "last_worker_start_failure_at_ms": 0,
}


def _dv_shadow_history_job_capture_ms(job):
    if not isinstance(job, dict):
        return 0
    try:
        value = int(job.get("captured_at_ms") or 0)
    except (TypeError, ValueError):
        return 0
    return max(0, value)


def _record_dv_shadow_history_event(event, job=None):
    """Aktualisiert nur speicherinterne Diagnosezähler."""

    now_ms = int(time.time() * 1000)
    with _DV_SHADOW_HISTORY_STATUS_LOCK:
        if event == "accepted":
            _DV_SHADOW_HISTORY_STATUS["accepted_total"] += 1
        elif event == "processed":
            _DV_SHADOW_HISTORY_STATUS["processed_total"] += 1
        elif event == "dropped":
            _DV_SHADOW_HISTORY_STATUS["dropped_total"] += 1
            _DV_SHADOW_HISTORY_STATUS["last_drop_at_ms"] = now_ms
            _DV_SHADOW_HISTORY_STATUS[
                "last_dropped_snapshot_at_ms"
            ] = _dv_shadow_history_job_capture_ms(job)
        elif event == "write_failure":
            _DV_SHADOW_HISTORY_STATUS["write_failures_total"] += 1
            _DV_SHADOW_HISTORY_STATUS["last_write_failure_at_ms"] = now_ms
        elif event == "worker_start_failure":
            _DV_SHADOW_HISTORY_STATUS["worker_start_failures_total"] += 1
            _DV_SHADOW_HISTORY_STATUS[
                "last_worker_start_failure_at_ms"
            ] = now_ms


def _dv_shadow_history_queue_status():
    """Liefert einen wirkungsfreien, datensparsamen Queue-Status."""

    with _DV_SHADOW_HISTORY_STATUS_LOCK:
        status = dict(_DV_SHADOW_HISTORY_STATUS)
    worker = _DV_SHADOW_HISTORY_WORKER
    status.update(
        {
            "schema_version": "dv_shadow_history_queue_status_v1",
            "capacity": max(
                0,
                int(
                    getattr(
                        _DV_SHADOW_HISTORY_QUEUE,
                        "maxsize",
                        _DV_SHADOW_HISTORY_QUEUE_CAPACITY,
                    )
                    or 0
                ),
            ),
            "queue_depth": max(
                0,
                int(_DV_SHADOW_HISTORY_QUEUE.qsize()),
            ),
            "worker_alive": bool(
                worker is not None and worker.is_alive()
            ),
            "control_effect": False,
        }
    )
    evidence_complete = (
        status["dropped_total"] == 0
        and status["write_failures_total"] == 0
        and status["worker_start_failures_total"] == 0
    )
    status["evidence_complete"] = evidence_complete
    status["status"] = "COMPLETE" if evidence_complete else "EVIDENCE_LIMIT"
    return status


def _prepare_dv_shadow_history_job(plan):
    """Entfernt den Vollpayload und erzeugt einen wirkungsfreien Archivauftrag.

    Dieser Schritt führt keinerlei Datei-I/O aus. Damit kann der produktive
    Plan zuerst atomar veröffentlicht werden; die Diagnosearchivierung läuft
    anschließend unabhängig in einer begrenzten Hintergrundwarteschlange.
    """

    if not isinstance(plan, dict):
        return None
    shadow = plan.pop("_dv_shadow_history_payload", None)
    planner = plan.get("planner") if isinstance(plan.get("planner"), dict) else {}
    summary = (
        planner.get("dv_shadow_v1")
        if isinstance(planner.get("dv_shadow_v1"), dict)
        else None
    )
    if not isinstance(shadow, dict) or not isinstance(summary, dict):
        return None

    job = {
        "shadow": shadow,
        "captured_at_ms": plan.get("generated_at_ts_ms"),
        "productive_context": {
            "plan_id": plan.get("plan_id"),
            "valid_from_ts_ms": plan.get("valid_from_ts_ms"),
            "valid_until_ts_ms": plan.get("valid_until_ts_ms"),
        },
    }

    summary.pop("summary_id", None)
    summary["full_payload_persisted"] = None
    summary["history"] = {
        "schema_version": "dv_shadow_history_v1",
        "status": "ASYNC_AFTER_PLAN_PUBLISH",
        "snapshot_id": None,
        "change_kind": None,
        "queue": _dv_shadow_history_queue_status(),
        "control_effect": False,
    }
    summary["summary_id"] = revision_hash(summary)
    return job


def _write_dv_shadow_history_job(job):
    """Schreibt genau einen vorbereiteten Auftrag im Diagnose-Worker."""

    try:
        try:
            from .dv_shadow_history import append_shadow_history
        except Exception:
            from dv_shadow_history import append_shadow_history
        history_result = append_shadow_history(
            job["shadow"],
            captured_at_ms=job.get("captured_at_ms"),
            productive_context=job.get("productive_context"),
            archive_queue_status=job.get("archive_queue_status"),
        )
        logger.debug(
            "DV-Shadow-Historie verarbeitet: status=%s change=%s",
            (
                "APPENDED"
                if history_result.get("inserted") is True
                else history_result.get("reason", "UNCHANGED")
            ),
            history_result.get("change_kind"),
        )
        return True
    except Exception as exc:
        logger.warning(
            "DV-Shadow-Historie nicht geschrieben; produktiver Plan bleibt "
            "unverändert (%s).",
            type(exc).__name__,
        )
        return False


def _dv_shadow_history_worker():
    """Verarbeitet seriell die begrenzte diagnostische Warteschlange."""

    while True:
        job = _DV_SHADOW_HISTORY_QUEUE.get()
        try:
            write_ok = _write_dv_shadow_history_job(job)
            _record_dv_shadow_history_event("processed", job)
            if not write_ok:
                _record_dv_shadow_history_event("write_failure", job)
        finally:
            _DV_SHADOW_HISTORY_QUEUE.task_done()


def _ensure_dv_shadow_history_worker():
    """Startet den einzigen daemonisierten Diagnose-Worker genau einmal."""

    global _DV_SHADOW_HISTORY_WORKER
    with _DV_SHADOW_HISTORY_WORKER_LOCK:
        worker = _DV_SHADOW_HISTORY_WORKER
        if worker is not None and worker.is_alive():
            return True
        try:
            worker = threading.Thread(
                target=_dv_shadow_history_worker,
                name="dv-shadow-history",
                daemon=True,
            )
            worker.start()
        except Exception as exc:
            _record_dv_shadow_history_event("worker_start_failure")
            logger.warning(
                "DV-Shadow-Historienworker nicht gestartet; produktiver Plan "
                "bleibt unverändert (%s).",
                type(exc).__name__,
            )
            return False
        _DV_SHADOW_HISTORY_WORKER = worker
        return True


def _enqueue_dv_shadow_history(job):
    """Plant Diagnose-I/O nach Planpublikation ein, ohne darauf zu warten."""

    if not isinstance(job, dict) or not _ensure_dv_shadow_history_worker():
        return False
    queued_job = dict(job)
    queued_job["archive_queue_status"] = _dv_shadow_history_queue_status()
    try:
        _DV_SHADOW_HISTORY_QUEUE.put_nowait(queued_job)
        _record_dv_shadow_history_event("accepted", job)
        return True
    except queue.Full:
        _record_dv_shadow_history_event("dropped", job)
        status = _dv_shadow_history_queue_status()
        logger.warning(
            "DV_SHADOW_HISTORY_QUEUE_OVERFLOW EVIDENCE_LIMIT: Snapshot wird "
            "nicht archiviert (depth=%s capacity=%s dropped_total=%s); "
            "produktiver Plan bleibt unverändert.",
            status["queue_depth"],
            status["capacity"],
            status["dropped_total"],
        )
        return False


def _configure_process_timezone():
    """Keep scheduler timestamps aligned with the Berlin-time PHP/frontend views."""
    explicit_tz = os.environ.get("E3DC_TIMEZONE")
    tz_name = explicit_tz or os.environ.get("TZ") or "Europe/Berlin"
    if not explicit_tz and str(tz_name).strip().upper() in ("UTC", "ETC/UTC"):
        tz_name = "Europe/Berlin"
    if hasattr(time, "tzset"):
        try:
            os.environ["TZ"] = tz_name
            time.tzset()
        except Exception:
            pass


_configure_process_timezone()


def _slot_planned_load_w(slot):
    try:
        return max(0.0, float(slot.get("planned_load_w", 0) or 0))
    except Exception:
        return 0.0


def _slot_net_surplus_w(slot):
    return (
        float(slot.get("pv_w", 0) or 0)
        - float(slot.get("home_w", 0) or 0)
        - float(slot.get("wp_w", 0) or 0)
        - float(slot.get("climate_w", 0) or 0)
        - _slot_planned_load_w(slot)
    )


def _slot_storage_chargeable_forecast_contract(
    slot,
    dc_only=False,
    expected_topology_revision=None,
):
    """Bindet prognostizierten Speicherüberschuss an die erlaubte PV-Quelle."""

    slot = slot if isinstance(slot, dict) else {}
    total_surplus_w = max(0.0, _slot_net_surplus_w(slot))
    if not dc_only:
        return {
            "complete": True,
            "reason": "total_pv_legacy_scope",
            "source_scope": "TOTAL_PV",
            "chargeable_surplus_w": total_surplus_w,
            "total_surplus_w": total_surplus_w,
            "e3dc_dc_pv_w": None,
            "external_ac_pv_w": None,
            "forecast_fresh": slot.get("forecast_fresh") is True,
        }

    dc_raw = slot.get("e3dc_dc_pv_w")
    external_raw = slot.get("external_ac_pv_w")
    values_typed = bool(
        isinstance(dc_raw, (int, float))
        and not isinstance(dc_raw, bool)
        and isinstance(external_raw, (int, float))
        and not isinstance(external_raw, bool)
        and math.isfinite(float(dc_raw))
        and math.isfinite(float(external_raw))
    )
    topology_bound = bool(
        str(slot.get("pv_topology_status") or "") == "bound"
        and str(slot.get("pv_resource_projection_status") or "") == "complete"
    )
    slot_topology_revision = str(
        slot.get("pv_topology_revision") or ""
    )
    expected_revision = str(expected_topology_revision or "")
    topology_revision_match = bool(
        slot_topology_revision
        and (
            not expected_revision
            or slot_topology_revision == expected_revision
        )
    )
    forecast_fresh = bool(
        slot.get("forecast_fresh") is True
        or slot.get("pv_forecast_fresh") is True
    )
    complete = bool(
        values_typed
        and float(dc_raw) >= 0.0
        and float(external_raw) >= 0.0
        and topology_bound
        and topology_revision_match
        and forecast_fresh
    )
    if not values_typed:
        reason = "pv_source_split_untyped"
    elif float(dc_raw) < 0.0 or float(external_raw) < 0.0:
        reason = "pv_source_split_negative"
    elif not topology_bound:
        reason = "pv_topology_unbound"
    elif not topology_revision_match:
        reason = "pv_topology_revision_mismatch"
    elif not forecast_fresh:
        reason = "pv_forecast_stale"
    else:
        reason = "e3dc_dc_only_complete"
    e3dc_dc_pv_w = max(0.0, float(dc_raw)) if values_typed else 0.0
    external_ac_pv_w = max(0.0, float(external_raw)) if values_typed else 0.0
    return {
        "complete": complete,
        "reason": reason,
        "source_scope": "E3DC_DC_ONLY",
        # Der Laderahmen darf weder den realen Gesamtüberschuss noch die
        # prognostizierte interne DC-Erzeugung überschreiten.
        "chargeable_surplus_w": (
            min(total_surplus_w, e3dc_dc_pv_w) if complete else 0.0
        ),
        "total_surplus_w": total_surplus_w,
        "e3dc_dc_pv_w": e3dc_dc_pv_w if values_typed else None,
        "external_ac_pv_w": external_ac_pv_w if values_typed else None,
        "forecast_fresh": forecast_fresh,
        "topology_revision": slot_topology_revision or None,
        "expected_topology_revision": expected_revision or None,
        "topology_revision_match": topology_revision_match,
    }


def historical_curve_start_soc(start_soc, first_anchor_soc):
    """Chart-Hilfspunkte duerfen einen eingefrorenen Startanker nicht anheben."""
    try:
        start = float(start_soc)
        anchor = float(first_anchor_soc)
    except Exception:
        return first_anchor_soc
    return min(start, anchor)


def check_awattar(epex_slots, current_soc, target_soc, v4_config):
    """
    Vereinfachte Python-Version von Eba's CheckaWATTar() aus awattar.cpp.

    Returncode (awattar_mode):
      0 = Entladen stoppen (Preis aktuell hoch, Batterie schonen fuer spaeter)
      1 = Normalbetrieb (kein Eingriff)
      2 = Aus Netz laden (Preis gerade guenstig, Batterie nachladen)

    Eba-Logik (vereinfacht):
      - Wenn aktueller Preis <= Niedrigpreis-Schwelle UND SoC < Ziel: Netzladen (2)
      - Wenn aktueller Preis hoch UND ausreichend SoC bis naechstes Preistief: Stopp (0)
      - Sonst: Normal (1)
    """
    if not epex_slots:
        return 1, 'kein EPEX', 0.0

    try:
        def _sf(k, d):
            try: return float(str(v4_config.get(k, d) or d).replace(',', '.'))
            except: return float(d)

        # Konfiguration Batterie-Arbitrage (NICHT dvcarlimit = das ist fuer Wallbox/DV!)
        # bat_buy_price_limit = max. Preis fuer Batterie-Netzladen (EUR/MWh)
        bat_buy_limit = _sf('bat_buy_price_limit', -1.0)  # -1 = deaktiviert
        aw_diff       = _sf('aw_diff',       100.0)  # Mindestdiff High-Low (EUR/MWh)
        aw_aufschlag  = _sf('aw_aufschlag',  1.25)   # Preisaufschlag fuer High-Suche
        reserve_pct   = _sf('ep_reserve_pct', 8.0)   # Notfallreserve %

        # WICHTIG: bat_buy_price_limit steuert BEIDE Richtungen (Arbitrage-Paar):
        # Wenn nicht gesetzt (-1): KEIN Stop-Discharge, KEIN Netzladen.
        if bat_buy_limit <= 0:
            return 1, 'kein bat_buy_price_limit (Arbitrage deaktiviert)', 0.0

        now_ms = time.time() * 1000

        # Aktuellen Slot finden
        curr = next((s for s in epex_slots
                     if s.get('start_timestamp', 0) <= now_ms <= s.get('end_timestamp', 0)), None)
        if curr is None:
            # Erster zukuenftiger Slot
            future = [s for s in epex_slots if s.get('start_timestamp', 0) >= now_ms]
            curr = future[0] if future else epex_slots[0]

        curr_price = float(curr.get('marketprice', 0))  # EUR/MWh

        # Naechste 24h Slots
        cutoff_ms = now_ms + 24 * 3600 * 1000
        future_slots = [s for s in epex_slots
                        if s.get('start_timestamp', 0) > now_ms
                        and s.get('start_timestamp', 0) <= cutoff_ms]

        min_future = min((s.get('marketprice', 9999) for s in future_slots), default=9999)
        max_future = max((s.get('marketprice', 0)    for s in future_slots), default=0)

        # ---- Eba-Logik ----
        # 1. Netzladen: wenn Preis guenstig UND SoC unter Ziel
        #    bat_buy_price_limit = Grenzpreis (EUR/MWh). 0 oder -1 = deaktiviert.
        if bat_buy_limit > 0 and current_soc < target_soc:
            if curr_price <= bat_buy_limit:
                reason = 'Netzladen: %.1f EUR/MWh <= Limit %.1f, SoC %.1f%%<%.1f%%' % (
                    curr_price, bat_buy_limit, current_soc, target_soc)
                return 2, reason, curr_price

        # 2. Entladen stoppen: aktueller Preis hoch, aber bald kommt Billigfenster
        #    (Eba: current > low * aufschlag + Diff -> Speicher fuer spaeter aufheben)
        if future_slots:
            low_price_slot = min(future_slots, key=lambda s: s.get('marketprice', 9999))
            low_price = low_price_slot.get('marketprice', 9999)
            # Preisspread gross genug fuer Arbitrage?
            if curr_price > (low_price * aw_aufschlag + aw_diff):
                # Genug SoC um bis zum Tiefpunkt zu ueberbruecken?
                hours_to_low = max(0, (low_price_slot.get('start_timestamp', now_ms) - now_ms) / 3600000)
                # Grobe Annahme: 3% SoC/h Hausverbrauch (konfigurierbar)
                soc_consumption_pct_h = _sf('aw_soc_per_h', 3.0)
                needed_soc = hours_to_low * soc_consumption_pct_h + reserve_pct
                if current_soc > needed_soc + 5:
                    reason = 'Entladen stopp: aktuell %.1f > %.1f (low=%.1f in %.1fh, SoC%.1f%%>%.1f%%)' % (
                        curr_price, low_price * aw_aufschlag + aw_diff,
                        low_price, hours_to_low, current_soc, needed_soc)
                    return 0, reason, curr_price

        # 3. Normal
        reason = 'Normal: %.1f EUR/MWh (min24h=%.1f max24h=%.1f)' % (
            curr_price, min_future, max_future)
        return 1, reason, curr_price

    except Exception as e:
        logger.warning('check_awattar Fehler: %s' % e)
        return 1, 'Fehler: %s' % e, 0.0


class StorageSimulator:
    @staticmethod
    def _retain_slot_headroom_evidence(slot, pressure, reserve_pressure=None):
        """Hält Slot-Evidence nur für gebundene Topologie oder echten Druck.

        Der Legacy-/topology_unbound-Nullfall bleibt über den Top-Level-Vertrag
        explizit, dupliziert aber keinen großen Druckdatensatz in jedem Slot.
        """

        def _positive(contract):
            if not isinstance(contract, dict):
                return False
            for key in ("dc_pressure_w", "pcc_pressure_w", "combined_pressure_w"):
                try:
                    if float(contract.get(key, 0.0) or 0.0) > 0.0:
                        return True
                except (TypeError, ValueError):
                    continue
            return False

        bound = str(slot.get("pv_topology_status") or "") == "bound"
        material = bound or _positive(pressure) or _positive(reserve_pressure)
        if not material:
            for key in (
                "headroom_pressure",
                "headroom_reserve_pressure",
                "dc_headroom_pressure_w",
                "pcc_headroom_pressure_w",
                "headroom_pressure_w",
            ):
                slot.pop(key, None)
            return False

        slot["headroom_pressure"] = pressure
        if isinstance(reserve_pressure, dict):
            slot["headroom_reserve_pressure"] = reserve_pressure
        else:
            slot.pop("headroom_reserve_pressure", None)
        slot["dc_headroom_pressure_w"] = pressure.get("dc_pressure_w", 0.0)
        slot["pcc_headroom_pressure_w"] = pressure.get("pcc_pressure_w", 0.0)
        slot["headroom_pressure_w"] = pressure.get("combined_pressure_w", 0.0)
        return True

    def _safe_float(self, value, default_val):
        try:
            if value is None or str(value).strip() == "":
                return float(default_val)
            return float(str(value).replace(',', '.'))
        except ValueError:
            return float(default_val)

    def _cfg_bool(self, value, default=False):
        if value is None:
            return bool(default)
        if isinstance(value, bool):
            return value
        return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")

    def _refresh_pcc_headroom_limit_contract(self, live_readback_limit_w=None):
        configured_limit_w = self._safe_float(self.v4_config.get("einspeiselimit", 0.0), 0.0)
        if 0.0 < configured_limit_w < 100.0:
            configured_limit_w *= 1000.0
        buffer_w = max(
            0.0,
            self._safe_float(self.v4_config.get("abregel_puffer_w", 300.0), 300.0),
        )
        self.configured_export_limit_w = max(0.0, configured_limit_w)
        self.live_derate_limit_w = (
            max(0.0, self._safe_float(live_readback_limit_w, 0.0))
            if live_readback_limit_w is not None
            else 0.0
        )
        self.pcc_headroom_limit_contract = resolve_buffered_pcc_limit(
            self.configured_export_limit_w,
            self.live_derate_limit_w,
            buffer_w,
        )
        return self.pcc_headroom_limit_contract

    def _pcc_headroom_limit_for_topology(self, topology_status):
        """Wählt die PCC-Grenze ohne aggressivere Legacy-Wirkung.

        Der gepufferte Reglervertrag wirkt erst bei vollständig typisiertem
        DC/AC-Split. Ungebundene Altpläne behalten exakt die bisherige
        ungepufferte PCC-Grenze.
        """

        contract = dict(getattr(self, "pcc_headroom_limit_contract", {}) or {})
        if str(topology_status or "") == "bound" and contract.get("active") is True:
            return {
                **contract,
                "applied": True,
                "application": "typed_topology_buffered_pcc",
            }
        legacy_limit = max(0.0, self._safe_float(getattr(self, "export_limit_w", 0.0), 0.0))
        return {
            "schema_version": contract.get("schema_version", "pcc_headroom_limit_v1"),
            "active": legacy_limit > 0.0,
            "limit_w": legacy_limit if legacy_limit > 0.0 else None,
            "hard_limit_w": legacy_limit if legacy_limit > 0.0 else None,
            "buffer_w": 0.0,
            "source": getattr(self, "export_limit_source", "unavailable"),
            "applied": legacy_limit > 0.0,
            "application": "legacy_topology_unbound_unchanged",
            "typed_candidate": contract,
            "zero_semantics": "MISSING_UNLESS_EXPLICITLY_ACTIVE",
        }
    @staticmethod
    def _percentile_value(values, q):
        clean = []
        for value in values or []:
            try:
                number = float(value)
            except Exception:
                continue
            if math.isfinite(number):
                clean.append(number)
        if not clean:
            return None
        clean.sort()
        q = max(0.0, min(1.0, float(q)))
        idx = int(math.ceil(q * len(clean))) - 1
        return clean[max(0, min(len(clean) - 1, idx))]

    @staticmethod
    def _curve_target_mode_from_config(config):
        raw = ""
        if isinstance(config, dict):
            raw = str(config.get("storage_curve_target_mode", "anchored") or "").strip().lower()
        raw = raw.replace("-", "_").replace(" ", "_")
        if raw in ("forecast_100", "forecast_only_100", "forecast_only", "prognose_100", "prognose"):
            return "forecast_100"
        return "anchored"

    def _forecast_only_curve_enabled(self):
        return self._curve_target_mode_from_config(getattr(self, "v4_config", {})) == "forecast_100"

    def _load_manual_soc_anchor(self, now_s=None):
        try:
            with open(MANUAL_ANCHOR_FILE, "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except Exception as exc:
            logger.debug("Manueller SoC-Anker konnte nicht gelesen werden: %s", exc)
            return {}
        if not isinstance(data, dict) or not self._cfg_bool(data.get("active", True), True):
            return {}
        mode = str(data.get("mode", "") or "").strip().lower()
        target_soc = self._safe_float(data.get("target_soc", data.get("reached_soc")), -1.0)
        if mode not in ("charge", "discharge") or target_soc < 0.0:
            return {}
        now = self._safe_float(now_s, time.time())
        expires_ts = self._safe_float(data.get("expires_ts"), 0.0)
        if expires_ts > 0.0 and expires_ts < now:
            return {}
        anchor = dict(data)
        anchor["mode"] = mode
        anchor["target_soc"] = max(0.0, min(100.0, target_soc))
        anchor["reached_soc"] = max(
            0.0,
            min(100.0, self._safe_float(data.get("reached_soc"), anchor["target_soc"])),
        )
        return anchor

    def _apply_manual_anchor_to_adaptive_headroom(
        self,
        adaptive_headroom,
        manual_anchor,
        *,
        now_ms,
        current_soc,
        target_soc,
        capacity_wh,
        forecast_surplus_wh,
        can_reach_target,
    ):
        if not isinstance(adaptive_headroom, dict):
            adaptive_headroom = {}
        if not isinstance(manual_anchor, dict) or not manual_anchor:
            return adaptive_headroom

        def _norm_points(points):
            clean = []
            for point in points or []:
                if not isinstance(point, dict):
                    continue
                try:
                    ts = int(float(point.get("ts", 0) or 0))
                    soc = float(point.get("soc", 0) or 0)
                except Exception:
                    continue
                if ts > 0:
                    clean.append({"ts": ts, "soc": round(max(0.0, min(100.0, soc)), 2)})
            clean.sort(key=lambda item: item["ts"])
            return clean

        def _curve_at(points, anchor_ms, fallback_soc):
            if not points:
                return float(fallback_soc)
            if anchor_ms <= points[0]["ts"]:
                return float(points[0]["soc"])
            if anchor_ms >= points[-1]["ts"]:
                return float(points[-1]["soc"])
            for idx in range(1, len(points)):
                left = points[idx - 1]
                right = points[idx]
                if anchor_ms <= right["ts"]:
                    span = max(1.0, float(right["ts"] - left["ts"]))
                    frac = max(0.0, min(1.0, (float(anchor_ms) - float(left["ts"])) / span))
                    return float(left["soc"]) + (float(right["soc"]) - float(left["soc"])) * frac
            return float(points[-1]["soc"])

        def _merge_points(points, prefer_low):
            merged = {}
            for point in points:
                ts = int(point["ts"])
                soc = round(max(0.0, min(100.0, float(point["soc"]))), 2)
                if ts not in merged:
                    merged[ts] = soc
                elif prefer_low:
                    merged[ts] = min(merged[ts], soc)
                else:
                    merged[ts] = max(merged[ts], soc)
            return [{"ts": ts, "soc": merged[ts]} for ts in sorted(merged)]

        mode = str(manual_anchor.get("mode", "") or "").strip().lower()
        raw_anchor_soc = self._safe_float(manual_anchor.get("target_soc"), -1.0)
        anchor_soc = max(0.0, min(100.0, raw_anchor_soc))
        target = max(0.0, min(100.0, self._safe_float(target_soc, 0.0)))
        capacity = max(0.0, self._safe_float(capacity_wh, 0.0))
        now = int(self._safe_float(now_ms, time.time() * 1000.0))
        current = max(0.0, min(100.0, self._safe_float(current_soc, anchor_soc)))
        forecast_surplus = max(0.0, self._safe_float(forecast_surplus_wh, 0.0))
        hold_h = max(
            0.25,
            min(6.0, self._safe_float(self.v4_config.get("storage_manual_anchor_hold_h", 2.0), 2.0)),
        )
        anchor_start_s = self._safe_float(
            manual_anchor.get("ts", manual_anchor.get("manual_ts")),
            now / 1000.0,
        )
        if anchor_start_s <= 0.0:
            anchor_start_s = now / 1000.0
        hold_until = int(anchor_start_s * 1000.0 + hold_h * 3600000.0)
        expires_ts = self._safe_float(manual_anchor.get("expires_ts"), 0.0)
        if expires_ts > 0.0:
            hold_until = min(hold_until, int(expires_ts * 1000.0))
        margin = max(
            1.0,
            min(1.35, self._safe_float(self.v4_config.get("storage_manual_anchor_forecast_margin", 1.10), 1.10)),
        )
        reserve_soc = max(0.0, self._safe_float(self.v4_config.get("ep_reserve_pct", 8.0), 8.0))
        manual_min_soc = max(
            reserve_soc + 2.0,
            self._safe_float(self.v4_config.get("storage_manual_anchor_min_soc", 10.0), 10.0),
        )
        manual_min_soc = max(0.0, min(target if target > 0.0 else 100.0, manual_min_soc))
        floor_points = _norm_points(adaptive_headroom.get("soc_min_curve"))
        if not floor_points:
            floor_points = [{"ts": now, "soc": round(current, 2)}]
        current_floor = _curve_at(floor_points, now, current)

        adaptive_headroom["manual_anchor_mode"] = mode
        adaptive_headroom["manual_anchor_target_soc"] = round(anchor_soc, 2)
        adaptive_headroom["manual_anchor_hold_h"] = round(hold_h, 2)
        adaptive_headroom["manual_anchor_start_ts"] = int(anchor_start_s * 1000.0)
        adaptive_headroom["manual_anchor_hold_until_ts"] = int(hold_until)
        adaptive_headroom["manual_anchor_forecast_surplus_wh"] = round(forecast_surplus, 0)
        adaptive_headroom["manual_anchor_active"] = False

        if mode not in ("charge", "discharge") or raw_anchor_soc < 0.0:
            adaptive_headroom["manual_anchor_reason"] = "ungueltiger manueller Anker"
            return adaptive_headroom
        if now > hold_until:
            adaptive_headroom["manual_anchor_reason"] = "Manuelles Anker-Haltefenster ist abgelaufen"
            return adaptive_headroom
        if target > 0.0:
            anchor_soc = min(anchor_soc, target)
        if mode == "discharge" and anchor_soc < manual_min_soc - 0.05:
            adaptive_headroom["manual_anchor_reason"] = (
                "Manueller Entladeanker unter Sicherheitsboden %.1f%% ignoriert" % manual_min_soc
            )
            return adaptive_headroom
        if not bool(can_reach_target):
            adaptive_headroom["manual_anchor_reason"] = "Tagesziel laut Prognose nicht sicher erreichbar"
            return adaptive_headroom

        needed_wh = max(0.0, (target - anchor_soc) * capacity / 100.0) * margin
        adaptive_headroom["manual_anchor_forecast_need_wh"] = round(needed_wh, 0)
        if capacity > 0.0 and forecast_surplus + 1.0 < needed_wh:
            adaptive_headroom["manual_anchor_reason"] = (
                "Restprognose %.0fWh reicht fuer manuellen Anker nicht sicher aus" % forecast_surplus
            )
            return adaptive_headroom

        headroom_target = self._safe_float(
            adaptive_headroom.get("adaptive_headroom_target_soc"),
            target,
        )
        if mode == "charge" and target > 0.0 and anchor_soc > headroom_target + 0.2:
            adaptive_headroom["manual_anchor_reason"] = (
                "Abregel-Headroom hat Vorrang vor manuellem Ladeanker %.1f%%" % anchor_soc
            )
            return adaptive_headroom

        prefer_low = mode == "discharge"
        if mode == "discharge" and current_floor <= anchor_soc + 0.05:
            adaptive_headroom["manual_anchor_reason"] = "Kurvenboden liegt bereits beim manuellen Ziel"
            return adaptive_headroom
        if mode == "charge" and current_floor >= anchor_soc - 0.05:
            adaptive_headroom["manual_anchor_reason"] = "Kurvenboden liegt bereits beim manuellen Ziel"
            return adaptive_headroom

        adjusted = []
        changed = 0
        for point in floor_points:
            ts = int(point["ts"])
            soc = float(point["soc"])
            if now <= ts <= hold_until:
                if mode == "discharge" and soc > anchor_soc + 0.05:
                    soc = anchor_soc
                    changed += 1
                elif mode == "charge" and soc < anchor_soc - 0.05:
                    soc = anchor_soc
                    changed += 1
            adjusted.append({"ts": ts, "soc": round(soc, 2)})
        adjusted.extend([
            {"ts": now, "soc": round(anchor_soc, 2)},
            {"ts": hold_until, "soc": round(anchor_soc, 2)},
        ])
        adaptive_headroom["soc_min_curve"] = _merge_points(adjusted, prefer_low=prefer_low)
        adaptive_headroom["adaptive_soc_floor"] = round(anchor_soc, 2)
        if mode == "charge":
            ceiling_points = _norm_points(adaptive_headroom.get("soc_ceiling_curve"))
            if ceiling_points:
                ceiling_points.extend([
                    {"ts": now, "soc": round(anchor_soc, 2)},
                    {"ts": hold_until, "soc": round(anchor_soc, 2)},
                ])
                adaptive_headroom["soc_ceiling_curve"] = _merge_points(ceiling_points, prefer_low=False)
            adaptive_headroom["adaptive_soc_ceiling"] = round(
                max(anchor_soc, self._safe_float(adaptive_headroom.get("adaptive_soc_ceiling"), anchor_soc)),
                2,
            )
        adaptive_headroom["manual_anchor_active"] = True
        adaptive_headroom["manual_anchor_floor_soc"] = round(anchor_soc, 2)
        adaptive_headroom["manual_anchor_hold_until_ts"] = int(hold_until)
        adaptive_headroom["manual_anchor_adjusted_points"] = int(changed)
        adaptive_headroom["manual_anchor_reason"] = (
            "Manueller %s-Anker %.1f%% prognosebegrenzt fuer %.1fh uebernommen"
            % ("Lade" if mode == "charge" else "Entlade", anchor_soc, hold_h)
        )
        return adaptive_headroom

    @staticmethod
    def _start_anchor_floor(proposed_start_soc, minimum_start_soc):
        """Clamp the first curve anchor to the configured planning floor."""
        try:
            proposed = float(proposed_start_soc)
            minimum = float(minimum_start_soc)
        except Exception:
            try:
                return float(proposed_start_soc), False
            except Exception:
                return proposed_start_soc, False

        clamped = max(proposed, minimum)
        return clamped, clamped > proposed + 0.05

    @staticmethod
    def _normalise_curve_points(points, day_start_ms=0, day_end_ms=0, target_soc=None):
        """Return sorted curve points with numeric ms timestamps and clamped SoC."""
        clean = []
        try:
            day_start = float(day_start_ms or 0)
        except Exception:
            day_start = 0.0
        try:
            day_end = float(day_end_ms or 0)
        except Exception:
            day_end = 0.0
        try:
            max_soc = float(target_soc) if target_soc is not None else 100.0
        except Exception:
            max_soc = 100.0
        max_soc = max(0.0, min(100.0, max_soc))
        for point in points or []:
            if not isinstance(point, dict):
                continue
            try:
                ts = int(float(point.get("ts", 0) or 0))
                soc = float(point.get("soc", 0) or 0)
            except Exception:
                continue
            if ts <= 0:
                continue
            if day_start and ts < day_start - 60000:
                continue
            if day_end and ts >= day_end + 60000:
                continue
            clean.append({"ts": ts, "soc": round(max(0.0, min(max_soc, soc)), 2)})
        clean.sort(key=lambda item: item["ts"])
        merged = []
        for point in clean:
            if merged and abs(point["ts"] - merged[-1]["ts"]) <= 1000:
                merged[-1]["soc"] = max(merged[-1]["soc"], point["soc"])
                continue
            merged.append(point)
        return merged

    @staticmethod
    def _curve_soc_at_points(points, ts, default=None):
        """Interpolate a SoC value from sorted curve points."""
        if not points:
            return default
        try:
            ts_f = float(ts)
        except Exception:
            return default
        if ts_f <= float(points[0]["ts"]):
            return float(points[0]["soc"])
        if ts_f >= float(points[-1]["ts"]):
            return float(points[-1]["soc"])
        for idx in range(1, len(points)):
            left = points[idx - 1]
            right = points[idx]
            if ts_f <= float(right["ts"]):
                span = max(1.0, float(right["ts"]) - float(left["ts"]))
                frac = max(0.0, min(1.0, (ts_f - float(left["ts"])) / span))
                return float(left["soc"]) + (float(right["soc"]) - float(left["soc"])) * frac
        return default

    @staticmethod
    def _published_curve_floor_from_plan(
        plan,
        day_start_ms,
        day_end_ms,
        mode,
        curve_start_policy,
        planning_target_soc,
    ):
        """Load the same-day published curve as no-downshift floor."""
        if not isinstance(plan, dict) or not plan:
            return {"active": False, "points": [], "source": "", "reason": "no_existing_plan"}
        meta = plan.get("target_curve_meta") if isinstance(plan.get("target_curve_meta"), dict) else {}
        if meta.get("mode") and str(meta.get("mode")) != str(mode):
            return {"active": False, "points": [], "source": "", "reason": "mode_changed"}
        if meta.get("curve_start_policy") and str(meta.get("curve_start_policy")) != str(curve_start_policy):
            return {"active": False, "points": [], "source": "", "reason": "curve_start_policy_changed"}
        try:
            existing_day = float(meta.get("curve_day_start_ts", 0) or 0)
            day_start = float(day_start_ms or 0)
        except Exception:
            existing_day = 0.0
            day_start = 0.0
        if existing_day and day_start and abs(existing_day - day_start) > 60000:
            return {"active": False, "points": [], "source": "", "reason": "day_changed"}
        try:
            old_target = float(plan.get("planning_target_soc", plan.get("target_soc", 0)) or 0)
            new_target = float(planning_target_soc or 0)
        except Exception:
            old_target = 0.0
            new_target = 0.0
        if old_target > 0 and new_target > 0 and abs(old_target - new_target) > 0.5:
            return {"active": False, "points": [], "source": "", "reason": "planning_target_changed"}

        for source in ("published_target_floor_curve", "target_timeline", "soc_min_curve", "curve_anchors"):
            points = StorageSimulator._normalise_curve_points(
                plan.get(source) or [],
                day_start_ms,
                day_end_ms,
                planning_target_soc or plan.get("target_soc") or 100.0,
            )
            if len(points) >= 2:
                return {"active": True, "points": points, "source": source, "reason": "ok"}
        return {"active": False, "points": [], "source": "", "reason": "no_curve_points"}

    @staticmethod
    def _protect_curve_points_against_floor(points, floor_points, target_soc=100.0):
        """Prevent a newly computed curve from dropping below the published floor."""
        diag = {
            "active": bool(floor_points),
            "points_clamped": 0,
            "max_lift_pct": 0.0,
        }
        if not points or not floor_points:
            return diag
        try:
            max_soc = max(0.0, min(100.0, float(target_soc)))
        except Exception:
            max_soc = 100.0
        for point in points:
            if not isinstance(point, dict):
                continue
            try:
                ts = float(point.get("ts", 0) or 0)
                old_soc = float(point.get("soc", 0) or 0)
            except Exception:
                continue
            floor_soc = StorageSimulator._curve_soc_at_points(floor_points, ts, None)
            if floor_soc is None:
                continue
            new_soc = round(max(old_soc, min(max_soc, float(floor_soc))), 2)
            if new_soc > old_soc + 0.05:
                point["soc"] = new_soc
                diag["points_clamped"] += 1
                diag["max_lift_pct"] = max(diag["max_lift_pct"], round(new_soc - old_soc, 2))
        return diag

    @staticmethod
    def _smooth_tail_target_anchors(anchors, now_ms, target_soc, current_soc=None, frontload_factor=1.35):
        """Lift late future anchors so the final target does not become a cliff.

        The target path is intentionally slightly front-loaded. Real PV days
        often lose usable evening energy earlier than the forecast says, so a
        linear tail still arrives too late on small batteries.
        """
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        def _is_user_curve_anchor(anchor):
            return str((anchor or {}).get("kind") or "") in {"intermediate", "noon"}

        if not anchors or len(anchors) < 2:
            return 0
        removed_transient = sum(
            1 for anchor in anchors
            if str((anchor or {}).get("kind") or "") == "tail_target_start"
        )
        if removed_transient:
            anchors[:] = [
                anchor for anchor in anchors
                if str((anchor or {}).get("kind") or "") != "tail_target_start"
            ]
        if len(anchors) < 2:
            return removed_transient
        anchors.sort(key=lambda a: _sf(a.get("ts", 0.0), 0.0))
        final_ts = _sf(anchors[-1].get("ts", 0.0), 0.0)
        final_soc = max(0.0, min(100.0, _sf(target_soc, anchors[-1].get("soc", 0.0))))
        now = _sf(now_ms, 0.0)
        if final_ts <= now or final_soc <= 0.0:
            return removed_transient

        active_idx = 0
        for idx, anchor in enumerate(anchors[:-1]):
            if _sf(anchor.get("ts", 0.0), 0.0) <= now:
                active_idx = idx

        base_soc = max(0.0, min(final_soc, _sf(anchors[active_idx].get("soc", 0.0), 0.0)))
        # Der Sollpfad bleibt am eingefrorenen Anker. Der Live-SoC darf hier
        # nicht als neuer Kurvenfuss wirken, sonst wandert die Anzeige mit.
        if final_soc <= base_soc + 0.1:
            return removed_transient
        frontload = max(1.0, min(2.5, _sf(frontload_factor, 1.35)))

        start_ts = _sf(anchors[active_idx].get("ts", 0.0), now)
        if start_ts >= final_ts:
            return removed_transient

        changes = removed_transient

        total_ms = max(1.0, final_ts - start_ts)
        for anchor in anchors:
            if _is_user_curve_anchor(anchor):
                continue
            ts = _sf(anchor.get("ts", 0.0), 0.0)
            if ts <= start_ts or ts > final_ts:
                continue
            frac = max(0.0, min(1.0, (ts - start_ts) / total_ms))
            eased_frac = 1.0 - ((1.0 - frac) ** frontload)
            desired_soc = base_soc + (final_soc - base_soc) * eased_frac
            old_soc = _sf(anchor.get("soc", desired_soc), desired_soc)
            if old_soc < desired_soc - 0.05:
                anchor["soc"] = round(desired_soc, 2)
                if anchor.get("frozen"):
                    anchor["tail_lifted"] = True
                changes += 1
        anchors[-1]["soc"] = round(final_soc, 2)
        anchors[-1]["t"] = datetime.fromtimestamp(final_ts / 1000).strftime("%H:%M")
        return changes

    @staticmethod
    def _conservative_curve_end_ts(slots, pv_start_ts, pv_end_ts, offset_ms, relevant_surplus_w):
        """Return the charge-curve end from the last relevant PV surplus slot."""
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        start_ts = _sf(pv_start_ts, 0.0)
        end_ts = _sf(pv_end_ts, 0.0)
        offset = max(0.0, _sf(offset_ms, 0.0))
        if start_ts <= 0.0 or end_ts <= start_ts:
            return int(end_ts or 0)

        min_end_ts = start_ts + 3600000
        curve_end = max(min_end_ts, end_ts - offset)
        relevant_w = max(200.0, _sf(relevant_surplus_w, 500.0))
        last_relevant_ts = None
        last_positive_ts = None
        for slot in slots or []:
            ts = _sf(slot.get("ts"), 0.0)
            if ts < start_ts or ts > end_ts:
                continue
            pv_w = _sf(slot.get("pv_w"), 0.0)
            surplus_w = _sf(slot.get("surplus_w"), _slot_net_surplus_w(slot))
            if pv_w > 500.0 and surplus_w > 200.0:
                last_positive_ts = ts
                if surplus_w >= relevant_w:
                    last_relevant_ts = ts

        if last_relevant_ts is not None:
            curve_end = max(min_end_ts, last_relevant_ts - offset)
        elif last_positive_ts is not None:
            curve_end = max(min_end_ts, last_positive_ts - offset)
        return int(curve_end)

    @staticmethod
    def _forecast100_late_full_curve_end_ts(pv_start_ts, pv_end_ts, offset_ms):
        """Return the latest Forecast-100 landing time from the raw PV window.

        Forecast-100 optimizes for a late 100% landing. It should not inherit
        the anchored curve's conservative "last relevant surplus" shortcut,
        because that can park a large battery at 100% for several hours.
        """
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        start_ts = _sf(pv_start_ts, 0.0)
        end_ts = _sf(pv_end_ts, 0.0)
        offset = max(0.0, _sf(offset_ms, 0.0))
        if start_ts <= 0.0 or end_ts <= start_ts:
            return int(end_ts or 0)
        return int(max(start_ts + 3600000.0, end_ts - offset))

    @staticmethod
    def _extend_curve_end_for_user_anchors(
        curve_end_ts,
        today_0_ms,
        today_end_ms,
        timeline_start_ts,
        target_soc,
        raw_intermediate_anchors,
        anchor_step_ms,
    ):
        """Keep configured curve anchors from being clipped by the automatic release time."""
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        adjusted_end = int(_sf(curve_end_ts, 0.0))
        day_start = _sf(today_0_ms, 0.0)
        day_end = _sf(today_end_ms, day_start + 86400000.0)
        start_ts = _sf(timeline_start_ts, day_start)
        target = _sf(target_soc, 100.0)
        step_ms = int(max(15 * 60000, _sf(anchor_step_ms, 3600000.0)))
        extensions = []
        if adjusted_end <= 0 or day_start <= 0:
            return adjusted_end, extensions

        for candidate in sorted(raw_intermediate_anchors or [], key=lambda a: _sf((a or {}).get("hour", 0.0), 0.0)):
            try:
                raw_soc = _sf(candidate.get("soc", 0.0), 0.0)
                if raw_soc <= 0.0:
                    continue
                anchor_h = max(0.0, min(23.75, _sf(candidate.get("hour", 0.0), 0.0)))
                anchor_ts = int(day_start + (anchor_h * 3600000.0))
                if anchor_ts <= start_ts + 60000.0 or anchor_ts >= day_end:
                    continue
                if anchor_ts < adjusted_end - 60000:
                    continue
                required_end = min(int(day_end), anchor_ts + step_ms)
                if required_end <= adjusted_end:
                    continue
                extensions.append({
                    "label": candidate.get("label", ""),
                    "source_key": candidate.get("source_key", ""),
                    "anchor_ts": int(anchor_ts),
                    "anchor_t": datetime.fromtimestamp(anchor_ts / 1000).strftime("%H:%M"),
                    "anchor_soc": round(min(target, raw_soc), 2),
                    "old_curve_end_ts": int(adjusted_end),
                    "old_curve_end_t": datetime.fromtimestamp(adjusted_end / 1000).strftime("%H:%M"),
                    "new_curve_end_ts": int(required_end),
                    "new_curve_end_t": datetime.fromtimestamp(required_end / 1000).strftime("%H:%M"),
                })
                adjusted_end = int(required_end)
            except Exception:
                continue
        return int(adjusted_end), extensions

    @staticmethod
    def _build_anchor_registry(target_curve_meta, curve_anchors, adaptive_headroom, now_ms=0):
        """Classify effective curve anchors without changing the curve itself."""
        def _sf(value, default=0.0):
            try:
                number = float(value)
                return number if math.isfinite(number) else float(default)
            except Exception:
                return float(default)

        def _ts(value):
            raw = _sf(value, 0.0)
            return int(raw) if raw > 0 else 0

        def _soc(value):
            raw = _sf(value, -1.0)
            if raw < 0.0:
                return None
            return round(max(0.0, min(100.0, raw)), 2)

        meta = target_curve_meta if isinstance(target_curve_meta, dict) else {}
        adaptive = adaptive_headroom if isinstance(adaptive_headroom, dict) else {}
        anchors = [dict(a) for a in curve_anchors or [] if isinstance(a, dict)]
        curve_end_ts = _ts(meta.get("curve_end_ts"))
        registry = []
        seen_ids = set()

        def add(anchor_id, kind, owner, priority, priority_rank, active, source, **extra):
            if not anchor_id or anchor_id in seen_ids:
                return
            seen_ids.add(anchor_id)
            item = {
                "id": str(anchor_id),
                "kind": str(kind),
                "owner": str(owner),
                "priority": str(priority),
                "priority_rank": int(priority_rank),
                "active": bool(active),
                "source": str(source),
                "rule": str(extra.pop("rule", "")),
            }
            ts = _ts(extra.pop("ts", 0))
            if ts:
                item["ts"] = ts
            expires_ts = _ts(extra.pop("expires_ts", 0))
            if expires_ts:
                item["expires_ts"] = expires_ts
            soc = _soc(extra.pop("soc", None))
            if soc is not None:
                item["soc"] = soc
            target_soc = _soc(extra.pop("target_soc", None))
            if target_soc is not None:
                item["target_soc"] = target_soc
            reason = str(extra.pop("reason", "") or "")
            if reason:
                item["reason"] = reason
            locked = extra.pop("locked", None)
            if locked is not None:
                item["locked"] = bool(locked)
            for key, value in sorted(extra.items()):
                if value is not None and value != "":
                    item[str(key)] = value
            registry.append(item)

        if anchors:
            first = anchors[0]
            first_ts = _ts(first.get("ts"))
            first_kind = str(first.get("kind") or meta.get("start_anchor_kind") or "hourly")
            add(
                "curve_start",
                "forecast_anchor",
                "storage_simulator",
                "forecast",
                40,
                True,
                "curve_anchors[0]",
                ts=first_ts,
                expires_ts=curve_end_ts,
                soc=first.get("soc"),
                locked=first.get("frozen", False),
                anchor_kind=first_kind,
                rule="Frozen start anchor; live reanchor is disabled and must not slide on replans.",
            )
            last = anchors[-1]
            add(
                "curve_end",
                "forecast_anchor",
                "storage_simulator",
                "forecast",
                40,
                True,
                "curve_anchors[-1]",
                ts=last.get("ts"),
                soc=last.get("soc"),
                locked=last.get("frozen", False),
                anchor_kind=str(last.get("kind") or "target"),
                rule="End-of-curve target anchor; automatic release may open after this point.",
            )
            for anchor in anchors:
                source_key = str(anchor.get("source_key") or "")
                if not source_key:
                    continue
                add(
                    "user_%s" % source_key,
                    "user_anchor",
                    "user_config",
                    "user",
                    70,
                    True,
                    "curve_anchors",
                    ts=anchor.get("ts"),
                    expires_ts=curve_end_ts,
                    soc=anchor.get("soc"),
                    locked=bool(meta.get("intermediate_anchors_locked") or anchor.get("frozen")),
                    label=anchor.get("label"),
                    source_key=source_key,
                    hour_key=anchor.get("hour_key"),
                    rule="Configured intermediate anchors are hard user targets; reachability may not silently lift them.",
                )

        if bool(meta.get("forecast_only_target_active")):
            add(
                "forecast_only_target",
                "forecast_anchor",
                "forecast_curve",
                "forecast",
                40,
                True,
                "target_curve_meta",
                expires_ts=curve_end_ts,
                soc=meta.get("forecast_only_target_soc", 100.0),
                rule="Forecast-only target has no user anchors; catch-up must use guarded surplus logic.",
            )

        if bool(meta.get("predump_curve_active")) or _sf(meta.get("predump_dump_wh"), 0.0) >= 200.0 or bool(meta.get("hard_predump_enabled")):
            add(
                "predump_curve",
                "predump_anchor",
                "predump",
                "predump",
                80,
                bool(meta.get("predump_curve_active") or meta.get("hard_predump_enabled")),
                "target_curve_meta",
                ts=meta.get("predump_curve_start_ts") or meta.get("predump_start_ts"),
                expires_ts=meta.get("predump_end_ts") or curve_end_ts,
                soc=meta.get("predump_curve_soc") or meta.get("hard_predump_target_soc"),
                reason=meta.get("predump_reason", ""),
                hard_predump=bool(meta.get("hard_predump_enabled")),
                rule="Pre-Dump anchor is valid only for its dump window and is not a normal night floor.",
            )

        manual_target = adaptive.get("manual_anchor_target_soc")
        if manual_target is not None:
            add(
                "manual_battery_anchor",
                "manual_temporary_anchor",
                "manual_override",
                "manual",
                60,
                bool(adaptive.get("manual_anchor_active")),
                "manual_bat_anchor.json",
                ts=adaptive.get("manual_anchor_start_ts"),
                expires_ts=adaptive.get("manual_anchor_hold_until_ts"),
                soc=adaptive.get("manual_anchor_floor_soc", manual_target),
                target_soc=manual_target,
                mode=adaptive.get("manual_anchor_mode", ""),
                reason=adaptive.get("manual_anchor_reason", ""),
                rule="Manual anchor expires from the original manual timestamp; replans must not move this window.",
            )

        has_adaptive_band = bool(adaptive.get("soc_min_curve")) or bool(adaptive.get("soc_ceiling_curve"))
        has_headroom = _sf(adaptive.get("adaptive_headroom_required_wh"), 0.0) > 0.0
        has_reserve = bool(adaptive.get("headroom_reserve_active"))
        has_comfort = bool(adaptive.get("adaptive_comfort_active"))
        if has_adaptive_band or has_headroom or has_reserve or has_comfort:
            add(
                "adaptive_headroom_band",
                "safety_anchor",
                "adaptive_headroom",
                "safety",
                90,
                True,
                "adaptive_headroom",
                ts=now_ms,
                expires_ts=curve_end_ts,
                soc=adaptive.get("adaptive_soc_floor"),
                target_soc=adaptive.get("adaptive_soc_ceiling"),
                headroom_required_wh=adaptive.get("adaptive_headroom_required_wh", 0.0),
                headroom_reserve_active=bool(adaptive.get("headroom_reserve_active")),
                rule="Safety band must expose floor and ceiling explicitly; hidden hard floors are not allowed.",
            )

        if bool(meta.get("wallbox_floor_soc_active")) or bool(meta.get("wallbox_target_soc_active")):
            add(
                "wallbox_soc_floor",
                "safety_anchor",
                "wallbox_policy",
                "safety",
                85,
                True,
                "target_curve_meta",
                expires_ts=curve_end_ts,
                soc=meta.get("wallbox_floor_soc") or meta.get("wallbox_target_soc"),
                reason=meta.get("wallbox_floor_reason", meta.get("wallbox_target_reason", "")),
                rule="Wallbox support floor is policy-owned and must be visible before it changes storage support.",
            )

        storm_guard = meta.get("storm_guard") if isinstance(meta.get("storm_guard"), dict) else {}
        if bool(storm_guard.get("active") or storm_guard.get("grid_charge_active")):
            add(
                "storm_guard",
                "safety_anchor",
                "storm_guard",
                "safety",
                95,
                True,
                "storm_guard",
                ts=now_ms,
                expires_ts=storm_guard.get("expires_ts") or curve_end_ts,
                soc=storm_guard.get("target_soc") or storm_guard.get("reserve_soc"),
                reason=storm_guard.get("reason", ""),
                rule="Storm and emergency reserve owns the highest safety priority.",
            )

        registry.sort(key=lambda item: (-int(item.get("priority_rank", 0)), int(item.get("ts", 0))))
        counts = {}
        for item in registry:
            counts[item["kind"]] = counts.get(item["kind"], 0) + 1
        return {
            "version": "anchor_registry_v1",
            "policy": "explicit_owner_expiry_priority_no_live_reanchor",
            "summary": {
                "total": len(registry),
                "by_kind": counts,
                "highest_priority": registry[0]["priority"] if registry else "none",
            },
            "anchors": registry,
        }

    @staticmethod
    def _build_hardening_contracts(
        target_curve_meta,
        adaptive_headroom,
        anchor_registry,
        direct_marketing,
        cheap_grid_charge=None,
        storm_grid_charge=None,
        market_plan=None,
    ):
        """Stellt Härtungsfelder als maschinenlesbare Laufzeitverträge bereit."""
        def _sf(value, default=0.0):
            try:
                number = float(value)
                return number if math.isfinite(number) else float(default)
            except Exception:
                return float(default)

        def _truthy(value):
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in ("1", "true", "yes", "on", "ja", "ein")

        meta = target_curve_meta if isinstance(target_curve_meta, dict) else {}
        adaptive = adaptive_headroom if isinstance(adaptive_headroom, dict) else {}
        registry = anchor_registry if isinstance(anchor_registry, dict) else {}
        direct = direct_marketing if isinstance(direct_marketing, dict) else {}
        cheap = cheap_grid_charge if isinstance(cheap_grid_charge, dict) else {}
        storm = storm_grid_charge if isinstance(storm_grid_charge, dict) else {}
        market = market_plan if isinstance(market_plan, dict) else {}
        direct_flags = direct.get("flags") if isinstance(direct.get("flags"), dict) else {}
        direct_blocked = direct.get("blocked_reasons") if isinstance(direct.get("blocked_reasons"), list) else []
        direct_economics = direct.get("economics") if isinstance(direct.get("economics"), dict) else {}
        market_blocked = market.get("blocked_reasons") if isinstance(market.get("blocked_reasons"), list) else []
        market_economics = market.get("economics") if isinstance(market.get("economics"), dict) else {}
        anchor_items = registry.get("anchors") if isinstance(registry.get("anchors"), list) else []
        anchor_by_kind = registry.get("summary", {}).get("by_kind", {}) if isinstance(registry.get("summary"), dict) else {}
        headroom_sources = []
        if _truthy(adaptive.get("historical_headroom_active")):
            headroom_sources.append("historical_peak")
        raw_reserve_source = str(adaptive.get("headroom_reserve_source") or "").strip()
        if raw_reserve_source:
            for item in raw_reserve_source.replace(",", "+").split("+"):
                item = item.strip()
                if item and item not in headroom_sources:
                    headroom_sources.append(item)
        if _truthy(adaptive.get("headroom_reserve_active")) and not headroom_sources:
            headroom_sources.append("headroom_reserve")
        if _truthy(adaptive.get("adaptive_comfort_limited_by_headroom")):
            headroom_sources.append("comfort_limited")

        def contract(
            roadmap_item,
            name,
            owner,
            active,
            status,
            rule,
            *,
            signals=None,
            blockers=None,
            exports=None,
        ):
            return {
                "roadmap_item": int(roadmap_item),
                "name": name,
                "owner": owner,
                "active": bool(active),
                "status": status,
                "rule": rule,
                "signals": signals or {},
                "blockers": blockers or [],
                "exports": exports or [],
            }

        contracts = {
            "abregel_headroom_extreme_days": contract(
                3,
                "Abregel-Headroom bei Extremtagen",
                "adaptive_headroom",
                bool(
                    headroom_sources
                    or _sf(adaptive.get("curtailment_pressure_wh"), 0.0) > 0.0
                    or _sf(adaptive.get("adaptive_headroom_required_wh"), 0.0) > 0.0
                ),
                "guarded" if headroom_sources else "forecast_only",
                "Headroom uses explicit pressure and reserve sources; comfort floors may be limited by real headroom need.",
                signals={
                    "sources": headroom_sources,
                    "curtailment_pressure_wh": round(_sf(adaptive.get("curtailment_pressure_wh"), 0.0), 0),
                    "curtailment_unavoidable_wh": round(_sf(adaptive.get("curtailment_unavoidable_wh"), 0.0), 0),
                    "headroom_required_wh": round(_sf(adaptive.get("adaptive_headroom_required_wh"), 0.0), 0),
                    "headroom_available_wh": round(_sf(adaptive.get("adaptive_headroom_available_wh"), 0.0), 0),
                    "historical_safe_home_w": round(_sf(adaptive.get("historical_headroom_safe_home_w"), 0.0), 0),
                    "historical_temp_factor_max": round(_sf(adaptive.get("historical_headroom_temp_factor_max"), 1.0), 3),
                    "historical_min_temp_c": adaptive.get("historical_headroom_min_temp_c"),
                    "cloud_edge_forecast_ratio": adaptive.get("headroom_reserve_forecast_ratio"),
                },
                exports=[
                    "adaptive_headroom_required_wh",
                    "headroom_reserve_source",
                    "curtailment_pressure_wh",
                ],
            ),
            "manual_intervention_bounds": contract(
                4,
                "Manuelle Eingriffe",
                "manual_override",
                adaptive.get("manual_anchor_target_soc") is not None,
                "active" if _truthy(adaptive.get("manual_anchor_active")) else "bounded_or_rejected",
                "Manual anchors are forecast-checked and expire from the original manual timestamp.",
                signals={
                    "mode": adaptive.get("manual_anchor_mode", ""),
                    "target_soc": adaptive.get("manual_anchor_target_soc"),
                    "floor_soc": adaptive.get("manual_anchor_floor_soc"),
                    "start_ts": adaptive.get("manual_anchor_start_ts"),
                    "expires_ts": adaptive.get("manual_anchor_hold_until_ts"),
                    "reason": adaptive.get("manual_anchor_reason", ""),
                },
                blockers=[] if _truthy(adaptive.get("manual_anchor_active")) else [adaptive.get("manual_anchor_reason", "")],
                exports=["manual_anchor_active", "manual_anchor_reason", "anchor_registry"],
            ),
            "multi_wallbox_fairness": contract(
                5,
                "Multi-Wallbox-Fairness",
                "storage_manager_runtime",
                False,
                "runtime_contract_pending",
                "Runtime decision adds slot count, active slots, phase/minimum constraints and storage support source.",
                exports=["hardening_contracts.multi_wallbox_fairness", "wallbox_native.wb_details"],
            ),
            "diagnosis_visibility": contract(
                6,
                "Diagnose statt Rätselraten",
                "storage_plan",
                True,
                "visible",
                "Plan exports owners, priorities, sources, blockers and expiry rules for the hardened paths.",
                signals={
                    "anchor_registry_version": registry.get("version"),
                    "anchor_count": registry.get("summary", {}).get("total", len(anchor_items)),
                    "anchor_by_kind": anchor_by_kind,
                    "direct_marketing_blockers": direct_blocked,
                    "market_plan_action": (market.get("active_contract") or {}).get("action") if isinstance(market.get("active_contract"), dict) else None,
                    "market_plan_blockers": market_blocked,
                },
                exports=[
                    "anchor_registry",
                    "hardening_contracts",
                    "target_curve_meta",
                    "direct_marketing",
                    "market_plan",
                ],
            ),
            "direct_marketing_arbitrage_bounds": contract(
                7,
                "Direktvermarktung und Arbitrage",
                "direct_marketing",
                bool(direct.get("active") or direct_flags.get("commands_allowed") or cheap.get("active") or storm.get("active")),
                (
                    "commands_allowed"
                    if _truthy(direct_flags.get("commands_allowed"))
                    else "shadow_or_blocked"
                    if direct.get("active") or direct_blocked
                    else "off"
                ),
                "Direktvermarktung bleibt ein Storage-Manager-Vertrag; Arbitrage ist in 5.4 nicht freigegeben und bleibt wirkungslos.",
                signals={
                    "mode": direct.get("mode", "off"),
                    "shadow": direct.get("shadow"),
                    "commands_allowed": bool(direct_flags.get("commands_allowed")),
                    "owner_contract_version": direct.get("owner_contract_version"),
                    "plan_owner": direct.get("plan_owner"),
                    "controller_owner": direct.get("controller_owner"),
                    "profit_ok": bool(
                        direct_economics.get("profit_ok")
                        or direct_economics.get("grid_profit_ok")
                        or direct_economics.get("pv_shift_profit_ok")
                    ),
                    "cheap_grid_charge_active": bool(cheap.get("active")),
                    "storm_grid_charge_active": bool(storm.get("active")),
                    "market_plan_active": bool(market.get("active")),
                    "market_grid_profit_ok": bool(market_economics.get("grid_profit_ok")),
                },
                blockers=direct_blocked,
                exports=["direct_marketing", "market_plan", "cheap_grid_charge", "storm_grid_charge"],
            ),
        }
        return {
            "version": "hardening_contracts_v1",
            "scope": "roadmap_3_to_7",
            "contracts": contracts,
        }

    @staticmethod
    def _predump_curve_start_anchor_soc(
        dump_target_soc,
        timeline_slots,
        curve_start_ts,
        capacity_wh,
        now_ms,
        fallback_soc,
        morning_floor=0.0,
    ):
        """Return the Pre-Dump curve anchor at the real curve start, not at midnight."""
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        dump_target = max(0.0, min(100.0, _sf(dump_target_soc, fallback_soc)))
        start_ts = _sf(curve_start_ts, 0.0)
        fallback = max(0.0, min(100.0, _sf(fallback_soc, dump_target)))

        projected_start_soc = None
        for slot in sorted(timeline_slots or [], key=lambda s: _sf(s.get("ts", 0.0), 0.0)):
            if _sf(slot.get("ts", 0.0), 0.0) >= start_ts:
                projected_start_soc = _sf(slot.get("soc", fallback), fallback)
                break
        if projected_start_soc is None:
            projected_start_soc = fallback

        future_dump_wh = 0.0
        for slot in timeline_slots or []:
            slot_ts = _sf(slot.get("ts", 0.0), 0.0)
            if _sf(now_ms, 0.0) <= slot_ts < start_ts:
                future_dump_wh += max(0.0, _sf(slot.get("grid_dump_w", 0.0), 0.0)) * 0.25

        if _sf(capacity_wh, 0.0) > 0.0 and future_dump_wh > 0.0:
            projected_start_soc -= (future_dump_wh / _sf(capacity_wh, 1.0)) * 100.0

        projected_start_soc = max(0.0, min(100.0, projected_start_soc))
        anchor_soc = min(dump_target, projected_start_soc)
        if _sf(morning_floor, 0.0) > 0.0:
            anchor_soc = max(anchor_soc, _sf(morning_floor, 0.0))
        return max(0.0, min(100.0, anchor_soc)), projected_start_soc, future_dump_wh

    @staticmethod
    def _predump_dump_need_from_headroom(
        raw_pressure_wh,
        soc_at_first_pressure,
        target_soc,
        capacity_wh,
        regelbuffer_wh,
        max_dumpable_wh,
        min_need_wh=300.0,
    ):
        """Return the real Pre-Dump need after safe storage headroom is used.

        Raw pressure is only potential clipping. Existing headroom up to the
        day target can absorb that pressure without active pre-discharge.
        """
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        raw_pressure = max(0.0, _sf(raw_pressure_wh, 0.0))
        capacity = max(0.0, _sf(capacity_wh, 0.0))
        target = max(0.0, min(100.0, _sf(target_soc, 0.0)))
        pressure_soc = max(0.0, min(100.0, _sf(soc_at_first_pressure, target)))
        buffer_wh = max(0.0, _sf(regelbuffer_wh, 0.0))
        max_dumpable = max(0.0, _sf(max_dumpable_wh, 0.0))
        min_need = max(0.0, _sf(min_need_wh, 0.0))

        safe_headroom_wh = max(0.0, (target - pressure_soc) * capacity / 100.0)
        need_without_buffer_wh = max(0.0, raw_pressure - safe_headroom_wh)
        if need_without_buffer_wh < min_need:
            dump_target_wh = 0.0
        else:
            dump_target_wh = min(max_dumpable, need_without_buffer_wh + buffer_wh)
        return {
            "raw_pressure_wh": raw_pressure,
            "safe_headroom_wh": safe_headroom_wh,
            "need_without_buffer_wh": need_without_buffer_wh,
            "regelbuffer_wh": buffer_wh if need_without_buffer_wh >= min_need else 0.0,
            "dump_target_wh": dump_target_wh,
        }

    @staticmethod
    def _predump_adaptive_min_soc(
        predump_min_soc,
        target_soc,
        capacity_wh,
        raw_pressure_wh,
        regelbuffer_wh,
        comfort_enabled=True,
        comfort_soc=80.0,
        large_storage_threshold_kwh=25.0,
        min_need_wh=300.0,
    ):
        """Return the lowest normal Pre-Dump target for adaptive large storage."""
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        base_min = max(0.0, min(100.0, _sf(predump_min_soc, 0.0)))
        target = max(0.0, min(100.0, _sf(target_soc, 0.0)))
        capacity = max(0.0, _sf(capacity_wh, 0.0))
        if target <= 0.0 or capacity <= 0.0:
            return base_min

        threshold_kwh = max(1.0, _sf(large_storage_threshold_kwh, 25.0))
        if not comfort_enabled or (capacity / 1000.0) < threshold_kwh:
            return base_min

        floor_soc = max(0.0, min(target, _sf(comfort_soc, 80.0)))
        raw_pressure = max(0.0, _sf(raw_pressure_wh, 0.0))
        if raw_pressure >= max(0.0, _sf(min_need_wh, 300.0)):
            reserve_wh = raw_pressure + max(0.0, _sf(regelbuffer_wh, 0.0))
            floor_soc = min(floor_soc, target - (reserve_wh / capacity) * 100.0)

        return max(base_min, max(0.0, min(target, floor_soc)))

    def _configured_predump_adaptive_min_soc(self, predump_min_soc, raw_pressure_wh, regelbuffer_wh):
        return self._predump_adaptive_min_soc(
            predump_min_soc,
            self.target_soc,
            self.capacity_wh,
            raw_pressure_wh,
            regelbuffer_wh,
            comfort_enabled=self._cfg_bool(
                self.v4_config.get("storage_adaptive_comfort_enable"),
                True,
            ),
            comfort_soc=self._safe_float(
                self.v4_config.get("storage_adaptive_comfort_soc", 80.0),
                80.0,
            ),
            large_storage_threshold_kwh=self._safe_float(
                self.v4_config.get("storage_adaptive_large_storage_kwh", 25.0),
                25.0,
            ),
        )

    @staticmethod
    def _adaptive_headroom_band(
        target_timeline,
        day_slots,
        now_ms,
        current_soc,
        target_soc,
        capacity_wh,
        max_charge_w,
        export_limit_w,
        regelbuffer_wh,
        trust_wp_forecast_sink=False,
        min_home_w=300.0,
        min_need_wh=300.0,
        comfort_soc=0.0,
        large_storage_threshold_kwh=25.0,
        topology_contract=None,
        e3dc_dc_limit_w=0.0,
        e3dc_dc_limit_source="",
        pcc_limit_source="",
        pcc_limit_contract=None,
    ):
        """Build a floor/ceiling band from real preventable curtailment pressure.

        The ceiling is not derived from total PV. It only reacts to surplus that
        would exceed the export limit after safe consumers and that the battery
        could actually absorb within its charge-power limit.
        """
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        capacity = max(0.0, _sf(capacity_wh, 0.0))
        target = max(0.0, min(100.0, _sf(target_soc, 0.0)))
        now = _sf(now_ms, 0.0)
        current = max(0.0, min(100.0, _sf(current_soc, 0.0)))
        charge_limit = max(0.0, _sf(max_charge_w, 0.0))
        export_limit = _sf(export_limit_w, 0.0)
        buffer_wh = max(0.0, _sf(regelbuffer_wh, 0.0))
        min_need = max(0.0, _sf(min_need_wh, 0.0))
        safe_home_w = max(0.0, _sf(min_home_w, 300.0))
        storage_kwh = capacity / 1000.0 if capacity > 0.0 else 0.0
        large_threshold_kwh = max(1.0, _sf(large_storage_threshold_kwh, 25.0))
        comfort_target_soc = max(0.0, min(target, _sf(comfort_soc, 0.0)))
        storage_class = "large" if storage_kwh >= large_threshold_kwh else "small"
        topology = dict(topology_contract or {})
        topology_revision = topology.get("revision")
        dc_limit = max(0.0, _sf(e3dc_dc_limit_w, 0.0))
        typed_pcc_contract = dict(pcc_limit_contract or {})

        floor_points = []
        for point in target_timeline or []:
            try:
                floor_points.append({
                    "ts": int(_sf(point.get("ts", 0), 0)),
                    "soc": round(max(0.0, min(100.0, _sf(point.get("soc", target), target))), 2),
                })
            except Exception:
                continue
        floor_points.sort(key=lambda p: p["ts"])

        def _floor_at(ts):
            ts = _sf(ts, now)
            if not floor_points:
                return current
            if ts <= floor_points[0]["ts"]:
                return floor_points[0]["soc"]
            for idx in range(len(floor_points) - 1):
                left = floor_points[idx]
                right = floor_points[idx + 1]
                if left["ts"] <= ts <= right["ts"]:
                    dur = max(1.0, float(right["ts"]) - float(left["ts"]))
                    frac = max(0.0, min(1.0, (ts - float(left["ts"])) / dur))
                    return left["soc"] + (right["soc"] - left["soc"]) * frac
            return floor_points[-1]["soc"]

        def _safe_consumers_w(slot):
            # For curtailment headroom, only guaranteed baseline load is a safe
            # sink. ML/live home spikes can disappear and must not erase reserve.
            home_w = max(0.0, _sf(slot.get("safe_home_w", safe_home_w), safe_home_w))
            wp_w = _sf(slot.get("wp_w", 0.0), 0.0) if trust_wp_forecast_sink else 0.0
            climate_w = _sf(slot.get("climate_w", 0.0), 0.0) if trust_wp_forecast_sink else 0.0
            return home_w + wp_w + climate_w + _slot_planned_load_w(slot)

        def _slot_export_limit_contract(slot):
            topology_bound = (
                str(slot.get("pv_topology_status") or "") == "bound"
                and str(slot.get("pv_topology_revision") or "") == str(topology_revision or "")
            )
            if topology_bound and typed_pcc_contract.get("active") is True:
                base_active = True
                base_limit = max(0.0, _sf(typed_pcc_contract.get("limit_w"), 0.0))
                base_source = str(typed_pcc_contract.get("source") or "none")
                application = "typed_topology_buffered_pcc"
            else:
                base_active = export_limit > 0.0
                base_limit = max(0.0, export_limit)
                base_source = str(pcc_limit_source or "unavailable")
                application = "legacy_topology_unbound_unchanged"

            slot_key = None
            for candidate in ("hard_export_limit_w", "curtailment_limit_w"):
                if candidate in slot:
                    slot_key = candidate
                    break
            raw_slot_limit = _sf(slot.get(slot_key), 0.0) if slot_key else 0.0
            slot_explicit_active = (
                slot.get("hard_export_limit_active") is True
                or slot.get("curtailment_limit_active") is True
            )
            slot_active = bool(slot_key) and (raw_slot_limit > 0.0 or slot_explicit_active)
            if not slot_active:
                return {
                    "active": base_active,
                    "limit_w": base_limit if base_active else None,
                    "source": base_source,
                    "application": application,
                    "slot_limit_active": False,
                }

            slot_limit = max(0.0, raw_slot_limit)
            effective = min(base_limit, slot_limit) if base_active else slot_limit
            return {
                "active": True,
                "limit_w": effective,
                "source": f"{base_source}+slot:{slot_key}",
                "application": application,
                "slot_limit_active": True,
                "slot_limit_w": slot_limit,
                "slot_zero_explicit": slot_limit == 0.0 and slot_explicit_active,
            }

        pressure_samples = []
        curtailment_pressure_wh = 0.0
        dc_pressure_wh = 0.0
        pcc_pressure_wh = 0.0
        headroom_reserve_pressure_wh = 0.0
        headroom_reserve_slots = 0
        curtailment_unavoidable_wh = 0.0
        applied_pcc_contracts = {}
        pcc_candidate_active = bool(export_limit > 0.0 or typed_pcc_contract.get("active") is True)
        if (pcc_candidate_active or dc_limit > 0.0) and charge_limit > 0.0 and capacity > 0.0:
            for slot in day_slots or []:
                ts = _sf(slot.get("ts", 0.0), 0.0)
                if ts < now - 60000:
                    continue
                pv_w = _sf(slot.get("pv_w", 0.0), 0.0)
                slot_export_contract = _slot_export_limit_contract(slot)
                slot_export_limit = slot_export_contract.get("limit_w")
                contract_key = (
                    bool(slot_export_contract.get("active") is True),
                    slot_export_limit,
                    str(slot_export_contract.get("source") or "unavailable"),
                    str(slot_export_contract.get("application") or "unknown"),
                )
                applied_pcc_contracts[contract_key] = applied_pcc_contracts.get(contract_key, 0) + 1
                safe_consumers_w = _safe_consumers_w(slot)
                pressure = slot_headroom_pressure(
                    total_pv_w=pv_w,
                    e3dc_dc_pv_w=slot.get("e3dc_dc_pv_w"),
                    external_ac_pv_w=slot.get("external_ac_pv_w"),
                    topology_status=slot.get("pv_topology_status"),
                    topology_revision=slot.get("pv_topology_revision"),
                    expected_topology_revision=topology_revision,
                    e3dc_dc_limit_w=dc_limit,
                    pcc_limit_w=slot_export_limit,
                    pcc_limit_active=slot_export_contract.get("active") is True,
                    safe_consumers_w=safe_consumers_w,
                    charge_limit_w=charge_limit,
                    e3dc_dc_limit_source=e3dc_dc_limit_source,
                    pcc_limit_source=slot_export_contract.get("source", pcc_limit_source),
                )
                pressure_w = float(pressure.get("combined_pressure_w", 0.0) or 0.0)
                preventable_w = float(pressure.get("preventable_w", 0.0) or 0.0)
                unavoidable_w = float(pressure.get("unavoidable_w", 0.0) or 0.0)
                reserve_pv_w = max(
                    pv_w,
                    _sf(slot.get("pv_headroom_w", 0.0), 0.0),
                    _sf(slot.get("cloud_edge_pv_w", 0.0), 0.0),
                )
                reserve_dc_w = slot.get("e3dc_dc_pv_w")
                reserve_external_w = slot.get("external_ac_pv_w")
                if pv_w > 0.0 and reserve_pv_w > pv_w and reserve_dc_w is not None and reserve_external_w is not None:
                    _reserve_scale = reserve_pv_w / pv_w
                    reserve_dc_w = max(0.0, _sf(reserve_dc_w, 0.0)) * _reserve_scale
                    reserve_external_w = max(0.0, _sf(reserve_external_w, 0.0)) * _reserve_scale
                    _external_limit_w = max(
                        0.0,
                        _sf((topology.get("limits_w") or {}).get("external_ac_inverter"), 0.0),
                    )
                    if _external_limit_w > 0.0:
                        reserve_external_w = min(reserve_external_w, _external_limit_w)
                        reserve_dc_w = max(0.0, reserve_pv_w - reserve_external_w)
                reserve_pressure = slot_headroom_pressure(
                    total_pv_w=reserve_pv_w,
                    e3dc_dc_pv_w=reserve_dc_w,
                    external_ac_pv_w=reserve_external_w,
                    topology_status=slot.get("pv_topology_status"),
                    topology_revision=slot.get("pv_topology_revision"),
                    expected_topology_revision=topology_revision,
                    e3dc_dc_limit_w=dc_limit,
                    pcc_limit_w=slot_export_limit,
                    pcc_limit_active=slot_export_contract.get("active") is True,
                    safe_consumers_w=safe_consumers_w,
                    charge_limit_w=charge_limit,
                    e3dc_dc_limit_source=e3dc_dc_limit_source,
                    pcc_limit_source=slot_export_contract.get("source", pcc_limit_source),
                )
                reserve_pressure_w = float(reserve_pressure.get("combined_pressure_w", 0.0) or 0.0)
                reserve_preventable_w = float(reserve_pressure.get("preventable_w", 0.0) or 0.0)
                reserve_extra_w = max(0.0, reserve_preventable_w - preventable_w)
                total_preventable_w = max(preventable_w, reserve_preventable_w)
                StorageSimulator._retain_slot_headroom_evidence(
                    slot,
                    pressure,
                    reserve_pressure,
                )
                dc_pressure_wh += max(
                    float(pressure.get("dc_pressure_w", 0.0) or 0.0),
                    float(reserve_pressure.get("dc_pressure_w", 0.0) or 0.0),
                ) * 0.25
                pcc_pressure_wh += max(
                    float(pressure.get("pcc_pressure_w", 0.0) or 0.0),
                    float(reserve_pressure.get("pcc_pressure_w", 0.0) or 0.0),
                ) * 0.25
                if reserve_extra_w > 0.0:
                    headroom_reserve_slots += 1
                    headroom_reserve_pressure_wh += reserve_extra_w * 0.25
                if total_preventable_w > 0.0 or unavoidable_w > 0.0:
                    pressure_samples.append({
                        "ts": int(ts),
                        "preventable_w": total_preventable_w,
                        "curtailment_w": preventable_w,
                        "reserve_extra_w": reserve_extra_w,
                        "unavoidable_w": unavoidable_w,
                        "pcc_limit_contract": slot_export_contract,
                    })
                curtailment_pressure_wh += preventable_w * 0.25
                curtailment_unavoidable_wh += unavoidable_w * 0.25
        total_headroom_pressure_wh = curtailment_pressure_wh + headroom_reserve_pressure_wh
        if total_headroom_pressure_wh >= min_need and capacity > 0.0:
            headroom_target_soc = target - ((total_headroom_pressure_wh + buffer_wh) / capacity) * 100.0
            headroom_target_soc = max(0.0, min(target, headroom_target_soc))
        else:
            headroom_target_soc = target

        comfort_floor_soc = 0.0
        comfort_active = False
        comfort_limited_by_headroom = False
        comfort_raised_points = 0
        if storage_class == "large" and comfort_target_soc > 0.0 and floor_points and capacity > 0.0:
            comfort_floor_soc = comfort_target_soc
            if total_headroom_pressure_wh >= min_need:
                headroom_limited_soc = headroom_target_soc
                if headroom_limited_soc < comfort_floor_soc:
                    comfort_limited_by_headroom = True
                comfort_floor_soc = max(0.0, min(comfort_floor_soc, headroom_limited_soc))
            for point in floor_points:
                old_soc = float(point.get("soc", 0.0) or 0.0)
                new_soc = max(old_soc, comfort_floor_soc)
                if new_soc > old_soc + 0.05:
                    point["soc"] = round(min(target, new_soc), 2)
                    comfort_raised_points += 1
            comfort_active = comfort_raised_points > 0

        first_pressure_ts = 0
        soc_at_first_pressure = current
        for sample in pressure_samples:
            if sample.get("preventable_w", 0.0) > 0.0:
                first_pressure_ts = int(sample["ts"])
                soc_at_first_pressure = max(current, _floor_at(first_pressure_ts))
                break

        available_headroom_wh = max(0.0, (target - soc_at_first_pressure) * capacity / 100.0)
        need_without_buffer_wh = max(0.0, total_headroom_pressure_wh - available_headroom_wh)
        if need_without_buffer_wh < min_need:
            required_headroom_wh = 0.0
            applied_buffer_wh = 0.0
        else:
            required_headroom_wh = need_without_buffer_wh + buffer_wh
            applied_buffer_wh = buffer_wh

        pressure_by_ts = [(float(s["ts"]), float(s.get("preventable_w", 0.0)) * 0.25) for s in pressure_samples]
        reserve_pressure_by_ts = [
            (float(s["ts"]), float(s.get("reserve_extra_w", 0.0)) * 0.25)
            for s in pressure_samples
        ]
        reserve_floor_protected = False
        reserve_floor_protected_points = 0
        reserve_floor_protected_max_delta = 0.0
        if headroom_reserve_pressure_wh >= min_need and capacity > 0.0:
            for point in floor_points:
                future_reserve_wh = sum(wh for ts, wh in reserve_pressure_by_ts if ts >= float(point["ts"]))
                if future_reserve_wh < min_need:
                    continue
                future_pressure_wh = sum(wh for ts, wh in pressure_by_ts if ts >= float(point["ts"]))
                floor_soc = max(0.0, min(target, _sf(point.get("soc", target), target)))
                available_from_floor_wh = max(0.0, (target - floor_soc) * capacity / 100.0)
                future_need_wh = max(0.0, future_pressure_wh - available_from_floor_wh)
                future_required_wh = future_need_wh + buffer_wh if future_need_wh >= min_need else 0.0
                if future_required_wh <= 0.0:
                    future_required_wh = min(future_pressure_wh + buffer_wh, target * capacity / 100.0)
                reserve_floor_soc = target - (future_required_wh / capacity) * 100.0
                new_floor_soc = max(0.0, min(floor_soc, max(current, reserve_floor_soc)))
                if new_floor_soc < floor_soc - 0.05:
                    reserve_floor_protected = True
                    reserve_floor_protected_points += 1
                    reserve_floor_protected_max_delta = max(
                        reserve_floor_protected_max_delta,
                        floor_soc - new_floor_soc,
                    )

        adaptive_soc_floor = max(0.0, min(target, _floor_at(now)))
        if required_headroom_wh > 0.0 and capacity > 0.0:
            adaptive_soc_ceiling_raw = target - (required_headroom_wh / capacity) * 100.0
        else:
            adaptive_soc_ceiling_raw = target
        adaptive_headroom_floor_conflict = adaptive_soc_ceiling_raw < adaptive_soc_floor - 0.05
        adaptive_soc_ceiling = max(adaptive_soc_floor, min(target, adaptive_soc_ceiling_raw))

        ceiling_curve = []
        ceiling_conflict_points = 0
        ceiling_conflict_max_delta = 0.0
        for point in floor_points:
            floor_soc = max(0.0, min(target, _sf(point.get("soc", adaptive_soc_floor), adaptive_soc_floor)))
            # pressure_by_ts already contains max(curtailment, reserve).
            future_pressure_wh = sum(wh for ts, wh in pressure_by_ts if ts >= float(point["ts"]))
            available_from_floor_wh = max(0.0, (target - floor_soc) * capacity / 100.0)
            future_need_wh = max(0.0, future_pressure_wh - available_from_floor_wh)
            future_required_wh = future_need_wh + buffer_wh if future_need_wh >= min_need else 0.0
            if future_required_wh > 0.0 and capacity > 0.0:
                ceiling_soc_raw = target - (future_required_wh / capacity) * 100.0
            else:
                ceiling_soc_raw = target
            if ceiling_soc_raw < floor_soc - 0.05:
                ceiling_conflict_points += 1
                ceiling_conflict_max_delta = max(ceiling_conflict_max_delta, floor_soc - ceiling_soc_raw)
            ceiling_curve.append({
                "ts": int(point["ts"]),
                "soc": round(max(floor_soc, min(target, ceiling_soc_raw)), 2),
            })

        future_charge_slots = []
        last_floor_ts = floor_points[-1]["ts"] if floor_points else 0
        for slot in day_slots or []:
            ts = _sf(slot.get("ts", 0.0), 0.0)
            if ts < now - 60000:
                continue
            if last_floor_ts and ts > last_floor_ts:
                continue
            raw_surplus_w = (
                _sf(slot.get("pv_w", 0.0), 0.0)
                - _sf(slot.get("home_w", 0.0), 0.0)
                - _sf(slot.get("wp_w", 0.0), 0.0)
                - _sf(slot.get("climate_w", 0.0), 0.0)
                - _slot_planned_load_w(slot)
            )
            charge_w = min(charge_limit, max(0.0, raw_surplus_w))
            if charge_w > 0.0:
                future_charge_slots.append((int(ts), charge_w * 0.25))

        remaining_target_wh = max(0.0, (target - current) * capacity / 100.0)
        realistic_charge_wh = sum(wh for _, wh in future_charge_slots)
        target_need_with_margin_wh = remaining_target_wh * 1.05
        evening_shortfall_wh = max(0.0, target_need_with_margin_wh - realistic_charge_wh)
        latest_charge_start_ts = 0
        if remaining_target_wh > 0.0:
            if evening_shortfall_wh > 0.0:
                latest_charge_start_ts = int(now)
            else:
                acc_wh = 0.0
                for ts, wh in reversed(future_charge_slots):
                    acc_wh += wh
                    latest_charge_start_ts = int(ts)
                    if acc_wh >= target_need_with_margin_wh:
                        break

        if len(applied_pcc_contracts) == 1:
            (active, limit_w, source, application), samples = next(iter(applied_pcc_contracts.items()))
            applied_pcc_contract = {
                "schema_version": "pcc_headroom_limit_application_v1",
                "active": active,
                "limit_w": limit_w if active else None,
                "source": source,
                "application": application,
                "samples": samples,
                "typed_candidate": typed_pcc_contract,
            }
        else:
            applied_pcc_contract = {
                "schema_version": "pcc_headroom_limit_application_v1",
                "active": None,
                "limit_w": None,
                "source": "per_slot_mixed",
                "application": "per_slot_typed_or_legacy",
                "samples": sum(applied_pcc_contracts.values()),
                "variants": [
                    {
                        "active": key[0],
                        "limit_w": key[1] if key[0] else None,
                        "source": key[2],
                        "application": key[3],
                        "samples": count,
                    }
                    for key, count in sorted(
                        applied_pcc_contracts.items(),
                        key=lambda item: (item[0][2], str(item[0][1])),
                    )
                ],
                "typed_candidate": typed_pcc_contract,
            }

        return {
            "adaptive_headroom_required_wh": round(required_headroom_wh, 0),
            "adaptive_headroom_available_wh": round(available_headroom_wh, 0),
            "adaptive_headroom_need_without_buffer_wh": round(need_without_buffer_wh, 0),
            "adaptive_headroom_buffer_wh": round(applied_buffer_wh, 0),
            "adaptive_headroom_target_soc": round(headroom_target_soc, 2),
            "adaptive_soc_ceiling": round(adaptive_soc_ceiling, 2),
            "adaptive_soc_floor": round(adaptive_soc_floor, 2),
            "headroom_reserve_pressure_wh": round(headroom_reserve_pressure_wh, 0),
            "headroom_reserve_slots": int(headroom_reserve_slots),
            "headroom_reserve_active": bool(headroom_reserve_pressure_wh >= min_need),
            "headroom_reserve_floor_lowered": False,
            "headroom_reserve_floor_protected": bool(reserve_floor_protected),
            "headroom_reserve_floor_protected_points": int(reserve_floor_protected_points),
            "headroom_reserve_floor_protected_max_delta_pct": round(reserve_floor_protected_max_delta, 2),
            "headroom_floor_policy": "published_floor_no_downshift_v1",
            "adaptive_soc_ceiling_raw": round(max(0.0, min(target, adaptive_soc_ceiling_raw)), 2),
            "adaptive_headroom_floor_conflict": bool(adaptive_headroom_floor_conflict or ceiling_conflict_points > 0),
            "adaptive_headroom_floor_conflict_points": int(ceiling_conflict_points),
            "adaptive_headroom_floor_conflict_max_delta_pct": round(
                max(0.0, ceiling_conflict_max_delta, adaptive_soc_floor - adaptive_soc_ceiling_raw),
                2,
            ),
            "adaptive_storage_class": storage_class,
            "adaptive_storage_kwh": round(storage_kwh, 2),
            "adaptive_large_storage_threshold_kwh": round(large_threshold_kwh, 2),
            "adaptive_comfort_soc": round(comfort_target_soc, 2) if comfort_target_soc > 0.0 else None,
            "adaptive_comfort_floor_soc": round(comfort_floor_soc, 2) if comfort_active else None,
            "adaptive_comfort_active": bool(comfort_active),
            "adaptive_comfort_limited_by_headroom": bool(comfort_limited_by_headroom),
            "curtailment_pressure_wh": round(curtailment_pressure_wh, 0),
            "combined_headroom_pressure_wh": round(total_headroom_pressure_wh, 0),
            "dc_headroom_pressure_wh": round(dc_pressure_wh, 0),
            "pcc_headroom_pressure_wh": round(pcc_pressure_wh, 0),
            "headroom_combination_rule": "max_dc_pcc_no_double_count",
            "pv_topology_status": topology.get("status", "topology_unbound"),
            "pv_topology_reason": topology.get("reason", "TOPOLOGY_UNAVAILABLE"),
            "pv_topology_revision": topology_revision,
            "e3dc_dc_limit_w": round(dc_limit, 0) if dc_limit > 0.0 else None,
            "e3dc_dc_limit_source": str(e3dc_dc_limit_source or "unavailable"),
            "pcc_limit_w": applied_pcc_contract.get("limit_w"),
            "pcc_limit_source": str(applied_pcc_contract.get("source") or "unavailable"),
            "pcc_limit_contract": applied_pcc_contract,
            "curtailment_unavoidable_wh": round(curtailment_unavoidable_wh, 0),
            "curtailment_first_pressure_ts": int(first_pressure_ts),
            "curtailment_soc_at_first_pressure": round(soc_at_first_pressure, 2),
            "latest_charge_start_ts": int(latest_charge_start_ts),
            "evening_shortfall_wh": round(evening_shortfall_wh, 0),
            "soc_min_curve": floor_points,
            "soc_ceiling_curve": ceiling_curve,
        }

    @staticmethod
    def _pace_predump_slots(slots, dump_wh_target, max_dump_w, min_dump_w=300.0):
        """Distribute Pre-Dump energy over the remaining window instead of front-loading it."""
        def _sf(value, default=0.0):
            try:
                return float(value)
            except Exception:
                return float(default)

        paced_slots = sorted(slots or [], key=lambda s: _sf(s.get("ts", 0.0), 0.0))
        remaining_wh = max(0.0, _sf(dump_wh_target, 0.0))
        max_w = max(0.0, _sf(max_dump_w, 0.0))
        min_w = max(0.0, _sf(min_dump_w, 0.0))
        dumped_wh = 0.0
        if not paced_slots or remaining_wh <= 0.0 or max_w <= 0.0:
            return 0.0, []

        for idx, slot in enumerate(paced_slots):
            if remaining_wh <= 0.0:
                break
            slots_left = max(1, len(paced_slots) - idx)
            pace_w = remaining_wh / max(0.25, slots_left * 0.25)
            actual_w = min(max_w, pace_w)
            if 0.0 < actual_w < min_w and remaining_wh > min_w * 0.25:
                actual_w = min(min_w, max_w)
            actual_wh = min(remaining_wh, actual_w * 0.25)
            if actual_wh <= 0.0:
                continue
            slot_w = actual_wh / 0.25
            slot["grid_dump_w"] = max(_sf(slot.get("grid_dump_w", 0.0), 0.0), slot_w)
            slot["predump_candidate_w"] = slot["grid_dump_w"]
            slot["predump_candidate"] = True
            slot["predump_selected"] = False
            slot["predump_executable"] = False
            slot["predump_commands_allowed"] = False
            slot["predump_status"] = "candidate_only"
            slot["predump_block_reason"] = "awaiting_storage_manager_selection"
            dumped_wh += actual_wh
            remaining_wh -= actual_wh

        positive_slots = [
            slot for slot in paced_slots
            if _sf(slot.get("grid_dump_w", 0.0), 0.0) > 0.0
        ]
        return dumped_wh, positive_slots

    @staticmethod
    def _future_intermediate_anchor_config_changed(anchor, configured_soc, configured_hour, now_ms, today_0_ms):
        """True if a not-yet-reached user anchor differs from current config."""
        try:
            existing_soc = float(anchor.get("configured_soc", anchor.get("soc", 0.0)) or 0.0)
            existing_hour = float(anchor.get("hour", 0.0) or 0.0)
            existing_ts = float(anchor.get("ts", 0.0) or 0.0)
            if existing_ts <= 0.0:
                existing_ts = float(today_0_ms) + existing_hour * 3600000.0
            configured_soc = float(configured_soc or 0.0)
            configured_hour = float(configured_hour if configured_hour is not None else existing_hour)
        except Exception:
            return False
        if existing_ts <= float(now_ms) + 60000.0:
            return False
        if configured_soc <= 0.0:
            return True
        return abs(configured_soc - existing_soc) > 0.5 or abs(configured_hour - existing_hour) > 0.02

    def _observe_wallbox_storage_policy_requested(self, intent, mode):
        if mode != MODE_OFF:
            return False
        reason = str((intent or {}).get("reason") or "").strip().lower()
        request = str((intent or {}).get("battery_request") or "").strip().lower()
        if reason in ("no_vehicle_connected", "manual_pause", "bev_full_blocked") or request == "release":
            return False
        if not bool(
            (intent or {}).get("active")
            or (intent or {}).get("car_active")
            or (intent or {}).get("charging_active")
            or (intent or {}).get("connected")
            or (intent or {}).get("plugged")
            or abs(self._safe_float((intent or {}).get("wb_power_w"), 0.0)) > 500.0
        ):
            return False
        raw_policy = (intent or {}).get("observe_storage_policy")
        if raw_policy is None:
            wb_id = int(self._safe_float((intent or {}).get("wb_id", (intent or {}).get("charger_id", 1)), 1))
            for key in (
                f"wb{wb_id}_observe_storage_policy",
                f"wb{wb_id}_storage_observe_policy",
                "wb_observe_storage_policy",
            ):
                if key in self.v4_config:
                    raw_policy = self.v4_config.get(key)
                    break
        if raw_policy is None:
            for wb_id in (1, 2):
                if normalize_wb_mode(self.v4_config.get(f"wb{wb_id}_mode", MODE_OFF)) != MODE_OFF:
                    continue
                for key in (f"wb{wb_id}_observe_storage_policy", f"wb{wb_id}_storage_observe_policy"):
                    if key in self.v4_config:
                        raw_policy = self.v4_config.get(key)
                        break
                if raw_policy is not None:
                    break
        return str(raw_policy or "").strip().lower() in (
            "reserve",
            "pv_battery",
            "battery",
            "akku",
            "wbminsoc",
            "floor",
            "1",
            "true",
            "yes",
            "on",
        )

    def _wallbox_target_curve_soc(self, default_target_soc):
        """Liest den aktiven wbminSoC-Boden fuer die Wallbox, ohne das Speicherziel zu aendern."""
        try:
            if not os.path.exists(WB_INTENT_FILE):
                return None, {}
            age_s = time.time() - os.path.getmtime(WB_INTENT_FILE)
            if age_s > 60:
                return None, {"stale_s": round(age_s, 1)}
            with open(WB_INTENT_FILE, "r", encoding="utf-8") as f:
                intent = json.load(f)
            mode = normalize_wb_mode(intent.get("wb_mode_active", 0))
            observe_storage_floor = self._observe_wallbox_storage_policy_requested(intent, mode)
            if mode != MODE_TARGET and not observe_storage_floor:
                return None, intent
            if not bool(intent.get("active", intent.get("car_active", False))):
                return None, intent
            external_owner = bool(
                intent.get("external_wallbox_manager")
                or intent.get("openwb_primary_observe_only")
                or intent.get("autonomous_wallbox")
                or observe_storage_floor
            )
            if not external_owner:
                return None, intent
            raw_floor = intent.get("effective_wb_floor_soc", intent.get("wbminsoc", self.v4_config.get("wbminsoc", default_target_soc)))
            target = max(5.0, min(100.0, self._safe_float(raw_floor, default_target_soc)))
            return target, intent
        except Exception as exc:
            return None, {"error": str(exc)}

    def __init__(self):
        self.v4_config = self._load_v4_config()
        self.last_plan_valid_until_ts_ms = 0

        # Batteriegroesse aus V4 Config (Fallback wenn RSCP keinen Wert liefert)
        self.capacity_kwh = self._safe_float(self.v4_config.get('speichergroesse', '10.0'), 10.0)
        self.capacity_wh = self.capacity_kwh * 1000.0

        # Max Lade-Grenzleistung
        self.max_charge_w = self._safe_float(self.v4_config.get('maximumladeleistung', '3000'), 3000.0)
        self.max_discharge_w = self.max_charge_w

        # Ziel-SoC (aus Config, default 90%)
        self.target_soc = self._safe_float(self.v4_config.get('storage_target_soc', 90.0), 90.0)
        self.curve_target_mode = self._curve_target_mode_from_config(self.v4_config)
        if self.curve_target_mode == "forecast_100":
            self.target_soc = 100.0

        # Einspeise-Limit
        self.export_limit_w = self._safe_float(self.v4_config.get('einspeiselimit', 0.0), 0.0)
        if self.export_limit_w < 100.0 and self.export_limit_w > 0:
            self.export_limit_w *= 1000.0
        self.export_limit_source = "config:einspeiselimit" if self.export_limit_w > 0.0 else "unavailable"
        self.pv_topology_contract = build_pv_forecast_topology(self.v4_config)
        self._refresh_pcc_headroom_limit_contract()

        # Optionale Zwischenanker fuer Auto-/Wallbox-Tage:
        # Danach steigt die Kurve nur noch bis zum Tagesziel/Freilauf weiter.
        self.mid_target_soc = self._safe_float(self.v4_config.get('storage_mid_target_soc', 0.0), 0.0)
        self.mid_hour       = self._safe_float(self.v4_config.get('storage_mid_hour', 11.0), 11.0)
        self.noon_target_soc  = self._safe_float(self.v4_config.get('storage_noon_target_soc', 0.0), 0.0)
        self.noon_hour        = self._safe_float(self.v4_config.get('storage_noon_hour', 14.0), 14.0)

        # Morgendlicher Mindest-SoC: Untergrenze fuer den Kurvenstart.
        # Das ist kein Entladeziel und kein Deckel: liegt die Prognose morgens
        # ueber diesem Wert, startet die Kurve dort. Nur ein zu niedriger
        # Prognoseanker wird auf den Morgenpuffer angehoben.
        self.morning_soc = self._safe_float(self.v4_config.get('storage_morning_soc', 20.0), 20.0)
        self.morning_hour = self._safe_float(self.v4_config.get('storage_morning_hour', 9.0), 9.0)
        self.predump_min_soc = self._safe_float(
            self.v4_config.get('storage_predump_min_soc',
                               self.v4_config.get('eco_dump_min_soc',
                                                  self.v4_config.get('ep_reserve_pct', 8.0))),
            8.0
        )
        self.predump_enabled = self._cfg_bool(self.v4_config.get('predump_enable'), True)
        self.hard_predump_enabled = self._cfg_bool(self.v4_config.get('hard_predump_enable'), False)
        self.hard_predump_target_soc = max(
            0.0,
            min(100.0, self._safe_float(self.v4_config.get('hard_predump_target_soc', self.predump_min_soc), self.predump_min_soc)),
        )
        self.storm_guard_mode = self._storm_guard_mode_from_config()
        self.storm_guard_min_level = int(max(1, min(4, self._safe_float(self.v4_config.get('storm_guard_min_level', 2), 2))))
        self.storm_guard_grid_enable = self._cfg_bool(self.v4_config.get('storm_guard_grid_enable'), False)
        self.storm_guard_grid_min_level = int(max(1, min(4, self._safe_float(self.v4_config.get('storm_guard_grid_min_level', 3), 3))))
        self.storm_guard_grid_morning_soc = max(0.0, min(100.0, self._safe_float(self.v4_config.get('storm_guard_grid_morning_soc', 20), 20.0)))
        self.storm_guard_lead_min = max(15.0, min(240.0, self._safe_float(self.v4_config.get('storm_guard_precharge_lead_min', 60), 60.0)))
        self.storm_guard_min_precharge_kwh = max(0.0, self._safe_float(self.v4_config.get('storm_guard_min_precharge_kwh', 0.3), 0.3))
        self.storm_guard_max_soc = max(50.0, min(100.0, self._safe_float(self.v4_config.get('storm_guard_max_soc', 95), 95.0)))

    def _update_params_from_live(self):
        """Aktualisiert Hardware-Parameter dynamisch aus dem RSCP Live-Stream.
        WICHTIG: Config-Werte (speichergroesse, maximumladeleistung) haben VORRANG!
        Live-Werte werden nur als Fallback verwendet wenn Config-Wert = 0/fehlt.
        """
        if not os.path.exists(LIVE_DATA_FILE): return
        try:
            # Config jedes Mal neu laden damit Aenderungen sofort wirken
            self.v4_config = self._load_v4_config()
            self.pv_topology_contract = build_pv_forecast_topology(self.v4_config)
            with open(LIVE_DATA_FILE, 'r') as f:
                d = json.load(f)

            # 1. Nutzbare Kapazitaet: Config hat IMMER Vorrang (User weiss besser als RSCP).
            #    RSCP-Fallback NUR wenn Config komplett fehlt (0 oder leer).
            #    WICHTIG: RSCP-Wert darf Config-Wert NICHT ueberschreiben!
            #    (Bug-Ursache: cfg_cap=35kWh, RSCP=34.21kWh -> bat_cap im Plan schwankte)
            cfg_cap = self._safe_float(self.v4_config.get('speichergroesse', 0), 0.0)
            if cfg_cap > 0:
                # Config gesetzt -> capacity_kwh/capacity_wh immer auf Config-Wert halten
                # (verhindert, dass __init__-Wert durch spateres RSCP-Read ueberschrieben wird)
                self.capacity_kwh = cfg_cap
                self.capacity_wh  = cfg_cap * 1000.0
            elif "bat_total_usable_kwh" in d or "bat_usable_kwh" in d:
                # Kein Config-Wert -> RSCP-Fallback (Neuinstallation ohne speichergroesse)
                self.capacity_kwh = float(d.get("bat_total_usable_kwh", d.get("bat_usable_kwh")))
                self.capacity_wh  = self.capacity_kwh * 1000.0

            # 2. Max Ladeleistung: Config hat Vorrang
            cfg_charge = self._safe_float(self.v4_config.get('maximumladeleistung', 0), 0.0)
            if cfg_charge > 0:
                # Config konfiguriert -> unveraendert lassen
                pass
            elif "bat_charge_limit_w" in d:
                # Kein Config-Wert -> BMS-Fallback
                self.max_charge_w = float(d["bat_charge_limit_w"])

            # 3. Einspeiselimit (Abregelung) - kein eigener Config-Key, RSCP-Wert OK
            _live_derate_limit_w = None
            if "derate_at_power_w" in d and float(d["derate_at_power_w"]) > 0:
                _live_derate_limit_w = float(d["derate_at_power_w"])
                self.export_limit_w = _live_derate_limit_w
                self.export_limit_source = "live:derate_at_power_w"
            else:
                _cfg_export_limit_w = self._safe_float(self.v4_config.get('einspeiselimit', 0.0), 0.0)
                if 0.0 < _cfg_export_limit_w < 100.0:
                    _cfg_export_limit_w *= 1000.0
                if _cfg_export_limit_w > 0.0:
                    self.export_limit_w = _cfg_export_limit_w
                    self.export_limit_source = "config:einspeiselimit"
            self._refresh_pcc_headroom_limit_contract(_live_derate_limit_w)

            self.target_soc = self._safe_float(self.v4_config.get('storage_target_soc', self.target_soc), self.target_soc)
            self.curve_target_mode = self._curve_target_mode_from_config(self.v4_config)
            if self.curve_target_mode == "forecast_100":
                self.target_soc = 100.0
            # Wichtig fuer den Rueckweg nach Forecast-Ausfall: Ein im Notkurven-
            # Pfad injiziertes Notkurven-Zwischenziel darf im laufenden Service-Prozess
            # nicht als Pseudo-Config haengen bleiben, sobald echte Prognosen
            # wieder verfuegbar sind.
            self.noon_target_soc = self._safe_float(self.v4_config.get('storage_noon_target_soc', 0.0), 0.0)
            self.noon_hour = self._safe_float(self.v4_config.get('storage_noon_hour', 14.0), 14.0)
            self.mid_target_soc = self._safe_float(self.v4_config.get('storage_mid_target_soc', 0.0), 0.0)
            self.mid_hour = self._safe_float(self.v4_config.get('storage_mid_hour', 11.0), 11.0)
            self.morning_soc = self._safe_float(self.v4_config.get('storage_morning_soc', self.morning_soc), self.morning_soc)
            self.morning_hour = self._safe_float(self.v4_config.get('storage_morning_hour', self.morning_hour), self.morning_hour)
            self.predump_min_soc = self._safe_float(
                self.v4_config.get('storage_predump_min_soc',
                                   self.v4_config.get('eco_dump_min_soc', self.predump_min_soc)),
                self.predump_min_soc
            )
            self.predump_enabled = self._cfg_bool(self.v4_config.get('predump_enable'), True)
            self.hard_predump_enabled = self._cfg_bool(self.v4_config.get('hard_predump_enable'), False)
            self.hard_predump_target_soc = max(
                0.0,
                min(100.0, self._safe_float(self.v4_config.get('hard_predump_target_soc', self.predump_min_soc), self.predump_min_soc)),
            )
            self.storm_guard_mode = self._storm_guard_mode_from_config()
            self.storm_guard_min_level = int(max(1, min(4, self._safe_float(self.v4_config.get('storm_guard_min_level', 2), 2))))
            self.storm_guard_grid_enable = self._cfg_bool(self.v4_config.get('storm_guard_grid_enable'), False)
            self.storm_guard_grid_min_level = int(max(1, min(4, self._safe_float(self.v4_config.get('storm_guard_grid_min_level', 3), 3))))
            self.storm_guard_grid_morning_soc = max(0.0, min(100.0, self._safe_float(self.v4_config.get('storm_guard_grid_morning_soc', 20), 20.0)))
            self.storm_guard_lead_min = max(15.0, min(240.0, self._safe_float(self.v4_config.get('storm_guard_precharge_lead_min', 60), 60.0)))
            self.storm_guard_min_precharge_kwh = max(0.0, self._safe_float(self.v4_config.get('storm_guard_min_precharge_kwh', 0.3), 0.3))
            self.storm_guard_max_soc = max(50.0, min(100.0, self._safe_float(self.v4_config.get('storm_guard_max_soc', 95), 95.0)))

        except: pass


    def _load_v4_config(self):
        if os.path.exists(V4_CONFIG_FILE):
            try:
                with open(V4_CONFIG_FILE, 'r', encoding='utf-8') as f:
                    raw = json.load(f)
                if isinstance(raw.get("config"), dict):
                    core_keys = (
                        "server_ip", "e3dc_user", "speichergroesse",
                        "storage_target_soc", "storage_morning_soc",
                    )
                    if any(k in raw for k in core_keys):
                        nested = raw.get("config", {})
                        merged = dict(nested)
                        merged.update(raw)
                        raw = merged
                    else:
                        raw = raw["config"]
                return raw
            except: pass
        return {}

    def get_live_soc(self):
        try:
            if os.path.exists(LIVE_DATA_FILE):
                with open(LIVE_DATA_FILE, 'r', encoding='utf-8-sig') as f:
                    data = json.load(f)
                    # Support für live_data_py.json (Native V4)
                    if "SOC" in data: return float(data["SOC"])
                    if "batterie_soc" in data: return float(data["batterie_soc"])

                    for k, v in data.items():
                        if str(k).lower() in ["soc", "batterie_soc", "bat_soc"]:
                            return float(str(v).replace(',', '.'))
        except Exception as e:
            logger.error(f"Fehler beim Lesen des Live-SoC: {e}")
        logger.warning("Konnte keinen gueltigen Live-SoC finden. Erzeuge keinen neuen Speicherplan.")
        return None

    def _storm_guard_mode_from_config(self):
        raw = str(self.v4_config.get('storm_guard_mode', 'warn') or 'warn').strip().lower()
        aliases = {
            "0": "off", "aus": "off", "off": "off", "false": "off", "nein": "off",
            "1": "warn", "warn": "warn", "warning": "warn", "warnung": "warn", "nur_warnung": "warn",
            "2": "control", "on": "control", "ein": "control", "regelung": "control",
            "regeleingriff": "control", "control": "control",
        }
        return aliases.get(raw, "warn")

    def _storm_empty(self, reason=None):
        return {
            "mode": getattr(self, "storm_guard_mode", "warn"),
            "active": False,
            "warning_only": getattr(self, "storm_guard_mode", "warn") == "warn",
            "control_active": False,
            "grid_allowed": False,
            "level": 0,
            "reason": reason or "Keine Unwetterwarnung für die Speicherregelung",
            "action_label": "Keine aktive Wetterwirkung",
            "control_summary": "Keine Wetterwarnung für die Ladekurve aktiv.",
        }

    def _storm_grid_empty(self, current_soc, reason=None):
        return {
            "active": False,
            "reason": reason or "Unwetter-Netzladen nicht aktiv",
            "target_soc": round(float(current_soc), 1),
            "charge_w": 0,
            "hysteresis_pct": 0.5,
            "window_start": 0,
            "window_end": 0,
        }

    def _parse_alert_ts(self, value):
        try:
            if value in (None, "", 0):
                return None
            if isinstance(value, (int, float)):
                return float(value) * 1000.0 if float(value) < 10_000_000_000 else float(value)
            text = str(value).strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text).timestamp() * 1000.0
            except Exception:
                return float(text) * 1000.0 if float(text) < 10_000_000_000 else float(text)
        except Exception:
            return None

    def _read_weather_alerts(self):
        payload, metadata = _stable_json_object(WEATHER_ALERTS_FILE)
        if not isinstance(payload, dict) or not isinstance(metadata, dict):
            logger.warning(
                "Unwetterwächter: weather_alerts.json nicht stabil oder nicht lesbar"
            )
            return None
        payload["_age_s"] = max(
            0.0,
            time.time() - float(metadata.get("mtime") or 0.0),
        )
        return payload

    def _storm_guard_event(self, now_ms):
        mode = self._storm_guard_mode_from_config()
        self.storm_guard_mode = mode
        if mode == "off":
            return self._storm_empty("Unwetterwächter deaktiviert")

        payload = self._read_weather_alerts()
        if not payload:
            return self._storm_empty("Keine Wetterwarnungsdaten vorhanden")
        if float(payload.get("_age_s", 0.0)) > 6 * 3600:
            return self._storm_empty("Wetterwarnungen sind veraltet")

        horizon_ms = now_ms + 36 * 3600 * 1000
        candidates = []

        def _add_candidate(source, level, start_ms, end_ms, title, reason):
            try:
                level_i = int(level or 0)
                if level_i <= 0 or start_ms is None:
                    return
                start = float(start_ms)
                end = float(end_ms) if end_ms is not None else start + 90 * 60000
                if end <= now_ms - 5 * 60000 or start > horizon_ms:
                    return
                if end <= start:
                    end = start + 90 * 60000
                candidates.append({
                    "source": source,
                    "level": level_i,
                    "start_ts": int(start),
                    "end_ts": int(end),
                    "title": title or "Wetterwarnung",
                    "reason": reason or title or "Wetterwarnung",
                })
            except Exception:
                return

        for alert in payload.get("alerts") or []:
            if not isinstance(alert, dict):
                continue
            text = " ".join(str(alert.get(k, "") or "") for k in ("event", "headline", "description")).upper()
            level_raw = alert.get("level", payload.get("highest_level", 0))
            try:
                level_i = int(level_raw or 0)
            except Exception:
                level_i = 0
            thunder = bool(alert.get("thunderstorm")) or "GEWITTER" in text or "THUNDER" in text
            critical_weather = level_i >= 4
            winter_weather = level_i >= 3 and any(token in text for token in (
                "SCHNEE", "SCHNEEVERWEH", "GLATTEIS", "GLÄTTE", "GLAETTE",
                "EISREGEN", "EISBRUCH"
            ))
            if not thunder and not critical_weather and not winter_weather:
                continue
            _add_candidate(
                "dwd_cap",
                level_i,
                self._parse_alert_ts(alert.get("start_ts", alert.get("start"))),
                self._parse_alert_ts(alert.get("end_ts", alert.get("end"))),
                alert.get("event") or alert.get("headline"),
                alert.get("headline") or alert.get("description"),
            )

        risk = payload.get("risk") or {}
        if isinstance(risk, dict) and risk.get("active"):
            peak_ms = self._parse_alert_ts(risk.get("ts", risk.get("time")))
            if peak_ms is not None:
                _add_candidate(
                    "open_meteo_dwd_icon",
                    risk.get("level", 0),
                    peak_ms - 30 * 60000,
                    peak_ms + 90 * 60000,
                    "Gewitterrisiko",
                    risk.get("reason"),
                )

        if not candidates:
            highest = int(payload.get("highest_level") or 0)
            return {
                **self._storm_empty(payload.get("summary") or "Keine relevante Unwetterwarnung im Planungshorizont"),
                "level": highest,
                "payload_active": bool(payload.get("active")),
                "payload_thunderstorm_active": bool(payload.get("thunderstorm_active")),
            }

        candidates.sort(key=lambda c: (-int(c.get("level", 0)), int(c.get("start_ts", 0))))
        event = candidates[0]
        level = int(event.get("level", 0))
        active = level >= int(self.storm_guard_min_level)
        warning_only = mode == "warn" or not active
        control = mode == "control" and active
        grid_allowed = bool(
            control
            and self.storm_guard_grid_enable
            and level >= int(self.storm_guard_grid_min_level)
        )
        title = event.get("title") or payload.get("title") or "Wetterwarnung"
        reason = event.get("reason") or payload.get("summary") or title
        action_label = "Warnung beobachtet, kein aktiver Eingriff"
        if grid_allowed:
            action_label = "Warnung darf Netzladen auslösen"
        elif control:
            action_label = "Warnung für Regeleingriff bewertet"
        return {
            "mode": mode,
            "active": active,
            "warning_only": warning_only,
            "control_active": control,
            "grid_allowed": grid_allowed,
            "level": level,
            "source": event.get("source"),
            "title": title,
            "reason": reason,
            "action_label": action_label,
            "control_summary": action_label + ": " + reason,
            "start_ts": int(event.get("start_ts", 0)),
            "end_ts": int(event.get("end_ts", 0)),
            "start_t": datetime.fromtimestamp(float(event.get("start_ts", 0)) / 1000).strftime("%H:%M"),
            "end_t": datetime.fromtimestamp(float(event.get("end_ts", 0)) / 1000).strftime("%H:%M"),
            "min_level": int(self.storm_guard_min_level),
            "grid_min_level": int(self.storm_guard_grid_min_level),
        }

    def _curve_soc_at_anchor_ts(self, anchors, ts, fallback_soc):
        if not anchors:
            return float(fallback_soc)
        ordered = sorted((a for a in anchors if a.get("ts") is not None), key=lambda a: float(a.get("ts", 0)))
        if not ordered:
            return float(fallback_soc)
        target_ts = float(ts)
        if target_ts <= float(ordered[0].get("ts", 0)):
            return float(ordered[0].get("soc", fallback_soc))
        if target_ts >= float(ordered[-1].get("ts", 0)):
            return float(ordered[-1].get("soc", fallback_soc))
        for prev, nxt in zip(ordered, ordered[1:]):
            t0 = float(prev.get("ts", 0))
            t1 = float(nxt.get("ts", 0))
            if t0 <= target_ts <= t1:
                s0 = float(prev.get("soc", fallback_soc))
                s1 = float(nxt.get("soc", s0))
                if t1 <= t0:
                    return s1
                return s0 + ((target_ts - t0) / (t1 - t0)) * (s1 - s0)
        return float(fallback_soc)

    def _storm_guard_night_grid_plan(self, event, timeline, anchors, current_soc, now_ms, curve_start_ts):
        grid_charge = self._storm_grid_empty(current_soc)
        if not event.get("control_active"):
            return event, grid_charge
        if not event.get("grid_allowed"):
            event["night_guard_active"] = False
            event["night_guard_reason"] = "Nachtreserve braucht explizit erlaubtes Netzladen"
            return event, grid_charge

        start_ts = float(event.get("start_ts") or now_ms)
        end_ts = float(event.get("end_ts") or (start_ts + 90 * 60000))
        lead_ms = float(self.storm_guard_lead_min) * 60000.0
        precharge_ts = max(float(now_ms), start_ts - lead_ms)
        window_end = min(max(end_ts, start_ts + 15 * 60000), float(curve_start_ts))
        if now_ms < precharge_ts:
            event["night_guard_active"] = False
            event["night_guard_reason"] = "Nachtwarnung noch ausserhalb der Vorlaufzeit"
            return event, grid_charge
        if now_ms >= window_end:
            event["night_guard_active"] = False
            event["night_guard_reason"] = "Nachtwarnung liegt ausserhalb des Restnacht-Fensters"
            return event, grid_charge

        night_load_wh = 0.0
        for slot in timeline:
            ts = float(slot.get("ts", slot.get("start_timestamp", 0)) or 0)
            if ts < now_ms - 15 * 60000 or ts >= float(curve_start_ts):
                continue
            pv = float(slot.get("pv_w", 0) or 0)
            home = float(slot.get("home_w", 0) or 0)
            wp = float(slot.get("wp_w", 0) or 0)
            climate = float(slot.get("climate_w", 0) or 0)
            surplus = float(slot.get("surplus_w", pv - home - wp - climate) or 0)
            night_load_wh += max(0.0, -surplus) * 0.25

        if night_load_wh <= 0:
            event["night_guard_active"] = False
            event["night_guard_reason"] = "Kein Restnacht-Verbrauchsbedarf in der Prognose"
            return event, grid_charge

        regular_morning_soc = float(anchors[0].get("soc", self.morning_soc)) if anchors else float(self.morning_soc)
        morning_buffer_soc = max(0.0, min(100.0, float(getattr(self, "storm_guard_grid_morning_soc", 20.0))))
        night_load_pct = night_load_wh / max(1.0, self.capacity_wh) * 100.0
        projected_morning_soc = float(current_soc) - night_load_pct
        target_soc = min(float(self.storm_guard_max_soc), morning_buffer_soc + night_load_pct)
        target_soc = round(max(float(current_soc), min(100.0, target_soc)), 1)

        event.update({
            "night_guard_active": True,
            "night_guard_load_wh": round(night_load_wh, 0),
            "night_guard_load_pct": round(night_load_pct, 2),
            "night_guard_morning_buffer_soc": round(morning_buffer_soc, 1),
            "night_guard_regular_morning_anchor_soc": round(regular_morning_soc, 1),
            "night_guard_projected_morning_soc": round(projected_morning_soc, 1),
            "night_guard_target_soc": target_soc,
        })

        if projected_morning_soc >= morning_buffer_soc - 0.5:
            event["night_guard_reason"] = (
                "Nachtreserve ausreichend: Prognose %.1f%% am Morgen, Puffer %.1f%%"
                % (projected_morning_soc, morning_buffer_soc)
            )
            return event, grid_charge
        if target_soc <= float(current_soc) + 0.2:
            event["night_guard_reason"] = "Nachtreserve waere noetig, Ziel ist durch Max-SoC bereits erreicht"
            return event, grid_charge

        grid_charge = {
            "active": bool(float(current_soc) < target_soc - 0.5),
            "reason": (
                "Unwetter-Nachtreserve: Stufe %s, Ziel %.1f%% für Morgenpuffer %.1f%%"
                % (event.get("level"), target_soc, morning_buffer_soc)
            ),
            "target_soc": target_soc,
            "charge_w": int(max(300, self.max_charge_w)),
            "hysteresis_pct": 0.5,
            "window_start": int(precharge_ts),
            "window_end": int(window_end),
            "level": int(event.get("level", 0)),
            "night_guard": True,
        }
        event["night_guard_reason"] = grid_charge["reason"]
        event["reason"] = grid_charge["reason"]
        return event, grid_charge

    def _storm_guard_plan(self, timeline, anchors, current_soc, now_ms, curve_start_ts, curve_end_ts):
        event = self._storm_guard_event(now_ms)
        grid_charge = self._storm_grid_empty(current_soc)
        if not event.get("active"):
            return event, anchors, grid_charge

        start_ts = float(event.get("start_ts") or now_ms)
        end_ts = float(event.get("end_ts") or (start_ts + 90 * 60000))
        if end_ts <= now_ms - 5 * 60000:
            event["reason"] = "Unwetterwarnung liegt bereits hinter dem Planfenster"
            return event, anchors, grid_charge
        if start_ts < float(curve_start_ts):
            event, night_grid_charge = self._storm_guard_night_grid_plan(
                event, timeline, anchors, current_soc, now_ms, curve_start_ts
            )
            if night_grid_charge.get("active"):
                grid_charge = night_grid_charge
        if end_ts <= float(curve_start_ts):
            if not grid_charge.get("active"):
                event["reason"] = event.get("night_guard_reason") or "Unwetter vor dem aktiven Ladefenster"
            event["control_active"] = False
            return event, anchors, grid_charge

        lead_ms = float(self.storm_guard_lead_min) * 60000.0
        precharge_ts = max(float(curve_start_ts), min(start_ts, max(float(now_ms), start_ts - lead_ms)))
        event_end_for_curve = max(start_ts + 15 * 60000, min(end_ts, float(curve_end_ts)))
        if precharge_ts >= float(curve_end_ts):
            event["reason"] = "Unwetter nach dem aktiven Ladefenster"
            return event, anchors, grid_charge

        curve_pre_soc = self._curve_soc_at_anchor_ts(anchors, precharge_ts, current_soc)
        curve_start_soc = self._curve_soc_at_anchor_ts(anchors, start_ts, curve_pre_soc)
        curve_end_soc = self._curve_soc_at_anchor_ts(anchors, event_end_for_curve, curve_start_soc)

        storm_net_load_wh = 0.0
        future_surplus_wh = 0.0
        for slot in timeline:
            ts = float(slot.get("ts", slot.get("start_timestamp", 0)) or 0)
            pv = float(slot.get("pv_w", 0) or 0)
            home = float(slot.get("home_w", 0) or 0)
            wp = float(slot.get("wp_w", 0) or 0)
            climate = float(slot.get("climate_w", 0) or 0)
            surplus = float(slot.get("surplus_w", pv - home - wp - climate) or 0)
            if start_ts <= ts < end_ts:
                storm_net_load_wh += max(0.0, home + wp + climate - pv * 0.25) * 0.25
            if end_ts <= ts < float(curve_end_ts):
                future_surplus_wh += max(0.0, surplus) * 0.25

        need_after_storm_wh = max(0.0, (float(self.target_soc) - curve_end_soc) * self.capacity_wh / 100.0)
        pv_recovery_factor = max(0.0, min(1.0, 1.0 - (future_surplus_wh / max(1.0, need_after_storm_wh)))) if need_after_storm_wh > 0 else 1.0
        time_factor = max(0.0, min(1.0, (start_ts - float(curve_start_ts)) / max(1.0, float(curve_end_ts) - float(curve_start_ts))))
        precharge_factor = max(time_factor, pv_recovery_factor)

        storm_need_wh = max(0.0, (curve_end_soc - curve_start_soc) * self.capacity_wh / 100.0) + storm_net_load_wh
        precharge_wh = storm_need_wh * precharge_factor
        min_precharge_wh = float(self.storm_guard_min_precharge_kwh) * 1000.0
        max_soc = min(float(self.storm_guard_max_soc), 100.0)
        target_soc = min(max_soc, max(curve_pre_soc, curve_pre_soc + (precharge_wh / max(1.0, self.capacity_wh)) * 100.0))
        target_soc = round(max(0.0, min(100.0, target_soc)), 1)

        event.update({
            "precharge_ts": int(precharge_ts),
            "precharge_t": datetime.fromtimestamp(precharge_ts / 1000).strftime("%H:%M"),
            "storm_need_wh": round(storm_need_wh, 0),
            "precharge_wh": round(precharge_wh, 0),
            "precharge_factor": round(precharge_factor, 3),
            "future_surplus_after_wh": round(future_surplus_wh, 0),
            "need_after_storm_wh": round(need_after_storm_wh, 0),
            "curve_pre_soc": round(curve_pre_soc, 1),
            "curve_start_soc": round(curve_start_soc, 1),
            "curve_end_soc": round(curve_end_soc, 1),
            "target_soc": target_soc,
        })

        if not event.get("control_active"):
            event["action_label"] = "Warnung beobachtet, kein aktiver Eingriff"
            event["reason"] = "%s (nur Warnung, kein Regeleingriff)" % (event.get("reason") or "Unwetterwarnung")
            event["control_summary"] = event["action_label"] + ": " + event["reason"]
            return event, anchors, grid_charge
        if precharge_wh < min_precharge_wh:
            event["control_active"] = False
            event["action_label"] = "Warnung beobachtet, kein aktiver Eingriff"
            event["reason"] = "Warnung beobachtet, kein aktiver Eingriff: Vorladebedarf %.0fWh unter Mindestwert %.0fWh" % (precharge_wh, min_precharge_wh)
            event["control_summary"] = event["reason"]
            return event, anchors, grid_charge
        if target_soc <= curve_pre_soc + 0.2:
            event["control_active"] = False
            event["action_label"] = "Warnung beobachtet, kein aktiver Eingriff"
            event["reason"] = "Warnung beobachtet, kein aktiver Eingriff: Kurve hat bereits genug Reserve"
            event["control_summary"] = event["reason"]
            return event, anchors, grid_charge

        new_anchors = [dict(a) for a in anchors]
        updated_existing = False
        for anchor in new_anchors:
            if abs(float(anchor.get("ts", 0)) - precharge_ts) <= 5 * 60000:
                if not anchor.get("frozen") or precharge_ts <= now_ms + 60000:
                    anchor["soc"] = round(max(float(anchor.get("soc", 0)), target_soc), 2)
                anchor["kind"] = "storm_guard"
                anchor["storm_guard"] = True
                anchor["t"] = datetime.fromtimestamp(float(anchor.get("ts", precharge_ts)) / 1000).strftime("%H:%M")
                updated_existing = True
                break
        if not updated_existing:
            new_anchors.append({
                "ts": int(precharge_ts),
                "t": datetime.fromtimestamp(precharge_ts / 1000).strftime("%H:%M"),
                "soc": target_soc,
                "frozen": False,
                "kind": "storm_guard",
                "storm_guard": True,
            })
        new_anchors.sort(key=lambda a: float(a.get("ts", 0)))
        event["reason"] = (
            "Unwetterwächter: Ziel %.1f%% bis %s, Faktor %.0f%%, Rest-PV nach Warnende %.0fWh"
            % (target_soc, event.get("precharge_t"), precharge_factor * 100.0, future_surplus_wh)
        )
        event["action_label"] = "Warnung hebt Ladekurve an"
        event["control_summary"] = event["reason"]

        if event.get("grid_allowed"):
            window_end = start_ts if now_ms < start_ts else min(end_ts, float(curve_end_ts))
            active_grid = bool(precharge_ts <= now_ms < window_end and current_soc < target_soc - 0.5)
            grid_charge = {
                "active": active_grid,
                "reason": (
                    "Unwetterwächter Netzladen: Stufe %s, Ziel %.1f%% bis %s"
                    % (event.get("level"), target_soc, event.get("precharge_t"))
                ),
                "target_soc": target_soc,
                "charge_w": int(max(300, self.max_charge_w)),
                "hysteresis_pct": 0.5,
                "window_start": int(precharge_ts),
                "window_end": int(window_end),
                "level": int(event.get("level", 0)),
            }
            event["action_label"] = "Warnung darf Netzladen auslösen"
            event["control_summary"] = grid_charge["reason"]
        return event, new_anchors, grid_charge

    def _configured_location(self):
        lat = self._safe_float(self.v4_config.get('hoehe', self.v4_config.get('latitude', 49.0)), 49.0)
        lon = self._safe_float(self.v4_config.get('laenge', self.v4_config.get('longitude', 10.0)), 10.0)
        return lat, lon

    def _configured_pv_peak_kw(self):
        try:
            if os.path.exists(LIVE_DATA_FILE):
                with open(LIVE_DATA_FILE, 'r', encoding='utf-8-sig') as f:
                    live = json.load(f)
                peak_w = self._safe_float(live.get("installed_peak_power_w", 0), 0.0)
                if peak_w > 500:
                    return peak_w / 1000.0
        except Exception:
            pass

        for key in (
            "installed_peak_power_w", "pv_peak_w", "pv_peak_power_w",
            "pv_kwp", "pv_peak_kwp", "anlagenleistung_kwp",
        ):
            value = self._safe_float(self.v4_config.get(key, 0), 0.0)
            if value > 500:
                return value / 1000.0
            if value > 0:
                return value

        roof_kwp = 0.0
        for key in ("forecast1", "forecast2", "forecast3"):
            raw = str(self.v4_config.get(key, "") or "").strip()
            if not raw:
                continue
            try:
                roof_kwp += max(0.0, float(raw.split(",")[2].strip()))
            except Exception:
                continue
        if roof_kwp > 0:
            return roof_kwp

        return max(3.0, min(30.0, (float(self.max_charge_w) / 1000.0) + 2.0))

    def _solar_window_hours(self, day_date):
        lat, lon = self._configured_location()
        try:
            doy = day_date.timetuple().tm_yday
            decl = math.radians(23.45 * math.sin(2.0 * math.pi * (doy - 81) / 364.0))
            lat_r = math.radians(max(-66.0, min(66.0, lat)))
            cos_ha = -math.tan(lat_r) * math.tan(decl)
            cos_ha = max(-0.999, min(0.999, cos_ha))
            ha_h = math.degrees(math.acos(cos_ha)) / 15.0
            timezone_center_lon = 15.0
            solar_noon = 12.0 - ((lon - timezone_center_lon) / 15.0)
            sunrise = max(3.5, solar_noon - ha_h)
            sunset = min(22.5, solar_noon + ha_h)
            return sunrise, solar_noon, sunset
        except Exception:
            return 6.0, 12.5, 19.0

    def _clear_sky_pv_w(self, slot_dt, peak_kw):
        sunrise, _solar_noon, sunset = self._solar_window_hours(slot_dt.date())
        hour = slot_dt.hour + slot_dt.minute / 60.0
        if hour < sunrise or hour > sunset or sunrise >= sunset:
            return 0.0
        progress = (hour - sunrise) / max(0.1, sunset - sunrise)
        solar_elev = max(0.0, math.sin(math.pi * progress))
        if solar_elev < 0.01:
            return 0.0
        air_mass = 1.0 / max(0.05, solar_elev)
        transmission = 0.70 ** air_mass
        return max(0.0, peak_kw * 1000.0 * solar_elev * transmission)

    def _apply_live_cloud_edge_headroom(self, day_slots, now_ms, live_snapshot):
        if not self._cfg_bool(self.v4_config.get("storage_cloud_edge_headroom_enable"), True):
            return {"active": False, "reason": "disabled"}
        if not day_slots or self.export_limit_w <= 0 or self.max_charge_w <= 0:
            return {"active": False, "reason": "no_export_limit"}

        live_snapshot = live_snapshot or {}
        live_pv_w = max(0.0, self._safe_float(live_snapshot.get("pv_w", 0.0), 0.0))
        if live_pv_w <= 0.0:
            return {"active": False, "reason": "no_live_pv"}
        live_ts_s = self._safe_float(live_snapshot.get("ts_s", 0.0), 0.0)
        now_s = max(0.0, float(now_ms) / 1000.0)
        max_age_s = max(60.0, self._safe_float(self.v4_config.get("storage_cloud_edge_live_max_age_s", 900.0), 900.0))
        if live_ts_s > 0.0 and now_s > 0.0 and now_s - live_ts_s > max_age_s:
            return {"active": False, "reason": "stale_live"}

        grid_w = self._safe_float(live_snapshot.get("grid_w", 0.0), 0.0)
        live_export_w = max(0.0, -grid_w)
        live_derate_w = max(0.0, self._safe_float(live_snapshot.get("derate_w", 0.0), 0.0))
        power_limits_active = self._cfg_bool(live_snapshot.get("power_limits_active"), False)
        safe_home_w = max(0.0, self._safe_float(self.v4_config.get("storage_cloud_edge_min_home_w", 300.0), 300.0))
        enter_margin_w = max(100.0, self._safe_float(self.v4_config.get("storage_cloud_edge_enter_margin_w", 500.0), 500.0))
        export_near_band_w = max(200.0, self._safe_float(self.v4_config.get("storage_cloud_edge_export_near_band_w", 1200.0), 1200.0))
        live_pressure_w = max(0.0, live_pv_w - self.export_limit_w - safe_home_w)
        high_pv_threshold_w = max(
            self.export_limit_w + safe_home_w + enter_margin_w,
            self._safe_float(self.v4_config.get("storage_cloud_edge_min_pv_w", 0.0), 0.0),
        )

        slots = sorted(
            [slot for slot in (day_slots or []) if isinstance(slot, dict)],
            key=lambda item: self._safe_float(item.get("ts", 0.0), 0.0),
        )
        if not slots:
            return {"active": False, "reason": "no_slots"}
        nearest = min(slots, key=lambda item: abs(self._safe_float(item.get("ts", 0.0), 0.0) - float(now_ms)))
        forecast_now_w = max(0.0, self._safe_float(nearest.get("pv_w", 0.0), 0.0))
        ratio = live_pv_w / forecast_now_w if forecast_now_w >= 500.0 else 99.0
        ratio_enter = max(1.02, self._safe_float(self.v4_config.get("storage_cloud_edge_forecast_ratio", 1.25), 1.25))
        export_near = bool(live_export_w >= max(0.0, self.export_limit_w - export_near_band_w))
        live_high = bool(live_pv_w >= high_pv_threshold_w or live_pressure_w >= enter_margin_w)
        under_forecast = bool(ratio >= ratio_enter)
        if not (live_high and (under_forecast or export_near or power_limits_active or live_derate_w > 0.0)):
            return {
                "active": False,
                "reason": "below_trigger",
                "live_pv_w": round(live_pv_w, 0),
                "forecast_now_w": round(forecast_now_w, 0),
                "forecast_ratio": round(ratio, 3),
            }

        horizon_h = max(0.5, min(8.0, self._safe_float(self.v4_config.get("storage_cloud_edge_horizon_h", 4.0), 4.0)))
        ratio_cap = max(1.0, self._safe_float(self.v4_config.get("storage_cloud_edge_ratio_cap", 1.8), 1.8))
        clear_factor = max(0.0, self._safe_float(self.v4_config.get("storage_cloud_edge_clear_sky_factor", 1.08), 1.08))
        peak_factor = max(1.0, self._safe_float(self.v4_config.get("storage_cloud_edge_peak_factor", 1.15), 1.15))
        min_pressure_w = max(100.0, self._safe_float(self.v4_config.get("storage_cloud_edge_min_pressure_w", 300.0), 300.0))
        peak_kw = max(0.0, self._configured_pv_peak_kw())
        physical_cap_w = max(live_pv_w, peak_kw * 1000.0 * peak_factor)
        ratio_limited = min(ratio_cap, max(1.0, ratio))
        horizon_ms = horizon_h * 3600000.0
        marked = 0
        reserve_pressure_wh = 0.0
        max_headroom_w = 0.0
        for slot in slots:
            ts = self._safe_float(slot.get("ts", 0.0), 0.0)
            if ts < float(now_ms) - 60000.0 or ts > float(now_ms) + horizon_ms:
                continue
            base_pv_w = max(0.0, self._safe_float(slot.get("pv_w", 0.0), 0.0))
            if base_pv_w <= 50.0:
                continue
            progress = max(0.0, min(1.0, (ts - float(now_ms)) / max(1.0, horizon_ms)))
            taper = 1.0 - progress
            slot_dt = datetime.fromtimestamp(ts / 1000.0)
            clear_w = self._clear_sky_pv_w(slot_dt, peak_kw) * clear_factor if peak_kw > 0.0 else 0.0
            scaled_w = base_pv_w * (1.0 + (ratio_limited - 1.0) * taper)
            carry_w = live_pv_w * max(0.0, 1.0 - progress * 1.5)
            headroom_pv_w = min(physical_cap_w, max(base_pv_w, clear_w, scaled_w, carry_w))
            pressure_w = max(0.0, headroom_pv_w - self.export_limit_w - safe_home_w)
            pressure_w = min(float(self.max_charge_w), pressure_w)
            if pressure_w < min_pressure_w or headroom_pv_w <= base_pv_w + 50.0:
                continue
            previous_headroom_w = max(
                0.0,
                self._safe_float(slot.get("pv_headroom_w", slot.get("cloud_edge_pv_w", 0.0)), 0.0),
            )
            if headroom_pv_w > previous_headroom_w + 50.0:
                slot["pv_headroom_w"] = round(headroom_pv_w, 0)
                slot["headroom_reserve_active"] = True
                slot["headroom_reserve_source"] = "live_cloud_edge"
                marked += 1
                reserve_pressure_wh += pressure_w * 0.25
                max_headroom_w = max(max_headroom_w, headroom_pv_w)

        if marked <= 0:
            return {
                "active": False,
                "reason": "no_future_pressure",
                "live_pv_w": round(live_pv_w, 0),
                "forecast_now_w": round(forecast_now_w, 0),
                "forecast_ratio": round(ratio, 3),
            }
        return {
            "active": True,
            "source": "live_cloud_edge",
            "live_pv_w": round(live_pv_w, 0),
            "live_export_w": round(live_export_w, 0),
            "forecast_now_w": round(forecast_now_w, 0),
            "forecast_ratio": round(ratio, 3),
            "horizon_h": round(horizon_h, 2),
            "slots": int(marked),
            "reserve_pressure_wh": round(reserve_pressure_wh, 0),
            "max_headroom_pv_w": round(max_headroom_w, 0),
            "export_limit_w": round(self.export_limit_w, 0),
        }

    def _load_weather_forecast_hours(self):
        try:
            if not os.path.exists(WEATHER_FORECAST_FILE):
                return {}
            with open(WEATHER_FORECAST_FILE, "r", encoding="utf-8-sig") as f:
                data = json.load(f)
        except Exception:
            return {}

        hourly = data.get("hourly") if isinstance(data, dict) else None
        if not isinstance(hourly, dict):
            return {}

        result = {}
        for raw_ts, entry in hourly.items():
            if not isinstance(entry, dict):
                continue
            try:
                ts = float(raw_ts)
                if ts > 100000000000:
                    ts /= 1000.0
                hour_ts = int(round(ts / 3600.0) * 3600)
            except Exception:
                continue
            result[hour_ts] = entry
        return result

    def _weather_cold_peak_factor(self, ts_ms, weather_hours):
        if not self._cfg_bool(self.v4_config.get("storage_historical_peak_temp_enable"), True):
            return 1.0, None, None
        if not weather_hours:
            return 1.0, None, None

        try:
            ts_s = float(ts_ms) / 1000.0
            nearest = min(weather_hours.keys(), key=lambda key: abs(float(key) - ts_s))
            if abs(float(nearest) - ts_s) > 5400.0:
                return 1.0, None, None
            entry = weather_hours.get(nearest) or {}
            temp_c = None
            radiation_wm2 = None
            if entry.get("temp_c") is not None:
                temp_c = float(str(entry.get("temp_c")).replace(",", "."))
            if entry.get("radiation_wm2") is not None:
                radiation_wm2 = float(str(entry.get("radiation_wm2")).replace(",", "."))
        except Exception:
            return 1.0, None, None

        if temp_c is None:
            return 1.0, None, radiation_wm2
        rad_min = max(0.0, self._safe_float(
            self.v4_config.get("storage_historical_peak_temp_min_radiation_wm2", 500.0),
            500.0,
        ))
        if radiation_wm2 is not None and radiation_wm2 > 0.0 and radiation_wm2 < rad_min:
            return 1.0, temp_c, radiation_wm2

        ref_c = self._safe_float(self.v4_config.get("storage_historical_peak_temp_ref_c", 12.0), 12.0)
        per_c = max(0.0, min(0.02, self._safe_float(
            self.v4_config.get("storage_historical_peak_temp_gain_per_c", 0.006),
            0.006,
        )))
        max_boost = max(0.0, min(0.20, self._safe_float(
            self.v4_config.get("storage_historical_peak_temp_max_boost", 0.08),
            0.08,
        )))
        if temp_c >= ref_c:
            return 1.0, temp_c, radiation_wm2
        return 1.0 + min(max_boost, (ref_c - temp_c) * per_c), temp_c, radiation_wm2

    def _historical_curtailment_limit_w(self):
        configured = 0.0
        for key in (
            "storage_historical_peak_physical_limit_w",
            "storage_pv_physical_limit_w",
            "wechselrichter_ac_limit_w",
            "inverter_ac_limit_w",
        ):
            value = self._safe_float(self.v4_config.get(key, 0.0), 0.0)
            if value > 0.0:
                configured = value
                break
        if configured > 0.0 and self.export_limit_w > 0.0:
            return min(float(self.export_limit_w), configured)
        if configured > 0.0:
            return configured
        return float(self.export_limit_w)

    def _history_power_value(self, item, keys):
        if not isinstance(item, dict):
            return None
        for key in keys:
            if key not in item:
                continue
            try:
                return max(0.0, self._safe_float(item.get(key), 0.0))
            except Exception:
                continue
        return None

    def _history_peak_paths(self, target_date, max_days, max_files):
        cutoff_date = target_date - timedelta(days=max_days)
        paths = []
        if os.path.exists(LIVE_HISTORY_FILE):
            paths.append(LIVE_HISTORY_FILE)

        try:
            files = sorted(
                (fn for fn in os.listdir(HISTORY_DIR) if fn.startswith("history_") and fn.endswith(".txt")),
                reverse=True,
            )
        except Exception:
            files = []

        for filename in files:
            file_date = None
            raw_date = filename[len("history_"):-len(".txt")]
            try:
                file_date = datetime.fromisoformat(raw_date).date()
            except Exception:
                file_date = None
            if file_date is not None:
                if file_date >= target_date or file_date < cutoff_date:
                    continue
            path = os.path.join(HISTORY_DIR, filename)
            if path not in paths:
                paths.append(path)
            if len(paths) >= max_files:
                break
        return paths

    def _apply_historical_peak_headroom(self, day_slots, selected_day_start_ms, now_ms):
        if not self._cfg_bool(self.v4_config.get("storage_historical_peak_headroom_enable"), True):
            return {"active": False, "reason": "disabled"}
        if not day_slots or self.max_charge_w <= 0.0:
            return {"active": False, "reason": "no_slots"}

        curtailment_limit_w = self._historical_curtailment_limit_w()
        if curtailment_limit_w <= 0.0:
            return {"active": False, "reason": "no_curtailment_limit"}

        try:
            target_date = datetime.fromtimestamp(float(selected_day_start_ms) / 1000.0).date()
        except Exception:
            target_date = datetime.now().date()

        max_days = int(max(2, min(90, self._safe_float(
            self.v4_config.get("storage_historical_peak_days", 21),
            21,
        ))))
        max_files = int(max(3, min(120, self._safe_float(
            self.v4_config.get("storage_historical_peak_max_files", max_days + 7),
            max_days + 7,
        ))))
        paths = self._history_peak_paths(target_date, max_days, max_files)
        if not paths:
            return {"active": False, "reason": "no_history"}

        start_hour = max(0.0, min(23.75, self._safe_float(
            self.v4_config.get("storage_historical_peak_start_hour", 8.0),
            8.0,
        )))
        end_hour = max(start_hour + 0.25, min(24.0, self._safe_float(
            self.v4_config.get("storage_historical_peak_end_hour", 17.0),
            17.0,
        )))
        pv_quantile = max(0.50, min(0.99, self._safe_float(
            self.v4_config.get("storage_historical_peak_pv_quantile", 0.90),
            0.90,
        )))
        home_quantile = max(0.05, min(0.50, self._safe_float(
            self.v4_config.get("storage_historical_safe_home_quantile", 0.20),
            0.20,
        )))
        min_days = int(max(1, min(10, self._safe_float(
            self.v4_config.get("storage_historical_peak_min_days", 2),
            2,
        ))))
        min_slot_samples = int(max(1, min(20, self._safe_float(
            self.v4_config.get("storage_historical_peak_min_slot_samples", 2),
            2,
        ))))
        flex_limit_w = max(0.0, self._safe_float(
            self.v4_config.get("storage_historical_safe_home_flex_limit_w", 250.0),
            250.0,
        ))
        min_home_w = max(0.0, self._safe_float(
            self.v4_config.get(
                "storage_historical_peak_min_home_w",
                self.v4_config.get("storage_cloud_edge_min_home_w", 300.0),
            ),
            300.0,
        ))
        min_pressure_w = max(100.0, self._safe_float(
            self.v4_config.get("storage_historical_peak_min_pressure_w", 300.0),
            300.0,
        ))
        peak_cap_factor = max(1.0, min(1.5, self._safe_float(
            self.v4_config.get("storage_historical_peak_cap_factor", 1.15),
            1.15,
        )))
        explicit_peak_cap_w = self._safe_float(
            self.v4_config.get("storage_historical_peak_max_pv_w", 0.0),
            0.0,
        )
        configured_peak_w = max(0.0, self._configured_pv_peak_kw() * 1000.0 * peak_cap_factor)
        pv_cap_w = explicit_peak_cap_w if explicit_peak_cap_w > 0.0 else configured_peak_w

        slot_values = {}
        pv_days = set()
        safe_days = set()
        all_safe_home = []
        parsed_rows = 0
        cutoff_date = target_date - timedelta(days=max_days)

        for path in paths:
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        dt = self._parse_history_timestamp(item.get("ts", item.get("_ts")))
                        if dt is None or dt.date() >= target_date or dt.date() < cutoff_date:
                            continue
                        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
                        if hour < start_hour or hour > end_hour:
                            continue
                        pv_w = self._history_power_value(
                            item,
                            ("pv", "PV_Power", "pv_w", "solar_w", "solar", "Sonne"),
                        )
                        if pv_w is None:
                            continue
                        slot_key = dt.hour * 4 + int(dt.minute / 15)
                        bucket = slot_values.setdefault(slot_key, {"pv": [], "safe_home": []})
                        bucket["pv"].append(pv_w)
                        if pv_w > 50.0:
                            pv_days.add(dt.date().isoformat())
                        parsed_rows += 1

                        home_w = self._history_power_value(
                            item,
                            ("home", "Home_Power", "Consumption_W", "home_raw"),
                        )
                        wb_w = self._history_power_value(item, ("wb", "Wallbox_Power", "wb_power_w")) or 0.0
                        wb2_w = self._history_power_value(item, ("wb2", "Wallbox2_Power", "wb2_power_w")) or 0.0
                        wp_w = self._history_power_value(item, ("wp", "WP_Power", "Leistungsaufnahme")) or 0.0
                        hs_w = self._history_power_value(item, ("hs", "hs_power", "Heizstab_Power")) or 0.0
                        if home_w is not None and home_w >= 50.0 and (wb_w + wb2_w + wp_w + hs_w) <= flex_limit_w:
                            bucket["safe_home"].append(home_w)
                            all_safe_home.append(home_w)
                            safe_days.add(dt.date().isoformat())
            except Exception:
                continue

        if len(pv_days) < min_days:
            return {
                "active": False,
                "reason": "insufficient_samples",
                "sample_days": len(pv_days),
                "parsed_rows": parsed_rows,
            }

        global_safe_home = self._percentile_value(all_safe_home, home_quantile)
        if global_safe_home is None:
            global_safe_home = min_home_w
        global_safe_home = max(min_home_w, global_safe_home)

        weather_hours = self._load_weather_forecast_hours()
        marked = 0
        reserve_pressure_wh = 0.0
        max_headroom_w = 0.0
        max_temp_factor = 1.0
        min_temp_c = None
        max_radiation_wm2 = None

        for slot in sorted((slot for slot in day_slots if isinstance(slot, dict)), key=lambda s: self._safe_float(s.get("ts", 0), 0.0)):
            ts = self._safe_float(slot.get("ts", 0.0), 0.0)
            if ts < float(now_ms) - 60000.0:
                continue
            try:
                slot_dt = datetime.fromtimestamp(ts / 1000.0)
            except Exception:
                continue
            slot_key = slot_dt.hour * 4 + int(slot_dt.minute / 15)
            bucket = slot_values.get(slot_key)
            if not bucket or len(bucket.get("pv", [])) < min_slot_samples:
                continue
            hist_pv_w = self._percentile_value(bucket.get("pv", []), pv_quantile)
            if hist_pv_w is None:
                continue
            slot_safe_home = self._percentile_value(bucket.get("safe_home", []), home_quantile)
            if slot_safe_home is None:
                slot_safe_home = global_safe_home
            safe_home_w = max(min_home_w, slot_safe_home)
            temp_factor, temp_c, radiation_wm2 = self._weather_cold_peak_factor(ts, weather_hours)
            headroom_pv_w = hist_pv_w * max(1.0, temp_factor)
            if pv_cap_w > 0.0:
                headroom_pv_w = min(pv_cap_w, headroom_pv_w)
            pressure_w = max(
                0.0,
                headroom_pv_w - curtailment_limit_w - safe_home_w - _slot_planned_load_w(slot),
            )
            preventable_w = min(float(self.max_charge_w), pressure_w)
            if preventable_w < min_pressure_w:
                continue
            previous_headroom_w = max(
                0.0,
                self._safe_float(slot.get("pv_headroom_w", slot.get("cloud_edge_pv_w", 0.0)), 0.0),
            )
            if headroom_pv_w <= previous_headroom_w + 50.0:
                continue
            slot["pv_headroom_w"] = round(headroom_pv_w, 0)
            slot["headroom_reserve_active"] = True
            slot["headroom_reserve_source"] = "historical_peak"
            slot["safe_home_w"] = round(safe_home_w, 0)
            slot["curtailment_limit_w"] = round(curtailment_limit_w, 0)
            marked += 1
            reserve_pressure_wh += preventable_w * 0.25
            max_headroom_w = max(max_headroom_w, headroom_pv_w)
            max_temp_factor = max(max_temp_factor, max(1.0, temp_factor))
            if temp_c is not None:
                min_temp_c = temp_c if min_temp_c is None else min(min_temp_c, temp_c)
            if radiation_wm2 is not None:
                max_radiation_wm2 = radiation_wm2 if max_radiation_wm2 is None else max(max_radiation_wm2, radiation_wm2)

        if marked <= 0:
            return {
                "active": False,
                "reason": "no_future_pressure",
                "sample_days": len(pv_days),
                "safe_sample_days": len(safe_days),
                "parsed_rows": parsed_rows,
            }

        return {
            "active": True,
            "source": "historical_peak",
            "slots": int(marked),
            "sample_days": len(pv_days),
            "safe_sample_days": len(safe_days),
            "parsed_rows": parsed_rows,
            "pv_quantile": round(pv_quantile, 2),
            "home_quantile": round(home_quantile, 2),
            "safe_home_w": round(global_safe_home, 0),
            "reserve_pressure_wh": round(reserve_pressure_wh, 0),
            "max_headroom_pv_w": round(max_headroom_w, 0),
            "curtailment_limit_w": round(curtailment_limit_w, 0),
            "temp_factor_max": round(max_temp_factor, 3),
            "min_temp_c": round(min_temp_c, 1) if min_temp_c is not None else None,
            "max_radiation_wm2": round(max_radiation_wm2, 0) if max_radiation_wm2 is not None else None,
        }

    def _parse_history_timestamp(self, value):
        if value is None:
            return None
        try:
            if isinstance(value, (int, float)):
                ts = float(value)
                if ts > 100000000000:
                    ts /= 1000.0
                return datetime.fromtimestamp(ts)
            text = str(value).strip()
            if not text:
                return None
            if text.endswith("Z"):
                text = text[:-1] + "+00:00"
            try:
                return datetime.fromisoformat(text.replace(" ", "T"))
            except Exception:
                for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%d.%m.%Y %H:%M:%S"):
                    try:
                        return datetime.strptime(text[:19], fmt)
                    except Exception:
                        continue
        except Exception:
            return None
        return None

    def _load_night_consumption_samples(self, night_end_hour=None, max_files=900):
        """Lade echte Nachtverlaeufe fuer saisonale Verbrauchsprofile."""
        try:
            files = sorted(
                (fn for fn in os.listdir(HISTORY_DIR) if fn.startswith("history_") and fn.endswith(".txt")),
                reverse=True,
            )
        except Exception:
            files = []

        end_hour = self._safe_float(
            night_end_hour if night_end_hour is not None else self.morning_hour,
            self.morning_hour,
        )
        end_hour = max(4.0, min(10.0, end_hour))
        nights = []

        def _item_power(item, keys):
            for key in keys:
                if key in item:
                    return max(0.0, self._safe_float(item.get(key), 0.0))
            return 0.0

        for filename in files[:max_files]:
            path = os.path.join(HISTORY_DIR, filename)
            rows = []
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        dt = self._parse_history_timestamp(item.get("ts", item.get("_ts")))
                        if dt is None:
                            continue
                        hour = dt.hour + dt.minute / 60.0 + dt.second / 3600.0
                        if 0.0 <= hour <= end_hour + 0.15:
                            rows.append((dt, item))
            except Exception:
                continue
            if len(rows) < 20:
                continue

            rows.sort(key=lambda x: x[0])
            first_hour = rows[0][0].hour + rows[0][0].minute / 60.0
            last_hour = rows[-1][0].hour + rows[-1][0].minute / 60.0
            if first_hour > 0.35 or last_hour < end_hour - 0.35:
                continue

            home_kwh = 0.0
            wp_kwh = 0.0
            prev = None
            for dt, item in rows:
                if prev is None:
                    prev = (dt, item)
                    continue
                prev_dt, prev_item = prev
                delta_s = (dt - prev_dt).total_seconds()
                if 0.0 < delta_s <= 1800.0:
                    delta_h = delta_s / 3600.0
                    home_kwh += _item_power(prev_item, ("home", "Home_Power", "Consumption_W", "home_raw")) * delta_h / 1000.0
                    wp_kwh += _item_power(prev_item, ("wp", "WP_Power", "Leistungsaufnahme")) * delta_h / 1000.0
                prev = (dt, item)

            if home_kwh <= 0.1:
                continue
            nights.append({
                "date": rows[0][0].date(),
                "home_kwh": round(home_kwh, 3),
                "wp_kwh": round(wp_kwh, 3),
            })

        return nights, end_hour

    def _night_consumption_profile_from_samples(self, samples, end_hour, target_date, recent_days=10):
        """Saisonaler Nachtverbrauch: gleiche Jahreszeit bevorzugen, recent fallback."""
        if not samples:
            return None

        try:
            if isinstance(target_date, str):
                target_date = datetime.fromisoformat(target_date).date()
            elif isinstance(target_date, datetime):
                target_date = target_date.date()
        except Exception:
            target_date = datetime.now().date()

        def _day_distance(a, b):
            try:
                diff = abs(int(a.strftime("%j")) - int(b.strftime("%j")))
                return min(diff, 366 - diff)
            except Exception:
                return 366

        history = [
            n for n in samples
            if n.get("date") and n.get("date") < target_date
        ]
        if not history:
            return None

        seasonal_window_days = int(max(21, min(
            75,
            self._safe_float(getattr(self, "v4_config", {}).get("storage_night_season_window_days", 45), 45),
        )))
        seasonal = [
            n for n in history
            if _day_distance(n["date"], target_date) <= seasonal_window_days
        ]
        seasonal.sort(key=lambda n: (_day_distance(n["date"], target_date), -n["date"].toordinal()))

        if len(seasonal) >= 5:
            nights = seasonal[:45]
            source = "seasonal"
        else:
            history.sort(key=lambda n: n["date"], reverse=True)
            nights = history[:recent_days]
            source = "recent"

        if len(nights) < 3:
            return None

        def _percentile(values, q):
            ordered = sorted(float(v) for v in values)
            if not ordered:
                return 0.0
            idx = int(math.ceil(q * len(ordered))) - 1
            return ordered[max(0, min(len(ordered) - 1, idx))]

        home_values = [n["home_kwh"] for n in nights]
        wp_values = [n["wp_kwh"] for n in nights]
        home_median = _percentile(home_values, 0.50)
        wp_median = _percentile(wp_values, 0.50)
        home_p80 = _percentile(home_values, 0.80)
        wp_p80 = _percentile(wp_values, 0.80)
        return {
            "samples": len(nights),
            "source": source,
            "target_date": target_date.isoformat(),
            "season_window_days": seasonal_window_days,
            "night_end_hour": round(end_hour, 2),
            "home_median_kwh": round(home_median, 3),
            "wp_median_kwh": round(wp_median, 3),
            "home_p80_kwh": round(home_p80, 3),
            "wp_p80_kwh": round(wp_p80, 3),
            "home_cap_kwh": round(max(home_median * 1.12, home_median + 0.3), 3),
            "wp_cap_kwh": round(max(wp_median * 1.15, wp_median + 0.4), 3),
            "dates": [n["date"].isoformat() for n in nights],
        }

    def _recent_night_consumption_profile(self, days=10, night_end_hour=None, target_date=None):
        """Kompatibilitaetswrapper: liefert saisonale Nachtbasis mit Recent-Fallback."""
        samples, end_hour = self._load_night_consumption_samples(night_end_hour=night_end_hour)
        return self._night_consumption_profile_from_samples(
            samples,
            end_hour,
            target_date or datetime.now().date(),
            recent_days=days,
        )

    def _apply_night_consumption_sanity(self, timeline, now_dt):
        samples, default_end_hour = self._load_night_consumption_samples(night_end_hour=self.morning_hour)
        if not samples:
            return None

        by_day = {}
        for slot in timeline or []:
            try:
                dt = datetime.fromtimestamp(float(slot.get("ts", 0.0)) / 1000.0)
            except Exception:
                continue
            hour = dt.hour + dt.minute / 60.0
            end_hour = float(default_end_hour)
            if 0.0 <= hour < end_hour and dt.date() >= now_dt.date():
                by_day.setdefault(dt.date().isoformat(), []).append(slot)

        adjusted_days = 0
        min_home_factor = 1.0
        min_wp_factor = 1.0
        profiles = []
        for day, slots in by_day.items():
            profile = self._night_consumption_profile_from_samples(
                samples,
                default_end_hour,
                day,
            )
            if not profile:
                continue
            profiles.append(profile)
            end_hour = float(profile.get("night_end_hour", self.morning_hour) or self.morning_hour)
            home_cap_kwh = float(profile.get("home_cap_kwh", 0.0) or 0.0)
            wp_cap_kwh = float(profile.get("wp_cap_kwh", 0.0) or 0.0)
            if home_cap_kwh <= 0.0 and wp_cap_kwh <= 0.0:
                continue
            slots = [
                s for s in slots
                if 0.0 <= (
                    datetime.fromtimestamp(float(s.get("ts", 0.0)) / 1000.0).hour
                    + datetime.fromtimestamp(float(s.get("ts", 0.0)) / 1000.0).minute / 60.0
                ) < end_hour
            ]
            home_kwh = sum(max(0.0, float(s.get("home_w", 0.0) or 0.0)) * 0.25 for s in slots) / 1000.0
            wp_kwh = sum(max(0.0, float(s.get("wp_w", 0.0) or 0.0)) * 0.25 for s in slots) / 1000.0
            home_factor = 1.0
            wp_factor = 1.0
            if home_cap_kwh > 0.0 and home_kwh > home_cap_kwh:
                home_factor = max(0.35, min(1.0, home_cap_kwh / home_kwh))
            if wp_cap_kwh > 0.0 and wp_kwh > wp_cap_kwh:
                wp_factor = max(0.25, min(1.0, wp_cap_kwh / wp_kwh))
            if home_factor >= 0.999 and wp_factor >= 0.999:
                continue
            adjusted_days += 1
            min_home_factor = min(min_home_factor, home_factor)
            min_wp_factor = min(min_wp_factor, wp_factor)
            for slot in slots:
                slot["home_w"] = float(slot.get("home_w", 0.0) or 0.0) * home_factor
                slot["wp_w"] = float(slot.get("wp_w", 0.0) or 0.0) * wp_factor
                slot["night_consumption_sanity"] = True

        if not profiles:
            return None

        profile = profiles[0]
        profile.update({
            "adjusted_days": adjusted_days,
            "min_home_factor": round(min_home_factor, 3),
            "min_wp_factor": round(min_wp_factor, 3),
            "profile_days": [
                {
                    "date": p.get("target_date"),
                    "source": p.get("source"),
                    "samples": p.get("samples"),
                    "home_cap_kwh": p.get("home_cap_kwh"),
                    "wp_cap_kwh": p.get("wp_cap_kwh"),
                }
                for p in profiles[:4]
            ],
        })
        return profile

    def _load_quarter_emergency_profile(self, now_dt):
        rows = []
        parsed_files = 0
        try:
            files = sorted(
                (fn for fn in os.listdir(HISTORY_DIR) if fn.startswith("history_") and fn.endswith(".txt")),
                reverse=True,
            )
        except Exception:
            files = []

        for filename in files[:180]:
            path = os.path.join(HISTORY_DIR, filename)
            prev_dt = None
            prev_e_pv = None
            try:
                with open(path, "r", encoding="utf-8-sig") as f:
                    parsed_files += 1
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            item = json.loads(line)
                        except Exception:
                            continue
                        dt = self._parse_history_timestamp(item.get("ts", item.get("_ts")))
                        if dt is None:
                            continue

                        pv_w = None
                        for key in ("PV_Power", "pv_w", "pv", "solar_w", "solar", "Sonne"):
                            if key in item:
                                pv_w = max(0.0, self._safe_float(item.get(key), 0.0))
                                break

                        if pv_w is None and "e_pv" in item:
                            try:
                                e_pv = float(item.get("e_pv", 0) or 0)
                                if prev_dt is not None and prev_e_pv is not None:
                                    dt_h = max(1.0 / 3600.0, (dt - prev_dt).total_seconds() / 3600.0)
                                    delta_kwh = e_pv - prev_e_pv
                                    if 0 <= delta_kwh <= 5.0:
                                        pv_w = max(0.0, (delta_kwh * 1000.0) / dt_h)
                                prev_dt = dt
                                prev_e_pv = e_pv
                            except Exception:
                                pass
                        elif "e_pv" in item:
                            try:
                                prev_dt = dt
                                prev_e_pv = float(item.get("e_pv", 0) or 0)
                            except Exception:
                                pass

                        if pv_w is None:
                            continue
                        quarter = int((dt.month - 1) / 3) + 1
                        slot_key = dt.hour * 4 + int(dt.minute / 15)
                        rows.append((quarter, dt.date().isoformat(), slot_key, float(pv_w)))
            except Exception:
                continue

        current_quarter = int((now_dt.month - 1) / 3) + 1
        selected = [row for row in rows if row[0] == current_quarter]
        if len(selected) < 24:
            selected = rows

        slot_values = {}
        sample_days = set()
        for _q, day, slot_key, pv_w in selected:
            slot_values.setdefault(slot_key, []).append(pv_w)
            if pv_w > 50:
                sample_days.add(day)

        factor = max(0.35, min(1.0, self._safe_float(
            self.v4_config.get("storage_emergency_forecast_factor", 0.75), 0.75
        )))
        slot_power = {}
        for slot_key, values in slot_values.items():
            clean = sorted(v for v in values if v >= 0)
            if not clean:
                continue
            median = clean[len(clean) // 2]
            slot_power[slot_key] = max(0.0, median * factor)

        daily_kwh = sum(slot_power.values()) * 0.25 / 1000.0
        return {
            "quarter": current_quarter,
            "sample_days": len(sample_days),
            "parsed_files": parsed_files,
            "slot_power": slot_power,
            "peak_w": max(slot_power.values()) if slot_power else 0.0,
            "daily_kwh": daily_kwh,
            "history_factor": factor,
        }

    def _build_emergency_pv_timeline(self, start_dt, end_dt, slot_ms):
        now_dt = datetime.now()
        profile = self._load_quarter_emergency_profile(now_dt)
        peak_kw = self._configured_pv_peak_kw()
        rows = []
        ts = start_dt
        while ts < end_dt:
            ts_end = ts + timedelta(milliseconds=slot_ms)
            slot_key = ts.hour * 4 + int(ts.minute / 15)
            sun_w = self._clear_sky_pv_w(ts, peak_kw)
            hist_w = profile.get("slot_power", {}).get(slot_key)
            if hist_w is not None and profile.get("sample_days", 0) > 0:
                pv_w = max(0.0, (0.70 * float(hist_w)) + (0.30 * min(float(sun_w), max(float(profile.get("peak_w", 0)), 1.0))))
            else:
                pv_w = sun_w * 0.55
            rows.append({
                "start_timestamp": int(ts.timestamp() * 1000),
                "end_timestamp": int(ts_end.timestamp() * 1000),
                "predicted_kwh": round(max(0.0, pv_w) / 1000.0, 4),
                "fallback": "sun_history_quarter",
                "pv_forecast_fresh": False,
                "forecast_fresh": False,
                "pv_forecast_freshness_source": "emergency_curve_unverified",
            })
            ts = ts_end

        sunrise, solar_noon, sunset = self._solar_window_hours(now_dt.date())
        total_emergency_kwh = sum(r.get("predicted_kwh", 0) * 0.25 for r in rows)
        horizon_days = max(1.0, (end_dt - start_dt).total_seconds() / 86400.0)
        meta = {
            "forecast_source": "sun_history_quarter",
            "forecast_trust": "emergency",
            "forecast_confidence": 0.35 if profile.get("sample_days", 0) <= 0 else 0.55,
            "emergency_curve_active": True,
            "emergency_curve_reason": "PV-Prognose fehlt; Not-Ladekurve aus Sonnenstand und Quartalshistory",
            "emergency_curve_quarter": profile.get("quarter", 0),
            "emergency_curve_sample_days": profile.get("sample_days", 0),
            "emergency_curve_files": profile.get("parsed_files", 0),
            "emergency_curve_daily_kwh": round(total_emergency_kwh / horizon_days, 2),
            "emergency_curve_peak_w": round(max((r.get("predicted_kwh", 0) * 1000 for r in rows), default=0), 0),
            "emergency_noon_hour": round(float(solar_noon), 2),
            "emergency_sunrise_hour": round(float(sunrise), 2),
            "emergency_sunset_hour": round(float(sunset), 2),
            "emergency_peak_kwp": round(float(peak_kw), 2),
        }

        try:
            os.makedirs(os.path.dirname(EMERGENCY_CURVE_FILE), exist_ok=True)
            tmp = EMERGENCY_CURVE_FILE + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump({"meta": meta, "timeline": rows}, f)
            os.replace(tmp, EMERGENCY_CURVE_FILE)
        except Exception:
            pass

        logger.warning(
            "PV-Prognose fehlt: Not-Ladekurve aktiv (%s, %d History-Tage, %.1fkWh/Tag)."
            % (meta["forecast_source"], meta["emergency_curve_sample_days"], meta["emergency_curve_daily_kwh"])
        )
        return rows, meta

    def _check_awattar_full(self, timeline, current_soc, epex_tl):
        """
        Vollstaendige Python-Version von Eba's fHighprice() + CheckaWATTar().

        Nutzt unser timeline-Array (Ensemble-PV + ML-Verbrauch + EPEX-Preise)
        statt Eba's simple wetter_s Struct. Eba-Mapping:
          wetter[j].solar    -> timeline[j]['pv_w']   (3-Modell-Ensemble!)
          wetter[j].hourly   -> timeline[j]['home_w'] (ML-Prediction)
          wetter[j].wpbedarf -> timeline[j]['wp_w']
          w[j].pp/10         -> timeline[j]['marketprice']  (EUR/MWh)
          Octopus Heat       -> timeline[j]['billing_price_ct'] * 10
          ladeleistung * 0.9 -> charge_eta (konfig., default 0.90)

        Returncode (awattar_mode):
          0 = Entladen stoppen (Batterie fuer Preisberg schonen)
          1 = Normalbetrieb
          2 = Aus Netz laden (Preis gerade guenstig, SoC zu niedrig)
        """
        try:
            cfg = self.v4_config
            def _sf(k, d):
                try: return float(str(cfg.get(k, d) or d).replace(',', '.'))
                except: return float(d)

            bat_buy_limit = _sf('bat_buy_price_limit', -1.0)
            aw_diff       = _sf('aw_diff',       100.0)
            aw_aufschlag  = _sf('aw_aufschlag',  1.25)
            reserve_pct   = _sf('ep_reserve_pct', 8.0)
            charge_eta    = _sf('charge_eta',    0.90)   # Eba: ladeleistung * 0.9
            lookahead_h   = int(_sf('aw_lookahead_h', 24.0))
            lookahead_slots = lookahead_h * 4  # 15-Min-Raster
            tariff = str(cfg.get('stromtarif_typ', 'static')).strip().lower()
            use_billing_price = tariff in ('octopus_heat', 'special', 'spezial', 'special_tariff')
            price_unit = 'ct/kWh*10' if use_billing_price else 'EUR/MWh'

            def _slot_price(slot, default=0.0):
                if use_billing_price:
                    billing = slot.get('billing_price_ct')
                    if billing is not None and str(billing).strip() != '':
                        return float(billing) * 10.0
                return float(slot.get('marketprice', default) or default)

            if not timeline:
                return 1, 'timeline leer (Fallback)', 0.0

            now_ms = time.time() * 1000

            # Aktuellen Slot finden (Eba: Pos 'h')
            h = 0
            for i, slot in enumerate(timeline):
                if slot['ts'] >= now_ms:
                    h = i
                    break

            curr_price = _slot_price(timeline[h], 0.0)

            # Wenn keine EPEX-Preise vorhanden: Normal
            if curr_price == 0.0 and all(_slot_price(s, 0.0) == 0.0 for s in timeline[h:h+4]):
                return 1, 'keine Preiswerte in timeline', 0.0

            # Wenn Arbitrage deaktiviert: immer Normal
            if bat_buy_limit <= 0:
                return 1, 'bat_buy_price_limit nicht gesetzt', curr_price

            # --- fHighprice() Aequivalent (Eba awattar.cpp Z.177-248) ---
            # Berechne Energie-Bedarf (% SoC) in Slots mit hoeherem Preis als jetzt
            fConsumption_pct = 0.0
            pv_gain_pct      = 0.0   # Solarer Zugewinn bei Hochpreisslots (wie Eba x2)
            future = timeline[h+1 : h+1+lookahead_slots]
            prices_future = [_slot_price(s, 0.0) for s in future]
            min_future_price = min(prices_future) if prices_future else curr_price
            max_future_price = max(prices_future) if prices_future else curr_price

            for slot in future:
                price  = _slot_price(slot, 0.0)
                pv_w   = float(slot.get('pv_w',   0))
                home_w = float(slot.get('home_w', 0))
                wp_w   = float(slot.get('wp_w',   0))
                climate_w = float(slot.get('climate_w', 0))

                # Netto-Bedarf in diesem Slot (wie Eba: x3 = hourly + wpbedarf - solar)
                net_w = home_w + wp_w + climate_w + _slot_planned_load_w(slot) - pv_w

                if price > curr_price:
                    # Hochpreisslot: wie viel SoC wird verbraucht / gewonnen?
                    slot_wh  = net_w * (900.0 / 3600.0)       # 15min -> Wh
                    slot_pct = slot_wh / (self.capacity_wh / 100.0)  # Wh -> % SoC
                    if slot_pct > 0:
                        # Verbrauch bei Hochpreis (wie Eba: x1 += x3)
                        fConsumption_pct += slot_pct
                    else:
                        # PV-Ueberschuss bei Hochpreis (wie Eba: x2 -= x3)
                        pv_gain_pct += (-slot_pct)
                        if pv_gain_pct >= 100:
                            break   # Speicher voll, Abbruch wie Eba

            # PV-Gewinn verrechnen (wie Eba: if x2 > x3 -> x2 = x2 - x3)
            fConsumption_pct = max(0.0, fConsumption_pct - pv_gain_pct)

            # --- SoC-Bilanz (Eba: faval = fSoC - fConsumption) ---
            fSoC_eff = current_soc - reserve_pct
            faval = fSoC_eff - fConsumption_pct

            logger.debug('CheckaWATTar-Full: curr=%.1f %s SoC=%.1f%% '
                         'fConsumption=%.1f%% pv_gain=%.1f%% faval=%.1f%%' % (
                         curr_price, price_unit, current_soc, fConsumption_pct, pv_gain_pct, faval))

            # --- Entscheidungsbaum (exakt wie Eba) ---

            # 1. Genug SoC fuer alle Hochpreisphasen: Normal entladen (Eba return 1)
            if faval >= 0:
                reason = 'Normal: SoC%.1f%% deckt Hochpreisbedarf%.1f%% (faval=+%.1f)' % (
                    fSoC_eff, fConsumption_pct, faval)
                return 1, reason, curr_price

            # 2. Preisdifferenz gross genug fuer Eingriff? (Eba: SucheDiff)
            threshold = min_future_price * aw_aufschlag + aw_diff
            if curr_price <= threshold:
                # Kein ausreichender Spread -> Normal (Eba return 1)
                reason = 'Normal: Spread zu klein (curr=%.1f <= %.1f)' % (curr_price, threshold)
                return 1, reason, curr_price

            # 3. Spread gross genug: wieviel SoC fehlt?
            SollSoc = -faval   # Fehlende % SoC bis zur Deckung

            # Ladeverluste einrechnen (Eba: ladeleistung * 0.9 -> wir brauchen mehr als SollSoc)
            SollSoc_mit_verlust = SollSoc / charge_eta

            # 3a. Preis gerade guenstig UND SoC fehlt -> Netzladen (Eba return 2)
            if curr_price <= bat_buy_limit and SollSoc_mit_verlust > 0.5:
                reason = ('Netzladen: %.1f %s <= Limit %.1f | '
                          'SoC fehlt %.1f%% (mit Verlust %.1f%%)') % (
                    curr_price, price_unit, bat_buy_limit, SollSoc, SollSoc_mit_verlust)
                return 2, reason, curr_price

            # 3b. Preis gerade teuer, aber Tiefpunkt kommt -> Entladen stoppen (Eba return 0)
            # Pruefen ob wir genug SoC haben um bis zum Tiefpunkt zu halten
            hours_to_low = 0.0
            low_slot = min(future, key=lambda s: _slot_price(s, 9999.0), default=None)
            if low_slot:
                hours_to_low = max(0, (low_slot['ts'] - now_ms) / 3600000)

            # SoC-Verbrauch bis zum guenstigen Zeitpunkt (fuer "Halten reicht" Check)
            hold_consumption_pct = 0.0
            for slot in future:
                if _slot_price(slot, 9999.0) >= min_future_price * 1.5:
                    continue   # Ueberspringen bis guenstiger Slot
                net_w = (
                    float(slot.get('home_w', 0))
                    + float(slot.get('wp_w', 0))
                    + float(slot.get('climate_w', 0))
                    + _slot_planned_load_w(slot)
                    - float(slot.get('pv_w', 0))
                )
                hold_consumption_pct += max(0, net_w) * (900.0 / 3600.0) / (self.capacity_wh / 100.0)

            can_hold = (fSoC_eff - hold_consumption_pct) > (SollSoc + reserve_pct + 2)

            if can_hold:
                reason = ('Entladen stopp: %.1f %s hoch (min=%.1f in %.1fh) | '
                          'SoC%.1f%% halten fuer Preisberg %.1f%%') % (
                    curr_price, price_unit, min_future_price, hours_to_low, fSoC_eff, SollSoc)
                return 0, reason, curr_price

            # 4. Nichts davon passt -> Normal (Eba return 0 "kein Ergebnis")
            reason = ('Normal (kein Eingriff): Spread=%.1f..%.1f, faval=%.1f, '
                      'bat_buy_limit=%.1f') % (
                curr_price, min_future_price, faval, bat_buy_limit)
            return 1, reason, curr_price

        except Exception as e:
            logger.warning('_check_awattar_full Fehler: %s' % e)
            return 1, 'Fehler: %s' % e, 0.0

    def _cheap_grid_charge_plan(self, timeline, current_soc, target_timeline):
        """
        Preis-Boost: Netzladen nur so weit erlauben, dass die prognostizierte PV
        danach weiterhin in den Speicher passt. EPEX liefert nur das Zeitfenster;
        diese Funktion entscheidet die zulaessige SoC-Menge.
        """
        def _empty(reason):
            return {
                "active": False, "reason": reason, "target_soc": current_soc,
                "charge_w": 0, "hysteresis_pct": 0.5, "window_end": 0,
                "future_pv_wh": 0, "pv_space_target_soc": current_soc
            }

        try:
            if str(self.v4_config.get("cheap_grid_boost_enable", 0)).strip().lower() not in ("1", "true", "yes", "on"):
                return _empty("Preis-Boost deaktiviert")
            if not os.path.exists(PRICE_BOOST_PLAN_FILE):
                return _empty("kein price_boost_plan")
            plan = json.load(open(PRICE_BOOST_PLAN_FILE, encoding="utf-8"))
            if not plan.get("enabled") or not plan.get("active"):
                return _empty("kein aktives Billigfenster")
            if not plan.get("allow", {}).get("battery", True):
                return _empty("Batterie-Preisboost deaktiviert")

            active_window = plan.get("active_window") or {}
            now_ms = int(time.time() * 1000)
            win_start = int(active_window.get("start_timestamp", 0) or 0)
            win_end = int(active_window.get("end_timestamp", 0) or 0)
            if not (win_start <= now_ms < win_end):
                return _empty("Billigfenster nicht aktuell")

            limits = plan.get("limits", {})
            max_soc = min(
                self.target_soc,
                self._safe_float(limits.get("battery_max_soc", 80.0), 80.0)
            )
            pv_buffer_pct = self._safe_float(limits.get("pv_buffer_pct", 2.0), 2.0)
            hyst = max(0.2, self._safe_float(limits.get("soc_hysteresis_pct", 0.5), 0.5))
            user_max_w = self._safe_float(limits.get("battery_max_w", 0.0), 0.0)
            charge_w_max = min(self.max_charge_w, user_max_w) if user_max_w > 0 else self.max_charge_w

            if max_soc <= current_soc + hyst:
                return _empty("Max-SoC bereits erreicht")

            if target_timeline:
                day_end = int(target_timeline[-1].get("ts", now_ms + 86400000))
            else:
                day_end = now_ms + 86400000

            future_pv_wh = 0.0
            for slot in timeline:
                ts = int(slot.get("ts", 0) or 0)
                if ts < win_end or ts > day_end:
                    continue
                surplus_w = float(slot.get(
                    "surplus_w",
                    float(slot.get("pv_w", 0) or 0)
                    - float(slot.get("home_w", 0) or 0)
                    - float(slot.get("wp_w", 0) or 0)
                    - float(slot.get("climate_w", 0) or 0)
                    - _slot_planned_load_w(slot)
                ) or 0)
                future_pv_wh += max(0.0, surplus_w) * 0.25

            future_pv_pct = (future_pv_wh / max(1.0, self.capacity_wh)) * 100.0
            pv_space_target_soc = self.target_soc - future_pv_pct - pv_buffer_pct
            target_soc = min(max_soc, self.target_soc, max(current_soc, pv_space_target_soc))
            target_soc = round(max(0.0, min(100.0, target_soc)), 1)

            if target_soc <= current_soc + hyst:
                return {
                    "active": False,
                    "reason": (
                        "PV-Freiraum ausreichend: SoC %.1f%%, erlaubtes Netz-Ziel %.1f%%"
                        % (current_soc, target_soc)
                    ),
                    "target_soc": target_soc,
                    "charge_w": 0,
                    "hysteresis_pct": hyst,
                    "window_end": win_end,
                    "future_pv_wh": round(future_pv_wh, 0),
                    "pv_space_target_soc": round(pv_space_target_soc, 1),
                }

            reason = (
                "Preis-Boost aktiv: Ziel %.1f%%, danach %.0fWh PV-Freiraum reserviert"
                % (target_soc, future_pv_wh)
            )
            return {
                "active": True,
                "reason": reason,
                "target_soc": target_soc,
                "charge_w": int(max(300, charge_w_max)),
                "hysteresis_pct": hyst,
                "window_end": win_end,
                "future_pv_wh": round(future_pv_wh, 0),
                "pv_space_target_soc": round(pv_space_target_soc, 1),
                "price_limit_ct": plan.get("price_limit_ct"),
                "active_window": active_window,
            }
        except Exception as e:
            logger.warning("Preis-Boost Plan Fehler: %s" % e)
            return _empty("Fehler: %s" % e)

    @staticmethod
    def _copy_forecast_quantile_axis(target, source, axis):
        """Reicht nur explizite, bereits in Watt typisierte Quantile durch."""

        if not isinstance(target, dict) or not isinstance(source, dict):
            return
        fields = (
            "p10_w",
            "p50_w",
            "p90_w",
            "quantile_convention",
            "quantile_source",
            "quantile_revision",
            "quantile_fresh",
            "quantile_generated_ts_ms",
            "quantile_lead_time_bucket",
            "quantile_lead_time_min_minutes",
            "quantile_lead_time_max_minutes",
            "quantile_calibration_status",
            "quantile_calibration_method",
            "quantile_calibration_revision",
            "quantile_calibration_sample_count",
            "quantile_calibration_day_count",
            "quantile_calibration_window_start_ts_ms",
            "quantile_calibration_window_end_ts_ms",
            "quantile_decision_use_allowed",
        )
        for suffix in fields:
            key = f"{axis}_{suffix}"
            if key in source:
                target[key] = source.get(key)

    @staticmethod
    def _copy_slot_bound_forecast_evidence(
        target,
        source,
        key,
        slot_start_ts_ms,
        slot_end_ts_ms,
    ):
        """Kopiert vorhandene Forecast-Evidenz nur für denselben exakten Slot."""

        if not isinstance(target, dict) or not isinstance(source, dict):
            return False
        if key not in source:
            return False
        if (
            source.get("start_timestamp") != slot_start_ts_ms
            or source.get("end_timestamp") != slot_end_ts_ms
        ):
            return False
        target[key] = copy.deepcopy(source[key])
        return True

    @staticmethod
    def _bind_forecast_slot_topology(slot, forecast_slot):
        """Materialisiert Split oder typisierte Missingness für jeden PV-Slot."""

        status = str(forecast_slot.get("pv_topology_status") or "topology_unbound")
        reason = str(forecast_slot.get("pv_topology_reason") or "SLOT_TOPOLOGY_MISSING")
        dc_w = forecast_slot.get("e3dc_dc_pv_w")
        external_w = forecast_slot.get("external_ac_pv_w")
        if status == "bound" and (dc_w is None or external_w is None):
            status = "topology_unbound"
            reason = "RESOURCE_PROJECTION_INCOMPLETE"
        slot["pv_topology_status"] = status
        slot["pv_topology_reason"] = reason
        slot["pv_topology_revision"] = forecast_slot.get("pv_topology_revision")
        slot["pv_topology_source"] = str(
            forecast_slot.get("pv_topology_source") or "pv_forecast_slot"
        )
        slot["pv_topology_quality"] = str(
            forecast_slot.get("pv_topology_quality")
            or ("complete" if status == "bound" else "missing_or_incoherent_resource_projection")
        )
        slot["pv_resource_projection_status"] = str(
            forecast_slot.get("pv_resource_projection_status")
            or ("complete" if status == "bound" else "unbound")
        )
        slot["pv_resource_projection_reason"] = str(
            forecast_slot.get("pv_resource_projection_reason") or reason
        )
        if "pv_forecast_fresh" in forecast_slot or "forecast_fresh" in forecast_slot:
            fresh_value = (
                forecast_slot.get("pv_forecast_fresh")
                if "pv_forecast_fresh" in forecast_slot
                else forecast_slot.get("forecast_fresh")
            )
            slot["pv_forecast_fresh"] = fresh_value is True
            slot["forecast_fresh"] = fresh_value is True
        if "pv_forecast_freshness_source" in forecast_slot:
            slot["pv_forecast_freshness_source"] = str(
                forecast_slot.get("pv_forecast_freshness_source")
                or "model_provenance_unknown"
            )
        slot["e3dc_dc_pv_w"] = dc_w if status == "bound" else None
        slot["external_ac_pv_w"] = external_w if status == "bound" else None
        slot["pv_resource_contributions"] = (
            forecast_slot.get("pv_resources")
            if isinstance(forecast_slot.get("pv_resources"), list)
            else []
        )
        slot["pv_external_ac_capped_w"] = forecast_slot.get("pv_external_ac_capped_w", 0.0)
        StorageSimulator._copy_forecast_quantile_axis(
            slot,
            forecast_slot,
            "e3dc_dc_pv",
        )
        StorageSimulator._copy_forecast_quantile_axis(
            slot,
            forecast_slot,
            "external_ac_pv",
        )

    def generate_plan(self):
        # Update Hardware-Params vor jedem Lauf
        self._update_params_from_live()
        forecast_only_curve = self._forecast_only_curve_enabled()
        if forecast_only_curve:
            self.target_soc = 100.0

        logger.info(f"Generiere V4 Fahrplan ({self.capacity_kwh:.1f} kWh Akku, {self.max_charge_w:.0f} W Limit)...")

        current_soc = self.get_live_soc()
        if current_soc is None:
            return False
        storm_guard = self._storm_empty("Unwetterwächter noch nicht bewertet")
        storm_grid_charge = self._storm_grid_empty(current_soc)

        # Lade externe Arrays
        pv_tl = []
        pv_source_meta = {}
        if os.path.exists(PV_ENV_FILE):
            try:
                with open(PV_ENV_FILE, 'r', encoding='utf-8-sig') as f: pv_tl = json.load(f)
                if not isinstance(pv_tl, list):
                    pv_tl = []
            except Exception as e:
                logger.error(f"Fehler beim Laden von {PV_ENV_FILE}: {e}")
        else:
            logger.warning(f"Datei nicht gefunden: {PV_ENV_FILE}")
        if os.path.exists(PV_META_FILE):
            try:
                with open(PV_META_FILE, 'r', encoding='utf-8-sig') as f:
                    _pv_source_meta = json.load(f)
                if isinstance(_pv_source_meta, dict):
                    pv_source_meta = _pv_source_meta
            except Exception as e:
                logger.warning(f"PV-Metadaten nicht nutzbar ({PV_META_FILE}): {e}")
        pv_forecast_missing = not bool(pv_tl)
        forecast_meta = {
            "forecast_source": "pv_forecast",
            "forecast_trust": "forecast",
            # Ein vorhandener Punktforecast ist keine kalibrierte
            # Eintrittswahrscheinlichkeit. Fehlende Kalibrierung bleibt
            # typisierte Missingness statt erfundener 100-%-Sicherheit.
            "forecast_confidence": None,
            "forecast_confidence_status": "evidence_limit",
            "emergency_curve_active": False,
            "pv_topology": pv_source_meta.get("pv_topology")
            if isinstance(pv_source_meta.get("pv_topology"), dict)
            else self.pv_topology_contract,
        }

        home_tl = []; wp_tl = []
        ml_data = {}
        ml_tl = []
        if os.path.exists(ML_PRED_FILE):
            try:
                with open(ML_PRED_FILE, 'r', encoding='utf-8-sig') as f:
                    ml_data = json.load(f)
                    if not isinstance(ml_data, dict):
                        ml_data = {}
                    ml_tl = ml_data.get('timeline', [])
            except Exception as e:
                logger.error(f"Fehler beim Laden von {ML_PRED_FILE}: {e}")
        else:
            logger.error(f"Datei nicht gefunden: {ML_PRED_FILE}")

        # ML-Prognose ist OPTIONAL: liefert Hausverbrauch + WP-Verbrauch
        ml_available = bool(ml_tl)
        if pv_forecast_missing:
            logger.warning(f"Keine PV-Prognose gefunden (Slots={len(pv_tl)}). Nutze Not-Ladekurve.")
        if not ml_tl:
            logger.info(f"Keine ML-Prognose ({ML_PRED_FILE}). Nutze konservativen Verbrauchs-Fallback (M1).")

        # M1: Konservativer Fallback fuer fehlende ML-Slots.
        # ml_prediction.json deckt je nach Datenlage nicht immer die vollen 72h ab.
        # Fehlt ein Slot, wird zuerst das letzte bekannte Tagesprofil zur gleichen
        # 15-Minuten-Uhrzeit genutzt, danach eine vorsichtige Grundlast.
        _cfg_tmp = self._load_v4_config()
        try:
            _wp_type = int(_cfg_tmp.get('wp_type', 0))
        except Exception:
            _wp_type = 0
        _fallback_home_w = 500.0
        _fallback_wp_w = 300.0 if _wp_type > 0 else 0.0

        consumption_forecast_meta = {
            "schema_version": ml_data.get("schema_version") if ml_available else None,
            "forecast_mode": ml_data.get("forecast_mode") if ml_available else "static_m1_fallback",
            "model_ready": bool(ml_data.get("model_ready")) if ml_available else False,
            "model_reason": ml_data.get("model_reason") if ml_available else "ml_prediction_missing",
            "generated_at": ml_data.get("ts") if ml_available else None,
            "history_profile": ml_data.get("history_profile") if ml_available else None,
            "consumer_sources": ml_data.get("consumer_sources") if ml_available else {
                "home": {"sources": ["static_m1_fallback"], "quality": ["fallback"]},
                "wp": {
                    "sources": ["static_m1_fallback" if _wp_type > 0 else "not_applicable"],
                    "quality": ["fallback" if _wp_type > 0 else "not_applicable"],
                },
                "climate": {"sources": ["not_applicable"], "quality": ["not_applicable"]},
                "wallbox": {
                    "sources": ["excluded_dynamic_without_explicit_plan"],
                    "quality": ["not_applicable"],
                },
                "heating_element": {
                    "sources": ["included_in_home_or_explicit_planned_load"],
                    "quality": ["no_dedicated_history_series"],
                },
                "domestic_hot_water_heatpump": {
                    "sources": ["included_in_home_or_explicit_planned_load"],
                    "quality": ["no_dedicated_history_series"],
                },
            },
        }

        ml_profile = {}
        if ml_available:
            for m in ml_tl:
                try:
                    ts_m = float(m.get("start_timestamp", 0)) / 1000.0
                    dt_m = datetime.fromtimestamp(ts_m)
                    slot_key = dt_m.hour * 4 + int(dt_m.minute / 15)
                    home_w = float(m.get("home_kwh", 0) or 0) * 1000.0
                    wp_w = float(m.get("wp_kwh", 0) or 0) * 1000.0
                    climate_w = float(m.get("climate_kwh", 0) or 0) * 1000.0
                    if home_w <= 0 and wp_w <= 0 and climate_w <= 0:
                        continue
                    ml_profile.setdefault(slot_key, {
                        "home": [], "wp": [], "climate": [],
                        "home_source": [], "home_quality": [],
                        "wp_source": [], "wp_quality": [],
                        "climate_source": [], "climate_quality": [],
                    })
                    ml_profile[slot_key]["home"].append(home_w)
                    ml_profile[slot_key]["wp"].append(wp_w)
                    ml_profile[slot_key]["climate"].append(climate_w)
                    for field in (
                        "home_source", "home_quality",
                        "wp_source", "wp_quality",
                        "climate_source", "climate_quality",
                    ):
                        if m.get(field) not in (None, ""):
                            ml_profile[slot_key][field].append(str(m.get(field)))
                except Exception:
                    continue

        ml_profile_avg = {}
        for slot_key, values in ml_profile.items():
            homes = values.get("home") or []
            wps = values.get("wp") or []
            climates = values.get("climate") or []
            ml_profile_avg[slot_key] = {
                "home_w": sum(homes) / len(homes) if homes else _fallback_home_w,
                "wp_w": sum(wps) / len(wps) if wps else _fallback_wp_w,
                "climate_w": sum(climates) / len(climates) if climates else 0.0,
                "home_source": "profile_extension:" + str(
                    (values.get("home_source") or [consumption_forecast_meta["forecast_mode"]])[0]
                ),
                "home_quality": "profile_extension:" + str(
                    (values.get("home_quality") or ["fallback"])[0]
                ),
                "wp_source": "profile_extension:" + str(
                    (values.get("wp_source") or ["not_applicable"])[0]
                ),
                "wp_quality": "profile_extension:" + str(
                    (values.get("wp_quality") or ["not_applicable"])[0]
                ),
                "climate_source": "profile_extension:" + str(
                    (values.get("climate_source") or ["not_applicable"])[0]
                ),
                "climate_quality": "profile_extension:" + str(
                    (values.get("climate_quality") or ["not_applicable"])[0]
                ),
            }

        if not ml_available:
            logger.info(f"  M1-Fallback: home={_fallback_home_w:.0f}W, wp={_fallback_wp_w:.0f}W (wp_type={_wp_type})")

        # Kurzfrist-Korrektur: Die ML-Verbrauchsprognose darf die naechsten
        # Stunden nicht deutlich ueber der realen Hauslast liegen. Sonst kappt
        # der Simulator an sonnigen Tagen die Ladekurve zu frueh und der
        # Storage Manager folgt einem kuenstlich zu niedrigen Abendziel.
        _live_home_w = None
        _live_pv_w = None
        _live_grid_w = 0.0
        _live_derate_w = 0.0
        _live_external_ac_w = None
        _live_e3dc_dc_limit_w = 0.0
        _live_e3dc_dc_limit_source = "unavailable"
        _live_power_limits_active = False
        _live_ts = 0.0
        _live_d = {}
        try:
            if os.path.exists(LIVE_DATA_FILE):
                with open(LIVE_DATA_FILE, 'r', encoding='utf-8-sig') as f:
                    _live_d = json.load(f)
                _live_ts = float(_live_d.get('_ts', _live_d.get('ts', 0)) or 0)
                _home_live = _live_d.get('Home_Power', _live_d.get('Consumption_W', None))
                if _home_live is not None:
                    _live_home_w = max(250.0, float(_home_live or 0.0))
                _pv_live = _live_d.get('PV_Power', _live_d.get('pv_power', None))
                if _pv_live is not None:
                    _live_pv_w = max(0.0, float(_pv_live or 0.0))
                _live_grid_w = float(_live_d.get('Grid_Power', _live_d.get('grid_power', 0)) or 0)
                _live_derate_w = max(0.0, float(_live_d.get('derate_at_power_w', 0) or 0))
                if _live_d.get('Ext_PV_Power_Valid') is True:
                    _ext_live = max(0.0, float(_live_d.get('Ext_PV_Power', 0) or 0))
                    if _live_pv_w is not None and _ext_live <= _live_pv_w + 5.0:
                        _live_external_ac_w = min(_ext_live, _live_pv_w)
                _dc_limit_parts = []
                for _dc_index in range(4):
                    _dc_limit_part = max(0.0, float(_live_d.get(f'dc{_dc_index}_max_w', 0) or 0))
                    if _dc_limit_part > 0.0:
                        _dc_limit_parts.append(_dc_limit_part)
                if _dc_limit_parts:
                    _live_e3dc_dc_limit_w = sum(_dc_limit_parts)
                    _live_e3dc_dc_limit_source = "live:pvi_dc_max_power_sum"
                _live_power_limits_active = self._cfg_bool(
                    _live_d.get('power_limits_active', _live_d.get('ems_derating_active', False)),
                    False,
                )
        except Exception:
            _live_home_w = None
            _live_pv_w = None
            _live_grid_w = 0.0
            _live_derate_w = 0.0
            _live_external_ac_w = None
            _live_e3dc_dc_limit_w = 0.0
            _live_e3dc_dc_limit_source = "unavailable"
            _live_power_limits_active = False
            _live_ts = 0.0
            _live_d = {}

        if _live_e3dc_dc_limit_w <= 0.0:
            _configured_dc_limit_w = self._safe_float(
                (self.pv_topology_contract.get("limits_w") or {}).get("e3dc_dc_configured"),
                0.0,
            )
            if _configured_dc_limit_w > 0.0:
                _live_e3dc_dc_limit_w = _configured_dc_limit_w
                _live_e3dc_dc_limit_source = "config:pv_e3dc_dc_inverter_limit_w"

        try:
            _configured_ep_reserve_soc = self._safe_float(self.v4_config.get("ep_reserve_pct", 8.0), 8.0)
            _ep_reserve_floor_soc = effective_ep_reserve_pct(
                self.v4_config,
                _live_d,
                default=_configured_ep_reserve_soc,
            )
        except Exception as _ep_reserve_err:
            logger.warning("Notstromreserve-Floor konnte nicht normalisiert werden: %s" % _ep_reserve_err)
            _ep_reserve_floor_soc = self._safe_float(self.v4_config.get("ep_reserve_pct", 8.0), 8.0)
        _ep_reserve_floor_soc = max(0.0, min(100.0, float(_ep_reserve_floor_soc)))
        _start_anchor_min_soc = max(
            _ep_reserve_floor_soc,
            0.0 if forecast_only_curve else float(self.morning_soc),
        )

        epex_tl = []
        if os.path.exists(EPEX_DATA_FILE):
            try:
                with open(EPEX_DATA_FILE, 'r', encoding='utf-8') as f:
                    epex_tl = json.load(f)
            except Exception as e:
                logger.error(f"Fehler beim Laden von {EPEX_DATA_FILE}: {e}")

        eco_tl = []
        if os.path.exists(ECO_SCORE_FILE):
            try:
                with open(ECO_SCORE_FILE, 'r', encoding='utf-8') as f:
                    eco_tl = json.load(f)
            except Exception as e:
                logger.error(f"Fehler beim Laden von {ECO_SCORE_FILE}: {e}")

        # --- 1. Zeitstrahl ab voller Stunde aufbauen (15 Minuten Raster) ---
        now = datetime.now()
        # Start: aktuelle volle Stunde
        start_dt = now.replace(minute=0, second=0, microsecond=0)
        # Ende: immer bis Ende des 3. Tages (=3 volle Folgetage)
        # So haben wir immer mindestens ~72h Prognose, egal zu welcher Stunde
        tonight_midnight = (start_dt + timedelta(days=1)).replace(hour=0, minute=0, second=0)
        end_dt = tonight_midnight + timedelta(hours=72)  # Volle 3 Tage ab Mitternacht
        start_ms = start_dt.timestamp() * 1000
        end_ms = end_dt.timestamp() * 1000

        # Heutiges Datum als String fuer Dump-Lock (robust gegen Float/DST-Jitter)
        today_date_str = now.date().isoformat()  # z.B. "2026-04-26"

        slot_ms = 900 * 1000  # 15 Minuten in Millisekunden
        n_slots = int((end_ms - start_ms) / slot_ms)

        pv_forecast_unusable = pv_forecast_missing
        if not pv_forecast_missing:
            pv_forecast_unusable = not any(
                self._safe_float(p.get("end_timestamp", 0), 0.0) > start_ms
                and self._safe_float(p.get("start_timestamp", 0), 0.0) < end_ms
                for p in pv_tl
                if isinstance(p, dict)
            )
            if pv_forecast_unusable:
                logger.warning("PV-Prognose enthaelt keine nutzbaren Zukunftsslots. Nutze Not-Ladekurve.")

        if pv_forecast_unusable:
            pv_tl, forecast_meta = self._build_emergency_pv_timeline(start_dt, end_dt, slot_ms)
            if (not forecast_only_curve) and self.noon_target_soc <= 0:
                emergency_noon_soc = max(0.0, min(100.0, self._safe_float(
                    self.v4_config.get("storage_emergency_noon_target_soc", 80.0), 80.0
                )))
                if emergency_noon_soc > 0:
                    self.noon_target_soc = min(float(self.target_soc), max(float(self.morning_soc), emergency_noon_soc))
                    self.noon_hour = float(forecast_meta.get("emergency_noon_hour", self.noon_hour))
                    forecast_meta["emergency_noon_anchor_injected"] = True
                    forecast_meta["emergency_noon_target_soc"] = round(float(self.noon_target_soc), 1)

        timeline = []
        for offset in range(n_slots):
            ts_start = start_ms + (offset * slot_ms)
            timeline.append({
                "ts": ts_start, "pv_w": 0, "home_w": 0, "wp_w": 0, "climate_w": 0,
                "home_source": "unresolved", "home_quality": "missing",
                "wp_source": "unresolved", "wp_quality": "missing",
                "climate_source": "unresolved", "climate_quality": "missing",
                "surplus_w": 0, "charge_w": 0, "soc": 0,
                "marketprice": 0.0, "billing_price_ct": None,
                "optimization_score": None, "pure_eco_score": None,
                "price_available": False, "price_fresh": False,
                "price_stale": True, "price_status": "source_interval_missing",
                "pv_forecast_fresh": False, "forecast_fresh": False,
                "pv_forecast_freshness_source": "source_interval_missing",
                "eco_score_available": False,
                "grid_dump_w": 0,
                "predump_candidate_w": 0,
                "predump_candidate": False,
                "predump_selected": False,
                "predump_executable": False,
                "predump_commands_allowed": False,
                "predump_status": "none",
                "predump_block_reason": None
            })

        def _refresh_slot_energy(slot):
            slot["surplus_w"] = _slot_net_surplus_w(slot)
            if slot["surplus_w"] < 0:
                slot["charge_w"] = max(slot["surplus_w"], -self.max_discharge_w)
            else:
                slot["charge_w"] = min(slot["surplus_w"], self.max_charge_w)

        # --- 2. Daten mappen ---
        _live_home_clamped_slots = 0
        _ml_direct_slots = 0
        _ml_profile_slots = 0
        _ml_base_slots = 0
        for slot in timeline:
            ts = slot["ts"]

            # Interpoliere PV:
            # predicted_kwh aus dem Ensemble-Forecast ist bereits mittlere Leistung in kW
            # (nicht Energie! Siehe pv_forecast_service.py: slotted_kw wird als predicted_kwh gespeichert)
            # => einfach * 1000 fuer Watt
            for p in pv_tl:
                if ts >= p["start_timestamp"] and ts < p["end_timestamp"]:
                    slot["pv_w"] = p.get("predicted_kwh", 0) * 1000.0
                    _pv_fresh = (
                        p.get("pv_forecast_fresh")
                        if "pv_forecast_fresh" in p
                        else p.get("forecast_fresh")
                    )
                    slot["pv_forecast_fresh"] = _pv_fresh is True
                    slot["forecast_fresh"] = _pv_fresh is True
                    slot["pv_forecast_freshness_source"] = str(
                        p.get("pv_forecast_freshness_source")
                        or "model_provenance_missing"
                    )
                    self._copy_slot_bound_forecast_evidence(
                        slot,
                        p,
                        "pv_zero_evidence",
                        ts,
                        ts + slot_ms,
                    )
                    self._copy_forecast_quantile_axis(
                        slot,
                        p,
                        "pv",
                    )
                    if self.pv_topology_contract.get("split_usable") or any(
                        key in p
                        for key in (
                            "pv_topology_status",
                            "pv_topology_revision",
                            "e3dc_dc_pv_w",
                            "external_ac_pv_w",
                        )
                    ):
                        self._bind_forecast_slot_topology(slot, p)
                    break

            # Interpoliere ML (Home / WP / Klima):
            ml_matched = False
            for m in ml_tl:
                if ts >= m["start_timestamp"] and ts < m["end_timestamp"]:
                    slot["home_w"] = m.get("home_kwh", 0) * 1000.0
                    slot["wp_w"] = m.get("wp_kwh", 0) * 1000.0
                    slot["climate_w"] = m.get("climate_kwh", 0) * 1000.0
                    slot["home_source"] = m.get("home_source", ml_data.get("forecast_mode", "unknown"))
                    slot["home_quality"] = m.get("home_quality", "unknown")
                    slot["wp_source"] = m.get("wp_source", ml_data.get("forecast_mode", "unknown"))
                    slot["wp_quality"] = m.get("wp_quality", "unknown")
                    slot["climate_source"] = m.get("climate_source", "not_applicable")
                    slot["climate_quality"] = m.get("climate_quality", "not_applicable")
                    self._copy_forecast_quantile_axis(
                        slot,
                        m,
                        "load",
                    )
                    ml_matched = True
                    _ml_direct_slots += 1
                    break

            # M1: Fallback anwenden wenn fuer diesen Slot keine ML-Prognose vorhanden ist.
            if not ml_matched:
                dt_slot = datetime.fromtimestamp(ts / 1000.0)
                slot_key = dt_slot.hour * 4 + int(dt_slot.minute / 15)
                prof = ml_profile_avg.get(slot_key)
                if prof:
                    slot["home_w"] = prof["home_w"]
                    slot["wp_w"] = prof["wp_w"]
                    slot["climate_w"] = prof["climate_w"]
                    for field in (
                        "home_source", "home_quality",
                        "wp_source", "wp_quality",
                        "climate_source", "climate_quality",
                    ):
                        slot[field] = prof[field]
                    _ml_profile_slots += 1
                else:
                    slot["home_w"] = _fallback_home_w
                    slot["wp_w"] = _fallback_wp_w
                    slot["climate_w"] = 0.0
                    slot["home_source"] = "static_m1_fallback"
                    slot["home_quality"] = "fallback"
                    slot["wp_source"] = "static_m1_fallback" if _fallback_wp_w > 0.0 else "not_applicable"
                    slot["wp_quality"] = "fallback" if _fallback_wp_w > 0.0 else "not_applicable"
                    slot["climate_source"] = "not_applicable"
                    slot["climate_quality"] = "not_applicable"
                    _ml_base_slots += 1

            if _live_home_w is not None and _live_ts > 0:
                # Nur sehr kurzfristig korrigieren. Die weitere Tagesplanung
                # soll weiterhin aus ML/History kommen.
                try:
                    _slot_delta_s = (float(ts) / 1000.0) - time.time()
                    if 0 <= _slot_delta_s <= 2.0 * 3600.0:
                        _home_cap_w = max(500.0, _live_home_w * 1.45, _live_home_w + 700.0)
                        if slot["home_w"] > _home_cap_w:
                            slot["home_w"] = _home_cap_w
                            _live_home_clamped_slots += 1
                except Exception:
                    pass

            # Interpoliere EPEX (Marketprice):
            for e in epex_tl:
                if ts >= e["start_timestamp"] and ts < e["end_timestamp"]:
                    slot["marketprice"] = float(e.get("marketprice", 0.0))
                    _price_stale = e.get("price_stale") is True
                    _price_available = e.get("price_available", True) is True
                    _price_fresh = e.get("price_fresh", not _price_stale) is True
                    slot["price_available"] = bool(_price_available)
                    slot["price_fresh"] = bool(_price_fresh and not _price_stale)
                    slot["price_stale"] = bool(_price_stale or not _price_fresh)
                    slot["price_status"] = (
                        "source_interval_match"
                        if slot["price_available"] and slot["price_fresh"]
                        else "source_interval_invalid"
                    )
                    for key in (
                        "price_source",
                        "tariff_provider",
                        "price_resolution_min",
                        "source_resolution_min",
                        "direct_marketing_marketprice",
                        "direct_marketing_market_price_ct",
                        "direct_marketing_price_source",
                        "direct_marketing_price_resolution_min",
                        "direct_marketing_source_resolution_min",
                        "direct_marketing_price_revision",
                        "direct_marketing_price_revision_source",
                        "direct_marketing_price_available",
                    ):
                        if key in e:
                            slot[key] = e[key]
                    break

            # Endkundenpreis fuer Octopus Heat: LT/HT/UHT steuert die Speicher-Arbitrage.
            for score in eco_tl:
                if ts >= score["start_timestamp"] and ts < score["end_timestamp"]:
                    slot["eco_score_available"] = True
                    _score_stale = score.get("price_stale") is True
                    _score_available = score.get("price_available", True) is True
                    _score_fresh = score.get("price_fresh", not _score_stale) is True
                    slot["price_available"] = bool(_score_available)
                    slot["price_fresh"] = bool(_score_fresh and not _score_stale)
                    slot["price_stale"] = bool(_score_stale or not _score_fresh)
                    slot["price_status"] = (
                        "billing_interval_match"
                        if slot["price_available"] and slot["price_fresh"]
                        else "billing_interval_invalid"
                    )
                    billing_price = score.get("billing_price")
                    if billing_price is not None:
                        slot["billing_price_ct"] = float(billing_price)
                    optimization_score = score.get("optimization_score")
                    if optimization_score is not None:
                        slot["optimization_score"] = float(optimization_score)
                    pure_eco_score = score.get("pure_eco_score")
                    if pure_eco_score is not None:
                        slot["pure_eco_score"] = float(pure_eco_score)
                    break

            # Netto PV Überschuss
            _refresh_slot_energy(slot)

        planned_load_meta = apply_planned_loads_to_timeline(timeline, self.v4_config)
        for slot in timeline:
            _refresh_slot_energy(slot)

        night_consumption_profile = self._apply_night_consumption_sanity(timeline, now)
        if night_consumption_profile:
            forecast_meta["night_consumption_sanity"] = {
                k: v for k, v in night_consumption_profile.items()
                if k != "dates"
            }
            if int(night_consumption_profile.get("adjusted_days", 0) or 0) > 0:
                logger.info(
                    "Nachtverbrauchs-Sanity: %d Tag(e) gegen echte Nachtbasis begrenzt "
                    "(00-%.1fh, Haus Median %.1fkWh, WP Median %.1fkWh, Faktor min. Haus x%.2f/WP x%.2f)."
                    % (
                        int(night_consumption_profile.get("adjusted_days", 0) or 0),
                        float(night_consumption_profile.get("night_end_hour", self.morning_hour) or self.morning_hour),
                        float(night_consumption_profile.get("home_median_kwh", 0.0) or 0.0),
                        float(night_consumption_profile.get("wp_median_kwh", 0.0) or 0.0),
                        float(night_consumption_profile.get("min_home_factor", 1.0) or 1.0),
                        float(night_consumption_profile.get("min_wp_factor", 1.0) or 1.0),
                    )
                )
            apply_planned_loads_to_timeline(timeline, self.v4_config)
            for slot in timeline:
                _refresh_slot_energy(slot)

        if ml_available:
            logger.info(
                f"  ML-Verbrauchsprofil: direkt={_ml_direct_slots}/{len(timeline)} Slots, "
                f"Tagesprofil-Ergaenzung={_ml_profile_slots}, M1-Grundlast={_ml_base_slots}."
            )

        if _live_home_clamped_slots:
            logger.info(
                "Live-Home-Korrektur: %d nahe Slots auf %.0fW-Basis begrenzt."
                % (_live_home_clamped_slots, _live_home_w or 0.0)
            )
        if planned_load_meta.get("enabled"):
            logger.info(
                "Geplante Lasten: %.0fWh in %d Slots eingeplant (%d Fenster, %d verworfen)."
                % (
                    float(planned_load_meta.get("total_wh", 0) or 0),
                    int(planned_load_meta.get("active_slots", 0) or 0),
                    len(planned_load_meta.get("windows") or []),
                    len(planned_load_meta.get("rejected") or []),
                )
            )

        # --- 3. V4 Intelligenz: Peak-Shaving & Sunset-Targeting für volle 3-4 Tage ---
        midnight_today = now.replace(hour=0, minute=0, second=0, microsecond=0).timestamp() * 1000
        days = [
            midnight_today,
            midnight_today + 86400000,
            midnight_today + 172800000,
            midnight_today + 259200000
        ]

        def _curve_day_index(day_ms):
            try:
                return max(0, int(round((float(day_ms) - float(midnight_today)) / 86400000.0)))
            except Exception:
                return 0

        def _curve_day_label(day_ms):
            idx = _curve_day_index(day_ms)
            if idx == 0:
                return "Heute"
            if idx == 1:
                return "Morgen"
            return f"In {idx} Tagen"

        def _curve_day_date(day_ms):
            try:
                return datetime.fromtimestamp(float(day_ms) / 1000.0).date().isoformat()
            except Exception:
                return ""

        if forecast_only_curve:
            self.target_soc = 100.0
        config_target_soc = float(self.target_soc)
        planning_target_soc = config_target_soc
        wallbox_target_soc, wallbox_target_intent = (
            (None, {"storage_curve_target_mode": "forecast_100"})
            if forecast_only_curve
            else self._wallbox_target_curve_soc(config_target_soc)
        )
        wallbox_floor_soc_active = (
            (not forecast_only_curve)
            and wallbox_target_soc is not None
            and wallbox_target_soc < config_target_soc - 0.1
        )
        _config_target_reachable = True
        _config_target_reach_meta = {}
        if wallbox_floor_soc_active:
            try:
                today_end_ms = midnight_today + 86400000
                future_today_slots = [
                    s for s in timeline
                    if float(s.get("ts", 0)) >= start_ms and float(s.get("ts", 0)) < today_end_ms
                ]
                future_surplus_wh = sum(
                    min(float(self.max_charge_w), max(0.0, float(s.get("surplus_w", 0.0)))) * 0.25
                    for s in future_today_slots
                )
                need_wh = self.capacity_wh * max(0.0, config_target_soc - float(current_soc)) / 100.0
                needed_with_margin_wh = need_wh * 1.10
                _config_target_reachable = (
                    float(current_soc) >= config_target_soc - 0.2
                    or future_surplus_wh >= needed_with_margin_wh
                )
                _config_target_reach_meta = {
                    "future_surplus_wh": round(future_surplus_wh, 0),
                    "needed_wh": round(need_wh, 0),
                    "needed_with_margin_wh": round(needed_with_margin_wh, 0),
                    "config_target_soc": round(config_target_soc, 1),
                    "current_soc": round(float(current_soc), 1),
                }
            except Exception as _reach_err:
                _config_target_reachable = True
                _config_target_reach_meta = {"error": str(_reach_err)}
        wallbox_target_soc_active = bool(wallbox_floor_soc_active and not _config_target_reachable)
        if wallbox_floor_soc_active:
            if wallbox_target_soc_active:
                planning_target_soc = wallbox_target_soc
                self.target_soc = wallbox_target_soc
                logger.info(
                    "Wallbox-Rückfallziel aktiv: Tagesziel %.1f%% laut Restprognose nicht erreichbar; "
                    "Kurve endet bei der Hausakku-Reserve %.1f%%."
                    % (config_target_soc, wallbox_target_soc)
                )
            else:
                logger.info(
                    "Wallbox-Speicherboden aktiv: Hausakku-Reserve %.1f%% begrenzt die Wallbox-Stütze; "
                    "Speicher-Tagesziel %.1f%% bleibt erreichbar und aktiv."
                    % (wallbox_target_soc, config_target_soc)
                )
        weather_reserve_active = False
        weather_reserve_need_wh = 0.0
        try:
            # C++-nah: Wenn die kommenden Tage absehbar schlecht werden,
            # ist das Config-Abendziel kein harter Deckel. Dann darf V4 heute
            # bewusst mehr speichern, statt sonnige Energie wegzugeben und
            # morgen aus dem Netz zu beziehen. Das ist ein echtes Plan-Ziel,
            # getrennt von max_reachable_soc.
            future_slots = [
                s for s in timeline
                if days[1] <= float(s.get("ts", 0)) < days[3]
            ]
            future_deficit_wh = sum(max(0.0, -float(s.get("surplus_w", 0))) * 0.25 for s in future_slots)
            future_surplus_wh = sum(max(0.0,  float(s.get("surplus_w", 0))) * 0.25 for s in future_slots)
            weather_reserve_need_wh = max(0.0, future_deficit_wh - future_surplus_wh)
            if (not wallbox_target_soc_active
                    and config_target_soc < 99.5
                    and weather_reserve_need_wh > self.capacity_wh * 0.02):
                reserve_pct = (weather_reserve_need_wh / max(1.0, self.capacity_wh)) * 100.0
                planning_target_soc = min(100.0, config_target_soc + reserve_pct)
                if planning_target_soc > config_target_soc + 0.5:
                    self.target_soc = planning_target_soc
                    weather_reserve_active = True
                    logger.info(
                        "Schlechtwetterreserve: 48h-Bilanz %.0fWh Defizit -> Tagesziel %.1f%% auf %.1f%% angehoben."
                        % (weather_reserve_need_wh, config_target_soc, planning_target_soc)
                    )
        except Exception as _reserve_err:
            logger.warning("Schlechtwetterreserve konnte nicht berechnet werden: %s" % _reserve_err)
            planning_target_soc = config_target_soc
            self.target_soc = config_target_soc
            if wallbox_target_soc_active:
                planning_target_soc = wallbox_target_soc
                self.target_soc = wallbox_target_soc

        # Hilfsfunktion für Vorwärts-Projektion des SoC zum Tagesbeginn
        def project_soc(start_soc, until_ts):
            psoc = start_soc
            for s in timeline:
                if s["ts"] >= until_ts: break
                cw = s.get("target_charge_w", s["charge_w"])
                psoc += ((cw * 0.25) / self.capacity_wh) * 100.0
                psoc = max(0, min(100, psoc))
            return psoc

        for day_ms in days:
            day_end_ms = day_ms + 86400000

            sunset_ts = 0
            peaking_slots = []

            for slot in timeline:
                if slot["ts"] >= day_end_ms: break
                if slot["ts"] >= day_ms:
                    if slot["pv_w"] > 100: sunset_ts = slot["ts"]
                    if self.export_limit_w > 0 and slot["surplus_w"] > self.export_limit_w:
                        peaking_slots.append(slot)

            if sunset_ts == 0: continue

            # Neu in V4.1: Wir berechnen die benötigte Energie nicht ab Mitternacht,
            # sondern ab dem prognostizierten Tiefpunkt am Morgen (Morning Dip).
            day_slots = [s for s in timeline if s["ts"] >= day_ms and s["ts"] < day_end_ms]

            # Simuliere den SoC-Verlauf in der Nacht bis zum PV-Start
            temp_soc = current_soc if day_ms == midnight_today else project_soc(current_soc, day_ms)
            min_soc_today = temp_soc
            for s in day_slots:
                if s["pv_w"] > 50: break # Sonne geht auf, Tiefpunkt erreicht
                # Wir rechnen den Verbrauch ab
                discharge_wh = (s["home_w"] + s["wp_w"] + s.get("climate_w", 0) + _slot_planned_load_w(s)) * 0.25
                temp_soc -= (discharge_wh / self.capacity_wh) * 100.0
                temp_soc = max(0, temp_soc)
                if temp_soc < min_soc_today: min_soc_today = temp_soc

            required_wh = self.capacity_wh * max(0.0, (self.target_soc - min_soc_today)) / 100.0

            if required_wh > 0:
                day_name = _curve_day_label(day_ms)
                logger.info(f"Battery Target {day_name}: Brauche {required_wh:.0f} Wh (berechnet ab {min_soc_today:.1f}% SoC Tiefstwert, Ziel {self.target_soc:.0f}%) bis Sonnenuntergang.")

            # A) PRIO 1: Peak-Shaving (Vorsorgliche Ladung in extremen Spitzen sichern)
            peak_charge_wh = 0
            for slot in peaking_slots:
                # Wieviel W überschreiten das Export Limit?
                excess_w = slot["surplus_w"] - self.export_limit_w
                charge_power = min(excess_w, self.max_charge_w)
                slot["is_peak"] = True

                # Wenn wir im Peak laden, tracken wir das bereits
                slot["target_charge_w"] = charge_power
                peak_charge_wh += (charge_power * 0.25)
                # Surplus wurde schon bedient
                slot["surplus_w"] -= charge_power

            remaining_wh = required_wh - peak_charge_wh

            # B) PRIO 2: Ladeverzögerung / Glattes Aufteilen des Restes über alle sonnigen Stunden bis Sunset
            if remaining_wh > 0:
                chargeable_slots = []
                for slot in timeline:
                    if slot["ts"] >= day_end_ms: break
                    if slot["ts"] >= day_ms:
                        # Sicherheits-Puffer: Wir tun so, als wäre der Sonnenuntergang 1,5 Stunden früher!
                        if slot["ts"] > (sunset_ts - 5400000): break
                        if slot.get("is_peak"): continue
                        if slot["surplus_w"] > 0:
                            chargeable_slots.append(slot)

                if chargeable_slots:
                    slots_left = len(chargeable_slots)
                    # KRITISCH: Mindest-Lade-Leistung die sicherstellt, dass required_wh
                    # in den verfuegbaren Slots tatsaechlich erreicht wird.
                    # Ohne dies drosselt die Avg-Berechnung zu stark bei großem PV-Surplus
                    # (z.B. 14.7kWp mit 94kWh Tag: remaining_surplus >> required_wh*1.5 immer True,
                    # avg_w bleibt bei 63W statt 342W -> Max SoC nie ueber 86%)
                    min_charge_w = (remaining_wh / (slots_left * 0.25)) if slots_left > 0 else 0

                    for i, slot in enumerate(chargeable_slots):
                        if remaining_wh <= 0:
                            slot["target_charge_w"] = 0
                            continue

                        # Vorausblick: Wie viel Überschuss Energie (Wh) erwartet uns heute noch INSGESAMT?
                        remaining_surplus_wh = sum(s["surplus_w"] * 0.25 for s in chargeable_slots[i:])

                        # Wenn der gesamte noch kommende Sonnenüberschuss eh nur noch knapp reicht
                        # (mit 50% Puffer für Wolken-Sicherheit), dann nehmen wir alles!
                        if remaining_surplus_wh <= (remaining_wh * 1.5):
                            target = slot["surplus_w"]
                        else:
                            # Glaetten und drosseln - aber MINIMUM nicht unterschreiten!
                            # min_charge_w stellt sicher dass Ziel-SoC erreichbar bleibt.
                            avg_w_required = remaining_wh / (slots_left * 0.25)
                            target = max(min_charge_w, min(avg_w_required, slot["surplus_w"]))

                        target = min(target, self.max_charge_w)
                        slot["target_charge_w"] = target

                        remaining_wh -= (target * 0.25)
                        # min_charge_w dynamisch anpassen wenn remaining_wh sinkt
                        if slots_left > 1:
                            min_charge_w = (remaining_wh / ((slots_left - 1) * 0.25)) if slots_left > 1 else 0
                        slots_left -= 1


        # --- 4. Simulation von SoC anwenden ---
        sim_soc = current_soc
        for idx, slot in enumerate(timeline):
            if idx == 0:
                slot["soc"] = sim_soc

            if "target_charge_w" in slot:
                slot["charge_w"] = slot["target_charge_w"]

            # Lade-Logik limitiert durch SoC!
            actual_charge_w = slot["charge_w"]
            if sim_soc >= 100 and slot["charge_w"] > 0:
                actual_charge_w = 0
            elif sim_soc <= 0 and slot["charge_w"] < 0:
                actual_charge_w = 0

            # SoC-Update testen
            energy_wh = actual_charge_w * 0.25
            next_soc = sim_soc + (energy_wh / self.capacity_wh) * 100.0

            # Hatten wir Überladung im Frame?
            if next_soc > 100:
                actual_charge_w = ((100.0 - sim_soc) / 100.0) * self.capacity_wh * 4.0
                next_soc = 100.0
            elif next_soc < 0:
                actual_charge_w = ((0.0 - sim_soc) / 100.0) * self.capacity_wh * 4.0
                next_soc = 0.0

            slot["charge_w"] = actual_charge_w
            sim_soc = next_soc
            if idx > 0:
                slot["soc"] = sim_soc

        # --- 5. Active Eco-Dumping (PV-Abregelungs-Praevention) ---
        # DUMP-LOCK v4.4.6: Persistentes Tages-Flag verhindert Re-Dumping bei Simulator-Neustart.
        # Problem: Slot-Suche im alten Plan versagt wenn vorheriger Lock bereits keine Slots gesetzt hat.
        # Fix: eco_dump_day (Mitternacht-Timestamp) wird im Plan gespeichert. Gleicher Tag = Lock.

        _dump_lock_today = False
        _predump_lock_reset_today = False
        _dump_active_days = set()
        _dump_active_day_keys = set()
        _dump_target_soc_by_day = {}
        _dump_dump_wh_by_day = {}
        _dump_preventable_wh_by_day = {}
        _dump_raw_pressure_wh_by_day = {}
        _dump_safe_headroom_wh_by_day = {}
        _dump_unavoidable_wh_by_day = {}
        _dump_start_ts_by_day = {}
        _dump_end_ts_by_day = {}
        _dump_reason_by_day = {}
        if self.predump_enabled:
            try:
                _old_plan = json.load(open(OUTPUT_FILE, encoding='utf-8'))
                _plan_dump_date = _old_plan.get('eco_dump_date', '')
                _old_meta = _old_plan.get("target_curve_meta", {}) if isinstance(_old_plan, dict) else {}
                _old_dump_wh = self._safe_float(
                    _old_plan.get("predump_dump_wh", _old_meta.get("predump_dump_wh", 0.0)),
                    0.0,
                )
                if _plan_dump_date == today_date_str:
                    if _old_dump_wh >= 200.0:
                        _lock_deadline_ms = None
                        if self.morning_soc > 0:
                            _lock_deadline_ms = midnight_today + max(0.0, min(23.75, float(self.morning_hour))) * 3600000
                        _old_curve_soc = self._safe_float(
                            _old_plan.get(
                                "predump_curve_soc",
                                _old_meta.get(
                                    "predump_curve_soc",
                                    _old_plan.get("morning_target", self.predump_min_soc),
                                ),
                            ),
                            self.predump_min_soc,
                        )
                        _old_preventable_wh = self._safe_float(
                            _old_plan.get(
                                "predump_preventable_clipping_wh",
                                _old_meta.get("predump_preventable_clipping_wh", _old_dump_wh),
                            ),
                            _old_dump_wh,
                        )
                        _old_raw_pressure_wh = self._safe_float(
                            _old_plan.get(
                                "predump_raw_pressure_wh",
                                _old_meta.get("predump_raw_pressure_wh", _old_preventable_wh),
                            ),
                            _old_preventable_wh,
                        )
                        _old_curtailment_pressure_wh = self._safe_float(
                            _old_plan.get(
                                "curtailment_pressure_wh",
                                _old_meta.get("curtailment_pressure_wh", _old_raw_pressure_wh),
                            ),
                            _old_raw_pressure_wh,
                        )
                        _old_pressure_underestimated = (
                            _old_curtailment_pressure_wh
                            > _old_raw_pressure_wh + max(500.0, self.capacity_wh * 0.01)
                        )
                        _old_regelbuffer_pct = max(
                            0.0,
                            self._safe_float(self.v4_config.get("eco_dump_regelbuffer_pct", 2.0), 2.0),
                        )
                        _old_adaptive_min_soc = self._configured_predump_adaptive_min_soc(
                            self.predump_min_soc,
                            _old_raw_pressure_wh,
                            self.capacity_wh * _old_regelbuffer_pct / 100.0,
                        )
                        _old_adaptive_floor_invalid = _old_curve_soc < _old_adaptive_min_soc - 0.5
                        _old_has_headroom_guard = (
                            "predump_safe_headroom_wh" in _old_plan
                            or "predump_safe_headroom_wh" in _old_meta
                        )
                        _old_adaptive_required_wh = self._safe_float(
                            _old_plan.get(
                                "adaptive_headroom_required_wh",
                                _old_meta.get("adaptive_headroom_required_wh", -1.0),
                            ),
                            -1.0,
                        )
                        _old_evening_shortfall_wh = self._safe_float(
                            _old_plan.get(
                                "evening_shortfall_wh",
                                _old_meta.get("evening_shortfall_wh", 0.0),
                            ),
                            0.0,
                        )
                        _old_adaptive_lock_invalid = bool(
                            _old_evening_shortfall_wh >= 200.0
                            or (
                                _old_adaptive_required_wh >= 0.0
                                and (
                                    _old_adaptive_required_wh < 200.0
                                    or _old_dump_wh > _old_adaptive_required_wh + 250.0
                                )
                            )
                            or _old_adaptive_floor_invalid
                        )
                        _old_topology = _old_plan.get("pv_topology") if isinstance(_old_plan.get("pv_topology"), dict) else {}
                        _old_topology_revision = str(_old_topology.get("revision") or "")
                        _current_topology_revision = str(self.pv_topology_contract.get("revision") or "")
                        _topology_revision_changed = bool(
                            not _old_topology_revision
                            or _old_topology_revision != _current_topology_revision
                        )
                        if _topology_revision_changed:
                            _predump_lock_reset_today = True
                            logger.info(
                                "Eco-Dump Lock neu bewertet: PV-Topologierevision fehlt oder hat sich geändert."
                            )
                        elif not _old_has_headroom_guard:
                            _predump_lock_reset_today = True
                            logger.info(
                                f"Eco-Dump Lock neu bewertet: {today_date_str} hatte {_old_dump_wh:.0f}Wh "
                                "ohne Headroom-Abzug; neuer Plan darf korrigieren."
                            )
                        elif _old_adaptive_floor_invalid:
                            _predump_lock_reset_today = True
                            logger.info(
                                f"Eco-Dump Lock neu bewertet: {today_date_str} Ziel {_old_curve_soc:.1f}% "
                                f"liegt unter adaptiver Unterkante {_old_adaptive_min_soc:.1f}%. "
                                "Neuer Plan darf korrigieren."
                            )
                        elif _old_adaptive_lock_invalid:
                            _predump_lock_reset_today = True
                            logger.info(
                                f"Eco-Dump Lock neu bewertet: {today_date_str} hatte {_old_dump_wh:.0f}Wh, "
                                f"adaptiv benoetigt {_old_adaptive_required_wh:.0f}Wh, "
                                f"Abendziel-Risiko {_old_evening_shortfall_wh:.0f}Wh. Neuer Plan darf korrigieren."
                            )
                        elif _old_pressure_underestimated:
                            _predump_lock_reset_today = True
                            logger.info(
                                f"Eco-Dump Lock neu bewertet: {today_date_str} hatte Rohdruck "
                                f"{_old_raw_pressure_wh:.0f}Wh, aktueller Abregeldruck "
                                f"{_old_curtailment_pressure_wh:.0f}Wh. Neuer Plan darf erhoehen."
                            )
                        elif _lock_deadline_ms is not None and time.time() * 1000 >= _lock_deadline_ms:
                            _predump_lock_reset_today = True
                            logger.info(
                                f"Eco-Dump Lock abgelaufen: {today_date_str} hatte {_old_dump_wh:.0f}Wh, "
                                "aber der Kurvenstart ist vorbei. Kein aktiver Pre-Dump nach Kurvenstart."
                            )
                        else:
                            _dump_lock_today = True
                            _dump_active_days.add(midnight_today)
                            _dump_active_day_keys.add(today_date_str)
                            _dump_target_soc_by_day[midnight_today] = max(0.0, min(100.0, _old_curve_soc))
                            _dump_dump_wh_by_day[midnight_today] = _old_dump_wh
                            _dump_preventable_wh_by_day[midnight_today] = _old_preventable_wh
                            _dump_raw_pressure_wh_by_day[midnight_today] = _old_raw_pressure_wh
                            _dump_unavoidable_wh_by_day[midnight_today] = self._safe_float(
                                _old_plan.get(
                                    "predump_unavoidable_clipping_wh",
                                    _old_meta.get("predump_unavoidable_clipping_wh", 0.0),
                                ),
                                0.0,
                            )
                            _dump_safe_headroom_wh_by_day[midnight_today] = self._safe_float(
                                _old_plan.get(
                                    "predump_safe_headroom_wh",
                                    _old_meta.get("predump_safe_headroom_wh", 0.0),
                                ),
                                0.0,
                            )
                            _dump_reason_by_day[midnight_today] = _old_plan.get(
                                "predump_reason",
                                _old_meta.get("predump_reason", ""),
                            )
                            _dump_start_ts_by_day[midnight_today] = int(self._safe_float(
                                _old_plan.get("predump_start_ts", _old_meta.get("predump_start_ts", 0)),
                                0,
                            ))
                            _dump_end_ts_by_day[midnight_today] = int(self._safe_float(
                                _old_plan.get("predump_end_ts", _old_meta.get("predump_end_ts", 0)),
                                0,
                            ))
                            logger.info(
                                f"Eco-Dump Lock: Dump am {today_date_str} bereits geplant "
                                f"({_old_dump_wh:.0f}Wh auf {_old_curve_soc:.1f}%), kein Re-Dump (verhindert Ping-Pong)."
                            )
                    else:
                        logger.info(
                            f"Eco-Dump Lock ignoriert: {today_date_str} hat nur {_old_dump_wh:.0f}Wh Dump-Energie."
                        )
            except Exception:
                pass
        else:
            logger.info("Pre-Discharge Planung deaktiviert: kein Eco-Dump, keine Pre-Dump-Entladung in der Prognose.")

        # Alias fuer Section 6 (Kurvenstart-Fix nach Dump)
        _existing_dump_active = _dump_lock_today

        for day_ms in days:
            day_end_ms = day_ms + 86400000
            if not self.predump_enabled:
                continue
            _hard_predump_requested = bool(self.hard_predump_enabled)
            _typed_dc_pressure_available = bool(
                self.pv_topology_contract.get("split_usable")
                and _live_e3dc_dc_limit_w > 0.0
            )
            if self.export_limit_w <= 0 and not _typed_dc_pressure_available and not _hard_predump_requested:
                continue

            day_slots = [s for s in timeline if day_ms <= s["ts"] < day_end_ms]
            if not day_slots: continue
            if weather_reserve_active:
                logger.info(
                    "Schlechtwetterreserve aktiv: kein Pre-Dump; Energie wird fuer die kommenden Defizittage gehalten."
                )
                continue

            # PV-Start finden
            pv_start_ts = day_end_ms
            for s in day_slots:
                if s["pv_w"] > 50:
                    pv_start_ts = s["ts"]
                    break
            if pv_start_ts >= day_end_ms: continue

            # DUMP-LOCK: Kein neuer Dump wenn heute bereits einer geplant wurde
            if _dump_lock_today and day_ms <= midnight_today:
                continue

            # Start-SoC fuer diesen Tag
            if day_ms <= midnight_today:
                start_soc_day = current_soc
            else:
                start_soc_day = next(
                    (s.get("soc", current_soc) for s in timeline if s["ts"] >= day_ms),
                    current_soc
                )

            min_soc_allowed = max(0.0, min(100.0, float(self.predump_min_soc)))
            hard_predump_target_soc = max(
                min_soc_allowed,
                min(100.0, float(self.hard_predump_target_soc)),
            )
            if _hard_predump_requested:
                day_label = _curve_day_label(day_ms)
                now_ms = time.time() * 1000
                hard_deadline_ts = day_ms + max(0.0, min(23.75, float(self.morning_hour))) * 3600000 if self.morning_soc > 0 else pv_start_ts
                hard_dump_wh_target = self.capacity_wh * max(0.0, float(start_soc_day) - hard_predump_target_soc) / 100.0
                if hard_dump_wh_target < 200.0:
                    logger.info(
                        f"Hard-Pre-Dump {day_label}: Fixziel {hard_predump_target_soc:.1f}%% bereits erreicht "
                        f"(Start-SoC {float(start_soc_day):.1f}%%)."
                    )
                    continue
                if now_ms >= hard_deadline_ts:
                    _dump_target_soc_by_day[day_ms] = hard_predump_target_soc
                    _dump_dump_wh_by_day[day_ms] = 0.0
                    _dump_preventable_wh_by_day[day_ms] = hard_dump_wh_target
                    _dump_reason_by_day[day_ms] = (
                        "Kein aktiver Hard-Pre-Dump: Vorab-Fenster vor Kurvenstart ist vorbei. "
                        "Fixziel %.1f%% wird heute nicht mehr aktiv angefahren."
                        % hard_predump_target_soc
                    )
                    _dump_active_days.add(day_ms)
                    logger.info(
                        f"Hard-Pre-Dump {day_label}: Vorab-Fenster vor Kurvenstart vorbei; "
                        f"kein aktiver Dump auf Fixziel {hard_predump_target_soc:.1f}%%."
                    )
                    continue
                hard_window_h_raw = self._safe_float(self.v4_config.get("pd_max_hours", 5.0), 5.0)
                hard_window_h = hard_window_h_raw if hard_window_h_raw > 0 else 5.0
                now_slot_ts = (int(now_ms) // 900000) * 900000
                hard_start_ts = max(now_slot_ts, hard_deadline_ts - max(0.25, hard_window_h) * 3600000)
                hard_slots = [s for s in day_slots if hard_start_ts <= s["ts"] < hard_deadline_ts]
                if not hard_slots:
                    logger.info(
                        f"Hard-Pre-Dump {day_label}: Fixziel {hard_predump_target_soc:.1f}%% wartet auf Startfenster."
                    )
                    continue

                hard_slots_ramp = sorted(hard_slots, key=lambda x: x["ts"])
                max_dump_w = min(self.max_discharge_w, 4500.0)
                hard_dumped_wh, _positive_hard_slots = self._pace_predump_slots(
                    hard_slots_ramp,
                    hard_dump_wh_target,
                    max_dump_w,
                    300.0,
                )

                hard_target_reached_soc = max(
                    hard_predump_target_soc,
                    float(start_soc_day) - ((hard_dumped_wh / self.capacity_wh) * 100.0 if self.capacity_wh > 0 else 0.0),
                )
                _dump_target_soc_by_day[day_ms] = hard_target_reached_soc
                _dump_dump_wh_by_day[day_ms] = hard_dumped_wh
                _dump_preventable_wh_by_day[day_ms] = hard_dump_wh_target
                _dump_reason_by_day[day_ms] = (
                    "Hard-Pre-Dump geplant: %.1f kWh vorab entladen auf festen Ziel-SoC %.1f%%."
                    % (hard_dumped_wh / 1000.0, hard_predump_target_soc)
                )
                if _positive_hard_slots:
                    _dump_start_ts_by_day[day_ms] = int(float(_positive_hard_slots[0]["ts"]))
                    _dump_end_ts_by_day[day_ms] = int(float(_positive_hard_slots[-1]["ts"]) + 900000)
                _dump_active_days.add(day_ms)
                _dump_active_day_keys.add(_curve_day_date(day_ms))
                if day_ms <= midnight_today:
                    _existing_dump_active = True
                logger.info(
                    f"Hard-Pre-Dump Punktlandung {day_label}: {hard_dumped_wh:.0f} Wh auf Fixziel "
                    f"{hard_predump_target_soc:.1f}%% in {len(_positive_hard_slots)} Slots."
                )
                continue

            # Schritt 1: Hardware-Realitaets-Simulation
            # E3DC laedt in Wirklichkeit bis 100% wenn genug PV da ist (ignoriert unsere target_soc-Drosselung).
            # min_home_w: konservativer Mindest-Eigenverbrauch (nicht ML, der ueberschaetzt oft den Verbrauch).
            # Die Waermepumpen-Prognose ist fuer WW-/Sommerbetrieb keine sichere Senke im Abregel-Fenster.
            trust_wp_forecast_sink = self._cfg_bool(
                self.v4_config.get("predump_trust_heatpump_forecast_as_sink"),
                False,
            )
            min_home_w = 300.0
            hw_soc = start_soc_day
            battery_full_ts = None
            first_pressure_ts = None
            soc_at_first_pressure = None
            raw_clipping_pressure_wh = 0.0
            unavoidable_clipping_wh = 0.0
            for s in day_slots:
                wp_sink_w = s.get("wp_w", 0) if trust_wp_forecast_sink else 0
                # Pre-Dump must reserve space against PV pressure. Forecast or
                # live-derived home spikes are not guaranteed sinks here.
                home_sink_w = min_home_w
                _predump_pcc_contract = self._pcc_headroom_limit_for_topology(
                    s.get("pv_topology_status")
                )
                _predump_pressure = slot_headroom_pressure(
                    total_pv_w=s.get("pv_w", 0.0),
                    e3dc_dc_pv_w=s.get("e3dc_dc_pv_w"),
                    external_ac_pv_w=s.get("external_ac_pv_w"),
                    topology_status=s.get("pv_topology_status"),
                    topology_revision=s.get("pv_topology_revision"),
                    expected_topology_revision=self.pv_topology_contract.get("revision"),
                    e3dc_dc_limit_w=_live_e3dc_dc_limit_w,
                    pcc_limit_w=_predump_pcc_contract.get("limit_w"),
                    pcc_limit_active=_predump_pcc_contract.get("active") is True,
                    safe_consumers_w=home_sink_w + wp_sink_w,
                    charge_limit_w=self.max_charge_w,
                    e3dc_dc_limit_source=_live_e3dc_dc_limit_source,
                    pcc_limit_source=_predump_pcc_contract.get("source", self.export_limit_source),
                )
                pressure_w = float(_predump_pressure.get("combined_pressure_w", 0.0) or 0.0)
                preventable_pressure_w = float(_predump_pressure.get("preventable_w", 0.0) or 0.0)
                unavoidable_pressure_w = float(_predump_pressure.get("unavoidable_w", 0.0) or 0.0)
                StorageSimulator._retain_slot_headroom_evidence(s, _predump_pressure)
                if preventable_pressure_w > 0.0 and first_pressure_ts is None:
                    first_pressure_ts = s["ts"]
                    soc_at_first_pressure = hw_soc
                raw_clipping_pressure_wh += preventable_pressure_w * 0.25
                unavoidable_clipping_wh += unavoidable_pressure_w * 0.25
                hw_surplus = s["pv_w"] - home_sink_w - wp_sink_w
                hw_charge = min(hw_surplus, self.max_charge_w) if hw_surplus > 0 else max(hw_surplus, -self.max_discharge_w)
                hw_soc = max(0.0, min(100.0, hw_soc + (hw_charge * 0.25 / self.capacity_wh) * 100.0))
                if hw_soc >= 99.5 and battery_full_ts is None:
                    battery_full_ts = s["ts"]

            if battery_full_ts is None: continue  # Batterie wird nicht voll -> kein Clipping
            if first_pressure_ts is None or raw_clipping_pressure_wh < 300.0:
                continue  # Unter 300 Wh Schwelle -> ignorieren

            # Schritt 3: Pre-Discharge planen
            # Zeitfenster: jetzt bis battery_full_ts (Nacht + fruehe PV-Stunden mit freier Netz-Kapazitaet)
            # E3DC exportiert PV + Batterie-Entladung zusammen, bleibt unter Einspeise-Limit.
            # Etwas Extra-Platz fuer Regelpuffer schaffen: bei Abregelrisiko
            # soll der Akku nicht exakt auf Kante geplant werden.
            regelbuffer_pct  = max(0.0, self._safe_float(self.v4_config.get("eco_dump_regelbuffer_pct", 2.0), 2.0))
            regelbuffer_wh   = self.capacity_wh * regelbuffer_pct / 100.0
            base_min_soc_allowed = max(0.0, min(100.0, float(self.predump_min_soc)))
            min_soc_allowed = self._configured_predump_adaptive_min_soc(
                base_min_soc_allowed,
                raw_clipping_pressure_wh,
                regelbuffer_wh,
            )
            if min_soc_allowed > base_min_soc_allowed + 0.1:
                logger.info(
                    "Pre-Dump adaptive Unterkante: %.1f%% statt %.1f%% "
                    "(Rohdruck %.0fWh, Puffer %.0fWh, Start-SoC %.1f%%)."
                    % (
                        min_soc_allowed,
                        base_min_soc_allowed,
                        raw_clipping_pressure_wh,
                        regelbuffer_wh,
                        float(start_soc_day),
                    )
                )
            max_dumpable_wh  = self.capacity_wh * max(0.0, start_soc_day - min_soc_allowed) / 100.0
            headroom_need = self._predump_dump_need_from_headroom(
                raw_clipping_pressure_wh,
                soc_at_first_pressure,
                self.target_soc,
                self.capacity_wh,
                regelbuffer_wh,
                max_dumpable_wh,
            )
            preventable_clipping_wh = headroom_need["need_without_buffer_wh"]
            safe_headroom_wh = headroom_need["safe_headroom_wh"]
            dump_wh_target = headroom_need["dump_target_wh"]
            if dump_wh_target < 200.0: continue

            full_dt = datetime.fromtimestamp(battery_full_ts / 1000).strftime("%H:%M")
            pressure_dt = datetime.fromtimestamp(first_pressure_ts / 1000).strftime("%H:%M")
            day_label = _curve_day_label(day_ms)
            logger.warning(
                f"Abregelungsrisiko {day_label}: vermeidbarer Rohdruck {raw_clipping_pressure_wh:.0f} Wh ab {pressure_dt} Uhr, "
                f"in der Pre-Dump-Rechnung vorab anrechenbar {safe_headroom_wh:.0f} Wh, "
                f"Pre-Dump-Bedarf {preventable_clipping_wh:.0f} Wh "
                f"(Batterie-voll-Simulation ab {full_dt} Uhr, unvermeidbar {unavoidable_clipping_wh:.0f} Wh)."
            )

            now_ms      = time.time() * 1000
            # Aktiver Pre-Dump ist ein Vorab-Pfad und endet am Kurvenstart.
            # Nach Kurvenstart darf nur noch die Ladekurve Speicherplatz halten.
            predump_deadline_ts = battery_full_ts
            if self.morning_soc > 0:
                predump_deadline_ts = min(
                    predump_deadline_ts,
                    day_ms + max(0.0, min(23.75, float(self.morning_hour))) * 3600000,
                )
            else:
                predump_deadline_ts = min(predump_deadline_ts, pv_start_ts)

            predump_window_h_raw = self._safe_float(self.v4_config.get("pd_max_hours", 5.0), 5.0)
            predump_window_h = predump_window_h_raw if predump_window_h_raw > 0 else 5.0
            predump_window_start_ts = predump_deadline_ts - max(0.25, predump_window_h) * 3600000
            active_dump_start_ts = max(now_ms, predump_window_start_ts)

            # Startfenster noch nicht erreicht: Planung/Anzeige schreiben,
            # aber noch keine Entlade-Slots aktivieren.
            if now_ms < predump_window_start_ts:
                planned_target_soc = max(
                    min_soc_allowed,
                    start_soc_day - ((dump_wh_target / self.capacity_wh) * 100.0 if self.capacity_wh > 0 else 0.0),
                )
                _dump_target_soc_by_day[day_ms] = planned_target_soc
                _dump_dump_wh_by_day[day_ms] = dump_wh_target
                _dump_preventable_wh_by_day[day_ms] = preventable_clipping_wh
                _dump_raw_pressure_wh_by_day[day_ms] = raw_clipping_pressure_wh
                _dump_safe_headroom_wh_by_day[day_ms] = safe_headroom_wh
                _dump_unavoidable_wh_by_day[day_ms] = unavoidable_clipping_wh
                _dump_start_ts_by_day[day_ms] = int(predump_window_start_ts)
                _dump_end_ts_by_day[day_ms] = int(predump_deadline_ts)
                _dump_reason_by_day[day_ms] = (
                    "Kurvenpuffer/Pre-Dump-Bedarf: Bis zum PV-Druckfenster sollen %.1f kWh "
                    "Speicherplatz frei bleiben. Der Abregeldruck über den Tag liegt bei %.1f kWh; "
                    "%.1f kWh sind in der Pre-Dump-Rechnung bis dahin vorab anrechenbar. Daraus ergeben sich "
                    "%.1f kWh zusätzlicher Bedarf plus Regelpuffer. Das Startfenster beginnt um %s; "
                    "aktiv entladen wird erst dann und nur über erlaubte Pfade."
                    % (
                        dump_wh_target / 1000.0,
                        raw_clipping_pressure_wh / 1000.0,
                        safe_headroom_wh / 1000.0,
                        preventable_clipping_wh / 1000.0,
                        datetime.fromtimestamp(predump_window_start_ts / 1000).strftime("%H:%M"),
                    )
                )
                _dump_active_days.add(day_ms)
                _dump_active_day_keys.add(_curve_day_date(day_ms))
                if day_ms <= midnight_today:
                    _existing_dump_active = True
                logger.info(
                    f"Pre-Dump {day_label}: {dump_wh_target:.0f} Wh geplant, wartet bis "
                    f"{datetime.fromtimestamp(predump_window_start_ts / 1000).strftime('%H:%M')} "
                    f"(Ziel-SoC={planned_target_soc:.1f}%%)."
                )
                continue

            # Slots vom aktiven Startfenster bis zum Kurvenstart.
            dump_slots = [s for s in day_slots if active_dump_start_ts <= s["ts"] < predump_deadline_ts]
            if not dump_slots:
                hold_target_soc = max(
                    min_soc_allowed,
                    start_soc_day - ((dump_wh_target / self.capacity_wh) * 100.0 if self.capacity_wh > 0 else 0.0),
                )
                _dump_target_soc_by_day[day_ms] = hold_target_soc
                _dump_dump_wh_by_day[day_ms] = 0.0
                _dump_preventable_wh_by_day[day_ms] = preventable_clipping_wh
                _dump_raw_pressure_wh_by_day[day_ms] = raw_clipping_pressure_wh
                _dump_safe_headroom_wh_by_day[day_ms] = safe_headroom_wh
                _dump_unavoidable_wh_by_day[day_ms] = unavoidable_clipping_wh
                _dump_reason_by_day[day_ms] = (
                    "Kein aktiver Pre-Dump: Das Vorab-Fenster ist vorbei. "
                    "Die Pre-Dump-Rechnung hätte bis zur Unterkante höchstens %.1f kWh "
                    "zusätzlichen Speicherplatz freigegeben. Ab jetzt hält die adaptive Ladekurve "
                    "den nötigen Headroom; dieser Wert ist nicht der gesamte Abregeldruck "
                    "(Abregeldruck %.1f kWh, in der Pre-Dump-Rechnung vorab anrechenbar %.1f kWh)."
                    % (
                        dump_wh_target / 1000.0,
                        raw_clipping_pressure_wh / 1000.0,
                        safe_headroom_wh / 1000.0,
                    )
                )
                _dump_active_days.add(day_ms)
                logger.info(
                    f"Kurvenpuffer {day_label}: Vorab-Fenster vor Kurvenstart vorbei; "
                    f"kein aktiver Pre-Dump, Kurvenpuffer Ziel-SoC={hold_target_soc:.1f}%%."
                )
                continue

            # Punktlandung: Energie gleichmaessig ueber das verbleibende Fenster
            # verteilen. Das Startfenster ist kein Signal fuer sofortigen Vollgas-Dump.
            dump_slots_ramp = sorted(dump_slots, key=lambda x: x["ts"])
            max_dump_w  = min(self.max_discharge_w, 4500.0)
            dumped_wh, _positive_dump_slots = self._pace_predump_slots(
                dump_slots_ramp,
                dump_wh_target,
                max_dump_w,
                300.0,
            )

            n_slots = len(_positive_dump_slots)
            n_night = sum(1 for s in _positive_dump_slots if s["ts"] < pv_start_ts)
            n_early = n_slots - n_night
            p_first = _positive_dump_slots[0].get("grid_dump_w", 0) if _positive_dump_slots else 0
            p_last  = _positive_dump_slots[-1].get("grid_dump_w", 0) if _positive_dump_slots else 0
            dump_target_soc = max(
                min_soc_allowed,
                start_soc_day - ((dumped_wh / self.capacity_wh) * 100.0 if self.capacity_wh > 0 else 0.0),
            )
            _dump_target_soc_by_day[day_ms] = dump_target_soc
            _dump_dump_wh_by_day[day_ms] = dumped_wh
            _dump_preventable_wh_by_day[day_ms] = preventable_clipping_wh
            _dump_raw_pressure_wh_by_day[day_ms] = raw_clipping_pressure_wh
            _dump_safe_headroom_wh_by_day[day_ms] = safe_headroom_wh
            _dump_unavoidable_wh_by_day[day_ms] = unavoidable_clipping_wh
            _dump_reason_by_day[day_ms] = (
                "Kurvenpuffer/Pre-Dump-Bedarf: %.1f kWh Speicherplatz sollen vor dem "
                "PV-Druckfenster frei bleiben, um ca. %.1f kWh Rest-Abregelung plus "
                "Regelpuffer zu vermeiden. Der Abregeldruck über den Tag liegt bei %.1f kWh; "
                "%.1f kWh sind in der Pre-Dump-Rechnung bis dahin vorab anrechenbar. Erreichbar ist der Wert "
                "nur über erlaubte Entladepfade."
                % (
                    dumped_wh / 1000.0,
                    preventable_clipping_wh / 1000.0,
                    raw_clipping_pressure_wh / 1000.0,
                    safe_headroom_wh / 1000.0,
                )
            )
            if _positive_dump_slots:
                _dump_start_ts_by_day[day_ms] = int(float(_positive_dump_slots[0]["ts"]))
                _dump_end_ts_by_day[day_ms] = int(float(_positive_dump_slots[-1]["ts"]) + 900000)
            logger.info(
                f"Pre-Discharge Punktlandung {day_label}: {dumped_wh:.0f} Wh in {n_slots} Slots "
                f"({n_night} Nacht + {n_early} fruehe PV, {p_first:.0f}W->{p_last:.0f}W, "
                f"Rohdruck {raw_clipping_pressure_wh:.0f} Wh - Headroom {safe_headroom_wh:.0f} Wh "
                f"= Restbedarf {preventable_clipping_wh:.0f} Wh + Regelpuffer {regelbuffer_wh:.0f} Wh, "
                f"Ziel-SoC={dump_target_soc:.1f}%%, min_soc={min_soc_allowed:.0f}%)."
            )
            # DUMP-LOCK setzen: Tages-Flag fuer naechste Simulator-Zyklen
            _dump_active_days.add(day_ms)
            _dump_active_day_keys.add(_curve_day_date(day_ms))
            if day_ms <= midnight_today:
                _existing_dump_active = True  # Kurvenstart-Fix in Section 6 aktivieren




        # --- 6. Soll-Trajektorie (target_timeline) via Rolling Frozen Window Anker ---
        target_timeline = []
        curve_anchors   = []  # Wird im try-Block befuellt; bei Exception leer
        target_curve_meta = {}
        selected_predump_curve_active = False
        selected_predump_curve_soc = None
        selected_predump_dump_wh = 0.0
        selected_predump_preventable_clipping_wh = 0.0
        selected_predump_start_ts = 0
        selected_predump_end_ts = 0
        selected_predump_midnight_target_soc = None
        selected_predump_curve_start_ts = 0
        selected_predump_curve_start_soc = None
        selected_predump_curve_future_dump_wh = 0.0
        selected_predump_raw_pressure_wh = 0.0
        selected_predump_safe_headroom_wh = 0.0
        selected_predump_unavoidable_clipping_wh = 0.0
        adaptive_headroom = {}
        historical_headroom_meta = {}
        cloud_edge_headroom_meta = {}
        pv_today_kwh = None
        published_curve_floor_points = []
        published_curve_floor_active = False
        published_curve_floor_source = ""
        published_curve_floor_reason = "not_loaded"
        published_curve_floor_policy = "published_floor_no_downshift_v1"
        published_curve_anchor_clamps = 0
        published_curve_timeline_clamps = 0
        published_curve_max_lift_pct = 0.0
        published_curve_reset_allowed = False
        published_curve_reset_reason = ""
        if not self.predump_enabled:
            selected_predump_reason = "Pre-Dump deaktiviert"
        elif weather_reserve_active:
            selected_predump_reason = "Pre-Dump pausiert: Schlechtwetterreserve haelt Energie im Speicher."
        elif self.export_limit_w <= 0 and not (
            self.pv_topology_contract.get("split_usable") and _live_e3dc_dc_limit_w > 0.0
        ):
            selected_predump_reason = "Kein Pre-Dump: weder PCC- noch typisiertes E3DC-DC-Limit verfügbar."
        else:
            selected_predump_reason = "Kein Pre-Dump: Prognose sieht kein relevantes Abregelrisiko."
        morning_soc_tl  = current_soc
        q_ratio         = None
        curve_end_ts    = None
        # Der Storage Manager öffnet die EMS-Ladegrenze vor dem Freilauf
        # bereits weich. Der Simulator-Puffer bleibt deshalb nur noch ein
        # Prognose-/Sonnenuntergangspuffer. Relevant ist nicht das letzte
        # 200-W-Restlicht, sondern der letzte nutzbare Überschuss, damit kleine
        # Speicher das Abendziel nicht erst in einer optimistischen Abendfahne
        # erreichen sollen.
        _curve_end_guard_min = max(
            30.0,
            min(120.0, self._safe_float(self.v4_config.get("storage_curve_end_guard_min", 45.0), 45.0)),
        )
        LADEENDE_OFFSET_MS = int(_curve_end_guard_min * 60 * 1000)
        _default_relevant_surplus_w = max(500.0, min(900.0, float(self.max_charge_w) * 0.12))
        _curve_relevant_surplus_w = max(
            200.0,
            min(
                2500.0,
                self._safe_float(
                    self.v4_config.get("storage_curve_relevant_surplus_w", _default_relevant_surplus_w),
                    _default_relevant_surplus_w,
                ),
            ),
        )
        if self._forecast_only_curve_enabled():
            _curve_tail_frontload_factor = max(
                1.0,
                min(
                    2.5,
                    self._safe_float(
                        self.v4_config.get("storage_forecast100_tail_frontload_factor", 1.0),
                        1.0,
                    ),
                ),
            )
        else:
            _curve_tail_frontload_factor = max(
                1.0,
                min(
                    2.5,
                    self._safe_float(self.v4_config.get("storage_curve_tail_frontload_factor", 1.35), 1.35),
                ),
            )
        try:
            # PV-Start/-Ende: erster/letzter Slot mit pv_estimate > 100W.
            # Robustheit: Nach Sonnenuntergang hat der aktuelle Tag kein
            # nutzbares Rest-PV-Fenster mehr. Dann bauen wir die Sollkurve
            # direkt fuer den naechsten Prognosetag statt leer zu bleiben.
            # WICHTIG: Wir lesen aus pv_tl, da timeline immer bei "jetzt" anfaengt
            pv_start_ts = None
            pv_end_ts   = None

            now_ms_select = datetime.now().timestamp() * 1000

            def _pv_window_for_day(day_ms, cap_late_start=False):
                day_end_ms = day_ms + 86400000
                day_pv = [
                    p for p in pv_tl
                    if p["start_timestamp"] >= day_ms and p["start_timestamp"] < day_end_ms
                ]
                start_ts = None
                end_ts = None
                for p in day_pv:
                    if (p.get("predicted_kwh", p.get("pv_estimate", 0)) * 1000.0) > 100:
                        start_ts = p["start_timestamp"]
                        break
                # Wenn die Wetter-API vergangene Stunden des aktuellen Tages
                # abschneidet, darf der Kurvenstart nicht auf den Nachmittag
                # springen. Fuer echte Folgetage bleibt die Prognosezeit erhalten.
                if cap_late_start and start_ts and start_ts > day_ms + 28800000:
                    start_ts = day_ms + 25200000
                for p in reversed(day_pv):
                    if (p.get("predicted_kwh", p.get("pv_estimate", 0)) * 1000.0) > 100:
                        end_ts = p["start_timestamp"]
                        break
                return start_ts, end_ts, day_pv

            today_0_ms = days[0]
            today_end_ms = days[0] + 86400000
            selected_day_offset = 0
            selected_day_label = "Heute"
            selected_pv_tl = []

            for _idx, _day_ms in enumerate(days):
                _start, _end, _day_pv = _pv_window_for_day(_day_ms, cap_late_start=(_idx == 0))
                if not (_start and _end):
                    continue
                _candidate_end = max(_start + 3600000, _end - LADEENDE_OFFSET_MS)
                # Ist die Kurve fuer den aktuellen Tag bereits vorbei, wird
                # sofort der naechste PV-Tag geplant. Das verhindert die rohe
                # Physik-Prognose am Abend bis Mitternacht.
                if _candidate_end <= now_ms_select + 5 * 60000:
                    continue
                today_0_ms = _day_ms
                today_end_ms = _day_ms + 86400000
                pv_start_ts = _start
                pv_end_ts = _end
                selected_pv_tl = _day_pv
                selected_day_offset = _idx
                selected_day_label = _curve_day_label(_day_ms)
                break

            if selected_day_offset > 0:
                logger.info(
                    "Sollkurven-Tag: %s gewaehlt, weil der aktuelle PV-Tag abgeschlossen ist."
                    % selected_day_label
                )

            # Alle Timeline-Slots fuer den gewaehlten Kurventag (aus der berechneten Simulation)
            today_slots_tl = [s for s in timeline if today_0_ms <= s['ts'] < today_end_ms]
            historical_headroom_meta = self._apply_historical_peak_headroom(
                today_slots_tl,
                today_0_ms,
                now_ms_select,
            )
            if historical_headroom_meta.get("active"):
                logger.info(
                    "Historische Abregelreserve aktiv: %d Slots, %.0fWh Druck, "
                    "Peak %.0fW, sichere Hauslast %.0fW, Samples=%d Tage."
                    % (
                        int(historical_headroom_meta.get("slots", 0) or 0),
                        float(historical_headroom_meta.get("reserve_pressure_wh", 0.0) or 0.0),
                        float(historical_headroom_meta.get("max_headroom_pv_w", 0.0) or 0.0),
                        float(historical_headroom_meta.get("safe_home_w", 0.0) or 0.0),
                        int(historical_headroom_meta.get("sample_days", 0) or 0),
                    )
                )
            cloud_edge_headroom_meta = self._apply_live_cloud_edge_headroom(
                today_slots_tl,
                now_ms_select,
                {
                    "pv_w": _live_pv_w or 0.0,
                    "grid_w": _live_grid_w,
                    "derate_w": _live_derate_w,
                    "power_limits_active": _live_power_limits_active,
                    "ts_s": _live_ts,
                },
            )
            if cloud_edge_headroom_meta.get("active"):
                logger.info(
                    "Abregelreserve aktiv: Live-PV %.0fW statt Prognose %.0fW, "
                    "%d Slots bis %.1fh mit Headroom markiert (%.0fWh Druck)."
                    % (
                        float(cloud_edge_headroom_meta.get("live_pv_w", 0.0) or 0.0),
                        float(cloud_edge_headroom_meta.get("forecast_now_w", 0.0) or 0.0),
                        int(cloud_edge_headroom_meta.get("slots", 0) or 0),
                        float(cloud_edge_headroom_meta.get("horizon_h", 0.0) or 0.0),
                        float(cloud_edge_headroom_meta.get("reserve_pressure_wh", 0.0) or 0.0),
                    )
                )

            # PV-Fenster aus der Roh-Prognose bestimmen (stabiler als simulierte timeline)
            today_pv_tl = selected_pv_tl

            curve_end_ts = pv_end_ts
            _forecast100_late_full_guard_active = False
            _forecast100_late_full_guard_old_end_ts = None
            _forecast100_late_full_guard_policy = ""
            if pv_start_ts and pv_end_ts:
                curve_end_ts = self._conservative_curve_end_ts(
                    today_slots_tl,
                    pv_start_ts,
                    pv_end_ts,
                    LADEENDE_OFFSET_MS,
                    _curve_relevant_surplus_w,
                )
                if forecast_only_curve and self._cfg_bool(
                    self.v4_config.get("storage_forecast100_late_full_guard_enable"),
                    True,
                ):
                    _late_curve_end_ts = self._forecast100_late_full_curve_end_ts(
                        pv_start_ts,
                        pv_end_ts,
                        LADEENDE_OFFSET_MS,
                    )
                    if _late_curve_end_ts > int(curve_end_ts) + 15 * 60000:
                        _forecast100_late_full_guard_active = True
                        _forecast100_late_full_guard_old_end_ts = int(curve_end_ts)
                        curve_end_ts = int(_late_curve_end_ts)
                        logger.info(
                            "Forecast-100 Vollstand-Schutz: Freilauf %s -> %s "
                            "(spätes 100%%-Ziel statt früher Vollstand)."
                            % (
                                datetime.fromtimestamp(_forecast100_late_full_guard_old_end_ts / 1000).strftime("%H:%M"),
                                datetime.fromtimestamp(curve_end_ts / 1000).strftime("%H:%M"),
                            )
                        )
                    _forecast100_late_full_guard_policy = "forecast100_late_full_v1"

            _frozen_ladestart_soc = None
            existing_plan = {}
            if os.path.exists(OUTPUT_FILE):
                try:
                    with open(OUTPUT_FILE, 'r') as _f:
                        existing_plan = json.load(_f)
                except: pass
            existing_meta = existing_plan.get("target_curve_meta", {}) if isinstance(existing_plan, dict) else {}
            _curve_meta_mode = "forecast_only_100_v1" if forecast_only_curve else "hourly_weather_ml_v2"
            _curve_start_policy = "forecast_only_integral_v1" if forecast_only_curve else "frozen_anchor_v1"
            _existing_curve_contract_ok = bool(
                isinstance(existing_meta, dict)
                and existing_meta.get("curve_start_policy") == _curve_start_policy
            )
            _now_ms_for_reanchor = time.time() * 1000.0
            _live_reanchored_today = False
            _live_reanchor_reason = ""
            _live_reanchor_old_soc = None
            _live_reanchor_new_soc = None
            _live_reanchor_start_ts = None
            _live_reanchor_before_morning_blocked = False
            _live_reanchor_blocked_reason = ""
            _live_reanchor_threshold_pct = max(
                2.0,
                self._safe_float(self.v4_config.get("storage_live_reanchor_drop_pct", 5.0), 5.0),
            )
            _configured_morning_anchor_ts = None
            if self.morning_soc > 0 and not forecast_only_curve:
                _morning_h_for_reanchor = max(0.0, min(23.75, float(self.morning_hour)))
                _configured_morning_anchor_ts = int(today_0_ms + (_morning_h_for_reanchor * 3600000))

            def _live_reanchor_candidate_soc():
                candidate_soc = max(0.0, min(100.0, float(current_soc)))
                return max(0.0, min(100.0, candidate_soc))

            if pv_start_ts and curve_end_ts and curve_end_ts > pv_start_ts:
                # Anker: SoC am PV-Start (Fußpunkt der Ladekurve)
                # Wenn PV-Start in der Vergangenheit liegt → aus altem Plan lesen

                if (
                    pv_start_ts <= _now_ms_for_reanchor
                    and 'ladestart_soc' in existing_plan
                    and _existing_curve_contract_ok
                    and not _predump_lock_reset_today
                ):
                    _existing_curve_anchors = existing_plan.get("curve_anchors") or []
                    if _existing_curve_anchors:
                        _old_ladestart_soc = float(_existing_curve_anchors[0].get('soc', existing_plan.get('ladestart_soc', current_soc)))
                    else:
                        _old_ladestart_soc = float(existing_plan.get('ladestart_soc', current_soc))
                    _candidate_ladestart_soc = _live_reanchor_candidate_soc()
                    if _old_ladestart_soc - _candidate_ladestart_soc >= _live_reanchor_threshold_pct:
                        _before_configured_morning = bool(
                            _configured_morning_anchor_ts
                            and _now_ms_for_reanchor < float(_configured_morning_anchor_ts)
                        )
                        if _before_configured_morning:
                            morning_soc_tl = _old_ladestart_soc
                            _live_reanchor_before_morning_blocked = True
                            _live_reanchor_blocked_reason = (
                                "Live-Reanker vor konfiguriertem Morgenanker blockiert: "
                                "alter Tagesanker %.1f%% bleibt bis %s gueltig, Live-SoC %.1f%%."
                                % (
                                    _old_ladestart_soc,
                                    datetime.fromtimestamp(float(_configured_morning_anchor_ts) / 1000).strftime("%H:%M"),
                                    float(current_soc),
                                )
                            )
                            logger.info(_live_reanchor_blocked_reason)
                        else:
                            morning_soc_tl = _old_ladestart_soc
                            _live_reanchor_old_soc = round(_old_ladestart_soc, 1)
                            _live_reanchor_new_soc = round(_candidate_ladestart_soc, 1)
                            _live_reanchor_reason = (
                                "Live-SoC %.1f%% liegt %.1f%% unter dem eingefrorenen Tagesanker %.1f%%"
                                % (
                                    float(current_soc),
                                    _old_ladestart_soc - _candidate_ladestart_soc,
                                    _old_ladestart_soc,
                                )
                            )
                            _live_reanchor_blocked_reason = (
                                "Automatischer Live-Reanker deaktiviert: "
                                "Tagesanker %.1f%% bleibt fix, Live-SoC %.1f%% aendert nur Erreichbarkeit/Diagnose."
                                % (_old_ladestart_soc, float(current_soc))
                            )
                            logger.info(_live_reanchor_blocked_reason)
                    else:
                        morning_soc_tl = _old_ladestart_soc
                        logger.info(
                            f"Kurvenstart frozen: bestehender Tagesanker {_old_ladestart_soc:.1f}%% bleibt erhalten "
                            "(kein deutlicher Live-SoC-Abfall)."
                        )
                else:
                    _slot_anchor_found = False
                    for s in today_slots_tl:
                        if s['ts'] >= pv_start_ts:
                            morning_soc_tl = s.get('soc', current_soc)
                            _slot_anchor_found = True
                            break
                    if (
                        not _slot_anchor_found
                        or (
                            pv_start_ts <= (datetime.now().timestamp() * 1000)
                            and not _existing_curve_contract_ok
                            and morning_soc_tl <= 1.0
                            and current_soc > 5.0
                        )
                    ):
                        if self.morning_soc > 0:
                            morning_soc_tl = float(self.morning_soc)
                            logger.info(
                                f"Kurvenstart-Fallback: PV-Start liegt in der Vergangenheit, "
                                f"kein gueltiger frozen_anchor_v1 vorhanden -> Morgenpuffer {self.morning_soc:.1f}%%"
                            )
                        else:
                            morning_soc_tl = current_soc
                            logger.info(
                                f"Kurvenstart-Fallback: storage_morning_soc=0, kein gueltiger Tagesanker -> "
                                f"Live-SoC {current_soc:.1f}%% als Notanker."
                            )

                # Eba/C++-nahe Kurve: Eco-Dump darf den Morgenanker nicht auf den
                # aktuellen SoC hochziehen. Der alte Regler bleibt am Zielanker
                # orientiert und berechnet daraus die sanfte Nachladung.
                _current_day_dump_planned = (
                    any(float(s.get("grid_dump_w", 0) or 0) > 0 for s in today_slots_tl)
                )
                _curve_dump_active = bool(
                    today_0_ms in _dump_active_days
                    or _current_day_dump_planned
                    or (selected_day_offset == 0 and _existing_dump_active)
                )
                if _curve_dump_active:
                    _dump_target_soc = _dump_target_soc_by_day.get(today_0_ms)
                    if _dump_target_soc is None and selected_day_offset == 0 and existing_plan:
                        _existing_curve_meta = existing_plan.get("target_curve_meta", {}) if isinstance(existing_plan, dict) else {}
                        _dump_target_soc = existing_plan.get(
                            "predump_curve_soc",
                            _existing_curve_meta.get(
                                "predump_curve_soc",
                                existing_plan.get("morning_target"),
                            ),
                        )
                    if _dump_target_soc is None:
                        _dump_target_soc = self.predump_min_soc
                    _dump_midnight_target_soc = max(0.0, min(100.0, float(_dump_target_soc)))
                    _dump_curve_start_ts = float(pv_start_ts)
                    if self.morning_soc > 0 and curve_end_ts:
                        _configured_dump_curve_start_ts = today_0_ms + max(0.0, min(23.75, float(self.morning_hour))) * 3600000
                        if float(pv_start_ts) < _configured_dump_curve_start_ts < float(curve_end_ts):
                            _dump_curve_start_ts = float(_configured_dump_curve_start_ts)
                    _dump_anchor_soc, _dump_projected_start_soc, _dump_future_wh = self._predump_curve_start_anchor_soc(
                        _dump_midnight_target_soc,
                        today_slots_tl,
                        _dump_curve_start_ts,
                        self.capacity_wh,
                        _now_ms_for_reanchor,
                        morning_soc_tl,
                        float(self.morning_soc) if self.morning_soc > 0 else 0.0,
                    )
                    _old_morning_soc_tl = morning_soc_tl
                    morning_soc_tl = _dump_anchor_soc
                    selected_predump_curve_active = True
                    selected_predump_curve_soc = round(float(_dump_anchor_soc), 1)
                    selected_predump_midnight_target_soc = round(float(_dump_midnight_target_soc), 1)
                    selected_predump_curve_start_ts = int(float(_dump_curve_start_ts))
                    selected_predump_curve_start_soc = round(float(_dump_projected_start_soc), 1)
                    selected_predump_curve_future_dump_wh = round(float(_dump_future_wh), 0)
                    selected_predump_dump_wh = round(float(
                        _dump_dump_wh_by_day.get(
                            today_0_ms,
                            sum(max(0.0, float(s.get("grid_dump_w", 0) or 0)) * 0.25 for s in today_slots_tl),
                        )
                    ), 0)
                    selected_predump_preventable_clipping_wh = round(float(
                        _dump_preventable_wh_by_day.get(today_0_ms, selected_predump_dump_wh)
                    ), 0)
                    selected_predump_raw_pressure_wh = round(float(
                        _dump_raw_pressure_wh_by_day.get(today_0_ms, selected_predump_preventable_clipping_wh)
                    ), 0)
                    selected_predump_safe_headroom_wh = round(float(
                        _dump_safe_headroom_wh_by_day.get(today_0_ms, 0.0)
                    ), 0)
                    selected_predump_unavoidable_clipping_wh = round(float(
                        _dump_unavoidable_wh_by_day.get(today_0_ms, 0.0)
                    ), 0)
                    selected_predump_start_ts = int(_dump_start_ts_by_day.get(today_0_ms, 0) or 0)
                    selected_predump_end_ts = int(_dump_end_ts_by_day.get(today_0_ms, 0) or 0)
                    selected_predump_reason = _dump_reason_by_day.get(today_0_ms, "")
                    if not selected_predump_reason:
                        if selected_predump_dump_wh >= 200.0:
                            selected_predump_reason = (
                                "Pre-Dump geplant: %.1f kWh vorab entladen, um ca. %.1f kWh PV-Abregelung plus Regelpuffer zu vermeiden."
                                % (
                                    selected_predump_dump_wh / 1000.0,
                                    selected_predump_preventable_clipping_wh / 1000.0,
                                )
                            )
                        else:
                            selected_predump_reason = (
                                "Kein aktiver Pre-Dump nach Kurvenstart: Die adaptive Ladekurve hält den nötigen Headroom bis zum PV-Druckfenster frei."
                            )
                    if selected_predump_dump_wh >= 200.0:
                        logger.info(
                            f"Eco-Dump: Kurvenstart {_old_morning_soc_tl:.1f}%% -> {morning_soc_tl:.1f}%% "
                            f"(berechnetes Pre-Dump-Ziel {selected_predump_midnight_target_soc:.1f}%%, "
                            f"Prognose am Start {selected_predump_curve_start_soc:.1f}%%, "
                            f"Rest-Dump {selected_predump_curve_future_dump_wh/1000.0:.1f} kWh)."
                        )
                    else:
                        logger.info(
                            f"Kurvenpuffer: Ziel-SoC {morning_soc_tl:.1f}%% "
                            "ohne aktiven Pre-Dump nach Kurvenstart "
                            f"(Startprognose {selected_predump_curve_start_soc:.1f}%%)."
                        )
                else:
                    # Der normale Tagesanker kommt aus Prognose/Morgenpuffer,
                    # nicht aus dem momentanen Live-SoC. storage_morning_soc=0
                    # laesst die Prognose frei, sonst ist es die Untergrenze.
                    if forecast_only_curve:
                        logger.info(
                            f"Forecast-only-Kurve: Start-SoC {morning_soc_tl:.1f}%% "
                            "ohne Morgenpuffer, Zwischenziele oder Tagesziel-Anker; Ziel=100% bis Freilauf."
                        )
                    elif self.morning_soc > 0:
                        _old_morning_soc_tl = float(morning_soc_tl)
                        morning_soc_tl, _raised_to_floor = self._start_anchor_floor(
                            morning_soc_tl,
                            self.morning_soc,
                        )
                        if _raised_to_floor:
                            logger.info(
                                "Kurvenstart frozen: %.1f%% -> %.1f%% "
                                "(Morgenpuffer als Mindestanker, keine Live-SoC-Hochankerung)."
                                % (_old_morning_soc_tl, float(morning_soc_tl))
                            )
                        else:
                            logger.info(
                                f"Kurvenstart frozen: morning_soc_tl={morning_soc_tl:.1f}%% "
                                f"(Morgenpuffer {self.morning_soc:.0f}%%, keine Live-SoC-Hochankerung)."
                            )
                    else:
                        logger.info(
                            f"Kurvenstart: morning_soc_tl={morning_soc_tl:.1f}%% "
                            "(storage_morning_soc=0 -> kein Morgen-Deckel)"
                        )

                _old_start_anchor_soc = float(morning_soc_tl)
                morning_soc_tl, _raised_to_start_floor = self._start_anchor_floor(
                    morning_soc_tl,
                    _start_anchor_min_soc,
                )
                if _raised_to_start_floor:
                    _floor_label = (
                        "Notstrom-/Fallbackreserve"
                        if _ep_reserve_floor_soc > max(0.0, float(self.morning_soc)) + 0.05
                        else "Morgenpuffer/Notstromreserve"
                    )
                    logger.info(
                        "Kurvenstart-Floor: %.1f%% -> %.1f%% (%s als Mindestanker, keine Live-SoC-Hochankerung)."
                        % (_old_start_anchor_soc, float(morning_soc_tl), _floor_label)
                    )

                # Zwischenziele fuer den Tag einfrieren, solange sie bereits
                # erreicht wurden. Noch zukuenftige UI-Aenderungen muessen am
                # gleichen Tag wirken, statt erst nach manuellem Neustart.
                now_ms = datetime.now().timestamp() * 1000
                _plan_frozen_today = (
                    existing_plan.get('ladestart_ts') and
                    float(existing_plan.get('ladestart_ts', 0)) >= today_0_ms and
                    float(existing_plan.get('ladestart_ts', 0)) < today_end_ms
                )
                _existing_plan_emergency = bool(
                    existing_plan.get("emergency_curve_active", False)
                    or (
                        isinstance(existing_plan.get("target_curve_meta"), dict)
                        and existing_plan.get("target_curve_meta", {}).get("emergency_curve_active", False)
                    )
                    or str(existing_plan.get("forecast_trust", "")).lower() == "emergency"
                )
                _current_plan_emergency = bool(forecast_meta.get("emergency_curve_active", False))
                _configured_noon_soc = self._safe_float(
                    self.v4_config.get('storage_noon_target_soc', 0.0), 0.0
                )
                _configured_mid_soc = self._safe_float(
                    self.v4_config.get('storage_mid_target_soc', 0.0), 0.0
                )
                _configured_noon_hour = self._safe_float(
                    self.v4_config.get('storage_noon_hour', self.noon_hour), self.noon_hour
                )
                _configured_mid_hour = self._safe_float(
                    self.v4_config.get('storage_mid_hour', self.mid_hour), self.mid_hour
                )
                _skip_emergency_noon_freeze = bool(
                    _existing_plan_emergency
                    and not _current_plan_emergency
                    and _configured_noon_soc <= 0
                    and _configured_mid_soc <= 0
                )
                _intermediate_anchor_config_changed = False

                def _configured_anchor_value(source_key):
                    if source_key == 'storage_mid_target_soc':
                        return _configured_mid_soc, _configured_mid_hour
                    if source_key == 'storage_noon_target_soc':
                        return _configured_noon_soc, _configured_noon_hour
                    return None

                if (not forecast_only_curve
                        and _plan_frozen_today
                        and existing_plan.get('intermediate_anchors')
                        and not _skip_emergency_noon_freeze):
                    _frozen_intermediate_count = 0
                    for _frozen_anchor in existing_plan.get('intermediate_anchors') or []:
                        try:
                            _source_key = str(_frozen_anchor.get('source_key', ''))
                            _frozen_soc = float(_frozen_anchor.get('soc', 0.0))
                            _frozen_hour = float(_frozen_anchor.get('hour', 0.0))
                            _configured_anchor = _configured_anchor_value(_source_key)
                            if _configured_anchor is not None:
                                _configured_soc, _configured_hour = _configured_anchor
                                if self._future_intermediate_anchor_config_changed(
                                    _frozen_anchor,
                                    _configured_soc,
                                    _configured_hour,
                                    now_ms,
                                    today_0_ms,
                                ):
                                    _intermediate_anchor_config_changed = True
                                    logger.info(
                                        "Zwischenziel %s geaendert/deaktiviert: Plan %.1f%% um %.2fh, Config %.1f%% um %.2fh."
                                        % (_source_key, _frozen_soc, _frozen_hour, _configured_soc, _configured_hour)
                                    )
                                    continue
                            if _source_key == 'storage_mid_target_soc':
                                self.mid_target_soc = _frozen_soc
                                self.mid_hour = _frozen_hour
                                _frozen_intermediate_count += 1
                            elif _source_key == 'storage_noon_target_soc':
                                self.noon_target_soc = _frozen_soc
                                self.noon_hour = _frozen_hour
                                _frozen_intermediate_count += 1
                        except Exception:
                            continue
                    logger.debug("Zwischenziele eingefroren: %d Anker aus Plan." % _frozen_intermediate_count)
                elif (not forecast_only_curve
                        and _plan_frozen_today
                        and 'noon_target_soc' in existing_plan
                        and not _skip_emergency_noon_freeze):
                    _legacy_noon_hour = float(existing_plan.get('noon_hour', self.noon_hour))
                    _legacy_noon_anchor = {
                        "soc": float(existing_plan['noon_target_soc']),
                        "configured_soc": float(existing_plan['noon_target_soc']),
                        "hour": _legacy_noon_hour,
                        "ts": float(today_0_ms) + _legacy_noon_hour * 3600000.0,
                    }
                    if self._future_intermediate_anchor_config_changed(
                        _legacy_noon_anchor,
                        _configured_noon_soc,
                        _configured_noon_hour,
                        now_ms,
                        today_0_ms,
                    ):
                        _intermediate_anchor_config_changed = True
                        logger.info(
                            "Zwischenziel storage_noon_target_soc geaendert/deaktiviert: Plan %.1f%% um %.2fh, Config %.1f%% um %.2fh."
                            % (
                                float(existing_plan['noon_target_soc']),
                                _legacy_noon_hour,
                                _configured_noon_soc,
                                _configured_noon_hour,
                            )
                        )
                    else:
                        self.noon_target_soc = float(existing_plan['noon_target_soc'])
                        if 'noon_hour' in existing_plan:
                            self.noon_hour = float(existing_plan['noon_hour'])
                        logger.debug(f"noon_target_soc eingefroren: {self.noon_target_soc:.0f}%% (aus Plan, kein Zwischenziel-Sprung)")
                elif _skip_emergency_noon_freeze:
                    logger.info("Forecast wieder verfuegbar: Notkurven-Zwischenziel wird nicht in den normalen Plan uebernommen.")

                # FIX v4.6.9: ladestart_soc einfrieren (analog noon_target_soc)
                # Problem: Simulator laeuft um 14:07 neu -> morning_soc_tl = aktueller SOC (66%)
                # -> ladestart_soc wird 66% obwohl Morgen-SoC 27% war.
                # Fix: Wenn Plan fuer heute bereits existiert, alten ladestart_soc uebernehmen.
                _frozen_ladestart_soc = None
                if (
                    ((not _curve_dump_active) or selected_predump_dump_wh < 200.0)
                    and _plan_frozen_today
                    and 'ladestart_soc' in existing_plan
                    and not _predump_lock_reset_today
                ):
                    _existing_curve_anchors = existing_plan.get("curve_anchors") or []
                    if _existing_curve_anchors:
                        _old_frozen_ladestart_soc = float(_existing_curve_anchors[0].get('soc', existing_plan['ladestart_soc']))
                    else:
                        _old_frozen_ladestart_soc = float(existing_plan['ladestart_soc'])
                    if (not forecast_only_curve) and self.morning_soc > 0 and _old_frozen_ladestart_soc < float(self.morning_soc) - 0.5:
                        logger.info(
                            "ladestart_soc Reset: alter Tagesanker %.1f%% < Morgenpuffer %.1f%%."
                            % (_old_frozen_ladestart_soc, float(self.morning_soc))
                        )
                    else:
                        _frozen_ladestart_soc = _old_frozen_ladestart_soc
                        logger.debug(f"ladestart_soc eingefroren: {_frozen_ladestart_soc:.1f}%% (aus Plan, verhindert SOC-Ueberschreibung)")

                pv_duration_ms = pv_end_ts - pv_start_ts

                # FIX v4.6.9: target_timeline soll ab ladestart_ts starten, nicht ab pv_start_ts.
                # Wenn heute Morgen bewoelkt war, liegt pv_start_ts erst bei 11:00 statt 07:00.
                # Dann ist t_frac um 11:45 = 0.75h / 8.5h = 0.088 -> Soll = 33% statt ~50%.
                # Korrekt: Die Ladekurve laeuft immer ab ladestart_ts (dem tatsaechlichen Morgen-Anker).
                _timeline_start_ts = pv_start_ts
                _lts = existing_plan.get('ladestart_ts') if existing_plan else None
                if _lts:
                    _lts_f = float(_lts)
                    if today_0_ms <= _lts_f < today_end_ms:
                        # ladestart_ts ist valide fuer heute -> als Kurvenstart verwenden
                        _timeline_start_ts = min(pv_start_ts, _lts_f)
                        if _timeline_start_ts < pv_start_ts:
                            logger.info(
                                f"TL-Start: ladestart {datetime.fromtimestamp(_lts_f/1000).strftime('%H:%M')} "
                                f"< pv_start {datetime.fromtimestamp(pv_start_ts/1000).strftime('%H:%M')} "
                                f"-> Kurve ab Ladestart (bewoelkter Morgen-Fix)"
                            )

                # --- Rolling Frozen Window: stuendliche Ankerpunkte ---
                # Ersetzt die zeitbasierte t_exp-Kurve durch energieintegral-basierte Anker.
                # Anker in Vergangenheit + aktiver + naechster (Option B) werden eingefroren.
                # Nur Zukunfts-Anker werden aus aktueller Prognose aktualisiert.
                ANCHOR_STEP_MS    = int(3600 * 1000)       # 1h Schritte
                ANCHOR_FREEZE_CNT = 1                      # aktiv + naechster frozen

                # q_ratio fuer JSON-Export erhalten (grobe Einschaetzung PV-Ertrag/Kapazitaet)
                pv_w_sum     = sum(s.get('pv_w', 0) for s in today_slots_tl if pv_start_ts <= s['ts'] <= pv_end_ts)
                pv_today_kwh = (pv_w_sum * 0.25) / 1000.0
                bat_kwh      = self.capacity_wh / 1000.0
                q_ratio      = round(pv_today_kwh / bat_kwh, 2) if bat_kwh > 0 else 1.0

                _raw_intermediate_anchors = [] if forecast_only_curve else [
                    {
                        "kind": "intermediate",
                        "label": "Z1",
                        "name": "Zwischenziel 1",
                        "source_key": "storage_mid_target_soc",
                        "hour_key": "storage_mid_hour",
                        "hour": self.mid_hour,
                        "soc": self.mid_target_soc,
                    },
                    {
                        "kind": "noon",
                        "label": "Z2",
                        "name": "Zwischenziel 2",
                        "source_key": "storage_noon_target_soc",
                        "hour_key": "storage_noon_hour",
                        "hour": self.noon_hour,
                        "soc": self.noon_target_soc,
                    },
                ]
                _auto_curve_end_ts_before_user_anchors = int(curve_end_ts)
                curve_end_ts, _user_anchor_curve_end_extensions = self._extend_curve_end_for_user_anchors(
                    curve_end_ts,
                    today_0_ms,
                    today_end_ms,
                    _timeline_start_ts,
                    self.target_soc,
                    _raw_intermediate_anchors,
                    ANCHOR_STEP_MS,
                )
                if _user_anchor_curve_end_extensions:
                    _last_extension = _user_anchor_curve_end_extensions[-1]
                    logger.info(
                        "Nutzer-Zwischenziel %s um %s verschiebt Freilauf %s -> %s."
                        % (
                            _last_extension.get("label") or _last_extension.get("source_key"),
                            _last_extension.get("anchor_t"),
                            _last_extension.get("old_curve_end_t"),
                            _last_extension.get("new_curve_end_t"),
                        )
                    )

                # Morgenanker: Bis zu dieser Uhrzeit darf der E3DC autonom den
                # Morgenpuffer aufbauen. Die TL-Ladekurve startet erst hier,
                # sofern die Restprognose ab dann noch reicht. Bei schwacher
                # Prognose bleibt der fruehere PV-/Ladestart aktiv.
                morning_anchor_ts = None
                morning_anchor_delayed = False
                if self.morning_soc > 0 and not forecast_only_curve:
                    _morning_h = max(0.0, min(23.75, float(self.morning_hour)))
                    morning_anchor_ts = int(today_0_ms + (_morning_h * 3600000))
                    if _timeline_start_ts < morning_anchor_ts < curve_end_ts and now_ms < morning_anchor_ts:
                        def _rough_slot_charge_w(s):
                            raw_surplus_w = (
                                float(s.get('pv_w', 0))
                                - float(s.get('home_w', 0))
                                - float(s.get('wp_w', 0))
                                - float(s.get('climate_w', 0))
                                - _slot_planned_load_w(s)
                            )
                            weather_w = min(float(self.max_charge_w), max(0.0, raw_surplus_w))
                            planned_w = max(0.0, float(s.get('target_charge_w', s.get('charge_w', 0)) or 0))
                            return max(weather_w, planned_w)

                        _morning_anchor_soc = max(float(morning_soc_tl), float(self.morning_soc))
                        future_wh = sum(
                            _rough_slot_charge_w(s) * 0.25
                            for s in today_slots_tl
                            if morning_anchor_ts <= float(s['ts']) < float(curve_end_ts)
                        )
                        needed_wh = max(0.0, (float(self.target_soc) - _morning_anchor_soc) * (self.capacity_wh / 100.0))
                        if future_wh >= needed_wh * 0.95:
                            _timeline_start_ts = morning_anchor_ts
                            if morning_soc_tl < _morning_anchor_soc - 0.1:
                                logger.info(
                                    "Morgenanker: Start-SoC %.1f%% -> %.1f%% angehoben "
                                    "(Morgenpuffer, alter Tagesanker verworfen)."
                                    % (float(morning_soc_tl), _morning_anchor_soc)
                                )
                            morning_soc_tl = _morning_anchor_soc
                            morning_anchor_delayed = True
                            logger.info(
                                "Morgenanker: Kurve startet erst um %s "
                                "(Puffer %.1f%%, Restprognose %.0fWh >= Bedarf %.0fWh)."
                                % (datetime.fromtimestamp(morning_anchor_ts/1000).strftime('%H:%M'),
                                   float(self.morning_soc), future_wh, needed_wh)
                            )
                        else:
                            logger.info(
                                "Morgenanker: Prognose knapp -> Kurve bleibt ab %s aktiv "
                                "(Restprognose %.0fWh < Bedarf %.0fWh)."
                                % (datetime.fromtimestamp(_timeline_start_ts/1000).strftime('%H:%M'),
                                   future_wh, needed_wh)
                            )
                    else:
                        morning_anchor_ts = None
                intermediate_anchors = []
                _prev_anchor_soc = float(morning_soc_tl)
                for _candidate in sorted(_raw_intermediate_anchors, key=lambda a: float(a.get("hour", 0.0))):
                    _candidate_soc = float(_candidate.get("soc", 0.0) or 0.0)
                    if _candidate_soc <= 0.0:
                        continue
                    _anchor_h = max(0.0, min(23.75, float(_candidate.get("hour", 0.0))))
                    _anchor_ts = int(today_0_ms + (_anchor_h * 3600000))
                    if _timeline_start_ts < _anchor_ts < curve_end_ts:
                        # Nur sinnvolle Zwischenziele erzwingen: nicht unter Start
                        # und nicht ueber dem Tagesziel.
                        _anchor_soc = min(
                            float(self.target_soc),
                            max(float(_prev_anchor_soc), _candidate_soc),
                        )
                        _anchor = {
                            "ts": int(_anchor_ts),
                            "t": datetime.fromtimestamp(_anchor_ts / 1000).strftime("%H:%M"),
                            "hour": round(float(_anchor_h), 2),
                            "soc": round(float(_anchor_soc), 2),
                            "configured_soc": round(float(_candidate_soc), 2),
                            "kind": _candidate["kind"],
                            "label": _candidate["label"],
                            "name": _candidate["name"],
                            "source_key": _candidate["source_key"],
                            "hour_key": _candidate["hour_key"],
                        }
                        intermediate_anchors.append(_anchor)
                        _prev_anchor_soc = float(_anchor_soc)
                    else:
                        logger.info(
                            "%s ignoriert: %.2fh liegt ausserhalb der Kurve (%s-%s)."
                            % (
                                _candidate["name"],
                                _anchor_h,
                                datetime.fromtimestamp(_timeline_start_ts / 1000).strftime("%H:%M"),
                                datetime.fromtimestamp(curve_end_ts / 1000).strftime("%H:%M"),
                            )
                        )
                noon_anchor = next((a for a in intermediate_anchors if a.get("source_key") == "storage_noon_target_soc"), None)
                noon_anchor_ts = int(noon_anchor["ts"]) if noon_anchor else None
                noon_anchor_soc = float(noon_anchor["soc"]) if noon_anchor else None

                # Hilfsfunktion: SOC-Prognose via kumulativen Energie-Integral
                # until_ts: Zeitpunkt bis zu dem integriert wird
                # Basis immer morning_soc_tl + geplante Ladeenergie bis until_ts.
                # (base_soc/base_ts Parameter entfernt - immer ab Tag-Start rechnen)
                def _slot_charge_w(s):
                    """Wetter/ML-Nettoenergie fuer eine glatte Sollkurve."""
                    raw_surplus_w = (
                        float(s.get('pv_w', 0))
                        - float(s.get('home_w', 0))
                        - float(s.get('wp_w', 0))
                        - float(s.get('climate_w', 0))
                        - _slot_planned_load_w(s)
                    )
                    weather_w = min(float(self.max_charge_w), max(0.0, raw_surplus_w))
                    planned_w = 0.0
                    if 'target_charge_w' in s:
                        planned_w = max(0.0, float(s.get('target_charge_w', 0)))
                    elif float(s.get('charge_w', 0)) > 0:
                        planned_w = max(0.0, float(s.get('charge_w', 0)))
                    return max(weather_w, planned_w)

                def _energy_between(start_ts, end_ts):
                    return sum(
                        _slot_charge_w(s) * 0.25
                        for s in today_slots_tl
                        if float(start_ts) <= float(s['ts']) < float(end_ts)
                    )

                def _segment_soc(start_ts, end_ts, start_soc, end_soc, until_ts):
                    """Verteilt ein SoC-Ziel innerhalb eines Zeitfensters nach Wetter/ML-Energie."""
                    start_ts = float(start_ts)
                    end_ts = float(end_ts)
                    until_ts = max(start_ts, min(float(until_ts), end_ts))
                    if end_ts <= start_ts:
                        return float(end_soc)

                    total_wh = _energy_between(start_ts, end_ts)
                    if total_wh > 0:
                        frac = _energy_between(start_ts, until_ts) / total_wh
                    else:
                        frac = (until_ts - start_ts) / (end_ts - start_ts)
                    frac = max(0.0, min(1.0, frac))
                    return float(start_soc) + (float(end_soc) - float(start_soc)) * frac

                curve_path_points = (
                    [{"ts": int(_timeline_start_ts), "soc": float(morning_soc_tl)}]
                    + [dict(a) for a in intermediate_anchors]
                    + [{"ts": int(curve_end_ts), "soc": float(self.target_soc)}]
                )

                def _soc_at_ts(until_ts):
                    """Soll-SoC bei until_ts; optional mit festen Zwischenzielen."""
                    if intermediate_anchors:
                        _until = float(until_ts)
                        for _idx in range(len(curve_path_points) - 1):
                            _p0 = curve_path_points[_idx]
                            _p1 = curve_path_points[_idx + 1]
                            if _until <= float(_p1["ts"]):
                                return _segment_soc(
                                    float(_p0["ts"]),
                                    float(_p1["ts"]),
                                    float(_p0["soc"]),
                                    float(_p1["soc"]),
                                    _until,
                                )
                        return float(self.target_soc)

                    # Ohne Zwischenziel: Wetter/ML-Ladeintegral ueber das ganze PV-Fenster.
                    planned_wh = sum(
                        _slot_charge_w(s) * 0.25
                        for s in today_slots_tl
                        if float(s['ts']) < float(until_ts)
                    )

                    total_planned_wh = sum(
                        _slot_charge_w(s) * 0.25
                        for s in today_slots_tl
                        if float(s['ts']) < float(curve_end_ts)
                    )

                    needed_wh = (float(self.target_soc) - float(morning_soc_tl)) * (self.capacity_wh / 100.0)

                    if total_planned_wh > needed_wh and total_planned_wh > 0:
                        # Geplante Energie ist groesser als Bedarf -> Kurve ueber den Tag strecken.
                        fraction = planned_wh / total_planned_wh
                        gain = fraction * (float(self.target_soc) - float(morning_soc_tl))
                    else:
                        # Prognose reicht knapp/nicht -> alles laden was nach Wetter/ML kommt.
                        gain = (planned_wh / self.capacity_wh) * 100.0 if self.capacity_wh > 0 else 0.0

                    return min(float(self.target_soc), float(morning_soc_tl) + gain)

                def _apply_user_curve_anchors(anchors):
                    """Fuegt konfigurierte Zwischenziele in die Kurve ein."""
                    if not intermediate_anchors:
                        return anchors
                    anchors = [dict(a) for a in anchors]
                    for configured in intermediate_anchors:
                        _matched = False
                        for anchor in anchors:
                            if abs(int(float(anchor.get("ts", 0))) - int(configured["ts"])) <= 60000:
                                anchor.update({
                                    "ts": int(configured["ts"]),
                                    "t": configured["t"],
                                    "kind": configured["kind"],
                                    "label": configured["label"],
                                    "name": configured["name"],
                                    "source_key": configured["source_key"],
                                    "hour_key": configured["hour_key"],
                                    "hour": configured["hour"],
                                    "configured_soc": configured["configured_soc"],
                                })
                                if not anchor.get("frozen"):
                                    anchor["soc"] = round(float(configured["soc"]), 2)
                                _matched = True
                                break
                        if not _matched:
                            anchors.append({
                                "ts": int(configured["ts"]),
                                "t": configured["t"],
                                "soc": round(float(configured["soc"]), 2),
                                "frozen": False,
                                "kind": configured["kind"],
                                "label": configured["label"],
                                "name": configured["name"],
                                "source_key": configured["source_key"],
                                "hour_key": configured["hour_key"],
                                "hour": configured["hour"],
                                "configured_soc": configured["configured_soc"],
                            })
                    return sorted(anchors, key=lambda a: int(float(a["ts"])))

                # Alle Anker fuer den heutigen Tag generieren (erster Lauf oder Reset).
                # Ohne Zwischenziel folgt die Kurve dem Wetter/ML-Energieintegral.
                # Mit Zwischenzielen wird daraus: Ladestart -> Zwischenziele -> Tagesziel.
                def _generate_fresh_anchors():
                    anchors = []
                    ts = int(_timeline_start_ts)
                    while ts < int(curve_end_ts):
                        soc_tgt = _soc_at_ts(ts)
                        anchors.append({
                            "ts":     ts,
                            "t":      datetime.fromtimestamp(ts / 1000).strftime("%H:%M"),
                            "soc":    round(soc_tgt, 2),
                            "frozen": False
                        })
                        ts += ANCHOR_STEP_MS
                    # Letzter Anker: genau curve_end_ts mit target_soc
                    anchors.append({
                        "ts":     int(curve_end_ts),
                        "t":      datetime.fromtimestamp(curve_end_ts / 1000).strftime("%H:%M"),
                        "soc":    round(self.target_soc, 2),
                        "frozen": False
                    })
                    # Erster Anker: morning_soc_tl als fixierter Startpunkt
                    if anchors:
                        anchors[0]["soc"] = round(morning_soc_tl, 2)
                    return _apply_user_curve_anchors(anchors)

                # Einfrierung anwenden (Option B): aktiver + naechster Anker einfrieren
                def _apply_freeze(anchors):
                    active_idx = 0
                    for i, a in enumerate(anchors):
                        if float(a["ts"]) <= now_ms:
                            active_idx = i
                    freeze_until = min(active_idx + ANCHOR_FREEZE_CNT, len(anchors) - 1)
                    for i in range(freeze_until + 1):
                        anchors[i]["frozen"] = True
                    return anchors, active_idx

                # Existierende Anker laden (aus bestehendem Plan, falls vorhanden)
                existing_anchors = existing_plan.get("curve_anchors", []) if existing_plan else []
                _anchor_step_ok = True
                if len(existing_anchors) >= 2:
                    try:
                        _first_step = int(float(existing_anchors[1].get("ts", 0)) - float(existing_anchors[0].get("ts", 0)))
                        _anchor_step_ok = abs(_first_step - ANCHOR_STEP_MS) <= 60000
                    except Exception:
                        _anchor_step_ok = False
                published_curve_reset_allowed = bool(
                    _intermediate_anchor_config_changed
                    or _predump_lock_reset_today
                    or selected_predump_dump_wh >= 200.0
                )
                if published_curve_reset_allowed:
                    if _intermediate_anchor_config_changed:
                        published_curve_reset_reason = "intermediate_anchor_changed"
                    elif _predump_lock_reset_today:
                        published_curve_reset_reason = "predump_lock_reset"
                    else:
                        published_curve_reset_reason = "active_predump"
                    published_curve_floor_reason = published_curve_reset_reason
                    published_curve_floor_active = False
                    published_curve_floor_points = []
                else:
                    _published_floor = self._published_curve_floor_from_plan(
                        existing_plan,
                        today_0_ms,
                        today_end_ms,
                        _curve_meta_mode,
                        _curve_start_policy,
                        planning_target_soc,
                    )
                    published_curve_floor_active = bool(_published_floor.get("active"))
                    published_curve_floor_points = list(_published_floor.get("points") or [])
                    published_curve_floor_source = str(_published_floor.get("source") or "")
                    published_curve_floor_reason = str(_published_floor.get("reason") or "")
                    if published_curve_floor_active:
                        logger.info(
                            "Veröffentlichte Ladekurve als Mindestfahrplan aktiv: %d Punkte aus %s."
                            % (len(published_curve_floor_points), published_curve_floor_source)
                        )
                _anchors_for_today = (
                    existing_anchors and
                    _anchor_step_ok and
                    existing_meta.get("mode") == _curve_meta_mode and
                    _existing_curve_contract_ok and
                    existing_plan.get("ladestart_ts") and
                    today_0_ms <= float(existing_plan.get("ladestart_ts", 0)) < today_end_ms
                )
                if (
                    _anchors_for_today
                    and forecast_only_curve
                    and self._cfg_bool(self.v4_config.get("storage_forecast100_late_full_guard_enable"), True)
                    and existing_meta.get("forecast100_late_full_guard_policy") != "forecast100_late_full_v1"
                ):
                    _anchors_for_today = False
                    logger.info(
                        "Rolling Window Reset: Forecast-100 Vollstand-Schutz aktiviert; "
                        "zukünftige Anker werden neu auf spätes 100%%-Ziel geplant."
                    )
                _anchor_reset_allowed = True
                try:
                    if existing_anchors:
                        _existing_first_anchor_ts = float(existing_anchors[0].get("ts", 0) or 0)
                        if _existing_first_anchor_ts and now_ms >= _existing_first_anchor_ts:
                            _anchor_reset_allowed = False
                except Exception:
                    _anchor_reset_allowed = False
                if _anchors_for_today and _intermediate_anchor_config_changed:
                    _anchors_for_today = False
                    logger.info("Rolling Window Reset: zukuenftiges Zwischenziel wurde geaendert oder deaktiviert.")
                if _anchors_for_today and not _anchor_reset_allowed and _frozen_ladestart_soc is not None:
                    try:
                        _frozen_start_soc = round(float(_frozen_ladestart_soc), 2)
                        _first_anchor_soc = float(existing_anchors[0].get("soc", _frozen_start_soc))
                        if abs(_first_anchor_soc - _frozen_start_soc) > 0.5:
                            existing_anchors[0]["soc"] = _frozen_start_soc
                            if len(existing_anchors) > 1:
                                _next_anchor_soc = float(existing_anchors[1].get("soc", _frozen_start_soc))
                                if _next_anchor_soc < _frozen_start_soc - 0.1:
                                    existing_anchors[1]["soc"] = _frozen_start_soc
                            logger.warning(
                                "Rolling Window Konsistenz-Fix: eingefrorener Tagesanker %.1f%% "
                                "wieder in die aktiven Anker uebernommen; kein Startanker-Reset nach Kurvenstart."
                                % _frozen_start_soc
                            )
                    except Exception:
                        pass
                if _anchors_for_today and not _anchor_reset_allowed and existing_anchors:
                    try:
                        _reserve_start_floor = round(float(_start_anchor_min_soc), 2)
                        _first_anchor_soc = float(existing_anchors[0].get("soc", _reserve_start_floor))
                        if _first_anchor_soc < _reserve_start_floor - 0.5:
                            existing_anchors[0]["soc"] = _reserve_start_floor
                            if len(existing_anchors) > 1:
                                _next_anchor_soc = float(existing_anchors[1].get("soc", _reserve_start_floor))
                                if _next_anchor_soc < _reserve_start_floor - 0.1:
                                    existing_anchors[1]["soc"] = _reserve_start_floor
                            logger.warning(
                                "Rolling Window Konsistenz-Fix: Startanker %.1f%% auf %.1f%% angehoben "
                                "(Notstrom-/Fallbackreserve als Mindestanker; kein Startanker-Reset nach Kurvenstart)."
                                % (_first_anchor_soc, _reserve_start_floor)
                            )
                    except Exception:
                        pass
                if _anchors_for_today:
                    _existing_curve_end = existing_meta.get("curve_end_ts") or existing_plan.get("ladeende_ts")
                    if not _existing_curve_end:
                        _anchors_for_today = False
                        logger.info("Rolling Window Reset: Plan ohne explizites Ladeende verworfen.")
                    elif _anchor_reset_allowed and abs(float(_existing_curve_end) - float(curve_end_ts)) > 15 * 60000:
                        _anchors_for_today = False
                        logger.info(
                            "Rolling Window Reset: Ladeende %s -> %s."
                            % (
                                datetime.fromtimestamp(float(_existing_curve_end) / 1000).strftime("%H:%M"),
                                datetime.fromtimestamp(float(curve_end_ts) / 1000).strftime("%H:%M")
                            )
                        )
                    else:
                        try:
                            _existing_target = float(existing_plan.get(
                                "planning_target_soc",
                                existing_plan.get("target_soc", config_target_soc)
                            ))
                            if _anchor_reset_allowed and abs(_existing_target - float(planning_target_soc)) > 0.5:
                                _anchors_for_today = False
                                logger.info(
                                    "Rolling Window Reset: Speicherziel %.1f%% -> %.1f%%."
                                    % (_existing_target, float(planning_target_soc))
                                )
                        except Exception:
                            pass
                if (_anchors_for_today and _anchor_reset_allowed and morning_anchor_delayed and morning_anchor_ts
                        and existing_anchors
                        and float(existing_anchors[0].get("ts", 0)) < float(morning_anchor_ts)):
                    _anchors_for_today = False
                    logger.info(
                        "Rolling Window Reset: Morgenanker %s ersetzt fruehere Tagesanker."
                        % datetime.fromtimestamp(morning_anchor_ts / 1000).strftime("%H:%M")
                    )
                if (_anchors_for_today and _anchor_reset_allowed and morning_anchor_delayed and morning_anchor_ts
                        and existing_anchors
                        and abs(float(existing_anchors[0].get("ts", 0)) - float(morning_anchor_ts)) > 60000):
                    _anchors_for_today = False
                    logger.info(
                        "Rolling Window Reset: erster Multi-Anker %s passt nicht zum Morgenanker %s."
                        % (
                            datetime.fromtimestamp(float(existing_anchors[0].get("ts", 0)) / 1000).strftime("%H:%M"),
                            datetime.fromtimestamp(morning_anchor_ts / 1000).strftime("%H:%M")
                        )
                    )
                if (_anchors_for_today and _anchor_reset_allowed and existing_anchors
                        and float(existing_anchors[0].get("soc", 0)) < float(_start_anchor_min_soc) - 0.5):
                    _anchors_for_today = False
                    logger.info(
                        "Rolling Window Reset: alter Startanker %.1f%% < Startanker-Floor %.1f%% "
                        "(Morgenpuffer/Notstrom-/Fallbackreserve)."
                        % (float(existing_anchors[0].get("soc", 0)), float(_start_anchor_min_soc))
                    )
                if (_anchors_for_today and _anchor_reset_allowed and _curve_dump_active
                        and selected_predump_dump_wh >= 200.0 and existing_anchors):
                    try:
                        _predump_first_anchor_soc = float(existing_anchors[0].get("soc", morning_soc_tl))
                        if abs(_predump_first_anchor_soc - float(morning_soc_tl)) > 0.5:
                            _anchors_for_today = False
                            logger.info(
                                "Rolling Window Reset: Pre-Dump-Ziel %.1f%% ersetzt alten Startanker %.1f%%."
                                % (float(morning_soc_tl), _predump_first_anchor_soc)
                            )
                    except Exception:
                        pass
                if (_anchors_for_today and existing_anchors):
                    try:
                        _first_anchor_ts = float(existing_anchors[0].get("ts", 0))
                        _first_anchor_soc = float(existing_anchors[0].get("soc", 0))
                        _new_start_soc = float(morning_soc_tl)
                        if (_anchor_reset_allowed
                                and now_ms < _first_anchor_ts
                                and abs(_first_anchor_soc - _new_start_soc) > 0.5):
                            _anchors_for_today = False
                            logger.info(
                                "Rolling Window Reset: Startanker %.1f%% -> %.1f%% "
                                "(Konfig/Pre-Dump vor Ladestart geaendert)."
                                % (_first_anchor_soc, _new_start_soc)
                            )
                    except Exception:
                        pass
                try:
                    _bad_zero_anchor = (
                        self.morning_soc <= 0
                        and current_soc > 20.0
                        and existing_anchors
                        and float(existing_anchors[0].get("soc", 0)) <= 1.0
                    )
                except Exception:
                    _bad_zero_anchor = False
                if _bad_zero_anchor and _anchor_reset_allowed:
                    _anchors_for_today = False
                    logger.info(
                        "Rolling Window Reset: vorhandene 0%-Anker verworfen "
                        f"(storage_morning_soc=0, aktueller SoC={current_soc:.1f}%%)."
                    )

                if _anchors_for_today:
                    # Folgelauf: Anker aus bestehendem Plan uebernehmen. Alte
                    # tail_target_start-Hilfsanker waren nur ein transienter
                    # Glaettungsfuss und duerfen nicht als echte Tagesanker einfrieren.
                    curve_anchors = _apply_user_curve_anchors([
                        a for a in existing_anchors
                        if str((a or {}).get("kind") or "") != "tail_target_start"
                    ])
                    curve_anchors, active_idx = _apply_freeze(curve_anchors)

                    # Nicht-eingefrorene Anker aus aktueller Prognose aktualisieren
                    _updated = 0
                    _intermediate_by_source = {
                        str(anchor.get("source_key")): anchor
                        for anchor in intermediate_anchors
                        if anchor.get("source_key")
                    }
                    for anchor in curve_anchors:
                        _configured_anchor = _intermediate_by_source.get(str(anchor.get("source_key", "")))
                        if _configured_anchor is not None:
                            if not anchor.get("frozen"):
                                anchor["soc"] = round(float(_configured_anchor["soc"]), 2)
                            anchor["t"] = datetime.fromtimestamp(float(anchor["ts"]) / 1000).strftime("%H:%M")
                            continue
                        if not anchor.get("frozen"):
                            old_soc = anchor["soc"]
                            new_soc = round(_soc_at_ts(anchor["ts"]), 2)
                            if published_curve_floor_active:
                                floor_soc = self._curve_soc_at_points(
                                    published_curve_floor_points,
                                    anchor["ts"],
                                    None,
                                )
                                if floor_soc is not None and new_soc < float(floor_soc) - 0.05:
                                    published_curve_anchor_clamps += 1
                                    published_curve_max_lift_pct = max(
                                        published_curve_max_lift_pct,
                                        round(float(floor_soc) - new_soc, 2),
                                    )
                                    new_soc = round(float(floor_soc), 2)
                            anchor["soc"] = new_soc
                            anchor["t"] = datetime.fromtimestamp(float(anchor["ts"]) / 1000).strftime("%H:%M")
                            if abs(anchor["soc"] - old_soc) > 0.5:
                                logger.info(
                                    f"Anker {anchor['t']}: SOC {old_soc:.1f}%% -> {anchor['soc']:.1f}%% "
                                    f"(Prognose-Update)"
                                )
                            _updated += 1
                    _frozen_count = sum(1 for a in curve_anchors if a.get("frozen"))
                    logger.info(
                        f"Rolling Window: {_frozen_count}/{len(curve_anchors)} Anker eingefroren, "
                        f"{_updated} aktualisiert"
                    )
                else:
                    # Erster Lauf des Tages oder Reset: alle Anker frisch berechnen
                    curve_anchors = _generate_fresh_anchors()
                    curve_anchors, _ = _apply_freeze(curve_anchors)
                    logger.info(
                        f"Neue Anker generiert: {len(curve_anchors)} Punkte (1h-Schritte), "
                        f"Ladestart={datetime.fromtimestamp(_timeline_start_ts/1000).strftime('%H:%M')} "
                        f"Freilauf={datetime.fromtimestamp(curve_end_ts/1000).strftime('%H:%M')}"
                    )

                if curve_anchors:
                    curve_anchors[-1]["soc"] = round(float(self.target_soc), 2)
                    curve_anchors[-1]["t"] = datetime.fromtimestamp(
                        float(curve_anchors[-1]["ts"]) / 1000
                    ).strftime("%H:%M")
                if curve_anchors:
                    try:
                        _reserve_start_floor = round(float(_start_anchor_min_soc), 2)
                        _first_anchor_soc = float(curve_anchors[0].get("soc", morning_soc_tl))
                        if _first_anchor_soc < _reserve_start_floor - 0.05:
                            curve_anchors[0]["soc"] = _reserve_start_floor
                            if len(curve_anchors) > 1:
                                _next_anchor_soc = float(curve_anchors[1].get("soc", _reserve_start_floor))
                                if _next_anchor_soc < _reserve_start_floor - 0.1:
                                    curve_anchors[1]["soc"] = _reserve_start_floor
                            logger.info(
                                "Startanker-Floor: %.1f%% -> %.1f%% "
                                "(Notstrom-/Fallbackreserve als Mindestanker uebernommen)."
                                % (_first_anchor_soc, _reserve_start_floor)
                            )
                    except Exception:
                        pass
                if curve_anchors:
                    storm_guard, curve_anchors, storm_grid_charge = self._storm_guard_plan(
                        today_slots_tl,
                        curve_anchors,
                        current_soc,
                        now_ms,
                        _timeline_start_ts,
                        curve_end_ts,
                )
                tail_target_smoothing_points = 0
                if curve_anchors:
                    tail_target_smoothing_points = self._smooth_tail_target_anchors(
                        curve_anchors,
                        now_ms,
                        self.target_soc,
                        current_soc,
                        frontload_factor=_curve_tail_frontload_factor,
                    )
                    if tail_target_smoothing_points:
                        logger.info(
                            "Tagesziel-Glaettung: %d spaete Anker auf gleichmaessigen Zielpfad angehoben."
                            % tail_target_smoothing_points
                        )
                if curve_anchors and published_curve_floor_active:
                    _anchor_floor_diag = self._protect_curve_points_against_floor(
                        curve_anchors,
                        published_curve_floor_points,
                        self.target_soc,
                    )
                    if _anchor_floor_diag.get("points_clamped"):
                        published_curve_anchor_clamps += int(_anchor_floor_diag["points_clamped"])
                        published_curve_max_lift_pct = max(
                            published_curve_max_lift_pct,
                            float(_anchor_floor_diag.get("max_lift_pct", 0.0) or 0.0),
                        )
                        logger.info(
                            "Veröffentlichte Ladekurve schützt %d Anker vor Prognose-Absenkung "
                            "(max %.1f%%)."
                            % (
                                int(_anchor_floor_diag["points_clamped"]),
                                float(_anchor_floor_diag.get("max_lift_pct", 0.0) or 0.0),
                            )
                        )
                _meta_morning_anchor_soc = round(float(morning_soc_tl), 2)
                _meta_start_anchor_ts = None
                _meta_start_anchor_t = ""
                _meta_start_anchor_kind = ""
                if curve_anchors:
                    _meta_start_anchor_ts = int(float(curve_anchors[0].get("ts", _timeline_start_ts)))
                    _meta_start_anchor_t = datetime.fromtimestamp(_meta_start_anchor_ts / 1000).strftime("%H:%M")
                    _meta_start_anchor_kind = str(curve_anchors[0].get("kind") or "hourly")
                    _meta_morning_anchor_soc = round(
                        float(curve_anchors[0].get("soc", morning_soc_tl)),
                        2,
                    )

                target_curve_meta = {
                    "mode": _curve_meta_mode,
                    "target_mode": "forecast_100" if forecast_only_curve else "anchored",
                    "forecast_only_target_active": bool(forecast_only_curve),
                    "forecast_only_no_user_anchors": bool(forecast_only_curve),
                    "forecast_only_target_soc": 100.0 if forecast_only_curve else 0.0,
                    "curve_day_label": selected_day_label,
                    "curve_day_offset": selected_day_offset,
                    "curve_day_start_ts": int(today_0_ms),
                    "anchor_step_min": int(ANCHOR_STEP_MS / 60000),
                    "curve_start_policy": _curve_start_policy,
                    "live_reanchor_enabled": False,
                    "live_reanchor_policy": "frozen_start_anchor_no_auto_reanchor",
                    "live_reanchored_today": bool(_live_reanchored_today),
                    "live_reanchor_blocked": bool(_live_reanchor_blocked_reason),
                    "live_reanchor_reason": _live_reanchor_reason,
                    "live_reanchor_old_soc": _live_reanchor_old_soc,
                    "live_reanchor_new_soc": _live_reanchor_new_soc,
                    "live_reanchor_threshold_pct": round(float(_live_reanchor_threshold_pct), 1),
                    "live_reanchor_start_ts": _live_reanchor_start_ts,
                    "live_reanchor_before_morning_blocked": bool(_live_reanchor_before_morning_blocked),
                    "live_reanchor_blocked_reason": _live_reanchor_blocked_reason,
                    "start_anchor_ts": _meta_start_anchor_ts,
                    "start_anchor_t": _meta_start_anchor_t,
                    "start_anchor_soc": _meta_morning_anchor_soc,
                    "start_anchor_kind": _meta_start_anchor_kind,
                    "frozen_anchor_count": sum(1 for a in curve_anchors if a.get("frozen")),
                    "lookahead_h": 2.0,
                    "basis": "raw_net_surplus_from_pv_ml_weather",
                    "pv_forecast_kwh": round(float(pv_today_kwh), 2),
                    "curve_end_ts": int(curve_end_ts),
                    "curve_end_t": datetime.fromtimestamp(curve_end_ts / 1000).strftime("%H:%M"),
                    "curve_end_auto_ts": int(_auto_curve_end_ts_before_user_anchors),
                    "curve_end_auto_t": datetime.fromtimestamp(_auto_curve_end_ts_before_user_anchors / 1000).strftime("%H:%M"),
                    "curve_end_user_anchor_extended": bool(_user_anchor_curve_end_extensions),
                    "curve_end_user_anchor_extensions": [dict(item) for item in _user_anchor_curve_end_extensions],
                    "auto_release_offset_min": int(LADEENDE_OFFSET_MS / 60000),
                    "curve_end_guard_min": round(float(_curve_end_guard_min), 1),
                    "curve_end_relevant_surplus_w": round(float(_curve_relevant_surplus_w), 0),
                    "curve_tail_frontload_factor": round(float(_curve_tail_frontload_factor), 2),
                    "forecast100_late_full_guard_active": bool(_forecast100_late_full_guard_active),
                    "forecast100_late_full_guard_policy": _forecast100_late_full_guard_policy,
                    "forecast100_late_full_guard_old_end_ts": _forecast100_late_full_guard_old_end_ts,
                    "forecast100_late_full_guard_old_end_t": (
                        datetime.fromtimestamp(_forecast100_late_full_guard_old_end_ts / 1000).strftime("%H:%M")
                        if _forecast100_late_full_guard_old_end_ts
                        else ""
                    ),
                    "morning_anchor_active": bool(morning_anchor_ts),
                    "morning_anchor_delayed": bool(morning_anchor_delayed),
                    "morning_anchor_t": datetime.fromtimestamp(morning_anchor_ts / 1000).strftime("%H:%M") if morning_anchor_ts else "",
                    "morning_anchor_soc": _meta_morning_anchor_soc,
                    "config_morning_soc": round(float(self.morning_soc), 2) if self.morning_soc > 0 else 0.0,
                    "intermediate_anchor_active": bool(intermediate_anchors),
                    "intermediate_anchor_count": len(intermediate_anchors),
                    "intermediate_anchors": [dict(a) for a in intermediate_anchors],
                    "noon_anchor_active": bool(noon_anchor_ts and noon_anchor_soc is not None),
                    "noon_anchor_t": datetime.fromtimestamp(noon_anchor_ts / 1000).strftime("%H:%M") if noon_anchor_ts else "",
                    "noon_anchor_soc": round(float(noon_anchor_soc), 2) if noon_anchor_soc is not None else 0.0,
                    "predump_curve_active": bool(selected_predump_curve_active),
                    "predump_curve_soc": selected_predump_curve_soc,
                    "predump_midnight_target_soc": selected_predump_midnight_target_soc,
                    "predump_curve_start_ts": selected_predump_curve_start_ts,
                    "predump_curve_start_soc": selected_predump_curve_start_soc,
                    "predump_curve_future_dump_wh": selected_predump_curve_future_dump_wh,
                    "predump_dump_wh": selected_predump_dump_wh,
                    "predump_preventable_clipping_wh": selected_predump_preventable_clipping_wh,
                    "predump_raw_pressure_wh": selected_predump_raw_pressure_wh,
                    "predump_safe_headroom_wh": selected_predump_safe_headroom_wh,
                    "predump_unavoidable_clipping_wh": selected_predump_unavoidable_clipping_wh,
                    "predump_reason": selected_predump_reason,
                    "predump_start_ts": selected_predump_start_ts,
                    "predump_end_ts": selected_predump_end_ts,
                    "hard_predump_enabled": bool(self.predump_enabled and self.hard_predump_enabled),
                    "hard_predump_target_soc": round(float(self.hard_predump_target_soc), 1),
                    "hard_predump_grid_enabled": self._cfg_bool(self.v4_config.get("hard_predump_grid_enable"), False),
                    "storm_guard": storm_guard,
                    "published_curve_floor_policy": published_curve_floor_policy,
                    "published_curve_floor_active": bool(published_curve_floor_active),
                    "published_curve_floor_source": published_curve_floor_source,
                    "published_curve_floor_reason": published_curve_floor_reason,
                    "published_curve_floor_reset_allowed": bool(published_curve_reset_allowed),
                    "published_curve_floor_reset_reason": published_curve_reset_reason,
                    "published_curve_anchor_clamps": int(published_curve_anchor_clamps),
                    "published_curve_timeline_clamps": int(published_curve_timeline_clamps),
                    "published_curve_max_lift_pct": round(float(published_curve_max_lift_pct), 2),
                }
                if tail_target_smoothing_points:
                    target_curve_meta["tail_target_smoothing"] = True
                    target_curve_meta["tail_target_smoothing_points"] = int(tail_target_smoothing_points)
                target_curve_meta.update(forecast_meta)
                if wallbox_floor_soc_active:
                    target_curve_meta.update({
                        "wallbox_floor_soc_active": True,
                        "wallbox_floor_soc": round(float(wallbox_target_soc), 1),
                        "wallbox_floor_reason": "PV + Akku bis Untergrenze: Wallbox-Stütze endet an der Hausakku-Reserve",
                        "wallbox_config_target_reachable": bool(_config_target_reachable),
                        "wallbox_config_target_reach": _config_target_reach_meta,
                    })
                if wallbox_target_soc_active:
                    target_curve_meta.update({
                        "wallbox_target_soc_active": True,
                        "wallbox_target_soc": round(float(wallbox_target_soc), 1),
                        "wallbox_target_reason": "PV + Akku bis Untergrenze: Tagesziel laut Restprognose nicht erreichbar, Rückfall auf Hausakku-Reserve",
                    })

                # --- target_timeline aus Ankern interpolieren (15-Min-Slots) ---
                # Lineares Interpolieren zwischen benachbarten Ankerpunkten.
                # Der storage_manager liest target_timeline unveraendert (kein Code-Aenderung noetig).
                all_day_slots = [s for s in timeline if today_0_ms <= s['ts'] < today_end_ms]
                if len(curve_anchors) >= 2:
                    for i in range(len(curve_anchors) - 1):
                        a0 = curve_anchors[i]
                        a1 = curve_anchors[i + 1]
                        dur_ms = max(1, int(a1["ts"]) - int(a0["ts"]))
                        ts = int(a0["ts"])
                        while ts < int(a1["ts"]):
                            frac = (ts - int(a0["ts"])) / dur_ms
                            soc  = a0["soc"] + (a1["soc"] - a0["soc"]) * frac
                            target_timeline.append({"ts": ts, "soc": round(min(soc, self.target_soc), 2)})
                            ts += 900000  # 15-Min-Slots
                    # Letzten Anker anhaengen (= curve_end_ts = Freilauf = target_soc)
                    target_timeline.append({"ts": int(curve_anchors[-1]["ts"]), "soc": round(self.target_soc, 2)})
                    # Nach Freilauf: 2 Ankerpunkte (+15min, +60min) fuer Chart-Darstellung
                    for extra_ms in [900000, 3600000]:
                        target_timeline.append({"ts": int(curve_anchors[-1]["ts"]) + extra_ms, "soc": round(self.target_soc, 2)})

                # --- Historische Punkte voranstellen ---
                # Problem: today_slots_tl startet ab jetzt (14:00), nicht ab 07:00.
                # Chart haette vor 14:00 leere Kurve. Fix: lineare Interpolation 07:00->erster Slot.
                if target_timeline and (target_timeline[0]['ts'] - _timeline_start_ts) > 60000:
                    _hist_start_ts  = int(_timeline_start_ts)
                    _hist_end_ts    = target_timeline[0]['ts']
                    _hist_end_soc   = float(target_timeline[0].get('soc', morning_soc_tl))
                    _hist_raw_start_soc = float(morning_soc_tl)
                    _hist_start_soc = historical_curve_start_soc(_hist_raw_start_soc, _hist_end_soc)
                    if _hist_start_soc < _hist_raw_start_soc - 0.05:
                        target_curve_meta["historical_points_clamped_to_first_anchor"] = True
                        target_curve_meta["historical_points_raw_start_soc"] = round(_hist_raw_start_soc, 1)
                        target_curve_meta["historical_points_anchor_soc"] = round(_hist_end_soc, 1)
                    _hist_dur_ms    = max(1, _hist_end_ts - _hist_start_ts)
                    _slot_ms        = 900000
                    _hist_ts        = _hist_start_ts
                    _hist_slots     = []
                    while _hist_ts < _hist_end_ts:
                        _frac = (_hist_ts - _hist_start_ts) / _hist_dur_ms
                        _soc  = _hist_start_soc + (_hist_end_soc - _hist_start_soc) * _frac
                        _hist_slots.append({'ts': _hist_ts, 'soc': round(_soc, 1)})
                        _hist_ts += _slot_ms
                    target_timeline = _hist_slots + target_timeline
                    logger.info(
                        f"Histpunkte: {len(_hist_slots)} Slots "
                        f"{datetime.fromtimestamp(_hist_start_ts/1000).strftime('%H:%M')} "
                        f"({_hist_start_soc:.1f}%%) -> "
                        f"{datetime.fromtimestamp(_hist_end_ts/1000).strftime('%H:%M')} "
                        f"({_hist_end_soc:.1f}%%)"
                    )

            # --- Rueckwaerts-Erreichbarkeits-Pass ---
            # Stellt sicher dass jeder Punkt der target_timeline physisch erreichbar ist.
            if len(target_timeline) >= 2:
                slot_dur_h = 0.25  # 15-Minuten-Slots
                max_ch_pct_per_slot = (self.max_charge_w * slot_dur_h / self.capacity_wh) * 100.0
                _hard_curve_anchors = [
                    dict(anchor)
                    for anchor in locals().get("intermediate_anchors", [])
                    if anchor.get("ts") and anchor.get("soc") is not None
                ]
                _hard_curve_anchors.sort(key=lambda a: float(a.get("ts", 0)))
                for i in range(len(target_timeline) - 2, -1, -1):
                    next_soc = target_timeline[i + 1]['soc']
                    min_required = next_soc - max_ch_pct_per_slot
                    # Explizit gesetzte Zwischenziele sind harte Nutzer-
                    # Vorgabe. Der Erreichbarkeits-Pass darf ihn und die Punkte
                    # davor nicht still auf Tagesziel-Kurs hochziehen. Wenn das
                    # Tagesziel danach nicht mehr erreichbar ist, bewertet der
                    # Storage Manager das separat und gibt den E3DC autonom frei.
                    _point_ts = float(target_timeline[i].get('ts', 0))
                    for _anchor in _hard_curve_anchors:
                        if _point_ts <= float(_anchor.get("ts", 0)):
                            min_required = min(min_required, float(_anchor.get("soc", min_required)))
                            break
                    if target_timeline[i]['soc'] < min_required:
                        target_timeline[i]['soc'] = round(min_required, 2)
                if _hard_curve_anchors:
                    for point in target_timeline:
                        for _anchor in _hard_curve_anchors:
                            if abs(float(point.get('ts', 0)) - float(_anchor.get("ts", 0))) <= 60000:
                                point['soc'] = round(float(_anchor.get("soc", point.get("soc", 0))), 2)
                                break
                    target_curve_meta["noon_anchor_locked"] = True
                    target_curve_meta["intermediate_anchors_locked"] = True
                    target_curve_meta["noon_anchor_lock_reason"] = "Zwischenziele bleiben harte Nutzervorgabe"
                logger.info(
                    f"Erreichbarkeits-Pass: TL[0]={target_timeline[0]['soc']:.1f}%%, "
                    f"TL[-1]={target_timeline[-1]['soc']:.1f}%% "
                    f"(max_ch_pct/slot={max_ch_pct_per_slot:.1f}%%)"
                )

        except Exception as e:
            logger.warning(f"target_timeline Berechnung fehlgeschlagen: {e}")
            curve_anchors = []

        # --- 7. Export fuer PHP Diagramm ---
        reach_day_start_ms = locals().get('today_0_ms', days[0])
        reach_day_end_ms = locals().get('today_end_ms', days[0] + 86400000)
        max_soc_today = current_soc
        for slot in timeline:
            if reach_day_start_ms <= slot["ts"] < reach_day_end_ms:
                max_soc_today = max(max_soc_today, slot.get("soc", 0))

        # Die Erreichbarkeit darf nur noch Energie zählen, die innerhalb des
        # verbleibenden Slots und der erlaubten PV-Quellen tatsächlich in den
        # Speicher gelangen kann. Bei aktivem DC-first-Vertrag bleibt externe
        # AC-PV vollständig aus der Reichweitenbehauptung.
        _target_reach_now_ts = int(time.time())
        _target_reach_now_ms = float(_target_reach_now_ts) * 1000.0
        _target_reach_dc_only = self._cfg_bool(
            self.v4_config.get("storage_dc_first_charge_limit_enable"),
            False,
        )
        _target_reach_slot_contracts = []
        _target_reach_missing_reasons = set()
        today_chargeable_surplus_wh = 0.0
        today_total_surplus_wh = 0.0
        for _reach_slot in timeline:
            _slot_start_ms = float(_reach_slot.get("ts", 0.0) or 0.0)
            if not (reach_day_start_ms <= _slot_start_ms < reach_day_end_ms):
                continue
            _slot_end_ms = min(reach_day_end_ms, _slot_start_ms + 900000.0)
            _remaining_h = max(
                0.0,
                (_slot_end_ms - max(_target_reach_now_ms, _slot_start_ms))
                / 3600000.0,
            )
            if _remaining_h <= 0.0:
                continue
            _source_contract = _slot_storage_chargeable_forecast_contract(
                _reach_slot,
                dc_only=_target_reach_dc_only,
                expected_topology_revision=(
                    self.pv_topology_contract.get("revision")
                    if _target_reach_dc_only
                    else None
                ),
            )
            _target_reach_slot_contracts.append(_source_contract)
            if not _source_contract.get("complete"):
                _target_reach_missing_reasons.add(
                    str(_source_contract.get("reason") or "source_evidence_incomplete")
                )
            _slot_total_surplus_w = max(
                0.0,
                float(_source_contract.get("total_surplus_w", 0.0) or 0.0),
            )
            _slot_chargeable_w = min(
                float(self.max_charge_w),
                max(
                    0.0,
                    float(
                        _source_contract.get("chargeable_surplus_w", 0.0)
                        or 0.0
                    ),
                ),
            )
            today_total_surplus_wh += _slot_total_surplus_w * _remaining_h
            today_chargeable_surplus_wh += _slot_chargeable_w * _remaining_h
        _target_reach_source_evidence_complete = bool(
            _target_reach_slot_contracts
            and all(
                contract.get("complete") is True
                for contract in _target_reach_slot_contracts
            )
        )
        if not _target_reach_dc_only:
            _target_reach_source_evidence_complete = True
        # Energiebedarf vom Kurvenstart bis Ziel-SoC. Nach Sonnenuntergang kann
        # die Sollkurve bereits fuer morgen gebaut sein; dann waere der aktuelle
        # Abend-SoC als Ausgangspunkt fachlich falsch und wuerde Pre-Dump/Planung
        # blockieren.
        # Für die verbleibende Tagesreichweite ist der aktuelle reale SoC der
        # Ausgangspunkt. Ein Morgenanker würde bereits geladene Energie
        # nochmals als Restbedarf zählen und die Reichweite verfälschen.
        reach_start_soc = float(current_soc)
        required_wh_for_target = self.capacity_wh * max(0.0, (self.target_soc - reach_start_soc)) / 100.0
        # Erreichbar wenn Surplus >= Bedarf (mit 10% Sicherheitsmarge fuer Verluste).
        # Die Diagnose trennt die Gruende bewusst: Bei bereits erreichtem Ziel darf
        # kein irrefuehrender Rest-Surplus-Vergleich geloggt werden.
        physical_needed_wh = required_wh_for_target * 1.1
        can_reach_target_physical = bool(
            _target_reach_source_evidence_complete
            and today_chargeable_surplus_wh >= physical_needed_wh
        )
        target_already_reached = current_soc >= (self.target_soc - 0.2)
        # Auch die simulierte SoC-Kurve einbeziehen (als zweites Kriterium).
        # Hier gilt das echte Tagesziel; die 95%-Toleranz gehoert nicht in die
        # Erreichbarkeits-Aussage, sonst sieht ein 86%-Tag bei 90% Ziel "erreichbar" aus.
        can_reach_target_sim = (max_soc_today >= (self.target_soc - 0.2))
        # Die Gesamtsimulation darf bei DC-first keinen externen AC-Anteil als
        # Ladeerreichbarkeit zurückschmuggeln. Ohne DC-first bleibt der
        # historische Simulationsweg aus Kompatibilitätsgründen erhalten.
        can_reach_target_point = bool(
            target_already_reached
            or can_reach_target_physical
            or (not _target_reach_dc_only and can_reach_target_sim)
        )
        # Der aktuelle Produktivforecast ist eine Punktprognose. Er enthält
        # weder eine kalibrierte quellenbezogene PV-Untergrenze noch eine
        # zukünftige, SoC-/Taper-gebundene Batterieannahme. Er darf daher
        # weder einen Ladeaufschub noch die neue AC-Ausnahme autorisieren.
        _target_reach_conservative_quantile_bound = False
        _target_reach_charge_acceptance_bound = False
        _target_reach_latest_start_bound = False
        _target_reach_decision_evidence_complete = False
        _target_reach_decision_use_allowed = False
        can_reach_target = bool(
            target_already_reached
            or (
                not _target_reach_dc_only
                and can_reach_target_point
            )
        )

        if (
            _target_reach_dc_only
            and not _target_reach_decision_use_allowed
            and not target_already_reached
        ):
            logger.info(
                "Prognose EVIDENCE_LIMIT: DC-Punktprognose ist "
                "quellengetrennt, aber nicht quantil-, Taper- und "
                "Akzeptanz-gebunden. Kein Ladeaufschub."
            )
        elif not can_reach_target:
            logger.info(
                f"Prognose: Ziel-SoC {self.target_soc:.0f}%% nicht erreichbar "
                f"(max_sim {max_soc_today:.1f}%%, Rest-Surplus {today_chargeable_surplus_wh:.0f}Wh "
                f"< Bedarf {physical_needed_wh:.0f}Wh inkl. Reserve). Discharge-Sperre aktiv."
            )
        else:
            if target_already_reached:
                reach_reason = f"SoC bereits bei {current_soc:.1f}%%"
            elif can_reach_target_physical:
                reach_reason = (
                    f"Rest-Surplus {today_chargeable_surplus_wh:.0f}Wh deckt "
                    f"Bedarf {physical_needed_wh:.0f}Wh inkl. Reserve"
                )
            else:
                reach_reason = f"Simulation erreicht {max_soc_today:.1f}%%"
            logger.info(
                f"Prognose: Ziel-SoC {self.target_soc:.0f}%% erreichbar "
                f"(max_sim {max_soc_today:.1f}%%, Rest-Surplus {today_chargeable_surplus_wh:.0f}Wh, "
                f"Kurvenbedarf {required_wh_for_target:.0f}Wh). Grund: {reach_reason}."
            )

        # Das Tagesziel aus der Config bleibt fachlich fix. Die Simulation darf
        # nur bewerten, ob dieses Ziel erreichbar ist. max_reachable_soc ist
        # Diagnose/Anzeige, aber kein neues Regelziel fuer iFc.
        effective_target_soc = float(self.target_soc)
        if can_reach_target:
            max_reachable_soc = round(float(self.target_soc), 1)
        elif (
            _target_reach_dc_only
            and _target_reach_decision_evidence_complete
            and self.capacity_wh > 0.0
        ):
            source_limited_soc = reach_start_soc + (
                today_chargeable_surplus_wh
                / 1.1
                / self.capacity_wh
                * 100.0
            )
            max_reachable_soc = round(
                max(
                    float(current_soc),
                    min(float(self.target_soc), float(source_limited_soc)),
                ),
                1,
            )
        elif _target_reach_dc_only:
            # Ohne vollständigen Entscheidungsvertrag wird aus der
            # Punktprognose kein erreichbarer Zukunfts-SoC behauptet.
            max_reachable_soc = round(float(current_soc), 1)
        else:
            max_reachable_soc = round(max(float(current_soc), min(float(self.target_soc), float(max_soc_today))), 1)
        if target_already_reached:
            target_reach_state = "reachable"
        elif _target_reach_dc_only and not _target_reach_decision_use_allowed:
            target_reach_state = "evidence_limit"
        else:
            target_reach_state = (
                "reachable" if can_reach_target else "unreachable_auto"
            )
        target_reach_mode = "curve_servo" if can_reach_target else "e3dc_auto"
        if target_reach_state == "reachable":
            target_reach_reason = (
                "Tagesziel erreichbar: Zielkurve aktiv. Die Prognose wird bei "
                "jedem Planlauf neu geprüft."
            )
        elif target_reach_state == "evidence_limit":
            target_reach_reason = (
                "EVIDENCE_LIMIT: Der quellengetrennten Punktprognose fehlen "
                "kalibrierte Untergrenze, Taper-/Batterieannahme und gebundener "
                "spätester Ladestart. Kein Prognose-Aufschub und keine "
                "AC-Freigabe."
            )
        else:
            target_reach_reason = (
                "Tagesziel belastbar nicht erreichbar: E3DC AUTO. Der E3DC "
                "nutzt realen PV-Überschuss autonom; Entladung bleibt geschützt."
            )
        _previous_target_reach_state = ""
        _previous_target_reach_last_change_ts = 0
        try:
            _previous_target_reach_state = str(
                existing_plan.get("target_reach_state")
                or existing_meta.get("target_reach_state")
                or ""
            )
            _previous_target_reach_last_change_ts = int(float(
                existing_plan.get("target_reach_last_change_ts")
                or existing_meta.get("target_reach_last_change_ts")
                or 0
            ))
        except Exception:
            _previous_target_reach_state = ""
            _previous_target_reach_last_change_ts = 0
        if (
            _previous_target_reach_state == target_reach_state
            and _previous_target_reach_last_change_ts > 0
        ):
            target_reach_last_change_ts = _previous_target_reach_last_change_ts
        else:
            target_reach_last_change_ts = _target_reach_now_ts
        target_reach_chargeability_contract = {
            "schema": "storage_forecast_chargeability_v1",
            "status": "evidence_limit",
            "plan_id": None,
            "decision_use_allowed": False,
            "wait_allowed": False,
            "aux_ac_allowed": False,
            "wait_coverage_proven": False,
            "dc_shortfall_risk_bounded": False,
            "evaluated_ts": _target_reach_now_ts,
            "valid_until_ts": _target_reach_now_ts + 1200,
            "source_scope": (
                "E3DC_DC_ONLY" if _target_reach_dc_only else "TOTAL_PV"
            ),
            "topology_revision": (
                self.pv_topology_contract.get("revision")
                if _target_reach_dc_only
                else None
            ),
            "forecast_fresh": bool(
                _target_reach_slot_contracts
                and all(
                    contract.get("forecast_fresh") is True
                    for contract in _target_reach_slot_contracts
                )
            ),
            "forecast_revision": None,
            "calibration_revision": None,
            "conservative_quantile_bound": False,
            "charge_acceptance_bound": False,
            "latest_start_bound": False,
            "shortfall_proven": False,
            "blockers": [
                "source_specific_calibrated_lower_quantile_missing",
                "future_battery_acceptance_and_taper_missing",
                "latest_charge_start_not_evidence_bound",
            ],
        }
        target_reach_contract = {
            "target_reach_state": target_reach_state,
            "target_reach_mode": target_reach_mode,
            "target_reach_reason": target_reach_reason,
            "target_reach_recheck_active": True,
            "target_reach_policy": "source_separated_chargeable_surplus_v2",
            "target_reach_control_owner": "storage_manager_can_reach_target",
            "target_reach_status_only": True,
            "target_reach_changed": bool(
                _previous_target_reach_state
                and _previous_target_reach_state != target_reach_state
            ),
            "target_reach_last_change_ts": target_reach_last_change_ts,
            "target_reach_stable_s": max(0, _target_reach_now_ts - target_reach_last_change_ts),
            "target_reach_can_reach_target": bool(can_reach_target),
            "target_reach_point_can_reach_target": bool(
                can_reach_target_point
            ),
            "target_reach_decision_evidence_complete": bool(
                _target_reach_decision_evidence_complete
            ),
            "target_reach_conservative_quantile_bound": bool(
                _target_reach_conservative_quantile_bound
            ),
            "target_reach_charge_acceptance_bound": bool(
                _target_reach_charge_acceptance_bound
            ),
            "target_reach_latest_start_bound": bool(
                _target_reach_latest_start_bound
            ),
            "target_reach_decision_use_allowed": bool(
                _target_reach_decision_use_allowed
            ),
            "target_reach_wait_coverage_proven": False,
            "target_reach_dc_shortfall_risk_bounded": False,
            "target_reach_wait_allowed": False,
            "target_reach_aux_ac_allowed": False,
            "target_reach_shortfall_proven": bool(
                _target_reach_decision_use_allowed
                and not can_reach_target
            ),
            "target_reach_chargeability_contract": (
                target_reach_chargeability_contract
            ),
            "target_reach_evaluated_ts": _target_reach_now_ts,
            "target_reach_source_scope": (
                "E3DC_DC_ONLY" if _target_reach_dc_only else "TOTAL_PV"
            ),
            "target_reach_source_evidence_complete": bool(
                _target_reach_source_evidence_complete
            ),
            "target_reach_source_evidence_reasons": sorted(
                _target_reach_missing_reasons
            ),
            "target_reach_forecast_fresh": bool(
                _target_reach_slot_contracts
                and all(
                    contract.get("forecast_fresh") is True
                    for contract in _target_reach_slot_contracts
                )
            ),
            "target_reach_topology_revision": (
                self.pv_topology_contract.get("revision")
                if _target_reach_dc_only
                else None
            ),
            "target_reach_surplus_wh": round(float(today_chargeable_surplus_wh), 0),
            "target_reach_total_surplus_wh": round(float(today_total_surplus_wh), 0),
            "target_reach_required_wh": round(float(physical_needed_wh), 0),
            "target_reach_margin_wh": round(float(today_chargeable_surplus_wh - physical_needed_wh), 0),
            "target_reach_sim_max_soc_pct": round(float(max_soc_today), 1),
            "target_reach_max_reachable_soc": max_reachable_soc,
        }
        target_curve_meta.update(target_reach_contract)
        if (
            target_reach_state == "unreachable_auto"
            and target_timeline
        ):
            if max_reachable_soc < float(self.target_soc) - 0.2:
                target_curve_meta["effective_target_soc"] = float(self.target_soc)
                target_curve_meta["max_reachable_soc"] = max_reachable_soc
                target_curve_meta["target_capped_unreachable"] = True
                target_curve_meta["target_capped_reason"] = (
                    "Tagesziel nicht mehr erreichbar; E3DC Auto statt Kurvenjagd"
                )
                logger.info(
                    "Tagesziel %.1f%% nicht erreichbar (max %.1f%%). Sollziel bleibt fix, Storage Manager gibt bei Kurvenrueckstand Auto frei."
                    % (float(self.target_soc), max_reachable_soc)
                )
        if weather_reserve_active:
            target_curve_meta["weather_reserve_active"] = True
            target_curve_meta["config_target_soc"] = round(config_target_soc, 1)
            target_curve_meta["planning_target_soc"] = round(planning_target_soc, 1)
            target_curve_meta["weather_reserve_need_wh"] = round(weather_reserve_need_wh, 0)
            target_curve_meta["weather_reserve_reason"] = (
                "Mehrtageprognose schlecht; sonnige Energie wird als Reserve vorgezogen"
            )

        if target_timeline:
            _last_soc = None
            _smooth_changes = 0
            target_timeline.sort(key=lambda x: float(x.get("ts", 0)))
            for point in target_timeline:
                try:
                    soc = float(point.get("soc", 0))
                    if _last_soc is not None and soc < _last_soc:
                        soc = _last_soc
                        _smooth_changes += 1
                    point["soc"] = round(soc, 2)
                    _last_soc = soc
                except Exception:
                    continue

            if curve_anchors:
                _last_anchor_soc = None
                curve_anchors.sort(key=lambda x: float(x.get("ts", 0)))
                for anchor in curve_anchors:
                    try:
                        soc = float(anchor.get("soc", 0))
                        if _last_anchor_soc is not None and soc < _last_anchor_soc:
                            soc = _last_anchor_soc
                        anchor["soc"] = round(soc, 2)
                        _last_anchor_soc = soc
                    except Exception:
                        continue

            if _smooth_changes:
                target_curve_meta["monotonic_smoothing"] = True
                target_curve_meta["monotonic_smoothing_points"] = _smooth_changes
                logger.info("Sollkurve geglättet: %d fallende Punkte entfernt." % _smooth_changes)

        if target_timeline and published_curve_floor_active:
            _timeline_floor_diag = self._protect_curve_points_against_floor(
                target_timeline,
                published_curve_floor_points,
                self.target_soc,
            )
            if _timeline_floor_diag.get("points_clamped"):
                published_curve_timeline_clamps += int(_timeline_floor_diag["points_clamped"])
                published_curve_max_lift_pct = max(
                    published_curve_max_lift_pct,
                    float(_timeline_floor_diag.get("max_lift_pct", 0.0) or 0.0),
                )
                target_curve_meta["published_curve_floor_active"] = True
                target_curve_meta["published_curve_timeline_clamps"] = int(published_curve_timeline_clamps)
                target_curve_meta["published_curve_max_lift_pct"] = round(float(published_curve_max_lift_pct), 2)
                logger.info(
                    "Veröffentlichte Ladekurve schützt %d Timeline-Punkte vor Prognose-Absenkung "
                    "(max %.1f%%)."
                    % (
                        int(_timeline_floor_diag["points_clamped"]),
                        float(_timeline_floor_diag.get("max_lift_pct", 0.0) or 0.0),
                    )
                )

        if target_timeline:
            published_curve_floor_points = self._normalise_curve_points(
                target_timeline,
                reach_day_start_ms,
                reach_day_end_ms,
                planning_target_soc,
            )
            if not published_curve_floor_active and published_curve_floor_points:
                published_curve_floor_source = "current_target_timeline"
                published_curve_floor_reason = "seeded_current_plan"
            target_curve_meta["published_curve_floor_policy"] = published_curve_floor_policy
            target_curve_meta["published_curve_floor_active"] = bool(published_curve_floor_active)
            target_curve_meta["published_curve_floor_source"] = published_curve_floor_source
            target_curve_meta["published_curve_floor_reason"] = published_curve_floor_reason
            target_curve_meta["published_curve_floor_reset_allowed"] = bool(published_curve_reset_allowed)
            target_curve_meta["published_curve_floor_reset_reason"] = published_curve_reset_reason
            target_curve_meta["published_curve_floor_points"] = len(published_curve_floor_points)
            target_curve_meta["published_curve_anchor_clamps"] = int(published_curve_anchor_clamps)
            target_curve_meta["published_curve_timeline_clamps"] = int(published_curve_timeline_clamps)
            target_curve_meta["published_curve_max_lift_pct"] = round(float(published_curve_max_lift_pct), 2)

        if target_timeline:
            try:
                _adaptive_regelbuffer_pct = max(
                    0.0,
                    self._safe_float(self.v4_config.get("eco_dump_regelbuffer_pct", 2.0), 2.0),
                )
                _adaptive_regelbuffer_wh = self.capacity_wh * _adaptive_regelbuffer_pct / 100.0
                _adaptive_trust_wp_sink = self._cfg_bool(
                    self.v4_config.get("predump_trust_heatpump_forecast_as_sink"),
                    False,
                )
                _adaptive_comfort_enabled = self._cfg_bool(
                    self.v4_config.get("storage_adaptive_comfort_enable"),
                    True,
                )
                _adaptive_comfort_soc = (
                    max(0.0, min(float(self.target_soc), self._safe_float(
                        self.v4_config.get("storage_adaptive_comfort_soc", 80.0),
                        80.0,
                    )))
                    if _adaptive_comfort_enabled
                    else 0.0
                )
                _adaptive_large_threshold_kwh = max(
                    1.0,
                    self._safe_float(
                        self.v4_config.get("storage_adaptive_large_storage_kwh", 25.0),
                        25.0,
                    ),
                )
                _adaptive_day_slots = locals().get("today_slots_tl")
                if not _adaptive_day_slots:
                    _adaptive_day_slots = [
                        s for s in timeline
                        if reach_day_start_ms <= s["ts"] < reach_day_end_ms
                    ]
                adaptive_headroom = self._adaptive_headroom_band(
                    target_timeline,
                    _adaptive_day_slots,
                    time.time() * 1000.0,
                    current_soc,
                    self.target_soc,
                    self.capacity_wh,
                    self.max_charge_w,
                    self.export_limit_w,
                    _adaptive_regelbuffer_wh,
                    trust_wp_forecast_sink=_adaptive_trust_wp_sink,
                    comfort_soc=_adaptive_comfort_soc,
                    large_storage_threshold_kwh=_adaptive_large_threshold_kwh,
                    topology_contract=self.pv_topology_contract,
                    e3dc_dc_limit_w=_live_e3dc_dc_limit_w,
                    e3dc_dc_limit_source=_live_e3dc_dc_limit_source,
                    pcc_limit_source=self.export_limit_source,
                    pcc_limit_contract=self.pcc_headroom_limit_contract,
                )
                _observed_dc_pv_w = None
                if _live_pv_w is not None and _live_external_ac_w is not None:
                    _observed_dc_pv_w = max(0.0, _live_pv_w - _live_external_ac_w)
                _observed_topology_status = (
                    "bound" if self.pv_topology_contract.get("split_usable") else "topology_unbound"
                )
                _observed_pcc_contract = self._pcc_headroom_limit_for_topology(
                    _observed_topology_status
                )
                adaptive_headroom["observed_pressure"] = {
                    **slot_headroom_pressure(
                        total_pv_w=_live_pv_w or 0.0,
                        e3dc_dc_pv_w=_observed_dc_pv_w,
                        external_ac_pv_w=_live_external_ac_w,
                        topology_status=_observed_topology_status,
                        topology_revision=self.pv_topology_contract.get("revision"),
                        expected_topology_revision=self.pv_topology_contract.get("revision"),
                        e3dc_dc_limit_w=_live_e3dc_dc_limit_w,
                        pcc_limit_w=_observed_pcc_contract.get("limit_w"),
                        pcc_limit_active=_observed_pcc_contract.get("active") is True,
                        safe_consumers_w=_live_home_w or 0.0,
                        charge_limit_w=self.max_charge_w,
                        e3dc_dc_limit_source=_live_e3dc_dc_limit_source,
                        pcc_limit_source=_observed_pcc_contract.get("source", self.export_limit_source),
                    ),
                    "observed_at": int(_live_ts) if _live_ts > 0.0 else None,
                    "live_sample_available": bool(_live_ts > 0.0 and _live_pv_w is not None),
                    "external_ac_split_valid": bool(_live_external_ac_w is not None),
                    "evidence_scope": "observed_output_not_latent_clipping_proof",
                    "pcc_limit_contract": _observed_pcc_contract,
                }
                _reserve_sources = []
                if historical_headroom_meta.get("active"):
                    _reserve_sources.append(historical_headroom_meta.get("source", "historical_peak"))
                    adaptive_headroom["historical_headroom_active"] = True
                    adaptive_headroom["historical_headroom_slots"] = historical_headroom_meta.get("slots", 0)
                    adaptive_headroom["historical_headroom_sample_days"] = historical_headroom_meta.get("sample_days", 0)
                    adaptive_headroom["historical_headroom_safe_sample_days"] = historical_headroom_meta.get("safe_sample_days", 0)
                    adaptive_headroom["historical_headroom_safe_home_w"] = historical_headroom_meta.get("safe_home_w", 0.0)
                    adaptive_headroom["historical_headroom_max_pv_w"] = historical_headroom_meta.get("max_headroom_pv_w", 0.0)
                    adaptive_headroom["historical_headroom_curtailment_limit_w"] = historical_headroom_meta.get("curtailment_limit_w", 0.0)
                    adaptive_headroom["historical_headroom_temp_factor_max"] = historical_headroom_meta.get("temp_factor_max", 1.0)
                    adaptive_headroom["historical_headroom_min_temp_c"] = historical_headroom_meta.get("min_temp_c")
                    adaptive_headroom["historical_headroom_max_radiation_wm2"] = historical_headroom_meta.get("max_radiation_wm2")
                if cloud_edge_headroom_meta.get("active"):
                    _reserve_sources.append(cloud_edge_headroom_meta.get("source", "live_cloud_edge"))
                    adaptive_headroom["headroom_reserve_live_pv_w"] = cloud_edge_headroom_meta.get("live_pv_w", 0.0)
                    adaptive_headroom["headroom_reserve_forecast_now_w"] = cloud_edge_headroom_meta.get("forecast_now_w", 0.0)
                    adaptive_headroom["headroom_reserve_forecast_ratio"] = cloud_edge_headroom_meta.get("forecast_ratio", 0.0)
                    adaptive_headroom["headroom_reserve_horizon_h"] = cloud_edge_headroom_meta.get("horizon_h", 0.0)
                    adaptive_headroom["headroom_reserve_max_pv_w"] = cloud_edge_headroom_meta.get("max_headroom_pv_w", 0.0)
                if _reserve_sources:
                    adaptive_headroom["headroom_reserve_source"] = "+".join(_reserve_sources)
                if (
                    selected_predump_dump_wh >= 200.0
                    and not bool(self.hard_predump_enabled)
                    and float(adaptive_headroom.get("evening_shortfall_wh", 0.0) or 0.0) < 200.0
                ):
                    _selected_dump_wh = max(0.0, float(selected_predump_dump_wh))
                    _adaptive_required_wh = max(
                        0.0,
                        float(adaptive_headroom.get("adaptive_headroom_required_wh", 0.0) or 0.0),
                    )
                    if _selected_dump_wh > _adaptive_required_wh + 1.0:
                        _selected_need_without_buffer_wh = max(
                            0.0,
                            float(selected_predump_preventable_clipping_wh or 0.0),
                        )
                        _selected_buffer_wh = max(0.0, _selected_dump_wh - _selected_need_without_buffer_wh)
                        adaptive_headroom["adaptive_headroom_required_wh"] = round(_selected_dump_wh, 0)
                        adaptive_headroom["adaptive_headroom_need_without_buffer_wh"] = round(
                            _selected_need_without_buffer_wh,
                            0,
                        )
                        adaptive_headroom["adaptive_headroom_buffer_wh"] = round(_selected_buffer_wh, 0)
                        adaptive_headroom["adaptive_headroom_available_wh"] = round(
                            max(0.0, float(selected_predump_safe_headroom_wh or 0.0)),
                            0,
                        )
                        adaptive_headroom["curtailment_pressure_wh"] = round(
                            max(
                                float(adaptive_headroom.get("curtailment_pressure_wh", 0.0) or 0.0),
                                float(selected_predump_raw_pressure_wh or 0.0),
                            ),
                            0,
                        )
                        if self.capacity_wh > 0:
                            _floor_soc = max(
                                0.0,
                                min(
                                    float(self.target_soc),
                                    float(adaptive_headroom.get("adaptive_soc_floor", 0.0) or 0.0),
                                ),
                            )
                            _ceiling_soc = float(self.target_soc) - (_selected_dump_wh / self.capacity_wh) * 100.0
                            adaptive_headroom["adaptive_soc_ceiling"] = round(
                                max(_floor_soc, min(float(self.target_soc), _ceiling_soc)),
                                2,
                            )
                        adaptive_headroom["adaptive_headroom_source"] = "predump_pre_gate"
                _manual_soc_anchor = self._load_manual_soc_anchor(now_s=now.timestamp())
                if _manual_soc_anchor:
                    adaptive_headroom = self._apply_manual_anchor_to_adaptive_headroom(
                        adaptive_headroom,
                        _manual_soc_anchor,
                        now_ms=time.time() * 1000.0,
                        current_soc=current_soc,
                        target_soc=self.target_soc,
                        capacity_wh=self.capacity_wh,
                        forecast_surplus_wh=today_chargeable_surplus_wh,
                        can_reach_target=can_reach_target,
                    )
                _adaptive_summary = {
                    k: v for k, v in adaptive_headroom.items()
                    if k not in ("soc_min_curve", "soc_ceiling_curve")
                }
                _adaptive_summary["adaptive_floor_is_band"] = bool(adaptive_headroom.get("soc_min_curve"))
                target_curve_meta.update(_adaptive_summary)
                logger.info(
                    "Adaptiver Headroom: Abregeldruck %.0fWh, Reserve %.0fWh, vorhanden %.0fWh, "
                    "zusaetzlich %.0fWh, Floor %.1f%%, Ceiling %.1f%%."
                    % (
                        float(adaptive_headroom.get("curtailment_pressure_wh", 0.0) or 0.0),
                        float(adaptive_headroom.get("headroom_reserve_pressure_wh", 0.0) or 0.0),
                        float(adaptive_headroom.get("adaptive_headroom_available_wh", 0.0) or 0.0),
                        float(adaptive_headroom.get("adaptive_headroom_required_wh", 0.0) or 0.0),
                        float(adaptive_headroom.get("adaptive_soc_floor", 0.0) or 0.0),
                        float(adaptive_headroom.get("adaptive_soc_ceiling", 0.0) or 0.0),
                    )
                )
            except Exception as _adaptive_err:
                adaptive_headroom = {}
                target_curve_meta["adaptive_headroom_error"] = str(_adaptive_err)
                logger.warning("Adaptiver Headroom konnte nicht berechnet werden: %s" % _adaptive_err)

        ladeende_ts_export = None
        ladeende_soc_export = round(float(self.target_soc), 1)
        try:
            if curve_anchors:
                ladeende_ts_export = int(float(curve_anchors[-1].get("ts", curve_end_ts or 0)))
            elif curve_end_ts:
                ladeende_ts_export = int(float(curve_end_ts))
            if ladeende_ts_export:
                target_curve_meta["curve_end_ts"] = ladeende_ts_export
                target_curve_meta["curve_end_t"] = datetime.fromtimestamp(ladeende_ts_export / 1000).strftime("%H:%M")
                target_curve_meta["curve_end_soc"] = ladeende_soc_export
                target_curve_meta["auto_release_offset_min"] = int(LADEENDE_OFFSET_MS / 60000)
        except Exception:
            ladeende_ts_export = None

        _anchor_registry = self._build_anchor_registry(
            target_curve_meta,
            curve_anchors,
            adaptive_headroom,
            now_ms=time.time() * 1000.0,
        )
        target_curve_meta["anchor_registry_version"] = _anchor_registry.get("version")
        target_curve_meta["anchor_registry_policy"] = _anchor_registry.get("policy")
        target_curve_meta["anchor_registry_summary"] = _anchor_registry.get("summary", {})
        target_curve_meta["anchor_registry"] = _anchor_registry.get("anchors", [])

        # --- Eba CheckaWATTar(): EPEX-Preisanalyse mit Ensemble-PV-Prognose ---
        # Vollimplementierung: nutzt timeline (PV+Verbrauch+EPEX) statt simple Preisvergleich.
        # Fallback auf check_awattar() (nur Preise) wenn timeline keine PV-Daten hat.
        pv_in_timeline = any(s.get('pv_w', 0) > 10 for s in timeline)
        if pv_in_timeline or True:  # Immer _check_awattar_full nutzen (timeline hat immer home_w)
            awattar_mode, awattar_reason, awattar_price = self._check_awattar_full(
                timeline, current_soc, epex_tl)
            logger.info('CheckaWATTar (full): mode=%d %s' % (awattar_mode, awattar_reason[:80]))
        else:
            # Fallback: simple Preisvergleich ohne Verbrauchsprognose
            awattar_mode, awattar_reason, awattar_price = check_awattar(
                epex_tl, current_soc, self.target_soc, self.v4_config)
            logger.info('CheckaWATTar (simple): mode=%d %s' % (awattar_mode, awattar_reason))

        cheap_grid_charge = self._cheap_grid_charge_plan(timeline, current_soc, target_timeline)
        _previous_direct_marketing_policy = None
        _previous_direct_marketing_market_windows = None
        try:
            if os.path.exists(OUTPUT_FILE):
                with open(OUTPUT_FILE, "r", encoding="utf-8") as _previous_plan_handle:
                    _previous_plan = json.load(_previous_plan_handle)
                _previous_direct = (
                    _previous_plan.get("direct_marketing")
                    if isinstance(_previous_plan, dict) and isinstance(_previous_plan.get("direct_marketing"), dict)
                    else {}
                )
                if isinstance(_previous_direct.get("policy_decision"), dict):
                    _previous_direct_marketing_policy = _previous_direct.get("policy_decision")
                if isinstance(_previous_direct.get("market_windows"), list):
                    _previous_direct_marketing_market_windows = _previous_direct.get("market_windows")
        except Exception as _previous_direct_err:
            logger.debug("DV-Fortsetzungsvertrag konnte nicht gelesen werden: %s", _previous_direct_err)

        _direct_marketing_config = dict(self.v4_config)
        try:
            if os.path.exists(DIRECT_MARKETING_REPORT_FILE):
                with open(DIRECT_MARKETING_REPORT_FILE, "r", encoding="utf-8") as _dm_report_handle:
                    _dm_report = json.load(_dm_report_handle)
                _direct_marketing_config = direct_marketing_runtime_config(
                    self.v4_config,
                    _dm_report,
                )
        except Exception as _dm_report_err:
            logger.debug("DV-Tagesdurchsatz konnte nicht gelesen werden: %s", _dm_report_err)

        direct_marketing = build_direct_marketing_shadow_plan(
            _direct_marketing_config,
            timeline,
            current_soc,
            self.capacity_wh,
            self.target_soc,
            now_ms=time.time() * 1000,
            target_timeline=target_timeline,
            previous_policy_decision=_previous_direct_marketing_policy,
            previous_market_windows=_previous_direct_marketing_market_windows,
        )
        _market_economics_config = dict(self.v4_config)
        try:
            _configured_market_reserve = self._safe_float(
                self.v4_config.get("ep_reserve_pct", 8.0),
                8.0,
            )
            if _ep_reserve_floor_soc > _configured_market_reserve + 0.05:
                _market_economics_config["ep_reserve_pct"] = str(round(float(_ep_reserve_floor_soc), 2))
                _market_economics_config["market_reserve_floor_source"] = "live_ep_reserve"
        except Exception:
            pass
        _market_now_ms = int(time.time() * 1000)
        _market_required_horizon_end_ms = min(
            int(end_ms),
            _market_now_ms + MARKET_HORIZON_MS,
        )
        market_plan = build_market_economics_plan(
            _market_economics_config,
            timeline,
            current_soc,
            self.capacity_wh,
            self.target_soc,
            now_ms=_market_now_ms,
            target_timeline=target_timeline,
            required_energy_horizon_end_ts_ms=_market_required_horizon_end_ms,
        )
        if storm_grid_charge.get("active"):
            awattar_mode = 2
            awattar_reason = storm_grid_charge.get("reason", awattar_reason)
            logger.info("Unwetterwächter Speicher-Netzladen: %s" % awattar_reason)
        elif cheap_grid_charge.get("active"):
            awattar_mode = 2
            awattar_reason = cheap_grid_charge.get("reason", awattar_reason)
            logger.info("Preis-Boost Speicher: %s" % awattar_reason)

        _hardening_contracts = self._build_hardening_contracts(
            target_curve_meta,
            adaptive_headroom,
            _anchor_registry,
            direct_marketing,
            cheap_grid_charge,
            storm_grid_charge,
            market_plan,
        )
        target_curve_meta["hardening_contracts_version"] = _hardening_contracts.get("version")
        target_curve_meta["hardening_contracts_scope"] = _hardening_contracts.get("scope")
        target_curve_meta["hardening_contracts"] = _hardening_contracts.get("contracts", {})

        # ATOMIC WRITE: Schreibe zuerst in .tmp, dann atomar umbenennen.
        # Verhindert Race-Condition: storage_manager koennte waehrend des Schreibens
        # ein leeres/korruptes JSON lesen -> target_timeline nicht gefunden.
        _tmp_file = OUTPUT_FILE + '.tmp'
        _action_projection_tmp_file = ACTION_PROJECTION_FILE + '.tmp'
        _action_projection_ready = False
        with open(_tmp_file, 'w', encoding='utf-8') as f:
            ladestart_ts  = None
            ladestart_soc = None
            try:
                if pv_start_ts:
                    if curve_anchors:
                        _first_curve_anchor = curve_anchors[0]
                        ladestart_ts = float(_first_curve_anchor.get("ts", _timeline_start_ts))
                        ladestart_soc = round(float(_first_curve_anchor.get("soc", morning_soc_tl)), 1)
                    else:
                        _existing_lts = existing_plan.get('ladestart_ts') if existing_plan else None
                        if locals().get('morning_anchor_delayed', False) and locals().get('morning_anchor_ts'):
                            ladestart_ts = float(morning_anchor_ts)
                        elif _existing_lts and float(_existing_lts) >= today_0_ms and float(_existing_lts) < today_end_ms:
                            ladestart_ts = float(_existing_lts)
                        else:
                            _nine_am_ts = today_0_ms + 32400000
                            if pv_start_ts and pv_start_ts <= _nine_am_ts:
                                ladestart_ts = pv_start_ts
                            else:
                                ladestart_ts = today_0_ms + 25200000
                        ladestart_soc = round(float(_frozen_ladestart_soc), 1) if _frozen_ladestart_soc is not None else round(float(morning_soc_tl), 1)
                else:
                    # Kein PV-Fenster bekannt (noch keine Prognose nach Update/Neustart).
                    # Wenn storage_morning_soc=0 ist, ist der Morgen-Deckel deaktiviert:
                    # dann nicht auf 0% clampen, sondern Tagesanker oder Live-SoC nutzen.
                    ladestart_ts  = today_0_ms + 28800000  # 08:00 UTC Fallback
                    _existing_lss = existing_plan.get('ladestart_soc') if existing_plan else None
                    _existing_lts = existing_plan.get('ladestart_ts') if existing_plan else None
                    if (_existing_lss is not None and _existing_lts
                            and today_0_ms <= float(_existing_lts) < today_end_ms):
                        ladestart_soc = round(float(_existing_lss), 1)
                        logger.info(f'Kein PV-Fenster: bestehender ladestart_soc={ladestart_soc:.1f}%% bleibt erhalten.')
                    elif (not forecast_only_curve) and self.morning_soc > 0:
                        ladestart_soc = round(float(self.morning_soc), 1)
                        logger.info(f'Kein PV-Fenster: ladestart_soc={ladestart_soc:.1f}%% (Morgenpuffer als Mindestanker)')
                    else:
                        ladestart_soc = round(float(current_soc), 1)
                        logger.info(f'Kein PV-Fenster: storage_morning_soc=0 -> Live-SoC {ladestart_soc:.1f}%% als Fallback, kein 0%%-Anker.')
                if ladestart_soc is not None:
                    _old_ladestart_soc = float(ladestart_soc)
                    ladestart_soc = round(max(_old_ladestart_soc, float(_start_anchor_min_soc)), 1)
                    if ladestart_soc > _old_ladestart_soc + 0.05:
                        logger.info(
                            "Ladestart-SoC %.1f%% -> %.1f%% angehoben "
                            "(Notstrom-/Fallbackreserve als Start-SoC)."
                            % (_old_ladestart_soc, ladestart_soc)
                        )
            except Exception as _e:
                logger.warning(f'Ladestart-Anker Berechnung fehlgeschlagen: {_e}')

	            # --- 6b. Anker in target_timeline sicherstellen ---
            # Damit das Frontend die Kurve ab Ladestart (z.B. 07:00) zeichnet
            if ladestart_ts and ladestart_soc is not None and not locals().get('morning_anchor_delayed', False):
                if not target_timeline or target_timeline[0]['ts'] > ladestart_ts:
                    target_timeline.insert(0, {'ts': int(ladestart_ts), 'soc': float(ladestart_soc)})
                else:
                    try:
                        _first_timeline_ts = float(target_timeline[0].get("ts", 0))
                        _first_timeline_soc = float(target_timeline[0].get("soc", 0))
                        if abs(_first_timeline_ts - float(ladestart_ts)) <= 60000 and _first_timeline_soc < float(ladestart_soc) - 0.05:
                            target_timeline[0]["soc"] = float(ladestart_soc)
                    except Exception:
                        pass

            _heat_wp_type = int(self._safe_float(self.v4_config.get("wp_type", -1), -1))
            _heat_enabled = self._cfg_bool(self.v4_config.get("luxtronik"), False)
            _heat_has_shelly = any(
                str(self.v4_config.get(key, "") or "").strip() not in ("", "0.0.0.0")
                for key in ("shelly_sg_ip", "shelly_pause_ip")
            )
            _heat_separate_targets = _heat_enabled and _heat_wp_type in (0, 1)
            _heat_combined_target = _heat_enabled and (
                _heat_wp_type == 5 or _heat_has_shelly
            )
            _heat_controllable = bool(
                _heat_separate_targets or _heat_combined_target
            )
            _heat_price_boost_config = {
                "price_boost_enable": self.v4_config.get("price_boost_enable", 0),
                "heat_price_boost_scope": self.v4_config.get("heat_price_boost_scope", "both"),
                "heat_price_boost_windows": self.v4_config.get("heat_price_boost_windows", ""),
                "price_limit": self.v4_config.get("price_limit"),
                "price_hard_limit": self.v4_config.get("price_hard_limit"),
                "price_pause_limit": self.v4_config.get("price_pause_limit"),
                "price_min_duration": self.v4_config.get("price_min_duration"),
                "stromtarif_typ": self.v4_config.get("stromtarif_typ"),
                "auto_mode": self.v4_config.get("auto_mode", 1),
                "heat_policy_runtime_enable": self.v4_config.get("heat_policy_runtime_enable", 0),
                "wp_min_runtime_min": self.v4_config.get("wp_min_runtime_min", 30),
                "wp_restart_block_min": self.v4_config.get("wp_restart_block_min", 20),
                "wp_type": _heat_wp_type,
                "heatpump_configured": bool(_heat_enabled),
                "heatpump_controllable": _heat_controllable,
                "heatpump_driver_class": (
                    "separate_targets"
                    if _heat_separate_targets
                    else "combined_sg_ready"
                    if _heat_combined_target
                    else "unavailable"
                ),
                "heatpump_allowed_scopes": (
                    ["heating", "dhw", "both"]
                    if _heat_separate_targets
                    else ["both"]
                    if _heat_combined_target
                    else []
                ),
            }

            _storage_plan_payload = {
                "ts":              now.isoformat(),
                "battery_capacity": self.capacity_wh,
                "bat_cap_kwh":     round(self.capacity_kwh, 2),
                "max_charge_w":    round(float(self.max_charge_w), 0),
                "max_discharge_w": round(float(self.max_discharge_w), 0),
                "export_limit_w":  round(float(self.export_limit_w), 0),
                "pv_topology": self.pv_topology_contract,
                "headroom_topology": {
                    "schema_version": "pv_headroom_topology_evidence_v1",
                    "topology_status": adaptive_headroom.get("pv_topology_status", "topology_unbound"),
                    "topology_reason": adaptive_headroom.get("pv_topology_reason", "TOPOLOGY_UNAVAILABLE"),
                    "topology_revision": adaptive_headroom.get("pv_topology_revision"),
                    "dc_pressure_wh": adaptive_headroom.get("dc_headroom_pressure_wh", 0.0),
                    "pcc_pressure_wh": adaptive_headroom.get("pcc_headroom_pressure_wh", 0.0),
                    "combined_pressure_wh": adaptive_headroom.get("combined_headroom_pressure_wh", 0.0),
                    "combination_rule": adaptive_headroom.get("headroom_combination_rule", "max_dc_pcc_no_double_count"),
                    "limits_w": {
                        "e3dc_dc": adaptive_headroom.get("e3dc_dc_limit_w"),
                        "pcc": adaptive_headroom.get("pcc_limit_w"),
                    },
                    "limit_sources": {
                        "e3dc_dc": adaptive_headroom.get("e3dc_dc_limit_source", "unavailable"),
                        "pcc": adaptive_headroom.get("pcc_limit_source", "unavailable"),
                    },
                    "pcc_limit_contract": adaptive_headroom.get(
                        "pcc_limit_contract",
                        self.pcc_headroom_limit_contract,
                    ),
                    "first_pressure_ts": adaptive_headroom.get("curtailment_first_pressure_ts", 0),
                    "deadline_ts": selected_predump_end_ts or adaptive_headroom.get("curtailment_first_pressure_ts", 0),
                    "observed_pressure": adaptive_headroom.get("observed_pressure", {}),
                },
                "forecast_shortfall_aux_ac_config": {
                    "schema_version": (
                        "storage_forecast_shortfall_aux_ac_config_v1"
                    ),
                    "enabled": bool(
                        FORECAST_SHORTFALL_AUX_AC_RELEASED
                        and forecast_only_curve
                        and self._cfg_bool(
                            self.v4_config.get(
                                "storage_forecast_shortfall_aux_ac_charge_enable"
                            ),
                            False,
                        )
                    ),
                    "dc_first_enabled": self._cfg_bool(
                        self.v4_config.get(
                            "storage_dc_first_charge_limit_enable"
                        ),
                        False,
                    ),
                    "forecast_only_target_active": bool(
                        forecast_only_curve
                    ),
                    "target_soc_pct": 100.0,
                    "deadline_ts_ms": ladeende_ts_export,
                    "battery_capacity_wh": self.capacity_wh,
                    "max_charge_power_w": self.max_charge_w,
                    # Keine implizite Risikowahl: Bis ein expliziter,
                    # gemeinsam festgelegter Schwellenwert vorhanden ist,
                    # bleibt der Action-Erzeuger fail-closed.
                    "risk_threshold_pct": self.v4_config.get(
                        "storage_forecast_shortfall_risk_threshold_pct"
                    ),
                    "max_forecast_age_s": self.v4_config.get(
                        "storage_forecast_shortfall_max_age_s",
                        1200,
                    ),
                    "shortfall_deadband_wh": self.v4_config.get(
                        "storage_forecast_shortfall_deadband_wh",
                        100,
                    ),
                },
                "forecast_shortfall_joint_horizon_evidence": (
                    pv_source_meta.get(
                        "storage_forecast_joint_horizon_evidence"
                    )
                    if isinstance(
                        pv_source_meta.get(
                            "storage_forecast_joint_horizon_evidence"
                        ),
                        dict,
                    )
                    else {}
                ),
                "physical_reserve_soc": round(float(_ep_reserve_floor_soc), 2),
                "current_soc":      round(float(current_soc), 3),
                "target_soc":      round(config_target_soc, 1),
                "planning_target_soc": round(planning_target_soc, 1),
                "wallbox_target_soc_active": bool(wallbox_target_soc_active),
                "wallbox_target_soc": round(float(wallbox_target_soc), 1) if wallbox_target_soc is not None else None,
                "wallbox_floor_soc_active": bool(wallbox_floor_soc_active),
                "wallbox_floor_soc": round(float(wallbox_target_soc), 1) if wallbox_target_soc is not None else None,
                "wallbox_config_target_reachable": bool(_config_target_reachable),
                "wallbox_config_target_reach": _config_target_reach_meta,
                "weather_reserve_active": weather_reserve_active,
                "weather_reserve_need_wh": round(weather_reserve_need_wh, 0),
                "effective_target_soc": round(effective_target_soc, 1),
                "max_reachable_soc": max_reachable_soc,
                "morning_target":  round(morning_soc_tl, 1),
                "morning_hour":    self.morning_hour,
                "predump_enabled": bool(self.predump_enabled),
                "predump_min_soc": round(self.predump_min_soc, 1),
                "predump_curve_active": bool(selected_predump_curve_active),
                "predump_curve_soc": selected_predump_curve_soc,
                "predump_midnight_target_soc": selected_predump_midnight_target_soc,
                "predump_curve_start_ts": selected_predump_curve_start_ts,
                "predump_curve_start_soc": selected_predump_curve_start_soc,
                "predump_curve_future_dump_wh": selected_predump_curve_future_dump_wh,
                "predump_dump_wh": selected_predump_dump_wh,
                "predump_preventable_clipping_wh": selected_predump_preventable_clipping_wh,
                "predump_raw_pressure_wh": selected_predump_raw_pressure_wh,
                "predump_safe_headroom_wh": selected_predump_safe_headroom_wh,
                "predump_unavoidable_clipping_wh": selected_predump_unavoidable_clipping_wh,
                "predump_reason": selected_predump_reason,
                "predump_start_ts": selected_predump_start_ts,
                "predump_end_ts": selected_predump_end_ts,
                "adaptive_headroom_required_wh": adaptive_headroom.get("adaptive_headroom_required_wh", 0.0),
                "adaptive_headroom_available_wh": adaptive_headroom.get("adaptive_headroom_available_wh", 0.0),
                "adaptive_headroom_need_without_buffer_wh": adaptive_headroom.get("adaptive_headroom_need_without_buffer_wh", 0.0),
                "adaptive_headroom_buffer_wh": adaptive_headroom.get("adaptive_headroom_buffer_wh", 0.0),
                "adaptive_headroom_target_soc": adaptive_headroom.get("adaptive_headroom_target_soc", None),
                "adaptive_soc_ceiling": adaptive_headroom.get("adaptive_soc_ceiling", None),
                "adaptive_soc_floor": adaptive_headroom.get("adaptive_soc_floor", None),
                "adaptive_soc_ceiling_raw": adaptive_headroom.get("adaptive_soc_ceiling_raw", None),
                "adaptive_headroom_floor_conflict": adaptive_headroom.get("adaptive_headroom_floor_conflict", False),
                "adaptive_headroom_floor_conflict_points": adaptive_headroom.get("adaptive_headroom_floor_conflict_points", 0),
                "adaptive_headroom_floor_conflict_max_delta_pct": adaptive_headroom.get("adaptive_headroom_floor_conflict_max_delta_pct", 0.0),
                "published_curve_floor_policy": published_curve_floor_policy,
                "published_curve_floor_active": bool(target_curve_meta.get("published_curve_floor_active", published_curve_floor_active)),
                "published_curve_floor_source": target_curve_meta.get("published_curve_floor_source", published_curve_floor_source),
                "published_curve_floor_reason": target_curve_meta.get("published_curve_floor_reason", published_curve_floor_reason),
                "published_curve_floor_reset_allowed": bool(target_curve_meta.get("published_curve_floor_reset_allowed", published_curve_reset_allowed)),
                "published_curve_floor_reset_reason": target_curve_meta.get("published_curve_floor_reset_reason", published_curve_reset_reason),
                "published_curve_anchor_clamps": int(target_curve_meta.get("published_curve_anchor_clamps", published_curve_anchor_clamps)),
                "published_curve_timeline_clamps": int(target_curve_meta.get("published_curve_timeline_clamps", published_curve_timeline_clamps)),
                "published_curve_max_lift_pct": target_curve_meta.get("published_curve_max_lift_pct", round(float(published_curve_max_lift_pct), 2)),
                "manual_anchor_active": adaptive_headroom.get("manual_anchor_active", False),
                "manual_anchor_mode": adaptive_headroom.get("manual_anchor_mode", ""),
                "manual_anchor_target_soc": adaptive_headroom.get("manual_anchor_target_soc", None),
                "manual_anchor_floor_soc": adaptive_headroom.get("manual_anchor_floor_soc", None),
                "manual_anchor_reason": adaptive_headroom.get("manual_anchor_reason", ""),
                "anchor_registry_version": target_curve_meta.get("anchor_registry_version"),
                "anchor_registry_policy": target_curve_meta.get("anchor_registry_policy"),
                "anchor_registry_summary": target_curve_meta.get("anchor_registry_summary", {}),
                "anchor_registry": target_curve_meta.get("anchor_registry", []),
                "hardening_contracts_version": target_curve_meta.get("hardening_contracts_version"),
                "hardening_contracts_scope": target_curve_meta.get("hardening_contracts_scope"),
                "hardening_contracts": target_curve_meta.get("hardening_contracts", {}),
                "headroom_reserve_active": adaptive_headroom.get("headroom_reserve_active", False),
                "headroom_reserve_pressure_wh": adaptive_headroom.get("headroom_reserve_pressure_wh", 0.0),
                "headroom_reserve_slots": adaptive_headroom.get("headroom_reserve_slots", 0),
                "headroom_reserve_floor_protected": adaptive_headroom.get("headroom_reserve_floor_protected", False),
                "headroom_reserve_floor_protected_points": adaptive_headroom.get("headroom_reserve_floor_protected_points", 0),
                "headroom_reserve_floor_protected_max_delta_pct": adaptive_headroom.get("headroom_reserve_floor_protected_max_delta_pct", 0.0),
                "headroom_floor_policy": adaptive_headroom.get("headroom_floor_policy", "published_floor_no_downshift_v1"),
                "headroom_reserve_source": adaptive_headroom.get("headroom_reserve_source", ""),
                "headroom_reserve_live_pv_w": adaptive_headroom.get("headroom_reserve_live_pv_w", 0.0),
                "headroom_reserve_forecast_now_w": adaptive_headroom.get("headroom_reserve_forecast_now_w", 0.0),
                "headroom_reserve_forecast_ratio": adaptive_headroom.get("headroom_reserve_forecast_ratio", 0.0),
                "headroom_reserve_horizon_h": adaptive_headroom.get("headroom_reserve_horizon_h", 0.0),
                "curtailment_pressure_wh": adaptive_headroom.get("curtailment_pressure_wh", selected_predump_raw_pressure_wh),
                "dc_headroom_pressure_wh": adaptive_headroom.get("dc_headroom_pressure_wh", 0.0),
                "pcc_headroom_pressure_wh": adaptive_headroom.get("pcc_headroom_pressure_wh", 0.0),
                "curtailment_unavoidable_wh": adaptive_headroom.get("curtailment_unavoidable_wh", selected_predump_unavoidable_clipping_wh),
                "curtailment_first_pressure_ts": adaptive_headroom.get("curtailment_first_pressure_ts", 0),
                "curtailment_soc_at_first_pressure": adaptive_headroom.get("curtailment_soc_at_first_pressure", None),
                "latest_charge_start_ts": adaptive_headroom.get("latest_charge_start_ts", 0),
                "evening_shortfall_wh": adaptive_headroom.get("evening_shortfall_wh", 0.0),
                "hard_predump_enabled": bool(self.predump_enabled and self.hard_predump_enabled),
                "hard_predump_target_soc": round(float(self.hard_predump_target_soc), 1),
                "hard_predump_grid_enabled": self._cfg_bool(self.v4_config.get("hard_predump_grid_enable"), False),
                "mid_target_soc": self.mid_target_soc,
                "mid_hour": self.mid_hour,
                "noon_target_soc": self.noon_target_soc,
                "noon_hour":       self.noon_hour,
                "intermediate_anchors": target_curve_meta.get("intermediate_anchors", []),
                "pv_forecast_kwh": round(float(pv_today_kwh), 2) if pv_today_kwh is not None else None,
                "q_ratio":         q_ratio,
                "max_soc_pct":     round(max_soc_today, 1),
                "can_reach_target": can_reach_target,
                "target_reach_state": target_reach_contract.get("target_reach_state"),
                "target_reach_mode": target_reach_contract.get("target_reach_mode"),
                "target_reach_reason": target_reach_contract.get("target_reach_reason"),
                "target_reach_recheck_active": target_reach_contract.get("target_reach_recheck_active"),
                "target_reach_policy": target_reach_contract.get("target_reach_policy"),
                "target_reach_control_owner": target_reach_contract.get("target_reach_control_owner"),
                "target_reach_status_only": target_reach_contract.get("target_reach_status_only"),
                "target_reach_changed": target_reach_contract.get("target_reach_changed"),
                "target_reach_last_change_ts": target_reach_contract.get("target_reach_last_change_ts"),
                "target_reach_stable_s": target_reach_contract.get("target_reach_stable_s"),
                "target_reach_can_reach_target": target_reach_contract.get("target_reach_can_reach_target"),
                "target_reach_point_can_reach_target": target_reach_contract.get("target_reach_point_can_reach_target"),
                "target_reach_decision_evidence_complete": target_reach_contract.get("target_reach_decision_evidence_complete"),
                "target_reach_conservative_quantile_bound": target_reach_contract.get("target_reach_conservative_quantile_bound"),
                "target_reach_charge_acceptance_bound": target_reach_contract.get("target_reach_charge_acceptance_bound"),
                "target_reach_latest_start_bound": target_reach_contract.get("target_reach_latest_start_bound"),
                "target_reach_decision_use_allowed": target_reach_contract.get("target_reach_decision_use_allowed"),
                "target_reach_wait_coverage_proven": target_reach_contract.get("target_reach_wait_coverage_proven"),
                "target_reach_dc_shortfall_risk_bounded": target_reach_contract.get("target_reach_dc_shortfall_risk_bounded"),
                "target_reach_wait_allowed": target_reach_contract.get("target_reach_wait_allowed"),
                "target_reach_aux_ac_allowed": target_reach_contract.get("target_reach_aux_ac_allowed"),
                "target_reach_shortfall_proven": target_reach_contract.get("target_reach_shortfall_proven"),
                "target_reach_chargeability_contract": target_reach_contract.get("target_reach_chargeability_contract"),
                "target_reach_evaluated_ts": target_reach_contract.get("target_reach_evaluated_ts"),
                "target_reach_source_scope": target_reach_contract.get("target_reach_source_scope"),
                "target_reach_source_evidence_complete": target_reach_contract.get("target_reach_source_evidence_complete"),
                "target_reach_source_evidence_reasons": target_reach_contract.get("target_reach_source_evidence_reasons"),
                "target_reach_forecast_fresh": target_reach_contract.get("target_reach_forecast_fresh"),
                "target_reach_topology_revision": target_reach_contract.get("target_reach_topology_revision"),
                "target_reach_surplus_wh": target_reach_contract.get("target_reach_surplus_wh"),
                "target_reach_total_surplus_wh": target_reach_contract.get("target_reach_total_surplus_wh"),
                "target_reach_required_wh": target_reach_contract.get("target_reach_required_wh"),
                "target_reach_margin_wh": target_reach_contract.get("target_reach_margin_wh"),
                "target_reach_sim_max_soc_pct": target_reach_contract.get("target_reach_sim_max_soc_pct"),
                "target_reach_max_reachable_soc": target_reach_contract.get("target_reach_max_reachable_soc"),
                "ladestart_ts":    ladestart_ts,
                "ladestart_soc":   ladestart_soc,
                "ladeende_ts":     ladeende_ts_export,
                "ladeende_soc":    ladeende_soc_export,
                "eco_dump_date":   today_date_str if _existing_dump_active else "",
                "eco_dump_days":   sorted(d for d in _dump_active_day_keys if d),
                # --- Eba CheckaWATTar() Ergebnis ---
                # 0=Entladen stoppen, 1=Normalbetrieb, 2=Netzladen
                "awattar_mode":    awattar_mode,
                "awattar_reason":  awattar_reason,
                "awattar_price":   awattar_price,
                "cheap_grid_charge": cheap_grid_charge,
                "market_plan": market_plan,
                "direct_marketing": direct_marketing,
                "storm_guard": storm_guard,
                "storm_grid_charge": storm_grid_charge,
                "planned_loads": planned_load_meta,
                "consumption_forecast": consumption_forecast_meta,
	                "forecast_source": forecast_meta.get("forecast_source", "pv_forecast"),
	                "forecast_trust": forecast_meta.get("forecast_trust", "forecast"),
	                "forecast_confidence": forecast_meta.get("forecast_confidence"),
	                "forecast_confidence_status": forecast_meta.get(
	                    "forecast_confidence_status",
	                    "evidence_limit",
	                ),
                "heat_price_boost_config": _heat_price_boost_config,
	                "emergency_curve_active": bool(forecast_meta.get("emergency_curve_active", False)),
                "emergency_curve_reason": forecast_meta.get("emergency_curve_reason", ""),
                "timeline":        timeline,
                "target_timeline": target_timeline,
                "published_target_floor_curve": published_curve_floor_points,
                "soc_min_curve": adaptive_headroom.get("soc_min_curve", target_timeline),
                "soc_ceiling_curve": adaptive_headroom.get("soc_ceiling_curve", []),
                "curve_anchors":    curve_anchors,
                "target_curve_meta": target_curve_meta,
            }
            _dispatch_started = time.perf_counter()
            _storage_plan_payload = build_canonical_dispatch_plan(
                _storage_plan_payload,
                capture_dv_shadow_history=True,
            )
            _dispatch_runtime_ms = round((time.perf_counter() - _dispatch_started) * 1000.0, 3)
            _dv_shadow_history_job = _prepare_dv_shadow_history_job(
                _storage_plan_payload
            )
            _shadow_runtime = _storage_plan_payload.get("shadow_dispatch")
            if isinstance(_shadow_runtime, dict):
                # runtime_ms ist im Planhash bewusst ausgeschlossen. So bleibt
                # der fachliche Plan deterministisch, die Pi-Laufzeit aber
                # für Shadow-/Phase-5-Gates sichtbar.
                _shadow_runtime["runtime_ms"] = _dispatch_runtime_ms
                _shadow_runtime["runtime_measurement"] = "storage_simulator_perf_counter"
            _storage_plan_json = json.dumps(
                _storage_plan_payload,
                ensure_ascii=False,
                separators=(",", ":"),
            )
            _storage_plan_bytes = _storage_plan_json.encode("utf-8")
            try:
                _action_projection_payload = (
                    build_storage_plan_action_projection_artifact(
                        _storage_plan_payload,
                        raw_plan_sha256=(
                            "sha256:"
                            + hashlib.sha256(_storage_plan_bytes).hexdigest()
                        ),
                        raw_plan_size=len(_storage_plan_bytes),
                    )
                )
                with open(
                    _action_projection_tmp_file,
                    "w",
                    encoding="utf-8",
                ) as projection_file:
                    projection_file.write(json.dumps(
                        _action_projection_payload,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ))
                _action_projection_ready = True
            except Exception as exc:
                try:
                    os.unlink(_action_projection_tmp_file)
                except OSError:
                    pass
                logger.warning(
                    "Action-Projektionsartefakt nicht erzeugt; Web-Fallback "
                    "bleibt fail-closed (%s).",
                    type(exc).__name__,
                )
            f.write(_storage_plan_json)
        os.replace(_tmp_file, OUTPUT_FILE)  # Atomar: kein Leser sieht korruptes JSON
        try: os.chmod(OUTPUT_FILE, 0o664)
        except: pass
        if _action_projection_ready:
            try:
                # Erst der Plan, danach sein exakter Rohbyte-Seal. Ein Leser
                # zwischen beiden Renames sieht höchstens ein Mismatch und
                # bleibt dadurch ohne Action-only-Rückfall.
                os.replace(
                    _action_projection_tmp_file,
                    ACTION_PROJECTION_FILE,
                )
                os.chmod(ACTION_PROJECTION_FILE, 0o664)
            except Exception as exc:
                try:
                    os.unlink(_action_projection_tmp_file)
                except OSError:
                    pass
                logger.warning(
                    "Action-Projektionsartefakt nicht veröffentlicht; "
                    "Web-Fallback bleibt fail-closed (%s).",
                    type(exc).__name__,
                )
        self.last_plan_valid_until_ts_ms = int(
            _storage_plan_payload.get("valid_until_ts_ms") or 0
        )
        _enqueue_dv_shadow_history(_dv_shadow_history_job)

        logger.info(f"[OK] V4 Speicher-Plan generiert und in {OUTPUT_FILE} gespeichert.")
        return True

import time

def _storage_plan_input_file_signature(path):
    try:
        info = os.stat(path, follow_symlinks=False)
    except OSError:
        return (path, "missing")

    if path == WEATHER_ALERTS_FILE:
        payload, metadata = _stable_json_object(path)
        if isinstance(payload, dict) and isinstance(metadata, dict):
            alerts = []
            for alert in payload.get("alerts") or []:
                if not isinstance(alert, dict):
                    continue
                alerts.append({
                    "level": alert.get("level", payload.get("highest_level", 0)),
                    "thunderstorm": alert.get("thunderstorm"),
                    "event": alert.get("event"),
                    "headline": alert.get("headline"),
                    "description": alert.get("description"),
                    "start": alert.get("start_ts", alert.get("start")),
                    "end": alert.get("end_ts", alert.get("end")),
                })
            alerts.sort(
                key=lambda item: json.dumps(
                    item,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
            )
            risk = payload.get("risk") if isinstance(payload.get("risk"), dict) else {}
            age_s = max(
                0.0,
                time.time() - float(metadata.get("mtime") or 0.0),
            )
            semantic_payload = {
                "freshness": "stale" if age_s > 6 * 3600 else "fresh",
                "active": payload.get("active"),
                "thunderstorm_active": payload.get("thunderstorm_active"),
                "highest_level": payload.get("highest_level"),
                "title": payload.get("title"),
                "summary": payload.get("summary"),
                "alerts": alerts,
                "risk": {
                    "active": risk.get("active"),
                    "level": risk.get("level"),
                    "time": risk.get("ts", risk.get("time")),
                    "reason": risk.get("reason"),
                },
            }
            return (
                path,
                "weather_alerts_semantic_v2",
                revision_hash(semantic_payload),
            )
        return (
            path,
            "weather_alerts_invalid",
            int(info.st_dev),
            int(info.st_ino),
            int(info.st_size),
            int(info.st_mtime_ns),
            int(info.st_ctime_ns),
        )

    return (
        path,
        int(info.st_dev),
        int(info.st_ino),
        int(info.st_size),
        int(info.st_mtime_ns),
        int(info.st_ctime_ns),
    )


def _storage_plan_input_signature():
    return tuple(
        _storage_plan_input_file_signature(path)
        for path in SIM_REPLAN_INPUT_FILES
    )


def _generate_plan_bound_to_inputs(sim):
    """Erzeugt einen Plan und verliert keine Änderung während des Planlaufs."""

    last_input_signature = _storage_plan_input_signature()
    for attempt in range(2):
        input_signature_before = _storage_plan_input_signature()
        try:
            sim.generate_plan()
        except Exception as exc:
            logger.error(f"Unerwarteter Fehler im Storage Simulator Loop: {exc}")
        input_signature_after = _storage_plan_input_signature()
        last_input_signature = input_signature_after
        if input_signature_after == input_signature_before:
            return last_input_signature
        if attempt == 0:
            logger.info(
                "Storage-Plan Eingangsdaten änderten sich während des Planlaufs "
                "- führe genau einen gebundenen Nachlauf aus."
            )
            continue
        # Bei dauerhaft churnenden Quellen verhindert der kurze Pollpfad eine
        # ungebremste Replan-Schleife. Die Vor-Signatur sorgt dafür, dass die
        # letzte Änderung beim nächsten Poll sicher erneut auffällt.
        logger.warning(
            "Storage-Plan Eingangsdaten änderten sich auch im Nachlauf; "
            "erneute Prüfung im kurzen Eingangspoll."
        )
        return input_signature_before
    return last_input_signature


def _storage_plan_requires_immediate_replan(sim, now_s=None):
    """Verhindert eine abgelaufene Erstgeneration über einer Slotgrenze."""

    now_ms = int((time.time() if now_s is None else float(now_s)) * 1000.0)
    valid_until_ms = int(
        getattr(sim, "last_plan_valid_until_ts_ms", 0) or 0
    )
    return bool(valid_until_ms > 0 and now_ms >= valid_until_ms)


def run_service():
    logger.info("Starte E3DC Storage Simulator Service (V4)...")
    sim = StorageSimulator()
    last_input_signature = _storage_plan_input_signature()
    while True:
        last_input_signature = _generate_plan_bound_to_inputs(sim)

        if _storage_plan_requires_immediate_replan(sim):
            logger.warning(
                "Erzeugter Storage-Plan überschritt während der Berechnung "
                "seine Slotgrenze; plane unmittelbar für den aktuellen Slot neu."
            )
            continue

        # Die Simulation baut auf der PV Prognose auf. Alle 15 Minuten updaten reicht völlig aus.
        # Config-/Forecast-Aenderungen sollen aber zeitnah sichtbar werden, ohne dass Nutzer
        # den Dienst neu starten muessen.
        # Nach dem ersten Start exakt an die 15-Minuten-Slotgrenze takten.
        # So endet der ausführbare Receding-Horizon-Slot nie vor dem nächsten
        # regulären Replan; Eingangsänderungen lösen weiterhin sofort neu aus.
        now_s = time.time()
        deadline = (int(now_s // SIM_INTERVAL_S) + 1) * SIM_INTERVAL_S + 0.05
        while time.time() < deadline:
            current_signature = _storage_plan_input_signature()
            if current_signature != last_input_signature:
                logger.info("Storage-Plan Eingangsdaten geändert - plane zeitnah neu.")
                last_input_signature = current_signature
                break
            time.sleep(min(SIM_INPUT_POLL_S, max(0.2, deadline - time.time())))

if __name__ == "__main__":
    run_service()
