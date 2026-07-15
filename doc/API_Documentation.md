# E3DC-Control V4: API-Dokumentation für externe Frontends & Widgets

Diese Dokumentation beschreibt die verfügbaren API-Endpunkte deiner E3DC-Control-Installation zur Abfrage von Live-Daten, Prognosen und historischen Verläufen. Alle Endpunkte sind über deinen Cloudflare-Tunnel erreichbar und für Cross-Origin-Anfragen (CORS) freigegeben, sofern sie korrekt authentifiziert sind.

---

## 1. Authentifizierung & Sicherheit

Alle Anfragen an die API-Endpunkte müssen authentifiziert werden, wenn eine `web_pin` in deiner Konfiguration hinterlegt ist. Es stehen drei gleichwertige Übertragungswege für den API-Schlüssel zur Verfügung:

### Option A: HTTP Bearer Token (Empfohlen für modernste Apps)
Sende den API-Schlüssel im standardisierten `Authorization`-Header der HTTP-Anfrage:
```http
Authorization: Bearer <DEINE_WEB_PIN>
```

### Option B: Custom HTTP Header (Optimiert für einfache Smart-Charging-Widgets)
Sende den API-Schlüssel in einem dedizierten Header-Feld:
```http
X-API-PIN: <DEINE_WEB_PIN>
```

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
| `wb` | Float | Aktuelle Ladeleistung der Wallbox 1 | Watt |
| `wb2` | Float | Aktuelle Ladeleistung der Wallbox 2 | Watt |
| `wp` | Float | Aktuelle Leistungsaufnahme der Wärmepumpe | Watt |
| `hs_power` | Float | Aktuelle Leistung des Heizstabs | Watt |
| `price_ct` | Float | Aktueller Börsenstrompreis (Brutto, inkl. Steuern & Gebühren) | Cent/kWh |
| `notstrom_reserve` | Float | Konfigurierte Notstromreserve | % |
| `storage_state` | String | Aktueller Regelungszustand des Storage Managers (z.B. `pre_discharge_wait`) | - |

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

Deine API fängt diese `OPTIONS`-Anfragen vollautomatisch an der Quelle ab, sendet die zulässigen Header zurück und antwortet sofort mit HTTP `200 OK`, sodass der Browser die eigentliche `GET`- oder `POST`-Anfrage ohne Sicherheitsverzögerung absetzen kann.
