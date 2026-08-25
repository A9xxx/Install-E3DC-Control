#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Git-unabhängiger Vergleich der installierten mit der Stable-Version."""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from pathlib import Path


LATEST_RELEASE_URL = "https://github.com/A9xxx/Install-E3DC-Control/releases/latest"
RELEASE_PREFIX = "https://github.com/A9xxx/Install-E3DC-Control/releases/tag/"
VERSION_PATTERN = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)([A-Za-z0-9._-]*)$")


def normalize_release_version(value: object) -> str:
    text = str(value or "").strip()
    match = VERSION_PATTERN.fullmatch(text)
    if match is None:
        raise ValueError(f"Ungültige Release-Version: {text or '<leer>'}")
    return f"{match.group(1)}.{match.group(2)}.{match.group(3)}{match.group(4)}"


def release_version_key(value: object) -> tuple[int, int, int, int, tuple[tuple[int, object], ...]]:
    normalized = normalize_release_version(value)
    match = VERSION_PATTERN.fullmatch(normalized)
    assert match is not None
    suffix = match.group(4).lower().strip("._-")
    suffix_parts: list[tuple[int, object]] = []
    for part in re.findall(r"\d+|[^\d]+", suffix):
        suffix_parts.append((0, int(part)) if part.isdigit() else (1, part))
    return (
        int(match.group(1)),
        int(match.group(2)),
        int(match.group(3)),
        0 if suffix == "" else 1,
        tuple(suffix_parts),
    )


def read_installed_version(install_root: Path) -> str:
    for path in (Path(install_root) / "VERSION", Path("/var/www/html/VERSION")):
        try:
            raw = path.read_text(encoding="utf-8").strip()
            return normalize_release_version(raw)
        except FileNotFoundError:
            continue
        except (OSError, UnicodeError, ValueError):
            continue
    raise RuntimeError("Installierte VERSION ist weder im Produktordner noch im Webroot lesbar.")


def resolve_latest_stable_tag(*, timeout: float = 20.0, opener=None) -> str:
    request = urllib.request.Request(
        LATEST_RELEASE_URL,
        headers={"User-Agent": "E3DC-Control-Updater/1", "Accept": "text/html"},
        method="GET",
    )
    open_url = opener or urllib.request.urlopen
    try:
        with open_url(request, timeout=timeout) as response:
            effective_url = str(response.geturl())
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(f"GitHub Stable-Release ist nicht erreichbar: {exc}") from exc
    if not effective_url.startswith(RELEASE_PREFIX):
        raise RuntimeError("GitHub lieferte keinen eindeutigen Stable-Release-Tag.")
    tag = effective_url[len(RELEASE_PREFIX) :].strip("/")
    normalize_release_version(tag)
    return tag if tag.startswith("v") else f"v{tag}"


def stable_update_check(install_root: Path, *, opener=None) -> dict[str, object]:
    try:
        current = read_installed_version(Path(install_root))
        target_tag = resolve_latest_stable_tag(opener=opener)
        target = normalize_release_version(target_tag)
    except Exception as exc:
        return {"success": False, "missing": 0, "error": str(exc)}

    current_key = release_version_key(current)
    target_key = release_version_key(target)
    return {
        "success": True,
        "missing": 1 if current_key < target_key else 0,
        "missing_exact": True,
        "same_release": current_key == target_key,
        "ahead": current_key > target_key,
        "current_version": current,
        "target_version": target,
        "target_tag": target_tag,
        "upstream": "github_latest_stable_release",
    }
