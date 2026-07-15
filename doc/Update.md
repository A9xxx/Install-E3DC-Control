# Update-Prozess

Updates werden ausschliesslich über den Installer ausgeführt. Ein manuelles
`git pull --ff-only` ist für den einmaligen Wechsel auf die bereinigte
Release-Historie ungeeignet, weil alter und neuer Git-Stand nicht miteinander
verwandt sein muessen.

Die Konsolenbeispiele verwenden den zuvor geprueften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Normales Update

Der bevorzugte Weg ist der Update-Button der Weboberflaeche. Auf der Konsole
steht derselbe Installer-Pfad zur Verfuegung:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
bash "$E3DC_INSTALL_PATH/e3dc-setup" --update-e3dc
```

Wenn `--check` eine fehlende Web-/sudo-Freigabe meldet:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

## Einmaliger Wechsel von alten Installationen

Der Bootstrap gilt auch für V4.0.1 bis V4.0.5 sowie für V3-/ZIP-Staende ohne
`.git`. Lade das veroeffentlichte Release-Archiv in ein temporaeres Verzeichnis
und pruefe dessen veroeffentlichte SHA-256. Notiere ausserdem den vollstaendigen
40-stelligen Commit-SHA und die bestehende HA-/Shadow-Rolle (`off`, `master`,
`slave` oder `shadow`).

```bash
/tmp/e3dc-release/e3dc-bootstrap \
  "$E3DC_INSTALL_PATH" \
  vX.Y.Z \
  0123456789abcdef0123456789abcdef01234567 \
  off
```

Tag, SHA und Rolle werden durch die auf der Release-Seite freigegebenen Werte
und die zuvor geprüfte Rolle ersetzt. Der Launcher verlangt Python 3.10 oder
neuer und beendet sich bei einer aelteren Laufzeit, bevor der Zielbaum
veraendert wird. Bei einer V3-Installation kann der absolute Zielpfad zum
Beispiel `$HOME/E3DC-Control` sein.

Der Bootstrap akzeptiert nur einen annotierten Tag, dessen aufgeloester Commit
exakt dem angegebenen SHA entspricht. Er sichert zuerst den tatsaechlichen
Zielbaum. Erst nach Manifest- und Pruefsummengate werden Git initialisiert und
der neue Release-Stand eingespielt. Ein vorhandener alter Git-Verlauf muss
keinen gemeinsamen Vorfahren mit dem neuen Verlauf besitzen.

## Harte Gates

Der Installer fuehrt den Wechsel in dieser Reihenfolge aus:

1. externes Backup mit vollstaendigem Manifest und SHA-256 erstellen;
2. alle katalogisierten Writer-/Integrationsdienste und den Watchdog stoppen;
3. annotierten Ziel-Tag und vollstaendigen Ziel-SHA gegen manuelle Freigabe
   beziehungsweise die Rollback-Policy binden;
4. `UPDATE_POLICY.json` direkt aus dem verifizierten Commit-Objekt lesen;
5. den Installationsbaum auf genau diesen SHA setzen und `HEAD` erneut prüfen;
6. Webdateien synchronisieren und veraltete Pfade nur über feste Positivlisten
   entfernen;
7. eingefrorene HA-/Shadow-Rolle und Feature-Konfiguration, alle erwarteten
   Dienste, lokale HTTP-Endpunkte und Boot-Sanity hart prüfen.

Scheitert ein Gate nach einer Änderung, setzt der Installer den alten Git-Stand
zurueck, entfernt bei einem ZIP-Bootstrap die neu angelegte `.git`-Struktur,
stellt das Sicherheits-Backup wieder her und prueft Rolle, Dienste und HTTP
erneut. Ist diese Wiederherstellung nicht vollstaendig beweisbar, bleiben die
Writer-/Aktor-Dienste gestoppt.

## Gezielter Rückfall

Der bereinigte Stand `v5.3.2b` ist selbst der Rollback-Root und gibt keinen
älteren öffentlichen Tag frei. Ein späterer Stable-Stand darf `v5.3.2b` als
einzigen Rollback anbieten. Freie Commit-Hashes sind nie Rückfallversionen;
jeder angebotene Tag ist in der Policy an genau einen SHA gebunden.

## Docker-Update

```bash
export E3DC_DOCKER_PATH="/absoluter/pfad/zur/docker-installation"
cd "$E3DC_DOCKER_PATH"
docker compose pull
docker compose up -d --force-recreate
docker compose ps
```

Ein Docker-Rückfall verwendet ausschliesslich einen freigegebenen Image-Tag
beziehungsweise den verifizierten Digest. Der Container steuert den Host-Docker
nicht selbst.
