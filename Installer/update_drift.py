#!/usr/bin/python3
"""Rein lesender Inhaltsvergleich vor einem Stable-Releasewechsel.

Der Vergleich trennt zwei Sachverhalte bewusst:

* Fehlende veröffentlichte Dateien kann der Updater ohne Inhaltsverlust
  wiederherstellen. Sie werden sichtbar, benötigen aber keine Sonderfreigabe.
* Lokal veränderte veröffentlichte Dateien oder unbekannte Dateien an einem
  Pfad, den das Zielrelease ersetzen würde, benötigen eine ausdrückliche
  Bestätigung.

Unbekannte Dateien außerhalb der tatsächlichen Zielprojektion werden weder
inventarisiert noch verändert. Der Detector schreibt nichts in die laufende
Installation, startet keine Dienste und erstellt kein Backup. Eine benötigte
veröffentlichte Altbaseline wird ausschließlich in den privaten
Release-Checkout geladen. Die autoritative Prüfung läuft später im
verifizierten Ziel-Updater unter dessen Update-Lock erneut.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import stat
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


CONTRACT_PATH = Path("/etc/e3dc-control/runtime_permissions_contract.json")
CONTRACT_SCHEMA = "e3dc_runtime_permissions_v1"
CONTRACT_FEATURE = "e3dc_runtime_permissions_cli_v3"
RESULT_SCHEMA = "e3dc_update_drift_v1"
MAX_CONTRACT_BYTES = 1024 * 1024
MAX_TARGET_ENTRIES = 10000
GENERATED_DIRECTORY_NAMES = frozenset(
    {"__pycache__", ".pytest_cache", ".venv", "node_modules"}
)
PUBLISHED_ORIGIN_URLS = frozenset(
    {
        "https://github.com/A9xxx/Install-E3DC-Control",
        "https://github.com/A9xxx/Install-E3DC-Control.git",
    }
)
PRESERVED_WEB_ENTRIES = frozenset(
    {
        "data",
        "history_backups",
        "logs",
        "ramdisk",
        "tmp",
        ".htaccess",
        "e3dc_paths.json",
    }
)
WEB_ROOT = Path("/var/www/html")
WEB_RELEASE_ROOT_FILES = frozenset({"VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"})
TRANSIENT_RETIRED_WEB_PATHS = frozenset(
    {
        "tmp/luxtronik.php",
        "ramdisk/bluelink_debug.json",
        "data/morning_boost_state.json",
    }
)


class DriftInspectionError(RuntimeError):
    """Der Nur-Lese-Vergleich konnte nicht sicher abgeschlossen werden."""


@dataclass(frozen=True)
class BaselineEntry:
    kind: str
    sha256: str = ""
    git_blob: str = ""
    size: int | None = None


@dataclass(frozen=True)
class TargetEntry:
    path: Path
    scope: str
    kind: str


@dataclass(frozen=True)
class ObservedEntry:
    kind: str
    fingerprint: str
    sha256: str = ""
    git_blob: str = ""
    size: int | None = None


def _stable_json_file(path: Path, *, max_bytes: int) -> tuple[dict[str, Any], str]:
    metadata = os.lstat(path)
    if (
        not stat.S_ISREG(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_nlink != 1
        or metadata.st_uid != 0
        or metadata.st_gid != 0
        or stat.S_IMODE(metadata.st_mode) & 0o022
        or metadata.st_size <= 0
        or metadata.st_size > max_bytes
    ):
        raise DriftInspectionError(f"Unsicherer JSON-Vertrag: {path}")
    parent = path.parent.absolute()
    while True:
        parent_metadata = os.lstat(parent)
        if (
            not stat.S_ISDIR(parent_metadata.st_mode)
            or stat.S_ISLNK(parent_metadata.st_mode)
            or parent_metadata.st_uid != 0
            or parent_metadata.st_gid != 0
            or stat.S_IMODE(parent_metadata.st_mode) & 0o022
        ):
            raise DriftInspectionError(f"Unsicherer Elternpfad des JSON-Vertrags: {parent}")
        if parent == parent.parent:
            break
        parent = parent.parent
    descriptor = os.open(
        path,
        os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        opened = os.fstat(descriptor)
        payload = bytearray()
        while len(payload) <= max_bytes:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes + 1 - len(payload)))
            if not chunk:
                break
            payload.extend(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    named = os.stat(path, follow_symlinks=False)
    if (
        len(payload) > max_bytes
        or (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns)
        != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        or (after.st_dev, after.st_ino) != (named.st_dev, named.st_ino)
    ):
        raise DriftInspectionError(f"JSON-Vertrag driftete beim Lesen: {path}")
    try:
        value = json.loads(bytes(payload).decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise DriftInspectionError(f"Ungültiger JSON-Vertrag: {path}") from exc
    if not isinstance(value, dict):
        raise DriftInspectionError(f"JSON-Vertrag ist kein Objekt: {path}")
    return value, hashlib.sha256(bytes(payload)).hexdigest()


def _safe_relative(value: Any) -> Path:
    raw = str(value or "").replace("\\", "/")
    candidate = Path(raw)
    if (
        not raw
        or candidate.is_absolute()
        or any(part in {"", ".", ".."} for part in candidate.parts)
    ):
        raise DriftInspectionError("Ungültiger relativer Produktpfad")
    return candidate


def _runtime_contract_baseline_identity(
    contract_digest: str,
    baseline: dict[Path, BaselineEntry],
) -> str:
    if (
        len(contract_digest) != 64
        or any(character not in "0123456789abcdef" for character in contract_digest)
    ):
        raise DriftInspectionError("Inhaltsdigest des Runtime-Vertrags ist ungültig")
    baseline_payload = json.dumps(
        [
            {
                "path": str(path),
                "kind": entry.kind,
                "sha256": entry.sha256,
                "git_blob": entry.git_blob,
                "size": entry.size,
            }
            for path, entry in sorted(baseline.items(), key=lambda item: str(item[0]))
        ],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    baseline_digest = hashlib.sha256(baseline_payload).hexdigest()
    return f"runtime_contract:{contract_digest}:{baseline_digest}"


def _contract_baseline(
    target_root: Path,
    contract_path: Path = CONTRACT_PATH,
) -> tuple[dict[Path, BaselineEntry], str] | None:
    try:
        contract, contract_digest = _stable_json_file(
            contract_path,
            max_bytes=MAX_CONTRACT_BYTES,
        )
    except (FileNotFoundError, PermissionError, DriftInspectionError, OSError):
        return None
    if (
        contract.get("schema") != CONTRACT_SCHEMA
        or contract.get("launcher_feature") != CONTRACT_FEATURE
        or Path(str(contract.get("install_root") or "")).absolute() != target_root
    ):
        return None
    roots = contract.get("roots")
    if not isinstance(roots, list) or not roots:
        return None
    baseline: dict[Path, BaselineEntry] = {}
    try:
        for root in roots:
            if not isinstance(root, dict):
                raise DriftInspectionError("Ungültige Vertragswurzel")
            root_path = Path(str(root.get("path") or "")).absolute()
            if root_path not in {target_root, WEB_ROOT}:
                continue
            entries = root.get("entries")
            if not isinstance(entries, list):
                raise DriftInspectionError("Ungültige Vertragsliste")
            for item in entries:
                if not isinstance(item, dict):
                    raise DriftInspectionError("Ungültiger Vertragseintrag")
                relative = _safe_relative(item.get("path"))
                kind = str(item.get("kind") or "")
                if kind not in {"file", "directory"}:
                    continue
                absolute = root_path / relative
                digest = str(item.get("sha256") or "")
                size_raw = item.get("size")
                size = int(size_raw) if isinstance(size_raw, int) and size_raw >= 0 else None
                if kind == "file" and (
                    len(digest) != 64
                    or any(character not in "0123456789abcdef" for character in digest)
                    or size is None
                ):
                    raise DriftInspectionError("Unvollständiger Inhaltsnachweis")
                baseline[absolute] = BaselineEntry(kind=kind, sha256=digest, size=size)
    except (DriftInspectionError, TypeError, ValueError):
        return None
    if not baseline:
        return None
    return (
        baseline,
        _runtime_contract_baseline_identity(contract_digest, baseline),
    )


def _isolated_git(target_root: Path, *arguments: str) -> subprocess.CompletedProcess[bytes]:
    environment = {
        "PATH": "/usr/bin:/bin",
        "HOME": "/nonexistent",
        "XDG_CONFIG_HOME": "/nonexistent",
        "LANG": "C",
        "LC_ALL": "C",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_SYSTEM": "/dev/null",
        "GIT_CONFIG_GLOBAL": "/dev/null",
        "GIT_ATTR_NOSYSTEM": "1",
        "GIT_NO_REPLACE_OBJECTS": "1",
        "GIT_OPTIONAL_LOCKS": "0",
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ALLOW_PROTOCOL": "https",
    }
    return subprocess.run(
        [
            "/usr/bin/git",
            "--no-replace-objects",
            "-c",
            "credential.helper=",
            "-c",
            "core.fileMode=false",
            "-c",
            "core.autocrlf=false",
            "-c",
            "protocol.file.allow=never",
            "-c",
            "protocol.ext.allow=never",
            "-c",
            "protocol.ssh.allow=never",
            "-C",
            str(target_root),
            *arguments,
        ],
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=30,
    )


def _published_release_baseline(
    target_root: Path,
    release_root: Path,
) -> tuple[dict[Path, BaselineEntry], str] | None:
    """Lädt die veröffentlichte aktuelle Version als Altbaseline.

    Der lokale ``HEAD`` ist ausdrücklich keine Autorität: Er kann nach einem
    Reparaturupdate absichtlich älter oder lokal verändert sein. Ausschließlich
    der exakt benannte veröffentlichte Tag des gebundenen GitHub-Repositories
    darf als Kompatibilitätsbaseline dienen. Der Fetch verändert nur den schon
    privaten Release-Checkout, nie die laufende Installation.
    """

    try:
        raw_version = (target_root / "VERSION").read_bytes()
        version = raw_version.decode("ascii", errors="strict").strip()
    except (OSError, UnicodeError):
        return None
    if not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-z]?", version):
        return None
    origin = _isolated_git(release_root, "remote", "get-url", "origin")
    origin_url = origin.stdout.decode("utf-8", errors="ignore").strip()
    if origin.returncode != 0 or origin_url not in PUBLISHED_ORIGIN_URLS:
        return None
    tag = f"v{version}"
    baseline_ref = f"refs/e3dc-baseline/{tag}"
    fetched = _isolated_git(
        release_root,
        "fetch",
        "--no-tags",
        "--depth=1",
        "origin",
        f"+refs/tags/{tag}:{baseline_ref}",
    )
    if fetched.returncode != 0:
        return None
    resolved = _isolated_git(release_root, "rev-parse", "--verify", f"{baseline_ref}^{{commit}}")
    commit = resolved.stdout.decode("ascii", errors="ignore").strip()
    if (
        resolved.returncode != 0
        or len(commit) != 40
        or any(character not in "0123456789abcdef" for character in commit)
    ):
        return None
    tree = _isolated_git(release_root, "ls-tree", "-r", "-z", commit)
    if tree.returncode != 0:
        return None
    baseline: dict[Path, BaselineEntry] = {}
    for record in tree.stdout.split(b"\0"):
        if not record:
            continue
        try:
            metadata, raw_path = record.split(b"\t", 1)
            raw_mode, object_type, raw_blob = metadata.split(b" ", 2)
            relative_text = raw_path.decode("utf-8", errors="strict")
            relative = _safe_relative(relative_text)
            mode = raw_mode.decode("ascii")
            blob = raw_blob.decode("ascii")
        except (ValueError, UnicodeError, DriftInspectionError):
            return None
        if object_type != b"blob" or len(blob) != 40:
            continue
        kind = "symlink" if mode == "120000" else "file"
        baseline[target_root / relative] = BaselineEntry(kind=kind, git_blob=blob)
        if relative.parts[0] == "html" and len(relative.parts) > 1:
            web_relative = Path(*relative.parts[1:])
            if web_relative.parts[0] not in PRESERVED_WEB_ENTRIES:
                baseline[WEB_ROOT / web_relative] = BaselineEntry(kind=kind, git_blob=blob)
        elif relative.as_posix() in WEB_RELEASE_ROOT_FILES:
            baseline[WEB_ROOT / relative] = BaselineEntry(kind=kind, git_blob=blob)
    return (baseline, f"published_tag:{tag}:{commit}") if baseline else None


def _target_entries(release_root: Path, target_root: Path) -> list[TargetEntry]:
    release_root = release_root.resolve(strict=True)
    entries: list[TargetEntry] = []

    def walk(
        source: Path,
        destination: Path,
        scope: str,
        *,
        excluded_top: frozenset[str] = frozenset(),
        depth: int = 0,
    ) -> None:
        for item in sorted(os.scandir(source), key=lambda candidate: candidate.name):
            if depth == 0 and item.name in excluded_top:
                continue
            if item.name in GENERATED_DIRECTORY_NAMES or item.name.endswith((".pyc", ".pyo")):
                continue
            metadata = item.stat(follow_symlinks=False)
            target = destination / item.name
            if stat.S_ISDIR(metadata.st_mode):
                entries.append(TargetEntry(path=target, scope=scope, kind="directory"))
                walk(
                    source / item.name,
                    target,
                    scope,
                    excluded_top=excluded_top,
                    depth=depth + 1,
                )
            elif stat.S_ISREG(metadata.st_mode) and metadata.st_nlink == 1:
                entries.append(TargetEntry(path=target, scope=scope, kind="file"))
            elif stat.S_ISLNK(metadata.st_mode):
                entries.append(TargetEntry(path=target, scope=scope, kind="symlink"))
            else:
                raise DriftInspectionError(f"Zielrelease enthält Spezialpfad: {source / item.name}")
            if len(entries) > MAX_TARGET_ENTRIES:
                raise DriftInspectionError("Zielrelease überschreitet die Inventurgrenze")

    walk(release_root, target_root, "installation", excluded_top=frozenset({".git"}))
    walk(
        release_root / "html",
        WEB_ROOT,
        "webroot",
        excluded_top=PRESERVED_WEB_ENTRIES,
    )
    for name in sorted(WEB_RELEASE_ROOT_FILES):
        source = release_root / name
        if not source.is_file() or source.is_symlink():
            raise DriftInspectionError(f"Zielrelease-Datei fehlt: {source}")
        entries.append(TargetEntry(path=WEB_ROOT / name, scope="webroot", kind="file"))
    return entries


def _retired_targets(release_root: Path, target_root: Path) -> list[TargetEntry]:
    try:
        policy = json.loads((release_root / "UPDATE_POLICY.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise DriftInspectionError("UPDATE_POLICY.json ist nicht sicher lesbar") from exc
    raw_targets = policy.get("delete_files") if isinstance(policy, dict) else None
    if raw_targets is None:
        return []
    if not isinstance(raw_targets, list) or len(raw_targets) > 1000:
        raise DriftInspectionError("Ungültige Löschzielliste im Zielrelease")
    targets: list[TargetEntry] = []
    seen: set[Path] = set()
    for raw in raw_targets:
        if not isinstance(raw, str) or not raw.strip() or "\0" in raw:
            raise DriftInspectionError("Ungültiges Löschziel im Zielrelease")
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = target_root / candidate
        candidate = Path(os.path.abspath(candidate))
        selected_scope = ""
        for root, scope in (
            (WEB_ROOT.absolute(), "retired_webroot"),
            (target_root.absolute(), "retired_installation"),
        ):
            try:
                relative = candidate.relative_to(root)
            except ValueError:
                continue
            if relative.parts:
                selected_scope = scope
                break
        if not selected_scope:
            raise DriftInspectionError(f"Löschziel liegt außerhalb erlaubter Produktwurzeln: {candidate}")
        if candidate not in seen:
            targets.append(TargetEntry(path=candidate, scope=selected_scope, kind="retired"))
            seen.add(candidate)
    return targets


def _fd_mount_id(descriptor: int) -> int:
    try:
        payload = Path(f"/proc/self/fdinfo/{descriptor}").read_text(encoding="ascii")
    except (OSError, UnicodeError) as exc:
        raise DriftInspectionError("Mount-Bindung der Inventur ist nicht lesbar") from exc
    for line in payload.splitlines():
        if line.startswith("mnt_id:"):
            try:
                return int(line.split(":", 1)[1].strip())
            except ValueError as exc:
                raise DriftInspectionError("Mount-Bindung der Inventur ist ungültig") from exc
    raise DriftInspectionError("Mount-Bindung der Inventur fehlt")


def _open_absolute_directory_nofollow(path: Path) -> int:
    """Öffnet auch die Wurzel selbst komponentenweise ohne Symlinkauflösung."""

    normalized = Path(os.path.normpath(os.path.abspath(str(path))))
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not normalized.is_absolute() or not nofollow or not directory:
        raise DriftInspectionError("Sichere nofollow-Inventur ist nicht verfügbar")
    flags = os.O_RDONLY | nofollow | directory | cloexec
    descriptor = os.open(os.sep, flags)
    try:
        for component in normalized.parts[1:]:
            named = os.stat(component, dir_fd=descriptor, follow_symlinks=False)
            child = os.open(component, flags, dir_fd=descriptor)
            opened = os.fstat(child)
            if (
                not stat.S_ISDIR(named.st_mode)
                or stat.S_ISLNK(named.st_mode)
                or not stat.S_ISDIR(opened.st_mode)
                or (named.st_dev, named.st_ino) != (opened.st_dev, opened.st_ino)
            ):
                os.close(child)
                raise DriftInspectionError(
                    f"Produktwurzel besitzt eine unsichere Pfadkomponente: {normalized}"
                )
            os.close(descriptor)
            descriptor = child
        return descriptor
    except Exception:
        os.close(descriptor)
        raise


def _bound_observation(
    path: Path,
    root: Path,
    *,
    allow_directory: bool,
    label: str,
) -> ObservedEntry | None:
    """Liest ein Ziel vollständig fd-relativ ohne externe Symlink-/Mountpfade."""

    root = Path(os.path.normpath(os.path.abspath(str(root))))
    path = Path(os.path.normpath(os.path.abspath(str(path))))
    try:
        relative = path.relative_to(root)
    except ValueError as exc:
        raise DriftInspectionError(f"{label} besitzt eine fremde Wurzel: {path}") from exc
    if not relative.parts:
        raise DriftInspectionError(f"Eine Produktwurzel darf kein {label} sein")
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    directory = getattr(os, "O_DIRECTORY", 0)
    cloexec = getattr(os, "O_CLOEXEC", 0)
    if not nofollow or not directory:
        raise DriftInspectionError("Sichere nofollow-Inventur ist nicht verfügbar")
    try:
        root_fd = _open_absolute_directory_nofollow(root)
    except FileNotFoundError:
        return None
    opened = [root_fd]
    try:
        root_bound = os.fstat(root_fd)
        root_mount_id = _fd_mount_id(root_fd)
        parent_fd = root_fd
        for component in relative.parts[:-1]:
            try:
                named = os.stat(component, dir_fd=parent_fd, follow_symlinks=False)
            except FileNotFoundError:
                return None
            if not stat.S_ISDIR(named.st_mode) or stat.S_ISLNK(named.st_mode):
                raise DriftInspectionError(
                    f"Unsichere Symlink-/Dateikomponente vor {label}: {path}"
                )
            try:
                child_fd = os.open(
                    component,
                    os.O_RDONLY | nofollow | directory | cloexec,
                    dir_fd=parent_fd,
                )
            except OSError as exc:
                raise DriftInspectionError(
                    f"Unsichere Pfadkomponente vor {label}: {path}"
                ) from exc
            bound = os.fstat(child_fd)
            if (
                not stat.S_ISDIR(bound.st_mode)
                or (named.st_dev, named.st_ino) != (bound.st_dev, bound.st_ino)
                or bound.st_dev != root_bound.st_dev
                or _fd_mount_id(child_fd) != root_mount_id
            ):
                os.close(child_fd)
                raise DriftInspectionError(
                    f"Fremder Mount oder driftende Pfadkomponente vor {label}: {path}"
                )
            opened.append(child_fd)
            parent_fd = child_fd
        name = relative.parts[-1]
        try:
            metadata = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
        except FileNotFoundError:
            return None
        mode = stat.S_IMODE(metadata.st_mode)
        if stat.S_ISDIR(metadata.st_mode):
            if not allow_directory:
                raise DriftInspectionError(f"{label} ist ein Verzeichnis: {path}")
            descriptor = os.open(
                name,
                os.O_RDONLY | nofollow | directory | cloexec,
                dir_fd=parent_fd,
            )
            try:
                bound = os.fstat(descriptor)
                rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if (
                    not stat.S_ISDIR(bound.st_mode)
                    or (metadata.st_dev, metadata.st_ino)
                    != (bound.st_dev, bound.st_ino)
                    or (bound.st_dev, bound.st_ino)
                    != (rebound.st_dev, rebound.st_ino)
                    or bound.st_dev != root_bound.st_dev
                    or _fd_mount_id(descriptor) != root_mount_id
                ):
                    raise DriftInspectionError(f"{label} driftete beim Lesen: {path}")
                return ObservedEntry(
                    kind="directory",
                    fingerprint=(
                        f"directory:{mode:o}:{bound.st_uid}:{bound.st_gid}"
                    ),
                )
            finally:
                os.close(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            if metadata.st_nlink != 1:
                raise DriftInspectionError(f"{label} besitzt Hardlinks: {path}")
            descriptor = os.open(name, os.O_RDONLY | nofollow | cloexec, dir_fd=parent_fd)
            try:
                bound = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(bound.st_mode)
                    or bound.st_nlink != 1
                    or (metadata.st_dev, metadata.st_ino)
                    != (bound.st_dev, bound.st_ino)
                    or bound.st_dev != root_bound.st_dev
                    or _fd_mount_id(descriptor) != root_mount_id
                ):
                    raise DriftInspectionError(f"{label} driftete beim Öffnen: {path}")
                sha256 = hashlib.sha256()
                git_blob = hashlib.sha1()
                git_blob.update(f"blob {bound.st_size}\0".encode("ascii"))
                while True:
                    chunk = os.read(descriptor, 1024 * 1024)
                    if not chunk:
                        break
                    sha256.update(chunk)
                    git_blob.update(chunk)
                rebound = os.fstat(descriptor)
            finally:
                os.close(descriptor)
            named = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (metadata.st_dev, metadata.st_ino, metadata.st_size, metadata.st_mtime_ns)
                != (bound.st_dev, bound.st_ino, bound.st_size, bound.st_mtime_ns)
                or (bound.st_dev, bound.st_ino, bound.st_size, bound.st_mtime_ns)
                != (rebound.st_dev, rebound.st_ino, rebound.st_size, rebound.st_mtime_ns)
                or (rebound.st_dev, rebound.st_ino) != (named.st_dev, named.st_ino)
            ):
                raise DriftInspectionError(f"{label} driftete beim Lesen: {path}")
            digest = sha256.hexdigest()
            return ObservedEntry(
                kind="file",
                fingerprint=f"file:{mode:o}:1:{bound.st_size}:{digest}",
                sha256=digest,
                git_blob=git_blob.hexdigest(),
                size=int(bound.st_size),
            )
        if stat.S_ISLNK(metadata.st_mode):
            target = os.readlink(name, dir_fd=parent_fd)
            rebound = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if (
                (metadata.st_dev, metadata.st_ino, metadata.st_mtime_ns)
                != (rebound.st_dev, rebound.st_ino, rebound.st_mtime_ns)
                or metadata.st_dev != root_bound.st_dev
            ):
                raise DriftInspectionError(f"{label}-Symlink driftete beim Lesen: {path}")
            payload = os.fsencode(target)
            blob = hashlib.sha1(f"blob {len(payload)}\0".encode("ascii") + payload).hexdigest()
            return ObservedEntry(
                kind="symlink",
                fingerprint="symlink:" + hashlib.sha256(payload).hexdigest(),
                git_blob=blob,
            )
        raise DriftInspectionError(f"{label} ist ein Spezialpfad: {path}")
    finally:
        for descriptor in reversed(opened):
            os.close(descriptor)


def _bound_target_observation(path: Path, root: Path) -> ObservedEntry | None:
    return _bound_observation(
        path,
        root,
        allow_directory=True,
        label="Produktziel",
    )


def _bound_retired_observation(path: Path, root: Path) -> ObservedEntry | None:
    return _bound_observation(
        path,
        root,
        allow_directory=False,
        label="Löschziel",
    )


def _observed_matches(expected: BaselineEntry, observed: ObservedEntry) -> bool:
    if expected.kind != observed.kind:
        return False
    if expected.kind == "directory":
        return True
    if expected.kind == "file" and expected.sha256:
        return expected.size == observed.size and expected.sha256 == observed.sha256
    return bool(expected.git_blob) and expected.git_blob == observed.git_blob


def _release_identity(release_root: Path) -> str:
    resolved = _isolated_git(release_root, "rev-parse", "--verify", "HEAD^{commit}")
    candidate = resolved.stdout.decode("ascii", errors="ignore").strip()
    if resolved.returncode == 0 and len(candidate) == 40 and all(
        character in "0123456789abcdef" for character in candidate
    ):
        return candidate
    digest = hashlib.sha256()
    for name in ("VERSION", "UPDATE_POLICY.json"):
        payload = (release_root / name).read_bytes()
        digest.update(name.encode("ascii") + b"\0" + payload + b"\0")
    return digest.hexdigest()


def _confirmation_token(
    release_root: Path,
    destructive: list[dict[str, str]],
    *,
    baseline_source: str,
    inspection_fingerprint: str,
) -> str:
    if not destructive and baseline_source != "unavailable":
        return ""
    payload = json.dumps(
        {
            "schema": RESULT_SCHEMA,
            "release": _release_identity(release_root),
            "baseline": baseline_source,
            "inspection_fingerprint": inspection_fingerprint,
            "content_drift": destructive,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def inspect_update_drift(
    *,
    target_root: Path,
    release_root: Path,
    contract_path: Path = CONTRACT_PATH,
) -> dict[str, Any]:
    target_root = target_root.absolute()
    release_root = release_root.resolve(strict=True)
    baseline_binding = _contract_baseline(target_root, contract_path)
    if baseline_binding is None:
        baseline_binding = _published_release_baseline(target_root, release_root)
    baseline: dict[Path, BaselineEntry]
    baseline_source: str
    if baseline_binding is None:
        baseline, baseline_source = {}, "unavailable"
    else:
        baseline, baseline_source = baseline_binding

    destructive: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []
    retired_runtime: list[dict[str, str]] = []
    snapshot_digest = hashlib.sha256()
    checked = 0
    target_entries = _target_entries(release_root, target_root)
    target_paths = {target.path for target in target_entries}
    for target in target_entries:
        checked += 1
        expected = baseline.get(target.path)
        observation_root = (
            WEB_ROOT.absolute() if target.scope == "webroot" else target_root
        )
        observed = _bound_target_observation(target.path, observation_root)
        if observed is None:
            snapshot_digest.update(
                f"{target.scope}\0{target.path}\0{target.kind}\0missing\n".encode("utf-8")
            )
            if expected is not None:
                missing.append(
                    {
                        "path": str(target.path),
                        "scope": target.scope,
                        "status": "missing_product_file",
                    }
                )
            continue
        observed_fingerprint = observed.fingerprint
        snapshot_digest.update(
            (
                f"{target.scope}\0{target.path}\0{target.kind}\0"
                f"{observed_fingerprint}\n"
            ).encode("utf-8")
        )

        if target.kind == "directory" and expected is None:
            if observed.kind == "directory":
                continue
            destructive.append(
                {
                    "path": str(target.path),
                    "scope": target.scope,
                    "status": "unknown_target_collision",
                    "fingerprint": observed_fingerprint,
                }
            )
            continue

        if expected is None:
            if baseline_source != "unavailable":
                destructive.append(
                    {
                        "path": str(target.path),
                        "scope": target.scope,
                        "status": "unknown_target_collision",
                        "fingerprint": observed_fingerprint,
                    }
                )
            continue
        if not _observed_matches(expected, observed):
            destructive.append(
                {
                    "path": str(target.path),
                    "scope": target.scope,
                    "status": "local_content_changed",
                    "fingerprint": observed_fingerprint,
                }
            )

    for target in _retired_targets(release_root, target_root):
        checked += 1
        if target.path in target_paths:
            raise DriftInspectionError(
                f"Pfad ist zugleich Zielrelease-Datei und freigegebenes Löschziel: {target.path}"
            )
        root = WEB_ROOT.absolute() if target.scope == "retired_webroot" else target_root
        if target.scope == "retired_webroot":
            ramdisk_root = WEB_ROOT.absolute() / "ramdisk"
            try:
                target.path.relative_to(ramdisk_root)
            except ValueError:
                pass
            else:
                root = ramdisk_root
        observed = _bound_retired_observation(target.path, root)
        if observed is None:
            snapshot_digest.update(
                f"{target.scope}\0{target.path}\0retired\0missing\n".encode("utf-8")
            )
            continue
        snapshot_digest.update(
            (
                f"{target.scope}\0{target.path}\0retired\0"
                f"{observed.fingerprint}\n"
            ).encode("utf-8")
        )
        is_transient_runtime = False
        if target.scope == "retired_webroot":
            web_relative = target.path.relative_to(WEB_ROOT.absolute()).as_posix()
            is_transient_runtime = web_relative in TRANSIENT_RETIRED_WEB_PATHS
        if is_transient_runtime:
            retired_runtime.append(
                {
                    "path": str(target.path),
                    "scope": target.scope,
                    "status": "managed_runtime_retire",
                    "fingerprint": observed.fingerprint,
                }
            )
            continue
        expected = baseline.get(target.path)
        if expected is None:
            destructive.append(
                {
                    "path": str(target.path),
                    "scope": target.scope,
                    "status": "unknown_retired_file",
                    "fingerprint": observed.fingerprint,
                }
            )
        elif not _observed_matches(expected, observed):
            destructive.append(
                {
                    "path": str(target.path),
                    "scope": target.scope,
                    "status": "local_retired_content_changed",
                    "fingerprint": observed.fingerprint,
                }
            )

    destructive.sort(key=lambda item: item["path"])
    missing.sort(key=lambda item: item["path"])
    retired_runtime.sort(key=lambda item: item["path"])
    inspection_fingerprint = snapshot_digest.hexdigest()
    confirmation_token = _confirmation_token(
        release_root,
        destructive,
        baseline_source=baseline_source,
        inspection_fingerprint=inspection_fingerprint,
    )
    requires_confirmation = bool(destructive) or baseline_source == "unavailable"
    return {
        "success": True,
        "schema": RESULT_SCHEMA,
        "baseline": baseline_source,
        "baseline_complete": baseline_source != "unavailable",
        "requires_confirmation": requires_confirmation,
        "confirmation_token": confirmation_token,
        "inspection_fingerprint": inspection_fingerprint,
        "content_drift_count": len(destructive),
        "content_drift": destructive,
        "missing_product_count": len(missing),
        "missing_product_files": missing,
        "managed_runtime_retire_count": len(retired_runtime),
        "managed_runtime_retire_files": retired_runtime,
        "checked_target_entries": checked,
        "message": (
            f"{len(destructive)} lokale Inhaltsabweichung(en) vor der Zielprojektion erkannt."
            if destructive
            else (
                "Die veröffentlichte Altbaseline konnte nicht sicher gebunden werden; "
                "die Zielprojektion benötigt eine bewusste generische Freigabe."
                if baseline_source == "unavailable"
                else "Keine bedrohten lokalen Produktinhalte erkannt."
            )
        ),
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Nur-Lese-Prüfung lokaler Updateinhalte")
    parser.add_argument("--target", required=True)
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--json", action="store_true", required=True)
    return parser


def main(argv: Iterable[str] | None = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    try:
        result = inspect_update_drift(
            target_root=Path(args.target),
            release_root=Path(args.release_root),
        )
    except Exception as exc:
        result = {
            "success": False,
            "schema": RESULT_SCHEMA,
            "error": "inspection_failed",
            "message": str(exc).strip() or exc.__class__.__name__,
        }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":")))
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())
