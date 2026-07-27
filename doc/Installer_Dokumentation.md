# Betrieb des E3DC-Control Installers

Dokumentation Stand: 5.4.2b

Der Installer ist der freigegebene Einstieg für Installation, Update,
Reparatur, Backup, Rollback und Deinstallation. Die vollständige Bedienung ist
in [E3DC-Control Installer](Installer.md) beschrieben.

## Start

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
bash "$E3DC_INSTALL_PATH/e3dc-setup"
```

Für automatisierte Vorprüfungen und Rechte-Reparatur:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
```

## Betriebsvertrag

- Der Installationskontext muss eindeutig und lesbar sein.
- Vor Update und Rollback ist ein externes, manifestiertes und prüfbares Backup Pflicht.
- Writer-/Aktor-Dienste werden vor einem Programmbaumwechsel nachweislich gestoppt.
- Ziel-Tag und vollständiger Ziel-SHA müssen zusammenpassen.
- Webdateien werden nur über geprüfte Positiv- und Löschlisten synchronisiert.
- Dienste starten erst nach erfolgreicher Migration, Rollenprüfung und lokaler HTTP-Prüfung.
- Bei unvollständigem Rollback bleiben Writer gestoppt.
- Nur eine echte Erstinstallation erhält beim erstmaligen Erzeugen der
  Konfiguration die Einzelanlagenrolle `off`. Bestehende Konfigurationen ohne
  gültige HA-/Shadow-Rolle bleiben fail-closed.
- Jeder Installationsschritt liefert seinen tatsächlichen Erfolg bis zum
  Menü- und Prozess-Exitcode weiter; ein `False`-Ergebnis wird nicht als
  abgeschlossene Aktion protokolliert.

Systemd-Units werden durch den Servicekatalog und die jeweils ausgelieferten
Modulinstaller transaktional verwaltet. Nicht vorhandene Hilfsskripte oder
manuelle Unit-Vorlagen sind nicht erforderlich.
