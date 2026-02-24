# E3DC-Control Installer & Web-Interface

Modularer Installer und Backend-Controller für E3DC-Control auf einem (Headless) Raspberry Pi.
Optimiert für die Nutzung als Progressive Web App (PWA) über Cloudflare Tunnel.

## Voraussetzungen

- Raspberry Pi OS (Bullseye oder neuer) - Headless (ohne Desktop) unterstützt!
- Python 3.7+
- sudo-Rechte
- Webserver (Apache/Nginx) mit PHP-Unterstützung für das `/var/www/html/` Frontend

## Installation

### Option 1 (empfohlen): Installation über GitHub

```bash
sudo apt update
sudo apt install -y git
cd ~
git clone [https://github.com/A9xxx/Install-E3DC-Control.git](https://github.com/A9xxx/Install-E3DC-Control.git) Install
cd Install
sudo python3 installer_main.py
Option 2: Headless / Unattended Modus (Neu)
Für automatisierte Installationen (oder Ausführung via PHP/Webinterface ohne Konsoleneingaben) kann der Installer im unbeaufsichtigten Modus gestartet werden. Alle Abfragen werden automatisch mit den Standardwerten beantwortet:

Bash
sudo python3 installer_main.py --unattended
Rechteprüfung für das Web-Interface (PWA)
Da das System über PHP (www-data) auf die Skripte zugreift, sind korrekte Dateiberechtigungen essenziell. Prüfe diese regelmäßig mit:

Bash
cd ~/Install
chmod +x check_permissions.sh
./check_permissions.sh
Wichtige Hinweise zum Ablauf
Beim Start erfolgt zuerst eine Update-Abfrage (im --unattended Modus wird dieses automatisch installiert).

Die IPC-Kommunikation (Inter-Process-Communication) zwischen PHP und Python läuft primär über die Datei e3dc_paths.json und das tmp/ Verzeichnis im Web-Root.


---

### 2. Verbesserung von `check_permissions.sh`
Dein aktuelles `check_permissions.sh` prüft sehr gut die `installer_config.json` und `e3dc_paths.json`. Was aber für dein Headless-PWA-Setup **extrem wichtig** ist: Deine PHP-Skripte (wie `archiv_diagramm.php` oder `start_content.php`) schreiben sogenannte Lockfiles (z.B. `plot_soc_running_mobile`) in das Verzeichnis `/var/www/html/tmp`. Wenn `www-data` hier keine Schreibrechte hat, friert das Frontend ein oder Diagramme laden nicht.

Ich habe das Bash-Skript so erweitert, dass es auch diese PWA-relevanten Ordner und die Ramdisk auf Konsistenz prüft!

**Hier ist die verbesserte `check_permissions.sh`:**

```bash
#!/usr/bin/env bash
set -euo pipefail

INSTALL_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CONFIG_JSON="$INSTALL_ROOT/Installer/installer_config.json"
WEB_JSON="/var/www/html/e3dc_paths.json"
WEB_TMP="/var/www/html/tmp"
WEB_RAMDISK="/var/www/html/ramdisk"

read_config_value() {
    local key="$1"
    if command -v jq >/dev/null 2>&1; then
        jq -r ".${key} // empty" "$CONFIG_JSON"
    else
        python3 - <<PY
import json
try:
    with open("$CONFIG_JSON", "r", encoding="utf-8") as f:
        data = json.load(f)
    print(data.get("$key", ""))
except Exception:
    print("")
PY
    fi
}

echo "== E3DC Installer & PWA Permissions Check =="
echo

if [[ ! -f "$CONFIG_JSON" ]]; then
    echo "[FEHLER] Config-Datei fehlt: $CONFIG_JSON"
    exit 1
fi

INSTALL_USER="$(read_config_value install_user)"
HOME_DIR="$(read_config_value home_dir)"

if [[ -z "$INSTALL_USER" ]]; then
    echo "[FEHLER] install_user konnte aus $CONFIG_JSON nicht gelesen werden"
    exit 1
fi

echo "Install-Root : $INSTALL_ROOT"
echo "Install-User : $INSTALL_USER"
echo "Home-Dir     : ${HOME_DIR:-<leer>}"
echo

echo "-- Besitzer/Modus prüfen --"
sudo stat -c '%U:%G %a %n' "$CONFIG_JSON" || true
sudo stat -c '%U:%G %a %n' "$WEB_JSON" 2>/dev/null || echo "[Hinweis] $WEB_JSON nicht vorhanden"
echo

echo "-- Zugriffstests Installer --"
if sudo -u "$INSTALL_USER" test -r "$CONFIG_JSON" && sudo -u "$INSTALL_USER" test -w "$CONFIG_JSON"; then
    echo "[OK] $INSTALL_USER kann installer_config.json lesen und schreiben"
else
    echo "[FEHLER] $INSTALL_USER hat nicht die vollen Rechte für installer_config.json"
fi

if sudo -u www-data test -r "$CONFIG_JSON"; then
    echo "[OK] www-data (Webserver) kann installer_config.json lesen"
else
    echo "[WARNUNG] www-data kann installer_config.json nicht lesen"
fi

echo
echo "-- Zugriffstests PWA (PHP Frontend) --"
if [[ -f "$WEB_JSON" ]]; then
    if sudo -u www-data test -r "$WEB_JSON"; then
        echo "[OK] www-data kann e3dc_paths.json lesen"
    else
        echo "[WARNUNG] www-data kann e3dc_paths.json nicht lesen"
    fi
fi

# NEU: Prüfe tmp-Verzeichnis für Lockfiles (Extrem wichtig für PHP Web-App)
if [[ -d "$WEB_TMP" ]]; then
    if sudo -u www-data test -w "$WEB_TMP"; then
        echo "[OK] www-data kann in $WEB_TMP schreiben (Wichtig für Lockfiles)"
    else
        echo "[FEHLER] www-data kann NICHT in $WEB_TMP schreiben! Diagramme könnten fehlschlagen."
        echo "Lösung: sudo chown -R www-data:www-data $WEB_TMP && sudo chmod -R 775 $WEB_TMP"
    fi
else
    echo "[HINWEIS] $WEB_TMP existiert noch nicht. Wird beim ersten PHP-Lauf erstellt."
fi

# NEU: Prüfe Ramdisk für Live-History
if [[ -d "$WEB_RAMDISK" ]]; then
    if sudo -u www-data test -r "$WEB_RAMDISK/live.txt" 2>/dev/null; then
        echo "[OK] www-data kann live.txt in der Ramdisk lesen"
    else
        echo "[WARNUNG] www-data kann live.txt nicht lesen oder Datei existiert nicht"
    fi
fi

echo
echo "-- Hardcoding-Scan (pi/home/pi) --"
grep -RInE '\bpi\b|/home/pi|pi:www-data|pi:pi|~pi' "$INSTALL_ROOT" --include='*.py' || true

echo
echo "Fertig."