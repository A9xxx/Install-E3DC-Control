# E3DC-Control Installer

Ein automatisierter Installer für E3DC-Control auf Raspberry Pi. Das Skript kümmert sich um die komplette Einrichtung, Updates und Wartung.

## 📋 Voraussetzungen

- **Raspberry Pi** (getestet auf Pi 4, funktioniert auch auf älteren Versionen)
- **Raspberry Pi OS** (Bullseye oder neuer empfohlen)
- **Python 3.7+** (normalerweise vorinstalliert)
- **Internet-Verbindung** (für GitHub Updates)
- **sudo-Rechte** (erforderlich)

## 🚀 Installation

### Option 1: Mit Git (empfohlen)

```bash
# Git installieren (falls nicht vorhanden)
sudo apt update
sudo apt install git -y

# Repository klonen
cd ~
git clone https://github.com/A9xxx/Install-E3DC-Control.git Install

# In Verzeichnis wechseln
cd Install

# Installer starten
sudo python3 installer_main.py
```

### Option 2: Mit Release-ZIP

```bash
# ZIP herunterladen und entpacken
cd ~
wget https://github.com/A9xxx/Install-E3DC-Control/releases/latest/download/Install-E3DC-Control.zip
unzip Install-E3DC-Control.zip

# In Verzeichnis wechseln
cd Install-E3DC-Control/Install

# Installer starten
sudo python3 installer_main.py
```

## 📖 Was macht der Installer?

Der Installer ist modular aufgebaut und bietet folgende Funktionen:

### Hauptfunktionen
- **Installer aktualisieren** - Prüft automatisch auf GitHub Updates und instaliert diese
- **Rechte prüfen & korrigieren** - Stellt sicher, dass alle Dateien korrekte Besitzer haben
- **Backup verwalten** - Erstellt und verwaltet Sicherungen
- **Update E3DC-Control** - Aktualisiert die E3DC-Control Software
- **Diagramm und PHP einrichten** - Installiert Monitoring-Tools
- **Configuration bearbeiten** - Ermöglicht Konfigurationsänderungen
- **E3DC-Konfiguration erstellen** - Assistiert bei der Ersteinrichtung
- **Screen + Cronjob einrichten** - Automatisiert die Ausführung
- **Systempakete installieren** - Lädt notwendige Abhängigkeiten
- **E3DC-Control neu installieren** - Vollständige Neuinstallation
- **E3DC-Control starten** - Startet die Anwendung
- **Alles installieren** - Automatische Vollinstallation
- **Rollback (Backup)** - Stellt eine frühere Version wieder her
- **Rollback (Commit-Auswahl)** - Wählt einen spezifischen Git-Commit
- **Strompreis-Wizard** - Konfiguriert Strompreise
- **Deinstallation** - Entfernt E3DC-Control vollständig

### Update-System

Der Installer prüft beim Start automatisch auf neue Versionen:

```
→ Neue Version verfügbar!
Soll die neue Version jetzt installiert werden? (j/n): j

→ Lade Release herunter…
✓ Download abgeschlossen (3.5 MB)
→ Entpacke Update…
→ Aktualisiere Installer-Verzeichnis…
✓ Update erfolgreich installiert
✓ VERSION-Datei aktualisiert
✓ Rechte für /home/pi/Install auf pi:pi gesetzt
→ Installer wird neu gestartet…
```

**Besonderheiten:**
- 🔄 **Automatischer Neustart** nach Update
- 🔐 **Rechtevergabe** wird automatisch korrigiert (wichtig für WinSCP)
- 💾 **Backup** wird vor dem Update erstellt
- 🔄 **Rollback** möglich bei Fehlern

## 🏗️ Projektstruktur

```
Install/
├── installer_main.py          # Haupteinstiegspunkt (mit sudo starten)
├── Installer/
│   ├── core.py               # Menü und Kommando-System
│   ├── self_update.py        # Update-System (automatisches Prüfen)
│   ├── utils.py              # Hilfsfunktionen
│   └── commands/             # Modulare Befehle
│       ├── __init__.py
│       ├── backups.py        # Backup-Verwaltung
│       ├── config.py         # Konfiguration
│       ├── installation.py   # Installation von E3DC-Control
│       └── ...
├── VERSION                    # Versionsnummer (wird auto-aktualisiert)
└── .gitignore                # Git-Ignorieren (Cache, Backups, etc.)
```

## 🔧 Verwendung

### Starten des Installers

```bash
cd ~/Install
sudo python3 installer_main.py
```

### Update-Prüfung

Der Installer prüft beim Start automatisch auf Updates. Um sofort zu aktualisieren:

```
Auswahl: 0
→ Neue Version verfügbar!
Soll die neue Version jetzt installiert werden? (j/n): j
```

### Backup und Rollback

```
Auswahl: 2
→ Backup verwalten
  1) Backup erstellen
  2) Rollback (aus Sicherung)
  3) Rollback (Commit-Auswahl)
```

## 🛂 Remote-Verwaltung mit WinSCP

Du kannst den Installer auch über WinSCP verwalten:

1. Verbinde dich mit deinem Raspberry Pi
2. Navigiere zu `/home/pi/Install`
3. Bearbeite Dateien direkt (z. B. Konfigurationen)
4. Starte den Installer über SSH: `sudo python3 installer_main.py`

**Wichtig:** Nach Updates sollten die Rechte automatisch korrigiert werden. Falls nicht:
```bash
sudo chown -R pi:pi /home/pi/Install
```

## 📦 Release erstellen

Um eine neue Version zu veröffentlichen:

```bash
# ZIP-Datei automatisch erstellen
cd ~
./create_release_zip.sh

# Auf GitHub ein neues Release erstellen
# https://github.com/A9xxx/Install-E3DC-Control/releases/new
```

Das Skript `create_release_zip.sh` erstellt automatisch die saubere ZIP-Datei ohne temporäre Dateien.

## 🔄 Git-Integration

Das Projekt nutzt Git für Versionskontrolle. Wichtige Befehle:

```bash
# In das Installationsverzeichnis gehen
cd ~/Install

# Status prüfen
git status

# Updates vom Repository holen
git pull origin master

# Aktuelle Version anzeigen
git log --oneline -5

# Zu einer bestimmten Version zurückgehen
git checkout v1.0.0
```

## ⚙️ Systemvoraussetzungen

Der Installer kümmert sich automatisch um:
- ✓ Python 3.7+
- ✓ sudo-Rechte
- ✓ Notwendige Systempakete
- ✓ Dateirechte und Besitzer
- ✓ Cronjobs für Automation

## 🐛 Fehlerbehebung

### "can't open file 'installer_main.py'"
**Lösung:** Mit absolutem Pfad starten oder cd in das Verzeichnis:
```bash
sudo python3 /home/pi/Install/installer_main.py
```

### "Permission denied"
**Lösung:** Rechte korrigieren:
```bash
sudo chown -R pi:pi /home/pi/Install
sudo chmod -R u+rwX,go+rX /home/pi/Install
```

### WinSCP kann keine Dateien speichern
**Lösung:** Rechtevergabe reparieren:
```bash
sudo chown -R pi:pi /home/pi/Install
```


## 👨‍💻 Autor

A9xxx


**Letzte Aktualisierung:** 11. Februar 2026
