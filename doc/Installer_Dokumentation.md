# Betrieb des E3DC-Control Installers

Dokumentation Stand: 5.4.4h

Der Installer ist der freigegebene Einstieg für Installation, Update,
Reparatur, Backup, Rollback und Deinstallation. Die vollständige Bedienung ist
in [E3DC-Control Installer](Installer.md) beschrieben.

Für heterogene Altinstallationen wird genau eine Datei auf den Raspberry Pi
kopiert. Der Aufruf `sudo /bin/sh ./e3dc-update-bootstrap` erkennt
Installationsordner, Installationsbenutzer und Rolle selbst und startet den
gemeinsamen systemd-Hintergrundauftrag. Nach diesem einmaligen Übergang starten
Dashboard, Konsole und Installer-Menü denselben Dispatcher. Die automatische
Updateprüfung verwendet dieselbe Stable-Quelle, informiert aber nur über einen
neuen Stand.

5.4.4h bestätigt den Hintergrundauftrag über dieselbe aktive systemd-Unit,
MainPID, Prozess und root-kontrollierten Status auf beiden Seiten. Vor dieser
Bestätigung werden weder Ziel-Updater noch Produktpfad freigegeben. Der
eigenständige Community-Bootstrap darf das private Zielrelease vorher sicher
binden, führt es aber noch nicht aus. Frühfehler
bleiben im Dateiprotokoll erhalten. Geht der Prozess erst nach bestätigtem
Ausführungsbeginn verloren, fordert die Oberfläche die Zustands- und
Rückfallprüfung und behauptet keinen unveränderten Anlagenzustand.

Der enthaltene Installer-Anteil von 5.4.4g lässt den heruntergeladenen Ziel-Updater
Vollbackup, kurze ruhende Daten-Nachsicherung, Releaseprojektion, Reparatur
bekannter Rechte und Dienstneustart durchführen. Das Nutzer-`.git`, lokale
Produktänderungen, fehlende Produktdateien und historische Rechte sind keine
vorgelagerten Startbedingungen. Platzmangel, schreibgeschütztes Dateisystem,
fehlende Rechte oder mehrere gleichrangige Installationen werden mit
betroffenem Pfad beziehungsweise Kandidatenliste und einem konkreten nächsten
Befehl ausgegeben. Die Web-Rechte-Reparatur verwendet denselben root-eigenen
Backup-/Updateauftrag. Alle sichtbaren zustandsändernden
Webaktionen werten Anmeldung, CSRF, HTTP-Ergebnis, Antwortinhalt und Teilfehler
aus, bevor sie Erfolg anzeigen. Für private Modus-5-Daten gilt `02770`; der
ausdrücklich gewählte Kompatibilitätsmodus `02775` bleibt zulässig.

Für eine beibehaltene `external_pv_topology.json` stellt 5.4.4g den
Installationsnutzer, die Webgruppe und den gemeinsamen Lesemodus `0664`
wieder her. Die übrigen Konfigurations- und Geheimnisdateien bleiben bei ihren
strengeren Rechteverträgen.

Der private Release-Checkout bleibt root-eigen und auf `0700`/`0600`
begrenzt. Der betriebene Produktbaum erhält beim Cutover dagegen definierte
Live-Rechte: Verzeichnisse `0755`, normale Dateien `0644` und echte
Startskripte mit Interpreterzeile `0755`, jeweils als Installationsnutzer und
Webgruppe. Vor dem Dienststart werden PHP-Pfadauflösung und Installer-Importkette in einer
bereinigten Umgebung real als `www-data` geprüft. Die separate
Service-Pythonumgebung bleibt privat; ein Fehler aktiviert den bestehenden
Rücklauf.

Bei Installationen unter einem privaten Home-Pfad bindet der Zielcode
owner-private, nicht-setgid Vorfahren eng an die Webgruppe und ergänzt nur
deren Traversierrecht. Eine vorhandene gezielte POSIX-ACL für `www-data` wird
akzeptiert. Gemeinsam genutzte oder setgid Vorfahren werden ohne diese ACL vor
der Produktmutation mit einer konkreten Anleitung gesperrt. Globales
`Other-Execute`, Auflisten, Lesen und Schreiben bleiben gesperrt. Direkte und
verschachtelte Home-Pfade sowie `/opt` bleiben gültige Installationsvarianten.

Der Zielbestand beträgt maximal drei automatisch verwaltete
System-Backup-Familien und separat maximal drei Web-Installer-Sicherungen.
Schutzbindungen dürfen die Zielgrenzen vorübergehend überschreiten und werden
nicht erzwungen gelöscht. Die offene Grenze bleibt sichtbar und wird bei der
nächsten sicheren Backup-Bereinigung erneut angewendet.

Die exakt bestätigte RAM-Disk unter `/var/www/html/ramdisk` bleibt beim
Dateiaustausch erhalten. Nach einem Rücklauf werden ihre und die übrigen
bekannten Webrechte vor jedem Dienstneustart wiederhergestellt. Master und
Slave übergeben aktivierte Regelungsdienste an HA; Shadow und zuvor aktive,
aber deaktivierte Dienste werden direkt gestartet.

Der Installer-Anteil von 5.4.4c verwendet den heruntergeladenen Ziel-Updater
direkt. Das `.git`-Verzeichnis des Nutzers, lokale Produktänderungen, fehlende
Dateien und frühere Rechte entscheiden nicht über die Annahme des Updates. Das
Vollbackup wird bei laufenden Diensten erstellt; nach genau einem kurzen
Dienststopp folgt die Nachsicherung der nun ruhenden veränderlichen Daten.
Danach werden Dateien, Rechte, Core-Units und Launcher auf den Releasezustand
gebracht und die benötigten Dienste neu gestartet. Der alte Release-Finalizer,
die Bedingung eines Ziel-Updaters auf demselben Produkt-Dateisystem und der
persistente Recovery-Bootblock gehören nicht zum Normalpfad. Derselbe
Stable-Stand darf zur Reparatur erneut installiert werden; der Versionscheck
bleibt eine reine Anzeige. Für einen defekten 5.4.4b-Launcher kann einmalig der
portable `e3dc-update-bootstrap` nötig sein. Regelung und Hardwareausgänge
bleiben gegenüber 5.4.4b unverändert.

Der Installer-Anteil von 5.4.4b bindet eine laufende Einzelinstanz aus ihrem
systemd-Dienst und verwendet den aktuellen Ziel-Updater für Inventar, Backup,
Dateiprojektion und Recovery. Der Installationsbenutzer und das Verzeichnis
sind nicht fest codiert. Lokale Änderungen, fehlende Release-Dateien, alte
Rechte und beschädigte Git-Metadaten werden vor dem Ersetzen gesichert, statt
den Auftrag vorgelagert zu blockieren. Download, Vorprüfung und vollständiges
Backup erfolgen vor dem kurzen Writer-Stopp; danach folgen atomare Projektion,
Dienststart und Healthcheck. Mehrere gleichrangige Instanzen werden angezeigt
und nicht geraten. Ein kontrollierter Abbruch nennt Ursache, Systemzustand und
den nächsten sicheren Befehl. Nur exakt als abgeschlossen belegte
5.4.4-/5.4.4a-Recoveryreste dürfen automatisch bereinigt werden.

Der Installer-Anteil von 5.4.4a verwendet den vorhandenen Alt-Updater, lokale
Git-Metadaten, getrackte Änderungen und historische Dateimodi nicht als
vorgelagertes Gate. Der gestartete veröffentlichte Updatepfad bindet
Installationsroot, Installationsbenutzer und Anlagenrolle eindeutig; mehrere
gleichrangige Kandidaten oder widersprüchliche Rollen stoppen mit Diagnose.
Alte Installer- und
Web-Pfadmetadaten werden auf den erkannten Bestand normalisiert und vom
Zielprozess ohne Bootstrap-Umgebung erneut geprüft. Danach gilt weiterhin der
vollständige Ablauf: Backup erstellen und prüfen, Writer und Dienste stoppen,
Dateien und Rechte projizieren, Dienste starten und Gesundheit prüfen. EMS-,
Direktvermarktungs-, Wallbox- und Hardwarelogik entsprechen unverändert 5.4.4.

Der Installer-Anteil von 5.4.4 konsolidiert Web- und Download-Update für
heterogene Altanlagen. Der veröffentlichte Ziel-Updater läuft aus einem
root-eigenen Ausführungssnapshot, bleibt aber eindeutig an den tatsächlichen
Installationsroot gebunden. Nach erstelltem und geprüftem Backup sowie
stillgelegten Diensten werden Produktdateien, Rechte, ein eng bekannter
redundanter Storage-Override und die benötigte Laufzeitumgebung auf den
Zielstand gebracht. Anschließend werden die Dienste gestartet und geprüft;
unsichere Pfade, nicht eindeutig gebundene Systemdateien und konkurrierende
Writer bleiben gesperrt.

Der Installer-Anteil von 5.4.3s legt im administrativen
Root-Download-Bootstrap einen wirklich fehlenden kanonischen Backup-Root
sicher an. Danach bleibt der Ablauf unverändert: Backup erstellen und prüfen,
Dienste stoppen, Dateien und Rechte aktualisieren, Dienste starten und ihren
Zustand prüfen. EMS-, Direktvermarktungs-, Wallbox- und Hardwarelogik ändern
sich nicht.

Der Installer-Anteil von 5.4.3r darf beim ausdrücklich mit Rolle und Peer
gebundenen Download-Bootstrap einen wirklich fehlenden HA-Rollenanker aus
dieser bereits bestehenden Bindung erzeugen. Das geschieht erst nach
verifiziertem Backup und bestätigter Writer-Ruhe. EMS-Regelung und
Hardwareausgänge ändern sich nicht.

Der Installer-Anteil von 5.4.3q verwendet im Finalizer des administrativen
Download-Bootstraps den absoluten Pfad `/usr/sbin/visudo` und ist damit nicht
von einem verkürzten Root-`PATH` abhängig. EMS-Regelung und Hardwareausgänge
ändern sich nicht.

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
