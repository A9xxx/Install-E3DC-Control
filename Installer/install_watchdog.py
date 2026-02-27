import os
import sys
import subprocess
import tempfile
import re
from .installer_config import get_install_user
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
        "ROUTER_IP": "192.168.178.1"
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
        MSG="🚀 $DEVICE_NAME gestartet.%0A📍 IP: $IP_ADDR%0Aℹ️ Ursache: Manueller Start oder Stromausfall."
    else
        CLEAN_REASON=$(echo "$REASON" | sed 's/.*PIGUARD: //')
        MSG="⚠️ $DEVICE_NAME REBOOT erfolgt!%0A📍 IP: $IP_ADDR%0A❌ Grund: $CLEAN_REASON"
    fi
elif [ "$1" == "DAILY" ]; then
    UPTIME=$(uptime -p)
    TEMP=$(vcgencmd measure_temp | cut -d'=' -f2)
    MSG="✅ Status: $DEVICE_NAME Online.%0A📍 IP: $IP_ADDR%0A⏱ Laufzeit: $UPTIME%0A🌡 Temp: $TEMP"
else
    MSG="$1%0A📍 IP: $IP_ADDR"
fi
{telegram_logic}
"""
    with open("boot_notify.sh", "w", encoding="utf-8") as f:
        f.write(notify_content)
    subprocess.run(["sudo", "mv", "boot_notify.sh", NOTIFY_PATH])
    subprocess.run(["sudo", "chmod", "+x", NOTIFY_PATH])

def create_pi_guard(router_ips):
    """Erstellt das Watchdog-Skript."""
    print(f"Erstelle {GUARD_PATH}...")
    
    # IPs normalisieren (Kommas zu Leerzeichen, doppelte Leerzeichen entfernen)
    ips_normalized = " ".join(router_ips.replace(",", " ").split())
    install_user = get_install_user()
    
    guard_content = f"""#!/bin/bash
sleep 300
fb_fail=0
e3dc_fail=0
warned_fb=false
warned_e3dc=false

# Zu überwachende IPs (leerzeichengetrennt)
ROUTER_IPS="{ips_normalized}"

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
        /usr/local/bin/boot_notify.sh "⚠️ E3DC Screen fehlt! Reboot in 2 Min."
        warned_e3dc=true
    fi
  else
    e3dc_fail=0
    warned_e3dc=false
  fi

  if [ $fb_fail -ge 30 ]; then
    echo "Netzwerk ($ROUTER_IPS) seit 5 Min weg. Reboot!" | logger -t PIGUARD
    systemctl reboot
  fi
  if [ $e3dc_fail -ge 18 ]; then
    echo "E3DC Screen fehlt seit 3 Min. Reboot!" | logger -t PIGUARD
    systemctl reboot
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
        daily_job = f"0 {daily_hour} * * * /usr/local/bin/boot_notify.sh DAILY"
        
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
        create_pi_guard(router_ip)
        create_service()
        configure_hardware_watchdog()
        update_cronjobs(daily_enabled=(use_daily=="j"), daily_hour=daily_hour)
        
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
            create_pi_guard(new_ip)
            subprocess.run(["sudo", "systemctl", "restart", "piguard.service"])
            print("✓ Router-IP aktualisiert und Service neugestartet.")

if __name__ == "__main__":
    setup_watchdog_menu()

register_command("15", "Watchdog & Telegram konfigurieren", setup_watchdog_menu, sort_order=150)