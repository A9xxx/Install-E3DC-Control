# Backup-System

Dieses Dokument beschreibt die Backup-Funktionalität des E3DC-Control Installers.

## Zweck

Das Backup-System dient primär dazu, **automatisch** einen Sicherungspunkt zu erstellen, bevor ein Update oder ein Rollback ausgeführt wird. Dies ermöglicht es, jederzeit zu einer früheren, funktionierenden Version zurückzukehren.

Es kann auch manuell genutzt werden, um einen gezielten Schnappschuss des aktuellen Zustands zu erstellen.

## Was wird gesichert?

Ein Backup ist eine `.zip`-Datei und enthält einen Schnappschuss aller relevanten Anwendungsdateien. Dazu gehören:

*   Der gesamte Inhalt des `E3DC-Control`-Verzeichnisses (`/home/pi/E3DC-Control`).
*   Alle zugehörigen Web-Dateien aus `/var/www/html`.

**Nicht** im Backup enthalten sind:
*   System-Bibliotheken oder Pakete.
*   Ihre Konfigurationsdateien (z.B. `e3dc.config.txt`). Diese werden bewusst nicht angetastet, damit Ihre Einstellungen bei einem Rollback erhalten bleiben.
*   Log-Dateien und Verlaufsdaten.

## Backup-Speicherort

Alle Backups werden im Verzeichnis `~/Install/backups` gespeichert. Der Dateiname enthält die Version und das Datum, z.B. `backup-v2.1.3-20260313143000.zip`.

## Manuelles Backup erstellen

Normalerweise müssen Sie kein manuelles Backup erstellen, da dies vor kritischen Operationen automatisch geschieht. Wenn Sie es dennoch tun möchten:

1.  **Navigieren Sie zum Installer-Verzeichnis:**
    ```bash
    cd ~/Install
    ```

2.  **Starten Sie den Installer:**
    ```bash
    python3 installer_main.py
    ```

3.  **Wählen Sie die Backup-Option:**
    Im Menü finden Sie einen Punkt namens "Backup erstellen". Wählen Sie diesen aus, um eine Sicherung des aktuellen Zustands zu erstellen.
