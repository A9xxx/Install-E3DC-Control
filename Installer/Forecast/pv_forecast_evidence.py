#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Private, rein diagnostische Evidenzablage für PV-Prognosen.

Das Modul gehört ausschließlich dem niedrig priorisierten Diagnose-Sidecar.
Es importiert weder Regelungs- noch Konfigurationsmodule und besitzt keinerlei
Schnittstelle für Hardwarebefehle oder automatische Modellauswahl.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import math
import os
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterable
from urllib.parse import quote


EVIDENCE_STATE_DIR = "/var/lib/e3dc-control/forecast-evidence"
EVIDENCE_DB_PATH = os.path.join(EVIDENCE_STATE_DIR, "pv_forecast_evidence.db")
EVIDENCE_LOCK_PATH = os.path.join(EVIDENCE_STATE_DIR, "writer.lock")
SUMMARY_JSON_PATH = "/var/www/html/ramdisk/pv_forecast_diagnostic_summary.json"

EVIDENCE_SCHEMA = "pv_forecast_evidence_v2"
SUMMARY_SCHEMA = "pv_forecast_diagnostic_summary_v2"
OPERATION_MODE = "read_only_diagnostic"
HISTORY_SOURCE_CONTRACT = "e3dc_db_history_day_15m_v1"

ARCHIVE_MIN_INTERVAL_S = 6 * 60 * 60
SUMMARY_MIN_INTERVAL_S = 24 * 60 * 60
RAW_RETENTION_S = 90 * 24 * 60 * 60
EVALUATION_WINDOW_S = RAW_RETENTION_S
MIN_EVALUATION_DELAY_S = 60 * 60
MIN_RELEVANT_SLOTS = 96
MIN_RELEVANT_DAYS = 7
YIELD_RELEVANT_ENERGY_WH = 25.0
MAX_DATABASE_BYTES = 256 * 1024 * 1024
SQLITE_BUSY_TIMEOUT_MS = 750
SQLITE_BEGIN_DELAYS_S = (0.05, 0.15, 0.30)
MAX_SUMMARY_BYTES = 64 * 1024

DIAGNOSTIC_LABELS = {
    "trefferabweichung_wh": "Trefferabweichung",
    "richtungsversatz_wh": "Richtungsversatz",
    "energiegewichtete_gesamtabweichung_pct": "Energiegewichtete Gesamtabweichung",
    "vergleichsabdeckung_pct": "Vergleichsabdeckung",
}


class EvidenceLimitError(RuntimeError):
    """Die Evidenzgrenze wurde erreicht; weitere Schreibvorgänge bleiben aus."""


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    )


def _sha256_record(prefix: str, value: Any) -> str:
    digest = hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()
    return f"{prefix}:{digest}"


def _finite_optional(value: Any, *, nonnegative: bool = False) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or (nonnegative and number < 0.0):
        return None
    return number


def _valid_revision(value: Any) -> str | None:
    revision = str(value or "").strip()
    if not revision.startswith("sha256:") or len(revision) != 71:
        return None
    digest = revision[7:]
    if any(char not in "0123456789abcdef" for char in digest.lower()):
        return None
    return "sha256:" + digest.lower()


def _ensure_private_location(database_path: str, *, create: bool = True) -> None:
    path = os.path.abspath(database_path)
    directory = os.path.dirname(path)
    if os.path.lexists(directory) and os.path.islink(directory):
        raise EvidenceLimitError("private_state_directory_symlink")
    if create:
        os.makedirs(directory, mode=0o700, exist_ok=True)
        os.chmod(directory, 0o700)
    elif not os.path.isdir(directory):
        raise EvidenceLimitError("private_state_directory_missing")
    if os.path.lexists(path) and os.path.islink(path):
        raise EvidenceLimitError("private_database_symlink")
    if not create and not os.path.exists(path):
        raise EvidenceLimitError("private_database_missing")
    if os.path.exists(path) and not os.path.isfile(path):
        raise EvidenceLimitError("private_database_not_regular")
    if os.path.exists(path) and os.path.getsize(path) > MAX_DATABASE_BYTES:
        raise EvidenceLimitError("private_database_size_limit")


def _initialize_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS evidence_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forecast_issues (
            issue_id TEXT PRIMARY KEY,
            issued_at_utc_s INTEGER NOT NULL,
            topology_revision TEXT NOT NULL,
            operation_mode TEXT NOT NULL CHECK (operation_mode = 'read_only_diagnostic'),
            control_effect INTEGER NOT NULL CHECK (control_effect = 0),
            configuration_writes INTEGER NOT NULL CHECK (configuration_writes = 0),
            automatic_model_selection INTEGER NOT NULL CHECK (automatic_model_selection = 0),
            created_at_utc_s INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS forecast_slots (
            issue_id TEXT NOT NULL REFERENCES forecast_issues(issue_id) ON DELETE CASCADE,
            slot_start_utc_s INTEGER NOT NULL,
            slot_end_utc_s INTEGER NOT NULL,
            predicted_e3dc_dc_energy_wh REAL,
            source_fresh INTEGER NOT NULL,
            source_quality TEXT NOT NULL,
            PRIMARY KEY (issue_id, slot_start_utc_s)
        );

        CREATE TABLE IF NOT EXISTS observed_slots (
            observation_id TEXT PRIMARY KEY,
            slot_start_utc_s INTEGER NOT NULL,
            slot_end_utc_s INTEGER NOT NULL,
            topology_revision TEXT NOT NULL,
            source_contract TEXT NOT NULL,
            actual_e3dc_dc_energy_wh REAL,
            valid INTEGER NOT NULL,
            reason TEXT NOT NULL,
            observed_at_utc_s INTEGER NOT NULL
        );

        CREATE TABLE IF NOT EXISTS diagnostic_summaries (
            summary_id TEXT PRIMARY KEY,
            topology_revision TEXT NOT NULL,
            calculated_at_utc_s INTEGER NOT NULL,
            payload_json TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_forecast_issue_revision_time
            ON forecast_issues(topology_revision, issued_at_utc_s);
        CREATE INDEX IF NOT EXISTS idx_forecast_slot_time
            ON forecast_slots(slot_start_utc_s, slot_end_utc_s);
        CREATE INDEX IF NOT EXISTS idx_observed_revision_time
            ON observed_slots(topology_revision, slot_start_utc_s, observed_at_utc_s);
        CREATE INDEX IF NOT EXISTS idx_observed_summary_rank
            ON observed_slots(
                topology_revision,
                source_contract,
                slot_start_utc_s,
                valid DESC,
                observed_at_utc_s DESC,
                observation_id DESC
            );
        CREATE INDEX IF NOT EXISTS idx_summary_revision_time
            ON diagnostic_summaries(topology_revision, calculated_at_utc_s);

        CREATE TRIGGER IF NOT EXISTS forecast_issues_no_update
        BEFORE UPDATE ON forecast_issues
        BEGIN SELECT RAISE(ABORT, 'forecast_issues are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS forecast_slots_no_update
        BEFORE UPDATE ON forecast_slots
        BEGIN SELECT RAISE(ABORT, 'forecast_slots are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS observed_slots_no_update
        BEFORE UPDATE ON observed_slots
        BEGIN SELECT RAISE(ABORT, 'observed_slots are immutable'); END;
        CREATE TRIGGER IF NOT EXISTS diagnostic_summaries_no_update
        BEFORE UPDATE ON diagnostic_summaries
        BEGIN SELECT RAISE(ABORT, 'diagnostic_summaries are immutable'); END;
        """
    )
    connection.execute(
        "INSERT OR IGNORE INTO evidence_meta(key, value) VALUES('schema_version', ?)",
        (EVIDENCE_SCHEMA,),
    )


def _schema_is_ready(connection: sqlite3.Connection) -> bool:
    try:
        row = connection.execute(
            "SELECT value FROM evidence_meta WHERE key = 'schema_version'"
        ).fetchone()
    except sqlite3.OperationalError as exc:
        if "no such table" in str(exc).lower():
            return False
        raise
    if row is None:
        return False
    if str(row[0]) != EVIDENCE_SCHEMA:
        raise EvidenceLimitError("private_database_schema_mismatch")
    required_objects = {
        "forecast_issues",
        "forecast_slots",
        "observed_slots",
        "diagnostic_summaries",
        "idx_observed_summary_rank",
        "forecast_issues_no_update",
        "forecast_slots_no_update",
        "observed_slots_no_update",
        "diagnostic_summaries_no_update",
    }
    present = {
        str(item[0])
        for item in connection.execute(
            """
            SELECT name
            FROM sqlite_master
            WHERE name IN (
                'forecast_issues',
                'forecast_slots',
                'observed_slots',
                'diagnostic_summaries',
                'idx_observed_summary_rank',
                'forecast_issues_no_update',
                'forecast_slots_no_update',
                'observed_slots_no_update',
                'diagnostic_summaries_no_update'
            )
            """
        ).fetchall()
    }
    return required_objects == present


def _database_is_busy(exc: BaseException) -> bool:
    message = str(exc).lower()
    return "locked" in message or "busy" in message


def _new_database_connection(path: str, *, write: bool) -> sqlite3.Connection:
    if write:
        database_target = path
        use_uri = False
    else:
        database_target = (
            f"file:{quote(os.path.abspath(path), safe='/')}?mode=ro&immutable=0"
        )
        use_uri = True
    connection = sqlite3.connect(
        database_target,
        timeout=SQLITE_BUSY_TIMEOUT_MS / 1000.0,
        isolation_level=None,
        uri=use_uri,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute(f"PRAGMA busy_timeout = {SQLITE_BUSY_TIMEOUT_MS}")
    return connection


def _prepare_writer_database(path: str) -> sqlite3.Connection:
    """Bindet die gesamte Writer-Initialisierung an einen begrenzten Retry.

    Auch ein rein lesender, bereits geöffneter SQLite-Client kann
    Initialisierungs-PRAGMAs oder den Schemaabgleich kurz blockieren. Deshalb
    gilt der Retry nicht erst für ``BEGIN IMMEDIATE``, sondern für die gesamte
    Writer-Vorbereitung. Jeder Fehlversuch wird vollständig geschlossen.
    """

    last_error: sqlite3.OperationalError | None = None
    attempt_delays = (0.0, *SQLITE_BEGIN_DELAYS_S)
    for attempt, delay_s in enumerate(attempt_delays):
        if delay_s > 0.0:
            time.sleep(delay_s)
        connection = _new_database_connection(path, write=True)
        if os.path.exists(path):
            os.chmod(path, 0o600)
        try:
            connection.execute("PRAGMA synchronous = NORMAL")
            # ``executescript`` beendet eine bestehende Transaktion implizit.
            # Der Schemaabgleich muss deshalb vor dem abschließenden
            # BEGIN IMMEDIATE liegen. Ein fertiges Schema wird nur gelesen und
            # erzeugt auch bei parallelen Diagnose-Lesern keinen DDL-Lock.
            if not _schema_is_ready(connection):
                _initialize_schema(connection)
            page_size = int(connection.execute("PRAGMA page_size").fetchone()[0])
            max_pages = max(1, MAX_DATABASE_BYTES // page_size)
            connection.execute(f"PRAGMA max_page_count = {max_pages}")
            connection.execute("BEGIN IMMEDIATE")
            if not connection.in_transaction:
                raise sqlite3.OperationalError("writer_transaction_not_active")
            return connection
        except sqlite3.OperationalError as exc:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
            connection.close()
            if not _database_is_busy(exc):
                raise
            last_error = exc
            if attempt + 1 >= len(attempt_delays):
                break
        except sqlite3.DatabaseError:
            try:
                connection.rollback()
            except sqlite3.DatabaseError:
                pass
            connection.close()
            raise
    raise EvidenceLimitError("database_writer_busy") from last_error


def _open_database(path: str, *, write: bool) -> sqlite3.Connection:
    _ensure_private_location(path, create=write)
    if write:
        try:
            return _prepare_writer_database(path)
        except sqlite3.DatabaseError as exc:
            if "full" in str(exc).lower():
                raise EvidenceLimitError("private_database_size_limit") from exc
            raise
    connection = _new_database_connection(path, write=False)
    connection.execute("PRAGMA query_only = ON")
    return connection


@contextmanager
def database(path: str = EVIDENCE_DB_PATH, *, write: bool = False):
    connection = _open_database(path, write=write)
    try:
        yield connection
        if write:
            connection.commit()
    except sqlite3.DatabaseError as exc:
        if write:
            connection.rollback()
        if "full" in str(exc).lower():
            raise EvidenceLimitError("private_database_size_limit") from exc
        raise
    except Exception:
        if write:
            connection.rollback()
        raise
    finally:
        connection.close()
        if write and os.path.exists(path):
            os.chmod(path, 0o600)


def initialize_database(path: str = EVIDENCE_DB_PATH) -> None:
    with database(path, write=True):
        pass


@contextmanager
def single_writer_lock(lock_path: str = EVIDENCE_LOCK_PATH):
    """Erlaubt genau einen Diagnose-Writer pro privatem Zustandspfad."""

    directory = os.path.dirname(os.path.abspath(lock_path))
    if os.path.lexists(directory) and os.path.islink(directory):
        raise EvidenceLimitError("private_state_directory_symlink")
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise EvidenceLimitError("single_writer_already_active") from exc
        yield
    finally:
        try:
            fcntl.flock(descriptor, fcntl.LOCK_UN)
        finally:
            os.close(descriptor)


def _normalized_forecast_slot(slot: dict[str, Any]) -> dict[str, Any] | None:
    start_ms = _finite_optional(slot.get("start_timestamp"), nonnegative=True)
    end_ms = _finite_optional(slot.get("end_timestamp"), nonnegative=True)
    if start_ms is None or end_ms is None:
        return None
    start_s = int(round(start_ms / 1000.0))
    end_s = int(round(end_ms / 1000.0))
    if start_s <= 0 or start_s % 900 != 0 or end_s - start_s != 900:
        return None
    if str(slot.get("pv_topology_status") or "") != "bound":
        return None
    dc_w = _finite_optional(slot.get("e3dc_dc_pv_w"), nonnegative=True)
    if dc_w is None:
        return None
    return {
        "slot_start_utc_s": start_s,
        "slot_end_utc_s": end_s,
        "predicted_e3dc_dc_energy_wh": round(dc_w * 0.25, 6),
        "source_fresh": 1 if slot.get("pv_forecast_fresh") is True else 0,
        "source_quality": str(slot.get("pv_topology_quality") or "unknown")[:80],
    }


def archive_forecast_snapshot(
    slots: Iterable[dict[str, Any]],
    *,
    issued_at_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
    min_interval_s: int = ARCHIVE_MIN_INTERVAL_S,
) -> dict[str, Any]:
    """Archiviert höchstens alle sechs Stunden eine topologiegebundene Ausgabe."""

    issued = int(time.time() if issued_at_utc_s is None else issued_at_utc_s)
    normalized: list[dict[str, Any]] = []
    revisions: set[str] = set()
    for slot in slots or ():
        if not isinstance(slot, dict):
            continue
        revision = _valid_revision(slot.get("pv_topology_revision"))
        item = _normalized_forecast_slot(slot)
        if revision is None or item is None:
            continue
        revisions.add(revision)
        normalized.append(item)
    if not normalized:
        return {"archived": False, "reason": "no_bound_forecast_slots"}
    if len(revisions) != 1:
        return {"archived": False, "reason": "topology_revision_missing_or_mixed"}
    revision = next(iter(revisions))
    normalized.sort(key=lambda item: item["slot_start_utc_s"])

    with database(database_path, write=True) as connection:
        last_issue = connection.execute(
            """
            SELECT issued_at_utc_s
            FROM forecast_issues
            WHERE topology_revision = ?
            ORDER BY issued_at_utc_s DESC, issue_id DESC
            LIMIT 1
            """,
            (revision,),
        ).fetchone()
        if last_issue and issued - int(last_issue[0]) < max(
            ARCHIVE_MIN_INTERVAL_S,
            int(min_interval_s),
        ):
            return {
                "archived": False,
                "reason": "archive_cadence",
                "topology_revision": revision,
            }
        issue_material = {
            "schema_version": EVIDENCE_SCHEMA,
            "issued_at_utc_s": issued,
            "topology_revision": revision,
            "slots": normalized,
        }
        issue_id = _sha256_record("forecast", issue_material)
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO forecast_issues(
                issue_id, issued_at_utc_s, topology_revision, operation_mode,
                control_effect, configuration_writes, automatic_model_selection,
                created_at_utc_s
            ) VALUES(?, ?, ?, ?, 0, 0, 0, ?)
            """,
            (issue_id, issued, revision, OPERATION_MODE, issued),
        )
        if cursor.rowcount:
            connection.executemany(
                """
                INSERT INTO forecast_slots(
                    issue_id, slot_start_utc_s, slot_end_utc_s,
                    predicted_e3dc_dc_energy_wh, source_fresh, source_quality
                ) VALUES(
                    :issue_id, :slot_start_utc_s, :slot_end_utc_s,
                    :predicted_e3dc_dc_energy_wh, :source_fresh, :source_quality
                )
                """,
                [{"issue_id": issue_id, **item} for item in normalized],
            )
    return {
        "archived": True,
        "inserted": bool(cursor.rowcount),
        "issue_id": issue_id,
        "topology_revision": revision,
        "slot_count": len(normalized),
    }


def store_history_observations(
    history_slots: Iterable[dict[str, Any]],
    *,
    topology_revision: str,
    observed_at_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, Any]:
    """Speichert History nur für die ausdrücklich aktuelle Topologierevision."""

    revision = _valid_revision(topology_revision)
    if revision is None:
        return {"inserted": 0, "reason": "topology_revision_invalid"}
    observed = int(time.time() if observed_at_utc_s is None else observed_at_utc_s)
    records: list[tuple[Any, ...]] = []
    for slot in history_slots or ():
        if not isinstance(slot, dict):
            continue
        start_s = int(slot.get("slot_start_utc_s") or 0)
        end_s = int(slot.get("slot_end_utc_s") or 0)
        if start_s <= 0 or start_s % 900 != 0 or end_s - start_s != 900:
            continue
        source_contract = str(slot.get("source_contract") or "")
        valid = bool(
            slot.get("valid") is True
            and slot.get("history_contract_valid") is True
            and source_contract == HISTORY_SOURCE_CONTRACT
        )
        energy_wh = (
            _finite_optional(slot.get("e3dc_dc_energy_wh"), nonnegative=True)
            if valid
            else None
        )
        if energy_wh is None:
            valid = False
        reason = "ok" if valid else str(slot.get("reason") or "history_contract_invalid")[:80]
        material = {
            "slot_start_utc_s": start_s,
            "slot_end_utc_s": end_s,
            "topology_revision": revision,
            "source_contract": source_contract,
            "actual_e3dc_dc_energy_wh": energy_wh,
            "valid": valid,
            "reason": reason,
        }
        records.append(
            (
                _sha256_record("observation", material),
                start_s,
                end_s,
                revision,
                source_contract,
                energy_wh,
                1 if valid else 0,
                reason,
                observed,
            )
        )
    inserted = 0
    with database(database_path, write=True) as connection:
        for record in records:
            cursor = connection.execute(
                """
                INSERT OR IGNORE INTO observed_slots(
                    observation_id, slot_start_utc_s, slot_end_utc_s,
                    topology_revision, source_contract,
                    actual_e3dc_dc_energy_wh, valid, reason, observed_at_utc_s
                )
                SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?
                WHERE EXISTS(
                    SELECT 1
                    FROM forecast_slots AS slot
                    JOIN forecast_issues AS issue ON issue.issue_id = slot.issue_id
                    WHERE slot.slot_start_utc_s = ?
                      AND issue.topology_revision = ?
                )
                """,
                (*record, record[1], revision),
            )
            inserted += max(0, int(cursor.rowcount))
    return {"inserted": inserted, "topology_revision": revision}


def enforce_retention(
    *,
    now_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, int]:
    """Löscht ausschließlich abgeschlossene Rohdaten außerhalb von 90 UTC-Tagen."""

    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    cutoff = now_s - RAW_RETENTION_S
    deleted = {"observations": 0, "forecasts": 0, "summaries": 0}
    with database(database_path, write=True) as connection:
        cursor = connection.execute(
            "DELETE FROM observed_slots WHERE slot_end_utc_s < ?",
            (cutoff,),
        )
        deleted["observations"] = max(0, int(cursor.rowcount))
        cursor = connection.execute(
            """
            DELETE FROM forecast_issues
            WHERE issue_id IN (
                SELECT issue.issue_id
                FROM forecast_issues AS issue
                LEFT JOIN forecast_slots AS slot ON slot.issue_id = issue.issue_id
                GROUP BY issue.issue_id
                HAVING MAX(COALESCE(slot.slot_end_utc_s, 0)) < ?
            )
            """,
            (cutoff,),
        )
        deleted["forecasts"] = max(0, int(cursor.rowcount))
        cursor = connection.execute(
            "DELETE FROM diagnostic_summaries WHERE calculated_at_utc_s < ?",
            (cutoff,),
        )
        deleted["summaries"] = max(0, int(cursor.rowcount))
    return deleted


def calculate_diagnostic_summary(
    connection: sqlite3.Connection,
    topology_revision: str,
    *,
    now_utc_s: int | None = None,
) -> dict[str, Any]:
    """Berechnet die 90-Tage-Auswertung mit genau einer mengenbasierten Abfrage."""

    revision = _valid_revision(topology_revision)
    if revision is None:
        raise ValueError("topology_revision_invalid")
    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    evaluation_cutoff = now_s - MIN_EVALUATION_DELAY_S
    window_cutoff = now_s - EVALUATION_WINDOW_S
    row = connection.execute(
        """
        WITH ranked_forecasts AS (
            SELECT
                slot.slot_start_utc_s,
                slot.predicted_e3dc_dc_energy_wh AS predicted_wh,
                ROW_NUMBER() OVER (
                    PARTITION BY slot.slot_start_utc_s
                    ORDER BY issue.issued_at_utc_s DESC, issue.issue_id DESC
                ) AS rank_no
            FROM forecast_slots AS slot
            JOIN forecast_issues AS issue ON issue.issue_id = slot.issue_id
            WHERE issue.topology_revision = :revision
              AND issue.issued_at_utc_s <= slot.slot_start_utc_s
              AND slot.slot_start_utc_s >= :window_cutoff
              AND slot.slot_end_utc_s <= :evaluation_cutoff
              AND slot.predicted_e3dc_dc_energy_wh IS NOT NULL
              AND slot.source_fresh = 1
        ),
        compared AS (
            SELECT
                forecast.slot_start_utc_s,
                forecast.predicted_wh,
                (
                    SELECT
                        CASE
                            WHEN observation.valid = 1
                            THEN observation.actual_e3dc_dc_energy_wh
                            ELSE NULL
                        END
                    FROM observed_slots AS observation
                        INDEXED BY idx_observed_summary_rank
                    WHERE observation.topology_revision = :revision
                      AND observation.source_contract = :source_contract
                      AND observation.slot_start_utc_s = forecast.slot_start_utc_s
                    ORDER BY
                        observation.valid DESC,
                        observation.observed_at_utc_s DESC,
                        observation.observation_id DESC
                    LIMIT 1
                ) AS actual_wh
            FROM ranked_forecasts AS forecast
            WHERE forecast.rank_no = 1
        ),
        relevant AS (
            SELECT *
            FROM compared
            WHERE actual_wh IS NOT NULL
              AND MAX(predicted_wh, actual_wh) >= :relevant_wh
        )
        SELECT
            (SELECT COUNT(*) FROM compared) AS eligible_slots,
            (SELECT COUNT(*) FROM compared WHERE actual_wh IS NOT NULL) AS compared_slots,
            (SELECT COUNT(*) FROM relevant) AS relevant_slots,
            (SELECT COUNT(DISTINCT date(slot_start_utc_s, 'unixepoch')) FROM relevant)
                AS relevant_days,
            (SELECT AVG(ABS(actual_wh - predicted_wh)) FROM relevant)
                AS trefferabweichung_wh,
            (SELECT AVG(actual_wh - predicted_wh) FROM relevant)
                AS richtungsversatz_wh,
            (SELECT
                CASE
                    WHEN SUM(actual_wh) > 0
                    THEN SUM(ABS(actual_wh - predicted_wh)) / SUM(actual_wh) * 100.0
                    ELSE NULL
                END
             FROM relevant) AS energiegewichtete_gesamtabweichung_pct
        """,
        {
            "revision": revision,
            "source_contract": HISTORY_SOURCE_CONTRACT,
            "window_cutoff": window_cutoff,
            "evaluation_cutoff": evaluation_cutoff,
            "relevant_wh": YIELD_RELEVANT_ENERGY_WH,
        },
    ).fetchone()

    eligible = int(row["eligible_slots"] or 0)
    compared = int(row["compared_slots"] or 0)
    relevant = int(row["relevant_slots"] or 0)
    relevant_days = int(row["relevant_days"] or 0)
    sufficient = relevant >= MIN_RELEVANT_SLOTS and relevant_days >= MIN_RELEVANT_DAYS
    provisional_reasons: list[str] = []
    if relevant < MIN_RELEVANT_SLOTS:
        provisional_reasons.append("zu_wenige_ertragsrelevante_slots")
    if relevant_days < MIN_RELEVANT_DAYS:
        provisional_reasons.append("zu_wenige_vergleichstage")

    def rounded(name: str) -> float | None:
        value = _finite_optional(row[name])
        return None if value is None else round(value, 3)

    return {
        "schema_version": SUMMARY_SCHEMA,
        "calculated_at_utc_s": now_s,
        "topology_revision": revision,
        "status": "belastbar" if sufficient else "vorläufig",
        "available": True,
        "provisional": not sufficient,
        "provisional_reasons": provisional_reasons,
        "operation_mode": OPERATION_MODE,
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "evaluation_window_days": RAW_RETENTION_S // 86400,
        "evaluation_delay_minutes": MIN_EVALUATION_DELAY_S // 60,
        "minimum_relevant_slots": MIN_RELEVANT_SLOTS,
        "minimum_relevant_days": MIN_RELEVANT_DAYS,
        "eligible_forecast_slots": eligible,
        "compared_slots": compared,
        "yield_relevant_slots": relevant,
        "yield_relevant_days": relevant_days,
        "metrics": {
            "trefferabweichung_wh": rounded("trefferabweichung_wh"),
            "richtungsversatz_wh": rounded("richtungsversatz_wh"),
            "energiegewichtete_gesamtabweichung_pct": rounded(
                "energiegewichtete_gesamtabweichung_pct"
            ),
            "vergleichsabdeckung_pct": (
                round(compared / eligible * 100.0, 3) if eligible else None
            ),
        },
        "labels": dict(DIAGNOSTIC_LABELS),
    }


def append_summary_if_due(
    *,
    topology_revision: str,
    now_utc_s: int | None = None,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, Any]:
    """Materialisiert pro Topologie höchstens einmal täglich eine Zusammenfassung."""

    revision = _valid_revision(topology_revision)
    if revision is None:
        raise ValueError("topology_revision_invalid")
    now_s = int(time.time() if now_utc_s is None else now_utc_s)
    with database(database_path, write=True) as connection:
        latest = connection.execute(
            """
            SELECT payload_json, calculated_at_utc_s
            FROM diagnostic_summaries
            WHERE topology_revision = ?
            ORDER BY calculated_at_utc_s DESC, summary_id DESC
            LIMIT 1
            """,
            (revision,),
        ).fetchone()
        if latest and now_s - int(latest["calculated_at_utc_s"]) < SUMMARY_MIN_INTERVAL_S:
            return json.loads(str(latest["payload_json"]))
        payload = calculate_diagnostic_summary(
            connection,
            revision,
            now_utc_s=now_s,
        )
        summary_id = _sha256_record(
            "summary",
            {"topology_revision": revision, "payload": payload},
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO diagnostic_summaries(
                summary_id, topology_revision, calculated_at_utc_s, payload_json
            ) VALUES(?, ?, ?, ?)
            """,
            (summary_id, revision, now_s, _canonical_json(payload)),
        )
        return payload


def latest_summary_for_topology(
    topology_revision: str,
    *,
    database_path: str = EVIDENCE_DB_PATH,
) -> dict[str, Any] | None:
    revision = _valid_revision(topology_revision)
    if revision is None or not os.path.exists(database_path):
        return None
    with database(database_path, write=False) as connection:
        row = connection.execute(
            """
            SELECT payload_json
            FROM diagnostic_summaries
            WHERE topology_revision = ?
            ORDER BY calculated_at_utc_s DESC, summary_id DESC
            LIMIT 1
            """,
            (revision,),
        ).fetchone()
    if not row:
        return None
    try:
        payload = json.loads(str(row["payload_json"]))
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def unavailable_summary(
    topology_revision: str | None,
    reason: str,
    *,
    now_utc_s: int | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": SUMMARY_SCHEMA,
        "calculated_at_utc_s": int(time.time() if now_utc_s is None else now_utc_s),
        "topology_revision": _valid_revision(topology_revision),
        "status": "nicht_verfügbar",
        "available": False,
        "reason": str(reason or "evidence_unavailable")[:80],
        "operation_mode": OPERATION_MODE,
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "metrics": {},
        "labels": dict(DIAGNOSTIC_LABELS),
    }


def _sanitized_summary(payload: dict[str, Any]) -> dict[str, Any]:
    metrics = payload.get("metrics") if isinstance(payload.get("metrics"), dict) else {}
    sanitized_metrics: dict[str, float | None] = {}
    for key in DIAGNOSTIC_LABELS:
        value = _finite_optional(metrics.get(key))
        sanitized_metrics[key] = None if value is None else round(value, 3)
    return {
        "schema_version": SUMMARY_SCHEMA,
        "calculated_at_utc_s": int(payload.get("calculated_at_utc_s") or 0),
        "topology_revision": _valid_revision(payload.get("topology_revision")),
        "status": str(payload.get("status") or "nicht_verfügbar")[:32],
        "available": payload.get("available") is True,
        "reason": str(payload.get("reason") or "")[:80],
        "provisional": payload.get("provisional") is True,
        "provisional_reasons": [
            str(item)[:80]
            for item in (payload.get("provisional_reasons") or [])
            if isinstance(item, str)
        ][:8],
        "operation_mode": OPERATION_MODE,
        "control_effect": False,
        "configuration_writes": False,
        "automatic_model_selection": False,
        "evaluation_window_days": int(payload.get("evaluation_window_days") or 0),
        "evaluation_delay_minutes": int(payload.get("evaluation_delay_minutes") or 0),
        "minimum_relevant_slots": int(payload.get("minimum_relevant_slots") or 0),
        "minimum_relevant_days": int(payload.get("minimum_relevant_days") or 0),
        "eligible_forecast_slots": int(payload.get("eligible_forecast_slots") or 0),
        "compared_slots": int(payload.get("compared_slots") or 0),
        "yield_relevant_slots": int(payload.get("yield_relevant_slots") or 0),
        "yield_relevant_days": int(payload.get("yield_relevant_days") or 0),
        "metrics": sanitized_metrics,
        "labels": dict(DIAGNOSTIC_LABELS),
    }


def publish_summary_json(
    payload: dict[str, Any],
    *,
    summary_path: str = SUMMARY_JSON_PATH,
) -> None:
    """Veröffentlicht ausschließlich eine sanitierte Summary atomar."""

    directory = os.path.dirname(os.path.abspath(summary_path))
    if os.path.lexists(directory) and os.path.islink(directory):
        raise EvidenceLimitError("summary_directory_symlink")
    os.makedirs(directory, exist_ok=True)
    if os.path.lexists(summary_path) and os.path.islink(summary_path):
        raise EvidenceLimitError("summary_target_symlink")
    encoded = (_canonical_json(_sanitized_summary(payload)) + "\n").encode("utf-8")
    if len(encoded) > MAX_SUMMARY_BYTES:
        raise EvidenceLimitError("summary_size_limit")
    descriptor, temporary_path = tempfile.mkstemp(
        prefix=".pv_forecast_diagnostic_summary.",
        suffix=".tmp",
        dir=directory,
    )
    try:
        os.fchmod(descriptor, 0o644)
        with os.fdopen(descriptor, "wb", closefd=True) as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_path, summary_path)
        os.chmod(summary_path, 0o644)
    finally:
        if os.path.exists(temporary_path):
            os.unlink(temporary_path)


def run_write_step(
    operation: Callable[[], Any],
    *,
    database_path: str = EVIDENCE_DB_PATH,
    lock_path: str | None = None,
) -> Any:
    """Kapselt einen Sidecar-Schreibschritt hinter dem Ein-Writer-Vertrag."""

    effective_lock_path = lock_path or os.path.join(
        os.path.dirname(os.path.abspath(database_path)),
        "writer.lock",
    )
    with single_writer_lock(effective_lock_path):
        return operation()


# Kompatible Funktionsnamen für lokale Diagnosewerkzeuge; produktiv importiert
# ausschließlich der Sidecar dieses Modul.
archive_forecast_issue = archive_forecast_snapshot
