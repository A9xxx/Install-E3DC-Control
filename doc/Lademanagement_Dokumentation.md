# Dokumentation: Intelligentes Lademanagement

Dieses Dokument beschreibt die intelligenten Lade- und Entladestrategien rund um V4 Storage Simulator, Storage Manager, Wallbox Manager und optionale Verbraucher. Das Ziel ist eine ruhige Speicher-Ladekurve, sinnvolles PV-Laden, geplantes Netzladen bei passenden Slots und kontrolliertes Freimachen von Speicherkapazitaet, wenn sonst Abregelung droht.

---

## 1. Anwendungsfälle & Installation

Das System ist modular aufgebaut und kann in zwei Hauptkonfigurationen genutzt werden:

### a) Nur Ladeplanung (Wallbox)
*   **Zielgruppe:** Anwender, die **keine steuerbare Wärmepumpe** (Luxtronik) besitzen, aber die intelligente Entladung über ihre E3DC-Wallbox nutzen möchten.
*   **Installation:** Wählen Sie im Installer-Menü unter "Erweiterungen" den Punkt **"Intelligentes Lademanagement installieren/konfigurieren"**.
*   **Funktion:** In diesem Modus werden nur die Wallbox-bezogenen Steuerungsoptionen aktiviert.

### b) Energy Manager (Wallbox & Wärmepumpe)
*   **Zielgruppe:** Anwender, die neben der Wallbox auch eine smarte Wärmepumpe (Luxtronik nativ ODER SG-Ready via Shelly) besitzen.
*   **Installation:** Wählen Sie im Installer-Menü unter "Erweiterungen" den Punkt **"Energy Manager installieren (Wärmepumpe & Lademanagement)"**.
*   **Funktion:** In diesem Modus können Sie flexibel wählen, ob die Entladung über die Wallbox oder die Wärmepumpe (oder beides als Fallback) erfolgen soll.

Die Einstellungen für beide Modi finden Sie im Web-Interface im **Config Editor**.

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
*   **Wärmepumpe:** konfiguriertes WP-Budget bzw. `wpmax` aus `e3dc_v4.json` + Grundlast.
3.  **Benötigte Dauer:** `Energiebedarf / Entladeleistung`
4.  **Startzeit:** `Ziel-Zeit - Benötigte Dauer`

**Ergebnis:** Der Speicher versorgt das Haus so lange wie möglich über Nacht und beginnt erst zum optimalen Zeitpunkt mit der Entladung, um pünktlich zum Sonnenaufgang leer zu sein.

### Prioritäten-Auswahl
*   **Wallbox (mit Fallback):** Das System versucht, das E-Auto zu laden. Ist es nicht angeschlossen, wird automatisch die Wärmepumpe als "Notfall-Verbraucher" gestartet. Ideal, um die Energie in jedem Fall zu nutzen.
*   **Nur Wallbox (kein Fallback):** Das System versucht ausschließlich, das Auto zu laden. Ist es nicht angeschlossen, passiert nichts. Perfekt für Anlagen ohne steuerbare Wärmepumpe.
*   **Wärmepumpe:** Das System startet direkt den Wärmepumpen-Boost.

---

## 3. Pre-Dump und eindeutige Zuständigkeiten

Für die geplante Entladung vor einem erwarteten PV-Überschuss gilt eine klare
Besitzlogik:

*   Der **Storage Simulator** plant Ladekurve, Pre-Dump und Zielanker.
*   Der **Storage Manager** entscheidet, ob Speicherleistung gehalten, geladen
    oder gezielt entladen werden soll.
*   Wallbox Manager und Energy Manager führen nur noch die freigegebenen
    Verbraucher aus und melden ihre reale Leistung zurück.

**Pre-Dump** entlädt den Speicher nur im geplanten Vorab-Fenster, nur bis zum
freigegebenen Ziel-SoC und nur mit nachvollziehbarem Besitzer. Die lokale
Prognose liefert dafür Eingabedaten, besitzt aber keinen Hardwareausgang.

---

## 4. Feature: Wärme-Gestehungskosten (Thermischer Preis-Boost)

Dieses Feature optimiert das Heizen mit dynamischen Stromtarifen (aWATTar/Tibber), wenn eine Wärmepumpe angeschlossen ist.
*   **Das Problem:** Strom ist nachts oft am günstigsten. Aber nachts ist es auch am kältesten, was den Wirkungsgrad (COP) der Wärmepumpe drückt. 15 ct/kWh bei einem COP von 2,5 sind am Ende teurer als 20 ct/kWh am Mittag bei einem COP von 4,0!
*   **Die Lösung:** Der Energy Manager schätzt den zu erwartenden COP anhand der Quelleneintrittstemperatur (Sole oder Luft). Bei schlechtem COP wird dein eingestelltes Preislimit (`price_limit`) automatisch **nach unten korrigiert**. Bei sehr effizienten Bedingungen (hohe Quellentemperatur) wird das Limit leicht angehoben. Es wird also nach dem echten *Wärmepreis* geregelt, nicht nur nach dem Strompreis.

---

## 5. Feature: Quell-Erholung (Pausenmodus für Wärmepumpen)

Der frühere Begriff **PV-Pause** beschreibt technisch nur den sichtbaren Effekt.
Der treffendere Fachbegriff ist **Quell-Erholung**: Die Wärmepumpe wird nicht
willkürlich abgeschaltet, sondern kontrolliert aus einem ungünstigen oder
unnötigen Lauf herausgenommen, damit Wärmequelle, Gebäude und Speicherplanung
wieder in einen besseren Arbeitspunkt kommen.

### Fachliche Grundlage

Wärmepumpen arbeiten effizienter, wenn die Temperaturdifferenz zwischen
Wärmequelle und Heiz-/Warmwasserseite klein bleibt. Bei Sole- und
Erdwärmeanlagen kann eine kurze Pause die Umgebung des Kollektors oder der
Sonde thermisch entlasten; bei Luft-Wärmepumpen vermeidet sie vor allem
ungünstige Laufzeiten, Takten und unnötige Speicherentladung vor erwarteter PV.
Stand der Technik ist dabei nicht "möglichst oft aus", sondern ein
kontrollierter Betrieb mit Mindestlaufzeit, Wiedereinschaltsperre,
Temperaturüberwachung und Komfortgrenzen.

### Regelprinzip in E3DC-Control

*   **Besitzer:** Die Quell-Erholung darf langfristig nur als Auftrag des
    Storage Managers laufen. Der Energy Manager bleibt Aktor für SG-Ready,
    Luxtronik, Shelly oder Heizstab und darf keine versteckte Eigenregelung
    gegen den Speicherpfad starten.
*   **Startbedingung:** Eine Pause ist nur sinnvoll, wenn Prognose,
    Speicherstand, Preisfenster oder Pre-Dump-Plan zeigen, dass ein späterer
    Lauf energetisch günstiger ist.
*   **Mindestlaufzeit:** Eine bereits laufende Wärmepumpe wird nicht nach wenigen
    Minuten abgewürgt. Verdichter und Wärmequelle brauchen saubere Laufzeiten.
*   **Wiedereinschaltsperre:** Nach einer Pause wird nicht sofort wieder
    gependelt. Hysterese und Sperrzeit verhindern Takten.
*   **Komfortwächter:** Warmwasser, Rücklauf, Außentemperatur und
    Temperaturabfall können die Pause abbrechen oder verhindern.
*   **Transparenz:** Dashboard und Diagnose müssen den Besitzer sichtbar machen:
    `Quell-Erholung`, `Pre-Dump`, `Preisfenster`, `manuell` oder `kein Auftrag`.

Damit ist Quell-Erholung eine fachlich begründete Optimierung, aber kein
zusätzlicher Regelkreis neben dem Storage Manager.

---

## 6. Feature: Universal SG-Ready & EVU-Sperre
Der Energy Manager ist nicht mehr auf Luxtronik-Anlagen beschränkt. Über die Konfigurations-Parameter `shelly_sg_ip` und `shelly_pause_ip` können Sie einfache WLAN-Shelly-Relais an die Steuerungskontakte jeder handelsüblichen Wärmepumpe anschließen.
*   **SG-Ready (Boost):** Das System schließt den Kontakt bei PV-Überschuss oder günstigen Preisen, woraufhin die WP die Temperatur anhebt.
*   **EVU-Sperre (Pause):** Das System unterbricht den (normalerweise geschlossenen) Kontakt, um die Wärmepumpe in die Prognose-Pause ("Aushungern") zu schicken oder bei extrem hohen Strompreisen abzuschalten.

---

## 7. Feature: Intelligentes Zielladen (Auto SoC)

Dieses Feature hebt die klassische "Lade für X Stunden" Logik auf ein völlig neues Level.

*   **Die Funktion:** Das System nutzt den echten Akkustand deines Elektroautos, wenn er bestätigt geliefert wird, zum Beispiel über den integrierten Bluelink-Client, EVCC, Home Assistant via MQTT oder die Wallbox selbst.
*   **Ohne Smart-Vehicle (Manueller SoC):** Falls dein Fahrzeug über keine Schnittstelle verfügt, kannst du den aktuellen Ladezustand oben auf der Wallbox-Seite in der Karte **Fahrzeugzuordnung** eingeben. Der Wallbox Manager registriert ab diesem Moment die exakt eingeladene Energie (kWh) und interpoliert den SoC weiter.
*   **Fahrzeug-Vorlagen Speichern (Gast-Fahrzeuge):** Für Autos ohne Cloud-Anbindung können im Lade-Menü bequeme Profile ("Honda e", "Skoda Elroq") samt Kapazität und typischer Ladeleistung gespeichert werden. Das System berechnet Ladezeiten und Restreichweite dann auto-spezifisch, selbst für reine "Gast-Fahrzeuge"!
*   **Die Berechnung:** Hast du einen Ziel-SoC (z.B. 80%) im Config-Editor hinterlegt, berechnet der Energy Manager anhand der konfigurierten Batteriekapazität (z.B. 72 kWh) präzise die noch fehlende Energiemenge.
*   **Verlust-Kalkulation:** Das System schlägt automatisch **10 % Ladeverluste** auf den Bedarf auf.
*   **Die Ausführung & Anzeige:** Anhand der dynamischen Ladeleistung (z.B. 11 kW) wandelt das System die benötigten kWh in volle Lade-Stunden um, die auch im Dashboard direkt als **Restladezeit** (*"Voll: 3:30h"*) grafisch visualisiert werden. Dieser Wert wird zudem automatisch für die Preis-Logiken genutzt, sodass das E3DC Kernprogramm sofort die günstigsten Nachtstunden für die Ladung bucht.

Sobald das Auto den gewünschten Ziel-SoC erreicht hat, werden geplante
Ladefenster und Pre-Dump-Freigaben für dieses Fahrzeug ausgesetzt, um es nicht
unnötig mit Strom zu beladen.

Wichtig: Ein gespeichertes Fahrzeugprofil ist nur die Identität des Fahrzeugs,
kein bestätigter Ladestand. Ohne Wallbox-/Cloud-/MQTT-SoC oder manuell gesetzten
SoC zeigt das Dashboard `-- SoC` und trifft keine SoC-basierte Abschaltentscheidung.
Normales Laden nach PV, Preisfenster, Budget oder kWh-Ziel bleibt weiterhin
möglich.

### Multi-EV & Multi-Wallbox Support (Flotten-Management)
Das System ist vollständig "Flotten-tauglich" und unterstützt ab Version 3.8.7.2 den parallelen Betrieb von mehreren Fahrzeugen und Ladestellen:

*   **Intelligente Fahrzeug-Erkennung:** Wenn du mehrere Autos hast (z. B. über einen Hyundai-Account mit zwei Fahrzeugen oder per MQTT via EVCC), baut das Dashboard automatisch für jedes Auto einen eigenen Karteireiter auf. Der Energy Manager prüft minütlich alle registrierten Fahrzeuge. Sobald ein Auto als "angesteckt" (Connected) gemeldet wird, aktiviert das System **automatisch** die für dieses Fahrzeug hinterlegten Parameter (Batteriekapazität, individuelles Ladeziel/Target-SoC) für die weitere Ladeplanung.
*   **Duale Wallbox-Visualisierung:** Neben der Haupt-Wallbox (E3DC) kann eine zweite, externe Ladestelle (z. B. go-e, openWB via MQTT oder Shelly) eingebunden werden. Diese wird im Dashboard als eigene Kachel "Wallbox 2" geführt und im Energiefluss-Diagramm separat dargestellt.
*   **Zentrales Logging:** Die Verbräuche beider Ladestellen werden getrennt erfasst, summiert und in die Langzeitstatistik exportiert. Das System erkennt dabei automatisch, welche Energie für welches Fahrzeug aufgewendet wurde, sofern die Zuordnung über die Ladepunkte (Loadpoints) erfolgt.
*   **Interaktive Steuerung:** Ladeziele können für jedes Fahrzeug individuell im Dashboard oder direkt in der App des Herstellers geändert werden – der Energy Manager synchronisiert diese Werte bidirektional.

### Native Wallbox Steuerung
Das alte Wallbox-Management wurde durch den nativen **Python Wallbox Manager** ersetzt. Die sichtbaren Nutzer-Modi sind:

* **Aus:** NGNA. E3DC-Control laedt nicht und sendet keine laufenden Wallbox-Befehle. Nur beim bewussten Wechsel auf `Aus` in der WebUI wird einmalig die Grundeinstellung freigegeben.
* **PV-Kurve ruhig:** Ruhiges Laden entlang der Speicher-Ladekurve.
* **Grundladung stabil:** 1p/3p-Grundladung, solange der Hausspeicher `wbminSoc` laut Planung noch erreichen kann.
* **PV + Akku bis Untergrenze:** Das Auto darf PV plus Hausspeicher oberhalb der Hausakku-Untergrenze nutzen; unterhalb der Grenze stützt der Speicher nur Hausverbrauch und Wärmepumpe, Netz bleibt außen vor.
* **Sofort bis Preislimit:** Sofortiges Netzladen nur, wenn der aktuelle Preis unter dem Wallbox-Preislimit liegt.

Geplantes Netzladen per Zeitfenster/Slot ist davon getrennt: Es darf in allen aktiven Modi laden und ignoriert das globale Wallbox-Preislimit, weil die guenstigsten Stunden bereits bei der Slotplanung ausgewaehlt werden. Im Modus **Aus** bleibt geplantes Laden gesperrt.

### Externe Wallboxen und MQTT
Externe Wallboxen können gesteuert oder nur gemessen werden:

* **openWB Pro:** Direkte Steuerung über `connect.php`, wenn E3DC-Control Master sein soll.
* **openWB Software 2.x:** Als `primary` regelt openWB selbst; E3DC-Control liest Messwerte oder schaltet bewusst den openWB-Lademodus per simpleAPI. Als `secondary` muss in openWB `Steuerungsmodus: secondary` und `Steuerung über Modbus als secondary: An` gesetzt sein.
* **go-e:** Steuerung über HTTP-API v2.
* **Direkte MQTT-Leistung:** Topics wie `evcc/loadpoints/1/chargePower` werden im Bereich **Wallbox-Leistung per MQTT** eingetragen, nicht im Fahrzeug-SoC-Feld.
* **Shelly-Messung:** `shelly_wb_ip` bzw. `shelly_wb2_ip` liefern reine Messwerte.

Alle externen Leistungswerte werden NaN-/Inf-sicher verarbeitet und vom reinen Hausverbrauch abgezogen.

---

## 9. Sicherheit

Pre-Dump, Quell-Erholung und Ladekurve werden allein vom Storage Manager
geführt. Nach einem Neustart wird eine aktive Entladung nur aus einer frischen,
sichtbaren Pre-Dump- oder Kurvenentscheidung wieder freigegeben.

---

## 10. Lokale Verbrauchsprognose
Das Lademanagement kann lokale historische Betriebsdaten für die
Verbrauchsprognose nutzen. Bedienung, Datenschutz, Fallback und Rücksetzen sind
in `Verbrauchsprognose_Dokumentation.md` beschrieben.

---

## 11. System-Autonomie (Survival Mode)
**Inselnetz-Schutz:** Fällt das öffentliche Stromnetz aus, schaltet der Energy Manager sofort in den "Survival Mode". Alle Großverbraucher (wie Wärmepumpen-Boost oder das Laden des Autos) werden hart abgeworfen. Das System regelt extrem defensiv, um die Energie im Hausakku maximal lange für kritische Infrastruktur (Kühlschrank, Router) zu erhalten.
