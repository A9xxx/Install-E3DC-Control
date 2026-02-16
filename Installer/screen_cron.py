import os
import subprocess
import shlex
import time

from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_user_ids, get_install_user

INSTALL_PATH = get_install_path()


def is_e3dc_running():
    """Prüft ob E3DC in einer Screen-Session läuft."""
    install_user = get_install_user()
    # Wichtig: screen -ls muss als der richtige Benutzer laufen, um dessen Sessions zu sehen!
    result = run_command(f"sudo -u {install_user} screen -ls", timeout=5)
    
    # screen -ls kann Exit-Code 1 zurückgeben (ähnlich wie crontab -l)
    # Das ist nicht unbedingt ein Fehler, also prüfe stdout/stderr
    if not result['success'] and result.get('returncode') != 1:
        return None
    
    output = result['stdout'] if result['success'] else result.get('stderr', '')
    
    # Prüfe ob E3DC-Session aktiv ist
    for line in output.split("\n"):
        if "E3DC" in line and ("Attached" in line or "Detached" in line):
            return True
    
    return False


def start_e3dc_control():
    """Startet E3DC-Control in einer Screen-Session."""
    print("\n=== E3DC-Control starten ===\n")

    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")
    install_user = get_install_user()

    # Prüfe ob E3DC.sh existiert
    if not os.path.exists(sh_path):
        print(f"✗ Startskript nicht gefunden: {sh_path}")
        choice = input("Soll 'Screen + Cronjob einrichten' jetzt ausgeführt werden? (j/n): ").strip().lower()
        if choice == "j":
            install_screen_cron()
            print("→ Versuche jetzt E3DC zu starten...\n")
            # Starte E3DC erneut nach Setup
            start_e3dc_control()
        else:
            print("→ Abgebrochen.\n")
        return
    
    # Prüfe ob E3DC.sh ausführbar ist
    if not os.access(sh_path, os.X_OK):
        print(f"⚠ Startskript ist nicht ausführbar: {sh_path}")
        choice = input("Soll es ausführbar gemacht werden? (j/n): ").strip().lower()
        if choice == "j":
            try:
                os.chmod(sh_path, 0o755)
                print("✓ Startskript ist jetzt ausführbar.\n")
            except Exception as e:
                print(f"✗ Fehler beim Setzen der Ausführungsrechte: {e}\n")
                return
        else:
            print("→ Kann nicht gestartet werden ohne Ausführungsrechte.\n")
            return

    # Prüfe ob E3DC bereits läuft (stillen Modus, keine Warnung)
    running = is_e3dc_running()
    
    if running:
        print("⚠ E3DC-Control läuft bereits.")
        choice = input("Möchtest du es neu starten? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Abgebrochen.\n")
            return
        print("→ Stoppe alte Session…")
        run_command(f"sudo -u {install_user} screen -S E3DC -X quit", timeout=5)

    print("→ Starte E3DC-Control…")
    # Wichtig: Screen muss als der richtige Benutzer laufen, nicht als root!
    result = run_command(f"sudo -u {install_user} screen -dmS E3DC {shlex.quote(sh_path)}", timeout=5)
    
    if result['success']:
        print("✓ E3DC-Control gestartet.\n")
        
        # Verifiziere dass es wirklich läuft
        time.sleep(1)
        if is_e3dc_running():
            print("✓ E3DC-Control läuft im Hintergrund.")
            choice = input("Möchtest du die Screen-Session öffnen? (j/n): ").strip().lower()
            if choice == "j":
                print(f"→ Öffne Screen-Session…\n")
                os.system(f"sudo -u {install_user} screen -x E3DC")
            else:
                print("  Mit 'screen -r E3DC' kannst du die Ausgabe später ansehen.\n")
        else:
            print("⚠ Warnung: Konnte nicht verifizieren, dass E3DC läuft.")
            print("  Prüfe manuell mit: screen -ls\n")
    else:
        print(f"✗ Start fehlgeschlagen: {result['stderr']}\n")


def get_user_crontab():
    """Holt den aktuellen Benutzer-Crontab. Liest direkt die Datei falls möglich."""
    # Verwende den echten Installationsbenutzer (nicht getuid(), da Installer mit sudo läuft!)
    install_user = get_install_user()
    
    # Versuche, die crontab-Datei direkt zu lesen
    crontab_file = f"/var/spool/cron/crontabs/{install_user}"
    
    try:
        if os.path.exists(crontab_file):
            with open(crontab_file, 'r') as f:
                return f.read()
    except Exception:
        pass
    
    # Fallback: Verwende crontab -l
    result = run_command("crontab -l", timeout=5)
    if result['success']:
        return result['stdout']
    elif "no crontab for" in result['stderr'].lower() or "no crontab found" in result['stderr'].lower():
        return ""
    else:
        return ""


def get_root_crontab():
    """Holt den Root-Crontab. Liest direkt die Datei falls möglich."""
    # Versuche, die root-crontab-Datei direkt zu lesen
    root_crontab_file = "/var/spool/cron/crontabs/root"
    
    try:
        if os.path.exists(root_crontab_file):
            with open(root_crontab_file, 'r') as f:
                return f.read()
    except Exception:
        pass
    
    # Fallback: Verwende sudo crontab -l
    result = run_command("sudo crontab -l", timeout=5)
    if result['success']:
        return result['stdout']
    elif "no crontab for" in result['stderr'].lower() or "no crontab found" in result['stderr'].lower():
        return ""
    else:
        return ""


def set_crontab(crontab_content, use_sudo=False):
    """Schreibt einen neuen Crontab."""
    try:
        install_user = get_install_user()
        
        # Bereinige leere Zeilen am Anfang und Ende
        lines = [line for line in crontab_content.split('\n') if line.strip() or line == '']
        cleaned_content = '\n'.join(lines)
        
        # Stelle sicher, dass die crontab mit Newline endet
        if cleaned_content and not cleaned_content.endswith('\n'):
            cleaned_content += '\n'
        
        if use_sudo:
            # Root-Crontab
            cmd = ["sudo", "crontab", "-"]
        else:
            # Benutzer-Crontab mit korrektem User-Kontext
            # Verwende sudo -u um sicherzustellen, dass es in der richtigen crontab gespeichert wird
            cmd = ["sudo", "-u", install_user, "crontab", "-"]
        
        process = subprocess.Popen(
            cmd, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True
        )
        stdout, stderr = process.communicate(input=cleaned_content, timeout=10)
        
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
    """Richtet Screen und Cronjob ein. Gibt True bei Erfolg zurück."""
    print("\n=== Screen + Cronjob einrichten ===\n")

    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")

    # Erstelle E3DC.sh Startskript
    print("→ Erstelle Startskript…")
    
    # Prüfe ob E3DC.sh schon existiert
    if os.path.exists(sh_path):
        print(f"⚠ Startskript existiert bereits: {sh_path}")
        choice = input("Überschreiben? (j/n): ").strip().lower()
        if choice != "j":
            print("→ Startskript wird beibehalten.\n")
        else:
            try:
                with open(sh_path, "w") as f:
                    f.write("#!/bin/bash\n")
                    f.write(f"cd {INSTALL_PATH}\n")
                    f.write("while true; do ./E3DC-Control; sleep 30; done\n")
                
                os.chmod(sh_path, 0o755)
                uid, gid = get_user_ids()
                os.chown(sh_path, uid, gid)
                print("✓ Startskript überschrieben (Install-User, 755).\n")
            except Exception as e:
                print(f"✗ Fehler beim Erstellen des Startskripts: {e}\n")
                return False
    else:
        try:
            with open(sh_path, "w") as f:
                f.write("#!/bin/bash\n")
                f.write(f"cd {INSTALL_PATH}\n")
                f.write("while true; do ./E3DC-Control; sleep 30; done\n")
            
            # Setze Berechtigungen: Owner auf Install-User, ausführbar
            os.chmod(sh_path, 0o755)  # rwxr-xr-x
            uid, gid = get_user_ids()
            os.chown(sh_path, uid, gid)
            print("✓ Startskript erstellt (Install-User, 755).\n")
        except Exception as e:
            print(f"✗ Fehler beim Erstellen des Startskripts: {e}\n")
            return False

    # Crontab-Eintrag
    cron_line = f"@reboot sleep 10 && echo 0 > {INSTALL_PATH}/stop && /usr/bin/screen -dmS E3DC {sh_path}"
    entry_identifier = "E3DC"

    # Benutzer-Crontab (OHNE sudo!)
    print("→ Richte Benutzer-Cronjob ein…")
    user_cron = get_user_crontab()

    if crontab_has_entry(user_cron, entry_identifier):
        print("✓ Cronjob ist bereits vorhanden.")
    else:
        print("  → Cronjob fehlt – füge ihn hinzu…")
        new_cron = user_cron.rstrip() + "\n" + cron_line + "\n"
        success, error = set_crontab(new_cron, use_sudo=False)
        
        if success:
            # Verifiziere, dass der Eintrag tatsächlich gespeichert wurde
            time.sleep(1)
            verify_cron = get_user_crontab()
            
            if crontab_has_entry(verify_cron, entry_identifier):
                print("✓ Cronjob zum Benutzer-crontab hinzugefügt.")
            else:
                print(f"✗ Cronjob konnte nicht verifiziert werden!")
                print("  Prüfe manuell mit: crontab -l\n")
                return False
        else:
            print(f"✗ Fehler beim Schreiben des Crontabs: {error}\n")
            return False

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
    
    # Finale Verifizierung: Zeige den crontab-Eintrag
    print("→ Verifizierung - aktueller Benutzer-Crontab:")
    print("-" * 60)
    final_cron = get_user_crontab()
    if final_cron.strip():
        print(final_cron)
    else:
        print("⚠ WARNUNG: Benutzer-Crontab ist LEER!")
    print("-" * 60 + "\n")
    
    return True


def screen_cron_menu():
    """Menü für Screen+Cron Setup."""
    install_screen_cron()


def start_menu():
    """Menü für Start."""
    start_e3dc_control()


register_command("7", "Screen + Cronjob einrichten", install_screen_cron, sort_order=70)
register_command("10", "E3DC-Control starten", start_e3dc_control, sort_order=100)
