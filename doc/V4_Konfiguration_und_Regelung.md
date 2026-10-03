# Konfiguration und Regelung

Diese Datei beschreibt den aktuellen Stand der nativen Python-Architektur.

## Konfigurationsquelle

Die kanonische Konfiguration ist:

```text
/var/www/html/data/e3dc_v4.json
```

`e3dc.config.txt` ist nur noch ein Legacy-Import und Fallback für alte
Installationen, Migrationen und einzelne Debug-Werkzeuge. Neue
WebUI-Einstellungen, Dienste und Installer-Optionen sollen in `e3dc_v4.json`
gespeichert werden.

Wichtige Folgen:

- Der Config-Editor arbeitet direkt mit `e3dc_v4.json`.
- Python-Dienste lesen zuerst `e3dc_v4.json`.
- `e3dc.config.txt` darf nicht mehr als primäre Quelle für neue Features
  verwendet werden.
- `e3dc.wallbox.txt` und `e3dc.wallbox.out` bleiben nur für Legacy-Importe,
  alte Debug-Werkzeuge und historische Ladepläne relevant.

## Aktive Dienste

| Dienst | Aufgabe | Primäre Datenquelle |
|---|---|---|
| `e3dc-live` / `e3dc_live.py` | Live-Daten aus RSCP in die Ramdisk schreiben | `e3dc_v4.json` |
| `e3dc-storage-simulator` / `storage_simulator.py` | PV-, Wetter-, Preis- und SoC-Plan für die Ladekurve berechnen | `e3dc_v4.json`, Ramdisk |
| `e3dc-storage-manager` / `storage_manager.py` | Batterie-EMS nach Ladekurve, Pre-Dump, Abregelgrenze und Preislogik führen | `storage_plan.json`, `storage_manager_state.json` |
| `e3dc-wallbox-manager` / `wallbox_manager.py` | Native und externe Wallboxen nach Budget, Modus und Hysterese regeln | `storage_manager_state.json`, `e3dc_v4.json` |
| `energy_manager` | Luxtronik, IDM, Stiebel/ISG, SG-Ready und Heizstab nach Budget steuern | `storage_manager_state.json`, `e3dc_v4.json` |
| `e3dc-epex-manager` / `epex_manager.py` | EPEX/SMARD/ENTSO-E/aWATTar-Preise, Nutzerpreis und Eco-Score liefern | `e3dc_v4.json` |
| `e3dc-bluelink` / `bluelink_client.py` | Hyundai/Kia SoC in `vehicles.json` schreiben | `e3dc_v4.json` |
| `e3dc-mqtt-hub` / `e3dc_mqtt_hub.py` | Livewerte publizieren und freigegebene MQTT-Messwerte annehmen | `e3dc_v4.json`, Ramdisk |

Der Live-Dienst kann zusätzlich einen zweiten, netzgekoppelten Wechselrichter
per Modbus TCP direkt auslesen (`ext_inverter_type`, `ext_inverter_ip`,
`ext_inverter_port`, `ext_inverter_unit_id`, `ext_inverter_poll_s`). Die Lesung
ist nur Diagnose und liefert Phasen- und String-Daten in `live_data_py.json`;
die Regelung nutzt weiterhin den E3DC-Messwert des externen Leistungsmessers.
Details: [Zusatzwechselrichter](Zusatzwechselrichter.md).

## Regelungsphilosophie (verbindlich)

Dieser Abschnitt beschreibt die Grundsätze, nach denen E3DC-Control Speicher,
Wallboxen und Wärmeerzeuger regelt. Er ist verbindlich: Jede Änderung an der
Regelung, jede neue Schwelle und jeder neue Sonderpfad wird an diesen Regeln
gemessen. Wo eine Regel im Code an einem Konfigurationsschlüssel oder Zustand
hängt, ist er genannt; die Detailabläufe stehen in `doc/Native_Wallbox.md`,
`doc/Speicher_Ladesteuerung_Ablauf.md` und `doc/Pre_Dump.md`.

### Oberste Regelziele

Harte Schutzschranken stehen vor jeder Optimierung. Nutzer-`Aus`, ausdrückliche
Freigaben, Hardwarelimits, Hausanschlussgrenzen (`grid_max_amps`), Notstrom- und
Hausreserve (E3DC-Livereserve, Rückfall `ep_reserve_pct`), Datenvalidität und
sichere Rückfallpfade schlagen Autarkie, Ertrag und Komfort. Innerhalb dieser
Schranken gilt die folgende Rangfolge:

1. **Minimaler Netzbezug, maximale Autarkie.** PV wird zuerst im Haus, im
   Speicher, im Fahrzeug, in der Wärme oder in anderen steuerbaren Verbrauchern
   genutzt, bevor eingespeist oder abgeregelt wird.
2. **Ertrag und Kosten.** Spotmarkt (EPEX/aWATTar/Octopus), Preisfenster und
   Direktvermarktung werden wirtschaftlich bewertet: günstig laden, teuer
   verkaufen, Abregelung vermeiden. Preislogik ist Opt-in und ersetzt keine
   Schutzschranke.
3. **Netzdienlichkeit.** Lastspitzen vermeiden (Peak Shaving), harte Netz- und
   Hausanschlussgrenzen einhalten.
4. **Bedarfsgerechte Steuerung.** So viel wie nötig, nicht mehr. Komfortgrenzen
   wie die Mindesttemperatur der Wärmepumpe, die Hausreserve, `wbminsoc` und der
   Fahrzeug-Ziel-SoC werden eingehalten, aber nicht sinnlos übererfüllt.
5. **Maximale Energieausbeute.** Prognosen für PV, Hausverbrauch, Wärmebedarf,
   Fahrprofile und Preise dienen dazu, Abregelung und unnötige Einspeisung zu
   minimieren.
6. **Ein Entscheider je Aktor.** Pro Zyklus und Aktor gibt es genau einen
   fachlichen Entscheider und genau einen Hardware-Ausgang: Der Storage Manager
   entscheidet über Speicher und RSCP, die zentrale Wallbox-Policy mit dem
   Wallbox Manager über die Wallboxen, der Energy Manager über Wärmepumpe,
   SG-Ready und Heizstab. Treiber, Wächter und Diagnosepfade liefern Eingaben
   und Anzeigen, senden aber keine konkurrierenden Kommandos.
7. **Physik und Praxis vor reiner Softwarelogik.** Jede Regel und jede
   Parameterprüfung muss zur physischen Realität der Anlage passen:
   - *Nutzer-Pragmatismus vor harten Grenzen.* Werte, die aus dem Alltag
     kommen (zum Beispiel ein tiefes `wbminsoc` für die morgendliche
     Reichweite), werden nicht mit einem Abbruch blockiert, sondern mit einer
     beratenden Warnung des Konfigurationsvalidators begleitet.
   - *Eiserne Hardware-Limits.* Eine Konfiguration, die die Notstromreserve des
     Speichers antastet, wird als harter Fehler abgewiesen; der Validator lehnt
     zum Beispiel ein `wbminsoc` unterhalb der wirksamen Notstromreserve ab.
   - *Elektromechanischer Schutz der Aktorik.* Ein Phasenwechsel an der openWB
     Pro verwendet nur die kurze, vom Gerät benötigte CP-Unterbrechung. Nach
     einer bestätigten Umschaltung sperrt eine feste, nicht konfigurierbare
     Hardware-Sperre von mindestens 480 s ausschließlich einen weiteren
     Phasenwechsel. Sie blockiert weder den bestätigten Wiederanlauf noch die
     laufende Ladung. Davon getrennt sind die Beharrungszeiten vor einem
     Wechsel: abwärts `wb_phase_down_delay_s` (an der openWB Pro Standard
     480 s, Untergrenze 60 s), aufwärts `wb_phase_up_forecast_hold_s`
     (Beharrung 1p→3p Standard 60 s, Untergrenze 30 s) auf einem
     30-s-Mittel des verfügbaren Überschusses (Einspeisung plus Wallbox oder
     frisches Budget des Speicherreglers, Speicher-Entladung zählt als
     Defizit) als leckende Uhr – Wolkenlücken zählen sie zurück statt sie zu
     löschen – oder alternativ das Export-Wh-Konto (Standard 120 Wh). Für ein
     phasenschaltfähiges Paar aus Fahrzeug und Wallbox gilt als „1p
     ausgereizt“ der Referenzstrom aus 3p-Minimum plus Puffer (etwa 20 A),
     nicht der dynamische 1p-Deckel. Sie filtern Wolkenlücken und kurze
     Lastspitzen vor einem Schützwechsel aus; die Beruhigung nach dem Schalten
     ist allein die Hardware-Sperre. Während dieser Sperre zählen weder die
     Uhr noch das Energiekonto: beide werden danach neu verdient, damit auf
     einen Abstieg kein sofortiger Wiederaufstieg folgt. Das gilt für jeden
     Wallbox-Typ gleich: Am E3DC-Direktvertrag ist die Haltezeit nach jedem
     Wechsel diese Sperre, und das experimentelle 10-min-Fenster beginnt in
     ihr ebenfalls neu.
   - *Anti-Flattern.* Hysterese, Deadband, Zeitbedingungen und Energiewächter
     werden gemeinsam aus Messrauschen, realen Leistungsstufen und dem Verhalten
     der Aktorik ausgelegt. Jede Kante bekommt ihre eigene, passende Schwelle
     (etwa das Export-Wh-Konto für die Hochschaltung 1p→3p); es gibt keine
     pauschale Leistungsdifferenz für alle Ein- und Ausschaltschwellen.

### Regelungstechnik und Hardware-Schutz

- Reale Relais, Schütze, Wechselrichter und Wallboxen dürfen nicht flattern.
  Jede Schwelle hat eine passende Hysterese, ein Deadband, eine Mindestlaufzeit,
  eine Wiedereinschaltverzögerung oder eine Entprellung. Beispiele:
  `wb_soc_hysterese_pct` an `wbminsoc`, die Beharrungszeiten des
  Phasenwechsels, die 120-s-Freigabe und 45-s-Gnadenfrist des Mindesthalts
  sowie die 120-s-Budgetstabilität für den Wiederanlauf nach einem
  Kaskaden-Stop (`wb_pv_only_release_hold_s`, `wb_pv_only_hold_stale_guard_s`).
- Zustandsautomaten haben saubere Übergänge. `Aus` heißt `Aus`: Deaktivierte
  Systeme werden beobachtet, aber nicht aktiv angesteuert.
- Fehlende oder unplausible Messwerte werden nie als echte `0` geschrieben,
  wenn eine `0` später als Messwert gelesen werden könnte. Stattdessen `null`
  plus Gültigkeit und Quelle (`*_valid`, `*_source`). Fehlende Daten sind keine
  Freigabe: Ohne frische, gültige Live-Werte bleiben Budgets geschlossen,
  Konten eingefroren und Aktoren im sicheren Zustand.
- Notstromschutz: An der Notstromreserve wird die Entladung sofort gesperrt.
  Ein Export- oder Verkaufsbefehl (`FORCE_EXPORT`) endet an dieser Grenze
  beziehungsweise am Reserveboden der Direktvermarktung; der Speicher fällt in
  den normalen Betrieb zurück, unabhängig davon, was Preis- oder
  Direktvermarktungslogik verlangt.
- Flash-Schreibschutz: Kurzzeitige Leistungsvorgaben laufen über die flüchtige
  Vorgabe `EMS_REQ_SET_POWER`. Den Laderahmen im AUTO-Betrieb setzt die Regelung
  über `EMS_REQ_SET_POWER_SETTINGS`. Die E3DC setzt diese Einstellung bei einem
  Neustart zurück; die Regelung schreibt sie trotzdem nur bei
  echter Änderung, nicht schützende Änderungen des Laderahmens höchstens alle
  30 s, bestätigt jeden Schreibvorgang per Rücklesen und zählt jeden
  gesendeten Schreibvorgang im Protokoll des Speicherreglers, auch wenn ihn
  erst der nächste Live-Readback bestätigt: Ein Wechsel in die Grenzklasse
  „Entladen 0 W“ (Entladung gesperrt) oder aus ihr heraus steht immer mit den
  Sollwerten in einer eigenen Zeile, auch ein kurzer. Andere Wechsel der
  Grenzklasse (Grenzen aus; Laden 0 W oder begrenzt; Entladen begrenzt oder
  frei) gegenüber der zuletzt protokollierten Klasse bekommen höchstens alle
  10 s eine eigene Zeile; ein so zurückgestellter Wechsel steht spätestens in
  der nächsten Sammelzeile. Alle übrigen Schreibvorgänge fasst eine Sammelzeile
  mit Zähler, Startzeit und letztem Sollwert zusammen; sie erscheint
  spätestens 60 s nach dem ersten noch nicht protokollierten Schreibvorgang,
  auch wenn danach keiner mehr folgt, und beim Beenden des Speicherreglers.
  Fehler und unbestätigte SET-Antworten eines gesendeten Schreibvorgangs haben
  eigene Warn- und Fehlerzeilen mit der RSCP-Antwort des E3DC, die die Dämpfung
  wiederholter Meldungen nicht zurückhält; gleichlautende Wiederholungen
  derselben Ursache (gleiche Meldung, gleiche RSCP-Antwort) erscheinen
  höchstens einmal je Minute und zählen sonst in der Sammelzeile.
  Einstellungen, die der E3DC nachweislich dauerhaft speichert, schreibt die
  Regelung nie zyklisch.
- Diagnose-Reihenfolge bei unerklärlichem Verhalten: zuerst Messwertqualität,
  physikalische Glitches, Netzwerk-/API-Latenz, veraltete Daten, Phasenmessung
  und Hardwaregrenzen prüfen, bevor Einstellungen oder Regeln geändert werden.
- **Reaktionszeit des Speichers.** Der Hausspeicher gleicht eine Laständerung
  am Netzpunkt erst nach einigen Sekunden aus (am E3DC 5–8 s bis zum Umschwung
  des Akkus, sichtbar im 3-s-Takt der Livewerte). Die Wallbox-Regelung regelt
  nicht dagegen: Nach neuem Netzbezug – nach einer eigenen Anhebung oder bei
  einer kurzen Lastspitze wie einem Induktionskochfeld – senkt der Defizitregler
  erst ab, wenn der Bezug die Einschwingfrist `wb_grid_import_settle_s`
  (Standard 10 s, 0 = aus) überdauert. Angehoben wird erst nach Beruhigung und
  in Schritten von höchstens 2 A je Wallbox; in einer Gruppe kann jede
  Wallbox im selben Zyklus eine Stufe anheben. Harte Grenzen wirken sofort:
  Hausanschluss je Phase
  (`grid_max_amps_l1..3` abzüglich `grid_wallbox_reserve_amps_l1..3`),
  Phasen- und Schieflastdeckel, Nutzer-`Aus` und Netzbezug über der Reichweite
  des Speichers (aktuelle Ladeleistung plus der noch freie Teil der wirksamen
  Entladegrenze; diese ist der kleinste Wert aus `maximaleentladeleistung`,
  gesetzter und vom E3DC genutzter Entladegrenze). In der PV-only-Klasse, an
  der Notstromreserve und ohne gültigen Leistungseinstellungsbereich der
  Livewerte (Reichweite unbekannt) gilt keine Frist. Der Keepalive der openWB
  Pro wiederholt nur den
  zuletzt ausgegebenen Sollstrom und ist kein zweiter Entscheider. Details und
  Energiebetrachtung: `doc/Native_Wallbox.md`, Abschnitt „Netzbezug beim Laden:
  Einschwingfrist, Anhebung und Grid-Wächter“.
- **Ungültige Liveprobe.** Eine einzelne ungültige Probe stoppt keine laufende
  Ladung: Bei allen geregelten Wallbox-Typen bleibt der zuletzt ausgeführte
  Strom höchstens 10 s nach der letzten gültigen Probe (ihrem Zeitstempel)
  stehen, ohne Anheben, Neustart oder Phasenwechsel; je Wallbox dauern alle
  solchen Halte in 60 s zusammen höchstens 10 s. Voraussetzung ist der letzte
  gültige Zyklus: frischer Status der ladenden Wallbox, gültiges Budget für den
  laufenden Mindeststrom und kein Netzbezug über der Toleranz; ein als gültig
  markierter Netzwert mit Bezug in der ungültigen Probe verhindert den Halt
  ebenfalls. Sonst gilt das bisherige Verhalten. Nutzer-`Aus`, Pause, Sperre,
  Notaus, Zwangsstopp, Notstromreserve, Ladefensterende, Ladeende sowie
  Hausanschluss- und Phasenstromüberlast wirken sofort; Absenkungen bis
  hinunter zum Mindeststrom (ohne Schütz) laufen durch. Details:
  `doc/Native_Wallbox.md`, Abschnitt „Ungültige Liveprobe: kurzer Halt“.
- **Eine Stellgröße je Regelkreis.** Innerhalb eines Regelkreises verändert die
  Regelung nach Möglichkeit nur eine Größe, damit sich Regler nicht gegenseitig
  aufschaukeln. Speicher und Verbraucher sind über das Budget entkoppelt; jede
  Seite hat genau eine eigene Stellgröße.
  1. **Speicher – Laderahmen.** Die Speicherladung ist das obere Stellglied.
     Liefert der Speicherplan eine Ladekurve, ist `iFc` der Laderahmen: die
     durchschnittlich nötige Ladeleistung bis zum nächsten Kurvenanker,
     einschließlich gedämpftem Aufhol- und Abregelschutzbedarf. Ohne Ladekurve
     (Kurvenlage `no_curve`) oder beim Halten für ein Hochpreisfenster ist das
     Laden frei; eine PV-Spitze nimmt der Speicher dann ohne zusätzliche
     Regelung auf. Verbraucher verstellen diesen Rahmen nicht.
  2. **Speicher – Entladegrenze.** Sie wird nicht gegen den Netzpunkt
     geregelt, sondern legt fest, wen der Speicher versorgt, etwa Hausverbrauch
     und Wärmepumpe, aber nicht das Auto. Darf der Speicher keinen Verbraucher
     versorgen, ist sie 0, und alle Verbraucher beziehen aus dem Netz. Die
     Grenze der PV-only-Klasse unter dem Kurvenkorridor (Akkustützung nach
     Korridorlage, Punkt 3) gilt nur, solange eine Wallbox tatsächlich lädt
     (Ein- und Aus-Kante dort), und folgt dabei dem Hausbedarf; nachgeführt
     wird sie erst ab einer Änderung von 200 W (Totband), eine Senkung durch
     eine harte Grenze (Gerätegrenze, Notstromreserve, 0 W eines
     Schutz- oder Haltezustands) wirkt sofort. Von E3DC-Control geregelte
     Wallboxen senkt zuerst der Wallbox Manager im Strom ab, und das
     Akku-Wh-Konto stoppt sie an seiner Schwelle; die Entladegrenze ist für
     sie nur eine zusätzliche Begrenzung. Wallboxen, die E3DC-Control nur misst
     und nicht selbst regelt (zum Beispiel eine openWB mit eigener Regelung
     oder eine andere Wallbox mit Leistungsmessung), begrenzt es über die
     Entladegrenze des Speichers entsprechend dem gewählten Modus; für sie ist
     sie die einzige Stellgröße, das Totband der PV-only-Klasse gilt dort
     nicht, und Senkungen wirken sofort. Läuft eine Wallbox-Ladung bereits, darf der Speicher sie
     auch unter dem Kurvenkorridor im Rahmen des Wh-Kontingents an der
     Untergrenze stützen, damit kurze PV-Lücken nicht zu Schützschalten führen;
     ist es aufgebraucht, übernimmt die reguläre Halte-, Phasen- und
     Stop-Politik.
  3. **Verbraucher.** Was nach dem Laderahmen an PV übrig bleibt, geht als
     Budget an die Verbraucher. Jeder Verbraucher regelt nur seine eigene
     Leistung am Budget, die Wallbox zum Beispiel ihren Ladestrom. Ein
     einzelner Messwert einer externen Wallbox mit weniger Leistung je Ampere
     oder weniger genutzten Phasen senkt ihr Budget erst, wenn die Folgeprobe
     ihn bestätigt, und nur, wenn der E3DC-Hauswert (er enthält die externe
     Wallbox) den Einbruch nicht schon zeigt. Sofort wirken: ein vom Hauswert
     bestätigter Einbruch, Netzbezug, ein gesenkter ausgegebener oder von der
     Wallbox bestätigter Ladestrom (der Deckel allein genügt nicht), ein
     geändertes Phasenziel sowie ein laufender oder in den letzten 10 s
     beendeter Phasenwechsel. Zeigt der Hauswert eine eigene Stromabsenkung
     schon, die Wallboxprobe aber noch nicht, zählt das Budget die Leistung
     nicht doppelt: Wallboxwert und E3DC-Rest stammen aus demselben
     Probenpaar.
  4. **`PV + Akku bis Untergrenze`.** Die Stellgröße ist der Ladestrom der
     Wallbox. Der Speicher lädt weiter nach der Ladekurve mit `iFc` als
     Laderahmen, der PV-Rest geht als Budget an die Wallbox. Der Modus schaltet
     den Speicher nicht in den freien Automatikbetrieb, damit auch ein großer
     Speicher nicht vorzeitig voll ist. Oberhalb der Haltezone darf der
     Speicher die Wallbox bei Akku-Bezug unabhängig von der Kurvenlage bis
     `wbminsoc` stützen. In der Haltezone direkt über `wbminsoc` bekommt die
     Wallbox nur den batterieneutralen PV-Rahmen (Akkustützung, Punkt 5).
     Unterhalb von `wbminsoc` verhält sich der Modus wie `PV-Kurve ruhig`; an
     der Untergrenze senkt beziehungsweise stoppt der Wallbox Manager die
     Ladung. Im Wallbox-Automatikbetrieb des Speichers (`parallel_wb_auto`)
     setzt der Speicher in diesem Modus keine Entladegrenze: Eine geregelte
     Wallbox wird zuerst abgesenkt, und das Akku-Wh-Konto (Akkustützung,
     Punkt 4) schaltet sie bei Überschreiten seines Budgets ab.

### Zentrale Wallbox-Policy

- Die fachliche Wallbox-Entscheidung liegt zentral in den Policy-/Decision-
  Modulen unter `Installer/Wallbox/`, nicht in den einzelnen Treibern.
- Für alle aktiv geregelten Wallbox-Typen (E3DC, openWB, openWB Pro, go-e)
  gelten dieselben Grundentscheidungen: Laden, Halten, Reduzieren, Stoppen,
  nicht anfassen, Budget, Zielstrom, Netzfreigabe, Speicherstützung,
  `wbminsoc`-Grenzen, Pre-Dump, Preisfenster, Ladefenster und Hausabsicherung.
  Es gibt keine eigene Ladephilosophie je Wallbox-Typ.
- Treiber enthalten nur Gerätefähigkeiten, Messwert-Normalisierung und
  Befehlsübersetzung. Unterschiede wie 1p/3p, Umschaltfähigkeit, Fahrzeug nur
  einphasig, Mindeststrom, Weckverhalten oder fehlende Steuerfähigkeit sind
  Fähigkeits-Eingaben der zentralen Policy, keine eigenen Regelwelten.
  Messprotokoll und Steuerautorität sind zwei verschiedene Dinge.
- `Aus` ist der bewusste Beobachtungsmodus: keine Regelbefehle, keine
  Auto-Erkennung. Nur beim bewussten Wechsel auf `Aus` in der WebUI wird einmalig
  die Wallbox-Grundeinstellung freigegeben, nicht bei Reconnect oder Neustart.
  Deaktivierte Wallboxen werden weder erkannt noch gesteuert.

### Akkustützung der Wallbox in `PV-Kurve ruhig` nach Korridorlage

1. **Grundsatz.** In `PV-Kurve ruhig` lädt die Wallbox aus PV-Überschuss. Der
   Hausspeicher ist keine Ladequelle für das Fahrzeug. Er darf die Wallbox nur
   im Rahmen eines kleinen, benannten Wolken-Kontingents stützen; ob und wie
   viel, entscheidet die Speicher-Ladekurve, nicht ein fester SoC-Wert.
   Akkuladen des Fahrzeugs bis zu einer Untergrenze gibt es ausschließlich in
   `PV + Akku bis Untergrenze` (Untergrenze `wbminsoc`).
2. **Bezugsgröße.** Die Lage des Speicher-SoC zum Kurvenkorridor zwischen
   Untergrenze (`soc_min_curve`) und Obergrenze (`soc_ceiling_curve`) des
   Speicherplans: `above_ceiling`, `inside_band`, `below_floor` oder `no_curve`
   (Feld `adaptive_curve_relation` im Storage-Zustand).
3. **Regel je Korridorlage.**
   - Über dem Korridor: Der Storage Manager autorisiert Stützung (Grund
     `curve_above_target`), begrenzt auf den Anteil über dem Kurvenziel. Das
     Wallbox-Budget bleibt PV-basiert; Akkuenergie, die im Automatikbetrieb des
     E3DC dennoch in die Wallbox fließt, zählt in das Akku-Wh-Konto
     (Punkt 4).
   - Im Korridor: Laden fortsetzen (Grund `curve_within_corridor`); der Strom
     wird so geführt, dass möglichst lange ohne Schützwechsel geladen wird, mit
     Reduktion bis zum Mindeststrom vor jedem Stop. Stützung nur im Rahmen
     dieser Haltung und ebenfalls nur bis zum Akku-Wh-Konto (Punkt 4).
   - Unter dem Korridor: Das Wallbox-Budget ist der reine PV-Überschuss
     (`wallbox_curve_pv_only_budget_w`). Solange eine Wallbox tatsächlich lädt,
     begrenzt der Storage Manager die Entladung, damit der Automatikbetrieb des
     E3DC die Wallbox nicht aus dem Speicher speist: auf die Hausgrundlast laut
     Wallbox-Manager plus 200 W, mindestens `wb_curve_house_baseline_min_w`
     (Standard 1500 W, Untergrenze 300 W), in `PV-Kurve ruhig` zusätzlich auf
     Hausverbrauch plus Wärmepumpe minus PV zuzüglich der Reserve
     `wb_curve_pv_only_house_reserve_w` (Standard und Mindestwert 300 W). Das
     gilt in `PV-Kurve ruhig` und in den Speicherzuständen mit EMS-Grenze;
     im Wallbox-Automatikbetrieb von `PV + Akku bis Untergrenze`
     (`parallel_wb_auto`) setzt der Speicher keine Entladegrenze, dort
     begrenzen die Absenkung durch den Wallbox Manager und das Akku-Wh-Konto
     (Punkt 4). Nachgeführt wird die Grenze erst ab 200 W Änderung (Totband);
     eine Senkung durch eine harte Grenze wirkt sofort.
     Die Grenze schaltet ein, sobald eine Wallbox ab 500 W lädt oder ihre
     Messung ungültig, veraltet, unvollständig oder für ein laut Wallbox
     Manager gestecktes Fahrzeug nicht vorhanden ist. Sie schaltet erst wieder
     aus, wenn jeder gesteckte Ladepunkt 45 s ohne Unterbrechung gültig und
     frisch unter 300 W misst; eine einzelne 0-W-Probe hebt sie nicht auf.
     Unter 500 W darf der Speicher das Fahrzeug weiter mitversorgen (etwa
     beim Ausklingen der Ladung); die Zeitbedingung verhindert, dass die
     Grenze an der Schwelle flattert. Ist das Fahrzeug nur gesteckt und lädt
     nicht, bleibt die Entladung frei.
     Der Laderahmen des Speichers folgt dabei der Kurvenführung über `iFc`.
   - Wh-Kontingent an der Untergrenze: Unter dem Korridor darf der Speicher die
     Wallbox nur mit einem kleinen Energiekontingent stützen, um Wolkenlücken am
     Mindeststrom zu überbrücken. Das Kontingent ist `wb_curve_floor_support_wh`
     (0 oder leer = automatisch 0,5 % der Speicherkapazität `speichergroesse`,
     mindestens 50 Wh; ein Handwert gilt absolut, nie unter 50 Wh).
     Diese Kontingentregel gilt nur in `PV-Kurve ruhig` unter dem Korridor.
     Die Modi mit Akkuladen bis zur Untergrenze behalten den Pfad aus Punkt 5.
     Unter dem Korridor steuert genau dieses Kontingent die Speicherklasse,
     die Wallboxaktion und die Anzeige. An seinem Ende folgt im selben Zyklus
     genau eine Entscheidung je Kontingent, unabhängig vom bisherigen Ladestrom
     (Aktionsgrund `curve_floor_contingent_end`):
     - Trägt das batterieneutrale PV-Budget die Mindestleistung der aktuellen
       Phasenzahl (konfigurierter Mindeststrom, Standard 6 A, · 230 V · Phasen), lädt die Wallbox weiter aus PV;
       ein höherer Strom wird auf das PV-Budget abgesenkt.
     - Andernfalls folgt ein Abstieg von 3p auf 1p nur bei verfügbarem
       Phasenausgang, freier Phasensperre und genügend PV für den Wiederanlauf samt Messreserve
       (an der openWB Pro bei 6 A und 0,1-A-Schritten 1530 W).
     - In allen anderen Fällen, auch bei unbekanntem, ungültigem oder
       veraltetem PV-Budget, folgt Stop. Der Wiederanlauf folgt Punkt 4.
     Maßgeblich ist das batterieneutrale Gruppen-PV abzüglich der realen
     Leistung anderer Wallboxen, ohne Klemme durch die Slot-Zuteilung.
     Bei frischem Status wird die reale Leistung jeder anderen Wallbox abgezogen.
     Ohne frischen Status macht eine zuletzt ladende Box (mindestens 500 W
     oder Status „charging“, letzte gültige Probe höchstens zehn Minuten alt)
     den Rahmen unbekannt. Eine zuletzt nicht ladende, seit Dienststart nie
     gesehene oder seit mehr als zehn Minuten ohne gültigen Status gebliebene
     Fremdbox zählt für dieses Budget mit 0 W; die PV-only-Grenze und
     das Netzkonto bleiben wirksam. Die eigene letzte gültige Probe darf bis
     zu 15 s alt sein. Ein Stop bei unbekanntem Budget nennt den Statusgrund
     im Journal.
     Während der Brücke wird die gewählte Aktion fortgeschrieben, nicht neu
     entschieden. Danach gelten wieder Netz- und Budgetkonto, Einschwingfrist,
     Fast-Grid und Wh-Wächter. Ein erschöpftes Kontingent löst keine weitere
     Absenkung, keinen weiteren Phasenwechsel und keinen weiteren Stop aus.
     Die Speicherstützung bleibt bis zur frischen Bestätigung unter 500 W
     erhalten; dies bestätigt auch den 0-A-Schritt eines Phasenabstiegs.
     Spätestens 30 s nach dem Kontingent-Ende gilt dennoch die PV-only-Grenze,
     falls die Wallbox nicht folgt. Trägt PV die aktuelle Mindestladung,
     gilt PV-only sofort. Harte Schutzgrenzen behalten Vorrang.
     Das Kontingent zählt nicht bei ausdrücklich erlaubter Stützung:
     offenem wbminSoC-Tor in den Modi mit Akkuladen bis zur Untergrenze,
     Grundladung, Netz-, Preis-, Pre-Dump-, Direktvermarktungs- oder
     Prognosefreigabe sowie einer gebundenen Startreservierung des Speichers
     für eine real ladende Wallbox. Diese Ausnahmen ändern nur Zählung und
     Anzeige, nicht die bestehende Stützungsfreigabe, Klasse oder deren Grund.
     Es beginnt nach 60 s ohne geltende Korridorregel oder ohne Ladung wieder
     bei null. Das gilt auch nach 60 s ausdrücklich erlaubter Stützung.
4. **Akku-Wh-Konto im laufenden Mindesthalt.** Der gemeinsame
   Defizitregler, der eine laufende Wallbox bis zum Mindeststrom reduziert und
   dort hält, führt für die marginale Wallbox in `PV-Kurve ruhig` ein drittes
   Energiekonto. Es zählt den Anteil der Speicherentladung, der tatsächlich in
   die Wallbox fließt: min(Entladung, Wallbox-Ist − PV-Rest) abzüglich 100 W
   Toleranz, mit dem Leck `wb_min_current_import_release_w` (Standard 80 W).
   Schwelle ist dasselbe Kontingent wie unter Punkt 3. In und über dem
   Korridor bleibt dieses Konto unverändert. Unter dem Korridor gilt allein
   der Kontingentstand aus Punkt 3, ohne zweite Zählung mit Toleranz und Leck;
   nur seine einmalige Aktionswahl am Ende ersetzt dort die Stufenfolge
   Strom, Phase, Stop. Mit der Endentscheidung beginnt das Akkukonto wieder
   bei null. Nach Ende der Brücke zählt es wieder mit 100 W Toleranz und
   80 W Leck als zeitlicher Rückhalt gegen weitere Akkuentladung.
   Netz- und Budgetkonto bleiben wirksam; die Endentscheidung bleibt einmalig.
   Es gilt nur ohne Netz-, Preis-, Pre-Dump-,
   Direktvermarktungs- oder Prognose-Akkufreigabe und ohne Startreservierung
   der Speicherseite; in `Grundladung stabil` und den Modi mit Akkuladen bis
   zur Untergrenze ist die Stützung ausdrücklich gewollt und wird nicht
   gezählt. Je Zyklus zählt genau eine Komponente in der Rangfolge
   **Netz > Akku > Budgetüberziehung**: Das Netzkonto
   (`wb_min_current_import_stop_wh`, Standard 40 Wh, Toleranz
   `wb_min_current_import_tolerance_w` 200 W) zählt, wenn es seine
   Netzschwelle mindestens so schnell erreicht wie das Akkukonto sein
   Kontingent; sonst zählt der Akku und das Netzkonto pausiert. Das Akkukonto
   zählt, sobald der Speicher die Wallbox speist; die Budgetüberziehung bleibt
   sonst Diagnose. Speist der Netzpunkt dagegen ein (mindestens die Toleranz,
   stabil 30 s) und lädt oder ruht der Speicher dabei, ruht das Budgetkonto:
   es fehlt physikalisch nichts, die Kachel bleibt leer. Netz- und Akkukonto
   bleiben vorrangig; fehlen frische Live- oder Batteriedaten, zählt es weiter
   wie bisher, und nach einer Datenlücke muss die Ruhe neu verdient werden.
   Ist die Schwelle
   erreicht, senkt die Kaskade zuerst den Strom, schaltet dann die Phasen
   herunter und stoppt zuletzt (Aktionsgrund `battery_support_threshold`,
   Journal „Akku-Wh-Zähler x/y Wh … → Stop“). Der Phasenabstieg läuft als
   vollständige Sequenz (0 A, Phasenziel, Wiederanlauf auf der Zielphase).
   Ein Abstieg reserviert nur die Wiederanlaufleistung der Zielphase samt
   Reserve; beim Aufstieg bleibt auch die bisherige Last reserviert.
   Beginnt ein angeforderter Kaskadenabstieg binnen 30 s keinen Ausgang,
   folgt nur bei fortbestehendem Defizit ein Stop: Netzbezug über der
   Toleranz oder eine gezählte Akku-Komponente mit erreichter Akkuschwelle.
   Ein PV-Rahmen unter dem
   aktuellen Minimum zählt dafür nur ohne erlaubte Akkustützung: nach dem
   Kontingent unter dem Korridor in `PV-Kurve ruhig` oder bei geschlossenem
   wbminSoC-Tor. Bei erlaubter Stützung ist Netzbezug innerhalb der Toleranz
   kein Stopgrund. Eine reine Budgetüberziehung ohne diesen Stopgrund lässt
   den unbegonnenen Abstieg nach 30 s verfallen. Die Reservierung wird frei,
   das Konto beginnt bei null; erst nach erneutem Kontodurchlauf kann derselbe
   Abstieg wieder angefordert werden. Nach einem Kaskadenabstieg auf eine
   Phase startet die openWB Pro mit dem konfigurierten Mindeststrom der
   jeweiligen Wallbox. Derselbe Strom liegt der gebundenen
   Wiederanlaufreservierung zugrunde. Werte unter dem Geräteminimum werden auf
   das Geräteminimum angehoben, Werte über der Gerätehöchstgrenze begrenzt.
   Aktuelle Stromgrenzen, Budget, Hausanschluss und Reservierung müssen diesen
   Strom vollständig tragen; andernfalls wartet der Wiederanlauf und startet
   nicht mit einem niedrigeren Ersatzwert. Fehlt der Konfigurationswert oder
   ist er ungültig, gilt der bisherige Rückfall auf 6 A. Der Status der
   Phasenreservierung nennt den Wiederanlaufstrom (`restart_amp`), seine
   Quelle (`restart_amp_source`: `configured_min`, `device_min` oder
   `fallback_6a`) und den Grund (`restart_amp_reason`). Nach bestätigtem
   Wiederanlauf folgt die normale Stromrampe. Startfenster und Re-Emit, Stop-
   und Aus-Vetos sowie das Schnellstart-Angebot nach dem Abstecken behalten
   ihre bestehenden Regeln.
   Für eine
   begonnene Sequenz gilt weiterhin die Bestätigungsfrist von standardmäßig
   240 s (`wb_phase_confirm_timeout_s`).
   Ist das Ende der Defizitepisode bestätigt (Einspeisung bei ladendem oder
   ruhendem Speicher, stabil 30 s), auch nach Ablauf der Ausgangsfrist,
   verfällt der noch nicht angelaufene Auftrag, das Konto beginnt neu,
   und eine schon zugesagte Phasenreservierung wird ohne Ausgang freigegeben.
   Schaltet der Nutzer vor dem Phasenziel auf `Aus`, endet die Sequenz
   ebenso ohne weiteren Ausgang; nach der Rückkehr gilt keine alte
   Phasenentscheidung.
   Das Konto ist ausgesetzt, solange
   eine andere Wallbox mit autorisierter Speicherstützung (`PV + Akku bis
   Untergrenze`, `Sofort bis Preislimit`, `Akku bis Abfahrt`, Grundladung oder
   Startreservierung) real lädt, weil Entladung und PV-Rest Gruppengrößen sind.
   Das gilt nur mit frischem, gültigem Status dieser Wallbox: Fehlt ihr Status
   oder ist er veraltet, gilt sie als nicht vorhanden und das Konto zählt
   weiter; fällt der Blocker weg, zählt das Konto ohne Sprung weiter.
   Fehlen Batterie-, PV- oder Leistungsdaten, bleibt das Konto eingefroren; es
   stoppt nie aus geschätzten Größen.
   Nach einem Stop der Kaskade startet die Wallbox in derselben Stecksession
   erst wieder, wenn das batterieneutrale PV-Budget die Mindestleistung der
   erwarteten Phasenzahl (6 A · 230 V · Phasen, also 1p 1380 W und 3p 4140 W)
   ununterbrochen `wb_pv_only_release_hold_s` (Standard 120 s) lang deckt;
   ungültige Live-Daten setzen die Zeit zurück, eine einzelne Wolkenlücke
   startet nicht. Netz-/Preisfenster, Nutzer-`Aus` oder ein ausdrücklicher
   Start heben die Wartezeit auf. Der erste Start nach dem Anstecken bleibt
   unverändert. Solange dieses Wiederanlauftor bewaffnet und blockiert ist,
   legt die Policy für eine stehende Wallbox keine neue Phasenreservierung an.
   Erst nach Freigabe beginnt eine erforderliche Phasenvorbereitung; deren
   Sequenz und Wiedereinschaltverzögerung kommen zur Wartezeit hinzu.
5. **`wbminsoc`.** `wbminsoc` begrenzt ausschließlich die Akkustützung, nie das
   PV-Laden. Liegt der SoC morgens unter `wbminsoc` und es wird eingespeist,
   lädt die Wallbox aus PV-Überschuss; im Korridor wird geladen wie unter
   Punkt 3. Ist `wbminsoc` gesetzt, wirkt es als zusätzliche Untergrenze der
   Stützung (mit Hysterese `wb_soc_hysterese_pct`); ist es aus, gilt allein die
   Kurvenregel. „Aus“ bedeutet nie „unbegrenzt bis zur E3DC-Grenze“. Eine
   Anhebung von `wbminsoc` im laufenden Betrieb sperrt die Stützung sofort
   (`wbminsoc_runtime_raise`).
   In den Modi mit Akkuladen bis zur Untergrenze (`PV + Akku bis
   Untergrenze`, `Sofort bis Preislimit`, `Akku bis Abfahrt`) gilt die
   Korridorregel aus Punkt 3 nur, solange das wbminSoC-Tor geschlossen ist.
   Ist es offen, stützt der Speicher die Wallbox unabhängig von der
   Korridorlage (Grund `wbminsoc_floor_open`); der Speicher lädt dabei weiter
   nach der Ladekurve. Unterhalb von `wbminsoc` verhalten sich diese Modi wie
   `PV-Kurve ruhig`, jedoch ohne das Wolken-Kontingent aus Punkt 3.
   **Haltezone.** Zwischen `wbminsoc` und `wbminsoc` plus dem
   Neustart-Abstand (der größere Wert aus `wb_soc_hysterese_pct` und
   `wb_target_restart_above_wbminsoc_pct`, Standard 2 Prozentpunkte) hält
   der Speicher den SoC, statt zu pumpen – ohne Netz-, Preis-, Boost- oder
   Pre-Dump-Fenster und unabhängig davon, ob das wbminSoC-Tor gerade offen
   oder geschlossen ist. Sie gilt in `PV + Akku bis Untergrenze` und in
   `Sofort bis Preislimit` ohne Preis- oder Netzfenster, nicht in `Akku bis
   Abfahrt` (eigener Stop an der Untergrenze). Die Wallbox bekommt dort nur den batterieneutralen
   PV-Rahmen: PV minus Haus, Wärmepumpe und Heizstab. Der Speicher stützt
   nicht und lädt nicht vorrangig nach der Kurve; er lädt nur, was die
   Wallbox nicht abnimmt (Vertrag `wallbox_wbminsoc_hold_zone` im
   Wallbox-Rahmen). Eine
   vorher gestützte höhere Wallboxleistung wird beim Eintritt nicht gehalten.
   Kurze Schwankungen – eine Lastspitze etwa eines Induktionskochfelds oder
   eine Wolke – fängt der Speicher ab, ohne dass die Wallbox nachregelt:
   War die Wallbox zuvor vom PV-Rahmen gedeckt, bleibt ihre laufende Leistung
   gehalten, solange die dabei aus dem Akku in die Wallbox fließende Energie
   (abzüglich 100 W Messtoleranz) das Wolken-Kontingent
   `wb_curve_floor_support_wh` nicht erreicht; das Kontingent beginnt wieder
   bei null, wenn 60 s lang keine Akkustützung der Wallbox anlag, die
   Haltezone verlassen war oder keine Wallbox lud. Begrenzt die PV-only-
   Entladegrenze aus Punkt 3 den Speicher auf das Haus, gilt nur der
   aktuelle PV-Rahmen. Unter der Haltezone gilt die Kurvenregel, darüber die
   Stützung bis `wbminsoc`. Weil der E3DC den SoC in ganzen Prozent meldet,
   verlässt der Speicher die Haltezone nach oben erst ab Neustart-Abstand
   plus `wb_soc_hysterese_pct` (Standard 2,7 Prozentpunkte über `wbminsoc`);
   dorthin kommt er nur mit PV-Überschuss, den die Wallbox nicht abnimmt.
   Diese Hysterese hängt nur am SoC; ein einzelner Zyklus ohne gültigen
   Wallbox-Intent setzt sie nicht zurück. Der Vertrag nennt das PV-Budget der
   Wallboxgruppe im Band (`budget_w`, gleich `hold_frame_w`); es gilt,
   solange der Vertrag aktiv und höchstens 15 s alt ist. Wallboxwert und
   E3DC-Rest des Rahmens stammen aus demselben Probenpaar („Eine Stellgröße
   je Regelkreis“, Punkt 3).
   Strom, Phasen und Stop der Wallbox entscheidet weiter der Wallbox Manager.
   Er nutzt im Band einschließlich der Untergrenze den veröffentlichten
   Haltezonen-Rahmen als PV-Budget und rechnet dort kein eigenes Budget:
   Sinkt der Rahmen, folgt der Sollstrom direkt; schließt das wbminSoC-Tor
   an der Untergrenze, bleibt der Rahmen das Budget. Trägt der Rahmen die
   Mindestleistung der genutzten Phasenzahl nicht, gilt auch bei offenem Tor
   der Direktabsenk- und Stop-Pfad der Untergrenze (siehe unten). Der
   Akkuwächter an der Untergrenze senkt im Band nicht gegen eine
   Überbrückung aus dem Wolken-Kontingent ab.
   In `PV + Akku bis Untergrenze` – und in `Sofort bis Preislimit`, das ohne
   Preis- oder Netzfenster denselben Regelpfad bis zur Untergrenze nutzt –
   gilt an der erreichten Untergrenze: Das wbminSoC-Tor ist geschlossen, der
   Wallbox-Intent meldet `battery_support_authorized` false mit Grund
   `wbminsoc_floor_closed`. Maßgeblich ist der Modus der Wallbox, die gerade
   geregelt wird – auch wenn eine andere gesteckte Wallbox einen anderen Modus
   hat – und ihr eigenes batterieneutrales PV-Budget (der kleinere Wert aus
   Gruppen-PV und ihrer Zuteilung; in der Haltezone ist das Gruppen-PV der
   Haltezonen-Rahmen). Trägt es die Mindestleistung der aktuell
   genutzten Phasenzahl nicht (1p 1380 W, 3p 4140 W), setzt die Kaskade die
   laufende Wallbox bei dieser Phasenzahl auf den Mindeststrom und lässt das
   vorhandene Wh-Konto genau einmal bis zu seiner Schwelle
   (`wb_min_current_import_stop_wh`, Standard 40 Wh) weiterlaufen – die
   Absenkung setzt es nicht zurück. Danach wechselt eine dreiphasig ladende,
   phasenumschaltbare Wallbox auf 1p, wenn ihr PV-Budget das 1p-Minimum trägt
   und der Regler an ihr die Phasen umschalten kann (openWB Pro); in allen
   anderen Fällen stoppt die Kaskade ohne Phasenwechsel. Bleibt die
   Bestätigung dieses Phasenwechsels aus, stoppt sie ebenfalls.
   Laden mehrere Wallboxen, wird eine Wallbox in dieser Lage ohne Netzbezug
   zuerst geregelt, auch wenn eine andere Wallbox mehr über ihrer Zuteilung
   lädt – sofern sie selbst, gemessen an ihrer tatsächlichen Leistung, mehr
   als das Leck des Kontos (`wb_min_current_import_release_w`, Standard 80 W)
   über ihrer eigenen Zuteilung lädt. Deckt ihre Zuteilung die tatsächliche
   Ladeleistung, etwa beim Mindeststrom, der real oft unter 1380 W liegt,
   lädt sie weiter und wird nicht wegen der Überziehung einer anderen Wallbox
   gestoppt. Mehrere solche Wallboxen werden nacheinander auf den Mindeststrom
   gesetzt, bevor das Konto durchläuft. Besteht Netzbezug, baut die Kaskade
   zuerst diesen ab. Läuft an einer anderen Wallbox gerade ein Phasenwechsel
   oder Stop der Kaskade, wartet die Absenkung dessen Ende ab (höchstens
   `wb_phase_confirm_timeout_s`, Standard 240 s für eine begonnene Sequenz;
   ohne begonnenen Ausgang bei fortbestehendem Defizit höchstens 30 s).
   Die Absenkung geschieht sofort, wenn für diese Wallbox kein PV anliegt
   (nachts oder eigene Zuteilung 0 W) oder das Gruppen-PV-Budget unbekannt
   ist. Liegt noch Gruppen-PV an (Dämmerung, Wolke) – auch wenn die eigene
   Zuteilung fehlt –, muss die Unterdeckung erst
   `wb_floor_pv_only_phase_down_hold_s` (Standard 20 s, dieselbe Haltezeit wie
   für den Phasenabstieg an der Untergrenze) ununterbrochen anstehen; bis
   dahin warten die vom Budgetkonto ausgelösten Strom- und Phasenstufen, das
   Konto zählt weiter. Diese Bestätigung läuft je Wallbox, unabhängig davon,
   welche Wallbox die Kaskade gerade regelt; ein Wechsel startet sie nicht
   neu. Netzbezug regelt die Kaskade dabei wie immer vorrangig
   ab (sofort proportional, bei vollem Netzkonto die nächste Stufe). Ein
   kurzer PV-Einbruch senkt also nicht ab. Der eine
   Kontodurchlauf ist die Bestätigung gegen Flattern; die Energie unter der
   Grenze bleibt damit im Bereich eines Kontodurchlaufs (in einer Gruppe
   zuzüglich der genannten Wartezeiten). Weil das Konto bei
   der Absenkung nicht neu beginnt, kann der Stop nach einer
   Eintrittsbestätigung bei vollem Strom schon kurz nach Erreichen des
   Mindeststroms folgen. Folgt die Wallbox der Absenkung nicht, zählt das
   Konto mit der gemessenen Leistung weiter und die Kaskade stoppt sie an der
   Schwelle, frühestens 30 s nach der Absenkung – auch wenn zwischendurch eine
   andere Wallbox geregelt wird. Trägt das PV-Budget die Mindestleistung,
   bleibt es bei der stufenweisen Kaskade. `Akku bis Abfahrt` behält seinen eigenen Stop an der
   Untergrenze. Nach dem Stop startet die Wallbox in derselben Stecksession
   nur über das Wiederanlauftor nach einem Kaskaden-Stop (Punkt 4) neu. Das
   wbminSoC-Tor gibt die Akkustützung in diesen Modi erst wieder frei, wenn
   der SoC die Untergrenze um den größeren Wert aus `wb_soc_hysterese_pct` und
   `wb_target_restart_above_wbminsoc_pct` (Standard 2 Prozentpunkte)
   übersteigt; das startet die Ladung aber nicht allein.
6. **Ein Entscheider.** Ob und wie viel Stützung erlaubt ist, entscheidet der
   Storage Manager je Zyklus auf Basis des Wallbox-Intents. Der Wallbox-Intent
   meldet die Kurvenklasse der Wallbox (`battery_support_authorized`,
   `battery_support_reason` mit den Werten `curve_above_target`,
   `curve_within_corridor`, `curve_floor_wh_guard`,
   `curve_below_target_pv_only` und `wbminsoc_runtime_raise`, in den Modi mit
   Akkuladen bis zur Untergrenze `wbminsoc_floor_open` und
   `wbminsoc_floor_closed`). Sperrt die Entscheidung des Speichers die
   Stützung, schreibt er Freigabe und Grund im selben Zyklus in den
   Wallbox-Rahmen: `false` mit `curve_below_target_pv_only` (PV-only-Klasse),
   `wbminsoc_hold_zone` (Haltezone, Punkt 5; der Haltezonenvertrag
   `wallbox_wbminsoc_hold_zone` steht zusätzlich im Rahmen) oder
   `wbminsoc_runtime_raise`. Ohne solche Sperre stehen beide Felder nicht im
   Rahmen; der Wallbox Manager nutzt dann seine eigene Kurvenklasse. In den
   Modi mit Akkuladen bis zur Untergrenze wertet die Speicherseite direkt das
   wbminSoC-Tor (`wbminsoc_gate_open` im Wallbox-Intent) aus.
   Der Wallbox Manager fordert keine Stützung an; er
   regelt innerhalb des Rahmens und ist seinerseits alleiniger Entscheider für
   Strom, Phasen und Stop der Wallbox.
7. **Harte Schranken unberührt.** Nutzer-`Aus`, Notstromreserve, Hausanschluss,
   Pre-Dump, Preisfenster und `Sofort bis Preislimit` behalten ihre Regeln.
8. **Anzeige.** Das Dashboard zeigt im Wallbox-Slot das Kontingent als
   „Akku x/y Wh“ (Stützung an der Korridor-Untergrenze) beziehungsweise
   „PV-only x/y Wh“ (Kontingent aufgebraucht, Wallbox nur aus PV-Überschuss).
   Die Kachel erscheint nur, solange die Korridorregel tatsächlich gilt und
   geladen wird; bei ausdrücklich erlaubter Stützung bleibt sie verborgen.
   Während der höchstens 30 s langen Stützung bis zur Wirkung bleibt die
   Akku-Klasse sichtbar, auch wenn das Kontingent bereits verbraucht ist;
   der Zustand des Defizitreglers steht in `wb_deficit_counter` und je Wallbox
   in `group_deficit_battery_support` der Entscheidungsdatei.

### Wallbox-Grundregeln

- Bei klassischer openWB wird die tatsächliche Rolle unterschieden: Secondary
  mit Sollstrom und Heartbeat, Primary-PV mit eigener PV-/Ladepunktlogik oder
  ausdrücklich aktivierter Primary-Direktpfad. Eine Rolle folgt nie aus dem
  Produktnamen oder einem MQTT-Messwert. Direkte Phasenbefehle an eine
  klassische openWB gibt es nicht; Herstellerregler und lokale Grenzen bleiben
  bestehen, und es gibt keine pauschalen Lademodus-Kommandos für alle Rollen.
  Anzeige und Diagnose passen zur tatsächlichen Rolle und Befehlsautorität.
- openWB Pro wird im Standalone-Betrieb ausschließlich über `connect.php`
  gesteuert: Lesen per `GET`, Sollstrom `ampere` (0 oder 6,0–32,0), Phasenziel
  `phasetarget` 1 oder 3, Weckruf `cp_interrupt=true`, Heartbeat `update=1`.
  Regelmäßiges Lesen und Steuern hält den Heartbeat aktiv.
- openWB-Serien- und native MQTT-Wallboxen liefern reale Zählerwerte, wenn
  vorhanden. Langzeitberechnung und Hausverbrauch rechnen mit echten
  Wallbox-Messwerten, nicht mit interpolierten Sollwerten.
- Wiederanlauf, Mindestladezeit und Wolken-Halten folgen dem wirksamen
  Geräteprofil und der jeweiligen Konfiguration; es gibt keine universelle
  Herstellergrenze. Die Sperre für einen weiteren Phasenwechsel ist von der
  CP-Unterbrechung getrennt und beträgt beim gebundenen Vertrag mindestens
  480 s.
- Phasenfähigkeit der Wallbox, erlaubtes Schaltziel und tatsächlich vom
  Fahrzeug genutzte Phasen werden getrennt geführt. Eine Ladepause oder
  fehlende Leistung belegt keinen Phasenverlust. Die begrenzte
  6-A-Phasenerkennung gilt nur im stromgeregelten E3DC-Festphasenpfad; die
  zentrale Quellenfreigabe und die gemeinsamen persistenten Wh-/Zeitwächter
  bleiben erhalten (siehe `doc/Native_Wallbox.md`, „Phasenerkennung an einer
  festen Wallbox“).
- Geplante Ladefenster starten mit der maximal erlaubten Leistung unter
  Beachtung der Hausabsicherung und enden hart mit 0 W.
- `wbminsoc` begrenzt die Unterstützung der Wallbox aus dem Hausakku. Echter
  freier PV-Überschuss darf auch darunter Laden ermöglichen, sofern die
  übrigen Schutzgrenzen eingehalten sind. Daraus folgt weder eine pauschale
  PV-Ladesperre noch ein abgesenktes Speicher-Kurvenziel.
- Wärmepumpe und Wallbox: Die Priorität berücksichtigt die tatsächliche
  Aufnahme und den geschützten Verdichterbetrieb. Eine Startreserve gilt nur
  im reservierten Betrieb; der messwertgeführte PV-Boost reserviert nichts.
  Nicht angenommene freigegebene Leistung kann die Wallbox erhalten.
  Mindestlaufzeiten werden nur durch benannte Schutzfälle verkürzt; ein
  gewöhnlicher Budget- oder Prioritätswechsel genügt nicht. Es gelten die
  gerätespezifischen elektrischen Leistungsprofile, keine erfundenen
  Solltemperaturen.
- Bei Vorrang der Wärmepumpe zählt für den Defizittimer eines bereits
  ausgespielten PV-Boosts auch die aktuell gemessene Leistung nachrangiger
  Wallboxen. Die Bewertung rechnet diese Last einmal aus der gemessenen Netz-
  und Akkubilanz heraus: zuerst wird der Netzbezug entlastet, anschließend
  jede gemessene Akkuentladung bis zur verbleibenden gemessenen
  Wallboxleistung. Ein Defizit liegt erst vor, wenn ohne diese Wallboxlast
  noch Netzbezug oder Akkuentladung über dem bestehenden Toleranzband bliebe.
  Starts behalten ihre bisherige Qualifikation. Nimmt die Wärmepumpe Strom
  auf, verteilt der bestehende Verbraucherregler die Leistung neu und
  begrenzt die nachrangige Wallbox; die Defizitbewertung sendet keinen
  zusätzlichen Wallbox-Befehl. Bestehende Quellen-, Kontingent- und
  Schutzgrenzen gelten weiter. Bei Vorrang der Wallbox bleibt die bisherige
  Bewertung bestehen.
- `Sofort bis Preislimit`: Das Netzpreislimit (`wallbox_price_limit_ct`,
  Rückfall `dvcarlimit`) gilt nur für diesen Modus. Die normale Ladeplanung wird
  nicht durch das globale Preislimit blockiert.

### Pre-Dump-Grundregeln

- Pre-Dump bleibt abschaltbar (`predump_enable`).
- Ein Punktlandungsfenster `pd_max_hours = 0` darf die Ladekurve nicht
  versteckt aushebeln.
- Pre-Dump-Untergrenze (`storage_predump_min_soc`) und Startfenster gehören in
  denselben Bedienblock wie der Pre-Dump-Schalter.
- Deaktivierter Pre-Dump wird weder als aktiver Entladepfad angezeigt noch
  geplant; die Kurve fällt dann nicht zur Pre-Dump-Untergrenze ab.
- Die Pre-Dump-Untergrenze ist nur die Untergrenze der aktiven
  Vorab-Entladung, kein normales Nachtziel.
- Nachts darf der Speicher grundsätzlich bis zur Notstromreserve
  beziehungsweise entlang eines sinnvollen Nachtpfads entladen; morgens wird
  mit PV Richtung Morgenpuffer geladen.

## Speicher und Ladekurve

Die Ladekurve ist eine Soll-SoC-Trajektorie für den Speicher. Sie wird vom
`storage_simulator.py` mit Wetterprognose, Verbrauchsmodell, EPEX/Eco-Score,
saisonalem Nachtverbrauch und optionalem Mittagsziel erstellt.

Aktueller Stand:

- vergangene und aktive Stützpunkte werden eingefroren,
- kommende Punkte werden rollierend geglättet,
- zukünftige Pre-Dump-/Startanker werden nicht auf den aktuellen SoC
  hochgezogen,
- Pre-Dump schafft vor Kurvenstart gezielt Platz gegen Abregelung,
- im Zielkurven-Modus „Prognose auf 100%“ werden 100 Prozent nur dann erst kurz
  vor dem PV-Ende geplant, wenn ein Grund vorliegt (Einspeiselimit,
  Abregeldruck, Direktvermarktung oder Pre-Dump); sonst endet die Kurve am
  letzten nutzbaren Überschuss (Details in `Ladekurve_Berechnung.md`, Freilauf),
- Pre-Dump kann freigegebene Verbraucher über ein gemeinsames Budget nutzen,
- Wärmepumpenleistung wird als eigener Verbraucher geführt und nicht doppelt im
  Hausverbrauch gezählt,
- der Manager führt die Kurve weich über `iFc`, Kontroll-SoC und geglätteten
  Aufholbedarf,
- abends wird nicht mehr zwanghaft auf die Kurve entladen, weil das Haus den
  Speicher ohnehin natürlich nutzt.

Jede Verbraucherleistung zählt in Budget und Bilanz genau einmal. Eine
E3DC-Wallbox wird vom E3DC getrennt vom Hausverbrauch gemessen; externe
Wallboxen stecken im Hausverbrauch. In einer gemischten Messung wird deshalb
nur der externe Anteil aus dem Hauswert entfernt. Die Wärmepumpe mit
E3DC-Leistungsmesser (WP-Typ 6) ist in der aufbereiteten Hausleistung bereits
herausgerechnet und wird am allgemeinen Leistungsdeckel nicht nochmals
abgezogen. Fehlende Einzelmessungen eingebetteter Lasten erzeugen keinen
zusätzlichen Verbrauchsabzug. Die kurze Entprellung eines frisch gemessenen
Leistungsabfalls bleibt erhalten.

Wenn E3DC-Control diese Ladekurve führt, sollte das wetterbasierte Laden im
E3/DC-Hauskraftwerk deaktiviert sein. Die E3/DC-Funktion ist ein eigener
Ladeplaner und kann die Batterieladung trotz einer von E3DC-Control gesetzten
AUTO-Ladeobergrenze zurückhalten. Die Open-Meteo-/Forecast-Prognose von
E3DC-Control arbeitet davon unabhängig weiter. Erkennt E3DC-Control gleichzeitig
die E3/DC-Statussignale `Laden gesperrt` und `Warten auf Sonnenschein`, wird die
Kurvenladung als extern zurückgehalten angezeigt. Die Geräteeinstellung wird
weder automatisch noch zyklisch über RSCP verändert.

## Wallbox-Regelung

Die Wallbox-Regelung arbeitet pro Wallbox mit Modus, Mindest-SoC, Budget und
Hysterese. Die sichtbaren Nutzer-Modi sind bewusst klein gehalten:

| UI-Modus | Bedeutung |
|---|---|
| `Aus` | NGNA: keine aktive E3DC-Control-Ladung und keine laufenden Steuerbefehle. Nur beim bewussten Wechsel auf `Aus` in der WebUI wird einmalig die Wallbox-Grundeinstellung freigegeben. Geplantes Netzladen bleibt gesperrt. |
| `PV-Kurve ruhig` | Laden entlang der Speicher-Ladekurve mit Hysterese; Speicherziel hat Vorrang. |
| `Grundladung stabil` | Wie PV-Kurve ruhig, aber mit stabiler 1p/3p-Grundladung, solange `wbminSoc` laut Planung erreichbar bleibt. |
| `PV + Akku bis Untergrenze` | Das Auto darf PV plus Hausspeicher oberhalb der Hausakku-Untergrenze nutzen; der Speicher lädt dabei weiter nach der Ladekurve. Unterhalb der Grenze lädt das Auto wie in `PV-Kurve ruhig` nur aus PV-Überschuss, der Speicher stützt nur Hausverbrauch und Wärmepumpe, Netz bleibt außen vor. |
| `Sofort bis Preislimit` | Sofortiges Netzladen, solange der aktuelle Preis das Wallbox-Preislimit erfüllt. |
| `Akku bis Abfahrt` | Lädt im Freigabefenster vor der Abfahrtszeit aus PV und Hausspeicher bis zur Hausakku-Untergrenze `wbminsoc`; Netzladen bleibt gesperrt. Gestoppt wird bei erreichter Abfahrtszeit, vollem Fahrzeug oder erreichter Untergrenze (`wb<n>_battery_departure_time`, `wb<n>_battery_departure_window_h`). |

Geplantes Netzladen per Zeitfenster/Slot darf in allen aktiven Modi laden und
ignoriert bewusst das globale Wallbox-Preislimit, weil die Preislogik bereits
bei der Slot-Auswahl steckt.

Wichtige Schutzlogik:

- Schwellen wie `wbminsoc` arbeiten mit Hysterese.
- Grid-Wächter (letzte Schutzstufe gegen anhaltenden Netzbezug): Liegt der
  Netzbezug länger als 45 s über 500 W, senkt der Wächter jede Wallbox, die laut
  Messung tatsächlich lädt und über 6 A steht, auf 6 A ab. Er hebt nie an,
  startet nie und setzt keinen Ladezustand. Gestoppte, pausierte, abgesteckte
  oder gesteckte, aber nicht ladende Wallboxen, Wallboxen im Modus `Aus` und
  Wallboxen, die der Manager gerade stoppt, bleiben außen vor. Ist Netzbezug
  gewollt (Preisoptimierung oder freigegebenes Netzladen), ist der Wächter für
  alle Wallboxen aus. Eine Wallbox, die der Defizitregler im selben Zyklus
  führt, überlässt er diesem; erreicht dessen Absenkung die Wallbox nicht,
  senkt der Wächter sie selbst ab. Weist ein nachgelagertes Gate seinen
  Befehl ab, versucht er es in den folgenden Zyklen erneut, höchstens dreimal
  in Folge; danach beginnt die 45-s-Frist neu. Meldet der Treiber nur noch den
  letzten guten Stand (Status gedrosselt, höchstens 45 s alt), senkt der
  Wächter ab, wenn dieser Stand „lädt“ zeigt; maßgeblich ist dann der eigene
  Sollstrom. Details: `doc/Native_Wallbox.md`, Abschnitt „Netzbezug beim Laden:
  Einschwingfrist, Anhebung und Grid-Wächter“.
- Liegt der Speicher in `PV-Kurve ruhig` unter seinem Zielkorridor und ist das
  Wh-Stützkontingent der Wallbox-Ladung verbraucht (Akkustützung nach Korridorlage,
  PV-only), begrenzt der Speicher-Manager, solange eine Wallbox tatsächlich
  lädt (ein ab 500 W, aus erst nach 45 s gültig unter 300 W; Akkustützung,
  Punkt 3), die Entladung auf Hauslast plus
  Wärmepumpe minus PV zuzüglich der Reserve `wb_curve_pv_only_house_reserve_w`
  (Standard 300 W, Mindestwert 300 W; Config-Editor, Wallbox → „Regelung:
  PV-Überschuss und Hausakku“, Feld „Reserve unter der Kurve“). Die Wallboxen
  erhalten dann nur echten PV-Überschuss; die Reserve fängt Lastsprünge bis zum
  nächsten Heartbeat ab.
- Ob die Wärmepumpenleistung im E3DC-Hauswert steckt, legt
  `storage_home_wp_split` fest (Config-Editor, Wallbox → „Regelung:
  PV-Überschuss und Hausakku“, Auswahl „Wärmepumpe im Hauswert“): `auto`
  (Standard) entscheidet je Messung – die Wärmepumpe gilt als enthalten, wenn
  der rohe Hauswert mindestens max(500 W, 55 % der WP-Leistung) erreicht;
  `include` rechnet die gemeldete WP-Leistung immer aus dem Hauswert heraus
  (Haus = max(0, Hauswert − WP), typisch ohne separaten Wurzelzähler);
  `separate` zählt die getrennt gemessene Wärmepumpe zusätzlich zum Hauswert.
  Die Automatik kann bei kleiner Rohhauslast (Verdichterstopp) springen; wer
  seine Messanordnung kennt, wählt fest `include` oder `separate`. Der
  Validator meldet unbekannte Werte (wirken wie `auto`).
- Einphasiges Laden an einer openWB Pro darf über 20 A hinaus nur nach
  Messung freigegeben werden: `wb_pcc_phase_basis` (Standard
  `e3dc_pm_active_power` = Netzbezug je Phase vom E3DC-Wurzelzähler ÷
  Wechselrichter-Spannung plus gemessener Wallbox-Strom; `off` = fest 20 A),
  `wb_pcc_power_factor_margin` (Reserve auf den Fremdanteil, 0,8–1,0) und
  `grid_pcc_imbalance_max_a` (Schieflast-Wächter, 10–32 A, Standard 20 A ≙
  4,6 kVA) stehen im Config-Editor unter Wallbox → „Hausanschluss & gemeinsame
  Grenzen“ → „Einphasiger Deckel openWB Pro (Messbasis)“. Voraussetzungen: eine einphasige Obergrenze über 20 A in `wb<n>_openwb_pro_1p_max_amp` (leer = 20 A), eine
  ausdrücklich eingetragene Hausabsicherung `grid_max_amps` (der stille
  Standard 35 A zählt nicht), die Zuordnung `wb<n>_grid_phase` und der
  automatische Software-Nachweis dieser Zuordnung beim Laden (Dashboard:
  „Zuordnung bestätigt“); ohne frische Messwerte (10 s) gilt sofort wieder
  20 A. Anhebung +1 A je Regelschritt, Absenkung sofort. Details:
  `doc/Native_Wallbox.md`, Abschnitt „Einphasiger Stromdeckel aus
  Netzphasenmessung“.
- openWB Pro, schneller Ladestart: Nach bestätigtem Abstecken stellt die
  Einstellung „Nach dem Abstecken: Schneller Start (6 A)“ einmalig das konfigurierte Angebot von
  6 A her. Das gilt unabhängig vom PV-Budget, denn eine leere Box zieht
  keine Ladeleistung. Ein bereits passendes Angebot löst keinen Befehl aus;
  fehlt die Bestätigung, bleiben die begrenzten Readback-Wiederholungen aktiv.
  Nutzer-`Aus`, Notstromreserve, Hausanschluss- und Hardwareschutz gelten
  weiterhin. Nach dem Anstecken übernimmt die Regelung eine bereits laufende
  Ladung; Akku-Wh-Kontingent und Defizitkaskade begrenzen sie wie üblich.
  Das kann ohne ausreichendes Budget kurze Ladefenster und zusätzliche
  Schützschaltungen verursachen und ist eine bewusste Komfortwahl.
  Die Alternative „Sicherheitsvariante (0 A)“ lässt die Box ohne positives Angebot stehen:
  Laden beginnt erst bei ausreichendem Budget. Nach einem Managerneustart
  bleiben bis zum ersten gültigen Budget Starts und Anhebungen gesperrt.
  Typisierte Absenkungen, Stops, Schutz-Null und Not-Aus bleiben erlaubt.
  Allein das oben beschriebene Angebot an die frisch bestätigte leere Box
  ist von der reinen Budgetsperre ausgenommen. Erzwungener Stop, Pre-Dump-Halt,
  Laufzeitsperre und Budget-Timeout sperren auch dieses Angebot.
- openWB Pro Startfenster: Nach dem Anstecken bietet der
  Manager 6 A an, übernimmt ein stehendes Angebot der Box und hält das
  Angebot `openwb_pro_start_hold_s` (Standard 180 s) ohne 0 A und ohne
  Anhebung, auch bei kurzen Budget-Einbrüchen, bis das Auto Leistung
  aufnimmt. Eine Absenkung ab 6 A unter das frisch zurückgemeldete
  Box-Angebot setzt er dagegen sofort um, etwa wenn die Box nach einem
  eigenen Neustart mit höherem Strom lädt. Nimmt das Auto nicht an, wird alle `openwb_pro_start_retry_cycle_s`
  (Standard 300 s) ein neuer Zyklus mit höchstens einem Weckimpuls gezählt,
  nach drei Zyklen bleibt das Angebot mit Meldung stehen; „Ladung beendet“ nur
  bei erreichtem Ziel-SoC. Die CP-Karenz `openwb_pro_start_cp_grace_s` liegt nie
  unter 60 s. Einen Phasenabstieg gibt das Fenster erst frei, wenn der
  Hausspeicher die dreiphasige Mindestladung nachweislich nicht mehr stützen
  kann (keine Stützung autorisiert, Haltezone, Notstromreserve,
  PV-only-Klasse oder `wbminsoc` erreicht, wirksame Entladegrenze unter dem
  Bedarf max(0, 3p-Mindestleistung + Haus − PV), Hausanschluss je Phase,
  Bezug über der Speicherreichweite oder Bezug in jeder gültigen Probe über
  die Einschwingfrist hinaus, gezählt nach Liveproben, nicht nach
  Regelzyklen); solange er es kann, bleibt die Ladung
  dreiphasig. Unbekannte Reichweite, Entladegrenze oder unbekannter Bedarf
  geben nicht frei. Ist der Abstieg freigegeben, erfolgt er im Fenster
  tatsächlich: 0 A, Phasenziel 1p und Wiederanlauf mit dem Mindeststrom, ohne
  weiteren Stopp. Editor: Wallbox → „Fahrzeug-Weckruf und Wiederanlauf“ („Pro
  Startfenster (s)“, „Pro Start-Wiederholzyklus (s)“). Details:
  `doc/Native_Wallbox.md`, Abschnitt „Startfenster, Wiederholzyklus und
  Weckimpuls“.
- openWB Pro, Hochschaltung 1p→3p: Die Regelung reserviert beim
  Speicherregler die bisher laufende Leistung beziehungsweise den
  Wiederanlauf mit 6 A je Phase, nicht den einphasigen Strom je Zielphase.
  Solange der Auftrag auf die Freigabe wartet, rechnet die Zuteilung schon
  mit drei Phasen; belegt der frische Status eine einphasige Ladung, behält
  diese ihren Strom im Rahmen des Budgets. Die vorläufige Variante (b)
  bindet bei einer Einzelbox Mindestbudget und Stromumrechnung an diese
  eine Phase, auch im Zyklus eines Abbruchs ohne Ausgang. Bei mehreren
  Wallboxen bleibt die Stromzuteilung zum Schutz des Hausanschlusses
  dreiphasig; die dadurch mögliche Begrenzung auf etwa ein Drittel der
  Leistung ist eine bekannte Einschränkung, kein Regelungsziel.
  Ohne ausreichende Freigabe endet der Auftrag nach 90 s ohne Geräteausgang,
  eine erteilte, aber ungenutzte Freigabe spätestens 90 s nach ihrem Eingang,
  und fordert die Regelung die Hochschaltung zwei Zyklen lang nicht mehr an
  (etwa in einer Ladepause), endet er sofort. Der nächste Versuch folgt
  frühestens nach `wb_phase_retry_block_s` (Standard 900 s); Vorlauf,
  Export-Wh-Konto und 10-min-Fenster werden danach neu verdient. Nach dem
  Wechsel läuft die Box mit 6 A je Phase wieder an; bis der Wiederanlauf
  unter Last bestätigt ist (drei Messungen über 500 W, stabil über 10 s,
  gleich mit wie vielen Phasen das Fahrzeug lädt), begrenzt die Regelung
  jeden Strom auf die freigegebene Reservierung. Danach folgt der Strom
  wieder dem Wattbudget, jedoch bis zum Ende der Reservierung weiter auf
  drei Phasen umgerechnet. Ein zwei- oder einphasig ladendes Fahrzeug
  erreicht deshalb in dieser Zeit höchstens etwa zwei Drittel beziehungsweise
  ein Drittel des Budgets. Diese bekannte Grenze wird hier nicht aufgehoben.
  Details: `doc/Native_Wallbox.md`, Abschnitt „Hochschaltung ausgeben:
  Freigabe, Wartegrenze und Wiederanlauf“.
- Experimentelles 10-min-Fenster 1p→3p (`wb_phase_up_window_enable`,
  Standard aus): Nur mit eingeschalteter Energie-Phasenpolitik an der openWB
  Pro und am E3DC-Direktvertrag. Das Fenster ist ein zusätzlicher Auslöser:
  Trägt der verfügbare Überschuss im 10-Minuten-Mittel das 3p-Minimum plus
  15 % ohne Einbruch in den letzten 2 Minuten, schaltet die Regelung auf drei
  Phasen; für diesen Auslöser entfallen Vorlauf, Export-Wh-Konto und die
  Bedingung „eine Phase ausgereizt“. Der bisherige Weg über Vorlauf oder
  Export-Wh-Konto bleibt daneben wirksam. Eine neue Ladung startet nur mit
  einem Fahrzeugprofil mit drei Phasen direkt dreiphasig. Fehlende Messwerte
  zählen nie als Überschuss. Sperre nach jedem Wechsel, Netzbezugssperre,
  einphasige Fahrzeuge und die Freigabe durch den Speicherregler gelten
  unverändert. Details und Testanleitung: `doc/Native_Wallbox.md`,
  Abschnitt „Experimentelles 10-min-Fenster 1p→3p“.
- openWB Pro mit einphasig hinterlegtem Fahrzeug: Die Phasen der Box werden
  nicht umgeschaltet; Mindestleistung und Budget folgen dem Fahrzeug
  (1,38 kW), der einphasige Stromdeckel den gemessenen aktiven Phasen.
  Details: `doc/Native_Wallbox.md`, Abschnitt „openWB Pro als reiner Aktuator“.
- openWB Pro wird direkt über `connect.php` gesteuert, wenn E3DC-Control Master
  sein soll.
- openWB Software 2.x als `primary` bleibt eigener Energiemanager; E3DC-Control
  darf nur per bewusst aktiviertem Primary-Pfad Modus/Stop/Sofortladen über
  simpleAPI setzen.
- openWB Software 2.x als `secondary` muss in openWB selbst auf
  `Steuerungsmodus: secondary` und `Steuerung über Modbus als secondary: An`
  stehen. Dann setzt E3DC-Control Sollstrom plus Heartbeat.
- Direkte MQTT-Leistungstopics wie `evcc/loadpoints/1/chargePower` werden als
  reale Wallboxleistung angenommen und aus dem reinen Hausverbrauch
  herausgerechnet.

## Wärmepumpen

Im Luxtronik-Messwertbetrieb (`wp_pv_control_mode=measured`) bietet der
Energy Manager den PV-Boost bei qualifiziertem Überschuss an. Die Startkante
benötigt die WP-Startleistung plus das bestehende 500-W-Schaltband. Bei Vorrang
„WP vor Wallbox“ zählt auch der Überschuss, den die Wallbox gerade verbraucht;
ihr Haltebudget erzeugt kein künstliches PV-Defizit. Ein aktiver Wallbox-Start-Halt
sperrt trotzdem neue WP-Starts. Beginnt er erst nach dem Versand, bleibt der
gemessene WP-Verbrauch im Budget sichtbar.

Der PV-Boost reserviert keine Leistung und keine Energie, auch nicht beim
Versand. Die Voraussetzung ist die frische E3DC-Bilanz: Einspeisung abzüglich
des noch nicht erfüllten Kurvenladebedarfs. Bereits gemessene Akkuladung und
Verbraucher werden nicht nochmals abgezogen. Die geplante Akkuladung behält
Vorrang. Beim Halten eines gesendeten Boosts, auch vor dem Verdichterstart, darf der momentane
Kurvenbedarf zurückstehen, wenn P10 (ersatzweise 70 % von P50 oder 70 % der
frischen Punktprognose) bis zum heutigen Ladeende nach Haus und Wallbox den
restlichen Akkubedarf am wirksamen Kurvenziel und die Wärme deckt. Haus und
Wallbox gehen mit P50 oder ihrer gültigen Punktprognose ohne Abschlag ein, der
Wärmebedarf wird nicht zusätzlich von der PV-Deckung abgezogen. Je Abschnitt
zählt höchstens die Leistung, die Akku (laut bestehender Ladegrenze) und
Wärmepumpe zusammen aufnehmen können. Eine noch nicht erneuerte Plandatei gilt
dafür bis höchstens fünf Minuten nach ihrem Slotende; die maximale Planalterung
von 30 Minuten bleibt. Für diese Halteprüfung zählt die gemessene
Akkuladung genau einmal zum verfügbaren PV-Rahmen; eine Entladung bleibt
über die bestehende 200-W-Bandprüfung ausgeschlossen.
Ohne temperaturgebundene Restlaufschätzung gilt für den Boost eine volle
Mindestlaufzeit mit mindestens der konfigurierten Start-/Maximalleistung;
eine höhere Wärmeprognose ersetzt diesen Ansatz, sie wird nicht addiert.
Zusätzlich bleiben 10 % des Gesamtbedarfs, mindestens 0,5 kWh, sowie die
WP-Energie für die Wolkenüberbrückung (höchstens 300 s) als Reserve.
Ein einzelner deckender Messpunkt setzt die Defizitfrist nicht zurück;
dazu sind mindestens 60 s durchgehende Deckung nötig.
Fehlende, veraltete oder unplausible Prognosen lassen den Kurvenvorrang bestehen.
Echter Netzbezug oder Akkuentladung für die WP bleibt ein Rücknahmegrund nach
der Wolkenüberbrückung. Mindestlaufzeit und Schutzzeiten bleiben vorrangig.
Die Ausnahme gibt keinen neuen Start frei und ändert keinen Speicher-Ausgang.
Der Haltegrund `measured_pv_offer_held_forecast_covers_curve` und
`curve_forecast` zeigen Entscheidung und Rechengrößen.
Der Storage Manager zählt die gemessene WP-Aufnahme genau einmal;
ohne Aufnahme bleibt das Budget für andere Verbraucher verfügbar.
`wp_pv_start_wait_s` bleibt im Messwertbetrieb eine Diagnosefrist und beendet
weder das Sollwertangebot noch eine Reservierung. Fehlende oder veraltete
Messwerte behandelt der folgende Abschnitt.

Der gesendete Sollwert bleibt bei ausreichendem Überschuss stehen, auch wenn
der Verdichter nicht startet oder die Zieltemperatur erreicht wird. Die Anlage
entscheidet ihren Verdichterstart selbst. Ein bereits erreichtes Boost-Ziel
öffnet keinen neuen Auftrag. Erst anhaltendes PV-Defizit über
`wp_pv_boost_release_s`, Schutz, Nutzer-Aus, ein Besitzerwechsel oder das Ende
der bestehenden Freigabe beendet den Boost. Mindestlaufzeit und
Wiedereinschaltsperre bleiben wirksam. Der normale WW-Timer ist die
Rückkehrstellung, kein konkurrierender Boost-Besitzer.

Bei `ww_circ_boost=1` übersteuert der Boost den normalen Zirkulationszeitplan
nur während frisch bestätigter Warmwasserbereitung mit laufendem Verdichter.
Die Einschaltung wartet 10 Sekunden auf einen stabilen Nachweis. Bei frisch
bestätigtem Ende gilt sofort wieder der normale Zeitplan. Fehlt lediglich eine
frische Rücklesung, bleibt die bereits eingeschaltete Boost-Zirkulation noch
höchstens 30 Sekunden bestehen. Wiederholte Leselücken verlängern diese Frist
nicht. Ein gehaltener Boost-Sollwert allein startet keine Zirkulation.

### Fehlende Leistungsmessung der Wärmepumpe

Im Messwertbetrieb steckt die tatsächliche WP-Leistung bei fehlender separater
WP-Messung bereits im gemessenen Hausverbrauch und in der E3DC-Bilanz. Es geht
nur die Aufteilung zwischen Haus und Wärmepumpe verloren. Ein alter WP-Wert
oder ein geschätzter Anteil aus dem Hausverbrauch wird nicht zusätzlich vom
Verbraucherbudget abgezogen.

Die Wallbox erhält den Rahmen aus der gemessenen Bilanz. Startfenster,
Hysteresen, Rampen sowie Netz- und Akkuwächter gelten weiterhin. Es gibt keine
Startreservierung für den PV-Boost. Der Storage Manager verteilt Leistung,
der Wallbox Manager entscheidet über Strom, Phasen und Stop.

Ohne gültige WP-Leistungsmessung wird kein neuer Boost gestartet. Ein bereits
gesendeter Boost kann bei frischen, gültigen Netz- und Akkuwerten bestehen
bleiben. Anhaltender Netzbezug oder Akkuentladung oberhalb des bestehenden
200-W-Defizitbands lässt den Defizittimer weiterlaufen. Nach
`wp_pv_boost_release_s` wird die Rücknahme angefordert; die bestehenden
Signalhalte- und Mindestlaufzeitregeln bestimmen weiterhin ihre Ausführung.
Fehlen auch gültige Bilanzdaten, bleibt die bestehende Schutzlogik wirksam.

Dieselbe Prioritätsregel gilt bei fehlender WP-Einzelmessung, solange Netz-
und Akkubilanz sowie die zurückgerechnete Wallboxleistung frisch und gültig
sind. Ein Sollstrom, Startangebot oder Altwert ersetzt keine gemessene
Wallboxleistung. Ist die Wärmepumpe bereits im Hausverbrauch enthalten, wird
für ihre Messlücke keine zusätzliche alte Verbraucherleistung vom Restbudget
abgezogen. Liegt ihre Messung außerhalb des Hausverbrauchs, bleibt die
erforderliche Rückhaltereserve erhalten.

Der letzte gültige WP-Leistungswert wird mit Quelle und ursprünglichem Alter
höchstens 300 Sekunden beziehungsweise bis zur kürzeren Defizitfrist angezeigt.
Er beeinflusst die Budgetverteilung nicht. Ein aus Hausverbrauch, bekanntem
Wallboxanteil und konfigurierter Grundlast gebildeter Näherungswert dient
ebenfalls ausschließlich der Diagnose. Unbekannte Messwerte bleiben `null`
mit Begründung. Reservierter Betrieb und Schutzpfade bleiben unverändert.

Der Luxtronik-Warmwasser-Timer wirkt wie ein eingebauter WP-Timer:
Bei aktiviertem Timer gilt im Zeitfenster `ww_normal`, außerhalb `ww_eco`.
Bei ausgeschaltetem Timer gilt ganztägig `ww_eco` als Voreinstellung.
Dieser Grund-Sollwert bleibt auch bei niedrigem SoC, Notstromreserve,
Nutzer-`Aus` und wirtschaftlichen Sperren erhalten. Geschrieben wird nur bei
Änderung oder abweichender gültiger Rücklesung.

Auch während einer E3DC-Datenlücke folgt der Warmwasser-Grundwert dem
Timerfenster. Voraussetzung ist eine frische, gültige Rücklesung der
Wärmepumpe; ohne sie wird nichts geschrieben, und die Diagnose meldet
`ww_baseline_suppressed_reason=e3dc_live_gap`. Eine in der Lücke geschriebene
Grundstellung zeigt `ww_baseline_written_in_gap=true`. Ferien- und
Frostschutzmodus, Uhrprüfung, Takt- und Schreibschutz sowie Hardware- und
Wärmequellenschutz bleiben wirksam; kommt ein Wert nicht an, gelten die
normalen Wiederholungsabstände, Versuchsgrenzen und Alarme.

Ein PV-Boost setzt seinen höheren Sollwert darüber, solange seine
Voraussetzungen erfüllt sind. Entfällt eine Voraussetzung, auch bei
Nutzer-`Aus` oder erreichter Notstromreserve, wird ein in diesem Boost
nachweislich laufender Verdichter bis `wp_min_runtime_min` gehalten.
Danach gilt sofort der Grund-Sollwert der aktuellen Uhrzeit. Läuft der
Verdichter nicht, fehlt ein gültiger Laufbeleg oder ist die Mindestlaufzeit
bereits erfüllt, erfolgt die Rücknahme sofort. EVU-/Fremdsperren sind kein
Haltegrund. Hardware- und Hausanschlussschutz behalten Vorrang.
Die Wiedereinschaltsperre und die Regelung des Heizkanals bleiben bestehen.

Der an der Wärmepumpe gewählte Ferien-, Urlaubs- oder Frostschutzmodus hat
Vorrang. E3DC-Control startet darin keine neuen Boosts und schreibt keine
Timer- oder Eco-Grundstellung auf Kanäle ohne eigene Wirkung. Ein bereits
eigener Warmwasser-Boost wird auf den letzten gültig gelesenen Sollwert vor
dem Boostversand zurückgenommen. Dieser Wert wird mit dem Startauftrag
gesichert und bleibt auch nach einem Neustart des Energy Managers erhalten.
Ist er unbekannt, wird der gültige Wert `ww_eco` verwendet; fehlt auch dieser,
erfolgt keine Schreibung, und die Diagnose meldet das fehlende Rückkehrziel.
Läuft der Verdichter noch innerhalb von `wp_min_runtime_min`, bleibt der
eigene Boost bis zum Fristende bestehen; fehlende Statusdaten und harte
Schutzbedingungen erlauben keine Verlängerung. Entspricht eine frische
Rücklesung bereits dem Rückkehrziel, entfällt der Schreibbefehl. Nach dem
Verlassen des Modus gilt wieder die Warmwasser-Grundstellung. Die Diagnose
`heatpump_channel_dispatch` nennt `ww_baseline_suppressed_reason=wp_holiday_mode`
sowie `ww_boost_return_target_c` und `ww_boost_return_source`
(`pre_boost_readback`, `ww_eco_fallback` oder `none`).

Luxtronik, IDM, Stiebel/ISG, SG-Ready und Heizstab arbeiten nicht als
Nebenregler am Speicher vorbei. Sie erhalten Budget, Freigabe und
Mindestlaufzeiten aus dem Energy Manager beziehungsweise dem Storage Manager.
Der experimentelle SG-Ready-Ausgang über das Stiebel-ISG
(`stiebel_isg_sg_ready_write`, Standard aus) ist nur ein weiterer Ausgang
derselben Entscheidung: Er schreibt ausschließlich SG-Ready-Eingang 1 bei
Zustandswechseln und nimmt nur den selbst gesetzten Eingang zurück; Einzelheiten
stehen in `doc/Stiebel_Eltron_ISG.md`.

Fehlende E3DC-Livedaten sind keine Freigabe: Während einer Datenlücke startet
kein neuer Wärmepumpenauftrag, und kein Boost-Sollwert wird angehoben. Die feste
Warmwasser-Grundstellung folgt weiter dem Timerfenster (siehe oben). Ein bereits
laufender Luxtronik-Auftrag und ein laufender manueller Boost jeder anderen
Wärmepumpe (iDM, Dimplex, SG-Ready) werden erst nach einer begrenzten Frist
zurückgenommen, damit ein einzelner Aussetzer keinen Verdichter abbricht und
keine Wiedereinschaltsperre auslöst: Nutzeraufträge und der
budgetgebundene Timer-Aufträge anderer Wärmepumpen nach fünf Minuten,
automatische Aufträge aus freigegebenem
Budget nach 45 Sekunden. Der Nutzerauftrag selbst bleibt dabei bestehen.
Für den Luxtronik-WW-Grundwert und die Rücknahme seines PV-Boosts gelten die
oben beschriebenen Timer- und Mindestlaufzeitregeln. Für die übrigen
Aufträge wirken Nutzer-Aus, Hardware- und Quellenschutz sofort. Die unabhängige
Sicherheitsabschaltung (Ladestand unter `min_soc` minus 5 Prozentpunkte oder
mehr als 2.500 W Netzbezug) nimmt auch einen laufenden manuellen Boost jeder
Wärmepumpe sofort zurück; der Auftrag bleibt bestehen. Dasselbe gilt bei iDM,
Dimplex und SG-Ready im Notstrom- oder Inselbetrieb und solange der Speicher
die Notstromreserve hält.

Die Wiedereinschaltsperre (`wp_restart_block_min`) zählt ab dem gemessenen
Verdichterstillstand und gilt für alle Startwege, auch für den manuellen Boost
von iDM, Dimplex und SG-Ready und für den Warmwasser-Timer. An einem
SG-Ready-Kontakt (Stiebel-ISG, Shelly, Dimplex) schreibt der Warmwasser-Timer
nicht selbst; seine Nachfrage startet über den zentralen Startweg und endet
über die zentrale Rücknahme. Bei iDM schreibt er den Warmwasserkanal nur als
alleiniger Besitzer und erst nach Ablauf der Sperre; solange ein Preis-,
Pre-Dump-, PV- oder manueller Auftrag, eine Pause oder Warmwasser sofort
läuft, gehört der Aktor diesem Auftrag. „Warmwasser sofort“ startet an
SG-Ready-Kontakten und bei iDM ohne PV-Überschuss und unabhängig von der
Budgethöhe über den zentralen Startweg und endet über die zentrale Rücknahme,
frühestens nach dem Signalhalt von 10 Minuten. Wie der PV- und Preis-Boost
braucht der Start ein frisches Budget des Storage Managers; fehlt es, wartet
der Befehl (`storage_budget_stale`). Es gelten die Wiedereinschaltsperre und,
wie beim manuellen Boost, nach jeder Rücknahme mindestens zehn Minuten
beziehungsweise `wp_restart_block_min` bis zum nächsten Start. In einer
E3DC-Datenlücke und wenn das Speicher-Budget nicht mehr frisch ist, hält
der Manager das Signal höchstens fünf Minuten; im Notstrom- oder Inselbetrieb
und unter der Notstromreserve startet und hält der Knopf nicht. Eine fremde
Sperre (von außen ausgeschalteter Pause-Kontakt, Dimplex „Rot“) überschreibt
er nicht. Nutzer-Aus, Hardware- und Quellenschutz und das erreichte Ziel
beenden den Befehl. Bei SG Ready bewirkt der Knopf den verstärkten Betrieb der
ganzen Anlage; das Warmwasser regelt der Wärmepumpenregler selbst. Nach einem
Neustart zählt die Wiedereinschaltsperre, bis wieder ein
Stillstand gemessen ist, ab dem spätesten bekannten Zeitpunkt aus Rücknahme und
gespeichertem Stillstand. Zusätzlich sperrt jede schutzbedingte Rücknahme ab
ihrem Zeitpunkt mindestens zehn Minuten beziehungsweise
`wp_restart_block_min`, bei der Luxtronik jede Rücknahme den betroffenen Kanal;
maßgeblich ist das spätere Ende. So flattert kein Kontakt, wenn eine
Schutzbedingung kurz auftritt und wieder verschwindet. Einzelheiten stehen in
`doc/Luxtronik.md`.

Der Storage Manager kann die echte elektrische Wärmepumpenleistung aus
`energy_decision_latest.json` übernehmen. Diese Leistung wird als `WP_Power`
geführt und aus dem Hausverbrauch bereinigt, wenn der Zähler sie dort bereits
enthält. Ob das der Fall ist, steuert `storage_home_wp_split` (siehe
Wallbox-Regelung, Schutzlogik).

### Tariffenster und Negativpreis-Netzboost

Tariffenster-Heizen ist eine eigene, standardmäßig ausgeschaltete Luxtronik-
Funktion. Im Schattenbetrieb entsteht nur Diagnose. Aktiv darf sie bei belegtem
Wärmerestbedarf und frischer Speicherfreigabe günstige Octopus-Heat-Zeiten nutzen.
Fehlt `heat_tariff_shift_windows`, gelten 02:00–06:00 und 12:00–16:00 als
Laufzeitstandard. Ausdrücklich leere oder ungültige Fenster sperren. Frühere
Negativpreisfenster werden nicht übernommen. Die normalen Komforttimer bleiben
Grundstellung; hohe Preise sperren sie nicht.

Das Wärmeziel `heat_price_boost_scope` gilt für Tarif- und Negativpreis-Boost:
nur WW, nur Heizung oder beides. Ungültige Werte sperren beide Pfade.
Im Tariffenster gilt WW-Vorrang. `price_min_duration` ist die Mindest-Angebotszeit
für neue Starts im verbleibenden Fenster. Ein laufender Boost endet dadurch
nicht vorzeitig. `price_max_daily` begrenzt weiterhin die Tages-Auftragszeit.

Der Negativpreis-Netzboost arbeitet ausschließlich mit aktueller expliziter
Negativpreisfreigabe. Er hat keine konfigurierbaren Zeitfenster und ignoriert
`heat_price_boost_windows`, erhält aber dessen gespeicherten Wert. Im Sommer
bietet er nur WW an, sofern das Wärmeziel WW erlaubt. Im Winter bietet er
WW und/oder Heizung im Rahmen des Wärmeziels an. Mindest-Angebotszeit und Tarif-
Tagesmaximum wirken hier nicht und bleiben beim Tarifwechsel gespeichert.

Für beide Funktionen startet HZ nur unter der mittleren Außentemperaturgrenze
`heat_grid_boost_max_outdoor_c` (Standard 10 °C, leer ohne Grenze). Ein laufendes
HZ-Angebot bleibt bis 1 K darüber freigegeben, um Flattern zu verhindern. Die
bestehende Heizgrenze bleibt zusätzlich wirksam. Fehlende oder ungültige
Mitteltemperatur sperrt HZ; Warmwasser bleibt ausgenommen. Der Installer fügt
weder diese Grenze noch die Fenster in bestehende Konfigurationen ein.

Der Akku-Halt nutzt ausschließlich frisch gemessene WP-Leistung im vorhandenen
AUTO-Vertrag. Die übrige Hauslast darf der Akku weiter versorgen, auch wenn die
WP 0 W misst. Verbraucher werden in Bilanz und Budget genau einmal gezählt.
Nutzer-Aus, Notstromreserve, Quellen- und Hausanschlussgrenzen bleiben vorrangig.
Der lesende Tagesexport `heat_tariff_diagnostics.php` liefert ausschließlich
freigegebene Diagnosefelder. Details stehen in [Luxtronik](Luxtronik.md).


## Preislogik

Der netzdienliche Eco-Modus und der Negativpreis-/Preis-Boost sind Opt-in-Pfade.
Unbekannte externe Dauerlasten werden nicht geraten. Wenn ein BEV, eine
Wärmepumpe oder ein anderer großer Verbraucher regelrelevant sein soll, muss
seine Leistung eingebunden oder geplant sein.

## Dokumentationsregel

Wenn eine Doku von Konfiguration spricht, ist damit diese zentrale Datei gemeint:

```text
data/e3dc_v4.json
```

Nur Migrations-, Rollback- und Legacy-Abschnitte sollen `e3dc.config.txt` als
aktive Datei nennen.

Die Außentemperaturgrenze betrifft ausschließlich HZ-Boost-Angebote.
PV-, Preis- und Hochpreis-Pausen mit abgesenktem Sollwert bleiben davon unberührt.
