# ⚡ E3DC-Control Installer & Web-Interface

**Intelligente Steuerung und Visualisierung für E3DC Hauskraftwerke auf dem Raspberry Pi.**

Dieses Projekt ist ein Erweiterungsmodul für [E3DC-Control von Eba-M](https://github.com/Eba-M/E3DC-Control).
Es bietet eine Komplettlösung zur Installation, Verwaltung und Visualisierung der E3DC-Control Software, um die Installation und Rechtevergabe so benutzerfreundlich wie möglich zu machen und eine moderne Bedienoberfläche zu schaffen.

---

## 🎯 Was macht dieses Projekt?

Es verbindet die leistungsstarke C++ Steuerung des Basis-Projekts mit einem modernen, responsiven Web-Dashboard.

Die Kernfunktionen der Steuerung (von [Eba-M](https://github.com/Eba-M/E3DC-Control)):
*   **🔋 Intelligentes Laden:** Der Speicher wird basierend auf Wetterprognosen und dynamischen Strompreisen (aWATTar/Tibber) geladen.
*   **📉 Kostenoptimierung:** Nutzung günstiger Strompreisfenster zum Nachladen (insb. im Winter).
*   **☀️ Prognosebasiert:** Vermeidung von Abregelungsverlusten durch vorausschauendes Lademanagement.

Zusätzliche Funktionen dieses Moduls:
*   **📊 Visualisierung:** Ein umfassendes Web-Dashboard zeigt Live-Werte, Historie und Prognosen für PV, Batterie, Hausverbrauch, Netz, Wallbox und Wärmepumpe.
*   **🚗 Wallbox-Steuerung:** Manuelle und automatische Steuerung der E3DC Wallbox inkl. Ladeplanung.

---

## 📋 Voraussetzungen

Bevor du startest, stelle sicher, dass folgende Punkte erfüllt sind:

*   **Hardware:** Raspberry Pi (Empfohlen: Pi 4 oder Pi 5, läuft auch auf Pi Zero 2 W) mit SD-Karte oder SSD.
*   **Betriebssystem:** Raspberry Pi OS Lite (Bullseye oder neuer, 64-bit empfohlen).
*   **Netzwerk:** Der Pi muss im gleichen Netzwerk wie das E3DC Hauskraftwerk sein und Internetzugriff haben.
*   **Zugriff:** SSH-Zugriff auf den Pi.

*Hinweis: Ein Webserver (Apache/PHP) ist nicht zwingend vorinstalliert nötig, da der Installer diesen auf Wunsch automatisch einrichtet.*

---

## 🚀 Installation

Die Installation erfolgt bequem über die Kommandozeile.

### Schritt 1: System aktualisieren & Git installieren

Melde dich per SSH auf deinem Raspberry Pi an und führe folgende Befehle aus:

```bash
sudo apt update
sudo apt install -y git
```

### Schritt 2: Repository klonen

Lade den Installer herunter:

```bash
cd ~
git clone https://github.com/A9xxx/Install-E3DC-Control.git Install
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

## 🔄 Upgrade auf die neueste Version

Um von einer älteren Version auf die aktuelle Version (mit Systemd & neuem Menü) zu wechseln:

1.  **Installer aktualisieren:**
    Wähle im Menü **"Installation & Update"** -> **"Installer aktualisieren"** (oder `git pull` im Verzeichnis).
2.  **Service umstellen:**
    Gehe zu **"System & Dienste"** -> **"E3DC-Control Service einrichten (Systemd)"** (Punkt 11). Dies ersetzt den alten Crontab-Autostart durch einen stabilen Systemdienst.
3.  **Status prüfen:**
    Nutze **"System-Status anzeigen"** (Punkt 21), um sicherzustellen, dass alles läuft.

---

## �️ Wartung & Updates

Der Installer dient auch als Wartungstool. Starte ihn jederzeit erneut (`sudo python3 installer_main.py`), um Updates einzuspielen, Berechtigungen zu reparieren oder Backups zu verwalten.

Für automatisierte Abläufe (z.B. via Cronjob) gibt es den Headless-Modus: `sudo python3 installer_main.py --unattended`


---
