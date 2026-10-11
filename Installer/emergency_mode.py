import os
import json
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
    print("\n" + "!" * 60)
    print("!!! NOTFALL-MODUS / SYSTEM-REPARATUR !!!")
    print("!" * 60)
    print("Dieser Modus führt nacheinander folgende Schritte aus:")
    print("1. Systempakete überprüfen & nachinstallieren")
    print("2. Dateirechte prüfen & korrigieren (Permissions)")
    print("3. e3dc-live, e3dc-storage-simulator und e3dc-epex-manager neu starten")
    print("4. Watchdog-Konfiguration überprüfen (Piguard)")
    print("\nDies kann helfen, wenn Live-Daten fehlen oder Dienste beendet sind.")
    
    if input("\nNotfall-Reparatur starten? (j/n): ").strip().lower() != 'j':
        print("Abbruch.")
        return

    # Cache-Bereinigung nach ausdrücklicher Bestätigung
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
        role = None
        ha_state = "unknown"
        v4_path = "/var/www/html/data/e3dc_v4.json"
        ha_status_path = "/var/www/html/ramdisk/ha_status.json"
        try:
            with open(v4_path, "r", encoding="utf-8") as handle:
                v4_data = json.load(handle)
            role = str(v4_data["ha_mode"]).strip().lower()
            if role not in {"off", "master", "slave", "shadow"}:
                raise ValueError("Rolle fehlt oder ist ungültig")
            if os.path.exists(ha_status_path):
                with open(ha_status_path, "r", encoding="utf-8") as handle:
                    ha_data = json.load(handle)
                ha_state = str(ha_data.get("state", "unknown")).strip().lower()
        except (OSError, ValueError, TypeError, KeyError, AttributeError):
            role = None
        if role is None:
            print("  ℹ Schritt 3 übersprungen: Rolle oder HA-Status nicht sicher lesbar; "
                  "kein Dienstneustart. Systemzustand über Hauptmenü 2) Systemstatus anzeigen prüfen.")
        elif role == "shadow" or (role == "slave" and ha_state != "failover"):
            print(f"  ℹ Schritt 3 übersprungen: Rolle {role} im Standby; kein Dienstneustart.")
        else:
            if role == "slave":
                print("  ℹ Slave im Failover: Dienste werden für die aktive Rolle neu gestartet.")
            # V4: RSCP Live-Dienst (e3dc-live) neu starten
            # Der alte C++ e3dc.service wird in V4 nicht mehr benötigt
            for svc in ["e3dc-live", "e3dc-storage-simulator", "e3dc-epex-manager"]:
                res = subprocess.run(["sudo", "systemctl", "is-enabled", svc],
                                      capture_output=True, text=True)
                if res.returncode == 0:  # Nur wenn installiert
                    restart = subprocess.run(["sudo", "systemctl", "restart", svc], check=False)
                    if restart.returncode == 0:
                        print(f"  [OK] {svc} neu gestartet.")
                    else:
                        print(f"  [!] {svc} konnte nicht neu gestartet werden (Exit {restart.returncode}).")
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
    print("Systemzustand über Hauptmenü 2) Systemstatus anzeigen prüfen.")
    print("=" * 60)
    log_task_completed("Notfall-Modus ausgeführt")

register_command("99", "NOTFALL-MODUS (System reparieren)", run_emergency_mode, sort_order=990)
