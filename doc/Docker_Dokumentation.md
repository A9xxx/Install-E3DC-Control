# E3DC-Control: Docker Installation & Betrieb

Veröffentlichte Images entstehen ausschließlich aus einem versionierten stabilen Release-Tag. `latest` verweist damit auf die zuletzt veröffentlichte stabile Version.

Der aktuelle Stable-Stand ist `v5.4.2d`. `latest` wird für diesen Hotfix erst
nach erfolgreicher Digest-, SBOM-, Provenance- und Attestierungsprüfung gesetzt.

E3DC-Control kann isoliert über **Docker** betrieben werden. Der Container kapselt die Anwendung; persistente Betriebsdaten liegen in den dafür vorgesehenen Volumes. Der Multi-Architektur-Support (`arm64`, `amd64`) deckt die vorgesehenen Plattformen ab.

---

## 1. Voraussetzungen & Einrichtung

### Schritt 1: Docker installieren
Falls Docker noch nicht auf deinem Raspberry Pi, NUC oder Ubuntu-System installiert ist, richte das offizielle Docker-APT-Repository mit eigenem Keyring ein:
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
```
*(Melde dich danach einmal ab und wieder an, damit die Gruppenrechte aktiv werden)*

### Schritt 2: Verzeichnis vorbereiten
Erstelle einen Ordner für E3DC-Control. Die dauerhafte Konfiguration und
Historie liegen im Unterordner `data`; der Anwendungscode kommt im Normalfall
aus dem veröffentlichten GHCR-Image. Ein Repository-Checkout ist dafür nicht
erforderlich.
```bash
export E3DC_DOCKER_PATH="/absoluter/pfad/zur/docker-installation"
mkdir -p "$E3DC_DOCKER_PATH"
cd "$E3DC_DOCKER_PATH"
mkdir -p data logs
```
Neue Systeme werden im Browser-Wizard bzw. im Config-Editor eingerichtet und speichern nach `data/e3dc_v4.json`. Eine vorhandene `e3dc.config.txt` kannst du optional nach `data/` kopieren; sie wird beim Start als Legacy-Quelle in die aktuelle Konfiguration migriert.

### Schritt 3: Die `docker-compose.yml`
Erstelle in `$E3DC_DOCKER_PATH` eine Datei namens `docker-compose.yml` und kopiere folgenden Inhalt hinein:

```yaml
services:
  e3dc-control:
    image: "ghcr.io/a9xxx/install-e3dc-control:${E3DC_IMAGE_TAG:-latest}"
    container_name: e3dc-control
    restart: unless-stopped
    network_mode: "host"
    volumes:
      # Sichert Konfiguration, SQLite und Historie dauerhaft auf deiner Festplatte
      - ./data:/var/www/html/data
      # Persistente Logs ausserhalb des Containers
      - ./logs:/var/www/html/logs
      # Privates lokales Lernmodell
      - e3dc_ml:/var/lib/e3dc-control/ml
      # Private Rohdaten der PV-Prognosediagnose; standardmäßig bleibt sie aus
      - e3dc_forecast_evidence:/var/lib/e3dc-control/forecast-evidence
    tmpfs:
      # Schont die SD-Karte extrem, indem temporäre Dateien direkt in den RAM des Hosts geschrieben werden
      - /var/www/html/ramdisk:size=32M,uid=33,gid=33,mode=2775
    environment:
      - TZ=Europe/Berlin
      # Optional fuer Synology/NAS, wenn Port 80 vom Host belegt oder umgeleitet wird:
      # - E3DC_WEB_PORT=8085
      # Optional: Apache nur auf eine bestimmte Host-IP binden:
      # - E3DC_WEB_BIND=192.0.2.20

volumes:
  e3dc_ml:
  e3dc_forecast_evidence:
```
*(Hinweis: `network_mode: "host"` ist sinnvoll, damit die nativen Python-Dienste das E3DC Hauskraftwerk und lokale MQTT-/Wallbox-Geräte direkt erreichen und der Webserver ohne Port-Mapping erreichbar ist).*

Die vier persistenten Bereiche sind absichtlich nach Datenklasse und
Rechtevertrag getrennt:

| Bereich | Inhalt und Lebensdauer | Backup |
|---|---|---|
| `data` | Konfiguration, SQLite-Historie, Betriebszustand und sichere Docker-Warmstartdaten | immer sichern |
| `logs` | Laufzeitprotokolle sowie neu aufbaubare adaptive Auswertungsreihen | optional; Löschen setzt Support- und Auswertungshistorie zurück |
| `e3dc_ml` | root-privates lokales Lernmodell außerhalb des Webroots | empfohlen; ohne Backup ist ein neues Training aus der Historie nötig |
| `e3dc_forecast_evidence` | optionale, root-private Prognosebelege mit rollierender Aufbewahrung bis zu 90 Tagen | optional; Verlust beeinflusst die Regelung nicht, setzt aber die Diagnosehistorie zurück |

Die Compose-Datei im vollständigen Repository verwendet für alle vier Bereiche
benannte Volumes. Das obige Beispiel und der automatische Docker-Umstieg
verwenden für `data` und `logs` besser sichtbare Bind-Mounts sowie für die
beiden privaten Bereiche benannte Volumes. Beide Layouts bilden denselben
fachlichen Vertrag ab. Die Ramdisk ist absichtlich flüchtig und gehört nicht
ins Backup.

Die aktuellen Dateien `pv_forecast.json` und `ml_prediction.json` sind
flüchtige Rechenergebnisse in der Ramdisk und werden neu erzeugt. Das Volume
`e3dc_forecast_evidence` enthält dagegen nicht die laufende Prognose, sondern
das optionale Diagnosearchiv mit einer rollierenden Aufbewahrung bis zu 90
Tagen.

`e3dc_ml` darf nicht einfach unter das heutige Web-`data` verschoben werden:
Der Datenbaum gehört dem Webbenutzer, während das verifizierte, serialisierte
Modell und die privaten Prognosebelege aus Sicherheitsgründen root-privat
bleiben. Eine Reduktion auf nur `data` und `logs` würde unterschiedliche
Eigentümer-, Sicherheits-, Aufbewahrungs- und Backupverträge vermischen und
wird deshalb nicht unterstützt. Eine spätere Zusammenlegung privater Volumes
benötigt eine verifizierte Datenmigration samt Rechteprüfung und Rückfallweg.

**Synology / NAS mit belegtem Port 80:**
Wenn der Host Port 80 selbst abfaengt oder auf die NAS-GUI umleitet, kannst du
den Apache-Port im Container per ENV setzen. Mit `network_mode: "host"` gibt es
kein Docker-`ports:`-Mapping; der Container bindet direkt auf dem Host-Port.

```yaml
environment:
  - TZ=Europe/Berlin
  - E3DC_WEB_PORT=8085
  # Optional, wenn nur eine bestimmte Host-IP genutzt werden soll:
  # - E3DC_WEB_BIND=192.0.2.20
```

Danach erreichst du E3DC-Control z.B. unter
`http://<NAS-IP>:8085/`. Der Synology Reverse Proxy kann dann auf diesen Port
weiterleiten. `E3DC_WEB_BIND` erwartet eine IP-Adresse der gewuenschten
Schnittstelle, keinen Interface-Namen wie `eth0`.

**Wichtig bei bestehenden Images:**
`docker compose up -d` baut ein vorhandenes Image nicht automatisch neu und
zieht auch nicht zwingend die neueste Version. Wenn `E3DC_WEB_PORT` im Container
sichtbar ist, Apache aber trotzdem weiter auf `0.0.0.0:80` hoert, laeuft sehr
wahrscheinlich noch ein altes Image oder ein alter Container.

Fertiges GitHub-Image aktualisieren:

```bash
(
  set -euo pipefail
  cd "$E3DC_DOCKER_PATH"
  sudo docker compose config --images
  sudo docker compose pull e3dc-control
  sudo docker compose up -d --force-recreate e3dc-control
)
```

Ohne `E3DC_IMAGE_TAG` folgt diese Compose-Datei dem geprüften Stable-Tag
`latest`. Ein fester Tag bleibt bei `pull` absichtlich unverändert. Für einen
bewussten Pin wird zum Beispiel `E3DC_IMAGE_TAG=v5.4.2d` in der Datei `.env`
gesetzt. `docker compose config --images` zeigt vorab das tatsächlich gewählte
Image.

Ältere Installationen können in ihrer `docker-compose.yml` noch einen Tag
direkt in der `image:`-Zeile enthalten, zum Beispiel `v5.3.2b` oder
`v5.4.0a`. Bei diesen Dateien hat `E3DC_IMAGE_TAG` noch keine Wirkung. Stelle
die Zeile deshalb einmalig auf die variable Form um:

```yaml
image: "ghcr.io/a9xxx/install-e3dc-control:${E3DC_IMAGE_TAG:-latest}"
```

Sichere die vorhandene Compose-Datei vorher und prüfe anschließend die
tatsächlich aufgelöste Image-Adresse:

```bash
cp -a docker-compose.yml docker-compose.yml.before-image-variable
sudo docker compose config --images
```

Erst wenn dort der gewünschte Tag erscheint, folgen `pull` und
`up -d --force-recreate`. Ein eventuell bereits vorhandener Eintrag
`E3DC_IMAGE_TAG=...` in `.env` bleibt dabei die maßgebliche bewusste
Versionswahl.

Gezielte Rückfallversion:

Den Stable-Container `v5.4.2d` auf den veröffentlichten Rollback-Root
`v5.3.2b` zurücksetzen:

```bash
(
  set -euo pipefail
  TAG=v5.3.2b
  cd "$E3DC_DOCKER_PATH"
  EXPECTED_IMAGE="ghcr.io/a9xxx/install-e3dc-control:$TAG"
  BACKUP_FILE="$PWD/e3dc-data-$(date +%Y%m%d-%H%M%S).tgz"
  sudo docker compose exec -T e3dc-control \
    tar czf - -C /var/www/html/data . > "$BACKUP_FILE"
  test -s "$BACKUP_FILE"
  RESOLVED_IMAGE="$(sudo env E3DC_IMAGE_TAG="$TAG" docker compose config --images e3dc-control)"
  [ "$RESOLVED_IMAGE" = "$EXPECTED_IMAGE" ]
  sudo env E3DC_IMAGE_TAG="$TAG" docker compose pull e3dc-control
  sudo env E3DC_IMAGE_TAG="$TAG" docker compose up -d --force-recreate e3dc-control
  sudo docker compose ps
  sudo docker logs --tail=80 e3dc-control
)
```

Der Rückfall ist bewusst ein Host-Befehl: Der E3DC-Control-Container soll nicht
den Docker-Daemon des Hosts steuern. Die Weboberfläche kann deshalb die
passenden Befehle für den gewählten Tag anzeigen, aber sie führt sie im
Docker-Betrieb nicht selbst aus.

Für einen dauerhaft festgehaltenen Tag wird derselbe Wert zusätzlich als
`E3DC_IMAGE_TAG=v5.3.2b` in einer vorhandenen `.env` ergänzt, ohne andere dort
gespeicherte Werte zu überschreiben.

Der Stand `v5.3.2b` ist selbst der Rollback-Root und verweist auf kein älteres
öffentliches Image. Die Befehle sind daher für den Rückfall von einem späteren
Stable-Image auf diesen Stand vorgesehen.

### Optionaler Entwickler-Selbstbau

Für normale Installationen bleibt das veröffentlichte GHCR-Image der
vorgesehene Weg. Ein lokaler Build benötigt den **vollständigen
Repository-Checkout**, weil `Dockerfile`, `entrypoint.sh` und der Quellbaum im
Build-Kontext liegen müssen.

Docker Compose ist nicht auf Images aus einer Registry beschränkt. Es kann ein
lokal gebautes Image direkt aus dem Docker-Daemon verwenden. Entscheidend ist,
dass der unter `image:` eingetragene Name exakt dem lokalen Tag entspricht.
Die normale Release-Compose bleibt dabei unverändert auf GHCR gebunden; der
lokale Image-Name wird über eine kleine Override-Datei gesetzt.

```bash
export E3DC_REPO_PATH="/absoluter/pfad/zum/repository-checkout"
git clone https://github.com/A9xxx/Install-E3DC-Control.git "$E3DC_REPO_PATH"
cd "$E3DC_REPO_PATH"
sudo docker build --pull -t e3dc-control:local .
```

Lege daneben eine Datei `docker-compose.local.yml` an:

```yaml
services:
  e3dc-control:
    image: "e3dc-control:local"
    pull_policy: never
```

Anschließend wird die normale Compose-Datei mit diesem lokalen Override
gestartet:

```bash
sudo docker compose \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  config --images
sudo docker compose \
  -f docker-compose.yml \
  -f docker-compose.local.yml \
  up -d --force-recreate e3dc-control
```

`config --images` muss `e3dc-control:local` ausgeben.
`pull_policy: never` verhindert für diesen Entwicklerweg jeden Registry-Pull
und meldet einen Fehler, wenn das lokale Image fehlt. Soll das selbst gebaute
Image auf weiteren Hosts laufen, erhält es stattdessen einen vollständigen
privaten Registry-Namen, wird dorthin gepusht und unter demselben Namen in
`image:` eingetragen.

Ein bloßes `build: .` in einem ansonsten leeren Installationsordner ist
weiterhin unvollständig und wird nicht unterstützt. Der vollständige
Repository-Checkout muss der Build-Kontext sein.

Nach einem bewussten lokalen Build ist die Ausgabe von
`docker inspect e3dc-control --format
'Image={{.Image}} Created={{.Created}}'` ein guter Plausibilitätscheck: Das
`Created`-Datum muss zum gerade ausgeführten Build passen. Für normale
Installationen bleibt das fertige GitHub-Image der bequemere Weg. Ein lokaler
Build ist für Plattformen oder Anpassungen gedacht, die nicht vom fertigen
Image abgedeckt werden.

In der Ausgabe von `docker compose config` ist `volume: {}` bei benannten
Volumes normal. Docker verwaltet deren echten Hostpfad. Das gilt insbesondere
für `e3dc_ml` und `e3dc_forecast_evidence`; beide privaten Datenklassen bleiben
dadurch vom Webverzeichnis getrennt. Den Pfad siehst du bei Bedarf mit
`docker volume inspect <Compose-Projekt>_e3dc_forecast_evidence`.

Pruefen, ob der neue Entrypoint aktiv ist:

```bash
sudo docker exec e3dc-control sh -lc '
echo PORT=$E3DC_WEB_PORT BIND=$E3DC_WEB_BIND
grep -n "configure_apache_web_port" /usr/local/bin/entrypoint.sh /app/pi/Install/entrypoint.sh 2>/dev/null
grep -R "^[[:space:]]*Listen[[:space:]]" /etc/apache2/ports.conf /etc/apache2/conf-enabled /etc/apache2/sites-enabled 2>/dev/null
apache2ctl -S
'
```

Erwartung bei `E3DC_WEB_PORT=8085` und `E3DC_WEB_BIND=192.0.2.222`:

```text
Listen 192.0.2.222:8085
VirtualHost configuration:
192.0.2.222:8085 ...
```

Wenn das Dashboard danach erreichbar ist, aber oben `Warte auf E3DC...` zeigt,
ist der Webserver repariert und der naechste Schritt ist RSCP: Konfiguration
speichern, Container neu starten und dann Port/Anmeldung pruefen.

```bash
sudo docker compose restart e3dc-control
sudo docker exec e3dc-control sh -lc '
python3 - <<PY
import json, socket
cfg=json.load(open("/var/www/html/data/e3dc_v4.json"))
print("server_ip=", cfg.get("server_ip"))
print("server_port=", cfg.get("server_port", 5033))
print("e3dc_user gesetzt=", bool(cfg.get("e3dc_user")))
s=socket.create_connection((cfg["server_ip"], int(cfg.get("server_port") or 5033)), 5)
print("TCP 5033 OK")
s.close()
PY
/opt/venv/bin/python3 /app/pi/Install/Installer/e3dc_live.py --loops 1
'
```

Kommt `TCP 5033 OK`, aber `e3dc_live.py` liefert keine Daten, sind meist
Benutzer, Passwort oder AES-Passwort zu pruefen. Scheitert schon TCP, liegt es
an IP, Netzwerk, VLAN, Firewall oder daran, dass RSCP am E3DC nicht erreichbar
ist.

Wenn du nur den Port aendern willst, ist es oft robuster, `E3DC_WEB_BIND`
wegzulassen. Dann hoert Apache auf allen Host-IP-Adressen auf dem gewaehlten
Port, z.B. `Listen 8085`.

### Schritt 4: Starten
Starte den Container im Hintergrund:
```bash
sudo docker compose up -d
```
Das System lädt nun das fertige Image aus dem Internet herunter, richtet den Webserver, die RAM-Disk und alle Python-Skripte vollautomatisch ein. Du erreichst dein Dashboard sofort über die IP-Adresse deines Raspberry Pi im Browser.

---

## 2. Architektur & Unterschiede zur normalen Installation

Wenn E3DC-Control in Docker läuft, verhält es sich intern etwas anders als bei einer "Bare-Metal" Installation auf dem Raspberry Pi.

* **Keine Cronjobs:** Docker-Container haben von Haus aus keinen Aufgabenplaner (Cron). Diese Aufgabe übernimmt vollautomatisch der in Python geschriebene **Schedule & Notification Manager** (`notification_manager.py`). Er läuft im Hintergrund und triggert die Minutenspeicherung, Backups und Telegram-Nachrichten.
* **Web-Updates deaktiviert:** Da ein Container „immutable" (unveränderlich) ist, ist der „Update"-Knopf im Web-Dashboard deaktiviert. Klickst du darauf, informiert dich das System, dass Updates über Docker bezogen werden müssen.
* **Kein Systemd:** Befehle wie `systemctl restart e3dc` funktionieren im Container nicht. Wenn du den Dienst neu starten möchtest, startest du einfach den gesamten Container neu (siehe unten). Logs der Python-Dienste findest du unter `/var/www/html/logs/`.
* **Auto-Start:** Du benötigst keine Watchdogs oder Crontab-Einträge mehr, damit E3DC nach einem Stromausfall hochfährt. Der Parameter `restart: unless-stopped` in der `docker-compose.yml` sorgt dafür, dass Docker das System immer am Leben hält.

### Aktive Hintergrunddienste im Container

Folgende Python-Dienste starten automatisch über die `entrypoint.sh`. Alle Logs liegen unter `/var/www/html/logs/`:

| Dienst | Startbedingung | Log-Datei |
|--------|----------------|-----------|
| `e3dc_websocket.py` | Immer | `e3dc_websocket.log` |
| `energy_manager.py` | Automatik/Verbrauchslogging im Frontend aktiv | `energy_manager.log` |
| `lux_live.py` | Wärmepumpen Typ: Luxtronik | `lux_live.log` |
| `idm_live.py` | Wärmepumpen Typ: IDM und IDM-IP gesetzt | `idm_live.log` |
| `stiebel/stiebel_live.py` | Wärmepumpen Typ: Stiebel Eltron ISG / WPM und ISG-IP gesetzt | `stiebel_live.log` |
| `heizstab_manager.py` | Heizstab/Shelly als Waermequelle konfiguriert | `heizstab_manager.log` |
| `wallbox_manager.py` | `wb_native_enable=1` | `wallbox_manager.log` |
| `e3dc_mqtt_hub.py` | `mqtt_hub_ip=...` | `e3dc_mqtt_hub.log` |
| `bluelink_client.py` | `bluelink_refresh_token=...` | `bluelink_client.log` |
| `epex_manager.py` | Immer | `epex_manager.log` |
| `Forecast/pv_forecast_service.py` | Immer | `weather_manager.log` |
| `storage_simulator.py` | Immer | `storage_simulator.log` |
| `e3dc_live.py` | Immer | `e3dc_live.log` |
| `notification_manager.py` | Immer | `notification_manager.log` |

Fuer Stiebel Eltron ISG/WPM reicht im Docker-Betrieb nach dem Update auf ein
Image ab `5.0.4g`: Im Config-Editor unter **Smart Home &
Verbrauchsprognose** den Schalter **WP-/Verbrauchslogging aktivieren**
einschalten, bei **Wärmepumpen Typ** **Stiebel Eltron ISG / WPM** waehlen,
die **ISG IP-Adresse** eintragen und den Container neu starten. Der
Live-Treiber ist read-only; ein optionaler Shelly-Leistungsmesser wird
ebenfalls nur gelesen und schaltet kein Relais. Der Dienst startet automatisch
aus der `entrypoint.sh`.

> **Wichtig:** Wenn du ein Feature **nachträglich** im Config-Editor aktivierst (z.B. Wallbox Manager, Heizstab), muss der Container **einmal neugestartet** werden — erst dann wertet die `entrypoint.sh` die neue Einstellung aus und startet den Dienst!

---

### MQTT-Hub im Docker starten

Im Docker wird kein `systemctl` verwendet. Der MQTT-Hub ist daher nicht als
klassischer Linux-Dienst zu installieren. Er startet automatisch, sobald in der
E3DC-Control-Konfiguration ein MQTT-Broker eingetragen ist.

Vorgehen:

1. Im Config-Editor den Bereich **Smart Home MQTT-Hub** oeffnen.
2. Broker-IP/Host (`mqtt_hub_ip`) und bei Bedarf Port, Benutzer, Passwort und
   Topics eintragen.
3. Konfiguration speichern.
4. Den Container einmal neu starten:

```bash
cd "$E3DC_DOCKER_PATH"
sudo docker compose restart e3dc-control
```

Beim naechsten Start meldet der Container im Log:

```text
-> MQTT Hub aktiv.
```

Pruefen kannst du den Dienst so:

```bash
sudo docker logs e3dc-control | grep "MQTT Hub"
sudo docker exec e3dc-control tail -50 /var/www/html/logs/e3dc_mqtt_hub.log
```

Wenn der MQTT Explorer bereits Werte im Broker zeigt, ist der Broker erreichbar.
E3DC-Control liest diese Werte aber erst ein, wenn der MQTT-Hub im Container
laeuft und die passenden Subscribe-Topics in der Config gesetzt sind. Der Hub
uebernimmt nicht automatisch alle MQTT-Werte aus dem Broker.

---

## 3. Updates und optionaler Watchtower

Der sichere Standardweg bleibt bewusst auf dem Docker-Host:
```bash
(
  set -euo pipefail
  cd "$E3DC_DOCKER_PATH"
  sudo docker compose config --images
  sudo docker compose pull e3dc-control
  sudo docker compose up -d --force-recreate e3dc-control
  sudo docker inspect e3dc-control --format '{{.Config.Image}} {{.State.Status}}'
  sudo docker exec e3dc-control cat /app/pi/Install/VERSION
)
```
`docker compose pull` aktualisiert Python/PHP-Code, Container-Startskript und
Systempakete nur innerhalb des von `config --images` angezeigten Tags.
`--force-recreate` stellt sicher, dass der Container wirklich aus dem neuen
Image startet.

Schlägt `pull` fehl, ist der Updateversuch beendet. Ein bereits vorhandenes
Altimage darf danach weder automatisch neu gestartet noch als aktualisierte
Version gemeldet werden. Die Image-Referenz aus `config --images`, der
laufende Containerstatus und die `VERSION` im Container müssen gemeinsam
zum erwarteten Release passen.

Für den Rückfall eines späteren Stable-Images wird `E3DC_IMAGE_TAG` einmalig
vor die drei Compose-Befehle gesetzt oder für einen dauerhaften Pin in `.env`
eingetragen. Die `docker-compose.yml` selbst muss dafür nicht verändert werden.
Der einzige vorgesehene öffentliche Rollback-Root ist
`ghcr.io/a9xxx/install-e3dc-control:v5.3.2b`; dieser Stand selbst verweist auf
kein älteres Image. Danach dieselben Pull-/Up-Befehle ausführen. Das
funktioniert nur für tatsächlich veröffentlichte, verifizierte GHCR-Images.

Watchtower ist nur noch ein ausdrückliches Opt-in. Das Upstream-Projekt wird
nicht mehr gepflegt. Der Dienst liegt für bestehende Installationen im
Compose-Profil `auto-update` und startet bei einem normalen
`docker compose up -d` nicht. Sein notwendiger Zugriff auf
`/var/run/docker.sock` gibt dem Container weitreichende Kontrolle über den
Docker-Host. Wer diese Risiken bewusst akzeptiert, aktiviert das Profil:

```bash
cd "$E3DC_DOCKER_PATH"
sudo docker compose --profile auto-update up -d watchtower
```

Der Enable-Label-Filter begrenzt Watchtower dabei auf E3DC-Control; andere
Container des Hosts werden nicht automatisch aktualisiert. Ein bereits aus
einer älteren Compose-Datei laufender Watchtower wird mit
`sudo docker compose --profile auto-update stop watchtower` und danach
`sudo docker compose --profile auto-update rm -f watchtower` deaktiviert.
Eine Wartungsfreiheit wird auch im Opt-in-Betrieb nicht zugesichert.

---

## 4. Befehle & Fehlerbehebung (Troubleshooting)

Da du nicht mehr klassisch über die Linux-Konsole auf E3DC zugreifst, nutzt du nun Docker-Befehle, um das System zu steuern:

**Dashboard im Terminal ansehen (E3DC Screen):**
*Da es keinen klassischen Linux `screen` mehr gibt, schaust du dir einfach den Live-Output des Containers an:*
```bash
sudo docker logs -f e3dc-control
```
*(Mit `Strg+C` beendest du die Ansicht, der Container läuft im Hintergrund weiter).*

**Einzelne Dienst-Logs ansehen:**
```bash
sudo docker exec e3dc-control tail -f /var/www/html/logs/wallbox_manager.log
sudo docker exec e3dc-control tail -f /var/www/html/logs/e3dc_live.log
```

**E3DC-Control komplett neu starten (inkl. Webserver und Diensten):**
```bash
cd "$E3DC_DOCKER_PATH"
sudo docker compose restart
```

**ML-Prognose fehlt (`ml_prediction.json` nicht vorhanden):**
`/var/www/html/ramdisk` ist im Docker absichtlich ein `tmpfs`. Nach jedem
Container-Neustart oder `--force-recreate` ist dieser Ordner leer und wird von
den Diensten neu befuellt. Die Datei `ml_prediction.json` ist kein persistenter
Bestandteil der Installation, sondern das temporaere Ergebnis von
`ml_predictor.py --predict`.

Der private Modellstore `/var/lib/e3dc-control/ml` wird ebenfalls nicht im Docker-Image
mitgeliefert. Er enthält ein lokales Lernmodell und wird erst aus den eigenen
Verlaufsdaten des jeweiligen Systems erzeugt. Bei einer frischen Installation
oder nach einem Wechsel von einer alten Host-Installation in ein neues Docker-
Volume kann das Training zunaechst melden:

```text
Nicht genug Trainingsdaten: 0 Datensaetze (benoetigt: 50).
```

Das ist kein Docker- oder Rechtefehler. Es bedeutet nur, dass im neuen
persistent gemounteten `data`-Bereich noch keine verwertbare Historie fuer das
ML-Modell vorhanden ist. Der Storage Simulator nutzt in dieser Zeit automatisch
den konservativen Verbrauchs-Fallback.

Ab den neueren Images sichert der Entrypoint beim normalen Container-Stopp
wichtige Warmstartdaten aus der Ramdisk nach
`/var/www/html/data/docker_ramdisk_cache/` und spielt sie beim naechsten Start
wieder ein. Gesichert werden nur unkritische Prognose-, Preis- und
Verlaufsdaten, keine Steuerflags und keine Live-Schaltzustaende. Dadurch bleibt
das Dashboard nach einem Rebuild schneller plausibel, während die Dienste die
Daten anschließend frisch nachrechnen.

Wichtig ist das persistente, separate Modell-Volume unter `/var/lib/e3dc-control/ml`.
Wenn `ml_predictor.py --predict` meldet `Kein Modell gefunden`, liegt das nicht
an `uid=33,gid=33` der Ramdisk, sondern daran, dass noch kein Modell trainiert
wurde oder das separate Modell-Volume nicht korrekt eingebunden ist. Ein altes
Web-Pickle unter `/var/www/html/data` wird nicht geladen oder übernommen.

Die PV-Prognosediagnose ist davon getrennt und standardmäßig ausgeschaltet.
Erst `forecast_diagnostics_enable=1` startet beim Containerstart den
niedrig priorisierten, rein lesenden Diagnosedienst. Seine Rohdatenbank liegt mit
0700-Verzeichnisrechten im separaten Volume
`/var/lib/e3dc-control/forecast-evidence`; das Webportal erhält nur eine kleine,
sanitierte Zusammenfassung in der Ramdisk. Nach dem Ein- oder Ausschalten ist
ein Containerneustart erforderlich. Bei `Aus` erfolgen weder
E3/DC-Historienabfrage noch Datenbankschreibzugriff.

Beim Wechsel zwischen Bare Metal und Docker wird diese private
Diagnosedatenbank bewusst nicht kopiert. Die Auswertung beginnt im jeweiligen
Laufzeitmodell mit einer neuen Vergleichshistorie; die Regelung ist davon nicht
betroffen. Das alte benannte Docker-Volume wird bei `docker compose down` ohne
`--volumes` nicht gelöscht.

Pruefung im Container:
```bash
sudo docker exec -it e3dc-control bash
ls -ld /var/www/html/ramdisk
ls -la /var/lib/e3dc-control/ml
/opt/venv/bin/python3 /app/pi/Install/Installer/ml_predictor.py --train
/opt/venv/bin/python3 /app/pi/Install/Installer/ml_predictor.py --model-ready
/opt/venv/bin/python3 /app/pi/Install/Installer/ml_predictor.py --predict
ls -l /var/www/html/ramdisk/ml_prediction.json
```

Kommt beim Training `Nicht genug Trainingsdaten`, ist das bei neuen Systemen
normal. Der Storage Simulator nutzt dann automatisch den konservativen
Fallback, bis genug Historie vorhanden ist.

Wenn du pruefen moechtest, ob ueberhaupt neue Historie entsteht:

```bash
sudo docker exec e3dc-control ls -l /var/www/html/ramdisk/live_history.txt
sudo docker exec e3dc-control tail -5 /var/www/html/ramdisk/live_history.txt
sudo docker exec e3dc-control ls -l /var/www/html/data
```

Wachsen dort Live- oder Verlaufsdaten, einfach weiterlaufen lassen. Bleibt das
Training nach mehreren Tagen weiter bei `0 Datensaetze`, pruefe zuerst, ob dein
Docker-Volume bzw. Host-Mount fuer `/var/www/html/data` wirklich dauerhaft
erhalten bleibt.
