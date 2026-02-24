# E3DC Mobile Historie Dokumentation

Die Datei `mobile_history.php` bietet eine spezialisierte Ansicht für den zeitlichen Verlauf der Hauskraftwerk-Daten, optimiert für mobile Endgeräte. Sie ermöglicht sowohl den Blick auf die aktuellen Live-Daten als auch den Zugriff auf das Archiv der letzten 30 Tage.

## Hauptfunktionen

### 1. Live-Verlauf (Aktuell)
*   **Flexible Zeiträume:** Über eine Button-Gruppe kann der Zeitraum für die aktuelle Ansicht gewählt werden: **6h, 12h, 24h oder 48h**.
*   **Echtzeit-Generierung:** Bei Auswahl eines Zeitraums oder Klick auf "Update" wird das Python-Skript `plot_live_history.py` im Hintergrund aufgerufen, um das Diagramm basierend auf der `live_history.txt` neu zu erstellen.
*   **Automatischer Refresh:** Beim ersten Laden der Seite wird geprüft, ob das vorhandene Diagramm älter als 5 Minuten ist. Falls ja, wird automatisch eine Aktualisierung (Standard 6h) angestoßen.

### 2. Archiv-Zugriff (Historische Daten)
*   **Backup-Auswahl:** Ein Dropdown-Menü erlaubt den Zugriff auf die täglichen Sicherungen der letzten 30 Tage (erstellt durch den Cronjob für `backup_history.php`).
*   **Feste Tagesansicht:** Sobald ein Archiv-Tag ausgewählt wird, schaltet die Ansicht fest auf ein 24-Stunden-Diagramm für diesen spezifischen Tag um. 
*   **Kontextsensitive UI:** Die Stunden-Filter (6h-48h) werden im Archiv-Modus automatisch ausgeblendet, da Backups immer einen vollen Tag repräsentieren.

### 3. Benutzerführung & Feedback
*   **Status-Anzeige:** Während das Diagramm im Hintergrund generiert wird, zeigt ein Text-Label den aktuellen Fortschritt an (z.B. "Erstelle 12h Diagramm…").
*   **Interaktives Iframe:** Das Diagramm wird in einem responsiven Iframe geladen. Ein Zeitstempel-Parameter (`?t=...`) verhindert dabei Browser-Caching-Probleme nach einem Update.
*   **Sperrmechanismus:** Während eines laufenden Updates werden die Buttons deaktiviert, um Mehrfachaufrufe und Serverlast zu vermeiden.

## Technische Details

### Verwendete Dateien & Pfade
*   **Datenquellen:**
    *   `live_history.txt`: Die aktuelle Datei (meist in der RAM-Disk) für den Live-Verlauf.
    *   `/var/www/html/tmp/history_backups/history_YYYY-MM-DD.txt`: Die täglichen Backups.
*   **Logik & Verarbeitung:**
    *   `run_live_history.php`: Der AJAX-Handler, der die Parameter (`hours`, `file`) validiert und das Python-Skript startet.
    *   `plot_live_history.py`: Das Python-Skript, welches die Plotly-Grafik als HTML-Datei generiert.
*   **Ausgabe:**
    *   `live_diagramm.html`: Die vom Python-Skript erzeugte HTML-Datei, die im Iframe angezeigt wird.

### Ablauf eines Diagramm-Updates
1.  **Trigger:** Benutzer wählt einen Zeitraum oder eine Archiv-Datei.
2.  **Request:** Ein `fetch`-Aufruf sendet die Auswahl an `run_live_history.php`.
3.  **Prozess:** PHP startet den Python-Prozess asynchron (`shell_exec` mit `&`) und gibt sofort `status: started` zurück.
4.  **Polling:** Das JavaScript in `mobile_history.php` fragt jede Sekunde den Status über `run_live_history.php?mode=status` ab.
5.  **Abschluss:** Sobald die Lock-Datei (`plot_live_history_running`) vom Python-Skript gelöscht wurde, erkennt das Frontend das Ende des Vorgangs und lädt das Iframe neu.

## Integration
Die Seite ist als Modul für die `mobile.php` konzipiert und wird dort über den Parameter `seite=history` eingebunden. Sie nutzt das Bootstrap-Framework für das Styling und Font-Awesome für die Icons.

---
*Hinweis: Damit die Archiv-Funktion Daten anzeigt, muss das Skript `backup_history.php` einmal täglich (idealerweise um Mitternacht) per Cronjob ausgeführt werden. Der Cronjob wird automatisch mit diagramm.php über das Menü Diagramm-Installation & Automatisierung installiert.*
```


->