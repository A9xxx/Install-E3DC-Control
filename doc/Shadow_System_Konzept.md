# Shadow-Instanz: sicherer Vergleichsbetrieb

Die Shadow-Instanz ist eine optionale zweite E3DC-Control-Instanz für einen schreibgeschützten Vergleichs- und Testbetrieb. Sie liest ausschließlich Betriebs-Snapshots der aktiven Instanz und berechnet daraus Entscheidungen, ohne selbst Hardwarebefehle auszugeben.

Die vollständige Konfigurationsdatei der aktiven Anlage wird nicht übertragen. Zugangsdaten, Kennwörter, API-Schlüssel und Tokens verbleiben auf der aktiven Instanz. Vergleichsparameter werden auf der Shadow-Instanz lokal konfiguriert.

## Rollen und Schreibrechte

| Rolle | Zweck | Aktive Hardwaresteuerung |
|---|---|---|
| `master` | Aktive Regelinstanz | Ja |
| `slave` | HA-Fallback | Nur nach bestätigter Übernahme |
| `shadow` | Schreibgeschützte Vergleichsinstanz neben der aktiven Instanz | Nein, niemals |

Ein HA-Slave kann nach den HA-Regeln übernehmen. Eine Shadow-Instanz darf dagegen auch bei Master-Ausfall nicht aktiv werden. Keine Steuerbefehle an Wallbox, Wärmepumpe, Heizstab, MQTT-Ausgänge, Matter oder den Speicher dürfen aus dem Shadow-Pfad gesendet werden.

Die produktiven Writer- und Integrationsdienste einschließlich `e3dc-storage-manager`, `e3dc-wallbox-manager` und `e3dc-matter-bridge` bleiben im Shadow-Modus gestoppt. `e3dc-shadow-sync` holt ausschließlich die für Simulation und Vergleich benötigten Daten.

## Shadow-Instanz einrichten

Verwende für Beispiele ausschließlich eine Dokumentationsadresse. `192.0.2.10` stammt aus dem für Dokumentation reservierten Netz und ist kein reales Ziel.

```bash
curl -fsS http://192.0.2.10/get_live_json.php | head -c 200
```

Im Konfigurationseditor:

1. Bereich `High Availability / Shadow` öffnen.
2. `Cluster-Rolle` auf `Shadow (Simulation / Read-only)` stellen.
3. `Shadow Master URL` auf eine eigene, erreichbare Instanz setzen, zum Beispiel `http://192.0.2.10`. HTTPS sollte verwendet werden, wenn es auf der aktiven Instanz mit einem vertrauenswürdigen Zertifikat eingerichtet ist.
4. Shadow-Takt, HTTP-Timeout und maximales Snapshot-Alter prüfen; beim Speichern werden die Defaults `5`, `2.5` und `30` genutzt.
5. Nach dem Speichern sicherstellen, dass keine schreibenden Dienste aktiv sind.

Eine IP-Adresse ist nur der Host-Teil. Für `shadow_master_url` ist immer die vollständige Basis-URL mit `http://` oder `https://` erforderlich.

## Sicherheitsprüfungen

Die Diagnose meldet unter anderem:

- `master_points_to_self`, wenn die Shadow-Quelle auf die eigene Instanz zeigt;
- `active_writer_services`, wenn schreibende Dienste trotz Shadow-Rolle laufen;
- Alter und Erreichbarkeit des letzten Master-Snapshots;
- Rollen- und Peer-Widersprüche.

Bei einem Widerspruch gilt der sicherere Zustand. Eine Shadow-Auswertung darf als fachlich passender oder weniger passend bewertet werden, aber niemals Schreibrechte erhalten. Aussagen wie „Shadow wirkt passender“ sind reine Vergleichsergebnisse; die aktive Regelinstanz bleibt alleiniger Hardware-Owner.

## Abbruchbedingungen

Der Vergleich ist abzubrechen, wenn die Quelle auf die eigene Instanz zeigt, Snapshots dauerhaft veraltet sind, die Rollen nicht eindeutig sind oder ein Writer-Dienst aktiv wird. Vor einer späteren HA-Nutzung ist eine getrennte HA-Freigabe erforderlich; ein Shadow-System wird nicht allein durch Konfigurationsänderung zum produktiven Fallback.
