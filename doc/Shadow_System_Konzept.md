# Shadow-Instanz: sicherer Vergleichsbetrieb

Die Shadow-Instanz ist eine optionale zweite E3DC-Control-Instanz für einen schreibgeschützten Vergleichs- und Testbetrieb. Sie liest ausschließlich Betriebs-Snapshots der aktiven Instanz und berechnet daraus Entscheidungen, ohne selbst Hardwarebefehle auszugeben.

Die vollständige Konfigurationsdatei der aktiven Anlage wird nicht übertragen. Zugangsdaten, Kennwörter, API-Schlüssel und Tokens verbleiben auf der aktiven Instanz. Nur das gemeinsame `shadow_snapshot_token` wird einmalig auf Master und Shadow identisch hinterlegt; der Snapshot-Endpunkt überträgt, protokolliert oder projiziert es niemals.

## Rollen und Schreibrechte

| Rolle | Zweck | Aktive Hardwaresteuerung |
|---|---|---|
| `master` | Aktive Regelinstanz | Ja |
| `slave` | HA-Fallback | Nur nach bestätigter Übernahme |
| `shadow` | Schreibgeschützte Vergleichsinstanz neben der aktiven Instanz | Nein, niemals |

Ein HA-Slave kann nach den HA-Regeln übernehmen. Eine Shadow-Instanz darf dagegen auch bei Master-Ausfall nicht aktiv werden. Keine Steuerbefehle an Wallbox, Wärmepumpe, Heizstab, MQTT-Ausgänge, Matter oder den Speicher dürfen aus dem Shadow-Pfad gesendet werden.

Die produktiven Writer- und Integrationsdienste einschließlich `e3dc-storage-manager`, `e3dc-wallbox-manager` und `e3dc-matter-bridge` bleiben im Shadow-Modus gestoppt. `e3dc-shadow-sync` holt ausschließlich die für Simulation und Vergleich benötigten Daten.

## Shadow-Instanz einrichten

Erzeuge das Peer-Geheimnis einmalig auf einem administrativen System. Das Ergebnis besteht aus 64 Hex-Zeichen:

```bash
openssl rand -hex 32
```

Trage diesen Wert auf beiden Instanzen als `Shadow Snapshot Token` ein. Gib ihn nicht als Klartext in Supportnachrichten, Diagnose-ZIPs oder Shell-Kommandos weiter. Ein fehlender oder ungültiger lokaler Wert sperrt den Endpunkt mit HTTP 503. Ein fehlender oder falscher Peer-Wert wird einheitlich mit HTTP 403 abgewiesen. Der zusätzliche Header `X-E3DC-Shadow-Contract: e3dc-shadow-read-v1` bleibt die Versionsbindung und ersetzt die Authentisierung nicht.

Der Shadow-Sync greift nicht direkt auf `ramdisk/` oder `data/` zu. Diese Webpfade bleiben durch Apache gesperrt. Der dedizierte Endpunkt liefert nur feste, positiv typisierte Read-only-Projektionen. Seriennummern, RFID-Daten, Fahrzeug- und Gerätenamen, Host-/IP-Felder, freie Kommandodaten sowie vollständige Plan-Slots werden nicht übertragen. Beim Speicherplan werden nur Ziel-, Unter- und Oberkurve samt den für den Regler erforderlichen Metadaten übertragen. Die Config-Projektion enthält ausschließlich ausdrücklich erlaubte Anlagen- und Reglerparameter; das Peer-Geheimnis gehört nie dazu.

Im Konfigurationseditor:

1. Bereich `High Availability / Shadow` öffnen.
2. `Cluster-Rolle` auf `Shadow (Simulation / Read-only)` stellen.
3. Dasselbe 64-stellige `Shadow Snapshot Token` auf Master und Shadow hinterlegen.
4. `Shadow Master URL` auf die eigene aktive Instanz setzen. Für HTTP ist ausschließlich eine literale Loopback-, RFC1918-, CGNAT-/Tailscale- oder private IPv6-ULA-Adresse zulässig, zum Beispiel `http://192.168.1.10`. DNS-Namen und öffentliche Ziele erfordern HTTPS.
5. Bei HTTPS ein Zertifikat verwenden, das die normale System-Zertifikatsprüfung besteht. Benutzername oder Passwort in der URL, Pfade, Query-Parameter und Fragmente sind unzulässig.
6. Shadow-Takt, HTTP-Timeout und maximales Snapshot-Alter prüfen; beim Speichern werden die Defaults `5`, `2.5` und `30` genutzt.
7. Nach dem Speichern sicherstellen, dass keine schreibenden Dienste aktiv sind.

Eine IP-Adresse ist nur der Host-Teil. Für `shadow_master_url` ist immer die vollständige Basis-URL mit `http://` oder `https://` erforderlich.

## Sicherheitsprüfungen

Die Diagnose meldet unter anderem:

- `snapshot_token_invalid`, wenn das lokale Peer-Geheimnis fehlt oder nicht exakt 64 Hex-Zeichen hat; vor diesem Gate findet kein Netzwerkzugriff statt;
- `master_points_to_self`, wenn die Shadow-Quelle auf die eigene Instanz zeigt;
- `active_writer_services`, wenn schreibende Dienste trotz Shadow-Rolle laufen;
- fehlende, ungültige, mehr als 60 Sekunden zukünftige oder veraltete `_ts`-Zeitstempel der verpflichtenden primären `live_data`-Quelle;
- fehlende Pflichtressourcen getrennt für Speicher- und Wallbox-Simulation;
- Rollen- und Peer-Widersprüche.

Bei einem Widerspruch gilt der sicherere Zustand. Fehlt eine Pflichtressource, schreibt der betroffene Simulator keine neue abgeleitete Datei; eine ältere Datei bleibt nur diagnostischer Preimage und wird von der WebUI bei `PAUSED` oder veralteter Quelle nicht als frisch verwendet. Eine Shadow-Auswertung darf als fachlich passender oder weniger passend bewertet werden, aber niemals Schreibrechte erhalten. Aussagen wie „Shadow wirkt passender“ sind reine Vergleichsergebnisse; die aktive Regelinstanz bleibt alleiniger Hardware-Owner.

## Abbruchbedingungen

Der Vergleich ist abzubrechen, wenn die Quelle auf die eigene Instanz zeigt, Snapshots dauerhaft veraltet sind, die Rollen nicht eindeutig sind oder ein Writer-Dienst aktiv wird. Vor einer späteren HA-Nutzung ist eine getrennte HA-Freigabe erforderlich; ein Shadow-System wird nicht allein durch Konfigurationsänderung zum produktiven Fallback.
