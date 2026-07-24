# Update-Prozess

Updates werden ausschließlich über den Installer ausgeführt. Ein manuelles
`git pull --ff-only` ist für den einmaligen Wechsel auf die bereinigte
Release-Historie ungeeignet, weil alter und neuer Git-Stand nicht miteinander
verwandt sein müssen.

Die Konsolenbeispiele verwenden den zuvor geprüften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Normales Update

Der bevorzugte Weg ist der Update-Button der Weboberfläche. Auf der Konsole
steht derselbe Installer-Pfad zur Verfügung:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
bash "$E3DC_INSTALL_PATH/e3dc-setup" --update-e3dc
```

Wenn `--check` eine fehlende Web-/sudo-Freigabe meldet:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

Ist bei einer 5.3.2b-Installation bereits der privilegierte
Web-Launcher fehlend oder nicht ausführbar, kann die Weboberfläche genau
diesen Einstieg nicht selbst reparieren. Dann ist einmalig eine interaktive
SSH-Konsole erforderlich. Bei der üblichen Installation ist folgende Kette
vollständig kopierbar:

```bash
export E3DC_INSTALL_PATH="$HOME/Install"
test -f "$E3DC_INSTALL_PATH/installer_main.py"
test -x "$HOME/.venv_e3dc/bin/python3"
cd "$E3DC_INSTALL_PATH"
sudo /usr/bin/python3 installer_main.py --fix-permissions
sudo /usr/bin/python3 installer_main.py --check
sudo /usr/bin/python3 installer_main.py --update-e3dc
cat VERSION
systemctl --failed --no-pager
```

Für den einmaligen Wechsel aus 5.3.2b sind ausschließlich der
Web-Update-Button oder der oben gezeigte direkte Aufruf mit
`--update-e3dc` freigegeben. Der interaktive Installer-Menüpunkt lädt im
Altprozess bereits vor dem Git-Wechsel zusätzliche Module und darf für diesen
Hybridübergang nicht verwendet werden.

Liegt E3DC-Control nicht unter `$HOME/Install`, muss ausschließlich die erste
Zeile an den tatsächlichen absoluten Installationspfad angepasst werden. Eine
Passwortabfrage von `sudo` ist an der interaktiven SSH-Konsole in diesem Fall
normal. Schlägt eine der beiden `test`-Zeilen fehl, bitte dort stoppen und
weder System-`pip` noch `--break-system-packages` verwenden. Nach der
erfolgreichen Reparatur steht der reguläre Web-Update-Pfad wieder zur
Verfügung.

Python-Abhängigkeiten werden bei einem Release-Wechsel ausschließlich im
gebundenen Benutzer-venv installiert. Auf Debian-Systemen mit PEP 668 wird
deshalb kein System-`pip` und kein `--break-system-packages` verwendet. Fehlt
das Standard-venv, darf der neue Installer nach Installation von
`python3-venv` genau dieses venv im Home des Installationsbenutzers neu
anlegen. Beim direkten ersten Wechsel aus 5.3.2b läuft bis zum
Git-Wechsel noch der alte Prozess; für diesen ersten Schritt muss das
gebundene Benutzer-venv bereits vorhanden sein. Andernfalls bricht der
Altprozess ab und stellt den Ausgangszustand wieder her. Abweichende oder
mehrdeutige venv-Pfade brechen den Updatevorgang ebenfalls ab.

In einer Docker-Installation führen weder Weboberfläche noch Konsole einen
Release-Wechsel im laufenden Container aus. Sie zeigen stattdessen die drei
Host-Befehle aus dem Abschnitt [Docker-Update](#docker-update).

## Einmaliger Wechsel von alten Installationen

Der Bootstrap gilt auch für 5.3.2a, V4.0.1 bis V4.0.5 sowie für V3-/ZIP-Stände
ohne `.git`. Diese Stände wechseln zuerst auf die bewusst dafür veröffentlichte
Übergangsbasis 5.3.2b und führen danach deren regulären Updatepfad aus. Lade
das veröffentlichte 5.3.2b-Release-Archiv in ein temporäres Verzeichnis
und prüfe dessen veröffentlichte SHA-256. Notiere außerdem den vollständigen
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
neuer und beendet sich bei einer älteren Laufzeit, bevor der Zielbaum
verändert wird. Bei einer V3-Installation kann der absolute Zielpfad zum
Beispiel `$HOME/E3DC-Control` sein.

Der Bootstrap akzeptiert nur einen annotierten Tag, dessen aufgelöster Commit
exakt dem angegebenen SHA entspricht. Er sichert zuerst den tatsächlichen
Zielbaum. Erst nach Manifest- und Prüfsummengate werden Git initialisiert und
der neue Release-Stand eingespielt. Ein vorhandener alter Git-Verlauf muss
keinen gemeinsamen Vorfahren mit dem neuen Verlauf besitzen.

## Harte Gates

Sobald der neue Updater selbst läuft, führt er den Wechsel in dieser
Reihenfolge aus:

1. externes Backup mit vollständigem Manifest, SHA-256 und dem Zustand
   kanonischer systemd-Masken erstellen;
2. alle katalogisierten Writer-/Integrationsdienste und den Watchdog stoppen;
3. annotierten Ziel-Tag und vollständigen Ziel-SHA gegen manuelle Freigabe
   beziehungsweise die Rollback-Policy binden;
4. `UPDATE_POLICY.json` direkt aus dem verifizierten Commit-Objekt lesen;
5. den Installationsbaum auf genau diesen SHA setzen und `HEAD` erneut prüfen;
6. einen eigenen Target-Finalizer aus dem verifizierten Zielbaum gegen
   Ziel-SHA, Ziel-Tag und gebundene Ausgangsdaten prüfen und starten;
7. Webdateien synchronisieren und veraltete Pfade nur über feste Positivlisten
   entfernen;
8. eingefrorene HA-/Shadow-Rolle und Feature-Konfiguration, alle erwarteten
   Dienste, lokale HTTP-Endpunkte und Boot-Sanity hart prüfen.

Beim direkten ersten Wechsel aus 5.3.2b bleibt der bereits
gestartete Altprozess bis zum Abschluss aktiv. Nach dem Git-Wechsel importiert
er die neue, dienstneutrale Rechteprüfung; der zielgebundene Finalizer gilt ab
dem anschließend laufenden neuen Updater. Der erste Wechsel behauptet deshalb
keine nachträgliche Ausführung eines Finalizers, den der Altprozess noch nicht
kennt.

Für diesen ersten Hybridwechsel enthält die Zielpolicy ausschließlich die
sieben Pflichtdienste. Der Altprozess erfasst die vor dem Wechsel bereits
installierten Zusatzdienste und die gebundene HA-/Shadow-Rolle. Nur die in der
eingefrorenen Konfiguration aktiven Zusatzdienste werden gestartet;
deaktivierte Zusatzdienste bleiben aus. Eine
vorbereitete Konfiguration allein installiert oder startet keine bislang
fehlende Wallbox-, Wärme- oder Integrationssteuerung. Solche konfigurierten,
aber nicht installierten Zusatzmodule werden im Updateprotokoll genannt und
können nach dem Release-Wechsel bewusst über das Install-Center eingerichtet
werden.

Scheitert ein Gate nach einer Änderung, setzt der Installer den alten Git-Stand
zurück, entfernt bei einem ZIP-Bootstrap die neu angelegte `.git`-Struktur,
stellt das Sicherheits-Backup wieder her und prüft Rolle, Dienste und HTTP
erneut. Ist diese Wiederherstellung nicht vollständig beweisbar, bleiben die
Writer-/Aktor-Dienste gestoppt.

## Gezielter Rückfall

`v5.4.1` bietet den bereinigten Root `v5.3.2b` ausschließlich als
Docker-Rückfall-Image an. Dieser Root gibt selbst keinen älteren öffentlichen
Tag frei. Auf Bare Metal wird `v5.3.2b` nicht als Programm-Rückfall angeboten,
weil der Altstand keinen zielgebundenen Release-Finalizer enthält. Freie
Commit-Hashes oder ein manueller Checkout sind kein Ersatz; dort bleibt die
Wiederherstellung eines verifizierten Datei-Backups der sichere Rückweg.

## Docker-Update

```bash
(
  set -euo pipefail
  export E3DC_DOCKER_PATH="/absoluter/pfad/zur/docker-installation"
  cd "$E3DC_DOCKER_PATH"
  docker compose config --images
  docker compose pull e3dc-control
  docker compose up -d --force-recreate e3dc-control
  docker compose ps
)
```

Der Web-Updater erkennt den Containerkontext auch über den Marker des
offiziellen Images und zeigt diese Befehle an. Er benötigt keinen Zugriff auf
den Docker-Socket und versucht bewusst nicht, den eigenen Container zu
ersetzen.

Der optionale Watchtower-Dienst startet nicht zusammen mit der
Standardanwendung. Das Upstream-Projekt wird nicht mehr gepflegt und sein
Docker-Socket-Zugriff ermöglicht weitreichende Kontrolle über den Docker-Host.
Der Dienst bleibt nur für bestehende Installationen im Compose-Profil
`auto-update`. Der bewusste Opt-in lautet:

```bash
docker compose --profile auto-update up -d watchtower
```

Watchtower berücksichtigt dabei durch den Enable-Label-Filter ausschließlich
den E3DC-Control-Container. Ein bereits aus einer älteren Compose-Datei
laufender Watchtower kann mit
`docker compose --profile auto-update stop watchtower` und anschließend
`docker compose --profile auto-update rm -f watchtower` entfernt werden.

Die mitgelieferte Compose-Datei verwendet standardmäßig
`ghcr.io/a9xxx/install-e3dc-control:${E3DC_IMAGE_TAG:-latest}`. Ein in der
Compose-Datei oder über `.env` fest eingestellter Versions-Tag bleibt bei
`pull` unverändert. `docker compose config --images` muss deshalb vor dem
Update den gewünschten Stable-Tag oder `latest` anzeigen.

Ein Docker-Rückfall verwendet ausschließlich einen Eintrag mit
`docker_supported: true`, dessen freigegebenen Image-Tag beziehungsweise den
verifizierten Digest. Der Container steuert den Host-Docker nicht selbst.
