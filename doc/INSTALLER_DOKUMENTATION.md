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
- Gezielte Deinstallation einzelner Komponenten oder des gesamten Systems.
- Ein Notfall-Modus zur automatischen Fehlerbehebung.

## 2. Voraussetzungen

- **Betriebssystem:** Ein Debian-basiertes Linux (z.B. Raspberry Pi OS).
- **Abhängigkeiten:** Git muss auf dem System installiert sein.
- **Python:** Python 3.7 oder neuer.
- **Rechte:** Das Hauptskript muss mit `sudo` ausgeführt werden, da es Systempakete installiert und systemweite Änderungen vornimmt.

## 3. Ausführung

Der Installer wird über das Hauptskript `installer_main.py` gestartet. Er unterstützt mittlerweile beliebige Installationsverzeichnisse (z.B. Git-Clones) und ist nicht mehr stur auf den Pfad `~/Install` festgelegt.

```bash
sudo python3 installer_main.py
```

Nach dem Start erscheint ein interaktives, in Kategorien unterteiltes Menü, das über eine Suchfunktion verfügt.

### Automatischer Modus (`--unattended`)

Für die Ausführung durch andere Skripte (z.B. PHP aus dem Webportal) gibt es einen unbeaufsichtigten Modus. Blockierende Eingabeaufforderungen wurden hier entfernt, sodass automatische Installationen vollständig ohne Interaktion durchlaufen.

```bash
sudo python3 installer_main.py --unattended
```

## 4. Hauptkomponenten (Die Skripte im `Installer`-Ordner)

### `installer_main.py`
Dies ist der zentrale Einstiegspunkt. Seine Aufgaben sind:
- Starten der Logging-Funktion.
- Prüfen der Python-Version und der `sudo`-Rechte.
- Warnung ausgeben, falls eine parallele Installation im Standardpfad gefunden wird.
- **Selbst-Update:** Prüfen, ob eine neue Version des Installers auf GitHub verfügbar ist und diese bei Bedarf aktualisieren.
- Sicherstellen, dass ein Installationsbenutzer ausgewählt wurde.
- Erzwingen der Anlage der `e3dc_paths.json` mit korrekten Berechtigungen.
- Starten des interaktiven Hauptmenüs (aus `core.py`), außer im `--unattended`-Modus.

### `core.py`
Dieses Skript ist für die Darstellung und Logik des interaktiven Hauptmenüs zuständig. Es sammelt alle verfügbaren Aktionen und zeigt sie dem Benutzer in Kategorien (Installation, Konfiguration, System, Erweiterungen, Backup) zur Auswahl an.

### `install_all.py`
Führt die komplette Neuinstallation in einer festen, logischen Reihenfolge durch:
1.  Initiale Korrektur von Berechtigungen.
2.  Installation von System-Abhängigkeiten und Einrichtung des Python Virtual Environments (Standard: `~/.venv_e3dc`). Eine bestehende venv-Umgebung wird automatisch erkannt und genutzt.
3.  Klonen des `E3DC-Control` Git-Repositorys und Kompilieren des C-Programms via `make`.
4.  Einrichten des PHP-Webportals und automatische Installation des Diagramm-Systems.
5.  Erstellen der initialen Konfigurationsdateien.
6.  Optionale Konfiguration von Strompreisen.
7.  Einrichten des System-Dienstes für den automatischen Betrieb.
8.  Einrichten einer RAM-Disk zur Schonung der SD-Karte.
9.  Finale Verifizierung aller Berechtigungen.
10. Erstellen eines ersten Backups.

### `permissions.py`
Stellt sicher, dass die Dateiberechtigungen immer korrekt sind, damit sowohl der ausführende Benutzer als auch der Webserver (`www-data`) korrekt auf die Dateien zugreifen können.
- **Struktur & Sicherheit:** Die Logik ist in `check_*`- und `fix_*`-Funktionen getrennt. Eine zentrale `FILE_DEFINITIONS` Liste definiert den Soll-Zustand.
- **Zusatzfunktionen:** Erkennt und entfernt doppelte Variablen in der `e3dc.config.txt`. Prüft Schreibrechte für temporäre Web-Verzeichnisse.
- **System-Integration:** Richtet Sudoers-Dateien (`010_e3dc_web_update`, `010_e3dc_web_git`) ein und wird am Ende des Update-Skripts immer automatisch ausgeführt.

### `update.py`
Realisiert den Update-Prozess für E3DC-Control:
- **Sicherheit:** Führt zu Beginn ein Backup durch und sichert lokale Änderungen via `git stash`.
- **Flexibilität:** Bietet Optionen, um eine Installation zu erzwingen (Re-Install) oder lokale Änderungen zu verwerfen (`git reset --hard`).
- **Web-Portal:** Aktualisiert zuverlässig die Web-Oberfläche durch Extraktion der `E3DC-Control.zip`.
- **Auto-Update:** Kann täglich zu einer festgelegten Zeit automatisch aktualisieren und nutzt dabei Richtlinien aus der `UPDATE_POLICY.json`.

### `backup.py`
Verwaltet den Backup-Lebenszyklus:
- Erstellt intelligente Backups in Zeitstempel-Ordnern. Diese beinhalten nun auch Watchdog-Skripte, Systemd-Dateien, `e3dc_paths.json` und Spezial-Konfigurationen.
- Erstellt automatisch ein Sicherheits-Backup vor jedem Rollback oder Update.
- Listet verfügbare Backups auf, stellt diese wieder her und löscht alte Versionen.

### `install_watchdog.py`
Ein zentraler Installer für den Watchdog-Dienst (`piguard`):
- Überwacht, ob Dateien (z.B. Logfiles) regelmäßig aktualisiert werden. Stoppt die Aktualisierung, wird der E3DC-Dienst gezielt neu gestartet.
- Ermöglicht die Konfiguration von IP-Überwachung, SD-Karten-Warnungen und Telegram-Benachrichtigungen.

### Weitere wichtige Helfer
- **`config_wizard.py`:** Ein einfacher Assistent zur Bearbeitung der `e3dc.config.txt`.
- **`installer_config.py`:** Verwaltet die Konfiguration des Installers selbst in `installer_config.json`.
- **`service_setup.py`:** Richtet E3DC-Control als echten Systemd-Service (`e3dc.service`) ein, was den alten Crontab-Autostart ersetzt.

### Erweiterungsmodule
- **Webportal (`diagrammphp.py`):** Richtet das PHP-Frontend ein. Prüft beim Start die Version des Webportals und bietet primär Konfigurations-Optionen an, falls dieses aktuell ist, um versehentliche Neuinstallationen zu verhindern.
- **RAM-Disk (`ramdisk.py`):** Konfiguriert den SD-Karten-Schutz und richtet den `e3dc-grabber` Systemd-Service für die Live-Daten ein.
- **Luxtronik (`install_luxtronik.py`):** Installiert den `energy_manager` für die Wärmepumpen-Steuerung als eigenständigen Systemd-Service.
- **Lademanagement (`install_lademanagement.py`):** Eine schlankere Installationsroutine für die intelligente Wallbox-Steuerung ohne steuerbare Wärmepumpe.

## 5. Konfigurationsdateien

- **`e3dc.config.txt`**: Die Hauptkonfigurationsdatei. Sie dient nun auch als zentrale Konfiguration für den Luxtronik Energy Manager.
- **`installer_config.json`**: Speichert die Konfiguration des Installers selbst (z.B. den Installationsbenutzer).
- **`e3dc_paths.json`**: Speichert explizit den Pfad zum Python Virtual Environment.
- **`UPDATE_POLICY.json`**: Wird bei Releases mitgeliefert und teilt dem Updater mit, welche Aktionen nach der Installation ausgeführt werden müssen.

## 6. Diagnose, Wartung & Logging

Der Installer bietet weitreichende Möglichkeiten zur Fehlersuche:
- **Notfall-Modus (Menü 99):** Ein Assistent, der bei Problemen automatisch eine Rechte-Reparatur, Service-Einrichtung und einen Watchdog-Check durchführt.
- **Erweiterter Status-Check:** Prüft Internetverbindung, CPU-Temperatur, RAM-Disk-Status und zeigt Service-Logs bei Fehlern an.

Log-Dateien befinden sich im `logs/`-Verzeichnis:
- **`install.log`**: Das allgemeine Log für die meisten Aktionen.
- **`permissions.log`**: Log für Aktionen des Berechtigungs-Skripts.
- **`energy_manager.log`**: Protokolliert Update-Prüfungen und Ergebnisse der Wärmepumpen-Steuerung.