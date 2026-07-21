# Watchdog und Dienstueberwachung

E3DC-Control V4 läuft über systemd-Dienste. `screen`-Sessions sind nur noch
Legacy und werden nicht für den normalen Betrieb verwendet.

## Wichtige Dienste

```text
e3dc-live
e3dc-storage-simulator
e3dc-storage-manager
e3dc-wallbox-manager
e3dc-mqtt-hub
e3dc-epex-manager
energy_manager
apache2
```

Optionale Dienste wie Heizstab, Luxtronik, IDM, Bluelink, Matter oder
Benachrichtigungen sind nur aktiv, wenn das jeweilige Modul installiert ist.

## Status prüfen

```bash
systemctl is-active e3dc-live e3dc-storage-manager e3dc-wallbox-manager apache2
systemctl status e3dc-live --no-pager
journalctl -u e3dc-live -n 80 --no-pager
```

## Neustart

```bash
sudo systemctl restart e3dc-live
sudo systemctl restart e3dc-storage-manager
sudo systemctl restart e3dc-wallbox-manager
sudo systemctl restart e3dc-mqtt-hub
sudo systemctl restart apache2
```

## Installer-Reparatur

Wenn Dienste fehlen, falsche Rechte haben oder Web-Aktionen keine sudo-Freigabe
haben:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

## Startlimit

Die systemd-Units setzen `StartLimitIntervalSec` und `StartLimitBurst` im
`[Unit]`-Abschnitt. Wenn ein Dienst nach mehreren Fehlerstarts blockiert ist:

```bash
sudo systemctl reset-failed e3dc-live
sudo systemctl restart e3dc-live
journalctl -u e3dc-live -n 120 --no-pager
```

Erst die Ursache im Journal prüfen, dann dauerhaft neu starten. Wiederholte
Fehlerstarts können auf fehlende Konfiguration, Rechteprobleme oder eine
ungültige Live-/Ramdisk-Datei hinweisen.

## Web-Diagnose

Die Installationszentrale kann Diagnosepakete erzeugen. Diese enthalten
maskierte Konfiguration, Dienststatus, ausgewaehlte Logs und Ramdisk-Dateien.
Vor dem Teilen immer kurz prüfen, ob keine privaten Zusatzdateien enthalten
sind.
