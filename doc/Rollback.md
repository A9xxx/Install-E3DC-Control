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

## Programmstand zurücksetzen

Der aktuelle Stable-Stand `v5.4.3g` führt den gebundenen Rückfallvertrag fort.

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
