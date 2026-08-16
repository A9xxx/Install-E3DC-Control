# Speicher-Ladesteuerung - Systemablauf

> **Stand:** v5.4.3h, gegen den Release-Betriebsvertrag geprüft am 2026-08-16
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
