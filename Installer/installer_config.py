import json
import os
import pwd
import grp

DEFAULT_INSTALL_USER = "pi"
CONFIG_FILE = os.path.join(os.path.dirname(__file__), "installer_config.json")
WEB_CONFIG_FILE = "/var/www/html/e3dc_paths.json"


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
    return {"install_user": DEFAULT_INSTALL_USER}


def save_config(config):
    """Persist installer config to disk."""
    os.makedirs(os.path.dirname(CONFIG_FILE), exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2)


def get_install_user():
    config = load_config()
    return config.get("install_user", DEFAULT_INSTALL_USER)


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


def ensure_web_config(install_user=None):
    """Write web config so PHP can resolve paths."""
    user = install_user or get_install_user()
    data = {
        "install_user": user,
        "home_dir": get_home_dir(user),
        "install_path": get_install_path(user)
    }
    try:
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
        return True
    except Exception:
        return False
