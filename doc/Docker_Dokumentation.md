# E3DC-Control: Docker Installation & Betrieb

Veröffentlichte Images entstehen ausschließlich aus einem versionierten stabilen Release-Tag. `latest` verweist damit auf die zuletzt veröffentlichte stabile Version.

Der aktuelle Stable-Stand ist `v5.4.3h`. Die Tags `latest`, `v5.4.3h` und
`5.4.3h` müssen auf denselben geprüften Multi-Arch-Digest verweisen.

E3DC-Control kann isoliert über **Docker** betrieben werden. Der Container kapselt die Anwendung; persistente Betriebsdaten liegen in den dafür vorgesehenen Volumes. Der Multi-Architektur-Support (`arm64`, `amd64`) deckt die vorgesehenen Plattformen ab. Docker benötigt dabei zwingend ein 64-Bit-Betriebssystem; `armhf`, `arm/v7` und andere 32-Bit-Installationen können das veröffentlichte Image nicht starten.

Bei aktivierter Matter-Bridge startet der Container zuerst D-Bus und den
Bookworm-Avahi-Daemon, überwacht dessen Prozess und wartet begrenzt auf den
mDNS-Bereitschaftsnachweis. Erst danach wird die Matter-Bridge gestartet; ein
fehlender Discovery-Dienst lässt den Container absichtlich ungesund enden.

Docker ist ausschließlich für eine eigenständige Instanz mit exakt
`ha_mode=off` freigegeben. HA-Master/-Slave und die read-only Shadow-Instanz
bleiben Bare-Metal-Betriebsarten; der Container bricht bei einer abweichenden
Konfiguration vor dem ersten Hardware-Writer ab. Beim ersten Start wird ein
persistenter Instanzrollenanker create-once auf exakt `off` projiziert; ein
späterer Widerspruch zwischen Anker und Konfiguration ist ebenfalls ein harter
Startabbruch. Für den Wechsel einer bereits
laufenden nativen Installation ist im Installer Menüpunkt **31 „Zu Docker
wechseln“** vorgesehen: Er stoppt zuerst Supervisoren und historische Dienste,
deaktiviert alle Produkt-Units und verlangt zwei stabile inaktive systemd- und
`/proc`-Snapshots. Manuell gestartete Hardware-Writer und Legacy-Screens
blockieren die Migration; der Installer beendet sie bewusst nicht. Erst danach
startet Compose. Menüpunkt 31 ist ausschließlich die erste
Bare-Metal-zu-Docker-Migration: Ein vorhandener E3DC-Container, eine vorhandene
Compose-Datei oder bereits verwaltete E3DC-Docker-Daten stoppen den Assistenten
vor der ersten Änderung. Ein bei diesem Erstversuch neu erzeugter Ziel- und
Compose-Baum wird bei einem Fehler nach verifiziertem Kandidatenstillstand auf
den zuvor fehlenden beziehungsweise leeren Zustand zurückgesetzt, sodass der
Assistent sauber wiederholt werden kann. Bestehende Docker-Installationen werden nur über den
unten dokumentierten Compose-Updateweg gepflegt. Die folgende manuelle Einrichtung ist für einen
frischen oder bereits ausschließlich containerisierten Host gedacht. Sie darf
nicht parallel zu einer nativen E3DC-Control-Installation gestartet werden.

---

## 1. Voraussetzungen & Einrichtung

### Schritt 1: Docker installieren
Prüfe zuerst die Paketarchitektur. Nur `amd64` und `arm64` sind freigegeben:

```bash
case "$(dpkg --print-architecture)" in
  amd64|arm64) ;;
  *) echo "E3DC-Control Docker benötigt ein 64-Bit-System mit amd64 oder arm64." >&2; exit 1 ;;
esac
```

Auf einem Raspberry Pi mit `armhf` oder `arm/v7` installierst du zuerst Raspberry Pi OS Bookworm 64-Bit neu. Falls Docker noch nicht auf deinem Raspberry Pi, NUC oder Ubuntu-System installiert ist, richte anschließend das offizielle Docker-APT-Repository mit eigenem Keyring ein:
```bash
if ! command -v docker >/dev/null 2>&1; then
  test ! -S /run/docker.sock
  if sudo test -e /var/lib/docker; then
    test -z "$(sudo find -P /var/lib/docker -mindepth 1 -maxdepth 1 -print -quit)" || {
      echo "Unbekannter Bestand unter /var/lib/docker; Installation abgebrochen." >&2
      exit 1
    }
  fi
fi
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
# Die Paketinstallation kann den Daemon bereits gestartet haben. Fremde
# Container und Volumes bleiben unberührt; nur bestehender E3DC-Control-Bestand
# blockiert die frische Installation und gehört in den Updateweg.
E3DC_CONTAINER_CONFLICTS="$(
  sudo docker container ls -a \
    --format '{{.Names}}|{{.Image}}|{{.Label "com.docker.compose.service"}}' |
  awk -F '|' '$1 == "e3dc-control" || $2 ~ /(^|\/)(install-)?e3dc-control([:@]|$)/ || $3 == "e3dc-control" {print $1 " (" $2 ")"}'
)"
test -z "$E3DC_CONTAINER_CONFLICTS" || {
  echo "E3DC-Control-Container besteht bereits: $E3DC_CONTAINER_CONFLICTS" >&2
  exit 1
}
E3DC_VOLUME_CONFLICTS="$(
  sudo docker volume ls --format '{{.Name}}' |
  awk '$0 ~ /(^|_)(e3dc_data|e3dc_logs|e3dc_ml|e3dc_forecast_evidence|e3dc_instance_role)$/'
)"
test -z "$E3DC_VOLUME_CONFLICTS" || {
  echo "Verwalteter E3DC-Docker-Datenbestand besteht bereits: $E3DC_VOLUME_CONFLICTS" >&2
  exit 1
}
sudo usermod -aG docker "$USER"
```
*(Melde dich danach einmal ab und wieder an, damit die Gruppenrechte aktiv werden)*

### Schritt 2: Verzeichnis vorbereiten
Der Repository-Checkout liefert die kanonische Compose-Datei und den
fail-closed Host-Updater. Der Anwendungscode selbst kommt im Normalfall
weiterhin aus dem veröffentlichten GHCR-Image.
```bash
export E3DC_DOCKER_PATH="/absoluter/pfad/zur/docker-installation"
git clone https://github.com/A9xxx/Install-E3DC-Control.git "$E3DC_DOCKER_PATH"
cd "$E3DC_DOCKER_PATH"
test -f ./docker-compose.yml
test -f ./Installer/docker_compose_update.py
```
Neue Systeme werden im Config-Editor eingerichtet. Fehlt bei einer
Bestandsanlage die V4-Konfiguration, bleibt die Weboberfläche fail-closed;
stelle dann zuerst ein geprüftes Backup administrativ wieder her.

### Schritt 3: Mitgelieferte `docker-compose.yml` verwenden

Die geklonte `docker-compose.yml` ist der kanonische Installationsweg. Lege sie
nicht noch einmal von Hand an und überschreibe sie nicht mit einem abweichenden
Beispiel. Prüfe vor dem ersten Start nur das aufgelöste Image:

```bash
sudo docker compose config --images e3dc-control
```

Ohne bewussten Pin muss genau
`ghcr.io/a9xxx/install-e3dc-control:latest` erscheinen. `network_mode: host`
ermöglicht den Zugriff auf das E3/DC-Hauskraftwerk und lokale
MQTT-/Wallbox-Geräte, ohne ein separates Port-Mapping anzulegen.

Das Docker-Engine-Log des Hauptcontainers ist damit auf drei Dateien zu je
10 MiB begrenzt. Diese Grenze gilt zusätzlich zu den getrennt persistierten
Produktlogs im Volume `e3dc_logs`. Im Image läuft dafür ein eigener, vom Healthcheck
überwachter Logrotate-Taktgeber: Er rotiert Produkt- und Apache-Logs ab 10 MiB,
hält sieben Generationen und beendet den Container bei einem Rotationsfehler
ungleich null. Fachliche Historien und Backups gehören nicht zu dieser Rotation.

Die fünf persistenten Bereiche sind absichtlich nach Datenklasse und
Rechtevertrag getrennt:

| Bereich | Inhalt und Lebensdauer | Backup |
|---|---|---|
| `e3dc_data` | Konfiguration, SQLite-Historie, Betriebszustand und sichere Docker-Warmstartdaten | immer sichern |
| `e3dc_logs` | Laufzeitprotokolle sowie neu aufbaubare adaptive Auswertungsreihen | optional; Löschen setzt Support- und Auswertungshistorie zurück |
| `e3dc_ml` | root-privates lokales Lernmodell außerhalb des Webroots | empfohlen; ohne Backup ist ein neues Training aus der Historie nötig |
| `e3dc_forecast_evidence` | optionale, root-private Prognosebelege mit rollierender Aufbewahrung bis zu 90 Tagen | optional; Verlust beeinflusst die Regelung nicht, setzt aber die Diagnosehistorie zurück |
| `e3dc_instance_role` | root-privater create-once-Anker für exakt `ha_mode=off`; überlebt Container-Recreates | auf demselben Docker-Host erhalten; nicht als Rollenanker auf einen anderen Host kopieren |

Die mitgelieferte Compose-Datei verwendet für alle fünf Bereiche benannte
Volumes. Ein abweichendes Bind-Mount-Layout ist kein Teil des manuellen
Quickstarts und darf die ausgelieferte Compose-Datei nicht ungeprüft ersetzen.
Die Ramdisk ist absichtlich flüchtig und gehört nicht ins Backup.

Die aktuellen Dateien `pv_forecast.json` und `ml_prediction.json` sind
flüchtige Rechenergebnisse in der Ramdisk und werden neu erzeugt. Das Volume
`e3dc_forecast_evidence` enthält dagegen nicht die laufende Prognose, sondern
das optionale Diagnosearchiv mit einer rollierenden Aufbewahrung bis zu 90
Tagen.

`e3dc_ml` darf nicht einfach unter das heutige Web-`data` verschoben werden:
Der Datenbaum gehört dem Webbenutzer, während das verifizierte, serialisierte
Modell und die privaten Prognosebelege aus Sicherheitsgründen root-privat
bleiben. Eine Reduktion auf nur `e3dc_data` und `e3dc_logs` würde unterschiedliche
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
`docker compose up -d --wait --wait-timeout 300 e3dc-control` baut ein vorhandenes Image nicht automatisch neu und
zieht auch nicht zwingend die neueste Version. Wenn `E3DC_WEB_PORT` im Container
sichtbar ist, Apache aber trotzdem weiter auf `0.0.0.0:80` hoert, laeuft sehr
wahrscheinlich noch ein altes Image oder ein alter Container.

Fertiges GitHub-Image aktualisieren:

```bash
cd "$E3DC_DOCKER_PATH"
sudo python3 ./Installer/docker_compose_update.py --compose-dir . --sudo
sudo docker compose logs --tail=80 e3dc-control
```

Der Helfer zieht ausdrücklich das von Compose projizierte Image, bindet dessen
sha256-ID und OCI-Version vor dem Start und vergleicht damit die laufende
`VERSION`. Start-, Warte-, Health-, Snapshot- oder Versionsfehler führen immer
zu einem bestätigten Stopp des Kandidaten.

Fehlt `docker_compose_update.py` in einer älteren Docker-Installation, verwende
einen separaten frischen Checkout des veröffentlichten `main` als
Verwaltungsbaum. Starte daraus `Installer/docker_compose_update.py` und übergib
mit `--compose-dir` den absoluten Pfad des bestehenden
`e3dc-docker`-Verzeichnisses. Der Helfer migriert ausschließlich unveränderte
offizielle Compose-Dateien aus 5.4.2 bis 5.4.2d sowie die bekannte
Installer-Bind-Mount-Variante atomar, also ganz oder gar nicht. `.env` und die
vorhandenen Daten-, Log-, ML- und Forecast-Quellen bleiben unverändert. Einen
alten Watchtower stoppt und prüft er vor Migration und Pull; er bleibt danach
aus und darf nur per ausdrücklichem Opt-in wieder aktiviert werden. Ältere,
angepasste, per Override ergänzte oder mehrdeutige Compose-Stände bleiben
unverändert gesperrt und benötigen eine manuelle Prüfung.

Ohne `E3DC_IMAGE_TAG` folgt diese Compose-Datei dem geprüften Stable-Tag
`latest`. Ein fester Tag bleibt bei `pull` absichtlich unverändert. Für einen
bewussten Pin wird zum Beispiel `E3DC_IMAGE_TAG=v5.4.3h` in der Datei `.env`
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
sudo docker compose config --images e3dc-control
```

Erst wenn dort der gewünschte Tag erscheint, folgt der Host-Helfer. Ein eventuell bereits vorhandener Eintrag
`E3DC_IMAGE_TAG=...` in `.env` bleibt dabei die maßgebliche bewusste
Versionswahl.

Gezielte Rückfallversion:

Den Stable-Container `v5.4.3h` auf den veröffentlichten Rollback-Root
`v5.3.2b` zurücksetzen:

```bash
(
  set -euo pipefail
  TAG=v5.3.2b
  cd "$E3DC_DOCKER_PATH"
  BACKUP_FILE="$PWD/e3dc-data-$(date +%Y%m%d-%H%M%S).tgz"
  sudo docker compose exec -T e3dc-control \
    tar czf - -C /var/www/html/data . > "$BACKUP_FILE"
  test -s "$BACKUP_FILE"
  sudo python3 ./Installer/docker_compose_update.py \
    --compose-dir . --sudo --image-tag "$TAG" \
    --legacy-no-healthcheck-version 5.3.2b
  sudo docker compose logs --tail=80 e3dc-control
)
```

Der Rückfall ist bewusst ein Host-Befehl: Der E3DC-Control-Container soll nicht
den Docker-Daemon des Hosts steuern. Die Weboberfläche kann deshalb die
passenden Befehle für den gewählten Tag anzeigen, aber sie führt sie im
Docker-Betrieb nicht selbst aus.

Der fest gebundene historische Rückfall-Root `v5.3.2b` enthält noch keinen
imagegebundenen Healthcheck. Nur für genau diesen Tag belegt der Rückfall daher
zwei identische laufende Container-Snapshots statt `healthy`. Ein aktuelles oder
künftiges Image ohne Healthcheck bleibt dagegen ein harter Fehler.

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

Der Docker-Build normalisiert den kopierten Produktbaum unabhängig von den
Dateimodi des Host-Dateisystems auf root-eigene, für Gruppe und Andere nicht
schreibbare Pfade. Persistente Konfiguration, Logs, RAM-Disk und Matter-Storage
bleiben davon getrennt in den vorgesehenen Laufzeitverzeichnissen und Volumes.

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
(
  set -euo pipefail
  E3DC_LOCAL_CANDIDATE_MAY_EXIST=0
  stop_local_candidate() {
    [ "$E3DC_LOCAL_CANDIDATE_MAY_EXIST" = 1 ] || return 0
    sudo docker compose \
      -f docker-compose.yml \
      -f docker-compose.local.yml \
      stop --timeout 30 e3dc-control || true
    STOPPED_SNAPSHOT_1="$(sudo docker compose \
      -f docker-compose.yml \
      -f docker-compose.local.yml \
      ps -q --status running e3dc-control)" || return 70
    sleep 1
    STOPPED_SNAPSHOT_2="$(sudo docker compose \
      -f docker-compose.yml \
      -f docker-compose.local.yml \
      ps -q --status running e3dc-control)" || return 70
    [ -z "$STOPPED_SNAPSHOT_1" ] && [ -z "$STOPPED_SNAPSHOT_2" ] || return 70
  }
  trap 'rc=$?; if ! stop_local_candidate; then exit 70; fi; exit "$rc"' ERR
  sudo docker compose \
    -f docker-compose.yml \
    -f docker-compose.local.yml \
    config --images e3dc-control
  E3DC_LOCAL_CANDIDATE_MAY_EXIST=1
  sudo docker compose \
    -f docker-compose.yml \
    -f docker-compose.local.yml \
    up -d --force-recreate --wait --wait-timeout 300 e3dc-control
  docker_health_snapshot() {
    sudo docker inspect e3dc-control --format '{{.Id}} {{.Image}} {{.RestartCount}} {{.State.StartedAt}} {{.State.Status}} {{.State.Health.Status}}'
  }
  HEALTH_SNAPSHOT_1="$(docker_health_snapshot)"
  sleep 2
  HEALTH_SNAPSHOT_2="$(docker_health_snapshot)"
  test "$HEALTH_SNAPSHOT_1" = "$HEALTH_SNAPSHOT_2"
  case "$HEALTH_SNAPSHOT_2" in
    *" running healthy") ;;
    *) echo "Container ist nicht stabil gesund: $HEALTH_SNAPSHOT_2" >&2; exit 1 ;;
  esac
  printf '%s\n' "$HEALTH_SNAPSHOT_2"
  E3DC_LOCAL_CANDIDATE_MAY_EXIST=0
  trap - ERR
)
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
für `e3dc_ml`, `e3dc_forecast_evidence` und `e3dc_instance_role`; diese
root-privaten Datenklassen bleiben dadurch vom Webverzeichnis getrennt. Den Pfad siehst du bei Bedarf mit
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
sudo python3 ./Installer/docker_compose_update.py \
  --compose-dir . --sudo --recreate-current
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
sudo python3 ./Installer/docker_compose_update.py --compose-dir . --sudo
sudo docker compose logs --tail=80 e3dc-control
```
Das System lädt nun das fertige Image aus dem Internet herunter, richtet den
Webserver, die RAM-Disk und die Python-Dienste ein. Bei einer frischen leeren
JSON-Konfiguration materialisiert der Konfigurationsassistent zuerst den
Standalone-Wert `ha_mode=off`. Meldet der Assistent keinen Erfolg oder bleibt
danach ein anderer HA-Modus stehen, startet kein Hardware-Writer und der
Container endet mit Fehler. Nach erfolgreichem Start erreichst du das Dashboard
über die IP-Adresse des Hosts.

---

## 2. Architektur & Unterschiede zur normalen Installation

Wenn E3DC-Control in Docker läuft, verhält es sich intern etwas anders als bei einer "Bare-Metal" Installation auf dem Raspberry Pi.

* **Keine Cronjobs:** Docker-Container haben von Haus aus keinen Aufgabenplaner (Cron). Diese Aufgabe übernimmt vollautomatisch der in Python geschriebene **Schedule & Notification Manager** (`notification_manager.py`). Er läuft im Hintergrund und triggert die Minutenspeicherung, Backups und Telegram-Nachrichten.
* **Web-Updates deaktiviert:** Da ein Container „immutable" (unveränderlich) ist, ist der „Update"-Knopf im Web-Dashboard deaktiviert. Klickst du darauf, informiert dich das System, dass Updates über Docker bezogen werden müssen.
* **Kein Systemd:** Befehle wie `systemctl restart e3dc` funktionieren im Container nicht. Wenn du den Dienst neu starten möchtest, startest du einfach den gesamten Container neu (siehe unten). Logs der Python-Dienste findest du unter `/var/www/html/logs/`.
* **Auto-Start:** Du benötigst keine Watchdogs oder Crontab-Einträge mehr, damit E3DC nach einem Stromausfall hochfährt. Der Parameter `restart: unless-stopped` in der `docker-compose.yml` sorgt dafür, dass Docker das System immer am Leben hält.
* **Fail-fast-Supervision:** PID 1 überwacht Apache als echten Vordergrundprozess sowie alle gestarteten Python-/Node-Worker. Endet einer dieser Dauerprozesse nach der Readiness, beendet sich der Container ungleich null; die Docker-Restart-Policy startet anschließend den vollständigen, konsistenten Dienstsatz neu.
* **Web-Neustart:** Der Web-Endpunkt erzeugt ausschließlich ein inhaltlich und per Inode gebundenes Neustartflag. Der Notifier konsumiert genau dieses Flag und beendet sich ungleich null; `wait -n` in PID 1 beendet daraufhin den vollständigen Dienstsatz. Es gibt keinen wirkungslosen Prozessnamen- oder `systemctl`-Neustart im Container.
* **Imagegebundener Healthcheck:** Das Image prüft Apache, alle Pflichtprozesse und exakt die beim Boot aus der Konfiguration projizierten Zusatzdienste. Es liest dafür nur den PID-Namensraum, die root-gebundene Boot-Projektion und die Apache-Konfiguration; RSCP, Geräte und Aktoren werden nicht angesprochen. Die Hostbefehle verlangen anschließend zwei identische Snapshots derselben Container-, Image- und Startgeneration mit unverändert grünem Healthstatus.

### Aktive Hintergrunddienste im Container

Folgende Python-Dienste startet die `entrypoint.sh`. Persistente Dienstlogs liegen unter `/var/www/html/logs/`; Einträge mit „Docker-Engine-Log“ erscheinen in `docker compose logs`:

| Dienst | Startbedingung | Log-Datei |
|--------|----------------|-----------|
| `e3dc_websocket.py` | Immer | `e3dc_websocket.log` |
| `energy_manager.py` | Wärmeintegration aktiviert und gültige Wärmequelle oder SG-Ready-Shelly konfiguriert | Docker-Engine-Log |
| `lux_live.py` | Wärmeintegration aktiviert, Typ Luxtronik und gültige Luxtronik-IP | `lux_live.log` |
| `idm_live.py` | Wärmeintegration aktiviert, Typ IDM und gültige IDM-IP | `idm_live.log` |
| `stiebel/stiebel_live.py` | Wärmeintegration aktiviert, Typ Stiebel Eltron ISG/WPM und gültige ISG-IP | `stiebel_live.log` |
| `dimplex/dimplex_live.py` | `luxtronik` aktiviert, `wp_type=5` und gültige `dimplex_ip` | `dimplex_live.log` |
| `climate_live.py` | Klimamessung aktiviert und gültige Zähler-IP | `climate_live.log` |
| `climate_control.py` | `climate_control_enable` explizit aktiviert | `climate_control.log` |
| `heizstab_manager.py` | Heizstab/Shelly als Wärmequelle konfiguriert | `heizstab_manager.log` |
| `wallbox_manager.py` | Native Wallbox aktiviert, Legacy-Wallbox aus und `wbmode=0` | Docker-Engine-Log |
| `e3dc_mqtt_hub.py` | Externer MQTT-Broker oder mindestens ein MQTT-Eingangstopic konfiguriert | Docker-Engine-Log |
| `bluelink_client.py` | Bluelink-Refresh-Token oder VIN konfiguriert | `bluelink_client.log` |
| `matter/matter_bridge.js` | Matter Bridge explizit aktiviert und Paket-/Lockdatei vorhanden | `matter_bridge.log` |
| `epex_manager.py` | Immer | Docker-Engine-Log |
| `Forecast/pv_forecast_service.py` | Immer | Docker-Engine-Log |
| `forecast_evidence_sidecar.py` | `forecast_diagnostics_enable` explizit aktiviert | Docker-Engine-Log |
| `storage_simulator.py` | Immer | Docker-Engine-Log |
| `storage_manager.py` | Immer | Docker-Engine-Log |
| `e3dc_live.py` | Immer | Docker-Engine-Log |
| `notification_manager.py` | Immer | `notification_manager.log` |

Für Stiebel Eltron ISG/WPM reicht im Docker-Betrieb nach dem Update auf ein
Image ab `5.0.4g`: Im Config-Editor unter **Smart Home &
Verbrauchsprognose** den Schalter **WP-/Verbrauchslogging aktivieren**
einschalten, bei **Wärmepumpen Typ** **Stiebel Eltron ISG / WPM** wählen,
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

1. Im Config-Editor den Bereich **Smart Home MQTT-Hub** öffnen.
2. Broker-IP/Host (`mqtt_hub_ip`) und bei Bedarf Port, Benutzer, Passwort und
   Topics eintragen.
3. Konfiguration speichern.
4. Den Container einmal neu starten:

```bash
cd "$E3DC_DOCKER_PATH"
sudo python3 ./Installer/docker_compose_update.py \
  --compose-dir . --sudo --recreate-current
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
cd "$E3DC_DOCKER_PATH"
sudo python3 ./Installer/docker_compose_update.py --compose-dir . --sudo
sudo docker compose logs --tail=80 e3dc-control
```
Der Helfer aktualisiert Python/PHP-Code, Container-Startskript und Systempakete
nur innerhalb des von Compose projizierten Tags. Er bindet das gezogene Image
vor dem Start an sha256-ID und OCI-Version. `--wait` akzeptiert den Kandidaten
erst nach dem imagegebundenen Healthcheck; zwei identische Snapshots binden
zusätzlich Container-ID, Image-ID, Restart-Zähler, Startzeit, Dienstsatz und
Laufzeit-`VERSION`.

Schlägt `pull` fehl, ist der Updateversuch beendet. Ein bereits vorhandenes
Altimage darf danach weder automatisch neu gestartet noch als aktualisierte
Version gemeldet werden. Nach einem begonnenen Kandidatenstart führt jeder
Fehler zum verifizierten Stopp; bleibt dieser Stopp unbestätigt, meldet der
Helfer einen gesonderten Sicherheitsfehler.

Für einen Rückfall wird ausschließlich der Host-Helfer aus dem Abschnitt
„Gezielte Rückfallversion“ verwendet. Er erhält den freigegebenen Tag über
`--image-tag`, bindet Image und Laufzeit und bestätigt bei einem Fehler den
Stillstand des Kandidaten. Der einzige vorgesehene öffentliche Rollback-Root
ist `ghcr.io/a9xxx/install-e3dc-control:v5.3.2b`; dieser Stand selbst verweist
auf kein älteres Image. Ein dauerhafter Pin in `.env` wird erst nach dem
verifizierten Rückfall gesetzt. Rohe Pull-/Up-Befehle ersetzen den Helfer nicht.

Watchtower ist nur noch ein ausdrückliches Opt-in. Das Enable-Label steht mit
`${E3DC_WATCHTOWER_ENABLE:-false}` ebenfalls standardmäßig auf `false`. Das Upstream-Projekt wird
nicht mehr gepflegt. Der Dienst liegt für bestehende Installationen im
Compose-Profil `auto-update` und startet bei einem normalen
`docker compose up -d --wait --wait-timeout 300 e3dc-control` nicht. Sein notwendiger Zugriff auf
`/var/run/docker.sock` gibt dem Container weitreichende Kontrolle über den
Docker-Host. Wer diese Risiken bewusst akzeptiert, aktiviert das Profil:

```bash
cd "$E3DC_DOCKER_PATH"
printf '%s\n' 'E3DC_WATCHTOWER_ENABLE=true' >> .env
sudo python3 ./Installer/docker_compose_update.py \
  --compose-dir . --sudo --recreate-current
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
sudo python3 ./Installer/docker_compose_update.py \
  --compose-dir . --sudo --recreate-current
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
