# Betrieb des E3DC-Control Installers

Dokumentation Stand: 5.4.3j

Der Installer ist der freigegebene Einstieg für Installation, Update,
Reparatur, Backup, Rollback und Deinstallation. Die vollständige Bedienung ist
in [E3DC-Control Installer](Installer.md) beschrieben.

Der Installer-Anteil von 5.4.3j ergänzt ausschließlich den gebundenen
Altübergang eines flaglosen, root-eigenen 5.4.2d-Ziel-Snapshots. Fehlt dort die
vom alten Aufrufer entfernte
Variable `E3DC_BOOTSTRAP_USER`, müssen Repository und `.git` nach dem Root-Lock
demselben gültigen lokalen Nicht-Root-Nutzer gehören. Die Bindung wird direkt
vor dem Finalizer erneut geprüft und die Aufruferumgebung danach
wiederhergestellt. Ein bereits gesetzter Nutzerwert wird nicht ersetzt, muss
aber exakt dem gebundenen Repository-Eigentümer entsprechen. Root, `www-data`,
fremde oder unterschiedliche Eigentümer und ein abweichender Nutzerwert bleiben
harte Abbruchgründe.

5.4.3i richtet einen argumentlosen, root-eigenen Web-Update-Launcher ein. Er
bindet Installationspfad, Installationsnutzer und veröffentlichten Ausgangstag;
freie Aktionen und Zielparameter bleiben gesperrt. Der erste Wechsel von
5.4.3f auf 5.4.3i erfolgt noch über die administrative Konsole.
Ältere 5.4.2-Zielübergänge binden den lokalen Installationsnutzer zusätzlich
aus der kanonischen Repository-Eigentümerstruktur und prüfen diese unmittelbar
vor dem versiegelten Kindstart erneut.

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
- Dienste starten erst nach erfolgreicher Migration und Rollenprüfung; ihr
  Enable-/Aktiv-Endzustand wird getrennt von den Zwischen-Rückgabecodes
  verifiziert.
- Fehlende optionale Units sind beim Maskenrücklauf ein legitimer Zustand;
  echte Unit-, Masken- oder Wiederherstellungsabweichungen bleiben blockierend.
- Die lokale HTTP-Prüfung bleibt Bestandteil des erfolgreichen Abschlusses.
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
