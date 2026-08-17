# Update-Prozess

Updates werden ausschließlich über den Installer ausgeführt. Ein manuelles
`git pull --ff-only` ist für den einmaligen Wechsel auf die bereinigte
Release-Historie ungeeignet, weil alter und neuer Git-Stand nicht miteinander
verwandt sein müssen.

Der aktuelle Stable-Stand ist `v5.4.3q`. Der Ziel-Updater bindet den
freigegebenen Zielstand vor Backup und Dienststopp eindeutig an Version,
Herkunft und Anlagenrolle. Fortschritt und Lebenszeichen bleiben auf
langsameren Raspberry Pis sichtbar. Eine bereits vollständig installierte
Version bleibt beim normalen Update ohne Unterbrechung unverändert; eine
Reparatur oder Neuinstallation derselben Version muss ausdrücklich bestätigt
werden. Das Dashboard darf ausschließlich das reguläre Update über einen
argumentlosen, root-eigenen Systemjob starten. Freie Installer-Aktionen,
Pfade, Release-Tags, Reparaturen, Neuinstallationen und Rückfälle bleiben im
Web gesperrt und erfolgen über den geschützten Konsolenweg.

Die Konsolenbeispiele verwenden den zuvor geprüften absoluten Produktpfad:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
test -f "$E3DC_INSTALL_PATH/e3dc-setup"
```

## Normales Update

Der bevorzugte Weg ist der Update-Button der Weboberfläche. Er bindet den
unveränderten veröffentlichten Ausgangsstand an dessen Remote-Tag und startet
den Installer aus einem versiegelten Snapshot. Auf der Konsole steht derselbe
reguläre Installer-Pfad zur Verfügung:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
bash "$E3DC_INSTALL_PATH/e3dc-setup" --update-e3dc
```

### Rettungsweg für heterogene Altinstallationen

Wenn ein alter lokaler Updater an einer früheren Dateiberechtigung, einer
manuell kopierten Produktdatei oder einer veralteten Dienstprojektion scheitert,
kann der aktuelle Updater unabhängig vom installierten Programmstand geladen
werden:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
BOOTSTRAP_DIR=$(sudo /usr/bin/mktemp -d /run/e3dc-update-bootstrap.XXXXXX)
sudo /usr/bin/curl -q -fsS --proto '=https' --tlsv1.2 \
  -o "$BOOTSTRAP_DIR/e3dc-update-bootstrap" \
  https://raw.githubusercontent.com/A9xxx/Install-E3DC-Control/main/e3dc-update-bootstrap
sudo /bin/chmod 0700 "$BOOTSTRAP_DIR/e3dc-update-bootstrap"
sudo /bin/sh "$BOOTSTRAP_DIR/e3dc-update-bootstrap" "$E3DC_INSTALL_PATH"
sudo /bin/rm -rf -- "$BOOTSTRAP_DIR"
```

Der Download liegt damit von Anfang an in einer zufälligen, root-eigenen
`0700`-Laufzeitfläche. Ein lokaler Benutzer kann die anschließend als Root
ausgeführten Bytes weder austauschen noch über eine persönliche `curl`-
Konfiguration beeinflussen. Die HTTPS-Quelle ist das öffentliche
Projekt-Repository; der Bootstrap selbst bindet danach Stable-Tag und Commit
noch einmal über einen isolierten Git-Transport.

Der kleine Bootstrap ermittelt den aktuellen veröffentlichten Stable-Tag samt
Commit, lädt dessen Installer und führt **nicht** den vorhandenen Alt-Updater
aus. Die Rolle wird aus dem bestehenden Rollenanker beziehungsweise der
Konfiguration übernommen. Ist sie nicht eindeutig, muss sie als zweites
Argument (`off`, `master`, `slave` oder `shadow`) angegeben werden.

Die vorhandene `.git`-Fläche ist dabei keine Eingangsbedingung. Der Rettungsweg
liest weder ihre Rechte noch ihren Index oder ihre lokale Historie als
Updateautorität, sondern baut die für den Zielstand benötigten Git-Metadaten
nach dem Backup frisch auf. Die neue `.git`-Fläche entsteht dabei als der
zuvor eindeutig gebundene Installationsbenutzer. Gesichert werden die
tatsächlich betriebenen Produkt- und Konfigurationsdateien, nicht ein
möglicherweise beschädigter Git-Zwischenspeicher.

Der Ziel-Updater erstellt und prüft zuerst das Backup, stoppt danach die
bekannten Writer, projiziert Release-Dateien, Rechte und Dienste auf den
kanonischen Zielzustand und prüft anschließend den Wiederanlauf. Abweichende
Besitzer oder Modi bekannter Produktdateien sind dabei zu normalisierender
Altbestand und kein eigener Abbruchgrund. Unklare Symlinks, Spezialdateien,
konkurrierende Updates, nicht stoppbare Writer sowie ein fehlgeschlagenes
Backup oder Dienst-Endgate bleiben harte Stopps.

**Einmaliger Übergang auf 5.4.3i oder neuer:** Installationen bis einschließlich
5.4.3f besitzen den neuen root-eigenen Web-Launcher noch nicht. Dieser erste
Wechsel muss deshalb über den oben gezeigten administrativen Konsolenweg
erfolgen. Seit der erfolgreichen Installation von 5.4.3i richtet der Installer
den engen Launcher und seine argumentlose sudoers-Freigabe ein; alle folgenden
normalen Updates können wieder aus dem Dashboard gestartet werden.

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

Der Web-Update-Launcher bleibt davon bewusst getrennt und weiterhin
clean-only: Sobald er lokale Änderungen an getrackten Produktdateien erkennt,
bricht er vor dem Aufruf des Ziel-Updaters ab. Die nachfolgend beschriebene
Dirty-Recovery gilt ausschließlich für den regulären Konsolen-/nativen
Root-Updaterpfad und lockert das Web-Gate nicht. 5.4.3l behebt am Web-Einstieg
nur den EXIT-Cleanup. Auch bei einem frühen Abbruch bleiben dadurch die
primäre Fehlermeldung und der Exitstatus erhalten; ein bereits erzeugter
root-eigener Ausführungssnapshot unter `/run` wird entfernt, ohne den
Primärfehler durch einen nachgelagerten Snapshot-Scope-Fehler zu überdecken.

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

Ab dem Release mit Ziel-Updater-Handoff lädt die laufende Version zunächst
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

Ist der exakt gebundene Release-Stand bereits installiert und stimmt seine
kanonische Webprojektion überein, endet ein normales Update ohne Backup und
ohne Dienstunterbrechung mit „Du bist auf dem neuesten Stand“. Dabei werden
keine Produkt- oder Webdateien, kein Dienstzustand und keine globale oder
repo-lokale Git-Konfiguration verändert. Die Git-Prüfung verwendet
ausschließlich pro Aufruf gebundene Optionen. Ungetrackte private Laufzeit-
und Diagnosedateien lösen dabei keinen Reparaturalarm aus.

Weicht dagegen eine getrackte Produktdatei oder eine kanonische Webdatei vom
veröffentlichten Stand ab, meldet das normale Update `REPAIR_REQUIRED` und
verändert weder Produkt- oder Webdateien noch Dienstzustände. Die aktuelle
Version kann dann ausschließlich nach ausdrücklicher Bestätigung auf der
administrativen Konsole erneut installiert werden:

```bash
bash "$E3DC_INSTALL_PATH/e3dc-setup" --reinstall-current
```

Diese Neuinstallation ist ein vollständiger Releasewechsel derselben
veröffentlichten SHA: verifiziertes Backup, Aktorruhe, Release-Synchronisation,
Dienst- und Gesundheitsgates sowie ein beweisbarer Rückweg bleiben erhalten.
`--update-e3dc`, `--reinstall-current` und ein gezielter Release-Tag dürfen
nicht kombiniert werden.

Der versiegelte Release-Finalizer meldet seine Phasen und während langer
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

Ist bei einer 5.3.2b-Installation bereits der privilegierte
Web-Launcher fehlend oder nicht ausführbar, kann die Weboberfläche genau
diesen Einstieg nicht selbst reparieren. Dann ist einmalig eine interaktive
SSH-Konsole erforderlich. Bei der üblichen Installation ist folgende Kette
vollständig kopierbar:

```bash
export E3DC_INSTALL_PATH="$HOME/Install"
test -f "$E3DC_INSTALL_PATH/installer_main.py"
test -x "$HOME/.venv_e3dc/bin/python3"
cd "$E3DC_INSTALL_PATH"
sudo /usr/bin/python3 installer_main.py --fix-permissions
sudo /usr/bin/python3 -I -B -u installer_main.py --check
sudo /usr/bin/python3 -I -B -u installer_main.py --update-e3dc
cat VERSION
systemctl --failed --no-pager
```

Für den einmaligen Wechsel aus 5.3.2b ist ausschließlich der oben gezeigte
direkte Konsolenaufruf mit `--update-e3dc` freigegeben. Der Web-Launcher ist
in diesem Altstand noch nicht vorhanden. Der interaktive Installer-Menüpunkt lädt im
Altprozess bereits vor dem Git-Wechsel zusätzliche Module und darf für diesen
Hybridübergang nicht verwendet werden.

Liegt E3DC-Control nicht unter `$HOME/Install`, muss ausschließlich die erste
Zeile an den tatsächlichen absoluten Installationspfad angepasst werden. Eine
Passwortabfrage von `sudo` ist an der interaktiven SSH-Konsole in diesem Fall
normal. Schlägt eine der beiden `test`-Zeilen fehl, bitte dort stoppen und
weder System-`pip` noch `--break-system-packages` verwenden. Nach der
erfolgreichen Reparatur steht der reguläre Web-Update-Pfad wieder zur
Verfügung.

Python-Abhängigkeiten werden bei einem Release-Wechsel ausschließlich im
gebundenen Benutzer-venv installiert. Auf Debian-Systemen mit PEP 668 wird
deshalb kein System-`pip` und kein `--break-system-packages` verwendet. Fehlt
das Standard-venv, darf der neue Installer nach Installation von
`python3-venv` genau dieses venv im Home des Installationsbenutzers neu
anlegen. Beim direkten ersten Wechsel aus 5.3.2b läuft bis zum
Git-Wechsel noch der alte Prozess; für diesen ersten Schritt muss das
gebundene Benutzer-venv bereits vorhanden sein. Andernfalls bricht der
Altprozess ab und stellt den Ausgangszustand wieder her. Abweichende oder
mehrdeutige venv-Pfade brechen den Updatevorgang ebenfalls ab.

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

## Einmaliger Wechsel von alten Installationen

Der Bootstrap gilt auch für 5.3.2a, V4.0.1 bis V4.0.5 sowie für V3-/ZIP-Stände
ohne `.git`. Diese Stände wechseln zuerst auf die bewusst dafür veröffentlichte
Übergangsbasis 5.3.2b und führen danach deren regulären Updatepfad aus. Lade
das veröffentlichte 5.3.2b-Release-Archiv in ein temporäres Verzeichnis
und prüfe dessen veröffentlichte SHA-256. Notiere außerdem den vollständigen
40-stelligen Commit-SHA und die bestehende HA-/Shadow-Rolle (`off`, `master`,
`slave` oder `shadow`).

```bash
/tmp/e3dc-release/e3dc-bootstrap \
  "$E3DC_INSTALL_PATH" \
  vX.Y.Z \
  0123456789abcdef0123456789abcdef01234567 \
  off
```

Tag, SHA und Rolle werden durch die auf der Release-Seite freigegebenen Werte
und die zuvor geprüfte Rolle ersetzt. Der Launcher verlangt Python 3.10 oder
neuer und beendet sich bei einer älteren Laufzeit, bevor der Zielbaum
verändert wird. Bei einer V3-Installation kann der absolute Zielpfad zum
Beispiel `$HOME/E3DC-Control` sein.

Der Bootstrap akzeptiert nur einen annotierten Tag, dessen aufgelöster Commit
exakt dem angegebenen SHA entspricht. Er sichert zuerst den tatsächlichen
Zielbaum. Erst nach Manifest- und Prüfsummengate werden Git initialisiert und
der neue Release-Stand eingespielt. Ein vorhandener alter Git-Verlauf muss
keinen gemeinsamen Vorfahren mit dem neuen Verlauf besitzen.

## Harte Gates

Sobald der neue Updater selbst läuft, führt er den Wechsel in dieser
Reihenfolge aus:

1. annotierten Ziel-Tag, vollständigen Ziel-SHA und
   `UPDATE_POLICY.json` aus dem verifizierten Commit-Objekt gegen Freigabe,
   Ausgangsrepository und Rolle binden;
2. ein externes, root-eigenes Backup mit vollständigem Manifest, SHA-256,
   Git-Ausgangszustand und dem Zustand kanonischer systemd-Masken erstellen
   und mit einem Transaktionsbeleg erneut prüfen;
3. im versiegelten Normalpfad den txid-, ziel-, rollen-, backup-, bootblock-
   und servicegebundenen ausstehenden Update-Sicherheitsbeleg (`pending`)
   zusammen mit den eigenen dynamischen `00`-Startbedingungen persistieren
   und den Marker vor der ersten Produktmutation armieren;
4. PiGuard und danach alle katalogisierten Writer-/Integrationsdienste unter
   diesem Startschutz stoppen; `inactive/dead`, `MainPID=0`, Backup,
   Repository, privilegierte Konfiguration und den vollständigen Schutzvertrag
   unmittelbar am Mutationsgate erneut prüfen;
5. eine exakt freigegebene historische Storage-Manager-Unit gegebenenfalls
   unter demselben Schutz atomar auf den root-eigenen Vertrag migrieren,
   `daemon-reload` ausführen und das effektive Unit-Bündel erneut lesen;
6. den Installationsbaum auf genau den Ziel-SHA setzen und `HEAD` erneut
   prüfen;
7. den Target-Finalizer aus einem separaten root-eigenen,
   schreibgeschützten Ausführungssnapshot des verifizierten Zielcommits als
   verwalteten systemd-Dienst unter demselben Update-Lock und seiner
   transaktionsgebundenen Lease starten;
8. Webdateien synchronisieren und veraltete Pfade nur über feste Positivlisten
   entfernen;
9. eingefrorene HA-/Shadow-Rolle und Feature-Konfiguration, alle erwarteten
   Dienste, lokale HTTP-Endpunkte und Boot-Sanity hart prüfen;
10. erst danach den Update-Sicherheitsbeleg dauerhaft als `committed`
    bestätigen. Ein Altstand-Rollback ist ab hier verboten; der Finalizer
    entfernt nur seinen exakt eigenen Marker-/`00`-Vertrag. Der äußere Pfad
    entfernt das exakt gebundene Receipt erst nach inaktiver Lease; ein
    unterbrochener Abschluss bleibt auf genau dieses Cleanup begrenzt.

Beim direkten ersten Wechsel aus 5.3.2b bleibt der bereits
gestartete Altprozess bis zum Abschluss aktiv. Nach dem Git-Wechsel importiert
er die neue, dienstneutrale Rechteprüfung; der zielgebundene Finalizer gilt ab
dem anschließend laufenden neuen Updater. Der erste Wechsel behauptet deshalb
keine nachträgliche Ausführung eines Finalizers, den der Altprozess noch nicht
kennt.

Für diesen ersten Hybridwechsel enthält die Zielpolicy ausschließlich die
sieben Pflichtdienste. Der Altprozess erfasst die vor dem Wechsel bereits
installierten Zusatzdienste und die gebundene HA-/Shadow-Rolle. Nur die in der
eingefrorenen Konfiguration aktiven Zusatzdienste werden gestartet;
deaktivierte Zusatzdienste bleiben aus. Eine
vorbereitete Konfiguration allein installiert oder startet keine bislang
fehlende Wallbox-, Wärme- oder Integrationssteuerung. Solche konfigurierten,
aber nicht installierten Zusatzmodule werden im Updateprotokoll genannt und
können nach dem Release-Wechsel bewusst über das Install-Center eingerichtet
werden.

Scheitert ein Gate nach einer Änderung, setzt der Installer den alten Git-Stand
zurück, entfernt bei einem ZIP-Bootstrap die neu angelegte `.git`-Struktur,
stellt das Sicherheits-Backup wieder her und prüft Rolle, Dienste und HTTP
erneut. Ist diese Wiederherstellung nicht vollständig beweisbar, bleiben die
Writer-/Aktor-Dienste gestoppt.

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
