# Stiebel Eltron ISG / WPM Integration

Diese Dokumentation beschreibt die Stiebel-Eltron-Anbindung ab E3DC-Control
`5.0.5`. Der Dienst `e3dc-stiebel-live` ist bewusst als
Read-only-Live-Treiber gebaut. Er liest Werte aus dem ISG/WPM aus und speist
sie in die bestehende Waermepumpen-Anzeige ein. Aktive SG-Ready- oder
Temperatur-Schreibzugriffe sind in diesem Live-Dienst nicht enthalten.

## Funktionsumfang

- Live-Monitoring ueber Stiebel ISG / WPM per Modbus TCP.
- Anzeige von Aussen-, Vorlauf-, Ruecklauf-, Warmwasser- und
  Quellentemperatur.
- Auslesen von Betriebsart, Verdichterstatus, SG-Ready-Zustand und
  Tages-Energiezaehlern, soweit die ISG-Firmware die Register liefert.
- Schaetzung der aktuellen elektrischen Leistungsaufnahme der Waermepumpe.
- Optionaler externer Shelly-Leistungsmesser als bevorzugte Live-Leistung,
  z.B. Shelly Pro 3EM, Shelly 3EM, Shelly Plug/Plus oder Shelly PM.
- Optionales Auslesen der Verdichterfrequenz aus der ISG-Webseite
  `http://<ISG-IP>/?s=1,1`.
- Schreiben der Livewerte nach `/var/www/html/ramdisk/waermepumpe.json` und
  `/var/www/html/ramdisk/stiebel_isg.json`.
- Integration in Install-Center, Service-Katalog, Docker-Entrypoint,
  Rechte-Reparatur, Update, Uninstall und Diagnose.

## Sicherheitsmodell

Der Stiebel-Live-Dienst schreibt keine Register.

Insbesondere werden nicht geschrieben:

- keine Komforttemperaturen,
- keine Warmwasser-Solltemperaturen,
- keine Betriebsart,
- keine SG-Ready-Zustaende,
- keine EEPROM-relevanten Sollwerte.

Das ist Absicht. Die Heizung ist kritische Infrastruktur, und viele
Temperaturparameter werden im Regler dauerhaft gespeichert. Aktive
Schreiblogik muss deshalb getrennt vom Live-Treiber, mit Watchdog und nur nach
bewusster Nutzerfreigabe getestet werden.

## Voraussetzungen

- Stiebel Eltron ISG im lokalen Netz.
- Modbus TCP im ISG/WPM aktiviert.
- Der E3DC-Control Host erreicht das ISG auf Port `502`.
- Fuer die optionale Verdichter-Hz-Erkennung muss die ISG-Webseite erreichbar
  sein. Falls die Webseite ein Login verlangt, koennen Benutzer und Passwort
  in der Config eingetragen werden.

## Konfiguration

Die Einrichtung erfolgt im Frontend, nicht durch manuelles Setzen von
Roh-Keys.

1. Config-Editor oeffnen.
2. Bereich **Smart Home & Verbrauchsprognose** oeffnen.
3. **WP-/Verbrauchslogging aktivieren** einschalten.
4. Bei **Wärmepumpen Typ** den Eintrag **Stiebel Eltron ISG / WPM** waehlen.
5. **ISG IP-Adresse** eintragen, z.B. `192.0.2.233`.
6. **Port** auf `502` und **Unit-ID** auf `1` lassen, falls am ISG nichts
   anderes eingestellt ist.
7. **Hz aus Web** nur auf **Ja** stellen, wenn die ISG-Prozessdaten-Seite aus
   dem Host oder Docker-Container wirklich erreichbar ist. Bei wiederholten
   Timeouts pausiert der Dienst automatisch 30 Minuten; Modbus/Shelly-Werte
   laufen weiter. Wenn die Meldung stoert, auf **Nein** lassen.
8. Optional einen **Externen Leistungsmesser** aktivieren, wenn ein separater
   Shelly die elektrische Waermepumpenleistung misst.

Der Schalter **Automatik darf Geräte steuern** kann fuer reines Stiebel-Live-
Monitoring ausgeschaltet bleiben. Der Live-Dienst liest nur Werte. SG-Ready-
Schreiben bleibt separat abgesichert.

Die folgenden internen Keys sind nur fuer Support, Diagnose und JSON-Pruefung
gedacht. Nutzer muessen sie normalerweise nicht direkt anfassen:

| Frontend-Feld | interner Key | Hinweis |
| --- | --- | --- |
| WP-/Verbrauchslogging aktivieren | `luxtronik` | Historischer Key fuer den gemeinsamen Waermepumpen-/Energy-Manager-Pfad. |
| Wärmepumpen Typ: Stiebel Eltron ISG / WPM | `wp_type` | Wird vom Frontend automatisch auf Stiebel gesetzt. |
| ISG IP-Adresse | `stiebel_isg_ip` | IP-Adresse des ISG, z.B. `192.0.2.233`. |
| Port | `stiebel_isg_port` | Modbus-TCP-Port, Standard `502`. |
| Unit-ID | `stiebel_isg_device_id` | Modbus Unit-ID, Standard `1`. |
| HZ Leistung (W) | `stiebel_isg_power_heating_w` | Nenn-/Schaetzleistung in Watt fuer Heizbetrieb. |
| WW Leistung (W) | `stiebel_isg_power_dhw_w` | Nenn-/Schaetzleistung in Watt fuer Warmwasser. |
| COP Schätzung | `stiebel_isg_cop_estimate` | Faktor fuer die angezeigte thermische Momentanleistung, wenn das ISG keine echte Waermeleistung liefert. |
| Standby (W) | `stiebel_isg_standby_w` | Standby-Leistung der WP-Steuerung in Watt. |
| Max Hz | `stiebel_isg_max_hz` | Maximal angenommene Verdichterfrequenz fuer lineare Hz-Schaetzung. |
| Hz/Watt Kennlinie | `stiebel_isg_hz_power_map` | Optionale Kennlinie, z.B. `0:35,15:400,30:850,60:1800`. |
| Hz aus Web | `stiebel_isg_scrape_hz_enable` | Optionales Lesen der ISG-Prozessdaten-Seite, Standard `Nein`; nach drei Timeouts pausiert der Dienst 30 Minuten. |
| Web Benutzer | `stiebel_isg_web_user` | Optionaler ISG-Weblogin-Benutzer. |
| Web Passwort | `stiebel_isg_web_password` | Optionales ISG-Weblogin-Passwort. |
| Externer Leistungsmesser | `stiebel_isg_power_meter_enable` | Nutzt einen Shelly read-only als bevorzugte elektrische WP-Leistung. |
| Zaehler-IP | `stiebel_isg_power_meter_ip` | IP-Adresse des Shelly-Leistungsmessers. |
| Zaehlertyp | `stiebel_isg_power_meter_type` | `auto`, `shelly_3em`, `shelly_plug` oder `shelly_pm`. |

## Bare-Metal-Betrieb

Auf einer normalen Raspberry-Pi-/Linux-Installation wird der Dienst als
systemd-Service installiert:

```bash
sudo systemctl status e3dc-stiebel-live
sudo systemctl restart e3dc-stiebel-live
journalctl -u e3dc-stiebel-live -n 80
```

Die wichtigsten Dateien:

```text
/home/pi/Install/Installer/stiebel/stiebel_live.py
/var/www/html/logs/stiebel_live.log
/var/www/html/ramdisk/stiebel_isg.json
/var/www/html/ramdisk/waermepumpe.json
```

## Docker-Betrieb

Im Docker gibt es keinen systemd-Dienst. Der Container startet
`stiebel/stiebel_live.py` direkt aus der `entrypoint.sh`, wenn die Config
passt:

- **WP-/Verbrauchslogging aktivieren** ist eingeschaltet.
- **Wärmepumpen Typ** steht auf **Stiebel Eltron ISG / WPM**.
- **ISG IP-Adresse** ist gesetzt und nicht `0.0.0.0`.

Nach einer Config-Aenderung muss der Container einmal neu gestartet oder neu
erstellt werden, weil die Startlogik nur beim Containerstart ausgewertet wird.

Fertiges Image aktualisieren:

```bash
cd ~/e3dc-docker
sudo docker compose pull e3dc-control
sudo docker compose up -d --force-recreate e3dc-control
```

Lokales Image aus einem frisch gezogenen Repository neu bauen:

```bash
cd ~/e3dc-docker
sudo docker compose build --no-cache e3dc-control
sudo docker compose up -d --force-recreate e3dc-control
```

Pruefen:

```bash
sudo docker logs e3dc-control | grep "Stiebel ISG Live"
sudo docker exec e3dc-control sh -lc 'tail -n 80 /var/www/html/logs/stiebel_live.log'
sudo docker exec e3dc-control sh -lc 'cat /var/www/html/ramdisk/stiebel_isg.json'
```

Empfohlen ist `network_mode: "host"`, damit der Container das ISG im lokalen
Netz direkt erreicht.

## Datenfluss

Der Dienst pollt alle 30 Sekunden:

1. `data/e3dc_v4.json` lesen.
2. Stiebel-Register auslesen.
3. Optional Verdichter-Hz aus der ISG-Webseite lesen.
4. Optional externen Shelly-Leistungsmesser read-only lesen.
5. Werte normalisieren.
6. JSON atomar in die RAM-Disk schreiben.
7. Dashboard, Waermepumpen-Seite, MQTT-Hub und R5-Diagnose lesen diese
   normalisierten Daten.

## Gelesene Register

Die offizielle Stiebel-Eltron-Modbus-Dokumentation nennt 1-basierte
Tabellenadressen. Die Modbus-PDU im Code nutzt die technische Adresse
`Tabellenadresse - 1`. Das ist genau der Effekt, den viele FHEM-Setups zeigen:
Verdichter 1 steht in der Tabelle auf `2542`, gelesen wird im Code aber
`2541`.

Quelle: `https://www.stiebel-eltron.de/toolbox/content/docs/anleitungen/installation/ISG_Modbus/321798-44755-9770_ISG%20Modbus_de_en_fr_it_nl_cs_sk_pl_hu.pdf`

| Zugriff | Doku-Adresse | Code-Adresse | Zweck |
| --- | ---: | ---: | --- |
| FC04 Input | `501` | `500` | Systemwerte, u.a. Temperaturen |
| FC04 Input | `2501` | `2500` | Statuswerte, u.a. Verdichter und Pumpen |
| FC04 Input | `3501` | `3500` | Energiezaehler fuer Tag/Verbrauch |
| FC04 Input | `5001` | `5000` | SG-Ready-/Reglerinformation |
| FC04 Input | `6128` | `6127` | Verdichterleistung in Prozent, falls Firmware/Register vorhanden |
| FC03 Holding | `1501..1511` | `1500..1510` | Betriebsart und Soll-/Komfortparameter, read-only im Live-Dienst |
| FC03 Holding | `4001..4003` | `4000..4002` | SG-Ready-Schalter/Eingaenge, read-only im Live-Dienst |

Wichtige Einzelwerte:

| Wert | Doku-Adresse | Code-Adresse |
| --- | ---: | ---: |
| Aussentemperatur | `507` | `506` |
| Ruecklaufisttemperatur | `516` | `515` |
| Warmwasser-Ist | `522` | `521` |
| Quellentemperatur | `536` | `535` |
| Betriebsstatus (Bitfeld) | `2501` | `2500` |
| Kühlbetrieb Status | `2520` | `2519` |
| Verdichter 1 | `2542` | `2541` |
| SG-Ready Betriebszustand | `5001` | `5000` |

Nicht jede ISG-/Firmware-Version liefert alle Register. Fehlende optionale
Werte werden ausgelassen oder als unbekannt behandelt.

Der Betriebsstatus `2501` ist ein Bitfeld. Der Live-Dienst wertet daraus unter
anderem `B3` Heizen, `B4` Warmwasser, `B5` Verdichter läuft, `B6` Sommerbetrieb
und `B7` Kühlbetrieb aus. Zusätzlich wird das optionale Statusregister `2520`
als Kühlhinweis genutzt. Damit wird ein reiner Kühlstatus wie `128` als
`Kühlen` angezeigt und nicht mehr pauschal als `Heizen` oder `Verdichter ein`.
Wenn der Kühlhinweis gesetzt ist, aber der Verdichter nicht läuft, wird das als
passive Kühlung angezeigt. Warmwasserbetrieb plus Kühlhinweis erscheint als
`WW + passive Kühlung`. Eine elektrische Neben-/Pumpenleistung ist dabei nur
Plausibilität, nicht der primäre Statusgeber.
Bei WPF-`cool`-Anlagen ohne Modulation, z.B. WPF 10 cool, ist das besonders
wichtig: Passive Kühlung bedeutet Pumpenbetrieb über Sole/Fußbodenheizung,
nicht Verdichterleistung. Ein KNX-/WPM-Status `Kühlbetrieb=1` entspricht dem
Stiebel-Register `2520` und wird als aktive passive Kühlung übernommen.

Bekannte ISG-/FHEM-Mappings werden bevorzugt beruecksichtigt, u.a.
`i506` Aussen und `i521` Warmwasser-Ist. Die Quellentemperatur kommt nach
offizieller Tabelle von `536`, also als Codeadresse `535`; `537`/`536` bleibt
als Fallback fuer abweichende Firmware erhalten.

## Leistungsaufnahme

Viele Stiebel-ISG liefern keine direkte elektrische Live-Leistung in Watt. Der
Treiber schaetzt deshalb die Aufnahme in dieser Reihenfolge:

1. Externer Shelly-Leistungsmesser, wenn aktiviert und erreichbar.
2. Direkte WPMG-Aufnahmeleistung je Phase, wenn die ISG-Firmware diese
   Register bereitstellt.
3. Verdichterleistung in Prozent (Doku `6128`, Code `6127`), wenn vorhanden.
4. Verdichterfrequenz in Hz aus der ISG-Prozessdaten-Seite, wenn aktiviert.
5. Standby-Leistung, wenn der Verdichter aus ist.
6. Konfigurierter Nennwert fuer WW oder Heizen als Fallback.

Die Quelle steht im JSON-Feld `stiebel_power_source`, z.B.:

- `passive_cooling_standby`: Kühlhinweis aktiv, Verdichter aus; Leistung ist
  die konfigurierte Standby-/Nebenleistungsannahme, sofern kein externer
  Leistungsmesser vorhanden ist.

- `compressor_percent`
- `compressor_hz`
- `standby`
- `status_nominal_dhw`
- `status_nominal_heating`
- `shelly_3em_rpc`
- `shelly_3em_status`
- `shelly_switch_rpc`
- `shelly_pm_rpc`
- `shelly_meter`
- `wpmg_phase_power_primary`
- `wpmg_phase_power_secondary_1` bis `wpmg_phase_power_secondary_5`
- `wpmg_phase_power_system`

### Direkte WPMG-Leistungsregister

Einige ISG-Plus-/WPMG-Dokumentationen enthalten direkte elektrische
Aufnahmeleistungen je Phase. Der Treiber probiert diese Werte read-only und
summiert L1+L2+L3, wenn alle drei Phasen plausibel lesbar sind.

| Quelle | L1 Doku | L2 Doku | L3 Doku | L1 Code |
| --- | ---: | ---: | ---: | ---: |
| Primaere Waermepumpe | `6118` | `6119` | `6120` | `6117` |
| Sekundaere Waermepumpe 1 | `6268` | `6269` | `6270` | `6267` |
| Sekundaere Waermepumpe 2 | `6418` | `6419` | `6420` | `6417` |
| Sekundaere Waermepumpe 3 | `6568` | `6569` | `6570` | `6567` |
| Sekundaere Waermepumpe 4 | `6718` | `6719` | `6720` | `6717` |
| Sekundaere Waermepumpe 5 | `6868` | `6869` | `6870` | `6867` |
| System-/Sammeladresse | `36118` | `36119` | `36120` | `36117` |

Auf Ursis ISG Plus sind diese Register aktuell nicht freigeschaltet
(`Modbus exception 2`). Das ist kein Fehler im Treiber; dann faellt E3DC-Control
automatisch auf Verdichterstatus, Prozent/Hz, Shelly oder Nennwerte zurueck.

Wenn eine Anlage kalibriert werden soll, ist `stiebel_isg_hz_power_map` der
genaueste Weg. Beispiel:

```text
0:35,15:400,30:850,60:1800
```

Zwischen den Stuetzpunkten wird linear interpoliert.

### Externer Shelly-Leistungsmesser

Viele Nutzer messen die Waermepumpe bereits mit einem separaten Shelly. Fuer
Stiebel ist das der beste Weg, wenn ISG/Modbus keine elektrische Live-Leistung
liefert oder die Verdichter-Hz-Seite nicht erreichbar ist.

Unterstuetzt werden read-only:

- Shelly Pro 3EM / Shelly 3EM ueber `EM.GetStatus` oder `/status`,
- Shelly Plug / Plus Plug ueber `Switch.GetStatus` oder `/meter/0`,
- Shelly PM ueber `PM1.GetStatus`.

Der Messwert ueberschreibt nur die Felder `Leistung_Verdichter_W` und
`Leistungsaufnahme` im Live-JSON. Er schaltet kein Relais und schreibt keine
Stiebel-Register. Zusatzfelder wie `stiebel_external_power_w`,
`stiebel_external_power_source` und die Phasenwerte helfen bei Diagnose und
Kalibrierung.

## Betriebsart und SG-Ready

Der Live-Dienst liest die Betriebsart aus Doku-Register `1501` (Codeadresse
`1500`), schreibt sie aber nicht. Die ueblichen Werte sind:

| Wert | Bedeutung |
| ---: | --- |
| `0` | Notbetrieb |
| `1` | Bereitschaft |
| `2` | Programmbetrieb |
| `3` | Komfortbetrieb |
| `4` | Eco-Betrieb |
| `5` | Warmwasserbetrieb |

SG-Ready-Informationen werden gelesen und im Dashboard angezeigt. Die aktive
SG-Ready-Regelung ist nicht Teil dieses Live-Dienstes. Fuer spaetere aktive
Regelung gilt: erst Vor-Ort-Test, dann Watchdog, dann Opt-in.

## Troubleshooting

### Keine Daten im Dashboard

Bare metal:

```bash
sudo systemctl status e3dc-stiebel-live
journalctl -u e3dc-stiebel-live -n 80
```

Docker:

```bash
sudo docker logs e3dc-control | grep "Stiebel ISG"
sudo docker exec e3dc-control sh -lc 'tail -n 80 /var/www/html/logs/stiebel_live.log'
```

Pruefen:

- Ist im Config-Editor unter **Smart Home & Verbrauchsprognose** der Schalter
  **WP-/Verbrauchslogging aktivieren** eingeschaltet?
- Ist bei **Wärmepumpen Typ** **Stiebel Eltron ISG / WPM** ausgewaehlt?
- Ist die **ISG IP-Adresse** korrekt eingetragen?
- Fuer reines Live-Monitoring darf **Automatik darf Geräte steuern**
  ausgeschaltet bleiben.
- Erreicht der Host `http://<ISG-IP>/`?
- Ist Port `502` erreichbar?

### Leistungsaufnahme wirkt ungenau

Das ist normal, wenn das ISG weder Verdichter-Prozent noch Verdichter-Hz
liefert. Dann nutzt E3DC-Control die konfigurierten Nennwerte. Fuer bessere
Werte die Hz-Erkennung aktivieren oder eine Kennlinie in
`stiebel_isg_hz_power_map` eintragen.

### Prozessdaten-Hz nicht lesbar / Timeout

Das betrifft nur das optionale Auslesen der ISG-Webseite fuer die
Verdichterfrequenz. Modbus-Livewerte und externe Shelly-Leistungsmesser laufen
trotzdem weiter. Nach drei Web-Timeouts pausiert der Dienst die Hz-Abfrage fuer
30 Minuten und loggt nur gedrosselt. Wenn die Meldung dauerhaft stoert, im
Config-Editor bei Stiebel `Hz aus Web` auf `Nein` stellen oder den externen
Shelly-Leistungsmesser verwenden.

### Die ISG-Webseite braucht Login

`stiebel_isg_web_user` und `stiebel_isg_web_password` setzen. Der Dienst nutzt
diese Daten nur, um die Prozessdaten-Seite read-only zu lesen.

### Docker startet den Stiebel-Prozess nicht

Der Container wertet die Startbedingungen nur beim Start aus. Nach einer
Config-Aenderung:

```bash
sudo docker compose restart e3dc-control
```

Wenn der Code selbst neu ist, vorher Image ziehen oder lokal neu bauen.
