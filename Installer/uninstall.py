import os
import subprocess
import shutil

from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_install_user

INSTALL_PATH = get_install_path()


def uninstall_e3dc():
    """Deinstalliert E3DC-Control."""
    print("\n=== E3DC-Control deinstallieren ===\n")

    confirm = input("Bist du sicher, dass du E3DC-Control entfernen möchtest? (j/n): ").strip().lower()
    if confirm != "j":
        print("→ Abgebrochen.\n")
        return

    # Cronjob entfernen
    print("→ Entferne Cronjob…")
    try:
        install_user = get_install_user()
        result = run_command(f"sudo -u {install_user} crontab -l", timeout=5)
        
        if result['success']:
            lines = [
                l.strip() for l in result['stdout'].splitlines()
                if "E3DC" not in l and l.strip() and not l.strip().startswith("#")
            ]
            
            if lines:
                new_cron = "\n".join(lines) + "\n"
                process = subprocess.Popen(
                    ["sudo", "-u", install_user, "crontab", "-"],
                    stdin=subprocess.PIPE,
                    text=True
                )
                process.communicate(input=new_cron, timeout=10)
                print("✓ Cronjob entfernt")
            else:
                run_command(f"sudo -u {install_user} crontab -r", timeout=5)
                print("✓ Cronjob gelöscht")
    except Exception as e:
        print(f"⚠ Fehler beim Entfernen des Cronjobs: {e}")

    # Screen-Session beenden
    print("→ Beende Screen-Session…")
    install_user = get_install_user()
    run_command(f"sudo -u {install_user} screen -S E3DC -X quit", timeout=5)
    run_command(f"sudo -u {install_user} pkill -f E3DC-Control", timeout=5)
    print("✓ Screen-Session beendet")

    # Webportal optional löschen
    print("\n→ Webportal-Dateien:")
    wp = input("  Löschen? (j/n): ").strip().lower()
    if wp == "j":
        print("  → Entferne /var/www/html/*…")
        try:
            run_command("sudo rm -rf /var/www/html/*", timeout=10)
            print("  ✓ Webportal gelöscht")
        except Exception as e:
            print(f"  ✗ Fehler: {e}")

    # Konfiguration optional behalten
    print("\n→ Konfigurationsdateien:")
    keep_cfg = input("  Behalten? (j/n): ").strip().lower()

    try:
        if keep_cfg == "j":
            print("→ Entferne Programmdateien, behalte Konfiguration…")
            
            keep_files = [
                "e3dc.config.txt",
                "e3dc.wallbox.txt",
                "e3dc.strompreis.txt",
                "backups"
            ]
            
            if os.path.exists(INSTALL_PATH):
                for item in os.listdir(INSTALL_PATH):
                    if item not in keep_files:
                        path = os.path.join(INSTALL_PATH, item)
                        try:
                            if os.path.isdir(path):
                                shutil.rmtree(path)
                            else:
                                os.remove(path)
                        except Exception as e:
                            print(f"  ⚠ Konnte {item} nicht löschen: {e}")
            
            print("✓ Programm deinstalliert, Konfiguration behalten")
        else:
            print("→ Entferne gesamten Installationsordner…")
            if os.path.exists(INSTALL_PATH):
                shutil.rmtree(INSTALL_PATH, ignore_errors=True)
            print("✓ Installationsordner gelöscht")
    except Exception as e:
        print(f"✗ Fehler beim Löschen: {e}")

    print("\n✓ Deinstallation abgeschlossen.\n")


register_command("15", "Deinstallation", uninstall_e3dc, sort_order=150)