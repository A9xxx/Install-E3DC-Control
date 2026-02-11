import os
import subprocess

from .core import register_command
from .permissions import run_permissions_wizard, check_permissions, check_webportal_permissions, check_file_permissions, fix_permissions, fix_webportal_permissions, fix_file_permissions
from .system import install_system_packages, install_e3dc_control
from .diagrammphp import install_diagramm
from .create_config import create_e3dc_config
from .strompreis_wizard import strompreis_wizard
from .screen_cron import install_screen_cron
from .backup import backup_current_version
from .utils import run_command

INSTALL_PATH = "/home/pi/E3DC-Control"


def restart_apache():
    """Startet Apache2 neu."""
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
    
    # Verzeichnis-Rechte
    dir_issues = check_permissions()
    if dir_issues:
        fix_permissions(dir_issues)
    
    # Webportal-Rechte
    wp_issues = check_webportal_permissions()
    if wp_issues:
        fix_webportal_permissions(wp_issues)
    
    # Datei-Rechte
    files_to_check = [
        (f"{INSTALL_PATH}/e3dc.config.txt", "664"),
        (f"{INSTALL_PATH}/e3dc.wallbox.txt", "664"),
        (f"{INSTALL_PATH}/e3dc.strompreis.txt", "664"),
        (f"{INSTALL_PATH}/plot_soc_changes.py", "755")
    ]
    
    for file_path, mode in files_to_check:
        if os.path.exists(file_path):
            file_issues = check_file_permissions()
            if file_issues:
                fix_file_permissions(file_issues)
    
    print("✓ Finale Berechtigungen gesetzt.\n")


def install_all_main():
    """Komplette Installation mit korrekter Reihenfolge."""
    print("\n" + "=" * 60)
    print("  KOMPLÈTE E3DC-CONTROL INSTALLATION")
    print("=" * 60 + "\n")

    print("Diese Installation führt folgende Schritte in dieser Reihenfolge durch:\n")
    print("  1. Berechtigungen prüfen & setzen (Initial)")
    print("  2. Systempakete installieren (build-essential, Apache, PHP, etc.)")
    print("  3. E3DC-Control klonen & kompilieren")
    print("  4. Webportal & Diagramm-System einrichten")
    print("  5. E3DC-Konfiguration erstellen")
    print("  6. Strompreise konfigurieren (optional)")
    print("  7. Screen-Session & Cronjob einrichten")
    print("  8. FINALE BERECHTIGUNGSSETZUNG")
    print("  9. Backup der Initialversion erstellen\n")

    confirm = input("Alle Schritte ausführen? (j/n): ").strip().lower()
    if confirm != "j":
        print("→ Abgebrochen.\n")
        return

    # =========================================================
    # SCHRITT 1: Berechtigungen (Initial)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 1 / 9: Berechtigungen prüfen & korrigieren (Initial)")
    print("=" * 60)
    run_permissions_wizard()

    # =========================================================
    # SCHRITT 2: Systempakete
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 2 / 9: Systempakete installieren")
    print("=" * 60)
    install_system_packages()

    # =========================================================
    # SCHRITT 3: E3DC-Control Binary
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 3 / 9: E3DC-Control klonen & kompilieren")
    print("=" * 60)
    if not install_e3dc_control():
        print("\n✗ E3DC-Control Installation fehlgeschlagen!")
        print("→ Installation abgebrochen.\n")
        return

    # =========================================================
    # SCHRITT 4: Diagramm & PHP
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 4 / 9: Webportal & Diagramm-System")
    print("=" * 60)
    install_diagramm()
    
    # Starte Apache neu nach Installation
    restart_apache()

    # =========================================================
    # SCHRITT 5: Konfiguration
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 5 / 9: E3DC-Konfiguration erstellen")
    print("=" * 60)
    create_e3dc_config()

    # =========================================================
    # SCHRITT 6: Strompreise (optional)
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 6 / 9: Strompreise (optional)")
    print("=" * 60)
    choice = input("Strompreise jetzt konfigurieren? (j/n): ").strip().lower()
    if choice == "j":
        strompreis_wizard()
    else:
        print("→ Übersprungen (kann später hinzugefügt werden).\n")

    # =========================================================
    # SCHRITT 7: Screen & Cronjob
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 7 / 9: Screen-Session & Cronjob einrichten")
    print("=" * 60)
    install_screen_cron()

    # =========================================================
    # SCHRITT 8: FINALE BERECHTIGUNGEN
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 8 / 9: Finale Berechtigungssetzung")
    print("=" * 60)
    finalize_permissions()

    # =========================================================
    # SCHRITT 9: Backup
    # =========================================================
    print("\n" + "=" * 60)
    print("SCHRITT 9 / 9: Backup erstellen")
    print("=" * 60)
    print("→ Erstelle Backup der Initialversion…")
    backup_current_version()

    # =========================================================
    # Abschluss
    # =========================================================
    print("\n" + "=" * 60)
    print("✓ KOMPLÈTE INSTALLATION ERFOLGREICH ABGESCHLOSSEN")
    print("=" * 60 + "\n")

    print("Nächste Schritte:")
    print("  1. Webportal öffnen:")
    print("     → http://localhost oder http://<PI-IP>\n")
    print("  2. E3DC-Control starten:")
    print("     → Menüpunkt '7' oder 'screen -r E3DC'\n")
    print("  3. Ausgabe ansehen:")
    print("     → 'screen -r E3DC'\n")
    print("  4. Session verlassen:")
    print("     → Ctrl+A Ctrl+D\n")


register_command("11", "Alles installieren", install_all_main, sort_order=110)