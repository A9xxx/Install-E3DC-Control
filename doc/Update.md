# Update-Prozess

Dieses Dokument beschreibt, wie Sie Ihre E3DC-Control Installation auf den neuesten Stand bringen.

## Update-Varianten

Es gibt zwei Hauptmethoden, um ein Update durchzuführen:

1.  **Automatisches Update (Empfohlen):** Die Software kann sich selbstständig aktualisieren. Dies kann im Webportal unter "Einstellungen" -> "Updates" konfiguriert werden.
2.  **Manuelles Update:** Sie können das Update jederzeit manuell über das Installer-Menü anstoßen.

## Manuelles Update ausführen

1.  **Navigieren Sie zum Installer-Verzeichnis:**
    ```bash
    cd ~/Install
    ```

2.  **Starten Sie den Installer:**
    ```bash
    python3 installer_main.py
    ```

3.  **Wählen Sie die Update-Option:**
    Im Menü finden Sie einen Punkt namens "Update E3DC-Control". Wählen Sie diesen aus.

## Was passiert beim Update?

Das Update-Skript (`update.py`) führt folgende Schritte aus:

1.  **Installer-Selbstupdate:** Zuerst prüft der Installer, ob er selbst auf dem neuesten Stand ist. Falls nicht, aktualisiert er sich zuerst (`self_update.py`).
2.  **Git-Update:** Das Skript führt einen `git pull` im Installationsverzeichnis (`/home/pi/E3DC-Control`) aus, um die neuesten Code-Änderungen von GitHub herunterzuladen.
3.  **Build-Prozess:** Nach dem Update wird die `E3DC-Control` C++ Anwendung neu kompiliert, um sicherzustellen, dass sie mit dem neuen Code kompatibel ist.
4.  **Web-Dateien kopieren:** Geänderte oder neue Dateien für die Weboberfläche werden in das Verzeichnis `/var/www/html` kopiert.
5.  **Berechtigungsprüfung:** Am Ende des Update-Prozesses wird automatisch der **vollständige** `run_permissions_wizard` ausgeführt. Dieser stellt sicher, dass alle neuen Dateien die korrekten Berechtigungen haben und prüft auch Services, Cronjobs und Konfigurationen.
6.  **Neustart des Service:** Abschließend wird der `e3dc` Service neu gestartet, um die Änderungen zu übernehmen.

Dieser Prozess stellt sicher, dass nach einem Update alle Komponenten des Systems konsistent und lauffähig sind.
