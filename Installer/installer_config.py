import json
import os
import pwd
import grp
import logging

CONFIG_FILE = os.path.join(os.path.dirname(__file__), "installer_config.json")
WEB_CONFIG_FILE = "/var/www/html/e3dc_paths.json"


def get_default_install_user():
    """Ermittelt einen sinnvollen Default-User ohne statisches Hardcoding."""
    env_user = os.environ.get("SUDO_USER") or os.environ.get("USER")
    if env_user and env_user != "root":
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


def save_config(config):
    """Persist installer config to disk."""
    install_user = config.get("install_user", get_default_install_user())
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)
    set_config_file_permissions(install_user)


def get_install_user():
    config = load_config()
    return config.get("install_user", get_default_install_user())


def get_home_dir(install_user=None):
    user = install_user or get_install_user()
    try:
        return pwd.getpwnam(user).pw_dir
    except KeyError:
        return os.path.join("/home", user)


def get_install_path(install_user=None):
    return os.path.join(get_home_dir(install_user), "E3DC-Control")


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


def ensure_web_config(install_user=None):
    """Write web config so PHP can resolve paths."""
    logger = logging.getLogger("install")
    user = install_user or get_install_user()
    data = {
        "install_user": user,
        "home_dir": get_home_dir(user),
        "install_path": get_install_path(user)
    }
    try:
        existing_user = None
        if os.path.exists(WEB_CONFIG_FILE):
            try:
                with open(WEB_CONFIG_FILE, "r", encoding="utf-8") as f:
                    existing_data = json.load(f)
                if isinstance(existing_data, dict):
                    existing_user = existing_data.get("install_user")
            except Exception:
                existing_user = None

        # Nur bei Erstinstallation (Datei fehlt) oder manuellem Benutzerwechsel schreiben
        if os.path.exists(WEB_CONFIG_FILE) and existing_user == user:
            logger.info(
                "e3dc_paths.json unverändert (install_user=%s) – kein Rewrite notwendig.",
                user
            )
            return True

        os.makedirs(os.path.dirname(WEB_CONFIG_FILE), exist_ok=True)
        with open(WEB_CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        try:
            uid, _ = get_user_ids(user)
            gid = get_www_data_gid()
            os.chown(WEB_CONFIG_FILE, uid, gid)
            os.chmod(WEB_CONFIG_FILE, 0o664)
        except Exception:
            pass
        logger.info(
            "e3dc_paths.json geschrieben: user=%s, home_dir=%s, install_path=%s",
            data["install_user"],
            data["home_dir"],
            data["install_path"]
        )
        return True
    except Exception as e:
        logger.error("Fehler beim Schreiben von e3dc_paths.json: %s", e)
        return False
