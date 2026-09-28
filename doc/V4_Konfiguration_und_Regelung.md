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
  über `EMS_REQ_SET_POWER_SETTINGS`; dass der E3DC diese Einstellung nur flüchtig
  hält, ist nicht belegt. Die Regelung schreibt sie deshalb nur bei echter
  Änderung, nicht schützende Änderungen höchstens alle 30 s, und bestätigt jeden
  Schreibvorgang per Rücklesen. Einstellungen, die der E3DC nachweislich dauerhaft
  speichert, schreibt die Regelung nie zyklisch.
- Diagnose-Reihenfolge bei unerklärlichem Verhalten: zuerst Messwertqualität,
  physikalische Glitches, Netzwerk-/API-Latenz, veraltete Daten, Phasenmessung
  und Hardwaregrenzen prüfen, bevor Einstellungen oder Regeln geändert werden.
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
  2. **Speicher – Entladegrenze.** Sie ist keine nachgeführte Stellgröße,
     sondern legt fest, wen der Speicher versorgt, etwa Hausverbrauch und
     Wärmepumpe, aber nicht das Auto. Darf der Speicher keinen Verbraucher
     versorgen, ist sie 0, und alle Verbraucher beziehen aus dem Netz. Läuft
     eine Wallbox-Ladung bereits, darf der Speicher sie auch unter dem
     Kurvenkorridor im Rahmen des Wh-Kontingents an der Untergrenze stützen,
     damit kurze PV-Lücken nicht zu Schützschalten führen; ist es aufgebraucht,
     übernimmt die reguläre Halte-, Phasen- und Stop-Politik.
  3. **Verbraucher.** Was nach dem Laderahmen an PV übrig bleibt, geht als
     Budget an die Verbraucher. Jeder Verbraucher regelt nur seine eigene
     Leistung am Budget, die Wallbox zum Beispiel ihren Ladestrom.
  4. **`PV + Akku bis Untergrenze`.** Die Stellgröße ist der Ladestrom der
     Wallbox. Der Speicher lädt weiter nach der Ladekurve mit `iFc` als
     Laderahmen, der PV-Rest geht als Budget an die Wallbox. Der Modus schaltet
     den Speicher nicht in den freien Automatikbetrieb, damit auch ein großer
     Speicher nicht vorzeitig voll ist. Oberhalb von `wbminsoc` darf der
     Speicher die Wallbox bei Akku-Bezug unabhängig von der Kurvenlage bis
     `wbminsoc` stützen. Unterhalb von `wbminsoc` verhält sich der Modus wie
     `PV-Kurve ruhig`; an der Untergrenze senkt beziehungsweise stoppt der
     Wallbox Manager die Ladung.

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
     (`wallbox_curve_pv_only_budget_w`). Der Storage Manager begrenzt die
     Entladung auf Hausverbrauch plus Wärmepumpe minus PV zuzüglich der Reserve
     `wb_curve_pv_only_house_reserve_w` (Standard und Mindestwert 300 W), damit
     der Automatikbetrieb des E3DC die Wallbox nicht aus dem Speicher speist.
     Der Laderahmen des Speichers folgt dabei der Kurvenführung über `iFc`.
   - Wh-Kontingent an der Untergrenze: Unter dem Korridor darf der Speicher die
     Wallbox nur mit einem kleinen Energiekontingent stützen, um Wolkenlücken am
     Mindeststrom zu überbrücken. Das Kontingent ist `wb_curve_floor_support_wh`
     (0 oder leer = automatisch 0,5 % der Speicherkapazität `speichergroesse`,
     mindestens 50 Wh; ein Handwert gilt absolut, nie unter 50 Wh). Ist es
     aufgebraucht, gilt PV-only (Klasse `pv_only`), und die reguläre
     Halte-, Phasen- und Stop-Politik übernimmt. Eine laufende Wallbox, deren
     Zuteilung unter den Mindeststrom fällt, wird in dieser Klasse sofort auf
     den Mindeststrom gesetzt statt am Iststrom gehalten; der Stop bleibt an
     den Wh-Wächter gebunden. Das Kontingent beginnt erst wieder bei null, wenn
     der Speicher 60 s lang nicht mehr unter dem Korridor lag oder keine
     Wallbox lädt.
4. **Akku-Wh-Konto im laufenden Mindesthalt.** Der gemeinsame
   Defizitregler, der eine laufende Wallbox bis zum Mindeststrom reduziert und
   dort hält, führt für die marginale Wallbox in `PV-Kurve ruhig` ein drittes
   Energiekonto. Es zählt den Anteil der Speicherentladung, der tatsächlich in
   die Wallbox fließt: min(Entladung, Wallbox-Ist − PV-Rest) abzüglich 100 W
   Toleranz, mit dem Leck `wb_min_current_import_release_w` (Standard 80 W).
   Schwelle ist dasselbe Kontingent wie unter Punkt 3; die Korridorlage spielt
   für dieses Konto keine Rolle. Es gilt nur ohne Netz-, Preis-, Pre-Dump-,
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
   Journal „Akku-Wh-Zähler x/y Wh … → Stop“). Das Konto ist ausgesetzt, solange
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
   unverändert.
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
   `PV-Kurve ruhig`.
   In `PV + Akku bis Untergrenze` – und in `Sofort bis Preislimit`, das ohne
   Preis- oder Netzfenster denselben Regelpfad bis zur Untergrenze nutzt –
   gilt an der erreichten Untergrenze: Das wbminSoC-Tor ist geschlossen, der
   Wallbox-Intent meldet `battery_support_authorized` false mit Grund
   `wbminsoc_floor_closed`. Maßgeblich ist der Modus der Wallbox, die gerade
   geregelt wird – auch wenn eine andere gesteckte Wallbox einen anderen Modus
   hat – und ihr eigenes batterieneutrales PV-Budget (der kleinere Wert aus
   Gruppen-PV und ihrer Zuteilung). Trägt es die Mindestleistung der aktuell
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
   `wb_phase_confirm_timeout_s`, Standard 240 s).
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
   Storage Manager je Zyklus auf Basis des Wallbox-Intents und schreibt es in
   den Wallbox-Rahmen (`battery_support_authorized`, `battery_support_reason`
   mit den Werten `curve_above_target`, `curve_within_corridor`,
   `curve_floor_wh_guard`, `curve_below_target_pv_only` und
   `wbminsoc_runtime_raise`). Die Gründe `wbminsoc_floor_closed` und
   `wbminsoc_floor_open` meldet dagegen nur der Wallbox-Intent (Punkt 5); in den Modi mit Akkuladen bis
   zur Untergrenze wertet die Speicherseite direkt das wbminSoC-Tor
   (`wbminsoc_gate_open` im Wallbox-Intent) aus.
   Der Wallbox Manager fordert keine Stützung an; er
   regelt innerhalb des Rahmens und ist seinerseits alleiniger Entscheider für
   Strom, Phasen und Stop der Wallbox.
7. **Harte Schranken unberührt.** Nutzer-`Aus`, Notstromreserve, Hausanschluss,
   Pre-Dump, Preisfenster und `Sofort bis Preislimit` behalten ihre Regeln.
8. **Anzeige.** Das Dashboard zeigt im Wallbox-Slot das Kontingent als
   „Akku x/y Wh“ (Stützung an der Korridor-Untergrenze) beziehungsweise
   „PV-only x/y Wh“ (Kontingent aufgebraucht, Wallbox nur aus PV-Überschuss);
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
  Aufnahme, die Startreserve und den geschützten Verdichterbetrieb. Nicht
  angenommene freigegebene Leistung kann die Wallbox erhalten.
  Mindestlaufzeiten werden nur durch benannte Schutzfälle verkürzt; ein
  gewöhnlicher Budget- oder Prioritätswechsel genügt nicht. Es gelten die
  gerätespezifischen elektrischen Leistungsprofile, keine erfundenen
  Solltemperaturen.
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
- Liegt der Speicher in `PV-Kurve ruhig` unter seinem Zielkorridor und ist das
  Wh-Stützkontingent der Wallbox-Ladung verbraucht (Akkustützung nach Korridorlage,
  PV-only), begrenzt der Speicher-Manager die Entladung auf Hauslast plus
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
  unter 60 s. Editor: Wallbox → „Fahrzeug-Weckruf und Wiederanlauf“ („Pro
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

Luxtronik, IDM, Stiebel/ISG, SG-Ready und Heizstab arbeiten nicht als
Nebenregler am Speicher vorbei. Sie erhalten Budget, Freigabe und
Mindestlaufzeiten aus dem Energy Manager beziehungsweise dem Storage Manager.

Der Storage Manager kann die echte elektrische Wärmepumpenleistung aus
`energy_decision_latest.json` übernehmen. Diese Leistung wird als `WP_Power`
geführt und aus dem Hausverbrauch bereinigt, wenn der Zähler sie dort bereits
enthält. Ob das der Fall ist, steuert `storage_home_wp_split` (siehe
Wallbox-Regelung, Schutzlogik).

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
