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
nur bis zum Ende des laufenden 15-Minuten-Slots. Vor dem berechneten spätesten
Ladestart bleibt der Speicher entladegesperrt und nimmt PV-Überschuss auf;
Netzladen trotz Einspeisung beginnt erst ab diesem Zeitpunkt. Ist das Ziel
erreicht (Hysterese 1 SoC-Punkt, `market_target_hysteresis_pct`; kleinere Werte
werden auf 1 angehoben), wird im selben Fenster nicht erneut aus dem Netz
geladen: Der Speicher bleibt bis zum Fensterende entladegesperrt, PV lädt
weiter. Für den Marktpfad freigegebene Verbraucher (Wallbox über
`market_wallbox_enable`, nur für Ladepunkte in `Sofort bis Preislimit`;
Heizstab über `market_heater_enable`) bleiben bis zum Fensterende
freigegeben. Die Wärmepumpe gibt der normale Marktpfad nicht frei; sie
behält ihre Prognose-, PV- und Pre-Dump-Führung mit Taktschutz. Nur der
Negativpreis-Boost (Abschnitt 2) kann sie gesondert freigeben.

Der Netzladebedarf wird zeitgerichtet bestimmt: Er reicht nur bis zum nächsten
nutzbaren günstigen Preisfenster (gleicher Tarifpreis oder höchstens um die
Mindestmarge teurer); was dieses Fenster physisch liefern kann, wird
angerechnet. Die für das laufende Fenster erwartete PV-Erzeugung wird zuerst
berücksichtigt; aus dem Netz wird nur der Rest geladen, der zum Fensterende
noch fehlt, gedeckelt auf den freien Raum bis zum Ladeziel. Das Netzladen liegt
deshalb am Ende des Fensters; nach dem spätesten Ladestart wird es auch dann
ausgeführt, wenn PV den Speicher gerade lädt (der PV-Überschuss fließt weiter in
den Speicher). Ladeziel und Netzladejob leiten sich aus dem Sollbestand am
Fensterende ab: dem Bedarf bis zum nächsten nutzbaren Fenster zuzüglich der
Puffer. Beide bleiben während des Fensters stabil, auch wenn der Speicher
zwischenzeitlich aus PV oder Netz lädt; deckt der erwartete Bestand den
Sollbestand, wird nicht aus dem Netz geladen. Während des laufenden Fensters
wird die Hauslast nicht vom Speicherbestand abgezogen, weil der Speicher dort
nicht entlädt. Für die Ladedauer wird die tatsächlich beobachtete Ladeleistung
des Speichers verwendet, falls sie unter der konfigurierten liegt. Eine Messung
zählt erst, wenn der Netzladebefehl mit gleichem Sollwert schon
mindestens eine Minute ansteht (der erste Zyklus nach einer Umschaltung zeigt
noch die PV-Ladung oder die Anlauframpe); eine höhere gelieferte Leistung hebt
den Wert sofort an (höchstens bis zur konfigurierten Ladeleistung), abgesenkt
wird er nur aus Messungen an der Ladegrenze oder bei Unterlieferung; er verfällt
nach sieben Tagen. Begrenzt der beobachtete Wert die Planung, fordert die
Regelung während des Netzladens einmal je Viertelstunde für rund eine Minute die
konfigurierte Ladeleistung an (Anhebeprobe); nimmt der Speicher dann wieder mehr
auf, steigt der Wert sofort, sonst bleibt er. Hausanschluss- und Ladegrenzen
gelten dabei unverändert. Es zählt nur die Fehlmenge, deren späterer Bezugspreis
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
für neue Starts und für die Zukunftsgutschrift abgezogen. Dieser mittlere Preis wird beim
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
betroffenen Feld; die Eingaben bleiben erhalten. Im Komfortprofil beendet
jeder Preiswechsel das bisherige Tarifsegment, auch wenn die nächste Stufe
noch unter dem Preislimit liegt. Dort wird ein eigener Ladejob mit neuem
Fensterende und eigenem Referenzpreis berechnet; das Komfortziel bleibt gleich.

Ohne belegte physische Ladeleistung entsteht kein Netzladeauftrag. Die
Marktobergrenze begrenzt eine frische Geräte-, BMS- oder eingestellte Nutzergrenze
nur nach oben. Die Mindestmarge wird am gewichteten Nutzen der tatsächlich
versorgten Defizitslots geprüft; ein einzelner Spitzenpreis reicht nicht.
Ein reiner Zielpuffer ohne belegten Mengennutzen eröffnet keinen neuen Auftrag.
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
| `market_late_fill_safety_min` | Sicherheitsabstand vor dem Fensterende (Standard 15 min) |
| `market_late_fill_buffer_pct` | Puffer im Sollbestand am Fensterende, in Prozent der Speicherkapazität (Standard 3 %) |
| `maximumladeleistung` / `market_battery_max_w` | konfigurierte Ladeleistung, in der Planung zusätzlich durch die beobachtete Ladeleistung gedeckelt |

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

### Ende eines laufenden Markthalts

Ein vollständig geprüfter Folgeplan kann die Entladesperre bewusst beenden; der
Akku darf dann wieder das Haus versorgen. Ist der Plan vorübergehend nicht lesbar
oder unvollständig, bleibt ausschließlich ein bereits gültiger Vollslot-Markthalt
bis zum fest erwarteten nächsten Plan plus 20 Sekunden Übergangsfrist bestehen,
höchstens 15 Minuten ab dem ersten ungeklärten Übergang. Wiederholte Fehler
verlängern diese Frist nicht. Danach wird die Entladung mit einem benannten
Ablaufgrund freigegeben. Anteilige Halteenden, Nutzer-Aus, Notstromreserve und
andere vorrangige Regelungen wirken sofort. Ein Neustart nimmt eine alte
Fehlerbrücke nicht wieder auf. Planqualität und tatsächlich aktive Steuerung
werden getrennt angezeigt.
