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

