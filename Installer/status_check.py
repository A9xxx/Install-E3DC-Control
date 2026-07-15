import os
import sys
from .core import register_command
from .utils import run_command
from .logging_manager import get_or_create_logger, log_task_completed
from .installer_config import get_install_user, get_install_path, load_config, get_home_dir

status_logger = get_or_create_logger("status_check")

# ANSI Colors
GREEN = '\033[92m'
RED = '\033[91m'
RESET = '\033[0m'

def check_internet_connection():
    """Prüft die Internetverbindung (Ping zu Google DNS)."""
    res = run_command("ping -c 1 -W 2 8.8.8.8")
    return res['success']

def check_service_details(service_name):
    """Prüft einen Systemd-Service detailliert."""
    # Prüfe an üblichen Orten
    if not os.path.exists(f"/etc/systemd/system/{service_name}.service") and \
       not os.path.exists(f"/lib/systemd/system/{service_name}.service"):
        return {"status": "not_installed", "active": False, "enabled": False, "log": ""}

    # Status prüfen
    res_active = run_command(f"systemctl is-active {service_name}")
    is_active = res_active['stdout'].strip() == "active"
    
    res_enabled = run_command(f"systemctl is-enabled {service_name}")
    is_enabled = res_enabled['stdout'].strip() == "enabled"
    
    # Letzte Logs holen (letzte 10 Zeilen für mehr Kontext)
    res_log = run_command(f"journalctl -u {service_name} -n 10 --no-pager")
    log_lines = res_log['stdout'].strip() if res_log['success'] else "Keine Logs verfügbar."
    
    return {
        "status": "installed",
        "active": is_active,
        "enabled": is_enabled,
        "log": log_lines
    }

def show_system_status():
    """Zeigt den Status aller relevanten Dienste an."""
    print("\n=== System-Status & Diagnose ===\n")
    status_logger.info("Starte System-Statusprüfung.")
    
    issues_found = []

    # HA Status ermitteln
    is_standby = False
    standby_label = "SLAVE"
    ha_mode = "off"
    ha_state = "unknown"
    try:
        v4_path = "/var/www/html/data/e3dc_v4.json"
        if os.path.exists(v4_path):
            import json
            with open(v4_path, "r", encoding="utf-8") as f:
                v4_data = json.load(f)
                ha_mode = str(v4_data.get('ha_mode', 'off')).lower()

        ha_status_file = "/var/www/html/ramdisk/ha_status.json"
        if os.path.exists(ha_status_file):
            import json
            with open(ha_status_file, "r") as f:
                ha_data = json.load(f)
            ha_state = ha_data.get('state', 'unknown').lower()

        if ha_mode == "shadow":
            is_standby = True
            standby_label = "SHADOW"
        elif ha_mode == "slave" and ha_state != "failover":
            is_standby = True
    except: pass

    install_user = get_install_user()
    is_docker = os.path.exists(os.path.join(get_home_dir(install_user), "e3dc-docker", "docker-compose.yml"))
    if is_docker:
        print("\n--- Docker-Modus ---")
        print(f"{GREEN}✓{RESET} System läuft in Docker. Lokale Host-Dienste sind absichtlich inaktiv.")

    def print_standby_service_status(service, issue_key="ha_running_in_standby", indent=""):
        if not is_standby:
            return False
        enabled_icon = f"{GREEN}✓{RESET}" if service["enabled"] else f"{RED}✗{RESET}"
        if not service["active"]:
            print(f"{indent}{GREEN}✓{RESET} Service Status: Inaktiv (Korrekt für {standby_label} im Standby)")
        else:
            print(f"{indent}{RED}✗{RESET} Service Status: Aktiv (FEHLER: Sollte im {standby_label}-Standby gestoppt sein!)")
            issues_found.append(issue_key)
        print(f"{indent}{enabled_icon} Autostart:     {'Aktiviert (enabled)' if service['enabled'] else 'Deaktiviert (disabled)'}")
        return True

    # 0. Internet Check
    print("--- Netzwerk ---")
    if check_internet_connection():
        print(f"{GREEN}✓{RESET} Internetverbindung: OK (Ping 8.8.8.8)")
    else:
        print(f"{RED}✗{RESET} Internetverbindung: FEHLGESCHLAGEN")
        issues_found.append("internet")

    # 1. E3DC-Control Service (Legacy C++ - in V4 optional)
    print("\n--- E3DC-Control C++ (Legacy) ---")
    e3dc_srv = check_service_details("e3dc")
    live_srv = check_service_details("e3dc-live")

    if e3dc_srv["status"] == "not_installed":
        # In V4 ist das der Normalfall - kein Fehler!
        print("  [i] Service 'e3dc' (C++ Legacy): Nicht installiert (V4 Normal)")
        print("      RSCP-Kommunikation laeuft nativ ueber e3dc-live (Python).")
    elif is_docker:
        if not e3dc_srv["active"]:
            print(f"{GREEN}[OK]{RESET} Service Status: Inaktiv (Korrekt fuer Docker-Modus)")
        else:
            print(f"{RED}[!]{RESET}  Service Status: Aktiv (FEHLER: Lokaler Dienst laeuft trotz Docker!)")
            issues_found.append("docker_host_conflict")
    elif is_standby:
        if not e3dc_srv["active"]:
            print(f"{GREEN}[OK]{RESET} Service Status: Inaktiv (Korrekt fuer {standby_label} im Standby)")
        else:
            print(f"{RED}[!]{RESET}  Service Status: Aktiv (FEHLER: Sollte im Standby gestoppt sein!)")
            issues_found.append("ha_running_in_standby")
    else:
        # Legacy-Systeme die noch C++ nutzen
        status_icon = f"{GREEN}[OK]{RESET}" if e3dc_srv["active"] else f"  [i] "
        enabled_icon = f"{GREEN}[OK]{RESET}" if e3dc_srv["enabled"] else f"  [i] "
        
        if e3dc_srv["active"] and live_srv["active"]:
            label = 'Aktiv (KRITISCHER FEHLER: Konflikt mit e3dc-live!)'
            status_icon = f"{RED}✗{RESET}"
            issues_found.append("v4_legacy_conflict")
        else:
            label = 'Aktiv (Legacy C++ Modus)' if e3dc_srv["active"] else 'Inaktiv (V4 Native Modus aktiv)'
            
        print(f"{status_icon} Service Status: {label}")
        print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if e3dc_srv['enabled'] else 'Deaktiviert (disabled)'}")

    # 2. Watchdog (Piguard)
    print("\n--- Watchdog (Piguard) ---")
    guard_srv = check_service_details("piguard")
    
    if guard_srv["status"] == "not_installed":
        print("⚪ Service 'piguard': Nicht installiert")
    else:
        status_icon = f"{GREEN}✓{RESET}" if guard_srv["active"] else f"{RED}✗{RESET}"
        enabled_icon = f"{GREEN}✓{RESET}" if guard_srv["enabled"] else f"{RED}✗{RESET}"
        
        print(f"{status_icon} Service Status: {'Aktiv (running)' if guard_srv['active'] else 'Inaktiv (stopped/failed)'}")
        print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if guard_srv['enabled'] else 'Deaktiviert (disabled)'}")
        
        if not guard_srv["active"]:
             print("\n  ⚠ Diagnose-Logs (letzte 10 Zeilen):")
             print("  " + "-" * 40)
             for line in guard_srv["log"].split("\n"):
                if line.strip():
                    print(f"    {line}")
             print("  " + "-" * 40)
             issues_found.append("watchdog_failed")

    # 2c. Energy Manager
    print("\n--- Energy Manager ---")
    lux_srv = check_service_details("energy_manager")
    
    if lux_srv["status"] == "not_installed":
        print("⚪ Service 'energy_manager': Nicht installiert")
    else:
        if print_standby_service_status(lux_srv, "energy_manager_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if lux_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if lux_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if lux_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if lux_srv['enabled'] else 'Deaktiviert (disabled)'}")
            
            if not lux_srv["active"] and lux_srv["enabled"]:
                 print("\n  ⚠ Diagnose-Logs (Letzte 10 Zeilen):")
                 print("  " + "-" * 40)
                 for line in lux_srv["log"].split("\n"):
                    if line.strip(): print(f"    {line}")
                 print("  " + "-" * 40)
                 issues_found.append("luxtronik_failed")
                 
    lux_ws_srv = check_service_details("e3dc-lux-live")
    if lux_ws_srv["status"] != "not_installed":
        print(f"  └─ WebSocket Daemon:")
        if print_standby_service_status(lux_ws_srv, "lux_live_running_in_standby", indent="     "):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if lux_ws_srv["active"] else f"{RED}✗{RESET}"
            print(f"     {status_icon} Status: {'Aktiv (running)' if lux_ws_srv['active'] else 'Inaktiv'}")

    idm_srv = check_service_details("e3dc-idm-live")
    if idm_srv["status"] != "not_installed":
        print(f"  └─ IDM Modbus Daemon:")
        if print_standby_service_status(idm_srv, "idm_live_running_in_standby", indent="     "):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if idm_srv["active"] else f"{RED}✗{RESET}"
            print(f"     {status_icon} Status: {'Aktiv (running)' if idm_srv['active'] else 'Inaktiv'}")

            if not idm_srv["active"]:
                 print("\n     ⚠ Diagnose-Logs (Letzte 10 Zeilen):")
                 print("     " + "-" * 37)
                 for line in idm_srv["log"].split("\n"):
                    if line.strip(): print(f"       {line}")
                 print("     " + "-" * 37)
                 issues_found.append("idm_live_failed")

    stiebel_srv = check_service_details("e3dc-stiebel-live")
    if stiebel_srv["status"] != "not_installed":
        print(f"  Stiebel ISG Live:")
        if print_standby_service_status(stiebel_srv, "stiebel_live_running_in_standby", indent="     "):
            pass
        else:
            status_icon = f"{GREEN}OK{RESET}" if stiebel_srv["active"] else f"{RED}FEHLER{RESET}"
            print(f"     {status_icon} Status: {'Aktiv (running)' if stiebel_srv['active'] else 'Inaktiv'}")

            if not stiebel_srv["active"]:
                 print("\n     Stiebel Diagnose-Logs (Letzte 10 Zeilen):")
                 print("     " + "-" * 37)
                 for line in stiebel_srv["log"].split("\n"):
                    if line.strip(): print(f"       {line}")
                 print("     " + "-" * 37)
                 issues_found.append("stiebel_live_failed")

    dimplex_srv = check_service_details("e3dc-dimplex-live")
    if dimplex_srv["status"] != "not_installed":
        print(f"  Dimplex WPM Live:")
        if print_standby_service_status(dimplex_srv, "dimplex_live_running_in_standby", indent="     "):
            pass
        else:
            status_icon = f"{GREEN}OK{RESET}" if dimplex_srv["active"] else f"{RED}FEHLER{RESET}"
            print(f"     {status_icon} Status: {'Aktiv (running)' if dimplex_srv['active'] else 'Inaktiv'}")

            if not dimplex_srv["active"]:
                 print("\n     Dimplex Diagnose-Logs (Letzte 10 Zeilen):")
                 print("     " + "-" * 37)
                 for line in dimplex_srv["log"].split("\n"):
                    if line.strip(): print(f"       {line}")
                 print("     " + "-" * 37)
                 issues_found.append("dimplex_live_failed")

    # 2d. High Availability (Cluster)
    print("\n--- High Availability (Cluster) ---")
    ha_srv = check_service_details("e3dc-ha")
    
    if ha_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-ha': Nicht installiert")
    else:
        if ha_mode == "shadow":
            print_standby_service_status(ha_srv, "ha_running_in_standby")
        else:
            status_icon = f"{GREEN}✓{RESET}" if ha_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if ha_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if ha_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if ha_srv['enabled'] else 'Deaktiviert (disabled)'}")

            # Zeige Cluster-State aus Ramdisk
            ha_status_file = "/var/www/html/ramdisk/ha_status.json"
            if os.path.exists(ha_status_file):
                try:
                    import json
                    with open(ha_status_file, "r") as f:
                        ha_data = json.load(f)
                    print(f"  Cluster Rolle: {ha_data.get('mode', 'off').upper()}")
                    print(f"  Cluster Status: {ha_data.get('state', 'unknown').upper()}")
                    print(f"  Partner (Peer) Online: {'JA' if ha_data.get('peer_online') else 'NEIN'}")
                except: pass

            if not ha_srv["active"] and ha_srv["enabled"]:
                 issues_found.append("ha_failed")

    # 2e. Notification Manager
    print("\n--- Notification Manager ---")
    not_srv = check_service_details("e3dc-notifier")
    
    if not_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-notifier': Nicht installiert")
    else:
        if ha_mode == "shadow" and print_standby_service_status(not_srv, "notifier_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if not_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if not_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if not_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if not_srv['enabled'] else 'Deaktiviert (disabled)'}")

    # 2f. WebSocket Server
    print("\n--- WebSocket Server ---")
    ws_srv = check_service_details("e3dc-websocket")
    
    if ws_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-websocket': Nicht installiert")
    else:
        if print_standby_service_status(ws_srv, "websocket_running_in_standby"):
            pass
        elif is_docker:
            if not ws_srv["active"]:
                print(f"{GREEN}✓{RESET} Service Status: Inaktiv (Korrekt für Docker-Modus)")
            else:
                print(f"{RED}✗{RESET} Service Status: Aktiv (FEHLER: Lokaler Dienst läuft trotz Docker!)")
            print(f"{GREEN}✓{RESET} Autostart:     {'Deaktiviert (disabled)' if not ws_srv['enabled'] else 'Aktiviert (enabled)'}")
        else:
            status_icon = f"{GREEN}✓{RESET}" if ws_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if ws_srv["enabled"] else f"{RED}✗{RESET}"
            
            print(f"{status_icon} Service Status: {'Aktiv (running)' if ws_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if ws_srv['enabled'] else 'Deaktiviert (disabled)'}")
            if not ws_srv["active"] and ws_srv["enabled"]:
                 issues_found.append("websocket_failed")

    # 2g. MQTT Hub
    print("\n--- Smart Home MQTT-Hub ---")
    mqtt_srv = check_service_details("e3dc-mqtt-hub")
    
    if mqtt_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-mqtt-hub': Nicht installiert")
    else:
        if print_standby_service_status(mqtt_srv, "mqtt_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if mqtt_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if mqtt_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if mqtt_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if mqtt_srv['enabled'] else 'Deaktiviert (disabled)'}")

    # 2h. Bluelink Client
    print("\n--- Fahrzeug Integration (Bluelink) ---")
    bl_srv = check_service_details("e3dc-bluelink")
    if bl_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-bluelink': Nicht installiert")
    else:
        if print_standby_service_status(bl_srv, "bluelink_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if bl_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if bl_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if bl_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if bl_srv['enabled'] else 'Deaktiviert (disabled)'}")

    # 2i. Native Wallbox Manager
    print("\n--- Native Wallbox Regelung ---")
    wb_srv = check_service_details("e3dc-wallbox-manager")
    if wb_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-wallbox-manager': Nicht installiert")
    else:
        if print_standby_service_status(wb_srv, "wallbox_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if wb_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if wb_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if wb_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if wb_srv['enabled'] else 'Deaktiviert (disabled)'}")

            # Datendatei prüfen
            wb_json = "/var/www/html/ramdisk/wallbox_native.json"
            if os.path.exists(wb_json):
                print(f"  {GREEN}✓{RESET} Wallbox-Daten:   Verfügbar (RAM-Disk)")
            else:
                if wb_srv["active"]:
                    print(f"  {RED}✗{RESET} Wallbox-Daten:   FEHLT (Obwohl Dienst läuft!)")
                    issues_found.append("wb_native_data_missing")

    # 2j. EPEX Manager (Börsenpreise)
    print("\n--- EPEX Manager (Börsen- & 15m Preise) ---")
    epex_srv = check_service_details("e3dc-epex-manager")
    if epex_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-epex-manager': Nicht installiert")
    else:
        if print_standby_service_status(epex_srv, "epex_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if epex_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if epex_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if epex_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if epex_srv['enabled'] else 'Deaktiviert (disabled)'}")

            if not epex_srv["active"] and epex_srv["enabled"]:
                print("\n  ⚠ Diagnose-Logs (Letzte 10 Zeilen):")
                print("  " + "-" * 40)
                for line in epex_srv["log"].split("\n"):
                    if line.strip(): print(f"    {line}")
                print("  " + "-" * 40)
                issues_found.append("epex_failed")

    # 2k. PV-Wetter Forecast Manager
    print("\n--- Wetter & PV-Forecast (Ensemble V4) ---")
    weather_srv = check_service_details("e3dc-weather-manager")
    if weather_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-weather-manager': Nicht installiert")
    else:
        if print_standby_service_status(weather_srv, "weather_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if weather_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if weather_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if weather_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if weather_srv['enabled'] else 'Deaktiviert (disabled)'}")

            if not weather_srv["active"] and weather_srv["enabled"]:
                print("\n  ⚠ Diagnose-Logs (Letzte 10 Zeilen):")
                print("  " + "-" * 40)
                for line in weather_srv["log"].split("\n"):
                    if line.strip(): print(f"    {line}")
                print("  " + "-" * 40)
                issues_found.append("weather_failed")

    # 2k2. Storage Simulator
    print("\n--- Storage Simulator (V4 KI Batterieplanung) ---")
    storage_srv = check_service_details("e3dc-storage-simulator")
    if storage_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-storage-simulator': Nicht installiert")
    else:
        if print_standby_service_status(storage_srv, "storage_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if storage_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if storage_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if storage_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if storage_srv['enabled'] else 'Deaktiviert (disabled)'}")

            if not storage_srv["active"] and storage_srv["enabled"]:
                print("\n  ⚠ Diagnose-Logs (Letzte 10 Zeilen):")
                print("  " + "-" * 40)
                for line in storage_srv["log"].split("\n"):
                    if line.strip(): print(f"    {line}")
                print("  " + "-" * 40)
                issues_found.append("storage_failed")

    # 2l. Mosquitto
    print("\n--- Lokaler MQTT Broker (Mosquitto) ---")
    mq_srv = check_service_details("mosquitto")
    if mq_srv["status"] == "not_installed":
        print("⚪ Service 'mosquitto': Nicht installiert")
    else:
        status_icon = f"{GREEN}✓{RESET}" if mq_srv["active"] else f"{RED}✗{RESET}"
        print(f"{status_icon} Service Status: {'Aktiv (running)' if mq_srv['active'] else 'Inaktiv'}")

    # 2j. Apache Webserver (NUR bei Bare Metal)
    if not is_docker:
        print("\n--- Webserver (Apache) ---")
        web_srv = check_service_details("apache2")
        if web_srv["status"] == "not_installed":
            print("⚪ Service 'apache2': Nicht installiert")
        else:
            status_icon = f"{GREEN}✓{RESET}" if web_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if web_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if web_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if web_srv['enabled'] else 'Deaktiviert (disabled)'}")
            
            if not web_srv["active"] or not web_srv["enabled"]:
                issues_found.append("apache_issue")

    # Matter Bridge
    print("\n--- Smart Home Matter Bridge ---")
    matter_srv = check_service_details("e3dc-matter-bridge")
    if matter_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc-matter-bridge': Nicht installiert")
    else:
        if print_standby_service_status(matter_srv, "matter_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}✓{RESET}" if matter_srv["active"] else f"{RED}✗{RESET}"
            enabled_icon = f"{GREEN}✓{RESET}" if matter_srv["enabled"] else f"{RED}✗{RESET}"
            print(f"{status_icon} Service Status: {'Aktiv (running)' if matter_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if matter_srv['enabled'] else 'Deaktiviert (disabled)'}")
            if not matter_srv["active"] and matter_srv["enabled"]:
                issues_found.append("matter_bridge_failed")

    # 2m. RSCP Live-Dienst (Python-nativ)
    print("\n--- RSCP Live-Dienst (Python-nativ) ---")
    live_srv = check_service_details("e3dc-live")
    if live_srv["status"] == "not_installed":
        print("  Service 'e3dc-live': Nicht installiert")
        print("  Tipp: sudo cp ~/Install/Installer/e3dc-live.service /etc/systemd/system/")
        print("        sudo systemctl enable --now e3dc-live")
    else:
        if print_standby_service_status(live_srv, "rscp_live_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}[OK]{RESET}" if live_srv["active"] else f"{RED}[!]{RESET} "
            enabled_icon = f"{GREEN}[OK]{RESET}" if live_srv["enabled"] else f"{RED}[!]{RESET} "
            print(f"{status_icon} Service Status: {'Aktiv (running)' if live_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if live_srv['enabled'] else 'Deaktiviert (disabled)'}")
            live_json = "/var/www/html/ramdisk/live_data_py.json"
            if os.path.exists(live_json):
                import time as _time
                age = _time.time() - os.path.getmtime(live_json)
                age_ok = age < 15
                a_icon = f"{GREEN}[OK]{RESET}" if age_ok else f"{RED}[!]{RESET} "
                print(f"  {a_icon} live_data_py.json: {'OK' if age_ok else 'VERALTET'} ({age:.0f}s alt)")
                if not age_ok:
                    issues_found.append("rscp_live_stale")
            else:
                if live_srv["active"]:
                    print(f"  {RED}[!]{RESET}  live_data_py.json: FEHLT (Dienst laeuft aber schreibt nicht)")
                    issues_found.append("rscp_live_no_data")
            if not live_srv["active"] and live_srv["enabled"]:
                issues_found.append("rscp_live_failed")

    # wp_type aus V4 JSON bestimmen
    wp_type = 0
    climate_enabled = False
    climate_control_enabled = False
    v4_path = "/var/www/html/data/e3dc_v4.json"
    if os.path.exists(v4_path):
        try:
            import json
            with open(v4_path, "r", encoding="utf-8") as f:
                v4_data = json.load(f)
                if 'wp_type' in v4_data:
                    wp_type = int(v4_data['wp_type'])
                climate_enabled = str(v4_data.get("climate_enable", "0")).strip().lower() in ("1", "true", "yes", "on")
                climate_control_enabled = str(v4_data.get("climate_control_enable", "0")).strip().lower() in ("1", "true", "yes", "on")
        except Exception: pass

    # 2n. Heizstab / Shelly Manager (wp_type=2) oder Shelly Pro3EM WP-Monitoring (wp_type=3)
    # Beide Typen laufen ueber e3dc-heizstab/heizstab_manager.py.
    hs_srv = check_service_details("e3dc-heizstab")
    if wp_type in (2, 3):
        print(f"\n--- Heizstab / Shelly Manager (wp_type={wp_type}) ---")
        if hs_srv["status"] == "not_installed":
            print("  Service 'e3dc-heizstab': Nicht installiert")
            print("  Tipp: sudo systemctl enable --now e3dc-heizstab")
        else:
            if print_standby_service_status(hs_srv, "heizstab_running_in_standby"):
                pass
            else:
                status_icon = f"{GREEN}[OK]{RESET}" if hs_srv["active"] else f"{RED}[!]{RESET} "
                enabled_icon = f"{GREEN}[OK]{RESET}" if hs_srv["enabled"] else f"{RED}[!]{RESET} "
                print(f"{status_icon} Service Status: {'Aktiv (running)' if hs_srv['active'] else 'Inaktiv'}")
                print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if hs_srv['enabled'] else 'Deaktiviert (disabled)'}")
                hs_json = "/var/www/html/ramdisk/heizstab_data.json"
                if os.path.exists(hs_json):
                    print(f"  {GREEN}[OK]{RESET} heizstab_data.json: Vorhanden")
                else:
                    if hs_srv["active"]:
                        print(f"  {RED}[!]{RESET}  heizstab_data.json: FEHLT")
                        issues_found.append("heizstab_no_data")
                if not hs_srv["active"] and hs_srv["enabled"]:
                    issues_found.append("heizstab_failed")

    # 2o. Klimaanlage / gemessener Zusatzverbraucher
    climate_srv = check_service_details("e3dc-climate-live")
    if climate_enabled or climate_srv["status"] != "not_installed":
        print("\n--- Klimaanlage Live ---")
        if climate_srv["status"] == "not_installed":
            print("  Service 'e3dc-climate-live': Nicht installiert")
            print("  Tipp: sudo systemctl enable --now e3dc-climate-live")
        elif print_standby_service_status(climate_srv, "climate_live_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}[OK]{RESET}" if climate_srv["active"] else f"{RED}[!]{RESET} "
            enabled_icon = f"{GREEN}[OK]{RESET}" if climate_srv["enabled"] else f"{RED}[!]{RESET} "
            print(f"{status_icon} Service Status: {'Aktiv (running)' if climate_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if climate_srv['enabled'] else 'Deaktiviert (disabled)'}")
            climate_json = "/var/www/html/ramdisk/climate_load.json"
            if os.path.exists(climate_json):
                print(f"  {GREEN}[OK]{RESET} climate_load.json: Vorhanden")
            else:
                if climate_srv["active"]:
                    print(f"  {RED}[!]{RESET}  climate_load.json: FEHLT")
                    issues_found.append("climate_live_no_data")
            if not climate_srv["active"] and climate_srv["enabled"]:
                issues_found.append("climate_live_failed")

    climate_control_srv = check_service_details("e3dc-climate-control")
    if climate_control_enabled or climate_control_srv["status"] != "not_installed":
        print("\n--- Klimaanlage Regel-Vorbereitung ---")
        if climate_control_srv["status"] == "not_installed":
            print("  Service 'e3dc-climate-control': Nicht installiert")
            print("  Tipp: sudo systemctl enable --now e3dc-climate-control")
        elif print_standby_service_status(climate_control_srv, "climate_control_running_in_standby"):
            pass
        else:
            status_icon = f"{GREEN}[OK]{RESET}" if climate_control_srv["active"] else f"{RED}[!]{RESET} "
            enabled_icon = f"{GREEN}[OK]{RESET}" if climate_control_srv["enabled"] else f"{RED}[!]{RESET} "
            print(f"{status_icon} Service Status: {'Aktiv (running)' if climate_control_srv['active'] else 'Inaktiv'}")
            print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if climate_control_srv['enabled'] else 'Deaktiviert (disabled)'}")
            climate_control_json = "/var/www/html/ramdisk/climate_control.json"
            if os.path.exists(climate_control_json):
                print(f"  {GREEN}[OK]{RESET} climate_control.json: Vorhanden")
            else:
                if climate_control_srv["active"]:
                    print(f"  {RED}[!]{RESET}  climate_control.json: FEHLT")
                    issues_found.append("climate_control_no_data")
            if not climate_control_srv["active"] and climate_control_srv["enabled"]:
                issues_found.append("climate_control_failed")

    # 3. System-Ressourcen
    print("\n--- System-Ressourcen ---")
    # CPU Temp
    res_temp = run_command("vcgencmd measure_temp")
    if res_temp['success']:
        temp = res_temp['stdout'].strip().replace("temp=", "")
        print(f"CPU Temperatur:    {temp}")

    # RAM-Disk
    res_ram = run_command("mount | grep '/var/www/html/ramdisk'")
    if res_ram['success'] and "tmpfs" in res_ram['stdout']:
        print(f"RAM-Disk:          Aktiv")
    else:
        print(f"RAM-Disk:          NICHT AKTIV")
        issues_found.append("ramdisk_missing")

    # Disk Usage
    res_disk = run_command("df -h /")
    if res_disk['success']:
        lines = res_disk['stdout'].splitlines()
        if len(lines) >= 2:
            parts = lines[1].split()
            usage_percent = parts[4]
            print(f"Speicherplatz (/): {usage_percent} belegt ({parts[3]} frei)")
            if int(usage_percent.strip('%')) > 90:
                print("  ⚠ WARNUNG: Speicherplatz fast voll!")
                issues_found.append("disk_full")
    
    # Uptime
    res_up = run_command("uptime -p")
    if res_up['success']:
        print(f"Laufzeit:          {res_up['stdout'].strip()}")

    # 4. Python Umgebung
    print("\n--- Python Umgebung ---")
    install_path = get_install_path()
    home_dir = get_home_dir(get_install_user())
    config = load_config()
    venv_name = config.get("venv_name", ".venv_e3dc")
    
    if "venv_name" in config and config["venv_name"] is None:
        print("Modus:             System-Python (global)")
    else:
        venv_full_path = os.path.join(install_path, venv_name)
        venv_home_path = os.path.join(home_dir, venv_name)
        
        if os.path.exists(venv_home_path):
            print(f"Modus:             Virtual Environment")
            print(f"Pfad:              {venv_home_path}")
            print(f"Status:            Aktiv")
        elif os.path.exists(venv_full_path):
            print(f"Modus:             Virtual Environment (Legacy)")
            print(f"Pfad:              {venv_full_path}")
            print(f"Status:            Aktiv")
        else:
            print(f"Modus:             Virtual Environment (konfiguriert)")
            print(f"Status:            FEHLT ({venv_name} nicht gefunden)")
            issues_found.append("venv_missing")

    # 5. Lösungsvorschläge
    if issues_found:
        print("\n=== 💡 Lösungsvorschläge ===")
        if "v4_legacy_conflict" in issues_found:
            print("• KRITISCHER KONFLIKT: C++ Legacy und Python V4 Modus laufen gleichzeitig!")
            print("  Behebe dies sofort mit: sudo systemctl stop e3dc e3dc-websocket")
            print("  und: sudo systemctl disable e3dc e3dc-websocket")
        if "docker_host_conflict" in issues_found:
            print("• Docker Konflikt: Es laufen noch alte, lokale Dienste!")
            print("  Behebe dies mit: sudo systemctl stop e3dc e3dc-websocket")
            print("  und: sudo systemctl disable e3dc e3dc-websocket")
        if "internet" in issues_found:
            print("• Internet: Prüfe Netzwerkkabel/WLAN und Router. Prüfe DNS-Einstellungen.")
        
        if "e3dc_not_running" in issues_found or "e3dc_service_failed" in issues_found:
            print("• E3DC C++ (Legacy) Fehler: In V4 wird e3dc-live (Python RSCP) genutzt.")
            print("  Prüfe: sudo systemctl status e3dc-live")
            print("  Neustart: sudo systemctl restart e3dc-live")
            print("  Oder nutze Menuepunkt '99' (Notfall-Modus).")          

        if "watchdog_failed" in issues_found:
            print("• Watchdog Fehler: Nutze Menüpunkt '15' (Watchdog konfigurieren) zur Reparatur.")

        if "disk_full" in issues_found:
            print("• Speicher voll: Lösche alte Logs oder Backups (z.B. in /var/www/html/tmp/).")

        if "ramdisk_missing" in issues_found:
            print("• RAM-Disk fehlt: Nutze Menüpunkt '14' (RAM-Disk einrichten).")
            
        if "luxtronik_failed" in issues_found:
            print("• Luxtronik Fehler: Prüfe 'journalctl -u energy_manager -e' oder die config.lux.json.")
            
        if "venv_missing" in issues_found:
            print("• Venv fehlt: Nutze Hauptmenüpunkt '8' (Systempakete vorbereiten) zur Paket-/venv-Reparatur.")
            print("  Alternativ im Expertenmenü: Punkt '21' (Python venv neu aufbauen).")
            
        if "ha_failed" in issues_found:
            print("• HA Cluster Fehler: Prüfe Logs mit 'journalctl -u e3dc-ha -e' auf Probleme.")
        if "ha_running_in_standby" in issues_found:
            print("• HA Cluster Warnung: Ein Dienst läuft fälschlicherweise auf dem Slave im Standby. Bitte 'journalctl -u e3dc-ha -e' prüfen.")
        if "websocket_failed" in issues_found:
            print("• WebSocket Fehler: Der Push-Server läuft nicht. Prüfe Logs mit 'journalctl -u e3dc-websocket -e'.")
        if "apache_issue" in issues_found:
            print("• Apache Webserver: Der Webserver ist inaktiv oder der Autostart ist deaktiviert.")
            print("  Behebung: Nutze Menüpunkt '2' (Rechte & Webportal reparieren) zur Re-Aktivierung.")
        if "matter_bridge_failed" in issues_found:
            print("• Matter Bridge Fehler: Der Node.js-Dienst läuft nicht. Prüfen Sie 'journalctl -u e3dc-matter-bridge -e'.")
        if "wb_native_data_missing" in issues_found:
             print("• Native Wallbox Fehler: Der Dienst läuft, aber liefert keine Daten.")
             print("  Prüfe die Logs mit: journalctl -u e3dc-wallbox-manager -e")
             print("  Oder versuche einen Neustart: sudo systemctl restart e3dc-wallbox-manager")
        if "epex_failed" in issues_found:
             print("• EPEX Manager Fehler: Der Börsenpreis-Fetcher stürzt ab.")
             print("  Prüfe die Logs mit: journalctl -u e3dc-epex-manager -e")
        if "weather_failed" in issues_found:
             print("• Wetter Forecast Fehler: Der PV-Prognose Dienst stürzt ab.")
             print("  Prüfe die Logs mit: journalctl -u e3dc-weather-manager -e")
        if "storage_failed" in issues_found:
             print("• Storage Simulator Fehler: Die V4 KI Batterieplanung stürzt ab.")
             print("  Prüfe die Logs mit: journalctl -u e3dc-storage-simulator -e")
    else:
        print(f"\n{GREEN}✓{RESET} Keine offensichtlichen Probleme gefunden.")

    print("\n==============================\n")
    log_task_completed("System-Statusprüfung")

register_command("20", "Systemstatus anzeigen", show_system_status, sort_order=20)
