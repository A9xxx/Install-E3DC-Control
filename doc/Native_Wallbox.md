# Native Multi-Wallbox-Steuerung

Der V4-Wallbox-Manager ist ein eigenständiger Python-Dienst. Er regelt E3DC,
openWB/openWB Pro, go-e und reine Mess-Wallboxen über dieselbe Budget-,
Hysterese- und Schutzlogik, nutzt aber je Wallbox den passenden Treiber.

## Was kann der Wallbox-Manager?

* **Multi-Wallbox:** WB1 und WB2 werden getrennt gelesen, angezeigt und
  budgetiert.
* **Klare Nutzer-Modi:** `Aus`, `PV-Kurve ruhig`, `Grundladung stabil`,
  `PV + Akku bis Untergrenze` und `Sofort bis Preislimit`.
* **Geplantes Netzladen:** Slotladen funktioniert in allen aktiven Modi und
  ignoriert das Wallbox-Preislimit, weil die Preislogik bereits in der
  Slot-Auswahl steckt. Im Modus `Aus` ist geplantes Laden gesperrt.
* **Fahrzeugzuordnung:** Pro Wallbox wird ein Fahrzeug automatisch gespeichert;
  der manuelle Start-SoC wird über **SoC setzen** geschrieben.
* **Bestätigter Fahrzeug-SoC:** Ziel-SoC, Restladezeit und `Auto voll` nutzen
  nur SoC-Werte aus Wallbox/openWB, Bluelink/MQTT oder bewusster manueller
  Eingabe. Unbestätigte Profil- oder Altwerte erscheinen als `-- SoC` und
  sperren normales PV-/Budgetladen nicht.
* **Phasen- und Mindestleistung:** openWB Pro, normale openWB, go-e und E3DC
  werden unterschiedlich angesprochen, aber mit derselben Schutzlogik geregelt.
* **openWB-Autoerkennung:** E3DC-Control liest openWB Software 2.x read-only
  aus und passt den Treiber an die erkannte Rolle an, ohne die openWB selbst
  umzustellen.
* **NaN-sichere Messwerte:** Externe MQTT-Werte wie
  `evcc/loadpoints/1/chargePower` werden als reale Wallboxleistung angenommen,
  aber `NaN`, `Infinity` und leere Payloads werden ignoriert.

## Modusübersicht

| Modus | Zweck |
|---|---|
| `Aus` | NGNA: E3DC-Control beobachtet nur. Eine Standardfreigabe wird nur einmalig nach bewusstem Wechsel auf `Aus` in der WebUI gesendet. |
| `PV-Kurve ruhig` | Lädt entlang der Speicher-Ladekurve mit Hysterese. Der Hausspeicher behält Vorrang, wenn die Prognose knapper wird. |
| `Grundladung stabil` | Hält eine ruhige Grundladung, solange das Speicherziel laut Planung erreichbar bleibt. |
| `PV + Akku bis Untergrenze` | Erlaubt dem Auto PV plus Hausspeicher nur oberhalb der Hausakku-Untergrenze; Netz bleibt außen vor. |
| `Sofort bis Preislimit` | Netzladen nur, wenn der aktuelle Preis unter dem Wallbox-Preislimit liegt. |

## Konfiguration

Neue Systeme werden im Config-Editor in `data/e3dc_v4.json` konfiguriert.
Wichtige Felder:

```ini
wb_native_enable = 1
wb_native_type = e3dc|openwb|openwb_pro|goe|none
wb_native_ip = 192.0.2.50
wb_native_mode = 0|1|2   # Dual-Wallbox-Priorität: 0=beide, 1=WB1, 2=WB2
wb1_mode = 0|2|3|4|5
wb2_mode = 0|2|3|4|5
wbminsoc = 50
wbmaxladestrom = 16
wb1_max_amp = 16
wb2_max_amp = 16
wb1_current_step_amp = 1.0
wb2_current_step_amp = 1.0
wb_openwb_auto_discovery = 1
wb_openwb_auto_role_enable = 1
wb_openwb_command_fail_limit = 3
wb_openwb_command_block_s = 300
wb_openwb_start_cp_retries = 3   # openWB Pro: 1..3, Standard 3; ungültig -> 3
```

Eine alte `e3dc.config.txt` ist nur noch Migration und Legacy-Fallback. Neue
Einstellungen gehören in `e3dc_v4.json`.

Die Ladepriorität wird in der Wallbox-WebUI nur angezeigt, wenn WB1 und WB2
konfiguriert sind. Bei Ein-Wallbox-Anlagen bleibt die Verteilung automatisch
ausgeglichen und der Prioritätsschalter wird ausgeblendet.

## Sollstrom-Schrittweite

Der zentrale Wallbox-Manager entscheidet Budget, Netzfreigabe,
Hausakku-Untergrenze, Hysterese und Phasenfreigabe. Die konkrete Rundung des
Ampere-Sollwerts ist Treibervertrag:

* E3DC und go-e bleiben konservativ auf ganze Ampere gerundet.
* openWB Pro nutzt die `connect.php`-Schnittstelle mit 0,1-A-Schritten.
* openWB Software im Secondary-Pfad bleibt standardmäßig bei 1,0 A; pro
  Wallbox kann `wb1_current_step_amp` bzw. `wb2_current_step_amp` auf `0.5`
  oder `0.1` gesetzt werden, wenn die konkrete openWB-/Firmware-Kombination
  diese Werte annimmt und an den Ladepunkt weitergibt.

Dadurch kann die zentrale Regelung Leistungsbudgets als Dezimal-Ampere bis zum
Treiber transportieren, ohne unsichere Hardwarepfade global feiner zu stellen.

## Fahrzeug-SoC-Vertrag

Die Fahrzeugauswahl und der Fahrzeug-SoC sind getrennte Informationen. Die
Auswahl sagt nur, welches Profil an welcher Wallbox steht. Ein SoC wird erst
zur Regelbasis, wenn er frisch bestätigt ist:

* die Wallbox oder openWB/openWB Pro meldet den SoC,
* Bluelink oder ein konfiguriertes MQTT-Topic liefert den Fahrzeug-SoC,
* der Nutzer trägt den aktuellen Wert ein und klickt **SoC setzen**.

Alte Startwerte, einfache Profilwerte und Werte aus einer beendeten Session
werden nicht fortgeschrieben. Nach Abstecken wird die SoC-Session geschlossen;
nach erneutem Anstecken zeigt die WebUI `-- SoC`, bis wieder eine bestätigte
Quelle vorhanden ist. Laden nach PV, Mindestleistung, Preisfenster oder kWh-Ziel
bleibt möglich. Nur SoC-basierte Zielentscheidungen und `Auto voll` benötigen
den bestätigten Wert.


## Zustandsmaschine und Ladeende

Der Wallbox-Manager verwendet eine explizite Zustandsmaschine. Die Diagnose zeigt diese Zustände:

| Zustand | Bedeutung |
|---|---|
| `idle` | Keine Ladeanforderung und kein bestätigter Energiefluss. |
| `offered` | Ein zulässiges Leistungsangebot liegt vor. |
| `starting` | Ein expliziter Startimpuls wurde gesendet; echte Ladung ist noch nicht bestätigt. |
| `charging` | Stromfluss und Ladestatus bestätigen die laufende Session. |
| `stopping` | Ein Stopimpuls wurde gesendet; Schutz- und Rücklesefrist laufen. |
| `ended` | Die Session ist fachlich beendet und bleibt bis zu einer benannten Freigabe gelatcht. |
| `rscp_error` | Antwort oder Rücklesung ist ungültig; es wird kein Erfolg vorgetäuscht. |

Ein Ladeende darf grundsätzlich durch einen bewussten UI-Wechsel
(`wallbox_php_limit_or_profile_change`) oder einen bestätigten Neustart des
Fahrzeugs (`vehicle_self_restart`) freigegeben werden. Eine vollständig belegte
3/3-Startablehnung der openWB Pro ist davon ausgenommen: Bloße Modus-, Limit-,
Ziel-SoC- oder Profiländerungen erhalten ihren Latch und die Wake-up-Evidenz.
Sie werden nur durch eine bestätigte neue Stecksession oder einen typisierten
Modus-5-Nutzerauftrag gelöst, der bereits bei Annahme exakt an Boot,
Stecksession, Konfigurationsstand, Preislimit und dieselbe persistierte
Latchgeneration gebunden wurde. Andere, nicht vollständige
Startablehnungs-/Ladeende-Latches behalten den bisherigen bewussten
UI-Freigabevertrag. Stromrampen, Start-/Stopflanken und Ladeende bleiben
getrennte Verträge; Manager, Treiber und Diagnose geben denselben Zustand aus.

## E3DC-native Regelvertrag

Die native E3DC-Wallbox wird anders behandelt als openWB, openWB Pro oder go-e.
Die E3DC-RSCP-Schnittstelle arbeitet als Flankensteuerung mit
Messwert-Rückmeldung und nicht als absoluter Start-/Stop-Schalter:

* `WB_REQ_SET_EXTERN` setzt den Betriebsmodus und den Stromdeckel. `WBchar6[1]`
  ist der gewünschte Amperewert.
* `WBchar6[4]` ist ein Toggle-Impuls, kein dauerhaftes Soll. Der Impuls darf nur
  für eine bewusste Start- oder Stop-Flanke gesetzt werden.
* `force_state=None` bedeutet bei E3DC immer: nur Stromdeckel/Keepalive, kein
  Toggle. Ein reines Ampere-Update darf eine wartende oder bereits beendete
  Ladung niemals wieder starten.
* `force_state=2` ist der explizite Startimpuls. Er darf erst gesendet werden,
  wenn das physische Budget für die Mindestleistung plausibel da ist.
  Bei einer Easy Connect sind je frisch bestätigter Stop-Episode höchstens drei
  solche Impulse mit mindestens 60 Sekunden Abstand zulässig. Jeder Impuls
  benötigt erneut einen frischen Stop-Readback; Laden, Abstecken oder ein neuer
  Stopzustand beendet beziehungsweise erneuert die Episode. Andere native
  E3/DC-Familien bleiben bei genau einem Startimpuls je Stop-Episode.
* `force_state=1` ist der harte Stopimpuls. Er darf nur für echte harte
  Stopgründe und bei verifizierter aktiver Ladung verwendet werden.

In der Diagnose wird dieser Vertrag als
`e3dc_native_production_v1` sichtbar: Ampere-Updates sind Stromdeckel, keine
Toggles; Startflanken laufen nur über die Session-State-Machine; harte
Stopflanken brauchen einen harten Grund und verifizierte Ladung.

Die Regelung glaubt einer Startfreigabe nicht blind. Eine E3DC-Ladung gilt erst
als echt, wenn mindestens einer dieser Nachweise vorhanden ist:

* `TAG_WB_EXTERN_DATA_ALG` meldet Laden bzw. Start über die E3DC-Statusbits.
* Die Phasenleistungen `TAG_WB_PM_POWER_L1/L2/L3` ergeben eine plausible
  verifizierte Wallboxleistung.

Zustände wie `Startfreigabe`, `freigegeben`, `Start wartet`,
`Wartet Mindestleistung` oder `Warte auf Sonne` sind nur angebotene Leistung.
Sie dürfen im Frontend und in der Budgetrechnung nicht als echte Ladung
fortgeschrieben werden, solange keine verifizierte Phasenleistung oder aktive
Ladebestätigung vorliegt. Beim Ladeende muss die angezeigte Wallboxleistung
sofort auf `0 W` fallen, damit Phantomladen nicht wieder Hausverbrauch,
Langzeitwerte oder Folgeregelungen verfälscht.

Ein erkannter Ladeende-Latch blockiert erneute Autostarts bis zu einem klaren
Ereignis. Gültige Ausnahmen sind Umstecken oder Fahrzeugwechsel, bewusste
Änderungen in `Wallbox.php` an Modus, Maximalstrom, Ziel-SoC oder
Fahrzeugprofil sowie ein echtes Selbst-Wiederanlaufen des Autos mit
verifizierter Ladeleistung. Ein geänderter Stromdeckel ist dabei nur die
Freigabe für einen neuen Versuch; er beweist niemals aktive Ladung.

## openWB richtig einordnen

openWB muss nach Betriebsart unterschieden werden. Die Begriffe klingen ähnlich,
führen technisch aber in verschiedene Schnittstellen. Wichtig ist: Es darf nur
einen aktiven Regler geben. Wenn openWB Software 2.x selbst regelt, darf
E3DC-Control nicht gleichzeitig denselben Ladepunkt über den Secondary-Pfad
überfahren.

Die Autoerkennung reduziert hier Fehlkonfigurationen: E3DC-Control ruft
`simpleAPI.php?get_chargepoint_all` und, wenn erreichbar, das V1-Config-Topic
`openWB/chargepoint/<id>/config` read-only ab. Wird ein interner openWB-
Ladepunkt mit `type=internal_openwb` oder `configuration.mode=series` erkannt,
behandelt der Treiber diese Wallbox als openWB-Primary und nutzt den
Primary-simpleAPI-Pfad. Ist in E3DC-Control ausdrücklich Modbus Secondary oder
Primary konfiguriert, bleibt diese Betreiberentscheidung sichtbar und wird nur
mit der erkannten openWB-Rolle abgeglichen.

Bei einer openWB-Software mit zwei Ladepunkten kann die Erkennung WB2 zur
Laufzeit ergänzen, wenn WB1 als openWB Controller konfiguriert ist, dieselbe
IP beide Ladepunkte meldet und WB2 noch leer ist. Das schreibt keine neue
Konfiguration, sondern verhindert nur, dass ein vorhandener zweiter Ladepunkt
unsichtbar bleibt.

Wenn drei Schreibbefehle hintereinander nicht bestätigt werden, pausiert
E3DC-Control weitere openWB-Schreibbefehle kurz und meldet den Zustand im
Frontend als `openWB-Befehle nicht angenommen`. Damit wird eine falsche
Primary/Secondary-Annahme sichtbar, statt still weiter Befehle zu senden.

| Aufbau | Einstellung in openWB | Auswahl in E3DC-Control | Bemerkung |
|---|---|---|---|
| **openWB Pro standalone** | Keine openWB-Software-Rolle nötig. Die Pro wird direkt angesprochen. | `openWB Pro (connect.php)` mit IP der Pro | Empfohlener Masterpfad, wenn E3DC-Control regeln soll. E3DC-Control setzt `ampere`, `phasetarget` und bei Bedarf `cp_interrupt`. |
| **openWB Software 2.x als Primary mit Pro/Satellit/Fremdwallbox** | `Einstellungen -> Allgemein -> Steuerungsmodus: primary`; Ladepunkt in openWB einrichten. | `openWB Controller`, `openWB Primary` bewusst aktiv | openWB regelt selbst. E3DC-Control wertet reale Leistung aus und kann den openWB-Lademodus per simpleAPI umschalten. Aktive Stromvorgaben laufen im Primary-Direktpfad über openWB-Sofortladen (`chargecurrent`); openWB-SoC- und Energiemengenlimits bleiben wirksam. |
| **openWB Software 2.x als Secondary** | `Steuerungsmodus: secondary`; `Steuerung über Modbus als secondary: An`; danach openWB neu starten. | `openWB Controller`, Secondary/Modbus aktiv, Port `1502`, Slave-ID `1` | E3DC-Control gibt Sollstrom plus Heartbeat vor; openWB stoppt bei ausbleibendem Heartbeat. |
| **Nur Messwerte aus evcc/openWB** | Keine Steuerfreigabe nötig; nur MQTT-Leistungstopic bereitstellen. | Observe-only per MQTT-Leistungstopic | Keine Steuerbefehle; Leistung wird für Dashboard, Historie und Hausverbrauchsbereinigung genutzt. |

Bei `openWB Primary` gibt es deshalb zwei bewusst benannte Rollen: **Primary
PV-geführt** heißt, openWB bleibt im PV-Modus und E3DC-Control führt nur den
Speicherrahmen anhand der gemessenen Wallboxleistung. **Primary-Direktpfad**
heißt, E3DC-Control setzt Strom über den dokumentierten openWB-Sofortladen-Strom
`chargecurrent`; openWB zeigt dann Sofortladen und Sofortladen-Limits wie
Ziel-SoC oder Energiemenge können die Ladung beenden. Wer eine aktive
E3DC-Control-Stromführung ohne diesen openWB-Sofortladen-Pfad möchte, betreibt
die openWB Software als Secondary oder eine openWB Pro direkt über `connect.php`.
Wenn openWB für die simpleAPI mit `401 Unauthorized` antwortet, verlangt die
openWB-HTTP-Seite eine Anmeldung. In diesem Fall nutzt E3DC-Control die
bestehenden Wallbox-Zugangsdaten `wb_user`/`wb_pass` als Basic Auth; ohne
gesetzten Benutzer wird kein Auth-Header gesendet.

### openWB Pro als reiner Aktuator

Für den reibungslosen Betrieb einer openWB Pro mit E3DC-Control hat sich dieser
Pfad bewährt:

* **Direkte HTTP-API:** Die Pro wird über ihre Hardware-IP und `connect.php`
  angesprochen. Eine zusätzliche openWB-Software-2-Instanz als Vermittler führt
  leicht zu Ping-Pong, weil die SW2 ihre eigene Regellogik gegen die Vorgaben
  von E3DC-Control setzt.
* **Klare Stromvorgaben:** E3DC-Control trennt hart zwischen `0 A` als Stop und
  dem normgerechten Startbereich ab `6 A`. Nach Ladeende werden keine
  wiederkehrenden Ladeimpulse gesendet, damit Fahrzeugsteuergeräte und
  12-V-Batterie schlafen können.
* **Phasenwechsel mit Haltezeit:** E3DC-Control setzt über `connect.php` nur das
  Ziel (`phasetarget=1` oder `phasetarget=3`). Die Hardware der Pro übernimmt
  Schütz-Trennung und CP-Ablauf. Nach der kurzen sicheren 0-A-/CP-Beruhigung
  darf der Strom wieder anlaufen. Der persistente Schutz von mindestens
  `480 s` beginnt erst mit dem bestätigten Wire-Receipt dieses realen
  Phasenausgangs. Eine reine Budgetreservierung erzeugt keinen Cooldown. Die
  Sperre schützt ausschließlich vor einem weiteren Phasenwechsel; sie
  blockiert weder den bestätigten Wiederanlauf noch die laufende Stromregelung.
* **Recovery vor neuem Budget:** Eine mögliche ältere Ausgangsgeneration wird
  vor einem neuen Storage-Grant und vor jeder Supersession ausgewertet. Ein
  gestrandeter 0-A-Intent darf nur anhand seines eigenen Intent-/ACK-Paars und
  eines frischen, zeitlich nachfolgenden 0-A-/0-W-Gerätereadbacks geschlossen
  werden. Dieser Recoverypfad sendet keinen neuen Hardwarebefehl. Mehrdeutige
  oder fremde Generationen bleiben gesperrt.
* **1-Phasen-Fallback:** Wenn die Pro oder das Fahrzeug nach einem
  3-phasigen Ziel nicht plausibel auf 3-phasig wechselt, behandelt das System
  die Session als 1-phasig. Das verhindert Endlosschleifen bei Fahrzeugen mit
  reinem 1-Phasen-Lader.
* **CP-Interrupt nur als Weckruf:** `cp_interrupt=true` wird nicht für den
  normalen Phasenwechsel genutzt. Er ist ein gezielter Wakeup, wenn ein
  angestecktes Fahrzeug trotz freigegebener Leistung eingeschlafen ist.
* **Begrenzte Wake-up-Episode:** Je positiver Stromfreigabe sind ein bis drei
  Wake-up-Versuche konfigurierbar; Standard sind drei. Bei bewusst gewähltem
  Wert `1` darf bereits der erste vollständig belegte Versuch weitere
  automatische Starts derselben Stecksession sperren. Bei zwei oder drei
  Versuchen reicht ein einzelner Fehlversuch dafür nicht aus. Boolesche, nicht
  endliche und nicht ganzzahlige Werte sind ungültig und fallen auf drei
  Versuche zurück; Wake-up-Planung und Startablehnung verwenden denselben
  Parservertrag. Eine dauerhafte Startablehnung benötigt außerdem den
  typisierten Receipt der vollständig abgearbeiteten Episode. Stecksession,
  aktuelle Stromfreigabe und Zeitkette müssen exakt zusammenpassen.
* **Bewusster neuer Sofortauftrag:** Ein erneuter WebUI-Auftrag für `Sofort bis
  Preislimit` erhält eine eigene zufällige Kennung. Nur wenn dieselbe aktuelle
  Stecksession eine vollständig belegte Startablehnung erreicht hat, darf der
  Manager deren eigene Wake-up-Episode einmalig für diesen Nutzerauftrag neu
  öffnen. Der Auftrag selbst sendet keinen Gerätebefehl und ändert kein
  Budget. Preislimit, Nutzer-`Aus`, Not-Aus, Speicherreserve, Netzpunkt- und
  Hardwaregrenzen bleiben danach unverändert vorrangig.

### openWB Software 2.x als Primary

Wenn die openWB Software selbst eine Pro, einen Satelliten oder eine
Fremdwallbox als Primary regelt, bleibt openWB der Energiemanager. E3DC-Control
liest Leistung und Status, bereinigt den Hausverbrauch und kann mit bewusst
aktiviertem `openWB Primary` nur den openWB-eigenen Modus wechseln:

```text
Normal/Rückgabe -> set_chargemode=pv
Aktiver Eingriff -> chargecurrent=<Ampere>, set_chargemode=instant
Schutz/Stop      -> set_chargemode=stop
```

Dieser Pfad ist ein Opt-in und kein automatischer Default. Besonders bei einer
extern eingebundenen openWB Pro ist er nicht als Pro-Fernsteuerung gedacht. Wenn
E3DC-Control die Pro wirklich führen soll, die Pro direkt als
`openWB Pro (connect.php)` konfigurieren.

### openWB Software 2.x als Secondary

Für openWB Software 2.x im Secondary-Betrieb gilt der offizielle Secondary-Pfad.
Die Modbus-Dokumentation nennt Port `1502`, Slave-ID `1`, `10171` für den
LP1-Sollstrom und `10190` für den Heartbeat:

```text
openWB/set/internal_chargepoint/global_data
openWB/set/internal_chargepoint/<duo_num>/data/set_current
```

Der Heartbeat muss zyklisch geschrieben werden; openWB stoppt sonst die Ladung.
`duo_num` ist die lokale Nummer des internen Ladepunkts, nicht zwingend die
sichtbare `chargepoint/<id>`-Nummer im openWB-Primary-System.

Quellen zur Einordnung:

- evcc dokumentiert openWB Software 2.x mit `Steuerungsmodus: secondary` und
  `Steuerung über Modbus als secondary: An`:
  <https://docs.evcc.io/docs/devices/chargers>
- openWB beschreibt `primary` als steuerndes System und `secondary` als
  gesteuertes System:
  <https://wiki.openwb.de/doku.php?id=openwb%3Avc%3A2.1.9%3Asoftware%3Aeinstell-konfig%3Aeinstellungen%3Aallgemein>
- Die openWB simpleAPI dokumentiert Lademodus-Setzen (`instant`, `pv`, `eco`,
  `stop`, `target`) und Sofortladestrom:
  <https://wiki.openwb.de/doku.php?id=openwb%3Avc%3A2.2.0%3Asimpleapi>
- Die openWB-Modbus-Rev2.0-Dokumentation nennt Sollstrom, Phasen, Heartbeat und
  Port `1502`:
  <https://openwb.de/main/wp-content/uploads/2023/10/ModbusTCP-openWB-series2-Pro-1.pdf>

Direkte evcc/openWB-Messwerte können zusätzlich über den MQTT-Hub eingetragen
werden:

```ini
wb_ip = 192.0.2.126:1883
wb_topic = evcc/loadpoints/1/chargePower
```

## go-e

Die go-e Wallbox wird direkt über die HTTP-API v2 gelesen und gesteuert:

```text
http://<ip>/api/status
```

In der go-e App muss **HTTP-API (v2) erlaubt** sein.

## Hausverbrauch und Statistik

Externe Wallboxleistung wird vom reinen Hausverbrauch abgezogen. Dadurch landen
evcc/openWB/go-e-Leistungen nicht doppelt als Hausverbrauch und Wallboxverbrauch
in Dashboard, Historie und Planung. Bei zwei Wallboxen werden WB1 und WB2
getrennt ausgewiesen.

## Docker

Im Docker startet der Wallbox-Manager mit dem Container. Wenn
Wallbox-Funktionen nachträglich aktiviert oder deaktiviert werden, den Container
mit dem vorhandenen Image neu erstellen und vollständig prüfen:

```bash
cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
sudo python3 ./Installer/docker_compose_update.py \
  --compose-dir . --sudo --recreate-current
sudo docker compose logs --tail=80 e3dc-control
```

Für ein Image-Update wird derselbe Host-Helfer ohne `--recreate-current`
aufgerufen. Er zieht und bindet dann zuerst das neue Image.

## Troubleshooting

### Wallbox wird nicht angezeigt

Die UI blendet deaktivierte Wallboxen aus. Setze einen Wallbox-Typ, eine IP,
ein Shelly-Feld oder ein direktes MQTT-Leistungstopic.

### evcc-Leistung kommt per MQTT, Dashboard zeigt aber 0 W

Prüfe, ob das Leistungs-Topic im Bereich **Wallbox-Leistung per MQTT** steht:

```text
wb_topic = evcc/loadpoints/1/chargePower
```

`chargePower` gehört nicht in `mqtt_hub_sub_soc_topic`.

### E3DC steht nach `Aus` noch auf 6 A

Ab 5.0.0 gilt NGNA: `Aus` sendet keine wiederholten Korrekturen mehr an die
Wallbox. Wenn du in der Wallbox-WebUI bewusst von einem aktiven Modus auf `Aus`
wechselst, gibt E3DC-Control die Wallbox einmalig auf die konfigurierte
Grundeinstellung frei, z.B. 32 A und PV/Sonnenmodus bei E3DC. Danach wird nur
noch beobachtet, bis wieder ein aktiver Modus gewählt wird. Ein Neustart, ein
kurzer Verbindungsverlust oder "kein Fahrzeug verbunden" löst keine erneute
Freigabe aus.
