# Backup-System

Der Installer legt vor jedem Update und Rollback einen verifizierten
Sicherungspunkt an. Ein fehlendes, leeres, unvollstaendiges oder nicht lesbares
Backup bricht den Vorgang ab. Diese Sperre kann nicht umgangen werden.

Die Konsolenbeispiele verwenden den zuvor geprüften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Speicherort und Schutz

Der Standardpfad liegt ausserhalb von Installations- und Benutzer-Home-Baeumen
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

Vorhandene Quellen werden vollstaendig und ohne Dateiendungsfilter erfasst:

- der gesamte Installationsbaum ohne Versionsmetadaten, lokale Entwicklungs-/
  Koordinationsverzeichnisse und Backup-Baeume;
- V4- und Legacy-Konfiguration, Statistiken und Datenbanken;
- Matter-Kopplungsdaten, Lernprofile, Manual-Locks und weitere Zustandsdaten;
- Web-Programmdateien und Web-Daten;
- `/var/lib/e3dc-control` und `/etc/e3dc-control`;
- alle katalogisierten systemd-Units sowie Watchdog- und Boot-Notify-Skripte.

Fehlende optionale Quellen werden im Manifest vermerkt. Ist eine vorhandene
Quelle nur teilweise lesbar, gilt das gesamte Backup als fehlgeschlagen. Der
unvollstaendige, manifestlose Ordner bleibt als Quarantaenebeleg liegen und
wird weder von der Retention gelöscht noch als Restore angeboten.

## Manifest, Restore und Retention

Jeder gueltige Sicherungspunkt enthaelt `backup-manifest.json` und
`backup-manifest.sha256`. Das Manifest fuehrt Dateisatz, Groesse, Besitzer,
Gruppe, Modus, Verzeichnisstruktur, Kategorie, Restore-Ziel und SHA-256 auf.
SQLite wird über die Online-Backup-Schnittstelle konsistent gesichert.

Beim Restore wird zuerst der gesamte Satz geprüft und vorbereitet. Alle Ziele
bilden eine Transaktion: Scheitert ein Austausch, werden bereits ersetzte
Dateien exakt zurueckgesetzt und neu angelegte Ziele entfernt. Die Retention
loescht nur verifizierte direkte Kindverzeichnisse des passenden Backup-Typs;
fremde Dateien, Symlinks, unvollstaendige Ordner und der gerade ausgewaehlte
Rollback-Sicherungspunkt bleiben unangetastet.

## Manuelles Backup

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Waehle **`13) System-Backup erstellen / verwalten`**.
