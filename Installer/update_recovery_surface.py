"""Enger Recovery-Vertrag für privilegierte Update-Nebenflächen.

Dieses Modul sichert bewusst nicht pauschal ``/etc``. Es bindet die bekannten,
vom Releasepfad veränderbaren Root-Dateien, System- und Nutzer-Crontabs sowie
eine ausdrücklich benannte systemd-Unitmenge. Der rebootfeste systemd-Vertrag
ist vom älteren flüchtigen ``Installer.utils``-Bundle getrennt und startet beim
Datei-/Enablement-Restore niemals selbst einen Dienst.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, replace
import errno
import hashlib
import json
import os
import pwd
import re
import shlex
import stat
import subprocess
from typing import Callable, Mapping, Sequence

from .secure_file_transaction import (
    atomic_write_bound_file,
    ensure_bound_directory,
    open_bound_directory,
    remove_bound_file,
    restore_bound_file,
    snapshot_bound_file,
    snapshots_match,
)


CRONTAB_BINARY = "/usr/bin/crontab"
MAX_CRONTAB_BYTES = 1024 * 1024
RECOVERY_SURFACE_RECEIPT_SCHEMA = "e3dc_update_recovery_surface_receipt_v2"
MAX_RECOVERY_SURFACE_RECEIPT_BYTES = 12 * 1024 * 1024
MAX_ROOT_MANAGED_FILE_BYTES = 4 * 1024 * 1024
MAX_ROOT_MANAGED_FILE_ENTRIES = 128
MAX_APACHE_SECURITY_BYTES = 64 * 1024
RECOVERY_SURFACE_RECEIPT_PATH = (
    "/var/lib/e3dc-update-safety/recovery-surface.json"
)
SYSTEMD_RECOVERY_RECEIPT_SCHEMA = "e3dc_update_systemd_recovery_receipt_v1"
MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES = 32 * 1024 * 1024
MAX_SYSTEMD_UNIT_BYTES = 256 * 1024
MAX_SYSTEMD_DROPIN_ENTRIES = 256
MAX_SYSTEMD_DROPIN_TOTAL_BYTES = 4 * 1024 * 1024
SYSTEMD_CONTROL_BINARY = "/usr/bin/systemctl"
SYSTEMD_RECOVERY_RECEIPT_PATH = (
    "/var/lib/e3dc-update-safety/systemd-recovery-surface.json"
)
_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")


class UpdateRecoverySurfaceError(RuntimeError):
    """Eine eng begrenzte Recovery-Fläche ist nicht sicher beherrschbar."""

    def __init__(
        self,
        code: str,
        subject: str,
        message: str,
        solution: str,
    ) -> None:
        self.code = str(code)
        self.subject = str(subject)
        self.solution = str(solution)
        super().__init__(
            f"[{self.code}] {message} Betroffen: {self.subject}. "
            f"Lösung: {self.solution}"
        )


class _MissingRootParentError(RuntimeError):
    """Ein notwendiger Elternpfad fehlt vor dem unveränderten Preimage-Capture."""

    def __init__(self, path: str) -> None:
        self.path = str(path)
        super().__init__(f"Erforderlicher Root-Elternpfad fehlt: {self.path}")


@dataclass(frozen=True)
class RootFileSpec:
    path: str
    max_bytes: int
    expected_uid: int = 0
    expected_gid: int = 0


@dataclass(frozen=True)
class RootFilePreimage:
    path: str
    max_bytes: int
    exists: bool
    payload: bytes | None
    sha256: str | None
    uid: int | None
    gid: int | None
    mode: int | None
    parent_identity: tuple[int, ...]
    identity: tuple[int, ...] | None

    def as_bound_snapshot(self) -> dict[str, object]:
        return {
            "schema": "e3dc_bound_file_v1",
            "path": self.path,
            "parent_path": os.path.dirname(self.path),
            "parent_identity": self.parent_identity,
            "exists": self.exists,
            "kind": "regular" if self.exists else "missing",
            "identity": self.identity,
            "payload": self.payload if self.exists else None,
            "sha256": self.sha256 if self.exists else None,
            "uid": self.uid if self.exists else None,
            "gid": self.gid if self.exists else None,
            "mode": self.mode if self.exists else None,
        }


@dataclass(frozen=True)
class RootFileInventory:
    files: tuple[RootFilePreimage, ...]


@dataclass(frozen=True)
class RootFileRestoreGuard:
    current_files: tuple[RootFilePreimage, ...]


@dataclass(frozen=True)
class UserCrontabPreimage:
    user: str
    payload: bytes | None
    sha256: str | None


@dataclass(frozen=True)
class CrontabInventory:
    install_user: str
    system_crontab: RootFilePreimage
    user_crontabs: tuple[UserCrontabPreimage, ...]


@dataclass(frozen=True)
class RootManagedFileRecoveryPreimage:
    """Neutraler, rebootfest serialisierbarer Root-Dateivertrag."""

    path: str
    existed: bool
    payload: bytes | None
    sha256: str | None
    uid: int | None
    gid: int | None
    mode: int | None
    parent_dev: int
    parent_ino: int


@dataclass(frozen=True)
class ApacheSecurityRecoveryPreimage:
    """Byte-, Link- und Dienst-Prestate der Apache-Schutzkonfiguration."""

    available: bool
    payload: bytes | None
    sha256: str | None
    uid: int | None
    gid: int | None
    mode: int | None
    enabled: bool
    enabled_target: str | None
    apache_available: bool
    apache_was_active: bool
    apache_unit_file_state: str


@dataclass(frozen=True)
class RecoverySurfaceReceipt:
    """Transaktions- und Backup-gebundene Recovery-Nebenflächen."""

    transaction_id: str
    install_root: str
    full_backup_id: str
    root_files: RootFileInventory
    crontabs: CrontabInventory
    root_managed_files: tuple[RootManagedFileRecoveryPreimage, ...] = ()
    apache_security: ApacheSecurityRecoveryPreimage | None = None


@dataclass(frozen=True)
class PersistedRecoverySurfaceReceipt:
    receipt: RecoverySurfaceReceipt
    path: str
    dev: int
    ino: int
    sha256: str
    identity: tuple[int, ...]


@dataclass(frozen=True)
class CrontabRestoreGuard:
    system_crontab: RootFilePreimage
    user_crontabs: tuple[UserCrontabPreimage, ...]


@dataclass
class SystemdBundlePreimage:
    """Mutable Hülle, damit bestehende Bundle-Helfer Postimages ergänzen können."""

    units: tuple[str, ...]
    snapshot: dict[str, dict[str, object]]


@dataclass(frozen=True)
class SystemdFilePreimage:
    """Nofollow-gebundener systemd-Dateieintrag einschließlich echter Maske."""

    path: str
    kind: str
    payload: bytes | None
    sha256: str | None
    link_target: str | None
    uid: int | None
    gid: int | None
    mode: int | None
    parent_identity: tuple[int, ...]
    identity: tuple[int, ...] | None

    def as_bound_snapshot(self) -> dict[str, object]:
        exists = self.kind != "absent"
        return {
            "schema": "e3dc_bound_file_v1",
            "path": self.path,
            "parent_path": os.path.dirname(self.path),
            "parent_identity": self.parent_identity,
            "exists": exists,
            "kind": (
                "missing"
                if not exists
                else "regular"
                if self.kind == "regular"
                else "symlink"
            ),
            "identity": self.identity,
            "payload": (
                self.payload
                if self.kind == "regular"
                else (
                    self.link_target.encode("utf-8")
                    if self.kind == "mask_symlink"
                    else None
                )
            ),
            "sha256": self.sha256,
            "uid": self.uid,
            "gid": self.gid,
            "mode": self.mode,
        }


@dataclass(frozen=True)
class SystemdDropinDirectoryPreimage:
    path: str
    exists: bool
    uid: int | None
    gid: int | None
    mode: int | None
    parent_identity: tuple[int, ...]
    identity: tuple[int, ...] | None
    entries: tuple[SystemdFilePreimage, ...]


@dataclass(frozen=True)
class SystemdUnitRecoveryPreimage:
    unit: str
    load_state: str
    unit_file_state: str
    pre_active_state: str
    fragment_path: str
    active_dropin_paths: tuple[str, ...]
    main_file: SystemdFilePreimage
    managed_dropins: SystemdDropinDirectoryPreimage
    opaque_dropins: tuple[SystemdFilePreimage, ...]


@dataclass(frozen=True)
class SystemdRecoveryReceipt:
    transaction_id: str
    install_root: str
    full_backup_id: str
    unit_root: str
    units: tuple[SystemdUnitRecoveryPreimage, ...]


@dataclass(frozen=True)
class PersistedSystemdRecoveryReceipt:
    receipt: SystemdRecoveryReceipt
    path: str
    dev: int
    ino: int
    sha256: str
    identity: tuple[int, ...]


@dataclass(frozen=True)
class SystemdRecoveryRestoreGuard:
    """Unmittelbares Postimage aller noch gestoppten Unit-Flächen."""

    current: SystemdRecoveryReceipt


@dataclass(frozen=True)
class SystemdRecoveryStartPlan:
    """Explizit vom Datei-/Enablement-Restore getrennte Startfreigabe."""

    transaction_id: str
    units: tuple[str, ...]
    preactive_units: tuple[str, ...]
    preserved_gate_dropins: tuple[SystemdFilePreimage, ...] = ()


ROOT_FILE_SPECS = (
    RootFileSpec("/etc/fstab", 2 * 1024 * 1024),
    RootFileSpec("/etc/tmpfiles.d/e3dc-control-locks.conf", 64 * 1024),
    RootFileSpec("/usr/local/lib/e3dc-control-watchdog.sha256", 64 * 1024),
)
_SYSTEM_CRONTAB_SPEC = RootFileSpec("/etc/crontab", MAX_CRONTAB_BYTES)
_APACHE_SECURITY_CONF_AVAILABLE = (
    "/etc/apache2/conf-available/e3dc-control-security.conf"
)
_APACHE_SECURITY_CONF_ENABLED = (
    "/etc/apache2/conf-enabled/e3dc-control-security.conf"
)
_APACHE_UNIT_FILE_RECOVERY_STATES = frozenset(
    {
        "enabled",
        "enabled-runtime",
        "disabled",
        "static",
        "indirect",
        "masked",
        "masked-runtime",
        "generated",
        "transient",
        "alias",
        "linked",
        "linked-runtime",
    }
)


def _root_file_solution(path: str) -> str:
    return (
        f"prüfe `sudo ls -ld {os.path.dirname(path)} {path}`. Der Pfad darf "
        "kein Symlink oder Sonderdateityp sein; eine vorhandene Datei muss "
        "root:root gehören und darf für Gruppe/Andere nicht schreibbar sein"
    )


def _missing_root_parent_solution(spec: RootFileSpec, missing_parent: str) -> str:
    target_parent = os.path.dirname(spec.path)
    if spec.path == "/etc/tmpfiles.d/e3dc-control-locks.conf" and missing_parent == target_parent:
        return (
            "prüfe `sudo ls -ld /etc /etc/tmpfiles.d`. Fehlt ausschließlich "
            "`/etc/tmpfiles.d`, lege es vor dem erneuten Update einmalig mit "
            "`sudo install -d -o root -g root -m 0755 /etc/tmpfiles.d` an. "
            "Der Updater legt dieses Verzeichnis vor dem Backup bewusst nicht selbst an"
        )
    if spec.path == "/usr/local/lib/e3dc-control-watchdog.sha256" and missing_parent in {
        "/usr/local",
        "/usr/local/lib",
    }:
        return (
            "prüfe `sudo ls -ld /usr /usr/local /usr/local/lib`. Lege ausschließlich "
            "die fehlende Standardkette vor dem erneuten Update mit "
            "`sudo install -d -o root -g root -m 0755 /usr/local/lib` an. "
            "Der Updater verändert diese Elternkette vor dem Backup nicht"
        )
    if missing_parent in {"/etc", "/usr"}:
        return (
            f"der Systempfad `{missing_parent}` fehlt. Lege ihn nicht blind an; "
            "stelle ihn samt Besitzern und Inhalt aus einem verifizierten Systembackup "
            "oder per Raspberry-Pi-OS-Reparatur wieder her und starte erst danach das Update"
        )
    return (
        f"prüfe `sudo ls -ld {os.path.dirname(missing_parent)} {missing_parent}`. "
        "Stelle den fehlenden Elternpfad mit seinen ursprünglichen root:root-Rechten "
        "wieder her; der Updater erzeugt ihn vor dem Backup nicht automatisch"
    )


def _assert_root_controlled_parent_chain(path: str) -> None:
    target = os.path.normpath(str(path or ""))
    if not os.path.isabs(target) or target != path or target == "/":
        raise ValueError("Root-Dateipfad ist nicht absolut und kanonisch")
    current = "/"
    for component in os.path.dirname(target).split(os.sep):
        if not component:
            continue
        current = os.path.join(current, component)
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise _MissingRootParentError(current) from exc
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) & 0o022
        ):
            raise RuntimeError(
                f"Root-Elternpfad ist nicht ausschließlich root-kontrolliert: {current}"
            )


def _validate_root_file_spec(spec: RootFileSpec) -> None:
    if (
        not isinstance(spec, RootFileSpec)
        or not os.path.isabs(spec.path)
        or os.path.normpath(spec.path) != spec.path
        or isinstance(spec.max_bytes, bool)
        or spec.max_bytes <= 0
        or spec.max_bytes > 4 * 1024 * 1024
        or spec.expected_uid < 0
        or spec.expected_gid < 0
    ):
        raise ValueError("Root-Dateivertrag ist ungültig")


def _preimage_from_snapshot(
    spec: RootFileSpec,
    snapshot: Mapping[str, object],
) -> RootFilePreimage:
    exists = bool(snapshot.get("exists"))
    payload = snapshot.get("payload")
    digest = snapshot.get("sha256")
    uid = snapshot.get("uid")
    gid = snapshot.get("gid")
    mode = snapshot.get("mode")
    identity = snapshot.get("identity")
    parent_identity = tuple(snapshot.get("parent_identity") or ())
    if len(parent_identity) < 2:
        raise RuntimeError("Root-Datei besitzt keine gebundene Elternidentität")
    if exists:
        if (
            snapshot.get("kind") != "regular"
            or not isinstance(payload, bytes)
            or not isinstance(digest, str)
            or hashlib.sha256(payload).hexdigest() != digest
            or uid != spec.expected_uid
            or gid != spec.expected_gid
            or not isinstance(mode, int)
            or bool(mode & 0o022)
            or not isinstance(identity, tuple)
            or len(identity) < 2
        ):
            raise RuntimeError("Root-Datei besitzt kein sicheres reguläres Preimage")
    elif (
        snapshot.get("kind") != "missing"
        or payload is not None
        or digest is not None
        or uid is not None
        or gid is not None
        or mode is not None
        or identity is not None
    ):
        raise RuntimeError("Fehlendes Root-Datei-Preimage ist widersprüchlich")
    return RootFilePreimage(
        path=spec.path,
        max_bytes=spec.max_bytes,
        exists=exists,
        payload=payload if isinstance(payload, bytes) else None,
        sha256=digest if isinstance(digest, str) else None,
        uid=int(uid) if isinstance(uid, int) else None,
        gid=int(gid) if isinstance(gid, int) else None,
        mode=int(mode) if isinstance(mode, int) else None,
        parent_identity=parent_identity,
        identity=tuple(identity) if isinstance(identity, tuple) else None,
    )


def _capture_root_file(spec: RootFileSpec) -> RootFilePreimage:
    _validate_root_file_spec(spec)
    try:
        _assert_root_controlled_parent_chain(spec.path)
        snapshot = snapshot_bound_file(
            spec.path,
            allow_missing=True,
            expected_uid=spec.expected_uid,
            expected_gid=spec.expected_gid,
            max_bytes=spec.max_bytes,
        )
        return _preimage_from_snapshot(spec, snapshot)
    except UpdateRecoverySurfaceError:
        raise
    except _MissingRootParentError as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-ROOT-005",
            exc.path,
            f"Der Elternpfad für das Preimage `{spec.path}` fehlt; ohne unmittelbare "
            "Eltern-Inode wäre eine spätere automatische Entfernung nicht sicher gebunden.",
            _missing_root_parent_solution(spec, exc.path),
        ) from exc
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-ROOT-001",
            spec.path,
            f"Root-Datei-Preimage konnte nicht sicher erfasst werden: {exc}.",
            _root_file_solution(spec.path),
        ) from exc


def _validate_root_inventory(
    inventory: RootFileInventory,
    specs: Sequence[RootFileSpec],
) -> None:
    if not isinstance(inventory, RootFileInventory):
        raise ValueError("Root-Dateiinventar besitzt den falschen Typ")
    expected = tuple(spec.path for spec in specs)
    actual = tuple(item.path for item in inventory.files)
    if actual != expected or len(set(actual)) != len(actual):
        raise ValueError("Root-Dateiinventar ist unvollständig, umsortiert oder doppelt")
    for item, spec in zip(inventory.files, specs):
        if item.max_bytes != spec.max_bytes:
            raise ValueError("Root-Dateiinventar besitzt einen fremden Größenvertrag")
        rebound = _preimage_from_snapshot(spec, item.as_bound_snapshot())
        if rebound != item:
            raise ValueError("Root-Dateiinventar ist semantisch inkonsistent")


def _capture_root_inventory(specs: Sequence[RootFileSpec]) -> RootFileInventory:
    materialized = tuple(specs)
    if len({spec.path for spec in materialized}) != len(materialized):
        raise ValueError("Root-Dateivertrag enthält doppelte Pfade")
    return RootFileInventory(tuple(_capture_root_file(spec) for spec in materialized))


def capture_root_file_preimages() -> RootFileInventory:
    """Bindet ausschließlich die drei freigegebenen optionalen Root-Dateien."""

    return _capture_root_inventory(ROOT_FILE_SPECS)


def _capture_root_restore_guard(
    inventory: RootFileInventory,
    specs: Sequence[RootFileSpec],
) -> RootFileRestoreGuard:
    materialized = tuple(specs)
    _validate_root_inventory(inventory, materialized)
    current = _capture_root_inventory(materialized)
    for previous, actual in zip(inventory.files, current.files):
        if previous.parent_identity[:2] != actual.parent_identity[:2]:
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-ROOT-002",
                previous.path,
                "Elternverzeichnis driftete seit dem Recovery-Preimage.",
                "keine Datei manuell ersetzen; prüfe den genannten Elternpfad und das Updatejournal",
            )
    return RootFileRestoreGuard(current.files)


def capture_root_file_restore_guard(
    inventory: RootFileInventory,
) -> RootFileRestoreGuard:
    """Bindet den aktuellen, zu überschreibenden Zustand unmittelbar vor Restore."""

    return _capture_root_restore_guard(inventory, ROOT_FILE_SPECS)


def _restored_semantics_match(
    actual: RootFilePreimage,
    expected: RootFilePreimage,
) -> bool:
    return bool(
        actual.path == expected.path
        and actual.max_bytes == expected.max_bytes
        and actual.exists == expected.exists
        and actual.payload == expected.payload
        and actual.sha256 == expected.sha256
        and actual.uid == expected.uid
        and actual.gid == expected.gid
        and actual.mode == expected.mode
        and actual.parent_identity[:2] == expected.parent_identity[:2]
    )


def _restore_root_inventory(
    inventory: RootFileInventory,
    guard: RootFileRestoreGuard,
    specs: Sequence[RootFileSpec],
) -> None:
    materialized = tuple(specs)
    _validate_root_inventory(inventory, materialized)
    guard_inventory = RootFileInventory(guard.current_files)
    _validate_root_inventory(guard_inventory, materialized)
    live = _capture_root_inventory(materialized)
    for actual, expected in zip(live.files, guard.current_files):
        if not snapshots_match(
            actual.as_bound_snapshot(),
            expected.as_bound_snapshot(),
            exact_metadata=True,
        ):
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-ROOT-003",
                actual.path,
                "Root-Datei driftete nach Erzeugung des Restore-Guards.",
                "Updater gestoppt lassen, Datei nicht manuell überschreiben und Updatejournal prüfen",
            )

    failures: list[str] = []
    for previous, current, spec in zip(
        inventory.files,
        guard.current_files,
        materialized,
    ):
        try:
            if not _restored_semantics_match(current, previous):
                restore_bound_file(
                    previous.as_bound_snapshot(),
                    expected_current=current.as_bound_snapshot(),
                    max_bytes=spec.max_bytes,
                )
            restored = _capture_root_file(spec)
            if not _restored_semantics_match(restored, previous):
                raise RuntimeError("Restore-Endzustand weicht vom Preimage ab")
        except Exception as exc:
            failures.append(f"{previous.path}: {exc}")
    if failures:
        detail = "; ".join(failures)[:2400]
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-ROOT-004",
            ", ".join(item.path for item in inventory.files),
            f"Root-Dateien konnten nicht vollständig bytegenau restauriert werden: {detail}.",
            "Dienste nicht manuell starten; Sicherung und Updatejournal erhalten und die genannten Dateien gezielt reparieren",
        )


def restore_root_file_preimages(
    inventory: RootFileInventory,
    guard: RootFileRestoreGuard,
) -> None:
    """Restauriert die drei Root-Dateien nur gegen einen frischen Guard."""

    _restore_root_inventory(inventory, guard, ROOT_FILE_SPECS)


def _assert_trusted_crontab_binary(path: str = CRONTAB_BINARY) -> None:
    try:
        metadata = os.lstat(path)
    except FileNotFoundError as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-CRON-008",
            path,
            "Das für bytegenaue Nutzer-Crontab-Preimages erforderliche Programm fehlt.",
            "führe `sudo apt-get update` und danach "
            "`sudo apt-get install -y cron` aus. Prüfe anschließend "
            f"`sudo ls -l {path}` und starte denselben Updatebefehl erneut",
        ) from exc
    except OSError as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-CRON-009",
            path,
            f"Das Crontab-Programm ist nicht sicher lesbar: {exc}.",
            f"prüfe `sudo ls -l {path}` sowie das Dateisystem und starte danach "
            "denselben Updatebefehl erneut",
        ) from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or not stat.S_IMODE(metadata.st_mode) & 0o111
    ):
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-CRON-010",
            path,
            "Das Crontab-Programm besitzt keinen vertrauenswürdigen Systemvertrag.",
            "führe `sudo apt-get update` und danach "
            "`sudo apt-get install --reinstall -y cron` aus. Prüfe anschließend "
            f"`sudo ls -l {path}` und starte denselben Updatebefehl erneut",
        )


def _default_crontab_runner(
    argv: Sequence[str],
    *,
    input_bytes: bytes | None = None,
) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        tuple(str(item) for item in argv),
        input=input_bytes,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        shell=False,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )


CrontabRunner = Callable[..., subprocess.CompletedProcess[bytes]]


def _normalize_crontab_users(install_user: str) -> tuple[str, ...]:
    users = tuple(dict.fromkeys(("root", "www-data", str(install_user or ""))))
    if any(
        not user
        or user.startswith("-")
        or any(char in user for char in "\x00\r\n\t /\\")
        for user in users
    ):
        raise ValueError("Crontab-Kontenliste enthält einen ungültigen Benutzernamen")
    for user in users:
        try:
            pwd.getpwnam(user)
        except KeyError as exc:
            if user == "www-data":
                solution = (
                    "prüfe `getent passwd www-data`. Fehlt das Systemkonto, führe "
                    "`sudo apt-get update` und danach "
                    "`sudo apt-get install --reinstall -y base-passwd` aus; prüfe "
                    "anschließend erneut `getent passwd www-data`. Das Update legt "
                    "das Konto nicht selbst an"
                )
            elif user == "root":
                solution = (
                    "prüfe `getent passwd root`. Fehlt das Root-Konto, brich die "
                    "Installation ab und repariere `/etc/passwd` aus einer verifizierten "
                    "Sicherung oder im Raspberry-Pi-OS-Rettungssystem; das Update legt "
                    "Root niemals an"
                )
            else:
                solution = (
                    f"prüfe `getent passwd {user}`. Stelle das ursprüngliche "
                    "Installationskonto mit identischer UID, primärer Gruppe, Home und "
                    "Dateibesitzern aus der Systemsicherung wieder her; kein Paket und "
                    "der Updater dürfen dieses anlagenspezifische Konto blind neu anlegen"
                )
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-CRON-001",
                user,
                "Erforderliches lokales Konto fehlt.",
                solution,
            ) from exc
    return users


def _capture_user_crontab(
    user: str,
    runner: CrontabRunner,
) -> UserCrontabPreimage:
    try:
        _assert_trusted_crontab_binary()
        result = runner((CRONTAB_BINARY, "-u", user, "-l"), input_bytes=None)
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-CRON-002",
            user,
            f"Crontab konnte nicht per fester Argumentliste gelesen werden: {exc}.",
            f"prüfe `sudo {CRONTAB_BINARY} -u {user} -l` und behebe den gemeldeten lokalen Cronfehler",
        ) from exc

    returncode = int(result.returncode)
    stdout = bytes(result.stdout or b"")
    stderr = bytes(result.stderr or b"")
    if returncode == 0 and not stderr and len(stdout) <= MAX_CRONTAB_BYTES:
        return UserCrontabPreimage(
            user=user,
            payload=stdout,
            sha256=hashlib.sha256(stdout).hexdigest(),
        )
    missing_messages = {
        f"no crontab for {user}".encode("utf-8"),
        f"crontab: no crontab for {user}".encode("utf-8"),
    }
    if returncode == 1 and not stdout and stderr.strip() in missing_messages:
        return UserCrontabPreimage(user=user, payload=None, sha256=None)
    detail = (stderr or stdout)[:800].decode("utf-8", errors="replace").strip()
    raise UpdateRecoverySurfaceError(
        "E3DC-UPD-RECOVERY-CRON-003",
        user,
        f"Crontab-Readback ist nicht eindeutig (Exit {returncode}: {detail or 'keine Diagnose'}).",
        f"führe `sudo {CRONTAB_BINARY} -u {user} -l` aus, behebe den Fehler und starte danach dasselbe Update erneut",
    )


def _validate_user_crontabs(
    values: Sequence[UserCrontabPreimage],
    users: Sequence[str],
) -> None:
    materialized = tuple(values)
    if tuple(item.user for item in materialized) != tuple(users):
        raise ValueError("Crontab-Inventar ist unvollständig, umsortiert oder doppelt")
    for item in materialized:
        if item.payload is None:
            if item.sha256 is not None:
                raise ValueError("Fehlendes Crontab-Preimage besitzt einen Hash")
        elif (
            not isinstance(item.payload, bytes)
            or len(item.payload) > MAX_CRONTAB_BYTES
            or item.sha256 != hashlib.sha256(item.payload).hexdigest()
        ):
            raise ValueError("Crontab-Preimage besitzt keinen gültigen Bytevertrag")


def capture_crontab_preimages(
    install_user: str,
    *,
    _runner: CrontabRunner | None = None,
) -> CrontabInventory:
    """Bindet System-Crontab und Nutzer-Crontabs ohne Shellauswertung."""

    bound_install_user = str(install_user or "")
    users = _normalize_crontab_users(bound_install_user)
    runner = _runner or _default_crontab_runner
    system_crontab = _capture_root_file(_SYSTEM_CRONTAB_SPEC)
    user_crontabs = tuple(_capture_user_crontab(user, runner) for user in users)
    return CrontabInventory(bound_install_user, system_crontab, user_crontabs)


def _validate_crontab_inventory(inventory: CrontabInventory) -> tuple[str, ...]:
    if not isinstance(inventory, CrontabInventory):
        raise ValueError("Crontab-Inventar besitzt den falschen Typ")
    users = _normalize_crontab_users(inventory.install_user)
    _validate_user_crontabs(inventory.user_crontabs, users)
    rebound = _preimage_from_snapshot(
        _SYSTEM_CRONTAB_SPEC,
        inventory.system_crontab.as_bound_snapshot(),
    )
    if rebound != inventory.system_crontab:
        raise ValueError("System-Crontab-Preimage ist inkonsistent")
    return users


def _strict_sha256_id(raw: object, *, label: str) -> str:
    if not isinstance(raw, str) or not _SHA256_RE.fullmatch(raw):
        raise ValueError(f"{label} muss aus genau 64 kleinen Hexzeichen bestehen")
    return raw


def _canonical_install_root(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("Installationswurzel muss ein kanonischer absoluter Pfad sein")
    if (
        not raw
        or raw == "/"
        or raw.startswith("//")
        or not os.path.isabs(raw)
        or os.path.normpath(raw) != raw
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
        or len(raw.encode("utf-8")) > 4096
    ):
        raise ValueError("Installationswurzel muss ein kanonischer absoluter Pfad sein")
    return raw


def _exact_mapping(
    raw: object,
    *,
    keys: frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(raw, dict) or set(raw) != set(keys):
        raise ValueError(f"{label} besitzt kein exaktes Schema")
    if any(not isinstance(key, str) for key in raw):
        raise ValueError(f"{label} besitzt einen ungültigen Schlüssel")
    return raw


def _strict_integer(
    raw: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int = (1 << 64) - 1,
) -> int:
    if (
        isinstance(raw, bool)
        or not isinstance(raw, int)
        or raw < minimum
        or raw > maximum
    ):
        raise ValueError(f"{label} liegt außerhalb des Ganzzahlvertrags")
    return raw


def _identity_from_json(
    raw: object,
    *,
    label: str,
    expected_length: int,
) -> tuple[int, ...]:
    if not isinstance(raw, list) or len(raw) != expected_length:
        raise ValueError(f"{label} besitzt keine vollständige Inodebindung")
    result: list[int] = []
    for index, item in enumerate(raw):
        if expected_length == 9 and index >= 7:
            value = _strict_integer(
                item,
                label=f"{label}[{index}]",
                minimum=-(1 << 63),
                maximum=(1 << 63) - 1,
            )
        else:
            value = _strict_integer(item, label=f"{label}[{index}]")
        result.append(value)
    return tuple(result)


def _payload_to_base64(payload: bytes | None) -> str | None:
    if payload is None:
        return None
    if not isinstance(payload, bytes):
        raise ValueError("Recovery-Payload besitzt keinen Bytevertrag")
    return base64.b64encode(payload).decode("ascii")


def _payload_from_base64(
    raw: object,
    *,
    label: str,
    max_bytes: int,
) -> bytes | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise ValueError(f"{label} besitzt keine Base64-Zeichenkette")
    max_encoded = ((max_bytes + 2) // 3) * 4
    if len(raw) > max_encoded:
        raise ValueError(f"{label} überschreitet den Größenvertrag")
    try:
        encoded = raw.encode("ascii")
        payload = base64.b64decode(encoded, validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError(f"{label} besitzt keine gültige Base64-Kodierung") from exc
    if len(payload) > max_bytes or base64.b64encode(payload) != encoded:
        raise ValueError(f"{label} besitzt keine kanonische Base64-Kodierung")
    return payload


_ROOT_PREIMAGE_KEYS = frozenset(
    {
        "path",
        "max_bytes",
        "exists",
        "payload_b64",
        "sha256",
        "uid",
        "gid",
        "mode",
        "parent_identity",
        "identity",
    }
)


def _root_preimage_mapping(
    preimage: RootFilePreimage,
    spec: RootFileSpec,
) -> dict[str, object]:
    rebound = _preimage_from_snapshot(spec, preimage.as_bound_snapshot())
    if rebound != preimage:
        raise ValueError("Root-Datei-Preimage ist semantisch inkonsistent")
    mapping: dict[str, object] = {
        "path": preimage.path,
        "max_bytes": preimage.max_bytes,
        "exists": preimage.exists,
        "payload_b64": _payload_to_base64(preimage.payload),
        "sha256": preimage.sha256,
        "uid": preimage.uid,
        "gid": preimage.gid,
        "mode": preimage.mode,
        "parent_identity": list(preimage.parent_identity),
        "identity": list(preimage.identity) if preimage.identity is not None else None,
    }
    if _root_preimage_from_mapping(mapping, spec) != preimage:
        raise ValueError("Root-Datei-Preimage verletzt den Persistenzvertrag")
    return mapping


def _root_preimage_from_mapping(
    raw: object,
    spec: RootFileSpec,
) -> RootFilePreimage:
    mapping = _exact_mapping(
        raw,
        keys=_ROOT_PREIMAGE_KEYS,
        label="Root-Datei-Preimage",
    )
    if mapping["path"] != spec.path:
        raise ValueError("Root-Datei-Preimage besitzt einen fremden Pfad")
    max_bytes = _strict_integer(
        mapping["max_bytes"],
        label="Root-Datei.max_bytes",
        minimum=1,
        maximum=4 * 1024 * 1024,
    )
    if max_bytes != spec.max_bytes:
        raise ValueError("Root-Datei-Preimage besitzt einen fremden Größenvertrag")
    if type(mapping["exists"]) is not bool:
        raise ValueError("Root-Datei-Preimage besitzt keinen booleschen Existenzstatus")
    exists = bool(mapping["exists"])
    parent_identity = _identity_from_json(
        mapping["parent_identity"],
        label="Root-Datei.parent_identity",
        expected_length=5,
    )
    if (
        parent_identity[2] != 0
        or parent_identity[3] != 0
        or parent_identity[4] > 0o7777
        or bool(parent_identity[4] & 0o022)
    ):
        raise ValueError("Root-Datei-Elternidentität ist nicht root-kontrolliert")

    payload = _payload_from_base64(
        mapping["payload_b64"],
        label="Root-Datei.payload_b64",
        max_bytes=spec.max_bytes,
    )
    if exists:
        if payload is None:
            raise ValueError("Vorhandenes Root-Datei-Preimage besitzt keine Bytes")
        digest = _strict_sha256_id(mapping["sha256"], label="Root-Datei-SHA256")
        uid = _strict_integer(mapping["uid"], label="Root-Datei.uid")
        gid = _strict_integer(mapping["gid"], label="Root-Datei.gid")
        mode = _strict_integer(
            mapping["mode"],
            label="Root-Datei.mode",
            maximum=0o7777,
        )
        identity = _identity_from_json(
            mapping["identity"],
            label="Root-Datei.identity",
            expected_length=9,
        )
        if (
            digest != hashlib.sha256(payload).hexdigest()
            or uid != spec.expected_uid
            or gid != spec.expected_gid
            or bool(mode & 0o022)
            or identity[2] != uid
            or identity[3] != gid
            or identity[4] != mode
            or identity[5] != 1
            or identity[6] != len(payload)
        ):
            raise ValueError("Root-Datei-Preimage besitzt widersprüchliche Metadaten")
    else:
        if any(
            mapping[key] is not None
            for key in ("payload_b64", "sha256", "uid", "gid", "mode", "identity")
        ):
            raise ValueError("Fehlendes Root-Datei-Preimage besitzt Restmetadaten")
        payload = None
        digest = None
        uid = None
        gid = None
        mode = None
        identity = None

    preimage = RootFilePreimage(
        path=spec.path,
        max_bytes=spec.max_bytes,
        exists=exists,
        payload=payload,
        sha256=digest,
        uid=uid,
        gid=gid,
        mode=mode,
        parent_identity=parent_identity,
        identity=identity,
    )
    if _preimage_from_snapshot(spec, preimage.as_bound_snapshot()) != preimage:
        raise ValueError("Root-Datei-Preimage ist nach Parse inkonsistent")
    return preimage


_USER_CRONTAB_KEYS = frozenset({"user", "payload_b64", "sha256"})


def _user_crontab_mapping(preimage: UserCrontabPreimage) -> dict[str, object]:
    return {
        "user": preimage.user,
        "payload_b64": _payload_to_base64(preimage.payload),
        "sha256": preimage.sha256,
    }


def _user_crontab_from_mapping(
    raw: object,
    *,
    expected_user: str,
) -> UserCrontabPreimage:
    mapping = _exact_mapping(
        raw,
        keys=_USER_CRONTAB_KEYS,
        label="Nutzer-Crontab-Preimage",
    )
    if mapping["user"] != expected_user:
        raise ValueError("Nutzer-Crontab-Preimage ist unvollständig oder umsortiert")
    payload = _payload_from_base64(
        mapping["payload_b64"],
        label=f"Crontab[{expected_user}].payload_b64",
        max_bytes=MAX_CRONTAB_BYTES,
    )
    digest = mapping["sha256"]
    if payload is None:
        if digest is not None:
            raise ValueError("Fehlendes Nutzer-Crontab-Preimage besitzt einen Hash")
        return UserCrontabPreimage(expected_user, None, None)
    bound_digest = _strict_sha256_id(digest, label="Nutzer-Crontab-SHA256")
    if bound_digest != hashlib.sha256(payload).hexdigest():
        raise ValueError("Nutzer-Crontab-Preimage besitzt einen falschen Hash")
    return UserCrontabPreimage(expected_user, payload, bound_digest)


def _canonical_root_managed_path(raw: object) -> str:
    if not isinstance(raw, str):
        raise ValueError("Root-Managed-Pfad muss eine Zeichenkette sein")
    if (
        not raw
        or raw == "/"
        or raw.startswith("//")
        or not os.path.isabs(raw)
        or os.path.normpath(raw) != raw
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
        or len(raw.encode("utf-8")) > 4096
    ):
        raise ValueError("Root-Managed-Pfad ist nicht absolut und kanonisch")
    return raw


_ROOT_MANAGED_PREIMAGE_KEYS = frozenset(
    {
        "path",
        "existed",
        "payload_b64",
        "sha256",
        "uid",
        "gid",
        "mode",
        "parent_dev",
        "parent_ino",
    }
)


def _validate_root_managed_file_preimage(
    value: RootManagedFileRecoveryPreimage,
) -> RootManagedFileRecoveryPreimage:
    if not isinstance(value, RootManagedFileRecoveryPreimage):
        raise ValueError("Root-Managed-Preimage besitzt den falschen Typ")
    path = _canonical_root_managed_path(value.path)
    if type(value.existed) is not bool:
        raise ValueError("Root-Managed-Preimage besitzt keinen booleschen Existenzstatus")
    parent_dev = _strict_integer(
        value.parent_dev,
        label="Root-Managed.parent_dev",
    )
    parent_ino = _strict_integer(
        value.parent_ino,
        label="Root-Managed.parent_ino",
        minimum=1,
    )
    if value.existed:
        if (
            not isinstance(value.payload, bytes)
            or len(value.payload) > MAX_ROOT_MANAGED_FILE_BYTES
        ):
            raise ValueError("Root-Managed-Preimage überschreitet den Bytevertrag")
        digest = _strict_sha256_id(value.sha256, label="Root-Managed-SHA256")
        uid = _strict_integer(value.uid, label="Root-Managed.uid")
        gid = _strict_integer(value.gid, label="Root-Managed.gid")
        mode = _strict_integer(
            value.mode,
            label="Root-Managed.mode",
            maximum=0o7777,
        )
        if (
            digest != hashlib.sha256(value.payload).hexdigest()
            or uid != 0
            or gid != 0
            or bool(mode & 0o022)
        ):
            raise ValueError("Root-Managed-Preimage besitzt widersprüchliche Metadaten")
    else:
        if any(
            item is not None
            for item in (value.payload, value.sha256, value.uid, value.gid, value.mode)
        ):
            raise ValueError("Fehlendes Root-Managed-Preimage besitzt Restmetadaten")
    if path != value.path or parent_dev != value.parent_dev or parent_ino != value.parent_ino:
        raise ValueError("Root-Managed-Preimage ist nicht kanonisch")
    return value


def _validate_root_managed_files(
    values: Sequence[RootManagedFileRecoveryPreimage],
) -> tuple[RootManagedFileRecoveryPreimage, ...]:
    if not isinstance(values, Sequence) or isinstance(values, (str, bytes, bytearray)):
        raise ValueError("Root-Managed-Inventar ist keine endliche Sequenz")
    materialized = tuple(values)
    if len(materialized) > MAX_ROOT_MANAGED_FILE_ENTRIES:
        raise ValueError("Root-Managed-Inventar besitzt zu viele Einträge")
    validated = tuple(_validate_root_managed_file_preimage(item) for item in materialized)
    paths = tuple(item.path for item in validated)
    if len(paths) != len(set(paths)):
        raise ValueError("Root-Managed-Inventar besitzt doppelte Pfade")
    if sum(len(item.payload or b"") for item in validated) > 8 * 1024 * 1024:
        raise ValueError("Root-Managed-Inventar überschreitet das Gesamtbytelimit")
    return validated


def _root_managed_preimage_mapping(
    value: RootManagedFileRecoveryPreimage,
) -> dict[str, object]:
    _validate_root_managed_file_preimage(value)
    return {
        "path": value.path,
        "existed": value.existed,
        "payload_b64": _payload_to_base64(value.payload),
        "sha256": value.sha256,
        "uid": value.uid,
        "gid": value.gid,
        "mode": value.mode,
        "parent_dev": value.parent_dev,
        "parent_ino": value.parent_ino,
    }


def _root_managed_preimage_from_mapping(
    raw: object,
) -> RootManagedFileRecoveryPreimage:
    mapping = _exact_mapping(
        raw,
        keys=_ROOT_MANAGED_PREIMAGE_KEYS,
        label="Root-Managed-Preimage",
    )
    if type(mapping["existed"]) is not bool:
        raise ValueError("Root-Managed-Preimage besitzt keinen booleschen Existenzstatus")
    existed = bool(mapping["existed"])
    payload = _payload_from_base64(
        mapping["payload_b64"],
        label="Root-Managed.payload_b64",
        max_bytes=MAX_ROOT_MANAGED_FILE_BYTES,
    )
    value = RootManagedFileRecoveryPreimage(
        path=_canonical_root_managed_path(mapping["path"]),
        existed=existed,
        payload=payload,
        sha256=(
            _strict_sha256_id(mapping["sha256"], label="Root-Managed-SHA256")
            if existed
            else mapping["sha256"]
        ),
        uid=(
            _strict_integer(mapping["uid"], label="Root-Managed.uid")
            if existed
            else mapping["uid"]
        ),
        gid=(
            _strict_integer(mapping["gid"], label="Root-Managed.gid")
            if existed
            else mapping["gid"]
        ),
        mode=(
            _strict_integer(
                mapping["mode"],
                label="Root-Managed.mode",
                maximum=0o7777,
            )
            if existed
            else mapping["mode"]
        ),
        parent_dev=_strict_integer(
            mapping["parent_dev"],
            label="Root-Managed.parent_dev",
        ),
        parent_ino=_strict_integer(
            mapping["parent_ino"],
            label="Root-Managed.parent_ino",
            minimum=1,
        ),
    )
    return _validate_root_managed_file_preimage(value)


_APACHE_SECURITY_PREIMAGE_KEYS = frozenset(
    {
        "available",
        "payload_b64",
        "sha256",
        "uid",
        "gid",
        "mode",
        "enabled",
        "enabled_target",
        "apache_available",
        "apache_was_active",
        "apache_unit_file_state",
    }
)


def _validate_apache_enabled_target(raw: object) -> str:
    if (
        not isinstance(raw, str)
        or not raw
        or len(raw.encode("utf-8")) > 4096
        or any(ord(char) < 32 or ord(char) == 127 for char in raw)
        or os.path.normpath(raw) != raw
    ):
        raise ValueError("Apache-Schutzaktivierung besitzt kein kanonisches Linkziel")
    resolved = os.path.normpath(
        raw
        if os.path.isabs(raw)
        else os.path.join(os.path.dirname(_APACHE_SECURITY_CONF_ENABLED), raw)
    )
    if resolved != _APACHE_SECURITY_CONF_AVAILABLE:
        raise ValueError("Apache-Schutzaktivierung verweist auf einen fremden Pfad")
    return raw


def _validate_apache_security_preimage(
    value: ApacheSecurityRecoveryPreimage | None,
) -> ApacheSecurityRecoveryPreimage | None:
    if value is None:
        return None
    if not isinstance(value, ApacheSecurityRecoveryPreimage):
        raise ValueError("Apache-Schutz-Preimage besitzt den falschen Typ")
    if (
        type(value.available) is not bool
        or type(value.enabled) is not bool
        or type(value.apache_available) is not bool
        or type(value.apache_was_active) is not bool
    ):
        raise ValueError("Apache-Schutz-Preimage besitzt keinen booleschen Statusvertrag")
    if value.available:
        if (
            not isinstance(value.payload, bytes)
            or len(value.payload) > MAX_APACHE_SECURITY_BYTES
        ):
            raise ValueError("Apache-Schutz-Preimage überschreitet den Bytevertrag")
        digest = _strict_sha256_id(value.sha256, label="Apache-Schutz-SHA256")
        uid = _strict_integer(value.uid, label="Apache-Schutz.uid")
        gid = _strict_integer(value.gid, label="Apache-Schutz.gid")
        mode = _strict_integer(
            value.mode,
            label="Apache-Schutz.mode",
            maximum=0o7777,
        )
        if (
            digest != hashlib.sha256(value.payload).hexdigest()
            or uid != 0
            or gid != 0
            or bool(mode & 0o022)
        ):
            raise ValueError("Apache-Schutz-Preimage besitzt widersprüchliche Metadaten")
    elif any(
        item is not None
        for item in (value.payload, value.sha256, value.uid, value.gid, value.mode)
    ):
        raise ValueError("Fehlendes Apache-Schutz-Preimage besitzt Restmetadaten")

    if value.enabled:
        if not value.available:
            raise ValueError("Apache-Schutzaktivierung existiert ohne Konfiguration")
        _validate_apache_enabled_target(value.enabled_target)
    elif value.enabled_target is not None:
        raise ValueError("Inaktive Apache-Schutzaktivierung besitzt ein Linkziel")

    state = value.apache_unit_file_state
    if (
        not isinstance(state, str)
        or not state
        or any(ord(char) < 32 or ord(char) == 127 for char in state)
    ):
        raise ValueError("Apache UnitFileState besitzt keinen Zeichenkettenvertrag")
    if value.apache_available:
        if state not in _APACHE_UNIT_FILE_RECOVERY_STATES:
            raise ValueError("Apache UnitFileState ist nicht recoveryfähig")
    elif value.apache_was_active or state != "absent":
        raise ValueError("Abwesender Apache besitzt einen Dienst-Prestate")
    return value


def _apache_security_preimage_mapping(
    value: ApacheSecurityRecoveryPreimage | None,
) -> dict[str, object] | None:
    if _validate_apache_security_preimage(value) is None:
        return None
    assert value is not None
    return {
        "available": value.available,
        "payload_b64": _payload_to_base64(value.payload),
        "sha256": value.sha256,
        "uid": value.uid,
        "gid": value.gid,
        "mode": value.mode,
        "enabled": value.enabled,
        "enabled_target": value.enabled_target,
        "apache_available": value.apache_available,
        "apache_was_active": value.apache_was_active,
        "apache_unit_file_state": value.apache_unit_file_state,
    }


def _apache_security_preimage_from_mapping(
    raw: object,
) -> ApacheSecurityRecoveryPreimage | None:
    if raw is None:
        return None
    mapping = _exact_mapping(
        raw,
        keys=_APACHE_SECURITY_PREIMAGE_KEYS,
        label="Apache-Schutz-Preimage",
    )
    for key in ("available", "enabled", "apache_available", "apache_was_active"):
        if type(mapping[key]) is not bool:
            raise ValueError(f"Apache-Schutz.{key} ist nicht boolesch")
    available = bool(mapping["available"])
    payload = _payload_from_base64(
        mapping["payload_b64"],
        label="Apache-Schutz.payload_b64",
        max_bytes=MAX_APACHE_SECURITY_BYTES,
    )
    value = ApacheSecurityRecoveryPreimage(
        available=available,
        payload=payload,
        sha256=(
            _strict_sha256_id(mapping["sha256"], label="Apache-Schutz-SHA256")
            if available
            else mapping["sha256"]
        ),
        uid=(
            _strict_integer(mapping["uid"], label="Apache-Schutz.uid")
            if available
            else mapping["uid"]
        ),
        gid=(
            _strict_integer(mapping["gid"], label="Apache-Schutz.gid")
            if available
            else mapping["gid"]
        ),
        mode=(
            _strict_integer(
                mapping["mode"],
                label="Apache-Schutz.mode",
                maximum=0o7777,
            )
            if available
            else mapping["mode"]
        ),
        enabled=bool(mapping["enabled"]),
        enabled_target=mapping["enabled_target"],
        apache_available=bool(mapping["apache_available"]),
        apache_was_active=bool(mapping["apache_was_active"]),
        apache_unit_file_state=mapping["apache_unit_file_state"],
    )
    return _validate_apache_security_preimage(value)


def _validate_recovery_surface_receipt(
    receipt: RecoverySurfaceReceipt,
) -> RecoverySurfaceReceipt:
    if not isinstance(receipt, RecoverySurfaceReceipt):
        raise ValueError("Recovery-Nebenflächen-Receipt besitzt den falschen Typ")
    transaction_id = _strict_sha256_id(
        receipt.transaction_id,
        label="Transaktions-ID",
    )
    install_root = _canonical_install_root(receipt.install_root)
    full_backup_id = _strict_sha256_id(
        receipt.full_backup_id,
        label="Vollbackup-ID",
    )
    _validate_root_inventory(receipt.root_files, ROOT_FILE_SPECS)
    _validate_crontab_inventory(receipt.crontabs)
    _validate_root_managed_files(receipt.root_managed_files)
    _validate_apache_security_preimage(receipt.apache_security)
    if (
        transaction_id != receipt.transaction_id
        or install_root != receipt.install_root
        or full_backup_id != receipt.full_backup_id
    ):
        raise ValueError("Recovery-Nebenflächen-Receipt ist nicht kanonisch gebunden")
    return receipt


def create_recovery_surface_receipt(
    *,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    root_files: RootFileInventory,
    crontabs: CrontabInventory,
    root_managed_files: Sequence[RootManagedFileRecoveryPreimage] = (),
    apache_security: ApacheSecurityRecoveryPreimage | None = None,
) -> RecoverySurfaceReceipt:
    """Erzeugt den validierten In-Memory-Receipt ohne ihn zu persistieren."""

    receipt = RecoverySurfaceReceipt(
        transaction_id=_strict_sha256_id(transaction_id, label="Transaktions-ID"),
        install_root=_canonical_install_root(install_root),
        full_backup_id=_strict_sha256_id(full_backup_id, label="Vollbackup-ID"),
        root_files=root_files,
        crontabs=crontabs,
        root_managed_files=_validate_root_managed_files(root_managed_files),
        apache_security=_validate_apache_security_preimage(apache_security),
    )
    return _validate_recovery_surface_receipt(receipt)


def _recovery_surface_receipt_mapping(
    receipt: RecoverySurfaceReceipt,
) -> dict[str, object]:
    _validate_recovery_surface_receipt(receipt)
    return {
        "schema": RECOVERY_SURFACE_RECEIPT_SCHEMA,
        "state": "complete",
        "transaction_id": receipt.transaction_id,
        "install_root": receipt.install_root,
        "full_backup_id": receipt.full_backup_id,
        "root_files": [
            _root_preimage_mapping(preimage, spec)
            for preimage, spec in zip(receipt.root_files.files, ROOT_FILE_SPECS)
        ],
        "crontabs": {
            "install_user": receipt.crontabs.install_user,
            "system_crontab": _root_preimage_mapping(
                receipt.crontabs.system_crontab,
                _SYSTEM_CRONTAB_SPEC,
            ),
            "user_crontabs": [
                _user_crontab_mapping(preimage)
                for preimage in receipt.crontabs.user_crontabs
            ],
        },
        "root_managed_files": [
            _root_managed_preimage_mapping(preimage)
            for preimage in receipt.root_managed_files
        ],
        "apache_security": _apache_security_preimage_mapping(
            receipt.apache_security
        ),
    }


def _canonical_recovery_json(mapping: Mapping[str, object]) -> bytes:
    try:
        payload = (
            json.dumps(
                mapping,
                ensure_ascii=True,
                allow_nan=False,
                sort_keys=True,
                separators=(",", ":"),
            )
            + "\n"
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Recovery-Nebenflächen-Receipt ist nicht JSON-kodierbar") from exc
    if len(payload) > MAX_RECOVERY_SURFACE_RECEIPT_BYTES:
        raise ValueError("Recovery-Nebenflächen-Receipt überschreitet das Größenlimit")
    return payload


def serialize_recovery_surface_receipt(
    receipt: RecoverySurfaceReceipt,
) -> bytes:
    """Serialisiert exakt ein kanonisches JSON-Receipt, niemals Python-Objektcode."""

    return _canonical_recovery_json(_recovery_surface_receipt_mapping(receipt))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Doppelter JSON-Schlüssel im Recovery-Receipt: {key}")
        result[key] = value
    return result


def _reject_json_noninteger(raw: str) -> object:
    raise ValueError(f"Nicht-ganzzahliger JSON-Wert ist unzulässig: {raw}")


_RECEIPT_KEYS = frozenset(
    {
        "schema",
        "state",
        "transaction_id",
        "install_root",
        "full_backup_id",
        "root_files",
        "crontabs",
        "root_managed_files",
        "apache_security",
    }
)
_CRONTAB_INVENTORY_KEYS = frozenset(
    {"install_user", "system_crontab", "user_crontabs"}
)


def parse_recovery_surface_receipt(
    payload: bytes,
    *,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
) -> RecoverySurfaceReceipt:
    """Parst nur kanonische Bytes mit drei vom Aufrufer bestätigten Bindungen."""

    expected_transaction = _strict_sha256_id(
        expected_transaction_id,
        label="Erwartete Transaktions-ID",
    )
    expected_root = _canonical_install_root(expected_install_root)
    expected_backup = _strict_sha256_id(
        expected_full_backup_id,
        label="Erwartete Vollbackup-ID",
    )
    if not isinstance(payload, bytes):
        raise ValueError("Recovery-Nebenflächen-Receipt muss als Bytes vorliegen")
    if not payload or len(payload) > MAX_RECOVERY_SURFACE_RECEIPT_BYTES:
        raise ValueError("Recovery-Nebenflächen-Receipt besitzt eine ungültige Größe")
    try:
        decoded = payload.decode("utf-8")
        raw = json.loads(
            decoded,
            object_pairs_hook=_unique_json_object,
            parse_float=_reject_json_noninteger,
            parse_constant=_reject_json_noninteger,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Recovery-Nebenflächen-Receipt ist kein striktes JSON") from exc
    mapping = _exact_mapping(raw, keys=_RECEIPT_KEYS, label="Recovery-Receipt")
    if (
        mapping["schema"] != RECOVERY_SURFACE_RECEIPT_SCHEMA
        or mapping["state"] != "complete"
    ):
        raise ValueError("Recovery-Nebenflächen-Receipt besitzt Schema oder Status nicht")
    transaction_id = _strict_sha256_id(
        mapping["transaction_id"],
        label="Transaktions-ID",
    )
    install_root = _canonical_install_root(mapping["install_root"])
    full_backup_id = _strict_sha256_id(
        mapping["full_backup_id"],
        label="Vollbackup-ID",
    )
    if (
        transaction_id != expected_transaction
        or install_root != expected_root
        or full_backup_id != expected_backup
    ):
        raise ValueError("Recovery-Nebenflächen-Receipt gehört nicht zur erwarteten Transaktion")

    raw_root_files = mapping["root_files"]
    if not isinstance(raw_root_files, list) or len(raw_root_files) != len(ROOT_FILE_SPECS):
        raise ValueError("Root-Dateiinventar besitzt keine vollständige Dateiliste")
    root_files = RootFileInventory(
        tuple(
            _root_preimage_from_mapping(raw_item, spec)
            for raw_item, spec in zip(raw_root_files, ROOT_FILE_SPECS)
        )
    )

    raw_crontabs = _exact_mapping(
        mapping["crontabs"],
        keys=_CRONTAB_INVENTORY_KEYS,
        label="Crontab-Inventar",
    )
    install_user = raw_crontabs["install_user"]
    if not isinstance(install_user, str):
        raise ValueError("Crontab-Inventar besitzt keinen Installationsbenutzer")
    users = _normalize_crontab_users(install_user)
    raw_user_crontabs = raw_crontabs["user_crontabs"]
    if not isinstance(raw_user_crontabs, list) or len(raw_user_crontabs) != len(users):
        raise ValueError("Crontab-Inventar besitzt keine vollständige Nutzerliste")
    crontabs = CrontabInventory(
        install_user=install_user,
        system_crontab=_root_preimage_from_mapping(
            raw_crontabs["system_crontab"],
            _SYSTEM_CRONTAB_SPEC,
        ),
        user_crontabs=tuple(
            _user_crontab_from_mapping(raw_item, expected_user=user)
            for raw_item, user in zip(raw_user_crontabs, users)
        ),
    )
    raw_root_managed = mapping["root_managed_files"]
    if (
        not isinstance(raw_root_managed, list)
        or len(raw_root_managed) > MAX_ROOT_MANAGED_FILE_ENTRIES
    ):
        raise ValueError("Root-Managed-Inventar besitzt keine gültige Dateiliste")
    root_managed_files = _validate_root_managed_files(
        tuple(
            _root_managed_preimage_from_mapping(raw_item)
            for raw_item in raw_root_managed
        )
    )
    apache_security = _apache_security_preimage_from_mapping(
        mapping["apache_security"]
    )
    receipt = create_recovery_surface_receipt(
        transaction_id=transaction_id,
        install_root=install_root,
        full_backup_id=full_backup_id,
        root_files=root_files,
        crontabs=crontabs,
        root_managed_files=root_managed_files,
        apache_security=apache_security,
    )
    if serialize_recovery_surface_receipt(receipt) != payload:
        raise ValueError("Recovery-Nebenflächen-Receipt ist nicht kanonisch serialisiert")
    return receipt


def _assert_no_recovery_receipt_xattrs(path: str) -> None:
    try:
        names = tuple(os.listxattr(path, follow_symlinks=False))
    except AttributeError as exc:
        raise RuntimeError("xattr-Prüfung wird vom Betriebssystem nicht unterstützt") from exc
    except OSError as exc:
        if exc.errno in {
            getattr(errno, "ENODATA", -1),
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            return
        raise
    if names:
        raise RuntimeError("Recovery-Nebenflächen-Receipt besitzt ACL/xattr")


def _persisted_recovery_surface_binding(
    receipt: RecoverySurfaceReceipt,
    path: str,
    snapshot: Mapping[str, object],
) -> PersistedRecoverySurfaceReceipt:
    identity = tuple(snapshot.get("identity") or ())
    payload = snapshot.get("payload")
    canonical_payload = serialize_recovery_surface_receipt(receipt)
    if (
        snapshot.get("path") != path
        or snapshot.get("kind") != "regular"
        or not isinstance(payload, bytes)
        or payload != canonical_payload
        or len(identity) != 9
        or identity[2] != 0
        or identity[3] != 0
        or identity[4] != 0o600
        or identity[5] != 1
        or identity[6] != len(payload)
        or snapshot.get("uid") != 0
        or snapshot.get("gid") != 0
        or snapshot.get("mode") != 0o600
        or snapshot.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise ValueError("Persistiertes Recovery-Nebenflächen-Receipt verletzt root:root-0600")
    return PersistedRecoverySurfaceReceipt(
        receipt=receipt,
        path=path,
        dev=int(identity[0]),
        ino=int(identity[1]),
        sha256=str(snapshot["sha256"]),
        identity=identity,
    )


def write_recovery_surface_receipt(
    receipt: RecoverySurfaceReceipt,
    *,
    receipt_path: str = RECOVERY_SURFACE_RECEIPT_PATH,
) -> PersistedRecoverySurfaceReceipt:
    """Persistiert einen neuen vollständigen Root-Receipt atomar und nofollow."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-RECEIPT-001",
            receipt_path,
            "Das persistente Recovery-Nebenflächen-Receipt darf ausschließlich Root schreiben.",
            "starte denselben Updatebefehl mit sudo; ändere die Receipt-Rechte nicht manuell",
        )
    path = str(receipt_path or "")
    payload = serialize_recovery_surface_receipt(receipt)
    try:
        _assert_root_controlled_parent_chain(path)
        before = snapshot_bound_file(
            path,
            allow_missing=True,
            expected_uid=0,
            expected_gid=0,
            max_bytes=MAX_RECOVERY_SURFACE_RECEIPT_BYTES,
        )
        if before.get("exists"):
            raise RuntimeError("Am Receipt-Pfad liegt bereits ein gebundener Zustand")
        installed = atomic_write_bound_file(
            path,
            payload,
            uid=0,
            gid=0,
            mode=0o600,
            expected_snapshot=before,
            max_existing_bytes=MAX_RECOVERY_SURFACE_RECEIPT_BYTES,
        )
        binding = _persisted_recovery_surface_binding(receipt, path, installed)
        rebound = read_recovery_surface_receipt(
            receipt_path=path,
            expected_transaction_id=receipt.transaction_id,
            expected_install_root=receipt.install_root,
            expected_full_backup_id=receipt.full_backup_id,
        )
        if rebound != binding:
            raise RuntimeError("Receipt-Readback wich vom atomaren Commit ab")
        return binding
    except UpdateRecoverySurfaceError:
        raise
    except Exception as exc:
        # Ein Fehler nach os.replace/fsync darf einen bereits durable gebundenen
        # Receipt nicht als fehlgeschlagen verlieren. Nur exakt derselbe Vertrag
        # wird einmal nofollow rückgebunden und als Commit akzeptiert.
        try:
            rebound = read_recovery_surface_receipt(
                receipt_path=path,
                expected_transaction_id=receipt.transaction_id,
                expected_install_root=receipt.install_root,
                expected_full_backup_id=receipt.full_backup_id,
            )
        except Exception:
            rebound = None
        if rebound is not None:
            return rebound
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-RECEIPT-002",
            path,
            "Recovery-Nebenflächen-Receipt konnte nicht dauerhaft geschrieben "
            f"werden: {exc}.",
            f"prüfe `sudo ls -ld {os.path.dirname(path)} {path}` und freien "
            "Speicherplatz; vorhandenes Receipt nicht löschen, sondern das "
            "Updatejournal auswerten",
        ) from exc


def read_recovery_surface_receipt(
    *,
    receipt_path: str = RECOVERY_SURFACE_RECEIPT_PATH,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
) -> PersistedRecoverySurfaceReceipt:
    """Bindet das durable Root-Receipt nach Prozessende beziehungsweise Reboot neu."""

    path = str(receipt_path or "")
    try:
        _assert_root_controlled_parent_chain(path)
        snapshot = snapshot_bound_file(
            path,
            allow_missing=False,
            expected_uid=0,
            expected_gid=0,
            max_bytes=MAX_RECOVERY_SURFACE_RECEIPT_BYTES,
        )
        if snapshot.get("mode") != 0o600:
            raise RuntimeError("Receipt-Modus ist nicht 0600")
        _assert_no_recovery_receipt_xattrs(path)
        rebound = snapshot_bound_file(
            path,
            allow_missing=False,
            expected_uid=0,
            expected_gid=0,
            max_bytes=MAX_RECOVERY_SURFACE_RECEIPT_BYTES,
        )
        if not snapshots_match(snapshot, rebound, exact_metadata=True):
            raise RuntimeError("Receipt driftete während der xattr-Prüfung")
        receipt = parse_recovery_surface_receipt(
            bytes(snapshot["payload"]),
            expected_transaction_id=expected_transaction_id,
            expected_install_root=expected_install_root,
            expected_full_backup_id=expected_full_backup_id,
        )
        return _persisted_recovery_surface_binding(receipt, path, snapshot)
    except UpdateRecoverySurfaceError:
        raise
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-RECEIPT-003",
            path,
            "Persistiertes Recovery-Nebenflächen-Receipt ist nicht exakt lesbar: "
            f"{exc}.",
            f"Receipt nicht verändern; prüfe `sudo stat {path}` und verwende "
            "die im Updatejournal gebundene Transaktions-/Backup-ID",
        ) from exc


def remove_recovery_surface_receipt(
    binding: PersistedRecoverySurfaceReceipt,
) -> None:
    """Entfernt ausschließlich den vollständig rückgebundenen Receipt-Inode."""

    if not isinstance(binding, PersistedRecoverySurfaceReceipt):
        raise ValueError("Recovery-Nebenflächen-Receipt-Bindung besitzt den falschen Typ")
    current = read_recovery_surface_receipt(
        receipt_path=binding.path,
        expected_transaction_id=binding.receipt.transaction_id,
        expected_install_root=binding.receipt.install_root,
        expected_full_backup_id=binding.receipt.full_backup_id,
    )
    if current != binding:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-RECEIPT-004",
            binding.path,
            "Receipt-Inode oder Hash driftete vor dem Entfernen.",
            "Receipt nicht manuell löschen; das Updatejournal und den unerwarteten Inode prüfen",
        )
    snapshot = snapshot_bound_file(
        binding.path,
        allow_missing=False,
        expected_uid=0,
        expected_gid=0,
        max_bytes=MAX_RECOVERY_SURFACE_RECEIPT_BYTES,
    )
    if (
        tuple(snapshot.get("identity") or ()) != binding.identity
        or snapshot.get("sha256") != binding.sha256
    ):
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-RECEIPT-004",
            binding.path,
            "Receipt driftete zwischen Readback und Entfernen.",
            "Receipt nicht manuell löschen; den gebundenen Inode und das Updatejournal prüfen",
        )
    remove_bound_file(snapshot, max_bytes=MAX_RECOVERY_SURFACE_RECEIPT_BYTES)


def capture_crontab_restore_guard(
    inventory: CrontabInventory,
    *,
    _runner: CrontabRunner | None = None,
) -> CrontabRestoreGuard:
    """Bindet alle aktuellen Cronzustände vor der ersten Restore-Mutation."""

    users = _validate_crontab_inventory(inventory)
    runner = _runner or _default_crontab_runner
    current_system = _capture_root_file(_SYSTEM_CRONTAB_SPEC)
    if (
        current_system.parent_identity[:2]
        != inventory.system_crontab.parent_identity[:2]
    ):
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-CRON-004",
            _SYSTEM_CRONTAB_SPEC.path,
            "Elternverzeichnis des System-Crontabs driftete.",
            "Cron nicht manuell verändern; `/etc` und das Updatejournal prüfen",
        )
    current_users = tuple(_capture_user_crontab(user, runner) for user in users)
    return CrontabRestoreGuard(current_system, current_users)


def _same_user_crontab(
    left: UserCrontabPreimage,
    right: UserCrontabPreimage,
) -> bool:
    return bool(
        left.user == right.user
        and left.payload == right.payload
        and left.sha256 == right.sha256
    )


def _restore_user_crontab(
    previous: UserCrontabPreimage,
    current: UserCrontabPreimage,
    runner: CrontabRunner,
) -> None:
    if _same_user_crontab(previous, current):
        return
    _assert_trusted_crontab_binary()
    argv = [CRONTAB_BINARY, "-u", previous.user]
    input_bytes = None
    if previous.payload is None:
        argv.append("-r")
    else:
        argv.append("-")
        input_bytes = previous.payload
    result = runner(tuple(argv), input_bytes=input_bytes)
    if int(result.returncode) != 0 or bytes(result.stderr or b""):
        detail = bytes(result.stderr or result.stdout or b"")[:800].decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(
            f"crontab-Restore scheiterte mit Exit {int(result.returncode)}: "
            f"{detail or 'keine Diagnose'}"
        )


def restore_crontab_preimages(
    inventory: CrontabInventory,
    guard: CrontabRestoreGuard,
    *,
    _runner: CrontabRunner | None = None,
) -> None:
    """Restauriert alle Cronbytes nur gegen einen vollständigen frischen Guard."""

    users = _validate_crontab_inventory(inventory)
    if not isinstance(guard, CrontabRestoreGuard):
        raise ValueError("Crontab-Restore-Guard besitzt den falschen Typ")
    _validate_user_crontabs(guard.user_crontabs, users)
    guard_system = _preimage_from_snapshot(
        _SYSTEM_CRONTAB_SPEC,
        guard.system_crontab.as_bound_snapshot(),
    )
    if guard_system != guard.system_crontab:
        raise ValueError("System-Crontab-Guard ist inkonsistent")
    runner = _runner or _default_crontab_runner

    live_system = _capture_root_file(_SYSTEM_CRONTAB_SPEC)
    live_users = tuple(_capture_user_crontab(user, runner) for user in users)
    if not snapshots_match(
        live_system.as_bound_snapshot(),
        guard.system_crontab.as_bound_snapshot(),
        exact_metadata=True,
    ):
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-CRON-005",
            _SYSTEM_CRONTAB_SPEC.path,
            "System-Crontab driftete nach Erzeugung des Restore-Guards.",
            "Cron unverändert lassen und das Updatejournal prüfen",
        )
    for actual, expected in zip(live_users, guard.user_crontabs):
        if not _same_user_crontab(actual, expected):
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-CRON-006",
                actual.user,
                "Nutzer-Crontab driftete nach Erzeugung des Restore-Guards.",
                f"prüfe `sudo {CRONTAB_BINARY} -u {actual.user} -l` und das Updatejournal",
            )

    failures: list[str] = []
    for previous, current in reversed(
        tuple(zip(inventory.user_crontabs, guard.user_crontabs))
    ):
        try:
            _restore_user_crontab(previous, current, runner)
        except Exception as exc:
            failures.append(f"{previous.user}: {exc}")

    root_inventory = RootFileInventory((inventory.system_crontab,))
    root_guard = RootFileRestoreGuard((guard.system_crontab,))
    try:
        _restore_root_inventory(
            root_inventory,
            root_guard,
            (_SYSTEM_CRONTAB_SPEC,),
        )
    except Exception as exc:
        failures.append(f"{_SYSTEM_CRONTAB_SPEC.path}: {exc}")

    for previous in inventory.user_crontabs:
        try:
            restored = _capture_user_crontab(previous.user, runner)
            if not _same_user_crontab(restored, previous):
                raise RuntimeError(
                    "Nutzer-Crontab-Endzustand weicht vom Preimage ab"
                )
        except Exception as exc:
            failures.append(f"{previous.user} (Endprüfung): {exc}")
    try:
        restored_system = _capture_root_file(_SYSTEM_CRONTAB_SPEC)
        if not _restored_semantics_match(
            restored_system,
            inventory.system_crontab,
        ):
            raise RuntimeError("System-Crontab-Endzustand weicht vom Preimage ab")
    except Exception as exc:
        failures.append(f"{_SYSTEM_CRONTAB_SPEC.path} (Endprüfung): {exc}")

    if failures:
        detail = "; ".join(failures)[:2400]
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-CRON-007",
            "System- und Nutzer-Crontabs",
            f"Cron-Preimages konnten nicht vollständig restauriert werden: {detail}.",
            "Dienste nicht manuell starten; Cronzustand und Updatejournal erhalten und die genannten Konten beziehungsweise Dateien gezielt reparieren",
        )


_SYSTEMD_LOAD_STATES = frozenset({"loaded", "not-found", "masked"})
_SYSTEMD_UNIT_FILE_RECOVERY_STATES = frozenset(
    {
        "",
        "not-found",
        "enabled",
        "enabled-runtime",
        "disabled",
        "static",
        "indirect",
        "masked",
        "masked-runtime",
    }
)
_SYSTEMD_PRE_ACTIVE_STATES = frozenset({"active", "inactive", "failed"})
_SYSTEMD_UNIT_RE = re.compile(r"[A-Za-z0-9_.@-]+(?:\.service)?\Z")


def _systemd_directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        int(metadata.st_gid),
        stat.S_IMODE(metadata.st_mode),
    )


def _systemd_file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_uid),
        int(metadata.st_gid),
        stat.S_IMODE(metadata.st_mode),
        int(metadata.st_nlink),
        int(metadata.st_size),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _systemd_surface_solution(path: str) -> str:
    return (
        f"prüfe `sudo ls -ld {os.path.dirname(path)} {path}` und "
        f"`sudo getfacl -p {path}`. Entferne keine fremden Inhalte blind; "
        "stelle für die genannte Unit ausschließlich reguläre, nicht "
        "gruppen-/weltbeschreibbare Dateien unter root-kontrollierten "
        "systemd-Verzeichnissen her und starte danach denselben Updatebefehl erneut"
    )


def _assert_no_systemd_xattrs(path: str) -> None:
    try:
        names = tuple(os.listxattr(path, follow_symlinks=False))
    except AttributeError as exc:
        raise RuntimeError("xattr-Prüfung wird vom Betriebssystem nicht unterstützt") from exc
    except OSError as exc:
        if exc.errno in {
            getattr(errno, "ENODATA", -1),
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            return
        raise
    if names:
        raise RuntimeError(
            "ACL/xattr kann nicht verlustfrei im systemd-Recovery-Receipt abgebildet werden"
        )


def _systemd_file_from_bound_snapshot(
    snapshot: Mapping[str, object],
) -> SystemdFilePreimage:
    path = str(snapshot.get("path") or "")
    parent_identity = tuple(snapshot.get("parent_identity") or ())
    if len(parent_identity) != 5:
        raise RuntimeError("systemd-Datei besitzt keine vollständige Elternbindung")
    if not snapshot.get("exists"):
        if snapshot.get("kind") != "missing":
            raise RuntimeError("Abwesende systemd-Datei besitzt einen falschen Typ")
        return SystemdFilePreimage(
            path, "absent", None, None, None, None, None, None,
            parent_identity, None,
        )
    if snapshot.get("kind") != "regular":
        raise RuntimeError("systemd-Datei ist nicht regulär")
    payload = snapshot.get("payload")
    identity = tuple(snapshot.get("identity") or ())
    if (
        not isinstance(payload, bytes)
        or len(payload) > MAX_SYSTEMD_UNIT_BYTES
        or len(identity) != 9
        or int(identity[5]) != 1
        or snapshot.get("sha256") != hashlib.sha256(payload).hexdigest()
        or bool(int(snapshot.get("mode") or 0) & 0o022)
    ):
        raise RuntimeError("systemd-Datei besitzt kein eindeutiges Byte-/Metadatenpreimage")
    return SystemdFilePreimage(
        path=path,
        kind="regular",
        payload=payload,
        sha256=str(snapshot["sha256"]),
        link_target=None,
        uid=int(snapshot["uid"]),
        gid=int(snapshot["gid"]),
        mode=int(snapshot["mode"]),
        parent_identity=parent_identity,
        identity=identity,
    )


def _capture_systemd_file(
    path: str,
    *,
    allow_mask: bool,
    require_root_owner: bool,
) -> SystemdFilePreimage:
    target = str(path or "")
    if (
        not os.path.isabs(target)
        or os.path.normpath(target) != target
        or target == "/"
        or any(ord(char) < 32 or ord(char) == 127 for char in target)
    ):
        raise ValueError("systemd-Dateipfad ist nicht kanonisch absolut")
    try:
        _assert_root_controlled_parent_chain(target)
        parent_path = os.path.dirname(target)
        parent_descriptor, parent_identity = open_bound_directory(parent_path)
        try:
            try:
                before = os.stat(
                    os.path.basename(target),
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                snapshot = snapshot_bound_file(
                    target,
                    allow_missing=True,
                    max_bytes=MAX_SYSTEMD_UNIT_BYTES,
                )
                return _systemd_file_from_bound_snapshot(snapshot)
            if stat.S_ISLNK(before.st_mode):
                if not allow_mask:
                    raise RuntimeError("Drop-in ist ein unzulässiger Symlink")
                link_target = os.readlink(
                    os.path.basename(target),
                    dir_fd=parent_descriptor,
                )
                after = os.stat(
                    os.path.basename(target),
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if (
                    link_target != "/dev/null"
                    or _systemd_file_identity(before) != _systemd_file_identity(after)
                    or before.st_uid != 0
                    or before.st_gid != 0
                    or before.st_nlink != 1
                ):
                    raise RuntimeError(
                        "Nur eine unveränderte root:root-/dev/null-Maske ist zulässig"
                    )
                _assert_no_systemd_xattrs(target)
                final_named = os.stat(
                    os.path.basename(target),
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
                if _systemd_file_identity(final_named) != _systemd_file_identity(after):
                    raise RuntimeError("systemd-Maske driftete während der xattr-Prüfung")
                payload = link_target.encode("utf-8")
                return SystemdFilePreimage(
                    path=target,
                    kind="mask_symlink",
                    payload=None,
                    sha256=hashlib.sha256(payload).hexdigest(),
                    link_target=link_target,
                    uid=int(after.st_uid),
                    gid=int(after.st_gid),
                    mode=stat.S_IMODE(after.st_mode),
                    parent_identity=tuple(parent_identity),
                    identity=_systemd_file_identity(after),
                )
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError("Pfad ist weder reguläre Datei noch echte systemd-Maske")
        finally:
            os.close(parent_descriptor)
        snapshot = snapshot_bound_file(
            target,
            allow_missing=False,
            expected_uid=0 if require_root_owner else None,
            expected_gid=0 if require_root_owner else None,
            max_bytes=MAX_SYSTEMD_UNIT_BYTES,
        )
        captured = _systemd_file_from_bound_snapshot(snapshot)
        _assert_no_systemd_xattrs(target)
        rebound = snapshot_bound_file(
            target,
            allow_missing=False,
            expected_uid=0 if require_root_owner else None,
            expected_gid=0 if require_root_owner else None,
            max_bytes=MAX_SYSTEMD_UNIT_BYTES,
        )
        if not snapshots_match(snapshot, rebound, exact_metadata=True):
            raise RuntimeError("systemd-Datei driftete während der xattr-Prüfung")
        return captured
    except UpdateRecoverySurfaceError:
        raise
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-005",
            target,
            f"systemd-Datei konnte nicht opaque und nofollow gebunden werden: {exc}.",
            _systemd_surface_solution(target),
        ) from exc


def _capture_systemd_dropin_directory(
    path: str,
) -> SystemdDropinDirectoryPreimage:
    target = str(path or "")
    try:
        if (
            not os.path.isabs(target)
            or os.path.normpath(target) != target
            or not target.endswith(".service.d")
        ):
            raise ValueError("verwaltetes Drop-in-Verzeichnis ist nicht kanonisch")
        _assert_root_controlled_parent_chain(
            os.path.join(os.path.dirname(target), ".e3dc-systemd-parent-check")
        )
        parent_descriptor, parent_identity = open_bound_directory(os.path.dirname(target))
        try:
            try:
                named = os.stat(
                    os.path.basename(target),
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                return SystemdDropinDirectoryPreimage(
                    target, False, None, None, None, tuple(parent_identity), None, ()
                )
            if (
                not stat.S_ISDIR(named.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or named.st_uid != 0
                or named.st_gid != 0
                or stat.S_IMODE(named.st_mode) & 0o022
            ):
                raise RuntimeError("Drop-in-Verzeichnis ist nicht root-kontrolliert")
        finally:
            os.close(parent_descriptor)
        descriptor, identity = open_bound_directory(target)
        try:
            opened = os.fstat(descriptor)
            if _systemd_directory_identity(opened) != _systemd_directory_identity(named):
                raise RuntimeError("Drop-in-Verzeichnis wechselte beim Öffnen")
            if os.listxattr(descriptor):
                raise RuntimeError("Drop-in-Verzeichnis besitzt ACL/xattr")
            names = tuple(sorted(os.listdir(descriptor)))
            if len(names) > MAX_SYSTEMD_DROPIN_ENTRIES:
                raise RuntimeError("Drop-in-Verzeichnis besitzt zu viele Einträge")
            entries: list[SystemdFilePreimage] = []
            total = 0
            for name in names:
                if not name or name in {".", ".."} or os.sep in name:
                    raise RuntimeError("Drop-in-Verzeichnis besitzt ungültigen Namen")
                entry = _capture_systemd_file(
                    os.path.join(target, name),
                    allow_mask=False,
                    require_root_owner=True,
                )
                if entry.kind != "regular":
                    raise RuntimeError(f"Drop-in ist nicht regulär: {entry.path}")
                total += len(entry.payload or b"")
                if total > MAX_SYSTEMD_DROPIN_TOTAL_BYTES:
                    raise RuntimeError("Drop-in-Verzeichnis überschreitet das Bytelimit")
                entries.append(entry)
            if _systemd_directory_identity(os.fstat(descriptor)) != tuple(identity):
                raise RuntimeError("Drop-in-Verzeichnis driftete während des Scans")
            if tuple(sorted(os.listdir(descriptor))) != names:
                raise RuntimeError("Drop-in-Namensmenge driftete während des Scans")
            return SystemdDropinDirectoryPreimage(
                path=target,
                exists=True,
                uid=int(opened.st_uid),
                gid=int(opened.st_gid),
                mode=stat.S_IMODE(opened.st_mode),
                parent_identity=tuple(parent_identity),
                identity=tuple(identity),
                entries=tuple(entries),
            )
        finally:
            os.close(descriptor)
    except UpdateRecoverySurfaceError:
        raise
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-006",
            target,
            f"Drop-in-Verzeichnis ist nicht vollständig und opaque bindbar: {exc}.",
            _systemd_surface_solution(target),
        ) from exc


SystemdRunner = Callable[[Sequence[str]], subprocess.CompletedProcess[bytes]]
ClosedGateVerifier = Callable[[str, tuple[str, ...]], bool]
StartAuthorizer = Callable[[str, tuple[str, ...]], bool]


def _default_systemd_runner(argv: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    return subprocess.run(
        tuple(str(item) for item in argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
        check=False,
        shell=False,
        env={**os.environ, "LC_ALL": "C", "LANG": "C"},
    )


def _run_systemd(
    runner: SystemdRunner,
    argv: Sequence[str],
    *,
    require_success: bool,
) -> subprocess.CompletedProcess[bytes]:
    try:
        result = runner(tuple(str(item) for item in argv))
    except Exception as exc:
        raise RuntimeError(f"systemd-Kommando konnte nicht ausgeführt werden: {exc}") from exc
    if not isinstance(result, subprocess.CompletedProcess):
        raise RuntimeError("systemd-Runner lieferte keinen CompletedProcess")
    stderr = bytes(result.stderr or b"")
    if require_success and (int(result.returncode) != 0 or stderr):
        detail = (stderr or bytes(result.stdout or b""))[:800].decode(
            "utf-8", errors="replace"
        ).strip()
        raise RuntimeError(
            f"systemd-Kommando scheiterte mit Exit {int(result.returncode)}: "
            f"{detail or 'keine Diagnose'}"
        )
    return result


def _capture_systemd_show(
    unit: str,
    runner: SystemdRunner,
) -> dict[str, object]:
    properties = (
        "LoadState",
        "UnitFileState",
        "ActiveState",
        "FragmentPath",
        "DropInPaths",
    )
    argv = [SYSTEMD_CONTROL_BINARY, "show", "--no-pager"]
    argv.extend(f"--property={name}" for name in properties)
    argv.extend(("--", unit))
    result = _run_systemd(runner, argv, require_success=True)
    try:
        stdout = bytes(result.stdout or b"").decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError(f"systemctl show für {unit} ist nicht UTF-8") from exc
    values: dict[str, str] = {}
    for line in stdout.splitlines():
        key, separator, value = line.partition("=")
        if (
            separator != "="
            or key not in properties
            or key in values
            or key != key.strip()
            or "\x00" in value
        ):
            raise RuntimeError(f"systemctl show für {unit} ist widersprüchlich")
        values[key] = value
    if set(values) != set(properties):
        raise RuntimeError(f"systemctl show für {unit} ist unvollständig")
    load_state = values["LoadState"].strip().lower()
    unit_file_state = values["UnitFileState"].strip().lower()
    active_state = values["ActiveState"].strip().lower()
    fragment_path = values["FragmentPath"].strip()
    try:
        dropin_paths = tuple(shlex.split(values["DropInPaths"]))
    except ValueError as exc:
        raise RuntimeError(f"DropInPaths für {unit} sind nicht eindeutig") from exc
    if (
        load_state not in _SYSTEMD_LOAD_STATES
        or unit_file_state not in _SYSTEMD_UNIT_FILE_RECOVERY_STATES
        or active_state not in _SYSTEMD_PRE_ACTIVE_STATES
        or len(dropin_paths) != len(set(dropin_paths))
    ):
        raise RuntimeError(f"systemd-Zustand von {unit} ist nicht recoveryfähig")
    for path in (*dropin_paths, *((fragment_path,) if fragment_path else ())):
        if (
            not os.path.isabs(path)
            or os.path.normpath(path) != path
            or any(ord(char) < 32 or ord(char) == 127 for char in path)
        ):
            raise RuntimeError(f"systemd-Pfad von {unit} ist nicht kanonisch: {path}")
    return {
        "load_state": load_state,
        "unit_file_state": unit_file_state,
        "pre_active_state": active_state,
        "fragment_path": fragment_path,
        "dropin_paths": dropin_paths,
    }


def _normalize_systemd_units(service_names: Sequence[str]) -> tuple[str, ...]:
    result: list[str] = []
    for raw_name in service_names:
        name = str(raw_name or "").strip()
        if not _SYSTEMD_UNIT_RE.fullmatch(name):
            raise ValueError(f"Ungültiger systemd-Dienstname: {name!r}")
        unit = name if name.endswith(".service") else name + ".service"
        if unit in result:
            raise ValueError(f"Doppelte systemd-Unit im Recovery-Bundle: {unit}")
        result.append(unit)
    if not result:
        raise ValueError("Recovery-Bundle benötigt mindestens eine systemd-Unit")
    return tuple(result)


def _canonical_systemd_root(raw: object) -> str:
    value = _canonical_install_root(raw)
    if not value.endswith("/systemd/system"):
        raise ValueError("systemd-Unitroot endet nicht auf /systemd/system")
    return value


def _capture_systemd_unit_preimage(
    unit: str,
    *,
    unit_root: str,
    runner: SystemdRunner,
) -> SystemdUnitRecoveryPreimage:
    state = _capture_systemd_show(unit, runner)
    main_path = os.path.join(unit_root, unit)
    main = _capture_systemd_file(
        main_path,
        allow_mask=True,
        require_root_owner=False,
    )
    managed_path = os.path.join(unit_root, unit + ".d")
    managed = _capture_systemd_dropin_directory(managed_path)
    active_paths = tuple(state["dropin_paths"])
    managed_entries = {entry.path: entry for entry in managed.entries}
    for path in active_paths:
        if os.path.dirname(path) == managed_path and path not in managed_entries:
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-007",
                path,
                "Ein wirksames Drop-in fehlt im vollständig gebundenen Unitverzeichnis.",
                _systemd_surface_solution(path),
            )
    opaque_paths = tuple(
        path for path in active_paths if os.path.dirname(path) != managed_path
    )
    opaque = tuple(
        _capture_systemd_file(
            path,
            allow_mask=False,
            require_root_owner=True,
        )
        for path in opaque_paths
    )
    if any(item.kind != "regular" for item in opaque):
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-008",
            ", ".join(opaque_paths),
            "Ein fremdes wirksames Drop-in ist nicht als reguläres opaque Preimage gebunden.",
            "prüfe die genannten DropInPaths; ersetze Symlinks oder Sonderdateien nicht blind, sondern stelle deren ursprüngliche reguläre root-Datei wieder her",
        )

    load_state = str(state["load_state"])
    unit_file_state = str(state["unit_file_state"])
    fragment_path = str(state["fragment_path"])
    if main.kind == "regular":
        if load_state != "loaded" or fragment_path != main_path:
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-009",
                main_path,
                f"Hauptunit und wirksamer FragmentPath widersprechen sich ({fragment_path or 'leer'}).",
                f"prüfe `sudo systemctl cat {unit}` und `sudo systemctl show {unit} -p FragmentPath`; bereinige ausschließlich die doppelte oder fremde Unitquelle",
            )
    elif main.kind == "mask_symlink":
        if unit_file_state != "masked" or load_state != "masked":
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-010",
                main_path,
                "Die /dev/null-Maske stimmt nicht mit systemd UnitFileState/LoadState überein.",
                f"prüfe `sudo systemctl status {unit}` und `sudo systemctl is-enabled {unit}`; korrigiere die Maske anschließend mit systemctl, nicht per blindem rm",
            )
    elif load_state == "loaded":
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-011",
            fragment_path or main_path,
            "Die Zielunit wird aus einem fremden FragmentPath geladen, während die gebundene /etc-Hauptunit fehlt.",
            f"prüfe `sudo systemctl cat {unit}`. Verschiebe keine Vendor-Datei; kläre zuerst, welche Paket- oder lokale Unit diese E3DC-Instanz tatsächlich startet",
        )
    elif load_state == "not-found" and (fragment_path or active_paths):
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-012",
            unit,
            "Eine nicht vorhandene Unit besitzt dennoch Fragmente oder Drop-ins.",
            f"prüfe `sudo systemctl show {unit} -p LoadState -p FragmentPath -p DropInPaths` und führe nach gezielter Bereinigung `sudo systemctl daemon-reload` aus",
        )
    return SystemdUnitRecoveryPreimage(
        unit=unit,
        load_state=load_state,
        unit_file_state=unit_file_state,
        pre_active_state=str(state["pre_active_state"]),
        fragment_path=fragment_path,
        active_dropin_paths=active_paths,
        main_file=main,
        managed_dropins=managed,
        opaque_dropins=opaque,
    )


def create_systemd_recovery_receipt(
    *,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    unit_root: str,
    units: Sequence[SystemdUnitRecoveryPreimage],
) -> SystemdRecoveryReceipt:
    materialized = tuple(units)
    names = _normalize_systemd_units(tuple(item.unit for item in materialized))
    if names != tuple(item.unit for item in materialized):
        raise ValueError("systemd-Recovery-Units sind nicht kanonisch")
    receipt = SystemdRecoveryReceipt(
        transaction_id=_strict_sha256_id(transaction_id, label="Transaktions-ID"),
        install_root=_canonical_install_root(install_root),
        full_backup_id=_strict_sha256_id(full_backup_id, label="Vollbackup-ID"),
        unit_root=_canonical_systemd_root(unit_root),
        units=materialized,
    )
    # Der Mapping-Builder ist zugleich die vollständige semantische Validierung.
    _systemd_recovery_receipt_mapping(receipt)
    return receipt


def capture_systemd_recovery_receipt(
    service_names: Sequence[str],
    *,
    transaction_id: str,
    install_root: str,
    full_backup_id: str,
    unit_root: str = "/etc/systemd/system",
    _runner: SystemdRunner | None = None,
) -> SystemdRecoveryReceipt:
    """Bindet die vollständige Unitfläche vor jeder systemd-Mutation."""

    units = _normalize_systemd_units(service_names)
    root = _canonical_systemd_root(unit_root)
    runner = _runner or _default_systemd_runner
    try:
        captured = tuple(
            _capture_systemd_unit_preimage(unit, unit_root=root, runner=runner)
            for unit in units
        )
        return create_systemd_recovery_receipt(
            transaction_id=transaction_id,
            install_root=install_root,
            full_backup_id=full_backup_id,
            unit_root=root,
            units=captured,
        )
    except UpdateRecoverySurfaceError:
        raise
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-013",
            ", ".join(units),
            f"Persistierbares systemd-Preimage ist nicht vollständig: {exc}.",
            "prüfe für jede genannte Unit `systemctl show -p LoadState -p UnitFileState -p ActiveState -p FragmentPath -p DropInPaths`; behebe nur den dort konkret genannten lokalen Pfad",
        ) from exc


_SYSTEMD_FILE_KEYS = frozenset(
    {
        "path", "kind", "payload_b64", "sha256", "link_target", "uid", "gid",
        "mode", "parent_identity", "identity",
    }
)


def _systemd_file_mapping(value: SystemdFilePreimage) -> dict[str, object]:
    if not isinstance(value, SystemdFilePreimage):
        raise ValueError("systemd-Dateipreimage besitzt den falschen Typ")
    mapping = {
        "path": value.path,
        "kind": value.kind,
        "payload_b64": _payload_to_base64(value.payload),
        "sha256": value.sha256,
        "link_target": value.link_target,
        "uid": value.uid,
        "gid": value.gid,
        "mode": value.mode,
        "parent_identity": list(value.parent_identity),
        "identity": list(value.identity) if value.identity is not None else None,
    }
    if _systemd_file_from_mapping(mapping, expected_path=value.path) != value:
        raise ValueError("systemd-Dateipreimage verletzt den Persistenzvertrag")
    return mapping


def _systemd_file_from_mapping(
    raw: object,
    *,
    expected_path: str,
    require_root_owner: bool = False,
) -> SystemdFilePreimage:
    mapping = _exact_mapping(raw, keys=_SYSTEMD_FILE_KEYS, label="systemd-Dateipreimage")
    if mapping["path"] != expected_path or not os.path.isabs(expected_path) or os.path.normpath(expected_path) != expected_path:
        raise ValueError("systemd-Dateipreimage besitzt einen fremden Pfad")
    kind = mapping["kind"]
    if kind not in {"absent", "regular", "mask_symlink"}:
        raise ValueError("systemd-Dateipreimage besitzt einen fremden Typ")
    parent_identity = _identity_from_json(
        mapping["parent_identity"], label="systemd-Datei.parent_identity", expected_length=5
    )
    if parent_identity[2] != 0 or parent_identity[3] != 0 or parent_identity[4] & 0o022:
        raise ValueError("systemd-Dateiparent ist nicht root-kontrolliert")
    if kind == "absent":
        if any(mapping[key] is not None for key in ("payload_b64", "sha256", "link_target", "uid", "gid", "mode", "identity")):
            raise ValueError("Abwesende systemd-Datei besitzt Restmetadaten")
        return SystemdFilePreimage(expected_path, kind, None, None, None, None, None, None, parent_identity, None)
    identity = _identity_from_json(
        mapping["identity"], label="systemd-Datei.identity", expected_length=9
    )
    uid = _strict_integer(mapping["uid"], label="systemd-Datei.uid")
    gid = _strict_integer(mapping["gid"], label="systemd-Datei.gid")
    mode = _strict_integer(mapping["mode"], label="systemd-Datei.mode", maximum=0o7777)
    if identity[2:5] != (uid, gid, mode) or identity[5] != 1 or (require_root_owner and (uid != 0 or gid != 0)):
        raise ValueError("systemd-Dateimetadaten sind widersprüchlich")
    if kind == "regular":
        payload = _payload_from_base64(mapping["payload_b64"], label="systemd-Datei.payload_b64", max_bytes=MAX_SYSTEMD_UNIT_BYTES)
        digest = _strict_sha256_id(mapping["sha256"], label="systemd-Datei-SHA256")
        if payload is None or mapping["link_target"] is not None or digest != hashlib.sha256(payload).hexdigest() or identity[6] != len(payload) or mode & 0o022:
            raise ValueError("Reguläres systemd-Dateipreimage ist widersprüchlich")
        return SystemdFilePreimage(expected_path, kind, payload, digest, None, uid, gid, mode, parent_identity, identity)
    target = mapping["link_target"]
    digest = _strict_sha256_id(mapping["sha256"], label="systemd-Masken-SHA256")
    if mapping["payload_b64"] is not None or target != "/dev/null" or digest != hashlib.sha256(b"/dev/null").hexdigest() or uid != 0 or gid != 0:
        raise ValueError("systemd-Maske ist keine exakte root-/dev/null-Maske")
    return SystemdFilePreimage(expected_path, kind, None, digest, target, uid, gid, mode, parent_identity, identity)


_SYSTEMD_DROPIN_DIR_KEYS = frozenset(
    {"path", "exists", "uid", "gid", "mode", "parent_identity", "identity", "entries"}
)


def _systemd_dropin_directory_mapping(value: SystemdDropinDirectoryPreimage) -> dict[str, object]:
    if not isinstance(value, SystemdDropinDirectoryPreimage):
        raise ValueError("systemd-Drop-in-Verzeichnis besitzt den falschen Typ")
    mapping = {
        "path": value.path,
        "exists": value.exists,
        "uid": value.uid,
        "gid": value.gid,
        "mode": value.mode,
        "parent_identity": list(value.parent_identity),
        "identity": list(value.identity) if value.identity is not None else None,
        "entries": [_systemd_file_mapping(entry) for entry in value.entries],
    }
    if _systemd_dropin_directory_from_mapping(mapping, expected_path=value.path) != value:
        raise ValueError("systemd-Drop-in-Verzeichnis verletzt den Persistenzvertrag")
    return mapping


def _systemd_dropin_directory_from_mapping(raw: object, *, expected_path: str) -> SystemdDropinDirectoryPreimage:
    mapping = _exact_mapping(raw, keys=_SYSTEMD_DROPIN_DIR_KEYS, label="systemd-Drop-in-Verzeichnis")
    if mapping["path"] != expected_path or not expected_path.endswith(".service.d"):
        raise ValueError("systemd-Drop-in-Verzeichnis besitzt einen fremden Pfad")
    if type(mapping["exists"]) is not bool:
        raise ValueError("systemd-Drop-in-Verzeichnis besitzt keinen booleschen Status")
    parent = _identity_from_json(mapping["parent_identity"], label="Drop-in.parent_identity", expected_length=5)
    if parent[2] != 0 or parent[3] != 0 or parent[4] & 0o022:
        raise ValueError("Drop-in-Parent ist nicht root-kontrolliert")
    entries_raw = mapping["entries"]
    if not isinstance(entries_raw, list) or len(entries_raw) > MAX_SYSTEMD_DROPIN_ENTRIES:
        raise ValueError("Drop-in-Dateiliste ist ungültig")
    if not mapping["exists"]:
        if entries_raw or any(mapping[key] is not None for key in ("uid", "gid", "mode", "identity")):
            raise ValueError("Abwesendes Drop-in-Verzeichnis besitzt Restmetadaten")
        return SystemdDropinDirectoryPreimage(expected_path, False, None, None, None, parent, None, ())
    uid = _strict_integer(mapping["uid"], label="Drop-in.uid")
    gid = _strict_integer(mapping["gid"], label="Drop-in.gid")
    mode = _strict_integer(mapping["mode"], label="Drop-in.mode", maximum=0o7777)
    identity = _identity_from_json(mapping["identity"], label="Drop-in.identity", expected_length=5)
    if uid != 0 or gid != 0 or mode & 0o022 or identity[2:5] != (uid, gid, mode):
        raise ValueError("Drop-in-Verzeichnismetadaten sind unsicher")
    entries: list[SystemdFilePreimage] = []
    total = 0
    for item in entries_raw:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError("Drop-in-Eintrag besitzt keinen Pfad")
        path = item["path"]
        if os.path.dirname(path) != expected_path:
            raise ValueError("Drop-in-Eintrag verlässt sein Verzeichnis")
        parsed = _systemd_file_from_mapping(item, expected_path=path, require_root_owner=True)
        if parsed.kind != "regular":
            raise ValueError("Drop-in-Eintrag ist nicht regulär")
        total += len(parsed.payload or b"")
        entries.append(parsed)
    if tuple(item.path for item in entries) != tuple(sorted(item.path for item in entries)) or len({item.path for item in entries}) != len(entries) or total > MAX_SYSTEMD_DROPIN_TOTAL_BYTES:
        raise ValueError("Drop-in-Einträge sind nicht kanonisch oder zu groß")
    return SystemdDropinDirectoryPreimage(expected_path, True, uid, gid, mode, parent, identity, tuple(entries))


_SYSTEMD_UNIT_KEYS = frozenset(
    {
        "unit", "load_state", "unit_file_state", "pre_active_state",
        "fragment_path", "active_dropin_paths", "main_file", "managed_dropins",
        "opaque_dropins",
    }
)


def _systemd_unit_mapping(value: SystemdUnitRecoveryPreimage, *, unit_root: str) -> dict[str, object]:
    if not isinstance(value, SystemdUnitRecoveryPreimage):
        raise ValueError("systemd-Unitpreimage besitzt den falschen Typ")
    mapping = {
        "unit": value.unit,
        "load_state": value.load_state,
        "unit_file_state": value.unit_file_state,
        "pre_active_state": value.pre_active_state,
        "fragment_path": value.fragment_path,
        "active_dropin_paths": list(value.active_dropin_paths),
        "main_file": _systemd_file_mapping(value.main_file),
        "managed_dropins": _systemd_dropin_directory_mapping(value.managed_dropins),
        "opaque_dropins": [_systemd_file_mapping(item) for item in value.opaque_dropins],
    }
    if _systemd_unit_from_mapping(mapping, expected_unit=value.unit, unit_root=unit_root) != value:
        raise ValueError("systemd-Unitpreimage verletzt den Persistenzvertrag")
    return mapping


def _systemd_unit_from_mapping(
    raw: object,
    *,
    expected_unit: str,
    unit_root: str,
) -> SystemdUnitRecoveryPreimage:
    mapping = _exact_mapping(raw, keys=_SYSTEMD_UNIT_KEYS, label="systemd-Unitpreimage")
    if mapping["unit"] != expected_unit or _normalize_systemd_units((expected_unit,)) != (expected_unit,):
        raise ValueError("systemd-Unitpreimage besitzt einen fremden Unitnamen")
    load_state = mapping["load_state"]
    unit_file_state = mapping["unit_file_state"]
    pre_active_state = mapping["pre_active_state"]
    fragment_path = mapping["fragment_path"]
    active_paths_raw = mapping["active_dropin_paths"]
    if (
        load_state not in _SYSTEMD_LOAD_STATES
        or unit_file_state not in _SYSTEMD_UNIT_FILE_RECOVERY_STATES
        or pre_active_state not in _SYSTEMD_PRE_ACTIVE_STATES
        or not isinstance(fragment_path, str)
        or not isinstance(active_paths_raw, list)
    ):
        raise ValueError("systemd-Unitzustand ist nicht recoveryfähig")
    active_paths: list[str] = []
    for path in active_paths_raw:
        if (
            not isinstance(path, str)
            or not os.path.isabs(path)
            or os.path.normpath(path) != path
            or path in active_paths
        ):
            raise ValueError("systemd-DropInPaths sind nicht kanonisch")
        active_paths.append(path)
    main_path = os.path.join(unit_root, expected_unit)
    managed_path = main_path + ".d"
    main = _systemd_file_from_mapping(mapping["main_file"], expected_path=main_path)
    managed = _systemd_dropin_directory_from_mapping(mapping["managed_dropins"], expected_path=managed_path)
    opaque_raw = mapping["opaque_dropins"]
    if not isinstance(opaque_raw, list):
        raise ValueError("Opaque Drop-ins sind keine Liste")
    expected_opaque_paths = tuple(path for path in active_paths if os.path.dirname(path) != managed_path)
    if len(opaque_raw) != len(expected_opaque_paths):
        raise ValueError("Opaque Drop-ins sind unvollständig")
    opaque = tuple(
        _systemd_file_from_mapping(item, expected_path=path, require_root_owner=True)
        for item, path in zip(opaque_raw, expected_opaque_paths)
    )
    managed_paths = {item.path for item in managed.entries}
    if any(
        os.path.dirname(path) == managed_path and path not in managed_paths
        for path in active_paths
    ):
        raise ValueError("Wirksames verwaltetes Drop-in fehlt im vollständigen Verzeichnis")
    if main.kind == "regular" and (load_state != "loaded" or fragment_path != main_path):
        raise ValueError("Reguläre systemd-Hauptunit widerspricht FragmentPath")
    if main.kind == "mask_symlink" and (
        load_state != "masked" or unit_file_state != "masked"
    ):
        raise ValueError("systemd-Maske widerspricht dem Unitstatus")
    if main.kind == "absent" and load_state == "loaded":
        raise ValueError("Fremder FragmentPath ist nicht Teil der gebundenen Unitfläche")
    if load_state == "not-found" and (fragment_path or active_paths):
        raise ValueError("Nicht vorhandene Unit besitzt wirksame Fragmente")
    return SystemdUnitRecoveryPreimage(
        unit=expected_unit,
        load_state=str(load_state),
        unit_file_state=str(unit_file_state),
        pre_active_state=str(pre_active_state),
        fragment_path=fragment_path,
        active_dropin_paths=tuple(active_paths),
        main_file=main,
        managed_dropins=managed,
        opaque_dropins=opaque,
    )


_SYSTEMD_RECEIPT_KEYS = frozenset(
    {"schema", "state", "transaction_id", "install_root", "full_backup_id", "unit_root", "units"}
)


def _systemd_recovery_receipt_mapping(receipt: SystemdRecoveryReceipt) -> dict[str, object]:
    if not isinstance(receipt, SystemdRecoveryReceipt):
        raise ValueError("systemd-Recovery-Receipt besitzt den falschen Typ")
    transaction = _strict_sha256_id(receipt.transaction_id, label="Transaktions-ID")
    install_root = _canonical_install_root(receipt.install_root)
    backup_id = _strict_sha256_id(receipt.full_backup_id, label="Vollbackup-ID")
    unit_root = _canonical_systemd_root(receipt.unit_root)
    names = _normalize_systemd_units(tuple(item.unit for item in receipt.units))
    if names != tuple(item.unit for item in receipt.units):
        raise ValueError("systemd-Recovery-Receipt besitzt keine geordnete Unitmenge")
    return {
        "schema": SYSTEMD_RECOVERY_RECEIPT_SCHEMA,
        "state": "complete",
        "transaction_id": transaction,
        "install_root": install_root,
        "full_backup_id": backup_id,
        "unit_root": unit_root,
        "units": [_systemd_unit_mapping(item, unit_root=unit_root) for item in receipt.units],
    }


def _canonical_systemd_recovery_json(mapping: Mapping[str, object]) -> bytes:
    try:
        payload = (
            json.dumps(mapping, ensure_ascii=True, allow_nan=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("systemd-Recovery-Receipt ist nicht kanonisch JSON-kodierbar") from exc
    if not payload or len(payload) > MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES:
        raise ValueError("systemd-Recovery-Receipt überschreitet das Größenlimit")
    return payload


def serialize_systemd_recovery_receipt(receipt: SystemdRecoveryReceipt) -> bytes:
    return _canonical_systemd_recovery_json(_systemd_recovery_receipt_mapping(receipt))


def parse_systemd_recovery_receipt(
    payload: bytes,
    *,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
    expected_units: Sequence[str],
    expected_unit_root: str = "/etc/systemd/system",
) -> SystemdRecoveryReceipt:
    transaction = _strict_sha256_id(expected_transaction_id, label="Erwartete Transaktions-ID")
    install_root = _canonical_install_root(expected_install_root)
    backup_id = _strict_sha256_id(expected_full_backup_id, label="Erwartete Vollbackup-ID")
    units = _normalize_systemd_units(expected_units)
    unit_root = _canonical_systemd_root(expected_unit_root)
    if not isinstance(payload, bytes) or not payload or len(payload) > MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES:
        raise ValueError("systemd-Recovery-Receipt besitzt eine ungültige Größe")
    try:
        raw = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_unique_json_object,
            parse_float=_reject_json_noninteger,
            parse_constant=_reject_json_noninteger,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("systemd-Recovery-Receipt ist kein striktes JSON") from exc
    mapping = _exact_mapping(raw, keys=_SYSTEMD_RECEIPT_KEYS, label="systemd-Recovery-Receipt")
    if mapping["schema"] != SYSTEMD_RECOVERY_RECEIPT_SCHEMA or mapping["state"] != "complete":
        raise ValueError("systemd-Recovery-Receipt besitzt Schema oder Status nicht")
    if (
        mapping["transaction_id"] != transaction
        or mapping["install_root"] != install_root
        or mapping["full_backup_id"] != backup_id
        or mapping["unit_root"] != unit_root
    ):
        raise ValueError("systemd-Recovery-Receipt gehört nicht zur erwarteten Transaktion")
    raw_units = mapping["units"]
    if not isinstance(raw_units, list) or len(raw_units) != len(units):
        raise ValueError("systemd-Recovery-Receipt besitzt keine vollständige Unitmenge")
    parsed_units = tuple(
        _systemd_unit_from_mapping(item, expected_unit=unit, unit_root=unit_root)
        for item, unit in zip(raw_units, units)
    )
    receipt = SystemdRecoveryReceipt(transaction, install_root, backup_id, unit_root, parsed_units)
    if serialize_systemd_recovery_receipt(receipt) != payload:
        raise ValueError("systemd-Recovery-Receipt ist nicht kanonisch serialisiert")
    return receipt


def _persisted_systemd_binding(
    receipt: SystemdRecoveryReceipt,
    path: str,
    snapshot: Mapping[str, object],
) -> PersistedSystemdRecoveryReceipt:
    identity = tuple(snapshot.get("identity") or ())
    payload = snapshot.get("payload")
    if (
        snapshot.get("kind") != "regular"
        or not isinstance(payload, bytes)
        or len(identity) != 9
        or identity[2] != 0
        or identity[3] != 0
        or identity[4] != 0o600
        or identity[5] != 1
        or snapshot.get("sha256") != hashlib.sha256(payload).hexdigest()
    ):
        raise ValueError("Persistiertes systemd-Recovery-Receipt verletzt root:root-0600")
    return PersistedSystemdRecoveryReceipt(
        receipt=receipt,
        path=path,
        dev=int(identity[0]),
        ino=int(identity[1]),
        sha256=str(snapshot["sha256"]),
        identity=identity,
    )


def write_systemd_recovery_receipt(
    receipt: SystemdRecoveryReceipt,
    *,
    receipt_path: str = SYSTEMD_RECOVERY_RECEIPT_PATH,
) -> PersistedSystemdRecoveryReceipt:
    """Schreibt ein neues Root-Receipt atomar; vorhandene Namen werden nie übernommen."""

    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-014",
            receipt_path,
            "Das persistente systemd-Recovery-Receipt darf ausschließlich Root schreiben.",
            "starte denselben Updatebefehl mit sudo; ändere die Receipt-Rechte nicht manuell",
        )
    path = str(receipt_path or "")
    payload = serialize_systemd_recovery_receipt(receipt)
    try:
        _assert_root_controlled_parent_chain(path)
        before = snapshot_bound_file(path, allow_missing=True, expected_uid=0, expected_gid=0, max_bytes=MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES)
        if before.get("exists"):
            raise RuntimeError("Am Receipt-Pfad liegt bereits ein gebundener Zustand")
        installed = atomic_write_bound_file(
            path,
            payload,
            uid=0,
            gid=0,
            mode=0o600,
            expected_snapshot=before,
            max_existing_bytes=MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES,
        )
        binding = _persisted_systemd_binding(receipt, path, installed)
        rebound = read_systemd_recovery_receipt(
            receipt_path=path,
            expected_transaction_id=receipt.transaction_id,
            expected_install_root=receipt.install_root,
            expected_full_backup_id=receipt.full_backup_id,
            expected_units=tuple(item.unit for item in receipt.units),
            expected_unit_root=receipt.unit_root,
        )
        if rebound != binding:
            raise RuntimeError("Receipt-Readback wich vom atomaren Commit ab")
        return binding
    except UpdateRecoverySurfaceError:
        raise
    except Exception as exc:
        # Ein Fehler nach dem atomaren Namensersatz darf den bereits durable
        # gebundenen Receipt-Inode nicht in einen vermeintlich fehlenden Stand
        # zurückstufen. Exakt derselbe Receipt-Vertrag wird deshalb einmal
        # nofollow rückgebunden und gilt dann als erfolgreicher Commit.
        try:
            rebound = read_systemd_recovery_receipt(
                receipt_path=path,
                expected_transaction_id=receipt.transaction_id,
                expected_install_root=receipt.install_root,
                expected_full_backup_id=receipt.full_backup_id,
                expected_units=tuple(item.unit for item in receipt.units),
                expected_unit_root=receipt.unit_root,
            )
        except Exception:
            rebound = None
        if rebound is not None:
            return rebound
        if isinstance(exc, UpdateRecoverySurfaceError):
            raise
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-015",
            path,
            f"systemd-Recovery-Receipt konnte nicht dauerhaft geschrieben werden: {exc}.",
            f"prüfe `sudo ls -ld {os.path.dirname(path)} {path}` und freien Speicherplatz; vorhandenes Receipt nicht löschen, sondern das Updatejournal auswerten",
        ) from exc


def read_systemd_recovery_receipt(
    *,
    receipt_path: str = SYSTEMD_RECOVERY_RECEIPT_PATH,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
    expected_units: Sequence[str],
    expected_unit_root: str = "/etc/systemd/system",
) -> PersistedSystemdRecoveryReceipt:
    path = str(receipt_path or "")
    try:
        _assert_root_controlled_parent_chain(path)
        snapshot = snapshot_bound_file(path, allow_missing=False, expected_uid=0, expected_gid=0, max_bytes=MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES)
        if snapshot.get("mode") != 0o600:
            raise RuntimeError("Receipt-Modus ist nicht 0600")
        _assert_no_systemd_xattrs(path)
        rebound = snapshot_bound_file(
            path,
            allow_missing=False,
            expected_uid=0,
            expected_gid=0,
            max_bytes=MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES,
        )
        if not snapshots_match(snapshot, rebound, exact_metadata=True):
            raise RuntimeError("Receipt driftete während der xattr-Prüfung")
        receipt = parse_systemd_recovery_receipt(
            bytes(snapshot["payload"]),
            expected_transaction_id=expected_transaction_id,
            expected_install_root=expected_install_root,
            expected_full_backup_id=expected_full_backup_id,
            expected_units=expected_units,
            expected_unit_root=expected_unit_root,
        )
        return _persisted_systemd_binding(receipt, path, snapshot)
    except UpdateRecoverySurfaceError:
        raise
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-016",
            path,
            f"Persistiertes systemd-Recovery-Receipt ist nicht exakt lesbar: {exc}.",
            f"Receipt nicht verändern; prüfe `sudo stat {path}` und verwende die im Updatejournal gebundene Transaktions-/Backup-ID",
        ) from exc


def remove_systemd_recovery_receipt(binding: PersistedSystemdRecoveryReceipt) -> None:
    """Entfernt ausschließlich genau den nach außen gebundenen Receipt-Inode."""

    if not isinstance(binding, PersistedSystemdRecoveryReceipt):
        raise ValueError("systemd-Recovery-Receipt-Bindung besitzt den falschen Typ")
    current = read_systemd_recovery_receipt(
        receipt_path=binding.path,
        expected_transaction_id=binding.receipt.transaction_id,
        expected_install_root=binding.receipt.install_root,
        expected_full_backup_id=binding.receipt.full_backup_id,
        expected_units=tuple(item.unit for item in binding.receipt.units),
        expected_unit_root=binding.receipt.unit_root,
    )
    if current != binding:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-017",
            binding.path,
            "Receipt-Inode oder Hash driftete vor dem Entfernen.",
            "Receipt nicht manuell löschen; das Updatejournal und den unerwarteten Inode prüfen",
        )
    snapshot = snapshot_bound_file(binding.path, allow_missing=False, expected_uid=0, expected_gid=0, max_bytes=MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES)
    identity = tuple(snapshot.get("identity") or ())
    if (
        identity != binding.identity
        or snapshot.get("sha256") != binding.sha256
    ):
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-017",
            binding.path,
            "Receipt driftete zwischen Readback und Entfernen.",
            "Receipt nicht manuell löschen; den gebundenen Inode und das Updatejournal prüfen",
        )
    remove_bound_file(snapshot, max_bytes=MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES)


def _systemd_file_exact(left: SystemdFilePreimage, right: SystemdFilePreimage) -> bool:
    return left == right


def _systemd_file_semantics(left: SystemdFilePreimage, right: SystemdFilePreimage) -> bool:
    left_mtime = left.identity[7] if left.identity is not None else None
    right_mtime = right.identity[7] if right.identity is not None else None
    return bool(
        left.path == right.path
        and left.kind == right.kind
        and left.payload == right.payload
        and left.sha256 == right.sha256
        and left.link_target == right.link_target
        and left.uid == right.uid
        and left.gid == right.gid
        and left.mode == right.mode
        and left_mtime == right_mtime
    )


def _systemd_dropin_semantics(
    left: SystemdDropinDirectoryPreimage,
    right: SystemdDropinDirectoryPreimage,
) -> bool:
    return bool(
        left.path == right.path
        and left.exists == right.exists
        and left.uid == right.uid
        and left.gid == right.gid
        and left.mode == right.mode
        and len(left.entries) == len(right.entries)
        and all(
            _systemd_file_semantics(a, b)
            for a, b in zip(left.entries, right.entries)
        )
    )


def _systemd_unit_surface_semantics(
    expected: SystemdUnitRecoveryPreimage,
    actual: SystemdUnitRecoveryPreimage,
    *,
    require_pre_active: bool,
) -> bool:
    return bool(
        expected.unit == actual.unit
        and expected.load_state == actual.load_state
        and expected.unit_file_state == actual.unit_file_state
        and (not require_pre_active or expected.pre_active_state == actual.pre_active_state)
        and expected.fragment_path == actual.fragment_path
        and expected.active_dropin_paths == actual.active_dropin_paths
        and _systemd_file_semantics(expected.main_file, actual.main_file)
        and _systemd_dropin_semantics(expected.managed_dropins, actual.managed_dropins)
        and len(expected.opaque_dropins) == len(actual.opaque_dropins)
        and all(
            _systemd_file_semantics(a, b)
            for a, b in zip(expected.opaque_dropins, actual.opaque_dropins)
        )
    )


def capture_systemd_recovery_restore_guard(
    receipt: SystemdRecoveryReceipt,
    *,
    _runner: SystemdRunner | None = None,
) -> SystemdRecoveryRestoreGuard:
    """Bindet das aktuelle Postimage; aktive Units sind am Restore-Gate verboten."""

    _systemd_recovery_receipt_mapping(receipt)
    current = capture_systemd_recovery_receipt(
        tuple(item.unit for item in receipt.units),
        transaction_id=receipt.transaction_id,
        install_root=receipt.install_root,
        full_backup_id=receipt.full_backup_id,
        unit_root=receipt.unit_root,
        _runner=_runner,
    )
    for previous, actual in zip(receipt.units, current.units):
        if actual.pre_active_state == "active":
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-018",
                actual.unit,
                "Eine Unit läuft noch vor dem geschlossenen Datei-/Enablement-Restore.",
                f"Updater nicht fortsetzen; prüfe `sudo systemctl status {actual.unit}` und den persistenten Update-Bootblock",
            )
        if (
            previous.main_file.parent_identity[:2] != actual.main_file.parent_identity[:2]
            or previous.managed_dropins.parent_identity[:2]
            != actual.managed_dropins.parent_identity[:2]
            or tuple(item.path for item in previous.opaque_dropins)
            != tuple(item.path for item in actual.opaque_dropins)
        ):
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-019",
                actual.unit,
                "Unitparent oder opaque Fremd-Drop-in-Menge driftete seit dem Preimage.",
                f"Dienste gestoppt lassen; prüfe `sudo systemctl show {actual.unit} -p FragmentPath -p DropInPaths` und das Updatejournal",
            )
    return SystemdRecoveryRestoreGuard(current=current)


def _require_closed_systemd_gate(
    receipt: SystemdRecoveryReceipt,
    verifier: ClosedGateVerifier,
) -> None:
    if not callable(verifier) or verifier(
        receipt.transaction_id,
        tuple(item.unit for item in receipt.units),
    ) is not True:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-020",
            ", ".join(item.unit for item in receipt.units),
            "Der persistente Startsperren-Vertrag ist nicht geschlossen bestätigt.",
            "keine Unit manuell starten; prüfe den statischen Update-Bootblock und dessen transaktionsgebundene Drop-ins, danach denselben Recovery-Befehl erneut ausführen",
        )


def _capture_absent_systemd_file(path: str) -> SystemdFilePreimage:
    return _capture_systemd_file(path, allow_mask=True, require_root_owner=False)


def _unlink_exact_systemd_entry(current: SystemdFilePreimage) -> SystemdFilePreimage:
    live = _capture_systemd_file(
        current.path,
        allow_mask=True,
        require_root_owner=False,
    )
    if not _systemd_file_exact(live, current):
        raise RuntimeError(f"systemd-Eintrag driftete vor dem Entfernen: {current.path}")
    if current.kind == "absent":
        return current
    if current.kind == "regular":
        removed = remove_bound_file(
            current.as_bound_snapshot(),
            max_bytes=MAX_SYSTEMD_UNIT_BYTES,
        )
        return _systemd_file_from_bound_snapshot(removed)
    parent_descriptor, parent_identity = open_bound_directory(os.path.dirname(current.path))
    try:
        if tuple(parent_identity)[:2] != current.parent_identity[:2]:
            raise RuntimeError("Maskenparent driftete vor dem Entfernen")
        named = os.stat(os.path.basename(current.path), dir_fd=parent_descriptor, follow_symlinks=False)
        if _systemd_file_identity(named) != current.identity:
            raise RuntimeError("Maskeninode driftete vor dem Entfernen")
        os.unlink(os.path.basename(current.path), dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    finally:
        os.close(parent_descriptor)
    return _capture_absent_systemd_file(current.path)


def _set_systemd_entry_mtime(
    installed: SystemdFilePreimage,
    expected: SystemdFilePreimage,
) -> SystemdFilePreimage:
    if installed.kind == "absent" or expected.identity is None:
        return installed
    expected_mtime = int(expected.identity[7])
    parent_descriptor, parent_identity = open_bound_directory(os.path.dirname(installed.path))
    descriptor = -1
    try:
        if tuple(parent_identity)[:2] != installed.parent_identity[:2]:
            raise RuntimeError("systemd-Dateiparent driftete vor Zeitmetadaten-Restore")
        name = os.path.basename(installed.path)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if _systemd_file_identity(named) != installed.identity:
            raise RuntimeError("systemd-Datei driftete vor Zeitmetadaten-Restore")
        if installed.kind == "regular":
            descriptor = os.open(
                name,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            if _systemd_file_identity(os.fstat(descriptor)) != installed.identity:
                raise RuntimeError("systemd-Datei wechselte beim Öffnen")
            os.utime(descriptor, ns=(expected_mtime, expected_mtime))
            os.fsync(descriptor)
        else:
            os.utime(
                name,
                ns=(expected_mtime, expected_mtime),
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        os.fsync(parent_descriptor)
    finally:
        if descriptor >= 0:
            os.close(descriptor)
        os.close(parent_descriptor)
    return _capture_systemd_file(
        installed.path,
        allow_mask=True,
        require_root_owner=False,
    )


def _restore_systemd_file(
    previous: SystemdFilePreimage,
    current: SystemdFilePreimage,
) -> SystemdFilePreimage:
    live = _capture_systemd_file(
        current.path,
        allow_mask=True,
        require_root_owner=False,
    )
    if not _systemd_file_exact(live, current):
        raise RuntimeError(f"systemd-Datei driftete nach Restore-Guard: {current.path}")
    if _systemd_file_semantics(previous, current):
        return current
    if previous.kind == "absent":
        restored = _unlink_exact_systemd_entry(current)
    elif previous.kind == "regular":
        installed = atomic_write_bound_file(
            previous.path,
            bytes(previous.payload or b""),
            uid=int(previous.uid),
            gid=int(previous.gid),
            mode=int(previous.mode),
            expected_snapshot=current.as_bound_snapshot(),
            allow_existing_symlink=True,
            max_existing_bytes=MAX_SYSTEMD_UNIT_BYTES,
        )
        restored = _systemd_file_from_bound_snapshot(installed)
        restored = _set_systemd_entry_mtime(restored, previous)
    else:
        absent = _unlink_exact_systemd_entry(current)
        parent_descriptor, parent_identity = open_bound_directory(os.path.dirname(previous.path))
        try:
            if tuple(parent_identity)[:2] != absent.parent_identity[:2]:
                raise RuntimeError("Maskenparent driftete vor Wiederherstellung")
            name = os.path.basename(previous.path)
            os.symlink("/dev/null", name, dir_fd=parent_descriptor)
            os.chown(
                name,
                int(previous.uid),
                int(previous.gid),
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            os.fsync(parent_descriptor)
        finally:
            os.close(parent_descriptor)
        restored = _capture_systemd_file(previous.path, allow_mask=True, require_root_owner=False)
        restored = _set_systemd_entry_mtime(restored, previous)
    if not _systemd_file_semantics(previous, restored):
        raise RuntimeError(f"systemd-Datei weicht nach Restore vom Preimage ab: {previous.path}")
    return restored


def _restore_systemd_dropin_directory(
    previous: SystemdDropinDirectoryPreimage,
    current: SystemdDropinDirectoryPreimage,
    *,
    preserved_entries: Sequence[SystemdFilePreimage] = (),
    after_mutation: Callable[[], None] | None = None,
) -> SystemdDropinDirectoryPreimage:
    mutation_guard = after_mutation or (lambda: None)
    live = _capture_systemd_dropin_directory(current.path)
    if live != current:
        raise RuntimeError(f"Drop-in-Verzeichnis driftete nach Restore-Guard: {current.path}")
    preserved = tuple(preserved_entries)
    if preserved:
        current_by_path = {item.path: item for item in current.entries}
        previous_paths = {item.path for item in previous.entries}
        if (
            not current.exists
            or len({item.path for item in preserved}) != len(preserved)
            or any(
                item.path in previous_paths
                or os.path.dirname(item.path) != current.path
                or current_by_path.get(item.path) != item
                or item.kind != "regular"
                or item.uid != 0
                or item.gid != 0
                for item in preserved
            )
        ):
            raise RuntimeError("Erhaltene Startsperr-Drop-ins sind nicht exakt gebunden")
        base = previous if previous.exists else replace(
            current,
            entries=(),
        )
        desired = replace(
            base,
            entries=tuple(
                sorted(
                    (*previous.entries, *preserved),
                    key=lambda item: item.path,
                )
            ),
        )
    else:
        desired = previous

    if not desired.exists and current.exists:
        for item in reversed(current.entries):
            _unlink_exact_systemd_entry(item)
            mutation_guard()
        parent_descriptor, parent_identity = open_bound_directory(os.path.dirname(current.path))
        try:
            if tuple(parent_identity)[:2] != current.parent_identity[:2]:
                raise RuntimeError("Drop-in-Parent driftete vor rmdir")
            named = os.stat(os.path.basename(current.path), dir_fd=parent_descriptor, follow_symlinks=False)
            if _systemd_directory_identity(named) != current.identity:
                raise RuntimeError("Drop-in-Verzeichnis driftete vor rmdir")
            os.rmdir(os.path.basename(current.path), dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
            mutation_guard()
        finally:
            os.close(parent_descriptor)
        restored = _capture_systemd_dropin_directory(desired.path)
        if not _systemd_dropin_semantics(desired, restored):
            raise RuntimeError("Abwesendes Drop-in-Verzeichnis blieb nach Restore vorhanden")
        return restored

    if desired.exists and not current.exists:
        ensure_bound_directory(
            desired.path,
            uid=int(desired.uid),
            gid=int(desired.gid),
            mode=int(desired.mode),
            expected_parent_identity=current.parent_identity,
            expected_missing=True,
        )
        mutation_guard()
        current = _capture_systemd_dropin_directory(desired.path)

    if not desired.exists:
        return current
    previous_by_path = {item.path: item for item in desired.entries}
    current_by_path = {item.path: item for item in current.entries}
    for path in sorted(set(previous_by_path) | set(current_by_path)):
        before = current_by_path.get(path)
        desired = previous_by_path.get(path)
        if before is None:
            before = _capture_absent_systemd_file(path)
        if desired is None:
            _unlink_exact_systemd_entry(before)
        else:
            _restore_systemd_file(desired, before)
        mutation_guard()
    descriptor, _identity = open_bound_directory(desired.path)
    try:
        os.fchown(descriptor, int(desired.uid), int(desired.gid))
        os.fchmod(descriptor, int(desired.mode))
        os.fsync(descriptor)
        mutation_guard()
    finally:
        os.close(descriptor)
    restored = _capture_systemd_dropin_directory(desired.path)
    if not _systemd_dropin_semantics(desired, restored):
        raise RuntimeError(f"Drop-in-Verzeichnis weicht nach Restore ab: {desired.path}")
    return restored


def _restore_systemd_enablement(
    unit: str,
    state: str,
    runner: SystemdRunner,
) -> None:
    if state == "masked":
        argv = (SYSTEMD_CONTROL_BINARY, "mask", "--", unit)
        _run_systemd(runner, argv, require_success=True)
    elif state == "masked-runtime":
        argv = (SYSTEMD_CONTROL_BINARY, "mask", "--runtime", "--", unit)
        _run_systemd(runner, argv, require_success=True)
    elif state in {"enabled", "enabled-runtime", "disabled", "not-found", ""}:
        _run_systemd(
            runner,
            (SYSTEMD_CONTROL_BINARY, "unmask", "--", unit),
            require_success=False,
        )
        if state == "enabled":
            argv = (SYSTEMD_CONTROL_BINARY, "enable", "--", unit)
            _run_systemd(runner, argv, require_success=True)
        elif state == "enabled-runtime":
            argv = (SYSTEMD_CONTROL_BINARY, "enable", "--runtime", "--", unit)
            _run_systemd(runner, argv, require_success=True)
        else:
            argv = (SYSTEMD_CONTROL_BINARY, "disable", "--", unit)
            _run_systemd(runner, argv, require_success=False)
    elif state in {"static", "indirect"}:
        # Diese Zustände entstehen aus den Unitbytes; enable/disable darf sie
        # nicht künstlich in einen anderen Vertrag umschreiben.
        pass
    else:
        raise RuntimeError(f"UnitFileState ist nicht restaurierbar: {state}")
    rebound = _capture_systemd_show(unit, runner)
    if rebound["unit_file_state"] != state:
        raise RuntimeError(
            f"UnitFileState von {unit} blieb {rebound['unit_file_state']!r} statt {state!r}"
        )


def _validated_preserved_gate_dropins(
    receipt: SystemdRecoveryReceipt,
    guard: SystemdRecoveryRestoreGuard,
    values: Sequence[SystemdFilePreimage],
) -> tuple[SystemdFilePreimage, ...]:
    materialized = tuple(values)
    if len({item.path for item in materialized}) != len(materialized):
        raise ValueError("Startsperr-Drop-ins enthalten doppelte Pfade")
    ordered: list[SystemdFilePreimage] = []
    provided = {item.path: item for item in materialized}
    for previous, current in zip(receipt.units, guard.current.units):
        previous_paths = {item.path for item in previous.managed_dropins.entries}
        current_by_path = {item.path: item for item in current.managed_dropins.entries}
        for path in sorted(provided):
            if os.path.dirname(path) != current.managed_dropins.path:
                continue
            item = provided[path]
            parsed = _systemd_file_from_mapping(
                _systemd_file_mapping(item),
                expected_path=path,
                require_root_owner=True,
            )
            if (
                parsed.kind != "regular"
                or path in previous_paths
                or current_by_path.get(path) != parsed
                or path not in current.active_dropin_paths
            ):
                raise ValueError(
                    f"Startsperr-Drop-in ist nicht als zusätzliches wirksames Postimage gebunden: {path}"
                )
            ordered.append(parsed)
    if {item.path for item in ordered} != set(provided):
        raise ValueError("Startsperr-Drop-in gehört zu keiner gebundenen Unit")
    return tuple(ordered)


def _gate_dropins_for_unit(
    unit: SystemdUnitRecoveryPreimage,
    values: Sequence[SystemdFilePreimage],
) -> tuple[SystemdFilePreimage, ...]:
    return tuple(
        item
        for item in values
        if os.path.dirname(item.path) == unit.managed_dropins.path
    )


def _strip_preserved_gate_surface(
    expected: SystemdUnitRecoveryPreimage,
    actual: SystemdUnitRecoveryPreimage,
    gates: Sequence[SystemdFilePreimage],
) -> SystemdUnitRecoveryPreimage:
    gate_paths = {item.path for item in gates}
    current_paths = {item.path for item in actual.managed_dropins.entries}
    if not gate_paths.issubset(current_paths):
        raise ValueError("Startsperr-Drop-in fehlt im aktuellen Unitpostimage")
    remaining_entries = tuple(
        item for item in actual.managed_dropins.entries if item.path not in gate_paths
    )
    if expected.managed_dropins.exists:
        managed = replace(actual.managed_dropins, entries=remaining_entries)
    else:
        if remaining_entries:
            raise ValueError("Neben der Startsperre entstanden fremde Drop-ins")
        managed = expected.managed_dropins
    return replace(
        actual,
        active_dropin_paths=tuple(
            path for path in actual.active_dropin_paths if path not in gate_paths
        ),
        managed_dropins=managed,
    )


def restore_systemd_files_masks_enablement(
    receipt: SystemdRecoveryReceipt,
    guard: SystemdRecoveryRestoreGuard,
    *,
    closed_gate_verifier: ClosedGateVerifier,
    preserved_gate_dropins: Sequence[SystemdFilePreimage] = (),
    _runner: SystemdRunner | None = None,
) -> SystemdRecoveryStartPlan:
    """Restauriert Dateien, Masken und Enablement, startet aber keinen Dienst."""

    _systemd_recovery_receipt_mapping(receipt)
    if not isinstance(guard, SystemdRecoveryRestoreGuard):
        raise ValueError("systemd-Restore-Guard besitzt den falschen Typ")
    runner = _runner or _default_systemd_runner
    live_guard = capture_systemd_recovery_restore_guard(receipt, _runner=runner)
    if live_guard != guard:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-021",
            ", ".join(item.unit for item in receipt.units),
            "Unitfläche driftete nach Erzeugung des Restore-Guards.",
            "Dienste gestoppt lassen; keine Unitdatei manuell ersetzen und das Updatejournal prüfen",
        )
    try:
        preserved = _validated_preserved_gate_dropins(
            receipt,
            guard,
            preserved_gate_dropins,
        )
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-028",
            ", ".join(item.path for item in preserved_gate_dropins) or "keine",
            f"Startsperr-Drop-ins besitzen keinen exakten Postimagevertrag: {exc}.",
            "Startsperre nicht entfernen; die Drop-in-Inodes aus dem Update-Safety-Receipt erneut binden",
        ) from exc
    _require_closed_systemd_gate(receipt, closed_gate_verifier)
    failures: list[str] = []
    for previous, current in zip(receipt.units, guard.current.units):
        try:
            _require_closed_systemd_gate(receipt, closed_gate_verifier)
            _restore_systemd_file(previous.main_file, current.main_file)
            _require_closed_systemd_gate(receipt, closed_gate_verifier)
            _restore_systemd_dropin_directory(
                previous.managed_dropins,
                current.managed_dropins,
                preserved_entries=_gate_dropins_for_unit(current, preserved),
                after_mutation=lambda: _require_closed_systemd_gate(
                    receipt, closed_gate_verifier
                ),
            )
            _require_closed_systemd_gate(receipt, closed_gate_verifier)
            for wanted, actual in zip(previous.opaque_dropins, current.opaque_dropins):
                _restore_systemd_file(wanted, actual)
                _require_closed_systemd_gate(receipt, closed_gate_verifier)
        except Exception as exc:
            failures.append(f"{previous.unit}: {exc}")
    if failures:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-022",
            ", ".join(item.unit for item in receipt.units),
            "systemd-Dateien/Drop-ins konnten nicht vollständig restauriert werden: "
            + "; ".join(failures)[:2400]
            + ".",
            "Startsperre geschlossen lassen; die konkret genannten Pfade aus dem Receipt wiederherstellen und denselben Recovery-Befehl erneut ausführen",
        )
    _require_closed_systemd_gate(receipt, closed_gate_verifier)
    _run_systemd(
        runner,
        (SYSTEMD_CONTROL_BINARY, "daemon-reload"),
        require_success=True,
    )
    for previous in receipt.units:
        _require_closed_systemd_gate(receipt, closed_gate_verifier)
        _restore_systemd_enablement(previous.unit, previous.unit_file_state, runner)
        _require_closed_systemd_gate(receipt, closed_gate_verifier)
    _require_closed_systemd_gate(receipt, closed_gate_verifier)
    restored = capture_systemd_recovery_receipt(
        tuple(item.unit for item in receipt.units),
        transaction_id=receipt.transaction_id,
        install_root=receipt.install_root,
        full_backup_id=receipt.full_backup_id,
        unit_root=receipt.unit_root,
        _runner=runner,
    )
    for previous, actual in zip(receipt.units, restored.units):
        gates = _gate_dropins_for_unit(actual, preserved)
        projected = _strip_preserved_gate_surface(previous, actual, gates)
        if actual.pre_active_state == "active" or not _systemd_unit_surface_semantics(
            previous,
            projected,
            require_pre_active=False,
        ):
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-023",
                previous.unit,
                "Offline restaurierte Unitfläche oder Enablement weicht vom Preimage ab.",
                f"Startsperre geschlossen lassen; prüfe `sudo systemctl show {previous.unit} -p UnitFileState -p ActiveState -p FragmentPath -p DropInPaths`",
            )
    return SystemdRecoveryStartPlan(
        transaction_id=receipt.transaction_id,
        units=tuple(item.unit for item in receipt.units),
        preactive_units=tuple(
            item.unit for item in receipt.units if item.pre_active_state == "active"
        ),
        preserved_gate_dropins=preserved,
    )


def restore_systemd_pre_active_state(
    receipt: SystemdRecoveryReceipt,
    plan: SystemdRecoveryStartPlan,
    *,
    start_authorizer: StartAuthorizer,
    _runner: SystemdRunner | None = None,
) -> None:
    """Startet erst nach gesonderter Autorisierung exakt die zuvor aktiven Units."""

    units = tuple(item.unit for item in receipt.units)
    expected_preactive = tuple(
        item.unit for item in receipt.units if item.pre_active_state == "active"
    )
    if (
        not isinstance(plan, SystemdRecoveryStartPlan)
        or plan.transaction_id != receipt.transaction_id
        or plan.units != units
        or plan.preactive_units != expected_preactive
    ):
        raise ValueError("systemd-Startplan gehört nicht zum Recovery-Receipt")
    runner = _runner or _default_systemd_runner
    start_guard = capture_systemd_recovery_restore_guard(
        receipt,
        _runner=runner,
    )
    preserved = _validated_preserved_gate_dropins(
        receipt,
        start_guard,
        plan.preserved_gate_dropins,
    )
    for previous, actual in zip(receipt.units, start_guard.current.units):
        projected = _strip_preserved_gate_surface(
            previous,
            actual,
            _gate_dropins_for_unit(actual, preserved),
        )
        if not _systemd_unit_surface_semantics(
            previous,
            projected,
            require_pre_active=False,
        ):
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-026",
                previous.unit,
                "Unitfläche driftete zwischen Offline-Restore und Dienststart.",
                f"Dienst nicht starten; prüfe `sudo systemctl cat {previous.unit}` sowie Updatejournal und Receipt",
            )
    if not callable(start_authorizer) or start_authorizer(receipt.transaction_id, units) is not True:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-024",
            ", ".join(units),
            "Der getrennte Dienststart ist nicht transaktionsgebunden freigegeben.",
            "Datei-/Enablement-Restore nicht wiederholen; zuerst Startgate/Updatejournal prüfen und dann denselben Recovery-Befehl fortsetzen",
        )
    immediate_guard = capture_systemd_recovery_restore_guard(
        receipt,
        _runner=runner,
    )
    if immediate_guard != start_guard:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-027",
            ", ".join(units),
            "Unitfläche driftete nach der Startfreigabe.",
            "Startfreigabe verwerfen; Dienste gestoppt lassen und das Updatejournal prüfen",
        )
    try:
        for gate in preserved:
            _unlink_exact_systemd_entry(gate)
        for previous, current in zip(receipt.units, immediate_guard.current.units):
            if (
                not previous.managed_dropins.exists
                and _gate_dropins_for_unit(current, preserved)
            ):
                rebound = _capture_systemd_dropin_directory(
                    current.managed_dropins.path
                )
                _restore_systemd_dropin_directory(
                    previous.managed_dropins,
                    rebound,
                )
        _run_systemd(
            runner,
            (SYSTEMD_CONTROL_BINARY, "daemon-reload"),
            require_success=True,
        )
        unblocked = capture_systemd_recovery_receipt(
            units,
            transaction_id=receipt.transaction_id,
            install_root=receipt.install_root,
            full_backup_id=receipt.full_backup_id,
            unit_root=receipt.unit_root,
            _runner=runner,
        )
        for previous, actual in zip(receipt.units, unblocked.units):
            if actual.pre_active_state == "active" or not _systemd_unit_surface_semantics(
                previous,
                actual,
                require_pre_active=False,
            ):
                raise RuntimeError(
                    f"Unitfläche von {previous.unit} driftete beim gebundenen Öffnen des Startgates"
                )
        for unit in units:
            if unit in plan.preactive_units:
                _run_systemd(
                    runner,
                    (SYSTEMD_CONTROL_BINARY, "reset-failed", "--", unit),
                    require_success=False,
                )
                _run_systemd(
                    runner,
                    (SYSTEMD_CONTROL_BINARY, "start", "--", unit),
                    require_success=True,
                )
            else:
                _run_systemd(
                    runner,
                    (SYSTEMD_CONTROL_BINARY, "stop", "--", unit),
                    require_success=False,
                )
        for previous in receipt.units:
            active = str(_capture_systemd_show(previous.unit, runner)["pre_active_state"])
            if (
                (previous.pre_active_state == "active" and active != "active")
                or (
                    previous.pre_active_state != "active"
                    and active not in {"inactive", "failed"}
                )
            ):
                raise RuntimeError(
                    f"Aktivitätsendzustand von {previous.unit} ist {active}"
                )
    except Exception as exc:
        for unit in reversed(units):
            try:
                _run_systemd(
                    runner,
                    (SYSTEMD_CONTROL_BINARY, "stop", "--", unit),
                    require_success=False,
                )
            except Exception:
                pass
        if isinstance(exc, UpdateRecoverySurfaceError):
            raise
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-025",
            ", ".join(units),
            f"Getrennter Dienststart blieb unvollständig: {exc}.",
            "Dienste gestoppt lassen; `systemctl status` der genannten Units und das Updatejournal prüfen, danach denselben Recovery-Befehl erneut ausführen",
        ) from exc


def resume_systemd_pre_active_state_after_gate_open(
    receipt: SystemdRecoveryReceipt,
    *,
    gate_dropins: Mapping[str, Mapping[str, object]],
    start_authorizer: StartAuthorizer,
    _runner: SystemdRunner | None = None,
) -> None:
    """Vollendet einen durable autorisierten Altstart nach Teil-Gate-Cleanup.

    Fehlende eigene Gate-Dateien gelten ausschließlich in diesem bereits
    terminal autorisierten Pfad als schon entfernt. Jeder noch vorhandene Name
    muss dagegen byte-, inode- und metadatengebunden derselben Transaktion
    gehören, bevor er idempotent entfernt wird.
    """

    _systemd_recovery_receipt_mapping(receipt)
    units = tuple(item.unit for item in receipt.units)
    expected_preactive = tuple(
        item.unit for item in receipt.units if item.pre_active_state == "active"
    )
    if not callable(start_authorizer):
        raise ValueError("Terminaler systemd-Startautorizer ist nicht aufrufbar")
    specs = dict(gate_dropins)
    expected_directories = {
        item.managed_dropins.path for item in receipt.units
    }
    if (
        len(specs) != len(receipt.units)
        or {os.path.dirname(path) for path in specs} != expected_directories
    ):
        raise ValueError("Terminaler Gatevertrag deckt die Unitmenge nicht exakt ab")

    runner = _runner or _default_systemd_runner
    gate_paths = set(specs)

    def capture_terminal_guard() -> SystemdRecoveryRestoreGuard:
        current = capture_systemd_recovery_receipt(
            units,
            transaction_id=receipt.transaction_id,
            install_root=receipt.install_root,
            full_backup_id=receipt.full_backup_id,
            unit_root=receipt.unit_root,
            _runner=runner,
        )
        for previous, actual in zip(receipt.units, current.units):
            if (
                actual.pre_active_state == "active"
                and previous.pre_active_state != "active"
            ):
                raise UpdateRecoverySurfaceError(
                    "E3DC-UPD-RECOVERY-SYSTEMD-033",
                    actual.unit,
                    "Eine ursprünglich inaktive Unit läuft im terminalen Altstart.",
                    f"Dienst stoppen und `sudo systemctl status {actual.unit}` sowie das Updatejournal prüfen",
                )
        return SystemdRecoveryRestoreGuard(current)

    def bind_present_gates(
        guard: SystemdRecoveryRestoreGuard,
    ) -> tuple[SystemdFilePreimage, ...]:
        actual_files = {
            item.path: item
            for unit in guard.current.units
            for item in unit.managed_dropins.entries
        }
        active_paths = {
            path
            for unit in guard.current.units
            for path in unit.active_dropin_paths
        }
        present: list[SystemdFilePreimage] = []
        for path, raw_spec in sorted(specs.items()):
            if not isinstance(raw_spec, Mapping) or set(raw_spec) != {
                "device",
                "inode",
                "payload",
                "uid",
                "gid",
                "mode",
                "nlink",
            }:
                raise ValueError(
                    f"Terminaler Gatevertrag ist unvollständig: {path}"
                )
            payload = raw_spec["payload"]
            if not isinstance(payload, bytes):
                raise ValueError(
                    f"Terminaler Gatepayload ist kein Bytevertrag: {path}"
                )
            current = _capture_systemd_file(
                path,
                allow_mask=False,
                require_root_owner=True,
            )
            if current.kind == "absent":
                if path in actual_files or path in active_paths:
                    raise UpdateRecoverySurfaceError(
                        "E3DC-UPD-RECOVERY-SYSTEMD-028",
                        path,
                        "Abwesende Startsperre widerspricht dem systemd-Postimage.",
                        "Datei nicht manuell verändern; systemd daemon-reload und Updatejournal prüfen",
                    )
                continue
            identity = tuple(current.identity or ())
            if (
                current.kind != "regular"
                or identity[:2]
                != (int(raw_spec["device"]), int(raw_spec["inode"]))
                or current.payload != payload
                or current.sha256 != hashlib.sha256(payload).hexdigest()
                or current.uid != int(raw_spec["uid"])
                or current.gid != int(raw_spec["gid"])
                or current.mode != int(raw_spec["mode"])
                or len(identity) != 9
                or identity[5] != int(raw_spec["nlink"])
                or identity[6] != len(payload)
                or actual_files.get(path) != current
                or path not in active_paths
            ):
                raise UpdateRecoverySurfaceError(
                    "E3DC-UPD-RECOVERY-SYSTEMD-028",
                    path,
                    "Verbliebener Startsperren-Inode gehört nicht exakt zur "
                    "terminal autorisierten Transaktion.",
                    "Datei nicht manuell entfernen; Updatejournal, Inode und "
                    "systemd-Drop-in prüfen und denselben Updatebefehl erneut ausführen",
                )
            present.append(current)
        return tuple(present)

    def verify_projected_alt_surface(
        guard: SystemdRecoveryRestoreGuard,
    ) -> None:
        for previous, actual in zip(receipt.units, guard.current.units):
            remaining_entries = tuple(
                item
                for item in actual.managed_dropins.entries
                if item.path not in gate_paths
            )
            if previous.managed_dropins.exists:
                managed = replace(
                    actual.managed_dropins,
                    entries=remaining_entries,
                )
            else:
                if remaining_entries:
                    raise UpdateRecoverySurfaceError(
                        "E3DC-UPD-RECOVERY-SYSTEMD-029",
                        actual.managed_dropins.path,
                        "Neben der Startsperre besitzt das ursprünglich fehlende "
                        "Drop-in-Verzeichnis fremde Einträge.",
                        "fremde Drop-ins nicht löschen; Unitfläche und Updatejournal prüfen",
                    )
                managed = previous.managed_dropins
            projected = replace(
                actual,
                active_dropin_paths=tuple(
                    path
                    for path in actual.active_dropin_paths
                    if path not in gate_paths
                ),
                managed_dropins=managed,
            )
            if not _systemd_unit_surface_semantics(
                previous,
                projected,
                require_pre_active=False,
            ):
                raise UpdateRecoverySurfaceError(
                    "E3DC-UPD-RECOVERY-SYSTEMD-030",
                    previous.unit,
                    "Unitfläche weicht außerhalb der gebundenen Startsperre vom "
                    "Altpreimage ab.",
                    f"Dienst nicht starten; prüfe `sudo systemctl cat {previous.unit}` und das Updatejournal",
                )

    guard = capture_terminal_guard()
    initially_present = bind_present_gates(guard)
    verify_projected_alt_surface(guard)

    if start_authorizer(receipt.transaction_id, units) is not True:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-031",
            ", ".join(units),
            "Terminaler Altstart ist nicht durch das durable Master-Journal autorisiert.",
            "keine Startsperre manuell entfernen; denselben Updatebefehl erneut ausführen",
        )

    try:
        # Der durable Autorizer ist eine eigene Stromausfallgrenze. Danach
        # wird die komplette Unitfläche erneut gebunden; einzig das monotone
        # Verschwinden eigener Gate-Inodes aus einem früheren Prozesslauf ist
        # als Abweichung zulässig. Innerhalb dieses Aufrufs muss der zweimal
        # gebundene Gate-Satz dagegen identisch bleiben.
        immediate_guard = capture_terminal_guard()
        present = bind_present_gates(immediate_guard)
        verify_projected_alt_surface(immediate_guard)
        initially_present_paths = {item.path for item in initially_present}
        immediate_present_paths = {item.path for item in present}
        if initially_present_paths != immediate_present_paths:
            changed = sorted(
                initially_present_paths.symmetric_difference(
                    immediate_present_paths
                )
            )
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-034",
                ", ".join(changed),
                "Die Startsperren-Fläche driftete während der terminalen "
                "Autorisierung.",
                "keine Drop-in-Datei manuell verändern; Inodes und "
                "Updatejournal prüfen und denselben Updatebefehl erneut "
                "ausführen",
            )
        if start_authorizer(receipt.transaction_id, units) is not True:
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-036",
                ", ".join(units),
                "Die exakte Journal-Autorität fehlt unmittelbar vor dem "
                "Startsperren-Cleanup.",
                "keine Startsperre manuell entfernen; Journal-Inode prüfen "
                "und denselben Updatebefehl erneut ausführen",
            )
        for gate in present:
            _unlink_exact_systemd_entry(gate)
        immediate_units = {
            item.unit: item for item in immediate_guard.current.units
        }
        for previous in receipt.units:
            guarded_unit = immediate_units[previous.unit]
            expected_after_gate_unlink = replace(
                guarded_unit.managed_dropins,
                entries=tuple(
                    item
                    for item in guarded_unit.managed_dropins.entries
                    if item.path not in gate_paths
                ),
            )
            current_directory = _capture_systemd_dropin_directory(
                previous.managed_dropins.path
            )
            if current_directory != expected_after_gate_unlink:
                raise UpdateRecoverySurfaceError(
                    "E3DC-UPD-RECOVERY-SYSTEMD-035",
                    previous.managed_dropins.path,
                    "Die Drop-in-Fläche driftete nach dem gebundenen "
                    "Startsperren-Cleanup.",
                    f"fremde Drop-ins nicht löschen; `sudo systemctl cat "
                    f"{previous.unit}` und das Updatejournal prüfen, danach "
                    "denselben Updatebefehl erneut ausführen",
                )
            _restore_systemd_dropin_directory(
                previous.managed_dropins,
                expected_after_gate_unlink,
            )
        _run_systemd(
            runner,
            (SYSTEMD_CONTROL_BINARY, "daemon-reload"),
            require_success=True,
        )
        unblocked = capture_systemd_recovery_receipt(
            units,
            transaction_id=receipt.transaction_id,
            install_root=receipt.install_root,
            full_backup_id=receipt.full_backup_id,
            unit_root=receipt.unit_root,
            _runner=runner,
        )
        for previous, actual in zip(receipt.units, unblocked.units):
            if not _systemd_unit_surface_semantics(
                previous,
                actual,
                require_pre_active=False,
            ):
                raise RuntimeError(
                    f"Unitfläche von {previous.unit} blieb nach Gate-Cleanup abweichend"
                )
        unblocked_activity = {
            item.unit: item.pre_active_state for item in unblocked.units
        }
        if start_authorizer(receipt.transaction_id, units) is not True:
            raise UpdateRecoverySurfaceError(
                "E3DC-UPD-RECOVERY-SYSTEMD-037",
                ", ".join(units),
                "Die exakte Journal-Autorität fehlt unmittelbar vor den "
                "terminalen Dienstaktionen.",
                "Dienste gestoppt lassen; Journal-Inode und Updatejournal "
                "prüfen und denselben Updatebefehl erneut ausführen",
            )
        for unit in units:
            if unit in expected_preactive:
                if unblocked_activity.get(unit) != "active":
                    _run_systemd(
                        runner,
                        (SYSTEMD_CONTROL_BINARY, "reset-failed", "--", unit),
                        require_success=False,
                    )
                    _run_systemd(
                        runner,
                        (SYSTEMD_CONTROL_BINARY, "start", "--", unit),
                        require_success=True,
                    )
            else:
                _run_systemd(
                    runner,
                    (SYSTEMD_CONTROL_BINARY, "stop", "--", unit),
                    require_success=False,
                )
        for previous in receipt.units:
            active = str(
                _capture_systemd_show(previous.unit, runner)["pre_active_state"]
            )
            if (
                (previous.pre_active_state == "active" and active != "active")
                or (
                    previous.pre_active_state != "active"
                    and active not in {"inactive", "failed"}
                )
            ):
                raise RuntimeError(
                    f"Aktivitätsendzustand von {previous.unit} ist {active}"
                )
    except Exception as exc:
        for unit in reversed(units):
            try:
                _run_systemd(
                    runner,
                    (SYSTEMD_CONTROL_BINARY, "stop", "--", unit),
                    require_success=False,
                )
            except Exception:
                pass
        if isinstance(exc, UpdateRecoverySurfaceError):
            raise
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-032",
            ", ".join(units),
            f"Terminal autorisierter Altstart blieb unvollständig: {exc}.",
            "Dienste gestoppt lassen; denselben Updatebefehl erneut ausführen, "
            "damit nur der gebundene Altstart fortgesetzt wird",
        ) from exc


def capture_systemd_bundle_preimage(
    service_names: Sequence[str],
    *,
    expected_recovery_dropins: Mapping[str, object] | None = None,
    allow_optional_not_found_compat: bool = False,
) -> SystemdBundlePreimage:
    """Kapselt ausschließlich den bestehenden systemd-Bundle-Capture."""

    units = _normalize_systemd_units(service_names)
    try:
        from .utils import capture_systemd_service_bundle

        snapshot = capture_systemd_service_bundle(
            units,
            expected_recovery_dropins=expected_recovery_dropins,
            allow_optional_not_found_compat=allow_optional_not_found_compat,
        )
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-001",
            ", ".join(units),
            f"systemd-Bundle-Preimage ist nicht eindeutig: {exc}.",
            "prüfe `systemctl show <unit> -p FragmentPath -p DropInPaths -p UnitFileState -p ActiveState`; fremde oder mehrdeutige Units zuerst bereinigen",
        ) from exc
    if not isinstance(snapshot, dict) or tuple(snapshot) != units:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-002",
            ", ".join(units),
            "systemd-Bundle-Capture lieferte keinen vollständigen geordneten Vertrag.",
            "Update nicht fortsetzen; Dienstkatalog und Updatejournal prüfen",
        )
    return SystemdBundlePreimage(units=units, snapshot=snapshot)


def rollback_systemd_bundle_preimage(preimage: SystemdBundlePreimage) -> None:
    """Delegiert den Rückfall unverändert an den bestehenden Bundle-Rollback."""

    if (
        not isinstance(preimage, SystemdBundlePreimage)
        or tuple(preimage.snapshot) != preimage.units
        or _normalize_systemd_units(preimage.units) != preimage.units
    ):
        raise ValueError("systemd-Bundle-Preimage ist unvollständig oder umsortiert")
    try:
        from .utils import rollback_systemd_service_bundle

        restored = rollback_systemd_service_bundle(preimage.snapshot)
    except Exception as exc:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-003",
            ", ".join(preimage.units),
            f"systemd-Bundle-Rollback blieb unvollständig: {exc}.",
            "Dienste nicht manuell starten; `systemctl status` der genannten Units und das Updatejournal prüfen",
        ) from exc
    if restored is not True:
        raise UpdateRecoverySurfaceError(
            "E3DC-UPD-RECOVERY-SYSTEMD-004",
            ", ".join(preimage.units),
            "systemd-Bundle-Rollback bestätigte keinen vollständigen Erfolg.",
            "Dienste nicht manuell starten; persistente Startsperre und Updatejournal prüfen",
        )


__all__ = [
    "ApacheSecurityRecoveryPreimage",
    "CrontabInventory",
    "CrontabRestoreGuard",
    "MAX_APACHE_SECURITY_BYTES",
    "MAX_RECOVERY_SURFACE_RECEIPT_BYTES",
    "MAX_ROOT_MANAGED_FILE_BYTES",
    "MAX_ROOT_MANAGED_FILE_ENTRIES",
    "MAX_SYSTEMD_RECOVERY_RECEIPT_BYTES",
    "RECOVERY_SURFACE_RECEIPT_PATH",
    "RECOVERY_SURFACE_RECEIPT_SCHEMA",
    "SYSTEMD_RECOVERY_RECEIPT_PATH",
    "SYSTEMD_RECOVERY_RECEIPT_SCHEMA",
    "PersistedRecoverySurfaceReceipt",
    "PersistedSystemdRecoveryReceipt",
    "RecoverySurfaceReceipt",
    "RootFileInventory",
    "RootFilePreimage",
    "RootFileRestoreGuard",
    "RootManagedFileRecoveryPreimage",
    "SystemdBundlePreimage",
    "SystemdDropinDirectoryPreimage",
    "SystemdFilePreimage",
    "SystemdRecoveryReceipt",
    "SystemdRecoveryRestoreGuard",
    "SystemdRecoveryStartPlan",
    "SystemdUnitRecoveryPreimage",
    "UpdateRecoverySurfaceError",
    "UserCrontabPreimage",
    "capture_crontab_preimages",
    "capture_crontab_restore_guard",
    "capture_root_file_preimages",
    "capture_root_file_restore_guard",
    "capture_systemd_bundle_preimage",
    "capture_systemd_recovery_receipt",
    "capture_systemd_recovery_restore_guard",
    "create_recovery_surface_receipt",
    "create_systemd_recovery_receipt",
    "parse_recovery_surface_receipt",
    "parse_systemd_recovery_receipt",
    "read_recovery_surface_receipt",
    "read_systemd_recovery_receipt",
    "remove_recovery_surface_receipt",
    "remove_systemd_recovery_receipt",
    "restore_crontab_preimages",
    "restore_root_file_preimages",
    "restore_systemd_files_masks_enablement",
    "restore_systemd_pre_active_state",
    "resume_systemd_pre_active_state_after_gate_open",
    "rollback_systemd_bundle_preimage",
    "serialize_recovery_surface_receipt",
    "serialize_systemd_recovery_receipt",
    "write_systemd_recovery_receipt",
    "write_recovery_surface_receipt",
]
