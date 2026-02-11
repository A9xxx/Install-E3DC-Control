import os
import subprocess
import time
import shutil

from .core import register_command
from .backup import choose_backup_version, restore_backup
from .utils import replace_in_file, run_command

INSTALL_PATH = "/home/pi/E3DC-Control"


def hard_stop_e3dc():
    """Stoppt E3DC-Control vollständig."""
    print("→ Stoppe E3DC-Control vollständig…")

    try:
        # Setze Stop-Flag
        config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")
        if os.path.exists(config_file):
            replace_in_file(config_file, "stop", "stop = 1")
        
        # Beende Prozesse
        commands = [
            "screen -S E3DC -X quit",
            "pkill -f E3DC-Control",
            "pkill -f E3DC.sh",
            f"pkill -f {INSTALL_PATH}/E3DC.sh"
        ]
        
        for cmd in commands:
            run_command(cmd, timeout=5)
        
        time.sleep(1.5)
        print("✓ E3DC-Control gestoppt")
        return True
    except Exception as e:
        print(f"⚠ Fehler beim Stoppen: {e}")
        return False


def start_e3dc():
    """Startet E3DC-Control."""
    print("→ Starte E3DC-Control…")
    
    result = run_command(f"screen -dmS E3DC {INSTALL_PATH}/E3DC.sh", timeout=5)
    
    if result['success']:
        print("✓ E3DC-Control gestartet")
        return True
    else:
        print("⚠ Warnung: E3DC-Control konnte nicht gestartet werden")
        return False


def rollback(backup_dir):
    """Kompletter Rollback-Prozess vom Backup."""
    print(f"\n→ Rollback von {os.path.basename(backup_dir)}…\n")

    if not hard_stop_e3dc():
        print("✗ Konnte E3DC nicht stoppen – Rollback abgebrochen")
        return False

    success = restore_backup(backup_dir)

    # Service neustarten
    config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")
    if os.path.exists(config_file):
        replace_in_file(config_file, "stop", "stop = 0")
    
    start_e3dc()

    if success:
        print("\n✓ Rollback abgeschlossen.\n")
    else:
        print("\n⚠ Rollback mit Problemen abgeschlossen.\n")
    
    return success


def get_last_commits(limit=20):
    """Holt letzte Commits vom Remote."""
    result = run_command(f"cd {INSTALL_PATH} && git fetch origin master", timeout=15)
    if not result['success']:
        return None

    result = run_command(
        f"cd {INSTALL_PATH} && git log origin/master --oneline -{limit}",
        timeout=10
    )

    if not result['success'] or not result['stdout'].strip():
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

    # Git Reset
    result = run_command(f"cd {INSTALL_PATH} && git reset --hard {commit_hash}")
    if not result['success']:
        print(f"✗ Git Reset fehlgeschlagen")
        return False

    # Kompilierung
    print("→ Kompiliere…")
    result = run_command(f"cd {INSTALL_PATH} && make", timeout=300)
    if not result['success']:
        print(f"✗ Kompilierung fehlgeschlagen")
        return False

    # Service neustarten
    config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")
    if os.path.exists(config_file):
        replace_in_file(config_file, "stop", "stop = 1")
        time.sleep(3)
        replace_in_file(config_file, "stop", "stop = 0")

    print("✓ Rollback abgeschlossen.\n")
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


register_command("12", "Rollback (Backup)", rollback_menu, sort_order=120)
register_command("13", "Rollback (Commit-Auswahl)", rollback_commit_menu, sort_order=130)
