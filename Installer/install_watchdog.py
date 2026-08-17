from __future__ import annotations

import fcntl
import hashlib
import ipaddress
import json
import os
import pwd
import re
import secrets
import shlex
import stat
import subprocess
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Mapping, Sequence

from .core import register_command
from .install_notifier import begin_watchdog_child, notifier_install_transaction
from .transition_context import TransitionContext, TransitionContextError, get_transition_context
from .transition_systemd import (
    CommandResult,
    SystemdTransitionManager,
    TransitionRecoveryRequired,
    TransitionRolledBack,
)

NOTIFY_PATH = "/usr/local/bin/boot_notify.sh"
GUARD_PATH = "/usr/local/bin/pi_guard.sh"
SERVICE_PATH = "/etc/systemd/system/piguard.service"
MANIFEST_PATH = "/usr/local/lib/e3dc-control-watchdog.sha256"
WATCHDOG_JOURNAL_ROOT = "/var/lib/e3dc-control/watchdog-transitions"
SYSTEMD_JOURNAL_ROOT = "/var/lib/e3dc-control/systemd-transitions"
SYSTEM_CONF_PATH = "/etc/systemd/system.conf"
WATCHDOG_SERVICE = "piguard.service"
WATCHDOG_BUNDLE_SCHEMA = 1


class WatchdogTransitionError(RuntimeError):
    """Basisfehler der atomaren Watchdog-Installation."""


class WatchdogTransitionRolledBack(WatchdogTransitionError):
    """Ein fehlgeschlagener Watchdog-Übergang hat den exakten Vorzustand wiederhergestellt."""

    def __init__(self, transaction_id: str):
        super().__init__(f"Watchdog-Transaktion {transaction_id} wurde zurückgerollt")
        self.transaction_id = transaction_id


class WatchdogRecoveryRequired(WatchdogTransitionError):
    """Der Vorzustand konnte nicht belegt werden; der Schreiber bleibt gestoppt."""

    def __init__(self, transaction_id: str):
        super().__init__(f"Watchdog-Transaktion {transaction_id} benötigt Wiederherstellung")
        self.transaction_id = transaction_id


@dataclass(frozen=True)
class WatchdogPaths:
    notify: str = NOTIFY_PATH
    guard: str = GUARD_PATH
    service: str = SERVICE_PATH
    manifest: str = MANIFEST_PATH
    journal_root: str = WATCHDOG_JOURNAL_ROOT
    systemd_journal_root: str = SYSTEMD_JOURNAL_ROOT
    system_conf: str = SYSTEM_CONF_PATH


@dataclass(frozen=True)
class WatchdogBundle:
    bundle_sha256: str
    interpreter_sha256: str
    notify: bytes
    guard: bytes
    service: str
    manifest: bytes


@dataclass(frozen=True)
class _FileSnapshot:
    target: str
    existed: bool
    sha256: str | None
    mode: int | None
    uid: int | None
    gid: int | None
    mtime_ns: int | None
    backup_file: str | None

    def as_dict(self) -> dict[str, object]:
        return {
            "target": self.target,
            "existed": self.existed,
            "sha256": self.sha256,
            "mode": self.mode,
            "uid": self.uid,
            "gid": self.gid,
            "mtime_ns": self.mtime_ns,
            "backup_file": self.backup_file,
        }

    @classmethod
    def from_dict(cls, value: Mapping[str, object]) -> "_FileSnapshot":
        return cls(
            target=str(value["target"]),
            existed=bool(value["existed"]),
            sha256=str(value["sha256"]) if value.get("sha256") is not None else None,
            mode=int(value["mode"]) if value.get("mode") is not None else None,
            uid=int(value["uid"]) if value.get("uid") is not None else None,
            gid=int(value["gid"]) if value.get("gid") is not None else None,
            mtime_ns=int(value["mtime_ns"]) if value.get("mtime_ns") is not None else None,
            backup_file=(
                str(value["backup_file"]) if value.get("backup_file") is not None else None
            ),
        )


CommandRunner = Callable[[Sequence[str], int], CommandResult | subprocess.CompletedProcess[str]]
PhaseHook = Callable[[str, Mapping[str, object]], None]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _default_command_runner(argv: Sequence[str], timeout: int) -> CommandResult:
    env = os.environ.copy()
    env["PATH"] = "/usr/sbin:/usr/bin:/sbin:/bin"
    env["LC_ALL"] = "C"
    result = subprocess.run(
        list(argv),
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
        env=env,
    )
    return CommandResult(result.returncode, result.stdout, result.stderr)


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


def detect_default_gateway():
    """Ermittelt das lokale Standard-Gateway ohne einen Netzbereich zu erfinden."""
    try:
        result = subprocess.run(
            ["ip", "route", "show", "default"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""

    for line in result.stdout.splitlines():
        fields = line.split()
        if "via" not in fields:
            continue
        gateway_index = fields.index("via") + 1
        if gateway_index >= len(fields):
            continue
        try:
            return validate_router_ips(fields[gateway_index])
        except ValueError:
            continue
    return ""

def get_current_config():
    """Liest bestehende Konfiguration aus boot_notify.sh aus."""
    config = {
        "TOKEN": "",
        "CHAT_ID": "",
        "DEVICE_NAME": "E3DC-Control",
        "ROUTER_IP": detect_default_gateway(),
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

def _render_boot_notify(context: TransitionContext, bundle_sha256: str) -> str:
    """Erzeugt das Benachrichtigungsskript, ohne die aktive Installation zu verändern."""
    push_script = os.path.join(context.install_path, "Installer", "send_push.py")
    push_script_value = shlex.quote(push_script if os.path.isfile(push_script) else "")
    push_python_value = shlex.quote(context.venv_python)
    bundle_value = shlex.quote(bundle_sha256)

    notify_content = r"""#!/bin/bash
BUNDLE_SHA256=__BUNDLE_SHA256__
PYTHON_BIN=__PUSH_PYTHON__
V4_CONFIG="/var/www/html/data/e3dc_v4.json"
TOKEN=$("$PYTHON_BIN" -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('telegram_token',''))" 2>/dev/null)
CHAT_ID=$("$PYTHON_BIN" -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('telegram_chat_id',''))" 2>/dev/null)
DEVICE_NAME=$("$PYTHON_BIN" -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('telegram_device_name',''))" 2>/dev/null)
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
PUSH_ENABLED=$("$PYTHON_BIN" -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print('1' if d.get('push_notify_warnings') in ['1','true',True] or d.get('push_notify_notstrom') in ['1','true',True] else '')" 2>/dev/null)
if [ -n "$PUSH_ENABLED" ]; then
    PUSH_PY=__PUSH_SCRIPT__
    VENV_PY=__PUSH_PYTHON__
    if [ -x "$VENV_PY" ] && [ -f "$PUSH_PY" ]; then
        TITLE="System / HA-Slave Info"
        "$VENV_PY" "$PUSH_PY" "$TITLE" "$MSG" > /dev/null 2>&1 &
    fi
fi
"""
    notify_content = notify_content.replace("__BUNDLE_SHA256__", bundle_value)
    notify_content = notify_content.replace("__PUSH_SCRIPT__", push_script_value)
    notify_content = notify_content.replace("__PUSH_PYTHON__", push_python_value)
    return notify_content

def _render_pi_guard(
    context: TransitionContext,
    router_ips: str,
    monitor_file: str = "",
    bundle_sha256: str = "",
) -> str:
    """Erzeugt das Guard-Skript, ohne die aktive Installation zu verändern."""
    ips_normalized = validate_router_ips(router_ips)
    router_ips_quoted = shlex.quote(ips_normalized)
    monitor_file_quoted = shlex.quote(str(monitor_file or ""))
    install_dir_quoted = shlex.quote(context.install_path)
    venv_python_quoted = shlex.quote(context.venv_python)
    bundle_value = shlex.quote(bundle_sha256)

    guard_content = f"""#!/bin/bash
VERSION="2026.05.15"
BUNDLE_SHA256={bundle_value}

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
sm_quiesce_attempted=false
sm_incident_id=""
warned_sm=false
warned_quiesce_latch=false
# Zu überwachende IPs (leerzeichengetrennt)
ROUTER_IPS={router_ips_quoted}
MONITOR_FILE={monitor_file_quoted}
V4_CONFIG="/var/www/html/data/e3dc_v4.json"
INSTALL_DIR={install_dir_quoted}
VENV_PY={venv_python_quoted}
CORE_SERVICE="e3dc-live.service"
CORE_SERVICE_LABEL="${{CORE_SERVICE%.service}}"
WATCHDOG_PAUSE_FILE="/var/www/html/ramdisk/watchdog.update_pause"
WATCHDOG_GRACE_FILE="/var/www/html/ramdisk/watchdog.update_grace"
UPDATE_STATUS_FILES="/var/www/html/ramdisk/e3dc_update_status.json /var/www/html/ramdisk/e3dc_self_update_status.json /var/www/html/ramdisk/web_install_status.json"
EMERGENCY_STATE_DIR="/var/lib/e3dc-control/emergency-quiesce"
EMERGENCY_LATCH="$EMERGENCY_STATE_DIR/active-incident.json"

restart_core_service() {{
    if [ -z "$CORE_SERVICE" ]; then
        log_msg "WATCHDOG: Kein Core-Service definiert - Restart uebersprungen."
        return 1
    fi
    systemctl restart "$CORE_SERVICE"
}}

emergency_latch_active() {{
    [ -f "$EMERGENCY_LATCH" ]
}}

new_storage_incident_id() {{
    local random_part
    if [ -r /proc/sys/kernel/random/uuid ]; then
        random_part=$(tr -cd 'A-Za-z0-9-' < /proc/sys/kernel/random/uuid)
    else
        random_part="$$-$(date +%s%N)"
    fi
    printf 'storage-%s-%s' "$(date +%s)" "$random_part"
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
    if pgrep -f "[i]nstaller_main.py (--update-e3dc|--reinstall-current)|[i]nstaller_wrapper.sh (update_e3dc|reinstall_current)|[s]elf_update.py --silent|[I]nstaller/update.py|[r]elease_finalize.py" >/dev/null 2>&1; then
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
    grace_s=$("$VENV_PY" -c "import json; d=json.load(open('$WATCHDOG_GRACE_FILE')); print(int(float(d.get('grace_s', 300))))" 2>/dev/null || echo 300)
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
      if ! emergency_latch_active; then
          sm_quiesce_attempted=false
          sm_incident_id=""
          warned_quiesce_latch=false
      fi
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
      if ! emergency_latch_active; then
          sm_quiesce_attempted=false
          sm_incident_id=""
          warned_quiesce_latch=false
      fi
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
  ha_mode=$("$VENV_PY" -c "import json; d=json.load(open('/var/www/html/data/e3dc_v4.json')); print(d.get('ha_mode','off').lower())" 2>/dev/null || echo "off")
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
      current_hash=$("$VENV_PY" "$INSTALL_DIR/Installer/live_snapshot.py" --watchdog-hash 2>/dev/null)
      if [ -n "$current_hash" ]; then
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

          if emergency_latch_active; then
              if [ "$warned_quiesce_latch" = false ]; then
                  log_msg "WATCHDOG: Emergency-Quiesce-Latch aktiv; kein Restart und kein erneuter Aktorpfad. Manuelle Prüfung erforderlich."
                  printf '{{"warning": "Emergency-Quiesce aktiv - manueller Reset nach Prüfung erforderlich", "level": "critical", "ts": %s}}\n' "$sm_current_time" > /var/www/html/ramdisk/watchdog_warning.json
                  chmod 664 /var/www/html/ramdisk/watchdog_warning.json 2>/dev/null
                  /usr/local/bin/boot_notify.sh "Watchdog: Emergency-Quiesce aktiv - kein automatischer Wiederholungsversuch."
                  warned_quiesce_latch=true
              fi
              warned_sm=true
          elif [ $sm_diff -gt 300 ] || [ "$sm_service_down_too_long" = true ]; then
              sm_action_age=$((sm_current_time - sm_last_action_ts))
              if [ $sm_action_age -ge 90 ]; then
                  if [ "$sm_quiesce_attempted" = true ]; then
                      : # One-shot: nach dem ersten Quiesce-Versuch nur noch Alarmstatus halten.
                  elif [ $sm_restart_attempts -lt 2 ]; then
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
                      sm_quiesce_attempted=true
                      if [ -z "$sm_incident_id" ]; then
                          sm_incident_id=$(new_storage_incident_id)
                      fi
                      log_msg "WATCHDOG: Storage Manager nach 2 Neustarts weiter gestoert. Starte einmaligen Emergency-Quiesce."
                      printf '{{"warning": "Kerndienst ausgefallen - einmaliger Emergency-Quiesce gestartet", "level": "critical", "ts": %s}}\n' "$sm_current_time" > /var/www/html/ramdisk/watchdog_warning.json
                      chmod 664 /var/www/html/ramdisk/watchdog_warning.json 2>/dev/null
                      if [ -x "$VENV_PY" ] && [ -f "$INSTALL_DIR/Installer/emergency_release.py" ]; then
                          "$VENV_PY" "$INSTALL_DIR/Installer/emergency_release.py" \
                              --incident-id "$sm_incident_id" \
                              --state-dir "$EMERGENCY_STATE_DIR"
                          quiesce_rc=$?
                      else
                          quiesce_rc=127
                      fi
                      if [ $quiesce_rc -eq 0 ]; then
                          log_msg "WATCHDOG: Emergency-Quiesce und Owner-Lease bestätigt; keine Hardware-Fallbacks ausgeführt."
                          /usr/local/bin/boot_notify.sh "Watchdog: Storage-Writer geordnet beendet; Emergency-Quiesce bestätigt."
                      else
                          log_msg "WATCHDOG: Emergency-Quiesce unvollstaendig (Exit $quiesce_rc); kein Retry und kein Hardware-Fallback."
                          printf '{{"warning": "Emergency-Quiesce unvollstaendig - manueller Eingriff erforderlich", "level": "critical", "ts": %s, "exit_code": %s}}\n' "$sm_current_time" "$quiesce_rc" > /var/www/html/ramdisk/watchdog_warning.json
                          chmod 664 /var/www/html/ramdisk/watchdog_warning.json 2>/dev/null
                          /usr/local/bin/boot_notify.sh "Watchdog: Emergency-Quiesce unvollstaendig - manueller Eingriff erforderlich."
                      fi
                  fi
              fi
          else
              # StorMgr Datei ist frisch (< 5 Min) UND Dienst laeuft -> Warnung aufloesen
              sm_restart_attempts=0
              sm_last_action_ts=0
              sm_quiesce_attempted=false
              sm_incident_id=""
              warned_sm=false
              warned_quiesce_latch=false
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
    return guard_content

def _render_service(paths: WatchdogPaths, bundle_sha256: str) -> str:
    for value in (paths.guard, paths.manifest):
        if not os.path.isabs(value) or any(char.isspace() for char in value):
            raise WatchdogTransitionError("Watchdog-Pfade müssen absolut und leerzeichenfrei sein")
    return f"""[Unit]
Description=E3DC and FritzBox Guard Service
Wants=network-online.target
After=network-online.target

[Service]
Type=simple
Environment=E3DC_WATCHDOG_BUNDLE_SHA={bundle_sha256}
ExecStartPre=/usr/bin/sha256sum --check --strict {paths.manifest}
ExecStart={paths.guard}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""


class WatchdogBundleInstaller:
    """Installiert Skripte und PiGuard-Unit als eine wiederherstellbare Transaktion.

    Es wird keine Rechteausweitung versucht. Der Aufrufer muss bereits alle Ziele
    schreiben und systemd steuern dürfen. ``system.conf`` liegt bewusst außerhalb
    dieses Release-Übergangs und wird hier weder gelesen noch geschrieben.
    """

    def __init__(
        self,
        *,
        paths: WatchdogPaths | None = None,
        runner: CommandRunner | None = None,
        systemd_manager: SystemdTransitionManager | None = None,
        target_uid: int = 0,
        target_gid: int = 0,
        phase_hook: PhaseHook | None = None,
        command_timeout: int = 30,
    ) -> None:
        self.paths = paths or WatchdogPaths()
        self.runner = runner or _default_command_runner
        self.target_uid = int(target_uid)
        self.target_gid = int(target_gid)
        self.phase_hook = phase_hook
        self.command_timeout = int(command_timeout)
        self.journal_root = Path(os.path.abspath(self.paths.journal_root))
        self.lock_path = self.journal_root / ".watchdog-transition.lock"
        self.recovery_status_path = self.journal_root / "recovery-required.json"
        self.systemd = systemd_manager or SystemdTransitionManager(
            unit_root=str(Path(self.paths.service).parent),
            journal_root=self.paths.systemd_journal_root,
            runner=self.runner,
            target_uid=self.target_uid,
            target_gid=self.target_gid,
            command_timeout=self.command_timeout,
        )

    @staticmethod
    def _assert_no_symlink_components(path: Path) -> None:
        absolute = Path(os.path.abspath(path))
        current = Path(absolute.anchor)
        for part in absolute.parts[1:]:
            current /= part
            if os.path.lexists(current) and stat.S_ISLNK(os.lstat(current).st_mode):
                raise WatchdogTransitionError("Autoritätspfad enthält einen Symlink")

    @staticmethod
    def _directory_flags() -> int:
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        directory = getattr(os, "O_DIRECTORY", 0)
        if not nofollow or not directory:
            raise WatchdogTransitionError(
                "Watchdog-Journal benötigt O_NOFOLLOW und O_DIRECTORY"
            )
        return os.O_RDONLY | nofollow | directory | getattr(os, "O_CLOEXEC", 0)

    def _ensure_private_root(self) -> None:
        if os.geteuid() != 0:
            raise WatchdogTransitionError("Watchdog-Journal benötigt Root-Rechte")
        flags = self._directory_flags()
        descriptor = os.open("/", flags)
        current = Path("/")
        try:
            components = self.journal_root.parts[1:]
            for index, component in enumerate(components):
                final = index == len(components) - 1
                created = False
                try:
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    os.mkdir(component, 0o700 if final else 0o755, dir_fd=descriptor)
                    os.fsync(descriptor)
                    next_descriptor = os.open(component, flags, dir_fd=descriptor)
                    created = True
                metadata = os.fstat(next_descriptor)
                current /= component
                if (
                    not stat.S_ISDIR(metadata.st_mode)
                    or metadata.st_uid != 0
                    or stat.S_IMODE(metadata.st_mode) & 0o022
                ):
                    os.close(next_descriptor)
                    raise WatchdogTransitionError(
                        f"Watchdog-Journal-Elternpfad ist nicht root-kontrolliert: {current}"
                    )
                if final:
                    os.fchown(next_descriptor, 0, 0)
                    os.fchmod(next_descriptor, 0o700)
                    os.fsync(next_descriptor)
                    rebound = os.fstat(next_descriptor)
                    if (
                        rebound.st_uid != 0
                        or rebound.st_gid != 0
                        or stat.S_IMODE(rebound.st_mode) != 0o700
                    ):
                        os.close(next_descriptor)
                        raise WatchdogTransitionError(
                            "Watchdog-Journalroot ist nicht root:root 0700"
                        )
                elif created:
                    os.fchown(next_descriptor, 0, 0)
                    os.fchmod(next_descriptor, 0o755)
                    os.fsync(next_descriptor)
                if created:
                    os.fsync(descriptor)
                os.close(descriptor)
                descriptor = next_descriptor
        finally:
            os.close(descriptor)
        info = os.lstat(self.journal_root)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise WatchdogTransitionError("Watchdog-Journal ist nicht root:root 0700")

    @contextmanager
    def _locked(self):
        self._ensure_private_root()
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise WatchdogTransitionError("Watchdog-Lock benötigt O_NOFOLLOW")
        flags = os.O_RDWR | os.O_CREAT | nofollow | getattr(os, "O_CLOEXEC", 0)
        lock_existed = os.path.lexists(self.lock_path)
        fd = os.open(self.lock_path, flags, 0o600)
        try:
            initial = os.fstat(fd)
            if (
                not stat.S_ISREG(initial.st_mode)
                or initial.st_nlink != 1
            ):
                raise WatchdogTransitionError("Watchdog-Prozesslock ist nicht privat")
            if not lock_existed:
                os.fchown(fd, 0, 0)
                os.fchmod(fd, 0o600)
                os.fsync(fd)
                self._fsync_dir(self.journal_root)
            info = os.fstat(fd)
            if (
                info.st_nlink != 1
                or info.st_uid != 0
                or info.st_gid != 0
                or stat.S_IMODE(info.st_mode) != 0o600
            ):
                raise WatchdogTransitionError("Watchdog-Prozesslock ist nicht root-privat")
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise WatchdogTransitionError("Eine Watchdog-Transaktion läuft bereits") from exc
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _normalize_result(
        result: CommandResult | subprocess.CompletedProcess[str],
    ) -> CommandResult:
        return CommandResult(int(result.returncode), str(result.stdout or ""), str(result.stderr or ""))

    def _run(self, argv: Sequence[str], *, check: bool = True) -> CommandResult:
        if not argv or any("\x00" in str(item) for item in argv):
            raise WatchdogTransitionError("Ungültiger Watchdog-Befehl")
        if not os.path.isabs(str(argv[0])):
            raise WatchdogTransitionError("Watchdog-Befehle müssen absolut sein")
        try:
            result = self._normalize_result(self.runner(tuple(map(str, argv)), self.command_timeout))
        except (OSError, subprocess.SubprocessError) as exc:
            raise WatchdogTransitionError("Watchdog-Befehl konnte nicht ausgeführt werden") from exc
        if check and result.returncode != 0:
            raise WatchdogTransitionError(
                f"{os.path.basename(str(argv[0]))} scheiterte mit Exitcode {result.returncode}"
            )
        return result

    def _phase(self, phase: str, record: Mapping[str, object]) -> None:
        if self.phase_hook is not None:
            self.phase_hook(phase, record)

    @staticmethod
    def _read_regular(path: Path, *, single_link: bool = True) -> tuple[bytes, os.stat_result]:
        if path.is_symlink():
            raise WatchdogTransitionError("Watchdog-Datei ist ein Symlink")
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise WatchdogTransitionError("Watchdog-Dateizugriff benötigt O_NOFOLLOW")
        flags = os.O_RDONLY | nofollow | getattr(os, "O_CLOEXEC", 0)
        fd = os.open(path, flags)
        try:
            before = os.fstat(fd)
            if not stat.S_ISREG(before.st_mode) or (single_link and before.st_nlink != 1):
                raise WatchdogTransitionError("Watchdog-Datei ist nicht eindeutig regulär")
            chunks: list[bytes] = []
            while True:
                chunk = os.read(fd, 1024 * 1024)
                if not chunk:
                    break
                chunks.append(chunk)
            after = os.fstat(fd)
            named_after = os.lstat(path)
            identity_before = (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
            identity_after = (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
            identity_named = (
                named_after.st_dev,
                named_after.st_ino,
                named_after.st_size,
                named_after.st_mtime_ns,
            )
            if identity_before != identity_after or identity_after != identity_named:
                raise WatchdogTransitionError("Watchdog-Datei wurde während des Lesens verändert")
            return b"".join(chunks), after
        finally:
            os.close(fd)

    def _hash_interpreter(self, context: TransitionContext) -> str:
        raw = Path(context.venv_python)
        if not context.trusted or not context.venv_python or not raw.is_absolute():
            raise TransitionContextError("Watchdog benötigt einen exakten vertrauenswürdigen Interpreter")
        try:
            target = raw.resolve(strict=True)
            info = target.stat()
        except OSError as exc:
            raise TransitionContextError("Watchdog-Interpreter ist nicht auflösbar") from exc
        if not stat.S_ISREG(info.st_mode) or not os.access(raw, os.X_OK):
            raise TransitionContextError("Watchdog-Interpreter ist nicht ausführbar")
        payload, final_info = self._read_regular(target, single_link=False)
        if (info.st_dev, info.st_ino, info.st_mtime_ns) != (
            final_info.st_dev,
            final_info.st_ino,
            final_info.st_mtime_ns,
        ):
            raise TransitionContextError("Watchdog-Interpreter wurde ausgetauscht")
        return _sha256(payload)

    @staticmethod
    def _require_product_file(context: TransitionContext, relative: str) -> Path:
        root = Path(context.install_path).resolve(strict=True)
        candidate = root / relative
        if candidate.is_symlink() or not candidate.is_file():
            raise WatchdogTransitionError(f"Erforderliche Produktdatei fehlt: {relative}")
        resolved = candidate.resolve(strict=True)
        if root not in resolved.parents:
            raise WatchdogTransitionError("Produktdatei liegt außerhalb des Installationsroots")
        info = candidate.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
            raise WatchdogTransitionError("Produktdatei ist nicht eindeutig regulär")
        return candidate

    def render_bundle(
        self,
        context: TransitionContext,
        router_ips: str,
        monitor_file: str = "",
    ) -> WatchdogBundle:
        interpreter_sha = self._hash_interpreter(context)
        self._require_product_file(context, "Installer/emergency_release.py")
        self._require_product_file(context, "Installer/send_push.py")
        for value in (
            self.paths.notify,
            self.paths.guard,
            self.paths.service,
            self.paths.manifest,
            context.venv_python,
        ):
            if (
                not os.path.isabs(value)
                or any(char in value for char in ("\x00", "\r", "\n", "\t", "\\"))
            ):
                raise WatchdogTransitionError("Manifestpfade enthalten unzulässige Zeichen")

        placeholder = "0" * 64
        notify_template = _render_boot_notify(context, placeholder).encode("utf-8")
        guard_template = _render_pi_guard(
            context, router_ips, monitor_file, placeholder
        ).encode("utf-8")
        service_template = _render_service(self.paths, placeholder).encode("utf-8")
        seed = {
            "schema": WATCHDOG_BUNDLE_SCHEMA,
            "interpreter_path": context.venv_python,
            "interpreter_sha256": interpreter_sha,
            "install_path": context.install_path,
            "notify_template_sha256": _sha256(notify_template),
            "guard_template_sha256": _sha256(guard_template),
            "service_template_sha256": _sha256(service_template),
            "router_ips": validate_router_ips(router_ips),
            "monitor_file": str(monitor_file or ""),
        }
        bundle_sha = _sha256(
            (json.dumps(seed, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        )
        notify = _render_boot_notify(context, bundle_sha).encode("utf-8")
        guard = _render_pi_guard(context, router_ips, monitor_file, bundle_sha).encode("utf-8")
        service = _render_service(self.paths, bundle_sha)
        service_bytes = service.encode("utf-8")
        entries = (
            (_sha256(notify), self.paths.notify),
            (_sha256(guard), self.paths.guard),
            (interpreter_sha, context.venv_python),
            (_sha256(service_bytes), self.paths.service),
        )
        manifest = "".join(f"{digest}  {path}\n" for digest, path in entries).encode("utf-8")
        return WatchdogBundle(bundle_sha, interpreter_sha, notify, guard, service, manifest)

    def _validate_target(self, target: Path) -> None:
        if not target.is_absolute():
            raise WatchdogTransitionError("Watchdog-Ziel ist nicht absolut")
        self._assert_no_symlink_components(target.parent)
        parent_info = os.lstat(target.parent)
        if (
            stat.S_ISLNK(parent_info.st_mode)
            or not stat.S_ISDIR(parent_info.st_mode)
            or parent_info.st_uid != os.geteuid()
            or stat.S_IMODE(parent_info.st_mode) & 0o022
        ):
            raise WatchdogTransitionError(
                "Watchdog-Zielverzeichnis ist nicht exklusiv root-kontrolliert"
            )
        if target.exists() or target.is_symlink():
            info = os.lstat(target)
            if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise WatchdogTransitionError("Watchdog-Ziel ist nicht eindeutig regulär")

    @staticmethod
    def _fsync_dir(path: Path) -> None:
        fd = os.open(path, WatchdogBundleInstaller._directory_flags())
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    def _atomic_write(
        self,
        target: Path,
        payload: bytes,
        *,
        mode: int,
        uid: int,
        gid: int,
        mtime_ns: int | None = None,
    ) -> None:
        self._validate_target(target)
        temp = target.parent / f".{target.name}.e3dc-{secrets.token_hex(8)}"
        nofollow = getattr(os, "O_NOFOLLOW", 0)
        if not nofollow:
            raise WatchdogTransitionError("Watchdog-Dateischreiben benötigt O_NOFOLLOW")
        flags = (
            os.O_WRONLY
            | os.O_CREAT
            | os.O_EXCL
            | nofollow
            | getattr(os, "O_CLOEXEC", 0)
        )
        fd = os.open(temp, flags, 0o600)
        try:
            view = memoryview(payload)
            while view:
                written = os.write(fd, view)
                view = view[written:]
            os.fsync(fd)
            os.fchmod(fd, mode)
            os.fchown(fd, uid, gid)
            if mtime_ns is not None:
                os.utime(fd, ns=(mtime_ns, mtime_ns))
            os.fsync(fd)
        finally:
            os.close(fd)
        try:
            self._validate_target(target)
            os.replace(temp, target)
            self._fsync_dir(target.parent)
        finally:
            try:
                os.unlink(temp)
            except FileNotFoundError:
                pass

    def _write_journal(self, tx_dir: Path, record: Mapping[str, object]) -> None:
        payload = (json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
        self._atomic_write(
            tx_dir / "journal.json",
            payload,
            mode=0o600,
            uid=0,
            gid=0,
        )

    def _advance(self, tx_dir: Path, record: dict[str, object], phase: str) -> None:
        record["phase"] = phase
        record["updated_at"] = _utc_now()
        self._write_journal(tx_dir, record)
        self._phase(phase, record)

    def _new_transaction(
        self,
        bundle: WatchdogBundle,
        context: TransitionContext,
        *,
        child_correlation_id: str | None = None,
        start_service: bool = True,
    ) -> tuple[str, Path, dict[str, object]]:
        correlation = str(child_correlation_id or "")
        if correlation and not re.fullmatch(r"[0-9a-f]{32}", correlation):
            raise WatchdogTransitionError("Watchdog-Korrelation ist ungültig")
        if correlation:
            matches = []
            for existing_dir in sorted(self.journal_root.glob("tx-*")):
                if existing_dir.is_symlink() or not existing_dir.is_dir():
                    raise WatchdogTransitionError("Unsicherer Eintrag im Watchdog-Journal")
                existing = self._read_journal(existing_dir)
                if str(existing.get("child_correlation_id") or "") == correlation:
                    matches.append(str(existing.get("transaction_id") or ""))
            if matches:
                raise WatchdogTransitionError(
                    "Watchdog-Korrelation wurde bereits durch eine Kindtransaktion belegt"
                )
        transaction_id = f"{int(datetime.now(timezone.utc).timestamp())}-{secrets.token_hex(8)}"
        tx_dir = self.journal_root / f"tx-{transaction_id}"
        staging_dir = self.journal_root / (
            f".prepare-{transaction_id}-{secrets.token_hex(8)}"
        )
        staging_dir.mkdir(mode=0o700)
        os.chown(staging_dir, 0, 0)
        os.chmod(staging_dir, 0o700)
        self._fsync_dir(self.journal_root)
        self._fsync_dir(staging_dir)
        record: dict[str, object] = {
            "schema": WATCHDOG_BUNDLE_SCHEMA,
            "transaction_id": transaction_id,
            "child_correlation_id": correlation or None,
            "bundle_sha256": bundle.bundle_sha256,
            "interpreter_path": context.venv_python,
            "interpreter_sha256": bundle.interpreter_sha256,
            "created_at": _utc_now(),
            "updated_at": _utc_now(),
            "state": "preparing",
            "phase": "created",
            "previous_active": "unknown",
            "desired_active": "active" if start_service else "inactive",
            "writer_stop_started": False,
            "writer_stopped": False,
            "live_files_installed": [],
            "files": [],
            "expected": {
                self.paths.notify: {
                    "sha256": _sha256(bundle.notify),
                    "mode": 0o755,
                    "uid": self.target_uid,
                    "gid": self.target_gid,
                },
                self.paths.guard: {
                    "sha256": _sha256(bundle.guard),
                    "mode": 0o755,
                    "uid": self.target_uid,
                    "gid": self.target_gid,
                },
                self.paths.manifest: {
                    "sha256": _sha256(bundle.manifest),
                    "mode": 0o600,
                    "uid": self.target_uid,
                    "gid": self.target_gid,
                },
                self.paths.service: {
                    "sha256": _sha256(bundle.service.encode("utf-8")),
                    "mode": 0o644,
                    "uid": self.target_uid,
                    "gid": self.target_gid,
                },
            },
        }
        self._write_journal(staging_dir, record)
        if os.path.lexists(tx_dir):
            raise WatchdogTransitionError("Watchdog-Transaktions-ID ist nicht eindeutig")
        os.rename(staging_dir, tx_dir)
        self._fsync_dir(self.journal_root)
        return transaction_id, tx_dir, record

    def _stage_bundle(self, tx_dir: Path, bundle: WatchdogBundle) -> dict[str, Path]:
        candidate_dir = tx_dir / "candidates"
        candidate_dir.mkdir(mode=0o700)
        os.chown(candidate_dir, 0, 0)
        os.chmod(candidate_dir, 0o700)
        self._fsync_dir(tx_dir)
        self._fsync_dir(candidate_dir)
        candidates = {
            "notify": candidate_dir / "boot_notify.sh",
            "guard": candidate_dir / "pi_guard.sh",
            "service": candidate_dir / WATCHDOG_SERVICE,
            "manifest": candidate_dir / "watchdog-bundle.sha256",
        }
        for name, payload, mode in (
            ("notify", bundle.notify, 0o600),
            ("guard", bundle.guard, 0o600),
            ("service", bundle.service.encode("utf-8"), 0o600),
            ("manifest", bundle.manifest, 0o600),
        ):
            self._atomic_write(
                candidates[name], payload, mode=mode, uid=0, gid=0
            )
        self._run(["/usr/bin/bash", "-n", str(candidates["notify"])])
        self._run(["/usr/bin/bash", "-n", str(candidates["guard"])])
        self._run(["/usr/bin/systemd-analyze", "verify", str(candidates["service"])])
        return candidates

    def _capture_snapshot(self, target: Path, tx_dir: Path, index: int) -> _FileSnapshot:
        self._validate_target(target)
        if not target.exists():
            return _FileSnapshot(str(target), False, None, None, None, None, None, None)
        payload, info = self._read_regular(target)
        snapshot_dir = tx_dir / "snapshots"
        if not snapshot_dir.exists():
            snapshot_dir.mkdir(mode=0o700)
            os.chown(snapshot_dir, 0, 0)
            os.chmod(snapshot_dir, 0o700)
            self._fsync_dir(tx_dir)
            self._fsync_dir(snapshot_dir)
        backup_name = f"file-{index:02d}.bin"
        self._atomic_write(
            snapshot_dir / backup_name,
            payload,
            mode=0o600,
            uid=0,
            gid=0,
        )
        return _FileSnapshot(
            str(target),
            True,
            _sha256(payload),
            stat.S_IMODE(info.st_mode),
            info.st_uid,
            info.st_gid,
            info.st_mtime_ns,
            str(Path("snapshots") / backup_name),
        )

    def _restore_snapshot(self, snapshot: _FileSnapshot, tx_dir: Path) -> None:
        target = Path(snapshot.target)
        if not snapshot.existed:
            if target.exists() or target.is_symlink():
                self._validate_target(target)
                os.unlink(target)
                self._fsync_dir(target.parent)
            return
        if snapshot.backup_file is None:
            raise WatchdogTransitionError("Watchdog-Snapshot besitzt keine Sicherung")
        payload, backup_info = self._read_regular(tx_dir / snapshot.backup_file)
        if (
            backup_info.st_uid != 0
            or backup_info.st_gid != 0
            or stat.S_IMODE(backup_info.st_mode) != 0o600
        ):
            raise WatchdogTransitionError("Watchdog-Snapshot ist nicht root-privat")
        if _sha256(payload) != snapshot.sha256:
            raise WatchdogTransitionError("Watchdog-Snapshot-Prüfsumme stimmt nicht")
        self._atomic_write(
            target,
            payload,
            mode=int(snapshot.mode),
            uid=int(snapshot.uid),
            gid=int(snapshot.gid),
            mtime_ns=int(snapshot.mtime_ns),
        )
        restored, info = self._read_regular(target)
        if (
            _sha256(restored) != snapshot.sha256
            or stat.S_IMODE(info.st_mode) != snapshot.mode
            or info.st_uid != snapshot.uid
            or info.st_gid != snapshot.gid
            or info.st_mtime_ns != snapshot.mtime_ns
        ):
            raise WatchdogTransitionError("Watchdog-Datei wurde nicht exakt wiederhergestellt")

    def _validate_snapshot_still_current(self, snapshot: _FileSnapshot) -> None:
        target = Path(snapshot.target)
        if not snapshot.existed:
            if target.exists() or target.is_symlink():
                raise WatchdogTransitionError("Watchdog-Ziel entstand nach dem Snapshot")
            return
        payload, info = self._read_regular(target)
        if (
            _sha256(payload) != snapshot.sha256
            or stat.S_IMODE(info.st_mode) != snapshot.mode
            or info.st_uid != snapshot.uid
            or info.st_gid != snapshot.gid
            or info.st_mtime_ns != snapshot.mtime_ns
        ):
            raise WatchdogTransitionError("Watchdog-Ziel änderte sich nach dem Snapshot")

    def _query_active(self) -> str:
        result = self._run(["/usr/bin/systemctl", "is-active", WATCHDOG_SERVICE], check=False)
        value = next((line.strip().lower() for line in result.stdout.splitlines() if line.strip()), "")
        if value == "active" and result.returncode == 0:
            return "active"
        if value in {"inactive", "unknown", "not-found"} and result.returncode in {3, 4, 5}:
            return "inactive"
        if not value and result.returncode in {3, 4, 5}:
            return "inactive"
        raise WatchdogTransitionError("Piguard-Aktivzustand ist nicht eindeutig")

    def _query_quiesced(self) -> bool:
        """Belegt für den Release-Preparepfad inactive/dead und MainPID 0."""

        result = self._run(
            [
                "/usr/bin/systemctl",
                "show",
                "--no-pager",
                "--property=ActiveState",
                "--property=SubState",
                "--property=MainPID",
                WATCHDOG_SERVICE,
            ],
            check=False,
        )
        values: dict[str, str] = {}
        expected = {"ActiveState", "SubState", "MainPID"}
        for raw_line in str(result.stdout or "").splitlines():
            key, separator, value = raw_line.partition("=")
            if (
                separator != "="
                or key not in expected
                or key in values
                or value != value.strip()
            ):
                return False
            values[key] = value
        return bool(
            result.returncode == 0
            and not str(result.stderr or "")
            and values
            == {
                "ActiveState": "inactive",
                "SubState": "dead",
                "MainPID": "0",
            }
        )

    def _stop_writer(self) -> None:
        self._run(["/usr/bin/systemctl", "stop", WATCHDOG_SERVICE], check=False)
        if self._query_active() != "inactive":
            raise WatchdogTransitionError("Piguard-Writer konnte nicht bestätigt gestoppt werden")

    def _restore_activity(self, previous_active: str) -> None:
        if previous_active == "active":
            self._run(["/usr/bin/systemctl", "start", WATCHDOG_SERVICE])
            if self._query_active() != "active":
                raise WatchdogTransitionError("Alter Piguard-Zustand wurde nicht wieder aktiv")
        elif previous_active == "inactive":
            if self._query_active() != "inactive":
                raise WatchdogTransitionError("Alter Piguard-Zustand wurde nicht inaktiv")
        else:
            raise WatchdogTransitionError("Alter Piguard-Zustand ist unbekannt")

    @staticmethod
    def _digest_path(path: str) -> str:
        payload, _info = WatchdogBundleInstaller._read_regular(Path(path))
        return _sha256(payload)

    @staticmethod
    def _target_matches(path: str, expected: Mapping[str, object]) -> bool:
        try:
            payload, info = WatchdogBundleInstaller._read_regular(Path(path))
        except (OSError, WatchdogTransitionError):
            return False
        return (
            _sha256(payload) == str(expected.get("sha256", ""))
            and stat.S_IMODE(info.st_mode) == int(expected.get("mode", -1))
            and info.st_uid == int(expected.get("uid", -1))
            and info.st_gid == int(expected.get("gid", -1))
        )

    def _verify_live(self, bundle: WatchdogBundle, context: TransitionContext) -> bool:
        expected_files: dict[str, Mapping[str, object]] = {
            self.paths.notify: {
                "sha256": _sha256(bundle.notify),
                "mode": 0o755,
                "uid": self.target_uid,
                "gid": self.target_gid,
            },
            self.paths.guard: {
                "sha256": _sha256(bundle.guard),
                "mode": 0o755,
                "uid": self.target_uid,
                "gid": self.target_gid,
            },
            self.paths.manifest: {
                "sha256": _sha256(bundle.manifest),
                "mode": 0o600,
                "uid": self.target_uid,
                "gid": self.target_gid,
            },
            self.paths.service: {
                "sha256": _sha256(bundle.service.encode("utf-8")),
                "mode": 0o644,
                "uid": self.target_uid,
                "gid": self.target_gid,
            },
        }
        try:
            return all(
                self._target_matches(path, metadata)
                for path, metadata in expected_files.items()
            ) and self._hash_interpreter(context) == bundle.interpreter_sha256
        except (OSError, TransitionContextError, WatchdogTransitionError):
            return False

    def _mark_recovery_required(
        self,
        tx_dir: Path,
        record: dict[str, object],
        errors: Sequence[str],
    ) -> None:
        try:
            self._stop_writer()
            writer_stopped = True
        except Exception:
            writer_stopped = False
        record["state"] = "recovery_required"
        record["phase"] = "rollback_incomplete"
        record["rollback_errors"] = list(errors)
        record["writer_stopped"] = writer_stopped
        record["updated_at"] = _utc_now()
        self._write_journal(tx_dir, record)
        status = {
            "schema": WATCHDOG_BUNDLE_SCHEMA,
            "status": "recovery_required",
            "transaction_id": record["transaction_id"],
            "writer_stopped": writer_stopped,
            "error_count": len(errors),
            "updated_at": _utc_now(),
        }
        self._atomic_write(
            self.recovery_status_path,
            (json.dumps(status, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8"),
            mode=0o600,
            uid=0,
            gid=0,
        )

    def _read_journal(self, tx_dir: Path) -> dict[str, object]:
        info = os.lstat(tx_dir)
        if (
            stat.S_ISLNK(info.st_mode)
            or not stat.S_ISDIR(info.st_mode)
            or info.st_uid != 0
            or info.st_gid != 0
            or stat.S_IMODE(info.st_mode) != 0o700
        ):
            raise WatchdogTransitionError("Watchdog-Transaktionsverzeichnis ist unsicher")
        journal = tx_dir / "journal.json"
        payload, journal_info = self._read_regular(journal)
        if (
            journal_info.st_uid != 0
            or journal_info.st_gid != 0
            or stat.S_IMODE(journal_info.st_mode) != 0o600
        ):
            raise WatchdogTransitionError("Watchdog-Journaldatei ist nicht privat")
        try:
            record = json.loads(payload.decode("utf-8"))
        except Exception as exc:
            raise WatchdogTransitionError("Watchdog-Journal ist nicht lesbar") from exc
        if (
            not isinstance(record, dict)
            or record.get("schema") != WATCHDOG_BUNDLE_SCHEMA
            or not re.fullmatch(
                r"[0-9]+-[0-9a-f]{16}",
                str(record.get("transaction_id") or ""),
            )
            or tx_dir.name != f"tx-{record.get('transaction_id')}"
        ):
            raise WatchdogTransitionError("Watchdog-Journal besitzt ein unbekanntes Schema")
        correlation = record.get("child_correlation_id")
        if correlation is not None and (
            not isinstance(correlation, str)
            or not re.fullmatch(r"[0-9a-f]{32}", correlation)
        ):
            raise WatchdogTransitionError("Watchdog-Journal besitzt eine ungültige Korrelation")
        return record

    def _pending_transactions(self) -> list[tuple[Path, dict[str, object]]]:
        pending = []
        for tx_dir in sorted(self.journal_root.glob("tx-*")):
            if tx_dir.is_symlink() or not tx_dir.is_dir():
                raise WatchdogTransitionError("Unsicherer Eintrag im Watchdog-Journal")
            record = self._read_journal(tx_dir)
            if record.get("state") in {"preparing", "in_progress", "recovery_required"}:
                pending.append((tx_dir, record))
        return pending

    def _record_live_complete(self, record: Mapping[str, object]) -> bool:
        expected = record.get("expected")
        if not isinstance(expected, dict):
            return False
        try:
            if not all(
                isinstance(metadata, dict)
                and self._target_matches(str(path), metadata)
                for path, metadata in expected.items()
            ):
                return False
            interpreter = Path(str(record.get("interpreter_path", ""))).resolve(strict=True)
            payload, _info = self._read_regular(interpreter, single_link=False)
            return _sha256(payload) == str(record.get("interpreter_sha256", ""))
        except (OSError, WatchdogTransitionError):
            return False

    def _unit_is_candidate(self, record: Mapping[str, object]) -> bool:
        expected = record.get("expected")
        if not isinstance(expected, dict) or self.paths.service not in expected:
            return False
        metadata = expected[self.paths.service]
        if not isinstance(metadata, dict):
            return False
        try:
            return self._target_matches(self.paths.service, metadata)
        except (OSError, WatchdogTransitionError):
            return False

    def _clear_recovery_status_if_safe(self) -> None:
        if self._pending_transactions():
            return
        if self.recovery_status_path.exists() or self.recovery_status_path.is_symlink():
            self._validate_target(self.recovery_status_path)
            os.unlink(self.recovery_status_path)
            self._fsync_dir(self.recovery_status_path.parent)

    def _recover_incomplete_locked(self) -> list[str]:
        recovered: list[str] = []
        for tx_dir, record in self._pending_transactions():
            transaction_id = str(record["transaction_id"])
            if record.get("state") == "recovery_required":
                previous_errors = record.get("rollback_errors")
                errors = (
                    list(map(str, previous_errors))
                    if isinstance(previous_errors, (list, tuple)) and previous_errors
                    else ["sticky_recovery_required"]
                )
                self._mark_recovery_required(tx_dir, record, errors)
                raise WatchdogRecoveryRequired(transaction_id)
            desired_active = str(record.get("desired_active") or "active")
            if desired_active not in {"active", "inactive"}:
                self._mark_recovery_required(
                    tx_dir,
                    record,
                    ["desired-active:invalid"],
                )
                raise WatchdogRecoveryRequired(transaction_id)
            live_state_matches = (
                self._query_active() == "active"
                if desired_active == "active"
                else self._query_quiesced()
            )
            if self._record_live_complete(record) and live_state_matches:
                record["state"] = "committed"
                record["recovered"] = True
                self._advance(tx_dir, record, "recovered_commit")
                recovered.append(transaction_id)
                continue
            if self._unit_is_candidate(record):
                errors = ["unit:candidate-with-incomplete-bundle"]
                self._mark_recovery_required(tx_dir, record, errors)
                raise WatchdogRecoveryRequired(transaction_id)
            errors = self._rollback(tx_dir, record, unit_state_proven=True)
            if errors:
                raise WatchdogRecoveryRequired(transaction_id)
            recovered.append(transaction_id)
        self._clear_recovery_status_if_safe()
        return recovered

    def recover_incomplete(self) -> list[str]:
        """Recover interrupted outer and systemd journals without guessing."""
        with self._locked():
            self.systemd.recover_incomplete()
            return self._recover_incomplete_locked()

    def correlation_status(self, child_correlation_id: str) -> dict[str, object]:
        """Belegt genau einen korrelierten, live wirksamen Kindcommit."""

        correlation = str(child_correlation_id or "")
        if not re.fullmatch(r"[0-9a-f]{32}", correlation):
            raise WatchdogTransitionError("Watchdog-Korrelation ist ungültig")
        with self._locked():
            self.systemd.recover_incomplete()
            self._recover_incomplete_locked()
            return self._correlation_status_locked(correlation)

    def _correlation_status_locked(self, correlation: str) -> dict[str, object]:
        matches: list[tuple[Path, dict[str, object]]] = []
        for tx_dir in sorted(self.journal_root.glob("tx-*")):
            if tx_dir.is_symlink() or not tx_dir.is_dir():
                raise WatchdogTransitionError("Unsicherer Eintrag im Watchdog-Journal")
            record = self._read_journal(tx_dir)
            if str(record.get("child_correlation_id") or "") == correlation:
                matches.append((tx_dir, record))
        if len(matches) > 1:
            return {
                "status": "ambiguous",
                "child_correlation_id": correlation,
                "match_count": len(matches),
            }
        if not matches:
            return {
                "status": "missing",
                "child_correlation_id": correlation,
                "match_count": 0,
            }
        _tx_dir, record = matches[0]
        state = str(record.get("state") or "")
        phase = str(record.get("phase") or "")
        if (
            state == "committed"
            and phase in {"commit_complete", "recovered_commit"}
            and self._record_live_complete(record)
            and self._query_active() == "active"
        ):
            return {
                "status": "committed",
                "child_correlation_id": correlation,
                "match_count": 1,
                "transaction_id": str(record.get("transaction_id") or ""),
                "bundle_sha256": str(record.get("bundle_sha256") or ""),
                "phase": phase,
                "recovered": bool(record.get("recovered")),
            }
        if state == "recovery_required":
            status = "recovery_required"
        elif state == "rolled_back":
            status = "rolled_back"
        elif state in {"preparing", "in_progress"}:
            status = "incomplete"
        else:
            status = "drifted"
        return {
            "status": status,
            "child_correlation_id": correlation,
            "match_count": 1,
            "transaction_id": str(record.get("transaction_id") or ""),
            "phase": phase,
        }

    def _rollback(
        self,
        tx_dir: Path,
        record: dict[str, object],
        *,
        unit_state_proven: bool,
    ) -> list[str]:
        errors: list[str] = []
        live_touched = bool(record.get("writer_stop_started")) or bool(
            record.get("live_files_installed")
        ) or bool(record.get("systemd_transaction_id"))
        if live_touched:
            try:
                self._stop_writer()
            except Exception as exc:
                errors.append(f"stop:{type(exc).__name__}")
            for raw in reversed(list(record.get("files", []))):
                try:
                    self._restore_snapshot(_FileSnapshot.from_dict(raw), tx_dir)
                except Exception as exc:
                    errors.append(f"restore:{type(exc).__name__}")
            if unit_state_proven and not errors:
                try:
                    self._restore_activity(str(record.get("previous_active", "unknown")))
                except Exception as exc:
                    errors.append(f"activity:{type(exc).__name__}")
            elif not unit_state_proven:
                errors.append("unit:unproven")
        if errors:
            self._mark_recovery_required(tx_dir, record, errors)
        else:
            record["state"] = "rolled_back"
            record["rollback_errors"] = []
            self._advance(tx_dir, record, "rollback_complete")
        return errors

    def install(
        self,
        context: TransitionContext,
        router_ips: str,
        monitor_file: str = "",
        *,
        child_correlation_id: str | None = None,
        start_service: bool = True,
    ) -> str:
        if not isinstance(start_service, bool):
            raise WatchdogTransitionError("Watchdog-Startmodus ist nicht boolesch")
        if child_correlation_id and not start_service:
            raise WatchdogTransitionError(
                "Korrelierte Watchdog-Installation darf nicht quiesced bleiben"
            )
        bundle = self.render_bundle(context, router_ips, monitor_file)
        with self._locked():
            self.systemd.recover_incomplete()
            self._recover_incomplete_locked()
            transaction_id, tx_dir, record = self._new_transaction(
                bundle,
                context,
                child_correlation_id=child_correlation_id,
                start_service=start_service,
            )
            systemd_called = False
            try:
                self._phase("preflight_complete", record)
                candidates = self._stage_bundle(tx_dir, bundle)
                self._advance(tx_dir, record, "candidates_verified")

                snapshots = [
                    self._capture_snapshot(Path(target), tx_dir, index)
                    for index, target in enumerate(
                        (self.paths.notify, self.paths.guard, self.paths.manifest), start=1
                    )
                ]
                snapshots_by_target = {snapshot.target: snapshot for snapshot in snapshots}
                record["files"] = [snapshot.as_dict() for snapshot in snapshots]
                record["previous_active"] = self._query_active()
                record["state"] = "in_progress"
                self._advance(tx_dir, record, "snapshots_captured")

                record["writer_stop_started"] = True
                self._advance(tx_dir, record, "writer_stop_started")
                self._stop_writer()
                record["writer_stopped"] = True
                self._advance(tx_dir, record, "writer_stopped")
                for name, target, mode in (
                    ("notify", self.paths.notify, 0o755),
                    ("guard", self.paths.guard, 0o755),
                    ("manifest", self.paths.manifest, 0o600),
                ):
                    self._validate_snapshot_still_current(snapshots_by_target[target])
                    payload, _info = self._read_regular(candidates[name])
                    self._atomic_write(
                        Path(target),
                        payload,
                        mode=mode,
                        uid=self.target_uid,
                        gid=self.target_gid,
                    )
                    record["live_files_installed"] = [
                        *list(record.get("live_files_installed", [])),
                        name,
                    ]
                    self._advance(tx_dir, record, f"installed:{name}")

                def postcheck(_spec) -> bool:
                    desired_state_ok = (
                        self._query_active() == "active"
                        if start_service
                        else self._query_quiesced()
                    )
                    return self._verify_live(bundle, context) and desired_state_ok

                systemd_called = True
                result = self.systemd.install_unit(
                    self.paths.service,
                    bundle.service,
                    enable=True,
                    start=start_service,
                    writer=True,
                    postcheck=postcheck,
                    label=f"watchdog-{transaction_id}",
                )
                record["systemd_transaction_id"] = result.transaction_id
                record["state"] = "committed"
                self._advance(tx_dir, record, "commit_complete")
                return bundle.bundle_sha256
            except Exception as exc:
                record["failure_type"] = type(exc).__name__
                record["updated_at"] = _utc_now()
                self._write_journal(tx_dir, record)
                unit_state_proven = (not systemd_called) or isinstance(exc, TransitionRolledBack)
                if isinstance(exc, TransitionRecoveryRequired):
                    unit_state_proven = False
                errors = self._rollback(tx_dir, record, unit_state_proven=unit_state_proven)
                if errors:
                    raise WatchdogRecoveryRequired(transaction_id) from exc
                raise WatchdogTransitionRolledBack(transaction_id) from exc


def watchdog_correlation_status(child_correlation_id: str) -> dict[str, object]:
    """Produktionspfad für die äußere Notifier↔Watchdog-Recovery."""

    return WatchdogBundleInstaller().correlation_status(child_correlation_id)


@contextmanager
def watchdog_correlation_guard(child_correlation_id: str):
    """Hält den Watchdog-Lock über äußere Entscheidung und Zustandsübergang."""

    correlation = str(child_correlation_id or "")
    if not re.fullmatch(r"[0-9a-f]{32}", correlation):
        raise WatchdogTransitionError("Watchdog-Korrelation ist ungültig")
    installer = WatchdogBundleInstaller()
    with installer._locked():
        installer.systemd.recover_incomplete()
        installer._recover_incomplete_locked()
        yield installer._correlation_status_locked(correlation)


def _restore_r1_watchdog_auxiliaries(
    context: TransitionContext,
    *,
    runner: CommandRunner | None = None,
) -> list[str]:
    """Stellt Logzugriff und Legacy-Dateibereinigung nach dem Commit wieder her.

    Dies läuft bewusst erst nach dem Commit der SHA-gebundenen Watchdog-Transaktion.
    Es nimmt an dieser Transaktion weder teil noch schwächt es sie. Jeder
    Systembefehl verwendet feste absolute Argumente; Fehler werden gemeldet,
    ohne einen Erfolg der Hilfsaktion vorzutäuschen.
    """

    warnings: list[str] = []
    command_runner = runner or _default_command_runner

    try:
        result = command_runner(
            ("/usr/sbin/usermod", "-aG", "systemd-journal", "www-data"),
            30,
        )
        if int(result.returncode) != 0:
            warnings.append(f"Journalgruppe:Exit{int(result.returncode)}")
    except Exception as exc:
        warnings.append(f"Journalgruppe:{type(exc).__name__}")

    web_restarted = False
    for service in ("apache2.service", "lighttpd.service"):
        try:
            state = command_runner(
                ("/usr/bin/systemctl", "is-active", service),
                30,
            )
            active = int(state.returncode) == 0 and any(
                line.strip().lower() == "active" for line in str(state.stdout or "").splitlines()
            )
            if not active:
                continue
            restart = command_runner(
                ("/usr/bin/systemctl", "restart", service),
                30,
            )
            if int(restart.returncode) != 0:
                warnings.append(f"Webserver:{service}:Exit{int(restart.returncode)}")
            else:
                web_restarted = True
            break
        except Exception as exc:
            warnings.append(f"Webserver:{service}:{type(exc).__name__}")
    if not web_restarted and not any(item.startswith("Webserver:") for item in warnings):
        warnings.append("Webserver:kein aktiver Dienst")

    for name in ("=5.0", "5.0"):
        legacy = Path(context.home_dir) / name
        try:
            info = legacy.lstat()
        except FileNotFoundError:
            continue
        try:
            if stat.S_ISREG(info.st_mode) and info.st_nlink == 1 and not legacy.is_symlink():
                legacy.unlink()
            else:
                warnings.append(f"Legacy-Datei:{name}:unsicher")
        except OSError as exc:
            warnings.append(f"Legacy-Datei:{name}:{type(exc).__name__}")
    return warnings


def install_watchdog_bundle(
    router_ips: str,
    monitor_file: str = "",
    *,
    install_auxiliaries: bool = False,
    explicit_install_path: str | None = None,
    explicit_install_user: str | None = None,
    explicit_home_dir: str | None = None,
    explicit_venv_path: str | None = None,
    start_service: bool = True,
) -> str:
    """Löst den einmaligen Transitionskontext auf und installiert genau ein Paket."""
    if not isinstance(start_service, bool):
        raise WatchdogTransitionError("Watchdog-Startmodus ist nicht boolesch")
    if install_auxiliaries and not start_service:
        raise WatchdogTransitionError(
            "Notifier- und Watchdog-Installation darf nicht quiesced gekoppelt werden"
        )
    context = get_transition_context(
        explicit_install_path=explicit_install_path,
        explicit_install_user=explicit_install_user,
        explicit_home_dir=explicit_home_dir,
        explicit_venv_path=explicit_venv_path,
        require_trusted=True,
    )
    if install_auxiliaries:
        # Der Notifier bleibt bis zum bestätigten Watchdog-Commit in derselben
        # gesperrten Außentransaktion. Rollt der Watchdog intern zurück, wird
        # deshalb auch der Notifier samt Config/Cron/Unit-Vorzustand restauriert.
        print("\n=== Benachrichtigungs-Dienst einrichten ===")
        with notifier_install_transaction(
            start_service=True,
            migrate_legacy_config=True,
            watchdog_required=True,
        ) as notifier_transaction:
            child_correlation_id = begin_watchdog_child(notifier_transaction)
            bundle_sha = WatchdogBundleInstaller().install(
                context,
                router_ips,
                monitor_file,
                child_correlation_id=child_correlation_id,
                start_service=start_service,
            )
    else:
        bundle_sha = WatchdogBundleInstaller().install(
            context,
            router_ips,
            monitor_file,
            start_service=start_service,
        )
    if install_auxiliaries:
        warnings = _restore_r1_watchdog_auxiliaries(context)
        for warning in warnings:
            print(f"Watchdog-Nachlauf: {warning}")
    return bundle_sha


def create_boot_notify() -> bool:
    """Kompatibilitätseinstieg: repariert das vollständige Bundle, nie ein loses Skript."""
    current = get_current_config()
    install_watchdog_bundle(current["ROUTER_IP"], current["MONITOR_FILE"])
    return True


def create_pi_guard(router_ips, monitor_file="", *, start_service: bool = True) -> bool:
    """Kompatibilitätseinstieg: ersetzt Guard, Notify und Unit atomar."""
    install_watchdog_bundle(
        router_ips,
        monitor_file,
        start_service=start_service,
    )
    return True


def create_service() -> bool:
    """Kompatibilitätseinstieg: repariert das vollständige SHA-gebundene Watchdog-Bundle."""
    current = get_current_config()
    install_watchdog_bundle(current["ROUTER_IP"], current["MONITOR_FILE"])
    return True


def configure_hardware_watchdog() -> bool:
    """Dieser Übergang ändert system.conf nicht; dies darf nur eine eigene Transaktion."""
    print("Hardware-Watchdog system.conf bleibt in diesem Übergang unverändert.")
    return True


def _resolve_watchdog_menu_context() -> TransitionContext:
    """Bindet den registrierten Menüpfad an Wrapper-Nutzer, Root, Home und venv."""

    product_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    install_user = str(os.environ.get("E3DC_BOOTSTRAP_USER") or "").strip()
    if not install_user or install_user in {"root", "www-data"}:
        raise TransitionContextError(
            "Der Watchdog-Menüpfad benötigt den durch e3dc-setup gebundenen Nutzer"
        )
    try:
        account = pwd.getpwnam(install_user)
    except KeyError as exc:
        raise TransitionContextError("Installationsbenutzer existiert nicht") from exc

    from .utils import require_bound_venv_runtime, resolve_venv_target

    _venv_name, venv_path = resolve_venv_target(install_user)
    require_bound_venv_runtime(
        install_user=install_user,
        venv_path=venv_path,
    )
    return get_transition_context(
        explicit_install_path=product_root,
        explicit_install_user=install_user,
        explicit_home_dir=account.pw_dir,
        explicit_venv_path=venv_path,
        require_trusted=True,
    )


def setup_watchdog_menu():
    if os.geteuid() != 0:
        print("❌ Fehler: Dieses Skript muss als root ausgeführt werden.")
        return False
    try:
        context = _resolve_watchdog_menu_context()
    except Exception as exc:
        print(f"Watchdog-Installation sicher abgebrochen: {exc}")
        return False
    print("\n=== PV-Wächter & Telegram Setup ===")
    current = get_current_config()
    is_installed = os.path.exists(NOTIFY_PATH)
    if is_installed:
        print("✓ Watchdog ist bereits installiert.")
        print(f"  Aktueller Name: {current['DEVICE_NAME']}")
        print(f"  Router-IP: {current['ROUTER_IP']}")
        print(f"  Telegram: {'Aktiv' if current['TOKEN'] else 'Inaktiv'}")
        print("\n1. Komplett neu installieren / reparieren")
        print("2. Router-IP ändern")
        print("3. Abbrechen")
        choice = input("Auswahl: ").strip()
        if choice == "3":
            return True
    else:
        choice = "1"
    if choice not in {"1", "2"}:
        print("Ungültige Auswahl.")
        return False
    prompt = "Router-IP(s) für Watchdog" if choice == "1" else "Neue Router-IP(s)"
    router_ip = input(f"{prompt} [{current['ROUTER_IP']}]: ").strip() or current["ROUTER_IP"]
    try:
        router_ip = validate_router_ips(router_ip)
        bundle_sha = install_watchdog_bundle(
            router_ip,
            "",
            install_auxiliaries=True,
            explicit_install_path=context.install_path,
            explicit_install_user=context.install_user,
            explicit_home_dir=context.home_dir,
            explicit_venv_path=context.venv_path,
        )
    except Exception as exc:
        print(f"Watchdog-Installation sicher abgebrochen: {exc}")
        return False
    print(f"✓ Watchdog-Bundle installiert ({bundle_sha[:12]}).")
    print("Hardware-Watchdog system.conf wurde nicht verändert.")
    return True


def install_watchdog_silent(
    *,
    explicit_install_path: str | None = None,
    explicit_install_user: str | None = None,
    explicit_home_dir: str | None = None,
    explicit_venv_path: str | None = None,
):
    """Installiert das komplette Bundle; ohne Gateway bleibt alles unverändert."""
    print("\n=== Watchdog-Installation (Automatisch) ===")
    router_ip = detect_default_gateway()
    if not router_ip:
        print("Watchdog nicht installiert: kein gültiges Standard-Gateway erkannt.")
        return False
    try:
        bundle_sha = install_watchdog_bundle(
            router_ip,
            "",
            install_auxiliaries=True,
            explicit_install_path=explicit_install_path,
            explicit_install_user=explicit_install_user,
            explicit_home_dir=explicit_home_dir,
            explicit_venv_path=explicit_venv_path,
        )
    except Exception as exc:
        print(f"Watchdog-Installation sicher abgebrochen: {exc}")
        return False
    print(f"✓ Watchdog-Bundle installiert ({bundle_sha[:12]}).")
    print("Hardware-Watchdog system.conf wurde nicht verändert.")
    return True


if __name__ == "__main__":
    setup_watchdog_menu()


register_command("15", "Watchdog & Telegram konfigurieren", setup_watchdog_menu, sort_order=150)
