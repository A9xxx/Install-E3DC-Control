# E3DC-Control v5.4.1d

E3DC-Control 5.4.1d ist ein eng begrenztes Wartungsrelease für
Klimaverbrauchsmessung, Update/Backup und Batterie-Vitals. Speicher-,
Wallbox-, Wärme- und Direktvermarktungsentscheidungen entsprechen unverändert
5.4.1c.

Der Service-Worker trägt ebenfalls die 5.4.1d-Kennung. Browser verwerfen damit
den alten statischen Cache-Namensraum beim Releasewechsel eindeutig; an der
Frontend- oder DV-Logik wird dabei nichts geändert.

## Klima im Docker-Container

- Der read-only Worker `climate_live.py` startet im Container genau einmal.
- Aktivierung, Deaktivierung und Shelly-Kanalwechsel werden in jedem
  Abfragezyklus erneut aus der Konfiguration gelesen; dafür ist kein
  Containerneustart erforderlich.
- `climate_enable=0` bleibt fail-closed: keine Shelly-Abfrage, keine
  Klimasteuerung und keine neue Verlaufshistorie.
- Bestehende Klimahistorien werden beim Deaktivieren weder gelöscht noch
  verändert.

## ML-Lock und verifiziertes Backup

- Vor dem Backup prüft der Updater den privaten ML-Store weiterhin vollständig,
  ohne ein Modell zu deserialisieren.
- Ausschließlich ein regulärer, unverlinkter, höchstens 64 KiB großer,
  aktuell unbelegter Alt-Lock mit gebundenem oder historischem Root-Eigentümer
  darf auf Installationsbenutzer, Store-Gruppe und Modus `0600` normalisiert
  werden.
- Inode, Pfad, Größe und Lockzustand werden vor und nach der
  Metadatenkorrektur erneut gebunden. Lockinhalt, Modell und Manifest bleiben
  unverändert.
- Symlinks, Hardlinks, fremde Eigentümer, Übergröße, unbekannte private
  Einträge und belegte Locks brechen den Updatepfad weiterhin hart ab.

### Wichtig für bereits blockierte Alt-Updater

Stände bis einschließlich 5.4.1c erstellen und prüfen das Backup noch mit
ihrem bereits installierten Code, bevor sie 5.4.1d laden. Wenn ein solcher
Updater mit

`Unsicherer privater ML-Eintrag: .ml_model.lock`

abbricht, ist deshalb einmalig der dokumentierte Metadaten-Feldfix aus
`doc/Update.md` erforderlich. `.ml_model.lock`, Modell und Manifest dürfen
nicht gelöscht oder umbenannt werden. Ab dem erfolgreich installierten
5.4.1d-Stand kann der Updater einen eindeutig sicheren Alt-Lock selbst
normalisieren.

## Batterie-Vitals

- Die Anfrage jedes DCB-Packs bleibt als `Uint16` indexiert.
- Ein vom E3/DC zurückgegebener `BAT_DCB_INDEX` wird zusätzlich zum bisherigen
  `Uint16` auch als RSCP-`Int32` akzeptiert.
- Die Sicherheitsbindung bleibt unverändert streng: echter Ganzzahlwert,
  Bereich `0…65535` und exakte Gleichheit mit dem angeforderten Packindex.
- Negative, nichtnumerische, vertauschte oder anders typisierte Antworten
  werden weiterhin verworfen und niemals unter einem falschen Pack angezeigt.

## Installation und Update

Bare-Metal-Nutzer verwenden den Web- oder Konsolen-Updater. Ein bereits am
ML-Lock gestoppter Altstand führt zuerst ausschließlich die dokumentierte
Metadatenreparatur aus und startet danach den normalen Updater erneut.

Docker-Nutzer prüfen das konfigurierte Image und recreaten erst nach einem
erfolgreichen Pull:

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

Ein fester Eintrag `E3DC_IMAGE_TAG` bleibt absichtlich fest. Für v5.4.1d
lautet der Pin `E3DC_IMAGE_TAG=v5.4.1d`; ohne Pin folgt die Compose-Datei dem
erst nach erfolgreicher Attestierungsprüfung gesetzten Stable-Tag `latest`.

Der öffentliche Docker-Rückfallstand bleibt `v5.3.2b`. Dieser Stand ist nicht
als Bare-Metal-Programm-Rückfall freigegeben; dort bleibt ein verifiziertes
Datei-Backup der sichere Rückweg.
