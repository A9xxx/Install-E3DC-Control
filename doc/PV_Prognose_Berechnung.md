# PV-Wetterprognose — Technische Dokumentation

> **Dienst:** `e3dc-weather-manager.service` · Intervall: 60 Min (Tag), 120 Min (Nacht)
> **Ausgabe:** `/var/www/html/ramdisk/pv_forecast.json` (72h-Horizont, 15-Min-Slots)

---

## 1. Überblick — Was passiert in einem Zyklus?

```
EnsemblePVForecaster.generate_ensemble()

  [1] API-Abruf (je nach TTL-Cache)
      M1: Forecast.Solar  (stundenweise, geometrische Dachberechnung)
      M2: Open-Meteo      (stundenweise, NWP-Ensemble)
      M3: Solcast         (alle 4h, Premium-Satelliten-ML)
                 |
  [2] Dynamische Gewichtung pro Stunden-Slot
      Phase A: Horizont-Faktoren (0-24h / 24-48h / 48-72h)
      Phase B: Wetterklassen-Faktoren (Klar / Misch / Bedeckt)
                 |
  [3] Interpolation: 1h-Werte -> 15-Min-Slots
                 |
  [4] EWMA-Bias-Korrektur (jahreszeitlicher Selbstlernfaktor)
                 |
  [5] Speichern -> pv_forecast.json (Ramdisk)
      + History-Buffer (24h Rolling, für Chart-Overlay)
      + weather_forecast.json (Temperatur für ML)
```

---

## 2. Konfiguration (Standort & Dachdaten)

Die Prognose-Güte hängt direkt von der korrekten Konfiguration ab:

| Config-Key | Inhalt | Beispiel |
|---|---|---|
| `hoehe` | Breitengrad | `51.16` |
| `laenge` | Laengengrad | `10.45` |
| `forecast1` | `Neigung/Azimuth/kWp` | `35/0/10.0` |
| `forecast2` | Zweite Dachflaeche (optional) | `30/90/5.2` |
| `forecast3` | Dritte Dachflaeche (optional) | `25/0/3.0` |

**Azimut-Konvention** (identisch für alle Modelle):
`0` = Süd · `-90` = Ost · `+90` = West · `180` = Nord

> [!TIP]
> Die Gesamt-kWp wird automatisch mit dem RSCP-Wert `installed_peak_power_w` aus der E3DC abgeglichen — so bleibt die Prognose auch nach Anlagenerweiterungen korrekt.

---

## 3. Die drei Datenquellen (Modelle)

### M1 — Forecast.Solar

**Was:** Geometrische PV-Berechnung, community-betrieben
**TTL:** 60 Minuten · **Horizont:** 2 Tage · **Kosten:** Kostenlos

```
Für jedes konfigurierte Dach:
  URL = api.forecast.solar/{lat}/{lon}/{tilt}/{azimuth}/{kwp}
  -> Antwort: {watts: {"2026-04-27 08:00:00": 3500, ...}}
  -> kWh/h = watts / 1000
  -> Dachdaten werden summiert
```

**Stärke:** Präzise Dachgeometrie, zuverlässig bei klarem Himmel
**Schwäche:** Kennt keine Wolken (rein geometrisch/astronomisch)

---

### M2 — Open-Meteo (Ensemble)

**Was:** Numerische Wettervorhersage (NWP) mit 3 internen Modellen
**TTL:** 60 Minuten · **Horizont:** 4 Tage · **Kosten:** Kostenlos, unlimitiert

```
Interne Modelle (pro Dachflaeche):
  - ICON-D2   (Deutscher Wetterdienst): Mitteleuropa, hohe Auflösung, bis 48h
  - ECMWF IFS (Europäisches Zentrum):   global, bis 7 Tage
  - best_match (Open-Meteo-Ensemble):   automatisch bestes Modell

Internes Blending pro Slot:
  if ICON + ECMWF verfuegbar:
    blended = ICON x 0.5 + ECMWF x 0.3 + best_match x 0.2
  elif nur ECMWF:
    blended = ECMWF x 0.6 + best_match x 0.4
  else:
    blended = best_match

Umrechnung (Global Tilted Irradiance -> kWh):
  est_kwh = (GTI_Wm2 / 1000) x kWp x 0.85   (Performance Ratio 85%)
```

**Stärke:** Wolken, Temperatur, diffuse Strahlung — das echte Wetter
**Schwäche:** Geometrie weniger präzise als M1, ICON-D2 nur bis 48h

---

### M3 — Solcast *(optional)*

**Was:** Satelliten-ML-Prognose, speziell für PV-Anlagen
**TTL:** 4 Stunden (nur 10 kostenlose Calls/Tag im Free Tier!)
**Horizont:** ~24-36h (Free Tier) · **Kosten:** Kostenlos bis 10 Calls/Tag

```
URL = api.solcast.com.au/rooftop_sites/{resource_id}/forecasts
-> Liefert 30-Min-Slots in kW (pv_estimate)
-> Umrechnung: kW x 0.5h = kWh pro 30-Min-Slot
-> Slots werden auf stuendliche Bins aufaddiert
```

**Stärke:** Satellitenbilder + historische Anlagen-Kalibrierung = sehr präzise
**Schwäche:** Nur 10 Calls/Tag frei, kurzer Horizont

---

## 4. Dynamische Gewichtung (Kern des Systems)

### Phase A — Horizont-abhängig

```
        M1-Gewicht  M2-Gewicht  M3-Gewicht
  0h:     x0.60      x1.40      x1.00   <- M2 (NWP) dominiert kurzfristig
 24h:     x0.60      x1.40      x1.00
         -------- Uebergangsbereich --------
 36h:     x0.85      x1.20      x0.50   <- M3 fällt linear auf 0
 48h:     x1.10      x1.00        0     <- M1 gewinnt (Geometrie dauerhaft korrekt)
 72h:     x1.30      x0.80        0     <- NWP verliert Präzision
```

**Wissenschaftliche Basis:** IEA Task 16 / WMO-Empfehlungen:
Kurzfristig ist das NWP-Ensemble am genauesten (aktuelle Wetterlage).
Langfristig ist die geometrische Berechnung stabiler (NWP divergiert).

---

### Phase B — Wetterklassen-abhängig

Nach Phase A wird das Gewicht nochmals angepasst:

| M2-GTI | Wettertyp | M1-Faktor | M2-Faktor | Begruendung |
|---|---|---|---|---|
| >= 0.30 kWh/h | Klar | x1.25 | x0.75 | Geometrie (M1) dominiert bei klarem Himmel |
| 0.08-0.30 kWh/h | Mischbewoelkt | x1.00 | x1.00 | Neutral |
| < 0.08 kWh/h | Bedeckt | x0.70 | x1.30 | NWP (M2) besser für Diffusstrahlung |

Nach beiden Phasen werden alle Gewichte auf Summe 1.0 renormiert.

```python
# Finaler Ensemble-Wert pro Stunden-Slot:
final_kwh = val1 * norm_w1 + val2 * norm_w2 + val3 * norm_w3
```

---

## 5. Interpolation: Stunde -> 15-Minuten-Slots

Stuendliche Ensemble-Werte werden mit gewichtetem Nachbar-Blending zerlegt:

```python
Fuer jede Stunde [prev_kw, curr_kw, next_kw]:
  Slot q=0  (HH:00): prev x 0.25 + curr x 0.75   <- "Einfahrt" (sanfter Start)
  Slot q=1  (HH:15): prev x 0.10 + curr x 0.90
  Slot q=2  (HH:30): next x 0.10 + curr x 0.90
  Slot q=3  (HH:45): next x 0.25 + curr x 0.75   <- "Ausfahrt" (Uebergang)
```

Verhindert harte Stufen zwischen Stunden, erzeugt eine glatte Rampe.

---

## 6. EWMA-Selbstlern-Bias-Korrektur

Das System lernt taeglich aus dem Vergleich **Prognose vs. Ist-Ertrag**.

### Ablauf (taeglich, max. 1x/h geprueft)

```
[1] Gestriger Ist-Ertrag    <- e3dc_stats.db (daily_stats.pv_yield)
[2] Gestrige Prognose-Summe <- pv_forecast_history.json (24h Buffer)
[3] bias_raw = Ist / Prognose

[4] Klassifizierung:
      bias > 1.10  -> "sunny"  (Modell hat unterschaetzt)
      bias >= 0.80 -> "mixed"
      bias < 0.80  -> "cloudy" -> KEIN Update! (Wolken = Zufall)

[5] EWMA-Update (nur sunny/mixed):
      alpha = 0.15  (träge: 15% neuer Tag, 85% bisheriger Schnitt)
      new_bias = 0.15 x bias_raw_clamped + 0.85 x old_bias

[6] Pro Jahreszeit gespeichert:
      Q1 = Winter (Jan-März)
      Q2 = Frühling (Apr-Jun)
      Q3 = Sommer (Jul-Sep)
      Q4 = Herbst (Okt-Dez)
```

### Anwendung auf den Forecast

```python
bias_safe = max(0.75, min(1.40, seasonal_bias[quarter])
#              max -25%        max +40%

for slot where predicted_kwh > 0.01:
    slot['predicted_kwh'] *= bias_safe

# Mindestens 7 Tage Daten noetig, sonst kein Bias
```

**Beispiel:** System prognostiziert im Frühling systematisch 15% zu wenig.
Nach 14 Tagen: `Q2_bias = 1.15` -> alle Slots x 1.15 korrigiert.

## 6.1 Plausibilitätskappen für PV-Spitzen

Solcast und andere kurzfristige Modelle können bei wechselnder Bewölkung sehr
hohe Momentanpeaks melden. Solche Cloud-Edge-Spitzen sind physikalisch möglich,
wenn dunklere Wolken und helle Wolkenränder die Einstrahlung kurzfristig
verstärken. Für die Speicherplanung sind aber breite, stundenlange Peaks oberhalb
der Anlagenleistung gefährlich, weil sie die Ladekurve zu optimistisch machen.

Deshalb gilt für die Weiterverarbeitung:

- kurze, schmale Spitzen dürfen oberhalb der Nennleistung sichtbar bleiben,
  solange sie zeitlich plausibel sind,
- breite Plateaus werden auf Anlagenleistung plus Sicherheitsreserve begrenzt,
- die Wechselrichterleistung bleibt eine eigene harte Grenze für
  Abregelschutz und Speicherplanung,
- die Bias-Korrektur darf eine Tagesprognose verbessern, aber keine dauerhaft
  unrealistischen Peaks erzwingen.

Damit bleiben reflektionsbedingte PV-Spitzen erhalten, während die Ladekurve
nicht auf mehrere Stunden "Wunderleistung" optimiert.

---

## 7. Ausgabe-Dateien

| Datei | Inhalt | Konsument |
|---|---|---|
| `pv_forecast.json` | 72h Ensemble-Prognose, 15-Min-Slots | `storage_simulator.py`, Dashboard |
| `pv_forecast_history.json` | 24h Rolling Buffer vergangener Slots | PHP Chart "Prognose vs. Real" |
| `pv_forecast_meta.json` | Metadaten + Bias-Status | Dashboard Diagnose |
| `weather_forecast.json` | Temperatur + Strahlung stuendlich | `ml_predictor.py` |
| `pv_forecast_eval.json` | EWMA-Bias-History, MAE-Log (90 Tage) | Selbstlern-System |
| `forecast_model_cache.json` | API-Cache (ueberlebt Neustarts) | TTL-System |

---

## 8. Abruf-Intervalle & Timing

```
Haupt-Loop:
  Tagzeit (07-21h): alle 60 Minuten
  Nacht   (21-07h): alle 120 Minuten

Adaptiver Wachhhund:
  Alle 60s: Ist pv_forecast.json aelter als 3h? -> Sofort neu abrufen!

Pro Modell (TTL-gesteuert):
  M1 Forecast.Solar: 60 Min  -> max 24 Calls/Tag
  M2 Open-Meteo:    60 Min  -> max 24 Calls/Tag (kostenlos, unlimitiert)
  M3 Solcast:       4h      -> max 6 Calls/Tag (Free Tier: 10/Tag)

Cache ueberlebt Neustarts:
  forecast_model_cache.json -> kein API-Hammer nach "systemctl restart"!
```

---

## 9. Vollstaendiger Datenfluss

```
Internet
  |- Forecast.Solar API  ---------------------------------+
  |- Open-Meteo API (ICON + ECMWF + best_match) ---------+-- EnsemblePVForecaster
  `- Solcast API (optional) -----------------------------+

                              |
                              v
              Dynamisches Gewichten (Phase A + B)
                              |
                              v
              15-Min-Interpolation + EWMA-Bias-Korrektur
                              |
                              v
                   pv_forecast.json (Ramdisk)
                              |
            +-----------------+-----------------+
            v                 v                 v
 storage_simulator.py    Dashboard-Chart   ml_predictor.py
 (Ladekurve berechnen)  (PV-Prognose-Linie) (Verbrauchsprognose)
            |
            v
 storage_plan.json -> storage_manager.py (Regelung)
```

---

## 10. Was beeinflusst die Prognose-Genauigkeit?

| Faktor | Einfluss | Verbesserung |
|---|---|---|
| Falsche Dach-Konfiguration (Azimuth/Neigung/kWp) | Sehr hoch | Config korrekt eintragen |
| Kein Solcast-Key | Mittel | Free-Tier Key anlegen (solcast.com.au) |
| System laeuft < 7 Tage | Mittel | EWMA-Bias noch inaktiv, waechst von selbst |
| Falsche Koordinaten | Hoch | Breitengrad/Laengengrad pruefen |
| Neue Dachflaeche nicht eingetragen | Hoch | forecast2/3 ergaenzen |
| Jahreszeitenwechsel | Gering | EWMA trennt Q1-Q4 automatisch |
