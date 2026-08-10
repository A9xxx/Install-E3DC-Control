#!/usr/bin/env python3
# E3DC-Control Installer – modular & dynamisch

import os
import sys
import logging
import pwd
import shlex
import argparse  # NEU: Für Argumenten-Parsing (Headless/PWA Support)


def _reject_privileged_web_invocation():
    """Sperrt alte, zu breite sudoers-Einstiege aus dem Webserverkontext."""
    sudo_user = str(os.environ.get("SUDO_USER") or "").strip()
    if os.geteuid() == 0 and sudo_user == "www-data":
        print(
            "✗ Sicherheitssperre: installer_main.py darf nicht privilegiert "
            "aus dem Webserverkontext gestartet werden.",
            file=sys.stderr,
        )
        raise SystemExit(126)


# Dieses Gate muss vor allen produktiven Installer-Importen laufen.
_reject_privileged_web_invocation()

# Basis-Pfade
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
INSTALLER_DIR = os.path.join(SCRIPT_DIR, "Installer")

# Sicherstellen, dass Installer-Paket importierbar ist
if INSTALLER_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# Produktimporte werden erst nach dem frühen Docker-CLI-Router gebunden. So
# bleibt der zwingende Host-/Compose-Vertrag auch bei einem unvollständigen
# Container-Pythonbestand erreichbar.
CONFIG_FILE = None
get_install_user = None
load_config = None
ensure_web_config = None
get_home_dir = None
require_bound_venv_runtime = None
resolve_venv_target = None
setup_logging = None

# Globale Variable für den Headless-Modus
# V4: Web-UI Zero Touch übernimmt die Konfiguration, Console ist immer Unattended
UNATTENDED_MODE = True


def _is_docker_environment():
    if os.path.isfile('/.dockerenv'):
        return True
    marker = str(os.environ.get('E3DC_CONTAINER_MODE') or '').strip().lower()
    return marker in {'1', 'true', 'yes', 'docker'}


def _print_docker_host_update_contract(*, image_tag=""):
    """Zeigt ausschließlich den kanonischen, hostseitigen Compose-Updater."""
    print("    EXTERNAL_ACTION_REQUIRED: Auf dem Docker-Host in das vorhandene")
    print("    e3dc-docker-/Compose-Verzeichnis wechseln.")
    print("    Fehlt dort docker_compose_update.py, muss der freigegebene Helper")
    print("    zuerst über den dokumentierten Host-Installationsweg projiziert werden.")
    command = "python3 ./docker_compose_update.py --compose-dir . --sudo"
    if image_tag:
        command += f" --image-tag {shlex.quote(str(image_tag))}"
    print(f"    Danach ausführen: {command}")

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

def check_python_version():
    """Prüft den seit V5 dokumentierten Python-3.10-Mindestvertrag."""
    if sys.version_info < (3, 10):
        print("✗ Fehler: Python 3.10+ erforderlich!")
        print(f" Deine Version: {sys.version}")
        sys.exit(1)

def check_root_privileges():
    """Prüft ob Skript mit root-Rechten läuft."""
    if os.geteuid() != 0:
        print("✗ Fehler: Dieses Skript muss mit sudo ausgeführt werden!")
        print("Beispiel: sudo python3 installer_main.py")
        sys.exit(1)

def ensure_install_user():
    """Bindet den Benutzer nur prozesslokal; persistiert wird erst nach Bestätigung."""
    logger = logging.getLogger("install")
    bootstrap_user = str(
        os.environ.get("E3DC_BOOTSTRAP_USER")
        or os.environ.get("SUDO_USER")
        or ""
    ).strip()
    if bootstrap_user in {"", "root", "www-data"}:
        print("✗ Ein normaler, durch den Aufruf gebundener Installationsbenutzer fehlt.")
        logger.error("Kein zulässiger Installationsbenutzer gebunden")
        return False
    os.environ["E3DC_BOOTSTRAP_USER"] = bootstrap_user
    try:
        install_user = get_install_user()
        user_info = pwd.getpwnam(install_user)
    except (KeyError, RuntimeError) as exc:
        print(f"✗ Installationsbenutzer konnte nicht sicher gebunden werden: {exc}")
        logger.error("Installationsbenutzer konnte nicht sicher gebunden werden: %s", exc)
        return False
    try:
        home_dir = get_home_dir(install_user)
    except RuntimeError as exc:
        print(f"✗ Home-Verzeichnis von '{install_user}' ist nicht eindeutig: {exc}")
        logger.error("Home-Verzeichnis ist nicht vertrauenswürdig: %s", exc)
        return False
    if home_dir != str(user_info.pw_dir or "").strip():
        print(f"✗ Home-Verzeichnis von '{install_user}' ist nicht eindeutig.")
        logger.error("Home-Verzeichnis ist nicht vertrauenswürdig: %s", home_dir)
        return False
    print(f"→ Installationsbenutzer vorgeprüft: {install_user} ({home_dir})")
    logger.info(
        "Installationsbenutzer prozesslokal gebunden: %s (%s)",
        install_user,
        home_dir,
    )
    return True

def ensure_web_config_safe(user, logger):
    """Hilfsfunktion zum sicheren Setzen der web config."""
    print("→ Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    logger.info("Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    try:
        venv_name, venv_path = resolve_venv_target(user)
        require_bound_venv_runtime(
            install_user=user,
            venv_path=venv_path,
        )
    except Exception as exc:
        print(f"⚠ Gebundenes Benutzer-venv fehlt: {exc}")
        logger.warning("Gebundenes Benutzer-venv fehlt (user=%s): %s", user, exc)
        return False
    if not ensure_web_config(
        user,
        explicit_venv_name=venv_name,
        explicit_venv_path=venv_path,
        require_bound_venv=True,
    ):
        print("⚠ Konnte e3dc_paths.json nicht prüfen/aktualisieren.")
        logger.warning("Konnte e3dc_paths.json nicht prüfen/aktualisieren (user=%s)", user)
        return False
    return True

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
    global CONFIG_FILE, get_install_user, load_config, ensure_web_config, get_home_dir
    global require_bound_venv_runtime, resolve_venv_target, setup_logging
    
    # NEU: Argumenten-Parser für Headless / Web-Trigger
    parser = argparse.ArgumentParser(description="E3DC-Control Installer")
    parser.add_argument("--unattended", action="store_true", help="Ohne Benutzereingaben ausführen (für PHP/Cron)")
    parser.add_argument("--update-e3dc", action="store_true", help="E3DC-Control aktualisieren (headless)")
    parser.add_argument(
        "--reinstall-current",
        action="store_true",
        help="Die aktuell veröffentlichte Version ausdrücklich neu installieren",
    )
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
    selected_release_actions = sum(
        bool(value)
        for value in (
            args.update_e3dc,
            args.reinstall_current,
            args.install_release_tag,
        )
    )
    if selected_release_actions > 1:
        parser.error(
            "--update-e3dc, --reinstall-current und --install-release-tag "
            "dürfen nicht kombiniert werden"
        )

    docker_environment = _is_docker_environment()
    docker_release_action = bool(
        args.update_e3dc
        or args.reinstall_current
        or args.install_release_tag
    )
    if docker_environment and docker_release_action:
        print("✗ Docker-Installation erkannt: Im Container ist kein Release-Wechsel zulässig.")
        _print_docker_host_update_contract(image_tag=args.install_release_tag)
        sys.exit(2)

    docker_native_mutations = tuple(
        option
        for option, selected in (
            ("--install-all", args.install_all),
            ("--fix-permissions", args.fix_permissions),
            ("--prepare-system-packages", args.prepare_system_packages),
        )
        if selected
    )
    if docker_environment and docker_native_mutations:
        print(
            "✗ Docker-Installation erkannt: Native Bare-Metal-Aktionen sind "
            "im Container hart gesperrt."
        )
        print("    Abgelehnte Aktion(en): " + ", ".join(docker_native_mutations))
        print("    EXTERNAL_ACTION_REQUIRED: Installation und Hostpakete werden nur")
        print("    auf dem Docker-Host über den dokumentierten Compose-Weg verwaltet.")
        sys.exit(2)

    check_python_version()
    check_root_privileges()

    try:
        from Installer.installer_config import (
            CONFIG_FILE,
            ensure_web_config,
            get_home_dir,
            get_install_user,
            load_config,
        )
        from Installer.utils import (
            require_bound_venv_runtime,
            resolve_venv_target,
            setup_logging,
        )
    except ImportError as exc:
        print(f"CRITICAL ERROR: Import fehlgeschlagen: {exc}")
        sys.exit(1)

    try:
        if args.check:
            print("OK: installer_main.py ist per sudo ausführbar.")
            sys.exit(0)

        # Produktdateien werden beim Start niemals mehr als root durch einen
        # pfadbasierten BOM-Scanner umgeschrieben. Ein seltener manueller
        # Encoding-Reparaturfall bleibt gemäß README eine separate Aktion des
        # Installationsbenutzers.
        setup_logging()
        print(f"→ Installer-Pfad: {SCRIPT_DIR}")
        print(f"→ Konfiguration:  {CONFIG_FILE}")
        sys.stdout.flush()

        # Direktes Update wenn angefordert
        if args.update_e3dc or args.reinstall_current:
            print(
                "→ Starte ausdrückliche Neuinstallation..."
                if args.reinstall_current
                else "→ Starte Update-Modul..."
            )
            sys.stdout.flush()
            from Installer.update import (
                UPDATE_ALREADY_CURRENT,
                start_installation_or_update,
            )
            update_ok = start_installation_or_update(
                allow_first_install=False,
                headless=True,
                reinstall_current=args.reinstall_current,
            )
            
            # Pfadmetadaten nur nach einem tatsächlichen Update synchronisieren.
            # Ein blockierter Direktaufruf darf kein halbes Erstsystem erzeugen.
            if update_ok not in (False, UPDATE_ALREADY_CURRENT):
                try:
                    user = get_install_user()
                    logger = logging.getLogger("install")
                    if ensure_web_config_safe(user, logger) is not True:
                        update_ok = False
                except Exception as metadata_exc:
                    print(f"✗ Lokale Rollenbindung nach Update fehlgeschlagen: {metadata_exc}")
                    update_ok = False
                
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
            if update_ok is not False and not args.bootstrap_install_path:
                try:
                    user = get_install_user()
                    logger = logging.getLogger("install")
                    if ensure_web_config_safe(user, logger) is not True:
                        update_ok = False
                except Exception as metadata_exc:
                    print(f"✗ Lokale Rollenbindung nach Releasewechsel fehlgeschlagen: {metadata_exc}")
                    update_ok = False

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

        # VENV-Status: Pfadmetadaten werden ausschließlich nach einem
        # vollständigen Laufzeitvertrag projiziert. System-Python ist kein
        # zulässiger Produktfallback.
        install_user = get_install_user()
        GREEN = '\033[92m'
        YELLOW = '\033[93m'
        RESET = '\033[0m'
        try:
            venv_name, venv_path = resolve_venv_target(install_user)
            require_bound_venv_runtime(
                install_user=install_user,
                venv_path=venv_path,
            )
            print(f"{GREEN}✓ Python venv aktiv: {venv_path}{RESET}")
        except Exception as exc:
            print(
                f"{YELLOW}ℹ️  Noch kein vertrauensgebundenes Python-venv: "
                f"{exc}. Die Komplettinstallation muss es zuerst einrichten.{RESET}"
            )

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

        menu_ok = run_main_menu()
        sys.exit(0 if menu_ok is not False else 1)

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
