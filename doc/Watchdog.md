Markdown# Dokumentation: Pi5 PV-Wächter System (Watchdog)

Diese Dokumentation beschreibt die Einrichtung eines mehrstufigen Überwachungssystems für den Raspberry Pi 5. Es schützt die E3DC-Steuerung vor Systemabstürzen, Netzwerkverlust und Softwarefehlern.

---

## 1. Hardware-Watchdog (Kernel-Ebene)
Die unterste Sicherungsebene. Wenn das Betriebssystem (Kernel) komplett einfriert, erzwingt die Hardware einen Neustart.

* **Datei:** `/etc/systemd/system.conf`
* **Konfiguration:**
  ```ini
  RuntimeWatchdogSec=60
Aktivierung: ```bashsudo systemctl daemon-reexec
2. Telegram-Benachrichtigung (boot_notify.sh)Schnittstelle für Statusberichte und Warnungen an das Smartphone.Pfad: /usr/local/bin/boot_notify.shRechte: sudo chmod +x /usr/local/bin/boot_notify.shInhalt:Bash#!/bin/bash
# --- KONFIGURATION ---
TOKEN="DEIN_BOT_TOKEN"
CHAT_ID="DEINE_CHAT_ID"
IP_ADDR=$(hostname -I | cut -d' ' -f1)

# --- LOGIK ---
# Wenn kein Argument übergeben wurde, wird eine Boot-Nachricht gesendet.
if [ -z "$1" ]; then
    # Ursachenforschung im Log des letzten Boots (-b -1)
    REASON=$(journalctl -b -1 -t PIGUARD --no-pager | tail -n 1)
    if [ -z "$REASON" ]; then
        MSG="🚀 Pi5ControlSSD gestartet.%0A📍 IP: $IP_ADDR%0Aℹ️ Ursache: Manueller Start oder Stromausfall."
    else
        CLEAN_REASON=$(echo "$REASON" | sed 's/.*PIGUARD: //')
        MSG="⚠️ Pi5ControlSSD REBOOT erfolgt!%0A📍 IP: $IP_ADDR%0A❌ Grund: $CLEAN_REASON"
    fi
else
    # Nachrichtentext aus dem ersten Argument ($1) übernehmen
    MSG="$1%0A📍 IP: $IP_ADDR"
fi

# Senden an Telegram (URL-encoded)
curl -s -X POST "[https://api.telegram.org/bot$TOKEN/sendMessage](https://api.telegram.org/bot$TOKEN/sendMessage)" \
     --data-urlencode "chat_id=$CHAT_ID" \
     --data-urlencode "text=$MSG" > /dev/null
3. Software-Wächter (pi_guard.sh)Überwacht aktiv die FritzBox (Ping) und den E3DC-Screen-Prozess.Pfad: /usr/local/bin/pi_guard.shRechte: sudo chmod +x /usr/local/bin/pi_guard.shInhalt:Bash#!/bin/bash
# 5 Minuten Boot-Pause (Sicherheitsfenster für manuelle Eingriffe)
sleep 300

fb_fail=0
e3dc_fail=0
warned_fb=false
warned_e3dc=false

while true; do
  # --- CHECK 1: FRITZBOX (Ping) ---
  if ! ping -c 1 -W 2 192.168.178.1 > /dev/null; then
    ((fb_fail++))
    # Vorwarnung nach 50 Sek. (5 Fehlversuche à 10s)
    if [ $fb_fail -eq 5 ] && [ "$warned_fb" = false ]; then
        /usr/local/bin/boot_notify.sh "⚠️ FritzBox weg! Reboot in ca. 4 Min."
        warned_fb=true
    fi
  else
    fb_fail=0
    warned_fb=false
  fi

  # --- CHECK 2: E3DC SCREEN (Sessions vom User 'pi') ---
  if ! screen -ls pi/ | grep -v "Dead" | grep -q "E3DC"; then
    ((e3dc_fail++))
    # Vorwarnung nach 50 Sek.
    if [ $e3dc_fail -eq 5 ] && [ "$warned_e3dc" = false ]; then
        /usr/local/bin/boot_notify.sh "⚠️ E3DC Screen fehlt! Reboot in ca. 2 Min."
        warned_e3dc=true
    fi
  else
    e3dc_fail=0
    warned_e3dc=false
  fi

  # --- REBOOT ENTSCHEIDUNG ---
  # FritzBox: 30 Fehlversuche = 5 Min
  if [ $fb_fail -ge 30 ]; then
    echo "FritzBox seit 5 Min weg. Reboot!" | logger -t PIGUARD
    systemctl reboot
  fi
  # E3DC-Screen: 18 Fehlversuche = 3 Min
  if [ $e3dc_fail -ge 18 ]; then
    echo "E3DC Screen fehlt seit 3 Min. Reboot!" | logger -t PIGUARD
    systemctl reboot
  fi

  sleep 10
done
4. Automatisierung (Service & Crontab)Hintergrund-Dienst für pi_guard.shErstellen der Datei /etc/systemd/system/piguard.service:Ini, TOML[Unit]
Description=E3DC and FritzBox Guard Service
After=network-online.target

[Service]
Type=simple
ExecStart=/usr/local/bin/pi_guard.sh
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
Befehle: sudo systemctl daemon-reloadsudo systemctl enable piguard.servicesudo systemctl start piguard.serviceZeitgesteuerte Aufgaben (Crontab)Befehl: crontab -eBash# Sende Boot-Nachricht 45 Sek nach Systemstart
@reboot sleep 45 && /usr/local/bin/boot_notify.sh

# Täglicher Statusbericht um 12:00 Uhr mittags
00 12 * * * /usr/local/bin/boot_notify.sh "✅ Status: Pi5 online. Laufzeit: $(uptime -p). Temp: $(vcgencmd measure_temp | cut -d'=' -f2)"
Zusammenfassung der ReaktionszeitenEreignisWarnung nachReboot nachSystem "Freeze"-60 SekundenFritzBox Ausfall50 Sekunden5 MinutenE3DC Screen Ausfall50 Sekunden3 Minuten

PiGuard überprüfen: 

sudo systemctl status piguard -l

Logeinträge PiGuard überprüfen:

journalctl -f -t PIGUARD -t systemd


# Dokumentation: Telegram Bot Einrichtung

Diese Anleitung beschreibt, wie ein neuer Telegram Bot erstellt wird und wie du die benötigten Zugangsdaten (API-Token und Chat-ID) für den Pi5-Watchdog ermittelst.

---

## 1. Bot erstellen (BotFather)

Der "BotFather" ist der offizielle Bot von Telegram, um andere Bots zu verwalten.

1. Suche in Telegram nach dem Kontakt **@BotFather** und starte den Chat.
2. Sende den Befehl: `/newbot`
3. Der BotFather fragt dich nach einem **Namen** für deinen Bot (z.B. `Mein Pi5 Waechter`).
4. Danach fragt er nach einem **Benutzernamen** (dieser muss auf `_bot` enden, z.B. `Pi5_Control_SSD_bot`).
5. **Ergebnis:** Du erhältst eine Nachricht mit deinem **HTTP API Token**.
   * *Format:* `123456789:ABCdefGHIjklMNOpqrstuv`
   * **WICHTIG:** Diesen Token geheim halten!

---

## 2. Bot aktivieren

Damit der Bot dir Nachrichten schicken darf, musst du ihn einmalig kontaktieren (Spamschutz).

1. Suche in Telegram nach deinem neu erstellten Bot (über den Benutzernamen).
2. Drücke unten auf **START** oder schreibe ihm eine Nachricht (z.B. "Hallo").

---

## 3. Chat-ID ermitteln

Jeder Chat in Telegram hat eine eindeutige Nummer (ID). Der Bot muss wissen, an welche ID er die Nachrichten senden soll.

1. Suche in Telegram nach dem Bot **@userinfobot**.
2. Drücke auf **START**.
3. Der Bot antwortet dir mit deiner ID (eine mehrstellige Zahl).
   * *Beispiel:* `Id: 123456789`
   * **Hinweis:** Falls du die Nachrichten in eine Gruppe schicken möchtest, musst du die ID der Gruppe ermitteln (diese fangen oft mit einem Minuszeichen an, z.B. `-987654321`).

---

## 4. Zusammenfassung der Daten

Diese beiden Werte werden im Python-Installationsskript abgefragt:

* **Token:** `123456789:ABC...` (vom BotFather)
* **Chat-ID:** `123456789` (vom UserInfoBot)

---

## 5. Testen der Verbindung (Manuell)

Wenn du testen willst, ob dein Bot funktioniert, kannst du diesen Befehl im Terminal deines Raspberry Pi eingeben (ersetze Token und ID):

```bash
curl -s -X POST "[https://api.telegram.org/bot](https://api.telegram.org/bot)<DEIN_TOKEN>/sendMessage" \
     -d "chat_id=<DEINE_ID>&text=Testnachricht vom Pi5"


## Kurzbefehl um E3DC-Screen zu starten:

nano ~/.bashrc

# Alias für den E3DC-Start
alias se3='screen -dmS E3DC /pfad/zu/deinem/e3dc-script.sh'

source ~/.bashrc

pi_guard.sh Dead Screen sicher machen:

# --- E3DC SCREEN CHECK MIT REPARATUR-VERSUCH ---
  if ! screen -ls pi/ | grep -v "Dead" | grep -q "E3DC"; then
    ((e3dc_fail++))
    
    # Versuch 1: Nach 20 Sekunden versuchen wir den Screen einfach neu zu starten
    if [ $e3dc_fail -eq 2 ]; then
        /usr/local/bin/boot_notify.sh "🔧 E3DC Screen weg - Versuche Neustart..."
        # Alte tote Sessions aufräumen
        screen -wipe > /dev/null
        # Screen neu starten (wie in der Crontab)
        sudo -u pi /usr/bin/screen -dmS E3DC /home/pi/E3DC-Control/E3DC.sh
    fi

    # Vorwarnung für Reboot bleibt gleich
    if [ $e3dc_fail -eq 5 ] && [ "$warned_e3dc" = false ]; then
        /usr/local/bin/boot_notify.sh "⚠️ Neustart-Versuch erfolglos! Reboot in 2 Min."
        warned_e3dc=true
    fi
  else
    e3dc_fail=0
    warned_e3dc=false
  fi