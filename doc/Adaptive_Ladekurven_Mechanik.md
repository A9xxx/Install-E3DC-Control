# Adaptive Ladekurven-Mechanik

> **Stand:** V5.2.2a, aktualisiert am 2026-06-16

Diese Datei erklärt die Idee hinter der adaptiven Speicher-Ladekurve in
einfacher Sprache. Die technische Detailbeschreibung steht in
[Ladekurve_Berechnung.md](Ladekurve_Berechnung.md).

## Ziel

Der Hausspeicher soll nicht morgens blind vollgeladen werden, wenn mittags eine
PV-Spitze droht. Gleichzeitig soll er am Abend zuverlässig sein Tagesziel
erreichen. Die Ladekurve ist deshalb ein Kompromiss aus:

- erwarteter PV-Leistung,
- Speichergröße,
- Haus- und Wärmepumpenverbrauch,
- bekannten Wallbox- oder Heizstablasten,
- Einspeise- und Wechselrichtergrenzen,
- optionalen Preis- oder Unwettervorgaben.

## Warum keine starre Kurve?

Eine starre Gerade passt an vielen Tagen nicht:

- Bei sehr viel PV muss morgens Platz bleiben.
- Bei wenig PV darf nicht zu lange gebremst werden.
- Bei Wolken und Wärmepumpenlast muss die Kurve ruhig nachführen.
- Kurz vor Freilauf soll der Speicher nicht mehr mit harten Sprüngen aufholen.

Der Simulator erzeugt deshalb Zeitanker, die zum erwarteten Energieverlauf
passen. Der Manager folgt diesen Ankern weich.

## Q-Verhältnis als grobe Einordnung

Das Verhältnis aus erwarteter PV-Tagesenergie und nutzbarer Speicherkapazität
bleibt ein nützlicher Indikator:

```text
Q = erwartete PV-kWh / nutzbare Speicher-kWh
```

- hohes Q: viel PV im Verhältnis zum Speicher, die Kurve bleibt vormittags
  flacher und lässt Platz für die Spitze.
- mittleres Q: gleichmäßiger Ladeverlauf.
- niedriges Q: schlechte Prognose, der E3DC darf mehr autonom laden.

In der aktuellen Regelung ist Q aber nicht mehr der einzige Formgeber. Wetter,
saisonaler Nachtverbrauch, reale Verbraucher, adaptive Headroom-Reserve,
Pre-Dump und Plausibilitätskappen fließen ebenfalls ein.

## Headroom-Reserve

Headroom heißt: Speicherplatz für eine spätere PV-Spitze freihalten. Die
Anzeige trennt dafür seit 5.2.2a:

- `frei`: Speicherplatz, der im späteren Druckfenster schon vorhanden ist.
- `Reserve max`: größtes plausibles Reserveband aus Prognose, historischen
  PV-Spitzen und Live-Plausibilität.
- `Pre-Dump-Bedarf`: echte Energie, die vor Kurvenstart aktiv heraus muss.

`Reserve max` ist also kein Befehl, sofort diese Energiemenge zu entladen. Es
ist die Obergrenze des Reservebands, das die Kurve bei Bedarf freihält. Wenn
`Pre-Dump-Bedarf` null ist, gibt es keinen aktiven Vorab-Entladeauftrag.

## Weiche Regelung statt Sägezahn

Der Storage Manager betrachtet nicht nur "unter Kurve" oder "über Kurve".
Er berechnet:

- `iFc`: die durchschnittlich nötige Ladeleistung bis zum nächsten Anker,
- `curve_gap_catchup_w`: den geglätteten Aufholbedarf,
- Abregelschutzbedarf,
- verfügbare PV-/Exportreserve.

Nahe der Kurve wird der Aufholterm gedämpft. Dadurch soll der Speicher ruhig
entlang der Kurve laufen, statt bei jedem kleinen Rückstand auf Vollgas zu
springen und danach wieder hart zu bremsen.

## Kontroll-SoC

Der echte SoC kommt vom E3DC und bleibt maßgeblich. Für die Regelung nutzt der
Manager zusätzlich einen `curve_control_soc`, der die gemessene
Batterieleistung integriert. Dadurch werden kleine Messsprünge nicht sofort als
neuer Regelauftrag interpretiert.

Wenn der Kontrollwert zu weit vom echten SoC abweicht, wird er zurückgesetzt.

## Pre-Dump

Pre-Dump schafft vor sehr starken PV-Tagen Speicherplatz. Er folgt einer
Zielrampe bis zum Kurvenstart. Lokale Verbraucher haben Vorrang, Netz-Dump ist
nur Fallback. Nach Kurvenstart darf Pre-Dump nicht mehr aktiv entladen.

Nach Kurvenstart kann Headroom trotzdem weiter wirken: zuerst über
Ladebegrenzung und Kurvenband, optional über kurze Headroom-Entladung. Diese
aktiven Impulse haben seit 5.2.2a einen eigenen Schalter mit Tageslimit und
Pause. Wer diesen Schalter deaktiviert, schaltet nicht die Kurve aus, sondern
nur die aktive Headroom-Entladung.

## Freilauf

Freilauf ist die Übergabe an den E3DC-AUTO-Betrieb am Ende der Kurvenführung.
Der Übergang wird weich geöffnet:

- keine harte Aufholjagd kurz vor Ziel,
- keine künstliche Ladebremse bis zur letzten Minute,
- schwache Rest-PV darf der E3DC autonom mitnehmen.

Damit soll der Speicher meist leicht auf oder knapp über der Kurve herauslaufen,
statt mit einem sichtbaren Sprung in den Freilauf zu fallen.
