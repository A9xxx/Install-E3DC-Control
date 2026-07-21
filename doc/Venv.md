# Python-Umgebung (venv)

E3DC-Control nutzt eine installationsbezogene Python-Umgebung. Der Installer
ermittelt ihren Namen und Pfad aus dem geprüften Installationskontext; ein
fester Benutzer- oder Home-Pfad wird nicht vorausgesetzt.

## Betrieb

- Systemd-Units verwenden automatisch den zur Installation gehörenden Interpreter.
- Die Weboberfläche liest den validierten Pfad aus `/var/www/html/e3dc_paths.json`.
- Die Umgebung muss im normalen Dienstbetrieb nicht manuell aktiviert werden.
- Modell-, Matter- und Betriebsdaten liegen nicht im flüchtigen Python-Paketverzeichnis.

## Reparatur

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Im Installer kann die Python-Umgebung über das Expertenmenü neu aufgebaut
werden. Schlägt die Paketinstallation fehl, werden Dienste nicht mit einer
unvollständigen Umgebung gestartet.
