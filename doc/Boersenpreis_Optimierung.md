# Börsenpreis-Optimierung und Preis-Boost

Diese Dokumentation trennt bewusst zwei Dinge, die in der Praxis oft vermischt
werden:

1. **Negativpreis-/Preis-Boost:** aktives Geld- oder Kostenvorteilsfenster,
   explizit freigegeben durch den Nutzer.
2. **Normale Speicherstrategie:** Ladekurve, Prognose, Pre-Dump und Autonomie
   ohne Raten unbekannter Großlasten.

## 1. Netzdienlicher Eco-Modus

Der netzdienliche Eco-Modus kann auch bei statischem Tarif aktiviert werden. Er
nutzt Marktpreise, um günstige und teure Stunden in den Eco-Score einzusortieren
und Verbraucher netzdienlicher zu planen.

Wichtig: Der Eco-Modus ändert nicht automatisch den angezeigten Vertragspreis.
Er stellt nur Preis- und Score-Daten für Speicher, Wallbox und Wärmepumpe bereit.

## 2. Negativpreis-/Preis-Boost

Der Boost-Block im Strompreisbereich ist ein eigener Regelkreis. Er ist für
Nutzer gedacht, die bei sehr günstigen oder negativen Preisen bewusst Leistung
ziehen wollen:

- Speicher laden,
- Wallbox starten,
- Wärmepumpe oder Heizstab freigeben,
- vorher Speicherplatz lassen, wenn später günstiger Netzstrom erwartet wird.

Die Preisgrenze (`cheap_grid_price_limit_ct`, im Config-Editor „Preisgrenze
(Legacy)“) kann die Freigabe nur verschärfen: Der Boost wird nur bei negativem
Abrechnungspreis aktiv, also unter 0 ct/kWh. `0` bedeutet „jeder negative
Preis“; ein negativer Wert verlangt mindestens diesen Preis (zum Beispiel `-2`
erst ab −2 ct/kWh). Positive Werte öffnen den Boost nicht.

Dieser Block ist nicht die allgemeine Antwort auf unbekannte hohe Hauslasten.
Er arbeitet nur mit freigegebenen Verbrauchern und klarer Preisgrenze.

Seit der Marktpfad-Härtung gilt zusätzlich: Preiswechsel allein erzeugen keinen
Speicher-Owner. Wenn PV-Prognose, aktueller SoC und vorhandene Reserven den
kommenden Bedarf decken, bleibt die normale Ladekurve führend. Der Preis-Boost
wird erst aktiv, wenn daraus ein plausibler, freigegebener Lade- oder
Halteauftrag entsteht.

## 3. Preis-Mindestmarge / Mindest-Preisvorteil

Die **Preis-Mindestmarge** (`market_min_margin_pct`) ist der Mindest-Preisvorteil,
den eine Netzladung des Speichers nach Wirkungsgrad, Akkukosten und
Sicherheitskorrektur bringen muss. Der Wert verhindert, dass das System wegen
winziger Preisdifferenzen unnötige Ladezyklen erzeugt.

Faustregel:

- 10 Prozent (Standard und Vorgabe des Ladeprofils „Wirtschaftlich“) decken
  typische Lade-/Entladeverluste und kleine Preisunsicherheiten ab,
- höhere Werte bevorzugen nur die wirklich günstigsten Stunden,
- niedrigere Werte reagieren aggressiver auf kleinere Preisunterschiede; die
  Profile „Ausgeglichen“ und „Komfort“ setzen 0 Prozent.

Das Feld ist nur im Ladeprofil „Eigene Einstellungen“ frei; die übrigen Profile
setzen es fest (Abschnitt 5, „Ladeprofil“). Für Octopus-Heat- oder EPEX-Nutzer
ist dieser Wert zugleich die Preistoleranz dafür, ob nur der günstigste Bereich
genutzt wird oder auch angrenzende Stunden als Folgefenster mitlaufen dürfen.

Das ältere Feld **Preis-Reserve (%)** (`awreserve`) wird von den aktuellen
Reglern und vom Webportal nicht ausgewertet.

## 4. Was die Preislogik nicht errät

E3DC-Control versucht nicht, aus unbekanntem Hausverbrauch automatisch auf eine
externe Wallbox, eine Fritteuse, ein Kochfeld oder eine andere Großlast zu
schließen.

Das ist eine bewusste Regel:

- Eine 11-kW-Dauerlast kann ein BEV sein, muss es aber nicht.
- Ein großer Speicher kann solche Lasten mehrere Stunden stützen, ein kleiner
  Speicher nicht.
- Ohne bekanntes Ende der Last ist nicht sicher berechenbar, ob das Halten des
  Speichers besser ist als Eigenverbrauch.
- Ein automatisches "Akku schonen bei hoher Hauslast" würde echte
  Eigenverbrauchsvorteile zerstören und könnte im Alltag falschen Netzbezug
  erzeugen.

Wenn eine Last regelrelevant sein soll, muss sie eingebunden oder geplant sein:

- Wallbox über nativen Treiber, openWB/evcc-Messwert oder Ladeplan,
- Wärmepumpe über Energy Manager, SG-Ready, Hersteller-API oder Messwert,
- Heizstab/Shelly über eigene Freigabe.

## 5. Speicher und günstige Preisfenster

Der Storage Simulator gibt die Ladekurve vor. Der Storage Manager darf
Preisfenster berücksichtigen, wenn alle Bedingungen passen:

- Preis-/Boost-Funktion ist aktiviert,
- Preisvorteil reicht inklusive Mindest-Preisvorteil,
- Ziel-SoC oder Boost-Ziel ist noch nicht erreicht,
- keine höhere Schutzlogik blockiert den Eingriff,
- Ladung aus Netz ist ausdrücklich erlaubt.

Bei Tariftypen mit festem Zeitfenster (z. B. Octopus Heat) ist das konfigurierte
Tarifzeitfenster die Regelgröße, nicht der Börsenpreis. Ein preisbasierter
Netzladevertrag des Speichers gilt bis zum Ende des günstigen Fensters, nicht
nur bis zum Ende des laufenden 15-Minuten-Slots. Über die gewählten günstigen
Slots verteilt der Speicher die nötige Energie gleichmäßig. PV-Überschuss
lädt ungedrosselt; das Netz ergänzt bei Bedarf die sanfte Laderate. Ist das Ziel
erreicht (Hysterese 1 SoC-Punkt, `market_target_hysteresis_pct`; kleinere Werte
werden auf 1 angehoben), wird im selben Fenster nicht erneut aus dem Netz
geladen: Der Speicher bleibt bis zum Fensterende entladegesperrt, PV lädt
weiter. Für den Marktpfad freigegebene Verbraucher (Wallbox über
`market_wallbox_enable`, nur für Ladepunkte in `Sofort bis Preislimit`;
Heizstab über `market_heater_enable`) bleiben bis zum Fensterende
freigegeben. Die Wärmepumpe gibt der normale Marktpfad nicht frei; sie
behält ihre Prognose-, PV- und Pre-Dump-Führung mit Taktschutz. Nur der
Negativpreis-Boost (Abschnitt 2) kann sie gesondert freigeben.

Beim Halten werden nachweisbare Nachladungen in günstigen Preisfenstern
berücksichtigt, sobald sie nach Wirkungsgrad, Akkukosten und Sicherheitskorrektur
einschließlich Mindestabstand und Mindestmarge günstiger sind als der aktuelle
Bezug. Der Speicher wird dann nur so lange gehalten, bis Bestand und rechtzeitig
nachweisbares Nachladen den teuren Bedarf decken. Das gilt auch für die Profile
„Wirtschaftlich“ und „Ausgeglichen“ bei Börsentarifen mit Preisunterschieden sowie
für „Komfort“. Der Teil eines Netzladeauftrags, der den Puffer lädt, zählt nicht
als Nachladen; im Komfort gibt es keinen Puffer. Beispiel: Bei 85 % Wirkungsgrad,
4 ct/kWh Akkukosten und 24,91 ct/kWh im günstigen Fenster kostet nachgeladene
Energie rund 33,3 ct/kWh; liegt der aktuelle Bezugspreis darunter, wird das
Nachladen nicht angerechnet. Eine negative Sicherheitskorrektur senkt diese
Ersatzkosten rechnerisch und kann Umladen auch dann auslösen, wenn es tatsächlich
teurer ist. Ein Prognosefehler vor dem ersten Nachladen ist nicht abgedeckt. Wird
ein eingeplantes Nachladen nicht ausgeführt, etwa weil E3DC-Control oder die
Verbindung zum E3DC ausfällt, fehlt dieser Ersatz, und in teuren Stunden kann
Netzbezug entstehen.

Der Netzladebedarf wird zeitgerichtet bestimmt: Er reicht nur bis zum nächsten
nutzbaren günstigen Preisfenster (gleicher Tarifpreis oder höchstens um die
Mindestmarge teurer); was dieses Fenster physisch liefern kann, wird
angerechnet. Die für das laufende Fenster erwartete PV-Erzeugung wird zuerst
berücksichtigt; aus dem Netz wird nur der Rest geladen, der zum Fensterende
noch fehlt, gedeckelt auf den freien Raum bis zum Ladeziel. Das Netzladen
verteilt sich über das gewählte Fenster, mit einem Ziel etwa 30 Minuten vor
Fensterende. PV deckt die Laderate zuerst; überschreitet der Überschuss die
Rate, lädt PV den Speicher im Automatikmodus ungedrosselt. Ladeziel und
Netzladejob leiten sich aus dem Sollbestand am
Fensterende ab: dem Bedarf bis zum nächsten nutzbaren Fenster zuzüglich der
Puffer. Beide bleiben während des Fensters stabil, auch wenn der Speicher
zwischenzeitlich aus PV oder Netz lädt; deckt der erwartete Bestand den
Sollbestand, wird nicht aus dem Netz geladen. Während des laufenden Fensters
wird die Hauslast nicht vom Speicherbestand abgezogen, weil der Speicher dort
nicht entlädt. Für die Ladedauer wird die gemessene Ladeleistung je Ladezustand verwendet.
Eine Messung zählt erst nach einer Minute mit gleichem Netzladebefehl und
weniger als 200 W PV-Leistung. Voll angenommene sanfte Laderaten senken die
Prognose nicht; Unterlieferung und Raten ab der bisherigen Prognose liefern
neue Erkenntnisse über die Ladefähigkeit. Die gelernte Ladeleistung bleibt
bei einem Neustart des Dienstes erhalten und verfällt nach sieben Tagen.
Nach einem Neustart des Geräts beginnt die Prognose wieder mit den Startwerten. Im günstigen Fenster lädt der Speicher gleichmäßig mit der
nötigen Leistung und erreicht das Ziel etwa 30 Minuten vor Fensterende. Bei
Fenstern unter einer Stunde bleibt die halbe verfügbare Zeit als Puffer.
Die Planung lernt die tatsächliche Ladeleistung je Ladezustand, auch die
Drosselung des Speichers kurz vor voll. Liegt das Laden zurück, steigt die
Leistung bis zur Gerätegrenze. PV-Überschuss wird nie gedrosselt; vor dem
Netzladestart bleibt die Entladung für den Hochtarif zurückgehalten.
Gerätegrenze, „Max. Laden“, Hausanschluss und die übrigen harten Grenzen bleiben
wirksam. Es zählt nur die Fehlmenge, deren späterer Bezugspreis
mindestens die effektiven Ladekosten erreicht (mittlerer Preis des Fensters
durch Wirkungsgrad plus Akkukosten plus Sicherheitskorrektur); als günstiges
Folgefenster gelten nur vom Ladeprofil erkannte, ausführbare Ladefenster, deren
Preis höchstens um die Mindestmarge über dem mittleren Preis des laufenden
Fensters liegt. Eine Gutschrift setzt vollständige Prognosen und Preise,
Ladefreigabe, verfügbare Leistung, freien Speicherraum und ausreichende Restzeit
einschließlich Sicherheitszeit voraus. Der spätere Auftrag muss seinen Bedarf
unabhängig von seiner eigenen Gutschrift belegen und das Startminimum an
Netzenergie erreichen. Ein positiver Rest unter dem Minimum bleibt als kleiner
Auftrag sichtbar und bedeutet keine vollständige Bedarfsdeckung. Für das
Ladeziel wird zusätzlich der notwendige Bestand ohne anfänglichen Vorrat
berechnet. Diese Vergleichsrechnung und die aus dem tatsächlichen Vorrat
geplante spätere Netzladung werden getrennt ausgewiesen; ein bereits höherer
Ladezustand verschiebt dadurch nicht das Ziel. Ein frisch nachgewiesener,
laufender Auftrag darf im selben Fenster auch unter dem Startminimum bis zum
Ziel weiterlaufen, spätestens bis zum Fensterende. Die Sicherheitszeit wird
für neue Starts und für die Zukunftsgutschrift abgezogen. Sie begrenzt die
als lieferbar angerechnete Energie zusätzlich zum Zielpuffer von 30 Minuten;
die Zeitabstände werden für die Zielzeit nicht addiert. Dieser mittlere Preis wird beim
ersten Plan im Fenster über alle zusammenhängenden Fensterslots gebildet und
bis zum Fensterende beibehalten; er wandert bei Börsentarifen nicht mit den
ablaufenden Slots, sodass Ladeziel und Netzladejob im Fenster stabil bleiben.
Nach erreichtem Ziel löst der bloße Verlust des Folgefensters (die
Bedarfsspur springt ohne neue Preisdaten zu einem späteren Fenster) keinen
zweiten Netzladestart im selben Fenster aus; wächst dagegen die Preisliste
(neu veröffentlichte Börsenpreise) und damit der echte Bedarf bis zum
nächsten günstigen Fenster, darf im laufenden Fenster nachgeladen werden.
Hinweis: Eine negative
Sicherheitskorrektur (`market_safety_correction_ct_per_kwh`) kann auch
Grundtarifstunden rechnerisch lohnend machen und erhöht damit das
Ladevolumen. Reicht das Ziel bis 100 %, weil der Bedarf bis zum nächsten
Fenster groß ist, kann danach PV eingespeist werden – das ist physikalisch
unvermeidbar. Solange der Speicher im Fenster gehalten wird, bezieht das Haus
seinen Verbrauch zum günstigen Preis aus dem Netz statt aus dem Speicher.

Bei Spezialtarifen mit Tagesplan und bei Octopus Heat werden günstige
Fenster aus den vollständig konfigurierten Tarifstufen bestimmt. Bei zwei
Stufen ist nur die untere günstig; Zwischenstufen zählen nur dann dazu, wenn
der Abstand zur höchsten späteren Stufe Wirkungsgrad, Akkukosten,
Sicherheitskorrektur und Mindestmarge deckt. Ein einheitlicher Tarif erzeugt
kein künstliches Preisfenster. Fehlende, ungültige oder widersprüchliche
Stufen geben keine Netzladung frei. Fensterende und Referenzpreis bleiben
innerhalb des Tarifsegments gebunden; eine Tarifänderung bindet die Referenz
neu. Prognosebedarf, Zielhysterese und Schutzgrenzen gelten weiterhin.
Börsentarife und die Verkaufspreisbewertung der Direktvermarktung behalten
ihre eigene Klassifikation. Die Entscheidungsvorschau liest beim Spezialtarif
den Tagesplan; leere Preisfelder gelten als nicht gesetzt. Alle Zwischenstufen
werden in der Vorschau einzeln bewertet. Ungültige Spezialtarif-Zeitpreise
oder fehlende Octopus-Pflichtpreise erzeugen eine beratende Warnung beim
betroffenen Feld; die Eingaben bleiben erhalten. Bei negativen Börsenpreisen
bildet die Entscheidungsvorschau die Regelung nur näherungsweise ab und weist
im Vorschauzeitraum darauf hin; der gesonderte Negativpreis-Boost ist nicht
enthalten. Für eine Tarifart, die der Marktpfad nicht unterstützt, zeigt sie
keine Entscheidung. Im Komfortprofil beendet
jeder Preiswechsel das bisherige Tarifsegment, auch wenn die nächste Stufe
noch unter dem Preislimit liegt. Dort wird ein eigener Ladejob mit neuem
Fensterende und eigenem Referenzpreis berechnet; das Komfortziel bleibt gleich.

Ohne belegte physische Ladeleistung entsteht kein Netzladeauftrag. Die
Marktobergrenze begrenzt eine frische Geräte-, BMS- oder eingestellte Nutzergrenze
nur nach oben. Die Mindestmarge wird am gewichteten Nutzen der tatsächlich
versorgten Defizitslots geprüft; ein einzelner Spitzenpreis reicht nicht.
Ein reiner Zielpuffer ohne belegten Mengennutzen eröffnet keinen neuen Auftrag.
Die Preisplanung bewertet die Frische des Livebilds und seiner Gerätegrenzen
zum Lesezeitpunkt. Ihre anschließende Rechenzeit zählt nicht zum Messalter;
bereits beim Lesen veraltete oder ungültige Proben geben keine Netzladung frei.
Die bestehenden Altersgrenzen und die Toleranz für Uhrabweichungen bleiben gleich.
Frisch nachgewiesene laufende Aufträge behalten ihren Fortsetzungsvertrag.
Schließt die Mengenprüfung einen Rest nach bereits erfolgter Netzladung, bleibt
der Speicher im selben Fenster gehalten; daraus entsteht kein neuer Start.

### Kosten und Mindestmenge der Speicher-Netzladung

Im Config-Editor unter **Tarif & Strompreise → Details zur Speicher-Netzladung**
sind folgende profilunabhängige Werte einstellbar. Die Mindestmenge beträgt
standardmäßig 5 % der nutzbaren Kapazität ohne festen Sockel: bei 11,7 kWh
585 Wh, bei 35 kWh 1 750 Wh. Gespeicherte eigene Werte bleiben erhalten:

| Parameter | Schlüssel | Standard ohne Bestandswert | Sinnvoller Bereich |
|---|---|---|---|
| Akkuverschleiß | `market_degradation_ct_per_kwh` | 4 ct/kWh | mindestens 0 |
| Speicher-Wirkungsgrad (Laden und Entladen zusammen) | `market_roundtrip_efficiency_pct` | 85 % | 1 bis 100 % |
| Mindestmenge je Netzladeauftrag | `market_grid_charge_min_job_wh` | 0 Wh | mindestens 0 |
| Kapazitätsanteil der Mindestmenge | `market_grid_charge_min_job_pct` | 5 % | 0 bis 100 % |

Fehlende oder leere Werte für Verschleiß und Wirkungsgrad verwenden weiterhin
`direct_marketing_degradation_ct_per_kwh` beziehungsweise
`direct_marketing_roundtrip_efficiency_pct`, sofern dort ein Wert gesetzt ist,
sonst den Standard. Ausdrückliche Werte einschließlich 0 bleiben erhalten;
0 % Wirkungsgrad wird in der Planung auf 1 % begrenzt. Leere Eingabefelder
zeigen den wirksamen Wert und seine Herkunft. Bloßes Öffnen oder Speichern
anderer Felder trägt keine abweichenden Standards ein. Bereinigung und Import
erhalten die vier Parameter. Unplausible Werte erzeugen beratende Warnungen,
keine Speichersperre; die Planung begrenzt Werte außerhalb ihres Bereichs.

**Mindestmenge = max(Wh-Wert, Kapazität × Prozent/100).** Kleinere Ladeaufträge
werden nicht ausgeführt. Es gilt die **aus dem Netz bezogene Energie je Auftrag**,
nicht die nach Wandlungsverlusten gespeicherte Menge. Bei 40 kWh Kapazität und
5 % beträgt das Minimum 2 000 Wh, auch bei einer Wh-Mindestmenge von 0.
Nur wenn beide Mindestwerte 0 sind, ist das Minimum aufgehoben. Die Schwelle
vermeidet kleine Ladeaufträge; ihre Einstellung löst selbst keine Ladung aus.

Die Ladekosten je später nutzbarer Kilowattstunde berücksichtigen den
Bezugspreis geteilt durch den Wirkungsgrad, zuzüglich Akkuverschleiß und
Sicherheitskorrektur. Die Mindestmarge kommt anschließend hinzu. Diese
Parameter ändern keine Freigaben oder Schutzgrenzen. Alle vier bleiben bei
jedem Profilwechsel unverändert; die bestehende Profilwirkung auf Marge,
Sicherheit, Mindestpreisabstand, Autarkie und Komfort bleibt erhalten.
Speichern ändert nur die Konfiguration. Die Parameter starten oder installieren
keinen Dienst; Einrichtung erfolgt über die Installationszentrale.

### Ladeprofil

Das Ladeprofil (`market_charge_profile`) legt fest, wie viel Netzstrom der
Speicher in günstigen Preisfenstern aufnehmen darf. **Wirtschaftlich** lädt
nur, wenn die Prognose ein Defizit zeigt und der spätere Bezug mindestens 10 %
teurer ist als die Ladekosten (Preis durch Wirkungsgrad plus Akkukosten).
**Ausgeglichen** lädt bei Defizit ohne Margenaufschlag. **Komfort** lädt im
Preisfenster bis zum Zielstand (höchstens „Speicher max.“), sobald der
Abrechnungspreis unter dem Komfort-Preislimit (`market_price_limit_ct`) liegt –
auch ohne Defizit; erwartete PV bis zum Fensterende wird zuerst angerechnet,
aus dem Netz kommt nur der Rest, und zwar am Ende des Fensters. Ohne eigenes
Limit gilt der Mittelpreis des Tarifs (Octopus Heat aus den festen
Tarifzeiten) beziehungsweise des gebundenen Preishorizonts. **Eigene
Einstellungen** gibt Marge und Sicherheitskorrektur frei. In jedem Profil
gelten Notstromreserve, Hausanschlussgrenze, Reserven und das Ladeziel;
fehlende Preis- oder Prognosedaten führen nie zu einer Freigabe. Beim Update
wird das Profil aus den vorhandenen Werten gesetzt: Standardwerte →
Wirtschaftlich, abweichende Werte → Eigene Einstellungen; das Verhalten ändert
sich dadurch nicht. Die Preisgrenze des Negativpreis-Boosts
(`cheap_grid_price_limit_ct`) bleibt davon getrennt.

Wirksame Schlüssel:

| Schlüssel | Wirkung |
|---|---|
| `market_charge_profile` | Ladeprofil: `economic`, `balanced`, `comfort`, `custom` |
| `market_price_limit_ct` | Komfort-Preislimit in ct/kWh; leer = Mittelpreis des Tarifs |
| `cheap_grid_battery_max_soc` | Komfort-Ziel „Speicher max.“ (Standard 80 %) |
| `market_min_margin_pct` | Mindestmarge (vom Profil gesetzt, frei bei Eigene Einstellungen) und Preistoleranz für das nächste Fenster |
| `market_safety_correction_ct_per_kwh` | Sicherheitskorrektur (vom Profil gesetzt, frei bei Eigene Einstellungen) |
| `market_autarky_horizon_buffer_wh` | Prognosepuffer im Sollbestand am Fensterende und in der Autarkieprüfung (Standard 500 Wh) |
| `market_target_hysteresis_pct` | Zielhysterese, mindestens 1 SoC-Punkt |
| `market_late_fill_safety_min` | Zeitabzug für die lieferbare Energie neuer Jobs und die Zukunftsgutschrift (Standard 15 min), zusätzlich zum 30-min-Zielpuffer; verschiebt die Zielzeit nicht nochmals |
| `market_late_fill_buffer_pct` | Puffer im Sollbestand am Fensterende, in Prozent der Speicherkapazität (Standard 3 %) |
| `maximumladeleistung` / `market_battery_max_w` | Obergrenze für den Ladebefehl; die Planung berücksichtigt die gelernte Ladefähigkeit je Ladezustand und holt bei Bedarf bis zur Grenze auf |

Netzladen wird also nicht durch den Morgenpuffer allein ausgelöst. Der
Morgenpuffer ist eine PV-/Autonomiegröße. Netzladen bleibt ein eigener
Preis-, Unwetter- oder manueller Pfad.

Die Strompreislinie im Verlaufsdiagramm wird slotgenau dargestellt: Ein neuer
15-Minuten-Preis beginnt exakt am Slotanfang. Zwischen zwei Preiswerten wird
kein Zwischenpreis interpoliert; der Hover zeigt deshalb nur echte Slotpreise.

## 6. Eigenverbrauch statt Netz-Arbitrage

Reine Arbitrage - günstig laden und teuer einspeisen - ist bei Heimspeichern
selten attraktiv, weil Netzentgelte, Steuern, Ladeverluste und Batteriealterung
den Spread auffressen.

Der wirtschaftlichere Fall ist meist:

```text
günstig laden -> später teuren Netzbezug vermeiden
```

Dabei spart der Nutzer nicht nur den Börsenpreis, sondern auch die Nebenkosten
des teuren Bezugs. Trotzdem bleibt es ein Opt-in, weil nicht jede Anlage und
nicht jeder Tarif davon profitiert.

## 7. Winter und schlechte Prognose

Bei schlechter PV-Prognose darf das System den Speicher weniger stark bremsen
und günstige Preisfenster gezielt nutzen. Aber auch hier gilt:

- bekannte geplante Verbraucher dürfen berücksichtigt werden,
- unbekannte Dauerlast wird nicht geraten,
- Notstromreserve und harte Schutzgrenzen bleiben vorrangig,
- teures Netzladen in der Nacht wird nicht durch einen vorherigen, unklaren
  Hauslastfall automatisch provoziert.

## 8. Diagnose

Relevante Dateien:

| Datei | Bedeutung |
|---|---|
| `epex_daten.json` | Rohpreise und Zeitfenster |
| `eco_score.json` | Nutzerpreis, Marktpreis, Eco-Score |
| `price_boost_plan.json` | erkannte Boost-Fenster |
| `storage_manager_state.json` | aktueller Preis-/Speicherzustand |
| `storage_plan.json` | Speicherplan des Storage Simulators mit Marktplan (`market_plan`), Netzladejob und Fensterdiagnose (`grid_charge_window_fill`) |
| Wallbox-Entscheidungen | Preisfreigabe, Ladeplan, Netzfreigabe |

Wenn ein System bei günstigem Preis nicht lädt, zuerst prüfen:

1. Ist der Boost aktiv und ist die Preisgrenze erreicht?
2. Ist Speicherladen als freigegebener Verbraucher aktiv?
3. Ist das Speicherziel bereits erreicht?
4. Blockiert Pre-Dump, Notreserve, Abregelschutz oder ein manueller Zustand?
5. Wird der aktuelle Nutzerpreis oder nur der Rohmarktpreis betrachtet?

### Übergang zum nächsten Marktplan

Nach einem zuvor bestätigten aktiven Halte- oder Netzladevertrag gilt bei jeder
Unterbrechung der Planquelle eine Karenz von höchstens 20 Sekunden ab dem ersten
Befund. Das umfasst abgelaufene Slots, Neuplanung mitten im Slot, fehlende oder
nicht lesbare Pläne, unvollständige oder ungebundene Planqualität und Schemafehler.
Wiederholte Befunde verlängern die Frist nicht. Währenddessen erscheint
„Folgeplan wird gelesen“. Aktuelle Messwerte, Netzladeziel, Nutzerfreigaben,
Notstromreserve und bekannte vorrangige Regelungen wirken weiter. Entzogene
Befugnisse aus Konfiguration und Zustand beenden die Karenz sofort; ein erst im
nicht lesbaren Folgeplan erkennbarer Besitzerwechsel bleibt höchstens 20 Sekunden
unsichtbar. Das gilt auch für einen Direktvermarkter-Besitz.
Netzladeverträge und anteilige Halte enden an ihrem Vertragsende. Nur ein Halt mit
auf Viertelstunden ausgerichtetem Beginn und Ende darf bis höchstens 20 Sekunden
über sein Ende hinaus bestehen. Ein gültiger Folgeplan entscheidet sofort über
Halten, Netzladen oder Freigabe.

Bei Simulatorausfall bleibt ein noch gültiger Planslot zunächst gültig. Jede
anschließende Planunterbrechung wird höchstens 20 Sekunden überbrückt. Nach einem
Neustart gibt es keine Karenz aus einem früheren Vertrag. Planqualität und
tatsächlich aktive Steuerung werden getrennt angezeigt.

## Prognosegrafik

Die Standard-SoC-Prognose rechnet ohne Halten und ohne Nachladen und endet an der wirksamen Notstromreserve. Liegt der gemessene Startwert bereits unter der Reserve, bleibt die Linie mindestens beim Startwert; eine Reserve-Rückgewinnung durch Netzladung wird hier nicht prognostiziert. Ist keine gültige Reserve im Plan enthalten, bleibt der prognostizierte Verlauf sichtbar.

Die SoC-Prognose mit Marktplan übernimmt die vollständige gültige Halteplanung bis zur letzten geplanten Akkuentnahme. Haltefenster entstehen nur, solange tatsächlich Energie oberhalb der Reserve zurückgehalten wird. PV-Überschüsse laden wie im Standardverlauf; anschließend folgt die Prognose wieder dem Standardverlauf mit Reserveboden. Netzladen wird nur für einen ausgewählten Ladeauftrag berücksichtigt. Die Linie ist geplant, wird jede Viertelstunde neu bestätigt und ist keine Zusage eines durchgehenden Betriebszustands. Ohne gültige Halteplanung, oder wenn die Marktplanung das aktuelle Halten verworfen oder eine andere Aktion gewählt hat, gilt die bisherige Anzeige ausgewählter Marktaktionen.
