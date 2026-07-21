# Upgrade alter V3-/V4-Installationen

Diese Anleitung gilt für alte Installationen mit C++-Kern, für V4.0.1 bis
V4.0.5 und für entpackte V3-/ZIP-Stände ohne Git-Metadaten. Ein direktes
`git pull --ff-only` darf für diesen einmaligen Übergang nicht verwendet
werden, weil die bereinigte Historie keinen gemeinsamen Vorfahren haben muss.

## Vorbereitung

1. Ermittle den absoluten Installationspfad und setze ihn für die folgenden
   Beispiele, etwa:

   ```bash
   export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
   test -f "$E3DC_INSTALL_PATH/e3dc.config.txt" -o -f "$E3DC_INSTALL_PATH/data/e3dc_v4.json"
   ```

2. Lade das veröffentlichte Release-Archiv in ein temporäres Verzeichnis und
   prüfe dessen veröffentlichte SHA-256.
3. Notiere den vollständigen freigegebenen Commit-SHA.
4. Ermittle die aktuelle HA-/Shadow-Rolle. Bei fehlender oder unlesbarer Rolle
   darf der Bootstrap nicht gestartet werden.

## Geprüften Bootstrap starten

```bash
/tmp/e3dc-release/e3dc-bootstrap \
  "$E3DC_INSTALL_PATH" \
  vX.Y.Z \
  0123456789abcdef0123456789abcdef01234567 \
  off
```

Ersetze Tag, 40-stelligen Commit-SHA und Rolle durch die auf der Release-Seite
geprüften Werte. Der Launcher benötigt Python 3.10 oder neuer und prüft dies
vor jeder Änderung.

Der Installer erstellt vor jeder Git-Änderung ein externes Backup mit Manifest
und SHA-256. Danach stoppt er alle Writer-/Integrationsdienste, initialisiert
Git bei Bedarf, setzt den Baum auf den exakt freigegebenen Release-SHA und
prüft Dienste, lokale HTTP-Endpunkte, HA-/Shadow-Rolle und Boot-Sanity.

Scheitert Git-Initialisierung, Migration oder ein nachgelagertes Gate, wird der
vorherige Baum automatisch aus Git und dem Sicherheits-Backup
wiederhergestellt. Nur wenn auch dieser Nachweis scheitert, bleiben die
Writer-/Aktor-Dienste gestoppt.

Nach einem erfolgreichen Übergang wird der lokale Installer über seinen
portablen Einstieg gestartet:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

Weitere Details stehen in [Update](Update.md), [Backup](Backup.md) und
[Rollback](Rollback.md).
