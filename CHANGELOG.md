# Changelog

## [Aktuelles Update] - Dashboard 2.0 & Wallbox-Intelligenz

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
