# Quickstart: E3DC-Control Installation

Diese Anleitung fasst die schnellsten Schritte zusammen, um E3DC-Control auf einem frischen Raspberry Pi OS (oder ähnlichem Debian-System) zu installieren.

## Schritt 1: System vorbereiten

Stellen Sie sicher, dass Ihr System auf dem neuesten Stand ist und `git` installiert ist.

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y git
```

## Schritt 2: Installer herunterladen

Klonen Sie das Repository, das den Installer enthält. Wenn Sie dieses Dokument lesen, haben Sie diesen Schritt wahrscheinlich schon erledigt. Falls nicht, hier ein Beispielbefehl (URL anpassen):

```bash
# Beispiel für das Klonen in das Home-Verzeichnis
cd ~
git clone [https://github.com/A9xxx/Install-E3DC-Control.git](https://github.com/A9xxx/Install-E3DC-Control.git) Install
cd Install
```
*Hinweis: Passen Sie die URL und die Verzeichnisnamen entsprechend an.*

## Schritt 3: Installation starten

Führen Sie das Haupt-Installationsskript im `Install`-Verzeichnis mit `sudo` aus. Dies startet den interaktiven Installer.

```bash
sudo python3 installer_main.py
```

Ein Menü mit verschiedenen Optionen wird angezeigt.

## Schritt 4: "Alles installieren" auswählen

Für eine Erstinstallation ist die empfohlene Option **"Alles installieren"** (normalerweise die Nr. 16).
1.  Wählen Sie die entsprechende Nummer aus dem Menü.
2.  Bestätigen Sie die Nachfrage mit `j` und drücken Sie Enter.
3.  Der Installer führt nun alle notwendigen Schritte automatisch aus. Folgen Sie den Anweisungen auf dem Bildschirm.

---

## Wichtige Befehle für die Wartung

Nach der Installation können Sie den Installer für Wartungsaufgaben verwenden. Führen Sie immer wieder `sudo python3 installer_main.py` im `Install`-Verzeichnis aus und wählen Sie die gewünschte Option:

- **E3DC-Control aktualisieren:**
  - Option `5` (E3DC-Control aktualisieren)
  - Hält die Anwendung auf dem neuesten Stand.

- **Berechtigungen überprüfen & korrigieren:**
  - Option `2` (Rechte prüfen & korrigieren)
  - Sehr nützlich, wenn es nach manuellen Änderungen zu Zugriffsproblemen kommt.

- **Backups verwalten:**
  - Option `14` (Backup verwalten)
  - Erstellen, Wiederherstellen oder Löschen von Backups.

- **Laufenden Prozess überprüfen:**
  - Um die `E3DC-Control` Anwendung zu beobachten, können Sie sich auf die `screen`-Sitzung verbinden:
  ```bash
  screen -r E3DC
  ```
  - Sie verlassen die Sitzung mit der Tastenkombination `Strg + A`, gefolgt von `D`.
