# Backup-System

Der Installer legt vor jedem Update und Rollback einen verifizierten
Sicherungspunkt an. Ein fehlendes, leeres, unvollständiges oder nicht lesbares
Backup bricht den Vorgang ab. Diese Sperre kann nicht umgangen werden.

Die Konsolenbeispiele verwenden den zuvor geprüften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Speicherort und Schutz

Der Standardpfad liegt außerhalb von Installations- und Benutzer-Home-Bäumen
im dafür vorgesehenen System-Backupbereich:

```text
/srv/e3dc-control-backups
```

Ein alternativer absoluter Pfad kann mit `E3DC_BACKUP_ROOT` festgelegt werden,
muss aber ein eigener Unterbaum namens `e3dc-control-backups` oder
`.e3dc-control-backups` sein. Root-, Home-, Installations-, System-, Symlink-
und bereits fremd belegte Verzeichnisse werden abgelehnt. Ein Marker bindet den
Backup-Root an genau eine Installation.

Backup-Verzeichnisse erhalten Modus `0700`, Dateien `0600`.

## Gesicherter Umfang

Vorhandene Quellen werden vollständig und ohne Dateiendungsfilter erfasst:

- der gesamte Installationsbaum ohne Versionsmetadaten, lokale Entwicklungs-/
  Koordinationsverzeichnisse und Backup-Bäume;
- V4- und Legacy-Konfiguration, Statistiken und Datenbanken;
- Matter-Kopplungsdaten, Lernprofile, Manual-Locks und weitere Zustandsdaten;
- Web-Programmdateien und Web-Daten;
- `/var/lib/e3dc-control` und `/etc/e3dc-control`;
- alle katalogisierten systemd-Units sowie Watchdog- und Boot-Notify-Skripte.

Fehlende optionale Quellen werden im Manifest vermerkt. Ist eine vorhandene
Quelle nur teilweise lesbar, gilt das gesamte Backup als fehlgeschlagen. Der
unvollständige, manifestlose Ordner bleibt als Quarantänebeleg liegen und
wird weder von der Retention gelöscht noch als Restore angeboten.

## Manifest, Restore und Retention

Jeder gültige Sicherungspunkt enthält `backup-manifest.json` und
`backup-manifest.sha256`. Das Manifest führt Dateisatz, Größe, Besitzer,
Gruppe, Modus, Verzeichnisstruktur, Kategorie, Restore-Ziel und SHA-256 auf.
SQLite wird über die Online-Backup-Schnittstelle konsistent gesichert.

Beim Restore wird zuerst der gesamte Satz geprüft und vorbereitet. Alle Ziele
bilden eine Transaktion: Scheitert ein Austausch, werden bereits ersetzte
Dateien exakt zurückgesetzt und neu angelegte Ziele entfernt. Die Retention
löscht nur verifizierte direkte Kindverzeichnisse des passenden Backup-Typs;
fremde Dateien, Symlinks, unvollständige Ordner und der gerade ausgewählte
Rollback-Sicherungspunkt bleiben unangetastet.

Nach der Verifikation des neuen Backups gelten zwei getrennte Zielgrenzen des
automatisch verwalteten Bestands: maximal drei System-Backup-Familien und
maximal drei Web-Installer-Sicherungen. Eine ruhende Daten-Nachsicherung gehört zu ihrem
Vollbackup und belegt keinen zusätzlichen Familienplatz. Das frisch erzeugte
Backup sowie ein für den Rollback ausgewähltes Backup zählen innerhalb der
drei Plätze, bleiben während ihrer Schutzbindung aber immer erhalten.

Bei einem aktiven Update- oder Recovery-Beleg bleibt die gesamte Retention ein
mutationsfreier Sicherheitsstopp. Sind in einem ansonsten freien
Wartungslauf ausnahmsweise mehr als drei gültige Familien gleichzeitig
geschützt, wird der Vorgang nicht abgebrochen: Ungeschützte Altbestände werden
soweit sicher möglich entfernt, die noch offene Grenze wird sichtbar gemeldet
und bei der nächsten sicheren Backup-Bereinigung erneut angewendet. Nicht
verifizierbare, fremde oder quarantänisierte Verzeichnisse werden niemals zur
Einhaltung einer bloßen Zahl zwangsweise gelöscht.

Die Drei-Generationen-Grenze reduziert den dauerhaften Speicherbedarf. Vor
einem Update wird weiterhin ein vollständiges neues Backup geschrieben; die
Grenze allein reduziert daher nicht die Schreibmenge dieses einzelnen Updates.

## Manuelles Backup

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Wähle **`6) Backup erstellen / verwalten`**.
