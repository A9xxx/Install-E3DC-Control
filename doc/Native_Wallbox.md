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
* **Klare Nutzer-Modi:** `Aus`, `PV-Kurve ruhig`, `Grundladung stabil`,
  `PV + Akku bis Untergrenze` und `Sofort bis Preislimit`.
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
| `PV-Kurve ruhig` | Lädt entlang der Speicher-Ladekurve mit Hysterese. Der Hausspeicher behält Vorrang, wenn die Prognose knapper wird. |
| `Grundladung stabil` | Hält eine ruhige Grundladung, solange das Speicherziel laut Planung erreichbar bleibt. |
| `PV + Akku bis Untergrenze` | Nutzt PV und oberhalb der Hausakku-Untergrenze zusätzlich den Speicher. Die unten beschriebene begrenzte Phasenerkennung darf kurz Netzleistung überbrücken. Ist die Untergrenze erreicht und trägt das PV-Budget dieser Wallbox die Mindestleistung der aktuellen Phasenzahl nicht, wird eine laufende Ladung bei dieser Phasenzahl auf den Mindeststrom gesetzt (ohne PV sofort, bei noch anliegendem PV nach 20 s Bestätigung) und nach einem Durchlauf des Wh-Kontos gestoppt; eine dreiphasig ladende openWB Pro wechselt stattdessen auf 1p, wenn das PV-Budget das 1p-Minimum trägt. Laden mehrere Wallboxen, wird diese Wallbox ohne Netzbezug zuerst abgesenkt, sofern sie selbst über ihrer Zuteilung lädt; deckt ihre Zuteilung die tatsächliche Ladeleistung, lädt sie weiter. Netzbezug und ein laufender Phasenwechsel einer anderen Wallbox haben Vorrang. |
| `Sofort bis Preislimit` | Netzladen nur, wenn der aktuelle Preis unter dem Wallbox-Preislimit liegt. Ohne Preis- oder Netzfenster gilt an der Hausakku-Untergrenze dieselbe Absenkung wie in `PV + Akku bis Untergrenze`. |
| `Akku bis Abfahrt` | Lädt im Freigabefenster vor der Abfahrtszeit aus PV und Hausspeicher bis zur Hausakku-Untergrenze `wbminsoc`; Netzladen bleibt gesperrt. Gestoppt wird bei erreichter Abfahrtszeit, vollem Fahrzeug oder erreichter Untergrenze. Abfahrtszeit `wb<n>_battery_departure_time` (Standard `06:30`), Fenster `wb<n>_battery_departure_window_h` (Standard 3 h, 1–36 h). |

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

## Netzbezug beim Laden: Einschwingfrist, Anhebung und Grid-Wächter

Der Hausspeicher regelt im Automatikbetrieb den Netzpunkt auf null, braucht
dafür aber einige Sekunden: Ein E3DC gleicht eine Laststufe typischerweise
nach 5–8 s aus, große Sprünge von mehreren Kilowatt nach bis zu 13 s; die
Livewerte kommen im 3-s-Takt. Senkt die Wallbox in dieser Zeit wegen des
Netzbezugs ab, gleicht der Speicher danach eine Last aus, die es nicht mehr
gibt. Es folgen Einspeisung, eine erneute Anhebung und erneuter Bezug – die
Ladung pendelt.

### Einschwingfrist

* Oberhalb des Mindeststroms senkt der Defizitregler bei Netzbezug erst ab,
  wenn der Bezug seit Beginn der Bezugsepisode die Einschwingfrist
  `wb_grid_import_settle_s` überdauert. Standard sind 10 s (Reaktionszeit des
  Speichers plus ein Messtakt), 0 schaltet die Frist ab, höchstens gelten
  30 s. Danach senkt er wie bisher proportional zum noch anstehenden Bezug ab.
* Die Episode endet erst nach einer vollen Frist ohne Bezug. Kurze Pausen
  zwischen wiederholten Spitzen verlängern die Frist nicht; anhaltender Bezug
  und Pulse mit Pausen unter der Fristlänge werden spätestens nach der Frist
  abgeregelt. Pulsierender Bezug mit Pausen ab der Fristlänge beginnt jedes
  Mal eine neue Episode; ihn begrenzt das Netz-Wh-Konto (Schwelle
  `wb_min_current_import_stop_wh`, Leck `wb_min_current_import_release_w`).
  Gleicht der Speicher solche Pulse nicht aus, können bis zur ersten
  Absenkung je nach Pulsmuster einige bis einige zehn Wh Bezug entstehen
  (etwa 1,5 kW mit 9 s Bezug und 14 s Pause rund 11 Wh, 2,5 kW mit 8 s Bezug
  und 16 s Pause rund 48 Wh).
* Das Netz-Wh-Konto zählt während der Frist weiter. Erreicht es seine
  Schwelle (`wb_min_current_import_stop_wh`), endet die Frist sofort. Am
  Mindeststrom gilt die Frist nicht; dort entscheidet wie bisher das Wh-Konto
  über Phasenwechsel und Stop.
* Sofort, ohne Frist, senkt der Defizitregler ab, wenn der gemessene Bezug
  einer Netzphase ihre Betriebsgrenze erreicht (`grid_max_amps_l1..3`
  abzüglich `grid_wallbox_reserve_amps_l1..3`, Rückfall auf `grid_max_amps`
  und `grid_wallbox_reserve_amps`; ohne Phasenwerte zählt konservativ der
  Summenbezug auf einer Phase gegen die kleinste Phasengrenze, eine einzelne
  fehlende Phase gilt als unbekannt und zählt ebenso mit dem Summenbezug),
  wenn der Bezug die Reichweite des Speichers übersteigt und wenn die
  Reichweite unbekannt ist. Die Reichweite ist die aktuelle Ladeleistung plus
  der noch freie Teil der wirksamen Entladegrenze abzüglich der aktuellen
  Entladung. Die wirksame Entladegrenze ist der kleinste Wert aus
  `maximaleentladeleistung`, der Entladegrenze, die der Speicherregler gerade
  gesetzt hat (etwa die Klemme der PV-only-Klasse), und der vom E3DC gemeldeten
  genutzten Entladegrenze (nur ein Wert über 0 W zählt). Grundlage ist der
  gültige Leistungseinstellungsbereich der Livewerte; fehlt er, ist er nur
  teilweise gelesen oder fehlt der Batteriewert, ist die Reichweite unbekannt
  – die Konfiguration allein ersetzt ihn nicht.
* In der PV-only-Klasse unter dem Kurvenkorridor (der Speicher speist die
  Wallbox dort nicht) und an der Notstromreserve gilt keine Frist.
* Hausanschlussdeckel der Wallbox, Phasen- und Schieflastdeckel, Nutzer-`Aus`
  und der Grid-Wächter sind eigene Grenzen und bleiben von der Frist
  unberührt.

### Anhebung nach Beruhigung

* Eine laufende Ladung wird je Schritt um höchstens 2 A über den zuletzt
  ausgegebenen Sollstrom angehoben. Der nächste Schritt folgt erst, wenn seit
  der letzten Anhebung die Einschwingfrist vergangen ist, kein Netzbezug über
  der Toleranz `wb_min_current_import_tolerance_w` (Standard 200 W) anliegt und
  keine Bezugsepisode mehr offen ist. Ohne gültige Livewerte wird nicht
  angehoben.
* Die Stufe gilt je Wallbox. Laden mehrere Wallboxen, kann jede im selben
  Zyklus eine Stufe anheben; eine gemeinsame Frist für die Gruppe gibt es
  bewusst nicht, weil eine häufig anhebende Wallbox die andere sonst dauerhaft
  blockieren würde. Der gemeinsame Netzbezug und die offene Bezugsepisode
  bremsen trotzdem beide: Solange Bezug ansteht, hebt keine an.
* Absenkungen, Starts sowie freigegebene Netz-, Preis-, Slot- und
  Pre-Dump-Fenster sind nicht betroffen. Der PV-Überschussregler der openWB
  Pro in `PV-Kurve ruhig` hebt nur um die gemessene Einspeisung an und bleibt
  unverändert.
* Folge: Von 6 A auf 16 A dauert es mindestens fünf Schritte und damit
  mindestens 50 s; der Überschuss fließt in dieser Zeit in den Speicher.

### Energiebetrachtung

* Je Anhebungsstufe einer Wallbox entsteht bis zum Ausgleich höchstens
  2 A · 230 V · Phasen Bezug, dreiphasig 1,38 kW, einphasig 0,46 kW. Gleicht
  der Speicher nicht aus, endet der Bezug spätestens nach Frist plus Messtakt
  (13 s): höchstens 5,0 Wh je Stufe (einphasig 1,7 Wh). Bei einem Ausgleich
  nach 3–8 s sind es 1,2–3,1 Wh (dreiphasig). Heben zwei Wallboxen im selben
  Zyklus an, addiert sich das (dreiphasig 2,76 kW, höchstens rund 10 Wh).
* Eine kurze Lastspitze kostet nur ihre eigene Energie bis zum Ausgleich durch
  den Speicher, etwa 2,5 kW für 4 s ≈ 2,8 Wh. Dieser Bezug entstand auch
  vorher; es entfallen die Folgeschritte des Pendelns.
* Anhaltenden Bezug, den der Speicher nicht deckt, regelt die Wallbox
  höchstens um die Frist später ab: zusätzlich höchstens Bezug · 13 s, bei
  3 kW also rund 11 Wh je Episode. Liegt der Bezug schon über der Reichweite
  des Speichers (gesperrte oder geklemmte Entladung, PV-only-Klasse,
  Notstromreserve), entfällt diese Verzögerung.

### Keepalive der openWB Pro

* Der Keepalive wiederholt ausschließlich den Strom des zuletzt tatsächlich
  ausgeführten Strombefehls derselben Stecksession. Er berechnet keinen
  eigenen Sollstrom, senkt nie selbst ab und hebt nie an.
* Er sendet nur, wenn dieser Befehl mindestens 10 s zurückliegt – so lange
  gilt ein abweichender Readback als Latenz der Box – und die Box weiter
  abweicht: Sie bietet mehr als 0,5 A mehr an, bei laufender Ladung mehr als
  0,5 A weniger, oder sie zieht deutlich mehr Leistung, als der Sollstrom
  erlaubt.
* Liegt der letzte Sollstrom über der Obergrenze des laufenden Zyklus
  (Ausgangsautorität oder Allokationsdeckel), sendet der Keepalive nichts;
  die Absenkung gehört dem Regelpfad. Ein Stop-, Phasen- oder CP-Befehl
  beendet den Keepalive-Wert, bis wieder ein Strombefehl ausgeführt wurde.

### Grid-Wächter

Der Grid-Wächter ist die letzte Schutzstufe gegen anhaltenden Netzbezug.

* Liegt der Netzbezug länger als 45 s über 500 W, senkt er jede Wallbox, die
  laut Messung tatsächlich lädt und über 6 A steht, auf 6 A ab (Journal
  „[Wachter] … Netzbezug > 45s -> Deckel auf 6A“).
* Er hebt nie an, startet nie, sendet nie 0 A und setzt keinen Ladezustand.
  Seine Absenkung passiert die Ausgangsgates nur als Absenkung unter den
  stehenden Sollstrom.
* Gestoppte, pausierte, abgesteckte oder gesteckte, aber nicht ladende
  Wallboxen, Wallboxen im Modus `Aus` und Wallboxen, die der Manager gerade
  stoppt, bleiben außen vor.
* Ist Netzbezug gewollt (Preisoptimierung oder freigegebenes Netzladen), ist
  der Wächter für alle Wallboxen aus.
* Eine Wallbox, die der Defizitregler im selben Zyklus führt – er senkt sie
  selbst ab oder darf es ohne gesperrten Stromausgang –, überlässt er diesem.
  Erreicht die Absenkung des Defizitreglers die Wallbox nicht, senkt der
  Wächter sie selbst ab.
* Weist ein nachgelagertes Gate seinen Befehl ab, bleibt er fällig und
  versucht es in den folgenden Zyklen erneut, höchstens dreimal in Folge;
  danach beginnt die 45-s-Frist neu.
* Meldet der Treiber nur noch den letzten guten Stand (gedrosselter Status,
  höchstens 45 s alt), senkt der Wächter ab, wenn dieser Stand „lädt“ zeigt.
  Maßgeblich ist dann der eigene Sollstrom über 6 A; ein Geräteangebot aus dem
  gedrosselten Stand wird nicht verwendet.

### Ungültige Liveprobe: kurzer Halt

Meldet die Plausibilitätsprüfung eine Liveprobe als ungültig (etwa ein
Grid-/PM-Widerspruch oder eine RSCP-Zeitüberschreitung), sind Netz-, Haus-
und PV-Werte dieser Probe nicht belastbar. Eine laufende Ladung wird deshalb
nicht aus diesen Werten gestoppt, sondern kurz gehalten. Das gilt für alle
geregelten Wallbox-Typen (openWB Pro, openWB, go-e, E3DC).

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
  und zeigt er Bezug über dieser Toleranz, gibt es keinen Halt (außer beim
  Grid-/PM-Widerspruch, für den seine eigene Zählerprüfung gilt).
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
  `invalid_live_sample_no_write_hold` (beim reinen Grid-/PM-Widerspruch
  `single_grid_pm_delta_high_no_write_hold`), zurückgestellte Befehle mit
  `invalid_live_sample_output_hold`.

## Haltezone an `wbminsoc`

In `PV + Akku bis Untergrenze` (und in `Sofort bis Preislimit` ohne Preis-
oder Netzfenster) hält der Speicher den SoC zwischen `wbminsoc` und
`wbminsoc` plus Neustart-Abstand, statt zu pumpen (Regelungsphilosophie in
`doc/V4_Konfiguration_und_Regelung.md`, Akkustützung Punkt 5). Er
veröffentlicht dafür im Wallbox-Rahmen den Vertrag
`wallbox_wbminsoc_hold_zone` mit dem Rahmen `budget_w` (gleich
`hold_frame_w`): dem batterieneutralen PV-Rahmen der Wallboxgruppe (PV minus
Haus, Wärmepumpe und Heizstab) oder, während einer kurzen Schwankung, der aus
dem Wolkenkontingent gehaltenen Leistung.

* Im Band einschließlich der Untergrenze ist dieser Rahmen das PV-Budget der
  Wallbox. Der Wallbox Manager regelt Strom, Phasen und Stop darin und
  rechnet dort kein eigenes Budget: keine Reduktion der Akkustützung nahe
  `wbminsoc`, keine eigene Untergrenzen-PV und kein PV-Budget 0 W, wenn das
  wbminSoC-Tor an der Untergrenze schließt. Sinkt der Rahmen, folgt der
  Sollstrom direkt, statt über das Budgetkonto in kleinen Schritten.
* Trägt der Rahmen die Mindestleistung der genutzten Phasenzahl nicht
  (1p 1380 W, 3p 4140 W), greift auch bei offenem Tor derselbe Pfad wie an
  der geschlossenen Untergrenze: Mindeststrom, ein Durchlauf des Wh-Kontos,
  danach 1p (wenn der Rahmen das 1p-Minimum trägt und die Wallbox umschalten
  kann) oder Stop.
* Der Akkuwächter an der Untergrenze (Akkuentladung über
  `wb_target_floor_battery_discharge_threshold_w`, Standard 700 W) senkt im
  Band nicht gegen eine Überbrückung aus dem Wolkenkontingent ab.
* Gebunden ist das an einen frischen, gültigen Speicherrahmen und Vertrag
  (höchstens 15 s alt), an die Modi mit Haltezone (nicht `Akku bis Abfahrt`)
  und an den Regelpfad der Untergrenze ohne Netz-, Preis-, Boost- oder
  Pre-Dump-Fenster; sonst gilt die bisherige Regel. Diagnose: `wbminsoc_hold_zone_frame` in
  der Zustandsdatei des Wallbox Managers.

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
Wichtige Felder:

```ini
wb_native_enable = 1
wb_native_type = e3dc|openwb|openwb_pro|goe|none
wb_native_ip = 192.0.2.50
wb_native_mode = 0|1|2   # Dual-Wallbox-Priorität: 0=beide, 1=WB1, 2=WB2
wb1_mode = 0|2|3|4|5
wb2_mode = 0|2|3|4|5
wbminsoc = 50
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
wb_openwb_start_retry_s = 45     # openWB Pro: wirksam max(60, Wert); praktisch wirkungslos, der Zyklus entscheidet
openwb_pro_start_reject_timeout_s = 120  # openWB Pro: wird gelesen, ohne Wirkung auf den Latch (SoC entscheidet)
openwb_pro_start_grace_s = 60    # openWB Pro: Fallback für Alt-Aufrufer des Angebots-Halts; im Startfenster ohne Wirkung
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
* `WBchar6[4]` ist ein Toggle-Impuls, kein dauerhaftes Soll. Der Impuls darf nur
  für eine bewusste Start- oder Stop-Flanke gesetzt werden.
* `force_state=None` bedeutet bei E3DC immer: nur Stromdeckel/Keepalive, kein
  Toggle. Ein reines Ampere-Update darf eine wartende oder bereits beendete
  Ladung niemals wieder starten.
* `force_state=2` ist der explizite Startimpuls. Er darf erst gesendet werden,
  wenn das physische Budget für die Mindestleistung plausibel da ist.
  Bei einer Easy Connect sind je frisch bestätigter Stop-Episode höchstens drei
  solche Impulse mit mindestens 60 Sekunden Abstand zulässig. Jeder Impuls
  benötigt erneut einen frischen Stop-Readback; Laden, Abstecken oder ein neuer
  Stopzustand beendet beziehungsweise erneuert die Episode. Andere native
  E3/DC-Familien bleiben bei genau einem Startimpuls je Stop-Episode.
* `force_state=1` ist der harte Stopimpuls. Er darf nur für echte harte
  Stopgründe und bei verifizierter aktiver Ladung verwendet werden.

In der Diagnose wird dieser Vertrag als
`e3dc_native_production_v1` sichtbar: Ampere-Updates sind Stromdeckel, keine
Toggles; Startflanken laufen nur über die Session-State-Machine; harte
Stopflanken brauchen einen harten Grund und verifizierte Ladung.

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
PV-geführt** heißt, openWB bleibt im PV-Modus und E3DC-Control führt nur den
Speicherrahmen anhand der gemessenen Wallboxleistung. **Primary-Direktpfad**
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
* **Budgetabhängige Bereitschaft:** Fehlt das Budget für die gehaltenen Phasen,
  setzt die Regelung `ampere=0`. Bei erneut ausreichendem Budget bietet sie
  den Mindeststrom nach den bestehenden Wiederanlauf- und Schutzfristen wieder
  an. Es entsteht keine dauerhafte Fahrzeug-Ladeende-Sperre. `6 A` an drei
  Phasen benötigen rund `4,14 kW`; eine Nullmessung wird nicht als einphasiges
  Fahrzeug behandelt. Nach einem Stop der Defizit-Kaskade (Netz- oder
  Akku-Wh-Konto) in derselben Stecksession bietet die Regelung den Mindeststrom
  erst wieder an, wenn das PV-Budget die Mindestleistung der erwarteten
  Phasenzahl `wb_pv_only_release_hold_s` (Standard 120 s) lang durchgehend
  deckt; eine einzelne Wolkenlücke startet nicht. Eigene
  Ziel-SoC-/Energiemengen- und Abfahrtsgrenzen, Nutzerpause und
  Schutzvorgaben behalten Vorrang. `Aus` bleibt beobachtend.
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
* **Phasenbeharrung 3p→1p 480 s:** Die Bedingung für einen Abstieg muss nach
  der openWB-Referenz 480 s dauerhaft erfüllt sein – Budgetmangel am
  3p-Minimum oder Deckel 0. Wolkenlücken und kurze Lastspitzen lösen so
  keinen Schützwechsel aus. Nur Schutzfunktionen verkürzen die Wartezeit auf
  den kurzen Schutzpfad (`60 s`): Netzbezug über `wb_phase_down_grid_w`,
  wbminSoC-Untergrenze, unautorisierte Akkustützung, Speichervorrang und
  Floor-PV-only. Konfigurierbar über `wb_phase_down_delay_s` (mindestens
  `60`).
* **Hochschaltung 1p→3p nach evcc/openWB-Muster:** Eine Messgröße entscheidet
  in beide Richtungen: der verfügbare Überschuss `P_avail` = Wallbox-Leistung
  + Einspeisung (abzüglich `wb_phase_up_export_margin_w`) – Akku-Entladung
  (eine Entladung des Speichers zählt als Defizit, nicht als Überschuss), als
  gleitendes 30-s-Mittel. Liegt ein frisches, autorisiertes Wallbox-Budget des
  Speicherreglers vor, zählt der größere Wert aus Messung und Budget (abzüglich
  einer Akku-Entladung): Was der Speicher in seiner Ladekurve aufnimmt, ist für
  die Wallbox verfügbar. Als Budget zählt die für genau diesen Ladepunkt
  zugeteilte Leistung; sie enthält die laufende Ladeleistung bereits und wird
  nicht zusätzlich addiert. Hochgeschaltet wird, wenn (a) dieser Überschuss das
  3p-Minimum plus Puffer trägt (`4140 W + wb_phase_up_buffer_w`, an der openWB
  Pro 300 W) und (b) die eine Phase ausgereizt ist. Sind Fahrzeug (Profil
  3-phasig) und Wallbox phasenschaltfähig, gilt der Referenzstrom: der kleinere
  Wert aus dem 1p-Deckel und dem aufgerundeten Quotienten (3p-Minimum + Puffer)
  / 230 V (an der openWB Pro 20 A) – ausgereizt, wenn der gemessene Strom bis
  auf 2 A daran steht oder der Zielstrom `P_avail / 230 V` darüber liegt. Ein
  dynamischer 1p-Deckel (Schieflast, Phasenreserve) oberhalb der Referenz
  verschiebt die Schwelle nicht mehr auf 7,4 kW. Ohne schaltfähiges Paar zählt
  der wirksame 1p-Deckel (Zielstrom darüber oder gemessener Strom bis auf 1 A
  daran). Eine 16-A-Wallbox (einphasig höchstens 3,68 kW) gilt damit schon ab
  dem 3p-Minimum als ausgereizt. Geschaltet wird, sobald die Uhr
  `wb_phase_up_forecast_hold_s` (Standard `60 s`, mindestens `30`) den Vorlauf
  erreicht **oder** das Export-Wh-Konto (`wb_phase_up_export_wh`, Standard
  120 Wh) voll ist. Die Uhr leckt: Bei erfüllter Bedingung läuft sie, bei
  Wolkenlücken zählt sie zurück, und erst 15 s ohne Bedingung setzen sie auf 0
  (hart nur bei Abstecken, Ladeende, Phasenwechsel oder anhaltendem
  Netzbezug). Das Konto füllt sich mit dem Überschuss oberhalb des
  Referenzstroms, eine Wolke zieht nur das ab, was drei Phasen fehlen würde,
  und es sättigt bei der doppelten Schwelle. Netzbezug sperrt die
  Hochschaltung erst, wenn er 30 s anhält; ein Blip von wenigen Sekunden oder
  ein nur noch abklingender Rest des Netz-Wh-Zählers setzt weder Uhr noch
  Konto zurück. Fehlen Messwerte (Wallbox-Strom, Netz, Speicher), gibt es
  weder Auf- noch Abstieg.
  Nach jedem bestätigten Phasenwechsel gilt die Sperre von 480 s (openWB Pro:
  `openwb_pro_phase_wait_s`; E3DC-Direktvertrag: derselbe Wert, solange kein
  eigener `wb_phase_change_hold_s` gesetzt ist) – sie ist die Beruhigung nach
  dem Schalten, nicht ein Vorlauf davor. Während dieser Sperre steht die Uhr
  auf 0 und das Konto hält seinen Stand: Vorlauf und Konto werden nach der
  Sperre neu verdient, damit auf einen Abstieg kein sofortiger Wiederaufstieg
  folgt. Am E3DC-Direktvertrag gilt die Haltezeit nach jedem Wechsel als diese
  Sperre, genau wie an der openWB Pro. Wie eine Hochschaltung an der openWB
  Pro ausgegeben wird (Freigabe, Wartegrenze, Wiederanlauf), steht im
  Abschnitt „Hochschaltung ausgeben: Freigabe, Wartegrenze und Wiederanlauf“;
  das experimentelle 10-min-Fenster im Abschnitt „Experimentelles
  10-min-Fenster 1p→3p“. Optional bewertet die
  Symmetrie-Klausel `wb_phase_up_symmetry_enable` (Standard aus) eine
  einphasige Ladung ab dem Schieflastwert `grid_pcc_imbalance_max_a`
  (Standard 20 A) als ausgereizt, damit die Einspeisung nicht einseitig auf
  einer Netzphase reduziert wird; vor dem Einschalten ist zu klären, ob die
  Unsymmetriegrenze des Netzbetreibers auch für die Einspeiseseite gilt.
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

#### Hochschaltung ausgeben: Freigabe, Wartegrenze und Wiederanlauf

Eine Hochschaltung 1p→3p an der openWB Pro braucht vor jedem Geräteausgang
die Freigabe des Speicherreglers (Budgetpflicht: Der Speicherregler vergibt die
Leistung, der Wallbox-Manager überschreitet sie nicht).

- **Reservierung:** Reserviert werden die bisher laufende Leistung
  beziehungsweise der Wiederanlauf mit 6 A je Phase (rund 4,14 kW) plus eine
  kleine Messreserve, nicht der bisherige einphasige Strom je Zielphase
  (29 A × 3 Phasen wären rund 20 kW).
- **Wartegrenze:** Solange der Auftrag auf die Freigabe wartet, lädt die Box
  einphasig weiter, während Zuteilung und Speicherregler schon mit drei Phasen
  rechnen. Belegt der frische Status genau eine Phase (Statusziel 1,
  verifizierte Phasenleistung), behält die einphasige Ladung dabei ihren
  Strom im Rahmen des Budgets: Der Wattdeckel rechnet ihn mit dieser einen Phase um, denn vor dem
  ersten Phasenausgang kann die Box nicht dreiphasig ziehen. Bei mehreren
  Wallboxen bleibt die Stromzuteilung der Box dreiphasig, damit die
  Phasenreservierung am Hausanschluss konservativ bleibt. Dies ist die
  vorläufige Variante (b): Die mögliche Drittel-Leistungsbeschränkung bei
  mehreren Ladepunkten bleibt eine bekannte Einschränkung. Bei einer
  Einzelbox gilt dieselbe frische Phasenbindung auch für die Mindestbudget-
  Sperre und noch im Zyklus eines ausgangslosen Abbruchs. Der Auftrag endet
  ohne Geräteausgang, sobald eine dieser Bedingungen eintritt: keine
  ausreichende Freigabe nach 90 s; eine erteilte Freigabe 90 s nach ihrem
  Eingang ungenutzt; die Regelung fordert die Hochschaltung zwei Zyklen lang
  nicht mehr an, etwa in einer Ladepause. Ein Weckimpuls oder CP-Start zählt
  dabei nur als Ausgang, wenn er zu diesem Auftrag gehört; ein Weckstart vom
  Beginn der Stecksession hält ihn nicht fest.
- **Rückzug:** Nach einem solchen Abbruch folgt der nächste Versuch frühestens
  nach `wb_phase_retry_block_s` (Standard 900 s, mindestens die
  Phasenwartezeit von 480 s). Vorlauf, Export-Wh-Konto und 10-min-Fenster
  beginnen neu und werden erst danach neu verdient; am Ende der Sperre folgt
  kein Sofortversuch. Die Log-Zeile lautet „Hochschaltung 1p→3p ohne Ausgang
  abgebrochen: …; nächster Versuch frühestens in 15 min“; die Ursache steht
  dazwischen, zum Beispiel „keine Speicherfreigabe nach 90 s“,
  „Speicherfreigabe 90 s ungenutzt“ oder „Hochschaltung nach 8 s nicht mehr
  angefordert“.
- **Wiederanlauf:** Nach dem Phasenwechsel läuft die Box mit 6 A je Phase an.
  Bis der Wiederanlauf unter Last bestätigt ist (mindestens drei Messungen
  über 500 W, stabil über mindestens 10 s, gleich mit wie vielen Phasen das
  Fahrzeug lädt), begrenzt die Regelung jeden Strom auf die freigegebene
  Reservierung, zum Beispiel auf 6,6 A dreiphasig nach 4,6 kW einphasiger
  Ladung. Danach folgt die Rampe wieder dem Wattbudget, solange die
  Reservierung besteht jedoch weiterhin auf drei Phasen umgerechnet.
  Ein Fahrzeug mit zwei beziehungsweise einer tatsächlich genutzten Phase
  erreicht bis zum Ende der Reservierung deshalb höchstens etwa zwei Drittel
  beziehungsweise ein Drittel des Budgets. Der Lastbeleg beendet nur den
  Wiederanlaufdeckel, nicht die an drei Zielphasen gebundene Reservierung.
  Diese bekannte Grenze bleibt unverändert.
- **Neustart des Managers:** Wartete ein Auftrag beim Neustart noch ohne
  Ausgang, bleiben positiver Strom, Phasenziel und CP gesperrt, bis ein
  frischer Readback nach dem Neustart vorliegt. Ein Ruhe-Readback (CP
  inaktiv, Ladebit aus, höchstens 50 W und ein Stromangebot von höchstens
  0,5 A oder das initiale 6-A-Angebot bei noch alter Phasenzahl) oder eine
  belegte Ladung mit der bisherigen Phasenzahl beendet den Auftrag ohne
  Geräteausgang; danach geht der Start normal durch. Fehlende Felder sind
  kein Ruhebeleg.
- **Diagnose:** Warum eine Hochschaltung nicht ausgegeben wurde, steht je
  Wallbox in `ramdisk/wallbox_native.json` (`wb_details[]`) und
  `ramdisk/wallbox_decision_latest.json` (`wallboxes[]`): `phase_up_block_reason` und
  `phase_up_block_ts` (zum Beispiel `await_storage_grant:0/4750 W`),
  `openwb_pro_phase_grant_wait` (Grund, Wartezeit, ungenutzte Freigabe),
  `openwb_pro_phase_grant_wait_abort`, `openwb_pro_phase_up_request`,
  `openwb_pro_phase_reservation_output_gate` und
  `phase_transition_reservation` (dort zeigen `restart_load_confirmed_ts` und
  `restart_load_phases`, wann und mit wie vielen Phasen der Wiederanlauf unter
  Last bestätigt war). Das Log meldet „Hochschaltung 1p→3p
  ausgesetzt: …“ je Grund höchstens alle 5 Minuten; das bloße Warten auf die
  Freigabe erst nach 90 s. Eine normal laufende Umschaltung erzeugt keinen
  Eintrag; nach Ausgabe, Abschluss, Abbruch oder Ende der Anforderung wird er
  gelöscht.

#### Experimentelles 10-min-Fenster 1p→3p

Schalter `wb_phase_up_window_enable` (Standard 0 = aus). Der Schalter hat im
Config-Editor noch kein eigenes Bedienelement; er wird wie
`wb_phase_energy_policy_enable` direkt in der Konfiguration `e3dc_v4.json`
gesetzt (`1` = ein, `0` = aus). Das Fenster wirkt nur an der openWB Pro
und am E3DC-Direktvertrag und nur mit eingeschalteter Energie-Phasenpolitik
(`wb_phase_energy_policy_enable`).

- **Messgröße:** der verfügbare Überschuss wie bei der Hochschaltung
  (Einspeisung plus Wallbox oder das frische Budget des Speicherreglers; eine
  Akku-Entladung zählt als Defizit). Fehlende oder veraltete Messwerte zählen
  nie als Überschuss.
- **Bereit:** Das Mittel der letzten 10 Minuten erreicht das 3p-Minimum plus
  15 % (rund 4,76 kW) bei mindestens 9 Minuten gültigen Messwerten, und in den
  letzten 2 Minuten ist jeder 30-s-Abschnitt zu mindestens 90 % gemessen und
  liegt über dem 3p-Minimum (4,14 kW). Eine Messung überbrückt höchstens 10 s
  plus die Verlängerung des Leerlauftakts (`wb_idle_poll_s`, etwa 18 s bei
  10 s Leerlauftakt); längere Lücken zählen als fehlende Daten.
- **Wirkung:** Das Fenster ist ein zusätzlicher Auslöser. Ist es bereit,
  schaltet die Regelung eine laufende einphasige Ladung ohne Vorlauf,
  Export-Wh-Konto und die Bedingung „eine Phase ausgereizt“ auf drei Phasen
  (`trigger` = `window`). Vor dem Ladebeginn setzt die openWB Pro das
  Phasenziel 3 nur für ein Fahrzeug mit dreiphasigem Profil, auch wenn die
  Box ruht (`trigger` = `start_window`); unbekannte, ein- und zweiphasige
  Fahrzeuge starten weiter zuerst einphasig. Der bisherige Weg über Vorlauf
  oder Export-Wh-Konto (ohne `trigger`) bleibt daneben wirksam und kann
  früher hochschalten.
- **Unverändert:** Sperre nach jedem Wechsel (480 s; das Fenster beginnt in ihr
  neu und wird danach neu verdient, frühestens also 480 s plus 9 Minuten nach
  einem Wechsel bereit; über Vorlauf oder Export-Wh-Konto frühestens 480 s
  plus 60 s Vorlauf nach einem Abstieg), Netzbezugssperre, einphasige
  Fahrzeuge, Freigabe durch den Speicherregler sowie Wartegrenze, Rückzug und
  Wiederanlauf aus dem vorigen Abschnitt.
- **Diagnose:** `phase_energy_policy.window_enabled`, `window_ready`,
  `window_reason` (`ready`, `cover_short`, `mean_below_threshold`,
  `recent_dip`, `sample_invalid`), `window_mean_w`, `window_cover_s`,
  `window_dip_min_w`, `window_dip_cover_s`, `window_dip_gap`,
  `window_threshold_w` und `trigger` (`window` für die Hochschaltung,
  `start_window` für den dreiphasigen Start). Das Fenster läuft auch bei
  ausgeschaltetem Schalter als Diagnose mit.

**Testanleitung (Community):**

1. Voraussetzungen: openWB Pro oder E3DC-Direktvertrag, Energie-Phasenpolitik
   ein, für den Test des dreiphasigen Starts ein Fahrzeugprofil mit drei
   Phasen. Möglichst eine Wallbox; mit zwei Wallboxen bitte ausdrücklich
   vermerken.
2. Einschalten: in `e3dc_v4.json` `wb_phase_up_window_enable` auf `1` setzen.
   Zurück zum bisherigen Verhalten geht es jederzeit mit `0` (Standard);
   weitere Schritte sind nicht nötig.
3. Beobachten, am besten an einem Tag mit wechselnder Bewölkung. Das Fenster
   ist ein zusätzlicher Auslöser; maßgeblich ist das Feld `trigger` des
   Aufstiegs. Kommen Aufstiege mit `trigger` = `window` beziehungsweise
   `start_window` nur bei stabilem Überschuss? Liegen zwischen einem Wechsel
   und dem nächsten Aufstieg mit `trigger` = `window` mindestens rund
   17 Minuten (480 s Sperre plus 9 Minuten Fenster)? Aufstiege ohne `trigger`
   kommen weiter über Vorlauf oder Export-Wh-Konto, frühestens rund 9 Minuten
   nach einem Abstieg (480 s Sperre plus 60 s Vorlauf); sie sind kein Fehler
   des Fensters. Startet eine neue Ladung nur mit dreiphasigem Profil direkt
   dreiphasig? Bleibt die Ladeleistung, solange eine Hochschaltung auf die
   Freigabe wartet (`phase_transition_reservation.stage` = `await_budget`),
   bei einer Einzelbox im Rahmen des einphasigen Budgets, statt auf rund
   ein Drittel zu fallen? Mehrfachzuteilung bleibt vorläufig dreiphasig.
   Endet bei einem zwei- oder einphasig ladenden Fahrzeug nach mindestens
   drei Lastmessungen über mindestens 10 s der Wiederanlaufdeckel? Strom,
   tatsächliche Phasen und Wattbudget notieren: Die Umrechnung bleibt bis
   zum Ende der Reservierung dreiphasig; etwa zwei Drittel beziehungsweise
   ein Drittel des Budgets sind dann die bekannte Grenze. Folgt nach
   „ohne Ausgang abgebrochen“ kein
   Sofortversuch? Entsteht durch den Wechsel Netzbezug?
4. Rückmeldung: die Log-Zeilen „Hochschaltung 1p→3p …“ (einschließlich der
   Ursache eines Abbruchs, zum Beispiel „keine Speicherfreigabe nach 90 s“,
   „Speicherfreigabe 90 s ungenutzt“ oder „Hochschaltung nach N s nicht mehr
   angefordert“) und „openWB auf 3p angefordert“ mit Uhrzeit sowie aus
   `ramdisk/wallbox_decision_latest.json` (`wallboxes[]`) je Wallbox die
   Felder `phase_energy_policy` (nur die `window_*`-Felder und `trigger`),
   `phase_up_block_reason`, `openwb_pro_phase_grant_wait`,
   `openwb_pro_phase_grant_wait_abort` und `phase_transition_reservation`.
   Diese Felder enthalten nur Leistungs-, Zeit- und Zustandswerte; bitte keine
   Zugangsdaten, IP-Adressen, Fahrzeugkennungen oder Standortangaben
   mitsenden.

#### Startfenster, Wiederholzyklus und Weckimpuls

Beim Ladestart an einer openWB Pro können Budget-Einbrüche durch die eigene,
im Hauswert nachlaufende Ladeleistung, ein `charge_state` ohne Leistung und
die Readback-Latenz der Box zu 0-A-Schnitten, doppelten Kaltstarts, verfrühten
Weckimpulsen und einem „Ladung beendet“ ohne SoC-Beleg führen. Der
Wallbox-Manager führt deshalb je Stecksession ein Startfenster
(`openwb_pro_start_window`, Vertrag
`openwb_pro_session.start_window_contract`, Ein-Entscheider je Zyklus):

- Zustände: `inactive` → `budget_wait` (angesteckt, Budget ≥ Mindestleistung
  in 2 Frames) → Adoption eines stehenden Box-Angebots ohne Befehl, wenn es
  ≤ clamp(cap, 6, Deckel) liegt, sonst genau ein Startbefehl 6 A
  (`offer_pending`) → Readback ≥ 6 A setzt den Anker (`offer_frozen`) →
  bis Anker + `openwb_pro_start_hold_s` kein 0 A und keine Anhebung →
  `charging_confirmed` (2 Frames > 500 W oder > 5 Wh; erst hier werden die
  Startbelege gelöscht) mit Anhebungen/Absenkungen ≥ 6 A im 30-s-Raster →
  `regulating`. Ohne Ladung: `retry_wait` (Zyklen von
  `openwb_pro_start_retry_cycle_s`, je Zyklus höchstens ein Weckimpuls), nach
  drei Zyklen `exhausted` (Angebot bleibt stehen, Meldung „Fahrzeug lädt trotz
  Freigabe nicht“, kein Weckimpuls mehr).
- Absenkungen: Ein Strombefehl ab 6 A unter dem Angebot, das die Box frisch
  zurückmeldet (Status höchstens 10 s alt; ein eigener, noch nicht
  zurückgemeldeter Strombefehl begrenzt den Wert nach oben), passiert das
  Fenster in jedem Zustand und auch außerhalb des Rasters, im Executor-Gate
  wie im Direktpfad. An der Wattgrenze wird eine solche Absenkung nie
  verworfen, höchstens auf 6 A geklemmt. Das betrifft etwa ein übernommenes
  Box-Angebot über dem Budget oder eine Box, die nach ihrem eigenen Neustart
  mit höherem Strom lädt. 0 A, `force_state 1` und Anhebungen bleiben Sache
  des Fensters. Kein Beleg für eine Absenkung sind ein veralteter Status und,
  bis zur Rückmeldung der Box (höchstens 20 s), ein eigenes 0 A oder ein
  anderer eigener Ausgang wie Phasenziel oder Weckimpuls; dann gilt die
  Fensterregel ohne Absenkungsbeleg.
- Nullbudget: ein Box-Angebot ≥ 6 A bleibt `openwb_pro_start_hold_s` stehen,
  dann 0 A als lösbarer Anker (`off_no_budget`, „Wartet auf PV-Budget“); mit
  Budget in 2 Frames erneut 6 A. Bietet die Box 0 A, wird nichts geschrieben.
- Nullbudget nach bestätigter Ladung: In `charging_confirmed` läuft eine
  eigene Nullbudget-Uhr. 0 A wird erst frei, wenn das Budget 60 s lang
  durchgehend fehlt oder in dieser Nullbudget-Episode 100 Wh aus dem Netz
  bezogen wurden (Grund `charging_confirmed_zero_released`); ein Frame mit
  Budget setzt Uhr und Wh-Zähler zurück, Netzbezug mit Budget (Hauslast,
  Akkustützung) zählt nicht. Fehlt das Budget, ist die Ladung bestätigt
  (> 500 W) und steht das Angebot über dem Mindeststrom, darf der Manager
  vorher im 30-s-Raster genau einen Befehl auf den Mindeststrom senden (Grund
  `minimum_hold_reduction`). Diese Absenkung bindet sich an das Box-Readback
  (ohne Readback ≥ 6 A keine Absenkung) und passiert Strom-, Zuteilungs- und
  Budgettor nur als dieser eine Befehl; sie ist nie 0 A.
- Wiederanlauf als Einladung: Sendet der Manager selbst 0 A (Nullbudget,
  Prioritäts-, Pre-Dump- oder `wbminsoc`-Stopp, Stopp der Defizit-Kaskade,
  Direktpfad), fällt das Fenster aus `regulating` oder `charging_confirmed`
  nach `off_no_budget` (Grund `regulating_offer_ended` bzw.
  `confirmed_offer_ended`: Box < 6 A in zwei frischen Frames, kein
  `charge_state`, ≤ 100 W).
  Die nächste Budgetfreigabe eröffnet wieder das 6-A-Fenster
  (`offer_pending` → `offer_frozen` → `charging_confirmed`, Anhebungen im
  30-s-Raster); es gibt keinen Sprung auf den Budgetstrom. Ein 0 A aus einer
  Phasensequenz oder ein box-seitiger Abfall ohne eigenen Befehl lässt den
  Zustand unverändert. Nutzerpause und Nutzer-`Aus` markieren das Fenster
  ebenso; nach der Freigabe zählt eine zuvor bestätigte Ladung nicht als
  Ablehnung (Zyklen beginnen bei null).
- Ohne Readback nach 20 s wird derselbe Befehl höchstens zweimal wiederholt,
  danach `box_unresponsive` (60 s Pause). Nullt die Box das Angebot in zwei
  frischen Frames (außerhalb des eigenen CP-Nachlaufs), folgt ein
  Wiederangebot 6 A mit neuem Anker.
- Harte Kanten (0 A auch im Fenster): Nutzer-Aus, Pause, Sperre, Notaus,
  Hausanschluss/Peak-Shaving/1p-Deckel unter 6 A, Ende eines geplanten
  Ladefensters, bestätigte Trennung, ungültiger Boxstatus in zwei Frames,
  Ladeende-Vertrag nach bestätigter Ladung. Keine Kanten: eigener CP-Impuls,
  Phasenreservierung/-sequenz, Budget-/Speicher-Hard-Block, Prioritätswechsel,
  ein einzelner ungültiger Frame.
- Weckimpuls nur aus dem Wake-up-Tick und nur aus dem Readback: Angebot ≥ 6 A,
  `charge_state` falsch, ≤ 100 W, kein aktiver CP, ≥ 60 s nach `phasetarget`,
  frühestens Anker + `openwb_pro_start_cp_grace_s` (nie unter 60 s, auch mit
  Fahrzeugprofil), ein Impuls je Zyklus, höchstens
  `wb_openwb_start_cp_retries` je Stecken. Ein CP ersetzt nie einen Strombefehl.
- HLC-Faktor: meldet die Box `evse_signaling` ≠ `basic`/`basic iec61851`
  (etwa `basic+fake_highlevel_dc`), verdoppeln sich Fenster, Zyklus und Karenz;
  reines PWM bleibt bei Faktor 1.
- Phasenwahrheit der Stecksession (`openwb_pro_session_phase_latch`): zwei
  bestätigte Frames des Box-Ziels bzw. gemessener Phasen latchen 1p/3p für
  Zuteilung, Speicher-Hard-Block und Direktphasen; der eigene CP-Impuls, ein
  einzelner Stecker-Frame oder ein Ziel-0-Glitch fallen nicht auf das
  3p-Fahrzeugprofil zurück. Nach einem eigenen Phasenwechsel zählen die
  Belege, sobald die Box das neue Ziel frisch bestätigt hat, und nur für
  dieses Ziel. Bei einem einphasig hinterlegten Fahrzeug gilt ein
  Leerlaufziel 3 der Box als eine Phase, bis die Stecksession drei aktive
  Phasen misst.
- Hauswert: die frische Ladeleistung von openWB/openWB Pro/go-e wird
  immer vom Hauswert abgezogen (Deadband 100 W, Stale-Halt 30 s), unabhängig
  von einer E3DC-Wallbox. Das Speicherbudget bleibt bindend.
- „Ladung beendet“ entsteht nur noch bei `exhausted` mit frischem SoC ≥ Ziel
  oder über den Ladeende-Vertrag nach bestätigter Ladung; sonst zeigt die
  Session „Start abgelehnt – Wiederholung hh:mm (Zyklus n/3)“.
- Nach bestätigter Ladung schützt das Fenster nur noch den Anlauf, es bremst
  nicht die Regelung: Das eingefrorene Angebot ist stets das, was die Box
  zurückmeldet. Ist der gemerkte Wert darüber hinausgelaufen, weil eine
  Schreibung die Box nicht erreicht hat, wird er nach dem 30-s-Raster auf die
  Rückmeldung zurückgeholt – eine Anhebung darüber gilt nie als Absenkung.
  Steht ein Phasenwechsel an, während das Fahrzeug bestätigt lädt (> 500 W),
  liegen Anker **und** bestätigte Ladung jeweils mindestens 30 s zurück und
  steht die Box wirklich auf dem gemerkten Angebot (frische Rückmeldung), ist
  der Start vorbei: das Phasenziel wird freigegeben. Das Fenster geht erst
  dann auf `regulating`, wenn der Phasenwechsel wirklich angelaufen ist;
  bricht er vorher ab, bleibt der Anlaufschutz vollständig stehen. Vor der
  bestätigten Ladung bleibt beides gesperrt – dort ist ein Phasenziel der
  Abbruch des laufenden Startversuchs.
- Phasenabstieg im Fenster: Er bleibt gesperrt, solange der Hausspeicher die
  dreiphasige Mindestladung stützen kann – Stützung autorisiert, keine
  Haltezone, SoC über Notstromreserve und `wbminsoc`, Bezug innerhalb der
  Reichweite des Speichers (siehe Einschwingfrist) und eine wirksame
  Entladegrenze, die den Bedarf der Mindestladung trägt: max(0,
  3p-Mindestleistung + Haus ohne Wallboxen − PV) aus demselben Livezyklus
  (Grenze gleich Bedarf reicht). Wurde eine 6-A-Ladung dreiphasig
  freigegeben, soll der Akku sie tragen, statt dass das Fenster den Anlauf
  abbricht. Freigegeben wird der Abstieg in dem Zyklus, in dem der Speicher
  das nachweislich nicht mehr kann: keine Stützung autorisiert, Haltezone (PV
  = Haus + Verbraucher, der Akku speist die Wallbox dort nicht),
  Notstromreserve, PV-only-Klasse oder `wbminsoc` erreicht, Entladegrenze
  unter dem Bedarf der Mindestladung, Hausanschluss je Phase (auch bei
  unbekannter Hausanschlussgrenze, weil der Abstieg die Last senkt), Bezug über
  der Speicherreichweite oder Bezug über der Toleranz in jeder gültigen Probe
  der Einschwingfrist (eine bezugsfreie Probe beginnt die Frist neu;
  pulsierende Lasten, deren Pausen der Speicher ausgleicht, geben nicht frei;
  gezählt werden Liveproben nach ihrem Zeitstempel, nicht Regelzyklen – eine
  wiederholte Probe, etwa bei hängendem Livedienst, gilt weder als Bezug noch
  als Pause).
  Stützungsfreigabe und Grund liest die Wallbox aus dem Speicherrahmen, wenn
  der Speicherregler sie in diesem Zyklus setzt, sonst aus ihrer eigenen
  Kurvenklasse. Unbekannte Reichweite, Entladegrenze oder unbekannter Bedarf
  (ungültige PV- oder Hauswerte) sind kein Freigabegrund; dann gibt nur der
  gemessene Bezug über die Einschwingfrist frei. Die Freigabe wird wie oben
  vorgemerkt, gilt nur für die Phasenreservierung dieses Wechsels und wird mit
  dem angelaufenen Phasenwechsel eingelöst: Die Sequenz läuft dann im Fenster
  vollständig (0 A, `phasetarget` 1p, Wiederanlauf mit dem Mindeststrom), ohne
  weiteren Stopp. Ohne Freigabe erreicht kein 0-A-Schritt einer Phasensequenz
  die Box. Gibt die Defizitkaskade den Abstieg auf, bevor die Sequenz einen
  Ausgang hatte (etwa weil die Defizitepisode endet), wird die zugesagte
  Reservierung ohne Hardwareausgang freigegeben; die 480-s-Sperre nach einer
  Umschaltung bleibt unberührt. Ohne gültige Livewerte bleibt die Sperre;
  Hochschaltungen folgen weiter nur der Regel nach bestätigter Ladung.
  Diagnose:
  `openwb_pro_start_window_output_gate` mit Grund
  `phase_down_released_battery_cannot_support` und den auslösenden Gründen.
- Diagnose: `wb_details[].openwb_pro_start_window` (Zustand, Anker, Angebot,
  Zyklus, Haltegründe), `openwb_pro_start_window_output_gate`,
  `openwb_pro_session_phase_latch`, Statusfelder
  `openwb_pro_start_window_*`; Persistenz in
  `wallbox_phase_transition_state.json` je Stecksession.
- Laufende Ladung beim Anstecken-Zustand: Läuft in `budget_wait` bereits
  eine Ladung (zwei Frames > 500 W bei stehendem Box-Angebot, etwa nach
  Manager-Neustart, Force-Start oder einer Lücke > 300 s), wird das Angebot
  übernommen: deckt das Budget es (≤ clamp),
  läuft das Fenster als `charging_confirmed`; liegt es darüber, gilt sofort
  `regulating`, damit die Regelung ohne Startfenster absenken darf. Vor dem
  eigenen Angebot sind Absenkungen ≥ 6 A bei > 500 W sofort erlaubt, 0 A
  nie. Weiche EMS-Stopps (Priorität, Pre-Dump, wbminSoC, Zuteilung,
  Nullbudget) halten auch als typisierte Stopps im Fenster; harte Gründe
  stoppen unverändert. Der Fast-Pfad und das Executor-Gate lesen vor der
  Materialisierung den persistierten Fensterkern derselben Stecksession
  (konservativ: keine Anhebung, kein 0 A; Absenkungen unter das frisch
  zurückgemeldete Angebot passieren wie oben). Der Kern wird bei jedem
  Zustandsübergang und im aktiven Fenster alle 120 s gesichert, damit der
  Fensterrest einen Neustart überlebt.

#### Verhalten nach dem Abstecken

Die openWB Pro behält nach dem Abstecken ihren letzten Sollstrom
(`connect.php` ist pegelgesteuert). Stand zuletzt ein Angebot ab 6 A an, lädt
ein Fahrzeug beim nächsten Anstecken sofort, bevor die Regelung entscheidet;
nach einem eigenen Stopp stehen dagegen 0 A an. Der Schalter
`wb<n>_openwb_pro_unplug_offer` legt das Verhalten je Ladepunkt fest
(Config-Editor: „Nach dem Abstecken“):

- `safe` (Standard, Sicherheitsvariante): nach der bestätigten Trennung
  einmalig 0 A. Beim Anstecken startet nichts, bis die Regelung freigibt.
- `fast_start` (Schneller Start): nach der bestätigten Trennung einmalig 6 A,
  auch nach einem vorherigen eigenen Stopp. Das Fahrzeug startet beim
  Anstecken sofort, auch aus Akku oder Netz; danach übernimmt die Regelung das
  stehende Angebot wie oben beschrieben (Übernahme, Startfenster, Kontingent).
  Bis dahin läuft dieser Start ohne Zuteilung: Hausanschluss, eine zweite
  ladende Wallbox und das Budget berücksichtigt erst die Regelung, sobald sie
  das Angebot übernimmt.
- Ein fehlender oder ungültiger Wert gilt als `safe`.

Regeln:

- Auslöser ist ausschließlich die bestätigte Trennung (frische, ausdrücklich
  getrennte Frames über die Entprellzeit). Kurzes Ab- und Anstecken innerhalb
  dieser Zeit sowie fehlende, veraltete (älter als 10 s) oder ungültige
  Statusdaten lösen nichts aus.
- Vor jedem Senden und vor der Bestätigung gelten dieselben Trennungsbelege
  wie für die Trennung selbst: Schloss, Rohstecker und Fahrzeugcode melden
  keine Verbindung, und es fließt nichts (Ladeflag, Leistung, Phasenströme).
  Bei einem Widerspruch wird gewartet, ohne einen Versuch zu verbrauchen.
- Gesendet wird genau ein Strombefehl je Trennung und nur, wenn die Box ein
  anderes Angebot als das Ziel zurückmeldet. Passt die Rückmeldung danach
  nicht, folgen höchstens zwei Wiederholungen im Abstand von mindestens 20 s,
  danach nur noch Diagnose. Keine Phasenbefehle, keine CP-Unterbrechung.
  Blockiert ein Tor den Befehl, bevor die Box erreicht wird (etwa
  Speicher-Hard-Block, belegter Ausgangsslot, HA-Rollentor), verbraucht das
  keinen Versuch; Grund `output_blocked:…`, neuer Versuch frühestens nach 30 s.
- In `Aus`, bei Sperre, im Beobachtungsmodus, während NOT-AUS oder wenn das
  HA-Rollentor den Ausgang sperrt, wird nichts gesendet; der Auftrag wartet
  dann und sendet nach dem Verlassen von `Aus` bzw. der Sperre, solange die
  Box getrennt bleibt. Eine manuelle Pause endet bereits mit dem ersten
  getrennten Frame und hält den Auftrag daher in der Regel nicht auf.
  Budget-, Zuteilungs- und Startfenster-Regeln gelten
  für dieses Stehangebot nicht, weil an der getrennten Box keine Leistung
  fließt. Hausabsicherung, Leistungsgrenzen und Budget regelt die normale
  Logik ab dem Anstecken.
- Wird das Fahrzeug vorher wieder angesteckt, entfällt der Auftrag.
- Der Auftrag wird im Phasenzustand gespeichert
  (`wallbox_phase_transition_state.json`). Nach einem Neustart des
  Wallbox-Managers wird zuerst die Rückmeldung der Box geprüft; meldet sie das
  Ziel, wird nichts erneut gesendet.
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

* **Formeln** (k = zugeordnete Netzphase aus `wb<n>_grid_phase`, m = Bezug je
  Phase in A, I_wb = gemessener Wallbox-Strom, PF = `wb_pcc_power_factor_margin`):
  Fremdlast `I_fremd = max(0, m_k − I_wb)`, wirksamer Bezug
  `I_eff = min(m_k, I_wb) + I_fremd / PF`, Headroom
  `H_k = (Sicherung_k − Reserve_k) − I_eff` (vorzeichenbehaftet: bei Überlast
  fällt der Deckel sofort unter den Ist-Strom), `cap_sich = I_wb + H_k`.
* **Harte Schranken:** Nutzergrenze `wb<n>_openwb_pro_1p_max_amp`,
  Wallboxgrenze, Betriebslimit Sicherung − Reserve; ganze Ampere, unter 6 A
  → 0 A (Wallbox pausiert, Grund `phase_headroom_exhausted`).
* **Schieflast-Wächter:** `cap_imb = max(20 A, I_wb + (grid_pcc_imbalance_max_a −
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
  ungültiges Limit/Reserve, Nachweis `pending`/`unverified`,
  Wurzelzähler-Widerspruch (`Grid_PM_Delta_Rule_Effective`), ungültige oder
  über 10 s alte Netzphasen-Messung (`_ts`), ungültiger/über 10 s alter
  openWB-Status, kein Live-Snapshot im Zyklus (`pcc_measurement_basis_missing`),
  Ausnahme im Vertrag (`contract_exception`).
* **Rampe:** Anhebung +1 A je Regelschritt (`wb_stable_follow_hold_s`,
  mindestens 4 s) und nur bei Headroom ≥ 1,5 A; Absenkung sofort, danach
  dreifacher Nachlauf ohne Anhebung. Neue Stecksession, bestätigter
  Phasenwechsel und jeder Fail-closed-Zyklus starten wieder bei 20 A
  (20 → 32 A in rund 48–60 s).
* **Software-Nachweis der Zuordnung:** E3DC-Control bestätigt
  `wb<n>_grid_phase` beim einphasigen Laden selbst. Aus Plateaus des
  Wallbox-Stroms (≥ 3 Stichproben über ≥ 10 s, Spannweite ≤ 0,5 A) prüft es
  statisch `P_load_k / (I_wb · U_k) ≥ 0,8` (ab 8 A) und an Stromsprüngen
  ≥ 3 A, ob die Laständerung `ΔP_j / (ΔI · U_j)` auf der zugeordneten Phase
  erscheint (0,6–1,4) und auf den anderen nicht (< 0,5). Ladestart/-stopp
  (Sprung ≥ 10 A) zählt doppelt. `verified` verlangt Statik ok und Sprünge
  mit Gewicht ≥ 2 (mehr als das Doppelte der Widersprüche); `unverified`
  entsteht nur aus Widersprüchen auf ein und derselben Fremdphase (Gewicht ≥ 2
  oder ein Widerspruch plus 60 s Statik-Fehlschlag mit Trägerphase) und
  bleibt bis zu einer Konfigurationsänderung bestehen (Dashboard rot:
  „Phasenzuordnung WB2 stimmt nicht: Last auf L1 statt L3“). Ein reiner
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
* **Energie-Phasenpolitik:** „Strom zuerst, dann Phasen“ bewertet die eine
  Phase gegen den wirksamen Deckel (Vertrag, sonst min(20 A, statisch),
  zusätzlich ein bestätigtes 1p-OBC-Limit des Fahrzeugs), nicht gegen den
  statischen Nutzerwert; für die Hochschaltung zählt der Deckel ohne den
  Rampengrund (Rohdeckel), sonst wäre „ausgereizt“ in jeder Anhebung trivial
  wahr; für ein phasenschaltfähiges Paar gilt der kleinere Referenzstrom aus
  Deckel und 3p-Minimum plus Puffer. Das Export-Wh-Konto (Standard 120 Wh)
  füllt sich mit dem Überschuss oberhalb des Referenzstroms; Vorlauf 60 s oder
  volles Konto, Sperre 480 s nach dem Wechsel (siehe „Hochschaltung 1p→3p nach
  evcc/openWB-Muster“).
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
  Tooltip der Zeile, z. B. „1p-Deckel WB1 27 A: Sicherung 38 A − Fremdlast
  L3 11 A (Bezug 27 A, 230 V) · Zuordnung bestätigt“ („Sicherung“ ist das
  Betriebslimit Hausabsicherung − Reserve, im Beispiel 40 A − 2 A; mit
  `grid_wallbox_reserve_amps = 10` stünde dort 30 A).
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
Normal/Rückgabe -> set_chargemode=pv
Aktiver Eingriff -> chargecurrent=<Ampere>, set_chargemode=instant
Schutz/Stop      -> set_chargemode=stop
```

Dieser Pfad ist ein Opt-in und kein automatischer Default. Besonders bei einer
extern eingebundenen openWB Pro ist er nicht als Pro-Fernsteuerung gedacht. Wenn
E3DC-Control die Pro wirklich führen soll, die Pro direkt als
`openWB Pro (connect.php)` konfigurieren.

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
| `E3DC-Startimpuls wiederholt: 32A bei 0W` | Freigabe steht, das Fahrzeug nimmt keinen Strom an. | Fahrzeug schläft oder hat die Session beendet; E3DC kennt keinen CP-Interrupt über RSCP, meist hilft nur Neustecken. |
| `Netzladefenster beendet: Wallbox gestoppt` | Geplanter Slot vorbei, Stop-Toggle gesendet. | Keine. |
| `Netzladefenster beendet: Fahrzeug hat im Fenster nicht geladen …` | Freigabe stand, es wurde nie geladen; Freigabe auf Mindeststrom ohne Toggle zurückgenommen. | Keine; ein schlafendes Fahrzeug wird nicht angestoßen. |
| `Flankengate: stop_toggle_downgraded` | Stop angefordert, aber ohne belegtes Laden wird kein Toggle gesendet. | Keine; Diagnosehinweis. |
| `wb_pv_budget.json veraltet (Ns) – drossle WB auf 6A` | Storage Manager hat 15–45 s nichts geschrieben. | Storage-Manager-Journal prüfen, wenn es sich häuft. |
| `wb_pv_budget.json Timeout (Ns) – stoppe WB` | Storage Manager > 45 s ohne Budgetdatei. | Storage Manager läuft nicht oder hängt; Dienst prüfen. |
| `wb_pv_budget.json Lesefehler überbrückt (…)` | Lesezugriff traf den atomaren Austausch; der letzte gültige Stand zählt weiter. | Keine; nur die Häufigkeit ist ein Hinweis auf stark ausgelastete SD-Karte/CPU. |
| `wb_pv_budget.json nicht lesbar und kein gültiger Vorstand` | Datei seit Prozessstart nie gültig gelesen. | Ramdisk und Storage Manager prüfen. |
| `Plan verbraucht` / `Ladeplanung auf 0 gesetzt` | Manueller Ladeplan ohne Wiederholung ist abgearbeitet. | Bei täglichem Bedarf „Täglich wiederholen“ einschalten. |
