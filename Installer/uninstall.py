import os
import subprocess
import shutil

from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_install_user
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
uninstall_logger = get_or_create_logger("uninstall")


def uninstall_e3dc():
    """Deinstalliert E3DC-Control."""
    print("\n=== E3DC-Control deinstallieren ===\n")

    confirm = input("Bist du sicher, dass du E3DC-Control entfernen möchtest? (j/n): ").strip().lower()
    if confirm != "j":
        print("→ Abgebrochen.\n")
        uninstall_logger.info("Deinstallation vom Benutzer abgebrochen.")
        return

    uninstall_logger.info("Starte Deinstallation von E3DC-Control.")

    # Cronjob entfernen
    print("→ Entferne Cronjobs…")
    try:
        install_user = get_install_user()
        result = run_command(f"sudo -u {install_user} crontab -l", timeout=5)
        
        if result['success']:
            lines = [
                l.strip() for l in result['stdout'].splitlines()
                if "E3DC" not in l and "get_live.sh" not in l and l.strip() and not l.strip().startswith("#")
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
            uninstall_logger.info("Cronjobs entfernt.")
    except Exception as e:
        print(f"⚠ Fehler beim Entfernen des Cronjobs: {e}")
        log_warning("uninstall", f"Fehler beim Entfernen des Cronjobs: {e}")

    # Screen-Session beenden
    print("→ Beende Screen-Sessions…")
    install_user = get_install_user()
    run_command(f"sudo -u {install_user} screen -S E3DC -X quit", timeout=5)
    run_command(f"sudo -u {install_user} screen -S live-grabber -X quit", timeout=5)
    run_command(f"sudo -u {install_user} pkill -f E3DC-Control", timeout=5)
    run_command(f"sudo -u {install_user} pkill -f get_live.sh", timeout=5)
    print("✓ Screen-Sessions beendet")
    uninstall_logger.info("Screen-Sessions beendet.")

    # RAM-Disk entfernen
    print("→ Entferne RAM-Disk…")
    run_command("sudo umount /var/www/html/ramdisk", timeout=5)
    try:
        if os.path.exists("/etc/fstab"):
            with open("/etc/fstab", "r") as f:
                f_lines = f.readlines()
            with open("/etc/fstab", "w") as f:
                for line in f_lines:
                    if "/var/www/html/ramdisk" not in line:
                        f.write(line)
        print("✓ fstab bereinigt")
        uninstall_logger.info("RAM-Disk aus fstab entfernt.")
    except Exception as e:
        print(f"⚠ Fehler beim fstab Bereinigen: {e}")
        log_warning("uninstall", f"Fehler beim Bereinigen der fstab: {e}")

    # Webportal optional löschen
    print("\n→ Webportal-Dateien:")
    wp = input("  Löschen? (j/n): ").strip().lower()
    if wp == "j":
        print("  → Entferne /var/www/html/*…")
        try:
            run_command("sudo rm -rf /var/www/html/*", timeout=10)
            print("  ✓ Webportal gelöscht")
            uninstall_logger.info("Webportal-Dateien gelöscht.")
        except Exception as e:
            print(f"  ✗ Fehler: {e}")
            log_error("uninstall", f"Fehler beim Löschen des Webportals: {e}", e)

    # Konfiguration optional behalten
    print("\n→ Konfigurationsdateien:")
    keep_cfg = input("  Behalten? (j/n): ").strip().lower()

    # Grabber Skript entfernen
    grabber_script = os.path.join(os.path.expanduser(f"~{get_install_user()}"), "get_live.sh")
    if os.path.exists(grabber_script):
        try:
            os.remove(grabber_script)
            print("✓ Grabber-Skript entfernt")
            uninstall_logger.info(f"Grabber-Skript entfernt: {grabber_script}")
        except Exception as e:
            print(f"⚠ Fehler beim Löschen von {grabber_script}: {e}")
            log_warning("uninstall", f"Fehler beim Löschen von {grabber_script}: {e}")

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
                            log_warning("uninstall", f"Konnte {item} nicht löschen: {e}")
            
            print("✓ Programm deinstalliert, Konfiguration behalten")
            uninstall_logger.info("Programm deinstalliert, Konfiguration behalten.")
        else:
            print("→ Entferne gesamten Installationsordner…")
            if os.path.exists(INSTALL_PATH):
                shutil.rmtree(INSTALL_PATH, ignore_errors=True)
            print("✓ Installationsordner gelöscht")
            uninstall_logger.info("Gesamter Installationsordner gelöscht.")
    except Exception as e:
        print(f"✗ Fehler beim Löschen: {e}")
        log_error("uninstall", f"Fehler beim Löschen der Programmdateien: {e}", e)

    print("\n✓ Deinstallation abgeschlossen.\n")
    log_task_completed("Deinstallation")


register_command("20", "Deinstallation", uninstall_e3dc, sort_order=200)