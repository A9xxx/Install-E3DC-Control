#!/usr/bin/env python3
"""Verifizierte Offline-Vorbereitung für kurze E3DC-Update-Umschaltungen.

Das Modul trennt bewusst zwei Phasen:

* Vor der Diensteruhe dürfen die von :func:`create_preparation_plan`
  gelieferten Download-Kommandos ausgeführt werden.
* Nach dem Freeze dürfen ausschließlich die von
  :func:`build_offline_install_commands` erzeugten lokalen Kommandos laufen.

Es startet selbst keine Prozesse. Der Ziel-Updater übergibt seinen gebundenen
argv-Runner ausdrücklich an :func:`execute_preparation`. Dadurch bleibt die
Netzwerkgrenze testbar und ein Import dieses Moduls ist nebenwirkungsfrei.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
import os
from pathlib import Path
import re
import shlex
import stat
from typing import Callable, Mapping, Sequence


DISK_ERROR_CODE = "E3DC-UPD-DISK-001"
PREPARATION_ERROR_CODE = "E3DC-UPD-OFFLINE-001"
MANIFEST_SCHEMA = "e3dc_update_offline_cache_v1"
MANIFEST_NAME = "offline-preflight-manifest.json"
MANIFEST_DIGEST_NAME = "offline-preflight-manifest.sha256"
DEFAULT_CACHE_PARENT = "/var/cache/e3dc-control/update-offline"
WHEEL_MIRROR_SCHEMA = "e3dc_update_offline_wheel_mirror_v1"
WHEEL_MIRROR_MANIFEST_NAME = "wheel-mirror-manifest.json"
WHEEL_MIRROR_DIGEST_NAME = "wheel-mirror-manifest.sha256"
DEFAULT_WHEEL_MIRROR_PARENT = "/var/cache/e3dc-update-offline-mirror"
RECEIPT_SCHEMA = "e3dc_update_offline_receipt_v1"
TRANSACTION_RE = re.compile(r"[0-9a-f]{64}\Z")
PACKAGE_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.+-]{0,127}\Z")
SHA256_RE = re.compile(r"[0-9a-f]{64}\Z")
MIN_HEADROOM_BYTES = 64 * 1024 * 1024
DEFAULT_RESERVE_BYTES = 512 * 1024 * 1024
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_RECEIPT_BYTES = 1024 * 1024


class OfflinePreflightError(RuntimeError):
    """Strukturierter Abbruchvertrag für Web, Konsole und Protokoll."""

    def __init__(self, code: str, message: str, solution: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.solution = str(solution)

    def __str__(self) -> str:
        return (
            f"[ABBRUCH] {self.code}\n"
            f"Was ist passiert: {self.message}\n"
            f"Lösung: {self.solution}"
        )


@dataclass(frozen=True)
class DiskSpaceReceipt:
    path: str
    available_bytes: int
    payload_bytes: int
    backup_bytes: int
    working_bytes: int
    headroom_bytes: int
    reserve_bytes: int
    required_bytes: int


@dataclass(frozen=True)
class OfflineCacheIdentity:
    transaction_id: str
    root: str
    device: int
    inode: int


@dataclass(frozen=True)
class OfflineWheelMirrorReceipt:
    transaction_id: str
    root: str
    device: int
    inode: int
    source_manifest_sha256: str
    manifest_path: str
    manifest_sha256: str
    wheels: tuple[str, ...]


@dataclass(frozen=True)
class OfflinePreparationPlan:
    cache: OfflineCacheIdentity
    apt_packages: tuple[str, ...]
    pip_packages: tuple[str, ...]
    expected_venv_state: str
    apt_download_argv: tuple[str, ...] | None
    apt_offline_probe_argv: tuple[str, ...] | None
    pip_download_argv: tuple[str, ...] | None

    @property
    def pre_freeze_argvs(self) -> tuple[tuple[str, ...], ...]:
        """Download- und Prüfkommandos, die zwingend vor dem Freeze laufen."""

        return tuple(
            command
            for command in (
                self.apt_download_argv,
                self.pip_download_argv,
                self.apt_offline_probe_argv,
            )
            if command is not None
        )


@dataclass(frozen=True)
class OfflinePackageReceipt:
    cache: OfflineCacheIdentity
    manifest_path: str
    manifest_sha256: str
    apt_packages: tuple[str, ...]
    pip_packages: tuple[str, ...]
    expected_venv_state: str
    apt_archives: tuple[str, ...]
    pip_wheels: tuple[str, ...]
    wheel_mirror: OfflineWheelMirrorReceipt | None = None


@dataclass(frozen=True)
class OfflineInstallCommands:
    apt_install_argv: tuple[str, ...] | None
    dpkg_fallback_argv: tuple[str, ...] | None
    pip_install_argv: tuple[str, ...] | None

    @property
    def all_argvs(self) -> tuple[tuple[str, ...], ...]:
        return tuple(
            command
            for command in (
                self.apt_install_argv,
                self.dpkg_fallback_argv,
                self.pip_install_argv,
            )
            if command is not None
        )


def _expected_root_uid() -> int:
    return 0


def _expected_root_gid() -> int:
    return 0


def _require_root() -> None:
    if not hasattr(os, "geteuid") or os.geteuid() != _expected_root_uid():
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Der Offline-Paketcache darf ausschließlich durch Root vorbereitet werden.",
            "Starte den aktuellen Update-Bootstrap mit sudo erneut.",
        )


def _nonnegative_bytes(name: str, raw: object) -> int:
    if isinstance(raw, bool):
        raise ValueError(f"{name} muss eine nichtnegative Ganzzahl sein")
    try:
        value = int(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} muss eine nichtnegative Ganzzahl sein") from exc
    if value < 0:
        raise ValueError(f"{name} muss eine nichtnegative Ganzzahl sein")
    return value


def _strict_nonnegative_int(name: str, raw: object) -> int:
    if isinstance(raw, bool) or not isinstance(raw, int) or raw < 0:
        raise ValueError(f"{name} muss eine nichtnegative Ganzzahl sein")
    return raw


def _human_bytes(raw: int) -> str:
    value = float(max(0, int(raw)))
    units = ("B", "KiB", "MiB", "GiB", "TiB")
    for unit in units:
        if value < 1024.0 or unit == units[-1]:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TiB"


def require_conservative_free_space(
    path: str | os.PathLike[str],
    *,
    payload_bytes: int,
    backup_bytes: int = 0,
    working_bytes: int = 0,
    reserve_bytes: int = DEFAULT_RESERVE_BYTES,
    headroom_ratio: float = 0.25,
    statvfs_fn: Callable[[str], os.statvfs_result] = os.statvfs,
) -> DiskSpaceReceipt:
    """Prüft mit ``f_bavail``, Reserve und zusätzlichem Arbeitsaufschlag.

    ``required_bytes`` ist absichtlich nicht direkt übergebbar: Der Aufrufer
    muss Nutzlast, Backup und temporäre Arbeitskopien getrennt benennen. So
    kann keine große Updatekomponente versehentlich aus der Schätzung fallen.
    """

    raw_path = os.fspath(path)
    if not isinstance(raw_path, str):
        raise ValueError("Speicherprüfpfad muss Text sein")
    candidate = os.path.abspath(raw_path)
    if not os.path.isabs(candidate) or any(char in candidate for char in "\x00\r\n"):
        raise ValueError("Speicherprüfpfad ist nicht kanonisch")
    payload = _nonnegative_bytes("payload_bytes", payload_bytes)
    backup = _nonnegative_bytes("backup_bytes", backup_bytes)
    working = _nonnegative_bytes("working_bytes", working_bytes)
    reserve = _nonnegative_bytes("reserve_bytes", reserve_bytes)
    if not isinstance(headroom_ratio, (int, float)) or not math.isfinite(headroom_ratio):
        raise ValueError("headroom_ratio ist ungültig")
    if float(headroom_ratio) < 0.0 or float(headroom_ratio) > 2.0:
        raise ValueError("headroom_ratio muss zwischen 0 und 2 liegen")
    estimate = payload + backup + working
    proportional = math.ceil(estimate * float(headroom_ratio))
    headroom = max(MIN_HEADROOM_BYTES if estimate else 0, proportional)
    required = estimate + headroom + reserve
    try:
        filesystem = statvfs_fn(candidate)
        fragment_size = int(filesystem.f_frsize or filesystem.f_bsize)
        if fragment_size <= 0 or int(filesystem.f_bavail) < 0:
            raise ValueError("ungültige statvfs-Werte")
        available = int(filesystem.f_bavail) * fragment_size
    except (OSError, TypeError, ValueError, AttributeError) as exc:
        raise OfflinePreflightError(
            DISK_ERROR_CODE,
            f"Freier Speicherplatz ist für {candidate} nicht zuverlässig lesbar.",
            f"Prüfe den Datenträger mit: df -h -- {shlex.quote(candidate)}. "
            "Korrigiere Pfad, Einhängung oder Dateisystemfehler und starte danach "
            "denselben Updatebefehl erneut; es wurden noch keine Dienste gestoppt.",
        ) from exc
    receipt = DiskSpaceReceipt(
        path=candidate,
        available_bytes=available,
        payload_bytes=payload,
        backup_bytes=backup,
        working_bytes=working,
        headroom_bytes=headroom,
        reserve_bytes=reserve,
        required_bytes=required,
    )
    if available < required:
        shortfall = required - available
        raise OfflinePreflightError(
            DISK_ERROR_CODE,
            f"Auf {candidate} sind {_human_bytes(available)} frei; für Backup, "
            f"Updatevorbereitung und Sicherheitsreserve werden mindestens "
            f"{_human_bytes(required)} benötigt.",
            f"Gib mindestens {_human_bytes(shortfall)} auf diesem Dateisystem frei. "
            f"Prüfen mit: df -h -- {shlex.quote(candidate)}. Danach denselben "
            "Updatebefehl erneut starten; es wurden noch keine Dienste gestoppt.",
        )
    return receipt


def _canonical_absolute_path(raw: str | os.PathLike[str], *, label: str) -> str:
    text = os.fspath(raw)
    if (
        not isinstance(text, str)
        or not text
        or any(char in text for char in "\x00\r\n\t")
        or not os.path.isabs(text)
    ):
        raise ValueError(f"{label} muss ein absoluter Pfad ohne Steuerzeichen sein")
    normalized = os.path.abspath(text)
    if normalized != text or normalized in {"/", "/var", "/var/cache"}:
        raise ValueError(f"{label} ist nicht kanonisch oder zu weit gefasst")
    return normalized


def _validate_transaction_id(raw: object) -> str:
    value = str(raw or "").strip().lower()
    if not TRANSACTION_RE.fullmatch(value):
        raise ValueError("Transaktions-ID muss aus genau 64 Hexzeichen bestehen")
    return value


def _normalize_packages(raw: Sequence[str], *, label: str) -> tuple[str, ...]:
    if isinstance(raw, (str, bytes)):
        raise ValueError(f"{label} muss eine Paketliste sein")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str) or not PACKAGE_RE.fullmatch(item):
            raise ValueError(f"Ungültiger Paketname in {label}: {item!r}")
        if item in result:
            raise ValueError(f"Doppelter Paketname in {label}: {item}")
        result.append(item)
    return tuple(result)


def _validate_ancestor_directory(path: str, metadata: os.stat_result) -> None:
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != _expected_root_uid()
        or metadata.st_gid != _expected_root_gid()
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            f"Cache-Elternpfad ist nicht root-kontrolliert: {path}",
            "Korrigiere Besitzer und Schreibrechte des genannten Pfads oder entferne "
            "einen dort vorhandenen Symlink; starte danach das Update erneut.",
        )


def _open_secure_directory(
    path: str,
    *,
    create: bool,
    leaf_mode: int,
) -> tuple[int, os.stat_result]:
    if leaf_mode not in {0o700, 0o755}:
        raise ValueError("Sicherer Verzeichnismodus muss 0700 oder 0755 sein")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    if not nofollow or not directory:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Das Betriebssystem unterstützt keinen sicheren Offline-Cachepfad.",
            "Aktualisiere das Raspberry-Pi-Betriebssystem und starte das Update erneut.",
        )
    flags = os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open("/", flags)
    current = ""
    try:
        for component in Path(path).parts[1:]:
            current = current + "/" + component
            try:
                child = os.open(component, flags, dir_fd=descriptor)
            except FileNotFoundError:
                if not create:
                    raise
                os.mkdir(component, leaf_mode, dir_fd=descriptor)
                child = os.open(component, flags, dir_fd=descriptor)
                os.fchmod(child, leaf_mode)
            try:
                metadata = os.fstat(child)
                named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
                if (
                    metadata.st_dev,
                    metadata.st_ino,
                    metadata.st_mode,
                    metadata.st_uid,
                    metadata.st_gid,
                ) != (
                    named.st_dev,
                    named.st_ino,
                    named.st_mode,
                    named.st_uid,
                    named.st_gid,
                ):
                    raise OfflinePreflightError(
                        PREPARATION_ERROR_CODE,
                        f"Cachepfad driftete beim Öffnen: {current}",
                        "Entferne den unsicheren Cachepfad und starte das Update erneut.",
                    )
                _validate_ancestor_directory(current, metadata)
            except Exception:
                os.close(child)
                raise
            os.close(descriptor)
            descriptor = child
        final = os.fstat(descriptor)
        if (
            final.st_uid != _expected_root_uid()
            or final.st_gid != _expected_root_gid()
            or stat.S_IMODE(final.st_mode) != leaf_mode
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Verzeichnis besitzt nicht den gebundenen Root-Modus "
                f"{leaf_mode:04o}: {path}",
                f"Setze für das Verzeichnis root:root und Modus {leaf_mode:04o} oder "
                "entferne es; starte danach das Update erneut.",
            )
        return descriptor, final
    except Exception:
        os.close(descriptor)
        raise


def _create_root_child(
    parent_descriptor: int,
    name: str,
    *,
    mode: int,
) -> tuple[int, os.stat_result]:
    if not name or "/" in name or name in {".", ".."}:
        raise ValueError("Root-kontrollierter Unterpfad ist ungültig")
    if mode not in {0o700, 0o755}:
        raise ValueError("Root-kontrollierter Unterpfad verlangt Modus 0700 oder 0755")
    try:
        os.mkdir(name, mode, dir_fd=parent_descriptor)
    except FileExistsError as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            f"Transaktionsgebundener Cache-Unterpfad existiert bereits: {name}",
            "Verwende eine neue Transaktions-ID oder bereinige den alten Cache mit dem "
            "zugehörigen Receipt.",
        ) from exc
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = -1
    try:
        descriptor = os.open(name, flags, dir_fd=parent_descriptor)
        os.fchmod(descriptor, mode)
        metadata = os.fstat(descriptor)
        named = os.stat(name, dir_fd=parent_descriptor, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != _expected_root_uid()
            or metadata.st_gid != _expected_root_gid()
            or stat.S_IMODE(metadata.st_mode) != mode
            or (metadata.st_dev, metadata.st_ino) != (named.st_dev, named.st_ino)
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Cache-Unterpfad besitzt nach Anlage nicht den Root-Modus "
                f"{mode:04o}: {name}",
                "Entferne den unvollständigen Cache und starte das Update erneut.",
            )
        return descriptor, metadata
    except Exception:
        if descriptor >= 0:
            os.close(descriptor)
        try:
            os.rmdir(name, dir_fd=parent_descriptor)
        except OSError:
            pass
        raise


def create_offline_cache(
    transaction_id: str,
    *,
    cache_parent: str = DEFAULT_CACHE_PARENT,
) -> OfflineCacheIdentity:
    """Erzeugt genau einen neuen root-privaten Cache für eine Transaktion."""

    _require_root()
    transaction = _validate_transaction_id(transaction_id)
    parent = _canonical_absolute_path(cache_parent, label="Cache-Elternpfad")
    parent_descriptor, _parent_metadata = _open_secure_directory(
        parent,
        create=True,
        leaf_mode=0o700,
    )
    cache_descriptor = -1
    created = False
    try:
        cache_descriptor, metadata = _create_root_child(
            parent_descriptor,
            transaction,
            mode=0o700,
        )
        created = True
        for name in ("apt", "wheels"):
            child, _child_metadata = _create_root_child(
                cache_descriptor,
                name,
                mode=0o700,
            )
            try:
                if name == "apt":
                    partial, _partial_metadata = _create_root_child(
                        child,
                        "partial",
                        mode=0o700,
                    )
                    os.close(partial)
            finally:
                os.close(child)
        return OfflineCacheIdentity(
            transaction_id=transaction,
            root=os.path.join(parent, transaction),
            device=int(metadata.st_dev),
            inode=int(metadata.st_ino),
        )
    except Exception:
        if created and cache_descriptor >= 0:
            try:
                _purge_directory_contents(cache_descriptor)
                os.rmdir(transaction, dir_fd=parent_descriptor)
            except OSError:
                pass
        raise
    finally:
        if cache_descriptor >= 0:
            os.close(cache_descriptor)
        os.close(parent_descriptor)


def _cache_paths(cache: OfflineCacheIdentity) -> tuple[str, str]:
    return os.path.join(cache.root, "apt"), os.path.join(cache.root, "wheels")


def create_preparation_plan(
    transaction_id: str,
    *,
    apt_packages: Sequence[str] = (),
    pip_packages: Sequence[str] = (),
    expected_venv_state: str = "present",
    download_python: str = "/usr/bin/python3",
    cache_parent: str = DEFAULT_CACHE_PARENT,
) -> OfflinePreparationPlan:
    """Erzeugt Cache und argv-Vertrag, führt aber noch keinen Prozess aus."""

    apt = _normalize_packages(apt_packages, label="apt_packages")
    pip = _normalize_packages(pip_packages, label="pip_packages")
    if expected_venv_state not in {"present", "missing"}:
        raise ValueError("expected_venv_state muss present oder missing sein")
    python = _validate_python_executable(download_python)
    cache = create_offline_cache(transaction_id, cache_parent=cache_parent)
    apt_directory, wheel_directory = _cache_paths(cache)
    apt_common = (
        "/usr/bin/apt-get",
        "-o",
        f"Dir::Cache::archives={apt_directory}",
        "-o",
        "APT::Sandbox::User=root",
    )
    apt_download = (
        *apt_common,
        "install",
        "--download-only",
        "--no-remove",
        "--no-upgrade",
        "--no-install-recommends",
        "-y",
        "--",
        *apt,
    ) if apt else None
    apt_probe = (
        *apt_common,
        "install",
        "--simulate",
        "--no-download",
        "--no-remove",
        "--no-upgrade",
        "--no-install-recommends",
        "-y",
        "--",
        *apt,
    ) if apt else None
    pip_download_values: list[str] | None = None
    if pip:
        pip_download_values = [
            python,
            "-I",
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--dest",
            wheel_directory,
            "--only-binary=:all:",
        ]
        if expected_venv_state == "present":
            pip_download_values.append("--no-deps")
        else:
            pip_download_values.append("--prefer-binary")
        pip_download_values.extend(("--", *pip))
    return OfflinePreparationPlan(
        cache=cache,
        apt_packages=apt,
        pip_packages=pip,
        expected_venv_state=expected_venv_state,
        apt_download_argv=tuple(apt_download) if apt_download else None,
        apt_offline_probe_argv=tuple(apt_probe) if apt_probe else None,
        pip_download_argv=tuple(pip_download_values) if pip_download_values else None,
    )


def _runner_succeeded(result: object) -> bool:
    if isinstance(result, Mapping):
        success = result.get("success")
        if isinstance(success, bool):
            return success
        return_code = result.get("returncode")
        return (
            isinstance(return_code, int)
            and not isinstance(return_code, bool)
            and return_code == 0
        )
    return_code = getattr(result, "returncode", None)
    return isinstance(return_code, int) and not isinstance(return_code, bool) and return_code == 0


def _validate_preparation_plan_contract(plan: OfflinePreparationPlan) -> None:
    if not isinstance(plan, OfflinePreparationPlan):
        raise TypeError("Offline-Vorbereitungsplan fehlt")
    try:
        apt = _normalize_packages(plan.apt_packages, label="plan.apt_packages")
        pip = _normalize_packages(plan.pip_packages, label="plan.pip_packages")
    except (TypeError, ValueError) as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Vorbereitungsplan enthält eine ungültige Paketliste.",
            "Erzeuge den Plan mit create_preparation_plan erneut.",
        ) from exc
    if plan.expected_venv_state not in {"present", "missing"}:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Vorbereitungsplan besitzt keinen gültigen venv-Ausgangszustand.",
            "Erzeuge den Plan mit create_preparation_plan erneut.",
        )
    apt_directory, wheel_directory = _cache_paths(plan.cache)
    apt_common = (
        "/usr/bin/apt-get",
        "-o",
        f"Dir::Cache::archives={apt_directory}",
        "-o",
        "APT::Sandbox::User=root",
    )
    expected_apt_download = (
        *apt_common,
        "install",
        "--download-only",
        "--no-remove",
        "--no-upgrade",
        "--no-install-recommends",
        "-y",
        "--",
        *apt,
    ) if apt else None
    expected_apt_probe = (
        *apt_common,
        "install",
        "--simulate",
        "--no-download",
        "--no-remove",
        "--no-upgrade",
        "--no-install-recommends",
        "-y",
        "--",
        *apt,
    ) if apt else None
    expected_pip_download: tuple[str, ...] | None = None
    if pip:
        command = plan.pip_download_argv
        if not command:
            python = ""
        else:
            try:
                python = _validate_python_executable(command[0])
            except (TypeError, ValueError):
                python = ""
        values = [
            python,
            "-I",
            "-m",
            "pip",
            "download",
            "--disable-pip-version-check",
            "--dest",
            wheel_directory,
            "--only-binary=:all:",
            "--no-deps" if plan.expected_venv_state == "present" else "--prefer-binary",
            "--",
            *pip,
        ]
        expected_pip_download = tuple(values)
    if (
        apt != plan.apt_packages
        or pip != plan.pip_packages
        or plan.apt_download_argv != expected_apt_download
        or plan.apt_offline_probe_argv != expected_apt_probe
        or plan.pip_download_argv != expected_pip_download
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Vorbereitungsplan weicht vom gebundenen argv-Vertrag ab.",
            "Erzeuge den Plan mit create_preparation_plan erneut; führe keine "
            "manuell veränderten Paketkommandos aus.",
        )


def execute_preparation(
    plan: OfflinePreparationPlan,
    runner: Callable[..., object],
) -> OfflinePackageReceipt:
    """Führt ausschließlich die expliziten *Vor-Freeze*-argv über ``runner`` aus."""

    if not isinstance(plan, OfflinePreparationPlan) or not callable(runner):
        raise TypeError("Vorbereitungsplan und argv-Runner sind erforderlich")
    _validate_preparation_plan_contract(plan)
    commands = (
        (plan.apt_download_argv, 600),
        (plan.pip_download_argv, 600),
        (plan.apt_offline_probe_argv, 180),
    )
    for command, timeout in commands:
        if command is None:
            continue
        result = runner(list(command), timeout=timeout)
        if not _runner_succeeded(result):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Die Paketvorbereitung vor der Diensteruhe ist fehlgeschlagen: "
                + shlex.join(command[:6]),
                "Behebe Netzwerk-, Paketquellen- oder Paketauflösungsfehler. Die "
                "E3DC-Dienste wurden durch diesen Vorbereitungsschritt nicht gestoppt.",
            )
    return seal_preparation(plan)


def _file_sha256(descriptor: int, expected_size: int) -> str:
    digest = hashlib.sha256()
    os.lseek(descriptor, 0, os.SEEK_SET)
    remaining = int(expected_size)
    while remaining:
        block = os.read(descriptor, min(1024 * 1024, remaining))
        if not block:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Offline-Cachedatei endet vor ihrer gebundenen Größe.",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        digest.update(block)
        remaining -= len(block)
    if os.read(descriptor, 1):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cachedatei überschreitet ihre gebundene Größe.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        )
    return digest.hexdigest()


def _open_bound_cache(cache: OfflineCacheIdentity) -> tuple[int, os.stat_result]:
    if not isinstance(cache, OfflineCacheIdentity):
        raise TypeError("Offline-Cache-Identität fehlt")
    transaction = _validate_transaction_id(cache.transaction_id)
    root = _canonical_absolute_path(cache.root, label="Offline-Cachepfad")
    if os.path.basename(root) != transaction:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cachepfad und Transaktions-ID widersprechen sich.",
            "Verwende ausschließlich das bei der Vorbereitung erzeugte Cache-Receipt.",
        )
    descriptor, metadata = _open_secure_directory(
        root,
        create=False,
        leaf_mode=0o700,
    )
    if metadata.st_dev != cache.device or metadata.st_ino != cache.inode:
        os.close(descriptor)
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cache wurde seit seiner Anlage ersetzt.",
            "Verwirf den Cache und starte die Vorbereitung mit einer neuen Transaktion.",
        )
    return descriptor, metadata


def _normalize_cache_payload_file(path: str, *, suffix: str) -> dict[str, object]:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        named_before = os.stat(path, follow_symlinks=False)
        if (
            not path.endswith(suffix)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or (before.st_dev, before.st_ino) != (named_before.st_dev, named_before.st_ino)
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Cache enthält eine unsichere Paketdatei: {path}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        if before.st_uid != _expected_root_uid() or before.st_gid != _expected_root_gid():
            os.fchown(descriptor, _expected_root_uid(), _expected_root_gid())
        os.fchmod(descriptor, 0o600)
        normalized = os.fstat(descriptor)
        digest = _file_sha256(descriptor, normalized.st_size)
        after = os.fstat(descriptor)
        named_after = os.stat(path, follow_symlinks=False)
        signature = (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_uid,
            after.st_gid,
            stat.S_IMODE(after.st_mode),
            after.st_nlink,
        )
        if signature != (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_size,
            named_after.st_uid,
            named_after.st_gid,
            stat.S_IMODE(named_after.st_mode),
            named_after.st_nlink,
        ) or signature[3:] != (_expected_root_uid(), _expected_root_gid(), 0o600, 1):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Cachedatei driftete beim Versiegeln: {path}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        return {
            "size": int(after.st_size),
            "sha256": digest,
            "mode": 0o600,
            "uid": int(after.st_uid),
            "gid": int(after.st_gid),
        }
    finally:
        os.close(descriptor)


def _payload_records(plan: OfflinePreparationPlan) -> list[dict[str, object]]:
    apt_directory, wheel_directory = _cache_paths(plan.cache)
    records: list[dict[str, object]] = []
    partial = os.path.join(apt_directory, "partial")
    for directory in (apt_directory, partial, wheel_directory):
        metadata = os.stat(directory, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != _expected_root_uid()
            or metadata.st_gid != _expected_root_gid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Cache-Unterverzeichnis ist nicht root-privat: {directory}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
    apt_lock = os.path.join(apt_directory, "lock")
    try:
        lock_metadata = os.stat(apt_lock, follow_symlinks=False)
    except FileNotFoundError:
        lock_metadata = None
    if lock_metadata is not None:
        if not stat.S_ISREG(lock_metadata.st_mode) or lock_metadata.st_nlink != 1:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "APT-Cachelock besitzt einen unsicheren Pfadtyp.",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        os.unlink(apt_lock)
    if os.listdir(partial):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "APT hinterließ einen unvollständigen Download im Offline-Cache.",
            "Prüfe Netzwerk und Paketquellen und starte die Vorbereitung erneut.",
        )
    for directory, kind, suffix in (
        (apt_directory, "apt", ".deb"),
        (wheel_directory, "pip", ".whl"),
    ):
        for name in sorted(os.listdir(directory)):
            if directory == apt_directory and name == "partial":
                continue
            if "/" in name or name in {".", ".."}:
                raise OfflinePreflightError(
                    PREPARATION_ERROR_CODE,
                    "Offline-Cache enthält einen ungültigen Dateinamen.",
                    "Verwirf den Cache und starte die Vorbereitung erneut.",
                )
            path = os.path.join(directory, name)
            record = _normalize_cache_payload_file(path, suffix=suffix)
            record.update({"kind": kind, "path": f"{os.path.basename(directory)}/{name}"})
            records.append(record)
    if plan.pip_packages and not any(record["kind"] == "pip" for record in records):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Für die angeforderten Python-Pakete wurden keine Wheels vorbereitet.",
            "Prüfe die Wheel-Verfügbarkeit für diese Plattform und starte das Update erneut.",
        )
    return records


def _canonical_json(value: object) -> bytes:
    text = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return (text + "\n").encode("utf-8")


def _atomic_root_write(
    directory_descriptor: int,
    name: str,
    payload: bytes,
    *,
    mode: int,
) -> None:
    if mode not in {0o600, 0o644} or not name or "/" in name or name in {".", ".."}:
        raise ValueError("Root-Dateivertrag ist ungültig")
    temporary = f".{name}.{os.getpid()}.tmp"
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    descriptor = os.open(temporary, flags, mode, dir_fd=directory_descriptor)
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("Manifest konnte nicht vollständig geschrieben werden")
            view = view[written:]
        os.fsync(descriptor)
        os.fchown(descriptor, _expected_root_uid(), _expected_root_gid())
        os.fchmod(descriptor, mode)
    except Exception:
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except OSError:
            pass
        raise
    finally:
        os.close(descriptor)
    try:
        os.rename(
            temporary,
            name,
            src_dir_fd=directory_descriptor,
            dst_dir_fd=directory_descriptor,
        )
    except Exception:
        try:
            os.unlink(temporary, dir_fd=directory_descriptor)
        except OSError:
            pass
        raise
    os.fsync(directory_descriptor)
    metadata = os.stat(name, dir_fd=directory_descriptor, follow_symlinks=False)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or metadata.st_uid != _expected_root_uid()
        or metadata.st_gid != _expected_root_gid()
        or stat.S_IMODE(metadata.st_mode) != mode
        or metadata.st_nlink != 1
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            f"Root-Datei wurde nicht mit dem gebundenen Modus {mode:04o} angelegt: {name}",
            "Verwirf das Offline-Artefakt und starte die Vorbereitung erneut.",
        )


def seal_preparation(plan: OfflinePreparationPlan) -> OfflinePackageReceipt:
    """Versiegelt vorbereitete Archive/Wheels mit Manifest und SHA-Receipt."""

    _require_root()
    _validate_preparation_plan_contract(plan)
    descriptor, _metadata = _open_bound_cache(plan.cache)
    try:
        for name in (MANIFEST_NAME, MANIFEST_DIGEST_NAME):
            try:
                os.stat(name, dir_fd=descriptor, follow_symlinks=False)
            except FileNotFoundError:
                continue
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Offline-Cache wurde bereits versiegelt.",
                "Verwende das vorhandene Receipt oder beginne eine neue Transaktion.",
            )
        files = _payload_records(plan)
        manifest = {
            "schema": MANIFEST_SCHEMA,
            "state": "complete",
            "transaction_id": plan.cache.transaction_id,
            "cache_root": plan.cache.root,
            "apt_packages": list(plan.apt_packages),
            "pip_packages": list(plan.pip_packages),
            "expected_venv_state": plan.expected_venv_state,
            "files": files,
        }
        payload = _canonical_json(manifest)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Offline-Cachemanifest überschreitet die zulässige Größe.",
                "Verwirf den Cache und prüfe den unerwartet großen Paketumfang.",
            )
        digest = hashlib.sha256(payload).hexdigest()
        _atomic_root_write(descriptor, MANIFEST_NAME, payload, mode=0o600)
        _atomic_root_write(
            descriptor,
            MANIFEST_DIGEST_NAME,
            f"{digest}  {MANIFEST_NAME}\n".encode("ascii"),
            mode=0o600,
        )
    finally:
        os.close(descriptor)
    apt_archives = tuple(
        os.path.join(plan.cache.root, str(record["path"]))
        for record in files
        if record["kind"] == "apt"
    )
    pip_wheels = tuple(
        os.path.join(plan.cache.root, str(record["path"]))
        for record in files
        if record["kind"] == "pip"
    )
    receipt = OfflinePackageReceipt(
        cache=plan.cache,
        manifest_path=os.path.join(plan.cache.root, MANIFEST_NAME),
        manifest_sha256=digest,
        apt_packages=plan.apt_packages,
        pip_packages=plan.pip_packages,
        expected_venv_state=plan.expected_venv_state,
        apt_archives=apt_archives,
        pip_wheels=pip_wheels,
    )
    verify_preparation(receipt)
    return receipt


def _read_root_file(
    path: str,
    maximum_bytes: int,
    *,
    mode: int,
) -> tuple[bytes, os.stat_result]:
    if mode not in {0o600, 0o644}:
        raise ValueError("Root-Dateimodus ist ungültig")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != _expected_root_uid()
            or before.st_gid != _expected_root_gid()
            or stat.S_IMODE(before.st_mode) != mode
            or before.st_size < 0
            or before.st_size > maximum_bytes
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Datei besitzt unsichere Metadaten: {path}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        data = bytearray()
        while len(data) <= maximum_bytes:
            block = os.read(descriptor, min(65536, maximum_bytes + 1 - len(data)))
            if not block:
                break
            data.extend(block)
        after = os.fstat(descriptor)
        named = os.stat(path, follow_symlinks=False)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_uid,
            before.st_gid,
            before.st_mode,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if len(data) > maximum_bytes or identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_uid,
            after.st_gid,
            after.st_mode,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or identity != (
            named.st_dev,
            named.st_ino,
            named.st_size,
            named.st_uid,
            named.st_gid,
            named.st_mode,
            named.st_nlink,
            named.st_mtime_ns,
            named.st_ctime_ns,
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Datei driftete beim Lesen: {path}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        return bytes(data), after
    finally:
        os.close(descriptor)


def _verify_manifest_payload_file(
    path: str,
    *,
    expected_size: int,
    expected_sha256: str,
    expected_mode: int = 0o600,
    expected_device: int | None = None,
    expected_inode: int | None = None,
) -> os.stat_result:
    if expected_mode not in {0o600, 0o644}:
        raise ValueError("Manifest-Dateimodus ist ungültig")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            f"Manifestgebundene Paketdatei ist nicht sicher lesbar: {path}",
            "Verwirf das Offline-Artefakt und starte die Vorbereitung erneut.",
        ) from exc
    try:
        before = os.fstat(descriptor)
        named_before = os.stat(path, follow_symlinks=False)
        if (
            expected_size < 0
            or not SHA256_RE.fullmatch(expected_sha256)
            or not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_uid != _expected_root_uid()
            or before.st_gid != _expected_root_gid()
            or stat.S_IMODE(before.st_mode) != expected_mode
            or before.st_size != expected_size
            or (expected_device is not None and before.st_dev != expected_device)
            or (expected_inode is not None and before.st_ino != expected_inode)
            or (before.st_dev, before.st_ino) != (named_before.st_dev, named_before.st_ino)
            or _file_sha256(descriptor, expected_size) != expected_sha256
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Paketdatei weicht vom Manifest ab: {path}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        after = os.fstat(descriptor)
        named_after = os.stat(path, follow_symlinks=False)
        identity = (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_uid,
            before.st_gid,
            before.st_mode,
            before.st_nlink,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        if identity != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_uid,
            after.st_gid,
            after.st_mode,
            after.st_nlink,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or identity != (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_size,
            named_after.st_uid,
            named_after.st_gid,
            named_after.st_mode,
            named_after.st_nlink,
            named_after.st_mtime_ns,
            named_after.st_ctime_ns,
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Paketdatei driftete beim Readback: {path}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        return after
    finally:
        os.close(descriptor)


def _verify_exact_cache_membership(
    receipt: OfflinePackageReceipt,
    *,
    apt_files: Sequence[str],
    pip_files: Sequence[str],
) -> None:
    expected_root = {"apt", "wheels", MANIFEST_NAME, MANIFEST_DIGEST_NAME}
    expected_apt = {"partial", *(os.path.basename(path) for path in apt_files)}
    expected_pip = {os.path.basename(path) for path in pip_files}
    partial = os.path.join(receipt.cache.root, "apt", "partial")
    for directory in (
        os.path.join(receipt.cache.root, "apt"),
        partial,
        os.path.join(receipt.cache.root, "wheels"),
    ):
        metadata = os.stat(directory, follow_symlinks=False)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or stat.S_ISLNK(metadata.st_mode)
            or metadata.st_uid != _expected_root_uid()
            or metadata.st_gid != _expected_root_gid()
            or stat.S_IMODE(metadata.st_mode) != 0o700
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Cache-Unterverzeichnis ist nicht root-privat: {directory}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
    actual_root = set(os.listdir(receipt.cache.root))
    actual_apt = set(os.listdir(os.path.join(receipt.cache.root, "apt")))
    actual_pip = set(os.listdir(os.path.join(receipt.cache.root, "wheels")))
    if (
        actual_root != expected_root
        or actual_apt != expected_apt
        or actual_pip != expected_pip
        or os.listdir(partial)
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cache enthält nicht manifestgebundene oder unvollständige Dateien.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        )


def _validate_manifest(
    receipt: OfflinePackageReceipt,
) -> tuple[dict[str, object], tuple[str, ...], tuple[str, ...]]:
    expected_manifest_path = os.path.join(receipt.cache.root, MANIFEST_NAME)
    if receipt.manifest_path != expected_manifest_path:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Manifestpfad widerspricht der gebundenen Cache-Identität.",
            "Verwende ausschließlich das bei der Vorbereitung erzeugte Receipt.",
        )
    manifest_raw, _manifest_metadata = _read_root_file(
        receipt.manifest_path,
        MAX_MANIFEST_BYTES,
        mode=0o600,
    )
    digest_raw, _digest_metadata = _read_root_file(
        os.path.join(receipt.cache.root, MANIFEST_DIGEST_NAME),
        1024,
        mode=0o600,
    )
    actual_digest = hashlib.sha256(manifest_raw).hexdigest()
    expected_line = f"{actual_digest}  {MANIFEST_NAME}\n".encode("ascii")
    if (
        not isinstance(receipt.manifest_sha256, str)
        or not SHA256_RE.fullmatch(receipt.manifest_sha256)
        or receipt.manifest_sha256 != actual_digest
        or digest_raw != expected_line
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cachemanifest oder seine Prüfsumme weicht vom Receipt ab.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        )
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cachemanifest ist nicht lesbar.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        ) from exc
    required_keys = {
        "schema",
        "state",
        "transaction_id",
        "cache_root",
        "apt_packages",
        "pip_packages",
        "expected_venv_state",
        "files",
    }
    if not isinstance(manifest, dict) or set(manifest) != required_keys:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cachemanifest besitzt keinen bekannten vollständigen Vertrag.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        )
    try:
        apt = _normalize_packages(
            manifest["apt_packages"],
            label="manifest.apt_packages",
        )
        pip = _normalize_packages(
            manifest["pip_packages"],
            label="manifest.pip_packages",
        )
    except (TypeError, ValueError) as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cachemanifest enthält eine ungültige Paketliste.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        ) from exc
    if (
        manifest["schema"] != MANIFEST_SCHEMA
        or manifest["state"] != "complete"
        or manifest["transaction_id"] != receipt.cache.transaction_id
        or manifest["cache_root"] != receipt.cache.root
        or manifest["expected_venv_state"] not in {"present", "missing"}
        or apt != receipt.apt_packages
        or pip != receipt.pip_packages
        or manifest["expected_venv_state"] != receipt.expected_venv_state
        or not isinstance(manifest["files"], list)
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Cachemanifest widerspricht dem gebundenen Vorbereitungs-Receipt.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        )
    apt_files: list[str] = []
    pip_files: list[str] = []
    seen: set[str] = set()
    for raw in manifest["files"]:
        expected_keys = {"kind", "path", "size", "sha256", "mode", "uid", "gid"}
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Offline-Cachemanifest enthält einen ungültigen Dateieintrag.",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        kind = str(raw["kind"])
        relative = str(raw["path"])
        expected_prefix, expected_suffix = (
            ("apt/", ".deb")
            if kind == "apt"
            else ("wheels/", ".whl")
            if kind == "pip"
            else ("", "")
        )
        path_value = Path(relative)
        if (
            not expected_prefix
            or relative in seen
            or not relative.startswith(expected_prefix)
            or not relative.endswith(expected_suffix)
            or path_value.is_absolute()
            or ".." in path_value.parts
            or len(path_value.parts) != 2
            or path_value.as_posix() != relative
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Offline-Cachemanifest enthält einen fremden Paketpfad.",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        seen.add(relative)
        absolute = os.path.join(receipt.cache.root, relative)
        try:
            expected_size = _strict_nonnegative_int("size", raw["size"])
            expected_mode = _strict_nonnegative_int("mode", raw["mode"])
            expected_uid = _strict_nonnegative_int("uid", raw["uid"])
            expected_gid = _strict_nonnegative_int("gid", raw["gid"])
        except (TypeError, ValueError) as exc:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Paketdatei besitzt ungültige Manifestwerte: {relative}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            ) from exc
        if (
            expected_mode != 0o600
            or expected_uid != _expected_root_uid()
            or expected_gid != _expected_root_gid()
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Offline-Paketdatei besitzt falsche Manifestrechte: {relative}",
                "Verwirf den Cache und starte die Vorbereitung erneut.",
            )
        _verify_manifest_payload_file(
            absolute,
            expected_size=expected_size,
            expected_sha256=str(raw["sha256"]),
        )
        (apt_files if kind == "apt" else pip_files).append(absolute)
    apt_result = tuple(sorted(apt_files))
    pip_result = tuple(sorted(pip_files))
    _verify_exact_cache_membership(
        receipt,
        apt_files=apt_result,
        pip_files=pip_result,
    )
    return manifest, apt_result, pip_result


def verify_preparation(receipt: OfflinePackageReceipt) -> OfflinePackageReceipt:
    """Prüft Cache-Inode, Manifest, Digest und jede lokale Paketdatei erneut."""

    _require_root()
    if not isinstance(receipt, OfflinePackageReceipt):
        raise TypeError("Offline-Paket-Receipt fehlt")
    descriptor, _metadata = _open_bound_cache(receipt.cache)
    os.close(descriptor)
    _manifest, apt_files, pip_files = _validate_manifest(receipt)
    if apt_files != receipt.apt_archives or pip_files != receipt.pip_wheels:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Paketdateimenge widerspricht dem gebundenen Receipt.",
            "Verwirf den Cache und starte die Vorbereitung erneut.",
        )
    return receipt


def _copy_manifest_wheel(
    source: str,
    *,
    expected_size: int,
    expected_sha256: str,
    destination_descriptor: int,
    name: str,
) -> dict[str, object]:
    if (
        os.path.basename(source) != name
        or not name.endswith(".whl")
        or "/" in name
        or name in {".", ".."}
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirror erhielt einen ungültigen Paketpfad.",
            "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
        )
    expected_size = _strict_nonnegative_int("wheel.size", expected_size)
    if not isinstance(expected_sha256, str) or not SHA256_RE.fullmatch(expected_sha256):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            f"Wheel besitzt keine gültige Manifestprüfsumme: {name}",
            "Verwirf den Cache und starte die Paketvorbereitung erneut.",
        )
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0)
    write_flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    source_descriptor = os.open(source, read_flags)
    target_descriptor = -1
    created = False
    try:
        source_before = os.fstat(source_descriptor)
        source_named = os.stat(source, follow_symlinks=False)
        if (
            not stat.S_ISREG(source_before.st_mode)
            or source_before.st_nlink != 1
            or source_before.st_uid != _expected_root_uid()
            or source_before.st_gid != _expected_root_gid()
            or stat.S_IMODE(source_before.st_mode) != 0o600
            or source_before.st_size != expected_size
            or (source_before.st_dev, source_before.st_ino)
            != (source_named.st_dev, source_named.st_ino)
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Quell-Wheel ist vor dem Mirror-Aufbau nicht mehr gebunden: {name}",
                "Verwirf den Cache und starte die Paketvorbereitung erneut.",
            )
        target_descriptor = os.open(
            name,
            write_flags,
            0o644,
            dir_fd=destination_descriptor,
        )
        created = True
        digest = hashlib.sha256()
        copied = 0
        while True:
            block = os.read(source_descriptor, 1024 * 1024)
            if not block:
                break
            digest.update(block)
            copied += len(block)
            view = memoryview(block)
            while view:
                written = os.write(target_descriptor, view)
                if written <= 0:
                    raise OSError("Wheel-Mirror konnte nicht vollständig geschrieben werden")
                view = view[written:]
        source_after = os.fstat(source_descriptor)
        source_named_after = os.stat(source, follow_symlinks=False)
        source_identity = (
            source_before.st_dev,
            source_before.st_ino,
            source_before.st_size,
            source_before.st_uid,
            source_before.st_gid,
            source_before.st_mode,
            source_before.st_nlink,
            source_before.st_mtime_ns,
            source_before.st_ctime_ns,
        )
        if (
            copied != expected_size
            or digest.hexdigest() != expected_sha256
            or source_identity
            != (
                source_after.st_dev,
                source_after.st_ino,
                source_after.st_size,
                source_after.st_uid,
                source_after.st_gid,
                source_after.st_mode,
                source_after.st_nlink,
                source_after.st_mtime_ns,
                source_after.st_ctime_ns,
            )
            or source_identity
            != (
                source_named_after.st_dev,
                source_named_after.st_ino,
                source_named_after.st_size,
                source_named_after.st_uid,
                source_named_after.st_gid,
                source_named_after.st_mode,
                source_named_after.st_nlink,
                source_named_after.st_mtime_ns,
                source_named_after.st_ctime_ns,
            )
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Quell-Wheel driftete beim Mirror-Aufbau: {name}",
                "Verwirf Cache und Mirror und starte die Paketvorbereitung erneut.",
            )
        os.fsync(target_descriptor)
        os.fchown(target_descriptor, _expected_root_uid(), _expected_root_gid())
        os.fchmod(target_descriptor, 0o644)
        target = os.fstat(target_descriptor)
        target_named = os.stat(
            name,
            dir_fd=destination_descriptor,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(target.st_mode)
            or target.st_uid != _expected_root_uid()
            or target.st_gid != _expected_root_gid()
            or stat.S_IMODE(target.st_mode) != 0o644
            or target.st_nlink != 1
            or target.st_size != expected_size
            or (target.st_dev, target.st_ino) != (target_named.st_dev, target_named.st_ino)
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Wheel-Mirrordatei besitzt unsichere Metadaten: {name}",
                "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
            )
        return {
            "path": f"wheels/{name}",
            "size": int(target.st_size),
            "sha256": expected_sha256,
            "mode": 0o644,
            "uid": int(target.st_uid),
            "gid": int(target.st_gid),
            "device": int(target.st_dev),
            "inode": int(target.st_ino),
        }
    except Exception:
        if created:
            try:
                os.unlink(name, dir_fd=destination_descriptor)
            except OSError:
                pass
        raise
    finally:
        if target_descriptor >= 0:
            os.close(target_descriptor)
        os.close(source_descriptor)


def _open_bound_wheel_mirror(
    mirror: OfflineWheelMirrorReceipt,
) -> tuple[int, os.stat_result]:
    if not isinstance(mirror, OfflineWheelMirrorReceipt):
        raise TypeError("Wheel-Mirror-Receipt fehlt")
    transaction = _validate_transaction_id(mirror.transaction_id)
    root = _canonical_absolute_path(mirror.root, label="Wheel-Mirrorpfad")
    if os.path.basename(root) != transaction:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirrorpfad und Transaktions-ID widersprechen sich.",
            "Verwende ausschließlich das beim Mirror-Aufbau erzeugte Receipt.",
        )
    descriptor, metadata = _open_secure_directory(
        root,
        create=False,
        leaf_mode=0o755,
    )
    if metadata.st_dev != mirror.device or metadata.st_ino != mirror.inode:
        os.close(descriptor)
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirror wurde seit seiner Anlage ersetzt.",
            "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
        )
    return descriptor, metadata


def materialize_wheel_mirror(
    receipt: OfflinePackageReceipt,
    *,
    mirror_parent: str = DEFAULT_WHEEL_MIRROR_PARENT,
) -> OfflinePackageReceipt:
    """Kopiert verifizierte Wheels in einen root-eigenen 0755/0644-Mirror.

    Der private Downloadcache bleibt 0700/0600. Nur dieser zweite, vollständig
    manifestgebundene Pfad ist für den Installationsnutzer lesbar; weder
    Verzeichnisse noch Dateien sind für ihn beschreibbar.
    """

    _require_root()
    verify_preparation(receipt)
    if not receipt.pip_packages or not receipt.pip_wheels:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirror wurde ohne vorbereitete Python-Pakete angefordert.",
            "Überspringe den Mirror, wenn die Release-Policy keine pip-Pakete enthält.",
        )
    if receipt.wheel_mirror is not None:
        verify_wheel_mirror(receipt)
        return receipt
    manifest, _apt_files, pip_files = _validate_manifest(receipt)
    source_records = {
        os.path.join(receipt.cache.root, str(record["path"])): record
        for record in manifest["files"]
        if isinstance(record, dict) and record.get("kind") == "pip"
    }
    if tuple(sorted(source_records)) != pip_files:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirror kann die Quellpakete nicht eindeutig dem Manifest zuordnen.",
            "Verwirf den Cache und starte die Paketvorbereitung erneut.",
        )
    parent = _canonical_absolute_path(mirror_parent, label="Wheel-Mirror-Elternpfad")
    parent_descriptor, _parent_metadata = _open_secure_directory(
        parent,
        create=True,
        leaf_mode=0o755,
    )
    root_descriptor = -1
    created = False
    try:
        root_descriptor, root_metadata = _create_root_child(
            parent_descriptor,
            receipt.cache.transaction_id,
            mode=0o755,
        )
        created = True
        wheels_descriptor, _wheels_metadata = _create_root_child(
            root_descriptor,
            "wheels",
            mode=0o755,
        )
        try:
            mirror_records = []
            for source in pip_files:
                raw = source_records[source]
                mirror_records.append(
                    _copy_manifest_wheel(
                        source,
                        expected_size=_strict_nonnegative_int("wheel.size", raw["size"]),
                        expected_sha256=str(raw["sha256"]),
                        destination_descriptor=wheels_descriptor,
                        name=os.path.basename(source),
                    )
                )
            os.fsync(wheels_descriptor)
        finally:
            os.close(wheels_descriptor)
        mirror_root = os.path.join(parent, receipt.cache.transaction_id)
        mirror_manifest = {
            "schema": WHEEL_MIRROR_SCHEMA,
            "state": "complete",
            "transaction_id": receipt.cache.transaction_id,
            "mirror_root": mirror_root,
            "source_manifest_sha256": receipt.manifest_sha256,
            "files": mirror_records,
        }
        payload = _canonical_json(mirror_manifest)
        if len(payload) > MAX_MANIFEST_BYTES:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Wheel-Mirrormanifest überschreitet die zulässige Größe.",
                "Verwirf den Mirror und prüfe den unerwartet großen Paketumfang.",
            )
        digest = hashlib.sha256(payload).hexdigest()
        _atomic_root_write(
            root_descriptor,
            WHEEL_MIRROR_MANIFEST_NAME,
            payload,
            mode=0o644,
        )
        _atomic_root_write(
            root_descriptor,
            WHEEL_MIRROR_DIGEST_NAME,
            f"{digest}  {WHEEL_MIRROR_MANIFEST_NAME}\n".encode("ascii"),
            mode=0o644,
        )
        mirror = OfflineWheelMirrorReceipt(
            transaction_id=receipt.cache.transaction_id,
            root=mirror_root,
            device=int(root_metadata.st_dev),
            inode=int(root_metadata.st_ino),
            source_manifest_sha256=receipt.manifest_sha256,
            manifest_path=os.path.join(mirror_root, WHEEL_MIRROR_MANIFEST_NAME),
            manifest_sha256=digest,
            wheels=tuple(
                os.path.join(mirror_root, str(record["path"]))
                for record in mirror_records
            ),
        )
        completed = OfflinePackageReceipt(
            cache=receipt.cache,
            manifest_path=receipt.manifest_path,
            manifest_sha256=receipt.manifest_sha256,
            apt_packages=receipt.apt_packages,
            pip_packages=receipt.pip_packages,
            expected_venv_state=receipt.expected_venv_state,
            apt_archives=receipt.apt_archives,
            pip_wheels=receipt.pip_wheels,
            wheel_mirror=mirror,
        )
    except Exception as materialization_error:
        cleanup_error: Exception | None = None
        if created and root_descriptor >= 0:
            try:
                _purge_directory_contents(root_descriptor)
                os.rmdir(receipt.cache.transaction_id, dir_fd=parent_descriptor)
            except Exception as exc:
                cleanup_error = exc
        if cleanup_error is not None:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Wheel-Mirror-Materialisierung schlug fehl: "
                f"{_cleanup_error_detail(materialization_error)}; zusätzlich konnte "
                "der unvollständige Mirror nicht vollständig bereinigt werden: "
                f"{_cleanup_error_detail(cleanup_error)}",
                f"Verwende den Mirror unter {parent}/"
                f"{receipt.cache.transaction_id} nicht. Prüfe den genannten Pfad "
                "ohne rekursive Wildcard-Löschung und wiederhole danach die "
                "gebundene Offline-Vorbereitung.",
            ) from materialization_error
        raise
    finally:
        if root_descriptor >= 0:
            os.close(root_descriptor)
        os.close(parent_descriptor)
    try:
        verify_wheel_mirror(completed)
    except Exception as verification_error:
        cleanup_error = None
        try:
            cleanup_wheel_mirror(completed.wheel_mirror)
        except Exception as exc:
            cleanup_error = exc
        if cleanup_error is not None:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Wheel-Mirror-Endverifikation schlug fehl: "
                f"{_cleanup_error_detail(verification_error)}; zusätzlich konnte "
                "der unbrauchbare Mirror nicht vollständig bereinigt werden: "
                f"{_cleanup_error_detail(cleanup_error)}",
                f"Verwende den Mirror unter {completed.wheel_mirror.root} nicht. "
                "Prüfe den Pfad anhand des vollständigen Updatejournals und "
                "wiederhole danach die gebundene Offline-Vorbereitung.",
            ) from verification_error
        raise
    return completed


def _validate_wheel_mirror_manifest(
    receipt: OfflinePackageReceipt,
) -> tuple[dict[str, object], tuple[str, ...]]:
    mirror = receipt.wheel_mirror
    if mirror is None:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirror-Receipt fehlt.",
            "Erzeuge den Wheel-Mirror vor der Diensteruhe erneut.",
        )
    expected_manifest_path = os.path.join(mirror.root, WHEEL_MIRROR_MANIFEST_NAME)
    if mirror.manifest_path != expected_manifest_path:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirrormanifestpfad widerspricht der Mirror-Identität.",
            "Verwende ausschließlich das erzeugte Mirror-Receipt.",
        )
    manifest_raw, _manifest_metadata = _read_root_file(
        mirror.manifest_path,
        MAX_MANIFEST_BYTES,
        mode=0o644,
    )
    digest_raw, _digest_metadata = _read_root_file(
        os.path.join(mirror.root, WHEEL_MIRROR_DIGEST_NAME),
        1024,
        mode=0o644,
    )
    actual_digest = hashlib.sha256(manifest_raw).hexdigest()
    expected_digest_line = (
        f"{actual_digest}  {WHEEL_MIRROR_MANIFEST_NAME}\n".encode("ascii")
    )
    if (
        not isinstance(mirror.manifest_sha256, str)
        or not SHA256_RE.fullmatch(mirror.manifest_sha256)
        or mirror.manifest_sha256 != actual_digest
        or digest_raw != expected_digest_line
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirrormanifest oder seine Prüfsumme weicht vom Receipt ab.",
            "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
        )
    try:
        manifest = json.loads(manifest_raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirrormanifest ist nicht lesbar.",
            "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
        ) from exc
    required_keys = {
        "schema",
        "state",
        "transaction_id",
        "mirror_root",
        "source_manifest_sha256",
        "files",
    }
    if (
        not isinstance(manifest, dict)
        or set(manifest) != required_keys
        or manifest["schema"] != WHEEL_MIRROR_SCHEMA
        or manifest["state"] != "complete"
        or manifest["transaction_id"] != receipt.cache.transaction_id
        or manifest["transaction_id"] != mirror.transaction_id
        or manifest["mirror_root"] != mirror.root
        or manifest["source_manifest_sha256"] != receipt.manifest_sha256
        or mirror.source_manifest_sha256 != receipt.manifest_sha256
        or not isinstance(manifest["files"], list)
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirrormanifest widerspricht dem gebundenen Paket-Receipt.",
            "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
        )
    wheels: list[str] = []
    source_manifest, _apt_files, _pip_files = _validate_manifest(receipt)
    source_by_name = {
        os.path.basename(str(record["path"])): (
            _strict_nonnegative_int("source.size", record["size"]),
            str(record["sha256"]),
        )
        for record in source_manifest["files"]
        if isinstance(record, dict) and record.get("kind") == "pip"
    }
    seen: set[str] = set()
    for raw in manifest["files"]:
        expected_keys = {
            "path",
            "size",
            "sha256",
            "mode",
            "uid",
            "gid",
            "device",
            "inode",
        }
        if not isinstance(raw, dict) or set(raw) != expected_keys:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Wheel-Mirrormanifest enthält einen ungültigen Dateieintrag.",
                "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
            )
        relative = str(raw["path"])
        relative_path = Path(relative)
        if (
            relative in seen
            or not relative.startswith("wheels/")
            or not relative.endswith(".whl")
            or relative_path.is_absolute()
            or ".." in relative_path.parts
            or len(relative_path.parts) != 2
            or relative_path.as_posix() != relative
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Wheel-Mirrormanifest enthält einen fremden Paketpfad.",
                "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
            )
        seen.add(relative)
        try:
            size = _strict_nonnegative_int("mirror.size", raw["size"])
            mode = _strict_nonnegative_int("mirror.mode", raw["mode"])
            uid = _strict_nonnegative_int("mirror.uid", raw["uid"])
            gid = _strict_nonnegative_int("mirror.gid", raw["gid"])
            device = _strict_nonnegative_int("mirror.device", raw["device"])
            inode = _strict_nonnegative_int("mirror.inode", raw["inode"])
        except ValueError as exc:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Wheel-Mirrormanifest besitzt ungültige Werte: {relative}",
                "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
            ) from exc
        name = os.path.basename(relative)
        if (
            mode != 0o644
            or uid != _expected_root_uid()
            or gid != _expected_root_gid()
            or source_by_name.get(name) != (size, str(raw["sha256"]))
        ):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                f"Wheel-Mirrordatei widerspricht dem privaten Quellmanifest: {name}",
                "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
            )
        absolute = os.path.join(mirror.root, relative)
        _verify_manifest_payload_file(
            absolute,
            expected_size=size,
            expected_sha256=str(raw["sha256"]),
            expected_mode=0o644,
            expected_device=device,
            expected_inode=inode,
        )
        wheels.append(absolute)
    result = tuple(sorted(wheels))
    expected_root = {
        "wheels",
        WHEEL_MIRROR_MANIFEST_NAME,
        WHEEL_MIRROR_DIGEST_NAME,
    }
    wheels_directory = os.path.join(mirror.root, "wheels")
    wheels_metadata = os.stat(wheels_directory, follow_symlinks=False)
    if (
        not stat.S_ISDIR(wheels_metadata.st_mode)
        or stat.S_ISLNK(wheels_metadata.st_mode)
        or wheels_metadata.st_uid != _expected_root_uid()
        or wheels_metadata.st_gid != _expected_root_gid()
        or stat.S_IMODE(wheels_metadata.st_mode) != 0o755
        or set(os.listdir(mirror.root)) != expected_root
        or set(os.listdir(wheels_directory))
        != {os.path.basename(path) for path in result}
        or result != tuple(sorted(mirror.wheels))
        or len(result) != len(receipt.pip_wheels)
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Mirror enthält fremde Dateien, Symlinks oder falsche Rechte.",
            "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
        )
    return manifest, result


def verify_wheel_mirror(receipt: OfflinePackageReceipt) -> OfflinePackageReceipt:
    """Bindet Mirror-Inode, Manifest, Quelldigest, Dateien und Leserechte erneut."""

    _require_root()
    verify_preparation(receipt)
    if receipt.wheel_mirror is None:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Für die Python-Paketinstallation fehlt der Wheel-Mirror.",
            "Erzeuge den Wheel-Mirror vor der Diensteruhe erneut.",
        )
    descriptor, _metadata = _open_bound_wheel_mirror(receipt.wheel_mirror)
    os.close(descriptor)
    _manifest, wheels = _validate_wheel_mirror_manifest(receipt)
    if wheels != receipt.wheel_mirror.wheels:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Wheel-Dateimenge widerspricht dem gebundenen Mirror-Receipt.",
            "Verwirf den Mirror und starte die Paketvorbereitung erneut.",
        )
    return receipt


def _receipt_mapping(receipt: OfflinePackageReceipt) -> dict[str, object]:
    mirror = receipt.wheel_mirror
    mirror_mapping: dict[str, object] | None = None
    if mirror is not None:
        mirror_mapping = {
            "transaction_id": mirror.transaction_id,
            "root": mirror.root,
            "device": mirror.device,
            "inode": mirror.inode,
            "source_manifest_sha256": mirror.source_manifest_sha256,
            "manifest_path": mirror.manifest_path,
            "manifest_sha256": mirror.manifest_sha256,
            "wheels": list(mirror.wheels),
        }
    return {
        "schema": RECEIPT_SCHEMA,
        "cache": {
            "transaction_id": receipt.cache.transaction_id,
            "root": receipt.cache.root,
            "device": receipt.cache.device,
            "inode": receipt.cache.inode,
        },
        "manifest_path": receipt.manifest_path,
        "manifest_sha256": receipt.manifest_sha256,
        "apt_packages": list(receipt.apt_packages),
        "pip_packages": list(receipt.pip_packages),
        "expected_venv_state": receipt.expected_venv_state,
        "apt_archives": list(receipt.apt_archives),
        "pip_wheels": list(receipt.pip_wheels),
        "wheel_mirror": mirror_mapping,
    }


def serialize_offline_package_receipt(receipt: OfflinePackageReceipt) -> bytes:
    """Serialisiert ein installierbares Receipt kanonisch für den Finalizer."""

    verify_preparation(receipt)
    if receipt.pip_packages:
        verify_wheel_mirror(receipt)
    elif receipt.wheel_mirror is not None:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Receipt enthält einen Wheel-Mirror ohne angeforderte Python-Pakete.",
            "Verwirf das widersprüchliche Receipt und starte die Vorbereitung erneut.",
        )
    payload = _canonical_json(_receipt_mapping(receipt))
    if len(payload) > MAX_RECEIPT_BYTES:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Paket-Receipt überschreitet die zulässige Größe.",
            "Verwirf das Receipt und prüfe den unerwartet großen Paketumfang.",
        )
    return payload


def _receipt_path_tuple(raw: object, *, label: str) -> tuple[str, ...]:
    if not isinstance(raw, list):
        raise ValueError(f"{label} muss eine kanonische Pfadliste sein")
    result: list[str] = []
    for item in raw:
        if not isinstance(item, str):
            raise ValueError(f"{label} enthält keinen Textpfad")
        path = _canonical_absolute_path(item, label=label)
        if path in result:
            raise ValueError(f"{label} enthält einen doppelten Pfad")
        result.append(path)
    return tuple(result)


def _parse_cache_identity(raw: object) -> OfflineCacheIdentity:
    keys = {"transaction_id", "root", "device", "inode"}
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("cache besitzt keinen vollständigen Vertrag")
    return OfflineCacheIdentity(
        transaction_id=_validate_transaction_id(raw["transaction_id"]),
        root=_canonical_absolute_path(raw["root"], label="receipt.cache.root"),
        device=_strict_nonnegative_int("receipt.cache.device", raw["device"]),
        inode=_strict_nonnegative_int("receipt.cache.inode", raw["inode"]),
    )


def _parse_wheel_mirror(raw: object) -> OfflineWheelMirrorReceipt | None:
    if raw is None:
        return None
    keys = {
        "transaction_id",
        "root",
        "device",
        "inode",
        "source_manifest_sha256",
        "manifest_path",
        "manifest_sha256",
        "wheels",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        raise ValueError("wheel_mirror besitzt keinen vollständigen Vertrag")
    source_digest = raw["source_manifest_sha256"]
    manifest_digest = raw["manifest_sha256"]
    if (
        not isinstance(source_digest, str)
        or not SHA256_RE.fullmatch(source_digest)
        or not isinstance(manifest_digest, str)
        or not SHA256_RE.fullmatch(manifest_digest)
    ):
        raise ValueError("wheel_mirror besitzt eine ungültige Prüfsumme")
    return OfflineWheelMirrorReceipt(
        transaction_id=_validate_transaction_id(raw["transaction_id"]),
        root=_canonical_absolute_path(raw["root"], label="receipt.wheel_mirror.root"),
        device=_strict_nonnegative_int(
            "receipt.wheel_mirror.device",
            raw["device"],
        ),
        inode=_strict_nonnegative_int(
            "receipt.wheel_mirror.inode",
            raw["inode"],
        ),
        source_manifest_sha256=source_digest,
        manifest_path=_canonical_absolute_path(
            raw["manifest_path"],
            label="receipt.wheel_mirror.manifest_path",
        ),
        manifest_sha256=manifest_digest,
        wheels=_receipt_path_tuple(
            raw["wheels"],
            label="receipt.wheel_mirror.wheels",
        ),
    )


def parse_offline_package_receipt(
    payload: bytes | str,
) -> OfflinePackageReceipt:
    """Parst nur die kanonische Form und verifiziert alle Dateisystembindungen."""

    if isinstance(payload, str):
        try:
            raw_bytes = payload.encode("utf-8")
        except UnicodeError as exc:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Offline-Paket-Receipt ist nicht als UTF-8 darstellbar.",
                "Übergebe das unveränderte Receipt aus der Vorbereitungsphase.",
            ) from exc
    elif isinstance(payload, bytes):
        raw_bytes = payload
    else:
        raise TypeError("Offline-Paket-Receipt muss bytes oder Text sein")
    if not raw_bytes or len(raw_bytes) > MAX_RECEIPT_BYTES or b"\x00" in raw_bytes:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Paket-Receipt ist leer, zu groß oder enthält Nullbytes.",
            "Übergebe das unveränderte Receipt aus der Vorbereitungsphase.",
        )
    try:
        mapping = json.loads(raw_bytes.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Paket-Receipt ist kein gültiges JSON.",
            "Übergebe das unveränderte Receipt aus der Vorbereitungsphase.",
        ) from exc
    required_keys = {
        "schema",
        "cache",
        "manifest_path",
        "manifest_sha256",
        "apt_packages",
        "pip_packages",
        "expected_venv_state",
        "apt_archives",
        "pip_wheels",
        "wheel_mirror",
    }
    if (
        not isinstance(mapping, dict)
        or set(mapping) != required_keys
        or mapping.get("schema") != RECEIPT_SCHEMA
        or _canonical_json(mapping) != raw_bytes
    ):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Paket-Receipt besitzt keine kanonische vollständige Form.",
            "Übergebe das bytegleich serialisierte Receipt aus der Vorbereitungsphase.",
        )
    try:
        cache = _parse_cache_identity(mapping["cache"])
        manifest_digest = mapping["manifest_sha256"]
        if (
            not isinstance(manifest_digest, str)
            or not SHA256_RE.fullmatch(manifest_digest)
        ):
            raise ValueError("manifest_sha256 ist ungültig")
        apt_packages = _normalize_packages(
            mapping["apt_packages"],
            label="receipt.apt_packages",
        )
        pip_packages = _normalize_packages(
            mapping["pip_packages"],
            label="receipt.pip_packages",
        )
        expected_venv_state = mapping["expected_venv_state"]
        if expected_venv_state not in {"present", "missing"}:
            raise ValueError("expected_venv_state ist ungültig")
        receipt = OfflinePackageReceipt(
            cache=cache,
            manifest_path=_canonical_absolute_path(
                mapping["manifest_path"],
                label="receipt.manifest_path",
            ),
            manifest_sha256=manifest_digest,
            apt_packages=apt_packages,
            pip_packages=pip_packages,
            expected_venv_state=expected_venv_state,
            apt_archives=_receipt_path_tuple(
                mapping["apt_archives"],
                label="receipt.apt_archives",
            ),
            pip_wheels=_receipt_path_tuple(
                mapping["pip_wheels"],
                label="receipt.pip_wheels",
            ),
            wheel_mirror=_parse_wheel_mirror(mapping["wheel_mirror"]),
        )
    except (TypeError, ValueError) as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Paket-Receipt enthält ungültige typisierte Felder.",
            "Übergebe das unveränderte Receipt aus der Vorbereitungsphase.",
        ) from exc
    verify_preparation(receipt)
    if receipt.pip_packages:
        verify_wheel_mirror(receipt)
    elif receipt.wheel_mirror is not None:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Paket-Receipt enthält einen unerwarteten Wheel-Mirror.",
            "Übergebe das unveränderte Receipt aus der Vorbereitungsphase.",
        )
    return receipt


def _validate_python_executable(raw: str) -> str:
    path = _canonical_absolute_path(raw, label="venv-Python")
    try:
        metadata = os.stat(path, follow_symlinks=True)
    except OSError as exc:
        raise ValueError("venv-Python ist nicht lesbar") from exc
    if not stat.S_ISREG(metadata.st_mode) or not stat.S_IMODE(metadata.st_mode) & 0o111:
        raise ValueError("venv-Python verweist nicht auf eine ausführbare reguläre Datei")
    return path


def assert_local_only_argv(
    argv: Sequence[str],
    *,
    receipt: OfflinePackageReceipt,
) -> tuple[str, ...]:
    """Fail-closed Netzwerkgrenze für jedes nach dem Freeze erlaubte argv."""

    command = tuple(str(item) for item in argv)
    if not command or any("\x00" in item for item in command):
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Leeres oder ungültiges Nach-Freeze-Kommando wurde abgewiesen.",
            "Verwende ausschließlich die manifestgebundenen Offline-Kommandos.",
        )
    apt_directory, _private_wheel_directory = _cache_paths(receipt.cache)
    valid = False
    if command[0] == "/usr/bin/apt-get":
        valid = command == (
            "/usr/bin/apt-get",
            "-o",
            f"Dir::Cache::archives={apt_directory}",
            "-o",
            "APT::Sandbox::User=root",
            "install",
            "--no-download",
            "--no-remove",
            "--no-upgrade",
            "--no-install-recommends",
            "-y",
            "--",
            *receipt.apt_packages,
        )
    elif command[0] == "/usr/bin/dpkg":
        valid = bool(receipt.apt_archives) and command == (
            "/usr/bin/dpkg",
            "--install",
            "--",
            *receipt.apt_archives,
        )
    elif len(command) >= 5 and command[1:5] == ("-I", "-m", "pip", "install"):
        mirror = receipt.wheel_mirror
        if mirror is not None:
            wheel_directory = os.path.join(mirror.root, "wheels")
            state_option = (
                "--no-deps"
                if receipt.expected_venv_state == "present"
                else "--prefer-binary"
            )
            try:
                python = _validate_python_executable(command[0])
            except ValueError:
                python = ""
            valid = bool(python) and command[1:] == (
                    "-I",
                    "-m",
                    "pip",
                    "install",
                    "--quiet",
                    "--disable-pip-version-check",
                    "--no-index",
                    "--find-links",
                    wheel_directory,
                    state_option,
                    "--",
                    *receipt.pip_packages,
                )
    if not valid:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Nach dem Dienst-Freeze wurde ein nicht eindeutig lokales Paketkommando abgewiesen.",
            "Verwende apt-get --no-download, den manifestgebundenen dpkg-Fallback "
            "oder pip --no-index --find-links aus diesem Offline-Receipt.",
        )
    return command


def build_offline_install_commands(
    receipt: OfflinePackageReceipt,
    *,
    venv_python: str | None = None,
    include_pip: bool = True,
) -> OfflineInstallCommands:
    """Erzeugt ausschließlich manifestgebundene lokale argv für die Ruhephase."""

    if not isinstance(include_pip, bool):
        raise TypeError("include_pip muss boolesch sein")
    verify_preparation(receipt)
    apt_directory, _private_wheel_directory = _cache_paths(receipt.cache)
    apt_argv: tuple[str, ...] | None = None
    dpkg_argv: tuple[str, ...] | None = None
    pip_argv: tuple[str, ...] | None = None
    if receipt.apt_packages:
        apt_argv = (
            "/usr/bin/apt-get",
            "-o",
            f"Dir::Cache::archives={apt_directory}",
            "-o",
            "APT::Sandbox::User=root",
            "install",
            "--no-download",
            "--no-remove",
            "--no-upgrade",
            "--no-install-recommends",
            "-y",
            "--",
            *receipt.apt_packages,
        )
        assert_local_only_argv(apt_argv, receipt=receipt)
        if receipt.apt_archives:
            dpkg_argv = ("/usr/bin/dpkg", "--install", "--", *receipt.apt_archives)
            assert_local_only_argv(dpkg_argv, receipt=receipt)
    if receipt.pip_packages and include_pip:
        if receipt.wheel_mirror is None:
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Für die Nach-Freeze-pip-Installation fehlt der lesbare Wheel-Mirror.",
                "Erzeuge den manifestgebundenen Wheel-Mirror vor der Diensteruhe und "
                "übergebe danach das vervollständigte Receipt an den Finalizer.",
            )
        verify_wheel_mirror(receipt)
        if not venv_python:
            raise ValueError("venv_python ist für vorbereitete pip-Pakete erforderlich")
        python = _validate_python_executable(venv_python)
        wheel_directory = os.path.join(receipt.wheel_mirror.root, "wheels")
        pip_values = [
            python,
            "-I",
            "-m",
            "pip",
            "install",
            "--quiet",
            "--disable-pip-version-check",
            "--no-index",
            "--find-links",
            wheel_directory,
        ]
        if receipt.expected_venv_state == "present":
            pip_values.append("--no-deps")
        else:
            pip_values.append("--prefer-binary")
        pip_values.extend(("--", *receipt.pip_packages))
        pip_argv = tuple(pip_values)
        assert_local_only_argv(pip_argv, receipt=receipt)
    return OfflineInstallCommands(
        apt_install_argv=apt_argv,
        dpkg_fallback_argv=dpkg_argv,
        pip_install_argv=pip_argv,
    )


def _descriptor_mount_id(descriptor: int) -> int:
    try:
        with open(f"/proc/self/fdinfo/{int(descriptor)}", encoding="ascii") as handle:
            for line in handle:
                if line.startswith("mnt_id:"):
                    value = int(line.split(":", 1)[1].strip())
                    if value > 0:
                        return value
                    break
    except (OSError, UnicodeError, ValueError) as exc:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Mount-Grenze des Offline-Artefakts ist nicht sicher lesbar.",
            "Bereinige den Pfad nicht rekursiv. Prüfe /proc und vorhandene Mounts und "
            "starte danach die gebundene Bereinigung erneut.",
        ) from exc
    raise OfflinePreflightError(
        PREPARATION_ERROR_CODE,
        "Mount-Grenze des Offline-Artefakts ist nicht eindeutig.",
        "Bereinige den Pfad nicht rekursiv. Prüfe /proc und vorhandene Mounts und "
        "starte danach die gebundene Bereinigung erneut.",
    )


def _purge_directory_contents(
    descriptor: int,
    *,
    expected_device: int | None = None,
    expected_mount_id: int | None = None,
) -> None:
    root_metadata = os.fstat(descriptor)
    device = root_metadata.st_dev if expected_device is None else expected_device
    mount_id = (
        _descriptor_mount_id(descriptor)
        if expected_mount_id is None
        else expected_mount_id
    )
    if root_metadata.st_dev != device or _descriptor_mount_id(descriptor) != mount_id:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            "Offline-Artefakt überschreitet vor der Bereinigung seine Mount-Grenze.",
            "Hänge verschachtelte Dateisysteme aus und starte die gebundene Bereinigung erneut.",
        )
    flags = (
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_DIRECTORY", 0)
        | getattr(os, "O_CLOEXEC", 0)
    )
    with os.scandir(descriptor) as entries:
        names = [entry.name for entry in entries]
    for name in names:
        metadata = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
        if stat.S_ISDIR(metadata.st_mode) and not stat.S_ISLNK(metadata.st_mode):
            child = os.open(name, flags, dir_fd=descriptor)
            try:
                opened = os.fstat(child)
                named = os.stat(name, dir_fd=descriptor, follow_symlinks=False)
                if (opened.st_dev, opened.st_ino) != (named.st_dev, named.st_ino):
                    raise OfflinePreflightError(
                        PREPARATION_ERROR_CODE,
                        "Offline-Cacheverzeichnis driftete bei der Bereinigung.",
                        "Beende konkurrierende Root-Prozesse und wiederhole die Bereinigung.",
                    )
                if opened.st_dev != device or _descriptor_mount_id(child) != mount_id:
                    raise OfflinePreflightError(
                        PREPARATION_ERROR_CODE,
                        "Verschachtelter Mount im Offline-Artefakt wurde nicht betreten.",
                        "Hänge den genannten Teilpfad aus und wiederhole die gebundene "
                        "Bereinigung; lösche ihn nicht rekursiv.",
                    )
                _purge_directory_contents(
                    child,
                    expected_device=device,
                    expected_mount_id=mount_id,
                )
            finally:
                os.close(child)
            os.rmdir(name, dir_fd=descriptor)
        else:
            os.unlink(name, dir_fd=descriptor)


def _cleanup_error_detail(error: Exception) -> str:
    """Formatiert Cleanup-Fehler kompakt, ohne den Primärfehler zu verlieren."""

    if isinstance(error, OfflinePreflightError):
        detail = f"{error.code}: {error.message}"
    else:
        detail = f"{type(error).__name__}: {error}"
    return " ".join(str(detail).split())


def _cleanup_target_is_absent(
    *,
    transaction_id: str,
    root: str,
    label: str,
) -> bool:
    """Erkennt ausschließlich das Fehlen des exakt gebundenen Zielpfads.

    Andere Pfadfehler, Symlinks oder ersetzte Inodes bleiben harte Fehler. Das
    macht eine Wiederholung nach bereits erfolgreichem Teil-Cleanup
    idempotent, ohne die Inode- und Nofollow-Prüfungen aufzuweichen.
    """

    transaction = _validate_transaction_id(transaction_id)
    canonical_root = _canonical_absolute_path(root, label=f"{label}pfad")
    if os.path.basename(canonical_root) != transaction:
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            f"{label}pfad und Transaktions-ID widersprechen sich.",
            "Verwende ausschließlich das bei der Vorbereitung erzeugte Receipt.",
        )
    try:
        os.lstat(canonical_root)
    except FileNotFoundError:
        return True
    return False


def cleanup_wheel_mirror(mirror: OfflineWheelMirrorReceipt) -> None:
    """Entfernt nur den inodegebundenen öffentlichen Lesemirror, nie den Parent."""

    _require_root()
    if not isinstance(mirror, OfflineWheelMirrorReceipt):
        raise TypeError("Wheel-Mirror-Receipt fehlt")
    if _cleanup_target_is_absent(
        transaction_id=mirror.transaction_id,
        root=mirror.root,
        label="Wheel-Mirror",
    ):
        return
    try:
        descriptor, _metadata = _open_bound_wheel_mirror(mirror)
    except FileNotFoundError:
        if _cleanup_target_is_absent(
            transaction_id=mirror.transaction_id,
            root=mirror.root,
            label="Wheel-Mirror",
        ):
            return
        raise
    parent_path = os.path.dirname(mirror.root)
    parent_descriptor = -1
    try:
        _purge_directory_contents(descriptor)
        parent_descriptor, _parent_metadata = _open_secure_directory(
            parent_path,
            create=False,
            leaf_mode=0o755,
        )
        named = os.stat(
            mirror.transaction_id,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        opened = os.fstat(descriptor)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Wheel-Mirror wurde während der Bereinigung ersetzt.",
                "Beende konkurrierende Root-Prozesse und prüfe den Mirrorpfad manuell.",
            )
        os.rmdir(mirror.transaction_id, dir_fd=parent_descriptor)
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        os.close(descriptor)


def cleanup_offline_cache(cache: OfflineCacheIdentity) -> None:
    """Entfernt ausschließlich den inodegebundenen Transaktionscache, nie den Parent."""

    _require_root()
    if not isinstance(cache, OfflineCacheIdentity):
        raise TypeError("Offline-Cache-Identität fehlt")
    if _cleanup_target_is_absent(
        transaction_id=cache.transaction_id,
        root=cache.root,
        label="Offline-Cache",
    ):
        return
    try:
        descriptor, _metadata = _open_bound_cache(cache)
    except FileNotFoundError:
        if _cleanup_target_is_absent(
            transaction_id=cache.transaction_id,
            root=cache.root,
            label="Offline-Cache",
        ):
            return
        raise
    parent_path = os.path.dirname(cache.root)
    parent_descriptor = -1
    try:
        _purge_directory_contents(descriptor)
        parent_descriptor, _parent_metadata = _open_secure_directory(
            parent_path,
            create=False,
            leaf_mode=0o700,
        )
        named = os.stat(cache.transaction_id, dir_fd=parent_descriptor, follow_symlinks=False)
        opened = os.fstat(descriptor)
        if (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino):
            raise OfflinePreflightError(
                PREPARATION_ERROR_CODE,
                "Offline-Cache wurde während der Bereinigung ersetzt.",
                "Beende konkurrierende Root-Prozesse und prüfe den Cachepfad manuell.",
            )
        os.rmdir(cache.transaction_id, dir_fd=parent_descriptor)
    finally:
        if parent_descriptor >= 0:
            os.close(parent_descriptor)
        os.close(descriptor)


def cleanup_offline_package_artifacts(receipt: OfflinePackageReceipt) -> None:
    """Bereinigt Mirror und privaten Cache anhand ihrer gebundenen Inodes."""

    if not isinstance(receipt, OfflinePackageReceipt):
        raise TypeError("Offline-Paket-Receipt fehlt")
    failures: list[tuple[str, Exception]] = []
    if receipt.wheel_mirror is not None:
        try:
            cleanup_wheel_mirror(receipt.wheel_mirror)
        except Exception as exc:
            failures.append(("Wheel-Mirror", exc))
    try:
        cleanup_offline_cache(receipt.cache)
    except Exception as exc:
        failures.append(("Offline-Cache", exc))
    if failures:
        details = "; ".join(
            f"{label}: {_cleanup_error_detail(error)}"
            for label, error in failures
        )
        raise OfflinePreflightError(
            PREPARATION_ERROR_CODE,
            f"Offline-Artefakte konnten nicht vollständig bereinigt werden: {details}",
            "Prüfe nur die genannten transaktionsgebundenen Pfade. Führe danach "
            "dieselbe gebundene Bereinigung erneut aus; bereits entfernte Artefakte "
            "werden sicher übersprungen.",
        ) from failures[0][1]


__all__ = [
    "DISK_ERROR_CODE",
    "OfflinePreflightError",
    "DiskSpaceReceipt",
    "OfflineCacheIdentity",
    "OfflineWheelMirrorReceipt",
    "OfflinePreparationPlan",
    "OfflinePackageReceipt",
    "OfflineInstallCommands",
    "require_conservative_free_space",
    "create_offline_cache",
    "create_preparation_plan",
    "execute_preparation",
    "seal_preparation",
    "verify_preparation",
    "materialize_wheel_mirror",
    "verify_wheel_mirror",
    "serialize_offline_package_receipt",
    "parse_offline_package_receipt",
    "assert_local_only_argv",
    "build_offline_install_commands",
    "cleanup_offline_cache",
    "cleanup_wheel_mirror",
    "cleanup_offline_package_artifacts",
]
