# Ladekurve - technische Dokumentation

> **Quellcode:** `Installer/storage_simulator.py` (Berechnung) ·
> `Installer/storage_manager.py` und `Installer/storage_parallel_regulator.py`
> (Regelung)

Die Ladekurve ist eine regelungsnahe Soll-SoC-Kurve für den laufenden Tag. Sie
ist keine Garantie für den späteren realen SoC, sondern die Führungsgröße, an
der Speicher, Wallbox, Wärmepumpe, Abregelschutz und Preislogik geordnet werden.

## 1. Grundidee

Der Speicher soll nicht morgens stumpf vollgeladen werden, wenn mittags
Abregelung droht. Gleichzeitig soll das Tagesziel zum Freilauf möglichst sicher
erreicht werden. Der Simulator berechnet deshalb eine zeitliche SoC-Trajektorie:

```text
PV-Prognose - erwarteter Hausverbrauch - bekannte Verbraucher
      -> integrierbare Speicherenergie
      -> Ziel-SoC je Zeitanker
```

Der Manager vergleicht den echten beziehungsweise geglätteten SoC mit dieser
Kurve und setzt bevorzugt nur EMS-Leistungsgrenzen im E3DC-AUTO-Betrieb.

## Zeitbezug von Simulation und Korridoranzeige

Die Zeitachse des Plans beginnt weiterhin an der vollen Stunde. Vergangene
Viertelstunden werden nicht erneut bilanziert: Ihre Start- und Endwerte
entsprechen dem gemessenen SoC beim Planlauf. Der laufende Slot wird nur ab
diesem Zeitpunkt gerechnet. Ladeverteilung, Nacht-Tiefpunkt, Tagesprojektion
und Pre-Dump-Hardwaresimulation verwenden dieselbe verbleibende Dauer.
`timeline[].soc_start` bezeichnet den Start der jeweiligen Fortschreibung,
`timeline[].soc` deren Ende. Bestehende Pläne ohne Startwert bleiben lesbar.
Die Anzeige ordnet beide Werte dem tatsächlichen Zeitpunkt zu.

„Jetzt“ zeigt die je Zyklus veröffentlichte Korridorlage und tatsächliche
Regelart des Speicher-Managers. Im Band wird der Korridor angezeigt,
darunter die Unterkante und darüber die Obergrenze. Die Unterkante ist
nur bei aktiver Kurvenführung ein Regelziel. Innerhalb des Bands ist der
Ist-SoC kein Zielwert. „E3DC regelt frei“ erscheint nur bei tatsächlich
freigegebenen EMS-Grenzen. Ein Preis- oder Reserve-Halt, Netzladen und
Schutzfall werden anhand des finalen Ausgangs benannt. Fehlt dessen
Nachweis, bleibt die Regelart unbekannt. Ohne gültige, frische Managerdaten
erscheint „Kein Regelziel verfügbar“. Die Sollkurve bleibt eine eigene
Diagrammlinie.

## 2. Simulator: Plan und Anker

Der `storage_simulator.py` läuft zyklisch und schreibt `storage_plan.json` in
die Ramdisk. Wichtige Inhalte:

- `target_timeline`: Soll-SoC-Anker für den Tag.
- `ladestart_ts`: Kurvenstart, meist der Beginn sinnvoller PV-Leistung.
- `ladestart_soc`: Startwert am Kurvenbeginn.
- `target_soc`: Tagesziel zum Freilauf.
- optionaler Mittagsanker (`storage_noon_target_soc`, `storage_noon_hour`).
- Pre-Dump-Daten (`predump_dump_wh`, `predump_start_ts`, `predump_end_ts`,
  `predump_reason`).
- Adaptive-Headroom-Daten (`adaptive_headroom_available_wh`,
  `adaptive_headroom_required_wh`, `adaptive_headroom_buffer_wh`,
  `headroom_reserve_pressure_wh`, `headroom_reserve_source`,
  `curtailment_pressure_wh`).

### Abendziel bei PV + Akku

Für alle Wallboxen (geregelt und beobachtet) gilt dieselbe Zielwahl:
In `PV + Akku bis Untergrenze` sowie in `Aus` mit der Auswahl
„Nur beobachten, Speicher oberhalb Hausreserve freigegeben“ prüft der
Planer Akku, Fahrzeug und Gesamtbedarf getrennt. Mit „Nur beobachten, Speicher folgt Ladekurve“ bleibt das normale Abendziel unverändert.

Der Akku-Anteil ist auf seine Ladeleistung begrenzt. Fahrzeugenergie zählt
nur in Slots, deren Überschuss die Fahrzeug-Mindestleistung erreicht.
Jedes Fahrzeug wird einzeln gegen seinen Bedarf geprüft; ein kleines
Ladeziel kann die fehlende Lademöglichkeit eines anderen Autos nicht decken.
Bei aktivem Rückfallziel nimmt der Speicher im Kurvenkorridor Überschuss
auf, den das Fahrzeug nicht nutzen kann: unter seiner kleinsten gültigen
Mindestleistung oder als Rest, wenn alle ladenden Fahrzeuge ihre Höchstleistung
erreichen. Dafür muss die frische Fahrzeugleistung höchstens 200 W unter der
gültigen Höchstleistung liegen oder das Stromziel am Höchststrom stehen.
Aus dem Null-Halt beginnt die Ladung nach 60 Sekunden durchgehend gültigem
Rest mindestens 200 W unter dem Fahrzeugminimum beziehungsweise mindestens
200 W Rest bei Höchstleistung. Im Zwischenband bleibt der bisherige Zustand;
ungültige Proben oder Lücken setzen die Eintrittszeit zurück. Eine bereits
laufende Akkuladung aus einem höchstens 10 Sekunden alten Vorzyklus setzt
sich ohne neue Wartezeit fort. Die Ladegrenze ist das kleinere von Geräte-
Maximalleistung und gemessenem Rest, ohne Begrenzung durch die Kurvensteigung.
Ladegrenzen unter 300 W werden wie sonst auf 0 W geklemmt. Eine Lastspitze
beendet den Zustand erst nach 60 Sekunden durchgehend fehlendem Rest;
das aktuelle Ladeangebot wird sofort auf den gemessenen Rest begrenzt.
An `soc_ceiling_curve` endet diese Ausnahme hart; dort gilt die normale Kurve.
Der bestehende Abregel-/Einspeiseschutz und AUTO-Freilauf bleiben wirksam.
Hausanschluss-/Einspeisereserve, Abendfreigabe, Halt vor Kurvenstart, Pre-Dump,
Preis-/Slotfenster und Schutzzustände haben Vorrang. Innerhalb dieser Grenzen
geht der Korridorrest dem gleitenden Prognosehorizont und Landungshalten vor.
Der Rest ist der gemessene Saldo `max(0, Akkuladung − Netzbezug)`, mit
negativem Netzbezug bei Einspeisung und negativer Akkuladung bei Entladung.
Haus, Wärmepumpe und jede Wallbox zählen dabei genau einmal.
Die Halteprognose der Wärmepumpe berücksichtigt weiterhin das normale
Speicherziel; das abgesenkte Fahrzeugziel schafft keine zusätzliche Wärmefreigabe.
Der Wallbox Manager berücksichtigt die veröffentlichte Korridorladung bei
seiner eigenen Startprüfung genau einmal: Der Speicher veröffentlicht den
Rest aus einem gemeinsamen Netz-/Akkuprobenpaar. Rest und gefilterter
Exportraum werden alternativ verwendet, niemals addiert.
Sobald er starten oder höher regeln
will, gibt der Speicher frei. Strom, Phasen und Stop entscheidet die Wallboxregelung.
Kann keines der geeigneten Fahrzeuge an irgendeinem Zeitpunkt aus PV laden,
gilt die normale Kurve (`vehicle_cannot_use_pv`). Unbekannte Leistungsgrenzen
sind kein Beleg fehlender PV-Lademöglichkeit.

Die Leistungsgrenzen verwenden mögliche Phasen sowie Geräte- und
Fahrzeuggrenzen. Der Mindeststrom ist mindestens 6 A, mindestens der
konfigurierte Wert und bei gemeldetem Gerätewert der höhere. Die Herkunft
von Mindeststrom, Höchststrom und Phasen wird getrennt ausgewiesen.
Nennwerte sind keine Messung. Fehlen Grenzen, vergrößert das Fahrzeug
den nutzbaren PV-Anteil nicht. Phasenstufen werden für die Prognose als
Leistungsbereich angenähert; der Gerätepfad prüft die ausführbare Stufe.
Nur die Bedarfsbilanz beginnt am aktuellen 15-Minuten-Slot; der übrige
Planerzeitstrahl beginnt an der vollen Stunde. Fahrzeugbedarf und geplante
Lasten der Art `external_wallbox` zählen genau einmal. Eine geplante
Wallboxlast wird im Vergleich bei aktivem Fahrzeugbedarf oder bestätigter
Ladung mit unbekanntem Bedarf zurückgebucht, ohne Zuordnung zu einer
bestimmten Wallbox und ohne Änderung der Slotbilanz.

Reicht die PV nicht für Akku und Fahrzeug, endet die heutige PV-Kurve bei
der Hausakku-Untergrenze (`effective_wb_floor_soc`), auch bei „Prognose auf
100%“. Bekannter Nullbedarf lässt das normale Ziel bestehen. Der Bedarf
stammt aus bestätigtem SoC, Ziel und Fahrzeugkapazität oder dem restlichen
kWh-Sitzungsziel einschließlich Ladeverlusten. Ein unbekanntes eigenes
openWB-Ziel wird nicht als E3DC-Control-Ziel angenommen.

Der Messbeleg schaltet ab 500 W ein und nach 45 Sekunden gültig unter
300 W aus; genau 300 bis unter 500 W halten den bisherigen Zustand.
Eine Messlücke hält ihn höchstens 600 Sekunden. Unterbrechungen setzen den
45-Sekunden-Timer zurück; negative Leistung bleibt ungültig, niemals 0 W.
Der getrennte Beleg „Auto hat geladen“ hält bei unbekanntem Bedarf das
Rückfallziel nur am Tag der letzten Bestätigung ab 500 W und nur innerhalb
derselben Steck-Episode. Er endet bei Pause, Vollmeldung, bestätigtem Abstecken oder
nach 60 Minuten gültig unter 300 W, während genügend PV-Überschuss und ein
Stromangebot mindestens in Höhe des Fahrzeugminimums vorhanden sind.
Kurze Wolken, fehlendes Angebot, ungültige Proben und einzelne unbestätigte
Absteckmeldungen bis einschließlich zwei Minuten halten die Verfallsuhr nur an;
sie zählen nicht als Angebotszeit. Eine längere Unterbrechung setzt sie zurück.
Das Angebot verwendet den vorhandenen geglätteten Netzwert. Normale Akkuladung
ist kein Fahrzeugangebot: Für diese Uhr zählen Einspeisung und belegter
Mindeststrom. Ohne belegte Leistungsgrenzen oder Stromangebot läuft die Uhr
nicht; der Diagnosegrund bleibt sichtbar. Der Tagesbeleg liegt im Dienstspeicher
und benötigt nach dessen Neustart eine neue Bestätigung ab 500 W.
Bei Messlücken gilt für die Zielwahl höchstens die 600-Sekunden-Überbrückung.
Der getrennte Entladeschutz gilt bei gestecktem oder unbekannt gestecktem Auto,
geregelt wie beobachtet: Der Lückendeckel ist der höhere Wert aus Hausgrundlast
plus Wärmepumpe und dem letzten gültigen Defizitdeckel, höchstens 600 Sekunden
alt. Danach gilt nur Grundlast plus Wärmepumpe. Fehlende Daten setzen ihn nie
auf 0 W. Weder Hausmessung inklusive Wallbox noch ein Wallbox-Ersatzwert wird
zusätzlich gebucht. Bestätigtes Abstecken beendet den Schutz dieses Ladepunkts.

Zur normalen Kurve geht es ab 110 % Deckung aller drei Energieprüfungen,
zurück unter 100 %. Jede Wahl hält etwa 15 Minuten (30 Sekunden Toleranz
für den Planlauftakt). Entfallener Ladebeleg, fehlende PV-Lademöglichkeit,
Pause und bestätigtes Abstecken lassen die normale Wahl sofort zu.
Der Übergang nutzt die frisch berechnete Kurve mit einem gespeicherten
abklingenden Versatz, höchstens eine Stunde. Prognoseänderungen wirken
weiter; der Sprung je Planlauf bleibt auf eine maximale 15-Minuten-Ladung
begrenzt. Eine bloße Unwetterwarnung beendet die Überbrückung nicht; ein aktiver
Unwettereingriff, ein geändertes Ziel oder eine geänderte Reserve beenden sie.
Auch Entscheidungswechsel, Tageswechsel und Ablauf der Stunde beenden den
Übergang. Die Begrenzung gilt am aktuellen Kurvenpunkt; die Rampe danach
kann steiler sein, am Kurventagesende gilt unmittelbar das normale Ziel.
Eine relevante Intent-Änderung muss 60 Sekunden stabil bleiben.

Bei aktivem Rückfallziel darf die Unwetter-Vorladung die PV-Zielkurve bis
zum höheren Unwetterziel anheben, einschließlich des Endankers, aber nur
bis zum Warnende. Danach gilt wieder das PV-Ziel. Ohne aktives Rückfallziel
bleibt die Unwetterkurve auf das normale Speicherziel begrenzt.

`active_target_soc` und `can_reach_target` beziehen sich auf das wirksame
PV-Kurvenziel. `normal_target_soc` und `normal_target_can_reach` beschreiben
das normale Speicherziel separat. Pre-Dump und Preis-Netzladen behalten
ihr normales Ziel. Unwetter-Vorladeanker werden nicht auf das Rückfallziel
begrenzt. Bei aktivem Rückfallziel bleibt die Schlechtwetterreserve aus;
nach Kurvenende und für die Folgetagskurve gilt sie wieder. Der Folgetag
veröffentlicht eine normale Zielwahl. Notstromreserve, Nutzer-Aus und
andere Schutzgrenzen behalten Vorrang.

Im Plan nennen `wallbox_curve_target_state` und `wallbox_config_target_reach`
Entscheidung, Grund, Restüberschuss, Akku- und Fahrzeugbedarf samt Quellen
sowie aktives Ziel und Haltezeit. Fehlende Bedarfe stehen als `null` mit Grund.

### Einfrieren und Glätten

Vergangene Anker und der aktive Führungsbereich werden eingefroren. Zukünftige
Anker dürfen sich mit neuer Wetter- und Verbrauchsprognose noch bewegen.

Damit die Kurve am Tagesende nicht plötzlich eine harte Aufholkante bildet,
glättet der Simulator den Übergang zum Freilauf. Wichtig ist dabei:

- Bereits gestartete Anker dürfen an den echten Verlauf angepasst werden.
- Zukünftige Pre-Dump- oder Kurvenstartanker dürfen nicht einfach auf den
  aktuellen SoC hochgezogen werden.
- Das Tagesziel bleibt erreichbar, aber der letzte Abschnitt soll keine
  unrealistische 7-Prozent-Kante in 15 Minuten erzeugen.

## 3. Forecast-Plausibilität

Der Simulator nutzt PV-Prognose, saisonalen Nachtverbrauch und bekannte
Verbraucher. Prognose-Spitzen werden plausibilisiert:

- Anlagenleistung und Wechselrichterleistung bilden den physikalischen Rahmen.
- Kurze Cloud-Edge-Spitzen können plausibel sein, wenn wechselnde Bewölkung und
  helle Wolken auftreten.
- Breite, stundenlange Peaks oberhalb der Anlagenlogik werden gekappt, damit die
  Kurve nicht zu optimistisch wird.

Für den Start-SoC ist der Nachtverbrauch wichtiger als ein Tagesdurchschnitt.
Sommer-Wärmepumpen laufen oft tagsüber nur für Warmwasser; nachts ist der
Verbrauch deutlich niedriger. Deshalb nutzt die Planung saisonale
Nachtverbrauchswerte, soweit vorhanden.

## 4. Abregelreserve und adaptiver Headroom

Der adaptive Headroom trennt Abregeldruck, freien Speicherplatz und
Reserveband. Dadurch kann die Anzeige erklären, warum die Kurve Speicherplatz
frei hält, ohne daraus automatisch einen aktiven Entladeauftrag zu machen.

Die wichtigsten Größen sind:

- `curtailment_pressure_wh`: plausibler echter Abregeldruck im späteren
  Druckfenster.
- `adaptive_headroom_available_wh`: Speicherplatz, der im Druckfenster bereits
  frei ist.
- `adaptive_headroom_required_wh`: zusätzlicher Headroom, der rechnerisch über
  den aktuell freien Speicherplatz hinaus im Reserveband steckt. Im Dashboard
  wird daraus zusammen mit dem freien Platz `Headroom-Reserve max`.
- `adaptive_headroom_buffer_wh`: kleiner Regelpuffer auf den Headroom.
- `headroom_reserve_pressure_wh`: historisch oder live plausibilisierte
  Reserve für mögliche PV-Spitzen, zum Beispiel historische Peaks oder
  Cloud-Edge bei niedriger Prognose.
- `headroom_reserve_source`: Herkunft dieser Reserve, etwa `historical_peak`
  oder `live_cloud_edge`.

Wichtig für die Interpretation: `Headroom-Reserve max` ist ein Diagnose- und
Kurvenwert. Er sagt, welchen Speicherplatz die Regelung im ungünstigen
plausiblen Verlauf vorhalten kann. Er sagt nicht, dass diese Energie sofort
entladen werden muss. Ein echter Vorab-Auftrag steht nur im `Pre-Dump-Bedarf`.

Nach Kurvenstart wirkt Headroom zuerst über Kurvenunterkante, Oberkante und
EMS-Ladegrenzen. Aktive `DISCH`-Impulse sind seit 5.2.2a separat freigegeben:

- `storage_headroom_discharge_enable`: `0` verbietet aktive Headroom-Entladung,
  lässt Ladegrenzen und Kurvenband aber weiter wirken.
- `storage_headroom_discharge_daily_limit_pct`: Tageslimit für
  Headroom-DISCH in Prozent der Speicherkapazität.
- `storage_headroom_discharge_cooldown_min`: Pause zwischen einzelnen
  Headroom-Impulsen.

Der Zustand `parallel_headroom_discharge` darf nur entstehen, wenn PV läuft,
Exportraum vorhanden ist, der SoC oberhalb der aktuellen Unterkante liegt und
weder Wallbox, Kurvenladung noch echter Abregeldruck Vorrang haben.

## 5. Manager: AUTO mit EMS-Grenzen

Der Storage Manager bleibt im normalen PV-Betrieb bevorzugt im E3DC-AUTO-Modus.
Er setzt nur die relevanten EMS-Power-Settings:

```text
MAX_CHARGE_POWER      maximale Batterieladung
MAX_DISCHARGE_POWER   maximale Batterieentladung
POWER_LIMITS_USED     EMS-Grenzen aktiv/frei
```

Aktive harte Modi wie `GRID`, `DISCH` oder `IDLE` sind Schutzpfade und müssen
einen klaren Besitzer haben, z.B. manuelles Kommando, Preis-Netzladen,
Pre-Dump-Fallback, Unwetterreserve oder Notfall. Ein unbekannter harter Modus
wird nicht als normaler Kurvenzustand akzeptiert.

## 6. Weiche Kurvenführung

Die Kurvenladung besteht aus drei Größen:

- `iFc`: durchschnittliche Ladeleistung, die bis zum nächsten sinnvollen Anker
  gebraucht wird.
- Rückstand zur Kurve: Energie, die durch Verbraucher, Wolken oder Prognosefehler
  zusätzlich fehlt.
- verfügbare PV-/Exportreserve: was physikalisch ohne Netzbezug und ohne
  unnötiges Ping-Pong geladen werden kann.

Seit 5.0.6 wird der Rückstand nicht mehr als harter Vollgas-Sprung behandelt.
Der Manager mittelt den Aufholbedarf über ein Zeitfenster, tapert ihn nahe der
Kurve ab und integriert den sichtbaren Batteriefluss in einen
`curve_control_soc`. Dadurch reagieren Regler und Anzeige weniger auf
sekündliches SoC-Rauschen und die typische Sägezahnform wird gedämpft.

```text
unter der Kurve, aber nah dran -> iFc + sanfter Aufholterm
deutlich unter der Kurve       -> stärkerer Aufholterm, begrenzt durch PV/WR/Config
auf/über der Kurve             -> Ladegrenze reduzieren oder halten
```

Eine Wallbox oder Wärmepumpe mit echter Leistung wird dabei als Verbraucher
berücksichtigt. Die Wärmepumpe wird vom Energy Manager übernommen und aus dem
reinen Hausverbrauch herausgerechnet, damit sie nicht doppelt in die Regelung
eingeht.

## 7. Abregelschutz

Der Abregelschutz ist ein harter Pfad, aber er darf nicht kleben. Er greift nur,
wenn echte Einspeise- oder Wechselrichtergrenzen erreicht werden könnten:

- Export am Netzpunkt über der konfigurierten Zielkante,
- live gemeldetes E3DC-Derating,
- DC-Leistung oberhalb der Wechselrichterleistung, die sonst abgeregelt würde.

Oberhalb der Ladekurve lädt der Speicher nur den echten Abregelbedarf. Unter der
Kurve darf zusätzlich `iFc` beziehungsweise der sanfte Aufholbedarf wirken,
solange Exportdruck und PV-Reserve vorhanden sind. Dadurch bleibt der
Abregelschutz stabil, ohne große Aufholjagden durch die Hintertür zu erzwingen.

## 8. Freilauf

Freilauf bedeutet: Das Tagesziel beziehungsweise der letzte Führungsabschnitt ist
erreicht, und der E3DC soll wieder möglichst autonom arbeiten. Der Übergang wird
sanft geöffnet:

- kurz vor Freilauf werden harte Ladegrenzen nicht bis zur letzten Sekunde
  festgehalten,
- der Simulator setzt das Kurvenende um den Kurvenende-Puffer
  (`storage_curve_end_guard_min`, Standard 45 Minuten, Bereich 30–120) vor das
  prognostizierte Ende des nutzbaren PV-Überschusses; der Manager öffnet die
  Ladegrenze davor zusätzlich weich,
- der E3DC darf schwache Rest-PV autonom mitnehmen,
- am Abend wird keine Kurvenjagd mehr erzwungen, wenn keine relevante PV mehr
  kommt.

Das Ziel ist ein ruhiger Übergang ohne Sprung von `0 W` auf maximale
Speicherladung.

### Kurvenende im Zielkurven-Modus „Prognose auf 100%“

Im Modus „Prognose auf 100%“ (`storage_curve_target_mode = forecast_100`) kann
das Kurvenende später liegen: 100 Prozent werden dann erst kurz vor dem
PV-Ende geplant (PV-Ende minus Kurvenende-Puffer). Das hält Speicherplatz für
Mittagsspitzen frei und verhindert, dass ein großer Speicher stundenlang bei
100 Prozent steht. Dieser späte Vollstand gilt nur mit Grund:

- ein Einspeiselimit ist konfiguriert (`einspeiselimit`) und liegt unter der
  erwarteten PV-Spitze des Kurventags abzüglich 300 W Grundlast; ein Limit
  oberhalb der erreichbaren PV-Leistung regelt nie ab und zählt nicht,
- die Prognose zeigt für den Kurventag vermeidbaren Abregeldruck an der
  Einspeisegrenze, am gemeldeten Wechselrichter-Derating oder an der DC-Grenze
  (Eintritt ab 300 Wh),
- Direktvermarktung ist aktiv (`direct_marketing_enable`),
- Pre-Dump ist für den Kurventag geplant oder aktiv.

Ein eingetretener Abregeldruck- oder Einspeiselimit-Grund gilt bis zum Ende
des Kurventags. Die Rechnung betrachtet nur die noch kommenden Stunden; ohne
dieses Halten würde der Grund an jedem sonnigen Nachmittag wegfallen und das
Kurvenende täglich springen.

Ohne Grund endet die Kurve wie in der Ankerkurve am letzten nutzbaren
Überschuss minus Kurvenende-Puffer. Überschuss oberhalb der Kurve geht dann
mittags in den Speicher statt ins Netz, und Wolken am Nachmittag gefährden das
Tagesziel weniger.

Der Schalter `storage_forecast100_late_full_guard_enable` (Standard an, im
Konfigurations-Editor „Später Vollstand nur mit Grund“) schaltet den späten
Vollstand ganz ab (`0` = nie). Die Plan-Diagnose nennt den Grund in
`target_curve_meta.late_full_reason`: `none` = kein Grund, `disabled` =
abgeschaltet, sonst die Gründe, zum Beispiel `export_limit`,
`curtailment_pressure`, `direct_marketing` oder `predump`.
`target_curve_meta.late_full_export_limit_state` zeigt, ob das Einspeiselimit
bindet (`binding`, `held`) oder oberhalb der PV-Spitze liegt (`not_binding`);
`late_full_pv_peak_w` ist die dafür verwendete PV-Spitze. Die Ankerkurve ist
davon nicht betroffen.

## 9. Preis- und Netzladen

Netzladen bleibt ein separater Opt-in-Pfad. Die Ladekurve allein löst kein
günstiges Netzladen aus. Explizite Preis-/Negativpreisfenster, Unwetterreserve
oder manuelle Befehle dürfen Speichergrenzen setzen; unbekannte externe
Dauerlasten werden nicht geraten.

## 10. Diagnose

Für die Nachvollziehbarkeit sind diese Werte entscheidend:

| Feld | Bedeutung |
|---|---|
| `curve_soc_now` | aktueller Sollwert der Ladekurve |
| `curve_gap_pct` | Abstand zwischen Regel-SoC und Kurve |
| `i_fc_w` / `ifc_w` | Grund-Ladeleistung bis zum nächsten Anker |
| `curve_gap_catchup_w` | zusätzlicher, geglätteter Aufholterm |
| `rscp_charge_limit_w` | tatsächlich gesetzte EMS-Ladegrenze |
| `abregel_*` | physikalischer Abregeldruck und Zielkante |
| `adaptive_headroom_available_wh` | aktuell freier Speicherplatz im Headroom-Druckfenster |
| `adaptive_headroom_required_wh` | rechnerischer Zusatzanteil des Reservebands, kein alleiniger Entladeauftrag |
| `headroom_reserve_pressure_wh` | historisch oder live plausibilisierte Headroom-Reserve |
| `headroom_reserve_source` | Quelle der Reserve, z.B. historische Spitze oder Live-Cloud-Edge |
| `headroom_discharge_*` | Tageslimit, Pause, Blockgrund und Leistung aktiver Headroom-DISCH-Impulse |
| `WP_Power`, `Home_Power_Raw`, `Home_Power` | getrennte Verbraucherbilanz |

Die dazugehörigen Betreiber-Schalter heißen
`storage_headroom_discharge_enable`,
`storage_headroom_discharge_daily_limit_pct` und
`storage_headroom_discharge_cooldown_min`.

Wenn die reale Batterieladung stark schwankt, zuerst prüfen:

1. Ändert sich der gesetzte RSCP-Sollwert oder nur die E3DC-Reaktion?
2. Ist ein harter Schutzpfad aktiv?
3. Wird WP-/Wallboxleistung doppelt oder gar nicht im Hausverbrauch geführt?
4. Ist ein zukünftiger Anker durch einen Prognose- oder Glättungsfehler
   gewandert?
5. Wird `Headroom-Reserve max` als offener Entladeauftrag gelesen, obwohl der
   echte Auftrag nur bei `Pre-Dump-Bedarf` oder `parallel_headroom_discharge`
   liegt?
