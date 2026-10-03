# Stiebel Eltron ISG / WPM Integration

Diese Dokumentation beschreibt die Stiebel-Eltron-Anbindung ab E3DC-Control
`5.0.5`. Der Dienst `e3dc-stiebel-live` ist bewusst als
Read-only-Live-Treiber gebaut. Er liest Werte aus dem ISG/WPM aus und speist
sie in die bestehende Wärmepumpen-Anzeige ein. Aktive SG-Ready- oder
Temperatur-Schreibzugriffe sind in diesem Live-Dienst nicht enthalten.
Der Wärmepumpen-Manager kann zusätzlich, experimentell und standardmäßig
ausgeschaltet, den SG-Ready-Eingang 1 des ISG schreiben; siehe
[SG Ready schreiben (experimentell)](#sg-ready-schreiben-experimentell).

## Funktionsumfang

- Live-Monitoring über Stiebel ISG / WPM per Modbus TCP.
- Anzeige von Außen-, Vorlauf-, Rücklauf-, Warmwasser- und
  Quellentemperatur.
- Auslesen von Betriebsart, Verdichterstatus, SG-Ready-Zustand und
  Tages-Energiezählern, soweit die ISG-Firmware die Register liefert.
- Schätzung der aktuellen elektrischen Leistungsaufnahme der Wärmepumpe.
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
- keine SG-Ready-Zustände,
- keine EEPROM-relevanten Sollwerte.

Das ist Absicht. Die Heizung ist kritische Infrastruktur, und viele
Temperaturparameter werden im Regler dauerhaft gespeichert. Aktive
Schreiblogik muss deshalb getrennt vom Live-Treiber, mit Watchdog und nur nach
bewusster Nutzerfreigabe getestet werden.

Der einzige Schreibweg ist der experimentelle SG-Ready-Ausgang im
Wärmepumpen-Manager. Er schreibt ausschließlich Register `4002`
(SG-Ready-Eingang 1) mit dem Wert `0` oder `1` und nur, wenn der Schalter
**SG Ready schreiben** eingeschaltet ist.

## Voraussetzungen

- Stiebel Eltron ISG im lokalen Netz.
- Modbus TCP im ISG/WPM aktiviert.
- Der E3DC-Control Host erreicht das ISG auf Port `502`.
- Für die optionale Verdichter-Hz-Erkennung muss die ISG-Webseite erreichbar
  sein. Falls die Webseite ein Login verlangt, können Benutzer und Passwort
  in der Config eingetragen werden.

## Konfiguration

Die Einrichtung erfolgt im Frontend, nicht durch manuelles Setzen von
Roh-Keys.

1. Config-Editor öffnen.
2. Bereich **Smart Home & Verbrauchsprognose** öffnen.
3. **WP-/Verbrauchslogging aktivieren** einschalten.
4. Bei **Wärmepumpen Typ** den Eintrag **Stiebel Eltron ISG / WPM** wählen.
5. **ISG IP-Adresse** eintragen, z.B. `192.0.2.233`.
6. **Port** auf `502` und **Unit-ID** auf `1` lassen, falls am ISG nichts
   anderes eingestellt ist.
7. **Hz aus Web** nur auf **Ja** stellen, wenn die ISG-Prozessdaten-Seite aus
   dem Host oder Docker-Container wirklich erreichbar ist. Bei wiederholten
   Timeouts pausiert der Dienst automatisch 30 Minuten; Modbus/Shelly-Werte
   laufen weiter. Wenn die Meldung stört, auf **Nein** lassen.
8. Optional einen **Externen Leistungsmesser** aktivieren, wenn ein separater
   Shelly die elektrische Wärmepumpenleistung misst.

Der Schalter **Automatik darf Geräte steuern** kann für reines Stiebel-Live-
Monitoring ausgeschaltet bleiben. Der Live-Dienst liest nur Werte. SG-Ready-
Schreiben bleibt separat abgesichert.

Die folgenden internen Keys sind nur für Support, Diagnose und JSON-Prüfung
gedacht. Nutzer müssen sie normalerweise nicht direkt anfassen:

| Frontend-Feld | interner Key | Hinweis |
| --- | --- | --- |
| WP-/Verbrauchslogging aktivieren | `luxtronik` | Historischer Key für den gemeinsamen Wärmepumpen-/Energy-Manager-Pfad. |
| Wärmepumpen Typ: Stiebel Eltron ISG / WPM | `wp_type` | Wird vom Frontend automatisch auf Stiebel gesetzt. |
| ISG IP-Adresse | `stiebel_isg_ip` | IP-Adresse des ISG, z.B. `192.0.2.233`. |
| Port | `stiebel_isg_port` | Modbus-TCP-Port, Standard `502`. |
| Unit-ID | `stiebel_isg_device_id` | Modbus Unit-ID, Standard `1`. |
| HZ Leistung (W) | `stiebel_isg_power_heating_w` | Nenn-/Schätzleistung in Watt für Heizbetrieb. |
| WW Leistung (W) | `stiebel_isg_power_dhw_w` | Nenn-/Schätzleistung in Watt für Warmwasser. |
| COP Schätzung | `stiebel_isg_cop_estimate` | Faktor für die angezeigte thermische Momentanleistung, wenn das ISG keine echte Wärmeleistung liefert. |
| Standby (W) | `stiebel_isg_standby_w` | Standby-Leistung der WP-Steuerung in Watt. Die Leerlaufgrenze beträgt diesen Wert plus 25 W Messspielraum, mindestens 50 und höchstens 100 W. Standard 35 W ergibt 60 W. |
| Max Hz | `stiebel_isg_max_hz` | Maximal angenommene Verdichterfrequenz für lineare Hz-Schätzung. |
| Hz/Watt Kennlinie | `stiebel_isg_hz_power_map` | Optionale Kennlinie, z.B. `0:35,15:400,30:850,60:1800`. |
| Hz aus Web | `stiebel_isg_scrape_hz_enable` | Optionales Lesen der ISG-Prozessdaten-Seite, Standard `Nein`; nach drei Timeouts pausiert der Dienst 30 Minuten. |
| Web Benutzer | `stiebel_isg_web_user` | Optionaler ISG-Weblogin-Benutzer. |
| Web Passwort | `stiebel_isg_web_password` | Optionales ISG-Weblogin-Passwort. |
| Externer Leistungsmesser | `stiebel_isg_power_meter_enable` | Nutzt einen Shelly read-only als bevorzugte elektrische WP-Leistung. |
| Zähler-IP | `stiebel_isg_power_meter_ip` | IP-Adresse des Shelly-Leistungsmessers. |
| Zählertyp | `stiebel_isg_power_meter_type` | `auto`, `shelly_3em`, `shelly_plug` oder `shelly_pm`. |
| SG Ready schreiben | `stiebel_isg_sg_ready_write` | Experimentell, Standard `0`. `1` lässt den Wärmepumpen-Manager SG-Ready-Eingang 1 (`4002`) schreiben. |

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
<INSTALL_PATH>/Installer/stiebel/stiebel_live.py
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

Nach einer Config-Änderung muss der Container einmal neu gestartet oder neu
erstellt werden, weil die Startlogik nur beim Containerstart ausgewertet wird.

Fertiges Image aktualisieren:

```bash
cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
sudo python3 ./Installer/docker_compose_update.py --compose-dir . --sudo
sudo docker compose logs --tail=80 e3dc-control
```

Für einen lokalen Entwickler-Build gelten der getrennte
`docker-compose.local.yml`-Vertrag und `pull_policy: never` aus der
[Docker-Dokumentation](Docker_Dokumentation.md). Der normale GHCR-Weg baut
kein lokales Image.

Prüfen:

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
7. Dashboard, Wärmepumpen-Seite, MQTT-Hub und R5-Diagnose lesen diese
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
| FC04 Input | `3501` | `3500` | Energiezähler für Tag/Verbrauch |
| FC04 Input | `5001` | `5000` | SG-Ready-/Reglerinformation |
| FC04 Input | `6128` | `6127` | Verdichterleistung in Prozent, falls Firmware/Register vorhanden |
| FC03 Holding | `1501..1511` | `1500..1510` | Betriebsart und Soll-/Komfortparameter, read-only im Live-Dienst |
| FC03 Holding | `4001..4003` | `4000..4002` | SG-Ready-Schalter/Eingaenge, read-only im Live-Dienst |
| FC06 Holding | `4002` | `4001` | Nur der experimentelle SG-Ready-Ausgang des Wärmepumpen-Managers, Wert `0` oder `1` |

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

Der Betriebsstatus `2501` ist ein Bitfeld mit der Zählung ab `B0`. Der
Live-Dienst wertet daraus laut Herstellerdoku `B4` Heizen, `B5` Warmwasser,
`B6` Verdichter läuft, `B7` Sommerbetrieb und `B8` Kühlbetrieb aus; `B3`
meldet dort NHZ-Stufen und wird nicht als Heizen gelesen. Zusätzlich wird das
optionale Statusregister `2520` als Kühlhinweis genutzt. Damit wird ein reiner
Kühlstatus als `Kühlen` angezeigt und nicht pauschal als `Heizen` oder
`Verdichter ein`.
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
als Fallback für abweichende Firmware erhalten.

## Leistungsaufnahme

Viele Stiebel-ISG liefern keine direkte elektrische Live-Leistung in Watt. Der
Treiber schaetzt deshalb die Aufnahme in dieser Reihenfolge:

1. Externer Shelly-Leistungsmesser, wenn aktiviert und erreichbar.
2. Direkte WPMG-Aufnahmeleistung je Phase, wenn die ISG-Firmware diese
   Register bereitstellt.
3. Verdichterleistung in Prozent (Doku `6128`, Code `6127`), wenn vorhanden.
4. Verdichterfrequenz in Hz aus der ISG-Prozessdaten-Seite, wenn aktiviert.
5. Standby-Leistung, wenn der Verdichter aus ist.
6. Konfigurierter Nennwert für WW oder Heizen als Fallback.

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
| Primäre Wärmepumpe | `6118` | `6119` | `6120` | `6117` |
| Sekundäre Wärmepumpe 1 | `6268` | `6269` | `6270` | `6267` |
| Sekundäre Wärmepumpe 2 | `6418` | `6419` | `6420` | `6417` |
| Sekundäre Wärmepumpe 3 | `6568` | `6569` | `6570` | `6567` |
| Sekundäre Wärmepumpe 4 | `6718` | `6719` | `6720` | `6717` |
| Sekundäre Wärmepumpe 5 | `6868` | `6869` | `6870` | `6867` |
| System-/Sammeladresse | `36118` | `36119` | `36120` | `36117` |

Auf manchen ISG Plus sind diese Register nicht freigeschaltet
(`Modbus exception 2`). Das ist kein Fehler im Treiber; dann faellt E3DC-Control
automatisch auf Verdichterstatus, Prozent/Hz, Shelly oder Nennwerte zurück.

Wenn eine Anlage kalibriert werden soll, ist `stiebel_isg_hz_power_map` der
genaueste Weg. Beispiel:

```text
0:35,15:400,30:850,60:1800
```

Zwischen den Stuetzpunkten wird linear interpoliert.

### Externer Shelly-Leistungsmesser

Viele Nutzer messen die Wärmepumpe bereits mit einem separaten Shelly. Für
Stiebel ist das der beste Weg, wenn ISG/Modbus keine elektrische Live-Leistung
liefert oder die Verdichter-Hz-Seite nicht erreichbar ist.

Unterstuetzt werden read-only:

- Shelly Pro 3EM / Shelly 3EM über `EM.GetStatus` oder `/status`,
- Shelly Plug / Plus Plug über `Switch.GetStatus` oder `/meter/0`,
- Shelly PM über `PM1.GetStatus`.

Der Messwert überschreibt nur die Felder `Leistung_Verdichter_W` und
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
SG-Ready-Ansteuerung ist nicht Teil dieses Live-Dienstes, sondern ein
experimenteller Ausgang des Wärmepumpen-Managers (nächster Abschnitt).

## SG Ready schreiben (experimentell)

Der Wärmepumpen-Manager (`energy_manager`) kann den SG-Ready-Eingang 1 des ISG
per Modbus TCP schreiben. Die Funktion ist experimentell und standardmäßig aus.
Sie ist ein weiterer Ausgang hinter derselben zentralen Entscheidung, die auch
einen Shelly-SG-Ready-Kontakt schaltet. PV-Überschuss, Preis- und
Pre-Dump-Freigaben, Wiedereinschaltsperre ab Verdichterstillstand,
Mindestlaufzeit, Signalhalt, Sicherheitsabschaltung und die Toleranz bei
E3DC-Datenlücken gelten unverändert. Der Ausgang hat keine eigene Regellogik.

### Voraussetzungen

- Wärmepumpen-Typ **Stiebel Eltron ISG / WPM**, **WP-/Verbrauchslogging
  aktivieren** eingeschaltet und die ISG-Adresse eingetragen.
- Im WPM: `SG READY AKTIVIERT = EIN` und `SG-READY EINGANG = MODBUS`. Die
  erhöhten Werte für den SG-Ready-Zustand 3 unter `EINSTELLUNGEN /
  ENERGIEMANAGEMENT` konservativ wählen.
- Ein Sicherheitstemperaturbegrenzer im Heizungsvorlauf. Stiebel verlangt ihn
  für SG Ready, weil Heizungswasser mit hoher Vorlauftemperatur in den
  Heizkreis gelangen kann.
- Keine zweite SG-Ready-Steuerung parallel, etwa FHEM, Home Assistant oder
  Kontakte an den SG-Ready-Klemmen. Ist in E3DC-Control ein
  Shelly-SG-Ready- oder EVU-Kontakt eingetragen, hat dieser Vorrang und das
  ISG wird nicht beschrieben.
- **Automatik darf Geräte steuern** ist eingeschaltet.
- Im Config-Editor bei Stiebel **SG Ready schreiben** auf **Ein
  (experimentell)** stellen und danach den Wärmepumpen-Manager neu starten
  (Bare Metal: `sudo systemctl restart energy_manager`, Docker: Container neu
  starten).

### Verhalten

| Zentrale Entscheidung | Eingang 1 (`4002`) | erwarteter Betriebszustand (`5001`) |
| --- | ---: | ---: |
| Normalbetrieb, Pause, Nutzer-Aus, Sicherheitsabschaltung, Ende der Datenlücken-Toleranz | `0` | `2` |
| PV-Überschuss, Preis-/Boost-Freigabe oder Warmwasser sofort | `1` | `3` |

- Geschrieben wird ausschließlich Register `4002` (Codeadresse `4001`) mit
  FC06 und dem Wert `0` oder `1`. Der Funktionsschalter `4001` und Eingang 2
  (`4003`) werden nur gelesen. Blockschreiben und alle anderen Register,
  insbesondere `1501` bis `1521`, sind gesperrt. Die Zustände 1 (Sperre) und
  4 (Maximalwerte) steuert E3DC-Control nie an.
- Geschrieben wird nur bei einem Wechsel, nie zyklisch. Nach jedem Schreiben
  liest der Manager `4002` und `5001` zurück.
- Bestätigt das Rücklesen ein geschriebenes `1` nicht, bleibt der Eingang als
  eigen, aber unbestätigt markiert, und der Manager schreibt vorerst keine
  weitere `1`, er liest nur. Diese Sperre endet, sobald die zentrale
  Entscheidung Normalbetrieb verlangt oder seit 150 Sekunden keine Freigabe mehr
  verlangt hat; eine spätere Freigabe darf dann wieder einmal schreiben.
  Verlangt die Entscheidung die Freigabe weiter, folgt nach 15 Minuten Sperre
  genau ein neuer Schreibversuch; bleibt auch er unbestätigt, beginnt eine neue
  Sperre. Als eigen gilt der unbestätigte Eingang nur innerhalb von
  150 Sekunden nach dem eigenen Schreibvorgang. Zeigt eine Leserunde in dieser
  Zeit `1`, übernimmt die nächste Freigabe der Entscheidung den Eingang ohne
  neuen Schreibvorgang; verlangt die Entscheidung seit 150 Sekunden keine
  Freigabe mehr, nimmt der Manager ihn zurück. Ein unbestätigtes `1` gilt
  innerhalb von 150 Sekunden nach dem eigenen Schreibvorgang nie als fremd.
  Danach endet der Anspruch; ein später gelesenes `1` gilt als fremd (siehe
  „Fremder Schreiber“). Nach einem Neustart gilt dasselbe Fenster ab dem im
  Merker gespeicherten Schreibzeitpunkt.
- Als abgewiesen gilt ein Schreibvorgang nur, wenn das ISG das FC06 selbst mit
  einer Modbus-Exception beantwortet. Scheitert erst das Rücklesen danach, gilt
  das geschriebene `1` als unbestätigt eigen (wie oben), eine Rücknahme bleibt
  offen.
- Übernimmt das ISG eine Rücknahme auf `0` nicht, wiederholt der Manager sie
  im Abstand von 15 Sekunden, nach drei unbestätigten Versuchen nur noch alle
  15 Minuten, und warnt einmal. Gelesen wird weiter. Schaltet der Nutzer die
  Automatik oder den Schreibschalter aus, folgt der erste Versuch sofort.
- Steht `4001` auf `0`, gibt es keine Freigabe; die Diagnose meldet „SG Ready
  im WPM nicht aktiviert“. Ist Eingang 2 gesetzt, schreibt der Manager keine
  `1`, weil daraus Zustand 4 entstünde. Wird Eingang 2 gesetzt, während der
  eigene Eingang 1 aktiv ist, nimmt der Manager den eigenen Eingang zurück.
- E3DC-Control nimmt nur einen Eingang zurück, den es selbst gesetzt hat. Das
  hält ein Merker in `/var/www/html/data/stiebel_sg_ready_state.json` fest; er
  wird nur bei einem Eigentumswechsel geschrieben. Der Merker ist an die
  Reglerkennung (`5002`) und die Geräteadresse (Unit-ID) gebunden, die
  IP-Adresse dient nur als Hinweis: Wechselt das ISG die Adresse, etwa per
  DHCP, erkennt der Manager den eigenen Eingang weiter. Die Reglerkennung
  bezeichnet den Reglertyp (zum Beispiel `449` für WPMsystem), kein einzelnes
  Gerät. Weicht sie beim Start vom Merker ab, bleibt der Merker offen, bis zwei
  aufeinanderfolgende Lesungen denselben Wert liefern; erst dann gilt ein
  gesetzter Eingang als fremd. Kommen nach 15 Minuten oder zehn abweichenden
  Lesungen keine zwei gleichen zustande, gilt der Merker ebenfalls als
  abweichend; die Diagnose meldet dann einen Konflikt. Liefert das ISG keinen Reglertyp, zählt die
  unveränderte Adresse. Beim Start, beim sauberen
  Beenden und nach dem Ausschalten des Schalters oder der Automatik setzt der
  Manager den eigenen Eingang auf `0`. Nach der Wiederkehr eines nicht
  erreichbaren ISG prüft er den eigenen Eingang und setzt ihn auf `0`, wenn
  die zentrale Entscheidung keine Freigabe mehr verlangt.
- Ausfall und Hochlauf des ISG: An einem ISG plus stand Eingang 1 nach einem
  Stromausfall des ISG wieder auf `0` und `5001` auf `2`; der Eingang ist dort
  also flüchtig. Solange das ISG ausfällt oder hochfährt (bis es alle Werte
  liefert, dauerte es dort fast 10 Minuten), hält der WPM den zuletzt
  gesetzten Zustand; E3DC-Control kann ihn in dieser Zeit nicht zurücksetzen.
  Liefert das ISG unvollständige Werte (`5001` nicht `1` bis `4`,
  Reglertyp `0` oder Ersatzwert `0x8000`), schreibt der Manager keine `1` und
  bewertet keinen Eingang als fremd. Danach gilt ein auf `0` gefallener eigener
  Eingang nicht als fremdes Rücksetzen: Verlangt die Entscheidung weiter eine
  Freigabe, setzt der Manager ihn einmal neu. Bleiben die Werte länger als
  15 Minuten unvollständig, erscheint eine Warnung. Meldet ein ISG den
  Reglertyp dauerhaft als `0`, während `4002` und `5001` gültig sind, nimmt der
  Manager einen eigenen gesetzten Eingang trotzdem zurück.
- Fremder Schreiber: Steht `4002` auf `1`, ohne dass E3DC-Control es gesetzt
  hat, bleibt der Wert unverändert, und die Diagnose meldet einen Konflikt.
  Setzt jemand den eigenen Eingang auf `0` zurück, schreibt E3DC-Control in
  derselben Freigabe-Episode nicht dagegen an. Wieder geschrieben wird erst,
  nachdem die zentrale Entscheidung Normalbetrieb verlangt oder seit
  150 Sekunden keine Freigabe mehr verlangt hat, und frühestens 15 Minuten nach
  dem fremden Rücksetzen; wiederholt es sich, verdoppelt sich diese Wartezeit
  bis höchstens 2 Stunden. Das Ende der Episode setzt sie nicht zurück, erst
  2 Stunden ohne fremdes Rücksetzen. Ausgenommen ist der Rückfall auf `0` nach
  Ausfall oder Hochlauf des ISG.
- Die Heiz- und Warmwasseraktivität, die der Live-Dienst aus `2501` liest, ist
  Eigenbetrieb des WPM und kein Hinweis auf eine fremde SG-Ready-Freigabe. Für
  den ISG-Ausgang zählen allein `4002` und `5001`, für einen Shelly-SG-Ready-
  oder EVU-Kontakt an einer Stiebel-Anlage allein der gelesene Relaiszustand:
  Steht der SG-Kontakt laut Rücklesen auf Ein, ohne dass eine Freigabe, eine
  Pause, ein manueller Boost oder Warmwasser sofort aktiv ist, schaltet der
  Manager nach 60 Sekunden ohne Befehl ausschließlich den SG-Kontakt aus; ein
  EVU-/Pause-Kontakt bleibt unberührt. Steht das Relais danach ohne eigenen
  Befehl wieder auf Ein, folgt nach dem zweiten erfolglosen Ausschalten keines
  mehr; die Diagnose (`shelly_sg_relay_guard`) meldet dann einen fremden
  Schreiber am SG-Relais. Bestätigt der Shelly das Ausschalten zweimal nicht,
  schaltet der Manager ebenfalls nicht weiter und meldet das. Bei
  ausgeschalteter Automatik schaltet der Manager nicht, er warnt nur; mit dem
  Wiedereinschalten der Automatik ist dieser Hinweis erledigt.
- Ist das ISG nicht erreichbar oder weist es einen Zugriff mit einer
  Modbus-Exception ab, gibt es keine Freigabe; ein neuer Versuch folgt
  frühestens nach 15 Sekunden. Das Log unterscheidet „nicht erreichbar“, „hat
  abgewiesen“ (Exception auf den Schreibbefehl) und „FC06 quittiert,
  Rücklesen abgewiesen“ (Wirkung unbekannt).
- Die Diagnose zählt die Schreibvorgänge je Tag. Ab 24 am Tag erscheint eine
  Warnung, die nichts sperrt.

### Schaltfenster und Taktschutz

Der Ausgang schaltet nur, wenn die zentrale Entscheidung wechselt. Wie lange
eine Freigabe mindestens steht, bestimmen dieselben Einstellungen wie beim
Shelly-SG-Ready-Kontakt:

| Einstellung | Wirkung | Standard |
| --- | --- | ---: |
| Signalhalt (fest) | Eine gesetzte Freigabe steht mindestens so lange. | 10 min |
| `stop_delay_minutes` | So lange muss die PV-Deckung fehlen, bevor eine PV-Freigabe endet. | 10 min |
| `wp_min_runtime_min` | Mindestlaufzeit ab gemessenem Verdichterstart; eine PV-Freigabe endet wegen fehlender Deckung frühestens danach. | 30 min |
| `wp_restart_block_min` | Wiedereinschaltsperre ab gemessenem Verdichterstillstand. | 20 min |

Ist das Temperaturziel erreicht, endet die Freigabe frühestens nach dem Signalhalt.
Der Warmwasser-Timer schaltet den SG-Ready-Ausgang (ISG oder Shelly-Kontakt)
nicht selbst: Seine Nachfrage startet über denselben zentralen Startweg mit
Wiedereinschaltsperre, Signalhalt und Budget und endet über dieselbe
Rücknahme.
„Warmwasser sofort“ startet am SG-Ready-Ausgang (ISG oder Shelly-Kontakt)
ohne PV-Überschuss und unabhängig von der Budgethöhe über denselben zentralen
Startweg. Wie der PV- und Preis-Boost braucht er aber ein frisches Budget des
Storage Managers; fehlt es, wartet der Befehl (Log: `storage_budget_stale`).
Wie bei einer Luxtronik-Anlage braucht der Start eingeschaltete Automatik,
keinen manuellen Wärmepumpen-Boost, gültige E3DC-Livedaten, ein gültiges
Warmwasser-Ist unter dem Ziel und keine Sicherheitsabschaltung (Speicher unter
Mindest-SoC minus 5 %, Netzbezug über 2500 W, Hardware- oder Quellenschutz). Im Notstrom- oder
Inselbetrieb und während der Speicher die Notstromreserve hält, startet und
hält der Knopf nicht. Es gelten die Wiedereinschaltsperre ab
Verdichterstillstand und, wie beim manuellen Wärmepumpen-Boost, nach jeder
Rücknahme eine Sperre von mindestens 10 Minuten bzw. `wp_restart_block_min`,
auch wenn der Verdichter nicht lief. Eine laufende eigene Preis- oder PV-Pause
übernimmt der Befehl, eine neue beginnt während des Befehls nicht. Eine fremde
Sperre, etwa ein von außen ausgeschalteter Pause-Kontakt am Shelly, überschreibt
er nicht; die Diagnose meldet sie. Der Befehl endet frühestens nach dem
Signalhalt von 10 Minuten (eine Mindestlaufzeit gilt dafür nicht), wenn die
eingestellte Dauer abläuft, der Nutzer ihn stoppt oder das Warmwasser das Ziel
erreicht hat; mit dem erreichten Ziel ist der Befehl erledigt (ein
Warmwasser-Zyklus je Befehl), auch wenn inzwischen ein Pre-Dump das Signal
hält. Hardware- oder Quellenschutz und Nutzer-Aus beenden ihn. Die übrigen
Sicherheitsabschaltungen, eine E3DC-Datenlücke und ein seit mehr als 5 Minuten
nicht mehr frisches Speicher-Budget unterbrechen ihn nur: Solange die Dauer
läuft, startet er nach der Sperre neu.
Bei SG Ready bewirkt der Knopf den verstärkten Betrieb der ganzen Anlage
(Zustand 3); das Warmwasser regelt der WPM selbst.
Sicherheitsabschaltung, Nutzer-Aus und das Ende der Datenlücken-Toleranz
wirken unabhängig von diesen Zeiten. Wer statt häufiger Wechsel längere
Fenster von etwa ein bis zwei Stunden möchte, erhöht `wp_min_runtime_min`
(zum Beispiel auf 60 bis 120) und bei Bedarf `wp_restart_block_min`. Dann läuft
die Wärmepumpe bei nachlassender PV entsprechend länger aus Speicher oder Netz
weiter. Eine eigene Fensterlogik gibt es nicht.

### Diagnose

`/var/www/html/ramdisk/luxtronik.json` enthält den Block `stiebel_sg_ready`
ohne Adressen und Zugangsdaten, unter anderem:

| Feld | Bedeutung |
| --- | --- |
| `target_input1` | Soll von Eingang 1 aus der zentralen Entscheidung |
| `input1` | gelesener Eingang 1 (`4002`) |
| `operating_state` | gelesener Betriebszustand (`5001`) |
| `sg_ready_switch` | gelesener Funktionsschalter (`4001`) |
| `input2` | gelesener Eingang 2 (`4003`) |
| `last_write_ts`, `writes_today` | letzter Schreibzeitpunkt, Schreibvorgänge heute |
| `reason`, `reason_text` | Grund des letzten Ergebnisses |
| `conflict` | fremder Schreiber erkannt, Art und Zeitpunkt |
| `own_input`, `own_input_confirmed` | eigener Eingang gesetzt, durch Rücklesen bestätigt |
| `unconfirmed_write_latched` | unbestätigtes `1`; neuer Schreibversuch erst nach Normalbetrieb, nach dem Ende der Freigabeanforderung oder nach 15 Minuten |
| `release_pending`, `release_backoff_active` | Rücknahme offen; nach mehreren unbestätigten Versuchen läuft die Wartezeit |
| `writes_warning` | ab 24 Schreibvorgängen am Tag |
| `isg_reachable`, `isg_ready` | ISG erreichbar, liefert vollständige SG-Ready-Werte |
| `owner_identity` | Bindung des Merkers: `controller_id`, `address`, `pending` oder `mismatch` |

Ist der Schalter aus, steht dort `active: false` mit dem Grund.

### Kurze Testanleitung

1. Schalter aus lassen und in `/var/www/html/ramdisk/stiebel_isg.json`
   prüfen: `stiebel_sg_ready_switch` = `1`, `stiebel_sg_ready_input1` = `0`,
   `stiebel_sg_ready_input2` = `0`, `stiebel_sg_ready_state` = `2`.
   `stiebel_controller_id` sollte einen Reglertyp aus der Herstellerdoku
   liefern (zum Beispiel `449` für WPMsystem); das bestätigt auch den
   Adressversatz.
2. Schalter einschalten, den Wärmepumpen-Manager neu starten und im Block
   `stiebel_sg_ready` prüfen: `active` = `true`, `writes_today` = `0`.
3. Bei PV-Überschuss mit laufender Wärmefreigabe: `input1` = `1`,
   `operating_state` = `3`; die Zeit bis zur Umschaltung notieren.
4. Nach dem Ende der Freigabe: `input1` = `0`, `operating_state` = `2`.
5. Neustart-Test: Während `input1` = `1` den Wärmepumpen-Manager neu starten.
   Spätestens nach dem Start muss `input1` wieder `0` sein.
6. Optional: Während `input1` = `1` das ISG kurz stromlos machen. Während des
   Hochlaufs zeigt der Block `isg_ready` = `false` und `writes_today` bleibt
   gleich; danach `input1` und `operating_state` notieren.
7. Bei Problemen erwarteten und gelesenen Zustand, den betroffenen
   Testschritt und die beobachtete Verzögerung nennen. Ein **Diagnosepaket
   aus der Installationszentrale** beifügen und vor dem Teilen prüfen; darin
   ist insbesondere der Block `stiebel_sg_ready` aus `luxtronik.json` relevant.

### Grenzen

- Die Herstellerdoku sagt nichts dazu, ob die SG-Ready-Register flüchtig oder
  dauerhaft gespeichert werden und ob es einen Watchdog gibt. An einem ISG plus
  war Eingang 1 nach einem Stromausfall des ISG wieder `0` (flüchtig). Ob das
  für jede ISG-Variante gilt und was ein Neustart des WPM allein bewirkt, ist
  nicht belegt.
- Einen Rückfall bei Verbindungsverlust gibt es nicht: Fällt der Host oder das
  Netz aus, während Eingang 1 auf `1` steht, bleibt Zustand 3 bestehen, bis der
  Wärmepumpen-Manager wieder läuft. Während Ausfall und Hochlauf des ISG hält
  der WPM den zuletzt gesetzten Zustand. In beiden Fällen heizt die Wärmepumpe
  bis zu den im WPM hinterlegten erhöhten Werten, auch mit Netzstrom.
- `5001` folgte an einem ISG plus dem Schreiben von `4002` praktisch sofort.
  Dokumentiert ist das Zeitverhalten nicht.
- Die englische Fassung der Herstellerdoku nennt für `4001` vertauschte
  Werte. E3DC-Control folgt der deutschen Fassung (`1` = EIN).
- Die Reglerkennung `5002` unterscheidet Reglertypen, keine einzelnen Geräte.
  Zwei ISG desselben Typs mit derselben Unit-ID kann der Merker nach einem
  Adresswechsel nicht auseinanderhalten.
- Im HA-Betrieb liegt der Merker in `data/` und wird mit den übrigen Daten zur
  anderen Instanz synchronisiert, standardmäßig stündlich
  (`ha_sync_interval`). Übernimmt die andere Instanz, nimmt sie einen Eingang
  zurück, den der zuletzt übertragene Merker als eigen ausweist. Wurde der
  Eingang erst nach der letzten Synchronisation gesetzt, meldet sie ihn als
  fremd und lässt ihn stehen. Die erste Instanz nimmt ihn beim nächsten Start
  zurück, sofern ihr eigener Merker dann noch vorliegt und sie schreiben darf.
  Andernfalls bleibt der Eingang stehen und wird als fremd gemeldet.

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

Prüfen:

- Ist im Config-Editor unter **Smart Home & Verbrauchsprognose** der Schalter
  **WP-/Verbrauchslogging aktivieren** eingeschaltet?
- Ist bei **Wärmepumpen Typ** **Stiebel Eltron ISG / WPM** ausgewaehlt?
- Ist die **ISG IP-Adresse** korrekt eingetragen?
- Für reines Live-Monitoring darf **Automatik darf Geräte steuern**
  ausgeschaltet bleiben.
- Erreicht der Host `http://<ISG-IP>/`?
- Ist Port `502` erreichbar?

### Leistungsaufnahme wirkt ungenau

Das ist normal, wenn das ISG weder Verdichter-Prozent noch Verdichter-Hz
liefert. Dann nutzt E3DC-Control die konfigurierten Nennwerte. Für bessere
Werte die Hz-Erkennung aktivieren oder eine Kennlinie in
`stiebel_isg_hz_power_map` eintragen.

### Prozessdaten-Hz nicht lesbar / Timeout

Das betrifft nur das optionale Auslesen der ISG-Webseite für die
Verdichterfrequenz. Modbus-Livewerte und externe Shelly-Leistungsmesser laufen
trotzdem weiter. Nach drei Web-Timeouts pausiert der Dienst die Hz-Abfrage für
30 Minuten und loggt nur gedrosselt. Wenn die Meldung dauerhaft stoert, im
Config-Editor bei Stiebel `Hz aus Web` auf `Nein` stellen oder den externen
Shelly-Leistungsmesser verwenden.

### Die ISG-Webseite braucht Login

`stiebel_isg_web_user` und `stiebel_isg_web_password` setzen. Der Dienst nutzt
diese Daten nur, um die Prozessdaten-Seite read-only zu lesen.

### Docker startet den Stiebel-Prozess nicht

Der Container wertet die Startbedingungen nur beim Start aus. Nach einer
Config-Änderung:

```bash
cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
sudo python3 ./Installer/docker_compose_update.py \
  --compose-dir . --sudo --recreate-current
```

Wenn der Code selbst neu ist, den Host-Helfer ohne `--recreate-current`
aufrufen; dadurch wird das gewählte GHCR-Image vor dem Start ausdrücklich
gezogen und geprüft.

### Startfreigabe und Leerlauf

Bei aktivem SG-Ready-Schreiben gilt ein Startfenster von mindestens 150 Sekunden.
Es deckt die durchgehende Startverzögerung `pv_boost_delay` und danach mindestens
120 Sekunden für die Anlaufsperre des Dienstes, Schreiben und Verdichterannahme ab. Eine längere Verzögerung
verlängert das Fenster; ab 150 Sekunden warnt die Konfigurationsprüfung vor der
langen Budgetbindung. Die Wärmepumpe entscheidet weiterhin selbst über den Start.

Der Betrag von `grid_start_limit` wird mit dem freien Verbraucherbudget nach der
Akkuladung verglichen, nicht mit der Einspeisung am Netzpunkt. Die Zuteilung
benötigt mindestens 1500 W. Kleinere Beträge erzeugen eine beratende Warnung.

Nach einer ungenutzten Freigabe kann eine neue Anfrage frühestens 60 Sekunden nach
deren Ende und nach der Wiederholsperre entstehen: Der Ausgang muss frisch als
zurückgenommen bestätigt sein, der Verdichter nachweislich stehen und die frische
Leistung unter der Leerlaufgrenze liegen. Eine interne Bereitschaftsmeldung löst
die Sperre, ohne einen zusätzlichen Gerätebefehl auszulösen. Fehlende oder alte
Leistungsdaten gelten nie als Leerlauf. Automatik-Aus, Schutzgrenzen,
Mindestlaufzeit und Wiedereinschaltsperre behalten Vorrang.
