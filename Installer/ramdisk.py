import os
import subprocess
import shutil
import logging
from .core import register_command
from .utils import run_command, setup_logging
from .installer_config import get_install_path, get_install_user, get_home_dir, get_www_data_gid

INSTALL_PATH = get_install_path()
RAMDISK_PATH = "/var/www/html/ramdisk"
GRABBER_SCRIPT = os.path.join(get_home_dir(), "get_live.sh")
FSTAB_PATH = "/etc/fstab"
CRON_COMMENT = "E3DC Live Grabber"

def setup_ramdisk():
    """Richtet die RAM-Disk und den Live-Grabber ein."""
    setup_logging()
    print("\n=== Live-Status & RAM-Disk Setup ===\n")
    logging.info("Starte RAM-Disk Setup")

    install_user = get_install_user()
    
    # 1. RAM-Disk Verzeichnis erstellen
    print("→ Erstelle RAM-Disk Verzeichnis…")
    if not os.path.exists(RAMDISK_PATH):
        run_command(f"sudo mkdir -p {RAMDISK_PATH}")
    
    # 2. fstab Eintrag
    print("→ Konfiguriere /etc/fstab für tmpfs…")
    # UID des install_user dynamisch ermitteln
    import pwd
    try:
        user_uid = pwd.getpwnam(install_user).pw_uid
    except Exception as e:
        print(f"  ✗ Fehler beim Ermitteln der UID für {install_user}: {e}")
        logging.error(f"UID Fehler: {e}")
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
        else:
            print("  ✓ Eintrag überschrieben")
        # Schreibe die neuen Zeilen zurück
        with open(FSTAB_PATH, "w") as f:
            f.writelines(new_lines)
    except Exception as e:
        print(f"  ✗ Fehler beim Bearbeiten von fstab: {e}")
        logging.error(f"FSTAB Fehler: {e}")

    # 3. Mounten
    print("→ Mounte RAM-Disk…")
    run_command("sudo mount -a")
    
    # Beitzrechte für RAM-Disk setzen
    run_command(f"sudo chown {install_user}:www-data {RAMDISK_PATH}")
    run_command(f"sudo chmod 775 {RAMDISK_PATH}")

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
    except Exception as e:
        print(f"  ✗ Fehler beim Erstellen des Skripts: {e}")
        logging.error(f"Skript Fehler: {e}")

    # 5. Autostart via Crontab (für install_user)
    print(f"→ Erstelle Crontab-Eintrag für @reboot…")
    cron_line = f"@reboot /usr/bin/screen -dmS live-grabber {GRABBER_SCRIPT}"
    
    try:
        # Bestehende Crontab laden
        result = run_command(f"sudo -u {install_user} crontab -l")
        existing_cron = result['stdout'] if result['success'] else ""
        
        if GRABBER_SCRIPT not in existing_cron:
            new_cron = existing_cron.strip() + f"\n{cron_line}\n"
            process = subprocess.Popen(["sudo", "-u", install_user, "crontab", "-"], stdin=subprocess.PIPE, text=True)
            process.communicate(input=new_cron)
            print("  ✓ Crontab aktualisiert")
        else:
            print("  ✓ Crontab-Eintrag bereits vorhanden")
    except Exception as e:
        print(f"  ✗ Fehler beim Crontab-Setup: {e}")
        logging.error(f"Crontab Fehler: {e}")

    # 6. Grabber jetzt starten (falls E3DC screen läuft)
    print("→ Starte Live-Grabber jetzt…")
    # Zuerst alten Grabber killen falls er läuft
    run_command(f"sudo -u {install_user} screen -S live-grabber -X quit")
    # Neu starten
    run_command(f"sudo -u {install_user} screen -dmS live-grabber {GRABBER_SCRIPT}")

    print("\n✓ RAM-Disk und Live-Status-Grabber erfolgreich eingerichtet.\n")
    logging.info("RAM-Disk Setup abgeschlossen")

register_command("17", "Live-Status & RAM-Disk Setup", setup_ramdisk, sort_order=170)
