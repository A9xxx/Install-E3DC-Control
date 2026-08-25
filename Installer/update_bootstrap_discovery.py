#!/usr/bin/env python3
"""Bindet einen Rettungsupdate-Aufruf an Installation, Benutzer und Rolle.

Das Modul liest ausschließlich lokale Systemquellen. Es verändert weder den
Produktbaum noch Dienste und ist damit die gemeinsame, testbare Vorstufe für
den Community-Bootstrap und den installierten Update-Dispatcher.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
import sys


VALID_ROLES = frozenset({"off", "master", "slave", "shadow"})
ACTIVE_UNIT_STATES = frozenset({"active", "activating"})
CONTROL_CHARACTERS = ("\x00", "\t", "\r", "\n")
PRODUCT_MARKERS = (
    ("VERSION", "file"),
    ("installer_main.py", "file"),
    ("Installer", "directory"),
)
INSTANCE_ANCHOR_DIRECTORY = Path("/etc/e3dc-control/instances.d")
INSTANCE_ANCHOR_SCHEMA = "e3dc_update_instance_v1"


@dataclass(frozen=True)
class Binding:
    root: str
    user: str
    role: str


@dataclass(frozen=True)
class InstallationCandidate:
    root: str
    tier: int
    sources: tuple[str, ...]


class DiscoveryError(RuntimeError):
    """Maschinenlesbarer Abbruch mit einer unmittelbar ausführbaren Lösung."""

    def __init__(self, code: str, message: str, solution: str):
        super().__init__(message)
        self.code = code
        self.solution = solution


def _has_control_characters(value: object) -> bool:
    text = str(value or "")
    return any(character in text for character in CONTROL_CHARACTERS)


def valid_user(raw: object) -> str | None:
    value = str(raw or "").strip()
    if (
        not value
        or "/" in value
        or "\\" in value
        or not all(character.isalnum() or character in {"_", "-", "."} for character in value)
        or value in {"root", "www-data"}
    ):
        return None
    try:
        pwd.getpwnam(value)
    except KeyError:
        return None
    return value


def canonical_product_root(
    raw: object,
    *,
    allow_one_missing_marker: bool = False,
    allow_all_missing_markers: bool = False,
    ignore_product_markers: bool = False,
) -> Path | None:
    text = str(raw or "")
    if _has_control_characters(text):
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    lexical = Path(os.path.abspath(text))
    try:
        candidate = lexical.resolve(strict=True)
    except OSError:
        return None
    if str(candidate) in {"/", "/bin", "/etc", "/home", "/lib", "/sbin", "/usr", "/var"}:
        return None
    try:
        if not stat.S_ISDIR(os.stat(candidate).st_mode):
            return None
        if not ignore_product_markers:
            missing_markers = 0
            for relative, expected_kind in PRODUCT_MARKERS:
                marker = candidate / relative
                try:
                    metadata = os.lstat(marker)
                except FileNotFoundError:
                    missing_markers += 1
                    continue
                valid_kind = (
                    stat.S_ISREG(metadata.st_mode)
                    if expected_kind == "file"
                    else stat.S_ISDIR(metadata.st_mode)
                )
                if stat.S_ISLNK(metadata.st_mode) or not valid_kind:
                    missing_markers += 1
            allowed_missing = (
                len(PRODUCT_MARKERS)
                if allow_all_missing_markers
                else (1 if allow_one_missing_marker else 0)
            )
            if missing_markers > allowed_missing:
                return None
    except OSError:
        return None
    return candidate


def product_root_from_hint(
    raw: object,
    *,
    allow_one_missing_marker: bool = False,
    allow_all_missing_markers: bool = False,
    ignore_product_markers: bool = False,
) -> Path | None:
    text = str(raw or "")
    if _has_control_characters(text):
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    current = Path(os.path.abspath(text))
    if current.suffix and not current.is_dir():
        current = current.parent
    for _ in range(8):
        root = canonical_product_root(
            current,
            allow_one_missing_marker=allow_one_missing_marker,
            allow_all_missing_markers=allow_all_missing_markers,
            ignore_product_markers=ignore_product_markers,
        )
        if root is not None:
            return root
        if current == current.parent:
            break
        current = current.parent
    return None


def _unit_working_directory_root(raw: object) -> str:
    """Leitet aus einem root-eigenen Unit-Arbeitsverzeichnis den Produktroot ab."""

    text = str(raw or "").strip().lstrip("-")
    if _has_control_characters(text) or not text.startswith("/"):
        return ""
    marker_bound = product_root_from_hint(text, allow_one_missing_marker=True)
    if marker_bound is not None:
        return str(marker_bound)
    candidate = Path(os.path.abspath(text))
    try:
        installer_index = candidate.parts.index("Installer")
    except ValueError:
        return str(candidate)
    if installer_index <= 1:
        return ""
    return str(Path(*candidate.parts[:installer_index]))


def _safe_root_file(path: Path, *, require_root_read_execute: bool = False) -> bool:
    try:
        metadata = os.lstat(path)
        mode = stat.S_IMODE(metadata.st_mode)
        if (
            stat.S_ISLNK(metadata.st_mode)
            or not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_uid != 0
            or mode & 0o022
            or (require_root_read_execute and mode & 0o500 != 0o500)
        ):
            return False
        current = path.parent
        while current != current.parent:
            parent = os.lstat(current)
            if (
                stat.S_ISLNK(parent.st_mode)
                or not stat.S_ISDIR(parent.st_mode)
                or parent.st_uid != 0
                or stat.S_IMODE(parent.st_mode) & 0o022
            ):
                return False
            current = current.parent
    except OSError:
        return False
    return True


def _decode_launcher_literal(raw: str) -> str | None:
    try:
        tokens = shlex.split(raw, comments=False, posix=True)
    except ValueError:
        return None
    if len(tokens) != 1:
        return None
    decoded = tokens[0]
    canonical = "'" + decoded.replace("'", "'\"'\"'") + "'"
    if raw == canonical:
        return decoded
    legacy = re.fullmatch(r'"([A-Za-z0-9._/-]+)"', raw)
    return legacy.group(1) if legacy else None


def _unit_properties(name: str) -> dict[str, str] | None:
    try:
        result = subprocess.run(
            [
                "/usr/bin/systemctl",
                "show",
                name,
                "--no-pager",
                "--property=LoadState",
                "--property=ActiveState",
                "--property=FragmentPath",
                "--property=WorkingDirectory",
                "--property=User",
                "--property=ExecStart",
                "--property=MainPID",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    fields: dict[str, str] = {}
    for line in result.stdout.splitlines():
        key, separator, value = line.partition("=")
        if separator:
            fields[key] = value.strip()
    if result.returncode != 0 or fields.get("LoadState") == "not-found":
        return None
    return fields


def _decode_systemd_word(raw: object) -> str | None:
    """Dekodiert genau ein von ``systemctl show`` ausgegebenes Wort.

    Unbekannte Escape-Sequenzen werden bewusst nicht geraten. Damit können
    Pfade mit den üblichen systemd-ASCII- und UTF-8-Hex-Escapes verwendet
    werden, ohne aus beliebigem Unit-Text einen Shell-Ausdruck zu machen.
    """

    value = str(raw or "").strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
        value = value[1:-1]
    result: list[str] = []
    index = 0
    while index < len(value):
        if value[index] != "\\":
            result.append(value[index])
            index += 1
            continue
        if index + 1 >= len(value):
            return None
        escape = value[index + 1]
        if escape == "x":
            encoded = bytearray()
            while (
                index + 3 < len(value)
                and value[index : index + 2] == "\\x"
                and re.fullmatch(r"[0-9A-Fa-f]{2}", value[index + 2 : index + 4])
            ):
                encoded.append(int(value[index + 2 : index + 4], 16))
                index += 4
            if not encoded:
                return None
            try:
                result.append(encoded.decode("utf-8"))
            except UnicodeDecodeError:
                return None
            continue
        replacements = {"s": " ", "\\": "\\", '"': '"', "'": "'"}
        if escape not in replacements:
            return None
        result.append(replacements[escape])
        index += 2
    decoded = "".join(result)
    if _has_control_characters(decoded):
        return None
    return decoded


def _absolute_exec_start_hints(raw: object) -> tuple[str, ...]:
    """Liefert ausschließlich literale absolute Pfade aus ExecStart."""

    payload = str(raw or "")
    if not payload or _has_control_characters(payload):
        return ()
    lexer = shlex.shlex(
        payload.replace("{", " ").replace("}", " ").replace(";", " "),
        posix=False,
    )
    lexer.whitespace_split = True
    lexer.commenters = ""
    hints: set[str] = set()
    try:
        tokens = list(lexer)
    except ValueError:
        return ()
    for token in tokens:
        for prefix in ("path=", "argv[]=", "argv="):
            if token.startswith(prefix):
                token = token[len(prefix) :]
                break
        decoded = _decode_systemd_word(token)
        if decoded is not None and decoded.startswith("/"):
            hints.add(decoded)
    return tuple(sorted(hints))


def _product_root_from_process_hint(raw: object) -> Path | None:
    """Bindet einen belegbaren Prozesspfad an einen existierenden Produktroot."""

    decoded = _decode_systemd_word(raw)
    if decoded is None or not decoded.startswith("/"):
        return None
    lexical = Path(os.path.abspath(decoded))
    parts = lexical.parts
    try:
        installer_index = parts.index("Installer")
    except ValueError:
        installer_index = -1
    if installer_index > 1:
        return canonical_product_root(
            Path(*parts[:installer_index]),
            ignore_product_markers=True,
        )
    if lexical.name in {"installer_main.py", "e3dc-bootstrap", "e3dc-update-bootstrap"}:
        return canonical_product_root(
            lexical.parent,
            ignore_product_markers=True,
        )
    return product_root_from_hint(lexical, allow_one_missing_marker=True)


def _read_proc_regular(path: Path, maximum_bytes: int) -> bytes:
    """Liest eine kleine proc-Datei ohne Symlink-Following und mit Inodebindung."""

    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if not stat.S_ISREG(before.st_mode):
            raise OSError("proc-Eintrag ist keine reguläre Datei")
        raw = bytearray()
        while len(raw) <= maximum_bytes:
            chunk = os.read(descriptor, min(8192, maximum_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named_after = os.stat(path, follow_symlinks=False)
        stable_before = (before.st_dev, before.st_ino, before.st_uid, before.st_mode)
        stable_after = (after.st_dev, after.st_ino, after.st_uid, after.st_mode)
        stable_named = (
            named_after.st_dev,
            named_after.st_ino,
            named_after.st_uid,
            named_after.st_mode,
        )
        if len(raw) > maximum_bytes or stable_before != stable_after or stable_after != stable_named:
            raise OSError("proc-Eintrag wechselte während des Lesens")
        return bytes(raw)
    finally:
        os.close(descriptor)


def _proc_start_time(raw: bytes) -> str:
    """Extrahiert Linux ``/proc/<pid>/stat`` Feld 22 ohne den comm-Inhalt zu raten."""

    try:
        text = raw.decode("utf-8", errors="strict").strip()
    except UnicodeError as exc:
        raise OSError("proc-Status ist nicht UTF-8") from exc
    closing = text.rfind(")")
    if closing < 2:
        raise OSError("proc-Status ist unvollständig")
    fields = text[closing + 1 :].strip().split()
    if len(fields) <= 19 or not fields[19].isdigit():
        raise OSError("proc-Startzeit fehlt")
    return fields[19]


def _stable_main_pid_hints(
    unit_name: str,
    fields: dict[str, str],
) -> tuple[tuple[str, str], ...]:
    """Liest cwd/cmdline nur für die stabil an die aktive Unit gebundene MainPID."""

    if fields.get("ActiveState") not in ACTIVE_UNIT_STATES:
        return ()
    raw_pid = str(fields.get("MainPID", "")).strip()
    if not raw_pid.isdigit() or int(raw_pid) <= 1:
        return ()
    pid = int(raw_pid)
    proc_root = Path("/proc") / str(pid)
    stat_path = proc_root / "stat"
    cmdline_path = proc_root / "cmdline"
    cwd_path = proc_root / "cwd"
    try:
        start_before = _proc_start_time(_read_proc_regular(stat_path, 64 * 1024))
        cmdline_before = _read_proc_regular(cmdline_path, 1024 * 1024)
        cwd_before = os.lstat(cwd_path)
        if not stat.S_ISLNK(cwd_before.st_mode):
            return ()
        cwd_target_before = os.readlink(cwd_path)
        cwd_after = os.lstat(cwd_path)
        if not stat.S_ISLNK(cwd_after.st_mode):
            return ()
        cwd_target_after = os.readlink(cwd_path)
        cmdline_after = _read_proc_regular(cmdline_path, 1024 * 1024)
        start_after = _proc_start_time(_read_proc_regular(stat_path, 64 * 1024))
    except OSError:
        return ()
    cwd_token_before = (
        cwd_before.st_dev,
        cwd_before.st_ino,
        cwd_before.st_uid,
        cwd_before.st_mode,
    )
    cwd_token_after = (
        cwd_after.st_dev,
        cwd_after.st_ino,
        cwd_after.st_uid,
        cwd_after.st_mode,
    )
    if (
        start_before != start_after
        or cmdline_before != cmdline_after
        or cwd_token_before != cwd_token_after
        or cwd_target_before != cwd_target_after
    ):
        return ()
    rebound = _unit_properties(unit_name)
    if (
        rebound is None
        or rebound.get("ActiveState") not in ACTIVE_UNIT_STATES
        or str(rebound.get("MainPID", "")).strip() != raw_pid
    ):
        return ()
    hints: set[tuple[str, str]] = set()
    if cwd_target_before.startswith("/") and not _has_control_characters(cwd_target_before):
        hints.add((cwd_target_before, "cwd"))
    for raw_argument in cmdline_before.split(b"\x00"):
        if not raw_argument:
            continue
        try:
            argument = raw_argument.decode("utf-8", errors="strict")
        except UnicodeError:
            continue
        if argument.startswith("/") and not _has_control_characters(argument):
            hints.add((argument, "cmdline"))
    return tuple(sorted(hints))


def _collect_installation_evidence(
) -> tuple[list[dict[str, set[str]]], list[dict[str, set[str]]]]:
    tiers: list[dict[str, set[str]]] = [defaultdict(set) for _ in range(5)]
    tier_users: list[dict[str, set[str]]] = [defaultdict(set) for _ in range(5)]
    live_active_users: dict[str, set[str]] = defaultdict(set)
    other_active_users: dict[str, set[str]] = defaultdict(set)

    def add(
        raw: object,
        source: str,
        user: object = None,
        *,
        tier: int,
        allow_one_missing_marker: bool = False,
        allow_all_missing_markers: bool = False,
        ignore_product_markers: bool = False,
        exact_root: bool = False,
    ) -> str | None:
        if exact_root:
            root = canonical_product_root(
                raw,
                allow_one_missing_marker=allow_one_missing_marker,
                allow_all_missing_markers=allow_all_missing_markers,
                ignore_product_markers=ignore_product_markers,
            )
        else:
            root = product_root_from_hint(
                raw,
                allow_one_missing_marker=allow_one_missing_marker,
                allow_all_missing_markers=allow_all_missing_markers,
                ignore_product_markers=ignore_product_markers,
            )
        if root is None:
            return None
        key = str(root)
        tiers[tier][key].add(source)
        selected_user = valid_user(user)
        if selected_user:
            tier_users[tier][key].add(selected_user)
        return key

    launcher = Path("/usr/local/sbin/e3dc-web-update-launcher")
    if _safe_root_file(launcher, require_root_read_execute=True):
        try:
            _metadata, raw_payload = _stable_regular_read(launcher, 131072)
            payload = raw_payload.decode("utf-8", errors="strict")
        except (OSError, UnicodeError):
            payload = ""
        if len(payload) <= 131072:
            root_matches = re.findall(r"^readonly INSTALL_ROOT=(.+)$", payload, re.MULTILINE)
            user_matches = re.findall(r"^readonly INSTALL_USER=(.+)$", payload, re.MULTILINE)
            if len(root_matches) == 1 and len(user_matches) == 1:
                bound_root = _decode_launcher_literal(root_matches[0])
                bound_user = _decode_launcher_literal(user_matches[0])
                if bound_root is not None and bound_user is not None:
                    add(
                        bound_root,
                        "root-eigener Update-Dispatcher",
                        bound_user,
                        tier=1,
                        ignore_product_markers=True,
                        exact_root=True,
                    )

    try:
        anchor_paths = sorted(INSTANCE_ANCHOR_DIRECTORY.glob("*.json"))[:128]
    except OSError:
        anchor_paths = []
    for anchor_path in anchor_paths:
        if _has_control_characters(str(anchor_path)) or not _safe_root_file(anchor_path):
            continue
        try:
            metadata, payload = _stable_regular_read(anchor_path, 64 * 1024)
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                continue
            value = json.loads(payload.decode("utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(value, dict) or value.get("schema") != INSTANCE_ANCHOR_SCHEMA:
            continue
        add(
            value.get("install_root"),
            f"root-eigener Instanzanker:{anchor_path}",
            value.get("install_user"),
            tier=1,
            ignore_product_markers=True,
            exact_root=True,
        )

    try:
        listed = subprocess.run(
            [
                "/usr/bin/systemctl",
                "list-unit-files",
                "--type=service",
                "--no-legend",
                "--no-pager",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        listed = None
    unit_names: list[str] = []
    if listed is not None and listed.returncode == 0:
        for line in listed.stdout.splitlines():
            fields = line.split()
            if not fields:
                continue
            name = fields[0]
            if (
                re.fullmatch(r"e3dc[A-Za-z0-9_.@-]*\.service", name)
                or name in {"energy_manager.service", "luxtronik.service"}
            ):
                unit_names.append(name)
    for name in sorted(set(unit_names))[:128]:
        fields = _unit_properties(name)
        if fields is None:
            continue
        fragment_text = fields.get("FragmentPath", "")
        fragment = Path(fragment_text) if fragment_text.startswith("/") else None
        active = fields.get("ActiveState") in ACTIVE_UNIT_STATES
        if not active and (fragment is None or not _safe_root_file(fragment)):
            continue
        if not active:
            add(
                fields.get("WorkingDirectory", "").lstrip("-"),
                f"systemd:{name}:WorkingDirectory",
                fields.get("User", ""),
                tier=2,
            )
            continue

        active_roots: set[str] = set()
        working_root = _unit_working_directory_root(fields.get("WorkingDirectory", ""))
        working_key = add(
            working_root,
            f"systemd:{name}:WorkingDirectory",
            tier=0,
            allow_one_missing_marker=True,
            ignore_product_markers=True,
            exact_root=True,
        )
        if working_key:
            active_roots.add(working_key)

        for hint in _absolute_exec_start_hints(fields.get("ExecStart", "")):
            root = _product_root_from_process_hint(hint)
            key = add(
                root,
                f"systemd:{name}:ExecStart",
                tier=0,
                ignore_product_markers=True,
                exact_root=True,
            )
            if key:
                active_roots.add(key)

        for hint, origin in _stable_main_pid_hints(name, fields):
            root = _product_root_from_process_hint(hint)
            key = add(
                root,
                f"systemd:{name}:MainPID:{origin}",
                tier=0,
                ignore_product_markers=True,
                exact_root=True,
            )
            if key:
                active_roots.add(key)

        selected_user = valid_user(fields.get("User", ""))
        if selected_user:
            user_map = (
                live_active_users
                if name == "e3dc-live.service" or name.startswith("e3dc-live@")
                else other_active_users
            )
            for root in active_roots:
                user_map[root].add(selected_user)

    # Der Kernprozess e3dc-live ist die primäre Benutzerautorität. Ein
    # optionaler Dienst mit historisch falschem User darf dieselbe Installation
    # nicht mehrdeutig machen; sein anderer Produktroot bleibt aber weiterhin
    # ein eigener aktiver Installationskandidat.
    for root in tiers[0]:
        authoritative = live_active_users.get(root) or other_active_users.get(root)
        if authoritative:
            tier_users[0][root].update(authoritative)

    for metadata_path, tier in (
        (Path("/var/www/html/data/e3dc_v4.json"), 3),
        (Path("/var/www/html/e3dc_paths.json"), 4),
    ):
        try:
            metadata = os.lstat(metadata_path)
            if (
                stat.S_ISLNK(metadata.st_mode)
                or not stat.S_ISREG(metadata.st_mode)
                or metadata.st_nlink != 1
                or not 2 <= metadata.st_size <= 1024 * 1024
            ):
                continue
            raw = json.loads(metadata_path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(raw, dict):
            continue
        nested = raw.get("config")
        if isinstance(nested, dict):
            raw = {**nested, **raw}
        root = canonical_product_root(raw.get("install_path"))
        if root is None:
            continue
        declared_user = valid_user(raw.get("install_user") or raw.get("user"))
        try:
            owner_user = valid_user(pwd.getpwuid(os.lstat(root).st_uid).pw_name)
        except (KeyError, OSError):
            owner_user = None
        selected_user = declared_user or owner_user
        if selected_user is None:
            continue
        add(root, f"Pfadmetadaten:{metadata_path}", selected_user, tier=tier)

    return tiers, tier_users


def list_installation_candidates() -> tuple[InstallationCandidate, ...]:
    tiers, _tier_users = _collect_installation_evidence()
    selected_tier = next((index for index, values in enumerate(tiers) if values), None)
    if selected_tier is None:
        return ()
    return tuple(
        InstallationCandidate(
            root=root,
            tier=selected_tier,
            sources=tuple(sorted(tiers[selected_tier][root])),
        )
        for root in sorted(tiers[selected_tier])
    )


def _resolve_install_user(
    *,
    selected: Path,
    selected_key: str,
    tier_users: list[dict[str, set[str]]],
    explicit_user: str,
    sudo_user: str,
) -> str:
    install_user = valid_user(explicit_user)
    if install_user is None:
        for level in tier_users:
            trusted_users = sorted(level.get(selected_key, set()))
            if not trusted_users:
                continue
            if len(trusted_users) > 1:
                raise DiscoveryError(
                    "E3DC-UPD-PATH-006",
                    f"Installationsbenutzer ist mehrdeutig für {selected_key}: "
                    + ", ".join(trusted_users),
                    "Starte den Bootstrap erneut mit dem eindeutigen Installationspfad; "
                    "bereinige danach den widersprüchlichen Dienst- oder Instanzanker.",
                )
            install_user = trusted_users[0]
            break
    if install_user is None:
        install_user = valid_user(sudo_user)
    if install_user is None:
        try:
            install_user = valid_user(pwd.getpwuid(os.lstat(selected).st_uid).pw_name)
        except (KeyError, OSError):
            install_user = None
    if install_user is None:
        raise DiscoveryError(
            "E3DC-UPD-PATH-007",
            f"Installationsbenutzer ist nicht bestimmbar für {selected_key}",
            "Ergänze den Installationsbenutzer im root-eigenen Update-Dispatcher "
            "oder Instanzanker und starte den Bootstrap anschließend erneut.",
        )
    return install_user


def discover_installation(
    *,
    explicit_target: str = "",
    explicit_user: str = "",
    sudo_user: str = "",
) -> tuple[Path, str]:
    if _has_control_characters(explicit_target):
        raise DiscoveryError(
            "E3DC-UPD-PATH-001",
            "Expliziter Installationspfad enthält unzulässige Steuerzeichen",
            "Verwende einen absoluten lokalen Verzeichnispfad ohne Steuerzeichen.",
        )
    explicit = str(explicit_target or "").strip()
    tiers, tier_users = _collect_installation_evidence()

    if explicit:
        marker_bound = canonical_product_root(explicit)
        selected = marker_bound or canonical_product_root(
            explicit,
            ignore_product_markers=True,
        )
        if selected is None:
            raise DiscoveryError(
                "E3DC-UPD-PATH-002",
                f"Expliziter Installationspfad ist ungültig: {explicit}",
                "Prüfe, ob das Verzeichnis existiert und als absoluter lokaler Pfad "
                "aufgelöst werden kann.",
            )
        selected_key = str(selected)
        # Eine explizite Konsolenwahl ist selbst die Autorität. Fehlende
        # Produktmarker und falsche Altberechtigungen sind gerade der
        # Reparaturauftrag des Ziel-Updaters, nicht nochmals ein Startveto.
    else:
        selected_tier = next((index for index, values in enumerate(tiers) if values), None)
        candidates = sorted(tiers[selected_tier]) if selected_tier is not None else []
        if not candidates:
            raise DiscoveryError(
                "E3DC-UPD-PATH-004",
                "Keine Installation aus aktiven Diensten, Root-Dispatcher, Instanzankern, "
                "geladenen Diensten oder sicheren Web-Pfadmetadaten erkannt.",
                "Starte einen vorhandenen E3DC-Dienst oder rufe den Bootstrap mit dem "
                "absoluten Installationspfad auf.",
            )
        if len(candidates) != 1:
            details = ["Automatische Installationserkennung ist mehrdeutig:"]
            for candidate in candidates:
                sources = ", ".join(sorted(tiers[selected_tier][candidate]))
                details.append(f"  - {candidate} ({sources})")
            details.append(
                "Kein Pfad wurde geraten; jede Installation muss getrennt aktualisiert werden."
            )
            raise DiscoveryError(
                "E3DC-UPD-PATH-005",
                "\n".join(details),
                "Wähle im Terminal eine Instanz aus oder starte den ausgegebenen "
                "Hintergrundbefehl für genau einen Installationspfad.",
            )
        selected_key = candidates[0]
        selected = Path(selected_key)

    install_user = _resolve_install_user(
        selected=selected,
        selected_key=selected_key,
        tier_users=tier_users,
        explicit_user=explicit_user,
        sudo_user=sudo_user,
    )
    return selected, install_user


def _metadata_token(metadata: os.stat_result) -> tuple[int, ...]:
    return (
        metadata.st_dev,
        metadata.st_ino,
        metadata.st_size,
        metadata.st_mtime_ns,
        metadata.st_ctime_ns,
        metadata.st_uid,
        metadata.st_gid,
        stat.S_IMODE(metadata.st_mode),
        metadata.st_nlink,
    )


def _stable_regular_read(path: Path, maximum_bytes: int) -> tuple[os.stat_result, bytes]:
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        before = os.fstat(descriptor)
        if (
            not stat.S_ISREG(before.st_mode)
            or before.st_nlink != 1
            or before.st_size > maximum_bytes
        ):
            raise OSError("Datei besitzt unzulässige Metadaten")
        raw = bytearray()
        while len(raw) <= maximum_bytes:
            chunk = os.read(descriptor, min(8192, maximum_bytes + 1 - len(raw)))
            if not chunk:
                break
            raw.extend(chunk)
        after = os.fstat(descriptor)
        named_after = os.stat(path, follow_symlinks=False)
        if (
            len(raw) > maximum_bytes
            or _metadata_token(before) != _metadata_token(after)
            or _metadata_token(after) != _metadata_token(named_after)
        ):
            raise OSError("Datei wurde während des Lesens verändert")
        return after, bytes(raw)
    finally:
        os.close(descriptor)


def _configured_signal(value: object) -> bool:
    return str(value or "").strip().lower() not in {
        "",
        "0",
        "0.0.0.0",
        "false",
        "none",
        "null",
        "off",
    }


def _active_role_services() -> frozenset[str]:
    active: set[str] = set()
    for unit in ("e3dc-ha.service", "e3dc-shadow-sync.service"):
        fields = _unit_properties(unit)
        if fields is not None and fields.get("ActiveState") in ACTIVE_UNIT_STATES:
            active.add(unit)
    return frozenset(active)


def _validate_anchor_role_against_services(
    role: str,
    active_services: frozenset[str],
) -> None:
    ha_active = "e3dc-ha.service" in active_services
    shadow_active = "e3dc-shadow-sync.service" in active_services
    if ha_active and shadow_active:
        raise RuntimeError(
            "e3dc-ha.service und e3dc-shadow-sync.service sind gleichzeitig aktiv; "
            "die Instanzrolle ist widersprüchlich"
        )
    if ha_active and role not in {"master", "slave"}:
        raise RuntimeError(
            f"Rollenanker mode={role} widerspricht dem aktiven e3dc-ha.service; "
            "für diesen Dienst muss mode master oder slave eindeutig im Rollenanker stehen"
        )
    if shadow_active and role != "shadow":
        raise RuntimeError(
            f"Rollenanker mode={role} widerspricht dem aktiven "
            "e3dc-shadow-sync.service; dafür muss mode shadow im Rollenanker stehen"
        )


def discover_role(target: Path) -> str:
    active_role_services = _active_role_services()
    if {
        "e3dc-ha.service",
        "e3dc-shadow-sync.service",
    }.issubset(active_role_services):
        _validate_anchor_role_against_services("off", active_role_services)

    anchor = Path("/etc/e3dc-control/instance_role.json")
    try:
        anchor_entry = os.lstat(anchor)
    except FileNotFoundError:
        anchor_entry = None
    except OSError as exc:
        raise RuntimeError(f"Root-eigener Rollenanker ist nicht prüfbar: {exc}") from exc
    if anchor_entry is not None:
        try:
            metadata, raw = _stable_regular_read(anchor, 64 * 1024)
            if metadata.st_uid != 0 or stat.S_IMODE(metadata.st_mode) & 0o022:
                raise RuntimeError("Rollenanker ist nicht root-eigen und schreibgeschützt")
            parent = anchor.parent
            while parent != parent.parent:
                parent_metadata = parent.lstat()
                if (
                    parent.is_symlink()
                    or not stat.S_ISDIR(parent_metadata.st_mode)
                    or parent_metadata.st_uid != 0
                    or stat.S_IMODE(parent_metadata.st_mode) & 0o022
                ):
                    raise RuntimeError(f"Rollenanker-Elternpfad ist nicht sicher: {parent}")
                parent = parent.parent
            value = json.loads(raw.decode("utf-8"))
            role = str(value.get("mode") if isinstance(value, dict) else "").strip().lower()
            if role not in VALID_ROLES:
                raise RuntimeError("Rollenanker besitzt keine gültige Rolle")
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError):
            # Ein beschädigter oder falsch berechtigter Altanker ist bei einem
            # ausdrücklich gestarteten Update reparierbarer Bestand. Die Rolle
            # wird dann aus laufendem Dienst und Nutzerkonfiguration ermittelt;
            # echte HA-Mehrdeutigkeit bleibt weiter gesperrt.
            pass
        else:
            _validate_anchor_role_against_services(role, active_role_services)
            return role

    fallback_roles: list[tuple[str, str]] = []
    invalid_sources: list[str] = []
    ha_indicators: list[str] = []
    ha_peer_ips: list[tuple[str, str]] = []

    def collect_ha_peer(source: str, raw: object) -> None:
        value = str(raw or "").strip()
        if not value:
            return
        try:
            normalized = str(ipaddress.ip_address(value))
        except ValueError:
            invalid_sources.append(f"{source}:ha_peer_ip ungültig")
            return
        ha_peer_ips.append((source, normalized))

    def collect_json_role(path: Path) -> None:
        try:
            metadata, raw = _stable_regular_read(path, 1024 * 1024)
            if metadata.st_size < 2:
                invalid_sources.append(f"{path}:leer")
                return
            value = json.loads(raw.decode("utf-8"))
        except FileNotFoundError:
            return
        except (OSError, UnicodeError, json.JSONDecodeError):
            invalid_sources.append(f"{path}:ungültig")
            return
        if not isinstance(value, dict):
            invalid_sources.append(f"{path}:kein Objekt")
            return
        nested = value.get("config")
        if isinstance(nested, dict):
            value = {**nested, **value}
        raw_role = str(value.get("ha_mode") or "").strip().lower()
        if raw_role:
            if raw_role in VALID_ROLES:
                fallback_roles.append((str(path), raw_role))
            else:
                invalid_sources.append(f"{path}:ha_mode ungültig")
        for key in ("ha_peer_ip", "shadow_master_url", "shadow_master_ip"):
            if _configured_signal(value.get(key)):
                ha_indicators.append(f"{path}:{key}")
                if key == "ha_peer_ip":
                    collect_ha_peer(str(path), value.get(key))

    for path in (
        Path("/var/www/html/data/e3dc_v4.json"),
        target / "data/e3dc_v4.json",
        target / "Installer/installer_config.json",
    ):
        collect_json_role(path)
    for path in (target / "e3dc.config.txt", target / "data/e3dc.config.txt"):
        try:
            metadata, raw = _stable_regular_read(path, 1024 * 1024)
            if metadata.st_size < 1:
                invalid_sources.append(f"{path}:leer")
                continue
            text = raw.decode("utf-8-sig")
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError):
            invalid_sources.append(f"{path}:ungültig")
            continue
        for line in text.splitlines():
            role_match = re.fullmatch(r"\s*ha_mode\s*=\s*([^#;\s]+)\s*", line)
            if role_match:
                role = role_match.group(1).strip().lower()
                if role in VALID_ROLES:
                    fallback_roles.append((str(path), role))
                else:
                    invalid_sources.append(f"{path}:ha_mode ungültig")
                continue
            peer_match = re.fullmatch(
                r"\s*(ha_peer_ip|shadow_master_url|shadow_master_ip)\s*=\s*(.*?)\s*",
                line,
            )
            if peer_match and _configured_signal(peer_match.group(2)):
                ha_indicators.append(f"{path}:{peer_match.group(1)}")
                if peer_match.group(1) == "ha_peer_ip":
                    collect_ha_peer(str(path), peer_match.group(2))

    if "e3dc-shadow-sync.service" in active_role_services:
        fallback_roles.append(("systemd:e3dc-shadow-sync.service", "shadow"))

    unique_roles = sorted({role for _source, role in fallback_roles})
    if not unique_roles:
        if "e3dc-ha.service" in active_role_services:
            raise RuntimeError(
                "e3dc-ha.service ist aktiv, aber weder ein gültiger Rollenanker noch "
                "eine eindeutige alte ha_mode-Konfiguration bindet master oder slave"
            )
        if ha_indicators:
            raise RuntimeError(
                "Rolle ist wegen vorhandener HA-/Konfigurationsindizien nicht als off bindbar: "
                + "; ".join(sorted(set(ha_indicators)))
            )
        # Ungültige Alt-Konfigurationsdateien und ein reparierbarer Altanker
        # erzeugen ohne jedes HA-Signal keine zweite Instanzrolle.
        return "off"
    if len(unique_roles) != 1:
        details = "; ".join(
            f"{source}={role}" for source, role in sorted(set(fallback_roles))
        )
        raise RuntimeError(f"Fallback-Rollenquellen widersprechen sich: {details}")
    if "e3dc-ha.service" in active_role_services:
        if unique_roles[0] not in {"master", "slave"}:
            raise RuntimeError(
                f"Aktives e3dc-ha.service widerspricht der eindeutigen Altrolle {unique_roles[0]}"
            )
        unique_peer_ips = sorted({peer for _source, peer in ha_peer_ips})
        if len(unique_peer_ips) != 1:
            details = "; ".join(
                f"{source}={peer}" for source, peer in sorted(set(ha_peer_ips))
            ) or "keine gültige ha_peer_ip"
            raise RuntimeError(
                "Aktives e3dc-ha.service benötigt genau eine gültige alte ha_peer_ip: "
                + details
            )
    if unique_roles[0] == "off" and ha_indicators:
        raise RuntimeError(
            "Rolle off widerspricht vorhandenen HA-Indizien: "
            + "; ".join(sorted(set(ha_indicators)))
        )
    return unique_roles[0]


def bind(
    *,
    explicit_target: str = "",
    explicit_user: str = "",
    sudo_user: str = "",
    requested_role: str = "auto",
) -> Binding:
    root, user = discover_installation(
        explicit_target=explicit_target,
        explicit_user=explicit_user,
        sudo_user=sudo_user,
    )
    role = discover_role(root)
    if requested_role not in {*VALID_ROLES, "auto"}:
        raise RuntimeError("Rolle muss auto, off, master, slave oder shadow sein")
    if requested_role != "auto" and requested_role != role:
        raise RuntimeError(
            f"Explizite Rolle {requested_role} widerspricht der erkannten Rolle {role}"
        )
    return Binding(str(root), user, role)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--list-candidates", action="store_true")
    parser.add_argument("--explicit-root", default="")
    parser.add_argument("--explicit-user", default="")
    parser.add_argument("--sudo-user", default="")
    parser.add_argument("--requested-role", default="auto")
    args = parser.parse_args(argv)
    try:
        if args.list_candidates:
            if any(
                (
                    args.explicit_root,
                    args.explicit_user,
                    args.sudo_user,
                    args.requested_role != "auto",
                )
            ):
                raise DiscoveryError(
                    "E3DC-UPD-PATH-008",
                    "Kandidatenliste und explizite Bindungsparameter wurden vermischt",
                    "Rufe --list-candidates ohne weitere Bindungsparameter auf.",
                )
            for candidate in list_installation_candidates():
                print(f"{candidate.root}\t{', '.join(candidate.sources)}")
            return 0
        binding = bind(
            explicit_target=args.explicit_root,
            explicit_user=args.explicit_user,
            sudo_user=args.sudo_user,
            requested_role=args.requested_role,
        )
    except DiscoveryError as exc:
        print(f"[ABBRUCH] {exc.code}", file=sys.stderr)
        print(f"Was ist passiert: {exc}", file=sys.stderr)
        print(f"Lösung: {exc.solution}", file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print("[ABBRUCH] E3DC-UPD-BIND-099", file=sys.stderr)
        print(f"Was ist passiert: {exc}", file=sys.stderr)
        print(
            "Lösung: Prüfe die genannte Rollen- oder Konfigurationsquelle und starte "
            "denselben Updatebefehl anschließend erneut.",
            file=sys.stderr,
        )
        return 1
    print(f"{binding.root}\t{binding.user}\t{binding.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
