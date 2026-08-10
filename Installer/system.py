import os
import shlex
import shutil
import subprocess

from .utils import (
    command_as_user,
    command_exists,
    format_command_failure,
    pip_install,
    resolve_venv_target,
    run_command,
)
from .installer_config import get_install_path, get_install_user, get_home_dir, load_config, get_venv_path, get_venv_name, get_venv_pip
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH = get_install_path()
system_logger = get_or_create_logger("system")

# V4 Python-Pakete (immer benoetigt)
PYTHON_PACKAGES = [
    "paho-mqtt",            # MQTT (wallbox_manager, mqtt_hub)
    "requests",             # HTTP (bluelink, APIs)
    "websocket-client",     # Luxtronik WebSocket (lux_live.py)
    "websockets",           # e3dc_websocket.py server
    "pymodbus",             # Modbus-TCP (energy_manager, idm_live)
    "hyundai_kia_connect_api", # Bluelink / Fahrzeug SoC
    "pywebpush",            # Web-Push Notifications (braucht rustc/cargo!)
    "py3rijndael"           # RSCP AES Verschluesselung (rscp_client.py)
]

def install_rscpgui():
    # Veraltet: Wird nicht mehr benötigt, da vital_stats.py nun den nativen rscp_client nutzt.
    # Die Funktion bleibt leer, um Fehler zu vermeiden, falls sie noch irgendwo aufgerufen wird.
    pass

def setup_venv(show_header=False):
    """Richtet das Python Virtual Environment ein."""
    if show_header:
        print("\n=== Python Virtual Environment einrichten ===\n")

    install_user = get_install_user()
    venv_name, venv_path = resolve_venv_target(install_user)

    # Altes venv im E3DC-Control Ordner bereinigen (Migration)
    old_venv_path = os.path.join(INSTALL_PATH, venv_name)
    if os.path.exists(old_venv_path) and os.path.isdir(old_venv_path):
        if os.path.abspath(old_venv_path) != os.path.abspath(venv_path):
            print(f"→ Entferne veraltetes venv: {old_venv_path}")
            try:
                shutil.rmtree(old_venv_path)
                print("✓ Altes venv bereinigt.")
            except Exception as e:
                print(f"⚠ Konnte altes venv nicht löschen: {e}")

    print(f"→ Ziel: {venv_path}")

    if not os.path.exists(venv_path):
        print("→ Erstelle venv…")
        # Erstelle venv mit Zugriff auf System-Pakete (für apt-installierte Module wie RPi.GPIO falls nötig)
        create_cmd = command_as_user(
            f"python3 -m venv {shlex.quote(venv_path)} --system-site-packages",
            install_user,
        )
        res = run_command(create_cmd, timeout=60)
        if res['success']:
            print("✓ venv erstellt.")
            system_logger.info(f"Virtual Environment erstellt: {venv_path}")
        else:
            print(f"✗ Fehler beim Erstellen: {format_command_failure(res)}")
            return False
    else:
        print("✓ venv existiert bereits.")

    venv_pip = get_venv_pip(install_user)

    # Nutze zentrale Installationsfunktion
    install_python_packages()

    if show_header:
        print("\n✓ Python-Umgebung eingerichtet.\n")
        log_task_completed("Python venv eingerichtet")
    return True

def list_venv_packages():
    """Listet installierte Pakete im venv auf."""
    print("\n=== Python venv Pakete ===\n")

    install_user = get_install_user()
    venv_name = get_venv_name() or ".venv_e3dc"
    venv_pip = os.path.join(get_home_dir(install_user), venv_name, "bin", "pip")

    if not os.path.exists(venv_pip):
        print("✗ Kein venv gefunden.")
        return

    res = run_command(command_as_user(f"{shlex.quote(venv_pip)} list", install_user))
    if res['success']:
        print(res['stdout'])
    else:
        print(f"✗ Fehler: {format_command_failure(res)}")
    print()

def install_python_packages():
    """Installiert Python-Pakete (bevorzugt im venv)."""
    install_user = get_install_user()
    venv_name, venv_path = resolve_venv_target(install_user)

    print(f"\n→ Installiere Python-Pakete im venv ({venv_name})…")
    system_logger.info(f"Installiere {len(PYTHON_PACKAGES)} Python-Pakete im venv.")

    for pkg in PYTHON_PACKAGES:
        pip_install(pkg, venv_path=venv_path, user=install_user)

def cleanup_legacy_python_packages(use_venv=True):
    """Entfernt alte, schwere Diagramm-Pakete, um Speicherplatz (SD-Karte) freizugeben."""
    print("\n→ Bereinige veraltete Python-Pakete (Plotly, Pandas)...")
    legacy_apt = ["python3-plotly", "python3-pandas"]
    run_command("sudo apt-get remove -y " + " ".join(legacy_apt))
    run_command("sudo apt-get autoremove -y")

    legacy_pip = ["plotly", "pandas", "pandas-stubs", "matplotlib", "pytz", "kaleido"]
    install_user = get_install_user()
    if use_venv:
        venv_name = get_venv_name() or ".venv_e3dc"
        venv_pip = os.path.join(get_home_dir(install_user), venv_name, "bin", "pip")
        if os.path.exists(venv_pip):
            uninstall_cmd = f"{shlex.quote(venv_pip)} uninstall -y " + " ".join(shlex.quote(pkg) for pkg in legacy_pip)
            run_command(command_as_user(uninstall_cmd, install_user))
    else:
        run_command("sudo pip3 uninstall -y --break-system-packages " + " ".join(legacy_pip))
    print("  ✓ Veraltete Pakete entfernt (Speicherplatz freigegeben).")

def install_system_packages(use_venv=True):
    """Delegiert auf den einzigen fail-closed Paket-/Apache-Vertrag."""

    from .utils import install_system_packages as install_bound_system_packages

    return install_bound_system_packages(use_venv=use_venv)

def setup_service_wrapper():
    """Delegiert Wrapper und sudoers an den zentralen fail-closed Reparaturpfad."""
    print("→ Richte Web-UI Service Wrapper ein...")
    from . import web_installer

    if web_installer.is_docker():
        print("  ✓ Docker: kein systemd-/sudoers-Wrapper erforderlich.")
        return True

    result = web_installer.repair_permissions(repair_runtime=False)
    if not result.get("success"):
        message = result.get("message") or "Wrapper-/sudoers-Reparatur fehlgeschlagen."
        system_logger.error("Zentrale Wrapper-/sudoers-Reparatur fehlgeschlagen: %s", result)
        raise RuntimeError(message)

    head = str((result.get("wrapper_integrity") or {}).get("head") or "")[:12]
    suffix = f" (Git-HEAD {head})" if head else ""
    print(f"  ✓ Wrapperintegrität und sudoers atomar geprüft{suffix}.")
    return True


def setup_websocket_service(start_service=True, defer_activation=False):
    """Delegiert auf den kanonischen venv- und transaktionsgebundenen Helper."""

    from .utils import setup_websocket_service as setup_bound_websocket_service

    return setup_bound_websocket_service(
        start_service=start_service,
        defer_activation=defer_activation,
    )

def install_e3dc_control(headless=False):
    print("[i] E3DC-Control C++ Binary wird nicht mehr benoetigt.")
    print("    Bitte 'Installation und Update V4' nutzen.")
    return True

def system_packages_menu():
    """Menue fuer Systempakete."""
    install_system_packages()


def setup_venv_menu():
    """Menue fuer venv Einrichtung."""
    setup_venv(show_header=True)
