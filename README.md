# E3DC-Control Web-Portal & Installer

Ein hochperformantes, modulares Dashboard und Installations-System für [Eba-M/E3DC-Control](https://github.com/Eba-M/E3DC-Control). Es verwandelt das bewährte Konsolen-Programm in ein intelligentes Smart-Home-Zentrum mit moderner Web-Oberfläche, eigenem Energy Manager und proaktivem Systemschutz.

![E3DC-Control Dashboard](html/icons/app-icon-512.png)

## ✨ Highlights & Features

### 📊 Modernes Live-Dashboard & Statistik
* **Echtzeit-Energiefluss:** Animierte Darstellung aller Energieflüsse (Haus, PV, Netz, Batterie, Wallbox, Wärmepumpe).
* **Tagesstatistik:** Hochpräzise Echtzeit-Berechnung von **Autarkie** und **Eigenverbrauch** sowie detaillierte Aufschlüsselung der Energieverteilung in kWh und Prozent.
* **Responsive & PWA:** Vollständig optimiert für Desktop und Mobile (Dark/Light Mode). Dank PWA-Support wie eine native App auf iOS und Android installierbar.
* **Dynamische Liniendiagramme:** Schneller Wechsel zwischen Live-Verlauf, SoC-Prognose und Archiv-Daten der letzten 30 Tage.

### ⚡ Smart Charging & Luxtronik Energy Manager
* **Intelligentes Lademanagement:** Nutze dynamische Stromtarife (aWATTar / Tibber), um dein Auto oder Haus an den günstigsten Punkten des Tages aufzuladen.
* **Luxtronik Wärmepumpen-Integration:** Native Modbus-Steuerung für Alpha Innotec / Novelan Wärmepumpen.
* **Superintelligenz & Morning Boost:** Die Anlage plant vollautomatisch voraus, belädt bei extrem günstigen Preisen (z.B. < 0 ct/kWh) oder pausiert die Wärmepumpe strategisch ("Aushungern"), um Platz für PV-Überschuss am Mittag zu schaffen.

### 🚀 Maximale Performance & SD-Karten-Schutz
* **RAM-Disk Caching:** Konfigurationen, Strompreise, Live-Werte und Log-Daten werden intelligent im Arbeitsspeicher gehalten. Dies schont die SD-Karte des Raspberry Pi massiv und reduziert die CPU-Last.
* **Sanftes Rendering:** Diagramme laden dank asynchronem Polling, DNS-Prefetching und weichen CSS-Übergängen (`onload`-Events) spürbar flüssiger.

### 🛡️ System-Stabilität & Watchdog
* **Systemd-Dienste:** Alle Module (E3DC-Core, Energy Manager, Live-Grabber) laufen als robuste Hintergrunddienste mit Auto-Restart-Fähigkeit.
* **Piguard Watchdog:** Überwacht das Netzwerk, den SD-Karten-Speicher und Dateihänger. Startet bei Bedarf einzelne Dienste (oder den Raspberry Pi) intelligent neu.
* **Telegram-Benachrichtigungen:** Erhalte tägliche Statusberichte (Uptime, Temperatur) oder Warnungen direkt auf dein Smartphone.
* **Wartungsfrei:** Automatisches Log-Management (Log-Rotation) verhindert volllaufende Festplatten, und eine intelligente Selbstreparatur der Dateirechte sorgt für störungsfreien Dauerbetrieb.

### 🔄 Auto-Update & Rollback
* **Selbstheilend:** Das System prüft (optional vollautomatisch) nachts auf Updates und aktualisiert sowohl das E3DC-Core-Programm als auch das Web-Dashboard.
* **Sicheres Rollback:** Vor jedem Update wird ein lokales Backup erstellt, zu dem jederzeit per Mausklick (oder über das Konsolenmenü) zurückgekehrt werden kann.

---

## ️ Installation

Die Installation erfordert einen Raspberry Pi (oder ein Debian-basiertes System) und Root-Rechte.

1. **Repository klonen:**
```bash
cd ~
git clone https://github.com/A9xxx/Install-E3DC-Control.git Install
cd Install
```

### Schritt 3: Installer starten

Wechsle in das Verzeichnis und starte das Setup:

```bash
sudo python3 fix_bom.py
cd Install
sudo python3 installer_main.py
```

Im Menü wählst du für eine Neuinstallation am besten die Option **"Alles installieren"**. Der Assistent führt dich durch die Einrichtung von Abhängigkeiten, der E3DC-Software, dem Webserver und der Konfiguration.

---

## ️ Wartung & Updates

Der Installer dient auch als Wartungstool. Starte ihn jederzeit erneut (`sudo python3 installer_main.py`), um Updates einzuspielen, Berechtigungen zu reparieren oder Backups zu verwalten.

Für automatisierte Abläufe (z.B. via Cronjob) gibt es den Headless-Modus: `sudo python3 installer_main.py --unattended`


---
