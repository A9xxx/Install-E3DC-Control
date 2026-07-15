# E3DC-Control Web-Portal & Installer

Ein hochperformantes, modulares Dashboard und Installations-System für die **native Python-Architektur** [A9xxx/Install-E3DC-Control](https://github.com/A9xxx/Install-E3DC-Control) <kbd>Version 5.3.2b</kbd>. Es verwandelt das System in ein intelligentes Smart-Home-Zentrum mit moderner Web-Oberfläche, eigenem Energy Manager und proaktivem Systemschutz.

![E3DC-Control Dashboard](html/app-icon-512.png)

## Aktuelle Version und Update

Die aktuelle stabile Version ist **5.3.2b**. Hinweise zum Web-, Konsolen- und Docker-Update sowie zur geprüften Wiederherstellung stehen in [doc/Update.md](doc/Update.md). Dieser Stand ist der bereinigte, parentlose Rollback-Root und gibt selbst keinen älteren öffentlichen Programmstand als Rückfall frei.

> [!WARNING]
> **⚠️ Achtung: Nutzung auf eigenes Risiko!**
> Diese Software greift aktiv in Energieflüsse, Speicher-, Wallbox-, Wärmepumpen- und Smart-Home-Logik ein. Installation und Betrieb setzen voraus, dass der Nutzer seine Anlage, Netzvorgaben und Leistungsgrenzen fachlich versteht. Ausführlicher Haftungsausschluss siehe unten.

## ✨ Highlights & Features

> **Aktueller Architekturstand:** Die zentrale Konfiguration liegt weiterhin in `data/e3dc_v4.json`. Der Dateiname bleibt aus Kompatibilitätsgründen bestehen. Eine alte `e3dc.config.txt` wird nur noch für Migration und Legacy-Fallbacks importiert. Details stehen in [doc/V4_Konfiguration_und_Regelung.md](doc/V4_Konfiguration_und_Regelung.md).

> **Config-Schutz:** Standardinstallationen speichern `data/e3dc_v4.json` und lokale Config-Backups mit `660` für Install-User und `www-data`, damit WebUI und Dienste weiter automatisch starten, die Datei aber nicht mehr weltlesbar ist. Der normale Config-Download ist redigiert; der Raw-Download enthält Zugangsdaten und wird nur angeboten, wenn eine Web-PIN gesetzt ist. Der Kompatibilitätsmodus (`664`) ist nur für eigene externe Leser gedacht.

> **Bedienansichten:** Config-Editor und Wallbox-Seite unterscheiden zwischen einfacher Ansicht für Einrichtung und täglichen Betrieb sowie erweiterter Ansicht für alle Detailparameter. Die Logik und Abgrenzung sind in [doc/Frontend_Ansichten.md](doc/Frontend_Ansichten.md) dokumentiert.

> **Neu in 5.3.2b Stable:** Der fachlich geprüfte Stand wird als parentloser Nullpunkt einer bereinigten Historienepoche veröffentlicht. Der einmalige Wechsel bestehender Installationen erfolgt über den geprüften Bootstrap-/Installerpfad mit externer Sicherung und Hashbindung.

> **Dynamische Preisquellen:** SMARD bleibt die Standardquelle für Börsenstrompreise. Optional kann ein ENTSO-E Transparency Platform Security Token als 15-Minuten-Fallback hinterlegt werden; danach bleibt aWATTar der grobe Stundenfallback. Den ENTSO-E-Zugang erhältst du über einen Transparency-Platform-Account, eine E-Mail mit Betreff `RESTful API access` an `transparency@entsoe.eu`, die Freigabe durch ENTSO-E und anschließend die Token-Erzeugung im Account.

### 📊 Modernes Live-Dashboard & Statistik
* **Echtzeit-Energiefluss:** Animierte Darstellung aller Energieflüsse (Haus, PV, Netz, Batterie, Wallbox, Wärmepumpe). Nodes können je Ansicht verschoben, farblich angepasst und per Standardlayout zurückgesetzt werden.
* **Smart-Home Visualisierung:** Automatische Einblendung eines Heizstabs (sofern konfiguriert) und eines pulsierenden, roten Warn-Banners bei einem E3DC-Notstrom-Einsatz.
* **Tagesstatistik:** Hochpräzise Echtzeit-Berechnung von **Autarkie** und **Eigenverbrauch** sowie detaillierte Aufschlüsselung der Energieverteilung in kWh und Prozent.
* **CO₂-Fußabdruck & Gamification:** Ein animierter CO₂-Baum wächst mit dem Autarkiegrad – vom Setzling 🌱 zum vollen Wald 🌲🌳🌲. Zeigt die täglich eingesparten kg CO₂ basierend auf dem deutschen Strommix (0,38 kg/kWh) an.
* **Energie-Quellen (Mix):** Ein interaktives Doughnut-Chart mit klickbarer Legende (PV, Einspeisung, Bezug, Batterie laden/entladen) und farbcodierten Energiebilanz-Badges auf dem Smartphone.
* **Langzeit-Archiv (SQLite):** Betrachte interaktive Balkendiagramme für die Bilanzen der letzten Tage, Monate oder Jahre inkl. Stromkosten-Mittelwert. Ein administrativer **Statistik-Editor** erlaubt zudem die rasche Korrektur von Messfehlern direkt in der Datenbank.
* **Aufgeräumtes Design:** Ein globales Auge-Icon in der Kopfleiste erlaubt es dir, detaillierte technische Parameter in Echtzeit aus dem Dashboard auszublenden, um eine wunderschön puristische und ablenkungsfreie Oberfläche zu schaffen.
* **Responsive & PWA:** Vollständig optimiert für Desktop und Mobile (Dark/Light Mode). Dank PWA-Support wie eine native App installierbar. Inklusive optionalem **PIN-Schutz**.
* **🧭 Batterie-Diagnostik (Vitals):** Ein dediziertes Batterie-Gesundheits-Dashboard zeigt SOH, Ladezyklen, Zelltemperaturen (Min/Max) und den Zell-Drift (Spannungs-Spread in mV) pro Pack in Echtzeit an. Basiert auf dem Open-Source-Projekt [RSCPGui von rxhan](https://github.com/rxhan/RSCPGui) via direktem RSCP-Zugang zum BMS der E3DC.

### ⚡ Smart Charging & Luxtronik Energy Manager
* **Multi-EV Support (Flotten-Management):** Das System unterstützt mehrere Fahrzeuge per Bluelink, MQTT, openWB-SoC oder manueller Vorlage. Auf der Wallbox-Seite werden Fahrzeuge direkt pro Wallbox zugeordnet, damit Akku, Ziel-SoC und Ladeleistung auch bei Wallboxen ohne Fahrzeugerkennung eindeutig passen.
* **Dynamische Ladezeit-Berechnung:** Das Dashboard (und das Backend) berechnet durchgängig anhand der aktuellen Ladeleistung vollautomatisch die geschätzte Restdauer bis zum Erreichen von 100% sowie zum Ziel-SoC.
* **Universal-Wallbox Integration:** Nativer, entkoppelter Python Wallbox Manager mit Dual-WB Support für E3DC, openWB/openWB Pro und go-e. openWB Pro wird direkt über `connect.php` als Aktuator geführt, normale openWB-Software bleibt sauber in Primary-/Secondary-Rollen getrennt. Die sichtbaren Modi sind `Aus`, `PV-Kurve ruhig`, `Grundladung stabil`, `PV + Akku bis Untergrenze`, `Sofort bis Preislimit` und `Akku bis Abfahrt`; geplantes Netzladen greift in allen aktiven Modi und bleibt bei `Aus` gesperrt, spontane Marktfreigaben für Wallbox-Netzladen benötigen dagegen `Sofort bis Preislimit`. Im Beobachten-Modus kann der Storage Manager optional nur den Hausspeicher bis zur Untergrenze führen, ohne Wallbox-Befehle zu senden.
* **V2H/V2G-Vorbereitung (read-only):** Bidirektionale Wallboxleistung und gemeldete Fähigkeitsdaten werden erkannt und angezeigt. Eine aktive V2H-/V2G-Steuerung oder SoC-Abschaltung ist in 5.3.2b noch nicht freigegeben. Details: [V2H/V2G-Status](doc/V2x_Dokumentation.md).
* **Intelligenter SoC- und Reichweiten-Sync:** Verzichtest du auf eine direkte Fahrzeuganbindung, kann der **SoC des Fahrzeugs am Dashboard manuell übermittelt werden**. Das System rechnet (interpoliert) ab dann vollautomatisch im Hintergrund die eingeladene Energie ein. Bei openWB-SoC berechnet E3DC-Control die Restreichweite aus Akku-Kapazität und hinterlegtem Verbrauch, damit openWB und Dashboard vergleichbare km-Werte zeigen.
* **Universal Wärmepumpen-Integration:** Native Anbindung für **Luxtronik** (WebSocket), **IDM-Wärmepumpen** (Modbus-TCP) und **Stiebel Eltron ISG/WPM** (read-only Live-Daten). IDM kann mit PV-Überschuss und konfigurierbarer Leistungsobergrenze ruhig als Grundlast laufen; Stiebel liefert Livewerte und nutzt optional einen externen Shelly-Leistungsmesser für die elektrische Live-Leistung in Dashboard/R5. SG-Ready per WLAN-Shelly bleibt als robuste Freigabe für andere Marken verfügbar. Details: [Stiebel-Eltron-ISG-Dokumentation](doc/Stiebel_Eltron_ISG.md).
* **Storage Simulator & adaptive Ladekurve:** Die Anlage plant vollautomatisch voraus. Wetterprognosen, saisonaler Nachtverbrauch, EPEX/Eco-Score und optionales Mittagsziel erzeugen eine geglättete Soll-SoC-Kurve. Der Storage Manager führt die Kurve weich über `iFc`, Kontroll-SoC und gedämpften Aufholbedarf; [Pre-Dump](doc/Pre_Dump.md) schafft vor Kurvenstart Platz gegen Abregelung. Die Abregelreserve hält an passenden Hochleistungs-/Cloud-Edge-Tagen Speicherplatz für PV-Spitzen frei, ohne echten Netz-/WR-Abregeldruck zu blockieren. Der optionale [Unwetterwächter](doc/Unwetterwaechter.md) kann DWD-Warnungen als Kurvenanker oder Nachtreserve berücksichtigen; Speicher-Netzladen und Speicher-Halten im normalen Marktpfad bleiben getrennte, standardmäßig ausgeschaltete Opt-ins und werden beim Ausführen erneut gegen die aktuelle Freigabe geprüft. Zusätzlich blockiert `PV-autark zuerst` den normalen Marktpfad, wenn Speicher plus erwartete PV den restlichen Horizont decken; fällt der SOC unter die Low-SOC-Schwelle, darf ein bewusst freigegebener Speicherpfad wieder wirtschaftlich prüfen. Live-PV und Netzexport haben beim normalen Markt-Netzladen Ausführungsvorrang: dann wartet der Marktpfad in AUTO, statt GRID vorzuziehen.
* **Geplante Lastfenster:** Große, nicht direkt steuerbare Verbraucher können als enges Zeitfenster mit statischer Leistung hinterlegt werden. Der Simulator berücksichtigt die Last in der Prognose, der Manager schützt den Speicher aber erst, wenn die Last im Fenster plausibel sichtbar ist. Details: [Geplante Lastfenster](doc/Geplante_Lastfenster.md).

### 🚀 Maximale Performance & SD-Karten-Schutz
* **RAM-Disk Caching:** Konfigurationen, Strompreise, Live-Werte und Log-Daten werden intelligent im Arbeitsspeicher gehalten. Dies schont die SD-Karte des Raspberry Pi massiv und reduziert die CPU-Last.
* **Native Python Live-API:** Der RSCP-Live-Dienst schreibt atomare JSON-Werte direkt in die RAM-Disk. Ungültige Werte wie `NaN` werden abgefangen, damit Dashboard, Historie und MQTT-Hub stabil weiterlaufen.
* **Klassisches und modernes Frontend:** Beide Dashboard-Layouts bleiben produktiv auswählbar und unterstützen die Detailstufen kompakt, normal und detailreich.
* **Frontend-Optimierung:** Statische Assets (JavaScript) werden automatisch komprimiert (minifiziert) und mit Cache-Busting-Mechanismen ausgeliefert, um die Ladezeiten des Dashboards zu minimieren.

### 🏠 Smart Home Integration
* **Apple Home / Google Home (lokale Matter Bridge, nicht zertifiziert):** Drei read-only Statusschalter bilden Wallbox-Ladung, PV-Produktion und Netzeinspeisung ab. Kopplungsschlüssel werden lokal gesichert; Befehle aus Matter werden nicht an die Anlage weitergegeben. Details: [Matter Bridge](doc/Smart_Home_Matter.md).
* **Web-Push Benachrichtigungen:** Native Push-Nachrichten für Alarme (Notstrom, HA-Failover) und dynamische Statusmeldungen (Ziel-SoC erreicht, Erinnerungen) direkt auf das Smartphone; dafür ist keine Messenger-Integration erforderlich.
* **E3DC MQTT Hub (Auto-Discovery):** Das System pusht vollautomatisch alle Live-Werte (PV, Netz, WP, Wallbox, Strompreis) an deinen MQTT-Broker. Direkte evcc/openWB-Leistungstopics wie `evcc/loadpoints/1/chargePower` können ohne Home Assistant als reale Wallboxleistung eingebunden werden.
* **Entkoppelte Architektur:** Dedizierte Dienste lesen und schreiben JSON-Daten hocheffizient über die RAM-Disk und halten Netzwerk-, Web- und Regelungslogik voneinander getrennt.

### 🛡️ System-Stabilität & Watchdog
* **High Availability Cluster (HA):** Unterstützung für einen zweiten Raspberry Pi als Ausfall-Backup (Aktiv/Passiv) mit überwachten Heartbeats, Konfigurationsabgleich und kontrolliertem Rollenwechsel. Umschaltzeit und Wiederanlauf hängen von Installation und Dienstzustand ab.
  * **Langzeit-Sync:** Die gesamte Ladehistorie und SQLite-Datenbank wird in Echtzeit redundant auf dem Slave gespiegelt.
* **Shadow-Vergleichs-/Testinstanz:** Eine optionale zweite Instanz liest Betriebs-Snapshots der aktiven Anlage und berechnet lokale Vergleichsentscheidungen. Sie erhält keine Anlagenzugangsdaten, sendet keine Hardwarebefehle und übernimmt niemals per Failover. Details: [Shadow-System](doc/Shadow_System_Konzept.md).
* **Systemd-Dienste:** Alle Kernmodule (`e3dc-live`, Storage Manager, Storage Simulator, Wallbox Manager, MQTT-Hub und optionale Verbraucher) laufen als robuste Hintergrunddienste mit Auto-Restart-Fähigkeit.
* **Piguard Watchdog:** Überwacht das Netzwerk, den SD-Karten-Speicher und Dateihänger. Startet bei Bedarf einzelne Dienste (oder den Raspberry Pi) intelligent neu.
* **Telegram-Benachrichtigungen:** Erhalte tägliche Statusberichte (Uptime, Temperatur), Tagesstatistiken zur Energieverteilung sowie einen detaillierten Wochenrückblick direkt auf dein Smartphone. Komfortabel über das Web-UI ohne lästige Cronjobs konfigurierbar.
* **Betriebswartung:** Log-Rotation und begrenzte Update-/Installer-Backups reduzieren den Speicherbedarf. Die Rechteprüfung kann bekannte Abweichungen korrigieren; Systemzustand und freier Speicher bleiben zu überwachen.

### 🔄 Auto-Update & Rollback
* **Web-Updater:** Freigegebene Stable-Stände lassen sich über das Web-Dashboard (`index.php`) installieren. Der Browser zeigt den Installationsfortschritt; Fehler brechen den Vorgang ab und bleiben diagnostizierbar.
* **Optionale Updateprüfung:** Das System kann nachts nach einem freigegebenen Stable-Stand suchen und den geprüften Installerweg starten.
* **Policygebundener Rückfall:** Vor dem Update wird ein externes, manifestiertes und prüfsummengesichertes Backup verlangt. Ein Rückfall ist nur auf den in `UPDATE_POLICY.json` exakt gebundenen Stable-Rollback möglich und setzt ein lesbar validiertes Backup voraus.

## 💬 Community & Support

Besuche unseren offiziellen Support-Thread im Photovoltaikforum für Fragen, Feedback und Updates:
👉 **[E3DC-Control (Native Python): KI-Prognose, dynamische Stromtarife & Wallbox-Steuerung](https://www.photovoltaikforum.com/thread/259876-e3dc-control-native-python-ki-prognose-dynamische-stromtarife-wallbox-steuerung/)**

---

## Haftungsausschluss (Disclaimer)

Die Software wird ohne Gewähr für Fehlerfreiheit, Verfügbarkeit, Wirtschaftlichkeit oder Eignung für einen bestimmten Zweck bereitgestellt. Soweit gesetzlich zulässig, übernehmen die Autoren und Beitragenden keine Haftung für direkte oder indirekte Schäden, Fehlsteuerungen, Energieverluste, Netzbezug, entgangene Einspeisevergütung, Hardwareverschleiß, Datenverlust oder Folgekosten, die durch Nutzung, Fehlkonfiguration, Updates oder Ausfall der Software entstehen. Sicherheitsrelevante Einstellungen des Herstellers, Elektroinstallation, Netzbetreiber-Vorgaben und gesetzliche Anforderungen haben immer Vorrang.

---

## Herkunft & Lizenz

E3DC-Control ist eine eigenständige Python-Implementierung. Die robuste Regelbasis ist fachlich vom C++-Projekt von Eberhard Mayer inspiriert und wurde mit dessen Zustimmung als Grundlage verstanden und eigenständig neu implementiert. Details stehen in [NOTICE.md](NOTICE.md).

Dieses Projekt steht unter der **GNU Affero General Public License v3.0 oder später (AGPL-3.0-or-later)**. Private Nutzung, Anpassung und Community-Weiterentwicklung sind ausdrücklich willkommen.

Wer das Projekt, abgeleitete Versionen oder darauf basierende Dienste öffentlich bereitstellt, verteilt oder kommerziell nutzt, muss die Bedingungen der AGPL einhalten und den vollständigen zugehörigen Quellcode offenlegen. Kommerzielle Sonderlizenzen oder Integrationen außerhalb der AGPL sind nur nach vorheriger schriftlicher Zustimmung möglich.

---

## 💻 Systemvoraussetzungen

E3DC-Control ist ein ressourcenschonendes System zur Steuerung und Optimierung des Energiemanagements. Es läuft auf klassischen Raspberry-Pi-Systemen ebenso wie in virtuellen Umgebungen wie Proxmox, Docker, ESXi oder auf kleinen Intel-/AMD-Hosts.

### Hardware-Anforderungen

Die folgenden Werte sind Richtwerte für einen stabilen Dauerbetrieb:

| Komponente | Minimal, z.B. Raspberry Pi 3 | Empfohlen, z.B. Raspberry Pi 4 / 5 |
| :--- | :--- | :--- |
| **CPU** | 1 bis 2 Cores | 2 Cores |
| **Arbeitsspeicher** | 1 GB RAM | 2 GB RAM |
| **Swap-Speicher** | mindestens 512 MB | 1 GB |
| **Speicherplatz** | 8 GB freier Speicher | 16 GB oder mehr |

> **Hinweis zu Speicherplatz und RAM-Disk:** Eine normale Installation kann inklusive Paketen, Webdateien, Python-Umgebung, Logs, Diagnose- und Backupdaten bereits mehr als 4 GB belegen. Unter 8 GB freiem Speicher wird der Betrieb schnell eng. Werden temporäre Daten und Logfiles in einer RAM-Disk gehalten, schont das SD-Karten und SSDs, belegt aber direkt physischen Arbeitsspeicher. Für RAM-Disk, Docker, ML-Prognose oder größere Diagnosepakete sind 2 GB RAM dringend empfohlen.

### Betrieb in virtuellen Umgebungen

**LXC-Container, empfohlen für Proxmox:** LXC teilt sich den Kernel mit dem Host und arbeitet sehr effizient. Für E3DC-Control reichen in der Praxis meist 2 vCores, 1 GB RAM und 512 MB bis 1 GB Swap.

**Virtuelle Maschine, z.B. KVM/ESXi:** Eine VM bringt den Eigenbedarf des Gast-Betriebssystems mit. Plane 2 vCores, 1 bis 2 GB RAM und 1 GB Swap ein.

> **Wichtig:** Ein Betrieb ohne Swap wird in VMs nicht empfohlen. Bei Lastspitzen, etwa während `apt-get`-Updates oder Modulinstallationen, kann der Linux-Kernel sonst Prozesse über den Out-of-Memory-Killer beenden.

---

## 🛠️ Installation (Klassisch auf dem Raspberry Pi)

Die Installation erfordert einen Raspberry Pi oder ein Debian-basiertes System und einen normalen Benutzer mit `sudo`-Rechten. Gemeint ist **nicht**, dass du dich als Benutzer `root` anmelden sollst. Installiere aus einem eigenen Admin-Benutzer, z.B. `pi`, `admin` oder `e3dc`.

Falls noch kein geeigneter Benutzer vorhanden ist:

```bash
sudo adduser e3dc
sudo usermod -aG sudo e3dc
su - e3dc
```

Danach die Installation als dieser Benutzer starten. Der Installer fragt bei Bedarf das `sudo`-Passwort ab und richtet Systemdienste, Webrechte und Paketabhängigkeiten ein.

### Schritt 1: System vorbereiten & Klonen
```bash
sudo apt update && sudo apt install -y git
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
git clone https://github.com/A9xxx/Install-E3DC-Control.git "$E3DC_INSTALL_PATH"
```

### Schritt 2: Installer starten
Wechsle in das neue Verzeichnis, korrigiere eventuelle Windows-Dateiendungen und starte das Setup-Skript:
```bash
cd "$E3DC_INSTALL_PATH"
sudo python3 fix_bom.py
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

### Schritt 3: Installation / Update starten
Wähle im interaktiven Menü für eine Ersteinrichtung die Option **"1 Installation / Update"**. Der Installer richtet die benötigten Pakete, Dienste, Webdateien, Rechte und den Web-Wizard ein. Falls du aus älteren Anleitungen die Nummer `11` kennst: Diese Eingabe wird aus Kompatibilitätsgründen ebenfalls akzeptiert.

Das Konsolenmenü ist bewusst klein gehalten:

```text
1) Installation / Update
2) Systemstatus anzeigen
3) Rechte prüfen & korrigieren
4) Notfallmodus / System reparieren
5) Policygebundener Stable-Rollback
6) Backup erstellen / verwalten
7) Expertenmenü
8) Systempakete vorbereiten
9) Deinstallation
q) Beenden
```

Erweiterungen wie Docker, Energy Manager, native Wallbox, MQTT, Bluelink oder HA liegen gesammelt im **Expertenmenü**. Normale Konfigurationen erfolgen danach im WebUI.

Das Expertenmenü ist in 10er-Blöcke sortiert:

```text
Kernsystem & Update
  14) Rollback (Datei-Backup)
  15) Watchdog & Telegram konfigurieren
Umgebung & Python
  21) Python venv neu aufbauen (Reparatur)
  22) Python venv Namen ändern
Docker Migration & Verwaltung
  31) Zu Docker wechseln (Auto-Install & Migration)
  32) Docker auflösen & zum lokalen System zurückkehren
Erweiterungen & Smart Home
  41) Energy Manager
```

---

## 🐳 Installation (Via Docker)
E3DC-Control kann alternativ komplett isoliert über Docker betrieben werden. Das Image unterstützt native ARM- (Raspberry Pi) sowie AMD64-Architekturen (Intel NUC, Synology, QNAP).

**Voraussetzung:** Docker und Git müssen installiert sein.

### Schritt 1: Docker installieren
```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl
sudo install -m 0755 -d /etc/apt/keyrings
. /etc/os-release
DOCKER_REPO=debian
DOCKER_CODENAME="${VERSION_CODENAME}"
if [ "${ID}" = "ubuntu" ] || echo "${ID_LIKE:-}" | grep -qw ubuntu; then
  DOCKER_REPO=ubuntu
  DOCKER_CODENAME="${UBUNTU_CODENAME:-$VERSION_CODENAME}"
fi
sudo curl -fsSL --proto '=https' --tlsv1.2 \
  "https://download.docker.com/linux/${DOCKER_REPO}/gpg" \
  -o /etc/apt/keyrings/docker.asc
sudo chmod a+r /etc/apt/keyrings/docker.asc
printf 'Types: deb\nURIs: https://download.docker.com/linux/%s\nSuites: %s\nComponents: stable\nArchitectures: %s\nSigned-By: /etc/apt/keyrings/docker.asc\n' \
  "$DOCKER_REPO" "$DOCKER_CODENAME" "$(dpkg --print-architecture)" | \
  sudo tee /etc/apt/sources.list.d/docker.sources > /dev/null
sudo apt-get update
sudo apt-get install -y docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
sudo usermod -aG docker "$USER"
```
Danach einmal ab- und wieder anmelden, damit die Docker-Gruppenrechte aktiv werden.

### Schritt 2: Repository klonen
```bash
export E3DC_DOCKER_PATH="/absoluter/pfad/zur/docker-installation"
git clone https://github.com/A9xxx/Install-E3DC-Control.git "$E3DC_DOCKER_PATH"
cd "$E3DC_DOCKER_PATH"
```

### Schritt 3: Datenverzeichnis vorbereiten
```bash
mkdir -p data
# Optional: alte e3dc.config.txt nur für die Erstmigration ablegen:
cp /dein/pfad/e3dc.config.txt data/   # optional
```

Neue Installationen werden über den Web-Wizard bzw. den Config-Editor in `data/e3dc_v4.json` eingerichtet.

### Schritt 4: Container starten
```bash
docker compose up -d
```

Das Docker-Image enthaelt den Anwendungscode. Das geklonte Repository liefert die `docker-compose.yml`; Konfiguration und Historie liegen dauerhaft im Docker-Volume bzw. Datenverzeichnis.

### Updates einspielen
```bash
docker compose pull
docker compose up -d --force-recreate
```
> `docker compose pull` holt das aktuelle Release inklusive Python/PHP, Startskript
> und Systempaketen. `--force-recreate` stellt sicher, dass der Container wirklich
> aus dem neuen Image gestartet wird.

> **Wichtig bei zusätzlichen Code-Volumes:** Ein lokales Verzeichnis unter
> `/app/pi/Install` überschreibt den Release-Code aus dem Docker-Image. Für den
> regulären Betrieb werden deshalb nur `data`, `logs` und die Ramdisk dauerhaft
> eingebunden.

---

## 🛠️ Wartung & Updates

Der Installer dient gleichzeitig als dein zentrales Wartungstool. Starte ihn jederzeit erneut mit `bash "$E3DC_INSTALL_PATH/e3dc-setup"`, um Updates einzuspielen, Berechtigungen zu reparieren, Modbus-Geräte nachzuinstallieren oder Backups zu verwalten.

Für automatisierte Abläufe gibt es den Headless-Modus: `bash "$E3DC_INSTALL_PATH/e3dc-setup" --unattended`
