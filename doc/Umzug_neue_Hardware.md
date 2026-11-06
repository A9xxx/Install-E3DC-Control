# Umzug auf neue Hardware

Diese Anleitung beschreibt den Umzug einer Einzelanlage (`ha_mode=off`) auf ein anderes Gerät. Übernommen werden Konfiguration, Langzeitstatistik, Kostenhistorie, Tageshistorien, Klima- und Wärmepumpenarchive und auf Wunsch das Lernmodell. Für HA-Master/-Slave und Shadow-Instanzen gilt der Weg in der [High-Availability-Dokumentation](High_Availability_Dokumentation.md), Abschnitt „Konfiguration einspielen und Ersatzhardware“.

## Grundregeln

- **Nie zwei Regler gleichzeitig.** E3DC-Control erkennt ein zweites Gerät am selben Speicher nicht. Das Altgerät wird deshalb stillgelegt, bevor das neue Gerät Zugangsdaten erhält, und nach der Datensicherung ausgeschaltet. Die Stilllegung über `systemctl` ist nicht wartungsfest: „Rechte prüfen & korrigieren“ (`e3dc-setup --fix-permissions`), das Update und andere Wartungswege des Installers aktivieren und starten gestoppte Dienste wieder. Auf dem Altgerät deshalb weder die Rechte-Reparatur noch ein Update oder eine andere Installer-Wartung ausführen, solange das neue Gerät regelt.
- **Zuerst installieren, dann Daten einspielen.** Die Erstinstallation legt die Rollenbindung des neuen Geräts an. Liegt vorher schon eine Konfiguration auf dem Gerät, bricht die Installation mit „Root-privater Instanzrollen-Anker fehlt oder widerspricht der Konfiguration“ ab.
- **Keine Kopie der Speicherkarte oder des Systems.** Eine Kartenkopie behält Hostnamen und Rollenbindung des Altgeräts; beide Geräte erscheinen dann als dieselbe Anlage.
- **Das Vollbackup des Installers ist kein Umzugsweg.** Es ist an die Installation gebunden und stellt auch Systemdateien des Altgeräts wieder her. Es dient auf dem Altgerät als zusätzlicher Rückweg.
- **Gleiche Version auf beiden Geräten.** Updates erst nach dem Umzug einspielen.
- **Benutzername und Installationspfad dürfen abweichen.** Der Import behält die Pfade des neuen Geräts; die Rechte-Reparatur passt die Eigentümer der übernommenen Dateien an.
- **Die Umzugsdateien sind vertraulich.** Sie enthalten Zugangsdaten, die Web-PIN und Fahrzeugdaten. Ablage mit eingeschränkten Rechten anlegen, nur über vertrauenswürdige Wege übertragen und danach löschen. Nicht in Forum oder Diagnosebeiträge stellen.

## Was übernommen wird

| Bereich | Weg | Hinweis |
|---|---|---|
| Konfiguration (`e3dc_v4.json`) | „Raw Download“ und Import im Config-Editor | Installationspfade und HA-Rolle des neuen Geräts bleiben erhalten. Übernommen werden auch die Web-PIN, Pausen und Ladepunktsperren. Eine bestätigte höhere Hausabsicherung wird nicht übernommen und ist neu zu bestätigen. |
| Langzeitstatistik und Kosten (`e3dc_stats.db`) | Kopie über die SQLite-Sicherungsschnittstelle | Langzeit- und Kostenansicht zeigen danach die früheren Werte. |
| Tageshistorien (30 Tage), Klima- und Wärmepumpenarchiv | Archiv der Verzeichnisse | Grundlage für das Neutraining des Lernmodells |
| Lernmodell | Neutraining (empfohlen) oder Kopie | Kopie nur bei gleichem Betriebssystem- und Bibliotheksstand und gleicher Architektur |
| Gelernte PV-Korrektur (`pv_forecast_eval.json`) | optional | Bleibt bei gleichen Dachflächen gültig; bei geänderten Dachflächen beginnt die Kalibrierung neu. Nicht im Vollbackup enthalten. |
| Not-Aus | **nicht übernommen** | NOT-AUS sperrt beide Ladepunkte; diese Sperren kommen mit der Konfiguration, die Not-Aus-Markierung selbst nicht. Not-Aus bei Bedarf auf dem neuen Gerät erneut auslösen. |
| Wärmepumpen- und Wallbox-Betriebszustand, Planaufträge | **nicht übernommen** | entstehen neu |
| Matter-Kopplung | **nicht übernommen** | am neuen Gerät neu koppeln |
| Rollenbindung, Dienste, Systemverträge, Backup-Ablage, RAM-Disk | **nicht übernommen** | entstehen bei der Installation des neuen Geräts |
| Gelernte Netzladeleistung je Ladezustand | **nicht übernommen** | liegt in der RAM-Disk und beginnt mit den Startwerten |
| `e3dc.config.txt` im Installationsverzeichnis | **nicht übernehmen** | nur Alt- und Hilfsdatei; maßgeblich ist `e3dc_v4.json` |

---

# A. Bare Metal (Raspberry Pi und vergleichbare Systeme)

## A1. Vorbereitung am Altgerät

Das Altgerät regelt in diesem Abschnitt weiter.

1. **Version und Benutzer festhalten:**
   ```bash
   export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
   cat "$E3DC_INSTALL_PATH/VERSION"
   id
   ```
   Auf dem neuen Gerät dieselbe Version installieren.
2. **Web-PIN setzen.** Der „Raw Download“ der Konfiguration ist nur mit gesetzter Web-PIN verfügbar; ohne PIN zeigt die Karte „Raw Download gesperrt“. Die PIN gilt nach dem Import auch auf dem neuen Gerät.
3. **Ladepunkte notieren.** Auf der Seite „Wallbox“ je Ladepunkt Modus, Pause, Ladepunktsperre und Not-Aus notieren. Laufende Ladungen beenden und alle Fahrzeuge abstecken. Sie bleiben abgesteckt, bis auf dem neuen Gerät Pause, Sperre und Not-Aus wieder wie notiert stehen (A8).
4. **Vollbackup als Rückweg anlegen:** `bash "$E3DC_INSTALL_PATH/e3dc-setup"` → „6) Backup erstellen / verwalten“ → „1 = Vollständiges Backup erstellen“. Das Backup bleibt auf dem Altgerät.
5. **Kennzahlen notieren** (als Installationsbenutzer):
   ```bash
   python3 - <<'PY'
   import sqlite3
   c = sqlite3.connect("file:/var/www/html/data/e3dc_stats.db?mode=ro", uri=True)
   print("daily_stats:", c.execute("select count(*), min(date), max(date) from daily_stats").fetchone())
   PY
   ls /var/www/html/data/history_backups | wc -l
   ```
6. **Konfiguration herunterladen:** Config-Editor → „System-Werkzeuge“ → „Konfigurationssicherung“ → „Raw Download“. Die Datei nur auf einem privaten Datenträger ablegen. „Einstellungen ohne Zugangsdaten“ eignet sich nicht für den Umzug.

## A2. Neues Gerät installieren

1. Das neue Gerät wie im [Quickstart](Quickstart.md) installieren, mit einem anderen Hostnamen als das Altgerät. Benutzername und Installationspfad sind frei wählbar.
2. Der Konsolenassistent der Installation fragt zahlreiche Anlagenwerte ab. Für den Umzug alle Fragen mit Enter übernehmen und **keine** E3DC-Adresse und keine Zugangsdaten eintragen. Abweichend davon antworten:
   - „Alle Schritte ausführen? (j/n)“: `j`
   - „Strompreise jetzt konfigurieren? (j/n)“: `n`
   - „Energy Manager aktivieren (empfohlen)? (j/n)“: `n`
   - „Native Wallbox Steuerung aktivieren? (j/n)“: `n`

   Die auf dem Altgerät genutzten Module bietet nach dem Import der Einrichtungsplan an (A6); ein dort nicht angebotenes Modul in der Installationszentrale oder im Expertenmenü einrichten. Der PV-Wächter wird bei der Installation automatisch eingerichtet.
3. Ohne Zugangsdaten regelt das neue Gerät nicht; der Speicherregler meldet „Unvollständige E3DC-RSCP-Konfiguration. Warte auf Ziel und Zugangsdaten.“

## A3. Altgerät stilllegen

Zeitpunkt kurz nach Minute :55 einer vollen Stunde wählen; dann ist die stündliche Sicherung von Statistik und Minutenhistorie gerade gelaufen. Minutenwerte danach liegen nur in der RAM-Disk und gehen beim Stopp verloren.

Zuerst den PV-Wächter, dann die Hardware-Schreiber, danach Mess-, Prognose- und Hilfsdienste stoppen und deaktivieren:

```bash
UNITS="piguard e3dc-watchdog e3dc-ha e3dc-shadow-sync \
 e3dc-wallbox-manager energy_manager e3dc-heizstab e3dc-storage-manager \
 e3dc-storage-simulator e3dc-mqtt-hub e3dc-bluelink e3dc-matter-bridge \
 e3dc-notifier e3dc-climate-control e3dc-climate-live e3dc-lux-live \
 e3dc-idm-live e3dc-stiebel-live e3dc-dimplex-live e3dc-forecast-evidence \
 e3dc-weather-manager e3dc-epex-manager e3dc-live e3dc e3dc-websocket"
for u in $UNITS; do
  if systemctl cat "$u.service" >/dev/null 2>&1; then
    sudo systemctl disable --now "$u.service"
  fi
done
sudo systemctl stop apache2
```

Stilllegung prüfen. Alle drei Befehle sollen keine Ausgabe liefern:

```bash
systemctl list-units --state=active --no-legend 'e3dc*' 'energy_manager*' 'piguard*'
systemctl list-unit-files --state=enabled --no-legend 'e3dc*' 'energy_manager*' 'piguard*'
pgrep -af 'storage_manager|wallbox_manager|energy_manager|heizstab_manager|e3dc_live|E3DC-Control|screen.*E3DC'
```

Manuell gestartete Prozesse eines alten C++-Reglers oder `screen`-Sitzungen ebenfalls beenden.

## A4. Daten sichern und Altgerät ausschalten

Ablage mit eingeschränkten Rechten anlegen:

```bash
umask 077
mkdir -p "$HOME/umzug"
```

**Statistikdatenbank** über die SQLite-Sicherungsschnittstelle kopieren:

```bash
python3 - <<'PY'
import os, sqlite3
src = sqlite3.connect("/var/www/html/data/e3dc_stats.db", timeout=30)
dst = sqlite3.connect(os.path.expanduser("~/umzug/e3dc_stats.db"))
src.backup(dst)
print("integrity_check:", dst.execute("PRAGMA integrity_check").fetchone()[0])
dst.close(); src.close()
PY
```

Erwartung: `integrity_check: ok`.

**Historien und Archive** (nur vorhandene Einträge):

```bash
cd /var/www/html/data
ITEMS=""
for n in history_backups climate_history luxtronik_archive \
         battery_vitals_history.json battery_vitals_trigger_state.json \
         kwh_retter.json wb_sessions.csv climate_energy_baseline.json \
         saved_cars.json external_pv_topology.json; do
  [ -e "$n" ] && ITEMS="$ITEMS $n"
done
sudo tar --numeric-owner -p -czf "$HOME/umzug/daten.tar.gz" $ITEMS
```

`config_backups`, `matter-storage`, Sperr- und Markerdateien (`*.lock`, `*.flag`) sowie die Betriebszustände von Wallbox und Wärmepumpe bewusst nicht einpacken.

**Gelernte PV-Korrektur** (optional):

```bash
sudo tar --numeric-owner -p -czf "$HOME/umzug/auswertung.tar.gz" \
  -C /var/www/html/logs pv_forecast_eval.json ml_consumption_eval.json
```

**Lernmodell** (optional, siehe A7):

```bash
sudo tar --numeric-owner -p -czf "$HOME/umzug/ml.tar.gz" -C /var/lib/e3dc-control ml
```

Die in A1 heruntergeladene Konfigurationsdatei ebenfalls nach `~/umzug` legen.

**Prüfsummen bilden, übertragen, Altgerät ausschalten:**

```bash
cd "$HOME/umzug" && rm -f SHA256SUMS && sha256sum -- * > SHA256SUMS
scp -r "$HOME/umzug" <benutzer>@<neues-geraet>:~/
sudo poweroff
```

Das Altgerät bleibt ausgeschaltet. Es wird erst für einen Rückweg (A9) wieder eingeschaltet; die Dienste starten dann nicht von selbst. Auf dem wieder eingeschalteten Altgerät keine Rechte-Reparatur und kein Update ausführen, solange das neue Gerät regelt (siehe Grundregeln).

## A5. Neues Gerät: Daten einspielen

Prüfsummen kontrollieren:

```bash
cd "$HOME/umzug" && sha256sum -c SHA256SUMS
```

Laufende Produktdienste anhalten:

```bash
for u in $(systemctl list-units --state=active --no-legend --plain 'e3dc*' 'energy_manager*' 'piguard*' | awk '{print $1}'); do
  sudo systemctl stop "$u"
done
```

**Statistikdatenbank** ersetzen (`<benutzer>` = Installationsbenutzer des neuen Geräts):

```bash
sudo rm -f /var/www/html/data/e3dc_stats.db-wal /var/www/html/data/e3dc_stats.db-shm /var/www/html/data/e3dc_stats.db-journal
sudo install -o <benutzer> -g www-data -m 664 "$HOME/umzug/e3dc_stats.db" /var/www/html/data/e3dc_stats.db
```

**Historien, Archive und PV-Korrektur:**

```bash
sudo tar --numeric-owner -p -xzf "$HOME/umzug/daten.tar.gz" -C /var/www/html/data
sudo tar --numeric-owner -p -xzf "$HOME/umzug/auswertung.tar.gz" -C /var/www/html/logs
```

**Rechte setzen:**

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
```

Die Reparatur setzt Eigentümer und Rechte der übernommenen Dateien und startet die Kerndienste wieder. Solange keine Zugangsdaten eingetragen sind, regeln sie nicht.

## A6. Konfiguration importieren

1. Im Config-Editor des neuen Geräts: „System-Werkzeuge“ → „Konfigurationssicherung“ → „Upload“. Im Dialog „Konfiguration importieren“ die Datei aus A1 wählen und „Importieren“ bestätigen. Eine Web-PIN ist auf dem neuen Gerät dafür nicht nötig.
2. Die Meldung prüfen: „Konfiguration importiert (n Werte). Lokale Installationspfade wurden beibehalten. HA-Rolle und Peer dieser Anlage wurden beibehalten. Backup wurde bestätigt.“ mit dem Befund „Rollenanker: passt zum Gerät“. Meldet der Import einen Rollen- oder Ankerkonflikt, **nicht** umgehen und die [High-Availability-Dokumentation](High_Availability_Dokumentation.md) beachten.
3. Ab jetzt gilt die Web-PIN des Altgeräts.
4. Den direkt nach dem Import angebotenen Einrichtungsdialog bestätigen. Die Installationszentrale richtet dann die fehlenden Module ein, zum Beispiel den Wallbox Manager. Das Angebot erscheint nur unmittelbar nach dem Import; später lassen sich Module in der Installationszentrale einzeln einrichten.
5. Galt auf dem Altgerät ein höherer Wert der Hausabsicherung als der Systemwert, beim Feld „Hausabsicherung/SLS (A je Phase)“ das Kästchen „Systemwert überschreiben“ erneut setzen und speichern.
6. Matter-Kopplung (falls genutzt) auf der Matter-Seite neu herstellen.
7. **Neues Gerät neu starten** (`sudo reboot`). Laufende Dienste übernehmen die importierten Zugangsdaten erst beim Start. Nach dem Neustart regelt das neue Gerät.

## A7. Lernmodell

**Empfohlen: Neutraining aus den übertragenen Historien** (mindestens 50 gültige Viertelstunden der letzten 30 Tage), als Installationsbenutzer mit dem Interpreter der Produkt-Umgebung (Standard `~/.venv_e3dc/bin/python3`):

```bash
"<venv-python>" -B "$E3DC_INSTALL_PATH/Installer/ml_predictor.py" --train
"<venv-python>" -B "$E3DC_INSTALL_PATH/Installer/ml_predictor.py" --predict
```

Erwartet: „ML-Modell erfolgreich trainiert“ und „Verbrauchsprognose verwendet das manifest- und hashgeprüfte ML-Modell.“

**Optional: Modellordner übernehmen.** Nur bei gleichem Betriebssystem- und Bibliotheksstand (`python3-sklearn`, `python3-numpy`) und gleicher Architektur. Bei einem Wechsel von 32 auf 64 Bit nicht übernehmen, sondern neu trainieren.

```bash
sudo tar --numeric-owner -p -xzf "$HOME/umzug/ml.tar.gz" -C /var/lib/e3dc-control
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
"<venv-python>" -B "$E3DC_INSTALL_PATH/Installer/ml_predictor.py" --predict
```

Maßgeblich ist die Ausgabe von `--predict`. `--model-ready` prüft nur Manifest, Rechte und Prüfsumme und meldet auch ein nicht ladbares Modell als bereit. Meldet `--predict` „Kein nutzbares sicheres ML-Modell; verwende variables Historienprofil“, mit `--train` neu trainieren.

## A8. Prüfen

1. **Systemstatus:** `bash "$E3DC_INSTALL_PATH/e3dc-setup"` → „2) Systemstatus anzeigen“. Erwartet: „Keine offensichtlichen Probleme gefunden.“
2. **Statistik übernommen:** Die Abfrage aus A1 Schritt 5 liefert dieselbe Zeilenzahl und denselben Datumsbereich von `daily_stats` und dieselbe Anzahl Tageshistorien. Die Langzeitansicht zeigt die Tage seit Beginn der Aufzeichnung, die Kostenhistorie (falls eingeschaltet) die früheren Werte.
3. **Lernmodell:** `--predict` meldet das manifest- und hashgeprüfte Modell.
4. **Regelung:** Speicher, Wallbox und Wärmepumpe zeigen aktuelle Werte; es erscheint keine Meldung zur Rollenbindung.
5. **Ladepunkte:** Pause und Ladepunktsperre stehen wie auf dem Altgerät. Ein notiertes Not-Aus erneut auslösen. Erst danach Fahrzeuge anstecken.
6. **Beobachtungsphase:** mindestens einen vollständigen Tag mit PV-Ertrag und eine Nacht beobachten. Danach das Altgerät neu aufsetzen oder abbauen.

## A9. Rückweg

1. Neues Gerät mit derselben Befehlsfolge wie in A3 stilllegen und ausschalten.
2. Altgerät einschalten und die Dienste wieder aktivieren, zuerst `e3dc-live`, zuletzt den PV-Wächter, dazu die Weboberfläche:
   ```bash
   for u in e3dc-live e3dc-epex-manager e3dc-weather-manager e3dc-forecast-evidence \
            e3dc-storage-simulator e3dc-storage-manager e3dc-wallbox-manager energy_manager \
            e3dc-heizstab e3dc-notifier piguard; do
     if systemctl cat "$u.service" >/dev/null 2>&1; then sudo systemctl enable --now "$u.service"; fi
   done
   sudo systemctl enable --now apache2
   ```
   Weitere auf dem Altgerät genutzte Zusatzdienste (z. B. MQTT-Hub, Bluelink, Matter) ebenso aktivieren.
3. Statistik und Historien, die erst auf dem neuen Gerät entstanden sind, werden nicht zurückgeführt; bei Bedarf vorher wie in A4 in umgekehrter Richtung sichern. Zwei Statistikdatenbanken lassen sich nicht zusammenführen.
4. Bei Bedarf auf dem Altgerät das vor dem Umzug angelegte Vollbackup über „Backup wiederherstellen“ einspielen.

---

# B. Docker

Docker läuft nur auf 64-Bit-Systemen (`arm64`, `amd64`). Ein 32-Bit-System kann das Image nicht starten.

## B1. Volumes

Die mitgelieferte Compose-Datei verwendet fünf Volumes. Ihr Name beginnt mit dem Compose-Projektnamen, standardmäßig dem Namen des Compose-Ordners (Anzeige mit `sudo docker volume ls`).

| Volume | Pfad im Container | Umzug |
|---|---|---|
| `e3dc_data` | `/var/www/html/data` | **ja** (Konfiguration, Statistik, Historien) |
| `e3dc_logs` | `/var/www/html/logs` | optional (gelernte Auswertungsreihen, Protokolle) |
| `e3dc_ml` | `/var/lib/e3dc-control/ml` | optional; fehlt es, trainiert der Container beim ersten Start einmalig aus der übertragenen Historie |
| `e3dc_forecast_evidence` | `/var/lib/e3dc-control/forecast-evidence` | nein (Diagnosearchiv) |
| `e3dc_instance_role` | `/etc/e3dc-control` | **nein**: nie auf einen anderen Host kopieren. Der Container hat auf jedem Host denselben Hostnamen; eine Kopie würde deshalb passen und ein zweites System als dieselbe Anlage ausweisen. |

Die `.env` mit dem Watchtower-Token nicht übertragen, sondern am neuen Ort neu anlegen. Die RAM-Disk gehört nicht in die Sicherung.

## B2. Vorbereitung

1. Auf beiden Hosts denselben Image-Tag verwenden (`E3DC_IMAGE_TAG` in der `.env`, siehe [Docker-Dokumentation](Docker_Dokumentation.md)). Updates erst nach dem Umzug.
2. Web-PIN und Ladepunkte wie in A1 Schritt 2 und 3; laufende Ladungen beenden, Fahrzeuge abstecken.
3. Auf dem neuen Host den Compose-Ordner gleich benennen wie auf dem Altsystem; dann stimmen die Volumenamen überein.

## B3. Altsystem: Kennzahlen, Stilllegung, Sicherung

Im Compose-Ordner des Altsystems, solange der Container noch läuft, die Kennzahlen notieren:

```bash
cd "$E3DC_DOCKER_PATH"
sudo docker exec e3dc-control /opt/venv/bin/python3 -c 'import sqlite3; c = sqlite3.connect("file:/var/www/html/data/e3dc_stats.db?mode=ro", uri=True); print(c.execute("select count(*), min(date), max(date) from daily_stats").fetchone())'
sudo docker exec e3dc-control sh -c 'ls /var/www/html/data/history_backups | wc -l'
```

Watchtower (falls genutzt) und den Container stoppen und den Stillstand prüfen:

```bash
sudo docker compose --profile auto-update stop watchtower
sudo docker compose stop e3dc-control
sudo docker compose ps -a
```

`docker compose ps -a` muss den Container als beendet (`exited`) zeigen. Kein `docker compose down -v` und kein `docker volume rm`.

Volumes bei gestopptem Container sichern, mit numerischen Eigentümern (`<projekt>` = Compose-Projektname):

```bash
umask 077
mkdir -p "$PWD/umzug"
for v in <projekt>_e3dc_data <projekt>_e3dc_logs <projekt>_e3dc_ml; do
  sudo docker run --rm -v "$v":/quelle:ro -v "$PWD/umzug":/ziel debian:bookworm-slim \
    tar --numeric-owner -cpzf "/ziel/$v.tar.gz" -C /quelle \
    --exclude=./matter-storage --exclude=./.wallbox_plan_jobs \
    --exclude='./*.lock' --exclude='./*.flag' \
    --exclude=./wallbox_mode5_user_start_request.json .
done
sudo chown "$USER" umzug/*.tar.gz
( cd umzug && sha256sum *.tar.gz > SHA256SUMS )
```

Die Ausschlüsse betreffen das Datenvolume: Kopplungs-, Sperr- und Markerdateien sowie offene Planaufträge werden nicht übertragen, also auch nicht die Not-Aus-Markierung.

Den Container des Altsystems danach nicht wieder starten und dort kein Update ausführen, solange das neue System regelt.

## B4. Neues System vorbereiten und einspielen

1. Docker installieren und das Repository klonen (siehe [Quickstart](Quickstart.md), Variante B), Image-Tag wie auf dem Altsystem. **Noch nicht starten.**
2. Container und Volumes anlegen, ohne zu starten:
   ```bash
   sudo docker compose create
   sudo docker volume ls
   ```
3. Sicherung übertragen (`scp`) und prüfen: `( cd umzug && sha256sum -c SHA256SUMS )`.
4. Einspielen:
   ```bash
   for v in <projekt>_e3dc_data <projekt>_e3dc_logs <projekt>_e3dc_ml; do
     sudo docker run --rm -v "$v":/ziel -v "$PWD/umzug":/quelle:ro debian:bookworm-slim \
       tar --numeric-owner -xpzf "/quelle/$v.tar.gz" -C /ziel
   done
   ```
   Das Volume `e3dc_instance_role` bleibt leer; der erste Start legt die Rollenbindung für `ha_mode=off` an.
5. `e3dc_ml` nur einspielen, wenn Image-Tag und Architektur gleich sind. Sonst die Sicherung weglassen; der Container trainiert beim Start aus der Historie.
6. Starten und prüfen:
   ```bash
   sudo docker compose up -d
   sudo docker compose ps
   sudo docker compose logs --tail=100 e3dc-control
   ```
   Der Container wird nach dem Start gesund (`healthy`).

## B5. Prüfung

- Container gesund, Weboberfläche erreichbar, Systemstatus ohne Fehler.
- Statistik: Zeilenzahl und Datumsbereich von `daily_stats` und Anzahl der Tageshistorien wie auf dem Altsystem (Befehle aus B3).
- Lernmodell mit dem festen Launcher prüfen (siehe [Docker-Dokumentation](Docker_Dokumentation.md), Abschnitt „ML-Prognose fehlt“): `ml_predictor.py --predict`, bei Bedarf `--train`. Ein direkter Aufruf als Root ist nicht zulässig.
- Ladepunkte: Pause und Ladepunktsperre sind übernommen; ein notiertes Not-Aus erneut auslösen, erst danach Fahrzeuge anstecken. Matter-Kopplung ggf. neu herstellen.
- Bei der Volume-Kopie bleibt eine bestätigte Hausabsicherung erhalten; den Wert im Config-Editor kontrollieren.
- Beobachtungsphase wie in A8 Schritt 6.

## B6. Rückweg

1. Neues System stilllegen: `sudo docker compose stop e3dc-control`, Watchtower ebenfalls stoppen.
2. Altsystem im eigenen Compose-Ordner neu erstellen und starten; die Volumes bleiben dabei unverändert:
   ```bash
   sudo docker compose up -d --force-recreate e3dc-control
   sudo docker compose ps
   ```
3. Nach dem Umzug neu entstandene Daten werden nicht zurückgeführt.

---

# Grenzen

- Es gibt keinen Umzugsassistenten; die Schritte nutzen ausschließlich vorhandene Werkzeuge (Config-Editor-Export und -Import, SQLite-Sicherungsschnittstelle, Rechte-Reparatur, Neutraining, Compose).
- Zwischen Stilllegung und Neustart des neuen Geräts regelt E3DC-Control nicht. Speicher, Wallbox und Wärmepumpe arbeiten in dieser Zeit ohne Vorgaben von E3DC-Control.
- Das Laden eines Lernmodells über Architektur- oder Bibliotheksgrenzen (32 → 64 Bit, andere scikit-learn-Version) ist nicht zugesichert. Der sichere Weg ist das Neutraining.
- Nicht übernommen werden Matter-Kopplung, Not-Aus-Markierung, Update- und Rollenbelege sowie der Betriebszustand der Aktoren.
- Ein Docker-Hostwechsel über Architekturgrenzen (`arm64` ↔ `amd64`) und ein Wechsel zwischen Bare Metal und Docker auf verschiedenen Geräten sind nicht beschrieben.
