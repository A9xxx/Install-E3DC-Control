# IDM Wärmepumpen-Integration für E3DC-Control

Dieses Modul ermöglicht die Anbindung von **IDM-Wärmepumpen** (z.B. AERO, TERRA mit Navigator 1.7 oder 2.0 Regler) an das E3DC-Control Web-Portal. Die Kommunikation erfolgt über **Modbus-TCP**.

---

## 1. Funktionen

*   **Live-Monitoring:** Anzeige von Außentemperatur (inkl. Mitteltemperatur), Verdichterfrequenz (Hz), Vorlauf, Rücklauf-Soll, Warmwasser (Soll/Ist), Heizleistung und aktuellem Betriebszustand im Dashboard.
*   **Interaktive native Steuerung:** Volle native Unterstützung für **Modbus-Register 74 (PV-Überschuss)**. Der `energy_manager.py` teilt der Wärmepumpe vollautomatisch und ohne Umwege alle 10 Sekunden den stufenlosen PV-Überschuss des Hauskraftwerks mit, um die Eigenverbrauchsquote ohne Cloud/MQTT zu maximieren.
*   **Intelligentes Kühlen (Sommer):** Vollautomatische Aktivierung der IDM-Kühlung (Register `1711` & `1010`) bei hohen Außentemperaturen (>Heizgrenze) inklusive Boost-Modus für Smart Grid (`1006=2`).
*   **COP-Berechnung:** Automatische Berechnung des aktuellen Wirkungsgrades (Arbeitszahl) basierend auf thermischer Leistung und elektrischer Aufnahme.
*   **Historisierung:** Erfassung der Wärmepumpen-Daten in der SQLite-Datenbank für Langzeit-Statistiken inkl. präziser **Tages-Arbeitszahl**.
*   **MQTT-Bridge:** Nahtlose Integration in Smart-Home-Systeme via MQTT Hub.
*   **UI-Übersetzung:** Rohe Betriebszustände der IDM (1, 2, 3...) werden im Dashboard automatisch in verständliche Betriebsarten (Heizen, Kühlen, Warmwasser, Abtauen) übersetzt.

---

## 2. Voraussetzungen

1.  **Gebäudeleittechnik (GLT):** Die Modbus-TCP Schnittstelle muss in der Navigator-Regelung unter dem Menüpunkt „Gebäudeleittechnik“ freigeschaltet sein (erfordert ggf. den Fachmann-Code).
2.  **Netzwerk:** Die Wärmepumpe muss im selben LAN wie der Raspberry Pi erreichbar sein.
3.  **Port 502:** Der Standard-Modbus-Port 502 muss offen sein.

---

## 3. Installation

Die Einrichtung erfolgt über Web-Config und Installationszentrale:

1. Öffne den **Config Editor**.
2. Aktiviere **WP-/Verbrauchslogging** und wähle als Wärmepumpen-Typ **IDM**.
3. Trage die lokale IDM-IP ein und speichere die Konfiguration.
4. Öffne die **Installationszentrale** und installiere beziehungsweise starte
   `e3dc-idm-live` und den gemeinsamen Wärmepumpen-Manager `energy_manager`.
   Die benötigte Bibliothek `pymodbus` wird dabei vorbereitet.

---

## 4. Konfiguration

Die Einstellungen werden in `data/e3dc_v4.json` gespeichert und bequem über den **Config Editor** im Web-Portal angepasst. Eine alte `e3dc.config.txt` wird nur noch bei der Migration als Fallback gelesen:

| Parameter | Beschreibung |
| :--- | :--- |
| `wp_type` | Muss auf `1` stehen (IDM Modbus). |
| `idm_ip` | Die lokale IP-Adresse deiner IDM Wärmepumpe. |
| `khl` | Kühlung Soll in Grad C. Wird als Software-Grenze genutzt, nicht zyklisch als EEPROM-Temperatur geschrieben. |

---

## 5. Technik & Datenfluss

### Der Live-Dienst (`idm_live.py`)
Dieser Dienst fungiert als Modbus-Client und fragt die IDM alle **5 Sekunden** ab.
*   **Word-Swap:** Da IDM Daten im Big-Endian Format mit vertauschten Words (CD AB) sendet, führt das Skript automatisch ein Word-Swapping durch, um korrekte IEEE 754 Float-Werte zu erhalten.
*   **Daten-Übergabe:** Die Werte werden in `/var/www/html/ramdisk/waermepumpe.json` geschrieben.

### Die native Überschusssteuerung (`energy_manager.py`)
Die Überschusssteuerung erfolgt nativ über den Energy Manager:
*   **Register 74 (Überschuss):** Erkennt das Hauskraftwerk eine Einspeisung ins Netz (Überschuss), so übermittelt der `energy_manager.py` diesen Wert (z.B. `-2.5 kW`) sofort per ModbusTCP direkt an die Wärmepumpe. Die IDM regelt stufenlos hoch und schmiegt sich an den Überschuss an. Um den Modbus nicht zu fluten, ist ein 10-Sekunden-Ratenlimit implementiert.
*   **Kühl-Intervention (Register 1711):** Im Sommer-Betrieb (Außentemperatur > Heizgrenze) verlagert sich die Überschusssteuerung. Ist PV-Leistung vorhanden und der manuelle "Boost" wird betätigt, schreibt die Steuerung automatisch auf Register `1711`. Gleichzeitig wird das Smart Grid Register `1006` auf "2" gesetzt, um der Maschine die Freigabe für Vollgas-Kühlung zu erteilen.


### Ruhige PV-Grundlast mit Leistungsobergrenze

Für Anlagen mit starker PV kann die IDM morgens bei ausreichendem Überschuss als ruhige Grundlast laufen. E3DC-Control sendet dabei keine zyklischen Temperatur-Sollwerte, sondern Leistungs-/Überschusswerte und externe Anforderungen über Modbus. Eine konfigurierbare Obergrenze begrenzt die Leistungsanforderung, z.B. auf 2 kW, damit die WP nicht nervös zwischen Aus und Vollgas pendelt.

Die Schutzlogik besteht aus:

- Mindestlaufzeit und Nachlaufzeit gegen Takten,
- Hysterese an den Temperaturgrenzen,
- Keep-alive nur für aktive externe Anforderungen,
- keine zyklischen Schreibzugriffe auf EEPROM-relevante Temperaturparameter.
---

## 6. Boost-Steuerung & Software-Thermostat

> **Wichtiger Architektur-Hinweis:** Die IDM verhält sich anders als die Luxtronik!

### Das Problem: Externe Register überschreiben den internen Setpoint

Wenn in den IDM-Modbus-Registern eine externe Anforderung gesetzt wird, **ignoriert die IDM ihren intern konfigurierten Temperatur-Setpoint** und heizt/kühlt ohne eigene Begrenzung:

| Register | Funktion | Wert `1` = |
| :--- | :--- | :--- |
| `1710` | Externe Heizanforderung | IDM heizt, ignoriert internen HZ-Setpoint |
| `1711` | Externe Kühlanforderung | IDM kühlt, ignoriert internen KHL-Setpoint |
| `1712` | Externe WW-Anforderung | IDM heizt WW, ignoriert internen WW-Setpoint |
| `1006` | Smart Grid | `0`=Sperre, `1`=Normal, `2`=PV-Vollgas |

Die in `e3dc_v4.json` konfigurierten Grenzwerte (`wws`, `www`, `hz`, `khl`) kennt die IDM **nicht** als eigene interne Sollwerte. E3DC-Control nutzt sie als Software-Thermostat mit Hysterese und Nachlaufzeit.

### Die zwei Steuerungs-Modi

**`set_boost()` — PV-Überschuss-Automatik (normaler Betrieb)**

Prüft die Ist-Temperaturen aus dem IDM-Modbus und aktiviert die externe Anforderung nur mit Hysterese:
- Aktivierung: `Ist-Temp < Soll - 2°C`
- Deaktivierung: `Ist-Temp >= Soll`

> Der 2°C-Puffer verhindert häufiges Ein-/Ausschalten.

**`force_boost(ww_max, hz_max)` — Manueller Boost (Dashboard-Button)**

Startet sofort ohne Hysterese-Verzögerung, schützt aber trotzdem vor Überheizung:
- Aktivierung: **sofort** wenn `Ist-Temp < Soll` (kein 2°C-Puffer)
- Liest aktuelle Temperaturen aus Modbus-Registern `1030` (WW) und `1008` (HZ)
- Schreibt Register nur auf `1`, wenn Temperatur **unter** dem konfigurierten Limit liegt

```
Beispiel: WW-Ist = 57°C, www = 48°C
→ force_boost prüft: 57 >= 48 → WW-Register 1712 bleibt auf 0
→ Wärmepumpe heizt kein Warmwasser (schon zu warm)
```

### Sommer- vs. Winter-Modus

Der Modus wird anhand der **10-Minuten-Mitteltemperatur** aus `waermepumpe.json` bestimmt und mit dem konfigurierten `heizgrenze_temp`-Wert verglichen:

| Bedingung | Modus | Aktive Register |
| :--- | :--- | :--- |
| Mitteltemp > Heizgrenze | **Sommer** | WW (`1712`) + optional Kühlung (`1711`) |
| Mitteltemp <= Heizgrenze | **Winter** | Heizung (`1710`) + WW (`1712`) |

> **Sicherheits-Fallback:** Kann die Außentemperatur nicht aus `waermepumpe.json` gelesen werden, wird immer **Winter-Modus** angenommen (Default `0.0°C`). Damit wird eine versehentliche Kühlung im Winter bei nicht erreichbarer JSON-Datei verhindert.

---

## 7. Troubleshooting

### Keine Daten im Dashboard?
1. Prüfe, ob der Dienst läuft:
   ```bash
   sudo systemctl status e3dc-idm-live
   ```
2. Prüfe auf Modbus-Fehler im Log:
   ```bash
   journalctl -u e3dc-idm-live -n 50
   ```
3. Teste die Erreichbarkeit der IDM:
   ```bash
   ping <DEINE_IDM_IP>
   ```

### Falsche Werte?
Stelle sicher, dass in der IDM-Regelung Modbus TCP aktiviert ist und die Liste der Standard-Register (Navigator 2.0) verwendet wird.
