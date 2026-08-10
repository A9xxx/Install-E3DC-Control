"""Fail-closed-Vertrag für die E3DC-RAM-Disk und systemd-Dienste."""

from __future__ import annotations

import os
import secrets
import shlex
import stat
import subprocess
from pathlib import Path
from typing import Callable, Iterable, Sequence


RAMDISK_PATH = "/var/www/html/ramdisk"
FINDMNT_PATH = "/usr/bin/findmnt"
SYSTEMCTL_PATH = "/usr/bin/systemctl"
SYSTEMD_CONFIG_DIR = "/etc/systemd/system"
RAMDISK_DROPIN_NAME = "20-e3dc-ramdisk-tmpfs.conf"
RAMDISK_MOUNT_UNIT = "var-www-html-ramdisk.mount"
MANAGED_DROPIN_PREFIX = b"# E3DC-Control managed: ramdisk-tmpfs-"
MAX_DROPIN_BYTES = 16 * 1024
_UNBOUND_PREIMAGE = object()

# Bewusst feste Positivliste. Reparaturpfade wie piguard, e3dc-grabber,
# Apache und systemd-Mount-Units dürfen nicht von der zu reparierenden
# RAM-Disk abhängig werden.
RAMDISK_SERVICE_UNITS: tuple[str, ...] = (
    "e3dc-live.service",
    "energy_manager.service",
    "e3dc-wallbox-manager.service",
    "e3dc-epex-manager.service",
    "e3dc-weather-manager.service",
    "e3dc-storage-simulator.service",
    "e3dc-storage-manager.service",
    "e3dc-ha.service",
    "e3dc-matter-bridge.service",
    "e3dc-bluelink.service",
    "e3dc-lux-live.service",
    "e3dc-idm-live.service",
    "e3dc-stiebel-live.service",
    "e3dc-dimplex-live.service",
    "e3dc-heizstab.service",
    "e3dc-climate-live.service",
    "e3dc-climate-control.service",
    "e3dc-forecast-evidence.service",
    "e3dc-notifier.service",
    "e3dc-mqtt-hub.service",
    "e3dc-websocket.service",
    "e3dc-shadow-sync.service",
)

RAMDISK_RECOVERY_UNITS: tuple[str, ...] = (
    "piguard.service",
    "e3dc-watchdog.service",
    "e3dc-grabber.service",
    "apache2.service",
)


class RamdiskGuardError(RuntimeError):
    """Der RAM-Disk- oder Drop-in-Vertrag ist nicht sicher erfüllbar."""


def _nofollow_flag() -> int:
    value = int(getattr(os, "O_NOFOLLOW", 0))
    if not value:
        raise RamdiskGuardError("Sicheres Datei-Öffnen ohne Symlink-Fallback fehlt")
    return value


def _directory_open_flags() -> int:
    directory = int(getattr(os, "O_DIRECTORY", 0))
    if not directory:
        raise RamdiskGuardError("Sicheres Verzeichnis-Öffnen wird nicht unterstützt")
    return (
        os.O_RDONLY
        | directory
        | _nofollow_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )


def is_docker_environment() -> bool:
    if os.path.isfile("/.dockerenv"):
        return True
    marker = str(os.environ.get("E3DC_CONTAINER_MODE") or "").strip().lower()
    return marker in {"1", "true", "yes", "docker"}


def findmnt_probe_argv(
    *,
    ramdisk_path: str = RAMDISK_PATH,
    findmnt_path: str = FINDMNT_PATH,
) -> tuple[str, ...]:
    """Liefert den read-only-Probe einschließlich sichtbar bleibendem Root-Fallback."""

    return (
        findmnt_path,
        "--kernel",
        "--first-only",
        "--target",
        ramdisk_path,
        "--noheadings",
        "--pairs",
        "--output",
        "TARGET,FSTYPE",
    )


def _parse_findmnt_pairs(output: str) -> dict[str, str]:
    lines = [line.strip() for line in str(output or "").splitlines() if line.strip()]
    if len(lines) != 1:
        return {}
    parsed: dict[str, str] = {}
    try:
        tokens = shlex.split(lines[0], posix=True)
    except ValueError:
        return {}
    for token in tokens:
        key, separator, value = token.partition("=")
        if not separator or key not in {"TARGET", "FSTYPE"} or key in parsed:
            return {}
        parsed[key] = value
    return parsed if set(parsed) == {"TARGET", "FSTYPE"} else {}


def classify_findmnt_result(
    returncode: int,
    stdout: str,
    *,
    ramdisk_path: str = RAMDISK_PATH,
) -> dict[str, object]:
    """Klassifiziert tmpfs, Root-Fallback und falschen Dateisystemtyp eindeutig."""

    if int(returncode) != 0:
        return {
            "ok": False,
            "reason": "findmnt_failed",
            "target": "",
            "fstype": "",
        }
    parsed = _parse_findmnt_pairs(stdout)
    if not parsed:
        return {
            "ok": False,
            "reason": "findmnt_output_invalid",
            "target": "",
            "fstype": "",
        }
    target = parsed["TARGET"]
    fstype = parsed["FSTYPE"]
    if target != ramdisk_path:
        return {
            "ok": False,
            "reason": "root_fallback" if target == "/" else "not_exact_mountpoint",
            "target": target,
            "fstype": fstype,
        }
    if fstype != "tmpfs":
        return {
            "ok": False,
            "reason": "wrong_fstype",
            "target": target,
            "fstype": fstype,
        }
    return {
        "ok": True,
        "reason": "exact_tmpfs",
        "target": target,
        "fstype": fstype,
    }


def probe_ramdisk_tmpfs(
    *,
    ramdisk_path: str = RAMDISK_PATH,
    findmnt_path: str = FINDMNT_PATH,
    runner: Callable[..., object] = subprocess.run,
) -> dict[str, object]:
    """Prüft ohne Mutation, ob exakt am Zielpfad ein tmpfs gemountet ist."""

    argv = findmnt_probe_argv(
        ramdisk_path=ramdisk_path,
        findmnt_path=findmnt_path,
    )
    try:
        result = runner(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        returncode = int(getattr(result, "returncode"))
        stdout = str(getattr(result, "stdout", "") or "")
        stderr = str(getattr(result, "stderr", "") or "")
    except Exception as exc:
        return {
            "ok": False,
            "reason": "findmnt_unavailable",
            "target": "",
            "fstype": "",
            "command": argv,
            "error": str(exc),
        }
    classified = classify_findmnt_result(
        returncode,
        stdout,
        ramdisk_path=ramdisk_path,
    )
    classified.update(
        {
            "command": argv,
            "returncode": returncode,
            "stderr": stderr.strip(),
        }
    )
    return classified


def render_ramdisk_service_dropin() -> str:
    """Rendert die native systemd-Sperre ohne Shell oder installierte Hülle.

    Der Drop-in schützt ausschließlich den RAM-Disk-Startvertrag. Die
    dienstspezifische Neustartzeit bleibt Eigentum der jeweiligen Haupt-Unit;
    andernfalls würde dieser generische Wächter beispielsweise die bewusst
    kürzere Wiederanlaufzeit des Storage Managers überschreiben.
    """

    return f"""# E3DC-Control managed: ramdisk-tmpfs-v1
[Unit]
RequiresMountsFor={RAMDISK_PATH}
After={RAMDISK_MOUNT_UNIT}
StartLimitIntervalSec=300
StartLimitBurst=3

[Service]
ExecStartPre={FINDMNT_PATH} --kernel --first-only --mountpoint {RAMDISK_PATH} --types tmpfs --noheadings --output TARGET
"""


def _metadata_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
    )


def _directory_identity(metadata: os.stat_result) -> tuple[int, ...]:
    """Bindet den Verzeichnisknoten ohne durch eigene Renames volatile Zeiten."""

    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_mode,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
    )


def _file_node_identity(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_nlink,
        metadata.st_uid,
        metadata.st_gid,
    )


def _open_trusted_directory(path: str) -> int:
    metadata = os.lstat(path)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RamdiskGuardError(f"Systemd-Verzeichnis ist nicht vertrauenswürdig: {path}")
    flags = _directory_open_flags()
    descriptor = os.open(path, flags)
    opened = os.fstat(descriptor)
    if _metadata_identity(opened) != _metadata_identity(metadata):
        os.close(descriptor)
        raise RamdiskGuardError(f"Systemd-Verzeichnis driftete beim Öffnen: {path}")
    return descriptor


def _read_regular_file_nofollow(
    directory_fd: int,
    filename: str,
) -> tuple[os.stat_result, bytes] | None:
    try:
        before = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISREG(before.st_mode)
        or stat.S_ISLNK(before.st_mode)
        or before.st_nlink != 1
        or before.st_uid != os.geteuid()
        or before.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        or before.st_size < 0
        or before.st_size > MAX_DROPIN_BYTES
    ):
        raise RamdiskGuardError(f"Drop-in-Datei ist nicht vertrauenswürdig: {filename}")
    flags = (
        os.O_RDONLY
        | _nofollow_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(filename, flags, dir_fd=directory_fd)
    try:
        opened = os.fstat(descriptor)
        if _metadata_identity(opened) != _metadata_identity(before):
            raise RamdiskGuardError(f"Drop-in-Datei driftete beim Öffnen: {filename}")
        chunks: list[bytes] = []
        remaining = before.st_size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        payload = b"".join(chunks)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    named_after = os.stat(filename, dir_fd=directory_fd, follow_symlinks=False)
    if (
        len(payload) != before.st_size
        or _metadata_identity(after) != _metadata_identity(before)
        or _metadata_identity(named_after) != _metadata_identity(before)
    ):
        raise RamdiskGuardError(f"Drop-in-Datei driftete beim Lesen: {filename}")
    return before, payload


def _same_preimage(
    first: tuple[os.stat_result, bytes] | None,
    second: tuple[os.stat_result, bytes] | None,
) -> bool:
    if first is None or second is None:
        return first is None and second is None
    return (
        _metadata_identity(first[0]) == _metadata_identity(second[0])
        and first[1] == second[1]
    )


def _same_preimage_semantic(
    first: tuple[os.stat_result, bytes] | None,
    second: tuple[os.stat_result, bytes] | None,
) -> bool:
    if first is None or second is None:
        return first is None and second is None
    return (
        first[1] == second[1]
        and first[0].st_uid == second[0].st_uid
        and first[0].st_gid == second[0].st_gid
        and stat.S_IMODE(first[0].st_mode) == stat.S_IMODE(second[0].st_mode)
    )


def _write_all(descriptor: int, payload: bytes) -> None:
    offset = 0
    while offset < len(payload):
        written = os.write(descriptor, payload[offset:])
        if written <= 0:
            raise RamdiskGuardError("Drop-in-Datei konnte nicht vollständig geschrieben werden")
        offset += written


def _atomic_write_managed_dropin(
    directory_fd: int,
    filename: str,
    payload: bytes,
    *,
    expected_preimage: object = _UNBOUND_PREIMAGE,
) -> bool:
    preimage = _read_regular_file_nofollow(directory_fd, filename)
    if (
        expected_preimage is not _UNBOUND_PREIMAGE
        and not _same_preimage(expected_preimage, preimage)
    ):
        raise RamdiskGuardError(
            f"Drop-in-Preimage driftete vor dem Staging: {filename}"
        )
    if preimage is not None:
        metadata, existing = preimage
        if existing == payload and stat.S_IMODE(metadata.st_mode) == 0o644:
            return False
        if existing != payload and not existing.startswith(MANAGED_DROPIN_PREFIX):
            raise RamdiskGuardError(
                f"Fremdes Drop-in mit gleichem Namen bleibt unangetastet: {filename}"
            )

    temporary = f".{filename}.{os.getpid()}.{secrets.token_hex(6)}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _nofollow_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        _write_all(descriptor, payload)
        os.fchmod(descriptor, 0o644)
        os.fsync(descriptor)
        written = os.fstat(descriptor)
        if (
            not stat.S_ISREG(written.st_mode)
            or written.st_nlink != 1
            or written.st_uid != os.geteuid()
            or stat.S_IMODE(written.st_mode) != 0o644
            or written.st_size != len(payload)
        ):
            raise RamdiskGuardError("Temporäres Drop-in verletzt den Dateivertrag")
    finally:
        os.close(descriptor)

    try:
        rebound = _read_regular_file_nofollow(directory_fd, filename)
        if not _same_preimage(preimage, rebound):
            raise RamdiskGuardError(
                f"Drop-in-Ziel driftete vor dem atomaren Austausch: {filename}"
            )
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass

    installed = _read_regular_file_nofollow(directory_fd, filename)
    if (
        installed is None
        or installed[1] != payload
        or stat.S_IMODE(installed[0].st_mode) != 0o644
    ):
        raise RamdiskGuardError(f"Drop-in-Endzustand ist ungültig: {filename}")
    return True


def _open_existing_dropin_directory(
    root_fd: int,
    directory_name: str,
) -> int | None:
    try:
        metadata = os.stat(directory_name, dir_fd=root_fd, follow_symlinks=False)
    except FileNotFoundError:
        return None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RamdiskGuardError(
            f"Drop-in-Verzeichnis ist nicht vertrauenswürdig: {directory_name}"
        )
    flags = _directory_open_flags()
    descriptor = os.open(directory_name, flags, dir_fd=root_fd)
    opened = os.fstat(descriptor)
    if _metadata_identity(opened) != _metadata_identity(metadata):
        os.close(descriptor)
        raise RamdiskGuardError(
            f"Drop-in-Verzeichnis driftete beim Öffnen: {directory_name}"
        )
    return descriptor


def _capture_dropin_records(
    root_fd: int,
    selected: Sequence[str],
    payload: bytes,
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    for unit in selected:
        directory_name = f"{unit}.d"
        directory_fd = _open_existing_dropin_directory(root_fd, directory_name)
        directory_existed = directory_fd is not None
        if directory_fd is None:
            preimage = None
            directory_identity = None
        else:
            try:
                directory_identity = _directory_identity(os.fstat(directory_fd))
                preimage = _read_regular_file_nofollow(
                    directory_fd,
                    RAMDISK_DROPIN_NAME,
                )
            finally:
                os.close(directory_fd)
        if (
            preimage is not None
            and preimage[1] != payload
            and not preimage[1].startswith(MANAGED_DROPIN_PREFIX)
        ):
            raise RamdiskGuardError(
                "Fremdes Drop-in mit gleichem Namen bleibt unangetastet: "
                f"{unit}/{RAMDISK_DROPIN_NAME}"
            )
        changed = bool(
            preimage is None
            or preimage[1] != payload
            or stat.S_IMODE(preimage[0].st_mode) != 0o644
        )
        records.append(
            {
                "unit": unit,
                "directory_name": directory_name,
                "directory_existed": directory_existed,
                "directory_identity": directory_identity,
                "directory_created": False,
                "created_directory_identity": None,
                "preimage": preimage,
                "stage_created": False,
                "staged_node_identity": None,
                "staged_preimage": None,
                "target_mutation_observed": False,
                "committed_preimage": None,
                "changed": changed,
            }
        )
    return records


def _open_created_stage_bundle(root_fd: int, name: str) -> int:
    descriptor = _open_existing_dropin_directory(root_fd, name)
    if descriptor is None:
        raise RamdiskGuardError("Drop-in-Staging-Verzeichnis fehlt nach Erstellung")
    metadata = os.fstat(descriptor)
    if stat.S_IMODE(metadata.st_mode) != 0o700:
        os.close(descriptor)
        raise RamdiskGuardError("Drop-in-Staging-Verzeichnis besitzt falsche Rechte")
    return descriptor


def _stage_bundle_payloads(
    stage_fd: int,
    records: Sequence[dict[str, object]],
    payload: bytes,
) -> None:
    for record in records:
        if not record.get("changed"):
            continue
        filename = f"{record['unit']}.staged"
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | _nofollow_flag()
            | getattr(os, "O_CLOEXEC", 0)
        )
        descriptor = os.open(filename, flags, 0o600, dir_fd=stage_fd)
        record["stage_created"] = True
        record["staged_node_identity"] = _file_node_identity(
            os.fstat(descriptor)
        )
        try:
            _write_all(descriptor, payload)
            os.fchmod(descriptor, 0o644)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        staged = _read_regular_file_nofollow(stage_fd, filename)
        if (
            staged is None
            or staged[1] != payload
            or stat.S_IMODE(staged[0].st_mode) != 0o644
        ):
            raise RamdiskGuardError(f"Drop-in-Staging ist ungültig: {record['unit']}")
        if _file_node_identity(staged[0]) != record.get("staged_node_identity"):
            raise RamdiskGuardError(f"Drop-in-Staging driftete: {record['unit']}")
        record["staged_preimage"] = staged
    os.fsync(stage_fd)


def _verify_stage_bundle(
    stage_fd: int,
    records: Sequence[dict[str, object]],
) -> None:
    expected = {
        f"{record['unit']}.staged": record
        for record in records
        if record.get("changed")
    }
    if set(os.listdir(stage_fd)) != set(expected):
        raise RamdiskGuardError("Drop-in-Staging-Bundle ist nicht vollständig")
    for filename, record in expected.items():
        staged = _read_regular_file_nofollow(stage_fd, filename)
        if not _same_preimage(record.get("staged_preimage"), staged):
            raise RamdiskGuardError(
                f"Drop-in-Staging-Preimage driftete: {record['unit']}"
            )


def _record_directory_fd(
    root_fd: int,
    record: dict[str, object],
    *,
    create_missing: bool,
) -> int | None:
    directory_name = str(record["directory_name"])
    descriptor = _open_existing_dropin_directory(root_fd, directory_name)
    if record.get("directory_existed"):
        if descriptor is None:
            raise RamdiskGuardError(f"Drop-in-Verzeichnis verschwand: {directory_name}")
        if _directory_identity(os.fstat(descriptor)) != record.get("directory_identity"):
            os.close(descriptor)
            raise RamdiskGuardError(f"Drop-in-Verzeichnis driftete: {directory_name}")
        return descriptor
    if descriptor is not None:
        if (
            record.get("directory_created")
            and _directory_identity(os.fstat(descriptor))
            == record.get("created_directory_identity")
        ):
            return descriptor
        os.close(descriptor)
        raise RamdiskGuardError(
            f"Drop-in-Verzeichnis erschien außerhalb der Transaktion: {directory_name}"
        )
    if not create_missing:
        return None
    try:
        os.mkdir(directory_name, 0o755, dir_fd=root_fd)
    except FileExistsError as exc:
        raise RamdiskGuardError(
            f"Drop-in-Verzeichnis erschien vor der eigenen Erstellung: {directory_name}"
        ) from exc
    record["directory_created"] = True
    descriptor = _open_existing_dropin_directory(root_fd, directory_name)
    if descriptor is None:
        raise RamdiskGuardError(
            f"Eigenes Drop-in-Verzeichnis verschwand: {directory_name}"
        )
    record["created_directory_identity"] = _directory_identity(
        os.fstat(descriptor)
    )
    return descriptor


def _verify_dropin_prestate(
    root_fd: int,
    records: Sequence[dict[str, object]],
) -> None:
    for record in records:
        descriptor = _record_directory_fd(
            root_fd,
            record,
            create_missing=False,
        )
        if descriptor is None:
            current = None
        else:
            try:
                current = _read_regular_file_nofollow(
                    descriptor,
                    RAMDISK_DROPIN_NAME,
                )
            finally:
                os.close(descriptor)
        if not _same_preimage(record.get("preimage"), current):
            raise RamdiskGuardError(
                f"Drop-in-Prestate driftete vor Commit: {record['unit']}"
            )


def _verify_dropin_rollback_state(
    root_fd: int,
    records: Sequence[dict[str, object]],
) -> None:
    for record in records:
        if not record.get("directory_existed"):
            descriptor = _open_existing_dropin_directory(
                root_fd,
                str(record["directory_name"]),
            )
            if descriptor is not None:
                os.close(descriptor)
                raise RamdiskGuardError(
                    f"Neu erzeugtes Drop-in-Verzeichnis nicht entfernt: {record['unit']}"
                )
            continue
        descriptor = _record_directory_fd(
            root_fd,
            record,
            create_missing=False,
        )
        if descriptor is None:
            current = None
        else:
            try:
                current = _read_regular_file_nofollow(
                    descriptor,
                    RAMDISK_DROPIN_NAME,
                )
            finally:
                os.close(descriptor)
        comparator = (
            _same_preimage_semantic
            if record.get("target_mutation_observed")
            else _same_preimage
        )
        if not comparator(record.get("preimage"), current):
            raise RamdiskGuardError(
                f"Drop-in-Gesamtpreimage nicht restauriert: {record['unit']}"
            )


def _verify_dropin_commit_state(
    root_fd: int,
    records: Sequence[dict[str, object]],
) -> None:
    for record in records:
        descriptor = _record_directory_fd(
            root_fd,
            record,
            create_missing=False,
        )
        if descriptor is None:
            raise RamdiskGuardError(
                f"Drop-in-Verzeichnis fehlt nach Bundle-Commit: {record['unit']}"
            )
        try:
            current = _read_regular_file_nofollow(
                descriptor,
                RAMDISK_DROPIN_NAME,
            )
        finally:
            os.close(descriptor)
        expected = (
            record.get("committed_preimage")
            if record.get("changed")
            else record.get("preimage")
        )
        if not _same_preimage(expected, current):
            raise RamdiskGuardError(
                f"Drop-in-Bundle-Commit driftete: {record['unit']}"
            )


def _install_staged_bundle(
    root_fd: int,
    stage_fd: int,
    records: Sequence[dict[str, object]],
) -> tuple[str, ...]:
    committed: list[str] = []
    for record in records:
        if not record.get("changed"):
            continue
        staged = _read_regular_file_nofollow(
            stage_fd,
            f"{record['unit']}.staged",
        )
        if staged is None:
            raise RamdiskGuardError(f"Drop-in-Staging fehlt: {record['unit']}")
        if not _same_preimage(record.get("staged_preimage"), staged):
            raise RamdiskGuardError(f"Drop-in-Staging driftete: {record['unit']}")
        directory_fd = _record_directory_fd(
            root_fd,
            record,
            create_missing=True,
        )
        if directory_fd is None:
            raise RamdiskGuardError(f"Drop-in-Verzeichnis fehlt: {record['unit']}")
        try:
            try:
                target_changed = _atomic_write_managed_dropin(
                    directory_fd,
                    RAMDISK_DROPIN_NAME,
                    staged[1],
                    expected_preimage=record.get("preimage"),
                )
            except Exception:
                observed = _read_regular_file_nofollow(
                    directory_fd,
                    RAMDISK_DROPIN_NAME,
                )
                if (
                    not _same_preimage(record.get("preimage"), observed)
                    and observed is not None
                    and observed[1] == staged[1]
                ):
                    record["target_mutation_observed"] = True
                    record["committed_preimage"] = observed
                raise
            if target_changed:
                record["target_mutation_observed"] = True
            installed = _read_regular_file_nofollow(
                directory_fd,
                RAMDISK_DROPIN_NAME,
            )
            if installed is None or installed[1] != staged[1]:
                raise RamdiskGuardError(
                    f"Drop-in fehlt nach Bundle-Commit: {record['unit']}"
                )
            record["committed_preimage"] = installed
        finally:
            os.close(directory_fd)
        committed.append(str(record["unit"]))
    return tuple(committed)


def _atomic_restore_dropin_preimage(
    directory_fd: int,
    filename: str,
    preimage: tuple[os.stat_result, bytes],
    expected_current: tuple[os.stat_result, bytes],
) -> None:
    metadata, payload = preimage
    temporary = f".{filename}.{os.getpid()}.{secrets.token_hex(6)}.restore"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | _nofollow_flag()
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(temporary, flags, 0o600, dir_fd=directory_fd)
    try:
        _write_all(descriptor, payload)
        os.fchown(descriptor, metadata.st_uid, metadata.st_gid)
        os.fchmod(descriptor, stat.S_IMODE(metadata.st_mode))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    try:
        rebound = _read_regular_file_nofollow(directory_fd, filename)
        if not _same_preimage(expected_current, rebound):
            raise RamdiskGuardError(f"Drop-in driftete vor Restore: {filename}")
        os.replace(
            temporary,
            filename,
            src_dir_fd=directory_fd,
            dst_dir_fd=directory_fd,
        )
        os.fsync(directory_fd)
    finally:
        try:
            os.unlink(temporary, dir_fd=directory_fd)
        except FileNotFoundError:
            pass
    restored = _read_regular_file_nofollow(directory_fd, filename)
    if restored is None or (
        restored[1] != payload
        or restored[0].st_uid != metadata.st_uid
        or restored[0].st_gid != metadata.st_gid
        or stat.S_IMODE(restored[0].st_mode) != stat.S_IMODE(metadata.st_mode)
    ):
        raise RamdiskGuardError(f"Drop-in-Preimage nicht restauriert: {filename}")


def _rollback_dropin_record(
    root_fd: int,
    record: dict[str, object],
    payload: bytes,
) -> None:
    directory_name = str(record["directory_name"])
    descriptor = _open_existing_dropin_directory(root_fd, directory_name)
    if descriptor is None:
        if record.get("directory_existed"):
            raise RamdiskGuardError(f"Drop-in-Verzeichnis fehlt beim Rollback: {directory_name}")
        return
    try:
        if record.get("directory_existed") and (
            _directory_identity(os.fstat(descriptor)) != record.get("directory_identity")
        ):
            raise RamdiskGuardError(f"Drop-in-Verzeichnis driftete beim Rollback: {directory_name}")
        if not record.get("directory_existed"):
            if not record.get("directory_created"):
                raise RamdiskGuardError(
                    f"Fremdes Drop-in-Verzeichnis bleibt unangetastet: {directory_name}"
                )
            if _directory_identity(os.fstat(descriptor)) != record.get(
                "created_directory_identity"
            ):
                raise RamdiskGuardError(
                    f"Neu erzeugtes Drop-in-Verzeichnis driftete: {directory_name}"
                )
        current = _read_regular_file_nofollow(descriptor, RAMDISK_DROPIN_NAME)
        preimage = record.get("preimage")
        if _same_preimage(preimage, current) or _same_preimage_semantic(
            preimage,
            current,
        ):
            pass
        else:
            committed_preimage = record.get("committed_preimage")
            if not _same_preimage(committed_preimage, current):
                raise RamdiskGuardError(
                    f"Drop-in-Endzustand ist fremd und bleibt unangetastet: "
                    f"{record['unit']}"
                )
            if current is None or current[1] != payload:
                raise RamdiskGuardError(
                    f"Drop-in-Endzustand ist nicht rückrollbar: {record['unit']}"
                )
            if preimage is None:
                os.unlink(RAMDISK_DROPIN_NAME, dir_fd=descriptor)
                os.fsync(descriptor)
            else:
                _atomic_restore_dropin_preimage(
                    descriptor,
                    RAMDISK_DROPIN_NAME,
                    preimage,
                    current,
                )
        if record.get("directory_existed"):
            final = _read_regular_file_nofollow(descriptor, RAMDISK_DROPIN_NAME)
            if not _same_preimage_semantic(preimage, final):
                raise RamdiskGuardError(f"Drop-in-Rollback unvollständig: {record['unit']}")
            return
        entries = os.listdir(descriptor)
        if entries:
            raise RamdiskGuardError(
                f"Neu erzeugtes Drop-in-Verzeichnis ist nicht leer: {directory_name}"
            )
    finally:
        os.close(descriptor)
    os.rmdir(directory_name, dir_fd=root_fd)
    os.fsync(root_fd)
    rebound = _open_existing_dropin_directory(root_fd, directory_name)
    if rebound is not None:
        os.close(rebound)
        raise RamdiskGuardError(
            f"Neu erzeugtes Drop-in-Verzeichnis blieb zurück: {directory_name}"
        )


def _cleanup_stage_bundle(
    root_fd: int,
    stage_name: str | None,
    stage_identity: tuple[int, ...] | None,
    records: Sequence[dict[str, object]],
) -> None:
    if not stage_name:
        return
    if stage_identity is None:
        raise RamdiskGuardError("Drop-in-Staging besitzt keine gebundene Identität")
    stage_fd = _open_existing_dropin_directory(root_fd, stage_name)
    if stage_fd is None:
        return
    try:
        if _directory_identity(os.fstat(stage_fd)) != stage_identity:
            raise RamdiskGuardError("Drop-in-Staging driftete vor der Bereinigung")
        expected = {
            f"{record['unit']}.staged": record
            for record in records
            if record.get("changed")
        }
        actual = set(os.listdir(stage_fd))
        if not actual.issubset(set(expected)):
            raise RamdiskGuardError("Drop-in-Staging enthält fremde Einträge")
        for filename in actual:
            record = expected[filename]
            staged = _read_regular_file_nofollow(stage_fd, filename)
            if (
                not record.get("stage_created")
                or staged is None
                or _file_node_identity(staged[0])
                != record.get("staged_node_identity")
            ):
                raise RamdiskGuardError(
                    f"Fremdes Drop-in-Staging bleibt unangetastet: {filename}"
                )
            os.unlink(filename, dir_fd=stage_fd)
        os.fsync(stage_fd)
    finally:
        os.close(stage_fd)
    os.rmdir(stage_name, dir_fd=root_fd)
    os.fsync(root_fd)
    rebound = _open_existing_dropin_directory(root_fd, stage_name)
    if rebound is not None:
        os.close(rebound)
        raise RamdiskGuardError("Drop-in-Staging blieb nach der Bereinigung zurück")


def _rollback_dropin_bundle(
    *,
    root_fd: int,
    records: Sequence[dict[str, object]],
    payload: bytes,
    stage_name: str | None,
    stage_identity: tuple[int, ...] | None,
    runner: Callable[..., object],
    reload_systemd: bool,
    mutation_started: bool,
) -> dict[str, object]:
    errors: list[str] = []
    for record in reversed(tuple(records)):
        if not record.get("changed"):
            continue
        try:
            _rollback_dropin_record(root_fd, record, payload)
        except Exception as exc:
            errors.append(f"{record['unit']}: {exc}")
    try:
        _cleanup_stage_bundle(root_fd, stage_name, stage_identity, records)
    except Exception as exc:
        errors.append(f"Staging: {exc}")
    try:
        _verify_dropin_rollback_state(root_fd, records)
    except Exception as exc:
        errors.append(f"Gesamtpreimage: {exc}")
    daemon_reload: dict[str, object] = {
        "ok": True,
        "skipped": True,
        "reason": "no_mutation",
    }
    if mutation_started and reload_systemd:
        daemon_reload = _run_daemon_reload(runner)
        if not daemon_reload.get("ok"):
            errors.append("systemd daemon-reload nach Drop-in-Rollback fehlgeschlagen")
    return {
        "ok": not errors,
        "errors": tuple(errors),
        "daemon_reload": daemon_reload,
    }


def _normalise_units(units: Iterable[str] | None) -> tuple[str, ...]:
    selected = tuple(RAMDISK_SERVICE_UNITS if units is None else units)
    if (
        not selected
        or len(selected) != len(set(selected))
        or any(unit not in RAMDISK_SERVICE_UNITS for unit in selected)
    ):
        raise RamdiskGuardError("Drop-in-Ziele verletzen die feste Service-Positivliste")
    return selected


def _run_daemon_reload(
    runner: Callable[..., object],
) -> dict[str, object]:
    argv = (SYSTEMCTL_PATH, "daemon-reload")
    try:
        result = runner(
            list(argv),
            capture_output=True,
            text=True,
            check=False,
            timeout=20,
        )
        returncode = int(getattr(result, "returncode"))
        return {
            "ok": returncode == 0,
            "command": argv,
            "returncode": returncode,
            "stdout": str(getattr(result, "stdout", "") or "").strip(),
            "stderr": str(getattr(result, "stderr", "") or "").strip(),
        }
    except Exception as exc:
        return {
            "ok": False,
            "command": argv,
            "returncode": -1,
            "stdout": "",
            "stderr": str(exc),
        }


def ensure_ramdisk_service_dropins(
    *,
    units: Sequence[str] | None = None,
    systemd_dir: str = SYSTEMD_CONFIG_DIR,
    reload_systemd: bool = True,
    runner: Callable[..., object] = subprocess.run,
    container_mode: bool | None = None,
) -> dict[str, object]:
    """Installiert alle Drop-ins als ein rückrollbares Preimage-/Reload-Bundle."""

    if container_mode is None:
        container_mode = is_docker_environment()
    if container_mode:
        return {
            "success": True,
            "skipped": True,
            "reason": "docker_uses_entrypoint_tmpfs_gate",
            "changed": (),
            "unchanged": (),
            "rollback": {"ok": True, "needed": False},
        }

    changed: list[str] = []
    unchanged: list[str] = []
    records: list[dict[str, object]] = []
    payload = render_ramdisk_service_dropin().encode("utf-8")
    root_fd: int | None = None
    stage_fd: int | None = None
    stage_name: str | None = None
    stage_identity: tuple[int, ...] | None = None
    mutation_started = False
    committed: tuple[str, ...] = ()
    daemon_reload: dict[str, object] = {
        "ok": True,
        "skipped": True,
        "reason": "unchanged",
    }
    try:
        selected = _normalise_units(units)
        root_fd = _open_trusted_directory(systemd_dir)
        records = _capture_dropin_records(root_fd, selected, payload)
        changed = [str(record["unit"]) for record in records if record.get("changed")]
        unchanged = [str(record["unit"]) for record in records if not record.get("changed")]
        if not changed:
            return {
                "success": True,
                "skipped": False,
                "changed": (),
                "unchanged": tuple(unchanged),
                "daemon_reload": daemon_reload,
                "rollback": {"ok": True, "needed": False},
                "dropin_name": RAMDISK_DROPIN_NAME,
            }

        stage_candidate = (
            f".e3dc-ramdisk-dropins-{os.getpid()}-{secrets.token_hex(8)}"
        )
        os.mkdir(stage_candidate, 0o700, dir_fd=root_fd)
        # Erst nach erfolgreichem mkdir gehört genau dieser Name zur
        # Transaktion; eine vorbestehende Kollision darf nie bereinigt werden.
        stage_name = stage_candidate
        stage_fd = _open_created_stage_bundle(root_fd, stage_name)
        stage_identity = _directory_identity(os.fstat(stage_fd))
        _stage_bundle_payloads(stage_fd, records, payload)
        os.close(stage_fd)
        stage_fd = None

        stage_fd = _open_existing_dropin_directory(root_fd, stage_name)
        if stage_fd is None:
            raise RamdiskGuardError("Drop-in-Staging verschwand vor dem Commit")
        if _directory_identity(os.fstat(stage_fd)) != stage_identity:
            raise RamdiskGuardError("Drop-in-Staging-Verzeichnis driftete vor Commit")
        _verify_stage_bundle(stage_fd, records)
        # Gesamter Ziel-Prestate wird nach abgeschlossenem und erneut
        # gebundenem Staging unmittelbar vor der ersten Zielmutation geprüft.
        _verify_dropin_prestate(root_fd, records)
        mutation_started = True
        committed = _install_staged_bundle(root_fd, stage_fd, records)
        os.close(stage_fd)
        stage_fd = None
        if tuple(changed) != committed:
            raise RamdiskGuardError("Drop-in-Bundle wurde nicht vollständig committed")
        _verify_dropin_commit_state(root_fd, records)

        daemon_reload = {
            "ok": True,
            "skipped": True,
            "reason": "caller_bound_reload",
        }
        if reload_systemd:
            daemon_reload = _run_daemon_reload(runner)
            if not daemon_reload.get("ok"):
                raise RamdiskGuardError(
                    "systemd daemon-reload nach Drop-in-Bundle fehlgeschlagen"
                )
        _verify_dropin_commit_state(root_fd, records)

        # Auch Staging gehört zur Transaktion. Scheitert dessen vollständige
        # Entfernung, wird das bereits installierte Bundle zurückgerollt.
        _cleanup_stage_bundle(root_fd, stage_name, stage_identity, records)
        stage_name = None
        stage_identity = None
        return {
            "success": True,
            "skipped": False,
            "changed": tuple(changed),
            "unchanged": tuple(unchanged),
            "daemon_reload": daemon_reload,
            "rollback": {"ok": True, "needed": False},
            "dropin_name": RAMDISK_DROPIN_NAME,
        }
    except Exception as exc:
        if stage_fd is not None:
            os.close(stage_fd)
            stage_fd = None
        if root_fd is None:
            rollback = {
                "ok": True,
                "errors": (),
                "daemon_reload": {
                    "ok": True,
                    "skipped": True,
                    "reason": "no_mutation",
                },
            }
        else:
            rollback = _rollback_dropin_bundle(
                root_fd=root_fd,
                records=records,
                payload=payload,
                stage_name=stage_name,
                stage_identity=stage_identity,
                runner=runner,
                reload_systemd=reload_systemd,
                mutation_started=mutation_started,
            )
        return {
            "success": False,
            "skipped": False,
            "error": str(exc),
            "changed": tuple(changed),
            "unchanged": tuple(unchanged),
            "committed_before_rollback": committed,
            "daemon_reload": daemon_reload,
            "rollback": rollback,
        }
    finally:
        if stage_fd is not None:
            os.close(stage_fd)
        if root_fd is not None:
            os.close(root_fd)


def require_ramdisk_service_dropins(**kwargs) -> dict[str, object]:
    """Hebt einen unvollständigen Install-/Updatevertrag als harten Fehler."""

    result = ensure_ramdisk_service_dropins(**kwargs)
    if not result.get("success"):
        raise RamdiskGuardError(
            str(result.get("error") or "RAM-Disk-Drop-ins konnten nicht installiert werden")
        )
    return result


def dropin_paths(
    *,
    systemd_dir: str = SYSTEMD_CONFIG_DIR,
) -> tuple[Path, ...]:
    """Liefert ausschließlich die verwalteten Pfade der festen Positivliste."""

    root = Path(systemd_dir)
    return tuple(
        root / f"{unit}.d" / RAMDISK_DROPIN_NAME
        for unit in RAMDISK_SERVICE_UNITS
    )
