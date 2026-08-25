# Update-Prozess

Updates werden ausschließlich über den Installer ausgeführt. Ein manuelles
`git pull` ist kein unterstützter Updateweg. Das `.git`-Verzeichnis einer
Nutzerinstallation ist für den regulären Ziel-Updater weder Voraussetzung noch
Updateautorität.

Der aktuelle Stable-Stand ist `v5.4.4d`. Das Dashboard startet ausschließlich
den argumentlosen, root-eigenen Systemjob. Dieser installiert den neuesten
veröffentlichten Stable-Stand oder repariert dieselbe Version. Der
Stable-Versionscheck ist nur eine Anzeige und keine Startfreigabe. Freie Pfade,
Release-Tags, Neuinstallationen und Rückfälle bleiben im Web gesperrt.

## Normales Update

Der bevorzugte Weg ist der Update-Button der Weboberfläche. Weboberfläche,
Installer-Menü und Konsole starten denselben root-eigenen Hintergrundauftrag.
Der lokale Git-, Rechte- oder Änderungszustand entscheidet nicht darüber, ob
dieser Auftrag angenommen wird. Der Ziel-Updater erstellt das Vollbackup,
sichert nach dem kurzen Dienststopp die nun ruhenden veränderlichen Daten nach,
tauscht Dateien, Rechte, Core-Units und Launcher aus und startet die benötigten
Dienste neu. Die unterschiedlichen Web-Launcher aus 5.4.4a, 5.4.4b und 5.4.4c
werden direkt unterstützt: Fehlen spätere Übergabeparameter, lädt der
commitgebundene Ziel-Bootstrap den vollständigen Releasebaum selbst und prüft
Tag und Commit erneut.

Nach einmaliger Installation des gemeinsamen Dispatchers kann der Auftrag auch
direkt auf der Konsole gestartet werden:

```bash
sudo /usr/local/sbin/e3dc-web-update-launcher
```

Der Befehl kehrt nach Annahme des Auftrags zurück. Status und Protokoll laufen
unabhängig vom SSH-Fenster weiter:

```bash
systemctl status --no-pager e3dc-web-update.service
journalctl -fu e3dc-web-update.service
```

### Rettungsweg für heterogene Altinstallationen

Für heterogene Altinstallationen wird genau **eine Datei** benötigt:
`e3dc-update-bootstrap`. Sie kann über den Community-Beitrag oder aus dem
offiziellen Repository heruntergeladen und anschließend an einen beliebigen Ort
auf den Raspberry Pi kopiert werden. Im Verzeichnis der Datei genügt:

```bash
sudo /bin/sh ./e3dc-update-bootstrap
```

Ein `chmod`, ein fester Installationspfad und weitere Argumente sind nicht
nötig. Der Aufruf startet den veröffentlichten Updatepfad in einem
root-kontrollierten Hintergrundauftrag. Dieser ermittelt Installationsordner,
Installationsbenutzer und Anlagenrolle aus dem laufenden System. Werden auf
einer Anlage in derselben maßgeblichen Evidenzstufe mehrere Installationen
gefunden, listet der Auftrag die Kandidaten auf und stoppt, statt eine davon zu
raten.

Der Start gibt die Befehle für Status und Protokoll aus; das Terminal kann
danach geschlossen werden. Die Community-Datei wird zuerst kopiert und dann
mit `sudo /bin/sh` gestartet; sie wird nicht über eine Pipe direkt aus dem
Netz als Root ausgeführt.

Der kleine Bootstrap ermittelt den aktuellen veröffentlichten Stable-Tag samt
Commit, lädt dessen Installer und führt **nicht** den vorhandenen Alt-Updater
aus. Ein sicherer kanonischer Rollenanker hat Vorrang; nur wenn er fehlt, wird
eine eindeutige vorhandene Konfiguration als Fallback verwendet. Ein
widersprüchlicher Rollen- oder Installationsbestand stoppt mit Diagnose.

Die vorhandene `.git`-Fläche ist dabei keine Eingangsbedingung. Der Ziel-Updater
liest weder ihre Rechte noch ihren Index oder ihre lokale Historie als
Updateautorität und muss sie für den Releasewechsel nicht neu aufbauen.
Gesichert werden die tatsächlich betriebenen Produkt-, Konfigurations- und
Betriebsdateien, nicht ein möglicherweise beschädigter Git-Zwischenspeicher.

Der Ziel-Updater erstellt zuerst das Vollbackup bei laufenden Diensten. Danach
stoppt er die betroffenen Dienste genau einmal kurz, sichert die ruhenden
veränderlichen Daten nach, projiziert Release-Dateien, Rechte, Core-Units und
Launcher und startet die Dienste neu. Abweichende Besitzer oder Modi bekannter
Produktdateien sind zu reparierender Altbestand und kein eigener Abbruchgrund.
Eine mehrdeutige laufende Installation, ein fehlgeschlagenes Backup, ein nicht
verfügbarer Release, fehlende Root- oder Schreibrechte und ein nicht startender
Pflichtdienst bleiben klare Stopps mit konkreter Lösungsausgabe.

Ein vorhandener Altregler wird vor dem Dateiaustausch nur gestoppt. Erst nach
dem bestätigten Start des neuen, zur Anlagenrolle passenden Dienstsatzes wird
er deaktiviert. Scheitert der Wechsel nach begonnener Produktmutation, stellt
der Ziel-Updater zuerst das Vollbackup und danach die neuere ruhende
Daten-Nachsicherung wieder her; erst anschließend startet er den exakt zuvor
aktiven Dienstsatz. Ein bereits bestätigter Zielstand wird bei einem
späteren Bereinigungsfehler nicht zurückgesetzt. Bei HA startet ausschließlich
der HA-Manager die verwalteten
Dienste. Deren lokales systemd-Starttor akzeptiert im HA-Betrieb nur eine
gültige Owner-Lease. Standalone-Dienste erhalten weder dieses Starttor noch
eine Abhängigkeit vom HA-Dienst. Im Shadow-Betrieb bleiben lokale Regel- und Watchdog-Dienste
gestoppt.

Ein vorhandener alter `wp-manager.service` wird beim Wechsel vollständig auf
`energy_manager.service` übertragen: War die alte Unit aktiv, wird die neue
Unit projiziert, gestartet und als Pflichtdienst geprüft. War sie nur
aktiviert, wird die neue Unit ebenfalls nur aktiviert. Erst nach dem
bestätigten Zielstand wird die alte Unit deaktiviert.

**Übergang aus 5.4.4a bis 5.4.4c:** Die drei veröffentlichten Web-Launcher
reichen unterschiedliche Teilverträge weiter. 5.4.4d erkennt diese gebundenen,
root-privaten Einstiege und vervollständigt den Zielrelease selbst. Der
Ein-Datei-Befehl bleibt für noch ältere, beschädigte oder gar nicht mehr
startbare Web-Launcher der universelle Rettungsweg; für den normalen Übergang
aus 5.4.4a bis 5.4.4c ist er nicht mehr erforderlich.

### Übergang aus älteren 5.4.2-Beständen

5.4.3i bindet den lokalen Installationsnutzer im Legacy-Zielübergang aus der
kanonischen Repository-Eigentümerstruktur, prüft Nutzer und Repository
unmittelbar vor dem Kindstart erneut und reicht die Bindung ausdrücklich an den
versiegelten Ziel-Finalizer weiter. Die lokale `installer_config.json` ist
bewusst kein Bestandteil dieses unveränderlichen Snapshots und muss dort nicht
vorhanden sein. Root, `www-data`, fremde Konten und ein ausgetauschtes
Repository bleiben harte Abbruchgründe.

5.4.3j schließt zusätzlich exakt den flaglosen, root-eigenen Ziel-Snapshot des
alten 5.4.2d-Aufrufers. Dieser Aufrufer entfernt `E3DC_BOOTSTRAP_USER`, bevor
der Ziel-Finalizer startet. Fehlt die Variable in diesem eng gebundenen
Altübergang, darf der Zielcode den Installationsnutzer erst nach dem Root-Lock
aus dem übereinstimmenden Eigentümer von Repository und `.git` ermitteln.
Beide Pfade und das lokale Benutzerkonto werden unmittelbar vor dem Finalizer
erneut geprüft. Ein bereits gesetzter Nutzerwert wird nicht ersetzt, muss aber
exakt dem gebundenen Repository-Eigentümer entsprechen. Root, `www-data`,
unterschiedliche oder fremde Eigentümer und ein abweichender Nutzerwert bleiben
gesperrt; nach dem Finalizer wird die Aufruferumgebung exakt auf ihren vorherigen
Zustand zurückgesetzt.

5.4.3k schließt zusätzlich den älteren nativen
`--target-updater-handoff`, der `E3DC_BOOTSTRAP_USER` ebenfalls vor seinem
root-eigenen Ziel-Snapshot entfernt. Dieser Einstieg und der bereits in
5.4.3j gebundene flaglose Snapshot dürfen den Installationsnutzer erst nach
dem Root-Lock aus demselben gültigen Nicht-Root-Eigentümer von Repository und
`.git` binden. Nach der Bindung des versiegelten Snapshots werden Repository,
`.git`, lokales Benutzerkonto und Nutzerwert unmittelbar vor dem ersten
Import aus dem Zielcode erneut geprüft. Die fail-closed Grenzen und alle
übrigen Härtungen aus 5.4.3j bleiben unverändert.

### 5.4.4d: lösungsorientierter Ziel-Updater und bestätigte Webaktionen

5.4.4d führt Vollbackup, kurze ruhende Daten-Nachsicherung,
Releaseprojektion, Reparatur bekannter Rechte und Dienstneustart vollständig im
heruntergeladenen Ziel-Updater aus. Das Nutzer-`.git`, lokale
Produktänderungen, fehlende Produktdateien und historische Rechte sind keine
vorgelagerten Startbedingungen. Der Rechte-Button verwendet denselben
root-eigenen Backup-/Updateauftrag.

Kann der Auftrag wegen `ENOSPC`, `EROFS` oder `EACCES` nicht fortfahren, nennt
er den betroffenen Pfad und einen konkreten Prüf- beziehungsweise
Fortsetzungsbefehl. Werden mehrere gleichrangige Installationen gefunden,
werden dauerhafte, einzeln ausführbare Befehle für die Kandidaten ausgegeben;
der Updater rät keine Instanz.

Alle sichtbaren zustandsändernden Webaktionen verlangen Anmeldung
und CSRF-Schutz. Ein Erfolg setzt passenden HTTP-Status, gültige Antwort und
ausgewertete Teilergebnisse voraus. Das bestätigt den Softwarevertrag der
Buttons, nicht die Feldbetätigung jedes erreichbaren physischen Aktors.

Der Modus-5-Datenvertrag akzeptiert den privaten Standard `02770` und den
ausdrücklich gewählten Kompatibilitätsmodus `02775`. HA-Leases werden nur bei
exakt passender Rolle und Peer-Bindung übernommen. Bluelink priorisiert
ausdrücklich vorhandene aktuelle Felder; ein bewusst leerer oberster Token wird
nicht durch einen älteren Fallback ersetzt und ein fehlender Token nur einmalig
als ruhige Information gemeldet.

### 5.4.4c: direkter, Git-unabhängiger Updatepfad

5.4.4c lädt den veröffentlichten Ziel-Updater und lässt diesen den betriebenen
Bestand sichern und ersetzen. Das Nutzer-`.git`, lokale Produktänderungen,
fehlende Produktdateien und historische Rechte blockieren den Start nicht. Die
Updateanzeige vergleicht nur die installierte `VERSION` mit dem neuesten
Stable-Release und bleibt vom eigentlichen Startpfad getrennt. Damit kann auch
dieselbe Version bewusst als Reparatur erneut installiert werden.

Das Vollbackup entsteht bei laufenden Diensten. Danach gibt es genau einen
kurzen Dienststopp: Zuerst werden veränderliche Daten im ruhenden Zustand
nachgesichert, anschließend Produkt- und Webdateien, Rechte, Core-Units und
root-eigene Launcher direkt aus dem Zielrelease projiziert. Nach dem Neustart
werden installierte Version und Pflichtdienste geprüft.

Der Normalpfad verwendet keinen alten Release-Finalizer, verlangt keinen
Ziel-Updater auf demselben Produkt-Dateisystem und setzt keinen persistenten
Recovery-Bootblock. Kann ein bereits installierter 5.4.4b-Launcher den neuen
Pfad noch nicht erreichen, bleibt `sudo /bin/sh ./e3dc-update-bootstrap` die
einmalige portable Brücke. 5.4.4c ändert gegenüber 5.4.4b keine EMS- oder
Hardwarelogik.

### 5.4.4b: universeller Ziel-Updater mit geringer Ausfallzeit

5.4.4b lädt für jeden normalen Einstieg zuerst den veröffentlichten
Ziel-Updater. Eine laufende Einzelinstanz liefert Installationsroot und
Installationsbenutzer unabhängig davon, ob sie unter `pi`, einem anderen
lokalen Konto oder in einem abweichenden Verzeichnis installiert wurde.
Pfade aus vorhandenen Metadatendateien dienen nur als Hinweise und müssen zum
laufenden Dienst passen. Werden mehrere gleichrangige Instanzen erkannt,
werden ihre Pfade ausgegeben und der Auftrag stoppt ohne Änderung.

Der alte Produktbaum entscheidet nicht, welche Daten gesichert werden. Der
neue Ziel-Updater inventarisiert Konfiguration, Betriebsdaten, Programmbaum,
Webdateien und privilegierte Releaseflächen nach seinem eigenen
Backupvertrag, erstellt das Backup und prüft Manifest sowie Digest. Dienste
und Hardware-Writer laufen während Download, Vorprüfung und Backup weiter.
Erst unmittelbar vor der atomaren Datei-, Rechte- und Unit-Projektion werden
sie gestoppt. Nach Dienststart, Healthcheck und stabilem Zielzustand wird der
transaktionsgebundene Startschutz entfernt.

Lokale Produktänderungen, fehlende Release-Dateien, frühere Besitzer oder
Modi und beschädigte Git-Metadaten sind deshalb keine vorgelagerte
Updateautorität. Sie werden gesichert und nach bestätigter Writer-Ruhe durch
den freigegebenen Releasebaum ersetzt. Echte Mehrdeutigkeit, Pfadflucht,
Symlinks, Spezialdateien, konkurrierende Writer oder ein nicht prüfbares
Backup bleiben harte Stopps.

Ein kontrollierter Abbruch enthält immer Fehlercode, Ursache, belegten
Systemzustand, Updateziel und genau den nächsten sicheren Diagnose- oder
Reparaturbefehl. Bereits `committed` markierte Sicherheitsreste aus 5.4.4 und
5.4.4a dürfen nur bei exakter Tag-, Commit-, Backup-, Drop-in- und
Instanzbindung bereinigt werden. Ein `pending`-Beleg wird nicht geraten oder
automatisch fortgesetzt; die Ausgabe verweist dann auf das gebundene Journal.

### 5.4.4a: ein gemeinsamer Ein-Datei-Updateeinstieg

5.4.4a macht `e3dc-update-bootstrap` zum portablen Community-Einstieg für
heterogene Altinstallationen. Der Aufruf
`sudo /bin/sh ./e3dc-update-bootstrap` benötigt weder einen fest codierten
Installationspfad noch ein vorgeschaltetes `chmod` oder Rollenargument. Der
damit gestartete veröffentlichte Updatepfad bindet Installationsroot,
Installationsbenutzer und Anlagenrolle eindeutig. Mehrere gleichrangige
Kandidaten oder eine widersprüchliche Rolle werden angezeigt und nicht geraten.

Weboberfläche, Konsole, Installer-Menü und automatische Updateprüfung starten
danach denselben root-eigenen systemd-Hintergrundauftrag. Der vorhandene
Alt-Updater, lokale Git-Metadaten, getrackte Änderungen und historische
Dateimodi sind keine vorgelagerte Updateautorität. Installer- und
Web-Pfadmetadaten werden auf den erkannten Bestand normalisiert und vom
Zielprozess ohne Bootstrap-Umgebung erneut geprüft.

Der eigentliche Zielvertrag bleibt unverändert: Backup erstellen und prüfen,
Writer und Dienste stoppen, veröffentlichte Dateien kopieren, Release-Rechte
setzen, Dienste starten und Gesundheit prüfen. Unsichere Pfade, Links oder
Spezialdateien, konkurrierende Writer und fehlgeschlagene Backup-, Start- oder
Healthchecks bleiben harte Stopps. EMS-, Direktvermarktungs-, Wallbox- und
Hardwarelogik entsprechen unverändert 5.4.4.

### 5.4.4: konsolidierter Ziel-Updater

5.4.4 führt Web- und Download-Update für heterogene Altanlagen in einem
gemeinsamen Zielstand zusammen. Der Webpfad startet den veröffentlichten
Ziel-Updater aus einem root-eigenen Ausführungssnapshot; der tatsächliche
Installationsroot bleibt davon getrennt und eindeutig gebunden. Der
vorhandene Alt-Updater liefert keine Updateautorität.

Der Ablauf bleibt bewusst kurz: Backup erstellen und prüfen, Dienste und
Writer stilllegen, Produktdateien kopieren, Zielrechte und eng bekannte alte
Dienstdarstellungen herstellen, Dienste starten und Healthcheck prüfen. Eine
benötigte Laufzeitumgebung darf im ausdrücklich gebundenen Downloadpfad
erstellt werden. Frühere Dateirechte oder ein redundanter kanonischer
Storage-Override blockieren das Update nicht allein. Pfadflucht, Symlinks,
Spezialdateien, nicht eindeutig gebundene Systemdateien, konkurrierende Writer
sowie ein fehlgeschlagenes Backup oder ein fehlgeschlagener Ziel-Healthcheck
bleiben harte Abbruchgründe.

### 5.4.3s: fehlender kanonischer Backup-Root

5.4.3s legt im administrativen Root-Download-Bootstrap einen wirklich
fehlenden kanonischen Backup-Root sicher an. Ein bereits vorhandener
unsicherer oder widersprüchlicher Pfad bleibt ein harter Abbruchgrund. Danach
bleibt der Ablauf unverändert: Backup erstellen und prüfen, Dienste stoppen,
Dateien und Rechte aktualisieren, Dienste starten und ihren Zustand prüfen.
EMS-, Direktvermarktungs-, Wallbox- und Hardwarelogik ändern sich nicht.

### 5.4.3r: HA-Rollenanker aus gebundener Rolle und Peer

5.4.3r darf beim ausdrücklich mit Rolle und Peer gebundenen
Download-Bootstrap einen wirklich fehlenden HA-Rollenanker aus dieser bereits
bestehenden Bindung erzeugen. Das geschieht erst nach verifiziertem Backup und
bestätigter Writer-Ruhe. Alle übrigen Update- und Sicherheitsverträge bleiben
unverändert; EMS-Regelung und Hardwareausgänge ändern sich nicht.

### 5.4.3q: absoluter visudo-Pfad im Bootstrap-Finalizer

5.4.3q verwendet im Finalizer des administrativen Download-Bootstraps den
absoluten Pfad `/usr/sbin/visudo`. Der Abschluss ist damit nicht von einem
verkürzten Root-`PATH` abhängig. Alle übrigen Update- und Sicherheitsverträge
bleiben unverändert; EMS-Regelung und Hardwareausgänge ändern sich nicht.

### 5.4.3p: Git-Metadaten beim Installationsbenutzer

5.4.3p erzeugt die vom administrativen Download-Bootstrap neu aufgebaute
`.git`-Fläche als den zuvor eindeutig gebundenen Installationsbenutzer. Der
anschließende Ziel-Updater erfüllt damit den regulären
Repository-Eigentümervertrag.

Verifiziertes Backup, bestätigte Writer-Ruhe und sämtliche Safety-Gates des
Rettungswegs bleiben unverändert verpflichtend. EMS-Regelung und
Hardwareausgänge ändern sich nicht.

### 5.4.3o: robuster Rettungsweg für heterogene Altinstallationen

5.4.3o führt `e3dc-update-bootstrap` als einheitlichen administrativen
Rettungsweg ein. Er ermittelt den veröffentlichten Stable-Tag samt Commit über
isolierten Git-Transport, lädt einen root-eigenen temporären Ziel-Checkout und
führt ausschließlich dessen Ziel-Updater aus. Der vorhandene Alt-Updater und
seine `.git`-Metadaten liefern keine Updateautorität.

Nach verifiziertem Backup und bestätigter Writer-Ruhe normalisiert der
Ziel-Updater bekannte Release-Dateien, Rechte und Units. Pfadflucht, Symlinks,
Spezialdateien, zusätzliche Hardlinks, konkurrierende Updates, nicht
stillgelegte Writer sowie ein ungültiges Backup oder fehlgeschlagener
Ziel-Healthcheck bleiben harte Stopps.

### 5.4.3n: pfadgenauer Rollenanker im Backup- und Recoveryvertrag

5.4.3n korrigiert ausschließlich die Metadatenprüfung der privilegierten
Restorequelle. Nur der kanonische Pfad
`/etc/e3dc-control/instance_role.json` wird mit dem vom Rollenmodell
vorgegebenen Vertrag `root:www-data 0640` akzeptiert. Die daraus erzeugte
private Backup-Payload bleibt `root:root 0600`.

Für alle anderen privilegierten Pfade gilt weiterhin der strengere
`root:root`-Vertrag. Ein falscher Pfad, eine andere Gruppe oder ein anderer
Modus sowie Symlinks, zusätzliche Hardlinks, ACLs, Attribute oder eine
Identitätsdrift brechen den Update-Rückweg weiterhin fail-closed ab. Die
EMS-Regelung, HA-, Wallbox-, Wärme- und Direktvermarktungsentscheidungen sowie
sämtliche Hardwareausgänge bleiben gegenüber 5.4.3m unverändert.

### 5.4.3m: vorwärtsgebundener Rollenanker und eindeutige Drop-ins

5.4.3m erlaubt ausschließlich dem vollständig versiegelten nativen
Ziel-Updater bei einem durch `git merge-base --is-ancestor` als echt
vorwärtsgerichtet belegten Releasewechsel, einen wirklich fehlenden
Instanzrollenanker einmalig auf `off` zu projizieren. Dafür müssen die
eingefrorene Rolle exakt `off` und die konfigurierte HA-Peer-Adresse leer sein.
Bootstrap, Reinstall, Rollback, ein identischer oder rückwärtsgerichteter
Commit und andere Aufrufpfade besitzen diese Autorität nicht.

Der Bedarf wird zunächst rein lesend geprüft. Die Erzeugung erfolgt erst nach
dem Root-Receipt-gebundenen Transaktionsbackup, einer gegebenenfalls nötigen
und vollständig bestätigten Storage-Manager-Unit-Promotion sowie der
nachgewiesenen Aktorruhe. Dadurch liegt die Projektion innerhalb derselben
Update- und Recovery-Transaktion. Fehlt der gebundene Recovery-Vertrag, bleibt
die Mutation gesperrt. Ein passender vorhandener Anker bleibt unverändert;
`master`, `slave`, `shadow`, ein konfigurierter Peer sowie ein vorhandener
fremder oder widersprüchlicher Anker bleiben harte Abbruchgründe.

Atomare Schreibvorgänge für Notifier-Drop-ins verwenden ihr privates
Transaktionsstaging ab 5.4.3m außerhalb des `*.service.d`-Verzeichnisses. Ein
vom älteren Writer zurückgelassener verschachtelter Staging-Ordner wird nur
dann entfernt, wenn er descriptorgebunden stabil, root-eigen, Modus `0700`,
exakt leer und frei von ACLs oder Attributen ist. Ein nichtleerer,
ausgetauschter oder fremder Bestand bleibt gesperrt.

Für eine optionale, nicht installierte Unit darf die On-Disk-Drop-in-Fläche
neben dem eigenen Recovery-Startschutz ausschließlich das byte- und
metadatengenau geprüfte kanonische RAM-Disk-Drop-in enthalten. Weitere Namen,
abweichende Bytes, Eigentümer, Modi, Links, ACLs oder Attribute bleiben
fail-closed. Dadurch ist eine bewusst optionale Unit nicht allein wegen ihres
kanonischen RAM-Disk-Schutzes mehrdeutig, ohne die Fremdflächenprüfung zu
lockern.

### 5.4.3l: updater-eigener Git-Rückweg

5.4.3l bindet den nativen Rückweg vor der ersten Dienstmutation an
Repository, `old_commit`, root-eigenes Transaktionsbackup und
Transaktionskennung. Der konfigurierte Backup-Root und seine Elternkette
müssen bereits root-kontrolliert und frei von unsicheren Links, ACLs oder
Attributen sein. Der Updater macht einen ungeeigneten Bestandsordner nicht
durch nachträgliche Rechteänderungen vertrauenswürdig.

Historisch war der damalige Web-Launcher davon getrennt und noch clean-only.
Der aktuelle gemeinsame Dispatcher verwendet diesen vorgelagerten Git- und
Dirty-Vertrag nicht mehr; Web, Konsole und automatische Prüfung starten
denselben Download-Zielpfad. Die hier beschriebene 5.4.3l-Recovery bleibt als
historischer Rückweg dokumentiert, ist aber keine Startbedingung des heutigen
Ein-Datei-Bootstraps.

Bei belegten, weiterhin vorhandenen Änderungen an getrackten Dateien stellt
dieser Rückweg die gesicherten Bytes wieder her und härtet den Dateimodus auf
den im gebundenen `old_commit` belegten Git-Modus. Unveränderte getrackte
Dateien folgen vollständig diesem Ausgangscommit. Staged Indexstände,
ungetrackte oder gelöschte Dateien sowie allgemeine manuelle, ZIP- und ältere
Restorepfade erhalten durch 5.4.3l keine neue Wiederherstellungszusage.

Eine eng freigegebene historische Familie der
`e3dc-storage-manager.service` wird, falls sie exakt passt, noch vor dem
ersten Dienststopp atomar in eine neue root-eigene Unit mit Modus `0644`
überführt. Danach müssen `daemon-reload` und der erneut gelesene effektive
Unit-Vertrag vollständig passen. Abweichende Units oder fremde Drop-ins
bleiben gesperrt. PiGuard mit dem exakten Zustand
`ActiveState=activating`/`SubState=auto-restart` wird als zuvor laufender
Wächter erfasst, vor den Writer-Diensten gestoppt und im Erfolgs- oder
belegten Rückweg wieder entsprechend hergestellt.

Scheitert der updater-eigene Rückweg synchron und nachweisbar, bleibt ein an
dieselbe Transaktion gebundener systemd-Startschutz für PiGuard und die
bekannten Writer bestehen. Ein vorhandener Marker oder ein nur teilweise
belegbarer Satz reservierter Startbedingungen blockiert den nächsten normalen
Updateversuch vor Backup und Dienstmutation. Der Startschutz ist keine
pauschale Garantie bei Stromausfall, `SIGKILL` oder einem Prozessabbruch
außerhalb dieses erkannten Fehlerpfads.

### 5.4.3m: persistenter Update-Sicherheitsbeleg

Der vollständig versiegelte Normalpfad persistiert nach dem verifizierten
Backup und vor der ersten Produktmutation einen ausstehenden
Update-Sicherheitsbeleg (`pending`). Er bindet Transaktionskennung, Zielstand,
Rolle, Backup, dynamische `00`-Bootblock-Inodes und den verwalteten
Finalizer-Dienst. Anschließend werden Marker und Startschutz armiert, Writer und
PiGuard gestoppt sowie Backup, Repository, privilegierte Konfiguration und
Dienstruhe unmittelbar vor der Mutation erneut geprüft.

`pending` bleibt fail-closed und ist ausdrücklich **kein Forward-Auto-Resume**.
Ein ausstehender, älterer, fremder, unvollständiger oder nicht mehr eindeutig
gebundener Vertrag verlangt einen separat geprüften manuellen Rückweg. Seine
Marker, Drop-ins und Belege werden weder adoptiert noch einzeln entfernt oder
durch einen neuen Updateversuch umgangen.

Erst wenn Zielstand, Dienststart, Gesundheit und Boot-Sanity vollständig
bestätigt sind, wird der Beleg dauerhaft auf `committed` gesetzt. Ab diesem
Punkt ist jeder Rückweg auf den Altstand verboten. Bei einem unterbrochenen
Abschluss ist ausschließlich das exakt gebundene Cleanup der eigenen Marker-,
`00`- und Receipt-Reste zulässig. Ein äußerer oder späterer Cleanup-Pfad darf
erst nach nachweislich inaktiver Finalizer-Lease eingreifen. Fremde oder
driftende Flächen bleiben unangetastet und fail-closed.

Erreicht ein abweichender oder noch älterer lokaler Updater diese gebundene
Kompatibilitätsbrücke nicht, gilt der oben beschriebene Download-Bootstrap als
einheitlicher Rettungsweg. Er prüft Installationsnutzer, Stable-Tag,
Commit-SHA, Zielpfad und Rolle im heruntergeladenen Zielstand erneut. Bei einem
Fehler bleibt das verifizierte Backup der Rückweg.

Die folgenden Absätze dokumentieren ausschließlich den historischen
Ziel-Updater-/Finalizer-Pfad bis 5.4.4b. Sie beschreiben nicht den normalen
Updateablauf ab 5.4.4c.

Ab dem damaligen Release mit Ziel-Updater-Handoff lud die laufende Version zunächst
ausschließlich die Git-Objekte des freigegebenen Zielstands. Sie bindet
Repository-Origin, volle Commit-SHA, annotierten Stable-Tag,
VERSION-/Policy-Zuordnung und die vorhandene Anlagenrolle. Danach startet sie
noch vor Backup und Dienststopp einen bytegenau versiegelten Ziel-Updater auf
demselben Dateisystem. Erst dieser Ziel-Updater interpretiert seine eigenen
Dienst-, Paket-, Lösch- und Wiederanlaufverträge und besitzt die vollständige
Transaktion einschließlich Backup, Aktorruhe, Finalisierung und
Wiederherstellung.

Dieser verifizierte Fetch ist die einzige beabsichtigte vorgelagerte
`.git`-Mutation: Er ergänzt Zielobjekte und den gebundenen Remote-Ref, verändert
aber weder Produktdateien noch globale oder repo-lokale Git-Konfiguration. Alle
anschließenden Git-Leseprüfungen laufen mit `GIT_OPTIONAL_LOCKS=0`, damit auch
Index-/Stat-Cache nicht beiläufig aktualisiert werden.

Der äußere Handoff besitzt kein hartes Zeitlimit für die Gesamttransaktion.
Eine wichtige Übergangsgrenze bleibt technisch unvermeidbar: Beim ersten
Sprung von einer Version ohne diesen Vertrag läuft zunächst noch deren alter
Updater. Insbesondere der in `v5.4.2d` veröffentlichte Außenprozess behält für
diesen ersten Sprung sein 900-Sekunden-Limit; Zielcode kann einen bereits
gestarteten Altprozess nicht rückwirkend ersetzen. Die Kompatibilitätsbrücke
begrenzt deshalb ihren gesamten lokalen Handoff auf zwölf Minuten. Ist dieses
Budget schon vor dem Mutationsstart erschöpft, startet sie keinen Zielprozess;
andernfalls beendet sie ihn spätestens an dieser Grenze als ganze Prozessgruppe,
erzwingt dies nach zehn weiteren Sekunden und meldet den Fehler erst nach dem
nachgewiesenen Prozessende an den Altprozess. Dessen Wiederherstellung kann
dadurch nicht parallel zu einem weiterlaufenden Ziel-Finalizer arbeiten. Nach
erfolgreicher Installation des neuen Vertrags verwenden alle folgenden Updates
den Ziel-Updater-Handoff.

Der damalige gemeinsame Update-Einstieg verwendete auch beim bereits aktuellen Bestand
den vollständigen Ziel-Updater-Vertrag: Backup erstellen und prüfen, Dienste
kontrolliert anhalten, den veröffentlichten Stand samt Rechten projizieren,
Dienste starten und ihren Zustand prüfen. Lokale Git-Metadaten, abweichende
Dateimodi oder regulär geänderte Produktdateien sperren diesen
administrativen Reparaturweg nicht mehr vorab. Unsichere Pfadobjekte, ein
fehlerhaftes Backup, unklare Rollen oder Installationswurzeln, konkurrierende
Schreiber und ein fehlgeschlagener Wiederanlauf bleiben harte Abbruchgründe.

`--reinstall-current` und ein gezielter Release-Tag bleiben eigenständige
administrative Vorgänge und dürfen nicht mit `--update-e3dc` kombiniert
werden.

Der versiegelte Release-Finalizer meldete seine Phasen und während langer
Arbeit alle 30 Sekunden einen Heartbeat. Für langsame Raspberry Pis gilt ein
hartes Gesamtzeitlimit von 30 Minuten ausschließlich für diese mutierende
Finalizer-Phase. Backup und verifizierte Wiederherstellung liegen außerhalb
dieses Zeitlimits. Wird es überschritten oder bleibt ein notwendiger Dienst
trotz begrenztem Konvergenzfenster nicht beweisbar
`loaded/enabled/active`, bricht die Finalizer-Phase ab und der Ziel-Updater
versucht die verifizierte Wiederherstellung des Ausgangszustands. Nur wenn
deren Dienst-, Rollen- und Gesundheitsnachweis vollständig gelingt, werden die
Writer wieder freigegeben; andernfalls bleiben sie fail-closed gestoppt. Das
Zeitlimit wird nicht durch stilles Weiterwarten oder ein schwächeres
Gesundheitsgate umgangen.

Seit 5.4.3l hinterlässt ein von diesem Ziel-Updater synchron erkannter und
nicht vollständig behebbarer Recoveryfehler zusätzlich den oben beschriebenen
persistenten Startschutz. Diese Aussage erweitert weder ältere noch manuelle
Restorepfade und setzt voraus, dass der laufende Fehlerpfad den Schutz selbst
belegen konnte.

Wenn `--check` eine fehlende Web-/sudo-Freigabe meldet:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --fix-permissions
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

Ist bei einer Altinstallation der privilegierte Web-Launcher fehlend oder nicht
ausführbar, wird die veröffentlichte Datei `e3dc-update-bootstrap` auf den
Raspberry Pi kopiert. In der SSH-Konsole genügt unabhängig vom
Installationspfad:

```bash
sudo /bin/sh ./e3dc-update-bootstrap
```

Der Bootstrap startet das Update im Hintergrund und führt weder den alten
Web-Launcher noch den alten `installer_main.py`-Updatepfad aus. Eine
Passwortabfrage von `sudo` ist an der interaktiven SSH-Konsole normal. Meldet
der Bootstrap mehrere gleichrangige Installationen oder einen
widersprüchlichen Rollenbestand, bitte dort stoppen und die ausgegebenen
Kandidaten prüfen. Nach einem erfolgreichen Abschluss steht der reguläre
Web-Update-Pfad wieder zur Verfügung.

Python-Abhängigkeiten werden bei einem Release-Wechsel ausschließlich im
gebundenen Benutzer-venv installiert. Auf Debian-Systemen mit PEP 668 wird
deshalb kein System-`pip` und kein `--break-system-packages` verwendet. Fehlt
das Standard-venv, darf der heruntergeladene Ziel-Installer nach Installation
von `python3-venv` genau dieses venv im Home des Installationsbenutzers neu
anlegen. Der Ein-Datei-Bootstrap führt hierfür keinen alten lokalen
Installerprozess weiter. Abweichende oder mehrdeutige venv-Pfade brechen den
Updatevorgang weiterhin ab.

## Einmalige ML-Lock-Reparatur für Alt-Updater bis 5.4.1c

Diese Reparatur ist ausschließlich für einen Updateabbruch mit der exakten
Meldung `Unsicherer privater ML-Eintrag: .ml_model.lock` vorgesehen. Updater
bis einschließlich 5.4.1c prüfen das Backup, bevor sie die neuen Releasebytes
laden, und können diese Kante deshalb nicht selbst reparieren. Ab einem
erfolgreich installierten 5.4.1d-Stand normalisiert der Updater ausschließlich
einen eindeutig sicheren und unbelegten Alt-Lock selbst; alle anderen Fälle
bleiben fail-closed.

Der folgende Block muss in der SSH-Sitzung des normalen
Installationsbenutzers ausgeführt werden, nicht aus einer direkten Root-Shell.
Er verändert ausschließlich Eigentümer, Gruppe und Modus einer eindeutig
regulären, unverlinkten, größenbegrenzten und aktuell nicht belegten
Sperrdatei. Modell, Manifest und Konfiguration bleiben unverändert.

```bash
sudo /usr/bin/python3 - <<'PY'
import fcntl
import os
import pwd
import stat
import time
from pathlib import Path

store = Path("/var/lib/e3dc-control/ml")
lock_name = ".ml_model.lock"

def stop(message):
    raise SystemExit("ABBRUCH: " + message)

user = os.environ.get("SUDO_USER", "")
if not user or user == "root":
    stop("bitte direkt als Installationsbenutzer mit sudo ausführen")

account = pwd.getpwnam(user)

try:
    resolved = store.resolve(strict=True)
except FileNotFoundError:
    stop("privater ML-Store fehlt")

if resolved != store:
    stop("Symlink-Komponente im privaten ML-Store")

store_info = os.lstat(store)
if (
    not stat.S_ISDIR(store_info.st_mode)
    or stat.S_IMODE(store_info.st_mode) != 0o700
    or store_info.st_uid != account.pw_uid
):
    stop("ML-Store ist nicht das erwartete 0700-Verzeichnis des Installationsbenutzers")

directory_fd = os.open(
    store,
    os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0),
)

lock_fd = None
locked = False
try:
    opened_store = os.fstat(directory_fd)
    if (opened_store.st_dev, opened_store.st_ino) != (
        store_info.st_dev,
        store_info.st_ino,
    ):
        stop("ML-Store wurde beim Öffnen ausgetauscht")

    try:
        path_before = os.stat(
            lock_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        print("OK: ML-Sperrdatei fehlt; keine Reparatur erforderlich")
        raise SystemExit(0)

    if (
        not stat.S_ISREG(path_before.st_mode)
        or path_before.st_nlink != 1
        or path_before.st_size > 64 * 1024
        or path_before.st_uid not in {0, account.pw_uid}
    ):
        stop("ML-Sperrdatei ist nicht sicher normalisierbar")

    lock_fd = os.open(
        lock_name,
        os.O_RDWR
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
        dir_fd=directory_fd,
    )

    opened_lock = os.fstat(lock_fd)
    if (opened_lock.st_dev, opened_lock.st_ino) != (
        path_before.st_dev,
        path_before.st_ino,
    ):
        stop("ML-Sperrdatei wurde beim Öffnen ausgetauscht")

    deadline = time.monotonic() + 10.0
    while True:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            locked = True
            break
        except BlockingIOError:
            if time.monotonic() >= deadline:
                stop("ML-Sperrdatei ist länger als 10 Sekunden belegt")
            time.sleep(0.1)

    before = os.fstat(lock_fd)
    try:
        path_locked = os.stat(
            lock_name,
            dir_fd=directory_fd,
            follow_symlinks=False,
        )
    except FileNotFoundError:
        stop("ML-Sperrdatei wurde während der Sperrprüfung entfernt")

    if (
        (path_locked.st_dev, path_locked.st_ino)
        != (before.st_dev, before.st_ino)
        or not stat.S_ISREG(before.st_mode)
        or before.st_nlink != 1
        or before.st_size > 64 * 1024
        or before.st_uid not in {0, account.pw_uid}
    ):
        stop("ML-Sperrdatei ist nach der Sperrprüfung nicht sicher normalisierbar")

    os.fchown(lock_fd, account.pw_uid, store_info.st_gid)
    os.fchmod(lock_fd, 0o600)
    os.fsync(lock_fd)
    after = os.fstat(lock_fd)

    if (
        (after.st_dev, after.st_ino, after.st_size, after.st_mtime_ns)
        != (before.st_dev, before.st_ino, before.st_size, before.st_mtime_ns)
        or after.st_nlink != 1
        or after.st_uid != account.pw_uid
        or after.st_gid != store_info.st_gid
        or stat.S_IMODE(after.st_mode) != 0o600
    ):
        stop("Metadaten wurden nicht exakt normalisiert")

    path_after = os.stat(
        lock_name,
        dir_fd=directory_fd,
        follow_symlinks=False,
    )
    if (path_after.st_dev, path_after.st_ino) != (
        after.st_dev,
        after.st_ino,
    ):
        stop("Lockpfad wurde während der Reparatur ausgetauscht")

    print("OK: ausschließlich die Metadaten der ML-Sperrdatei wurden repariert")
finally:
    if lock_fd is not None:
        try:
            if locked:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
        finally:
            os.close(lock_fd)
    os.close(directory_fd)
PY
```

Nach einer `OK`-Meldung kann der normale Web- oder Konsolen-Updater erneut
gestartet werden, sobald der angebotene Zielstand mindestens **5.4.2** ist.
Dieser Stand enthält zusätzlich die Kompatibilitätsbrücke für den bereits vor
dem Git-Wechsel geladenen Backup-Validator aus 5.4.0a. Bei einem älteren
angebotenen Zielstand denselben Webupdate-Versuch nicht wiederholen. Bei
`ABBRUCH` nichts löschen und insbesondere `.ml_model.lock`, Modell und
Manifest nicht manuell entfernen.

Scheitert ein bereits separat gestarteter **v5.4.0b-Bootstrap** mit
`Target-Datei besitzt keine vertrauenswürdigen Eigentümer-/Schreibrechte`,
darf derselbe alte Zwischenrunner nicht erneut verwendet und der
Installationsbaum nicht pauschal rekursiv umgeschrieben werden. Nach
verifizierter Wiederherstellung wird stattdessen der `e3dc-bootstrap` aus dem
aktuellen Release-Archiv direkt mit dessen veröffentlichtem Tag und
40-stelliger Commit-SHA gestartet. Der aktuelle Runner bindet den Zielbaum
bytegenau an diesen Commit und startet den privilegierten Finalizer
ausschließlich aus einem separaten root-eigenen, schreibgeschützten
Ausführungssnapshot. Fremde Eigentümer, schreibbare Komponenten, Hardlinks,
Symlinks und Inhaltsabweichungen bleiben harte Abbruchgründe; ein erneuter
Fehler nennt den relativen Pfad sowie UID, GID, Modus und Linkzahl. Der
Snapshot ist dabei ausschließlich die geprüfte Codeherkunft. Alle operativen
Pfade für Logs, Dienste, Rechte, Web-Wrapper und Sudoers werden gegen die
gebundene Produktinstallation aufgelöst.

In einer Docker-Installation führen weder Weboberfläche noch Konsole einen
Release-Wechsel im laufenden Container aus. Sie zeigen stattdessen die drei
Host-Befehle aus dem Abschnitt [Docker-Update](#docker-update).

## Alte Installationen und unterstützte Plattform

Auch alte, veränderte oder unvollständige Bare-Metal-Installationen verwenden
heute ausschließlich den oben beschriebenen Ein-Datei-Rettungsweg. Ein
Zwischenschritt über 5.3.2b, ein manueller Git-Aufbau, ein selbst eingetragener
Commit oder der historische Target-Finalizer sind nicht mehr erforderlich und
sollen nicht mehr ausgeführt werden.

Der Ziel-Updater benötigt Python 3.10 oder neuer. Ist nur eine ältere
Python-Version vorhanden, endet der Auftrag vor jeder Änderung mit dem
Fehlercode `E3DC-UPD-PLATFORM-001`, bestätigt den unveränderten Systemzustand
und nennt die Lösung. Prüfe zunächst:

```bash
python3 --version
```

Aktualisiere ein zu altes Raspberry-Pi-/Debian-System auf eine unterstützte
Version mit Python 3.10 oder neuer und starte anschließend exakt denselben
Updatebefehl erneut. Der Updater versucht kein eigenmächtiges Betriebssystem-
Upgrade.

Der aktuelle Ablauf bleibt bewusst kurz: Zielrelease laden, Vollbackup bei
laufenden Diensten, Abhängigkeiten vorbereiten, einmaliger kurzer Dienststopp,
ruhende Daten nachsichern, Produkt/Web/Rechte/Units projizieren und die
benötigten Dienste neu starten. Das Nutzer-`.git` ist daran nicht beteiligt.
Scheitert der Austausch nach begonnener Produktänderung, werden das gebundene
Vollbackup und danach die neuere ruhende Daten-Nachsicherung wiederhergestellt,
bevor der vorherige Dienstsatz erneut gestartet wird.

## Gezielter Rückfall

`v5.4.2d` übernimmt unverändert den in `v5.4.2` veröffentlichten
Rückfallvertrag.

`v5.4.2` bietet den bereinigten Root `v5.3.2b` ausschließlich als
Docker-Rückfall-Image an. Dieser Root gibt selbst keinen älteren öffentlichen
Tag frei. Auf Bare Metal wird `v5.3.2b` nicht als Programm-Rückfall angeboten,
weil der Altstand keinen zielgebundenen Release-Finalizer enthält. Freie
Commit-Hashes oder ein manueller Checkout sind kein Ersatz; dort bleibt die
Wiederherstellung eines verifizierten Datei-Backups der sichere Rückweg.

## Docker-Update

```bash
(
  set -euo pipefail
  cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
  if [ -f ./docker_compose_update.py ]; then
    E3DC_DOCKER_HELPER=./docker_compose_update.py
  elif [ -f ./Installer/docker_compose_update.py ]; then
    E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py
  else
    echo "docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen." >&2
    exit 2
  fi
  sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo
  sudo docker compose logs --tail=80 e3dc-control
)
```

Der Web-Updater erkennt den Containerkontext auch über den Marker des
offiziellen Images und zeigt diese Befehle an. Er benötigt keinen Zugriff auf
den Docker-Socket und versucht bewusst nicht, den eigenen Container zu
ersetzen.

Der optionale Watchtower-Dienst startet nicht zusammen mit der
Standardanwendung. Das Upstream-Projekt wird nicht mehr gepflegt und sein
Docker-Socket-Zugriff ermöglicht weitreichende Kontrolle über den Docker-Host.
Der Dienst bleibt nur für bestehende Installationen im Compose-Profil
`auto-update`. Der bewusste Opt-in benötigt zusätzlich das standardmäßig
deaktivierte Containerlabel:

```bash
printf '%s\n' 'E3DC_WATCHTOWER_ENABLE=true' >> .env
sudo python3 ./Installer/docker_compose_update.py --compose-dir . --sudo
docker compose --profile auto-update up -d watchtower
```

Erst mit `E3DC_WATCHTOWER_ENABLE=true` berücksichtigt Watchtower durch den
Enable-Label-Filter den E3DC-Control-Container. Ohne diesen Wert bleibt auch
ein versehentlich gestartetes Profil für den Hauptcontainer wirkungslos. Ein
bereits aus einer älteren Compose-Datei
laufender Watchtower kann mit
`docker compose --profile auto-update stop watchtower` und anschließend
`docker compose --profile auto-update rm -f watchtower` entfernt werden.

Die mitgelieferte Compose-Datei verwendet standardmäßig
`ghcr.io/a9xxx/install-e3dc-control:${E3DC_IMAGE_TAG:-latest}`. Ein in der
Compose-Datei oder über `.env` fest eingestellter Versions-Tag bleibt bei
`pull` unverändert. `docker compose config --images` muss deshalb vor dem
Update den gewünschten Stable-Tag oder `latest` anzeigen.

Ein Docker-Rückfall verwendet ausschließlich einen Eintrag mit
`docker_supported: true`, dessen freigegebenen Image-Tag beziehungsweise den
verifizierten Digest. Der Container steuert den Host-Docker nicht selbst.
