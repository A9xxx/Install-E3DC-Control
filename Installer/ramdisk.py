import os
import subprocess
import shutil
import tempfile
from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_install_user, get_home_dir
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
RAMDISK_PATH = "/var/www/html/ramdisk"
GRABBER_SCRIPT = os.path.join(get_home_dir(), "get_live.sh")
FSTAB_PATH = "/etc/fstab"
CRON_COMMENT = "E3DC Live Grabber"
ramdisk_logger = get_or_create_logger("ramdisk")

def setup_ramdisk():
    """Richtet die RAM-Disk und den Live-Grabber ein."""
    print("\n=== Live-Status & RAM-Disk Setup ===\n")
    ramdisk_logger.info("Starte RAM-Disk und Live-Grabber Setup.")

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
        # Schreibe die neuen Zeilen zurück
        with open(FSTAB_PATH, "w") as f:
            f.writelines(new_lines)
        print("  → Reloading systemd manager configuration…")
        run_command("sudo systemctl daemon-reload")
    except Exception as e:
        print(f"  ✗ Fehler beim Bearbeiten von fstab: {e}")
        log_error("ramdisk", f"Fehler beim Bearbeiten von /etc/fstab: {e}", e)

    # 3. Mounten
    print("→ Mounte RAM-Disk…")
    run_command("sudo mount -a")
    
    # Besitzrechte für RAM-Disk setzen
    run_command(f"sudo chown {install_user}:www-data {RAMDISK_PATH}")
    run_command(f"sudo chmod 775 {RAMDISK_PATH}")
    ramdisk_logger.info("RAM-Disk gemountet und Berechtigungen gesetzt.")

    # 4. Grabber Skript erstellen
    print(f"→ Erstelle Grabber-Skript: {GRABBER_SCRIPT}")
    script_content = f"""#!/bin/bash
# {CRON_COMMENT}
while true; do
  /usr/bin/screen -S E3DC -X hardcopy {RAMDISK_PATH}/live.txt
  sleep 4
done
"""
    try:
        with open(GRABBER_SCRIPT, "w") as f:
            f.write(script_content)
        
        run_command(f"sudo chown {install_user}:{install_user} {GRABBER_SCRIPT}")
        run_command(f"sudo chmod +x {GRABBER_SCRIPT}")
        print("  ✓ Skript erstellt und ausführbar gemacht")
        ramdisk_logger.info(f"Grabber-Skript erstellt: {GRABBER_SCRIPT}")
    except Exception as e:
        print(f"  ✗ Fehler beim Erstellen des Skripts: {e}")
        log_error("ramdisk", f"Fehler beim Erstellen des Grabber-Skripts: {e}", e)

    # 5. Autostart via Crontab (für install_user)
    print(f"→ Erstelle Crontab-Einträge…")
    
    grabber_cron = f"@reboot /usr/bin/screen -dmS live-grabber {GRABBER_SCRIPT}"
    history_cron = "* * * * * cd /var/www/html && /usr/bin/php get_live_json.php > /dev/null 2>&1"
    
    try:
        # Bestehende Crontab laden
        result = run_command(f"sudo -u {install_user} crontab -l")
        existing_cron = result['stdout'] if result['success'] else ""
        
        new_cron = existing_cron
        modified = False
        
        if GRABBER_SCRIPT not in existing_cron:
            new_cron = new_cron.strip() + f"\n{grabber_cron}\n"
            modified = True
            print("  ✓ Live-Grabber Autostart hinzugefügt")
            ramdisk_logger.info("Live-Grabber Autostart zum Cronjob hinzugefügt.")
        else:
            print("  ✓ Live-Grabber Autostart bereits vorhanden")

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

    # 6. Grabber jetzt starten (falls E3DC screen läuft)
    print("→ Starte Live-Grabber jetzt…")
    # Zuerst alten Grabber killen falls er läuft
    run_command(f"sudo -u {install_user} screen -S live-grabber -X quit")
    # Neu starten
    run_command(f"sudo -u {install_user} screen -dmS live-grabber {GRABBER_SCRIPT}")
    ramdisk_logger.info("Live-Grabber gestartet.")

    print("\n✓ RAM-Disk und Live-Status-Grabber erfolgreich eingerichtet.\n")
    log_task_completed("RAM-Disk & Live-Status Setup")

register_command("14", "Live-Status & RAM-Disk Setup", setup_ramdisk, sort_order=140)
