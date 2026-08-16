# E3DC-Control Installer

Dokumentation Stand: 5.4.3h

Der Installer verwaltet Bare-Metal-Installation, Update, Rechte, Dienste,
Backup, Rollback und optionale Produktmodule. Er ermittelt Benutzer, Home,
Installationspfad und Python-Umgebung aus dem geprüften Installationskontext.

Mit 5.4.3h darf die Weboberfläche das reguläre System-Update wieder über
einen argumentlosen, root-eigenen Launcher starten. Freie Aktionen, Pfade,
Tags, Reparaturen, Neuinstallationen und Rückfälle bleiben im Web gesperrt.
Installationen bis einschließlich 5.4.3f benötigen für diesen einmaligen
Übergang noch den administrativen Konsolenweg aus [Update.md](Update.md).

Seit 5.4.2d bewertet der Updatepfad den Wiederanlauf erforderlicher Dienste
anhand des belegten systemd-Endzustands. Nicht installierte optionale Units
brechen den verifizierten Maskenrücklauf nicht allein wegen einer abweichenden
systemd-Textausgabe ab. Echte Start-, Masken- oder
Wiederherstellungsabweichungen bleiben harte, fail-closed Abbruchgründe.

## Portabler Einstieg

Setze für Konsolenbeispiele den tatsächlichen absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

Danach stehen dieselben Aktionen wie im Web-Installationscenter bereit:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
bash "$E3DC_INSTALL_PATH/e3dc-setup" --update-e3dc
bash "$E3DC_INSTALL_PATH/e3dc-setup" --reinstall-current
```

`e3dc-setup` validiert den Produkt-Root, übernimmt bei Bedarf die nötigen
Rechte und startet den Installer mit dem zur Installation gehörenden
Interpreter. Releasewechsel selbst laufen mit dem root-kontrollierten
System-Python im isolierten, ungepufferten Modus; das Benutzer-venv bleibt auf
die gebundenen Zielabhängigkeiten begrenzt. Ein manuelles `git pull` ersetzt
diesen Weg nicht.

## Frische Installation auf Raspberry Pi OS Bookworm

Die klassische Installation wird aus einem normalen Benutzerkonto mit
`sudo`-Rechten gestartet. Für ein Standardkonto und den üblichen Zielpfad gilt:

```bash
sudo apt-get update
sudo apt-get install -y git
export E3DC_INSTALL_PATH="$HOME/Install"
git clone https://github.com/A9xxx/Install-E3DC-Control.git "$E3DC_INSTALL_PATH"
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

`--check` prüft nur den lokalen Produkt-Root, den freigegebenen
Bootstrap-Interpreter und die Ausführbarkeit des Installer-Einstiegs. Erst
`Installation / Update` prüft und installiert Paket-, Web-, Rechte- und
Dienstabhängigkeiten. Der Paketvertrag enthält auch `rsync`, weil der
Webportal-Schritt damit die Programmdateien nach `/var/www/html` projiziert.
Die Komplettinstallation benötigt ein Benutzer-venv im per `passwd`
gebundenen Home-Verzeichnis. Wird das venv abgelehnt oder die abschließende
Bestätigung nicht erteilt, endet der Lauf vor dem Schreiben der venv-, Pfad-
und Webmetadaten mit einem Fehlerstatus.
Die Kern-Units werden erst nach Konfiguration, RAM-Disk und Initialbackup
gebündelt eingerichtet; der Paket-Schritt startet den WebSocket-Dienst nicht
vorzeitig.

Eine vollständige Bare-Metal-Installation besitzt anschließend diese sieben
Pflichtdienste:

- `e3dc-live`
- `e3dc-epex-manager`
- `e3dc-weather-manager`
- `e3dc-storage-simulator`
- `e3dc-storage-manager`
- `e3dc-websocket`
- `e3dc-notifier`

Jeder Unit-Schreib-, `daemon-reload`-, Enable- oder Startfehler bricht den
betroffenen Installationsschritt ab. Ein gestarteter Pflichtdienst gilt erst
nach erfolgreichem `systemctl is-active` als eingerichtet. Webportal,
Konfiguration, RAM-Disk, Initialbackup, Kerndienste und WebSocket bauen strikt
aufeinander auf; nach dem ersten Fehler wird kein folgender Pflichtschritt
ausgeführt. Bei einem Abbruch
nicht blind erneut installieren, sondern zuerst die erste sichtbare
Fehlermeldung und den belegten Zustand sichern:

```bash
tail -n 200 "$E3DC_INSTALL_PATH/logs/install.log"
systemctl --failed --no-pager
systemctl status e3dc-live e3dc-epex-manager e3dc-weather-manager \
  e3dc-storage-simulator e3dc-storage-manager e3dc-websocket e3dc-notifier \
  --no-pager
```

`--update-e3dc` installiert ausschließlich einen neueren freigegebenen Stand.
Ist der exakte Release bereits vollständig vorhanden, endet der Aufruf ohne
Backup und Dienstunterbrechung. `--reinstall-current` ist die ausdrücklich
gewählte Reparatur beziehungsweise Neuinstallation desselben veröffentlichten
Stands und durchläuft deshalb den vollständigen Backup-, Ruhe-, Finalizer-,
Gesundheits- und Rücklaufvertrag. Details und Zeitgrenzen stehen in
[Update.md](Update.md).

Seit 5.4.2 unterscheidet der Einstieg drei Zustände:

- Eine **frische Installation** besitzt noch keine Anlagenbestandteile. Nur in
  diesem Fall wird beim ersten Erzeugen der Konfiguration `ha_mode=off` als
  Einzelanlage vorbelegt und das vollständige Setup gestartet.
- Eine sicher erkennbare **unvollständige Installation** kann über
  `Installation / Update` fortgesetzt werden.
- Eine vorhandene, aber widersprüchliche Installation ohne gültige
  HA-/Shadow-Rolle bleibt gesperrt. Sie wird nicht still als Einzelanlage
  umgedeutet und muss zuerst über die Systemreparatur geprüft werden.

Paket-, Konfigurations-, Webportal-, Rechte- und Dienstfehler werden bis zum
Menü- und Prozess-Exitcode weitergegeben. Ein fehlgeschlagener Schritt wird
nicht als erfolgreicher Abschluss angezeigt.

Für den einmaligen Wechsel von 5.3.2b auf das aktuelle Stable-Release gilt eine engere
Startbedingung: ausschließlich der direkte administrative
`sudo /usr/bin/python3 -I -B -u installer_main.py --update-e3dc`-Aufruf aus
[Update.md](Update.md). Der interaktive Menüpunkt lädt im 5.3.2b-Altprozess
bereits zusätzliche Module und ist für diesen ersten Hybridwechsel nicht
freigegeben.

## Hauptmenü

| Menüpunkt | Zweck |
| :--- | :--- |
| `1) Installation / Update` | Installation oder Update mit Paketen, Webdateien, Rechten und Diensten. |
| `2) Systemstatus anzeigen` | Read-only Übersicht für Dienste, Pfade und Konfiguration. |
| `3) Rechte prüfen & korrigieren` | Repariert Besitzer, Gruppen, sudoers, Webrechte und Ramdisk. |
| `4) Notfallmodus / System reparieren` | Gebündelte Reparatur einer beschädigten Installation. |
| `5) Policygebundener Programm-Rückfall` | Rückfall auf einen ausdrücklich für Bare Metal freigegebenen Stable-Stand; `v5.3.2b` ist dafür nicht freigegeben. |
| `6) Backup erstellen / verwalten` | Verifizierte Sicherungen erstellen, prüfen oder wiederherstellen. |
| `7) Expertenmenü` | Docker, Energy Manager, Wallbox, MQTT, HA, Matter und weitere Module. |
| `8) Systempakete vorbereiten` | Paketbasis und Python-Umgebung für die Installation vorbereiten. |
| `9) Deinstallation` | E3DC-Control kontrolliert entfernen. |

Optionale Dienste werden über den Servicekatalog und ihre jeweiligen
Modulinstaller eingerichtet. Unit-Erzeugung und Start werden fail-closed
geprüft; nach einem abgebrochenen Systemschritt kann bereits angelegter
Teilbestand vorhanden sein und wird beim nächsten geprüften Lauf repariert.

## Wichtige Betriebsdaten

- `$E3DC_INSTALL_PATH/data/e3dc_v4.json`: lokale Konfigurationskopie und Migrationsquelle.
- `$E3DC_INSTALL_PATH/logs/install.log`: Installer-Protokoll.
- `/var/www/html/data/e3dc_v4.json`: kanonische Web-Konfiguration.
- `/var/www/html/ramdisk`: flüchtige Live-, Plan- und Statusdaten.
- `/var/www/html/e3dc_paths.json`: geprüfter Installations- und Python-Kontext für die Weboberfläche.
- `/srv/e3dc-control-backups`: Standardbereich für verifizierte externe Sicherungen.

## Fehlerbehebung

Bei fehlenden Rechten oder einem abgebrochenen Web-Update:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

Bei stillstehenden Live-Daten:

```bash
systemctl status e3dc-live e3dc-storage-manager
journalctl -u e3dc-live -n 80 --no-pager
```

Ein Update oder Rollback meldet nur Erfolg, wenn Backup, Ziel-SHA, Migration,
Dienste, Rolle und lokale HTTP-Prüfung vollständig bestätigt wurden.
