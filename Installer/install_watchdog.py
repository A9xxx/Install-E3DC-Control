import os
import sys
import subprocess
import tempfile
import re
from .installer_config import get_install_user, get_install_path
from .utils import run_command
from .core import register_command

NOTIFY_PATH = "/usr/local/bin/boot_notify.sh"
GUARD_PATH = "/usr/local/bin/pi_guard.sh"
SERVICE_PATH = "/etc/systemd/system/piguard.service"

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
                ips_match = re.search(r'ROUTER_IPS="([^"]+)"', content)
                if ips_match:
                    config["ROUTER_IP"] = ips_match.group(1)
                else:
                    # Fallback auf altes Format (direkter Ping)
                    ip = re.search(r'ping -c 1 -W 2 ([0-9.]+)', content)
                    if ip: config["ROUTER_IP"] = ip.group(1)
                mf = re.search(r'MONITOR_FILE="([^"]*)"', content)
                if mf: config["MONITOR_FILE"] = mf.group(1)
        except Exception:
            pass
    return config

def create_boot_notify(token, chat_id, device_name):
    """Erstellt das Benachrichtigungs-Skript."""
    print(f"Erstelle {NOTIFY_PATH}...")
    
    # Telegram-Logik nur einbauen, wenn Token vorhanden
    telegram_logic = ""
    if token and chat_id:
        telegram_logic = f"""
# Senden an Telegram
curl -s -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \\
     --data-urlencode "chat_id=$CHAT_ID" \\
     --data-urlencode "text=$MSG" > /dev/null
"""
    else:
        telegram_logic = """
# Telegram deaktiviert (Kein Token/Chat-ID konfiguriert)
echo "Nachricht (nicht gesendet): $MSG"
"""

    notify_content = f"""#!/bin/bash
TOKEN="{token}"
CHAT_ID="{chat_id}"
DEVICE_NAME="{device_name}"
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
    TEMP=$(vcgencmd measure_temp | cut -d'=' -f2)
    MSG=$(printf "✅ Status: $DEVICE_NAME Online.\\n📍 IP: $IP_ADDR\\n⏱ Laufzeit: $UPTIME\\n🌡 Temp: $TEMP")
else
    MSG=$(printf "%s\\n📍 IP: $IP_ADDR" "$1")
fi
{telegram_logic}
"""
    with open("boot_notify.sh", "w", encoding="utf-8") as f:
        f.write(notify_content)
    subprocess.run(["sudo", "mv", "boot_notify.sh", NOTIFY_PATH])
    subprocess.run(["sudo", "chmod", "+x", NOTIFY_PATH])

def create_pi_guard(router_ips, monitor_file=""):
    """Erstellt das Watchdog-Skript."""
    print(f"Erstelle {GUARD_PATH}...")
    
    # IPs normalisieren (Kommas zu Leerzeichen, doppelte Leerzeichen entfernen)
    ips_normalized = " ".join(router_ips.replace(",", " ").split())
    install_user = get_install_user()
    
    guard_content = f"""#!/bin/bash
VERSION="2026.03.01"
echo "Watchdog v$VERSION gestartet. Warte 60s auf System..." | logger -t PIGUARD
sleep 60
fb_fail=0
e3dc_fail=0
file_fail=0
disk_fail=0
warned_fb=false
warned_e3dc=false
warned_file=false
warned_disk=false
LAST_CHECKED_FILE=""

# Zu überwachende IPs (leerzeichengetrennt)
ROUTER_IPS="{ips_normalized}"
MONITOR_FILE="{monitor_file}"

if [ -z "$MONITOR_FILE" ]; then
    echo "Info: Keine Datei-Überwachung konfiguriert." | logger -t PIGUARD
fi

while true; do
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

  # --- CHECK 2: E3DC SCREEN ---
  if ! screen -ls {install_user}/ | grep -v "Dead" | grep -q "E3DC"; then
    ((e3dc_fail++))
    if [ $e3dc_fail -eq 5 ] && [ "$warned_e3dc" = false ]; then
        /usr/local/bin/boot_notify.sh "⚠️ E3DC Screen fehlt! Restart Service in 2 Min."
        warned_e3dc=true
    fi
  else
    e3dc_fail=0
    warned_e3dc=false
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
      echo "Überwache Datei: $ACTUAL_FILE" | logger -t PIGUARD
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
    echo "Netzwerk ($ROUTER_IPS) seit 5 Min weg. Reboot!" | logger -t PIGUARD
    systemctl reboot
  fi
  if [ $e3dc_fail -ge 18 ]; then
    echo "E3DC Screen fehlt seit 3 Min. Restart E3DC Service!" | logger -t PIGUARD
    systemctl restart e3dc
    /usr/local/bin/boot_notify.sh "⚠️ E3DC Service neu gestartet (Screen fehlte)."
    e3dc_fail=0
    warned_e3dc=false
    sleep 60
  fi
  if [ $file_fail -ge 18 ]; then
    echo "Watchdog-Datei $ACTUAL_FILE seit >18 Min nicht aktualisiert. Restart E3DC Service!" | logger -t PIGUARD
    systemctl restart e3dc
    /usr/local/bin/boot_notify.sh "⚠️ E3DC Service neu gestartet (Datei $ACTUAL_FILE veraltet)."
    file_fail=0
    warned_file=false
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

def update_cronjobs(daily_enabled=True, daily_hour=12):
    """Aktualisiert die Cronjobs."""
    print("Aktualisiere Cronjobs...")
    try:
        install_user = get_install_user()
        
        # Basis-Job (immer vorhanden wenn Watchdog installiert)
        reboot_job = "@reboot sleep 45 && /usr/local/bin/boot_notify.sh"
        
        # Daily Job (optional)
        daily_job = f"0 {daily_hour} * * * /usr/local/bin/boot_notify.sh status"
        
        # Aktuelle Crontab lesen
        res = run_command(f"sudo crontab -u {install_user} -l")
        current_cron = res['stdout'] if res['success'] else ""
        
        # Liste neu aufbauen
        new_lines = []
        for line in current_cron.splitlines():
            if not line.strip(): continue
            # Entferne alle boot_notify Zeilen um sie sauber neu zu setzen
            if "/usr/local/bin/boot_notify.sh" in line:
                continue
            new_lines.append(line)
        
        # Neue Jobs hinzufügen
        new_lines.append(reboot_job)
        if daily_enabled:
            new_lines.append(daily_job)
            print(f"  + Täglicher Bericht aktiviert um {daily_hour}:00 Uhr")
        else:
            print("  - Täglicher Bericht deaktiviert")
        
        # Schreiben
        with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', delete=False) as tmp:
            tmp.write("\n".join(new_lines) + "\n")
            tmp_path = tmp.name
        
        res = run_command(f"sudo crontab -u {install_user} {tmp_path}")
        os.unlink(tmp_path)
        
        if res['success']:
            print("  ✓ Cronjobs erfolgreich geschrieben")
        else:
            print(f"  ✗ Fehler beim Schreiben der Crontab: {res['stderr']}")
            
    except Exception as e:
        print(f"  ✗ Fehler bei Cronjob-Einrichtung: {e}")

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
        print("2. Nur Gerätenamen ändern")
        print("3. Täglichen Bericht konfigurieren")
        print("4. Telegram-Einstellungen ändern")
        print("5. Router-IP ändern")
        print("6. Abbrechen")
        
        choice = input("Auswahl: ").strip()
        if choice == "6": return
    else:
        choice = "1"

    # --- LOGIK ---
    
    if choice == "1": # Installieren
        # Telegram
        use_tg = input("Möchtest du Telegram-Benachrichtigungen nutzen? (j/n) [j]: ").strip().lower() or "j"
        token = ""
        chat_id = ""
        if use_tg == "j":
            token = input(f"Telegram Bot-Token [{current['TOKEN']}]: ").strip() or current['TOKEN']
            chat_id = input(f"Telegram Chat-ID [{current['CHAT_ID']}]: ").strip() or current['CHAT_ID']
        
        # Name
        device_name = input(f"Gerätename [{current['DEVICE_NAME']}]: ").strip() or current['DEVICE_NAME']
        
        # Router IP
        router_ip = input(f"Router-IP(s) für Watchdog (getrennt durch Leerzeichen) [{current['ROUTER_IP']}]: ").strip() or current['ROUTER_IP']
        
        # Monitor File (Hänger-Schutz)
        monitor_file = current['MONITOR_FILE']
        install_path = get_install_path()
        if not monitor_file:
            # Versuche Logfile aus Config zu erraten
            try:
                config_path = os.path.join(install_path, "e3dc.config.txt")
                if os.path.exists(config_path):
                    with open(config_path, "r") as f:
                        for line in f:
                            if line.strip().lower().startswith("logfile"):
                                parts = line.split("=")
                                if len(parts) > 1:
                                    val = parts[1].strip().strip('"').strip("'")
                                    if val:
                                        monitor_file = val if val.startswith("/") else os.path.join(install_path, val)
                                    break
            except: pass
        
        # Fallback auf Standard-Protokoll, wenn nichts gefunden
        if not monitor_file:
            monitor_file = os.path.join(install_path, "protokoll.{day}.txt")
        
        use_file = input(f"Datei auf Aktualisierung überwachen (Hänger-Schutz)? (j/n) [j]: ").strip().lower()
        if use_file == 'j' or use_file == '':
            print("   ℹ️  Tipp: Für täglich wechselnde Dateien (z.B. protokoll.Sa.txt) nutze '{day}' als Platzhalter.")
            monitor_file = input(f"Pfad zur Datei [{monitor_file}]: ").strip() or monitor_file
        else:
            monitor_file = ""

        # Daily
        use_daily = input("Täglichen Statusbericht senden? (j/n) [j]: ").strip().lower() or "j"
        daily_hour = 12
        if use_daily == "j":
            try:
                h = input("Um wie viel Uhr? (0-23) [12]: ").strip()
                daily_hour = int(h) if h else 12
            except:
                daily_hour = 12
        
        # Ausführen
        create_boot_notify(token, chat_id, device_name)
        create_pi_guard(router_ip, monitor_file)
        create_service()
        configure_hardware_watchdog()
        update_cronjobs(daily_enabled=(use_daily=="j"), daily_hour=daily_hour)
        
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

    elif choice == "2": # Name ändern
        new_name = input(f"Neuer Gerätename [{current['DEVICE_NAME']}]: ").strip()
        if new_name:
            create_boot_notify(current['TOKEN'], current['CHAT_ID'], new_name)
            print("✓ Name geändert.")
            
    elif choice == "3": # Daily Config
        use_daily = input("Täglichen Statusbericht aktivieren? (j/n): ").strip().lower()
        hour = 12
        if use_daily == "j":
            try:
                h = input("Um wie viel Uhr? (0-23) [12]: ").strip()
                hour = int(h) if h else 12
            except: pass
        
        update_cronjobs(daily_enabled=(use_daily=="j"), daily_hour=hour)
        
    elif choice == "4": # Telegram Config
        use_tg = input("Telegram nutzen? (j/n): ").strip().lower()
        token = ""
        chat_id = ""
        if use_tg == "j":
            token = input(f"Token [{current['TOKEN']}]: ").strip() or current['TOKEN']
            chat_id = input(f"Chat-ID [{current['CHAT_ID']}]: ").strip() or current['CHAT_ID']
        
        create_boot_notify(token, chat_id, current['DEVICE_NAME'])
        print("✓ Telegram-Einstellungen aktualisiert.")
        
    elif choice == "5": # Router IP
        new_ip = input(f"Neue Router-IP(s) [{current['ROUTER_IP']}]: ").strip()
        if new_ip:
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
            router_ip = detected
    except:
        pass
        
    # 2. Monitor File ermitteln
    install_path = get_install_path()
    monitor_file = os.path.join(install_path, "protokoll.{day}.txt")
    
    # Versuche aus Config zu lesen
    try:
        config_path = os.path.join(install_path, "e3dc.config.txt")
        if os.path.exists(config_path):
            with open(config_path, "r") as f:
                for line in f:
                    if line.strip().lower().startswith("logfile"):
                        parts = line.split("=")
                        if len(parts) > 1:
                            val = parts[1].strip().strip('"').strip("'")
                            if val:
                                monitor_file = val if val.startswith("/") else os.path.join(install_path, val)
                            break
    except: pass

    print(f"Konfiguration (Silent):")
    print(f"  Telegram:      Deaktiviert")
    print(f"  Router-IP:     {router_ip}")
    print(f"  Monitor-Datei: {monitor_file}")

    # Installation durchführen
    create_boot_notify("", "", "E3DC-Control") # Token/ChatID leer -> Telegram aus
    create_pi_guard(router_ip, monitor_file)
    create_service()
    configure_hardware_watchdog()
    update_cronjobs(daily_enabled=True, daily_hour=12)
    
    # Rechte setzen
    subprocess.run(["sudo", "usermod", "-aG", "systemd-journal", "www-data"])
    subprocess.run("sudo systemctl restart apache2 2>/dev/null || sudo systemctl restart lighttpd 2>/dev/null", shell=True)
    subprocess.run(["sudo", "systemctl", "restart", "piguard.service"])
    
    print("✓ Watchdog erfolgreich installiert.")

if __name__ == "__main__":
    setup_watchdog_menu()

register_command("15", "Watchdog & Telegram konfigurieren", setup_watchdog_menu, sort_order=150)