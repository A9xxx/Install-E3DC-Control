#!/usr/bin/env python3
# E3DC-Control Installer – modular & dynamisch

import os
import sys
import subprocess
import logging
import pwd

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


def ensure_install_user():
    """Ask for install user only on first start and persist selection."""
    logger = logging.getLogger("install")
    config = load_config()
    saved_user = config.get("install_user")
    saved_home = config.get("home_dir")
    user_confirmed = bool(config.get("install_user_confirmed", False))

    default_user = saved_user or get_default_install_user()

    if user_confirmed and saved_user and saved_home and os.path.isdir(saved_home):
        print(f"→ Aktueller Installationsbenutzer: {saved_user} ({saved_home})")
        logger.info(f"Aktueller Installationsbenutzer: {saved_user} ({saved_home})")
        verify_config_file_access(saved_user)
        print("→ Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
        logger.info("Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
        if not ensure_web_config(saved_user):
            print("⚠ Konnte e3dc_paths.json nicht prüfen/aktualisieren.")
            logger.warning("Konnte e3dc_paths.json nicht prüfen/aktualisieren (user=%s)", saved_user)
        return True

    print("\n=== Installer Benutzer ===\n")
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
    print("→ Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    logger.info("Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    if not ensure_web_config(install_user):
        print("⚠ Konnte e3dc_paths.json nicht prüfen/aktualisieren.")
        logger.warning("Konnte e3dc_paths.json nicht prüfen/aktualisieren (user=%s)", install_user)
    return True


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
        writable_by_owner = bool(file_stat.st_mode & 0o200)
        readable_by_owner = bool(file_stat.st_mode & 0o400)

        if owner_ok and group_ok and readable_by_owner and writable_by_owner:
            logger.info(
                "Config-Zugriff OK: %s gehört %s:www-data und ist les-/schreibbar.",
                CONFIG_FILE,
                install_user
            )
            return

        logger.warning(
            "Config-Zugriff nicht ideal (owner_ok=%s, group_ok=%s, r=%s, w=%s). Korrigiere Rechte…",
            owner_ok,
            group_ok,
            readable_by_owner,
            writable_by_owner
        )
        fixed = set_config_file_permissions(install_user)
        if fixed:
            logger.info("Config-Rechte erfolgreich korrigiert für Benutzer '%s'.", install_user)
        else:
            logger.warning("Config-Rechte konnten nicht korrigiert werden für Benutzer '%s'.", install_user)

    except Exception as e:
        logger.warning("Config-Zugriffsprüfung fehlgeschlagen: %s", e)


def change_install_user_from_menu():
    """Menüaktion: Installationsbenutzer nachträglich ändern."""
    logger = logging.getLogger("install")
    config = load_config()
    current_user = config.get("install_user") or get_default_install_user()

    print("\n=== Installationsbenutzer ändern ===\n")
    user_input = input(f"Neuer Installationsbenutzer [{current_user}]: ").strip()
    install_user = user_input or current_user

    try:
        user_info = pwd.getpwnam(install_user)
        home_dir = user_info.pw_dir
    except KeyError:
        print(f"✗ Benutzer '{install_user}' existiert nicht.")
        logger.error(f"Installationsbenutzer existiert nicht: {install_user}")
        return

    config["install_user"] = install_user
    config["home_dir"] = home_dir
    config["install_user_confirmed"] = True
    save_config(config)
    verify_config_file_access(install_user)

    print(f"✓ Installationsbenutzer gesetzt auf '{install_user}' ({home_dir})")
    logger.info(f"Installationsbenutzer per Menü geändert auf '{install_user}' ({home_dir})")

    print("→ Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    logger.info("Prüfe e3dc_paths.json (Aktualisierung nur bei Bedarf)")
    if not ensure_web_config(install_user):
        print("⚠ Konnte e3dc_paths.json nicht prüfen/aktualisieren.")
        logger.warning("Konnte e3dc_paths.json nicht prüfen/aktualisieren (user=%s)", install_user)


def restart_installer():
    """Startet den Installer neu."""
    print("\n→ Starte Installer neu…\n")
    os.execv(sys.executable, [sys.executable] + sys.argv)


def check_for_updates():
    """Prüft auf verfügbare Updates und fragt nur bei Verfügbarkeit nach."""
    try:
        import inspect
        from Installer.self_update import check_and_update

        signature = inspect.signature(check_and_update)
        params = signature.parameters

        if "check_only" in params:
            # Prüfe zuerst stillschweigend auf Updates (keine Frage vorher)
            check_kwargs = {"check_only": True}
            if "silent" in params:
                check_kwargs["silent"] = True  # Still = keine Meldungen

            print("\n→ Prüfe auf Updates...")
            update_available = bool(check_and_update(**check_kwargs))
            
            if not update_available:
                print("✓ Du bist auf dem neuesten Stand.\n")
                return

            # Nur wenn Update verfügbar ist, fragen
            install_choice = input("✓ Update verfügbar. Jetzt installieren? (j/n): ").strip().lower()
            if install_choice != "j":
                print("→ Update übersprungen.\n")
                return

            install_kwargs = {"check_only": False}
            if "silent" in params:
                install_kwargs["silent"] = False
            if "auto_update" in params:
                install_kwargs["auto_update"] = True
            if "auto_install" in params:
                install_kwargs["auto_install"] = True
            if "install" in params:
                install_kwargs["install"] = True

            if check_and_update(**install_kwargs):
                restart_installer()
            return

        # Fallback: Versuche direkt mit Installation zu prüfen
        update_kwargs = {}
        if "silent" in params:
            update_kwargs["silent"] = True

        print("\n→ Prüfe auf Updates...")
        if check_and_update(**update_kwargs):
            restart_installer()
        else:
            print("✓ Du bist auf dem neuesten Stand.\n")
    
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
        # Logging initialisieren
        setup_logging()
        
        # Prüfungen
        check_python_version()
        check_root_privileges()

        # Prüfe auf Updates (vor User-Abfrage/Hauptmenü)
        check_for_updates()

        if not ensure_install_user():
            sys.exit(1)

        # Importiere Core-Modul
        try:
            from Installer.core import run_main_menu
        except ImportError as e:
            print(f"✗ Fehler beim Laden des Installer-Moduls: {e}")
            print(f"   Prüfe ob das Verzeichnis '{INSTALLER_DIR}' existiert.")
            sys.exit(1)

        # Registriere zusätzliche Menüaktionen (falls API vorhanden)
        try:
            from Installer.core import register_command
            register_command("98", "Installationsbenutzer ändern", change_install_user_from_menu, sort_order=98)
        except Exception:
            pass

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
