# Quickstart: E3DC-Control Installation

Diese Anleitung fasst die schnellsten Schritte zusammen, um E3DC-Control auf einem frischen Raspberry Pi OS (oder ähnlichem Debian-System) zu installieren.

Aktueller Stable-Stand: `v5.4.2b`.

## Variante A: Klassische Installation (Installer)


## Schritt 1: System vorbereiten

Stellen Sie sicher, dass Ihr System auf dem neuesten Stand ist und `git` installiert ist.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git
```

## Schritt 2: Installer herunterladen

Klonen Sie das Repository, das den Installer enthält. Wenn Sie dieses Dokument lesen, haben Sie diesen Schritt wahrscheinlich schon erledigt. Falls nicht, hier ein Beispielbefehl (URL anpassen):

```bash
# Beispiel für das Klonen in das Home-Verzeichnis
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
git clone https://github.com/A9xxx/Install-E3DC-Control.git "$E3DC_INSTALL_PATH"
cd "$E3DC_INSTALL_PATH"
```
*Hinweis: Passen Sie die URL und die Verzeichnisnamen entsprechend an.*

## Schritt 3: Installation starten

Führen Sie das Haupt-Installationsskript im `Install`-Verzeichnis mit `sudo` aus. Dies startet den interaktiven Installer.

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Das flache Hauptmenü zeigt nur noch die direkt benötigten Aktionen. Erweiterungen und Spezialfälle liegen gesammelt im Expertenmenü.

## Schritt 4: "Installation / Update" auswählen

Für eine Erstinstallation oder ein Update ist die empfohlene Option **"1 Installation / Update"**.
1.  Wählen Sie `1` im Hauptmenü.
2.  Der Installer richtet Pakete, Dienste, Webdateien, Rechte und den Web-Wizard ein.
3.  Folgen Sie den Anweisungen auf dem Bildschirm.

**Einmalige Ausnahme für Version 5.3.2b:** Der erste Wechsel auf das aktuelle Stable-Release
erfolgt über den Web-Update-Button oder direkt mit
`sudo /usr/bin/python3 installer_main.py --update-e3dc`. Der interaktive
Menüpunkt darf für genau diesen ersten Hybridwechsel nicht verwendet werden,
weil sein Altprozess bereits zusätzliche 5.3.2b-Module geladen hat. Die
vollständige geprüfte SSH-Kette steht in [Update.md](Update.md).

```text
1) Installation / Update
2) Systemstatus anzeigen
3) Rechte prüfen & korrigieren
4) Notfallmodus / System reparieren
5) Policygebundener Programm-Rückfall (falls für Bare Metal freigegeben)
6) Backup erstellen / verwalten
7) Expertenmenü
8) Systempakete vorbereiten
9) Deinstallation
q) Beenden
```

Optionen wie Docker, Energy Manager, native Wallbox, MQTT, Bluelink oder Home Assistant liegen im **Expertenmenü**. Die normale Anlagenkonfiguration erfolgt anschließend im WebUI.

Das Expertenmenü nutzt feste 10er-Blöcke:

```text
Kernsystem & Update
  14) Rollback (Datei-Backup)
  15) Watchdog & Telegram konfigurieren
Umgebung & Python
  21) Python venv neu aufbauen (Reparatur)
  22) Python venv Namen ändern
Docker Migration & Verwaltung
  31) Zu Docker wechseln (Auto-Install & Migration)
  32) Docker auflösen & zum lokalen System zurückkehren
Erweiterungen & Smart Home
  41) Energy Manager
```

## Variante B: Docker Installation (Empfohlen)

Mit Docker läuft E3DC-Control komplett gekapselt. Die Community kann das fertige Production-Image laden, ohne selbst kompilieren zu müssen.

1. **Docker installieren:**
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
. /etc/os-release
DOCKER_REPO=debian
DOCKER_CODENAME="${VERSION_CODENAME}"
if [ "${ID}" = "ubuntu" ] || echo "${ID_LIKE:-}" | grep -qw ubuntu; then
  DOCKER_REPO=ubuntu
  DOCKER_CODENAME="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
fi
sudo curl -fsSL --proto '=https' --tlsv1.2 \
  "https://download.docker.com/linux/${DOCKER_REPO}/gpg" \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
  "$DOCKER_REPO" "$DOCKER_CODENAME" "$(dpkg --print-architecture)" | \
  sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker $USER
newgrp docker
```
2. **Verzeichnis vorbereiten:**
Erstellen Sie einen neuen Ordner mit persistentem `data`-Verzeichnis. Der Anwendungscode kommt aus dem Docker-Image.
```bash
export E3DC_DOCKER_PATH="/absoluter/pfad/zur/docker-installation"
mkdir -p "$E3DC_DOCKER_PATH"
cd "$E3DC_DOCKER_PATH"
mkdir -p data logs
# optional für Altanlagen:
# cp /dein/pfad/e3dc.config.txt data/
```
3. **docker-compose.yml erstellen:**
Legen Sie folgende `docker-compose.yml` in diesem Ordner an:
```yaml
services:
  e3dc-control:
    image: "ghcr.io/a9xxx/install-e3dc-control:${E3DC_IMAGE_TAG:-latest}"
    container_name: e3dc-control
    restart: unless-stopped
    network_mode: "host"
    volumes:
      - ./data:/var/www/html/data
      - ./logs:/var/www/html/logs
      - e3dc_ml:/var/lib/e3dc-control/ml
      - e3dc_forecast_evidence:/var/lib/e3dc-control/forecast-evidence
    tmpfs:
      - /var/www/html/ramdisk:size=32M,uid=33,gid=33,mode=2775
    environment:
      - TZ=Europe/Berlin

volumes:
  e3dc_ml:
  e3dc_forecast_evidence:
```
4. **Starten:**
```bash
docker compose up -d
```
Den Zustand des laufenden Containers prüfen Sie ohne Zugriff auf den Docker-
Socket im Container mit:
```bash
docker inspect e3dc-control --format '{{.Config.Image}} {{.State.Status}}'
```
Das System richtet sich nun im Hintergrund selbst ein und ist nach ca. 60 Sekunden unter der IP Ihres Raspberry Pis im Browser erreichbar.

**Docker-Updates:**
```bash
(
  set -euo pipefail
  cd "$E3DC_DOCKER_PATH"
  docker compose config --images
  docker compose pull e3dc-control
  docker compose up -d --force-recreate e3dc-control
)
```
Ohne `E3DC_IMAGE_TAG` holt `pull` das aktuelle geprüfte Stable-Image `latest`.
Ein fester Tag bleibt absichtlich fest; `config --images` zeigt vorab das
tatsächlich gewählte Image. `--force-recreate` startet den Container daraus
neu.

Der optionale, nicht mehr gepflegte Watchtower startet nicht automatisch, weil
er weitreichenden Zugriff auf den Docker-Socket des Hosts benötigt. Ein
bewusster Opt-in erfolgt mit
`docker compose --profile auto-update up -d watchtower`; standardmäßig bleibt
der oben gezeigte manuelle Updateweg aktiv.

**Docker-Rückfall von v5.4.2b auf den veröffentlichten Docker-Rollback-Root:**
```bash
(
  set -euo pipefail
  TAG=v5.3.2b
  cd "$E3DC_DOCKER_PATH"
  E3DC_IMAGE_TAG="$TAG" docker compose config --images
  E3DC_IMAGE_TAG="$TAG" docker compose pull e3dc-control
  E3DC_IMAGE_TAG="$TAG" docker compose up -d --force-recreate e3dc-control
  docker logs --tail=80 e3dc-control
)
```
Soll der Pin dauerhaft gelten, wird `E3DC_IMAGE_TAG=v5.3.2b` in einer
vorhandenen `.env` ergänzt, ohne andere Werte darin zu überschreiben.
Der Container kann den Docker-Daemon des Hosts absichtlich nicht selbst
bedienen. Die Weboberfläche zeigt für Docker deshalb nur die passenden
Host-Befehle zur gewählten Version an. `v5.3.2b` ist der einzige vorgesehene
öffentliche Docker-Rückfallstand und gibt selbst keinen älteren Image-Tag frei.
Auf Bare Metal wird dieser Altstand nicht als Programm-Rückfall angeboten.

**Wichtig zur Ramdisk im Docker:** `/var/www/html/ramdisk` ist flüchtig und
nach jedem Container-Neustart leer. Dateien wie `ml_prediction.json` werden
erst von den Diensten neu erzeugt. Wenn `ml_predictor.py --predict`
`Kein Modell gefunden` meldet, fehlt ein gültiges Manifest im privaten
Modell-Volume `/var/lib/e3dc-control/ml`; das ist kein Fehler der
`uid=33,gid=33`-Ramdisk-Konfiguration. Eine alte Datei
`/var/www/html/data/ml_model.pkl` wird aus Sicherheitsgründen nicht geladen.

Die optionale PV-Prognosediagnose nutzt ein davon getrenntes privates Volume
unter `/var/lib/e3dc-control/forecast-evidence`. Sie bleibt ohne
`forecast_diagnostics_enable=1` vollständig aus. Nach einer Änderung dieses
Schalters muss der Container neu gestartet werden; bei `Aus` liest der
Diagnosedienst weder E3/DC-Historie noch legt er eine Datenbank an.

Bei einem normalen Container-Stopp/Rebuild legt E3DC-Control einen kleinen
Warmstart-Cache unter `data/docker_ramdisk_cache/` an. Dieser Cache enthält
Prognose-, Preis- und Verlaufsdaten, aber keine Steuerflags. So startet das
Dashboard nach `docker compose up -d --force-recreate` schneller mit
plausiblen Daten und wird danach von den Diensten aktualisiert.

**Synology/NAS Port 80 belegt:** Im Docker-Compose unter `environment` kann
der Webserver-Port gesetzt werden:

```yaml
- E3DC_WEB_PORT=8085
# optional: nur auf eine bestimmte Host-IP binden
# - E3DC_WEB_BIND=192.0.2.20
```

Mit `network_mode: host` wird kein `ports:`-Mapping genutzt; Apache bindet
direkt auf diesem Host-Port. Danach z.B. `http://<NAS-IP>:8085/` aufrufen oder
den Synology Reverse Proxy auf diesen Port zeigen lassen.

Wenn die Weboberfläche läuft, aber `Warte auf E3DC...` zeigt, ist Docker/Port
bereits in Ordnung. Dann im Config-Editor RSCP-IP, Port 5033, E3DC-Benutzer,
Passwort und AES-Passwort speichern und den Container einmal neu starten.

---

## Wichtige Befehle für die Wartung

Nach der Installation können Sie den Installer über `bash "$E3DC_INSTALL_PATH/e3dc-setup"` starten und die gewünschte Wartungsoption wählen:

- **E3DC-Control installieren oder aktualisieren:**
  - Option `1` (Installation / Update)
  - Hält Anwendung, Webdateien, Dienste und Rechte auf dem aktuellen Stand.
  - Ausnahme: Für den einmaligen Wechsel von 5.3.2b auf das aktuelle Stable-Release den
    Web-Update-Button oder den direkten `--update-e3dc`-Aufruf aus
    [Update.md](Update.md) verwenden, nicht den interaktiven Menüpunkt.

- **Berechtigungen überprüfen & korrigieren:**
  - Option `3` (Rechte prüfen & korrigieren)
  - Sehr nützlich, wenn es nach manuellen Änderungen zu Zugriffsproblemen kommt.

- **Backups verwalten:**
  - Option `6` (Backup erstellen / verwalten)
  - Erstellen, Wiederherstellen oder Löschen von Backups.

- **Laufende Dienste überprüfen:**
  ```bash
  systemctl is-active e3dc-live e3dc-storage-manager e3dc-wallbox-manager apache2
  journalctl -u e3dc-live -n 80 --no-pager
  ```
