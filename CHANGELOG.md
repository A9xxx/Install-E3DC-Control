# Changelog

Alle wichtigen Änderungen an diesem Projekt sind in dieser Datei dokumentiert.

Das Format basiert auf [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
und das Projekt folgt [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added
- Auto-Update-Funktion für den Installer
- GitHub-Release Integration
- Test-Script für Update-Funktion
- Ausführliche Dokumentation

### Changed
- `installer_main.py` - Neue Update-Prüfung beim Start

### Fixed
- (Zukünftige Bugfixes hier eintragen)

---

## Format für neue Releases

Verwende folgende Struktur für neue Versionen:

```markdown
## [1.x.x] - YYYY-MM-DD

### Added
- Neue Features hier eintragen
- Jeweils eine Zeile pro Feature

### Changed
- Änderungen bestehender Funktionen
- Format: "Beschreibung der Änderung"

### Fixed
- Behobene Bugs
- Format: "Behobenes Problem (Issue #XYZ)"

### Removed
- Entfernte Features
- Format: "Entfernte ungenutzten Code"

### Security
- Sicherheits-Patches
- Format: "Beschreibung des Patches"

### Deprecated
- Veraltete Features (werden in Zukunft entfernt)
```

---

## Versionierungsrichtlinien

**Versions-Format: MAJOR.MINOR.PATCH**

- **MAJOR**: Breaking Changes (z.B. Veränderung der API, Dateistrukturen)
- **MINOR**: Neue Features, die abwärtskompatibel sind
- **PATCH**: Bugfixes und kleine Verbesserungen

### Beispiele

```
1.0.0  → Erstes stabiles Release
1.0.1  → Bugfix
1.1.0  → Neue Features hinzugefügt
2.0.0  → Breaking Changes
```

---

## Branching-Strategie für Releases

```bash
# 1. Entwicklung auf 'develop' Branch
git checkout develop
git commit -m "Neue Features und Bugfixes"

# 2. Merge zu 'main' wenn Release ansteht
git checkout main
git merge develop

# 3. Version in VERSION-Datei aktualisieren
echo "1.1.0" > Install/VERSION
git add Install/VERSION
git commit -m "Bump version to 1.1.0"

# 4. Tag erstellen
git tag v1.1.0

# 5. Zu GitHub pushen (triggert GitHub Actions)
git push origin main v1.1.0
```

---

## GitHub Actions Automatisierung

Mit der Datei `.github/workflows/release.yml` werden Releases automatisch erstellt:

1. Ein Git-Tag mit Format `v1.x.x` wird gepusht
2. GitHub Actions erstellt automatisch:
   - ZIP-Archiv mit korrekter Struktur
   - GitHub Release mit Metadaten
   - Update der VERSION-Datei

---

## Template für Release Notes

Verwende diese Vorlage für GitHub Release Notes:

```markdown
## What's New in Version X.X.X

### Major Features
- Feature 1: Kurze Beschreibung
- Feature 2: Kurze Beschreibung

### Bug Fixes
- Fix 1: Problem XYZ wurde behoben
- Fix 2: Fehler ABC wurde korrigiert

### Performance Improvements
- Verbesserung 1: Detailbeschreibung
- Verbesserung 2: Detailbeschreibung

### Breaking Changes
- Falls vorhanden: Aufzählen und Migrationshilfe bereitstellen

### Installation
Die neue Version wird beim nächsten Start des Installers erkannt.
Zur manuellen Installation:
1. Lade `Install-E3DC-Control.zip` herunter
2. Entpacke in das Install-Verzeichnis
3. Starten Sie den Installer neu

### Commits in diesem Release
[COMMIT_SUMMARY_HERE]

---

### Danksagungen
Danke an alle Contributor und Tester!
```

---

## Tipps für gutes Changelog

1. **Aktuell halten**: Trage Änderungen during development ein, nicht erst beim Release
2. **Benutzer fokussieren**: Erkläre was sich für Benutzer ändert, nicht nur technische Details
3. **Gruppieren**: Nutze die Kategorien (Added, Fixed, Changed, etc.)
4. **Links verwenden**: Verlinke auf Issues/PRs wenn relevant
5. **Semantic Versioning**: Folge der Versionierung streng

---

## Beispiel-Changelog Entry

```markdown
## [1.2.0] - 2024-02-11

### Added
- Auto-Update Funktion für einfachere Upgrades (#42)
- GitHub Release Integration für automatisierte Deploys
- Umfangreiche Test-Suite für kritische Funktionen

### Changed
- Improved error handling in permission checks
- refactored `installer_main.py` for better modularity
- Updated documentation with comprehensive guides

### Fixed
- Fixed buggy version detection (Issue #40)
- Resolved edge case in ZIP extraction
- Corrected file permissions after installation

### Security
- Added input validation for GitHub API responses
- Improved backup/restore mechanism for safety
```

---

Weitere Informationen: https://keepachangelog.com/
