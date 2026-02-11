# E3DC-Control Installer - Auto-Update Dokumentation

## Übersicht

Der Installer verfügt über eine integrierte Auto-Update-Funktion, die beim Start automatisch überprüft, ob eine neue Version des Installers auf GitHub verfügbar ist.

## Funktionsweise

### Automatische Prüfung beim Start

Wenn der Installer mit `sudo python3 installer_main.py` ausgeführt wird, geschieht folgendes:

1. **Versions-Check** (stillschweigend)
   - Die aktuelle Version wird aus der `VERSION`-Datei oder dem Git-Commit ermittelt
   - Die neueste Release von GitHub wird abgerufen

2. **Bei neuer Version verfügbar**
   - Der Benutzer wird gefragt, ob das Update installiert werden soll
   - Falls "Ja" (j): Die neue Version wird heruntergeladen, entpackt und installiert
   - Der Installer wird automatisch neu gestartet
   - Falls "Nein": Das Menü wird normal angezeigt

3. **Bei aktueller Version**
   - Das Hauptmenü wird direkt angezeigt (ohne Ausgabe)

### Manuelles Update über das Menü

Zusätzlich ist im Hauptmenü die Option **"0) Installer aktualisieren"** verfügbar, um Updates manuell zu prüfen und zu installieren.

## Technische Details

### Module

- **self_update.py**: Enthält die gesamte Update-Logik
- **Hauptskript**: `installer_main.py` ruft die Update-Prüfung auf

### Versionsverwaltung

Die Version wird aus folgenden Quellen ermittelt (in dieser Reihenfolge):

1. `VERSION`-Datei im Installer-Verzeichnis
2. Git-Commit (Falls vorhanden)
3. "unknown" (Falls nichts gefunden)

### GitHub-API Integration

- Nutzt die GitHub REST API v3
- Holt Informationen vom neuesten Release
- Sucht nach `.zip` Download-Links

### Sicherheit

- **Backup**: Vor dem Update wird eine Sicherung des alten Installers erstellt
- **Restore**: Bei Fehlern wird automatisch die alte Version wiederhergestellt
- **Validierung**: ZIP-Datei wird auf Struktur überprüft

## Installation und GitHub-Konfiguration

### Repository-Setup

Das Repository muss auf GitHub folgende Struktur haben:

```
Install-E3DC-Control/
├── Install/
│   ├── installer_main.py
│   ├── VERSION
│   └── Installer/
│       ├── core.py
│       ├── self_update.py
│       └── ... (andere Module)
└── README.md
```

### GitHub Release erstellen

1. Bearbeite die `VERSION`-Datei mit der neuen Versionsnummer
2. Committe die Änderung: `git commit -m "Version x.x.x"`
3. Tag erstellen: `git tag v1.x.x`
4. Push: `git push origin main --tags`
5. Auf GitHub -> Releases -> "Draft a new release"
6. Wähle den Tag aus und erstelle das Release
7. **Wichtig**: Lade die `Install.zip` Datei mit folgender Struktur hoch:
   ```
   Install-E3DC-Control/
   └── Install/
       ├── installer_main.py
       ├── VERSION
       └── Installer/
           └── ... (alle Module)
   ```

## Troubleshooting

### "Netzwerkfehler beim Abrufen der Release-Informationen"

- Internetverbindung prüfen
- GitHub-API ist eventuell überlastet (Rate Limiting)
- Firewall prüft GitHub-Zugriff

### "Installer-Verzeichnis nicht in ZIP gefunden"

- Überprüfe die ZIP-Datei-Struktur
- Muss `Install/` Verzeichnis enthalten
- Kann in Root oder in einem Ordner namens `Install-E3DC-Control/` sein

### Update wurde unterbrochen

- Eine Sicherung befindet sich in `Install.backup`
- Manual Recovery:
  ```bash
  sudo rm -rf ~/Install
  sudo mv ~/Install.backup ~/Install
  ```

## Beispiel-Ablauf

### Szenario: Neue Version verfügbar

```
→ Prüfe Updates…

Installierte Version: 1.0.0
Neueste Version:      1.1.0

→ Neue Version verfügbar!

Änderungen:
----------------------------------------
- Neue Features hinzugefügt
- Bugs behoben
- Performance verbessert
----------------------------------------

Soll die neue Version jetzt installiert werden? (j/n): j

→ Lade Release herunter…
✓ Download abgeschlossen (5.2 MB)
→ Entpacke Update…
→ Aktualisiere Installer-Verzeichnis…
  → Sicherung erstellt: /home/pi/Install.backup
✓ Update erfolgreich installiert

→ Installer wird neu gestartet…

[Installer startet neu mit Version 1.1.0]
```

## Deaktivierung der Auto-Prüfung

Um die automatische Update-Prüfung zu deaktivieren, kommentiere folgende Zeilen in `installer_main.py` aus:

```python
# check_for_updates()  # <-- auskommentieren
```

Die manuelle Update-Option im Menü bleibt verfügbar.
