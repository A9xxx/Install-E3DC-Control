#!/usr/bin/python3
"""Enger root-eigener Metadaten-Reparaturpfad für bekannte Produktdateien.

Diese Datei wird vom verifizierten Updater nach ``/usr/local/sbin`` kopiert.
Der Webserver führt ausschließlich diese root-eigene Kopie in drei festen Modi
aus: Nur-Lese-Preflight, normale Reparatur oder bewusst bestätigte Reparatur bei
lokalen Inhaltsabweichungen. Der Launcher liest nur den ebenfalls root-eigenen,
vom Updater erzeugten Positivlisten-Vertrag und verändert weder Dateiinhalte
noch Dienste, Git, Backups oder sudoers.
"""

from __future__ import annotations

import fcntl
import grp
import hashlib
import hmac
import json
import os
import pwd
import secrets
import stat
import sys
import time
from pathlib import Path
from typing import Any


CONTRACT_PATH = Path("/etc/e3dc-control/runtime_permissions_contract.json")
LOCK_PATH = Path("/run/lock/e3dc-control/runtime-permissions-repair.lock")
UPDATE_LOCK_PATH = Path("/run/lock/e3dc-control/update.lock")
PENDING_DIRECTORY = Path("/run/e3dc-control/runtime-permissions-repair")
PENDING_PATH = PENDING_DIRECTORY / "content-drift-confirmation.json"
CONTRACT_SCHEMA = "e3dc_runtime_permissions_v1"
LAUNCHER_FEATURE = "e3dc_runtime_permissions_cli_v3"
PENDING_SCHEMA = "e3dc_runtime_permissions_confirmation_v1"
PENDING_TTL_SECONDS = 300
MAX_CONTRACT_BYTES = 1024 * 1024
MAX_PENDING_BYTES = 4 * 1024 * 1024
MAX_ROOTS = 8
MAX_ENTRIES = 5000
WEB_ROOT = "/var/www/html"
WEB_RUNTIME_ROOTS = frozenset({"data", "history_backups", "logs", "ramdisk", "tmp"})


class RepairError(RuntimeError):
    """Sicherer Abbruch mit einem stabilen Fehlercode."""

    def __init__(
        self,
        code: str,
        message: str,
        *,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.details = dict(details or {})


def _fd_mount_id(descriptor: int) -> str:
    try:
        with open(f"/proc/self/fdinfo/{descriptor}", "r", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("mnt_id:"):
                    return line.split(":", 1)[1].strip()
    except OSError as exc:
        raise RepairError("mount_evidence_missing", "Mount-Bindung ist nicht lesbar") from exc
    raise RepairError("mount_evidence_missing", "Mount-Bindung fehlt")


def _open_absolute_directory(path: str) -> int:
    normalized = os.path.normpath(os.path.abspath(path))
    if normalized == os.sep or not os.path.isabs(normalized):
        raise RepairError("unsafe_root", "Unzulässige Reparaturwurzel")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise RepairError("nofollow_unavailable", "Sicheres nofollow-Öffnen ist nicht verfügbar")
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(os.sep, flags)
    try:
        for component in Path(normalized).parts[1:]:
            named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            if not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode):
                raise RepairError("unsafe_root", f"Pfadkomponente ist kein echtes Verzeichnis: {normalized}")
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                os.close(child)
                raise RepairError("root_drift", f"Reparaturwurzel driftete: {normalized}")
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _read_contract() -> tuple[dict[str, Any], str]:
    try:
        metadata = os.lstat(CONTRACT_PATH)
    except FileNotFoundError as exc:
        raise RepairError("contract_missing", "Root-eigener Rechtevertrag fehlt") from exc
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size <= 0
        or metadata.st_size > MAX_CONTRACT_BYTES
    ):
        raise RepairError("contract_unsafe", "Root-eigener Rechtevertrag ist unsicher")
    descriptor = os.open(
        CONTRACT_PATH,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        payload = bytearray()
        while len(payload) <= MAX_CONTRACT_BYTES:
            chunk = os.read(descriptor, min(1024 * 1024, MAX_CONTRACT_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        rebound = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    named = os.stat(CONTRACT_PATH, follow_symlinks=False)
    if (
        len(payload) > MAX_CONTRACT_BYTES
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (rebound.st_dev, rebound.st_ino, rebound.st_size, rebound.st_mtime_ns)
        or (rebound.st_dev, rebound.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise RepairError("contract_drift", "Root-eigener Rechtevertrag driftete beim Lesen")
    try:
        contract = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RepairError("contract_invalid", "Root-eigener Rechtevertrag ist ungültig") from exc
    if (
        not isinstance(contract, dict)
        or contract.get("schema") != CONTRACT_SCHEMA
        or contract.get("launcher_feature") != LAUNCHER_FEATURE
    ):
        raise RepairError("contract_invalid", "Unbekanntes Rechtevertragsformat")
    return contract, hashlib.sha256(bytes(payload)).hexdigest()


def _open_root_lock(path: Path) -> int:
    """Öffnet eine root-eigene Sperrdatei ohne Symlink- oder Pfadwechsel."""

    parent = path.parent
    try:
        parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise RepairError("lock_unsafe", "Sperrverzeichnis ist nicht verfügbar") from exc
    parent_before = os.lstat(parent)
    if (
        not stat.S_ISDIR(parent_before.st_mode)
        or stat.S_ISLNK(parent_before.st_mode)
        or parent_before.st_uid != 0
        or parent_before.st_gid != 0
        or stat.S_IMODE(parent_before.st_mode) != 0o700
    ):
        raise RepairError("lock_unsafe", "Sperrverzeichnis ist nicht root-gebunden")
    directory_fd = os.open(
        parent,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        parent_opened = os.fstat(directory_fd)
        if (parent_opened.st_dev, parent_opened.st_ino) != (
            parent_before.st_dev,
            parent_before.st_ino,
        ):
            raise RepairError("lock_unsafe", "Sperrverzeichnis driftete")
        descriptor = os.open(
            path.name,
            os.O_RDWR
            | os.O_CREAT
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        opened = os.fstat(descriptor)
        named = os.stat(path.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_uid != 0
            or opened.st_gid != 0
            or stat.S_IMODE(opened.st_mode) != 0o600
            or (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
        ):
            os.close(descriptor)
            raise RepairError("lock_unsafe", "Sperrdatei ist nicht root-gebunden")
        return descriptor
    finally:
        os.close(directory_fd)


def _relative_parts(value: Any) -> tuple[str, ...]:
    raw = str(value or "").replace("\\", "/")
    if not raw or raw.startswith("/"):
        raise RepairError("contract_invalid", "Ungültiger Positivlistenpfad")
    parts = tuple(raw.split("/"))
    if any(part in {"", ".", ".."} for part in parts):
        raise RepairError("contract_invalid", "Ungültiger Positivlistenpfad")
    return parts


def _resolve_identity(value: Any, install_user: str, *, group: bool = False) -> int:
    name = str(value or "")
    if name == "install":
        name = install_user
    try:
        return grp.getgrnam(name).gr_gid if group else pwd.getpwnam(name).pw_uid
    except KeyError as exc:
        raise RepairError("identity_missing", f"Systemkonto fehlt: {name}") from exc


def _validate_root_path(path: str, install_root: str) -> None:
    normalized = os.path.normpath(os.path.abspath(path))
    allowed = {
        install_root,
        WEB_ROOT,
        *(os.path.join(WEB_ROOT, name) for name in WEB_RUNTIME_ROOTS),
    }
    if normalized not in allowed:
        raise RepairError("contract_scope_invalid", f"Reparaturwurzel liegt außerhalb des Vertrags: {normalized}")


def _open_entry(root_fd: int, root_mount_id: str, parts: tuple[str, ...], kind: str) -> tuple[int, os.stat_result]:
    descriptor = os.dup(root_fd)
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    try:
        for index, component in enumerate(parts):
            named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            last = index == len(parts) - 1
            if not last and (not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode)):
                raise RepairError("unsafe_parent", "Positivlisten-Parent ist kein echtes Verzeichnis")
            if last:
                if kind == "directory":
                    named_safe = stat.S_ISDIR(named.st_mode) and not stat.S_ISLNK(
                        named.st_mode
                    )
                elif kind == "file":
                    named_safe = stat.S_ISREG(named.st_mode) and named.st_nlink == 1
                else:
                    raise RepairError("contract_invalid", "Unbekannter Eintragstyp")
                if not named_safe:
                    raise RepairError(
                        "entry_unsafe",
                        "Positivlisteneintrag hat vor dem Öffnen einen unsicheren Typ",
                    )
            flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
            if not last or kind == "directory":
                flags |= directory
            elif kind == "file":
                flags |= getattr(os, "O_NONBLOCK", 0)
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (
                (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino)
                or _fd_mount_id(child) != root_mount_id
            ):
                os.close(child)
                raise RepairError("entry_drift", "Positivlisteneintrag driftete oder liegt auf einem Fremdmount")
            os.close(descriptor)
            descriptor = child
        opened = os.fstat(descriptor)
        if kind == "directory":
            safe_type = stat.S_ISDIR(opened.st_mode)
        elif kind == "file":
            safe_type = stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1
        else:
            raise RepairError("contract_invalid", "Unbekannter Eintragstyp")
        if not safe_type:
            raise RepairError("entry_unsafe", "Positivlisteneintrag hat einen unsicheren Typ")
        return descriptor, opened
    except Exception:
        os.close(descriptor)
        raise


def _sha256_fd(
    descriptor: int,
    before: os.stat_result,
    expected_size: int,
) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = expected_size + 1
    observed = 0
    while remaining > 0:
        chunk = os.read(descriptor, min(1024 * 1024, remaining))
        if not chunk:
            break
        digest.update(chunk)
        observed += len(chunk)
        remaining -= len(chunk)
    after = os.fstat(descriptor)
    if (
        before.st_size != expected_size
        or observed != expected_size
        or (before.st_size, before.st_mtime_ns, before.st_ino)
        != (after.st_size, after.st_mtime_ns, after.st_ino)
    ):
        raise RepairError("entry_drift", "Datei driftete während der Inhaltsprüfung")
    return digest.hexdigest()


def _stat_fingerprint(metadata: os.stat_result) -> dict[str, int]:
    """Bindet einen Inode so, dass ein billiger Recheck Inhaltswechsel erkennt.

    ``ctime_ns`` kann von einem normalen Installationsnutzer nicht auf einen
    alten Wert zurückgesetzt werden. Deshalb müssen beim Bestätigungslauf nur
    die bereits als inhaltlich abweichend erkannten Dateien erneut vollständig
    gehasht werden. Alle vorher releasegleichen Dateien werden über diesen
    exakten Inode-/Zeit-/Größenvertrag erneut gebunden.
    """

    return {
        "dev": int(metadata.st_dev),
        "ino": int(metadata.st_ino),
        "nlink": int(metadata.st_nlink),
        "uid": int(metadata.st_uid),
        "gid": int(metadata.st_gid),
        "mode": int(stat.S_IMODE(metadata.st_mode)),
        "size": int(metadata.st_size),
        "mtime_ns": int(metadata.st_mtime_ns),
        "ctime_ns": int(metadata.st_ctime_ns),
    }


def _entry_path(root: dict[str, Any], entry: dict[str, Any]) -> str:
    return os.path.join(str(root["path"]), str(entry["relative"]))


def _boot_id() -> str:
    try:
        value = Path("/proc/sys/kernel/random/boot_id").read_text(
            encoding="ascii"
        ).strip().lower()
    except OSError as exc:
        raise RepairError(
            "confirmation_runtime_unavailable",
            "Boot-Bindung für die Rechtebestätigung ist nicht lesbar",
        ) from exc
    if len(value) != 36 or any(character not in "0123456789abcdef-" for character in value):
        raise RepairError(
            "confirmation_runtime_unavailable",
            "Boot-Bindung für die Rechtebestätigung ist ungültig",
        )
    return value


def _open_pending_directory(*, create: bool) -> int:
    namespace = PENDING_DIRECTORY.parent
    try:
        namespace_meta = os.lstat(namespace)
    except FileNotFoundError as exc:
        if not create:
            raise RepairError(
                "confirmation_missing",
                "Die Dateilistenfreigabe fehlt; starte die Rechtereparatur erneut",
            ) from exc
        try:
            os.mkdir(namespace, 0o755)
            os.chown(namespace, 0, 0)
            os.chmod(namespace, 0o755)
            namespace_meta = os.lstat(namespace)
        except OSError as create_exc:
            raise RepairError(
                "confirmation_store_unsafe",
                "Root-eigener Laufzeitnamensraum ist nicht verfügbar",
            ) from create_exc
    if (
        not stat.S_ISDIR(namespace_meta.st_mode)
        or stat.S_ISLNK(namespace_meta.st_mode)
        or namespace_meta.st_uid != 0
        or namespace_meta.st_gid != 0
        or stat.S_IMODE(namespace_meta.st_mode) & 0o022
    ):
        raise RepairError(
            "confirmation_store_unsafe",
            "Root-eigener Laufzeitnamensraum ist nicht sicher gebunden",
        )
    namespace_fd = os.open(
        namespace,
        os.O_RDONLY
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0),
    )
    namespace_opened = os.fstat(namespace_fd)
    if (namespace_opened.st_dev, namespace_opened.st_ino) != (
        namespace_meta.st_dev,
        namespace_meta.st_ino,
    ):
        os.close(namespace_fd)
        raise RepairError(
            "confirmation_store_unsafe",
            "Root-eigener Laufzeitnamensraum driftete beim Öffnen",
        )
    try:
        try:
            metadata = os.stat(
                PENDING_DIRECTORY.name,
                dir_fd=namespace_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError as exc:
            if not create:
                raise RepairError(
                    "confirmation_missing",
                    "Die Dateilistenfreigabe fehlt; starte die Rechtereparatur erneut",
                ) from exc
            try:
                os.mkdir(PENDING_DIRECTORY.name, 0o700, dir_fd=namespace_fd)
                metadata = os.stat(
                    PENDING_DIRECTORY.name,
                    dir_fd=namespace_fd,
                    follow_symlinks=False,
                )
            except OSError as create_exc:
                raise RepairError(
                    "confirmation_store_unsafe",
                    "Root-eigener Bestätigungsspeicher ist nicht verfügbar",
                ) from create_exc
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise RepairError(
                "confirmation_store_unsafe",
                "Bestätigungsspeicher ist nicht root-gebunden",
            )
        descriptor = os.open(
            PENDING_DIRECTORY.name,
            os.O_RDONLY
            | getattr(os, "O_DIRECTORY", 0)
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            dir_fd=namespace_fd,
        )
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (metadata.st_dev, metadata.st_ino):
            os.close(descriptor)
            raise RepairError(
                "confirmation_store_unsafe",
                "Bestätigungsspeicher driftete beim Öffnen",
            )
        return descriptor
    finally:
        os.close(namespace_fd)


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise RepairError(
                "confirmation_store_failed",
                "Dateilistenfreigabe konnte nicht vollständig geschrieben werden",
            )
        offset += written


def _store_pending_record(record: dict[str, Any]) -> None:
    payload = (
        json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        + "\n"
    ).encode("utf-8")
    if not payload or len(payload) > MAX_PENDING_BYTES:
        raise RepairError(
            "confirmation_store_failed",
            "Dateilistenfreigabe überschreitet die sichere Größe",
        )
    directory_fd = _open_pending_directory(create=True)
    temporary_name = f".content-drift.{os.getpid()}.{secrets.token_hex(12)}"
    temporary_fd = -1
    try:
        temporary_fd = os.open(
            temporary_name,
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
            0o600,
            dir_fd=directory_fd,
        )
        _write_all(temporary_fd, payload)
        os.fchmod(temporary_fd, 0o600)
        os.fchown(temporary_fd, 0, 0)
        os.fsync(temporary_fd)
        metadata = os.fstat(temporary_fd)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RepairError(
                "confirmation_store_unsafe",
                "Dateilistenfreigabe blieb nicht root-gebunden",
            )
        os.close(temporary_fd)
        temporary_fd = -1
        os.rename(
            temporary_name,
            PENDING_PATH.name,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
        named = os.stat(PENDING_PATH.name, dir_fd=directory_fd, follow_symlinks=False)
        if (
            not stat.S_ISREG(named.st_mode)
            or named.st_nlink != 1
            or named.st_uid != 0
            or named.st_gid != 0
            or stat.S_IMODE(named.st_mode) != 0o600
            or named.st_size != len(payload)
        ):
            raise RepairError(
                "confirmation_store_unsafe",
                "Veröffentlichte Dateilistenfreigabe ist nicht root-gebunden",
            )
    finally:
        if temporary_fd >= 0:
            os.close(temporary_fd)
        try:
            os.unlink(temporary_name, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
        os.close(directory_fd)


def _discard_pending_record() -> None:
    """Entfernt nur den festen Knoten im root-eigenen Bestätigungsverzeichnis."""

    try:
        directory_fd = _open_pending_directory(create=False)
    except RepairError as exc:
        if exc.code == "confirmation_missing":
            return
        raise
    try:
        try:
            metadata = os.stat(
                PENDING_PATH.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o600
        ):
            raise RepairError(
                "confirmation_store_unsafe",
                "Vorhandene Dateilistenfreigabe ist nicht root-gebunden",
            )
        os.unlink(PENDING_PATH.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
    finally:
        os.close(directory_fd)


def _read_pending_record() -> tuple[dict[str, Any], int]:
    directory_fd = _open_pending_directory(create=False)
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                PENDING_PATH.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=directory_fd,
            )
        except FileNotFoundError as exc:
            raise RepairError(
                "confirmation_missing",
                "Die Dateilistenfreigabe fehlt oder wurde bereits verwendet; starte die Rechtereparatur erneut",
            ) from exc
        before = os.fstat(descriptor)
        named = os.stat(
            PENDING_PATH.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != 0
            or before.st_gid != 0
            or stat.S_IMODE(before.st_mode) != 0o600
            or before.st_size <= 0
            or before.st_size > MAX_PENDING_BYTES
            or (before.st_dev, before.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise RepairError(
                "confirmation_store_unsafe",
                "Dateilistenfreigabe ist nicht root-gebunden",
            )
        payload = bytearray()
        while len(payload) <= MAX_PENDING_BYTES:
            chunk = os.read(
                descriptor,
                min(1024 * 1024, MAX_PENDING_BYTES + 1 - len(payload)),
            )
            if not chunk:
                break
            payload.extend(chunk)
        rebound = os.fstat(descriptor)
        named_after = os.stat(
            PENDING_PATH.name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
        if (
            len(payload) > MAX_PENDING_BYTES
            or len(payload) != before.st_size
            or (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            != (rebound.st_dev, rebound.st_ino, rebound.st_size, rebound.st_mtime_ns)
            or (rebound.st_dev, rebound.st_ino)
            != (named_after.st_dev, named_after.st_ino)
        ):
            raise RepairError(
                "confirmation_store_drift",
                "Dateilistenfreigabe driftete beim Lesen",
            )
        try:
            decoded = json.loads(bytes(payload).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise RepairError(
                "confirmation_invalid",
                "Dateilistenfreigabe ist ungültig",
            ) from exc
        if not isinstance(decoded, dict):
            raise RepairError(
                "confirmation_invalid",
                "Dateilistenfreigabe besitzt kein gültiges Format",
            )
        return decoded, directory_fd
    except Exception:
        os.close(directory_fd)
        raise
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _validate_pending_record(
    record: dict[str, Any],
    token: str,
    contract_digest: str,
    *,
    now: float | None = None,
    boot_id: str | None = None,
) -> None:
    current_time = time.time() if now is None else float(now)
    current_boot = _boot_id() if boot_id is None else str(boot_id)
    created_at = record.get("created_at")
    expires_at = record.get("expires_at")
    token_hash = str(record.get("token_sha256") or "")
    if (
        record.get("schema") != PENDING_SCHEMA
        or not isinstance(created_at, (int, float))
        or isinstance(created_at, bool)
        or not isinstance(expires_at, (int, float))
        or isinstance(expires_at, bool)
        or expires_at <= created_at
        or expires_at - created_at > PENDING_TTL_SECONDS
        or created_at > current_time + 5
        or current_time > expires_at
        or str(record.get("boot_id") or "") != current_boot
        or str(record.get("contract_sha256") or "") != contract_digest
        or len(token_hash) != 64
        or any(character not in "0123456789abcdef" for character in token_hash)
    ):
        raise RepairError(
            "confirmation_stale",
            "Die Dateilistenfreigabe ist abgelaufen oder nicht mehr gebunden; starte die Rechtereparatur erneut",
        )
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise RepairError(
            "confirmation_token_invalid",
            "Die Dateilistenfreigabe ist ungültig; starte die Rechtereparatur bei Bedarf erneut",
        )
    supplied_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    if not hmac.compare_digest(token_hash, supplied_hash):
        raise RepairError(
            "confirmation_token_invalid",
            "Die Dateilistenfreigabe ist ungültig; starte die Rechtereparatur bei Bedarf erneut",
        )


def _consume_pending_record(token: str, contract_digest: str) -> dict[str, Any]:
    record, directory_fd = _read_pending_record()
    try:
        try:
            _validate_pending_record(record, token, contract_digest)
        except RepairError as exc:
            if exc.code == "confirmation_stale":
                os.unlink(PENDING_PATH.name, dir_fd=directory_fd)
                os.fsync(directory_fd)
            raise
        # Ab hier ist das Secret korrekt. Die Freigabe wird vor jedem weiteren
        # Recheck verbraucht; auch ein später erkannter Drift verlangt dadurch
        # zwingend einen neuen ersten Reparaturlauf.
        os.unlink(PENDING_PATH.name, dir_fd=directory_fd)
        os.fsync(directory_fd)
        return record
    finally:
        os.close(directory_fd)


def _read_confirmation_token() -> str:
    raw = sys.stdin.buffer.read(130)
    if len(raw) > 65 or raw.count(b"\n") > 1 or raw.endswith(b"\n\n"):
        raise RepairError(
            "confirmation_token_invalid",
            "Die Dateilistenfreigabe muss allein über stdin übergeben werden",
        )
    if raw.endswith(b"\n"):
        raw = raw[:-1]
    try:
        token = raw.decode("ascii")
    except UnicodeError as exc:
        raise RepairError(
            "confirmation_token_invalid",
            "Die Dateilistenfreigabe ist ungültig",
        ) from exc
    if len(token) != 64 or any(character not in "0123456789abcdef" for character in token):
        raise RepairError(
            "confirmation_token_invalid",
            "Die Dateilistenfreigabe ist ungültig",
        )
    return token


def _prepare_contract(contract: dict[str, Any]) -> tuple[str, list[dict[str, Any]]]:
    install_user = str(contract.get("install_user") or "").strip()
    install_root = os.path.normpath(os.path.abspath(str(contract.get("install_root") or "")))
    if not install_user or install_root in {"", os.sep, WEB_ROOT} or len(Path(install_root).parts) < 3:
        raise RepairError("contract_invalid", "Installationsbindung im Rechtevertrag ist ungültig")
    _resolve_identity(install_user, install_user)
    _resolve_identity("www-data", install_user, group=True)
    roots = contract.get("roots")
    if not isinstance(roots, list) or not roots or len(roots) > MAX_ROOTS:
        raise RepairError("contract_invalid", "Rechtevertrag enthält eine ungültige Wurzelliste")
    total_entries = 0
    prepared: list[dict[str, Any]] = []
    seen_roots: set[str] = set()
    for root in roots:
        if not isinstance(root, dict):
            raise RepairError("contract_invalid", "Rechtevertrag enthält einen ungültigen Wurzeleintrag")
        path = os.path.normpath(os.path.abspath(str(root.get("path") or "")))
        _validate_root_path(path, install_root)
        if path in seen_roots:
            raise RepairError("contract_invalid", "Rechtevertrag enthält doppelte Wurzeln")
        seen_roots.add(path)
        entries = root.get("entries", [])
        if not isinstance(entries, list):
            raise RepairError("contract_invalid", "Rechtevertrag enthält eine ungültige Positivliste")
        total_entries += len(entries)
        if total_entries > MAX_ENTRIES:
            raise RepairError("contract_invalid", "Rechtevertrag überschreitet die Positivlistengrenze")
        seen_entries: set[str] = set()
        normalized_entries = []
        for entry in entries:
            if not isinstance(entry, dict):
                raise RepairError("contract_invalid", "Ungültiger Positivlisteneintrag")
            parts = _relative_parts(entry.get("path"))
            relative = "/".join(parts)
            if relative in seen_entries:
                raise RepairError("contract_invalid", "Doppelter Positivlisteneintrag")
            seen_entries.add(relative)
            kind = str(entry.get("kind") or "")
            if kind not in {"file", "directory"}:
                raise RepairError("contract_invalid", "Ungültiger Positivlistentyp")
            mode = int(entry.get("mode", -1))
            if mode < 0 or mode > 0o7777 or mode & 0o7000 not in {0, 0o2000}:
                raise RepairError("contract_invalid", "Ungültiger Positivlistenmodus")
            expected_hash = str(entry.get("sha256") or "")
            if expected_hash and (kind != "file" or len(expected_hash) != 64 or any(c not in "0123456789abcdef" for c in expected_hash)):
                raise RepairError("contract_invalid", "Ungültiger Inhaltsnachweis")
            expected_size_raw = entry.get("size")
            expected_size = (
                int(expected_size_raw)
                if isinstance(expected_size_raw, int)
                and not isinstance(expected_size_raw, bool)
                else None
            )
            if expected_hash and (expected_size is None or expected_size < 0):
                raise RepairError("contract_invalid", "Inhaltsnachweis besitzt keine feste Größe")
            if not expected_hash and expected_size_raw is not None:
                raise RepairError("contract_invalid", "Dateigröße ohne Inhaltsnachweis ist unzulässig")
            normalized_entries.append({
                "parts": parts,
                "relative": relative,
                "kind": kind,
                "uid": _resolve_identity(entry.get("owner"), install_user),
                "gid": _resolve_identity(entry.get("group"), install_user, group=True),
                "mode": mode,
                "optional": bool(entry.get("optional", False)),
                "sha256": expected_hash,
                "size": expected_size,
            })
        prepared.append({
            "path": path,
            "uid": _resolve_identity(root.get("owner"), install_user),
            "gid": _resolve_identity(root.get("group"), install_user, group=True),
            "mode": int(root.get("mode", -1)),
            "entries": normalized_entries,
        })
        if prepared[-1]["mode"] < 0 or prepared[-1]["mode"] > 0o7777:
            raise RepairError("contract_invalid", "Ungültiger Wurzelmodus")
    required_roots = {install_root, WEB_ROOT, os.path.join(WEB_ROOT, "data")}
    if not required_roots.issubset(seen_roots):
        raise RepairError("contract_invalid", "Pflichtwurzeln fehlen im Rechtevertrag")
    return install_user, prepared


def _open_contract_roots(roots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    opened_roots: list[dict[str, Any]] = []
    try:
        for root in roots:
            descriptor = _open_absolute_directory(root["path"])
            metadata = os.fstat(descriptor)
            opened_roots.append(
                {
                    **root,
                    "fd": descriptor,
                    "identity": (metadata.st_dev, metadata.st_ino),
                    "mount_id": _fd_mount_id(descriptor),
                }
            )
        return opened_roots
    except Exception:
        for root in opened_roots:
            os.close(root["fd"])
        raise


def _scan_contract_entries(
    opened_roots: list[dict[str, Any]],
) -> tuple[int, int, list[dict[str, Any]], dict[str, dict[str, int]]]:
    checked = 0
    skipped_optional = 0
    tracked_files: list[dict[str, Any]] = []
    scanned_fingerprints: dict[str, dict[str, int]] = {}
    seen_paths: set[str] = set()
    for root in opened_roots:
        for entry in root["entries"]:
            path = _entry_path(root, entry)
            if path in seen_paths:
                raise RepairError(
                    "contract_invalid",
                    f"Rechtevertrag enthält einen überlappenden Eintrag: {path}",
                )
            seen_paths.add(path)
            try:
                descriptor, metadata = _open_entry(
                    root["fd"],
                    root["mount_id"],
                    entry["parts"],
                    entry["kind"],
                )
            except FileNotFoundError:
                if not entry["optional"]:
                    raise RepairError(
                        "entry_missing",
                        f"Bekannte Produktdatei fehlt: {path}",
                    )
                skipped_optional += 1
                if entry["sha256"]:
                    tracked_files.append(
                        {
                            "path": path,
                            "root_path": root["path"],
                            "relative": entry["relative"],
                            "state": "missing",
                            "fingerprint": None,
                            "observed_sha256": None,
                        }
                    )
                continue
            try:
                checked += 1
                fingerprint = _stat_fingerprint(metadata)
                scanned_fingerprints[path] = fingerprint
                if entry["sha256"]:
                    observed_hash = _sha256_fd(
                        descriptor,
                        metadata,
                        int(metadata.st_size),
                    )
                    tracked_files.append(
                        {
                            "path": path,
                            "root_path": root["path"],
                            "relative": entry["relative"],
                            "state": (
                                "unchanged"
                                if observed_hash == entry["sha256"]
                                else "drift"
                            ),
                            "fingerprint": fingerprint,
                            "observed_sha256": observed_hash,
                        }
                    )
            finally:
                os.close(descriptor)
    return checked, skipped_optional, tracked_files, scanned_fingerprints


def _set_metadata(descriptor: int, entry: dict[str, Any]) -> bool:
    before = os.fstat(descriptor)
    expected = (entry["uid"], entry["gid"], entry["mode"])
    if (before.st_uid, before.st_gid, stat.S_IMODE(before.st_mode)) == expected:
        return False
    os.fchown(descriptor, entry["uid"], entry["gid"])
    os.fchmod(descriptor, entry["mode"])
    after = os.fstat(descriptor)
    if (after.st_uid, after.st_gid, stat.S_IMODE(after.st_mode)) != expected:
        raise RepairError("metadata_unconfirmed", "Dateirechte blieben abweichend")
    return True


def _repair_release_equal_entries(
    opened_roots: list[dict[str, Any]],
    tracked_files: list[dict[str, Any]],
    scanned_fingerprints: dict[str, dict[str, int]],
) -> tuple[int, list[dict[str, Any]]]:
    drift_paths = {
        item["path"] for item in tracked_files if item["state"] == "drift"
    }
    changed = 0
    # Der erste Nutzerauftrag repariert sofort alle strukturell sicheren und
    # releasegleichen Einträge. Inhaltlich abweichende Dateien bleiben bis zur
    # exakt gebundenen Bestätigung vollständig unangetastet.
    for root in opened_roots:
        for entry in root["entries"]:
            path = _entry_path(root, entry)
            try:
                descriptor, metadata = _open_entry(
                    root["fd"],
                    root["mount_id"],
                    entry["parts"],
                    entry["kind"],
                )
            except FileNotFoundError:
                if entry["optional"]:
                    continue
                raise
            try:
                if _stat_fingerprint(metadata) != scanned_fingerprints.get(path):
                    raise RepairError(
                        "entry_drift",
                        f"Positivlisteneintrag driftete vor der Metadatenreparatur: {path}",
                    )
                if path not in drift_paths and _set_metadata(descriptor, entry):
                    changed += 1
            finally:
                os.close(descriptor)

        root_before = os.fstat(root["fd"])
        expected_root = (root["uid"], root["gid"], root["mode"])
        if (
            root_before.st_uid,
            root_before.st_gid,
            stat.S_IMODE(root_before.st_mode),
        ) != expected_root:
            os.fchown(root["fd"], root["uid"], root["gid"])
            os.fchmod(root["fd"], root["mode"])
            changed += 1
        root_after = os.fstat(root["fd"])
        named_after = os.lstat(root["path"])
        if (
            (root_after.st_dev, root_after.st_ino) != root["identity"]
            or (named_after.st_dev, named_after.st_ino) != root["identity"]
            or (
                root_after.st_uid,
                root_after.st_gid,
                stat.S_IMODE(root_after.st_mode),
            )
            != expected_root
        ):
            raise RepairError(
                "root_unconfirmed",
                f"Reparaturwurzel blieb nicht gebunden: {root['path']}",
            )

    # Snapshot erst nach den eben ausgeführten Metadatenkorrekturen bilden.
    # Dadurch kann der zweite Lauf unveränderte Dateien mit einem billigen
    # Stat-Recheck überspringen, ohne sie erneut zu hashen oder zu reparieren.
    root_by_path = {root["path"]: root for root in opened_roots}
    rebound: list[dict[str, Any]] = []
    for item in tracked_files:
        if item["state"] == "missing":
            rebound.append(dict(item))
            continue
        root = root_by_path[item["root_path"]]
        entry = next(
            value
            for value in root["entries"]
            if value["relative"] == item["relative"]
        )
        descriptor, metadata = _open_entry(
            root["fd"], root["mount_id"], entry["parts"], entry["kind"]
        )
        try:
            if (
                item["state"] == "drift"
                and _stat_fingerprint(metadata) != item["fingerprint"]
            ):
                raise RepairError(
                    "entry_drift",
                    f"Lokale Inhaltsabweichung driftete vor der Bestätigungsbindung: {item['path']}",
                )
            rebound_item = dict(item)
            rebound_item["fingerprint"] = _stat_fingerprint(metadata)
            rebound.append(rebound_item)
        finally:
            os.close(descriptor)
    return changed, rebound


def _drift_path_digest(paths: list[str]) -> str:
    payload = json.dumps(
        paths,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _create_pending_confirmation(
    *,
    contract_digest: str,
    tracked_files: list[dict[str, Any]],
    changed: int,
) -> tuple[str, list[str]]:
    drift_paths = [
        str(item["path"])
        for item in tracked_files
        if item.get("state") == "drift"
    ]
    token = secrets.token_hex(32)
    created_at = time.time()
    record = {
        "schema": PENDING_SCHEMA,
        "created_at": created_at,
        "expires_at": created_at + PENDING_TTL_SECONDS,
        "boot_id": _boot_id(),
        "contract_sha256": contract_digest,
        "token_sha256": hashlib.sha256(token.encode("ascii")).hexdigest(),
        "content_drift": drift_paths,
        "content_drift_sha256": _drift_path_digest(drift_paths),
        "tracked_files": tracked_files,
        "initial_changed": int(changed),
    }
    _store_pending_record(record)
    return token, drift_paths


def _confirm_content_drift(
    *,
    token: str,
    contract_digest: str,
    opened_roots: list[dict[str, Any]],
) -> dict[str, Any]:
    record = _consume_pending_record(token, contract_digest)
    tracked_raw = record.get("tracked_files")
    drift_paths_raw = record.get("content_drift")
    if not isinstance(tracked_raw, list) or not isinstance(drift_paths_raw, list):
        raise RepairError(
            "confirmation_invalid",
            "Dateilistenfreigabe besitzt keine vollständige Pfadbindung",
        )
    drift_paths = [str(path) for path in drift_paths_raw]
    if (
        not drift_paths
        or len(drift_paths) != len(set(drift_paths))
        or str(record.get("content_drift_sha256") or "")
        != _drift_path_digest(drift_paths)
    ):
        raise RepairError(
            "confirmation_invalid",
            "Dateilistenfreigabe besitzt keine eindeutige Driftliste",
        )

    current_entries: dict[str, tuple[dict[str, Any], dict[str, Any]]] = {}
    for root in opened_roots:
        for entry in root["entries"]:
            if not entry["sha256"]:
                continue
            path = _entry_path(root, entry)
            if path in current_entries:
                raise RepairError("contract_invalid", "Rechtevertrag überlappt")
            current_entries[path] = (root, entry)

    tracked: dict[str, dict[str, Any]] = {}
    for raw in tracked_raw:
        if not isinstance(raw, dict):
            raise RepairError(
                "confirmation_invalid",
                "Dateilistenfreigabe enthält einen ungültigen Eintrag",
            )
        path = str(raw.get("path") or "")
        state = str(raw.get("state") or "")
        if (
            path in tracked
            or path not in current_entries
            or state not in {"unchanged", "drift", "missing"}
        ):
            raise RepairError(
                "confirmation_invalid",
                "Dateilistenfreigabe stimmt nicht mit dem Rechtevertrag überein",
            )
        root, entry = current_entries[path]
        if (
            str(raw.get("root_path") or "") != root["path"]
            or str(raw.get("relative") or "") != entry["relative"]
        ):
            raise RepairError(
                "confirmation_invalid",
                "Dateilistenfreigabe besitzt eine ungültige Pfadzuordnung",
            )
        tracked[path] = raw
    if set(tracked) != set(current_entries):
        raise RepairError(
            "confirmation_invalid",
            "Dateilistenfreigabe enthält nicht alle bekannten Inhaltsdateien",
        )
    if drift_paths != [
        path for path, item in tracked.items() if item.get("state") == "drift"
    ]:
        raise RepairError(
            "confirmation_invalid",
            "Bestätigte Pfadliste und Dateizustände widersprechen sich",
        )

    # Jede content-gebundene Datei wird erneut no-follow geöffnet. Für vorher
    # unveränderte Dateien genügt die exakte Stat-/ctime-Bindung; nur die
    # bestätigten Driftdateien werden nochmals vollständig gehasht.
    for path, (root, entry) in current_entries.items():
        saved = tracked[path]
        try:
            descriptor, metadata = _open_entry(
                root["fd"], root["mount_id"], entry["parts"], entry["kind"]
            )
        except FileNotFoundError:
            if saved.get("state") == "missing" and entry["optional"]:
                continue
            raise RepairError(
                "confirmation_snapshot_changed",
                f"Datei fehlt seit dem ersten Lauf: {path}; starte die Rechtereparatur erneut",
            )
        try:
            if saved.get("state") == "missing":
                raise RepairError(
                    "confirmation_snapshot_changed",
                    f"Datei kam seit dem ersten Lauf hinzu: {path}; starte die Rechtereparatur erneut",
                )
            if _stat_fingerprint(metadata) != saved.get("fingerprint"):
                raise RepairError(
                    "confirmation_snapshot_changed",
                    f"Datei änderte sich seit dem ersten Lauf: {path}; starte die Rechtereparatur erneut",
                )
            if saved.get("state") == "drift":
                observed_hash = str(saved.get("observed_sha256") or "")
                if (
                    len(observed_hash) != 64
                    or any(character not in "0123456789abcdef" for character in observed_hash)
                    or observed_hash == entry["sha256"]
                    or _sha256_fd(descriptor, metadata, int(metadata.st_size))
                    != observed_hash
                ):
                    raise RepairError(
                        "confirmation_snapshot_changed",
                        f"Lokale Inhaltsabweichung änderte sich: {path}; starte die Rechtereparatur erneut",
                    )
        finally:
            os.close(descriptor)

    changed = 0
    for path in drift_paths:
        root, entry = current_entries[path]
        saved = tracked[path]
        descriptor, metadata = _open_entry(
            root["fd"], root["mount_id"], entry["parts"], entry["kind"]
        )
        try:
            if _stat_fingerprint(metadata) != saved.get("fingerprint"):
                raise RepairError(
                    "confirmation_snapshot_changed",
                    f"Datei driftete unmittelbar vor der Reparatur: {path}; starte die Rechtereparatur erneut",
                )
            if _set_metadata(descriptor, entry):
                changed += 1
        finally:
            os.close(descriptor)

    return {
        "success": True,
        "schema": "e3dc_runtime_permissions_result_v1",
        "message": (
            "Die Metadaten der vollständig bestätigten lokalen Dateien wurden "
            "repariert; ihre Inhalte blieben unverändert."
        ),
        "checked": len(tracked),
        "changed": changed,
        "initial_changed": int(record.get("initial_changed") or 0),
        "optional_missing": sum(
            1 for item in tracked.values() if item.get("state") == "missing"
        ),
        "content_drift_count": len(drift_paths),
        "content_drift": drift_paths,
        "confirmation_required": False,
        "repair_complete": True,
        "content_unchanged": True,
        "services_unchanged": True,
    }


def repair(
    *,
    apply_changes: bool = True,
    confirmation_token: str | None = None,
) -> dict[str, Any]:
    if os.geteuid() != 0:
        raise RepairError("root_required", "Rechtereparatur benötigt den root-eigenen Launcher")
    contract, contract_digest = _read_contract()
    _install_user, roots = _prepare_contract(contract)
    update_lock_fd = _open_root_lock(UPDATE_LOCK_PATH)
    lock_fd = _open_root_lock(LOCK_PATH)
    opened_roots: list[dict[str, Any]] = []
    try:
        try:
            fcntl.flock(update_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RepairError(
                "update_busy",
                "Update oder Deploy läuft; die Rechtereparatur wurde nicht gestartet",
            ) from exc
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RepairError("repair_busy", "Eine Rechtereparatur läuft bereits") from exc
        opened_roots = _open_contract_roots(roots)

        if confirmation_token is not None:
            if not apply_changes:
                raise RepairError(
                    "arguments_forbidden",
                    "Bestätigung und Nur-Lese-Prüfung dürfen nicht kombiniert werden",
                )
            return _confirm_content_drift(
                token=confirmation_token,
                contract_digest=contract_digest,
                opened_roots=opened_roots,
            )

        # Vollständige Typ-, Link-, Mount- und Inhaltsprüfung vor der ersten
        # Metadatenänderung. Die vollständige Driftliste wird nie gekürzt.
        checked, skipped_optional, tracked_files, scanned_fingerprints = (
            _scan_contract_entries(opened_roots)
        )
        content_drift = [
            str(item["path"])
            for item in tracked_files
            if item.get("state") == "drift"
        ]

        if not apply_changes:
            return {
                "success": True,
                "schema": "e3dc_runtime_permissions_result_v1",
                "message": "Rechte-Preflight abgeschlossen; es wurden keine Änderungen ausgeführt.",
                "preflight_only": True,
                "checked": checked,
                "changed": 0,
                "optional_missing": skipped_optional,
                "content_drift_count": len(content_drift),
                "content_drift": content_drift,
                "confirmation_required": False,
                "content_unchanged": True,
                "services_unchanged": True,
            }
        changed, tracked_files = _repair_release_equal_entries(
            opened_roots,
            tracked_files,
            scanned_fingerprints,
        )
        if content_drift:
            token, rebound_drift_paths = _create_pending_confirmation(
                contract_digest=contract_digest,
                tracked_files=tracked_files,
                changed=changed,
            )
            if rebound_drift_paths != content_drift:
                raise RepairError(
                    "confirmation_invalid",
                    "Interne Driftliste blieb nicht eindeutig gebunden",
                )
            return {
                "success": True,
                "schema": "e3dc_runtime_permissions_result_v1",
                "message": (
                    "Releasegleiche bekannte Einträge wurden sofort repariert. "
                    "Die vollständig aufgelisteten lokalen Inhaltsabweichungen "
                    "blieben unverändert und benötigen eine bewusste Bestätigung."
                ),
                "checked": checked,
                "changed": changed,
                "optional_missing": skipped_optional,
                "content_drift_count": len(content_drift),
                "content_drift": content_drift,
                "confirmation_required": True,
                "confirmation_token": token,
                "confirmation_expires_in_s": PENDING_TTL_SECONDS,
                "repair_complete": False,
                "content_unchanged": True,
                "services_unchanged": True,
            }

        _discard_pending_record()
        return {
            "success": True,
            "schema": "e3dc_runtime_permissions_result_v1",
            "message": "Bekannte Produkt- und Laufzeitrechte wurden ohne Backup und ohne Update geprüft und repariert.",
            "checked": checked,
            "changed": changed,
            "optional_missing": skipped_optional,
            "content_drift_count": 0,
            "content_drift": [],
            "confirmation_required": False,
            "repair_complete": True,
            "content_unchanged": True,
            "services_unchanged": True,
        }
    finally:
        for root in opened_roots:
            os.close(root["fd"])
        os.close(lock_fd)
        os.close(update_lock_fd)


def main() -> int:
    os.umask(0o077)
    try:
        if len(sys.argv) == 1:
            result = repair(apply_changes=True)
        elif len(sys.argv) == 2 and sys.argv[1] == "--check-json":
            result = repair(apply_changes=False)
        elif len(sys.argv) == 2 and sys.argv[1] == "--confirm-content-drift":
            result = repair(
                apply_changes=True,
                confirmation_token=_read_confirmation_token(),
            )
        else:
            raise RepairError(
                "arguments_forbidden",
                "Der Rechte-Launcher akzeptiert nur die drei festen Vertragsmodi",
            )
        code = 0
    except RepairError as exc:
        result = {
            "success": False,
            "schema": "e3dc_runtime_permissions_result_v1",
            "error_code": exc.code,
            "message": str(exc),
            "content_unchanged": True,
            "services_unchanged": True,
        }
        result.update(exc.details)
        code = 2
    except Exception as exc:  # fail-closed, keine Tracebacks oder Geheimnisse in der WebUI
        result = {
            "success": False,
            "schema": "e3dc_runtime_permissions_result_v1",
            "error_code": "unexpected_failure",
            "message": f"Rechtereparatur sicher abgebrochen: {type(exc).__name__}",
            "content_unchanged": True,
            "services_unchanged": True,
        }
        code = 3
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return code


if __name__ == "__main__":
    raise SystemExit(main())
