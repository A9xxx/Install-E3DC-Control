# Dokumentation: E3DC-Control Modular Installer

Der Installer ist das zentrale Werkzeug für Bare-Metal-Installationen auf
Raspberry Pi OS oder einem vergleichbaren Debian-System. Er richtet die
Python-Dienste, das Webportal, die Ramdisk, Rechte, optionale Module und
Updates ein.

## Wichtige Befehle auf dem Raspberry Pi

| Befehl | Beschreibung |
| :--- | :--- |
| `cd ~/Install` | Standard-Installationsverzeichnis. |
| `sudo python3 installer_main.py` | Interaktives Installer-Menü starten. |
| `sudo python3 installer_main.py --check` | Non-interaktiver Preflight für Web-Update und Rechte. |
| `sudo python3 installer_main.py --fix-permissions` | Dateirechte, sudoers und Web-Schreibrechte reparieren. |
| `sudo python3 installer_main.py --update-e3dc` | Projekt aktualisieren und Webdateien synchronisieren. |
| `tail -f ~/Install/logs/install.log` | Installer-Log verfolgen. |
| `systemctl status e3dc-live e3dc-storage-manager e3dc-wallbox-manager apache2` | Wichtige Dienste prüfen. |

`screen`-Sessions und der alte Dienst `e3dc.service` gehören nur noch zur
Legacy-Migration. Normale Installationen laufen über systemd-Dienste.

## Konsolenmenü

Das Konsolenmenü ist bewusst flach gehalten. Es gibt keine Untermenüs mehr,
außer dem sauber sortierten Expertenmenü für Erweiterungen und Spezialfälle.

| Menüpunkt | Zweck |
| :--- | :--- |
| `1) Installation / Update` | Erstinstallation oder Update inklusive Paketen, Webdateien, Rechten und Diensten. |
| `2) Systemstatus anzeigen` | Read-only Statusübersicht für Dienste, Pfade, Konfiguration und Diagnose. |
| `3) Rechte prüfen & korrigieren` | Repariert Besitzer, Gruppen, sudoers-Freigaben, Webrechte und Ramdisk. |
| `4) Notfallmodus / System reparieren` | Gebündelte Reparatur für beschädigte Installationen. |
| `5) Rollback auf Git-Stand` | Rücksprung auf einen bekannten Git-Stand. |
| `6) Backup erstellen / verwalten` | Lokale Sicherungen erstellen, prüfen oder zurückspielen. |
| `7) Expertenmenü` | Docker, Energy Manager, native Wallbox, MQTT, Home Assistant, Bluelink und weitere Spezialmodule. |
| `8) Systempakete vorbereiten` | Installiert Systempakete und Python-Abhängigkeiten, repariert das venv und beendet danach für Snapshots. |
| `9) Deinstallation` | E3DC-Control kontrolliert entfernen. |

Alte Modulnummern wie `11` werden aus Kompatibilitätsgründen weiterhin
akzeptiert, sind aber nicht mehr der empfohlene Einstieg. Eine frühere Option
`18 Alles installieren` wird nicht mehr im Hauptmenü angeboten; die normale
Installation läuft über `1) Installation / Update`.

Das Expertenmenü ist in feste 10er-Blöcke sortiert:

| Block | Beispiele |
| :--- | :--- |
| `11-19 Kernsystem & Update` | `14) Rollback (Datei-Backup)`, `15) Watchdog & Telegram konfigurieren` |
| `21-29 Umgebung & Python` | `21) Python venv neu aufbauen`, `22) Python venv Namen ändern` |
| `31-39 Docker Migration & Verwaltung` | `31) Zu Docker wechseln`, `32) Docker auflösen` |
| `41-49 Erweiterungen & Smart Home` | `41) Energy Manager`, native Wallbox, MQTT, Bluelink und Matter |

## Modul-Referenz

### Kern-Dateien

* `installer_main.py`: Haupteinstieg, root-Prüfung und Menü.
* `Installer/core.py`: registriert Menüpunkte und Modulaktionen.
* `Installer/utils.py`: gemeinsame Werkzeuge für apt/pip, systemd, Logging,
  Pfade, Rechte und Service-Dateien.
* `Installer/installer_config.py`: speichert lokale Installer-Pfade und
  Benutzerauswahl.

### Wichtige Module

* `permissions.py`: setzt Web-, Daten- und Ramdisk-Rechte und aktualisiert die
  erlaubten sudoers-Kommandos für die WebUI.
* `update.py`: aktualisiert den Code aus GitHub, erstellt Backups und kopiert
  Webdateien nach `/var/www/html`.
* `backup.py` / `rollback.py`: sichern und restaurieren lokale Stände.
* `ramdisk.py`: richtet `/var/www/html/ramdisk` ein.
* `config_wizard.py` und Web-Config-Editor: schreiben die zentrale
  Konfiguration `data/e3dc_v4.json`.

## Wichtige Pfade

* `~/Install`: Repository und Installer.
* `~/Install/data/e3dc_v4.json`: lokale Kopie der zentralen Konfiguration.
* `/var/www/html`: Live-Webportal.
* `/var/www/html/data/e3dc_v4.json`: kanonische Web-Konfiguration.
* `/var/www/html/ramdisk`: flüchtige Live-, Plan- und Statusdateien.
* `~/Install/logs/install.log`: Installer-Log.

## Troubleshooting

### Weboberfläche kann nicht speichern

```bash
cd ~/Install
sudo python3 installer_main.py --fix-permissions
sudo systemctl restart apache2
```

### Live-Daten stehen still

```bash
systemctl status e3dc-live
journalctl -u e3dc-live -n 80 --no-pager
```

### Web-Update meldet fehlende sudo-Rechte

```bash
cd ~/Install
sudo python3 installer_main.py --fix-permissions
sudo python3 installer_main.py --check
```

### PHP wird als Quelltext angezeigt

Ab 4.9.8i installiert der Installer `libapache2-mod-php`, aktiviert
`mpm_prefork` und prüft Apache/PHP im Preflight. Bei Altinstallationen:

```bash
cd ~/Install
sudo python3 installer_main.py --fix-permissions
sudo python3 installer_main.py --update-e3dc
sudo systemctl restart apache2
```

## PWA-Installation

Das Dashboard kann als Progressive Web App auf Smartphone oder Tablet
installiert werden. Für externe Zugriffe ist HTTPS nötig, z.B. über einen
Reverse Proxy oder Cloudflare Tunnel.

* Android/Chrome: Menü -> "App installieren" oder "Zum Startbildschirm".
* iOS/Safari: Teilen-Symbol -> "Zum Home-Bildschirm".

*Dokumentation Stand: 5.3.2b / Juli 2026*
