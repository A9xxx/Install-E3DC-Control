# E3DC-Control v5.4.3g

E3DC-Control 5.4.3g bringt das eng begrenzte System-Update ins Dashboard
zurück und härtet die Matter-Laufzeit. Speicher-, Wallbox-, Wärme- und
Direktvermarktungsregelung bleiben gegenüber 5.4.3f unverändert.

## Sicheres Web-Update

- Das Dashboard darf ausschließlich einen argumentlosen, root-eigenen
  Systemjob starten. Pfad, Installationsnutzer, unveränderter Ausgangscommit
  und dessen veröffentlichter annotierter Stable-Tag werden erneut gebunden.
- Freie Installer-Aktionen, Pfade, Tags, Reparaturen, Neuinstallationen und
  Rückfälle bleiben im Web gesperrt. Der eigentliche Updater läuft aus einem
  root-eigenen, versiegelten Snapshot und bleibt im Dashboard beobachtbar.
- Jede verifizierte Ziel-Policy muss die gebundene Rechte- und
  Root-Launcher-Aktualisierung ausführen. Ein Release kann deshalb nicht
  erfolgreich enden und zugleich einen Launcher des Vorgängerstands
  zurücklassen.
- Beim ersten Wechsel aus 5.4.2 beendet die Kompatibilitätsbrücke einen zu
  lange laufenden Ziel-Finalizer einschließlich seiner Kindprozesse sicher
  vor dem unveränderbaren 900-Sekunden-Limit des alten Updaters. Erst danach
  darf dessen Wiederherstellung beginnen.
- Der Standardlink `lib64 -> lib` eines eindeutig gebundenen Benutzer-venv
  wird akzeptiert. Absolute oder fremde Linkziele, falsche Eigentümer und
  ACL-Abweichungen bleiben fail-closed.
- Bestehende Installationen bis einschließlich 5.4.3f besitzen den neuen
  Launcher noch nicht. Der erste Wechsel auf 5.4.3g erfolgt deshalb einmalig
  über die administrative Konsole; danach steht der Dashboard-Weg bereit.

## Matter-Härtung

- Die Bridge verwendet die offizielle Matter-Kompatibilitätsschicht 0.12.6
  statt der alten verwundbaren Abhängigkeitskette. Nicht benötigte
  Laufzeitpakete wurden entfernt; der Laufzeit-Audit meldet keine bekannte
  npm-Schwachstelle.
- Web- und Matter-Installer verlangen Node.js ab Version 18 und installieren
  exakt aus der Lockdatei mit `npm ci --omit=dev --ignore-scripts`.
- Das bisherige Storageformat und bestehende Kopplungen bleiben erhalten. Die
  inkompatible Migration auf das neue `ServerNode`-Storageformat ist nicht
  Teil dieses Releases.
- Im Bookworm-Container werden D-Bus und Avahi ohne nicht vorhandenes
  SysV-Avahi-Skript gestartet und vor dem Bridge-Start auf echte
  mDNS-Bereitschaft geprüft.

## Docker-Veröffentlichung

- Der kopierte Produktbaum wird unabhängig von den Dateimodi des Build-Hosts
  root-eigen und ohne Schreibrechte für Gruppe oder Andere versiegelt.
- Der gebaute Multi-Arch-Kandidat wird real gestartet und erst nach Digest-,
  SBOM- und Provenance-Nachweis auf `v5.4.3g`, `5.4.3g` und `latest`
  befördert.

---

# E3DC-Control v5.4.3f

E3DC-Control 5.4.3f ist ein Wartungsrelease für Bare-Metal-Update,
Erstinstallation, openWB-Pro und Direktvermarktung. Die harten Nutzer-,
Hardware-, Reserve- und Datenfrischegrenzen bleiben unverändert vorrangig.

## Installation und Update

- Ein eindeutig gebundenes bestehendes Benutzer-venv kann historische
  Gruppen-Schreibrechte sicher verlieren. Eigentümer, Inodes, Links und ACLs
  werden vor der Änderung geprüft; danach muss der venv-Vertrag erneut gelten.
- Die Bare-Metal-Logrotate-Datei wird als reines LF-UTF-8 atomar ersetzt und
  vor sowie nach der Projektion mit dem echten Systemparser geprüft. Ein
  fehlerhafter Endstand stellt das gebundene Preimage wieder her.
- Eine frische Bookworm-Installation prüft die Apache-Laufzeitpfade erst nach
  der atomaren Veröffentlichung des Webbaums. Die vorherigen Paket-, Modul-
  und Apache-Gates bleiben zwingend.

## Wallbox und Direktvermarktung

- Beim Wechsel auf `Aus / autonom` wird nur die alte Evidenz eines
  openWB-Pro-Startversuchs verworfen. Stecksession, Ladeende-Latch,
  Manager-Nullanker und Phasenreservation bleiben erhalten.
- Das DV-Exportbudget wird je lokalem `Europe/Berlin`-Kalendertag geführt;
  Preisplateaus über Mitternacht werden an der Tagesgrenze getrennt.
- Die Speicherhistorie archiviert Auswahl, Anforderung, Ausgabe und
  Hardwarewirkung einer DV-Aktion getrennt. Fehlende Evidenz bleibt unbekannt.

## Docker-Veröffentlichung

- Der gebaute Multi-Arch-Kandidat wird real gestartet und erst nach Digest-,
  SBOM- und Provenance-Nachweis auf `v5.4.3f`, `5.4.3f` und `latest`
  befördert.

---

# E3DC-Control v5.4.3e

E3DC-Control 5.4.3e repariert den offiziellen Bare-Metal-Bootstrap und den
verifizierten Update-Rückweg. Speicher-, Wallbox-, Wärme- und
Direktvermarktungsregelung bleiben unverändert.

## Bootstrap und Rollenbindung

- `e3dc-bootstrap` bindet einen normalen lokalen Installationsnutzer und reicht
  ihn auch beim Wechsel über `sudo` ausdrücklich an den Ziel-Updater weiter.
- Fehlt auf einem ausdrücklich mit Tag, Commit-SHA und Rolle `off` gebundenen
  Einzelknoten der persistente Rollenanker, wird er erst nach Nutzerfreigabe,
  verifiziertem Backup und bestätigter Aktorruhe einmalig erzeugt.
- HA- und Shadow-Rollen werden nicht aus einer Web-Konfiguration geraten. Sie
  benötigen weiterhin einen bereits passenden, root-geschützten Rollenanker.

## Wiederherstellung

- Eine auf der internen Positivliste gebundene, nicht installierte
  Kompatibilitäts-Unit darf beim Rückweg als `not-found` bestätigt werden. Ein
  erwarteter Maskenzustand oder eine fremde Unit wird dadurch nicht geöffnet.
- Root-eigene, reguläre systemd-Units mit Modus `0600`, `0640` oder `0644`
  können sicher gelesen und aktualisiert werden. Ein Rollback stellt den
  ursprünglichen Modus exakt wieder her; schreibbare oder verlinkte Dateien
  bleiben gesperrt.

## Docker-Veröffentlichung

- Der aktuelle Stable-Kandidat wird nach erfolgreichem Build, Starttest,
  Digest-, SBOM- und Provenance-Nachweis wieder auf `latest` befördert.

---

# E3DC-Control v5.4.3d

E3DC-Control 5.4.3d behebt den Containerstart nach dem Update auf 5.4.3.

## Docker-Start

- Der Production-Container bindet seine eigene Root-Installationsrolle nur bei
  aktivem Docker-Modus und dem exakten Produktpfad `/app/pi/Install`.
- Die Sicherung und Migration der V4-Konfiguration läuft dadurch wieder durch.
- Bare-Metal-Installationen erhalten keine zusätzliche Root-Freigabe.
- Vor der OCI-Beförderung wird der gebaute Kandidat künftig real gestartet und
  muss die Konfigurationsmigration mit einer echten tmpfs-RAM-Disk verlassen.

Wer wegen Watchtower bereits zurück auf 5.4.2d gewechselt hat, kann diesen Pin
bis zur erfolgreich veröffentlichten Version 5.4.3d beibehalten.

---

# E3DC-Control v5.4.3c

E3DC-Control 5.4.3c behebt den letzten reproduzierten Abbruch der
Bare-Metal-Installation im Notifier-Dienstvertrag.

## Notifier-Transaktion

- Das temporäre systemd-Start-Drop-in des Notifier-Installers wird jetzt mit
  demselben gebundenen Unit-Snapshotformat wie die übrigen Drop-ins geprüft.
- Der eigene Schutzblock wird dadurch nicht länger als fremder Drift gewertet.
- Installation, Dienststart und ein notwendiger Rückfall bleiben weiterhin
  eindeutig, transaktional und fail-closed.

Dieser Hotfix enthält außerdem 5.4.3a und 5.4.3b und ändert keine
EMS-Regelung.

---

# E3DC-Control v5.4.3b

E3DC-Control 5.4.3b ergänzt den Bare-Metal-Updatehotfix um den
standardkonformen Debian-Lockroot.

## Debian-Lockroot

- Ein root-eigenes `/run/lock` mit Modus `1777` und gesetztem Sticky-Bit wird
  als sicherer gemeinsamer Lock-Namensraum akzeptiert.
- Die eigentliche Transaktionsdatei bleibt root-eigen, `0600`, regulär,
  einfach verlinkt, nofollow-gebunden und exklusiv gesperrt.
- Ein weltbeschreibbares `/run/lock` ohne Sticky-Bit bleibt fail-closed.

Dieser Hotfix enthält außerdem die Benutzerübergabe aus 5.4.3a und ändert
keine EMS-Regelung.

---

# E3DC-Control v5.4.3a

E3DC-Control 5.4.3a ist ein eng begrenzter Hotfix für den versiegelten
Bare-Metal-Updateübergang von 5.4.3.

## Bare-Metal-Update

- Der bereits lokal geprüfte Installationsbenutzer wird jetzt über beide
  versiegelten Prozesswechsel an den Ziel-Updater und den Target-Finalizer
  weitergegeben.
- Dadurch kann der Finalizer die lokale Installer-Rolle bestätigen, obwohl
  `installer_config.json` als lokale Betriebsdatei bewusst nicht im
  unveränderlichen Commit-Snapshot enthalten ist.
- Ziel-Commit, Stable-Tag, HA-Rolle, Backup, Aktorruhe, Dienste und
  Gesundheitsprüfungen bleiben unverändert fail-closed gebunden.

Dieser Hotfix ändert keine EMS-, Speicher-, Wallbox-, Wärme- oder
Direktvermarktungsregelung.

---

# E3DC-Control v5.4.3

E3DC-Control 5.4.3 bündelt Verbesserungen für Installation, Docker,
Speicher- und Direktvermarktungssteuerung, Wallboxen, Wärmeverbraucher und die
Darstellung im Dashboard. Fehlende, veraltete oder widersprüchliche Daten
führen weiterhin zu einem sicheren Zustand und nicht zu einer zusätzlichen
Leistungsfreigabe.

## Installation und Update

- Eine frische Installation auf Raspberry Pi OS Bookworm führt Paket-,
  Apache-, Konfigurations-, RAM-Disk-, Web- und Dienstschritte in einer festen
  Reihenfolge aus. Ein Fehler beendet die Installation verständlich; ein
  vorhandener funktionierender Zustand bleibt erhalten beziehungsweise wird
  bei einem Abbruch wiederhergestellt.
- Der Updater bindet den freigegebenen Zielstand vor Backup und Dienststopp
  eindeutig an Version, Herkunft und Anlagenrolle. Fortschritt und
  Lebenszeichen bleiben auch auf langsameren Raspberry Pis sichtbar.
- Ist dieselbe Version bereits vollständig installiert, bleibt ein normales
  Update ohne Unterbrechung wirkungslos. Eine Reparatur oder Neuinstallation
  derselben Version muss ausdrücklich bestätigt werden.
- Der bisherige allgemeine Web-/sudo-Pfad für administrative Produktänderungen
  entfällt. Installation, Reparatur und Rückfall werden über den geschützten
  Konsolenweg ausgeführt; die Weboberfläche kann den laufenden Vorgang weiter
  anzeigen.
- Beim ersten Wechsel aus 5.4.2d gilt einmalig noch die Zeitgrenze des dort
  gestarteten Updaters. Danach greift der neue, phasenbezogene Updatevertrag.

## Docker

- Docker-Installation und -Update werden eindeutig als Aktionen auf dem
  Docker-Host behandelt. Der Host-Helfer prüft Architektur, Anlagenrolle,
  vorhandene Instanzen und konkurrierende Dienste vor einer Änderung.
- Ein neues Image wird vollständig gestartet und anhand der tatsächlich
  gewählten Dienste geprüft. Bei einem fehlerhaften Kandidaten wird dieser
  wieder gestoppt.
- Unveränderte offizielle Compose-Dateien aus 5.4.2 bis 5.4.2d und die
  bekannte Installer-Variante werden sicher auf den neuen Compose-Vertrag
  übernommen. `.env` und vorhandene Daten-, Log-, ML- und Forecast-Speicherorte
  bleiben erhalten. Angepasste oder mehrdeutige Installationen werden zur
  manuellen Prüfung angehalten statt automatisch überschrieben.
- Unterstützt werden 64-Bit-ARM und AMD64. 32-Bit-ARM wird vor dem Download
  verständlich abgelehnt; Dateien sind auch für rootless Containerlaufzeiten
  mit gültigen Besitzern vorbereitet.
- Die Compose-Vorgabe begrenzt und rotiert die Docker-Engine-Logs.
  Automatische Watchtower-Updates bleiben eine bewusste Zusatzoption.

## Speicher und Direktvermarktung

- Marktpreisabhängiges Netzladen wird nur bei einer vollständigen, frischen
  Gesamtunterdeckung geplant. Energiemenge, Leistung und Startzeit richten
  sich nach dem belegten Bedarf.
- Speicherreserve, Sollkurve, Hausversorgung und Verbraucherbudgets bleiben
  getrennt. Fehlende oder abweichende E3/DC-Rückmeldungen öffnen keinen
  zusätzlichen Lade- oder Entladerahmen.
- Storage Manager, Direktvermarktung, Wallboxführung und externe E3/DC-Regler
  verwenden einen gemeinsamen Ownervertrag. Spätere Nebenpfade können eine
  stärkere oder bereits bestätigte Speicherentscheidung nicht verdrängen.
- Bei aktiver Direktvermarktung öffnet nur ein aktuell gültiger und vollständig
  gebundener Abschnitt „PV speichern“ die Speicherladung. Andere Abschnitte
  halten den Laderahmen geschlossen, ohne die geschützte Hausversorgung aus
  dem Speicher zu erzwingen.
- Ein Preis von genau 0 ct/kWh gilt nicht als Negativpreis. Rohbörsenpreis,
  Abrechnungspreis und erwarteter Nettoerlös werden getrennt bewertet und
  angezeigt.
- Schaltzustände eines angebundenen Zusatzwechselrichters werden vor und nach
  einem Befehl zurückgelesen. Unbestätigte Zustände bleiben sichtbar; eine
  Gegenschaltsperre schützt das Relais vor Flattern.
- Plan, freigegebene Aktion und tatsächliche Wirkung werden getrennt
  dargestellt. Das Dashboard zeigt den finalen Speicherzustand und dessen
  tatsächlichen Ausführungseigentümer aus einer gemeinsamen Quelle.

## Wallbox und openWB

- Eine neue Stecksession wird auch während Pause, „Aus“ oder Wartephasen
  erkannt. Der erste Start erfolgt mit dem vorgesehenen Ladestrom; Wake-up-
  Versuche folgen erst nach einer erfolglosen Antwortfrist.
- Eine kurze 0-W-Rückmeldung beendet eine openWB-Ladung nicht mehr. Ein
  Ladeende muss innerhalb derselben Stecksession durch mehrere frische
  Rückmeldungen bestätigt werden.
- Bei der openWB Pro beginnen Warte- und Schutzzeiten erst nach einem
  tatsächlich ausgeführten Phasenbefehl. Die Schutzzeit verhindert nur einen
  weiteren Phasenwechsel und nicht den bestätigten Wiederanlauf.
- Eine frische, zur aktuellen Stecksession passende openWB-Gesamtreichweite
  hat Vorrang. Eine berechnete Profilreichweite dient nur noch als Rückfall,
  wenn kein verlässlicher openWB-Wert vorliegt.
- Genau ein Wallbox Manager besitzt den Hardwareausgang. Deaktivierte
  Ladepunkte, alte Cachewerte und frühere Web-Stellwege können keine
  zusätzliche Wallbox oder einen konkurrierenden Befehl erzeugen.
- Leistung wird als gemeinsames Wattbudget verteilt und anhand der bestätigten
  Phasenzahl je Wallbox übersetzt. Gerätegrenzen, Hausanschluss,
  Mindestleistung, Lastspitzenbegrenzung und Nutzerpriorität bleiben
  vorrangig.

## Wärme

- Wallboxen, Wärmepumpe und Heizstab erhalten getrennte Teilbudgets aus einem
  gemeinsamen Leistungsrahmen. Die eingestellte Verbraucherpriorität
  entscheidet; ungenutzte Leistung wird im nächsten Zyklus wieder
  freigegeben.
- Der Luxtronik-Anlauf berücksichtigt Pumpenvorlauf und Verdichterstart in
  einem festen Anlauffenster. Danach wird nur noch die tatsächlich gemessene
  Leistungsaufnahme reserviert.
- Der Shelly-Pro3EM-Pfad bindet Messung, Relaiszustand und Aktor-Owner an
  frische Rückmeldungen. Fehlende, veraltete oder konkurrierende Daten lösen
  keinen neuen Start aus.
- Mindestlaufzeiten, Startwächter und andere Schutzzeiten bleiben über
  Dienstneustarts und Änderungen der Systemuhr erhalten.
- Stiebel-Eltron-Zustände werden anhand der dokumentierten Statusbits
  ausgewertet. Betriebsmodus 5 allein erscheint nicht mehr als aktive
  Warmwasserbereitung; veraltete oder fremde Daten werden nicht als aktuelle
  Anlage angezeigt.
- Ein gültiges Toshiba-Sitzungstoken wird ausschließlich im Arbeitsspeicher
  wiederverwendet. Neue Anmeldungen und Wiederholungen bei Ratenbegrenzung
  erfolgen kontrolliert, ohne das Token dauerhaft zu speichern.

## Dashboard und Prognose

- Bei aktiver Direktvermarktung zeigt das Dashboard nur den DV-Fahrplan; ohne
  Direktvermarktung nur die Standardprognose. Auch im Ladekurvenfenster
  erscheint ausschließlich die zum Betriebsmodus passende Kurve.
- Der aktuelle Batterie-SoC wird als eigener, frischer Messpunkt dargestellt
  und verändert keine geplante Ladekurve. Fehlt ein verlässlicher Messwert,
  bleibt der Punkt leer.
- Plan-, DV- und Diagnosedaten werden je Anfrage einmal verarbeitet. Weniger
  Achsenmarken, sichtbare Tageswechsel und vollständige Datumsangaben machen
  mehrtägige Diagramme übersichtlicher.
- Die SoC-Historie blockiert den ersten Diagrammaufbau nicht mehr. Verspätete
  Antworten oder ein Moduswechsel können keine ältere Kurve oder Anzeige über
  einen neueren Zustand legen.
- Nach PWA-Standby, Seitenrückkehr oder erneuter Netzwerkverbindung wird eine
  alte Liveabfrage ersetzt. Teilweise WebSocket-Daten löschen keine weiterhin
  gültigen Plan- und Zielkurven.
- Gebündelte Liveabfragen, wiederverwendete Planstände und begrenzte
  Historienzugriffe reduzieren unnötige Rechen-, PHP- und Datenträgerlast.

## Sicherheit und Rückfall

- Vor einem Update wird der tatsächliche Dienstzustand gebunden. Bei einem
  Abbruch wird genau dieser Zustand wiederhergestellt; ohne vollständigen
  Rollen-, Dienst- und Gesundheitsnachweis bleiben Writer und Aktoren sicher
  gestoppt.
- Pro Domäne bleibt genau ein fachlicher Entscheider und ein Hardwareausgang
  wirksam. Doppelprozesse, fremde Startpfade sowie unlesbare oder
  widersprüchliche Zustände werden vor der Ansteuerung blockiert.
- Bare-Metal- und Docker-Dienste starten nur mit einer echten flüchtigen
  RAM-Disk. Ein falscher Mount oder ein Rückfall auf das Root-Dateisystem
  stoppt die betroffenen Produktdienste, während Reparaturwege erreichbar
  bleiben.
- Prognose-, Shadow- und Wärme-Diagnosen bleiben ohne Hardwarewirkung.
  Unvollständige Evidenz wird sichtbar gekennzeichnet und kann keine
  Speicher-, Wallbox- oder Wärmefreigabe erzeugen.
- Schreibzugriffe auf SD-Karte und SSD werden durch gebündelte Status-,
  Historien- und Datenbankaktualisierungen reduziert. Sicherheitsrelevante
  Übergänge bleiben dennoch unmittelbar nachvollziehbar.

---

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

> **Historischer Hinweis für 5.4.2:** Der folgende Befehlsblock dokumentiert
> den damaligen Ablauf und ist nicht für aktuelle Installationen oder Updates
> bestimmt. Aktuelle Stände verwenden den sicheren Host-Helfer aus dem
> [Docker-Updateweg](doc/Docker_Dokumentation.md#3-updates-und-optionaler-watchtower).

Docker-Nutzer prüften damals das konfigurierte Image und erzeugten den Container
erst nach einem erfolgreichen Pull neu:

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
