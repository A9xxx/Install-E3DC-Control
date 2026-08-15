# Smart Home Matter Bridge

Die Matter-Bridge bringt Live-Werte und einfache Schaltzustände von E3DC-Control lokal in Apple Home, Google Home und andere Matter-fähige Smart-Home-Systeme. Sie ist eine lokale, nicht zertifizierte read-only Statusintegration und arbeitet ohne Cloud.

## Datenfluss

```text
E3DC RSCP -> e3dc_live.py -> Ramdisk JSON -> Matter Bridge
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
```

Die Bridge verwendet den festen Matter-Standardport `5540`. Dieser Port ist
derzeit nicht als Konfigurationsparameter freigegeben.

Matter ist ein optionales Modul. Das normale E3DC-Control-Update installiert
deshalb keine Node.js-, npm-, Avahi- oder D-Bus-Pakete. Erst die ausdrücklich
gestartete Matter-Installation prüft diese Paketgruppe gemeinsam und bricht bei
einem Solverfehler ab, ohne die Core-Aktualisierung zu blockieren.

Die Node-Abhängigkeiten sind vollständig in `package-lock.json` gebunden und
werden mit `npm ci --omit=dev --ignore-scripts` installiert. Die Bridge nutzt
Matter.js `0.12.6`; nicht benötigte QR- und TypeScript-Laufzeitpakete gehören
nicht zum installierten Produktionsbaum. Die Installation verlangt dafür
Node.js 18 oder neuer und bricht mit einer eindeutigen Meldung ab, wenn das
optionale Matter-Modul auf einem älteren System gestartet wird.

## Bestehende Kopplungen bei Aktualisierungen

Die Aktualisierung auf Matter.js `0.12.6` bleibt bewusst auf der kompatiblen
Matter.js-Geräte-API und verwendet den vorhandenen Storage weiter. Sie löscht
keine Kopplungen und verlangt keinen Werksreset. Lösche
`/var/www/html/data/matter-storage` nur über den ausdrücklich bestätigten
Werksreset in der Weboberfläche.

Eine spätere Umstellung auf die neue native `ServerNode`-API wird getrennt
angekündigt: Deren Storageformat ist laut Matter.js-Migrationsvertrag nicht mit
der bisherigen Geräte-API kompatibel und erfordert eine neue Kopplung.

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
