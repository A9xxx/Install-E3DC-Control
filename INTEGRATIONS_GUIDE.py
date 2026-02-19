"""
INTEGRATIONS-GUIDE: Logging in install_all.py
==============================================

Diese Datei zeigt, wie install_all.py die neuen Logging-Features nutzt.

VORHER:
-------
def main():
    while True:
        print("1. Installation")
        choice = input("Wähle: ").strip()
        if choice == "1":
            install_system()

NACHHER:
--------
"""

import sys
from .logging_manager import setup_installation_loggers, print_installation_summary, log_error
from .task_executor import safe_menu_action
from .core import get_registered_commands

# Optional: Weitere Module
try:
    from . import permissions
    from . import backup
    from . import rollback
    from . import uninstall
    from . import ramdisk
    from . import update
except ImportError as e:
    print(f"⚠ Warnung beim Laden von Modulen: {e}")


def display_menu(commands_dict):
    """Zeige Menü mit sortierten Einträgen."""
    print("\n" + "="*50)
    print("E3DC INSTALLATION & VERWALTUNG")
    print("="*50 + "\n")
    
    # Sortiere nach sort_order
    sorted_commands = sorted(
        commands_dict.items(),
        key=lambda x: x[1].get("sort_order", 999)
    )
    
    for num, cmd_data in sorted_commands:
        name = cmd_data.get("name", "?")
        print(f"  {num:2} ) {name}")
    
    print(f"  {'0':2} ) Beenden")
    print("="*50)


def main():
    """
    Hauptschleife des Installers mit Fehlerbehandlung und Logging.
    """
    # Initialisiere Logging-System
    try:
        setup_installation_loggers()
    except Exception as e:
        print(f"✗ Kritischer Fehler beim Setup von Logging: {e}")
        sys.exit(1)
    
    install_logger = __import__("logging").getLogger("install")
    install_logger.info("="*60)
    install_logger.info("E3DC Installer gestartet")
    install_logger.info("="*60)
    
    # Hol registrierte Commands
    try:
        commands = get_registered_commands()  # Aus core.py
    except Exception as e:
        log_error("MainMenu", f"Fehler beim Laden der Commands: {e}", e)
        print("✗ Kritischer Fehler beim Laden des Menüs")
        sys.exit(1)
    
    # Hauptschleife
    while True:
        try:
            display_menu(commands)
            choice = input("\nWähle Menüpunkt: ").strip().lower()
            
            if choice == "0":
                print("\nAuf Wiedersehen!\n")
                install_logger.info("Installer vom Benutzer beendet")
                break
            
            if choice not in commands:
                print(f"✗ Ungültige Auswahl: {choice}")
                continue
            
            cmd_data = commands[choice]
            menu_name = cmd_data["name"]
            menu_func = cmd_data["function"]
            
            # Führe mit Fehlerbehandlung aus
            success = safe_menu_action(choice, menu_name, menu_func)
            
            if not success:
                print("⚠ Task beendet mit Fehlern (siehe error.log)")
            
            # Optionaler Pause nach Menüpunkt
            input("\nDrücke ENTER zum Fortfahren…")
        
        except KeyboardInterrupt:
            print("\n\n✗ Installation vom Benutzer unterbrochen (Ctrl+C)")
            install_logger.warning("Installation vom Benutzer unterbrochen")
            break
        
        except Exception as e:
            log_error("MainMenu", f"Unerwarteter Fehler: {e}", e)
            print(f"\n✗ KRITISCHER FEHLER: {e}")
            print("  Bitte error.log prüfen")
    
    # Zeige finale Zusammenfassung
    print_installation_summary()


if __name__ == "__main__":
    main()
