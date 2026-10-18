# Quickstart: E3DC-Control Installation

Diese Anleitung fasst die schnellsten Schritte zusammen, um E3DC-Control auf einem frischen Raspberry Pi OS (oder ähnlichem Debian-System) zu installieren.

Aktueller Stable-Stand: `v5.5.5a`.

Was sich in den einzelnen Versionen geändert hat, steht in den
[Release Notes](../RELEASE_NOTES.md) und im [Changelog](../CHANGELOG.md).

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

Starten Sie das Haupt-Installationsskript als normaler Benutzer mit
`sudo`-Rechten. Das Skript fordert Systemrechte nur für die Schritte an, die
sie tatsächlich benötigen.

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Das flache Hauptmenü zeigt nur noch die direkt benötigten Aktionen. Erweiterungen und Spezialfälle liegen gesammelt im Expertenmenü.

## Schritt 4: "Installation / Update" auswählen

Für eine Erstinstallation oder ein Update ist die empfohlene Option **"1 Installation / Update"**.
1.  Wählen Sie `1` im Hauptmenü.
2.  Der Installer richtet Pakete, Dienste, Webdateien, Rechte und die sichere Grundkonfiguration ein.
3.  Folgen Sie den Anweisungen auf dem Bildschirm.

**Einmaliger Übergang aus einem Altstand:** Kopiere die veröffentlichte Datei
`e3dc-update-bootstrap` an einen beliebigen Ort auf den Raspberry Pi und starte
sie im Ablageverzeichnis:

```bash
sudo /bin/sh ./e3dc-update-bootstrap
```

Der damit gestartete veröffentlichte Updatepfad erkennt Installationsordner,
Installationsbenutzer und Rolle selbst und arbeitet als
systemd-Hintergrundauftrag. Ein fester
Installationspfad oder ein `chmod` ist nicht erforderlich. Bei mehreren
gleichrangigen Installationen stoppt sie und zeigt die Kandidaten an. Weitere
Hintergründe stehen in [Update.md](Update.md).

```text
1) Installation / Update
2) Systemstatus anzeigen
3) Rechte prüfen & korrigieren
4) Notfallmodus / System reparieren
5) Rollback auf freigegebenes Stable-Release
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
  15) PV-Wächter (Watchdog) einrichten / reparieren
Umgebung & Python
  21) Python venv neu aufbauen (Reparatur)
  22) Python venv Namen ändern
Docker Migration & Verwaltung
  31) Zu Docker wechseln (Auto-Install & Migration)
  32) Docker auflösen & zum lokalen System zurückkehren
Erweiterungen & Smart Home
  41) Energy Manager (Wärmepumpe, Heizstab & Lademanagement)
  42) Native Wallbox Manager (E3DC, openWB, go-e)
  43) Hyundai/Kia SoC-Abfrage (Bluelink)
  44) Benachrichtigungs-Dienst einrichten
  45) Smart Home Matter Bridge
  46) openWB MQTT Integration
  47) Lokalen MQTT-Broker (Mosquitto)
  48) Smart Home MQTT-Hub (HA/ioBroker)
  49) High Availability (Cluster)
```

## Variante B: Docker Installation (Empfohlen)

Mit Docker läuft E3DC-Control komplett gekapselt. Die Community kann das fertige Production-Image laden, ohne selbst kompilieren zu müssen.

1. **Docker installieren:**
```bash
DOCKER_ARCH="$(dpkg --print-architecture)"
case "$DOCKER_ARCH" in
  arm64|amd64) ;;
  *) echo "Nicht unterstützte Docker-Architektur: $DOCKER_ARCH (benötigt: arm64 oder amd64)" >&2; exit 1 ;;
esac
sudo apt-get update
sudo apt-get install -y ca-certificates curl git
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
```

Danach einmal ab- und wieder anmelden, damit die neue Docker-Gruppenzugehörigkeit
wirksam wird. Die folgenden Quickstart-Befehle verwenden zusätzlich bewusst
`sudo` und hängen deshalb nicht von einer halb geöffneten `newgrp`-Shell ab.

2. **Mitgelieferte Compose-Datei beziehen:**

Die Compose-Datei und der sichere Host-Helfer stammen aus dem veröffentlichten
Repository. Der Anwendungscode selbst kommt weiterhin aus dem fertigen
GHCR-Image; es wird kein lokales Image gebaut.

```bash
export E3DC_DOCKER_PATH="$HOME/e3dc-docker"
git clone https://github.com/A9xxx/Install-E3DC-Control.git "$E3DC_DOCKER_PATH"
cd "$E3DC_DOCKER_PATH"
```
3. **Container aus dem GHCR-Image starten:**

```bash
sudo docker compose up -d
sudo docker compose logs --tail=80 e3dc-control
```

Ohne `E3DC_IMAGE_TAG` in `.env` zieht der Start das geprüfte Stable-Image
`latest`. Die mitgelieferte Compose-Datei enthält die persistenten Daten-,
Log-, Modell-, Prognose- und Instanzrollen-Volumes, die RAM-Disk, feste
Loggrenzen und den Watchtower-Dienst im Compose-Profil `auto-update`. Docker
ist nur für die eigenständige Rolle `ha_mode=off` freigegeben.

Damit der Knopf **System Update** in der Weboberfläche und das Auto-Update
im Config-Editor funktionieren, einmalig Watchtower freischalten (Token in
`.env`, Container mit Token neu erstellen, Watchtower starten):

```bash
printf 'E3DC_WATCHTOWER_API_TOKEN=%s\n' "$(head -c 24 /dev/urandom | od -An -tx1 | tr -d ' \n')" >> .env
sudo docker compose up -d
sudo docker compose --profile auto-update up -d watchtower
```

Wer Start und Update mit Platzprüfung, Healthcheck-Wartezeit und automatischem
Rückfall absichern will, verwendet stattdessen den Host-Helfer:

```bash
sudo python3 ./Installer/docker_compose_update.py --compose-dir . --sudo
```

Der Helfer zieht das gewählte Image ausdrücklich, bindet Image-ID und
Produktversion, wartet auf den Image-Healthcheck und verlangt zwei identische
gesunde Folgesnapshots. Scheitert ein Schritt nach dem Kandidatenstart, stoppt
er den Kandidaten wieder und bestätigt dessen Stillstand. Einen laufenden
Watchtower, der denselben Container ersetzen kann, vor dem Helferaufruf selbst
stoppen (`sudo docker compose --profile auto-update stop watchtower`).
Der Helfer stoppt Watchtower nicht selbst und bricht bei dieser Konkurrenz ab.
Nach erfolgreichem Helferlauf oder bestätigtem Rückfall den zuvor gestoppten Watchtower
mit `sudo docker compose --profile auto-update up -d watchtower` wieder starten
(bei eigenen Projekten mit denselben Projekt- und Dateiauswahlargumenten).

Danach ist das System über die IP des Docker-Hosts erreichbar. Eine frische
Konfiguration wird im Config-Editor eingerichtet.

**Docker-Updates:**

Der Knopf **System Update** in der Weboberfläche und das **Auto-Update** im
Config-Editor geben Watchtower das Signal, das neue Image zu laden und den
Container neu zu erstellen (Voraussetzung: die Freischaltung aus Schritt 3).
Der Knopf steht ab 5.5.0 zur Verfügung; das Update von 5.4.x auf 5.5.0 erfolgt
noch über den Host-Weg (siehe [Docker-Dokumentation](Docker_Dokumentation.md),
„Übergang von 5.4.x auf 5.5.0“). Er aktualisiert nur Container, deren Image aus
der Registry gezogen wurde; ein selbst gebautes Image prüft Watchtower nicht.
Auf dem Host genügen `sudo docker compose pull` und
`sudo docker compose up -d` im Compose-Ordner. Der Host-Helfer bleibt der
geprüfte Weg mit automatischem Rückfall:

Vor dem Imagewechsel den tatsächlich verwendeten Host-Updater aktualisieren,
einschließlich einer gegebenenfalls bevorzugten Kopie direkt im Compose-Ordner.
Ein neues Containerimage ersetzt diese Hostdatei nicht. Benötigte Volumes bei
gestopptem Container auf dem Host mit erhaltenen numerischen Eigentümern und
Dateirechten sichern. Container-Neuerstellungen bei beendeter Fahrzeugladung
und ohne laufenden Phasenwechsel durchführen; private Wallbox-Steuerzustände
überleben nur den Neustart desselben Containers. Die
[Docker-Dokumentation](Docker_Dokumentation.md) beschreibt die Details.

```bash
(
  set -euo pipefail
  cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
  if [ -f ./docker_compose_update.py ]; then
    E3DC_DOCKER_HELPER=./docker_compose_update.py
  elif [ -f ./Installer/docker_compose_update.py ]; then
    E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py
  else
    echo "docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen." >&2
    exit 2
  fi
  sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo
  sudo docker compose logs --tail=80 e3dc-control
)
```
Ohne `E3DC_IMAGE_TAG` holt `pull` das aktuelle geprüfte Stable-Image `latest`.
Ein fester Tag bleibt absichtlich fest; `config --images` zeigt vorab das
tatsächlich gewählte Image. Der Host-Helfer übernimmt Pull, Start, Wartephase,
Identitätsprüfung, den bestätigten Stopp eines fehlerhaften Kandidaten und den
gebundenen Rückstart des vorherigen Images als eine Transaktion. Vor dem Pull
verlangt er mindestens 2 GiB freien Platz im DockerRootDir und löscht weder
Images noch Volumes automatisch.

Fehlt der Helfer in einer älteren Installation, wird ein separater frischer
Checkout des veröffentlichten Verwaltungsbaums verwendet. Dessen
`Installer/docker_compose_update.py` erhält mit `--compose-dir` den Pfad des
bestehenden Compose-Verzeichnisses. Er migriert ausschließlich die semantisch
gebundene offizielle 5.3.2b-Compose-Datei, unveränderte offizielle
Compose-Dateien aus 5.4.2 bis 5.4.2d sowie die bekannte
Installer-Bind-Mount-Variante atomar, also ganz oder gar nicht. `.env` und die
vorhandenen Daten-, Log-, ML- und Forecast-Quellen bleiben unverändert. Einen
alten Watchtower stoppt der Helfer nicht selbst: Läuft ein Watchtower, der den
Zielcontainer parallel aktualisieren könnte, meldet er die Konkurrenz und
bricht vor Migration und Pull ab.
Ältere, angepasste, per Override ergänzte oder mehrdeutige Compose-Stände
bleiben unverändert gesperrt und benötigen eine manuelle Prüfung.

Watchtower startet nicht automatisch, weil er Zugriff auf den Docker-Socket des
Hosts benötigt; er läuft nur im Compose-Profil `auto-update` und handelt
ausschließlich auf das Signal aus E3DC-Control (kein eigenes Polling). Die
Vorlage verwendet den gepflegten Fork `ghcr.io/nicholas-fedor/watchtower:1`.
Das Containerlabel `com.centurylinklabs.watchtower.enable` steht standardmäßig
auf `true`; mit `E3DC_WATCHTOWER_ENABLE=false` in `.env` bleibt der
Hauptcontainer auch bei laufendem Watchtower ausgenommen. Einzelheiten stehen
in der [Docker-Dokumentation](Docker_Dokumentation.md), Abschnitt „Updates:
Weboberfläche, Host und Watchtower“.

**Docker-Rückfall von v5.5.5a auf den veröffentlichten Docker-Rollback-Root:**

Der aktuelle Host-Updater ist für die private Rückmigration zwingend. Aus
Bridge zuerst dieselbe aktuelle Runtime-Version im Hostprofil neu aufbauen
und den gesunden Start prüfen; erst danach den folgenden Root-Rückfall
ausführen. Den vorhandenen Runtime-Container und seine lokale Image-ID bis
zum Abschluss erhalten. Die vollständigen Sicherungs- und Rückfallschritte
stehen in der [Docker-Dokumentation](Docker_Dokumentation.md).

```bash
(
  set -euo pipefail
  cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
  sudo python3 ./Installer/docker_compose_update.py \
    --compose-dir . --sudo \
    --image-tag v5.3.2b \
    --legacy-no-healthcheck-version 5.3.2b
  sudo docker compose logs --tail=80 e3dc-control
)
```
Soll der Pin dauerhaft gelten, wird `E3DC_IMAGE_TAG=v5.3.2b` in einer
vorhandenen `.env` ergänzt, ohne andere Werte darin zu überschreiben.
Der Container kann den Docker-Daemon des Hosts absichtlich nicht selbst
bedienen. Ein Rückfall auf eine ältere Version erfolgt deshalb immer auf dem
Host; die Weboberfläche zeigt dafür nur die passenden Host-Befehle zur
gewählten Version an. `v5.3.2b` ist der einzige in der Update-Policy
vorgesehene öffentliche Docker-Rückfallstand und gibt selbst keinen älteren
Image-Tag frei; auf Bare Metal wird dieser Altstand nicht als
Programm-Rückfall angeboten. Davon getrennt ist der Wechsel zwischen
Runtime-Images, etwa von 5.5.0 zurück auf 5.4.6d: `E3DC_IMAGE_TAG=v5.4.6d`
in `.env` setzen und `sudo docker compose up -d` ausführen (siehe
[Rollback](Rollback.md)).

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

Bei einem normalen Container-Stopp beziehungsweise einer Neuerstellung über
den Host-Helfer legt E3DC-Control einen kleinen
Warmstart-Cache unter `data/docker_ramdisk_cache/` an. Dieser Cache enthält
Prognose-, Preis- und Verlaufsdaten, aber keine Steuerflags. So startet das
Dashboard nach dem Neustart schneller mit
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

Auf Bare Metal kannst Du den Installer nach der Installation über `bash "$E3DC_INSTALL_PATH/e3dc-setup"` starten und die gewünschte Wartungsoption wählen. Für Docker gelten die Host-Update- und Sicherungswege oben:

- **E3DC-Control installieren oder aktualisieren:**
  - Option `1` (Installation / Update)
  - Hält Anwendung, Webdateien, Dienste und Rechte auf dem aktuellen Stand.
  - Für den einmaligen Wechsel aus einem heterogenen Altstand die Datei
    `e3dc-update-bootstrap` kopieren und mit
    `sudo /bin/sh ./e3dc-update-bootstrap` starten; Details stehen in
    [Update.md](Update.md).

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
  `e3dc-wallbox-manager` läuft nur, wenn im Config-Editor unter „Wallbox & Fahrzeug“
  der Schalter „Wallbox-Regelung aktiv“ eingeschaltet ist.

### Fehlende Watchtower-Umgebung

Der Host-Helfer ergänzt fehlende API-Umgebung nur bei einem eindeutig zugeordneten
lokalen Watchtower und nicht leerem Token in `.env`. Vorhandene Werte bleiben
unverändert. Vor dem Schreiben entsteht eine Sicherung neben der Compose-Datei;
bei fehlgeschlagener Nachprüfung wird der Ausgangsstand wiederhergestellt.
Einrichtung, Grenzen bei externem Watchtower und mehreren Dateien sowie den
manuellen Rückweg beschreibt [Watchtower-Umgebung in eigenen Compose-Dateien](Docker_Dokumentation.md#watchtower-umgebung-in-eigenen-compose-dateien).
