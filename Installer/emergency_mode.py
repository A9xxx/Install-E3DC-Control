import os
import time
from .core import register_command
from .permissions import run_permissions_wizard
from .service_setup import install_e3dc_service, start_e3dc_control
from .install_watchdog import setup_watchdog_menu, create_service
from .system import install_system_packages
from .logging_manager import get_or_create_logger, log_task_completed
from .installer_config import load_config

logger = get_or_create_logger("emergency")

def run_emergency_mode():
    """Führt alle Reparatur-Maßnahmen nacheinander aus."""
    print("\n" + "!" * 60)
    print("!!! NOTFALL-MODUS / SYSTEM-REPARATUR !!!")
    print("!" * 60)
    print("Dieser Modus führt nacheinander folgende Schritte aus:")
    print("1. Systempakete überprüfen & nachinstallieren")
    print("2. Dateirechte prüfen & korrigieren (Permissions)")
    print("3. E3DC-Service neu einrichten & starten (Systemd)")
    print("4. Watchdog-Konfiguration überprüfen (Piguard)")
    print("\nDies kann helfen, wenn E3DC-Control nicht startet oder abstürzt.")
    
    if input("\nNotfall-Reparatur starten? (j/n): ").strip().lower() != 'j':
        print("Abbruch.")
        return

    logger.info("Notfall-Modus gestartet.")

    # 1. Systempakete
    print("\n" + "="*40)
    print(">>> SCHRITT 1/4: Systempakete")
    print("="*40)
    time.sleep(1)
    try:
        # Prüfe Konfiguration für venv
        config = load_config()
        use_venv = True
        if "venv_name" in config and config["venv_name"] is None:
            use_venv = False
        install_system_packages(use_venv=use_venv)
    except Exception as e:
        print(f"❌ Fehler in Schritt 1: {e}")
        logger.error(f"Fehler in Schritt 1 (Systempakete): {e}")

    # 2. Rechte
    print("\n" + "="*40)
    print(">>> SCHRITT 2/4: Rechte-Reparatur")
    print("="*40)
    time.sleep(1)
    try:
        # Wir rufen den Wizard auf. Der User muss ggf. mit 'j' bestätigen.
        run_permissions_wizard()
    except Exception as e:
        print(f"❌ Fehler in Schritt 2: {e}")
        logger.error(f"Fehler in Schritt 2 (Permissions): {e}")

    # 3. Service
    print("\n" + "="*40)
    print(">>> SCHRITT 3/4: Service-Reparatur")
    print("="*40)
    time.sleep(1)
    try:
        # Service installieren (bereinigt auch alte Crontabs)
        install_e3dc_service()
        # Versuchen zu starten
        start_e3dc_control()
    except Exception as e:
        print(f"❌ Fehler in Schritt 3: {e}")
        logger.error(f"Fehler in Schritt 3 (Service): {e}")

    # 4. Watchdog
    print("\n" + "="*40)
    print(">>> SCHRITT 4/4: Watchdog-Check")
    print("="*40)
    time.sleep(1)
    try:
        # Automatische Aktualisierung der Service-Datei (Fix für Wants=network-online.target)
        if os.path.exists("/usr/local/bin/pi_guard.sh"):
             print("Aktualisiere Watchdog-Service Definition...")
             create_service()

        print("Rufe Watchdog-Menü auf...")
        setup_watchdog_menu()
    except Exception as e:
        print(f"❌ Fehler in Schritt 4: {e}")
        logger.error(f"Fehler in Schritt 4 (Watchdog): {e}")

    print("\n" + "=" * 60)
    print("Notfall-Modus abgeschlossen.")
    print("Bitte prüfe nun, ob das System wieder läuft (Menü 'Status anzeigen').")
    print("=" * 60)
    log_task_completed("Notfall-Modus ausgeführt")

register_command("99", "NOTFALL-MODUS (System reparieren)", run_emergency_mode, sort_order=990)