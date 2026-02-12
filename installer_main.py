#!/usr/bin/env python3
# E3DC-Control Installer – modular & dynamisch

import os
import sys
import subprocess

# Basis-Pfade
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALLER_DIR = os.path.join(SCRIPT_DIR, "Installer")

# Sicherstellen, dass Installer-Paket importierbar ist
if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)


def check_python_version():
    """Prüft ob Python 3.7+ vorhanden ist."""
    if sys.version_info < (3, 7):
        print("✗ Fehler: Python 3.7+ erforderlich!")
        print(f"  Deine Version: {sys.version}")
        sys.exit(1)


def check_root_privileges():
    """Prüft ob Skript mit root-Rechten läuft."""
    if os.geteuid() != 0:
        print("✗ Fehler: Dieses Skript muss mit sudo ausgeführt werden!")
        print("Beispiel: sudo python3 installer_main.py")
        sys.exit(1)


def restart_installer():
    """Startet den Installer neu."""
    print("\n→ Starte Installer neu…\n")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def check_for_updates():
    """Prüft auf verfügbare Updates beim Start."""
    try:
        from Installer.self_update import check_and_update
        
        # Stille Update-Prüfung beim Start
        if check_and_update(silent=True):
            # Update wurde installiert, Skript neu starten
            restart_installer()
    
    except ImportError:
        # self_update-Modul nicht vorhanden, ignorieren
        pass
    except Exception as e:
        # Fehler bei Update-Prüfung, aber nicht fatalen (nur warnen)
        print(f"⚠ Warnung: Update-Prüfung fehlgeschlagen: {e}\n")


def check_permissions_on_startup():
    """Prüft Berechtigungen beim Start und fragt nach Korrektur."""
    try:
        from Installer.permissions import (
            check_permissions,
            check_webportal_permissions,
            check_file_permissions,
            fix_permissions,
            fix_webportal_permissions,
            fix_file_permissions
        )
        
        # Prüfe Berechtigungen
        issues = check_permissions()
        wp_issues = check_webportal_permissions()
        file_issues = check_file_permissions()
        
        has_issues = bool(issues) or bool(wp_issues) or bool(file_issues)
        
        if not has_issues:
            return  # Alle Berechtigungen OK, weitermachen
        
        # Berechtigungsprobleme gefunden - nachfragen
        print("\n⚠ Berechtigungsprobleme gefunden.")
        choice = input("Automatisch korrigieren? (j/n): ").strip().lower()
        
        if choice != "j":
            print("✗ Korrektur übersprungen.\n")
            return
        
        # Korrigiere Berechtigungen
        all_success = True
        
        if issues:
            success = fix_permissions(issues)
            all_success = all_success and success

        if wp_issues:
            success = fix_webportal_permissions(wp_issues)
            all_success = all_success and success
        
        if file_issues:
            success = fix_file_permissions(file_issues)
            all_success = all_success and success

        if all_success:
            print("\n✓ Alle Berechtigungen korrigiert.\n")
        else:
            print("\n⚠ Einige Berechtigungen konnten nicht korrigiert werden.\n")
    
    except ImportError:
        # permissions-Modul nicht vorhanden, ignorieren
        pass
    except Exception as e:
        # Fehler bei Berechtigungsprüfung, aber nicht fatale (nur warnen)
        print(f"⚠ Warnung: Berechtigungsprüfung fehlgeschlagen: {e}\n")


def main():
    """Haupteinstiegspunkt."""
    try:
        # Prüfungen
        check_python_version()
        check_root_privileges()

        # Importiere Core-Modul
        try:
            from Installer.core import run_main_menu
        except ImportError as e:
            print(f"✗ Fehler beim Laden des Installer-Moduls: {e}")
            print(f"   Prüfe ob das Verzeichnis '{INSTALLER_DIR}' existiert.")
            sys.exit(1)

        # Prüfe auf Updates (vor dem Hauptmenü)
        check_for_updates()

        # Prüfe Berechtigungen beim Start
        check_permissions_on_startup()

        # Starte Hauptmenü
        run_main_menu(restart_callback=restart_installer)

    except KeyboardInterrupt:
        print("\n\n✗ Installer unterbrochen (Ctrl+C).")
        sys.exit(0)
    except Exception as e:
        print(f"\n✗ Unerwarteter Fehler: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
