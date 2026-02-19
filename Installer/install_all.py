import os
import subprocess

from .core import register_command
from .permissions import run_permissions_wizard, check_permissions, check_webportal_permissions, check_file_permissions, fix_permissions, fix_webportal_permissions, fix_file_permissions
from .system import install_system_packages, install_e3dc_control
from .diagrammphp import install_diagramm
from .create_config import create_e3dc_config
from .strompreis_wizard import strompreis_wizard
from .screen_cron import install_screen_cron
from .ramdisk import setup_ramdisk
from .backup import backup_current_version
from .utils import run_command
from .installer_config import get_install_path, get_user_ids, get_www_data_gid
from .logging_manager import log_task_completed, log_error, log_warning

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


def collect_installation_errors():
    """Sammelt und gibt Fehler am Ende aus."""
    return {}


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
    print("  KOMPLÈTE E3DC-CONTROL INSTALLATION")
    print("=" * 60 + "\n")

    print("Diese Installation führt folgende Schritte in dieser Reihenfolge durch:\n")
    print("  1. Berechtigungen prüfen & korrigieren (Initial)")
    print("  2. Systempakete installieren (build-essential, Apache, PHP, etc.)")
    print("  3. E3DC-Control klonen & kompilieren")
    print("  4. Webportal & Diagramm-System einrichten")
    print("  5. E3DC-Konfiguration & Wallbox-Datei erstellen")
    print("  6. Strompreise konfigurieren (optional)")
    print("  7. Screen-Session & Cronjob einrichten")
    print("  8. RAM-Disk & Live-Status-Grabber einrichten")
    print("  9. Berechtigungen verifizieren & final setzen")
    print("  10. Backup der Initialversion erstellen\n")

    confirm = input("Alle Schritte ausführen? (j/n): ").strip().lower()
    if confirm != "j":
        print("→ Abgebrochen.\n")
        return

    # Fehler-Sammler für abschließende Ausgabe
    errors = {}

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
    print("SCHRITT 2 / 10: Systempakete installieren")
    print("=" * 60)
    install_system_packages()

    # =========================================================
    # SCHRITT 3: E3DC-Control Binary
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 3 / 10: E3DC-Control klonen & kompilieren")
    print("=" * 60)
    if not install_e3dc_control():
        print("\n✗ E3DC-Control Installation fehlgeschlagen!")
        log_error("install_all", "E3DC-Control Installation fehlgeschlagen")
        print("→ Installation abgebrochen.\n")
        return

    # =========================================================
    # SCHRITT 4: Diagramm & PHP
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 4 / 10: Webportal & Diagramm-System")
    print("=" * 60)
    try:
        install_diagramm()
        
        # Starte Apache neu nach Installation
        if not restart_apache():
            print("⚠ Apache-Neustart fehlgeschlagen – fahre fort.\n")
    except Exception as e:
        print(f"⚠ Warnung bei Diagramm-Installation: {e}")
        print("→ Installation wird fortgesetzt.\n")

    # =========================================================
    # SCHRITT 5: Konfiguration
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 5 / 10: E3DC-Konfiguration erstellen")
    print("=" * 60)
    try:
        create_e3dc_config()
        
        # Erstelle auch leere wallbox.txt wenn noch nicht vorhanden
        wallbox_file = os.path.join(INSTALL_PATH, "e3dc.wallbox.txt")
        if not os.path.exists(wallbox_file):
            try:
                with open(wallbox_file, "w") as f:
                    f.write("# Wallbox Konfiguration\n")
                    f.write("# Wird automatisch von der Weboberfläche generiert\n")
                uid, _ = get_user_ids()
                os.chown(wallbox_file, uid, get_www_data_gid())  # <user>:www-data
                os.chmod(wallbox_file, 0o664)
                print(f"✓ Wallbox-Datei erstellt: {wallbox_file}\n")
            except Exception as e:
                print(f"⚠ Wallbox-Datei konnte nicht erstellt werden: {e}\n")
                errors["wallbox"] = str(e)
    except Exception as e:
        print(f"⚠ Warnung bei Konfiguration: {e}")
        print("→ Installation wird fortgesetzt (Config kann später angepasst werden).\n")
        errors["config"] = str(e)

    # =========================================================
    # SCHRITT 6: Strompreise (optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 6 / 10: Strompreise (optional)")
    print("=" * 60)
    choice = input("Strompreise jetzt konfigurieren? (j/n): ").strip().lower()
    if choice == "j":
        try:
            strompreis_wizard()
        except Exception as e:
            print(f"⚠ Warnung bei Strompreis-Konfiguration: {e}\n")
            errors["strompreis"] = str(e)
    else:
        print("→ Übersprungen (kann später hinzugefügt werden).\n")

    # =========================================================
    # SCHRITT 7: Screen & Cronjob
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 7 / 10: Screen-Session & Cronjob einrichten")
    print("=" * 60)
    try:
        install_screen_cron()
    except Exception as e:
        print(f"⚠ Warnung bei Screen/Cronjob-Setup: {e}\n")
        errors["screen_cron"] = str(e)

    # =========================================================
    # SCHRITT 8: RAM-Disk & Live-Status
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 8 / 10: RAM-Disk & Live-Status-Grabber einrichten")
    print("=" * 60)
    try:
        setup_ramdisk()
    except Exception as e:
        print(f"⚠ Warnung bei RAM-Disk/Live-Status Setup: {e}\n")
        errors["ramdisk"] = str(e)

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
    print("\n" + "=" * 60)
    print("SCHRITT 10 / 10: Backup erstellen")
    print("=" * 60)
    print("→ Erstelle Backup der Initialversion…")
    try:
        backup_current_version()
    except Exception as e:
        print(f"⚠ Warnung bei Backup: {e}\n")
        errors["backup"] = str(e)

    # =========================================================
    # Abschluss + Fehlersammlung
    # =========================================================
    print("\n" + "=" * 60)
    if errors:
        print("✓ INSTALLATION ABGESCHLOSSEN (mit Warnungen)")
    else:
        print("✓ KOMPLÈTE INSTALLATION ERFOLGREICH ABGESCHLOSSEN")
    print("=" * 60 + "\n")

    # Fehler-Zusammenfassung am Ende
    if errors:
        print("⚠ FOLGENDE WARNUNGEN TRATEN AUF:\n")
        for step, error in errors.items():
            print(f"  [{step}] {error}")
        print()

    print("Nächste Schritte:")
    print("  1. Webportal öffnen:")
    print("     → http://localhost oder http://<PI-IP>\n")
    print("  2. E3DC-Control starten:")
    print("     → Mit 'screen -r E3DC' überprüfen (läuft bei Reboot automatisch)\n")
    print("  3. Bei Fehlern: Config überprüfen")
    if "config" in errors:
        print(f"     → {INSTALL_PATH}/e3dc.config.txt\n")


register_command("16", "Alles installieren", install_all_main, sort_order=160)