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
CONTROL_CHARACTERS = ("\x00", "\t", "\r", "\n")
PRODUCT_MARKERS = (
    ("VERSION", "file"),
    ("installer_main.py", "file"),
    ("Installer", "directory"),
)


@dataclass(frozen=True)
class Binding:
    root: str
    user: str
    role: str


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
) -> Path | None:
    text = str(raw or "")
    if _has_control_characters(text):
        return None
    text = text.strip()
    if not text.startswith("/"):
        return None
    candidate = Path(os.path.abspath(text))
    current = Path(candidate.anchor)
    try:
        for component in candidate.parts[1:]:
            current /= component
            metadata = os.lstat(current)
            if stat.S_ISLNK(metadata.st_mode):
                return None
        if not stat.S_ISDIR(os.lstat(candidate).st_mode):
            return None
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
                return None
        if missing_markers > (1 if allow_one_missing_marker else 0):
            return None
    except OSError:
        return None
    return candidate


def product_root_from_hint(
    raw: object,
    *,
    allow_one_missing_marker: bool = False,
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
        )
        if root is not None:
            return root
        if current == current.parent:
            break
        current = current.parent
    return None


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


def discover_installation(
    *,
    explicit_target: str = "",
    explicit_user: str = "",
    sudo_user: str = "",
) -> tuple[Path, str]:
    if _has_control_characters(explicit_target):
        raise RuntimeError("Expliziter Installationspfad enthält unzulässige Steuerzeichen")
    explicit = str(explicit_target or "").strip()
    tiers: list[dict[str, set[str]]] = [defaultdict(set) for _ in range(5)]
    tier_users: list[dict[str, set[str]]] = [defaultdict(set) for _ in range(5)]

    def add(
        raw: object,
        source: str,
        user: object = None,
        *,
        tier: int,
        allow_one_missing_marker: bool = False,
    ) -> None:
        root = product_root_from_hint(
            raw,
            allow_one_missing_marker=allow_one_missing_marker,
        )
        if root is None:
            return
        key = str(root)
        tiers[tier][key].add(source)
        selected_user = valid_user(user)
        if selected_user:
            tier_users[tier][key].add(selected_user)

    launcher = Path("/usr/local/sbin/e3dc-web-update-launcher")
    if _safe_root_file(launcher, require_root_read_execute=True):
        try:
            payload = launcher.read_text(encoding="utf-8", errors="strict")
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
                        allow_one_missing_marker=True,
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
        if fragment is None or not _safe_root_file(fragment):
            continue
        active = fields.get("ActiveState") == "active"
        add(
            fields.get("WorkingDirectory", "").lstrip("-"),
            f"systemd:{name}:WorkingDirectory",
            fields.get("User", ""),
            tier=0 if active else 2,
            allow_one_missing_marker=active,
        )

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
        if selected_user is None or (
            owner_user is not None
            and declared_user is not None
            and owner_user != declared_user
        ):
            continue
        add(root, f"Pfadmetadaten:{metadata_path}", selected_user, tier=tier)

    if explicit:
        selected = canonical_product_root(explicit, allow_one_missing_marker=True)
        if selected is None:
            raise RuntimeError(f"Expliziter Installationspfad ist ungültig: {explicit}")
        selected_key = str(selected)
    else:
        selected_tier = next((index for index, values in enumerate(tiers) if values), None)
        candidates = sorted(tiers[selected_tier]) if selected_tier is not None else []
        if not candidates:
            raise RuntimeError(
                "Keine Installation aus aktiven Diensten, Root-Dispatcher, geladenen "
                "Diensten oder sicheren Web-Pfadmetadaten erkannt."
            )
        if len(candidates) != 1:
            details = ["Automatische Installationserkennung ist mehrdeutig:"]
            for candidate in candidates:
                sources = ", ".join(sorted(tiers[selected_tier][candidate]))
                details.append(f"  - {candidate} ({sources})")
            details.append(
                "Kein Pfad wurde geraten; jede Installation muss getrennt aktualisiert werden."
            )
            raise RuntimeError("\n".join(details))
        selected_key = candidates[0]
        selected = Path(selected_key)

    install_user = valid_user(explicit_user)
    if install_user is None:
        for level in tier_users:
            trusted_users = sorted(level.get(selected_key, set()))
            if not trusted_users:
                continue
            if len(trusted_users) > 1:
                raise RuntimeError(
                    f"Installationsbenutzer ist mehrdeutig für {selected_key}: "
                    + ", ".join(trusted_users)
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
        raise RuntimeError(f"Installationsbenutzer ist nicht bestimmbar für {selected_key}")
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


def discover_role(target: Path) -> str:
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
        except (OSError, UnicodeError, json.JSONDecodeError, RuntimeError) as exc:
            raise RuntimeError(f"Root-eigener Rollenanker ist ungültig: {exc}") from exc
        return role

    fallback_roles: list[tuple[str, str]] = []
    invalid_sources: list[str] = []
    ha_indicators: list[str] = []

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
        raw_role = str(value.get("ha_mode") or "").strip().lower()
        if raw_role:
            if raw_role in VALID_ROLES:
                fallback_roles.append((str(path), raw_role))
            else:
                invalid_sources.append(f"{path}:ha_mode ungültig")
        for key in ("ha_peer_ip", "shadow_master_url", "shadow_master_ip"):
            if _configured_signal(value.get(key)):
                ha_indicators.append(f"{path}:{key}")

    for path in (
        Path("/var/www/html/data/e3dc_v4.json"),
        target / "data/e3dc_v4.json",
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

    for unit in ("e3dc-ha.service", "e3dc-shadow-sync.service"):
        fields = _unit_properties(unit)
        if fields is not None and (
            fields.get("LoadState") not in {None, "", "not-found"}
            or fields.get("ActiveState") == "active"
        ):
            ha_indicators.append(f"systemd:{unit}")

    unique_roles = sorted({role for _source, role in fallback_roles})
    if not unique_roles:
        blockers = sorted(set(invalid_sources + ha_indicators))
        if blockers:
            raise RuntimeError(
                "Rolle ist wegen vorhandener HA-/Konfigurationsindizien nicht als off bindbar: "
                + "; ".join(blockers)
            )
        return "off"
    if len(unique_roles) != 1:
        details = "; ".join(
            f"{source}={role}" for source, role in sorted(set(fallback_roles))
        )
        raise RuntimeError(f"Fallback-Rollenquellen widersprechen sich: {details}")
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
    parser.add_argument("--explicit-root", default="")
    parser.add_argument("--explicit-user", default="")
    parser.add_argument("--sudo-user", default="")
    parser.add_argument("--requested-role", default="auto")
    args = parser.parse_args(argv)
    try:
        binding = bind(
            explicit_target=args.explicit_root,
            explicit_user=args.explicit_user,
            sudo_user=args.sudo_user,
            requested_role=args.requested_role,
        )
    except RuntimeError as exc:
        print(str(exc), file=sys.stderr)
        return 1
    print(f"{binding.root}\t{binding.user}\t{binding.role}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
