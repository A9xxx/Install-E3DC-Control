# Quick-Start: Auto-Update-Aktivierung

## 5-Minuten Anleitung

### 1. Dateien lokal installieren

Das Installer-Verzeichnis sollte folgende neue Dateien enthalten:

```
Install/
├── installer_main.py          (AKTUALISIERT)
├── VERSION                     (NEU)
├── AUTO_UPDATE_DOKU.md        (NEU - Dokumentation)
├── GITHUB_RELEASE_ANLEITUNG.md (NEU - GitHub Setup)
├── test_auto_update.py        (NEU - Tests)
└── Installer/
    ├── __init__.py            (AKTUALISIERT)
    └── self_update.py         (NEU)
```

### 2. Installer-Änderungen überprüfen

```bash
# Überprüfe dass installer_main.py die neue check_for_updates() Funktion hat
grep -n "check_for_updates" ~/Install/installer_main.py

# Sollte etwa so aussehen:
# 37: def check_for_updates():
# 71: check_for_updates()
```

### 3. Lokal testen (Optional)

```bash
# Test-Script ausführen
python3 ~/Install/test_auto_update.py

# Sollte mit "✓ Alle Tests erfolgreich" enden
```

### 4. GitHub vorbereiten

```bash
cd ~/Install

# 4a. VERSION-Datei aktualisieren
echo "1.0.0" > VERSION

# 4b. Lokale Änderungen committen
git add .
git commit -m "Add Auto-Update functionality"

# 4c. Tag erstellen (wichtig!)
git tag v1.0.0

# 4d. Zu GitHub pushen
git push origin main
git push origin v1.0.0
```

### 5. GitHub-Release erstellen

Gehe zu: `https://github.com/A9xxx/Install-E3DC-Control/releases`

1. Klicke **"Create a new release"**
2. Wähle Tag: **v1.0.0**
3. Release Title: **Version 1.0.0**
4. Description:
   ```
   ## What's New
   - Auto-Update-Funktion hinzugefügt
   - Installer prüft automatisch auf neue Versionen
   
   ## Installation
   Das Update wird beim nächsten Start automatisch erkannt.
   ```
5. Lade ZIP hoch: **Install-E3DC-Control.zip**
   - Struktur: `Install-E3DC-Control/Install/...`
6. Klicke **"Publish release"**

### 6. Fertig! 🎉

Die Auto-Update-Funktion ist jetzt aktiv:

```bash
# Installer starten
sudo python3 ~/Install/installer_main.py

# Beim nächsten Update:
# → Installer prüft GitHub ab
# → Fragt ob aktualisieren
# → Lädt neue Version herunter
# → Startet automatisch neu
```

## Was passiert beim Update?

### Automatische Prüfung (beim Start)

```
sudo python3 installer_main.py

→ (Still) Prüfung ob Updates verfügbar...

[Falls Update verfügbar]
Neue Version verfügbar!

Soll die neue Version jetzt installiert werden? (j/n): j

→ Lade Release herunter…
✓ Download abgeschlossen
→ Entpacke Update…
✓ Update erfolgreich installiert

→ Installer wird neu gestartet…
```

### Manuelles Update (aus Menü)

```
E3DC-Control Installer

  0) Installer aktualisieren
  1) Rechte prüfen & korrigieren
  ...

Auswahl: 0

=== Installer-Update Prüfung ===

Installierte Version: 1.0.0
Neueste Version:      1.0.1

→ Neue Version verfügbar!
[...]
```

## Häufig gestellte Fragen

### F: Wird der Installer automatisch updated?
A: Ja, beim Start werden neue Versionen erkannt. Der Benutzer wird gefragt, ob das Update installiert werden soll. Bei "Ja" wird automatisch heruntergeladen und installiert.

### F: Was passiert bei Netzwerkfehlern?
A: Fehler werden still ignoriert und der Installer startet normal. Die Auto-Update-Prüfung ist nicht-kritisch.

### F: Wie kann ich Auto-Update deaktivieren?
A: Kommentiere diese Zeile in `installer_main.py` aus:
```python
# check_for_updates()  # <-- auskommentieren
```

### F: Welche Python-Version wird benötigt?
A: Python 3.7+ (wird am Start geprüft)

### F: Funktioniert das auch ohne sudo?
A: Nein, der Installer benötigt sudo. Bio-Einrichtung ist daher auch mit sudo erforderlich.

## Weitere Informationen

- 📖 [Ausführliche Dokumentation](AUTO_UPDATE_DOKU.md)
- 🚀 [GitHub Release Setup](GITHUB_RELEASE_ANLEITUNG.md)
- 🧪 [Test-Script](test_auto_update.py)

## Troubleshooting

### "Installer-Verzeichnis nicht in ZIP gefunden"

ZIP-Struktur prüfen:
```bash
unzip -l Install-E3DC-Control.zip | head -20

# Sollte beginnen mit:
# Archive: Install-E3DC-Control.zip
#   Length     Date   Time    Name
# -------- ---------- ----- ----
#        0  2024-02-11 10:00   Install-E3DC-Control/
#        0  2024-02-11 10:00   Install-E3DC-Control/Install/
#        ...
```

### "Release nicht auf GitHub sichtbar"

Prüfung Checkliste:
- [ ] Git tag erstellt? `git tag -l | grep v1`
- [ ] Zu GitHub gepusht? `git push origin v1.0.0`
- [ ] Release auf GitHub erstellt?
- [ ] ZIP herunterladbar? (Test-Download)

### Installer lädt nicht herunter

```bash
# Verwende curl zum Testen
curl -I "https://github.com/A9xxx/Install-E3DC-Control/releases/download/v1.0.0/Install-E3DC-Control.zip"

# Sollte HTTP 200 zurückgeben
```

---

**Brauchen Sie weitere Hilfe?** Siehe [Ausführliche Dokumentation](AUTO_UPDATE_DOKU.md)
