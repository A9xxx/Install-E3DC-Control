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
*   **Wallbox & Wärmepumpe:** Werden automatisch eingeblendet, sobald sie konfiguriert sind oder frische Messwerte liefern. Deaktivierte Wallboxen verschwinden aus dem Energiefluss; reine evcc/openWB-MQTT-Leistung gilt als aktive Wallboxmessung.

### 2. Strompreis-Integration (aWATTar)
*   **Aktueller Preis:** Anzeige in ct/kWh inklusive Tendenz-Icon (steigend, fallend, stabil).
*   **Frühwarnsystem:** Das Tendenz-Icon blinkt, wenn in den nächsten 2 Stunden ein Preissprung von mehr als 5 Cent bevorsteht.
*   **Preis-Chart:** Ein im Hintergrund der Karte liegendes Area-Chart zeigt den Preisverlauf des Tages.
    *   Eine vertikale, gestrichelte Linie markiert die aktuelle Uhrzeit (GMT-korrigiert).
    *   Hervorhebung: Die Karte leuchtet grün, wenn der Preis unter 10 ct/kWh fällt.
*   **Min/Max Werte:** Anzeige der günstigsten und teuersten Zeitpunkte für heute bzw. morgen.

### 3. Interaktives Diagramm
*   Die Ansicht **Historie** unter `mobile.php?seite=history` zeigt lokale
    Leistungs-, PV-, Batterie-, Netz-, Preis- und optionale Verbraucherdaten.
*   Zeiträume und Archivtag werden direkt im Browser ausgewählt; der
    Update-Knopf lädt die gewählte lokale Datenansicht neu.

### 3b. Tagesstatistik & CO₂-Bilanz
*   **Statistik-Overlay:** Tippe auf die Autarkie-/Eigenverbrauch-Leiste, um die detaillierte Tagesstatistik zu öffnen. Die Daten werden beim Öffnen automatisch vom Backend geladen.
*   **CO₂-Baum:** Zwischen Autarkie und Eigenverbrauch wächst ein animierter Baum, der den Autarkiegrad visualisiert (🌱→🌿→🪴→🌳→🌲🌳→🌲🌳🌲). Darunter werden die eingesparten kg CO₂ angezeigt.
*   **Energiebilanz-Badges:** Farbcodierte Badges zeigen die Tageswerte für PV-Ertrag (☀), Einspeisung (📤), Netzbezug (⚡), Batterie laden (🔋↓) und Batterie entladen (🔋↑) kompakt an.
*   **Kosten & Ersparnis:** Aufschlüsselung der Stromkosten und Ersparnisse pro Verbraucher (Haus, Wallbox, Wärmepumpe), inklusive dynamischer Strompreise.

### 4. UI-Features & Performance
*   **Globales Auge-Icon (Clean UI):** In der Kopfleiste gibt es ein Auge-Icon ("Details umschalten"). Damit lassen sich detaillierte technische Parameter (wie String-Spannungen der PV, Phasen/Modus der Wallbox, Verdichter-Temperaturen der Wärmepumpe) global ein- oder ausblenden. Die Wahl wird dauerhaft im Browser gespeichert (`localStorage`).
*   **Intelligentes Ausblenden:** Elemente mit nicht vorhandenen Werten (z.B. Phasen bei Fremd-Wallboxen via Shelly/MQTT) werden vollautomatisch ausgeblendet, anstatt `0` anzuzeigen. Dynamische Warnfarben (Orange/Rot) weisen zudem auf kritische Batterie-SoCs nahe der Reserve hin.
*   **Performance:** Das Dashboard nutzt minifiziertes JavaScript (`solar.min.js`) und asynchrones Skript-Laden (`defer`). Dies eliminiert "Render-Blocking" und sorgt für unmittelbaren Seitenaufbau, auch im Mobilfunknetz.
*   **Sicherheit (PIN-Schutz):** E3DC-Control zeigt im Heimnetz das Live-Dashboard mit Diagrammen, Prognose und Tagesstatistik einschließlich Archivtagen auch mit gesetzter Web-PIN ohne Anmeldung an. Geräte im Heimnetz sehen dabei dieselben vollständigen Dashboard-Daten wie ohne PIN. Die Ausnahme setzt einen lokalen Namen oder eine private Adresse und einen Absender im Heimnetz voraus. E3DC-Control verlangt für die Kostenhistorie, Konfiguration, Wallbox, Wärmepumpe einschließlich Vorschau, Klima, Matter, Systemmetriken und alle Schaltfunktionen weiterhin die Anmeldung. Außerhalb des Heimnetzes benötigen auch die Dashboard-Daten die Anmeldung. E3DC-Control verlangt auch für einen Proxy oder Tunnel auf demselben Gerät die Anmeldung, wenn dieser die Anlage über `127.0.0.1`/`localhost` anspricht und der Zugriff mit einer solchen Absenderadresse ankommt. Ein Ziel über die Heimnetz-Adresse fällt bei lokalem Namen und Heimnetz-Absender unter die Ausnahme. Für eine Anzeige direkt am Gerät das Dashboard über dessen Heimnetz-Adresse öffnen; über `localhost` verlangt E3DC-Control die Anmeldung.
*   **Zugriff aus dem Internet:** E3DC-Control schützt bei gesetzter Web-PIN Einstellungen und Schaltfunktionen auch bei Zugriffen aus dem Internet. Für eine Internetfreigabe eine Web-PIN setzen. Über den dokumentierten Cloudflare-Tunnel schützt die PIN auch das Dashboard, weil der Tunnel den öffentlichen Namen durchreicht. Ersetzt ein Reverse-Proxy oder eine Docker-Portweiterleitung den Absender durch eine interne Adresse und den aufgerufenen Namen durch einen lokalen Namen, kann E3DC-Control den Zugriff von außen nicht erkennen; die Dashboard-Daten bleiben dann trotz PIN ohne Anmeldung sichtbar. In diesem Fall den öffentlichen Namen durchreichen lassen oder ein VPN nutzen.
*   **Domainzugriff ohne Web-PIN:** Ohne Web-PIN ist der Zugriff über Domainnamen (eigene Domain, DynDNS, MyFRITZ, Reverse-Proxy) und öffentliche IP-Adressen (Portfreigabe) gesperrt; die Oberfläche zeigt dann nur „Web-PIN erforderlich“ mit dem Weg zum Freischalten. Die Sperre greift erst 14 Tage nach dem ersten solchen Zugriff; bis dahin erscheint oben ein Hinweis mit dem Datum. Private IP-Adressen, lokale Namen (etwa „.local“, „.fritz.box“, „.lan“) und Tailscale (Adressen und Namen unter „.ts.net“) bleiben frei. Kommt eine Anfrage von außerhalb des Heimnetzes, etwa über eine Portfreigabe, gilt die Sperre auch bei einem lokalen Namen. Die Sperre ersetzt keine Web-PIN: Ein Reverse-Proxy oder eine Docker-Portweiterleitung, die den Absender durch eine interne Adresse ersetzt, ist für E3DC-Control nicht als Zugriff von außen erkennbar; bei einem lokalen aufgerufenen Namen können die Dashboard-Daten dann auch mit Web-PIN ohne Anmeldung sichtbar sein. Für den Zugriff aus dem Internet eine Web-PIN setzen; bei einer Weiterleitung mit internem Absender und lokalem Namen den öffentlichen Namen durchreichen lassen oder ein VPN nutzen. Zum Freischalten die Oberfläche im Heimnetz über die IP-Adresse der Anlage oder einen lokalen Namen öffnen und im Config-Editor unter „Sicherheit, Webansicht & Updates“ im Bereich „Sicherheit“ entweder die Web-PIN setzen oder „Domainzugriff ohne Web-PIN“ bewusst auf „Ja, bewusst ohne Web-PIN“ stellen; dann bleibt der Zugriff wie bisher, und jede im Browser geöffnete Webseite, die einen Domainnamen auf die Anlage umleitet, kann die Oberfläche bedienen. Mit Web-PIN fragt die Oberfläche nach einer Pause ohne Aufruf (meist 24 Minuten) oder nach dem Schließen von Browser bzw. App erneut nach der PIN.

## Technische Details

### Verwendete Dateien
*   `e3dc_v4.json`: Auslesen von kWp, Speicherkapazität, Standort (Lat/Lon), maximaler Ladeleistung und UI-Einstellungen.
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
