# E3DC-Control V4: API-Dokumentation für externe Frontends & Widgets

Diese Dokumentation beschreibt die verfügbaren API-Endpunkte deiner E3DC-Control-Installation zur Abfrage von Live-Daten, Prognosen und historischen Verläufen. Die Endpunkte sind im lokalen Netz und über einen eingerichteten Cloudflare-Tunnel erreichbar. Sie sind für Widgets, Apps, Home-Assistant-REST-Sensoren und Skripte gedacht, die die Anfrage selbst senden. Browser-Anfragen von fremden Webseiten (Cross-Origin, CORS) sind nicht freigegeben, siehe Abschnitt 3.

---

## 1. Authentifizierung & Sicherheit

Alle Anfragen an die API-Endpunkte müssen authentifiziert werden, wenn eine `web_pin` in deiner Konfiguration hinterlegt ist. Ist keine `web_pin` gesetzt, ist die API ohne Anmeldung offen.

### API-Schlüssel per Header
Sende den API-Schlüssel in einem der beiden gleichwertigen Header-Felder:
```http
X-API-PIN: <DEINE_WEB_PIN>
```
oder
```http
Authorization: Bearer <DEINE_WEB_PIN>
```

Beide Wege werden gleich geprüft. Schickt ein Client beide Header, wird der Wert aus `Authorization: Bearer` verwendet.

### Sperre nach Fehlversuchen
*   Für beide Header-Wege gilt dieselbe Sperre wie für die Anmeldung im Browser: Nach 5 falschen PIN-Versuchen wird die Client-Adresse für 10 Minuten gesperrt. Fehlversuche über `X-API-PIN` und `Authorization: Bearer` zählen gemeinsam.
*   Während der Sperre antwortet die API auch mit der richtigen PIN mit HTTP `403`. Eine erfolgreiche Anmeldung setzt den Zähler zurück.
*   Die Sperre gilt je Client-Adresse, bei IPv6 je /64-Netz. Ein Widget mit veralteter PIN sperrt deshalb auch die Browser-Anmeldung vom selben Gerät bzw. Netz. Nach einer PIN-Änderung zuerst alle Widgets und Skripte anpassen.
*   Bereits angemeldete Browser-Sitzungen bleiben von der Sperre unberührt.
*   Hinter einem Tunnel oder Reverse-Proxy: Die vom Cloudflare-Tunnel übermittelte Client-Adresse (`CF-Connecting-IP`) wird nur ausgewertet, wenn der Tunnel lokal auf demselben System angebunden ist (Verbindung von `127.0.0.1` bzw. `::1`). Andere Weiterleitungs-Header wie `X-Forwarded-For` werden nicht ausgewertet. Kommen alle Anfragen über eine gemeinsame Adresse an, teilen sich diese Clients eine Sperre; Fehlversuche Dritter können dann auch die eigenen Widgets für 10 Minuten aussperren.
*   Docker im Bridge-Netz: Ein Tunnel auf dem Host oder in einem eigenen Container erreicht den Container über die Docker-Netzadresse, nicht über `127.0.0.1`. `CF-Connecting-IP` wird dann nicht ausgewertet, und alle Tunnel-Zugriffe teilen sich eine Sperre. Mit `network_mode: host` und dem Tunnel auf demselben Host gilt die Verbindung als lokal.
*   Betreibst du auf demselben System einen anderen Reverse-Proxy (z. B. nginx, Caddy oder den Reverse Proxy einer Synology), muss er einen vom Client mitgeschickten `CF-Connecting-IP`-Header entfernen oder überschreiben. Sonst lässt sich die Sperre mit wechselnden Header-Werten umgehen.
*   Ein vorgeschalteter Proxy oder Anmeldedienst, der einen eigenen `Authorization: Bearer`-Header an E3DC-Control weiterreicht, löst bei jeder Anfrage einen Fehlversuch aus, weil dieser Wert als PIN geprüft wird. Nach 5 Anfragen ist die Adresse gesperrt, auch für die Anmeldung im Browser. Richte den Proxy so ein, dass er diesen Header nicht an E3DC-Control weiterreicht. Ein zusätzlich mitgeschickter `X-API-PIN` hilft in diesem Fall nicht, weil der Wert aus `Authorization: Bearer` Vorrang hat.

### Empfehlung zur PIN
Verwende eine PIN mit mindestens 6 Zeichen; Buchstaben und Ziffern sind erlaubt. Eine kurze, rein numerische PIN ist deutlich leichter zu erraten.

---

## 2. API-Endpunkte im Detail

### 2.1. Live-Daten-Schnittstelle (`get_live_json.php`)
Liefert den kompletten, aktuellen Betriebszustand deines EMS-Systems aus der schnellen Ramdisk.

*   **URL:** `https://dein-tunnel.de/get_live_json.php`
*   **Methode:** `GET`
*   **Wichtige Felder im JSON-Response:**

| JSON-Key | Datentyp | Beschreibung | Einheit |
| :--- | :--- | :--- | :--- |
| `soc` | Float | Aktueller Ladestand (SoC) der Batterie | % |
| `pv` | Float | Aktuelle Solarstrom-Erzeugung (PV) | Watt |
| `bat` | Float | Batterieleistung (positiv = Laden, negativ = Entladen) | Watt |
| `grid` | Float | Netzleistung (positiv = Netzbezug, negativ = Einspeisung) | Watt |
| `home` | Float | Bereinigter Hausverbrauch (gefiltert & stabilisiert) | Watt |
| `wb` | Float oder `null` | Aktuelle Ladeleistung der Wallbox 1 | Watt |
| `wb2` | Float oder `null` | Aktuelle Ladeleistung der Wallbox 2 | Watt |
| `wp` | Float | Aktuelle Leistungsaufnahme der Wärmepumpe | Watt |
| `hs_power` | Float | Aktuelle Leistung des Heizstabs | Watt |
| `price_ct` | Float | Aktueller Börsenstrompreis (Brutto, inkl. Steuern & Gebühren) | Cent/kWh |
| `notstrom_reserve` | Float | Konfigurierte Notstromreserve | % |
| `storage_state` | String | Aktueller Regelungszustand des Storage Managers (z.B. `pre_discharge_wait`) | - |

Bei nativer Wallboxregelung sind fehlende, ungültige oder veraltete Messwerte `null`; ein gemessenes `0` bleibt eine echte Nullmessung. `wb_observation` beziehungsweise `wb2_observation` liefern `valid`, `reason`, `sample_ts`, `age_s` und `missing_fields`; der Grund steht zusätzlich in `wb_status_reason` beziehungsweise `wb2_status_reason`. Eine gültige Teilmessung kann einzelne `null`-Kanäle enthalten. `wb_phases`/`wb2_phases` und ihre Ist-Aliase bevorzugen vollständige aktuelle Stromkanäle; native RSCP-Leser nutzen ihre belegten Leistungskanäle. Teilweise oder ungültige Kanalantworten bleiben unbekannt. Nur ohne Kanalantwort darf eine gültige Phasenmeldung der Box einspringen, niemals eine Anschluss- oder Zielphasenzahl. `wb_phases_source`/`wb2_phases_source`, `active_wb_phases_source` und `phases_source` in den Beobachtungsmetadaten kennzeichnen `current_channels`, `power_channels`, `reported` oder `unknown`; die Oberfläche markiert `reported` als „gemeldet“. `active_wb_phases` gehört zu `active_wb_id`; ohne gültige Phaseninformation dieses Ladepunkts ist der Wert `null`. `detected_phases` bleibt der Ist-Alias für Wallbox 1 und ist ohne deren gültige Phaseninformation `null`. `wallbox_phases_reason` nennt den gemeinsamen Gültigkeitsgrund.

`get_live_json.php?wallbox_native_snapshot=1` ist der rohe Diagnosevertrag des Wallbox-Managers. Er enthält auch Regler-, EVSE- und Zielwerte, die keine physische Istmessung belegen. Für Messanzeigen daraus ausschließlich die frischen, gültigen `wb_details[].observation.values` samt Zeitstempel und Fehlgründen verwenden; insbesondere dürfen `phases_target` oder der EVSE-Phasenstatus eine fehlende Messung nicht ersetzen.

*   **Beispiel-Response (Auszug):**
```json
{
  "soc": 68.5,
  "pv": 4250,
  "bat": 2500,
  "grid": -1750,
  "home": 500,
  "wb": 0,
  "wp": 0,
  "price_ct": 24.85,
  "storage_state": "normal_charge"
}
```

---

### 2.2. Prognose- und Strompreis-Schnittstelle (`get_forecast_data.php`)
Liefert die PV-Ertragsprognosen der nächsten Tage (Forecast.Solar, Open-Meteo, Solcast) sowie den EPEX/Awattar-Strompreisverlauf für die Lade- und Heizplanung.

*   **URL:** `https://dein-tunnel.de/get_forecast_data.php`
*   **Methode:** `GET`
*   **Wichtige Felder im JSON-Response:**
    *   `prices`: Array aus 24 Fließkommawerten (Börsenpreise von heute 00:00 Uhr bis 23:00 Uhr in Cent/kWh).
    *   `tomorrow_prices`: Array aus 24 Werten für den Folgetag (verfügbar ab ca. 13:30 Uhr).
    *   `pv_forecast`: Stundenweise prognostizierte PV-Leistung.
    *   `plan_timeline`: Die vom System berechnete ideale Batterie-Ladekurve.

---

### 2.3. Historien-Schnittstelle (`get_chart_data.php`)
Liefert aggregierte historische Energiewerte zur Visualisierung des Tages- oder Monatsverlaufs.

*   **URL:** `https://dein-tunnel.de/get_chart_data.php?days=1`
*   **Methode:** `GET`
*   **Parameter:** `days` (Anzahl der historischen Tage, Standard ist 1).
*   **Response:** Ein Array aus Zeitstempeln und den zugehörigen Leistungen für eine reibungslose clientseitige Rendering-Engine (z.B. ApexCharts).

---

## 3. CORS Preflight & Browser-Kompatibilität

Moderne Web-App-Frameworks (React, Vue, Angular) senden vor einer HTTP-Anfrage mit Custom-Headern eine `OPTIONS`-Preflight-Anfrage (CORS). 

Deine API beantwortet solche `OPTIONS`-Anfragen mit HTTP `403` und gibt keine CORS-Freigabe zurück. Browser-Apps, die von einer anderen Adresse (fremdem Origin) aus auf die API zugreifen, werden damit bewusst abgewiesen. Die Web-PIN ist keine Freigabeliste für fremde Webseiten.

Widgets, Apps, Home-Assistant-REST-Sensoren und Skripte, die ihre Anfrage selbst senden, sind davon nicht betroffen: Sie senden keinen Preflight und authentifizieren sich wie in Abschnitt 1 beschrieben.
