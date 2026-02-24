# Dokumentation des E3DC-Control Installers

Dieses Dokument beschreibt die Architektur und die Komponenten des E3DC-Control Installers. Das System ist modular aufgebaut, um Wartung und Erweiterungen zu vereinfachen.

## 1. Überblick

Der Installer ist ein Python-basiertes Kommandozeilen-Tool, das den gesamten Lebenszyklus von E3DC-Control auf einem Raspberry Pi (oder einem ähnlichen Debian-basierten System) verwaltet.

Zu seinen Hauptaufgaben gehören:
- Eine geführte, komplette Neuinstallation aller benötigten Komponenten.
- Ein sicherer und robuster Update-Mechanismus.
- Ein ausgeklügeltes System zur Überprüfung und Korrektur von Dateiberechtigungen.
- Eine Backup- und Wiederherstellungsfunktion.
- Assistenten zur Vereinfachung der Konfiguration.

## 2. Voraussetzungen

- **Betriebssystem:** Ein Debian-basiertes Linux (z.B. Raspberry Pi OS).
- **Python:** Python 3.7 oder neuer.
- **Rechte:** Das Hauptskript muss mit `sudo` ausgeführt werden, da es Systempakete installiert und systemweite Änderungen vornimmt.

## 3. Ausführung

Der Installer wird über das Hauptskript `installer_main.py` im `Install`-Verzeichnis gestartet.

```bash
sudo python3 installer_main.py
```

Nach dem Start erscheint ein interaktives Menü, aus dem die gewünschte Aktion ausgewählt werden kann.

### Automatischer Modus (`--unattended`)

Für die Ausführung durch andere Skripte (z.B. PHP aus dem Webportal) gibt es einen unbeaufsichtigten Modus. In diesem Modus werden keine benutzereingaben erwartet. Er wird für den Auto-Update-Mechanismus des Installers selbst genutzt.

```bash
sudo python3 installer_main.py --unattended
```

## 4. Hauptkomponenten (Die Skripte im `Installer`-Ordner)

### `installer_main.py`
Dies ist der zentrale Einstiegspunkt. Seine Aufgaben sind:
- Starten der Logging-Funktion.
- Prüfen der Python-Version und der `sudo`-Rechte.
- **Selbst-Update:** Prüfen, ob eine neue Version des Installers auf GitHub verfügbar ist und diese bei Bedarf aktualisieren.
- Sicherstellen, dass ein Installationsbenutzer ausgewählt wurde und diesen in der `installer_config.json` speichern.
- Starten des interaktiven Hauptmenüs (aus `core.py`), außer im `--unattended`-Modus.

### `core.py`
Dieses Skript ist für die Darstellung und Logik des interaktiven Hauptmenüs zuständig. Es sammelt alle verfügbaren Aktionen (die in anderen Skripten mit `register_command` registriert wurden) und zeigt sie dem Benutzer zur Auswahl an.

### `install_all.py`
Führt die komplette Neuinstallation in einer festen, logischen Reihenfolge durch:
1.  Initiale Korrektur von Berechtigungen.
2.  Installation von System-Abhängigkeiten (z.B. `apache2`, `php`, `build-essential`).
3.  Klonen des `E3DC-Control` Git-Repositorys und Kompilieren des C-Programms via `make`.
4.  Einrichten des PHP-Webportals.
5.  Erstellen der initialen Konfigurationsdateien (`e3dc.config.txt`, `e3dc.wallbox.txt`).
6.  Optionale Konfiguration von Strompreisen.
7.  Einrichten von `cron` und `screen` für den automatischen Betrieb.
8.  Einrichten einer RAM-Disk zur Schonung der SD-Karte.
9.  **Finale Verifizierung aller Berechtigungen.**
10. Erstellen eines ersten Backups.

### `permissions.py`
Dies ist eine der wichtigsten und komplexesten Komponenten. Sie stellt sicher, dass die Dateiberechtigungen immer korrekt sind, damit sowohl der ausführende Benutzer als auch der Webserver (`www-data`) korrekt auf die Dateien zugreifen können.
- **Struktur:** Die Logik ist sauber in `check_*`- und `fix_*`-Funktionen getrennt. Zuerst werden alle Probleme gesammelt, dann wird dem Benutzer eine Korrektur angeboten.
- **`FILE_DEFINITIONS`:** Eine zentrale Liste definiert den Soll-Zustand (Besitzer, Gruppe, Modus) für jede wichtige Datei. Dies macht die Logik wartbar und übersichtlich.
- **Sicherheits-Features:** Das Skript kann proaktiv fälschlicherweise `root` gehörende Dateien korrigieren und prüft auch den `execute`-Status von Home-Verzeichnissen, eine häufige Fehlerquelle.

### `update.py`
Realisiert einen sehr sicheren Update-Prozess für eine bestehende `E3DC-Control` Installation:
1.  **Backup:** Führt zu Beginn immer ein Backup der bestehenden Version durch.
2.  **Lokale Änderungen:** Erkennt lokale, nicht gespeicherte Änderungen des Benutzers und sichert diese automatisch via `git stash`.
3.  **Update:** Führt `git pull` und `make` aus.
4.  **Abschluss:** Korrigiert die Berechtigungen und bietet an, die zuvor gesicherten lokalen Änderungen (`git stash pop`) wiederherzustellen.

### `backup.py`
Verwaltet den Backup-Lebenszyklus:
- **`backup_current_version`:** Erstellt ein intelligentes, selektives Backup der wichtigsten Dateien in einem Zeitstempel-Ordner.
- **`restore_backup`:** Listet verfügbare Backups auf und stellt eine ausgewählte Version nach einer Sicherheitsabfrage wieder her.
- **`delete_backup`:** Löscht alte, nicht mehr benötigte Backups.

### Weitere wichtige Helfer
- **`config_wizard.py`:** Ein einfacher Assistent zur Bearbeitung der `e3dc.config.txt`, um Fehleingaben zu vermeiden.
- **`installer_config.py`:** Verwaltet die Konfiguration des Installers selbst (z.B. den Namen des Installationsbenutzers), gespeichert in `installer_config.json`.
- **`utils.py`:** Stellt Hilfsfunktionen wie `run_command` oder `replace_in_file` bereit, die von vielen Modulen genutzt werden.
- **`system.py`:** Kümmert sich um die Installation von APT-Paketen.

## 5. Konfigurationsdateien

- **`pi/Install/Installer/installer_config.json`**: Speichert die Konfiguration des Installers selbst. Hauptsächlich der gewählte Installationsbenutzer, um die Abfrage nicht bei jedem Start zu wiederholen.
- **`E3DC-Control/e3dc.config.txt`**: Die Hauptkonfigurationsdatei für die C-Anwendung `E3DC-Control`. Wird über den `config_wizard.py` bearbeitet.

## 6. Logging

Der Installer schreibt detaillierte Log-Dateien, die für die Fehlersuche unerlässlich sind. Sie befinden sich im `pi/Install/logs/`-Verzeichnis.
- **`install.log`**: Das allgemeine Log für die meisten Aktionen des Installers.
- **`permissions.log`**: Ein dediziertes Log für alle Aktionen des Berechtigungs-Skripts. Sehr nützlich, wenn es Probleme mit dem Zugriff auf Dateien gibt.
