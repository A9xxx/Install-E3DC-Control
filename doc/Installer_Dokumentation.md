# Dokumentation des E3DC-Control Installers

Der Installer verwaltet den Lebenszyklus einer E3DC-Control Installation:
Neuinstallation, Update, Rechte-Reparatur, Backup, Rollback, systemd-Dienste,
Webportal und optionale Module.

## Voraussetzungen

* Debian-basiertes System, z.B. Raspberry Pi OS.
* `git`, `python3` und `sudo`.
* Repository im Standardfall unter `~/Install`.

Start:

```bash
cd ~/Install
sudo python3 installer_main.py
```

Headless-Kommandos:

```bash
sudo python3 installer_main.py --check
sudo python3 installer_main.py --fix-permissions
sudo python3 installer_main.py --update-e3dc
```

## Konsolenmenü

Das Hauptmenü enthält nur noch direkt ausführbare Standardaktionen. Das einzige
Untermenü ist das Expertenmenü; dort sind Erweiterungen und Sonderfälle nach
Bereichen sortiert.

| Menüpunkt | Bedeutung |
| :--- | :--- |
| `1) Installation / Update` | Erstinstallation oder Update mit Websync, Rechten, Diensten und Paketprüfung. |
| `2) Systemstatus anzeigen` | Diagnoseübersicht ohne aktive Regel- oder Update-Eingriffe. |
| `3) Rechte prüfen & korrigieren` | Repariert Dateirechte, Gruppen, sudoers, Ramdisk und Web-Schreibrechte. |
| `4) Notfallmodus / System reparieren` | Reparaturpfad bei beschädigter Installation oder fehlenden Paketen. |
| `5) Rollback auf Git-Stand` | Rücksprung auf einen bekannten Git-Stand. |
| `6) Backup erstellen / verwalten` | Sicherungen erstellen, wiederherstellen oder prüfen. |
| `7) Expertenmenü` | Docker, Energy Manager, native Wallbox, MQTT, Home Assistant, Bluelink, Watchdog und Spezialmodule. |
| `8) Systempakete vorbereiten` | Installiert Systempakete und Python-Abhängigkeiten, repariert das venv und beendet danach für Snapshot-/VM-Vorbereitung. |
| `9) Deinstallation` | Kontrollierter Rückbau. |

Der frühere Sammelpunkt `18 Alles installieren` ist nicht mehr Teil des
Hauptmenüs. Für normale Installationen und Updates wird immer `1) Installation /
Update` genutzt.

Die Expertenpunkte sind in 10er-Blöcke gegliedert:

| Block | Inhalt |
| :--- | :--- |
| `11-19 Kernsystem & Update` | Rollback, Datei-Backup, Watchdog/Telegram und Kernsystem-Aktionen. |
| `21-29 Umgebung & Python` | `21) Python venv neu aufbauen`, `22) Python venv Namen ändern`, Rechte, Status, Paketbasis und Deinstallation. |
| `31-39 Docker Migration & Verwaltung` | `31) Zu Docker wechseln`, `32) Docker auflösen` und spätere Docker-Werkzeuge. |
| `41-49 Erweiterungen & Smart Home` | `41) Energy Manager`, native Wallbox, Bluelink, MQTT, Matter und HA. |

## Hauptkomponenten

* `installer_main.py`: Einstiegspunkt, root-Prüfung, Pfaderkennung und Menü.
* `Installer/core.py`: Menü- und Modulregistrierung.
* `Installer/utils.py`: gemeinsame Funktionen für apt/pip, systemd,
  Dateioperationen, Rechte und Logging.
* `Installer/permissions.py`: korrigiert Besitzer, Gruppen, Ramdisk,
  Web-Schreibrechte und sudoers-Freigaben.
* `Installer/update.py`: GitHub-Update, Backup, Websync und Neustart.
* `Installer/backup.py` / `Installer/rollback.py`: lokale Sicherung und
  Rücksprung.
* `Installer/config_wizard.py`: Migration und interaktive Konfiguration;
  aktuelle Web-Einstellungen liegen in `data/e3dc_v4.json`.

## Dienste

Typische systemd-Dienste:

```text
e3dc-live
e3dc-epex-manager
e3dc-weather-manager
e3dc-storage-simulator
e3dc-storage-manager
e3dc-notifier
e3dc-wallbox-manager
e3dc-mqtt-hub
e3dc-matter-bridge
energy_manager
e3dc-websocket
apache2
```

Der alte Dienst `e3dc.service` und `screen`-Sessions sind keine normalen
Installationsziele mehr. Sie dürfen nur noch für Migration oder Rückbau
erkannt, gestoppt oder bereinigt werden.

## Konfiguration und Pfade

* `/var/www/html/data/e3dc_v4.json`: kanonische Konfiguration.
* `~/Install/data/e3dc_v4.json`: lokale Projektkopie bzw. Migrationsquelle.
* `/var/www/html/ramdisk`: flüchtige Live-, Plan- und Statusdateien.
* `/var/www/html/e3dc_paths.json`: Pfade für WebUI und venv.
* `UPDATE_POLICY.json`: Update-Nacharbeiten, Pakete, Dienste und Rechte.
* `~/Install/logs/install.log`: Installer-Log.

## Update-Ablauf

1. Backup erstellen.
2. `git fetch origin main` und Aktualisierung auf den GitHub-Stand.
3. Webdateien nach `/var/www/html` synchronisieren.
4. `--fix-permissions` ausführen.
5. Relevante Dienste neu starten.

Bei Web-Update-Problemen:

```bash
cd ~/Install
sudo python3 installer_main.py --fix-permissions
sudo python3 installer_main.py --check
sudo python3 installer_main.py --update-e3dc
```

## Diagnose

* `systemctl status <dienst> --no-pager`
* `journalctl -u <dienst> -n 80 --no-pager`
* WebUI: Installationszentrale -> Diagnosepaket

Diagnosepakete enthalten technische Daten und maskierte Konfiguration. Vor dem
Teilen bitte kurz prüfen, ob keine privaten Zusatzdateien enthalten sind.

*Dokumentation Stand: 5.3.2b / Juli 2026*
