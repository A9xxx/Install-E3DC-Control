# Zweit-Wallbox und Multi-Wallbox-Betrieb

E3DC-Control V4 kann WB1 und WB2 getrennt messen, anzeigen und regeln. Das gilt
für E3DC-native Wallboxen, openWB/openWB Pro, go-e sowie reine
MQTT-/Shelly-Messpunkte.

## Funktionsweise

* **Live-Anzeige:** Dashboard und Energiefluss zeigen nur konfigurierte oder
  frisch messende Wallboxen.
* **Regelung:** Der Wallbox-Manager verteilt das verfügbare Budget pro
  Ladepunkt und beachtet pro Wallbox Modus, Maximalstrom, Phasenlogik,
  Fahrzeugzuordnung und Hysterese.
* **Statistik:** Verbrauch von WB1 und WB2 wird getrennt erfasst. Tageswerte
  führen beide Ladepunkte zusammen und weisen deren Einzelanteile aus; die
  Langzeitstatistik übernimmt dieselbe bestätigte Summe ohne Doppelzählung.
* **Hausverbrauch:** Externe Wallboxleistung wird vom reinen Hausverbrauch
  abgezogen, damit keine Doppelzählung entsteht.

## Konfiguration

Die Konfiguration erfolgt im Web-Config-Editor im Bereich **Wallbox** beziehungsweise
**Schnittstellen & MQTT**.

### Direkte MQTT-Leistung, z.B. evcc/openWB

```text
wb_ip    = 192.0.2.126:1883
wb_topic = evcc/loadpoints/1/chargePower

wb2_ip    = 192.0.2.126:1883
wb2_topic = evcc/loadpoints/2/chargePower
```

`chargePower` ist eine Leistung in Watt und gehört nicht in das
Fahrzeug-SoC-Feld. Der MQTT-Hub verarbeitet diese Werte NaN-/Inf-sicher und
schreibt sie in Dashboard, Historie, Planung und Hausverbrauchsbereinigung.

### Shelly-Messung

```text
shelly_wb_ip  = 192.0.2.50
shelly_wb2_ip = 192.0.2.51
```

### Native Steuerung

Für steuerbare Wallboxen werden Typ, IP, optionaler Ladepunkt und maximale
Ampere pro Wallbox gesetzt. Die sichtbaren Modi sind:

* `Aus`
* `PV-Kurve ruhig`
* `Grundladung stabil`
* `PV + Akku bis Untergrenze`
* `Sofort bis Preislimit`

Geplantes Netzladen per Slot funktioniert in allen aktiven Modi. `Aus` bleibt
wirklich aus: Der Manager beobachtet nur und sendet keine laufenden Befehle. Die
Wallbox-Grundeinstellung wird nur einmalig nach bewusstem Wechsel auf `Aus` in
der WebUI freigegeben.

Die Ladepriorität `WB1 / Beide / WB2` wird nur eingeblendet, wenn auch zwei
Wallboxen konfiguriert sind. Bei einer einzelnen Wallbox gibt es nichts zu
verteilen; die Einstellung bleibt intern automatisch auf `Beide`.

### openWB Software 2.x richtig einordnen

Bei openWB gibt es drei verschiedene Fälle, die nicht gemischt werden sollten:

| Ziel | Einstellung in openWB Software | Einstellung in E3DC-Control |
|---|---|---|
| E3DC-Control soll eine openWB Pro direkt regeln | Keine openWB-Software-Rolle nötig; IP der Pro verwenden | Wallbox-Typ `openWB Pro (connect.php)` |
| openWB Software regelt selbst eine Pro, einen Satelliten oder eine Fremdwallbox | `Steuerungsmodus: primary`; Ladepunkt in openWB einrichten | Wallbox-Typ `openWB Controller`, Schalter `openWB Primary` bewusst aktivieren |
| E3DC-Control oder evcc soll openWB Software 2.x als Secondary führen | `Steuerungsmodus: secondary`; `Steuerung über Modbus als secondary: An`; danach openWB neu starten | Wallbox-Typ `openWB Controller`, Secondary/Modbus aktiv, Port `1502`, Slave-ID `1` |

Eine openWB Software, die als Primary eine externe `openWB Pro` steuert, darf
nicht gleichzeitig über den Secondary-Setstrom als Pro-Fernsteuerung behandelt
werden. Dieser Pfad kommt bei der extern eingebundenen Pro nicht zuverlässig als
Ampere-Vorgabe an. Für echte E3DC-Regelung deshalb die Pro direkt per
`connect.php` konfigurieren. Für openWB-eigene Regelung openWB im Primary
lassen und E3DC-Control nur Leistung, Status und Hausverbrauch bereinigen
lassen.

Wichtig für `openWB Primary`: **Primary PV-geführt** bedeutet, openWB bleibt im
PV-Modus und E3DC-Control führt nur den Speicherrahmen anhand der gemessenen
Wallboxleistung. Sobald E3DC-Control im Primary aktiv Strom vorgibt, ist das der
**Primary-Direktpfad** über den dokumentierten openWB-Sofortladen-Strom
`chargecurrent`. openWB zeigt dann Sofortladen; openWB-Sofortladen-Limits wie
Ziel-SoC oder Energiemenge bleiben wirksam und können die Ladung beenden.
Wer eine aktive E3DC-Control-Stromführung ohne diesen Sofortladen-Pfad möchte,
betreibt openWB Software als Secondary oder eine openWB Pro direkt über
`connect.php`.
Antwortet openWB auf die simpleAPI mit `401 Unauthorized`, sind die
openWB-HTTP-Zugangsdaten erforderlich. E3DC-Control verwendet dafür die
vorhandenen Felder `wb_user`/`wb_pass` als Basic Auth und lässt den Header weg,
solange kein Benutzer eingetragen ist.

Die Autoerkennung greift genau an dieser Stelle: E3DC-Control liest die
openWB-Ladepunkte read-only über `get_chargepoint_all` und die V1-Konfiguration
aus. Erkennt die openWB einen internen Ladepunkt oder den Serien-/Primary-Pfad,
nutzt der Treiber automatisch den passenden Primary-simpleAPI-Pfad. Erkennt er
einen Secondary-/Modbus-Pfad oder wurde dieser bewusst konfiguriert, bleibt der
Secondary-Sollstrompfad aktiv. E3DC-Control stellt die openWB dabei nicht um.

Meldet eine openWB-Software zwei Ladepunkte und ist WB2 in E3DC-Control noch
nicht konfiguriert, kann WB2 zur Laufzeit aus der openWB-Auswahlliste ergänzt
werden. Das ist absichtlich nur eine Laufzeit-Erkennung: Die Nutzerkonfiguration
wird nicht still überschrieben.

Werden drei openWB-Schreibbefehle nacheinander nicht angenommen, stoppt
E3DC-Control weitere Schreibversuche für kurze Zeit und zeigt den Zustand im
Frontend an. Dadurch fällt eine falsche Primary-/Secondary-Annahme sofort auf.

### Ampere-Schrittweite

Die zentrale Regelung gibt ein Leistungsbudget vor; die Rundung auf konkrete
Ampere-Schritte passiert im Treiber. openWB Pro kann über `connect.php`
0,1-Ampere-Schritte übernehmen. Normale openWB Software im Secondary-Pfad
bleibt aus Kompatibilitätsgründen standardmäßig bei 1,0 A. Wenn die konkrete
openWB-/Firmware-Kombination Dezimal-Ampere sauber annimmt und weitergibt,
kann pro Wallbox im Config-Editor `0,5 A` oder `0,1 A` gewählt werden.

Bei Unsicherheit bleibt `1 A` die robuste Einstellung. Eine zu fein eingestellte
openWB, die die Werte intern doch rundet oder verwirft, fällt über den
Befehls-/Readback-Status im Frontend auf und sollte wieder auf `1 A` gestellt
werden.

## Fahrzeugzuordnung

Auf der Wallbox-Seite wird pro Wallbox das Fahrzeug ausgewählt. Die Auswahl wird
automatisch gespeichert. Der Button **SoC setzen** schreibt nur den manuellen
Start-SoC für das aktuell gewählte Fahrzeug.

Die Auswahl eines Fahrzeugs bestätigt keinen SoC. Für Ziel-SoC und `Auto voll`
zählen nur frische, regelbestätigte SoC-Werte aus Wallbox/openWB,
Bluelink/MQTT oder eine bewusste manuelle Eingabe. Ab 5.4.5a kann ein frisch
beobachteter openWB-SoC mit Quelle und Alter rein lesend erscheinen, wenn
Stecksession oder Fahrzeugprofil eindeutig passen. Diese Anzeige erteilt keine
Regel- oder Schreibautorität. Ohne Bestätigung bleibt die SoC-basierte
Entscheidung geschlossen; nach erneutem Anstecken muss der Regelwert wieder
gelesen oder gesetzt werden. PV-/Budgetladen wird dadurch nicht gesperrt.

## Häufige Fragen

### Wird die zweite Wallbox auch gesteuert?

Ja, sofern sie als steuerbare Wallbox konfiguriert ist. Reine MQTT- oder
Shelly-Messpunkte liefern nur Messwerte, werden aber trotzdem korrekt in
Dashboard, Statistik und Hausverbrauch berücksichtigt.

### Was passiert, wenn keine Wallbox konfiguriert ist?

Dashboard und Wallbox-Seite blenden die Wallbox-Kacheln aus. Sobald ein
Wallbox-Typ, eine IP oder ein direktes MQTT-Leistungstopic gesetzt ist, wird die
betreffende Wallbox wieder sichtbar.
