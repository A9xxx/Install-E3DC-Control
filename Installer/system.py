import os
import shlex
import shutil
import subprocess
import tempfile

from .utils import (
    apt_install,
    command_as_user,
    command_exists,
    ensure_apache_php_module,
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
    """Installiert alle notwendigen Systempakete."""
    print("\n=== Systempakete installieren ===\n")
    system_logger.info("Starte Installation der System- und Python-Pakete.")

    # ---------------------------------------------------------------
    # V4 Kern-Pakete (immer benoetigt)
    # ---------------------------------------------------------------
    packages = [
        # Web-Server
        "apache2", "php", "libapache2-mod-php", "php-curl", "php-sqlite3", "php-mbstring",
        # Python Grundausstattung
        "python3", "python3-pip", "python3-venv",
        "python3-websockets",
        # Python Bibliotheken (via apt, fuer ML / Datenverarbeitung)
        "python3-sklearn", "python3-numpy", "python3-cryptography",
        "python3-bs4",          # Luxtronik WebSocket-Scraping (lux_live.py)
        # System-Hilfspakete
        "curl",                 # allgemein nuetzlich
        "git",                  # Self-Update (UPDATE_POLICY.json)
        # Rust (pywebpush braucht dies beim pip-compile)
        "rustc", "cargo", "libffi-dev", "python3-dev",
    ]

    # ---------------------------------------------------------------
    # C++ Legacy-Pakete (nur benoetigt wenn e3dc.service noch laeuft)
    # In V4 Native Mode koennen diese weggelassen werden:
    #   "build-essential"     -- C++ kompilieren (make)
    #   "cmake"               -- C++ Build-System
    #   "libcurl4-openssl-dev" -- C++ RSCP Link-Bibliothek
    #   "libssl-dev"          -- C++ SSL Link-Bibliothek
    #   "libmosquitto-dev"    -- C++ MQTT Header-Dateien
    #   "libjsoncpp-dev"      -- C++ JSON Header-Dateien
    #   "libsqlite3-dev"      -- C++ SQLite Linker
    #   "jq"                  -- Shell-Skripte im C++ Repo
    # ---------------------------------------------------------------
    import os as _os
    cpp_still_active = _os.path.exists("/etc/systemd/system/e3dc.service")
    if cpp_still_active:
        print("  [i] Legacy e3dc.service erkannt - installiere auch C++ Build-Abhaengigkeiten.")
        packages += [
            "build-essential", "cmake",
            "libcurl4-openssl-dev", "libssl-dev",
            "libmosquitto-dev", "libjsoncpp-dev",
            "libsqlite3-dev",
            "jq",
        ]
    else:
        print("  [i] V4 Native Mode - C++ Build-Pakete werden nicht installiert.")

    print("→ Installiere Systempakete…\n")
    system_logger.info(f"Installiere {len(packages)} Systempakete.")
    for pkg in packages:
        apt_install(pkg)

    cleanup_legacy_python_packages(use_venv)

    install_rscpgui()

    # --- NEU: Apache PHP + WebSocket Reverse Proxy automatisch einrichten ---
    ensure_apache_php_module()
    print("\n→ Konfiguriere Apache Reverse Proxy für WebSockets...")
    run_command("sudo a2enmod proxy")
    run_command("sudo a2enmod proxy_wstunnel")

    conf_path = "/etc/apache2/sites-available/000-default.conf"
    if os.path.exists(conf_path):
        with open(conf_path, "r") as f:
            content = f.read()

        modified = False
        if 'ProxyPass "/ws"' not in content:
            content = content.replace("</VirtualHost>", '    ProxyPass "/ws" "ws://127.0.0.1:8765/"\n</VirtualHost>')
            modified = True
        elif '127.0.0.1:8080' in content:
            content = content.replace('127.0.0.1:8080', '127.0.0.1:8765')
            modified = True

        if modified:
            with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
                tmp.write(content)
                tmp_name = tmp.name
            run_command(f"sudo cp {tmp_name} {conf_path}")
            os.remove(tmp_name)
            print("  ✓ Proxy-Regel erfolgreich in Apache-Konfiguration auf Port 8765 gesetzt.")
            system_logger.info("Apache Proxy-Regel für WebSockets auf Port 8765 aktualisiert.")
        else:
            print("  ✓ Proxy-Regel existiert bereits korrekt.")

    run_command("sudo systemctl restart apache2")
    # ------------------------------------------------------------------

    # Python Umgebung einrichten
    if use_venv:
        setup_venv(show_header=False)
    else:
        install_python_packages()

    # WebSocket Service am Ende der Paket-Installation mit einrichten
    installer_dir = os.path.dirname(os.path.abspath(__file__))
    ws_script = os.path.join(installer_dir, "e3dc_websocket.py")
    if os.path.exists(ws_script):
        setup_websocket_service()

    # Wrapper & Sudoers für Web-UI Task Manager einrichten
    setup_service_wrapper()

    print("\n✓ Systempakete vollständig installiert.\n")
    system_logger.info("Installation der Pakete abgeschlossen.")
    log_task_completed("Systempakete installieren")

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


def setup_websocket_service(start_service=True):
    """Richtet den E3DC WebSocket Server als Systemd-Dienst ein."""
    print("→ Richte e3dc-websocket Service ein...")
    install_user = get_install_user()
    install_path = get_install_path()
    installer_dir = os.path.dirname(os.path.abspath(__file__))

    service_content = f"""[Unit]
Description=E3DC WebSocket Server fuer fluessige Dashboard Animationen
After=network.target apache2.service

[Service]
Type=simple
User={install_user}
Group=www-data
WorkingDirectory={installer_dir}
ExecStart=/usr/bin/python3 {installer_dir}/e3dc_websocket.py
Restart=always
RestartSec=5
StandardOutput=journal
StandardError=journal
SyslogIdentifier=e3dc-websocket

[Install]
WantedBy=multi-user.target
"""
    with tempfile.NamedTemporaryFile("w", delete=False) as tmp:
        tmp.write(service_content)
        tmp_name = tmp.name

    run_command(f"sudo cp {tmp_name} /etc/systemd/system/e3dc-websocket.service")
    os.remove(tmp_name)
    run_command("sudo systemctl daemon-reload")
    run_command("sudo systemctl enable e3dc-websocket.service")
    run_command("sudo systemctl reset-failed e3dc-websocket.service 2>/dev/null || true")
    if start_service:
        run_command("sudo systemctl start e3dc-websocket.service")
        print("  ✓ Service e3dc-websocket erstellt und gestartet.")
    else:
        print("  ✓ Service e3dc-websocket erstellt und aktiviert; Start wird gesammelt ausgeführt.")

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
