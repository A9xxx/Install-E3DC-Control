import os
import json
import shutil
import subprocess

from .core import register_command
from .utils import run_command, pip_install
from .installer_config import get_install_path, get_install_user, get_user_ids, get_www_data_gid, load_config

INSTALL_PATH = get_install_path()
LUX_SCRIPT_NAME = "energy_manager.py"
LUX_CONFIG_NAME = "config.lux.json"
SERVICE_NAME = "energy_manager"
# Wir nutzen direkt das Verzeichnis im Installer (kein Kopieren mehr)
LUX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luxtronik")

def install_dependencies():
    """Installiert Python-Abhängigkeiten für Luxtronik."""
    print("\n→ Installiere Luxtronik-Abhängigkeiten…")
    # pip_install prüft und installiert nur bei Bedarf
    pip_install("luxtronik")
    pip_install("requests")
    return True

def setup_script():
    """Setzt Berechtigungen für die Skripte im Installer-Verzeichnis."""
    print(f"\n→ Setze Berechtigungen in {LUX_DIR}…")
    
    if not os.path.exists(LUX_DIR):
        print(f"✗ Fehler: Verzeichnis {LUX_DIR} nicht gefunden.")
        return False

    install_user = get_install_user()
    
    try:
        # Ordner Rechte: User + www-data (für Config-Schreibzugriff)
        run_command(f"chown -R {install_user}:www-data {LUX_DIR}")
        
        # Standard: Dateien 664, Ordner 775
        run_command(f"find {LUX_DIR} -type f -exec chmod 664 {{}} +")
        run_command(f"find {LUX_DIR} -type d -exec chmod 775 {{}} +")
        
        # Skript ausführbar machen (755)
        script_path = os.path.join(LUX_DIR, LUX_SCRIPT_NAME)
        if os.path.exists(script_path):
            run_command(f"chmod 755 {script_path}")
            
        print(f"✓ Berechtigungen gesetzt.")
        return True
    except Exception as e:
        print(f"✗ Fehler beim Setzen der Berechtigungen: {e}")
        return False

def configure_luxtronik_obsolete():
    """Informiert den User, dass die Konfiguration umgezogen ist."""
    print("\n" + "="*50)
    print("  HINWEIS: Die Konfiguration für den Luxtronik Energy Manager")
    print("  erfolgt nun zentral im Web-Interface unter 'Config Editor'.")
    print("  Dieses Menü dient nur noch der Installation des Dienstes.")
    print("="*50 + "\n")
    input("Drücke Enter um fortzufahren...")

def cleanup_old_service():
    """Entfernt den alten wp-manager Service falls vorhanden."""
    old_service = "wp-manager"
    service_file = f"/etc/systemd/system/{old_service}.service"
    
    if os.path.exists(service_file):
        print(f"\n→ Entferne alten Service '{old_service}'…")
        try:
            run_command(f"sudo systemctl stop {old_service}")
            run_command(f"sudo systemctl disable {old_service}")
            os.remove(service_file)
            run_command("sudo systemctl daemon-reload")
            print(f"✓ Alter Service '{old_service}' entfernt.")
        except Exception as e:
            print(f"⚠ Fehler beim Entfernen des alten Services: {e}")

def setup_service():
    """Richtet den Systemd Service ein."""
    print(f"\n→ Richte Service '{SERVICE_NAME}' ein…")
    
    install_user = get_install_user()
    script_path = os.path.join(LUX_DIR, LUX_SCRIPT_NAME)
    
    # Venv Python nutzen falls vorhanden
    venv_name = load_config().get("venv_name", ".venv_e3dc")
    python_bin = "/usr/bin/python3"
    venv_python = os.path.join(get_install_path(), "..", venv_name, "bin", "python3") # Relativ zum Install User Home
    
    # Exakter Pfad checken
    from .installer_config import get_home_dir
    abs_venv_python = os.path.join(get_home_dir(install_user), venv_name, "bin", "python3")
    
    if os.path.exists(abs_venv_python):
        python_bin = abs_venv_python

    service_content = f"""[Unit]
Description=Luxtronik Energy Manager
After=network.target

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
    
    with open("temp_lux.service", "w") as f:
        f.write(service_content)
    
    run_command(f"sudo mv temp_lux.service {service_file}")
    run_command("sudo systemctl daemon-reload")
    run_command(f"sudo systemctl enable {SERVICE_NAME}")
    run_command(f"sudo systemctl restart {SERVICE_NAME}")
    print(f"✓ Service {SERVICE_NAME} installiert und gestartet.")

def install_luxtronik_menu():
    print(f"→ Stoppe Service '{SERVICE_NAME}'…")
    run_command(f"sudo systemctl stop {SERVICE_NAME}")

    if install_dependencies():
        setup_script()
        configure_luxtronik_obsolete()
        cleanup_old_service()
        setup_service()

register_command("101", "Luxtronik Manager installieren/konfigurieren", install_luxtronik_menu, category="Erweiterungen", sort_order=145)