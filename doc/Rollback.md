# Rollback

Ein Rollback besteht aus zwei getrennten Teilen: Der Programmstand wird auf
einen in `UPDATE_POLICY.json` freigegebenen Release-/Rollback-Tag gesetzt;
persistente Betriebsdaten können aus einem manifestierten Sicherungspunkt
wiederhergestellt werden.

Die Konsolenbeispiele verwenden den zuvor geprüften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Programmstand zurücksetzen

`v5.4.0` darf ausschließlich den öffentlichen Rollback-Root `v5.3.2b`
anbieten. Dieser Root enthält keinen älteren öffentlichen Tag.

Freie Commit-Hashes und Zwischencommits sind keine Rückfallversionen. Sie
können weder im Konsolenmenü noch über den Installer ausgeführt werden.
Jeder angebotene Tag ist in der verifizierten HEAD-Policy an genau einen
vollständigen Commit-SHA gebunden. Programm-Rollback, Update und Bootstrap
verwenden denselben Backup-, Websync-, Migrations-, Boot-, HTTP-, Dienst- und
HA-/Shadow-Prüfpfad. Der Legacy-Dienst `e3dc.service` wird nie neu gestartet.

## Betriebsdaten wiederherstellen

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Wähle **`13) System-Backup erstellen / verwalten`** und den gewünschten
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
