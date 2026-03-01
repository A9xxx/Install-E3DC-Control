import os
import subprocess

from .core import register_command
from .permissions import run_permissions_wizard, check_permissions, check_webportal_permissions, check_file_permissions, fix_permissions, fix_webportal_permissions, fix_file_permissions
from .system import install_system_packages, install_e3dc_control
from .diagrammphp import install_diagramm
from .create_config import create_e3dc_config
from .strompreis_wizard import strompreis_wizard
from .screen_cron import install_e3dc_service
from .ramdisk import setup_ramdisk
from .backup import backup_current_version
from .utils import run_command
from .installer_config import get_install_path, get_user_ids, get_www_data_gid
from .logging_manager import setup_installation_loggers, print_installation_summary, log_task_completed, log_error, log_warning
from .task_executor import safe_execute_task

INSTALL_PATH = get_install_path()


def restart_apache():
    """Startet Apache2 neu. Gibt True zurück bei Erfolg."""
    print("→ Starte Apache2 neu…")
    try:
        result = run_command("sudo systemctl restart apache2", timeout=10)
        if result['success']:
            print("✓ Apache2 neugestartet\n")
            return True
        else:
            print("⚠ Apache2 Neustart fehlgeschlagen\n")
            return False
    except Exception as e:
        print(f"⚠ Fehler: {e}\n")
        return False


def finalize_permissions():
    """Setzt alle Berechtigungen endgültig nach der Installation."""
    print("\n→ Setze finale Berechtigungen…\n")
    
    try:
        # Verzeichnis-Rechte
        dir_issues = check_permissions()
        if dir_issues:
            fix_permissions(dir_issues)
        
        # Webportal-Rechte
        wp_issues = check_webportal_permissions()
        if wp_issues:
            fix_webportal_permissions(wp_issues)
        
        # Datei-Rechte (alle auf einmal prüfen und fixen)
        file_issues = check_file_permissions()
        if file_issues:
            fix_file_permissions(file_issues)
        
        print("✓ Finale Berechtigungen gesetzt.\n")
        log_task_completed("Finale Berechtigungsprüfung", details="Alle Berechtigungen validiert")
        
    except Exception as e:
        log_error("install_all", f"Fehler bei finalen Berechtigungen: {e}", e)
        print(f"✗ Fehler bei Berechtigungen: {e}\n")
        raise


def install_all_main():
    """Komplette Installation mit korrekter Reihenfolge."""
    print("\n" + "=" * 60)
    print("  KOMPLETTE E3DC-CONTROL INSTALLATION")
    print("=" * 60 + "\n")

    print("Diese Installation führt folgende Schritte in dieser Reihenfolge durch:\n")
    print("  1. Berechtigungen prüfen & korrigieren (Initial)")
    print("  2. Systempakete installieren (build-essential, Apache, PHP, etc.)")
    print("  3. E3DC-Control klonen & kompilieren")
    print("  4. Webportal & Diagramm-System einrichten")
    print("  5. E3DC-Konfiguration & Wallbox-Datei erstellen")
    print("  6. Strompreise konfigurieren (optional)")
    print("  7. E3DC-Control Service einrichten (Systemd)")
    print("  8. RAM-Disk & Live-Status-Grabber einrichten")
    print("  9. Berechtigungen verifizieren & final setzen")
    print("  10. Backup der Initialversion erstellen\n")

    confirm = input("Alle Schritte ausführen? (j/n): ").strip().lower()
    if confirm != "j":
        print("→ Abgebrochen.\n")
        return

    # Logging für diese "Alles installieren"-Sitzung initialisieren
    setup_installation_loggers()
    failed_steps = []

    # =========================================================
    # SCHRITT 1: Berechtigungen (Initial) - PRÜFE & KORRIGIERE SOFORT
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 1 / 10: Berechtigungen prüfen & korrigieren (Initial)")
    print("=" * 60)
    print("\n→ Prüfe und korrigiere Berechtigungen…\n")
    
    # Verzeichnis-Rechte
    dir_issues = check_permissions()
    if dir_issues:
        print("⚠ Verzeichnis-Berechtigungen werden korrigiert…")
        fix_permissions(dir_issues)
    else:
        print("✓ Verzeichnis-Berechtigungen OK")
    
    # Webportal-Rechte
    wp_issues = check_webportal_permissions()
    if wp_issues:
        print("⚠ Webportal-Berechtigungen werden korrigiert…")
        fix_webportal_permissions(wp_issues)
    else:
        print("✓ Webportal-Berechtigungen OK")
    
    # Datei-Rechte (nur vorhandene Dateien prüfen)
    file_issues = check_file_permissions()
    if file_issues:
        print("⚠ Datei-Berechtigungen werden korrigiert…")
        fix_file_permissions(file_issues)
    else:
        print("✓ Datei-Berechtigungen OK\n")

    # =========================================================
    # SCHRITT 2: Systempakete
    # =========================================================
    print("\n" + "=" * 60)
    if not safe_execute_task("SCHRITT 2/10: Systempakete installieren", install_system_packages):
        failed_steps.append("Systempakete")

    # =========================================================
    # SCHRITT 3: E3DC-Control Binary
    # =========================================================
    print("\n" + "=" * 60)
    if not safe_execute_task("SCHRITT 3/10: E3DC-Control klonen & kompilieren", install_e3dc_control):
        failed_steps.append("E3DC-Control")
        print("\n✗ Kritischer Fehler: E3DC-Control konnte nicht installiert werden. Installation wird abgebrochen.\n")
        print_installation_summary()
        return

    # =========================================================
    # SCHRITT 4: Diagramm & PHP
    # =========================================================
    print("\n" + "=" * 60)
    def install_diagramm_and_restart_apache():
        install_diagramm()
        if not restart_apache():
            log_warning("install_all", "Apache-Neustart nach Diagramm-Installation fehlgeschlagen.")

    if not safe_execute_task("SCHRITT 4/10: Webportal & Diagramm-System einrichten", install_diagramm_and_restart_apache):
        failed_steps.append("Diagramm")

    # =========================================================
    # SCHRITT 5: Konfiguration
    # =========================================================
    print("\n" + "=" * 60)
    def create_configs_task():
        create_e3dc_config()
        # Erstelle auch leere wallbox.txt wenn noch nicht vorhanden
        wallbox_file = os.path.join(INSTALL_PATH, "e3dc.wallbox.txt")
        if not os.path.exists(wallbox_file):
            try:
                with open(wallbox_file, "w") as f:
                    f.write("# Wallbox Konfiguration\n")
                    f.write("# Wird automatisch von der Weboberfläche generiert\n")
                uid, _ = get_user_ids()
                os.chown(wallbox_file, uid, get_www_data_gid())
                os.chmod(wallbox_file, 0o664)
                log_task_completed("Leere Wallbox-Datei erstellt", details=wallbox_file)
                print(f"✓ Wallbox-Datei erstellt: {wallbox_file}\n")
            except Exception as e:
                log_warning("install_all", f"Leere Wallbox-Datei konnte nicht erstellt werden: {e}")
                print(f"⚠ Wallbox-Datei konnte nicht erstellt werden: {e}\n")

    if not safe_execute_task("SCHRITT 5/10: E3DC-Konfiguration erstellen", create_configs_task):
        failed_steps.append("Konfiguration")

    # =========================================================
    # SCHRITT 6: Strompreise (optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 6/10: Strompreise (optional)")
    print("=" * 60)
    choice = input("Strompreise jetzt konfigurieren? (j/n): ").strip().lower()
    if choice == "j":
        try:
            strompreis_wizard()
            log_task_completed("Strompreise konfiguriert")
        except Exception as e:
            log_error("Strompreis-Wizard", f"Fehler bei der Strompreis-Konfiguration: {e}", e)
            failed_steps.append("Strompreise")
    else:
        print("→ Übersprungen (kann später hinzugefügt werden).\n")

    # =========================================================
    # SCHRITT 7: Service (Systemd)
    # =========================================================
    if not safe_execute_task("SCHRITT 7/10: E3DC-Control Service einrichten (Systemd)", install_e3dc_service):
        failed_steps.append("Service")

    # =========================================================
    # SCHRITT 8: RAM-Disk & Live-Status
    # =========================================================
    if not safe_execute_task("SCHRITT 8/10: RAM-Disk & Live-Status einrichten", setup_ramdisk):
        failed_steps.append("RAM-Disk")

    # =========================================================
    # SCHRITT 9: FINALE BERECHTIGUNGEN (Sicherheitscheck)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 9 / 10: Berechtigungen verifizieren & final setzen")
    print("=" * 60)
    print("\n→ Verifiziere alle Berechtigungen nach der Installation…\n")
    finalize_permissions()

    # =========================================================
    # SCHRITT 10: Backup
    # =========================================================
    if not safe_execute_task("SCHRITT 10/10: Backup der Initialversion erstellen", backup_current_version):
        failed_steps.append("Backup")

    # =========================================================
    # Abschluss + Fehlersammlung
    # =========================================================
    print_installation_summary()

    print("Nächste Schritte:")
    print("  1. Webportal öffnen:")
    print("     → http://localhost oder http://<PI-IP>\n")
    print("  2. E3DC-Control starten:")
    print("     → Mit 'screen -r E3DC' überprüfen (läuft bei Reboot automatisch)\n")


register_command("18", "Alles installieren", install_all_main, sort_order=180)