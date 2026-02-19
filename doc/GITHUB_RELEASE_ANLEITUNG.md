# GitHub Release-Anleitung für Auto-Updates

## Checkliste für den ersten Release

- [ ] Repository auf GitHub erstellt und Struktur stimmt
- [ ] `VERSION` Datei mit Versionsnummer erstellt (z.B. "1.0.0")
- [ ] `self_update.py` Modul im `Installer/` Verzeichnis vorhanden
- [ ] `installer_main.py` wurde aktualisiert

## Release erstellen (Schritt für Schritt)

### 1. Vorbereitung im lokalen Repository

```bash
# aktuelle Version in VERSION-Datei aktualisieren
echo "1.0.0" > Install/VERSION

# Änderungen committen
git add Install/VERSION
git commit -m "Release 1.0.0"

# Tag erstellen (wichtig für Auto-Update!)
git tag v1.0.0

# Alles zu GitHub pushen
git push origin main
git push origin v1.0.0
```

### 2. Release-ZIP erstellen

Das Skript muss in folgender Struktur auf GitHub verfügbar sein:

```bash
# ZIP mit korrekter Struktur erstellen
zip -r Install-E3DC-Control.zip Install/

# Wichtig: ZIP muss diese Struktur haben:
# Install-E3DC-Control/
# └── Install/
#     ├── installer_main.py
#     ├── VERSION
#     └── Installer/
#         ├── self_update.py
#         ├── core.py
#         └── ...alle anderen Module
```

### 3. GitHub Release erstellen

1. Gehe zu GitHub -> Repository -> "Releases"
2. Klicke "Create a new release"
3. Wähle den gerade erstellten Tag (v1.0.0)
4. **Release title**: "Version 1.0.0"
5. **Description**: Schreibe die Änderungen:
   ```
   ## Neuerungen
   - Feature 1
   - Feature 2
   
   ## Bugfixes
   - Bug 1 behoben
   
   ## Installation
   Lade die `Install-E3DC-Control.zip` herunter
   ```
6. **Assets hochladen**:
   - Klicke "Attach binaries..."
   - Wähle die `Install-E3DC-Control.zip` aus
7. Klicke "Publish release"

## Automatischer Release-Upload (Optional)

Für GitHub Actions Workflow (`.github/workflows/release.yml`):

```yaml
name: Create Release

on:
  push:
    tags:
      - 'v*'

jobs:
  release:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v3
      
      - name: Create ZIP
        run: |
          zip -r Install-E3DC-Control.zip Install/
      
      - name: Create Release
        uses: softprops/action-gh-release@v1
        with:
          files: Install-E3DC-Control.zip
        env:
          GITHUB_TOKEN: ${{ secrets.GITHUB_TOKEN }}
```

## Testen der Auto-Update-Funktion

### Lokal testen (mit Mock-Server)

```python
# test_self_update.py
from Installer.self_update import get_latest_release_info

# Teste Release-Abruf
info = get_latest_release_info()
if info:
    print(f"✓ Neueste Version: {info['version']}")
    print(f"  Download-URL: {info['download_url']}")
else:
    print("✗ Release-Abfrage fehlgeschlagen")
```

### Auf dem Pi testen

```bash
# Update-Prüfung manuell testen
sudo python3 /home/pi/Install/installer_main.py

# Sollte prüfen ob Updates verfügbar sind
# und ggfs. zum Update fragen
```

## Versionsformat

Verwende [Semantic Versioning](https://semver.org/):
- **Major**: `1.0.0` - Breaking Changes
- **Minor**: `1.1.0` - Neue Features
- **Patch**: `1.0.1` - Bugfixes

Beispiele:
```
1.0.0  (Initiales Release)
1.0.1  (Bugfix)
1.1.0  (Neue Features)
2.0.0  (Breaking Changes)
```

## Troubleshooting

### "Installer-Verzeichnis nicht in ZIP gefunden"

Die ZIP-Datei muss folgende Struktur haben:
❌ Falsch:
```
installer_main.py
VERSION
Installer/
```

✅ Richtig:
```
Install-E3DC-Control/
└── Install/
    ├── installer_main.py
    ├── VERSION
    └── Installer/
```

### Update wird nicht erkannt

1. Überprüfe ob Tag (z.B. `v1.0.0`) existiert: `git tag -l`
2. Überprüfe GitHub Releases Seite
3. Prüfe ZIP-Download-Link im Release
4. Teste API manuell:
   ```bash
   curl -s https://api.github.com/repos/A9xxx/Install-E3DC-Control/releases/latest | jq .
   ```

## Weitere Informationen

- [GitHub Releases API](https://docs.github.com/en/rest/releases)
- [Semantic Versioning](https://semver.org/)
- [GitHub Actions Dokumentation](https://docs.github.com/en/actions)
