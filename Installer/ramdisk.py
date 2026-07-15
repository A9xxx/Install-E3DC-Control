import os
import shlex
import subprocess
import shutil
import tempfile
import time
# register_command NICHT importieren - Ramdisk wird automatisch via
# permissions.py (run_permissions_wizard) eingerichtet, kein eigener Menüpunkt.
from .utils import run_command
from .installer_config import get_install_path, get_install_user, get_home_dir
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
RAMDISK_PATH = "/var/www/html/ramdisk"
GRABBER_SCRIPT = os.path.join(get_home_dir(get_install_user()), "get_live.sh")
FSTAB_PATH = "/etc/fstab"
CRON_COMMENT = "E3DC Live Grabber"
SERVICE_NAME = "e3dc-grabber"
SERVICE_PATH = f"/etc/systemd/system/{SERVICE_NAME}.service"
ramdisk_logger = get_or_create_logger("ramdisk")


def _validate_fstab_file(path):
    """Validate an fstab file when findmnt is available."""
    if shutil.which("findmnt") is None:
        return True, ""
    result = run_command(f"findmnt --verify --tab-file {shlex.quote(path)}", timeout=20)
    if result.get("success"):
        return True, result.get("stdout", "")
    return False, (result.get("stderr") or result.get("stdout") or "").strip()


def _write_fstab_safely(lines):
    """Write /etc/fstab with backup and validation."""
    backup_path = f"{FSTAB_PATH}.e3dc-backup-{time.strftime('%Y%m%d-%H%M%S')}"
    with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", delete=False) as tmp:
        tmp.writelines(lines)
        tmp_path = tmp.name
    try:
        ok, message = _validate_fstab_file(tmp_path)
        if not ok:
            return False, backup_path, f"Neue fstab ist ungueltig: {message}"
        if os.path.exists(FSTAB_PATH):
            shutil.copy2(FSTAB_PATH, backup_path)
        shutil.copy2(tmp_path, FSTAB_PATH)
        ok, message = _validate_fstab_file(FSTAB_PATH)
        if not ok:
            if os.path.exists(backup_path):
                shutil.copy2(backup_path, FSTAB_PATH)
            return False, backup_path, f"fstab nach Schreiben ungueltig, Backup wiederhergestellt: {message}"
        return True, backup_path, ""
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass


def _restore_fstab(backup_path):
    if backup_path and os.path.exists(backup_path):
        shutil.copy2(backup_path, FSTAB_PATH)
        run_command("sudo systemctl daemon-reload", timeout=15)
        return True
    return False


def setup_ramdisk():
    """Richtet die RAM-Disk ein."""
    print("\n=== RAM-Disk Setup ===\n")
    ramdisk_logger.info("Starte RAM-Disk Setup.")

    # Cleanup: Alten Grabber-Service stoppen und entfernen (falls noch vorhanden)
    run_command(f"sudo systemctl stop {SERVICE_NAME}")
    run_command(f"sudo systemctl disable {SERVICE_NAME}")
    run_command(f"sudo rm -f {SERVICE_PATH}")

    install_user = get_install_user()
    
    # 1. RAM-Disk Verzeichnis erstellen
    print("→ Erstelle RAM-Disk Verzeichnis…")
    if not os.path.exists(RAMDISK_PATH):
        run_command(f"sudo mkdir -p {RAMDISK_PATH}")
        ramdisk_logger.info(f"RAM-Disk Verzeichnis erstellt: {RAMDISK_PATH}")
    
    # 2. fstab Eintrag
    print("→ Konfiguriere /etc/fstab für tmpfs…")
    # UID des install_user dynamisch ermitteln
    import pwd
    try:
        user_uid = pwd.getpwnam(install_user).pw_uid
    except Exception as e:
        print(f"  ✗ Fehler beim Ermitteln der UID für {install_user}: {e}")
        log_error("ramdisk", f"UID für {install_user} konnte nicht ermittelt werden: {e}", e)
        user_uid = 1000  # Fallback
    fstab_entry = f"tmpfs {RAMDISK_PATH} tmpfs nodev,nosuid,size=32M,uid={user_uid},gid=33,mode=2775 0 0"
    
    backup_path = ""
    fstab_changed = False
    try:
        with open(FSTAB_PATH, "r") as f:
            lines = f.readlines()
        new_lines = []
        replaced = False
        for line in lines:
            if line.strip().startswith("tmpfs") and RAMDISK_PATH in line:
                new_lines.append(fstab_entry + "\n")
                replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(fstab_entry + "\n")
            print("  ✓ Eintrag hinzugefügt")
            ramdisk_logger.info("fstab-Eintrag für RAM-Disk hinzugefügt.")
        else:
            print("  ✓ Eintrag überschrieben")
            ramdisk_logger.info("fstab-Eintrag für RAM-Disk aktualisiert.")
        fstab_changed = new_lines != lines
        ok, backup_path, error = _write_fstab_safely(new_lines)
        if not ok:
            raise RuntimeError(error)
        print("  → Reloading systemd manager configuration…")
        run_command("sudo systemctl daemon-reload")
    except Exception as e:
        print(f"  ✗ Fehler beim Bearbeiten von fstab: {e}")
        log_error("ramdisk", f"Fehler beim Bearbeiten von /etc/fstab: {e}", e)
        return False

    # 3. Mounten
    print("→ Mounte RAM-Disk…")
    if not run_command(f"mountpoint -q {shlex.quote(RAMDISK_PATH)}", timeout=5).get("success"):
        mount_result = run_command(f"sudo mount {shlex.quote(RAMDISK_PATH)}", timeout=20)
        if not mount_result.get("success"):
            error = mount_result.get("stderr") or mount_result.get("stdout") or "unbekannter mount-Fehler"
            print(f"  ✗ RAM-Disk konnte nicht gemountet werden: {error.strip()}")
            log_error("ramdisk", f"RAM-Disk Mount fehlgeschlagen: {error}")
            if fstab_changed and _restore_fstab(backup_path):
                print("  ✓ fstab-Backup wiederhergestellt; bitte Fehler vor Reboot pruefen.")
            return False
    
    # Besitzrechte für RAM-Disk setzen
    run_command(f"sudo chown {install_user}:www-data {RAMDISK_PATH}")
    run_command(f"sudo chmod 2775 {RAMDISK_PATH}")
    ramdisk_logger.info("RAM-Disk gemountet und Berechtigungen gesetzt.")

    # 4. Crontab bereinigen (Alten Grabber entfernen, History Writer behalten)
    print(f"→ Aktualisiere Crontab (entferne alten Live-Grabber-Job)…")
    history_cron = "* * * * * cd /var/www/html && /usr/bin/php get_live_json.php > /dev/null 2>&1"
    
    try:
        # Bestehende Crontab laden
        result = run_command(f"sudo -u {install_user} crontab -l")
        existing_cron = result['stdout'] if result['success'] else ""
        
        new_cron = existing_cron
        modified = False
        
        # Entferne alten Grabber-Eintrag falls vorhanden
        if "get_live.sh" in new_cron:
            # Wir bauen die Crontab neu auf, ohne die Grabber-Zeile
            lines = new_cron.splitlines()
            new_lines = [line for line in lines if "get_live.sh" not in line]
            new_cron = "\n".join(new_lines)
            modified = True
            print("  ✓ Alter Live-Grabber Cronjob entfernt")

        if "get_live_json.php" not in existing_cron:
            new_cron = new_cron.strip() + f"\n{history_cron}\n"
            modified = True
            print("  ✓ Live-History Writer hinzugefügt")
            ramdisk_logger.info("Live-History Writer zum Cronjob hinzugefügt.")
        else:
            print("  ✓ Live-History Writer bereits vorhanden")

        if modified:
            # Sicher über Temp-File schreiben
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tmp:
                tmp.write(new_cron + "\n")
                tmp_path = tmp.name
            
            res = run_command(f"sudo crontab -u {install_user} {tmp_path}")
            os.unlink(tmp_path)
            
            if res['success']:
                print("  ✓ Crontab aktualisiert")
                ramdisk_logger.info("Crontab aktualisiert.")
            else:
                print(f"  ✗ Fehler beim Schreiben der Crontab: {res['stderr']}")
    except Exception as e:
        print(f"  ✗ Fehler beim Crontab-Setup: {e}")
        log_error("ramdisk", f"Fehler beim Crontab-Setup: {e}", e)

    # 5. Reste aufräumen
    run_command(f"sudo rm -f {GRABBER_SCRIPT}")

    print("\n✓ RAM-Disk erfolgreich eingerichtet.\n")
    log_task_completed("RAM-Disk Setup")
    return True

# Kein register_command: setup_ramdisk() wird automatisch von
# permissions.py aufgerufen wenn der tmpfs-Mount fehlt (ramdisk_not_mounted).
# Kein manueller Menüpunkt mehr nötig — verhindert ausserdem den Key-Konflikt
# mit rollback.py das ebenfalls Key '14' nutzt.
