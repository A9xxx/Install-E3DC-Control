# E3DC Mobile Dashboard Dokumentation

Die Datei `mobile.php` dient als zentrale, mobil-optimierte Oberfläche zur Überwachung und Steuerung des E3DC-Hauskraftwerks sowie der Integration von dynamischen Strompreisen (aWATTar).

## Hauptfunktionen

### 1. Live-Dashboard
*   **Photovoltaik:** Anzeige der aktuellen Erzeugung. 
    *   Berechnet die Summe aller konfigurierten Strings (`forecast1` bis `forecast5`).
    *   Farbliches Feedback: Grün bei >90% Effizienz, Rot bei <50% der Prognose.
*   **Batterie:** Anzeige von SoC (%) und aktueller Leistung.
    *   Dynamische Puls-Animation: Die Geschwindigkeit und Intensität des Pulsierens passt sich der Lade-/Entladeleistung an.
    *   Farbcodierung: Grün (Laden), Rot (Entladen).
*   **Hausverbrauch & Netz:** Visualisierung des Eigenverbrauchs und der Netzeinspeisung/-bezugs inklusive Richtungs-Icons.
*   **Wallbox & Wärmepumpe:** Werden automatisch eingeblendet, sobald sie konfiguriert sind oder frische Messwerte liefern. Deaktivierte Wallboxen verschwinden aus dem Energiefluss; reine evcc/openWB-MQTT-Leistung gilt als aktive Wallboxmessung.

### 2. Strompreis-Integration (aWATTar)
*   **Aktueller Preis:** Anzeige in ct/kWh inklusive Tendenz-Icon (steigend, fallend, stabil).
*   **Frühwarnsystem:** Das Tendenz-Icon blinkt, wenn in den nächsten 2 Stunden ein Preissprung von mehr als 5 Cent bevorsteht.
*   **Preis-Chart:** Ein im Hintergrund der Karte liegendes Area-Chart zeigt den Preisverlauf des Tages.
    *   Eine vertikale, gestrichelte Linie markiert die aktuelle Uhrzeit (GMT-korrigiert).
    *   Hervorhebung: Die Karte leuchtet grün, wenn der Preis unter 10 ct/kWh fällt.
*   **Min/Max Werte:** Anzeige der günstigsten und teuersten Zeitpunkte für heute bzw. morgen.

### 3. Tagesstatistik & CO₂-Bilanz
*   **Statistik-Overlay:** Tippe auf die Autarkie-/Eigenverbrauch-Leiste, um die detaillierte Tagesstatistik zu öffnen. Die Daten werden beim Öffnen automatisch vom Backend geladen.
*   **CO₂-Baum:** Zwischen Autarkie und Eigenverbrauch wächst ein animierter Baum, der den Autarkiegrad visualisiert (🌱→🌿→🪴→🌳→🌲🌳→🌲🌳🌲). Darunter werden die eingesparten kg CO₂ angezeigt.
*   **Energiebilanz-Badges:** Farbcodierte Badges zeigen die Tageswerte für PV-Ertrag (☀), Einspeisung (📤), Netzbezug (⚡), Batterie laden (🔋↓) und Batterie entladen (🔋↑) kompakt an.
*   **Kosten & Ersparnis:** Aufschlüsselung der Stromkosten und Ersparnisse pro Verbraucher (Haus, Wallbox, Wärmepumpe), inklusive dynamischer Strompreise.

### 4. UI-Features & Performance
*   **Globales Auge-Icon (Clean UI):** In der Kopfleiste gibt es ein Auge-Icon ("Details umschalten"). Damit lassen sich detaillierte technische Parameter (wie String-Spannungen der PV, Phasen/Modus der Wallbox, Verdichter-Temperaturen der Wärmepumpe) global ein- oder ausblenden. Die Wahl wird dauerhaft im Browser gespeichert (`localStorage`).
*   **Intelligentes Ausblenden:** Elemente mit nicht vorhandenen Werten (z.B. Phasen bei Fremd-Wallboxen via Shelly/MQTT) werden vollautomatisch ausgeblendet, anstatt `0` anzuzeigen. Dynamische Warnfarben (Orange/Rot) weisen zudem auf kritische Batterie-SoCs nahe der Reserve hin.
*   **Performance:** Das Dashboard nutzt minifiziertes JavaScript (`solar.min.js`) und asynchrones Skript-Laden (`defer`). Dies eliminiert "Render-Blocking" und sorgt für unmittelbaren Seitenaufbau, auch im Mobilfunknetz.
*   **Sicherheit (PIN-Schutz):** Das Web-Interface kann optional durch eine PIN (`web_pin`) gesichert werden. Das Live-Dashboard bleibt für jeden einsehbar (Read-Only), während kritische Steuerungs-Ansichten (Konfiguration, Wallbox) gesperrt bleiben.

## Technische Details

### Verwendete Dateien
*   `e3dc_v4.json`: Auslesen von kWp, Speicherkapazität, Standort (Lat/Lon), maximaler Ladeleistung und UI-Einstellungen.
*   `live_history.txt`: Berechnung der 24h-Mittelwerte für die Skalierung der Balken.
*   `get_live_json.php`: Datenquelle für die Echtzeit-Werte.

### Berechnungen
*   **Sonnenstand:** Mathematische Berechnung von Elevation und Azimut basierend auf Zeit und Geoposition zur Ermittlung der theoretischen PV-Leistung.
*   **Prognose-Leistung:** Umrechnung der Prozentwerte aus der Prognose-Datei in reale Watt-Leistung unter Berücksichtigung der Speichergröße und des Zeitintervalls.

## Navigation
Die Fußzeile ermöglicht den schnellen Wechsel zwischen:
*   **Live:** Das Haupt-Dashboard.
*   **Wallbox / Config / Historie / Archiv:** Spezialansichten für die Steuerung und Analyse.
