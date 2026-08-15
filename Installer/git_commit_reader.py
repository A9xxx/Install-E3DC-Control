"""Fail-closed Leser für freigegebene Git-Commit-Blobs.

Die Funktionen lesen ausschließlich Objekt-IDs und Blobbytes. Arbeitsbaum-
Filter, Hooks, Replace-Refs, Archive-Treiber und vererbte Git-Konfiguration
gehören nicht zur Vertrauenskette.
"""

from __future__ import annotations

import io
import hashlib
import os
from pathlib import Path, PurePosixPath
import pwd
import re
import subprocess
from typing import Iterable, Sequence


FULL_SHA1_RE = re.compile(r"[0-9a-f]{40}\Z")
GIT_PATH = "/usr/bin/git"
ENV_PATH = "/usr/bin/env"
SUDO_PATH = "/usr/bin/sudo"
ACCOUNT_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]*\Z")


def repository_git_reader_user(repo_root: str | Path) -> str | None:
    """Senkt root-seitige Git-Leser auf den gebundenen Repository-Eigentümer ab."""

    root = Path(repo_root)
    metadata = root.lstat()
    git_metadata = (root / ".git").lstat()
    if (
        root.is_symlink()
        or not root.is_dir()
        or (root / ".git").is_symlink()
        or not (root / ".git").is_dir()
        or metadata.st_uid == 0
        or git_metadata.st_uid != metadata.st_uid
    ):
        raise RuntimeError("Git-Repository besitzt keinen gebundenen Nicht-Root-Eigentümer")
    try:
        account = pwd.getpwuid(metadata.st_uid)
    except KeyError as exc:
        raise RuntimeError("Git-Repository-Eigentümer fehlt lokal") from exc
    if not ACCOUNT_NAME_RE.fullmatch(account.pw_name):
        raise RuntimeError("Git-Repository-Eigentümer besitzt keinen sicheren Namen")
    if os.geteuid() == metadata.st_uid:
        return None
    if os.geteuid() != 0:
        raise RuntimeError("Git-Leser läuft weder als Root noch als Repository-Eigentümer")
    return account.pw_name


def isolated_git_environment_assignments() -> tuple[str, ...]:
    return (
        "PATH=/usr/bin:/bin",
        "HOME=/nonexistent",
        "XDG_CONFIG_HOME=/nonexistent",
        "LANG=C",
        "LC_ALL=C",
        "GIT_CONFIG_NOSYSTEM=1",
        "GIT_CONFIG_SYSTEM=/dev/null",
        "GIT_CONFIG_GLOBAL=/dev/null",
        "GIT_ATTR_NOSYSTEM=1",
        "GIT_NO_REPLACE_OBJECTS=1",
        "GIT_OPTIONAL_LOCKS=0",
        "GIT_TERMINAL_PROMPT=0",
        "GIT_ASKPASS=/bin/false",
        "SSH_ASKPASS=/bin/false",
        "GIT_ALLOW_PROTOCOL=https",
    )


def isolated_git_command(
    repo_root: str | Path,
    *args: str,
    run_as_user: str | None = None,
) -> list[str]:
    root = Path(repo_root)
    git_dir = root / ".git"
    if not root.is_absolute() or root.resolve(strict=True) != root:
        raise RuntimeError("Git-Produktpfad ist nicht absolut und kanonisch")
    if not git_dir.is_dir() or git_dir.is_symlink():
        raise RuntimeError("Git-Objektdatenbank ist kein gebundenes Verzeichnis")

    prefix: list[str]
    if run_as_user is not None:
        user = str(run_as_user or "").strip()
        if not ACCOUNT_NAME_RE.fullmatch(user):
            raise RuntimeError("Git-Lesebenutzer ist ungültig")
        try:
            account = pwd.getpwnam(user)
        except KeyError as exc:
            raise RuntimeError("Git-Lesebenutzer fehlt lokal") from exc
        if account.pw_uid == 0:
            raise RuntimeError("Git-Lesebenutzer darf nicht Root sein")
        prefix = [SUDO_PATH, "-n", "-H", "-u", user, "--", ENV_PATH, "-i"]
    else:
        prefix = [ENV_PATH, "-i"]

    return [
        *prefix,
        *isolated_git_environment_assignments(),
        GIT_PATH,
        "--no-replace-objects",
        "-c",
        f"safe.directory={root}",
        "-c",
        "core.hooksPath=/dev/null",
        "-c",
        "core.fsmonitor=false",
        "-c",
        "core.untrackedCache=false",
        "-c",
        "core.attributesFile=/dev/null",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=/bin/false",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=never",
        f"--git-dir={git_dir}",
        f"--work-tree={root}",
        *args,
    ]


def run_isolated_git(
    repo_root: str | Path,
    *args: str,
    run_as_user: str | None = None,
    input_bytes: bytes | None = None,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    try:
        return subprocess.run(
            isolated_git_command(repo_root, *args, run_as_user=run_as_user),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Isolierter Git-Leseaufruf ist fehlgeschlagen") from exc


def run_isolated_remote_git(
    remote_url: str,
    *args: str,
    timeout: int = 30,
) -> subprocess.CompletedProcess[bytes]:
    """Führt einen HTTPS-Remote-Leseaufruf ohne lokales Repository aus."""

    url = str(remote_url or "").strip()
    if (
        not re.fullmatch(r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._/-]+\.git", url)
        or any(character in url for character in ("\\", "..", "@"))
    ):
        raise RuntimeError("Git-Remote-URL ist nicht als HTTPS-Repository gebunden")
    if not args or args[0] != "ls-remote" or args.count(url) != 1:
        raise RuntimeError("Git-Remoteaufruf ist nicht auf genau eine feste URL begrenzt")
    command = [
        ENV_PATH,
        "-i",
        *isolated_git_environment_assignments(),
        GIT_PATH,
        "--no-replace-objects",
        "-c",
        "credential.helper=",
        "-c",
        "core.askPass=/bin/false",
        "-c",
        "protocol.ext.allow=never",
        "-c",
        "protocol.file.allow=never",
        *args,
    ]
    try:
        return subprocess.run(
            command,
            cwd="/",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise RuntimeError("Isolierter Git-Remoteaufruf ist fehlgeschlagen") from exc


def _validate_commit_path(raw_path: bytes) -> str:
    if len(raw_path) > 4096 or any(len(component) > 255 for component in raw_path.split(b"/")):
        raise RuntimeError("Commit-Pfad überschreitet die feste Längengrenze")
    try:
        path = raw_path.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Commit enthält keinen gültigen UTF-8-Pfad") from exc
    pure = PurePosixPath(path)
    if (
        not path
        or path.startswith("/")
        or "\\" in path
        or any(part in {"", ".", ".."} for part in pure.parts)
        or any(ord(character) < 32 or ord(character) == 127 for character in path)
        or pure.as_posix() != path
    ):
        raise RuntimeError("Commit enthält einen unzulässigen Pfad")
    return path


def _read_verified_objects(
    repo_root: str | Path,
    object_ids: Sequence[str],
    *,
    run_as_user: str | None,
    allowed_types: frozenset[bytes],
    maximum_object_bytes: int,
    maximum_total_bytes: int,
) -> dict[str, tuple[bytes, bytes]]:
    ordered_ids = list(dict.fromkeys(object_ids))
    request = b"".join(object_id.encode("ascii") + b"\n" for object_id in ordered_ids)
    completed = run_isolated_git(
        repo_root,
        "cat-file",
        "--batch",
        run_as_user=run_as_user,
        input_bytes=request,
        timeout=60,
    )
    if completed.returncode != 0:
        detail = completed.stderr.decode("utf-8", errors="replace").strip()
        raise RuntimeError("Commit-Objekte konnten nicht gelesen werden: " + detail[-500:])

    stream = io.BytesIO(bytes(completed.stdout or b""))
    result: dict[str, tuple[bytes, bytes]] = {}
    total = 0
    for expected_id in ordered_ids:
        header = stream.readline()
        try:
            raw_id, object_type, raw_size = header.rstrip(b"\n").split(b" ", 2)
            actual_id = raw_id.decode("ascii")
            size = int(raw_size)
        except (UnicodeDecodeError, ValueError) as exc:
            raise RuntimeError("Commit-Objekt besitzt keine eindeutige Batch-Antwort") from exc
        if actual_id != expected_id or object_type not in allowed_types:
            raise RuntimeError("Commit-Objekt besitzt nicht den gebundenen Typ")
        if size < 0 or size > maximum_object_bytes:
            raise RuntimeError("Commit-Objekt besitzt eine unzulässige Größe")
        payload = stream.read(size)
        if len(payload) != size or stream.read(1) != b"\n":
            raise RuntimeError("Commit-Objekt wurde nicht vollständig gelesen")
        digest = hashlib.new(
            "sha1",
            object_type + b" " + str(size).encode("ascii") + b"\0" + payload,
            usedforsecurity=False,
        ).hexdigest()
        if digest != expected_id:
            raise RuntimeError("Commit-Objekt stimmt kryptographisch nicht mit seiner OID überein")
        total += size
        if total > maximum_total_bytes:
            raise RuntimeError("Commit-Objekte überschreiten die feste Gesamtgröße")
        result[expected_id] = (object_type, payload)
    if stream.read(1):
        raise RuntimeError("Git lieferte unerwartete zusätzliche Blobbytes")
    return result


def _commit_root_tree(
    repo_root: str | Path,
    commit: str,
    *,
    run_as_user: str | None,
) -> str:
    objects = _read_verified_objects(
        repo_root,
        (commit,),
        run_as_user=run_as_user,
        allowed_types=frozenset({b"commit"}),
        maximum_object_bytes=16 * 1024 * 1024,
        maximum_total_bytes=16 * 1024 * 1024,
    )
    _object_type, payload = objects[commit]
    first_line = payload.split(b"\n", 1)[0]
    if not first_line.startswith(b"tree "):
        raise RuntimeError("Commit-Objekt besitzt keinen gebundenen Wurzelbaum")
    try:
        tree_id = first_line[5:].decode("ascii")
    except UnicodeDecodeError as exc:
        raise RuntimeError("Commit-Wurzelbaum besitzt keine ASCII-OID") from exc
    if not FULL_SHA1_RE.fullmatch(tree_id):
        raise RuntimeError("Commit-Wurzelbaum besitzt keine volle SHA-1")
    return tree_id


def _parse_tree_payload(payload: bytes) -> list[tuple[str, bytes, str]]:
    result: list[tuple[str, bytes, str]] = []
    offset = 0
    while offset < len(payload):
        separator = payload.find(b" ", offset)
        terminator = payload.find(b"\0", separator + 1)
        if separator <= offset or terminator <= separator + 1 or terminator + 21 > len(payload):
            raise RuntimeError("Commit-Baum besitzt kein kanonisches Binärformat")
        raw_mode = payload[offset:separator]
        raw_name = payload[separator + 1:terminator]
        raw_oid = payload[terminator + 1:terminator + 21]
        try:
            mode = raw_mode.decode("ascii")
        except UnicodeDecodeError as exc:
            raise RuntimeError("Commit-Baum besitzt keinen ASCII-Modus") from exc
        if (
            mode not in {"40000", "100644", "100755"}
            or not raw_name
            or b"/" in raw_name
            or raw_name in {b".", b".."}
        ):
            raise RuntimeError("Commit-Baum enthält einen unzulässigen Pfadtyp")
        result.append((mode, raw_name, raw_oid.hex()))
        offset = terminator + 21
    if offset != len(payload):
        raise RuntimeError("Commit-Baum besitzt überzählige Binärdaten")
    return result


def _verified_commit_metadata(
    repo_root: str | Path,
    commit: str,
    canonical_includes: Sequence[str],
    *,
    include_all: bool,
    run_as_user: str | None,
    maximum_files: int,
) -> dict[str, tuple[str, int]]:
    root_tree = _commit_root_tree(repo_root, commit, run_as_user=run_as_user)
    frontier: list[tuple[str, bytes, int]] = [(root_tree, b"", 0)]
    tree_cache: dict[str, bytes] = {}
    metadata: dict[str, tuple[str, int]] = {}
    casefold_paths: set[str] = set()
    record_count = 0
    tree_total_bytes = 0
    maximum_records = max(4096, maximum_files * 8)

    while frontier:
        missing = tuple(dict.fromkeys(
            object_id for object_id, _prefix, _depth in frontier
            if object_id not in tree_cache
        ))
        if missing:
            objects = _read_verified_objects(
                repo_root,
                missing,
                run_as_user=run_as_user,
                allowed_types=frozenset({b"tree"}),
                maximum_object_bytes=16 * 1024 * 1024,
                maximum_total_bytes=32 * 1024 * 1024,
            )
            tree_cache.update(
                (object_id, payload)
                for object_id, (_object_type, payload) in objects.items()
            )
            tree_total_bytes += sum(len(payload) for _object_type, payload in objects.values())
            if tree_total_bytes > 32 * 1024 * 1024:
                raise RuntimeError("Commit-Bäume überschreiten die feste Gesamtgröße")

        next_frontier: list[tuple[str, bytes, int]] = []
        for object_id, raw_prefix, depth in frontier:
            if depth > 64:
                raise RuntimeError("Commit-Baum überschreitet die feste Verzeichnistiefe")
            for mode, raw_name, child_id in _parse_tree_payload(tree_cache[object_id]):
                record_count += 1
                if record_count > maximum_records:
                    raise RuntimeError("Commit-Baum überschreitet die feste Strukturgröße")
                raw_path = raw_name if not raw_prefix else raw_prefix + b"/" + raw_name
                path = _validate_commit_path(raw_path)
                folded = path.casefold()
                if folded in casefold_paths:
                    raise RuntimeError("Commit-Baum enthält mehrdeutige Pfadnamen")
                casefold_paths.add(folded)
                if mode == "40000":
                    next_frontier.append((child_id, raw_path, depth + 1))
                    continue
                if include_all or any(
                    path == include or path.startswith(include + "/")
                    for include in canonical_includes
                ):
                    metadata[path] = (
                        child_id,
                        0o555 if mode == "100755" else 0o444,
                    )
                    if len(metadata) > maximum_files:
                        raise RuntimeError("Commit-Baum überschreitet die feste Dateizahl")
        frontier = next_frontier
    return metadata


def read_commit_entries(
    repo_root: str | Path,
    expected_commit: str,
    include_paths: Iterable[str],
    *,
    required_paths: Iterable[str] = (),
    include_all: bool = False,
    run_as_user: str | None = None,
    maximum_files: int = 4096,
    maximum_file_bytes: int = 8 * 1024 * 1024,
    maximum_total_bytes: int = 128 * 1024 * 1024,
) -> dict[str, tuple[bytes, int]]:
    commit = str(expected_commit or "").strip().lower()
    if not FULL_SHA1_RE.fullmatch(commit):
        raise RuntimeError("Erwartete Release-SHA ist ungültig")
    root = Path(repo_root)

    object_format = run_isolated_git(
        root,
        "rev-parse",
        "--show-object-format",
        run_as_user=run_as_user,
        timeout=15,
    )
    if object_format.returncode != 0 or object_format.stdout.strip() != b"sha1":
        raise RuntimeError("Git-Repository verwendet nicht das gebundene SHA-1-Objektformat")
    replace_refs = run_isolated_git(
        root,
        "for-each-ref",
        "--format=%(refname)",
        "refs/replace/",
        run_as_user=run_as_user,
        timeout=15,
    )
    if replace_refs.returncode != 0 or replace_refs.stdout.strip():
        raise RuntimeError("Git-Repository enthält Replace-Refs")
    grafts = root / ".git" / "info" / "grafts"
    if grafts.exists() or grafts.is_symlink():
        raise RuntimeError("Git-Repository enthält eine Legacy-Graft-Datei")

    includes = tuple(str(path) for path in include_paths)
    if not includes and not include_all:
        raise RuntimeError("Commit-Leser besitzt keine freigegebenen Pfade")
    if includes and include_all:
        raise RuntimeError("Commit-Leser darf Vollbaum und Teilpfade nicht mischen")
    canonical_includes: list[str] = []
    for include in includes:
        try:
            encoded = include.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RuntimeError("Commit-Leser besitzt keinen gültigen UTF-8-Pfad") from exc
        canonical_includes.append(_validate_commit_path(encoded))
    metadata = _verified_commit_metadata(
        root,
        commit,
        canonical_includes,
        include_all=include_all,
        run_as_user=run_as_user,
        maximum_files=maximum_files,
    )

    required = set(required_paths)
    for required_path in required:
        try:
            encoded = required_path.encode("utf-8")
        except UnicodeEncodeError as exc:
            raise RuntimeError("Pflicht-Blob besitzt keinen gültigen UTF-8-Pfad") from exc
        if _validate_commit_path(encoded) != required_path:
            raise RuntimeError("Pflicht-Blobpfad ist nicht kanonisch")
    if not required.issubset(metadata):
        missing = ", ".join(sorted(required.difference(metadata)))
        raise RuntimeError("Commit-Baum ist unvollständig: " + missing)
    objects = _read_verified_objects(
        root,
        [object_id for object_id, _mode in metadata.values()],
        run_as_user=run_as_user,
        allowed_types=frozenset({b"blob"}),
        maximum_object_bytes=maximum_file_bytes,
        maximum_total_bytes=maximum_total_bytes,
    )
    return {
        path: (objects[object_id][1], mode)
        for path, (object_id, mode) in metadata.items()
    }
