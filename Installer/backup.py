import os
import datetime
import shutil

from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_user_ids, get_www_data_gid

INSTALL_PATH = get_install_path()
WEBPORTAL_FILES = [
    "wallbox.php",
    "start_content.php",
    "auto.php",
    "index.php",
    "mobile.php",
    "status.php",
    "sw.js",
    "manifest.json",
    "archiv_diagramm.php",
    "run_now.php",
    "archiv.php",
    "config_editor.php"
]


def backup_current_version():
    """Erstellt ein Backup der aktuellen Version."""
    try:
        timestamp = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M")
        backup_dir = os.path.join(INSTALL_PATH, "backups", timestamp)
        os.makedirs(backup_dir, exist_ok=True)

        print(f"→ Erstelle Backup unter {backup_dir}…")

        # Hauptprogramm sichern
        bin_path = os.path.join(INSTALL_PATH, "E3DC-Control")
        if os.path.exists(bin_path):
            try:
                shutil.copy2(bin_path, backup_dir)
                print("  ✓ Hauptprogramm gesichert")
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern des Programms: {e}")

        # Konfiguration sichern
        cfg_path = os.path.join(INSTALL_PATH, "e3dc.config.txt")
        if os.path.exists(cfg_path):
            try:
                shutil.copy2(cfg_path, backup_dir)
                print("  ✓ Konfiguration gesichert")
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern der Konfiguration: {e}")

        # Webportal-Dateien sichern
        wp_backup_dir = os.path.join(backup_dir, "webportal")
        os.makedirs(wp_backup_dir, exist_ok=True)

        for filename in WEBPORTAL_FILES:
            src = os.path.join("/var/www/html", filename)
            if os.path.exists(src):
                try:
                    shutil.copy2(src, wp_backup_dir)
                except Exception as e:
                    print(f"  ⚠ Fehler bei {filename}: {e}")
        
        # Icons sichern
        icons_src = "/var/www/html/icons"
        if os.path.exists(icons_src):
            try:
                shutil.copytree(icons_src, os.path.join(wp_backup_dir, "icons"), dirs_exist_ok=True)
            except Exception as e:
                print(f"  ⚠ Fehler beim Sichern der Icons: {e}")
        
        print("  ✓ Webportal-Dateien gesichert")

        # Installer-Datei sichern
        plot_installer = os.path.join(os.path.dirname(__file__), "install.py")
        if os.path.exists(plot_installer):
            try:
                shutil.copy2(plot_installer, backup_dir)
            except Exception:
                pass

        # Rechte des Backups auf den Installationsbenutzer setzen
        try:
            uid, _ = get_user_ids()
            gid = get_www_data_gid()
            
            # Rekursiv alles im Backup-Ordner auf pi:www-data setzen
            for root, dirs, files in os.walk(backup_dir):
                for d in dirs:
                    os.chown(os.path.join(root, d), uid, gid)
                for f in files:
                    os.chown(os.path.join(root, f), uid, gid)
            os.chown(backup_dir, uid, gid)
            print("  ✓ Besitzrechte auf pi:www-data gesetzt")
        except Exception as e:
            print(f"  ⚠ Konnte Besitzrechte für Backup nicht setzen: {e}")

        print("✓ Backup abgeschlossen.\n")
        return backup_dir
    except Exception as e:
        print(f"✗ Fehler beim Backup: {e}\n")
        return None


def choose_backup_version():
    """Wählt eine Backup-Version aus."""
    backup_root = os.path.join(INSTALL_PATH, "backups")
    if not os.path.exists(backup_root):
        print("✗ Keine Backups vorhanden.\n")
        return None

    try:
        versions = sorted(os.listdir(backup_root))
    except Exception as e:
        print(f"✗ Fehler beim Lesen der Backups: {e}\n")
        return None

    if not versions:
        print("✗ Keine Backups vorhanden.\n")
        return None

    print("\nVerfügbare Backups:")
    for i, v in enumerate(versions):
        print(f"  {i+1}: {v}")

    choice = input("\nWelche Version wiederherstellen? (Nummer): ").strip()
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
        print("→ Stelle Backup wieder her…")

        # Hauptprogramm wiederherstellen
        bin_backup = os.path.join(backup_path, "E3DC-Control")
        if os.path.exists(bin_backup):
            try:
                bin_dest = os.path.join(INSTALL_PATH, "E3DC-Control")
                shutil.copy2(bin_backup, bin_dest)
                print("  ✓ Hauptprogramm wiederhergestellt")
            except Exception as e:
                print(f"  ✗ Fehler: {e}")

        # Konfiguration wiederherstellen
        cfg_backup = os.path.join(backup_path, "e3dc.config.txt")
        if os.path.exists(cfg_backup):
            try:
                cfg_dest = os.path.join(INSTALL_PATH, "e3dc.config.txt")
                shutil.copy2(cfg_backup, cfg_dest)
                print("  ✓ Konfiguration wiederhergestellt")
            except Exception as e:
                print(f"  ✗ Fehler: {e}")

        # Webportal-Dateien wiederherstellen
        wp_backup_dir = os.path.join(backup_path, "webportal")
        if os.path.exists(wp_backup_dir):
            choice = input("\n→ Webportal-Dateien wiederherstellen? (j/n): ").strip().lower()
            if choice == "j":
                for filename in os.listdir(wp_backup_dir):
                    src = os.path.join(wp_backup_dir, filename)
                    dst = os.path.join("/var/www/html", filename)
                    try:
                        if os.path.isdir(src):
                            shutil.copytree(src, dst, dirs_exist_ok=True)
                        else:
                            shutil.copy2(src, dst)
                    except Exception as e:
                        print(f"  ⚠ {filename}: {e}")
                
                # Berechtigungen nach Restore korrigieren
                print("\n→ Korrigiere Berechtigungen nach Wiederherstellung…")
                from .permissions import run_permissions_wizard
                run_permissions_wizard()
                
                print("  ✓ Webportal-Dateien wiederhergestellt")

        print("✓ Wiederherstellung abgeschlossen.\n")
        return True
    except Exception as e:
        print(f"✗ Fehler bei Wiederherstellung: {e}\n")
        return False


def backup_menu():
    """Menü für Backup-Verwaltung."""
    print("\n=== Backup-Verwaltung ===\n")
    print("1 = Backup erstellen")
    print("2 = Backup wiederherstellen")
    choice = input("Auswahl: ").strip()

    if choice == "1":
        backup_current_version()
    elif choice == "2":
        backup_path = choose_backup_version()
        if backup_path:
            restore_backup(backup_path)
    else:
        print("✗ Ungültige Auswahl.\n")


register_command("14", "Backup verwalten", backup_menu, sort_order=140)
