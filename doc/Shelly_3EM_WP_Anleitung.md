# Shelly Pro3EM — Waermepumpe einbinden (wp_type=3)

> **Fuer:** Nutzer ohne native WP-Anbindung (kein Luxtronik, kein IDM)
> **Ziel:** Shelly Pro3EM misst die WP-Leistung auf allen 3 Phasen
>           und schaltet optional das integrierte Relais bei PV-Ueberschuss.
> **Dienst:** `e3dc-heizstab.service` (wird fuer 3EM mitgenutzt)

---

## Voraussetzungen

- E3DC-Control V4 laeuft (alle Kerndienste aktiv)
- Shelly Pro3EM ist im Heimnetz erreichbar (feste IP empfohlen!)
- Shelly Gen2 Firmware (RPC API muss aktiv sein — Standard ab Werk)
- SSH-Zugang zum Raspberry Pi

---

## Schritt 1 — Shelly Pro3EM vorbereiten

### 1a. Feste IP im Router vergeben

Vergib dem Shelly im DHCP-Server deines Routers eine feste IP, z.B. `192.0.2.163`.
So aendert sich die IP nie und der Dienst findet ihn immer.

### 1b. Erreichbarkeit testen (vom PC aus)

Oeffne im Browser:
```
http://192.0.2.163/rpc/EM.GetStatus?id=0
```

Du solltest eine JSON-Antwort mit `current_a`, `voltage`, `act_power` sehen.
Wenn das funktioniert — alles bereit.

### 1c. Shelly an die WP anschliessen

- Phase(n) der WP durch den Pro3EM fuehren (Einbau durch Elektriker!)
- Wenn du das **Relais** nutzen willst (Ein/Aus-Steuerung): Schaltausgang des Shelly
  in die Steuersignalleitung der WP einschleifen (z.B. SG-Ready Kontakt)
- Wenn du **nur messen** willst (Monitoring): Nur die Stromwandler klemmen, Relais offen lassen

---

## Schritt 2 — Dienst pruefen / installieren

Verbinde dich per SSH mit dem Pi:

```bash
ssh pi@192.0.2.36
```

Pruefe ob der Heizstab-Dienst bereits existiert:

```bash
sudo systemctl status e3dc-heizstab.service
```

**Falls der Dienst fehlt** (Fehlermeldung "not found"):

```bash
# Service-Datei anlegen
sudo nano /etc/systemd/system/e3dc-heizstab.service
```

Inhalt einfuegen (Strg+Shift+V):

```ini
[Unit]
Description=E3DC Heizstab / Shelly-3EM WP Manager
After=network.target e3dc-live.service
Wants=e3dc-live.service

[Service]
Type=simple
User=pi
WorkingDirectory=/home/pi/Install/Installer
ExecStart=/home/pi/.venv_e3dc/bin/python3 /home/pi/Install/Installer/heizstab_manager.py
Restart=always
RestartSec=15
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Speichern mit Strg+O, Enter, Strg+X.

Dienst registrieren:

```bash
sudo systemctl daemon-reload
sudo systemctl enable e3dc-heizstab.service
```

---

## Schritt 3 — Konfiguration im Web-UI

Oeffne den Konfiguration Editor im Dashboard:
**Einstellungen -> Konfiguration Editor -> V4 Smart Home (Regelung & KI)**

Dort im Abschnitt **Waermepumpen Typ**:

| Feld | Wert |
|---|---|
| **Waermepumpen Typ** | `Shelly Pro3EM (WP ohne native Anbindung)` auswaehlen |
| **Shelly Pro3EM IP-Adresse** | z.B. `192.0.2.163` |
| **Automatik darf Geräte steuern** | Für reines Messen optional; zum Schalten einschalten |
| **Relais-ID** | `-1` = nur messen / `0` = Relais 0 schalten |
| **PV-Auto-Steuerung** | `0 = Nur messen` oder `1 = PV-Auto (Schalten)` |
| **WP Mindestleistung (W)** | z.B. `1500` (Einschaltschwelle: ab wieviel W Ueberschuss darf die WP starten) |
| **WP Nennleistung (W)** | z.B. `3000` (laut Typenschild deiner WP) |
| **Mindest-SOC (%)** | z.B. `20` (Batterie-Reserve: WP startet nicht wenn SOC darunter) |

**Speichern** druecken.

Ab `5.1.8h` liest der Dienst die Pro3EM-Leistung auch dann aus, wenn
Heizstab-/PV-Auto deaktiviert ist. Fuer reines Monitoring reicht:

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

Dann Dienst starten und Live-Log beobachten:

```bash
sudo systemctl start e3dc-heizstab.service
sudo journalctl -u e3dc-heizstab.service -f --no-pager
```

Du solltest alle 15 Sekunden eine Zeile sehen wie:

```
[10:23:45] PV=4500W Grid=-1200W SOC=72%
  [3EM] WP=2300W (A:780W B:760W C:760W) [LAEUFT]
```

Das bestaetigt: Shelly Pro3EM wird korrekt ausgelesen.

Ramdisk-Status pruefen:

```bash
cat /var/www/html/ramdisk/heizstab_data.json | python3 -m json.tool | head -30
```

Relevante Felder:

```json
{
  "wp_power_w":   2300.0,    <- Gesamtleistung WP (alle 3 Phasen)
  "wp_phase_a_w": 780.0,     <- Phase A
  "wp_phase_b_w": 760.0,     <- Phase B
  "wp_phase_c_w": 760.0,     <- Phase C
  "wp_is_running": true,     <- true wenn >= 30% der Mindestleistung
  "wp_relay_on":  false,     <- Relaisstatus
  "wp_takt_protect_active": false, <- Mindestlaufzeit/Wiedereinschaltsperre aktiv
  "surplus_w":    1200.0,    <- Verfuegbarer PV-Ueberschuss
  "soc":          72.0       <- Aktueller Batterie-SoC
}
```

---

## Schritt 5 — Automatische Relais-Steuerung aktivieren (optional)

Wenn die Messung korrekt laeuft und du das Relais auch schalten moechtest:

Im Web-UI aendern:

```
Relais-ID      = 0     (oder 1/2 je nach Anschluss)
PV-Auto        = 1     (PV-Auto-Steuerung ein)
```

Dienst neu starten:

```bash
sudo systemctl restart e3dc-heizstab.service
sudo journalctl -u e3dc-heizstab.service -f --no-pager
```

Wenn genuegend PV-Ueberschuss vorhanden und SOC >= Minimum:

```
  [3EM] -> WP Relais 0 EINgeschaltet
```

Ab 5.1.8b nutzt das Pro3EM-Relais die allgemeinen Waermepumpen-Taktschutzwerte
`wp_min_runtime_min` und `wp_restart_block_min`. Kurze Wolkenkanten halten das
Relais deshalb bis zur Mindestlaufzeit und blockieren direkte Neustarts nach
einem Stop; SoC-Schutz und manuelles/Auto-Aus bleiben harte Stopps.

Wenn Ueberschuss wegbricht oder SOC zu niedrig:

```
  [3EM] -> WP Relais 0 AUSgeschaltet (Ueberschuss zu gering (800W < 1000W))
```

### Schaltlogik im Detail

```
Einschalten:  PV-Ueberschuss >= WP-Mindestleistung UND SOC >= Mindest-SOC
Ausschalten:  PV-Ueberschuss <  (Mindest-Ueberschuss - 500W Hysterese)
              ODER SOC < Mindest-SOC

Hysterese:    500W Totband verhindert schnelles Ein/Aus-Takten bei schwankender PV
```

**Beispiel** mit Mindestleistung=1500W:
```
Einschalten ab:  1500W Ueberschuss
Ausschalten bei: 1500 - 500 = 1000W Ueberschuss (erst dann geht Relais aus)
```

---

## Schritt 6 — Dauerbetrieb sicherstellen

```bash
# Dienst dauerhaft aktivieren (startet automatisch nach Reboot)
sudo systemctl enable e3dc-heizstab.service

# Status pruefen
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

# Dienst neu starten (nach Config-Aenderung)
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

## Haeufige Fehler

| Symptom | Ursache | Loesung |
|---|---|---|
| `wp_type nicht 2 oder 3 - inaktiv` | wp_type noch auf 0 | Im Web-UI wp_type=3 setzen und speichern |
| `shelly_3em_ip fehlt` | IP nicht konfiguriert | shelly_3em_ip eintragen |
| `[3EM] WP=0W [STAND]` | Shelly nicht erreichbar | IP pruefen, Shelly im Browser oeffnen |
| WP schaltet nie ein | Ueberschuss reicht nicht | WP-Mindestleistung pruefen (zu hoch?) |
| WP taktet schnell ein/aus | Mindestleistung knapp am Ueberschuss oder Taktschutz zu kurz | Mindestleistung leicht erhoehen und `wp_min_runtime_min`/`wp_restart_block_min` pruefen |
| Relais schaltet nicht | relay_id=-1 oder enable=0 | relay_id=0 und enable=1 setzen |

---

> [!NOTE]
> Die WP-Leistungsdaten (`wp_power_w`) fliessen automatisch in die
> Langzeit-Statistik und das ML-Training ein — so lernt das System
> deinen WP-Verbrauch und plant die Ladekurve entsprechend besser.

> [!WARNING]
> Immer erst im Monitoring-Modus testen (relay_id=-1, enable=0),
> bevor du das automatische Schalten aktivierst.
> Eine WP darf nicht beliebig oft ein- und ausgeschaltet werden
> (Kompressor-Schutz beachten — mindestens 3-5 Minuten Pause).
