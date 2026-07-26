import json
import os
import pwd
import grp
import logging
import stat
import tempfile
from pathlib import Path

from .config_secret_permissions import config_secret_file_mode

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "installer_config.json")
WEB_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
LEGACY_WEB_CONFIG_FILE = "/var/www/html/e3dc_paths.json"
WEB_CONFIG_START_DEFAULTS = {
    "server_ip": "",
    "server_port": "5033",
    "e3dc_user": "",
    "e3dc_password": "",
    "aes_password": "",
    "wurzelzaehler": "0",
    "wurzelzaehler_invertiert": "0",
    "frontend_variant": "classic",
    "frontend_detail_mode": "normal",
    "config_secret_protection_mode": "standard",
    "forecast_diagnostics_enable": "0",
}


def apply_web_config_start_defaults(data, *, first_install=False):
    """Fill first-start defaults that must exist before the Web-UI is saved."""
    result = dict(data or {})
    for key, default in WEB_CONFIG_START_DEFAULTS.items():
        value = result.get(key)
        if key not in result or value is None or value == "":
            result[key] = default
    # Die Rolle darf ausschließlich beim erstmaligen Erzeugen der V4-Datei
    # vorbelegt werden. Bestehende Anlagen ohne Rollenbindung bleiben bewusst
    # fail-closed und werden nicht still als Einzelanlage umgedeutet.
    if first_install and not str(result.get("ha_mode") or "").strip():
        result["ha_mode"] = "off"
    return result

def get_default_install_user():
    """Ermittelt einen sinnvollen Default-User ohne statisches Hardcoding."""
    env_user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    if env_user and env_user not in ["root", "www-data"]:
        return env_user

    try:
        for entry in pwd.getpwall():
            if entry.pw_uid == 1000 and entry.pw_name != "root":
                return entry.pw_name

        for entry in pwd.getpwall():
            if entry.pw_uid >= 1000 and entry.pw_name != "root" and entry.pw_dir.startswith("/home/"):
                return entry.pw_name
    except Exception:
        pass

    return "root"


def load_config():
    """Load installer config from disk or return defaults."""
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception:
            pass
    return {"install_user": get_default_install_user()}


def _system_user_exists(user):
    try:
        return bool(user) and pwd.getpwnam(str(user)) is not None
    except Exception:
        return False


def save_config(config):
    """Persist installer config to disk."""
    install_user = config.get("install_user", get_default_install_user())
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    set_config_file_permissions(install_user)


def get_install_user():
    config = load_config()
    configured_user = str(config.get("install_user") or "").strip()
    web_user = str(_get_from_web_config("install_user") or "").strip()
    web_install_path = str(_get_from_web_config("install_path") or "").strip()

    # Auf bestehenden Fremdsystemen kann installer_config.json fehlen oder aus
    # einem alten Pfad stammen. Die V4-Web-Config ist dort die kanonische lokale
    # Metadatenquelle und darf nicht von einem Default-User wie pi überschrieben
    # werden.
    if web_user and _system_user_exists(web_user) and web_install_path and os.path.exists(web_install_path):
        return web_user
    if configured_user and _system_user_exists(configured_user):
        return configured_user
    if web_user and _system_user_exists(web_user):
        return web_user
    return get_default_install_user()


def _get_from_web_config(key):
    if os.path.exists(WEB_CONFIG_FILE):
        try:
            with open(WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if key in data: return data[key]
        except Exception:
            pass
    # Fallback to legacy
    elif os.path.exists(LEGACY_WEB_CONFIG_FILE):
        try:
            with open(LEGACY_WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if key in data: return data[key]
        except Exception:
            pass
    return None


def get_home_dir(install_user=None):
    if not install_user:
        d = _get_from_web_config("home_dir")
        if d and os.path.isabs(str(d)) and os.path.isdir(d):
            return os.path.realpath(d)

    explicit_home = str(os.environ.get("E3DC_HOME_DIR") or "").strip()
    if explicit_home:
        if not os.path.isabs(explicit_home) or not os.path.isdir(explicit_home):
            raise RuntimeError("E3DC_HOME_DIR ist kein vorhandenes absolutes Verzeichnis")
        return os.path.realpath(explicit_home)

    user = install_user or get_install_user()
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError as exc:
        raise RuntimeError("Home-Verzeichnis ist ohne gültiges Benutzerkonto nicht auflösbar") from exc


def _validated_product_root(value):
    candidate = str(value or "").strip()
    if not candidate or not os.path.isabs(candidate):
        raise RuntimeError("Installationspfad fehlt oder ist nicht absolut")
    resolved = os.path.realpath(candidate)
    markers = (
        os.path.join(resolved, "VERSION"),
        os.path.join(resolved, "installer_main.py"),
        os.path.join(resolved, "Installer", "installer_config.py"),
    )
    if not all(os.path.isfile(path) for path in markers):
        raise RuntimeError("Installationspfad besitzt nicht alle Release-Marker")
    return resolved


def _validated_strict_product_root(value, label):
    """Bindet einen echten Produktbaum ohne Symlink-Komponente."""

    candidate = str(value or "").strip()
    if not candidate or not os.path.isabs(candidate):
        raise RuntimeError(f"{label} fehlt oder ist nicht absolut")
    normalized = os.path.abspath(candidate)
    current = os.path.sep
    for component in Path(normalized).parts[1:]:
        current = os.path.join(current, component)
        try:
            metadata = os.lstat(current)
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} existiert nicht") from exc
        if stat.S_ISLNK(metadata.st_mode):
            raise RuntimeError(f"{label} enthält eine Symlink-Komponente")
    if not stat.S_ISDIR(os.lstat(normalized).st_mode):
        raise RuntimeError(f"{label} ist kein Verzeichnis")
    if os.path.realpath(normalized) != normalized:
        raise RuntimeError(f"{label} ist nicht kanonisch")
    markers = (
        os.path.join(normalized, "VERSION"),
        os.path.join(normalized, "installer_main.py"),
        os.path.join(normalized, "Installer", "installer_config.py"),
    )
    for marker in markers:
        try:
            metadata = os.lstat(marker)
        except FileNotFoundError as exc:
            raise RuntimeError(f"{label} besitzt nicht alle Release-Marker") from exc
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError(f"{label} besitzt keinen eindeutigen Release-Marker")
    return normalized


def _resolve_install_root(module_root, explicit_root="", configured_root=""):
    """Erlaubt einen fremden Zielroot nur im vollständig dual gebundenen Bootstrap."""

    module = _validated_product_root(module_root)
    bootstrap_target = str(os.environ.get("E3DC_BOOTSTRAP_ROOT") or "").strip()
    bootstrap_runner = str(os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT") or "").strip()
    if bool(bootstrap_target) != bool(bootstrap_runner):
        raise RuntimeError("Bootstrap-Root und Bootstrap-Runner müssen gemeinsam gebunden sein")

    if bootstrap_target:
        runner = _validated_strict_product_root(bootstrap_runner, "Bootstrap-Runner")
        target = _validated_strict_product_root(bootstrap_target, "Bootstrap-Ziel")
        strict_module = _validated_strict_product_root(module_root, "Ausgeführter Release-Root")
        if runner != strict_module or module != strict_module:
            raise RuntimeError("Bootstrap-Runner stimmt nicht mit dem ausgeführten Release-Root überein")
        common = os.path.commonpath((runner, target))
        if runner == target or common in {runner, target}:
            raise RuntimeError("Bootstrap-Runner und Bootstrap-Ziel müssen getrennte Bäume sein")
        for candidate, label in (
            (explicit_root, "E3DC_INSTALL_ROOT"),
            (configured_root, "Pfadmetadaten"),
        ):
            if candidate and _validated_strict_product_root(candidate, label) != target:
                raise RuntimeError(f"{label} widersprechen dem gebundenen Bootstrap-Ziel")
        return target

    root = _validated_product_root(explicit_root or configured_root or module_root)
    if root != module:
        raise RuntimeError("Pfadmetadaten und ausgeführter Release-Root widersprechen sich")
    return root


def get_install_path(install_user=None):
    """Löst den exakten Produktstamm auf, ohne ein Benutzerverzeichnis zu durchsuchen."""
    explicit = str(os.environ.get("E3DC_INSTALL_ROOT") or "").strip()
    configured = str(_get_from_web_config("install_path") or "").strip()
    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return _resolve_install_root(module_root, explicit, configured)


def get_user_ids(install_user=None):
    user = install_user or get_install_user()
    try:
        info = pwd.getpwnam(user)
        return info.pw_uid, info.pw_gid
    except KeyError:
        return os.getuid(), os.getgid()


def get_www_data_gid():
    return grp.getgrnam("www-data").gr_gid


def set_config_file_permissions(install_user=None):
    """Setzt Rechte der installer_config.json so, dass der Install-User zugreifen kann."""
    logger = logging.getLogger("install")
    user = install_user or get_install_user()

    try:
        uid, _ = get_user_ids(user)
        gid = get_www_data_gid()
        os.chown(CONFIG_FILE, uid, gid)
        os.chmod(CONFIG_FILE, 0o664)
        logger.info("installer_config.json Rechte gesetzt auf %s:www-data (664)", user)
        return True
    except Exception as e:
        logger.warning("Konnte Rechte der installer_config.json nicht setzen: %s", e)
        return False


def _write_json_with_web_permissions(path, data, user):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".e3dc_cfg_", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp_path, path)
        try:
            uid, _ = get_user_ids(user)
            gid = get_www_data_gid()
            os.chown(path, uid, gid)
            mode = config_secret_file_mode(data) if os.path.basename(path) == "e3dc_v4.json" else 0o664
            os.chmod(path, mode)
        except Exception:
            pass
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except Exception:
            pass


def ensure_web_config(install_user=None, *, bind_first_install_role=False):
    """Write V4 web config so PHP can resolve paths and handle tariffs."""
    logger = logging.getLogger("install")
    user = install_user or get_install_user()

    # Migriere alte Datei falls vorhanden
    if os.path.exists(LEGACY_WEB_CONFIG_FILE) and not os.path.exists(WEB_CONFIG_FILE):
        os.makedirs(os.path.dirname(WEB_CONFIG_FILE), exist_ok=True)
        try:
            import shutil
            shutil.move(LEGACY_WEB_CONFIG_FILE, WEB_CONFIG_FILE)
            logger.info("Migrated e3dc_paths.json to e3dc_v4.json")
        except Exception as e:
            pass

    try:
        first_install = not os.path.exists(WEB_CONFIG_FILE) or bind_first_install_role
        old_data = {}
        existing_user = None
        if os.path.exists(WEB_CONFIG_FILE):
            try:
                with open(WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                if isinstance(existing_data, dict):
                    old_data = existing_data
                    existing_user = existing_data.get("install_user")
            except Exception:
                existing_user = None

        data = dict(old_data)
        data.update({
            "install_user": user,
            "home_dir": get_home_dir(user),
            "install_path": get_install_path(user)
        })
        installer_data = load_config()
        if installer_data.get("venv_name"):
            data["venv_name"] = installer_data["venv_name"]
            data["venv_path"] = installer_data.get("venv_path") or os.path.join(data["home_dir"], installer_data["venv_name"])
        data = apply_web_config_start_defaults(data, first_install=first_install)

        needs_write = not os.path.exists(WEB_CONFIG_FILE) or existing_user != user
        if not needs_write:
            for key in (
                "install_user",
                "home_dir",
                "install_path",
                "venv_name",
                "venv_path",
                "ha_mode",
                *WEB_CONFIG_START_DEFAULTS.keys(),
            ):
                if old_data.get(key) != data.get(key):
                    needs_write = True
                    break
        if not needs_write:
            try:
                with open(LEGACY_WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
                    legacy_data = json.load(f)
                needs_write = (
                    not isinstance(legacy_data, dict)
                    or legacy_data.get("install_user") != data.get("install_user")
                    or legacy_data.get("home_dir") != data.get("home_dir")
                    or legacy_data.get("install_path") != data.get("install_path")
                    or legacy_data.get("venv_name") != data.get("venv_name")
                    or legacy_data.get("venv_path") != data.get("venv_path")
                )
            except Exception:
                needs_write = True

        # Nur bei Erstinstallation (Datei fehlt) oder manuellem Benutzerwechsel schreiben
        if not needs_write:
            logger.info(
                "e3dc_v4.json unverändert (install_user=%s) – Defaults und Pfade vorhanden.",
                user
            )
            return True

        # Preserve other arbitrary V4 values!
        if os.path.exists(WEB_CONFIG_FILE):
            try:
                with open(WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
                    old_data = json.load(f)
                    data.update({k:v for k,v in old_data.items() if k not in ["install_user", "home_dir", "install_path", "venv_name", "venv_path", *WEB_CONFIG_START_DEFAULTS.keys()]})
            except: pass

        _write_json_with_web_permissions(WEB_CONFIG_FILE, data, user)
        logger.info(
            "e3dc_v4.json path config generiert: user=%s, home_dir=%s, install_path=%s",
            data["install_user"],
            data["home_dir"],
            data["install_path"]
        )

        # Legacy-Kompatibilität: einige alte Dienste und das Install-Center lesen
        # e3dc_paths.json sehr früh im Start. Deshalb immer als echte JSON-Datei
        # schreiben, nicht nur als Symlink, und mit denselben User-/Pfadwerten.
        paths_payload = {
            "install_user": data["install_user"],
            "home_dir": data["home_dir"],
            "install_path": data["install_path"],
        }
        for key in ("venv_name", "venv_path"):
            if data.get(key):
                paths_payload[key] = data[key]
        try:
            if os.path.islink(LEGACY_WEB_CONFIG_FILE):
                os.unlink(LEGACY_WEB_CONFIG_FILE)
            _write_json_with_web_permissions(LEGACY_WEB_CONFIG_FILE, paths_payload, user)
            logger.info("e3dc_paths.json aktualisiert: install_path=%s", paths_payload["install_path"])
        except Exception as exc:
            logger.warning("e3dc_paths.json konnte nicht aktualisiert werden: %s", exc)
        return True
    except Exception as e:
        logger.error("Fehler beim Schreiben von e3dc_v4.json: %s", e)
        return False
def get_venv_name():
    """Gibt den Namen des Virtual Environments zurück (Standard: .venv_e3dc)."""
    return load_config().get("venv_name", ".venv_e3dc")

def get_venv_path(install_user=None):
    """Gibt den absoluten Pfad zum venv-Ordner zurück."""
    p = _get_from_web_config("venv_path")
    if p and os.path.exists(p): return p

    user = install_user or get_install_user()
    return os.path.join(get_home_dir(user), get_venv_name())

def get_venv_python(install_user=None):
    """Gibt den Pfad zum Python-Interpreter im venv zurück."""
    return os.path.join(get_venv_path(install_user), "bin", "python3")

def get_venv_pip(install_user=None):
    """Gibt den Pfad zum pip-Binary im venv zurück."""
    return os.path.join(get_venv_path(install_user), "bin", "pip")
