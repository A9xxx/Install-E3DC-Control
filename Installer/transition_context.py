"""Strikter Kontext nur für Release-Bootstrap, Sicherung, Update und Rollback.

Die normale Produktlaufzeit behält ihre etablierte Pfadsemantik. Dieses Modul
ist bewusst getrennt, damit der Übergang aus einer unabhängigen Historie weder
Nutzer, Home-Verzeichnis, Installationswurzel noch virtuelle Umgebung erraten
muss.
"""

from __future__ import annotations

from dataclasses import dataclass
import errno
import grp
import json
import os
from pathlib import Path
import pwd
import stat
from typing import Iterable


class TransitionContextError(RuntimeError):
    """Wird ausgelöst, wenn der einmalige Release-Kontext nicht eindeutig ist."""


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


def venv_group_is_private(group_id: int, install_user: str) -> bool:
    """Akzeptiert Gruppen-Schreibrechte nur ohne fremde Gruppenmitglieder."""
    try:
        group = grp.getgrgid(int(group_id))
        principals = {
            entry.pw_name
            for entry in pwd.getpwall()
            if int(entry.pw_gid) == int(group_id)
        }
        principals.update(str(name) for name in group.gr_mem if str(name))
    except (KeyError, OSError, TypeError, ValueError):
        return False
    return principals.issubset({"root", str(install_user)})


def venv_has_extended_acl(path: Path) -> bool:
    """Unbekannte oder erweiterte POSIX-ACLs bleiben an der Vertrauensgrenze rot."""
    try:
        names = set(os.listxattr(
            path,
            follow_symlinks=False,
        ))
        return bool({"system.posix_acl_access", "system.posix_acl_default"} & names)
    except AttributeError:
        return False
    except OSError as exc:
        if exc.errno in {
            getattr(errno, "ENODATA", -1),
            getattr(errno, "ENOTSUP", -1),
            getattr(errno, "EOPNOTSUPP", -1),
        }:
            return False
        return True


def venv_metadata_is_trusted(
    path: Path,
    account,
    *,
    kind: str,
    single_link: bool = False,
) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        return False
    if path.is_symlink():
        return False
    if kind == "directory" and not stat.S_ISDIR(metadata.st_mode):
        return False
    if kind == "regular" and not stat.S_ISREG(metadata.st_mode):
        return False
    if single_link and metadata.st_nlink != 1:
        return False
    if metadata.st_uid not in (0, account.pw_uid):
        return False
    if metadata.st_mode & stat.S_IWOTH:
        return False
    if metadata.st_mode & stat.S_IWGRP and not venv_group_is_private(
        metadata.st_gid,
        account.pw_name,
    ):
        return False
    return not venv_has_extended_acl(path)


def venv_directory_chain_is_trusted(root: Path, target: Path, account) -> bool:
    try:
        relative = target.relative_to(root)
    except ValueError:
        return False
    current = root
    if not venv_metadata_is_trusted(current, account, kind="directory"):
        return False
    for component in relative.parts:
        current = current / component
        if not venv_metadata_is_trusted(current, account, kind="directory"):
            return False
    return True


def _validate_venv_python(venv: Path, account) -> Path:
    marker = venv / "pyvenv.cfg"
    if not venv_metadata_is_trusted(
        marker,
        account,
        kind="regular",
        single_link=True,
    ):
        raise TransitionContextError("Venv besitzt keinen vertrauenswürdigen pyvenv.cfg-Marker")

    bin_dir = venv / "bin"
    if not venv_directory_chain_is_trusted(venv, bin_dir, account):
        raise TransitionContextError("Venv-bin besitzt keinen vertrauenswürdigen Pfad")

    python = bin_dir / "python3"
    try:
        link_info = python.lstat()
        target = python.resolve(strict=True)
        target_info = target.stat()
    except OSError as exc:
        raise TransitionContextError("Venv-Interpreter ist nicht auflösbar") from exc
    if not os.access(python, os.X_OK):
        raise TransitionContextError("Venv-Interpreter ist nicht ausführbar")
    if link_info.st_uid not in (0, account.pw_uid):
        raise TransitionContextError("Venv-Interpreter besitzt keinen vertrauenswürdigen Eigentümer")
    if not stat.S_ISLNK(link_info.st_mode) and not venv_metadata_is_trusted(
        python,
        account,
        kind="regular",
    ):
        raise TransitionContextError("Venv-Interpreterdatei ist nicht vertrauenswürdig")
    if not venv_metadata_is_trusted(target, account, kind="regular"):
        raise TransitionContextError("Venv-Interpreterziel ist nicht vertrauenswürdig")
    return python


def get_transition_context(
    *,
    explicit_install_path: str | None = None,
    explicit_install_user: str | None = None,
    explicit_home_dir: str | None = None,
    explicit_venv_path: str | None = None,
    require_trusted: bool = False,
) -> TransitionContext:
    """Löst einen exakten einmaligen Übergangskontext ohne Altbestandsannahmen auf."""

    try:
        bootstrap_root = str(os.environ.get("E3DC_BOOTSTRAP_ROOT") or "").strip()
        bootstrap_runner = str(
            os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT") or ""
        ).strip()
        bootstrap_user = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
        bootstrap_venv = str(os.environ.get("E3DC_BOOTSTRAP_VENV") or "").strip()
        if any((bootstrap_root, bootstrap_runner, bootstrap_user, bootstrap_venv)) and os.geteuid() != 0:
            raise TransitionContextError(
                "Bootstrap-Autorität ist ausschließlich als Root zulässig"
            )
        root_values = _unique_nonempty((explicit_install_path, bootstrap_root))
        if len(root_values) > 1:
            roots = {str(Path(value).resolve(strict=True)) for value in root_values}
            if len(roots) != 1:
                raise TransitionContextError("Explizite Installationspfade widersprechen sich")
        root = _real_directory(root_values[0] if root_values else str(_MODULE_ROOT), "Produkt-Root")
        if not _has_product_markers(root):
            raise TransitionContextError("Produkt-Root besitzt nicht alle Release-Marker")

        if bool(bootstrap_root) != bool(bootstrap_runner):
            raise TransitionContextError(
                "Bootstrap-Root und Bootstrap-Runner müssen gemeinsam gebunden sein"
            )
        bootstrap_authority = False
        if bootstrap_root:
            if not bootstrap_user:
                raise TransitionContextError(
                    "Download-Bootstrap besitzt keine vollständige Nutzerbindung"
                )
            runner = _real_directory(bootstrap_runner, "Bootstrap-Runner")
            target = _real_directory(bootstrap_root, "Bootstrap-Ziel")
            module_root = _real_directory(str(_MODULE_ROOT), "Ausgeführter Release-Root")
            if runner != module_root or target != root:
                raise TransitionContextError(
                    "Download-Bootstrap stimmt nicht mit Runner oder Ziel überein"
                )
            common = os.path.commonpath((str(runner), str(target)))
            if runner == target or common in {str(runner), str(target)}:
                raise TransitionContextError(
                    "Bootstrap-Runner und Bootstrap-Ziel müssen getrennte Bäume sein"
                )
            bootstrap_authority = True
        explicit_authority = bool(
            bootstrap_authority
            or (root_values and (explicit_install_user or bootstrap_user))
        )
        metadata: list[tuple[str, dict]] = []
        rejected_metadata = False
        if not bootstrap_authority:
            for path in (_METADATA_PATHS[0], *_METADATA_PATHS[1:]):
                try:
                    data = _flatten_metadata(_read_metadata(path))
                except TransitionContextError:
                    # Veröffentlichte 5.3.2a-Installationen können noch eine
                    # installer_config.json mit Modus 0664 enthalten. Solche Daten
                    # werden ignoriert und nie als vertrauenswürdig behandelt; ein
                    # sicherer kanonischer Datensatz oder eine vollständige
                    # root-eigene Bootstrap-Autorität muss den Kontext auflösen.
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
        raw_account_home = Path(account.pw_dir)
        account_home_path = _real_directory(str(raw_account_home), "Benutzer-Home")
        if not venv_metadata_is_trusted(account_home_path, account, kind="directory"):
            raise TransitionContextError("Benutzer-Home ist nicht vertrauenswürdig")
        account_home = str(account_home_path)
        if home_values:
            homes = {str(_real_directory(value, "Home-Verzeichnis")) for value in home_values}
            if homes != {account_home}:
                raise TransitionContextError("Home-Metadaten stimmen nicht mit dem Benutzerkonto überein")
        home_dir = account_home

        container = _is_container()
        if container and (str(root) != "/app/pi/Install" or home_dir != "/app"):
            raise TransitionContextError("Container-Kontext entspricht nicht dem Image-Vertrag")

        metadata_venvs = _unique_nonempty(data.get("venv_path") for _, data in metadata)
        venv_values = _unique_nonempty(
            (explicit_venv_path, bootstrap_venv, *metadata_venvs)
        )
        if len(venv_values) > 1:
            raise TransitionContextError("Venv-Metadaten sind widersprüchlich")
        venv_path = ""
        venv_python = ""
        if venv_values:
            venv = _real_directory(venv_values[0], "Venv-Verzeichnis")
            if not venv_directory_chain_is_trusted(account_home_path, venv, account):
                raise TransitionContextError("Venv-Pfad ist nicht vertrauenswürdig")
            python = _validate_venv_python(venv, account)
            venv_path = str(venv)
            venv_python = str(python)

        source = "bootstrap-authority" if bootstrap_authority else (
            "explicit"
            if root_values or bootstrap_user or explicit_venv_path
            else "secure-metadata"
        )
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
    """Liefert nur freigegebene Altdateien unterhalb eines bereits vertrauenswürdigen Kontexts."""

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
