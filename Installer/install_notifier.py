import os
import subprocess
import tempfile
import re
from .core import register_command
from .installer_config import get_install_user, get_install_path
from .utils import run_command
from .logging_manager import log_task_completed
from .config_secret_permissions import apply_config_secret_permissions

def cleanup_old_crons(user):
    """Entfernt die alten Notification/Backup Cronjobs (User, Root, www-data & /etc/crontab)."""
    users_to_check = [user, "root", "www-data"]
    for u in users_to_check:
        res = run_command(f"sudo crontab -u {u} -l")
        if res['success']:
            lines = res['stdout'].splitlines()
            new_lines = []
            modified = False
            for line in lines:
                if "boot_notify.sh" in line or "send_daily_telegram.php" in line or "send_weekly_telegram.php" in line or "backup_history.php" in line or "get_live_json.php" in line or "sqlite_archiver.py" in line or "diagram_helpers.py" in line:
                    modified = True
                    continue
                new_lines.append(line)
            
            if modified:
                    if not new_lines:
                        run_command(f"sudo crontab -u {u} -r")
                    else:
                        with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
                            tmp.write("\n".join(new_lines) + "\n")
                            tmp_path = tmp.name
                        run_command(f"sudo crontab -u {u} {tmp_path}")
                        os.unlink(tmp_path)
                    print(f"  ✓ Alte Cronjobs für '{u}' bereinigt.")

    # Zusätzlich /etc/crontab prüfen
    res = run_command("sudo cat /etc/crontab")
    if res['success']:
        lines = res['stdout'].splitlines()
        new_lines = []
        modified = False
        for line in lines:
            if "boot_notify.sh" in line or "send_daily_telegram.php" in line or "send_weekly_telegram.php" in line or "backup_history.php" in line:
                modified = True
                continue
            new_lines.append(line)
        
        if modified:
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
                tmp.write("\n".join(new_lines) + "\n")
                tmp_path = tmp.name
            run_command(f"sudo cp {tmp_path} /etc/crontab")
            run_command("sudo chmod 644 /etc/crontab")
            os.unlink(tmp_path)
            print("  ✓ Alte Cronjobs aus /etc/crontab bereinigt.")

def migrate_telegram_tokens():
    """Extrahiert alte Telegram-Tokens aus boot_notify.sh und speichert sie in e3dc_v4.json."""
    import json
    notify_path = "/usr/local/bin/boot_notify.sh"
    v4_path = "/var/www/html/data/e3dc_v4.json"
    if not os.path.exists(notify_path) or not os.path.exists(v4_path):
        return

    token, chat_id, dev_name = "", "", ""
    try:
        with open(notify_path, "r") as f:
            content = f.read()
            t = re.search(r'TOKEN="([^"]+)"', content)
            c = re.search(r'CHAT_ID="([^"]+)"', content)
            d = re.search(r'DEVICE_NAME="([^"]+)"', content)
            if t: token = t.group(1)
            if c: chat_id = c.group(1)
            if d: dev_name = d.group(1)
    except: pass

    if token and chat_id:
        try:
            with open(v4_path, "r", encoding="utf-8-sig") as f:
                v4_data = json.load(f)

            if not v4_data.get("telegram_token"):
                v4_data["telegram_token"] = token
                v4_data["telegram_chat_id"] = chat_id
                if dev_name:
                    v4_data["telegram_device_name"] = dev_name
                tmp = v4_path + ".tmp"
                with open(tmp, "w", encoding="utf-8") as f:
                    json.dump(v4_data, f, indent=4)
                os.replace(tmp, v4_path)
                apply_config_secret_permissions(v4_path, install_user=get_install_user(), data=v4_data)
                print("[OK] Telegram-Zugangsdaten erfolgreich in e3dc_v4.json migriert.")
        except Exception as e:
            print(f"[!] Fehler bei der Token-Migration: {e}")



def install_notifier(start_service=True, migrate_legacy_config=True):
    print("\n=== Benachrichtigungs-Dienst einrichten ===")
    user = get_install_user()
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notification_manager.py")

    if migrate_legacy_config:
        migrate_telegram_tokens()
    else:
        print("→ Betriebskonfiguration bleibt im Release-Fenster unverändert.")
    run_command(f"sudo chmod +x {script_path}")
    cleanup_old_crons(user)
    
    from .utils import _create_service_file
    
    _create_service_file(
        service_name="e3dc-notifier",
        description="E3DC Notification Manager",
        python_script_rel_path="notification_manager.py",
        restart_sec=10,
        start_service=start_service,
    )
    
    if start_service:
        print("✓ Benachrichtigungs-Dienst (Cron-Ersatz) installiert und gestartet.")
    else:
        print("✓ Benachrichtigungs-Dienst (Cron-Ersatz) installiert; Start wird gesammelt ausgeführt.")
    log_task_completed("Notifier Setup")

register_command("44", "Benachrichtigungs-Dienst einrichten", install_notifier, sort_order=44)
