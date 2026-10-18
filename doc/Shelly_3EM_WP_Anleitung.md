# Shelly Pro3EM — Wärmepumpe einbinden (wp_type=3)

> **Für:** Nutzer ohne native WP-Anbindung (kein Luxtronik, kein IDM)
> **Ziel:** Shelly Pro3EM misst die WP-Leistung auf allen 3 Phasen
>           und schaltet optional das integrierte Relais bei PV-Überschuss.
> **Dienst:** `e3dc-heizstab.service` (wird für 3EM mitgenutzt)

---

## Voraussetzungen

- E3DC-Control V4 läuft (alle Kerndienste aktiv)
- Shelly Pro3EM ist im Heimnetz erreichbar (feste IP empfohlen!)
- Shelly Gen2 Firmware (RPC API muss aktiv sein — Standard ab Werk)
- SSH-Zugang zum Raspberry Pi

---

## Schritt 1 — Shelly Pro3EM vorbereiten

### 1a. Feste IP im Router vergeben

Vergib dem Shelly im DHCP-Server deines Routers eine feste IP, z.B. `192.0.2.163`.
So ändert sich die IP nie und der Dienst findet ihn immer.

### 1b. Erreichbarkeit testen (vom PC aus)

Öffne im Browser:
```
http://192.0.2.163/rpc/EM.GetStatus?id=0
```

Du solltest eine JSON-Antwort mit `current_a`, `voltage`, `act_power` sehen.
Wenn das funktioniert — alles bereit.

### 1c. Shelly an die WP anschließen

- Phase(n) der WP durch den Pro3EM führen (Einbau durch Elektriker!)
- Wenn du das **Relais** nutzen willst (Ein/Aus-Steuerung): Schaltausgang des Shelly
  in die Steuersignalleitung der WP einschleifen (z.B. SG-Ready Kontakt)
- Wenn du **nur messen** willst (Monitoring): Nur die Stromwandler klemmen, Relais offen lassen

---

## Schritt 2 — Dienst prüfen / installieren

Den Dienststatus über **Hauptmenü 2) Systemstatus anzeigen** prüfen.
Fehlt der Dienst, zunächst die Konfiguration aus Schritt 3 speichern.
Danach in der **Installationszentrale → Heizstab / Shelly → Installieren**
die verwaltete Unit mit passendem Python-venv, Gruppenrechten und gemeinsamem
Manager-Lock einrichten. Alternativ **Konsole 41) Energy Manager** öffnen und
**3) Shelly Pro3EM (WP ohne native Anbindung)** auswählen. Die Unit wird durch
diesen Installationsweg erstellt; eine manuell angelegte Service-Datei ist
nicht erforderlich.

---

## Schritt 3 — Konfiguration im Web-UI

Öffne den Konfiguration Editor im Dashboard:
**Einstellungen -> Konfiguration Editor -> V4 Smart Home (Regelung & KI)**

Dort im Abschnitt **Wärmepumpen Typ**:

| Feld | Wert |
|---|---|
| **Wärmepumpen Typ** | `Shelly Pro3EM (WP ohne native Anbindung)` auswählen |
| **Shelly Pro3EM IP-Adresse** | z.B. `192.0.2.163` |
| **Automatik darf Geräte steuern** | Für reines Messen optional; zum Schalten einschalten |
| **Relais-ID** | `-1` = nur messen / `0` = Relais 0 schalten |
| **PV-Auto-Steuerung** | `0 = Nur messen` oder `1 = PV-Auto (Schalten)` |
| **WP Mindestleistung (W)** | Standard `1000`, z.B. `1500` für eine größere WP. Ab dieser gemessenen WP-Leistung gilt die WP als laufend; das Relais schaltet ein, wenn das der WP zugewiesene Budget diesen Wert, mindestens aber 1500 W, erreicht |
| **WP Nennleistung (W)** | z.B. `3000` (laut Typenschild deiner WP) |
| **Mindest-SOC (%)** | z.B. `20` (Batterie-Reserve: WP startet nicht wenn SOC darunter) |

**Speichern** drücken.

Ab `5.1.8h` liest der Dienst die Pro3EM-Leistung auch dann aus, wenn
Heizstab-/PV-Auto deaktiviert ist. Für reines Monitoring reicht:

```
Relais-ID          = -1    (nicht schalten)
PV-Auto-Steuerung = 0     (nur messen)
```

Das Relais bleibt dabei unangetastet; `wp_power_w` wird trotzdem in
`heizstab_data.json` geschrieben und kann aus dem Hausverbrauch herausgerechnet
werden.

---

## Schritt 4 — Erst im Monitoring-Modus testen

Fange IMMER mit dem reinen Mess-Modus an:

```
Relais-ID      = -1    (nicht schalten)
PV-Auto        = 0     (nur messen)
```

Die Pro3EM-Messung läuft auch dann, wenn **Automatik darf Geräte steuern**
ausgeschaltet ist. Schaltbefehle werden erst gesendet, wenn zusätzlich eine
Relais-ID ab `0`, `PV-Auto = 1` und die Automatik freigegeben sind.

Bei globalem „Automatik aus“ werden keine neuen Relaisstarts angefordert.
Ein zugeordnetes Relais, das beim ersten erfolgreichen Readback nach
Dienststart eingeschaltet vorgefunden wird, gilt als eigener laufender
Auftrag. Seine Mindestlaufzeit beginnt bei unbekanntem Einschaltalter ab
dieser Beobachtung. Nach Ablauf wird es genau einmal bestätigt ausgeschaltet.
Ein später extern eingeschaltetes Relais bleibt bei globalem Aus reine
Messung. Vom Dienst selbst eingeschaltete Relais bleiben eigene Aufträge.
Fehlende oder veraltete Livedaten mit unbekanntem SoC verkürzen die
Mindestlaufzeit nicht; ein gemessener SoC unter `hs_min_soc` bewirkt sofort
die Sicherheitsabschaltung.

Bei einer myPV AC ELWA-E sperrt ein unbestätigter Gerätetimeout in Register
1004 die Sollwertsteuerung nicht. Die Heizstabkarte warnt dann vor der
fehlenden geräteseitigen Zeitgrenze. Fehlgeschlagene Timeout-Schreibversuche
werden höchstens stündlich wiederholt; nach Bestätigung erfolgt kein weiterer
Schreibversuch innerhalb desselben Dienstprozesses. Eine 0-W-Rücknahme bleibt
auch nach einem abgelehnten oder unbestätigten Schreibbefehl erlaubt.


Dann Dienst starten und Live-Log beobachten:

```bash
sudo systemctl start e3dc-heizstab.service
sudo journalctl -u e3dc-heizstab.service -f --no-pager
```

Alle 15 Sekunden erscheinen Zeilen wie:

```
[10:23:45] PV=4500W Grid=-1200W SOC=72%
  [3EM] WP=2300W (A:780W B:760W C:760W) [LASTANNAHME]
```

`[LASTANNAHME]` erscheint, wenn die gemessene Leistung die WP Mindestleistung
erreicht; liegt sie darunter (etwa Pumpenvorlauf oder Standby), steht
`[STAND/PUMPENVORLAUF]`.

Das bestätigt: Shelly Pro3EM wird korrekt ausgelesen.

Ramdisk-Status prüfen:

```bash
cat /var/www/html/ramdisk/heizstab_data.json | python3 -m json.tool | head -30
```

Relevante Felder:

```json
{
  "wp_power_w":   2300.0,    <- Gesamtleistung WP (alle 3 Phasen); null, wenn die Messung nicht verfügbar ist
  "wp_phase_a_w": 780.0,     <- Phase A
  "wp_phase_b_w": 760.0,     <- Phase B
  "wp_phase_c_w": 760.0,     <- Phase C
  "wp_is_running": true,     <- true ab 100 % der WP Mindestleistung (Lastannahme)
  "wp_relay_on":  false,     <- Relaisstatus
  "wp_takt_protect_active": false, <- Mindestlaufzeit/Wiedereinschaltsperre aktiv
  "surplus_w":    1200.0,    <- Verfügbarer PV-Überschuss
  "soc":          72.0       <- Aktueller Batterie-SoC
}
```

Liegt die gemessene Leistung über 0 W, aber unter der WP Mindestleistung, ist
`wp_is_running` false; das System wertet das als Pumpenvorlauf oder Standby
(`wp_acceptance_stage` = `pump_prelude_or_standby`).

---

## Schritt 5 — Automatische Relais-Steuerung aktivieren (optional)

Wenn die Messung korrekt läuft und du das Relais auch schalten möchtest:

Im Web-UI ändern:

```
Relais-ID      = 0     (oder 1/2 je nach Anschluss)
PV-Auto        = 1     (PV-Auto-Steuerung ein)
```

Dienst neu starten:

```bash
sudo systemctl restart e3dc-heizstab.service
sudo journalctl -u e3dc-heizstab.service -f --no-pager
```

Wenn genügend PV-Überschuss vorhanden und SOC >= Minimum:

```
  [3EM] -> WP Relais 0 EINgeschaltet
```

Ab 5.1.8b nutzt das Pro3EM-Relais die allgemeinen Wärmepumpen-Taktschutzwerte
`wp_min_runtime_min` und `wp_restart_block_min`. Kurze Wolkenkanten halten das
Relais deshalb bis zur Mindestlaufzeit und blockieren direkte Neustarts nach
einem Stopp. Ein gültig gemessener SoC unter `hs_min_soc` stoppt sofort.
Nutzer-Aus nimmt die Freigabe zurück; eine eigene laufende WP behält dabei
ihre Mindestlaufzeit.

Auch bei eingeschalteter Automatik gibt unbekannter SoC keine neuen Starts
frei. Dazu zählen fehlende, beschädigte oder veraltete Livedaten, fehlende,
explizit ungültige oder unplausible Werte. Gültige 0 % bleiben ein Messwert,
auch bei `hs_min_soc = 0`. Eine eigene laufende WP beendet ihre bestehende
Mindestlaufzeit und erhält danach einen Aus-Auftrag, auch bei weiterhin
positivem Budget oder einem Wallbox-Übergang. Fehlende Bestätigung lässt den
Auftrag offen. Kehrt ein gültiger SoC vorher zurück, gelten wieder die
normalen Regeln; nach bestätigtem Aus gilt die Wiedereinschaltsperre.
Die Relaisrücklesung und gemessene Lastannahme beweisen keinen Verdichterlauf.
Der zum Relais gehörende Laufzeitbeleg bleibt über Dienstneustarts erhalten;
fehlt das Einschaltalter, beginnt einmalig der konservative Mindestlaufhalt.
Nach einem Neustart des Rechners lässt sich das Alter einer Lauf- oder
Ausschaltphase nicht aus der Systemuhr belegen. Eine laufende eigene WP behält
dann ab dem Neustart ihre volle Mindestlaufzeit, und eine bereits abgelaufene
Wiedereinschaltsperre (`wp_restart_block_min`) beginnt erneut mit voller Dauer.
Ein Start kann sich dadurch nach einem Neustart um bis zu diese Sperrzeit
verzögern.

Ein unabhängiger automatisch geregelter Heizstab wird bei unbekanntem SoC
ohne Verdichter-Mindestlaufhalt zurückgenommen. Das betrifft eigene ELWA-,
generische Modbus- und Shelly-Ausgänge; Markt-, Pre-Dump- und Preisbudgets
geben dabei keine neue Leistung frei. Der manuelle Vollgasauftrag hat einen
eigenen Regelpfad.

Wenn das zugewiesene WP-Budget wegfällt oder der SOC zu niedrig ist:

```
  [3EM] -> WP Relais 0 AUSgeschaltet (kein zugewiesenes WP-Budget (0W))
  [3EM] -> WP Relais 0 AUSgeschaltet (SOC zu niedrig (15% < 20.0%))
```

### Schaltlogik im Detail

Das Relais folgt dem Budget, das die zentrale Verbraucherverteilung der
Wärmepumpe zuweist, nicht dem rohen PV-Überschuss.

```
Einschalten:  zugewiesenes WP-Budget >= Startminimum UND SOC >= Mindest-SOC
              UND keine Wiedereinschaltsperre UND kein aktiver Wallbox-Übergang
Ausschalten:  zugewiesenes WP-Budget fällt auf 0 W ODER SOC < Mindest-SOC
              (bei Budget 0 erst nach Ablauf der Mindestlaufzeit,
              der SOC-Schutz stoppt sofort)

Startminimum: WP Mindestleistung, mindestens jedoch 1500 W
```

Ein lokales Totband gibt es für das Relais nicht mehr. Kurze Wolkenkanten fängt
die Mindestlaufzeit (`wp_min_runtime_min`, Standard 30 Minuten) ab, direktes
Neutakten nach einem Stop die Wiedereinschaltsperre (`wp_restart_block_min`,
Standard 20 Minuten).

**Beispiel** mit WP Mindestleistung = 1500 W:
```
Einschalten ab:  1500 W zugewiesenem WP-Budget
Ausschalten bei: 0 W zugewiesenem WP-Budget (nach der Mindestlaufzeit)
```

**Wallbox-Phasenwechsel:** Meldet die zentrale Verbraucherverteilung einen aktiven
Wallbox-Phasen- oder Stromübergang, stoppt das System eine laufende Wärmepumpe
deswegen nicht (Anzeige „WP läuft weiter: Wallbox-Übergang darf sie nicht
stoppen“) und startet eine stehende in dieser Zeit nicht. Der SOC-Schutz bleibt
davon unberührt.

---

## Schritt 6 — Dauerbetrieb sicherstellen

```bash
# Dienst dauerhaft aktivieren (startet automatisch nach Reboot)
sudo systemctl enable e3dc-heizstab.service

# Status prüfen
sudo systemctl status e3dc-heizstab.service --no-pager

# Letzten 20 Log-Zeilen
sudo journalctl -u e3dc-heizstab.service -n 20 --no-pager
```

---

## Schnellreferenz — Alle Konsolenbefehle

```bash
# Dienst starten
sudo systemctl start e3dc-heizstab.service

# Dienst stoppen
sudo systemctl stop e3dc-heizstab.service

# Dienst neu starten (nach Config-Änderung)
sudo systemctl restart e3dc-heizstab.service

# Live-Log verfolgen
sudo journalctl -u e3dc-heizstab.service -f --no-pager

# Status-JSON lesen
cat /var/www/html/ramdisk/heizstab_data.json | python3 -m json.tool

# Shelly direkt abfragen (Diagnose)
curl http://192.0.2.163/rpc/EM.GetStatus?id=0

# Relais manuell EIN (Diagnose, ID anpassen)
curl -X POST http://192.0.2.163/rpc/Switch.Set \
  -H "Content-Type: application/json" \
  -d '{"id":0,"on":true}'

# Relais manuell AUS
curl -X POST http://192.0.2.163/rpc/Switch.Set \
  -H "Content-Type: application/json" \
  -d '{"id":0,"on":false}'
```

---

## Häufige Fehler

| Symptom | Ursache | Lösung |
|---|---|---|
| `wp_type nicht 2 oder 3 - inaktiv` | wp_type noch auf 0 | Im Web-UI wp_type=3 setzen und speichern |
| `shelly_3em_ip fehlt` | IP nicht konfiguriert | shelly_3em_ip eintragen |
| `[!] Shelly Pro3EM <IP> Lesefehler: …` | Shelly nicht erreichbar (Zeitüberschreitung 3 s) | IP prüfen, Shelly im Browser öffnen; bis dahin gilt die WP-Leistung als unbekannt (`wp_power_w` ist `null`), nicht als 0 W |
| WP schaltet nie ein | Überschuss reicht nicht | WP-Mindestleistung prüfen (zu hoch?) |
| WP taktet schnell ein/aus | Mindestleistung knapp am Überschuss oder Taktschutz zu kurz | Mindestleistung leicht erhöhen und `wp_min_runtime_min`/`wp_restart_block_min` prüfen |
| Relais schaltet nicht | relay_id=-1 oder enable=0 | relay_id=0 und enable=1 setzen |

---

> [!NOTE]
> Die WP-Leistungsdaten (`wp_power_w`) fließen automatisch in die
> Langzeit-Statistik und das ML-Training ein — so lernt das System
> deinen WP-Verbrauch und plant die Ladekurve entsprechend besser.

> [!WARNING]
> Immer erst im Monitoring-Modus testen (relay_id=-1, enable=0),
> bevor du das automatische Schalten aktivierst.
> Eine WP darf nicht beliebig oft ein- und ausgeschaltet werden
> (Kompressor-Schutz beachten — mindestens 3-5 Minuten Pause).


### Gemeinsames Relais mit SG-Ready oder EVU-Kontakt

Zeigen die aktive Pro3EM-Relaissteuerung und ein Shelly-Kontakt des Energy
Managers auf dieselbe Adresse und dasselbe Relais, erscheint dieser Hinweis:
„Beide Dienste können dasselbe Shelly-Relais schalten. Eine der beiden
Zuordnungen sollte entfernt werden.“ Die Warnung ist beratend; die
Konfiguration kann gespeichert werden. Sie verändert das Schaltverhalten
keines Dienstes. Für dieses Relais sollte nur eine Zuordnung verwendet
werden, damit keine widersprüchlichen Schaltbefehle entstehen.

Hostnamen werden unabhängig von Groß-/Kleinschreibung und Leerzeichen
verglichen; ein fehlender HTTP-Port entspricht Port 80. Verschiedene Relais
bleiben unabhängig. Die bestehenden SG-Ready- und EVU-Ausgänge verwenden
fest Relais 0. Eine unlesbare Pro3EM-Relais-ID wird wie -1 behandelt
(nur Messung) und erzeugt keine Konfliktwarnung. Ein unbekannter Port bei
gleichem Host bleibt ein möglicher Konflikt.
