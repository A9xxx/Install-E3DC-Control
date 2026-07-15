# Börsenpreis-Optimierung und Preis-Boost

> **Stand:** V5.2.4e, aktualisiert am 2026-06-23

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

Die Preisgrenze beschreibt den maximal erlaubten Nutzerpreis für diesen
Boost-Pfad. Beispiel: `0 ct/kWh` bedeutet, dass der Boost erst bei
Null- oder Negativpreis aktiv wird.

Dieser Block ist nicht die allgemeine Antwort auf unbekannte hohe Hauslasten.
Er arbeitet nur mit freigegebenen Verbrauchern und klarer Preisgrenze.

Seit der Marktpfad-Härtung gilt zusätzlich: Preiswechsel allein erzeugen keinen
Speicher-Owner. Wenn PV-Prognose, aktueller SoC und vorhandene Reserven den
kommenden Bedarf decken, bleibt die normale Ladekurve führend. Der Preis-Boost
wird erst aktiv, wenn daraus ein plausibler, freigegebener Lade- oder
Halteauftrag entsteht.

## 3. Preis-Reserve / Mindest-Preisvorteil

Die bisherige Bezeichnung `Preis-Reserve (%)` meint technisch eher eine
Preis-Toleranz beziehungsweise einen Mindest-Preisvorteil. Der Wert verhindert,
dass das System wegen winziger Preisdifferenzen unnötige Ladezyklen erzeugt.

Faustregel:

- um 15 Prozent deckt typische Lade-/Entladeverluste und kleine Preisunsicherheiten ab,
- höhere Werte bevorzugen nur die wirklich günstigsten Stunden,
- niedrigere Werte reagieren aggressiver auf kleinere Preisunterschiede.

Für Octopus-Heat- oder EPEX-Nutzer ist dieser Wert die Stellgröße dafür, ob nur
der günstigste Bereich genutzt wird oder auch angrenzende Stunden mitlaufen
dürfen.

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
| Wallbox-Entscheidungen | Preisfreigabe, Ladeplan, Netzfreigabe |

Wenn ein System bei günstigem Preis nicht lädt, zuerst prüfen:

1. Ist der Boost aktiv und ist die Preisgrenze erreicht?
2. Ist Speicherladen als freigegebener Verbraucher aktiv?
3. Ist das Speicherziel bereits erreicht?
4. Blockiert Pre-Dump, Notreserve, Abregelschutz oder ein manueller Zustand?
5. Wird der aktuelle Nutzerpreis oder nur der Rohmarktpreis betrachtet?
