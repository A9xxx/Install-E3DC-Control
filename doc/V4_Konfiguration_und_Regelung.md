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

E3DC-Control regelt auf die Messwerte des E3DC. Messgrößen werden nicht gegeneinander geprüft; eine Abweichung zwischen Netzwert und Phasenwerten erscheint erst nach 5 Minuten durchgehender Abweichung als Hinweis.

Geregelt wird so, wie es eingestellt ist; Einstellungen haben Vorrang in der
Regelung, Schutzfunktionen gehen vor. Die Verbraucherpriorität
(`consumer_priority_order`) gilt auch beim Start: Ein frisch qualifizierter
Start eines vorrangigen Verbrauchers erhält seinen Anteil vor rücknehmbaren
nachrangigen Lasten. Während der Startqualifizierung bleibt deren Budget
erhalten. Erst die verbindliche Startzuteilung senkt ihren Anteil. Die Wallbox
setzt ihren verbleibenden Rahmen mit Stromsenkung, Mindesthalt, Phasenwechsel
oder Stop und ihrem vorhandenen Wiederanlaufschutz um. Die Istleistung bleibt
während dieses Übergangs in der Bilanz enthalten. Die Akkuaufteilung bei
„Kurve zuerst“ und die Schutzzeiten der Geräte gelten weiterhin.

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
     laufende Ladung. Der Regelkern steigt nach 60 Sekunden ausreichender
     Deckung des dreiphasigen Mindeststroms durch den zugeteilten Leistungsanteil
     auf und nach 60 Sekunden geeigneter Unterdeckung ab. Fahrzeug, Wallbox, Hausanschluss
     und Phasenfreigabe müssen den Wechsel erlauben; nach jedem Wechsel gilt
     die feste Phasensperre von 480 Sekunden.
     Auch ein 16-A-Ladepunkt kann bei ausreichendem gemeinsam zugeteiltem Anteil auf drei Phasen wechseln; maßgeblich ist die Deckung des dreiphasigen Mindeststroms.
   - *Anti-Flattern.* Hysterese, Deadband, Zeitbedingungen und Energiewächter
     werden gemeinsam aus Messrauschen, realen Leistungsstufen und dem Verhalten
     der Aktorik ausgelegt. Jede Kante bekommt ihre eigene, passende Schwelle
     (etwa die 60 Sekunden Deckung für die Hochschaltung 1p→3p); es gibt keine
     pauschale Leistungsdifferenz für alle Ein- und Ausschaltschwellen.

### Ladeprofil und bekannte Preise

Preisbasiertes Netzladen plant mit den bekannten Preisen. Stunden ohne Preis
sperren keinen Auftrag und begründen keinen zusätzlichen Energiebedarf. Neue
Preise führen zur normalen Neuplanung. Der vollständige Preis des laufenden
Slots, frische Quellen, der erforderliche Energiehorizont und der lückenlose
Preisvergleich für die gewählte Aktion bleiben Voraussetzung. Interne
Preislücken werden nicht durch angenommene Preise geschlossen.

Das Ladeprofil bestimmt die Menge: Wirtschaftlich berücksichtigt Bedarf,
PV-Prognose und Mindestmarge, Ausgeglichen den Bedarf ohne Margenaufschlag.
Komfort lädt im günstigen Preisfenster bis zum eingestellten Ziel, auch wenn
am Folgetag viel PV erwartet wird. Eigene Einstellungen verwenden die
gespeicherten Profilwerte. Haus und enthaltene Verbraucher zählen genau einmal.
Der Hausanschluss geht vor; im gemeinsamen Rahmen erhält der Hausspeicher
seine Ladeleistung vor den Fahrzeugen.

Die Linie „SoC-Prognose mit Marktplan“ stellt ein nachgewiesenes späteres
Nachladen mit der gesamten Akkuladeleistung einschließlich PV und dem Ziel dar.
Die veröffentlichte Netzenergiemenge ist kein laufender Energiezähler. Im
Ladefenster kommt der Hausbedarf aus dem Netz, der Akku wird nicht entladen.
Die Legende kennzeichnet diese Fenster als „Nachladen bei günstigem Preis“.
Ein neuer Plan beginnt beim dann gemessenen SoC.

### Regelungstechnik und Hardware-Schutz

- Reale Relais, Schütze, Wechselrichter und Wallboxen dürfen nicht flattern.
  Jede Schwelle hat eine passende Hysterese, ein Deadband, eine Mindestlaufzeit,
  eine Wiedereinschaltverzögerung oder eine Entprellung. Beispiele:
  `wb_soc_hysterese_pct` an `wbminsoc`, die Beharrungszeiten des
  Phasenwechsels mit je 60 Sekunden für Auf- und Abstieg, 60 Sekunden
  PV-Startqualifizierung, drei Minuten Unterdeckung bis zum Stopp und fünf Minuten Mindestladezeit. Bei freigegebener Netzquelle
  entfällt die PV-Startqualifizierung; die harten Schranken bleiben wirksam.
- Zustandsautomaten haben saubere Übergänge. `Aus` heißt `Aus`: Deaktivierte
  Systeme werden beobachtet, aber nicht aktiv angesteuert.
- Fehlende oder unplausible Messwerte werden nie als echte `0` geschrieben,
  wenn eine `0` später als Messwert gelesen werden könnte. Stattdessen `null`
  plus Gültigkeit und Quelle (`*_valid`, `*_source`). Fehlende Daten sind keine
  Freigabe: Ohne frische, gültige Live-Werte bleiben Budgets geschlossen,
  Konten eingefroren und Aktoren im sicheren Zustand.
- Notstromschutz: An der Notstromreserve wird die Entladung sofort gesperrt. Ab 5.5.6 entfällt nur die zusätzliche EMS-Entladegrenze, wenn das Reservefeld leer oder 0 ist, die Gerätereserve frisch und gültig gelesen wird und sie den wirksamen Boden bildet: Der E3DC hält die Reserve selbst; eigene Entlade-, Verkaufs- und Exportbefehle bleiben bis zur bestehenden SoC-Erholung gesperrt. Leer bedeutet weiterhin 8 % Rückfallreserve; liegt diese über der Gerätereserve, bleibt die 0-W-Grenze aktiv. Ein gespeicherter Wert, auch 8,0, zählt als eigene Reserve. Für den Freilauf das Feld leeren oder 0 eintragen. Nach ungültigen oder veralteten Daten bleibt die 0-W-Grenze bis Reserve + 1,5 Prozentpunkte und 30 Sekunden stabilen Messwerten bestehen. Die Reserve sperrt nur die Akkuentladung: Laden aus PV und vom Speicherplan angeforderte Netzladungen bleiben möglich, Wallbox und Wärmepumpe werden weiter aus dem Netz versorgt, der Hausanschluss je Phase geht vor. Die Unwetter-Vorladung und das Komfort-Netzladen in günstigen Preisfenstern starten an der Reserve nicht.
  Ein Export- oder Verkaufsbefehl (`FORCE_EXPORT`) endet an dieser Grenze
  beziehungsweise am Reserveboden der Direktvermarktung; der Speicher fällt in
  den normalen Betrieb zurück, unabhängig davon, was Preis- oder
  Direktvermarktungslogik verlangt.
- **Freiwillige Notstrom-Vorhaltung (experimentell, Standard aus).**
  `storage_backup_hold_enable` hat die Stufen 0 (Aus), 1 (Beobachten) und
  2 (Halten). Bisherige boolesche Werte bleiben Aus beziehungsweise Beobachten.
  Die Eingabe ist Speicherinhalt in kWh zur einstellbaren abendlichen Bezugszeit.
  „Versorgung mindestens bis“ kalibriert eine konstante rechnerische Ersatzlast;
  eine frühere bestätigte PV-Übernahme verkürzt die Aufgabe. Verluste,
  AC-Reichweite und Notstromfähigkeit werden nicht geprüft oder garantiert.
  Der Boden nimmt nachts mit der Restzeit ab und berücksichtigt tagsüber den
  Wiederaufbau des nächsten Abendziels. Eine chronologische Bilanz enthält auch
  spätere Wolkenlücken, Ladeleistung und Speichersättigung. Fehlt eine frische
  vollständige Prognose oder der Plan, bleibt die volle konfigurierte Energie,
  mindestens die Gerätereserve, auch über die eingestellte Uhrzeit hinaus stehen.
  Ist ein externer AC-Wechselrichter bekannt (über „Externer AC-WR“ in der Kopplungsauswahl der Flächen, den Kasten „PV-Flächen und Wechselrichtergruppen“, „Externe AC-WR-Leistung (W)“, AC-Speicherroute, Shelly-IP oder zentrale Steuerung des Zusatz-WR eingetragen), hält die Notstrom-Vorhaltung die volle Energie, bis alle Flächen eingetragen sind. Ohne bekannten externen AC-Wechselrichter gilt eine offene Fläche
  wie eine E3DC-Fläche; die Vorhaltung nutzt die Gesamtprognose wie bei einer E3DC-Anlage.
  Bei „Speicherladung auf E3DC-DC begrenzen“ oder aktivem Sicherheitsveto
  „ausschließlich E3DC-DC-PV“ der Direktvermarktung bleibt ohne gebundene Aufteilung
  die volle Energie stehen.
  Ist ein externer AC-Wechselrichter nicht eingetragen und läuft er im Inselbetrieb nicht,
  kann die Gesamtprognose die morgendliche Deckung rechnerisch zu früh anzeigen.
  Freigabe nach prognostizierter Übernahme verlangt zusätzlich 120 s lückenlose
  gemessene PV-Deckung des Hauses. Ein Wh-Puffer aus 1 % Kapazitätsauflösung
  und 35 s maximaler Entladung sowie ein eigener Wh-Abstand beruhigen Übergänge.
  Diese Reaktionszeit ist eine konservative Regelannahme, kein Feldnachweis.
  Der bestehende Speicherregler begrenzt flüchtig die Entladung und erhält den
  bereits zulässigen PV-Laderahmen. Bei veralteten Livedaten zieht die freiwillige
  Notstrom-Vorhaltung ihre eigene Entladegrenze zurück. Die Notstromreserve hält
  der E3DC weiterhin selbst ein. Verbraucher und wirtschaftlicher Verkauf,
  Export, Headroom und Pre-Dump nutzen nur Energie oberhalb der wirksamen Böden.
  Hausverbrauch und darin enthaltene Verbraucher zählen weiterhin genau einmal.
  Harte Anschlussgrenzen haben Vorrang; gewöhnliche Netzbezugsvermeidung öffnet
  den Boden nicht. Manuelle Batterieaufträge setzen die freiwillige Vorhaltung
  aus, die Gerätereserve bleibt. Inselbetrieb, globales Aus, externe Quellenbindung
  und fehlende Masterautorität behalten Vorrang. Beim Eintritt in den Inselbetrieb
  nimmt der gemeinsame Speicherausgang genau einmal einen zuvor eigenen
  Halteanteil zurück; unabhängige Leistungsgrenzen bleiben bestehen. Ohne eigenen
  Halt sowie in allen folgenden Inselzyklen erfolgen keine Schreibbefehle.
  Bei ungültigem oder mindestens
  30 s altem SoC entfallen ausschließlich Halten und Reserve-Netzladen;
  unabhängige Schutzsperren bleiben erhalten. Autonome Rückgabe wird erst nach
  bestätigtem Readback als erfolgt angezeigt. Ein Neustart öffnet kein Restkonto.
  `storage_backup_grid_charge_enable` ist ein eigener Opt-in, Standard aus.
  Nur bei Stufe 2 darf der bestehende Preis-Ladepfad das verbleibende Defizit
  bis zur Bezugszeit ergänzen. Alle Tarifmodelle verwenden dieselbe Tarifachse,
  feste Tarife eingeschlossen. Der vollständige Bezugspreis muss streng unter
  `storage_backup_grid_price_limit_ct` liegen; bei Gleichpreis gilt die frühere
  Zeit. Nur für diesen Reservegrund entfallen Arbitrage-, Low-SoC- und
  5-%-Mindestmengenbedingungen. Fehlende Preise erlauben keine Netzladung.
  Dieselbe Ladung zählt für normale und Reserveaufträge einmal; Netzladung
  erzeugt kein PV-Wiederaufladekontingent. Quellenverbote werden nicht übergangen.
  **Kosten:** Halten bedeutet nachts mehr Netzbezug; Reserve-Netzladen kostet
  den Bezugspreis und verspricht keine Arbitrageersparnis.
  Anzeige und Diagnose trennen Sollboden, angeforderte Entladegrenze,
  bestätigten Readback und gemessene Leistung. Unterdeckung heißt Aufbau/Defizit.
  Eine SET-Antwort beweist keine physische Nichtunterschreitung.
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
  Lade- und Entladegrenzen werden gegen gültige Gerätemaxima geprüft. Zu hohe,
  leere oder unplausible Eingaben verwenden das Gerätemaximum mit Hinweis;
  bewusste Drosselungen ab 300 W bleiben erhalten. Herstellerempfehlungen für
  Mindestleistungen sind beratend. Bei unbekannter Gerätegrenze bleibt der
  bisherige Rückfall wirksam. Ein abgelehntes oder unbestätigtes unverändertes
  Ziel wird frühestens nach 30 s erneut gesendet; weitere Fehlschläge verlängern
  den Abstand bis auf 5 min. Frische Istwerte heben diese Sperre nicht auf.
  Andere Ziele können sofort folgen. Der Schutzwechsel „Entladen 0 W“ bleibt
  sofort wirksam; seine eigenen Fehlschläge werden weiterhin nach 10 s versucht.
- Diagnose-Reihenfolge bei unerklärlichem Verhalten: zuerst Messwertqualität,
  physikalische Glitches, Netzwerk-/API-Latenz, veraltete Daten, Phasenmessung
  und Hardwaregrenzen prüfen, bevor Einstellungen oder Regeln geändert werden.
- **Reaktionszeit des Speichers.** Der Kern setzt den Budgetanteil direkt
  mit 1-A-Totband um. Die Netzbremse zählt zwei Takte über 300 W; solange ein
  eigener Ausgang oder Start, Anhebung oder Phasenwechsel einer Netzbox noch
  nicht quittiert ist, zählt der Takt nicht mit. Nur frische gemessene
  Netzboxleistung wird einmal vom Netzpunkt abgezogen. Nach einer Korrektur
  braucht eine Anhebung 30 s unter 300 W. Elektrische Grenzen und Nutzer-Aus wirken sofort.
- **Ungültige Liveprobe.** Eine einzelne ungültige Probe stoppt keine laufende
  Ladung: Bei allen geregelten Wallbox-Typen bleibt der zuletzt ausgeführte
  Strom höchstens 10 s nach der letzten gültigen Probe (ihrem Zeitstempel)
  stehen, ohne Anheben, Neustart oder Phasenwechsel; je Wallbox dauern alle
  solchen Halte in 60 s zusammen höchstens 10 s. Voraussetzung ist der letzte
  gültige Zyklus: frischer Status der ladenden Wallbox, gültiges Budget für den
  laufenden Mindeststrom und kein Netzbezug über der Toleranz; ein als gültig
  markierter Netzwert mit Bezug in der ungültigen Probe verhindert den Halt
  ebenfalls. Sonst gilt das bisherige Verhalten. Nutzer-`Aus`, Pause, Sperre,
  Notaus, Zwangsstopp, Ladefensterende, Ladeende sowie
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
     Entladegrenze des Speichers entsprechend dem gewählten Modus; der normale
     Laderahmen bleibt dabei erhalten (siehe „Beobachtete Wallboxen“). Das
     Totband der PV-only-Klasse gilt dort nicht, und Senkungen wirken sofort.
     Läuft eine von E3DC-Control geregelte Wallbox-Ladung bereits, darf der Speicher sie
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
     Wallbox. Reicht die PV für Hausakku und Auto nicht belegt aus, endet
     die normale Ladekurve bei `wbminsoc` (siehe Wallbox-Grundregeln).
     Der Speicher lädt weiter nach der Ladekurve mit `iFc` als
     Laderahmen, der PV-Rest geht als Budget an die Wallbox. Der Modus schaltet
     den Speicher auch bei ausgeschalteter zentraler PV-Aufteilung unterhalb
     der Haltezone nicht in den freien Automatikbetrieb. Oberhalb der Haltezone darf der
     Speicher die Wallbox bei Akku-Bezug unabhängig von der Kurvenlage bis
     `wbminsoc` stützen. In der Haltezone direkt über `wbminsoc` bekommt die
     Wallbox nur den batterieneutralen PV-Rahmen (Akkustützung, Punkt 5).
     Unterhalb von `wbminsoc` verhält sich der Modus wie `PV-Kurve ruhig`; an
     der Untergrenze senkt beziehungsweise stoppt der Wallbox Manager die
     Ladung. Bei Akkustützung bleibt die Ladegrenze der Ladekurve auch bei
     ausgeschalteter zentraler PV-Aufteilung erhalten; nur die Entladeseite
     wird im zulässigen Rahmen freigegeben. Nötige Ladung zur Vermeidung von
     Abregelung hat weiterhin Vorrang. Eine geregelte Wallbox wird zuerst
     abgesenkt, und das Akku-Wh-Konto (Akkustützung, Punkt 4) schaltet sie bei
     Überschreiten seines Budgets ab. Die Notstromreserve bleibt vorrangig.

### Zentrale PV-Aufteilung Akku/Wallbox (experimentell)

Der Experten-Schalter `storage_central_pv_split_enable` ist standardmäßig aus.
Ausgeschaltet gilt das bisherige Verhalten. Eingeschaltet bildet der
Speicher-Manager bereits bei gestecktem Fahrzeug an einer geregelten Wallbox einen
Akkuanteil und einen absoluten Gruppenrahmen: Kurvenbedarf `iFc` zuerst,
Wallboxrahmen = PV minus Haus ohne geregelte Wallbox minus Akkuanteil.
Wärmepumpe, Heizstab und beobachtete Wallboxen zählen in der Bilanz genau
einmal. E3DC-Wallboxen werden getrennt vom Hauswert gemessen; externe
geregelte Wallboxen werden mit ihrer frischen Eigenmessung aus dem Hauswert
herausgerechnet. Ohne diese Messung ist der Rahmen unbekannt, kein Ersatzwert.

Das gilt in `PV-Kurve ruhig` sowie unterhalb der Haltezone bei geschlossenem
Tor in `PV + Akku bis Untergrenze` und `Sofort bis Preislimit`, jeweils ohne
Preis-, Netz-, Slot- oder Boostfenster. Grundladung, `Akku bis Abfahrt`,
Pre-Dump, Haltezone und die Stützung oberhalb des offenen Tors behalten ihr
bisheriges Verhalten. Ein Rahmen unter der Mindestleistung verhindert einen
Start, ohne bei ruhendem Fahrzeug eine zusätzliche Akkugrenze zu setzen.
Ein ausreichender Kandidat darf den Akku zur Bestätigung des Startangebots
begrenzen; ein Startangebot, das das Fahrzeug nicht abnimmt, endet dabei
nicht von selbst.
Eine Wärmepumpe allein ändert die Akkufreigabe nicht.

Akkuanteil, flüchtiges AUTO-Limit und Budgetabzug sind dieselbe Zahl. Ein
Totband von max(300 W, 10 % des bisherigen Akkuanteils) und mindestens
240 Sekunden Abstand begrenzen Änderungen, die den Wallboxrahmen erhöhen.
Ein höherer Akkuanteil, der den Wallboxrahmen senkt, wirkt sofort; ebenso
harte Gerätegrenzen. Der Wallboxrahmen steigt gerampt;
Senkungen wirken sofort. Ohne Bestätigung des AUTO-Limits steigt er zunächst
nicht über den zuletzt veröffentlichten Rahmen. Ohne Vorwert gilt der
kleinere Wert aus Kandidat und PV-Rest nach gemessener Akkuladung. Nach
höchstens 60 Sekunden gilt nur dieser konservative Rest:
`min(Kandidat, max(0, PV − feste Last − max(0, Akkuladung)))`.
Fehlt die Batteriemessung, bleibt der konservative Rest unbekannt; daraus
entsteht keine Anhebung. Harte Schutzgrenzen behalten Vorrang.

Das Budget ist der Rahmen für die Wallboxen; Strom, Phasen und Stopp
entscheidet der Wallbox Manager. Schutzstopps, Unterdeckungsregel, Datenfrist
und Wiederanlauf wirken auch bei zentraler PV-Aufteilung wie ohne sie.
Die Kurvenleistung ist daher ein Laderahmen und keine garantierte physische
Akkuladeleistung. Im E3DC-Sonnenmodus begrenzt auch dieser Rahmen den
Stromdeckel; Mindeststrom und Geräteverzögerungen bleiben wirksam.

Kurzer Test: Auf einem eigenen System bei frischen Messwerten in
`PV-Kurve ruhig` einschalten, Akkuanteil, AUTO-Readback, Wallboxrahmen und
gemessene Leistungen beobachten. Eine Wolke muss den Rahmen sofort senken;
nach Sonne steigt er schrittweise. Bei unerwartetem Netzbezug, fehlender
Bestätigung oder unruhigen Ausgängen ausschalten und die Diagnose sichern.
Das Feld `central_pv_split` steht im Speicherzustand und Wallboxbudget.

### Beobachtete Wallboxen

Eine openWB Software 2.x als Primary in `PV-Kurve ruhig` oder `PV + Akku`
sowie ein konfigurierter Ladepunkt im Modus `Aus` mit ausdrücklich gesetzter
Beobachtungspolitik wird über den Speicher berücksichtigt. Rolle, Modus und
Netz-, Preis- oder Pre-Dump-Auftrag gelten je Ladepunkt. Andere geregelte
Ladepunkte behalten ihre Budgets. Ohne Beobachtungspolitik bleibt Modus `Aus`
unverändert. Deaktivierte oder für diesen Dienst gesperrte Ladepunkte lösen
keine Beobachtungspolitik aus.

Die Speicherladung folgt dem normalen Kurvenregler. Bei aktivem abgesenktem
Abendziel gilt die unten beschriebene Ausnahme für den nicht nutzbaren
Fahrzeugüberschuss bis zur Korridorobergrenze. Eine beobachtete Wallbox
fordert keine feste Speicherladung an.
Ihre Leistung zählt in der Energiebilanz genau einmal; externe Wallboxen sind
bereits im gemessenen Hausverbrauch enthalten und bleiben für die Kurvenladung
Hauslast. In `PV-Kurve ruhig` und bei Politik `curve` deckelt E3DC-Control die
Entladung auf das Defizit von Haus und Wärmepumpe. Der Deckel wird ab 500 W
aktiv und endet erst nach 45 Sekunden durchgehend gültig gemessener Leistung
unter 300 W. Für Datenlücken gilt die gemeinsame Haus-/WP-Lückenregel im
Abschnitt zum Abendziel. Senkungen wirken sofort; Erhöhungen des gemessenen
Defizitdeckels gedämpft und frühestens nach 30 Sekunden. Der Lückendeckel
wirkt ohne diese Verzögerung. Ein kleiner anhaltender
Hausbedarfssprung wird nach 30 Sekunden ebenfalls übernommen.

In `PV + Akku` und bei „Nur beobachten, Speicher oberhalb Hausreserve freigegeben“ ist die Entladung oberhalb der wirksamen
`wbminsoc`-Untergrenze mit Hysterese frei. Am Boden gilt der Haus-/WP-Deckel.
Die Beobachtung hebt die Kurve auch dort nicht an: Das aktive Planziel führt
die Ladekurve, einschließlich eines gültigen Rückfallziels bei `wbminsoc`.
Ohne gültige Kurve entsteht daraus kein fester Ladeauftrag. Echte Preis-,
Slot- und Unwetterfenster bleiben wirksam; die beobachtete Leistung allein
löst sie nicht aus. Notstromreserve, Peak-Shaving, Hausanschluss und
Datenwächter behalten Vorrang.

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
- Pause, Not-Aus und Ladepunktsperre beenden eine laufende Ladung sofort mit quittiertem Stopp, auch in „Beobachten“. Anti-Flattern und Mindestladezeit verzögern diesen Stopp nicht. Das Abbruchbit der Box zeigt, ob die Box eine Ladung freigibt oder gesperrt hat. Native E3DC-Wallboxen erhalten einen Stoppimpuls nur, wenn sie frisch „nicht abgebrochen“ melden, auch ohne Leistungsbezug. „Abgebrochen“ quittiert den Stopp ohne weitere Flanke. Fehlt die Abbruchmeldung der Box über mehrere Proben, ist genau ein Stopp mit frischem Ladebeleg zulässig; die Anzeige meldet „Zustand der Box nicht lesbar“. Andere Typen nehmen auch ein stehendes Angebot zurück. Bleibt nach einer nativen Stoppflanke der Bitwechsel 30 Sekunden aus, entsteht ein Fehler. Im Fehlerzustand „Box reagiert nicht auf den Stopp“ erhalten Pause, Ladepunktsperre, Not-Aus und Hausanschluss gemeinsam genau eine zusätzliche Stoppflanke: frühestens 60 Sekunden nach dem letzten Stoppversuch, bei frischer Probe mit „nicht abgebrochen“ oder unbekanntem Bit und nur mit frischem Ladebeleg. Die Fehleranzeige erscheint ab Fehlerbeginn. Die zusätzliche Einmalflanke ist höchstens einmal je Stecksession und Dienstlauf zulässig; ist sie verbraucht oder für den Stoppgrund nicht berechtigt, bittet die Oberfläche, den Ladepunkt zu prüfen. Nach einem Neustart des Dienstes steht sie einmal neu zur Verfügung. Eine Stecksession endet erst mit bestätigtem Abstecken: mindestens drei aufeinanderfolgende frische Proben ohne gestecktes Fahrzeug über mindestens zehn Sekunden. Insgesamt sind höchstens drei Versuche je Ereignis zulässig; eine bereits verbrauchte Einmalflanke wird durch einen späteren Nutzer-Stopp nicht erneuert. Ein beobachteter externer Neustart wird bei Nutzer-Aus, Schutzgründen, Ladefensterende, Ziel erreicht, Abfahrt oder Budget-Zeitlimit erneut gestoppt, frühestens 30 Sekunden nach der letzten Flanke. Bei Unterdeckung übernimmt der Regelkern die externe Ladung und beendet sie nach seinen normalen Sitzungsregeln. Sperre, Pause und Not-Aus bleiben nach Dienst- und Host-Neustart sowie Containerstart bestehen. Die Ladepunktsperre gilt als Nutzer-Aus. Die Ladepunktsperre über den Schalter mit dem Power-Symbol des jeweiligen Ladepunkts aufheben. Nach Not-Aus bleibt die Freigabe in der erweiterten Ansicht auf der Seite „Wallbox“. Moduswahl, Abstecken und Speichern in der einfachen Ansicht heben die Sperre nicht auf. Fortsetzen, ein bewusster Moduswechsel, Sofortstart oder externes Fortsetzen an einer openWB Primary heben nur die Pause auf. Eine Pause an einer leeren Box bleibt bis „Fortsetzen“. Abstecken beendet sie nur, wenn E3DC-Control das Fahrzeug während der Pause gesteckt gelesen hat. Konnte die Box in dieser Zeit nicht gelesen werden (Statuslücke, Neustart), bleibt die Pause nach dem Abziehen bestehen und endet mit „Fortsetzen“. Fehlt der eingestellte Ladepunkt in der openWB-Antwort, zeigt die Diagnose dessen Nummer; die Ladepunktnummer im Config-Editor prüfen. In den PV-Modi kehrt eine Primary nach Freigabe eines eigenen Stopp- oder Sofortauftrags, bewusstem Moduswechsel oder aufgehobener importierter Fremdpause genau einmal auf PV zurück; dies gilt auch nach Abstecken und Wiederanstecken. Während Pause, Not-Aus oder Ladepunktsperre gibt es keine PV-Rückgabe. Eine Nutzerwahl an der openWB ohne solche Aktion bleibt bestehen. Bleibt „Beobachten“ eingestellt, wird beim Fortsetzen oder Freigeben einmalig die Grundeinstellung freigegeben und anschließend wieder beobachtet. Bei nativen Boxen hebt Fortsetzen in „Beobachten“ nur die eigene Pause oder Ladepunktsperre derselben Stecksession genau einmal auf, wenn die Box frisch „abgebrochen“ meldet. Diese Übergabe benötigt kein Regelbudget. Eine in der E3DC-App gesetzte Sperre hebt „Beobachten“ nicht auf. Nach Not-Aus erfolgt in „Beobachten“ keine automatische Freigabe; die Box bleibt bis zur App-Freigabe gesperrt. Endet eine Pause in „Beobachten“ durch bestätigtes Abstecken, bleibt eine native Box gesperrt, bis sie in der E3DC-App freigegeben oder ein Regelmodus gewählt wird. In einem Regelmodus ist eine App-Sperre kein dauerhaftes Aus: Die Box erhält Start und Stopp über denselben Umschaltbefehl, deshalb hebt der nächste reguläre Start der Regelung eine vor dem Start in der App gesetzte Sperre auf. Für ein dauerhaftes Aus die Ladepunktsperre (Schalter mit dem Power-Symbol) verwenden. Die bewusste Freigabe über den Schalter der Ladepunktsperre gibt die Box genau einmal frei, auch ohne gestecktes Fahrzeug, solange seit der Sperre weder eine Pause noch ein neues Anstecken dazwischenlag.
- `Aus` ist der bewusste Beobachtungsmodus: keine Regelbefehle, keine
  Auto-Erkennung. Nur beim bewussten Wechsel auf `Aus` in der WebUI wird einmalig
  die Wallbox-Grundeinstellung freigegeben; zusätzlich einmal beim Fortsetzen
  einer Pause in diesem Modus, nicht bei Reconnect oder Neustart.
  Beim bewussten Wechsel auf „Beobachten“ und beim Fortsetzen einer Pause in diesem Modus setzt E3DC-Control eine E3DC-Wallbox einmalig auf die gewählte Betriebsart und den eingestellten Maximalstrom, begrenzt auf Hausanschluss minus Reserve. Die gemeinsame Einstellung „Betriebsart für Beobachten wählen“ im Config-Editor bietet Sonnenmodus (Werkseinstellung) und Mischmodus. Ein fehlender oder unbekannter Wert bedeutet Sonnenmodus. Änderungen wirken erst beim nächsten bewussten Wechsel oder Fortsetzen, nicht während „Beobachten“. Der Rahmen ändert die Start-/Stoppfreigabe nicht. Ohne PV-Überschuss kann die Box im Sonnenmodus trotzdem aufhören zu laden. Im Mischmodus kann die Box je nach E3DC-Einstellung auch Netz und Hausakku nutzen; dies entspricht derselben Einstellung im E3DC-Portal. Danach regelt der E3DC selbst.
  Deaktivierte Wallboxen werden weder erkannt noch gesteuert.

### Akkustützung der Wallbox in `PV-Kurve ruhig` nach Korridorlage

1. **Grundsatz.** In `PV-Kurve ruhig` lädt die Wallbox aus PV-Überschuss. Der
   Hausspeicher ist keine Ladequelle für das Fahrzeug. Er darf die Wallbox nur
   im Rahmen eines kleinen, benannten Wolken-Kontingents stützen; ob und wie
   viel, entscheidet die Speicher-Ladekurve, nicht ein fester SoC-Wert.
   Akkuladen des Fahrzeugs bis zu einer Untergrenze gibt es ausschließlich in
   `PV + Akku bis Untergrenze` (Untergrenze `wbminsoc`). Auch bei dieser
   Akkustützung bleibt die Ladegrenze der Ladekurve bestehen; freigegeben
   wird nur die Entladeseite im zulässigen Rahmen.
2. **Bezugsgröße.** Die Lage des Speicher-SoC zum Kurvenkorridor zwischen
   Untergrenze (`soc_min_curve`) und Obergrenze (`soc_ceiling_curve`) des
   Speicherplans: `above_ceiling`, `inside_band`, `below_floor` oder `no_curve`
   (Feld `adaptive_curve_relation` im Storage-Zustand).
3. **PV-Anteil und Wolkenstützung.** Der Speicher veröffentlicht
   `budget_pv_w` nach festen Lasten und seinem eigenen Ladeanspruch sowie
   `budget_battery_w` als Rest des Gesamtbudgets. In AUTO-Freilauf beträgt
   dieser Ladeanspruch 0 W. Keine Momentanbilanz der Akkuleistung vergrößert
   den PV-Anteil. Eine PV-Box erhält nur den verbleibenden PV-Anteil nach
   den Akkuboxen. Die Korridorlage gibt ihr keine Akkuquelle für Anhebungen.
4. **Kurzes Halten.** Das vorhandene Wolken-Kontingent
   `wb_curve_floor_support_wh` beträgt automatisch 0,5 % der Kapazität,
   mindestens 50 Wh; ein Handwert gilt absolut, ebenfalls mindestens 50 Wh.
   Es zählt nur den tatsächlich aus dem Akku gestützten Anteil der PV-Boxen.
   Die Akkuquelle einer anderen Box hebt die Kontingentgrenze nicht auf.
   Bei geeigneter Unterdeckung erfolgt nach 60 Sekunden der Abstieg auf
   eine Phase. Das verbrauchte Kontingent bleibt auch über eine längere
   Nullpause der Phasensequenz erhalten und steht danach mit seinem Rest
   für weitere Wolken bereit. Unterdeckung hält höchstens den Mindeststrom; nach drei Minuten und
   mindestens fünf Minuten Ladung folgt der reguläre Stopp. Fehlt die
   Stützungsfreigabe, gilt dieselbe Unterdeckungsregel. Nach Ende des
   Wolken-Kontingents kann der Akku deshalb bis zu 180 Sekunden weiter stützen.
   Nach einem Phasenwechsel hält die Box bei Unterdeckung in den ersten
   480 Sekunden der bestehenden Phasensperre Mindeststrom; Schutzstopps wirken
   sofort. Die Wiederherstellung der Sperre erhält auch diese Haltezeit.
   Ohne gesetzte Phasensperre erzeugt ein selbst ausgeführter Wechsel keinen
   zusätzlichen Halt.
5. **`wbminsoc`.** Die Untergrenze begrenzt nur Akkustützung. Ein ausreichender
   veröffentlichter PV-Anteil erlaubt weiterhin Laden. Die bestehende
   SoC-Hysterese und Speicher-Haltezone bleiben erhalten. Netzladen im
   freigegebenen Netzfenster und Netzangebote bleiben an der Notstromreserve
   zulässig; der Speicherregler sperrt die Akkuentladung, die Akkustützung
   der Wallbox endet an der Reserve. „PV + Akku bis
   Untergrenze“, „Sofort bis Preislimit“ ohne Netzfenster und „Akku bis Abfahrt“
   im Abfahrtsfenster nutzen oberhalb der offenen Untergrenze das Gesamtbudget;
   darunter nur den PV-Anteil. „Grundladung stabil“ hält Mindeststrom nur,
   solange seine bestehende Grundladungsbedingung gilt. Keine fehlende
   SoC-Messung öffnet eine Akkuquelle.
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
8. **Anzeige.** Der Entscheidungssatz je Ladepunkt nennt Anteil, Strom,
   Phasen und Grund. Das Wolken-Kontingent und die gemessene Wirkung bleiben
   von einer bloßen Freigabe unterscheidbar. Fehlende Quellen erscheinen als
   `null` mit Gültigkeit und Quelle. Fehlt nur der gültige PV-Teil, erhalten
   PV-Boxen weder Start noch Anhebung; ein gültiges Gesamtbudget bleibt für
   Akkuboxen nutzbar.

### Wallbox-Grundregeln

- Auch bei eingeschalteter zentraler PV-Aufteilung liest der Kern den
  veröffentlichten Rahmen über denselben Budgetvertrag. Harte Schranken
  bleiben vorrangig.

- Bei klassischer openWB wird die tatsächliche Rolle unterschieden: Secondary
  mit Sollstrom und Heartbeat, Primary-PV mit eigener PV-/Ladepunktlogik oder
  ausdrücklich aktivierter Primary-Direktpfad. Eine Rolle folgt nie aus dem
  Produktnamen oder einem MQTT-Messwert. Direkte Phasenbefehle an eine
  klassische openWB gibt es nicht; Herstellerregler und lokale Grenzen bleiben
  bestehen, und es gibt keine pauschalen Lademodus-Kommandos für alle Rollen.
  Anzeige und Diagnose passen zur tatsächlichen Rolle und Befehlsautorität.
- openWB Pro wird im Standalone-Betrieb ausschließlich über `connect.php`
  gesteuert: Lesen per `GET`, Sollstrom `ampere` (0 oder 6,0–32,0), Phasenziel
  `phasetarget` 1 oder 3, Weckruf `cp_interrupt=true`, Heartbeat `heartbeatenabled`.
  `update=1` löst das Firmware-Update der Box aus und gehört nicht zur Regelung.
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
  übrigen Schutzgrenzen eingehalten sind. Eine pauschale PV-Ladesperre folgt
  daraus nicht. In `PV + Akku bis Untergrenze` führt der Planer das Abendziel
  abhängig vom gemeinsamen Energiebedarf von Hausakku und Fahrzeugen:

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
  Bei externen Wallboxen löscht eine fehlende gültige Messung den Ladebeleg ohne Nachhaltezeit. Unterbrechungen setzen den
  45-Sekunden-Timer zurück; negative Leistung bleibt ungültig, niemals 0 W.
  Bei der E3DC-Wallbox bleibt die bisherige Nachhaltung bis zu 600 Sekunden
  ab der letzten gültigen Bestätigung erhalten. Die fehlende aktuelle Leistung
  bleibt dabei unbekannt.
  Der getrennte Beleg „Auto hat geladen“ hält bei unbekanntem Bedarf das
  Rückfallziel nur am Tag der letzten Bestätigung ab 500 W und nur innerhalb
  derselben Steck-Episode. Bei externen Wallboxen endet damit auch der Tagesbeleg bei Datenverlust. Er endet bei Pause, Vollmeldung, bestätigtem Abstecken oder
  nach 60 Minuten gültig unter 300 W, während genügend PV-Überschuss und ein
  Stromangebot mindestens in Höhe des Fahrzeugminimums vorhanden sind.
  Kurze Wolken, fehlendes Angebot und einzelne unbestätigte
  Absteckmeldungen bis einschließlich zwei Minuten halten die Verfallsuhr nur an;
  sie zählen nicht als Angebotszeit. Eine längere Unterbrechung setzt sie zurück.
  Das Angebot verwendet den vorhandenen geglätteten Netzwert. Normale Akkuladung
  ist kein Fahrzeugangebot: Für diese Uhr zählen Einspeisung und belegter
  Mindeststrom. Ohne belegte Leistungsgrenzen oder Stromangebot läuft die Uhr
  nicht; der Diagnosegrund bleibt sichtbar. Der Tagesbeleg liegt im Dienstspeicher
  und benötigt nach dessen Neustart eine neue Bestätigung ab 500 W.
  Bei fehlender gültiger Messung einer externen Wallbox nach der
  15-Sekunden-Frischegrenze entfällt
  der Wallbox-Einfluss ohne Nachhaltezeit: kein Entladedeckel und keine
  Budgetreservierung. Gültig gemessene weitere Ladepunkte bleiben wirksam.
  Der Hausverbrauch einschließlich externer Wallbox bleibt vollständig in
  der Bilanz. Mit der Rückkehr gilt die aktuelle Messung ohne alte Latches.

  Ohne gültige Messung einer konfigurierten externen Wallbox bleibt ihre
  unbekannte Leistung im gemessenen Hausverbrauch enthalten. Diese Zeitabschnitte
  werden als „Haus inkl. Wallbox (unbekannt)“ gekennzeichnet und nicht zum Lernen
  der Hausverbrauchsprognose oder ihrer historischen Profile verwendet. Die
  Energiesummen und die Regelung am Netzpunkt bleiben erhalten; es gibt keinen
  zusätzlichen Leistungsabzug. Die E3DC-Wallbox mit eigenem Messpunkt ist davon
  nicht betroffen.

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
- Wärmepumpe und Wallbox: Die Priorität berücksichtigt die tatsächliche
  Aufnahme und den geschützten Verdichterbetrieb. Auch im Messwertbetrieb
  kann ein höher priorisierter Start einmalig eine befristete Startreserve
  erhalten. Sobald die Wärmepumpe Leistung aufnimmt, ersetzt ihre gemessene
  Istleistung die Startreserve. Läuft die Frist ohne Start ab, wird die
  Reserve frei; derselbe Bedarf verdrängt die Wallbox nicht erneut.
  Nicht angenommene freigegebene Leistung kann die Wallbox erhalten.
  Mindestlaufzeiten werden nur durch benannte Schutzfälle verkürzt; ein
  gewöhnlicher Budget- oder Prioritätswechsel genügt nicht. Im Luxtronik-
  Messwertbetrieb schützt die Mindestlaufzeit auch beim Wh-Kontingentende
  und bei gesperrter Netz- oder Akkuunterstützung (0 W/0 Wh).
  Inselbetrieb, Hausanschlussgrenze, Gerätefehler und die SoC-Sicherheitsabschaltung
  wirken sofort. Der Netzstopp verlangt 30 Sekunden zusammenhängend frischen
  WP-Netzanteil über 2.500 W, begrenzt auf min(WP-Leistung, Netzbezug minus 200 W). Ohne frische WP-Einzelmessung (fehlend, ungültig oder älter als 45 Sekunden) zählt der gesamte Netzbezug abzüglich 200 W. Die Frist beträgt auch dann 30 Sekunden zusammenhängend mit frischen Netzwerten; ein Wechsel der Messgrundlage beginnt sie neu. Nutzer-Aus
  und Notstromreserve nehmen den PV-Auftrag zurück; ein laufender Verdichter
  beendet seine Mindestlaufzeit, für Heizung und Warmwasser gleich. An der
  Notstromreserve sperrt der Speicher die Entladung; die Restlaufzeit kommt aus
  dem Netz. Die SoC-Sicherheitsabschaltung gilt auch bei Preis- und Negativpreis-Boosts. Tarif-Aufträge
  mit aktivem Tarif-Halt behalten die Mindestlaufzeit. Der Speicherregler muss dazu
  im aktuellen Zyklus nachweislich die Entladung um die gemessene WP-Leistung
  begrenzen. Bei fehlendem oder veraltetem Beleg und bei Vorrang der
  Lastspitzenkappung wirkt der SoC-Schutz sofort.
  
  Nutzer-Aus nimmt auch Tarif-Aufträge zurück. Bei Luxtronik-Direktanbindung
  behält ein laufender Verdichter für Warmwasser und Heizung seine Mindestlaufzeit,
  auch nach dem durch „AUTO AUS“ ausgelösten Dienstneustart. Fehlt der aktuelle
  Gerätestatus oder die Verdichterbeobachtung, bleibt der Mindestlaufhalt für
  PV- und Tarif-Aufträge höchstens 60 Sekunden ab der letzten gültigen Beobachtung
  bestehen. Wiederholte Abfragen verlängern die Frist nicht; eine neue gültige Probe
  erneuert den Beleg. Danach wird der Auftrag zurückgenommen. Gerätefehler,
  EVU-Sperre, Inselbetrieb, Hausanschlussgrenze und Uhrfehler wirken weiterhin sofort.
  Es gelten die
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
| `Beobachten` (intern `Aus`) | E3DC-Control beobachtet, die Wallbox bzw. der E3DC regelt selbst. Beim bewussten Wechsel und beim Fortsetzen einer Pause in diesem Modus wird einmalig die Grundeinstellung freigegeben. Pause und Not-Aus stoppen weiterhin sofort. Geplantes Netzladen bleibt gesperrt. |
| `PV-Kurve ruhig` | Laden aus PV-Überschuss entlang der Speicher-Ladekurve mit Hysterese; über oder im Kurvenkorridor stützt der Hausspeicher nur im Rahmen des Wolken-Kontingents. Unter dem Korridor erhält die Wallbox bei ausgeschalteter zentraler PV-Aufteilung den reinen PV-Überschuss. Mit experimenteller zentraler PV-Aufteilung wird der Kurvenbedarf zuerst vom absoluten Wallboxrahmen abgezogen (Abschnitt „Akkustützung der Wallbox in `PV-Kurve ruhig` nach Korridorlage“). |
| `Grundladung stabil` | Wie PV-Kurve ruhig, aber mit stabiler 1p/3p-Grundladung, solange `wbminSoc` laut Planung erreichbar bleibt. |
| `PV + Akku bis Untergrenze` | Das Auto darf PV plus Hausspeicher oberhalb der Hausakku-Untergrenze nutzen. Oberhalb der Haltezone ist die Entladestützung freigegeben; die Ladegrenze folgt weiter der Ladekurve, einschließlich nötiger Ladung zur Vermeidung von Abregelung. Mit experimenteller zentraler PV-Aufteilung führt unterhalb der Haltezone bei geschlossenem Tor der Kurvenbedarf den Akkuanteil. Unterhalb der Grenze lädt das Auto wie in `PV-Kurve ruhig` nur aus PV-Überschuss, der Speicher stützt nur Hausverbrauch und Wärmepumpe, Netz bleibt außen vor. |
| `Sofort bis Preislimit` | Netzladen im freigegebenen Preisfenster mit zulässigem Höchststrom, Start ohne Wartezeit; außerhalb des Fensters wie „PV + Akku bis Untergrenze“. Das Fenster endet mit Stopp; ein späterer Start aus PV oder Akku benötigt erneut 60 Sekunden ausreichenden Anteil. |
| `Akku bis Abfahrt` | Lädt im Freigabefenster vor der Abfahrtszeit aus PV und Hausspeicher bis zur Hausakku-Untergrenze `wbminsoc`; Netzladen bleibt gesperrt. Außerhalb des Fensters, zur Abfahrtszeit und bei erreichtem Fahrzeugziel wird gestoppt; an der Untergrenze kann mit ausreichendem PV-Anteil weitergeladen werden (`wb<n>_battery_departure_time`, `wb<n>_battery_departure_window_h`). |

Geplantes Netzladen per Zeitfenster/Slot darf in allen aktiven Modi laden und
ignoriert bewusst das globale Wallbox-Preislimit, weil die Preislogik bereits
bei der Slot-Auswahl steckt.

Wichtige Schutzlogik:

- Schwellen wie `wbminsoc` arbeiten mit Hysterese.
- Die Netzbremse prüft Netzpunkt minus frisch gemessene Netzboxleistung.
  Zwei Regeltakte über 300 W senken den Strom bis zum Mindeststrom.
  Nach Start, Anhebung oder Phasenwechsel gilt die bestehende 20-s-Frist;
  Netzboxen haben keine Netzbremse. Hausanschlussgrenzen wirken sofort.
  Details: `doc/Native_Wallbox.md`, „Netzbezug beim Laden: direkte Regelung
  und Netzbremse“.

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
  gültige Hausabsicherung aus dem E3DC oder der Nutzereingabe und die Zuordnung
  `wb<n>_grid_phase`; der 35-A-Ersatzrahmen für PV-Laden zählt nicht. Ein höherer
  Nutzerwert überschreibt den E3DC-Wert erst nach ausdrücklicher Bestätigung.
  Ohne beide Werte ist Netzladen nicht möglich; der
  automatische Software-Nachweis dieser Zuordnung beim Laden ist nur ein Hinweis (Dashboard:
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
- openWB-Pro-Bereitschaft nach Fahrzeug-Ladeende bleibt an dieselbe
  Stecksession gebunden. Das Quellenbudget des aktiven Modus muss die
  Mindestleistung der gehaltenen oder erwarteten Phasen tragen; der
  Ladepunktrahmen allein ist dafür kein Maß. Kurze Budgeteinbrüche werden mit
  `openwb_pro_start_hold_s` überbrückt (Standard 180 s, 60–600 s, bestehender
  HLC-Faktor 2). Danach folgt einmal `ampere=0`; die Budgetrückkehr bietet
  nach den bestehenden Schutzfristen nur den Mindeststrom an, ohne CP-,
  Phasen- oder Startwiederholung. Für Mindestleistung und Ausgang gelten
  die frischen Phasen; bei Abweichung zum Ladeendebeleg der größere Bedarf.
  In „PV-Kurve ruhig“ ist das Akku-Wolkenkontingent keine Bereitschaftsquelle.
  Ein 15 bis unter 45 Sekunden alter Budgetdatensatz hält nur ein zuvor
  gedecktes, bereits stehendes Angebot unverändert; er erlaubt keine Anhebung
  und keinen neuen Ausgang. Dasselbe gilt bei einem kurzen Statusausfall
  eines anderen Ladepunkts bis 45 Sekunden nach dem letzten gültigen
  Quellenbeleg. Danach gilt 0 A. Ein Mindestangebot erhält nur Quellenbudget,
  das nicht bereits an andere Ladepunkte verteilt ist; Istleistung und
  Zuteilung derselben Box zählen dabei einmal mit dem höheren Wert.
  Ungültige eigene Status- und Live-Daten sowie
  Gruppen-, Hausanschluss- und andere harte Schutzgrenzen wirken sofort.
  Ohne Fahrzeug-Ladeende-Evidenz kehrt ein Kaskadenstopp über das normale
  Startfenster zurück. Details: `doc/Native_Wallbox.md`, „Budgetabhängige
  Bereitschaft“.
- Start und Phasen folgen dem gemeinsamen Regelkern. Ein gültiger Anteil
  über der Mindestleistung qualifiziert den Start nach 60 Sekunden; eine freigegebene Netzquelle startet ohne diese Wartezeit. Bleibt
  das Fahrzeug im Standby oder folgt seinem eigenen Ladeplan, wird der
  Start nicht wiederholt; nach spätestens 30 Sekunden nennt der
  Entscheidungssatz den fehlenden Ladebeginn. Ein später selbst ladendes
  Fahrzeug wird ohne neue Startflanke übernommen.
- Die Phasenentscheidung nutzt den eigenen Quellenanteil: aufwärts nach
  60 Sekunden, abwärts nach 60 Sekunden geeigneter Unterdeckung. Elektrische Reservierung,
  Speicherfreigabe, bestätigter Phasenreadback und die vorhandene
  Gerätesequenz bleiben nötig. Die mindestens 480 Sekunden nach einem
  Wechsel sperren weitere Phasenwechsel, nicht den bestätigten
  Wiederanlauf. Ein Export-Wh-Konto und ein zusätzlicher Vorlauf entfallen.

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

Bei SG-Ready-Ausgängen sperrt die Heizgrenze die automatische Freigabe nicht.
Bedarf besteht, wenn mindestens eine gültige, frische Warmwasser- oder
Rücklauftemperatur unter ihrem Maximum abzüglich Totzone liegt. Fehlende
Kanäle zählen nicht; ohne gültige Temperaturdaten gibt es keine automatische
Freigabe. Das Temperaturziel ist erst erreicht, wenn alle gültigen Kanäle
ihr Ziel erreicht haben. Signalhalt, Mindestlaufzeit und Wiedereinschaltsperre
bleiben wirksam. Luxtronik und iDM behalten ihre Sommer-/Winter-Umschaltung
über die Heizgrenze und ihre bisherige Saisonanzeige.

Im Luxtronik-Messwertbetrieb (`wp_pv_control_mode=measured`) bietet der
Energy Manager den PV-Boost bei qualifiziertem Überschuss an. Die Startkante
benötigt die WP-Startleistung plus das bestehende 500-W-Schaltband. Bei Vorrang
„WP vor Wallbox“ zählt auch der Überschuss, den die Wallbox gerade verbraucht;
ihr Haltebudget erzeugt kein künstliches PV-Defizit. Ein aktiver Wallbox-Start-Halt
sperrt trotzdem neue WP-Starts. Beginnt er erst nach dem Versand, bleibt der
gemessene WP-Verbrauch im Budget sichtbar.

Automatische Zusatzfreigaben für PV, Preis/Markt und Tariffenster beginnen
nur mit gültigem Akkustand ab `min_soc`. Unter `max(5, min_soc − 5)` endet
die Freigabe. Bei aktivem Taktschutz (Mindestlaufzeit oder Wiedereinschaltsperre
gesetzt) gilt zuvor die belegte Verdichter-Mindestlaufzeit und der zehnminütige
Signalhalt. Beide Fristen laufen parallel; danach folgt keine zusätzliche
Stoppverzögerung. Ohne bestätigten Verdichterstart gilt nur der belegte Signalhalt.
Ohne Taktschutz endet die Freigabe sofort unter dieser Stoppschwelle. Netz-, Hardware-, Quellen- und andere Schutzschranken
bleiben vorrangig. Die Eigenregelung der Wärmepumpe, Nutzeraufträge und die
eigene Pre-Dump-Planfreigabe behalten ihre bisherigen Bedingungen.

Die Laufzeitgrenze bleibt während einer laufenden Episode auch bei einem Besitzerwechsel oder einer Konfigurationsänderung fest. Nach einem Neustart gilt die kürzere Dauer aus dem gespeicherten Wert und der aktuellen Mindestlaufzeit der ursprünglich gespeicherten Klasse (Pre-Dump oder allgemein). Eine Erhöhung verlängert die Episode nie; eine Verringerung verkürzt sie nur nach einem Neustart auf den aktuellen Nutzerwert, sofern die gespeicherte Dauer nicht schon kürzer war. So bleibt der eingestellte Taktschutz erhalten, ohne zusätzliche Laufzeit mit möglichem Netzbezug oder Akkuentladung unter Min SoC zu erzeugen.

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
der bestehenden Freigabe beendet den Boost. Im direkten Luxtronik-Messwertbetrieb
zählen für ein Ende durch das positive Netz-Wh-Kontingent nur gemessene,
zugeordnete WP-Anteile, höchstens Netzbezug minus 200 W. Nicht gemessene
Buchungen und das Akkukonto sperren nur die jeweilige Brücke. Ein verbrauchtes
Netzkontingent beendet bei weiterem WP-Netzbezug erst nach Mindestlaufzeit und
der zehnminütigen Haltezeit des Sollwerts. Auch bei gesperrter Unterstützung
(0 W/0 Wh) bleibt dieser Mindestlaufhalt im Messwertbetrieb bestehen. Der
unabhängige Netzstopp bei mehr als 2.500 W WP-Netzanteil (ohne frische WP-Einzelmessung: gesamter Netzbezug abzüglich 200 W) über 30 Sekunden kann früher eingreifen.
Inselbetrieb, Hausanschlussgrenze, Gerätefehler und die SoC-Sicherheitsabschaltung
wirken sofort. Der Netzstopp verlangt 30 Sekunden zusammenhängend frischen
WP-Netzanteil über 2.500 W, begrenzt auf min(WP-Leistung, Netzbezug minus 200 W). Ohne frische WP-Einzelmessung (fehlend, ungültig oder älter als 45 Sekunden) zählt der gesamte Netzbezug abzüglich 200 W. Die Frist beträgt auch dann 30 Sekunden zusammenhängend mit frischen Netzwerten; ein Wechsel der Messgrundlage beginnt sie neu. Nutzer-Aus
und Notstromreserve nehmen den PV-Auftrag zurück; ein laufender Verdichter
beendet seine Mindestlaufzeit, für Heizung und Warmwasser gleich. An der
Notstromreserve sperrt der Speicher die Entladung; die Restlaufzeit kommt aus
dem Netz. Die SoC-Sicherheitsabschaltung gilt auch bei Preis- und Negativpreis-Boosts. Tarif-Aufträge
mit aktivem Tarif-Halt behalten die Mindestlaufzeit. Der Speicherregler muss dazu
im aktuellen Zyklus nachweislich die Entladung um die gemessene WP-Leistung
begrenzen. Bei fehlendem oder veraltetem Beleg und bei Vorrang der
Lastspitzenkappung wirkt der SoC-Schutz sofort.

Nutzer-Aus nimmt auch Tarif-Aufträge zurück. Bei Luxtronik-Direktanbindung
behält ein laufender Verdichter für Warmwasser und Heizung seine Mindestlaufzeit,
auch nach dem durch „AUTO AUS“ ausgelösten Dienstneustart. Fehlt der aktuelle
Gerätestatus oder die Verdichterbeobachtung, bleibt der Mindestlaufhalt für
PV- und Tarif-Aufträge höchstens 60 Sekunden ab der letzten gültigen Beobachtung
bestehen. Wiederholte Abfragen verlängern die Frist nicht; eine neue gültige Probe
erneuert den Beleg. Danach wird der Auftrag zurückgenommen. Gerätefehler,
EVU-Sperre, Inselbetrieb, Hausanschlussgrenze und Uhrfehler wirken weiterhin sofort.

Im Inselbetrieb werden alle Kanalbesitzer sofort zurückgenommen, auch Preis- und
Prognosepausen; die Wärmepumpe übernimmt wieder ihre eigene Regelung.
Bei Netzbezug wartet die WP auf eine von E3DC-Control abschaltbare Wallbox,
während diese am Mindeststrom lädt oder einen Phasenwechsel bzw. Stopp bestätigt.
Das gilt unabhängig von der Verbraucherrangfolge, auch bei Netzunterstützung auf 0.
Ein gesperrter Akkuanteil wird dadurch nicht freigegeben. Die Wolkenfrist der WP ruht
während dieses Wartens. Ein fehlender Beleg wird höchstens 60 Sekunden überbrückt;
ein bestätigter Ladestopp beendet das Warten mit der nächsten frischen Statusprobe.
Die Frist folgt dem Fortschritt des Wallbox-Netzkontos, endet aber spätestens
nach 30 Minuten je Ladevorgang, vom Beginn bis zum belegten Ladeende.
Nur ein belegtes Ladeende erlaubt eine neue Frist desselben Ladepunkts. Ein Phasenwechsel oder ein neuer WP-Auftrag setzt
die Frist nicht zurück. Zwei Ladepunkte werden getrennt verfolgt.
Die Wallbox-Schwelle ist auf höchstens 40 Wh begrenzt. Für ihr Netzkonto gelten
die eingestellte Netzbezugstoleranz und die laufende Verringerung des Kontostands
(Standard: 200 W Toleranz und Verringerung entsprechend 80 W). Während des
belegten Wallbox-Mindesthalts zählt auch die WP erst oberhalb ihrer Summe,
standardmäßig 280 W, mindestens jedoch 200 W. Kleiner Netzbezug darunter wird
auch bei einer Netzunterstützung von 0 W/0 Wh nicht gezählt.
Ohne diesen Mindesthalt gilt für die WP weiterhin das 200-W-Band.
Als Ladung gilt eine Wallbox-Aufnahme über 200 W, unabhängig von der Netztoleranz.
Wallboxen mit eigener Ladeentscheidung (etwa openWB als Hauptregler) sowie
Sofort-, Netz- und Preisladung begründen kein
Warten; Standby zählt nicht als Ladung. Die WP zählt ihren zugeordneten Anteil
währenddessen weiter. Fehlt eine Einzelmessung, entsteht kein gemessenes
Netzende; ohne gültigen Wallbox-Beleg gelten Bilanz und normale Wolkenfrist.

Bei Luxtronik im Messwertbetrieb kann der PV-Boost eine bereits laufende
Warmwasserbereitung übernehmen und ihr Ziel anheben. Voraussetzung sind eine
frische Verdichterbeobachtung im Betriebszustand Warmwasser, gemessener
Netzbezug höchstens 200 W und keine Akkuentladung. Das Startbudget ist
Einspeisung plus aktuelle gemessene WP-Leistung, genau einmal gezählt.
Da kein neuer Verdichterstart erfolgt, gelten für diese Übernahme weder
Startpuffer noch Wiedereinschaltsperre. Danach gelten Mindestlaufzeit,
Wallbox-Vorrang beim Netzende und das gemessene Netzkonto unverändert.
Bei Heizbetrieb oder fehlenden frischen Daten gilt die normale Startprüfung.
Eine unterbrochene oder veraltete Messreihe setzt die 30-s-Netzfrist zurück;
fehlende Werte werden nicht als 0 W verwendet.

Ein verbrauchtes gemessenes Netzkontingent ist keine Tagesobergrenze und keine
Startsperre. Ein neuer PV-Start braucht dann zusätzlich 500 W Überschuss zum
normalen Startpuffer von 500 W: bei 3.500 W Startleistung also mindestens 4.500 W.
Das verlangt mehr PV-Deckung, reserviert aber keine zusätzliche Last. Ein
erneuter selbstständiger Start der Wärmepumpe bei weiter stehendem PV-Sollwert
erhält die volle Mindestlaufzeit. Bei 2 kW und 30 Minuten können so je Lauf bis
zu 1 kWh zusätzlicher Netzbezug entstehen.
Preis- und Prognosepausen warten bei einem PV-gedeckten Boost bis zum Auftragsende;
ihr eigenes Ende nimmt den PV-Sollwert nicht zurück. Wird im Pausenzyklus ein
WP-Netzanteil oberhalb der Zählschwelle gemessen, endet der Boost nach seiner
Mindestlaufzeit.
Positive Anforderungen wie Warmwasser sofort, manueller Boost, Preis-Boost und
Pre-Dump behalten ihren Vorrang.
Anforderungsflackern ohne anderen Besitzer wird 45 Sekunden entprellt.
Das erreichte Ziel beendet den Auftrag nicht; die WP stoppt selbst.
Reservierungsbetrieb und andere Wärmepumpentypen behalten ihre Regeln.
Mindestlaufzeit und
Wiedereinschaltsperre bleiben wirksam. Der normale WW-Timer ist die
Rückkehrstellung. Erreicht sein Ziel mindestens das PV-WW-Ziel, übernimmt er
den Kanal mit einer Sollwertschreibung.

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

Fallen die Angaben des Speicherreglers vollständig aus (weder Speicherbudget
noch Speicherzustand lesbar), ist auch der Stand der Notstromreserve unbekannt.
Ein in einem eigenen Warmwasser- oder PV-Auftrag nachweislich laufender
Verdichter wird dann zum Schutz des Verdichters bis zum Ende seiner
Warmwasser-, Signalhalte- und Mindestlaufzeit gehalten; neue Starts und
Sollwertanhebungen gibt es in dieser Lage nicht. Ein dadurch kurzzeitig
höherer Netzbezug wird bewusst in Kauf genommen.

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
30 Sekunden zusammenhängend frischer WP-Netzanteil (ohne frische WP-Einzelmessung: gesamter Netzbezug abzüglich 200 W) über 2.500 W) nimmt auch
einen laufenden manuellen Boost zurück. Der SoC-Teil wirkt sofort; der Auftrag bleibt bestehen. Dasselbe gilt bei iDM,
Dimplex und SG-Ready im Notstrom- oder Inselbetrieb.

An der Notstromreserve gibt der Speicher keine Akkuentladung für die Wärmepumpe
frei. Nutzerbefehle wie „Warmwasser sofort“ und der manuelle Boost laufen dann
aus dem Netz, solange der eingestellte Mindest-Ladestand, die oben beschriebene
Sicherheitsabschaltung und der Hausanschluss es erlauben. Ohne frische
WP-Einzelmessung zählt die Sicherheitsabschaltung den gesamten Netzbezug;
weil der Akku an der Reserve nichts deckt, kann sie einen Nutzerbefehl dann
schon bei üblicher Hauslast nach 30 Sekunden beenden.
Ein PV-Auftrag endet an der Reserve; ein laufender Verdichter behält seine
Mindestlaufzeit.

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
startet und hält der Knopf nicht. An der Notstromreserve läuft er aus dem
Netz, solange Mindest-Ladestand, Sicherheitsabschaltung und Hausanschluss es
erlauben. Eine fremde
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

### Zeitbezug der Speicherprognose und Anzeige „Jetzt“

Der Plan beginnt an der vollen Stunde, bilanziert aber ausschließlich die
Zeit ab seiner Erzeugung. Vergangene Slots behalten den Live-SoC des
Planlaufs; der laufende Slot wird anteilig gerechnet. Start- und End-SoC
werden getrennt veröffentlicht und zeitrichtig dargestellt. Die
Ladeverteilung und die ergänzenden Speicherprojektionen verwenden denselben
Zeitbezug. Verbraucherenergie wird dabei jeweils einmal bilanziert.

Karte, Desktop-Kopfzeile, Mobilansicht und Diagramm lesen die aktuelle
Korridorlage und Regelart aus dem Zustand des Speicher-Managers. Die
Regelart beschreibt den finalen Ausgang: freie Regelung, Kurvenführung,
Vorab-Entladung, Direktvermarktung, Entladesperre, manuelle Vorgabe,
aktive Grenze, Preis- oder Reserve-Halt, Netzladen oder Notstrom-/Schutzfall.
Kurvenführung bezeichnet ausschließlich einen Zustand der Ladekurvenregelung;
andere Grenzen übernehmen die Bezeichnung ihres bestehenden Zustands. „E3DC regelt
frei“ erscheint nur bei freigegebenen EMS-Grenzen. Unterhalb des Bands
steht „Regelziel … (Unterkante)“ nur bei Kurvenführung; bei anderen
Regelarten bleibt die Unterkante eine Lageangabe. Fehlende Ausgangsnachweise
ergeben „Regelart unbekannt“, fehlende oder veraltete Korridordaten
„Kein Regelziel verfügbar“. Die Gültigkeit verwendet das Serveralter und
die seit dem Abruf verstrichene Dauer. Die Sollkurve bleibt separat sichtbar.


### Zwei Klimaanlagen messen

Optional einen zweiten Shelly-Zähler unter Klima eintragen: Anzeigenamen,
Adresse, Zählertyp und Phase beziehungsweise Kanal festlegen
(`climate_meter2_name`, `climate_meter2_ip`, `climate_meter2_type`,
`climate_meter2_phase`). Die Adresse leer oder auf `0.0.0.0` lassen, um nur den
bisherigen Zähler zu verwenden. Die Aktivierung bleibt an Klima-Messung und
Zähler 1 gebunden. Für einen EM Mini Gen4 wird immer `em1:0` gelesen; beim
Shelly EM Gen1 Kanal 0, Kanal 1 oder Summe wählen. Bei derselben Adresse
überlappende Messkanäle vermeiden; der Validator weist beratend darauf hin.

Dashboard, mobile Ansicht und Klima-Seite zeigen die Gesamtleistung „Klima“
und die Messwerte beider Anlagen mit ihren Namen. Leistung und Energie werden
getrennt geprüft. Bei einem fehlenden oder veralteten Leistungswert ist die
Summe unbekannt; der andere Zähler bleibt sichtbar. Der Hausanteil enthält dann
Klima („Haus inkl. Klima“), die Aufteilung ist unvollständig. Klimaleistung
steckt bereits im gemessenen Hausverbrauch und wird im Regelungsbudget nicht
erneut abgezogen. Für die getrennte Anzeige wird die gültige Summe einmal aus
dem Hauswert herausgelöst.

Nur messen: Der zweite Zähler erhält keine Steuerfunktion. Der vorhandene
Toshiba-Status verwendet weiterhin ausschließlich Zähler 1, auch beim Ausfall
des zweiten Zählers. Elektrische Details und die Frequenz bleiben je Messpunkt;
eine Frequenz-Ersatzquelle stammt weiterhin allein von Zähler 1.

Tagesenergie pro Messpunkt aus dessen Dauerzähler ab dem ersten gültigen Stand
des Tages bestimmen, dann summieren. Bestehende Tagesbasen bleiben erhalten.
Bei Neustart, Deaktivieren und erneutem Aktivieren am selben Tag bleibt die
Basis desselben Messziels erhalten; der Dauerzähler erfasst auch den Verbrauch
während der Unterbrechung. Ein neues Messziel oder ein zurückgesetzter Zähler
beginnt mit einer neuen Basis, ebenso der erste gültige Wert nach Mitternacht.
Alte Dauerzählerstände niemals als heutigen Verbrauch übernehmen. Fehlende
Energie bei gültiger Leistung als unbekannt anzeigen. Beim Entfernen von
Zähler 2 wieder ausschließlich Zähler 1 anzeigen.

Die Live-Historie speichert neue Zeilen zusätzlich mit der Aufteilung je
Anlage und ihrer Gültigkeit; die Klima-Verlaufansicht zeigt beide Anlagen als
eigene Kurven. Alte Zeilen behalten ihren bisherigen Gesamtwert und bleiben
unverändert. Die Statistikdatenbank speichert neue Tagesaufteilungen je
Zähleridentität mit dem damaligen Anzeigenamen und Qualitätsgrund. Monate und
Jahre werden daraus gebildet; die Langzeitansicht zeigt bei eingerichtetem
zweiten Zähler zusätzliche Zeilen je Anlage. Der Gesamtverbrauch bleibt für
Hausbilanz und Kosten maßgeblich. Telegram und Kostenansicht führen weiterhin
nur die Summe.

Nach Mitternacht ergänzt das Archiv die Einzelaufteilung aller noch offenen
Messtage in zeitlicher Reihenfolge. Die bestehenden Haus-, Kosten- und
Summenwerte werden dabei nicht neu berechnet. Nur ein gültiger Zählerstand aus
den letzten zwei Minuten vor Tagesende bestätigt den endgültigen Abschluss.
Unvollständige Tage bleiben für verspätet gespeicherte Messungen offen; ihre
bestätigte Teilenergie wird dabei nicht verringert. Nach Ablauf der
JSONL-Aufbewahrung von 90 Tagen werden sie als unvollständig verriegelt.
Vollständige Tage bleiben unverändert. Fehlende Einzelenergie bleibt unbekannt. Neue Messzeilen speichern einen eindeutigen Zeitanker für die
Zeitumstellung. Das Entfernen aller Zähler wird als Konfigurationsunterbrechung
festgehalten; eine Wiederaktivierung macht diesen Tag nicht vollständig.

Ein umbenannter Zähler behält seine Identität; abgeschlossene Tage behalten
ihren gespeicherten Namen. Ein anderer Messkanal oder eine andere Zähleradresse
beginnt eine neue Reihe. Angebrochene Messtage zeigen die gemessene Teilmenge
mit Kennzeichnung. Ein Zählerreset oder Entfernen und Wiederaktivieren während
eines Tages macht dessen Einzelenergie unbekannt; sie wird nicht aus der Summe
rekonstruiert. Historische Zeiträume ohne Einzelmessungen bleiben unbekannt,
insbesondere die zweite Anlage vor ihrer Einrichtung. Die JSONL-Aufbewahrung
bleibt auf 90 Tage begrenzt; dauerhafte Monate benötigen diese Dateien nicht.

### Ausfall einer externen Wallbox

Fehlt die gültige Wallboxmessung (Frischegrenze 15 Sekunden), regelt der
Speicher wie ohne diese Wallbox: ohne Wallbox-Entladedeckel, Budgetreservierung
oder Übernahme alter Lade-Latches. Der gemessene Hausverbrauch enthält die
externe Wallbox weiterhin vollständig und wird nicht nochmals abgezogen.
Andere gültige Wallboxen und alle übrigen Schutzgrenzen bleiben wirksam.
Bei gültiger Rückkehr beginnt die normale Wallbox-Logik mit der aktuellen
Messung. Ausgefallene Ladepunkte erhalten keine gewöhnlichen Steuerbefehle. Schutzbefehle
für Nutzer-Aus, Hausanschluss-Überlast, Notstrom und das Ladefensterende bleiben
bei antwortendem Schreibkanal wirksam und ausdrücklich als Schutz gekennzeichnet.

Dashboard-Badge und Wallbox-Kachel warnen bis zur Rückkehr gültiger Daten.
Bei reduzierter Bewegung bleibt die Hervorhebung ohne Animation sichtbar.
Externe Lasten können im Config-Editor unter **Tarif & Strompreise → Geplante
Lastfenster** abgesichert werden.

### Wallbox-Regelkern

Jeder konfigurierte, nicht deaktivierte Ladepunkt verwendet denselben
Regelkern. Die Betriebsart bleibt je Ladepunkt eingestellt; eine zusätzliche
Reglerauswahl gibt es nicht. Der Rückweg nach einem Update ist das Backup.

Der Speicher veröffentlicht das absolute Gruppenbudget in zwei Teilen:
`budget_pv_w` aus PV nach festen Lasten und seinem eigenen Ladeanspruch sowie
`budget_battery_w` als Rest von `budget_w`. Laufende Akkuladung und die eigene
Wallboxleistung werden davon nicht erneut abgezogen. Externe Wallboxen stecken
im Hauswert, native E3DC-Wallboxen haben einen eigenen Messpunkt.

Die Hausbasis der Speicherregelung (Abzug externer Wallboxen, Rückfall bei
unbekanntem Typ oder ungültiger Messung) beschreibt
`doc/Speicher_Ladesteuerung_Ablauf.md`, Abschnitt „4. Verbraucherbudget“.

Je Ladepunkt und Zyklus gilt eine Anteilsquelle: Netz bei ausdrücklicher
Netzfreigabe; Akku in „PV + Akku bis Untergrenze“, außerhalb des Netzfensters
in „Sofort bis Preislimit“ und im Abfahrtsfenster in „Akku bis Abfahrt“, jeweils
mit gültigem SoC, offener Untergrenze und Speicherautorisierung; sonst PV.
Netzboxen erhalten ihren zulässigen Höchststrom ohne Verbrauch des
Speicherbudgets. Die frisch gemessene Netzboxleistung wird genau einmal vom Gesamtbudget
abgezogen. Bei unbekannter oder mehr als 15 Sekunden alter Netzboxmessung
erhalten Akkuboxen höchstens den PV-Teil. Der PV-Rahmen bleibt unverändert.
Akkuboxen teilen zuerst das so bereinigte Gesamtbudget; PV-Boxen bekommen
den PV-Rest nach diesen Anteilen. Innerhalb einer Quelle gilt die eingestellte
Verteilpriorität. Reicht der Anteil nur für eine bereits ladende Box, behält
die Vorrangbox, sonst die länger ladende Sitzung ihren Anteil. Ein Rest unter
der Mindestleistung geht an den nächsten geeigneten Ladepunkt.

Der elektrische Zuteiler prüft danach Geräte- und Fahrzeuggrenzen,
Phasenlage und Hausanschluss je Phase. Im 10-Sekunden-Takt folgt der Strom
direkt dem Anteil, abwärts auf das Geräteraster gerundet. Ein Totband von 1 A
verhindert Änderungen bei kleinen Budgetschwankungen; es gibt keine
Anhebungstreppe und keine zusätzliche Bestätigungszeit.

Ein PV- oder Akkustart braucht 60 Sekunden ausreichenden Anteil; eine freigegebene Netzquelle startet ohne diese Wartezeit. Kurze Unterdeckung führt
zum Mindeststrom, solange Stützung autorisiert ist: bei „PV-Kurve ruhig“ nur
aus dem Wolken-Kontingent, bei „Grundladung stabil“ nur solange die bestehende
Grundladungsbedingung gilt, bei Akkubetriebsarten nur oberhalb der Untergrenze.
Haltestützung erlaubt keinen Start aus fehlender Quelle. Nach drei Minuten
Unterdeckung und mindestens fünf Minuten Ladung endet die Sitzung regulär;
danach benötigt ein neuer PV- oder Akkustart erneut 60 Sekunden ausreichenden Anteil. Diese Unterdeckungsregel gilt
auch ohne Stützungsfreigabe. Nutzer-Aus und Schutzstopps wirken sofort.

Die Netzbremse zählt nur unerlaubten Bezug: Netzpunkt minus frisch gemessene
Leistung der Netzboxen. Fehlt eine Messung oder ist sie älter als 15 Sekunden,
gilt der rohe Netzwert. Zwei Regeltakte über 300 W senken den Strom ab.
Solange eine Netzbox nach Start, Anhebung oder Phasenwechsel noch nicht
quittiert ist, zählt ihr Einschwingen für die anderen Boxen nicht als
Bezugstakt; nach eigenem Ausgang gilt dieselbe Quittungsregel. Netzboxen selbst
haben keine Netzbremse. Nach einer Korrektur
braucht eine erneute Anhebung 30 Sekunden Beruhigung unter 300 W.

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

Auch eine auf 16 A begrenzte Wallbox kann bei ausreichendem dreiphasigem
Anteil aufsteigen. Die vorhandenen Gerätesequenzen, Reservierungen und
frischen Rücklesebelege bleiben verbindlich. Nutzer-Aus, Notstromreserve,
Hausanschluss, Datenvalidität und sichere Hardwarekommandos haben Vorrang.
Eine native Stoppflanke braucht den frischen Zustand „nicht abgebrochen“. Fehlt das Bit, gilt der einmalige Rückfall mit frischem Ladebeleg. Im Fehlerzustand „Box reagiert nicht auf den Stopp“ erhalten Pause, Ladepunktsperre, Not-Aus und Hausanschluss gemeinsam genau eine zusätzliche Stoppflanke: frühestens 60 Sekunden nach dem letzten Stoppversuch, bei frischer Probe mit „nicht abgebrochen“ oder unbekanntem Bit und nur mit frischem Ladebeleg. Die Fehleranzeige erscheint ab Fehlerbeginn. Die zusätzliche Einmalflanke ist höchstens einmal je Stecksession und Dienstlauf zulässig; ist sie verbraucht oder für den Stoppgrund nicht berechtigt, bittet die Oberfläche, den Ladepunkt zu prüfen. Nach einem Neustart des Dienstes steht sie einmal neu zur Verfügung. Eine Stecksession endet erst mit bestätigtem Abstecken: mindestens drei aufeinanderfolgende frische Proben ohne gestecktes Fahrzeug über mindestens zehn Sekunden. Insgesamt sind höchstens drei Versuche je Ereignis zulässig; eine bereits verbrauchte Einmalflanke wird durch einen späteren Nutzer-Stopp nicht erneuert.

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

Der Kaltstart im freien AUTO-Betrieb nimmt Überschuss während der
60-Sekunden-Startqualifizierung im Speicher auf. Im Kurvenladezustand bleibt
die gewollte Kurven-Ladegrenze wirksam: Bei 5 kW Überschuss wurden bis zum
Ladebeginn rund 89 Wh Export gemessen. Die Grenze von 20 Wh gilt für freien
AUTO-Betrieb; sie ist für Kurvenladung keine Zusage.


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
