# Python Virtual Environment (venv)

E3DC-Control nutzt ein Python Virtual Environment, damit Abhaengigkeiten wie
`paho-mqtt`, `requests`, `websockets`, `pymodbus` oder `cryptography` nicht mit
dem Betriebssystem kollidieren. Das ist besonders wichtig auf Raspberry Pi OS
Bookworm, weil globale Pip-Installationen dort durch PEP 668 geschuetzt sind.

## Funktionsweise

Der Installer erstellt die Umgebung im Installationsverzeichnis `~/Install`.

* Standard-Name: `.venv_e3dc`
* Installer-Konfiguration: `~/Install/Installer/installer_config.json`
* Web-Pfadinfo: `/var/www/html/e3dc_paths.json`

Systemd-Units werden so erzeugt, dass sie die passende Python-Umgebung nutzen.
Man muss das venv im normalen Dienstbetrieb nicht manuell aktivieren.

## Manuelle Nutzung

Wenn du Skripte direkt testen willst:

```bash
cd ~/Install
source .venv_e3dc/bin/activate
python3 Installer/e3dc_mqtt_hub.py --help
pip list
deactivate
```

Alternativ kann der Python-Interpreter direkt aufgerufen werden:

```bash
~/Install/.venv_e3dc/bin/python Installer/storage_simulator.py --once
```

## Reparatur

Wenn Pakete fehlen oder ein Update abbricht:

```bash
cd ~/Install
sudo python3 installer_main.py --fix-permissions
sudo python3 installer_main.py
```

Im Installer kann die Python-Umgebung über das Expertenmenü neu aufgebaut
werden:

```text
7) Expertenmenü
21) Python venv neu aufbauen (Reparatur)
```

Für Snapshot-/VM-Vorbereitung oder eine komplette Paket-/venv-Reparatur gibt es
zusätzlich den Hauptmenüpunkt:

```text
8) Systempakete vorbereiten
```

Bestehende Konfigurationen unter `/var/www/html/data` bleiben davon unberuehrt.
