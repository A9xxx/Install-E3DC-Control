# Native Multi-Wallbox-Steuerung

Der V4-Wallbox-Manager ist ein eigenständiger Python-Dienst. Er regelt E3DC,
openWB/openWB Pro, go-e und reine Mess-Wallboxen über dieselbe Budget-,
Hysterese- und Schutzlogik, nutzt aber je Wallbox den passenden Treiber.
Die verbindlichen Grundsätze dahinter (harte Schranken, Ein-Entscheider je
Aktor, Anti-Flattern, Akkustützung nach Korridorlage) stehen in
`doc/V4_Konfiguration_und_Regelung.md`, Abschnitt „Regelungsphilosophie
(verbindlich)“.

## Was kann der Wallbox-Manager?

* **Multi-Wallbox:** WB1 und WB2 werden getrennt gelesen, angezeigt und
  budgetiert.
* **Klare Nutzer-Modi:** `Aus / autonom`, `PV-Kurve ruhig`, `Grundladung stabil`,
  `PV + Akku bis Untergrenze`, `Sofort bis Preislimit` und `Akku bis Abfahrt`.
* **Geplantes Netzladen:** Slotladen funktioniert in allen aktiven Modi und
  ignoriert das Wallbox-Preislimit, weil die Preislogik bereits in der
  Slot-Auswahl steckt. Im Modus `Aus` ist geplantes Laden gesperrt.
* **Fahrzeugzuordnung:** Pro Wallbox wird ein Fahrzeug automatisch gespeichert;
  der manuelle Start-SoC wird über **SoC setzen** geschrieben.
* **Bestätigter Fahrzeug-SoC:** Ziel-SoC, Restladezeit und `Auto voll` nutzen
  nur SoC-Werte aus Wallbox/openWB, Bluelink/MQTT oder bewusster manueller
  Eingabe. Ein frisch beobachteter openWB-Wert kann ab 5.4.5a mit Quelle und
  Alter rein lesend erscheinen, bestätigt aber für sich allein keinen
  Regel-SoC. Andere unbestätigte Profil- oder Altwerte erscheinen als `-- SoC`;
  normales PV-/Budgetladen bleibt davon unberührt.
* **Stecker-/Schloss-Symbol:** Meldet die Wallbox ein Verriegelungsbit (E3DC-Wallbox),
  zeigt der Wallbox-Knoten ohne Fahrzeug kein Symbol, bei gestecktem, nicht
  verriegeltem Fahrzeug ein offenes Schloss und bei verriegeltem Fahrzeug ein
  gelbes Schloss. Ohne Verriegelungsbit (etwa openWB Pro) erscheint bei
  gestecktem Fahrzeug ein gelbes Stecker-Symbol, ohne Aussage zur Verriegelung.
  Bei ungültigem Wallboxstatus erscheint kein Symbol.
* **Phasen- und Mindestleistung:** openWB Pro, normale openWB, go-e und E3DC
  werden unterschiedlich angesprochen, aber mit derselben Schutzlogik geregelt.
* **openWB-Autoerkennung:** E3DC-Control liest openWB Software 2.x read-only
  aus und passt den Treiber an die erkannte Rolle an, ohne die openWB selbst
  umzustellen.
* **NaN-sichere Messwerte:** Externe MQTT-Werte wie
  `evcc/loadpoints/1/chargePower` werden als reale Wallboxleistung angenommen,
  aber `NaN`, `Infinity` und leere Payloads werden ignoriert.

## Modusübersicht

| Modus | Zweck |
|---|---|
| `Aus` | NGNA: E3DC-Control beobachtet nur. Eine Standardfreigabe wird nur einmalig nach bewusstem Wechsel auf `Aus` in der WebUI gesendet. |
| `PV-Kurve ruhig` | Lädt aus PV-Überschuss entlang der Speicher-Ladekurve mit Hysterese. Der Hausspeicher ist keine Ladequelle für das Fahrzeug; über oder im Kurvenkorridor stützt er nur im Rahmen des Wolken-Kontingents. Unter dem Korridor erhält die Wallbox bei ausgeschalteter zentraler PV-Aufteilung den reinen PV-Überschuss; mit der experimentellen zentralen PV-Aufteilung („Kurve zuerst“) wird der Kurvenbedarf des Speichers zuerst abgezogen. Details stehen in `V4_Konfiguration_und_Regelung.md`, Abschnitt „Akkustützung der Wallbox in `PV-Kurve ruhig` nach Korridorlage“. |
| `Grundladung stabil` | Hält eine ruhige Grundladung, solange das Speicherziel laut Planung erreichbar bleibt. |
| `PV + Akku bis Untergrenze` | Nutzt PV und oberhalb der Hausakku-Untergrenze zusätzlich den Speicher. Reicht die PV unter den Speicher- und belegten Fahrzeug-Leistungsgrenzen nicht für Akku und geeignete Fahrzeuge, endet die heutige PV-Kurve bei wbminSoC, auch bei Prognose auf 100%. Bedarf 0 lässt das normale Ziel bestehen; unbekannter Bedarf zählt nach bestätigter Ladung weiter, nur am selben Kurventag und in derselben Steck-Episode; nach 60 Minuten unter 300 W bei ausreichendem PV- und Stromangebot endet dieser Beleg. Unterbrechungen bis zwei Minuten pausieren die Uhr; längere setzen sie zurück. Das gilt für alle Wallboxen (geregelt und beobachtet), in Aus nur mit „Nur beobachten, Speicher oberhalb Hausreserve freigegeben“. Zur normalen Kurve ab 110 %, zurück unter 100 %; jede Wahl hält etwa 15 Minuten. Bestätigtes Abstecken und Pause wirken ab dem nächsten Planlauf; relevante Änderungen lösen nach 60 Sekunden stabilem Zustand neu aus. Folgetage und Netzladeziele bleiben normal. Details stehen in `Ladekurve_Berechnung.md`, Abschnitt „Abendziel bei PV + Akku“. Die unten beschriebene begrenzte Phasenerkennung darf kurz Netzleistung überbrücken. Ist die Untergrenze erreicht und trägt das PV-Budget dieser Wallbox die Mindestleistung der aktuellen Phasenzahl nicht, wird eine laufende Ladung bei dieser Phasenzahl auf den Mindeststrom gesetzt (ohne PV sofort, bei noch anliegendem PV nach 20 s Bestätigung) und nach einem Durchlauf des Wh-Kontos gestoppt; eine dreiphasig ladende openWB Pro wechselt stattdessen auf 1p, wenn das PV-Budget das 1p-Minimum trägt. Laden mehrere Wallboxen, wird diese Wallbox ohne Netzbezug zuerst abgesenkt, sofern sie selbst über ihrer Zuteilung lädt; deckt ihre Zuteilung die tatsächliche Ladeleistung, lädt sie weiter. Netzbezug und ein laufender Phasenwechsel einer anderen Wallbox haben Vorrang. |
| `Sofort bis Preislimit` | Netzladen nur, wenn der aktuelle Preis unter dem Wallbox-Preislimit liegt. Ohne Preis- oder Netzfenster gilt an der Hausakku-Untergrenze dieselbe Absenkung wie in `PV + Akku bis Untergrenze`. |
| `Akku bis Abfahrt` | Lädt im Freigabefenster vor der Abfahrtszeit aus PV und Hausspeicher bis zur Hausakku-Untergrenze `wbminsoc`; Netzladen bleibt gesperrt. Gestoppt wird bei erreichter Abfahrtszeit und bei vollem Fahrzeug; an der Untergrenze kann mit ausreichendem PV-Anteil weitergeladen werden. Abfahrtszeit `wb<n>_battery_departure_time` (Standard `06:30`), Fenster `wb<n>_battery_departure_window_h` (Standard 3 h, 1–36 h). |

## Phasenerkennung an einer festen Wallbox

Bei einer durch Python stromgeregelten E3DC-Wallbox ohne nutzbare Phasenumschaltung
kann ein unbekanntes Fahrzeug mit 6 A gestartet werden, sobald mindestens
1.380 W echter, zentral freigegebener PV-Anteil verfügbar sind. Die 6 A gelten
je genutzter Phase: Das Fahrzeug beziehungsweise das Kabel bestimmt, ob daraus
ungefähr 1,38, 2,76 oder 4,14 kW werden. Es erfolgt kein Phasenwechselbefehl.
Die Grundbedeutung der Stromvorgabe ist in der
[Herstellerbeschreibung zu IEC 61851](https://infosys.beckhoff.com/content/1031/el6761/18724365579.html)
erläutert.

Reicht das PV-Angebot zunächst nicht für die mögliche dreiphasige Last, muss
der zentrale Leistungsentscheider die fehlende Leistung ausdrücklich freigeben.
Netzleistung bleibt dabei durch die Hausanschlussgrenzen begrenzt; eine gesperrte
oder erschöpfte Batterie ist keine Voraussetzung für diese Netzüberbrückung.
Die Batterie darf nur innerhalb ihrer eigenen Leistungs- und Reservegrenzen
beitragen. `Aus`, fehlende Freigaben, ungültige Messwerte und harte
Schutzgrenzen verhindern die Probe.

Für diese Erkennung gilt ein gemeinsames Konto für alle Wallboxen mit höchstens
40 Wh und genau einem Probeversuch zur selben Zeit. Eine kleinere konfigurierte
Wh-Stoppgrenze bleibt wirksam. Der mögliche Verbrauch wird vor dem Start privat
gespeichert; das Erkennungsfenster dauert höchstens 30 Sekunden. Der Wh-Wert
beschreibt die konservativ berechnete PV-Deckungslücke, keine getrennte Messung
der Batterieenergie. Fehlende oder widersprüchliche Messwerte beenden die
Überbrückung konservativ.

Während einer reservierten Probe startet kein weiterer ruhender Ladepunkt aus
dieser Zusage. Bereits laufende Ladungen bleiben in der gemeinsamen Zuteilung
berücksichtigt; eine ausdrücklich konfigurierte Wallboxpriorität bleibt wirksam.

Mehrere frische, vollständige Phasenmessungen bei stabilen 6 A bestätigen die
benutzten Phasen. Wurde das kurze Startfenster nicht erfolgreich ausgewertet,
kann die Erkennung auch bei einer später stabilen höheren Stromvorgabe erfolgen.
Die Leistung jeder belasteten Phase muss dabei zur Stromvorgabe passen; eine
Stromänderung startet die Einschwing- und Bestätigungszeit erneut. Die begrenzte
6-A-Startprobe und ihr Energiekonto bleiben unverändert.
Im laufenden Dienst bleiben bestätigte Phasen bei Ladepausen und 0 W
erhalten. Erst bestätigtes Abstecken beginnt die Erkennung erneut. Eine später
zusätzlich gemessene Phase erhöht die angesetzte Last sofort. Fehlen nach einem
Dienstneustart sichere Belege für die unveränderte Steckepisode, wird die
Phasenzahl erneut konservativ ermittelt.

Deckt die PV-Leistung die erkannte Last, geht die Ladung ohne erzwungenen Stopp
in die normale Regelung über. Andernfalls beendet der Wh- oder Zeitwächter die
Probe. Nach einem solchen Stopp gibt es in derselben Stecksession keine weitere
Überbrückungsprobe; eine normale Ladung mit ausreichendem PV-Angebot bleibt
unter den bestehenden Wiederanlaufzeiten möglich. Abstecken und Dienstneustart
löschen den verbrauchten Anteil nicht. Nur fortlaufend belegte tatsächliche
PV-Ladung baut die Defizitschuld langsam wieder ab.

Herstellereigene efy-Automatik, go-e und die Rollensteuerung einer normalen
openWB behalten ihre eigenen Startpfade. Geräte mit einem Mindeststrom oberhalb von
6 A erhalten keine 6-A-Probe; für sie gilt weiterhin die normale konservative
Startzuteilung. Ohne vollständige Phasenmessung wird keine kleinere Phasenzahl
behauptet. Es ist keine zusätzliche Konfiguration erforderlich.

## Budget beim Ende der tatsächlichen Ladung

Eine bestätigte Wallboxleistung von 0 W bleibt auch in der Bilanz eine echte
Null. Liegt danach frische Einspeisung vor, kann die Regelung das weiterhin
zentral erlaubte Ladeangebot erhalten, ohne auf einen nachlaufenden Netzfilter
zu warten. Dafür verwendet sie ausschließlich den Rest aus Netzpunkt und
Batterie desselben frischen Messrahmens; Batterieentladung zählt nicht als
PV-Überschuss. Bei einer laufenden oder nicht eindeutig lesbaren Wallbox bleibt
die bisherige Bilanz unverändert. Die zentralen Leistungsgrenzen, Netz- und
Akku-Wh-Wächter sowie Start-, Stopp- und Phasenschutzzeiten gelten weiter.

Die kurze zentrale Reservierung nach einem Lastabfall verhindert weiterhin
Doppelvergaben. Bestätigt die Wallbox frische 0 W und bleibt ihre bisherige
Stecksessiongruppe zum Laden freigegeben, kann deren bereits finanzierter
Reserveanteil wieder in das eigene Ladeangebot eingehen. Dieser Anteil wird
gleichzeitig aus der nicht nutzbaren Reserve entfernt. Ein Fahrzeugwechsel,
fehlende Messwerte, `Aus` oder eine neue Schutzgrenze erlauben diese Umbuchung
nicht. Eine laufende Wärmepumpe verliert dadurch kein bereits gebundenes Budget.

Die Entscheidungsdiagnose trennt das ursprünglich vom Storage Manager gelesene
Budget vom wirksamen Wallboxbudget. Sie zeigt außerdem die verwendete
Bilanzquelle und deren Messzeiten. Diese Angaben erteilen keine Ladefreigabe.

## Netzbezug beim Laden: direkte Regelung und Netzbremse

Der Kern setzt den freigegebenen Anteil direkt um. Seine Netzbremse misst
den Netzpunkt abzüglich frischer gemessener Netzboxleistung. Zwei aufeinander-
folgende Regeltakte über 300 W begrenzen den Strom bis zum Mindeststrom;
ein Stopp aus gewöhnlichem Netzbezug erfolgt über den Sitzungsautomaten.
Nach Start, Anhebung oder Phasenwechsel einer Netzbox zählt ihr Einschwingen
bis zur Quittung nicht für andere Ladepunkte, nach eigenem Ausgang gilt dieselbe
Quittungsregel. Eine frische Probe nach dem Befehl genügt bei Annäherung an den
neuen Sollwert, spätestens nach 30 Sekunden auch bei geringerer Fahrzeugaufnahme.
Eine erneute Anhebung braucht nach der Gegenkorrektur 30 Sekunden unter 300 W.
Hausanschluss je Phase, Nutzer-Aus und Notstromreserve wirken sofort.

Ein größerer Stromsprung kann während der Reaktionszeit des Speichers kurz
Netzenergie benötigen. Die Netzbremse und die elektrischen Grenzen begrenzen
ihn; ein Leistungsangebot ist noch keine gemessene Aufnahme des Fahrzeugs.
Eine bewusst aus dem Netz ladende Box verdrängt den PV-Anteil einer anderen
Box nicht. Fehlende oder mehr als 15 Sekunden alte Netzboxmessungen erlauben
keinen Leistungsabzug vom Netzwert. Details stehen unter „Wallbox-Regelkern“.

### Ungültige Liveprobe: kurzer Halt

Meldet die Plausibilitätsprüfung eine Liveprobe als ungültig (etwa eine
RSCP-Zeitüberschreitung oder leere bzw. unmögliche Werte), sind Netz-, Haus-
und PV-Werte dieser Probe nicht belastbar. Eine laufende Ladung wird deshalb
nicht aus diesen Werten gestoppt, sondern kurz gehalten. Das gilt für alle
geregelten Wallbox-Typen (openWB Pro, openWB, go-e, E3DC).
Weichen Netzwert des E3DC und Summe des Netzzählers voneinander ab, bleibt die Probe gültig; die Abweichung wird als Diagnose vermerkt. Netzladen des Hausspeichers, der Netzanteil der Wärmepumpe, die Nachladung bei Kappung und der Netzanteil beim Fixphasen-Erkennungsstart warten in diesem Moment, weil der freie Hausanschlussrahmen nicht sicher bestimmbar ist.

* Gehalten wird der zuletzt ausgeführte Strom, höchstens 10 s nach der
  letzten gültigen Probe (maßgeblich ist ihr Zeitstempel, nicht der
  Zeitpunkt der Auswertung). In dieser Zeit gibt es kein Anheben, keinen
  Neustart, keinen Phasenwechsel und keine neue Phasenreservierung; auch ein
  Stopp aus den ungültigen Werten wartet.
* Je Wallbox gilt zusätzlich ein Haltebudget: Alle Halte zusammen dauern in
  einem gleitenden Fenster von 60 s höchstens 10 s, jeweils ab der letzten
  gültigen Probe gerechnet. Wechseln gültige und ungültige Proben dauernd,
  endet der Halt so spätestens mit dem aufgebrauchten Budget; danach gilt das
  bisherige Verhalten.
* Voraussetzung sind Anker aus dem letzten gültigen Zyklus: ein frischer
  Status der Wallbox, die bestätigt lädt, ein gültiges Speicherbudget, das
  den laufenden Mindeststrom trägt, und eine gültige Probe mit Zeitstempel
  ohne Netzbezug über der Toleranz `wb_min_current_import_tolerance_w`. Fehlt
  ein Anker, gilt das bisherige Verhalten.
* Ist die Probe ungültig, ihr Netzwert aber ausdrücklich als gültig markiert
  und zeigt er Bezug über dieser Toleranz, gibt es keinen Halt.
* Sofort wirken weiter: Nutzer-`Aus`, Pause und Sperre, Notaus, Zwangsstopp
  des Speichers, Notstromreserve, Ende eines Ladefensters, Ladeende,
  Hausanschluss- und Phasenstromüberlast (auch aus der eigenen Strommessung
  der Wallbox).
* Absenkungen auf einen Strom unter dem gehaltenen, aber mindestens auf den
  Mindeststrom der Wallbox, laufen während des Halts durch (kein Schütz):
  etwa der Deckel des Hausanschlusses, der Grid-Wächter und die
  Defizitkaskade. Der Heartbeat läuft weiter, bei der openWB als Secondary
  auch ihr Heartbeat über den PV-Modus-Befehl, der keinen Strom setzt.
* Kommt vor Ablauf der 10 s wieder eine gültige Probe, regelt der Manager
  normal weiter. Endet der Halt ohne gültige Probe, gilt das bisherige
  Verhalten, und das Startfenster der openWB Pro bricht erst dann ab (Grund
  `live_sample_invalid`).
* Diagnose: Haltevertrag `transient_grid_pm_output_hold` mit Grund
  `invalid_live_sample_no_write_hold`, zurückgestellte Befehle mit
  `invalid_live_sample_output_hold`.

## Haltezone an `wbminsoc`

Der Speicher veröffentlicht den Rahmen der Haltezone als Budget. Der Kern
liest daraus den PV-Anteil; das geschlossene wbminSoC-Tor verbietet nur die
Akkustützung. Trägt PV die Mindestleistung, wird weitergeladen. Andernfalls
endet die Stützung; der vorhandene Sitzungsautomat entscheidet über den Stopp.
Strom und Phasen bleiben Entscheidungen des Wallbox Managers. Der
Speicherregler führt weiter seine Kurve, Quellenfreigabe und Haltezone.
Nutzer-Aus, Notstromreserve, Hausanschluss und Datenvalidität haben Vorrang.

## Phasenwahl bei mehreren Ladepunkten

Sind mehrere Ladepunkte aktiv geregelt, begrenzt die Gruppenverteilung den
ausführbaren Stromausgang bereits auf den Anteil des einzelnen Ladepunkts.
Die Phasenbewertung verwendet dieselbe Basis und nicht mehr das gemeinsame
Gruppenbudget. Ein Ladepunkt hält dadurch keine drei Phasen mehr, wenn seine
eigene Zuteilung das dreiphasige Mindestbudget gar nicht erreicht; er wechselt
stattdessen auf den einphasigen Startpfad, der bei knappem Budget für beide
Ladepunkte reicht.

Eine bereits hardwarebestätigte eigene Ladeleistung bleibt Teil dieser Basis.
Sie ist physisch gedeckt und löst durch diese Kante keinen zusätzlichen
Phasenrückwechsel aus. Die Regel kann das Budget der Phasenbewertung nur
absenken, nie anheben, und erteilt keine neue Leistungsfreigabe. Bei einer
einzelnen Wallbox ändert sich nichts. Mindestströme, Hardware-Sperrzeiten für
einen weiteren Phasenwechsel, CP-Schutz, Nutzer-`Aus` und die zentralen
Leistungsgrenzen bleiben unverändert.

## Konfiguration

Neue Systeme werden im Config-Editor in `data/e3dc_v4.json` konfiguriert.
Die Installationszentrale bietet „Installieren“ erst an, wenn der Schalter
„Native Wallbox-Regelung“ (`wb_native_enable=1`) im Config-Editor aktiviert ist.
Wichtige Felder:

```ini
wb_native_enable = 1
wb_native_type = e3dc_auto   # Standard. Werte wie in der Auswahlliste „Wallbox 1 Modell / API“ des Config-Editors:
                             # none | go-e | openwb | openwb_pro | e3dc_auto | e3dc_efy | e3dc_easy_connect | e3dc_multi | e3dc | dummy
                             # (goe wird als go-e verstanden)
wb_native_type2 = none       # zweite Wallbox, gleiche Werte; none oder leer = keine zweite Wallbox
wb_native_ip = 192.0.2.50
wb_native_mode = 0|1|2   # Dual-Wallbox-Priorität: 0=beide, 1=WB1, 2=WB2
wb1_mode = 0|2|3|4|5|12
wb2_mode = 0|2|3|4|5|12
wbminsoc = 70
wbmaxladestrom = 16
wb1_max_amp = 16
wb2_max_amp = 16
wb1_current_step_amp = 1.0
wb2_current_step_amp = 1.0
wb_openwb_auto_discovery = 1
wb_openwb_auto_role_enable = 1
wb_openwb_command_fail_limit = 3
wb_openwb_command_block_s = 300
wb_openwb_start_cp_retries = 3   # openWB Pro: 1..3, Standard 3; ungültig -> 3 (Deckel je Stecksession über alle Zyklen)
openwb_pro_start_hold_s = 180    # openWB Pro Startfenster: 60..600 s, ungültig -> 180
openwb_pro_start_retry_cycle_s = 300  # openWB Pro Start-Wiederholzyklus: 180..1200 s, ungültig -> 300
openwb_pro_start_cp_grace_s = 60 # openWB Pro CP-Karenz: Untergrenze 60 s (Fahrzeugprofil verkürzt nicht), max 600
wb_openwb_start_retry_s = 45     # openWB/openWB Pro: Mindestabstand für Start-Wiederholungen; wirksam mindestens 30 s, im Weckschritt des Pro-Startfensters mindestens 60 s. Den Wiederholzyklus des Startfensters bestimmt openwb_pro_start_retry_cycle_s.
openwb_pro_start_reject_timeout_s = 120  # openWB Pro: Mindestalter eines unbeantworteten Startangebots (wirksam mindestens 60 s); der Start-Reject-Latch verlangt zusätzlich eine vollständige Start-Episode und frischen SoC am Ziel.
openwb_pro_start_grace_s = 60    # openWB Pro: Annahmefrist eines frischen Startangebots; in dieser Zeit erzeugt ein Budget-Einbruch kein 0 A. Im haltenden Startfenster ohne zusätzliche Wirkung.
```

Eine alte `e3dc.config.txt` ist nur noch Migration und Legacy-Fallback. Neue
Einstellungen gehören in `e3dc_v4.json`.

Die Ladepriorität wird in der Wallbox-WebUI nur angezeigt, wenn WB1 und WB2
konfiguriert sind. Bei Ein-Wallbox-Anlagen bleibt die Verteilung automatisch
ausgeglichen und der Prioritätsschalter wird ausgeblendet.

### Ladeplan je Wallbox

```ini
wb1_plan_hours = 2          # manuelle Ladezeit im Preisfenster (0 = kein Plan, 99 = Sofort)
wb1_wbvon = 00:00|now       # Frühestens ab: feste Uhrzeit oder rollender Jetzt-Anker
wb1_wbbis = 07:00           # Fertig bis
wb1_smart_wbhour_enable = 0 # 1 = Dauer aus Fahrzeug-SoC, Ziel-SoC und Ladeleistung
wb1_native_eco = 1          # Eco-Score als Zusatzsortierung
wb1_plan_repeat = 0         # 1 = Täglich wiederholen (nur feste Startuhrzeit, manuelle Stunden)
```

Ein manueller Stundenplan ist ohne Wiederholung ein einmaliger Auftrag: Sind
alle geplanten Slots durchlaufen oder ist ein `now`-Fenster abgelaufen, setzt
der Planer `wb{N}_plan_hours` auf `0` („Plan verbraucht“), damit nicht jede
Nacht unbemerkt Netzstrom geladen wird. Mit `wb{N}_plan_repeat = 1` bleibt die
Stundenzahl erhalten: Der verbrauchte Plan bleibt bis zum Ende des laufenden
Fensters als Beleg liegen (kein zweites Laden im selben Fenster) und wird erst
für das nächste Fenster neu auf die günstigsten Slots gelegt. Liegen für den
Folgetag noch keine Preise vor, wartet der Planer ohne Reset. `now`, Sofortladen
(`99`) und das 24h-Rollfenster kennen keine Fensterinstanz; dort bleibt der
Schalter wirkungslos und die WebUI sperrt ihn.

`Sofort bis Preislimit` (Modus 5) wird für E3DC-, openWB- und go-e-Wallboxen als
gewöhnlicher Konfigurationscommit übernommen; die Preisfreigabe entscheidet der
Manager zyklisch. Nur openWB Pro erhält zusätzlich den typisierten, an die
Pro-Stecksession gebundenen Sofortauftrag (`wallbox_mode5_user_start_request.json`).

„Netz erlaubt + Fertig bis“ (Modus 5 mit `wb{N}_smart_wbhour_enable = 1`) ist
plan-gebunden, sobald ein bestätigter Fahrzeug-SoC vorliegt (manueller
Ist-SoC oder frischer Fahrzeugwert nach demselben Vertrag wie „Auto voll“):
Netzstrom fließt dann nur in den geplanten günstigen Slots bis „Fertig bis“,
außerhalb laden PV und Speicher. Ohne bestätigten SoC kann der Planer die
Dauer nicht bestimmen; die Wallbox lädt sofort bis Preislimit und das
Dashboard meldet `price_plan_soc_missing` als Warnung. Die Entscheidung steht
je Zyklus in `price_plan_bound_ids` / `price_plan_soc_missing_ids`.

## Sollstrom-Schrittweite

Der zentrale Wallbox-Manager entscheidet Budget, Netzfreigabe,
Hausakku-Untergrenze, Hysterese und Phasenfreigabe. Die konkrete Rundung des
Ampere-Sollwerts ist Treibervertrag:

* E3DC und go-e bleiben konservativ auf ganze Ampere gerundet.
* openWB Pro nutzt die `connect.php`-Schnittstelle mit 0,1-A-Schritten.
* openWB Software im Secondary-Pfad bleibt standardmäßig bei 1,0 A; pro
  Wallbox kann `wb1_current_step_amp` bzw. `wb2_current_step_amp` auf `0.5`
  oder `0.1` gesetzt werden, wenn die konkrete openWB-/Firmware-Kombination
  diese Werte annimmt und an den Ladepunkt weitergibt.

Dadurch kann die zentrale Regelung Leistungsbudgets als Dezimal-Ampere bis zum
Treiber transportieren, ohne unsichere Hardwarepfade global feiner zu stellen.

## Fahrzeug-SoC-Vertrag

Die Fahrzeugauswahl und der Fahrzeug-SoC sind getrennte Informationen. Die
Auswahl sagt nur, welches Profil an welcher Wallbox steht. Ein SoC wird erst
zur Regelbasis, wenn er frisch bestätigt ist:

* die Wallbox oder openWB/openWB Pro meldet den SoC,
* Bluelink oder ein konfiguriertes MQTT-Topic liefert den Fahrzeug-SoC,
* der Nutzer trägt den aktuellen Wert ein und klickt **SoC setzen**.

Alte Startwerte, einfache Profilwerte und Werte aus einer beendeten Session
werden nicht als Regelwert fortgeschrieben. Ein frisch beobachteter openWB-SoC
kann mit Quelle und Alter rein lesend angezeigt werden, wenn aktuelle
Stecksession oder Fahrzeugprofil eindeutig passen. Dieser Anzeigeweg bleibt
von Ziel-SoC, `Auto voll`, Planung und Hardwareausgang getrennt. Nach Abstecken
wird die SoC-Regelsession geschlossen; nach erneutem Anstecken bleibt sie ohne
neue Bestätigung gesperrt. Laden nach PV, Mindestleistung, Preisfenster oder
kWh-Ziel bleibt möglich.

Fehlt bei aktiver Zielplanung ein bestätigter Fahrzeug-SoC, plant der Ladeplaner
konservativ mit 0 % (volle Energiemenge bis zum Ziel). Die geplanten Ladefenster,
auch solche mit Netzbezug, bleiben dann bestehen, selbst wenn das Fahrzeug
bereits voll ist; die Regelung gibt in ihnen weiter frei. Für eine genaue
Planung den Fahrzeug-SoC eintragen oder übertragen lassen. Im Protokoll steht
dazu je Stecksession und Wallbox ein einzelner Hinweis.


## Zustandsmaschine und Ladeende

Der Wallbox-Manager verwendet eine explizite Zustandsmaschine. Die Diagnose zeigt diese Zustände:

| Zustand | Bedeutung |
|---|---|
| `idle` | Keine Ladeanforderung und kein bestätigter Energiefluss. |
| `offered` | Ein zulässiges Leistungsangebot liegt vor. |
| `starting` | Ein expliziter Startimpuls wurde gesendet; echte Ladung ist noch nicht bestätigt. |
| `charging` | Stromfluss und Ladestatus bestätigen die laufende Session. |
| `stopping` | Ein Stopimpuls wurde gesendet; Schutz- und Rücklesefrist laufen. |
| `ended` | Die Session ist fachlich beendet und bleibt bis zu einer benannten Freigabe gelatcht. |
| `rscp_error` | Antwort oder Rücklesung ist ungültig; es wird kein Erfolg vorgetäuscht. |

Bei der openWB Pro führt ein bestätigtes Fahrzeug-Ladeende zur Bereitschaft:
Solange die Regelung das nötige Budget freigibt, bleibt der eingestellte
Mindeststrom angeboten. Die Anzeige unterscheidet dieses Angebot von echter
Ladeleistung. Eigene Ladeziele und Schutzsperren bleiben verbindlich. Für die
anderen Treiber gilt weiterhin der bestehende Ladeende-Latch.

Ein Ladeende-Latch darf grundsätzlich durch einen bewussten UI-Wechsel
(`wallbox_php_limit_or_profile_change`) oder einen bestätigten Neustart des
Fahrzeugs (`vehicle_self_restart`) freigegeben werden. Eine vollständig belegte
3/3-Startablehnung der openWB Pro ist davon ausgenommen: Bloße Modus-, Limit-,
Ziel-SoC- oder Profiländerungen erhalten ihren Latch und die Wake-up-Evidenz.
Sie werden nur durch eine bestätigte neue Stecksession oder einen typisierten
Modus-5-Nutzerauftrag gelöst, der bereits bei Annahme exakt an Boot,
Stecksession, Konfigurationsstand, Preislimit und dieselbe persistierte
Latchgeneration gebunden wurde. Andere, nicht vollständige
Startablehnungs-/Ladeende-Latches behalten den bisherigen bewussten
UI-Freigabevertrag. Stromrampen, Start-/Stopflanken und Ladeende bleiben
getrennte Verträge; Manager, Treiber und Diagnose geben denselben Zustand aus.

## E3DC-native Regelvertrag

Die native E3/DC-Wallbox wird über die lokale RSCP-Verbindung des
Hauskraftwerks angesprochen. Maßgeblich sind dessen Serveradresse, Port,
Benutzer, Portalpasswort und RSCP-/AES-Passwort aus der gemeinsamen
Konfiguration. Eine im Wallboxbereich eingetragene Ladepunkt-IP ersetzt diese
Verbindung nicht. In JSON gespeicherte Zeichen wie `#`, `//` und Leerzeichen
innerhalb eines Passworts gehören unverändert zum Wert.

Für efy, Multi Connect und Easy Connect muss die bewusst gewählte
WBchar6-Kompatibilitätsregelung aktiviert sein, wenn die native Regelung
steuern soll. Bei ausgeschaltetem Kompatibilitätsmodus bleibt dieser Pfad
beobachtend. Eine erfolgreiche Anmeldung allein erteilt keine Steuerfreigabe.

Bei ausdrücklich ausgewählter **efy** oder **Multi Connect II** erkennt die
Regelung die vom Hersteller dokumentierte automatische 1-/3-Phasenumschaltung.
Ein zusätzlicher versteckter Nachweis-Schalter ist dafür nicht erforderlich.
Multi Connect II im Konfigurationseditor unter „Wallbox 1 Modell / API“ bzw.
„Wallbox 2 Modell / API“ als „E3/DC Multi Connect I/II“ auswählen und im Feld
„Modell“ ausdrücklich „Multi Connect II“ angeben. „Multi Connect I“ und
„nicht festgelegt“ behalten das bisherige Verhalten.
Im freigegebenen PV-Betrieb kann der vorhandene Sonnenmodus die Phasenwahl an
die E3/DC-Automatik übergeben. Die Diagnose unterscheidet diese Herstellerfähigkeit
von einem tatsächlich beobachteten Phasenwechsel. Die automatische
Phasenumschaltung muss auch am E3/DC-Gerät aktiviert sein.

Das freigegebene Budget bleibt dabei der Rahmen für die Energieabrechnung:
Gemessene Mehrleistung zählt auch im autonomen Sonnenmodus im Wh-Wächter.
Eine nur aus Phasenleistungen geschätzte Stromuntergrenze unter 6 A ist kein
Beweis für einen Ladestopp. Eine bestätigte reale Ladung bleibt überwacht;
aus dieser Schätzung entstehen keine positiven Strom- oder Phasenbefehle.

Diese Übergabe setzt frische, gültige Statusdaten und den aktivierten
Kompatibilitätspfad voraus. `Aus`, Stopps, Hausanschlussgrenzen, Wh-Wächter und
Speicherreserven bleiben wirksam. Die Stromvorgabe gilt je genutzter Phase;
ein Wattbudget ist in diesem Automatikpfad keine vom Gerät garantierte harte
Leistungsgrenze. Die elektrische Absicherung berücksichtigt deshalb weiterhin
die mögliche dreiphasige Last. Direkte Phasen-Schreibbefehle werden dadurch
nicht freigeschaltet. Easy Connect, nicht eindeutig bestimmte Geräte und die
Modellwahl Multi Connect ohne II erhalten diese Fähigkeit nicht automatisch.

**Experimenteller Direktvertrag für Phasenwechsel.** Der Schalter
`wb_e3dc_direct_phase_control_enable` (je Wallbox überschreibbar mit
`wb1_…`/`wb2_…`) ist standardmäßig aus. Eingeschaltet schaltet E3DC-Control bei
ausdrücklich gewählter efy oder Multi Connect die Phasen selbst: Je
Phasenwechsel schreibt die Regelung die Geräteeinstellungen Sonnenmodus,
automatische Phasenumschaltung und Phasenzahl und stellt Sonnenmodus und
automatische Phasenumschaltung bei der Rückgabe wieder her. Ob die Wallbox
diese Einstellungen dauerhaft speichert, ist nicht belegt; der Schalter ist
deshalb nicht für den Dauerbetrieb empfohlen, und die Konfigurationsprüfung
warnt, solange er eingeschaltet ist. Während der Direktvertrag eingeschaltet
ist, übergibt die Regelung die Phasenwahl nicht zusätzlich an die
E3/DC-Automatik (Sonnenmodus-Übergabe) – ein Regler je Wallbox. Fehlt dabei
die vollständige Rücklesung der Geräteeinstellungen, bleiben beide Wege
geschlossen und die Wallbox lädt mit fester Phasenzahl.

Referenzen: [E3/DC-Wallboxen](https://www.e3dc.com/produkte/wallbox-ii/),
[Herstellerdaten Multi Connect / Multi Connect II](https://www.e3dc.com/ch/wp-content/uploads/sites/4/2022/09/E3DC_TDB_wallbox-multi-connect.pdf).

Bei wiederholt fehlgeschlagener RSCP-Anmeldung werden die Verbindungsversuche
mit wachsendem Abstand bis höchstens 30 Sekunden wiederholt. Fehlgeschlagene
Verbindungen werden geschlossen; eine erfolgreiche Sitzung wird für weitere
Abfragen wiederverwendet. Bei einer Authentifizierungsmeldung zunächst die
gemeinsamen RSCP-Einstellungen lokal prüfen. Zugangsdaten gehören nicht in
öffentliche Diagnoseauszüge oder Supportnachrichten.

Die native E3DC-Wallbox wird anders behandelt als openWB, openWB Pro oder go-e.
Die E3DC-RSCP-Schnittstelle arbeitet als Flankensteuerung mit
Messwert-Rückmeldung und nicht als absoluter Start-/Stop-Schalter:

* `WB_REQ_SET_EXTERN` setzt den Betriebsmodus und den Stromdeckel. `WBchar6[1]`
  ist der gewünschte Amperewert.
* `WBchar6[4]` ist ein Umschaltimpuls. Stromvorgaben und Heartbeats setzen
  dieses Bit nie; Start und Stopp benutzen dasselbe Bit.
* Stopp: Das Abbruchbit der Box zeigt, ob die Box eine Ladung freigibt oder gesperrt hat. Ein Stoppimpuls erfolgt nur bei frischem `abort_active=False` („Box freigegeben“).
  `abort_active=True` („Box gesperrt“) quittiert den Stopp ohne weitere Flanke,
  auch wenn die Ladeleistung noch nachläuft. Das gilt auch für Pause, Sperre
  und Not-Aus sowie für eine freigegebene Box ohne Leistungsbezug.
* Start: nur bei frischem `abort_active=True`; der Wechsel auf `False`
  bestätigt die Freigabe. Die Easy Connect behält bis zu drei Startimpulse
  je Stopp-Episode mit mindestens 60 Sekunden Abstand, jeder mit erneutem
  frischem Abbruchbit der Box. Andere Familien erhalten genau einen Startimpuls.
* Frisch heißt: gültige Probe höchstens 15 Sekunden alt, angefragt nach
  Dienststart und nach der letzten eigenen oder mehrdeutig übertragenen Flanke.
  Widersprechen sich ALG-Bit 0x40 und `WB_ABORT_CHARGING`, ist der Zustand unbekannt.
* Eine Stoppflanke ohne Wire-Quittung darf genau einmal wiederholt werden,
  wenn eine mindestens 30 Sekunden später angefragte frische Probe weiter
  „nicht abgebrochen“ zeigt. Bei Wire-Quittung ohne Bitwechsel nach 30 Sekunden
  folgt ein Fehler: bei 0 W „Zustand der Box nicht lesbar“,
  sonst „Box reagiert nicht auf den Stopp“. In diesem Fehlerzustand erhalten Pause, Ladepunktsperre, Not-Aus, Notstromreserve und Hausanschluss gemeinsam genau eine zusätzliche Stoppflanke: frühestens 60 Sekunden nach dem letzten Stoppversuch, bei frischer Probe mit „nicht abgebrochen“ oder unbekanntem Bit und nur mit frischem Ladebeleg. Die Fehleranzeige erscheint ab Fehlerbeginn. Die zusätzliche Einmalflanke ist höchstens einmal je Stecksession und Dienstlauf zulässig; ist sie verbraucht oder für den Stoppgrund nicht berechtigt, bittet die Oberfläche, den Ladepunkt zu prüfen. Nach einem Neustart des Dienstes steht sie einmal neu zur Verfügung. Eine Stecksession endet erst mit bestätigtem Abstecken: mindestens drei aufeinanderfolgende frische Proben ohne gestecktes Fahrzeug über mindestens zehn Sekunden. Insgesamt sind höchstens drei Versuche je Ereignis zulässig; eine bereits verbrauchte Einmalflanke wird durch einen späteren Nutzer-Stopp nicht erneuert.
* Bei „Box gesperrt“ und frischer Leistung über 500 W länger als 30 Sekunden
  lautet der Fehler „Box gesperrt, lädt aber“. Eine weitere Flanke wäre eine
  Freigabe und bleibt aus.
* Fehlt das Bit über mehrere Proben, gilt nur der einmalige Rückfall
  mit frischem Ladebeleg, ohne Wiederholung, mit Hinweis „Zustand der Box nicht lesbar“.
  Start ohne Bit folgt der bisherigen Startregel. Ein veralteter oder ungültiger
  Boxstatus erlaubt weder Bitregel noch Rückfall.
* Ein externer Neustart ist nur der beobachtete Wechsel True → False nach
  einem eigenen bestätigten Stopp derselben Stecksession. Bei Nutzer-Aus,
  Notstromreserve, Hausanschluss, Datenfrist, Ladefensterende, Ziel erreicht,
  Abfahrt oder Budget-Zeitlimit wird frühestens nach 30 Sekunden erneut gestoppt.
  Bei Unterdeckung übernimmt der Regelkern die externe Ladung und beendet sie
  über Mindestladezeit und Unterdeckung. An der
  Pre-Dump-Untergrenze bleibt die vorhandene Stoppregel wirksam.

Bei nativen Boxen hebt Fortsetzen in „Beobachten“ nur die eigene Pause oder Ladepunktsperre derselben Stecksession genau einmal auf, wenn die Box frisch „abgebrochen“ meldet. Diese Übergabe benötigt kein Regelbudget. Eine App-Sperre wird nicht aufgehoben. Nach Not-Aus erfolgt in „Beobachten“ keine automatische Freigabe; die Box bleibt bis zur App-Freigabe gesperrt. Endet eine Pause in „Beobachten“ durch bestätigtes Abstecken, bleibt eine native Box gesperrt, bis sie in der E3DC-App freigegeben oder ein Regelmodus gewählt wird. Die bewusste Freigabe über den Schalter der Ladepunktsperre gibt die Box genau einmal frei, auch ohne gestecktes Fahrzeug, solange seit der Sperre weder eine Pause noch ein neues Anstecken dazwischenlag.

Die Regelung glaubt einer Startfreigabe nicht blind. Eine E3DC-Ladung gilt erst
als echt, wenn mindestens einer dieser Nachweise vorhanden ist:

* `TAG_WB_EXTERN_DATA_ALG` meldet Laden bzw. Start über die E3DC-Statusbits.
* Die Phasenleistungen `TAG_WB_PM_POWER_L1/L2/L3` ergeben eine plausible
  verifizierte Wallboxleistung.

Zustände wie `Startfreigabe`, `freigegeben`, `Start wartet`,
`Wartet Mindestleistung` oder `Warte auf Sonne` sind nur angebotene Leistung.
Sie dürfen im Frontend und in der Budgetrechnung nicht als echte Ladung
fortgeschrieben werden, solange keine verifizierte Phasenleistung oder aktive
Ladebestätigung vorliegt. Beim Ladeende muss die angezeigte Wallboxleistung
sofort auf `0 W` fallen, damit Phantomladen nicht wieder Hausverbrauch,
Langzeitwerte oder Folgeregelungen verfälscht.

Ein erkannter Ladeende-Latch blockiert erneute Autostarts bis zu einem klaren
Ereignis. Gültige Ausnahmen sind Umstecken oder Fahrzeugwechsel, bewusste
Änderungen in `Wallbox.php` an Modus, Maximalstrom, Ziel-SoC oder
Fahrzeugprofil sowie ein echtes Selbst-Wiederanlaufen des Autos mit
verifizierter Ladeleistung. Ein geänderter Stromdeckel ist dabei nur die
Freigabe für einen neuen Versuch; er beweist niemals aktive Ladung.

## openWB richtig einordnen

openWB muss nach Betriebsart unterschieden werden. Die Begriffe klingen ähnlich,
führen technisch aber in verschiedene Schnittstellen. Wichtig ist: Es darf nur
einen aktiven Regler geben. Wenn openWB Software 2.x selbst regelt, darf
E3DC-Control nicht gleichzeitig denselben Ladepunkt über den Secondary-Pfad
überfahren.

Die Autoerkennung reduziert hier Fehlkonfigurationen: E3DC-Control ruft
`simpleAPI.php?get_chargepoint_all` und, wenn erreichbar, das V1-Config-Topic
`openWB/chargepoint/<id>/config` read-only ab. Wird ein interner openWB-
Ladepunkt mit `type=internal_openwb` oder `configuration.mode=series` erkannt,
behandelt der Treiber diese Wallbox als openWB-Primary und nutzt den
Primary-simpleAPI-Pfad. Ist in E3DC-Control ausdrücklich Modbus Secondary oder
Primary konfiguriert, bleibt diese Betreiberentscheidung sichtbar und wird nur
mit der erkannten openWB-Rolle abgeglichen.

Ab 5.4.5a ergänzt ein strikt lesender SoC-Abonnent den bisherigen openWB-
Statuspfad. Er zeigt einen frischen Beobachtungswert nur mit passender Session-
oder Fahrzeugbindung und, soweit vorhanden, mit echtem Quellalter. Der
Abonnent veröffentlicht keine MQTT-Befehle und übernimmt keine Leistung,
Steckzustände oder Schaltzustände. Auch ein sichtbarer Wert erteilt keine
Regelautorität; dafür gilt weiterhin der getrennte bestätigte SoC-Vertrag.

Bei einer openWB-Software mit zwei Ladepunkten kann die Erkennung WB2 zur
Laufzeit ergänzen, wenn WB1 als openWB Controller konfiguriert ist, dieselbe
IP beide Ladepunkte meldet und WB2 noch leer ist. Das schreibt keine neue
Konfiguration, sondern verhindert nur, dass ein vorhandener zweiter Ladepunkt
unsichtbar bleibt.

Wenn drei Schreibbefehle hintereinander nicht bestätigt werden, pausiert
E3DC-Control weitere openWB-Schreibbefehle kurz und meldet den Zustand im
Frontend als `openWB-Befehle nicht angenommen`. Damit wird eine falsche
Primary/Secondary-Annahme sichtbar, statt still weiter Befehle zu senden.

| Aufbau | Einstellung in openWB | Auswahl in E3DC-Control | Bemerkung |
|---|---|---|---|
| **openWB Pro standalone** | Keine openWB-Software-Rolle nötig. Die Pro wird direkt angesprochen. | `openWB Pro (connect.php)` mit IP der Pro | Empfohlener Masterpfad, wenn E3DC-Control regeln soll. E3DC-Control setzt `ampere`, `phasetarget` und bei Bedarf `cp_interrupt`. |
| **openWB Software 2.x als Primary mit Pro/Satellit/Fremdwallbox** | `Einstellungen -> Allgemein -> Steuerungsmodus: primary`; Ladepunkt in openWB einrichten. | `openWB Controller`, `openWB Primary` bewusst aktiv | openWB regelt selbst. E3DC-Control wertet reale Leistung aus und kann den openWB-Lademodus per simpleAPI umschalten. Aktive Stromvorgaben laufen im Primary-Direktpfad über openWB-Sofortladen (`chargecurrent`); openWB-SoC- und Energiemengenlimits bleiben wirksam. |
| **openWB Software 2.x als Secondary** | `Steuerungsmodus: secondary`; `Steuerung über Modbus als secondary: An`; danach openWB neu starten. | `openWB Controller`, Secondary/Modbus aktiv, Port `1502`, Slave-ID `1` | E3DC-Control gibt Sollstrom plus Heartbeat vor; openWB stoppt bei ausbleibendem Heartbeat. |
| **Nur Messwerte aus evcc/openWB** | Keine Steuerfreigabe nötig; nur MQTT-Leistungstopic bereitstellen. | Observe-only per MQTT-Leistungstopic | Keine Steuerbefehle; Leistung wird für Dashboard, Historie und Hausverbrauchsbereinigung genutzt. |

Bei `openWB Primary` gibt es deshalb zwei bewusst benannte Rollen: **Primary
PV-geführt** heißt, openWB führt den Ladepunkt; E3DC-Control hält den normalen
Speicher-Laderahmen und begrenzt bei laufender Ladung nur die Entladeseite.
In `PV-Kurve ruhig` versorgt der Akku höchstens das Haus-/WP-Defizit. In
`PV + Akku bis Untergrenze` ist die Entladung oberhalb von `wbminsoc` frei,
am Boden gilt wieder der Haus-/WP-Deckel. Die Untergrenze hebt die Kurve nicht
zusätzlich an. Das aktive Planziel und der normale Kurvenregler bestimmen
die Speicherladung.

Empfohlen ist auf der openWB die Ladepriorität **Speicher**, damit ihre
Überschussregelung die Speicherladung nach Ladekurve berücksichtigt. Bei
Ladepriorität **Fahrzeuge** zählt die openWB auch die Speicherladung als
verfügbaren Überschuss. Jede Speicherentladung zieht sie grundsätzlich vom
Überschuss ab: Akkustrom für das Auto in `PV + Akku` muss deshalb zusätzlich
in der openWB selbst erlaubt sein. Die Entladefreigabe von E3DC-Control allein
erzwingt keine Fahrzeugladung.

Ein bewusster Wechsel in E3DC-Control auf `PV-Kurve ruhig` oder `PV + Akku bis
Untergrenze` setzt die openWB einmal auf PV. Eine direkt an der openWB gewählte
Einstellung `Sofort` oder `Stop` bleibt danach auch über einen Manager-Neustart
bestehen; der Speicher folgt weiter dem E3DC-Control-Modus. Einen selbst
vorher gesendeten Stop oder Sofortauftrag nimmt E3DC-Control nur mit einem
gespeicherten Eigenbefehl-Beleg zurück. Er bleibt bei Neustart und
Konfigurationsänderung höchstens sieben Tage erhalten. In `Aus` erfolgt
keine Modusrücknahme. Meldet die openWB einen anderen Modus,
entfällt die Rücknahme. Eine Handpause sendet einmal Stop; beim Aufheben
erfolgt die Rücknahme über denselben Beleg. Scheitert ein bewusster
PV-Moduswechsel, wird er höchstens alle 30 Sekunden für fünf Minuten
wiederholt. **Primary-Direktpfad**
heißt, E3DC-Control setzt Strom über den dokumentierten openWB-Sofortladen-Strom
`chargecurrent`; openWB zeigt dann Sofortladen und Sofortladen-Limits wie
Ziel-SoC oder Energiemenge können die Ladung beenden. Wer eine aktive
E3DC-Control-Stromführung ohne diesen openWB-Sofortladen-Pfad möchte, betreibt
die openWB Software als Secondary oder eine openWB Pro direkt über `connect.php`.
Wenn openWB für die simpleAPI mit `401 Unauthorized` antwortet, verlangt die
openWB-HTTP-Seite eine Anmeldung. In diesem Fall nutzt E3DC-Control die
bestehenden Wallbox-Zugangsdaten `wb_user`/`wb_pass` als Basic Auth; ohne
gesetzten Benutzer wird kein Auth-Header gesendet.

### openWB Pro als reiner Aktuator

Für den reibungslosen Betrieb einer openWB Pro mit E3DC-Control hat sich dieser
Pfad bewährt:

* **Direkte HTTP-API:** Die Pro wird über ihre Hardware-IP und `connect.php`
  angesprochen. Eine zusätzliche openWB-Software-2-Instanz als Vermittler führt
  leicht zu Ping-Pong, weil die SW2 ihre eigene Regellogik gegen die Vorgaben
  von E3DC-Control setzt.
* **Klare Stromvorgaben:** E3DC-Control trennt hart zwischen `0 A` als Stop und
  dem normgerechten Startbereich ab `6 A`. Beendet das Fahrzeug selbst die
  Ladung, bleibt bei ausreichendem Budget der konfigurierte Mindeststrom
  angeboten, zum Beispiel `6 A`. Das Fahrzeug kann damit selbst wieder laden
  oder Energie für die Vorklimatisierung beziehen. Die Bereitschaft löst keine
  wiederkehrenden Startimpulse, CP-Unterbrechungen oder Phasenwechsel aus.
  Sie bleibt an dieselbe Stecksession gebunden und übersteht einen Neustart
  des Managers. Sie bedeutet weder einen bestätigten Fahrzeug-SoC von 100 %
  noch dauerhaft geschlossene Leistungsschütze.
* **Budgetabhängige Bereitschaft:** Entscheidend ist das Quellenbudget des
  aktiven Modus für die frischen, von der Box zurückgemeldeten Phasen.
  Weichen Phasenvertrag und gespeicherte Ladeendephase ab, gilt der größere
  Leistungsbedarf. Ein auf 0 W
  gefallener Ladepunktrahmen nach einem Fahrzeug-App-Stopp ersetzt diese
  Messung nicht: Bei weiter ausreichender Quelle bleibt das Angebot stehen.
  Das PV-Budget folgt der bestehenden Bilanz aus Netzpunkt und Wallboxlast,
  ohne Akkuentladung als PV zu verbuchen. Die externe Wallbox steckt im
  gemessenen Hausverbrauch; ihre Leistung wird nicht zusätzlich abgezogen.
  Netz und Akku zählen nur mit den Freigaben und Grenzen des aktiven Modus.
  In „PV-Kurve ruhig“ zählt das Akku-Wolkenkontingent nicht als
  Bereitschaftsquelle. Bei mehreren Ladepunkten muss jeder Status frisch und
  gültig sein. Bereits verteilte Leistung zählt auch vor der tatsächlichen
  Ladeannahme als reserviert. Istleistung und Zuteilung derselben Box werden
  einmal berücksichtigt, der höhere Wert gilt. Das Mindestangebot erhält
  nur den danach freien Anteil des Quellenbudgets; das gilt auch bei
  freigegebener Netz- oder Akkuladung. Ruhende Angebote werden nach der
  bestehenden Verbraucherpriorität reserviert. Ein kurzer Statusausfall
  eines anderen Ladepunkts hält ausschließlich ein zuvor gedecktes,
  frisch bestätigtes Angebot unverändert, bis 45 Sekunden nach dem letzten
  gültigen Quellenbeleg. Er erlaubt weder Anheben noch Neuangebot; danach
  gilt 0 A. Der eigene Status und die Liveprobe müssen gültig bleiben. Gruppen- und
  Hausanschlussgrenzen gelten auch während der Toleranz sofort.
  Ein nur kurz veralteter Budgetdatensatz (15 bis unter 45 Sekunden) hält
  ausschließlich ein zuvor gedecktes, bereits stehendes Angebot unverändert.
  Er erlaubt weder eine Anhebung noch ein neues Angebot. Ab 45 Sekunden,
  bei ungültigen Live-Daten oder einer harten Schutzgrenze folgt 0 A.
  Ein kurzer Budgeteinbruch lässt das Angebot für `openwb_pro_start_hold_s`
  stehen (Standard 180 s, gültig 60–600 s; bei HLC oder unbekannter
  Signalisierung gilt der vorhandene Faktor 2). Danach setzt die Regelung
  einmal `ampere=0` und bleibt ohne weitere Befehle, solange der Zustand
  unverändert ist. Nutzer- und Schutzgrenzen wirken weiterhin sofort.
  Bei erneut ausreichendem Budget bietet sie
  nach den bestehenden Wiederanlauf- und Schutzfristen den Mindeststrom mit
  einem Strombefehl an, ohne Startfenster-Wiederholung, Weckimpuls,
  CP-Unterbrechung oder Phasenwechsel. Es entsteht keine dauerhafte
  Fahrzeug-Ladeende-Sperre. `6 A` an drei
  Phasen benötigen rund `4,14 kW`; eine Nullmessung wird nicht als einphasiges
  Fahrzeug behandelt. Nach einem Stop der Defizit-Kaskade (Netz- oder
  Akku-Wh-Konto) in derselben Stecksession bietet die Regelung den Mindeststrom
  erst wieder an, wenn das PV-Budget die Mindestleistung der erwarteten
  Phasenzahl `wb_pv_only_release_hold_s` (Standard 120 s) lang durchgehend
  deckt; eine einzelne Wolkenlücke startet nicht. Dieser Bereitschaftspfad
  setzt Fahrzeug-Ladeende-Evidenz derselben Stecksession voraus. Ein
  Kaskadenstopp während normaler Ladung ohne solche Evidenz kehrt über den
  dokumentierten Weg „Wiederanlauf als Einladung“ mit Startfenster und
  bestehenden Wiederhol- und Weckfristen zurück.
  Die Budgetrückkehr der Bereitschaft wird wie ihr Halten am Quellenbudget
  gemessen, auch wenn der Ladepunktrahmen weiterhin 0 W beträgt. Eigene
  Ziel-SoC-/Energiemengen- und Abfahrtsgrenzen, Nutzerpause und
  Schutzvorgaben behalten Vorrang. `Aus` bleibt beobachtend.
  Beim Eintritt in die Bereitschaft endet ein alter Startfensterkern.
  Sobald echte Ladeleistung bestätigt wird, übernimmt die normale Regelung.
  Bei der Pro ist `ampere=0` bereits der Stopbefehl; ein zusätzlicher
  CP-Reset wird für die Budgetpause nicht gesendet.
* **Phasenwechsel mit Haltezeit:** E3DC-Control setzt über `connect.php` nur das
  Ziel (`phasetarget=1` oder `phasetarget=3`). Die Hardware der Pro übernimmt
  Schütz-Trennung und CP-Ablauf. Nach der kurzen sicheren 0-A-/CP-Beruhigung
  darf der Strom wieder anlaufen. Der persistente Schutz von mindestens
  `480 s` beginnt erst mit dem bestätigten Wire-Receipt dieses realen
  Phasenausgangs. Eine reine Budgetreservierung erzeugt keinen Cooldown. Die
  Sperre schützt ausschließlich vor einem weiteren Phasenwechsel; sie
  blockiert weder den bestätigten Wiederanlauf noch die laufende Stromregelung.
  Wechselt die Box ohne laufende Ladung, etwa vor dem Start auf eine Phase,
  bietet das Startfenster nach dem frisch bestätigten neuen Ziel 6 A auf den
  Zielphasen an; Startfenster und Phasenwahrheit der Stecksession warten dafür
  nicht auf Ladeleistung. Die Phasenreservierung endet erst, wenn die
  Zielphasen unter Last bestätigt sind (mindestens drei Messungen über 500 W,
  stabil über mindestens 10 s). Das gilt auch nach Ablauf der
  Reservierungsfrist, nach einem Neustart des Managers sowie nach einem
  Force-Start oder einem Wechsel auf `Aus` und zurück; eine mehrdeutige
  Ausgangslage bleibt gesperrt, bis der Recovery-Pfad sie klärt (siehe
  „Recovery vor neuem Budget“). Schaltet der Nutzer dagegen auf `Aus`, bevor
  das Phasenziel gesendet ist (etwa nach dem 0-A-Schritt einer
  Kaskadensequenz), endet der Phasenwechsel sofort und ohne weiteren Ausgang:
  Die Reservierung wird freigegeben, und nach der Rückkehr entscheidet die
  Regelung neu, ohne die alte Phasenentscheidung.
* **Phasenbeharrung 3p→1p:** Der Regelkern steigt nach 60 Sekunden
  Unterdeckung ab. Nach einem bestätigten Phasenwechsel gilt eine
  feste Phasensperre von 480 Sekunden. Beide Zeiten sind nicht konfigurierbar.
* **Hochschaltung 1p→3p:** Der zugeteilte Leistungsanteil muss den
  dreiphasigen Mindeststrom über 60 Sekunden decken. Fahrzeug, Wallbox,
  Hausanschluss und Phasenfreigabe müssen den Wechsel erlauben.
  Als Budget zählt die für genau diesen Ladepunkt zugeteilte Leistung; sie
  enthält die laufende Ladeleistung bereits und wird nicht zusätzlich addiert.
  Ein Export-Wh-Konto, Referenzstrom oder zusätzlicher Leistungspuffer ist dafür nicht erforderlich.
  Auch ein 16-A-Ladepunkt kann bei ausreichendem gemeinsam zugeteiltem Anteil auf drei Phasen wechseln; maßgeblich ist die Deckung des dreiphasigen Mindeststroms.
* **Recovery vor neuem Budget:** Eine mögliche ältere Ausgangsgeneration wird
  vor einem neuen Storage-Grant und vor jeder Supersession ausgewertet. Ein
  gestrandeter 0-A-Intent darf nur anhand seines eigenen Intent-/ACK-Paars und
  eines frischen, zeitlich nachfolgenden 0-A-/0-W-Gerätereadbacks geschlossen
  werden. Dieser Recoverypfad sendet keinen neuen Hardwarebefehl. Mehrdeutige
  oder fremde Generationen bleiben gesperrt.
* **Phasen für das Budget:** Eine fehlende oder gesperrte Umschaltfreigabe
  bedeutet nicht, dass einphasig geladen wird. Strom und Mindestleistung
  werden mit der belegten Phasenzahl berechnet; ohne passende Evidenz bleibt
  die Berechnung konservativ. Drei Phasen benötigen bei 6 A und nominell
  230 V etwa 4,14 kW, nicht 1,38 kW. Ein bekannter einphasiger Fahrzeuglader
  oder eine bestätigte einphasige Last wird weiterhin einphasig behandelt.
  Zwei tatsächlich genutzte Phasen werden nicht auf eine Phase reduziert.
  Ein Auftrag, die Phasen unverändert zu lassen, löst keine Umschaltung aus.
* **Einphasig hinterlegte Fahrzeuge:** Ist für den Ladepunkt ein Fahrzeug mit
  einer Phase hinterlegt, schaltet E3DC-Control die Phasen der openWB Pro
  nicht um. Das Fahrzeug nutzt ohnehin nur eine Phase; ein Wechsel änderte
  seine Ladeleistung nicht, unterbräche aber die Verhandlung mit dem Fahrzeug
  und sperrte den nächsten Wechsel für 480 s. Die Box behält ihre Einstellung
  (eine oder drei Phasen). Mindestleistung und Budget richten sich nach dem
  Fahrzeug (6 A × 230 V = 1,38 kW), auch wenn die Box auf drei Phasen steht.
  Der einphasige Stromdeckel greift nach den gemessenen aktiven Phasen, also
  auch an einer Box im 3p-Modus. Ein einphasiges Fahrzeug an einem 3p-Ziel
  gilt nicht als laufender Phasenwechsel. Misst die Box dagegen drei aktive
  Phasen, etwa bei einem falsch hinterlegten Profil, gelten nach zwei
  Messungen für den Rest der Stecksession drei Phasen für Mindestleistung,
  Budget und Stromdeckel; reicht das Budget dafür nicht, endet die Ladung
  nach den Nullbudget-Regeln des Startfensters. Das Fahrzeugprofil sollte
  deshalb die tatsächliche Phasenzahl tragen.
* **CP-Interrupt nur als Weckruf:** `cp_interrupt=true` wird nicht für den
  normalen Phasenwechsel genutzt. Er ist ein gezielter Wakeup, wenn ein
  angestecktes Fahrzeug trotz freigegebener Leistung eingeschlafen ist.
* **Begrenzte Wake-up-Episode:** Je positiver Stromfreigabe sind ein bis drei
  Wake-up-Versuche konfigurierbar; Standard sind drei. Bei bewusst gewähltem
  Wert `1` darf bereits der erste vollständig belegte Versuch weitere
  automatische Starts derselben Stecksession sperren. Bei zwei oder drei
  Versuchen reicht ein einzelner Fehlversuch dafür nicht aus. Boolesche, nicht
  endliche und nicht ganzzahlige Werte sind ungültig und fallen auf drei
  Versuche zurück; Wake-up-Planung und Startablehnung verwenden denselben
  Parservertrag. Eine dauerhafte Startablehnung benötigt außerdem den
  typisierten Receipt der vollständig abgearbeiteten Episode. Stecksession,
  aktuelle Stromfreigabe und Zeitkette müssen exakt zusammenpassen.
* **Bewusster neuer Sofortauftrag:** Ein erneuter WebUI-Auftrag für `Sofort bis
  Preislimit` erhält eine eigene zufällige Kennung. Nur wenn dieselbe aktuelle
  Stecksession eine vollständig belegte Startablehnung erreicht hat, darf der
  Manager deren eigene Wake-up-Episode einmalig für diesen Nutzerauftrag neu
  öffnen. Der Auftrag selbst sendet keinen Gerätebefehl und ändert kein
  Budget. Preislimit, Nutzer-`Aus`, Not-Aus, Speicherreserve, Netzpunkt- und
  Hardwaregrenzen bleiben danach unverändert vorrangig.
* **Neustart der openWB Pro:** Startet die openWB Pro selbst neu, etwa nach
  einem Stromausfall oder einem manuellen Neustart, gilt bis zur ersten
  erfolgreichen Abfrage durch das EMS allein die Einstellung der Wallbox. Je
  nach Konfiguration beginnt sie sofort mit ihrem eigenen Ladestrom; im
  Werkszustand lädt sie laut Hersteller mit maximaler Leistung. Mit aktivem
  Heartbeat pausiert sie dagegen, bis sie wieder regelmäßig abgefragt wird.
  Das EMS sendet in dieser Zeit keine Befehle. Sobald es den Status wieder
  liest, übernimmt es eine laufende Ladung und regelt sie auf das aktuelle
  Budget. Kurzer Netzbezug oder Akkuentladung direkt nach einem Neustart der
  Wallbox geht deshalb auf die Wallbox zurück, nicht auf die Regelung. Die
  Absenkung auf das Budget (ab 6 A) folgt mit dem ersten frischen Status, auch
  während des Startfensters.

#### Phasenwechsel: Anteil, Freigabe und Wiederanlauf

Die Regelung wählt die Phasen aller Ladepunkte gemeinsam. Je Netzphase darf
die Summe der Wallboxströme höchstens Hausanschluss minus Wallbox-Reserve
betragen: 50 A mit 10 A Reserve ergeben 40 A. Die Hauslast wird zusätzlich
am Netzpunkt berücksichtigt: Erst werden die gemessenen Boxleistungen
abgezogen, danach erhält ausschließlich die verbleibende Hauslast die
Marge über den Leistungsfaktor. Für den Hausanschluss zählen ausschließlich die drei frischen, gültigen Phasenwerte des E3DC-Leistungsmessers. Darin sind Haus, Akku, Wärmepumpe und Wallboxen bereits enthalten. Die eigenen gemessenen Wallboxanteile werden genau einmal abgezogen; auf die verbleibende Last wirkt die Leistungsfaktormarge. Die Reserve begrenzt nur die Wallboxsumme: Der Rahmen ist der kleinere Wert aus Anschluss minus Reserve minus unbekannten Boxanteilen und Anschluss minus positiver gehaltener Hauslast. Die eingetragene Netzphase gilt wie eingetragen; eine abweichende Messung erscheint als Hinweis. Netzwert und Phasensumme werden für diesen Rahmen nicht gegeneinander geprüft.

Ohne gültige Phasenwerte warten neue Starts, Sofortladen, Fortsetzen und Phasenwechsel. Eine laufende Ladung nutzt während der vorhandenen Datenfrist den letzten gemessenen Hauswert unverändert und hält sofort Mindeststrom; nach 300 Sekunden endet sie. Nach dem Nullsetzen einer Phasenfolge bleibt die Box ohne gültige Phasenwerte auf 0 A. Elektrische Grenzen und Nutzerstopps wirken weiterhin sofort. Falsche, aber frische und gültige Zählerwerte oder eine falsch eingetragene Netzphase können zu einer dauerhaften Überschreitung führen. Ein fremder Lastsprung in der Datenlücke bleibt bis zum Fristende unbemerkt.

Meldet eine Wallbox keine Phasenströme, rechnet E3DC-Control ihren Strom aus der gemessenen Leistung mit derselben Sicherheitsmarge wie die übrige Hauslast. Die Stromschätzung gilt für U·cos φ ≥ 207 V (Spannung mal Leistungsfaktor, bei Marge 0,9). Wallboxen ohne eigene Phasenstrommessung behalten auch mit eingetragener Netzphase diesen Abstand. Derselbe Abstand gilt bereits bei der Verteilung des Zielstroms, damit der verfügbare Raum beim Start gleich bleibt. Steigt eine andere Last im Haus sprunghaft,
regelt E3DC-Control die Wallbox im nächsten Regeltakt herunter; für diesen
einen Takt kann der Netzstrom über dem eingestellten Hausanschluss liegen.
Mit `f` als Leistungsfaktor-Marge gilt je Phase:
`Haus = (Netzwirkleistung − eigene Boxwirkleistung) / (230 V × f)` und
`Rahmen = min(Hausanschluss − Reserve − U, Hausanschluss − max(0, Haus))`.
`U` ist der Hardwarehöchststrom veralteter Boxen, deren Leistung im Hauswert
bleibt. Bis zur Stromquittung bleibt der höhere Hauswert erhalten.
Ist die Netzphase einer ein- oder zweiphasig ladenden Wallbox nicht
eingetragen, rechnet E3DC-Control ihren Strom mit Sicherheitsabstand:
Die elektrische Belegung beträgt Zielstrom geteilt durch `f` auf allen
möglichen Phasen. Sobald eine Phase die Grenze erreicht, wird die Box
abgesenkt. Mit eingetragener Netzphase wird nur die Wallbox auf der
betroffenen Phase abgesenkt; mit eigener Phasenstrommessung werden Sicherung und Hausanschluss voll genutzt.
Bei 40 A zulässiger Boxsumme bleiben für eine unbekannte Box mit `f = 0,9`
höchstens 36 A. Auf dem Dashboard und auf der Seite „Wallbox“
erscheint bei fehlender Netzphase ein Hinweis mit dem Feldnamen im
Config-Editor. Der Hinweis verschwindet, sobald die Zuordnung eingetragen
ist oder die Box nicht mehr ein- oder zweiphasig lädt.
Ein neues Aufstiegsziel muss 60 Sekunden, ein Abstiegsziel 60 Sekunden
bestehen; nach einem Wechsel gelten mindestens 480 Sekunden Sperre.
Laden zwei Boxen einphasig auf derselben oder einer unbekannten Netzphase,
teilen sie deren Leistung entsprechend dem eingestellten Vorrang. Bei
bekannter gleicher Netzphase beschreibt die Meldung diese Aufteilung.


Die gemeinsame Wahl bewertet die energetisch und elektrisch nutzbaren Anteile aller Ladepunkte.
Auch eine auf 16 A begrenzte Wallbox kann bei ausreichendem dreiphasigem
Anteil aufsteigen. Der elektrische Zuteiler reserviert die
betroffenen Netzphasen. Die bestehende Freigabe des Speicherreglers und die
Hardware-Sequenz bleiben erforderlich: Strom auf 0 A, bestätigtes Phasenziel,
Wiederanlauf auf der bestätigten Zielphase. Ein nicht ausführbarer oder
unbestätigter Auftrag gibt keinen Strom frei.

Die openWB Pro erhält ausschließlich die bestehenden Befehle über
`connect.php`. Die CP-Unterbrechung bleibt Teil der Gerätesequenz. Nach einem
bestätigten Wechsel sperrt die vorhandene Hardwarefrist von mindestens
480 Sekunden einen weiteren Wechsel; sie ist keine zusätzliche Wartefrist
für das bestätigte Fortsetzen auf derselben Phase. Fahrzeug- und
Gerätefähigkeiten begrenzen die zulässigen Ziele.

#### Start und Fahrzeugbereitschaft

Nach 60 Sekunden ausreichendem, gültigem PV-Anteil folgt genau ein
Startangebot. Eine freigegebene Netzquelle startet ohne diese Wartezeit. Ein eigener Ladeplan oder Standby des Fahrzeugs erzeugt keine
wiederholten Startbefehle. Spätestens 30 Sekunden danach nennt der
Entscheidungssatz den fehlenden Ladebeginn. Beginnt das Fahrzeug später
selbst zu laden, wird dieselbe Sitzung übernommen. Bei fortgesetzter
Unterdeckung gilt der Sitzungsautomat mit autorisierter kurzer Stützung,
drei Minuten Unterdeckung und mindestens fünf Minuten Ladezeit. Nach einem
regulären Stopp benötigt ein neuer Start erneut 60 Sekunden ausreichenden Anteil. Harte Schranken
wirken sofort; ein Ladefenster endet im ersten folgenden Zyklus mit 0 W.
Ein zusätzlicher Anhebungsvorlauf, ein Export-Wh-Konto und ein wiederholtes
Wecken gehören nicht zu diesem Ablauf.

#### Verhalten nach dem Abstecken

„Sicherheitsvariante“ (`safe`, Standard) setzt beim ersten frischen,
widerspruchsfreien Absteckbeleg 0 A. Beim Anstecken beginnt die Ladung erst nach einer Freigabe.

„Schneller Start“ (`fast_start`) hält 6 A bereit, solange der gewählte Modus
gerade einen Start erlaubt: Netzladen ist freigegeben oder das verbleibende
Budget nach den zugeteilten Fahrzeugen trägt den Mindeststrom seit 60 Sekunden.
Der Hausanschluss muss auf jeder benötigten Phase ausreichend Raum bieten;
Messdaten und Reservefreigabe müssen gültig sein. Sonst gilt 0 A. Das Angebot
kann über den Tag wechseln. Bei Budgetunterdeckung wird es nach den vorhandenen
180 Sekunden zurückgenommen; Datenfehler, Hausanschlussgrenze, Aus, Pause,
Sperre und Reserve nehmen eine Freigabe sofort zurück. Im Beobachtungsmodus
erfolgt keine aktive Freigabe.

Die Regelung setzt das elektrische Ziel schon beim ersten frischen,
widerspruchsfreien Absteckbeleg über den bestehenden Befehlsweg mit Rückmeldung.
Die längere Bestätigung des Sitzungsendes bleibt davon unabhängig.
Das quittierte elektrische Angebot beendet die vorherige Ladeepisode.
Beim Wiederanstecken ohne positives Angebot gilt die normale neue
Startqualifizierung, statt die alte Ladung fortzusetzen. Bis zum
ersten frischen Frame kann ein sofort wieder angestecktes Fahrzeug noch den
alten Sollstrom sehen. Eine bereits laufende Ladung übernimmt der Kern im
nächsten Takt und begrenzt sie auf den gültigen Rahmen.
Not-Aus, Pause und Ladepunktsperre nehmen ein stehendes positives Angebot
sofort zurück; eine frühere Nullquittung gilt nach einem positiven Ausgang
nicht mehr. Das Angebot belegt elektrischen Raum gemeinsam mit den neuen
Fahrstromzielen desselben Takts. Es erhält keinen Energieanteil.
Das Reserve-Tor gilt für Netzladung und Netzangebote, bleibt nach einem
Dienstneustart geschlossen und öffnet erst ab Reserve plus 2 Prozentpunkten
(Standardwerte). Ein ausreichender PV-Anteil bleibt zulässig.
Ein unverändertes, zurückgemeldetes Ziel erzeugt
keinen weiteren Strombefehl. Eine abweichende Rückmeldung wird frühestens nach
20 Sekunden erneut geprüft; je Box und Zyklus gibt es höchstens einen Befehl.
Es gibt dabei keine Phasenbefehle und keine CP-Unterbrechung.

Liegt beim Anstecken das 6-A-Angebot an, wird die laufende Ladung im folgenden
Takt übernommen: „Schneller Start nach dem Anstecken“. Eine zweite
Startqualifizierung entfällt. Bei 0 A gilt der normale Startpfad.
Der Schlüssel `wb<n>_openwb_pro_unplug_offer` bleibt erhalten. Fehlende oder
ungültige Werte bedeuten `safe`. Nach einem Dienstneustart wird zuerst die
Rückmeldung der Box geprüft.

- Diagnose: `wb_details[].openwb_pro_unplug_offer` mit `mode`, `target_amp`,
  `stage`, `sends`, `sent`, `confirmed`, `reported_amp` und `reason`.

#### Einphasiger Stromdeckel aus Netzphasenmessung

Ohne phasenaufgelöste Strommessung am Netzpunkt bleibt einphasiges Laden an
einer openWB Pro fest auf 20 A gedeckelt. Freigegebene Messbasis für einen
höheren Deckel ist der Bezugsstrom je Netzphase aus dem E3DC-Wurzelzähler:
Wirkleistung je Phase (`grid_p1..3`, nur Bezug, Einspeisung zählt nie als
Spielraum) geteilt durch die vom Wechselrichter gemessene Phasenspannung
(`ac0..2_v`, plausibel 180–260 V und jünger als 10 s, sonst 230 V) plus der
von der openWB Pro gemessene Wallbox-Strom.

* **Gemeinsamer Phasenrahmen:** Vom Netzpunktwert wird zuerst die gemessene
  Wirkleistung aller frischen Boxen abgezogen. Nur die verbleibende Hauslast
  wird mit der Leistungsfaktor-Marge hochgerechnet. Der Pro-Deckel liest
  denselben Rest und zieht die belegten Ströme der frischen anderen Boxen ab;
  eine zweite Marge wird nicht angewandt. Unbekannte Phasenlagen belegen alle
  möglichen Phasen. Veraltete Boxen bleiben konservativ mit ihrem
  Hardwaremaximum im Rahmen berücksichtigt.
* **Harte Schranken:** Nutzergrenze `wb<n>_openwb_pro_1p_max_amp`,
  Wallboxgrenze und physischer Phasenrest. Die gemeinsame Boxsumme bleibt
  außerdem unter Hausabsicherung minus Reserve. Ganze Ampere, unter 6 A
  → 0 A (Wallbox pausiert, Grund `phase_headroom_exhausted`).
* **Schieflast-Wächter** (k = zugeordnete Netzphase, m = Bezug je Phase in A,
  I_wb = gemessener Wallbox-Strom): `cap_imb = max(20 A, I_wb + (grid_pcc_imbalance_max_a −
  (m_k − min_{j≠k} m_j)))` (Standard 20 A ≙ 4,6 kVA). Bewertet wird die
  Differenz der Bezugsströme am Netzpunkt, Einspeisung zählt als 0 A – nicht
  die vorzeichenbehaftete Differenz der Phasenleistungen. Ohne jeden Ausgleich
  (leerer Akku, keine PV) bleibt es bei 20 A. Entlädt oder speist das E3DC
  dagegen symmetrisch über alle drei Phasen, sinkt der Bezug der Ladephase,
  während die einspeisenden Nachbarphasen mit 0 A zählen: Der Deckel steigt
  dann nachts auf etwa 28–29 A und tagsüber je nach Einspeisung bis zur
  eingestellten Grenze. Mehr als 20 A entstehen auch, wenn beide
  Nachbarphasen mehr Bezug haben als die Ladephase ohne Wallbox-Strom
  (einphasige Verbraucher). Gleichmäßige dreiphasige Last (3p-Wallbox,
  Wärmepumpe) hebt den Deckel nicht an, solange die Ladephase Bezug hat;
  speisen die Nachbarphasen ein, kann sie ihn sogar senken. Wer einphasig
  strikt bei 20 A bleiben muss, lässt `wb<n>_openwb_pro_1p_max_amp` leer.
* **Fail-closed (20 A bzw. Nutzer-/Wallboxgrenze darunter):** Messbasis
  `wb_pcc_phase_basis = off`, fehlende/widersprüchliche Zuordnung, nur der
  stille Standard 35 A als Hausabsicherung (`grid_limit_not_explicit`),
  ungültiges Limit/Reserve, ungültige oder
  über 10 s alte Netzphasen-Messung (`_ts`), ungültiger/über 10 s alter
  openWB-Status, kein Live-Snapshot im Zyklus (`pcc_measurement_basis_missing`),
  Ausnahme im Vertrag (`contract_exception`).
* **Rampe:** Anhebung +1 A je Regelschritt (`wb_stable_follow_hold_s`,
  mindestens 4 s) und nur bei Headroom ≥ 1,5 A; Absenkung sofort, danach
  dreifacher Nachlauf ohne Anhebung. Neue Stecksession, bestätigter
  Phasenwechsel und jeder Fail-closed-Zyklus starten wieder bei 20 A
  (20 → 32 A in rund 48–60 s).
* **Software-Nachweis der Zuordnung:** Die eingetragene Netzphase gilt wie eingetragen. Der Software-Nachweis und eine Abweichung zwischen Netzwert und Phasensumme liefern Hinweise; sie begrenzen den Strom nicht. E3DC-Control prüft
  `wb<n>_grid_phase` beim einphasigen Laden zur Diagnose. Aus Plateaus des
  Wallbox-Stroms (≥ 3 Stichproben über ≥ 10 s, Spannweite ≤ 0,5 A) prüft es
  statisch `P_load_k / (I_wb · U_k) ≥ 0,8` (ab 8 A) und an Stromsprüngen
  ≥ 3 A, ob die Laständerung `ΔP_j / (ΔI · U_j)` auf der zugeordneten Phase
  erscheint (0,6–1,4) und auf den anderen nicht (< 0,5). Ladestart/-stopp
  (Sprung ≥ 10 A) zählt doppelt. `verified` verlangt Statik ok und Sprünge
  mit Gewicht ≥ 2 (mehr als das Doppelte der Widersprüche); `unverified`
  entsteht nur aus Widersprüchen auf ein und derselben Fremdphase (Gewicht ≥ 2
  oder ein Widerspruch plus 60 s Statik-Fehlschlag mit Trägerphase) und
  bleibt bis zu einer Konfigurationsänderung bestehen (Hinweis auf Dashboard und Seite „Wallbox“:
  „Eingetragene Netzphase passt nicht zur Messung – Zuordnung prüfen“; den Strom begrenzt er nicht). Ein reiner
  Statik-Fehlschlag (z. B. externer AC-Zusatz-Wechselrichter auf der Phase)
  bleibt `pending`. Der Nachweis liegt in
  `data/wallbox_phase_mapping_state.json` (Schema
  `wallbox_phase_mapping_proof_v1`, höchstens fünf Sprünge, überlebt Neustart
  und neue Stecksession); Ändern von `wb<n>_grid_phase`/`_rotation` oder
  Löschen der Datei setzt ihn zurück. Die Phasenlast ist die signierte Bilanz
  `max(0, grid_p_j + ac_j_w)`, damit Einspeisung auf einer Fremdphase keinen
  falschen Nachweis liefert.
* **Geltung im 3p-Modus:** Der Deckel gilt für jeden 1p-Sollwert und
  zusätzlich, wenn der Treiber nur eine aktive Phase meldet (einphasiges
  Fahrzeug an einer Box im 3p-Modus). Drei gemessene und aktive Phasen heben
  ihn auf.
* **Energie-Phasenpolitik:** Der laufende Strom und die Phasenbewertung
  folgen dem zugeteilten Leistungsanteil und dem wirksamen Geräte- und
  Fahrzeugdeckel. Aufwärts gelten 60 Sekunden Deckung, abwärts 60 Sekunden
  Unterdeckung und nach einem Wechsel 480 Sekunden Phasensperre.
* **Gruppenverteiler:** Der Zuteilungs-Slot eines 1p-only-Fahrzeugs an einer
  openWB Pro ist höchstens der wirksame Deckel (20 A → 4,6 kW statt 7,36 kW);
  phasenfähige Fahrzeuge bleiben unverändert.
* **Diagnose:** `wb_details[].openwb_pro_one_phase_cap_contract` (Deckel,
  Rohdeckel, Grund, Bezugsvektor, Spannungen, Fremdlast, Headroom, Schieflast,
  Rampe, Frische, Zuordnung), `openwb_pro_phase_mapping_proof` (Zustand,
  Statik, Sprünge, Evidenz), `openwb_pro_one_phase_cap_ramp` und
  `phase_energy_policy.one_phase_max_source` in `ramdisk/wallbox_native.json`
  und `wallbox_decision_latest.json`; Dashboard-Kurzzeile je Slot, z. B.
  „1p 27 A · L3 bestätigt“ (während der Rampe „1p 26 A → 32 A · L3
  bestätigt“, am Schieflast-Wächter „1p 20 A · Schieflast · L3 bestätigt“,
  Zuordnung „wird geprüft“ oder rot „widerlegt“). Die Einzelheiten stehen im
  Tooltip der Zeile mit Messbasis, Spannung, Zuordnung und Schieflast.
  Hauslast und Wallbox-Reserve werden im gemeinsamen Phasenrahmen
  berücksichtigt; die Reserve wird nicht zusätzlich von der Hauslast abgezogen.
* **Konfiguration:** `wb_pcc_phase_basis` (Standard `e3dc_pm_active_power`,
  `off` = fest 20 A), `wb_pcc_power_factor_margin` (0,8–1,0, Standard 0,9),
  `grid_pcc_imbalance_max_a` (10–32 A, Standard 20) im Config-Editor unter
  Wallbox → „Hausanschluss & gemeinsame Grenzen“ → „Einphasiger Deckel openWB
  Pro (Messbasis)“; Voraussetzung ist eine ausdrücklich eingetragene
  `grid_max_amps` (oder `grid_max_amps_l<k>`). Leere Phasenfelder
  `grid_max_amps_l<k>` / `grid_wallbox_reserve_amps_l<k>` (der Editor
  speichert sie als leeren Text) gelten wie beim Validator als nicht gesetzt
  – der Skalar gilt; nur ein nicht numerischer Eintrag sperrt den Deckel
  (`grid_contract_invalid`).

### openWB Software 2.x als Primary

Wenn die openWB Software selbst eine Pro, einen Satelliten oder eine
Fremdwallbox als Primary regelt, bleibt openWB der Energiemanager. E3DC-Control
liest Leistung und Status, bereinigt den Hausverbrauch und kann mit bewusst
aktiviertem `openWB Primary` nur den openWB-eigenen Modus wechseln:

```text
Moduswechsel / eigener Auftrag beendet / Fremdpause aufgehoben -> set_chargemode=pv
Aktiver Eingriff -> chargecurrent=<Ampere>, set_chargemode=instant
Schutz/Stop      -> set_chargemode=stop
```

In den PV-Modi setzt E3DC-Control PV genau einmal beim bewussten Moduswechsel,
bei der Rücknahme eines eigenen Stopp-/Sofortauftrags oder nach aufgehobener
importierter Fremdpause, auch nach Abstecken und Wiederanstecken. Während
Pause, Not-Aus oder Ladepunktsperre erfolgt keine PV-Rückgabe. Eine Nutzerwahl
an der openWB ohne solche Aktion bleibt bestehen. Der Speicher hält seinen Kurvenrahmen; seine
Entladegrenze schützt in `PV-Kurve ruhig` Haus und Wärmepumpe und gibt in
`PV + Akku` oberhalb von `wbminsoc` Akkustützung frei. Die Ladepriorität
**Speicher** auf der openWB berücksichtigt die Ladekurve. Akkustrom für das
Auto muss die openWB selbst erlauben, da sie Speicherentladung vom Überschuss
abzieht. Für volle Stromregelung durch E3DC-Control ist die openWB Software
als **Secondary** einzurichten.

Dieser Pfad ist ein Opt-in und kein automatischer Default. Besonders bei einer
extern eingebundenen openWB Pro ist er nicht als Pro-Fernsteuerung gedacht. Wenn
E3DC-Control die Pro wirklich führen soll, die Pro direkt als
`openWB Pro (connect.php)` konfigurieren.

Die openWB-Karte zeigt den von openWB gemeldeten Lademodus und ihre
Statusmeldung mit dem Alter seit dem letzten Lesen. Fehlende Statusmeldungen
bleiben leer. Steht E3DC-Control auf „PV-Kurve ruhig“ oder „PV + Akku bis Untergrenze“
und meldet die openWB „Sofort“ oder „Stop“, zeigt die Karte beide Modi nebeneinander.
Aus der Rückmeldung lässt sich nicht ableiten, wer den Modus gewählt hat:
Auch E3DC-Control kann im Primary-Betrieb „Stop“ oder „Sofort“ setzen.
Bei ausgefallener Statusabfrage bleibt der gemeldete Lademodus unbekannt.
Die Anzeige ist keine zusätzliche Ladefreigabe und sendet keine Befehle.

Unter „Ladepriorität und openWB-Details“ stehen die gelesene Ladepriorität,
Softwareversion und Abschaltverzögerung, soweit verfügbar. Die Empfangszeit der
Ladepriorität ist kein Änderungszeitpunkt: Nach einer Verbindung kann ein
gespeicherter Stand erneut geliefert werden. Unbekannte Werte werden nicht geschätzt.

Als Primary mit Ladepriorität „Fahrzeuge“ zählt die openWB auch die Leistung,
mit der der Hausakku lädt, als Überschuss für das Auto. In „PV-Kurve ruhig“ und
„PV + Akku bis Untergrenze“ erscheint deshalb ein beratender Hinweis: Die Ladekurve des Akkus
kann so nicht gehalten werden. Empfehlung: In den Einstellungen der openWB die
Ladepriorität „Speicher“ wählen. Bei „Nach SoC des Speichers“ weist die Karte auf
die eigenen SoC-Schwellen der openWB hin. Bei unbekannter Ladepriorität gibt es
keine Warnung. E3DC-Control liest diese Einstellung ausschließlich und schreibt
sie niemals; die Hinweise sperren nichts und ändern die Regelung nicht.

Bei belegter Primary-Rolle und „PV-Kurve ruhig“ ergänzt die Karte einen Hinweis,
wenn die openWB „Nach SoC des Speichers“ sowie eine aktivierte Fahrzeugentladung
mit positiver Leistung meldet. Die obere SoC-Schwelle eröffnet die Entladung;
nach Erreichen dieser Schwelle kann sie bis zur unteren SoC-Schwelle gelten.
Die Karte nennt beide Schwellen und die konfigurierte Leistung, keine gemessene
Entladung. Empfehlung: Ladepriorität „Speicher“. Bei gewünschter Akkustützung
kommt „PV + Akku bis Untergrenze“ infrage; Untergrenze und Schutzgrenzen gelten weiter.

Meldet das aktuell gebundene Ladeprofil „Sofort“ oder „Zielladen“, erscheint bei
einer frisch beobachteten, stabilen Ansteckepisode einmal ein Hinweis auf die
selbstständige Ladung der Primary-openWB. E3DC-Control stellt beim bewussten
Moduswechsel, bei der Rücknahme eigener Stopp- bzw. Sofortaufträge oder nach
aufgehobener importierter Fremdpause auf PV um, auch nach Abstecken und Wiederanstecken.
Reload und Reconnect erzeugen keine neue Episode. Beim Dienststart mit bereits
gestecktem Fahrzeug wird kein Anstecken angenommen. „Aus“, fehlende Daten,
Profil-/Fahrzeugwechsel und veraltete Daten entfernen den Hinweis.

Die Hinweise stehen auch unter „Ladepriorität und openWB-Details“. Der Profilhinweis
wird beim Reload gemeinsam mit dem Warnkasten unterdrückt; das Ladeprofil kann dort
weiterhin neutral als Zustand erscheinen.

Messwerte müssen jünger als 30 Sekunden sein. Einstellungen und Fahrzeugzuordnung
zählen als aktueller Stand, wenn sie innerhalb der ununterbrochenen MQTT-Sitzung
empfangen wurden und vollständig sowie widerspruchsfrei sind. Gespeicherte
Wiederzustellungen zählen ebenfalls; ein Sitzungsabbruch verwirft den Stand bis zur
Wiederzustellung. Zusätzlich muss ein vom openWB-Regelkern nach jeder Berechnung
erzeugter Wert jünger als 30 Sekunden sein. Die Erreichbarkeit des Brokers allein
genügt nicht. Beim Anstecken zählt auch der bereits davor empfangene aktuelle Stand.
Neuanstecken desselben Fahrzeugs mit unverändertem Profil beginnt eine neue Episode.
Ältere openWB-Versionen ohne die benannten Themen liefern keinen solchen Hinweis.

### openWB Software 2.x als Secondary

Für openWB Software 2.x im Secondary-Betrieb gilt der offizielle Secondary-Pfad.
Die Modbus-Dokumentation nennt Port `1502`, Slave-ID `1`, `10171` für den
LP1-Sollstrom und `10190` für den Heartbeat:

```text
openWB/set/internal_chargepoint/global_data
openWB/set/internal_chargepoint/<duo_num>/data/set_current
```

Der Heartbeat muss zyklisch geschrieben werden; openWB stoppt sonst die Ladung.
`duo_num` ist die lokale Nummer des internen Ladepunkts, nicht zwingend die
sichtbare `chargepoint/<id>`-Nummer im openWB-Primary-System.

Quellen zur Einordnung:

- evcc dokumentiert openWB Software 2.x mit `Steuerungsmodus: secondary` und
  `Steuerung über Modbus als secondary: An`:
  <https://docs.evcc.io/docs/devices/chargers>
- openWB beschreibt `primary` als steuerndes System und `secondary` als
  gesteuertes System:
  <https://wiki.openwb.de/doku.php?id=openwb%3Avc%3A2.1.9%3Asoftware%3Aeinstell-konfig%3Aeinstellungen%3Aallgemein>
- Die openWB simpleAPI dokumentiert Lademodus-Setzen (`instant`, `pv`, `eco`,
  `stop`, `target`) und Sofortladestrom:
  <https://wiki.openwb.de/doku.php?id=openwb%3Avc%3A2.2.0%3Asimpleapi>
- Die openWB-Modbus-Rev2.0-Dokumentation nennt Sollstrom, Phasen, Heartbeat und
  Port `1502`:
  <https://openwb.de/main/wp-content/uploads/2023/10/ModbusTCP-openWB-series2-Pro-1.pdf>

Direkte evcc/openWB-Messwerte können zusätzlich über den MQTT-Hub eingetragen
werden:

```ini
wb_ip = 192.0.2.126:1883
wb_topic = evcc/loadpoints/1/chargePower
```

## go-e

Die go-e Wallbox wird direkt über die HTTP-API v2 gelesen und gesteuert:

```text
http://<ip>/api/status
```

In der go-e App muss **HTTP-API (v2) erlaubt** sein.

## Hausverbrauch und Statistik

Externe Wallboxleistung wird vom reinen Hausverbrauch abgezogen. Dadurch landen
evcc/openWB/go-e-Leistungen nicht doppelt als Hausverbrauch und Wallboxverbrauch
in Dashboard, Historie und Planung. Bei zwei Wallboxen werden WB1 und WB2
getrennt ausgewiesen.

Die Anzeige des Hausverbrauchs verrechnet den E3DC-Hauswert nur mit einer
zeitgleichen Messung der Fremd-Wallbox: Die Wallboxleistung muss über den
ganzen E3DC-Messzyklus belegt gleich gewesen sein. Direkt nach einer
Leistungsänderung der Wallbox bleibt der letzte zeitgleich gebildete Wert
stehen (höchstens 60 Sekunden), statt einen Wert aus Messungen verschiedener
Zeitpunkte zu zeigen. Liefert die Wallbox keine gültige Messung mit
Zeitstempel, bleibt es bei der bisherigen Verrechnung. Die Regelung ist davon
nicht betroffen.

## Docker

Im Docker startet der Wallbox-Manager mit dem Container. Wenn
Wallbox-Funktionen nachträglich aktiviert oder deaktiviert werden, den Container
mit dem vorhandenen Image neu erstellen und vollständig prüfen:

```bash
cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
sudo python3 ./Installer/docker_compose_update.py \
  --compose-dir . --sudo --recreate-current
sudo docker compose logs --tail=80 e3dc-control
```

Für ein Image-Update wird derselbe Host-Helfer ohne `--recreate-current`
aufgerufen. Er zieht und bindet dann zuerst das neue Image.

## Troubleshooting

### Wallbox wird nicht angezeigt

Die UI blendet deaktivierte Wallboxen aus. Setze einen Wallbox-Typ, eine IP,
ein Shelly-Feld oder ein direktes MQTT-Leistungstopic.

### evcc-Leistung kommt per MQTT, Dashboard zeigt aber 0 W

Prüfe, ob das Leistungs-Topic im Bereich **Wallbox-Leistung per MQTT** steht:

```text
wb_topic = evcc/loadpoints/1/chargePower
```

`chargePower` gehört nicht in `mqtt_hub_sub_soc_topic`.

### E3DC steht nach `Aus` noch auf 6 A

Ab 5.0.0 gilt NGNA: `Aus` sendet keine wiederholten Korrekturen mehr an die
Wallbox. Wenn du in der Wallbox-WebUI bewusst von einem aktiven Modus auf `Aus`
wechselst, gibt E3DC-Control die Wallbox einmalig auf die konfigurierte
Grundeinstellung frei, z.B. 32 A und PV/Sonnenmodus bei E3DC. Danach wird nur
noch beobachtet, bis wieder ein aktiver Modus gewählt wird. Ein Neustart, ein
kurzer Verbindungsverlust oder "kein Fahrzeug verbunden" löst keine erneute
Freigabe aus.

### Logzeilen im Wallbox-Manager lesen

| Meldung | Bedeutung | Handlung |
|---|---|---|
| `WB1 START: 0A -> 6A` / `Deckel: 6A -> 32A` | Freigabe gesendet, Stromdeckel gesetzt. | Keine; echtes Laden zeigt erst der Status „Lädt“ mit Leistung. |
| `Strombefehl wiederholt: 32A bei 0W` | Freigabe steht, das Fahrzeug nimmt keinen Strom an. | Fahrzeug schläft oder hat die Session beendet; E3DC kennt keinen CP-Interrupt über RSCP, meist hilft nur Neustecken. |
| `Netzladefenster beendet: Wallbox gestoppt` | Geplanter Slot vorbei, Stop-Toggle gesendet. | Keine. |
| `Netzladefenster beendet: Fahrzeug hat im Fenster nicht geladen …` | Freigabe stand, es wurde nie geladen; Freigabe auf Mindeststrom ohne Toggle zurückgenommen. | Keine; ein schlafendes Fahrzeug wird nicht angestoßen. |
| `Flankengate: stop_toggle_downgraded` | Stopp angefordert; ohne frischen freigegebenen Boxzustand und ohne zulässigen Ladebeleg-Rückfall bleibt die Flanke aus. | Zustand und Verbindung der Box prüfen. |
| `wb_pv_budget.json veraltet (Ns) – drossle WB auf 6A` | Storage Manager hat 15–45 s nichts geschrieben. | Storage-Manager-Journal prüfen, wenn es sich häuft. |
| `wb_pv_budget.json Timeout (Ns) – stoppe WB` | Storage Manager > 45 s ohne Budgetdatei. | Storage Manager läuft nicht oder hängt; Dienst prüfen. |
| `wb_pv_budget.json Lesefehler überbrückt (…)` | Lesezugriff traf den atomaren Austausch; der letzte gültige Stand zählt weiter. | Keine; nur die Häufigkeit ist ein Hinweis auf stark ausgelastete SD-Karte/CPU. |
| `wb_pv_budget.json nicht lesbar und kein gültiger Vorstand` | Datei seit Prozessstart nie gültig gelesen. | Ramdisk und Storage Manager prüfen. |
| `Plan verbraucht` / `Ladeplanung auf 0 gesetzt` | Manueller Ladeplan ohne Wiederholung ist abgearbeitet. | Bei täglichem Bedarf „Täglich wiederholen“ einschalten. |

Der Eigenbefehl-Beleg bleibt auch über Ab- und Wiederanstecken erhalten,
höchstens sieben Tage und solange die openWB den eigenen Modus meldet.
Eine leere, beschädigte oder schemafremde Belegdatei wird mit Warnung in
Quarantäne verschoben. Ohne gültigen Beleg erfolgt kein Eingriff; andere
Ladepunkte werden dadurch nicht gesperrt. Die Rücknahme auf PV erfolgt
auch ohne Fahrzeug, aber niemals im Modus `Aus`. Bestätigtes PV oder eine fremde Moduswahl beendet den
Beleg; die persistente Datei wird nur bei Änderungen geschrieben.

Die Korridorrest- und Messlückenregeln stehen im Abschnitt „Abendziel bei
PV + Akku“ in [Ladekurve_Berechnung.md](Ladekurve_Berechnung.md).


## Startknopf und Wiederanlauf nach Kaskaden-Stop

Der Startknopf erhält den gewählten Lademodus und betrifft die ausgewählte
Wallbox. Bei „Aus / autonom“ entsteht kein Startbefehl. Ein angenommener
Start löst eigene Ladeende-, Untergrenzen- und Kaskadenanker derselben
Stecksession. Nach Kontingent-Ende oder gescheitertem Phasenabstieg gilt
derselbe Vertrag. Fremde oder geschützte Stopps bleiben erhalten.

Ein PV-Start benötigt 60 Sekunden ausreichenden, gültigen Quellenanteil.
Eine freigegebene Netzquelle startet ohne diese Wartezeit. Ein neuer Kaskaden-Stopp eröffnet eine neue Episode.
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

### Stromauflösung und Phasenfortsetzung

Das Geräteraster (`wb1_current_step_amp`, `wb2_current_step_amp`) bleibt bis zum
Befehl erhalten; alle elektrischen Deckel werden abwärts gerundet. Der Kern
setzt den Anteil mit 1-A-Totband direkt um. Nach einer Umschaltung wird dieselbe
Sitzung erst mit frischem Phasenreadback und bestehender Sequenz fortgesetzt.
Leases, Sessionbindung und Geräte-Cooldowns bleiben erhalten.

## Wallbox-Regelkern

Jeder konfigurierte, nicht deaktivierte Ladepunkt verwendet denselben
Regelkern. Die Betriebsart bleibt je Ladepunkt eingestellt; eine zusätzliche
Reglerauswahl gibt es nicht. Der Rückweg nach einem Update ist das Backup.

Der Speicher veröffentlicht das absolute Gruppenbudget in zwei Teilen:
`budget_pv_w` aus PV nach festen Lasten und seinem eigenen Ladeanspruch sowie
`budget_battery_w` als Rest von `budget_w`. Laufende Akkuladung und die eigene
Wallboxleistung werden davon nicht erneut abgezogen. Externe Wallboxen stecken
im Hauswert, native E3DC-Wallboxen haben einen eigenen Messpunkt.

Der Speicher-Ladeanspruch wird auch vor dem Wallboxstart und während einer
leistungsfreien Phasenumschaltung berücksichtigt. Er folgt dem tatsächlich
vorgesehenen Laderahmen einschließlich dessen Mindest- und Nullregeln;
oberhalb des Speicherziels ist er ohne weiteren Ladebedarf 0 W. Fehlende
Speicherdaten ergeben einen unbekannten PV-Teil mit Begründung. Erst wenn der
verbleibende PV-Anteil die Mindestleistung erreicht, kann eine PV-Box starten.
Ein echter Wechsel des Laderahmens darf den Strom über das bestehende
1-A-Totband nachführen; die eigene Wallboxlast erzeugt keinen neuen Anspruch.

Je Ladepunkt und Zyklus gilt eine Anteilsquelle: Netz bei ausdrücklicher
Netzfreigabe; Akku in „PV + Akku bis Untergrenze“, außerhalb des Netzfensters
in „Sofort bis Preislimit“ und im Abfahrtsfenster in „Akku bis Abfahrt“, jeweils
mit gültigem SoC, offener Untergrenze und Speicherautorisierung; sonst PV.
Netzboxen erhalten ihren zulässigen Höchststrom ohne Verbrauch des
Speicherbudgets. Akkuboxen teilen zuerst das Gesamtbudget; PV-Boxen bekommen
den PV-Rest nach diesen Anteilen. Innerhalb einer Quelle gilt die eingestellte
Verteilpriorität. Reicht der Anteil nur für eine bereits ladende Box, behält
die Vorrangbox, sonst die länger ladende Sitzung ihren Anteil. Ein Rest unter
der Mindestleistung geht an den nächsten geeigneten Ladepunkt.

Der elektrische Zuteiler prüft danach Geräte- und Fahrzeuggrenzen,
Phasenlage und Hausanschluss je Phase. Im 10-Sekunden-Takt folgt der Strom
direkt dem Anteil, abwärts auf das Geräteraster gerundet. Ein Totband von 1 A
verhindert Änderungen bei kleinen Budgetschwankungen; es gibt keine
Anhebungstreppe und keine zusätzliche Bestätigungszeit.

Ein PV-Start braucht 60 Sekunden ausreichenden Anteil. Eine freigegebene
Netzquelle startet ohne diese Wartezeit; gültige Daten, Hausanschluss,
Nutzer-Aus und Reserve gelten weiterhin vorrangig. Kurze Unterdeckung führt
zum Mindeststrom, solange Stützung autorisiert ist: bei „PV-Kurve ruhig“ nur
aus dem Wolken-Kontingent, bei „Grundladung stabil“ nur solange die bestehende
Grundladungsbedingung gilt, bei Akkubetriebsarten nur oberhalb der Untergrenze.
Haltestützung erlaubt keinen Start aus fehlender Quelle. Nach drei Minuten
Unterdeckung und mindestens fünf Minuten Ladung endet die Sitzung regulär;
danach benötigt ein PV-Start erneut 60 Sekunden ausreichenden Anteil.
Eine freigegebene Netzquelle startet ohne diese Wartezeit. Diese Unterdeckungsregel gilt
auch ohne Stützungsfreigabe. Für alle Ladungen zählt die Mindestladezeit ab
der ersten gültigen Ladeprobe und läuft über kurze Fahrzeugpausen weiter.
Sitzungsende, quittierter Start oder Stopp und eine Fahrzeugruhe von mindestens
der wirksamen Mindestladezeit beenden diesen Anker. Nutzer-Aus und Schutzstopps
wirken sofort.

Die Netzbremse zählt nur unerlaubten Bezug: Netzpunkt minus frisch gemessene
Leistung der Netzboxen. Fehlt eine Messung oder ist sie älter als 15 Sekunden,
gilt der rohe Netzwert. Zwei Regeltakte über 300 W senken den Strom ab.
Solange eine Netzbox nach Start, Anhebung oder Phasenwechsel noch nicht
quittiert ist, zählt ihr Einschwingen für die anderen Boxen nicht als
Bezugstakt; nach eigenem Ausgang gilt dieselbe Quittungsregel. Netzboxen selbst
haben keine Netzbremse. Nach einer Korrektur
braucht eine erneute Anhebung 30 Sekunden Beruhigung unter 300 W.

Der Hausanschlussrahmen folgt den Phasenwerten des E3DC-Leistungsmessers und der beschriebenen gemeinsamen Verteilung. Eine frische Boxprobe nach dem Befehl quittiert den Ausgang, wenn ihr Phasenvektor näher am neuen als am zuletzt quittierten Stand liegt; nach 30 Sekunden genügt eine frische Meldung auch bei geringerer Fahrzeugaufnahme. Der gehaltene Hauswert steigt sofort. Er sinkt erst nach Quittung aller Boxen auf dieser Phase und mit einer Zählerprobe, die jünger ist als die quittierte Boxprobe. Während der Datenfrist bleibt er eingefroren. Ohne frische Einzelmessung bleibt die Box im Haus inklusive Verbraucher und belegt zusätzlich ihren Hardwarehöchststrom im konservativen Wallboxrahmen. Status und Entscheidungssatz zeigen Hauswert und Quittungsstände.

Bei einer einzelnen phasenschaltfähigen Box mit belegter dreiphasiger
Fahrzeugfähigkeit qualifizieren sich Phasenwahl und Start im Leerlauf gemeinsam
60 Sekunden lang gegen die Mindestleistung der Zielphase. Ein ausreichender
einphasiger Anteil kann so auch nach einem quittierten Stopp einphasig starten.
Nach der Gerätesequenz folgt keine zweite Startqualifizierung. Fällt das Budget
nach dem gesendeten Phasenbefehl, bleibt die Box ohne Start auf einer Phase.
Vor diesem Befehl bleibt die bestehende Gerätefreigabe erforderlich. Unbekannte
Fähigkeiten und Gruppen behalten die bisherige Startprüfung. Die
480-Sekunden-Phasensperre bleibt auch bei einem Nutzerstart verbindlich. Ausgangslose Phasenreservierungen bleiben im Prozessspeicher
und werden beim Neustart verworfen. Erst ein gesendeter Phasenauftrag wird
gespeichert. Bereits gesendete Phasenaufträge werden nicht verworfen.

Fällt der Anteil während einer dreiphasigen Ladung unter die dreiphasige
Mindestleistung, deckt aber eine Phase, hält die Box bei freier Phasenumschaltung
60 Sekunden Mindeststrom und steigt dann auf eine Phase ab.
Das bisher verbrauchte Wolkenkontingent bleibt über die gesamte
Phasensequenz erhalten, auch bei einer Nullpause über 60 Sekunden.
Eine weitere Wolke auf einer Phase nutzt nur den verbleibenden Rest.
In den ersten 480 Sekunden nach einem wirksamen Phasenwechsel hält die Box
bei Unterdeckung den Mindeststrom; Schutzstopps wirken sofort. Diese
Mindesthaltezeit zählt die ersten 480 Sekunden der vorhandenen Phasensperre,
auch wenn die Gerätesperre gegen einen weiteren Wechsel länger dauert.
Sie wird zusammen mit der Sperre wiederhergestellt. Ein Warte-Timeout ohne
bestätigten Wechsel beginnt keine neue Haltezeit. Bei selbst ausgeführten
Wechseln ohne eine von der Regelung gesetzte Phasensperre entsteht kein
zusätzlicher Halt. Danach gelten drei Minuten Unterdeckung
und die Mindestladezeit. Endet die Akkustützung, gilt dieselbe Unterdeckungsregel;
die Ladung endet dadurch nicht sofort. Ein kurzer Datenhalt samt Erholung
behält den bisherigen Ausgang; danach folgt der Mindeststrom.

Eine Box ohne Fahrzeug erhält keinen Energieanteil. Nach einem Dienstneustart
ohne Phasenwerte fehlt der vorher gehaltene Hauswert: Bis zum Ende der
Datenfrist wird kein Hausabzug angesetzt, und eine übernommene laufende Box
hält den Mindeststrom. Neue Starts bleiben bis zu gültigen Phasenwerten gesperrt.

Ein Zuteilungsfehler macht den Anteil unbekannt. Fehlt die elektrische
Gruppenzuteilung, gilt für jede Box der elektrische Nulldeckel, und laufende
Boxen stoppen sofort. Für andere fehlende Live-, Box- oder Anteilsdaten sowie
gesperrte Budgets gilt die vorhandene 300-Sekunden-Datenfrist
mit Mindeststrom; danach stoppt die Ladung. Ein veraltetes Budget hält den Mindeststrom; nach 45 Sekunden ohne neues Budget endet eine PV- oder Akkuladung. Die Datenfrist wird erst nach zehn
Sekunden durchgehend gültiger Daten zurückgesetzt. Ein gültiges Nullbudget ist kein
Datenfehler; dafür gilt die Unterdeckungsregel. Kurze ungültige Liveproben halten den
Ausgang zunächst zehn Sekunden unverändert; bei länger ungültigen Proben gilt
Mindeststrom. Auch während der folgenden zehn Sekunden Erholung bleibt der
Ausgang unverändert: Es gibt keinen Start und keine Stromänderung außer durch Schutzgründe;
Hausanschlussgrenze, Notstromreserve, Pause, Not-Aus, Ladepunktsperre und das Ende eines
Ladefensters wirken sofort. Häufig
wiederkehrende Aussetzer mit weniger als zehn Sekunden gültigen Daten dazwischen
lassen die Datenfrist weiterlaufen und führen nach 300 Sekunden zum Datenstopp.
Auch unbekannte Grenzen teilen diese Frist; ein Wechsel der Fehlerart verlängert sie nicht.
Beobachtete Boxen bleiben beobachtet. Eine unbekannte Quelle beendet kein
Netzfenster. Der Netzsitzungsmerker gilt für dieselbe bestätigte Stecksession;
eine einzelne flackernde Steckprobe beendet sie nicht.

Eine Netzladung an der Notstromreserve bleibt zulässig, solange der Akku ruht
oder lädt. Nur eine frische, gültige Messung mit Entladung über 100 W an oder
unter der wirksamen Reserve führt sofort zum Schutzstopp. Unbekannte,
veraltete oder ungültige Live-, SoC- oder Akkuwerte laufen über die vorhandene
300-Sekunden-Datenfrist. Der Speicher fordert im Hauptlauf keinen Wallbox-Stopp.
Das Notstromsignal des Speicherbudgets sperrt Speicherentladung; allein
sperrt es keine Netzladung.

Die vorhandenen Gerätesequenzen,
Cooldowns, Reservierungen und frischen Rücklesebelege bleiben verbindlich.
Nutzer-Aus, Notstromreserve, Hausanschluss, Datenvalidität und sichere
Hardwarekommandos haben Vorrang.
Eine native Stoppflanke braucht den frischen Zustand „nicht abgebrochen“. Fehlt das Bit, gilt der einmalige Rückfall mit frischem Ladebeleg. Im Fehlerzustand „Box reagiert nicht auf den Stopp“ erhalten Pause, Ladepunktsperre, Not-Aus, Notstromreserve und Hausanschluss gemeinsam genau eine zusätzliche Stoppflanke: frühestens 60 Sekunden nach dem letzten Stoppversuch, bei frischer Probe mit „nicht abgebrochen“ oder unbekanntem Bit und nur mit frischem Ladebeleg. Die Fehleranzeige erscheint ab Fehlerbeginn. Die zusätzliche Einmalflanke ist höchstens einmal je Stecksession und Dienstlauf zulässig; ist sie verbraucht oder für den Stoppgrund nicht berechtigt, bittet die Oberfläche, den Ladepunkt zu prüfen. Nach einem Neustart des Dienstes steht sie einmal neu zur Verfügung. Eine Stecksession endet erst mit bestätigtem Abstecken: mindestens drei aufeinanderfolgende frische Proben ohne gestecktes Fahrzeug über mindestens zehn Sekunden. Insgesamt sind höchstens drei Versuche je Ereignis zulässig; eine bereits verbrauchte Einmalflanke wird durch einen späteren Nutzer-Stopp nicht erneuert.

„Beobachten“, reine Messquellen und extern geführte openWB-Primary-PV-Rollen
erhalten keine Start- oder Strombefehle. Pause, Sperre und Not-Aus bleiben
auch dort wirksam. Die Primary-Direktregelung verwendet denselben Kern.

Fehlt nur der gültige PV-Teil, halten PV-Boxen höchstens den Mindeststrom,
ohne Start oder Anhebung; Akkuboxen nutzen das gültige Gesamtbudget weiter.
Veraltetes Gesamtbudget führt zum Mindeststrom, nach 45 Sekunden zum Stopp.
Bei ungültigen Livewerten gelten zunächst zehn Sekunden Halt, danach
Mindeststrom und nach fünf Minuten ein Stopp über den bestehenden Befehlsweg.

„Sofort bis Preislimit“ startet im freigegebenen Netzfenster ohne Wartezeit. Das Ende des
Netzfensters stoppt im ersten Zyklus. Ein späterer Start mit anderer freigegebener Quelle benötigt erneut 60 Sekunden ausreichenden Anteil.
„Akku bis Abfahrt“ startet nur im Abfahrtsfenster, stoppt am Fensterende,
zur Abfahrtszeit und bei erreichtem Fahrzeugziel. An der Hausakku-Untergrenze
kann es mit ausreichendem PV-Anteil weiterladen. Die Speicherentscheidung bei
Netzladung bleibt bestehen: Ein freigegebener Preis-/Slot-Halt kann die
Entladung sperren; AUTO-Freilauf allein garantiert keine Entladesperre.

Auf der Seite „Wallbox“, auch in der mobilen Ansicht, steht je Ladepunkt der Entscheidungssatz
mit Anteil, Strom, Phasen und Wartegrund. Bleibt nach einem Start das Fahrzeug
aus, nennt der Satz dies spätestens nach 30 Sekunden; es wird kein weiterer
Start allein wegen des ausbleibenden Ladebeginns gesendet. Beginnt das Fahrzeug
später selbst zu laden, übernimmt der Kern die laufende Sitzung.
Die Diagnosefelder `simple_core_grid_corrections_1h` und
`simple_core_grid_import_wh_1h` zeigen die letzte Stunde seit Managerstart.
Ungültige Messungen und Lücken über 15 Sekunden werden ausgelassen.
Simulationen ersetzen keinen Feldnachweis der konkreten Geräte und Fahrzeuge.

Eine fest verdrahtete openWB Pro ohne einphasiges Fahrzeugprofil startet aus
der Ruhe erst, wenn ihre Phase belegt ist. Ein bloßes Phasenziel genügt nicht.

Fällt die elektrische Gruppenzuteilung aus, stoppen laufende Wallboxen über
die bestehende Hausanschlussbegrenzung. Es gibt während des Ausfalls keine
Startversuche. Danach gilt die Startqualifizierung von 60 Sekunden. Eine freigegebene, noch nicht
ladende native E3DC-Wallbox erhält beim Fehlerbeginn ebenfalls einmal einen
Stoppbefehl, auch ohne gestecktes Fahrzeug. Danach bleibt sie bis zum nächsten
qualifizierten Start gesperrt.

Kurze Fahrzeugpausen behalten Ladeanker und Mindestladezeit. Eine
Fahrzeugruhe von mindestens der wirksamen Mindestladezeit (mindestens
300 Sekunden) beendet die Ladeepisode. Beim nächsten Ladebeginn beginnt die
Mindestladezeit neu. Ein quittierter Start setzt den bisherigen Ladeanker
zurück. Die Unterdeckungszeit läuft durch Fahrzeugpausen weiter; beginnt
die Unterdeckung erst in der Pause, zählt diese Zeit mit. Auch ein während
der Pause gedeckter Anteil unterbricht diese Uhr nicht. Phasenumschaltung
und Wiederanlauf bis zum quittierten Wiederanlaufstrom zählen nicht als
Fahrzeugruhe. Ungültige Statusdaten verwerfen die Ruheuhr.

Wiederkehrende Ausfälle der Gruppenzuteilung können die Ladung nach erneuter Startqualifizierung wiederholt stoppen. Der elektrische Schutzstopp hat Vorrang; eine zusätzliche Startsperre gibt es nicht.


### Speicherentladung während des Wallbox-Netzladens

Unterhalb der Hausakku-Untergrenze der Wallbox bleibt die Entladung für das
Fahrzeug begrenzt. Der Speicher darf weiterhin das gemessene Haus- und
Wärmepumpendefizit decken. Externe Wallboxen sind bereits im Hausverbrauch
enthalten; ihre Leistung wird für diese Grenze genau einmal herausgerechnet.
Bei fehlender Wallbox-Messung und zuletzt eingestecktem oder ladendem Fahrzeug
gilt die vorhandene Messlückenregel: Die Grenze
beträgt höchstens die zuletzt belegte Haus-/WP-Grenze oder die konfigurierte
Hausgrundlast plus Wärmepumpe, je nachdem, welcher Wert größer ist. Der letzte
Beleg gilt dabei 600 Sekunden; danach bleibt die Grundlast mit Wärmepumpe.
Die konfigurierte maximale Entladeleistung begrenzt auch diesen Ersatzwert.
In Budgets und Bilanzen bleibt der gemessene Hausverbrauch einschließlich
unbekannter Verbraucher erhalten.

Während eine Wallbox unterhalb der Hausakku-Untergrenze aus dem Netz lädt,
bleibt die Wallbox-Entladegrenze vor einem Markt-Netzladeauftrag wirksam.

Ohne Fahrzeug beeinflusst eine ausgefallene Wallbox die Speicherregelung nicht.
Bei der openWB Pro belegt der fortschreitende Quellenzeitstempel die Frische.
Fehlt er bei älterer Firmware, gilt der erfolgreiche Abruf als Frischebeleg.
Jeder Leser behält seine engere Altersgrenze; im Speicherpfad sind es 30 Sekunden.
Ein Uhrversatz zur Steuerung entwertet die Messung nicht. Bleibt die Box-Uhr
über die vorhandene Datenfrist stehen oder läuft sie rückwärts, erscheint
die Messung als ungültig. Angezeigter Angebotsstrom ist die Rücklesung der
Wallbox; ein gesendeter Sollstrom bestätigt weder seine Übernahme noch die
physische Ladeleistung des Fahrzeugs.

### Wallbox ausschließlich per MQTT messen

Bei „Deaktiviert / Keine Wallbox 1“ bzw. „Deaktiviert / keine Wallbox 2“
(„Keine zweite Wallbox“) können Broker und Leistungs-Topic weiterhin zur Messung
verwendet werden. Die Messwallbox erscheint in Übersicht, Verlauf, Tagesstatistik und Kosten getrennt; Langzeitansicht und Telegram-Berichte zeigen den Hausverbrauch einschließlich dieser Wallbox. Die Box bietet keine Steuerung und
benötigt keinen Wallbox-Dienst. Ohne gültigen Messwert entfällt die Kachel.
Verbrauchsprognose und Regelung verwenden weiterhin den Hausverbrauch
einschließlich dieser Wallbox. Als Thema der Messwallbox nicht das Ausgabethema der eigenen Wallboxregelung eintragen.
