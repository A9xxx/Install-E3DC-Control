# Speicher-Ladesteuerung - Systemablauf

## Anzeige der E3DC-Regelung

**E3DC führt: AUTO** bezeichnet den autonomen Freilauf ohne angeforderte
Speicherbegrenzung durch E3DC-Control. Bei aktivem DC-Laderahmen heißt die Anzeige
**E3DC führt: DC only**: E3DC regelt autonom, während E3DC-Control den Laderahmen
anhand der E3DC-PV-Leistung begrenzt. Ist zusätzliches Laden aus dem Überschuss
des Zusatzwechselrichters freigegeben, erscheint **DC + Zusatz-PV**.

Die Erläuterung unterscheidet eine bestätigte Ladegrenze von einer noch offenen
Geräterückmeldung. Fehlt eine gültige Aufteilung der PV-Leistung, wird das
ausdrücklich angezeigt. Eine Ladegrenze in Watt ist keine gemessene Ladeleistung.
„DC only“ beschreibt die Ladestrategie; der technische E3DC-Modus bleibt AUTO.
Die Anzeige ist kein messtechnischer Nachweis der Herkunft jedes geladenen Watts.

## Automatisches Netzladen bei zeitvariablem Tarif

Für normales preisabhängiges Speichernachladen müssen der **netzdienliche
Eco-Modus** und **Speicher-Netzladen** eingeschaltet sein. Der separate
Negativpreis-Boost und die früheren aWATTar-Optionen ersetzen diese Freigaben
nicht. Ein günstiges Zeitfenster allein startet noch keine Ladung: Prognose,
nutzbarer Speicherinhalt, Preisvorteil, Verluste und Mindestlademenge bestimmen
den Bedarf. Innerhalb eines geeigneten Fensters kann der Plan bis zum
berechneten spätesten Ladebeginn warten.

Die Deckungsprüfung beachtet die zeitliche Reihenfolge und die
Speicherkapazität. Erwartete PV am nächsten Nachmittag kann eine vorherige
Versorgungslücke in der Nacht nicht decken. Reichen Speicher und rechtzeitig
verfügbare PV aus, bleibt das normale Netzladen aus.

Der Netzladebedarf wird zeitgerichtet bestimmt: Er reicht nur bis zum nächsten
nutzbaren günstigen Preisfenster; was dieses Fenster liefern kann, wird
angerechnet. Die für das laufende Fenster erwartete PV-Erzeugung wird zuerst
berücksichtigt; aus dem Netz wird nur der Rest geladen, der zum Fensterende
noch fehlt. Das Netzladen liegt deshalb am Ende des Fensters, davor lädt der
Speicher aus PV und bleibt entladegesperrt; nach dem spätesten Ladestart wird
aus dem Netz geladen, auch wenn PV gerade lädt. Ladeziel und Job folgen dem
Sollbestand am Fensterende (Bedarf bis zum nächsten nutzbaren Fenster plus
Puffer) und bleiben im Fenster stabil; deckt der erwartete Bestand den
Sollbestand, bleibt das Netzladen aus. Für die Ladedauer wird die
tatsächlich beobachtete Ladeleistung des Speichers verwendet. Ist das Ziel
erreicht, wird im selben Fenster nicht erneut aus dem Netz geladen (Hysterese
1 %, `market_target_hysteresis_pct`). Bei Tariftypen mit festem Zeitfenster
(z. B. Octopus Heat) ist das konfigurierte Tarifzeitfenster die Regelgröße,
nicht der Börsenpreis. Hinweis: Eine negative Sicherheitskorrektur
(`market_safety_correction_ct_per_kwh`) kann auch Grundtarifstunden
rechnerisch lohnend machen und erhöht damit das Ladevolumen. Das Ladeprofil
(Wirtschaftlich / Ausgeglichen / Komfort / Eigene Einstellungen) bestimmt
Marge, Sicherheitskorrektur und Preislimit; Komfort ersetzt die
PV-Autarkie-Sperre durch das Zeitziel (siehe Börsenpreis-Optimierung,
Abschnitt „Ladeprofil“).

Bei **Octopus Heat** und einem konfigurierten **Spezialtarif** sind die täglich
wiederkehrenden Abrechnungspreise über den vollständigen Planungshorizont
bekannt. Fehlende morgige Börsenpreise verkürzen diese Tarifachse nicht.
Börsenpreise für Direktvermarktung bleiben davon getrennt; aus einem
konfigurierten Kundentarif wird kein Börsenpreis abgeleitet.

## Speicher für teure Stunden halten

Mit **Speicher halten** kann der Haushalt in günstigeren Stunden Strom aus dem
Netz beziehen, während vorhandene Batterieenergie für spätere teure Stunden
verbleibt. Dafür müssen Eco-Modus und Halten freigegeben sein. Die Planung
berücksichtigt auch den Verbrauch bis zur Preisspitze und rechtzeitige PV.
Füllt PV den Speicher vorher ohnehin wieder auf, entfällt dieser Haltevorteil.

Die Planung verteilt die verfügbare Batterieenergie über den zusammenhängend
bekannten Preisabschnitt. Sie berücksichtigt den prognostizierten Verbrauch,
rechtzeitig nutzbare PV, Speicherkapazität und Lade-/Entladeleistung. Dabei soll
vorhandene Energie möglichst viel Bedarf decken und bevorzugt teuren Netzbezug
vermeiden. Eine spätere günstigere Haltemöglichkeit wird mitbewertet.
Die Ladekurve ist dabei ein Ladeziel; sie wird nicht als zusätzliche harte
Entladereserve für die Hausversorgung verwendet. Die gebundene Schutzreserve
bleibt unangetastet.

Wird nur ein Teil der aktuellen Viertelstunde zum Halten benötigt, endet der
Halteauftrag entsprechend früher. Dieser Zeitanteil wird aus der prognostizierten
Last und möglichen Entladeleistung berechnet; er ist keine gemessene Wh-Abschaltung.
Neue Messwerte und Prognosen werden bei der Neuplanung berücksichtigt. Fehlende
oder ungültige Eingaben geben keinen Halteauftrag frei. Der Storage Manager prüft
weiterhin die aktuellen Schutzgrenzen und die Gültigkeit des Auftrags.

**Speicher-Netzladen** schließt die Freigabe **Speicher halten** automatisch ein.
Der Halten-Schalter erscheint eingeschaltet und lässt sich bei aktivem Netzladen
nicht ausschalten. Beim Speichern werden beide Freigaben übernommen. Auch ältere
Konfigurationen mit Netzladen an und Halten aus werden entsprechend ausgewertet.
Wird Netzladen ausgeschaltet, bleibt Halten separat wählbar und zunächst an;
für vollständig ausgeschaltete Markt-Speicherpfade beide Schalter ausschalten.
Dies ist eine Freigabe, kein dauerhafter Halteauftrag: Ohne zeitlichen Bedarf
und wirtschaftlichen Vorteil wird der Speicher nicht gehalten.

Halten benötigt keine Mindestlademenge und gibt selbst kein Netzladen frei.
Reicht die vorhandene Energie trotz Halten nicht aus, wird zusätzliches
Netzladen nur mit eigener Freigabe und nach seinen Bedarfs- und Preisprüfungen
geplant. Geänderte Lasten oder Prognosen können die Planung im Betrieb ändern.

## Manuelles Laden aus PV und Netz

Ein bewusster manueller Ladeauftrag darf den Speicher auch an oder unterhalb
der Notstromreserve aufladen. Entladen und Export bleiben an dieser Grenze
gesperrt. Bei fehlenden gültigen Leistungsdaten oder fehlendem freien
Hausanschlussrahmen wartet der Auftrag. Inselbetrieb und ausgeschaltete
Speicherregelung geben kein manuelles Netzladen frei.

Ziel-SoC und Ablaufzeit beenden den manuellen Auftrag. Ein gespeicherter
Ladeauftrag beziehungsweise eine angeforderte Leistung bestätigt noch keine
physische Batterieladung; dafür ist die gemessene Batterieleistung maßgeblich.

## Beobachtete Wallbox und Speichergrenzen

Bei „Nur Beobachten, Wallbox regelt“ bleibt die Wallbox selbstständig. Ein
Preisfenster kann trotzdem die Batterieentladung für diese Last begrenzen.
Bei aktiver, erreichbarer Speicherladekurve wird die Ladegrenze dabei weiterhin
aus dem Kurvenbedarf berechnet. Der Beginn einer beobachteten Ladung öffnet
also nicht automatisch die maximale Batterieladeleistung.

Lade- und Entladegrenze bleiben unabhängig: notwendiges PV-Laden, harte Reserven
und Abregelschutz haben Vorrang; eine Entladesperre erteilt keine Netzladefreigabe.
Expliziter E3DC-Autobetrieb und Betrieb ohne gültiges Kurvenziel behalten ihre
bisherige Freigabe. Reservierte Kurvenladeleistung steht nicht zugleich als
zusätzliches Verbraucherbudget bereit.

## Speicherregelung ausschalten

In der Konfiguration steht oben der Schalter **Speicherregelung aktiv**, in
der einfachen und erweiterten Ansicht sowie auf dem Mobilgerät. Die Änderung
wird sofort gespeichert; der laufende Speicherregler übernimmt sie im nächsten
Zyklus. Bestehende Installationen sind ohne Änderung weiterhin eingeschaltet.

Beim Ausschalten gibt der laufende Regler seine Speicherlimits einmal frei.
Die Anzeige unterscheidet die gespeicherte Einstellung von der bestätigten
Limitfreigabe. Danach sendet E3DC-Control keine Speicherbefehle mehr. Auch
manuelles Laden und Entladen, Pre-Dump, preisgesteuertes Laden, Direktverkauf
aus der Batterie und die Speicherstützung für Verbraucher sind dann inaktiv.
Messwerte, Aufzeichnung, Prognose und Oberfläche laufen weiter. Die Ladekurve
bleibt eine Prognose; bei ausgeschalteter Speicherregelung wird der Speicher
nicht entlang dieser Kurve geregelt.

Wallbox, Wärme und die separate Steuerung eines Zusatzwechselrichters behalten
ihre eigenen Einstellungen. Der gemeinsame Verbraucherregler arbeitet weiter
mit gemessenem Überschuss und bereits laufenden Verbraucherlasten. Er fordert
dafür keine zusätzliche Batterieentladung an. Tatsächliche Batterieladung wird
nicht noch einmal als freies PV-Budget angeboten.

**Aus ist keine Batteriesperre:** Das E3DC beziehungsweise ein externer Regler
übernimmt die Speichersteuerung. Für eine Visualisierung des eigenen zweiten
S10 bleibt diese Instanz daher normal mit ihrem eigenen E3DC verbunden; eine
Shadow-Simulation mit Daten einer anderen Instanz ist dafür nicht erforderlich.

Vor dem Start eines externen Speicherreglers die bestätigte Limitfreigabe
abwarten. Ein früherer aktiver Leistungsbefehl kann noch bis zum geräteseitigen
Timeout nachwirken. Die Freigabebestätigung belegt die Rückgabe der Limits,
nicht eine bestimmte physische Lade- oder Entladeleistung.

Startet der Dienst bereits ausgeschaltet, sendet er auch keine Freigabebefehle:
Ein inzwischen übernehmender externer Regler bleibt unangetastet. Fehlt eine
frühere Bestätigung oder wurde die Freigabe unterbrochen, zeigt die Oberfläche
das ausdrücklich an. Eine unbestätigte Freigabe wird nicht automatisch
wiederholt; vorhandene Gerätevorgaben müssen vor einer externen Übernahme am
E3DC geprüft werden. Bei fehlender HA-Schreiberberechtigung erfolgt ebenfalls
kein Freigabebefehl.

Beim Wiedereinschalten gelten die aktuellen Einstellungen und frische
Gerätedaten. Alte manuelle Ladebefehle und Speicherlimits aus der Pause werden
nicht wieder aufgenommen. Technisch wird der Schalter als
`storage_regulation_enabled` (`1`/`0`, Standard `1`) gespeichert; der Web-Schalter
bindet die Übernahme mit `storage_regulation_changed_ts` an die aktuelle
Bedienaktion. Ein Dienststopp allein ersetzt diese geordnete Abschaltung nicht.

> **Stand:** v5.5.3c
>
> **Neu in 5.4.5a:** Ein frisch beobachteter openWB-Fahrzeug-SoC kann mit
> Quelle und Alter rein lesend erscheinen, wenn er zur aktuellen Stecksession
> oder zu einem eindeutig passenden Fahrzeugprofil gehört. Die Beobachtung
> bestätigt keinen Regel-SoC und öffnet weder Ziel-SoC, `Auto voll`, Planung
> noch Hardwareausgang. Dafür gelten weiterhin die getrennten bestätigten
> Session- und Fahrzeugverträge.
>
> **Neu in 5.4.5:** Ein zentraler Wattvertrag bindet die je Wallbox
> finanzierte Quelle, das physische Stromquant und den finalen Aktorauftrag.
> Echter Netzbezug wird pro frischem Netzpunkt-Snapshot gruppenweit genau
> einmal in den Wh-Wächter übernommen; ohne Netzbezug kann nur eine frisch
> belegte Überschreitung des autorisierten Wallboxbudgets zählen. Die
> Abregelung folgt Stromstärke, gegebenenfalls Phasenwechsel und erst zuletzt
> Stopp. Treiber übersetzen den finalen Auftrag, treffen aber keine eigene
> Lade- oder Speicherentscheidung. Eine fehlende oder veraltete Messung
> erzeugt weder Zusatzbudget noch eine erfundene Leistung.
>
> **Neu in 5.4.4i:** Ein manuell gespeicherter Fahrzeug-SoC bleibt an echten
> Aktionszeitpunkt, genau ein Profil sowie aktuellen Wallbox- und Steckkontext
> gebunden; die reine Lesekorrektur erzeugt keinen Hardwarebefehl. In
> `PV-Kurve ruhig` hält eine laufende Ladung bei kleinem Leistungsdefizit den
> physischen Mindeststrom, bis der bestehende Energiezähler den konfigurierten
> Wh-Rahmen erreicht. Eindeutiger Netzbezug und harte Schutzgrenzen bleiben
> vorrangig. Eine zentrale
> Prioritäts- oder Schutz-Null versiegelt den Strom- und
> Phasenausgang für den Zyklus. EFY-Herstellerautonomie, direkt
> kommandierbare Phasen und elektrische Phasenreserve bleiben getrennt; eine
> physisch bestätigte einphasige EFY-Umschaltung wird nicht behauptet.
> Nachgelagerte Legacy- oder Treiberrampen begrenzen ein bereits zentral
> entschiedenes Ziel nicht erneut. Eine Erhöhung bleibt an frischen
> Gerätezustand, verfügbares Budget und Schutzgrenzen gebunden. Reale flexible
> Verbraucherlast, gebundener Startvorgang und bloßer Startwunsch werden
> getrennt bilanziert, damit eine inaktive Wärmepumpe nicht dauerhaft
> Wallboxbudget reserviert.
>
> **Neu in 5.4.4g:** Fahrzeug-SoC wird in Planner, Tracker, Manager und
> Weboberfläche an die echte Quelle, deren Ereigniszeit sowie Fahrzeug-,
> Wallbox-, Steck- und Profilidentität gebunden. Eine unvollständige oder rein
> beschreibende Direktvermarktungstrajektorie verdrängt die Standard-PV-,
> Standard-SoC- und kWh-Prognose nicht. E3/DC-only verwendet Erzeugung eines
> Zusatzwechselrichters ohne ausdrücklich freigegebene AC-Speicherroute nicht
> als internen DC-Laderahmen. Diese Anzeige- und Bindungskorrekturen öffnen
> keinen zusätzlichen Hardwarebefehl.
>
> **Neu in 5.4.4f:** Im Modus `PV-Kurve ruhig` wird belegter physischer
> PV-Überschuss bereits vor der sanften Anfahrrampe berücksichtigt. Speicher-,
> Wallbox-, Hausanschluss-, Fahrzeug- und Hardwaregrenzen bleiben wirksam; es
> entsteht weder eine Netzladefreigabe noch eine zusätzliche
> Batterieentladefreigabe. Die Weboberfläche unterscheidet fehlende,
> unvollständige oder planfremde SoC-Prognosen und zeigt eine ersetzende
> Direktvermarktungsaktion als geplant, angefordert oder bestätigt wirksam.
> Der separat belegte batterieneutrale PV-Anteil wird dabei nicht erneut durch
> einen bereits von der Speicheraufnahme beeinflussten Netzpunktwert
> verkleinert. Eine zentral freigegebene einphasige Ladung fällt dadurch nicht
> allein wegen dieses zusätzlichen Filters wieder unter ihre Mindestleistung.
> Wird ein einzelner unplausibler Messwertsatz aus Sicherheitsgründen
> verworfen, kann das Wallboxbudget kurz `0 kW` anzeigen. Das ist nicht
> automatisch ein Ladeabbruch; mit dem nächsten gültigen Messwertsatz wird neu
> geregelt. Wiederholte Nullwerte bleiben ein Diagnosehinweis.
>
> **Hinweis zu 5.4.4e:** Dieses Release korrigiert ausschließlich den
> Webupdate- und Rücklaufpfad für die produktive RAM-Disk. Speicherentscheidung,
> Direktvermarktung, Wallbox- und Wärmepumpenbudgets, RSCP-Ausgang und
> Hardwaregrenzen entsprechen unverändert 5.4.4d.
>
> **Neu in 5.4.4d:** Das gemeinsame Wallboxziel wird in allen von E3DC-Control
> geführten Lademodi durch die konfigurierte Hausabsicherung abzüglich
> Wallbox-Reserve begrenzt. In `Aus / autonom` muss dieselbe Grenze in der
> Wallbox beziehungsweise im Ladeprofil hinterlegt sein, weil E3DC-Control
> dort nach der Übergabe keine Strombefehle mehr sendet.
> Optionale Grenz- und Reservewerte je Phase werden phasenbezogen angewendet;
> bei unbekannter einphasiger Zuordnung gilt der ungünstigste Fall. Die Reserve
> bildet den statisch konfigurierten Abstand für andere Verbraucher. Für den
> Gruppen-Phasenclamp bleibt eine aus Wirkleistung durch `P/230` abgeleitete
> Stromangabe rein diagnostisch; nur der einphasige Deckel der openWB Pro
> nutzt den Bezug je Netzphase aus dem E3DC-Wurzelzähler geteilt durch die
> Wechselrichter-Spannung als freigegebene Messbasis (siehe
> `doc/Native_Wallbox.md`). Das Speicherbudget bleibt für die Wallbox bindend;
> die eigene, im Hauswert nachlaufende Ladeleistung kann es für einige Zyklen
> einbrechen lassen, weil die Speicherseite die Wallboxleistung erst mit dem
> nächsten Messrahmen aus dem Hauswert herausrechnet. Das Startfenster des
> Wallbox-Managers hält das Stromangebot der openWB Pro über solche Einbrüche
> hinweg (kein 0 A aus Budgetgründen im Fenster).
> Fehlt `grid_max_amps` oder ist der Wert leer, gelten 35 A je Phase. Der
> Standard 35 A gilt nur für den Skalarpfad (3p-Gruppenbudget); die einphasige
> Freigabe > 20 A an einer openWB Pro verlangt eine ausdrücklich eingetragene
> Hausabsicherung. Mehrere
> Ladepunkte teilen diesen Phasenrahmen ohne pauschale Gleichteilung. Ein
> gebundenes einphasiges Fahrzeugprofil bestimmt die reale Lastphase; ohne
> Fahrzeugbeleg bleibt die feste Wallboxtopologie maßgeblich.
> Wird `wbminsoc` während der Ladung über den aktuellen Speicher-SoC angehoben,
> endet die Akku-Unterstützung im selben Zyklus. Nur das batterieneutrale
> PV-Budget darf die Ladung weiterführen; unterhalb der phasenabhängigen
> Mindestleistung wird gestoppt. Start-Holds dürfen einen Fehlbetrag nur dann
> ergänzen, wenn er vollständig durch verringerte Speicherladung oder eine
> ausdrückliche Batterie-Freigabe finanziert ist.
>
> **Hinweis zu 5.4.4c:** Dieses Release korrigiert ausschließlich den Update-
> und Reparaturpfad. Speicherentscheidung, Direktvermarktung, Wallbox- und
> Wärmepumpenbudgets, RSCP-Ausgang und Hardwaregrenzen entsprechen unverändert
> 5.4.4b.
>
> **Neu in 5.4.4b:** Im Sonnenmodus bleibt der batterieneutrale
> PV-Überschuss ein unantastbarer Wallbox-Mindestrahmen. Das Storage-Budget
> autorisiert ausschließlich darüber hinausgehende Leistung; Pre-Dump liefert
> diese Batterieentladung als typisierten, frischen
> `predump_discharge_add_contract_v1`, damit PV-Anteil und Entladezusatz nicht
> doppelt gezählt werden. Bei aktiver Wallbox bindet die parallele
> Speicherführung den AUTO-Laderahmen an `iFc`, ohne die Entladestützung zu
> sperren. Die Verbraucherpriorität weist einen nicht vollständig finanzierbaren
> Wärmepumpen-Startwunsch samt tatsächlichem nachrangigem Empfänger aus.
>
> **Neu in 5.4.4:** Bei aktiver Direktvermarktung wird genau ein effektiver
> Speicherplan aus Plan, Slot, Aktion, Zeitfenster, Owner und bestätigtem
> Phase-5-Lebenszyklus angezeigt. Ausstehende oder widersprüchliche Wirkung
> leert klassische Zielkurve, Leistung und Erreichbarkeitsbehauptung. Der
> WB-Entladungsschutz verlangt zugleich frische gemessene Fahrzeuglast;
> Netzladen der Wallbox benötigt eine eigene aktuelle Freigabe. Weder Anzeige
> noch Wallboxfreigabe erzeugen einen zweiten RSCP-Ausgang.
>
> **Hinweis:** 5.4.3p korrigiert ausschließlich den Eigentümer der vom
> Download-Bootstrap neu erzeugten Git-Metadaten. Speicherentscheidung,
> Direktvermarktung, RSCP-Ausgang und Hardwaregrenzen bleiben gegenüber
> 5.4.3o unverändert.
>
> **Hinweis:** 5.4.3o bindet den sicheren passiven
> Direktvermarktungs-Ladeblock auch bei einem bewusst kandidatlosen Planslot
> ausschließlich an Plan, Slot, DV-Owner und den tatsächlich übersetzten
> 0-W-Ausgang. Der Kandidat bleibt diagnostisch; Laden, Entladen,
> RSCP-Ausgang und Hardwaregrenzen erhalten keine zusätzliche Autorität.
>
> **Hinweis:** 5.4.3n korrigiert ausschließlich den Metadatenvertrag des
> kanonischen Rollenankers im privilegierten Backup- und Recoverypfad.
> Speicherentscheidung, Direktvermarktung, RSCP-Ausgang und Hardwaregrenzen
> bleiben gegenüber 5.4.3m unverändert.
>
> **Hinweis:** 5.4.3m härtet den versiegelten normalen Ziel-Updater, dessen
> Notifier-/Recovery-Drop-in-Vertrag und den openWB-Pro-Phasenautomaten. Eine
> reine Wallbox-Budgetreservierung startet keinen Phasen-Cooldown; eine alte
> Ausgangsgeneration wird vor einem neuen Storage-Grant ausgewertet. Der neue
> Sofortauftrag erzeugt selbst weder ein Budget noch einen Hardwareausgang.
> Speicherentscheidung, RSCP-Ausgang und Hardwaregrenzen bleiben gegenüber
> 5.4.3l unverändert.
>
> **Hinweis:** 5.4.3l härtet ausschließlich den updater-eigenen Git-Rückweg,
> die eng freigegebene Migration einer historischen Storage-Manager-Unit und
> den Startschutz nach einem synchron erkannten Recoveryfehler. Das betrifft
> den systemd- und Dateivertrag des Updates, nicht die fachliche
> Speicherregelung. Speicherentscheidungen, RSCP-Ausgänge und
> Hardwaregrenzen entsprechen unverändert 5.4.3k.
>
> **Neu in 5.4.3:** Speicherreserve, Sollkurve, Direktvermarktung und
> Verbraucherbudgets bleiben getrennt. Ein gemeinsamer Ownervertrag bindet die
> finale Speicherentscheidung bis zum Hardwareausgang; fehlende, veraltete
> oder widersprüchliche Rückmeldungen öffnen keinen zusätzlichen Lade- oder
> Entladerahmen. Plan, freigegebene Aktion und tatsächliche Wirkung werden
> getrennt dargestellt.
>
> **Hinweis:** 5.4.2d ändert ausschließlich den Update- und
> Wiederherstellungspfad. Speicherentscheidungen und Hardwareausgänge
> entsprechen unverändert 5.4.2c.
>
> **Dateien:** `Installer/storage_simulator.py`,
> `Installer/storage_manager.py`, `Installer/storage_parallel_regulator.py`
>
> **Dienste:** `e3dc-storage-simulator.service`,
> `e3dc-storage-manager.service`

## Überblick

Die Speicherregelung besteht aus zwei Diensten:

```text
storage_simulator.py
  plant Ladekurve, adaptiven Headroom, Pre-Dump, Prognose, Zielanker
  schreibt /var/www/html/ramdisk/storage_plan.json

storage_manager.py
  liest Livewerte, Plan, Wallbox-/Wärme-/Preiszustände
  entscheidet genau einen Speicherauftrag pro Zyklus
  sendet RSCP/EMS an den E3DC
```

Die Dienste kommunizieren über Dateien in der Ramdisk. Der Simulator plant, der
Manager regelt. Ein Zyklus hat genau einen Entscheider und genau einen
RSCP-Ausgang. Die verbindlichen Grundsätze dieser Regelung (harte Schranken vor
Optimierung, Ein-Entscheider je Aktor, Anti-Flattern, fehlende Daten sind keine
Freigabe, Akkustützung der Wallbox nach Korridorlage) stehen in
`doc/V4_Konfiguration_und_Regelung.md`, Abschnitt „Regelungsphilosophie
(verbindlich)“.

## 1. Planung

Der Simulator verarbeitet:

| Quelle | Datei | Inhalt |
|---|---|---|
| PV-Prognose | `pv_forecast.json` | Forecast.Solar, Open-Meteo, optional Solcast |
| Verbrauchsmodell | `ml_prediction.json` / Historie | Hausverbrauch, saisonaler Nachtverbrauch, Wärmepumpenbedarf |
| Preise | `epex_daten.json`, `eco_score.json`, `price_boost_plan.json` | Marktpreise, Nutzerpreis, Boost-Fenster |
| Livewerte | `live_data_py.json` | SoC, PV, Netz, Batterie, E3DC-Grenzen |

Die Planung erzeugt:

- `target_timeline` als Soll-SoC-Kurve,
- Kurvenstart und Freilaufziel,
- optionalen Mittagsanker,
- adaptiven Headroom mit freiem Speicherplatz, Reserve max, Quelle und
  Abregeldruck,
- Pre-Dump-Fenster und Pre-Dump-Energie,
- einen durchgehenden Direktvermarktungs-Tagesplan aus festen
  15-Minuten-Abschnitten, sofern Direktvermarktung aktiv ist,
- `can_reach_target` und Diagnosewerte.

Vergangene und aktive Anker werden eingefroren. Zukünftige Anker dürfen sich
bewegen, aber zukünftige Pre-Dump-/Startanker dürfen nicht durch den aktuellen
SoC nach oben gezogen werden.

Beginnt die erste Ladekurve erst am folgenden Kalendertag und liegt ihr
Start noch außerhalb des Vorhaltefensters (standardmäßig acht Stunden),
löst allein dieser morgige Startanker heute keine Ladepause aus. E3DC darf
Rest-PV autonom nutzen; kurzfristige Schwankungen von PV und Netzexport
wechseln diese Freigabe nicht. Die bestehende SoC-Obergrenze sowie Preis-,
Reserve-, Netz- und Abregelschutz gelten weiter. Innerhalb des
Vorhaltefensters übernimmt wieder die reguläre Vorstartentscheidung.
Die Anzeige nennt beim Startanker Datum und Uhrzeit.

### Effektive Direktvermarktungsprojektion

Die klassische Ladekurve bleibt eine Planung, solange Direktvermarktung den
aktuellen Slot führt. Für Diagnose und WebUI wird deshalb nur die tatsächlich
ausgewählte und bestätigte Wirkung projiziert:

- `PV_STORE` oder `PASSIVE_NORMAL` dürfen Zielkurve und Ladeleistung nur bei
  vollständig gebundener positiver Wirkung anzeigen.
- `CHARGE_BLOCK_WAIT`, `ECONOMIC_EXPORT` und `HEADROOM_EXPORT` ersetzen die
  klassische Ladeprojektion durch ihre bestätigte aktuelle Wirkung.
- Ein noch nicht ausgeführter, veralteter, unbekannter oder gemischter Zustand
  bleibt `PENDING` beziehungsweise `EVIDENCE_LIMIT`; Leistung, Zielwerte und
  `can_reach_target` bleiben dann leer.

Diese Projektion ist kein Entscheider. Sie übernimmt ausschließlich den
bereits vorhandenen Phase-5-Lebenszyklus und sendet keine RSCP-Kommandos.

## 2. Regelung

Der Manager läuft eng getaktet und bündelt alle Wächter. Notstrom, manuelle
Batteriekommandos, Datenvalidität und Hardwaregrenzen besitzen immer Vorrang.
Danach konkurrieren ausschließlich typisierte Kandidaten um denselben
Storage-Owner:

- Preis-/Unwetter-Netzladen mit expliziter Freigabe,
- gebundene Direktvermarktungs-Slots,
- Lastspitzenbegrenzung für die aktuelle Zähler-Viertelstunde,
- adaptiver Headroom, Pre-Dump und Abregelschutz,
- normale Ladekurve sowie Wallbox-/Wärmebudget,
- passiver Freilauf/AUTO.

Vor jedem Hardwareausgang werden Owner, Plan, Slot, Quellenfrische,
Notstromreserve und aktueller `POWER_SETTINGS`-Vertrag erneut geprüft.

### Wiederfreigabe nach Erreichen der Notstromreserve

An der Notstromreserve wird die Entladung weiterhin sofort gesperrt. Die
Wiederfreigabe erfolgt asymmetrisch: Standardmäßig muss der SoC mindestens
1,5 Prozentpunkte über der Reserve liegen und dort für 30 Sekunden mit
frischen, fortschreitenden Messungen bestätigt bleiben. Ein einzelner
SoC-Sprung genügt nicht. Eine vorhandene kleinere Hysterese wird auf mindestens
1,1 Prozentpunkte angehoben; die harte Reserve selbst wird nicht verändert.

Diese Werte sind konservative Reglereinstellungen, keine Herstellervorgabe.
Sie verhindern, dass eine in ganzen Prozentpunkten gelieferte SoC-Anzeige die
Entladesperre ständig löst und kurz darauf erneut setzt. Messlücken,
veraltete Daten und Uhrsprünge zählen nicht als bestätigte Erholung.

Ein Manager-Neustart übernimmt eine im vorhandenen Ramdisk-Zustand gespeicherte
Sperre, beginnt die Beobachtungszeit aber neu. Nach einem vollständigen Verlust
dieses Zustands kann eine frühere Sperre nicht rekonstruiert werden; der
aktuelle harte Reserveschutz bleibt davon unabhängig aktiv. Im Inselbetrieb
bleibt die bestehende E3DC-autonome Behandlung unverändert.

Im normalen PV-Betrieb bleibt der E3DC möglichst in `AUTO`. E3DC-Control setzt
dann nur EMS-Power-Settings:

```text
MAX_CHARGE_POWER
MAX_DISCHARGE_POWER
POWER_LIMITS_USED
```

Harte Modi wie `GRID`, `DISCH` oder `IDLE` sind Ausnahmen und müssen einen
sauberen Besitzer haben. Wenn ein harter Zustand ohne Besitzer entstehen würde,
wird auf AUTO mit freier Hausversorgung zurückgeführt.

## 3. Ladekurvenpfad

Die Ladekurve führt den Speicher entlang der Prognose. Der Manager berechnet:

- Grundbedarf `iFc`: mittlere nötige Ladeleistung bis zum nächsten Anker.
- Rückstand: wie weit der Speicher unter der Kurve liegt.
- verfügbare PV-/Exportreserve.
- harte Abregel- oder Wechselrichtergrenzen.

Der Aufholbedarf wird geglättet und nahe der Kurve gedämpft. Dadurch soll der
Speicher ruhig entlang der Kurve laden, statt in Sägezähnen zwischen 0 W und
Vollgas zu springen.

Die echte Regelgröße ist ein geglätteter `curve_control_soc`, der den
Batteriefluss integriert. Der Roh-SoC bleibt die Wahrheit, aber das Regelsignal
wird nicht bei jedem kleinen Messsprung zurückgesetzt.

Adaptive Headroom-Werte werden dabei nicht als direkter Entladeauftrag gelesen:

- `adaptive_headroom_available_wh`: aktuell freier Speicherplatz im späteren
  Druckfenster.
- `adaptive_headroom_required_wh`: rechnerischer Zusatzanteil der maximalen
  Headroom-Reserve.
- `headroom_reserve_pressure_wh`: historisch oder live plausibilisierte
  Reserve für mögliche PV-Spitzen.
- `curtailment_pressure_wh`: echter Abregeldruck, der die Reserve fachlich
  begründet.

Im Dashboard wird daraus `frei / Reserve max`. Ein Pre-Dump-Auftrag entsteht
erst, wenn `predump_dump_wh` beziehungsweise `Pre-Dump-Bedarf` größer null ist.

### Schreibbremse für die AUTO-Ladegrenze

Im Kurvenladebetrieb, im freien `AUTO` und in der Netzentlastung schreibt der
Manager die EMS-Ladegrenze (`MAX_CHARGE_POWER` mit `POWER_LIMITS_USED`) nicht
bei jeder kleinen Schwankung des Messrahmens neu:

- Nicht schützende Änderungen gehen höchstens alle 30 s hinaus, gezählt ab dem
  letzten Schreibvorgang der EMS-Power-Settings. Eine Absenkung und der
  Wechsel von der Freigabe auf eine Ladegrenze müssen zusätzlich 10 s lang
  gleich angefordert sein. Abweichungen unter 200 W bleiben stehen; besteht
  eine Abweichung länger als 300 s, schreibt der Manager den aktuellen Wert
  trotzdem.
- Schützende Absenkungen wirken im selben Zyklus: 0-W-Halt, Halten bei Kurve
  oberhalb oder vor dem Kurvenstart, Planwert 0, weiche Kurvengrenzen,
  Gleitpfad und Kurvenkante.
- In beide Richtungen ohne Wartezeit, also auch beim Anheben, führen wie
  bisher: Abregel- und Exportdruck an einer Einspeisegrenze, Vorgaben der
  Direktvermarktung (Preiskurve, Reservierung, Nachlauf), die Führung einer
  ladenden Wallbox in `PV-Kurve ruhig`, die Wallbox-Reserve, die kontrollierte
  Wallbox-Grenze und die Zielkorridor-Schnellladung.
- Keine Freigabe wird verzögert, die Netzbezug verhindern kann: Bei Netzbezug
  geht die Freigabe einer gehaltenen Ladegrenze sofort hinaus, denn eine
  gehaltene Ladegrenze begrenzt die Entladung auf die Entladegrenze der
  EMS-Power-Settings. Ebenso geht eine Öffnung sofort hinaus, wenn die
  Einspeisung schon innerhalb des Abregelpuffers (`abregel_puffer_w`) unter
  der Einspeisegrenze liegt. In `AUTO` lädt der Speicher nicht aus dem Netz;
  eine gehaltene Ladegrenze verschiebt nur PV-Leistung zwischen Speicher und
  Einspeisung.
- Ein eigener, per Rücklesen bestätigter Schreibvorgang gilt 4,5 s lang als
  Nachweis für denselben Wert; ein älterer Live-Wert aus dem laufenden
  Lesezyklus löst in dieser Zeit keinen zweiten Schreibvorgang mit diesem Wert
  aus. Ein neuer Wert, der nur innerhalb der Rücklesetoleranz vom letzten
  abweicht, wird wie bisher gegen den Live-Wert geprüft.
- Außerhalb der Bremse arbeiten wie bisher Nutzer-`Aus`, Notstromreserve,
  ungültige Messdaten, der Abregelpfad, die Reservierung für späteres
  Direktvermarktungs-Speichern, die E3/DC-PV-Ladebegrenzung (3.1), die
  PV-only-Entladegrenze der Wallbox, der Start vor der Kurve bei hohem Bedarf
  und harte Lademodi. Die Entscheidungshistorie zählt die Schreibvorgänge je
  Pfad (`write_brake`).
- Jeder gesendete `POWER_SETTINGS`-Schreibvorgang wird im Log des
  Speicherreglers gezählt, auch wenn ihn erst der nächste Live-Readback
  bestätigt oder er fehlschlägt. Ein Wechsel in die Grenzklasse „Entladen
  0 W“ (Entladung gesperrt) oder aus ihr heraus steht immer mit den Sollwerten
  in einer eigenen Zeile („RSCP POWER_SETTINGS: Grenzklasse A -> B …“), auch
  ein kurzer. Andere Wechsel der Grenzklasse (Grenzen aus; Laden 0 W oder
  begrenzt; Entladen begrenzt oder frei) gegenüber der zuletzt protokollierten
  Klasse bekommen höchstens alle 10 s eine eigene Zeile, also höchstens sechs
  je Minute; ein so zurückgestellter Wechsel steht spätestens in der nächsten
  Sammelzeile („…, Grenzklasse A -> B“). Alle übrigen Schreibvorgänge fasst
  eine Sammelzeile zusammen
  („RSCP POWER_SETTINGS: N SET seit HH:MM:SS …, letzter Sollwert …“),
  spätestens 60 s nach dem ersten noch nicht protokollierten Schreibvorgang
  und beim Beenden des Speicherreglers. Bei 13–17 Schreibvorgängen je Minute
  (etwa Kurvenladung bei laufender Wallbox) sind das eine Sammelzeile je
  Minute plus die Klassenwechsel; nicht schützende Änderungen des Laderahmens
  schreibt die Regelung ohnehin höchstens alle 30 s. Fehler und unbestätigte
  SET-Antworten eines gesendeten Schreibvorgangs haben eigene Warn- und
  Fehlerzeilen mit der RSCP-Antwort des E3DC („RSCP-Antwort …“), die die
  Dämpfung wiederholter Meldungen nicht zurückhält; gleichlautende
  Wiederholungen derselben Ursache (gleiche Meldung, gleiche RSCP-Antwort)
  erscheinen höchstens einmal je Minute, die übrigen zählt die Sammelzeile
  („… gleichlautende Fehler/Warnungen ohne eigene Zeile“). Ein nicht gesendeter Versuch zählt
  nicht als Schreibvorgang und bleibt der normalen Dämpfung unterworfen. Die
  Diagnose `rscp_power_settings` zeigt `set_log_pending` und
  `set_log_summary_lines`.
- Die PV-only-Entladegrenze der Wallbox gilt nur, solange eine Wallbox
  tatsächlich lädt: ein ab 500 W sofort, ebenso bei ungültiger, veralteter
  oder unvollständiger Messung; aus erst, wenn jeder gesteckte Ladepunkt 45 s
  ohne Unterbrechung gültig und frisch unter 300 W misst (Zustand
  `wallbox_pv_only_discharge_cap_latch`). Sie folgt dem Hausbedarf; neu
  geschrieben wird sie erst ab einer Änderung von 200 W (Totband, Zustand
  `wallbox_pv_only_discharge_deadband`). Eine Senkung durch eine harte Grenze
  wirkt sofort; für nur beobachtete oder extern geführte Wallboxen gilt das
  Totband nicht.

Die Grenze „höchstens alle 30 s“ gilt nur für nicht schützende Änderungen.
Schützende Absenkungen, Freigaben bei Netzbezug, die in beide Richtungen
sofort wirkenden Vorgaben und die Pfade außerhalb der Bremse können öfter
schreiben. Unter wechselnder Bewölkung oder Last tastet die Bremse den
Messrahmen nur alle 30 s ab. Die Ladegrenze folgt dann verzögert; einzelne
Abschnitte speisen dadurch mehr ein, andere weniger.

### 3.1 Optionale E3/DC-PV-Ladebegrenzung

`storage_dc_first_charge_limit_enable = 1` begrenzt Kurvenladung und
DV-PV-Speichern zusätzlich auf die frisch ermittelte E3/DC-PV-Leistung:

```text
wirksame Ladegrenze =
  min(Storage-Simulator-Obergrenze, frische E3/DC-PV-Leistung)
```

Die E3/DC-PV-Leistung wird aus dem gültigen, topologiegebundenen Split zwischen
gesamter PV und zusätzlicher AC-PV ermittelt. Ein externer AC-Wechselrichter
erhöht diesen Laderahmen nicht. Fehlt der frische Split, bleiben diese
PV-basierten Ladepfade mit 0 W fail-closed.

Der Manager setzt dafür einen `MAX_CHARGE_POWER`-Rahmen über
`EMS_REQ_SET_POWER_SETTINGS`. Die E3DC setzt diese Einstellung bei einem
Neustart zurück; die Regelung schreibt sie trotzdem nur bei
echter Änderung. Sie ist nicht mit der temporären Leistungsvorgabe
`EMS_REQ_SET_POWER` gleichzusetzen.
E3/DC bleibt in AUTO und die Entladung für wechselnden Hausverbrauch
bleibt offen. Die Funktion ist deshalb DC-first, aber keine physikalische
Garantie für einen ausschließlich internen DC/DC-Energiepfad. Preis- und
ausdrücklich freigegebenes Netzladen besitzen eigenständige Verträge.

Die Beobachtung bleibt schnell, kleine Öffnungen des Laderahmens werden jedoch
gesammelt. Eine zeitbezogene Nachführung vergleicht den gewünschten Verlauf mit
dem frisch bestätigten Gerätewert. Nicht jeder intern berechnete Zwischenwert
erzeugt einen neuen Geräteauftrag. Eine kleinere verbindliche Plan-, Quellen-
oder Schutzgrenze wirkt ohne zusätzliche Wartefrist; auch Nutzer-Aus, Plan-0
und ungültige Quelldaten behalten ihren Vorrang.

Ein kleiner verbleibender Abstand zum Ladeziel wird nicht unbegrenzt im Totband
gehalten. Die Abschlussregel berücksichtigt eine begrenzte Wartezeit, während
die vorhandene Protokolltoleranz erhalten bleibt. Berechneter Wunsch, offener
Auftrag und bestätigter Laderahmen sind getrennte Zustände. Ein unterdrückter
Wunsch ist kein neuer Gerätewert. Die tatsächliche Batterieladung kann wegen
Hauslast oder Gerätebegrenzungen weiterhin unter dem oberen Rahmen liegen.

Bei Dringlichkeit öffnet der Laderahmen schneller: Liegt der Speicher unter
der Korridor-Untergrenze der Ladekurve, muss ein Abendziel-Rückstand aufgeholt
werden, ist der späteste Ladebeginn erreicht, fehlt Energie bis zu einem
harten Kurvenanker oder steht die Kurvengrenze unter hartem Einspeisedruck,
öffnet der Rahmen mit mindestens 125 W/s (250 W je 2-s-Regelzyklus) statt mit
der ruhigen Rampe. Ohne Dringlichkeit bleibt die ruhige Öffnung aktiv.

Fällt das E3/DC-PV-Angebot unter die Einschaltschwelle von 300 W (bei
laufendem Rahmen unter die Ausschaltschwelle von 150 W), gilt:

- Ohne nennenswerte Zusatz-AC-PV (unter 150 W, nach einer Freigabe unter
  300 W) wird ein laufender Rahmen bei kurzen Einbrüchen, etwa durch Wolken,
  bis zu 600 s gehalten (`storage_dc_first_low_offer_hold_s`). Dauert der
  Einbruch länger oder läuft noch kein Rahmen, etwa morgens, gibt der Manager
  den Rahmen frei: E3/DC-AUTO ohne Ladegrenze und ohne Heartbeat. Die
  Rückkehr verlangt ein Angebot von mindestens 300 W über 60 s
  (`storage_dc_first_offer_return_s`) und startet direkt am Ziel.
  `storage_dc_first_low_offer_hold_enable = 0` schaltet Halten und Freigabe
  ab.
- Liefert der Zusatz-Wechselrichter selbst Leistung, gilt DC-first: Der Rahmen
  wird ohne Haltezeit auf 0 W gekappt und nicht freigegeben, weil ein
  gehaltener oder freigegebener Rahmen den Speicher aus Zusatz-AC-PV laden
  würde.
- Vorrang hat ein volles Ladeziel: Ist das Ladeziel gefährdet (Rückstand unter
  der Korridor-Untergrenze, Abendziel-Rückstand, spätester Ladebeginn erreicht
  oder ein harter Kurvenanker bereits verfehlt), bleiben Halten und Freigabe
  auch mit Zusatz-AC-PV wirksam; der Speicher darf dann auch aus dem
  Zusatz-Wechselrichter laden. Hoher Einspeisedruck allein zählt nicht dazu,
  ebenso wenig das planmäßige Laden vor einem noch nicht erreichten
  Kurvenanker. Beginnt die Gefährdung während einer Kappung, gibt der Manager
  ohne Wartezeit frei. Endet sie, bleibt der Vorrang noch 600 s
  (`storage_dc_first_low_offer_hold_s`) wirksam, damit der Rahmen nicht
  zwischen Freigabe und Kappung pendelt; erst danach gilt wieder DC-first.
- Ohne gültigen PV-Split gibt es weder Halten noch Freigabe; der Rahmen bleibt
  bei 0 W.

Grenzen dieser Regel: Nach einer Kappung wegen Zusatz-AC-PV gibt der Manager
frei, sobald die Zusatz-AC-PV unter 150 W fällt, ohne erneute Haltezeit. Nach
einer Freigabe bleibt Zusatz-AC-PV bis 299 W zulässig. Während der
Rückkehrprüfung (bis 60 s) bleibt der Rahmen freigegeben und kann in dieser
Zeit auch Zusatz-AC-PV aufnehmen. Oberhalb der Schwelle bleibt der Rahmen
auch bei gefährdetem Ladeziel auf die E3/DC-PV-Leistung begrenzt; nur der
unterhalb der Schwelle gehaltene oder freigegebene Rahmen nimmt dann auch
Zusatz-AC-PV auf.

### 3.2 Ladefreigabe bei Kurvenrückstand

Seit 5.4.2a gilt ein `EMS_USER_CHARGE_LIMIT`-Readback aus frischen, validen
`POWER_SETTINGS` nur dann als reflektierter Laderahmen, wenn
`maximumladeleistung` ausdrücklich konfiguriert ist und
`EMS_USER_CHARGE_LIMIT` sowie `EMS_MAX_CHARGE_POWER` strikt weniger als 50 W
voneinander abweichen. Fehlt eine dieser Bedingungen oder ist ein Wert
veraltet, invalid beziehungsweise abweichend, bleibt `EMS_USER_CHARGE_LIMIT`
als USER-Grenze wirksam.

Liegt der Speicher hinter der Ladekurve, öffnet der Manager den Laderahmen in
`AUTO` nur bei positiver, frischer E3/DC-only-Evidenz bis
`MAX_CHARGE_POWER`. Eine unbekannte oder veraltete Pfadzuordnung genügt dafür
nicht und bleibt fail-closed. Das ist kein aktiver Ladeauftrag:

- Entladen für den Hausverbrauch bleibt offen.
- Bei belegter zusätzlicher AC-PV wird der Laderahmen weiterhin sanft
  nachgeführt und DC-first auf die frisch belegte interne E3/DC-PV-Leistung
  begrenzt.
- Der Pfad erteilt keine Netzladefreigabe und fordert weder `GRID` noch einen
  anderen aktiven Ladebefehl an.

## 4. Verbraucherbudget

Der Storage Manager veröffentlicht ein Budget für andere Dienste:

| Verbraucher | Verhalten |
|---|---|
| Wallbox | nutzt Budget, Modus, Mindeststrom, Phasenlogik und Hysterese |
| Wärmepumpe | nutzt Budget und Mindestlaufzeiten über den Energy Manager |
| Heizstab | nutzt Budget nur bei expliziter Freigabe |

Das Wallbox-Potenzial, das der Storage Manager als Anforderung des
Wallboxanteils im Verbraucherbudget ansetzt (`possible_power_w`), ist die Summe über alle aktiven Ladepunkte
(angesteckt, ladend, Sollstrom > 0 oder Leistung > 250 W): je Ladepunkt
max(6, min(32, Maximalstrom)) · 230 V · Phasen. Eine phasenumschaltfähige
Box zählt mit ihrem 3p-Potenzial, auch wenn sie gerade einphasig lädt, damit
das Budget eine spätere Hochschaltung nicht vorab deckelt; eine ausdrücklich
einphasige Versorgung oder ein einphasiges Fahrzeug aus dem Phasenvertrag
zählt einphasig. Fehlende Daten werden nicht erfunden: ein Ladepunkt ohne
Sollstrom zählt nicht; ohne Phasenmeldung gilt an einer festen Box eine
Phase, an einer umschaltfähigen Box ihr 3p-Potenzial.
Schieflast-, Hausanschluss- und Deckelgrenzen setzt der Wallbox-Manager
anschließend je Ladepunkt; den einphasigen Deckel mit Schieflast-Wächter
gibt es dabei nur an der openWB Pro.

Die Leistung einer externen Wallbox und der E3DC-Hauswert stammen aus
verschiedenen Abfragen; der Hauswert enthält die externe Wallbox. Aus beiden
bildet der Speicherregler den Hausanteil ohne Wallbox und vergleicht ihn mit
dem letzten schlüssigen Probenpaar. Meldet die Wallbox in einer einzelnen
Probe weniger Leistung je Ampere oder weniger genutzte Phasen, ohne dass der
Hauswert um denselben Betrag fällt, hält die Budgetbildung den vom Hauswert
nicht bestätigten Teil bis zur Folgeprobe; das Wallbox-Budget sinkt erst,
wenn die Folgeprobe den Einbruch bestätigt. Keine Haltung gibt es bei einem
vom Hauswert bestätigten Einbruch, bei Netzbezug (ab 100 W), wenn der Wallbox
Manager den ausgegebenen oder die Wallbox den bestätigten Ladestrom gesenkt
hat (der Deckel `cap_amp` allein zählt nicht), bei geändertem Phasenziel,
während und bis 10 s nach einem Phasenwechsel, bei ungültiger Messung, ohne
vergleichbaren Hauswert (etwa an der E3DC-Wallbox) und bei mehr als 10 s
Abstand zur Vorprobe. Fällt umgekehrt der Hauswert bis 10 s nach einer
Stromabsenkung, bevor die Wallboxprobe folgt, senkt der Speicherregler den
Wallboxwert um den Teil, den die Absenkung erklärt (Ampere × 230 V ×
Phasen); so stammen Wallboxwert und E3DC-Rest aus demselben Probenpaar, und
kein Budget oder Haltezonen-Rahmen zählt die Leistung doppelt. Ein fallender
Hauswert ohne passende Absenkung (etwa ein Kochfeld schaltet ab) bleibt
unkorrigiert. Zwei gleichartige Folgeproben mit neuem Hausanteil gelten als
echte Hauslaständerung. Absenkungen werden je Ladepunkt geprüft, damit eine
gleichzeitige Anhebung an einem anderen Ladepunkt sie nicht verdeckt; fehlt
einem ladenden Ladepunkt die Kennung oder ist sie doppelt, gilt der Beleg aus
der Summe aller Ladepunkte. Diagnose: `wallbox_measurement_dip_confirmation`
im Speicherzustand.

In `PV + Akku bis Untergrenze` und in `Sofort bis Preislimit` ohne Preis- oder
Netzfenster gilt direkt über `wbminsoc` die Haltezone
(`doc/V4_Konfiguration_und_Regelung.md`, Akkustützung, Punkt 5), nicht in
`Akku bis Abfahrt`:
Das Restbudget ist dort PV minus alle laufenden Verbraucher ohne Abzug des
Kurvenbedarfs, der Stütz- und Startrahmen aus dem Akku entfällt, und die
laufende Wallbox wird höchstens mit ihrem batterieneutralen PV-Rahmen
gehalten; kurze Schwankungen überbrückt das Wolken-Kontingent. Den
Laderahmen des Speichers verändert die Haltezone nicht. Der Vertrag
`wallbox_wbminsoc_hold_zone` im Wallbox-Rahmen nennt das PV-Budget der
Wallboxgruppe im Band (`budget_w`, gültig solange `active` gilt und `ts`
höchstens `max_age_s` = 15 s alt ist); der Wallbox Manager regelt im Band
einschließlich der Untergrenze danach und rechnet dort kein eigenes Budget.
Diagnose: `wallbox_wbminsoc_hold_zone` im Budget.

Setzt die Entscheidung eines Zyklus die Akkustützung der Wallbox – in der
Haltezone, in der PV-only-Klasse unter dem Kurvenkorridor oder nach einer
Anhebung von `wbminsoc` während der Ladung, jeweils nicht autorisiert –,
stehen Freigabe und Grund (`battery_support_authorized`,
`battery_support_reason`) im selben Zyklus im veröffentlichten Wallbox-Rahmen.
Ohne solche Setzung fehlen beide Felder; der Wallbox Manager nutzt dann seine
eigene Kurvenklasse.

Wärmepumpenleistung wird aus `energy_decision_latest.json` übernommen und in den
Livewerten als `WP_Power` geführt. Wenn der Hausverbrauch die WP bereits enthält,
wird `Home_Power_Raw` behalten und `Home_Power` bereinigt. Dadurch erkennt der
Storage Manager Pre-Dump- und Kurvenlasten, ohne die Wärmepumpe doppelt zu
zählen.

## 5. Pre-Dump

Pre-Dump entlädt nicht sofort am Fensteranfang mit maximaler Leistung. Der
Manager berechnet aus Restenergie und Restzeit eine Zielrampe bis zum
Kurvenstart:

```text
nötige Entladung = verbleibende Pre-Dump-Energie / verbleibende Zeit
```

Lokale Verbraucher haben Vorrang. Netz-Dump wird nur als Fallback genutzt, wenn
das Ziel sonst nicht mehr erreichbar ist und die Einspeisegrenze noch Luft hat.
Am Pre-Dump-Minimum wird keine normale Hausversorgung blockiert.

## 6. Headroom nach Kurvenstart

Nach Kurvenstart ist Pre-Dump beendet. Der Headroom-Pfad darf dann weiter
Speicherplatz sichern, aber zuerst über Kurvenunterkante, Oberkante und
EMS-Ladegrenzen. Aktive `DISCH`-Impulse sind ein eigener, begrenzter Pfad:

- `storage_headroom_discharge_enable = 0` verbietet aktive
  Headroom-Entladung.
- `storage_headroom_discharge_daily_limit_pct` begrenzt die Tagesenergie.
- `storage_headroom_discharge_cooldown_min` erzwingt eine Mindestpause zwischen
  Impulsen.

Der aktive Zustand heißt `parallel_headroom_discharge`. Er ist nur erlaubt,
wenn PV läuft, Exportraum vorhanden ist, der SoC oberhalb der aktuellen
Kurven-Unterkante liegt und kein stärkerer Besitzer wie Abregelschutz,
Wallboxladung oder Kurvenladung Vorrang hat.

## 7. Abregelschutz

Abregelschutz greift bei echtem Druck:

- Einspeisung über Zielkante,
- live gemeldetes E3DC-Derating,
- DC-Leistung oberhalb der Wechselrichterleistung,
- physikalisch nicht nutzbarer Überschuss.

Der Zielwert ist die konservative Kante: konfiguriertes Einspeiselimit,
E3DC-Livewert und Puffer. Wenn kein Druck mehr besteht, fällt der Manager zurück
auf ruhige Kurvenführung.

## 8. Preislogik

Preislogik ist bewusst getrennt:

- Negativpreis-/Preis-Boost startet nur mit expliziter Freigabe und eigener
  Preisgrenze.
- Wallbox-Slots werden durch den Wallbox-Scheduler geplant.
- Unbekannte externe Dauerlasten werden nicht geraten. Wenn eine Wallbox,
  Wärmepumpe oder ein großer Verbraucher regelrelevant sein soll, muss seine
  Leistung eingebunden oder geplant sein.

## 9. Direktvermarktungs-Tagesplan

Bei aktiver Direktvermarktung besitzt jeder 15-Minuten-Abschnitt des Tages eine
Planbedeutung:

- **PV-Speichern** erlaubt ausschließlich den zum Slotvertrag passenden
  Ladepfad.
- **Speicherplatz halten** setzt einen Ladeblock mit 0 W; Entladen für
  Hausverbrauch bleibt offen.
- **Verkaufen** darf nur mit wirtschaftlicher Freigabe, verfügbarer Energie,
  gültigem Netzpunktvertrag und SoC oberhalb der Notstromreserve wirken.
- **Hausversorgung / NORMAL** ist ein passiver AUTO-Abschnitt ohne
  Speicherbremse.

Nach dem letzten PV-Speicherabschnitt bleibt ein künftiges Verkaufsfenster
allein kein Grund, den Speicher zu halten. Andere Storage-Manager-Entscheider
wie Pre-Dump, Preis-Netzladen oder Lastspitzenbegrenzung können weiterhin
Vorrang erhalten. Ein nicht freigegebener Kandidat bleibt Diagnose und erzeugt
keinen RSCP-Ausgang.

## 10. Peak Shaving am Netzbezug

`peak_shaving_enable = 1` schützt den mittleren Netzbezug in festen
Zähler-Viertelstunden. Der reine Policy-Baustein integriert nur frische,
lückenlose Netzpunktmessungen und liefert einen Kandidaten an den zentralen
Storage Manager.

- Beim Begrenzen und Halten bleibt E3/DC in AUTO; die Regelung setzt einen
  Lade- oder Entladerahmen und fordert keine Netzeinspeisung an. Für dessen
  Schreibweise über `POWER_SETTINGS` gilt Abschnitt 3.1.
- Sicherheitsabstand, Leistungshysterese, SoC-Hysterese und
  Freigabe-Entprellung verhindern Flattern.
- Der Lastspitzenpuffer liegt oberhalb der physischen Notstromreserve.
- Bei einer zu großen Messlücke bleibt der Pfad passiv und beginnt erst an einer
  neuen festen Viertelstundengrenze.
- Netz-Nachladung des Puffers benötigt
  `peak_shaving_grid_recharge_enable = 1`, verwendet vorübergehend den
  angeforderten Netzlademodus und bleibt zusätzlich an lückenlose Historie,
  Viertelstundenraum, Hausanschluss und Hardwarelimit gebunden.

`peak_shaving_enable = 0` ist neutral und erhält keine Regelhoheit.

## 11. Ausgabedateien

| Datei | Inhalt |
|---|---|
| `storage_plan.json` | Plan, Zielkurve, Headroom, Pre-Dump, Prognosewerte |
| `storage_manager_state.json` | aktueller Speicherzustand, Auftrag, Diagnosewerte |
| `peak_shaving_interval_state.json` | aktueller Viertelstundenstand, Messabdeckung und Lastspitzenkandidat |
| `direct_marketing_daily_report.json` | zusammengefasster Direktvermarktungs-Tagesplan und Ausführungsstatus |
| `predump_consumer_plan.json` | Pre-Dump-Verbraucherbudget |
| `energy_decision_latest.json` | Wärmepumpen-/Heizstabentscheidung |
| `wallbox_native.json` | Wallboxstatus, Budget, Modus, Messwerte |
| `storage_decisions*.jsonl.gz` | komprimierte Entscheidungsdiagnose |

## 12. Diagnosefragen

Bei Auffälligkeiten zuerst diese Reihenfolge prüfen:

1. Welcher Besitzer steht im Storage-Entscheidungslog?
2. Wurde ein harter Modus (`GRID`, `DISCH`, `IDLE`) sauber begründet?
3. Stimmen `Home_Power_Raw`, `Home_Power`, `WP_Power` und Wallboxleistung?
4. Ist die Kurve selbst gewandert oder nur der EMS-Auftrag?
5. Reagiert der E3DC sichtbar auf gesetzte EMS-Grenzen oder lädt er autonom?
6. Ist `Headroom-Reserve max` nur ein Diagnoseband, oder gibt es wirklich
   `Pre-Dump-Bedarf` beziehungsweise `parallel_headroom_discharge`?
7. Ist ein Direktvermarktungsabschnitt aktiv und freigegeben oder nur ein
   künftiger beziehungsweise diagnostischer Kandidat?
8. Ist die Lastspitzen-Viertelstunde lückenlos belegt und liegt der Puffer
   oberhalb der Notstromreserve?
