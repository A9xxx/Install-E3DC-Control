import os
import time
import shutil
from .core import register_command
from .permissions import run_permissions_wizard
from .install_watchdog import setup_watchdog_menu, create_service
from .utils import install_system_packages
from .logging_manager import get_or_create_logger, log_task_completed
from .installer_config import load_config

logger = get_or_create_logger("emergency")


def cleanup_pycache(start_path):
    """
    Bereinigt alle __pycache__-Ordner in einem gegebenen Pfad.
    """
    logger.info(f"Starte __pycache__-Bereinigung in {start_path}")
    
    for root, dirs, files in os.walk(start_path):
        if "__pycache__" in dirs:
            pycache_path = os.path.join(root, "__pycache__")
            logger.info(f"Entferne {pycache_path}")
            try:
                shutil.rmtree(pycache_path)
                print(f"✓ Cache in {os.path.basename(root)} entfernt.")
            except Exception as e:
                logger.error(f"Fehler beim Entfernen von {pycache_path}: {e}")
                print(f"⚠ Fehler beim Entfernen des Caches in {os.path.basename(root)}.")
    
    logger.info("__pycache__-Bereinigung abgeschlossen.")


def run_emergency_mode():
    """Führt alle Reparatur-Maßnahmen nacheinander aus."""
    # Cache-Bereinigung vor allen Operationen
    print("\n" + "=" * 60)
    print("  CACHE-BEREINIGUNG")
    print("=" * 60 + "\n")
    
    # Pfade für die Bereinigung definieren
    installer_dir = os.path.dirname(os.path.abspath(__file__))
    install_dir = os.path.dirname(installer_dir)
    pi_dir = os.path.dirname(install_dir)
    e3dc_control_dir = os.path.join(pi_dir, "E3DC-Control")

    cleanup_pycache(install_dir)
    # E3DC-Control (C++) Ordner existiert in V4 nicht mehr -> kein pycache dort
    
    print("\n" + "!" * 60)
    print("!!! NOTFALL-MODUS / SYSTEM-REPARATUR !!!")
    print("!" * 60)
    print("Dieser Modus führt nacheinander folgende Schritte aus:")
    print("1. Systempakete ueberpruefen & nachinstallieren")
    print("2. Dateirechte pruefen & korrigieren (Permissions)")
    print("3. RSCP Live-Dienst (e3dc-live) neuerstarten")
    print("4. Watchdog-Konfiguration ueberpruefen (Piguard)")
    print("\nDies kann helfen, wenn Live-Daten fehlen oder Dienste abgestuerzt sind.")
    
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
        import subprocess
        # V4: RSCP Live-Dienst (e3dc-live) neu starten
        # Der alte C++ e3dc.service wird in V4 nicht mehr benoetigt
        for svc in ["e3dc-live", "e3dc-storage-simulator", "e3dc-epex-manager"]:
            res = subprocess.run(["sudo", "systemctl", "is-enabled", svc],
                                  capture_output=True, text=True)
            if res.returncode == 0:  # Nur wenn erinstalliert
                subprocess.run(["sudo", "systemctl", "restart", svc], check=False)
                print(f"  [OK] {svc} neugestartet.")
        import os as _os
        if _os.path.exists("/etc/systemd/system/e3dc.service"):
            print("  [i] Legacy e3dc.service gefunden (wird in V4 ignoriert).")
    except Exception as e:
        print(f"[!] Fehler in Schritt 3: {e}")
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
