import os
import subprocess
import shlex

from .core import register_command
from .utils import run_command

INSTALL_PATH = "/home/pi/E3DC-Control"


def is_e3dc_running():
    """Prüft ob E3DC in einer Screen-Session läuft."""
    result = run_command("screen -ls", timeout=5)
    
    if not result['success']:
        return None
    
    # Prüfe ob E3DC-Session aktiv ist
    for line in result['stdout'].split("\n"):
        if "E3DC" in line and ("Attached" in line or "Detached" in line):
            return True
    
    return False


def start_e3dc_control():
    """Startet E3DC-Control in einer Screen-Session."""
    print("\n=== E3DC-Control starten ===\n")

    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")

    if not os.path.exists(sh_path):
        print(f"✗ Startskript nicht gefunden: {sh_path}")
        print("→ Bitte zuerst 'Screen + Cronjob einrichten' ausführen.\n")
        return

    running = is_e3dc_running()
    
    if running is None:
        print("⚠ Konnte Screen-Status nicht prüfen.")
        return
    
    if running:
        print("⚠ E3DC-Control läuft bereits.")
        choice = input("Möchtest du es neu starten? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Abgebrochen.\n")
            return
        print("→ Stoppe alte Session…")
        run_command("screen -S E3DC -X quit", timeout=5)

    print("→ Starte E3DC-Control…")
    result = run_command(f"screen -dmS E3DC {shlex.quote(sh_path)}", timeout=5)
    
    if result['success']:
        print("✓ E3DC-Control läuft im Hintergrund.")
        print("  Mit 'screen -r E3DC' kannst du die Ausgabe ansehen.\n")
    else:
        print(f"✗ Start fehlgeschlagen: {result['stderr']}\n")


def get_user_crontab():
    """Holt den aktuellen Benutzer-Crontab."""
    result = run_command("crontab -l", timeout=5)
    return result['stdout'] if result['success'] else ""


def get_root_crontab():
    """Holt den Root-Crontab."""
    result = run_command("sudo crontab -l", timeout=5)
    return result['stdout'] if result['success'] else ""


def set_crontab(crontab_content, use_sudo=False):
    """Schreibt einen neuen Crontab."""
    try:
        cmd = ["sudo", "crontab", "-"] if use_sudo else ["crontab", "-"]
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True
        )
        _, stderr = process.communicate(input=crontab_content, timeout=10)
        
        if process.returncode != 0:
            return False, stderr
        return True, None
    except subprocess.TimeoutExpired:
        process.kill()
        return False, "Timeout"
    except Exception as e:
        return False, str(e)


def crontab_has_entry(crontab_content, entry_identifier):
    """Prüft ob ein Crontab-Eintrag existiert."""
    for line in crontab_content.split("\n"):
        line = line.strip()
        if line.startswith("#") or not line:
            continue
        if entry_identifier in line:
            return True
    return False


def install_screen_cron():
    """Richtet Screen und Cronjob ein."""
    print("\n=== Screen + Cronjob einrichten ===\n")

    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")

    # Erstelle E3DC.sh Startskript
    print("→ Erstelle Startskript…")
    try:
        with open(sh_path, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"cd {INSTALL_PATH}\n")
            f.write("while true; do ./E3DC-Control; sleep 30; done\n")
        
        os.chmod(sh_path, 0o755)
        print("✓ Startskript erstellt.\n")
    except Exception as e:
        print(f"✗ Fehler beim Erstellen des Startskripts: {e}\n")
        return

    # Crontab-Eintrag
    cron_line = f"@reboot sleep 10 && echo 0 > {INSTALL_PATH}/stop && /usr/bin/screen -dmS E3DC {sh_path}"
    entry_identifier = "E3DC"

    # Benutzer-Crontab
    print("→ Richte Benutzer-Cronjob ein…")
    user_cron = get_user_crontab()

    if crontab_has_entry(user_cron, entry_identifier):
        print("✓ Cronjob ist bereits vorhanden.")
    else:
        print("  → Cronjob fehlt – füge ihn hinzu…")
        new_cron = user_cron.strip() + "\n" + cron_line + "\n"
        success, error = set_crontab(new_cron)
        
        if success:
            print("✓ Cronjob zum Benutzer-crontab hinzugefügt.")
        else:
            print(f"✗ Fehler beim Schreiben des Crontabs: {error}\n")
            return

    # Root-Crontab: Entferne Eintrag falls vorhanden
    print("\n→ Prüfe root-crontab…")
    root_cron = get_root_crontab()

    if crontab_has_entry(root_cron, entry_identifier):
        print("⚠ Eintrag im root-crontab gefunden – entferne ihn…")
        cleaned_lines = [
            line for line in root_cron.split("\n")
            if not (entry_identifier in line and not line.strip().startswith("#"))
        ]
        new_root_cron = "\n".join(cleaned_lines) + "\n"
        
        success, error = set_crontab(new_root_cron, use_sudo=True)
        if success:
            print("✓ Eintrag aus root-crontab entfernt.")
        else:
            print(f"⚠ Fehler beim Ändern des root-crontab: {error}")
    else:
        print("✓ root-crontab ist sauber.")

    print("\n✓ Screen + Cronjob vollständig eingerichtet.\n")


def screen_cron_menu():
    """Menü für Screen+Cron Setup."""
    install_screen_cron()


def start_menu():
    """Menü für Start."""
    start_e3dc_control()


register_command("7", "Screen + Cronjob einrichten", screen_cron_menu, sort_order=70)
register_command("10", "E3DC-Control starten", start_menu, sort_order=100)
