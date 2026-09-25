# Zusatzwechselrichter (Sungrow, Modbus TCP)

Optionale Direktlesung eines zweiten, netzgekoppelten Wechselrichters über
Modbus TCP. Sie liefert Phasen-, String- und Diagnosedaten für Dashboard und
Fehlersuche. Die Regelung bleibt davon unberührt: Speicher-, Wallbox- und
Wärmepumpenlogik nutzen weiterhin den E3DC-Messwert des externen
Leistungsmessers (`Ext_PV_Power`), der Zusatzwechselrichter wird nur gelesen
und nie gesteuert.

## Voraussetzungen

- Der Wechselrichter ist über LAN erreichbar und beantwortet Modbus-TCP-Anfragen
  (Standard Port 502, Unit-ID 1). Das Kommunikationsmodul des Herstellers muss
  Modbus TCP freigeben; ohne offenen Port bleibt die Direktlesung ungültig.
- Unterstützt ist die Registerbelegung der Sungrow-String-Wechselrichter
  (SG-Serie, ein- und dreiphasig). Hybridgeräte der SH-Serie verwenden eine
  andere Belegung und werden nicht ausgelesen.
- Der externe Leistungsmesser am E3DC bleibt Pflicht, wenn die Erzeugung des
  Zusatzwechselrichters in Prognose und Regelung einfließen soll.

## Konfiguration (`data/e3dc_v4.json`, Editor-Gruppe „Zusatzwechselrichter“)

| Schlüssel | Standard | Bedeutung |
|---|---|---|
| `ext_inverter_type` | `none` | `none` = aus, `sungrow_modbus` = Sungrow-String-Wechselrichter per Modbus TCP |
| `ext_inverter_ip` | leer | IPv4-Adresse des Wechselrichters bzw. seines Kommunikationsmoduls |
| `ext_inverter_port` | `502` | Modbus-TCP-Port (1–65535) |
| `ext_inverter_unit_id` | `1` | Modbus-Unit-ID (0–247) |
| `ext_inverter_poll_s` | `10` | Abfrageintervall in Sekunden (5–60) |

Ungültige Angaben (fehlende oder fehlerhafte IP, Port, Unit-ID oder Intervall)
deaktivieren die Lesung; der Konfigurations-Editor zeigt den Grund an. Der
Live-Dienst prüft je Zyklus den Änderungszeitpunkt der Konfigurationsdatei;
geänderte Werte wirken innerhalb weniger Sekunden ohne Neustart von
`e3dc-live` (die Lesung startet mit den neuen Werten neu, ungültige Werte
deaktivieren sie).

## Daten in `live_data_py.json`

Der Live-Dienst liest den Wechselrichter in einem eigenen Hintergrundthread im
konfigurierten Intervall; die RSCP-Zykluszeit wird davon nicht beeinflusst.
Jeder Zyklus übernimmt nur den letzten Stand:

- Block `ext_inverter`: `valid`, `age_s`, `error`, `device_type_code`,
  `nominal_kw`, `output_type`, `ac_w`, `ac_voltage_kind`, `ac_p1_w..ac_p3_w`,
  `ac_v1..ac_v3`, `ac_i1..ac_i3`, `apparent_va`, `reactive_var`, `power_factor`, `dc_w`,
  `mppt` (Liste je Tracker mit `v`, `i`, `w`), `daily_kwh`, `total_kwh`,
  `running_h`, `temp_c`, `freq_hz`, `state_code`, `state_name`.
- Flache Diagnosefelder: `ext_pv_valid`, `ext_pv_p1_w`, `ext_pv_p2_w`,
  `ext_pv_p3_w`, `ext_pv_dc_w`, `ext_pv_age_s`.

Gültig (`valid` = true) ist ein Stand nur, wenn der Typ aktiv ist, die letzte
Lesung fehlerfrei war und sie nicht älter ist als das doppelte Intervall plus
die Verbindungszeitüberschreitung. Zeitüberschreitung, Modbus-Ausnahme, falsche
Unit-ID oder eine unvollständige Antwort ergeben `valid` = false mit
Fehlertext und ohne Teildaten; alle flachen Felder sind dann `null`.

Die Phasenleistungen `ac_p1_w..ac_p3_w` sind eine Näherung aus Spannung × Strom
je Phase ohne Leistungsfaktor (Scheinleistung je Phase). Der Wechselrichter
liefert keine Wirkleistung je Phase; der gemessene Leistungsfaktor steht
getrennt zur Verfügung. Die Summe der drei Näherungen weicht deshalb leicht
von `ac_w` ab.

Welche Spannung die Register 5019–5021 liefern, hängt vom Ausgangstyp
(`output_type`) ab: dreiphasige Geräte mit Neutralleiter (Typ 1, 3P4L) melden
Phasenspannungen, Geräte ohne Neutralleiter (Typ 2, 3P3L) Leiterspannungen
A-B/B-C/C-A. Bei Typ 2 rechnet die Näherung mit Leiterspannung/√3 (symmetrisches
Netz vorausgesetzt); `ac_voltage_kind` (`phase`, `line` oder `unknown`) benennt
die Basis der Rohwerte `ac_v1..ac_v3`. Einphasige Geräte (Typ 0) belegen nur
Spannung und Strom der ersten Phase, die Felder der Phasen 2 und 3 sind `null`.
Bei unbekanntem Ausgangstyp bleiben die Phasenleistungen `null`; es wird kein
Schätzwert ausgegeben.

## Anzeige

Dashboard und Diagramme zeigen die Direktlesung getrennt vom E3DC-Messwert:

- PV-Diagramm: „Zusatz-WR (E3DC-Messung)“ ist der Messwert des externen
  Leistungsmessers (Regelgröße); „Zusatz-WR String 1/2/3“ sind die
  MPPT-Leistungen der Direktlesung. Netz-/Phasenansicht: „Zusatz-WR L1/L2/L3“.
- Diagnosekarte über dem PV-Diagramm (nur bei konfigurierter Direktlesung):
  Zustand, AC gesamt, Phasen, DC/MPPT, Temperatur, Tages-/Gesamtertrag,
  Betriebsstunden und Datenalter; die Mobilansicht zeigt eine Kompaktkarte.
- `live_history.txt` erhält bei konfigurierter Direktlesung die Spalten
  `ext_pv_p1_w`, `ext_pv_p2_w`, `ext_pv_p3_w`, `ext_pv_dc_w` und
  `ext_mppt1_w`..`ext_mppt3_w` (`null` ohne gültige Lesung); ältere Zeilen
  ohne diese Spalten bleiben lesbar.

## Registerbelegung

Eingangsregister (Funktionscode 04), Registernummern wie in der Sungrow-
Protokollbeschreibung für netzgekoppelte String-Wechselrichter (1-basiert; auf
dem Draht wird die Adresse Register − 1 gesendet). 32-Bit-Werte stehen mit
niederwertigem Wort zuerst.

| Register | Größe | Skalierung |
|---|---|---|
| 5000 | Gerätetypcode | U16 |
| 5001 | Nennleistung | U16, × 0,1 kW |
| 5002 | Ausgangstyp | U16 (0 einphasig, 1 dreiphasig 4L, 2 dreiphasig 3L) |
| 5003 | Tagesertrag | U16, × 0,1 kWh |
| 5004–5005 | Gesamtertrag | U32, kWh |
| 5006–5007 | Betriebsstunden | U32, h |
| 5008 | Innentemperatur | S16, × 0,1 °C |
| 5009–5010 | Scheinleistung | U32, VA |
| 5011–5016 | MPPT 1–3 Spannung/Strom | U16, × 0,1 V bzw. × 0,1 A |
| 5017–5018 | DC-Gesamtleistung | U32, W |
| 5019–5021 | Spannungen A/B/C: Phasenspannungen bei Ausgangstyp 1, Leiterspannungen A-B/B-C/C-A bei Ausgangstyp 2, bei Typ 0 nur 5019 | U16, × 0,1 V |
| 5022–5024 | Phasenströme A/B/C | S16, × 0,1 A |
| 5031–5032 | Wirkleistung gesamt | U32, W |
| 5033–5034 | Blindleistung gesamt | S32, var |
| 5035 | Leistungsfaktor | S16, × 0,001 |
| 5036 | Netzfrequenz | U16, × 0,1 Hz |
| 5038 | Betriebszustand | U16 (0 Betrieb, 0x8000 Stopp, 0x1400 Standby, 0x5500 Fehler, …) |

Die Belegung folgt der öffentlichen Sungrow-Protokollbeschreibung
„Communication Protocol of PV Grid-Connected String Inverters“ (Zeilen 22–24:
Spannungsbasis der Register 5019–5021 je Ausgangstyp 5002) und deckt
sich mit den offenen Integrationen SunGather (`registers-sungrow.yaml`), evcc
(Template `sungrow-inverter`, Wirkleistung Adresse 5030 = Register 5031) und
der Home-Assistant-Modbus-Konfiguration für Sungrow (Adresse = Register − 1,
Wort-Tausch bei 32-Bit-Werten). Eine Plausibilitätsschranke verwirft
Wirkleistungen über dem 1,5-fachen der Nennleistung als Lesefehler.

## Diagnose

Eine einzelne Lesung ohne laufenden Dienst:

```text
python3 Installer/ext_inverter_modbus.py <ip> [port] [unit]
```

Die Ausgabe ist das normalisierte Ergebnis als JSON; Rückgabewert 0 bei
gültiger Lesung, 1 bei Fehler. Zugangsdaten oder Geheimnisse sind für die
Direktlesung nicht nötig und werden nicht gespeichert.
