# Changelog

## [2026.02.26] - Bugfixes & UI-Verbesserungen

### 🐞 Bugfixes
*   **PV-Prognose (`mobile.php`):** Die Berechnung des theoretischen PV-Sollwerts wurde korrigiert. Sie berücksichtigt nun die atmosphärische Dämpfung (Air Mass), was zu deutlich realistischeren Werten bei tiefem Sonnenstand (morgens/abends) führt.
*   **Diagramm-Kompatibilität (`plot_live_history.py`):** Ein Fehler (`Invalid property: 'titlefont'`) wurde behoben, der auf Systemen mit neueren Plotly-Versionen (v4/v5+) zum Absturz der Diagrammerstellung führte.
*   **Desktop-Dashboard (`index.php`):** Ein Fehler wurde behoben, bei dem das Aktualisieren des Leistungsverlaufs-Diagramms fehlschlug.
*   **Wallbox-Anzeige (`Wallbox.php`):** Ein Anzeigefehler wurde korrigiert, durch den immer alle drei Ladekosten-Szenarien gleichzeitig sichtbar waren.
*   **Variablen-Groß-/Kleinschreibung:** Diverse PHP-Skripte und Python-Dateien wurden robuster gemacht, um Fehler durch inkonsistente Groß-/Kleinschreibung in Konfigurationsvariablen (z.B. `awmwst` vs. `AWMwSt`) zu verhindern.
*   **Self-Update (`self_update.py`):** Ein Fehler wurde behoben, der dazu führte, dass bei einem Self-Update bestehende `.json`-Konfigurationsdateien überschrieben wurden. Die unnötige Abfrage von bereits konfigurierten Werten wurde ebenfalls korrigiert.
*   **Ramdisk (`ramdisk.py`):** Der `systemctl daemon-reload` Befehl wird nun nach Änderungen an der `fstab` ausgeführt, um Systemwarnungen zu vermeiden und sicherzustellen, dass die Ramdisk korrekt eingebunden wird.

### ✨ UI & UX-Verbesserungen
*   **Wallbox-Ladekosten (`Wallbox.php`):**
    *   Die Auswahl der Ladeleistung (z.B. 7.2, 11 kW) für die Kostenschätzung ist nun in der `e3dc.config.txt` (`wbcostpowers`) frei konfigurierbar.
    *   Die zuletzt gewählte Ladeleistung wird im Browser gespeichert und beim nächsten Besuch automatisch wiederhergestellt (Standard ist 11 kW).
    *   Die Tooltips auf der Lade-Timeline funktionieren nun auch auf Touch-Geräten (durch Antippen).
*   **Konfigurations-Editor (`config_editor.php`):**
    *   Es können nun direkt über die Weboberfläche neue Variablen zur `e3dc.config.txt` hinzugefügt werden.
    *   Die Lesbarkeit von Hinweistexten im Dark-Mode wurde verbessert.
*   **Mobile Historie (`history.php`):** Das Diagramm wird beim Wechsel auf den Reiter "Historie" nicht mehr automatisch aktualisiert, sondern erst nach einem Klick auf den "Update"-Button, um unnötige Ladevorgänge zu vermeiden.
*   **Design-Anpassungen:**
    *   Der "Steuern"-Button auf der Wallbox-Kachel im Desktop-Dashboard wurde durch ein dezentes Zahnrad-Icon ersetzt.

---

## [2026.02.25] - Dashboard 2.0 & Wallbox-Intelligenz

### 🖥️ Desktop Dashboard (`index.php`)
*   **Komplettes Redesign:** Neues Grid-Layout, das die volle Bildschirmbreite nutzt.
*   **Live-Daten Kacheln:** Echtzeit-Visualisierung von PV, Batterie, Hausverbrauch, Netz, Wallbox und Wärmepumpe.
*   **Intelligente Strompreis-Anzeige:**
    *   Dynamischer Balken mit Farbcodierung (Grün/Gelb/Rot) je nach Preisniveau (Günstig/Teuer).
    *   Trend-Indikatoren (Pfeile) zeigen steigende oder fallende Preise an.
    *   Anzeige der Tages-Minima und -Maxima.
*   **Multi-View Diagramm:** Nahtloser Wechsel zwischen SoC-Prognose, Live-Leistungsverlauf und Archiv-Ansicht direkt im Dashboard.
*   **Smart Polling:** Asynchrone Datenaktualisierung mit Lade-Animationen für ein flüssiges Nutzererlebnis.
*   **Routing:** Integrierte Navigation zu Unterseiten (Wallbox, Config, Archiv) ohne die Hauptseite zu verlassen.

### 🔌 Wallbox & Ladeplanung (`Wallbox.php`)
*   **Visuelle 24h-Timeline:** Neue rollierende Ansicht (zentriert auf "Jetzt"), die vergangene und geplante Ladefenster grafisch darstellt.
*   **Kostenvorschau:** Automatische Berechnung der voraussichtlichen Ladekosten für den geplanten Zeitraum (basierend auf aWATTar-Preisen).
    *   Szenarien für 7.2 kW, 11 kW und 22 kW Ladeleistung.
*   **Echtzeit-Status:** Neue Status-Card am Kopf der Seite zeigt sofort an, wenn aktiv geladen wird (inkl. Leistung).
*   **Robustheit:** Umstellung auf Parsing der maschinenlesbaren `e3dc.wallbox.out` für präzisere Datenverarbeitung.
*   **Auto-Refresh:** Die Seite aktualisiert sich automatisch, sobald ein neuer Ladeplan berechnet wurde.
*   **Quick-Actions:** Schnellzugriff für "Stop" (0h) und "Sofort Laden" (99h).

### 📱 Mobile Ansicht (`mobile.php`)
*   **Preis-Chart:** Ein Area-Chart im Hintergrund der Preiskachel visualisiert den Tagesverlauf.
*   **Warnsystem:** Blinkende Indikatoren warnen vor starken Preissprüngen in den nächsten Stunden.
*   **Konsistenz:** Übernahme der Farb-Logik (Grün/Rot) für Preisniveaus vom Desktop.

### 🔄 Update-System & Wartung
*   **Web-Update:** E3DC-Control kann nun direkt über das Web-Portal aktualisiert werden (Desktop & Mobile).
    *   Echtzeit-Fortschrittsanzeige im Modal-Fenster.
    *   Polling-Mechanismus verhindert Timeouts bei langsamen Verbindungen (Cloudflare-Fix).
    *   Visuelles Feedback (Grüner Haken / Rotes Kreuz) bei Erfolg/Fehler.
*   **Headless-Installer:** Der Installer unterstützt nun einen `--unattended` Modus für automatisierte Abläufe.
*   **BOM-Bereinigung:** Automatisches Entfernen von Windows-Steuerzeichen (Byte Order Mark) aus Skripten zur Vermeidung von Syntaxfehlern.

### 🛠️ Technik & Backend
*   **Self-Healing:** `run_now.php` erkennt und bereinigt nun automatisch verwaiste Lockfiles (> 5 Min), um Systemhänger zu vermeiden.
*   **Rechte-Management:** Der Installer (`check_permissions.sh`) prüft nun auch Schreibrechte für temporäre Web-Verzeichnisse (`tmp/`, `ramdisk/`).
*   **Performance:** Optimiertes Caching und Polling-Intervalle zur Entlastung des Raspberry Pi.

---

### ✨ Mehrwert der Änderungen
*   **Sofortige Klarheit (UX):** Keine Interpretation von Textlisten mehr nötig – ein Blick auf die Timeline oder das Dashboard genügt.
*   **Kosten-Transparenz:** Du siehst vorab, was dich das Laden kosten wird.
*   **Live-Feedback:** Direkte Rückmeldung über Aktionen ohne manuelles Neuladen der Seite.
*   **Professionelle Optik:** Einheiltiche Designsprache (Dark Mode, Bootstrap 5) über alle Ansichten hinweg.

---
*Generiert für E3DC-Control Release Candidate*
