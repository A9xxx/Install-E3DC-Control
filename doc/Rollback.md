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

Der aktuelle Stable-Stand `v5.4.3m` führt den gebundenen Rückfallvertrag fort.

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
