# Dimplex WPM Touch / NWPM

E3DC-Control kann Dimplex-Wärmepumpen mit WPM Touch und NWPM IP-Modul über Modbus TCP anbinden.

## Konfiguration

Im Config Editor:

- `WP-/Verbrauchslogging aktivieren`: Ein
- `Wärmepumpen Typ`: `Dimplex WPM Touch / NWPM`
- `Dimplex IP-Adresse`: IP des NWPM-Moduls
- `Port`: `502`
- `Unit-ID`: meist `1`
- `WPM Software`: optional, z.B. `M3.21`; leer lassen, dann liest der Live-Dienst Register `65`/`66`/`67` automatisch
- `dimplex_cop_estimate`: optionaler COP-Schätzwert, Standard `3.0`
- Die Register für Vorlauf, Rücklauf, Warmwasser-Soll, Wärmequelle und Kühlkreis sind einzeln überschreibbar. Die Standardwerte entsprechen der offiziellen NWPM-Datenpunktliste für WPM J/L/M.

## Register

Die Standardwerte nutzen die offiziellen Dimplex-WPM-Touch-Dokumentationsadressen. E3DC-Control rechnet intern automatisch auf die 0-basierte Modbus-PDU-Adresse um.

| Wert | Dokumentationsadresse | PDU-Adresse | Bedeutung |
| --- | ---: | ---: | --- |
| Smart Grid | `5167` | `5166` | Holding Register für SG-Ready |
| Außentemperatur | `1` | `0` | INT, 0.1 °C |
| Rücklauf-Isttemperatur | `2` | `1` | INT, 0.1 °C |
| Warmwasser-Isttemperatur | `3` | `2` | INT, 0.1 °C |
| Vorlauf-Isttemperatur | `5` | `4` | INT, 0.1 °C |
| Wärmequelleneintritt | `6` | `5` | INT, 0.1 °C; optional, nicht jede Anlage liefert einen Wert |
| Wärmequellenaustritt | `7` | `6` | INT, 0.1 °C; optional |
| Kühlkreis Vorlauf | `19` | `18` | INT, 0.1 °C; optional |
| Kühlkreis Rücklauf | `20` | `19` | INT, 0.1 °C; optional |
| Kühlkreis Primär-Rücklauf | `21` | `20` | INT, 0.1 °C; optional |
| Rücklauf-Solltemperatur | `53` | `52` | INT, 0.1 °C |
| Warmwasser-Solltemperatur | `58` | `57` | INT, 0.1 °C |
| Betriebsmodus | `5015` | `5014` | Sommer/Winter/Urlaub/Party/2. WE/Kühlen |
| Wärmeleistung | `5168` | `5167` | W/10, ab WPM-Software M 3.5 |
| Elektrische Leistung | `5170` | `5169` | W/10, ab WPM-Software M 3.5 |
| Heartbeat Out | `5064` | `5063` | Heartbeat-Ausgabe, ab WPM-Software M 3.14 |
| Softwarestand | `65`/`66`/`67` | `64`/`65`/`66` | Version, Nummer und Index, z.B. `M3.21` |

Wenn deine Modbus-Doku oder dein Testtool bereits 0-basierte Adressen erwartet, setze `dimplex_modbus_zero_based=1`.

Wenn das elektrische Leistungsregister eine laufende Wärmepumpe zeigt, das Wärmeleistungsregister aber nur einen unplausibel kleinen Wert liefert, nutzt der Live-Dienst `dimplex_cop_estimate` als gekennzeichnete Wärmeleistungs-Schätzung. Der Rohwert bleibt im Frontend und in der Diagnose sichtbar.

## SG-Ready-Zustände

| Registerwert | Zustand | Nutzung in E3DC-Control |
| ---: | --- | --- |
| `0` | Gelb / Normalbetrieb | Keine aktive Freigabe; bestätigter Normalwert bei WPM Touch M3.21 |
| `10` | Gelb / Normalbetrieb | Optionaler/älterer Rohwert, wird nur noch als Livewert erkannt |
| `11` | Grün / Smart Grid Anhebung | PV-/Preis-Boost und Standard-Warmwasser-Boost |
| `12` | Rot / EVU-Sperrezwang | Preis-/PV-Pause |
| `13` | Dunkelgrün / maximale Anhebung | Nur wenn `dimplex_allow_dark_green=1` gesetzt ist |

`dimplex_allow_dark_green` ist standardmäßig aus. Laut Dimplex kann dunkelgrün neben der Wärmepumpe auch elektrische Wärmeerzeuger im verstärkten Betrieb anfordern.

Andere Live-Werte, z.B. `20`, werden bewusst als Dimplex-Rohwert angezeigt und nicht als Fehler umgedeutet. Dann bitte die Rohregister und den Softwarestand mitmelden.

Der Live-Dienst `e3dc-dimplex-live` liest nur. Aktive SG-Schreibbefehle kommen ausschließlich vom `energy_manager`, also über dieselben Freigaben wie PV-Budget, Preisfenster, Pre-Dump und manuelle Wärmeanforderung.

## Temperatur-Skalierung

`dimplex_temp_scale=auto` erkennt einfache INT-Werte und Zehntelgradwerte plausibel automatisch. Falls deine Anlage feste Rohwerte liefert, kann die Skalierung explizit gesetzt werden:

- `1` für direkte Grad Celsius
- `0.1` für Zehntelgrad
