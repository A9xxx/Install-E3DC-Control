# Smart Home Matter Bridge

Die optionale Matter Bridge stellt drei lokale read-only Statusschalter von E3DC-Control für Apple Home, Google Home, SmartThings und andere Matter-Systeme bereit:

- **E3DC Wallbox aktiv**: ein, wenn die Wallbox mehr als 50 W aufnimmt;
- **E3DC PV produziert**: ein, wenn die PV-Leistung mehr als 500 W beträgt;
- **E3DC Einspeisung aktiv**: ein, wenn mehr als 500 W ins Netz eingespeist werden.

Die Schalter eignen sich als Auslöser oder Bedingung für Routinen. Befehle aus dem Matter-System werden nicht an Speicher, Wallbox, Wärmepumpe oder andere Anlagenkomponenten weitergegeben.

## Voraussetzungen

- Node.js und npm;
- `avahi-daemon`, `avahi-utils` und D-Bus für die lokale mDNS-Erkennung;
- erreichbarer Matter-Port `5540/UDP` im lokalen Netz;
- IPv6 und Multicast dürfen zwischen E3DC-Control und dem Matter-Controller nicht blockiert sein.

Die Bridge ist ein nicht zertifizierter Matter-Knoten im Beta-Betrieb. Sie arbeitet lokal ohne Cloud-Verbindung. Die Kopplung wird dauerhaft unter `/var/www/html/data/matter-storage` gespeichert und vom E3DC-Control-Backup erfasst.

## Aktivieren und installieren

1. Im Konfigurationseditor unter **Webansicht & Updates** die Matter Bridge einschalten.
2. Im Installationszentrum das Modul **Matter Bridge** installieren beziehungsweise starten.
3. Die Seite **Smart Home (Matter)** öffnen.
4. Den angezeigten manuellen Pairing-Code in Apple Home, Google Home oder dem verwendeten Matter-Controller eingeben.

Der Pairing-Code wird nicht an einen externen QR-Code-Dienst übertragen.

Bare Metal:

```bash
sudo systemctl status e3dc-matter-bridge
sudo journalctl -u e3dc-matter-bridge -e
```

Docker:

```bash
docker compose restart
docker logs --tail=100 e3dc-control
```

Im Docker-Betrieb wird die Bridge beim Containerstart aktiviert, wenn `matter_bridge=1` gesetzt ist.

## Kopplung zurücksetzen

Die Schaltfläche **Auf Kopplung zurücksetzen** löscht die lokalen Matter-Kopplungsschlüssel und startet den Dienst neu. Danach müssen alle Matter-Plattformen erneut gekoppelt werden. Der Vorgang ist durch Web-Anmeldung, Bestätigungsdialog und CSRF-Schutz abgesichert.

Für Multi-Admin wird die vorhandene Kopplung nicht zurückgesetzt. Stattdessen in der zuerst gekoppelten Plattform den Freigabe- beziehungsweise Kopplungsmodus aktivieren und den dort erzeugten neuen Code in der weiteren Plattform verwenden.

## Fehlerdiagnose

- Dienst nicht aktiv: `systemctl status e3dc-matter-bridge` und Journal prüfen.
- Bridge wird nicht gefunden: Avahi, IPv6, Multicast und lokale Firewall prüfen.
- Pairing-Datei fehlt: Dienst starten und Seite nach einigen Sekunden neu laden.
- Nach Docker-Konfigurationsänderung: Container neu erstellen oder neu starten.
- Kopplung nach Restore verloren: prüfen, ob `matter-storage` im verwendeten Backup enthalten war.

MQTT bleibt für umfangreiche Home-Assistant-Integrationen die flexiblere Schnittstelle; Matter bietet bewusst nur die drei einfachen lokalen Statusschalter.
