# Deinstallation von E3DC-Control

Dieses Dokument beschreibt, wie Sie die E3DC-Control Software und alle zugehörigen Komponenten sauber von Ihrem System entfernen.

## Wichtiger Hinweis

Der Deinstallationsprozess entfernt Software-Komponenten, Konfigurationen und Services. Er löscht **nicht** Ihre aufgezeichneten Verlaufsdaten (`history.csv` etc.) oder erstellte Backups. Diese müssen bei Bedarf manuell gelöscht werden.

## Ausführen der Deinstallation

Die Deinstallation wird über das Haupt-Installer-Skript gestartet.

1.  **Navigieren Sie zum Installer-Verzeichnis:**
    ```bash
    cd ~/Install
    ```

2.  **Starten Sie den Installer:**
    ```bash
    python3 installer_main.py
    ```

3.  **Wählen Sie die Deinstallations-Option:**
    Im Menü finden Sie einen Punkt namens "Alles deinstallieren" oder ähnlich. Wählen Sie diesen aus, um den Prozess zu starten.

## Was wird entfernt?

Das Deinstallations-Skript (`uninstall.py`) führt unter anderem folgende Aktionen durch:

*   **Stoppen und Deaktivieren der Services:**
    *   `e3dc.service`
    *   `e3dc-grabber.service`
    *   `energy_manager.service` (Luxtronik)
    *   `piguard.service` (Watchdog)
*   **Entfernen der Service-Dateien** aus `/etc/systemd/system/`.
*   **Entfernen der Cronjobs**, die vom Installer eingerichtet wurden.
*   **Entfernen der Sudo-Rechte** für den `www-data` Benutzer.
*   **Löschen des Haupt-Installationsverzeichnisses** (`/home/pi/E3DC-Control`).
*   **Löschen der Web-Oberflächen-Dateien** aus `/var/www/html`.
*   **Entfernen der `screen`-Konfiguration**.

Die Deinstallation versucht, das System so weit wie möglich in den Zustand vor der Installation zurückzuversetzen.
