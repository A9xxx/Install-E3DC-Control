import json
import hashlib
import os
import pwd
import grp
import logging
import stat
from pathlib import Path

from .config_secret_permissions import config_secret_file_mode
from .secure_file_transaction import (
    atomic_write_bound_file,
    ensure_bound_directory,
    exclusive_transaction_lock,
    restore_bound_file,
    set_bound_file_metadata,
    snapshot_bound_file,
    snapshots_match,
)

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
    "shadow_snapshot_token": "",
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
    if os.path.lexists(CONFIG_FILE):
        try:
            data = _read_json_dict_nofollow(CONFIG_FILE)
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


def save_config(config, *, _venv_binding_lock_held=False):
    """Persist installer config to disk."""
    if not _venv_binding_lock_held:
        with exclusive_transaction_lock("e3dc-venv-binding.lock"):
            return save_config(config, _venv_binding_lock_held=True)

    install_user = str(config.get("install_user") or "").strip()
    bound_user = get_install_user()
    if not install_user or install_user != bound_user:
        raise RuntimeError(
            "Zu speichernder Installationsbenutzer widerspricht dem lokalen Rollenanker"
        )
    uid, _ = get_user_ids(install_user)
    gid = get_www_data_gid()
    payload = json.dumps(config, indent=2).encode("utf-8")
    if len(payload) > 1024 * 1024:
        raise RuntimeError("installer_config.json überschreitet das Bytelimit")
    with exclusive_transaction_lock("e3dc-installer-config.lock"):
        previous = snapshot_bound_file(
            CONFIG_FILE,
            allow_missing=True,
            max_bytes=1024 * 1024,
        )
        committed = None
        try:
            committed = atomic_write_bound_file(
                CONFIG_FILE,
                payload,
                uid=uid,
                gid=gid,
                mode=0o640,
                expected_snapshot=previous,
            )
            readback, readback_snapshot = _read_json_snapshot_nofollow(CONFIG_FILE)
            if (
                readback != config
                or not snapshots_match(
                    readback_snapshot,
                    committed,
                    exact_metadata=True,
                )
            ):
                raise RuntimeError("installer_config.json-Readback weicht vom Soll ab")
        except Exception:
            _restore_json_projection(
                previous,
                committed,
                payload,
                uid=uid,
                gid=gid,
                mode=0o640,
                staging_root=None,
            )
            raise


def _bound_local_role_metadata():
    snapshot = snapshot_bound_file(
        CONFIG_FILE,
        allow_missing=True,
        max_bytes=1024 * 1024,
    )
    if not snapshot.get("exists"):
        return "", snapshot
    try:
        data = json.loads(snapshot["payload"].decode("utf-8-sig"))
    except Exception as exc:
        raise RuntimeError("Lokale Installer-Rollenmetadaten sind ungültig") from exc
    if not isinstance(data, dict):
        raise RuntimeError("Lokale Installer-Rollenmetadaten sind kein JSON-Objekt")
    return str(data.get("install_user") or "").strip(), snapshot


def get_install_user():
    """Löst die lokale Rolle ohne Rückautorisierung aus Web-Metadaten auf."""

    container_mode = str(os.environ.get("E3DC_CONTAINER_MODE") or "").strip().lower()
    container_user = str(
        os.environ.get("E3DC_CONTAINER_INSTALL_USER") or ""
    ).strip()
    if container_mode in {"1", "true", "yes"}:
        module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        if (
            container_user != "root"
            or os.geteuid() != 0
            or module_root != "/app/pi/Install"
        ):
            raise RuntimeError("Docker-Installationsrolle ist nicht exakt gebunden")
        _validated_strict_product_root(module_root, "Docker-Produktpfad")
        return container_user

    bootstrap_user = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
    if bootstrap_user:
        bootstrap_authority = _bound_download_bootstrap_authority()
        if bootstrap_user in {"root", "www-data"}:
            raise RuntimeError("Bootstrap-Nutzer ist kein zulässiges lokales Konto")
        try:
            bootstrap_account = pwd.getpwnam(bootstrap_user)
        except KeyError as exc:
            raise RuntimeError("Bootstrap-Nutzer existiert nicht") from exc
        if bootstrap_authority is not None:
            return bootstrap_user
        configured_user, role_snapshot = _bound_local_role_metadata()
        if role_snapshot.get("exists") and (
            role_snapshot.get("uid") != bootstrap_account.pw_uid
            or int(role_snapshot.get("mode") or 0) & 0o022
        ):
            raise RuntimeError(
                "Lokale Rollenmetadaten sind fremd- oder web-schreibbar"
            )
        if configured_user and configured_user != bootstrap_user:
            raise RuntimeError(
                "Bootstrap-Nutzer widerspricht der lokalen Installer-Rollenbindung"
            )
        return bootstrap_user

    configured_user, role_snapshot = _bound_local_role_metadata()
    if configured_user:
        if configured_user in {"root", "www-data"}:
            raise RuntimeError("Lokale Installer-Rolle ist nicht zulässig")
        try:
            account = pwd.getpwnam(configured_user)
        except KeyError as exc:
            raise RuntimeError("Lokale Installer-Rolle existiert nicht") from exc
        if (
            role_snapshot.get("uid") != account.pw_uid
            or int(role_snapshot.get("mode") or 0) & 0o022
        ):
            raise RuntimeError(
                "Lokale Installer-Rollenmetadaten sind fremd- oder web-schreibbar"
            )
        return configured_user

    effective_uid = os.geteuid()
    if effective_uid != 0:
        process_user = pwd.getpwuid(effective_uid).pw_name
        if process_user not in {"root", "www-data"}:
            return process_user
    raise RuntimeError("Installationsbenutzer ist nicht lokal gebunden")


def _get_from_web_config(key):
    if os.path.lexists(WEB_CONFIG_FILE):
        try:
            data = _read_json_dict_nofollow(WEB_CONFIG_FILE)
            if key in data:
                return data[key]
        except Exception:
            pass
    # Fallback to legacy
    elif os.path.lexists(LEGACY_WEB_CONFIG_FILE):
        try:
            data = _read_json_dict_nofollow(LEGACY_WEB_CONFIG_FILE)
            if key in data:
                return data[key]
        except Exception:
            pass
    return None


def get_home_dir(install_user=None):
    user = install_user or get_install_user()
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise RuntimeError("Home-Verzeichnis ist ohne gültiges Benutzerkonto nicht auflösbar") from exc
    home_dir = str(account.pw_dir or "").strip()
    if (
        not os.path.isabs(home_dir)
        or os.path.abspath(home_dir) != home_dir
        or os.path.realpath(home_dir) != home_dir
    ):
        raise RuntimeError("passwd-Home ist nicht kanonisch")
    try:
        metadata = os.lstat(home_dir)
    except OSError as exc:
        raise RuntimeError("passwd-Home ist nicht eindeutig lesbar") from exc
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != account.pw_uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise RuntimeError("passwd-Home besitzt keinen vertrauenswürdigen Verzeichnisvertrag")

    explicit_home = str(os.environ.get("E3DC_HOME_DIR") or "").strip()
    if explicit_home and explicit_home != home_dir:
        raise RuntimeError("E3DC_HOME_DIR widerspricht dem gebundenen passwd-Home")
    return home_dir


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


def _validated_strict_product_root(
    value,
    label,
    *,
    allow_one_missing_marker=False,
    ignore_release_markers=False,
):
    """Bindet einen echten Produktbaum ohne Symlink-Komponente."""

    candidate = str(value or "").strip()
    if not candidate or not os.path.isabs(candidate):
        raise RuntimeError(f"{label} fehlt oder ist nicht absolut")
    normalized = os.path.abspath(candidate)
    if normalized in {"/", "/bin", "/etc", "/home", "/lib", "/sbin", "/usr", "/var"}:
        raise RuntimeError(f"{label} ist zu weit gefasst")
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
    if not ignore_release_markers:
        markers = (
            os.path.join(normalized, "VERSION"),
            os.path.join(normalized, "installer_main.py"),
            os.path.join(normalized, "Installer", "installer_config.py"),
        )
        missing_markers = 0
        for marker in markers:
            try:
                metadata = os.lstat(marker)
            except FileNotFoundError:
                missing_markers += 1
                continue
            if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode):
                raise RuntimeError(f"{label} besitzt keinen eindeutigen Release-Marker")
        if missing_markers > (1 if allow_one_missing_marker else 0):
            raise RuntimeError(f"{label} besitzt nicht alle Release-Marker")
    return normalized


def _bound_download_bootstrap_authority():
    """Bindet den unabhängigen Release-Runner an genau einen Produkt-Root."""

    target_value = str(os.environ.get("E3DC_BOOTSTRAP_ROOT") or "").strip()
    runner_value = str(os.environ.get("E3DC_BOOTSTRAP_RUNNER_ROOT") or "").strip()
    user_value = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
    venv_value = str(os.environ.get("E3DC_BOOTSTRAP_VENV") or "").strip()
    if any((target_value, runner_value, user_value, venv_value)) and os.geteuid() != 0:
        raise RuntimeError("Bootstrap-Autorität ist ausschließlich als Root zulässig")
    if bool(target_value) != bool(runner_value):
        raise RuntimeError("Bootstrap-Root und Bootstrap-Runner müssen gemeinsam gebunden sein")
    if not target_value:
        return None
    if not user_value:
        raise RuntimeError("Download-Bootstrap besitzt keine vollständige Nutzerbindung")
    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    runner = _validated_strict_product_root(runner_value, "Bootstrap-Runner")
    target = _validated_strict_product_root(
        target_value,
        "Bootstrap-Ziel",
        ignore_release_markers=True,
    )
    module = _validated_strict_product_root(module_root, "Ausgeführter Release-Root")
    if runner != module:
        raise RuntimeError("Bootstrap-Runner stimmt nicht mit dem ausgeführten Release-Root überein")
    common = os.path.commonpath((runner, target))
    if runner == target or common in {runner, target}:
        raise RuntimeError("Bootstrap-Runner und Bootstrap-Ziel müssen getrennte Bäume sein")
    return target, runner


def _resolve_install_root(module_root, explicit_root="", configured_root=""):
    """Erlaubt einen fremden Zielroot nur im vollständig dual gebundenen Bootstrap."""

    module = _validated_product_root(module_root)
    bootstrap_authority = _bound_download_bootstrap_authority()
    if bootstrap_authority is not None:
        target, runner = bootstrap_authority
        strict_module = _validated_strict_product_root(module_root, "Ausgeführter Release-Root")
        if module != strict_module or runner != strict_module:
            raise RuntimeError("Bootstrap-Runner stimmt nicht mit dem ausgeführten Release-Root überein")
        if explicit_root and _validated_strict_product_root(
            explicit_root,
            "E3DC_INSTALL_ROOT",
            ignore_release_markers=True,
        ) != target:
            raise RuntimeError("E3DC_INSTALL_ROOT widerspricht dem gebundenen Bootstrap-Ziel")
        # Alte Produkt-/Web-Metadaten sind in diesem engen Pfad ausschließlich
        # Backup- und Reparaturdaten. Die aus dem unabhängigen, root-eigenen
        # Runner gebundene Zielwurzel darf von ihnen nicht rückautorisiert werden.
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
    except KeyError as exc:
        raise RuntimeError(
            f"Installationsbenutzer existiert nicht: {user!r}"
        ) from exc


def get_www_data_gid():
    return grp.getgrnam("www-data").gr_gid


def set_config_file_permissions(install_user=None):
    """Setzt Rechte der installer_config.json so, dass der Install-User zugreifen kann."""
    logger = logging.getLogger("install")
    user = install_user or get_install_user()

    try:
        # Ein fehlendes Fresh-Bookworm-Datenverzeichnis wird zuerst mit dem
        # festen Root-/Runtimeflächenvertrag angelegt; erst danach werden die
        # beiden fehlenden Dateinamen als gebundene Preimages erfasst.
        _ensure_web_metadata_directories(user)
    except Exception as exc:
        logger.error("Web-Metadatenverzeichnisse sind nicht sicher bindbar: %s", exc)
        return False

    try:
        uid, _ = get_user_ids(user)
        gid = get_www_data_gid()
        set_bound_file_metadata(
            CONFIG_FILE,
            uid=uid,
            gid=gid,
            mode=0o640,
            max_bytes=1024 * 1024,
        )
        logger.info("installer_config.json Rechte gesetzt auf %s:www-data (640)", user)
        return True
    except Exception as e:
        logger.warning("Konnte Rechte der installer_config.json nicht setzen: %s", e)
        return False


def _ensure_web_metadata_directories(user):
    uid, _ = get_user_ids(user)
    gid = get_www_data_gid()
    ensure_bound_directory("/var/www", uid=0, gid=0, mode=0o755)
    ensure_bound_directory("/var/www/html", uid=0, gid=gid, mode=0o755)
    ensure_bound_directory("/var/www/html/data", uid=uid, gid=gid, mode=0o2775)


def _write_json_with_web_permissions(path, data, user, *, expected_snapshot=None):
    _ensure_web_metadata_directories(user)
    uid, _ = get_user_ids(user)
    gid = get_www_data_gid()
    mode = (
        config_secret_file_mode(data)
        if os.path.basename(path) == "e3dc_v4.json"
        else 0o640
    )
    return atomic_write_bound_file(
        path,
        json.dumps(data, indent=2).encode("utf-8"),
        uid=uid,
        gid=gid,
        mode=mode,
        expected_snapshot=expected_snapshot,
        max_existing_bytes=1024 * 1024,
        staging_root="/var/www",
    )


def _restore_json_projection(
    previous,
    committed,
    payload,
    *,
    uid,
    gid,
    mode,
    staging_root="/var/www",
):
    current = snapshot_bound_file(
        previous["path"],
        allow_missing=True,
        max_bytes=1024 * 1024,
    )
    if snapshots_match(current, previous, exact_metadata=True):
        return
    desired_sha = hashlib.sha256(payload).hexdigest()
    ours = bool(
        current.get("exists")
        and current.get("kind") == "regular"
        and current.get("sha256") == desired_sha
        and current.get("uid") == int(uid)
        and current.get("gid") == int(gid)
        and current.get("mode") == int(mode)
    )
    if committed is not None and snapshots_match(
        current,
        committed,
        exact_metadata=False,
    ):
        ours = True
    if not ours:
        raise RuntimeError(f"Konfigurations-Rollbackziel driftete fremd: {previous['path']}")
    restored = restore_bound_file(
        previous,
        expected_current=current,
        staging_root=staging_root,
        max_bytes=1024 * 1024,
    )
    if previous.get("exists"):
        restored_ok = bool(
            restored.get("exists")
            and restored.get("kind") == "regular"
            and restored.get("sha256") == previous.get("sha256")
            and restored.get("uid") == previous.get("uid")
            and restored.get("gid") == previous.get("gid")
            and restored.get("mode") == previous.get("mode")
        )
    else:
        restored_ok = not restored.get("exists")
    if not restored_ok:
        raise RuntimeError(
            f"Konfigurations-Rollback blieb unvollständig: {previous['path']}"
        )


def _write_web_config_pair(
    v4_data,
    paths_data,
    user,
    *,
    expected_v4_snapshot,
    expected_paths_snapshot,
):
    """Projiziert V4- und Legacy-Pfadmetadaten gemeinsam oder gar nicht."""

    uid, _ = get_user_ids(user)
    gid = get_www_data_gid()
    v4_payload = json.dumps(v4_data, indent=2).encode("utf-8")
    paths_payload = json.dumps(paths_data, indent=2).encode("utf-8")
    if len(v4_payload) > 1024 * 1024 or len(paths_payload) > 1024 * 1024:
        raise RuntimeError("Web-Pfadkonfiguration überschreitet das Bytelimit")
    v4_mode = config_secret_file_mode(v4_data)
    v4_previous = None
    paths_previous = None
    v4_committed = None
    paths_committed = None
    with exclusive_transaction_lock("e3dc-web-config.lock"):
        v4_previous = snapshot_bound_file(
            WEB_CONFIG_FILE,
            allow_missing=True,
            max_bytes=1024 * 1024,
        )
        paths_previous = snapshot_bound_file(
            LEGACY_WEB_CONFIG_FILE,
            allow_missing=True,
            max_bytes=1024 * 1024,
        )
        if not snapshots_match(
            v4_previous,
            expected_v4_snapshot,
            exact_metadata=True,
        ) or not snapshots_match(
            paths_previous,
            expected_paths_snapshot,
            exact_metadata=True,
        ):
            raise RuntimeError(
                "Web-Konfigurationsquellen drifteten seit dem gebundenen Read"
            )
        _ensure_web_metadata_directories(user)
        try:
            v4_committed = _write_json_with_web_permissions(
                WEB_CONFIG_FILE,
                v4_data,
                user,
                expected_snapshot=v4_previous,
            )
            paths_committed = _write_json_with_web_permissions(
                LEGACY_WEB_CONFIG_FILE,
                paths_data,
                user,
                expected_snapshot=paths_previous,
            )
            v4_readback, v4_readback_snapshot = _read_json_snapshot_nofollow(
                WEB_CONFIG_FILE
            )
            paths_readback, paths_readback_snapshot = _read_json_snapshot_nofollow(
                LEGACY_WEB_CONFIG_FILE
            )
            if (
                v4_readback != v4_data
                or paths_readback != paths_data
                or not snapshots_match(
                    v4_readback_snapshot,
                    v4_committed,
                    exact_metadata=True,
                )
                or not snapshots_match(
                    paths_readback_snapshot,
                    paths_committed,
                    exact_metadata=True,
                )
            ):
                raise RuntimeError("Web-Konfigurations-Readback weicht vom Soll ab")
        except Exception as exc:
            rollback_errors = []
            for previous, committed, payload, mode in (
                (paths_previous, paths_committed, paths_payload, 0o640),
                (v4_previous, v4_committed, v4_payload, v4_mode),
            ):
                try:
                    _restore_json_projection(
                        previous,
                        committed,
                        payload,
                        uid=uid,
                        gid=gid,
                        mode=mode,
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                raise RuntimeError(
                    f"Web-Konfiguration fehlgeschlagen ({exc}); Rollback unvollständig: "
                    + "; ".join(rollback_errors)
                ) from exc
            raise


def _project_path_metadata(data, *, user, home_dir, install_path, venv_path):
    """Ersetzt nur die fünf technischen Pfad-/Nutzerfelder eines JSON-Objekts."""

    projected = dict(data)
    projected.update({
        "install_user": user,
        "home_dir": home_dir,
        "install_path": install_path,
    })
    if venv_path:
        projected.update({"venv_name": os.path.basename(venv_path), "venv_path": venv_path})
    else:
        projected.pop("venv_name", None)
        projected.pop("venv_path", None)
    return projected


def project_download_bootstrap_metadata(
    target_root,
    install_user,
    *,
    venv_path,
    expected_v4_config,
    expected_v4_sha256,
):
    """Repariert genau drei Pfadspiegel unter der Root-Bootstrap-Autorität."""

    authority = _bound_download_bootstrap_authority()
    if authority is None:
        raise RuntimeError("Altmetadaten-Projektion verlangt die Root-Bootstrap-Autorität")
    target = os.path.abspath(str(target_root or ""))
    if target != authority[0]:
        raise RuntimeError("Altmetadaten-Projektion weicht vom gebundenen Bootstrap-Ziel ab")
    user = str(install_user or "").strip()
    if user != str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip():
        raise RuntimeError("Altmetadaten-Projektion besitzt keine eindeutige Nutzerbindung")
    try:
        account = pwd.getpwnam(user)
    except KeyError as exc:
        raise RuntimeError("Installationsbenutzer der Altmetadaten-Projektion existiert nicht") from exc
    home, venv = get_home_dir(user), str(venv_path or "").strip()
    if venv and (
        os.path.abspath(venv) != venv
        or os.path.realpath(venv) != venv
        or os.path.dirname(venv) != home
    ):
        raise RuntimeError("Bootstrap-venv ist kein kanonisches direktes Home-Kind")

    installer_path = os.path.join(target, "Installer", "installer_config.json")
    uid, gid = int(account.pw_uid), int(get_www_data_gid())
    paths = (installer_path, WEB_CONFIG_FILE, LEGACY_WEB_CONFIG_FILE)

    with exclusive_transaction_lock("e3dc-bootstrap-metadata.lock"):
        sources, snapshots = {}, {}
        for path in paths:
            sources[path], snapshots[path] = _read_json_snapshot_nofollow(
                path,
                allow_missing=path != WEB_CONFIG_FILE,
            )
        if (
            sources[WEB_CONFIG_FILE] != expected_v4_config
            or snapshots[WEB_CONFIG_FILE].get("sha256") != expected_v4_sha256
        ):
            raise RuntimeError("V4-Konfiguration driftete vor der Altmetadaten-Projektion")

        projected = {
            path: _project_path_metadata(
                sources[path] or {}, user=user, home_dir=home,
                install_path=target, venv_path=venv,
            )
            for path in paths
        }
        payloads = {
            path: json.dumps(projected[path], indent=2).encode("utf-8")
            for path in paths
        }
        if max(map(len, payloads.values())) > 1024 * 1024:
            raise RuntimeError("Bootstrap-Pfadmetadaten überschreiten das Bytelimit")

        installer_committed = None
        web_projected = False
        v4_mode = config_secret_file_mode(projected[WEB_CONFIG_FILE])
        try:
            _write_web_config_pair(
                projected[WEB_CONFIG_FILE],
                projected[LEGACY_WEB_CONFIG_FILE],
                user=user,
                expected_v4_snapshot=snapshots[WEB_CONFIG_FILE],
                expected_paths_snapshot=snapshots[LEGACY_WEB_CONFIG_FILE],
            )
            web_projected = True
            installer_committed = atomic_write_bound_file(
                installer_path,
                payloads[installer_path], uid=uid, gid=gid, mode=0o640,
                expected_snapshot=snapshots[installer_path],
                max_existing_bytes=1024 * 1024,
            )
            for path in paths:
                readback, readback_snapshot = _read_json_snapshot_nofollow(path)
                if readback != projected[path] or (
                    path == installer_path
                    and not snapshots_match(readback_snapshot, installer_committed, exact_metadata=True)
                ):
                    raise RuntimeError("Bootstrap-Pfadspiegel-Readback weicht vom Soll ab")
        except Exception as exc:
            rollback_errors = []
            rollback = [(snapshots[installer_path], installer_committed, payloads[installer_path], 0o640, None)]
            if web_projected:
                rollback += [
                    (snapshots[LEGACY_WEB_CONFIG_FILE], None, payloads[LEGACY_WEB_CONFIG_FILE], 0o640, "/var/www"),
                    (snapshots[WEB_CONFIG_FILE], None, payloads[WEB_CONFIG_FILE], v4_mode, "/var/www"),
                ]
            for previous, committed, payload, mode, staging_root in rollback:
                try:
                    _restore_json_projection(
                        previous, committed, payload, uid=uid, gid=gid,
                        mode=mode, staging_root=staging_root,
                    )
                except Exception as rollback_exc:
                    rollback_errors.append(str(rollback_exc))
            if rollback_errors:
                raise RuntimeError(
                    f"Bootstrap-Pfadmetadaten fehlgeschlagen ({exc}); "
                    "Rollback unvollständig: " + "; ".join(rollback_errors)
                ) from exc
            raise

    return {
        "config": projected[WEB_CONFIG_FILE],
        "config_sha256": hashlib.sha256(payloads[WEB_CONFIG_FILE]).hexdigest(),
        "install_user": user,
        "home_dir": home,
        "install_path": target,
        "venv_path": venv,
    }


def _existing_installation_markers():
    markers = []
    for path in (
        "/var/www/html/index.php",
        "/var/www/html/helpers.php",
        "/var/www/html/VERSION",
    ):
        if os.path.lexists(path):
            markers.append(path)
    for unit_dir in (
        "/etc/systemd/system",
        "/lib/systemd/system",
        "/usr/lib/systemd/system",
    ):
        try:
            names = os.listdir(unit_dir)
        except OSError:
            continue
        for name in names:
            if (
                (name.startswith("e3dc-") and name.endswith(".service"))
                or name in {"e3dc.service", "energy_manager.service"}
            ):
                markers.append(os.path.join(unit_dir, name))
    return tuple(sorted(set(markers)))


def _is_regular_file_nofollow(path):
    try:
        metadata = os.lstat(path)
    except FileNotFoundError:
        return False
    return stat.S_ISREG(metadata.st_mode)


def _read_json_snapshot_nofollow(
    path,
    *,
    allow_missing=False,
    max_bytes=1024 * 1024,
):
    snapshot = snapshot_bound_file(
        path,
        allow_missing=allow_missing,
        max_bytes=max_bytes,
    )
    if not snapshot.get("exists"):
        return None, snapshot
    decoded = json.loads(snapshot["payload"].decode("utf-8-sig"))
    if not isinstance(decoded, dict):
        raise ValueError("Konfigurationsdatei enthält kein JSON-Objekt")
    return decoded, snapshot


def _read_json_dict_nofollow(path, *, max_bytes=1024 * 1024):
    """Liest genau eine gebundene reguläre JSON-Datei ohne Link-Folgen."""
    decoded, _snapshot = _read_json_snapshot_nofollow(path, max_bytes=max_bytes)
    return decoded


def ensure_web_config(
    install_user=None,
    *,
    bind_first_install_role=False,
    explicit_venv_name=None,
    explicit_venv_path=None,
    require_bound_venv=False,
    _venv_binding_lock_held=False,
):
    """Write V4 web config so PHP can resolve paths and handle tariffs."""
    if not _venv_binding_lock_held:
        with exclusive_transaction_lock("e3dc-venv-binding.lock"):
            return ensure_web_config(
                install_user,
                bind_first_install_role=bind_first_install_role,
                explicit_venv_name=explicit_venv_name,
                explicit_venv_path=explicit_venv_path,
                require_bound_venv=require_bound_venv,
                _venv_binding_lock_held=True,
            )

    logger = logging.getLogger("install")
    try:
        bound_user = get_install_user()
    except Exception as exc:
        logger.error("Lokaler Installer-Rollenanker ist nicht bindbar: %s", exc)
        return False
    user = str(install_user or bound_user).strip()
    if user != bound_user:
        logger.error(
            "Expliziter Installationsbenutzer widerspricht dem lokalen Rollenanker"
        )
        return False

    # Fresh Bookworm besitzt nach der Apache-Paketinstallation regelmäßig noch
    # kein data-Verzeichnis. Die festen Verzeichnisrollen müssen deshalb vor
    # dem ersten Missing-Preimage sicher projiziert werden.
    try:
        _ensure_web_metadata_directories(user)
    except Exception as exc:
        logger.error("Web-Metadatenverzeichnisse sind nicht sicher bindbar: %s", exc)
        return False

    try:
        web_data, web_input_snapshot = _read_json_snapshot_nofollow(
            WEB_CONFIG_FILE,
            allow_missing=True,
        )
    except Exception as exc:
        logger.error(
            "e3dc_v4.json ist nicht sicher und eindeutig lesbar; "
            "automatische Konfigurationsänderung gesperrt: %s",
            exc,
        )
        return False
    try:
        legacy_data, legacy_input_snapshot = _read_json_snapshot_nofollow(
            LEGACY_WEB_CONFIG_FILE,
            allow_missing=True,
        )
    except Exception as exc:
        logger.error(
            "e3dc_paths.json ist nicht sicher und eindeutig lesbar; "
            "automatische Konfigurationsmigration gesperrt: %s",
            exc,
        )
        return False
    if web_data is None:
        existing_markers = _existing_installation_markers()
        if existing_markers and legacy_data is None:
            logger.error(
                "e3dc_v4.json fehlt bei vorhandenen Installationsmarkern; "
                "Default-Neuanlage und Rollenannahme bleiben gesperrt: %s",
                ", ".join(existing_markers),
            )
            return False

    # Migriere alte Datei falls vorhanden
    migrated_legacy = False
    if legacy_data is not None and web_data is None:
        # Die Legacy-Bytes werden nur als gebundenes Input-Preimage verwendet.
        # V4 und Legacy werden erst nach der vollständigen Projektion geschrieben.
        web_data = dict(legacy_data)
        migrated_legacy = True

    try:
        first_install = (
            not migrated_legacy
            and (web_data is None or bind_first_install_role)
        )
        old_data = dict(web_data or {})
        if bind_first_install_role:
            projected_role = str(old_data.get("ha_mode") or "").strip().lower()
            projected_peer = str(old_data.get("ha_peer_ip") or "").strip()
            if projected_role not in {"", "off"} or projected_peer:
                raise RuntimeError(
                    "Fresh-Rollenbindung widerspricht der festen Einzelanlagenrolle"
                )
        existing_user = old_data.get("install_user")

        data = dict(old_data)
        data.update({
            "install_user": user,
            "home_dir": get_home_dir(user),
            "install_path": get_install_path(user)
        })
        try:
            # Lokaler Import vermeidet einen Modulzyklus während des frühen
            # Bootstrap-Imports. Projiziert werden ausschließlich ein direkter
            # passwd-Home-Child und dessen vollständig gebundene Laufzeit.
            from .utils import require_bound_venv_runtime, resolve_venv_target

            resolved_name, resolved_path = resolve_venv_target(
                user,
                requested_venv_name=explicit_venv_name,
            )
            requested_name = str(
                resolved_name
                if explicit_venv_name is None
                else explicit_venv_name
            ).strip()
            requested_path = str(explicit_venv_path or resolved_path)
            if requested_name != resolved_name or requested_path != resolved_path:
                raise RuntimeError("Explizites venv widerspricht dem kanonischen Ziel")
            require_bound_venv_runtime(
                install_user=user,
                venv_name=resolved_name,
                venv_path=resolved_path,
            )
            data["venv_name"] = resolved_name
            data["venv_path"] = resolved_path
        except Exception:
            data.pop("venv_name", None)
            data.pop("venv_path", None)
            if require_bound_venv:
                raise
        data = apply_web_config_start_defaults(data, first_install=first_install)
        if bind_first_install_role:
            # Der erste privilegierte Rollenanker stammt aus dem expliziten
            # Fresh-Vertrag, niemals rückwärts aus der web-schreibbaren Datei.
            data["ha_mode"] = "off"

        needs_write = not web_input_snapshot.get("exists") or existing_user != user
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
        if web_input_snapshot.get("exists"):
            data.update({
                key: value
                for key, value in old_data.items()
                if key not in [
                    "install_user",
                    "home_dir",
                    "install_path",
                    "venv_name",
                    "venv_path",
                    *WEB_CONFIG_START_DEFAULTS.keys(),
                ]
            })
        if bind_first_install_role:
            data["ha_mode"] = "off"

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
        _write_web_config_pair(
            data,
            paths_payload,
            user,
            expected_v4_snapshot=web_input_snapshot,
            expected_paths_snapshot=legacy_input_snapshot,
        )
        logger.info(
            "e3dc_v4.json und e3dc_paths.json atomar projiziert: "
            "user=%s, home_dir=%s, install_path=%s",
            data["install_user"],
            data["home_dir"],
            data["install_path"],
        )
        return True
    except Exception as e:
        logger.error("Fehler beim Schreiben von e3dc_v4.json: %s", e)
        return False
def get_venv_name():
    """Liest den venv-Namen nur aus dem lokalen, nicht web-schreibbaren Anker."""

    default = ".venv_e3dc"
    data, snapshot = _read_json_snapshot_nofollow(
        CONFIG_FILE,
        allow_missing=True,
        max_bytes=1024 * 1024,
    )
    if data is None:
        return default
    user = get_install_user()
    account = pwd.getpwnam(user)
    if (
        snapshot.get("uid") != account.pw_uid
        or int(snapshot.get("mode") or 0) & 0o022
    ):
        return default
    name = str(data.get("venv_name") or default).strip()
    if (
        not name
        or name in {".", ".."}
        or os.path.basename(name) != name
        or os.path.sep in name
        or (os.path.altsep and os.path.altsep in name)
        or any(not (character.isalnum() or character in "._-") for character in name)
    ):
        raise RuntimeError("Lokaler venv-Name ist ungültig")
    return name

def get_venv_path(install_user=None):
    """Bindet das venv an passwd-Home und lokalen Namen; Webdaten sind nur Spiegel."""
    user = install_user or get_install_user()
    expected = os.path.join(get_home_dir(user), get_venv_name())
    projected = str(_get_from_web_config("venv_path") or "").strip()
    if projected and projected != expected:
        raise RuntimeError("Web-venv-Pfad widerspricht dem lokalen Laufzeitanker")
    return expected

def get_venv_python(install_user=None):
    """Gibt den Pfad zum Python-Interpreter im venv zurück."""
    return os.path.join(get_venv_path(install_user), "bin", "python3")

def get_venv_pip(install_user=None):
    """Gibt den Pfad zum pip-Binary im venv zurück."""
    return os.path.join(get_venv_path(install_user), "bin", "pip")
