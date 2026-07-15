# Geplante Lastfenster

Geplante Lastfenster sind ein enger Opt-in-Regelpfad für große, statische Verbraucher, die E3DC-Control nicht direkt steuern oder messen kann. Typische Beispiele sind eine externe Wallbox, ein Drehstromanschluss oder eine feste Zeitschaltuhr. Das System erkennt solche Lasten nicht automatisch aus hohem Hausverbrauch, sondern nutzt ausschließlich vom Nutzer eingetragene Zeitfenster.

## Ziel

Das Fenster macht eine sonst unplanbare Last berechenbar:

- Der Storage Simulator zieht die geplante Leistung im Zeitfenster von der PV-/Verbrauchsprognose ab.
- Der Storage Manager sperrt die Speicherentladung nur, wenn das Zeitfenster aktiv ist und die Last anhand der erwarteten statischen Leistung plausibel im Hausverbrauch sichtbar ist.
- Bleibt die Last aus, wird keine Speicherbremse gesetzt.

Damit wird verhindert, dass eine externe Wallbox den Hausspeicher leerzieht, ohne dass normale kurze Verbraucher wie Herd, Werkzeug oder Haushaltsgeräte fälschlich als Regelgrund behandelt werden.

## Strenge Regeln

- Die Last muss explizit als Zeitfenster konfiguriert sein.
- Die Leistung muss statisch genug sein und mindestens 2 kW erreichen.
- Die Dauer muss mindestens 45 Minuten erreichen.
- Die Start-Toleranz beträgt 15 Minuten, der Nachlauf 30 Minuten, ein Vorlauf ist nicht vorgesehen.
- Eine aktive Entlade-Sperre entsteht erst nach Plausibilitätsprüfung: aktuelle Hauslast minus prognostizierte Grundlast muss zur Fensterleistung passen.
- Der Manager setzt dafür E3DC-`AUTO` mit `MAX_DISCHARGE_POWER = 0 W`; Laden und PV-Aufnahme bleiben frei.
- Nicht erkannte oder zu kleine Lasten werden ignoriert und nur in der Plan-Metainfo als verworfen aufgeführt.

## Konfiguration

Die Fenster werden im Config-Editor im Block **Geplante Lastfenster** gepflegt. Nutzer setzen dort nur Aktivierung, Name, Art, Start, Ende, Leistung und Wochentage. Mindestleistung, Mindestdauer und Toleranzen sind feste Schutzwerte von E3DC-Control und werden nicht als normale Eingabefelder angeboten.

Intern liegen die Fenster in `data/e3dc_v4.json` unter `planned_load_windows`:

```json
{
  "planned_load_enable": true,
  "planned_load_windows": [
    {
      "name": "Externe Wallbox",
      "enabled": true,
      "type": "external_wallbox",
      "start": "02:00",
      "end": "06:00",
      "power_w": 11000,
      "mode": "protect_storage"
    }
  ]
}
```

Optionale Wochentage können mit `weekdays` angegeben werden. Werte sind `0` bis `6` für Montag bis Sonntag oder deutsche Kürzel wie `mo`, `di`, `mi`.

## Bewusst nicht enthalten

Geplante Lastfenster sind keine automatische Fremdlast-Erkennung. E3DC-Control entscheidet nicht aus mehreren Stunden hohem Hausverbrauch, dass es sich um ein Auto handeln muss. Ohne Nutzerfenster bleibt hoher Hausverbrauch normaler Hausverbrauch, weil die Regelung sonst Akku, Netzbezug und Komfort anhand nicht belegbarer Annahmen steuern würde.
