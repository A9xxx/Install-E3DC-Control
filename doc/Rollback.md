# Rollback / Zurücksetzen einer Version

Dieses Dokument erklärt, wie Sie Ihre E3DC-Control Installation auf eine frühere Version zurücksetzen können.

## Anwendungsfall

Ein Rollback kann nützlich sein, wenn nach einem Update unerwartete Probleme auftreten und Sie schnell zu einem vorherigen, stabilen Zustand zurückkehren möchten.

Der Rollback-Mechanismus verwendet das Backup, das automatisch vor jedem Update erstellt wird.

## Rollback ausführen

1.  **Navigieren Sie zum Installer-Verzeichnis:**
    ```bash
    cd ~/Install
    ```

2.  **Starten Sie den Installer:**
    ```bash
    python3 installer_main.py
    ```

3.  **Wählen Sie die Rollback-Option:**
    Im Menü finden Sie einen Punkt namens "Rollback / Version zurücksetzen". Wählen Sie diesen aus. Das Skript zeigt Ihnen eine Liste der verfügbaren Backups (Versionen) an, zu denen Sie zurückkehren können.

## Was passiert beim Rollback?

Das Rollback-Skript (`rollback.py`) führt folgende Schritte aus:

1.  **Auswahl des Backups:** Sie wählen die gewünschte Zielversion aus der Liste der verfügbaren Backups aus.
2.  **Wiederherstellung der Dateien:** Der Inhalt des ausgewählten Backup-Archivs (`.zip`-Datei) wird im Installationsverzeichnis (`/home/pi/E3DC-Control`) und im Web-Verzeichnis (`/var/www/html`) wiederhergestellt. Bestehende Dateien werden dabei überschrieben.
3.  **Build-Prozess:** Die `E3DC-Control` C++ Anwendung wird neu kompiliert, um sicherzustellen, dass sie zur wiederhergestellten Code-Version passt.
4.  **Berechtigungsprüfung:** Wie beim Update wird auch hier am Ende der **vollständige** `run_permissions_wizard` ausgeführt, um die korrekten Berechtigungen für alle Dateien und Systemkomponenten wiederherzustellen.
5.  **Neustart des Service:** Der `e3dc` Service wird neu gestartet, um die wiederhergestellte Version zu laden.

**Wichtiger Hinweis:** Ein Rollback stellt nur die Anwendungsdateien wieder her. Eventuelle Änderungen an der Konfiguration (`e3dc.config.txt`), die nach dem Backup gemacht wurden, bleiben erhalten.
