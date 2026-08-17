# Rollback

Ein Rollback besteht aus zwei getrennten Teilen: Ein Docker-Programmstand kann
auf einen in `UPDATE_POLICY.json` für Docker freigegebenen Image-Tag gesetzt
werden; persistente Betriebsdaten einer Bare-Metal-Installation können aus
einem manifestierten Sicherungspunkt wiederhergestellt werden.

Die Konsolenbeispiele verwenden den zuvor geprüften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Konsolidierter Update- und Rückweg in 5.4.4

5.4.4 führt den gebundenen Rückfallvertrag fort. Der Web-Updater führt den
veröffentlichten Zielcode aus einem root-eigenen Snapshot aus, hält den
tatsächlichen Installationsroot aber getrennt gebunden. Erst nach erstelltem
und geprüftem Backup und bestätigter Writer-Ruhe werden Dateien, Rechte und
eng bekannte historische Dienstdarstellungen auf den Zielstand gebracht.
Scheitern Kopie, Rechteprojektion, Dienststart oder Healthcheck, bleibt der
Rückweg an genau diese Sicherung und Transaktion gebunden.

Frühere Dateirechte, ein redundanter alter Storage-Override oder eine neu
benötigte Laufzeitumgebung sind im ausdrücklich gestarteten Downloadpfad kein
eigenständiger Rückfallgrund. Unsichere Pfade, Links, nicht eindeutig
gebundene Systemdateien, konkurrierende Writer und ein ungültiges Backup
werden dadurch nicht freigegeben.

## Fehlender Backup-Root im Download-Bootstrap in 5.4.3s

5.4.3s legt im administrativen Root-Download-Bootstrap einen wirklich
fehlenden kanonischen Backup-Root sicher an. Danach bleibt der Update- und
Rückfallablauf unverändert: Backup erstellen und prüfen, Dienste stoppen,
Dateien und Rechte aktualisieren, Dienste starten und ihren Zustand prüfen.
Ein bereits vorhandener unsicherer oder widersprüchlicher Pfad bleibt
gesperrt; EMS-, Direktvermarktungs-, Wallbox- und Hardwarelogik ändern sich
nicht.

## HA-Rollenanker im Download-Bootstrap in 5.4.3r

5.4.3r darf beim ausdrücklich mit Rolle und Peer gebundenen
Download-Bootstrap einen wirklich fehlenden HA-Rollenanker aus dieser bereits
bestehenden Bindung erzeugen. Die Erzeugung erfolgt erst nach verifiziertem
Backup und bestätigter Writer-Ruhe. Der Rückfallvertrag bleibt unverändert;
EMS-Regelung und Hardwareausgänge ändern sich nicht.

## Bootstrap-Finalizer in 5.4.3q

5.4.3q ruft `visudo` im Finalizer des administrativen Download-Bootstraps über
den absoluten Pfad `/usr/sbin/visudo` auf. Der Rückfallvertrag sowie sämtliche
übrigen Update- und Sicherheitsgrenzen bleiben unverändert; EMS-Regelung und
Hardwareausgänge ändern sich nicht.

## Rettungsupdate in 5.4.3o

Der Download-Bootstrap von 5.4.3o ist ein vorwärtsgerichteter administrativer
Rettungsweg, kein freier Programm-Rollback. Er erstellt und verifiziert zuerst
das externe Backup, stoppt die Writer und projiziert anschließend den
veröffentlichten Zielstand. Ein Fehler führt weiterhin ausschließlich über das
gebundene Backup zurück; freie Commits und ein Rückfall über den beschädigten
Alt-Updater bleiben ausgeschlossen.

## Bootstrap-Eigentümer in 5.4.3p

5.4.3p erzeugt die neue `.git`-Fläche des Download-Bootstraps als den zuvor
eindeutig gebundenen Installationsbenutzer. Der Rückfallvertrag ändert sich
nicht: Verifiziertes Backup, bestätigte Writer-Ruhe und sämtliche Safety-Gates
bleiben verpflichtend; EMS-Regelung und Hardwareausgänge bleiben unverändert.

## Rollenanker-Restoregrenze in 5.4.3n

5.4.3n akzeptiert ausschließlich den kanonischen Pfad
`/etc/e3dc-control/instance_role.json` mit `root:www-data 0640` als
privilegierte Restorequelle. Die private Backup-Payload bleibt
`root:root 0600`. Für alle anderen privilegierten Systempfade gilt weiterhin
der strenge `root:root`-Vertrag; ein falscher Pfad, eine abweichende Gruppe
oder ein anderer Modus sowie Symlinks, zusätzliche Hardlinks, ACLs, Attribute
oder Identitätsdrift bleiben harte Abbruchgründe. Regelungsentscheidungen und
Hardwareausgänge ändern sich gegenüber 5.4.3m nicht.

## Rollenanker-Grenze in 5.4.3m

5.4.3m erweitert ausschließlich den normalen vorwärtsgerichteten,
vollständig versiegelten Ziel-Updater: Ein wirklich fehlender Rollenanker darf
für den exakt gebundenen `off`-Einzelknoten ohne konfigurierten Peer erst nach
Root-Receipt-gebundenem Backup, abgeschlossener gegebenenfalls nötiger
Storage-Manager-Unit-Promotion und Aktorruhe einmalig erzeugt werden. Rollback
und Reinstall besitzen diese Autorität ausdrücklich nicht. Vorhandene fremde
oder widersprüchliche Anker sowie HA- und Shadow-Rollen bleiben gesperrt und
werden nicht repariert.

## Updateeigener Rückweg in 5.4.3l

Der in 5.4.3l ergänzte Byte- und Modivertrag gehört ausschließlich zum
nativen Git-basierten Ziel-Updater: Er bindet Repository, `old_commit`,
root-eigenes Transaktionsbackup und Update-Transaktion vor der ersten
Dienstmutation. Bei belegten, weiterhin vorhandenen Änderungen an getrackten
Dateien stellt dieser Rückweg die gesicherten Bytes wieder her und härtet den
Dateimodus auf den im gebundenen `old_commit` belegten Git-Modus.
Unveränderte getrackte Dateien folgen vollständig diesem Ausgangscommit.

Staged Indexstände, ungetrackte oder gelöschte Dateien sowie die unten
beschriebene manuelle Backup-Wiederherstellung erhalten durch diesen neuen
Vertrag keine zusätzliche Vollständigkeitszusage. Auch die privilegierte
Unit-Ausnahme bleibt eng: Nur eine exakt freigegebene historische Familie der
`e3dc-storage-manager.service` darf atomar in den root-eigenen Unit-Vertrag
überführt werden; abweichende Units und Drop-ins bleiben gesperrt.

Scheitert dieser updater-eigene Rückweg synchron und nachweisbar, bleibt ein
transaktionsgebundener systemd-Startschutz für PiGuard und die bekannten
Writer bestehen. Der Schutz wird erst nach vollständig verifiziertem Rückweg
entfernt. Das ist keine pauschale Absicherung gegen Stromausfall, `SIGKILL`
oder einen Prozessabbruch außerhalb des erkannten Fehlerpfads.

## Programmstand zurücksetzen

Der aktuelle Stable-Stand `v5.4.4` führt den gebundenen Rückfallvertrag fort.

Beim Rücklauf wird der tatsächliche Dateisystemzustand einer Unit weiterhin
streng gegen reguläre Unit-Datei, kanonische `/dev/null`-Maske, unerwarteten
Link oder fehlenden optionalen Zustand geprüft. Eine nicht installierte
optionale Unit darf den Rücklauf nicht allein wegen einer abweichenden
systemd-Textausgabe verwerfen; echte Maskenabweichungen bleiben blockierend.

`v5.4.2` bietet den öffentlichen Rollback-Root `v5.3.2b` ausschließlich als
Docker-Image an. Dieser Root enthält keinen älteren öffentlichen Tag. Auf Bare
Metal wird er nicht als Programm-Rückfall angeboten, weil ihm der
zielgebundene Release-Finalizer des aktuellen Transaktionsvertrags fehlt.

Freie Commit-Hashes und Zwischencommits sind keine Rückfallversionen. Sie
können weder im Konsolenmenü noch über den Installer ausgeführt werden.
Jeder angebotene Docker-Tag ist in der verifizierten HEAD-Policy an genau einen
vollständigen Commit-SHA und ein Image gebunden. Ein freier Bare-Metal-Checkout
auf den Altstand ist kein unterstützter Rückweg. Der Legacy-Dienst
`e3dc.service` wird nie neu gestartet.

## Betriebsdaten wiederherstellen

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Wähle im sichtbaren Hauptmenü **`6) Backup erstellen / verwalten`** und den gewünschten
Sicherungspunkt. Backups ohne gültiges Manifest und Manifest-Digest werden
nicht angeboten. Vor dem Überschreiben wird ein neues Sicherheits-Backup
erstellt; der ausgewählte Sicherungspunkt ist dabei von der Retention
ausdrücklich geschützt.

Konfiguration, Statistiken, Matter-Kopplungsdaten, Manual-Locks, Programmbaum,
Webdateien, Units und Watchdog-Pfade werden als eine Transaktion
wiederhergestellt. Dienste starten nur nach vollständigem Restore. Im HA-
Slave- oder Shadow-Modus bleiben die durch die Rolle gesperrten Dienste aus.

## Fehlerfall

Scheitert Sicherheits-Backup, Aktorruhe, Restore, Rollenvergleich, Dienst-,
HTTP- oder Boot-Gate, wird kein Erfolg gemeldet. Bereits ersetzte Dateien werden
innerhalb der Restore-Transaktion zurückgesetzt. Scheitert ein späteres Gate,
wird automatisch das unmittelbar zuvor erzeugte Sicherheits-Backup
wiederhergestellt. Ist auch dies nicht beweisbar, bleiben Writer gestoppt.
