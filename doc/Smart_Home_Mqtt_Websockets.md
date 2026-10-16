# E3DC-Control: Smart Home MQTT & WebSocket Architektur

E3DC-Control nutzt für Web, MQTT und externe Automationen eine entkoppelte RAM-Disk-Schnittstelle. Die nativen Python-Dienste halten RSCP, Web, MQTT und Regelung getrennt, damit Netzwerkprobleme nicht direkt die Speicher- oder Wallboxregelung blockieren.

## 1. Die Kern-Architektur (Atomares JSON)
Der Dienst `e3dc-live` liest über RSCP die Live-Daten aus dem E3DC-Speicher und erzeugt jede Sekunde ein JSON-Dokument in der RAM-Disk (`/var/www/html/ramdisk/live_data_py.json`).

Der Schreibvorgang erfolgt **atomar** (durch Schreiben in eine `.tmp` Datei und anschließendes `rename`), sodass Drittprogramme niemals unvollständige Dateien einlesen können.

---

## 2. Echtzeit-Dashboard (WebSockets)
Das Webportal liest Änderungen derzeit über regelmäßige HTTP-Abfragen. Der
WebSocket-Dienst ist installiert; die Weboberfläche nutzt ihn derzeit nicht.

Der bisherige E3DC-WebSocket-Dienst wird mit Version 5.5.6 entfernt. Für
externe Smart-Home-Anbindungen steht der MQTT-Hub mit den dokumentierten
Topics zur Verfügung; das Webportal nutzt weiterhin HTTP-Abfragen. Die
Luxtronik-Anbindung bleibt unverändert.

### Der WebSocket Daemon (`e3dc_websocket.py`)
Dieser Python-Dienst (als Systemd `e3dc-websocket.service` eingerichtet) wartet
ausschließlich auf der lokalen Loopback-Adresse `127.0.0.1:8765`. Browser
greifen weder bei einer nativen Installation noch im Docker-Betrieb direkt auf
diesen internen Port zu. Der Dienst ist als Pflichtdienst installiert; die
Weboberfläche nutzt ihn derzeit nicht und aktualisiert ihre Daten durch
regelmäßige HTTP-Abfragen.

### Apache Reverse Proxy (Der Cloudflare-Trick)
Da moderne Webbrowser unsichere Verbindungen (`ws://`) blockieren, wenn die
Webseite über SSL (`https://`, z. B. via Cloudflare Tunnel) geladen wird,
konfiguriert der Installer den Apache-Webserver automatisch als Reverse Proxy.

Für WebSocket-Clients steht am gleichen Host und Port wie das Dashboard der
Pfad `/ws` bereit. Apache tunnelt diesen Verkehr intern an `127.0.0.1:8765` weiter. Damit
bleibt der Python-Port selbst im LAN geschlossen; TLS und die externe
Erreichbarkeit enden am Webserver.

**Installation:**
Der Dienst wird über Hauptmenü **1) Installation / Update** eingerichtet und aktiviert.

---

## 3. Smart Home Integration (MQTT Hub)
Das System bietet nun eine dedizierte Push-Schnittstelle für externe Smart Home Systeme (Home Assistant, ioBroker, Node-RED, etc.).


### Freigabeliste für generische MQTT-Fahrzeuge

`mqtt_vehicle_allowlist` begrenzt ausschließlich `<Basis>/vehicle/<NAME>/<ATTRIBUT>`.
Beispiel: `Auto_A,Auto_B`. Die kommagetrennten NAME-Segmente werden exakt und mit
Groß-/Kleinschreibung verglichen; Leerzeichen um Einträge werden entfernt.
Leere Einträge, Wildcards (`+`, `#`), Schrägstriche und Steuerzeichen sind ungültig.
Fehlend oder leer (Standard) lässt den bisherigen Eingang offen. Eine ungültige
Liste sperrt den generischen Eingang und erzeugt eine beratende Diagnose.

Die Einstellung wirkt ab dem nächsten Lesen der gespeicherten Konfiguration,
ohne Neuverbindung des Hubs. Sie gilt ebenso für retained Nachrichten. Eine
Ablehnung wertet keine Payload aus, schreibt keine Fahrzeugdatei und erneuert
keine Frische. Frühere MQTT-Werte ausgeschlossener Fahrzeuge und daraus
abgeleitete SoC-Schätzungen verlieren ihre Regelgültigkeit; der Anzeigecache
kann sie nicht wieder freigeben. Datensätze werden dabei nicht gelöscht.
Neue MQTT-Werte erhalten additive Herkunftsangaben. Alte MQTT-Datensätze
unterscheiden dedizierte und generische Eingänge nicht. Bei aktiver Liste sind
sie deshalb bis zu einer neuen Nachricht mit belegter Herkunft nicht regelgültig,
auch wenn die ID als dedizierter Name konfiguriert oder in der Liste enthalten ist.
Eine neue dedizierte oder gelistete generische SoC-Nachricht bindet die Herkunft;
ein reiner Retain bestätigt weiterhin keinen frischen Regelwert. Ohne Liste
bleibt das Lesen der Altwerte unverändert.

Ein Fahrzeug, dessen MQTT-SoC dadurch nicht regelgültig ist, behandelt die
Ladeplanung wie jedes Fahrzeug ohne bestätigten SoC: Bei aktiver Zielplanung
plant sie konservativ mit 0 % und damit mit der vollen Energiemenge bis zum Ziel.
Geplante Ladefenster bleiben bestehen, auch solche mit Netzbezug (siehe
`Native_Wallbox.md`, Abschnitt „Fahrzeug-SoC-Vertrag“). Das betrifft ausgeschlossene
Fahrzeuge und Altwerte bis zur nächsten Nachricht mit belegter Herkunft. Vor dem
Aktivieren der Liste deshalb die Namen aller genutzten Fahrzeuge prüfen oder den
Ist-SoC manuell bestätigen.

Überlappt ein dediziertes SoC-Topic mit `<Basis>/vehicle/<NAME>/<ATTRIBUT>`, gilt
ohne Liste die bisherige Reihenfolge: der generische NAME und dessen ATTRIBUT.
Mit aktiver Liste gilt der konfigurierte dedizierte Name und SoC als Attribut;
die Liste blockiert diesen dedizierten Eingang nicht. Das gilt auch bei
ungültiger Listensyntax.

Die zwei dedizierten SoC-Topics, Bluelink und native openWB-Fahrzeugdaten liegen
außerhalb dieses Geltungsbereichs. Bei aktiver Liste überschreibt ein generischer Eingang keinen
fremden oder dedizierten SoC-Datensatz mit derselben ID; hierfür unterschiedliche
Namen wählen. Die gemeinsame Schreibsperre mit Bluelink bleibt erhalten.

`vehicle_input` im Hubstatus enthält nur einen aggregierten Ablehnungszähler
(seit Dienststart) und den letzten festen Grund: `not_allowed`,
`invalid_allowlist` oder `source_conflict`. Die Liste und freie Topicwerte werden
im Diagnoseexport maskiert bzw. pseudonymisiert. Kennungen, Positionen und
Kilometerstände gehören nicht in Logs oder Diagnosestatus; die lokale
Fahrzeugdatei behält ihre für die Zuordnung benötigten Funktionsdaten.

### Der MQTT Daemon (`e3dc_mqtt_hub.py`)
Dieser Dienst überwacht die Live-Daten des Dashboards und sendet Änderungen an den MQTT-Broker. Die Option „Werte zyklisch wiederholen (s)“ (`mqtt_hub_repeat_interval_s`, Standard `0` = aus) ergänzt Wiederholungen von `ha/state` und Einzeltopics bei gültigen, frischen Live-Daten; Einzelheiten und Budgetfrische stehen im Abschnitt „Werte zyklisch wiederholen“.

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
Mit `wp_power_topic` lässt sich optional ein vorhandenes **exaktes MQTT-Topic am zentralen Broker** (`mqtt_hub_*`) als WP-Leistungsquelle wählen, beispielsweise `ha/heatpump/power`. Eine Home-Assistant-Entity-ID wie `sensor.wp_leistung` ist kein MQTT-Topic: Der Sensor muss seine elektrische Leistung auf dem gewählten Topic veröffentlichen.

Die Nutzlast ist eine rohe endliche Zahl in Watt, zum Beispiel `1250` oder `0`, kein JSON, Zählerstand oder Wärmeleistungswert. Negative Werte werden wie bisher auf 0 W normalisiert; `unknown`, `unavailable`, NaN und Inf werden verworfen. Die Quelle gilt nur bei einer Datei- und Feldzeit von jeweils **weniger als 180 Sekunden** als frisch. Unbekannte Leistung ist kein gemessener Nullwert.

Leer bleibt das bisherige Verhalten mit `heatpump/power_w` und `heatpump/electric_w`. Bei Typ 7 ersetzt ein gesetztes Topic den festen Eingang `power_w`; ein separat gesendetes `electric_w` bleibt als COP-Eingang erhalten. Bei anderen WP-Typen ersetzt es **beide** festen Leistungseingänge, auch wenn es selbst unter `<Basis>/in/...` liegt; Status- und Temperaturtopics bleiben bestehen. Alte Werte werden nach einem Quellenwechsel anhand ihrer Feldtopics verworfen, auch nach einem Neustart vor der ersten neuen Nachricht. Es gibt keinen stillen Rückfall auf die festen Eingänge. Wildcards und Konflikte mit anderen Funktionstopics meldet die Plausibilitätsprüfung beratend.

Das Feld steht im Konfigurationseditor bei MQTT. Nach dem Speichern die Änderung über den **Einrichtungsplan der Installationszentrale** anwenden: Der bestehende Dienst wird geplant neu gestartet und abonniert anschließend das neue Topic. Auch das Leeren des Felds braucht diesen Neustart. Der Editor führt keine eigene Dienstaktion aus. Die Schalter für MQTT-Eingänge und History gelten weiterhin; ein E3DC-Leistungsmesser bleibt maßgeblich. Die WP-Leistung ist ausschließlich ein Messwert für Anzeige und Aufteilung und wird aus dem bereits gemessenen Hausverbrauch genau einmal herausgerechnet; sie erzeugt keinen zusätzlichen Budgetabzug.

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
| `e3dc/in/heatpump/power_w` | W | elektrische Leistungsaufnahme der Wärmepumpe ohne Zusatzheizung | direkt wirksam: WP-Kachel, E-Flow, Live-History, Prognose; wird aus dem reinen Hausverbrauch herausgerechnet |
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

Wenn Broker, Topic, Benutzer, Passwort oder Wiederholungsintervall im Config-Editor
geändert werden, speichert der Editor die Einstellung und leert den
Ramdisk-Konfigurationscache. Der Einrichtungsplan der Installationszentrale
übernimmt den Neustart von `e3dc-mqtt-hub`, damit die Änderung wirksam wird.

Bool-Werte können als `true/false`, `on/off`, `1/0` oder `ein/aus`
gesendet werden. Zahlen dürfen mit Punkt oder Komma kommen, z.B. `1234.5`
oder `1234,5`.

Eingangswerte gelten 180 s. Auch unveränderte Werte deshalb jede Minute erneut senden.
Ein reiner Zustandsauslöser sendet gleichbleibende Werte nicht erneut. Den Rohwert
senden: „unavailable“ und „unknown“ werden ignoriert; eine Umwandlung mit
`float(0)` würde fehlende Messwerte fälschlich als 0 ausgeben.

Beispiel Home-Assistant-Automation für eine Stiebel-/KNX-Wärmepumpe:

```yaml
alias: E3DC-Control sendet WP-Leistung
trigger:
  - platform: state
    entity_id: sensor.stiebel_eltron_wp_leistung
  - platform: time_pattern
    minutes: "/1"
action:
  - service: mqtt.publish
    data:
      topic: e3dc/in/heatpump/power_w
      payload: "{{ states('sensor.stiebel_eltron_wp_leistung') }}"
      retain: false
```

Beispiel für Warmwasser-Temperatur:

```yaml
alias: E3DC-Control sendet Warmwasser-Isttemperatur
trigger:
  - platform: state
    entity_id: sensor.stiebel_eltron_warmwasser_ist
  - platform: time_pattern
    minutes: "/1"
action:
  - service: mqtt.publish
    data:
      topic: e3dc/in/heatpump/ww_temp
      payload: "{{ states('sensor.stiebel_eltron_warmwasser_ist') }}"
      retain: false
```

Beispiel für externe Wallbox-Leistung aus Home Assistant:

```yaml
alias: E3DC-Control sendet Wallbox-1-Leistung
trigger:
  - platform: state
    entity_id: sensor.openwb_ladeleistung
  - platform: time_pattern
    minutes: "/1"
action:
  - service: mqtt.publish
    data:
      topic: e3dc/in/wallbox/power_w
      payload: "{{ states('sensor.openwb_ladeleistung') }}"
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

## Werte zyklisch wiederholen

Unter **Schnittstellen & MQTT** steuert „Werte zyklisch wiederholen (s)“
(`mqtt_hub_repeat_interval_s`) den Abstand in ganzen Sekunden. Standard `0`
schaltet die Wiederholung aus. Werte von 1 bis 9 wirken als 10 Sekunden,
Werte über 3600 als 3600 Sekunden; die Konfigurationsprüfung warnt beratend.
Der Editor speichert die Einstellung. Der Einrichtungsplan der
Installationszentrale übernimmt den Neustart des MQTT-Hubs.

Bei aktiviertem Intervall werden `e3dc/ha/state` und alle skalaren Einzeltopics
mit dem bisherigen retain-Flag wiederholt. `e3dc/live` wird ausschließlich bei
Änderung gesendet. Änderungen werden sofort veröffentlicht; bei Einzeltopics
beginnt das Intervall mit jeder Sendung dieses Topics neu. Der Hub prüft alle
zwei Sekunden, daher kann eine Sendung bis zu einen Schleifentakt später erfolgen.

Die Wiederholung benötigt einen im selben Zyklus gültig gelesenen Live-Snapshot
(höchstens 15 Sekunden alt). Bei fehlenden, ungültigen oder veralteten Live-Daten
endet sie, und `e3dc/status/availability` meldet `offline`. Nach Rückkehr gültiger
Daten meldet der Hub `online` und nimmt die Veröffentlichung wieder auf.
Alte Live-Werte werden nicht mit einem neuen Zeitstempel erneut veröffentlicht.
Das Feld `ts` in `ha/state` bezeichnet die **Sendezeit des Hubs**, nicht die
Datenzeit der Quelle.

Bei eingeschalteter Wiederholung gilt für `free_for_consumers_w`,
`wallbox_budget_w`, `wallbox_allowed`, `heatpump_allowed` und `heater_allowed`
auf **allen Sendewegen dieselbe Budgetfrische von höchstens 60 Sekunden**:
Einzeltopics, `ha/state`, die darauf beruhenden Discovery-Sensoren und
Wiederholungen. Die Werte stammen ausschließlich aus `wb_pv_budget.json`
(`energy_score.free_for_limbs_w`, `budget_w` und die dort vorhandenen
Freigabeanteile), auch wenn der Web-Snapshot ältere Budgetwerte enthält.
Das Alter ist das größere Alter aus Dateiänderungszeit und plausibler
Inhaltszeit `ts`; fehlt eine plausible Inhaltszeit, gilt die Dateiänderungszeit.
Bei älterem, fehlendem oder ungültigem Budget gelten 0 W beziehungsweise
`False`. Die Rückkehr eines frischen Budgets wird sofort als Änderung gesendet,
auch bei gleichem Wert. Die drei Freigaben folgen dabei ausschließlich dem
frischen Budget und seinen Freigabeanteilen; Preis-, Pre-Dump- oder Live-Anteile
aus anderen Dateien können sie nicht überstimmen. Das gilt auch für die
Freigaben in Wiederholungen.

Bei ausgeschalteter Option (`0`) bleiben der bisherige 180-s-Lesepfad, die
Quellenreihenfolge mit Live-Projektion und die Budgetanzeige ohne zusätzliche
Begrenzung erhalten. Die Altersgrenzen der übrigen Quellen bleiben unverändert
(`heizstab_data.json`: 180 s, `storage_manager_state.json`: 900 s).

Unabhängig von der Wiederholung prüft die Preisfreigabeanzeige das Fenster
bei jeder Auswertung: Am Fensterende entfällt sie sofort. Marktpreispläne
verwenden `enabled`, `active` und `consumer_releases`; ältere Pläne ohne
`consumer_releases` fallen auf `allow` zurück. Bei
`tariff_axis = "configured_recurring"` gilt `enabled` und `allow` bei jetzt
laufendem Fenster, ohne das bei Planerzeugung gespeicherte `active`.
`cheap_price_active` prüft die gleiche Fensterregel oder eine aktuelle
Live-Freigabe. Bei ausgeschalteter Wiederholung bleiben PV-, Pre-Dump- und
Live-Freigaben Teil der Gesamtanzeige.

Zur Prüfung das Intervall auf **30 s** setzen, den Einrichtungsplan ausführen und
`e3dc/ha/free_for_consumers_w` am Broker beobachten: Konstante Werte treffen erneut
ein. Den Live-Dienst kontrolliert stoppen: Nach Ablauf der 15-Sekunden-Frist enden
die Wiederholungen, und das Verfügbarkeitstopic meldet `offline`. Den Live-Dienst
anschließend wieder starten und `online` sowie die erneuten Sendungen prüfen.

`mqtt_hub_status.json` enthält neben `updated_at` und `connections` das wirksame
`repeat_interval_s`, die Anzahl wiederholter Nachrichten `repeats_sent`,
unterdrückte Wiederholungszyklen `repeats_suppressed_stale`, Zyklen mit begrenztem
Budget `repeats_budget_stale` und die letzte Wiederholung als `last_repeat_ts`
(Unix-Sekunden; ohne Wiederholung `null`).

## MQTT-Wärmepumpe (nur Anzeige)

`wp_type=7` zeigt eine extern betriebene Wärmepumpe dauerhaft in Kachel,
Energiefluss und beiden Detailansichten. Ohne Messung erscheint „unbekannt“.
Die Auswahl aktiviert keinen Herstellertreiber und sendet keine MQTT-Steuerbefehle.
Eine bestehende Steuerung über SG-Ready/Shelly bleibt wie bei „Keine Wärmepumpe“
erhalten. Der reine Wechsel zwischen −1 und 7 benötigt keine Dienstaktion;
der Wechsel von einer nativen Anbindung nutzt den bisherigen bewussten Dienstwechsel.

Der vorhandene MQTT-Hub nimmt bei aktiviertem Eingang unter `<Basis>/in/heatpump/`
folgende einzelne Rohwerte an (keine JSON-Objekte):

| Topic-Ende | Einheit / Typ |
|---|---|
| `power_w` | elektrische Leistung ohne Zusatzheizung in W, Vorrang für die Leistungsanzeige |
| `electric_w` | elektrische Leistung ohne Zusatzheizung in W, Eingang für COP |
| `heat_kw` | Heizleistung in kW |
| `state`, `mode` | Status und Modus als Text, ohne abgeleitete Verdichterfreigabe |
| `boost_active` | Wahrheitswert `true`/`false` oder `1`/`0` |
| `ww_temp`, `ww_target_temp` | Warmwasser Ist / Soll in °C |
| `flow_temp`, `return_temp`, `outside_temp` | Vorlauf / Rücklauf / Außentemperatur in °C |

Die elektrische Leistung unter `in/heatpump/power_w` bzw. `electric_w` umfasst die
Wärmepumpe **ohne Zusatzheizung**. Eine elektrische Zusatzheizung (Heizstab) wird
getrennt unter `<Basis>/in/heater/power_w` in W gesendet. Beide Werte werden für die
Anzeige jeweils genau einmal aus dem gemessenen Hausverbrauch herausgerechnet;
die Summe aus Haus, Wärmepumpe und Heizstab bleibt gleich.

Liefert die Quelle nur die Summe aus Wärmepumpe und Zusatzheizung, wird diese
**nur als `in/heatpump/power_w`** gesendet. `electric_w` und `in/heater/power_w`
bleiben dann aus. Die Aufteilung fehlt, die Gesamtbilanz bleibt richtig; aus der
Summenleistung wird kein COP berechnet.
**`heat_kw` ist die thermische Heizleistung in kW, nicht in W**:
3200 W Heizleistung werden als `3.2` gesendet.

Bei Typ 7 speist ein gesetztes `wp_power_topic` ausschließlich die Leistungsanzeige
und Bilanz (`power_w`). Es ersetzt den festen Eingang `in/heatpump/power_w`.
`electric_w` bleibt ohne eigene Nachricht unter `in/heatpump/electric_w` unbekannt,
mit Grund; aus der Summenquelle wird kein COP berechnet. Ein getrenntes frisches
`electric_w` bleibt der COP-Eingang. Temperaturen, Heizleistung und Status bleiben
auf den festen Topics. Andere WP-Typen behalten ihre bisherige Quellenwahl.
MQTT-Eingang und Broker werden über die vorhandene MQTT-Einrichtung aktiviert;
ein zusätzlicher Wärmepumpendienst ist für die Anzeige nicht erforderlich.

Bei Typ 7 sind negative Leistungswerte ungültig und bleiben `null`; die bisherigen
Normalisierungsregeln anderer WP-Typen bleiben erhalten.
Jedes Feld benötigt einen endlichen, typgültigen Wert, passende Herkunft sowie
Datei- und Feldalter jeweils **unter 180 Sekunden**. Exakt 180 Sekunden sind alt.
Neue Leistung verjüngt keine alte Temperatur. Ungültige, fehlende, alte oder
abgeschaltete Daten ergeben `null` mit Grund; gültige 0 und `false` bleiben erhalten.
Alter und Herkunft stehen je Wert auf der Detailseite. COP ist ausschließlich
`heat_kw * 1000 / electric_w`, mit beiden Eingängen frisch und `electric_w > 0`.
Es gibt keinen Schätz-COP und keine JAZ aus fremden Herstellerarchiven.
Die Ansichten enthalten keine Bedienelemente; Steuer-POSTs werden abgewiesen.

Die WP steckt im gemessenen Hausverbrauch. Nur die Darstellung teilt eine gültige
WP-Messung einmal ab; das Verbrauchsbudget erhält keinen zusätzlichen Abzug.
Ohne Einzelmessung bleibt der Hauswert einschließlich WP stehen.
Im Diagnose-Export enthält `mqtt_ha_inbound.json` zusätzlich `heatpump_display`
mit Typ, Frische, Gültigkeit und Gründen. Freie Status-/Modustexte sind ausgelassen,
Topics werden wie bisher bereinigt.
