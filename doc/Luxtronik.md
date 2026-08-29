# Wärmepumpen-Integration für E3DC-Control

> [!NOTE]
> Dieses System unterstützt mehrere native Integrationen:
> 1. **Luxtronik 2.0 / 2.1** (Diese Datei)
> 2. **[IDM Wärmepumpen (Modbus-TCP)](IDM_Integration.md)**
> 3. **[Stiebel Eltron ISG / WPM (read-only Live-Daten)](Stiebel_Eltron_ISG.md)**

Dieses Modul erweitert **E3DC-Control** um eine intelligente Steuerung für Wärmepumpen mit **Luxtronik 2.0 / 2.1** Regler (z.B. Alpha Innotec, Novelan). Es nutzt freigegebenes Budget aus Storage Manager, Pre-Dump oder Preislogik, um Warmwasser oder Heizung gezielt anzuheben ("Boost") und so Energie thermisch zu speichern.

---

## 1. Funktionen

*   **Budgetgeführte PV-/Preis-Steuerung:** Aktiviert den Boost-Modus der Wärmepumpe, wenn der Storage Manager, Pre-Dump oder ein explizites Preisfenster genügend Leistung freigibt.
*   **Batterie-Schutz:** Berücksichtigt den Ladestand (SoC) des E3DC-Hauskraftwerks, um die Batterie nicht leerzuziehen.
*   **Web-Interface:** Integration in das E3DC-Webportal mit Live-Status, COP-Berechnung und manueller Steuerung.
*   **Smart-Home-Schnittstelle:** Nutzt Modbus TCP zur Kommunikation mit der Wärmepumpe.
*   **System-Integration:** Vollständig in den E3DC-Control Installer, Status-Check und Rechte-Management integriert.

> **Update 4.9.3:** Das Frontend ergaenzt wichtige Livewerte wie Sole Ein/Aus,
> Vorlauf/Ruecklauf-Soll, Warmwasser-Soll und Energiezaehler jetzt robuster
> direkt aus den Luxtronik-Daten, wenn die WebSocket-Zwischendatei einzelne
> Felder nicht enthaelt.

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
    export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
    test -f "$E3DC_INSTALL_PATH/e3dc-setup"
    bash "$E3DC_INSTALL_PATH/e3dc-setup"
    ```

2.  Wähle im Hauptmenü unter **Erweiterungen** den Punkt:
    *   **101** – Luxtronik Manager installieren/konfigurieren

3.  Der Assistent führt dich durch die Einrichtung:
    *   Installation der Python-Abhängigkeiten (`luxtronik`, `requests`).
    *   Einrichtung des Systemdienstes (`energy_manager`).
    *   Abfrage der Konfigurationswerte (IP-Adresse, Grenzwerte).

---

## 4. Konfiguration

Die Konfiguration ist in V4 zentral in **`data/e3dc_v4.json`** gespeichert. Alte `config.lux.json` oder `e3dc.config.txt` Dateien werden beim Update/Migration nur noch als Quelle übernommen.

Die Bearbeitung erfolgt am einfachsten über das **Web-Interface** (Config Editor).

**Pfad:** `/var/www/html/data/e3dc_v4.json`

### Wichtige Parameter (in e3dc_v4.json)
| Parameter | Beschreibung | Standard |
| :--- | :--- | :--- |
| `luxtronik_ip` | IP-Adresse der Wärmepumpe im lokalen Netzwerk. | `0.0.0.0` (nicht konfiguriert) |
| `GRID_START_LIMIT` | Einspeiseleistung in Watt, ab der der Boost startet. **Negativ** bedeutet Einspeisung. | `-3500` |
| `MIN_SOC` | Mindest-Ladestand der Hausbatterie in %, damit der Boost freigegeben wird. | `65` |
| `AT_LIMIT` | Außentemperatur-Grenze in °C. Unterscheidet zwischen Sommer- (nur WW) und Winterbetrieb. | `14.0` |
| `WWS` | Warmwasser-Sollwert im Boost-Modus (Sommer/Übergang). | `55.0` |
| `WWW` | Warmwasser-Sollwert im Winterbetrieb. | `45.0` |
| `HZ` | Absoluter Heizungs-Setpoint für den Rücklauf während des Boosts. | `32.0` |
| `luxtronik_pause_setpoint_c` | Rücklauf-Sollwert für die weiche SHI-Sollwertsperre. EMS-Sicherheitsbereich 15 bis 22 °C; keine echte EVU-/SG-Ready-Sperre. | `20.0` |

---

## 5. Funktionsweise

### Der Hintergrunddienst (`energy_manager.py`)
Das Skript läuft als Systemd-Service (`energy_manager`) im Hintergrund.

1.  **Zyklus:** Der Energy Manager bewertet Wärmebudget und Gerätezustand in einem kurzen Regelzyklus. Live-Telemetrie kommt primär aus dem Luxtronik-WebSocket; die für Schutzentscheidungen nötigen physischen Zustände werden über dieselbe dauerhaft offene Modbus-Sitzung gelesen.
2.  **Entscheidung:**
    *   Liegt ein gültiger Besitzer vor, z.B. Pre-Dump, Preisfenster oder freigegebenes Wärmebudget aus dem Storage Manager?
    *   Sind Mindestlaufzeit, Komfortgrenzen, Warmwasser-/Heizgrenzen und Schutzwerte erfüllt?
    *   -> **Boost AN** (Warmwasser-Soll wird erhöht, ggf. Heizung angehoben).
3.  **Abschaltung:**
    *   Wird Strom aus dem Netz bezogen (> 50W) oder die Batterie entladen?
    *   -> Ein Timer startet. Nach 10 Minuten Defizit wird der **Boost AUS** geschaltet (Reset auf Normalwerte).

Der vollständige Minutenverlauf bleibt als begrenzter Live-Puffer in der
RAM-Disk. Das persistente Luxtronik-Betriebsarchiv schreibt höchstens eine
kompakte Stützstelle je fünf Minuten und bewahrt diese Tagesdateien sieben Tage
auf. Es ist damit ein kurzzeitiges Betriebsarchiv, kein Langzeit- oder
Sicherungsarchiv.

### Das Web-Interface (`waermepumpe.php`)
Die PHP-Datei visualisiert die Daten:
*   **Live-Werte:** Temperaturen (Vorlauf, Rücklauf, WW, Außen), Leistung, COP (Wirkungsgrad).
*   **Status:** Zeigt an, ob Verdichter, Heizstab oder Pumpen laufen.
*   **Steuerung:** Ermöglicht das manuelle Starten eines "Notfall-Boosts" (z.B. um die Batterie vor dem Abend schnell zu leeren).

### Quell-Erholung
Der Pausenmodus wird fachlich als **Quell-Erholung** geführt. Eine Pause soll
die Wärmepumpe nicht beliebig abschalten, sondern Quelle, Gebäude und
Speicherplanung in einen besseren Arbeitspunkt bringen. Sie braucht deshalb
immer einen Besitzer, eine Mindestlaufzeit, eine Wiedereinschaltsperre und
Komfortwächter für Warmwasser, Rücklauf und Außentemperatur.

In V5 ist die alte autonome PV-Pause des Energy Managers standardmäßig
gesperrt. Langfristig darf Quell-Erholung nur als Auftrag des Storage Managers
laufen; der Energy Manager setzt dann nur noch Luxtronik-Sollwerte oder
SG-Ready-/Shelly-Aktoren um.

Damit der Pausenmodus fachlich sauber bleibt, muss in der Konfiguration die
Wärmequelle gesetzt werden. Quell-Erholung ist nur für Sole/Erdreich, Grundwasser oder Direktverdampfung freigegeben.
Luft-Wärmepumpen und unbekannte Quellen werden nicht pausiert, weil dort keine
speichernde Quelle regenerieren kann und eine Pause eher Komfort- oder
Taktungsrisiken erzeugt.

Eine neue Quell-Erholung beginnt nur nach einer beobachteten realen
Verdichterlast. War der Verdichter vor dem geplanten Pausenbeginn bereits
mindestens so lange aus wie die geplante Pause, ist die Quelle ausreichend
erholt und der Auftrag wird verworfen. Eine laufende Quell-Erholung bleibt bis
zur prognostizierten PV-Kante verriegelt; kurzzeitige Wolken ändern diesen
Endpunkt nicht. Wärmebudget, WW-Schutz, Komfortgrenzen und Hardware-Schutz
können die Pause vorzeitig beenden. Danach verhindert eine
Wiedereinschaltsperre, dass dieselbe Prognosekante sofort eine neue Pause
startet.

Bei Luxtronik wird diese Pause als `Mode 1 = Setpoint` mit einem abgesenkten,
konfigurierbaren Rücklauf-Sollwert umgesetzt. `Mode 0` beendet nur die externe
SHI-Beeinflussung und übergibt die Entscheidung wieder an die interne
Luxtronik-Regelung. Die physische Betriebsart und der Heiz-/WW-Status werden
separat aus den Input-Registern gelesen.

### Warmwasser und Verdichterschutz

Der 30-Minuten-Schutz beginnt erst mit einem physisch bestätigten
Warmwasserlauf, nicht bereits beim Senden eines Sollwerts. Er schützt einen
gestarteten Zyklus vor einem EMS-bedingten Abbruch. Er zwingt die Wärmepumpe
nicht zum Überheizen: Erreicht die Luxtronik den Zielwert und beendet den Lauf
selbst, wird der externe Auftrag freigegeben.

Ein direkter 55-°C-Boost aus PV-Überschuss, Pre-Dump oder Preislogik bleibt
bei einem kurzen Budgetwechsel während der Signalhaltezeit und der
konfigurierten Defizitfrist stabil. Fehlt anschließend weiterhin ein
startfähiges Budget, wird ausschließlich das Warmwasserziel auf den aktiven
Timerwert oder auf die normale Luxtronik-Regelung zurückgegeben. Ein Heiztakt
gilt dabei nicht als Warmwasserlauf. Ein physisch belegter WW-Zyklus bleibt
geschützt; nach der Rücknahme öffnet erst eine neue, zur aktuellen
Wärmeanfrage gehörende Budgetentscheidung den direkten Boost erneut.

---

## 6. Dateistruktur

Die Dateien befinden sich unter `$E3DC_INSTALL_PATH/Installer/luxtronik/`:

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

### ⚠️ Fehlermeldung: „Unerwarteter Boost-Status"
Diese Meldung stammt aus älteren Ständen, die SHI-Auftrag und physischen
Betriebszustand vermischt haben. Aktuell werden Holding-Register als
`SHI_HZ_Mode` und `SHI_WW_Mode` getrennt von Verdichter, Betriebsart sowie
Heiz-/WW-Status ausgewertet. `Mode 1` bedeutet deshalb nur externer Setpoint
und beweist keinen laufenden Verdichter. In HA-Setups muss der Slave denselben
Release-Stand besitzen und im Standby alle Regel- und Modbus-Dienste gestoppt
halten.

### Modbus-Verbindungsverhalten

Die Luxtronik-SHI-Schnittstelle reagiert empfindlich auf konkurrierende
Verbindungen und Schreibfolgen. Der Energy Manager besitzt deshalb genau einen
Treiber und serialisiert alle Lese- und Schreibzugriffe auf dessen bestehender
TCP-Sitzung. Der zusätzliche physische Status wird nicht über eine zweite
Verbindung gelesen. Schreibbefehle werden nicht automatisch wiederholt; schlägt
der Modus-Schreibschritt fehl, wird der zugehörige Setpoint nicht mehr
geschrieben. Derselbe fehlgeschlagene Zielbefehl wird für 60 Sekunden nicht
erneut auf den Bus gegeben. Die bewährte Reihenfolge und die Wartezeiten
zwischen Modus und Setpoint bleiben unverändert.

Die offiziellen FC04-Input-Adressen bilden keinen lückenlosen Block:
`10000` enthält die Verdichter-/ZWE-Bitmaske, `10002..10004` enthalten
Betriebsart sowie Heiz- und Warmwasserstatus. Die unbelegten Adressen `10001`
und `10005` werden deshalb nicht mitgelesen. Der Treiber sendet pro
Regelabfrage zwei serialisierte Requests für die dokumentierten Bereiche.

Die Modbus-Sitzung bleibt auch im Zustand `NORMAL` offen.
Das ist reine Verbindungsverwaltung und keine zusätzliche Regelwirkung:
`Mode 0` bleibt ohne externe SHI-Beeinflussung, und die dauerhafte Sitzung
erzeugt weder zusätzliche Sollwerte noch zusätzliche FC06-Schreibbefehle.
