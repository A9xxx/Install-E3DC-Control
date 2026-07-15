"""Strict context for release bootstrap, backup, update and rollback only.

The normal v5.3.2b runtime keeps its published path semantics.  This module is
deliberately separate so the unrelated-history transition never has to guess a
user, home, installation root or virtual environment.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
import os
from pathlib import Path
import pwd
import stat
from typing import Iterable


class TransitionContextError(RuntimeError):
    """Raised when the one-shot release context is not unambiguous."""


@dataclass(frozen=True)
class TransitionContext:
    install_path: str
    install_user: str
    home_dir: str
    venv_path: str
    venv_python: str
    trusted: bool
    source: str
    container: bool
    error: str = ""


_MODULE_ROOT = Path(__file__).resolve().parent.parent
_METADATA_PATHS = (
    _MODULE_ROOT / "Installer" / "installer_config.json",
    Path("/var/www/html/data/e3dc_v4.json"),
    Path("/var/www/html/e3dc_paths.json"),
)
_MAX_METADATA_BYTES = 1024 * 1024


def _is_container() -> bool:
    return Path("/.dockerenv").is_file()


def _real_directory(value: str, label: str) -> Path:
    raw = Path(str(value or ""))
    if not raw.is_absolute():
        raise TransitionContextError(f"{label} ist nicht absolut")
    if raw.is_symlink() or not raw.is_dir():
        raise TransitionContextError(f"{label} ist kein echtes Verzeichnis")
    resolved = raw.resolve(strict=True)
    if resolved != raw:
        raise TransitionContextError(f"{label} enthält einen Symlink oder Alias")
    return resolved


def _has_product_markers(root: Path) -> bool:
    required = (
        root / "VERSION",
        root / "installer_main.py",
        root / "Installer" / "installer_config.py",
    )
    return all(path.is_file() and not path.is_symlink() for path in required)


def _read_metadata(path: Path) -> dict:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return {}
    if path.is_symlink() or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise TransitionContextError("Pfadmetadaten sind keine eindeutige reguläre Datei")
    if info.st_size < 2 or info.st_size > _MAX_METADATA_BYTES:
        raise TransitionContextError("Pfadmetadaten besitzen eine unzulässige Größe")
    if info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TransitionContextError("Pfadmetadaten sind gruppen- oder weltbeschreibbar")
    before = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        opened = os.fstat(fd)
        if before != (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns):
            raise TransitionContextError("Pfadmetadaten wurden während der Prüfung ausgetauscht")
        payload = b""
        while len(payload) <= _MAX_METADATA_BYTES:
            chunk = os.read(fd, min(65536, _MAX_METADATA_BYTES + 1 - len(payload)))
            if not chunk:
                break
            payload += chunk
        after = os.fstat(fd)
        if before != (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns):
            raise TransitionContextError("Pfadmetadaten wurden während des Lesens verändert")
    finally:
        os.close(fd)
    try:
        data = json.loads(payload.decode("utf-8-sig"))
    except Exception as exc:
        raise TransitionContextError("Pfadmetadaten sind nicht lesbar") from exc
    return data if isinstance(data, dict) else {}


def _flatten_metadata(data: dict) -> dict:
    result = dict(data or {})
    nested = result.get("config")
    if isinstance(nested, dict):
        for key, value in nested.items():
            result.setdefault(key, value)
    return result


def _unique_nonempty(values: Iterable[object]) -> list[str]:
    result: list[str] = []
    for value in values:
        text = str(value or "").strip()
        if text and text not in result:
            result.append(text)
    return result


def _untrusted(error: str) -> TransitionContext:
    return TransitionContext("", "", "", "", "", False, "blocked", _is_container(), error)


def _validate_venv_python(venv: Path, account_uid: int) -> Path:
    marker = venv / "pyvenv.cfg"
    if marker.is_symlink() or not marker.is_file():
        raise TransitionContextError("Venv besitzt keinen eindeutigen pyvenv.cfg-Marker")
    marker_info = marker.stat()
    if marker_info.st_nlink != 1 or marker_info.st_uid not in (0, account_uid):
        raise TransitionContextError("Venv-Marker besitzt keinen vertrauenswürdigen Eigentümer")
    if marker_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TransitionContextError("Venv-Marker ist gruppen- oder weltbeschreibbar")

    python = venv / "bin" / "python3"
    try:
        link_info = python.lstat()
        target = python.resolve(strict=True)
        target_info = target.stat()
    except OSError as exc:
        raise TransitionContextError("Venv-Interpreter ist nicht auflösbar") from exc
    if not stat.S_ISREG(target_info.st_mode) or not os.access(python, os.X_OK):
        raise TransitionContextError("Venv-Interpreter ist nicht ausführbar")
    if link_info.st_uid not in (0, account_uid) or target_info.st_uid not in (0, account_uid):
        raise TransitionContextError("Venv-Interpreter besitzt keinen vertrauenswürdigen Eigentümer")
    if not stat.S_ISLNK(link_info.st_mode) and link_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TransitionContextError("Venv-Interpreter-Link ist gruppen- oder weltbeschreibbar")
    if target_info.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise TransitionContextError("Venv-Interpreterziel ist gruppen- oder weltbeschreibbar")
    return python


def get_transition_context(
    *,
    explicit_install_path: str | None = None,
    explicit_install_user: str | None = None,
    explicit_home_dir: str | None = None,
    require_trusted: bool = False,
) -> TransitionContext:
    """Resolve an exact one-shot transition context without legacy guessing."""

    try:
        bootstrap_root = str(os.environ.get("E3DC_BOOTSTRAP_ROOT") or "").strip()
        root_values = _unique_nonempty((explicit_install_path, bootstrap_root))
        if len(root_values) > 1:
            roots = {str(Path(value).resolve(strict=True)) for value in root_values}
            if len(roots) != 1:
                raise TransitionContextError("Explizite Installationspfade widersprechen sich")
        root = _real_directory(root_values[0] if root_values else str(_MODULE_ROOT), "Produkt-Root")
        if not _has_product_markers(root):
            raise TransitionContextError("Produkt-Root besitzt nicht alle Release-Marker")

        bootstrap_user = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
        explicit_authority = bool(root_values and (explicit_install_user or bootstrap_user))
        metadata: list[tuple[str, dict]] = []
        rejected_metadata = False
        for path in (_METADATA_PATHS[0], *_METADATA_PATHS[1:]):
            try:
                data = _flatten_metadata(_read_metadata(path))
            except TransitionContextError:
                # Published 5.3.2b installations may still contain a 0664
                # installer_config.json.  Such data is ignored, never trusted;
                # a secure canonical record or complete root-owned bootstrap
                # authority must still resolve the context.
                rejected_metadata = True
                continue
            if data:
                metadata.append((str(path), data))
        if rejected_metadata and not metadata and not explicit_authority:
            raise TransitionContextError("Es existieren nur nicht vertrauenswürdige Pfadmetadaten")

        metadata_roots = _unique_nonempty(data.get("install_path") for _, data in metadata)
        for value in metadata_roots:
            candidate = _real_directory(value, "Metadaten-Produkt-Root")
            if candidate != root:
                raise TransitionContextError("Pfadmetadaten zeigen auf einen anderen Produkt-Root")

        user_values = _unique_nonempty(
            (explicit_install_user, bootstrap_user, *(data.get("install_user") for _, data in metadata))
        )
        if len(user_values) != 1:
            raise TransitionContextError("Installationsbenutzer fehlt oder ist widersprüchlich")
        install_user = user_values[0]
        if install_user == "www-data":
            raise TransitionContextError("Der Web-Benutzer darf kein Installationsbenutzer sein")
        try:
            account = pwd.getpwnam(install_user)
        except KeyError as exc:
            raise TransitionContextError("Installationsbenutzer existiert nicht") from exc

        home_values = _unique_nonempty(
            (explicit_home_dir, *(data.get("home_dir") for _, data in metadata))
        )
        account_home = str(Path(account.pw_dir).resolve(strict=True))
        if home_values:
            homes = {str(_real_directory(value, "Home-Verzeichnis")) for value in home_values}
            if homes != {account_home}:
                raise TransitionContextError("Home-Metadaten stimmen nicht mit dem Benutzerkonto überein")
        home_dir = account_home

        container = _is_container()
        if container and (str(root) != "/app/pi/Install" or home_dir != "/app"):
            raise TransitionContextError("Container-Kontext entspricht nicht dem Image-Vertrag")

        venv_values = _unique_nonempty(data.get("venv_path") for _, data in metadata)
        explicit_venv = str(os.environ.get("E3DC_BOOTSTRAP_VENV") or "").strip()
        if explicit_venv:
            venv_values = _unique_nonempty((explicit_venv, *venv_values))
        if len(venv_values) > 1:
            raise TransitionContextError("Venv-Metadaten sind widersprüchlich")
        venv_path = ""
        venv_python = ""
        if venv_values:
            venv = _real_directory(venv_values[0], "Venv-Verzeichnis")
            python = _validate_venv_python(venv, account.pw_uid)
            venv_path = str(venv)
            venv_python = str(python)

        source = "explicit" if root_values or bootstrap_user else "secure-metadata"
        return TransitionContext(
            str(root), install_user, home_dir, venv_path, venv_python, True, source, container, ""
        )
    except Exception as exc:
        if require_trusted:
            if isinstance(exc, TransitionContextError):
                raise
            raise TransitionContextError("Transition-Kontext ist nicht vertrauenswürdig") from exc
        return _untrusted(str(exc) or "Transition-Kontext ist nicht vertrauenswürdig")


def get_legacy_config_candidates(filename: str, *, include_web_root: bool = False) -> tuple[str, ...]:
    """Return only allowlisted legacy files below an already trusted context."""

    if not filename or os.path.basename(filename) != filename:
        raise TransitionContextError("Ungültiger Legacy-Dateiname")
    context = get_transition_context(require_trusted=True)
    roots = [Path(context.install_path), Path(context.home_dir) / "E3DC-Control"]
    if include_web_root:
        roots.extend((Path("/var/www/html/data"), Path("/var/www/html")))
    candidates: list[str] = []
    for root in roots:
        try:
            if root.is_symlink() or not root.is_dir():
                continue
            resolved_root = root.resolve(strict=True)
            candidate = resolved_root / filename
            if candidate.is_symlink() or not candidate.is_file():
                continue
            if candidate.resolve(strict=True).parent != resolved_root:
                continue
            value = str(candidate)
            if value not in candidates:
                candidates.append(value)
        except OSError:
            continue
    return tuple(candidates)

