# Dokumentation: Intelligentes Lademanagement

Dieses Dokument beschreibt die intelligenten Lade- und Entladestrategien rund um V4 Storage Simulator, Storage Manager, Wallbox Manager und optionale Verbraucher. Das Ziel ist eine ruhige Speicher-Ladekurve, sinnvolles PV-Laden, geplantes Netzladen bei passenden Slots und kontrolliertes Freimachen von Speicherkapazität, wenn sonst Abregelung droht.

---

## 1. Anwendungsfälle & Installation

Das System ist modular aufgebaut und kann in zwei Hauptkonfigurationen genutzt werden:

### a) Nur Ladeplanung (Wallbox)
*   **Zielgruppe:** Anwender, die **keine steuerbare Wärmepumpe** (Luxtronik) besitzen, aber die intelligente Ladeplanung über ihre Wallbox (E3DC, openWB/openWB Pro oder go-e) nutzen möchten.
*   **Installation:** Im Installer den Hauptmenüpunkt **7) Expertenmenü** öffnen und unter „Erweiterungen & Smart Home“ den Punkt **42) Native Wallbox Manager** ausführen. Danach im Config-Editor die Wallbox-Regelung einschalten: Einfache Ansicht, Abschnitt „Wallbox“, Schalter **Wallbox-Regelung aktiv** und Auswahl **Wallbox 1** (Erweiterte Ansicht: Gruppe „Wallbox & Fahrzeug“, Schalter **Native Python-Regelung aktivieren**, Auswahl **Wallbox 1 Modell / API**).
*   **Funktion:** In diesem Modus werden nur die Wallbox-bezogenen Steuerungsoptionen aktiviert.

### b) Energy Manager (Wallbox & Wärmepumpe)
*   **Zielgruppe:** Anwender, die neben der Wallbox auch eine smarte Wärmepumpe (Luxtronik nativ ODER SG-Ready via Shelly) besitzen.
*   **Installation:** Im **Expertenmenü** unter „Erweiterungen & Smart Home“ den Punkt **41) Energy Manager** ausführen; die Wallbox-Regelung wie unter a) im Config-Editor einschalten.
*   **Funktion:** Der Energy Manager führt die Wärmepumpe als Aktor. Welche Verbraucher eine geplante Vorab-Entladung (Pre-Dump) nutzen dürfen, wird im Config-Editor unter „Pre-Dump: freigegebene Verbraucher“ festgelegt (Abschnitt 3).

Die Einstellungen für beide Modi finden Sie im Web-Interface im **Config Editor**.

---

## 2. Legacy: Intelligenter Morgen-Boost

> **Hinweis:** Der Morgen-Boost ist abgelöst. Die prognosebasierte Vorab-Entladung
> übernimmt Pre-Dump im Storage Manager (Abschnitt 3 und `Pre_Dump.md`). Der
> Schlüssel `morning_boost_enable` wird vom Energy Manager nur noch gelesen, um
> einen Hinweis ins Log zu schreiben; der Config-Editor bietet dafür kein Feld
> mehr an. Die folgende Beschreibung dokumentiert das frühere Verhalten.

Dies war die frühere Funktion für die prognosebasierte Entladung.

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
*   **SG-Ready (Boost):** Das System schließt den Kontakt bei einer zentralen Freigabe. Die Heizgrenze sperrt diese nicht: Für den automatischen Start zählt eine gültige, frische Warmwasser- oder Rücklauftemperatur unter ihrem Maximum abzüglich Totzone. Ohne gültige Temperaturdaten gibt es keine automatische Freigabe. Die Wärmepumpe entscheidet selbst über den Anlauf; Zieltemperaturen werden über den Kontakt nicht übertragen.
*   **EVU-Sperre (Pause):** Das System unterbricht den (normalerweise geschlossenen) Kontakt, um die Wärmepumpe in die Prognose-Pause ("Aushungern") zu schicken oder bei extrem hohen Strompreisen abzuschalten.

---

## 7. Feature: Intelligentes Zielladen (Auto SoC)

Dieses Feature hebt die klassische "Lade für X Stunden" Logik auf ein völlig neues Level.

*   **Die Funktion:** Das System nutzt den echten Akkustand deines Elektroautos, wenn er bestätigt geliefert wird, zum Beispiel über den integrierten Bluelink-Client, EVCC, Home Assistant via MQTT oder die Wallbox selbst.
*   **Ohne Smart-Vehicle (Manueller SoC):** Falls dein Fahrzeug über keine Schnittstelle verfügt, kannst du den aktuellen Ladezustand oben auf der Wallbox-Seite in der Karte **Fahrzeugzuordnung** eingeben. Der Wallbox Manager registriert ab diesem Moment die exakt eingeladene Energie (kWh) und interpoliert den SoC weiter.
*   **Fahrzeug-Vorlagen Speichern (Gast-Fahrzeuge):** Für Autos ohne Cloud-Anbindung können im Lade-Menü bequeme Profile (zum Beispiel "Kleinwagen 36 kWh", "Kombi 77 kWh") samt Kapazität und typischer Ladeleistung gespeichert werden. Das System berechnet Ladezeiten und Restreichweite dann auto-spezifisch, selbst für reine "Gast-Fahrzeuge"!
*   **Die Berechnung:** Hast du einen Ziel-SoC (z.B. 80%) im Config-Editor hinterlegt, berechnet der Energy Manager anhand der konfigurierten Batteriekapazität (z.B. 72 kWh) präzise die noch fehlende Energiemenge.
*   **Verlust-Kalkulation:** Das System schlägt automatisch **10 % Ladeverluste** auf den Bedarf auf.
*   **Die Ausführung & Anzeige:** Anhand der dynamischen Ladeleistung (z.B. 11 kW) wandelt das System die benötigten kWh in volle Lade-Stunden um, die auch im Dashboard direkt als **Restladezeit** (*"Voll: 3:30h"*) grafisch visualisiert werden. Ist für die Wallbox „Fertig bis“ mit Dauer aus dem Fahrzeug-SoC aktiv (`wb<n>_smart_wbhour_enable = 1`), nutzt der Wallbox-Manager diese Dauer, um die günstigsten Lade-Slots bis zur Zielzeit zu planen; außerhalb der Slots laden PV und Speicher.

Sobald das Auto den gewünschten Ziel-SoC erreicht hat, werden geplante
Ladefenster und Pre-Dump-Freigaben für dieses Fahrzeug ausgesetzt, um es nicht
unnötig mit Strom zu beladen.

Wichtig: Ein gespeichertes Fahrzeugprofil ist nur die Identität des Fahrzeugs,
kein bestätigter Ladestand. Ohne regelbestätigten Wallbox-/Cloud-/MQTT-SoC oder
manuell gesetzten SoC trifft das System keine SoC-basierte Abschaltentscheidung.
Ab 5.4.5a kann ein frisch beobachteter openWB-SoC mit Quelle und Alter rein
lesend im Dashboard erscheinen, wenn Stecksession oder Fahrzeugprofil eindeutig
passen. Diese Anzeige erzeugt weder Planung noch Hardwarefreigabe; andere
unbestätigte Werte bleiben `-- SoC`. Normales Laden nach PV, Preisfenster,
Budget oder kWh-Ziel bleibt weiterhin möglich.

### Multi-EV & Multi-Wallbox Support (Flotten-Management)
Das System unterstützt den parallelen Betrieb von mehreren Fahrzeugen und Ladestellen:

*   **Intelligente Fahrzeug-Erkennung:** Wenn du mehrere Autos hast (z. B. über einen Hyundai-Account mit zwei Fahrzeugen oder per MQTT via EVCC), baut das Dashboard automatisch für jedes Auto einen eigenen Karteireiter auf. Der Energy Manager prüft minütlich alle registrierten Fahrzeuge. Sobald ein Auto als "angesteckt" (Connected) gemeldet wird, aktiviert das System **automatisch** die für dieses Fahrzeug hinterlegten Parameter (Batteriekapazität, individuelles Ladeziel/Target-SoC) für die weitere Ladeplanung.
*   **Duale Wallbox-Visualisierung:** Neben der Haupt-Wallbox (E3DC) kann eine zweite, externe Ladestelle (z. B. go-e, openWB via MQTT oder Shelly) eingebunden werden. Diese wird im Dashboard als eigene Kachel "Wallbox 2" geführt und im Energiefluss-Diagramm separat dargestellt.
*   **Zentrales Logging:** Die Verbräuche beider Ladestellen werden getrennt erfasst, summiert und in die Langzeitstatistik exportiert. Das System erkennt dabei automatisch, welche Energie für welches Fahrzeug aufgewendet wurde, sofern die Zuordnung über die Ladepunkte (Loadpoints) erfolgt.
*   **Interaktive Steuerung:** Ladeziele können für jedes Fahrzeug individuell im Dashboard oder direkt in der App des Herstellers geändert werden – der Energy Manager synchronisiert diese Werte bidirektional.

### Native Wallbox Steuerung
Das alte Wallbox-Management wurde durch den nativen **Python Wallbox Manager** ersetzt. Die sichtbaren Nutzer-Modi sind:

* **Beobachten (intern Aus):** E3DC-Control beobachtet, die Wallbox bzw. der E3DC regelt selbst. Beim bewussten Wechsel und beim Fortsetzen einer Pause in diesem Modus wird einmalig die Grundeinstellung freigegeben. Pause und Not-Aus stoppen weiterhin sofort. Geplantes Netzladen bleibt gesperrt.
* **PV-Kurve ruhig:** Laden aus PV-Überschuss entlang der Speicher-Ladekurve mit Hysterese; über oder im Kurvenkorridor stützt der Hausspeicher nur im Rahmen des Wolken-Kontingents. Unter dem Korridor erhält die Wallbox bei ausgeschalteter zentraler PV-Aufteilung den reinen PV-Überschuss. Mit experimenteller zentraler PV-Aufteilung reserviert der Speicher zuerst seinen Kurvenbedarf; die Wallbox erhält den absoluten Rest.
* **Grundladung stabil:** Wie PV-Kurve ruhig, aber mit stabiler 1p/3p-Grundladung, solange `wbminSoc` laut Planung erreichbar bleibt.
* **PV + Akku bis Untergrenze:** Das Auto darf PV plus Hausspeicher oberhalb der Hausakku-Untergrenze nutzen; oberhalb der Haltezone bleibt die bisherige Speicherfreigabe. Mit experimenteller zentraler PV-Aufteilung führt unterhalb der Haltezone bei geschlossenem Tor der Kurvenbedarf den Akkuanteil. Unterhalb der Grenze lädt das Auto wie in `PV-Kurve ruhig` nur aus PV-Überschuss, der Speicher stützt nur Hausverbrauch und Wärmepumpe, Netz bleibt außen vor.
* **Sofort bis Preislimit:** Sofortiges Netzladen, solange der aktuelle Preis das Wallbox-Preislimit erfüllt.
* **Akku bis Abfahrt:** Lädt im Freigabefenster vor der Abfahrtszeit aus PV und Hausspeicher bis zur Hausakku-Untergrenze `wbminsoc`; Netzladen bleibt gesperrt. Gestoppt wird bei erreichter Abfahrtszeit und bei vollem Fahrzeug; an der Untergrenze kann mit ausreichendem PV-Anteil weitergeladen werden (`wb<n>_battery_departure_time`, `wb<n>_battery_departure_window_h`).

Geplantes Netzladen per Zeitfenster/Slot ist davon getrennt: Es darf in allen aktiven Modi laden und ignoriert das globale Wallbox-Preislimit, weil die günstigsten Stunden bereits bei der Slotplanung ausgewählt werden. Im Modus **Aus** bleibt geplantes Laden gesperrt.

Pause, Not-Aus und Ladepunktsperre beenden eine laufende Ladung sofort mit quittiertem Stopp, auch in „Beobachten“. Anti-Flattern und Mindestladezeit verzögern diesen Stopp nicht. Das Abbruchbit der Box zeigt, ob die Box eine Ladung freigibt oder gesperrt hat. Native E3DC-Wallboxen erhalten einen Stoppimpuls nur, wenn sie frisch „nicht abgebrochen“ melden, auch ohne Leistungsbezug. „Abgebrochen“ quittiert den Stopp ohne weitere Flanke. Fehlt die Abbruchmeldung der Box über mehrere Proben, ist genau ein Stopp mit frischem Ladebeleg zulässig; die Anzeige meldet „Zustand der Box nicht lesbar“. Andere Typen nehmen auch ein stehendes Angebot zurück. Bleibt nach einer nativen Stoppflanke der Bitwechsel 30 Sekunden aus, entsteht ein Fehler. In diesem Fehlerzustand erhalten Pause, Ladepunktsperre, Not-Aus, Notstromreserve und Hausanschluss gemeinsam genau eine zusätzliche Stoppflanke: frühestens 60 Sekunden nach dem letzten Stoppversuch, bei frischer Probe mit „nicht abgebrochen“ oder unbekanntem Bit und nur mit frischem Ladebeleg. Die Fehleranzeige erscheint ab Fehlerbeginn. Die zusätzliche Einmalflanke ist höchstens einmal je Stecksession und Dienstlauf zulässig; ist sie verbraucht oder für den Stoppgrund nicht berechtigt, bittet die Oberfläche, den Ladepunkt zu prüfen. Nach einem Neustart des Dienstes steht sie einmal neu zur Verfügung. Eine Stecksession endet erst mit bestätigtem Abstecken: mindestens drei aufeinanderfolgende frische Proben ohne gestecktes Fahrzeug über mindestens zehn Sekunden. Insgesamt sind höchstens drei Versuche je Ereignis zulässig; eine bereits verbrauchte Einmalflanke wird durch einen späteren Nutzer-Stopp nicht erneuert. Ein beobachteter externer Neustart wird bei Nutzer-Aus, Schutzgründen, Ladefensterende, Ziel erreicht, Abfahrt oder Budget-Zeitlimit erneut gestoppt, frühestens 30 Sekunden nach der letzten Flanke. Bei Unterdeckung übernimmt der Regelkern die externe Ladung und beendet sie nach seinen normalen Sitzungsregeln. Sperre, Pause und Not-Aus bleiben nach Dienst- und Host-Neustart sowie Containerstart bestehen. Die Ladepunktsperre gilt als Nutzer-Aus. Sperre und Not-Aus lassen sich nur über den Schalter mit dem Power-Symbol des jeweiligen Ladepunkts in der erweiterten Ansicht auf der Seite „Wallbox“ aufheben. Moduswahl, Abstecken und Speichern in der einfachen Ansicht heben die Sperre nicht auf. Fortsetzen, ein bewusster Moduswechsel, Sofortstart, Abstecken oder externes Fortsetzen an einer openWB Primary heben nur die Pause auf. Bleibt „Beobachten“ eingestellt, wird beim Fortsetzen oder Freigeben einmalig die Grundeinstellung freigegeben und anschließend wieder beobachtet. Bei nativen Boxen hebt Fortsetzen in „Beobachten“ nur die eigene Pause oder Ladepunktsperre derselben Stecksession genau einmal auf, wenn die Box frisch „abgebrochen“ meldet. Diese Übergabe benötigt kein Regelbudget. Eine App-Sperre wird nicht aufgehoben. Nach Not-Aus erfolgt in „Beobachten“ keine automatische Freigabe; die Box bleibt bis zur App-Freigabe gesperrt. Endet eine Pause in „Beobachten“ durch bestätigtes Abstecken, bleibt eine native Box gesperrt, bis sie in der E3DC-App freigegeben oder ein Regelmodus gewählt wird. Die bewusste Freigabe über den Schalter der Ladepunktsperre gibt die Box genau einmal frei, auch ohne gestecktes Fahrzeug, solange seit der Sperre weder eine Pause noch ein neues Anstecken dazwischenlag.

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

Netzbezug beim Laden begrenzen der Defizitregler des Wallbox Managers und als
letzte Stufe der Grid-Wächter (anhaltend mehr als 500 W für 45 s: ladende
Wallboxen über 6 A auf 6 A). Kurze Lastspitzen und den Bezug direkt nach einer
Anhebung überlässt die Wallbox für die Einschwingfrist dem Hausspeicher, soweit
er sie im Rahmen seiner wirksamen Entladegrenze ausgleichen kann; in der
PV-only-Klasse, an der Notstromreserve und ohne gültigen
Leistungseinstellungsbereich der Livewerte (Reichweite unbekannt) senkt sie
sofort ab.
Einzelheiten stehen in `Native_Wallbox.md`, Abschnitt „Netzbezug beim Laden:
Einschwingfrist, Anhebung und Grid-Wächter“. Eine einzelne ungültige
Liveprobe stoppt keine laufende Ladung: Der zuletzt ausgeführte Strom bleibt
höchstens 10 s nach der letzten gültigen Probe stehen (je Wallbox höchstens
10 s in 60 s), sofern diese Probe keinen Netzbezug zeigte; harte Grenzen wie
Nutzer-`Aus`, Notstromreserve und Hausanschluss wirken sofort (Abschnitt
„Ungültige Liveprobe: kurzer Halt“).

---

## 10. Lokale Verbrauchsprognose
Das Lademanagement kann lokale historische Betriebsdaten für die
Verbrauchsprognose nutzen. Bedienung, Datenschutz, Fallback und Rücksetzen sind
in `Verbrauchsprognose_Dokumentation.md` beschrieben.

---

## 11. System-Autonomie (Survival Mode)
**Inselnetz-Schutz:** Fällt das öffentliche Stromnetz aus, schaltet der Energy Manager sofort in den "Survival Mode". Alle Großverbraucher (wie Wärmepumpen-Boost oder das Laden des Autos) werden hart abgeworfen. Das System regelt extrem defensiv, um die Energie im Hausakku maximal lange für kritische Infrastruktur (Kühlschrank, Router) zu erhalten.


## Startknopf und Wiederanlauf nach Kaskaden-Stop

Der Startknopf erhält den gewählten Lademodus und betrifft die ausgewählte
Wallbox. Bei „Aus / autonom“ entsteht kein Startbefehl. Ein angenommener
Start löst eigene Ladeende-, Untergrenzen- und Kaskadenanker derselben
Stecksession. Nach Kontingent-Ende oder gescheitertem Phasenabstieg gilt
derselbe Vertrag. Fremde oder geschützte Stopps bleiben erhalten.

Nach einem regulären Stopp benötigt ein PV-Start 60 Sekunden ausreichenden
Anteil. Bei freigegebener Netzquelle entfällt diese Startqualifizierung.
Eine zusätzliche fünfminütige Wiederanlaufsperre besteht nicht.
Wh-Konten, Untergrenzen, Messwertgültigkeit und Hardware-Schutzfristen
gelten weiter. Die Startflanke bleibt mindestens 60 Sekunden und mindestens
bis zum wirksamen Start-/Stop-Abstand des Geräteprofils zuzüglich eines
Regelzyklus verfügbar; sie verkürzt die Schutzfrist nicht.

Die Oberfläche unterscheidet gespeicherte, angenommene und abgelehnte
Startanforderungen. Eine Ablehnung nennt ihren Grund und verbraucht die
Anforderung nicht. Eine noch ausstehende Prüfung wird als solche gemeldet.
Die Anforderung wird höchstens 60 Sekunden aufbewahrt. Das Dashboard
fragt während dieser Zeit begrenzt nach und zeigt das Ergebnis. Eine Annahme
bestätigt die Entsperrung, nicht bereits die physische Fahrzeugladung.
Das globale Abbruchflag der automatischen SoC-Planung bleibt beim Einzelstart
erhalten. Sofortladen wird für die jeweilige Wallbox über den Modus
„Sofort bis Preislimit“ gewählt; die andere Wallbox bleibt unverändert.
Die Konfigurationsvariablen `wbhour`, `wb_sofort`, `wbvon` und `wbbis` bleiben
für die Ladeplanung verfügbar. Veraltete Direktsteuerungsformulare ändern
keine Einstellungen.

## Experimentelle zentrale PV-Aufteilung

Das Budget ist der Rahmen für die Wallboxen; Strom, Phasen und Stopp
entscheidet der Wallbox Manager. Schutzstopps, Unterdeckungsregel, Datenfrist
und Wiederanlauf wirken auch bei zentraler PV-Aufteilung wie ohne sie.
