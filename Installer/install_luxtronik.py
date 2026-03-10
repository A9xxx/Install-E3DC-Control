import os
import json
import shutil
import subprocess

from .core import register_command
from .utils import run_command, pip_install
from .installer_config import get_install_path, get_install_user, get_user_ids, get_www_data_gid, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH = get_install_path()
LUX_SCRIPT_NAME = "energy_manager.py"
LUX_CONFIG_NAME = "config.lux.json"
SERVICE_NAME = "energy_manager"
# Wir nutzen direkt das Verzeichnis im Installer (kein Kopieren mehr)
LUX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luxtronik")

lux_logger = get_or_create_logger("luxtronik")

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
        log_error("luxtronik", f"Fehler bei Berechtigungen: {e}")
        return False

def configure_luxtronik():
    """Erstellt oder bearbeitet die config.lux.json."""
    print("\n=== Luxtronik Konfiguration ===\n")
    
    config_path = os.path.join(LUX_DIR, LUX_CONFIG_NAME)
    current_conf = {}
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                current_conf = json.load(f)
        except: pass

    # Wizard
    def ask(key, text, default):
        val = input(f"{text} [{current_conf.get(key, default)}]: ").strip()
        return val if val else current_conf.get(key, default)

    new_conf = current_conf.copy()
    
    # Aktivierung
    act = ask("luxtronik", "Luxtronik aktivieren? (1=Ja, 0=Nein)", "1")
    new_conf["luxtronik"] = int(act)
    
    if new_conf["luxtronik"] == 1:
        new_conf["luxtronik_ip"] = ask("luxtronik_ip", "IP-Adresse der Wärmepumpe", "192.168.178.88")
        new_conf["GRID_START_LIMIT"] = int(ask("GRID_START_LIMIT", "Einspeisegrenze Start (Watt, negativ)", "-3500"))
        new_conf["MIN_SOC"] = int(ask("MIN_SOC", "Mindest-SoC (%)", "65"))
        new_conf["AT_LIMIT"] = float(ask("AT_LIMIT", "Außentemperatur-Grenze (°C)", "14.0"))
        
        print("\n--- Boost Temperaturen ---")
        new_conf["WWS"] = float(ask("WWS", "Warmwasser Soll (Sommer/Boost) (°C)", "55.0"))
        new_conf["WWW"] = float(ask("WWW", "Warmwasser Soll (Winter) (°C)", "45.0"))
        new_conf["HZ"] = float(ask("HZ", "Rücklauf Soll (°C)", "50.0"))

    # Speichern
    try:
        with open(config_path, 'w') as f:
            json.dump(new_conf, f, indent=4)
        
        # Rechte
        uid, _ = get_user_ids()
        gid = get_www_data_gid()
        os.chown(config_path, uid, gid)
        os.chmod(config_path, 0o664)
        print(f"✓ Konfiguration gespeichert: {config_path}")
    except Exception as e:
        print(f"✗ Fehler beim Speichern: {e}")

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
        configure_luxtronik()
        cleanup_old_service()
        setup_service()
        log_task_completed("Luxtronik Installation")

register_command("101", "Luxtronik Manager installieren/konfigurieren", install_luxtronik_menu, category="Erweiterungen", sort_order=145)