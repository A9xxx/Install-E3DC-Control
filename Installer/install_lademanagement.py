import os
import json
import shutil
import subprocess

from .core import register_command
from .utils import run_command, pip_install, replace_in_file
from .installer_config import get_install_path, get_install_user, get_user_ids, get_www_data_gid, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error

# This is the directory where the energy_manager.py and its config are located
# It's inside the Installer package structure
LADEMANAGEMENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luxtronik")
SCRIPT_NAME = "energy_manager.py"
SERVICE_NAME = "energy_manager" # We reuse the same service

lademanagement_logger = get_or_create_logger("lademanagement")

def install_dependencies():
    """Installiert Python-Abhängigkeiten für das Lademanagement."""
    print("\n→ Installiere Lademanagement-Abhängigkeiten…")
    # The only dependency is 'requests', which is usually installed by system.py
    # We ensure it's there.
    pip_install("requests")
    return True

def setup_script_permissions():
    """Setzt Berechtigungen für die Skripte."""
    print(f"\n→ Setze Berechtigungen für Energy Manager in {LADEMANAGEMENT_DIR}…")
    
    if not os.path.exists(LADEMANAGEMENT_DIR):
        print(f"✗ Fehler: Verzeichnis {LADEMANAGEMENT_DIR} nicht gefunden.")
        return False

    install_user = get_install_user()
    
    try:
        run_command(f"chown -R {install_user}:www-data {LADEMANAGEMENT_DIR}")
        run_command(f"find {LADEMANAGEMENT_DIR} -type f -exec chmod 664 {{}} +")
        run_command(f"find {LADEMANAGEMENT_DIR} -type d -exec chmod 775 {{}} +")
        
        script_path = os.path.join(LADEMANAGEMENT_DIR, SCRIPT_NAME)
        if os.path.exists(script_path):
            run_command(f"chmod 755 {script_path}")
            
        print(f"✓ Berechtigungen gesetzt.")
        return True
    except Exception as e:
        print(f"✗ Fehler beim Setzen der Berechtigungen: {e}")
        log_error("lademanagement", f"Fehler bei Berechtigungen: {e}")
        return False

def configure_lademanagement():
    """Informiert über zentrale Konfiguration und setzt Basis-Werte in e3dc.config.txt."""
    print("\n=== Intelligentes Lademanagement Konfiguration ===\n")
    
    print("HINWEIS: Die Konfiguration erfolgt nun zentral im Web-Interface")
    print("unter 'Config Editor' (Gruppe: Luxtronik Energy Manager).")
    print("Dieses Setup richtet den notwendigen Hintergrunddienst ein.\n")

    # Basis-Konfiguration setzen
    try:
        config_file = os.path.join(get_install_path(), "e3dc.config.txt")
        if os.path.exists(config_file):
            print("→ Passe e3dc.config.txt an (Nur-Lade-Modus)...")
            
            # 1. Luxtronik-Modul deaktivieren (wir wollen nur Lademanagement)
            replace_in_file(config_file, "luxtronik", "luxtronik = 0")
            
            # 2. Automatik-Modus aktivieren (damit der Energy Manager läuft)
            replace_in_file(config_file, "auto_mode", "auto_mode = 1")
            
            print("✓ 'luxtronik = 0' gesetzt (keine WP-Steuerung).")
            print("✓ 'auto_mode = 1' gesetzt (Energy Manager aktiv).")
            
            # Rechte sicherstellen
            uid, _ = get_user_ids()
            gid = get_www_data_gid()
            os.chown(config_file, uid, gid)
            os.chmod(config_file, 0o664)
    except Exception as e:
        print(f"⚠ Fehler beim Anpassen der Konfiguration: {e}")
        log_error("lademanagement", f"Fehler bei Config-Anpassung: {e}")

    input("\nDrücke Enter um fortzufahren...")
    return True

def setup_lademanagement_service():
    """Richtet den Systemd Service für den energy_manager ein."""
    print(f"\n→ Richte Service '{SERVICE_NAME}' ein…")
    
    install_user = get_install_user()
    script_path = os.path.join(LADEMANAGEMENT_DIR, SCRIPT_NAME)
    
    venv_name = load_config().get("venv_name", ".venv_e3dc")
    python_bin = "/usr/bin/python3"
    
    from .installer_config import get_home_dir
    abs_venv_python = os.path.join(get_home_dir(install_user), venv_name, "bin", "python3")
    
    if os.path.exists(abs_venv_python):
        python_bin = abs_venv_python
        print(f"  (Verwende Python aus venv: {python_bin})")

    service_content = f"""[Unit]
Description=E3DC Intelligent Charge Management
After=network.target e3dc.service

[Service]
Type=simple
User={install_user}
Group=www-data
ExecStart={python_bin} {script_path}
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
"""
    service_file = f"/etc/systemd/system/{SERVICE_NAME}.service"
    
    with open("temp_lademanagement.service", "w") as f:
        f.write(service_content)
    
    run_command(f"sudo mv temp_lademanagement.service {service_file}")
    run_command("sudo systemctl daemon-reload")
    run_command(f"sudo systemctl enable {SERVICE_NAME}")
    run_command(f"sudo systemctl restart {SERVICE_NAME}")
    print(f"✓ Service {SERVICE_NAME} installiert und gestartet.")

def install_lademanagement_menu():
    """Hauptfunktion für das Installationsmenü."""
    print(f"→ Stoppe Service '{SERVICE_NAME}'…")
    run_command(f"sudo systemctl stop {SERVICE_NAME}")

    if install_dependencies():
        setup_script_permissions()
        if configure_lademanagement():
            setup_lademanagement_service()
            log_task_completed("Intelligentes Lademanagement Installation")

register_command("102", "Intelligentes Lademanagement installieren/konfigurieren", install_lademanagement_menu, category="Erweiterungen", sort_order=146)