#!/usr/bin/env python3
"""SHA-gebundener zweiter Prozess für einen Release-Wechsel.

Der Bootstrap-Prozess erzeugt nach dem atomaren Git-Wechsel einen versiegelten
Ausführungssnapshot aus dem verifizierten Zielcommit und lädt den Finalizer
ausschließlich daraus. Erst dieser Prozess darf Zielmodule importieren und
Installation, Rechte, Web-Synchronisation sowie Dienststart finalisieren.
"""

from __future__ import annotations

import argparse
import io
import os
import re
import stat
import subprocess
import sys
import tarfile
from pathlib import Path


_SNAPSHOT_ROOT_FILES = ("VERSION", "installer_main.py")
_FINALIZER_FILES = (
    "Installer/__init__.py",
    "Installer/optional_service_contract.py",
    "Installer/release_finalize.py",
    "Installer/update.py",
)
_SNAPSHOT_MAX_FILES = 4096
_SNAPSHOT_MAX_FILE_BYTES = 8 * 1024 * 1024
_SNAPSHOT_MAX_TOTAL_BYTES = 128 * 1024 * 1024


def _regular_nofollow(path: Path, label: str) -> Path:
    metadata = path.lstat()
    if (
        path.is_symlink()
        or not stat.S_ISREG(metadata.st_mode)
        or metadata.st_nlink != 1
    ):
        raise RuntimeError(f"{label} ist keine eindeutige reguläre Datei")
    return path


def _bound_product_root(install_path: str) -> Path:
    raw = Path(str(install_path or ""))
    if not raw.is_absolute():
        raise RuntimeError("Installationspfad muss absolut sein")
    root = raw.resolve(strict=True)
    if root != raw:
        raise RuntimeError("Installationspfad darf kein Symlinkpfad sein")

    _regular_nofollow(root / "VERSION", "VERSION")
    _regular_nofollow(root / "installer_main.py", "installer_main.py")
    _regular_nofollow(root / "Installer" / "update.py", "Installer/update.py")
    return root


def _bound_execution_root(product_root: Path) -> Path:
    script = _regular_nofollow(Path(os.path.abspath(__file__)), "Target-Finalizer")
    snapshot_root = script.parent.parent
    if snapshot_root == product_root or snapshot_root.resolve(strict=True) != snapshot_root:
        raise RuntimeError("Target-Finalizer besitzt keinen getrennten kanonischen Ausführungssnapshot")
    return snapshot_root


def _read_regular_nofollow(path: Path, maximum: int = 1024 * 1024) -> bytes:
    _regular_nofollow(path, str(path))
    descriptor = os.open(str(path), os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
    try:
        before = os.fstat(descriptor)
        if before.st_size < 1 or before.st_size > maximum:
            raise RuntimeError(f"Target-Modul besitzt eine unzulässige Größe: {path}")
        chunks = []
        while True:
            chunk = os.read(descriptor, 64 * 1024)
            if not chunk:
                break
            chunks.append(chunk)
        after = os.fstat(descriptor)
        identity_before = (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_nlink,
            before.st_uid,
            before.st_gid,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        )
        identity_after = (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_nlink,
            after.st_uid,
            after.st_gid,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        )
        if identity_before != identity_after:
            raise RuntimeError(f"Target-Modul driftete während des Lesens: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _commit_execution_entries(root: Path, expected_commit: str) -> dict[str, tuple[bytes, int]]:
    commit = str(expected_commit or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Erwartete Release-SHA ist ungültig")
    try:
        result = subprocess.run(
            [
                "git", "-c", f"safe.directory={root}", "-c", "tar.umask=0022", "-C", str(root),
                "archive", "--format=tar", commit, "--",
                *_SNAPSHOT_ROOT_FILES,
                "Installer",
            ],
            capture_output=True,
            text=False,
            timeout=60,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Target-Commit konnte den Ausführungssnapshot nicht binden") from exc
    if result.returncode != 0:
        raise RuntimeError("Target-Commit enthält keinen eindeutigen Ausführungssnapshot")
    archive = bytes(result.stdout or b"")
    if not archive or len(archive) > _SNAPSHOT_MAX_TOTAL_BYTES + (16 * 1024 * 1024):
        raise RuntimeError("Target-Commit besitzt eine unzulässige Snapshot-Archivgröße")

    entries: dict[str, tuple[bytes, int]] = {}
    total = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(archive), mode="r:") as bundle:
            for member in bundle.getmembers():
                relative_path = str(member.name or "").rstrip("/")
                if not relative_path:
                    continue
                if (
                    relative_path.startswith("/")
                    or "\\" in relative_path
                    or any(part in {"", ".", ".."} for part in Path(relative_path).parts)
                    or not (
                        relative_path in _SNAPSHOT_ROOT_FILES
                        or relative_path == "Installer"
                        or relative_path.startswith("Installer/")
                    )
                ):
                    raise RuntimeError("Target-Commit enthält einen unzulässigen Snapshotpfad")
                if member.isdir():
                    continue
                if not member.isfile() or member.islnk() or member.issym():
                    raise RuntimeError("Target-Commit enthält keinen regulären Snapshot-Blob")
                if relative_path in entries or stat.S_IMODE(member.mode) not in {0o644, 0o755}:
                    raise RuntimeError("Target-Commit enthält einen doppelten oder unzulässigen Snapshot-Blob")
                if member.size < 0 or member.size > _SNAPSHOT_MAX_FILE_BYTES:
                    raise RuntimeError("Target-Commit enthält einen zu großen Snapshot-Blob")
                source = bundle.extractfile(member)
                if source is None:
                    raise RuntimeError("Target-Commit konnte einen Snapshot-Blob nicht lesen")
                payload = source.read(_SNAPSHOT_MAX_FILE_BYTES + 1)
                if len(payload) != member.size:
                    raise RuntimeError("Target-Commit besitzt eine driftende Snapshot-Blobgröße")
                total += len(payload)
                if len(entries) >= _SNAPSHOT_MAX_FILES or total > _SNAPSHOT_MAX_TOTAL_BYTES:
                    raise RuntimeError("Target-Commit überschreitet die feste Snapshotgröße")
                entries[relative_path] = (
                    payload,
                    0o555 if stat.S_IMODE(member.mode) & 0o111 else 0o444,
                )
    except (tarfile.TarError, OSError) as exc:
        raise RuntimeError("Target-Commit besitzt kein gültiges Snapshot-Archiv") from exc

    required = set(_SNAPSHOT_ROOT_FILES) | set(_FINALIZER_FILES)
    if not required.issubset(entries):
        raise RuntimeError("Target-Commit enthält keinen vollständigen Finalizer-Snapshot")
    return entries


def _bind_execution_snapshot(
    snapshot_root: Path,
    product_root: Path,
    expected_commit: str,
) -> None:
    expected = _commit_execution_entries(product_root, expected_commit)
    expected_directories = {""}
    for relative_path in expected:
        parts = Path(relative_path).parts
        for length in range(1, len(parts)):
            expected_directories.add(Path(*parts[:length]).as_posix())

    actual_directories = {""}
    actual_files = set()
    for directory, dirnames, filenames in os.walk(snapshot_root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        relative_directory = (
            "" if directory_path == snapshot_root
            else directory_path.relative_to(snapshot_root).as_posix()
        )
        metadata = directory_path.lstat()
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != 0o555
        ):
            raise RuntimeError("Ausführungssnapshot besitzt ein fremdes oder beschreibbares Verzeichnis")
        actual_directories.add(relative_directory)
        for name in list(dirnames):
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_metadata.st_mode):
                raise RuntimeError("Ausführungssnapshot besitzt eine Symlink-/Nichtverzeichniskomponente")
        for name in filenames:
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(child_metadata.st_mode):
                raise RuntimeError("Ausführungssnapshot besitzt eine nicht reguläre Datei")
            actual_files.add(child.relative_to(snapshot_root).as_posix())

    if actual_directories != expected_directories or actual_files != set(expected):
        raise RuntimeError("Ausführungssnapshot besitzt nicht den exakt gebundenen Dateibaum")

    for relative_path, (payload, expected_mode) in expected.items():
        target = snapshot_root / relative_path
        metadata = target.lstat()
        if (
            metadata.st_nlink != 1
            or metadata.st_uid != 0
            or metadata.st_gid != 0
            or stat.S_IMODE(metadata.st_mode) != expected_mode
            or metadata.st_mode & (stat.S_IWUSR | stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(f"Ausführungssnapshot besitzt unzulässige Metadaten: {relative_path}")
        if _read_regular_nofollow(target, maximum=_SNAPSHOT_MAX_FILE_BYTES) != payload:
            raise RuntimeError(f"Ausführungssnapshot weicht vom Ziel-Commit ab: {relative_path}")

    for relative_path in _FINALIZER_FILES:
        if _read_regular_nofollow(product_root / relative_path) != expected[relative_path][0]:
            raise RuntimeError(f"Target-Modul weicht vom freigegebenen Commit ab: {relative_path}")


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="E3DC-Control Release-Finalizer")
    parser.add_argument("--install-path", required=True)
    parser.add_argument("--expected-release-sha", required=True)
    parser.add_argument("--expected-release-tag", required=True)
    parser.add_argument("--expected-ha-role", required=True)
    parser.add_argument("--expected-config-state", required=True)
    parser.add_argument("--expected-config-sha256", required=True)
    parser.add_argument("--expected-units-sha256", required=True)
    parser.add_argument("--expected-legacy-activity", required=True)
    parser.add_argument(
        "--expected-venv-state",
        required=True,
        choices=("present", "missing", "unused"),
    )
    parser.add_argument("--expected-venv-path", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Release-Finalizer muss mit Root-Rechten laufen")
    root = _bound_product_root(args.install_path)
    execution_root = _bound_execution_root(root)
    _bind_execution_snapshot(execution_root, root, args.expected_release_sha)

    root_text = str(root)
    execution_text = str(execution_root)
    sys.path[:] = [
        execution_text,
        *[
            item
            for item in sys.path
            if item
            and os.path.realpath(item) not in {
                os.path.realpath(root_text),
                os.path.realpath(str(execution_root / "Installer")),
                os.path.realpath(execution_text),
            }
        ],
    ]

    from Installer.update import (  # pylint: disable=import-outside-toplevel
        TARGET_FINALIZER_SUCCESS,
        finalize_release_from_target,
    )
    update_module = sys.modules.get("Installer.update")
    if (
        update_module is None
        or Path(os.path.abspath(str(getattr(update_module, "__file__", "")))).parent.parent
        != execution_root
    ):
        raise RuntimeError("Installer.update wurde nicht aus dem versiegelten Snapshot geladen")

    finalize_release_from_target(
        repo_dir=root_text,
        execution_root=execution_text,
        target_commit=args.expected_release_sha,
        target_tag=args.expected_release_tag,
        expected_role=args.expected_ha_role,
        expected_config_state=args.expected_config_state,
        expected_config_sha256=args.expected_config_sha256,
        expected_units_sha256=args.expected_units_sha256,
        expected_legacy_activity=args.expected_legacy_activity,
        expected_venv_state=args.expected_venv_state,
        expected_venv_path=args.expected_venv_path,
        headless=True,
    )
    # Der Berechtigungsdurchlauf darf ausschließlich den gebundenen
    # Produktbaum verändern. Der privilegierte Ausführungssnapshot muss über
    # den gesamten Finalizer-Lauf byte- und modusidentisch bleiben.
    _bind_execution_snapshot(execution_root, root, args.expected_release_sha)
    print(
        f"{TARGET_FINALIZER_SUCCESS} "
        f"{args.expected_release_sha} {args.expected_release_tag}"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # Der aufrufende Bootstrap übernimmt Recovery und Logausgabe.
        print(f"Release-Finalizer fehlgeschlagen: {exc}", file=sys.stderr)
        raise SystemExit(1)
