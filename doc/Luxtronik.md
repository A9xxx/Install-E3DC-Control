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

> **Update 4.9.3:** Das Frontend ergänzt wichtige Livewerte wie Sole Ein/Aus,
> Vorlauf/Rücklauf-Soll, Warmwasser-Soll und Energiezähler jetzt robuster
> direkt aus den Luxtronik-Daten, wenn die WebSocket-Zwischendatei einzelne
> Felder nicht enthält.

---

### PV-Boost nach einem Update

Für den automatischen Luxtronik-PV-Boost (`wp_type=0`) werden ein elektrisches
Leistungsprofil und ausdrücklich erlaubte Akku-/Netzkontingente benötigt.
Fehlen diese Angaben, warten neue optionale PV-Starts. Normale Heizung,
Warmwasser-Zeitfenster und deren bestehende Schutzfunktionen bleiben unabhängig.
Die Installation selbst wird dadurch nicht blockiert. Die Einrichtung steht
im Config Editor unter **PV-Überschuss-Boost → Luxtronik: Leistungsprofil und
Energie zur Überbrückung**.

Eine ungenutzte Startfreigabe wird nicht als Ende des Wärmebedarfs behandelt.
Heizung und Warmwasser besitzen getrennt zugeordnete Aufträge und
Rückmeldungen. Ein berechtigter Warmwasser-Timer verriegelt deshalb keinen
neuen Heizungsauftrag. Nach einem ausbleibenden Start wird der betroffene
PV-Auftrag kontrolliert zurückgenommen; ein neuer Start wartet auf geklärte
Wirkung, frische Daten, ausreichende Deckung und die Wiedereinschaltsperre.
Ein bestätigter Sollwert allein beweist keinen Verdichterlauf.

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
| `grid_start_limit` | Startschwelle in Watt; **negativ** bedeutet Einspeisung. Die zentrale Verteilung berücksichtigt Verbraucherprioritäten. Kein Ersatz für die maximale elektrische Geräteaufnahme. | `-3500` |
| `min_soc` | Mindest-Ladestand der Hausbatterie für neue Boost-Starts. Die physische Notstromreserve wird zusätzlich geschützt. | `80` |
| `heizgrenze_temp` | Außentemperatur-Grenze in °C zwischen Sommer- und Winterbetrieb. | `10.0` |
| `wws` | Warmwasser-Sollwert im Boost-Modus im Sommer. | `50.0` |
| `www` | Warmwasser-Sollwert im Boost-Modus im Winter. | `48.0` |
| `hz` | Absoluter Heizungs-Sollwert für den Rücklauf während des Boosts. | `32.0` |
| `luxtronik_pause_setpoint_c` | Rücklauf-Sollwert für die weiche SHI-Sollwertsperre. EMS-Sicherheitsbereich 15 bis 22 °C; keine echte EVU-/SG-Ready-Sperre. | `20.0` |

### Leistungsprofil und Quellen für den Luxtronik-PV-Boost

Die folgenden Werte gelten für `wp_type=0`. Das Leistungsprofil muss die
maximal mögliche **elektrische** Aufnahme innerhalb derselben Messgrenze
beschreiben, die der Regler für die WP verwendet. Thermische Heizleistung,
typischer Verbrauch und Startschwelle sind keine sicheren Obergrenzen.
Nicht erfasste Pumpen verbleiben in der Hauslast oder werden separat genau
einmal berücksichtigt. Eine mögliche Zusatzheizung darf weder im Profil
fehlen noch nochmals als eigener Verbraucher abgezogen werden.

| Parameter | Bedeutung | Standard |
| :--- | :--- | :--- |
| `wp_pv_max_power_w` | Belegte maximale elektrische Aufnahme in W innerhalb der WP-Messgrenze. `0` bedeutet: Profil fehlt. | `0` |
| `wp_pv_battery_max_w` | Maximal erlaubte Akku-Überbrückungsleistung für die WP in W. `0` sperrt diese Quelle. | `0` |
| `wp_pv_battery_limit_wh` | Akkuenergie für PV-Überbrückung in den jeweils letzten 24 Stunden. `0` sperrt diese Quelle. | `0` |
| `wp_pv_grid_max_w` | Maximal erlaubte Netz-Überbrückungsleistung für die WP in W. `0` sperrt diese Quelle. | `0` |
| `wp_pv_grid_limit_wh` | Netzenergie für PV-Überbrückung in den jeweils letzten 24 Stunden. `0` sperrt diese Quelle. | `0` |
| `wp_min_runtime_min` | Geschützte Verdichterlaufzeit ab bestätigtem physischem Start. | `30` |
| `wp_restart_block_min` | Wiedereinschaltsperre nach bestätigtem Verdichterstopp. | `20` |
| `pv_boost_delay` | Dauer der stabilen PV-Startqualifikation in Sekunden, bevor eine verbindliche Startzuteilung beginnt. | `30` |
| `wp_pv_reaction_s` | Für Messung, Kommunikation und wirksame Lastanpassung anzusetzende Reaktionsfrist in Sekunden. Muss zum Geräteprofil passen. | `30` |
| `wp_pv_start_wait_s` | Wartefrist auf den tatsächlichen Verdichterstart nach einem Auftrag; mindestens 600 Sekunden. | `600` |
| `wp_pv_handoff_timeout_s` | Frist für die Übergabe von Wallboxleistung vor dem WP-Auftrag in Sekunden. | `120` |

Bei jeder Quelle müssen Leistung **und** Energie erlaubt sein. Vor einem
optionalen Start reserviert der Regler Energie für die maximale Geräteaufnahme
während Mindestlaufzeit, Startwartefrist und Reaktionsfrist. Die konfigurierten
Kontingente müssen diese Zusage auch bei ausfallender PV tragen können.
Erwarteter Sonnenschein ersetzt diese Deckung nicht. Reicht das Kontingent
nicht, wartet der zusätzliche PV-Start mit einem Diagnosegrund. Aktuelle
Speicherleistung, Notstromreserve, Hausanschlussgrenzen und bereits gebundene
Verbraucher begrenzen die tatsächlich verfügbare Deckung zusätzlich.

Bei freier Speicherautomatik kann der Akku einen Lastsprung bereits übernehmen,
bevor die nächste EMS-Vorgabe wirkt. Auch eine überwiegend aus dem Netz
vorgesehene Überbrückung benötigt deshalb ein erlaubtes Akku-Reaktionspolster,
solange eine wirksame Entladesperre nicht belegt ist. Eine gewünschte
Speicherbetriebsart allein belegt weder eine Sperre noch verfügbare Leistung.

Verbrauchte Wh werden aus der gebundenen Restenergie umgebucht. Spätere
Einspeisung erstattet sie nicht. Das rollierende 24-Stunden-Kontingent wird
weder um Mitternacht noch durch Wolkenwechsel, HZ-/WW-Wechsel,
Prioritätswechsel oder Dienstneustart zurückgesetzt. Nach geklärtem Zyklusende
wird nur ungenutzte Reservierung freigegeben. Fehlende Messungen oder ein
ungültiger Laufzeitzustand erzeugen keine kostenlose Energie und erlauben
keinen neuen Start ohne geklärte Deckung.

Das Energiekonto und die noch möglicherweise wirksamen PV-Kanalaufträge
werden getrennt und privat dauerhaft gespeichert. Neue Energiezusagen und
Auftragsabsichten werden vor ihrer Freigabe gesichert. Ein Dienst- oder
Hostneustart eröffnet deshalb kein neues Kontingent. Fehlt ein vertrauenswürdiges
Konto, etwa bei der ersten Einrichtung oder einer Docker-Neuerstellung,
warten neue optionale PV-Starts zunächst 24 Stunden nachweisbar verstrichene
Laufzeit. Ein weiterer Neustart behält die Restwartezeit bei. Die Freigabe
setzt anschließend frische Messwerte, einen bestätigten Verdichterstillstand
und geklärte alte Aufträge voraus. Normale Heizung und berechtigte
Warmwasser-Zeitfenster bleiben davon unabhängig. Die privaten Dateien dürfen
nicht als vermeintliche Reparatur gelöscht werden.

Die Fristen sind Einstellungen des Energiemanagements, keine garantierten
Herstellerzeiten. Ohne belegte Leistungsrampe muss die Überbrückung den
möglichen Leistungssprung tragen. Die AIT-SHI-Anleitung empfiehlt für
PV-Betrieb eine Ausschaltverzögerung. Eine dort beschriebene weiche
Leistungsbegrenzung kann bei entsprechender Temperaturabweichung übergangen
werden und ist deshalb keine harte elektrische Obergrenze.
[AIT-SHI-Anleitung, Seiten 5 und 16](https://files.ait-group.net/FILES/Alpha-InnoTec/Betriebsanleitungen/01%20Waermepumpen/05%20Regler/Zubehoer/83026900aDE_SHI.pdf)

---

## 5. Funktionsweise

### Der Hintergrunddienst (`energy_manager.py`)
Das Skript läuft als Systemd-Service (`energy_manager`) im Hintergrund.

1.  **Zyklus:** Der Energy Manager bewertet Wärmebudget und Gerätezustand in einem kurzen Regelzyklus. Live-Telemetrie kommt primär aus dem Luxtronik-WebSocket; die für Schutzentscheidungen nötigen physischen Zustände werden über dieselbe dauerhaft offene Modbus-Sitzung gelesen.
2.  **Entscheidung:**
    *   Liegt ein gültiger Besitzer vor, z.B. Pre-Dump, Preisfenster oder freigegebenes Wärmebudget aus dem Storage Manager?
    *   Sind Mindestlaufzeit, Komfortgrenzen, Warmwasser-/Heizgrenzen und Schutzwerte erfüllt?
    *   -> **Boost AN** (Warmwasser-Soll wird erhöht, ggf. Heizung angehoben).
3.  **PV-Freigabe und Rücknahme bei Luxtronik:**
    *   Bei WP-Vorrang erhält nutzbarer Wärmebedarf zuerst eine abgesicherte Freigabe. Die Wallbox nutzt den tatsächlich nicht angenommenen Rest und passt ihren Ladestrom an.
    *   Bei Wallbox-Vorrang beginnen neue optionale WP-Starts aus dem verbleibenden Rahmen. Ein bereits geschützter Verdichter behält seine gebundene Deckung.
    *   Fällt der Überschuss weg, tragen erlaubte Akku-/Netzquellen den geschützten Übergang. Die Rücknahme wartet auf das Ende der Mindestlaufzeit und der noch wirksamen Signalhaltezeit.
    *   Nur benannte Schutzfunktionen, etwa Nutzer-Aus, Gerätestörung, Notstromreserve, Hausanschlussgrenze oder eine harte Quellenenergiegrenze, dürfen die Schutzzeit verkürzen. Gewöhnlicher Netzbezug, ein Budgetwechsel oder eine wirtschaftliche Priorität sind kein solcher Schutzfall.

Heizung und Warmwasser teilen sich denselben Verdichter und werden elektrisch
nur einmal bilanziert. Ein zulässiges normales Warmwasser-Zeitfenster benötigt
keinen PV-Boost. Dessen Rückmeldung darf einen unabhängigen Heizungsauftrag
nicht als bereits ausgeführt bestätigen. Die interne Luxtronik darf ihren
Takt bei erreichtem Ziel selbst beenden; der Regler erhöht keine Temperaturen,
um eine Mindestlaufzeit künstlich zu erzwingen.

Startqualifikation, Leistungsübergabe und Befehlsprüfung besitzen getrennte
Fristen. Die 25 Sekunden für die Befehlsprüfung beginnen erst mit dem
tatsächlich gesendeten WP-Auftrag. Vorher muss eine erforderliche Absenkung
der Wallboxleistung tatsächlich sichtbar sein. Ein Phasenwechsel bleibt im
eigenen Schutzablauf der Wallbox; seine Sperrzeit wird nicht auf jede
Ladestromanpassung übertragen. Nach einer Signalrücknahme bleiben eine noch
ungeklärte Startwirkung und ein weiterlaufender Verdichter berücksichtigt.

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

Zusätzlich zeigt die Oberfläche den rein lesenden Warmwasser-Betriebsfortschritt
in fünf belegten Stufen: **WW angefordert**, **WW-Hydraulik aktiv**, **Verdichter
gestartet**, **40-Hz-Zwischenstufe** und **WW-Ziellast erreicht**. BUP oder ZUP
belegen nur im separat bestätigten Warmwasserbetrieb die Hydraulik; sie beweisen
keinen Verdichterlauf. Die 40-Hz-Stufe benötigt eine gemessene Frequenz von
35 bis 45 Hz, die Ziellast eine darüberliegende und zur Anforderung passende
Frequenz. Fehlende oder veraltete Telemetrie bleibt `EVIDENCE_LIMIT`. Diese
Anzeige schätzt keine Leistung und verändert weder Budget noch Regelung.

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

Ein zusätzlicher Warmwasser-Sollwert benötigt einen zugehörigen Auftrag aus
PV-Überschuss, Pre-Dump oder Preislogik. Beim Luxtronik-PV-Boost tragen die
getrennten Quellenkontingente die zugesagte Schutzfrist. Wird der zusätzliche
Auftrag danach zurückgenommen, gilt wieder der berechtigte Timerwert oder
die normale Luxtronik-Regelung. Ein Heiztakt gilt nicht als Warmwasserlauf;
beide Betriebsarten nutzen jedoch denselben physisch geschützten Verdichter.
Nach der Rücknahme benötigt ein neuer Boost erneut eine zur aktuellen
Wärmeanfrage gehörende Budgetentscheidung.

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
