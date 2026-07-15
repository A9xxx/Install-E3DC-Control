# Unwetterwächter

Der Unwetterwächter nutzt Wetterwarnungen, um den Speicher vor erwartbaren
PV-Einbrüchen oder lokalen Ausfallrisiken gezielt vorzubereiten. Standardmäßig
ist er defensiv: Er warnt und dokumentiert, greift aber erst ein, wenn der
Nutzer den Modus `Regeleingriff` aktiviert. Netzladen bleibt zusätzlich durch
einen eigenen Opt-in-Schalter geschützt.

## Datenquellen

Die Warninformationen werden vom Wetter-/Prognosedienst gelesen und in der
Ramdisk unter `/var/www/html/ramdisk/weather_alerts.json` abgelegt.

Genutzte Quellen:

- **DWD CAP-Warnungen** aus dem offiziellen DWD-Open-Data-Warnsystem für die
  hinterlegten Anlagenkoordinaten. Maßgeblich sind die DWD-Warnstufen 1 bis 4
  gemäß den offiziellen Warnkriterien:
  https://www.dwd.de/DE/wetter/warnungen_aktuell/kriterien/warnkriterien.html?nn=605882
- **Open-Meteo DWD ICON** als Kurzfristmodell für Gewittertendenzen. Verwendet
  werden u.a. Wettercode, Blitzpotential, Niederschlag, Schauer und Böen.

Die Daten sind reine Eingangsdaten. Der Unwetterwächter schreibt keine
Wetterdaten zurück und steuert keine externen Wetterdienste.

## Warnstufen

Die UI verwendet die DWD-Warnstufen:

- `1 Wetter`: einfache Wetterwarnung.
- `2 Markant`: markantes Wetter.
- `3 Unwetter`: Unwetterwarnung.
- `4 Extrem`: extremes Unwetter.

Für den Speicherregler werden derzeit diese Warnungen als relevant betrachtet:

- Gewitterwarnungen anhand DWD-CAP-Texten und DWD-Gruppenkennungen.
- Stufe 4 generell, also auch extremes Unwetter wie Orkan, extremer Starkregen,
  extremer Dauerregen, Schneefall, Schneeverwehung oder Glatteis.
- Winterwarnungen ab Stufe 3, wenn die DWD-Warnung Schnee, Schneeverwehung,
  Glätte, Glatteis, Eisregen oder Eisbruch enthält.

Damit ist der Wächter nicht nur ein Gewitterfilter, sondern ein
Unwetter-Ausfallschutz.

## Betriebsarten

### Aus

Das Dashboard-Wetterbadge bleibt sichtbar, der Speicherplan bewertet die
Warnung aber nicht. Es gibt keinen Kurvenanker, keine Nachtreserve und kein
Netzladen.

### Nur Warnung

Der Speicherplan schreibt Diagnose-Metadaten in `storage_plan.json`, greift aber
nicht aktiv ein. Dieser Modus eignet sich, um das Verhalten zu beobachten, ohne
die Regelung zu verändern.

### Regeleingriff

Der Wächter darf die Speicherplanung anheben. Netzladen ist damit noch nicht
automatisch erlaubt; dafür muss zusätzlich `Netzladen für Unwetter-Schutz
erlauben` aktiv sein.

## Regeleingriffe

### Kurvenanker vor Unwetter

Liegt die Warnung im aktiven Tagesfenster der Speicher-Ladekurve, kann der
Simulator einen zusätzlichen Anker in die `target_timeline` setzen oder einen
nahen vorhandenen Anker anheben.

Die Berechnung berücksichtigt:

- Warnbeginn und Warnende.
- Vorlaufzeit aus `Vorlauf (Min)`.
- prognostizierte Haus-/Wärmepumpenlast während des Ereignisses.
- verbleibende PV nach dem Ereignis.
- `Min kWh`, damit kleine theoretische Korrekturen keine Regelung auslösen.
- `Max SoC`, damit nicht unnötig PV-Freiraum zerstört wird.

Das Ziel ist nicht pauschal 100 %, sondern “genug Reserve, um nach dem Ereignis
wieder sinnvoll auf der Kurve zu liegen”.

### Nachtreserve

Liegt die Warnung vor dem ersten aktiven Kurvenfenster, wird kein künstlicher
Tagesanker erzeugt. Stattdessen prüft der Simulator nur die Restnacht:

1. Restnacht-Verbrauch bis zum normalen Kurvenstart wird aus der Prognose
   geschätzt.
2. Der erwartete Morgen-SoC wird berechnet.
3. Nur wenn dieser SoC unter den separaten `Netz-Morgenpuffer (%)` fallen würde,
   wird ein Nacht-Netzladefenster vorbereitet.

Wichtig: Der `Netz-Morgenpuffer (%)` ist unabhängig vom normalen
`Morgen-Puffer`. Nutzer können also einen hohen normalen Morgenanker für die
PV-Kurve setzen, ohne dass dieser hohe SoC nachts aus dem Netz geladen wird.

Ziel der Nachtreserve:

```text
Ziel-SoC = Netz-Morgenpuffer + prognostizierter Restnachtverbrauch
```

Der Wert wird durch `Max SoC` begrenzt.

### Netzladen

Netzladen wird nur aktiv, wenn alle Bedingungen erfüllt sind:

- Modus ist `Regeleingriff`.
- `Netzladen für Unwetter-Schutz erlauben` ist aktiv.
- Warnstufe ist größer/gleich `Netz ab Stufe`.
- Bei Nachtreserve: Speicher würde den `Netz-Morgenpuffer` unterschreiten.
- Aktueller SoC liegt unter dem berechneten Ziel abzüglich Hysterese.

Der Storage Manager behandelt diesen Pfad als geschützten Besitzer
`storm_guard_grid`. Währenddessen hat der Unwetterwächter Vorrang vor normalen
Komfortfunktionen. Nach Ende des Fensters fällt die Regelung wieder auf die
normale Ladekurve bzw. den E3DC-AUTO-Freilauf zurück.

## Konfigurationsfelder

- `Modus`: `Aus`, `Nur Warnung`, `Regeleingriff`.
- `Warnstufe ab`: Mindestwarnstufe für Diagnose bzw. Eingriff.
- `Vorlauf (Min)`: wie früh vor Warnbeginn der Zielanker erreicht sein soll.
- `Max SoC (%)`: Obergrenze für automatisch angehobene Schutz-Ziele.
- `Min kWh`: Mindestenergiebedarf für einen echten Kurveneingriff.
- `Netzladen für Unwetter-Schutz erlauben`: separates Opt-in für Netzbezug.
- `Netz ab Stufe`: Mindestwarnstufe für Netzladen.
- `Netz-Morgenpuffer (%)`: eigener Morgenpuffer für nächtliches Netzladen.

## Sicherheitsmodell

- Default ist `Nur Warnung`.
- Netzladen ist standardmäßig aus.
- DWD-Warnungen werden nur genutzt, wenn die Daten frisch sind.
- Veraltete oder fehlende Warnungsdaten erzeugen keinen Eingriff.
- Der normale hohe Morgenanker der Ladekurve wird nachts nicht automatisch als
  Netzladeziel verwendet.
- Der Unwetterwächter ersetzt keine Notstromplanung; er ist ein zusätzlicher
  prognosebasierter Schutzpfad.

