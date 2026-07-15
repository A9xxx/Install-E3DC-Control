# Rollback

Ein Rollback besteht aus zwei getrennten Teilen: Der Programmstand wird auf
einen in `UPDATE_POLICY.json` freigegebenen Stable-Tag gesetzt; persistente
Betriebsdaten koennen aus einem manifestierten Sicherungspunkt wiederhergestellt
werden.

Die Konsolenbeispiele verwenden den zuvor geprueften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Programmstand zuruecksetzen

`v5.3.2b` ist selbst der öffentliche Rollback-Root und enthält keinen älteren
Tag. Ein späterer Stable-Stand darf `v5.3.2b` als einzigen Rückfall anbieten.

Freie Commit-Hashes und Zwischencommits sind keine Rückfallversionen. Sie
koennen weder im Konsolenmenue noch über den Installer ausgeführt werden.
Jeder angebotene Tag ist in der verifizierten HEAD-Policy an genau einen
vollstaendigen Commit-SHA gebunden. Programm-Rollback, Update und Bootstrap
verwenden denselben Backup-, Websync-, Migrations-, Boot-, HTTP-, Dienst- und
HA-/Shadow-Pruefpfad. Der Legacy-Dienst `e3dc.service` wird nie neu gestartet.

## Betriebsdaten wiederherstellen

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Waehle **`13) System-Backup erstellen / verwalten`** und den gewuenschten
Sicherungspunkt. Backups ohne gueltiges Manifest und Manifest-Digest werden
nicht angeboten. Vor dem Überschreiben wird ein neues Sicherheits-Backup
erstellt; der ausgewaehlte Sicherungspunkt ist dabei von der Retention
ausdruecklich geschuetzt.

Konfiguration, Statistiken, Matter-Kopplungsdaten, Manual-Locks, Programmbaum,
Webdateien, Units und Watchdog-Pfade werden als eine Transaktion
wiederhergestellt. Dienste starten nur nach vollstaendigem Restore. Im HA-
Slave- oder Shadow-Modus bleiben die durch die Rolle gesperrten Dienste aus.

## Fehlerfall

Scheitert Sicherheits-Backup, Aktorruhe, Restore, Rollenvergleich, Dienst-,
HTTP- oder Boot-Gate, wird kein Erfolg gemeldet. Bereits ersetzte Dateien werden
innerhalb der Restore-Transaktion zurueckgesetzt. Scheitert ein spaeteres Gate,
wird automatisch das unmittelbar zuvor erzeugte Sicherheits-Backup
wiederhergestellt. Ist auch dies nicht beweisbar, bleiben Writer gestoppt.
