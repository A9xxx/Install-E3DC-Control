# Smart Home Matter Bridge

Die Matter-Bridge bringt Live-Werte und einfache Schaltzustände von E3DC-Control lokal in Apple Home, Google Home und andere Matter-fähige Smart-Home-Systeme. Sie ist eine lokale, nicht zertifizierte read-only Statusintegration und arbeitet ohne Cloud.

## Datenfluss

```text
E3DC RSCP -> e3dc_live.py -> Ramdisk JSON -> get_live_json.php -> Matter Bridge
```

Die Bridge nutzt die bestehenden Live-Daten aus der V4-Architektur. Sie liest keine Legacy-Config direkt.

## Aktivierung

Aktiviere Matter im Config-Editor. Gespeichert wird in:

```text
/var/www/html/data/e3dc_v4.json
```

Typische Parameter:

```ini
matter_bridge = 1
matter_port = 5540
```

## Dienst

Bare Metal:

```bash
sudo systemctl restart e3dc-matter-bridge
sudo systemctl status e3dc-matter-bridge
```

Docker:

```bash
docker restart e3dc-control
docker exec e3dc-control tail -f /var/www/html/logs/matter_bridge.log
```

## Verfügbare Entitäten

Die Bridge stellt aktuell genau drei virtuelle, ausschließlich lesende
Statusschalter bereit:

- `E3DC Wallbox aktiv`: EIN bei gemessener Wallboxleistung über 50 W.
- `E3DC PV produziert`: EIN bei gemessener PV-Leistung über 500 W.
- `E3DC Einspeisung aktiv`: EIN bei gemessener Netzeinspeisung über 500 W.

Die Schalter zeigen Zustände an. Eine Betätigung in Apple Home, Google Home
oder einem anderen Matter-System erzeugt keinen Wallbox-, PV- oder
Netzsteuerbefehl. Weitere Entitäten wie Hausverbrauch, Batterie-SoC,
Wärmepumpe oder Notstrom werden in dieser Version nicht bereitgestellt.

## Hinweise

- Matter ist lokal und reagiert empfindlich auf mDNS/IPv6/Firewall-Probleme.
- Wenn ein Gerät nicht auftaucht, zuerst `avahi-daemon` und den Matter-Dienst prüfen.
- Die stabile MQTT-Integration bleibt für Home Assistant weiterhin die robustere Option.
