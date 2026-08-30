#!/usr/bin/env python3
import time
import subprocess
import os
import json
import stat
from datetime import datetime

V4_CONFIG = '/var/www/html/data/e3dc_v4.json'

def read_config():
    # 1. Versuch: Lese blitzschnell aus dem RAM-Disk Cache des Webportals
    cache_file = '/var/www/html/ramdisk/e3dc_config_cache.json'
    try:
        if os.path.exists(cache_file):
            with open(cache_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if 'config' in data:
                    return data['config']
    except: pass

    # 2. Fallback: Direkt aus e3dc_v4.json lesen (Single Source of Truth)
    config = {}
    try:
        with open(V4_CONFIG, 'r', encoding='utf-8') as f:
            for k, v in json.load(f).items():
                config[str(k).lower()] = str(v)
    except: pass
    return config

def get_ha_status():
    """Liest den aktuellen Cluster-Status aus der RAM-Disk."""
    try:
        with open('/var/www/html/ramdisk/ha_status.json', 'r') as f:
            return json.load(f)
    except:
        return {"mode": "off", "state": "unknown"}

def safe_execute(cmd_list, silent=False):
    """Führt einen Befehl sicher aus, falls die Zieldatei existiert."""
    try:
        script_path = cmd_list[1] if len(cmd_list) > 1 else cmd_list[0]
        if os.path.exists(script_path):
            if not silent:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Fuehre aus: {' '.join(cmd_list)}", flush=True)
            
            res = subprocess.run(cmd_list, capture_output=True, text=True, check=False)
            
            if res.stderr.strip() and res.returncode != 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Fehler bei {' '.join(cmd_list)}:\n{res.stderr.strip()}", flush=True)
            elif res.stderr.strip() and not silent:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Hinweis von {' '.join(cmd_list)}:\n{res.stderr.strip()}", flush=True)
            if not silent and res.stdout.strip():
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Ausgabe:\n{res.stdout.strip()}", flush=True)
            if res.returncode != 0:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Befehl fehlgeschlagen (Rückgabecode {res.returncode}): {' '.join(cmd_list)}", flush=True)
                return False
            return True
        else:
            if not silent:
                print(f"[{datetime.now().strftime('%H:%M:%S')}] Ueberspringe (Datei fehlt): {script_path}", flush=True)
            return False
    except Exception as e:
        print(f"Fehler bei der Ausfuehrung von {cmd_list}: {e}", flush=True)
        return False

def normalize_time(t_str):
    """Stellt sicher, dass die Zeit immer HH:MM ist (z.B. 7:00 -> 07:00)"""
    try:
        h, m = map(int, t_str.split(':'))
        return f"{h:02d}:{m:02d}"
    except:
        return t_str


def _consume_docker_restart_flag(path):
    """Konsumiert genau das reguläre, inhaltlich gebundene Neustartflag."""
    before = os.lstat(path)
    if (
        not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size != 2
        or stat.S_IMODE(before.st_mode) != 0o660
    ):
        raise RuntimeError("Neustartflag verletzt Typ-, Link- oder Größenvertrag")

    open_flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is None:
        raise RuntimeError("O_NOFOLLOW ist für den Neustartflag-Vertrag nicht verfügbar")
    descriptor = os.open(path, open_flags | nofollow)
    try:
        opened = os.fstat(descriptor)
        if (
            not stat.S_ISREG(opened.st_mode)
            or opened.st_nlink != 1
            or opened.st_size != 2
            or stat.S_IMODE(opened.st_mode) != 0o660
            or (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino)
        ):
            raise RuntimeError("Neustartflag driftete beim sicheren Öffnen")
        payload = os.read(descriptor, 3)
        after_read = os.fstat(descriptor)
        at_path = os.lstat(path)
        if (
            payload != b"1\n"
            or (after_read.st_dev, after_read.st_ino, after_read.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or (at_path.st_dev, at_path.st_ino, at_path.st_size)
            != (opened.st_dev, opened.st_ino, opened.st_size)
            or at_path.st_nlink != 1
            or not stat.S_ISREG(at_path.st_mode)
            or stat.S_IMODE(at_path.st_mode) != 0o660
        ):
            raise RuntimeError("Neustartflag-Inhalt oder Inode ist nicht stabil gebunden")
    finally:
        os.close(descriptor)

    final = os.lstat(path)
    if (
        (final.st_dev, final.st_ino, final.st_size)
        != (before.st_dev, before.st_ino, before.st_size)
        or final.st_nlink != 1
        or not stat.S_ISREG(final.st_mode)
        or stat.S_IMODE(final.st_mode) != 0o660
    ):
        raise RuntimeError("Neustartflag driftete vor dem Konsum")
    os.unlink(path)

def is_true(val):
    return str(val).lower() in ['1', 'true', 'on']

def main():
    print("Starte E3DC Notification & Schedule Manager...", flush=True)
    last_run = {}
    script_dir = os.path.dirname(os.path.abspath(__file__))

    sqlite_script = os.path.join(script_dir, "sqlite_archiver.py")
    ml_script = os.path.join(script_dir, "ml_predictor.py")
    
    while True:
        cfg = read_config()
        now = datetime.now()
        c_time = now.strftime("%H:%M")
        c_date = now.strftime("%Y-%m-%d")
        c_minute = now.strftime("%M")
        
        # HA-Status ermitteln (Slave im Standby darf keine Stats senden)
        ha_status = get_ha_status()
        is_standby = (ha_status.get('mode') == 'slave' and ha_status.get('state') != 'failover')

        # 1. Boot-Benachrichtigung einmalig beim Start des Dienstes senden
        if last_run.get('boot') != 'done' and not is_standby:
            safe_execute(["/usr/bin/php", "/var/www/html/send_status_telegram.php", "boot"])
            last_run['boot'] = 'done'

        # 2. Täglicher Statusbericht (Uptime, Temp)
        if is_true(cfg.get('telegram_status_enable', '0')) and not is_standby:
            status_time = normalize_time(cfg.get('telegram_status_time', '12:00'))
            run_id = f"{c_date}_{status_time}"
            if c_time == status_time and last_run.get('status') != run_id:
                # safe_execute(["/bin/bash", "/usr/local/bin/boot_notify.sh", "status"])
                if safe_execute(["/usr/bin/php", "/var/www/html/send_status_telegram.php"]):
                    last_run['status'] = run_id

        # 3. Tägliche Statistik (gestern)
        if is_true(cfg.get('telegram_stats_enable', '0')) and not is_standby:
            stats_time = normalize_time(cfg.get('telegram_stats_time', '07:00'))
            run_id = f"{c_date}_{stats_time}"
            if c_time == stats_time and last_run.get('stats') != run_id:
                if safe_execute(["/usr/bin/php", "/var/www/html/send_daily_telegram.php"]):
                    last_run['stats'] = run_id

        # 4. Wöchentliche Statistik (Sonntag = 6)
        if is_true(cfg.get('telegram_weekly_enable', '0')) and not is_standby:
            try: weekly_day = int(cfg.get('telegram_weekly_day', '6'))
            except: weekly_day = 6
            weekly_time = normalize_time(cfg.get('telegram_weekly_time', '20:00'))
            run_id = f"{c_date}_{weekly_time}"
            if c_time == weekly_time and now.weekday() == weekly_day and last_run.get('weekly') != run_id:
                if safe_execute(["/usr/bin/php", "/var/www/html/send_weekly_telegram.php"]):
                    last_run['weekly'] = run_id

        # 5. History Backup (Immer um Mitternacht)
        backup_id = f"{c_date}_00:00"
        if c_time == "00:00" and last_run.get('backup') != backup_id and not is_standby:
            if safe_execute(["/usr/bin/php", "/var/www/html/backup_history.php"]):
                last_run['backup'] = backup_id

        # 6. SQLite Archiver (Immer um Minute 55, ersetzt Cron)
        archiver_id = f"{c_date}_{now.strftime('%H')}"
        if c_minute == "55" and last_run.get('archiver') != archiver_id and not is_standby:
            safe_execute(["/usr/bin/python3", sqlite_script])
            last_run['archiver'] = archiver_id
            
        # 6b. ML Modell Training (Sonntags um 03:00 Uhr)
        ml_train_id = f"{c_date}_train"
        if now.weekday() == 6 and c_time == "03:00" and last_run.get('ml_train') != ml_train_id and not is_standby:
            safe_execute(["/usr/bin/python3", ml_script, "--train"])
            last_run['ml_train'] = ml_train_id
            
        # 6c. Tägliche KI-Prognose (Um 00:05 Uhr)
        ml_pred_id = f"{c_date}_predict"
        if c_time == "00:05" and last_run.get('ml_predict') != ml_pred_id and not is_standby:
            safe_execute(["/usr/bin/python3", ml_script, "--predict"])
            last_run['ml_predict'] = ml_pred_id

        # 7. Live History Writer (Jede Minute, ersetzt Cron)
        history_id = f"{c_date}_{c_time}"
        if last_run.get('history') != history_id and not is_standby:
            safe_execute(
                [
                    "/usr/bin/php",
                    "/var/www/html/get_live_json.php",
                    "--history-sample",
                ],
                silent=True,
            )
            last_run['history'] = history_id

        # 8. Docker-Neustartsignal: Das Entfernen bestätigt genau einen Konsum.
        # Der ungleiche Exit wird von PID 1 über wait -n erkannt und beendet den
        # vollständigen Dienstsatz; restart: unless-stopped startet ihn neu.
        restart_flag = "/var/www/html/ramdisk/restart_container.flag"
        if os.environ.get("E3DC_CONTAINER_MODE") == "1" and os.path.lexists(restart_flag):
            try:
                _consume_docker_restart_flag(restart_flag)
            except Exception as exc:
                print(
                    f"[{datetime.now().strftime('%H:%M:%S')}] Neustartflag konnte nicht sicher konsumiert werden: {exc}",
                    flush=True,
                )
                raise SystemExit(74)
            print(
                f"[{datetime.now().strftime('%H:%M:%S')}] Neustartflag konsumiert. "
                "Notifier beendet sich für den vollständigen Container-Neustart.",
                flush=True,
            )
            raise SystemExit(75)

        time.sleep(10) # 10s Taktung für minütliche Präzision

if __name__ == '__main__':
    main()
