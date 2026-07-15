#!/usr/bin/env python3
"""Transactional systemd unit installation for release transitions.

This module is deliberately separate from :mod:`Installer.utils`.  It has no
call sites yet and therefore cannot silently change the product runtime.  A
later, explicitly reviewed integration may use :class:`SystemdTransitionManager`
for the small set of units that a clean-root update must replace.

The caller must already have the privileges needed to write ``unit_root`` and
to invoke systemd.  The implementation never guesses a user, invokes ``sudo``
or follows a unit symlink.  A legitimate systemd mask (an exact ``/dev/null``
symlink) is captured and restored as state; every other unit symlink is
rejected.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import secrets
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence


JOURNAL_SCHEMA = 1
UNIT_NAME_RE = re.compile(r"^[A-Za-z0-9_.@:-]+\.service$")
SUPPORTED_ENABLED_STATES = frozenset(
    {
        "enabled",
        "enabled-runtime",
        "disabled",
        "static",
        "masked",
        "masked-runtime",
        "not-found",
    }
)
SUPPORTED_ACTIVE_STATES = frozenset({"active", "inactive"})


class TransitionSystemdError(RuntimeError):
    """Base error for transition-systemd operations."""


class TransitionBusyError(TransitionSystemdError):
    """Another transition process owns the journal lock."""


class UnitSafetyError(TransitionSystemdError):
    """A unit path, state or journal entry is unsafe or ambiguous."""


class TransitionCommandError(TransitionSystemdError):
    """A fixed-argv system command failed."""


class TransitionRolledBack(TransitionSystemdError):
    """The requested transaction failed but its previous state was restored."""

    def __init__(self, transaction_id: str):
        super().__init__(f"systemd transition {transaction_id} was rolled back")
        self.transaction_id = transaction_id


class TransitionRecoveryRequired(TransitionSystemdError):
    """Rollback was incomplete; writers are held stopped pending recovery."""

    def __init__(self, transaction_id: str):
        super().__init__(f"systemd transition {transaction_id} requires recovery")
        self.transaction_id = transaction_id


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class UnitInstallSpec:
    """One unit replacement inside a single all-or-nothing transaction."""

    target_path: str
    content: str
    enable: bool | None = True
    start: bool = True
    writer: bool = True


@dataclass(frozen=True)
class TransitionResult:
    transaction_id: str
    state: str
    journal_path: str


@dataclass(frozen=True)
class UnitSnapshot:
    unit_name: str
    target_path: str
    file_kind: str
    exists: bool
    sha256: str | None
    size: int | None
    mode: int | None
    uid: int | None
    gid: int | None
    mtime_ns: int | None
    enabled_state: str
    active_state: str
    snapshot_file: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "unit_name": self.unit_name,
            "target_path": self.target_path,
            "file_kind": self.file_kind,
            "exists": self.exists,
            "sha256": self.sha256,
            "size": self.size,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "mtime_ns": self.mtime_ns,
            "enabled_state": self.enabled_state,
            "active_state": self.active_state,
            "snapshot_file": self.snapshot_file,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, Any]) -> "UnitSnapshot":
        required = {
            "unit_name",
            "target_path",
            "file_kind",
            "exists",
            "sha256",
            "size",
            "mode",
            "uid",
            "gid",
            "mtime_ns",
            "enabled_state",
            "active_state",
            "snapshot_file",
        }
        if set(value) != required:
            raise UnitSafetyError("transition journal has an unknown snapshot schema")
        return cls(**dict(value))


CommandRunner = Callable[[Sequence[str], int], CommandResult | subprocess.CompletedProcess[str]]
Postcheck = Callable[[UnitInstallSpec], bool]
PhaseHook = Callable[[str, Mapping[str, Any]], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _default_runner(argv: Sequence[str], timeout: int) -> CommandResult:
    env = os.environ.copy()
    env["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    env["LC_ALL"] = "C"
    result = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


class SystemdTransitionManager:
    """Install and recover a small, explicit batch of systemd units.

    ``journal_root`` is persistent so a later process can recover a transaction
    interrupted by process termination or power loss.  It is private (0700),
    each journal/snapshot is 0600 and a non-blocking ``flock`` serializes every
    transaction and recovery.
    """

    def __init__(
        self,
        *,
        unit_root: str | os.PathLike[str] = "/etc/systemd/system",
        journal_root: str | os.PathLike[str] = "/var/lib/e3dc-control/systemd-transitions",
        runner: CommandRunner | None = None,
        systemctl_path: str = "/usr/bin/systemctl",
        systemd_analyze_path: str = "/usr/bin/systemd-analyze",
        target_uid: int = 0,
        target_gid: int = 0,
        command_timeout: int = 30,
        phase_hook: PhaseHook | None = None,
    ) -> None:
        self.unit_root = Path(os.path.abspath(os.fspath(unit_root)))
        self.journal_root = Path(os.path.abspath(os.fspath(journal_root)))
        self.runner = runner or _default_runner
        self.systemctl_path = self._absolute_command(systemctl_path)
        self.systemd_analyze_path = self._absolute_command(systemd_analyze_path)
        self.target_uid = int(target_uid)
        self.target_gid = int(target_gid)
        self.command_timeout = int(command_timeout)
        self.phase_hook = phase_hook
        self.journal_uid = os.geteuid()
        self.journal_gid = os.getegid()
        self.lock_path = self.journal_root / ".transition.lock"
        self.recovery_status_path = self.journal_root / "recovery-required.json"

    @staticmethod
    def _absolute_command(value: str) -> str:
        path = str(value or "")
        if not os.path.isabs(path) or any(char in path for char in ("\x00", "\r", "\n")):
            raise ValueError("system command path must be absolute and free of control characters")
        return path

    @staticmethod
    def _assert_no_symlink_components(path: Path) -> None:
        """Reject every existing symlink component in an authority path."""

        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            if not os.path.lexists(current):
                continue
            if stat.S_ISLNK(os.lstat(current).st_mode):
                raise UnitSafetyError("authority path contains a symlink component")

    def _ensure_private_root(self) -> None:
        self._assert_no_symlink_components(self.journal_root)
        if self.journal_root.exists() or self.journal_root.is_symlink():
            info = os.lstat(self.journal_root)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
                raise UnitSafetyError("transition journal root is not a real directory")
            if info.st_uid != self.journal_uid:
                raise UnitSafetyError("transition journal root has an unexpected owner")
            os.chmod(self.journal_root, 0o700)
        else:
            self.journal_root.mkdir(parents=True, mode=0o700)
            os.chmod(self.journal_root, 0o700)
        self._assert_no_symlink_components(self.journal_root)
        info = os.lstat(self.journal_root)
        if stat.S_IMODE(info.st_mode) != 0o700:
            raise UnitSafetyError("transition journal root is not private")

    @contextmanager
    def _locked(self):
        self._ensure_private_root()
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            info = os.fstat(fd)
            if (
                not stat.S_ISREG(info.st_mode)
                or info.st_uid != self.journal_uid
                or stat.S_IMODE(info.st_mode) != 0o600
                or info.st_nlink != 1
            ):
                raise UnitSafetyError("transition process lock is not private")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise TransitionBusyError("another systemd transition is active") from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    def _normalize_result(
        self,
        result: CommandResult | subprocess.CompletedProcess[str],
    ) -> CommandResult:
        return CommandResult(
            int(result.returncode),
            str(result.stdout or ""),
            str(result.stderr or ""),
        )

    def _run(
        self,
        argv: Sequence[str],
        *,
        timeout: int | None = None,
        check: bool = True,
    ) -> CommandResult:
        if not argv or any("\x00" in str(item) for item in argv):
            raise UnitSafetyError("invalid command argument")
        try:
            result = self._normalize_result(
                self.runner(tuple(str(item) for item in argv), timeout or self.command_timeout)
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise TransitionCommandError(f"{os.path.basename(str(argv[0]))} could not run") from exc
        if check and result.returncode != 0:
            raise TransitionCommandError(
                f"{os.path.basename(str(argv[0]))} failed with rc={result.returncode}"
            )
        return result

    @staticmethod
    def _first_state_line(result: CommandResult) -> str:
        for source in (result.stdout, result.stderr):
            for line in source.splitlines():
                value = line.strip().lower()
                if value:
                    return value
        return ""

    def _query_enabled(self, unit_name: str) -> str:
        result = self._run(
            [self.systemctl_path, "is-enabled", unit_name],
            check=False,
        )
        value = self._first_state_line(result)
        if not value and result.returncode in {1, 4, 5}:
            value = "not-found"
        if value not in SUPPORTED_ENABLED_STATES:
            raise UnitSafetyError(f"unsupported is-enabled state for {unit_name}")
        return value

    def _query_active(self, unit_name: str) -> str:
        result = self._run(
            [self.systemctl_path, "is-active", unit_name],
            check=False,
        )
        value = self._first_state_line(result)
        if not value and result.returncode in {3, 4, 5}:
            value = "inactive"
        if value not in SUPPORTED_ACTIVE_STATES:
            raise UnitSafetyError(f"unsupported is-active state for {unit_name}")
        return value

    def _validate_target(self, target_path: str | os.PathLike[str]) -> tuple[Path, str]:
        self._assert_no_symlink_components(self.unit_root)
        root_info = os.lstat(self.unit_root)
        if stat.S_ISLNK(root_info.st_mode) or not stat.S_ISDIR(root_info.st_mode):
            raise UnitSafetyError("systemd unit root is not a real directory")
        target = Path(os.path.abspath(os.fspath(target_path)))
        if Path(os.path.realpath(target.parent)) != Path(os.path.realpath(self.unit_root)):
            raise UnitSafetyError("unit target is outside the configured unit root")
        unit_name = target.name
        if not UNIT_NAME_RE.fullmatch(unit_name):
            raise UnitSafetyError("invalid systemd unit name")
        if target.exists() or target.is_symlink():
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode):
                if os.readlink(target) != "/dev/null":
                    raise UnitSafetyError("symlink units are rejected")
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnitSafetyError("unit target is not a single regular file")
        return target, unit_name

    def _atomic_write(
        self,
        path: Path,
        payload: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
        mtime_ns: int | None = None,
    ) -> None:
        parent_info = os.lstat(path.parent)
        if stat.S_ISLNK(parent_info.st_mode) or not stat.S_ISDIR(parent_info.st_mode):
            raise UnitSafetyError("atomic-write parent is not a real directory")
        tmp_name = f".{path.name}.tmp.{os.getpid()}.{secrets.token_hex(8)}"
        tmp_path = path.parent / tmp_name
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
        fd: int | None = None
        try:
            fd = os.open(tmp_path, flags, mode)
            os.fchmod(fd, mode)
            os.fchown(fd, uid, gid)
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                if written <= 0:
                    raise OSError("short atomic write")
                view = view[written:]
            os.fsync(fd)
            os.close(fd)
            fd = None
            os.replace(tmp_path, path)
            if mtime_ns is not None:
                os.utime(path, ns=(mtime_ns, mtime_ns), follow_symlinks=False)
            dir_fd = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        finally:
            if fd is not None:
                os.close(fd)
            try:
                os.unlink(tmp_path)
            except FileNotFoundError:
                pass

    def _read_regular_nofollow(self, path: Path) -> tuple[bytes, os.stat_result]:
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
                raise UnitSafetyError("unit snapshot source is not a single regular file")
            chunks: list[bytes] = []
            remaining = before.st_size
            while remaining:
                chunk = os.read(fd, min(1024 * 1024, remaining))
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            after = os.fstat(fd)
            signature_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            signature_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            if remaining or signature_before != signature_after:
                raise UnitSafetyError("unit changed while its pre-state was captured")
            return b"".join(chunks), before
        finally:
            os.close(fd)

    def _capture_snapshot(self, spec: UnitInstallSpec, tx_dir: Path) -> UnitSnapshot:
        target, unit_name = self._validate_target(spec.target_path)
        enabled_state = self._query_enabled(unit_name)
        active_state = self._query_active(unit_name)
        if target.is_symlink():
            if enabled_state not in {"masked", "masked-runtime"}:
                raise UnitSafetyError("/dev/null unit link is not reported as masked")
            return UnitSnapshot(
                unit_name=unit_name,
                target_path=str(target),
                file_kind="mask",
                exists=True,
                sha256=None,
                size=None,
                mode=None,
                uid=None,
                gid=None,
                mtime_ns=None,
                enabled_state=enabled_state,
                active_state=active_state,
                snapshot_file=None,
            )
        if not target.exists():
            return UnitSnapshot(
                unit_name=unit_name,
                target_path=str(target),
                file_kind="missing",
                exists=False,
                sha256=None,
                size=None,
                mode=None,
                uid=None,
                gid=None,
                mtime_ns=None,
                enabled_state=enabled_state,
                active_state=active_state,
                snapshot_file=None,
            )

        payload, info = self._read_regular_nofollow(target)
        before_dir = tx_dir / "before"
        snapshot_path = before_dir / f"{unit_name}.unit"
        self._atomic_write(
            snapshot_path,
            payload,
            mode=0o600,
            uid=self.journal_uid,
            gid=self.journal_gid,
        )
        return UnitSnapshot(
            unit_name=unit_name,
            target_path=str(target),
            file_kind="regular",
            exists=True,
            sha256=_sha256(payload),
            size=info.st_size,
            mode=stat.S_IMODE(info.st_mode),
            uid=info.st_uid,
            gid=info.st_gid,
            mtime_ns=info.st_mtime_ns,
            enabled_state=enabled_state,
            active_state=active_state,
            snapshot_file=str(snapshot_path.relative_to(tx_dir)),
        )

    def _new_transaction_dir(self) -> tuple[str, Path]:
        transaction_id = (
            datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S.%fZ")
            + f"-{os.getpid()}-{secrets.token_hex(6)}"
        )
        tx_dir = self.journal_root / f"tx-{transaction_id}"
        os.mkdir(tx_dir, 0o700)
        os.mkdir(tx_dir / "before", 0o700)
        os.mkdir(tx_dir / "candidates", 0o700)
        return transaction_id, tx_dir

    def _write_journal(self, tx_dir: Path, record: Mapping[str, Any]) -> None:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self._atomic_write(
            tx_dir / "journal.json",
            payload,
            mode=0o600,
            uid=self.journal_uid,
            gid=self.journal_gid,
        )

    def _advance(self, tx_dir: Path, record: dict[str, Any], phase: str) -> None:
        record["phase"] = phase
        record["updated_at"] = _utc_now()
        self._write_journal(tx_dir, record)
        if self.phase_hook is not None:
            self.phase_hook(phase, dict(record))

    def _read_journal(self, tx_dir: Path) -> dict[str, Any]:
        tx_info = os.lstat(tx_dir)
        if (
            stat.S_ISLNK(tx_info.st_mode)
            or not stat.S_ISDIR(tx_info.st_mode)
            or tx_info.st_uid != self.journal_uid
            or stat.S_IMODE(tx_info.st_mode) != 0o700
        ):
            raise UnitSafetyError("transition directory is not private")
        journal_path = tx_dir / "journal.json"
        info = os.lstat(journal_path)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISREG(info.st_mode)
            or info.st_uid != self.journal_uid
            or stat.S_IMODE(info.st_mode) != 0o600
            or info.st_nlink != 1
        ):
            raise UnitSafetyError("transition journal is not private")
        payload, verified_info = self._read_regular_nofollow(journal_path)
        if (
            verified_info.st_uid != self.journal_uid
            or stat.S_IMODE(verified_info.st_mode) != 0o600
        ):
            raise UnitSafetyError("transition journal changed during verification")
        data = json.loads(payload.decode("utf-8"))
        if not isinstance(data, dict) or data.get("schema") != JOURNAL_SCHEMA:
            raise UnitSafetyError("unknown transition journal schema")
        if data.get("transaction_id") != tx_dir.name.removeprefix("tx-"):
            raise UnitSafetyError("transition journal id does not match its directory")
        if not isinstance(data.get("units"), list):
            raise UnitSafetyError("transition journal unit list is invalid")
        if not data["units"] and data.get("state") not in {"preparing", "rolled_back"}:
            raise UnitSafetyError("non-preparing transition journal contains no units")
        return data

    def _candidate_path(self, tx_dir: Path, unit_name: str) -> Path:
        return tx_dir / "candidates" / unit_name

    def _prepare_candidate(self, tx_dir: Path, spec: UnitInstallSpec, unit_name: str) -> Path:
        if not isinstance(spec.content, str) or not spec.content.strip() or "\x00" in spec.content:
            raise UnitSafetyError("unit candidate is empty or contains NUL")
        candidate = self._candidate_path(tx_dir, unit_name)
        self._atomic_write(
            candidate,
            spec.content.encode("utf-8"),
            mode=0o600,
            uid=self.journal_uid,
            gid=self.journal_gid,
        )
        self._run(
            [self.systemd_analyze_path, "verify", str(candidate)],
            timeout=30,
        )
        return candidate

    def _install_candidate(self, candidate: Path, snapshot: UnitSnapshot) -> None:
        target = Path(snapshot.target_path)
        if target.exists() or target.is_symlink():
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode):
                if os.readlink(target) != "/dev/null":
                    raise UnitSafetyError("unit became a non-mask symlink")
                self._run([self.systemctl_path, "unmask", snapshot.unit_name])
                if target.exists() or target.is_symlink():
                    raise UnitSafetyError("systemd unmask did not remove the mask link")
            elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise UnitSafetyError("unit target changed to an unsafe file")
        payload, _info = self._read_regular_nofollow(candidate)
        self._atomic_write(
            target,
            payload,
            mode=0o644,
            uid=self.target_uid,
            gid=self.target_gid,
        )
        installed, installed_info = self._read_regular_nofollow(target)
        if (
            installed != payload
            or stat.S_IMODE(installed_info.st_mode) != 0o644
            or installed_info.st_uid != self.target_uid
            or installed_info.st_gid != self.target_gid
        ):
            raise UnitSafetyError("installed unit candidate failed its file postcheck")

    def _stop_confirmed(self, unit_name: str) -> None:
        self._run([self.systemctl_path, "stop", unit_name], check=False)
        if self._query_active(unit_name) != "inactive":
            raise TransitionCommandError(f"writer {unit_name} did not stop")

    def _apply_desired_state(
        self,
        spec: UnitInstallSpec,
        snapshot: UnitSnapshot,
        *,
        standby: bool,
    ) -> None:
        if spec.enable is True:
            self._run([self.systemctl_path, "enable", snapshot.unit_name])
        elif spec.enable is False:
            self._run([self.systemctl_path, "disable", snapshot.unit_name])

        should_start = bool(spec.start and not (standby and spec.writer))
        if should_start:
            self._run([self.systemctl_path, "start", snapshot.unit_name])
        else:
            self._stop_confirmed(snapshot.unit_name)

        if spec.enable is True and self._query_enabled(snapshot.unit_name) != "enabled":
            raise TransitionCommandError(f"unit {snapshot.unit_name} is not enabled after install")
        if spec.enable is False and self._query_enabled(snapshot.unit_name) != "disabled":
            raise TransitionCommandError(f"unit {snapshot.unit_name} is not disabled after install")
        expected_active = "active" if should_start else "inactive"
        if self._query_active(snapshot.unit_name) != expected_active:
            raise TransitionCommandError(f"unit {snapshot.unit_name} failed its active-state postcheck")

    def _remove_target_nofollow(self, target: Path) -> None:
        if not target.exists() and not target.is_symlink():
            return
        info = os.lstat(target)
        if stat.S_ISLNK(info.st_mode):
            if os.readlink(target) != "/dev/null":
                raise UnitSafetyError("refusing to remove a non-mask unit symlink")
        elif not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise UnitSafetyError("refusing to remove an unsafe unit target")
        os.unlink(target)

    def _restore_file(self, tx_dir: Path, snapshot: UnitSnapshot) -> None:
        target = Path(snapshot.target_path)
        self._validate_target(target)
        self._remove_target_nofollow(target)
        if snapshot.file_kind == "regular":
            if snapshot.snapshot_file is None:
                raise UnitSafetyError("regular pre-state has no private snapshot")
            source = tx_dir / snapshot.snapshot_file
            if source.parent != tx_dir / "before":
                raise UnitSafetyError("snapshot path escaped its transaction")
            payload, source_info = self._read_regular_nofollow(source)
            if (
                source_info.st_uid != self.journal_uid
                or stat.S_IMODE(source_info.st_mode) != 0o600
                or _sha256(payload) != snapshot.sha256
                or len(payload) != snapshot.size
            ):
                raise UnitSafetyError("private unit snapshot does not match its manifest")
            if None in {snapshot.mode, snapshot.uid, snapshot.gid, snapshot.mtime_ns}:
                raise UnitSafetyError("regular pre-state metadata is incomplete")
            self._atomic_write(
                target,
                payload,
                mode=int(snapshot.mode),
                uid=int(snapshot.uid),
                gid=int(snapshot.gid),
                mtime_ns=int(snapshot.mtime_ns),
            )
        elif snapshot.file_kind not in {"missing", "mask"}:
            raise UnitSafetyError("unknown pre-state file kind")

    def _restore_enabled_state(self, snapshot: UnitSnapshot) -> None:
        unit_name = snapshot.unit_name
        state = snapshot.enabled_state
        if state == "masked":
            self._run([self.systemctl_path, "mask", unit_name])
        elif state == "masked-runtime":
            self._run([self.systemctl_path, "mask", "--runtime", unit_name])
        elif state == "enabled":
            self._run([self.systemctl_path, "unmask", unit_name], check=False)
            self._run([self.systemctl_path, "enable", unit_name])
        elif state == "enabled-runtime":
            self._run([self.systemctl_path, "unmask", unit_name], check=False)
            self._run([self.systemctl_path, "enable", "--runtime", unit_name])
        elif state in {"disabled", "static"}:
            self._run([self.systemctl_path, "unmask", unit_name], check=False)
            self._run([self.systemctl_path, "disable", unit_name])
        elif state == "not-found":
            self._run([self.systemctl_path, "disable", unit_name], check=False)
        else:  # defensive: journals are validated before this point
            raise UnitSafetyError("unsupported enabled pre-state")

    def _restore_active_state(self, snapshot: UnitSnapshot) -> None:
        if snapshot.active_state == "active":
            self._run([self.systemctl_path, "start", snapshot.unit_name])
        elif snapshot.active_state == "inactive":
            self._stop_confirmed(snapshot.unit_name)
        else:
            raise UnitSafetyError("unsupported active pre-state")

    def _validate_restored(self, snapshot: UnitSnapshot) -> None:
        target = Path(snapshot.target_path)
        if snapshot.file_kind == "regular":
            payload, info = self._read_regular_nofollow(target)
            actual = (
                _sha256(payload),
                len(payload),
                stat.S_IMODE(info.st_mode),
                info.st_uid,
                info.st_gid,
                info.st_mtime_ns,
            )
            expected = (
                snapshot.sha256,
                snapshot.size,
                snapshot.mode,
                snapshot.uid,
                snapshot.gid,
                snapshot.mtime_ns,
            )
            if actual != expected:
                raise UnitSafetyError("restored unit file does not match its exact pre-state")
        elif snapshot.file_kind == "missing":
            if target.exists() or target.is_symlink():
                raise UnitSafetyError("new unit was not removed during rollback")
        elif snapshot.file_kind == "mask":
            if not target.is_symlink() or os.readlink(target) != "/dev/null":
                raise UnitSafetyError("masked unit was not restored as an exact /dev/null mask")
        if self._query_enabled(snapshot.unit_name) != snapshot.enabled_state:
            raise UnitSafetyError("unit enabled state was not restored exactly")
        if self._query_active(snapshot.unit_name) != snapshot.active_state:
            raise UnitSafetyError("unit active state was not restored exactly")

    def _snapshots_from_record(self, record: Mapping[str, Any]) -> list[UnitSnapshot]:
        snapshots: list[UnitSnapshot] = []
        for entry in record.get("units", []):
            if not isinstance(entry, dict) or not isinstance(entry.get("snapshot"), dict):
                raise UnitSafetyError("invalid unit entry in transition journal")
            snapshot = UnitSnapshot.from_dict(entry["snapshot"])
            target, unit_name = self._validate_target(snapshot.target_path)
            if unit_name != snapshot.unit_name or str(target) != snapshot.target_path:
                raise UnitSafetyError("journal unit target is inconsistent")
            if snapshot.enabled_state not in SUPPORTED_ENABLED_STATES:
                raise UnitSafetyError("journal has an unsupported enabled state")
            if snapshot.active_state not in SUPPORTED_ACTIVE_STATES:
                raise UnitSafetyError("journal has an unsupported active state")
            snapshots.append(snapshot)
        if not snapshots:
            raise UnitSafetyError("journal contains no snapshots")
        return snapshots

    def _attempt_rollback(
        self,
        tx_dir: Path,
        record: dict[str, Any],
    ) -> list[str]:
        errors: list[str] = []
        if not record.get("units"):
            record["state"] = "rolling_back"
            self._advance(tx_dir, record, "rollback_started")
            return errors
        snapshots = self._snapshots_from_record(record)
        record["state"] = "rolling_back"
        self._advance(tx_dir, record, "rollback_started")

        for snapshot in reversed(snapshots):
            try:
                self._stop_confirmed(snapshot.unit_name)
            except Exception as exc:  # keep restoring every other unit
                errors.append(f"stop:{snapshot.unit_name}:{type(exc).__name__}")
        for snapshot in reversed(snapshots):
            try:
                self._restore_file(tx_dir, snapshot)
            except Exception as exc:
                errors.append(f"file:{snapshot.unit_name}:{type(exc).__name__}")

        try:
            self._run([self.systemctl_path, "daemon-reload"])
        except Exception as exc:
            errors.append(f"daemon-reload:{type(exc).__name__}")

        for snapshot in reversed(snapshots):
            try:
                self._restore_enabled_state(snapshot)
            except Exception as exc:
                errors.append(f"enabled:{snapshot.unit_name}:{type(exc).__name__}")
        for snapshot in reversed(snapshots):
            try:
                self._restore_active_state(snapshot)
            except Exception as exc:
                errors.append(f"active:{snapshot.unit_name}:{type(exc).__name__}")
        for snapshot in snapshots:
            try:
                self._validate_restored(snapshot)
            except Exception as exc:
                errors.append(f"validate:{snapshot.unit_name}:{type(exc).__name__}")
        return errors

    def _force_stop_writers(self, record: Mapping[str, Any]) -> dict[str, bool]:
        result: dict[str, bool] = {}
        for unit_name in sorted(set(str(item) for item in record.get("writer_units", []))):
            if not UNIT_NAME_RE.fullmatch(unit_name):
                result[unit_name] = False
                continue
            try:
                self._stop_confirmed(unit_name)
                result[unit_name] = True
            except Exception:
                result[unit_name] = False
        return result

    def _mark_recovery_required(
        self,
        tx_dir: Path,
        record: dict[str, Any],
        rollback_errors: Sequence[str],
    ) -> None:
        writer_stop = self._force_stop_writers(record)
        record["state"] = "recovery_required"
        record["phase"] = "rollback_incomplete"
        record["updated_at"] = _utc_now()
        record["rollback_errors"] = list(rollback_errors)
        record["writer_stop"] = writer_stop
        self._write_journal(tx_dir, record)
        status = {
            "schema": JOURNAL_SCHEMA,
            "status": "recovery_required",
            "transaction_id": record["transaction_id"],
            "updated_at": _utc_now(),
            "rollback_error_count": len(rollback_errors),
            "writer_stop": writer_stop,
        }
        self._atomic_write(
            self.recovery_status_path,
            (json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            mode=0o600,
            uid=self.journal_uid,
            gid=self.journal_gid,
        )

    def _finish_rollback(self, tx_dir: Path, record: dict[str, Any]) -> None:
        record["state"] = "rolled_back"
        record["rollback_errors"] = []
        self._advance(tx_dir, record, "rollback_complete")

    def _pending_transactions(self) -> list[tuple[Path, dict[str, Any]]]:
        pending: list[tuple[Path, dict[str, Any]]] = []
        for tx_dir in sorted(self.journal_root.glob("tx-*")):
            if tx_dir.is_symlink() or not tx_dir.is_dir():
                raise UnitSafetyError("unsafe entry in transition journal root")
            record = self._read_journal(tx_dir)
            if record.get("state") in {"preparing", "in_progress", "rolling_back", "recovery_required"}:
                pending.append((tx_dir, record))
        return pending

    def _clear_recovery_status_if_safe(self) -> None:
        if self._pending_transactions():
            return
        if self.recovery_status_path.exists() or self.recovery_status_path.is_symlink():
            info = os.lstat(self.recovery_status_path)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
                raise UnitSafetyError("hard recovery status is not a regular file")
            os.unlink(self.recovery_status_path)

    def _recover_incomplete_locked(self) -> list[str]:
        recovered: list[str] = []
        for tx_dir, record in self._pending_transactions():
            rollback_errors = self._attempt_rollback(tx_dir, record)
            if rollback_errors:
                self._mark_recovery_required(tx_dir, record, rollback_errors)
                raise TransitionRecoveryRequired(str(record["transaction_id"]))
            self._finish_rollback(tx_dir, record)
            recovered.append(str(record["transaction_id"]))
        self._clear_recovery_status_if_safe()
        return recovered

    def recover_incomplete(self) -> list[str]:
        """Idempotently restore every unfinished private journal."""

        with self._locked():
            return self._recover_incomplete_locked()

    def install_unit(
        self,
        target_path: str | os.PathLike[str],
        content: str,
        *,
        enable: bool | None = True,
        start: bool = True,
        writer: bool = True,
        standby: bool = False,
        postcheck: Postcheck | None = None,
        extra_writer_units: Iterable[str] = (),
        label: str = "",
    ) -> TransitionResult:
        return self.install_batch(
            [
                UnitInstallSpec(
                    target_path=os.fspath(target_path),
                    content=content,
                    enable=enable,
                    start=start,
                    writer=writer,
                )
            ],
            standby=standby,
            postcheck=postcheck,
            extra_writer_units=extra_writer_units,
            label=label,
        )

    def install_batch(
        self,
        specs: Iterable[UnitInstallSpec],
        *,
        standby: bool = False,
        postcheck: Postcheck | None = None,
        extra_writer_units: Iterable[str] = (),
        label: str = "",
    ) -> TransitionResult:
        """Install, enable/start and postcheck an all-or-nothing unit batch."""

        items = list(specs)
        if not items:
            raise ValueError("systemd transition batch is empty")
        validated: list[tuple[UnitInstallSpec, Path, str]] = []
        names: set[str] = set()
        for spec in items:
            if not isinstance(spec, UnitInstallSpec):
                raise TypeError("systemd transition batch contains an invalid spec")
            target, unit_name = self._validate_target(spec.target_path)
            if unit_name in names:
                raise UnitSafetyError("systemd transition batch contains a duplicate unit")
            names.add(unit_name)
            validated.append((spec, target, unit_name))

        writers = {unit_name for spec, _target, unit_name in validated if spec.writer}
        for unit_name in extra_writer_units:
            value = str(unit_name)
            if not UNIT_NAME_RE.fullmatch(value):
                raise UnitSafetyError("invalid extra writer unit name")
            writers.add(value)

        with self._locked():
            self._recover_incomplete_locked()
            transaction_id, tx_dir = self._new_transaction_dir()
            record: dict[str, Any] = {
                "schema": JOURNAL_SCHEMA,
                "transaction_id": transaction_id,
                "created_at": _utc_now(),
                "updated_at": _utc_now(),
                "state": "preparing",
                "phase": "created",
                "label": re.sub(r"[^A-Za-z0-9_.-]", "_", str(label or ""))[:80],
                "standby": bool(standby),
                "writer_units": sorted(writers),
                "units": [],
                "rollback_errors": [],
            }
            self._write_journal(tx_dir, record)

            candidate_paths: dict[str, Path] = {}
            try:
                self._advance(tx_dir, record, "created")
                for spec, _target, unit_name in validated:
                    snapshot = self._capture_snapshot(spec, tx_dir)
                    record["units"].append(
                        {
                            "unit_name": unit_name,
                            "target_path": snapshot.target_path,
                            "enable": spec.enable,
                            "start": bool(spec.start),
                            "writer": bool(spec.writer),
                            "snapshot": snapshot.as_dict(),
                        }
                    )
                record["state"] = "in_progress"
                self._advance(tx_dir, record, "snapshots_captured")

                for spec, _target, unit_name in validated:
                    candidate_paths[unit_name] = self._prepare_candidate(tx_dir, spec, unit_name)
                self._advance(tx_dir, record, "candidates_verified")

                snapshots = self._snapshots_from_record(record)
                by_name = {snapshot.unit_name: snapshot for snapshot in snapshots}
                for _spec, _target, unit_name in validated:
                    self._install_candidate(candidate_paths[unit_name], by_name[unit_name])
                    self._advance(tx_dir, record, f"installed:{unit_name}")

                self._run([self.systemctl_path, "daemon-reload"])
                self._advance(tx_dir, record, "daemon_reloaded")

                for spec, _target, unit_name in validated:
                    self._apply_desired_state(spec, by_name[unit_name], standby=standby)
                    self._advance(tx_dir, record, f"state_applied:{unit_name}")
                    if postcheck is not None and not bool(postcheck(spec)):
                        raise TransitionCommandError(f"custom postcheck failed for {unit_name}")
                    self._advance(tx_dir, record, f"postchecked:{unit_name}")

                record["state"] = "committed"
                self._advance(tx_dir, record, "commit_complete")
                for candidate in candidate_paths.values():
                    try:
                        os.unlink(candidate)
                    except FileNotFoundError:
                        pass
                return TransitionResult(
                    transaction_id=transaction_id,
                    state="committed",
                    journal_path=str(tx_dir / "journal.json"),
                )
            except Exception as exc:
                record["failure_type"] = type(exc).__name__
                record["updated_at"] = _utc_now()
                self._write_journal(tx_dir, record)
                rollback_errors = self._attempt_rollback(tx_dir, record)
                if rollback_errors:
                    self._mark_recovery_required(tx_dir, record, rollback_errors)
                    raise TransitionRecoveryRequired(transaction_id) from exc
                self._finish_rollback(tx_dir, record)
                for candidate in candidate_paths.values():
                    try:
                        os.unlink(candidate)
                    except FileNotFoundError:
                        pass
                raise TransitionRolledBack(transaction_id) from exc


__all__ = [
    "CommandResult",
    "SystemdTransitionManager",
    "TransitionBusyError",
    "TransitionCommandError",
    "TransitionRecoveryRequired",
    "TransitionResult",
    "TransitionRolledBack",
    "TransitionSystemdError",
    "UnitInstallSpec",
    "UnitSafetyError",
]

