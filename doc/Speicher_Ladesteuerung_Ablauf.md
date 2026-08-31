# Speicher-Ladesteuerung - Systemablauf

> **Stand:** v5.4.4i
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
> bildet den statisch konfigurierten Abstand für andere Verbraucher. Mangels
> echter PCC-Phasen-RMS-Messung bleibt eine aus Wirkleistung durch `P/230`
> abgeleitete Stromangabe rein diagnostisch und darf keine zusätzliche
> Ladeleistung autorisieren.
> Fehlt `grid_max_amps` oder ist der Wert leer, gelten 35 A je Phase. Mehrere
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
RSCP-Ausgang.

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

Der Manager setzt dafür ausschließlich einen flüchtigen `MAX_CHARGE_POWER`-
Rahmen. E3/DC bleibt in AUTO und die Entladung für wechselnden Hausverbrauch
bleibt offen. Die Funktion ist deshalb DC-first, aber keine physikalische
Garantie für einen ausschließlich internen DC/DC-Energiepfad. Preis- und
ausdrücklich freigegebenes Netzladen besitzen eigenständige Verträge.

### 3.2 Ladefreigabe bei Kurvenrückstand

Seit 5.4.2a gilt ein `EMS_USER_CHARGE_LIMIT`-Readback aus frischen, validen
`POWER_SETTINGS` nur dann als reflektierter flüchtiger Laderahmen, wenn
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

- Beim Begrenzen und Halten bleibt E3/DC in AUTO; die Regelung setzt nur einen
  flüchtigen Lade- oder Entladerahmen und fordert keine Netzeinspeisung an.
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
