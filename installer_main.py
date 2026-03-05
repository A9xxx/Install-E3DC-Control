#!/usr/bin/env python3
# E3DC-Control Installer – modular & dynamisch

import os
import sys
import subprocess
import logging
import pwd
import argparse  # NEU: Für Argumenten-Parsing (Headless/PWA Support)

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

# Pufferung deaktivieren (wichtig für Web-Interface Ausgabe)
# Muss so früh wie möglich geschehen
if not sys.stdout.isatty():
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except AttributeError:
        sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)

# Debug-Ausgabe ganz am Anfang (für Web-Update Diagnose)
print(f"→ Installer-Skript gestartet (PID: {os.getpid()})")
print(f"→ Arbeitsverzeichnis: {os.getcwd()}")
sys.stdout.flush()

# ZUSATZ-DIAGNOSE: Schreibe in eine separate Datei, um Redirect-Probleme auszuschließen
try:
    with open("/tmp/e3dc_installer_debug.txt", "w") as f:
        f.write(f"Gestartet um {os.popen('date').read().strip()}\nPID: {os.getpid()}\nUser: {os.geteuid()}\n")
except:
    pass

# Importe mit Fehlerbehandlung, damit Abstürze im Log landen
try:
    from Installer.installer_config import (
        CONFIG_FILE,
        get_default_install_user,
        load_config,
        save_config,
        ensure_web_config,
        get_home_dir,
        get_user_ids,
        get_www_data_gid,
        set_config_file_permissions,
        get_install_path
    )
    from Installer.utils import setup_logging
except ImportError as e:
    print(f"CRITICAL ERROR: Import fehlgeschlagen: {e}")
    sys.exit(1)

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

def check_duplicate_installations():
    """Prüft auf konkurrierende Installationen in ~/Install."""
    try:
        config = load_config()
        user = config.get("install_user")
        if not user: return

        home = get_home_dir(user)
        standard_path = os.path.join(home, "Install")
        
        current_path = os.path.abspath(SCRIPT_DIR)
        standard_path = os.path.abspath(standard_path)

        # Nur warnen, wenn wir NICHT im Standardpfad sind, aber einer existiert
        if os.path.exists(standard_path) and current_path != standard_path:
            print("\n" + "!" * 60)
            print("⚠ HINWEIS: Parallele Installation gefunden!")
            print(f"  Laufend:   {current_path}")
            print(f"  Gefunden:  {standard_path}")
            print("!" * 60)
            print("Dies kann zu Verwirrung führen (unterschiedliche Configs/Versionen).")
            print(f"Empfehlung: Lösche die alte Version, wenn sie nicht mehr benötigt wird.")
            print(f"Befehl: sudo rm -rf {standard_path}")
            print("-" * 60 + "\n")
            
    except Exception:
        pass

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
            sys.stdout.flush()
            fix_bom_main()
            print("✓ BOM-Prüfung abgeschlossen.")
            sys.stdout.flush()
        else:
            print("⚠ Warnung: BOM-Fixer-Skript (fix_bom.py) nicht gefunden.")
            sys.stdout.flush()

        setup_logging()
        check_python_version()
        check_root_privileges()
        
        print(f"→ Installer-Pfad: {SCRIPT_DIR}")
        print(f"→ Konfiguration:  {CONFIG_FILE}")
        sys.stdout.flush()

        # Direktes Update wenn angefordert
        if args.update_e3dc:
            print("→ Starte Update-Modul...")
            sys.stdout.flush()
            from Installer.update import update_e3dc
            update_e3dc(headless=True)
            sys.exit(0)

        check_for_updates()

        if not ensure_install_user():
            sys.exit(1)

        check_duplicate_installations()

        # VENV Status Check
        install_path = get_install_path()
        config = load_config()
        home_dir = get_home_dir(config.get("install_user"))
        venv_name = config.get("venv_name", ".venv_e3dc")
        
        venv_path = ""
        if venv_name:
            # Prüfe Home-Verzeichnis (Standard) und Install-Verzeichnis (Legacy)
            if os.path.exists(os.path.join(home_dir, venv_name)):
                venv_path = os.path.join(home_dir, venv_name)
            elif os.path.exists(os.path.join(install_path, venv_name)):
                venv_path = os.path.join(install_path, venv_name)
        
        GREEN = '\033[92m'
        YELLOW = '\033[93m'
        RESET = '\033[0m'
        if venv_name and venv_path:
            print(f"{GREEN}✓ Python venv aktiv: {venv_path}{RESET}")
            # Update e3dc_paths.json für PHP
            try:
                paths_file = "/var/www/html/e3dc_paths.json"
                if not os.path.exists(paths_file):
                    ensure_web_config(config.get("install_user"))

                if os.path.exists(paths_file):
                    import json
                    with open(paths_file, 'r') as f: d = json.load(f)
                    d['venv_name'] = venv_name
                    d['venv_path'] = venv_path
                    with open(paths_file, 'w') as f: json.dump(d, f, indent=2)
                    
                    # Rechte korrigieren
                    try:
                        uid, _ = get_user_ids(config.get("install_user"))
                        gid = get_www_data_gid()
                        os.chown(paths_file, uid, gid)
                        os.chmod(paths_file, 0o664)
                    except: pass
            except Exception as e:
                print(f"⚠ Fehler beim Aktualisieren von e3dc_paths.json: {e}")
        else:
            print(f"{YELLOW}ℹ️  Kein Python venv gefunden (System-Python wird genutzt){RESET}")

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