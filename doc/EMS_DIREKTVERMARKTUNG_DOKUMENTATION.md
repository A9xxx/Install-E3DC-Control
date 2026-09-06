# Direktvermarktung und Preis-Shadow

> Stand: 2026-09-06
> Status: Dokumentation für Safe-Shadow und den aktiven Storage-Manager-Owner-Vertrag

## Ziel

Der Direktvermarktungszweig soll Marktpreise, EcoScore, Speicherreserve und
PV-Prognose auswerten, ohne Standardnutzer zu beeinflussen. In `Aus` und `Safe`
bleibt der Zweig ein Shadow-Plan. In `Eco`, `Eco+` und `Arbitrage` darf der Storage
Manager nur dann aktiv werden, wenn der Planblock einen gültigen Owner-Vertrag
trägt, `commands_allowed = true` ist und alle Nutzerfreigaben sowie
Wirtschaftlichkeitsprüfungen passen.

Der Storage Manager bleibt Chef. EcoScore, EPEX-Daten und Direktvermarktungs-
Konfiguration sind Entscheidungshilfen, aber kein zweiter Regler.

## Plan, Prognose und tatsächliche Ausführung

Ein vollständiger Zeitraster allein ist noch kein vollständiger Aktionsplan.
Wird ein Verkaufskandidat beispielsweise wegen der Hausreserve verworfen,
bleibt für diesen Zeitraum die normale Hausversorgung als eigene Planaktion
erhalten. Der verworfene Kandidat und sein Ablehnungsgrund sind zusätzliche
Diagnoseinformationen, keine geplante Batterieentladung.

Die passive Eco+-Basisplanung hängt nicht davon ab, ob PV-Speichern aktiviert
oder gerade ein DV-Befehl freigegeben ist. Auch bekannte Viertelstunden ohne
Marktpreis behalten die normale Hausversorgung als Basis. Fehlende Preise und
fehlende tatsächliche Wirkung werden weiterhin separat ausgewiesen; sie sind
keine Lade- oder Verkaufsfreigabe. Mehrdeutige Ausführungsfenster oder bereits
angeforderte beziehungsweise versuchte Ausgaben werden nicht stillschweigend
als passive Basis umbenannt.

Die Anzeige **„DV-SoC · geplant“** beschreibt die erwartete Speicherentwicklung
aus dem gewählten Plan. Sie bleibt auch dann sichtbar, wenn für den aktuellen
Slot noch kein Hardwareeffekt bestätigt ist. **Geplant**, **angefordert** und
**bestätigt** sind verschiedene Zustände. Eine unvollständige oder zu einer
anderen Planrevision gehörende Prognose wird nicht durch eine scheinbar
passende Standardkurve ersetzt.

Wetterbedingtes PV-Potenzial und betriebsbedingt nutzbare PV sind getrennte
Größen. Eine konfigurierte, feste Abschaltregel für einen Zusatzwechselrichter
kann als ausdrücklich gekennzeichnete Planannahme berücksichtigt werden.
Eine unbekannte lokale Schaltregel oder ein unbekannter zukünftiger Zeitplan
des Direktvermarkters bleibt dagegen unbestätigt. Insbesondere bedeutet eine
Einspeisegrenze von 0 W nicht, dass auch die E3/DC-DC-Erzeugung für Haus und
Speicher vollständig entfällt. Bewusste Abregelung ist kein Wetterfehler.

Für einen bereits begonnenen Viertelstundenslot zählen nur die verbleibenden
Sekunden. SoC-Integration und der daraus berechnete Reserve-Leistungsdeckel
verwenden dieselbe Restdauer. Dadurch wird weder bereits vergangene Energie
erneut geplant noch eine korrekte Restslot-Prognose verworfen.

Die hinterlegten konstanten Wirkungsgrade sind keine gemessene
Wechselrichter-Teillastkennlinie. Ohne belastbare Eingaben werden weder eine
pauschale Wiederanlaufzeit noch zusätzliche Grund- oder Anlaufverluste
angenommen. Eine unbekannte Kennlinie ist eine Modellgrenze, kein zusätzlicher
Abzug von der prognostizierten Batterieladung.

Die PV-Aufnahmeplanung schreibt Hausbedarf und bereits ausgewählte Nachladung
zeitlich fort. Günstigere spätere Ladefenster behalten ihren reservierten
Speicherplatz. Ein tatsächlich ausgewähltes Verkaufsfenster kann anschließend
zusätzlichen Platz für bereits wirtschaftlich zulässige PV-Ladefenster schaffen.
Diese Nachallokation erfolgt einmal und zunächst im bestehenden DC-Pfad. Die
erneute Prüfung muss denselben Exportzeitraum, dieselbe Batterieabgabe und
denselben Reserveboden bestätigen; andernfalls bleibt der vorherige konsistente
Plan erhalten. Ein verworfener Kandidat oder eine Entladung nach dem Ladefenster
schafft keinen vorzeitig nutzbaren Platz. Die AUTO-Hausversorgung wird während
einer gewählten Batterieabgabe nicht noch einmal zusätzlich abgezogen.

Das ist eine bedingte Zukunftsplanung, kein Nachweis einer bereits erfolgten
Entladung und keine zusätzliche Hardwarefreigabe. Bei einer späteren
Neuplanung ohne gewählten Export fällt dessen zusätzlicher Ladeplatz wieder
weg. Die Nachallokation wählt keine neuen Verkaufsfenster und ist keine
vollständige gemeinsame Neuoptimierung aller Lade- und Verkaufsmöglichkeiten.

Eco, Eco+ und das gewählte Profitprofil bestimmen weiterhin die wirtschaftliche
Auswahl. Die Prognose erteilt keine zusätzliche Hardwarefreigabe und ändert
keine E3/DC-Steuerbefehle oder Schutzgrenzen.

Warn- und Verantwortungsgrenze: E3DC-Control berechnet Nettoerlös, Marge,
Reserve, PV-Überschuss und Netzpunkt plausibel und warnt bei riskanten
Einstellungen. Ob Batterieeinspeisung, Netzladen, Messkonzept,
Direktvermarktervertrag und Netzbetreiberfreigabe rechtlich und vertraglich
zulässig sind, bleibt Betreiberentscheidung.

## Sicherheitszustände

### Aus

Standard für alle Nutzer:

```text
direct_marketing_enable = 0
```

Erwartung:

- `direct_marketing.mode = off`
- `direct_marketing.active = false`
- keine Fenster
- kein Direktvermarktungs-Owner
- kein Einfluss auf Storage Manager, Wallbox, Wärmepumpe, Heizstab oder RSCP

### Safe Shadow

Test- und Beobachtungsmodus ohne Direktvermarktungsvertrag:

```text
direct_marketing_enable = 1
direct_marketing_mode = safe
direct_marketing_export_enable = 0
direct_marketing_grid_charge_enable = 0
direct_marketing_v2x_discharge_enable = 0
direct_marketing_low_price_curtail_enable = 0
```

Erwartung:

- günstige Fenster werden als `keep_headroom` markiert,
- teure Fenster werden als `safe_house_supply` markiert,
- `commands_allowed = false`,
- keine Batterieeinspeisung,
- kein Netzladen,
- keine WR-/PV-Abregelung.

## Empfohlene Safe-Testwerte

Für ein großes Heimspeichersystem ohne aktive Direktvermarktung:

| Key | Wert | Zweck |
|---|---:|---|
| `direct_marketing_home_reserve_soc_pct` | `35` | Hausreserve schützen |
| `direct_marketing_night_reserve_soc_pct` | `35` | Nachtreserve schützen |
| `direct_marketing_keep_headroom_pct` | `25` | Speicherplatz bei günstigen Preisen freihalten |
| `direct_marketing_min_margin_pct` | `10` | spätere Wirtschaftlichkeitsschwelle |
| `direct_marketing_degradation_ct_per_kwh` | `4.0` | Batteriekosten konservativ rechnen |
| `direct_marketing_roundtrip_efficiency_pct` | `85.0` | Speicherverluste berücksichtigen |
| `direct_marketing_safety_margin_ct_per_kwh` | `1.0` | Prognose-/Messrisiko puffern |
| `direct_marketing_max_cycles_per_day` | `0.5` | Degeneration im Test konservativ halten |

## Modi

### Safe

Safe ist defensiv:

- kein Netzladen,
- keine aktive Batterieeinspeisung,
- Vorrang für Hausversorgung,
- Reservegrenzen bleiben hart,
- günstige positive Preise greifen nicht in die normale Speicherregelung ein,
- negative Preise oder aktiv freigegebene Exportbegrenzung erzeugen
  Headroom-/Aufnahmehinweise,
- teure Preise erzeugen Hinweise zur Hausversorgung.

### Eco

Eco aktiviert das prognosebasierte PV-Speichern in günstigen und negativen
Preisfenstern, erlaubt aber keinen aktiven Batterieverkauf. Der Modus nutzt
dieselben Reserve-, Datenqualitäts-, Rampen- und Headroom-Wächter wie Eco+.
Teure Fenster werden ausschließlich als `eco_house_supply` für die
Hausversorgung bewertet. Auch ein gesetztes `direct_marketing_export_enable`
öffnet in Eco keinen Exportpfad.

### Eco+

Eco+ ist der abgeschwächte Ertragsmodus. Er bewertet nicht Netzladen, sondern
PV-Verschiebung: Energie, die in einem billigen Zeitfenster nicht oder nur
schlecht verkauft würde, kann für ein teures Verkaufsfenster zurückgehalten
werden.

Planaktionen:

- `eco_plus_store_pv_candidate`: PV-Überschuss in niedrigen
  Nettoerlösfenstern speichern. Harte Trigger sind Negativpreis und ein
  Nettoerlös unter `direct_marketing_pv_store_threshold_ct` beziehungsweise
  unter einer aus EEG-Stufen ableitbaren Schwelle. Der EcoScore ist nur
  Fallback, wenn keine solche Schwelle konfiguriert oder ableitbar ist. Der aktive Owner nutzt nie `MODE_GRID`. In normalen PV-Speicherfenstern
  darf er aktiv `MODE_CHRG` nutzen; in externen Abregel-/Exportlimit-Fenstern nutzt
  er E3DC-`AUTO` mit EMS-Laderahmen, damit eine externe Einspeisebegrenzung
  weiter vom E3DC/Direktvermarkter geführt werden kann.
  Der Plan budgetiert diese Fenster nach verbleibendem Speicherbedarf:
  Negativpreisfenster werden zuerst genutzt und zielen im Eco+-PV-Pfad auf das
  volle Tagesziel. Weiche EEG-/Tariffenster werden nur noch eingeplant, wenn
  danach wirklich Ladebedarf bleibt; innerhalb eines Segments verdrängen
  günstigere spätere Fenster teurere frühere Soft-Fenster. `eeg_soft` bedeutet
  ausschließlich Speicherpriorität: Bei positivem Marktpreis bleibt
  PV-Einspeisung zulässig und es entsteht kein Gesamtanlagen-Exportlimit.
- `eco_plus_negative_headroom_hold`: vorgelagertes Freihalten von Speicherplatz,
  wenn ein ausreichend langes Negativpreisfenster mit PV-Überschuss bevorsteht.
  Der aktive Owner lädt nicht aus dem Netz und entlädt nicht aktiv; er setzt nur
  eine EMS-Ladegrenze auf 0 W, solange genug SoC über der Reserve liegt.
- `eco_plus_export_candidate`: Batterie-/PV-Export in teuren Fenstern, nur wenn
  `direct_marketing_export_enable = 1`, Reserve und PV-Shift-Marge passen.

Die Wirtschaftlichkeit steht im Plan als `pv_shift_profit_ok`. Ein negatives
`grid_profit_ok` blockiert Eco+ nicht, weil Eco+ keinen Netzbezug einkalkuliert.

Aktiver Owner:

- `direct_marketing_eco_plus_pv_store`: geschützter PV-Speicher-Owner für
  PV-Überschuss in niedrigen Direktvermarktungsfenstern. Er blockiert bei
  Netzimport über `direct_marketing_pv_store_import_guard_w`, nutzt bei
  externem Exportlimit `AUTO` plus EMS-Ladegrenze und führt den Start
  über `direct_marketing_pv_store_ramp_step_w`, zeigt die Mindesthaltezeit
  `direct_marketing_pv_store_min_hold_s` diagnostisch und endet am Ziel-SoC.
  Mit `direct_marketing_pv_store_dc_only_enable` darf er externe
  AC-Zusatzwechselrichter nicht als Batterie-Ladequelle nutzen. Eine bereits
  laufende Batterieladung wird nur als Schätzwert betrachtet; der aktive
  Ladebefehl bleibt hart auf realen PV-Überschuss bzw. DC-Überschuss gedeckelt.
- `direct_marketing_eco_plus_headroom_hold`: geschützter AUTO-Owner vor
  Negativpreisfenstern. Er blockiert nur Speicherladung, lässt Entladung nach
  vorhandenen E3DC-/Reservegrenzen frei und wird nicht aktiv, wenn der SoC schon
  unter dem berechneten Headroom-Ziel liegt.
- `direct_marketing_eco_plus_export`: geschützter DISCH-Owner für teure
  Verkaufsfenster. Er nutzt nur PV-Shift-Wirtschaftlichkeit, schützt Reserve und
  sperrt bei aktiver Wallbox.

### Netzstrom-Arbitrage

Netzstrom-Arbitrage ist derzeit nicht freigegeben. Die Preisrechnung darf
weiterhin mögliche Kauf-/Verkaufsfenster diagnostisch bewerten, erzeugt daraus
aber weder einen ausführbaren Owner noch einen Speicherbefehl. Ein möglicher
Netzladeslot erscheint ausschließlich als Diagnosekandidat
`arbitrage_grid_charge_candidate`; dieser Schlüssel ist keine Freigabe und darf
niemals allein zu `selected`, `requested`, `issued` oder Hardwarewirkung führen.

Die früheren Konfigurationsfelder `direct_marketing_arbitrage_enable` und
`direct_marketing_arbitrage_experimental_enable` bleiben beim Einlesen und
Speichern erhalten, sind derzeit jedoch wirkungslos. Für die freigegebene
Direktvermarktung stehen Safe, Eco und Eco+ zur Verfügung.

## Kein Verkauf bei Billigpreis

Niedrige oder negative Preise sind nicht automatisch Verkaufsfenster. Das System
verkauft in solchen Phasen nicht aktiv über den Direktvermarktungs-Owner.
Bei positiven Billigpreisen bleibt die normale Speicherregelung zuständig:
der Speicher darf wie gewohnt PV aufnehmen und wird nicht pauschal auf
`100% - Headroom` gedeckelt.

Negative Preise sind der Sonderfall: hier sollen Batterie, freigegebene
Verbraucher und netzdienliche Boost-Pfade möglichst viel Energie aufnehmen.
Nur im harten Negativpreisfall darf, wenn separat freigegeben, als letzter
Schritt echte Netzeinspeisung begrenzt werden:

```text
direct_marketing_low_price_curtail_enable = 1
direct_marketing_low_price_curtail_limit_w = 0
```

Der historisch benannte Schalter ist bewusst getrennt von "kein Verkauf bei
Billigpreis". "Kein Verkauf" verhindert nur eine aktive Batterie-Verkaufsaktion
im Billigfenster; es ist weder eine Speicherbremse noch ein PV-Einspeiseverbot.
Positive EEG-/Billigpreisfenster bleiben auch bei gesetztem Schalter weich.

## Datenbasis und Zeitraster

Die Direktvermarktung trennt bewusst zwei Preiswelten:

- `price`: der hinterlegte Bezugs-/Kundentarif, z.B. Octopus Heat mit HT/LT/UHT
  inklusive Gebühren.
- `market_price`: der Börsen-/Direktvermarktungspreis aus EPEX bzw. der
  Direktvermarktungs-Abrechnungsbasis.

Verkaufen darf nur aus `market_price` entstehen. Wenn der Kundentarif nach
Mitternacht weiterläuft, aber noch kein echter EPEX-/Direktvermarktungswert
vorliegt, bleibt `market_price` leer. Der Direktvermarktungszweig darf daraus
kein Verkaufsfenster ableiten.

Die Vorschau arbeitet im 15-Minuten-Raster. Ein Wert mit Label `23:45` gilt für
das Intervall `23:45-00:00`, ein Wert mit Label `00:00` erst für
`00:00-00:15`. Preise und Nettoerlöse werden deshalb als Stufen angezeigt und
nicht als glatte Linien interpoliert.

Quelle für das Viertelstundenraster:

- EPEX SPOT beschreibt die Umstellung der Day-Ahead-gekoppelten Auktion auf
  15-Minuten-MTU für SDAC-Bidding-Zonen ab Handelstag 30.09.2025 mit Lieferung
  ab 01.10.2025:
  https://www.epexspot.com/en/15-minute-products-market-coupling

## Live-Netzpunkt und Mindest-Netzeinspeisung

Aktive Eco+- und Arbitrage-Verkaufsfenster setzen keinen starren
Netzeinspeisewert. Der Nutzerwert
`direct_marketing_max_export_w` ist die **Basis-Entladung**: so viel
Batterieentladung soll im Verkaufsfenster mindestens wirtschaftlich gefahren
werden, solange Reserve, Exportlimit und Schutzregeln passen.

`direct_marketing_min_grid_export_w` ist davon getrennt. Dieser Wert beschreibt
den gewünschten Mindestabstand am Netzpunkt, damit eine Verkaufsphase nicht in
unbeabsichtigten Netzbezug kippt. Standard ist `100 W`.

Der schnelle Regelkreis nutzt den Netzpunkt:

```text
wenn Netzbezug oder zu wenig Export entsteht:
    DISCH schnell erhöhen, begrenzt durch Hardware, Exportlimit und Reserve
wenn stabil genug eingespeist wird:
    DISCH langsam Richtung Basis-Entladung zurücknehmen
wenn Export nur knapp über dem Mindestwert liegt:
    Vorgabe halten
```

Wichtig: `Home_Power` ist in diesem Pfad Diagnose- und Sicherheitsgröße, aber
kein schneller Führungswert. In Live-Tests kann der abgeleitete Hausverbrauch
mit Batterie- und Netzpunktwerten mitschwingen. Würde der Regler diese Größe
wattgenau verfolgen, entstünde ein 200-Watt-Pendel. Deshalb gilt:

- Bezug am Netzpunkt wird schnell korrigiert.
- Rücknahme erfolgt nur mit Hysterese und Netzpunkt-Halteband.
- Das System regelt bewusst nicht exakt auf `100 W`, sondern hält ein kleines
  Band oberhalb des Mindestexports.
- Der harte Sicherheitswächter bleibt aktiv: Wenn ein erzwungener DISCH-Wert bei
  echtem Netzbezug unter der lokalen Last liegt, fällt der Storage Manager auf
  AUTO zurück.

Aktuelle interne Defaultwerte:

| Key | Standard | Wirkung |
|---|---:|---|
| `direct_marketing_min_grid_export_w` | `100 W` | gewünschter Mindestexport im Verkaufsfenster |
| `direct_marketing_netpoint_deadband_w` | `30 W` | Mess-/Totband gegen Kleinsignale |
| `direct_marketing_netpoint_ramp_up_w` | `1000 W` | schnelle Erhöhung bei Netzbezug |
| `direct_marketing_netpoint_ramp_down_w` | `100 W` | ruhige Rücknahme Richtung Basis |
| `direct_marketing_netpoint_release_margin_w` | `80 W` | Rücknahme erst, wenn Export mindestens Mindestexport plus Margin erreicht |

## Gewinn-Hysterese laufender Verkaufsfenster

Neue Eco+-Verkaufsfenster starten nur, wenn die normale Wirtschaftlichkeits-
prüfung positiv ist:

```text
pv_shift_spread_ct >= direct_marketing_min_profit_ct_per_kwh
pv_shift_margin_pct >= direct_marketing_min_margin_pct
```

Ein bereits aktiver Eco+-Direktvermarktungs-Owner darf dagegen innerhalb eines
kleinen Haltebands weiterlaufen. Das verhindert, dass ein laufendes Abendfenster
wegen Rundung, 15-Minuten-Replan oder minimal geänderter Prognose sofort
abreißt. Der Storage Manager überbrückt dabei nur den Planer-Blocker
`commands_not_allowed`; falscher Owner, abgelaufener Plan, deaktivierte
Direktvermarktung oder falsche Vertragsversion bleiben harte Stopps.

```text
profit_hold_floor = max(0, Mindestgewinn - Gewinn-Halteband)
margin_hold_floor = max(0, Mindestmarge - Margen-Halteband)
```

Standard:

- `direct_marketing_profit_hold_ct_per_kwh = 0.5`
- `direct_marketing_margin_hold_pct = 5.0`

Beispiel: Mindestgewinn `6,77 ct/kWh`, aktueller Spread `6,69 ct/kWh`. Ein neues
Fenster würde nicht starten. Wenn aber vorher schon
`direct_marketing_eco_plus_export` aktiv war, darf der Owner bis zur
Halteschwelle `6,27 ct/kWh` weiterlaufen. Unterhalb dieser Halteschwelle fällt
das System wieder in den normalen Storage-Manager-Pfad zurück.

Die Live-Diagnosefelder heißen unter anderem:

- `direct_marketing_export_base_w`
- `direct_marketing_export_desired_w`
- `direct_marketing_export_min_grid_export_w`
- `direct_marketing_export_netpoint_release_margin_w`
- `direct_marketing_export_grid_export_w`
- `direct_marketing_export_surplus_w`
- `direct_marketing_netpoint_release_hold`
- `direct_marketing_external_derating_active`
- `direct_marketing_external_derating_limit_w`
- `direct_marketing_owner_switch_cooldown_active`
- `direct_marketing_owner_switch_cooldown_remaining_s`

## EEG-Förderung, anzulegender Wert und Marktprämie

Für EEG-geförderte Anlagen ist die garantierte Vergütung nicht identisch mit
dem 15-Minuten-Verkaufserlös des Direktvermarkters. Fachlich sind drei Werte zu
trennen:

- `market_price`: der Börsen-/Direktvermarktungserlös des konkreten Zeitfensters.
- `direct_marketing_*fee*`: Gebühren, Abschläge und variable Kosten des
  Direktvermarkters.
- `direct_marketing_eeg_tariff_tiers`: der anzulegende Wert bzw. die
  garantierte EEG-Vergütung für förderfähige PV-Einspeisung.

Die Marktprämie gleicht nicht jeden individuell schlecht gewählten 15-Minuten-
Trade aus. Sie wird gegen einen Referenz-Marktwert bzw. Monatsmarktwert
berechnet. Deshalb darf der Storage Manager profitable Hochpreisfenster weiter
als echten Mehrerlös betrachten. Die EEG-Vergütung dient zusätzlich als
Vergleichs- und Förderbasis für PV-förderfähige Einspeisung.

Wichtig für die Architektur:

- EEG-/Marktprämienwerte gelten nur für förderfähige PV-Einspeisung.
- Beim PV-Speichern dient der gewichtete EEG-/Tarifwert als nachvollziehbare
  Schwelle: Wenn der erwartete Nettoerlös darunter liegt, ist Speichern
  wirtschaftlich plausibel, solange späterer Verkauf oder Eigenwert die
  Batteriekosten deckt.
- Netzgeladene Batterieenergie in Arbitrage bekommt keine EEG-Gutschrift.
- EEG-Anlage plus Netzladen plus spätere Einspeisung ist ein bestätigungspflichtiger
  Risikobetrieb. Ohne geeignetes Messkonzept, Direktvermarktervertrag und
  Netzbetreiberfreigabe kann diese Betriebsweise Förderansprüche gefährden.
- Nach Ende des Förderzeitraums fällt dieser EEG-Vergleich weg; dann zählt nur
  der echte Direktvermarktungserlös nach Kosten.
- Gestaffelte Anlagen werden über kWp-Schwellen gewichtet. Beispiel:
  `10 | 8,16` und `15,4 | 7,93` ergibt bei 15,4 kWp rund 8,08 ct/kWh.
- Die BNetzA-Archivtabellen von 01.01.2018 bis 31.07.2026 können anhand des
  Inbetriebnahmedatums automatisch übernommen werden. Der sichere Standard
  bleibt `manual`, damit Sondertarife und Netzbetreiber-Abrechnungen weiterhin
  bewusst überschrieben werden können.
- `langzeit.php` nutzt den gewichteten Wert als EEG-Einspeisevergleich und
  rechnet ihn nur ein, wenn `direct_marketing_eeg_enable = 1` ist und gültige
  Stufen vorhanden sind.

Eingebettete aktuelle BNetzA-Tabelle für Inbetriebnahmen vom 01.02.2026 bis
31.07.2026, als jüngster Datensatz des automatischen Archivs:

| Basis | Anlage | Einspeiseart | Stufen ct/kWh |
| --- | --- | --- | --- |
| Einspeisevergütung | Gebäude/Lärmschutzwand | Teileinspeisung | bis 10 kW: 7,78; bis 40 kW: 6,73; bis 100 kW: 5,50 |
| Einspeisevergütung | Gebäude/Lärmschutzwand | Volleinspeisung | bis 10 kW: 12,34; bis 40 kW: 10,35; bis 100 kW: 10,35 |
| Einspeisevergütung | sonstige Anlage | Teil-/Volleinspeisung | bis 100 kW: 6,26 |
| Marktprämie / anzulegender Wert | Gebäude/Lärmschutzwand | Teileinspeisung | bis 10 kW: 8,18; bis 40 kW: 7,13; bis 100 kW: 5,90; bis 400 kW: 5,90; bis 1000 kW: 5,90 |
| Marktprämie / anzulegender Wert | Gebäude/Lärmschutzwand | Volleinspeisung | bis 10 kW: 12,74; bis 40 kW: 10,75; bis 100 kW: 10,75; bis 400 kW: 8,94; bis 1000 kW: 7,70 |
| Marktprämie / anzulegender Wert | sonstige Anlage | Teil-/Volleinspeisung | bis 1000 kW: 6,66 |

Für historische Inbetriebnahmen ab 01.01.2018 nutzt `bnetza_archive` die
passende offizielle XLSX-Archivzeile. Beispiel: Inbetriebnahme 05.01.2021,
Gebäude, Teileinspeisung, feste Einspeisevergütung ergibt die Stufen
`10 | 8,16`, `40 | 7,93`, `100 | 6,22`, passend zur typischen
Netzbetreiber-Abrechnung dieses Zeitraums. Für ältere XLS/PDF-Zeiträume oder
Sonderfälle bleibt `Manuell / Sondertarif` vorgesehen. Das UI schreibt die
gewählte Tabelle sichtbar in `direct_marketing_eeg_tariff_tiers`, damit die
Rechengrundlage prüfbar bleibt.

Quellen:

- Bundesnetzagentur: Marktprämie kann bei geförderter Direktvermarktung
  beansprucht werden; Grundlage ist der anzulegende Wert:
  https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/ErneuerbareEnergien/EEG_Foerderung/start.html
- Bundesnetzagentur: Archiv der Vergütungs- und Fördersätze für historische
  Inbetriebnahmen:
  https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/ErneuerbareEnergien/EEG_Foerderung/Archiv_VergSaetze/start.html
- Netztransparenz: Monatsmarktwerte und Referenzwerte für EEG-Abrechnung:
  https://www.netztransparenz.de/de-de/Erneuerbare-Energien-und-Umlagen/EEG/Transparenzanforderungen/Marktpr%C3%A4mie/Marktwert%C3%BCbersicht
- Bundesnetzagentur: Jede EE-Anlage muss einer EEG-Veräußerungsform zugeordnet
  sein; genannt werden Einspeisevergütung, geförderte Direktvermarktung
  beziehungsweise Marktprämie und sonstige Direktvermarktung:
  https://www.bundesnetzagentur.de/DE/Fachthemen/ElektrizitaetundGas/ErneuerbareEnergien/Solaranlagen/start.html
- LUOX Energy beschreibt die Erlösbausteine aus Direktvermarktungserlös,
  Marktprämie und Kosten getrennt:
  https://www.luox-energy.de/wissensartikel/marktpramie-in-der-direktvermarktung

## Marktwert Solar Monitor

Der Marktwert-Solar-Monitor ist ein reiner Analysepfad. Er ermittelt fortlaufend
einen vorläufigen Monatswert-Trend, indem die 15-Minuten-Solarhochrechnung von
Netztransparenz mit den vorhandenen Spotpreisen gewichtet wird:

```text
marktwert_solar_trend_ct =
  sum(solar_energy_mwh_slot * spot_price_ct_slot)
  / sum(solar_energy_mwh_slot)
```

Wichtig:

- Die Ausgabe ist `read_only = true`, `control_effect = none` und
  `actionable_for_control = false`.
- Es entsteht kein Storage-Owner, kein RSCP-Befehl und keine Lade-/Entladefreigabe.
- Der Wert ist ein vorläufiger Trend. Der offizielle Monatsmarktwert bleibt die
  veröffentlichte ÜNB-/Netztransparenz-Marktwertübersicht.
- Fehlende Netztransparenz-Zugangsdaten führen nur zu `missing_credentials` im
  Monitor; die Direktvermarktungsregelung und die Preisdaten laufen weiter.
- Bei API-Ausfall darf ein lokal gecachter Solarhochrechnungsstand als
  Diagnose-Fallback genutzt werden. Der Status beginnt dann mit `cached_`.

Technisch wird der Monitor vom EPEX-Manager nach dem Schreiben der Preisdaten
aktualisiert. Die Ergebnisdatei liegt hier:

```text
/var/www/html/ramdisk/market_value_solar.json
```

Die verwendeten Netztransparenz-Zugangsdaten sind lokale WebAPI-Credentials:
`netztransparenz_client_id` und `netztransparenz_client_secret`. Das Secret wird
als Passwortfeld behandelt und in Diagnosepaketen maskiert.

Quellen:

- Netztransparenz WebAPI FAQ: URL-Struktur
  `api/v1/data/{data}/{product}/{dateFrom}/{dateTo}` und OAuth-Client-Credentials:
  https://www.netztransparenz.de/en/FAQ/FAQ-WebAPI
- Netztransparenz API-Dokumentation: Endpunkte `hochrechnung/Solar` und
  `Spotmarktpreise`:
  https://www.netztransparenz.de/xspproxy/api/staticfiles/ntp-relaunch/dokumente/web-api/dokumentation-webserviceapi-netztransparenz_v1.21.pdf
- Netztransparenz Marktwertübersicht als offizielle Monatswert-Referenz:
  https://www.netztransparenz.de/de-de/Erneuerbare-Energien-und-Umlagen/EEG/Transparenzanforderungen/Marktpr%C3%A4mie/Marktwert%C3%BCbersicht

## Technische Grundlage und Architekturregel

Die Direktvermarktung folgt dem in Technik und Wissenschaft üblichen
Arbitrage-Modell: Ertrag wird aus niedrigen und hohen Preiszeitpunkten unter
Nebenbedingungen optimiert. Eine feste Mindest-Speichergröße ist dabei keine
saubere Freigaberegel. Die Speichergröße ist eine Nebenbedingung für verfügbare
Energie und Zyklenbudget, aber keine harte Eintrittskarte.

Quellen und Begründung:

- NREL ATB Battery Storage behandelt Speichergröße, Leistung, Zyklenannahmen,
  Betriebskosten und Rundtrip-Wirkungsgrad als Modellparameter; 85% werden als
  repräsentativer Rundtrip-Wirkungsgrad genannt:
  https://atb-archive.nrel.gov/electricity/2020/index.php?t=st
- Das Great-Plains-Institute-Modell formuliert Batterie-Arbitrage als
  Gewinnmaximierung aus Laden/Kaufen und Entladen/Verkaufen unter
  Speicherstands-, Ladeleistungs- und Entladeleistungsgrenzen:
  https://betterenergy.org/wp-content/uploads/2018/02/GPI_Evaluating_Energy_Storage_Economics_July_2016.pdf
- Wankmüller, Thimmapuram, Gallagher und Botterud zeigen im Journal of Energy
  Storage, dass Batteriedegradation die Arbitrage-Profitabilität deutlich
  beeinflusst und als Kosten-/Penalty-Term berücksichtigt werden sollte:
  https://www.osti.gov/biblio/1393934
- ETH Zürich modelliert Arbitrage ebenfalls wirtschaftlich und
  degradation-aware, also nicht als reine Preis-Spitzen-Jagd:
  https://www.research-collection.ethz.ch/server/api/core/bitstreams/9c1c5b69-56df-4c77-ae35-a016e4f63a2c/content

Architekturregel für E3DC-Control:

- Keine harte Speichergrößen-Sperre wie "Direktvermarktung erst ab X kWh".
- `speichergroesse` begrenzt nur `available_export_kwh` und
  `cycle_limit_kwh`.
- Wirtschaftlichkeit wird ausschließlich über Nettoerlös, Bezugskosten,
  Gebühren, Wirkungsgrad, Degeneration, Sicherheitsaufschlag,
  Mindestgewinn und Mindestmarge freigegeben.
- Eine spätere Euro-Schwelle wie `direct_marketing_min_window_profit_eur` darf
  als konfigurierbare No-Trade-Zone ergänzt werden, aber nicht als versteckte
  Speichergrößenregel.
- Aktive Befehle bleiben an den Storage-Manager-Owner-Vertrag gebunden.

## Konfigurationsvariablen

| Key | Einheit | Bedeutung |
|---|---:|---|
| `direct_marketing_enable` | 0/1 | Harter Hauptschalter. `0` bedeutet: kein Plan, keine Befehle, kein Einfluss auf die bestehende Regelung. |
| `direct_marketing_mode` | Text | Strategie: `safe`, `eco`, `eco_plus` oder `arbitrage`. Eco speichert PV ohne Batterieverkauf; Eco+ ergänzt wirtschaftlich geprüften Export. |
| `direct_marketing_provider_name` | Text | Anzeigename des Direktvermarkters, z.B. Luox Energy oder Shadow-Test. |
| `direct_marketing_settlement_basis` | Text | Abrechnungsbasis: Day-Ahead 15 min, Stundenindex, Intraday, Monatsmarktwert oder eigener Vertragsindex. |
| `direct_marketing_revenue_offset_ct` | ct/kWh | Fester Auf- oder Abschlag auf den Börsenpreis vor Gebühren. |
| `direct_marketing_fee_ct_per_kwh` | ct/kWh | Feste Direktvermarktergebühr je verkaufter kWh. |
| `direct_marketing_fee_pct` | % | Variable Gebühr auf die separat konfigurierte Vertragsbasis. |
| `direct_marketing_monthly_fee_eur` | €/Monat | Grundgebühr. Sie beeinflusst die Kurve nicht slotweise, gehört aber in die spätere Tages-/Monatswirtschaftlichkeit. |
| `direct_marketing_variable_fee_basis` | Text | `sell_revenue`, `eeg_compensation` oder `manual`. Eine unvollständige aktive EEG-/manuelle Basis blockiert Befehle. |
| `direct_marketing_variable_fee_basis_ct_per_kwh` | ct/kWh | Manuelle Vertragsbasis der prozentualen Gebühr. Nur bei `manual` wirksam. |
| `direct_marketing_service_vat_pct` | % | USt. auf Dienstleistungs-, variable und Ausgleichskosten, Standard 19 %. |
| `direct_marketing_input_vat_recoverable` | 0/1 | `1` nur bei tatsächlich möglichem Vorsteuerabzug. Standard `0` rechnet USt. konservativ als Kosten. Keine Steuerberatung. |
| `direct_marketing_installed_kwp` | kWp | Abrechnungsleistung für Ausgleichskosten. `0` nutzt die Summe der PV-Prognoseanlagen. |
| `direct_marketing_balancing_cost_eur_per_kwp_month` | €/kWp/Monat | Aktueller Abschlag/Schätzwert der Ausgleichskosten. Nur Monatsabrechnung, keine Slot-Umsortierung. |
| `direct_marketing_balancing_cost_actual_eur_per_kwp_month` | €/kWp/Monat | Nachträglich abgerechneter Ist-Wert für Diagnose und Abgleich. |
| `direct_marketing_min_margin_pct` | % | Mindestmarge auf die jeweilige Kosten-/Opportunitätsbasis. Grenzwerte können wegen Rundung in der Anzeige um 0,1 Prozentpunkt wirken. |
| `direct_marketing_min_profit_ct_per_kwh` | ct/kWh | Absolute Mindestspanne. `0` bedeutet: nur die Prozentmarge entscheidet. |
| `direct_marketing_min_window_profit_eur` | €/Fenster | Absoluter Mindestgewinn im Standardprofil, Standard `0,25 €`. |
| `direct_marketing_min_export_energy_kwh` | kWh/Fenster | Mindestenergie im Standardprofil, Standard `1,5 kWh`. |
| `direct_marketing_min_export_window_min` | min | Technisches Hardgate für einen neuen Verkauf, Standard 15 Minuten. |
| `direct_marketing_preferred_export_plateau_min` | min | Weiche Laufruhepräferenz, Standard 60 Minuten; verbietet kein profitables 15-Minuten-Produkt. |
| `direct_marketing_price_plateau_tolerance_ct` | ct/kWh | Maximaler Preisabstand für ein gemeinsames, gleichmäßig gefahrenes Plateau. |
| `direct_marketing_max_daily_export_kwh` | kWh/Tag | Zusätzliches Tagesexportlimit; `0` nutzt das Zyklenlimit. |
| `direct_marketing_deep_cycle_threshold_pct` | %-Punkte | Entladetiefe, ab der zusätzlicher Tiefentlade-LCOS beginnt. |
| `direct_marketing_deep_cycle_lcos_factor` | Faktor | Stärke des zusätzlichen Tiefentlade-LCOS. |
| `direct_marketing_profit_hold_ct_per_kwh` | ct/kWh | Halteband für bereits aktive Eco+-Verkaufsfenster. Neue Fenster starten weiterhin streng. |
| `direct_marketing_margin_hold_pct` | %-Punkte | Margen-Halteband für bereits aktive Eco+-Verkaufsfenster. |
| `direct_marketing_degradation_ct_per_kwh` | ct/kWh | Angenommene Batteriealterungs-/Zykluskosten je umgesetzter kWh. |
| `direct_marketing_roundtrip_efficiency_pct` | % | Speicherwirkungsgrad für Laden plus Entladen. |
| `direct_marketing_safety_margin_ct_per_kwh` | ct/kWh | Sicherheitsaufschlag gegen Preis-, Prognose- und Messfehler. |
| `direct_marketing_export_enable` | 0/1 | Erlaubt aktive Batterieeinspeisung im Direktvermarktungszweig. |
| `direct_marketing_grid_charge_enable` | 0/1 | Derzeit ohne Hardwarewirkung; Netzstrom-Arbitrage ist nicht freigegeben. |
| `direct_marketing_arbitrage_enable` / `direct_marketing_arbitrage_experimental_enable` | 0/1 | Kompatible Altwerte; werden erhalten, bleiben derzeit aber wirkungslos. |
| `direct_marketing_pv_store_enable` | 0/1 | Erlaubt Eco+, PV-Überschuss in niedrigen Nettoerlösfenstern aktiv zu speichern. Standard `1`, aber ohne Hauptschalter wirkungslos. |
| `direct_marketing_pv_store_threshold_ct` | ct/kWh | Optionale Nettoerlös-Schwelle für PV-Speichern. Leer = EEG-/Tarifwert, wenn berechenbar, sonst EcoScore. |
| `direct_marketing_pv_store_max_w` | W | Maximale PV-Speicherladeleistung. `0` bedeutet System-Ladelimit und realen PV-Überschuss nutzen. |
| `direct_marketing_pv_store_min_surplus_w` | W | Mindest-PV-Überschuss für aktiven CHRG-Owner. Standard `300 W`. |
| `direct_marketing_pv_store_import_guard_w` | W | Netzimport-Wächter. Bei höherem Netzbezug wird PV-Speichern nicht aktiv. |
| `direct_marketing_pv_store_min_hold_s` | s | Mindesthaltezeit für laufendes DV-PV-Speichern. Harte Schutzgründe, Ziel-SoC und Fensterende bleiben vorrangig. |
| `direct_marketing_pv_store_ramp_step_w` | W/Zyklus | Hochlauf-Rampenlimit für DV-PV-Speichern. Runterregeln bleibt sofort möglich, um Netzbezug zu vermeiden. |
| `direct_marketing_pv_store_dc_only_enable` | 0/1 | Optionaler Zusatz-WR-Schutz: nur konservativ ermittelter E3DC-DC-PV-Überschuss darf die Batterie im DV-Pfad laden. |
| `direct_marketing_pv_store_external_ac_guard_w` | W | Rauschtoleranz für externe AC-PV, bevor DC-only greift. |
| `direct_marketing_pv_store_export_limit_guard_w` | W | Toleranz über dem DV-Exportlimit, bevor PV-Speichern die Laderampe beschleunigen darf. Standard `100 W`. |
| `direct_marketing_pv_store_export_limit_ramp_bypass_w` | W | Restexport-Schwelle, ab der Exportlimit-0-Fenster die normale PV-Laderampe übersteuern dürfen. Standard `300 W`. |
| `direct_marketing_price_max_age_s` | s | Optionales Maximalalter, wenn die Preisquelle ein Alter liefert. `0` nutzt nur explizite Stale-Markierungen. |
| `direct_marketing_v2x_discharge_enable` | 0/1 | Reserviert für spätere Auto-/V2X-Entladung. Aktuell ohne aktive Befehle. |
| `direct_marketing_max_export_w` | W | Harte Obergrenze der Batterieentladung im Verkaufsfenster. Lokale Wächter dürfen nur drosseln, niemals über das Policy-Budget erhöhen. |
| `direct_marketing_min_grid_export_w` | W | Mindest-Netzeinspeisung im aktiven Verkaufsfenster. Standard `100 W`; `0` deaktiviert den Netzpunkt-Puffer. |
| `direct_marketing_max_grid_charge_w` | W | Maximale Netzladeleistung im Arbitrage-Zweig. |
| `direct_marketing_max_cycles_per_day` | Zyklen/Tag | Tageslimit für zusätzliche Direktvermarktungs-Zyklen. |
| `direct_marketing_home_reserve_soc_pct` | % SoC | Mindestreserve für Hausversorgung. Leer bedeutet: aus vorhandenen Reserven ableiten. |
| `direct_marketing_night_reserve_soc_pct` | % SoC | Mindestreserve für die Nacht. Sie wird mit Haus- und Notstromreserve zur harten Untergrenze zusammengeführt. |
| `direct_marketing_morning_export_target_soc_pct` | % SoC | Tiefster gewünschter Morgen-SoC für geplanten Eco+-Export. Leer bedeutet: kein aktiver Morgenexport. |
| `direct_marketing_negative_price_no_export` | 0/1 | Sperrt Verkauf bei negativem Marktpreis. |
| `direct_marketing_negative_headroom_enable` | 0/1 | Erlaubt vorgelagerten Headroom-Hold vor langen Negativpreisfenstern. |
| `direct_marketing_negative_headroom_lookahead_min` | min | Maximaler Vorlauf, in dem ein kommendes Negativpreisfenster vorherige PV-Speicherladung blockieren darf. |
| `direct_marketing_negative_headroom_min_window_min` | min | Mindestdauer des Negativpreisfensters für vorgelagerten Headroom. |
| `direct_marketing_negative_headroom_min_surplus_wh` | Wh | Mindest-PV-Überschuss im Negativpreisfenster. |
| `direct_marketing_negative_headroom_buffer_pct` | % SoC | Zusatzpuffer auf den berechneten Speicherplatzbedarf. |
| `direct_marketing_low_price_no_export` | 0/1 | Sperrt Verkauf in billigen Preisfenstern. |
| `direct_marketing_keep_headroom_pct` | % | Freizuhaltender Speicherplatz bei billigen/negativen Preisen. |
| `direct_marketing_negative_price_charge_target_soc_pct` | % SoC | Ziel-SoC für günstiges/negatives Netzladen, nur in freigegebener Arbitrage. |
| `direct_marketing_low_price_curtail_enable` | 0/1 | Historischer Schlüssel: erlaubt als letzten Schritt die harte Gesamt-Exportbegrenzung ausschließlich bei negativen Marktpreisen. Positive EEG-/Billigpreisfenster bleiben weich. |
| `direct_marketing_low_price_curtail_limit_w` | W | Ziel-Netzexport während der harten Negativpreis-Begrenzung. `0` bedeutet möglichst keine Einspeisung. |
| `direct_marketing_market_value_solar_enable` | 0/1 | Aktiviert den read-only Marktwert-Solar-Monitor. Keine Regelwirkung. |
| `direct_marketing_market_value_solar_source` | Text | Aktuell `netztransparenz_hochrechnung_solar`. |
| `direct_marketing_aux_inverter_shelly_override` | local/central | Übergibt die Schützsteuerung des ungeregelten Zusatz-WR explizit an E3DC-Control. Standard `local`; dann sendet das System keine Shelly-Befehle. |
| `direct_marketing_aux_inverter_shelly_ip` | IPv4 | Lokale Adresse des Shelly-Aktors. |
| `direct_marketing_aux_inverter_shelly_invert` | 0/1 | Invertiert nur den physischen Relaispegel für NC-Schütze. Der logische Zustand bleibt immer `desired_wr_on`. |
| `direct_marketing_aux_inverter_shelly_dynamic_unblock_enable` | 0/1 | Erlaubt bei Negativpreis eine Last-Freigabe mit gültigen Live-Werten. Sicherer Standard `0`; `1` muss bewusst aktiviert werden, weil die 600-s-Schaltsperre einen falsch gewählten Lastwert nicht wirtschaftlich korrigieren kann. |
| `direct_marketing_aux_inverter_shelly_unblock_threshold_w` | W | Mindestlast für die dynamische Freigabe, Standard `3000 W`. Der Wert muss zur möglichen Zusatz-WR-Leistung passen. |
| `netztransparenz_client_id` | Text | WebAPI-Client-ID für Netztransparenz. |
| `netztransparenz_client_secret` | Secret | WebAPI-Client-Secret; wird lokal gespeichert und redigiert. |
| `direct_marketing_eeg_enable` | 0/1 | Aktiviert die EEG-/Marktprämien-Bewertung für förderfähige PV-Einspeisung. |
| `direct_marketing_eeg_commissioning_date` | Datum | Inbetriebnahmedatum zur Einordnung des Förderzeitraums. |
| `direct_marketing_eeg_support_years` | Jahre | Förderjahre nach Inbetriebnahmejahr, Standard 20. Das berechnete Förderende ist der 31.12. des Jahres `Inbetriebnahmejahr + Förderjahre` und wird in der Config angezeigt. |
| `direct_marketing_eeg_rate_source` | `manual`/Tabelle | Quelle der Vergütungsstufen. Standard `manual`; `bnetza_archive` wählt automatisch per Inbetriebnahmedatum; `bnetza_current_2026_02` bleibt als fester aktueller Satz 01.02.2026 bis 31.07.2026 erhalten. |
| `direct_marketing_eeg_system_type` | Typ | `building` für Gebäude/Lärmschutzwand oder `other` für sonstige Anlagen. |
| `direct_marketing_eeg_feed_type` | Typ | `partial` für Teileinspeisung oder `full` für Volleinspeisung. |
| `direct_marketing_eeg_compensation_basis` | Basis | `feed_in_tariff` für feste Einspeisevergütung oder `market_premium` für anzulegenden Wert/Marktprämie. |
| `direct_marketing_eeg_tariff_tiers` | kWp/ct | Vergütungsstufen, eine Zeile pro Stufe: `10 | 8,16`, `15,4 | 7,93`. |
| `direct_marketing_eeg_grid_export_risk_ack` | 0/1 | Explizite Bestätigung, dass Netzladen mit späterer Einspeisung bei einer EEG-Anlage vertraglich/messkonzeptionell erlaubt ist. Ohne diese Bestätigung bleiben Arbitrage-Befehle blockiert. |

### Zusatz-WR-Schützschutz

Der Dashboard-Button schreibt nur den persistenten Sperrwunsch in den kanonischen
Zusatz-WR-Zustand. Ausschließlich der Storage Manager übersetzt ihn unter
Beachtung der konfigurierten NC-/NO-Logik in einen Shelly-RPC-Befehl. Jede
erfolgreiche Relaisänderung aktualisiert den kanonischen Schutzstatus und startet
damit eine auch über Dienst- und Hostneustarts erhaltene Sperrzeit von 600
Sekunden. Bestehende Installationen mit älteren Schlüsseln und Zuständen werden
beim Update automatisch gespiegelt beziehungsweise migriert. Nur eine
neue manuelle Sperre darf diese Zeit in
Richtung `desired_wr_on = false` übergehen; das Aufheben der Sperre wartet den
Schützschutz ab.

Bei dynamischer Last-Freigabe bleibt ein bereits laufender Zusatz-WR während
eines Negativpreises an, solange am Netzpunkt nicht mehr als 100 W eingespeist
werden. Ein ausgeschalteter WR wird nur bei gültigen, frischen Live-Werten,
ohne aktuellen Export und oberhalb der konfigurierten Lastschwelle freigegeben.
Die Lastschwelle ersetzt keine Leistungsprognose des Zusatz-WR: Ein zu kleiner
Wert kann während der 10-Minuten-Schaltsperre trotzdem Export verursachen und
wird deshalb in Konfiguration und Diagnose ausdrücklich als Risiko markiert.

## Berechnung

Alle Erlöswerte werden in ct/kWh gerechnet.

Die Config-Vorschau nutzt für Direktvermarktung den verfügbaren Marktpreis-Horizont. Sobald EPEX-/Day-Ahead-Werte für morgen vorhanden sind, werden diese in der Direktvermarktungs-Vorschau mit Tagespräfix angezeigt; die normale Speicher-Ladekurvenvorschau bleibt bewusst beim kompakten Tagesausschnitt.

```text
gross_sell_ct = market_price_ct + direct_marketing_revenue_offset_ct
variable_fee_net_ct = variable_fee_basis_ct * direct_marketing_fee_pct / 100
vat_multiplier = 1 oder (1 + direct_marketing_service_vat_pct / 100)
fee_cost_ct = (direct_marketing_fee_ct_per_kwh + variable_fee_net_ct) * vat_multiplier
net_sell_ct = gross_sell_ct - fee_cost_ct
```

Der USt.-Multiplikator ist nur bei nicht abziehbarer Vorsteuer größer als 1.
Monatliche Grundgebühr und Ausgleichskosten werden separat ausgewiesen und
ändern `net_sell_ct` nicht.

`net_sell_ct` ist die weiße Nettoerlöskurve in der Vorschau. Sie ist die
Sortiergröße für knappe Verkaufsenergie: Wenn die Reserve nur für einen Teil der
teuren Fenster reicht, bekommt das Fenster mit dem höchsten `net_sell_ct`
Vorrang, nicht das früheste Fenster.

Der EEG-Vergleich in `langzeit.php` nutzt einen gewichteten Satz aus den
Leistungsstufen:

```text
eeg_weighted_ct = sum(segment_kwp * tier_ct) / total_kwp
eeg_feed_in_revenue_eur = grid_out_kwh * eeg_weighted_ct / 100
```

Dieser Wert ist ein Vergleichs- und Förderbasiswert für PV-Einspeisung. Er wird
nicht in die Arbitrage-Wirtschaftlichkeit für netzgeladene kWh eingerechnet.

Für PV-Speichern gilt:

```text
pv_store_threshold_ct = direct_marketing_pv_store_threshold_ct
                      oder gewichteter EEG-/Tarifwert
                      oder EcoScore-Tiefpreisfenster

pv_store_allowed = net_sell_ct <= pv_store_threshold_ct
                oder market_price_ct < 0
                oder EcoScore im günstigen Bereich
```

Die Planmenge ist zusätzlich energiebegrenzt. Der Schattenplan zählt also nicht
mehr jedes günstige 15-Minuten-Fenster mit voller Ladeleistung hoch, sondern
verteilt nur den Speicherbedarf bis zum Ziel-SoC beziehungsweise den vorher
geplanten Verkaufs-/Exportbedarf. Harte Negativpreisfenster haben Vorrang vor
weichen EEG-Schwellenfenstern. Wenn ein Negativpreisfenster genügend
prognostizierten PV-Überschuss liefert, erscheinen danach keine zusätzlichen
weichen PV-Speicherfenster nur wegen `net_sell_ct <= pv_store_threshold_ct`.
Wenn mehrere weiche Fenster vor dem nächsten Verkauf verfügbar sind, gewinnt das
günstigere beziehungsweise längere ausreichende Fenster; frühere fast
preisgleiche Fenster bleiben regelfrei.

Der aktive Storage-Manager-Owner prüft zusätzlich live:

```text
grid_import_w <= direct_marketing_pv_store_import_guard_w
pv_store_offer_w >= direct_marketing_pv_store_min_surplus_w
SOC < direct_marketing_target_soc_pct - hysteresis

wenn direct_marketing_pv_store_dc_only_enable = 1:
  Ext_PV_Power > direct_marketing_pv_store_external_ac_guard_w
  => pv_store_offer_w <= konservativer E3DC-DC-PV-Überschuss

wenn curtail_export_limit_w gesetzt ist:
  grid_export_w > curtail_export_limit_w + direct_marketing_pv_store_export_limit_guard_w
  => PV-Speichern darf die normale Laderampe bis zum Export-Aufnahmeziel
     beschleunigen, bleibt aber auf PV-/DC-Überschuss begrenzt und setzt die
     Ladeleistung als EMS-Laderahmen im E3DC-AUTO-Pfad statt als harten CHRG-Befehl.
```

Fehlende Marktpreise, explizit stale Preisdatensätze oder eine
60-Minuten-Preisauflösung bei `day_ahead_15min` erzeugen nur Shadow/Safe und
keine aktiven DV-Befehle.

### Eco+: PV-Shift

Eco+ vergleicht Verkauf später mit Verkauf beziehungsweise Nicht-Verkauf im
billigen Fenster. Netzbezug spielt in dieser Variante keine Rolle.

```text
pv_shift_revenue_ct = best_high_net_sell_ct * efficiency
pv_shift_opportunity_ct = max(best_low_net_sell_ct, best_low_gross_sell_ct)
pv_shift_cost_basis_ct = abs(pv_shift_opportunity_ct)
                       + direct_marketing_degradation_ct_per_kwh
                       + direct_marketing_safety_margin_ct_per_kwh
pv_shift_spread_ct = pv_shift_revenue_ct
                   - pv_shift_opportunity_ct
                   - direct_marketing_degradation_ct_per_kwh
                   - direct_marketing_safety_margin_ct_per_kwh
pv_shift_margin_pct = pv_shift_spread_ct / max(1, pv_shift_cost_basis_ct) * 100
```

Vermiedene Direktvermarktergebühren werden konservativ nicht als zusätzlicher
Batteriegewinn gutgeschrieben. Eine höhere Gebühr darf deshalb keinen weiteren
Zyklus scheinbar attraktiver machen.

Eco+ darf verkaufen, wenn gilt:

```text
pv_shift_spread_ct >= direct_marketing_min_profit_ct_per_kwh
pv_shift_margin_pct >= direct_marketing_min_margin_pct
```

### Arbitrage: Netzbezug plus Verkauf

Arbitrage bewertet zusätzlich den Netzbezug zum hinterlegten Bezugstarif.

```text
low_import_cost_ct = best_low_billing_ct / efficiency
grid_spread_ct = best_high_net_sell_ct
               - low_import_cost_ct
               - direct_marketing_degradation_ct_per_kwh
               - direct_marketing_safety_margin_ct_per_kwh
grid_margin_pct = grid_spread_ct
                / max(1, low_import_cost_ct
                         + direct_marketing_degradation_ct_per_kwh
                         + direct_marketing_safety_margin_ct_per_kwh)
                * 100
```

Die Kennzahlen bleiben Diagnosewerte. Sie können derzeit keine
Netzlade- oder Arbitrage-Exportfreigabe erzeugen.

### Reserve, Zyklen und Fensterpriorität

Die harte Untergrenze ist:

```text
effective_min_soc_pct = max(Notstromreserve, Hausreserve, Nachtreserve)
available_export_kwh = max(0, current_soc_pct - effective_min_soc_pct)
                     / 100 * speichergroesse_kwh
cycle_limit_kwh = direct_marketing_max_cycles_per_day * speichergroesse_kwh
```

Das nutzbare Exportbudget ist durch Reserve und Zyklenlimit begrenzt. Die
Speichergröße wirkt hier nur als Rechengröße für verfügbare Energie. Kleine
Speicher werden nicht pauschal gesperrt, sondern verkaufen nur dann, wenn nach
allen Verlusten und Kosten ein positives Fenster übrig bleibt. Große Speicher
werden ebenfalls nicht bevorzugt, wenn Spread, Gebühren oder Reserve nicht
passen. In Arbitrage kann zusätzlich geplante Netzladeenergie nach Wirkungsgrad
in das spätere Exportbudget eingehen. Danach werden alle Verkaufs-Slots nach
`net_sell_ct` sortiert. Teurere Slots gewinnen, auch wenn sie später liegen.
Nicht mehr bedienbare Slots werden in der Vorschau als Hausversorgung bzw.
reservebegrenzt markiert.

## Beobachtung im Shadow-Test

Nach neuen EPEX-Werten prüfen:

1. Gibt es nur Fenster mit echten Preis-/EcoScore-Daten?
2. Sind mittägliche günstige Fenster als `keep_headroom` sichtbar?
3. Sind teure Abendfenster als `safe_house_supply` sichtbar?
4. Bleibt `commands_allowed = false` im Safe-Modus?
5. Wird `commands_allowed = true` nur bei Eco+/Arbitrage mit gültiger Marge,
   passenden Freigaben und aktuellem Fenster?
6. Zeigt der Storage Manager weiter normale Kurvenführung?

## Live-Dateien

Wichtig für Diagnose:

```text
/var/www/html/ramdisk/storage_plan.json
/var/www/html/ramdisk/storage_manager_state.json
/var/www/html/ramdisk/storage_decision_latest.json
/var/www/html/ramdisk/direct_marketing_daily_report.json
/var/www/html/ramdisk/market_value_solar.json
/var/www/html/ramdisk/wb_pv_budget.json
/var/www/html/ramdisk/wallbox_storage_intent.json
/var/www/html/ramdisk/epex_daten.json
/var/www/html/ramdisk/pv_forecast.json
/var/www/html/data/e3dc_v4.json
```

Für Supportfälle gibt es in der Installationszentrale ein eigenes
Diagnosepaket-Preset **Direktvermarktung**. Es enthält zusätzlich
`status/direct_marketing_diagnosis.json`. Diese kompakte Statusdatei fasst
Konfiguration, Planfenster, Owner, Blocker, EPEX-/PV-Input, Live-Kontext,
Tagesbericht und `market_value_solar.json` zusammen und maskiert sensible
Werte. Besonders wichtig sind die
Hinweise `active_export_or_grid_charge_flags_require_user_ack`,
`shadow_only_no_commands`, `commands_not_allowed`,
`storage_plan_without_direct_marketing_contract` und `missing_price_file`.
Bei Anlagen mit externer Direktvermarkter-Abregelung sind zusätzlich
`external_derating`, `direct_marketing_external_derating_*` und
`owner_switch_cooldown_*` relevant: Sie zeigen, ob der E3DC/Direktvermarkter
die Einspeisegrenze führt und ob ein weicher Ownerwechsel bewusst kurz
entprellt wurde.
Damit ist für den Nutzer nachvollziehbar, ob die Direktvermarktung nur rechnet,
PV-Überschuss speichert oder bewusst freigeschaltete Batterieeinspeisung bzw.
Netzladen vorbereitet.

Der relevante Planblock heißt:

```json
{
  "direct_marketing": {
    "active": true,
    "shadow": true,
    "mode": "safe",
    "owner_contract_version": 1,
    "controller_owner": "storage_manager",
    "flags": {
      "owner_contract_version": 1,
      "commands_allowed": false
    },
    "windows": []
  }
}
```

## Aktive Owner-Felder

Bei aktiver Steuerung schreibt der Storage Manager zusätzlich:

- `direct_marketing_active`,
- `direct_marketing_mode`,
- `direct_marketing_owner`,
- `direct_marketing_action`,
- `direct_marketing_profit_ct_per_kwh`,
- `direct_marketing_reserve_floor_soc_pct`,
- `direct_marketing_target_soc_pct`,
- `direct_marketing_headroom_hold_active`,
- `direct_marketing_headroom_soc_ceiling_pct`,
- `direct_marketing_headroom_forecast_surplus_wh`,
- `direct_marketing_pv_store_w`,
- `direct_marketing_pv_store_offer_w`,
- `direct_marketing_pv_store_grid_import_w`,
- `direct_marketing_pv_store_grid_export_w`,
- `direct_marketing_pv_store_import_guard_w`,
- `direct_marketing_pv_store_requested_w`,
- `direct_marketing_pv_store_estimated_offer_w`,
- `direct_marketing_pv_store_pv_safe_cap_w`,
- `direct_marketing_pv_store_self_reference_limited`,
- `direct_marketing_pv_store_export_limit_active`,
- `direct_marketing_pv_store_export_over_limit_w`,
- `direct_marketing_pv_store_export_absorb_target_w`,
- `direct_marketing_pv_store_unavoidable_export_w`,
- `direct_marketing_pv_store_export_limit_ramp_bypass`,
- `direct_marketing_pv_store_ramp_limited`,
- `direct_marketing_pv_store_hold_active`,
- `direct_marketing_pv_total_w`,
- `direct_marketing_pv_e3dc_w`,
- `direct_marketing_pv_external_ac_w`,
- `direct_marketing_pv_store_dc_only`,
- `direct_marketing_pv_store_dc_surplus_w`,
- `direct_marketing_window`.

Der Decision-Verlauf setzt `control_owner = direct_marketing`, sobald einer der
aktiven Owner den Zyklus besitzt.
