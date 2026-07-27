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
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
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
_COMPAT_SNAPSHOT_PARENT = Path("/run")
_COMPAT_SNAPSHOT_PREFIX = ".e3dc-release-finalizer-compat-"
_FINALIZER_SUCCESS = "E3DC_RELEASE_TARGET_FINALIZER_OK"


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


def _file_identity(metadata: os.stat_result) -> tuple[int, ...]:
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


def _bind_legacy_product_invocation(
    root: Path,
    args: argparse.Namespace,
) -> dict[str, tuple[bytes, int]]:
    """Bindet den direkten Aufruf veröffentlichter Updater vor dem Snapshotwechsel."""

    install_root = str(os.environ.get("E3DC_INSTALL_ROOT") or "")
    if (
        not install_root
        or os.path.abspath(install_root) != str(root)
        or os.path.realpath(install_root) != str(root)
    ):
        raise RuntimeError("Direkter Target-Finalizer besitzt keine gebundene Installationswurzel")
    if any(
        os.environ.get(name)
        for name in (
            "E3DC_BOOTSTRAP_ROOT",
            "E3DC_BOOTSTRAP_RUNNER_ROOT",
            "E3DC_BOOTSTRAP_USER",
            "E3DC_BOOTSTRAP_VENV",
            "PYTHONHOME",
            "PYTHONPATH",
        )
    ):
        raise RuntimeError("Direkter Target-Finalizer besitzt einen widersprüchlichen Bootstrap-Kontext")

    entries = _commit_execution_entries(root, args.expected_release_sha)
    version_bytes = _read_regular_nofollow(root / "VERSION", maximum=256)
    if version_bytes != entries["VERSION"][0]:
        raise RuntimeError("Produktversion weicht vom freigegebenen Ziel-Commit ab")
    try:
        version = version_bytes.decode("utf-8").strip()
    except UnicodeDecodeError as exc:
        raise RuntimeError("Produktversion ist nicht als UTF-8 gebunden") from exc
    expected_tag = f"v{version}"
    if (
        not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+[a-z]?", version)
        or args.expected_release_tag != expected_tag
    ):
        raise RuntimeError("Release-Tag und Produktversion sind nicht kohärent")

    head = subprocess.run(
        [
            "git",
            "-c",
            f"safe.directory={root}",
            "-C",
            str(root),
            "rev-parse",
            "--verify",
            "HEAD^{commit}",
        ],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    actual_head = str(head.stdout or "").strip().lower()
    if head.returncode != 0 or actual_head != args.expected_release_sha.lower():
        raise RuntimeError("Direkter Target-Finalizer sieht nicht den freigegebenen Ziel-Commit")

    root_owner = root.lstat().st_uid
    for relative_path in _FINALIZER_FILES:
        target = root / relative_path
        metadata = target.lstat()
        if (
            target.is_symlink()
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid not in {0, root_owner}
            or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
        ):
            raise RuntimeError(
                f"Direktes Target-Modul besitzt unzulässige Metadaten: {relative_path}"
            )
        if _read_regular_nofollow(
            target,
            maximum=_SNAPSHOT_MAX_FILE_BYTES,
        ) != entries[relative_path][0]:
            raise RuntimeError(
                f"Direktes Target-Modul weicht vom freigegebenen Commit ab: {relative_path}"
            )
    return entries


def _trusted_snapshot_parent(parent: Path) -> Path:
    if not parent.is_absolute():
        raise RuntimeError("Snapshot-Elternverzeichnis muss absolut sein")
    canonical = parent.resolve(strict=True)
    metadata = parent.lstat()
    if (
        canonical != parent
        or parent.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
        or metadata.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
    ):
        raise RuntimeError("Snapshot-Elternverzeichnis ist nicht vertrauenswürdig")
    descriptor = os.open(
        str(parent),
        os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0),
    )
    try:
        if _file_identity(os.fstat(descriptor)) != _file_identity(metadata):
            raise RuntimeError("Snapshot-Elternverzeichnis driftete während der Bindung")
    finally:
        os.close(descriptor)
    return parent


def _remove_compat_execution_snapshot(snapshot_root: Path, parent: Path) -> None:
    root = Path(os.path.abspath(snapshot_root))
    bound_parent = Path(os.path.abspath(parent))
    if (
        root.parent != bound_parent
        or not root.name.startswith(_COMPAT_SNAPSHOT_PREFIX)
    ):
        raise RuntimeError("Kompatibilitäts-Snapshot liegt außerhalb der gebundenen Wurzel")
    try:
        metadata = root.lstat()
    except FileNotFoundError:
        return
    if (
        root.is_symlink()
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != os.geteuid()
        or metadata.st_gid != os.getegid()
    ):
        raise RuntimeError("Kompatibilitäts-Snapshot ist vor der Bereinigung nicht gebunden")
    for directory, dirnames, filenames in os.walk(root, topdown=True, followlinks=False):
        directory_path = Path(directory)
        for name in dirnames:
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISDIR(child_metadata.st_mode):
                raise RuntimeError("Kompatibilitäts-Snapshot enthält vor der Bereinigung einen Symlink")
        for name in filenames:
            child = directory_path / name
            child_metadata = child.lstat()
            if child.is_symlink() or not stat.S_ISREG(child_metadata.st_mode):
                raise RuntimeError("Kompatibilitäts-Snapshot enthält vor der Bereinigung eine Fremddatei")
        os.chmod(directory_path, 0o700)
    shutil.rmtree(root)


def _create_compat_execution_snapshot(
    entries: dict[str, tuple[bytes, int]],
    product_root: Path,
    expected_commit: str,
    *,
    snapshot_parent: Path = _COMPAT_SNAPSHOT_PARENT,
) -> Path:
    """Erzeugt den einmaligen, versiegelten Übergang für veröffentlichte Alt-Updater."""

    parent = _trusted_snapshot_parent(snapshot_parent)
    snapshot_root = Path(
        tempfile.mkdtemp(
            prefix=_COMPAT_SNAPSHOT_PREFIX,
            dir=str(parent),
        )
    )
    directories = {snapshot_root}
    try:
        root_metadata = snapshot_root.lstat()
        if (
            not stat.S_ISDIR(root_metadata.st_mode)
            or root_metadata.st_uid != os.geteuid()
            or root_metadata.st_gid != os.getegid()
            or stat.S_IMODE(root_metadata.st_mode) != 0o700
        ):
            raise RuntimeError("Kompatibilitäts-Snapshot wurde nicht privat erzeugt")

        for relative_path, (payload, final_mode) in sorted(entries.items()):
            if final_mode not in {0o444, 0o555}:
                raise RuntimeError("Kompatibilitäts-Snapshot enthält einen beschreibbaren Zielmodus")
            target = snapshot_root / relative_path
            target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            current = target.parent
            while current != snapshot_root:
                directories.add(current)
                current = current.parent
            descriptor = os.open(
                str(target),
                os.O_WRONLY
                | os.O_CREAT
                | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
            try:
                view = memoryview(payload)
                written = 0
                while written < len(view):
                    count = os.write(descriptor, view[written:])
                    if count <= 0:
                        raise RuntimeError(
                            "Kompatibilitäts-Snapshot konnte einen Blob nicht vollständig schreiben"
                        )
                    written += count
                os.fsync(descriptor)
                os.fchmod(descriptor, final_mode)
                sealed = os.fstat(descriptor)
                if (
                    not stat.S_ISREG(sealed.st_mode)
                    or sealed.st_nlink != 1
                    or sealed.st_uid != os.geteuid()
                    or sealed.st_gid != os.getegid()
                    or stat.S_IMODE(sealed.st_mode) != final_mode
                    or sealed.st_size != len(payload)
                ):
                    raise RuntimeError(
                        "Kompatibilitäts-Snapshot konnte eine Datei nicht versiegeln"
                    )
            finally:
                os.close(descriptor)

        for directory in sorted(directories, key=lambda item: len(item.parts), reverse=True):
            os.chmod(directory, 0o555)
        _bind_execution_snapshot(snapshot_root, product_root, expected_commit)
        return snapshot_root
    except Exception:
        _remove_compat_execution_snapshot(snapshot_root, parent)
        raise


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


def _run_legacy_product_bridge(
    root: Path,
    args: argparse.Namespace,
) -> int:
    entries = _bind_legacy_product_invocation(root, args)
    snapshot_root = _create_compat_execution_snapshot(
        entries,
        root,
        args.expected_release_sha,
    )
    python = str(sys.executable or "")
    if not os.path.isabs(python) or not os.access(python, os.X_OK):
        _remove_compat_execution_snapshot(snapshot_root, _COMPAT_SNAPSHOT_PARENT)
        raise RuntimeError("Python-Interpreter des Target-Finalizers ist nicht eindeutig ausführbar")

    environment = dict(os.environ)
    for name in (
        "E3DC_BOOTSTRAP_ROOT",
        "E3DC_BOOTSTRAP_RUNNER_ROOT",
        "E3DC_BOOTSTRAP_USER",
        "E3DC_BOOTSTRAP_VENV",
        "PYTHONHOME",
        "PYTHONPATH",
    ):
        environment.pop(name, None)
    environment["E3DC_BOOTSTRAP_ROOT"] = str(root)
    environment["E3DC_BOOTSTRAP_RUNNER_ROOT"] = str(snapshot_root)
    environment["E3DC_INSTALL_ROOT"] = str(root)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["LC_ALL"] = "C.UTF-8"
    environment["LANG"] = "C.UTF-8"

    finalizer = snapshot_root / "Installer" / "release_finalize.py"
    command = [
        python,
        str(finalizer),
        "--install-path",
        str(root),
        "--expected-release-sha",
        args.expected_release_sha,
        "--expected-release-tag",
        args.expected_release_tag,
        "--expected-ha-role",
        args.expected_ha_role,
        "--expected-config-state",
        args.expected_config_state,
        "--expected-config-sha256",
        args.expected_config_sha256,
        "--expected-units-sha256",
        args.expected_units_sha256,
        "--expected-legacy-activity",
        args.expected_legacy_activity,
        "--expected-venv-state",
        args.expected_venv_state,
        "--expected-venv-path",
        args.expected_venv_path,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=900,
            env=environment,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Kompatibilitäts-Snapshot konnte den Finalizer nicht ausführen") from exc
    finally:
        try:
            _remove_compat_execution_snapshot(snapshot_root, _COMPAT_SNAPSHOT_PARENT)
        except Exception as exc:
            sys.stderr.write(
                "WARNUNG: Kompatibilitäts-Snapshot konnte nach dem "
                f"Finalizerlauf nicht bereinigt werden: {exc}\n"
            )

    marker = (
        f"{_FINALIZER_SUCCESS} "
        f"{args.expected_release_sha} {args.expected_release_tag}"
    )
    lines = [line.strip() for line in str(result.stdout or "").splitlines()]
    if result.returncode != 0 or lines.count(marker) != 1:
        detail = "\n".join(
            part.strip()
            for part in (str(result.stdout or ""), str(result.stderr or ""))
            if part.strip()
        )
        raise RuntimeError(
            "Kompatibilitäts-Snapshot meldete keinen eindeutigen Erfolg: "
            + detail[-4000:]
        )
    if result.stderr:
        sys.stderr.write(result.stderr)
    sys.stdout.write(result.stdout)
    return 0


def main() -> int:
    args = _parse_args()
    if os.geteuid() != 0:
        raise RuntimeError("Release-Finalizer muss mit Root-Rechten laufen")
    root = _bound_product_root(args.install_path)
    script = _regular_nofollow(Path(os.path.abspath(__file__)), "Target-Finalizer")
    if script.parent.parent == root:
        return _run_legacy_product_bridge(root, args)
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
