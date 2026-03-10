import os
import json
import shutil
import subprocess

from .core import register_command
from .utils import run_command, pip_install
from .installer_config import get_install_path, get_install_user, get_user_ids, get_www_data_gid, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error

# This is the directory where the energy_manager.py and its config are located
# It's inside the Installer package structure
LADEMANAGEMENT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luxtronik")
SCRIPT_NAME = "energy_manager.py"
CONFIG_NAME = "config.lux.json" # We reuse the same config file for simplicity
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
    """Erstellt oder bearbeitet die config.lux.json für reines Lademanagement."""
    print("\n=== Intelligentes Lademanagement Konfiguration ===\n")
    
    config_path = os.path.join(LADEMANAGEMENT_DIR, CONFIG_NAME)
    current_conf = {}
    
    if os.path.exists(config_path):
        try:
            with open(config_path, 'r') as f:
                current_conf = json.load(f)
        except: pass

    # SAFETY CHECK: Warnung, falls Luxtronik bereits aktiv ist
    if current_conf.get("luxtronik") == 1:
        print("\n⚠️  ACHTUNG: Es wurde eine aktive Luxtronik-Konfiguration gefunden!")
        print("   Dieser Assistent aktiviert den 'Nur-Lade-Modus' und DEAKTIVIERT die Wärmepumpe.")
        print("   (Um beides zu nutzen: Installiere den Luxtronik-Manager und aktiviere")
        print("    das Lademanagement anschließend im Web-Interface.)\n")
        
        if input("   Trotzdem fortfahren und WP deaktivieren? (j/n) [n]: ").strip().lower() != 'j':
            print("Abbruch.")
            return False

    def ask(key, text, default, type_func=str):
        val = input(f"{text} [{current_conf.get(key, default)}]: ").strip()
        raw_val = val if val else current_conf.get(key, default)
        try:
            return type_func(raw_val)
        except (ValueError, TypeError):
            return default

    new_conf = current_conf.copy()
    
    # Deactivate Luxtronik module explicitly
    new_conf["luxtronik"] = 0
    new_conf["auto_mode"] = 1 # Automatik-Regelung muss an sein
    
    print("Die Luxtronik-Integration wird deaktiviert, um nur das Lademanagement zu nutzen.")
    print("Die Automatik-Regelung wird aktiviert.\n")

    # Morning Boost
    new_conf['morning_boost_enable'] = ask("morning_boost_enable", "Intelligente Speicher-Entladung (Morgen) aktivieren? (1=Ja, 0=Nein)", 1, int)
    if new_conf['morning_boost_enable'] == 1:
        print("\n--- Konfiguration: Intelligente Speicher-Entladung ---")
        # Priority: only wallbox options are relevant
        mb_prio = ask("morning_boost_prio", "Priorität (wallbox, wallbox_only)", "wallbox_only")
        if mb_prio not in ["wallbox", "wallbox_only"]:
            mb_prio = "wallbox_only"
        new_conf['morning_boost_prio'] = mb_prio
        new_conf['morning_boost_wb_power'] = ask("morning_boost_wb_power", "Annahme Ladeleistung der Wallbox (kW)", 7.0, float)
        new_conf['morning_boost_deadline'] = ask("morning_boost_deadline", "Ziel-Zeit (Stunde, z.B. 8 für 08:00)", 8, int)
        new_conf['morning_boost_target_soc'] = ask("morning_boost_target_soc", "Ziel-SoC nach Entladung (%)", 20, int)
        new_conf['morning_boost_min_hours'] = ask("morning_boost_min_hours", "Bedingung: Mind. Stunden mit 99% SoC Prognose", 3, int)
        new_conf['morning_boost_min_pv_pct'] = ask("morning_boost_min_pv_pct", "Bedingung: Mind. PV-Ertrag in diesen Stunden (%)", 50.0, float)

    # Superintelligence
    new_conf['super_intelligence_enable'] = ask("super_intelligence_enable", "\nSuperintelligenz (Experimentell) aktivieren? (1=Ja, 0=Nein)", 0, int)
    if new_conf['super_intelligence_enable'] == 1:
        print("\n--- Konfiguration: Superintelligenz ---")
        new_conf['super_intelligence_deadline'] = ask("super_intelligence_deadline", "Ziel-Zeit (Stunde, z.B. 8 für 08:00)", 8, int)
        print("Info: Entlädt auf den 'Manuell Boost Min-SoC' (siehe Luxtronik-Einstellungen).")

    # Speichern
    try:
        with open(config_path, 'w') as f:
            json.dump(new_conf, f, indent=4)
        
        uid, _ = get_user_ids()
        gid = get_www_data_gid()
        os.chown(config_path, uid, gid)
        os.chmod(config_path, 0o664)
        print(f"\n✓ Konfiguration gespeichert: {config_path}")
    except Exception as e:
        print(f"✗ Fehler beim Speichern: {e}")
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