import os
import sys
import subprocess
from .core import register_command

def setup_watchdog():
    # Prüfen auf Root-Rechte
    if os.geteuid() != 0:
        print("❌ Fehler: Dieses Skript muss mit 'sudo' ausgeführt werden!")
        print("Versuche: sudo python3 install_watchdog.py")
        sys.exit(1)

    print("--- Pi5 PV-Wächter Installations-Assistent ---")
    
    # 1. Benutzereingaben
    token = input("Bitte Telegram Bot-Token eingeben: ").strip()
    chat_id = input("Bitte Telegram Chat-ID eingeben: ").strip()
    
    # Pfade definieren
    notify_path = "/usr/local/bin/boot_notify.sh"
    guard_path = "/usr/local/bin/pi_guard.sh"
    service_path = "/etc/systemd/system/piguard.service"

    # 2. boot_notify.sh erstellen
    print(f"Erstelle {notify_path}...")
    notify_content = f"""#!/bin/bash
TOKEN="{token}"
CHAT_ID="{chat_id}"
IP_ADDR=$(hostname -I | cut -d' ' -f1)

if [ -z "$1" ]; then
    REASON=$(journalctl -b -1 -t PIGUARD --no-pager | tail -n 1)
    if [ -z "$REASON" ]; then
        MSG="🚀 ControlReserve gestartet.%0A📍 IP: $IP_ADDR%0Aℹ️ Ursache: Manueller Start oder Stromausfall."
    else
        CLEAN_REASON=$(echo "$REASON" | sed 's/.*PIGUARD: //')
        MSG="⚠️ ControlReserve REBOOT erfolgt!%0A📍 IP: $IP_ADDR%0A❌ Grund: $CLEAN_REASON"
    fi
else
    MSG="$1%0A📍 IP: $IP_ADDR"
fi

curl -s -X POST "https://api.telegram.org/bot$TOKEN/sendMessage" \\
     --data-urlencode "chat_id=$CHAT_ID" \\
     --data-urlencode "text=$MSG" > /dev/null
"""
    with open("boot_notify.sh", "w") as f:
        f.write(notify_content)
    subprocess.run(["sudo", "mv", "boot_notify.sh", notify_path])
    subprocess.run(["sudo", "chmod", "+x", notify_path])

    # 3. pi_guard.sh erstellen
    print(f"Erstelle {guard_path}...")
    guard_content = """#!/bin/bash
sleep 300
fb_fail=0
e3dc_fail=0
warned_fb=false
warned_e3dc=false

while true; do
  if ! ping -c 1 -W 2 192.168.178.1 > /dev/null; then
    ((fb_fail++))
    if [ $fb_fail -eq 5 ] && [ "$warned_fb" = false ]; then
        /usr/local/bin/boot_notify.sh "⚠️ FritzBox weg! Reboot in 4 Min."
        warned_fb=true
    fi
  else
    fb_fail=0
    warned_fb=false
  fi

  if ! screen -ls pi/ | grep -v "Dead" | grep -q "E3DC"; then
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
    echo "FritzBox seit 5 Min weg. Reboot!" | logger -t PIGUARD
    systemctl reboot
  fi
  if [ $e3dc_fail -ge 18 ]; then
    echo "E3DC Screen fehlt seit 3 Min. Reboot!" | logger -t PIGUARD
    systemctl reboot
  fi
  sleep 10
done
"""
    with open("pi_guard.sh", "w") as f:
        f.write(guard_content)
    subprocess.run(["sudo", "mv", "pi_guard.sh", guard_path])
    subprocess.run(["sudo", "chmod", "+x", guard_path])

    # 4. Systemd Service erstellen
    print(f"Erstelle Systemd Service {service_path}...")
    service_content = f"""[Unit]
Description=E3DC and FritzBox Guard Service
After=network-online.target

[Service]
Type=simple
ExecStart={guard_path}
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
"""
    with open("piguard.service", "w") as f:
        f.write(service_content)
    subprocess.run(["sudo", "mv", "piguard.service", service_path])
    subprocess.run(["sudo", "systemctl", "daemon-reload"])
    subprocess.run(["sudo", "systemctl", "enable", "piguard.service"])

    # 5. Hardware Watchdog konfigurieren
    print("Konfiguriere Hardware Watchdog in /etc/systemd/system.conf...")
    subprocess.run(["sudo", "sed", "-i", "s/^#RuntimeWatchdogSec=.*/RuntimeWatchdogSec=60/", "/etc/systemd/system.conf"])
    subprocess.run(["sudo", "sed", "-i", "s/^RuntimeWatchdogSec=.*/RuntimeWatchdogSec=60/", "/etc/systemd/system.conf"])

    print("\\n--- INSTALLATION ABGESCHLOSSEN ---")
    print("Bitte starte den Pi einmal neu, um alle Änderungen zu aktivieren.")

if __name__ == "__main__":
    setup_watchdog()

register_command("19", "Watchdog mit Telegram Benachrichtigung einrichten", setup_watchdog, sort_order=190)