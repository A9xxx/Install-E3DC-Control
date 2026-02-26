#!/usr/bin/env python3
# E3DC-Control Installer – modular & dynamisch

import os
import sys
import subprocess
import logging
import pwd
import argparse  # NEU: Für Argumenten-Parsing (Headless/PWA Support)

from Installer.installer_config import (
    CONFIG_FILE,
    get_default_install_user,
    load_config,
    save_config,
    ensure_web_config,
    get_home_dir,
    get_user_ids,
    get_www_data_gid,
    set_config_file_permissions
)
from Installer.utils import setup_logging

# Basis-Pfade
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALLER_DIR = os.path.join(SCRIPT_DIR, "Installer")

# Sicherstellen, dass Installer-Paket importierbar ist
if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# Importiere das BOM-Fix-Skript
try:
    from fix_bom import main as fix_bom_main
except ImportError:
    fix_bom_main = None

# Globale Variable für den Headless-Modus
UNATTENDED_MODE = False

def check_python_version():
    """Prüft ob Python 3.7+ vorhanden ist."""
    if sys.version_info < (3, 7):
        print("✗ Fehler: Python 3.7+ erforderlich!")
        print(f" Deine Version: {sys.version}")
        sys.exit(1)

def check_root_privileges():
    """Prüft ob Skript mit root-Rechten läuft."""
    if os.geteuid() != 0:
        print("✗ Fehler: Dieses Skript muss mit sudo ausgeführt werden!")
        print("Beispiel: sudo python3 installer_main.py")
        sys.exit(1)

def ensure_install_user():
    """Stellt sicher, dass ein valider Installationsbenutzer konfiguriert ist, ohne unnötig nachzufragen."""
    logger = logging.getLogger("install")
    config = load_config()
    saved_user = config.get("install_user")

    # 1. Prüfen, ob ein valider Benutzer bereits gespeichert ist
    if saved_user:
        try:
            user_info = pwd.getpwnam(saved_user)
            home_dir = user_info.pw_dir
            
            # Benutzer ist valide, also verwenden
            print(f"→ Aktueller Installationsbenutzer: {saved_user} ({home_dir})")
            logger.info(f"Aktueller Installationsbenutzer: {saved_user} ({home_dir})")
            
            # Sicherstellen, dass die Konfiguration vollständig ist und speichern
            if not config.get("install_user_confirmed") or config.get("home_dir") != home_dir:
                config["install_user"] = saved_user
                config["home_dir"] = home_dir
                config["install_user_confirmed"] = True
                save_config(config)
                logger.info("Benutzerkonfiguration verifiziert und gespeichert.")

            verify_config_file_access(saved_user)
            ensure_web_config_safe(saved_user, logger)
            return True
        except KeyError:
            # Gespeicherter Benutzer ist ungültig, also neu fragen
            print(f"⚠ Gespeicherter Benutzer '{saved_user}' ist ungültig. Bitte neu konfigurieren.")
            logger.warning(f"Gespeicherter Benutzer '{saved_user}' ist ungültig.")
            # Fall through to ask for a new user

    # 2. Wenn kein valider Benutzer gespeichert ist, neu fragen
    print("\n=== Installer Benutzer festlegen ===")
    
    default_user = get_default_install_user()

    if UNATTENDED_MODE:
        install_user = default_user
        print(f"Automatischer Modus aktiv. Setze Benutzer auf: {install_user}")
    else:
        user_input = input(f"Installationsbenutzer [{default_user}]: ").strip()
        install_user = user_input or default_user

    try:
        user_info = pwd.getpwnam(install_user)
        home_dir = user_info.pw_dir
    except KeyError:
        print(f"✗ Benutzer '{install_user}' existiert nicht.")
        logger.error(f"Installationsbenutzer existiert nicht: {install_user}")
        return False

    config["install_user"] = install_user
    config["home_dir"] = home_dir
    config["install_user_confirmed"] = True
    save_config(config)
    verify_config_file_access(install_user)
    ensure_web_config_safe(install_user, logger)
    return True

def ensure_web_config_safe(user, logger):
    """Hilfsfunktion zum sicheren Setzen der web config."""
    print("→ Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    logger.info("Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    if not ensure_web_config(user):
        print("⚠ Konnte e3dc_paths.json nicht prüfen/aktualisieren.")
        logger.warning("Konnte e3dc_paths.json nicht prüfen/aktualisieren (user=%s)", user)

def verify_config_file_access(install_user):
    """Prüft Besitzrechte der installer_config.json und korrigiert bei Bedarf."""
    logger = logging.getLogger("install")
    try:
        expected_uid, _ = get_user_ids(install_user)
        expected_gid = get_www_data_gid()
        if not os.path.exists(CONFIG_FILE):
            return

        file_stat = os.stat(CONFIG_FILE)
        owner_ok = file_stat.st_uid == expected_uid
        group_ok = file_stat.st_gid == expected_gid
        
        # NEU: Prüft zusätzlich, ob Gruppe (www-data) Lese/Schreibrechte hat
        readable_by_owner = bool(file_stat.st_mode & 0o400)
        readable_by_group = bool(file_stat.st_mode & 0o040)

        if owner_ok and group_ok and readable_by_owner and readable_by_group:
            logger.info("Config-Zugriff OK: %s gehört %s:www-data und ist lesbar.", CONFIG_FILE, install_user)
            return

        logger.warning("Config-Zugriff nicht ideal. Korrigiere Rechte…")
        fixed = set_config_file_permissions(install_user)
        if fixed:
            logger.info("Config-Rechte erfolgreich korrigiert für Benutzer '%s'.", install_user)
        else:
            logger.warning("Config-Rechte konnten nicht korrigiert werden für Benutzer '%s'.", install_user)
    except Exception as e:
        logger.warning("Config-Zugriffsprüfung fehlgeschlagen: %s", e)

def check_for_updates():
    """Prüft auf verfügbare Updates und fragt nur bei Verfügbarkeit nach."""
    try:
        import inspect
        from Installer.self_update import check_and_update
        signature = inspect.signature(check_and_update)
        params = signature.parameters

        print("\n→ Prüfe auf Updates...")
        
        # Sicherstellen, dass wir nur Parameter übergeben, die die Funktion auch kennt
        check_kwargs = {}
        if "check_only" in params:
            check_kwargs["check_only"] = True
        if "silent" in params:
            check_kwargs["silent"] = True

        # Führe die Funktion mit den sicheren Argumenten aus
        update_result = check_and_update(**check_kwargs)
        
        # Wenn deine Update-Funktion "check_only" gar nicht kennt, 
        # übernimmt sie vermutlich die ganzen (j/n) Abfragen intern.
        # In diesem Fall sind wir hier schon fertig.
        if "check_only" not in params:
            return

        update_available = bool(update_result)
        
        if not update_available:
            print("✓ Du bist auf dem neuesten Stand.\n")
            return

        # NEU: Unattended Mode installiert Updates automatisch
        if UNATTENDED_MODE:
            install_choice = "j"
            print("Automatischer Modus: Update wird installiert.")
        else:
            install_choice = input("✓ Update verfügbar. Jetzt installieren? (j/n): ").strip().lower()
            
        if install_choice != "j":
            print("→ Update übersprungen.\n")
            return

        install_kwargs = {}
        if "check_only" in params: install_kwargs["check_only"] = False
        if "silent" in params: install_kwargs["silent"] = False
        if "auto_update" in params: install_kwargs["auto_update"] = True
        if "auto_install" in params: install_kwargs["auto_install"] = True
        if "install" in params: install_kwargs["install"] = True

        if check_and_update(**install_kwargs):
            restart_installer()
            return
        else:
            print("✗ Update-Installation fehlgeschlagen. Bitte Log-Datei prüfen.\n")
            return

    except ImportError:
        pass
    except Exception as e:
        print(f"⚠ Warnung: Update-Prüfung fehlgeschlagen: {e}\n")

def restart_installer():
    """Startet den Installer neu."""
    print("\n→ Starte Installer neu…\n")
    # Argumente durchreichen, falls Unattended Mode aktiv ist
    os.execv(sys.executable, [sys.executable] + sys.argv)

def main():
    """Haupteinstiegspunkt."""
    global UNATTENDED_MODE
    
    # NEU: Argumenten-Parser für Headless / Web-Trigger
    parser = argparse.ArgumentParser(description="E3DC-Control Installer")
    parser.add_argument("--unattended", action="store_true", help="Ohne Benutzereingaben ausführen (für PHP/Cron)")
    parser.add_argument("--update-e3dc", action="store_true", help="E3DC-Control aktualisieren (headless)")
    args = parser.parse_args()
    UNATTENDED_MODE = args.unattended

    try:
        # Führe den BOM-Fixer aus, um Dateikodierungsprobleme zu beheben
        if fix_bom_main:
            print("→ Prüfe Dateikodierungen (BOM)...")
            fix_bom_main()
        else:
            print("⚠ Warnung: BOM-Fixer-Skript (fix_bom.py) nicht gefunden.")

        setup_logging()
        check_python_version()
        check_root_privileges()

        # Direktes Update wenn angefordert
        if args.update_e3dc:
            from Installer.update import update_e3dc
            update_e3dc(headless=True)
            sys.exit(0)

        check_for_updates()

        if not ensure_install_user():
            sys.exit(1)

        # Wenn im Unattended Mode, beenden wir das Script nach den Grund-Checks und Updates
        # Da das interaktive Menü in der Konsole hier keinen Sinn macht
        if UNATTENDED_MODE:
            print("✓ Automatischer Durchlauf abgeschlossen. Beende Installer.")
            sys.exit(0)

        # Importiere Core-Modul
        try:
            from Installer.core import run_main_menu
        except ImportError as e:
            print(f"✗ Fehler beim Laden des Installer-Moduls: {e}")
            print(f" Prüfe ob das Verzeichnis '{INSTALLER_DIR}' existiert.")
            sys.exit(1)

        run_main_menu()

    except KeyboardInterrupt:
        print("\n\n✗ Vorgang abgebrochen.")
        sys.exit(1)
    except Exception as e:
        print(f"\n✗ Unerwarteter Fehler: {e}")
        logging.error(f"Unerwarteter Fehler im Installer: {e}", exc_info=True)
        sys.exit(1)

if __name__ == "__main__":
    main()