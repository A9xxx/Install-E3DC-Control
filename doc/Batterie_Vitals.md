# Batterie-Vitals

Das Batterie-Vitals-Dashboard liest Gesundheitsdaten direkt per RSCP aus dem E3DC-Batteriemanagement.

## Voraussetzungen

- E3DC RSCP-Zugang aktiv
- Zugangsdaten in `data/e3dc_v4.json`
- Python-Abhaengigkeit `pycryptodome`

## Datenfluss

```text
vitals.php -> vital_stats.py -> rscp_client.py -> E3DC RSCP Port 5033
```

`vital_stats.py` liest die RSCP-Zugangsdaten aus `e3dc_v4.json`. `e3dc.config.txt` ist hier keine primaere Quelle mehr.

## Angezeigte Werte

| Wert | Bedeutung |
|---|---|
| SOH | State of Health des Batterie-Packs |
| Zyklen | abgeschlossene Ladezyklen |
| Zelltemperatur | minimale und maximale Zelltemperatur |
| Zell-Drift | Spannungsdifferenz zwischen staerkster und schwaechster Zelle |
| Softwarestand | vom E3DC gemeldete Version |

## Einordnung

Die Kapazitätsanzeige trennt die konfigurierte nutzbare Referenz
(`speichergroesse`) von BMS-Spezifikation, FCC und USABLE. Die aus Ah berechneten
BMS-Werte verwenden eine angenommene Modulspannung von 51,8 V. Sie beweisen weder
die auf dem Typenschild installierte Bruttokapazität noch die nutzbare Kapazität
im Neuzustand. Fehlende Werte bleiben unbekannt.

Eine SOH-basierte Schätzung verwendet ausschließlich die konfigurierte Referenz
mal SOH. Bereits vom BMS gemeldete Nutzwerte werden nicht nochmals mit SOH
multipliziert. Die Schätzung ersetzt keinen gemessenen Kapazitätstest. Für die
Referenz ist die zur Anlage passende nutzbare Herstellerangabe einzutragen.

- Zell-Drift unter 30 mV ist sehr gut.
- 30 bis 50 mV ist im Alltag meist unkritisch.
- 50 bis 100 mV sollte beobachtet werden.
- Über 100 mV kann auf Zellalterung, Balancing oder Messprobleme hinweisen.

## Fehlerbehebung

Wenn keine Daten erscheinen:

1. RSCP-Zugangsdaten im Config-Editor prüfen.
2. Port 5033 und AES-Key prüfen.
3. Test auf der Konsole ausfuehren:

```bash
<VENV_PATH>/bin/python3 <INSTALL_PATH>/Installer/vital_stats.py --once
```

Logs liegen unter:

```text
/var/www/html/logs/
```

## SOH-Quelle und Näherung

Echte Modulwerte aus `BAT_DCB_SOH` haben Vorrang. Fehlen sie vollständig in einem
Schrank, kann dessen BMS-Wert `BAT_ASOC` als gekennzeichnete SOH-Näherung dienen
(endlich, größer als 0 und höchstens 110 %). Daraus entstehen keine Modulwerte,
kein schwächster Pack und keine SOH-Spreizung. Die Diagnose-Ampel bewertet nur
vorhandene Pack-Werte. System-SOH und Durchschnitt bleiben kapazitätsgewichtet
und werden bei Beteiligung einer Näherung ebenfalls gekennzeichnet.

Die Verschleißprognose nennt bei ASOC ausdrücklich die Schrank-Näherung als Basis;
Mindestzyklen und Mindestalter gelten weiterhin. Kapazitätsangaben sind kein
Ersatz für SOH. Im Monatsverlauf enthält `cabinets` den Schrankwert,
`soh_source`, `soh_approx`, `soh_quality` und `soh_reason`. `packs` behält echte
Modulwerte bzw. `null` mit Grund. Alte Monatsstände ohne diese Zusatzfelder
bleiben lesbar; ihre Quellenqualität wird nicht nachträglich behauptet.
