# Ladekurve - technische Dokumentation

> **Stand:** V5.2.2a, aktualisiert am 2026-06-16

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

Der Rückstand wird nicht als harter Vollgas-Sprung behandelt.
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
- der Simulator setzt den letzten aktiven Kurvenabschnitt nur noch 30 Minuten
  vor das prognostizierte Ende des nutzbaren PV-Überschusses, weil der Manager
  die Ladegrenze selbst innerhalb eines 30-Minuten-Fensters weich öffnet,
- der E3DC darf schwache Rest-PV autonom mitnehmen,
- am Abend wird keine Kurvenjagd mehr erzwungen, wenn keine relevante PV mehr
  kommt.

Das Ziel ist ein ruhiger Übergang ohne Sprung von `0 W` auf maximale
Speicherladung.

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
