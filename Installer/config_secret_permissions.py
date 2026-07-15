"""Shared permissions for e3dc_v4.json and local config backups."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping

try:
    import grp
    import pwd
except ImportError:  # pragma: no cover - Windows tests fake these modules elsewhere
    grp = None
    pwd = None


CONFIG_SECRET_PROTECTION_MODE_KEY = "config_secret_protection_mode"
STANDARD_MODE = "standard"
COMPATIBILITY_MODE = "compatibility"
V4_CONFIG_PATH = "/var/www/html/data/e3dc_v4.json"


def normalise_secret_protection_mode(value: Any) -> str:
    raw = str(value or "").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"compat", "compatible", "compatibility", "legacy", "world_readable", "664"}:
        return COMPATIBILITY_MODE
    return STANDARD_MODE


def secret_protection_mode_from_data(data: Mapping[str, Any] | None) -> str:
    if not isinstance(data, Mapping):
        return STANDARD_MODE
    return normalise_secret_protection_mode(data.get(CONFIG_SECRET_PROTECTION_MODE_KEY, STANDARD_MODE))


def read_secret_protection_mode(config_path: str | os.PathLike[str] = V4_CONFIG_PATH) -> str:
    try:
        with open(config_path, "r", encoding="utf-8-sig") as handle:
            data = json.load(handle)
        return secret_protection_mode_from_data(data if isinstance(data, Mapping) else {})
    except Exception:
        return STANDARD_MODE


def config_secret_file_mode(data: Mapping[str, Any] | None = None) -> int:
    mode = secret_protection_mode_from_data(data) if data is not None else read_secret_protection_mode()
    return 0o664 if mode == COMPATIBILITY_MODE else 0o660


def config_secret_dir_mode(data: Mapping[str, Any] | None = None) -> int:
    mode = secret_protection_mode_from_data(data) if data is not None else read_secret_protection_mode()
    return 0o2775 if mode == COMPATIBILITY_MODE else 0o2770


def config_secret_file_mode_text(data: Mapping[str, Any] | None = None) -> str:
    return oct(config_secret_file_mode(data))[2:]


def config_secret_dir_mode_text(data: Mapping[str, Any] | None = None) -> str:
    return oct(config_secret_dir_mode(data))[2:]


def apply_config_secret_permissions(
    path: str | os.PathLike[str],
    *,
    install_user: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> bool:
    """Best-effort owner/group/mode for config files that may contain secrets."""
    target = Path(path)
    ok = True
    try:
        if install_user and pwd is not None:
            uid = pwd.getpwnam(str(install_user)).pw_uid
            gid = grp.getgrnam("www-data").gr_gid if grp is not None else -1
            os.chown(target, uid, gid)
        elif grp is not None:
            os.chown(target, -1, grp.getgrnam("www-data").gr_gid)
    except Exception:
        ok = False
    try:
        os.chmod(target, config_secret_file_mode(data))
    except Exception:
        ok = False
    return ok


def apply_config_backup_dir_permissions(
    path: str | os.PathLike[str],
    *,
    install_user: str | None = None,
    data: Mapping[str, Any] | None = None,
) -> bool:
    """Best-effort owner/group/mode for config backup directories."""
    target = Path(path)
    ok = True
    try:
        if install_user and pwd is not None:
            uid = pwd.getpwnam(str(install_user)).pw_uid
            gid = grp.getgrnam("www-data").gr_gid if grp is not None else -1
            os.chown(target, uid, gid)
        elif grp is not None:
            os.chown(target, -1, grp.getgrnam("www-data").gr_gid)
    except Exception:
        ok = False
    try:
        os.chmod(target, config_secret_dir_mode(data))
    except Exception:
        ok = False
    return ok
