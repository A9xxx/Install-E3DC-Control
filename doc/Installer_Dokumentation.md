# Betrieb des E3DC-Control Installers

Dokumentation Stand: 5.4.3p

Der Installer ist der freigegebene Einstieg für Installation, Update,
Reparatur, Backup, Rollback und Deinstallation. Die vollständige Bedienung ist
in [E3DC-Control Installer](Installer.md) beschrieben.

Der Installer-Anteil von 5.4.3p erzeugt die vom administrativen
Download-Bootstrap neu aufgebaute `.git`-Fläche als den zuvor eindeutig
gebundenen Installationsbenutzer. Verifiziertes Backup, bestätigte Writer-Ruhe
und sämtliche Safety-Gates bleiben unverändert verpflichtend. EMS-Regelung und
Hardwareausgänge ändern sich nicht.

Der Installer-Anteil von 5.4.3o ergänzt den administrativen
`e3dc-update-bootstrap`. Er lädt den veröffentlichten Stable-Tag samt Commit
in einen root-eigenen Ziel-Checkout und verwendet den vorhandenen Alt-Updater
sowie dessen `.git`-Metadaten nicht als Autorität. Erst nach verifiziertem
Backup und bestätigter Writer-Ruhe werden bekannte Release-Dateien, Rechte und
Units normalisiert. Pfadflucht, Symlinks, Spezialdateien, zusätzliche
Hardlinks, konkurrierende Updates sowie fehlgeschlagene Backup- oder
Healthchecks bleiben harte Stopps.

Der Installer-Anteil von 5.4.3n korrigiert ausschließlich den pfadgenauen
Metadatenvertrag der privilegierten Restorequelle. Nur
`/etc/e3dc-control/instance_role.json` wird mit `root:www-data 0640`
akzeptiert; die private Backup-Payload bleibt `root:root 0600`. Alle anderen
privilegierten Pfade und abweichende Eigentümer, Gruppen, Modi, Links, ACLs,
Attribute oder Identitätsdrift bleiben streng fail-closed. EMS-Regelung,
HA-, Wallbox-, Wärme- und Direktvermarktungsentscheidungen sowie
Hardwareausgänge ändern sich gegenüber 5.4.3m nicht.

Der Installer-Anteil von 5.4.3m darf ausschließlich im vollständig
versiegelten, normalen vorwärtsgerichteten Ziel-Updater einen wirklich
fehlenden Rollenanker für den exakt gebundenen `off`-Einzelknoten ohne Peer
einmalig projizieren. Das geschieht erst nach Root-Receipt-gebundenem Backup,
abgeschlossener gegebenenfalls nötiger Storage-Manager-Unit-Promotion und
bestätigter Aktorruhe. Bootstrap, Reinstall und Rollback bleiben ausgeschlossen.
Ein vorhandener fremder oder widersprüchlicher Anker sowie HA- und
Shadow-Rollen werden weder umgedeutet noch automatisch repariert.

Der Installer-Anteil von 5.4.3l bindet den nativen Git-Rückweg an den
updater-eigenen Ausgangscommit, das root-eigene Transaktionsbackup und die
laufende Transaktion. Bei belegten, weiterhin vorhandenen Änderungen an
getrackten Dateien werden die gesicherten Bytes wiederhergestellt und die
Dateimodi auf den in `old_commit` belegten Git-Modus gehärtet; staged,
ungetrackte oder gelöschte Zustände und allgemeine manuelle
Restorepfade sind nicht neu abgedeckt.

Vor dem ersten Dienststopp darf ausschließlich eine exakt bekannte ältere
Familie der `e3dc-storage-manager.service` atomar auf den root-eigenen
Unit-Vertrag migriert werden. PiGuard im exakten Zustand
`activating/auto-restart` wird als zuvor laufend behandelt. Ein vom
Ziel-Updater synchron erkannter Recoveryfehler hinterlässt einen
transaktionsgebundenen Startschutz für PiGuard und die bekannten Writer; eine
allgemeine Zusage für Stromausfall, `SIGKILL` oder einen außerhalb dieses
Fehlerpfads beendeten Prozess folgt daraus nicht.

Der Installer-Anteil von 5.4.3k ergänzt den älteren nativen
`--target-updater-handoff`, der `E3DC_BOOTSTRAP_USER` vor dem root-eigenen
Ziel-Snapshot entfernt. Dieser Einstieg und der in 5.4.3j geschlossene
flaglose Snapshot binden den Installationsnutzer erst nach dem Root-Lock aus
demselben gültigen Nicht-Root-Eigentümer von Repository und `.git`. Nach der
Snapshot-Bindung werden Repository, `.git`, Nutzerkonto und Nutzerwert vor dem
ersten Import aus dem Zielcode erneut geprüft. Die Härtungen und
Abbruchgründe aus 5.4.3j bleiben unverändert.

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
