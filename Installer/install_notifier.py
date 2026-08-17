"""Transaktionale Installation des Benachrichtigungs-Dienstes."""

from __future__ import annotations

from contextlib import contextmanager, nullcontext
import copy
from dataclasses import dataclass, field
from datetime import datetime, timezone
import grp
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import secrets
import shlex
import stat
import subprocess
from typing import Mapping, Sequence

from .config_secret_permissions import config_secret_file_mode
from .core import register_command
from .installer_config import get_install_path, get_install_user
from .logging_manager import log_task_completed
from .secure_file_transaction import (
    atomic_write_bound_file,
    exclusive_transaction_lock,
    open_bound_directory,
    restore_bound_file,
    snapshot_bound_file,
    snapshots_match,
)


LEGACY_NOTIFY_PATH = "/usr/local/bin/boot_notify.sh"
V4_CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"
SYSTEM_CRONTAB_PATH = "/etc/crontab"
NOTIFIER_SERVICE = "e3dc-notifier"
NOTIFIER_UNIT = "e3dc-notifier.service"
NOTIFIER_UNIT_PATH = "/etc/systemd/system/e3dc-notifier.service"
NOTIFIER_DROPIN_DIR = "/etc/systemd/system/e3dc-notifier.service.d"
SYSTEMD_TRANSACTION_STAGING_ROOT = "/etc/systemd/system"
LEGACY_TRANSACTION_STAGING_NAME = ".e3dc-control-transactions"
CRONTAB_BIN = "/usr/bin/crontab"
NOTIFIER_WATCHDOG_JOURNAL_ROOT = "/var/lib/e3dc-control/notifier-watchdog-transitions"
NOTIFIER_WATCHDOG_SCHEMA = 1
_OUTER_JOURNAL_MAX_BYTES = 8 * 1024 * 1024
_LEGACY_CRON_MARKERS = (
    "boot_notify.sh",
    "send_daily_telegram.php",
    "send_weekly_telegram.php",
    "send_status_telegram.php",
    "backup_history.php",
    "get_live_json.php",
    "sqlite_archiver.py",
    "diagram_helpers.py",
    "plot_soc_changes.py",
)


class NotifierInstallError(RuntimeError):
    """Die Notifier-Transaktion konnte nicht vollständig bestätigt werden."""


class NotifierRecoveryRequired(NotifierInstallError):
    """Der Vorzustand konnte nicht vollständig wiederhergestellt werden."""


class _NotifierAmbiguousState(NotifierRecoveryRequired):
    """Eine automatische Roll-forward-/Rollback-Wahl wäre nicht sicher."""


@dataclass
class _NotifierTransaction:
    install_user: str
    install_uid: int
    www_gid: int
    service_snapshot: dict[str, object]
    script_snapshot: Mapping[str, object]
    cron_preimages: dict[str, bytes | None]
    cron_postimages: dict[str, bytes | None] = field(default_factory=dict)
    file_preimages: dict[str, Mapping[str, object]] = field(default_factory=dict)
    file_postimages: dict[str, Mapping[str, object]] = field(default_factory=dict)
    uncertain_files: set[str] = field(default_factory=set)
    service_touched: bool = False
    service_original_snapshot: dict[str, object] | None = None
    start_block_preimage: Mapping[str, object] | None = None
    start_block_postimage: Mapping[str, object] | None = None
    start_block_path: str = ""
    start_block_marker: str = ""
    outer_journal: "_NotifierWatchdogJournal | None" = None
    outer_tx_dir: Path | None = None
    outer_record: dict[str, object] | None = None


def _outer_utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class _NotifierWatchdogJournal:
    """Root-privates, dauerhaftes Journal der äußeren Diensttransaktion."""

    def __init__(self, root: str = NOTIFIER_WATCHDOG_JOURNAL_ROOT) -> None:
        self.root = Path(os.path.abspath(root))
        self.recovery_status_path = self.root / "recovery-required.json"

    @staticmethod
    def _assert_no_symlink_components(path: Path) -> None:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            if os.path.lexists(current) and stat.S_ISLNK(os.lstat(current).st_mode):
                raise NotifierRecoveryRequired(
                    "Notifier-Außentransaktion enthält einen Symlink im Autoritätspfad"
                )

    @staticmethod
    def _directory_flags() -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory:
            raise NotifierRecoveryRequired(
                "Notifier-Journal benötigt O_NOFOLLOW und O_DIRECTORY"
            )
        return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        descriptor = os.open(path, _NotifierWatchdogJournal._directory_flags())
        try:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise NotifierRecoveryRequired("Journal-Elternpfad ist kein Verzeichnis")
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _require_private_dir(path: Path) -> os.stat_result:
        metadata = os.lstat(path)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise NotifierRecoveryRequired(
                f"Notifier-Journalverzeichnis ist nicht root-privat: {path}"
            )
        return metadata

    def ensure_root(self) -> None:
        if os.geteuid() != 0:
            raise NotifierInstallError("Notifier-Journal benötigt Root-Rechte")
        flags = self._directory_flags()
        descriptor = os.open("/", flags)
        current = Path("/")
        try:
            components = self.root.parts[1:]
            for index, component in enumerate(components):
                final = index == len(components) - 1
                created = False
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(component, 0o700 if final else 0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                    created = True
                metadata = os.fstat(next_descriptor)
                current /= component
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    os.close(next_descriptor)
                    raise NotifierRecoveryRequired(
                        f"Notifier-Journal-Elternpfad ist nicht root-kontrolliert: {current}"
                    )
                if final:
                    os.fchown(next_descriptor, 0, 0)
                    os.fchmod(next_descriptor, 0o700)
                    os.fsync(next_descriptor)
                    final_metadata = os.fstat(next_descriptor)
                    if (
                        final_metadata.st_uid != 0
                        or final_metadata.st_gid != 0
                        or stat.S_IMODE(final_metadata.st_mode) != 0o700
                    ):
                        os.close(next_descriptor)
                        raise NotifierRecoveryRequired(
                            "Notifier-Journalroot ist nicht root:root 0700"
                        )
                elif created:
                    os.fchown(next_descriptor, 0, 0)
                    os.fchmod(next_descriptor, 0o755)
                    os.fsync(next_descriptor)
                if created:
                    os.fsync(descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        finally:
            os.close(descriptor)
        self._require_private_dir(self.root)

    @staticmethod
    def _read_regular(path: Path) -> bytes:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise NotifierRecoveryRequired("Notifier-Journal benötigt O_NOFOLLOW")
        descriptor = os.open(
            path,
            os.O_RDONLY
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
        )
        try:
            before = os.fstat(descriptor)
            if (
                not stat.S_ISREG(before.st_mode)
                or before.st_nlink != 1
                or before.st_uid != 0
                or before.st_gid != 0
                or stat.S_IMODE(before.st_mode) != 0o600
                or before.st_size < 0
                or before.st_size > _OUTER_JOURNAL_MAX_BYTES
            ):
                raise NotifierRecoveryRequired(
                    f"Notifier-Journaldatei ist nicht eindeutig root-privat: {path}"
                )
            chunks: list[bytes] = []
            remaining = int(before.st_size)
            while remaining:
                chunk = os.read(descriptor, min(64 * 1024, remaining))
                if not chunk:
                    raise NotifierRecoveryRequired("Notifier-Journaldatei ist verkürzt")
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(descriptor)
            named_after = os.lstat(path)
            identity_before = (
                before.st_dev,
                before.st_ino,
                before.st_size,
                before.st_mtime_ns,
                before.st_ctime_ns,
                before.st_nlink,
            )
            identity_after = (
                after.st_dev,
                after.st_ino,
                after.st_size,
                after.st_mtime_ns,
                after.st_ctime_ns,
                after.st_nlink,
            )
            identity_named = (
                named_after.st_dev,
                named_after.st_ino,
                named_after.st_size,
                named_after.st_mtime_ns,
                named_after.st_ctime_ns,
                named_after.st_nlink,
            )
            if identity_before != identity_after or identity_after != identity_named:
                raise NotifierRecoveryRequired(
                    "Notifier-Journaldatei driftete während des Lesens"
                )
            return b"".join(chunks)
        finally:
            os.close(descriptor)

    def _atomic_write(self, target: Path, payload: bytes) -> None:
        if not isinstance(payload, bytes) or len(payload) > _OUTER_JOURNAL_MAX_BYTES:
            raise NotifierInstallError("Notifier-Journalpayload ist ungültig")
        self._require_private_dir(target.parent)
        if os.path.lexists(target):
            metadata = os.lstat(target)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or metadata.st_uid != 0
                or metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != 0o600
            ):
                raise NotifierRecoveryRequired("Bestehendes Notifier-Journalziel ist unsicher")
        temporary = target.parent / f".{target.name}.tmp-{secrets.token_hex(8)}"
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise NotifierRecoveryRequired("Notifier-Journal benötigt O_NOFOLLOW")
        descriptor = os.open(
            temporary,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
        )
        try:
            view = memoryview(payload)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise NotifierInstallError("Notifier-Journal konnte nicht geschrieben werden")
                view = view[written:]
            os.fchmod(descriptor, 0o600)
            os.fchown(descriptor, 0, 0)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        try:
            if os.path.lexists(target):
                metadata = os.lstat(target)
                if (
                    stat.S_ISLNK(metadata.st_mode)
                    or not stat.S_ISREG(metadata.st_mode)
                    or metadata.st_nlink != 1
                    or metadata.st_uid != 0
                    or metadata.st_gid != 0
                    or stat.S_IMODE(metadata.st_mode) != 0o600
                ):
                    raise NotifierRecoveryRequired(
                        "Notifier-Journalziel driftete vor dem Commit"
                    )
            os.replace(temporary, target)
            self._fsync_dir(target.parent)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass

    def _new_private_dir(self, path: Path) -> None:
        os.mkdir(path, 0o700)
        os.chown(path, 0, 0)
        os.chmod(path, 0o700)
        self._require_private_dir(path)
        self._fsync_dir(path.parent)
        self._fsync_dir(path)

    def _write_blob(self, tx_dir: Path, label: str, payload: bytes) -> dict[str, object]:
        blobs = tx_dir / "blobs"
        self._require_private_dir(blobs)
        safe_label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(label)).strip("-.") or "blob"
        name = f"{safe_label}-{secrets.token_hex(8)}.bin"
        self._atomic_write(blobs / name, payload)
        return {
            "$blob": name,
            "sha256": hashlib.sha256(payload).hexdigest(),
            "size": len(payload),
        }

    def encode(self, tx_dir: Path, value: object, *, label: str) -> object:
        counter = [0]

        def convert(item: object) -> object:
            if isinstance(item, bytes):
                counter[0] += 1
                return self._write_blob(
                    tx_dir,
                    f"{label}-{counter[0]:04d}",
                    item,
                )
            if isinstance(item, Mapping):
                return {str(key): convert(content) for key, content in item.items()}
            if isinstance(item, (tuple, list)):
                return [convert(content) for content in item]
            if item is None or isinstance(item, (str, int, float, bool)):
                return item
            raise NotifierInstallError(
                f"Nicht serialisierbarer Notifier-Journalwert: {type(item).__name__}"
            )

        return convert(value)

    def decode(self, tx_dir: Path, value: object) -> object:
        def convert(item: object) -> object:
            if isinstance(item, list):
                return [convert(content) for content in item]
            if isinstance(item, dict):
                if "$blob" in item:
                    name = str(item.get("$blob") or "")
                    if (
                        not name
                        or name != os.path.basename(name)
                        or not re.fullmatch(r"[A-Za-z0-9_.-]+", name)
                    ):
                        raise NotifierRecoveryRequired("Notifier-Blobname ist ungültig")
                    payload = self._read_regular(tx_dir / "blobs" / name)
                    if (
                        len(payload) != int(item.get("size", -1))
                        or hashlib.sha256(payload).hexdigest()
                        != str(item.get("sha256", ""))
                    ):
                        raise NotifierRecoveryRequired(
                            "Notifier-Blob ist nicht integritätsgebunden"
                        )
                    return payload
                return {str(key): convert(content) for key, content in item.items()}
            return item

        return convert(value)

    def create(
        self,
        transaction: "_NotifierTransaction",
        *,
        watchdog_required: bool,
        start_service: bool,
        migrate_legacy_config: bool,
    ) -> tuple[Path, dict[str, object]]:
        self.ensure_root()
        transaction_id = (
            f"{int(datetime.now(timezone.utc).timestamp())}-{secrets.token_hex(8)}"
        )
        tx_dir = self.root / f"tx-{transaction_id}"
        staging_dir = self.root / (
            f".prepare-{transaction_id}-{secrets.token_hex(8)}"
        )
        child_correlation_id = None
        if watchdog_required:
            used_correlations: set[str] = set()
            for existing_dir in sorted(self.root.glob("tx-*")):
                if existing_dir == tx_dir:
                    continue
                existing = self.read_record(existing_dir)
                existing_correlation = str(existing.get("child_correlation_id") or "")
                if existing_correlation:
                    used_correlations.add(existing_correlation)
            for _attempt in range(8):
                candidate = secrets.token_hex(16)
                if candidate not in used_correlations:
                    child_correlation_id = candidate
                    break
            if child_correlation_id is None:
                raise NotifierRecoveryRequired(
                    "Keine eindeutige Watchdog-Korrelation konnte erzeugt werden"
                )
        self._new_private_dir(staging_dir)
        self._new_private_dir(staging_dir / "blobs")
        prestate = {
            "install_user": transaction.install_user,
            "install_uid": transaction.install_uid,
            "www_gid": transaction.www_gid,
            "script_snapshot": transaction.script_snapshot,
            "cron_preimages": transaction.cron_preimages,
            "file_preimages": transaction.file_preimages,
            "service_snapshot": transaction.service_snapshot,
        }
        record: dict[str, object] = {
            "schema": NOTIFIER_WATCHDOG_SCHEMA,
            "transaction_id": transaction_id,
            "created_at": _outer_utc_now(),
            "updated_at": _outer_utc_now(),
            "state": "in_progress",
            "stage": "prestate_durable",
            "phase": "prestate_durable",
            "watchdog_required": bool(watchdog_required),
            "child_correlation_id": child_correlation_id,
            "watchdog_intent_durable": False,
            "start_service": bool(start_service),
            "migrate_legacy_config": bool(migrate_legacy_config),
            "prestate": self.encode(staging_dir, prestate, label="prestate"),
            "poststate": None,
            "final_poststate": None,
            "decision": None,
            "recovery_start_block": None,
            "mutation": None,
            "applied_steps": [],
        }
        self.write_record(staging_dir, record)
        if os.path.lexists(tx_dir):
            raise NotifierRecoveryRequired("Notifier-Transaktions-ID ist nicht eindeutig")
        os.rename(staging_dir, tx_dir)
        self._fsync_dir(self.root)
        self._require_private_dir(tx_dir)
        return tx_dir, record

    def write_record(self, tx_dir: Path, record: Mapping[str, object]) -> None:
        self._require_private_dir(tx_dir)
        payload = (
            json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
        ).encode("utf-8")
        self._atomic_write(tx_dir / "journal.json", payload)

    def advance(
        self,
        tx_dir: Path,
        record: dict[str, object],
        *,
        stage: str,
        phase: str,
    ) -> None:
        record["stage"] = stage
        record["phase"] = phase
        record["updated_at"] = _outer_utc_now()
        self.write_record(tx_dir, record)

    def intent(
        self,
        tx_dir: Path,
        record: dict[str, object],
        *,
        step: str,
        kind: str,
        key: str,
        expected: object,
    ) -> None:
        record["mutation"] = {
            "step": step,
            "kind": kind,
            "key": key,
            "phase": "intent",
            "expected": self.encode(tx_dir, expected, label=f"intent-{step}"),
        }
        self.advance(
            tx_dir,
            record,
            stage="notifier_in_progress",
            phase=f"{step}:intent",
        )

    def applied(
        self,
        tx_dir: Path,
        record: dict[str, object],
        *,
        actual: object,
    ) -> None:
        mutation = record.get("mutation")
        if not isinstance(mutation, dict) or mutation.get("phase") != "intent":
            raise NotifierRecoveryRequired("Notifier-Journal besitzt kein passendes Intent")
        completed = dict(mutation)
        completed["phase"] = "applied"
        completed["actual"] = self.encode(
            tx_dir,
            actual,
            label=f"applied-{str(completed.get('step', 'unknown'))}",
        )
        record["applied_steps"] = [*list(record.get("applied_steps") or []), completed]
        record["mutation"] = completed
        self.advance(
            tx_dir,
            record,
            stage="notifier_in_progress",
            phase=f"{str(completed.get('step', 'unknown'))}:applied",
        )

    def read_record(self, tx_dir: Path) -> dict[str, object]:
        self._require_private_dir(tx_dir)
        payload = self._read_regular(tx_dir / "journal.json")
        try:
            record = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise NotifierRecoveryRequired("Notifier-Außenjournal ist nicht lesbar") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema") != NOTIFIER_WATCHDOG_SCHEMA
            or not re.fullmatch(
                r"[0-9]+-[0-9a-f]{16}",
                str(record.get("transaction_id") or ""),
            )
            or tx_dir.name != f"tx-{record.get('transaction_id')}"
        ):
            raise NotifierRecoveryRequired("Notifier-Außenjournal besitzt ein fremdes Schema")
        state = str(record.get("state") or "")
        stage = str(record.get("stage") or "")
        allowed = {
            "in_progress": {
                "prestate_durable",
                "start_block_intent",
                "start_block_written",
                "start_block_active",
                "notifier_in_progress",
                "notifier_prepared",
                "watchdog_intent",
                "outer_commit_durable",
                "rollback_in_progress",
                "outer_rollback_durable",
            },
            "committed": {"committed"},
            "rolled_back": {"rolled_back"},
            "recovery_required": {"recovery_required"},
        }
        if state not in allowed or stage not in allowed[state]:
            raise NotifierRecoveryRequired("Notifier-Außenjournal besitzt einen fremden Zustand")
        decision = record.get("decision")
        if decision not in {None, "commit", "rollback"}:
            raise NotifierRecoveryRequired("Notifier-Außenjournal besitzt fremde Entscheidung")
        if state == "in_progress":
            expected_decision = {
                "outer_commit_durable": "commit",
                "outer_rollback_durable": "rollback",
            }.get(stage)
            if decision != expected_decision:
                raise NotifierRecoveryRequired(
                    "Notifier-Außenjournal besitzt widersprüchliche Entscheidungsphase"
                )
        elif state == "committed" and decision != "commit":
            raise NotifierRecoveryRequired("Notifier-Commit ist nicht dauerhaft entschieden")
        elif state == "rolled_back" and decision != "rollback":
            raise NotifierRecoveryRequired("Notifier-Rollback ist nicht dauerhaft entschieden")
        correlation = record.get("child_correlation_id")
        watchdog_intent = record.get("watchdog_intent_durable")
        if not isinstance(watchdog_intent, bool):
            raise NotifierRecoveryRequired("Notifier-Außenjournal besitzt fremde Kindabsicht")
        if bool(record.get("watchdog_required")):
            if not isinstance(correlation, str) or not re.fullmatch(
                r"[0-9a-f]{32}",
                correlation,
            ):
                raise NotifierRecoveryRequired("Notifier-Außenjournal besitzt keine Korrelation")
        elif correlation is not None or watchdog_intent:
            raise NotifierRecoveryRequired("Notifier-Außenjournal besitzt eine fremde Korrelation")
        if watchdog_intent and stage in {
            "prestate_durable",
            "start_block_intent",
            "start_block_written",
            "start_block_active",
            "notifier_in_progress",
            "notifier_prepared",
        }:
            raise NotifierRecoveryRequired("Notifier-Kindabsicht widerspricht der Außenphase")
        recovery_block = record.get("recovery_start_block")
        if recovery_block is not None and not isinstance(recovery_block, dict):
            raise NotifierRecoveryRequired("Notifier-Recoveryblockjournal ist ungültig")
        self._require_private_dir(tx_dir / "blobs")
        return record

    def pending(self) -> list[tuple[Path, dict[str, object]]]:
        self.ensure_root()
        result: list[tuple[Path, dict[str, object]]] = []
        for tx_dir in sorted(self.root.glob("tx-*")):
            if tx_dir.is_symlink() or not tx_dir.is_dir():
                raise NotifierRecoveryRequired("Unsicherer Eintrag im Notifier-Außenjournal")
            record = self.read_record(tx_dir)
            if record.get("state") in {"in_progress", "recovery_required"}:
                result.append((tx_dir, record))
        return result

    def mark_recovery_required(
        self,
        tx_dir: Path,
        record: dict[str, object],
        errors: Sequence[str],
    ) -> None:
        record["state"] = "recovery_required"
        record["recovery_errors"] = [str(error) for error in errors]
        self.advance(
            tx_dir,
            record,
            stage="recovery_required",
            phase="recovery_required",
        )
        status = {
            "schema": NOTIFIER_WATCHDOG_SCHEMA,
            "status": "recovery_required",
            "transaction_id": record.get("transaction_id"),
            "updated_at": _outer_utc_now(),
            "errors": [str(error) for error in errors],
        }
        self._atomic_write(
            self.recovery_status_path,
            (json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n").encode(
                "utf-8"
            ),
        )

    def clear_recovery_status_if_safe(self) -> None:
        if self.pending():
            return
        if os.path.lexists(self.recovery_status_path):
            self._read_regular(self.recovery_status_path)
            os.unlink(self.recovery_status_path)
            self._fsync_dir(self.root)


def _outer_intent(
    transaction: _NotifierTransaction,
    *,
    kind: str,
    key: str,
    expected: object,
) -> str | None:
    if (
        transaction.outer_journal is None
        or transaction.outer_tx_dir is None
        or transaction.outer_record is None
    ):
        return None
    index = len(list(transaction.outer_record.get("applied_steps") or [])) + 1
    step = f"{index:02d}-{kind}-{hashlib.sha256(key.encode('utf-8')).hexdigest()[:12]}"
    transaction.outer_journal.intent(
        transaction.outer_tx_dir,
        transaction.outer_record,
        step=step,
        kind=kind,
        key=key,
        expected=expected,
    )
    return step


def _outer_applied(transaction: _NotifierTransaction, *, actual: object) -> None:
    if (
        transaction.outer_journal is None
        or transaction.outer_tx_dir is None
        or transaction.outer_record is None
    ):
        return
    transaction.outer_journal.applied(
        transaction.outer_tx_dir,
        transaction.outer_record,
        actual=actual,
    )


def _capture_service_live_state(service_snapshot: Mapping[str, object]) -> dict[str, object]:
    from .utils import _systemd_show_contract

    result: dict[str, object] = {}
    for unit in service_snapshot:
        state = _systemd_show_contract(str(unit))
        result[str(unit)] = {
            key: state.get(key)
            for key in (
                "load_state",
                "active_state",
                "unit_file_state",
                "fragment_path",
                "dropin_paths",
                "service_user",
                "exec_start",
            )
        }
    return result


def _notifier_poststate(transaction: _NotifierTransaction) -> dict[str, object]:
    files = {
        path: transaction.file_postimages.get(path, preimage)
        for path, preimage in transaction.file_preimages.items()
        if path != LEGACY_NOTIFY_PATH
    }
    crons = {
        user: transaction.cron_postimages.get(user, preimage)
        for user, preimage in transaction.cron_preimages.items()
    }
    return {
        "script_snapshot": transaction.script_snapshot,
        "files": files,
        "crons": crons,
        "service_snapshot": transaction.service_snapshot,
        "service_live_state": _capture_service_live_state(transaction.service_snapshot),
        "start_block_path": transaction.start_block_path,
        "start_block_marker": transaction.start_block_marker,
        "start_block_preimage": transaction.start_block_preimage,
        "start_block_postimage": transaction.start_block_postimage,
    }


def _snapshot_current(expected: Mapping[str, object]) -> Mapping[str, object]:
    return snapshot_bound_file(
        str(expected["path"]),
        allow_missing=True,
    )


def _file_matches_snapshot(expected: Mapping[str, object]) -> bool:
    try:
        return snapshots_match(
            _snapshot_current(expected),
            expected,
            exact_metadata=True,
        )
    except Exception:
        return False


def _restored_preimage_matches_current(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    if not bool(expected.get("exists")):
        return not bool(current.get("exists"))
    return bool(
        current.get("exists")
        and current.get("kind") == "regular"
        and current.get("sha256") == expected.get("sha256")
        and current.get("uid") == expected.get("uid")
        and current.get("gid") == expected.get("gid")
        and current.get("mode") == expected.get("mode")
    )


def _file_matches_restored_preimage(expected: Mapping[str, object]) -> bool:
    """Belegt restaurierte Bytes/Rechte, ohne den ersetzten Inode vorzutäuschen."""

    try:
        current = _snapshot_current(expected)
    except Exception:
        return False
    return _restored_preimage_matches_current(current, expected)


def _file_matches_intent(
    current: Mapping[str, object],
    expected: Mapping[str, object],
) -> bool:
    payload = expected.get("payload")
    if not isinstance(payload, bytes):
        return False
    return bool(
        current.get("exists")
        and current.get("kind") == "regular"
        and current.get("sha256") == hashlib.sha256(payload).hexdigest()
        and current.get("uid") == int(expected.get("uid", -1))
        and current.get("gid") == int(expected.get("gid", -1))
        and current.get("mode") == int(expected.get("mode", -1))
    )


def _service_state_matches(
    service_snapshot: Mapping[str, object],
    *,
    use_postimage: bool,
    expected_live: Mapping[str, object] | None = None,
) -> bool:
    from .utils import (
        _read_bound_unit_preimage,
        _systemd_effective_contract_matches,
        _systemd_show_contract,
        _unit_preimages_match,
    )

    try:
        for raw_unit, raw_snapshot in service_snapshot.items():
            unit = str(raw_unit)
            if not isinstance(raw_snapshot, Mapping):
                return False
            expected_file = raw_snapshot.get("postimage" if use_postimage else "preimage")
            current_file = _read_bound_unit_preimage(str(raw_snapshot["path"]))
            if use_postimage:
                if not _unit_preimages_match(current_file, expected_file):
                    return False
            elif expected_file is None:
                if current_file is not None:
                    return False
            elif not isinstance(current_file, Mapping) or not isinstance(
                expected_file,
                Mapping,
            ):
                return False
            elif any(
                current_file.get(key) != expected_file.get(key)
                for key in ("bytes", "uid", "gid", "mode", "nlink")
            ):
                return False
            current_state = _systemd_show_contract(unit)
            if use_postimage:
                if expected_file is None:
                    return False
                effective = raw_snapshot.get("post_effective") or {}
                dropins = raw_snapshot.get("post_dropins") or {}
                if not _systemd_effective_contract_matches(
                    unit,
                    current_state,
                    effective,
                    dropins,
                ):
                    return False
                wanted = (expected_live or {}).get(unit)
                if not isinstance(wanted, Mapping):
                    return False
                for key in (
                    "load_state",
                    "active_state",
                    "unit_file_state",
                    "fragment_path",
                    "dropin_paths",
                    "service_user",
                    "exec_start",
                ):
                    left = current_state.get(key)
                    right = wanted.get(key)
                    if key == "dropin_paths":
                        if set(left or ()) != set(right or ()):
                            return False
                    elif left != right:
                        return False
            else:
                if current_state.get("load_state") != raw_snapshot.get("load_state"):
                    return False
                if current_state.get("active_state") != raw_snapshot.get("active_state"):
                    return False
                if current_state.get("unit_file_state") != raw_snapshot.get("unit_file_state"):
                    return False
                if expected_file is not None and not _systemd_effective_contract_matches(
                    unit,
                    current_state,
                    raw_snapshot.get("pre_effective") or {},
                    raw_snapshot.get("pre_dropins") or {},
                ):
                    return False
        return True
    except Exception:
        return False


def _service_files_match_restored_preimage(
    service_snapshot: Mapping[str, object],
) -> bool:
    from .utils import _read_bound_unit_preimage

    try:
        for raw_snapshot in service_snapshot.values():
            if not isinstance(raw_snapshot, Mapping):
                return False
            expected = raw_snapshot.get("preimage")
            current = _read_bound_unit_preimage(str(raw_snapshot["path"]))
            if expected is None:
                if current is not None:
                    return False
                continue
            if not isinstance(expected, Mapping) or not isinstance(current, Mapping):
                return False
            if any(
                current.get(key) != expected.get(key)
                for key in ("bytes", "uid", "gid", "mode", "nlink")
            ):
                return False
        return True
    except Exception:
        return False


def _service_files_match_exact_postimage(
    service_snapshot: Mapping[str, object],
) -> bool:
    from .utils import _read_bound_unit_preimage, _unit_preimages_match

    try:
        for raw_snapshot in service_snapshot.values():
            if not isinstance(raw_snapshot, Mapping):
                return False
            expected = raw_snapshot.get("postimage")
            if expected is None or not _unit_preimages_match(
                _read_bound_unit_preimage(str(raw_snapshot["path"])),
                expected,
            ):
                return False
        return True
    except Exception:
        return False


def _service_snapshot_for_resumed_prestate_rollback(
    service_snapshot: Mapping[str, object],
) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for unit, raw_snapshot in service_snapshot.items():
        if not isinstance(raw_snapshot, Mapping):
            raise NotifierRecoveryRequired("Notifier-Service-Vorzustand ist ungültig")
        unit_snapshot = dict(raw_snapshot)
        for key in ("postimage", "post_effective", "post_dropins"):
            unit_snapshot.pop(key, None)
        normalized[str(unit)] = unit_snapshot
    return normalized


def _decoded_prestate(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
) -> dict[str, object]:
    decoded = journal.decode(tx_dir, record.get("prestate"))
    if not isinstance(decoded, dict):
        raise NotifierRecoveryRequired("Notifier-Vorzustand fehlt im Außenjournal")
    required = {
        "install_user",
        "install_uid",
        "www_gid",
        "script_snapshot",
        "cron_preimages",
        "file_preimages",
        "service_snapshot",
    }
    if not required.issubset(decoded):
        raise NotifierRecoveryRequired("Notifier-Vorzustand ist unvollständig")
    return decoded


def _decoded_poststate(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
) -> dict[str, object] | None:
    if record.get("poststate") is None:
        return None
    decoded = journal.decode(tx_dir, record.get("poststate"))
    if not isinstance(decoded, dict):
        raise NotifierRecoveryRequired("Notifier-Nachzustand ist unvollständig")
    return decoded


def _decoded_start_block(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
) -> dict[str, object] | None:
    raw = record.get("start_block")
    if raw is None:
        return None
    decoded = journal.decode(tx_dir, raw)
    if not isinstance(decoded, dict):
        raise NotifierRecoveryRequired("Notifier-Startblockjournal ist ungültig")
    return decoded


def _decoded_final_poststate(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
) -> dict[str, object] | None:
    if record.get("final_poststate") is None:
        return None
    decoded = journal.decode(tx_dir, record.get("final_poststate"))
    if not isinstance(decoded, dict):
        raise NotifierRecoveryRequired("Finaler Notifier-Nachzustand ist unvollständig")
    return decoded


def _validated_start_block(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
    *,
    require_postimage: bool,
) -> dict[str, object]:
    block = _decoded_start_block(journal, tx_dir, record)
    transaction_id = str(record.get("transaction_id") or "")
    if not re.fullmatch(r"[0-9]+-[0-9a-f]{16}", transaction_id):
        raise _NotifierAmbiguousState("Notifier-Transaktions-ID ist ungültig")
    expected_path = os.path.join(
        NOTIFIER_DROPIN_DIR,
        f"90-e3dc-outer-{transaction_id}.conf",
    )
    expected_marker = str(tx_dir / "notifier-start-approved")
    expected_payload = (
        "[Unit]\n"
        f"ConditionPathExists={expected_marker}\n"
    ).encode("utf-8")
    if (
        not isinstance(block, dict)
        or block.get("path") != expected_path
        or block.get("marker") != expected_marker
        or block.get("payload") != expected_payload
        or not isinstance(block.get("preimage"), Mapping)
        or block["preimage"].get("path") != expected_path
        or bool(block["preimage"].get("exists"))
        or os.path.lexists(expected_marker)
    ):
        raise _NotifierAmbiguousState("Notifier-Startblockjournal ist nicht eindeutig")
    postimage = block.get("postimage")
    if require_postimage and not isinstance(postimage, Mapping):
        raise _NotifierAmbiguousState("Notifier-Startblock besitzt kein Postimage")
    if isinstance(postimage, Mapping) and postimage.get("path") != expected_path:
        raise _NotifierAmbiguousState("Notifier-Startblock-Postimage gehört zu fremdem Ziel")
    return block


def _transaction_with_persistent_state(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: dict[str, object],
    *,
    require_prepared_poststate: bool,
) -> _NotifierTransaction:
    prestate = _decoded_prestate(journal, tx_dir, record)
    poststate = _decoded_poststate(journal, tx_dir, record)
    if require_prepared_poststate and poststate is None:
        raise NotifierRecoveryRequired("Notifier besitzt keinen vorbereiteten Nachzustand")
    raw_service = (
        poststate.get("service_snapshot")
        if isinstance(poststate, Mapping)
        else prestate.get("service_snapshot")
    )
    if not isinstance(raw_service, Mapping):
        raise NotifierRecoveryRequired("Notifier-Servicezustand ist unvollständig")
    transaction = _NotifierTransaction(
        install_user=str(prestate["install_user"]),
        install_uid=int(prestate["install_uid"]),
        www_gid=int(prestate["www_gid"]),
        service_snapshot=copy.deepcopy(dict(raw_service)),
        service_original_snapshot=copy.deepcopy(dict(prestate["service_snapshot"])),
        script_snapshot=dict(prestate["script_snapshot"]),
        cron_preimages=dict(prestate["cron_preimages"]),
        file_preimages=dict(prestate["file_preimages"]),
        outer_journal=journal,
        outer_tx_dir=tx_dir,
        outer_record=record,
    )
    if isinstance(poststate, Mapping):
        files = poststate.get("files")
        crons = poststate.get("crons")
        if not isinstance(files, Mapping) or not isinstance(crons, Mapping):
            raise NotifierRecoveryRequired("Notifier-Nachzustand ist unvollständig")
        transaction.file_postimages = {
            str(path): snapshot
            for path, snapshot in files.items()
            if path in transaction.file_preimages
            and isinstance(snapshot, Mapping)
            and not snapshots_match(
                snapshot,
                transaction.file_preimages[path],
                exact_metadata=True,
            )
        }
        transaction.cron_postimages = {
            str(user): value
            for user, value in crons.items()
            if user in transaction.cron_preimages
            and (value is None or isinstance(value, bytes))
            and value != transaction.cron_preimages[user]
        }
        transaction.service_touched = True
    if record.get("start_block") is not None:
        block = _validated_start_block(
            journal,
            tx_dir,
            record,
            require_postimage=require_prepared_poststate,
        )
        transaction.start_block_path = str(block["path"])
        transaction.start_block_marker = str(block["marker"])
        transaction.start_block_preimage = block["preimage"]
        transaction.start_block_postimage = (
            block.get("postimage") if isinstance(block.get("postimage"), Mapping) else None
        )
    return transaction


def _notifier_state_matches(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
    *,
    post: bool,
) -> bool:
    prestate = _decoded_prestate(journal, tx_dir, record)
    if post:
        selected = _decoded_poststate(journal, tx_dir, record)
        if selected is None:
            return False
        files = selected.get("files")
        crons = selected.get("crons")
        script = selected.get("script_snapshot")
        service_snapshot = selected.get("service_snapshot")
        service_live = selected.get("service_live_state")
    else:
        raw_files = prestate.get("file_preimages")
        files = (
            {
                path: snapshot
                for path, snapshot in raw_files.items()
                if path != LEGACY_NOTIFY_PATH
            }
            if isinstance(raw_files, Mapping)
            else raw_files
        )
        crons = prestate.get("cron_preimages")
        script = prestate.get("script_snapshot")
        service_snapshot = prestate.get("service_snapshot")
        service_live = None
    if (
        not isinstance(files, Mapping)
        or not isinstance(crons, Mapping)
        or not isinstance(script, Mapping)
        or not isinstance(service_snapshot, Mapping)
    ):
        return False
    if not _file_matches_snapshot(script):
        return False
    for expected in files.values():
        if not isinstance(expected, Mapping) or not (
            _file_matches_snapshot(expected)
            if post
            else _file_matches_restored_preimage(expected)
        ):
            return False
    for user, expected in crons.items():
        try:
            if _capture_crontab(str(user)) != expected:
                return False
        except Exception:
            return False
    block = _decoded_start_block(journal, tx_dir, record)
    if block is not None:
        expected_block = block.get("postimage" if post else "preimage")
        marker = str(block.get("marker") or "")
        if not isinstance(expected_block, Mapping) or not marker:
            return False
        if post:
            if not _file_matches_snapshot(expected_block) or os.path.lexists(marker):
                return False
        elif not _file_matches_snapshot(expected_block):
            return False
    return _service_state_matches(
        service_snapshot,
        use_postimage=post,
        expected_live=service_live if isinstance(service_live, Mapping) else None,
    )


def _prepared_payloads_match(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
) -> bool:
    """Bindet die unveränderlichen Postimages auch während der Commit-Freigabe."""

    try:
        selected = _decoded_poststate(journal, tx_dir, record)
        block = _validated_start_block(
            journal,
            tx_dir,
            record,
            require_postimage=True,
        )
        if selected is None:
            return False
        files = selected.get("files")
        crons = selected.get("crons")
        script = selected.get("script_snapshot")
        service_snapshot = selected.get("service_snapshot")
        if (
            not isinstance(files, Mapping)
            or not isinstance(crons, Mapping)
            or not isinstance(script, Mapping)
            or not isinstance(service_snapshot, Mapping)
            or not _file_matches_snapshot(script)
            or not _service_files_match_exact_postimage(service_snapshot)
            or os.path.lexists(str(block["marker"]))
        ):
            return False
        for expected in files.values():
            if not isinstance(expected, Mapping) or not _file_matches_snapshot(expected):
                return False
        for user, expected in crons.items():
            if _capture_crontab(str(user)) != expected:
                return False
        postimage = block["postimage"]
        preimage = block["preimage"]
        return bool(
            isinstance(postimage, Mapping)
            and (
                _file_matches_snapshot(postimage)
                or _file_matches_snapshot(preimage)
            )
        )
    except Exception:
        return False


def _final_notifier_state_matches(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: Mapping[str, object],
) -> bool:
    try:
        selected = _decoded_final_poststate(journal, tx_dir, record)
        block = _validated_start_block(
            journal,
            tx_dir,
            record,
            require_postimage=True,
        )
        if selected is None:
            return False
        files = selected.get("files")
        crons = selected.get("crons")
        script = selected.get("script_snapshot")
        service_snapshot = selected.get("service_snapshot")
        service_live = selected.get("service_live_state")
        if (
            not isinstance(files, Mapping)
            or not isinstance(crons, Mapping)
            or not isinstance(script, Mapping)
            or not isinstance(service_snapshot, Mapping)
            or not isinstance(service_live, Mapping)
            or not _file_matches_snapshot(script)
            or os.path.lexists(str(block["marker"]))
            or not _file_matches_snapshot(block["preimage"])
        ):
            return False
        for expected in files.values():
            if not isinstance(expected, Mapping) or not _file_matches_snapshot(expected):
                return False
        for user, expected in crons.items():
            if _capture_crontab(str(user)) != expected:
                return False
        return _service_state_matches(
            service_snapshot,
            use_postimage=True,
            expected_live=service_live,
        )
    except Exception:
        return False


def _fail_closed_notifier() -> None:
    from .utils import _fail_closed_service_bundle

    if _fail_closed_service_bundle((NOTIFIER_SERVICE,)) is not True:
        raise NotifierRecoveryRequired(
            "Notifier konnte nach mehrdeutigem Zustand nicht bootpersistent gesperrt werden"
        )


def _mark_outer_recovery_required(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: dict[str, object],
    errors: Sequence[str],
) -> None:
    combined = [*map(str, errors)]
    block_error: Exception | None = None
    transaction_block_confirmed = False
    if record.get("start_block") is not None:
        try:
            blocked_transaction = _transaction_with_persistent_state(
                journal,
                tx_dir,
                record,
                require_prepared_poststate=False,
            )
            _ensure_transaction_start_block(blocked_transaction)
            transaction_block_confirmed = True
        except Exception as exc:
            block_error = exc
            combined.append(f"transaction_bootblock:{type(exc).__name__}")
    if not transaction_block_confirmed:
        try:
            _ensure_recovery_start_block(journal, tx_dir, record)
        except Exception as exc:
            combined.append(f"recovery_bootblock:{type(exc).__name__}")
            try:
                _fail_closed_notifier()
            except Exception as fallback_exc:
                block_error = fallback_exc
                combined.append(f"bootblock:{type(fallback_exc).__name__}")
            else:
                block_error = None
        else:
            block_error = None
    journal.mark_recovery_required(tx_dir, record, combined)
    if block_error is not None:
        raise NotifierRecoveryRequired(
            "Notifier-Recovery ist mehrdeutig und der Bootblock nicht vollständig bestätigt"
        ) from block_error


def _transaction_for_persistent_rollback(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: dict[str, object],
) -> tuple[_NotifierTransaction | None, list[str]]:
    prestate = _decoded_prestate(journal, tx_dir, record)
    transaction = _transaction_with_persistent_state(
        journal,
        tx_dir,
        record,
        require_prepared_poststate=False,
    )
    errors: list[str] = []
    applied = journal.decode(tx_dir, record.get("applied_steps") or [])
    if not isinstance(applied, list):
        return None, ["applied_steps:invalid"]
    service_post: dict[str, object] | None = None
    service_live: Mapping[str, object] | None = None
    for raw in applied:
        if not isinstance(raw, Mapping):
            errors.append("applied_step:invalid")
            continue
        kind = str(raw.get("kind") or "")
        key = str(raw.get("key") or "")
        actual = raw.get("actual")
        if kind == "file" and isinstance(actual, Mapping):
            transaction.file_postimages[key] = actual
        elif kind == "cron" and (actual is None or isinstance(actual, bytes)):
            transaction.cron_postimages[key] = actual
        elif kind == "service" and isinstance(actual, Mapping):
            candidate = actual.get("service_snapshot")
            live = actual.get("service_live_state")
            if isinstance(candidate, Mapping) and isinstance(live, Mapping):
                service_post = dict(candidate)
                service_live = live
            else:
                errors.append("service_applied:invalid")
        else:
            errors.append(f"applied_step:{kind or 'unknown'}")

    poststate = _decoded_poststate(journal, tx_dir, record)
    if poststate is not None:
        files = poststate.get("files")
        crons = poststate.get("crons")
        candidate = poststate.get("service_snapshot")
        live = poststate.get("service_live_state")
        if isinstance(files, Mapping):
            for path, snapshot in files.items():
                if (
                    path in transaction.file_preimages
                    and isinstance(snapshot, Mapping)
                    and not snapshots_match(
                        snapshot,
                        transaction.file_preimages[path],
                        exact_metadata=True,
                    )
                ):
                    transaction.file_postimages[str(path)] = snapshot
        if isinstance(crons, Mapping):
            for user, value in crons.items():
                if user in transaction.cron_preimages and value != transaction.cron_preimages[user]:
                    transaction.cron_postimages[str(user)] = value
        if isinstance(candidate, Mapping) and isinstance(live, Mapping):
            service_post = dict(candidate)
            service_live = live

    mutation = journal.decode(tx_dir, record.get("mutation"))
    if isinstance(mutation, Mapping) and mutation.get("phase") == "intent":
        kind = str(mutation.get("kind") or "")
        key = str(mutation.get("key") or "")
        expected = mutation.get("expected")
        if kind == "file" and isinstance(expected, Mapping):
            try:
                current = snapshot_bound_file(key, allow_missing=True)
            except Exception:
                errors.append(f"file_intent:{key}:unreadable")
            else:
                preimage = transaction.file_preimages.get(key)
                if isinstance(preimage, Mapping) and _restored_preimage_matches_current(
                    current,
                    preimage,
                ):
                    pass
                elif _file_matches_intent(current, expected):
                    transaction.file_postimages[key] = current
                else:
                    errors.append(f"file_intent:{key}:ambiguous")
        elif kind == "cron" and (expected is None or isinstance(expected, bytes)):
            try:
                current_cron = _capture_crontab(key)
            except Exception:
                errors.append(f"cron_intent:{key}:unreadable")
            else:
                if current_cron == transaction.cron_preimages.get(key):
                    pass
                elif current_cron == expected:
                    transaction.cron_postimages[key] = expected
                else:
                    errors.append(f"cron_intent:{key}:ambiguous")
        elif kind == "service":
            if not isinstance(expected, Mapping) or not isinstance(
                expected.get("unit_payload"),
                bytes,
            ):
                errors.append("service_intent:invalid")
            elif _service_files_match_restored_preimage(
                transaction.service_original_snapshot or {}
            ):
                pass
            else:
                try:
                    current_unit = snapshot_bound_file(
                        NOTIFIER_UNIT_PATH,
                        allow_missing=True,
                        expected_uid=0,
                        expected_gid=0,
                        max_bytes=256 * 1024,
                    )
                except Exception:
                    errors.append("service_intent:unreadable")
                else:
                    intended = {
                        "payload": expected["unit_payload"],
                        "uid": int(expected.get("uid", -1)),
                        "gid": int(expected.get("gid", -1)),
                        "mode": int(expected.get("mode", -1)),
                    }
                    if not _file_matches_intent(current_unit, intended):
                        errors.append("service_intent:ambiguous")
                    else:
                        try:
                            from .utils import record_systemd_service_bundle_postimage

                            candidate = _blocked_service_snapshot(transaction)
                            record_systemd_service_bundle_postimage(
                                candidate,
                                (NOTIFIER_SERVICE,),
                                expected_bytes={
                                    NOTIFIER_UNIT: expected["unit_payload"],
                                },
                            )
                            service_post = candidate
                            service_live = _capture_service_live_state(candidate)
                        except Exception:
                            errors.append("service_intent:unbound")
        else:
            errors.append("mutation_intent:invalid")

    for path, preimage in transaction.file_preimages.items():
        if path == LEGACY_NOTIFY_PATH:
            transaction.file_postimages.pop(path, None)
            continue
        try:
            current = snapshot_bound_file(path, allow_missing=True)
        except Exception:
            errors.append(f"file:{path}:unreadable")
            continue
        if _restored_preimage_matches_current(current, preimage):
            transaction.file_postimages.pop(path, None)
            continue
        expected = transaction.file_postimages.get(path)
        if not isinstance(expected, Mapping) or not snapshots_match(
            current,
            expected,
            exact_metadata=True,
        ):
            errors.append(f"file:{path}:ambiguous")

    for user, preimage in transaction.cron_preimages.items():
        try:
            current = _capture_crontab(user)
        except Exception:
            errors.append(f"cron:{user}:unreadable")
            continue
        if current == preimage:
            transaction.cron_postimages.pop(user, None)
            continue
        if user not in transaction.cron_postimages or current != transaction.cron_postimages[user]:
            errors.append(f"cron:{user}:ambiguous")

    original_service = transaction.service_original_snapshot or {}
    if service_post is not None and (
        _service_state_matches(
            service_post,
            use_postimage=True,
            expected_live=service_live,
        )
        or _service_files_match_exact_postimage(service_post)
    ):
        if transaction.start_block_path and isinstance(
            transaction.start_block_postimage,
            Mapping,
        ):
            unit_snapshot = service_post.get(NOTIFIER_UNIT)
            if not isinstance(unit_snapshot, dict):
                errors.append("service_post:invalid")
            else:
                for dropin_key in ("pre_dropins", "post_dropins"):
                    dropins = unit_snapshot.get(dropin_key)
                    if not isinstance(dropins, Mapping) or (
                        transaction.start_block_path not in dropins
                    ):
                        errors.append(f"service_post:{dropin_key}:missing_block")
                        continue
                    rebound_dropins = dict(dropins)
                    rebound_dropins[transaction.start_block_path] = (
                        transaction.start_block_postimage
                    )
                    unit_snapshot[dropin_key] = rebound_dropins
        transaction.service_snapshot = service_post
        transaction.service_touched = True
    elif _service_files_match_restored_preimage(original_service):
        if record.get("decision") == "rollback":
            transaction.service_snapshot = copy.deepcopy(original_service)
            transaction.service_touched = False
        elif transaction.start_block_path:
            try:
                transaction.service_snapshot = _blocked_service_snapshot(transaction)
                transaction.service_touched = True
            except Exception:
                errors.append("service_blocked_prestate:ambiguous")
        elif _service_state_matches(original_service, use_postimage=False):
            transaction.service_snapshot = copy.deepcopy(original_service)
            transaction.service_touched = False
        else:
            transaction.service_snapshot = _service_snapshot_for_resumed_prestate_rollback(
                original_service
            )
            transaction.service_touched = True
    else:
        errors.append("service:ambiguous")
    return (transaction if not errors else None), errors


def _recover_outer_record_locked(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: dict[str, object],
    child_status: Mapping[str, object] | None,
) -> None:
    if record.get("recovery_start_block") is not None and record.get(
        "state"
    ) != "recovery_required":
        _mark_outer_recovery_required(
            journal,
            tx_dir,
            record,
            ("sticky_recovery_block_intent",),
        )
        raise NotifierRecoveryRequired(
            f"Notifier-Außentransaktion {record.get('transaction_id')} benötigt Recovery"
        )
    if record.get("state") == "recovery_required":
        previous_errors = record.get("recovery_errors")
        sticky_errors = (
            tuple(map(str, previous_errors))
            if isinstance(previous_errors, (list, tuple)) and previous_errors
            else ("sticky_recovery_required",)
        )
        _mark_outer_recovery_required(
            journal,
            tx_dir,
            record,
            sticky_errors,
        )
        raise NotifierRecoveryRequired(
            f"Notifier-Außentransaktion {record.get('transaction_id')} benötigt Recovery"
        )

    decision = record.get("decision")
    if decision not in {None, "commit", "rollback"}:
        _mark_outer_recovery_required(
            journal,
            tx_dir,
            record,
            ("outer_decision:invalid",),
        )
        raise NotifierRecoveryRequired("Notifier-Außenentscheidung ist ungültig")

    if decision == "commit":
        if record.get("stage") != "outer_commit_durable":
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                ("outer_commit:stage_mismatch",),
            )
            raise NotifierRecoveryRequired("Notifier-Commitphase ist mehrdeutig")
        if bool(record.get("watchdog_required")) and (
            child_status is None
            or child_status.get("status") != "committed"
            or int(child_status.get("match_count", -1)) != 1
        ):
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                ("outer_commit:watchdog_not_committed",),
            )
            raise NotifierRecoveryRequired("Watchdog-Commit ist nicht mehr eindeutig")
        try:
            transaction = _transaction_with_persistent_state(
                journal,
                tx_dir,
                record,
                require_prepared_poststate=True,
            )
        except Exception as exc:
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                (f"outer_commit_reconstruct:{type(exc).__name__}",),
            )
            raise NotifierRecoveryRequired("Notifier-Commit ist nicht rekonstruierbar") from exc
        if not _prepared_payloads_match(journal, tx_dir, record):
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                ("outer_commit:notifier_postimage_drift",),
            )
            raise NotifierRecoveryRequired("Notifier-Commitpostimages drifteten")
        try:
            _finalize_outer_commit(transaction, recovered=True)
        except Exception as exc:
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                (f"outer_commit_finalize:{type(exc).__name__}",),
            )
            raise NotifierRecoveryRequired("Notifier-Commit konnte nicht fortgesetzt werden") from exc
        return

    if decision == "rollback":
        if record.get("stage") != "outer_rollback_durable":
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                ("outer_rollback:stage_mismatch",),
            )
            raise NotifierRecoveryRequired("Notifier-Rollbackphase ist mehrdeutig")
        try:
            transaction, errors = _transaction_for_persistent_rollback(
                journal,
                tx_dir,
                record,
            )
        except Exception as exc:
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                (f"outer_rollback_reconstruct:{type(exc).__name__}",),
            )
            raise NotifierRecoveryRequired("Notifier-Rollback ist nicht rekonstruierbar") from exc
        if transaction is None or not _complete_durable_outer_rollback(transaction):
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                errors or ("outer_rollback:unconfirmed",),
            )
            raise NotifierRecoveryRequired("Notifier-Rollback blieb unbestätigt")
        record["state"] = "rolled_back"
        record["recovered"] = True
        if child_status is not None:
            record["watchdog_status"] = dict(child_status)
        journal.advance(
            tx_dir,
            record,
            stage="rolled_back",
            phase="rollback_complete",
        )
        return

    if child_status is not None:
        status = str(child_status.get("status") or "")
        match_count = int(child_status.get("match_count", -1))
        if status in {"ambiguous", "recovery_required", "drifted"}:
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                (f"watchdog_correlation:{status}",),
            )
            raise NotifierRecoveryRequired(
                "Watchdog-Korrelation ist mehrdeutig oder gedriftet"
            )
        if not bool(record.get("watchdog_intent_durable")) and match_count != 0:
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                ("watchdog_child_without_durable_intent",),
            )
            raise NotifierRecoveryRequired(
                "Watchdog-Kind existiert ohne dauerhafte Kindabsicht"
            )
        if status == "committed" and record.get("stage") == "watchdog_intent":
            if match_count != 1:
                _mark_outer_recovery_required(
                    journal,
                    tx_dir,
                    record,
                    ("watchdog_commit:correlation_not_unique",),
                )
                raise NotifierRecoveryRequired("Watchdog-Korrelation ist nicht eindeutig")
            if _notifier_state_matches(journal, tx_dir, record, post=True):
                transaction = _transaction_with_persistent_state(
                    journal,
                    tx_dir,
                    record,
                    require_prepared_poststate=True,
                )
                _persist_outer_commit_decision(
                    transaction,
                    child_status=child_status,
                )
                try:
                    _finalize_outer_commit(transaction, recovered=True)
                except Exception as exc:
                    _mark_outer_recovery_required(
                        journal,
                        tx_dir,
                        record,
                        (f"recovered_commit_finalize:{type(exc).__name__}",),
                    )
                    raise NotifierRecoveryRequired(
                        "Notifier-Commitfreigabe blieb unvollständig"
                    ) from exc
                return

    if record.get("start_block") is not None:
        try:
            blocked_transaction = _transaction_with_persistent_state(
                journal,
                tx_dir,
                record,
                require_prepared_poststate=False,
            )
            _ensure_transaction_start_block(blocked_transaction)
        except Exception as exc:
            _mark_outer_recovery_required(
                journal,
                tx_dir,
                record,
                (f"rollback_start_block:{type(exc).__name__}",),
            )
            raise NotifierRecoveryRequired("Notifier-Startblock ist nicht eindeutig") from exc

    try:
        transaction, errors = _transaction_for_persistent_rollback(
            journal,
            tx_dir,
            record,
        )
    except Exception as exc:
        _mark_outer_recovery_required(
            journal,
            tx_dir,
            record,
            (f"rollback_reconstruct:{type(exc).__name__}",),
        )
        raise NotifierRecoveryRequired("Notifier-Zustand ist nicht rekonstruierbar") from exc
    if transaction is None:
        _mark_outer_recovery_required(journal, tx_dir, record, errors)
        raise NotifierRecoveryRequired("Notifier-Zustand ist mehrdeutig")
    journal.advance(
        tx_dir,
        record,
        stage="rollback_in_progress",
        phase="rollback_intent",
    )
    if not _rollback_transaction(transaction) or not _notifier_state_matches(
        journal,
        tx_dir,
        record,
        post=False,
    ):
        _mark_outer_recovery_required(
            journal,
            tx_dir,
            record,
            ("notifier_rollback:unconfirmed",),
        )
        raise NotifierRecoveryRequired("Notifier-Rollback blieb unbestätigt")
    record["state"] = "rolled_back"
    record["recovered"] = True
    if child_status is not None:
        record["watchdog_status"] = dict(child_status)
    journal.advance(
        tx_dir,
        record,
        stage="rolled_back",
        phase="rollback_complete",
    )


def _recover_outer_transactions_locked() -> None:
    journal = _NotifierWatchdogJournal()
    try:
        pending = journal.pending()
    except Exception:
        _fail_closed_notifier()
        raise
    for tx_dir, record in pending:
        if bool(record.get("watchdog_required")):
            correlation = str(record.get("child_correlation_id") or "")
            if not re.fullmatch(r"[0-9a-f]{32}", correlation):
                _mark_outer_recovery_required(
                    journal,
                    tx_dir,
                    record,
                    ("child_correlation:invalid",),
                )
                raise NotifierRecoveryRequired("Watchdog-Korrelation ist ungültig")
            try:
                from .install_watchdog import watchdog_correlation_guard

                with watchdog_correlation_guard(correlation) as child_status:
                    _recover_outer_record_locked(
                        journal,
                        tx_dir,
                        record,
                        child_status,
                    )
            except NotifierRecoveryRequired:
                raise
            except Exception as exc:
                _mark_outer_recovery_required(
                    journal,
                    tx_dir,
                    record,
                    (f"watchdog_guard:{type(exc).__name__}",),
                )
                raise NotifierRecoveryRequired(
                    "Korrelierter Watchdog-Zustand ist nicht eindeutig"
                ) from exc
        else:
            _recover_outer_record_locked(journal, tx_dir, record, None)
    journal.clear_recovery_status_if_safe()


def _ensure_root_controlled_directory(path: str, *, mode: int = 0o755) -> None:
    target = Path(path)
    if not target.is_absolute() or target == Path("/"):
        raise NotifierRecoveryRequired("Notifier-systemd-Pfad ist nicht absolut")
    flags = _NotifierWatchdogJournal._directory_flags()
    descriptor = os.open("/", flags)
    try:
        components = target.parts[1:]
        for index, component in enumerate(components):
            final = index == len(components) - 1
            created = False
            try:
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                os.mkdir(component, mode if final else 0o755, dir_fd=descriptor)
                os.fsync(descriptor)
                next_descriptor = os.open(component, flags, dir_fd=descriptor)
                created = True
            metadata = os.fstat(next_descriptor)
            if (
                not stat.S_ISDIR(metadata.st_mode)
                or metadata.st_uid != 0
                or stat.S_IMODE(metadata.st_mode) & 0o022
            ):
                os.close(next_descriptor)
                raise NotifierRecoveryRequired(
                    f"Notifier-systemd-Elternpfad ist nicht root-kontrolliert: {target}"
                )
            expected_mode = mode if final else 0o755
            if created:
                os.fchown(next_descriptor, 0, 0)
                os.fchmod(next_descriptor, expected_mode)
                os.fsync(next_descriptor)
                metadata = os.fstat(next_descriptor)
            if final and (
                metadata.st_gid != 0
                or stat.S_IMODE(metadata.st_mode) != expected_mode
            ):
                os.close(next_descriptor)
                raise NotifierRecoveryRequired(
                    f"Notifier-systemd-Zielverzeichnis ist nicht root:root "
                    f"{expected_mode:04o}: {target}"
                )
            if created:
                os.fsync(descriptor)
            os.close(descriptor)
            descriptor = next_descriptor
    finally:
        os.close(descriptor)


def _start_block_is_active(transaction: _NotifierTransaction) -> bool:
    from .utils import _systemd_show_contract

    if (
        not transaction.start_block_path
        or not isinstance(transaction.start_block_postimage, Mapping)
        or os.path.lexists(transaction.start_block_marker)
    ):
        return False
    if not _file_matches_snapshot(transaction.start_block_postimage):
        return False
    try:
        state = _systemd_show_contract(NOTIFIER_UNIT)
    except Exception:
        return False
    if state.get("active_state") not in {"", "inactive", "failed"}:
        return False
    if state.get("load_state") == "loaded":
        return transaction.start_block_path in set(state.get("dropin_paths") or ())
    return bool(
        state.get("load_state") == "not-found"
        and not os.path.lexists(NOTIFIER_UNIT_PATH)
    )


def _remove_legacy_notifier_staging_residue() -> bool:
    """Entfernt nur das exakt leere, eigene Alt-Staging im Drop-in-Verzeichnis."""

    from .utils import _descriptor_has_unsafe_unit_xattrs

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise NotifierRecoveryRequired(
            "Notifier-Staging-Migration darf ausschließlich Root ausführen"
        )
    try:
        parent_descriptor, parent_identity = open_bound_directory(
            NOTIFIER_DROPIN_DIR
        )
    except FileNotFoundError:
        return False
    staging_descriptor = -1
    try:
        parent_metadata = os.fstat(parent_descriptor)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_gid != 0
            or stat.S_IMODE(parent_metadata.st_mode) != 0o755
            or _descriptor_has_unsafe_unit_xattrs(parent_descriptor)
        ):
            raise NotifierRecoveryRequired(
                "Notifier-Drop-in-Verzeichnis ist für die Staging-Migration unsicher"
            )
        try:
            named_before = os.stat(
                LEGACY_TRANSACTION_STAGING_NAME,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return False
        signature = (
            named_before.st_dev,
            named_before.st_ino,
            named_before.st_uid,
            named_before.st_gid,
            stat.S_IMODE(named_before.st_mode),
        )
        if (
            not stat.S_ISDIR(named_before.st_mode)
            or named_before.st_uid != 0
            or named_before.st_gid != 0
            or stat.S_IMODE(named_before.st_mode) != 0o700
        ):
            raise NotifierRecoveryRequired(
                "Notifier-Alt-Staging besitzt keine sichere Verzeichnisform"
            )
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory:
            raise NotifierRecoveryRequired(
                "Notifier-Staging-Migration benötigt O_NOFOLLOW/O_DIRECTORY"
            )
        staging_descriptor = os.open(
            LEGACY_TRANSACTION_STAGING_NAME,
            os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0),
            dir_fd=parent_descriptor,
        )
        opened = os.fstat(staging_descriptor)
        named_after = os.stat(
            LEGACY_TRANSACTION_STAGING_NAME,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        opened_signature = (
            opened.st_dev,
            opened.st_ino,
            opened.st_uid,
            opened.st_gid,
            stat.S_IMODE(opened.st_mode),
        )
        named_after_signature = (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_uid,
            named_after.st_gid,
            stat.S_IMODE(named_after.st_mode),
        )
        if (
            opened_signature != signature
            or named_after_signature != signature
            or _descriptor_has_unsafe_unit_xattrs(staging_descriptor)
            or os.listdir(staging_descriptor)
        ):
            raise NotifierRecoveryRequired(
                "Notifier-Alt-Staging ist nicht exakt leer und stabil gebunden"
            )
        named_final = os.stat(
            LEGACY_TRANSACTION_STAGING_NAME,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            (
                named_final.st_dev,
                named_final.st_ino,
                named_final.st_uid,
                named_final.st_gid,
                stat.S_IMODE(named_final.st_mode),
            )
            != signature
            or os.listdir(staging_descriptor)
        ):
            raise NotifierRecoveryRequired(
                "Notifier-Alt-Staging driftete unmittelbar vor der Entfernung"
            )
        os.rmdir(
            LEGACY_TRANSACTION_STAGING_NAME,
            dir_fd=parent_descriptor,
        )
        os.fsync(parent_descriptor)
        try:
            os.stat(
                LEGACY_TRANSACTION_STAGING_NAME,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            pass
        else:
            raise NotifierRecoveryRequired(
                "Notifier-Alt-Staging blieb nach rmdir vorhanden"
            )
        parent_after = os.fstat(parent_descriptor)
        if (parent_after.st_dev, parent_after.st_ino) != tuple(parent_identity[:2]):
            raise NotifierRecoveryRequired(
                "Notifier-Drop-in-Verzeichnis driftete nach der Staging-Migration"
            )
    finally:
        if staging_descriptor >= 0:
            os.close(staging_descriptor)
        os.close(parent_descriptor)
    rebound_descriptor, rebound_identity = open_bound_directory(NOTIFIER_DROPIN_DIR)
    try:
        if tuple(rebound_identity[:2]) != tuple(parent_identity[:2]):
            raise NotifierRecoveryRequired(
                "Notifier-Drop-in-Pfad wurde nach der Staging-Migration ausgetauscht"
            )
    finally:
        os.close(rebound_descriptor)
    return True


def _ensure_recovery_start_block(
    journal: _NotifierWatchdogJournal,
    tx_dir: Path,
    record: dict[str, object],
) -> None:
    """Versiegelt einen zweiten, tx-spezifischen Block bei Journalmehrdeutigkeit."""

    from .utils import _systemd_show_contract, run_command

    transaction_id = str(record.get("transaction_id") or "")
    if not re.fullmatch(r"[0-9]+-[0-9a-f]{16}", transaction_id):
        raise NotifierRecoveryRequired("Notifier-Recoveryblock besitzt keine Transaktions-ID")
    _ensure_root_controlled_directory(NOTIFIER_DROPIN_DIR, mode=0o755)
    path = os.path.join(
        NOTIFIER_DROPIN_DIR,
        f"99-e3dc-outer-recovery-{transaction_id}.conf",
    )
    marker = str(tx_dir / "notifier-recovery-start-approved")
    payload = (
        "[Unit]\n"
        f"ConditionPathExists={marker}\n"
    ).encode("utf-8")
    raw = record.get("recovery_start_block")
    if raw is None:
        if os.path.lexists(marker):
            raise NotifierRecoveryRequired("Notifier-Recoveryfreigabe existiert unerwartet")
        preimage = snapshot_bound_file(
            path,
            allow_missing=True,
            expected_uid=0,
            expected_gid=0,
            max_bytes=64 * 1024,
        )
        if preimage.get("exists"):
            raise NotifierRecoveryRequired("Notifier-Recoveryblock existiert ungebunden")
        record["recovery_start_block"] = {
            "path": path,
            "marker": marker,
            "payload": journal.encode(tx_dir, payload, label="recovery-block-payload"),
            "preimage": journal.encode(tx_dir, preimage, label="recovery-block-preimage"),
            "postimage": None,
        }
        journal.write_record(tx_dir, record)
        raw = record["recovery_start_block"]
    decoded = journal.decode(tx_dir, raw)
    if (
        not isinstance(decoded, Mapping)
        or decoded.get("path") != path
        or decoded.get("marker") != marker
        or decoded.get("payload") != payload
        or not isinstance(decoded.get("preimage"), Mapping)
        or decoded["preimage"].get("path") != path
        or bool(decoded["preimage"].get("exists"))
        or os.path.lexists(marker)
    ):
        raise _NotifierAmbiguousState("Notifier-Recoveryblockjournal ist ungültig")
    current = snapshot_bound_file(
        path,
        allow_missing=True,
        expected_uid=0,
        expected_gid=0,
        max_bytes=64 * 1024,
    )
    postimage = decoded.get("postimage")
    if isinstance(postimage, Mapping) and snapshots_match(
        current,
        postimage,
        exact_metadata=True,
    ):
        rebound = postimage
    elif _file_matches_intent(
        current,
        {"payload": payload, "uid": 0, "gid": 0, "mode": 0o644},
    ):
        rebound = current
    elif snapshots_match(current, decoded["preimage"], exact_metadata=True):
        rebound = atomic_write_bound_file(
            path,
            payload,
            uid=0,
            gid=0,
            mode=0o644,
            expected_snapshot=current,
            max_existing_bytes=64 * 1024,
            staging_root=SYSTEMD_TRANSACTION_STAGING_ROOT,
        )
    else:
        raise _NotifierAmbiguousState("Notifier-Recoveryblock besitzt fremde Bytes")
    if not isinstance(postimage, Mapping) or not snapshots_match(
        rebound,
        postimage,
        exact_metadata=True,
    ):
        raw_record = record.get("recovery_start_block")
        if not isinstance(raw_record, dict):
            raise NotifierRecoveryRequired("Notifier-Recoveryblockjournal fehlt")
        raw_record["postimage"] = journal.encode(
            tx_dir,
            rebound,
            label="recovery-block-postimage",
        )
        journal.write_record(tx_dir, record)
    if not run_command("systemctl daemon-reload", timeout=30).get("success"):
        raise NotifierRecoveryRequired("Notifier-Recoveryblock wurde nicht neu geladen")
    stopped = run_command("systemctl stop e3dc-notifier.service", timeout=30)
    if not stopped.get("success"):
        state = _systemd_show_contract(NOTIFIER_UNIT)
        if state.get("active_state") not in {"", "inactive", "failed"}:
            raise NotifierRecoveryRequired("Notifier blieb unter Recoveryblock aktiv")
    state = _systemd_show_contract(NOTIFIER_UNIT)
    if (
        state.get("active_state") not in {"", "inactive", "failed"}
        or os.path.lexists(marker)
        or not _file_matches_snapshot(rebound)
        or (
            state.get("load_state") == "loaded"
            and path not in set(state.get("dropin_paths") or ())
        )
        or (
            state.get("load_state") == "not-found"
            and os.path.lexists(NOTIFIER_UNIT_PATH)
        )
    ):
        raise NotifierRecoveryRequired("Notifier-Recoveryblock ist nicht wirksam")


def _ensure_transaction_start_block(transaction: _NotifierTransaction) -> None:
    """Stellt den journalgebundenen Bootblock her, ohne fremde Bytes zu ersetzen."""

    from .utils import _systemd_show_contract, run_command

    journal = transaction.outer_journal
    tx_dir = transaction.outer_tx_dir
    record = transaction.outer_record
    if journal is None or tx_dir is None or record is None:
        raise NotifierRecoveryRequired("Notifier-Startblock ist nicht rekonstruierbar")
    block = _validated_start_block(
        journal,
        tx_dir,
        record,
        require_postimage=False,
    )
    transaction.start_block_path = str(block["path"])
    transaction.start_block_marker = str(block["marker"])
    transaction.start_block_preimage = block["preimage"]
    if isinstance(block.get("postimage"), Mapping):
        transaction.start_block_postimage = block["postimage"]
    payload = block["payload"]
    current = snapshot_bound_file(
        transaction.start_block_path,
        allow_missing=True,
        expected_uid=0,
        expected_gid=0,
        max_bytes=64 * 1024,
    )
    rebound: Mapping[str, object] | None = None
    if isinstance(transaction.start_block_postimage, Mapping) and snapshots_match(
        current,
        transaction.start_block_postimage,
        exact_metadata=True,
    ):
        rebound = transaction.start_block_postimage
    elif _file_matches_intent(
        current,
        {"payload": payload, "uid": 0, "gid": 0, "mode": 0o644},
    ):
        rebound = current
    elif snapshots_match(
        current,
        transaction.start_block_preimage,
        exact_metadata=True,
    ):
        rebound = atomic_write_bound_file(
            transaction.start_block_path,
            payload,
            uid=0,
            gid=0,
            mode=0o644,
            expected_snapshot=current,
            max_existing_bytes=64 * 1024,
            staging_root=SYSTEMD_TRANSACTION_STAGING_ROOT,
        )
    else:
        raise _NotifierAmbiguousState("Notifier-Startblock besitzt fremde Bytes")

    if not isinstance(rebound, Mapping):
        raise NotifierRecoveryRequired("Notifier-Startblock konnte nicht gebunden werden")
    if not isinstance(transaction.start_block_postimage, Mapping) or not snapshots_match(
        rebound,
        transaction.start_block_postimage,
        exact_metadata=True,
    ):
        transaction.start_block_postimage = rebound
        raw_block = record.get("start_block")
        if not isinstance(raw_block, dict):
            raise NotifierRecoveryRequired("Notifier-Startblockjournal fehlt")
        raw_block["postimage"] = journal.encode(
            tx_dir,
            rebound,
            label="start-block-rebound-postimage",
        )
        journal.write_record(tx_dir, record)

    if not run_command("systemctl daemon-reload", timeout=30).get("success"):
        raise NotifierRecoveryRequired("Notifier-Startblock konnte nicht neu geladen werden")
    stopped = run_command("systemctl stop e3dc-notifier.service", timeout=30)
    if not stopped.get("success"):
        state = _systemd_show_contract(NOTIFIER_UNIT)
        if state.get("active_state") not in {"", "inactive", "failed"}:
            raise NotifierRecoveryRequired("Notifier konnte unter Startblock nicht stoppen")
    if not _start_block_is_active(transaction):
        raise NotifierRecoveryRequired("Notifier-Startblock ist nicht wirksam")


def _release_transaction_start_block(transaction: _NotifierTransaction) -> None:
    """Entfernt ausschließlich das exakt gebundene Transaktions-Drop-in."""

    from .utils import run_command

    if (
        not transaction.start_block_path
        or not isinstance(transaction.start_block_preimage, Mapping)
        or not isinstance(transaction.start_block_postimage, Mapping)
        or os.path.lexists(transaction.start_block_marker)
    ):
        raise _NotifierAmbiguousState("Notifier-Startblock kann nicht freigegeben werden")
    current = snapshot_bound_file(
        transaction.start_block_path,
        allow_missing=True,
        expected_uid=0,
        expected_gid=0,
        max_bytes=64 * 1024,
    )
    if snapshots_match(
        current,
        transaction.start_block_postimage,
        exact_metadata=True,
    ):
        restore_bound_file(
            transaction.start_block_preimage,
            expected_current=transaction.start_block_postimage,
            max_bytes=64 * 1024,
        )
    elif not snapshots_match(
        current,
        transaction.start_block_preimage,
        exact_metadata=True,
    ):
        raise _NotifierAmbiguousState("Notifier-Startblock driftete vor der Freigabe")
    if not run_command("systemctl daemon-reload", timeout=30).get("success"):
        raise NotifierRecoveryRequired("Notifier-Startblock-Freigabe wurde nicht neu geladen")
    if (
        os.path.lexists(transaction.start_block_marker)
        or not _file_matches_snapshot(transaction.start_block_preimage)
    ):
        raise NotifierRecoveryRequired("Notifier-Startblock-Freigabe blieb unbestätigt")


def _install_notifier_start_block(transaction: _NotifierTransaction) -> None:
    from .utils import _systemd_show_contract, run_command

    journal = transaction.outer_journal
    tx_dir = transaction.outer_tx_dir
    record = transaction.outer_record
    if journal is None or tx_dir is None or record is None:
        raise NotifierRecoveryRequired("Notifier-Startblock besitzt kein Außenjournal")
    transaction_id = str(record.get("transaction_id") or "")
    if not re.fullmatch(r"[0-9]+-[0-9a-f]{16}", transaction_id):
        raise NotifierRecoveryRequired("Notifier-Startblock besitzt keine Transaktions-ID")
    _ensure_root_controlled_directory(NOTIFIER_DROPIN_DIR, mode=0o755)
    block_path = os.path.join(
        NOTIFIER_DROPIN_DIR,
        f"90-e3dc-outer-{transaction_id}.conf",
    )
    marker = str(tx_dir / "notifier-start-approved")
    if os.path.lexists(marker):
        raise NotifierRecoveryRequired("Notifier-Startfreigabe existiert unerwartet")
    preimage = snapshot_bound_file(
        block_path,
        allow_missing=True,
        expected_uid=0,
        expected_gid=0,
        max_bytes=64 * 1024,
    )
    if preimage.get("exists"):
        raise NotifierRecoveryRequired("Transaktionsspezifischer Startblock existiert bereits")
    payload = (
        "[Unit]\n"
        f"ConditionPathExists={marker}\n"
    ).encode("utf-8")
    record["start_block"] = {
        "path": block_path,
        "marker": marker,
        "payload": journal.encode(tx_dir, payload, label="start-block-payload"),
        "preimage": journal.encode(tx_dir, preimage, label="start-block-preimage"),
        "postimage": None,
    }
    journal.advance(
        tx_dir,
        record,
        stage="start_block_intent",
        phase="start_block_intent",
    )
    postimage = atomic_write_bound_file(
        block_path,
        payload,
        uid=0,
        gid=0,
        mode=0o644,
        expected_snapshot=preimage,
        max_existing_bytes=64 * 1024,
        staging_root=SYSTEMD_TRANSACTION_STAGING_ROOT,
    )
    transaction.start_block_path = block_path
    transaction.start_block_marker = marker
    transaction.start_block_preimage = preimage
    transaction.start_block_postimage = postimage
    record["start_block"]["postimage"] = journal.encode(
        tx_dir,
        postimage,
        label="start-block-postimage",
    )
    journal.advance(
        tx_dir,
        record,
        stage="start_block_written",
        phase="start_block_written",
    )
    if not run_command("systemctl daemon-reload", timeout=30).get("success"):
        raise NotifierRecoveryRequired("Notifier-Startblock wurde nicht neu geladen")
    stopped = run_command("systemctl stop e3dc-notifier.service", timeout=30)
    if not stopped.get("success"):
        state = _systemd_show_contract(NOTIFIER_UNIT)
        if state.get("active_state") not in {"", "inactive", "failed"}:
            raise NotifierRecoveryRequired("Notifier konnte vor der Transaktion nicht stoppen")
    if not _start_block_is_active(transaction):
        raise NotifierRecoveryRequired("Notifier-Startblock ist nicht wirksam versiegelt")
    journal.advance(
        tx_dir,
        record,
        stage="start_block_active",
        phase="start_block_active",
    )


def _blocked_service_snapshot(transaction: _NotifierTransaction) -> dict[str, object]:
    from .utils import _read_bound_unit_preimage, _systemd_show_contract

    original = transaction.service_original_snapshot or transaction.service_snapshot
    snapshot = copy.deepcopy(original)
    unit = snapshot.get(NOTIFIER_UNIT)
    if not isinstance(unit, dict) or not isinstance(
        transaction.start_block_postimage,
        Mapping,
    ):
        raise NotifierRecoveryRequired("Notifier-Service-Vorzustand ist nicht gebunden")
    if not _file_matches_snapshot(transaction.start_block_postimage):
        raise NotifierRecoveryRequired("Notifier-Startblock driftete vor dem Dienstsnapshot")
    bound_start_block = _read_bound_unit_preimage(transaction.start_block_path)
    if bound_start_block is None:
        raise NotifierRecoveryRequired("Notifier-Startblock fehlt im Dienstsnapshot")
    pre_dropins = dict(unit.get("pre_dropins") or {})
    pre_dropins[transaction.start_block_path] = bound_start_block
    unit["pre_dropins"] = pre_dropins
    state = _systemd_show_contract(NOTIFIER_UNIT)
    for key, value in state.items():
        unit[key] = value
    return snapshot


def _render_notifier_unit(transaction: _NotifierTransaction) -> bytes:
    from .utils import require_bound_venv_runtime, resolve_venv_target

    _venv_name, venv_path = resolve_venv_target(transaction.install_user)
    runtime = require_bound_venv_runtime(
        install_user=transaction.install_user,
        venv_path=venv_path,
    )
    script_path = str(transaction.script_snapshot["path"])
    working_dir = os.path.dirname(script_path)
    exec_start = " ".join(shlex.quote(item) for item in (runtime["python"], script_path))
    return f"""[Unit]
Description=E3DC Notification Manager
After=network.target

[Service]
Type=simple
User={transaction.install_user}
Group=www-data
WorkingDirectory={working_dir}
ExecStart={exec_start}
Restart=always
RestartSec=10

StandardOutput=journal
StandardError=journal


[Install]
WantedBy=multi-user.target
""".encode("utf-8")


def _install_notifier_unit_while_blocked(
    transaction: _NotifierTransaction,
    *,
    start_service: bool,
) -> None:
    from .utils import (
        activate_systemd_service_bundle,
        record_systemd_service_bundle_postimage,
    )

    if not _start_block_is_active(transaction):
        raise NotifierRecoveryRequired("Notifier-Startblock driftete vor der Unit-Mutation")
    service_snapshot = _blocked_service_snapshot(transaction)
    transaction.service_snapshot = service_snapshot
    transaction.service_touched = True
    payload = _render_notifier_unit(transaction)
    secure_preimage = snapshot_bound_file(
        NOTIFIER_UNIT_PATH,
        allow_missing=True,
        expected_uid=0,
        expected_gid=0,
        max_bytes=256 * 1024,
    )
    original = (transaction.service_original_snapshot or {})[NOTIFIER_UNIT]
    original_file = original.get("preimage") if isinstance(original, Mapping) else None
    if bool(secure_preimage.get("exists")) != bool(original_file is not None):
        raise NotifierRecoveryRequired("Notifier-Unit driftete seit dem Vorzustand")
    if original_file is not None and (
        secure_preimage.get("payload") != original_file.get("bytes")
        or tuple(secure_preimage.get("identity") or ())[:2]
        != (original_file.get("dev"), original_file.get("ino"))
    ):
        raise NotifierRecoveryRequired("Notifier-Unit-Inode driftete seit dem Vorzustand")
    _outer_intent(
        transaction,
        kind="service",
        key=NOTIFIER_SERVICE,
        expected={
            "unit_payload": payload,
            "uid": 0,
            "gid": 0,
            "mode": 0o644,
            "start_service_after_commit": bool(start_service),
        },
    )
    atomic_write_bound_file(
        NOTIFIER_UNIT_PATH,
        payload,
        uid=0,
        gid=0,
        mode=0o644,
        expected_snapshot=secure_preimage,
        max_existing_bytes=256 * 1024,
    )
    record_systemd_service_bundle_postimage(
        service_snapshot,
        (NOTIFIER_SERVICE,),
        expected_bytes={NOTIFIER_UNIT: payload},
    )
    if not activate_systemd_service_bundle(
        service_snapshot,
        enabled_units=(NOTIFIER_SERVICE,),
        start_order=(),
        start_services=False,
    ):
        raise NotifierRecoveryRequired("Notifier-Unit konnte unter Startblock nicht vorbereiten")
    live_state = _capture_service_live_state(service_snapshot)
    if live_state[NOTIFIER_UNIT].get("active_state") not in {"inactive", "failed"}:
        raise NotifierRecoveryRequired("Notifier lief trotz Startblock")
    if not _start_block_is_active(transaction):
        raise NotifierRecoveryRequired("Notifier-Startblock driftete nach der Unit-Mutation")
    _outer_applied(
        transaction,
        actual={
            "service_snapshot": service_snapshot,
            "service_live_state": live_state,
        },
    )


def _service_snapshot_without_start_block(
    transaction: _NotifierTransaction,
) -> dict[str, object]:
    snapshot = copy.deepcopy(transaction.service_snapshot)
    unit = snapshot.get(NOTIFIER_UNIT)
    if not isinstance(unit, dict):
        raise NotifierRecoveryRequired("Notifier-Servicepostimage fehlt")
    post_dropins = unit.get("post_dropins")
    if not isinstance(post_dropins, Mapping):
        raise NotifierRecoveryRequired("Notifier-Drop-in-Postimages fehlen")
    if transaction.start_block_path not in post_dropins:
        raise NotifierRecoveryRequired("Notifier-Startblock fehlt im vorbereiteten Dienst")
    final_dropins = dict(post_dropins)
    final_dropins.pop(transaction.start_block_path, None)
    unit["post_dropins"] = final_dropins
    return snapshot


def _final_notifier_poststate(transaction: _NotifierTransaction) -> dict[str, object]:
    files = {
        path: transaction.file_postimages.get(path, preimage)
        for path, preimage in transaction.file_preimages.items()
        if path != LEGACY_NOTIFY_PATH
    }
    crons = {
        user: transaction.cron_postimages.get(user, preimage)
        for user, preimage in transaction.cron_preimages.items()
    }
    return {
        "script_snapshot": transaction.script_snapshot,
        "files": files,
        "crons": crons,
        "service_snapshot": transaction.service_snapshot,
        "service_live_state": _capture_service_live_state(transaction.service_snapshot),
        "start_block_path": transaction.start_block_path,
        "start_block_marker": transaction.start_block_marker,
        "start_block_expected": transaction.start_block_preimage,
    }


def _persist_outer_commit_decision(
    transaction: _NotifierTransaction,
    *,
    child_status: Mapping[str, object] | None,
) -> None:
    journal = transaction.outer_journal
    tx_dir = transaction.outer_tx_dir
    record = transaction.outer_record
    if journal is None or tx_dir is None or record is None:
        raise NotifierRecoveryRequired("Notifier-Commit besitzt kein Außenjournal")
    if record.get("decision") not in {None, "commit"}:
        raise _NotifierAmbiguousState("Notifier-Außenentscheidung widerspricht dem Commit")
    record["decision"] = "commit"
    if child_status is not None:
        record["watchdog_commit"] = dict(child_status)
    journal.advance(
        tx_dir,
        record,
        stage="outer_commit_durable",
        phase="outer_commit_durable",
    )


def _finalize_outer_commit(
    transaction: _NotifierTransaction,
    *,
    recovered: bool,
) -> None:
    from .utils import (
        _systemd_show_contract,
        activate_systemd_service_bundle,
        run_command,
    )

    journal = transaction.outer_journal
    tx_dir = transaction.outer_tx_dir
    record = transaction.outer_record
    if (
        journal is None
        or tx_dir is None
        or record is None
        or record.get("decision") != "commit"
        or record.get("stage") != "outer_commit_durable"
    ):
        raise NotifierRecoveryRequired("Notifier-Commitentscheidung ist nicht dauerhaft")
    if not _prepared_payloads_match(journal, tx_dir, record):
        raise _NotifierAmbiguousState("Notifier-Postimages drifteten während der Commit-Freigabe")

    _release_transaction_start_block(transaction)
    final_snapshot = _service_snapshot_without_start_block(transaction)
    start_service = bool(record.get("start_service"))
    if not start_service:
        stopped = run_command("systemctl stop e3dc-notifier.service", timeout=30)
        if not stopped.get("success"):
            state = _systemd_show_contract(NOTIFIER_UNIT)
            if state.get("active_state") not in {"", "inactive", "failed"}:
                raise NotifierRecoveryRequired("Notifier blieb bei deaktiviertem Start aktiv")
    if not activate_systemd_service_bundle(
        final_snapshot,
        enabled_units=(NOTIFIER_SERVICE,),
        start_order=(NOTIFIER_SERVICE,) if start_service else (),
        start_services=start_service,
    ):
        raise NotifierRecoveryRequired("Notifier-Dienstcommit blieb unbestätigt")
    transaction.service_snapshot = final_snapshot
    record["final_poststate"] = journal.encode(
        tx_dir,
        _final_notifier_poststate(transaction),
        label="final-poststate",
    )
    if not _final_notifier_state_matches(journal, tx_dir, record):
        raise NotifierRecoveryRequired("Finaler Notifier-Nachzustand blieb unbestätigt")
    record["state"] = "committed"
    if recovered:
        record["recovered"] = True
    journal.advance(
        tx_dir,
        record,
        stage="committed",
        phase="recovered_commit" if recovered else "committed",
    )


def _tracked_atomic_write(
    transaction: _NotifierTransaction,
    path: str,
    payload: bytes,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> Mapping[str, object]:
    preimage = transaction.file_preimages[path]
    _outer_intent(
        transaction,
        kind="file",
        key=path,
        expected={
            "payload": payload,
            "uid": uid,
            "gid": gid,
            "mode": mode,
        },
    )
    try:
        postimage = atomic_write_bound_file(
            path,
            payload,
            uid=uid,
            gid=gid,
            mode=mode,
            expected_snapshot=preimage,
        )
    except Exception:
        try:
            current = snapshot_bound_file(path, allow_missing=True)
        except Exception:
            transaction.uncertain_files.add(path)
        else:
            if snapshots_match(current, preimage, exact_metadata=True):
                pass
            elif (
                current.get("exists")
                and current.get("sha256") == hashlib.sha256(payload).hexdigest()
                and current.get("uid") == uid
                and current.get("gid") == gid
                and current.get("mode") == mode
            ):
                transaction.file_postimages[path] = current
            else:
                transaction.uncertain_files.add(path)
        if path in transaction.file_postimages:
            _outer_applied(
                transaction,
                actual=transaction.file_postimages[path],
            )
        raise
    transaction.file_postimages[path] = postimage
    _outer_applied(transaction, actual=postimage)
    return postimage


def _require_root_identity() -> tuple[str, int, int]:
    if os.geteuid() != 0:
        raise NotifierInstallError("Die Notifier-Installation benötigt Root-Rechte")
    install_user = str(get_install_user() or "").strip()
    if not install_user or install_user in {"root", "www-data"}:
        raise NotifierInstallError("Installationsbenutzer ist nicht sicher gebunden")
    try:
        account = pwd.getpwnam(install_user)
        www_gid = grp.getgrnam("www-data").gr_gid
    except KeyError as exc:
        raise NotifierInstallError("Installationskonto oder www-data fehlt") from exc
    return install_user, int(account.pw_uid), int(www_gid)


def _run_crontab(
    user: str,
    arguments: tuple[str, ...],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise NotifierInstallError(f"Cron-Benutzer existiert nicht: {user}") from exc
    if account.pw_name != user or any(char in user for char in "\r\n\x00"):
        raise NotifierInstallError("Cron-Benutzer ist nicht eindeutig")
    env = dict(os.environ)
    env["LC_ALL"] = "C"
    try:
        return subprocess.run(
            (CRONTAB_BIN, "-u", user, *arguments),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
            check=False,
            env=env,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise NotifierInstallError(f"Crontab für {user} ist nicht ausführbar") from exc


def _capture_crontab(user: str) -> bytes | None:
    result = _run_crontab(user, ("-l",))
    if result.returncode == 0:
        return bytes(result.stdout)
    message = bytes(result.stderr).decode("utf-8", "replace").strip().lower()
    allowed_missing = {
        f"no crontab for {user}".lower(),
        f"crontab: no crontab for {user}".lower(),
    }
    if result.returncode == 1 and message in allowed_missing:
        return None
    raise NotifierInstallError(
        f"Cron-Vorzustand für {user} ist nicht eindeutig (Exit {result.returncode})"
    )


def _restore_crontab(user: str, expected_current: bytes | None, previous: bytes | None) -> None:
    current = _capture_crontab(user)
    if current == previous:
        return
    if current != expected_current:
        raise NotifierInstallError(f"Crontab für {user} driftete vor dem Rollback")
    if previous is None:
        result = _run_crontab(user, ("-r",))
        if result.returncode not in {0, 1}:
            raise NotifierInstallError(f"Crontab für {user} konnte nicht entfernt werden")
    else:
        result = _run_crontab(user, ("-",), input_bytes=previous)
        if result.returncode != 0:
            raise NotifierInstallError(f"Crontab für {user} konnte nicht restauriert werden")
    if _capture_crontab(user) != previous:
        raise NotifierInstallError(f"Crontab-Rollback für {user} ist unvollständig")


def _without_legacy_crons(payload: bytes) -> bytes:
    try:
        lines = payload.decode("utf-8").splitlines(keepends=True)
    except UnicodeDecodeError as exc:
        raise NotifierInstallError("Cron-Inhalt ist nicht UTF-8-lesbar") from exc
    return "".join(
        line for line in lines if not any(marker in line for marker in _LEGACY_CRON_MARKERS)
    ).encode("utf-8")


def _install_crontab(user: str, previous: bytes | None, desired: bytes | None) -> bytes | None:
    if desired == previous:
        return previous
    if desired is None or desired == b"":
        result = _run_crontab(user, ("-r",))
        if result.returncode not in {0, 1}:
            raise NotifierInstallError(f"Alte Cronjobs für {user} konnten nicht entfernt werden")
        desired = None
    else:
        result = _run_crontab(user, ("-",), input_bytes=desired)
        if result.returncode != 0:
            raise NotifierInstallError(f"Cronjobs für {user} konnten nicht ersetzt werden")
    actual = _capture_crontab(user)
    if actual != desired:
        raise NotifierInstallError(f"Cron-Readback für {user} weicht ab")
    return actual


def _legacy_token_updates(source: bytes, config_payload: bytes) -> tuple[bytes, dict] | None:
    try:
        source_text = source.decode("utf-8")
        data = json.loads(config_payload.decode("utf-8-sig"))
    except (UnicodeDecodeError, ValueError, TypeError) as exc:
        raise NotifierInstallError("Legacy-Tokenquelle oder V4-Konfiguration ist ungültig") from exc
    if not isinstance(data, dict):
        raise NotifierInstallError("V4-Konfiguration besitzt kein Objektformat")
    token_match = re.search(r'TOKEN="([^"]+)"', source_text)
    chat_match = re.search(r'CHAT_ID="([^"]+)"', source_text)
    device_match = re.search(r'DEVICE_NAME="([^"]+)"', source_text)
    if not token_match or not chat_match or data.get("telegram_token"):
        return None
    updated = dict(data)
    updated["telegram_token"] = token_match.group(1)
    updated["telegram_chat_id"] = chat_match.group(1)
    if device_match:
        updated["telegram_device_name"] = device_match.group(1)
    payload = (json.dumps(updated, ensure_ascii=False, indent=4) + "\n").encode("utf-8")
    return payload, updated


def _capture_transaction(
    *,
    migrate_legacy_config: bool,
    expected_recovery_dropins=None,
) -> _NotifierTransaction:
    from .utils import capture_systemd_service_bundle

    install_user, install_uid, www_gid = _require_root_identity()
    script_path = os.path.join(
        get_install_path(),
        "Installer",
        "notification_manager.py",
    )
    script = snapshot_bound_file(script_path, allow_missing=False)
    if script.get("kind") != "regular" or int(script.get("uid", -1)) not in {0, install_uid}:
        raise NotifierInstallError("Notifier-Produktskript ist nicht sicher gebunden")

    file_preimages: dict[str, Mapping[str, object]] = {}
    system_crontab = snapshot_bound_file(
        SYSTEM_CRONTAB_PATH,
        allow_missing=True,
        expected_uid=0,
        expected_gid=0,
    )
    file_preimages[SYSTEM_CRONTAB_PATH] = system_crontab

    if migrate_legacy_config:
        config = snapshot_bound_file(V4_CONFIG_PATH, allow_missing=True)
        if config.get("exists") and (
            config.get("kind") != "regular"
            or int(config.get("uid", -1)) not in {0, install_uid}
            or int(config.get("gid", -1)) not in {0, www_gid}
        ):
            raise NotifierInstallError("V4-Konfiguration besitzt unsichere Metadaten")
        source = snapshot_bound_file(LEGACY_NOTIFY_PATH, allow_missing=True)
        if source.get("exists") and (
            source.get("kind") != "regular"
            or int(source.get("uid", -1)) not in {0, install_uid}
            or int(source.get("mode", 0)) & 0o022
        ):
            raise NotifierInstallError("Legacy-Tokenquelle ist nicht vertrauenswürdig")
        file_preimages[V4_CONFIG_PATH] = config
        file_preimages[LEGACY_NOTIFY_PATH] = source

    cron_preimages = {
        user: _capture_crontab(user)
        for user in (install_user, "root", "www-data")
    }
    service_snapshot = capture_systemd_service_bundle(
        (NOTIFIER_SERVICE,),
        expected_recovery_dropins=expected_recovery_dropins,
    )
    return _NotifierTransaction(
        install_user=install_user,
        install_uid=install_uid,
        www_gid=www_gid,
        service_snapshot=service_snapshot,
        service_original_snapshot=copy.deepcopy(service_snapshot),
        script_snapshot=script,
        cron_preimages=cron_preimages,
        file_preimages=file_preimages,
    )


def _apply_transaction(
    transaction: _NotifierTransaction,
    *,
    start_service: bool,
    migrate_legacy_config: bool,
) -> None:
    if (
        transaction.outer_journal is not None
        and transaction.outer_tx_dir is not None
        and transaction.outer_record is not None
    ):
        transaction.outer_journal.advance(
            transaction.outer_tx_dir,
            transaction.outer_record,
            stage="notifier_in_progress",
            phase="notifier_in_progress",
        )

    current_script = snapshot_bound_file(
        str(transaction.script_snapshot["path"]),
        allow_missing=False,
    )
    if not snapshots_match(current_script, transaction.script_snapshot, exact_metadata=True):
        raise NotifierInstallError("Notifier-Produktskript driftete vor der Installation")

    if migrate_legacy_config:
        config = transaction.file_preimages.get(V4_CONFIG_PATH)
        source = transaction.file_preimages.get(LEGACY_NOTIFY_PATH)
        if config and source and config.get("exists") and source.get("exists"):
            updates = _legacy_token_updates(
                bytes(source["payload"]),
                bytes(config["payload"]),
            )
            if updates is not None:
                payload, data = updates
                _tracked_atomic_write(
                    transaction,
                    V4_CONFIG_PATH,
                    payload,
                    uid=transaction.install_uid,
                    gid=transaction.www_gid,
                    mode=config_secret_file_mode(data),
                )
                print("[OK] Telegram-Zugangsdaten sicher in e3dc_v4.json migriert.")
    else:
        print("→ Betriebskonfiguration bleibt im Release-Fenster unverändert.")

    system_crontab = transaction.file_preimages[SYSTEM_CRONTAB_PATH]
    if system_crontab.get("exists"):
        desired = _without_legacy_crons(bytes(system_crontab["payload"]))
        if desired != bytes(system_crontab["payload"]):
            _tracked_atomic_write(
                transaction,
                SYSTEM_CRONTAB_PATH,
                desired,
                uid=int(system_crontab["uid"]),
                gid=int(system_crontab["gid"]),
                mode=int(system_crontab["mode"]),
            )

    for user, previous in transaction.cron_preimages.items():
        desired = None if previous is None else _without_legacy_crons(previous)
        if desired != previous:
            # Das Soll wird vor der Mutation vermerkt. Falls crontab nach einem
            # Teilcommit Fehler meldet, kann der Außentransaktions-Rollback den
            # exakten Soll- oder unveränderten Vorzustand noch unterscheiden.
            transaction.cron_postimages[user] = desired
            _outer_intent(
                transaction,
                kind="cron",
                key=user,
                expected=desired,
            )
            transaction.cron_postimages[user] = _install_crontab(
                user,
                previous,
                desired,
            )
            _outer_applied(
                transaction,
                actual=transaction.cron_postimages[user],
            )

    _install_notifier_unit_while_blocked(
        transaction,
        start_service=start_service,
    )


def _nonservice_prestate_matches(transaction: _NotifierTransaction) -> bool:
    if not _file_matches_snapshot(transaction.script_snapshot):
        return False
    for path, expected in transaction.file_preimages.items():
        if path == LEGACY_NOTIFY_PATH:
            continue
        if not _file_matches_restored_preimage(expected):
            return False
    for user, expected in transaction.cron_preimages.items():
        try:
            if _capture_crontab(user) != expected:
                return False
        except Exception:
            return False
    return True


def _complete_durable_outer_rollback(transaction: _NotifierTransaction) -> bool:
    from .utils import rollback_systemd_service_bundle

    journal = transaction.outer_journal
    tx_dir = transaction.outer_tx_dir
    record = transaction.outer_record
    original_service = transaction.service_original_snapshot or {}
    if (
        journal is None
        or tx_dir is None
        or record is None
        or record.get("decision") != "rollback"
        or record.get("stage") != "outer_rollback_durable"
        or not _nonservice_prestate_matches(transaction)
        or not _service_files_match_restored_preimage(original_service)
    ):
        return False
    try:
        if transaction.start_block_path:
            _release_transaction_start_block(transaction)
        if rollback_systemd_service_bundle(original_service) is not True:
            return False
        return _notifier_state_matches(journal, tx_dir, record, post=False)
    except Exception:
        return False


def _rollback_transaction(transaction: _NotifierTransaction) -> bool:
    from .utils import (
        _systemd_show_contract,
        rollback_systemd_service_bundle,
        run_command,
    )

    journal = transaction.outer_journal
    tx_dir = transaction.outer_tx_dir
    record = transaction.outer_record
    if journal is None or tx_dir is None or record is None:
        return False
    if record.get("decision") == "commit":
        return False
    if record.get("decision") == "rollback":
        return _complete_durable_outer_rollback(transaction)

    errors: list[str] = [
        f"file_uncertain:{path}"
        for path in sorted(transaction.uncertain_files)
    ]
    if record.get("start_block") is not None:
        try:
            _ensure_transaction_start_block(transaction)
        except Exception:
            errors.append("start_block")
    if transaction.service_touched:
        stopped = run_command("systemctl stop e3dc-notifier.service", timeout=30)
        if not stopped.get("success"):
            try:
                state = _systemd_show_contract(NOTIFIER_UNIT)
            except Exception:
                errors.append("notifier_stop")
            else:
                if state.get("active_state") not in {"", "inactive", "failed"}:
                    errors.append("notifier_stop")

    for user in reversed(tuple(transaction.cron_postimages)):
        try:
            _restore_crontab(
                user,
                transaction.cron_postimages[user],
                transaction.cron_preimages[user],
            )
        except Exception:
            errors.append(f"cron:{user}")

    for path in reversed(tuple(transaction.file_postimages)):
        try:
            restore_bound_file(
                transaction.file_preimages[path],
                expected_current=transaction.file_postimages[path],
            )
        except Exception:
            errors.append(f"file:{path}")

    if not errors and transaction.service_touched:
        if rollback_systemd_service_bundle(transaction.service_snapshot) is not True:
            errors.append("service")
    if not errors and (
        not _nonservice_prestate_matches(transaction)
        or not _service_files_match_restored_preimage(
            transaction.service_original_snapshot or {}
        )
        or (
            record.get("start_block") is not None
            and not _start_block_is_active(transaction)
        )
    ):
        errors.append("blocked_prestate")
    if errors:
        run_command("systemctl disable e3dc-notifier.service", timeout=30)
        run_command("systemctl stop e3dc-notifier.service", timeout=30)
        return False

    record["decision"] = "rollback"
    journal.advance(
        tx_dir,
        record,
        stage="outer_rollback_durable",
        phase="outer_rollback_durable",
    )
    return _complete_durable_outer_rollback(transaction)


@contextmanager
def notifier_install_transaction(
    *,
    start_service: bool = True,
    migrate_legacy_config: bool = True,
    watchdog_required: bool = False,
    expected_recovery_dropins=None,
):
    """Installiert den Notifier und rollt bei einem Fehler exakt zurück.

    Der Kontext bleibt bis zum Commit des aufrufenden Dienstbundles gesperrt.
    So kann die Watchdog-Installation einen bereits vorbereiteten Notifier bei
    ihrem eigenen Fehlschlag ebenfalls wieder in den gebundenen Vorzustand
    versetzen.
    """

    with exclusive_transaction_lock("notifier-install"):
        _recover_outer_transactions_locked()
        if _remove_legacy_notifier_staging_residue():
            print("[OK] Leeres Notifier-Transaktionsstaging aus Altbestand entfernt.")
        transaction = _capture_transaction(
            migrate_legacy_config=migrate_legacy_config,
            expected_recovery_dropins=expected_recovery_dropins,
        )
        journal = _NotifierWatchdogJournal()
        tx_dir, record = journal.create(
            transaction,
            watchdog_required=watchdog_required,
            start_service=start_service,
            migrate_legacy_config=migrate_legacy_config,
        )
        transaction.outer_journal = journal
        transaction.outer_tx_dir = tx_dir
        transaction.outer_record = record
        try:
            _install_notifier_start_block(transaction)
            _apply_transaction(
                transaction,
                start_service=start_service,
                migrate_legacy_config=migrate_legacy_config,
            )
            record["poststate"] = journal.encode(
                tx_dir,
                _notifier_poststate(transaction),
                label="poststate",
            )
            record["mutation"] = None
            journal.advance(
                tx_dir,
                record,
                stage="notifier_prepared",
                phase="notifier_prepared",
            )
            yield transaction
            if watchdog_required:
                if record.get("stage") != "watchdog_intent":
                    raise NotifierInstallError(
                        "Watchdog-Kindtransaktion wurde nicht dauerhaft angekündigt"
                    )
                correlation = str(record.get("child_correlation_id") or "")
                try:
                    from .install_watchdog import watchdog_correlation_guard

                    with watchdog_correlation_guard(correlation) as child_status:
                        if child_status.get("status") in {
                            "ambiguous",
                            "recovery_required",
                            "drifted",
                        }:
                            raise _NotifierAmbiguousState(
                                "Watchdog-Kindzustand ist mehrdeutig oder gedriftet"
                            )
                        if (
                            child_status.get("status") != "committed"
                            or int(child_status.get("match_count", -1)) != 1
                        ):
                            raise NotifierInstallError(
                                "Watchdog-Kindtransaktion besitzt keinen bestätigten Commit"
                            )
                        if not _notifier_state_matches(
                            journal,
                            tx_dir,
                            record,
                            post=True,
                        ):
                            raise NotifierInstallError(
                                "Notifier-Nachzustand driftete vor dem äußeren Commit"
                            )
                        _persist_outer_commit_decision(
                            transaction,
                            child_status=child_status,
                        )
                        _finalize_outer_commit(transaction, recovered=False)
                except Exception as exc:
                    if isinstance(exc, (NotifierInstallError, NotifierRecoveryRequired)):
                        raise
                    raise _NotifierAmbiguousState(
                        "Watchdog-Kindzustand ist nicht eindeutig lesbar"
                    ) from exc
            else:
                if not _notifier_state_matches(journal, tx_dir, record, post=True):
                    raise NotifierInstallError(
                        "Notifier-Nachzustand driftete vor dem äußeren Commit"
                    )
                _persist_outer_commit_decision(
                    transaction,
                    child_status=None,
                )
                _finalize_outer_commit(transaction, recovered=False)
        except Exception as exc:
            if record.get("decision") == "commit":
                _mark_outer_recovery_required(
                    journal,
                    tx_dir,
                    record,
                    (f"outer_commit:{type(exc).__name__}",),
                )
                raise NotifierRecoveryRequired(
                    "Notifier-Commit wurde dauerhaft entschieden, aber nicht vollständig freigegeben"
                ) from exc
            if isinstance(exc, _NotifierAmbiguousState):
                _mark_outer_recovery_required(
                    journal,
                    tx_dir,
                    record,
                    (f"watchdog:{type(exc).__name__}",),
                )
                raise
            try:
                watchdog_guard = nullcontext(None)
                if watchdog_required:
                    from .install_watchdog import watchdog_correlation_guard

                    watchdog_guard = watchdog_correlation_guard(
                        str(record.get("child_correlation_id") or "")
                    )
                with watchdog_guard:
                    if record.get("decision") != "rollback":
                        journal.advance(
                            tx_dir,
                            record,
                            stage="rollback_in_progress",
                            phase="rollback_intent",
                        )
                    rollback_transaction, reconstruction_errors = (
                        _transaction_for_persistent_rollback(
                            journal,
                            tx_dir,
                            record,
                        )
                    )
                    if rollback_transaction is None:
                        raise _NotifierAmbiguousState(
                            "Notifier-Rollback ist nicht eindeutig rekonstruierbar: "
                            + ",".join(reconstruction_errors)
                        )
                    rollback_ok = _rollback_transaction(rollback_transaction)
                    prestate_ok = _notifier_state_matches(
                        journal,
                        tx_dir,
                        record,
                        post=False,
                    )
                    if rollback_ok and prestate_ok:
                        record["state"] = "rolled_back"
                        journal.advance(
                            tx_dir,
                            record,
                            stage="rolled_back",
                            phase="rollback_complete",
                        )
            except Exception as rollback_exc:
                _mark_outer_recovery_required(
                    journal,
                    tx_dir,
                    record,
                    (f"notifier_rollback:{type(rollback_exc).__name__}",),
                )
                raise NotifierRecoveryRequired(
                    "Notifier-Installation fehlgeschlagen; Rollback blieb unvollständig"
                ) from rollback_exc
            if not rollback_ok or not prestate_ok:
                _mark_outer_recovery_required(
                    journal,
                    tx_dir,
                    record,
                    ("notifier_rollback:unconfirmed",),
                )
                raise NotifierRecoveryRequired(
                    "Notifier-Installation fehlgeschlagen; Rollback blieb unvollständig"
                ) from exc
            raise


def begin_watchdog_child(transaction: _NotifierTransaction) -> str:
    """Persistiert die Kindabsicht vor dem ersten Watchdog-Journalrecord."""

    journal = transaction.outer_journal
    tx_dir = transaction.outer_tx_dir
    record = transaction.outer_record
    if journal is None or tx_dir is None or record is None:
        raise NotifierInstallError("Watchdog-Kind besitzt keine Außentransaktion")
    if not bool(record.get("watchdog_required")) or record.get("stage") != "notifier_prepared":
        raise NotifierInstallError("Watchdog-Kindabsicht liegt außerhalb der Vorbereitungsphase")
    correlation = str(record.get("child_correlation_id") or "")
    if not re.fullmatch(r"[0-9a-f]{32}", correlation):
        raise NotifierRecoveryRequired("Watchdog-Korrelation ist nicht sicher gebunden")
    record["watchdog_intent_durable"] = True
    journal.advance(
        tx_dir,
        record,
        stage="watchdog_intent",
        phase="watchdog_intent",
    )
    return correlation


def cleanup_old_crons(user: str) -> bool:
    """Kompatibilitätseinstieg mit eigenem exakten Cron-Rollback."""

    with exclusive_transaction_lock("notifier-install"):
        _require_root_identity()
        users = (str(user), "root", "www-data")
        preimages = {name: _capture_crontab(name) for name in users}
        postimages: dict[str, bytes | None] = {}
        try:
            for name, previous in preimages.items():
                desired = None if previous is None else _without_legacy_crons(previous)
                if desired != previous:
                    postimages[name] = desired
                    postimages[name] = _install_crontab(name, previous, desired)
            return True
        except Exception:
            rollback_ok = True
            for name in reversed(users):
                if name in postimages:
                    try:
                        _restore_crontab(name, postimages[name], preimages[name])
                    except Exception:
                        rollback_ok = False
            if not rollback_ok:
                raise NotifierRecoveryRequired("Cron-Rollback blieb unvollständig")
            return False


def migrate_telegram_tokens() -> bool:
    """Kompatibilitätseinstieg; die Hauptinstallation nutzt die Gesamttransaktion."""

    with exclusive_transaction_lock("notifier-install"):
        _install_user, install_uid, www_gid = _require_root_identity()
        config = snapshot_bound_file(V4_CONFIG_PATH, allow_missing=True)
        source = snapshot_bound_file(LEGACY_NOTIFY_PATH, allow_missing=True)
        if not config.get("exists") or not source.get("exists"):
            return True
        if (
            config.get("kind") != "regular"
            or int(config.get("uid", -1)) not in {0, install_uid}
            or int(config.get("gid", -1)) not in {0, www_gid}
            or source.get("kind") != "regular"
            or int(source.get("uid", -1)) not in {0, install_uid}
            or int(source.get("mode", 0)) & 0o022
        ):
            return False
        updates = _legacy_token_updates(bytes(source["payload"]), bytes(config["payload"]))
        if updates is None:
            return True
        payload, data = updates
        expected_sha = hashlib.sha256(payload).hexdigest()
        desired_mode = config_secret_file_mode(data)
        try:
            atomic_write_bound_file(
                V4_CONFIG_PATH,
                payload,
                uid=install_uid,
                gid=www_gid,
                mode=desired_mode,
                expected_snapshot=config,
            )
            return True
        except Exception:
            try:
                current = snapshot_bound_file(V4_CONFIG_PATH, allow_missing=True)
            except Exception as exc:
                raise NotifierRecoveryRequired(
                    "Token-Migration besitzt keinen lesbaren Rückfallzustand"
                ) from exc
            if snapshots_match(current, config, exact_metadata=True):
                return False
            if not (
                current.get("sha256") == expected_sha
                and current.get("uid") == install_uid
                and current.get("gid") == www_gid
                and current.get("mode") == desired_mode
            ):
                raise NotifierRecoveryRequired(
                    "Token-Migration driftete nach einem unklaren Teilcommit"
                )
            try:
                restore_bound_file(config, expected_current=current)
            except Exception as exc:
                raise NotifierRecoveryRequired(
                    "Token-Migration konnte den Vorzustand nicht restaurieren"
                ) from exc
            return False


def install_notifier(
    start_service: bool = True,
    migrate_legacy_config: bool = True,
    expected_recovery_dropins=None,
) -> bool:
    print("\n=== Benachrichtigungs-Dienst einrichten ===")
    try:
        with notifier_install_transaction(
            start_service=start_service,
            migrate_legacy_config=migrate_legacy_config,
            expected_recovery_dropins=expected_recovery_dropins,
        ):
            pass
    except Exception as exc:
        print(f"[!] Benachrichtigungs-Dienst konnte nicht sicher installiert werden: {exc}")
        return False

    if start_service:
        print("✓ Benachrichtigungs-Dienst (Cron-Ersatz) installiert und gestartet.")
    else:
        print("✓ Benachrichtigungs-Dienst (Cron-Ersatz) installiert; Start wird gesammelt ausgeführt.")
    log_task_completed("Notifier Setup")
    return True


register_command("44", "Benachrichtigungs-Dienst einrichten", install_notifier, sort_order=44)
