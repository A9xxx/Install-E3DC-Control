import os
import json
import hashlib
import shlex

from .core import register_command
from .utils import (
    MANAGER_LOCK_TMPFILES_CONFIG,
    cleanup_pycache,
    ensure_manager_lock_namespace,
    format_command_failure,
    pip_install,
    require_bound_venv_runtime,
    resolve_venv_target,
    run_command,
)
from .installer_config import get_install_path, get_install_user, get_user_ids, get_www_data_gid
from .logging_manager import get_or_create_logger, log_task_completed, log_error
from .secure_file_transaction import (
    atomic_write_bound_file,
    ensure_bound_directory,
    exclusive_transaction_lock,
    open_bound_directory,
    read_bound_regular_file,
    remove_bound_file,
    render_assignment_updates,
    restore_bound_file,
    set_bound_file_metadata,
    snapshot_bound_file,
    snapshot_bound_regular_tree,
    snapshots_match,
)

LUX_SCRIPT_NAME = "energy_manager.py"
SERVICE_NAME = "energy_manager"
SYSTEMD_UNIT_ROOT = "/etc/systemd/system"
LEGACY_SERVICE_NAME = "wp-manager"
_MAX_CONFIG_BYTES = 4 * 1024 * 1024
_MAX_UNIT_BYTES = 1024 * 1024
_RESTORABLE_UNIT_FILE_STATES = frozenset(
    {
        "enabled",
        "enabled-runtime",
        "masked",
        "masked-runtime",
        "disabled",
        "static",
        "indirect",
        "generated",
        "alias",
        "not-found",
        "",
    }
)

luxtronik_logger = get_or_create_logger("luxtronik")


def _installer_dir(explicit_install_path=None):
    """Bindet Produktoperationen an den kanonischen Zielbaum, nicht den Runner."""

    module_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    requested_root = str(explicit_install_path or get_install_path())
    product_root = os.path.abspath(requested_root)
    if (
        not os.path.isabs(requested_root)
        or requested_root != product_root
        or product_root != module_root
        or os.path.realpath(product_root) != product_root
    ):
        raise RuntimeError("Energy Manager ist nicht an den laufenden Produktroot gebunden")
    return os.path.join(product_root, "Installer")


def _lux_dir(explicit_install_path=None):
    return os.path.join(_installer_dir(explicit_install_path), "luxtronik")


def _service_unit_exists(service_name):
    return any(
        os.path.isfile(os.path.join(unit_dir, f"{service_name}.service"))
        for unit_dir in (
            "/etc/systemd/system",
            "/run/systemd/system",
            "/lib/systemd/system",
            "/usr/lib/systemd/system",
        )
    )


def _query_service_state(service_name):
    result = run_command(
        "LC_ALL=C systemctl show --no-pager "
        "--property=LoadState --property=ActiveState --property=UnitFileState "
        + shlex.quote(service_name),
        timeout=15,
    )
    values = {}
    for line in str(result.get("stdout") or "").splitlines():
        key, separator, value = line.partition("=")
        if separator:
            values[key.strip()] = value.strip().lower()
    # systemd darf für eine noch nicht vorhandene Fresh-Unit nonzero liefern,
    # sofern es den Zustand dennoch ausdrücklich als not-found ausweist.
    if not result.get("success") and values.get("LoadState") != "not-found":
        failure_text = " ".join(
            str(result.get(key) or "") for key in ("stdout", "stderr")
        ).lower()
        if "could not be found" in failure_text and not _service_unit_exists(service_name):
            values = {
                "LoadState": "not-found",
                "ActiveState": "inactive",
                "UnitFileState": "not-found",
            }
        else:
            raise RuntimeError(
                f"Dienstzustand von {service_name} ist nicht beweisbar: "
                + format_command_failure(result)
            )
    if not {"LoadState", "ActiveState", "UnitFileState"}.issubset(values):
        raise RuntimeError(f"Dienstzustand von {service_name} ist unvollständig")
    load_state = values.get("LoadState") or "not-found"
    active_state = values.get("ActiveState") or "inactive"
    enabled_state = values.get("UnitFileState") or "not-found"
    if load_state not in {"loaded", "not-found", "masked"}:
        raise RuntimeError(f"Unklarer LoadState für {service_name}: {load_state}")
    if active_state not in {"active", "inactive", "failed"}:
        raise RuntimeError(f"Unklarer ActiveState für {service_name}: {active_state}")
    return {
        "load_state": load_state,
        "active_state": active_state,
        "was_active": active_state == "active",
        "enabled_state": enabled_state,
    }


def _stop_service_confirmed(service_name, state=None):
    current = state or _query_service_state(service_name)
    if current.get("load_state") == "not-found":
        return True
    stop = run_command(
        "sudo systemctl stop " + shlex.quote(service_name),
        timeout=30,
    )
    after = _query_service_state(service_name)
    if after.get("active_state") not in {"inactive", "failed"}:
        raise RuntimeError(
            f"{service_name} konnte nicht sicher gestoppt werden: "
            + format_command_failure(stop)
        )
    return True


def _set_service_enabled(service_name, enabled):
    command = "enable" if enabled else "disable"
    result = run_command(
        f"sudo systemctl {command} " + shlex.quote(service_name),
        timeout=30,
    )
    if not result.get("success"):
        raise RuntimeError(
            f"{service_name} konnte nicht auf {command} gesetzt werden: "
            + format_command_failure(result)
        )


def _restore_enabled_state(service_name, state):
    enabled_state = str(state.get("enabled_state") or "not-found")
    if enabled_state == "enabled":
        _set_service_enabled(service_name, True)
    elif enabled_state == "enabled-runtime":
        current = _query_service_state(service_name)
        if current.get("enabled_state") in {"enabled", "enabled-runtime"}:
            _set_service_enabled(service_name, False)
        result = run_command(
            "sudo systemctl enable --runtime " + shlex.quote(service_name),
            timeout=30,
        )
        if not result.get("success"):
            raise RuntimeError(f"Runtime-Enable von {service_name} konnte nicht restauriert werden")
    elif enabled_state == "masked":
        current = _query_service_state(service_name)
        if current.get("enabled_state") in {"enabled", "enabled-runtime"}:
            _set_service_enabled(service_name, False)
        result = run_command("sudo systemctl mask " + shlex.quote(service_name), timeout=30)
        if not result.get("success"):
            raise RuntimeError(f"Maskierung von {service_name} konnte nicht restauriert werden")
    elif enabled_state == "masked-runtime":
        current = _query_service_state(service_name)
        if current.get("enabled_state") in {"enabled", "enabled-runtime"}:
            _set_service_enabled(service_name, False)
        result = run_command(
            "sudo systemctl mask --runtime " + shlex.quote(service_name),
            timeout=30,
        )
        if not result.get("success"):
            raise RuntimeError(f"Runtime-Maskierung von {service_name} konnte nicht restauriert werden")
    elif enabled_state == "disabled":
        _set_service_enabled(service_name, False)
    elif enabled_state in {"static", "indirect", "generated", "alias", "not-found", ""}:
        pass
    else:
        raise RuntimeError(
            f"Enable-Prestate von {service_name} ist nicht restaurierbar: {enabled_state}"
        )
    restored_state = _query_service_state(service_name).get("enabled_state")
    if restored_state != enabled_state:
        raise RuntimeError(
            f"Enable-Prestate von {service_name} blieb abweichend: "
            f"{restored_state} statt {enabled_state}"
        )


def _start_service_confirmed(service_name):
    result = run_command(
        "sudo systemctl start " + shlex.quote(service_name),
        timeout=45,
    )
    if not result.get("success"):
        raise RuntimeError(
            f"{service_name} konnte nicht gestartet werden: "
            + format_command_failure(result)
        )
    if _query_service_state(service_name).get("active_state") != "active":
        raise RuntimeError(f"{service_name} ist nach dem Start nicht aktiv")


def _unit_path(service_name):
    return os.path.join(SYSTEMD_UNIT_ROOT, f"{service_name}.service")


def _snapshot_unit(service_name):
    return snapshot_bound_file(
        _unit_path(service_name),
        allow_missing=True,
        max_bytes=_MAX_UNIT_BYTES,
    )


def _reload_systemd():
    result = run_command("sudo systemctl daemon-reload", timeout=30)
    if not result.get("success"):
        raise RuntimeError(
            "systemd konnte die Energy-Manager-Units nicht neu laden: "
            + format_command_failure(result)
        )


def _disable_if_enabled(service_name, state):
    if str(state.get("enabled_state") or "") in {"enabled", "enabled-runtime"}:
        _set_service_enabled(service_name, False)
        after = _query_service_state(service_name).get("enabled_state")
        if after in {"enabled", "enabled-runtime"}:
            raise RuntimeError(
                f"{service_name} blieb trotz Disable dauerhaft aktiviert"
            )


def _snapshot_matches_desired(current, payload, uid, gid, mode):
    return bool(
        current.get("exists")
        and current.get("kind") == "regular"
        and current.get("sha256") == hashlib.sha256(payload).hexdigest()
        and current.get("uid") == int(uid)
        and current.get("gid") == int(gid)
        and current.get("mode") == int(mode)
    )


def _restore_changed_file(
    previous,
    committed,
    desired_payload,
    uid,
    gid,
    mode,
    *,
    desired_missing=False,
):
    """Restauriert nur unverändertes Preimage oder unseren exakten Commit."""

    current = snapshot_bound_file(
        str(previous["path"]),
        allow_missing=True,
        max_bytes=max(_MAX_CONFIG_BYTES, _MAX_UNIT_BYTES),
    )
    if snapshots_match(current, previous, exact_metadata=True):
        return
    restored = None
    if desired_missing and not current.get("exists"):
        restored = restore_bound_file(
            previous,
            expected_current=current,
            max_bytes=max(_MAX_CONFIG_BYTES, _MAX_UNIT_BYTES),
        )
    elif committed is not None and snapshots_match(
        current,
        committed,
        exact_metadata=False,
    ):
        restored = restore_bound_file(
            previous,
            expected_current=current,
            max_bytes=max(_MAX_CONFIG_BYTES, _MAX_UNIT_BYTES),
        )
    elif restored is None and desired_payload is not None and _snapshot_matches_desired(
        current,
        desired_payload,
        uid,
        gid,
        mode,
    ):
        restored = restore_bound_file(
            previous,
            expected_current=current,
            max_bytes=max(_MAX_CONFIG_BYTES, _MAX_UNIT_BYTES),
        )
    if restored is None:
        raise RuntimeError(f"Rollback-Ziel driftete fremd: {previous['path']}")
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
        raise RuntimeError(f"Rollback blieb unvollständig: {previous['path']}")

def install_dependencies(wp_type=0, *, install_user=None, explicit_venv_path=None):
    """Installiert Python-Abhängigkeiten für die gewählte Wärmepumpe."""
    print("\n→ Installiere Abhängigkeiten…")

    bound_user = get_install_user()
    if install_user and str(install_user) != bound_user:
        print("✗ Expliziter Benutzer widerspricht dem lokalen Rollenanker.")
        return False
    install_user = bound_user
    try:
        _venv_name, resolved_venv_path = resolve_venv_target(install_user)
        venv_path = str(explicit_venv_path or resolved_venv_path)
        if venv_path != resolved_venv_path:
            raise RuntimeError("Explizites venv widerspricht dem kanonischen Ziel")
        require_bound_venv_runtime(
            install_user=install_user,
            venv_path=venv_path,
        )
    except Exception as exc:
        print(f"✗ Das gebundene Benutzer-venv ist nicht verwendbar: {exc}")
        return False

    packages = ["requests"]
    if wp_type in (1, 2, 4, 5):
        packages.append("pymodbus")
    if wp_type == 0:
        packages.append("luxtronik")
    for package in packages:
        if pip_install(
            package,
            venv_path=venv_path,
            user=install_user,
            require_venv=True,
        ) is not True:
            print(f"✗ Python-Abhängigkeit konnte nicht im venv installiert werden: {package}")
            return False
    return True


def _lock_product_code_permissions(path, install_user, label):
    """Härtet einen gebundenen reinen Produktbaum ohne rekursive Pfadbefehle."""

    product_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    target = os.path.abspath(path)
    uid, gid = get_user_ids(install_user)
    try:
        if (
            os.path.realpath(product_root) != product_root
            or os.path.realpath(target) != target
            or os.path.commonpath((product_root, target)) != product_root
        ):
            raise RuntimeError("Produktcode liegt außerhalb des laufenden Release-Roots")
        before = snapshot_bound_regular_tree(
            target,
            expected_uid=uid,
            require_owner_only_write=True,
            max_file_bytes=_MAX_CONFIG_BYTES,
        )
        ensure_bound_directory(
            target,
            uid=uid,
            gid=gid,
            mode=0o755,
            expected_identity=tuple(before["root_identity"]),
        )
        for relative, metadata in sorted(
            dict(before["directories"]).items(),
            key=lambda item: (item[0].count(os.sep), item[0]),
        ):
            if metadata["gid"] != gid or metadata["mode"] != 0o755:
                ensure_bound_directory(
                    os.path.join(target, relative),
                    uid=uid,
                    gid=gid,
                    mode=0o755,
                    expected_identity=tuple(metadata["identity"]),
                )
        for relative, metadata in sorted(dict(before["files"]).items()):
            if metadata["gid"] != gid or metadata["mode"] != 0o644:
                set_bound_file_metadata(
                    os.path.join(target, relative),
                    uid=uid,
                    gid=gid,
                    mode=0o644,
                    expected_snapshot=metadata,
                    max_bytes=_MAX_CONFIG_BYTES,
                )

        after = snapshot_bound_regular_tree(
            target,
            expected_uid=uid,
            require_owner_only_write=True,
            max_file_bytes=_MAX_CONFIG_BYTES,
        )
        if (
            set(after["directories"]) != set(before["directories"])
            or set(after["files"]) != set(before["files"])
            or tuple(after["root_identity"] or ())[:2]
            != tuple(before["root_identity"] or ())[:2]
        ):
            raise RuntimeError("Produktcode-Baum driftete während der Härtung")
        for relative, previous in dict(before["directories"]).items():
            current = after["directories"][relative]
            if (
                tuple(current["identity"] or ())[:2]
                != tuple(previous["identity"] or ())[:2]
                or current["uid"] != uid
                or current["gid"] != gid
                or current["mode"] != 0o755
            ):
                raise RuntimeError(f"Verzeichnis-Endzustand weicht ab: {relative}")
        for relative, previous in dict(before["files"]).items():
            current = after["files"][relative]
            if (
                tuple(current["identity"] or ())[:2]
                != tuple(previous["identity"] or ())[:2]
                or current["sha256"] != previous["sha256"]
                or current["uid"] != uid
                or current["gid"] != gid
                or current["mode"] != 0o644
            ):
                raise RuntimeError(f"Datei-Endzustand weicht ab: {relative}")
        rebound_descriptor, rebound_identity = open_bound_directory(target)
        try:
            if tuple(rebound_identity)[:2] != tuple(after["root_identity"] or ())[:2]:
                raise RuntimeError("Produktcode-Root wechselte nach der Härtung")
        finally:
            os.close(rebound_descriptor)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"✗ Rechte-Endzustand für {label} ist nicht sicher: {exc}")
        return False
    return True


def _lock_product_script_permissions(path, install_user, label):
    """Bindet ein einzelnes Managerskript ohne rekursive Fremdflächen."""

    uid, gid = get_user_ids(install_user)
    try:
        before = read_bound_regular_file(
            os.path.abspath(path),
            expected_uid=uid,
            max_bytes=_MAX_CONFIG_BYTES,
        )
        if int(before["mode"]) & 0o022:
            raise RuntimeError("Managerskript ist gruppen- oder weltbeschreibbar")
        after = set_bound_file_metadata(
            os.path.abspath(path),
            uid=uid,
            gid=gid,
            mode=0o644,
            expected_snapshot=before,
            max_bytes=_MAX_CONFIG_BYTES,
        )
        if (
            tuple(after.get("identity") or ())[:2]
            != tuple(before.get("identity") or ())[:2]
            or after.get("sha256") != before.get("sha256")
            or after.get("uid") != uid
            or after.get("gid") != gid
            or after.get("mode") != 0o644
        ):
            raise RuntimeError("Managerskript-Endzustand weicht vom Preimage ab")
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"✗ Rechte-Endzustand für {label} ist nicht sicher: {exc}")
        return False
    return True


def setup_script(wp_type=0, *, install_user=None, explicit_install_path=None):
    """Setzt Berechtigungen für die Skripte."""
    print(f"\n→ Setze Berechtigungen…")

    bound_user = get_install_user()
    if install_user and str(install_user) != bound_user:
        print("✗ Expliziter Benutzer widerspricht dem lokalen Rollenanker.")
        return False
    install_user = bound_user

    # Laufzeitzustände liegen getrennt unter /var/www/html/{data,ramdisk,logs}.
    # Der Produktbaum selbst bleibt für die Webserver-Gruppe strikt schreibgeschützt.
    lux_dir = _lux_dir(explicit_install_path)
    script_path = os.path.join(lux_dir, LUX_SCRIPT_NAME)
    if not os.path.isdir(lux_dir) or not os.path.isfile(script_path):
        print(f"✗ Energy-Manager-Skript fehlt: {script_path}")
        return False
    if not _lock_product_code_permissions(lux_dir, install_user, "Energy Manager"):
        return False

    if wp_type == 2:
        heater_script = os.path.join(
            _installer_dir(explicit_install_path),
            "heizstab_manager.py",
        )
        if not _lock_product_script_permissions(
            heater_script,
            install_user,
            "Heizstab-Manager",
        ):
            return False

    # 2. IDM falls gewählt
    for selected_type, driver_dir in ((1, "idm"), (4, "stiebel"), (5, "dimplex")):
        if wp_type == selected_type:
            vendor_dir = os.path.join(_installer_dir(explicit_install_path), driver_dir)
            if not os.path.isdir(vendor_dir):
                print(f"✗ Treiberverzeichnis fehlt: {vendor_dir}")
                return False
            if not _lock_product_code_permissions(
                vendor_dir,
                install_user,
                f"Treiber {driver_dir}",
            ):
                return False

    print(f"✓ Berechtigungen gesetzt.")
    return True

def configure_luxtronik(wp_type=0, headless=False, *, explicit_install_path=None):
    """Erfasst die Sollwerte; geschrieben werden sie erst in der Unit-Transaktion."""
    print("\n=== Energy Manager (Wärmepumpe & Lademanagement) ===\n")

    if wp_type not in {-1, 0, 1, 2, 4, 5}:
        print(f"✗ Nicht unterstützter Wärmepumpentyp: {wp_type}")
        return False

    print("HINWEIS: Die Konfiguration erfolgt nun zentral im Web-Interface")
    print("unter 'Config Editor' (Gruppe: Luxtronik Energy Manager / Smart Grid).")
    print("Dieses Setup richtet den zentralen Hintergrunddienst ein.\n")

    config_file = os.path.join(
        os.path.dirname(_installer_dir(explicit_install_path)),
        "e3dc.config.txt",
    )
    if not os.path.lexists(config_file):
        print(f"✗ Anlagenkonfiguration fehlt: {config_file}")
        return False

    replacements = {
        "wp_type": f"wp_type = {wp_type}",
        "auto_mode": "auto_mode = 1",
    }
    if not headless:
        if wp_type == 1:
            idm_ip = input("\nWie lautet die IP-Adresse deiner IDM Wärmepumpe?: ").strip()
            if idm_ip:
                replacements["idm_ip"] = f"idm_ip = {idm_ip}"
        elif wp_type == 4:
            stiebel_ip = input("\nWie lautet die IP-Adresse deines Stiebel Eltron ISG?: ").strip()
            if stiebel_ip:
                replacements["stiebel_isg_ip"] = f"stiebel_isg_ip = {stiebel_ip}"
        elif wp_type == 5:
            dimplex_ip = input("\nWie lautet die IP-Adresse deiner Dimplex WPM Touch / NWPM?: ").strip()
            if dimplex_ip:
                replacements["dimplex_ip"] = f"dimplex_ip = {dimplex_ip}"
        elif wp_type == 2:
            hs_ip = input("\nIP-Adresse des Heizstabs (Modbus-TCP) [0.0.0.0]: ").strip() or "0.0.0.0"
            sh_ip = input("IP-Adresse des Shelly-Heizlüfters HTTP [0.0.0.0]: ").strip() or "0.0.0.0"
            replacements["heizstab_ip"] = f"heizstab_ip = {hs_ip}"
            replacements["shelly_heiz_ip"] = f"shelly_heiz_ip = {sh_ip}"

    if not headless:
        input("Drücke Enter um fortzufahren...")
    return replacements

def cleanup_old_service():
    """Kompatibilitätshinweis: Legacy-Bereinigung ist Teil von setup_service()."""

    print("→ Legacy-Dienstbereinigung wird gemeinsam mit Config und Units ausgeführt.")
    return True

def setup_service(
    wp_type=0,
    config_updates=None,
    *,
    explicit_install_path=None,
    explicit_install_user=None,
    explicit_venv_path=None,
):
    """Projiziert Config, Units und Dienstzustände als eine Transaktion."""

    print("\n→ Richte Services ein…")
    if wp_type not in {-1, 0, 1, 2, 4, 5}:
        print(f"✗ Nicht unterstützter Wärmepumpentyp: {wp_type}")
        return False

    install_user = get_install_user()
    if explicit_install_user and str(explicit_install_user) != install_user:
        print("✗ Expliziter Benutzer widerspricht dem lokalen Rollenanker.")
        return False
    install_uid, _install_gid = get_user_ids(install_user)
    www_gid = get_www_data_gid()
    installer_dir = _installer_dir(explicit_install_path)
    try:
        _venv_name, resolved_venv_path = resolve_venv_target(install_user)
        venv_path = str(explicit_venv_path or resolved_venv_path)
        if venv_path != resolved_venv_path:
            raise RuntimeError("Explizites venv widerspricht dem kanonischen Ziel")
        python_bin = require_bound_venv_runtime(
            install_user=install_user,
            venv_path=venv_path,
        )["python"]
    except Exception as exc:
        print(f"✗ Gebundener venv-Laufzeitvertrag fehlt: {exc}")
        return False

    live_services = {
        0: (
            "e3dc-lux-live",
            "Luxtronik WebSocket Live Daemon",
            os.path.join(installer_dir, "luxtronik", "lux_live.py"),
        ),
        1: (
            "e3dc-idm-live",
            "IDM Modbus Live Daemon",
            os.path.join(installer_dir, "idm", "idm_live.py"),
        ),
        4: (
            "e3dc-stiebel-live",
            "Stiebel ISG Live Daemon",
            os.path.join(installer_dir, "stiebel", "stiebel_live.py"),
        ),
        5: (
            "e3dc-dimplex-live",
            "Dimplex WPM Touch Live Daemon",
            os.path.join(installer_dir, "dimplex", "dimplex_live.py"),
        ),
    }
    all_live_names = tuple(item[0] for item in live_services.values())
    manager_script = os.path.join(installer_dir, "luxtronik", "energy_manager.py")
    if wp_type == 2:
        service_specs = [
            (
                "e3dc-heizstab",
                "E3DC Heizstab / Shelly Manager",
                os.path.join(installer_dir, "heizstab_manager.py"),
                30,
                (),
            )
        ]
        conflicting_services = (SERVICE_NAME, *all_live_names)
    elif wp_type < 0:
        service_specs = [
            (
                SERVICE_NAME,
                "E3DC Energy Manager (Smart Charging)",
                manager_script,
                30,
                (),
            )
        ]
        conflicting_services = (*all_live_names, "e3dc-heizstab")
    else:
        live_name, live_description, live_script = live_services[wp_type]
        service_specs = [
            (live_name, live_description, live_script, 10, ()),
            (
                SERVICE_NAME,
                "E3DC Energy Manager (Heatpump & Wallbox)",
                manager_script,
                30,
                (f"{live_name}.service",),
            ),
        ]
        conflicting_services = tuple(
            name for name in (*all_live_names, "e3dc-heizstab") if name != live_name
        )

    if not install_user or any(
        character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
        for character in install_user
    ):
        print("✗ Installationsbenutzer ist für eine systemd-Unit nicht eindeutig.")
        return False
    for executable_path in (python_bin, *(spec[2] for spec in service_specs)):
        if (
            not os.path.isabs(executable_path)
            or os.path.normpath(executable_path) != executable_path
            or any(character.isspace() for character in executable_path)
            or not os.path.isfile(executable_path)
            or os.path.islink(executable_path)
        ):
            print(f"✗ Pflichtpfad ist nicht eindeutig systemd-tauglich: {executable_path}")
            return False

    replacements = dict(config_updates or {})
    replacements.setdefault("wp_type", f"wp_type = {wp_type}")
    replacements.setdefault("auto_mode", "auto_mode = 1")
    for key, replacement in replacements.items():
        if (
            not key
            or any(character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_" for character in key)
            or "\n" in replacement
            or "\r" in replacement
            or not (
                replacement.startswith(key + " ")
                or replacement.startswith(key + "=")
            )
        ):
            print(f"✗ Ungültige Konfigurationsprojektion für {key!r}.")
            return False

    unit_payloads = {}
    for service_name, description, script_path, restart_sec, after_units in service_specs:
        after = " ".join(("network.target", *after_units))
        manager_lock_prestart = (
            "ExecStartPre=+/usr/bin/systemd-tmpfiles --create "
            f"{MANAGER_LOCK_TMPFILES_CONFIG}\n"
            if service_name in {SERVICE_NAME, "e3dc-heizstab"}
            else ""
        )
        unit_payloads[service_name] = f"""[Unit]
Description={description}
After={after}

[Service]
Type=simple
User={install_user}
Group=www-data
WorkingDirectory={os.path.dirname(script_path)}
{manager_lock_prestart}ExecStart={python_bin} {script_path}
Restart=always
RestartSec={restart_sec}

[Install]
WantedBy=multi-user.target
""".encode("utf-8")

    # Energy Manager und Heizstab teilen denselben rebootfesten Owner-Lockraum
    # wie die zentral installierten Manager. Erst nach allen lokalen
    # Eingabe-/Unitprüfungen, aber vor jeder Unit-/Dienstmutation projizieren.
    if ensure_manager_lock_namespace() is not True:
        print("✗ Manager-Locknamespace konnte nicht sicher eingerichtet werden.")
        return False

    selected_names = tuple(spec[0] for spec in service_specs)
    decision_writers = (SERVICE_NAME, "e3dc-heizstab", LEGACY_SERVICE_NAME)
    all_names = tuple(
        dict.fromkeys((*selected_names, *conflicting_services, LEGACY_SERVICE_NAME))
    )
    quiesce_order = tuple(
        name for name in all_names if name in decision_writers
    ) + tuple(name for name in all_names if name not in decision_writers)
    restore_start_order = tuple(
        name for name in all_names if name not in decision_writers
    ) + tuple(name for name in all_names if name in decision_writers)

    config_path = os.path.join(
        os.path.dirname(installer_dir),
        "e3dc.config.txt",
    )
    config_preimage = None
    config_committed = None
    config_payload = None
    unit_preimages = {}
    unit_committed = {}
    service_prestates = {}
    legacy_committed = None
    legacy_removal_started = False
    transaction_started = False

    try:
        with exclusive_transaction_lock("e3dc-product-config.lock"):
            try:
                config_preimage = snapshot_bound_file(
                    config_path,
                    expected_uid=install_uid,
                    max_bytes=_MAX_CONFIG_BYTES,
                )
                if int(config_preimage.get("mode") or 0) & 0o022:
                    raise RuntimeError(
                        "Anlagenkonfiguration ist gruppen- oder weltbeschreibbar"
                    )
                config_payload = render_assignment_updates(
                    config_preimage["payload"],
                    replacements,
                )
                service_prestates = {
                    name: _query_service_state(name)
                    for name in all_names
                }
                failed_prestates = sorted(
                    name
                    for name, state in service_prestates.items()
                    if state.get("active_state") == "failed"
                )
                if failed_prestates:
                    raise RuntimeError(
                        "Failed-Prestates sind nicht exakt restaurierbar: "
                        + ", ".join(failed_prestates)
                    )
                unrestorable = {
                    name: service_prestates[name]["enabled_state"]
                    for name in all_names
                    if service_prestates[name]["enabled_state"]
                    not in _RESTORABLE_UNIT_FILE_STATES
                }
                if unrestorable:
                    raise RuntimeError(
                        "Nicht restaurierbare UnitFileStates: "
                        + ", ".join(
                            f"{name}={state}"
                            for name, state in sorted(unrestorable.items())
                        )
                    )
                if any(
                    service_prestates[name]["enabled_state"] in {"masked", "masked-runtime"}
                    for name in selected_names
                ):
                    raise RuntimeError("Ein ausgewählter Dienst ist explizit maskiert")
                unit_preimages = {
                    name: _snapshot_unit(name)
                    for name in (*selected_names, LEGACY_SERVICE_NAME)
                }
                transaction_started = True

                # Zuerst alle bisherigen Entscheider, anschließend deren Live-Quellen.
                for name in quiesce_order:
                    _stop_service_confirmed(name, service_prestates[name])

                # Konkurrierende Entscheider werden deaktiviert, solange ihre
                # bisherigen Units noch vollständig geladen und restaurierbar
                # sind. Erst danach folgen Datei- und daemon-reload-Commit.
                for name in conflicting_services:
                    _disable_if_enabled(name, service_prestates[name])
                _disable_if_enabled(
                    LEGACY_SERVICE_NAME,
                    service_prestates[LEGACY_SERVICE_NAME],
                )

                config_committed = atomic_write_bound_file(
                    config_path,
                    config_payload,
                    uid=install_uid,
                    gid=www_gid,
                    mode=0o640,
                    expected_snapshot=config_preimage,
                )
                for name in selected_names:
                    unit_committed[name] = atomic_write_bound_file(
                        _unit_path(name),
                        unit_payloads[name],
                        uid=0,
                        gid=0,
                        mode=0o644,
                        expected_snapshot=unit_preimages[name],
                        staging_root=SYSTEMD_UNIT_ROOT,
                    )
                legacy_preimage = unit_preimages[LEGACY_SERVICE_NAME]
                if legacy_preimage.get("exists"):
                    legacy_removal_started = True
                    legacy_committed = remove_bound_file(
                        legacy_preimage,
                        max_bytes=_MAX_UNIT_BYTES,
                    )

                _reload_systemd()
                for name in selected_names:
                    _set_service_enabled(name, True)
                    if _query_service_state(name).get("enabled_state") != "enabled":
                        raise RuntimeError(f"{name} ist nach Enable nicht dauerhaft aktiviert")

                # Die Live-Quelle wird immer vor ihrem fachlichen Entscheider gestartet.
                for name in selected_names:
                    _start_service_confirmed(name)

                # Erst der gemeinsame Endreadback schließt die Transaktion:
                # alle gewählten Units müssen gleichzeitig live und dauerhaft
                # aktiviert sein, während kein alter Entscheider wieder anlief.
                for name in selected_names:
                    final_state = _query_service_state(name)
                    if (
                        final_state.get("active_state") != "active"
                        or final_state.get("enabled_state") != "enabled"
                    ):
                        raise RuntimeError(
                            f"{name} verletzt den gemeinsamen Enable-/Active-Endzustand"
                        )
                for name in dict.fromkeys((*conflicting_services, LEGACY_SERVICE_NAME)):
                    final_state = _query_service_state(name)
                    if (
                        final_state.get("active_state") == "active"
                        or final_state.get("enabled_state")
                        in {"enabled", "enabled-runtime"}
                    ):
                        raise RuntimeError(
                            f"Konkurrierender Dienst {name} ist im Endzustand nicht quieszent"
                        )
            except Exception as exc:
                rollback_errors = []
                if transaction_started:
                    for name in quiesce_order:
                        try:
                            _stop_service_confirmed(name)
                        except Exception as rollback_exc:
                            rollback_errors.append(f"Stop {name}: {rollback_exc}")
                    decision_stopped = True
                    for name in decision_writers:
                        if name not in service_prestates:
                            continue
                        try:
                            if _query_service_state(name).get("active_state") == "active":
                                decision_stopped = False
                        except Exception as rollback_exc:
                            decision_stopped = False
                            rollback_errors.append(f"Stop-Readback {name}: {rollback_exc}")

                    if decision_stopped:
                        for name in selected_names:
                            try:
                                current_state = _query_service_state(name)
                                _disable_if_enabled(name, current_state)
                            except Exception as rollback_exc:
                                rollback_errors.append(f"Disable {name}: {rollback_exc}")
                        try:
                            _restore_changed_file(
                                config_preimage,
                                config_committed,
                                config_payload,
                                install_uid,
                                www_gid,
                                0o640,
                            )
                        except Exception as rollback_exc:
                            rollback_errors.append(f"Config: {rollback_exc}")
                        for name in reversed(selected_names):
                            try:
                                _restore_changed_file(
                                    unit_preimages[name],
                                    unit_committed.get(name),
                                    unit_payloads[name],
                                    0,
                                    0,
                                    0o644,
                                )
                            except Exception as rollback_exc:
                                rollback_errors.append(f"Unit {name}: {rollback_exc}")
                        try:
                            _restore_changed_file(
                                unit_preimages[LEGACY_SERVICE_NAME],
                                legacy_committed,
                                None,
                                0,
                                0,
                                0o644,
                                desired_missing=legacy_removal_started,
                            )
                        except Exception as rollback_exc:
                            rollback_errors.append(f"Legacy-Unit: {rollback_exc}")
                        try:
                            _reload_systemd()
                        except Exception as rollback_exc:
                            rollback_errors.append(f"Daemon-Reload: {rollback_exc}")

                        if not rollback_errors:
                            for name in all_names:
                                try:
                                    _restore_enabled_state(name, service_prestates[name])
                                except Exception as rollback_exc:
                                    rollback_errors.append(f"Enable-State {name}: {rollback_exc}")
                            if not rollback_errors:
                                for name in restore_start_order:
                                    if not service_prestates[name].get("was_active"):
                                        continue
                                    # Kein fachlicher Entscheider darf anlaufen,
                                    # wenn eine seiner zuvor aktiven Live-Quellen
                                    # oder ein früherer Entscheider nicht sauber
                                    # restauriert werden konnte.
                                    if name in decision_writers and rollback_errors:
                                        continue
                                    try:
                                        _start_service_confirmed(name)
                                    except Exception as rollback_exc:
                                        rollback_errors.append(f"Start {name}: {rollback_exc}")
                    else:
                        rollback_errors.append(
                            "Entscheider konnten vor dem Dateirücklauf nicht sicher gestoppt werden"
                        )

                    if rollback_errors:
                        for name in decision_writers:
                            if name not in service_prestates:
                                continue
                            try:
                                _stop_service_confirmed(name)
                            except Exception:
                                pass

                print(f"✗ Energy-Manager-Transaktion fehlgeschlagen: {exc}")
                if rollback_errors:
                    print("✗ Rollback unvollständig; alle erreichbaren Entscheider bleiben gestoppt:")
                    for rollback_error in rollback_errors:
                        print(f"  - {rollback_error}")
                elif transaction_started:
                    print("✓ Vorzustand von Config, Units und Diensten wurde restauriert.")
                return False
    except Exception as exc:
        print(f"✗ Energy-Manager-Transaktionssperre fehlgeschlagen: {exc}")
        return False

    installed = ", ".join(selected_names)
    print(f"✓ Energy-Manager-Konfiguration und Dienste sind atomar aktiv: {installed}")
    return True

def install_luxtronik_menu(
    headless=False,
    *,
    explicit_install_path=None,
    explicit_install_user=None,
    explicit_venv_path=None,
):
    print("\n=== Wärmepumpen & Energy Manager Setup ===\n")

    try:
        bound_user = get_install_user()
        if explicit_install_user and str(explicit_install_user) != bound_user:
            raise RuntimeError(
                "Expliziter Benutzer widerspricht dem lokalen Rollenanker"
            )
        bound_root = os.path.dirname(_installer_dir(explicit_install_path))
        _venv_name, resolved_venv_path = resolve_venv_target(bound_user)
        bound_venv_path = str(explicit_venv_path or resolved_venv_path)
        if bound_venv_path != resolved_venv_path:
            raise RuntimeError("Explizites venv widerspricht dem kanonischen Ziel")
        require_bound_venv_runtime(
            install_user=bound_user,
            venv_path=bound_venv_path,
        )
    except Exception as exc:
        print(f"✗ Energy-Manager-Kontext ist nicht vertrauenswürdig: {exc}")
        return False
    
    wp_type = -1
    if headless:
        try:
            snapshot = read_bound_regular_file(
                "/var/www/html/data/e3dc_v4.json",
                expected_uid=get_user_ids(bound_user)[0],
                expected_gid=get_www_data_gid(),
                max_bytes=_MAX_CONFIG_BYTES,
            )
            v4 = json.loads(snapshot["payload"].decode("utf-8-sig"))
            if not isinstance(v4, dict) or "wp_type" not in v4:
                raise ValueError("wp_type fehlt in der V4-Konfiguration")
            wp_type = int(v4.get("wp_type", -1))
        except Exception as exc:
            print(f"✗ Wärmepumpentyp ist im Headless-Modus nicht sicher lesbar: {exc}")
            return False
        print(f"→ Headless-Modus: wp_type = {wp_type} aus Konfiguration gelesen.")
    else:
        print("Welche Wärmepumpe möchtest du anbinden?")
        print("-1) Keine Wärmepumpe (nur Smart Charging / Wallbox)")
        print("0) Luxtronik 2.0 (Alpha Innotec, Novelan, etc.) via WebSocket")
        print("1) IDM (AERO, TERRA) via Modbus-TCP")
        print("2) Heizstab / Shelly Manager (Modbus-TCP & Shelly Plug)")
        print("4) Stiebel Eltron ISG / WPM via Modbus-TCP (read-only live)")
        print("5) Dimplex WPM Touch / NWPM via Modbus-TCP")
        wp_choice = input("\nAuswahl (Standard -1): ")
        if wp_choice == "-1": wp_type = -1
        elif wp_choice == "0": wp_type = 0
        elif wp_choice == "1": wp_type = 1
        elif wp_choice == "2": wp_type = 2
        elif wp_choice == "4": wp_type = 4
        elif wp_choice == "5": wp_type = 5

    if wp_type not in {-1, 0, 1, 2, 4, 5}:
        print(f"✗ Ungültige Wärmepumpenauswahl: {wp_type}")
        return False

    # Cache-Bereinigung
    cleanup_pycache(_lux_dir(bound_root))

    for action, label in (
        (
            lambda: install_dependencies(
                wp_type,
                install_user=bound_user,
                explicit_venv_path=bound_venv_path,
            ),
            "Python-Abhängigkeiten",
        ),
        (
            lambda: setup_script(
                wp_type,
                install_user=bound_user,
                explicit_install_path=bound_root,
            ),
            "Skriptrechte",
        ),
    ):
        if action() is not True:
            print(f"✗ Energy-Manager-Installation abgebrochen: {label} fehlgeschlagen.")
            return False

    config_updates = configure_luxtronik(
        wp_type,
        headless=headless,
        explicit_install_path=bound_root,
    )
    if config_updates is False:
        print("✗ Energy-Manager-Installation abgebrochen: Konfiguration fehlgeschlagen.")
        return False
    if setup_service(
        wp_type,
        config_updates=config_updates,
        explicit_install_path=bound_root,
        explicit_install_user=bound_user,
        explicit_venv_path=bound_venv_path,
    ) is not True:
        print("✗ Energy-Manager-Installation abgebrochen: Transaktion fehlgeschlagen.")
        return False

    if wp_type == 4:
        label = "Stiebel"
    elif wp_type == 5:
        label = "Dimplex"
    elif wp_type == 1:
        label = "IDM"
    elif wp_type == 2:
        label = "Heizstab/Shelly"
    elif wp_type == -1:
        label = "Smart Charging"
    else:
        label = "Luxtronik"
    log_task_completed(f"Energy Manager Installation ({label})")
    return True

register_command("41", "Energy Manager (Luxtronik/IDM Wärmepumpe & Lademanagement)", install_luxtronik_menu, sort_order=41)
