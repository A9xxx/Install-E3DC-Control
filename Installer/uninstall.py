import os
import subprocess
import shutil

from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_install_user, get_home_dir, load_config
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
uninstall_logger = get_or_create_logger("uninstall")


def remove_cron_pattern(pattern):
    """Entfernt Zeilen aus der Crontab, die das Pattern enthalten."""
    try:
        install_user = get_install_user()
        result = run_command(f"sudo -u {install_user} crontab -l", timeout=5)
        
        if result['success']:
            lines = result['stdout'].splitlines()
            # Behalte Zeilen, die das Pattern NICHT enthalten
            new_lines = [l for l in lines if pattern not in l and l.strip()]
            
            # Wenn sich die Anzahl geändert hat, schreiben wir neu
            if len(lines) != len(new_lines):
                new_cron = "\n".join(new_lines) + "\n"
                if not new_lines:
                     # Wenn leer, crontab entfernen
                     run_command(f"sudo -u {install_user} crontab -r", timeout=5)
                else:
                    process = subprocess.Popen(
                        ["sudo", "-u", install_user, "crontab", "-"],
                        stdin=subprocess.PIPE,
                        text=True
                    )
                    process.communicate(input=new_cron, timeout=10)
                return True
    except Exception as e:
        log_warning("uninstall", f"Fehler beim Entfernen von Cron-Pattern '{pattern}': {e}")
    return False

def uninstall_watchdog():
    """Entfernt Watchdog (Service, Skripte, Cron)."""
    print("\n→ Entferne Watchdog (Piguard)…")
    
    # Service stoppen und entfernen
    run_command("sudo systemctl stop piguard", timeout=10)
    run_command("sudo systemctl disable piguard", timeout=10)
    if os.path.exists("/etc/systemd/system/piguard.service"):
        os.remove("/etc/systemd/system/piguard.service")
        run_command("sudo systemctl daemon-reload")
        print("  ✓ Service entfernt")

    # Skripte entfernen
    for f in ["/usr/local/bin/pi_guard.sh", "/usr/local/bin/boot_notify.sh"]:
        if os.path.exists(f):
            os.remove(f)
            print(f"  ✓ {f} gelöscht")

    # Cronjobs entfernen
    if remove_cron_pattern("boot_notify.sh"):
        print("  ✓ Cronjobs bereinigt")
    
    uninstall_logger.info("Watchdog deinstalliert.")
    log_task_completed("Deinstallation (Watchdog)")

def uninstall_ramdisk():
    """Entfernt RAM-Disk und Live-Grabber."""
    print("\n→ Entferne RAM-Disk & Live-Status…")
    install_user = get_install_user()

    # Service stoppen und entfernen
    run_command("sudo systemctl stop e3dc-grabber", timeout=10)
    run_command("sudo systemctl disable e3dc-grabber", timeout=10)
    if os.path.exists("/etc/systemd/system/e3dc-grabber.service"):
        os.remove("/etc/systemd/system/e3dc-grabber.service")
        run_command("sudo systemctl daemon-reload")
        print("  ✓ Service 'e3dc-grabber' entfernt")

    # Screen/Prozesse killen
    run_command(f"sudo -u {install_user} screen -S live-grabber -X quit", timeout=5)
    run_command(f"sudo -u {install_user} pkill -f get_live.sh", timeout=5)

    # Unmount
    run_command("sudo umount /var/www/html/ramdisk", timeout=5)
    
    # fstab bereinigen
    try:
        if os.path.exists("/etc/fstab"):
            with open("/etc/fstab", "r") as f:
                lines = f.readlines()
            with open("/etc/fstab", "w") as f:
                for line in lines:
                    if "/var/www/html/ramdisk" not in line:
                        f.write(line)
            run_command("sudo systemctl daemon-reload")
            print("  ✓ fstab bereinigt")
    except Exception as e:
        print(f"  ⚠ Fehler bei fstab: {e}")

    # Skript löschen
    grabber_script = os.path.join(get_home_dir(install_user), "get_live.sh")
    if os.path.exists(grabber_script):
        os.remove(grabber_script)
        print("  ✓ get_live.sh gelöscht")

    # Cronjobs
    remove_cron_pattern("get_live.sh")
    remove_cron_pattern("get_live_json.php")
    print("  ✓ Cronjobs bereinigt")
    
    uninstall_logger.info("RAM-Disk deinstalliert.")
    log_task_completed("Deinstallation (RAM-Disk)")

def uninstall_diagramm():
    """Entfernt Diagramm-Skripte und Webportal."""
    print("\n→ Entferne Diagramm-System & Webportal…")
    
    # Cronjobs
    remove_cron_pattern("plot_soc_changes.py")
    remove_cron_pattern("backup_history.php")
    
    # Sudoers
    if os.path.exists("/etc/sudoers.d/010_e3dc_web_git"):
        os.remove("/etc/sudoers.d/010_e3dc_web_git")
        print("  ✓ Sudoers (git) entfernt")
    
    if os.path.exists("/etc/sudoers.d/010_e3dc_web_update"):
        os.remove("/etc/sudoers.d/010_e3dc_web_update")
        print("  ✓ Sudoers (update) entfernt")

    # Python Skripte im Install-Ordner
    for f in ["plot_soc_changes.py", "plot_live_history.py"]:
        p = os.path.join(INSTALL_PATH, f)
        if os.path.exists(p):
            os.remove(p)
            print(f"  ✓ {f} gelöscht")
    
    uninstall_logger.info("Diagramm-System deinstalliert.")
    log_task_completed("Deinstallation (Diagramm)")

def uninstall_service():
    """Entfernt E3DC Systemd Service."""
    print("\n→ Entferne E3DC-Control Service…")
    install_user = get_install_user()
    
    # Stop & Disable
    run_command("sudo systemctl stop e3dc", timeout=10)
    run_command("sudo systemctl disable e3dc", timeout=10)
    
    # Screen killen
    run_command(f"sudo -u {install_user} screen -S E3DC -X quit", timeout=5)
    
    # Service File weg
    if os.path.exists("/etc/systemd/system/e3dc.service"):
        os.remove("/etc/systemd/system/e3dc.service")
        run_command("sudo systemctl daemon-reload")
        print("  ✓ Service-Datei entfernt")
        
    # Startskript weg
    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")
    if os.path.exists(sh_path):
        os.remove(sh_path)
        print("  ✓ E3DC.sh entfernt")
        
    # Legacy Cronjob entfernen (falls vorhanden)
    if remove_cron_pattern("E3DC.sh"):
        print("  ✓ Legacy Cronjob entfernt")

    uninstall_logger.info("E3DC Service deinstalliert.")
    log_task_completed("Deinstallation (Service)")


def uninstall_system_packages():
    """Entfernt die installierten System-Pakete."""
    print("\n→ Entferne System-Pakete…")
    
    packages = [
        "curl", "jq", "python3-bs4", "git", "screen",
        "apache2", "php", "python3-pip", "python3-venv",
        "python3-plotly", "libjpeg-dev", "zlib1g-dev",
        "libcurl4-openssl-dev", "libssl-dev",
        "libmosquitto-dev", "libjsoncpp-dev",
        "libsqlite3-dev", "build-essential", "cmake"
    ]
    
    print("  → Folgende Pakete werden entfernt:")
    print("  " + ", ".join(packages))
    
    if input("\n  Fortfahren? (j/n): ").strip().lower() != 'j':
        print("→ Übersprungen.")
        return

    # Autoremove, um Abhängigkeiten zu bereinigen
    run_command("sudo apt-get -y autoremove --purge " + " ".join(packages), timeout=300)
    
    print("✓ System-Pakete entfernt.")
    uninstall_logger.info("System-Pakete deinstalliert.")
    log_task_completed("Deinstallation (System-Pakete)")


def uninstall_venv():
    """Entfernt das Python Virtual Environment."""
    config = load_config()
    venv_name = config.get("venv_name", ".venv_e3dc")
    install_user = config.get("install_user")
    
    if not install_user:
        print("  ✗ Installationsbenutzer nicht gefunden. Überspringe venv-Deinstallation.")
        return

    home_dir = get_home_dir(install_user)
    venv_path = os.path.join(home_dir, venv_name)

    print(f"\n→ Entferne Python venv ({venv_path})…")

    if os.path.exists(venv_path):
        try:
            shutil.rmtree(venv_path)
            print(f"  ✓ {venv_path} gelöscht")
            uninstall_logger.info(f"venv entfernt: {venv_path}")
        except Exception as e:
            print(f"  ✗ Fehler beim Löschen: {e}")
            log_error("uninstall", f"Fehler beim Löschen von venv: {e}", e)
    else:
        print("  ℹ️  Kein venv gefunden.")
    
    log_task_completed("Deinstallation (venv)")


def uninstall_full():
    """Komplette Deinstallation."""
    print("\n=== Vollständige Deinstallation ===\n")
    print("ACHTUNG: Dieser Vorgang entfernt ALLE zugehörigen Komponenten,")
    print("inklusive Webportal, Datenbanken und System-Pakete.")
    
    if input("Wirklich ALLES entfernen? (j/n): ").strip().lower() != "j":
        return

    # Reihenfolge optimiert:
    uninstall_watchdog()
    uninstall_service()
    uninstall_ramdisk()
    
    # Webportal ohne Nachfrage entfernen
    print("\n→ Entferne Webportal…")
    run_command("sudo rm -rf /var/www/html/*", timeout=20)
    print("  ✓ Webverzeichnis geleert")
    
    uninstall_diagramm()
    uninstall_venv()
    
    # System-Pakete deinstallieren
    uninstall_system_packages()
    
    # Config & Binary ohne Nachfrage entfernen
    print("\n→ Programmdateien & Konfiguration:")
    if os.path.exists(INSTALL_PATH):
        shutil.rmtree(INSTALL_PATH, ignore_errors=True)
        print("  ✓ Installationsordner gelöscht")
            
    print("\n✓ Deinstallation abgeschlossen.\n")
    log_task_completed("Vollständige Deinstallation")


def uninstall_menu():
    """Menü für Deinstallation."""
    print("\n=== Deinstallation ===")
    print("1. Alles entfernen (Full Uninstall)")
    print("2. Nur Watchdog entfernen")
    print("3. Nur RAM-Disk & Live-Status entfernen")
    print("4. Nur Diagramm & Webportal entfernen")
    print("5. Nur E3DC-Service entfernen")
    print("6. Nur Python venv entfernen")
    print("7. Abbrechen")
    
    choice = input("Auswahl: ").strip()
    
    if choice == "1": uninstall_full()
    elif choice == "2": uninstall_watchdog()
    elif choice == "3": uninstall_ramdisk()
    elif choice == "4": uninstall_diagramm()
    elif choice == "5": uninstall_service()
    elif choice == "6": uninstall_venv()
    else: print("Abbruch.")

register_command("20", "Deinstallation", uninstall_menu, sort_order=200)