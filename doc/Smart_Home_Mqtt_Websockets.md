# E3DC-Control: Smart Home MQTT & WebSocket Architektur

E3DC-Control nutzt für Web, MQTT und externe Automationen eine entkoppelte RAM-Disk-Schnittstelle. Die nativen Python-Dienste halten RSCP, Web, MQTT und Regelung getrennt, damit Netzwerkprobleme nicht direkt die Speicher- oder Wallboxregelung blockieren.

## 1. Die Kern-Architektur (Atomares JSON)
Der Dienst `e3dc-live` liest über RSCP die Live-Daten aus dem E3DC-Speicher und erzeugt jede Sekunde ein JSON-Dokument in der RAM-Disk (`/var/www/html/ramdisk/live_data_py.json`).

Der Schreibvorgang erfolgt **atomar** (durch Schreiben in eine `.tmp` Datei und anschließendes `rename`), sodass Drittprogramme niemals unvollständige Dateien einlesen können.

---

## 2. Echtzeit-Dashboard (WebSockets)
Das Webportal empfängt Änderungen über den WebSocket-Dienst. Dadurch bleiben
auch mehrere geöffnete Geräte ohne enges HTTP-Polling aktuell.

### Der WebSocket Daemon (`e3dc_websocket.py`)
Dieser Python-Dienst (als Systemd `e3dc-websocket.service` eingerichtet) wartet
ausschließlich auf der lokalen Loopback-Adresse `127.0.0.1:8765`. Browser
greifen weder bei einer nativen Installation noch im Docker-Betrieb direkt auf
diesen internen Port zu. Sobald ein Client das Dashboard öffnet, streamt der
Dienst Änderungen im JSON-File verzögerungsfrei an das Dashboard.
Das Dashboard kann dadurch extrem flüssige Ladeanimationen im Takt von unter einer Sekunde berechnen.

### Apache Reverse Proxy (Der Cloudflare-Trick)
Da moderne Webbrowser unsichere Verbindungen (`ws://`) blockieren, wenn die
Webseite über SSL (`https://`, z. B. via Cloudflare Tunnel) geladen wird,
konfiguriert der Installer den Apache-Webserver automatisch als Reverse Proxy.

Der Browser verwendet am gleichen Host und Port wie das Dashboard den Pfad
`/ws`. Apache tunnelt diesen Verkehr intern an `127.0.0.1:8765` weiter. Damit
bleibt der Python-Port selbst im LAN geschlossen; TLS und die externe
Erreichbarkeit enden am Webserver.

**Installation:**
Der Dienst wird vollautomatisch mit dem Befehl "Systempakete installieren" (Menüpunkt 3) im Installer aktiviert.

---

## 3. Smart Home Integration (MQTT Hub)
Das System bietet nun eine dedizierte Push-Schnittstelle für externe Smart Home Systeme (Home Assistant, ioBroker, Node-RED, etc.).

### Der MQTT Daemon (`e3dc_mqtt_hub.py`)
Dieser Dienst überwacht die Live-Daten des Dashboards und sendet Änderungen an den MQTT-Broker.

**Er sendet in drei Formaten:**
1. **Gebündelt:** Das komplette Dashboard-JSON bleibt auf `e3dc/live` erhalten.
2. **Home-Assistant-State:** `e3dc/ha/state` enthält einen kompakten EMS-Zustand mit PV, Netz, Speicher, Wallbox, Wärmepumpe, Heizstab, Freigaben und Regelstatus.
3. **Einzel-Werte:** Die wichtigsten Werte werden zusätzlich als einfache Topics unter `e3dc/ha/...` veröffentlicht, z.B. `e3dc/ha/pv_w`, `e3dc/ha/battery_soc` oder `e3dc/ha/free_for_consumers_w`.

Home Assistant erhält über MQTT Auto-Discovery automatisch ein Gerät **E3DC-Control EMS**. Dieses Gerät stellt neben klassischen Sensoren auch binäre Freigaben bereit:

- `Wallbox Freigabe`
- `Wärmepumpe Freigabe`
- `Heizstab Freigabe`
- `Pre-Dump aktiv`
- `Abregelschutz aktiv`
- `Preisfenster aktiv`

Damit kann Home Assistant Automationen sehr einfach formulieren: E3DC-Control sagt, wann Energie sinnvoll verfügbar ist, Home Assistant schaltet herstellerspezifische Geräte wie Stiebel-ISG, KNX, Shelly oder andere Wärmepumpen-Bridges.

### Kontrollierte Eingangs-Telemetrie
Der MQTT-Hub nimmt bewusst **keine freien Steuerbefehle** an. Er akzeptiert aber Messwerte auf einer festen Allowlist und schreibt diese atomar nach `/var/www/html/ramdisk/mqtt_ha_inbound.json`. `get_live_json.php` mischt frische Werte in das Dashboard ein.

Erlaubte Eingangs-Topics:

```text
e3dc/in/heatpump/power_w
e3dc/in/heatpump/mode
e3dc/in/heatpump/state
e3dc/in/heatpump/ww_temp
e3dc/in/heatpump/ww_target_temp
e3dc/in/heatpump/flow_temp
e3dc/in/heatpump/return_temp
e3dc/in/heatpump/outside_temp
e3dc/in/heatpump/heat_kw
e3dc/in/heatpump/electric_w
e3dc/in/heatpump/boost_active

e3dc/in/heater/power_w
e3dc/in/heater/water_temp
e3dc/in/heater/target_temp
e3dc/in/heater/state
e3dc/in/heater/mode

e3dc/in/wallbox/power_w
e3dc/in/wallbox/plugged
e3dc/in/wallbox/charging
e3dc/in/wallbox/soc
e3dc/in/wallbox/range_km

e3dc/in/wallbox1/power_w
e3dc/in/wallbox2/power_w

e3dc/in/house/extra_power_w
```

### Welche Werte soll Home Assistant senden?
Die folgenden Werte sind die für Nutzer relevanten MQTT-Eingangswerte. Nur die
als **direkt wirksam** markierten Werte werden aktuell im Dashboard angezeigt
und optional in Live-History/Prognose uebernommen.

| Topic | Einheit / Typ | Quelle in Home Assistant | Wirkung in E3DC-Control |
| --- | --- | --- | --- |
| `e3dc/in/heatpump/power_w` | W | elektrische Leistungsaufnahme der Wärmepumpe | direkt wirksam: WP-Kachel, E-Flow, Live-History, Prognose; wird aus dem reinen Hausverbrauch herausgerechnet |
| `e3dc/in/heatpump/ww_temp` | Grad C | Warmwasser-Isttemperatur | direkt wirksam: WP-Kachel und Detailanzeige |
| `e3dc/in/heatpump/ww_target_temp` | Grad C | Warmwasser-Solltemperatur | direkt wirksam: WP-Kachel und Detailanzeige |
| `e3dc/in/heatpump/flow_temp` | Grad C | Vorlauf | direkt wirksam: WP-Diagnose/Detailanzeige |
| `e3dc/in/heatpump/return_temp` | Grad C | Ruecklauf | direkt wirksam: WP-Diagnose/Detailanzeige |
| `e3dc/in/heatpump/outside_temp` | Grad C | Aussen-/Zulufttemperatur | direkt wirksam: WP-Diagnose/Detailanzeige |
| `e3dc/in/heatpump/heat_kw` | kW | thermische Leistung, falls vorhanden | direkt wirksam: Diagnosewert |
| `e3dc/in/heatpump/mode` | Text | Betriebsart, z.B. `WW`, `Heizen`, `Sommer` | direkt wirksam: lesbarer WP-Status |
| `e3dc/in/heatpump/state` | Text | optionaler Status | Telemetrie, Fallback für Status |
| `e3dc/in/heatpump/boost_active` | Bool | externer Boost aktiv | Telemetrie, Freigabe-/Statussensor |
| `e3dc/in/heater/power_w` | W | Heizstab-/ELWA-Leistung | direkt wirksam: Heizstab-Kachel, E-Flow, Live-History, Prognose; wird aus dem reinen Hausverbrauch herausgerechnet |
| `e3dc/in/heater/water_temp` | Grad C | Wasser-Isttemperatur | direkt wirksam: Heizstab-Kachel |
| `e3dc/in/heater/target_temp` | Grad C | Wasser-Solltemperatur | direkt wirksam: Heizstab-Kachel |
| `e3dc/in/heater/state` | Text | optionaler Status | Telemetrie |
| `e3dc/in/heater/mode` | Text | optionaler Modus | Telemetrie |
| `e3dc/in/wallbox/power_w` | W | externe Wallbox-Leistung für Wallbox 1 | direkt wirksam: WB1-Kachel, E-Flow, Live-History, Prognose; wird aus dem reinen Hausverbrauch herausgerechnet |
| `e3dc/in/wallbox1/power_w` | W | externe Wallbox-1-Leistung | Alias für `wallbox/power_w`, sinnvoll bei zwei Wallboxen |
| `e3dc/in/wallbox2/power_w` | W | externe Wallbox-2-Leistung | direkt wirksam: WB2-Kachel, E-Flow, Live-History, Prognose; wird aus dem reinen Hausverbrauch herausgerechnet |
| `e3dc/in/wallbox/plugged` | Bool | Fahrzeug verbunden | direkt wirksam: Status/Automation für WB1 |
| `e3dc/in/wallbox/charging` | Bool | Fahrzeug lädt | direkt wirksam: Status/Automation für WB1 |
| `e3dc/in/wallbox/soc` | Prozent | Fahrzeug-SoC | direkt wirksam als bestätigter Wallbox-SoC; für reine Fahrzeug-SoC-Quellen bleiben die dedizierten `mqtt_hub_sub_soc_topic` Felder möglich |
| `e3dc/in/wallbox/range_km` | km | Fahrzeug-Reichweite | direkt wirksam in der Fahrzeug-/Wallboxanzeige |

### Direkter evcc/openWB-Leistungswert ohne Home Assistant

Wenn Home Assistant nicht beteiligt sein soll, kann der MQTT-Hub die Wallbox-Leistung
direkt abonnieren. In der Web-Konfiguration steht das unter
`Schnittstellen & MQTT` -> `Wallbox-Leistung per MQTT`.

Beispiel evcc:

```text
wb_ip    = 192.0.2.126:1883
wb_topic = evcc/loadpoints/1/chargePower
```

Der Fahrzeug-SoC bleibt getrennt:

```text
mqtt_hub_sub_soc_topic = evcc/loadpoints/1/vehicleSoc
```

`chargePower` gehört also nicht in das SoC-Feld, sondern in das
Wallbox-Leistungs-Topic.

Ab 4.9.9 wird dieser direkte Leistungswert zugleich in die Wallbox-Messdatei
und in die kontrollierte Inbound-Telemetrie geschrieben. Dashboard,
Hausverbrauchsbereinigung, Historie und Planung sehen dadurch dieselbe reale
Wallboxleistung. Nicht endliche Payloads wie `NaN`, `Infinity`, leere Strings
oder unlesbare JSON-Werte werden ignoriert und ueberschreiben keine gueltigen
Messwerte.

Wenn Broker, Topic, Benutzer oder Passwort im Config-Editor geaendert werden,
leert die WebUI den Ramdisk-Konfigurationscache und startet den Dienst
`e3dc-mqtt-hub` neu, damit die neuen Abos ohne manuellen Neustart greifen.

Bool-Werte können als `true/false`, `on/off`, `1/0` oder `ein/aus`
gesendet werden. Zahlen dürfen mit Punkt oder Komma kommen, z.B. `1234.5`
oder `1234,5`.

Beispiel Home-Assistant-Automation für eine Stiebel-/KNX-Wärmepumpe:

```yaml
alias: E3DC-Control sendet WP-Leistung
trigger:
  - platform: state
    entity_id: sensor.stiebel_eltron_wp_leistung
action:
  - service: mqtt.publish
    data:
      topic: e3dc/in/heatpump/power_w
      payload: "{{ states('sensor.stiebel_eltron_wp_leistung') | float(0) }}"
      retain: false
```

Beispiel für Warmwasser-Temperatur:

```yaml
action:
  - service: mqtt.publish
    data:
      topic: e3dc/in/heatpump/ww_temp
      payload: "{{ states('sensor.stiebel_eltron_warmwasser_ist') | float(0) }}"
      retain: false
```

Beispiel für externe Wallbox-Leistung aus Home Assistant:

```yaml
alias: E3DC-Control sendet Wallbox-1-Leistung
trigger:
  - platform: state
    entity_id: sensor.openwb_ladeleistung
action:
  - service: mqtt.publish
    data:
      topic: e3dc/in/wallbox/power_w
      payload: "{{ states('sensor.openwb_ladeleistung') | float(0) }}"
      retain: false
```

Bei zwei externen Wallboxen wird die zweite Leistung auf
`e3dc/in/wallbox2/power_w` gesendet. Beide Werte werden nur als Messwerte
genutzt und aus dem reinen Hausverbrauch herausgerechnet.

**Vorteile der Entkopplung:**
Sollte Home Assistant neu starten oder das Netzwerk kurzzeitig ausfallen, läuft die E3DC-Control-Regelung unbeeindruckt weiter. MQTT liefert Zusatzdaten und Automationssignale, ersetzt aber nicht den lokalen RSCP-Regelkern.

### Einrichtung
Setzen und prüfen Sie zuerst den absoluten Produktpfad wie in
[Installer](Installer.md) beschrieben.

1. Starten Sie den Installer: `bash "$E3DC_INSTALL_PATH/e3dc-setup"`
2. Navigieren Sie zu den **Erweiterungen** und wählen Sie Punkt **104 (Smart Home MQTT-Hub einrichten)**.
3. Geben Sie die IP-Adresse Ihres Smart-Home-Servers (Broker) ein.
4. (Optional) Passen Sie Port, Benutzername, Passwort und das Basis-Topic an.

Die Daten werden sofort nach Abschluss des Setups an Ihr System gestreamt!
