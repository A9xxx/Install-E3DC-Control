import os
import tempfile

from .core import register_command, CAT_EXTENSIONS
from .utils import run_command
from .installer_config import get_install_path, get_install_user, get_venv_path
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH = get_install_path()
SERVICE_NAME = "e3dc-wallbox-manager"
SCRIPT_NAME = "wallbox_manager.py"
INSTALLER_DIR = os.path.dirname(os.path.abspath(__file__))

wb_logger = get_or_create_logger("wallbox_setup")

def setup_wallbox_service(headless=False):
    """Richtet den Systemd Service für den nativen Python Wallbox Manager ein."""
    print("\n=== Native Wallbox Regelung Setup ===\n")
    print("Dieser Schritt installiert den Hintergrunddienst für Drittanbieter Wallboxen (Go-E, etc.).")
    print("Hinweis: Vergiss nicht, wb_native_enable=1 in der E3DC-Config (Web UI) zu setzen!\n")
    
    install_user = get_install_user()
    python_bin = os.path.join(get_venv_path(install_user), "bin", "python3")
    script_path = os.path.join(INSTALLER_DIR, SCRIPT_NAME)
    
    if not os.path.exists(python_bin):
        python_bin = "/usr/bin/python3"
        
    print(f"→ Erstelle Systemd Service '{SERVICE_NAME}'…")
    
    service_content = f"""[Unit]
Description=E3DC Native Wallbox Manager
After=network.target

[Service]
Type=simple
User={install_user}
Group=www-data
WorkingDirectory={INSTALLER_DIR}
ExecStart={python_bin} {script_path}
Restart=always
RestartSec=15

[Install]
WantedBy=multi-user.target
"""
    try:
        with open("temp_wb.service", "w") as f:
            f.write(service_content)
            
        run_command(f"sudo mv temp_wb.service /etc/systemd/system/{SERVICE_NAME}.service")
        run_command("sudo systemctl daemon-reload")
        run_command(f"sudo systemctl enable {SERVICE_NAME}")
        run_command(f"sudo systemctl restart {SERVICE_NAME}")
        
        print(f"✓ Service '{SERVICE_NAME}' erfolgreich installiert und gestartet.")
        log_task_completed("Native Wallbox Manager Service eingerichtet")
        
    except Exception as e:
        print(f"✗ Fehler beim Installieren des Services: {e}")
        log_error("wallbox_setup", f"Fehler bei Service Installation: {e}")
        
    if not headless:
        input("\nDrücke Enter um ins Hauptmenü zurückzukehren...")

register_command("42", "Native Wallbox Manager (z.B. Go-E Charger)", setup_wallbox_service, sort_order=42)
