import os
import sys
from .core import register_command
from .utils import run_command
from .logging_manager import get_or_create_logger, log_task_completed
from .installer_config import get_install_user

status_logger = get_or_create_logger("status_check")

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

def check_screen_session(session_name):
    """Prüft ob eine Screen-Session existiert."""
    install_user = get_install_user()
    
    # Check user screens
    res = run_command(f"sudo -u {install_user} screen -ls")
    if session_name in res['stdout']:
        return True
        
    # Check root screens (falls versehentlich als root gestartet)
    res = run_command("screen -ls")
    if session_name in res['stdout']:
        return True
        
    return False

def show_system_status():
    """Zeigt den Status aller relevanten Dienste an."""
    print("\n=== System-Status & Diagnose ===\n")
    status_logger.info("Starte System-Statusprüfung.")
    
    issues_found = []

    # 0. Internet Check
    print("--- Netzwerk ---")
    if check_internet_connection():
        print("✓ Internetverbindung: OK (Ping 8.8.8.8)")
    else:
        print("✗ Internetverbindung: FEHLGESCHLAGEN")
        issues_found.append("internet")

    # 1. E3DC-Control Service
    print("\n--- E3DC-Control ---")
    e3dc_srv = check_service_details("e3dc")
    
    if e3dc_srv["status"] == "not_installed":
        print("⚪ Service 'e3dc': Nicht installiert (Systemd)")
        # Fallback Check Screen
        if check_screen_session("E3DC"):
            print("✓ Screen-Session 'E3DC': Gefunden (Legacy Mode)")
        else:
            print("✗ Screen-Session 'E3DC': Nicht gefunden")
            print("  -> E3DC-Control läuft nicht.")
            issues_found.append("e3dc_not_running")
    else:
        status_icon = "✓" if e3dc_srv["active"] else "✗"
        enabled_icon = "✓" if e3dc_srv["enabled"] else "✗"
        
        print(f"{status_icon} Service Status: {'Aktiv (running)' if e3dc_srv['active'] else 'Inaktiv (stopped/failed)'}")
        print(f"{enabled_icon} Autostart:     {'Aktiviert (enabled)' if e3dc_srv['enabled'] else 'Deaktiviert (disabled)'}")
        
        if not e3dc_srv["active"]:
            print("\n  ⚠ Diagnose-Logs (letzte 10 Zeilen):")
            print("  " + "-" * 40)
            for line in e3dc_srv["log"].split("\n"):
                if line.strip():
                    print(f"    {line}")
            print("  " + "-" * 40)
            print("  Tipp: Nutze 'journalctl -u e3dc -e' für mehr Details.")
            issues_found.append("e3dc_service_failed")

    # 2. Watchdog (Piguard)
    print("\n--- Watchdog (Piguard) ---")
    guard_srv = check_service_details("piguard")
    
    if guard_srv["status"] == "not_installed":
        print("⚪ Service 'piguard': Nicht installiert")
    else:
        status_icon = "✓" if guard_srv["active"] else "✗"
        enabled_icon = "✓" if guard_srv["enabled"] else "✗"
        
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

    # 3. System-Ressourcen
    print("\n--- System-Ressourcen ---")
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

    # 4. Lösungsvorschläge
    if issues_found:
        print("\n=== 💡 Lösungsvorschläge ===")
        if "internet" in issues_found:
            print("• Internet: Prüfe Netzwerkkabel/WLAN und Router. Prüfe DNS-Einstellungen.")
        
        if "e3dc_not_running" in issues_found:
            print("• E3DC läuft nicht: Nutze Menüpunkt '12' (Starten) oder '11' (Service einrichten).")
            print("  Falls es sofort wieder abstürzt: Prüfe 'e3dc.config.txt' und Logs.")
            print("  Oder nutze Menüpunkt '99' (Notfall-Modus).")
        
        if "e3dc_service_failed" in issues_found:
            print("• E3DC Service Fehler: Prüfe Logs oben. Oft sind es Rechteprobleme oder Config-Fehler.")
            print("  Versuche Menüpunkt '2' (Rechte korrigieren) oder '11' (Service neu einrichten).")
            print("  Auch Menüpunkt '4' (Neuinstallation) kann bei korrupten Binaries helfen.")
            print("  NEU: Nutze Menüpunkt '99' (Notfall-Modus) für eine geführte Reparatur.")

        if "watchdog_failed" in issues_found:
            print("• Watchdog Fehler: Nutze Menüpunkt '15' (Watchdog konfigurieren) zur Reparatur.")

        if "disk_full" in issues_found:
            print("• Speicher voll: Lösche alte Logs oder Backups (z.B. in /var/www/html/tmp/).")
    else:
        print("\n✓ Keine offensichtlichen Probleme gefunden.")

    print("\n==============================\n")
    log_task_completed("System-Statusprüfung")
    input("Drücke ENTER, um zum Menü zurückzukehren...")

register_command("21", "System-Status anzeigen", show_system_status, sort_order=210)