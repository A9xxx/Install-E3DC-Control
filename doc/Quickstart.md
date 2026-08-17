# Quickstart: E3DC-Control Installation

Diese Anleitung fasst die schnellsten Schritte zusammen, um E3DC-Control auf einem frischen Raspberry Pi OS (oder ähnlichem Debian-System) zu installieren.

Aktueller Stable-Stand: `v5.4.3s`.

5.4.3s legt im administrativen Root-Download-Bootstrap einen wirklich
fehlenden kanonischen Backup-Root sicher an. Danach bleibt der Ablauf
unverändert: Backup erstellen und prüfen, Dienste stoppen, Dateien und Rechte
aktualisieren, Dienste starten und ihren Zustand prüfen. EMS-,
Direktvermarktungs-, Wallbox- und Hardwarelogik ändern sich nicht.

5.4.3r darf beim ausdrücklich mit Rolle und Peer gebundenen
Download-Bootstrap einen wirklich fehlenden HA-Rollenanker aus dieser bereits
bestehenden Bindung erzeugen. Das geschieht erst nach verifiziertem Backup und
bestätigter Writer-Ruhe. EMS-Regelung und Hardwareausgänge ändern sich nicht.

5.4.3q verwendet im Finalizer des administrativen Download-Bootstraps den
absoluten Pfad `/usr/sbin/visudo` und ist damit nicht von einem verkürzten
Root-`PATH` abhängig. EMS-Regelung und Hardwareausgänge ändern sich nicht.

5.4.3p erzeugt die vom administrativen Download-Bootstrap neu aufgebaute
`.git`-Fläche als den zuvor eindeutig gebundenen Installationsbenutzer.
Verifiziertes Backup, bestätigte Writer-Ruhe und sämtliche Safety-Gates
bleiben unverändert verpflichtend. EMS-Regelung und Hardwareausgänge ändern
sich nicht.

5.4.3o ergänzt einen administrativen Download-Bootstrap für heterogene
Altinstallationen. Er verwendet weder den vorhandenen Alt-Updater noch dessen
`.git`-Metadaten als Autorität und normalisiert bekannte Release-Dateien,
Rechte und Units erst nach verifiziertem Backup und bestätigter Writer-Ruhe.
Pfadflucht, Links, Spezialdateien, zusätzliche Hardlinks, konkurrierende
Updates sowie fehlgeschlagene Backup- oder Healthchecks bleiben harte Stopps.
Der sichere passive Direktvermarktungs-Ladeblock bleibt zugleich auch ohne
diagnostischen Plankandidaten an Plan, Slot, DV-Owner und den tatsächlichen
0-W-Ausgang gebunden; Laden oder Entladen wird nicht neu autorisiert.

5.4.3n akzeptiert im privilegierten Backup- und Recoveryvertrag ausschließlich
den kanonischen Rollenanker `/etc/e3dc-control/instance_role.json` mit
`root:www-data 0640` als Restorequelle. Die private Backup-Payload bleibt
`root:root 0600`. Alle anderen privilegierten Pfade und abweichende
Metadaten, Links, ACLs, Attribute oder Identitätsdrift bleiben streng
fail-closed. EMS-Regelung, HA, Wallbox, Wärme, Direktvermarktung und
Hardwareausgänge bleiben gegenüber 5.4.3m unverändert.

5.4.3m erlaubt ausschließlich dem vollständig versiegelten nativen
Ziel-Updater beim normalen vorwärtsgerichteten Releasewechsel, einen wirklich
fehlenden Instanzrollenanker für den exakt gebundenen `off`-Einzelknoten ohne
Peer einmalig zu erzeugen. Das geschieht erst nach Root-Receipt-gebundenem
Backup, bewaffnetem persistentem Startschutz und bestätigter Aktorruhe; eine
gegebenenfalls nötige Storage-Manager-Unit-Promotion folgt erst unter diesem
Schutz. Bootstrap, Reinstall und Rollback besitzen diese
Autorität nicht. Vorhandene fremde oder widersprüchliche Anker sowie HA- und
Shadow-Rollen bleiben fail-closed und werden nicht automatisch repariert. Die
openWB-Pro-Regelung schließt zugleich eine gestrandete ältere
Phasenausgangsgeneration vor einem neuen Storage-Grant ausschließlich aus
exakt gebundenem Intent, ACK und frischem 0-A-/0-W-Readback ohne neuen
Hardwarebefehl. Eine Budgetreservierung startet keinen Phasen-Cooldown; die
mindestens 480 Sekunden beginnen erst nach bestätigtem realem Phasenausgang.
Ein bewusst neu gespeicherter Modus-5-Sofortauftrag darf nur die vollständig
belegte 3/3-Startablehnung derselben aktuellen Stecksession einmalig neu
öffnen. Preislimit, Nutzer-`Aus`, Not-Aus, Speicher-, Netzpunkt-, Phasen- und
Hardwaregrenzen bleiben vorrangig; der Auftrag selbst erzeugt weder ein Budget
noch einen Hardwareausgang.

5.4.3l härtet den updater-eigenen, nativen Git-Rückweg. Belegte,
weiterhin vorhandene Änderungen an getrackten Dateien werden bei einem
Rückweg mit ihren gesicherten Bytes wiederhergestellt; der Dateimodus folgt
dem im gebundenen `old_commit` belegten Git-Modus. Eine
exakt bekannte ältere Storage-Manager-Unit kann vor dem ersten Dienststopp
sicher in den root-eigenen Unit-Vertrag überführt werden. PiGuard im Zustand
`activating/auto-restart` gilt dabei als zuvor laufend. Erkennt der
Ziel-Updater einen Recoveryfehler synchron, bleibt ein transaktionsgebundener
Startschutz für PiGuard und die bekannten Writer bestehen. Staged,
ungetrackte oder gelöschte Zustände, allgemeine manuelle Restores,
Stromausfall und `SIGKILL` erhalten dadurch keine neue Vollständigkeitszusage.
Die EMS-Regelung bleibt unverändert.

5.4.3k schließt zusätzlich den älteren nativen
`--target-updater-handoff`, der `E3DC_BOOTSTRAP_USER` vor seinem root-eigenen
Ziel-Snapshot entfernt. Beide unterstützten Snapshot-Einstiege binden den
Installationsnutzer erst nach dem Root-Lock aus demselben gültigen
Nicht-Root-Eigentümer von Repository und `.git`. Nach der Bindung des
versiegelten Snapshots werden Repository, `.git`, Nutzerkonto und Nutzerwert
unmittelbar vor dem ersten Import aus dem Zielcode erneut geprüft. Die
Härtungen aus 5.4.3j bleiben unverändert.

5.4.3j schließt den flaglosen, root-eigenen 5.4.2d-Ziel-Snapshot: Fehlt die vom
alten Aufrufer entfernte Nutzerumgebung, darf sie nur aus dem übereinstimmenden
Eigentümer von Repository und `.git` sicher neu gebunden werden. Root,
`www-data`, fremde oder unterschiedliche Eigentümer bleiben gesperrt. Ein schon
gesetzter Nutzerwert muss exakt zum gebundenen Repository-Eigentümer passen.
Im Docker-Container wird der persistente Matter-Baum vor der Härtung nofollow
und descriptorgebunden geprüft; Symlinks, Sonderdateien, reguläre Dateien mit
mehreren Hardlinks oder eine Mount- beziehungsweise Identitätsdrift stoppen den
Start. Der Matter-Worker erzeugt neue Storage-Dateien durch `umask 077`
höchstens mit `0600`. Die Regelung für HA, Wallbox, Speicher, Wärme und
Direktvermarktung bleibt gegenüber 5.4.3i unverändert; Matter-Protokoll und
Kopplung ändern sich nicht.

5.4.3i bindet beim älteren 5.4.2-Zielübergang den lokalen Installationsnutzer,
hält private HA- und Matter-Daten knotenlokal und vervollständigt den
openWB-Pro-Startvertrag. Der erste Wechsel von 5.4.3f auf 5.4.3i erfolgt noch
einmalig über die administrative Konsole; danach steht der feste Web-Launcher
für normale Updates bereit. Speicher-, Wärme- und DV-Regelung bleiben
gegenüber 5.4.3h unverändert.

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

**Einmalige Ausnahme für Version 5.3.2b:** Der erste Wechsel auf das aktuelle
Stable-Release erfolgt über die folgende vollständige administrative
SSH-Kette. Der Web-Launcher ist in diesem Altstand noch nicht vorhanden. Der
interaktive Menüpunkt darf für genau diesen ersten
Hybridwechsel nicht verwendet werden, weil sein Altprozess bereits zusätzliche
5.3.2b-Module geladen hat.

```bash
export E3DC_INSTALL_PATH="$HOME/Install"
test -f "$E3DC_INSTALL_PATH/installer_main.py"
test -x "$HOME/.venv_e3dc/bin/python3"
cd "$E3DC_INSTALL_PATH"
sudo /usr/bin/python3 installer_main.py --fix-permissions
sudo /usr/bin/python3 -I -B -u installer_main.py --check
sudo /usr/bin/python3 -I -B -u installer_main.py --update-e3dc
cat VERSION
systemctl --failed --no-pager
```

Liegt E3DC-Control nicht unter `$HOME/Install`, wird nur die erste Zeile an den
tatsächlichen absoluten Installationspfad angepasst. Schlägt eine der beiden
`test`-Zeilen fehl, dort stoppen. Weitere Hintergründe stehen in
[Update.md](Update.md).

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
3. **Image ziehen, starten und vollständig prüfen:**

```bash
sudo python3 ./Installer/docker_compose_update.py --compose-dir . --sudo
sudo docker compose logs --tail=80 e3dc-control
```

Der Helfer zieht das gewählte Image ausdrücklich, bindet Image-ID und
Produktversion, wartet auf den Image-Healthcheck und verlangt zwei identische
gesunde Folgesnapshots. Scheitert ein Schritt nach dem Kandidatenstart, stoppt
er den Kandidaten wieder und bestätigt dessen Stillstand. Die mitgelieferte
Compose-Datei enthält die persistenten Daten-, Log-, Modell-, Prognose- und
Instanzrollen-Volumes, die RAM-Disk, feste Loggrenzen und den standardmäßig
deaktivierten Watchtower-Vertrag. Docker ist nur für die eigenständige Rolle
`ha_mode=off` freigegeben.

Danach ist das System über die IP des Docker-Hosts erreichbar. Eine frische
Konfiguration wird im Config-Editor eingerichtet.

**Docker-Updates:**
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
Identitätsprüfung und den bestätigten Stopp eines fehlerhaften Kandidaten als
eine Transaktion.

Fehlt der Helfer in einer älteren Installation, wird ein separater frischer
Checkout des veröffentlichten Verwaltungsbaums verwendet. Dessen
`Installer/docker_compose_update.py` erhält mit `--compose-dir` den Pfad des
bestehenden Compose-Verzeichnisses. Er migriert ausschließlich unveränderte
offizielle Compose-Dateien aus 5.4.2 bis 5.4.2d sowie die bekannte
Installer-Bind-Mount-Variante atomar, also ganz oder gar nicht. `.env` und die
vorhandenen Daten-, Log-, ML- und Forecast-Quellen bleiben unverändert. Einen
alten Watchtower stoppt und prüft der Helfer vor Migration und Pull; er bleibt
danach aus und darf nur per ausdrücklichem Opt-in wieder aktiviert werden.
Ältere, angepasste, per Override ergänzte oder mehrdeutige Compose-Stände
bleiben unverändert gesperrt und benötigen eine manuelle Prüfung.

Der optionale, nicht mehr gepflegte Watchtower startet nicht automatisch, weil
er weitreichenden Zugriff auf den Docker-Socket des Hosts benötigt. Ein
bewusster Opt-in benötigt **beides**: in `.env` exakt
`E3DC_WATCHTOWER_ENABLE=true` und anschließend das Profil `auto-update`.
Ohne das Label bleibt auch ein versehentlich gestarteter Watchtower für den
Hauptcontainer wirkungslos. Der oben gezeigte manuelle Host-Helfer bleibt der
empfohlene Updateweg.

**Docker-Rückfall von v5.4.3s auf den veröffentlichten Docker-Rollback-Root:**
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

Nach der Installation können Sie den Installer über `bash "$E3DC_INSTALL_PATH/e3dc-setup"` starten und die gewünschte Wartungsoption wählen:

- **E3DC-Control installieren oder aktualisieren:**
  - Option `1` (Installation / Update)
  - Hält Anwendung, Webdateien, Dienste und Rechte auf dem aktuellen Stand.
  - Ausnahme: Für den einmaligen Wechsel von 5.3.2b auf das aktuelle Stable-Release den
    direkten administrativen `--update-e3dc`-Aufruf aus
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
