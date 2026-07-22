# E3DC-Control Installer

Dokumentation Stand: 5.4.0a

Der Installer verwaltet Bare-Metal-Installation, Update, Rechte, Dienste,
Backup, Rollback und optionale Produktmodule. Er ermittelt Benutzer, Home,
Installationspfad und Python-Umgebung aus dem geprüften Installationskontext.

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
```

`e3dc-setup` validiert den Produkt-Root, übernimmt bei Bedarf die nötigen
Rechte und startet den Installer mit dem zur Installation gehörenden
Interpreter. Ein manuelles `git pull` ersetzt diesen Weg nicht.

## Hauptmenü

| Menüpunkt | Zweck |
| :--- | :--- |
| `1) Installation / Update` | Installation oder Update mit Paketen, Webdateien, Rechten und Diensten. |
| `2) Systemstatus anzeigen` | Read-only Übersicht für Dienste, Pfade und Konfiguration. |
| `3) Rechte prüfen & korrigieren` | Repariert Besitzer, Gruppen, sudoers, Webrechte und Ramdisk. |
| `4) Notfallmodus / System reparieren` | Gebündelte Reparatur einer beschädigten Installation. |
| `5) Rollback` | Rückfall auf den in der verifizierten Policy angebotenen Release. |
| `6) Backup erstellen / verwalten` | Verifizierte Sicherungen erstellen, prüfen oder wiederherstellen. |
| `7) Expertenmenü` | Docker, Energy Manager, Wallbox, MQTT, HA, Matter und weitere Module. |
| `8) Systempakete vorbereiten` | Paketbasis und Python-Umgebung für die Installation vorbereiten. |
| `9) Deinstallation` | E3DC-Control kontrolliert entfernen. |

Optionale Dienste werden über den Servicekatalog und ihre jeweiligen
Modulinstaller eingerichtet. Unit-Dateien werden transaktional vorbereitet,
aktiviert und bei Fehlern auf den vorherigen Zustand zurückgesetzt.

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
