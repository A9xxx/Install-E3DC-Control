import json
import os
import pwd
import grp
import logging
import tempfile

from .config_secret_permissions import config_secret_file_mode

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "installer_config.json")
WEB_CONFIG_FILE = "/var/www/html/data/e3dc_v4.json"
LEGACY_WEB_CONFIG_FILE = "/var/www/html/e3dc_paths.json"
WEB_CONFIG_START_DEFAULTS = {
    "server_ip": "",
    "server_port": "5033",
    "e3dc_user": "local.user",
    "e3dc_password": "",
    "aes_password": "",
    "wurzelzaehler": "0",
    "wurzelzaehler_invertiert": "0",
    "frontend_variant": "classic",
    "frontend_detail_mode": "normal",
    "config_secret_protection_mode": "standard",
}


def apply_web_config_start_defaults(data):
    """Fill first-start defaults that must exist before the Web-UI is saved."""
    result = dict(data or {})
    for key, default in WEB_CONFIG_START_DEFAULTS.items():
        value = result.get(key)
        if key not in result or value is None or value == "":
            result[key] = default
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
        if d and os.path.exists(d): return d
        
    user = install_user or get_install_user()
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return os.path.join("/home", user)


def get_install_path(install_user=None):
    """Ermittelt den V4 Install-Pfad dynamisch. Primaer: e3dc_v4.json, Fallback: /home/pi/Install."""
    # 1. V4 Config (kanonisch seit Migration)
    p = _get_from_web_config("install_path")
    if p and os.path.exists(p):
        return p

    user = install_user or get_install_user()
    home = get_home_dir(user)

    # 2. Neues V4 Standardverzeichnis
    v4_path = os.path.join(home, "Install")
    if os.path.exists(v4_path):
        return v4_path

    # 3. Legacy C++ Verzeichnis (nur wenn vorhanden – Übergangsphase)
    legacy_path = os.path.join(home, "E3DC-Control")
    if os.path.exists(legacy_path):
        return legacy_path

    # 4. Installer-Root (falls wir schon in Install/ laufen)
    installer_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if os.path.exists(os.path.join(installer_root, "Installer")):
        return installer_root

    # Absoluter Fallback: V4 Standardpfad (neues System)
    return v4_path


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


def ensure_web_config(install_user=None):
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
        data = apply_web_config_start_defaults(data)

        needs_write = not os.path.exists(WEB_CONFIG_FILE) or existing_user != user
        if not needs_write:
            for key in ("install_user", "home_dir", "install_path", "venv_name", "venv_path", *WEB_CONFIG_START_DEFAULTS.keys()):
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
