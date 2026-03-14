# Dokumentation: Intelligentes Lademanagement

Dieses Dokument beschreibt die Funktionsweise der intelligenten Lade- und Entladestrategien "Intelligenter Morgen-Boost" und "Superintelligenz". Das Ziel beider Funktionen ist es, bei einer sehr guten Photovoltaik-Prognose den Hausspeicher am frühen Morgen gezielt zu entleeren, um Platz für den erwarteten Solarertrag zu schaffen und Abregelungsverluste zu minimieren.

---

## 1. Anwendungsfälle & Installation

Das System ist modular aufgebaut und kann in zwei Hauptkonfigurationen genutzt werden:

### a) Nur Ladeplanung (Wallbox)
*   **Zielgruppe:** Anwender, die **keine steuerbare Wärmepumpe** (Luxtronik) besitzen, aber die intelligente Entladung über ihre E3DC-Wallbox nutzen möchten.
*   **Installation:** Wählen Sie im Installer-Menü unter "Erweiterungen" den Punkt **"Intelligentes Lademanagement installieren/konfigurieren"**.
*   **Funktion:** In diesem Modus werden nur die Wallbox-bezogenen Steuerungsoptionen aktiviert.

### b) Ladeplanung mit Luxtronik (Wallbox & Wärmepumpe)
*   **Zielgruppe:** Anwender, die sowohl eine E3DC-Wallbox als auch eine Luxtronik-Wärmepumpe besitzen.
*   **Installation:** Wählen Sie im Installer-Menü unter "Erweiterungen" den Punkt **"Luxtronik Manager installieren/konfigurieren"**.
*   **Funktion:** In diesem Modus können Sie flexibel wählen, ob die Entladung über die Wallbox oder die Wärmepumpe (oder beides als Fallback) erfolgen soll.

Die Einstellungen für beide Modi finden Sie im Web-Interface unter **Luxtronik -> Einstellungen**. Der Zugang erfolgt entweder über die Wärmepumpen-Kachel oder (im Nur-Laden-Modus) über das "Zauberstab"-Icon in der Wallbox-Kachel.

---

## 2. Feature: Intelligenter Morgen-Boost

Dies ist die Standard-Funktion für die prognosebasierte Entladung.

### Das Ziel
Der Speicher soll bis zu einer definierten **Ziel-Zeit** (z.B. 08:00 Uhr) auf einen einstellbaren **Ziel-SoC** (z.B. 20%) entladen werden.

### Die Bedingung
Die Funktion wird nur aktiv, wenn die PV-Prognose für den Tag gut ist. Die Bedingungen sind konfigurierbar:
*   **Dauer 99% SoC:** Wie viele Stunden wird der Speicher laut Prognose auf 99% sein?
*   **PV-Ertrag in diesen Stunden:** Wie viel Prozent der Speicherkapazität wird in dieser Zeit als Überschuss erwartet?

### Die "Just-in-Time" Intelligenz
Der entscheidende Vorteil ist die Berechnung des **optimalen Startzeitpunkts**. Anstatt den Speicher sofort um Mitternacht zu leeren (und danach teuren Netzstrom für den Hausverbrauch beziehen zu müssen), rechnet das System rückwärts:

1.  **Energiebedarf:** `(Aktueller SoC - Ziel-SoC) * Speicherkapazität in kWh`
2.  **Annahme der Entladeleistung:**
    *   **Wallbox:** Konfigurierte Ladeleistung (z.B. 7.0 kW) + angenommene Haus-Grundlast (ca. 0.5 kW).
    *   **Wärmepumpe:** `wpmax` aus der `e3dc.config.txt` + Grundlast.
3.  **Benötigte Dauer:** `Energiebedarf / Entladeleistung`
4.  **Startzeit:** `Ziel-Zeit - Benötigte Dauer`

**Ergebnis:** Der Speicher versorgt das Haus so lange wie möglich über Nacht und beginnt erst zum optimalen Zeitpunkt mit der Entladung, um pünktlich zum Sonnenaufgang leer zu sein.

### Prioritäten-Auswahl
*   **Wallbox (mit Fallback):** Das System versucht, das E-Auto zu laden. Ist es nicht angeschlossen, wird automatisch die Wärmepumpe als "Notfall-Verbraucher" gestartet. Ideal, um die Energie in jedem Fall zu nutzen.
*   **Nur Wallbox (kein Fallback):** Das System versucht ausschließlich, das Auto zu laden. Ist es nicht angeschlossen, passiert nichts. Perfekt für Anlagen ohne steuerbare Wärmepumpe.
*   **Wärmepumpe:** Das System startet direkt den Wärmepumpen-Boost.

---

## 3. Feature: Superintelligenz (Experimentell)

Dies ist eine erweiterte, aggressivere Form der Entladung, die **ausschließlich für die Wallbox** gilt.

### Das Ziel
Bei **extrem guten PV-Prognosen** wird der Speicher bis auf den `Manuell Boost Min-SoC` (z.B. 25%) entleert.

### Die Bedingung
Die Hürden sind höher als beim normalen Morgen-Boost:
*   Mindestens 8 Stunden prognostizierter 99% SoC.
*   Erwarteter PV-Ertrag ist größer als `1.5 * (100 - Ziel-SoC)`. Der Faktor 1.5 dient als hoher Sicherheitspuffer.

### Die "Super"-Intelligenz (Dynamische Anpassung)
Hier liegt der entscheidende Unterschied und Vorteil:

1.  **Start:** Das System startet die Entladung basierend auf einer Annahme (z.B. 7 kW).
2.  **Messen:** Nach wenigen Minuten **misst** das Skript die **tatsächlich fließende Ladeleistung** aus den Live-Daten des E3DC-Systems. Es erkennt also automatisch, ob das Auto z.B. nur 1-phasig (ca. 3.6 kW) oder 3-phasig (z.B. 11 kW) lädt.
3.  **Neuberechnung:** Basierend auf der *realen* Leistung wird die verbleibende Ladedauer neu berechnet.
4.  **Pausieren:** Stellt das System fest, dass es mit der aktuellen Leistung viel zu früh fertig wäre, **pausiert es den Ladevorgang** (`wbmode` wird auf den ursprünglichen Wert zurückgesetzt).
5.  **Fortsetzen:** Kurz vor dem neu berechneten, optimalen Startzeitpunkt wird der Ladevorgang (`wbmode = 10`) automatisch wieder aufgenommen.

**Ergebnis:** Eine perfekt auf das Ladeverhalten des Autos abgestimmte, punktgenaue Entladung zur Zieldestzeit.

*(Auch hier greift die dynamische Grundlast-Kompensation, um den Hausverbrauch bis zum Sonnenaufgang einzuplanen).*

---

## 4. Feature: Wärme-Gestehungskosten (Thermischer Preis-Boost)

Dieses Feature optimiert das Heizen mit dynamischen Stromtarifen (aWATTar/Tibber), wenn eine Wärmepumpe angeschlossen ist.
*   **Das Problem:** Strom ist nachts oft am günstigsten. Aber nachts ist es auch am kältesten, was den Wirkungsgrad (COP) der Wärmepumpe drückt. 15 ct/kWh bei einem COP von 2,5 sind am Ende teurer als 20 ct/kWh am Mittag bei einem COP von 4,0!
*   **Die Lösung:** Der Energy Manager schätzt den zu erwartenden COP anhand der Quelleneintrittstemperatur (Sole oder Luft). Bei schlechtem COP wird dein eingestelltes Preislimit (`price_limit`) automatisch **nach unten korrigiert**. Bei sehr effizienten Bedingungen (hohe Quellentemperatur) wird das Limit leicht angehoben. Es wird also nach dem echten *Wärmepreis* geregelt, nicht nur nach dem Strompreis.

---

## 5. Feature: Auskühlschutz (Wetter-Hysterese bei PV-Pause)

*   **Die Funktion:** Die "PV-Pause" (Aushungern) schaltet die Wärmepumpe am Vormittag bewusst ab, wenn später am Tag ein großer PV-Überschuss erwartet wird. So wird Speicherkapazität im Estrich geschaffen.
*   **Der Auskühlschutz:** Im tiefen Winter (z.B. bei -5 °C) würde das Haus durch die Pause zu stark auskühlen. Das Wiederaufheizen würde den späteren PV-Gewinn zunichtemachen. Über den Parameter `pv_pause_min_at` (z.B. `0.0` °C) kann eine Außentemperatur definiert werden, unterhalb derer die PV-Pause komplett ausgesetzt wird.

---

## 6. Sicherheit

Beide Systeme sind gegen Stromausfälle und Neustarts abgesichert. Ein laufender Entlade-Vorgang wird in einer persistenten Datei (`/var/www/html/tmp/morning_boost_state.json`) gespeichert. Nach einem Neustart liest der `energy_manager` diese Datei, setzt den Vorgang fort und stellt am Ende sicher, dass die `e3dc.config.txt` (`wbminsoc`, `wbmode`) wieder auf die ursprünglichen Werte zurückgesetzt wird.