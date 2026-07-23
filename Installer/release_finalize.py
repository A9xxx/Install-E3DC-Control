#!/usr/bin/env python3
"""SHA-gebundener zweiter Prozess für einen Release-Wechsel.

Der Bootstrap-Prozess lädt nach dem atomaren Git-Wechsel ausschließlich diese
Datei aus dem verifizierten Zielbaum. Erst dieser Prozess darf Zielmodule
importieren und Installation, Rechte, Web-Synchronisation sowie Dienststart
finalisieren.
"""

from __future__ import annotations

import argparse
import os
import re
import stat
import subprocess
import sys
from pathlib import Path


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

    script = _regular_nofollow(Path(os.path.abspath(__file__)), "Target-Finalizer")
    loaded_root = script.parent.parent
    if loaded_root != root:
        raise RuntimeError("Target-Finalizer stammt nicht aus dem gebundenen Zielbaum")

    _regular_nofollow(root / "VERSION", "VERSION")
    _regular_nofollow(root / "installer_main.py", "installer_main.py")
    _regular_nofollow(root / "Installer" / "update.py", "Installer/update.py")
    return root


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
        identity_before = (before.st_dev, before.st_ino, before.st_mode, before.st_size, before.st_mtime_ns)
        identity_after = (after.st_dev, after.st_ino, after.st_mode, after.st_size, after.st_mtime_ns)
        if identity_before != identity_after:
            raise RuntimeError(f"Target-Modul driftete während des Lesens: {path}")
        return b"".join(chunks)
    finally:
        os.close(descriptor)


def _bind_target_modules(root: Path, expected_commit: str) -> None:
    commit = str(expected_commit or "").strip().lower()
    if not re.fullmatch(r"[0-9a-f]{40}", commit):
        raise RuntimeError("Erwartete Release-SHA ist ungültig")
    for relative_path in (
        "Installer/__init__.py",
        "Installer/optional_service_contract.py",
        "Installer/release_finalize.py",
        "Installer/update.py",
    ):
        try:
            result = subprocess.run(
                [
                    "git", "-c", f"safe.directory={root}", "-C", str(root),
                    "cat-file", "blob", f"{commit}:{relative_path}",
                ],
                capture_output=True,
                text=False,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise RuntimeError(f"Target-Commit konnte {relative_path} nicht binden") from exc
        if result.returncode != 0:
            raise RuntimeError(f"Target-Commit enthält kein eindeutiges {relative_path}")
        actual = _read_regular_nofollow(root / relative_path)
        if actual != bytes(result.stdout or b""):
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
    root = _bound_product_root(args.install_path)
    if os.geteuid() != 0:
        raise RuntimeError("Release-Finalizer muss mit Root-Rechten laufen")
    _bind_target_modules(root, args.expected_release_sha)

    root_text = str(root)
    if sys.path[0] != root_text:
        sys.path.insert(0, root_text)

    from Installer.update import (  # pylint: disable=import-outside-toplevel
        TARGET_FINALIZER_SUCCESS,
        finalize_release_from_target,
    )

    finalize_release_from_target(
        repo_dir=root_text,
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
