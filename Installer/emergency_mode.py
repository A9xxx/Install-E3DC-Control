import time
from .core import register_command
from .permissions import run_permissions_wizard
from .screen_cron import install_e3dc_service, start_e3dc_control
from .install_watchdog import setup_watchdog_menu
from .logging_manager import get_or_create_logger, log_task_completed

logger = get_or_create_logger("emergency")

def run_emergency_mode():
    """Führt alle Reparatur-Maßnahmen nacheinander aus."""
    print("\n" + "!" * 60)
    print("!!! NOTFALL-MODUS / SYSTEM-REPARATUR !!!")
    print("!" * 60)
    print("Dieser Modus führt nacheinander folgende Schritte aus:")
    print("1. Dateirechte prüfen & korrigieren (Permissions)")
    print("2. E3DC-Service neu einrichten & starten (Systemd)")
    print("3. Watchdog-Konfiguration überprüfen (Piguard)")
    print("\nDies kann helfen, wenn E3DC-Control nicht startet oder abstürzt.")
    
    if input("\nNotfall-Reparatur starten? (j/n): ").strip().lower() != 'j':
        print("Abbruch.")
        return

    logger.info("Notfall-Modus gestartet.")

    # 1. Rechte
    print("\n" + "="*40)
    print(">>> SCHRITT 1/3: Rechte-Reparatur")
    print("="*40)
    time.sleep(1)
    try:
        # Wir rufen den Wizard auf. Der User muss ggf. mit 'j' bestätigen.
        run_permissions_wizard()
    except Exception as e:
        print(f"❌ Fehler in Schritt 1: {e}")
        logger.error(f"Fehler in Schritt 1 (Permissions): {e}")

    # 2. Service
    print("\n" + "="*40)
    print(">>> SCHRITT 2/3: Service-Reparatur")
    print("="*40)
    time.sleep(1)
    try:
        # Service installieren (bereinigt auch alte Crontabs)
        install_e3dc_service()
        # Versuchen zu starten
        start_e3dc_control()
    except Exception as e:
        print(f"❌ Fehler in Schritt 2: {e}")
        logger.error(f"Fehler in Schritt 2 (Service): {e}")

    # 3. Watchdog
    print("\n" + "="*40)
    print(">>> SCHRITT 3/3: Watchdog-Check")
    print("="*40)
    time.sleep(1)
    try:
        print("Rufe Watchdog-Menü auf...")
        setup_watchdog_menu()
    except Exception as e:
        print(f"❌ Fehler in Schritt 3: {e}")
        logger.error(f"Fehler in Schritt 3 (Watchdog): {e}")

    print("\n" + "=" * 60)
    print("Notfall-Modus abgeschlossen.")
    print("Bitte prüfe nun, ob das System wieder läuft (Menü 'Status anzeigen').")
    print("=" * 60)
    log_task_completed("Notfall-Modus ausgeführt")
    input("Drücke ENTER um zum Menü zurückzukehren...")

register_command("99", "NOTFALL-MODUS (System reparieren)", run_emergency_mode, sort_order=990)