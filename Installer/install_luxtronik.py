import os
import json
import shutil
import subprocess

from .core import register_command
from .utils import run_command, pip_install, replace_in_file, cleanup_pycache
from .installer_config import get_install_path, get_install_user, get_user_ids, get_www_data_gid, load_config, get_venv_path
from .logging_manager import get_or_create_logger, log_task_completed, log_error

INSTALL_PATH = get_install_path()
LUX_SCRIPT_NAME = "energy_manager.py"
LUX_CONFIG_NAME = "config.lux.json"
SERVICE_NAME = "energy_manager"
LIVE_SERVICE_NAME = "e3dc-lux-live"
# Wir nutzen direkt das Verzeichnis im Installer (kein Kopieren mehr)
LUX_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "luxtronik")

luxtronik_logger = get_or_create_logger("luxtronik")

def install_dependencies(wp_type=0):
    """Installiert Python-Abhängigkeiten für die gewählte Wärmepumpe."""
    print("\n→ Installiere Abhängigkeiten…")
    
    install_user = get_install_user()
    venv_path = get_venv_path(install_user)
    
    pip_install("requests", venv_path=venv_path, user=install_user)
    if wp_type in (1, 2, 4, 5):
        pip_install("pymodbus", venv_path=venv_path, user=install_user)
    
    if wp_type == 0:
        pip_install("luxtronik", venv_path=venv_path, user=install_user)
    return True

def setup_script(wp_type=0):
    """Setzt Berechtigungen für die Skripte."""
    print(f"\n→ Setze Berechtigungen…")
    
    install_user = get_install_user()
    
    # 1. Luxtronik/Energy Manager Basis
    if os.path.exists(LUX_DIR):
        run_command(f"chown -R {install_user}:www-data {LUX_DIR}")
        run_command(f"find {LUX_DIR} -type f -exec chmod 664 {{}} +")
        run_command(f"find {LUX_DIR} -type d -exec chmod 775 {{}} +")
        script_path = os.path.join(LUX_DIR, LUX_SCRIPT_NAME)
        if os.path.exists(script_path): run_command(f"chmod 755 {script_path}")

    # 2. IDM falls gewählt
    for selected_type, driver_dir in ((1, "idm"), (4, "stiebel"), (5, "dimplex")):
        if wp_type == selected_type:
            vendor_dir = os.path.join(INSTALL_PATH, "Installer", driver_dir)
            if os.path.exists(vendor_dir):
                run_command(f"chown -R {install_user}:www-data {vendor_dir}")
                run_command(f"find {vendor_dir} -type f -exec chmod 755 {{}} +")
            
    print(f"✓ Berechtigungen gesetzt.")
    return True

def configure_luxtronik(wp_type=0, headless=False):
    """Informiert über zentrale Konfiguration und aktiviert den Energy Manager."""
    print("\n=== Energy Manager (Wärmepumpe & Lademanagement) ===\n")
    
    print("HINWEIS: Die Konfiguration erfolgt nun zentral im Web-Interface")
    print("unter 'Config Editor' (Gruppe: Luxtronik Energy Manager / Smart Grid).")
    print("Dieses Setup richtet den zentralen Hintergrunddienst ein.\n")

    # Basis-Konfiguration setzen
    try:
        config_file = os.path.join(get_install_path(), "e3dc.config.txt")
        if os.path.exists(config_file):
            print("→ Passe e3dc.config.txt an (Luxtronik aktivieren)...")
            
            # 1. WP-Typ setzen
            replace_in_file(config_file, "wp_type", f"wp_type = {wp_type}")
            
            # 2. IP-Adressen abfragen
            if not headless:
                if wp_type == 1:
                    idm_ip = input("\nWie lautet die IP-Adresse deiner IDM Wärmepumpe?: ").strip()
                    if idm_ip:
                        replace_in_file(config_file, "idm_ip", f"idm_ip = {idm_ip}")
                        print(f"✓ IDM IP '{idm_ip}' gespeichert.")
                elif wp_type == 4:
                    stiebel_ip = input("\nWie lautet die IP-Adresse deines Stiebel Eltron ISG?: ").strip()
                    if stiebel_ip:
                        replace_in_file(config_file, "stiebel_isg_ip", f"stiebel_isg_ip = {stiebel_ip}")
                        print(f"Stiebel ISG IP '{stiebel_ip}' gespeichert.")
                elif wp_type == 5:
                    dimplex_ip = input("\nWie lautet die IP-Adresse deiner Dimplex WPM Touch / NWPM?: ").strip()
                    if dimplex_ip:
                        replace_in_file(config_file, "dimplex_ip", f"dimplex_ip = {dimplex_ip}")
                        print(f"Dimplex IP '{dimplex_ip}' gespeichert.")
                elif wp_type == 2:
                    hs_ip = input("\nIP-Adresse des Heizstabs (Modbus-TCP) [0.0.0.0]: ").strip() or "0.0.0.0"
                    sh_ip = input("IP-Adresse des Shelly-Heizlüfters HTTP [0.0.0.0]: ").strip() or "0.0.0.0"
                    replace_in_file(config_file, "heizstab_ip", f"heizstab_ip = {hs_ip}")
                    replace_in_file(config_file, "shelly_heiz_ip", f"shelly_heiz_ip = {sh_ip}")
                    print(f"✓ Heizstab: {hs_ip} | Shelly: {sh_ip} gespeichert.")
            
            # 3. Automatik-Modus aktivieren (Energy Manager)
            replace_in_file(config_file, "auto_mode", "auto_mode = 1")
            
            print(f"✓ wp_type = {wp_type} und auto_mode = 1 gesetzt.")
            
            # Rechte sicherstellen
            uid, _ = get_user_ids()
            gid = get_www_data_gid()
            os.chown(config_file, uid, gid)
            os.chmod(config_file, 0o664)
    except Exception as e:
        print(f"⚠ Fehler beim Anpassen der Konfiguration: {e}")
        log_error("luxtronik", f"Fehler bei Config-Anpassung: {e}")

    if not headless:
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

def setup_service(wp_type=0):
    """Richtet the Systemd Services ein."""
    print(f"\n→ Richte Services ein…")
    
    install_user = get_install_user()
    installer_dir = os.path.dirname(os.path.abspath(__file__))
    python_bin = os.path.join(get_venv_path(install_user), "bin", "python3")
    if not os.path.exists(python_bin): python_bin = "/usr/bin/python3"

    def stop_disable_live_service(service_name):
        run_command(f"sudo systemctl stop {service_name} 2>/dev/null || true")
        run_command(f"sudo systemctl disable {service_name} 2>/dev/null || true")

    if wp_type < 0:
        script_path = os.path.join(installer_dir, "luxtronik", "energy_manager.py")
        for live_service in ("e3dc-lux-live", "e3dc-idm-live", "e3dc-stiebel-live", "e3dc-dimplex-live"):
            stop_disable_live_service(live_service)
        service_content = f"""[Unit]
Description=E3DC Energy Manager (Smart Charging)
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
        with open("temp_wp.service", "w") as f:
            f.write(service_content)
        run_command(f"sudo mv temp_wp.service /etc/systemd/system/{SERVICE_NAME}.service")
        run_command("sudo systemctl daemon-reload")
        run_command(f"sudo systemctl enable {SERVICE_NAME}")
        run_command(f"sudo systemctl restart {SERVICE_NAME}")
        print("✓ Energy Manager ohne native Waermepumpe installiert.")
        return
    
    # 3. Heizstab/Shelly Manager (wp_type=2) und Shelly Pro3EM WP-Monitoring (wp_type=3)
    if wp_type in (2, 3):
        from .utils import _create_service_file
        # Stoppe alte Services falls vorhanden
        run_command(f"sudo systemctl stop {SERVICE_NAME} 2>/dev/null || true")
        for live_service in ("e3dc-lux-live", "e3dc-idm-live", "e3dc-stiebel-live", "e3dc-dimplex-live"):
            stop_disable_live_service(live_service)
        
        _create_service_file("e3dc-heizstab", "E3DC Heizstab / Shelly Manager", "heizstab_manager.py")
        run_command("sudo systemctl daemon-reload")
        run_command("sudo systemctl enable e3dc-heizstab")
        run_command("sudo systemctl restart e3dc-heizstab")
        print(f"✓ Heizstab/Shelly Manager Service (wp_type={wp_type}) installiert und gestartet.")
        return

    # Parameter für Luxtronik/IDM
    script_path = os.path.join(installer_dir, "luxtronik", "energy_manager.py")
    
    if wp_type == 1:
        live_script_path = os.path.join(installer_dir, "idm", "idm_live.py")
        live_service_id = "e3dc-idm-live"
        live_service_desc = "IDM Modbus Live Daemon"
        live_service_label = "IDM"
    elif wp_type == 4:
        live_script_path = os.path.join(installer_dir, "stiebel", "stiebel_live.py")
        live_service_id = "e3dc-stiebel-live"
        live_service_desc = "Stiebel ISG Live Daemon"
        live_service_label = "Stiebel"
    elif wp_type == 5:
        live_script_path = os.path.join(installer_dir, "dimplex", "dimplex_live.py")
        live_service_id = "e3dc-dimplex-live"
        live_service_desc = "Dimplex WPM Touch Live Daemon"
        live_service_label = "Dimplex"
    else:
        live_script_path = os.path.join(installer_dir, "luxtronik", "lux_live.py")
        live_service_id = "e3dc-lux-live"
        live_service_desc = "Luxtronik WebSocket Live Daemon"
        live_service_label = "Luxtronik"

    # 1. Energy Manager Service (Luxtronik/IDM Basis)
    service_content = f"""[Unit]
Description=E3DC Energy Manager (Heatpump & Wallbox)
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
    with open("temp_wp.service", "w") as f: f.write(service_content)
    run_command(f"sudo mv temp_wp.service /etc/systemd/system/{SERVICE_NAME}.service")
    
    # 2. Live Data Service
    ws_service_content = f"""[Unit]
Description={live_service_desc}
After=network.target

[Service]
Type=simple
User={install_user}
Group=www-data
ExecStart={python_bin} {live_script_path}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    with open("temp_wp_live.service", "w") as f: f.write(ws_service_content)
    run_command(f"sudo mv temp_wp_live.service /etc/systemd/system/{live_service_id}.service")
    
    run_command("sudo systemctl daemon-reload")
    run_command(f"sudo systemctl enable {SERVICE_NAME}")
    run_command(f"sudo systemctl restart {SERVICE_NAME}")
    for other_live_service in ("e3dc-lux-live", "e3dc-idm-live", "e3dc-stiebel-live", "e3dc-dimplex-live"):
        if other_live_service != live_service_id:
            stop_disable_live_service(other_live_service)
    run_command(f"sudo systemctl enable {live_service_id}")
    run_command(f"sudo systemctl restart {live_service_id}")
    if wp_type == 4:
        print("Waermepumpen Services (Stiebel) installiert.")
        return
    if wp_type == 5:
        print("Waermepumpen Services (Dimplex) installiert.")
        return
    print(f"✓ Wärmepumpen Services ({'IDM' if wp_type==1 else 'Luxtronik'}) installiert.")

def install_luxtronik_menu(headless=False):
    print("\n=== Wärmepumpen & Energy Manager Setup ===\n")
    
    wp_type = -1
    if headless:
        try:
            import json
            with open('/var/www/html/data/e3dc_v4.json', 'r') as f:
                v4 = json.load(f)
                wp_type = int(v4.get('wp_type', -1))
        except Exception:
            pass
        print(f"→ Headless-Modus: wp_type = {wp_type} aus Konfiguration gelesen.")
    else:
        print("Welche Wärmepumpe möchtest du anbinden?")
        print("-1) Keine Waermepumpe (nur Smart Charging / Wallbox)")
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

    # Cache-Bereinigung
    cleanup_pycache(LUX_DIR)
    run_command(f"sudo systemctl stop {SERVICE_NAME} 2>/dev/null || true")

    if install_dependencies(wp_type):
        setup_script(wp_type)
        configure_luxtronik(wp_type, headless=headless)
        cleanup_old_service()
        setup_service(wp_type)
        if wp_type == 4:
            log_task_completed("Energy Manager Installation (Stiebel)")
            return
        if wp_type == 5:
            log_task_completed("Energy Manager Installation (Dimplex)")
            return
        log_task_completed(f"Energy Manager Installation ({'IDM' if wp_type==1 else 'Luxtronik'})")

register_command("41", "Energy Manager (Luxtronik/IDM Wärmepumpe & Lademanagement)", install_luxtronik_menu, sort_order=41)
