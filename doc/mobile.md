# E3DC Mobile Dashboard Dokumentation

Die Datei `mobile.php` dient als zentrale, mobil-optimierte Oberfläche zur Überwachung und Steuerung des E3DC-Hauskraftwerks sowie der Integration von dynamischen Strompreisen (aWATTar).

## Hauptfunktionen

### 1. Live-Dashboard
*   **Photovoltaik:** Anzeige der aktuellen Erzeugung. 
    *   Berechnet die Summe aller konfigurierten Strings (`forecast1` bis `forecast5`).
    *   Vergleicht die Live-Leistung mit dem theoretischen Sonnenstand (Soll) und der E3DC-Prognose aus der `awattardebug.txt`.
    *   Farbliches Feedback: Grün bei >90% Effizienz, Rot bei <50% der Prognose.
*   **Batterie:** Anzeige von SoC (%) und aktueller Leistung.
    *   Dynamische Puls-Animation: Die Geschwindigkeit und Intensität des Pulsierens passt sich der Lade-/Entladeleistung an.
    *   Farbcodierung: Grün (Laden), Rot (Entladen).
*   **Hausverbrauch & Netz:** Visualisierung des Eigenverbrauchs und der Netzeinspeisung/-bezugs inklusive Richtungs-Icons.
*   **Wallbox & Wärmepumpe:** Werden automatisch eingeblendet, sobald aktive Lasten erkannt werden.

### 2. Strompreis-Integration (aWATTar)
*   **Aktueller Preis:** Anzeige in ct/kWh inklusive Tendenz-Icon (steigend, fallend, stabil).
*   **Frühwarnsystem:** Das Tendenz-Icon blinkt, wenn in den nächsten 2 Stunden ein Preissprung von mehr als 5 Cent bevorsteht.
*   **Preis-Chart:** Ein im Hintergrund der Karte liegendes Area-Chart zeigt den Preisverlauf des Tages.
    *   Eine vertikale, gestrichelte Linie markiert die aktuelle Uhrzeit (GMT-korrigiert).
    *   Hervorhebung: Die Karte leuchtet grün, wenn der Preis unter 10 ct/kWh fällt.
*   **Min/Max Werte:** Anzeige der günstigsten und teuersten Zeitpunkte für heute bzw. morgen.

### 3. Interaktives Diagramm
*   Über einen Button kann ein detailliertes SOC-Diagramm eingeblendet werden.
*   **Update-Logik:** Ein manueller Update-Button triggert das Python-Script `plot_soc_changes.py`.
*   **Intelligenter Refresh:** Das Diagramm aktualisiert sich beim Einblenden automatisch nur dann, wenn das letzte Update länger als 15 Minuten her ist, um Systemressourcen zu schonen.

## Technische Details

### Verwendete Dateien
*   `e3dc.config.txt`: Auslesen von kWp, Speicherkapazität, Standort (Lat/Lon) und maximaler Ladeleistung.
*   `awattardebug.0.txt` / `awattardebug.txt`: Datenbasis für Strompreise und PV-Prognose.
*   `live_history.txt`: Berechnung der 24h-Mittelwerte für die Skalierung der Balken.
*   `get_live_json.php`: Datenquelle für die Echtzeit-Werte.

### Berechnungen
*   **Sonnenstand:** Mathematische Berechnung von Elevation und Azimut basierend auf Zeit und Geoposition zur Ermittlung der theoretischen PV-Leistung.
*   **GMT-Korrektur:** Alle Zeitstempel aus den Debug-Dateien werden von UTC/GMT in die lokale Browserzeit umgerechnet.
*   **Prognose-Leistung:** Umrechnung der Prozentwerte aus der Prognose-Datei in reale Watt-Leistung unter Berücksichtigung der Speichergröße und des Zeitintervalls.

## Navigation
Die Fußzeile ermöglicht den schnellen Wechsel zwischen:
*   **Live:** Das Haupt-Dashboard.
*   **Wallbox / Config / Historie / Archiv:** Spezialansichten für die Steuerung und Analyse.