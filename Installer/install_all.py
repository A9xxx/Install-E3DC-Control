import os
import pwd
import hashlib
import socket
import stat

from .core import register_command
from .permissions import run_permissions_wizard
from .utils import install_system_packages
from .create_config import create_e3dc_config
from .strompreis_wizard import strompreis_wizard

from .ramdisk import setup_ramdisk
from .backup import backup_current_version
from .utils import run_command, cleanup_pycache
from .installer_config import get_home_dir, get_install_path, get_user_ids, get_www_data_gid, load_config, save_config, get_install_user, ensure_web_config
from .logging_manager import setup_installation_loggers, print_installation_summary, log_task_completed, log_error, log_warning
from .task_executor import safe_execute_task
from .secure_file_transaction import (
    atomic_write_bound_file,
    ensure_bound_directory,
    exclusive_transaction_lock,
    open_bound_directory,
    read_bound_regular_file,
    remove_bound_directory_if_empty,
    remove_bound_file,
    remove_bound_regular_tree,
    restore_bound_file,
    restore_bound_regular_tree,
    set_bound_file_metadata,
    snapshot_bound_file,
    snapshot_bound_regular_tree,
    snapshots_match,
    tree_snapshots_match,
)


_MAX_LEGACY_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_WEB_FILE_BYTES = 16 * 1024 * 1024
_MAX_WEB_TREE_BYTES = 128 * 1024 * 1024
_WEB_WRITABLE_TOP_LEVEL = frozenset({"data", "logs", "ramdisk", "tmp"})
_WEB_RUNTIME_TOP_LEVEL = frozenset(
    {
        "data",
        "logs",
        "ramdisk",
        "tmp",
        "history_backups",
        "live_history.txt",
        "e3dc_paths.json",
        "e3dc.config.txt",
        "e3dc.strompreise.txt",
        "e3dc.wallbox.txt",
        "e3dc.wallbox.out",
    }
)


def _read_bound_legacy_config(path, *, expected_uid):
    """Kompatibilitätswrapper für den gemeinsamen fd-/SHA-gebundenen Reader."""

    snapshot = read_bound_regular_file(
        os.path.abspath(path),
        expected_uid=int(expected_uid),
        max_bytes=_MAX_LEGACY_CONFIG_BYTES,
    )
    if int(snapshot.get("mode") or 0) & 0o022:
        raise RuntimeError(
            "Legacy-Konfigurationsquelle ist gruppen- oder weltbeschreibbar"
        )
    return snapshot


def _project_bound_legacy_config_locked(
    snapshot,
    target_path,
    *,
    uid,
    gid,
    target_preimage=None,
):
    """Projiziert gebundene Bytes über root-kontrolliertes Same-FS-Staging."""

    payload = snapshot.get("payload")
    if not isinstance(payload, bytes):
        raise RuntimeError("Gebundene Config-Bytes fehlen.")
    target = os.path.abspath(target_path)
    if target_preimage is not None:
        preimage = target_preimage
    elif str(snapshot.get("path") or "") == target:
        # Quelle und Ziel können bei einer bestehenden Standardinstallation
        # identisch sein. Dann bleibt exakt das Quellpreimage zugleich der
        # Commit-Vorbehalt; eine Änderung nach der Auswahl darf nicht verloren
        # gehen.
        preimage = snapshot
    else:
        preimage = snapshot_bound_file(
            target,
            allow_missing=True,
            max_bytes=_MAX_LEGACY_CONFIG_BYTES,
        )
    committed = None
    try:
        committed = atomic_write_bound_file(
            target,
            payload,
            uid=int(uid),
            gid=int(gid),
            mode=0o640,
            expected_snapshot=preimage,
            max_existing_bytes=_MAX_LEGACY_CONFIG_BYTES,
        )
        return committed
    except Exception as exc:
        current = snapshot_bound_file(
            target,
            allow_missing=True,
            max_bytes=_MAX_LEGACY_CONFIG_BYTES,
        )
        if snapshots_match(current, preimage, exact_metadata=True):
            raise
        ours = bool(
            current.get("exists")
            and current.get("kind") == "regular"
            and current.get("sha256") == hashlib.sha256(payload).hexdigest()
            and current.get("uid") == int(uid)
            and current.get("gid") == int(gid)
            and current.get("mode") == 0o640
        )
        if committed is not None and snapshots_match(
            current,
            committed,
            exact_metadata=False,
        ):
            ours = True
        if not ours:
            raise RuntimeError(
                f"Legacy-Konfigurationsrollback ist wegen Fremddrift gesperrt: {target}"
            ) from exc
        restored = restore_bound_file(
            preimage,
            expected_current=current,
            max_bytes=_MAX_LEGACY_CONFIG_BYTES,
        )
        if preimage.get("exists"):
            rollback_ok = bool(
                restored.get("exists")
                and restored.get("kind") == "regular"
                and restored.get("sha256") == preimage.get("sha256")
                and restored.get("uid") == preimage.get("uid")
                and restored.get("gid") == preimage.get("gid")
                and restored.get("mode") == preimage.get("mode")
            )
        else:
            rollback_ok = not restored.get("exists")
        if not rollback_ok:
            raise RuntimeError(
                f"Legacy-Konfigurationsrollback blieb unvollständig: {target}"
            ) from exc
        raise


def _project_bound_legacy_config(snapshot, target_path, *, uid, gid):
    with exclusive_transaction_lock("e3dc-product-config.lock"):
        return _project_bound_legacy_config_locked(
            snapshot,
            target_path,
            uid=uid,
            gid=gid,
        )


def _ensure_bound_legacy_wallbox_config(target_path, *, uid, gid):
    """Erstellt oder repariert die Legacy-Datei ohne einen Pfadlink zu verfolgen."""

    with exclusive_transaction_lock("e3dc-product-config.lock"):
        target = os.path.abspath(target_path)
        preimage = snapshot_bound_file(
            target,
            allow_missing=True,
            max_bytes=_MAX_LEGACY_CONFIG_BYTES,
        )
        if not preimage.get("exists"):
            _project_bound_legacy_config_locked(
                {"payload": b"# Wallbox Konfiguration (Legacy C++ Modus)\n"},
                target,
                uid=uid,
                gid=gid,
                target_preimage=preimage,
            )
            return
        set_bound_file_metadata(
            target,
            uid=int(uid),
            gid=int(gid),
            mode=0o640,
            expected_snapshot=preimage,
            max_bytes=_MAX_LEGACY_CONFIG_BYTES,
        )

def _resolve_complete_install_authority(install_path, install_user, venv_name):
    """Bindet den Komplettlauf an Wrapper-Nutzer, Produktroot und passwd-Home."""
    bootstrap_user = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
    if not bootstrap_user or bootstrap_user in {"root", "www-data"}:
        raise RuntimeError(
            "Der Komplettinstaller benötigt den durch e3dc-setup gebundenen "
            "normalen Installationsbenutzer."
        )
    if str(install_user or "").strip() != bootstrap_user:
        raise RuntimeError(
            "Konfigurierter Installationsbenutzer und Bootstrap-Nutzer widersprechen sich."
        )
    try:
        account = pwd.getpwnam(bootstrap_user)
    except KeyError as exc:
        raise RuntimeError("Der gebundene Installationsbenutzer existiert nicht.") from exc

    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if (
        not os.path.isabs(str(install_path or ""))
        or os.path.abspath(str(install_path)) != module_root
        or os.path.realpath(str(install_path)) != module_root
    ):
        raise RuntimeError(
            "Der konfigurierte Installationspfad entspricht nicht dem laufenden Produktroot."
        )

    home_dir = get_home_dir(bootstrap_user)
    if home_dir != str(account.pw_dir or "").strip():
        raise RuntimeError("Das passwd-Home des Installationsbenutzers ist nicht eindeutig.")

    normalized_venv_name = str(venv_name or "").strip()
    if (
        not normalized_venv_name
        or normalized_venv_name in {".", ".."}
        or os.path.basename(normalized_venv_name) != normalized_venv_name
        or "/" in normalized_venv_name
        or "\\" in normalized_venv_name
        or any(
            not (character.isalnum() or character in "._-")
            for character in normalized_venv_name
        )
    ):
        raise RuntimeError("Der Venv-Name muss ein einzelner lokaler Verzeichnisname sein.")
    venv_path = os.path.join(home_dir, normalized_venv_name)
    if os.path.commonpath((home_dir, venv_path)) != home_dir:
        raise RuntimeError("Das Venv muss innerhalb des passwd-Home liegen.")

    return {
        "install_path": module_root,
        "install_user": bootstrap_user,
        "home_dir": home_dir,
        "venv_name": normalized_venv_name,
        "venv_path": venv_path,
    }


def get_ip_address():
    """Holt die lokale IP-Adresse."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        # Es ist nicht notwendig, eine echte Verbindung herzustellen
        s.connect(("8.8.8.8", 80))
        ip_address = s.getsockname()[0]
        s.close()
        return ip_address
    except Exception:
        return "<IP nicht gefunden>"


def _query_apache_state():
    result = run_command(
        "systemctl show --no-pager --property=LoadState --property=ActiveState apache2.service",
        timeout=15,
    )
    if not result.get("success"):
        raise RuntimeError(
            "Apache-Zustand ist nicht beweisbar: "
            + str(result.get("stderr") or result.get("stdout") or "unbekannter Fehler")
        )
    values = {}
    for line in str(result.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip().lower()
    if values.get("LoadState") != "loaded":
        raise RuntimeError("Apache-Unit ist nach der Paketinstallation nicht geladen")
    active_state = values.get("ActiveState")
    if active_state not in {"active", "inactive", "failed"}:
        raise RuntimeError(f"Apache-Aktivzustand ist unklar: {active_state!r}")
    return {"active_state": active_state, "was_active": active_state == "active"}


def _stop_apache_confirmed():
    result = run_command("sudo systemctl stop apache2.service", timeout=30)
    after = _query_apache_state()
    if after["active_state"] not in {"inactive", "failed"}:
        raise RuntimeError(
            "Apache konnte nicht sicher gestoppt werden: "
            + str(result.get("stderr") or result.get("stdout") or "unbekannter Fehler")
        )


def _start_apache_confirmed():
    result = run_command("sudo systemctl start apache2.service", timeout=30)
    if not result.get("success") or _query_apache_state()["active_state"] != "active":
        raise RuntimeError(
            "Apache konnte nicht sicher gestartet werden: "
            + str(result.get("stderr") or result.get("stdout") or "unbekannter Fehler")
        )


def _restore_apache_state(previous):
    if previous.get("was_active"):
        _start_apache_confirmed()
    elif previous.get("active_state") == "inactive":
        _stop_apache_confirmed()
    else:
        raise RuntimeError(
            "Apache-Prestate ist nicht exakt restaurierbar: "
            + str(previous.get("active_state") or "unbekannt")
        )


def _web_snapshot_matches_desired(current, payload, uid, gid):
    return bool(
        current.get("exists")
        and current.get("kind") == "regular"
        and current.get("sha256") == hashlib.sha256(payload).hexdigest()
        and current.get("uid") == int(uid)
        and current.get("gid") == int(gid)
        and current.get("mode") == 0o644
    )


def _restore_web_file(previous, committed, desired_payload, *, uid, gid):
    current = snapshot_bound_file(
        previous["path"],
        allow_missing=True,
        max_bytes=_MAX_WEB_FILE_BYTES,
    )
    if snapshots_match(current, previous, exact_metadata=True):
        return
    restored = None
    if committed is not None and snapshots_match(
        current,
        committed,
        exact_metadata=False,
    ):
        restored = restore_bound_file(
            previous,
            expected_current=current,
            staging_root="/var/www",
            max_bytes=_MAX_WEB_FILE_BYTES,
        )
    elif _web_snapshot_matches_desired(current, desired_payload, uid, gid):
        restored = restore_bound_file(
            previous,
            expected_current=current,
            staging_root="/var/www",
            max_bytes=_MAX_WEB_FILE_BYTES,
        )
    if restored is None:
        raise RuntimeError(f"Web-Rollback-Ziel driftete fremd: {previous['path']}")
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
        raise RuntimeError(f"Web-Rollback blieb unvollständig: {previous['path']}")


def _restore_removed_web_file(previous, removed):
    """Restauriert auch einen Unlink, der vor dem Helper-Return bereits wirkte."""

    current = snapshot_bound_file(
        previous["path"],
        allow_missing=True,
        max_bytes=_MAX_WEB_FILE_BYTES,
    )
    if snapshots_match(current, previous, exact_metadata=True):
        return
    if current.get("exists"):
        raise RuntimeError(f"Web-Rollback-Ziel driftete fremd: {previous['path']}")
    expected_current = removed if removed is not None else current
    restored = restore_bound_file(
        previous,
        expected_current=expected_current,
        staging_root="/var/www",
        max_bytes=_MAX_WEB_FILE_BYTES,
    )
    if not (
        restored.get("exists")
        and restored.get("kind") == "regular"
        and restored.get("sha256") == previous.get("sha256")
        and restored.get("uid") == previous.get("uid")
        and restored.get("gid") == previous.get("gid")
        and restored.get("mode") == previous.get("mode")
    ):
        raise RuntimeError(
            f"Rollback der entfernten Webdatei blieb unvollständig: {previous['path']}"
        )


def _require_web_directory_contract(
    path,
    *,
    uid,
    gid,
    mode,
    expected_identity=None,
):
    descriptor, identity = open_bound_directory(path)
    try:
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISDIR(metadata.st_mode)
            or metadata.st_uid != int(uid)
            or metadata.st_gid != int(gid)
            or stat.S_IMODE(metadata.st_mode) != int(mode)
            or (
                expected_identity is not None
                and tuple(identity) != tuple(expected_identity)
            )
        ):
            raise RuntimeError(f"Web-Verzeichnisvertrag weicht ab: {path}")
        return identity
    finally:
        os.close(descriptor)


def _require_web_runtime_top_level_types(web_root, expected_root_identity):
    """Bindet explizit ausgeschlossene Web-Schreibflächen als echte Verzeichnisse."""

    root_descriptor, root_identity = open_bound_directory(web_root)
    try:
        if tuple(root_identity)[:2] != tuple(expected_root_identity)[:2]:
            raise RuntimeError("Webroot driftete vor der Runtimeflächenprüfung")
        for name in sorted(_WEB_WRITABLE_TOP_LEVEL):
            try:
                named = os.stat(
                    name,
                    dir_fd=root_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if stat.S_ISLNK(named.st_mode) or not stat.S_ISDIR(named.st_mode):
                raise RuntimeError(
                    f"Web-Runtimefläche ist kein echtes Verzeichnis: {name}"
                )
            child_descriptor, child_identity = open_bound_directory(
                os.path.join(web_root, name)
            )
            try:
                if tuple(child_identity)[:2] != (named.st_dev, named.st_ino):
                    raise RuntimeError(
                        f"Web-Runtimefläche wechselte beim Öffnen: {name}"
                    )
            finally:
                os.close(child_descriptor)
    finally:
        os.close(root_descriptor)


def _topmost_web_directories(paths):
    """Reduziert Pfade auf disjunkte, oberste zu entfernende Teilbäume."""

    selected = []
    for relative in sorted(set(paths), key=lambda value: (value.count(os.sep), value)):
        if any(
            relative == parent or relative.startswith(parent + os.sep)
            for parent in selected
        ):
            continue
        selected.append(relative)
    return selected


def _path_below_any(relative, parents):
    return any(
        relative == parent or relative.startswith(parent + os.sep)
        for parent in parents
    )


def _web_program_tree_matches_desired(
    snapshot,
    *,
    desired_directories,
    desired_payloads,
    install_uid,
    www_gid,
):
    """Bestätigt den exakten, gehärteten Zielbaum ohne Runtimeflächen."""

    actual_directories = dict(snapshot.get("directories") or {})
    actual_files = dict(snapshot.get("files") or {})
    if set(actual_directories) != set(desired_directories):
        return False
    if set(actual_files) != set(desired_payloads):
        return False
    if (
        snapshot.get("root_uid") != 0
        or snapshot.get("root_gid") != int(www_gid)
        or snapshot.get("root_mode") != 0o755
    ):
        return False
    for metadata in actual_directories.values():
        if (
            metadata.get("uid") != int(install_uid)
            or metadata.get("gid") != int(www_gid)
            or metadata.get("mode") != 0o755
        ):
            return False
    for relative, metadata in actual_files.items():
        payload = desired_payloads[relative]
        if (
            metadata.get("kind") != "regular"
            or metadata.get("sha256") != hashlib.sha256(payload).hexdigest()
            or metadata.get("uid") != int(install_uid)
            or metadata.get("gid") != int(www_gid)
            or metadata.get("mode") != 0o644
        ):
            return False
    return True


def _publish_webportal_transaction(authority):
    """Publiziert ausschließlich den gebundenen Produkt-Webbaum mit Rollback."""

    print("-> Installiere Web-Portal (/var/www/html)...")
    html_src = os.path.join(authority["install_path"], "html")
    install_uid, _install_gid = get_user_ids(authority["install_user"])
    www_gid = get_www_data_gid()
    web_root = "/var/www/html"
    desired_payloads = {}
    desired_directories = []
    release_source_snapshots = {}

    try:
        source_tree = snapshot_bound_regular_tree(
            html_src,
            expected_uid=install_uid,
            require_owner_only_write=True,
            max_file_bytes=_MAX_WEB_FILE_BYTES,
            max_total_bytes=_MAX_WEB_TREE_BYTES,
        )
        source_top_level = {
            relative.split(os.sep, 1)[0]
            for relative in (
                set(source_tree.get("directories") or {})
                | set(source_tree.get("files") or {})
            )
        }
        forbidden_source_entries = sorted(
            source_top_level & _WEB_RUNTIME_TOP_LEVEL
        )
        if forbidden_source_entries:
            raise RuntimeError(
                "Produkt-Webquelle enthält reservierte Runtimeflächen: "
                + ", ".join(forbidden_source_entries)
            )
        desired_directories = sorted(
            dict(source_tree["directories"]),
            key=lambda value: (value.count(os.sep), value),
        )
        desired_payloads = {
            relative: snapshot["payload"]
            for relative, snapshot in dict(source_tree["files"]).items()
        }
        for name in ("VERSION", "CHANGELOG.md", "UPDATE_POLICY.json"):
            if name in desired_directories or any(
                relative.startswith(name + os.sep)
                for relative in desired_directories
            ):
                raise RuntimeError(
                    f"Release-Metadatum kollidiert mit einem Quellverzeichnis: {name}"
                )
            source = os.path.join(authority["install_path"], name)
            metadata_snapshot = read_bound_regular_file(
                source,
                expected_uid=install_uid,
                max_bytes=_MAX_WEB_FILE_BYTES,
            )
            if int(metadata_snapshot["mode"]) & 0o022:
                raise RuntimeError(
                    f"Release-Metadatum ist gruppen- oder weltbeschreibbar: {source}"
                )
            desired_payloads[name] = metadata_snapshot["payload"]
            release_source_snapshots[name] = metadata_snapshot
    except Exception as exc:
        print(f"  [!] Webportal-Quelle ist nicht sicher gebunden: {exc}")
        return False

    from .apache_security import (
        apache_runtime_paths_protected,
        ensure_apache_runtime_path_protection,
    )
    from .permissions import harden_web_program_permissions

    target_preimages = {}
    target_committed = {}
    target_directory_snapshots = {}
    created_directories = []
    removed_program_files = []
    removed_program_trees = []
    removed_stale_app = False
    apache_prestate = None
    apache_state_mutation_started = False
    publication_started = False

    try:
        with exclusive_transaction_lock("e3dc-webportal-publish.lock"):
            try:
                apache_prestate = _query_apache_state()
                if apache_prestate.get("active_state") == "failed":
                    raise RuntimeError(
                        "Apache befindet sich in einem nicht exakt restaurierbaren Failed-Prestate"
                    )
                apache_state_mutation_started = True
                _stop_apache_confirmed()
                if not harden_web_program_permissions(
                    web_root=web_root,
                    install_user=authority["install_user"],
                    web_group="www-data",
                ):
                    raise RuntimeError(
                        "Web-Programmbaum bestand den fd-relativen Hardening-Gate nicht"
                    )
                web_root_identity = _require_web_directory_contract(
                    web_root,
                    uid=0,
                    gid=www_gid,
                    mode=0o755,
                )
                _require_web_runtime_top_level_types(
                    web_root,
                    web_root_identity,
                )
                publication_started = True

                target_program_tree = snapshot_bound_regular_tree(
                    web_root,
                    exclude_top_level=_WEB_RUNTIME_TOP_LEVEL,
                    max_file_bytes=_MAX_WEB_FILE_BYTES,
                    max_total_bytes=_MAX_WEB_TREE_BYTES,
                )
                target_directory_metadata = dict(
                    target_program_tree.get("directories") or {}
                )
                target_directories = set(target_directory_metadata)
                target_files = set(dict(target_program_tree.get("files") or {}))
                desired_directory_set = set(desired_directories)
                desired_file_set = set(desired_payloads)

                obsolete_directories = _topmost_web_directories(
                    target_directories - desired_directory_set
                )
                obsolete_files = sorted(
                    relative
                    for relative in target_files - desired_file_set
                    if not _path_below_any(relative, obsolete_directories)
                )

                for relative in sorted(
                    obsolete_directories,
                    key=lambda value: (value.count(os.sep), value),
                    reverse=True,
                ):
                    subtree = snapshot_bound_regular_tree(
                        os.path.join(web_root, relative),
                        max_file_bytes=_MAX_WEB_FILE_BYTES,
                        max_total_bytes=_MAX_WEB_TREE_BYTES,
                    )
                    # Das Preimage muss vor dem ersten Unlink im Rollbackjournal
                    # stehen: der Baum-Helper kann nach bereits wirksamen
                    # Teil-Unlinks noch mit einem Readbackfehler enden.
                    removed_program_trees.append(subtree)
                    remove_bound_regular_tree(
                        subtree,
                        max_file_bytes=_MAX_WEB_FILE_BYTES,
                        max_total_bytes=_MAX_WEB_TREE_BYTES,
                    )
                    if relative == "app":
                        removed_stale_app = True

                for relative in obsolete_files:
                    preimage = snapshot_bound_file(
                        os.path.join(web_root, relative),
                        max_bytes=_MAX_WEB_FILE_BYTES,
                    )
                    removal_record = {"preimage": preimage, "removed": None}
                    removed_program_files.append(removal_record)
                    removal_record["removed"] = remove_bound_file(
                        preimage,
                        max_bytes=_MAX_WEB_FILE_BYTES,
                    )

                for relative in desired_directories:
                    relative_parent = os.path.dirname(relative)
                    if relative_parent == ".":
                        relative_parent = ""
                    expected_parent_identity = (
                        web_root_identity
                        if not relative_parent
                        else target_directory_snapshots[relative_parent]["identity"]
                    )
                    existing_directory = target_directory_metadata.get(relative)
                    directory_snapshot = ensure_bound_directory(
                        os.path.join(web_root, relative),
                        uid=install_uid,
                        gid=www_gid,
                        mode=0o755,
                        expected_identity=(
                            tuple(existing_directory["identity"])
                            if existing_directory is not None
                            else None
                        ),
                        expected_parent_identity=tuple(expected_parent_identity),
                        expected_missing=existing_directory is None,
                    )
                    target_directory_snapshots[relative] = directory_snapshot
                    if directory_snapshot.get("created"):
                        created_directories.append(directory_snapshot)

                for relative in sorted(desired_payloads):
                    target_preimages[relative] = snapshot_bound_file(
                        os.path.join(web_root, relative),
                        allow_missing=True,
                        max_bytes=_MAX_WEB_FILE_BYTES,
                    )

                for relative in sorted(desired_payloads):
                    target_committed[relative] = atomic_write_bound_file(
                        os.path.join(web_root, relative),
                        desired_payloads[relative],
                        uid=install_uid,
                        gid=www_gid,
                        mode=0o644,
                        expected_snapshot=target_preimages[relative],
                        max_existing_bytes=_MAX_WEB_FILE_BYTES,
                        staging_root="/var/www",
                    )

                if not harden_web_program_permissions(
                    web_root=web_root,
                    install_user=authority["install_user"],
                    web_group="www-data",
                ):
                    raise RuntimeError("Web-Programmrechte sind nach der Projektion nicht sicher")
                _require_web_directory_contract(
                    web_root,
                    uid=0,
                    gid=www_gid,
                    mode=0o755,
                    expected_identity=web_root_identity,
                )
                _require_web_runtime_top_level_types(
                    web_root,
                    web_root_identity,
                )
                for relative in desired_directories:
                    _require_web_directory_contract(
                        os.path.join(web_root, relative),
                        uid=install_uid,
                        gid=www_gid,
                        mode=0o755,
                        expected_identity=target_directory_snapshots[relative][
                            "identity"
                        ],
                    )
                for relative, payload in sorted(desired_payloads.items()):
                    readback = read_bound_regular_file(
                        os.path.join(web_root, relative),
                        expected_uid=install_uid,
                        expected_gid=www_gid,
                        max_bytes=_MAX_WEB_FILE_BYTES,
                    )
                    if (
                        readback.get("sha256") != hashlib.sha256(payload).hexdigest()
                        or readback.get("mode") != 0o644
                        or not snapshots_match(
                            readback,
                            target_committed[relative],
                            # Das fd-relative Hardening bestätigt denselben
                            # Inode und dieselben Sollmetadaten, darf aber beim
                            # erneuten fchown/fchmod dessen ctime verändern.
                            exact_metadata=False,
                        )
                    ):
                        raise RuntimeError(f"Web-Datei-Readback weicht ab: {relative}")

                source_readback = snapshot_bound_regular_tree(
                    html_src,
                    expected_uid=install_uid,
                    require_owner_only_write=True,
                    max_file_bytes=_MAX_WEB_FILE_BYTES,
                    max_total_bytes=_MAX_WEB_TREE_BYTES,
                )
                if not tree_snapshots_match(
                    source_readback,
                    source_tree,
                    exact_identities=True,
                ):
                    raise RuntimeError("Webportal-Quelle driftete während der Publikation")
                for name, source_preimage in release_source_snapshots.items():
                    source_readback = read_bound_regular_file(
                        os.path.join(authority["install_path"], name),
                        expected_uid=install_uid,
                        max_bytes=_MAX_WEB_FILE_BYTES,
                    )
                    if not snapshots_match(
                        source_readback,
                        source_preimage,
                        exact_metadata=True,
                    ):
                        raise RuntimeError(
                            f"Release-Metadatum driftete während der Publikation: {name}"
                        )

                target_readback = snapshot_bound_regular_tree(
                    web_root,
                    exclude_top_level=_WEB_RUNTIME_TOP_LEVEL,
                    max_file_bytes=_MAX_WEB_FILE_BYTES,
                    max_total_bytes=_MAX_WEB_TREE_BYTES,
                )
                if not _web_program_tree_matches_desired(
                    target_readback,
                    desired_directories=desired_directory_set,
                    desired_payloads=desired_payloads,
                    install_uid=install_uid,
                    www_gid=www_gid,
                ):
                    raise RuntimeError(
                        "Web-Programmbaum entspricht nicht exakt der gebundenen Quelle"
                    )

                if not ensure_apache_runtime_path_protection(
                    run_command,
                    reload_apache=False,
                    allow_mutation=False,
                ):
                    raise RuntimeError("Apache-Laufzeitpfadschutz konnte nicht aktiviert werden")
                _start_apache_confirmed()
                if not apache_runtime_paths_protected():
                    raise RuntimeError(
                        "Apache-Laufzeitpfadschutz ist nach dem Neustart nicht wirksam"
                    )
            except Exception as exc:
                rollback_errors = []
                if publication_started:
                    try:
                        _stop_apache_confirmed()
                    except Exception as rollback_exc:
                        rollback_errors.append(f"Apache-Stop: {rollback_exc}")
                    if not rollback_errors:
                        for relative in reversed(sorted(target_preimages)):
                            try:
                                _restore_web_file(
                                    target_preimages[relative],
                                    target_committed.get(relative),
                                    desired_payloads[relative],
                                    uid=install_uid,
                                    gid=www_gid,
                                )
                            except Exception as rollback_exc:
                                rollback_errors.append(f"Datei {relative}: {rollback_exc}")
                        for directory_snapshot in reversed(created_directories):
                            try:
                                remove_bound_directory_if_empty(directory_snapshot)
                            except Exception as rollback_exc:
                                rollback_errors.append(
                                    f"Verzeichnis {directory_snapshot['path']}: {rollback_exc}"
                                )
                        for removal_record in reversed(removed_program_files):
                            try:
                                _restore_removed_web_file(
                                    removal_record["preimage"],
                                    removal_record["removed"],
                                )
                            except Exception as rollback_exc:
                                rollback_errors.append(
                                    "Entfernte Datei "
                                    f"{removal_record['preimage']['path']}: {rollback_exc}"
                                )
                        for subtree in reversed(removed_program_trees):
                            try:
                                restore_bound_regular_tree(
                                    subtree,
                                    staging_root="/var/www",
                                    max_file_bytes=_MAX_WEB_FILE_BYTES,
                                    max_total_bytes=_MAX_WEB_TREE_BYTES,
                                )
                            except Exception as rollback_exc:
                                rollback_errors.append(
                                    f"Entfernter Baum {subtree['path']}: {rollback_exc}"
                                )
                        try:
                            if not harden_web_program_permissions(
                                web_root=web_root,
                                install_user=authority["install_user"],
                                web_group="www-data",
                            ):
                                raise RuntimeError("Rollback-Hardening fehlgeschlagen")
                            _require_web_runtime_top_level_types(
                                web_root,
                                web_root_identity,
                            )
                        except Exception as rollback_exc:
                            rollback_errors.append(f"Web-Hardening: {rollback_exc}")

                if (
                    not rollback_errors
                    and apache_prestate is not None
                    and apache_state_mutation_started
                ):
                    try:
                        _restore_apache_state(apache_prestate)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"Apache-Prestate: {rollback_exc}")
                if rollback_errors:
                    try:
                        _stop_apache_confirmed()
                    except Exception:
                        pass
                    print("  [!] Webportal-Rollback unvollständig; Apache bleibt gestoppt:")
                    for rollback_error in rollback_errors:
                        print(f"      - {rollback_error}")
                elif publication_started:
                    print("  [OK] Webportal-Vorzustand wurde vollständig restauriert.")
                print(f"  [!] Webportal-Publikation fehlgeschlagen: {exc}")
                return False
    except Exception as exc:
        print(f"  [!] Webportal-Transaktionssperre ist nicht verfügbar: {exc}")
        return False

    print("  [OK] Web-Portal atomar publiziert und vollständig rückgelesen.")
    if removed_stale_app:
        print("  [OK] Experimentelle App-Vorschau sicher aus dem Webroot entfernt.")
    return True

def install_all_main(headless=False, *, bind_first_install_role=False):
    """Komplette Installation mit korrekter Reihenfolge."""
    install_user = get_install_user()
    install_path = get_install_path()
    config = load_config()

    print("\n" + "=" * 60)
    print("  ALLES INSTALLIEREN - E3DC-CONTROL KOMPLETT-SETUP")
    print("=" * 60 + "\n")

    print("Diese Installation führt folgende Schritte in dieser Reihenfolge durch:\n")
    print("  1. Systempakete installieren (Apache, PHP, Python, rsync etc.)")
    print("  2. Legacy-C++-Bestand prüfen (keine Neuinstallation)")
    print("  3. Webportal & Diagramm-System einrichten")
    print("  4. E3DC-Konfiguration & Wallbox-Datei erstellen")
    print("  5. Strompreise konfigurieren (optional)")
    print("  6. Kerndienst-Installation wird nach RAM-Disk und Backup in Schritt 10 gebündelt")
    print("  7. RAM-Disk einrichten")
    print("  8. Backup der Initialversion erstellen")
    print("  9. Live-, Markt-, Forecast-, Storage- und WebSocket-Kerndienste einrichten")
    print("  10. Energy Manager erst nach bestätigtem Kern-Livepfad einrichten")
    print("  11. Watchdog & Benachrichtigungs-Dienst installieren (Silent)")
    print("  12. Finale Prüfung & Einrichtung (Berechtigungen, Services etc.)\n")

    # Check auf vorhandene Config im Install-Ordner oder Legacy-Ordner
    bootstrap_user = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
    bootstrap_account = None
    try:
        bootstrap_account = pwd.getpwnam(bootstrap_user) if bootstrap_user else None
        bootstrap_home = get_home_dir(bootstrap_user) if bootstrap_account else ""
    except KeyError:
        bootstrap_home = ""
    possible_configs = (
        [
            os.path.join(bootstrap_home, "Install", "e3dc.config.txt"),
            os.path.join(bootstrap_home, "E3DC-Control", "e3dc.config.txt"),
        ]
        if bootstrap_home
        else []
    )
    possible_config = None
    possible_config_snapshot = None
    for p in possible_configs:
        if not os.path.lexists(p):
            continue
        try:
            possible_config_snapshot = _read_bound_legacy_config(
                p,
                expected_uid=bootstrap_account.pw_uid,
            )
        except (OSError, RuntimeError) as exc:
            print(f"✗ Unsichere vorhandene Konfiguration {p}: {exc}")
            return False
        possible_config = p
        break

    use_custom_config = False
    if possible_config:
        print(f"ℹ️  Gefunden: {possible_config}")
        if headless:
            use_custom_config = True
            print("  ✓ Wird im Headless-Modus automatisch verwendet.")
        else:
            if input("  Soll diese Konfigurationsdatei verwendet werden? (j/n): ").strip().lower() == 'j':
                use_custom_config = True
                print("  ✓ Wird in Schritt 4 integriert.")

    # Die Komplettinstallation ist an ein verifiziertes Benutzer-venv gebunden.
    # Eine System-Python-Auswahl darf keine Konfiguration und kein Paket verändern.
    print("\n" + "-" * 60)
    print("PYTHON UMGEBUNG")
    print("-" * 60)

    current_venv = str(config.get("venv_name") or ".venv_e3dc").strip()
    
    # Scan nach vorhandenen venvs
    possible_venvs = []
    if bootstrap_home and os.path.isdir(bootstrap_home):
        try:
            for item in os.listdir(bootstrap_home):
                if item.startswith(".venv") and os.path.isdir(os.path.join(bootstrap_home, item)):
                    possible_venvs.append(item)
        except OSError:
            possible_venvs = []

    venv_name = current_venv

    if headless:
        # Im Headless-Modus nutzen wir Standardwerte
        venv_name = ".venv_e3dc"
        print(f"→ Headless: Nutze Standard-Venv '{venv_name}'")
    else:
        if possible_venvs:
            print(f"Gefundene Umgebungen:")
            for i, v in enumerate(possible_venvs, 1):
                mark = " (aktuell)" if v == current_venv else ""
                print(f"  {i}) {v}{mark}")
            print(f"  n) Neue erstellen / Anderen Namen wählen")
            print("  x) Abbrechen (Komplettinstallation benötigt ein venv)")
            
            sel = input(f"Auswahl [1]: ").strip().lower()
            if not sel: sel = "1"
            
            if sel == 'x':
                print("✗ Komplettinstallation ohne venv ist nicht freigegeben.")
                return False
            elif sel == 'n':
                custom = input("Name für neues venv [.venv_e3dc]: ").strip()
                if custom: venv_name = custom
            elif sel.isdigit():
                idx = int(sel) - 1
                if 0 <= idx < len(possible_venvs):
                    venv_name = possible_venvs[idx]
        else:
            print("Die vollständige Installation benötigt eine isolierte Python-Umgebung.")
            sel = input("Soll das erforderliche Python venv angelegt werden? (j/n) [j]: ").strip().lower()
            if sel == 'n':
                print("✗ Komplettinstallation ohne venv ist nicht freigegeben.")
                return False
            else:
                custom = input("Name für venv [.venv_e3dc]: ").strip()
                if custom: venv_name = custom
                print(f"→ Installation erfolgt im venv ({venv_name}).")

    try:
        authority = _resolve_complete_install_authority(
            install_path,
            install_user,
            venv_name,
        )
    except Exception as exc:
        print(f"✗ Installationskontext ist nicht vertrauenswürdig: {exc}")
        return False



    if not headless:
        confirm = input("\nAlle Schritte ausführen? (j/n): ").strip().lower()
        if confirm != "j":
            print("→ Abgebrochen.\n")
            return False

    # Der direkte Konsolen-Menüpfad besitzt keinen Router-Parameter. Erst die
    # ausdrückliche Nutzerbestätigung darf deshalb die konservative
    # Installationsklassifikation laden und den festen Fresh-Anker `off`
    # autorisieren; ein frei wählbarer Web-Rollenwert ist niemals Quelle.
    if not bind_first_install_role:
        try:
            from .update import classify_installation_state

            installation_state, installation_detail = classify_installation_state()
        except Exception as exc:
            print(f"✗ Installationszustand ist nicht sicher klassifizierbar: {exc}")
            return False
        if installation_state == "fresh":
            bind_first_install_role = True
        elif installation_state == "blocked":
            print(f"✗ Vollinstallation ist im aktuellen Zustand gesperrt: {installation_detail}")
            return False

    # Erst nach der ausdrücklichen Bestätigung werden persistente
    # Installer- und Webmetadaten geschrieben.
    config["install_user"] = authority["install_user"]
    config["home_dir"] = authority["home_dir"]
    config["install_path"] = authority["install_path"]
    config["venv_name"] = authority["venv_name"]
    config["venv_path"] = authority["venv_path"]
    try:
        save_config(config)
    except Exception as exc:
        print(f"✗ Venv-Konfiguration konnte nicht sicher gespeichert werden: {exc}")
        return False
    # Cache-Bereinigung ist die erste nachgelagerte Dateisystemaktion.
    print("\n" + "=" * 60)
    print("  CACHE-BEREINIGUNG")
    print("=" * 60 + "\n")
    cleanup_pycache(authority["install_path"])

    # Logging für diese "Alles installieren"-Sitzung initialisieren
    setup_installation_loggers()
    failed_steps = []

    # =========================================================
    # SCHRITT 1: Systempakete
    # =========================================================
    print("\n" + "=" * 60)
    if not safe_execute_task("SCHRITT 1/12: Systempakete installieren", install_system_packages, use_venv=True):
        failed_steps.append("Systempakete")
        print("✗ Installation abgebrochen: Ohne vollständige Paket- und Apache-Basis werden keine Web-, Konfigurations- oder Dienstschritte ausgeführt.")
        print_installation_summary()
        return False

    try:
        from .utils import require_bound_venv_runtime
        from .ha_writer_admission import project_instance_role_anchor

        require_bound_venv_runtime(
            install_user=authority["install_user"],
            venv_path=authority["venv_path"],
        )
        if bind_first_install_role and project_instance_role_anchor(
            "off",
            peer_ip="",
        ) is not True:
            raise RuntimeError(
                "Der explizite Fresh-Rollenanker konnte nicht sicher projiziert werden"
            )
        web_config_ok = ensure_web_config(
            authority["install_user"],
            bind_first_install_role=bind_first_install_role,
            explicit_venv_name=authority["venv_name"],
            explicit_venv_path=authority["venv_path"],
            require_bound_venv=True,
        )
    except Exception as exc:
        print(f"✗ Das verpflichtende venv konnte nicht sicher projiziert werden: {exc}")
        print_installation_summary()
        return False
    if web_config_ok is not True:
        print("✗ Die V4-Startkonfiguration konnte nicht sicher angelegt werden.")
        print_installation_summary()
        return False

    # =========================================================
    # SCHRITT 2: E3DC-Control Binary (Legacy / optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 2/12: E3DC-Control C++ (Legacy - optional)")
    print("=" * 60)
    print("  [i] Das C++ Binary (Eba-M) wird nicht mehr benötigt.")
    print("       RSCP-Kommunikation erfolgt nativ über rscp_client.py")
    cpp_dir = os.path.join(authority["home_dir"], "E3DC-Control")
    if os.path.exists(cpp_dir):
        print(f"  [i] Legacy-Verzeichnis gefunden: {cpp_dir} (bleibt unverändert)")
    else:
        print("  [OK] Schritt übersprungen (Native-Python-Betrieb).\n")

    # =========================================================
    # SCHRITT 3: Web-Portal (PHP)
    # =========================================================
    print("\n" + "=" * 60)
    def install_webportal_and_restart_apache():
        return _publish_webportal_transaction(authority)
    if not safe_execute_task("SCHRITT 3/12: Webportal einrichten", install_webportal_and_restart_apache):
        failed_steps.append("Webportal")
        print("✗ Installation abgebrochen: Pflichtschritt Webportal fehlgeschlagen.")
        print_installation_summary()
        return False

    # =========================================================
    # SCHRITT 4: Konfiguration
    # =========================================================
    print("\n" + "=" * 60)
    def create_configs_task():
        if use_custom_config and possible_config and possible_config_snapshot:
            print(f"→ Kopiere vorhandene Konfiguration von {possible_config}...")
            try:
                target_config = os.path.join(authority["install_path"], "e3dc.config.txt")
                current_snapshot = _read_bound_legacy_config(
                    possible_config,
                    expected_uid=bootstrap_account.pw_uid,
                )
                if not snapshots_match(
                    current_snapshot,
                    possible_config_snapshot,
                    exact_metadata=True,
                ):
                    raise RuntimeError(
                        "Vorhandene Konfiguration änderte sich seit der Auswahl."
                    )
                uid, _ = get_user_ids(authority["install_user"])
                _project_bound_legacy_config(
                    current_snapshot,
                    target_config,
                    uid=uid,
                    gid=get_www_data_gid(),
                )
                log_task_completed("Konfiguration kopiert", details=possible_config)
                print("✓ Konfiguration erfolgreich migriert.")
            except Exception as e:
                log_error("install_all", f"Fehler beim Kopieren der Config: {e}", e)
                print(f"✗ Fehler beim Kopieren: {e}")
                return False
        else:
            if create_e3dc_config(
                headless=headless,
                install_path=authority["install_path"],
                install_user=authority["install_user"],
            ) is False:
                return False
            
        # Erstelle leere wallbox.txt nur im Legacy-Modus (wb_native_enable=0)
        # Im Native-Python-Modus wird wallbox.txt NICHT benötigt und darf von Python nicht beschrieben werden!
        # (Regel: Wallbox C++ Fallback ist im Native-Modus verboten)
        import json as _json
        try:
            _v4_snapshot = read_bound_regular_file(
                "/var/www/html/data/e3dc_v4.json",
                expected_uid=get_user_ids(authority["install_user"])[0],
                expected_gid=get_www_data_gid(),
                max_bytes=_MAX_LEGACY_CONFIG_BYTES,
            )
            _v4 = _json.loads(_v4_snapshot["payload"].decode("utf-8-sig"))
            if not isinstance(_v4, dict):
                raise ValueError("V4-Konfiguration ist kein JSON-Objekt")
            from .ha_writer_admission import (
                instance_role_anchor_matches,
            )

            configured_role = str(_v4.get("ha_mode") or "").strip().lower()
            configured_peer = str(_v4.get("ha_peer_ip") or "").strip()
            if bind_first_install_role:
                if configured_role != "off" or configured_peer:
                    raise RuntimeError(
                        "Fresh-Konfiguration widerspricht der festen Einzelanlagenrolle"
                    )
                role_ok = instance_role_anchor_matches("off", peer_ip="")
            else:
                role_ok = instance_role_anchor_matches(
                    configured_role,
                    peer_ip=configured_peer,
                )
            if role_ok is not True:
                raise RuntimeError(
                    "Root-privater Instanzrollen-Anker fehlt oder widerspricht der Konfiguration"
                )
        except Exception as exc:
            print(f"  [!] V4-Konfiguration ist nicht sicher lesbar: {exc}")
            return False
        wb_native = str(_v4.get('wb_native_enable', '1')) == '1'
        
        if not wb_native:
            wallbox_file = os.path.join(authority["install_path"], "e3dc.wallbox.txt")
            try:
                uid, _ = get_user_ids(authority["install_user"])
                _ensure_bound_legacy_wallbox_config(
                    wallbox_file,
                    uid=uid,
                    gid=get_www_data_gid(),
                )
                print(f"  [OK] Wallbox-Datei bereit (Legacy): {wallbox_file}")
            except Exception as e:
                print(f"  [!] Wallbox-Datei konnte nicht sicher erstellt werden: {e}")
                return False
        else:
            print("  [OK] Native Wallbox-Regelung aktiv -- wallbox.txt wird nicht erstellt.")
        return True

    if not safe_execute_task("SCHRITT 4/12: E3DC-Konfiguration erstellen", create_configs_task):
        failed_steps.append("Konfiguration")
        print("✗ Installation abgebrochen: Pflichtschritt Konfiguration fehlgeschlagen.")
        print_installation_summary()
        return False

    # =========================================================
    # SCHRITT 5: Strompreise (optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 5/12: Strompreise (optional)")
    print("=" * 60)
    choice = "n" if headless else input("Strompreise jetzt konfigurieren? (j/n): ").strip().lower()
    if choice == "j" or (headless and False): # Im Headless Modus Strompreise überspringen oder Default? Eher überspringen.
        try:
            strompreis_wizard(headless=headless)
            log_task_completed("Strompreise konfiguriert")
        except Exception as e:
            log_error("Strompreis-Wizard", f"Fehler bei der Strompreis-Konfiguration: {e}", e)
            failed_steps.append("Strompreise")
    else:
        print("→ Übersprungen (kann später hinzugefügt werden).\n")

    # =========================================================
    # SCHRITT 7: RAM-Disk
    # =========================================================
    if not safe_execute_task("SCHRITT 7/12: RAM-Disk einrichten", setup_ramdisk):
        failed_steps.append("RAM-Disk")
        print("✗ Installation abgebrochen: Pflichtschritt RAM-Disk fehlgeschlagen.")
        print_installation_summary()
        return False

    # =========================================================
    # SCHRITT 8: Backup
    # =========================================================
    def create_initial_backup_task():
        # backup_current_version() liefert den verifizierten Backup-Pfad und
        # bei Fehlern historisch None. Der Menü-Wrapper behandelt None aus
        # Kompatibilitätsgründen als Erfolg; hier gilt deshalb ein enger
        # expliziter Bool-Vertrag für diesen Pflichtschritt.
        return bool(backup_current_version())

    if not safe_execute_task(
        "SCHRITT 8/12: Backup der Initialversion erstellen",
        create_initial_backup_task,
    ):
        failed_steps.append("Backup")
        print("✗ Installation abgebrochen: Pflichtschritt Initialbackup fehlgeschlagen.")
        print_installation_summary()
        return False

    # =========================================================
    # SCHRITT 9: Pflicht-Kerndienste einschließlich WebSocket
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 9/12: Live-, Markt-, Forecast-, Storage- und WebSocket-Kerndienste")
    print("=" * 60)
    def install_core_services_task():
        # Der umfangreiche Markt-/Forecast-Import darf erst nach der
        # eingerichteten RAM-Disk und dem verifizierten Initialbackup erfolgen.
        from .epex_manager import install_epex_service

        # True wird erst nach dem gemeinsamen Unit-/Enable-/Active-Readback
        # des vollständigen Kernbundles geliefert.
        return install_epex_service(include_websocket=True)

    if not safe_execute_task("Kern-Manager Services installieren", install_core_services_task):
        failed_steps.append("Kern-Manager Services")
        print("✗ Installation abgebrochen: Pflichtschritt Kern-Manager Services fehlgeschlagen.")
        print_installation_summary()
        return False

    # =========================================================
    # SCHRITT 10: Energy Manager erst nach bewiesenem Kern-Livepfad
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 10/12: Energy Manager (Wärmepumpe & Smart Charging)")
    print("=" * 60)
    em_exists = os.path.lexists("/etc/systemd/system/energy_manager.service")
    choice_em = (
        "j" if em_exists else "n"
    ) if headless else input(
        "Energy Manager aktivieren (empfohlen)? (j/n) [j]: "
    ).strip().lower()
    if choice_em != "n":
        from .install_luxtronik import install_luxtronik_menu

        if not safe_execute_task(
            "Energy Manager einrichten",
            lambda: install_luxtronik_menu(
                headless=headless,
                explicit_install_path=authority["install_path"],
                explicit_install_user=authority["install_user"],
                explicit_venv_path=authority["venv_path"],
            ),
        ):
            failed_steps.append("Energy Manager")
            print("✗ Installation abgebrochen: ausgewählter Energy Manager fehlgeschlagen.")
            print_installation_summary()
            return False
    else:
        print("-> Übersprungen.\n")

    # =========================================================
    # SCHRITT 10b: Wallbox Manager
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 10b: Wallbox Manager (Native Python)")
    print("=" * 60)
    wb_exists = os.path.exists("/etc/systemd/system/e3dc-wallbox-manager.service")
    choice_wb = ("j" if wb_exists else "n") if headless else input("Native Wallbox Steuerung aktivieren? (j/n) [j]: ").strip().lower()
    if choice_wb != "n":
        from .install_native_wallbox import setup_wallbox_service
        if not safe_execute_task(
            "Wallbox Manager installieren",
            lambda: setup_wallbox_service(headless=headless),
        ):
            failed_steps.append("Wallbox Manager")
            print("✗ Installation abgebrochen: ausgewählter Wallbox Manager fehlgeschlagen.")
            print_installation_summary()
            return False
    else:
        print("-> Übersprungen.\n")

    # =========================================================
    # SCHRITT 11: Watchdog (Silent)
    # =========================================================
    from .install_watchdog import install_watchdog_silent
    if not safe_execute_task(
        "SCHRITT 11/12: Watchdog & Benachrichtigungs-Dienst installieren",
        lambda: install_watchdog_silent(
            explicit_install_path=authority["install_path"],
            explicit_install_user=authority["install_user"],
            explicit_home_dir=authority["home_dir"],
            explicit_venv_path=authority["venv_path"],
        ),
    ):
        failed_steps.append("Watchdog")
        print("✗ Installation abgebrochen: Watchdog oder Pflichtdienst Notifier fehlgeschlagen.")
        print_installation_summary()
        return False

    # Alle festen Drop-ins werden nach der Unit-Erzeugung nochmals gebunden.
    # Watchdog und Reparaturpfade gehören bewusst nicht zur Service-Positivliste.
    from .ramdisk_guard import require_ramdisk_service_dropins
    if not safe_execute_task(
        "SCHRITT 11b/12: tmpfs-Startsperren der Produktdienste prüfen",
        require_ramdisk_service_dropins,
    ):
        failed_steps.append("tmpfs-Startsperren")
        print("✗ Installation abgebrochen: tmpfs-Startsperren sind nicht vollständig.")
        print_installation_summary()
        return False

    # =========================================================
    # SCHRITT 12: FINALE PRÜFUNG & EINRICHTUNG
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 12/12: Finale Prüfung & Einrichtung (Berechtigungen, Services etc.)")
    print("=" * 60)
    try:
        print("\n→ Führe umfassende Prüfung und Einrichtung des Systems aus…\n")
        permissions_ok = run_permissions_wizard(headless=True)
        if permissions_ok is False:
            print("✗ Die finale Rechte- und Dienstprüfung meldet einen Fehler.\n")
            failed_steps.append("Finale Prüfung")
        else:
            log_task_completed(
                "Finale Prüfung & Einrichtung",
                details="run_permissions_wizard(headless=True) erfolgreich ausgeführt.",
            )
    except Exception as e:
        log_error("install_all", f"Fehler bei der finalen Prüfung: {e}", e)
        print(f"✗ Kritischer Fehler bei der finalen Prüfung und Einrichtung: {e}\n")
        failed_steps.append("Finale Prüfung")

    # Der Abschluss darf erst erfolgreich sein, wenn der reale Produktzustand
    # vollständig ist. Einzelne vorhandene Units oder Webdateien genügen nicht.
    try:
        from .update import classify_installation_state

        final_state, final_detail = classify_installation_state()
    except Exception as exc:
        final_state, final_detail = "blocked", f"Endzustand nicht prüfbar: {exc}"
    if final_state != "ready":
        failed_steps.append("Installations-Endzustand")
        print(f"✗ Installations-Endzustand ist nicht vollständig: {final_detail}\n")

    # =========================================================
    # Abschluss + Fehlersammlung
    # =========================================================
    print_installation_summary()

    ip_address = get_ip_address()

    print("Nächste Schritte:")
    print("  1. Webportal öffnen:")
    print(f"     → http://localhost oder http://{ip_address}\n")
    print("  2. System-Status prüfen:")
    print("     → Öffne das Webportal und navigiere zu 'Service Management', um alle Dienste zu sehen.\n")
    print("  3. Dokumentation:")
    print("     → Weitere Infos findest du im Ordner 'Install/doc' (z.B. zu Watchdog, Venv).\n")

    if failed_steps:
        unique_failed_steps = list(dict.fromkeys(failed_steps))
        print("✗ Installation nicht vollständig abgeschlossen.")
        print("  Fehlgeschlagene oder unvollständige Schritte: " + ", ".join(unique_failed_steps))
        return False
    return True


register_command("18", "Alles installieren - E3DC-Control Komplett-Setup (Empfohlen)", install_all_main, sort_order=10)
