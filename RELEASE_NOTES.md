# E3DC-Control v5.4.2d

E3DC-Control 5.4.2d ist ein eng begrenzter Hotfix für den verifizierten
Dienst-Wiederanlauf und den systemd-Maskenrücklauf des Bare-Metal-Updaters.

## Dienst-Wiederanlauf

- Nach `systemctl enable` und `systemctl restart` bewertet der Updater den
  nachweisbaren Endzustand eines erforderlichen Dienstes. Ein
  Zwischen-Rückgabecode allein gilt nicht mehr als Fehlschlag, wenn die Unit
  am Ende korrekt aktiviert und aktiv ist.
- Bleibt eine erforderliche Unit deaktiviert oder inaktiv, protokolliert der
  Updater Enable-, Restart- und Endzustand und bricht weiterhin fail-closed
  ab.

## Verifizierter Maskenrücklauf

- Eine nicht installierte optionale systemd-Unit wird als legitimer fehlender
  Zustand normalisiert. Eine abweichende textuelle Ausgabe von systemd kann
  deshalb nicht mehr allein einen ansonsten beweisbaren Rücklauf verwerfen.
- Der Schutz wird nicht gelockert: reguläre Unit-Dateien, exakte
  `/dev/null`-Masken, unerwartete Links und unlesbare Zustände bleiben
  getrennt geprüft. Echte Masken- oder Wiederherstellungsabweichungen lassen
  Writer und Aktoren weiterhin sicher gestoppt.

Dieser Hotfix ändert keine EMS-Regelung. Speicher, Direktvermarktung,
Wallboxen, Wärme, Prognose und Hardwareausgänge entsprechen unverändert
5.4.2c. Bare-Metal-Nutzer verwenden den normalen Web- oder Konsolen-Updater.
Docker-Nutzer verwenden nach erfolgreicher Digest-, SBOM-, Provenance- und
Attestierungsprüfung das GHCR-Image `v5.4.2d`; der öffentliche
Docker-Rückfallstand bleibt `v5.3.2b`.

---

# E3DC-Control v5.4.2c

E3DC-Control 5.4.2c ist ein Hotfix für die Priorität ausdrücklich geplanter
Wallbox-Netzladeslots, die festen Octopus-Heat-Tariffenster und die
Docker-Dokumentation.

## Wallbox und Speicherschutz

- Ein frischer, ausdrücklich gültiger Modus-5-Netzladeslot wird nach
  bestandenen harten Schutzprüfungen nicht mehr durch den rein wirtschaftlichen
  Pre-Dump-Floor auf 0 A gesetzt.
- Der Speicher bleibt dabei im geschützten AUTO-Rahmen. Die Fahrzeugladung
  wird nicht aus der Batterie finanziert; eine zulässige Speicherentladung
  berücksichtigt nur den nicht durch PV gedeckten Haus- und
  Wärmepumpenbedarf.
- Nutzer-`Aus`, manuelle und Abort-Sperren, Notstromreserve, Hardware- und
  Netzlimits, Datenvalidität sowie fehlende oder veraltete Slots bleiben
  unverändert vorrangig. Ohne gültigen Slot bleibt der wirtschaftliche
  Pre-Dump-Stopp wirksam.

## Octopus Heat

- Die festen Niedrigtariffenster 02:00–06:00 und 12:00–16:00 werden über eine
  gemeinsame lokale Tarifzeitachse in `Europe/Berlin` abgebildet und benötigen
  den netzdienlichen Eco-Modus nicht.
- Aktuelle Wärmepumpen-, Billigstrom- und Verbraucherfreigaben bleiben
  zwingend. Fehlerhafte, veraltete oder nach einem Tarifwechsel unpassende
  Planartefakte schließen fail-closed.
- Die lokale Tarifachse bleibt vom optionalen Marktpreis getrennt und erzeugt
  keinen erfundenen Börsenpreis.

## Docker

- Das veröffentlichte GHCR-Image bleibt der normale Installationsweg.
- Ein lokaler Entwickler-Selbstbau benötigt einen vollständigen Checkout und
  kann ohne Registry über `e3dc-control:local` und einen Compose-Override mit
  `pull_policy: never` gestartet werden.
- Daten, Logs, root-private ML-Modelle und optionale Prognosebelege behalten
  getrennte Rechte-, Aufbewahrungs- und Backupverträge. Es erfolgt keine
  automatische Volume-Migration.

Der in 5.4.2b korrigierte, versiegelte Alt-Updater-Übergang bleibt
unverändert. Bare-Metal-Nutzer verwenden den normalen Web- oder
Konsolen-Updater. Docker-Nutzer verwenden nach der veröffentlichten
Digest-Prüfung das GHCR-Image `v5.4.2c`; der öffentliche
Docker-Rückfallstand bleibt `v5.3.2b`.

---

# E3DC-Control v5.4.2b

E3DC-Control 5.4.2b ist ein eng begrenzter Kompatibilitäts-Hotfix für
Updateprozesse, die bereits vor dem Wechsel auf den neuen Zielstand gestartet
wurden.

- Erreicht ein solcher Prozess den neuen Finalizer noch direkt über den
  Produktpfad, werden Installationswurzel, Ziel-Commit, Version, Release-Tag
  und alle benötigten Finalizer-Dateien erneut gegen den freigegebenen Commit
  gebunden.
- Die privilegierte Fortsetzung startet ausschließlich aus einem privaten,
  root-eigenen und schreibgeschützten Ausführungssnapshot. Erfolg wird nur mit
  genau einem passenden SHA-/Tag-Marker akzeptiert.
- Eine reine Bereinigungsabweichung nach bereits erfolgreichem Finalizerlauf
  bleibt sichtbar, verdrängt aber nicht den gebundenen Erfolg und löst keinen
  falschen Rollback aus.
- Symlinks, Hardlinks, fremde Eigentümer, gruppen- oder weltbeschreibbare
  Dateien, Byte-, Commit-, Versions- und Kontextabweichungen bleiben harte
  Abbruchgründe.
- Der normale versiegelte Updatepfad bleibt unverändert. Speicher-,
  Direktvermarktungs-, Wallbox-, Wärme-, Prognose- und Hardwaresteuerung
  entsprechen 5.4.2a.

Bare-Metal-Nutzer verwenden weiterhin den normalen Web- oder
Konsolen-Updater. Docker-Nutzer verwenden das veröffentlichte GHCR-Image; der
öffentliche Docker-Rückfallstand bleibt v5.3.2b.

---

# E3DC-Control v5.4.2a

E3DC-Control 5.4.2a ist ein eng begrenzter Hotfix für die
Speicher-Ladefreigabe und den Releasewechsel aus alten, bereits laufenden
Updater-Prozessen sowie für harte Heizstab-`Aus`-Kanten.
Direktvermarktung, Lastspitzenbegrenzung, Wallbox und die fachliche
Prognosediagnose entsprechen unverändert 5.4.2.

## Speicher-Ladefreigabe

- Ein `EMS_USER_CHARGE_LIMIT`-Readback aus frischen, validen
  `POWER_SETTINGS` wird nur dann als reflektierter flüchtiger Laderahmen
  behandelt, wenn `maximumladeleistung` ausdrücklich konfiguriert ist und
  `EMS_USER_CHARGE_LIMIT` sowie
  `EMS_MAX_CHARGE_POWER` strikt weniger als 50 W voneinander abweichen.
  Andernfalls bleibt die USER-Grenze wirksam.
- Liegt der Speicher hinter seiner Ladekurve, öffnet der Manager den Laderahmen
  in `AUTO` nur bei positiver, frischer E3/DC-only-Evidenz bis
  `MAX_CHARGE_POWER`. Unbekannte oder veraltete Pfadzuordnung bleibt
  fail-closed.
- Die Entladung für wechselnden Hausverbrauch bleibt offen. Eine aktivierte
  E3/DC-PV-Ladebegrenzung führt den Rahmen bei zusätzlicher AC-PV weiterhin
  sanft und DC-first auf die frisch belegte interne PV-Leistung nach.
- Der Hotfix erteilt keine Netzladefreigabe, fordert weder `GRID` noch einen
  aktiven Ladebefehl an und verändert keine getrennt freigegebenen
  Preis-/Lastspitzen-Netzladeverträge.
- Nutzer-`Aus`, Notstromreserve, Hardwarelimits, Datenfrische und die
  Ein-Entscheider-Regel bleiben vorrangig.

## Kompatibilität alter Updateprozesse

- Ein aus 5.4.0a gestarteter Updateprozess kann nach dem verifizierten
  Baumwechsel noch die alte Signatur des Service-Helfers im Speicher tragen.
- Ist die optionale PV-Prognosediagnose ausgeschaltet, wird ausschließlich
  deren Sidecar kontrolliert übersprungen. Die Kerndienste werden weiter über
  ihren vollständigen Vertrag installiert.
- Ist die Diagnose ausdrücklich aktiviert, bleibt der Übergang fail-closed:
  Eine unvollständig definierte Unit wird nicht erzeugt und der transaktionale
  Updater stellt den verifizierten Ausgangszustand wieder her.
- Der privilegierte Finalizer startet ausschließlich aus einem separaten
  root-eigenen, schreibgeschützten Ausführungssnapshot des freigegebenen
  Zielcommits. Byte-, Modus-, Eigentümer-, Hardlink-, Symlink- und
  Komponentenabweichungen bleiben harte Abbruchgründe; Metadatenfehler nennen
  jetzt Datei, UID, GID, Modus und Linkzahl.
- Der Ausführungssnapshot dient ausschließlich als geprüfte Codeherkunft.
  Installationslogs, systemd-Units, Notifier-Rechte, Web-Wrapper und
  Sudoers-Einträge werden gegen den zuvor doppelt gebundenen Produktpfad
  erzeugt. Nach dem Entfernen des Snapshots bleibt deshalb kein installierter
  Dienst- oder Hilfspfad auf dessen temporären Ort gebunden.

## Heizstab und Shelly Pro3EM

- Das lokale `PV-AUTO AUS` hält den Heizstab nach dem bestätigten 0-W-/AUS-
  Übergang aus. PV-Überschuss, Pre-Dump und Marktfreigaben können im selben
  oder in einem unveränderten Folgezyklus keine positive Leistung mehr
  anfordern.
- Der Hauptschalter `heizstab=0` sperrt auch alte konfigurierte Endpunkte und
  einen manuellen Vollgasauftrag. Ein zuvor erreichbarer Aktor wird einmal
  sicher auf 0 W beziehungsweise AUS freigegeben.
- Fehlende Shelly-Pro3EM-Steuerfelder verwenden den sicheren Laufzeitstandard:
  Relais-ID `-1`, Relaissteuerung aus. Ein separat freigegebener
  Pro3EM-Wärmepumpenpfad bleibt vom lokalen Heizstab-`PV-AUTO AUS` unabhängig;
  das globale `AUTO AUS` stoppt und hält beide Pfade.

## Installation und Update

Bare-Metal-Nutzer verwenden den normalen Web- oder Konsolen-Updater.
Docker-Nutzer prüfen vor dem Pull das aufgelöste Image und verwenden für einen
bewussten Pin `E3DC_IMAGE_TAG=v5.4.2a`; ohne Pin folgt die Compose-Datei dem
erst nach erfolgreicher Attestierungsprüfung gesetzten Stable-Tag `latest`.
Der öffentliche Docker-Rückfallstand bleibt `v5.3.2b`.

Das veröffentlichte GHCR-Image ist der normale Docker-Installationsweg. Ein
lokaler Selbstbau benötigt einen vollständigen Repository-Checkout als
Build-Kontext; `build: .` in einem ansonsten leeren Installationsordner reicht
nicht aus. Daten-, Log-, ML- und Prognosebeleg-Volumes behalten in diesem
Hotfix ihre getrennten Rechte-, Aufbewahrungs- und Backupverträge. Es findet
keine stille Volume-Migration statt.

---

# E3DC-Control v5.4.2

E3DC-Control 5.4.2 ist ein Funktions- und Stabilitätsrelease für
Speicherplanung, Direktvermarktung, Lastspitzenbegrenzung, Installation und
PV-Prognosediagnose. Schutzgrenzen, Nutzer-`Aus`, Notstromreserve,
Hardwarelimits, Datenfrische und die Ein-Entscheider-Regel bleiben vorrangig.

Die neuen Funktionen **E3/DC-PV-Ladebegrenzung**, **Peak Shaving am
Netzbezug**, **Netz-Nachladung des Lastspitzenpuffers** und
**PV-Prognosediagnose** sind standardmäßig ausgeschaltet. Ein Update aktiviert
sie nicht stillschweigend.

## Speicherplanung und Direktvermarktung

- Der Direktvermarktungsplan deckt den Tag durchgehend mit festen
  15-Minuten-Abschnitten ab. Dadurch ist auch zwischen aktiven Marktfenstern
  eindeutig festgelegt, ob E3DC-Control eingreift oder die normale
  Hausversorgung dem E3/DC überlässt.
- **Speicherplatz halten** ist ein gebundener Ladeblock: Laden bleibt bis zum
  Ende des betreffenden Abschnitts gesperrt, Entladen für Hausverbrauch bleibt
  möglich. Ein bloß künftiges Verkaufsfenster erzeugt keinen allgemeinen HOLD.
- Nach dem letzten geplanten PV-Speicherabschnitt folgt wieder
  **Hausversorgung / AUTO**, sofern kein stärkerer Storage-Manager-Entscheider
  wie Lastspitzenbegrenzung, Preis-Netzladen, Pre-Dump oder eine
  Sicherheitsgrenze aktiv ist.
- Verkaufsfenster werden nach wirtschaftlichem Wert und verfügbarer Energie
  geplant. Notstromreserve, Leistungsgrenzen, Datenfrische und gültige
  Plan-/Slotbindung bleiben harte Voraussetzungen.
- Nicht freigegebene oder nicht ausführbare Kandidaten bleiben Diagnose. Sie
  erzeugen keinen Speicherbefehl.
- Angeforderte flüchtige Speichergrenzen müssen frisch und typisiert
  zurückgelesen werden. Owner, Plan und Slot bleiben bis zur bestätigten
  Wirkung gebunden.
- Ein zusätzlicher lückenloser **DV-Planer-Shadow** bildet die fünf
  Aktionsklassen Hausversorgung, PV-Speichern, Ladeblock, Netzladen und
  wirtschaftlichen Verkauf als eigenen validierten Diagnosevertrag ab. Er
  bleibt vollständig wirkungslos, verändert keine produktive Plan-/Slot-ID
  und wird weder von Phase 5 noch vom Storage Manager als Aktorquelle gelesen.

### Optionale AC-Speicherroute

Für Anlagen mit zusätzlichem AC-Wechselrichter gibt es eine getrennte,
standardmäßig ausgeschaltete Freigabe:

- `Aus` plant weiterhin ausschließlich E3/DC-DC-PV als Speicherquelle.
- `Nur notwendige Reserve sichern` und `Bei wirtschaftlichem Vorteil
  freigeben` sind getrennte bewusste Nutzerentscheidungen.
- E3/DC-DC bleibt vorrangig. Die AC-Route benötigt einen gültigen
  Topologievertrag und eine belegte DC-Unterdeckung.
- Die Freigabe erlaubt kein Netzladen. Fehlende oder veraltete Nachweise
  sperren den Pfad.

## Sanfte Ladebegrenzung auf E3/DC-PV

Mit **Speicher → Laden an E3DC-PV koppeln** können Kurvenladung und
DV-PV-Speichern auf die aktuell am E3/DC gemessene PV-Leistung begrenzt
werden:

- Der Storage-Simulator liefert weiterhin die fachliche Obergrenze.
- Die wirksame Ladegrenze ist höchstens die frisch und gültig ermittelte
  E3/DC-PV-Leistung. Leistung eines zusätzlichen AC-Wechselrichters erhöht
  diesen Rahmen nicht.
- E3/DC bleibt in AUTO und darf bei wechselnden Hauslasten jederzeit entladen.
  Die Regelung erteilt keinen dauernden harten Ladeauftrag.
- Sinkt die E3/DC-PV-Leistung, wird der flüchtige Laderahmen nachgeführt.
  Fehlt der gültige PV-Split oder ist er veraltet, werden diese PV-basierten
  Ladepfade sicher auf 0 W begrenzt.
- Dynamische Grenzen werden nur über flüchtige EMS-Power-Settings gesetzt.
  Dauerhafte Geräteeinstellungen werden nicht zyklisch verändert.

Die Option ist auch für Anlagen ohne Direktvermarktung nutzbar, wenn ein
zusätzlicher AC-Wechselrichter vorhanden ist. Sie ist ein DC-first-Rahmen und
keine physikalische Garantie, dass im Gerät zu jedem Zeitpunkt ausschließlich
DC-Leistung fließt. Preis- und ausdrücklich freigegebenes Netzladen besitzen
eigene Verträge und werden durch diese Option nicht umgedeutet.

## Wallbox-Ladeplanung bei wiederkehrenden Tarifen

Feste Tarife, Octopus Heat und frei konfigurierte Spezialtarife besitzen ein
vollständiges tägliches Tarifprofil. Ein manuelles Ladefenster wie
`00:00–05:00` mit fünf Stunden Ladezeit kann deshalb bereits für die kommende
Nacht erzeugt werden, auch wenn noch keine morgigen EPEX-Slots vorliegen.

Der konfigurierte Tarifpreis bleibt dabei vom optional vorhandenen Marktpreis
getrennt. Tibber, aWATTar und andere dynamische Tarife bleiben ohne
veröffentlichte Zukunftspreise strikt gesperrt. Kann der private Planer keinen
sicheren Kandidaten erzeugen, nennt die Weboberfläche einen validierten
deutschen Grund und übernimmt weder Konfiguration noch Teilplan.

## Peak Shaving am Netzbezug

Die neue Lastspitzenbegrenzung bewertet den mittleren Netzbezug in festen
Zähler-Viertelstunden:

- Der gewünschte maximale 15-Minuten-Netzbezug, ein Sicherheitsabstand und eine
  optionale Entladeobergrenze sind konfigurierbar.
- Leistungs- und SoC-Hysterese, maximale Messlücke und Freigabe-Entprellung
  verhindern instabile Grenzwechsel.
- Ein eigener Speicherpuffer kann oberhalb der physischen Notstromreserve
  reserviert werden. Die Notstromreserve selbst bleibt unverfügbar.
- Beim Begrenzen und Halten arbeitet E3/DC weiter in AUTO. Der Storage Manager
  setzt nur flüchtige Lade- oder Entladerahmen und fordert keine Einspeisung
  ins Netz an.
- Das Nachladen des Lastspitzenpuffers aus dem Netz ist eine getrennte
  ausdrückliche Freigabe. Es bleibt innerhalb der laufenden Viertelstunden-,
  Hausanschluss- und Hardwaregrenzen und nutzt dafür vorübergehend den
  ausdrücklich angeforderten Netzlademodus; standardmäßig ist es aus.

Bei fehlender oder unterbrochener Netzpunkthistorie bleibt die Regelung
passiv. Aus einer unvollständigen Viertelstunde wird kein aggressiver
Speicherauftrag abgeleitet.

## PV-Prognosediagnose

Die optionale Diagnose vergleicht vorhandene PV-Prognosen mit abgeschlossenen
nativen E3/DC-DC-Historienslots:

- Ein eigener niedrig priorisierter Diagnosedienst arbeitet getrennt von
  Live-Daten, Prognoseerzeugung und Speicherregelung.
- Bei `Aus` erfolgen weder E3/DC-Historienabfrage noch Datenbankschreibzugriff.
- Ausgewertet werden ausschließlich typisierte, abgeschlossene
  15-Minuten-Slots derselben Topologie-Revision. Fehlende Werte bleiben
  unbekannt und werden nicht als Messnull behandelt.
- Die Oberfläche zeigt verständliche Diagnosewerte:
  **Trefferabweichung**, **Richtungsversatz**,
  **energiegewichtete Gesamtabweichung** und **Vergleichsabdeckung**.
- Bis mindestens 96 ertragsrelevante Slots aus mindestens sieben
  Vergleichstagen vorliegen, bleiben die Ergebnisse als vorläufig
  gekennzeichnet.
- Es gibt keine automatische Modellauswahl, keine Konfigurationsänderung und
  keine Rückwirkung auf Speicher- oder Verbraucherentscheidungen.
- Der Konfigurationseditor verwaltet PV-Flächen, Wechselrichtergruppen und
  Provider-Bindungen als versionierten Topologievertrag. Fehlende Messwerte
  bleiben unbekannt; eine neue Topologie-Revision verhindert, dass ein alter
  PV-Split auf eine geänderte Anlage übertragen wird.

Die private SQLite-Datenbank liegt außerhalb des Webverzeichnisses unter
`/var/lib/e3dc-control/forecast-evidence`. Sie besitzt Größen- und
Aufbewahrungsgrenzen. Das Webportal erhält nur eine kleine sanitierte
Zusammenfassung.

## Installation, Update und Docker

- Eine frische Installation wird nicht mehr wie eine bestehende Anlage ohne
  HA-/Shadow-Rolle behandelt. Nur beim erstmaligen Erzeugen der Konfiguration
  wird die Einzelanlagenrolle `off` vorbelegt.
- Vorhandene Anlagen ohne gültige Rollenbindung werden nicht stillschweigend
  umgedeutet. Widersprüchliche Bestände bleiben fail-closed; sicher erkennbare
  unvollständige Erstinstallationen können über den vollständigen Installer
  fortgesetzt werden.
- Fehler bei Paketen, Konfiguration, Webportal, Rechten oder Diensten werden bis
  zum Menü- und Prozess-Exitcode weitergegeben. Ein fehlgeschlagener Schritt
  wird nicht mehr als erfolgreiche Installation angezeigt.
- Ein aus 5.4.0a gestarteter Altprozess kann nach dem verifizierten Git-Wechsel
  mit seinem bereits gecachten alten Backup-Validator fortfahren. Die neue
  Rechteprüfung erkennt dessen Signatur, übergibt keine unbekannten Argumente
  und führt in dieser Altgeneration keine Metadatenmutation aus. Unsichere
  ML-Sperrdateien bleiben weiterhin ein harter Abbruch.
- Der direkte Aufruf `--install-all` verwendet dieselbe Zustands- und
  Rollenprüfung wie der interaktive Menüpunkt.
- Der optionale Diagnosedienst gehört nicht zu den sieben
  Install-Center-Pflichtdiensten. Bei Updates wird er nur erhalten und
  gestartet, wenn er vorher installiert und in der eingefrorenen Konfiguration
  aktiviert war.
- Docker verwendet getrennte private Volumes für Lernmodell und
  Prognosediagnose. Nach dem Ein- oder Ausschalten der Diagnose ist ein
  Containerneustart erforderlich.
- Der Docker-Rückfallstand ist zusätzlich zum Tag an seinen OCI-Index-Digest
  gebunden. Vor `pull` und `up` muss Compose exakt das erwartete
  Rückfall-Image auflösen.

### Hinweis für ältere Installationen

Der dokumentierte erste Hybridwechsel aus 5.3.2b und die einmalige
ML-Lock-Reparatur für Updater bis einschließlich 5.4.1c bleiben unverändert.
5.4.2 ergänzt für bereits laufende 5.4.0a-Updater die danach benötigte
Importcache-Kompatibilitätsbrücke. Diese historischen Übergangsschritte dürfen
nicht durch einen manuellen `git pull` ersetzt werden. Details stehen in
`doc/Update.md`.

Docker-Nutzer prüfen das konfigurierte Image und recreaten erst nach einem
erfolgreichen Pull:

```bash
(
  set -euo pipefail
  docker compose config --images
  docker compose pull e3dc-control
  docker compose up -d --force-recreate e3dc-control
  docker inspect e3dc-control --format '{{.Config.Image}} {{.State.Status}}'
  docker exec e3dc-control cat /app/pi/Install/VERSION
)
```

Ein fester Eintrag `E3DC_IMAGE_TAG` bleibt absichtlich fest. Für v5.4.2 lautet
der Pin `E3DC_IMAGE_TAG=v5.4.2`; ohne Pin folgt die Compose-Datei dem erst nach
erfolgreicher Attestierungsprüfung gesetzten Stable-Tag `latest`.

Der öffentliche Docker-Rückfallstand bleibt `v5.3.2b`. Dieser Stand ist nicht
als Bare-Metal-Programm-Rückfall freigegeben; dort bleibt ein verifiziertes
Datei-Backup der sichere Rückweg.

## Oberfläche

- Der Tarifbereich trennt Direktvermarktung, Preisfunktionen und
  Lastspitzenbegrenzung in verständliche Abschnitte. Beschreibungen erklären
  die Wirkung von `Ein` und `Aus` in Alltagssprache.
- Bei zwei Wallboxen stehen die jeweiligen Einstellungen über den gesamten
  Bereich in zwei Spalten; gemeinsame Werte liegen über beiden Spalten. Bei nur
  einer Wallbox wird die volle Breite genutzt.
- Die Speicheranzeige unterscheidet den wirksamen Vertrag
  **Hausversorgung / AUTO**, **Speicherplatz halten**, **PV-Speichern** und
  **Verkaufen**. Redundante HOLD-Angaben sind zusammengeführt.
- Der Service-Worker verwendet einen neuen 5.4.2-Cache-Namensraum, damit alte
  JavaScript- und CSS-Dateien nach dem Update eindeutig verworfen werden.
