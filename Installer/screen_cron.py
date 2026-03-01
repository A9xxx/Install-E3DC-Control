import os
import subprocess
import shlex
import time
import tempfile

from .core import register_command
from .utils import run_command
from .installer_config import get_install_path, get_user_ids, get_install_user
from .logging_manager import get_or_create_logger, log_task_completed, log_error, log_warning

INSTALL_PATH = get_install_path()
screen_logger = get_or_create_logger("screen_cron")


def is_e3dc_running():
    """Prüft ob E3DC läuft (Systemd oder Screen)."""
    # 1. Systemd Check
    res = run_command("systemctl is-active e3dc")
    if res['success'] and res['stdout'].strip() == "active":
        return True

    # 2. Screen Check (Fallback)
    install_user = get_install_user()
    result = run_command(f"sudo -u {install_user} screen -ls", timeout=5)
    
    if not result['success'] and result.get('returncode') != 1:
        return None
    
    output = result['stdout'] if result['success'] else result.get('stderr', '')
    for line in output.split("\n"):
        if "E3DC" in line and ("Attached" in line or "Detached" in line):
            return True
    
    return False


def start_e3dc_control():
    """Startet E3DC-Control (via Systemd bevorzugt)."""
    print("\n=== E3DC-Control starten ===\n")
    screen_logger.info("Versuche E3DC-Control zu starten.")

    # Prüfe ob Service existiert
    if os.path.exists("/etc/systemd/system/e3dc.service"):
        print("→ Starte via Systemd...")
        res = run_command("sudo systemctl start e3dc")
        if res['success']:
            print("✓ Service gestartet.")
            time.sleep(1)
            if is_e3dc_running():
                print("✓ E3DC läuft.")
                log_task_completed("E3DC-Control gestartet (Systemd)")
            else:
                print("⚠ Service gestartet, aber Prozess scheint nicht zu laufen.")
        else:
            print(f"✗ Fehler beim Starten des Services: {res['stderr']}")
            log_error("screen_cron", f"Service Start fehlgeschlagen: {res['stderr']}")
        return
    
    # Fallback: Manuell (Legacy) oder Service installieren
    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")
    install_user = get_install_user()

    if not os.path.exists(sh_path) or not os.path.exists("/etc/systemd/system/e3dc.service"):
        print("⚠ E3DC-Service ist noch nicht eingerichtet.")
        choice = input("Soll der E3DC-Service jetzt installiert werden (empfohlen)? (j/n): ").strip().lower()
        if choice == "j":
            install_e3dc_service()
            start_e3dc_control()
            return

    # Legacy Startversuch (falls User Service ablehnt aber Skript da ist)
    if not os.path.exists(sh_path):
        print(f"✗ Startskript nicht gefunden: {sh_path}")
        return

    if not os.access(sh_path, os.X_OK):
        print(f"⚠ Startskript ist nicht ausführbar: {sh_path}")
        try:
            os.chmod(sh_path, 0o755)
            print("✓ Startskript ist jetzt ausführbar.\n")
        except Exception:
            pass

    running = is_e3dc_running()
    if running:
        print("⚠ E3DC-Control läuft bereits.")
        return

    print("→ Starte E3DC-Control (Screen manuell)…")
    result = run_command(f"sudo -u {install_user} screen -dmS E3DC {shlex.quote(sh_path)}", timeout=5)
    
    if result['success']:
        print("✓ E3DC-Control gestartet (Legacy Mode).\n")
        log_task_completed("E3DC-Control gestartet (Legacy)")
    else:
        print(f"✗ Start fehlgeschlagen: {result['stderr']}\n")
        log_error("screen_cron", f"Start fehlgeschlagen: {result['stderr']}")


def install_e3dc_service():
    """Richtet E3DC als Systemd Service ein und entfernt alte Cronjobs."""
    print("\n=== E3DC-Control Service einrichten (Systemd) ===\n")
    screen_logger.info("Starte Service-Einrichtung.")

    install_user = get_install_user()
    sh_path = os.path.join(INSTALL_PATH, "E3DC.sh")
    service_path = "/etc/systemd/system/e3dc.service"

    # 1. Startskript E3DC.sh erstellen
    print("→ Erstelle Startskript (E3DC.sh)…")
    try:
        with open(sh_path, "w") as f:
            f.write("#!/bin/bash\n")
            f.write(f"cd {INSTALL_PATH}\n")
            f.write("while true; do ./E3DC-Control; sleep 30; done\n")
        
        os.chmod(sh_path, 0o755)
        uid, gid = get_user_ids()
        os.chown(sh_path, uid, gid)
        print("✓ Startskript erstellt/aktualisiert.\n")
    except Exception as e:
        print(f"✗ Fehler beim Erstellen des Startskripts: {e}")
        log_error("screen_cron", f"Fehler E3DC.sh: {e}", e)
        return False

    # 2. Systemd Service erstellen
    print("→ Erstelle Systemd Service…")
    service_content = f"""[Unit]
Description=E3DC-Control Service
After=network.target

[Service]
Type=forking
User={install_user}
Group={install_user}
WorkingDirectory={INSTALL_PATH}
ExecStartPre=-/usr/bin/screen -wipe
ExecStartPre=/bin/sh -c 'echo 0 > stop'
ExecStart=/usr/bin/screen -dmS E3DC ./E3DC.sh
ExecStop=/usr/bin/screen -S E3DC -X quit
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    try:
        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
            tmp.write(service_content)
            tmp_path = tmp.name
        
        run_command(f"sudo mv {tmp_path} {service_path}")
        run_command(f"sudo chmod 644 {service_path}")
        run_command("sudo systemctl daemon-reload")
        run_command("sudo systemctl enable e3dc")
        print("✓ Service 'e3dc' erstellt und aktiviert.\n")
    except Exception as e:
        print(f"✗ Fehler beim Erstellen des Services: {e}")
        log_error("screen_cron", f"Fehler Service: {e}", e)
        return False

    # 3. Alte Cronjobs entfernen
    print("→ Prüfe auf alte Crontab-Einträge…")
    _remove_legacy_cronjobs(install_user)

    # 4. Service starten
    print("→ Starte Service…")
    # Alte Screen-Session beenden falls vorhanden
    run_command(f"sudo -u {install_user} screen -S E3DC -X quit")
    
    res = run_command("sudo systemctl restart e3dc")
    if res['success']:
        print("✓ Service gestartet.")
        log_task_completed("E3DC Service eingerichtet")
    else:
        print(f"✗ Fehler beim Starten: {res['stderr']}")
        log_error("screen_cron", f"Service Start Error: {res['stderr']}")

    return True


def _remove_legacy_cronjobs(user):
    """Entfernt alte Screen-Einträge aus User- und Root-Crontab."""
    # User Crontab
    res = run_command(f"sudo crontab -u {user} -l")
    if res['success']:
        lines = res['stdout'].splitlines()
        new_lines = []
        modified = False
        for line in lines:
            if "screen -dmS E3DC" in line:
                modified = True
                continue
            new_lines.append(line)
        
        if modified:
            content = "\n".join(new_lines) + "\n"
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            run_command(f"sudo crontab -u {user} {tmp_path}")
            os.unlink(tmp_path)
            print("✓ Alte Einträge aus Benutzer-Crontab entfernt.")
    
    # Root Crontab
    res = run_command("sudo crontab -l")
    if res['success']:
        lines = res['stdout'].splitlines()
        new_lines = []
        modified = False
        for line in lines:
            if "screen -dmS E3DC" in line:
                modified = True
                continue
            new_lines.append(line)
        
        if modified:
            content = "\n".join(new_lines) + "\n"
            with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tmp:
                tmp.write(content)
                tmp_path = tmp.name
            run_command(f"sudo crontab {tmp_path}")
            os.unlink(tmp_path)
            print("✓ Alte Einträge aus Root-Crontab entfernt.")


register_command("11", "E3DC-Control Service einrichten (Systemd)", install_e3dc_service, sort_order=110)
register_command("12", "E3DC-Control starten", start_e3dc_control, sort_order=120)
