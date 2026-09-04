# E3DC-Control Installer

Dokumentation Stand: 5.4.5

Der Installer verwaltet Bare-Metal-Installation, Update, Rechte, Dienste,
Backup, Rollback und optionale Produktmodule. Er ermittelt Benutzer, Home,
Installationspfad und Python-Umgebung aus dem geprüften Installationskontext.

5.4.5 führt die reguläre Worker-Bereinigung noch innerhalb des aktiven
Funktionskontexts aus. Der erfolgreiche Abschluss erzeugt dadurch unter
`set -u` keinen nachgelagerten Fehler wegen einer bereits nicht mehr gebundenen
lokalen Variable. Frühfehler und Signale bleiben durch den bestehenden
EXIT-Trap abgedeckt. Die neue PV-Prognosediagnose wird als statisches Webasset
mit dem regulären Update- und Rechtevertrag ausgeliefert.

Die Rechtereparatur ist in 5.4.5 ein eigener root-eigener Auftrag ohne
Vollbackup, Releasewechsel oder Dienstneustart. Unveränderte bekannte Pfade
werden beim ersten Auftrag direkt normalisiert. Inhaltlich lokal geänderte
Produktdateien bleiben dabei unangetastet und werden vollständig zur
Bestätigung angezeigt. Ein root-eigener Fünf-Minuten-Einmalvertrag bindet
Vertrags-Digest, exakte Pfadliste und Datei-Fingerprints; erst danach werden
nur deren Metadaten korrigiert. Das
Stable-Update prüft unabhängig davon seine exakte Ziel- und Löschprojektion
gegen einen belegten veröffentlichten Ausgangsstand und wiederholt den
fingerprintgebundenen Vergleich nach Writer-Ruhe unmittelbar vor dem Ersatz.

5.4.4i setzt im installierten Web-Launcher und im eigenständigen
Community-Bootstrap vor allen steuernden Dateitypprüfungen eine feste
C-Locale. Der gestartete systemd-Worker erhält dieselbe Umgebung ausdrücklich
mit. Lokalisierte `stat`-Ausgaben können zulässige root-kontrollierte Pfade
damit nicht mehr fälschlich ablehnen. Eigentümer-, Modus-, Symlink-, Hardlink-
und Pfadprüfungen bleiben unverändert wirksam.

Der enthaltene Stand aus 5.4.4h bindet den gestarteten Hintergrundauftrag beidseitig an aktive
systemd-Unit, MainPID, Prozess und root-kontrollierten Status. Ziel-Updater und
Produktpfad beginnen erst nach dieser Bestätigung. Der eigenständige
Community-Bootstrap darf das private Zielrelease vorher sicher binden, führt es
aber noch nicht aus. Ein früher Startfehler wird
mit konkretem Grund protokolliert; nach bestätigtem Ausführungsbeginn verlangt
ein unerwarteter Prozessverlust eine Zustands- und Rückfallprüfung statt einer
pauschalen Unverändert-Aussage.

Der enthaltene Installer-Anteil von 5.4.4g führt Backup, kurze Umschaltphase,
Releaseprojektion, Reparatur bekannter Rechte und Dienstneustart vollständig im
heruntergeladenen Ziel-Updater aus. Das Nutzer-`.git` ist keine
Updateautorität; 5.4.5 benennt lokale Inhaltsabweichungen im exakten
Zielumfang jedoch vor dem Überschreiben und verlangt dafür eine gebundene
Bestätigung. `ENOSPC`, `EROFS`, `EACCES` oder mehrere gleichrangige
Installationen enden mit einer konkreten Prüf- und Fortsetzungsanweisung. Die
Web-Rechtereparatur verwendet ab 5.4.5 den getrennten reinen Rechteauftrag;
alle sichtbaren zustandsändernden Webaktionen prüfen Anmeldung,
CSRF, HTTP-Ergebnis, Antwortinhalt und Teilfehler, bevor sie Erfolg melden.
Private Modus-5-Datenverzeichnisse werden als `02770`, im ausdrücklich
gewählten Kompatibilitätsmodus als `02775` geführt.
Die Web-Launcher aus 5.4.4a bis 5.4.4c werden mit ihren jeweiligen gebundenen
Übergabeverträgen unterstützt. Fehlt dort der vollständige Releasepfad, lädt
der aktuelle Einzel-Bootstrap den Zielbaum selbst und gleicht ihn mit Tag und
Commit ab. Eine neue Ziel-Pythonumgebung sowie optionale Abhängigkeiten werden
vor dem einmaligen Dienststopp vorbereitet; fremde System-Python-Pakete sind
keine Updatebedingung.

5.4.4g stellt nach der Releaseprojektion für eine beibehaltene
`external_pv_topology.json` den Installationsnutzer, die Webgruppe und den
gemeinsamen Lesemodus `0664` wieder her. Die übrigen Konfigurations- und
Geheimnisdateien behalten ihre strengeren Rechteverträge.

Der private Downloadbereich bleibt root-eigen mit `0700`/`0600`. Beim
Dateiaustausch veröffentlicht 5.4.4g den Live-Produktbaum dagegen mit einem
expliziten Vertrag aus Installationsnutzer, Webgruppe, Verzeichnissen `0755`,
normalen Dateien `0644` und Startskripten mit Interpreterzeile `0755`.
Anschließend müssen die PHP-Pfadauflösung und die Installer-Importkette als `www-data` den exakt
gebundenen Zielpfad lesen können. Die private Service-Pythonumgebung wird dafür
nicht geöffnet. Ein Fehler führt vor dem Dienststart in den gesicherten
Rücklauf.

Liegt die Installation unter einem privaten Home-Pfad, bindet der Updater
owner-private, nicht-setgid Vorfahren eng an die Webgruppe und ergänzt nur
deren Traversierrecht. Eine vorhandene gezielte POSIX-ACL für `www-data` wird
akzeptiert. Gemeinsam genutzte oder setgid Vorfahren werden ohne diese ACL vor
der Produktmutation mit einer konkreten Anleitung gesperrt; globales
`Other-Execute`, Auflisten, Lesen und Schreiben werden nicht freigegeben.
Direkte und verschachtelte Home-Installationen sowie `/opt`-Installationen
bleiben damit unterstützt.

Der Zielbestand beträgt maximal drei automatisch verwaltete
System-Backup-Familien und separat maximal drei Web-Installer-Sicherungen.
Schutzbindungen dürfen diese Zielgrenzen vorübergehend überschreiten.
Ungeschützte Altbestände werden weiter sicher bereinigt; die offene Zielgrenze
bleibt sichtbar und wird später erneut angewendet.

5.4.4e akzeptiert dabei die exakt bestätigte produktive RAM-Disk unter
`/var/www/html/ramdisk`. Eine ausdrücklich benannte Altdatei wird über den
eigenen fd-gebundenen RAM-Disk-Mountroot entfernt; andere fremde Mounts bleiben
gesperrt. Bei einem
Rücklauf werden die bekannten Webrechte vor dem Dienstneustart normalisiert;
eine zusätzliche feste HA-Wartefrist gibt es nicht.

Der Installer-Anteil von 5.4.4c ersetzt die bisherige Finalizer- und
Recovery-Bootblock-Kette durch einen direkten Ziel-Updater. Das `.git`-
Verzeichnis der Nutzerinstallation ist weder Eingangsbedingung noch
Updateautorität. Der Ziel-Updater erstellt bei laufenden Diensten das
Vollbackup, stoppt die betroffenen Dienste danach genau einmal kurz und sichert
die nun ruhenden veränderlichen Daten nach. Anschließend ersetzt er
Produktdateien und Webdateien, setzt Rechte, Core-Units sowie root-eigene
Launcher und startet die benötigten Dienste neu. Eine Same-Version-Reparatur
verwendet denselben Ablauf. Die Stable-Versionsanzeige ist rein informativ und
blockiert den Start nicht. Ein defekter 5.4.4b-Launcher kann für den ersten
Übergang einmalig `sudo /bin/sh ./e3dc-update-bootstrap` benötigen. EMS- und
Hardwarelogik entsprechen unverändert 5.4.4b.

Der Installer-Anteil von 5.4.4b macht den heruntergeladenen Ziel-Updater zur
einzigen Autorität für Inventar, Backup, Projektion und Recovery. Eine laufende
Einzelinstanz wird aus ihrem systemd-Dienst gebunden; Benutzername und
Installationsverzeichnis sind nicht fest codiert. Lokale Produktänderungen,
fehlende Release-Dateien, alte Rechte oder unbrauchbare Git-Metadaten werden
vor dem Ersetzen vollständig durch den aktuellen Backupvertrag erfasst.
Download, Vorprüfung und Backup erfolgen bei laufenden Diensten. Erst für die
abschließende Produkt-, Rechte- und Unit-Projektion werden Writer gestoppt und
nach dem Healthcheck wieder freigegeben. Mehrere gleichrangige Instanzen und
echte Sicherheitsabweichungen bleiben gesperrt. Jede kontrollierte
Fehlerausgabe nennt den Systemzustand und den nächsten sicheren Befehl; exakt
abgeschlossene 5.4.4-/5.4.4a-Recoveryreste können eng gebunden bereinigt
werden, ausstehende Belege nicht.

Der Installer-Anteil von 5.4.4a führt alle normalen Bare-Metal-Updateeinstiege
auf denselben root-eigenen Hintergrundauftrag. Für heterogene Altinstallationen
genügt die einzelne Datei `e3dc-update-bootstrap` mit
`sudo /bin/sh ./e3dc-update-bootstrap`. Der damit gestartete veröffentlichte
Updatepfad erkennt Installationsroot, Installationsbenutzer und Anlagenrolle
eindeutig; ein fester Pfad, ein
vorgeschaltetes `chmod` und weitere Argumente sind nicht nötig. Der vorhandene
Alt-Updater, lokale Git-Metadaten, getrackte Änderungen und historische
Dateimodi sind kein vorgelagertes Gate. Alte Installer- und Web-Pfadmetadaten
werden auf den erkannten Bestand normalisiert und vom Zielprozess erneut
geprüft. Der vollständige Vertrag aus Backup-Prüfung, Writer- und Dienststopp,
Datei- und Rechteprojektion, Dienststart und Healthcheck bleibt erhalten.
EMS-, Direktvermarktungs-, Wallbox- und Hardwarelogik entsprechen unverändert
5.4.4.

Der Installer-Anteil von 5.4.4 konsolidiert den Updatepfad für heterogene
Altanlagen. Web- und Download-Update starten den veröffentlichten Ziel-Updater
aus einem root-eigenen Ausführungssnapshot und binden ihn an den tatsächlichen
Installationsroot. Nach erstelltem und geprüftem Backup sowie stillgelegten
Diensten werden Produktdateien, Rechte, ein eng bekannter redundanter
Storage-Override und die benötigte Laufzeitumgebung auf den Zielstand gebracht.
Danach folgen Dienststart und Healthcheck. Pfadflucht, Links, nicht eindeutig
gebundene Systemdateien, konkurrierende Writer sowie fehlgeschlagene Backup-
oder Gesundheitsprüfungen bleiben harte Stopps.

Der Installer-Anteil von 5.4.3s legt im administrativen
Root-Download-Bootstrap einen wirklich fehlenden kanonischen Backup-Root
sicher an. Danach bleibt der Ablauf unverändert: Backup erstellen und prüfen,
Dienste stoppen, Dateien und Rechte aktualisieren, Dienste starten und ihren
Zustand prüfen. EMS-, Direktvermarktungs-, Wallbox- und Hardwarelogik ändern
sich nicht.

Der Installer-Anteil von 5.4.3r darf beim ausdrücklich mit Rolle und Peer
gebundenen Download-Bootstrap einen wirklich fehlenden HA-Rollenanker aus
dieser bereits bestehenden Bindung erzeugen. Das geschieht erst nach
verifiziertem Backup und bestätigter Writer-Ruhe. EMS-Regelung und
Hardwareausgänge ändern sich nicht.

Der Installer-Anteil von 5.4.3q verwendet im Finalizer des administrativen
Download-Bootstraps den absoluten Pfad `/usr/sbin/visudo` und ist damit nicht
von einem verkürzten Root-`PATH` abhängig. EMS-Regelung und Hardwareausgänge
ändern sich nicht.

Der Installer-Anteil von 5.4.3p erzeugt die vom administrativen
Download-Bootstrap neu aufgebaute `.git`-Fläche als den zuvor eindeutig
gebundenen Installationsbenutzer. Verifiziertes Backup, bestätigte Writer-Ruhe
und sämtliche Safety-Gates bleiben unverändert verpflichtend. EMS-Regelung und
Hardwareausgänge ändern sich nicht.

Der Installer-Anteil von 5.4.3o ergänzt den administrativen
`e3dc-update-bootstrap`. Er lädt den veröffentlichten Stable-Tag samt Commit
in einen root-eigenen Ziel-Checkout und verwendet den vorhandenen Alt-Updater
sowie dessen `.git`-Metadaten nicht als Autorität. Erst nach verifiziertem
Backup und bestätigter Writer-Ruhe werden bekannte Release-Dateien, Rechte und
Units normalisiert. Pfadflucht, Symlinks, Spezialdateien, zusätzliche
Hardlinks, konkurrierende Updates sowie fehlgeschlagene Backup- oder
Healthchecks bleiben harte Stopps.

Der Installer-Anteil von 5.4.3n korrigiert ausschließlich den pfadgenauen
Metadatenvertrag der privilegierten Restorequelle. Nur
`/etc/e3dc-control/instance_role.json` wird mit `root:www-data 0640`
akzeptiert; die private Backup-Payload bleibt `root:root 0600`. Alle anderen
privilegierten Pfade sowie falsche Eigentümer, Gruppen, Modi, Links, ACLs,
Attribute oder Identitätsdrift bleiben streng fail-closed. EMS-Regelung,
HA-, Wallbox-, Wärme- und Direktvermarktungsentscheidungen sowie
Hardwareausgänge ändern sich gegenüber 5.4.3m nicht.

Der Installer-Anteil von 5.4.3m erlaubt ausschließlich dem vollständig
versiegelten nativen Ziel-Updater beim normalen vorwärtsgerichteten
Releasewechsel, einen wirklich fehlenden Instanzrollenanker einmalig auf
`off` zu projizieren. Die eingefrorene Rolle muss exakt `off` sein, und es
darf kein HA-Peer konfiguriert sein. Die Erzeugung erfolgt erst nach
Root-Receipt-gebundenem Transaktionsbackup, abgeschlossener gegebenenfalls
nötiger Storage-Manager-Unit-Promotion und bestätigter Aktorruhe. Bootstrap,
Reinstall und Rollback besitzen diese Autorität nicht. Vorhandene fremde oder
widersprüchliche Anker sowie HA- und Shadow-Rollen bleiben fail-closed und
werden nicht automatisch repariert.

Der Web-Update-Launcher ist ein kleiner gemeinsamer Download-Dispatcher. Er
startet für Dashboard, Konsole und Installer-Menü denselben root-eigenen
Hintergrundauftrag. Die automatische Prüfung verwendet dieselbe Stable-Quelle,
informiert aber nur über einen neuen Stand. Lokale Git-Metadaten, getrackte
Änderungen und historische Dateimodi sind keine vorgelagerte Startautorität
mehr. Der heruntergeladene Ziel-Updater erstellt das Vollbackup, ersetzt den
Programmstand und bestätigt anschließend den Wiederanlauf.

Der Installer-Anteil von 5.4.3l bindet den updater-eigenen Git-Rückweg vor
der ersten Dienstmutation an Repository, `old_commit`, root-eigenes
Transaktionsbackup und Transaktionskennung. Bei belegten, weiterhin
vorhandenen Änderungen an getrackten Dateien stellt dieser Rückweg die
gesicherten Bytes wieder her und härtet den Dateimodus auf den im gebundenen
`old_commit` belegten Git-Modus. Unveränderte getrackte Dateien folgen
vollständig dem Ausgangscommit. Staged Indexstände, ungetrackte oder
gelöschte Dateien sowie allgemeine manuelle Restorepfade gehören nicht zu
dieser neuen Zusage.

Eine exakt freigegebene historische Familie der
`e3dc-storage-manager.service` kann vor dem ersten Dienststopp atomar in eine
root-eigene Unit mit Modus `0644` überführt werden; abweichende Units oder
Drop-ins bleiben gesperrt. PiGuard mit dem exakten systemd-Zustand
`activating/auto-restart` wird als zuvor laufender Wächter erfasst. Erkennt
der Ziel-Updater einen fehlgeschlagenen Rückweg synchron, hält er einen
transaktionsgebundenen Startschutz für PiGuard und die bekannten Writer. Das
ist keine pauschale Absicherung gegen Stromausfall, `SIGKILL` oder einen
Prozessabbruch außerhalb dieses erkannten Fehlerpfads.

Der Installer-Anteil von 5.4.3k schließt zusätzlich den älteren nativen
`--target-updater-handoff`, der `E3DC_BOOTSTRAP_USER` vor seinem root-eigenen
Ziel-Snapshot entfernt. Sowohl dieser Einstieg als auch der bereits in 5.4.3j
gebundene flaglose Snapshot dürfen den Installationsnutzer erst nach dem
Root-Lock aus demselben gültigen Nicht-Root-Eigentümer von Repository und
`.git` binden. Nach der Bindung des versiegelten Snapshots werden Repository,
`.git`, lokales Benutzerkonto und Nutzerwert unmittelbar vor dem ersten Import
aus dem Zielcode erneut geprüft. Die Härtungen und Abbruchgründe aus 5.4.3j
bleiben unverändert.

Der Installer-Anteil von 5.4.3j schließt ausschließlich den flaglosen,
root-eigenen Ziel-Snapshot eines älteren 5.4.2d-Aufrufers. Hat dieser Aufrufer
`E3DC_BOOTSTRAP_USER` entfernt,
darf der Zielcode den Installationsnutzer erst nach dem Root-Lock aus dem
übereinstimmenden Eigentümer von Repository und `.git` binden. Unmittelbar vor
dem Finalizer wird erneut geprüft; danach wird die Aufruferumgebung
wiederhergestellt. Ein bereits gesetzter Nutzerwert bleibt unverändert, muss
aber exakt dem gebundenen Repository-Eigentümer entsprechen. Root, `www-data`,
fremde oder unterschiedliche Eigentümer und ein abweichender Nutzerwert bleiben
gesperrt.

Mit 5.4.3i darf die Weboberfläche das reguläre System-Update wieder über
einen argumentlosen, root-eigenen Launcher starten. Freie Aktionen, Pfade,
Tags, Reparaturen, Neuinstallationen und Rückfälle bleiben im Web gesperrt.
Installationen bis einschließlich 5.4.3f benötigen für diesen einmaligen
Übergang noch den administrativen Konsolenweg aus [Update.md](Update.md).
Beim Zielübergang aus älteren 5.4.2-Beständen wird der Installationsnutzer aus
der kanonischen Repository-Eigentümerstruktur gebunden und unmittelbar vor dem
versiegelten Kindstart erneut geprüft; eine lokale Installer-Konfigurationsdatei
im Snapshot ist dafür nicht erforderlich.

Seit 5.4.2d bewertet der Updatepfad den Wiederanlauf erforderlicher Dienste
anhand des belegten systemd-Endzustands. Nicht installierte optionale Units
brechen den verifizierten Maskenrücklauf nicht allein wegen einer abweichenden
systemd-Textausgabe ab. Echte Start-, Masken- oder
Wiederherstellungsabweichungen bleiben harte, fail-closed Abbruchgründe.

## Portabler Einstieg

Für einen bereits aktualisierten Bestand kann der gemeinsame Hintergrundjob
ohne Kenntnis des Produktpfads gestartet werden:

```bash
sudo /usr/local/sbin/e3dc-web-update-launcher
```

Für heterogene Altstände wird nur die Datei `e3dc-update-bootstrap` auf den
Raspberry Pi kopiert und im Ablageverzeichnis gestartet:

```bash
sudo /bin/sh ./e3dc-update-bootstrap
```

Der veröffentlichte Updatepfad erkennt Produktpfad und Installationsbenutzer
selbst. Erst für andere administrative Installeraktionen wird der tatsächliche
absolute Produktpfad benötigt:

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

`--update-e3dc` startet denselben Hintergrundauftrag wie das Web. Auch wenn der
exakte Release bereits vorhanden ist, erstellt der Ziel-Updater das Vollbackup,
stoppt die betroffenen Dienste einmal kurz, ersetzt Programmstand, Rechte und
Units und prüft den Wiederanlauf. Die automatische Updateprüfung informiert nur
über einen neuen Stable-Stand. Details stehen in [Update.md](Update.md).

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

Für den einmaligen Wechsel aus einem heterogenen Altstand wird ausschließlich
die veröffentlichte Datei `e3dc-update-bootstrap` auf den Raspberry Pi kopiert
und dort mit `sudo /bin/sh ./e3dc-update-bootstrap` gestartet. Der damit
gestartete veröffentlichte Updatepfad erkennt die Installation selbst und
verwendet den alten `installer_main.py`-Updatepfad nicht. Details stehen in
[Update.md](Update.md).

## Hauptmenü

| Menüpunkt | Zweck |
| :--- | :--- |
| `1) Installation / Update` | Installation oder Update mit Paketen, Webdateien, Rechten und Diensten. |
| `2) Systemstatus anzeigen` | Read-only Übersicht für Dienste, Pfade und Konfiguration. |
| `3) Rechte prüfen & korrigieren` | Prüft und repariert ausschließlich den Rechtevertrag; kein Vollbackup, kein Releasewechsel und kein Neustart der Regelungsdienste. Inhaltlich lokal geänderte Produktdateien werden vor der Metadatenkorrektur einzeln zur Bestätigung angezeigt. |
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

Bei `ENOSPC`, `EROFS`, `EACCES` oder mehreren gleichrangigen Installationen
nennt die Ausgabe den betroffenen Pfad beziehungsweise die Kandidaten und den
nächsten ausführbaren Prüf- oder Fortsetzungsbefehl. Es wird keine Instanz
geraten und kein fehlgeschlagener Teilschritt als Erfolg ausgegeben.
