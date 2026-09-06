#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Niedrig priorisierter, rein lesender Sidecar für die Prognosediagnose."""

from __future__ import annotations

import argparse
import json
import logging
import os
import stat
import time
from typing import Any

try:
    from Forecast.pv_forecast_evidence import (
        EVIDENCE_DB_PATH,
        EVIDENCE_LOCK_PATH,
        SUMMARY_JSON_PATH,
        EvidenceLimitError,
        append_summary_if_due,
        archive_forecast_snapshot,
        database,
        enforce_retention,
        extract_forecast_issue_contract,
        initialize_database,
        publish_summary_json,
        single_writer_lock,
        store_external,
        store_history_observations,
        unavailable_summary,
        unavailable_summary_with_retained_continuity,
    )
    from e3dc_history_slots import HISTORY_DEFAULT_SLOT_COUNT, read_recent_closed_slots
    from Forecast.pv_forecast_diagnostic_details import ExternalEnergyAccumulator
    from rscp_client import RscpConnection
except ImportError:  # pragma: no cover - Paketimport
    from Installer.Forecast.pv_forecast_evidence import (
        EVIDENCE_DB_PATH,
        EVIDENCE_LOCK_PATH,
        SUMMARY_JSON_PATH,
        EvidenceLimitError,
        append_summary_if_due,
        archive_forecast_snapshot,
        database,
        enforce_retention,
        extract_forecast_issue_contract,
        initialize_database,
        publish_summary_json,
        single_writer_lock,
        store_external,
        store_history_observations,
        unavailable_summary,
        unavailable_summary_with_retained_continuity,
    )
    from Installer.e3dc_history_slots import (
        HISTORY_DEFAULT_SLOT_COUNT,
        read_recent_closed_slots,
    )
    from Installer.rscp_client import RscpConnection
    from Installer.Forecast.pv_forecast_diagnostic_details import ExternalEnergyAccumulator


CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
FORECAST_PATH = "/var/www/html/ramdisk/pv_forecast.json"
MAX_CONFIG_BYTES = 2 * 1024 * 1024
MAX_FORECAST_BYTES = 8 * 1024 * 1024
MAX_FORECAST_AGE_S = 6 * 60 * 60
CYCLE_INTERVAL_S = 15 * 60
LOCAL_SAMPLE_INTERVAL_S = 15
LIVE_PATH = "/var/www/html/ramdisk/live_data_py.json"

logger = logging.getLogger("E3DCForecastEvidence")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)


def _typed_reason(exc: BaseException, fallback: str) -> str:
    text = str(exc or "").strip()
    if text and len(text) <= 80 and all(
        character.isalnum() or character in {"_", "-"}
        for character in text
    ):
        return text
    if isinstance(exc, FileNotFoundError):
        return f"{fallback}_missing"
    return fallback


def _bounded_json_file(path: str, *, max_bytes: int) -> tuple[Any, os.stat_result]:
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode):
            raise ValueError("source_not_regular")
        if file_stat.st_size < 2 or file_stat.st_size > int(max_bytes):
            raise ValueError("source_size_invalid")
        chunks: list[bytes] = []
        remaining = int(max_bytes) + 1
        while remaining > 0:
            chunk = os.read(descriptor, min(65536, remaining))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        encoded = b"".join(chunks)
        if len(encoded) > int(max_bytes):
            raise ValueError("source_size_limit")
        return json.loads(encoded.decode("utf-8")), file_stat
    finally:
        os.close(descriptor)


def load_runtime_config(path: str = CONFIG_PATH) -> dict[str, Any]:
    value, _file_stat = _bounded_json_file(path, max_bytes=MAX_CONFIG_BYTES)
    if not isinstance(value, dict):
        raise ValueError("config_root_invalid")
    return value


def load_current_forecast(
    path: str = FORECAST_PATH,
    *,
    now_utc_s: int | None = None,
) -> tuple[list[dict[str, Any]], str, int, dict[str, Any] | None]:
    value, file_stat = _bounded_json_file(path, max_bytes=MAX_FORECAST_BYTES)
    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    published_at_utc_s = int(file_stat.st_mtime)
    if published_at_utc_s > now_s + 300:
        raise ValueError("forecast_timestamp_in_future")
    if now_s - published_at_utc_s > MAX_FORECAST_AGE_S:
        raise ValueError("forecast_stale")
    if not isinstance(value, list) or not value:
        raise ValueError("forecast_root_invalid")
    slots = [slot for slot in value if isinstance(slot, dict)]
    revisions = {
        str(slot.get("pv_topology_revision") or "").strip()
        for slot in slots
        if str(slot.get("pv_topology_status") or "") == "bound"
    }
    revisions.discard("")
    if len(revisions) != 1:
        raise ValueError("forecast_topology_revision_missing_or_mixed")
    revision = next(iter(revisions))
    if not revision.startswith("sha256:") or len(revision) != 71:
        raise ValueError("forecast_topology_revision_invalid")
    issue_contract = extract_forecast_issue_contract(slots)
    return slots, revision, published_at_utc_s, issue_contract


def diagnostics_enabled(config: dict[str, Any]) -> bool:
    return str(config.get("forecast_diagnostics_enable", "0")).strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
        "ein",
    }


def _rscp_config(config: dict[str, Any]) -> tuple[str, int, str, str, str]:
    host = str(config.get("server_ip") or "").strip()
    user = str(config.get("e3dc_user") or "").strip()
    password = str(config.get("e3dc_password") or "")
    aes_password = str(config.get("aes_password") or password)
    try:
        port = int(config.get("server_port") or 5033)
    except (TypeError, ValueError) as exc:
        raise ValueError("rscp_port_invalid") from exc
    if not host or not user or not password or not aes_password or not 1 <= port <= 65535:
        raise ValueError("rscp_config_incomplete")
    return host, port, user, password, aes_password


def read_history(config: dict[str, Any], *, now_utc_s: int) -> list[dict[str, Any]]:
    host, port, user, password, aes_password = _rscp_config(config)
    connection = RscpConnection(host, port, aes_password)
    try:
        connection.connect()
        connection.authenticate(user, password)
        return read_recent_closed_slots(
            connection,
            now_utc_s=now_utc_s,
            slot_count=HISTORY_DEFAULT_SLOT_COUNT,
        )
    finally:
        connection.close()


def run_cycle(
    *,
    config_path: str = CONFIG_PATH,
    forecast_path: str = FORECAST_PATH,
    database_path: str = EVIDENCE_DB_PATH,
    lock_path: str = EVIDENCE_LOCK_PATH,
    summary_path: str = SUMMARY_JSON_PATH,
    now_utc_s: int | None = None,
    external_observations: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    try:
        config = load_runtime_config(config_path)
    except Exception as exc:
        reason = _typed_reason(exc, "runtime_config_unavailable")
        payload = unavailable_summary(None, reason, now_utc_s=now_s)
        publish_summary_json(payload, summary_path=summary_path)
        return {"status": "unavailable", "reason": reason, "control_effect": False}
    if not diagnostics_enabled(config):
        return {"status": "disabled", "control_effect": False}

    try:
        (
            forecast_slots,
            revision,
            published_at_utc_s,
            forecast_issue_contract,
        ) = load_current_forecast(
            forecast_path,
            now_utc_s=now_s,
        )
    except Exception as exc:
        reason = _typed_reason(exc, "forecast_source_unavailable")
        payload = unavailable_summary(None, reason, now_utc_s=now_s)
        publish_summary_json(payload, summary_path=summary_path)
        return {"status": "unavailable", "reason": reason, "control_effect": False}
    if forecast_issue_contract is None:
        reason = "producer_issue_contract_missing"
        payload = unavailable_summary_with_retained_continuity(
            revision,
            reason,
            now_utc_s=now_s,
            database_path=database_path,
        )
        publish_summary_json(payload, summary_path=summary_path)
        return {"status": "unavailable", "reason": reason, "control_effect": False}

    history_status = "stored"
    try:
        with single_writer_lock(lock_path):
            initialize_database(database_path)
            enforce_retention(now_utc_s=now_s, database_path=database_path)
            archive_result = archive_forecast_snapshot(
                forecast_slots,
                forecast_issue_contract=forecast_issue_contract,
                published_at_utc_s=published_at_utc_s,
                captured_at_utc_s=now_s,
                database_path=database_path,
            )
            with database(database_path, write=True) as connection:
                external_inserted = store_external(connection, external_observations or [])
            try:
                history_slots = read_history(config, now_utc_s=now_s)
                history_result = store_history_observations(
                    history_slots,
                    topology_revision=revision,
                    observed_at_utc_s=now_s,
                    database_path=database_path,
                )
            except Exception as exc:
                history_status = "history_unavailable"
                history_result = {"inserted": 0, "reason": str(exc)}
                logger.warning("E3/DC-Historie derzeit nicht verfügbar: %s", exc)
            summary = append_summary_if_due(
                topology_revision=revision,
                now_utc_s=now_s,
                database_path=database_path,
            )
    except Exception as exc:
        reason = _typed_reason(exc, "evidence_writer_failed")
        payload = unavailable_summary(revision, reason, now_utc_s=now_s)
        publish_summary_json(payload, summary_path=summary_path)
        return {
            "status": "unavailable",
            "reason": reason,
            "topology_revision": revision,
            "control_effect": False,
        }

    publish_summary_json(summary, summary_path=summary_path)
    return {
        "status": "ok",
        "history_status": history_status,
        "archive": archive_result,
        "history": history_result,
        "external_observations_inserted": external_inserted,
        "topology_revision": revision,
        "control_effect": False,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Read-only Sidecar für die PV-Prognosediagnose"
    )
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--interval", type=int, default=CYCLE_INTERVAL_S)
    args = parser.parse_args()
    interval_s = max(CYCLE_INTERVAL_S, int(args.interval))
    accumulator = ExternalEnergyAccumulator()
    pending_external: list[dict[str, Any]] = []
    next_cycle = 0.0
    forecast_identity = None
    current_revision = None

    while True:
        try:
            now_s = time.time()
            if not args.once:
                config = load_runtime_config()
                if not diagnostics_enabled(config):
                    logger.info("PV-Prognosediagnose ist ausgeschaltet.")
                    return 0
                try:
                    identity = os.stat(FORECAST_PATH, follow_symlinks=False)
                    identity = (identity.st_ino, identity.st_mtime_ns, identity.st_size)
                    if identity != forecast_identity:
                        _slots, current_revision, _published, _contract = load_current_forecast(now_utc_s=int(now_s))
                        forecast_identity = identity
                    live, _stat = _bounded_json_file(LIVE_PATH, max_bytes=MAX_CONFIG_BYTES)
                    accumulator.observe(live, current_revision, now_s)
                except Exception:
                    # Eine fehlende, veraltete oder ungültige Probe öffnet eine Lücke.
                    accumulator.observe({}, None, now_s)
                pending_external.extend(accumulator.closed(now_s))
                # Bei längerem Ausfall keine unbegrenzte RAM-Warteschlange aufbauen.
                if len(pending_external) > 192:
                    logger.warning(
                        "Externe Messhistorie unvollständig: %d alte RAM-Slots konnten nicht archiviert werden.",
                        len(pending_external) - 192,
                    )
                    pending_external = pending_external[-192:]
            if not args.once and time.monotonic() < next_cycle:
                time.sleep(LOCAL_SAMPLE_INTERVAL_S)
                continue
            try:
                result = run_cycle(external_observations=pending_external)
            finally:
                # Auch unerwartete Fehler dürfen keine schnellen RSCP-/Disk-Retries auslösen.
                next_cycle = time.monotonic() + interval_s
            if result.get("status") == "ok":
                pending_external.clear()
            status = str(result.get("status") or "unknown")
            if status == "disabled":
                logger.info("PV-Prognosediagnose ist ausgeschaltet.")
                return 0
            logger.info(
                "Diagnosezyklus abgeschlossen: Status=%s, History=%s",
                status,
                result.get("history_status") or "n/a",
            )
        except EvidenceLimitError as exc:
            logger.error("Diagnose bleibt fail-closed: %s", exc)
            try:
                publish_summary_json(unavailable_summary(None, str(exc)))
            except Exception:
                pass
            return 2
        except Exception as exc:
            logger.error("Diagnosezyklus fehlgeschlagen: %s", exc)
        if args.once:
            return 0
        time.sleep(LOCAL_SAMPLE_INTERVAL_S if not args.once else interval_s)


if __name__ == "__main__":
    raise SystemExit(main())
