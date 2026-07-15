import os
import sys
import subprocess
import tempfile
import re
import ipaddress
import shlex
from .installer_config import get_install_user, get_install_path
from .utils import run_command
from .core import register_command
from .install_notifier import install_notifier

NOTIFY_PATH = "/usr/local/bin/boot_notify.sh"
GUARD_PATH = "/usr/local/bin/pi_guard.sh"
SERVICE_PATH = "/etc/systemd/system/piguard.service"

def validate_router_ips(router_ips):
    """Normalisiert und validiert Router-IPs für die spätere Bash-Zuweisung."""
    raw_ips = str(router_ips or "").replace(",", " ").split()
    if not raw_ips:
        raise ValueError("keine Router-IP angegeben")

    normalized = []
    for raw_ip in raw_ips:
        try:
            normalized.append(str(ipaddress.ip_address(raw_ip)))
        except ValueError as exc:
            raise ValueError(f"ungültige Router-IP: {raw_ip}") from exc

    return " ".join(normalized)

def get_current_config():
    """Liest bestehende Konfiguration aus boot_notify.sh aus."""
    config = {
        "TOKEN": "",
        "CHAT_ID": "",
        "DEVICE_NAME": "E3DC-Control",
        "ROUTER_IP": "192.168.178.1",
        "MONITOR_FILE": ""
    }
    if os.path.exists(NOTIFY_PATH):
        try:
            with open(NOTIFY_PATH, "r") as f:
                content = f.read()
                t = re.search(r'TOKEN="([^"]*)"', content)
                c = re.search(r'CHAT_ID="([^"]*)"', content)
                d = re.search(r'DEVICE_NAME="([^"]*)"', content)
                if t: config["TOKEN"] = t.group(1)
                if c: config["CHAT_ID"] = c.group(1)
                if d: config["DEVICE_NAME"] = d.group(1)
        except Exception:
            pass
            
    if os.path.exists(GUARD_PATH):
        try:
            with open(GUARD_PATH, "r") as f:
                content = f.read()
                # Versuche erst das neue Format (Variable)
                ips_match = re.search(r"ROUTER_IPS=(['\"])(.*?)\1", content)
                if ips_match:
                    config["ROUTER_IP"] = ips_match.group(2)
                else:
                    ips_unquoted = re.search(r"ROUTER_IPS=([^\s#]+)", content)
                    if ips_unquoted:
                        config["ROUTER_IP"] = ips_unquoted.group(1)
                    else:
                        # Fallback auf altes Format (direkter Ping)
                        ip = re.search(r'ping -c 1 -W 2 ([0-9.]+)', content)
                        if ip: config["ROUTER_IP"] = ip.group(1)
                mf = re.search(r"MONITOR_FILE=(['\"])(.*?)\1", content)
                if mf: config["MONITOR_FILE"] = mf.group(2)
        except Exception:
            pass
    return config

def create_boot_notify():
    """Erstellt das Boot-Benachrichtigungs-Skript (Liest Werte dynamisch aus Config)."""
    print(f"Erstelle {NOTIFY_PATH}...")
    
    notify_content = r"""#!/bin/bash
V4_CONFIG="/var/www/html/data/e3dc_v4.json"
TOKEN=$(python3 -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('telegram_token',''))" 2>/dev/null)
CHAT_ID=$(python3 -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('telegram_chat_id',''))" 2>/dev/null)
DEVICE_NAME=$(python3 -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('telegram_device_name',''))" 2>/dev/null)
if [ -z "$DEVICE_NAME" ]; then DEVICE_NAME="E3DC-Control"; fi
IP_ADDR=$(hostname -I | cut -d' ' -f1)

if [ -z "$1" ]; then
    REASON=$(journalctl -b -1 -t PIGUARD --no-pager | tail -n 1)
    if [ -z "$REASON" ]; then
        MSG=$(printf "🚀 $DEVICE_NAME gestartet.\\n📍 IP: $IP_ADDR\\nℹ️ Ursache: Manueller Start oder Stromausfall.")
    else
        CLEAN_REASON=$(echo "$REASON" | sed 's/.*PIGUARD: //')
        MSG=$(printf "⚠️ $DEVICE_NAME REBOOT erfolgt!\\n📍 IP: $IP_ADDR\\n❌ Grund: $CLEAN_REASON")
    fi
elif [ "$1" == "status" ]; then
    UPTIME=$(uptime -p)
    if [ -f /sys/class/thermal/thermal_zone0/temp ]; then
        TEMP=$(awk '{printf "%.1f°C", $1/1000}' /sys/class/thermal/thermal_zone0/temp)
    else
        TEMP=$(vcgencmd measure_temp | cut -d'=' -f2)
    fi
    MSG=$(printf "✅ Status: $DEVICE_NAME Online.\\n📍 IP: $IP_ADDR\\n⏱ Laufzeit: $UPTIME\\n🌡 Temp: $TEMP")
else
    MSG=$(printf "ℹ️ $DEVICE_NAME:\\n%s\\n📍 IP: $IP_ADDR" "$1")
fi

if [ -n "$TOKEN" ] && [ -n "$CHAT_ID" ]; then
    curl -s -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \
         --data-urlencode "chat_id=$CHAT_ID" \
         --data-urlencode "text=$MSG" > /dev/null
else
    echo "Nachricht (nicht gesendet, Token fehlt): $MSG"
fi

# Web-Push fuer Watchdog / System-Warnungen / Notstrom
PUSH_ENABLED=$(python3 -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print('1' if d.get('push_notify_warnings') in ['1','true',True] or d.get('push_notify_notstrom') in ['1','true',True] else '')" 2>/dev/null)
if [ -n "$PUSH_ENABLED" ]; then
    PUSH_PY=$(python3 -c "import json; d=json.load(open('/var/www/html/e3dc_paths.json')); print(d.get('install_path','/home/pi/Install').rstrip('/')+'/Installer/send_push.py')" 2>/dev/null)
    VENV_PY=$(python3 -c "import json; d=json.load(open('/var/www/html/e3dc_paths.json')); hp=d.get('home_dir','/home/pi'); print(hp+'/.venv_e3dc/bin/python')" 2>/dev/null)
    if [ -f "$VENV_PY" ] && [ -f "$PUSH_PY" ]; then
        TITLE="System / HA-Slave Info"
        "$VENV_PY" "$PUSH_PY" "$TITLE" "$MSG" > /dev/null 2>&1 &
    fi
fi
"""
    with open("boot_notify.sh", "w", encoding="utf-8") as f:
        f.write(notify_content)
    subprocess.run(["sudo", "mv", "boot_notify.sh", NOTIFY_PATH])
    subprocess.run(["sudo", "chmod", "+x", NOTIFY_PATH])

def create_pi_guard(router_ips, monitor_file=""):
    """Erstellt das Watchdog-Skript."""
    print(f"Erstelle {GUARD_PATH}...")
    
    ips_normalized = validate_router_ips(router_ips)
    router_ips_quoted = shlex.quote(ips_normalized)
    monitor_file_quoted = shlex.quote(str(monitor_file or ""))
    install_user = get_install_user()
    
    guard_content = f"""#!/bin/bash
VERSION="2026.05.15"

log_msg() {{
    echo "$1" | logger -t PIGUARD
    if [ -d "/var/www/html/logs" ]; then
        echo "$(LC_TIME=C date '+%b %d %H:%M:%S') PIGUARD: $1" >> /var/www/html/logs/piguard.log
        tail -n 200 /var/www/html/logs/piguard.log > /var/www/html/logs/piguard.tmp && mv /var/www/html/logs/piguard.tmp /var/www/html/logs/piguard.log
        chmod 664 /var/www/html/logs/piguard.log 2>/dev/null
    fi
}}

write_heartbeat() {{
    local status="${{1:-OK}}"
    if [ -d "/var/www/html/ramdisk" ]; then
        echo "$(date +%s);$status" > /var/www/html/ramdisk/watchdog.heartbeat
        chmod 664 /var/www/html/ramdisk/watchdog.heartbeat 2>/dev/null
    fi
}}

log_msg "Watchdog v$VERSION gestartet. Warte 60s auf System..."
write_heartbeat "STARTING"
sleep 60
fb_fail=0
e3dc_fail=0
file_fail=0
disk_fail=0
mqtt_fail=0
bluelink_fail=0
warned_fb=false
warned_e3dc=false
warned_file=false
warned_disk=false
warned_mqtt=false
warned_bluelink=false
warned_live_stale=false
warned_update_pause=false
warned_update_grace=false
LAST_CHECKED_FILE=""
LAST_LIVE_HASH=""
live_stale_fail=0
sm_restart_attempts=0
sm_last_action_ts=0
warned_sm=false
# Zu überwachende IPs (leerzeichengetrennt)
ROUTER_IPS={router_ips_quoted}
MONITOR_FILE={monitor_file_quoted}
V4_CONFIG="/var/www/html/data/e3dc_v4.json"
INSTALL_DIR=$(python3 -c "import json; d=json.load(open('/var/www/html/e3dc_paths.json')); print(d.get('install_path','/home/pi/Install').rstrip('/'))" 2>/dev/null || echo "/home/pi/Install")
CORE_SERVICE="e3dc-live.service"
CORE_SERVICE_LABEL="${{CORE_SERVICE%.service}}"
WATCHDOG_PAUSE_FILE="/var/www/html/ramdisk/watchdog.update_pause"
WATCHDOG_GRACE_FILE="/var/www/html/ramdisk/watchdog.update_grace"
UPDATE_STATUS_FILES="/var/www/html/ramdisk/e3dc_update_status.json /var/www/html/ramdisk/e3dc_self_update_status.json /var/www/html/ramdisk/web_install_status.json"

restart_core_service() {{
    if [ -z "$CORE_SERVICE" ]; then
        log_msg "WATCHDOG: Kein Core-Service definiert - Restart uebersprungen."
        return 1
    fi
    systemctl restart "$CORE_SERVICE"
}}

watchdog_update_active() {{
    local now mtime age file
    now=$(date +%s)
    if [ -f "$WATCHDOG_PAUSE_FILE" ]; then
        mtime=$(stat -c %Y "$WATCHDOG_PAUSE_FILE" 2>/dev/null || echo 0)
        age=$((now - mtime))
        if [ $age -lt 1800 ]; then
            return 0
        fi
    fi
    for file in $UPDATE_STATUS_FILES; do
        if [ -f "$file" ]; then
            mtime=$(stat -c %Y "$file" 2>/dev/null || echo 0)
            age=$((now - mtime))
            if [ $age -lt 1800 ]; then
                if grep -qiE '"(updating|running)"[[:space:]]*:[[:space:]]*true|"state"[[:space:]]*:[[:space:]]*"(running|pending)"' "$file" 2>/dev/null; then
                    return 0
                fi
            fi
        fi
    done
    if pgrep -f "[i]nstaller_main.py --update-e3dc|[i]nstaller_wrapper.sh update_e3dc|[s]elf_update.py --silent|[I]nstaller/update.py" >/dev/null 2>&1; then
        return 0
    fi
    return 1
}}

watchdog_update_grace_active() {{
    local now mtime age grace_s
    if [ ! -f "$WATCHDOG_GRACE_FILE" ]; then
        return 1
    fi
    now=$(date +%s)
    mtime=$(stat -c %Y "$WATCHDOG_GRACE_FILE" 2>/dev/null || echo 0)
    age=$((now - mtime))
    grace_s=$(python3 -c "import json; d=json.load(open('$WATCHDOG_GRACE_FILE')); print(int(float(d.get('grace_s', 300))))" 2>/dev/null || echo 300)
    if [ "$grace_s" -lt 60 ]; then grace_s=60; fi
    if [ "$grace_s" -gt 900 ]; then grace_s=900; fi
    if [ $age -lt $grace_s ]; then
        return 0
    fi
    rm -f "$WATCHDOG_GRACE_FILE" 2>/dev/null
    return 1
}}

IS_DOCKER=false
if command -v docker &> /dev/null; then
    if docker ps -a --format '{{.Names}}' | grep -q "^e3dc-control$"; then
        IS_DOCKER=true
    fi
fi

if [ -z "$MONITOR_FILE" ]; then
    echo "Info: Keine Datei-Überwachung konfiguriert." | logger -t PIGUARD
fi

while true; do
  status="OK"
  if [ "$warned_file" = true ] || [ "$warned_e3dc" = true ] || [ "$warned_live_stale" = true ] || [ "$warned_sm" = true ]; then status="WARNING"; fi
  write_heartbeat "$status"

  if watchdog_update_active; then
      if [ "$warned_update_pause" = false ]; then
          log_msg "WATCHDOG: Update/Wartung aktiv - Checks pausiert."
          warned_update_pause=true
      fi
      e3dc_fail=0
      file_fail=0
      mqtt_fail=0
      bluelink_fail=0
      live_stale_fail=0
      sm_restart_attempts=0
      sm_last_action_ts=0
      warned_e3dc=false
      warned_file=false
      warned_mqtt=false
      warned_bluelink=false
      warned_live_stale=false
      warned_sm=false
      warned_update_grace=false
      rm -f /var/www/html/ramdisk/watchdog_warning.json 2>/dev/null
      write_heartbeat "UPDATE"
      sleep 10
      continue
  elif watchdog_update_grace_active; then
      if [ "$warned_update_grace" = false ]; then
          log_msg "WATCHDOG: Update-Nachlauf aktiv - Dienste bekommen Zeit fuer frische Live-Daten."
          warned_update_grace=true
      fi
      e3dc_fail=0
      file_fail=0
      mqtt_fail=0
      bluelink_fail=0
      live_stale_fail=0
      sm_restart_attempts=0
      sm_last_action_ts=0
      warned_e3dc=false
      warned_file=false
      warned_mqtt=false
      warned_bluelink=false
      warned_live_stale=false
      warned_sm=false
      rm -f /var/www/html/ramdisk/watchdog_warning.json 2>/dev/null
      write_heartbeat "STARTING"
      sleep 10
      continue
  elif [ "$warned_update_pause" = true ] || [ "$warned_update_grace" = true ]; then
      log_msg "WATCHDOG: Update/Wartung beendet - Checks wieder aktiv."
      warned_update_pause=false
      warned_update_grace=false
  fi

  # --- CHECK 1: NETZWERK (Ping) ---
  network_ok=false
  for ip in $ROUTER_IPS; do
    if ping -c 1 -W 2 $ip > /dev/null; then
      network_ok=true
      break
    fi
  done

  if [ "$network_ok" = false ]; then
    ((fb_fail++))
    if [ $fb_fail -eq 5 ] && [ "$warned_fb" = false ]; then
        /usr/local/bin/boot_notify.sh "⚠️ Netzwerk weg ($ROUTER_IPS)! Reboot in 4 Min."
        warned_fb=true
    fi
  else
    fb_fail=0
    warned_fb=false
  fi

  # --- HA-CLUSTER PRÜFUNG ---
  # Prüfe ob wir ein Standby-Slave sind (dann laufen E3DC Dienste nicht)
  is_standby_slave=false
  ha_mode=$(python3 -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('ha_mode','off').lower())" 2>/dev/null || echo "off")
  if [ "$ha_mode" = "slave" ]; then
      # Wenn der e3dc-live Service nicht laeuft, sind wir im korrekten Standby
      if ! systemctl is-active --quiet e3dc-live 2>/dev/null && ! systemctl is-active --quiet e3dc 2>/dev/null; then
          is_standby_slave=true
      fi
  fi

  if [ "$is_standby_slave" = false ]; then
      # --- CHECK 2: E3DC SCREEN ODER DOCKER CONTAINER ---
      if [ "$IS_DOCKER" = true ]; then
          if ! docker ps --format '{{{{.Names}}}}' | grep -q "^e3dc-control$"; then
            ((e3dc_fail++))
            if [ $e3dc_fail -eq 5 ] && [ "$warned_e3dc" = false ]; then
                /usr/local/bin/boot_notify.sh "⚠️ Docker Container 'e3dc-control' fehlt! Restart in 2 Min."
                warned_e3dc=true
            fi
          else
            e3dc_fail=0
            warned_e3dc=false
          fi
      else
          if ! systemctl is-active --quiet e3dc-live 2>/dev/null; then
            ((e3dc_fail++))
            if [ $e3dc_fail -eq 5 ] && [ "$warned_e3dc" = false ]; then
                /usr/local/bin/boot_notify.sh "⚠️ e3dc-live Service down! Restart in 2 Min."
                warned_e3dc=true
            fi
          else
            e3dc_fail=0
            warned_e3dc=false
          fi
      fi
    
      # --- CHECK 3: DATEI-AKTIVITÄT (Hänger-Schutz) ---
      # Dynamische Dateinamen (z.B. protokoll.{{day}}.txt)
      ACTUAL_FILE="$MONITOR_FILE"
      if [[ "$MONITOR_FILE" == *"{{day}}"* ]]; then
          # Strategie: Wir suchen die neueste Datei, die auf das Muster passt.
          # Das löst Probleme beim Tageswechsel (z.B. wenn E3DC noch in die gestrige Datei schreibt).
          PATTERN=$(echo "$MONITOR_FILE" | sed 's/{{day}}/*/g')
          LATEST=$(ls -1t $PATTERN 2>/dev/null | head -n 1)
          if [ -n "$LATEST" ]; then
              ACTUAL_FILE="$LATEST"
          else
              dow=$(date +%u)
              case $dow in
                  1) d="Mo" ;; 2) d="Di" ;; 3) d="Mi" ;; 4) d="Do" ;; 5) d="Fr" ;; 6) d="Sa" ;; 7) d="So" ;;
              esac
              ACTUAL_FILE=$(echo "$MONITOR_FILE" | sed "s/{{day}}/$d/")
          fi
      fi
    
      # Logge Datei-Wechsel (z.B. neuer Tag oder Start)
      if [ "$ACTUAL_FILE" != "$LAST_CHECKED_FILE" ] && [ -n "$ACTUAL_FILE" ]; then
          log_msg "Überwache Datei: $ACTUAL_FILE"
          LAST_CHECKED_FILE="$ACTUAL_FILE"
      fi
    
      if [ -n "$ACTUAL_FILE" ] && [ -f "$ACTUAL_FILE" ]; then
        current_time=$(date +%s)
        file_time=$(stat -c %Y "$ACTUAL_FILE")
        diff=$((current_time - file_time))
        
        # Alarm wenn Datei älter als 15 Minuten (900 Sekunden)
        if [ $diff -gt 900 ]; then
          ((file_fail++))
          if [ $file_fail -eq 5 ] && [ "$warned_file" = false ]; then
              /usr/local/bin/boot_notify.sh "⚠️ Datei veraltet! ($ACTUAL_FILE > 15min). Restart Service in 2 Min."
              warned_file=true
          fi
        else
          file_fail=0
          warned_file=false
        fi
      fi

      # --- CHECK 5 & 6: Zusatzdienste (nur wenn nicht im Docker-Modus) ---
      if [ "$IS_DOCKER" = false ]; then
          if [ -f "/etc/systemd/system/e3dc-mqtt-hub.service" ]; then
              if systemctl is-enabled --quiet e3dc-mqtt-hub; then
                  if ! systemctl is-active --quiet e3dc-mqtt-hub; then
                      ((mqtt_fail++))
                      if [ $mqtt_fail -eq 3 ] && [ "$warned_mqtt" = false ]; then
                          /usr/local/bin/boot_notify.sh "⚠️ MQTT-Hub Service inaktiv! Restart in Kürze."
                          warned_mqtt=true
                      fi
                  else
                      mqtt_fail=0
                      warned_mqtt=false
                  fi
              fi
          fi

          if [ -f "/etc/systemd/system/e3dc-bluelink.service" ]; then
              if systemctl is-enabled --quiet e3dc-bluelink; then
                  if ! systemctl is-active --quiet e3dc-bluelink; then
                      ((bluelink_fail++))
                      if [ $bluelink_fail -eq 3 ] && [ "$warned_bluelink" = false ]; then
                          /usr/local/bin/boot_notify.sh "⚠️ Bluelink Service inaktiv! Restart in Kürze."
                          warned_bluelink=true
                      fi
                  else
                      bluelink_fail=0
                      warned_bluelink=false
                  fi
              fi
          fi
      fi
  else
      # Im Standby-Modus Fehlerzähler zurücksetzen
      e3dc_fail=0
      warned_e3dc=false
      file_fail=0
      warned_file=false
      mqtt_fail=0
      warned_mqtt=false
      bluelink_fail=0
      warned_bluelink=false
      live_stale_fail=0
      warned_live_stale=false
  fi

  # --- CHECK 7: E3DC LIVE-STAGNATION (Zukunftssicher & Freeze-Schutz) ---
  if [ "$is_standby_slave" = false ]; then
      live_json=$(curl -s --max-time 3 http://localhost/get_live_json.php 2>/dev/null)
      if [ -n "$live_json" ]; then
          home_pw=$(echo "$live_json" | grep -o '\"home_raw\":[ ]*[-0-9]*' | cut -d':' -f2 | tr -d ' ')
          pv_pw=$(echo "$live_json" | grep -o '\"pv\":[ ]*[-0-9]*' | cut -d':' -f2 | tr -d ' ')
          grid_pw=$(echo "$live_json" | grep -o '\"grid\":[ ]*[-0-9]*' | cut -d':' -f2 | tr -d ' ')
          if [ -n "$home_pw" ] && [ -n "$pv_pw" ] && [ -n "$grid_pw" ]; then
              current_hash="${{home_pw}}_${{pv_pw}}_${{grid_pw}}"
              if [ "$LAST_LIVE_HASH" == "$current_hash" ]; then
                  ((live_stale_fail++))
                  if [ $live_stale_fail -eq 12 ] && [ "$warned_live_stale" = false ]; then
                      /usr/local/bin/boot_notify.sh "⚠️ E3DC Werte eingefroren (>2 Min identische Wattzahlen). Restart Service in 1 Min."
                      warned_live_stale=true
                  fi
              else
                  live_stale_fail=0
                  warned_live_stale=false
                  LAST_LIVE_HASH="$current_hash"
              fi
          fi
      fi
  fi

  # --- CHECK 8: STORAGE MANAGER HANG / CRASH ---
  if [ "$is_standby_slave" = false ]; then
      if [ -f "/var/www/html/ramdisk/storage_manager_state.json" ]; then
          sm_time=$(stat -c %Y "/var/www/html/ramdisk/storage_manager_state.json")
          sm_current_time=$(date +%s)
          sm_diff=$((sm_current_time - sm_time))
          sm_service_active=false
          if systemctl is-active --quiet e3dc-storage-manager.service 2>/dev/null; then
              sm_service_active=true
          fi

          sm_service_down_too_long=false
          if [ "$sm_service_active" = false ] && [ $sm_diff -gt 45 ]; then
              sm_service_down_too_long=true
          fi

          if [ $sm_diff -gt 300 ] || [ "$sm_service_down_too_long" = true ]; then
              sm_action_age=$((sm_current_time - sm_last_action_ts))
              if [ $sm_action_age -ge 90 ]; then
                  if [ $sm_restart_attempts -lt 2 ]; then
                      ((sm_restart_attempts++))
                      sm_last_action_ts=$sm_current_time
                      log_msg "WATCHDOG: Storage Manager reagiert nicht (State ${{sm_diff}}s alt, Dienst aktiv=${{sm_service_active}}). Restart ${{sm_restart_attempts}}/2."
                      printf '{{"warning": "Storage Manager reagiert nicht - Neustartversuch %s/2", "level": "warning", "ts": %s}}\n' "$sm_restart_attempts" "$sm_current_time" > /var/www/html/ramdisk/watchdog_warning.json
                      chmod 664 /var/www/html/ramdisk/watchdog_warning.json 2>/dev/null
                      systemctl restart e3dc-storage-manager.service 2>/dev/null
                      if [ "$warned_sm" = false ]; then
                          /usr/local/bin/boot_notify.sh "Watchdog: E3DC Storage Manager reagiert nicht - Neustartversuch ${{sm_restart_attempts}}/2."
                          warned_sm=true
                      fi
                  else
                      sm_last_action_ts=$sm_current_time
                      log_msg "WATCHDOG: Storage Manager nach 2 Neustarts weiter gestoert. Loese Failsafe aus!"
                      systemctl stop e3dc-storage-manager.service 2>/dev/null
                      printf '{{"warning": "Kerndienst (Storage Manager) ausgefallen - Failsafe aktiv!", "level": "critical", "ts": %s}}\n' "$sm_current_time" > /var/www/html/ramdisk/watchdog_warning.json
                      chmod 664 /var/www/html/ramdisk/watchdog_warning.json 2>/dev/null
                      if [ -f "$INSTALL_DIR/Installer/emergency_release.py" ]; then
                          sudo -u pi python3 "$INSTALL_DIR/Installer/emergency_release.py"
                      fi
                      /usr/local/bin/boot_notify.sh "Watchdog: E3DC Storage Manager nach 2 Neustarts weiter gestoert - Failsafe aktiv!"
                  fi
              fi
          else
              # StorMgr Datei ist frisch (< 5 Min) UND Dienst laeuft -> Warnung aufloesen
              sm_restart_attempts=0
              sm_last_action_ts=0
              warned_sm=false
              if [ -f "/var/www/html/ramdisk/watchdog_warning.json" ]; then
                  if systemctl is-active --quiet e3dc-storage-manager.service 2>/dev/null; then
                      rm -f /var/www/html/ramdisk/watchdog_warning.json
                      log_msg "WATCHDOG: StorMgr erholt (Datei frisch, Dienst aktiv) - Warnung geloescht."
                  fi
              fi
          fi
      fi
  fi

  # --- CHECK 4: SPEICHERPLATZ (SD-Karte) ---
  # Prüft Root-Partition (/) auf Füllstand > 90%
  disk_usage=$(df / | awk 'NR==2 {{print $5}}' | tr -d '%')
  
  if [ "$disk_usage" -gt 90 ]; then
    ((disk_fail++))
    if [ $disk_fail -eq 5 ] && [ "$warned_disk" = false ]; then
        /usr/local/bin/boot_notify.sh "⚠️ Speicherplatz kritisch! SD-Karte zu $disk_usage% voll."
        warned_disk=true
    fi
  else
    disk_fail=0
    warned_disk=false
  fi
  
  if [ $fb_fail -ge 30 ]; then
    log_msg "Netzwerk ($ROUTER_IPS) seit 5 Min weg. Reboot!"
    systemctl reboot
  fi
  if [ $e3dc_fail -ge 18 ]; then
    if [ "$IS_DOCKER" = true ]; then
        log_msg "Docker Container fehlt seit 3 Min. Restart!"
        docker restart e3dc-control
        /usr/local/bin/boot_notify.sh "⚠️ E3DC Docker Container neu gestartet."
    else
        log_msg "$CORE_SERVICE_LABEL Watchdog: Restart Core Service!"
        restart_core_service
        /usr/local/bin/boot_notify.sh "⚠️ $CORE_SERVICE_LABEL Service neu gestartet (Watchdog)."
    fi
    e3dc_fail=0
    warned_e3dc=false
    sleep 60
  fi
  if [ $file_fail -ge 18 ]; then
    if [ "$IS_DOCKER" = true ]; then
        log_msg "Watchdog-Datei veraltet. Restart Docker Container!"
        docker restart e3dc-control
        /usr/local/bin/boot_notify.sh "⚠️ E3DC Docker Container neu gestartet (Datei $ACTUAL_FILE veraltet)."
    else
        log_msg "Watchdog-Datei $ACTUAL_FILE seit >18 Min nicht aktualisiert. Restart $CORE_SERVICE_LABEL Service!"
        restart_core_service
        /usr/local/bin/boot_notify.sh "⚠️ $CORE_SERVICE_LABEL Service neu gestartet (Datei $ACTUAL_FILE veraltet)."
    fi
    file_fail=0
    warned_file=false
    sleep 60
  fi
  if [ $mqtt_fail -ge 6 ]; then
    log_msg "MQTT-Hub Service down. Restart!"
    systemctl restart e3dc-mqtt-hub
    /usr/local/bin/boot_notify.sh "⚠️ MQTT-Hub Service durch Watchdog neu gestartet."
    mqtt_fail=0
    warned_mqtt=false
  fi
  if [ $bluelink_fail -ge 6 ]; then
    log_msg "Bluelink Service down. Restart!"
    systemctl restart e3dc-bluelink
    /usr/local/bin/boot_notify.sh "⚠️ Bluelink Service durch Watchdog neu gestartet."
    bluelink_fail=0
    warned_bluelink=false
  fi
  if [ $live_stale_fail -ge 18 ]; then
    if [ "$IS_DOCKER" = true ]; then
        log_msg "E3DC Werte (PV/Haus) 3 Min lang exakt gleich. Container Freeze! Restart!"
        docker restart e3dc-control
        /usr/local/bin/boot_notify.sh "⚠️ E3DC Docker Container neu gestartet (Werte eingefroren)."
    else
        log_msg "E3DC Werte (PV/Haus) 3 Min lang exakt gleich. Freeze! Restart $CORE_SERVICE_LABEL!"
        restart_core_service
        /usr/local/bin/boot_notify.sh "⚠️ $CORE_SERVICE_LABEL Service neu gestartet (Werte eingefroren)."
    fi
    live_stale_fail=0
    warned_live_stale=false
    LAST_LIVE_HASH=""
    sleep 60
  fi
  sleep 10
done
"""
    with open("pi_guard.sh", "w", encoding="utf-8") as f:
        f.write(guard_content)
    subprocess.run(["sudo", "mv", "pi_guard.sh", GUARD_PATH])
    subprocess.run(["sudo", "chmod", "+x", GUARD_PATH])

def create_service():
    """Erstellt den Systemd Service."""
    print(f"Erstelle Systemd Service {SERVICE_PATH}...")
    service_content = f"""[Unit]
Description=E3DC and FritzBox Guard Service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
ExecStart={GUARD_PATH}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    with open("piguard.service", "w", encoding="utf-8") as f:
        f.write(service_content)
    subprocess.run(["sudo", "mv", "piguard.service", SERVICE_PATH])
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    subprocess.run(["sudo", "systemctl", "enable", "piguard.service"])

def configure_hardware_watchdog():
    """Konfiguriert den Hardware Watchdog."""
    print("Konfiguriere Hardware Watchdog in /etc/systemd/system.conf...")
    subprocess.run(["sudo", "sed", "-i", "s/^#RuntimeWatchdogSec=.*/RuntimeWatchdogSec=60/", "/etc/systemd/system.conf"])
    subprocess.run(["sudo", "sed", "-i", "s/^RuntimeWatchdogSec=.*/RuntimeWatchdogSec=60/", "/etc/systemd/system.conf"])

def setup_watchdog_menu():
    # Prüfen auf Root-Rechte
    if os.geteuid() != 0:
        print("❌ Fehler: Dieses Skript muss mit 'sudo' ausgeführt werden!")
        return

    print("\n=== PV-Wächter & Telegram Setup ===")
    
    # Bestehende Config laden
    current = get_current_config()
    is_installed = os.path.exists(NOTIFY_PATH)
    
    if is_installed:
        print(f"✓ Watchdog ist bereits installiert.")
        print(f"  Aktueller Name: {current['DEVICE_NAME']}")
        print(f"  Router-IP: {current['ROUTER_IP']}")
        print(f"  Telegram: {'Aktiv' if current['TOKEN'] else 'Inaktiv'}")
        print("")
        print("1. Komplett neu installieren / reparieren")
        print("2. Router-IP ändern")
        print("3. Abbrechen")
        
        choice = input("Auswahl: ").strip()
        if choice == "3": return
    else:
        choice = "1"

    # --- LOGIK ---
    
    if choice == "1": # Installieren
        print("ℹ️  Telegram-Token, Chat-ID und Namen werden nun komfortabel im Web-Interface gepflegt!")
        # Router IP
        router_ip = input(f"Router-IP(s) für Watchdog (getrennt durch Leerzeichen) [{current['ROUTER_IP']}]: ").strip() or current['ROUTER_IP']
        try:
            router_ip = validate_router_ips(router_ip)
        except ValueError as e:
            print(f"Ungültige Router-IP-Konfiguration: {e}")
            return
        
        # Monitor File (Hänger-Schutz)
        monitor_file = current['MONITOR_FILE']
        install_path = get_install_path()
        # Veraltete E3DC-Control Dateiprüfung entfernt (Hänger-Schutz läuft jetzt primär über get_live_json.php Stagnation)
        monitor_file = ""

        # Ausführen
        create_boot_notify()
        create_pi_guard(router_ip, monitor_file)
        create_service()
        configure_hardware_watchdog()
        
        print("\nRichte zentralen Benachrichtigungs-Dienst ein...")
        try:
            install_notifier()
        except Exception as e:
            print(f"Fehler bei der Notifier-Installation: {e}")
        
        print("Setze Berechtigungen für Log-Zugriff...")
        subprocess.run(["sudo", "usermod", "-aG", "systemd-journal", "www-data"])
        # Webserver neu starten, damit Gruppenrechte sofort wirksam werden
        print("Starte Webserver neu (Rechte-Update)...")
        subprocess.run("sudo systemctl restart apache2 2>/dev/null || sudo systemctl restart lighttpd 2>/dev/null", shell=True)
        
        print("Starte PIGUARD Service neu...")
        subprocess.run(["sudo", "systemctl", "restart", "piguard.service"])
        
        # Cleanup: Entferne fehlerhafte Dateien aus alten Versionen (=5.0)
        user_home = f"/home/{get_install_user()}"
        bad_files = [os.path.join(user_home, "=5.0"), os.path.join(user_home, "5.0")]
        for bf in bad_files:
            if os.path.exists(bf):
                try:
                    os.remove(bf)
                    print(f"✓ Alte Fehler-Datei entfernt: {bf}")
                except: pass

        print("\n--- INSTALLATION ABGESCHLOSSEN ---")
        print("Bitte starte den Pi einmal neu, um alle Änderungen zu aktivieren.")

    elif choice == "2": # Router IP
        new_ip = input(f"Neue Router-IP(s) [{current['ROUTER_IP']}]: ").strip()
        if new_ip:
            try:
                new_ip = validate_router_ips(new_ip)
            except ValueError as e:
                print(f"Ungültige Router-IP-Konfiguration: {e}")
                return
            create_pi_guard(new_ip, current['MONITOR_FILE'])
            subprocess.run(["sudo", "systemctl", "restart", "piguard.service"])
            print("✓ Router-IP aktualisiert und Service neugestartet.")

def install_watchdog_silent():
    """Installiert den Watchdog automatisch mit sicheren Defaults (ohne Telegram)."""
    print("\n=== Watchdog-Installation (Automatisch) ===")
    
    # 1. Router IP ermitteln (Gateway)
    router_ip = "192.168.178.1" # Fallback
    try:
        # Versuche Gateway zu ermitteln
        res = subprocess.run("ip route | grep default | awk '{print $3}'", shell=True, capture_output=True, text=True)
        detected = res.stdout.strip()
        if detected:
            try:
                router_ip = validate_router_ips(detected)
            except ValueError:
                print(f"Warnung: erkannte Gateway-IP unplausibel ({detected!r}), nutze Fallback {router_ip}.")
    except:
        pass
        
    # Veraltete E3DC-Control Dateiprüfung entfernt
    monitor_file = ""

    print(f"Konfiguration (Silent):")
    print(f"  Telegram:      Deaktiviert")
    print(f"  Router-IP:     {router_ip}")
    print(f"  Monitor-Datei: {monitor_file}")

    # Installation durchführen
    create_boot_notify()
    create_pi_guard(router_ip, monitor_file)
    create_service()
    configure_hardware_watchdog()
    
    try:
        install_notifier()
    except Exception:
        pass

    # Rechte setzen
    subprocess.run(["sudo", "usermod", "-aG", "systemd-journal", "www-data"])
    subprocess.run("sudo systemctl restart apache2 2>/dev/null || sudo systemctl restart lighttpd 2>/dev/null", shell=True)
    subprocess.run(["sudo", "systemctl", "restart", "piguard.service"])
    
    print("✓ Watchdog erfolgreich installiert.")

if __name__ == "__main__":
    setup_watchdog_menu()

register_command("15", "Watchdog & Telegram konfigurieren", setup_watchdog_menu, sort_order=150)
