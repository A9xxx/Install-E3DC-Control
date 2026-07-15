# Deinstallation von E3DC-Control

Dieses Dokument beschreibt, wie E3DC-Control und die zugehörigen Dienste
sauber entfernt werden. Die Deinstallation löscht keine bewusst angelegten
Backups oder exportierten Diagnosepakete, sofern sie außerhalb der
Installationspfade liegen.

## Ausführen

```bash
cd ~/Install
sudo python3 installer_main.py
```

Wähle im Hauptmenü **`9) Deinstallation`**. Für einzelne
optionale Module kann die Installationszentrale im Webportal genutzt werden.
Core-Module werden dort aus Sicherheitsgründen nicht frei gelöscht.

## Was entfernt wird

Das Deinstallationsskript stoppt und deaktiviert unter anderem diese Dienste,
sofern sie vorhanden sind:

* `e3dc-live`
* `e3dc-storage-manager`
* `e3dc-storage-simulator`
* `e3dc-wallbox-manager`
* `e3dc-mqtt-hub`
* `e3dc-epex-manager`
* `energy_manager`
* `e3dc-websocket`
* optionale Dienste wie Heizstab, Luxtronik, IDM, Bluelink, Matter oder
  Benachrichtigungen

Zusätzlich werden passende systemd-Unit-Dateien, Installer-Cronjobs,
Web-sudoers-Freigaben und Webdateien unter `/var/www/html` entfernt bzw.
bereinigt.

## Was manuell zu prüfen ist

* `~/Install` enthält Repository, Logs, lokale Backups und Diagnoseausgaben.
* `/var/www/html/data` enthält Konfiguration und Langzeitdaten.
* Docker-Installationen haben eigene Volumes, z.B. `./data` und `./logs` neben
  der `docker-compose.yml`.

Wenn diese Daten dauerhaft gelöscht werden sollen, prüfe sie vorher manuell.
Konfigurationen, Tokens, Diagnose-Zips und Historien sollten nicht versehentlich
in fremde Systeme kopiert oder auf GitHub gepusht werden.
