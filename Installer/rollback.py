import os
import subprocess
import time
import shutil

from .core import register_command
from .backup import choose_backup_version, restore_backup
from .utils import replace_in_file, run_command
from .installer_config import get_install_path, get_install_user
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
rollback_logger = get_or_create_logger("rollback")


def hard_stop_e3dc():
    """Stoppt E3DC-Control vollständig."""
    print("→ Stoppe E3DC-Control vollständig…")
    rollback_logger.info("Starte hard_stop_e3dc")

    try:
        install_user = get_install_user()
        
        # Setze Stop-Flag
        config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")
        if os.path.exists(config_file):
            replace_in_file(config_file, "stop", "stop = 1")
        
        # Beende Prozesse
        commands = [
            f"sudo -u {install_user} screen -S E3DC -X quit",
            f"sudo -u {install_user} pkill -f E3DC-Control",
            f"sudo -u {install_user} pkill -f E3DC.sh",
            f"sudo -u {install_user} pkill -f {INSTALL_PATH}/E3DC.sh"
        ]
        
        for cmd in commands:
            run_command(cmd, timeout=5)
        
        time.sleep(1.5)
        print("✓ E3DC-Control gestoppt")
        rollback_logger.info("E3DC-Control erfolgreich gestoppt.")
        return True
    except Exception as e:
        print(f"⚠ Fehler beim Stoppen: {e}")
        log_error("rollback", f"Fehler beim Stoppen von E3DC-Control: {e}", e)
        return False


def start_e3dc():
    """Startet E3DC-Control."""
    print("→ Starte E3DC-Control…")
    rollback_logger.info("Starte E3DC-Control")
    
    install_user = get_install_user()
    result = run_command(f"sudo -u {install_user} screen -dmS E3DC {INSTALL_PATH}/E3DC.sh", timeout=5)
    
    if result['success']:
        print("✓ E3DC-Control gestartet")
        return True
    else:
        print("⚠ Warnung: E3DC-Control konnte nicht gestartet werden")
        log_warning("rollback", f"E3DC-Control konnte nicht gestartet werden: {result['stderr']}")
        return False


def rollback(backup_dir):
    """Kompletter Rollback-Prozess vom Backup."""
    print(f"\n→ Rollback von {os.path.basename(backup_dir)}…\n")
    rollback_logger.info(f"Starte Rollback von Backup: {os.path.basename(backup_dir)}")

    if not hard_stop_e3dc():
        print("✗ Konnte E3DC nicht stoppen – Rollback abgebrochen")
        log_error("rollback", "Konnte E3DC nicht stoppen, Rollback abgebrochen.")
        return False

    success = restore_backup(backup_dir)

    if success:
        # Berechtigungen nach Backup-Restore korrigieren
        print("\n→ Korrigiere Berechtigungen nach Wiederherstellung…")
        rollback_logger.info("Korrigiere Berechtigungen nach Wiederherstellung.")
        from .permissions import run_permissions_wizard
        run_permissions_wizard()

    # Service neustarten
    config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")
    if os.path.exists(config_file):
        replace_in_file(config_file, "stop", "stop = 0")
    
    start_e3dc()

    if success:
        print("\n✓ Rollback abgeschlossen.\n")
        log_task_completed("Rollback (Backup)", details=f"Von {os.path.basename(backup_dir)}")
    else:
        print("\n⚠ Rollback mit Problemen abgeschlossen.\n")
        log_error("rollback", f"Rollback von Backup {os.path.basename(backup_dir)} mit Problemen abgeschlossen.")
    
    return success


def get_last_commits(limit=20):
    """Holt letzte Commits vom Remote."""
    result = run_command(f"cd {INSTALL_PATH} && git fetch origin master", timeout=15)
    if not result['success']:
        log_warning("rollback", f"git fetch fehlgeschlagen: {result['stderr']}")
        return None

    result = run_command(
        f"cd {INSTALL_PATH} && git log origin/master --oneline -{limit}",
        timeout=10
    )

    if not result['success'] or not result['stdout'].strip():
        log_warning("rollback", f"git log fehlgeschlagen: {result['stderr']}")
        return None

    commits = []
    for line in result['stdout'].strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        parts = line.split(" ", 1)
        if len(parts) == 2:
            commits.append(tuple(parts))

    return commits if commits else None


def choose_commit():
    """Lässt Benutzer einen Commit wählen."""
    commits = get_last_commits()

    if not commits:
        print("✗ Konnte keine Commits abrufen.")
        rollback_logger.warning("Konnte keine Commits für Commit-Rollback abrufen.")
        return None

    print("\n=== Verfügbare Versionen (letzte 20 Commits) ===\n")

    for i, (commit, msg) in enumerate(commits, start=1):
        print(f"  {i:2d}: {commit} – {msg}")

    print("\n  0: Abbrechen\n")

    choice = input("Welche Version installieren? (Nummer): ").strip()

    if not choice.isdigit():
        print("✗ Ungültige Eingabe.\n")
        return None

    try:
        idx = int(choice)
        if idx == 0:
            return None
        if idx < 1 or idx > len(commits):
            print("✗ Ungültige Auswahl.\n")
            return None
        return commits[idx - 1][0]
    except (ValueError, IndexError):
        print("✗ Fehler bei der Auswahl.\n")
        return None


def rollback_to_commit(commit_hash):
    """Rollback auf bestimmten Commit."""
    print(f"\n→ Rollback auf {commit_hash}…\n")
    rollback_logger.info(f"Starte Rollback zu Commit: {commit_hash}")

    # Git Reset
    result = run_command(f"cd {INSTALL_PATH} && git reset --hard {commit_hash}")
    if not result['success']:
        print(f"✗ Git Reset fehlgeschlagen")
        log_error("rollback", f"Git Reset auf {commit_hash} fehlgeschlagen.", result['stderr'])
        return False

    # Kompilierung
    print("→ Kompiliere…")
    install_user = get_install_user()
    result = run_command(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && make'", timeout=300)
    if not result['success']:
        print(f"✗ Kompilierung fehlgeschlagen")
        log_error("rollback", f"Kompilierung nach Rollback auf {commit_hash} fehlgeschlagen.", result['stderr'])
        return False

    # Berechtigungen korrigieren
    print("\n→ Korrigiere Berechtigungen nach Rollback…")
    rollback_logger.info("Korrigiere Berechtigungen nach Commit-Rollback.")
    from .permissions import run_permissions_wizard
    run_permissions_wizard()

    # Service neustarten
    config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")
    if os.path.exists(config_file):
        replace_in_file(config_file, "stop", "stop = 1")
        time.sleep(5)
        replace_in_file(config_file, "stop", "stop = 0")

    print("✓ Rollback abgeschlossen.\n")
    log_task_completed("Rollback (Commit)", details=f"Zu Commit {commit_hash}")
    return True


def rollback_to_commit_hash():
    """Rollback zu Manual-Eingabe Commit-Hash."""
    print("\n=== Rollback zu Commit-Kennung ===\n")

    commit = input("Commit-Hash eingeben (oder leer zum Abbrechen): ").strip()

    if not commit:
        print("→ Abgebrochen.\n")
        return

    # Validiere Commit
    result = run_command(f"cd {INSTALL_PATH} && git cat-file -t {commit}")

    if not result['success']:
        print("✗ Ungültige oder nicht existierende Commit-Kennung.\n")
        log_warning("rollback", f"Ungültiger Commit-Hash für Rollback eingegeben: {commit}")
        return

    confirm = input(f"Rollback zu {commit}? (j/n): ").strip().lower()
    if confirm != "j":
        print("→ Abgebrochen.\n")
        return

    rollback_to_commit(commit)


def rollback_menu():
    """Menü für Backup-Rollback."""
    backup_dir = choose_backup_version()
    if backup_dir:
        rollback(backup_dir)


def rollback_commit_menu():
    """Menü für Commit-Rollback."""
    commit = choose_commit()
    if commit:
        confirm = input(f"Rollback zu {commit}? (j/n): ").strip().lower()
        if confirm == "j":
            rollback_to_commit(commit)


register_command("17", "Rollback (Backup)", rollback_menu, sort_order=170)
register_command("6", "E3DC-Control Rollback (Commit-Version)", rollback_commit_menu, sort_order=60)
