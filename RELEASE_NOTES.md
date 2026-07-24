# E3DC-Control v5.4.1c

E3DC-Control 5.4.1c korrigiert ausschließlich den semantischen
OCI-Release-Verifier. Die erwartete, bereits durch Workflow, Tag, `VERSION`,
Commit und Tree gebundene Releaseversion wird nun streng syntaktisch geprüft,
statt zusätzlich in einer bei jedem Release manuell zu erweiternden Liste zu
stehen. Produktbytes und Regelung entsprechen unverändert 5.4.1a.

## Docker-Release

- Der Topologieprüfer lädt weiterhin die vollständige Historie und bindet den
  parentlosen Veröffentlichungs-Root.
- Der OCI-Verifier akzeptiert die vom Workflow exakt gebundene 5.4.1c-Version
  und weist leere, mehrteilige, großgeschriebene oder anderweitig unzulässige
  Versionsformen weiterhin zurück.
- Das Gate bleibt fail-closed: Commit, Tree, Version, Noreply-Identität,
  vollständige Abstammung und Quellenmanifest müssen weiterhin exakt passen.
- Der 5.4.1b-Lauf erzeugte einen vollständigen temporären Multiarch-Kandidaten
  samt SBOM und Provenance, endete aber vor Stable-Tag-Promotion an der
  veralteten Versionsliste.

## Änderungen aus 5.4.1a und 5.4.1b

## Web-Update

- Der Web-Updater erhält neben dem Log einen strukturierten Exitcode und
  Abschlussstatus.
- Der kanonische Installer-Marker
  `[OK] self-update auf <Commit-SHA> abgeschlossen.` bleibt als
  Kompatibilitätsbeleg erhalten.
- Ein laufender Prozess, ein Fehlertext oder ein von null verschiedener
  Exitcode kann nicht als Erfolg erscheinen.
- Nach Prozessende wartet die Oberfläche kurz auf die letzte Status- und
  Logpublikation, statt vorschnell einen unklaren Fehler auszugeben.

## ML-Backup

- Eine neu erzeugte `.ml_model.lock` erhält unmittelbar den gebundenen
  Installationsbenutzer und Modus `0600`.
- Die Rechteprüfung kann ausschließlich einen regulären, unverlinkten,
  größenbegrenzten und aktuell nicht belegten Alt-Lock normalisieren.
- Symlinks, Sonderdateien, Hardlinks, fremde Eigentümer, Übergröße und ein
  belegter Lock bleiben ohne Änderung gesperrt.
- Modell- und Manifestbytes werden vor der Reparatur vollständig validiert,
  aber weder deserialisiert noch verändert.
- Ein bereits durch diesen Altbestand blockierter 5.4.0e-/5.4.1-Updater kann
  den neuen Code nicht selbst erreichen, weil er sein Backup vorher prüft.
  Dafür gilt einmalig die eng begrenzte SSH-Feldreparatur aus der
  Update-Anleitung.

## Frische Erstinstallation

- Der normale Einstieg `e3dc-setup` entfernt fremde oder unvollständige
  Release-Bootstrap-Variablen.
- Er übergibt ausschließlich den tatsächlichen Installationsbenutzer an den
  privilegierten Installationsprozess.
- Der echte Release-Bootstrap bleibt weiterhin an getrennten Runner- und
  Zielbaum, annotierten Tag und vollständige Ziel-SHA gebunden.
- Unvollständige, identische oder fremde Bootstrap-Bindungen bleiben
  fail-closed.

## Empfohlene E3/DC-Einstellungen

- Die E3/DC-eigene Wetterladung sollte ausgeschaltet sein, wenn E3DC-Control
  die Speicher-Ladekurve führt.
- Open-Meteo und die Dachflächenprognose von E3DC-Control bleiben davon
  unabhängig aktiv.
- RSCP-Zugang, physische Notstromreserve sowie Netz-, Batterie- und
  Wechselrichtergrenzen bleiben aktiv.
- Pro Speicher beziehungsweise Wallbox darf nur ein Regler Hardwarebefehle
  ausgeben.
- E3/DC und Host benötigen eine gemeinsame korrekte Uhrzeit, Zeitzone und
  NTP-Synchronisation.

## Installation und Update

Bare-Metal-Nutzer verwenden den Web- oder Konsolen-Updater. Ein bereits am
ML-Lock gestoppter Altstand führt zuerst die ausdrücklich dokumentierte
Feldreparatur aus und startet danach den normalen Updater erneut.

Docker-Nutzer prüfen das konfigurierte Image und recreaten ausschließlich nach
einem erfolgreichen Pull:

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

Ein fester Eintrag `E3DC_IMAGE_TAG` bleibt absichtlich fest. Für v5.4.1c
lautet der Pin `E3DC_IMAGE_TAG=v5.4.1c`; ohne Pin folgt die Compose-Datei dem
erst nach erfolgreicher Attestierungsprüfung gesetzten Stable-Tag `latest`.

Der öffentliche Docker-Rückfallstand bleibt `v5.3.2b`. Dieser Stand ist nicht
als Bare-Metal-Programm-Rückfall freigegeben; dort bleibt ein verifiziertes
Datei-Backup der sichere Rückweg.
