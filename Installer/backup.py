import os
import datetime
import shutil

from .core import register_command
from .installer_config import get_install_path, get_user_ids, get_www_data_gid
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
WEBPORTAL_EXTENSIONS = {".php", ".css", ".js", ".json"}
E3DC_CONTROL_EXTRA_EXTENSIONS = {".dat", ".json", ".py"}

backup_logger = get_or_create_logger("backup")


def _copy_matching_files(source_root, destination_root, extensions, exclude_dirs=None):
    """Kopiert rekursiv alle Dateien mit passenden Endungen und behält Pfadstruktur bei."""
    copied = 0
    excluded = {os.path.abspath(path) for path in (exclude_dirs or [])}

    for root, dirs, files in os.walk(source_root):
        root_abs = os.path.abspath(root)

        dirs[:] = [
            d for d in dirs
            if os.path.abspath(os.path.join(root, d)) not in excluded
        ]

        if root_abs in excluded:
            continue

        for filename in files:
            if os.path.splitext(filename)[1].lower() not in extensions:
                continue

            source_file = os.path.join(root, filename)
            relative_path = os.path.relpath(source_file, source_root)
            destination_file = os.path.join(destination_root, relative_path)
            os.makedirs(os.path.dirname(destination_file), exist_ok=True)
            shutil.copy2(source_file, destination_file)
            copied += 1
    return copied


def _count_files_recursive(path):
    """Zählt rekursiv alle Dateien in einem Verzeichnis."""
    count = 0
    for _, _, files in os.walk(path):
        count += len(files)
    return count


def backup_current_version():
    """Erstellt ein Backup der aktuellen Version."""
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
        backup_dir = os.path.join(INSTALL_PATH, "backups", timestamp)
        backup_logger.info(f"Starte Backup-Erstellung nach: {backup_dir}")
        os.makedirs(backup_dir, exist_ok=True)
        total_copied_files = 0

        print(f"→ Erstelle Backup unter {backup_dir}…")

        # Hauptprogramm sichern
        bin_path = os.path.join(INSTALL_PATH, "E3DC-Control")
        if os.path.exists(bin_path):
            try:
                shutil.copy2(bin_path, backup_dir)
                total_copied_files += 1
                print("  ✓ Hauptprogramm gesichert")
                backup_logger.info("Hauptprogramm 'E3DC-Control' gesichert.")
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern des Programms: {e}")
                log_error("backup", f"Fehler beim Sichern des Programms: {e}", e)

        # Konfiguration sichern
        cfg_path = os.path.join(INSTALL_PATH, "e3dc.config.txt")
        if os.path.exists(cfg_path):
            try:
                shutil.copy2(cfg_path, backup_dir)
                total_copied_files += 1
                print("  ✓ Konfiguration gesichert")
                backup_logger.info("Konfiguration 'e3dc.config.txt' gesichert.")
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern der Konfiguration: {e}")
                log_error("backup", f"Fehler beim Sichern der Konfiguration: {e}", e)

        # Webportal-Dateien sichern
        wp_backup_dir = os.path.join(backup_dir, "webportal")
        os.makedirs(wp_backup_dir, exist_ok=True)
        webroot_dir = "/var/www/html"

        if not os.path.isdir(webroot_dir):
            print(f"  ⚠ Quellordner fehlt: {webroot_dir}")
            log_warning("backup", f"Quellordner für Backup fehlt: {webroot_dir}")
        else:
            try:
                copied_webportal = _copy_matching_files(
                    webroot_dir,
                    wp_backup_dir,
                    WEBPORTAL_EXTENSIONS
                )
                total_copied_files += copied_webportal
                if copied_webportal > 0:
                    print(f"  ✓ {copied_webportal} Webportal-Dateien gesichert")
                    backup_logger.info(f"{copied_webportal} Webportal-Dateien gesichert.")
                else:
                    print("  ⚠ Keine passenden Webportal-Dateien gefunden")
                    backup_logger.warning("Keine passenden Webportal-Dateien für Backup gefunden.")
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern der Webportal-Dateien: {e}")
                log_error("backup", f"Fehler beim Sichern der Webportal-Dateien: {e}", e)
        
        # Icons sichern
        icons_src = "/var/www/html/icons"
        if os.path.exists(icons_src):
            try:
                icons_count = _count_files_recursive(icons_src)
                shutil.copytree(icons_src, os.path.join(wp_backup_dir, "icons"), dirs_exist_ok=True)
                total_copied_files += icons_count
                backup_logger.info(f"{icons_count} Icons gesichert.")
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern der Icons: {e}")
                log_error("backup", f"Fehler beim Sichern der Icons: {e}", e)

        # E3DC-Control-Zusatzdateien sichern
        e3dc_source_dir = INSTALL_PATH
        e3dc_extra_backup_dir = os.path.join(backup_dir, "e3dc-control-extra")
        backup_root_dir = os.path.join(INSTALL_PATH, "backups")

        if os.path.isdir(e3dc_source_dir):
            os.makedirs(e3dc_extra_backup_dir, exist_ok=True)
            try:
                copied_e3dc = _copy_matching_files(
                    e3dc_source_dir,
                    e3dc_extra_backup_dir,
                    E3DC_CONTROL_EXTRA_EXTENSIONS,
                    exclude_dirs={backup_root_dir}
                )
                total_copied_files += copied_e3dc
                if copied_e3dc > 0:
                    print(f"  ✓ {copied_e3dc} E3DC-Control-Zusatzdateien gesichert")
                    backup_logger.info(f"{copied_e3dc} E3DC-Control-Zusatzdateien gesichert.")
                else:
                    print("  ⚠ Keine passenden E3DC-Control-Zusatzdateien gefunden")
                    backup_logger.warning("Keine passenden E3DC-Control-Zusatzdateien für Backup gefunden.")
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern der E3DC-Control-Zusatzdateien: {e}")
                log_error("backup", f"Fehler beim Sichern der E3DC-Control-Zusatzdateien: {e}", e)
        else:
            print(f"  ⚠ Quellordner fehlt: {e3dc_source_dir}")
            log_warning("backup", f"Quellordner für Backup fehlt: {e3dc_source_dir}")

        # Installer-Datei sichern
        plot_installer = os.path.join(os.path.dirname(__file__), "install.py")
        if os.path.exists(plot_installer):
            try:
                shutil.copy2(plot_installer, backup_dir)
                backup_logger.info("Installer-Skript gesichert.")
                total_copied_files += 1
            except Exception:
                pass

        # Rechte des Backups auf den Installationsbenutzer setzen
        try:
            uid, _ = get_user_ids()
            gid = get_www_data_gid()
            
            # Rekursiv alles im Backup-Ordner auf install_user:www-data setzen
            for root, dirs, files in os.walk(backup_dir):
                for d in dirs:
                    os.chown(os.path.join(root, d), uid, gid)
                for f in files:
                    os.chown(os.path.join(root, f), uid, gid)
            os.chown(backup_dir, uid, gid)
            print("  ✓ Besitzrechte auf install_user:www-data gesetzt")
            backup_logger.info("Besitzrechte für Backup-Ordner gesetzt.")
        except Exception as e:
            print(f"  ⚠ Konnte Besitzrechte für Backup nicht setzen: {e}")
            log_warning("backup", f"Konnte Besitzrechte für Backup nicht setzen: {e}")

        if total_copied_files == 0:
            print("  ⚠ Es wurden keine Dateien gesichert")
            backup_logger.warning("Es wurden keine Dateien gesichert.")
        else:
            print(f"  ✓ Insgesamt {total_copied_files} Dateien gesichert")
            backup_logger.info(f"Insgesamt {total_copied_files} Dateien gesichert.")

        print("✓ Backup abgeschlossen.\n")
        log_task_completed("Backup erstellen", details=f"{total_copied_files} Dateien in {os.path.basename(backup_dir)}")
        return backup_dir
    except Exception as e:
        print(f"✗ Fehler beim Backup: {e}\n")
        log_error("backup", f"Genereller Fehler beim Backup: {e}", e)
        return None


def choose_backup_version(action_text="wiederherstellen"):
    """Wählt eine Backup-Version für die angegebene Aktion aus."""
    backup_root = os.path.join(INSTALL_PATH, "backups")
    if not os.path.exists(backup_root):
        print("✗ Keine Backups vorhanden.\n")
        backup_logger.warning("Kein Backup-Verzeichnis gefunden.")
        return None

    try:
        versions = sorted(os.listdir(backup_root))
    except Exception as e:
        print(f"✗ Fehler beim Lesen der Backups: {e}\n")
        log_error("backup", f"Fehler beim Lesen des Backup-Verzeichnisses: {e}", e)
        return None

    if not versions:
        print("✗ Keine Backups vorhanden.\n")
        return None

    print("\nVerfügbare Backups:")
    for i, v in enumerate(versions):
        print(f"  {i+1}: {v}")

    choice = input(f"\nWelche Version {action_text}? (Nummer): ").strip()
    if not choice.isdigit():
        print("✗ Ungültige Eingabe.\n")
        return None

    try:
        idx = int(choice) - 1
        if idx < 0 or idx >= len(versions):
            print("✗ Ungültige Auswahl.\n")
            return None
        return os.path.join(backup_root, versions[idx])
    except (ValueError, IndexError):
        print("✗ Fehler bei der Auswahl.\n")
        return None


def restore_backup(backup_path):
    """Stellt eine Backup-Version wieder her."""
    print(f"\n⚠ Willst du wirklich {os.path.basename(backup_path)} wiederherstellen?")
    confirm = input("Ja/Nein [n]: ").strip().lower()
    if confirm != "ja":
        print("✗ Abgebrochen.\n")
        return False

    try:
        backup_logger.info(f"Starte Wiederherstellung von: {os.path.basename(backup_path)}")
        print("→ Stelle Backup wieder her…")
        total_restored_files = 0

        # Hauptprogramm wiederherstellen
        bin_backup = os.path.join(backup_path, "E3DC-Control")
        if os.path.exists(bin_backup):
            try:
                bin_dest = os.path.join(INSTALL_PATH, "E3DC-Control")
                shutil.copy2(bin_backup, bin_dest)
                total_restored_files += 1
                print("  ✓ Hauptprogramm wiederhergestellt")
                backup_logger.info("Hauptprogramm wiederhergestellt.")
            except Exception as e:
                print(f"  ✗ Fehler: {e}")
                log_error("backup", f"Fehler beim Wiederherstellen des Hauptprogramms: {e}", e)

        # Konfiguration wiederherstellen
        cfg_backup = os.path.join(backup_path, "e3dc.config.txt")
        if os.path.exists(cfg_backup):
            try:
                cfg_dest = os.path.join(INSTALL_PATH, "e3dc.config.txt")
                shutil.copy2(cfg_backup, cfg_dest)
                total_restored_files += 1
                print("  ✓ Konfiguration wiederhergestellt")
                backup_logger.info("Konfiguration wiederhergestellt.")
            except Exception as e:
                print(f"  ✗ Fehler: {e}")
                log_error("backup", f"Fehler beim Wiederherstellen der Konfiguration: {e}", e)

        # Webportal-Dateien wiederherstellen
        wp_backup_dir = os.path.join(backup_path, "webportal")
        if os.path.exists(wp_backup_dir):
            choice = input("\n→ Webportal-Dateien wiederherstellen? (j/n): ").strip().lower()
            if choice == "j":
                restored_webportal_count = 0
                for filename in os.listdir(wp_backup_dir):
                    src = os.path.join(wp_backup_dir, filename)
                    dst = os.path.join("/var/www/html", filename)
                    try:
                        if os.path.isdir(src):
                            shutil.copytree(src, dst, dirs_exist_ok=True)
                            restored_webportal_count += _count_files_recursive(src)
                        else:
                            shutil.copy2(src, dst)
                            restored_webportal_count += 1
                    except Exception as e:
                        print(f"  ⚠ {filename}: {e}")
                        log_warning("backup", f"Fehler bei Wiederherstellung von {filename}: {e}")
                total_restored_files += restored_webportal_count
                
                # Berechtigungen nach Restore korrigieren
                print("\n→ Korrigiere Berechtigungen nach Wiederherstellung…")
                from .permissions import run_permissions_wizard
                run_permissions_wizard()
                
                print("  ✓ Webportal-Dateien wiederhergestellt")
                backup_logger.info(f"{restored_webportal_count} Webportal-Dateien wiederhergestellt.")

        # E3DC-Control-Zusatzdateien wiederherstellen
        e3dc_extra_backup_dir = os.path.join(backup_path, "e3dc-control-extra")
        e3dc_target_dir = INSTALL_PATH
        if os.path.isdir(e3dc_extra_backup_dir) and os.path.isdir(e3dc_target_dir):
            restored_count = 0
            for root, _, files in os.walk(e3dc_extra_backup_dir):
                for filename in files:
                    src = os.path.join(root, filename)
                    rel_path = os.path.relpath(src, e3dc_extra_backup_dir)
                    dst = os.path.join(e3dc_target_dir, rel_path)
                    try:
                        os.makedirs(os.path.dirname(dst), exist_ok=True)
                        shutil.copy2(src, dst)
                        restored_count += 1
                    except Exception as e:
                        print(f"  ⚠ Fehler bei E3DC-Control-Zusatzdatei {rel_path}: {e}")
                        log_warning("backup", f"Fehler bei Wiederherstellung von Zusatzdatei {rel_path}: {e}")
            total_restored_files += restored_count
            print(f"  ✓ {restored_count} E3DC-Control-Zusatzdateien wiederhergestellt")
            backup_logger.info(f"{restored_count} E3DC-Control-Zusatzdateien wiederhergestellt.")

        if total_restored_files == 0:
            print("  ⚠ Es wurden keine Dateien wiederhergestellt")
            backup_logger.warning("Es wurden keine Dateien wiederhergestellt.")
        else:
            print(f"  ✓ Insgesamt {total_restored_files} Dateien wiederhergestellt")
            backup_logger.info(f"Insgesamt {total_restored_files} Dateien wiederhergestellt.")

        print("✓ Wiederherstellung abgeschlossen.\n")
        log_task_completed("Backup wiederherstellen", details=f"{total_restored_files} Dateien aus {os.path.basename(backup_path)}")
        return True
    except Exception as e:
        print(f"✗ Fehler bei Wiederherstellung: {e}\n")
        log_error("backup", f"Genereller Fehler bei Wiederherstellung: {e}", e)
        return False


def delete_backup():
    """Löscht eine ausgewählte Backup-Version."""
    backup_path = choose_backup_version("löschen")
    if not backup_path:
        return False

    if not os.path.isdir(backup_path):
        print("✗ Ungültiges Backup-Verzeichnis.\n")
        return False

    print(f"\n⚠ Willst du wirklich das Backup {os.path.basename(backup_path)} löschen?")
    confirm = input("Ja/Nein [n]: ").strip().lower()
    if confirm != "ja":
        print("✗ Abgebrochen.\n")
        return False

    try:
        shutil.rmtree(backup_path)
        print("✓ Backup gelöscht.\n")
        backup_logger.info(f"Backup gelöscht: {os.path.basename(backup_path)}")
        log_task_completed("Backup löschen", details=os.path.basename(backup_path))
        return True
    except Exception as e:
        print(f"✗ Fehler beim Löschen des Backups: {e}\n")
        log_error("backup", f"Fehler beim Löschen des Backups: {e}", e)
        return False


def backup_menu():
    """Menü für Backup-Verwaltung."""
    print("\n=== Backup-Verwaltung ===\n")
    print("1 = Backup erstellen")
    print("2 = Backup wiederherstellen")
    print("3 = Backup löschen")
    choice = input("Auswahl: ").strip()

    if choice == "1":
        backup_current_version()
    elif choice == "2":
        backup_path = choose_backup_version()
        if backup_path:
            restore_backup(backup_path)
    elif choice == "3":
        delete_backup()
    else:
        print("✗ Ungültige Auswahl.\n")


register_command("16", "Backup verwalten", backup_menu, sort_order=160)
