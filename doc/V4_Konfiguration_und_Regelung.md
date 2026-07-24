# Konfiguration und Regelung

Diese Datei beschreibt den aktuellen Stand der nativen Python-Architektur.

## Konfigurationsquelle

Die kanonische Konfiguration ist:

```text
/var/www/html/data/e3dc_v4.json
```

`e3dc.config.txt` ist nur noch ein Legacy-Import und Fallback für alte
Installationen, Migrationen und einzelne Debug-Werkzeuge. Neue
WebUI-Einstellungen, Dienste und Installer-Optionen sollen in `e3dc_v4.json`
gespeichert werden.

Wichtige Folgen:

- Der Config-Editor arbeitet direkt mit `e3dc_v4.json`.
- Python-Dienste lesen zuerst `e3dc_v4.json`.
- `e3dc.config.txt` darf nicht mehr als primäre Quelle für neue Features
  verwendet werden.
- `e3dc.wallbox.txt` und `e3dc.wallbox.out` bleiben nur für Legacy-Importe,
  alte Debug-Werkzeuge und historische Ladepläne relevant.

## Aktive Dienste

| Dienst | Aufgabe | Primäre Datenquelle |
|---|---|---|
| `e3dc-live` / `e3dc_live.py` | Live-Daten aus RSCP in die Ramdisk schreiben | `e3dc_v4.json` |
| `e3dc-storage-simulator` / `storage_simulator.py` | PV-, Wetter-, Preis- und SoC-Plan für die Ladekurve berechnen | `e3dc_v4.json`, Ramdisk |
| `e3dc-storage-manager` / `storage_manager.py` | Batterie-EMS nach Ladekurve, Pre-Dump, Abregelgrenze und Preislogik führen | `storage_plan.json`, `storage_manager_state.json` |
| `e3dc-wallbox-manager` / `wallbox_manager.py` | Native und externe Wallboxen nach Budget, Modus und Hysterese regeln | `storage_manager_state.json`, `e3dc_v4.json` |
| `energy_manager` | Luxtronik, IDM, Stiebel/ISG, SG-Ready und Heizstab nach Budget steuern | `storage_manager_state.json`, `e3dc_v4.json` |
| `e3dc-epex-manager` / `epex_manager.py` | EPEX/SMARD/ENTSO-E/aWATTar-Preise, Nutzerpreis und Eco-Score liefern | `e3dc_v4.json` |
| `e3dc-bluelink` / `bluelink_client.py` | Hyundai/Kia SoC in `vehicles.json` schreiben | `e3dc_v4.json` |
| `e3dc-mqtt-hub` / `e3dc_mqtt_hub.py` | Livewerte publizieren und freigegebene MQTT-Messwerte annehmen | `e3dc_v4.json`, Ramdisk |

## Speicher und Ladekurve

Die Ladekurve ist eine Soll-SoC-Trajektorie für den Speicher. Sie wird vom
`storage_simulator.py` mit Wetterprognose, Verbrauchsmodell, EPEX/Eco-Score,
saisonalem Nachtverbrauch und optionalem Mittagsziel erstellt.

Aktueller Stand:

- vergangene und aktive Stützpunkte werden eingefroren,
- kommende Punkte werden rollierend geglättet,
- zukünftige Pre-Dump-/Startanker werden nicht auf den aktuellen SoC
  hochgezogen,
- Pre-Dump schafft vor Kurvenstart gezielt Platz gegen Abregelung,
- Pre-Dump kann freigegebene Verbraucher über ein gemeinsames Budget nutzen,
- Wärmepumpenleistung wird als eigener Verbraucher geführt und nicht doppelt im
  Hausverbrauch gezählt,
- der Manager führt die Kurve weich über `iFc`, Kontroll-SoC und geglätteten
  Aufholbedarf,
- abends wird nicht mehr zwanghaft auf die Kurve entladen, weil das Haus den
  Speicher ohnehin natürlich nutzt.

Wenn E3DC-Control diese Ladekurve führt, sollte das wetterbasierte Laden im
E3/DC-Hauskraftwerk deaktiviert sein. Die E3/DC-Funktion ist ein eigener
Ladeplaner und kann die Batterieladung trotz einer von E3DC-Control gesetzten
AUTO-Ladeobergrenze zurückhalten. Die Open-Meteo-/Forecast-Prognose von
E3DC-Control arbeitet davon unabhängig weiter. Erkennt E3DC-Control gleichzeitig
die E3/DC-Statussignale `Laden gesperrt` und `Warten auf Sonnenschein`, wird die
Kurvenladung als extern zurückgehalten angezeigt. Die Geräteeinstellung wird
weder automatisch noch zyklisch über RSCP verändert.

## Wallbox-Regelung

Die Wallbox-Regelung arbeitet pro Wallbox mit Modus, Mindest-SoC, Budget und
Hysterese. Die sichtbaren Nutzer-Modi sind bewusst klein gehalten:

| UI-Modus | Bedeutung |
|---|---|
| `Aus` | NGNA: keine aktive E3DC-Control-Ladung und keine laufenden Steuerbefehle. Nur beim bewussten Wechsel auf `Aus` in der WebUI wird einmalig die Wallbox-Grundeinstellung freigegeben. Geplantes Netzladen bleibt gesperrt. |
| `PV-Kurve ruhig` | Laden entlang der Speicher-Ladekurve mit Hysterese; Speicherziel hat Vorrang. |
| `Grundladung stabil` | Wie PV-Kurve ruhig, aber mit stabiler 1p/3p-Grundladung, solange `wbminSoc` laut Planung erreichbar bleibt. |
| `PV + Akku bis Untergrenze` | Das Auto darf PV plus Hausspeicher oberhalb der Hausakku-Untergrenze nutzen; unterhalb der Grenze stützt der Speicher nur Hausverbrauch und Wärmepumpe, Netz bleibt außen vor. |
| `Sofort bis Preislimit` | Sofortiges Netzladen, solange der aktuelle Preis das Wallbox-Preislimit erfüllt. |

Geplantes Netzladen per Zeitfenster/Slot darf in allen aktiven Modi laden und
ignoriert bewusst das globale Wallbox-Preislimit, weil die Preislogik bereits
bei der Slot-Auswahl steckt.

Wichtige Schutzlogik:

- Schwellen wie `wbminsoc` arbeiten mit Hysterese.
- openWB Pro wird direkt über `connect.php` gesteuert, wenn E3DC-Control Master
  sein soll.
- openWB Software 2.x als `primary` bleibt eigener Energiemanager; E3DC-Control
  darf nur per bewusst aktiviertem Primary-Pfad Modus/Stop/Sofortladen über
  simpleAPI setzen.
- openWB Software 2.x als `secondary` muss in openWB selbst auf
  `Steuerungsmodus: secondary` und `Steuerung über Modbus als secondary: An`
  stehen. Dann setzt E3DC-Control Sollstrom plus Heartbeat.
- Direkte MQTT-Leistungstopics wie `evcc/loadpoints/1/chargePower` werden als
  reale Wallboxleistung angenommen und aus dem reinen Hausverbrauch
  herausgerechnet.

## Wärmepumpen

Luxtronik, IDM, Stiebel/ISG, SG-Ready und Heizstab arbeiten nicht als
Nebenregler am Speicher vorbei. Sie erhalten Budget, Freigabe und
Mindestlaufzeiten aus dem Energy Manager beziehungsweise dem Storage Manager.

Der Storage Manager kann die echte elektrische Wärmepumpenleistung aus
`energy_decision_latest.json` übernehmen. Diese Leistung wird als `WP_Power`
geführt und aus dem Hausverbrauch bereinigt, wenn der Zähler sie dort bereits
enthält.

## Preislogik

Der netzdienliche Eco-Modus und der Negativpreis-/Preis-Boost sind Opt-in-Pfade.
Unbekannte externe Dauerlasten werden nicht geraten. Wenn ein BEV, eine
Wärmepumpe oder ein anderer großer Verbraucher regelrelevant sein soll, muss
seine Leistung eingebunden oder geplant sein.

## Dokumentationsregel

Wenn eine Doku von Konfiguration spricht, ist damit diese zentrale Datei gemeint:

```text
data/e3dc_v4.json
```

Nur Migrations-, Rollback- und Legacy-Abschnitte sollen `e3dc.config.txt` als
aktive Datei nennen.
