import os
import subprocess
import time

from .core import register_command
from .backup import backup_current_version
from .utils import replace_in_file, run_command
from .installer_config import get_install_path, get_install_user

INSTALL_PATH = get_install_path()


def get_current_version():
    """Holt die aktuelle Git-Commit-ID."""
    install_user = get_install_user()
    result = run_command(f"sudo -u {install_user} git -C {INSTALL_PATH} rev-parse HEAD", timeout=5)
    return result['stdout'].strip() if result['success'] else None


def get_latest_version():
    """Holt die neueste Commit-ID vom Remote."""
    git_dir = os.path.join(INSTALL_PATH, ".git")
    if not os.path.exists(git_dir):
        print("✗ Keine Git-Installation gefunden.")
        return None

    install_user = get_install_user()
    # Prüfe ob Remote existiert
    result = run_command(f"sudo -u {install_user} git -C {INSTALL_PATH} remote get-url origin", timeout=5)
    if not result['success']:
        print("✗ Kein Git-Remote 'origin' gefunden.")
        return None

    # Hole neueste Version vom Remote
    result = run_command(
        f"sudo -u {install_user} git -C {INSTALL_PATH} ls-remote --heads origin master",
        timeout=10
    )
    if not result['success'] or not result['stdout'].strip():
        print("✗ Branch 'master' nicht gefunden.")
        return None

    return result['stdout'].split()[0]


def count_missing_commits():
    """Zählt fehlende Commits."""
    install_user = get_install_user()
    # Fetch origin
    result = run_command(f"sudo -u {install_user} git -C {INSTALL_PATH} fetch origin master", timeout=15)
    if not result['success']:
        return None

    # Zähle Commits
    result = run_command(
        f"sudo -u {install_user} git -C {INSTALL_PATH} rev-list --count HEAD..origin/master",
        timeout=5
    )
    if result['success']:
        try:
            return int(result['stdout'].strip())
        except ValueError:
            return None
    return None


def list_missing_commits():
    """Listet fehlende Commits auf."""
    install_user = get_install_user()
    result = run_command(
        f"sudo -u {install_user} git -C {INSTALL_PATH} log HEAD..origin/master --oneline",
        timeout=5
    )
    return result['stdout'].strip() if result['success'] else None


def update_e3dc(headless=False):
    """Führt Update durch."""
    print("\n=== E3DC-Control aktualisieren ===\n")

    if not os.path.exists(INSTALL_PATH):
        print("✗ Installation nicht gefunden.")
        return

    old_version = get_current_version()
    if old_version is None:
        print("✗ Aktuelle Version konnte nicht ermittelt werden.")
        return

    latest_version = get_latest_version()
    if latest_version is None:
        print("✗ Update nicht möglich – prüfe Internet und Repository.")
        return

    print(f"Aktuelle Version: {old_version[:7]}")
    print(f"Neueste Version:  {latest_version[:7]}")

    # Speichere aktuellen Stand von origin/master für Reset bei Abbruch
    install_user = get_install_user()
    res = run_command(f"sudo -u {install_user} git -C {INSTALL_PATH} rev-parse origin/master", timeout=5)
    old_origin_sha = res['stdout'].strip() if res['success'] else None

    # Prüfe auf Updates
    missing = count_missing_commits()
    if missing is None:
        print("⚠ Commit-Zählung nicht möglich.")
    elif missing == 0:
        print("✓ Du bist auf dem neuesten Stand.")
        return
    else:
        print(f"→ Es fehlen {missing} Commit(s).\n")
        commits = list_missing_commits()
        if commits:
            print("Fehlende Commits:")
            print(commits)

    # Bestätigung
    if not headless:
        confirm = input("\n→ Möchtest du jetzt aktualisieren? (j/n): ").strip().lower()
        if confirm != "j":
            print("✗ Update abgebrochen. Es wurden keine Änderungen vorgenommen.\n")
            
            # Reset origin/master auf alten Stand, damit "git status" sauber bleibt
            if old_origin_sha:
                run_command(f"sudo -u {install_user} git -C {INSTALL_PATH} update-ref refs/remotes/origin/master {old_origin_sha}")
            return
    else:
        print("→ Starte Update (Headless-Modus)...\n")

    # Backup erstellen
    print("\n→ Erstelle Backup…")
    backup_dir = backup_current_version()
    if backup_dir is None:
        print("✗ Backup fehlgeschlagen. Update abgebrochen.\n")
        return

    # Prüfe auf lokale Änderungen
    print("→ Prüfe auf lokale Änderungen…")
    install_user = get_install_user()
    result1 = subprocess.run(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && git diff --quiet'", shell=True)
    result2 = subprocess.run(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && git diff --cached --quiet'", shell=True)
    has_changes = (result1.returncode != 0 or result2.returncode != 0)

    if has_changes:
        print("⚠ Lokale Änderungen gefunden – sichere automatisch per git stash…")
        result = run_command(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && git stash push -m \"Auto-Stash vor Update\"'")
        if not result['success']:
            print("✗ Stash fehlgeschlagen. Update abgebrochen.\n")
            return
        print("✓ Änderungen gestasht.")
    else:
        print("✓ Keine lokalen Änderungen.")

    # Rechte im .git-Ordner vor Pull korrigieren
    print("→ Korrigiere .git-Berechtigungen vor Update…")
    run_command(f"sudo chown -R {install_user}:{install_user} {INSTALL_PATH}/.git")

    # Update durchführen
    print("→ Hole neue Version…")
    result = run_command(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && git pull'", timeout=60)
    if not result['success']:
        print("✗ Git Pull fehlgeschlagen. Update abgebrochen.\n")
        return

    print("→ Kompiliere neue Version…")
    result = run_command(f"sudo -u {install_user} bash -c 'cd {INSTALL_PATH} && make'", timeout=300)
    if not result['success']:
        print("✗ Kompilierung fehlgeschlagen. Update abgebrochen.\n")
        return

    # Berechtigungen korrigieren
    print("\n→ Korrigiere Berechtigungen nach Update…")
    from .permissions import run_permissions_wizard
    run_permissions_wizard()

    # Neustart mit gestopptem Service
    config_file = os.path.join(INSTALL_PATH, "e3dc.config.txt")
    if os.path.exists(config_file):
        print("→ Neustart mit Konfiguration…")
        replace_in_file(config_file, "stop", "stop = 1")
        time.sleep(5)
        replace_in_file(config_file, "stop", "stop = 0")

    print("✓ Update erfolgreich abgeschlossen.\n")

    # Stash-Management
    result = run_command(f"sudo -u {install_user} git -C {INSTALL_PATH} stash list")
    if result['success'] and "Auto-Stash vor Update" in result['stdout']:
        restore = False
        if headless:
            restore = True # Im Headless-Modus automatisch wiederherstellen
        else:
            restore = input("→ Lokale Änderungen wiederherstellen? (j/n): ").strip().lower() == "j"
        
        if restore:
            print("→ Stelle Änderungen wieder her (git stash pop)…")
            run_command(f"sudo -u {install_user} git -C {INSTALL_PATH} stash pop", timeout=30)
            print("✓ Änderungen wiederhergestellt.\n")


def update_menu():
    update_e3dc()


register_command("5", "E3DC-Control aktualisieren (neueste Version)", update_menu, sort_order=50)
