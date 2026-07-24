# E3DC-Control v5.4.1

E3DC-Control 5.4.1 ist ein Konsolidierungsrelease für Wallboxen, Installation,
Docker und Betriebsdiagnose. Der Umfang wurde nach den Feldtests eingefroren;
neue experimentelle Regelpfade sind nicht Bestandteil dieses Releases.

## openWB Pro und Mehr-Wallbox-Balancing

- Start, Pause, Ladeende und Phasenwechsel sind an eine frische Stecksession,
  Sollabsicht und bestätigte Gerätewerte gebunden.
- Ein Phasenwechsel setzt zunächst 0 A, wartet auf den bestätigten Stillstand,
  übergibt das Phasenziel und gibt danach den zulässigen Ladestrom frei.
- Die persistente Sperre von mindestens 480 Sekunden verhindert ausschließlich
  einen weiteren Phasenwechsel. Sie blockiert weder den bestätigten
  Wiederanlauf noch die laufende Ladung.
- Fahrzeug-, Wallbox- und Nutzergrenzen werden gemeinsam ausgewertet.
  Ein Fahrzeug mit 11 kW und drei Phasen wird dadurch auf rund 16 A je Phase
  begrenzt, auch wenn der Ladepunkt 32 A anbietet.
- Mehrere Ladepunkte werden leistungsfair anhand der tatsächlichen Phasenzahl
  verteilt. Ein- und dreiphasige Amperewerte werden nicht pauschal addiert.
- Die physische Zuordnung der lokalen Wallboxphasen zum Netzanschlusspunkt ist
  konfigurierbar. Ohne einen echten, frischen PCC-RMS-Stromvektor bleibt eine
  dynamische einphasige Freigabe über 20 A aus Sicherheitsgründen gesperrt.
- Der Ioniq-5-SoC-Fallback verwendet nur die Energie der aktuellen
  Stecksession. Echte Fahrzeug- oder Wallboxwerte haben immer Vorrang.

## Update und Docker

- Der unterstützte Erstwechsel aus 5.3.2b läuft ausschließlich über den
  verifizierten Installer-/Bootstrapweg. Konfiguration, Rollen und bereits
  installierte optionale Dienste werden vor dem Wechsel gebunden.
- Alte Konfigurationsfelder installieren oder aktivieren während des Updates
  keine bisher fehlenden Wallbox-, Wärme- oder Integrationsdienste.
- Python-Abhängigkeiten bleiben im verwalteten Benutzer-venv; es gibt keinen
  System-`pip`-Eingriff und kein `--break-system-packages`.
- Der Docker-Installer prüft offizielle Paketquellen, Compose-Fähigkeit,
  Zielimage, Pull-Ergebnis und gestartete Containerversion explizit.
- Ein fehlgeschlagener Pull darf kein vorhandenes Altimage als erfolgreiches
  Update melden.
- Watchtower ist wegen des weitreichenden Docker-Socket-Zugriffs kein
  Standarddienst mehr. Er bleibt nur als bewusstes Compose-Profil verfügbar.
- Der GitHub-Workflow baut AMD64 und ARM64 als einen Kandidatendigest, prüft
  SBOM und Provenance und setzt erst danach die unveränderlichen Tags
  `v5.4.1`, `5.4.1` und `latest`.

## Frontend, Diagnose und Sicherheit

- Netzfrequenz sowie aktive SG-Ready-/Shelly-Freigaben werden im Dashboard
  sichtbar dargestellt.
- Gleichzeitig aktive E3/DC-Wetterladung wird als externer Vetozustand für die
  eigene Speicherregelung erkannt. E3/DC-Einstellungen werden dabei nicht
  automatisch verändert.
- Neue optionale Statuswerte bleiben bei fehlender oder ungültiger Quelle
  unbekannt, statt als echte `0` ausgegeben zu werden.
- Gemeldete Status- und Fehlertexte im Konfigurationseditor werden als Text
  dargestellt und nicht als ungeprüftes HTML eingefügt.
- Der optionale Legacy-Preisfallback bleibt für den Webserver lesbar. Fehlt die
  Datei oder ist sie vorübergehend nicht lesbar, bleibt das gesamte Frontend
  einschließlich Vitals erreichbar.
- Jeder Batterie-DCB-Pack wird mit seinem typisierten Packindex gelesen und,
  wenn vorhanden, an den Antwortindex gebunden. Der erste Pack wird dadurch
  nicht mehr mehrfach unter verschiedenen Nummern angezeigt.
- Der WebSocket-Dienst bindet nur noch an `127.0.0.1:8765`; das Dashboard nutzt
  den gleichursprünglichen Webserver-Pfad `/ws`.

## Update

Bare-Metal-Nutzer installieren v5.4.1 über den Web- oder Konsolen-Updater.
Für den einmaligen Wechsel aus 5.3.2b gelten weiterhin die Stopbedingungen und
die genaue Befehlskette der
[Update-Anleitung](https://github.com/A9xxx/Install-E3DC-Control/blob/v5.4.1/doc/Update.md).
Ein abgebrochener Update-, Backup- oder Rechtepfad erhält den zuletzt
konsistenten Zustand und gilt nicht als Erfolg.

Docker-Nutzer prüfen zuerst das konfigurierte Image und recreaten ausschließlich
nach einem erfolgreichen Pull:

```bash
(
  set -euo pipefail
  docker compose config --images
  docker compose pull e3dc-control
  docker compose up -d --force-recreate e3dc-control
  docker inspect e3dc-control --format '{{.Config.Image}} {{.State.Status}}'
  docker exec e3dc-control cat /app/pi/Install/VERSION
)
```

Ein fester Eintrag `E3DC_IMAGE_TAG` bleibt absichtlich fest. Für v5.4.1 lautet
der Pin `E3DC_IMAGE_TAG=v5.4.1`; ohne Pin folgt die Compose-Datei dem nach der
Attestierungsprüfung gesetzten Stable-Tag `latest`.

Der öffentliche Docker-Rückfallstand bleibt `v5.3.2b`. Dieser Stand ist nicht
als Bare-Metal-Programm-Rückfall freigegeben; dort bleibt ein verifiziertes
Datei-Backup der sichere Rückweg.
