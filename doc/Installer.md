# 📘 Dokumentation: E3DC-Control Modular Installer

Dieses System ist als modulares Framework aufgebaut. Der Kern (`core.py`) erkennt beim Start automatisch alle Module im Ordner `Installer/` und fügt sie dynamisch dem Hauptmenü hinzu.

---

## 🛠 Wichtige Befehle auf dem Raspberry Pi

Da das Programm in Hintergrund-Sessions (`screen`) läuft, sind diese Befehle für die tägliche Kontrolle am wichtigsten:

| Befehl | Beschreibung |
| :--- | :--- |
| `screen -ls` | Listet alle aktiven Sessions auf (`E3DC`, `live-grabber`). |
| `screen -r E3DC` | Verbindet dich mit der E3DC-Steuerung (ansehen). Abbrechen mit `Strg+A, D`. |
| `screen -r live-grabber` | Schaut dem Live-Status-Grabber bei der Arbeit zu. |
| `tail -f Install/logs/install.log` | Verfolgt live alle Aktionen und Fehler des Installers. |
| `ls -la /var/www/html` | Prüft die Rechte im Webverzeichnis (sollte `pi:www-data` sein). |
| `sudo mount -v /var/www/html/ramdisk`| Prüft, ob die RAM-Disk korrekt gemountet ist. |

---

## 🗂 Modul-Referenz (Was macht was?)

### Kern-Dateien (Infrastruktur)
*   installer_main.py: Der Haupteinstiegspunkt. Prüft root-Rechte, initialisiert den Nutzer und startet das Menü.
*   Installer/core.py: Das "Gehirn" des Menüs. Registriert Befehle und sortiert sie.
*   Installer/utils.py: Zentrale Werkzeuge für Logging, Dateiänderungen und `apt`/`pip`-Installationen.
*   Installer/installer_config.py: Speichert Einstellungen (Nutzername, Pfade), damit diese nur einmal abgefragt werden müssen.

### Funktions-Module (Menüpunkte)
*   **Rechteverwaltung (permissions.py)**: Erzwingt konsistente Berechtigungen. Setzt `pi:www-data` als Besitzer und stellt sicher, dass PHP und der Webserver schreiben dürfen.
*   **System & Kompilierung (system.py)**: Installiert alle Linux-Pakete (Apache, PHP, Plotly) und übersetzt den C-Code von E3DC-Control in ein ausführbares Programm.
*   **Update-System (update.py)**: Vergleicht deine Version mit dem GitHub-Stand und aktualisiert nur das Programm, ohne deine Konfiguration zu überschreiben.
*   **Backup & Rollback (backup.py / rollback.py)**: Erstellt Schnappschüsse deiner gesamten Installation (Binary, Config, Web). Erlaubt das Zurückkehren zu jedem Git-Commit oder lokalen Backup.
*   **Automatisierung (screen_cron.py)**: Erstellt die Autostart-Einträge in der `crontab`, damit E3DC-Control nach einem Stromausfall von selbst startet.
*   **Webportal (diagrammphp.py)**: Richtet das PHP-Frontend und die täglichen Diagramm-Updates ein.
*   **RAM-Disk & Live-Status (ramdisk.py)**: Konfiguriert den SD-Karten-Schutz durch eine 1MB RAM-Disk und startet das Grabber-Skript für das Mobile-Dashboard.
*   **Konfigurations-Wizards (create_config.py / config_wizard.py)**: Interaktive Abfragen für alle Parameter der `e3dc.config.txt`.

---

## 📂 Wichtige Pfade & Dateien

*   **/home/pi/E3DC-Control/**: Das Hauptverzeichnis des Programms.
    *   `E3DC-Control`: Das kompilierte C-Programm.
    *   `e3dc.config.txt`: Deine Zugangsdaten und Einstellungen.
    *   `backups/`: Alle während Updates erstellten Sicherungen.
*   **/var/www/html/**: Das Webverzeichnis.
    *   `ramdisk/live.txt`: Der aktuelle Status (wird alle 4 Sek. überschrieben).
    *   `diagramm.html`: Das große Desktop-Diagramm.
    *   `index.php`: Die Hauptseite des Dashboards.
*   **/home/pi/Install/logs/install.log**: Deine Fehlerdiagnose-Zentrale.

---

## 💡 Troubleshooting (Fehlerbehebung)

1.  **Weboberfläche meldet Fehler beim Speichern?**
    Lasse den Installer Punkt 2 (**Rechte prüfen & korrigieren**) laufen. Dies heilt 99% aller Web-Probleme.

2.  **Live-Daten im Mobile-Dashboard stehen still?**
    Prüfe mit `screen -ls`, ob `live-grabber` läuft. Wenn nicht, Punkt 17 im Installer erneut ausführen.

3.  **Kompilierung schlägt fehl?**
    In `logs/install.log` nachsehen. Meistens fehlt ein Systempaket (`libcurl4-openssl-dev` o.ä.), welches über Punkt 3 nachinstalliert werden kann.

---

## 📱 PWA-Installation (Dashboard als App)

Du kannst das Dashboard als **Progressive Web App (PWA)** auf deinem Handy installieren. Dies ermöglicht eine Vollbild-Ansicht ohne Browser-Leisten.

### Voraussetzungen
*   **HTTPS ist zwingend erforderlich**: PWAs funktionieren aus Sicherheitsgründen nur über eine verschlüsselte Verbindung.
*   Ein externer Zugang via **HTTPS-Tunnel** (z.B. Cloudflare Tunnel, MyFritz mit Let's Encrypt oder Reverse Proxy).

### Installationsschritte
1.  Öffne dein Dashboard über die externe **HTTPS-URL** in deinem mobilen Browser (Chrome für Android, Safari für iOS).
2.  **Android (Chrome)**: Klicke auf die drei Punkte oben rechts und wähle **"App installieren"** oder **"Zum Startbildschirm hinzufügen"**.
3.  **iOS (Safari)**: Klicke auf das **Teilen-Symbol** (Quadrat mit Pfeil nach oben) und wähle **"Zum Home-Bildschirm"**.
4.  Das E3DC-Logo erscheint nun als Icon auf deinem Home-Bildschirm und öffnet sich wie eine native App (ohne Adresszeile).

---
*Dokumentation Stand: Februar 2026*

