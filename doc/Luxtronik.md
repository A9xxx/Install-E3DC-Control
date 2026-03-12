# Luxtronik Energy Manager für E3DC-Control

Dieses Modul erweitert **E3DC-Control** um eine intelligente Steuerung für Wärmepumpen mit **Luxtronik 2.0 / 2.1** Regler (z.B. Alpha Innotec, Novelan). Es nutzt überschüssigen PV-Strom, um Warmwasser oder Heizung gezielt anzuheben ("Boost") und so Energie thermisch zu speichern.

---

## 1. Funktionen

*   **PV-Überschuss-Steuerung:** Aktiviert den Boost-Modus der Wärmepumpe, wenn genug Strom ins Netz eingespeist wird.
*   **Batterie-Schutz:** Berücksichtigt den Ladestand (SoC) des E3DC-Hauskraftwerks, um die Batterie nicht leerzuziehen.
*   **Web-Interface:** Integration in das E3DC-Webportal mit Live-Status, COP-Berechnung und manueller Steuerung.
*   **Smart-Home-Schnittstelle:** Nutzt Modbus TCP zur Kommunikation mit der Wärmepumpe.
*   **System-Integration:** Vollständig in den E3DC-Control Installer, Status-Check und Rechte-Management integriert.

---

## 2. Voraussetzungen

*   **Wärmepumpe:** Luxtronik 2.0 oder 2.1 Steuerung.
*   **Netzwerk:** Die Wärmepumpe muss per LAN im selben Netzwerk wie der Raspberry Pi erreichbar sein.
*   **Modbus:** Das Modbus-Protokoll muss an der Wärmepumpe freigeschaltet sein (Standard-Port 502).
*   **E3DC-Control:** Eine funktionierende Installation von E3DC-Control.

---

## 3. Installation

Die Installation erfolgt bequem über den zentralen Installer.

1.  Starte den Installer auf dem Raspberry Pi:
    ```bash
    cd ~/Install
    sudo python3 installer_main.py
    ```

2.  Wähle im Hauptmenü unter **Erweiterungen** den Punkt:
    *   **101** – Luxtronik Manager installieren/konfigurieren

3.  Der Assistent führt dich durch die Einrichtung:
    *   Installation der Python-Abhängigkeiten (`luxtronik`, `requests`).
    *   Einrichtung des Systemdienstes (`energy_manager`).
    *   Abfrage der Konfigurationswerte (IP-Adresse, Grenzwerte).

---

## 4. Konfiguration

Die Konfiguration wurde zentralisiert und befindet sich nun in der Datei **`e3dc.config.txt`**. 
Alte Konfigurationsdateien (`config.lux.json`) werden beim Update automatisch migriert.

Die Bearbeitung erfolgt am einfachsten über das **Web-Interface** (Config Editor).

**Pfad:** `~/E3DC-Control/e3dc.config.txt` (bzw. im Installationsverzeichnis)

### Wichtige Parameter (in e3dc.config.txt)

| Parameter | Beschreibung | Standard |
| :--- | :--- | :--- |
| `luxtronik_ip` | IP-Adresse der Wärmepumpe im lokalen Netzwerk. | `192.168.178.88` |
| `GRID_START_LIMIT` | Einspeiseleistung in Watt, ab der der Boost startet. **Negativ** bedeutet Einspeisung. | `-3500` |
| `MIN_SOC` | Mindest-Ladestand der Hausbatterie in %, damit der Boost freigegeben wird. | `65` |
| `AT_LIMIT` | Außentemperatur-Grenze in °C. Unterscheidet zwischen Sommer- (nur WW) und Winterbetrieb. | `14.0` |
| `WWS` | Warmwasser-Sollwert im Boost-Modus (Sommer/Übergang). | `55.0` |
| `WWW` | Warmwasser-Sollwert im Winterbetrieb. | `45.0` |
| `HZ` | Heizungsanhebung (Rücklauf-Soll) in Kelvin (°C) während des Boosts. | `50.0` |

---

## 5. Funktionsweise

### Der Hintergrunddienst (`energy_manager.py`)
Das Skript läuft als Systemd-Service (`energy_manager`) im Hintergrund.

1.  **Zyklus:** Alle 30 Sekunden werden Daten von der Wärmepumpe (via Modbus) und vom E3DC-System (via `localhost/get_live_json.php`) abgerufen.
2.  **Entscheidung:**
    *   Ist die Einspeisung höher als `GRID_START_LIMIT` (z.B. mehr als 3500W ins Netz)?
    *   UND ist die Batterie voller als `MIN_SOC`?
    *   -> **Boost AN** (Warmwasser-Soll wird erhöht, ggf. Heizung angehoben).
3.  **Abschaltung:**
    *   Wird Strom aus dem Netz bezogen (> 50W) oder die Batterie entladen?
    *   -> Ein Timer startet. Nach 10 Minuten Defizit wird der **Boost AUS** geschaltet (Reset auf Normalwerte).

### Das Web-Interface (`luxtronik.php`)
Die PHP-Datei visualisiert die Daten:
*   **Live-Werte:** Temperaturen (Vorlauf, Rücklauf, WW, Außen), Leistung, COP (Wirkungsgrad).
*   **Status:** Zeigt an, ob Verdichter, Heizstab oder Pumpen laufen.
*   **Steuerung:** Ermöglicht das manuelle Starten eines "Notfall-Boosts" (z.B. um die Batterie vor dem Abend schnell zu leeren).

---

## 6. Dateistruktur

Die Dateien befinden sich im Ordner `~/Install/Installer/luxtronik/`:

*   `energy_manager.py`: Das Haupt-Steuerungsskript (Python).
*   `luxtronik.py`: Hilfsdatei für die Modbus-Kommunikation.
*   `set_manual_boost.py`: Skript für manuelle Web-Befehle.

Temporäre Daten (für das Web-Interface) liegen in der RAM-Disk:
*   `/var/www/html/ramdisk/luxtronik.json`: Aktueller Status (JSON).
*   `/var/www/html/ramdisk/manual_boost.flag`: Marker für manuellen Boost.

---

## 7. Troubleshooting

### Dienst läuft nicht?
Prüfe den Status über den Installer (Menüpunkt 21) oder direkt:
```bash
sudo systemctl status energy_manager
```

### Fehler im Log?
Zeige die letzten Log-Meldungen an:
```bash
journalctl -u energy_manager -e
```
Häufige Fehler sind falsche IP-Adressen oder nicht erreichbare Modbus-Schnittstellen.