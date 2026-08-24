"""Unveränderlicher Recovery-Kontext einer E3DC-Update-Transaktion.

Der Kontext enthält ausschließlich die semantischen Altstandsbindungen, die
nach einem Prozessabbruch oder Reboot nicht aus dem bereits veränderten
Produktbaum abgeleitet werden dürfen. Große oder potenziell geheime
Nebenflächen bleiben in eigenen Surface-/systemd-Receipts; hier werden nur
deren kanonischer Pfad, Inode und SHA-256 gebunden.

Das Modul entscheidet weder über Vorwärtsabschluss noch Rückfall und importiert
bewusst keine Dataclasses aus ``Installer.update``. Dadurch bleibt der Codec
zyklusfrei und kann vom Bootstrap sowie vom Ziel-Updater früh geladen werden.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import json
import os
from pathlib import PurePosixPath
import re
import secrets
import stat
from typing import Mapping


RECOVERY_CONTEXT_SCHEMA = "e3dc_update_recovery_context_v1"
RECOVERY_CONTEXT_NAME = "recovery-context.json"
RECOVERY_CONTEXT_DIRECTORY = "/var/lib/e3dc-update-safety"
RECOVERY_CONTEXT_PATH = os.path.join(
    RECOVERY_CONTEXT_DIRECTORY,
    RECOVERY_CONTEXT_NAME,
)
MAX_RECOVERY_CONTEXT_BYTES = 4 * 1024 * 1024

CONFIG_SOURCE_FULL_BACKUP = "full_backup"
CONFIG_SOURCE_SYNTHETIC_MISSING = "synthetic_missing"
CONFIG_SOURCES = frozenset(
    {CONFIG_SOURCE_FULL_BACKUP, CONFIG_SOURCE_SYNTHETIC_MISSING}
)
VALID_HA_ROLES = frozenset({"off", "master", "slave", "shadow"})
VALID_LEGACY_ACTIVITIES = frozenset(
    {"absent", "inactive", "active", "failed"}
)
PRIVILEGED_CATEGORIES = frozenset(
    {"systemd", "watchdog", "system-config"}
)
WATCHDOG_PATHS = frozenset(
    {"/usr/local/bin/boot_notify.sh", "/usr/local/bin/pi_guard.sh"}
)

_SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
_RELEASE_TAG_RE = re.compile(r"v\d+\.\d+\.\d+[A-Za-z0-9._-]*\Z")
_USER_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}\Z")
_UNIT_RE = re.compile(r"[A-Za-z0-9_.@-]+\.service\Z")


class UpdateRecoveryContextError(RuntimeError):
    """Ein Recovery-Kontext ist nicht sicher erzeugbar oder lesbar."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        solution: str,
        subject: str = RECOVERY_CONTEXT_PATH,
    ) -> None:
        self.code = str(code)
        self.solution = str(solution)
        self.subject = str(subject)
        super().__init__(
            f"[{self.code}] {message} Betroffen: {self.subject}. "
            f"Lösung: {self.solution}"
        )


class UpdateRecoveryContextPersistenceError(UpdateRecoveryContextError):
    """Ein Create wirkte möglicherweise, sein Dauerbeweis blieb aber offen."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        solution: str,
        contract: "RecoveryContextContract | None",
        subject: str = RECOVERY_CONTEXT_PATH,
    ) -> None:
        self.contract = contract
        super().__init__(
            code,
            message,
            solution=solution,
            subject=subject,
        )


@dataclass(frozen=True)
class DirectoryIdentity:
    path: str
    device: int
    inode: int
    uid: int
    gid: int
    mode: int


@dataclass(frozen=True)
class RecoveryReceiptReference:
    path: str
    device: int
    inode: int
    sha256: str


@dataclass(frozen=True)
class RecoverySourceBinding:
    old_commit: str | None
    bootstrap_without_git: bool
    bootstrap_rebuild_git: bool


@dataclass(frozen=True)
class RecoveryTargetBinding:
    commit: str
    tag: str
    role: str


@dataclass(frozen=True)
class RecoveryTransitionBinding:
    ha_role: str
    config_path: str
    config_sha256: str
    config_source: str
    bootstrap_legacy_config: bool
    preinstalled_units: tuple[str, ...]
    preactive_units: tuple[str, ...]
    legacy_e3dc_activity: str


@dataclass(frozen=True)
class RecoveryBackupBinding:
    backup_dir: str
    backup_device: int
    backup_inode: int
    parent_device: int
    parent_inode: int
    path_chain: tuple[DirectoryIdentity, ...]
    backup_id: str
    manifest_sha256: str


@dataclass(frozen=True)
class RepoTrackedBinding:
    relative_path: str
    git_mode: int
    git_object_id: str


@dataclass(frozen=True)
class RepoRecoveryBinding:
    expected_commit: str
    tracked_git: tuple[RepoTrackedBinding, ...]
    dirty_paths: tuple[str, ...]


@dataclass(frozen=True)
class InventoryFingerprint:
    install_entries_count: int
    install_entries_sha256: str
    web_entries_count: int
    web_entries_sha256: str
    watchdog_files: tuple[str, ...]


@dataclass(frozen=True)
class PrivilegedBackupPayloadBinding:
    restore_path: str
    category: str
    backup_relative_path: str
    parent_path_chain: tuple[DirectoryIdentity, ...]
    device: int
    inode: int
    sha256: str
    size: int
    mode: int
    uid: int
    gid: int
    nlink: int
    mtime_ns: int
    ctime_ns: int


@dataclass(frozen=True)
class RecoveryContext:
    transaction_id: str
    install_root: str
    install_user: str
    source: RecoverySourceBinding
    target: RecoveryTargetBinding
    transition: RecoveryTransitionBinding
    backup: RecoveryBackupBinding
    repo: RepoRecoveryBinding | None
    inventory: InventoryFingerprint
    privileged_backup_payloads: tuple[PrivilegedBackupPayloadBinding, ...]
    surface_receipt: RecoveryReceiptReference
    systemd_receipt: RecoveryReceiptReference


@dataclass(frozen=True)
class RecoveryContextContract:
    context: RecoveryContext
    context_path: str
    context_device: int
    context_inode: int
    context_size: int
    context_sha256: str


def _solution_relaunch() -> str:
    return (
        "starte denselben Updatebefehl erneut. Der aktuelle Updater bindet "
        "den vorhandenen Recovery-Kontext automatisch; die Datei nicht "
        "manuell bearbeiten oder löschen"
    )


def _solution_parent(path: str) -> str:
    parent = os.path.dirname(path)
    return (
        f"prüfe `sudo ls -ld {parent}`. Der Recovery-Ordner muss root:root "
        "gehören und Modus 0700 besitzen; fehlende oder abweichende Rechte "
        "gezielt korrigieren und denselben Updatebefehl erneut starten"
    )


def _require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != 0:
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-ROOT-001",
            "Recovery-Kontextdateien dürfen ausschließlich Root verwalten.",
            solution="starte denselben Updatebefehl erneut mit `sudo`",
        )


def _strict_integer(
    value: object,
    *,
    label: str,
    minimum: int = 0,
    maximum: int | None = None,
) -> int:
    if (
        not isinstance(value, int)
        or isinstance(value, bool)
        or value < minimum
        or (maximum is not None and value > maximum)
    ):
        raise ValueError(f"{label} ist keine zulässige Ganzzahl")
    return value


def _strict_bool(value: object, *, label: str) -> bool:
    if not isinstance(value, bool):
        raise ValueError(f"{label} ist kein boolescher Wert")
    return value


def _strict_sha256(value: object, *, label: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise ValueError(f"{label} ist keine kanonische SHA-256")
    return value


def _strict_sha1(value: object, *, label: str, optional: bool = False) -> str | None:
    if optional and value is None:
        return None
    if not isinstance(value, str) or not _SHA1_RE.fullmatch(value):
        raise ValueError(f"{label} ist keine vollständige Git-SHA-1")
    return value


def _canonical_absolute_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or not os.path.isabs(value)
        or os.path.normpath(value) != value
        or os.path.abspath(value) != value
        or os.path.realpath(value) != value
        or value == "/"
    ):
        raise ValueError(f"{label} ist kein kanonischer absoluter Pfad")
    if len(value.encode("utf-8")) > 4096:
        raise ValueError(f"{label} ist zu lang")
    return value


def _canonical_directory_path(value: object, *, label: str) -> str:
    if value == "/":
        return "/"
    return _canonical_absolute_path(value, label=label)


def _canonical_relative_path(value: object, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or not value
        or "\x00" in value
        or "\r" in value
        or "\n" in value
        or "\\" in value
        or value.startswith("/")
        or len(value.encode("utf-8")) > 4096
    ):
        raise ValueError(f"{label} ist kein kanonischer relativer Pfad")
    candidate = PurePosixPath(value)
    if (
        candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
        or candidate.as_posix() != value
    ):
        raise ValueError(f"{label} ist kein kanonischer relativer Pfad")
    return value


def _strict_unit(value: object) -> str:
    if not isinstance(value, str) or not _UNIT_RE.fullmatch(value):
        raise ValueError("systemd-Unit ist nicht kanonisch")
    return value


def _strict_sorted_unique_texts(
    values: object,
    *,
    label: str,
    validator,
    maximum_items: int = 65536,
) -> tuple[str, ...]:
    if not isinstance(values, (tuple, list)) or len(values) > maximum_items:
        raise ValueError(f"{label} ist keine begrenzte Liste")
    normalized = tuple(validator(item) for item in values)
    if normalized != tuple(sorted(normalized)) or len(set(normalized)) != len(normalized):
        raise ValueError(f"{label} ist nicht eindeutig sortiert")
    return normalized


def inventory_entries_fingerprint(entries: object) -> tuple[int, str]:
    """Bindet ein Install-/Web-Inventar unabhängig von Set-Reihenfolgen."""

    if not isinstance(entries, (tuple, list, set, frozenset)):
        raise ValueError("Inventar ist keine endliche Pfadmenge")
    if len(entries) > 262144:
        raise ValueError("Inventar überschreitet das Pfadlimit")
    normalized = tuple(
        sorted(
            _canonical_relative_path(item, label="Inventarpfad")
            for item in entries
        )
    )
    if len(set(normalized)) != len(normalized):
        raise ValueError("Inventar enthält doppelte Pfade")
    encoded = json.dumps(
        list(normalized),
        ensure_ascii=True,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("ascii")
    return len(normalized), hashlib.sha256(encoded).hexdigest()


def _validate_directory_identity(
    identity: DirectoryIdentity,
    *,
    label: str,
) -> DirectoryIdentity:
    if not isinstance(identity, DirectoryIdentity):
        raise ValueError(f"{label} besitzt den falschen Typ")
    path = _canonical_directory_path(identity.path, label=f"{label}.path")
    device = _strict_integer(identity.device, label=f"{label}.device")
    inode = _strict_integer(identity.inode, label=f"{label}.inode", minimum=1)
    uid = _strict_integer(identity.uid, label=f"{label}.uid")
    gid = _strict_integer(identity.gid, label=f"{label}.gid")
    mode = _strict_integer(identity.mode, label=f"{label}.mode", maximum=0o7777)
    if uid != 0 or gid != 0 or mode & 0o022:
        raise ValueError(f"{label} ist nicht ausschließlich root-kontrolliert")
    normalized = DirectoryIdentity(path, device, inode, uid, gid, mode)
    if normalized != identity:
        raise ValueError(f"{label} ist nicht kanonisch")
    return identity


def _validate_directory_chain(
    chain: object,
    *,
    label: str,
    expected_final_path: str,
) -> tuple[DirectoryIdentity, ...]:
    if not isinstance(chain, (tuple, list)) or not chain or len(chain) > 256:
        raise ValueError(f"{label} ist keine vollständige Elternkette")
    normalized = tuple(
        _validate_directory_identity(item, label=f"{label}[{index}]")
        for index, item in enumerate(chain)
    )
    if normalized[0].path != "/" or normalized[-1].path != expected_final_path:
        raise ValueError(f"{label} bindet nicht Root und den erwarteten Endpfad")
    for parent, child in zip(normalized, normalized[1:]):
        if os.path.dirname(child.path) != parent.path:
            raise ValueError(f"{label} enthält keine lückenlose Pfadkette")
    return normalized


def _validate_receipt_reference(
    reference: RecoveryReceiptReference,
    *,
    label: str,
) -> RecoveryReceiptReference:
    if not isinstance(reference, RecoveryReceiptReference):
        raise ValueError(f"{label} besitzt den falschen Typ")
    normalized = RecoveryReceiptReference(
        path=_canonical_absolute_path(reference.path, label=f"{label}.path"),
        device=_strict_integer(reference.device, label=f"{label}.device"),
        inode=_strict_integer(reference.inode, label=f"{label}.inode", minimum=1),
        sha256=_strict_sha256(reference.sha256, label=f"{label}.sha256"),
    )
    if normalized != reference:
        raise ValueError(f"{label} ist nicht kanonisch")
    return reference


def _validate_source(source: RecoverySourceBinding) -> RecoverySourceBinding:
    if not isinstance(source, RecoverySourceBinding):
        raise ValueError("Recovery-Quelle besitzt den falschen Typ")
    old_commit = _strict_sha1(source.old_commit, label="Alt-Commit", optional=True)
    without_git = _strict_bool(
        source.bootstrap_without_git,
        label="bootstrap_without_git",
    )
    rebuild = _strict_bool(
        source.bootstrap_rebuild_git,
        label="bootstrap_rebuild_git",
    )
    if without_git != (old_commit is None):
        raise ValueError("Git-Quellenstatus widerspricht dem Alt-Commit")
    if not without_git and rebuild:
        raise ValueError("Ein gebundenes Alt-Repository darf nicht als Neubau markiert sein")
    return source


def _validate_target(target: RecoveryTargetBinding) -> RecoveryTargetBinding:
    if not isinstance(target, RecoveryTargetBinding):
        raise ValueError("Recovery-Ziel besitzt den falschen Typ")
    commit = _strict_sha1(target.commit, label="Ziel-Commit")
    if not isinstance(target.tag, str) or not _RELEASE_TAG_RE.fullmatch(target.tag):
        raise ValueError("Ziel-Tag ist nicht kanonisch")
    if not isinstance(target.role, str) or target.role not in VALID_HA_ROLES:
        raise ValueError("Zielrolle ist ungültig")
    if commit != target.commit:
        raise ValueError("Ziel-Commit ist nicht kanonisch")
    return target


def _validate_transition(
    transition: RecoveryTransitionBinding,
) -> RecoveryTransitionBinding:
    if not isinstance(transition, RecoveryTransitionBinding):
        raise ValueError("Transition besitzt den falschen Typ")
    if (
        not isinstance(transition.ha_role, str)
        or transition.ha_role not in VALID_HA_ROLES
    ):
        raise ValueError("Transition besitzt keine gültige HA-/Shadow-Rolle")
    _canonical_absolute_path(transition.config_path, label="Konfigurationspfad")
    config_sha = _strict_sha256(
        transition.config_sha256,
        label="Konfigurations-SHA-256",
    )
    if (
        not isinstance(transition.config_source, str)
        or transition.config_source not in CONFIG_SOURCES
    ):
        raise ValueError("Konfigurationsquelle ist unbekannt")
    legacy = _strict_bool(
        transition.bootstrap_legacy_config,
        label="bootstrap_legacy_config",
    )
    installed = _strict_sorted_unique_texts(
        transition.preinstalled_units,
        label="vorinstallierte Units",
        validator=_strict_unit,
        maximum_items=4096,
    )
    active = _strict_sorted_unique_texts(
        transition.preactive_units,
        label="voraktive Units",
        validator=_strict_unit,
        maximum_items=4096,
    )
    if not set(active).issubset(installed):
        raise ValueError("Aktive Units sind keine Teilmenge der installierten Units")
    if (
        not isinstance(transition.legacy_e3dc_activity, str)
        or transition.legacy_e3dc_activity not in VALID_LEGACY_ACTIVITIES
    ):
        raise ValueError("Legacy-e3dc-Aktivität ist ungültig")
    legacy_unit_present = "e3dc.service" in installed
    if legacy_unit_present != (transition.legacy_e3dc_activity != "absent"):
        raise ValueError("Legacy-e3dc-Aktivität widerspricht dem Unit-Inventar")
    if ("e3dc.service" in active) != (
        transition.legacy_e3dc_activity == "active"
    ):
        raise ValueError("Legacy-e3dc-Aktivität widerspricht dem Aktiv-Inventar")
    if legacy:
        if (
            transition.config_source != CONFIG_SOURCE_SYNTHETIC_MISSING
            or config_sha != hashlib.sha256(b"").hexdigest()
        ):
            raise ValueError("Legacy-Konfiguration besitzt keinen Missing-Vertrag")
    elif transition.config_source != CONFIG_SOURCE_FULL_BACKUP:
        raise ValueError("Bestehende Konfiguration muss aus dem Vollbackup stammen")
    if (
        installed != transition.preinstalled_units
        or active != transition.preactive_units
    ):
        raise ValueError("Transition-Unitlisten sind nicht kanonisch")
    return transition


def _validate_backup(backup: RecoveryBackupBinding) -> RecoveryBackupBinding:
    if not isinstance(backup, RecoveryBackupBinding):
        raise ValueError("Vollbackup-Bindung besitzt den falschen Typ")
    backup_dir = _canonical_absolute_path(backup.backup_dir, label="Vollbackup-Pfad")
    chain = _validate_directory_chain(
        backup.path_chain,
        label="Vollbackup-Elternkette",
        expected_final_path=backup_dir,
    )
    if len(chain) < 2:
        raise ValueError("Vollbackup besitzt keine gebundene Elternkomponente")
    device = _strict_integer(backup.backup_device, label="Vollbackup-Device")
    inode = _strict_integer(backup.backup_inode, label="Vollbackup-Inode", minimum=1)
    parent_device = _strict_integer(
        backup.parent_device,
        label="Vollbackup-Parent-Device",
    )
    parent_inode = _strict_integer(
        backup.parent_inode,
        label="Vollbackup-Parent-Inode",
        minimum=1,
    )
    if (chain[-1].device, chain[-1].inode) != (device, inode):
        raise ValueError("Vollbackup-Endinode widerspricht der Elternkette")
    if (chain[-2].device, chain[-2].inode) != (parent_device, parent_inode):
        raise ValueError("Vollbackup-Parent-Inode widerspricht der Elternkette")
    if chain[-1].uid != 0 or chain[-1].gid != 0 or chain[-1].mode != 0o700:
        raise ValueError("Vollbackup-Endverzeichnis ist nicht root:root 0700")
    _strict_sha256(backup.backup_id, label="Vollbackup-ID")
    _strict_sha256(backup.manifest_sha256, label="Vollbackup-Manifest-SHA-256")
    if chain != backup.path_chain:
        raise ValueError("Vollbackup-Elternkette ist nicht kanonisch")
    return backup


def _validate_repo_tracked(entry: RepoTrackedBinding) -> RepoTrackedBinding:
    if not isinstance(entry, RepoTrackedBinding):
        raise ValueError("Git-Dateibindung besitzt den falschen Typ")
    _canonical_relative_path(entry.relative_path, label="Git-Dateipfad")
    if entry.git_mode not in {0o644, 0o755}:
        raise ValueError("Git-Dateimodus ist nicht 0644 oder 0755")
    if not isinstance(entry.git_object_id, str) or not _SHA1_RE.fullmatch(
        entry.git_object_id
    ):
        raise ValueError("Git-Blob-ID ist nicht kanonisch")
    return entry


def _validate_repo(repo: RepoRecoveryBinding) -> RepoRecoveryBinding:
    if not isinstance(repo, RepoRecoveryBinding):
        raise ValueError("Repo-Recovery-Bindung besitzt den falschen Typ")
    _strict_sha1(repo.expected_commit, label="Repo-Alt-Commit")
    if (
        not isinstance(repo.tracked_git, tuple)
        or not repo.tracked_git
        or len(repo.tracked_git) > 65536
    ):
        raise ValueError("Git-Dateivertrag ist leer oder zu groß")
    tracked = tuple(_validate_repo_tracked(item) for item in repo.tracked_git)
    tracked_paths = tuple(item.relative_path for item in tracked)
    if tracked_paths != tuple(sorted(tracked_paths)) or len(set(tracked_paths)) != len(
        tracked_paths
    ):
        raise ValueError("Git-Dateivertrag ist nicht eindeutig sortiert")
    dirty = _strict_sorted_unique_texts(
        repo.dirty_paths,
        label="Dirty-Pfade",
        validator=lambda item: _canonical_relative_path(item, label="Dirty-Pfad"),
    )
    if not set(dirty).issubset(tracked_paths):
        raise ValueError("Dirty-Pfade sind keine Teilmenge des Git-Dateivertrags")
    if tracked != repo.tracked_git or dirty != repo.dirty_paths:
        raise ValueError("Repo-Recovery-Bindung ist nicht kanonisch")
    return repo


def _validate_inventory(inventory: InventoryFingerprint) -> InventoryFingerprint:
    if not isinstance(inventory, InventoryFingerprint):
        raise ValueError("Inventar-Fingerprint besitzt den falschen Typ")
    install_count = _strict_integer(
        inventory.install_entries_count,
        label="Install-Inventaranzahl",
        minimum=1,
    )
    web_count = _strict_integer(
        inventory.web_entries_count,
        label="Web-Inventaranzahl",
    )
    _strict_sha256(inventory.install_entries_sha256, label="Install-Inventar-SHA-256")
    _strict_sha256(inventory.web_entries_sha256, label="Web-Inventar-SHA-256")
    watchdogs = _strict_sorted_unique_texts(
        inventory.watchdog_files,
        label="Watchdog-Dateien",
        validator=lambda item: _canonical_absolute_path(item, label="Watchdog-Pfad"),
        maximum_items=len(WATCHDOG_PATHS),
    )
    if not set(watchdogs).issubset(WATCHDOG_PATHS):
        raise ValueError("Watchdog-Inventar enthält einen fremden Pfad")
    if (
        install_count != inventory.install_entries_count
        or web_count != inventory.web_entries_count
        or watchdogs != inventory.watchdog_files
    ):
        raise ValueError("Inventar-Fingerprint ist nicht kanonisch")
    return inventory


def _privileged_restore_path_allowed(path: str, category: str) -> bool:
    if category == "systemd":
        return os.path.dirname(path) in {
            "/etc/systemd/system",
            "/lib/systemd/system",
            "/usr/lib/systemd/system",
        }
    if category == "watchdog":
        return path in WATCHDOG_PATHS
    if category == "system-config":
        try:
            return os.path.commonpath(("/etc/e3dc-control", path)) == "/etc/e3dc-control"
        except ValueError:
            return False
    return False


def _validate_privileged_payload(
    payload: PrivilegedBackupPayloadBinding,
    *,
    backup: RecoveryBackupBinding,
) -> PrivilegedBackupPayloadBinding:
    if not isinstance(payload, PrivilegedBackupPayloadBinding):
        raise ValueError("Privilegierter Backup-Payload besitzt den falschen Typ")
    restore_path = _canonical_absolute_path(
        payload.restore_path,
        label="Privilegierter Restorepfad",
    )
    if (
        not isinstance(payload.category, str)
        or payload.category not in PRIVILEGED_CATEGORIES
        or not _privileged_restore_path_allowed(restore_path, payload.category)
    ):
        raise ValueError("Privilegierter Restorepfad oder Kategorie ist nicht freigegeben")
    relative_path = _canonical_relative_path(
        payload.backup_relative_path,
        label="Privilegierter Backup-Relativpfad",
    )
    absolute_payload = os.path.normpath(os.path.join(backup.backup_dir, relative_path))
    if os.path.commonpath((backup.backup_dir, absolute_payload)) != backup.backup_dir:
        raise ValueError("Privilegierter Backup-Payload verlässt den Backupbaum")
    parent_path = os.path.dirname(absolute_payload)
    chain = _validate_directory_chain(
        payload.parent_path_chain,
        label="Privilegierte Payload-Elternkette",
        expected_final_path=parent_path,
    )
    backup_chain_entries = [item for item in chain if item.path == backup.backup_dir]
    if len(backup_chain_entries) != 1 or (
        backup_chain_entries[0].device,
        backup_chain_entries[0].inode,
    ) != (backup.backup_device, backup.backup_inode):
        raise ValueError("Privilegierter Payload ist nicht an den Vollbackup-Inode gebunden")
    device = _strict_integer(payload.device, label="Payload-Device")
    inode = _strict_integer(payload.inode, label="Payload-Inode", minimum=1)
    size = _strict_integer(
        payload.size,
        label="Payload-Größe",
        maximum=8 * 1024 * 1024,
    )
    mode = _strict_integer(payload.mode, label="Payload-Modus", maximum=0o7777)
    uid = _strict_integer(payload.uid, label="Payload-UID")
    gid = _strict_integer(payload.gid, label="Payload-GID")
    nlink = _strict_integer(payload.nlink, label="Payload-Linkanzahl", minimum=1)
    mtime_ns = _strict_integer(payload.mtime_ns, label="Payload-mtime_ns")
    ctime_ns = _strict_integer(payload.ctime_ns, label="Payload-ctime_ns")
    _strict_sha256(payload.sha256, label="Payload-SHA-256")
    if device != backup.backup_device or uid != 0 or gid != 0 or nlink != 1:
        raise ValueError("Privilegierter Payload besitzt keinen eindeutigen Backup-Inode")
    if mode & (stat.S_IWGRP | stat.S_IWOTH | stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
        raise ValueError("Privilegierter Payload besitzt unsichere Modusbits")
    if chain != payload.parent_path_chain:
        raise ValueError("Privilegierte Payload-Elternkette ist nicht kanonisch")
    normalized = PrivilegedBackupPayloadBinding(
        restore_path=restore_path,
        category=payload.category,
        backup_relative_path=relative_path,
        parent_path_chain=chain,
        device=device,
        inode=inode,
        sha256=payload.sha256,
        size=size,
        mode=mode,
        uid=uid,
        gid=gid,
        nlink=nlink,
        mtime_ns=mtime_ns,
        ctime_ns=ctime_ns,
    )
    if normalized != payload:
        raise ValueError("Privilegierter Payload ist nicht kanonisch")
    return payload


def validate_recovery_context(context: RecoveryContext) -> RecoveryContext:
    """Validiert alle Typen, Sortierungen und transaktionsweiten Querbindungen."""

    if not isinstance(context, RecoveryContext):
        raise ValueError("Recovery-Kontext besitzt den falschen Typ")
    _strict_sha256(context.transaction_id, label="Transaktions-ID")
    _canonical_absolute_path(context.install_root, label="Installationsroot")
    if not isinstance(context.install_user, str) or not _USER_RE.fullmatch(
        context.install_user
    ):
        raise ValueError("Installationsbenutzer ist nicht kanonisch")
    _validate_source(context.source)
    _validate_target(context.target)
    _validate_transition(context.transition)
    _validate_backup(context.backup)
    _validate_inventory(context.inventory)
    _validate_receipt_reference(context.surface_receipt, label="Surface-Receipt")
    _validate_receipt_reference(context.systemd_receipt, label="systemd-Receipt")
    if context.surface_receipt.path == context.systemd_receipt.path:
        raise ValueError("Surface- und systemd-Receipt besitzen denselben Pfad")
    if context.target.role != context.transition.ha_role:
        raise ValueError("Zielrolle widerspricht der Transition")
    if context.transition.bootstrap_legacy_config and not context.source.bootstrap_without_git:
        raise ValueError("Legacy-Missing-Konfiguration ist nur im Bootstrap ohne Git zulässig")
    if context.source.old_commit is None:
        if context.repo is not None:
            raise ValueError("Bootstrap ohne Alt-Commit darf keinen Repo-Vertrag besitzen")
    else:
        if context.repo is None:
            raise ValueError("Alt-Commit benötigt einen Repo-Recovery-Vertrag")
        _validate_repo(context.repo)
        if context.repo.expected_commit != context.source.old_commit:
            raise ValueError("Repo-Recovery-Commit widerspricht der Quelle")
    if not isinstance(context.privileged_backup_payloads, tuple):
        raise ValueError("Privilegierte Payload-Belege sind keine unveränderliche Liste")
    if len(context.privileged_backup_payloads) > 4096:
        raise ValueError("Privilegierte Payload-Belege überschreiten das Limit")
    payloads = tuple(
        _validate_privileged_payload(item, backup=context.backup)
        for item in context.privileged_backup_payloads
    )
    restore_paths = tuple(item.restore_path for item in payloads)
    relative_paths = tuple(item.backup_relative_path for item in payloads)
    if (
        restore_paths != tuple(sorted(restore_paths))
        or len(set(restore_paths)) != len(restore_paths)
        or len(set(relative_paths)) != len(relative_paths)
    ):
        raise ValueError("Privilegierte Payload-Belege sind nicht eindeutig sortiert")
    if payloads != context.privileged_backup_payloads:
        raise ValueError("Privilegierte Payload-Belege sind nicht kanonisch")
    return context


def _directory_mapping(identity: DirectoryIdentity) -> dict[str, object]:
    return {
        "path": identity.path,
        "device": identity.device,
        "inode": identity.inode,
        "uid": identity.uid,
        "gid": identity.gid,
        "mode": identity.mode,
    }


def _receipt_mapping(reference: RecoveryReceiptReference) -> dict[str, object]:
    return {
        "path": reference.path,
        "device": reference.device,
        "inode": reference.inode,
        "sha256": reference.sha256,
    }


def _context_mapping(context: RecoveryContext) -> dict[str, object]:
    validate_recovery_context(context)
    repo_mapping = None
    if context.repo is not None:
        repo_mapping = {
            "expected_commit": context.repo.expected_commit,
            "tracked_git": [
                {
                    "relative_path": item.relative_path,
                    "git_mode": item.git_mode,
                    "git_object_id": item.git_object_id,
                }
                for item in context.repo.tracked_git
            ],
            "dirty_paths": list(context.repo.dirty_paths),
        }
    return {
        "schema": RECOVERY_CONTEXT_SCHEMA,
        "state": "complete",
        "transaction_id": context.transaction_id,
        "install": {
            "root": context.install_root,
            "user": context.install_user,
        },
        "source": {
            "old_commit": context.source.old_commit,
            "bootstrap_without_git": context.source.bootstrap_without_git,
            "bootstrap_rebuild_git": context.source.bootstrap_rebuild_git,
        },
        "target": {
            "commit": context.target.commit,
            "tag": context.target.tag,
            "role": context.target.role,
        },
        "transition": {
            "ha_role": context.transition.ha_role,
            "config_path": context.transition.config_path,
            "config_sha256": context.transition.config_sha256,
            "config_source": context.transition.config_source,
            "bootstrap_legacy_config": context.transition.bootstrap_legacy_config,
            "preinstalled_units": list(context.transition.preinstalled_units),
            "preactive_units": list(context.transition.preactive_units),
            "legacy_e3dc_activity": context.transition.legacy_e3dc_activity,
        },
        "backup": {
            "dir": context.backup.backup_dir,
            "device": context.backup.backup_device,
            "inode": context.backup.backup_inode,
            "parent_device": context.backup.parent_device,
            "parent_inode": context.backup.parent_inode,
            "path_chain": [
                _directory_mapping(item) for item in context.backup.path_chain
            ],
            "backup_id": context.backup.backup_id,
            "manifest_sha256": context.backup.manifest_sha256,
        },
        "repo": repo_mapping,
        "inventory": {
            "install_entries_count": context.inventory.install_entries_count,
            "install_entries_sha256": context.inventory.install_entries_sha256,
            "web_entries_count": context.inventory.web_entries_count,
            "web_entries_sha256": context.inventory.web_entries_sha256,
            "watchdog_files": list(context.inventory.watchdog_files),
        },
        "privileged_backup_payloads": [
            {
                "restore_path": item.restore_path,
                "category": item.category,
                "backup_relative_path": item.backup_relative_path,
                "parent_path_chain": [
                    _directory_mapping(directory)
                    for directory in item.parent_path_chain
                ],
                "device": item.device,
                "inode": item.inode,
                "sha256": item.sha256,
                "size": item.size,
                "mode": item.mode,
                "uid": item.uid,
                "gid": item.gid,
                "nlink": item.nlink,
                "mtime_ns": item.mtime_ns,
                "ctime_ns": item.ctime_ns,
            }
            for item in context.privileged_backup_payloads
        ],
        "receipts": {
            "surface": _receipt_mapping(context.surface_receipt),
            "systemd": _receipt_mapping(context.systemd_receipt),
        },
    }


def _canonical_json(mapping: Mapping[str, object]) -> bytes:
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
        ).encode("ascii")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise ValueError("Recovery-Kontext ist nicht kanonisch JSON-kodierbar") from exc
    if not payload or len(payload) > MAX_RECOVERY_CONTEXT_BYTES:
        raise ValueError("Recovery-Kontext überschreitet das 4-MiB-Limit")
    return payload


def serialize_recovery_context(context: RecoveryContext) -> bytes:
    """Serialisiert einen vollständig validierten Kontext kanonisch."""

    return _canonical_json(_context_mapping(context))


def _unique_json_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Doppelter JSON-Schlüssel im Recovery-Kontext: {key}")
        result[key] = value
    return result


def _reject_noninteger(raw: str) -> object:
    raise ValueError(f"Nicht-ganzzahliger JSON-Wert ist unzulässig: {raw}")


def _exact_mapping(
    value: object,
    *,
    keys: set[str] | frozenset[str],
    label: str,
) -> dict[str, object]:
    if not isinstance(value, dict) or set(value) != set(keys):
        raise ValueError(f"{label} besitzt nicht exakt den erwarteten Vertrag")
    return value


_DIRECTORY_KEYS = frozenset({"path", "device", "inode", "uid", "gid", "mode"})
_RECEIPT_KEYS = frozenset({"path", "device", "inode", "sha256"})


def _directory_from_mapping(raw: object, *, label: str) -> DirectoryIdentity:
    mapping = _exact_mapping(raw, keys=_DIRECTORY_KEYS, label=label)
    identity = DirectoryIdentity(
        path=mapping["path"],
        device=mapping["device"],
        inode=mapping["inode"],
        uid=mapping["uid"],
        gid=mapping["gid"],
        mode=mapping["mode"],
    )
    return _validate_directory_identity(identity, label=label)


def _receipt_from_mapping(raw: object, *, label: str) -> RecoveryReceiptReference:
    mapping = _exact_mapping(raw, keys=_RECEIPT_KEYS, label=label)
    reference = RecoveryReceiptReference(
        path=mapping["path"],
        device=mapping["device"],
        inode=mapping["inode"],
        sha256=mapping["sha256"],
    )
    return _validate_receipt_reference(reference, label=label)


def parse_recovery_context(
    payload: bytes,
    *,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
) -> RecoveryContext:
    """Parst nur kanonische Bytes mit drei extern bestätigten Bindungen."""

    expected_transaction = _strict_sha256(
        expected_transaction_id,
        label="Erwartete Transaktions-ID",
    )
    expected_root = _canonical_absolute_path(
        expected_install_root,
        label="Erwarteter Installationsroot",
    )
    expected_backup = _strict_sha256(
        expected_full_backup_id,
        label="Erwartete Vollbackup-ID",
    )
    if (
        not isinstance(payload, bytes)
        or not payload
        or len(payload) > MAX_RECOVERY_CONTEXT_BYTES
    ):
        raise ValueError("Recovery-Kontext besitzt eine ungültige Größe")
    try:
        raw = json.loads(
            payload.decode("ascii"),
            object_pairs_hook=_unique_json_object,
            parse_float=_reject_noninteger,
            parse_constant=_reject_noninteger,
        )
    except (UnicodeError, json.JSONDecodeError, ValueError) as exc:
        raise ValueError("Recovery-Kontext ist kein striktes JSON") from exc
    top = _exact_mapping(
        raw,
        keys={
            "schema",
            "state",
            "transaction_id",
            "install",
            "source",
            "target",
            "transition",
            "backup",
            "repo",
            "inventory",
            "privileged_backup_payloads",
            "receipts",
        },
        label="Recovery-Kontext",
    )
    if top["schema"] != RECOVERY_CONTEXT_SCHEMA or top["state"] != "complete":
        raise ValueError("Recovery-Kontext besitzt Schema oder Status nicht")
    install = _exact_mapping(
        top["install"],
        keys={"root", "user"},
        label="Installationsbindung",
    )
    source_map = _exact_mapping(
        top["source"],
        keys={"old_commit", "bootstrap_without_git", "bootstrap_rebuild_git"},
        label="Quellenbindung",
    )
    target_map = _exact_mapping(
        top["target"],
        keys={"commit", "tag", "role"},
        label="Zielbindung",
    )
    transition_map = _exact_mapping(
        top["transition"],
        keys={
            "ha_role",
            "config_path",
            "config_sha256",
            "config_source",
            "bootstrap_legacy_config",
            "preinstalled_units",
            "preactive_units",
            "legacy_e3dc_activity",
        },
        label="Transition",
    )
    if not isinstance(transition_map["preinstalled_units"], list) or not isinstance(
        transition_map["preactive_units"],
        list,
    ):
        raise ValueError("Transition-Unitinventare sind keine Listen")
    backup_map = _exact_mapping(
        top["backup"],
        keys={
            "dir",
            "device",
            "inode",
            "parent_device",
            "parent_inode",
            "path_chain",
            "backup_id",
            "manifest_sha256",
        },
        label="Vollbackup-Bindung",
    )
    raw_backup_chain = backup_map["path_chain"]
    if not isinstance(raw_backup_chain, list):
        raise ValueError("Vollbackup-Elternkette ist keine Liste")
    backup = RecoveryBackupBinding(
        backup_dir=backup_map["dir"],
        backup_device=backup_map["device"],
        backup_inode=backup_map["inode"],
        parent_device=backup_map["parent_device"],
        parent_inode=backup_map["parent_inode"],
        path_chain=tuple(
            _directory_from_mapping(item, label=f"Vollbackup-Elternkette[{index}]")
            for index, item in enumerate(raw_backup_chain)
        ),
        backup_id=backup_map["backup_id"],
        manifest_sha256=backup_map["manifest_sha256"],
    )
    repo = None
    if top["repo"] is not None:
        repo_map = _exact_mapping(
            top["repo"],
            keys={"expected_commit", "tracked_git", "dirty_paths"},
            label="Repo-Recovery-Bindung",
        )
        tracked_raw = repo_map["tracked_git"]
        if not isinstance(tracked_raw, list):
            raise ValueError("Git-Dateivertrag ist keine Liste")
        tracked = []
        for index, raw_entry in enumerate(tracked_raw):
            entry_map = _exact_mapping(
                raw_entry,
                keys={"relative_path", "git_mode", "git_object_id"},
                label=f"Git-Dateivertrag[{index}]",
            )
            tracked.append(
                RepoTrackedBinding(
                    relative_path=entry_map["relative_path"],
                    git_mode=entry_map["git_mode"],
                    git_object_id=entry_map["git_object_id"],
                )
            )
        if not isinstance(repo_map["dirty_paths"], list):
            raise ValueError("Dirty-Pfade sind keine Liste")
        repo = RepoRecoveryBinding(
            expected_commit=repo_map["expected_commit"],
            tracked_git=tuple(tracked),
            dirty_paths=tuple(repo_map["dirty_paths"]),
        )
    inventory_map = _exact_mapping(
        top["inventory"],
        keys={
            "install_entries_count",
            "install_entries_sha256",
            "web_entries_count",
            "web_entries_sha256",
            "watchdog_files",
        },
        label="Inventar-Fingerprint",
    )
    if not isinstance(inventory_map["watchdog_files"], list):
        raise ValueError("Watchdog-Inventar ist keine Liste")
    raw_privileged = top["privileged_backup_payloads"]
    if not isinstance(raw_privileged, list):
        raise ValueError("Privilegierte Payload-Belege sind keine Liste")
    privileged = []
    privileged_keys = {
        "restore_path",
        "category",
        "backup_relative_path",
        "parent_path_chain",
        "device",
        "inode",
        "sha256",
        "size",
        "mode",
        "uid",
        "gid",
        "nlink",
        "mtime_ns",
        "ctime_ns",
    }
    for index, raw_item in enumerate(raw_privileged):
        item_map = _exact_mapping(
            raw_item,
            keys=privileged_keys,
            label=f"Privilegierter Payload[{index}]",
        )
        raw_chain = item_map["parent_path_chain"]
        if not isinstance(raw_chain, list):
            raise ValueError("Privilegierte Payload-Elternkette ist keine Liste")
        privileged.append(
            PrivilegedBackupPayloadBinding(
                restore_path=item_map["restore_path"],
                category=item_map["category"],
                backup_relative_path=item_map["backup_relative_path"],
                parent_path_chain=tuple(
                    _directory_from_mapping(
                        directory,
                        label=f"Privilegierter Payload[{index}].Elternkette[{chain_index}]",
                    )
                    for chain_index, directory in enumerate(raw_chain)
                ),
                device=item_map["device"],
                inode=item_map["inode"],
                sha256=item_map["sha256"],
                size=item_map["size"],
                mode=item_map["mode"],
                uid=item_map["uid"],
                gid=item_map["gid"],
                nlink=item_map["nlink"],
                mtime_ns=item_map["mtime_ns"],
                ctime_ns=item_map["ctime_ns"],
            )
        )
    receipts_map = _exact_mapping(
        top["receipts"],
        keys={"surface", "systemd"},
        label="Receipt-Referenzen",
    )
    context = RecoveryContext(
        transaction_id=top["transaction_id"],
        install_root=install["root"],
        install_user=install["user"],
        source=RecoverySourceBinding(
            old_commit=source_map["old_commit"],
            bootstrap_without_git=source_map["bootstrap_without_git"],
            bootstrap_rebuild_git=source_map["bootstrap_rebuild_git"],
        ),
        target=RecoveryTargetBinding(
            commit=target_map["commit"],
            tag=target_map["tag"],
            role=target_map["role"],
        ),
        transition=RecoveryTransitionBinding(
            ha_role=transition_map["ha_role"],
            config_path=transition_map["config_path"],
            config_sha256=transition_map["config_sha256"],
            config_source=transition_map["config_source"],
            bootstrap_legacy_config=transition_map["bootstrap_legacy_config"],
            preinstalled_units=tuple(transition_map["preinstalled_units"]),
            preactive_units=tuple(transition_map["preactive_units"]),
            legacy_e3dc_activity=transition_map["legacy_e3dc_activity"],
        ),
        backup=backup,
        repo=repo,
        inventory=InventoryFingerprint(
            install_entries_count=inventory_map["install_entries_count"],
            install_entries_sha256=inventory_map["install_entries_sha256"],
            web_entries_count=inventory_map["web_entries_count"],
            web_entries_sha256=inventory_map["web_entries_sha256"],
            watchdog_files=tuple(inventory_map["watchdog_files"]),
        ),
        privileged_backup_payloads=tuple(privileged),
        surface_receipt=_receipt_from_mapping(
            receipts_map["surface"],
            label="Surface-Receipt",
        ),
        systemd_receipt=_receipt_from_mapping(
            receipts_map["systemd"],
            label="systemd-Receipt",
        ),
    )
    validate_recovery_context(context)
    if (
        context.transaction_id != expected_transaction
        or context.install_root != expected_root
        or context.backup.backup_id != expected_backup
    ):
        raise ValueError("Recovery-Kontext gehört nicht zur erwarteten Transaktion")
    if serialize_recovery_context(context) != payload:
        raise ValueError("Recovery-Kontext ist nicht kanonisch serialisiert")
    return context


def _listxattr_empty(descriptor: int, *, label: str) -> None:
    try:
        names = tuple(os.listxattr(descriptor))
    except (AttributeError, OSError) as exc:
        raise RuntimeError(f"{label}: xattr-Prüfung ist nicht verfügbar") from exc
    if names:
        raise RuntimeError(f"{label} besitzt ACLs oder andere xattrs")


def _open_secure_parent(path: str) -> tuple[int, str]:
    target = _canonical_absolute_path(path, label="Recovery-Kontextpfad")
    parent = os.path.dirname(target)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise RuntimeError("Recovery-Kontext benötigt O_NOFOLLOW und O_DIRECTORY")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags)
    current = "/"
    try:
        for component in parent.split(os.sep):
            if not component:
                continue
            before = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            current = os.path.join(current, component)
            if (
                not stat.S_ISDIR(before.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (before.st_dev, before.st_ino) != (opened.st_dev, opened.st_ino)
                or opened.st_uid != 0
                or opened.st_gid != 0
                or stat.S_IMODE(opened.st_mode) & 0o022
            ):
                os.close(child)
                raise RuntimeError(
                    f"Recovery-Kontext-Elternpfad ist nicht root-kontrolliert: {current}"
                )
            os.close(descriptor)
            descriptor = child
        metadata = os.fstat(descriptor)
        if (
            metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RuntimeError("Recovery-Kontext-Ordner ist nicht root:root 0700")
        return descriptor, os.path.basename(target)
    except BaseException:
        os.close(descriptor)
        raise


def _open_secure_parent_or_error(path: str) -> tuple[int, str]:
    try:
        return _open_secure_parent(path)
    except UpdateRecoveryContextError:
        raise
    except Exception as exc:
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-PARENT-001",
            "Recovery-Kontext besitzt keinen sicher geöffneten Elternpfad.",
            solution=_solution_parent(path),
            subject=path,
        ) from exc


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        int(metadata.st_dev),
        int(metadata.st_ino),
        int(metadata.st_size),
        int(metadata.st_uid),
        int(metadata.st_gid),
        int(stat.S_IMODE(metadata.st_mode)),
        int(metadata.st_nlink),
        int(metadata.st_mtime_ns),
        int(metadata.st_ctime_ns),
    )


def _read_context_payload_at(
    parent_descriptor: int,
    name: str,
    *,
    allow_missing: bool,
    expected_nlink: int = 1,
) -> tuple[bytes, os.stat_result] | None:
    try:
        before = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        if allow_missing:
            return None
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-READ-001",
            "Recovery-Kontextdatei fehlt.",
            solution=_solution_relaunch(),
        )
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_uid != 0
        or before.st_gid != 0
        or stat.S_IMODE(before.st_mode) != 0o600
        or before.st_nlink != expected_nlink
        or before.st_size <= 0
        or before.st_size > MAX_RECOVERY_CONTEXT_BYTES
    ):
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-READ-002",
            "Recovery-Kontextdatei besitzt unsichere Metadaten.",
            solution=_solution_relaunch(),
        )
    descriptor = os.open(
        name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        opened = os.fstat(descriptor)
        if _file_identity(opened) != _file_identity(before):
            raise UpdateRecoveryContextError(
                "E3DC-UPD-CONTEXT-READ-003",
                "Recovery-Kontext driftete beim Öffnen.",
                solution=_solution_relaunch(),
            )
        try:
            _listxattr_empty(descriptor, label="Recovery-Kontextdatei")
        except RuntimeError as exc:
            raise UpdateRecoveryContextError(
                "E3DC-UPD-CONTEXT-READ-008",
                "Recovery-Kontextdatei besitzt keinen leeren ACL-/xattr-Vertrag.",
                solution=_solution_relaunch(),
            ) from exc
        chunks = []
        remaining = int(opened.st_size)
        while remaining:
            block = os.read(descriptor, min(64 * 1024, remaining))
            if not block:
                raise UpdateRecoveryContextError(
                    "E3DC-UPD-CONTEXT-READ-004",
                    "Recovery-Kontext endet vor seiner gebundenen Größe.",
                    solution=_solution_relaunch(),
                )
            chunks.append(block)
            remaining -= len(block)
        if os.read(descriptor, 1):
            raise UpdateRecoveryContextError(
                "E3DC-UPD-CONTEXT-READ-005",
                "Recovery-Kontext ist größer als sein Inodebeleg.",
                solution=_solution_relaunch(),
            )
        after = os.fstat(descriptor)
        named_after = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _file_identity(after) != _file_identity(before)
            or _file_identity(named_after) != _file_identity(before)
        ):
            raise UpdateRecoveryContextError(
                "E3DC-UPD-CONTEXT-READ-006",
                "Recovery-Kontext driftete beim Readback.",
                solution=_solution_relaunch(),
            )
        return b"".join(chunks), after
    finally:
        os.close(descriptor)


def _interrupted_create_staging_pattern(name: str) -> re.Pattern[str]:
    return re.compile(
        rf"\.{re.escape(name)}\.[1-9][0-9]*\.[0-9a-f]{{32}}\.tmp\Z"
    )


def _reconcile_interrupted_hardlink_create(
    parent_descriptor: int,
    name: str,
    *,
    path: str,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
) -> None:
    """Vollendet ausschließlich den eigenen Link-vor-Unlink-Crashzustand."""

    try:
        target = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        return
    if target.st_nlink != 2:
        return
    if (
        not stat.S_ISREG(target.st_mode)
        or target.st_uid != 0
        or target.st_gid != 0
        or stat.S_IMODE(target.st_mode) != 0o600
        or target.st_size <= 0
        or target.st_size > MAX_RECOVERY_CONTEXT_BYTES
    ):
        return

    # Vor jeder Mutation wird bereits der nlink=2-Inode vollständig gelesen,
    # kanonisch geparst und an die vom Journal-/Dispatcher-Aufrufer bestätigte
    # Transaktion gebunden. Ein bloß passend benannter Fremdlink reicht nicht.
    staged_result = _read_context_payload_at(
        parent_descriptor,
        name,
        allow_missing=False,
        expected_nlink=2,
    )
    if staged_result is None:
        return
    payload, opened = staged_result
    context = parse_recovery_context(
        payload,
        expected_transaction_id=expected_transaction_id,
        expected_install_root=expected_install_root,
        expected_full_backup_id=expected_full_backup_id,
    )

    pattern = _interrupted_create_staging_pattern(name)
    candidates = []
    for candidate_name in os.listdir(parent_descriptor):
        if not pattern.fullmatch(candidate_name):
            continue
        candidate = os.stat(
            candidate_name,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        if (
            stat.S_ISREG(candidate.st_mode)
            and (candidate.st_dev, candidate.st_ino)
            == (target.st_dev, target.st_ino)
        ):
            candidates.append((candidate_name, candidate))
    if len(candidates) != 1:
        return
    temporary_name, temporary = candidates[0]
    if _file_identity(temporary) != _file_identity(target):
        return

    # Der Inode besitzt exakt zwei Namen: den kanonischen Zielnamen und genau
    # einen vom eigenen Create-Format abgeleiteten Stagingnamen. Während der
    # Deskriptor offen bleibt, wird nur dieser zweite Name entfernt.
    descriptor = os.open(
        temporary_name,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
        dir_fd=parent_descriptor,
    )
    try:
        staging_opened = os.fstat(descriptor)
        named_target = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            _file_identity(staging_opened) != _file_identity(target)
            or _file_identity(named_target) != _file_identity(target)
            or (opened.st_dev, opened.st_ino) != (target.st_dev, target.st_ino)
        ):
            return
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        try:
            os.fsync(parent_descriptor)
        except OSError as exc:
            rebound_contract = None
            try:
                rebound = _read_context_payload_at(
                    parent_descriptor,
                    name,
                    allow_missing=False,
                )
                if rebound is not None:
                    rebound_payload, rebound_metadata = rebound
                    rebound_context = parse_recovery_context(
                        rebound_payload,
                        expected_transaction_id=expected_transaction_id,
                        expected_install_root=expected_install_root,
                        expected_full_backup_id=expected_full_backup_id,
                    )
                    rebound_contract = RecoveryContextContract(
                        context=rebound_context,
                        context_path=path,
                        context_device=int(rebound_metadata.st_dev),
                        context_inode=int(rebound_metadata.st_ino),
                        context_size=int(rebound_metadata.st_size),
                        context_sha256=hashlib.sha256(rebound_payload).hexdigest(),
                    )
            except Exception:
                rebound_contract = None
            raise UpdateRecoveryContextPersistenceError(
                "E3DC-UPD-CONTEXT-RECOVER-001",
                "Unterbrochener Kontext-Create wurde bereinigt, der "
                "Verzeichnis-fsync blieb jedoch unbestätigt.",
                solution=_solution_relaunch(),
                contract=rebound_contract,
                subject=path,
            ) from exc
    finally:
        os.close(descriptor)

    try:
        os.stat(temporary_name, dir_fd=parent_descriptor, follow_symlinks=False)
    except FileNotFoundError:
        pass
    else:
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-RECOVER-002",
            "Eigener Staging-Hardlink blieb nach der Crashbereinigung vorhanden.",
            solution=_solution_relaunch(),
            subject=path,
        )
    final_result = _read_context_payload_at(
        parent_descriptor,
        name,
        allow_missing=False,
    )
    if final_result is None:
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-RECOVER-003",
            "Recovery-Kontext fehlt nach der Crashbereinigung.",
            solution=_solution_relaunch(),
            subject=path,
        )
    final_payload, final_metadata = final_result
    final_context = parse_recovery_context(
        final_payload,
        expected_transaction_id=expected_transaction_id,
        expected_install_root=expected_install_root,
        expected_full_backup_id=expected_full_backup_id,
    )
    if (
        final_context != context
        or (final_metadata.st_dev, final_metadata.st_ino)
        != (target.st_dev, target.st_ino)
    ):
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-RECOVER-004",
            "Recovery-Kontext driftete beim Abschluss des unterbrochenen Creates.",
            solution=_solution_relaunch(),
            subject=path,
        )


def read_recovery_context(
    *,
    expected_transaction_id: str,
    expected_install_root: str,
    expected_full_backup_id: str,
    path: str = RECOVERY_CONTEXT_PATH,
    allow_missing: bool = False,
) -> RecoveryContextContract | None:
    """Liest und bindet eine root-eigene unveränderliche Kontextdatei."""

    _require_root()
    parent_descriptor, name = _open_secure_parent_or_error(path)
    try:
        _reconcile_interrupted_hardlink_create(
            parent_descriptor,
            name,
            path=path,
            expected_transaction_id=expected_transaction_id,
            expected_install_root=expected_install_root,
            expected_full_backup_id=expected_full_backup_id,
        )
        result = _read_context_payload_at(
            parent_descriptor,
            name,
            allow_missing=allow_missing,
        )
    finally:
        os.close(parent_descriptor)
    if result is None:
        return None
    payload, metadata = result
    try:
        context = parse_recovery_context(
            payload,
            expected_transaction_id=expected_transaction_id,
            expected_install_root=expected_install_root,
            expected_full_backup_id=expected_full_backup_id,
        )
    except ValueError as exc:
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-READ-007",
            "Recovery-Kontext besitzt keinen gültigen kanonischen Vertrag.",
            solution=_solution_relaunch(),
            subject=path,
        ) from exc
    return RecoveryContextContract(
        context=context,
        context_path=path,
        context_device=int(metadata.st_dev),
        context_inode=int(metadata.st_ino),
        context_size=int(metadata.st_size),
        context_sha256=hashlib.sha256(payload).hexdigest(),
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise OSError(errno.EIO, "Recovery-Kontext konnte nicht vollständig geschrieben werden")
        offset += written


def write_recovery_context(
    context: RecoveryContext,
    *,
    path: str = RECOVERY_CONTEXT_PATH,
) -> RecoveryContextContract:
    """Erzeugt genau einmal einen root:root-0600-Kontext und liest ihn zurück."""

    _require_root()
    payload = serialize_recovery_context(context)
    parent_descriptor, name = _open_secure_parent_or_error(path)
    temporary_name = f".{name}.{os.getpid()}.{secrets.token_hex(16)}.tmp"
    descriptor = None
    linked = False
    try:
        try:
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            pass
        else:
            raise UpdateRecoveryContextError(
                "E3DC-UPD-CONTEXT-WRITE-001",
                "Ein unveränderlicher Recovery-Kontext ist bereits vorhanden.",
                solution=_solution_relaunch(),
                subject=path,
            )
        descriptor = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=parent_descriptor,
        )
        os.fchown(descriptor, 0, 0)
        os.fchmod(descriptor, 0o600)
        _write_all(descriptor, payload)
        os.fsync(descriptor)
        metadata = os.fstat(descriptor)
        _listxattr_empty(descriptor, label="Gestagter Recovery-Kontext")
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
            or metadata.st_nlink != 1
            or metadata.st_size != len(payload)
        ):
            raise RuntimeError("Gestagter Recovery-Kontext besitzt unsichere Metadaten")
        os.link(
            temporary_name,
            name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        linked = True
        os.unlink(temporary_name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
    except BaseException as exc:
        contract = None
        if linked:
            try:
                contract = read_recovery_context(
                    expected_transaction_id=context.transaction_id,
                    expected_install_root=context.install_root,
                    expected_full_backup_id=context.backup.backup_id,
                    path=path,
                )
            except Exception:
                contract = None
        try:
            os.unlink(temporary_name, dir_fd=parent_descriptor)
        except FileNotFoundError:
            pass
        if isinstance(exc, UpdateRecoveryContextError):
            raise
        raise UpdateRecoveryContextPersistenceError(
            "E3DC-UPD-CONTEXT-WRITE-002",
            "Recovery-Kontext konnte nicht mit vollständigem Dauerbeweis erzeugt werden.",
            solution=_solution_relaunch(),
            contract=contract,
            subject=path,
        ) from exc
    finally:
        if descriptor is not None:
            os.close(descriptor)
        os.close(parent_descriptor)
    rebound = read_recovery_context(
        expected_transaction_id=context.transaction_id,
        expected_install_root=context.install_root,
        expected_full_backup_id=context.backup.backup_id,
        path=path,
    )
    if rebound is None or rebound.context != context:
        raise UpdateRecoveryContextPersistenceError(
            "E3DC-UPD-CONTEXT-WRITE-003",
            "Recovery-Kontext weicht nach dem atomaren Create ab.",
            solution=_solution_relaunch(),
            contract=rebound,
            subject=path,
        )
    return rebound


def remove_recovery_context(contract: RecoveryContextContract) -> None:
    """Entfernt ausschließlich den exakt zuvor gelesenen Kontext-Inode."""

    _require_root()
    if not isinstance(contract, RecoveryContextContract):
        raise ValueError("Recovery-Kontext-Cleanup besitzt keinen Vertrag")
    current = read_recovery_context(
        expected_transaction_id=contract.context.transaction_id,
        expected_install_root=contract.context.install_root,
        expected_full_backup_id=contract.context.backup.backup_id,
        path=contract.context_path,
    )
    if current != contract:
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-REMOVE-001",
            "Recovery-Kontext driftete vor dem Cleanup.",
            solution=_solution_relaunch(),
            subject=contract.context_path,
        )
    parent_descriptor, name = _open_secure_parent_or_error(contract.context_path)
    try:
        metadata = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            int(metadata.st_dev),
            int(metadata.st_ino),
            int(metadata.st_size),
        ) != (
            contract.context_device,
            contract.context_inode,
            contract.context_size,
        ):
            raise UpdateRecoveryContextError(
                "E3DC-UPD-CONTEXT-REMOVE-002",
                "Fremder Recovery-Kontext wird nicht entfernt.",
                solution=_solution_relaunch(),
                subject=contract.context_path,
            )
        os.unlink(name, dir_fd=parent_descriptor)
        os.fsync(parent_descriptor)
        try:
            os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        except FileNotFoundError:
            return
        raise UpdateRecoveryContextError(
            "E3DC-UPD-CONTEXT-REMOVE-003",
            "Recovery-Kontext blieb nach dem Cleanup vorhanden.",
            solution=_solution_relaunch(),
            subject=contract.context_path,
        )
    finally:
        os.close(parent_descriptor)


__all__ = [
    "CONFIG_SOURCE_FULL_BACKUP",
    "CONFIG_SOURCE_SYNTHETIC_MISSING",
    "DirectoryIdentity",
    "InventoryFingerprint",
    "MAX_RECOVERY_CONTEXT_BYTES",
    "PrivilegedBackupPayloadBinding",
    "RECOVERY_CONTEXT_PATH",
    "RECOVERY_CONTEXT_SCHEMA",
    "RecoveryBackupBinding",
    "RecoveryContext",
    "RecoveryContextContract",
    "RecoveryReceiptReference",
    "RecoverySourceBinding",
    "RecoveryTargetBinding",
    "RecoveryTransitionBinding",
    "RepoRecoveryBinding",
    "RepoTrackedBinding",
    "UpdateRecoveryContextError",
    "UpdateRecoveryContextPersistenceError",
    "parse_recovery_context",
    "inventory_entries_fingerprint",
    "read_recovery_context",
    "remove_recovery_context",
    "serialize_recovery_context",
    "validate_recovery_context",
    "write_recovery_context",
]
