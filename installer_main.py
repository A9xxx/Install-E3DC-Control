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
# V4: Web-UI Zero Touch übernimmt die Konfiguration, Console ist immer Unattended
UNATTENDED_MODE = True


def _is_docker_environment():
    if os.path.isfile('/.dockerenv'):
        return True
    marker = str(os.environ.get('E3DC_CONTAINER_MODE') or '').strip().lower()
    return marker in {'1', 'true', 'yes', 'docker'}

# Standard-Ausgabe auf UTF-8 erzwingen (verhindert UnicodeEncodeError z.B. bei sudo ohne Locale)
# und Pufferung für Non-TTY Umgebungen (Web-Interface) anpassen
try:
    if not sys.stdout.isatty():
        sys.stdout.reconfigure(encoding='utf-8', line_buffering=True)
    else:
        sys.stdout.reconfigure(encoding='utf-8')
except AttributeError:
    if not sys.stdout.isatty():
        sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1, encoding='utf-8')
except Exception:
    pass

# Debug-Ausgabe ganz am Anfang (für Web-Update Diagnose)
print(f"→ Installer-Skript gestartet (PID: {os.getpid()})")
print(f"→ Arbeitsverzeichnis: {os.getcwd()}")
sys.stdout.flush()

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

def check_for_installer_updates():
    """(Obsolet: Update-Check passiert nun via Web-UI oder Menüpunkt 11)"""
    pass

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
    parser.add_argument("--install-release-tag", default="", help="Gezielt einen validierten Release-Tag installieren")
    parser.add_argument(
        "--bootstrap-install-path",
        default="",
        help="Absoluter Zielpfad für eine geprüfte V3/ZIP- oder V4-Altinstallation",
    )
    parser.add_argument(
        "--expected-release-sha",
        default="",
        help="Explizit freigegebene volle 40-stellige Ziel-SHA (für Bootstrap verpflichtend)",
    )
    parser.add_argument(
        "--expected-ha-role",
        choices=("off", "master", "slave", "shadow"),
        default="",
        help="Vor dem Bootstrap erwartete und danach unveränderte HA-/Shadow-Rolle",
    )
    parser.add_argument("--fix-permissions", action="store_true", help="Dateirechte und Dienste headless pruefen/reparieren")
    parser.add_argument("--prepare-system-packages", action="store_true", help="Nur Systempakete/Python-Abhängigkeiten installieren und danach beenden")
    parser.add_argument("--install-all", action="store_true", help="Vollständige Installation durchführen (headless)")
    parser.add_argument("--check", action="store_true", help="Non-interaktiver sudo/WebUI-Preflight")
    args = parser.parse_args()
    UNATTENDED_MODE = args.unattended

    bootstrap_values = (
        args.bootstrap_install_path,
        args.expected_release_sha,
        args.expected_ha_role,
    )
    if any(bootstrap_values) and not args.install_release_tag:
        parser.error("Bootstrap-Optionen sind nur zusammen mit --install-release-tag zulässig")

    if (args.update_e3dc or args.install_release_tag) and _is_docker_environment():
        print("[i] Docker-Installation erkannt: Im Container wird kein Release-Wechsel ausgeführt.")
        print("    Bitte auf dem Docker-Host im Verzeichnis Deiner vorhandenen Compose-Konfiguration ausführen:")
        print("    docker compose config --images")
        print("    docker compose pull e3dc-control")
        print("    docker compose up -d --force-recreate e3dc-control")
        print("    Hinweis: Ein fest eingetragener Versions-Tag bleibt fest. Für automatische Stable-Updates")
        print("    muss die Compose-Datei latest bzw. ${E3DC_IMAGE_TAG:-latest} verwenden.")
        sys.exit(0)

    try:
        # Führe den BOM-Fixer aus, um Dateikodierungsprobleme zu beheben
        if fix_bom_main and not args.bootstrap_install_path:
            print("→ Prüfe Dateikodierungen (BOM)...")
            sys.stdout.flush()
            fix_bom_main()
            print("✓ BOM-Prüfung abgeschlossen.")
            sys.stdout.flush()
        elif not fix_bom_main:
            print("⚠ Warnung: BOM-Fixer-Skript (fix_bom.py) nicht gefunden.")
            sys.stdout.flush()
        else:
            print("→ Release-Bootstrap: BOM-Prüfung bleibt bis nach dem verifizierten Backup ausgesetzt.")
            sys.stdout.flush()

        setup_logging()
        check_python_version()
        check_root_privileges()
        
        print(f"→ Installer-Pfad: {SCRIPT_DIR}")
        print(f"→ Konfiguration:  {CONFIG_FILE}")
        sys.stdout.flush()

        if args.check:
            print("OK: installer_main.py ist per sudo ausfuehrbar.")
            sys.exit(0)

        # Direktes Update wenn angefordert
        if args.update_e3dc:
            print("→ Starte Update-Modul...")
            sys.stdout.flush()
            from Installer.update import start_installation_or_update
            update_ok = start_installation_or_update(
                allow_first_install=False,
                headless=True,
            )
            
            # Pfadmetadaten nur nach einem tatsächlichen Update synchronisieren.
            # Ein blockierter Direktaufruf darf kein halbes Erstsystem erzeugen.
            if update_ok is not False:
                config = load_config()
                user = config.get("install_user")
                if user:
                    logger = logging.getLogger("install")
                    ensure_web_config_safe(user, logger)
                
            sys.exit(0 if update_ok is not False else 1)

        if args.install_release_tag:
            action_label = "Release-Bootstrap" if args.bootstrap_install_path else "Release-Rückfall"
            print(f"-> Starte {action_label} auf {args.install_release_tag}...")
            sys.stdout.flush()
            from Installer.update import update_e3dc
            update_ok = update_e3dc(
                headless=True,
                target_ref=args.install_release_tag,
                target_install_path=args.bootstrap_install_path or None,
                expected_release_sha=args.expected_release_sha or None,
                expected_ha_role=args.expected_ha_role or None,
            )

            # Im Archiv-Bootstrap darf nach dem Reset ausschließlich der
            # SHA-gebundene Target-Finalizer Zielcode ausführen.
            if not args.bootstrap_install_path:
                config = load_config()
                user = config.get("install_user")
                if user:
                    logger = logging.getLogger("install")
                    ensure_web_config_safe(user, logger)

            sys.exit(0 if update_ok is not False else 1)

        if args.fix_permissions:
            print("-> Starte Rechte-Reparatur...")
            sys.stdout.flush()
            if not ensure_install_user():
                sys.exit(1)
            from Installer.permissions import run_permissions_wizard
            ok = run_permissions_wizard(headless=True)
            try:
                from Installer.boot_sanity import check_boot_sanity
                if not check_boot_sanity(verbose=True):
                    ok = False
            except Exception as boot_exc:
                print(f"[WARNUNG] Boot-Sanitycheck konnte nicht ausgefuehrt werden: {boot_exc}")
                ok = False
            sys.exit(0 if ok is not False else 1)

        if args.prepare_system_packages:
            print("-> Starte Systempaket-Vorbereitung...")
            sys.stdout.flush()
            if not ensure_install_user():
                sys.exit(1)
            from Installer.utils import prepare_system_packages_for_snapshot
            ok = prepare_system_packages_for_snapshot(use_venv=True)
            sys.exit(0 if ok is not False else 1)

        # Direktes Install-All wenn angefordert (Zero-Touch)
        if args.install_all:
            print("→ Starte vollautomatische Installation (Zero-Touch)...")
            UNATTENDED_MODE = True
            if not ensure_install_user():
                sys.exit(1)
            from Installer.update import start_installation_or_update
            install_ok = start_installation_or_update(
                allow_first_install=True,
                headless=True,
            )
            sys.exit(0 if install_ok is not False else 1)

        check_for_installer_updates()

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
        try:
            from Installer.logging_manager import log_error, print_installation_summary
            log_error("installer_main", f"Unerwarteter Fehler: {e}", e)
            print_installation_summary()
        except: pass
        print(f"\n✗ KRITISCHER FEHLER: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main()
