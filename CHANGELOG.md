# 📘 Changelog

Dieser Changelog dokumentiert die nutzerrelevante Produktgeschichte aller veröffentlichten Versionen. Personen-, Anlagen- und interne Entwicklungsbezüge wurden eng anonymisiert; technische Änderungen und ihre Nutzerwirkung bleiben erhalten.

## 🙏 Danksagung

Danke an die Community für Rückmeldungen, Praxiserfahrungen und die gemeinsame Weiterentwicklung. Historische Einzelzuordnungen werden in diesem bereinigten Changelog nicht geführt.

## [5.4.4a] – 2026-08-19

### 🚗 Wallbox-Regelung & Phasenwechsel

- **Robuster Phasenwechsel und verlässlicher Wiederanlauf:** Ein potenzieller Parameterfehler (`started_ts`) im Wire-Cooldown nach Phasenwechseln wurde behoben. Telemetrie und physische Zielphasen-Readbacks werden vor der Lease-Ablaufprüfung ausgewertet, sodass bestätigte Umschaltungen ohne Verzögerung oder Deadlock direkt in die Freigabe des Ladestroms übergehen.
- **Saubere Recovery-Autorisierung bei Dienstneustarts:** Startet der Wallbox Manager während oder kurz nach einem Phasenwechsel neu, wird der Zustand anhand des bestätigten Hardware-Readbacks sofort wiederhergestellt, anstatt den Ladestrom fälschlicherweise in einer Dauersperre zu halten.
- **Entstörung der Start- und Haltezustände:** Im Hold-Current-Enforcement-Vertrag wurde ein Deadlock behoben, bei dem ein ruhendes Fahrzeug während der PV-Startvorbereitung fälschlicherweise gestoppt und das Startintegral zyklisch gelöscht wurde. Wiederholte Starts im selben Steckzyklus werden nach erfolgreicher Ladung nicht mehr fälschlicherweise als Startablehnung gewertet.
- **Zuverlässige E3DC- und openWB-Aktorik:** Die Stop-Verriegelung für E3DC-Wallboxen wurde entprellt, sodass neue Start-Toggles bei anliegendem Budget sofort übertragen werden. Das Speichern von openWB-Parametern über die Web-Oberfläche schlägt nicht mehr durch redundante Dateirechte-Prüfungen auf gemeinsamen Lockdateien fehl.
- **Stabilität auf der Wallbox-Bedienseite:** Dienstneustarts aus `Wallbox.php` werden zuverlässig quittiert und unnötige Dateisystemzugriffe auf geschützte Konfigurationsdateien unterbunden.

### 🔋 Speicher- und Ladekurvenführung

- **Beseitigung des 3-Minuten-Taktens bei aktiver Ladekurve:** In der Ladekurven-Haltefunktion (`parallel_curve_auto_hold`) wurde ein fehlerhafter Freilauf-Timeout behoben. Hält die Ladekurve den Speicher am Vormittag auf 0 W, bleibt diese EMS-Ladegrenze zustandstreu aktiv und kippt nicht mehr nach 90 Sekunden unbegründet in einen 3-kW-Freilauf.
- **Ruhige Batterieladung bei laufender Fahrzeugladung:** Die Drossellogik für DC-gekoppelte Speicher wurde geglättet, um sprunghafte Schwingungen zwischen Speicherladung und Netzeinspeisung bei schnellen Lastwechseln zuverlässig zu verhindern.

### 📊 Live-Verbrauch & Visualisierung

- **Glitch-Schutz für die Hausverbrauchsberechnung:** Fällt der berechnete Hausverbrauch durch sporadische 0-W-RSCP-Frames oder Messwert-Glitches kurzzeitig unter 50 W, hält die Visualisierung den letzten plausiblen Realverbrauch, um unruhige Ausschläge im Dashboard zu glätten.

### ☀️ Direktvermarktung & Ertragsberechnung

- **1:1 Zeitachsen-Kopplung im Fahrplan-Modal:** Das Direktvermarktungs-Diagramm ist nun exakt auf dasselbe 15-Minuten-Tagesraster (00:00–24:00 Uhr) wie die Standard-Ladekurve synchronisiert.
- **Konsistente Ertragsbewertung bei Negativpreisen:** Bei negativen Strompreisen (≤ 0 ct/kWh) wird nur noch der tatsächlich im Haus oder Speicher genutzte PV-Ertrag in die wirtschaftliche Ertragsanzeige einbezogen.


## [5.4.4] – 2026-08-17

### 🔄 Konsolidiertes Update für heterogene Altanlagen

- **Der Web-Updater startet den veröffentlichten Ziel-Updater aus seinem root-eigenen Snapshot:** Ausführungsort und tatsächlicher Installationsroot bleiben dabei eindeutig getrennt. Nach geprüftem Backup und stillgelegten Diensten werden Produktdateien, Rechte und bekannte alte Dienstdarstellungen auf den Zielstand gebracht; anschließend folgen Dienststart und Healthcheck.
- **Historischer Bestand ist keine unnötige Updatehürde:** Abweichende frühere Dateirechte, ein redundanter alter Storage-Override oder eine neu anzulegende Laufzeitumgebung dürfen im ausdrücklich gestarteten und sicher gebundenen Downloadpfad normalisiert werden. Pfadflucht, Links, nicht eindeutig gebundene Systemdateien, konkurrierende Writer und fehlgeschlagene Sicherungs- oder Gesundheitsprüfungen bleiben harte Stopps.

### 🚗 Wallbox-Regelung

- **Start, Wiederanlauf und Phasenwechsel kommen nachvollziehbar weiter:** E3DC-Multi-Connect hält den bestätigten Stromsollwert für den erforderlichen Heartbeat. Ein vor dem Hardwareausgang unterbrochener openWB-Pro-Phasenwechsel wird nach einem frischen Geräte-Readback ohne parallelen Phasenauftrag eindeutig abgeschlossen oder neu bewertet.
- **Netzladen benötigt eine eigene aktuelle Freigabe:** Preis- oder Modusflags allein öffnen kein Ladebudget. Nutzer-`Aus`, aktuelle Speicher- und Netzpunktgrenzen, manuelle Pausen, Not-Aus und Datenfrische bleiben vorrangig.
- **Kein falscher WB-Entladungsschutz ohne Fahrzeuglast:** Der Speicher begrenzt seine Entladung nur noch bei frischer, gemessener Wallboxleistung. Ein alter Intent, ein Ladebit oder ein früherer Hold allein kann dadurch keinen unnötigen Netzbezug halten.

### ☀️ Effektiver Direktvermarktungsplan

- **Planung und bestätigte Wirkung werden nicht mehr vermischt:** Die Oberfläche zeigt bei aktiver Direktvermarktung nur den an Plan, Slot, Aktion, Owner und Phase-5-Lebenszyklus gebundenen effektiven Speicherplan. Bei ausstehender, veralteter oder widersprüchlicher Evidenz werden Leistung, Zielkurve und Erreichbarkeitsversprechen geleert, statt einen klassischen 100-%-Pfad zu behaupten.
- **Ein Entscheider und ein Hardwareausgang bleiben erhalten:** Die Projektion dient ausschließlich Diagnose und Anzeige; sie erzeugt keinen zusätzlichen RSCP-Schreibpfad.

## [5.4.3s] – 2026-08-17

### 🔄 Fehlender Backup-Root im Download-Bootstrap

- **Der administrative Root-Bootstrap kann den ersten Sicherungspfad selbst vorbereiten:** Ein wirklich fehlender kanonischer Backup-Root wird sicher angelegt. Ein bereits vorhandener unsicherer oder widersprüchlicher Pfad bleibt ein harter Abbruchgrund.
- **Der Updateablauf bleibt einfach und eindeutig:** Danach wird das Backup unverändert erstellt und geprüft. Erst anschließend werden Dienste gestoppt, Dateien und Rechte aktualisiert, Dienste gestartet und ihr Zustand geprüft. EMS-, Direktvermarktungs-, Wallbox- und Hardwarelogik ändern sich nicht.

## [5.4.3r] – 2026-08-17

### 🔄 HA-Rollenanker im Download-Bootstrap

- **Der gebundene HA-Verbund bleibt updatefähig:** Beim ausdrücklich mit Rolle und Peer gebundenen Download-Bootstrap darf ein wirklich fehlender HA-Rollenanker aus dieser bereits bestehenden Bindung erzeugt werden.
- **Erst nach sicherem Vorlauf:** Die Erzeugung erfolgt erst nach verifiziertem Backup und bestätigter Writer-Ruhe. EMS-Regelung und Hardwareausgänge ändern sich nicht.

## [5.4.3q] – 2026-08-17

### 🔄 Absoluter visudo-Pfad im Download-Bootstrap

- **Der Bootstrap-Finalizer ruft `visudo` eindeutig auf:** Der administrative Download-Bootstrap verwendet den absoluten Pfad `/usr/sbin/visudo` und ist damit nicht von einem verkürzten Root-`PATH` abhängig.
- **Keine Regelungsänderung:** EMS-Regelung und Hardwareausgänge bleiben unverändert.

## [5.4.3p] – 2026-08-17

### 🔄 Git-Metadaten beim Installationsbenutzer

- **Der Download-Bootstrap erzeugt die neue `.git`-Fläche als gebundener Installationsbenutzer:** Dadurch erfüllt der anschließende Ziel-Updater den normalen Repository-Eigentümervertrag auch dann, wenn der Bootstrap administrativ mit Root-Rechten gestartet wurde.
- **Der Sicherheitsvertrag bleibt unverändert:** Verifiziertes Backup, bestätigte Writer-Ruhe und sämtliche Safety-Gates bleiben Pflicht. EMS-Regelung und Hardwareausgänge ändern sich nicht.

## [5.4.3o] – 2026-08-17

### 🔄 Robuster Rettungsweg für heterogene Altinstallationen

- **Der neue Download-Bootstrap verwendet ausschließlich den veröffentlichten Zielstand:** `e3dc-update-bootstrap` ermittelt den aktuellen Stable-Tag samt Commit über isolierten Git-Transport und führt den Installer aus einem root-eigenen temporären Ziel-Checkout aus. Der vorhandene Alt-Updater und seine möglicherweise beschädigte `.git`-Fläche liefern keine Autorität.
- **Historische Metadaten werden nach dem Sicherheitsbackup normalisiert:** Abweichende Besitzer, Modi, Attribute und bekannte alte Dienstdarstellungen blockieren den ausdrücklich gestarteten Rettungsweg nicht mehr allein. Nach verifiziertem Backup und bestätigter Writer-Ruhe projiziert Root die bekannten Release-Dateien, Rechte und Units auf den Zielzustand.
- **Echte Gefahren bleiben harte Stopps:** Pfadflucht, Symlinks, Spezialdateien, zusätzliche Hardlinks, ein konkurrierender Updater, nicht stillgelegte Writer, ein ungültiges Backup oder ein fehlgeschlagener Ziel-Healthcheck brechen weiterhin fail-closed ab.

### ⚡ Kandidatloser passiver DV-Ladeblock

- **Der sichere 0-W-Ladeblock hängt nicht von einem diagnostischen Kandidaten ab:** In einem vollständig gebundenen passiven Direktvermarktungsslot bleiben Plan, Slot, DV-Owner und der tatsächlich übersetzte sichere Ausgang maßgeblich. Ein absichtlich fehlender Plankandidat verhindert deshalb nicht mehr den autorisierten Laufzeitvertrag `CHARGE_BLOCK_WAIT`.
- **Keine zusätzliche Speicheraktion:** Der Kandidat bleibt rein diagnostisch. Die Änderung öffnet weder Laden noch Entladen und erzeugt keinen konkurrierenden Hardwarepfad; Reserve-, Netzpunkt-, Daten- und Ausgangsgrenzen bleiben unverändert vorrangig.

## [5.4.3n] – 2026-08-17

### 🔐 Pfadgenauer Rollenanker im Update-Rückweg

- **Der kanonische Rollenanker ist wieder sicher backupfähig:** Der privilegierte Backup- und Recovery-Vertrag akzeptiert ausschließlich `/etc/e3dc-control/instance_role.json` mit den bereits vom Rollenmodell vorgeschriebenen Metadaten `root:www-data 0640`. Die private Backup-Payload bleibt `root:root 0600`.
- **Keine allgemeine Eigentümerlockerung:** Andere privilegierte Systemkonfigurationen bleiben `root:root`. Falscher Pfad, Gruppe oder Modus, zusätzliche Hardlinks, Symlinks, ACLs, Attribute und jede Identitätsdrift bleiben harte Abbruchgründe.
- **Keine Regelungsänderung:** EMS-, HA-, Wallbox-, Wärme- und Direktvermarktungsentscheidungen sowie Hardwareausgänge entsprechen unverändert 5.4.3m.

## [5.4.3m] – 2026-08-17

### 🔐 Update- und Recovery-Vertrag

- **Der normale Ziel-Updater darf einen wirklich fehlenden `off`-Anker einmalig erzeugen:** Die neue Autorität gilt ausschließlich beim vollständig versiegelten und über Git-Ancestry als echt vorwärtsgerichtet belegten Releasewechsel für einen eindeutig gebundenen Einzelknoten mit `ha_mode=off` und ohne konfigurierten HA-Peer. Bootstrap, Reinstall, Rollback und andere Aufrufpfade bleiben davon ausgeschlossen.
- **Notifier-Drop-ins hinterlassen keine eigene Fremdfläche mehr:** Atomare Schreibvorgänge verwenden ihr privates Staging außerhalb des `*.service.d`-Verzeichnisses. Ein Altbestand wird nur dann migriert, wenn der verschachtelte Ordner stabil gebunden, root-eigen, exakt leer und frei von ACLs oder Attributen ist; jede Abweichung bleibt fail-closed.
- **Optionale Units bleiben beim Startschutz eindeutig:** Bei einer nicht installierten optionalen Unit akzeptiert der Recoveryvertrag neben seinem eigenen Startschutz ausschließlich das byte- und metadatengenau geprüfte kanonische RAM-Disk-Drop-in. Weitere Namen oder Abweichungen sperren die Wiederherstellung weiterhin.
- **Der Update-Sicherheitsbeleg bleibt über Prozessgrenzen eindeutig:** Nach verifiziertem Backup und vor der ersten Produktmutation persistiert der versiegelte Normalpfad einen txid-, ziel-, rollen-, backup-, bootblock- und servicegebundenen ausstehenden Beleg (`pending`) samt Marker und dynamischem `00`-Startschutz. `pending` bleibt fail-closed und ist kein Forward-Auto-Resume. Erst der vollständig bestätigte Ziel-, Dienst-, Gesundheits- und Bootvertrag wird dauerhaft `committed`; danach ist jeder Altstand-Rollback verboten und bei einem unterbrochenen Abschluss nur das exakt gebundene Cleanup eigener Reste zulässig. Ein äußerer oder späterer Cleanup-Pfad greift erst bei inaktiver Finalizer-Lease ein. Ältere, fremde, unvollständige oder nicht mehr eindeutig gebundene Recoveryflächen bleiben manuell zu prüfen und werden weder adoptiert noch einzeln entfernt.

### 🚗 openWB Pro

- **Die 480-Sekunden-Sperre beginnt erst am realen Phasenausgang:** Eine reine Budgetreservierung erzeugt keinen Cooldown. Die Sperre wird erst aus einem exakt generationgebundenen, bestätigten `send_phase`-Wire-Receipt abgeleitet und schützt ausschließlich vor einem weiteren Phasenwechsel.
- **Ein Recovery-Hold überlebt Budgetmangel, Lease-Ablauf und Manager-Neustart:** Der Manager wertet die alte Ausgangsgeneration vor Supersession und vor dem Storage-Grant-Veto aus. Ein gestrandeter älterer 0-A-Intent darf ohne neuen Hardwarebefehl nur aus seinem eigenen Intent-/ACK-Paar und einem frischen post-intent 0-A-/0-W-Readback geschlossen werden.
- **Ein neuer Sofortauftrag ist eine eigene Nutzerintention:** Der zentrale WebUI-Transaktionspfad übergibt einen typisierten, einmalig quittierten Modus-5-Auftrag an den Manager. Nur eine vollständig belegte 3/3-Startablehnung derselben aktuellen Stecksession wird für genau diesen neuen Auftrag zurückgesetzt. Preislimit, Nutzer-`Aus`, Not-Aus, Budget-, Reserve-, Phasen- und Hardwaregrenzen bleiben unverändert vorrangig.

## [5.4.3l] – 2026-08-16

### 🔄 Git-gebundener Update-Rückweg

- **Der Ziel-Updater besitzt einen beleggebundenen Rückweg:** Repository, Ausgangscommit, root-eigenes Transaktionsbackup und Update-Transaktion werden vor der ersten Dienstmutation gemeinsam gebunden. Ein unsicherer oder ausgetauschter Sicherungspfad bleibt ein harter Abbruchgrund und wird nicht durch eine nachträgliche Rechteänderung vertrauenswürdig gemacht.
- **Lokale getrackte Änderungen bleiben im belegten Umfang erhalten:** Bei weiterhin vorhandenen, geänderten Git-Dateien stellt der Rückweg die gesicherten Bytes wieder her und härtet den Dateimodus auf den im gebundenen `old_commit` belegten Git-Modus. Unveränderte getrackte Dateien folgen vollständig diesem Ausgangscommit. Staged Indexstände, ungetrackte oder gelöschte Dateien und allgemeine manuelle Restorepfade sind keine neue Zusage dieses Vertrags.
- **Das Web-Update bleibt absichtlich clean-only:** Lokale Änderungen an getrackten Produktdateien werden vom Web-Launcher weiterhin vor dem Aufruf des Ziel-Updaters abgelehnt. Die Dirty-Recovery gehört ausschließlich zum regulären Konsolen-/nativen Root-Updaterpfad und lockert dieses Web-Gate nicht. 5.4.3l korrigiert im Web-Launcher nur den EXIT-Cleanup: Ein früher Abbruch behält seine primäre Fehlermeldung und seinen Exitstatus, während ein bereits angelegter root-eigener Ausführungssnapshot unter `/run` entfernt wird.

### 🛡️ Kernunits und erkannter Recoveryfehler

- **Eine bekannte historische Storage-Unit wird vor dem Dienststopp sicher migriert:** Ausschließlich die exakt freigegebene ältere `e3dc-storage-manager.service`-Familie darf atomar durch eine neue root-eigene Unit mit Modus `0644` ersetzt werden. Inhalt, Eigentümer, Dateityp, Links, ACLs, Attribute und effektive Drop-ins werden eng geprüft; abweichende Bestände bleiben fail-closed gesperrt.
- **PiGuard im Auto-Restart bleibt als laufender Wächter erhalten:** Der exakte systemd-Zustand `activating/auto-restart` wird als voraktiver Zustand erfasst, vor den Hardware-Writer-Diensten gestoppt und nach einem erfolgreichen Update oder Rückweg wieder entsprechend hergestellt. Mehrdeutige Zustände werden nicht als aktiv geraten.
- **Ein synchron erkannter Recoveryfehler hinterlässt einen persistenten Startschutz:** Ein transaktionsgebundener Marker und eng geprüfte systemd-Drop-ins verhindern, dass PiGuard oder bekannte Writer nach einem Neustart unbeabsichtigt anlaufen. Ein vorhandener oder nur teilweise nachweisbarer Schutz blockiert den nächsten normalen Updateversuch vor Backup und Dienstmutation.

### 📏 Belegte Reichweite

- **Kein pauschaler Schutz bei Prozess- oder Stromverlust:** Der persistente Startschutz gilt für einen vom laufenden Ziel-Updater synchron erkannten Wiederherstellungsfehler. Stromausfall, `SIGKILL`, ein Abbruch außerhalb dieses Fehlerpfads sowie allgemeine manuelle, ZIP- oder ältere Restoreabläufe erhalten durch 5.4.3l keine neue Vollständigkeitszusage.
- **Keine EMS-Regelungsänderung:** HA-, Wallbox-, Speicher-, Wärme- und Direktvermarktungsentscheidungen sowie Hardwareausgänge entsprechen unverändert 5.4.3k.

## [5.4.3k] – 2026-08-16

### 🔄 Native Alt-Updater-Handoffs

- **Auch der ältere native Ziel-Updater-Handoff bleibt updatefähig:** Der Aufruf mit `--target-updater-handoff` entfernt `E3DC_BOOTSTRAP_USER`, bevor er den root-eigenen Ziel-Snapshot startet. 5.4.3k schließt diesen zweiten unterstützten Altübergang zusätzlich zum bereits in 5.4.3j gebundenen flaglosen Snapshot.
- **Beide Snapshot-Einstiege verwenden denselben engen Nutzervertrag:** Erst nach dem Root-Lock darf der lokale Installationsnutzer aus dem übereinstimmenden Eigentümer von Repository und `.git` gebunden werden. Nach der Bindung des versiegelten Snapshots werden Repository, `.git`, Nutzerkonto und Nutzerwert unmittelbar vor dem ersten Import aus dem Zielcode erneut geprüft.
- **Die Härtungen aus 5.4.3j bleiben unverändert:** Root, `www-data`, unterschiedliche oder fremde Eigentümer, ein abweichender Nutzerwert und ein ausgetauschtes Repository bleiben gesperrt. Der private Docker-Matter-Storage, die Worker-Umask sowie HA-, Wallbox-, Speicher-, Wärme- und Direktvermarktungslogik ändern sich nicht.

## [5.4.3j] – 2026-08-16

### 🔄 Update aus älteren Bare-Metal-Beständen

- **Der flaglose 5.4.2d-Ziel-Snapshot bleibt updatefähig:** Der alte Aufrufer entfernt vor dem root-eigenen Ziel-Finalizer die Variable `E3DC_BOOTSTRAP_USER`. Fehlt sie genau in diesem gebundenen Altübergang, darf 5.4.3j den lokalen Installationsnutzer erst nach dem Root-Lock aus dem übereinstimmenden Eigentümer von Repository und `.git` ermitteln.
- **Die Ersatzbindung bleibt eng und fail-closed:** Repository, `.git` und lokales Benutzerkonto werden unmittelbar vor dem Finalizer erneut geprüft. Ein bereits gesetzter Nutzerwert bleibt unverändert, muss aber exakt dem gebundenen Repository-Eigentümer entsprechen. Root, `www-data`, unterschiedliche oder fremde Eigentümer, ein abweichender Nutzerwert sowie ein ausgetauschtes Repository bleiben gesperrt. Nach dem Finalizer wird die Aufruferumgebung exakt auf ihren vorherigen Zustand zurückgesetzt.

### 🏠 Docker und Matter

- **Der persistente Matter-Storage wird fail-closed gebunden:** Vor der startseitigen Härtung akzeptiert der Container ausschließlich nofollow geöffnete Verzeichnisse sowie reguläre Dateien mit genau einem Hardlink auf derselben Dateisystem- und Mountgrenze. Symlinks, Sonderdateien, Mehrfachidentitäten, ein ausgetauschter Root oder ein driftender Namenssatz brechen den Start ab. Eigentümer und Modi werden nur über die gebundenen Deskriptoren gesetzt und unmittelbar vor dem Workerstart gegen dieselbe Rootidentität erneut geprüft.
- **Neue Matter-Storage-Dateien bleiben privat:** Der als `www-data` gestartete Matter-Worker setzt vor Node.js zwingend `umask 077`. Sicher gebundene Bestandsverzeichnisse werden auf `0700` und Dateien auf `0600` gehärtet; neu während der Laufzeit erzeugte Fabric-, Endpoint- und Sessiondateien entstehen ebenfalls höchstens mit `0600`.
- **Die EMS-Regelung bleibt unverändert:** Gegenüber 5.4.3i ändern sich keine HA-, Wallbox-, Speicher-, Wärme- oder Direktvermarktungsentscheidungen. Matter-Protokoll, Kopplung und mDNS-Verhalten bleiben unverändert; ausschließlich der persistente Docker-Matter-Dateivertrag einschließlich Worker-Umask wird verschärft.

## [5.4.3i] – 2026-08-16

### 🔄 Update und HA-Synchronisation

- **Ältere Bare-Metal-Systeme erreichen den Ziel-Updater wieder:** Beim ersten Releasewechsel aus einem älteren 5.4.2-Bestand wird der lokale Installationsnutzer aus der kanonischen Repository-Eigentümerstruktur gebunden, unmittelbar vor dem Kindstart erneut geprüft und ausdrücklich an den versiegelten Ziel-Finalizer übergeben. Eine fehlende lokale `installer_config.json` im unveränderlichen Snapshot führt dadurch nicht mehr zum Abbruch „Installationsbenutzer ist nicht lokal gebunden“; Root, `www-data`, fremde Konten und ein ausgetauschtes Repository bleiben gesperrt.
- **Konfigurations- und Matter-Geheimnisse bleiben knotenlokal:** Matter-Pairingdatei samt temporärer Schreibdatei, Matter-Storage, Config-Backups, eine alte `e3dc.config.txt`, temporäre beziehungsweise gesicherte V4-Konfigurationen und der V4-Laufzeitcache samt atomarer Schreibdatei werden weder übertragen noch durch die allgemeine Rechteprojektion verändert. Auch die Web-PIN wird aus der gefilterten HA-Konfiguration entfernt. Der Cache folgt dem gewählten Config-Schutz mit `0660` im Standard- oder `0664` im Kompatibilitätsmodus.
- **Weitere lokale Zustände blockieren den HA-Sync nicht mehr:** Analysezustand, Update-Pause und -Nachlauf sowie Wallbox-Planertransaktionen bleiben ebenfalls lokal. Pull-Härtungen folgen keinen Symlinks und überschreiten keine Dateisystemgrenze. Die gemeinsam geführte Statistikdatenbank einschließlich WebPush-Abonnements wird dagegen weiterhin bewusst repliziert; die Lokalitätszusage gilt deshalb ausdrücklich für Konfigurations- und Matter-Geheimnisse.
- **Altbestände benötigen eine einmalige Prüfung:** Der HA-Abgleich arbeitet weiterhin ohne `--delete`. Neue Ausschlüsse verhindern daher weitere Übertragungen, entfernen aber keine früher auf den Partner kopierten Dateien. Wer HA bereits vor 5.4.3i genutzt hat, prüft beide Knoten und rotiert betroffene Zugangsdaten, Web-PIN oder Matter-Kopplung, wenn vertrauliche Kopien auf dem jeweils anderen Knoten lagen.

### 🚗 openWB Pro

- **Startablehnung folgt exakt der konfigurierten Wake-up-Episode:** Ein bis drei Versuche sind zulässig; Standard sind drei. Bei bewusst konfiguriertem Wert `1` darf bereits der erste vollständig belegte Versuch weitere automatische Starts derselben Stecksession sperren. Boolesche, nicht endliche und nicht ganzzahlige Werte sind ungültig und fallen in beiden Entscheidungspfaden einheitlich auf drei Versuche zurück. Receipt, Stecksession, aktuelle Stromfreigabe und Zeitkette müssen zusammenpassen.
- **Schutzgrenzen bleiben unverändert:** Die kurze sichere 0-A-/CP-Beruhigung wird eingehalten. Nach bestätigtem Phasenziel darf der Strom wieder anlaufen; die 480-Sekunden-Sperre schützt ausschließlich vor einem weiteren Phasenwechsel. Nutzer-`Aus`, Budget-, Reserve- und Hardwaregrenzen bleiben vorrangig. Status- und Detailansichten entfernen veraltete Episodendiagnose auch im Mischbetrieb.
- **Andere EMS-Domänen bleiben unverändert:** Das Release ändert keine Preisfenster, Speicherreserve, RSCP-Leistungsbefehle, Wärme- oder Direktvermarktungsentscheidungen.

## [5.4.3h] – 2026-08-16

### 🧰 Update bestehender Python-Umgebungen

- **Vertrauenswürdiger Root-Besitz blockiert das Update nicht mehr:** Historisch mit `sudo pip` in ein eindeutig gebundenes Benutzer-venv installierte Paketpfade dürfen Root oder dem bestätigten Installationsnutzer gehören. Die Härtung verändert keinen Eigentümer und entfernt ausschließlich Schreibrechte einer nachweislich privaten Gruppe; welt- oder fremd beschreibbare Pfade werden nicht nachträglich als sicher eingestuft.
- **Fremde venv-Inhalte bleiben gesperrt:** Jede andere UID, fremde beschreibbare Gruppe, erweiterte ACL, Sonderdatei, mehrfach verlinkte reguläre Datei oder nicht freigegebene Symlink-Kante beendet den Releasewechsel weiterhin fail-closed. Geänderte Modi werden am nofollow-gebundenen Objekt ausgeführt und vor sowie nach der Änderung erneut geprüft. Ein abgebrochener Updateversuch stellt den vorherigen Produkt- und Dienststand verifiziert wieder her.
- **Web-Update und Matter-Härtung aus 5.4.3g bleiben enthalten:** Der argumentlose Root-Launcher, die versiegelte Zielausführung, die 5.4.2-Kompatibilitätsbrücke und Matter 0.12.6 werden unverändert fortgeführt.

## [5.4.3g] – 2026-08-16

### 🔐 Web-Update und Python-Umgebung

- **System-Update kehrt eng ins Dashboard zurück:** Die Weboberfläche darf ausschließlich einen argumentlosen, root-eigenen Systemjob starten. Der Launcher bindet Installationspfad, Installationsnutzer, unveränderten Ausgangscommit und dessen veröffentlichten Stable-Tag, führt den Installer aus einem versiegelten Snapshot aus und akzeptiert keine freien Aktionen, Pfade, Tags, Reparaturen, Neuinstallationen oder Rückfälle.
- **Erster Wechsel aus 5.4.2 bleibt transaktional:** Die Kompatibilitätsbrücke beendet einen zu lange laufenden Ziel-Finalizer samt Kindprozessen sicher vor dem unveränderbaren 900-Sekunden-Limit des alten Updaters. Erst nach dem bestätigten Prozessende darf dessen Wiederherstellung beginnen; Installation und Rückweg können dadurch nicht gleichzeitig schreiben.
- **Jeder Zielstand erneuert den gebundenen Root-Launcher:** Eine verifizierte Release-Policy darf den Rechte- und Launcher-Lauf nicht auslassen. Dadurch kann ein erfolgreicher Versionswechsel keinen Launcher mit der Commitbindung des Vorgängerstands zurücklassen.
- **Debian-venv bleibt updatefähig:** Der übliche relative Python-venv-Link `lib64 -> lib` wird nur dann akzeptiert, wenn Ziel, Eigentümer und ACL-Vertrag exakt zum selben gebundenen venv passen. Absolute, fremde, mehrdeutige oder manipulierte Linkziele bleiben fail-closed gesperrt.

### 🏠 Matter

- **Matter-Laufzeit ohne bekannte npm-Schwachstelle:** Die Bridge wechselt von der alten `matter-node.js`-Kette auf die offizielle Kompatibilitätsschicht `0.12.6`. Nicht benötigte Laufzeitpakete entfallen; der Installer verwendet die gebundene Lockdatei reproduzierbar mit `npm ci --omit=dev --ignore-scripts` und verlangt Node.js ab Version 18.
- **Bestehende Kopplungen bleiben erhalten:** Die Kompatibilitätsschicht liest den bisherigen Matter-Storage weiter. Ein Wechsel auf das inkompatible neue `ServerNode`-Storageformat ist ausdrücklich nicht Bestandteil dieses Releases.
- **Matter-mDNS startet im Bookworm-Container belegbar:** Der Entrypoint startet D-Bus und den vorhandenen Avahi-Daemon direkt, überwacht dessen Prozess und verlangt einen begrenzten Bereitschaftsnachweis, bevor die Bridge läuft. Ein fehlender oder früh beendeter Discovery-Dienst stoppt den Container fail-closed.

### 🐳 Docker

- **Produktcode bleibt auch bei lokalen Windows-/WSL-Builds unveränderlich:** Nach dem Kopieren wird der gesamte Produktbaum root-eigen gebunden und verliert Schreibrechte für Gruppe und Andere. Die erforderlichen Start- und Installerdateien bleiben ausführbar; persistente Laufzeitdaten liegen weiterhin ausschließlich in den vorgesehenen Volumes.

## [5.4.3f] – 2026-08-15

### 🧰 Installation und Update

- **Bestehende Python-Umgebungen werden eng repariert:** Der Releasewechsel entfernt an einem eindeutig gebundenen Benutzer-venv ausschließlich Schreibrechte für Gruppe und Andere. Eigentümer, Pfad, Inodes, Links und ACLs werden vor der Änderung geprüft; anschließend muss der vollständige venv-Vertrag erneut gelten. Damit können ältere Installationen mit historischen `0775`-/`0664`-Rechten wieder sicher aktualisiert werden.
- **Logrotate-Konfiguration wird atomar korrigiert:** Bare-Metal-Updates projizieren die mitgelieferte Konfiguration als reines LF-UTF-8 nach `/etc/logrotate.d/e3dc-control`. Der echte Systemparser prüft Stagingdatei und Endstand; bei einem Fehler wird das gebundene Preimage wiederhergestellt.
- **Frische Bookworm-Webroots blockieren die Paketphase nicht mehr:** Der HTTP-Nachweis der geschützten Apache-Laufzeitpfade erfolgt erst nach der atomaren Veröffentlichung des Webbaums. Paket-, Modul- und Apache-Konfiguration bleiben zuvor weiterhin zwingend.

### 🚗 Wallbox und Speicherplanung

- **Ein neuer Automatikstart verwendet keine alte Startablehnung:** Beim bewussten Wechsel auf `Aus / autonom` wird ausschließlich die Evidenz des vorherigen openWB-Pro-Startversuchs verworfen und persistiert. Stecksession, Ladeende-Latch, Manager-Nullanker und Phasenreservation bleiben unverändert geschützt.
- **DV-Tagesbudget bleibt am lokalen Markttag:** Geplante Speicherverkäufe werden je `Europe/Berlin`-Kalendertag begrenzt. Ein Preisplateau über Mitternacht wird an der Tagesgrenze getrennt, sodass weder der Verbrauch des alten Tages den neuen Tag sperrt noch das neue Tagesbudget rückwirkend doppelt verwendet wird.
- **DV-Ausführung wird belegbar archiviert:** Die kompakte Speicherhistorie hält `selected`, `requested`, `issued` und `hardware_effect` als getrennte, typisierte Ausführungsstufen fest. Fehlende Evidenz bleibt unbekannt und wird nicht als negative Ausführung ausgelegt.

## [5.4.3e] – 2026-08-11

### 🔐 Bootstrap und Update-Rückweg

- **Installationsnutzer bleibt eindeutig gebunden:** Der offizielle Bootstrap reicht den bestätigten lokalen Installationsnutzer ausdrücklich durch den privilegierten Prozesswechsel. Der versiegelte Ziel-Updater muss die Rolle dadurch nicht aus einer fehlenden lokalen Konfigurationsdatei erraten.
- **Fehlender Einzelknoten-Rollenanker wird eng migriert:** Nur ein explizit an Stable-Tag, Commit-SHA und Rolle `off` gebundener Bootstrap ohne HA-Peer darf einen fehlenden Rollenanker erzeugen. Die Mutation erfolgt erst nach Nutzerfreigabe, verifiziertem Backup und Aktorruhe; HA- und Shadow-Rollen bleiben ohne passenden Anker gesperrt.
- **Rückweg akzeptiert legitime fehlende Units:** Eine intern freigegebene, nicht maskierte Kompatibilitäts-Unit darf als `not-found` bestätigt werden. Erwartete Masken, fremde Pfade und widersprüchliche systemd-Zustände bleiben fail-closed.
- **Restriktive Unit-Rechte bleiben erhalten:** Vertrauenswürdige root-eigene systemd-Dateien mit `0600`, `0640` oder `0644` können transaktional aktualisiert werden. Der Rückfall stellt ihren ursprünglichen Modus byte- und metadatentreu wieder her; Gruppen- oder Weltschreibrechte bleiben unzulässig.

### 🐳 Docker-Veröffentlichung

- **Stable-Tag `latest` wird wieder fortgeschrieben:** Nach vollständig grünem Build-, Start-, Digest-, SBOM- und Provenance-Gate befördert die Releasepipeline `v5.4.3e` wieder auf den allgemeinen Stable-Tag `latest`.

## [5.4.3d] – 2026-08-11

### 🐳 Docker-Start

- **Containerrolle wieder eindeutig gebunden:** Der Production-Container erhält einen eigenen Installationsnutzervertrag für Root innerhalb des exakt gebundenen Docker-Produktpfads. Dadurch kann die V4-Konfiguration wieder gesichert und migriert werden; der strenge Bare-Metal-Rollenvertrag bleibt unverändert.
- **Echter Starttest vor der Veröffentlichung:** Die OCI-Releasepipeline startet den gebauten AMD64-Kandidaten künftig mit echter RAM-Disk und verlangt, dass er die Konfigurationsmigration erfolgreich verlässt. Ein Image, das nur statisch korrekt gebaut wurde, aber beim Entrypoint scheitert, wird nicht mehr auf `latest` befördert.

## [5.4.3c] – 2026-08-10

### 🔔 Notifier-Transaktion

- **Eigener Startblock wird korrekt versiegelt:** Der Notifier-Installer bindet sein vorübergehendes systemd-Start-Drop-in mit demselben Unit-Snapshotvertrag wie alle übrigen Drop-ins. Dadurch wird der eigene Schutzblock nicht mehr fälschlich als fremder Drift bewertet; Installation, Commit und eindeutiger Rückfall bleiben fail-closed.

## [5.4.3b] – 2026-08-10

### 🔐 Debian-Lockroot

- **Standardkonformer `/run/lock`-Vertrag:** Der root-eigene, mit Sticky-Bit geschützte Debian-Lockroot `1777` wird als sicherer gemeinsamer Namensraum akzeptiert. Transaktionsdateien bleiben regulär, root-eigen, einfach verlinkt, `0600`, nofollow-gebunden und exklusiv gesperrt; ein weltbeschreibbarer Lockroot ohne Sticky-Bit bleibt fail-closed.

## [5.4.3a] – 2026-08-10

### 🔐 Bare-Metal-Update

- **Installationsrolle bleibt über beide versiegelten Prozesswechsel gebunden:** Der bereits lokal geprüfte Installationsbenutzer wird sowohl an den Ziel-Updater als auch an den nachgelagerten Target-Finalizer weitergegeben. Dadurch kann eine bestehende Bare-Metal-Installation den veröffentlichten Zielstand vollständig sichern, projizieren und prüfen, obwohl die lokale `installer_config.json` bewusst nicht Teil des unveränderlichen Ausführungssnapshots ist.
- **Keine Lockerung der Schutzprüfungen:** Nutzerkonto, lokale Rollenmetadaten, Ziel-Commit, Stable-Tag, HA-Rolle, Backup, Aktorruhe, Dienstzustand und HTTP-Gesundheit bleiben unverändert zwingend. Eine fehlende oder widersprüchliche lokale Benutzerbindung wird weiterhin vor einer Produktprojektion abgewiesen.

## [5.4.3] – 2026-08-10

### 📦 Update, Reparatur und langsame Systeme

- 🔒 **Privilegierter Web-Installer fail-closed:** Der frühere gemeinsame `www-data`-/sudo-Pfad für Installer, Rechte-Reparatur, Update und Rückfall ist deaktiviert und wird aus der Ziel-sudoers entfernt. Frühe Sperren in allen kompatiblen Installer-Einstiegen neutralisieren auch noch vorhandene Altfreigaben; im Web verbleibt nur der getrennte, fest erlaubte Service-Wrapper. Bis ein root-kontrollierter, aktionsgebundener und race-freier Launcher vollständig geprüft ist, benötigen Produktmutationen eine administrative Konsole.
- 🔐 **Verifizierter Ziel-Updater:** Der laufende Stand lädt und bindet vor Backup und Dienststopp den freigegebenen Ziel-Commit, den annotierten Stable-Tag, Version, Updatepolicy, Repository-Ursprung und Anlagenrolle. Der Ziel-Updater wird bytegenau versiegelt auf demselben Dateisystem gestartet und besitzt anschließend die vollständige Transaktion aus Backup, Aktorruhe, Finalisierung, Gesundheitstest und Rückweg. Ein Zielstand ohne nativen Handoff wird ausschließlich über einen getrennten SHA-gebundenen Kompatibilitätsrunner ausgeführt.
- 🔁 **Bewusste Neuinstallation derselben Version:** Ein exakt passender, vollständig belegter Release endet beim normalen Update ohne Backup und Dienstunterbrechung. Abweichende getrackte Produkt- oder Webdateien liefern dagegen `REPAIR_REQUIRED` ohne Mutation; erst die ausdrückliche Webbestätigung oder `--reinstall-current` startet die vollständige Reparatur mit verifiziertem Rückweg. Update, Neuinstallation und gezielter Rückfall sind gegenseitig ausgeschlossen.
- 🐢 **Nachvollziehbar auf langsamen Raspberry Pis:** Der äußere Ziel-Updater-Handoff besitzt kein hartes Gesamtzeitlimit. Der mutierende Finalizer meldet seine Phase und alle 30 Sekunden ein Lebenszeichen; nur diese Phase hat ein 30-Minuten-Limit. Backup und Wiederherstellung liegen außerhalb dieses Limits, und Dienste werden nach einem Timeout anhand ihres tatsächlichen systemd-Endzustands nachgeprüft.
- 🧭 **Einmalige Übergangsgrenze:** Der erste Wechsel aus 5.4.2d startet technisch noch mit dessen bereits laufendem Außenprozess und dessen 900-Sekunden-Grenze. Erst nach erfolgreicher Installation des neuen Vertrags verwenden weitere Updates vollständig den Ziel-Updater-Handoff.
- 🛡️ **Isolierter Releasepfad:** Releasewechsel laufen mit dem root-kontrollierten System-Python ab Version 3.10 im isolierten, ungepufferten Modus. Ein systemweiter Update-Lock verhindert parallele Releasewechsel; Signale, Kindprozesse und Exitcodes werden eindeutig weitergereicht. Die BOM-Prüfung verändert vor dem verifizierten Backup keine Produktdatei.
- ♻️ **Beweisbarer Rückweg:** Vor dem Stop werden die tatsächlich aktiven Dienste erfasst und bei einem Abbruch exakt auf diesen Zustand zurückgeführt. Ein abgerissener Web- oder Terminal-Ausgabekanal beendet die Wiederherstellung nicht; kann Rolle, Dienstzustand oder Gesundheit nicht vollständig bestätigt werden, bleiben Writer und Aktoren fail-closed gestoppt.
- 🔒 **Ein Storage-Writer auch nach Update und Rückfall:** Bekannte alte `storage_manager_next.py`-/`storage_manager_legacy.py`-Overrides werden auf den kanonischen Manager migriert. Prozess- und ExecStart-Prüfung erkennen beide historischen Namen; Autodiscovery, Rechteprüfung, Updatefinalizer und beide Rückwege blockieren jeden Dienststart bei fremdem ExecStart, Legacy-/Doppelprozess oder unlesbarem Zustand. Die dafür verwendeten Prüfhelfer werden vor dem Datei-Restore an den Vorzustand gebunden.
- 🧹 **Git-Prüfung ohne Konfigurationsdrift:** Repository- und Updateprüfungen verwenden aufrufgebundene Git-Optionen und `GIT_OPTIONAL_LOCKS=0`. Sie verändern weder globale noch repo-lokale Git-Konfiguration und aktualisieren bei reinen Leseprüfungen keinen Index- oder Stat-Cache.
- 🖥️ **Web-Update bleibt beobachtbar:** Die Weboberfläche bindet volle lokale und entfernte Commit-SHAs. Ihr begrenztes Polling beendet nur die Anzeige, nicht einen weiterhin laufenden Ziel-Updater; Watchdog und Statusprüfung erkennen Finalizer und Neuinstallation weiterhin als aktiven Releasewechsel.
- ⏱️ **Dienstspezifische Wiederanlaufzeiten bleiben wirksam:** Der gemeinsame RAMdisk-Startwächter prüft weiterhin den echten tmpfs-Mount und begrenzt Fehlstarts, überschreibt aber nicht mehr die Neustartzeit jeder Unit pauschal mit 30 Sekunden. Dadurch greift insbesondere die bewusst auf fünf Sekunden gehärtete Storage-Manager-Wiederanlaufzeit tatsächlich, während andere Dienste ihre eigenen Schonzeiten behalten.
- 🍓 **Frische Bookworm-Installation:** Paket-, Apache-, Konfigurations-, RAM-Disk-, Webportal- und Dienstschritte werden in einer festen Reihenfolge geprüft. Ein Fehler beendet die Installation verständlich und fail-closed; ein bereits funktionierender Vorzustand wird gebunden zurückgestellt, statt eine unvollständige Installation als erfolgreich zu melden.
- 🐳 **Docker-Aktionen gehören auf den Host:** Installation und Update erkennen Containerkontext, Anlagenrolle, Prozessorarchitektur, vorhandene Instanzen und konkurrierende Hostdienste vor jeder Mutation. Der bereitgestellte Host-Helfer zieht und bindet das Zielimage, wartet auf die wirklichen Kern- und gewählten Zusatzdienste und stoppt einen fehlerhaften Kandidaten wieder.
- 🔄 **Sicheres Docker-Bestandsupdate:** Unveränderte offizielle Compose-Dateien aus 5.4.2 bis 5.4.2d sowie die bekannte Installer-Variante werden vor dem Imagewechsel atomar auf den neuen Pflichtvertrag gebracht. `.env` sowie Daten-, Log-, ML- und Forecast-Speicherorte bleiben erhalten; ein alter Watchtower wird vorher bestätigt gestoppt. Ältere, angepasste oder per Override ergänzte Stände werden nicht geraten und verlangen eine manuelle Prüfung.
- 📦 **Docker-Image auch für rootless Laufzeiten entpackbar:** Dateien aus dem Matter-Paketbaum werden im Image auf gültige Besitzer normalisiert und bereits in den OCI-Schichten geprüft. Unterstützt bleiben 64-Bit-ARM und AMD64; 32-Bit-ARM wird vor dem Download verständlich abgelehnt.

### 🔋 Speicher- und Marktplanung

- 🌞 **Netzladen nur bei belegter Gesamtunterdeckung:** Reguläres Markt-Netzladen wird ausschließlich aus einer vollständigen, frischen, slotweise simulierten Energieunterdeckung des gesamten Entscheidungshorizonts abgeleitet. Ein bloßer Abstand zur Sollkurve, eine günstige Preisverschiebung oder ein später durch PV gedeckter Bedarf reichen nicht.
- 🧮 **Harte Reserve und Sollkurve getrennt:** Die Simulation bilanziert verfügbare Speicherenergie, Hausverbrauch und PV je Slot. Ein regulärer Netzladejob benötigt mindestens 1,5 kWh beziehungsweise fünf Prozent der Speicherkapazität; kleinere oder unvollständige Jobs fallen auf passive Hausversorgung in E3/DC-AUTO zurück.
- ⚡ **Bedarfsgerechte Ladeleistung:** Energiemenge, Ladeleistung und spätester Start werden aus dem belegten Fehlbedarf und dem nutzbaren Preisfenster berechnet. Ein kleiner Fehlbetrag erzeugt keinen pauschalen Vollgasauftrag.
- 🕒 **Energie- und Preishorizont getrennt:** Der Energievertrag bleibt über den vollständigen deklarierten 48-Stunden-Horizont geschlossen. Preise werden ohne Imputation als frischer, lückenloser Prefix gebunden; der Prefix muss mindestens bis zum letzten ungedeckten Verbrauchsslot reichen. Ein regulär noch unveröffentlichter Preisschwanz nach dem relevanten Fenster blockiert nicht, interne Lücken, veraltete Quellen oder fehlende Abrechnungspreise dagegen schon.
- 🛡️ **Sicherer EMS-Budget-Anlauf:** Ein noch fehlender zentraler Budget-Readback blockiert flexible Verbraucherbudgets, ersetzt aber nicht mehr einen bereits sicher entschiedenen Speicherrahmen durch AUTO-Freilauf. Dadurch entstehen nach Dienststart oder kurzzeitig fehlendem Ack keine unbeabsichtigten Lade- oder Entladefreigaben.
- 🛡️ **Wallboxbudget an bestätigten Speicherrahmen gebunden:** Bei geführter Wallboxladung bleibt die Batterie auf dem vom Storage Manager vorgegebenen iFc-Rahmen begrenzt. Flexible Wallboxleistung wird erst freigegeben, wenn ein frischer `POWER_SETTINGS`-Readback die angeforderten Lade-, Entlade- und Startgrenzen bestätigt; fehlende, alte oder abweichende Rückmeldungen setzen ausschließlich das Wallboxbudget fail-closed auf 0 W, während sicher typisierte Stopps erreichbar bleiben.
- 🧭 **Ein gemeinsamer Speicher-Ownervertrag:** Storage Manager und kanonischer Ausführungspfad verwenden dieselbe vollständige Liste kompatibler, konkurrierender und stärkerer Primärpfade. Unbekannte oder nicht ausdrücklich kompatible Pfade bleiben fail-closed; Schutz-, manuelle, Wallbox- und Pre-Dump-Owner können nicht durch eine spätere DV-Übersetzung verdrängt werden.
- 🏠 **Hausversorgung bleibt eine passive AUTO-Wirkung:** `HOUSE_SUPPLY` fordert keine feste Batterieentladung an. Der Storage Manager setzt ausschließlich den flüchtigen Laderahmen auf 0 W und erhält die reserve- und hardwaregebundene Entladegrenze; ein aktiver Leistungs- oder Befehlsanspruch unter diesem Namen wird verworfen.
- ♨️ **Diagnosefallback erteilt keine Wärmefreigabe:** Wärmebudgets und Pausenaufträge werden erst nach vollständiger Producer-, Entscheidungs- und Gesamtrevisionsprüfung übernommen. Der ältere Storage-State-v1-Fallback bleibt reine Diagnose mit `EVIDENCE_LIMIT`; weder passende IDs noch manipulierte Wattwerte oder ein behauptetes `VALID` können daraus eine Verbraucherprojektion oder einen automatischen Wärmepumpen-/Heizaktorausgang erzeugen.
- 🔌 **Passiv beobachtete Wallbox am Speicherboden:** Bei einer extern geregelten Wallbox erzeugt der passive Kurvenpfad am Wallbox-Mindest-SoC keinen eigenen `CHRG`-Auftrag mehr. E3/DC bleibt in AUTO, echte PV-Überschüsse können weiterhin gespeichert werden und nur die Speicherentladung zugunsten der fremdgesteuerten Wallbox wird begrenzt.
- 🧹 **Nachwirkenden Ladeauftrag neutralisieren:** Ein einmaliges `AUTO/0` löst in diesem eng begrenzten passiven Kurvenfall einen noch im Gerät wirkenden früheren Ladeauftrag. Anschließend bleiben nur die bestätigten flüchtigen `POWER_SETTINGS` wirksam; aktive Wallboxmodi, geplante Netzladefenster und der getrennte Reservepfad bleiben unverändert.
- 🔀 **Dreistufige Ladequellenwahl:** Ohne neue Auswahl bleibt die bisherige Regelung unverändert. Wahlweise begrenzt die Prognose-100-Ladekurve ihre Speicherladung ausschließlich auf E3/DC-DC-PV oder merkt eine spätere Zusatz-AC-Route vor. Wirksam bleibt `E3DC_DC_ONLY`, bis eine historisch kalibrierte gemeinsame Horizontverteilung der tatsächlich speicherbaren DC-Energie, die vollständige Topologie-/Plan-/Slot-/Action-Bindung und eine fachlich beschlossene Risikoschwelle vorliegen. Ein alter 80-Prozent-Punktwert bleibt reine Diagnose ohne Steuerwirkung; fehlende oder schlechte Daten gelten nicht als Minderertrag, und Netzladen wird dadurch nie freigegeben.
- 📈 **Kein Hoffnungs-Hold unter der Prognose-100-Kurve:** Unterhalb der unteren Sollkurvenkante dürfen gleitender Prognosehorizont und Ziel-Lande-Hold keinen 0-W-Aufschub mehr erzeugen. Bei sicherem realem PV-Überschuss lädt der Speicher entlang der Kurve; fehlt er, wird E3/DC in AUTO ohne künstliche 0-W-Ladegrenze freigegeben. Klassische Anker und ihre Hysterese bleiben unverändert.

### 💶 Tarife und Direktvermarktung

- 📈 **PV speichern nur bei belegtem späterem Mehrerlös:** Bei positiven Börsenpreisen wird jeder Viertelstunden-Slot einzeln gegen tatsächlich noch freie spätere Verkaufsenergie bewertet. Freigegeben wird nur, wenn derselbe Nettoerlösvertrag unter Berücksichtigung von Wirkungsgrad, Batteriealterung/LCOS, Sicherheitsaufschlag, Reserve-, Last- und Tagesbudget sowie den eingestellten Mindestwerten einen positiven Grenzerlös belegt; negative Rohpreise bleiben davon als eigene harte Schutzkante getrennt.
- 🔋 **Aktiver DV-Plan als positive Ladefreigabe:** Im aktiven Feldbetrieb öffnet ausschließlich ein aktuell gültiger, vollständig kanonisch gebundener `PV_STORE`-Slot die Speicherladung. Jeder andere Viertelstunden-Slot setzt die flüchtige maximale Ladeleistung auf 0 W; die Entladung für Hausversorgung bleibt möglich. Planwechsel, ein kurz fehlender Folgeslot, unbekannte Aktionen und Übersetzungsfehler können dadurch nicht mehr auf einen permissiven E3/DC-AUTO-Ladepfad zurückfallen.
- ☀️ **DC-only-Fortsetzung im Negativpreisfenster:** Fehlt innerhalb eines bereits gültig begonnenen `PV_STORE`-Fensters vorübergehend die aufgeteilte Quellenprognose, öffnet der aktuelle Slot ausschließlich einen flüchtigen E3/DC-AUTO-Laderahmen. Er sendet keinen CHRG- oder Netzladebefehl; Zusatz-AC und Netzladen bleiben gesperrt. Die aktuell sichtbare DC-Leistung begrenzt diese Erlaubnis nicht vorab, weil eine externe 0-W-Regelung die PV bei geschlossenem Laderahmen selbst auf den Hausbedarf reduzieren kann. Jeder Viertelstunden-Slot bindet Rohpreis, Preisrevision, Ziel-SoC, Marktfenster, 0-W-Exportvertrag, frische Messqualität und externen E3/DC-Regler erneut. Die angezeigte Restdauer „bis voll“ ist keine feste Abschaltzeit. Ziel-SoC, ein Rohpreis ab 0 ct/kWh, das Marktfensterende oder ein Schutzveto beenden die Freigabe.
- 🧭 **Eine gemeinsame DV-Aktionsmatrix:** Planer, Slotprojektion, Arbitrierung, Laufzeitbindung und Executor beziehen Aktion, Quelle, Modus, Leistungsbudget und Freigabestatus aus demselben Vertrag. Quellaktion und Modus gelten nur als ausdrücklich freigegebenes Paar; kanonischer Slot, Policy sowie Auswahl-, Ausführungs- und Planfenster müssen exakt dieselbe Quellaktion tragen. Eine noch nicht freigegebene oder neu kombinierte Aktion erscheint weder als ausgewählt noch als ausführbar und bleibt bis zu ihrem vollständigen Wirkungsnachweis fail-closed. Eine geplante Speicherplatzreserve sperrt bereits das Laden, erzwingt aber ohne vollständig gebundene Reserve-, Energie- und Netzpunktprüfung noch keine Entladung.
- 📉 **Echter Negativpreisvertrag:** Negativpreis-Freigaben gelten ausschließlich für Tarife mit echten Spotmarktpreisen und nur bei einem Abrechnungspreis kleiner 0 ct/kWh. Exakt 0 ist nicht negativ; ein negativer Nutzergrenzwert kann die Schwelle nur weiter verschärfen.
- 🧭 **Tarifwechsel hebt Altverträge auf:** Ein aktiver Speicher- oder Verbraucherpfad prüft Tarifidentität, Preisrevision und aktuellen Slot erneut. Fehlende Identität, `future_unknown`, statische Tarife, wiederkehrende Spezialtarife und Octopus Heat können keinen alten Spotmarktvertrag weiterführen.
- 🌤️ **Rohpreis und Erlös getrennt:** Die Direktvermarktungsanzeige unterscheidet Rohbörsenpreis, Abrechnungspreis und Netto-Verkaufserlös einschließlich Quelle und zeitlicher Auflösung. Ein positiver Nettoerlös wird dadurch nicht mehr als positiver Rohbörsenpreis missverstanden.
- 🔀 **Getrennte Ausführungseigentümer:** Ein externer E3/DC-Regler wird nur durch eine ausdrückliche interne Topologiebindung zum Eigentümer der E3/DC-Abregelung. In einem so gebundenen harten Exportlimit setzt der Storage Manager ausschließlich den flüchtigen Batterie- und Entladerahmen und sendet keinen konkurrierenden `SET_POWER`- oder AUTO-Auftrag. Ohne gültige Bindung bleibt der Storage Manager der sichere Standard.
- ☀️ **Zusatzwechselrichter nur aus aktuellem Rohslot:** Aktorwirksam ist ausschließlich der aktuell gültige originale DV-Rohbörsenslot; der Plan dient nur der Diagnose. Exakt 0 ist nicht negativ und fordert keine Abschaltung an. Fehlender, veralteter, ungültiger oder widersprüchlicher Rohpreis wird nicht aus Plan, Endkundenpreis, Gebühren oder Nettoerlös ersetzt und erzeugt keinen neuen Schaltbefehl; Provenienz und Planabweichung bleiben sichtbar. Bestehende Schalt- und Hysteresesperren gelten weiter.
- ✅ **Shelly-Schaltzustand wird bestätigt:** Ein erfolgreicher Schaltaufruf allein gilt nicht mehr als ausgeführter Relaiszustand. Die zentrale Zusatzwechselrichter-Steuerung liest vor einem unsicheren Übergang und nach jedem Befehl den strikt booleschen Shelly-Istwert zurück; Fehler oder Abweichungen bleiben sichtbar unbestätigt, werden begrenzt erneut geprüft und durch eine persistente 600-Sekunden-Gegenschaltsperre gegen Flattern geschützt.
- 🔎 **Owner-Vertrag bis zum RSCP-Ausgang:** Planer, Fenstergruppierung, DC-first-Begrenzung, Phase 5 und der unmittelbare Vor-Sende-Recheck erhalten denselben Ausführungseigentümer und dieselbe Entladegrenze. Ein alter oder lediglich beobachteter Live-Marker kann keine Eigentümerrechte selbst erzeugen; Diagnosefelder trennen erwarteten, bestätigten und unterdrückten Ausgang.
- 🧭 **Owner-Diagnose bleibt eigentümerspezifisch:** `hard_export_owner_unconfirmed` gilt ausschließlich, wenn der Plan ausdrücklich den externen E3/DC-/Luox-Owner erwartet. Der legitime lokale Storage-Manager-Owner erzeugt keinen falschen externen Owner-Blocker.
- 🗣️ **DV-Haltebetrieb verständlich angezeigt:** Aktiver Ladeblock und beide Schutz-Fallbacks erscheinen in Desktop- und Mobilansicht als „Speicherplatz halten“ statt mit internen englischen Aktionsnamen. Die Begründung beschreibt den auf 0 W gesetzten Laderahmen als Sollvertrag und stellt klar, dass Hausversorgung nur innerhalb der weiterhin vorrangigen Schutzgrenzen aus dem Speicher erfolgen darf.
- 🧭 **Ein eindeutiger Speicherstatus im Dashboard:** Status, Titel und angezeigter Ausführungseigentümer stammen gemeinsam aus dem finalen Storage-Manager-Zyklus. Ein älteres Wallboxbudget darf diese Anzeige nicht mehr teilweise überschreiben; echte Wechsel zwischen DV-Slot und E3/DC-AUTO bleiben dagegen als fachlicher Zustandswechsel sichtbar.
- 🧾 **DV-Plan und tatsächliche Wirkung getrennt:** Ein wirtschaftlich geplanter `PV_STORE`-Kandidat bleibt im zugehörigen Viertelstundenfenster nachvollziehbar, erscheint bei einem gebundenen Ausführungsblock jedoch grau als „aktuell nicht ausgeführt“. Ausgewählte Aktion, Freigabeflags und Wirkung werden atomar aus demselben Registry-Vertrag projiziert; der abgelehnte Kandidat bleibt getrennt und kann keine Flags oder Wirkung erben. Die Runtimewirkung „Laderahmen 0 W“ wird nur bei exakt plan-, slot- und fenstergebundenem Ladeblock samt frischem bestätigtem Readback ausgewiesen. Fensterenergie, Verbraucherbudget und tatsächlicher Speicherrahmen bleiben getrennt; veraltete oder fremde Laufzeitdaten dürfen den Plan nicht umdeuten.
- 🏠 **Zusatzwechselrichter für Hausversorgung getrennt vorbereitet:** Neben `Aus`, reiner Reservestützung und wirtschaftlicher Nutzung existiert eine eigene Auswahl für prognostizierte Hausversorgung. Standard bleibt `Aus`; bis zum vollständigen konservativen Quellen-, Bedarfs- und Terminvertrag bleibt die Auswahl sichtbar als nicht entscheidungsfähig gesperrt und kann insbesondere eine bestehende hohe Einspeisevergütung nicht still opfern.

### 🔎 DV-Shadow-Diagnose

- 🔁 **Aktive Eco+-Basisregel und Shadow sauber getrennt:** Die aktive Regelung begrenzt einen gebundenen passiven Eco+-Normalabschnitt in geeigneten, ungeschützten AUTO-Zuständen auf 0 W Ladeleistung. Dem Prognose-Shadow fehlt dieser spätere Runtimezustand; er weist die 0-W-Kante deshalb als bedingten Kandidaten mit `EVIDENCE_LIMIT` aus, statt allgemeine Parität oder eine sichere Zukunftswirkung zu behaupten.
- 🔗 **0-W-Pilot an Action und Planslot gebunden:** Die anlagenspezifische Eco+-Ladebegrenzung ist standardmäßig aus. Nach ausdrücklicher Freigabe greift sie nur bei aktuell aktivierter Direktvermarktung im Modus Eco+, exakt gebundenem Storage-Manager- und Plan-Owner, typisiertem Vertragsstand und noch gültigem Plan. Der hashvalidierte aktuelle kanonische Slot, genau ein über das vollständige Slotintervall passiver Timeline-Vertrag und die aktuelle Policy müssen dieselbe Hausversorgungsaktion und dieselben Zeitgrenzen belegen; fehlende, alte, mehrdeutige, überlappende oder aktive Slots bleiben wirkungslos.
- 🧷 **Aktive Wirkung vollständig identifiziert:** Owner, Controller, Vertragsversion, Aktiv-/Shadow-Zustand, zulässige Befehle, Zeitfenster, Policy und Action werden gemeinsam bis zum Laufzeitpfad gebunden. Eine wirtschaftliche Headroom-Exportaktion darf den Hausversorgungsvertrag nicht wiederverwenden und bleibt ohne eigenen kanonischen Wirkungsvertrag fail-closed.
- 🌞 **Punktprognose bleibt Diagnose, keine Zukunftsfreigabe:** Eine deterministische Punktprognose allein begründet keinen zusätzlichen Ladeblock für ein späteres PV-Fenster. Der befehlsfreie Shadow weist diese Empfehlung als unzureichende Evidenz aus; eine spätere prognosebasierte Entscheidung benötigt quellengetrennte, konservativ kalibrierte PV-/Lastszenarien, eine vollständige Topologie und einen konkreten Bedarfstermin.
- 📊 **FIELD-Quantile strikt gebunden:** Eine spätere FIELD-Aktivierung akzeptiert P10/P50/P90 für PV und Last nur vollständig, frisch, quellen- und revisionsgebunden sowie mit ausdrücklich deklarierter CDF- oder Überschreitungs-Konvention und gültiger Reihenfolge. Für Regelwirkung müssen zusätzlich Ausgabezeit und Vorlaufklasse, Kalibriermethode, -revision und -fenster, mindestens 96 Stichproben an sieben Tagen sowie die ausdrückliche Nutzungsfreigabe für E3/DC-DC, Zusatz-AC und Last gemeinsam gebunden sein. Punktwerte werden nicht als P50 ergänzt; jede Lücke bleibt `EVIDENCE_LIMIT`.
- 🧮 **Reserveklassen statt vermischter Sollwerte:** Physische Notstromreserve, geschützter Haus-/Nachtbedarf und weiches Ladeziel sind getrennt. Eine bereits vorhandene Bedarfsreserve darf ohne Zukunftsbehauptung bewahrt werden; fehlt sie, bleibt der Shadow bis zu einem typisierten Wiederbefüllungspfad wirkungslos. Auch ein Wirtschafts-Export kann den höheren Schutzboden nicht durch ein kleineres Slotbudget absenken.
- 🕒 **Viertelstündliche Planhistorie:** Eingabe, Plan und Begründungen des Shadows werden in einer privaten, unveränderlichen und begrenzten Historie gespeichert. Plan- und Begründungsrevisionen machen nachträgliche Änderungen sichtbar; identische Viertelstunden werden nicht mehrfach archiviert und fehlende Historie wird nicht erfunden.
- 🛡️ **Weiterhin ohne Hardwarewirkung:** Jeder Historieneintrag muss den befehlsfreien Vertrag, den Storage Manager als einzigen Runtime-Owner und genau einen RSCP-Ausgang bestätigen. Ungültige, unvollständige oder zu große Datensätze werden verworfen; der Shadow verändert weder Konfiguration noch Hardwareausgänge.
- 🔐 **Authentisierter Snapshot-Transport:** Master und Shadow müssen dasselbe bewusst gesetzte 64-stellige Peer-Geheimnis besitzen. Ohne gültiges Token bleibt der Shadow vor jedem Netzwerkzugriff pausiert; der Master prüft es konstantzeitlich, bevor eine Betriebsressource geöffnet wird. Übertragen werden ausschließlich feste, positiv typisierte Projektionen ohne Anlagenkennungen, Zugangsdaten oder freie Kommandofelder. Fehlende, veraltete oder unvollständige Pflichtdaten erzeugen keine neue Vergleichsentscheidung und alte Preimages erscheinen nicht als frisch.

### 📈 Prognoseevidenz

- 🧭 **Vorlaufzeiten getrennt bewertet:** Die optionale PV-Diagnose trennt Forecast-/Ist-Vergleiche in 0–2, 2–6, 6–24, 24–48 und 48–72 Stunden. Bewertet wird ausschließlich eine vor dem Zielslot veröffentlichte, revisions- und quellengebundene Prognose; alte Sidecar-Erfassungszeiten werden nicht rückwirkend als echte Ausgabezeit umgedeutet.
- 📐 **Punktprognose bleibt Punktprognose:** Der sichtbare E3/DC-DC-Wert ist ausdrücklich als deterministische, nachbearbeitete Punktprognose gekennzeichnet. Ohne deklariertes Quantilniveau und eindeutige CDF-/Überschreitungs-Konvention wird weder P50 noch eine probabilistische Güte behauptet.
- 🗺️ **Flächenzuordnung zuverlässig speicherbar:** Der Konfigurationseditor serialisiert die sichtbare PV-Topologie auch beim Speichern eines einzelnen Abschnitts. Vollständige alte FC-Flächen und ihre vorhandenen Solcast-Zuordnungen werden ohne erfundene gemeinsame Wechselrichtergruppe vorbefüllt; bereits gültige, historisch als JSON-Text gespeicherte Topologien werden beim Lesen normalisiert und erst beim nächsten bewussten Speichern migriert. Zielgruppe und Allokationsliste können sich nicht mehr widersprechen, und Validierungsfehler werden nicht pauschal als Rechteproblem ausgegeben.
- 🧪 **Vollständigkeit vor Gütesiegel:** „Diagnostisch“ setzt neben 96 ertragsrelevanten Viertelstunden und sieben Vergleichstagen eine vollständige Forecast-/Ist-Abdeckung voraus. Selektiv fehlende Historie bleibt vorläufig und kann die Kennzahlen nicht still als belastbar erscheinen lassen.
- 📏 **Prognose gegen Historienreferenz:** Zusätzlich zu Bias und WAPE zeigt die Diagnose RMSE und den Skill gegenüber der bei Ausgabe bereits bekannten Tagespersistenz, getrennt nach Vorlaufzeit. Die Referenz verwendet denselben UTC-Slot des Vortags, ist damit frei von Zukunftswissen und bleibt reine Diagnose.
- 🔗 **Unveränderliche Prognoseausgabe:** Die Diagnose bindet echte Producer-Ausgabezeit, lokale Methoden- und Nachbearbeitungsrevision, Topologie, Quellenkomposition und sämtliche UTC-Zielslots an eine gemeinsame Issue-ID. Der vollständige Vertrag steht nur einmal im Forecast; jeder Slot trägt dieselbe ID. Eine unbekannte externe Modellversion bleibt ausdrücklich `EVIDENCE_LIMIT`, und Datei- oder Erfassungszeit werden nicht als Producer-Zeit umgedeutet.
- 🧾 **Kohortenwechsel ohne scheinbaren Datenverlust:** Nach einem Producer-Vertrags-, Methoden- oder Topologiewechsel zeigt die Diagnose aktuelle und zuvor erhaltene Vergleichstage getrennt an. Alte Rohdaten bleiben unverändert, werden aber niemals mit der neuen revisionsgebundenen Kohorte vermischt. Der Methodenstand wird beim Prozessstart gebunden, die Prognosedatei atomar in der RAM-Disk ersetzt und ein fehlender Producer-Vertrag erscheint mit seinem konkreten `EVIDENCE_LIMIT`-Grund.
- 🔎 **Evidenzgrenzen sichtbar:** Nur E3/DC-DC-PV besitzt derzeit ein gültiges Forecast-/Ist-Paar. Zusatzwechselrichter, Haus, Wärme und Wallbox sowie Abregel-, Clipping- und externe Abschaltfilter bleiben ausdrücklich `EVIDENCE_LIMIT`.
- 🔐 **Diagnoseaktivierung als gebundene Transaktion:** Die Webauswahl meldet „aktiv“ erst nach erfolgreichem Speichern, vorhandener Unit sowie belegtem Enable und Start. Schlägt ein Schritt fehl, werden Konfiguration und Dienstzustand auf den zuvor gebundenen Stand zurückgeführt; ein unprivilegierter oder unvollständiger Aufruf bleibt wirkungslos.
- 🛡️ **Keine Regelwirkung:** Die Diagnose darf weder eine Speicher-, Wärme-, Wallbox- oder Direktvermarktungsentscheidung freigeben noch Modell oder Konfiguration automatisch ändern.

### ♨️ Wärme-Shadow

- 🔎 **Veröffentlichbarer Diagnosevertrag:** Der revisionsgebundene Wärme-Intent ist strikt befehlsfrei, schreibgeschützt und nicht ausführbar. Er kann konservative Wärmebedarfs- und PV-Quantile vergleichen, besitzt aber keinen Treiber- oder Hardwareausgang.
- 🛡️ **Hochpreisfenster ohne Freigabe wirkungslos:** Der allgemeine Hochpreis-Kandidat darf weder eine Wärmepumpenpause noch einen Warmwasserabbruch auslösen. Dafür wäre zusätzlich zum zentralen Heat-Runtime-Schalter ein eigener ausdrücklicher Aktivierungsvertrag erforderlich; der echte Negativpreis-Sonderpfad bleibt davon getrennt.

### ❄️ Klima-Cloud

- 🕒 **Toshiba-Login schont die Ratenbegrenzung:** Ein gültiges Sitzungstoken wird ausschließlich im Prozess-RAM wiederverwendet. Erst ein bestätigter HTTP 401 erlaubt genau eine neue Anmeldung; HTTP 429 übernimmt `Retry-After` oder fällt auf einen begrenzten stufenweisen Rücksetzzeitraum zurück, der über einen Dienstneustart in der vorhandenen RAM-Statusdatei erhalten bleibt. Die Oberfläche zeigt Ratenbegrenzung und nächsten Versuch getrennt an, ohne Token zu speichern oder Klimakommandos freizugeben.

### 🔌 Wallboxen, Fahrzeuge und Lastspitzen

- 🚙 **Neue Stecksession vor jeder Regelpause erkannt:** Eine frische physische Ab-/Ansteckfolge der openWB Pro wird jetzt bereits beim Statusreadback verarbeitet und nicht mehr durch `Aus`, manuelle Pause oder Pre-Dump-Wartepfade übersprungen. Der erste Startauftrag einer neuen Sitzung ist dadurch wieder immer der Sollstrom; CP-Wake-ups folgen erst nach erfolgloser Wartezeit.
- 🧾 **CP-Versuche nur mit freiem Hardwareausgang gezählt:** Ein belegter Ausgangsslot verschiebt den Wake-up auf den nächsten Managerzyklus, ohne das begrenzte CP-Budget zu verbrauchen. Ab der tatsächlichen Ausgangskante bleibt ein unbestätigter Versuch dagegen bewusst fail-closed gezählt und wird eindeutig diagnostiziert.
- 🔒 **Direkter openWB-Webausgang stillgelegt:** Der frühere Web-Proxy nimmt keine Stellwerte mehr an und baut keine Wallboxverbindung auf; nach Authentifizierung und CSRF-Prüfung antwortet er mit HTTP 410. Nur zentrale Wallbox-Policy und Wallbox-Manager senden Hardwarebefehle.
- 🔐 **Genau ein Wallbox-Manager-Prozess:** Vor Hardwareerkennung und Treiberaufbau erwirbt der Manager exklusiv eine Sperre in der RAM-Disk. Ein zweiter Prozess beendet sich fail-closed, bevor er eine Wallbox entdecken oder ansteuern kann. Der frühere Web-Schreibpfad über `e3dc.wallbox.txt` ist ebenfalls wirkungslos; der getrennte Firmware-Wartungspfad bleibt erhalten.
- 🧘 **Ruhiger Speicher-Owner:** Wallbox-Sollstrom, Batteriehinweis, gestecktes Fahrzeug und autorisierter Start beanspruchen den Speicher-Owner nicht mehr. Erst frische, valide reale Fahrzeugleistung ab der physikalischen Mindestleistung öffnet den Wallbox-Freilauf; fällt sie weg, übernimmt die Speicherführung trotz laufender Haltezeit sofort wieder die Ladekurve. Startbudget und Wh-Puffer bleiben davon unberührt beim Wallbox Manager.
- 🚦 **Frischer Wallboxstart ohne Altpfad:** Eine aktuell verbundene, steuerbare Wallbox erhält ihre Startleistung direkt aus dem zentralen Gesamtwattrahmen. Ein früherer Nebenpfad darf weder ein 0-W-Startangebot vortäuschen noch Leistung zusätzlich erzeugen; nicht angenommene Freigabe wird im Folgezyklus anhand des frischen Istwerts wieder verteilt.
- 🚗 **Fahrzeug an die Stecksession gebunden:** Statische Wallboxzuordnung, Cloud- oder SoC-Profil dürfen nicht mehr beweisen, welches Fahrzeug aktuell angeschlossen ist. Eine kurz gelatchte Identität wird nur innerhalb derselben exakt passenden Stecksession wiederverwendet.
- 🛣️ **openWB-Reichweite bleibt maßgeblich:** Eine frische, zur aktuellen Stecksession und zum Fahrzeug passende Gesamtreichweite der openWB wird direkt angezeigt. Die Profilrechnung dient nur noch als Rückfall bei fehlender, veralteter oder identitätsfremder Quelle; geladene und gesamte Reichweite bleiben getrennte Werte.
- ⚡ **Phasenabhängige OBC-Grenzen:** Bestätigte Fahrzeuggrenzen für ein-, zwei- und dreiphasiges Laden werden erst nach sicherer Sessionzuordnung und nach der physisch wirksamen beziehungsweise gewählten Phase unmittelbar am einzigen Hardwareausgang angewendet. Ohne bestätigten Match bleibt die Wallbox- und Hardwaregrenze maßgeblich.
- 📉 **Wallboxen in der Lastspitzenbegrenzung:** Flexible Wallboxleistung erhält nur den belegten Restspielraum der laufenden Zähler-Viertelstunde nach Grundlast und bisherigem Netzbezug. PV-Überschuss wird nicht als Lastspitze behandelt; Mindestleistung, Hausanschluss- und Gerätegrenzen bleiben erhalten.
- 🛡️ **Keine erfundene Viertelstundenreserve:** Ist die Messhistorie lückenhaft, veraltet oder ungültig, erzeugt die zentrale Wallbox-Policy kein Lastspitzenbudget. Ein Rahmen unterhalb der physikalischen Mindestleistung führt zu einem sauberen Stopp statt zu einer instabilen Kleinstleistung.
- 👻 **Keine deaktivierten Phantom-Wallboxen:** Explizit ausgeschaltete oder nicht konfigurierte Wallbox-Slots werden vor Session-, MQTT-/HA-, Historien- und Peak-Bildung entfernt. Alte Cache-, Enable- oder Messwerte können insbesondere eine deaktivierte zweite Wallbox nicht wieder einblenden.
- 🧾 **Optionale Wallboxfelder ohne PHP-Warnschleife:** Fehlt ein deaktivierter Ladepunkt in der Live-Projektion, übernimmt die Verlaufserfassung neutrale Phasenwerte und einen unbekannten Verriegelungsstatus. Dadurch entstehen weder falsche Messwerte noch minütliche Apache-Fehlerlogeinträge.
- ⚡ **Wattbudget eindeutig je Wallbox übersetzt:** Das verfügbare Leistungsbudget wird anhand der frischen Phasenzahl und der jeweiligen Gerätegrenze in Ampere je Phase übersetzt. Eine unbekannte Phasenlage oder ein Budget unterhalb der physikalischen Mindestleistung bleibt bei 0 A; laufende Wallboxen und Prioritäten werden reihenfolgeunabhängig berücksichtigt.
- 🔒 **Stromangebot überschreitet das kW-Budget nicht:** Für die harte Amperefreigabe zählt immer die nominal mögliche Leistung von 230 V je aktiver Phase. Eine vorübergehend oder dauerhaft geringere Fahrzeugabnahme bleibt Diagnose und kann das gemeinsame Leistungsbudget weder in mehr Ampere umrechnen noch nach einem späteren Hochrampen überziehen.
- 🧮 **Gemeinsames Gruppenbudget ohne Doppelbelegung:** Das absolute Wattbudget reserviert eine real laufende, extern beobachtete, pausierte oder kurzzeitig statusgestörte Wallbox genau einmal, bevor der Rest verteilt wird. Ungültige, veraltete, degradierte oder glitch-behaftete Statuswerte erhalten keine neue Zuteilung; eine Strom-mal-Phasen-Schätzung ist nur mit einem höchstens 15 Sekunden alten bestätigten Ausgangsbeleg zulässig.
- 🔐 **Wallboxanteil exakt versiegelt:** Der Wallbox Manager übernimmt ausschließlich den für Wallboxen autorisierten Wattanteil der aktuellen Storage-Entscheidung. Widersprüchliche doppelte Allokationsflächen, nachträglich neu versiegelte Fremdansprüche oder eine nur teilweise passende Entscheidungsgeneration ergeben 0 W statt eines permissiven Ersatzbudgets.
- 🛟 **Ein gemeinsames 40-Wh-Gruppenkonto:** Der Start-/Mindestleistungswächter gilt einmal am Netzanschlusspunkt und wird bei zwei Wallboxen nicht verdoppelt. Seine 180-Sekunden-/40-Wh-Stützung beginnt erst nach belegtem Hardwareausgang und bleibt an die aktuelle Stecksession gebunden; reicht das Restbudget nicht für beide, erhält gemäß Gleichverteilung oder Priorität wenigstens eine laufende Ladung den Mindestbedarf, soweit harte Grenzen es erlauben.
- 🖥️ **Leistung statt mehrdeutiger Gruppenampere:** Die gemeinsame Obergrenze wird als kW-Cap angezeigt. Ampere, wirksame Phasenzahl, kW und kVA erscheinen je Ladepunkt in dessen eigener Zeile, damit ein- und dreiphasige Werte nicht verwechselt oder dem falschen Fahrzeug zugeordnet werden.
- ℹ️ **Minderabnahme neutral diagnostiziert:** Erst wenn ein unverändertes Stromangebot mindestens 45 Sekunden anliegt und die reale Ladeleistung deutlich darunter bleibt, erscheint der neutrale Hinweis „Abnahme unter Stromangebot“. Er unterscheidet zulässige Stromobergrenze und tatsächliche Fahrzeugabnahme, ohne vorschnell einen Fahrzeugfehler zu behaupten oder steuernd einzugreifen.
- 🧾 **Sendebeleg und physische Wirkung getrennt:** Nur ein tatsächlich ausgegebener Befehl gilt als Sendebeleg; blockierte oder gedrosselte Ausgänge melden keinen Scheinerfolg. Ein bestätigter Stoppbefehl setzt die gemessene Last erst nach einem frischen 0-W-/Nicht-Laden-Readback auf null. Bei einer kurzen Statusstörung bleibt die zuletzt belegte reale Leistung reserviert, bevor Restleistung verteilt wird.
- 🧩 **Kurzer 0-W-Status beendet keine openWB-Ladung:** Ein vermeintliches Fahrzeugladeende muss innerhalb derselben Stecksession durch mindestens drei frische, zeitlich getrennte Rückmeldungen während mindestens 45 Sekunden bestätigt werden. Während der Prüfung hält der Manager das bereits freigegebene Stromangebot ohne CP-Unterbrechung oder Startwiederholung; veraltete, unklare oder sitzungsfremde Werte verwerfen den Kandidaten.
- 🚦 **Start-Retry hat Vorrang vor Keepalive:** Solange eine openWB Pro nach der Stromfreigabe noch keine reale Ladeleistung meldet, sendet der Manager keinen allgemeinen Aufwärts-Keepalive. Der einzige Hardwareausgang bleibt dadurch für den zeitlich begrenzten kanonischen Start-/Wake-up-Retry frei; eine sichere Absenkung eines noch zu hohen physischen Angebots bleibt vorrangig möglich, und erst eine physisch laufende Ladung darf ein verlorenes Stromangebot per Keepalive erneuern.
- ⏱️ **Startantwortfrist strikt an die Stecksession gebunden:** Eine ältere Startzeit darf eine zuvor real ladende oder neu angesteckte openWB Pro bei einem späteren 0-W-Fenster nicht sofort als abgelehnten Start stoppen. Die Frist beginnt ausschließlich nach einem in derselben frischen Stecksession tatsächlich ausgegebenen positiven Stromangebot; veraltete, degradierte, sitzungsfremde oder nur vorgemerkte Starts zählen nicht. Phasen-/Wake-up-Warten pausiert die Frist, und die Ladeende-Entprellung erhält zuerst ihre vollständige Prüfzeit.
- 🧱 **Aktive Haltezustände halten auch das aktuelle Cap ein:** Liegt ein früheres Stromangebot oberhalb des neuen Leistungsbudgets, senkt der einzige Wallbox-Ausgang es noch im selben Zyklus ab. Ein Halt darf das Angebot niemals erhöhen; fällt die zulässige Leistung unter die physikalische Mindestleistung, wird stattdessen ein typisierter Stopp ausgeführt.
- 🔁 **openWB-Pro-Phasenwechsel an echte Rückmeldungen gebunden:** Settle- und Sperrzeiten beginnen erst mit dem erfolgreichen Phasenbefehl, und der Wiederanlauf benötigt anschließend einen frischen Geräte-Readback. Die mindestens 480 Sekunden lange Schutzzeit sperrt ausschließlich einen weiteren Phasenwechsel, nicht die bestätigte Wiederaufnahme der Ladung.
- 💾 **Auto-Save nicht durch RAM-Cache blockiert:** Wallbox-Einstellungen binden weiterhin Konfiguration, Pläne, Fahrzeugprofile und Schutzflags gegen echte Paralleländerungen. Der jederzeit neu erzeugbare Konfigurationscache in der RAM-Disk ist dagegen keine Transaktionsautorität mehr und kann einen legitimen Speichervorgang nicht als vermeintliche Paralleländerung abweisen.

### ♨️ Flexible Wärmeverbraucher

- 🧮 **Ein summenerhaltender Gesamtwattrahmen:** Wallboxen, Wärmepumpe und Heizstab erhalten disjunkte Teilbudgets aus genau einem verfügbaren Leistungsrahmen nach der konfigurierten Verbraucherpriorität. Eine Safety-Kürzung darf nur den betroffenen Ausgang reduzieren und die frei gewordene Leistung nicht innerhalb derselben Entscheidungsgeneration heimlich neu vergeben.
- 🔄 **Nicht angenommene Leistung wird wieder frei:** Eine Startbereitschaft reserviert Leistung nur befristet. Danach bindet ausschließlich frische gemessene Leistungsaufnahme den tatsächlich genutzten Anteil; der übrige Rahmen steht im nächsten Zyklus wieder den anderen Verbrauchern zur Verfügung. Bereits laufende, unter ihre Startschwelle modulierende Geräte bleiben mit ihrer exakten Istleistung bilanziert.
- ⏱️ **Luxtronik-Anlauf physikalisch abgebildet:** Die Wärmepumpe erhält ein absolutes 150-Sekunden-Anlauffenster für Signalerkennung, Pumpenvorlauf und Verdichterstart. Eine geringe Pumpenaufnahme um 150 W gilt als belegter Vorlauf, aber noch nicht als Verdichterannahme und verlängert die Frist nicht rollierend. Danach bindet auch ein weiterhin bestätigtes SG-/Relais-Signal keine volle Startreserve mehr: Nur die tatsächlich gemessene Leistungsaufnahme bleibt bilanziert, der Rest wird im nächsten Zyklus neu verteilt; ein später anlaufender Verdichter wird mit seiner frischen Istleistung übernommen. Die SG-/Boost-Freigabe selbst bleibt dabei bestehen, solange der fachliche Wärmebedarf gilt; nur ihr ungenutztes Leistungsbudget wird freigegeben.
- 🔐 **Pro3EM als typisierter Wärmepumpen-Owner:** Der Shelly-Pro3EM-Pfad liest Messung und Relaiszustand in jedem Zyklus strikt zurück und bindet Schema, Provider, Aktor-Owner, innere Messzeit, Leistung sowie Signal- und Rücknahmesemantik. Fehlende, veraltete, widersprüchliche oder konkurrierende Quellen erzeugen keinen neuen Start. Im reinen Beobachtungsmodus wird eine frische Istleistung bilanziert, aber kein Relaisbefehl freigegeben.
- 🕰️ **Schutzzeiten über Neustart und Uhrsprung:** Anlauflease, 40-Wh-Wächter, Mindestlaufzeit und Phasenwechsel verwenden monotone, persistierbare Zeitverträge. Ein Prozessneustart oder eine vor- beziehungsweise zurückspringende Systemuhr kann diese Schutzzeiten weder verkürzen noch unbemerkt neu beginnen lassen. Gespeichertes Wärmepumpen-Signalbookkeeping wird nach einem Neustart erst durch einen frischen, typisierten Aktor-Readback wieder zur laufenden Halteinformation; Sollcache oder alte Statusdatei allein erzeugen weder Reserve noch Ausgang.

### 📊 Ladekurve, Diagramme und Live-Daten

- 🧭 **Nur die passende Prognose:** Bei aktiver Direktvermarktung zeigt das Dashboard ausschließlich den gebundenen DV-Fahrplan; ohne Direktvermarktung ausschließlich die Standardprognose. Auch im Ladekurvenfenster erscheint nur noch die zum Betriebsmodus passende Kurve statt zweier widersprüchlicher Diagramme.
- 🔋 **Aktueller SoC bleibt ein Messwert:** Der aktuelle Hausbatterie-SoC wird als eigener Punkt gezeigt und verändert keine geplante Kurve. Er benötigt einen frischen, zeitlich widerspruchsfreien Messvertrag; fehlt dieser, bleibt der Punkt leer, statt einen alten Verlaufs- oder Planwert als aktuell auszugeben.
- ⚡ **Schnellere und lesbarere Prognose:** Plan-, DV- und Diagnoseverträge werden je Anfrage einmal gebunden und über einen Zeitindex den Diagrammpunkten zugeordnet. Wenige Achsenmarken, sichtbare Tageswechsel und vollständige Datumsangaben im Tooltip machen Prognose und Ladekurve auch über mehrere Tage übersichtlich.
- 📱 **Livewerte nach PWA-Standby sofort aktuell:** Beim Zurückkehren über Sichtbarkeit, `pageshow`, Fokus oder Netzwerk-Wiederkehr wird eine hängende alte Liveabfrage abgebrochen und genau eine neue Request-Generation gestartet. Verspätete Antworten können keine jüngeren Werte mehr überschreiben; abgelaufene CSRF-Sitzungen werden einmalig neu gebunden und eine erforderliche PIN führt in den Sperrbildschirm statt in einen dauerhaften Offline-Zustand.
- 🐛 **Partielle WebSocket-Daten erhalten den Plan:** Reine Messwerttelegramme leeren vollständige Soll-, Zielkorridor-, PV- und Direktvermarktungskurven nicht mehr. Desktop und Mobilansicht verwenden denselben zusammenführenden Cache; erst ein belegter Planwechsel verwirft die alten Kurven.
- 🧭 **Kurve aus gültigen Ankern:** Fehlt in einem partiellen Snapshot die slotweise Zielprojektion, kann die Oberfläche den sichtbaren Sollpfad aus den gültigen eingefrorenen Kurvenankern rekonstruieren. Die Regelentscheidung selbst wird dadurch nicht verändert.
- ⚡ **Schneller erster Diagrammaufbau:** Die SoC-Historie wird über einen engen Verlaufspfad nachgeladen und blockiert nicht mehr die erste sichtbare Darstellung.
- 🔄 **Keine verspäteten oder übernommenen Diagrammzustände:** Live-, Prognose-, Hybrid- und Preisdiagramme besitzen getrennte Request-Generationen. Ein Moduswechsel entwertet alte Antworten und Auto-Refresh-Timer sofort und baut den Diagrammkontext neu auf. Dadurch übernimmt weder die 72-Stunden-Prognose Achse oder Tooltip eines vorherigen Verlaufs noch ein anschließend gewähltes Verlaufsfeld die Datumsachse der Prognose.
- 🗓️ **DV-Planfenster mit eindeutigem Tagesbezug:** Der wirksame Plan zeigt heutige Slots weiterhin kompakt, kennzeichnet morgige Fenster mit „Morgen“ und spätere Fenster mit Datum. Dadurch sehen korrekt nach vollständigem Timestamp sortierte Folgetagsslots nicht mehr wie rückwärts einsortierte Uhrzeiten aus; Mitternacht und Zeitumstellung bleiben eindeutig.
- 🔥 **Pre-Dump verständlich benannt:** Die Oberfläche weist ausdrücklich darauf hin, dass ein freigegebener Heizstab-Pre-Dump Speicherenergie in Wärme verschieben kann. Die Funktion bleibt standardmäßig ausgeschaltet.

### 🚀 Performance und Datenträgerschonung

- 🧠 **Ein gebundener Planobjektgraph:** Der Storage-Plan wird je Dateigeneration nur einmal validiert, eingefroren und im Prozess geteilt. Unveränderte Abrufe öffnen und hashen die mehrere Megabyte große Plandatei nicht erneut; ein Austausch mit gleicher Größe und Zeitstempel wird weiterhin über die Dateigeneration erkannt.
- 🔄 **Eingangsgebundene Neuplanung:** Ändern sich Konfiguration oder semantisch relevante Wetterdaten während einer Berechnung, wird genau einmal auf dem neuen Eingangssatz nachgerechnet. Reine Diagnosefelder lösen keine teure Neuplanung aus; dauernd wechselnde Eingänge führen nicht in eine Endlosschleife.
- 🧩 **Interne Verbraucher ohne PHP-Schleife:** WebSocket, MQTT, Matter und Watchdog lesen einen begrenzten, validierten RAM-Snapshot direkt. Sie erzeugen nicht mehr im Hintergrund fortlaufend vollständige Apache-/PHP-Liveabfragen; ungültige oder veraltete Eingangsdaten bleiben fail-closed.
- 🌐 **Gebündelte Web-Liveabfrage:** Eine Dashboard-Seite teilt einen laufenden Live-Request und verteilt dessen Ergebnis an ihre Ansichten. Die breite Live-Erzeugung ist eine authentifizierte POST-Operation; parallele Aufrufe warten begrenzt auf denselben RAM-Cache statt mehrere teure PHP-Prozesse aufzubauen.
- 🧹 **Erfolgreiche Liveabfragen ohne Access-Log-Schreibsturm:** Ausschließlich erfolgreiche 2xx-Antworten des gebündelten Live-POSTs, des read-only Wallbox-Snapshots und des read-only Watchdog-Status werden nicht mehr dauerhaft protokolliert. Fehler, Umleitungen, Authentifizierung, Installation, Administration und alle anderen Endpunkte bleiben vollständig im Apache-Accesslog sichtbar.
- 🌡️ **Klima-Historie mit gebündelten Schreibzugriffen:** Das einstellbare Erfassungsintervall und alle maschinenlesbaren Messpunkte bleiben erhalten, werden aber im RAM gesammelt und spätestens alle fünf Minuten beziehungsweise beim geordneten Dienstende tageweise angehängt. Ein harter Stromausfall kann dadurch höchstens die noch nicht geschriebenen fünf Minuten verlieren; bestehende Historien, ML-Auswertung und Aufbewahrung bleiben unverändert.
- 💾 **Laufzeitstatus zuerst in der RAM-Disk:** Häufig wechselnde Energie-, Fahrzeug-, Wallbox- und Rettungszustände werden flüchtig aktualisiert. Für den Wiederanlauf werden semantische Änderungen und sicherheitsrelevante Übergänge sofort, aktive Zustände höchstens alle zwei Minuten und verbundene ruhende Fahrzeugsitzungen höchstens alle 15 Minuten dauerhaft gesichert. Ein bestätigtes Sitzungsende erzeugt genau eine Abschlusskante und danach keinen Heartbeat; unkritische letzte Minuten können nach einem harten Stromausfall bewusst fehlen.
- 🛡️ **RAM-Disk bleibt zwingend flüchtig:** Bare-Metal-Dienste erhalten eine direkte systemd-Startsperre auf den exakten `tmpfs`-Mount; Docker prüft denselben Vertrag vor Apache und den Laufzeitdiensten. Ein fehlender Mount, Root-Dateisystem-Fallback oder falscher Dateisystemtyp stoppt die betroffenen Produktdienste fail-closed, während Reparatur- und Wächterpfade erreichbar bleiben.
- 📉 **Plausibilitätsereignisse statt Schreibsturm:** Gleichbleibende Messwertfehler werden als gewichtete Fenster mit Übergang, 15-Minuten-Heartbeat und sauberem Abschluss geführt. Abgeschlossene Tageslogs werden inhaltlich geprüft, atomar komprimiert und nach 30 Tagen begrenzt; Recovery und dünne Messfenster verfälschen weder Ereigniszahlen noch Burst-Diagnosen.
- ♨️ **Luxtronik-Minutenwerte im RAM, kompaktes Archiv:** Der vollständige Minutenverlauf bleibt für Liveauswertung und ML in der RAM-Disk. Persistent entsteht höchstens ein kompakter Datensatz je fünf Minuten; partielle letzte Zeilen nach Stromausfall werden beim Wiederanlauf sicher getrennt und der RAM-Puffer bleibt auch unterhalb des Byte-Limits hart begrenzt. Das kompakte Betriebsarchiv besitzt einen eindeutigen Aufbewahrungsvertrag von sieben Tagen.
- 📚 **Wallbox-Historie ohne Vollscan pro Takt:** Tageswerte werden je Dateigeneration einmal streaming-basiert ermittelt und im RAM wiederverwendet. Die Webansicht hält nur die neuesten Einträge für die Darstellung, während Summen über die vollständige Datei korrekt bleiben.
- 🗜️ **Entscheidungsverläufe gebündelt:** Normale Speicher-, EMS-, Wallbox- und Energie-Manager-Bewegungen werden im RAM zusammengefasst und höchstens alle fünf Minuten komprimiert geschrieben; bestätigte Aktor-, Readback-, Schutz-, Stale- und Kontextwechsel bleiben sofort sichtbar. Reine Empfehlungen lösen keine kritische Persistenz aus. Ein nicht beweisbarer Gzip-Anhang sperrt den Pfad, statt die Historie still zu beschädigen.
- 🗃️ **SQLite ohne stündliche No-op-Schreibvorgänge:** Tages- und Trainingswerte verwenden änderungssensitive UPSERTs; identische Zeilen werden nicht erneut geschrieben. Reguläre Stundenarbeiten teilen eine Transaktion, Schemaänderungen laufen nur bei tatsächlich fehlenden Spalten und Dateirechte werden nur bei einer realen Abweichung gesetzt.
- 🐳 **Begrenzte Docker-Engine-Logs:** Die Compose-Vorgabe rotiert das Engine-Log des Hauptcontainers bei 10 MiB mit drei Dateien und das Watchtower-Log bei 5 MiB mit zwei Dateien. Produkt-, Diagnose- und Historienaufbewahrung behalten ihre getrennten Verträge.

### ♨️ Stiebel-Eltron

- 🐛 **Dokumentierte Statusbits:** Das Statusregister 2501 verwendet B4 für Heizen, B5 für Warmwasser, B6 für den Verdichter, B7 für Sommerbetrieb und B8 für Kühlen.
- 🚿 **Keine Warmwasser-Scheinaktivität:** Betriebsmodus 5 allein wird nicht mehr als aktiver Warmwasserbedarf interpretiert.
- 🛡️ **Frische Herstellerbindung:** Dashboard und Wärmepumpenseite akzeptieren nur erfolgreiche, frische und eindeutig Stiebel-Eltron zugeordnete Livedaten. Veraltete oder fremde Cachewerte werden als veraltet beziehungsweise nicht verfügbar angezeigt statt als scheinbar aktuelle Anlage.

## [5.4.2d] – 2026-07-28

### 📦 Dienst-Wiederanlauf

- 🐛 **Endzustand statt Zwischen-Rückgabecode:** Der Updater bewertet einen erforderlichen Dienst nach `enable` und `restart` anhand des nachweisbaren systemd-Endzustands. Ein zwischenzeitlicher Rückgabecode allein verwirft keinen tatsächlich aktivierten und laufenden Dienst mehr.
- 🔎 **Präzise Fehlerursache:** Bleibt ein erforderlicher Dienst deaktiviert oder inaktiv, enthält das Updateprotokoll den konkreten Enable-, Restart- und Endzustand. Echte Startfehler brechen den Releasewechsel weiterhin fail-closed ab.

### 🔐 Verifizierter Maskenrücklauf

- 🐛 **Optionale fehlende Units:** Eine auf dem Zielsystem nicht installierte optionale systemd-Unit wird beim Maskenrücklauf als legitimer fehlender Zustand behandelt und löst nicht mehr allein durch eine abweichende systemd-Textausgabe einen unbeweisbaren Rollback aus.
- 🛡️ **Maskenschutz bleibt strikt:** Reguläre Unit-Dateien, kanonische `/dev/null`-Masken, unerwartete Links und unlesbare Zustände bleiben getrennt geprüft. Reale Masken- oder Wiederherstellungsabweichungen halten Writer und Aktoren weiterhin sicher gestoppt.

### 📦 Releaseumfang

- 🧹 **Browser-Cache:** Der Service-Worker verwendet die 5.4.2d-Kennung.
- 🛡️ **Keine EMS-Änderung:** Speicher-, Direktvermarktungs-, Wallbox-, Wärme-, Prognose- und Hardwareverträge entsprechen unverändert 5.4.2c.

## [5.4.2c] – 2026-07-27

### 🔌 Wallbox-Netzladeslot

- 🐛 **Wirtschaftlicher Floor blockiert keinen gültigen Slot:** Ein frischer, ausdrücklich gültiger Modus-5-Netzladeslot wird nach bestandenen harten Schutzprüfungen nicht mehr durch `price_plan_storage_protect` beziehungsweise den Pre-Dump-Floor auf 0 A gesetzt.
- 🛡️ **Speicher finanziert das Fahrzeug nicht:** Der Slot hält den Speicher in einem geschützten AUTO-Rahmen. Batterieentladung bleibt auf den nicht durch PV gedeckten Haus- und Wärmepumpenbedarf begrenzt; Wallboxleistung wird nicht als Speicherbedarf eingerechnet.
- 🔐 **Harte Vorränge unverändert:** Nutzer-`Aus`, manuelle und Abort-Sperren, Notstromreserve, Hardware- und Netzlimits, Datenvalidität sowie fehlende oder veraltete Slots bleiben fail-closed. Ohne gültigen Slot bleibt der bisherige Pre-Dump-Stopp wirksam.

### ♨️ Octopus Heat

- 🕒 **Feste Niedrigtariffenster ohne Eco-Zwang:** Die Zeitfenster 02:00–06:00 und 12:00–16:00 werden über eine gemeinsame lokale Tarifzeitachse in `Europe/Berlin` materialisiert und funktionieren unabhängig vom netzdienlichen Eco-Modus.
- 🧭 **Einheitlicher Tarifvertrag:** Wallbox-, EPEX- und Wärmepumpenpfad verwenden dieselbe neutrale Auflösung für wiederkehrende Tarife, ohne einen lokalen Tarif als Börsenpreis umzudeuten.
- 🛡️ **Aktuelle Freigaben bleiben Pflicht:** Wärmepumpen-Automatik, Billigstrom- und Verbraucherfreigabe werden im aktuellen Zyklus erneut geprüft. Fehlerhafte, veraltete oder nach einem Tarifwechsel unpassende Planartefakte schließen sofort fail-closed.

### 🐳 Docker und Release

- 📖 **GHCR und Selbstbau getrennt:** Das veröffentlichte GHCR-Image bleibt der Normalweg. Ein lokaler Entwickler-Build verwendet einen vollständigen Checkout, den lokalen Tag `e3dc-control:local` und einen Compose-Override mit `pull_policy: never`; eine Registry ist dafür nicht erforderlich.
- 🗄️ **Volume-Verträge erklärt:** Daten, Logs, root-private ML-Modelle und optionale Prognosebelege behalten getrennte Rechte-, Aufbewahrungs- und Backupverträge. Es findet keine stille Volume-Migration statt.
- 🧹 **Browser-Cache:** Der Service-Worker verwendet die 5.4.2c-Kennung.

## [5.4.2b] – 2026-07-27

### 📦 Updater-Kompatibilität

- 🐛 **Direkter Alt-Updater-Übergang:** Ein bereits vor dem Zielwechsel gestarteter Updater kann den neuen Finalizer in dem eng gebundenen Kompatibilitätsfall noch aus dem Produktpfad erreichen. Installationswurzel, Ziel-Commit, Version, Tag und Finalizer-Dateien werden vor jeder privilegierten Fortsetzung erneut geprüft.
- 🔐 **Versiegelte Weitergabe:** Der Finalizer wird anschließend einmalig aus einem privaten, root-eigenen und schreibgeschützten Snapshot ausgeführt. Nur ein eindeutiger SHA-/Tag-Erfolgsmarker gilt als Erfolg; der Snapshot wird danach entfernt.
- 🧹 **Erfolg bleibt eindeutig:** Schlägt ausschließlich die abschließende Snapshot-Bereinigung nach einem bereits erfolgreichen Finalizerlauf fehl, bleibt der gebundene Erfolg erhalten und löst keinen falschen Rollback aus. Die Bereinigungsabweichung bleibt sichtbar protokolliert.
- 🛡️ **Keine aufgeweichten Schutzgrenzen:** Symlink-, Hardlink-, Eigentümer-, Modus-, Byte-, Commit-, Versions- oder Bootstrap-Abweichungen bleiben harte Abbruchgründe.

### 📦 Releaseumfang

- 🧹 **Browser-Cache:** Der Service-Worker verwendet die 5.4.2b-Kennung.
- 🛡️ **Keine Regelungsänderung:** Speicher-, Direktvermarktungs-, Wallbox-, Wärme-, Prognose- und Hardwareverträge entsprechen unverändert 5.4.2a.

## [5.4.2a] – 2026-07-27

### 🔋 Speicher-Ladefreigabe

- 🐛 **Keine Selbst-Rückkopplung:** Ein `EMS_USER_CHARGE_LIMIT`-Readback aus frischen, validen `POWER_SETTINGS` gilt nur dann als reflektierter flüchtiger Laderahmen, wenn `maximumladeleistung` ausdrücklich konfiguriert ist und `EMS_USER_CHARGE_LIMIT` sowie `EMS_MAX_CHARGE_POWER` strikt weniger als 50 W voneinander abweichen. Bei fehlenden, veralteten, invaliden oder abweichenden Werten bleibt die USER-Grenze wirksam.
- 🌞 **Kurvenrückstand mit belegtem E3/DC-Pfad:** Liegt der Speicher hinter seiner Ladekurve, öffnet der Manager den Laderahmen in `AUTO` nur bei positiver, frischer E3/DC-only-Evidenz bis `MAX_CHARGE_POWER`. Unbekannte oder veraltete Pfadzuordnung bleibt fail-closed; die Entladung für den Hausverbrauch bleibt offen.
- 🧭 **Externe AC-PV bleibt DC-first:** Bei belegter zusätzlicher AC-PV wird der Laderahmen nicht voll geöffnet, sondern weiterhin sanft nachgeführt und auf die frisch belegte interne E3/DC-PV-Leistung begrenzt. Zusätzliche AC-PV wird nicht als E3/DC-DC-Leistung umgedeutet.
- 🛡️ **Keine Netzladefreigabe:** Die Korrektur fordert weder `GRID` noch einen aktiven Ladebefehl an. Preis-, Lastspitzen- und andere ausdrücklich freigegebene Netzladepfade behalten ihre getrennten Verträge; Nutzer-`Aus`, Notstromreserve und Hardwarelimits bleiben vorrangig.

### 📦 Alt-Updater

- 🐛 **Alte Service-Helfer:** Ein bereits aus 5.4.0a laufender Updateprozess kann nach dem verifizierten Baumwechsel noch die frühere Service-Helper-Signatur im Speicher tragen. Ist die optionale PV-Prognosediagnose ausgeschaltet, wird nur deren Sidecar kontrolliert übersprungen; die Installation der Kerndienste wird dadurch nicht mehr abgebrochen.
- 🛡️ **Explizite Diagnose bleibt fail-closed:** War die Prognosediagnose ausdrücklich aktiviert, erzeugt ein alter Helper keine Unit mit unvollständigem Enable-, Restart- oder Prioritätsvertrag. Der Updater bricht dann mit einem präzisen Hinweis ab und stellt den verifizierten Ausgangszustand wieder her.
- 🔐 **Versiegelter Root-Finalizer:** Nach dem bytegenauen Zielwechsel erzeugt der Updater aus dem freigegebenen Commit einen separaten root-eigenen, schreibgeschützten Ausführungssnapshot. Nur daraus startet der privilegierte Finalizer; Byte-, Modus-, Eigentümer-, Hardlink-, Symlink- und Komponentenabweichungen brechen fail-closed ab.
- 🧭 **Prüfcode und Produktpfad getrennt:** Der Snapshot bestimmt ausschließlich die Herkunft des privilegierten Codes. Installationslogs, systemd-Units, Notifier-Rechte, Web-Wrapper und Sudoers-Einträge werden ausschließlich gegen den doppelt gebundenen Produktpfad erzeugt; kein installierter Dienst verweist nach dem Löschen des Snapshots ins Leere.

### 🔥 Heizstab und Shelly Pro3EM

- 🛡️ **Lokales PV-AUTO AUS ist hart und gehalten:** Nach dem bestätigten 0-W-/AUS-Übergang bleibt der Heizstab aus. PV-Überschuss, Pre-Dump und Marktfreigaben können ihn weder im selben noch in einem unveränderten Folgezyklus erneut einschalten.
- 🛡️ **Hauptschalter schlägt alte Endpunkte:** Bei `heizstab=0` werden noch konfigurierte Heizstab-/Shelly-Aktoren einmal sicher auf 0 W/AUS freigegeben. Automatik, Marktpfad und manueller Vollgasauftrag bleiben danach wirkungslos; ein ausdrücklich als Wärmepumpentyp gewählter Heizstabbetrieb bleibt kompatibel.
- 🔎 **Pro3EM bleibt eigenständig:** Fehlen bei einer Alt- oder Teilkonfiguration die Relaisfreigaben, verwendet der Laufzeitpfad dieselben sicheren Defaults wie die Oberfläche: Relais-ID `-1` und Steuerung aus. Ein separat freigegebener Pro3EM-Wärmepumpenpfad bleibt vom lokalen Heizstab-`PV-AUTO AUS` unabhängig; das globale `AUTO AUS` stoppt und hält beide Pfade.

### 📦 Releaseumfang

- 🧹 **Browser-Cache:** Der Service-Worker verwendet die 5.4.2a-Kennung, damit alte statische Frontenddateien beim Hotfix eindeutig verworfen werden.
- 🐳 **Docker-Dokumentation:** Das veröffentlichte GHCR-Image ist als Normalweg klargestellt. Ein lokaler Selbstbau benötigt einen vollständigen Repository-Checkout als Build-Kontext; Zweck, Rechteklasse, Aufbewahrung und Backupstufe von Daten-, Log-, ML- und Prognosebeleg-Volumes sind getrennt dokumentiert. Die Volume-Topologie selbst bleibt unverändert.
- 🛡️ **Eng begrenzter Hotfix:** Direktvermarktung, Lastspitzenbegrenzung, Wallbox und die fachliche Prognosediagnose entsprechen unverändert 5.4.2. Der Wärmepfad ändert ausschließlich die beschriebenen Nutzer-Aus- und sicheren Default-Kanten.

## [5.4.2] – 2026-07-27

### 🔋 Speicher und Direktvermarktung

- ⚙️ **Lückenloser Tagesplan:** Die Direktvermarktung plant den Tag durchgehend in festen 15-Minuten-Abschnitten. Aktive Speicherfenster, Ladepausen, Verkaufsfenster und passive Hausversorgungszeiten besitzen damit eine eindeutige Semantik; ein bloß künftiges Verkaufsfenster hält den Speicher nicht mehr unnötig fest.
- 🧭 **Saubere Übergänge:** Ein gebundener Abschnitt „Speicherplatz halten“ sperrt ausschließlich das Laden und lässt die Entladung für Hausverbrauch offen. Nach dem letzten PV-Speicherabschnitt geht die Anlage wieder in den passiven E3/DC-AUTO-Betrieb, solange kein stärkerer Storage-Manager-Entscheider wirkt.
- 💶 **Verkaufsfenster:** Wirtschaftlich freigegebene Entladeabschnitte werden nach Wert, verfügbarer Energie, Anlagen- und Notstromreserve geplant. Nicht freigegebene Kandidaten bleiben reine Diagnose und erhalten keinen Hardwareausgang.
- 🌞 **E3/DC-PV-Laderahmen:** Die neue, standardmäßig ausgeschaltete Option begrenzt Kurvenladung und DV-PV-Speichern sanft auf die frisch ermittelte E3/DC-PV-Leistung. E3/DC bleibt dabei in AUTO, Entladen bleibt offen und Leistung eines zusätzlichen AC-Wechselrichters erhöht den Laderahmen nicht. Fehlt ein gültiger PV-Split, werden diese PV-basierten Ladepfade sicher auf 0 W begrenzt. Preis- und ausdrücklich freigegebenes Netzladen bleiben eigenständige Verträge.
- 🔀 **Optionale AC-Speicherroute:** Anlagen mit zusätzlichem AC-Wechselrichter können dessen Energie getrennt für „Reserve sichern“ oder „wirtschaftlich“ freigeben. Standard bleibt `Aus`; E3/DC-DC hat Vorrang, Netzladen wird dadurch nicht freigegeben und unvollständige Topologie- oder DC-Unterdeckungsdaten sperren den Pfad.
- 🛡️ **Befehlsvertrag:** Temporäre `POWER_SETTINGS` bleiben an Owner, Plan, Slot, Datenfrische und typisierten Readback gebunden. Dynamische Speichergrenzen werden ausschließlich flüchtig gesetzt; dauerhafte Geräteeinstellungen werden nicht zyklisch verändert.
- 🔎 **DV-Planer-Shadow:** Ein neuer wirkungsloser Architektur-Shadow bildet jeden 15-Minuten-Slot ausschließlich als Hausversorgung, PV-Speichern, Ladeblock, ausdrücklich freigegebenes Netzladen oder wirtschaftlichen Verkauf ab. Er prüft Planbindung, Datenfrische, Topologie, Netzpunkt, Notstromreserve und verkäufliche Energie, verändert aber weder produktive Plan-/Slot-Identitäten noch den einzigen Hardwareausgang des Storage Managers.

### 📉 Lastspitzenbegrenzung

- ✨ **Peak Shaving am Netzbezug:** Eine neue, standardmäßig ausgeschaltete Regelung begrenzt den mittleren Netzbezug in festen Zähler-Viertelstunden. Sicherheitsabstand, Leistungs- und SoC-Hysterese, Messlückenprüfung sowie Freigabe-Entprellung verhindern ein Flattern der Speichergrenzen.
- 🔋 **Geschützter Puffer:** Die konfigurierbare Speicherschwelle bleibt oberhalb der physischen Notstromreserve. Beim Begrenzen und Halten arbeitet E3/DC weiter in AUTO; der Storage Manager setzt nur flüchtige Lade- oder Entladerahmen und fordert keine Netzeinspeisung an.
- 🔌 **Netz-Nachladung nur mit Freigabe:** Ein Nachladen des Lastspitzenpuffers aus dem Netz ist separat opt-in, an eine lückenlose aktuelle Viertelstunde sowie Hausanschluss- und Leistungsgrenzen gebunden. Standardmäßig bleibt Netzladen aus.

### 🔌 Wallbox-Ladeplanung

- 🕒 **Wiederkehrende Tarife:** Feste Tarife, Octopus Heat und Spezialtarife können ein morgiges 15-Minuten-Ladefenster bereits aus ihrem konfigurierten Tagesprofil planen, auch wenn für morgen noch keine EPEX-Slots vorliegen. Tarifpreis und optionaler Marktpreis bleiben getrennte Felder; dynamische Tarife planen ohne veröffentlichte Zukunftspreise weiterhin nicht.
- 🧭 **Verständliche Fehler:** Schlägt die private Kandidatenplanung fehl, zeigt die Weboberfläche einen validierten deutschen Grund statt nur des Prozesscodes. Ungültige oder freie Prozessausgabe wird nicht in die Oberfläche übernommen.

### 📊 PV-Prognosediagnose

- 🔎 **Historienvergleich:** Die optionale Diagnose vergleicht archivierte E3/DC-DC-Prognosen mit abgeschlossenen nativen 15-Minuten-Historienslots. Trefferabweichung, Richtungsversatz, energiegewichtete Gesamtabweichung und Vergleichsabdeckung bleiben reine Diagnosewerte.
- 🔐 **Strikte Trennung:** Der niedrig priorisierte Diagnosedienst ist standardmäßig aus, besitzt weder einen Steuerpfad noch einen Pfad zum Zurückschreiben der Konfiguration und wirkt weder auf Prognoseauswahl noch Speicherregelung zurück. Bei `Aus` erfolgen keine Historienabfrage und kein Datenbankschreibzugriff.
- 🗄️ **Private Ablage:** Rohdaten liegen außerhalb des Webverzeichnisses in einer größen- und altersbegrenzten SQLite-Datenbank. Das Webportal erhält ausschließlich eine kleine sanitierte Zusammenfassung; Docker verwendet dafür ein separates persistentes Volume.
- 🗺️ **Versionierte PV-Topologie:** Der Konfigurationseditor verwaltet beliebig viele PV-Flächen, Wechselrichtergruppen und Provider-Bindungen als einen geprüften Vertrag. Fehlende Messwerte bleiben unbekannt statt zu einer Messnull zu werden; eine neue Topologie-Revision verhindert die Wiederverwendung eines veralteten PV-Splits.

### 📦 Installation, Docker und Oberfläche

- 🐛 **Frische Erstinstallation:** Der Installer unterscheidet eine wirklich neue, eine sicher fortsetzbare unvollständige und eine widersprüchliche bestehende Installation. Nur beim erstmaligen Erzeugen der Konfiguration wird die Einzelanlagenrolle vorbelegt; vorhandene Anlagen ohne gültige HA-/Shadow-Bindung bleiben fail-closed.
- 🛡️ **Ehrlicher Abschlussstatus:** Fehlgeschlagene Paket-, Konfigurations-, Webportal-, Rechte- oder Dienstschritte werden bis zum Menü- und Prozess-Exitcode weitergereicht. Eine unvollständige Installation wird nicht mehr als erfolgreich abgeschlossen gemeldet.
- 🧭 **Gleicher sicherer Installationsweg:** Der direkte Aufruf `--install-all` verwendet dieselbe Zustands- und Rollenprüfung wie der interaktive Menüpunkt. Frische, fortsetzbare und widersprüchliche Bestände werden damit unabhängig vom Einstieg gleich behandelt.
- 🔐 **Unveränderte Nutzerkonfiguration:** Beim gebundenen Release-Wechsel bleiben sowohl die Pfad-/Default-Normalisierung als auch die alte Telegram-Migration außerhalb des eingefrorenen Release-Fensters. Eine bestehende Betriebskonfiguration und ihr Legacy-Pfadspiegel werden dadurch nicht still ergänzt; eine echte Erstinstallation darf die benötigten Startwerte weiterhin erzeugen.
- 🛡️ **Große Wiederherstellungen:** Der transaktionale Rücklauf reserviert sein prozesslokales Dateideskriptorbudget vor der ersten Änderung und stellt das vorherige Soft-Limit anschließend wieder her. Auch umfangreiche Installationsbäume bleiben damit atomar rückrollbar, ohne Systemlimits dauerhaft zu verändern.
- 🐛 **Altprozess-Kompatibilität:** Ein von 5.4.0a gestarteter Web-/Konsolen-Updater kann nach dem verifizierten Baumwechsel mit seinem bereits geladenen alten Backup-Modul fortfahren. Die Rechteprüfung erkennt dessen ältere Validator-Signatur, bleibt strikt read-only und fail-closed und verlangt keine Funktion aus einer noch nicht neu gestarteten Python-Generation.
- 🐳 **Optionale Diagnose im Container:** Modell und Prognosediagnose besitzen getrennte private Volumes. Der Diagnosedienst startet nur bei ausdrücklicher Aktivierung; ein Containerneustart übernimmt die Änderung.
- 🔒 **Gebundener Docker-Rückfall:** Der veröffentlichte Rückfallstand ist zusätzlich zum Tag an seinen unveränderlichen OCI-Index-Digest gebunden. Die angezeigte Rückfallkette prüft vor `pull` und `up`, dass Compose exakt dieses Image auflöst.
- 🖥️ **Verständlichere Konfiguration:** Der Tarifbereich gruppiert Direktvermarktung, Preisfunktionen und Lastspitzenbegrenzung klarer und beschreibt Schalter in Alltagssprache. Bei zwei Wallboxen stehen die individuellen Einstellungen durchgehend nebeneinander; bei einer Wallbox wird die verfügbare Breite genutzt.
- 🧭 **Passende Statusanzeige:** Das Dashboard unterscheidet passive Hausversorgung, Speicherplatz halten, PV-Speichern und Verkauf anhand des tatsächlich wirksamen Vertrags. Redundante HOLD-Felder sind zusammengeführt.
- 🧹 **Browser-Cache:** Der Service-Worker verwendet die 5.4.2-Kennung, damit alte statische Frontenddateien beim Releasewechsel eindeutig verworfen werden.

## [5.4.1d] – 2026-07-25

### 🌡️ Klima

- 🐛 **Docker-Messdienst:** Der read-only Klimamonitor startet im Container genau einmal und übernimmt Aktivierung, Deaktivierung sowie Kanalwechsel beim nächsten Abfragezyklus. Ein deaktivierter Klimaverbraucher fragt keinen Shelly ab und schreibt keine Verlaufshistorie.

### 📦 Update und Backup

- 🛡️ **ML-Sperrdatei:** Der laufende Updater kann vor dem verifizierten Backup ausschließlich einen eindeutig regulären, unverlinkten, größenbegrenzten und unbelegten Alt-Lock auf den gebundenen Installationsbenutzer und Modus `0600` normalisieren. Modell, Manifest und Lockinhalt bleiben unverändert; Symlinks, Hardlinks, fremde Eigentümer, Übergröße und belegte Locks brechen weiterhin hart ab.
- 🔎 **Alt-Updater:** Stände bis einschließlich 5.4.1c prüfen das Backup noch mit ihrem alten Code. Wenn sie bereits an `.ml_model.lock` abbrechen, benötigen sie einmalig den dokumentierten Metadaten-Feldfix; die Datei darf nicht gelöscht werden.

### 🔋 Batterie-Vitals

- 🐛 **Packindex:** E3/DC-Systeme, die `BAT_DCB_INDEX` als signierten RSCP-`Int32` statt als `Uint16` zurückgeben, zeigen ihre einzelnen Batteriepacks wieder an. Nur nichtnegative Ganzzahlen im zulässigen Bereich und exakt passend zum angeforderten Pack werden akzeptiert.

### 🧭 Releaseumfang

- 🧹 **Browser-Cache:** Der Service-Worker verwendet die 5.4.1d-Kennung, damit alte statische Cache-Namensräume beim Releasewechsel eindeutig verworfen werden.
- 🛡️ **Keine Regelungsänderung:** Speicher-, Wallbox-, Wärme- und Direktvermarktungsentscheidungen bleiben gegenüber 5.4.1c unverändert.

## [5.4.1c] – 2026-07-24

### 🐳 OCI-Verifikation

- 🐛 **Releaseversion:** Der OCI-Verifier prüft die vom Workflow bereits exakt gebundene Releaseversion mit einer strengen Versionssyntax statt einer bei jedem Wartungsrelease manuell zu erweiternden statischen Liste.
- 🛡️ **Keine ungeprüfte Promotion:** Der 5.4.1b-Lauf erzeugte einen vollständigen temporären AMD64-/ARM64-Kandidaten mit SBOM und Provenance, endete aber vor den unveränderlichen Stable-Tags. 5.4.1c ändert keine Speicher-, Wallbox-, Wärme- oder Direktvermarktungsregelung.

## [5.4.1b] – 2026-07-24

### 🐳 Docker-Veröffentlichung

- 🐛 **Vollständige Release-Historie:** Das vorgelagerte Docker-Gate lädt die Git-Historie vollständig, bevor es Commit, Tree, Noreply-Identität und den parentlosen Veröffentlichungs-Root prüft. Eine mit jedem Wartungsrelease zu klein werdende feste Checkout-Tiefe kann den Build dadurch nicht mehr vorzeitig blockieren.
- 🛡️ **Fail-closed beibehalten:** Der fehlgeschlagene erste 5.4.1a-Lauf endete vor Image-Build, SBOM, Provenance und Tag-Promotion. 5.4.1b ändert keine Speicher-, Wallbox-, Wärme- oder Direktvermarktungsregelung.

## [5.4.1a] – 2026-07-24

### 📦 Update, Backup und Erstinstallation

- 🐛 **Web-Update-Abschluss:** Die Weboberfläche wertet den strukturierten Exitcode und den kanonischen Erfolgsmarker des Installers aus. Ein erfolgreich abgeschlossener Release-Wechsel wird nicht mehr fälschlich als unklarer Fehler angezeigt; laufende oder tatsächlich fehlgeschlagene Prozesse bleiben davon getrennt.
- 🛡️ **Private ML-Sperrdatei:** Neu erzeugte `.ml_model.lock`-Dateien erhalten unmittelbar den gebundenen Installationsbenutzer und Modus `0600`. Die Rechteprüfung kann ausschließlich einen eindeutig regulären, unverlinkten und unbelegten Alt-Lock normalisieren; Modell- und Manifestbytes bleiben unverändert.
- 🐛 **Frische Erstinstallation:** Der normale Einstieg `e3dc-setup` übernimmt keine unvollständige Release-Bootstrap-Bindung mehr. Der SHA-gebundene Runner-/Zielvertrag für echte Release-Bootstraps bleibt unverändert fail-closed.
- 🔎 **Bestandsinstallationen:** Ein bereits durch einen unsicheren ML-Lock blockierter Alt-Updater benötigt einmalig die dokumentierte enge SSH-Reparatur, weil der alte Prozess sein Backup vor dem Laden der neuen Releasebytes prüft.

### 📖 Dokumentation

- 📝 **E3/DC-Geräteeinstellungen:** README und Hilfe trennen die E3/DC-eigene Wetterladung von der unabhängigen Open-Meteo-/Forecast-Prognose. RSCP-Zugang, Dachflächen, Notstromreserve, Hardwaregrenzen, Ein-Entscheider-Regel und gemeinsame Systemzeit sind als empfohlene Betriebsgrundlage dokumentiert.

## [5.4.1] – 2026-07-24

### 🔌 Wallboxen und Fahrzeuge

- 🐛 **openWB Pro:** Start, Wiederanlauf, Pause, Ladeende und Phasenwechsel sind an frische Stecksession, Sollabsicht und bestätigten Geräte-Readback gebunden. Die 480-Sekunden-Sperre verhindert ausschließlich einen weiteren Phasenwechsel und blockiert nicht die laufende Ladung.
- ⚙️ **Leistungsfaires Balancing:** Fahrzeug-, Nutzer- und Ladepunktgrenzen werden anhand der tatsächlichen Phasenzahl in Leistung umgerechnet. Ein- und dreiphasige Amperewerte werden nicht mehr als bedeutungslose skalare Summe behandelt.
- 🛡️ **Phasenschutz:** Die physische Zuordnung der Wallboxphasen zum Netzanschlusspunkt ist konfigurierbar. Eine dynamische einphasige Freigabe über 20 A bleibt ohne echten, frischen PCC-RMS-Stromvektor fail-closed.
- 🔋 **Fahrzeug-SoC:** Der Ioniq-5-Fallback verwendet ausschließlich die Energie der aktuellen Stecksession und bleibt hinter echten Fahrzeug- oder Wallboxwerten zurück.

### 📦 Update und Docker

- 🐛 **Alt-Updater:** Der unterstützte Erstwechsel aus 5.3.2b bindet Rechte-, Wrapper-, Dienst- und venv-Übergänge an den verifizierten Zielbaum. Konfigurationen und bereits installierte optionale Dienste werden erhalten, statt aus bloßen Altwerten neu aktiviert zu werden.
- 🐳 **Docker-Migration:** Die Installation prüft offizielle Docker-Pakete, Compose-Version, Zielimage und Containerzustand explizit. Ein fehlgeschlagener Pull darf kein altes Image als erfolgreiches Update ausgeben.
- 🔐 **Container-Promotion:** Ein einziger GitHub-Workflow baut den Multiarch-Kandidaten, prüft Digest, SBOM und Provenance erneut und setzt erst danach die unveränderlichen Versions-Tags sowie `latest`.
- 🛡️ **Watchtower:** Der nicht mehr gepflegte Updater ist kein Standarddienst mehr und bleibt nur als ausdrücklich aktiviertes Compose-Profil verfügbar.

### 🖥️ Frontend, Diagnose und Sicherheit

- ✨ **Statusanzeigen:** Netzfrequenz sowie aktive SG-Ready-/Shelly-Freigaben sind im Dashboard sichtbar; neue optionale Statuswerte bleiben bei fehlender Quelle unbekannt statt als falsche Nullwerte zu erscheinen.
- 🔎 **Regelkonflikte:** Eine gleichzeitig aktive E3/DC-Wetterladung wird als externer Speicher-Vetozustand sichtbar, ohne E3/DC-Einstellungen automatisch umzuschreiben.
- 🔐 **XSS-Härtung:** Gemeldete Status- und Fehlertexte im Konfigurationseditor werden als Text statt als ungeprüftes HTML eingesetzt.
- 🐛 **Frontend-Verfügbarkeit:** Der optionale Legacy-Preisfallback erhält einen eindeutigen Nur-Lese-Rechtevertrag für den Webserver. Eine fehlende oder vorübergehend nicht lesbare Preisdatei wird ignoriert und kann Startseite oder Vitals nicht mehr mit HTTP 500 blockieren.
- 🐛 **Batterie-Vitals:** Jeder DCB-Pack wird mit seinem typisierten Packindex abgefragt und, wenn vorhanden, an den passenden Antwortindex gebunden. Dadurch wird nicht mehr derselbe erste Pack mehrfach angezeigt.
- 🧱 **WebSocket:** Der Dienst bindet nur noch an `127.0.0.1:8765`; das Dashboard greift gleichursprünglich über den Webserver-Pfad `/ws` zu.

## [5.4.0e] – 2026-07-23

### 📦 Definierter Übergang aus 5.3.2b

- 🐛 **Hybrid-Updater:** Der vor dem Git-Wechsel gestartete 5.3.2b-Prozess übernimmt aus der Zielpolicy nur die sieben Pflichtdienste und erfasst ausschließlich bereits installierte Zusatzdienste. Davon werden nur die in der eingefrorenen Konfiguration aktiven Dienste gestartet; deaktivierte Dienste bleiben aus. 5.3.2b ist dafür die bewusst veröffentlichte Übergangsbasis; ältere oder nicht verwandte Installationen verwenden zuerst deren Bootstrap.
- 🛡️ **Keine implizite Hardwareaktivierung:** Alte oder vorbereitete Konfigurationsfelder installieren und starten keine bislang fehlenden Wallbox-, Wärme- oder Integrationsdienste.
- 🔎 **Sichtbare Diagnose:** Konfigurierte, aber nicht installierte Zusatzmodule werden im Updateprotokoll ausdrücklich genannt und bleiben bis zu einer bewussten Installation über das Install-Center unverändert.
- 🔐 **Eingefrorene Konfiguration:** Die Rechteprüfung migriert oder verändert die bereits gebundene Betriebskonfiguration während des Release-Wechsels nicht.
- 🔌 **Unveränderte Regelung:** openWB Pro, Speicher, Direktvermarktung, Wärmepumpe und sonstige fachliche Reglerbytes entsprechen unverändert 5.4.0d.

## [5.4.0d] – 2026-07-23

### 📦 Web-Update und Installation

- 🐛 **Private Verzeichnisse:** Auch unter einem `setgid`-Datenordner werden private Verzeichnisse exakt auf `0700` gesetzt. Matter-Storage, Wallbox-Planer und Zusatz-WR-Migrationsbackups verwenden denselben Modusvertrag.
- 🐛 **Private Wallbox-Lockdatei:** Klassischer Konfigurationspfad, transaktionaler Planer und Installer verwenden jetzt einheitlich den Dateimodus `0600`.
- 🛡️ **Wiederherstellung:** Breite Webroot-Reparaturen überspringen die privaten Matter- und Wallbox-Bäume, statt deren Eigentümer oder Modi zu verbreitern.
- 🔐 **Sicherheitsgrenze:** Der private Planer-Transaktionsbaum bleibt ausschließlich `www-data:www-data` vorbehalten; der Rechtevertrag wurde nicht gelockert.
- 🔌 **Unveränderte Wallbox-Regelung:** Start, Phasenwechsel, Pause und SoC-Verfolgung der openWB Pro entsprechen unverändert dem in 5.4.0c geprüften Stand.

## [5.4.0c] – 2026-07-23

### 📦 Web-Update und Installation

- 🐛 **Altstand-Übergang:** Der reale Web-Update-Pfad aus 5.3.2b übernimmt nach dem Git-Wechsel den neuen, service-neutralen Rechtevertrag. Die vom Updater selbst angehaltenen Dienste gelten nicht mehr als Fehler.
- 🐍 **PEP 668:** Leere Paketlisten lösen keinen System-`pip`-Aufruf aus. Abhängigkeiten bleiben an das verwaltete Benutzer-venv gebunden.
- 🔧 **Alt-Wrapper-Bootstrap:** Ist der alte privilegierte Wrapper nicht ausführbar, erfolgt einmalig ein interaktiver Konsolenaufruf. Ein nicht startbarer Root-Einstieg wird nicht aus dem Webprozess heraus umgeschrieben.
- 🔐 **Sudoers-Kompatibilität:** Fremde, klar abgegrenzte ioBroker-Freigaben werden gemeldet, aber nicht verändert und blockieren das E3DC-Control-Update nicht. Fremde direkte E3DC-`systemctl`-Freigaben bleiben fail-closed.
- 🔎 **Diagnose:** Fehler aus Rechteprüfung, Ziel-Finalizer und Dienststart werden vollständig an Weboberfläche und Konsole weitergereicht.

### 🔌 openWB Pro

- 🐛 **Start und Phasenwechsel:** Nach einem bestätigten Anstecken wird die Stromfreigabe ohne alten Nullanker zügig bis zum zulässigen Budget nachgeführt. Der eigentliche Phasenwechsel nutzt eine kurze sichere CP-Unterbrechung; die anschließenden 480 Sekunden sperren nur einen weiteren Phasenwechsel, nicht den Wiederanlauf.
- ⏸️ **Manuelle Pause:** Ein zentral blockierter STOP gilt nicht mehr als erfolgreich. Die Pause wird erst nach bestätigtem STOP oder bereits real stehender Wallbox übernommen; das private Transaktionsverzeichnis erhält die passenden Webserverrechte.
- 🔋 **Fahrzeug-SoC:** Der Ioniq-5-Fallback ist eindeutig an Fahrzeugprofil und Stecksession gebunden und verwendet ausschließlich die Energie der aktuellen openWB-Pro-Ladesitzung. Echte Fahrzeug- oder Wallboxwerte haben Vorrang.
- 🛡️ **Ladeende nach Neustart:** Ein bestätigtes Ladeende bleibt über einen Manager-Neustart an dieselbe Stecksession gebunden. Ein niedrigerer interpolierter SoC, ein einzelnes Disconnect-Bild oder ein Phasenübergang dürfen keinen neuen 6-A-Start auslösen.

## [5.4.0b] – 2026-07-23

### 📦 Installation, Update und Docker

- 🐛 **Fehlerbehebung:** Auf Debian-Systemen mit PEP 668 installiert das Release-Update keine Python-Pakete mehr in die verwaltete Systemumgebung. Ab dem neuen Updater werden Abhängigkeiten im gebundenen Benutzer-venv aktualisiert; ein fehlendes Standard-venv kann nach Installation von `python3-venv` kontrolliert neu angelegt werden. Der direkte erste Wechsel aus 5.3.2b setzt das vorhandene venv voraus.
- 🔄 **Kompatibilität:** Docker-Installationen werden im Web- und Konsolen-Updater eindeutig erkannt. Statt eines ungeeigneten Release-Wechsels im Container werden die notwendigen `docker compose`-Befehle für den Host angezeigt.
- 🐳 **Docker-Update:** Die mitgelieferte Compose-Datei folgt ohne bewussten Pin dem geprüften Stable-Tag `latest`. Ein fest eingetragener Versions-Tag bleibt fest; `docker compose config --images` macht das Ziel vor dem Pull sichtbar.
- 🛡️ **Docker-Rückfall:** Ein Stable-Container wird nur noch freigegeben, wenn das in der Update-Policy beworbene Rückfall-Image tatsächlich für AMD64 und ARM64 verfügbar ist. Historische Quellbytes und der aktuelle OCI-Prüfer bleiben dabei getrennt gebunden.
- 🛡️ **Bare-Metal-Rückfall:** `v5.3.2b` bleibt als Docker-Image verfügbar, wird auf Bare Metal aber nicht mehr als Programm-Rückfall angeboten. Der Altstand besitzt keinen zielgebundenen Finalizer; verifizierte Datei-Backups bleiben der sichere Bare-Metal-Rückweg.
- 🛡️ **Sicherheit:** Wrapper- und sudoers-Reparatur verwenden einen gebundenen Snapshot und rollen Teilfehler atomar zurück. Runtime- und Session-Rechte werden von diesem engen Reparaturpfad nicht nebenbei verändert.
- 🧱 **Stabilität:** Kanonische systemd-Masken auf `/dev/null` werden als Zustand gesichert und beim Restore verifiziert. Sie werden nicht mehr fälschlich wie reguläre Unit-Dateien behandelt; andere Symlinks bleiben gesperrt.
- 🔐 **Sicherheit:** Ab dem neuen Updater führt nach dem Git-Wechsel ein eigener, an Ziel-SHA und Zielbaum gebundener Finalizer die Installation fort. Der erste Wechsel aus 5.3.2b bleibt im alten Prozess, übernimmt aber nach dem Reset den neuen service-neutralen Rechtevertrag.

### 🔌 Wallboxen und PV-Kurve

- 🐛 **Fehlerbehebung:** Eine openWB Pro erhält nach einem bestätigten Ab- und Wiederanstecken eine neue, entprellte Stecksession. Alte Stop-, Null- und Wake-up-Belege blockieren den nächsten berechtigten Ladestart nicht mehr.
- 🛡️ **Sicherheit:** Die Startwiederherstellung gibt zuerst den Ladestrom aus. Im Automatikmodus folgt ein kurzer CP-Wake-up nur bei ausbleibender Ladeannahme und unterstützter openWB-Pro-API; ausdrückliche Nutzerfreigabe oder -sperre bleibt erhalten. Das Versuchsbudget ist begrenzt und persistent.
- 🧱 **Stabilität:** Ein bereits frisch bestätigtes Phasenziel wird ohne neuen Phasenwechsel übernommen. Nach einem tatsächlichen Wechsel bleibt das vollständige 480-Sekunden-Schutzfenster aktiv; fremde alte Ausgangsbelege werden nicht mit einer neuen Reservierung vermischt.
- ⚙️ **Regelung:** Alle unterstützten Wallboxpfade verwenden für fehlendes PV-Budget denselben typisierten Halte-/Stoppvertrag. Kurze Batteriestützung einer bereits laufenden PV-Ladung bleibt innerhalb des bestehenden Energiebudgets zulässig; harte Stopps und ein verbrauchtes Budget haben Vorrang.
- 🔎 **Diagnose:** Die Anzeige „sicherer Wiederanlauf nach Neustart“ erscheint nur noch, wenn die zugehörige Phasentransition tatsächlich aktiv ist.

### 🔋 Speicher und Ladefähigkeit

- 🐛 **Fehlerbehebung:** Liefert eine E3/DC-Generation keine vollständig auswertbare `POWER_SETTINGS`-SET-Antwort, kann ein unmittelbar anschließender, exakt passender und typisierter GET-Readback die sichere Wirkung bestätigen. Die unbekannte SET-Bestätigung bleibt dabei ausdrücklich als unbekannt ausgewiesen.
- 🛡️ **Sicherheit:** Fehlende oder abweichende GET-Werte bleiben ein Fehler und werden nicht als erfolgreiche Bestätigung umgedeutet.
- ⚙️ **Regelung:** Frische RSCP-Hardwaregrenzen dürfen den konfigurierten Ladewert nur absenken. Temporäre `POWER_SETTINGS`-Werte werden nicht als neue dauerhafte Ladefähigkeit in PV-Kurve oder Headroom-Planung übernommen.

## [5.4.0a] – 2026-07-22

### 📦 Installation und Update

- 🐛 **Fehlerbehebung:** Optionale Matter-Abhängigkeiten werden nicht mehr im normalen Core-Update installiert. Damit blockieren Konflikte zwischen Node.js-, npm- oder Avahi-Paketen das E3DC-Control-Update nicht mehr.
- 🛡️ **Sicherheit:** Der Web-Updater prüft seinen privilegierten Wrapper gegen die exakten Release-Bytes. Eine reine CRLF-Shebang-Beschädigung kann kontrolliert repariert werden; unbekannte Byteabweichungen, Symlinks oder Mehrfachlinks brechen sicher ab.
- 🔎 **Diagnose:** Die Weboberfläche unterscheidet einen beschädigten oder nicht ausführbaren Wrapper von einer fehlenden passwortlosen sudo-Freigabe und zeigt den passenden Reparaturweg.

### ♨️ Klima und Shelly

- 🔄 **Kompatibilität:** Alte Shelly-EM-Zähler der ersten Generation werden über die lokale read-only-Status-API unterstützt. Die automatische Erkennung kann von RPC auf Gen1 zurückfallen; Kanal 0, Kanal 1 oder die Summe werden explizit ausgewertet.
- 🛡️ **Sicherheit:** Fehlende, nicht endliche oder vom Gerät als ungültig markierte Shelly-Messwerte werden nicht als echte `0 W` übernommen und erzeugen keine Regelwirkung.

## [5.4.0] – 2026-07-21

### 🔋 Speicher und Direktvermarktung

- 🛡️ **Sicherheit:** Pro Domäne und Aktor gibt es genau einen Regel-Owner. Plan, Slot, Marktfenster, Freigabe, Geräteanforderung und Rücklesung bleiben über dieselbe Entscheidung gebunden.
- ⚙️ **Regelung:** Interne DC-PV und zusätzliche AC-Erzeuger werden getrennt bilanziert. DC- und Netzpunktdruck werden mit dem größeren Wert bewertet und nicht doppelt addiert.
- 🧱 **Stabilität:** Ungültige oder veraltete Markt-, Anlagen- und Rücklesedaten führen zu einer inaktiven Freigabe. Diagnosekandidaten können keine Hardwarewirkung erhalten.
- 🛡️ **Sicherheit:** Netzstrom-Arbitrage bleibt in 5.4.0 wirkungslos. Bestehende Altwerte werden kompatibel erhalten, erzeugen aber weder Owner noch Speicherbefehl.
- 🛡️ **Sicherheit:** Notstromreserve, Gerätegrenzen und dauerhafte RSCP-Einstellungen bleiben von Optimierung und Update unberührt.

### 🔌 Wallboxen und Fahrzeuge

- 🐛 **Fehlerbehebung:** Eine angesteckte und freigegebene openWB Pro verwirft abgelaufene eigene Phasenreservierungen und veraltete Nullanker. Nach bestätigter Bereitschaft wird die positive Startfreigabe ohne Umstecken oder Manager-Neustart erneut projiziert.
- ⚙️ **Regelung:** Das Mehr-Wallbox-Balancing verwendet die tatsächlichen L1/L2/L3-Stromvektoren, die reale Phasenzahl, Fahrzeug- und Ladepunktgrenzen sowie die Netzpunktreserve. Ein- und dreiphasige Amperewerte werden nicht pauschal addiert.
- 🧱 **Stabilität:** Die ruhige PV-Kurve folgt dem nachhaltigen PV- und Ladekurvenbudget mit Hysterese und Mindestlaufzeit. Eine bereits laufende Ladung darf kurze Einbrüche mit höchstens 75 Wh Batteriestützung überbrücken; Kaltstart und Phasenwechsel werden nicht aus dem Speicher finanziert.
- 🛡️ **Sicherheit:** Geschützte openWB-Phasenwechsel setzen zuerst 0 A, übergeben anschließend das Phasenziel an `phasetarget` und warten auf frische, bestätigte Rückmeldungen. Danach sperrt ein persistenter 480-Sekunden-Cooldown ausschließlich den nächsten Phasenwechsel. E3DC-Control sendet dabei keinen zweiten CP-Befehl. Direkte E3/DC-Sun-/Auto-/Abort-, Maximalstrom- und native Phasenbefehle bleiben gesperrt.
- 🔄 **Kompatibilität:** Der bestätigungsgebundene WBchar6-Pfad für vorhandene E3/DC-Wallboxen sowie openWB, openWB Pro und go-e bleiben unterstützt. Ein ausdrücklich deaktivierter Ladepunkt bleibt im Nur-Status-Betrieb.

### ♨️ Wärme und iDM

- 🛡️ **Sicherheit:** Wallboxaktionen oder der Verlust eines Wallboxkontexts stoppen keine bereits laufende Wärmepumpe eigenständig. Hardwarebefehle bleiben an frische, treiberspezifische Rückmeldungen gebunden.
- 🔎 **Diagnose:** Der manuelle iDM-Scanner liest Input-Register 1006 genau einmal per FC04. Ohne passend gebundenes Modell, Protokoll, Firmware und Unit-ID bleibt der Rohwert unbewertet; der Scanner schreibt keine Register.
- 🧱 **Stabilität:** Teilweise bestätigte Schreibfolgen gelten als Fehler. Die Steuerung fällt ausschließlich auf einen bestätigten sicheren Zustand zurück.

### 🖥️ Weboberfläche und Diagnose

- ✨ **Verbesserung:** Desktop- und Mobile-Layout besitzen getrennte Revisionen. Gleichzeitige Änderungen werden erkannt, unbekannte Felder bleiben erhalten und Tablet-/Querformatansichten ordnen Energiequellen und Verbraucher ohne abgeschnittene Badges an.
- 🔎 **Diagnose:** Regelruhe, Owner, Freigabe, ACK, Readback und Hardwarewirkung werden getrennt ausgewiesen. Fehlende Historie wird als Beweisgrenze statt als vermeintliche Ruhe behandelt.

### 📦 Installation, Update und Distribution

- 🛡️ **Sicherheit:** Update, Backup, Rollback und Web-Planung arbeiten transaktional. Unvollständige Sicherungen, Timeouts und Teilfehler brechen ab und erhalten den letzten konsistenten Konfigurations- und Dienstzustand.
- 🔐 **Datenschutz:** Lokale Konfigurationen, Sicherungen, Diagnosen, Zugangsdaten und Repository-Historie sind aus dem Docker-Buildkontext ausgeschlossen.
- 🔄 **Migration/Kompatibilität:** Der in `UPDATE_POLICY.json` exakt gebundene Rückfall-Tag `v5.3.2b` ist ausschließlich für Docker freigegeben; Bare Metal bietet für diesen Altstand keinen Programm-Rückfall an.

## [5.3.2b] – 2026-07-15

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Der fachliche Produktstand von 5.3.2a bleibt unverändert; Versions-, Aktualisierungs- und Rückfallhinweise wurden auf 5.3.2b fortgeschrieben.
- 🛡️ **Sicherheit:** Personen-, Anlagen- und private Betriebsbezüge wurden in öffentlichen Texten und Beispielen neutralisiert, ohne die Produktfunktionen zu verändern.

## [5.3.2a] – 2026-07-11

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Änderungen von Leistungsgrenzen gelten erst nach einer passenden Geräterückmeldung als übernommen. Eine garantierte Wiederaufnahme nach einer 0-W-Begrenzung wird nicht behauptet.
- 🧱 **Stabilität:** Abweichende Rückmeldungen lösen einen kontrollierten, begrenzten Wiederholungsversuch aus.
- 🔎 **Diagnose:** Vorgabe, Geräteantwort und anschließend gelesener Istzustand werden getrennt ausgewiesen.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Reservierte Leistung bleibt während eines Wallbox-Phasenwechsels für Wärmeverbraucher erhalten.
- 🧱 **Stabilität:** Kurze Ladeunterbrechungen werden korrekt eingeordnet und lösen keine konkurrierenden Neustarts aus.

### 📈 Direktvermarktung und Strompreise

- ⚙️ **Regelung:** Viertelstundenpreise werden lückenlos verarbeitet; aktive Eingriffe bleiben auf belastbare Day-Ahead-Preise beschränkt.
- 🛡️ **Sicherheit:** Tarifpreise und Erlöse aus Direktvermarktung bleiben getrennt. Laden, Entladen und Speicherplatzschaffen erfolgen nur bei tatsächlichem Bedarf.
- 🔎 **Diagnose:** Absicht, Ausführung und bestätigte Wirkung werden getrennt dargestellt.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Direktvermarktung, tatsächlich vermarktete Energie und die beteiligten PV-Quellen werden nachvollziehbarer dargestellt.
- 🛡️ **Sicherheit:** Der Status einer 0-W-Begrenzung ist sichtbar; neutrale Beispiele ersetzen frühere Anlagen- und Personenbezüge.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Klassische und moderne Ansicht, bestehende Smart-Home-Kopplungen und die Nur-Lese-Vergleichsinstanz bleiben unterstützt.
- 🔄 **Migration/Kompatibilität:** Der direkte Rückfall auf 5.3.2 bleibt vorgesehen; experimentelle Folgefunktionen gehören nicht zu diesem Stand.

## [5.3.2] – 2026-07-10

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Automatik-Freilauf sendet keinen 0-W-Befehl.
- 🐛 **Fehlerbehebung:** Leistungsbudget-Rückfall bleibt netzneutral.
- 🛡️ **Sicherheit:** Harte Stopps bleiben erhalten.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Phasenfolge rückt nur nach Erfolg weiter.
- 🧱 **Stabilität:** Häufige Wechsel-Historie kann nicht dauerhaft blockieren.
- ✨ **Verbesserung:** openWB und go-e präzisiert.
- ✨ **Verbesserung:** Ladevorgang-Identität zentralisiert.

## [5.3.1j] – 2026-07-10

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** SHI bleibt auch in NORMAL offen.

## [5.3.1i] – 2026-07-10

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Nur dokumentierte FC04-Bereiche.
- ✨ **Verbesserung:** Verbindung und Schreibschutz unverändert.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Gezielter Tag-Fetch.
- ✨ **Verbesserung:** Git als Installationsnutzer.

## [5.3.1h] – 2026-07-10

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Gemeinsamer Betriebsdaten-Zwischenspeicher und kompakter Steuervertrag.
- ✨ **Verbesserung:** Persistente Akquise als optionale Aktivierung.
- ✨ **Verbesserung:** Entscheidungsverlauf v2.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** SHI-Auftrag und physischer Zustand getrennt.
- 🛡️ **Sicherheit:** Warmwasserlauf folgt der realen Verdichterflanke.
- ⚙️ **Regelung:** Quell-Erholung ohne Wolkenflattern.
- ✨ **Verbesserung:** Weiche Luxtronik-Sperre benannt.
- ✨ **Verbesserung:** Kühlanforderung geschützt und diagnostizierbar.

### 📈 Direktvermarktung und Strompreise

- 🛡️ **Sicherheit:** Notstromreserve mit lokalem Veto.
- ⚙️ **Regelung:** EEG-weich und Negativpreis-hart.

## [5.3.1g] – 2026-07-10

### 🔋 Storage Manager

- 🔎 **Diagnose:** Status- und Verlauf-Schreiblast gedämpft.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Veraltet- und Messwertsprünge-Messwerte entwertet.
- ⚙️ **Regelung:** Langsame Budgets gefiltert, Fast-Netz roh.
- 🧱 **Stabilität:** Wallbox-Status und MQTT-Verbindung werden robuster aktualisiert.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Quellerholung respektiert laufende PV-Peaks.

### 📈 Direktvermarktung und Strompreise

- ⚙️ **Regelung:** Eco-Profil ohne Batterieverkauf.
- ✨ **Verbesserung:** Zusatz-WR-Schütz geschützt.
- ✨ **Verbesserung:** Zusatz-WR im Energiefluss.

## [5.3.1f] – 2026-07-09

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Phase 7 (produktive Umschaltung) vorbereitet & aktiviert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Phase H6 produktiv geschaltet.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Phase D7 & D8 implementiert.
- ✨ **Verbesserung:** Shelly-Zusatzwechselrichter-Aktor.

## [5.3.1e] – 2026-07-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Falsche Protection-Klassifikation verhindert.

### 📈 Direktvermarktung und Strompreise

- 🛡️ **Sicherheit:** Keine neue Regelwirkung.

## [5.3.1d] – 2026-07-08

### 🔋 Storage Manager

- 🔎 **Diagnose:** Speicher-Entscheider als klar getrennte Diagnosezustände sichtbar.
- 🛡️ **Sicherheit:** Keine neue Regelwirkung.

## [5.3.1c] – 2026-07-08

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Fahrzeug-Ladeende-Vertrag nutzt konfigurierten Mindeststrom.

## [5.3.1b] – 2026-07-08

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Ladevorgang-Verträge weiter extrahiert.
- 🛡️ **Sicherheit:** Stopp- und Ladeende-Semantik klarer.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Marktwert Solar bleibt sichtbar.
- ✨ **Verbesserung:** Statusgründe verständlicher.

## [5.3.1a] – 2026-07-07

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro übernimmt den Wechsel zurück zur PV-geführten Ladung zuverlässig.
- 🛡️ **Sicherheit:** Keine Zusatzbefehle beim ruhigen Moduswechsel.
- 🐛 **Fehlerbehebung:** Observe-only bleibt treiberlos.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Kanonische EMS-Entscheidungsfläche.

## [5.3.1] – 2026-07-07

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Sicherer Ladestart mit CP-Aufwecken.
- 🛡️ **Sicherheit:** Phasenwechsel strikt stromlos.
- 🐛 **Fehlerbehebung:** Ladeende und EMS-Stopp getrennt.
- ✨ **Verbesserung:** Ein Geräteanbindung-Ausgang bleibt erhalten.
- 🐛 **Fehlerbehebung:** PV-Kurvenstart nutzt reale freie PV-Leistung.
- 🧱 **Stabilität:** Export-Senke bei weicher Speicherreserve.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Marktplan unterdrückt veraltete Low-Price-Holds.
- 🐛 **Fehlerbehebung:** PV-Speicherfenster werden energie-budgetiert.
- 🔎 **Diagnose:** Marktwert Solar Monitor.

## [5.3.0i] – 2026-07-07

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Frühes Markt-Exportaufnehmen nutzt Automatik-Ladelimit statt Netz-Befehl.
- 🧱 **Stabilität:** Fälliges Netzladen bleibt Vollleistung.
- 🔎 **Diagnose:** Markt-Absorb-Kante sichtbar.

## [5.3.0h] – 2026-07-07

### 🔋 Storage Manager

- 🧱 **Stabilität:** Markt-Netzladen absorbiert Echtzeit-Export bedarfsgerecht.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** PV-Speichern holt Kurvenrückstand in Abregel-/Negativpreisfenstern auf.
- 🔎 **Diagnose:** Nachholregelung transparent sichtbar.

## [5.3.0g] – 2026-07-07

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Octopus-Late-Fill bleibt im gültigen Billing-Fenster.
- 🐛 **Fehlerbehebung:** Aktiver Marktvertrag übersteuert Echtzeit-Export-Wartezustand.
- 🔎 **Diagnose:** Marktplan im Echtzeit-Status und kompakter Diagnoseansicht sichtbar.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro bleibt während Phase-Wait startfähig.
- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeug Ladung ended löst bei SoC unter Ziel.
- 🐛 **Fehlerbehebung:** 2p-Fahrzeugprofile werden unterstützt.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Direktvermarktung nutzt separates Marktpreis-Detailansicht.
- 🧱 **Stabilität:** Negative Speicherreserve-Holds schließen neutrale Lücken.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** EEG-Einspeisevergütung im Tagesergebnis.

## [5.3.0f] – 2026-07-07

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Installationskonto-Ermittlung für abweichende Systemnutzer.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wetterwarnungs-Statusanzeige auf Mobilgeräten antippbar.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Der Installer unterstützt abweichende Installationsverzeichnisse zuverlässig.

## [5.3.0e] – 2026-07-06

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Installationskonto liest geschützte Konfiguration zuverlässig.
- 🧱 **Stabilität:** Alte Dienst-Templates nutzen Webserverkonto.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** PV-Tagesertrag mit externem AC-Zusatzwechselrichter.
- 🐛 **Fehlerbehebung:** DV-SoC-Vorschau slotgenau.

## [5.3.0d] – 2026-07-06

### 🔋 Storage Manager

- 🧱 **Stabilität:** RSCP-Zugangsdaten bleiben Pflichtwerte.
- 🐛 **Fehlerbehebung:** EEG-/PV-Speicher-Schwelle schlägt Ökobewertung.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Fahrzeug-Fehlertexte vor eingeschleusten Webinhalten geschützt gerendert.



### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** Externe E3DC-/Luox-Abregelung bleibt führend.
- 🧱 **Stabilität:** Weiche DV-Entscheider-Wechsel entprellt.
- 🔎 **Diagnose:** DV-Preisdomäne und Abregelkontext sichtbar.
- ✨ **Verbesserung:** DV-Vorschau im hellen Modus nachgeschärft.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Shelly EM Mini Gen4 für Klimaanlagen robuster gelesen.
- 🐛 **Fehlerbehebung:** openWB-Pro-Phasenanzeige nach Stop beruhigt.
- 🔎 **Diagnose:** Späte RSCP-Powermeter-Indizes erkannt.

## [5.3.0c] – 2026-07-06

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Netz-PM-Delta entprellt.
- 🔎 **Diagnose:** Diagnosegrund bleibt sichtbar.
- ✨ **Verbesserung:** Neue Einstellungen verbessern die Bewertung schneller Änderungen der Netzleistung.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** Speicherreserve vor Billigpreis-/PV-Speicherfenstern.
- 🐛 **Fehlerbehebung:** Verkauf und PV-Speichern teilen eine Planquelle.
- ✨ **Verbesserung:** Direktvermarktungsvorschau kontrastreicher.

## [5.3.0b] – 2026-07-05

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Updateprüfungen laufen nur noch über den geschützter Installationsweg.
- 🧱 **Stabilität:** Footer ermittelt die Version ohne Git-Aufruf aus dem Webserver.
- 🔎 **Diagnose:** Das Installationszentrum zeigt die installierte Produktversion ohne zusätzliche Systemaufrufe an.

## [5.3.0a] – 2026-07-05

### 🔋 Storage Manager

- 🔎 **Diagnose:** RSCP-Netzwerkausfälle sind keine Zero-Steuerung-Messwerte mehr.
- 🔎 **Diagnose:** Speicherreserve-Plateau-Grund sichtbar.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Gespeicherte Fahrzeugprofile bleiben im Dropdown erhalten.
- 🐛 **Fehlerbehebung:** Fahrzeugseite verliert Custom-Profil nicht bei abgesteckter openWB.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** DV-PV-Speichern hält EMS-Laderahmen an der 300-W-Kante.
- ✨ **Verbesserung:** DV-Verkauf wird getrennt von normaler Speicherentladung gezeigt.
- 🧱 **Stabilität:** Speicherreserve-Discharge stoppt am Zielplateau.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** BOM-Prüfung scannt keine alten Sicherungen mehr.
- 🧱 **Stabilität:** Sicherungen-Retention erweitert.
- 🐛 **Fehlerbehebung:** Schwere Archivkopien werden nicht mehr dupliziert.

## [5.3.0] – 2026-07-05

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** openWB Pro Startkante gehärtet.
- 🐛 **Fehlerbehebung:** Mobilansicht Rücksprünge aus der Installationszentrale.
- ✨ **Verbesserung:** Direktvermarktung im Konfigurationsseite erweitert.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Regelwirksame Messwertglitches sichtbarer.
- ✨ **Verbesserung:** Betreiberwarnungen und Tarifkontext erweitert.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Prognosebasiertes Eco+-PV-Speichern.
- 🛡️ **Sicherheit:** Externe Einspeisebegrenzung bleibt führend.
- ✨ **Verbesserung:** Negativpreis-Speicherreserve.
- 🧱 **Stabilität:** Exportlimit-0-Aufnahme beruhigt.
- 🔎 **Diagnose:** Zusatz-WR und DC-only sichtbar.

## [5.2.8k] – 2026-07-04

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Prognose-Marktregelung lädt oder hält den Speicher nicht mehr automatisch.
- 🛡️ **Sicherheit:** PV-autark zuerst für den normalen Marktregelung.
- 🛡️ **Sicherheit:** Echtzeit-PV hat Vorrang vor normalem Markt-Netzladen.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Markt-Wallboxfreigabe bleibt explizit.

### 🔗 Schnittstellen und Smart Home

- 🧱 **Stabilität:** Shelly-Zustand über Docker-Recreate synchronisiert.

## [5.2.8j] – 2026-07-03

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Die Regelruhediagnose kennzeichnet Messwertsprünge, die tatsächlich eine Regelentscheidung beeinflussen.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** Kurze Messwertsprünge unterbrechen das Speichern von PV-Energie für die Direktvermarktung nicht mehr; eine Hysterese beruhigt die Entscheidung.

## [5.2.8i] – 2026-07-03

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Octopus-Heat-Netzladen nutzt günstigste Abrechnungsfenster.
- 🧱 **Stabilität:** Aktive Entladebesitzer über kurze Echtzeit-Messwertsprünge gehalten.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Forum-kompaktes Diagnosepaket.
- 🔎 **Diagnose:** Analysepakete bleiben vollständig.

## [5.2.8h] – 2026-07-03

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Marktregelung bleibt bei guter Prognose neutral.
- 🐛 **Fehlerbehebung:** Markt-Automatik-Freigabe ist wieder einmalig.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Installationszentrale aus dem Konfigurationsseite erreichbar.

## [5.2.8g] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Markt-Netzladen-Kandidaten in der Prognose sichtbar.

## [5.2.8f] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Octopus-Heat-Marktregelung wartet auf echte Abrechnungspreise.
- 🐛 **Fehlerbehebung:** Zukünftige Markt-Netzladefenster pro Zeitfenster bewertet.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Messwertsprünge-Situationen maschinenlesbar auswertbar.
- 🔎 **Diagnose:** Betriebsdaten/GZ-Rohlogs bleiben auswertetauglich.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Verkaufsfenster werden bis zum nächsten Nachladefenster budgetiert.
- 🧱 **Stabilität:** DV-PV-Speichern resynchronisiert veraltet Mini-Caps.
- 🔎 **Diagnose:** PV-Speicher-Resync sichtbar.

## [5.2.8e] – 2026-07-02

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Markt-Netzladen respektiert Reserve und Zielkurve.
- 🧱 **Stabilität:** Wallbox-Entladeschutz bleibt bei Echtzeit-Messwertsprünge ruhig.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Webauthentifizierung-Ausnahme geschlossen.
- 🛡️ **Sicherheit:** PIN-Prüfung gehärtet.
- 🛡️ **Sicherheit:** Kommandozeile-Jobs im Webverzeichnis gegen HTTP-Aufruf geschützt.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** Watchdog-Installationsassistent validiert Router-IPs.
- 🛡️ **Sicherheit:** Installationsassistent-Paketcheck quote-sicherer.

## [5.2.8d] – 2026-07-02

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Shelly-SG-Ready akzeptiert gemeinsamen Leistungsanhebung-Aufruf.
- 🔎 **Diagnose:** Leistungsmessung sauber eingeordnet.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** DV-PV-Speichern beruhigt.
- 🐛 **Fehlerbehebung:** DV-PV-Speichern hart auf PV-Überschuss gedeckelt.
- 🔎 **Diagnose:** Externe AC-Zusatzwechselrichter getrennt.
- 🛡️ **Sicherheit:** Preisqualität als harter Rückfall.

## [5.2.8c] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Notstrom-/Rückfallreserve als Start-SoC.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Hauptsteuerung observe-only schreibt keine fiktive Start-Historie.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Diagnose-ZIP maschinenlesbarer.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Speicher-Regelung-Leistungsbudget startet SG-Ready-PV-Leistungsanhebung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** PV-Überschuss in niedrigen Direktvermarktungsfenstern speichern.
- 🧱 **Stabilität:** Batterieeinspeisung und Netzladen bleiben getrennte Freigaben.
- 🔎 **Diagnose:** Eigenes Direktvermarktungs-Diagnosepaket.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** SoC-Prognose-Kopfzeile mobil lesbarer.
- ✨ **Verbesserung:** Installationszentrale kehrt mobil zurück.

## [5.2.8b] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Veröffentlichte Ladekurve wandert nicht nach unten.
- 🔎 **Diagnose:** Untergrenze-Clamps sichtbar.

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** Fahrzeug nimmt weniger als angeboten.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** EWMA-/Messwertsprünge-Diagnose im Diagnose-ZIP.
- 🔎 **Diagnose:** Power-Decision-Stability in Regelung-Historien.
- 🧱 **Stabilität:** EWMA bleibt diagnostisch.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** Docker-Installation prüfbarer.

## [5.2.8a] – 2026-07-01

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Geschützte Preis-/Netzladung entlädt den Akku nicht in das Fahrzeug.
- 🔎 **Diagnose:** Wallbox-Speicher-Audit.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** ENTSO-E-Daten werden revisionsfest normalisiert.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Shelly 1 SG-Ready ohne direkt angebunden Wärmepumpe.
- 🧱 **Stabilität:** Taktschutz gilt auch für reines SG-Ready.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** ENTSO-E als 15-Minuten-Rückfall.

## [5.2.8] – 2026-07-01

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Geplantes Netzladen unter Wallbox-Mindest-SoC hält den Speichervertrag.
- 🧱 **Stabilität:** Einzelne unplausible Echtzeitmesswerte reißen den Vertrag nicht auf.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** PV und PV+Akku bleiben openWB-geführt.
- 🐛 **Fehlerbehebung:** Simple Schnittstelle-Status wird korrekt normalisiert.
- 🔎 **Diagnose:** Warnung bei 1-phasigem Netzladen.
- 🔎 **Diagnose:** Wallbox-Details tragen Hauptsteuerung-Warnfelder.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Diagnosearchive nennen zuverlässig die installierte Produktversion.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Tibber liefert jetzt 15-Minuten-Zeitfenster.
- ✨ **Verbesserung:** Tibber-Verbindungsprüfung zeigt die echte Auflösung.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** HA-Kommandos ohne Shell-Interpolation.
- 🛡️ **Sicherheit:** Docker-Installation prüfbarer.
- 🛡️ **Sicherheit:** Der Zugriff auf lokale ML-Modelle und Prognosedaten wurde abgesichert.
- 🛡️ **Sicherheit:** Force-Discharge bricht ohne konfigurierte Zugangsdaten ab.

## [5.2.7d] – 2026-07-01

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Preisfenster laden mit vollem Netzbudget.
- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC-Preisfenster schont den Speicher.
- 🐛 **Fehlerbehebung:** openWB/openWB Pro stoppt bei Wallbox-Mindest-SoC-Untergrenze und Nullbudget härter.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfigurationsseite erweitert.
- 🔎 **Diagnose:** EWMA-/Deadband-Export für Entscheidungswerte.
- 🔎 **Diagnose:** Plausibilitäts-Forensik.
- 🛡️ **Sicherheit:** Offene Web-Endpunkte geschützt.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Tibber als echte Tarifquelle.



### 🔗 Schnittstellen und Smart Home

- 🛡️ **Sicherheit:** Matter-Neustart über Dienst-geschützter Aufruf.

## [5.2.7c] – 2026-06-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Geplante Netz-/Preisstarts lösen alte Stop-Sperrzustände.
- 🧱 **Stabilität:** Schutz gegen Flattern bleibt aktiv.

## [5.2.7b] – 2026-06-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro startet bei ausreichendem Leistungsbudget direkt dreiphasig.
- 🧱 **Stabilität:** Geräteanbindung bleibt reiner openWB-Adapter.
- 🐛 **Fehlerbehebung:** Befehl-Schutz lässt legitime openWB-Pro-Startbefehle durch.
- ✨ **Verbesserung:** openWB Pro lädt real.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Keine leeren Preise bei veraltet SMARD-Daten.
- 🐛 **Fehlerbehebung:** Statische Tarife blockieren Sofort bis Preislimit nicht mehr.
- 🐛 **Fehlerbehebung:** EMS-Netzleistung bleibt gültig, wenn PM-Phasen fehlen.
- ✨ **Verbesserung:** Regelruhe bleibt grün.

## [5.2.7a] – 2026-06-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Harte Stops greifen trotz idle Ladevorgang bei echter Ladung.
- 🐛 **Fehlerbehebung:** Keine direkt angebunden 6A-Batterieladung unter Wallbox-Mindest-SoC-Untergrenze.
- 🧱 **Stabilität:** Batterie-Drain-Nullbudget ist ein E3DC-Hardstop.
- 📊 **Anzeige/Auswertung:** Stop-Anzeige läuft nicht 20 Sekunden nach.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** PV-Freigabe bleibt ruhig.

## [5.2.7] – 2026-06-29

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Frühere manuelle Batterievorgaben bleiben nach ihrem Ende nicht fälschlich in der Anzeige stehen.
- 🐛 **Fehlerbehebung:** Das morgendliche Ladeziel blockiert am Abend keine noch verfügbare PV-Energie.
- 📊 **Anzeige/Auswertung:** Bei vollständig erwarteter PV-Deckung zeigt die Prognose nur wirksame Kurveneinstellungen; interne Vergleichseinstellungen bleiben außerhalb der normalen Bedienung.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB bleibt bei PV- und PV-plus-Akku-Führung zuverlässig in der gewählten Betriebsart.
- 🧱 **Stabilität:** Ohne unterstützte Phasenumschaltung entstehen keine wiederkehrenden Wechsel zwischen Mindeststrom und Stopp.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wetterwarnungen verwenden passende Symbole; die Kopfzeile und der Mobilansicht Energiering wurden übersichtlicher.

## [5.2.6h] – 2026-06-29

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Pro-Phasenwechsel nach 1p-Rückschaltung beruhigt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Unvollständige Mini-Tage ziehen die Hausprognose nicht mehr herunter.
- 🧱 **Stabilität:** Plausibilitätsprüfung-Obergrenze bleibt konservativ, aber realistisch.
- 🐛 **Fehlerbehebung:** Ladekurve springt nicht mehr über Tagesgrenzen.
- ✨ **Verbesserung:** Ladekurve und voraussichtliche Ladung getrennt.

## [5.2.6g] – 2026-06-28

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** ML-Verbrauchsprognose nach Reboot wiederhergestellt.
- 🐛 **Fehlerbehebung:** Keine dauerhaften 500W/300W-Rückfall bei vorhandenem Modell.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Datum im Hybrid-Hinweistexte.
- 🔎 **Diagnose:** Tageswechsel sichtbar.

## [5.2.6f] – 2026-06-28

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicherreserve-Entladung nur bei fehlendem Speicherplatz.
- 🔎 **Diagnose:** Speicherreserve-Druck sichtbar.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Klima als eigener Prognose-Verbraucher.
- 🐛 **Fehlerbehebung:** Hausverbrauch-Kopfzeile nutzt bereinigten Speicherplan.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Klima in der Prognose-Kopfzeile.
- 🔎 **Diagnose:** In Echtzeit- und Prognose-Artefakte erweitert.

## [5.2.6e] – 2026-06-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Optionaler Kurven-Feinregelung.
- ✨ **Verbesserung:** Freies E3DC-Automatik bleibt frei.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Hauptsteuerung nutzt erkannte Ladepunktnummer.
- 🔎 **Diagnose:** Ladepunktquelle sichtbar.

## [5.2.6d] – 2026-06-28

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Halteentscheidungen zentralisiert.
- 🧱 **Stabilität:** Strom reduzieren vor Stop.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Keine Ansicht-Flackerei mehr.
- ✨ **Verbesserung:** Hintergrund-Tabs pollen schonender.

## [5.2.6c] – 2026-06-28

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicherreserve-Reserve führt laufenden Kurvenrahmen weiter.
- 🧱 **Stabilität:** E3DC-Automatik bleibt frei entladbar.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Start- und Phasenwechsel-Fenster zentral gehalten.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Moderne Akku-SoC-Anzeige beruhigt.
- ✨ **Verbesserung:** Akku-SoC direkt am Batteriesymbol.

## [5.2.6b] – 2026-06-27

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Octopus-Heat-Fenster bleiben sauber begrenzt.
- 🧱 **Stabilität:** Just-in-Time bleibt erhalten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Akku-SoC direkt am Batteriesymbol.
- 🐛 **Fehlerbehebung:** Klassische Kompaktansicht wieder konsistent.

## [5.2.6a] – 2026-06-27

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Manuelles Batterieladen bleibt führend.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Moderne Weboberfläche-Kompaktansicht bleibt auf moderne Optik begrenzt.
- 🐛 **Fehlerbehebung:** Weboberfläche-Auswahl wird zuverlässig gespeichert.
- 🔄 **Migration/Kompatibilität:** Rückfallkette aktualisiert.

## [5.2.6] – 2026-06-27

### 🔋 Storage Manager

- 🧱 **Stabilität:** Der optionale gleitende Prognosehorizont wurde für wechselnde Tagesverläufe stabilisiert.
- 🐛 **Fehlerbehebung:** Günstige Preisfenster werden ruhiger genutzt; die Notstromreserve verhindert dabei keine fachlich zulässige Netzladung.
- 🛡️ **Sicherheit:** Ungültige Echtzeitmesswerte lösen keinen aktiven Eingriff aus.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** openWB und openWB Pro setzen Stromänderungen sowie Phasenübergänge kontrollierter um.
- 🔄 **Migration/Kompatibilität:** Die Rückfallkette wurde auf den freigegebenen Vorgängerstand aktualisiert.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Prognose, Marktladung und die gemeinsame Budgetierung steuerbarer Verbraucher wurden weiter zusammengeführt.
- ✨ **Verbesserung:** Die selbstlernende Prognose berücksichtigt neue Messwerte, ohne bei fehlenden Daten unsichere Stellbefehle auszugeben.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Kompakt-, Normal- und Detailansicht wurden vereinheitlicht; Klimadaten können als eigener Verbraucher angezeigt werden.
- 🛡️ **Sicherheit:** Eine vorbereitete Klimaanbindung bleibt ohne aktive Schaltbefehle.

## [5.2.5a] – 2026-06-25

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kein falsches Automatik 0 W bei Unterkurven-Rückstand.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** RSCP-Messwertsätze bekommen Plausibilitätsflags statt versteckter 0-W-Werte.
- 🧱 **Stabilität:** Speicher und Wallbox nutzen nur gültige Leistungsframes aktiv.
- 🐛 **Fehlerbehebung:** Wallbox-Stromschritte bleiben treiberscharf.
- 🧱 **Stabilität:** 1p-Pflicht wird plausibilisiert.
- 🔎 **Diagnose:** Veralteter Fahrzeug-SoC wird sichtbar.
- 🔄 **Migration/Kompatibilität:** Stable-Rückfall bleibt v5.2.5.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche bleibt aktuell, Historie bleibt sauber.

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Diagnose-ZIPs werden stärker komprimiert.

## [5.2.5] – 2026-06-24

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Fehlende Preise sind kein günstiges Netzladefenster.
- 🐛 **Fehlerbehebung:** Netzladen braucht freigegebene Verbraucher.
- 🐛 **Fehlerbehebung:** Speicher-Netzladen endet am echten Bedarf.
- 🔎 **Diagnose:** Marktregelung-Schwelle sichtbar.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Wallbox-Mindest-SoC verhält sich wie PV-Kurve ruhig, bis die Reserve offen ist.
- 🧱 **Stabilität:** Oberhalb Wallbox-Mindest-SoC darf das Auto netzneutral übernehmen.
- 🧱 **Stabilität:** Phasen-/Stop-Kanten sind gehärtet.
- 🐛 **Fehlerbehebung:** Energiebilanz plausibilisiert Direkt angebundene Wallbox-Zähler.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Regelruhe bleibt scharf auf echte Kanten.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Statische Strompreise bleiben im Diagramm durchgängig.

## [5.2.4g] – 2026-06-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Strom-Schrittweite liegt im Wallboxtreiber.
- ✨ **Verbesserung:** openWB-Schrittweite dokumentiert.
- 🐛 **Fehlerbehebung:** Direkt angebundene Wallbox-Tageszähler plausibilisiert.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Energiebilanz klar benannt.
- 🔎 **Diagnose:** Reine Kurven-Führung entwarnt.
- ✨ **Verbesserung:** Marktregelung bleibt Erprobungsstand.

## [5.2.4f] – 2026-06-23

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Wartende Wallbox-Mindest-SoC-Autos ziehen keinen Speicher-Entscheider mehr.
- 🧱 **Stabilität:** Oberhalb Wallbox-Mindest-SoC bleibt der E3DC autonom, solange die Wallbox die Leistung aufnehmen kann.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC folgt unterhalb der Reserve der PV-Kurve ruhig.
- 🐛 **Fehlerbehebung:** PV-only-Untergrenze begrenzt laufende Direkt angebundene Wallboxen auf echten PV-Überschuss.
- 🧱 **Stabilität:** 1p/3p-Übergänge unter Untergrenze-Schutz schneller abgesichert.
- ✨ **Verbesserung:** Weboberfläche zeigt die Wallbox-Untergrenze direkt im Modus-Statusanzeige.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Fahrzeug voll und SoC-Anzeige brauchen bestätigte Quellen.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Doppelte Wallbox-Logzeilen verhindert.
- 🔎 **Diagnose:** Regelruhe-Diagnose gruppiert reine Kurvenführung.
- ✨ **Verbesserung:** Betriebsdokumente, Dienstkatalog und Rechteprüfung aktualisiert.

## [5.2.4e] – 2026-06-22

### 🔋 Storage Manager

- 🧱 **Stabilität:** Kleine Automatik-Rückkehrkanten bleiben ruhig.
- 🧱 **Stabilität:** Kein Überschuss -> CHARGE braucht wieder echten Ladebedarf.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Verlorenes Speicherbudget beendet alte Wärmepumpen-Leistungsanhebung zuverlässig.
- 🔎 **Diagnose:** Defizitgrund klarer.
- ✨ **Verbesserung:** Marktregelung bleibt Erprobungsstand.

## [5.2.4d] – 2026-06-22

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Eine geschützte openWB-Anbindung kann sich optional authentifizieren, statt an einer Zugriffsverweigerung zu scheitern.
- 📊 **Anzeige/Auswertung:** Die Hauptsteuerung der Wallbox wird in der Oberfläche eindeutig benannt.

## [5.2.4c] – 2026-06-22

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Marktregelung-Verbraucher speichern wieder zuverlässig.
- 🐛 **Fehlerbehebung:** Kein unnötiger Wärmepumpen-Regelung-Neustart.
- ✨ **Verbesserung:** Marktregelung-Einstellungen erklärt.

## [5.2.4b] – 2026-06-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Zielkorridor öffnet keinen 12-kW-Speicherbedarf mehr.
- 🧱 **Stabilität:** Regeln Entscheider bleibt ruhig.
- 🐛 **Fehlerbehebung:** Wallbox übernimmt Speicher-Überladung oberhalb berechneter Ladebedarf.



### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Forumtexte lassen sich auch in Browsern mit eingeschränkter Zwischenablage kopieren.
- 📊 **Anzeige/Auswertung:** Regelruhe-Auswertung mit Zeitraum.
- ✨ **Verbesserung:** Strompreislinien mit sauberen Flanken.

## [5.2.4a] – 2026-06-21

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Marktregelung nur bei prognostiziertem Energiemangel.
- 🐛 **Fehlerbehebung:** Markt-Entladesperre öffnet keine Ladegrenze.
- 🧱 **Stabilität:** Kurvenladung folgt gemessener Unterladung gedämpft.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Manuelle Wallbox-Pause löst alte Speicher-Holds.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Marktregelung und V5-Wording sichtbarer.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Telegram-Statistik akzeptiert Oberfläche-Zahlenwerte.

## [5.2.4] – 2026-06-20

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Abendziel blockiert zu frühen Freilauf.
- 🧱 **Stabilität:** Prognose-100-Ziele stärker geführt.
- 🐛 **Fehlerbehebung:** Autonome Wallbox ersetzt nicht die Speicherkurve.
- 🐛 **Fehlerbehebung:** EMS-Power-Settings werden aus 0 W rearmt.
- ✨ **Verbesserung:** Manuelle Batterietrainings laufen länger.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Beobachten mit Speicher-Regelung.
- ✨ **Verbesserung:** Einfache und erweiterte Ansicht konsistent.
- 🛡️ **Sicherheit:** Hausakku-Reserve besser bedienbar.
- ✨ **Verbesserung:** Wording geschärft.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Octopus Heat unterstützt günstige Preisfenster.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Fehlgeschlagene Tagesstatistik gilt nicht als erledigt.
- ✨ **Verbesserung:** Netzfluss richtungsabhängig eingefärbt.
- 📊 **Anzeige/Auswertung:** Netzqualität-Anzeige umbruchfest.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Telegram-Tagesstatistik scheitert nicht mehr still.

## [5.2.3d] – 2026-06-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nach einer kurzen Freigabe an die E3DC-Automatik übernimmt die Ladekurve ihre Führung wieder unmittelbar. Dadurch entstehen keine wiederkehrenden Ladeimpulse durch eine verzögerte Rückkehr in den Haltezustand.

## [5.2.3c] – 2026-06-18

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Ladekurvenkante bleibt ruhig bei PV-/Exportumfeld.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Manuelle Pause endet beim Abstecken.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Ausgeschaltete Wärmepumpen werden heruntergelernt.
- 🐛 **Fehlerbehebung:** Alte ML-Prognosen werden nicht als aktuell angezeigt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Batterie-Detailanzeige zeigt Zähler-/Bilanzdifferenzen.

## [5.2.3b] – 2026-06-18

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** E3DC-Wallbox-Untergrenze wird wirksam respektiert.
- 🔎 **Diagnose:** Untergrenze sichtbar und konsistent.
- 🐛 **Fehlerbehebung:** Moduswechsel beendet manuelle Pause.
- 🐛 **Fehlerbehebung:** Direkt an E3DC angebunden PV-Kurve nach Pause-Freigabe entprellt.
- 🧱 **Stabilität:** Mindeststrom-Stop nutzt ein Import-Integral.
- 🐛 **Fehlerbehebung:** Pause-Anzeige überall konsistent.
- ✨ **Verbesserung:** Energiequelle und Ladeabsicht klar getrennt.
- ⚙️ **Regelung:** Statuszeile ohne versteckte Planung.
- ✨ **Verbesserung:** Minimalistische Wallbox-Ansicht.
- 🛡️ **Sicherheit:** Beobachten bleibt Observe-only.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Beobachtete Wärmepumpenläufe sind keine Schaltbefehle.

## [5.2.3a] – 2026-06-17

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** PV- und Akkuladen berücksichtigt wieder die gewählte Hausakku-Reserve.
- 🐛 **Fehlerbehebung:** Keine neue direkte Startfreigabe direkt nach Stop.
- ✨ **Verbesserung:** Betriebsart, Ladeplan und globale Grenzwerte getrennt.
- ⚙️ **Regelung:** Statuszeile ohne versteckte Planung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Sichtbare Texte verwenden deutsche Umlaute.

## [5.2.3] – 2026-06-17

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Minimalistische Wallbox-Ansicht.
- ✨ **Verbesserung:** SoC- oder kWh-Ziel.
- ✨ **Verbesserung:** Laiengerechtes Wording.
- 🛡️ **Sicherheit:** Regelung aus bleibt Observe-only.



### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Quell-Erholung taktet nicht mehr bei kurzen Request-Lücken.
- 🔎 **Diagnose:** Haltezustände-Zustand sichtbar.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Octopus Heat behält den normalen Arbeitspreis.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Einfache und erweiterte Ansichten.
- ✨ **Verbesserung:** Konfigurationsseite aufgeräumt.
- ✨ **Verbesserung:** Installationszentrale direkt öffnen.
- ✨ **Verbesserung:** Ansichten dokumentiert.

## [5.2.2d] – 2026-06-16

### 🔋 Storage Manager

- 🧱 **Stabilität:** Reale Wallboxladung behält den Speicher-Entscheider.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro startet nicht mehr aus Scheinüberschuss.
- 🛡️ **Sicherheit:** Direktvermarktung blockiert den Direktsteuerung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Regelruhe-Zeitleiste ohne Überlagerung.
- 🐛 **Fehlerbehebung:** Wärmepumpen-Detailanzeige erklärt Zählerdifferenzen.

## [5.2.2c] – 2026-06-16

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** V4-Bereinigung behält gültige Wärmepumpenfelder.

## [5.2.2b] – 2026-06-16

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicher-Entscheider-Gerangel beseitigt.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** PV-Kurve ruhig schützt das Abendziel.
- 🧱 **Stabilität:** Mindestleistungs-Kanten nutzbar.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Entscheider-/Zustand-Pendeln erkannt.
- 🔎 **Diagnose:** Keine falschen Kurven-Alarme.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Wetterbadge fachlich korrigiert.
- ✨ **Verbesserung:** Mobilansicht Tagesstatistik erweitert.

## [5.2.2a] – 2026-06-16

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Speicherreserve-Begriffe entschärft.
- ✨ **Verbesserung:** Eigener Schalter für Speicherreserve-Entladung.
- 🔎 **Diagnose:** Sperrgründe und Tagesbudget sichtbar.
- 🔎 **Diagnose:** PV-Prognose plausibilisiert.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Kurvenwerte logisch sortiert.
- ✨ **Verbesserung:** Mobilansicht Konfiguration einheitlich.

## [5.2.2] – 2026-06-15

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Nur-Lese-Vergleichsmodus wird beim Speichern erkannt.
- 🧱 **Stabilität:** Dienste werden auch aktiviert, gestartet oder neu gestartet.
- 🛡️ **Sicherheit:** Keine direkten direkter Dienstaufruf-Sonderwege im Konfigurationsseite.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Dienstverwaltung im Web-Installationsassistent.

## [5.2.1h] – 2026-06-15

### 🔋 Storage Manager

- 🧱 **Stabilität:** Kleine und große Speicher werden mit passenden Reserve- und Leistungsgrenzen behandelt.
- 🐛 **Fehlerbehebung:** Eine vollständige PV-Prognose führt im Zielkorridor nicht mehr zu unnötigem Nachladen.
- 🔎 **Diagnose:** Gründe für Reserve-, Lade- und Halteentscheidungen sind besser nachvollziehbar.

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** openWB Pro, bidirektionale Wallboxen sowie Abfahrts- und Pendelenergie werden in der gemeinsamen Fahrzeugplanung berücksichtigt.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wetterqualität, selbstlernende Prognose und die gemeinsame Planung steuerbarer Verbraucher wurden erweitert.
- ✨ **Verbesserung:** Haushaltsgeräte können als flexible Verbraucher berücksichtigt werden.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Direktvermarktung und Preisdifferenzgeschäfte werden als getrennte wirtschaftliche Anwendungsfälle dargestellt.

## [5.2.1g] – 2026-06-15

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Pendelrahmen gehärtet.
- 🧱 **Stabilität:** openWB Pro Phasenpause ruhiger.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Härtungsfelder 3-7 als Verträge sichtbar.
- 🧱 **Stabilität:** Anker bleiben erklärbar.

### 📈 Direktvermarktung und Strompreise

- 🛡️ **Sicherheit:** Direktvermarktung bleibt abgegrenzt.

## [5.2.1f] – 2026-06-15

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Historischer Abregel-Speicherreserve.
- 🐛 **Fehlerbehebung:** Kaltwind-/Temperaturfaktor.
- 🐛 **Fehlerbehebung:** Komfortboden bleibt Speicherreserve-begrenzt.
- 🐛 **Fehlerbehebung:** Manuelle Lade-/Entladeziele werden sauber quittiert.
- 🛡️ **Sicherheit:** Manuelle SoC-Anker bleiben prognosebegrenzt.
- 🐛 **Fehlerbehebung:** Keine wandernden manuellen Anker.

## [5.2.1e] – 2026-06-14

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität nutzt Verbraucher gezielter.
- 🐛 **Fehlerbehebung:** Prognose 100% folgt wieder der Ladekurve.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Zentraler Multi-Wallbox-Allokationsvertrag.
- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC gilt auch bei Direkt angebundene Wallbox ruhiger.
- 🐛 **Fehlerbehebung:** 1p/3p-Startkanten gehärtet.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Tagesstatistik zeigt kWh-Retter.

## [5.2.1d] – 2026-06-13

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Normale openWB bekommt einen Nebensteuerung-Ladevorgang-Vertrag.
- 🧱 **Stabilität:** go-e bekommt denselben Stufe-Zustand-Vertrag.
- 🔎 **Diagnose:** HTTP-/Stufe-Zustand-Wallboxen sind sichtbar.
- 🐛 **Fehlerbehebung:** Phantom-Entscheider vermeiden.

## [5.2.1c] – 2026-06-13

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** openWB Pro bekommt einen expliziten Ladevorgang-Vertrag.
- 🐛 **Fehlerbehebung:** Angebotene Ampere zählen nicht als echte Ladung.
- 🔎 **Diagnose:** openWB-Pro-Zustände erreichen Weboberfläche-Details.

## [5.2.1b] – 2026-06-13

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** E3DC-Automatik bleibt bei aktiver geregelter Wallbox frei.
- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC-/Zielmodus pendelt nicht mehr gegen die Ladekurve.
- 🐛 **Fehlerbehebung:** Direkt an E3DC angebunden-PV-Starts übernehmen Ladevorgang-Entscheider sauber.
- 🧱 **Stabilität:** PV-Kurve für alle Wallboxen ruhiger.
- 🐛 **Fehlerbehebung:** Feste 3-phasige Direkt angebundene Wallboxen werden korrekt budgetiert.

## [5.2.1a] – 2026-06-13

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Geplante Ladefenster starten wieder aktiv.
- 🐛 **Fehlerbehebung:** Alter Stoppsperre blockiert keinen neuen Planstart.

## [5.2.1] – 2026-06-12

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Gehärteter Produktionsvertrag.
- 🐛 **Fehlerbehebung:** Kein zyklischer harter Stop ohne Grund.
- 🐛 **Fehlerbehebung:** Kein Startimpuls ohne physikalisches Leistungsbudget.
- 🐛 **Fehlerbehebung:** Ladeende wird exakt geführt.
- 🐛 **Fehlerbehebung:** Phantomladen nach Ladeende entfernt.
- 🔎 **Diagnose:** RSCP-Fehler sichtbar.
- ✨ **Verbesserung:** Direkt angebunden Tagesenergie exakt gezählt.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Wallbox-Betriebsdaten schreibt race-frei.
- 🐛 **Fehlerbehebung:** Wärmepumpenanzeige bleibt bei ausgeschaltetem Heizstab sichtbar.

### 📚 Dokumentation

- ✨ **Verbesserung:** Direkt angebunden-Wallbox-Vertrag dokumentiert.

## [5.2.0] – 2026-06-11

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** E3DC-Multi-Wallboxen werden robuster erkannt.
- 🐛 **Fehlerbehebung:** Phantomladen wird härter abgefangen.
- 🧱 **Stabilität:** Direkt angebundene Wallboxen halten ruhiger.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Langzeitbilanz neu strukturiert.
- ✨ **Verbesserung:** Konfiguration Download/Upload integriert.
- 🐛 **Fehlerbehebung:** Stufenpreise und Tageswechsel.
- 🔄 **Migration/Kompatibilität:** Dienste, Rechte und Aktualisierung geprüft.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Eigener Direktvermarktungszweig.
- ✨ **Verbesserung:** Wirtschaftlichkeitsprüfung sichtbar.
- ✨ **Verbesserung:** EEG-/Marktprämienvergleich.
- 🛡️ **Sicherheit:** Netzstrom-Arbitrage bleibt bewusst experimentell.
- ✨ **Verbesserung:** Direktvermarktungs-Vorschau.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Shelly-/Wärmepumpenmonitoring.

## [5.1.8j] – 2026-06-10

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Fehlende E3DC-Peakleistung ist nicht mehr 0 k Wärmepumpe.
- 🔎 **Diagnose:** Prognose kann Ursache sauber unterscheiden.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** V4-Bereinigung entfernt lokale lokale Installationsangaben nicht mehr.

## [5.1.8i] – 2026-06-10

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Ladekurve lässt den Speicher wieder laden.
- 🐛 **Fehlerbehebung:** Dachflächen akzeptieren deutsche Dezimalkommas.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** E3DC-0-k Wärmepumpe blockiert den Prognose nicht mehr.

## [5.1.8h] – 2026-06-09

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfiguration Download/Upload.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Pro 3 em-Messung läuft auch ohne Fahrzeugsteuerung.
- 🐛 **Fehlerbehebung:** Relais bleibt bei Nur-Messen unangetastet.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** V4-Rollback verdrahtet.

## [5.1.8g] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Eindeutiger Nur-Lese-Vergleichsmodus-Statusanzeige in der Kopfzeile.
- 🐛 **Fehlerbehebung:** Entscheidungs-Statusanzeige nutzt Nur-Lese-Vergleichsmodus-Datenstände.

## [5.1.8f] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Nur-Lese-Vergleichsmodus-Livequelle sichtbar.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Nur-Lese-Vergleichsmodus-Weboberfläche liest Hauptsystem-Datenstände.

## [5.1.8e] – 2026-06-08

### 🔋 Storage Manager

- ✨ **Verbesserung:** Optionaler Zielkurven-Modus Prognose 100.
- ✨ **Verbesserung:** Konfigurationsseite-Auswahl ergänzt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Voreinstellungen des Nur-Lese-Vergleichsmodus werden zuverlässig gespeichert.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Passive Stiebel-Kühlung wird nicht mehr als Standby angezeigt.

## [5.1.8d] – 2026-06-08

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Livewerte nutzen dieselbe Normalisierung.
- 🧱 **Stabilität:** Wallbox-Leerlauf-Wiederholungsverzögerung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Edge-Datenstände für in Echtzeit- und Speicherplandaten.
- 🛡️ **Sicherheit:** Zentrale Speicherentscheidung bleibt führend.
- 🔎 **Diagnose:** Kurzer Echtzeit-Status-Datenstände-Zwischenspeicher.
- 🔎 **Diagnose:** Dienst-Lastprofile und Diagnose-Datenstände.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Wiederholungsverzögerung für Hintergrundtabs.

## [5.1.8c] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Nur-Lese-Vergleichsmodus-Client als echte Nur-Lese Vergleichsinstanz vorbereitet.
- 🛡️ **Sicherheit:** Kein Failover, keine Hardwarebefehle.



### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Webdaten-Verzeichnis gehärtet.

### 📚 Dokumentation

- ✨ **Verbesserung:** Anleitung für Betriebsumgebungen ergänzt.

## [5.1.8b] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Hausverbrauch nicht aus asynchroner Bilanz hochziehen.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** WPM-M3.21-Register sauber angebunden.
- 🐛 **Fehlerbehebung:** Keine Phantom-Kältespeicherwerte.
- ✨ **Verbesserung:** Dimplex-Leistung ehrlich markiert.
- ✨ **Verbesserung:** Dimplex-Register dokumentiert.
- 🐛 **Fehlerbehebung:** Mitteltemperatur wird nicht mehr erfunden.
- 🔄 **Migration/Kompatibilität:** Saison-Rückfall bleibt intern erlaubt.

### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Uwe-Fall abgesichert.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Mindestanforderung Speicherplatz angehoben.
- ✨ **Verbesserung:** Administrative Installationsaufgaben benötigen keine direkte Anmeldung als Systemverwalter mehr.

## [5.1.8a] – 2026-06-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Zwischenziele bleiben harte Anker.
- 🐛 **Fehlerbehebung:** UTC-Hosts verschieben keine Uhrzeiten mehr.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Prognose-Kopfleiste bleibt nicht bei 0.0 kWh hängen.
- 🐛 **Fehlerbehebung:** Netzphasen bei Wurzelzähler-Seriennummern.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Standby statt irreführendem WW-/Kühlbetrieb.
- 🐛 **Fehlerbehebung:** WW-Leistungsquelle gewinnt gegen Kühlanforderung.
- 🐛 **Fehlerbehebung:** Mehr Heizkreis- und Speicherwerte im Weboberfläche.
- 🔎 **Diagnose:** Heizleistung bei Stiebel sichtbar.
- ✨ **Verbesserung:** Leistungsquelle lesbar.

## [5.1.8] – 2026-06-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die Kapazität mehrerer Batterieschränke wird als Gesamtsystem plausibel zusammengeführt.
- 🔎 **Diagnose:** Korrekturen an Kapazitätswerten bleiben nachvollziehbar.



### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Langzeit-PV-Erträge bleiben auch bei einem unvollständigen PV-Zähler nutzbar.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Die Prognoseanzeige bleibt bei vorübergehend fehlenden Strompreisdaten nutzbar.

## [5.1.7] – 2026-06-07

### 🔋 Storage Manager

- 🧱 **Stabilität:** Abregelreserve vor Online-Dienst-Edge-Spitzen.
- ✨ **Verbesserung:** Echter Abregeldruck bleibt Pflichtladung.
- 🔎 **Diagnose:** Reserve getrennt sichtbar.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Laufender Sommer-/Warmwasserbetrieb wird nicht mehr als Standby angezeigt.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Sicherungen-Limit gegen Altlasten.
- ✨ **Verbesserung:** Manuelles Sicherungen-Pruning.

## [5.1.6] – 2026-06-07

### 🔋 Storage Manager

- 🔎 **Diagnose:** Glättung bleibt sichtbar.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Schlafende Neben-Wallbox bleibt ruhig.
- ✨ **Verbesserung:** Gemeinsame Wallbox-Grundregeln.
- 🧱 **Stabilität:** Fahrzeugzuordnung pro Zeitfenster robust.

### ☀️ PV, Prognose und Energiemanagement

- 🔄 **Migration/Kompatibilität:** Prerelease-Schiene freigegeben.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Expertenmenü neu gegliedert.

## [5.1.5f] – 2026-06-06

### 🔋 Storage Manager

- 🔎 **Diagnose:** Status nennt den führenden Regelung.
- ✨ **Verbesserung:** Autonome Wallboxladung klarer.
- 🔎 **Diagnose:** Entscheider-Felder im Echtzeit-Status.
- 🐛 **Fehlerbehebung:** Ladekurve reagiert ohne Neustart auf neue Ziele.
- 🐛 **Fehlerbehebung:** Langzeit-Quellenbilanz bleibt physikalisch.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Ziel Wallbox-Mindest-SoC taktet nicht mehr hektisch.
- 🧱 **Stabilität:** Kurze Wolken werden gehalten.
- 🐛 **Fehlerbehebung:** Direkt an E3DC angebunden Hardstops entschärft.
- 🐛 **Fehlerbehebung:** Sofort bis Preislimit lässt PV weiter laden.
- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC trennt Speicherladung und Auto-Leistungsbudget.
- 🧱 **Stabilität:** Priorität ist kein Monopol mehr.
- 🔎 **Diagnose:** Ladepriorität wird in den Wallbox-Details sichtbar.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Nachtwerte werden gekappt.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Kühlbetrieb bleibt im Weboberfläche sichtbar.
- 🐛 **Fehlerbehebung:** WPM-Touch-Normalzustand 0.
- 🐛 **Fehlerbehebung:** SG-Freigabe ohne Besitzer wird zurückgesetzt.
- 🐛 **Fehlerbehebung:** Echtzeitverbindung-Daten robust gelesen.

## [5.1.5e] – 2026-06-05

### ♨️ Wärmepumpe und Wärme

- 🔎 **Diagnose:** WPM-Softwarestand wird gelesen.
- 🔎 **Diagnose:** SG-Rohwerte bleiben sichtbar.
- 🔎 **Diagnose:** Leistungsregister nachvollziehbar.
- ✨ **Verbesserung:** Optionales Feld für WPM Software.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker-Installationen bieten keine unpassende lokale Dienstinstallation mehr an.
- ✨ **Verbesserung:** Richtiger Docker-Ablauf.

## [5.1.5d] – 2026-06-05

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Keine wandernden jüngste Einträge-Hilfsanker.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC nähert sich ruhiger an.
- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeug-SoC springt nach Regelung-Neustart nicht mehr hoch.



### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Web-Update fängt Darstellung statt Betriebsdaten ab.
- 🐛 **Fehlerbehebung:** MQTT-Hub-Installationsassistent ist V4-tauglich.

## [5.1.5c] – 2026-06-05

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kurvenanker wandern nicht mehr.
- 🧱 **Stabilität:** Erreichbares Tagesziel bleibt Diagnose, nicht neuer Anker.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Phantomladung nach Stop gelöscht.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Phasenbelastung wieder sichtbar.
- 🐛 **Fehlerbehebung:** EMS-Netzleistung bleibt führend.
- ✨ **Verbesserung:** Wurzelzähler klarer erklärt.
- 🐛 **Fehlerbehebung:** Harte Notstromreserve in der Prognose.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Negativpreis-Leistungsanhebung nur für EPEX/Börse.
- 🐛 **Fehlerbehebung:** Speicher-Arbitrage nutzt Octopus-Endkundenpreis.
- ✨ **Verbesserung:** Wallbox-Preislimit geschärft.

## [5.1.5b] – 2026-06-05

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Echte Startanker bleiben sichtbar stabil.
- 🐛 **Fehlerbehebung:** Kein wandernder Morgenanker.
- 🧱 **Stabilität:** Hoher berechneter Ladebedarf darf früher laden.
- ✨ **Verbesserung:** Erreichbarkeit verständlicher.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Mobilansicht Update-Feld.
- 🐛 **Fehlerbehebung:** Update-direkten Webaufruf nutzt den aktuellen Einstieg.

## [5.1.5a] – 2026-06-04

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeug-SoC überlebt Regelung-Neustart.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfigurierte Dienste können automatisch installiert werden.
- ✨ **Verbesserung:** Wärmepumpen-Konfiguration typspezifisch.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Dimplex WPM Touch / NWPM via Modbus TCP.
- ✨ **Verbesserung:** Konfigurierbare Dimplex-Register.
- ✨ **Verbesserung:** SG-Ready-Steuerung.
- 🔎 **Diagnose:** Livewerte im Wärmepumpen-Weboberfläche.
- ✨ **Verbesserung:** Offizielle Dimplex-Dokumentation abgeglichen.
- ✨ **Verbesserung:** Heizstab/BWWP-Zusatzfelder werden eingeklappt.

## [5.1.5] – 2026-06-04

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** openWB-Phantomleistung nach Stop reduziert.
- 🧱 **Stabilität:** openWB Pro Wakeup robuster.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Kontrollierte Wallboxen pausieren unter dem Speicher-Untergrenze hart.
- 🐛 **Fehlerbehebung:** Speicherziel bleibt das Tagesziel.
- 🐛 **Fehlerbehebung:** Ruhiger Wiederanlauf oberhalb des Floors.
- 🐛 **Fehlerbehebung:** E3DC bleibt für Hauslasten frei.
- 🧱 **Stabilität:** Speicherladung bleibt Wallbox-unabhängig.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-ISG-Kühlstatus korrekt gelesen.
- 🧱 **Stabilität:** Ladekurven-Änderungen werden frischer übernommen.
- 🛡️ **Sicherheit:** Installationsverzeichnis gehärtet.

## [5.1.4h] – 2026-05-31

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Physikalischer Ampere-Deckel greift sofort.
- 🧱 **Stabilität:** Keine harten Neustart-Sprünge nach oben.
- 🛡️ **Sicherheit:** E3DC bleibt in Automatik.

## [5.1.4g] – 2026-05-31

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Berechneter Ladebedarf ist wieder feste Führungsgröße.
- 🧱 **Stabilität:** Wallbox-Leistungsbudget sieht dieselbe berechneter Ladebedarf-Führung.
- ✨ **Verbesserung:** Begriff geschärft.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Entladung bleibt im PV-Modus offen.

## [5.1.4f] – 2026-05-31

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** PV-Kurve ruhig bleibt PV-only.
- 🐛 **Fehlerbehebung:** Speicher schützt die Kurve bei aktiver Wallbox.
- ✨ **Verbesserung:** Zwei Zwischenziele für die Ladekurve.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Startfreigaben werden gehalten.
- 🧱 **Stabilität:** Phasenwechsel werden entprellt.
- 🔎 **Diagnose:** Mehr Pro-Diagnosen.

## [5.1.4e] – 2026-05-30

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Direkt angebunden Wallboxdaten werden berücksichtigt.

## [5.1.4d] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC-Halten gibt wieder frei.
- 🧱 **Stabilität:** Hysterese bleibt ruhig.
- 🐛 **Fehlerbehebung:** Direkt angebundene Wallbox-Bilanz gegen Messversatz.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Warnungen werden unabhängig vom PV-Prognose aktualisiert.
- 🐛 **Fehlerbehebung:** Wetterwarnungs-Statusanzeige erkennt mehr reale Signale.

## [5.1.4c] – 2026-05-30

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Kein falscher Luxtronik-Zwang in der Diagnose.
- 🐛 **Fehlerbehebung:** Stiebel-Fehlermeldung verständlicher.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Systempakete gezielt vorbereiten.
- ✨ **Verbesserung:** Headless-Paketmodus.
- 🧱 **Stabilität:** Paketliste zentralisiert.

## [5.1.4b] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC bleibt Speicherziel während externer Ladung.
- 🧱 **Stabilität:** Kein Rückfall zur höheren Tageskurve bei erreichtem Wallbox-Mindest-SoC.
- 🛡️ **Sicherheit:** Externe Wallbox bleibt Nebenbetrieb.

## [5.1.4a] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Externe Wallbox bleibt Nebenbetrieb.
- 🧱 **Stabilität:** Wallbox-Mindest-SoC ist die einzige Sondergrenze.
- 🧱 **Stabilität:** Kurze Wallbox-Intent-Aussetzer verlieren den Speicher-Entscheider nicht.
- 🛡️ **Sicherheit:** Keine Speicherentladung in externe Wallbox.

## [5.1.4] – 2026-05-30

### 🔋 Storage Manager

- ✨ **Verbesserung:** Zwei Kurven statt einer.
- 🐛 **Fehlerbehebung:** Speicherreserve nur aus echtem Abregeldruck.
- 🧱 **Stabilität:** Große und kleine Speicher werden unterschieden.
- 🛡️ **Sicherheit:** Abendziel vor schönem Speicherreserve.
- 🛡️ **Sicherheit:** Gezieltes Freihalten von Speicherkapazität bleibt letzter aktiver Eingriff.
- 🔎 **Diagnose:** Abregelschutz wird sichtbar.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Hauptsteuerung-Ladefenster takten nicht mehr bei kurzem 0-W-Leistungsbudget.
- 🧱 **Stabilität:** openWB/openWB Pro Netzfenster starten sanfter.
- 🛡️ **Sicherheit:** Autonome openWB bleibt autonom und schützt den Speicher.
- 🐛 **Fehlerbehebung:** openWB Pro startet robuster.
- 🐛 **Fehlerbehebung:** Tagesbilanz trennt externe openWB-Leistung wieder vom Hausverbrauch.

## [5.1.3f] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Direkt an E3DC angebundene Wallboxen werden nicht mehr fälschlich verriegelt.
- 🧱 **Stabilität:** Wiederholter Startversuch arbeitet nicht gegen eigene Stops.
- 🐛 **Fehlerbehebung:** Keine Phantomleistung aus RSCP-Restwerten.

## [5.1.3e] – 2026-05-30

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Volle oder startverweigernde Fahrzeuge werden nicht wiederholt angesteuert.
- 🐛 **Fehlerbehebung:** Eigene Stopps gelten nicht als abgeschlossenes Fahrzeugladen; nach Ladeende bleibt keine Phantomleistung stehen.
- 🧱 **Stabilität:** Ohne bestätigte Ladeleistung wird der Strom nicht weiter erhöht.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik erhält identische Warmwasserziele nicht mehr zyklisch erneut; iDM-Daten bleiben bei einer leeren Rückmeldung erhalten.

## [5.1.3d] – 2026-05-29

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Keine hängende Wallbox-Leistung nach Ladeende.
- ✨ **Verbesserung:** Browser-Glättung folgt derselben AHA-Logik.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Kerndienste werden beim Update gesammelt gestartet.
- 🧱 **Stabilität:** Dienstverwaltung-Startlimits werden vor dem finalen Neustart bereinigt.

## [5.1.3c] – 2026-05-29

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Pro-Reserve und alte Zählerwerte robuster.
- 🐛 **Fehlerbehebung:** Octopus-Heat-Kurve ohne Lücken.
- ✨ **Verbesserung:** Systemvoraussetzungen ergänzt.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Installationszentrum findet den echten Installationsassistent robuster.
- ✨ **Verbesserung:** Konfigurierte Dienste werden beim Speichern vorbereitet.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** MQTT-Hub installiert seine aktuelle Regelung Abhängigkeit.
- 🧱 **Stabilität:** Status-Topics sind keine Messwertfehler.

## [5.1.3b] – 2026-05-29

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Bestätigter Start, danach zügig Zielstrom.
- 🐛 **Fehlerbehebung:** Kein Rückfall auf 6 A bei widersprüchlichen openWB-Pro-Werten.
- 🛡️ **Sicherheit:** Start- und Ende-Regeln bleiben getrennt.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Solcast-Tagesbudget konfigurierbar.
- ✨ **Verbesserung:** Weboberfläche an aktuellen Solcast-Stand angepasst.

## [5.1.3a] – 2026-05-29

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Aus heißt aus, Ende heißt Ende.
- 🐛 **Fehlerbehebung:** Keine Startloops nach vollem Fahrzeug.
- 📊 **Anzeige/Auswertung:** Eindeutige Anzeige statt falscher Gewissheit.
- 🛡️ **Sicherheit:** NGNA bleibt still.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Auftrag endet bei Ziel-SoC.
- 🧱 **Stabilität:** Wartende Fenster schreiben nicht zyklisch.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** iDM-Kühlgrenze besser auffindbar.

## [5.1.3] – 2026-05-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Wallbox-Mindest-SoC-Rückfall friert die Kurve nicht mehr scheinbar ein.
- 🐛 **Fehlerbehebung:** Kurven-Neuberechnung bleibt nachvollziehbar.
- ✨ **Verbesserung:** Geplante Fremdlast-Stützung.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Wallbox-Mindest-SoC bleibt Schutzgrenze.
- 🐛 **Fehlerbehebung:** Phantomladen im Standby unterdrückt.
- 🧱 **Stabilität:** Modus-Prioritäten bleiben getrennt.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Standardisierte Diagnosepakete.

## [5.1.2d] – 2026-05-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Abregelschutz ignoriert Verbraucherrauschen.
- 🧱 **Stabilität:** Kurven-Rückstand holt bei aktiver Wallbox echte PV-Reste nach.
- 🧱 **Stabilität:** Prognose-Automatik nutzt freie Rest-PV vollständiger.
- 🧱 **Stabilität:** Lernender Korrekturrahmen bleibt bis zur Kurve aktiv.
- 🛡️ **Sicherheit:** Autonome Wallbox respektiert Wallbox-Mindest-SoC.
- ✨ **Verbesserung:** openWB-Reichweite wird lokal interpoliert.

### ☀️ PV, Prognose und Energiemanagement

- 🛡️ **Sicherheit:** Rechte-Reparatur respektiert den Standby-System-Standby.
- 🔎 **Diagnose:** Healthcheck unterscheidet Hauptsystem und Standby-System.

### ♨️ Wärmepumpe und Wärme

- 🧱 **Stabilität:** Eigene Kühlgrenze für iDM-Leistungsanhebung.
- ✨ **Verbesserung:** Konfigurierbare Kühlfreigabe.
- 🧱 **Stabilität:** Quell-Erholung bekommt eindeutigen Besitzer.
- 🐛 **Fehlerbehebung:** Stiebel/Shelly-Tagesverbrauch zählt wieder.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** HA-Standby-System startet keine Schreibdienste nach Update.

## [5.1.2c] – 2026-05-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Lernender Korrekturrahmen gegen Wellenladung.
- 📊 **Anzeige/Auswertung:** Ruhigere Anzeige im Speicherregelung.

## [5.1.2b] – 2026-05-28

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Autoerkennung führt die sichtbare Konfiguration.
- ✨ **Verbesserung:** Ladepunkte als Auswahl.
- 🛡️ **Sicherheit:** NGNA bleibt wirklich beobachtend.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Langzeit-Archivar als Kernsystem.
- 🧱 **Stabilität:** Kernsysteme werden vollständig sichergestellt.

## [5.1.2a] – 2026-05-27

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Komfort-Netz-Rückfall explizit.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB-Autoerkennung.
- ✨ **Verbesserung:** Weboberfläche zeigt erkannte Rolle.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Weboberfläche-Update pollingfest.
- 🧱 **Stabilität:** Update-Verarbeitung defensiv geladen.
- 🔄 **Migration/Kompatibilität:** Fehlerbehebung-Suffixe korrekt sortiert.

## [5.1.2] – 2026-05-27

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität zieht sicheren Speicherreserve ab.
- 🛡️ **Sicherheit:** Netz-Rückfall bleibt Restbedarf.
- ✨ **Verbesserung:** Komfort-gezieltes Freihalten von Speicherkapazität klar getrennt.
- 🐛 **Fehlerbehebung:** Normaler Kurvenrückstand holt weniger defensiv auf.
- 🧱 **Stabilität:** Laderahmen wird auch bei normalem Rückstand nachgeführt.
- ✨ **Verbesserung:** Kurvenanzeige nutzt Regel-SoC.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Tageszähler.
- 🔎 **Diagnose:** Kompakte Diagnose ohne Wärmepumpe stabil.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Docker-Rückfall als Host-Befehl.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Gezielt zurück auf Release.

## [5.1.1] – 2026-05-26

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Harte Kurvenanker werden nachgeführt.
- 🧱 **Stabilität:** Abendziel wird früher abgesichert.
- 📊 **Anzeige/Auswertung:** Anzeige spricht von Laderahmen.

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** openWB-Fremdregelung sichtbarer.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Tageswerte nach Mitternacht.
- 🔎 **Diagnose:** Kältespeicher im Verlauf.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Neuinstallation trotz aktuellem Git-Stand.
- 🧱 **Stabilität:** Weboberfläche-Abgleich gehärtet.
- 🛡️ **Sicherheit:** Keine doppelte Rechte-Reparatur.
- ✨ **Verbesserung:** Konsolenmenü aufgeräumt.

## [5.1.0] – 2026-05-25

### 🔋 Storage Manager

- 🧱 **Stabilität:** Kurvenladung wird sanfter geführt.
- 🐛 **Fehlerbehebung:** Sanfter Freilauf startet nicht mehr zu früh.
- 🐛 **Fehlerbehebung:** Kein Vollgas-Sprung beim Wallbox-Start.
- 🔎 **Diagnose:** Wirksamer Auftrag wird nachvollziehbar.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Gezieltes Freihalten von Speicherkapazität folgt dem Ziel bis zum Kurvenstart.
- ✨ **Verbesserung:** Verbraucherbudget klarer benannt.
- ✨ **Verbesserung:** Geplante Lastfenster.
- ✨ **Verbesserung:** Lastfenster-Schutzwerte sind feste Regeln.
- 🛡️ **Sicherheit:** Lastfenster unter 2 kW werden abgewiesen.
- 🛡️ **Sicherheit:** Zeitfenster werden direkt validiert.
- ✨ **Verbesserung:** Hinweistexte für Lastfenster.
- 🛡️ **Sicherheit:** Keine unplanbare Dauerlast-Raterei.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Speicherregelung ist Energie-Besitzer.
- ✨ **Verbesserung:** Quell-Erholung für geeignete Wärmepumpen.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Regelungsdokumentation aktualisiert.

## [5.0.6] – 2026-05-23

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität wird zeitlich geführt.
- 🧱 **Stabilität:** Verbrauchersteuerung für BEV/Wärmepumpe.
- 🧱 **Stabilität:** Später Kurvenrückstand darf Rest-PV mitnehmen.
- 🛡️ **Sicherheit:** Ziel erreicht heißt Freigabe.
- 🛡️ **Sicherheit:** Keine unplanbare Speicher-Preisstrategie.

### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Entscheidungslogs werden komprimiert.
- 🔎 **Diagnose:** EMS-Reaktionszeit sichtbar.
- 📊 **Anzeige/Auswertung:** Wallbox-Anzeige präzisiert.
- ✨ **Verbesserung:** PV-Kacheln zeigen Ist plus Restprognose.

## [5.0.5f] – 2026-05-23

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Hausversorgung bleibt freigegeben.
- 🧱 **Stabilität:** Wallbox-Regelung respektiert den Boden hart.
- 🐛 **Fehlerbehebung:** Diagnosegrund korrigiert.

## [5.0.5e] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nachtverbrauch nutzt echte saisonale Nachtdaten.
- 🔎 **Diagnose:** Max erreichbar bleibt nachvollziehbar.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox-Befehl-Sperre mit Datum.
- 🧱 **Stabilität:** Gezieltes Speicherplatzschaffen über die Wallbox bleibt kontrolliert geregelt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Unplausible PV-Plateaus werden physikalisch gekappt.

## [5.0.5d] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Aufholleistung skaliert wieder mit dem Speicher.
- 🧱 **Stabilität:** Lineares Taper-Band an der Kurve.
- 🐛 **Fehlerbehebung:** Kein Frei/0-W-Flattern nach Erreichen der Kurve.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wirksamer Ladeauftrag statt nur berechneter Ladebedarf.
- 🔎 **Diagnose:** Herleitung im Hinweistexte.
- ✨ **Verbesserung:** Keine Wallbox-Warteanzeige ohne Direkt angebundene Wallbox.

## [5.0.5c3] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Deutlicher aktueller Kurvenrückstand wird nachgezogen.
- 🛡️ **Sicherheit:** Keine Rückkehr zu Vollgas-Aufholjagden.
- 🔎 **Diagnose:** Rückstand in den Reglerdaten sichtbar.
- 🐛 **Fehlerbehebung:** Aktive EMS-Limits sind keine Hardware-Warnung.
- ✨ **Verbesserung:** Feld klarer benannt.

## [5.0.5c2] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Keine ungeführte Automatik-Aufholjagd unter der Kurve.
- 🧱 **Stabilität:** Berechneter Ladebedarf und Abregelbedarf werden sauber kombiniert.
- 🧱 **Stabilität:** Hysterese gegen Modusflattern.

### 🖥️ Weboberfläche

- 🛡️ **Sicherheit:** Keine Rückkehr zu direkten Web-Git-Rechten.
- ✨ **Verbesserung:** Kein erzwungener Echtzeit-Fetch vor jedem Dialog.
- 🔎 **Diagnose:** Schnellere Diagnose bei Startproblemen.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Update-Check nutzt den geschützter Installationsweg.

## [5.0.5c1] – 2026-05-22

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** RSCP-Derating bleibt Gegencheck.
- 🧱 **Stabilität:** Kein sinusfoermiges Gegenregeln.
- 🐛 **Fehlerbehebung:** Batterie- und Hausleistungs-Rückkopplung entfernt.
- 🐛 **Fehlerbehebung:** Berechneter Ladebedarf wird oberhalb der Momentankurve nicht mehr vorgezogen.
- 🛡️ **Sicherheit:** 300-W-Mindestfreigabe bei aktivem Abregelschutz.
- 🧱 **Stabilität:** Ladeverluste werden über den Netzpunkt ausgeregelt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Abregel-Ladebedarf vor berechneter Ladebedarf.
- 🔎 **Diagnose:** Ziel- und Freigabegrenze sichtbar.

## [5.0.5c] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität endet vor dem Beginn der Ladekurve.
- 🛡️ **Sicherheit:** Eine prognostizierte Wärmepumpenlast gilt nicht ohne Bestätigung als verfügbare Energiesenke.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ladefenster und ein bestätigtes Ladeende stoppen zuverlässig; ein volles Fahrzeug bleibt bis zum nächsten Steckvorgang gesperrt.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Abregelgrenze und Nachlauf wurden konservativer ausgelegt; große Fahrzeuglasten werden auch nahe der Wechselrichtergrenze sauber berücksichtigt.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Die 24- und 48-Stunden-Ansicht läuft wieder gleitend weiter; der Hausverbrauch wird bei aktiven EMS-Grenzen geglättet.

## [5.0.5b1] – 2026-05-21

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Elektrische Leistung als MQTT-Rückfall akzeptiert.

## [5.0.5b] – 2026-05-21

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Prognosejahr wird aus echtem Datum berechnet.
- ✨ **Verbesserung:** Prognosemonat als Hinweistexte.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Seite zeigt keine Luxtronik-Reste mehr.
- 🐛 **Fehlerbehebung:** Alte Echtzeit-Dienste werden beim Typwechsel deaktiviert.
- 🐛 **Fehlerbehebung:** Stiebel-Tagesenergie fließt in Verlauf und Hausverbrauchsbereinigung.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Octopus Heat verfälscht den Eco-Bewertung nicht mehr.

## [5.0.5a] – 2026-05-21

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Abregel-Schutz wird nach echtem Druck wieder freigegeben.
- 🛡️ **Sicherheit:** Echte PV-Spitzen bleiben geschützt.

## [5.0.5] – 2026-05-21

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Livedaten werden im Weboberfläche bevorzugt verarbeitet.
- 🧱 **Stabilität:** Hz-Webscraping hat automatischen Wiederholungsverzögerung.
- ✨ **Verbesserung:** Weboberfläche-Hinweis ergänzt.

## [5.0.4h] – 2026-05-21

### 🔋 Storage Manager

- 🔎 **Diagnose:** Kompakte Diagnose unterscheidet weiche Kurvenführung und echte Auffälligkeiten.
- 🐛 **Fehlerbehebung:** RSCP-Starteinstellungen werden zuverlässig gespeichert.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB-Software-Hauptsteuerung bewusst steuerbar.
- 🛡️ **Sicherheit:** openWB Pro bleibt eigener Direktsteuerung.
- 🐛 **Fehlerbehebung:** openWB-Pro-Phantomladung abgefangen.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Vitalwerte als PDF speicherbar.

## [5.0.4g] – 2026-05-20

### 🔋 Storage Manager

- ✨ **Verbesserung:** DWD-/Open-Meteo-Unwetterwächter.
- 🛡️ **Sicherheit:** Regeleingriff und Netzladen getrennt.
- ✨ **Verbesserung:** Nachtreserve und Netz-Morgenpuffer.
- ✨ **Verbesserung:** Winterereignisse berücksichtigt.
- ✨ **Verbesserung:** Batterie-Grunddaten entzerrt.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Externer Shelly-Leistungsmesser für Stiebel.
- 🐛 **Fehlerbehebung:** Docker-Installationszentrum blockiert Stiebel nicht mehr falsch.
- 🐛 **Fehlerbehebung:** ISG-Prozessdaten-Hz ist optionale Aktivierung und loggt ruhiger.
- 🔎 **Diagnose:** ISG-Register robuster gelesen.
- 🐛 **Fehlerbehebung:** Stiebel-Registeroffset explizit abgesichert.
- ✨ **Verbesserung:** WPMG-Phasenleistung als optionaler Direktwert.
- ✨ **Verbesserung:** Stiebel- und Docker-Doku erweitert.

## [5.0.4f] – 2026-05-20

### 🔋 Storage Manager

- 🔎 **Diagnose:** Tages-KPIs für Speicher, Energiemanagement und Wallboxen.
- 🐛 **Fehlerbehebung:** Implausible E3DC-Kapazitätsrohwerte werden plausibilisiert.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Speicherentscheidungen mit Regelbesitzer.
- 🔎 **Diagnose:** Wärmebudget sauber getrennt.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Stiebel-Eltron-Echtzeit-Dienst vorbereitet.
- 🛡️ **Sicherheit:** Keine Modbus-Schreibzugriffe im Echtzeit-Dienst.
- 🔎 **Diagnose:** Wärmepumpen-Diagnose generisch benannt.
- ✨ **Verbesserung:** Stiebel-Eltron-Dokumentation ergänzt.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** MQTT-Hub loggt ruhiger.

## [5.0.4e] – 2026-05-20

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kein hartes Vollgas bis zur SoC-Toleranz.
- 🔎 **Diagnose:** Neue Vergleichsdaten.
- ⚙️ **Regelung:** Mobilansicht Speicher-Regelung-Karte öffnet die Ladekurve.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Bright-Modus-Kontraste verbessert.
- ✨ **Verbesserung:** Redundantes Soll entfernt.
- 🐛 **Fehlerbehebung:** Energie-Flow-Layout bleibt updatesicher.
- 🐛 **Fehlerbehebung:** Flow-Punkte treffen die Nodes.

## [5.0.4d] – 2026-05-20

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Harte Entladung nur als Rückfall.
- 🔎 **Diagnose:** Kompakte Diagnose unterscheidet Verbraucher-Wartezustand und Rückfall.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Hardware-6A ist keine E3DC-Control-Startfreigabe.
- 🐛 **Fehlerbehebung:** Kein falscher Startimpuls bei Leistungsbudget 0.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Prognosegrafik erfindet keinen gezieltes Freihalten von Speicherkapazität mehr.
- ✨ **Verbesserung:** Prognose-Pause im Bright Modus lesbarer.
- ✨ **Verbesserung:** RSCP-Echtzeit-Statusanzeige besser erkennbar.

## [5.0.4c] – 2026-05-19

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Vitals-Direktseite lädt Helfer selbst.

## [5.0.4b] – 2026-05-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Saubere Freilauf-Übergabe am Kurvenende.
- 🐛 **Fehlerbehebung:** Batterie-Vitals laufen wieder unter Apache.



### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Alte intelligente Ladeplanung-/Ladeplanung-Navigation entfernt.
- 🔎 **Diagnose:** Mobilansicht Speicher-Status integriert.
- ✨ **Verbesserung:** Mobilansicht Ringanzeige bereinigt.
- 🐛 **Fehlerbehebung:** Darkmode-Schalter mobil repariert.
- 🧱 **Stabilität:** Batterie-Kachel bleibt stabil.
- 🔎 **Diagnose:** Echte Fehler im Vital-Weboberfläche sichtbar.

## [5.0.4a] – 2026-05-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kurvenladung gibt den Speicher nicht mehr komplett frei.
- 🐛 **Fehlerbehebung:** Neustart-/Haltezeit-Kante beseitigt.
- 🧱 **Stabilität:** Automatik ist wieder Übergabe statt Regelbefehl.
- 🛡️ **Sicherheit:** Keine neue Modusanzeige.

## [5.0.4] – 2026-05-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kurvenführung bleibt im E3DC-Automatik.
- 🐛 **Fehlerbehebung:** Preis-/Zeitfenster-Halten nutzt Entladegrenzen statt Ruhezustand.
- 🐛 **Fehlerbehebung:** Alte Nebenregelkreise entfernt.
- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität führt den Kurvenstart explizit.
- 🧱 **Stabilität:** Folgetage eindeutig.
- 🐛 **Fehlerbehebung:** Dynamische EMS-Momentanwerte nicht mehr als Hardware-Limits.
- 🐛 **Fehlerbehebung:** E3DC-Packkapazität wird auf Schrankebene normalisiert.
- ✨ **Verbesserung:** Interne V5-Regelungseinstellungen aus der normalen Konfiguration ausgeblendet.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Entscheidungsfunktionen getrennt.
- 🛡️ **Sicherheit:** Befehl-Sperre bleibt alleiniger Schreibzugang.

## [5.0.3e] – 2026-05-18

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Vor Morgenanker wird bei PV wieder geregelt.
- 🐛 **Fehlerbehebung:** Nacht-Automatik und Morgen-PV getrennt.
- 🧱 **Stabilität:** Kompakte Diagnose zeigt die neue Kante eindeutig.

## [5.0.3d] – 2026-05-18

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nachtbetrieb bleibt vor dem Morgenanker in Automatik.
- 🐛 **Fehlerbehebung:** Nachtanker-Fehler beseitigt.
- 🧱 **Stabilität:** Kurvenwerte werden zwischen aktiven Ankern interpoliert.

## [5.0.3c] – 2026-05-17

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Konfigurierbare Wallbox-Schaltzeiten pro Ladepunkt.
- 🐛 **Fehlerbehebung:** Kein automatisches Rückspringen auf PV-Modus.
- 🐛 **Fehlerbehebung:** Tageswerte mit Fremd-Wallbox bereinigt.
- 🐛 **Fehlerbehebung:** Langzeit-Archiv repariert aktuelle Fehlzeilen.
- 🧱 **Stabilität:** openWB und openWB Pro strikt getrennt.
- 📊 **Anzeige/Auswertung:** Capability-Anzeige angepasst.

## [5.0.3b] – 2026-05-17

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Deaktiviert / Keine Wallbox ist hart NGNA.
- 🐛 **Fehlerbehebung:** openWB wechselt nicht mehr unbeabsichtigt in den PV-Modus zurück.
- 🧱 **Stabilität:** openWB und openWB Pro getrennt behandelt.
- 🧱 **Stabilität:** Frische 3p-Kommandos gewinnen gegen veraltet openWB-Status.
- 🔎 **Diagnose:** Letzter Wallbox/openWB-Befehl sichtbar.

## [5.0.3a] – 2026-05-17

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität verhindert vermeidbaren Netzbezug.
- 🐛 **Fehlerbehebung:** Proaktive Kurvenbremse oberhalb der Ladekurve.
- 🧱 **Stabilität:** Kurvenführung bleibt netzschonend.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeugidentität stabilisiert.
- 🧱 **Stabilität:** openWB/openWB Pro rampen schneller und ruhiger.
- 🐛 **Fehlerbehebung:** openWB-Pro-Startfreigabe bei 0-Leistungsbudget gehärtet.
- ✨ **Verbesserung:** openWB-Pro-Firmwareupdate auf Nutzerwunsch.

## [5.0.3] – 2026-05-17

### 🔋 Storage Manager

- ✨ **Verbesserung:** Speicher-Entscheidungsrecorder.
- ✨ **Verbesserung:** Voller Regelung-Entscheidungsbaum.
- ⚙️ **Regelung:** Diagnosefenster zeigt Regelung-Entscheidungen.
- 🧱 **Stabilität:** Formale Speicher-Zustandswechsel.
- 🧱 **Stabilität:** Prognose-Vertrauen als Regelgröße.
- 🐛 **Fehlerbehebung:** Nacht-Idle bei Netzbezug gehärtet.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Wallbox Befehl-Sperre.
- 🔎 **Diagnose:** Wallbox Befehl-Sperre Diagnose.
- 🐛 **Fehlerbehebung:** openWB/openWB Pro Fahrzeugerkennung.
- 🧱 **Stabilität:** openWB/openWB Pro Startimpuls beruhigt.
- ✨ **Verbesserung:** Ladeplanung gegen Fehlbedienung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zentraler Konfigurationsprüfung.
- ✨ **Verbesserung:** Ladekurven-Vorschau in der Konfiguration.



### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Top-Menü und Wärme-Status bereinigt.
- 🛡️ **Sicherheit:** Seiten-Neustarts laufen über Dienst-geschützter Aufruf.

## [5.0.2e] – 2026-05-16

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Bezugspunkte der Ladekurve bleiben innerhalb ihres Zeitfensters stabil.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Ungültige Echtzeitdaten bleiben wirkungslos; eine Haltezeit verhindert Pendelbewegungen zwischen Automatik und aktiver Regelung.
- 🔎 **Diagnose:** Die wirksamen Feineinstellungen sind in der Diagnose sichtbar.

## [5.0.2d] – 2026-05-15

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Gastfahrzeuge mit unbekannter Phasenzahl.



### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Watchdog-Nachlauf nach Updates.
- 🐛 **Fehlerbehebung:** Rechte-Reparatur meldet Erfolg korrekt.
- 🐛 **Fehlerbehebung:** Dienst-Restarts normalisiert.

## [5.0.2c] – 2026-05-15

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Morgige Ladekurve nach Sonnenuntergang sichtbar.
- ✨ **Verbesserung:** Prognosechart zeigt die Sollkurve.
- 🐛 **Fehlerbehebung:** Fahrzeuganzeige wird beim Abstecken bereinigt.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Watchdog-Startphase mit regelmäßige Statusmeldung.
- 🐛 **Fehlerbehebung:** Notfall-Neustart nutzt Dienst-geschützter Aufruf.

## [5.0.2b] – 2026-05-15

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Watchdog pausiert bei Updates.
- ✨ **Verbesserung:** Der installierte Systemwächter erhält Korrekturen bei Aktualisierungen automatisch.

## [5.0.2a] – 2026-05-15

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Kurvenbremse auf Kurven-Plateaus.
- 🛡️ **Sicherheit:** Netzwächter für echte Lastsprünge.
- 🐛 **Fehlerbehebung:** PV-Kurve und Ziel-Wallbox-Mindest-SoC sauber getrennt.
- 🧱 **Stabilität:** Wallbox-Hochlauf und Fahrzeugphasen gehärtet.

## [5.0.2] – 2026-05-15

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Solcast-Mehrdach-Hinweis hervorgehoben.
- 🐛 **Fehlerbehebung:** Wärme-Regelungs-Statusanzeige nur bei Wärmeverbraucher.

## [5.0.1] – 2026-05-15

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Neuer Speicherregelung ist jetzt kanonisch.
- 🐛 **Fehlerbehebung:** Frühere manuelle Vorgaben werden bei der Aktualisierung übernommen.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zwei Solcast-Dachflächen mit einem Account.
- ✨ **Verbesserung:** Solcast-Hilfe erweitert.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Weboberfläche-Update nutzt den sicheren geschützter Installationsweg.
- 🐛 **Fehlerbehebung:** Rechte-Reparatur aus dem Weboberfläche bleibt geschützter Aufruf-konform.

## [5.0.0] – 2026-05-15

### 🔋 Storage Manager

- ✨ **Verbesserung:** Speicherregelung Next.
- 🐛 **Fehlerbehebung:** Aktive RSCP-Eingriffe werden gehalten.
- 🛡️ **Sicherheit:** Entladeleistung respektiert Nutzereingabe.
- 🛡️ **Sicherheit:** Notstromreserve und Reserven bleiben geschützt.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** NGNA wirklich One-Shot.
- ✨ **Verbesserung:** Ladepriorität nur bei Dual-Wallbox.
- 🧱 **Stabilität:** openWB, openWB Pro und go-e verwenden eine gemeinsame Wallboxregelung.
- 🐛 **Fehlerbehebung:** Geplante Ladefenster enden hart.
- ✨ **Verbesserung:** Fahrzeug-SoC je Wallbox stabiler.

### 🖥️ Weboberfläche

- ⚙️ **Regelung:** Regelung-Statusanzeige vereinheitlicht.
- 🔎 **Diagnose:** Wiederkehrende Hinweise der Speicher-, Wallbox- und Wärmeregelung werden gedrosselt; Warnungen, Fehler und wichtige Zustandswechsel bleiben sichtbar.
- ✨ **Verbesserung:** Energiefluss Layout bleibt erhalten.

## [4.9.9d] – 2026-05-14

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik/Energie-Regelung startet wieder sauber.

## [4.9.9c] – 2026-05-14

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Direkt an E3DC angebundene Wallbox startet robuster.
- 🐛 **Fehlerbehebung:** Fahrzeugzuordnung stabilisiert.
- 🐛 **Fehlerbehebung:** Keine Phantom-Fahrzeuge an freier WB2.

## [4.9.9b] – 2026-05-13

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Energiefluss Layout bleibt nach Updates erhalten.

## [4.9.9a] – 2026-05-13

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Ladekurve vor dem ersten Stuetzwert korrekt.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Prognose-Halt für alle aktiven Wallbox-Modi.
- 🐛 **Fehlerbehebung:** openWB Pro fällt im Prognose-Halt nicht mehr auf 1p zurück.
- 🛡️ **Sicherheit:** Prognose-Automatik ist Haltefreigabe, kein Vollgas.
- 🧱 **Stabilität:** Frühere Steuerungnahe Wallbox-Nachführung.
- 🧱 **Stabilität:** E3DC bleibt häufiger autonom.
- 🔎 **Diagnose:** Freies Wallbox-Leistungsbudget sichtbar.
- 🐛 **Fehlerbehebung:** Fahrzeugprofile kommen ins Weboberfläche.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik-Zustände vereinfacht.
- 🐛 **Fehlerbehebung:** Ladeplanung trennt WB1 und WB2 sichtbar.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Energie-Flow-Layout-Editor.
- ✨ **Verbesserung:** Standardlayout per Schaltflächen.
- 🐛 **Fehlerbehebung:** Einstellungen speichern bleibt sichtbar.
- 🐛 **Fehlerbehebung:** Speicher-Regelung-Status bleibt ohne Wallbox erhalten.

## [4.9.9] – 2026-05-12

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox-Modi bereinigt.
- 🐛 **Fehlerbehebung:** Wallbox-Modus und Fahrzeugzuordnung speichern wieder sicher.
- 🛡️ **Sicherheit:** Aus ist wirklich aus.
- ✨ **Verbesserung:** Geplantes Netzladen in allen aktiven Modi.
- 🧱 **Stabilität:** Speicher-/Wallbox-Übergänge abgesichert.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Direkte evcc/openWB-Wallboxleistung wirkt sofort.
- 📊 **Anzeige/Auswertung:** Deaktivierte Wallboxen verschwinden aus der Oberfläche.
- 🐛 **Fehlerbehebung:** Dienstverwaltung-Startlimit korrekt platziert.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** MQTT-Konfigurationswechsel greift ohne Handarbeit.

## [4.9.8i] – 2026-05-12

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ziel-Wallbox-Mindest-SoC deckelt externe Wallboxen sauber.
- 🧱 **Stabilität:** Direkt an E3DC angebundene Wallbox am 6A/3p-Minimum beruhigt.
- 🛡️ **Sicherheit:** Phasen- und Startlogik respektiert den wirksamen Deckel.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Neuinstallation liefert Weboberfläche nicht mehr als Klartext aus.
- 🐛 **Fehlerbehebung:** Web-Update-Vorprüfung reparierbar.

## [4.9.8h] – 2026-05-12

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Preisplan-Speicherschutz dreistufig.
- 🐛 **Fehlerbehebung:** Ziel-Wallbox-Mindest-SoC flacht die Speicher-Ladekurve ab.
- 🧱 **Stabilität:** openWB/openWB-Pro-Regelung beruhigt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Hausverbrauch stabiler bei externer Wallboxlast.
- ✨ **Verbesserung:** Prognose-Azimuth frei eingebbar.
- ✨ **Verbesserung:** Prognose-Zeile aufgeraeumt.

## [4.9.8g] – 2026-05-12

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Echtzeit-Status bleibt Weboberfläche-kompatibel.
- 🧱 **Stabilität:** Hausabsicherung bleibt im Blick.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Per-Wallbox-Maximalstrom.
- ✨ **Verbesserung:** Wallbox-Bedienung entschlackt.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Direkte MQTT-Wallboxleistung.
- ✨ **Verbesserung:** MQTT-Konfiguration sichtbarer.

## [4.9.8f] – 2026-05-11

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Installations- und Aktualisierungsablauf direkt startbar.
- 🐛 **Fehlerbehebung:** Wallbox-Netzpreislimit begrenzt nur Modus 5.
- ✨ **Verbesserung:** Historische openWB-Echtzeit-Direktsteuerung entfernt.
- ✨ **Verbesserung:** Bright-Modus-Konsistenz verbessert.
- 🐛 **Fehlerbehebung:** MQTT/HA-Eingang ignoriert leere Messwerte.
- ✨ **Verbesserung:** Fieser-Kardinal-Workaround dokumentiert.

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Docker-Erststart bleibt wartend statt tot.
- 🐛 **Fehlerbehebung:** Das Installationszentrum arbeitet im Docker-Betrieb robuster.

## [4.9.8e] – 2026-05-11

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität ist wieder eindeutig deaktivierbar.
- 🐛 **Fehlerbehebung:** Startfenster 0 ist Auto statt Tot-Schalter.
- ✨ **Verbesserung:** Wetterwarnungs-Statusanzeige.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Echtzeit-Tasten wieder robust.
- ✨ **Verbesserung:** Direktladen klar benannt.
- 🛡️ **Sicherheit:** Web-Schaltflächen sind echte Schaltflächen.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wärmepumpe farblich getrennt.
- 🐛 **Fehlerbehebung:** Strompreis-Kurven ohne falsche Flanken.
- 🐛 **Fehlerbehebung:** Release-Datum stabil.
- 🐛 **Fehlerbehebung:** Docker-Portprüfung robuster.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Die Aktualität wird für jeden MQTT- und Home-Assistant-Messwert getrennt bewertet.

## [4.9.8d] – 2026-05-11

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Gewitter-/PV-Einbruch beruhigt.
- 🧱 **Stabilität:** Netz-Wächter nutzt reale Entladung.
- ✨ **Verbesserung:** Klarnamen im Weboberfläche.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** MQTT/HA-Wärmepumpe sichtbar.

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Docker-Installationen erhalten wieder zuverlässig das aktuelle Container-Abbild.
- ✨ **Verbesserung:** Docker-Volumes erklärt.

## [4.9.8c] – 2026-05-11

### 🔋 Storage Manager

- 🧱 **Stabilität:** Gezieltes Freihalten von Speicherkapazität wartet sauber auf Verbraucher.
- 🐛 **Fehlerbehebung:** Kurvenbremse entlastet über Wallbox.
- 🧱 **Stabilität:** Übergänge abgesichert.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Normale openWB nutzt offizielle Simple Schnittstelle-Modi.
- 🧱 **Stabilität:** openWB Pro Start- und Phasenlogik beruhigt.
- 🐛 **Fehlerbehebung:** BEV-voll-Sperre entfernt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Scheinleistung für openWB/openWB Pro sichtbar.
- 🐛 **Fehlerbehebung:** Hausverbrauch bei Fremdverbrauchern geglättet.
- 🐛 **Fehlerbehebung:** HA/io Broker-Messwerte dokumentiert und abgesichert.

## [4.9.8b] – 2026-05-10

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Verschachtelte Alt-Repositories ignoriert.
- 🐛 **Fehlerbehebung:** Versionsvergleich robuster.

## [4.9.8a] – 2026-05-10

### 🖥️ Weboberfläche

- 🧱 **Stabilität:** Der Link zum PV-Forum öffnet wieder zuverlässig den neuesten Beitrag.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Phantom-Statusanzeige im Web-Oberfläche-Update entfernt.
- 🐛 **Fehlerbehebung:** Self-Update-Check nutzt frische Werte.
- 🐛 **Fehlerbehebung:** Git-Vergleich robuster.

## [4.9.8] – 2026-05-10

### 🔋 Storage Manager

- 🧱 **Stabilität:** Ladekurvenführung beruhigt.
- 🧱 **Stabilität:** Netz-Wächter und Abregelschutz sauberer platziert.
- 🧱 **Stabilität:** Preis-/Netzladen begrenzt.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Neue reduzierte Modusstrategie.
- 🧱 **Stabilität:** Schütz-Flattern reduziert.
- 🐛 **Fehlerbehebung:** openWB/openWB Pro Tageszähler haben Vorrang.
- 🐛 **Fehlerbehebung:** Zwei-Wallbox-Statistik konsolidiert.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Tageswerte in den Kacheln.
- 🐛 **Fehlerbehebung:** PV-Kachel gehärtet.
- 🐛 **Fehlerbehebung:** SoC-Prognose-Kopf rechnet Tages-Restwerte.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Diagnosepakete maskieren MQTT-Zugangsdaten.

## [4.9.7g] – 2026-05-10

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB-Startschleife gedrosselt.
- ✨ **Verbesserung:** Abbruchzähler robuster.
- ✨ **Verbesserung:** openWB-Pro-SoC wird auch während der Ladung nutzbar.
- ✨ **Verbesserung:** Unbekannte openWB-Fahrzeuge können direkt zugeordnet werden.
- 🐛 **Fehlerbehebung:** Reichweite und geladene Reichweite werden getrennt behandelt.
- 🐛 **Fehlerbehebung:** Bluelink/Online-Dienst-Werte haben Vorrang vor Schätzwerten.
- 🐛 **Fehlerbehebung:** Dual-Wallbox-Fahrzeugzuordnung bleibt Zeitfenster-treu.
- 🐛 **Fehlerbehebung:** openWB/openWB-Pro-Steckerstatus bleibt auch bei 0 W sichtbar.
- ✨ **Verbesserung:** Wallbox-Modi sind verständlicher benannt.
- 🐛 **Fehlerbehebung:** Leistungsänderungen der Wallbox erfolgen in ruhigeren Schritten.
- 🐛 **Fehlerbehebung:** Preisfenster schützen den Hausakku.
- 🐛 **Fehlerbehebung:** Kurvenauslauf ist ruhiger.
- 🐛 **Fehlerbehebung:** WB2-Ladeplanung nutzt den richtigen Wallbox-Typ.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Diagnosepakete sind forumstauglich kompakter.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Wärmepumpe sauber deaktivierbar.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Leere oder defekte Betriebsdaten blockiert HA nicht mehr.
- 🐛 **Fehlerbehebung:** Aktualisierungen korrigieren veraltete Installationsangaben automatisch.
- 🐛 **Fehlerbehebung:** ML-Artefakte bekommen gemeinsame Web-Rechte.
- ⚙️ **Regelung:** Docker-Planung nach ML-Lernprozess klarer eingeordnet.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Mittagsziel erzwingt Ladekurvenführung.
- 🐛 **Fehlerbehebung:** Kurventoleranz greift wieder bei 3% statt 30%.
- ✨ **Verbesserung:** Ein gemeldeter Praxisfall wurde abgesichert.
- 🐛 **Fehlerbehebung:** Lokale ML-Modelle erhalten zuverlässig die benötigten Zugriffsrechte.
- ✨ **Verbesserung:** Speicher-Simulator loggt ML-Verbrauchsquellen eindeutig.
- 🐛 **Fehlerbehebung:** ML-Lernprozess kann in Docker aus der Echtzeit-Historie starten.

## [4.9.7b] – 2026-05-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Eingestelltes Mittagsziel bleibt harte Nutzervorgabe.
- ✨ **Verbesserung:** Abendziel bleibt getrennte Erreichbarkeitsfrage.
- ✨ **Verbesserung:** Optionale Integrationen können über das Installationszentrum eingerichtet werden.
- ✨ **Verbesserung:** Konfigurationsbuttons springen zum richtigen Feld.
- 🐛 **Fehlerbehebung:** Die Einrichtung optionaler Integrationen prüft die benötigten Zugriffsrechte.
- 🐛 **Fehlerbehebung:** Kein falsches Gruen bei sofort crashenden Diensten.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Externe Wallbox-Leistung wird aus dem Hausverbrauch herausgerechnet.
- ✨ **Verbesserung:** SoC-Auslesung sauber abgesichert.
- ✨ **Verbesserung:** openWB Pro und Dual-Wallbox-Betrieb integriert.
- 🐛 **Fehlerbehebung:** Energiefluss und Wallbox-Kacheln für zwei Ladepunkte.
- 🐛 **Fehlerbehebung:** SoC-Prognose wieder regelungsnah.
- ✨ **Verbesserung:** Versionierung bereinigt.
- 🐛 **Fehlerbehebung:** Veraltet Multi-Connect-/Wallbox-Summenwerte werden herausgefiltert.
- 🐛 **Fehlerbehebung:** Kein aktives Laden ohne verbundenes Fahrzeug.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Prognose springt nach Tagesende auf den nächsten PV-Tag.
- 🐛 **Fehlerbehebung:** Prognose bleibt physikalischer und glatter.
- 🐛 **Fehlerbehebung:** Wallbox-Verlauf pro Ladepunkt trennbar.
- 🐛 **Fehlerbehebung:** Ökobewertung blockiert gezieltes Freihalten von Speicherkapazität nicht mehr pauschal.

### 🖥️ Weboberfläche

- 🛡️ **Sicherheit:** Etappen-Hinweise durch Betriebsmodell ersetzt.
- ✨ **Verbesserung:** Installationszentrale dokumentiert.
- 🔎 **Diagnose:** Diagnosepaket für Bare Metal und Docker.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** WB1/WB2 per MQTT getrennt nutzbar.
- ✨ **Verbesserung:** MQTT-Wizard, Hilfe und Smart-Home-Doku erweitert.
- 🔎 **Diagnose:** Diagnosepaket enthält MQTT-Eingangsdaten.
- ✨ **Verbesserung:** MQTT-Hub im Docker erklärt.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** ML-Modell bei frischen Docker-Installationen erklärt.

## [4.9.6] – 2026-05-06

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die E3DC-Automatik ist wieder der neutrale Grundzustand und wird nicht durch alte Lade- oder Entladegrenzen festgehalten.
- 🛡️ **Sicherheit:** Notstromreserve, Abregelschutz und Netzbezugsschutz haben Vorrang vor Komfort- und Preisoptimierung.
- ⚙️ **Regelung:** Unterhalb der Ladekurve bleibt der Speicher regelbar; ein unerreichbares Tagesziel löst keine Kurvenjagd aus.
- 🧱 **Stabilität:** Morgenpuffer, Tagesziel und Schlechtwetterreserve werden mit Hysterese und stabilen Bezugspunkten geführt.
- 🔎 **Diagnose:** Kapazität, Zelltemperaturen, Ladezyklen und SoH mehrerer Batterieschränke werden plausibel zusammengeführt.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Hausanschlussgrenze und Mindest-SoC schützen auch Installationen mit mehreren Wallboxen.
- 🐛 **Fehlerbehebung:** Phantomleistung, Messversatz und ein veralteter Kabelstatus werden nicht mehr als echte Ladung gewertet.
- 🧱 **Stabilität:** Start, Stopp und Leistungsänderungen erfolgen nur bei bestätigter Ladung; laufende Stromvorgaben überstehen einen Neustart.
- 📊 **Anzeige/Auswertung:** Reichweite, Steckerzustand und mehrere Ladepunkte werden eindeutig dargestellt.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Der Speicherregelung bleibt alleiniger Entscheider für Speicherbefehle; Wallbox und Wärmepumpe melden nur ihren Leistungsbedarf.
- 🐛 **Fehlerbehebung:** Hausverbrauch, SoC-Prognose und PV-Prognose werden nicht durch einzelne Ausreißer oder doppelt gezählte Verbraucher verfälscht.
- 🛡️ **Sicherheit:** Gezieltes Freihalten von Speicherkapazität endet bei schlechterer Prognose und verhindert vermeidbaren Netzbezug.

### ♨️ Wärmepumpe und Wärme

- 🧱 **Stabilität:** Ein modulierender Heizstab reagiert bei kleinem PV-Überschuss ruhiger und wird im Energiefluss nicht doppelt gezählt.
- 🐛 **Fehlerbehebung:** Unterbrochene Modbus-Verbindungen erzeugen keine schnellen Wiederholungen; echte Fehler bleiben von der Leistungsbilanz getrennt.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Der Tageswechsel erzeugt keinen falschen Nullpreis; zusammenhängende Preisfenster bleiben ohne Unterbrechung nutzbar.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Planwerte und Echtzeitspitzen, Wallboxzustände sowie Ladekurve und Prognose werden klarer getrennt.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** MQTT und Home Assistant erhielten eine geführte Einrichtung, nachvollziehbare Datenkanäle und automatische Geräteerkennung.
- 🐛 **Fehlerbehebung:** Matter-Schalter erhalten eindeutige Namen; ein Zurücksetzen der Kopplung bricht die Weboberfläche nicht ab.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Docker unterstützt frei zugeordnete Webanschlüsse; Aktualisierung, vollständiger Produktumfang und Hinweise für Synology-Installationen wurden korrigiert.

## [4.9.3] – 2026-05-01

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nacht- und Abendfreigabe zeigen wieder echtes Automatik.
- ✨ **Verbesserung:** Keine Kurvenjagd nach PV-Ende.
- 🐛 **Fehlerbehebung:** Max erreichbar bezieht sich auf den angezeigten Tag.
- ✨ **Verbesserung:** Dauerzustände werden gedrosselt geloggt.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB/go-e erzeugen keinen heimlichen Ladeplan mehr.
- ✨ **Verbesserung:** Wallbox-Ladeplanung ohne Plan-Spam.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Systemwächter reagiert weniger nervös auf kurze Dienst-Neustarts.
- 🐛 **Fehlerbehebung:** Rechte- und Altprozess-Bereinigung robuster.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik-Werte gehen nicht mehr auf dem Weg ins Weboberfläche verloren.
- 🔎 **Diagnose:** Wiederkehrende Null-Watt-Hinweise der Wärmepumpen werden reduziert; Schutzentscheidungen und echte Zustandswechsel bleiben sichtbar.
- 🐛 **Fehlerbehebung:** Langzeit- und Peak-Saving-Anzeigen konsistenter.
- ✨ **Verbesserung:** Hilfe, README und Fach-Dokumentation auf 4.9.3 aktualisiert.

## [4.9.2] – 2026-05-01

### 🔋 Storage Manager

- ✨ **Verbesserung:** Abregelschutz berücksichtigt Kurvennachlauf.
- ⚙️ **Regelung:** Abregelschutz-Rampe gegen Sollwert-Springen.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB/go-e Phantomwerte und Preisboost-Freigabe.
- ✨ **Verbesserung:** Systemwächter startet Speicherregelung vor Failsafe neu.
- ✨ **Verbesserung:** Ladekurven-Erklärung erweitert.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Negativpreis-/Preis-Leistungsanhebung für Speicher, Wallbox und Wärmepumpe.
- ✨ **Verbesserung:** Sicherer Ausstieg aus dem Preis-Leistungsanhebung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Preis-Leistungsanhebung und Ladekurvenstatus im Weboberfläche.

## [4.9.1c] – 2026-05-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Ladekurven-berechneter Ladebedarf wird direkt gesendet.
- ✨ **Verbesserung:** Keine doppelten CHRG-Pulse durch berechneter Ladebedarf-Obergrenze.
- ✨ **Verbesserung:** Wallbox-Prioritäten werden ohne widersprüchliche Speicherentladung umgesetzt.

## [4.9.1b] – 2026-05-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Vorgelagerte Entladung nutzt eigene Entladerampe.
- 🧱 **Stabilität:** Trajektorien-Hysterese für vorgelagerte Entladung.
- ✨ **Verbesserung:** Systemwächter-regelmäßige Statusmeldung in vorgelagerte Entladung-Pausen.

## [4.9.1a] – 2026-04-30

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Keine Tagesladekurven-Eingriffe bei PV 0W.
- ✨ **Verbesserung:** Nachtfreigabe nach defensivem Start-Ruhezustand.
- 🐛 **Fehlerbehebung:** Zweite Solcast-Konfiguration überlebt Docker-Neustart.
- 🐛 **Fehlerbehebung:** Mehrere Solcast-Prognosen werden korrekt zusammengeführt.
- 🐛 **Fehlerbehebung:** Solcast-Zwischenspeicher wird bei geaenderter Resource-ID erneuert.
- 🐛 **Fehlerbehebung:** ML-Prognose wird atomar geschrieben.

## [4.9.0] – 2026-04-30

### 🔋 Storage Manager

- ⚙️ **Regelung:** Die Ladekurve dient als weiches Zwischenziel und bremst den normalen PV-Betrieb nicht unnötig aus.
- 🛡️ **Sicherheit:** Abregelschutz und Notstromreserve haben Vorrang vor der Ladekurve.
- 🐛 **Fehlerbehebung:** Eine zu steile Ladekurve, fehlerhafte Null-Prozent-Ziele und verlorene Zielzeitpunkte nach Neustarts wurden korrigiert.
- ✨ **Verbesserung:** Manuelle Lade- und Entladevorgaben sind wieder verfügbar und werden nicht durch alte Sperrzustände blockiert.
- 🧱 **Stabilität:** Kurvenziele bleiben in einem rollierenden Zeitfenster stabil und reagieren nicht auf einzelne Messwertsprünge.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Der Mindest-SoC der Hausbatterie wird mit Hysterese geschützt; Laden stoppt bei echtem Netzbezug kontrolliert.
- ⚙️ **Regelung:** E3DC-Wallbox und openWB erhalten getrennte, klar zugeordnete Betriebsarten und Leistungsbudgets.
- 🧱 **Stabilität:** Stromänderungen erfolgen in begrenzten Schritten; Start, Pause und Wiederaufnahme werden zeitlich beruhigt.
- 📊 **Anzeige/Auswertung:** Wallbox-Betriebsarten und Feineinstellungen werden in verständlichem Klartext angezeigt.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Die Regelung hält den Speicher nahe der Zielkurve, nutzt drohende Abregelung aber weiterhin vorrangig zum Laden.
- 🐛 **Fehlerbehebung:** Netzbezug, unvollständige SoC-Messungen und veraltete Kurvenpunkte führen nicht mehr zu sprunghaften Lade- oder Entladevorgaben.
- 🧱 **Stabilität:** Wolkenphasen, Neustarts und kurzfristige Prognoseabweichungen werden mit Hysterese und begrenzten Leistungsänderungen abgefangen.
- ✨ **Verbesserung:** Verbrauchsprognose und Wetterbewertung liefern stündlich aktualisierte Ladeziele.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** PV-Überschuss und zulässige Speicherleistung werden bei der Freigabe von Wärmepumpe und Heizstab gemeinsam berücksichtigt.
- 🐛 **Fehlerbehebung:** Ein frisches Null-Watt-Leistungsbudget wird als echtes Stoppsignal behandelt.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Ladekurve, Ist-SoC und Prognose werden gemeinsam dargestellt; Wallbox- und Wärmeeinstellungen sind verständlicher gegliedert.

## [4.6.9] – 2026-04-28

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** EP-Reserve PV-Überschuss: Batterie lud nur 300W statt voller Kapazität.



### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Ladeleistung flackert 2000W↔3000W bei Speicherladung (Über-Kurve-Bremse).
- 🐛 **Fehlerbehebung:** Automatik-Modus: Speichersteuerung verursacht Leistungsvorgabe Konflikt.
- ✨ **Verbesserung:** Minimales Ladelimit 300W durchgängig.

## [4.6.8] – 2026-04-28

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Ursache (Timing-Bug).
- ✨ **Verbesserung:** Betroffene Anlagen.

## [4.6.7] – 2026-04-28

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Speicherregelung - SURVIVAL-Zustand sperrte Laden auch bei PV-Überschuss.
- ✨ **Verbesserung:** Betroffene Anlagen.
- 🐛 **Fehlerbehebung:** Installations- und Aktualisierungsablauf - Messwertfilter fehlte in file definitions.

## [4.6.6] – 2026-04-28

### 🔋 Storage Manager

- ✨ **Verbesserung:** Speicherregelung — Falsche RSCP-Moduswahl „Weit/Über Kurve".

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** WR-Speicherreserve-Check (NEU).
- ✨ **Verbesserung:** Netz-Schutz für Speicherladung-Limitierung.
- 🐛 **Fehlerbehebung:** Leistungsbegrenzung — 300W Minimum für Vorgabe der maximalen Ladeleistung.

## [4.6.5] – 2026-04-27

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die maximale Entladeleistung lässt sich wieder zuverlässig speichern; ein verborgenes doppeltes Eingabefeld überschreibt den Nutzerwert nicht mehr.
- ✨ **Verbesserung:** Speicher Automatik Entladung threshold (%, Standard: 20).
- 🐛 **Fehlerbehebung:** Speicherregelung: Vorgabe der maximalen Ladeleistung wurde für kleine Werte von der Firmware ignoriert.
- ✨ **Verbesserung:** Speicherregelung: Morgen SoC Ladekurve startete beim aktuellen SoC (82%) statt bei morgendliches Speicherziel (25%).
- 🔄 **Migration/Kompatibilität:** Speicherregelung — Morgen SoC Ladekurve Deckel, today Plan Zeitfenster Rückfall.
- 🐛 **Fehlerbehebung:** Speicherregelung: Discharge-manuelle Vorgabe wurde bei jedem PV > 0W abgebrochen.
- ✨ **Verbesserung:** Speicherregelung: Speichersteuerung im PV-Normalbetrieb eingefroren die Batterie.
- ✨ **Verbesserung:** Wann zuletzt korrekt.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Anbindung - Modus 9 lud nur mit PV-Leistung (4kW statt 11kW).
- ✨ **Verbesserung:** Batterieschutz bei Wallbox-Mindest-SoC.
- 🐛 **Fehlerbehebung:** Max-Schaltflächen (Sofortladen direkt angebunden) setzte falschen Modus.
- ✨ **Verbesserung:** Wallbox-Betriebsdaten als verbindliche Dokumentation.
- ✨ **Verbesserung:** Wallbox Modus params Kommentare.
- ✨ **Verbesserung:** Dropdown-Abschnitte klar beschriftet.
- 🐛 **Fehlerbehebung:** Safety-Watcher drosselte "Sofortladen" nach 45s auf 6A.
- ✨ **Verbesserung:** E3DC Direkt angebundene Wallbox: Ladefenster schlagen fehl (physischer Abbruch).

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Energiemanagement - erweiterte Speicherplanung setzte Wallboxmodus 10 (Netz erlaubt).
- 🐛 **Fehlerbehebung:** Speicherregelung - EP-Reserve sperrte auch Laden (EMS-Ruhezustand-Fehler).
- 🧱 **Stabilität:** Speichern-Schaltflächen in aufklappbaren Bereichen funktionieren nun auch unter iOS Safari zuverlässig.
- ✨ **Verbesserung:** Speicherregelung - "Über Kurve" vernichtete PV-Energie am MPPT.
- ⚙️ **Regelung:** E3DC-Automatik + berechneter Ladebedarf in Watt E3DC nimmt berechneter Ladebedarf in Watt als Basis-Sollwert aus PV.
- ✨ **Verbesserung:** Zusätzlich absorbiert E3DC automatisch den PV-Überschuss über Haus+Netz-Limit.
- 🛡️ **Sicherheit:** Eine bewusst gestartete Speicherentladung wird nur bei bestätigter E3DC-Abregelung beendet; ein bloßer Hinweis auf hohe PV-Auslastung erzeugt lediglich eine Warnung.
- ✨ **Verbesserung:** PV minus Haus in Watt als korrektes Gesamt-Leistungsbudget.
- ✨ **Verbesserung:** Speicher Automatik Entladung PV Schutz in Watt (Standard: 500W).
- ✨ **Verbesserung:** Geplanter Eco-Dump (aktiv Speicherplatzschaffen in Watt aus Speicherplan).
- 🐛 **Fehlerbehebung:** Speicherregelung: Speicherladung ersetzt E3DC-Automatik + Leistungsbegrenzung bei Über-Kurve.
- 🐛 **Fehlerbehebung:** Speicherregelung: Wolken-Erkennung bei Über-Kurve.
- ✨ **Verbesserung:** Wärmepumpendaten (geschrieben von Wärmepumpenanbindung) wurde nur für Wärmepumpentyp 2 gelesen, nie für Wärmepumpentyp 3.
- 🔎 **Diagnose:** Der Wärmepumpendaten-Überschreibe-Block lief auch für Wärmepumpentyp 3 und hat den bereits korrekt gesetzten Wert auf 0W zurückgesetzt (erkennbar an "Quelle: Wärmepumpendaten" in der Diagnose-Seite trotz Wärmepumpentyp 3)..
- 🐛 **Fehlerbehebung:** Speicherregelung: noon Ziel SoC wird nach dem ersten Tageslauf eingefroren.
- 🐛 **Fehlerbehebung:** Speicherregelung: Post-Dump-Anker verhindert aggressives Nachladen.
- ✨ **Verbesserung:** Speicherplan: noon Ziel SoC wird im Plan exportiert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** ELWA: bis zum nächsten internen 60s-Zeitüberschreitung.
- ✨ **Verbesserung:** Generisch/Shelly: theoretisch unbegrenzt Korrektur: Bei Automatik Modus inaktiv wird jetzt sofort 0W geschrieben (Heizstab-Modbus) und der Shelly explizit auf AUS gesetzt.



### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Hidden-Input name="save all" als Rückfall.
- 🐛 **Fehlerbehebung:** Speicherregelung: today Plan Zeitfenster Rückfall (ohne zeitlicher Sollverlauf) nutzte simulierte SOC-Werte als Zielkurve.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Grundlast-Filter für Shelly Pro 3 em (Unterverteilung).
- ✨ **Verbesserung:** Die direkte Shelly-Abfrage verwendet die für Shelly Pro 3EM konfigurierte Geräteadresse.
- ✨ **Verbesserung:** Daten fließen automatisch in Echtzeitdaten und ML-Lernprozess ein.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Weboberfläche - Docker Hot-Start via pkill + nohup.

## [4.4.6] – 2026-04-26

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicherregelung: vorgelagerte Entladung verhindert PV-Abregelung bei großen Anlagen.
- ✨ **Verbesserung:** Neuer Ansatz — Hardware-Realitäts-Simulation.
- ✨ **Verbesserung:** HW-Simulation: Simuliert was die E3DC-Firmware wirklich tut — lädt ungekürzt bis SoC 100% (kein Ziel-SoC-Obergrenze), mit konservativem Mindest-Eigenverbrauch 300W statt ML-Schätzung.
- ⚙️ **Regelung:** Vorgelagerte Entladung Ramp: Plant automatisch Entladung (Zeitfenster zwischen jetzt und Speicher full Zeitpunkt). Progressives Leistungsprofil: Früh morgens maximale Entladeleistung (bis 4500W), linear abnehmend auf 1500W nahe dem PV-Peak.
- ✨ **Verbesserung:** Konfigurierbarer Min-SoC: ökonomisch Speicherplatzschaffen minimal SoC (Standard 10%) schützt die Batterie vor Tiefentladung.
- 🐛 **Fehlerbehebung:** Speicherregelung: Kurvenstart nach Eco-Dump auf Post-Dump-SoC gesetzt.
- ✨ **Verbesserung:** Root-Cause (zweistufig).
- ✨ **Verbesserung:** Can reach Ziel = (maximal SoC today >= target 0.95) prüfte das Ergebnis der gedrosselten Simulation statt die physikalische Machbarkeit.
- ✨ **Verbesserung:** Minimal Ladung in Watt: Garantiert dass required in Wattstunden in den verfügbaren Zeitfenster physikalisch erreichbar ist (dynamisch nachgeführt).

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Spalte 1 (links, flex-grow).
- ✨ **Verbesserung:** Spalte 3 (rechts).

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Speicherregelung: Eco-Dump hat Vorrang vor Kurven-Logik.
- 🐛 **Fehlerbehebung:** Speicherregelung: Hysterese-Zone sperrt nie die Entladung.
- ✨ **Verbesserung:** Prognose- und Preislogik: Phase A — Horizont-abhängige Modellgewichtung (IEA Task 16 Standard).
- ✨ **Verbesserung:** Kurzfristige Prognosen gewichten aktuelle Wettermodelle stärker als längerfristige Vorhersagen.
- ✨ **Verbesserung:** 24–48h (Mittelfrist): Gleitender Übergang M1↔M2.
- ✨ **Verbesserung:** Prognose- und Preislogik: Phase B — Wetterklassen-Gewichtung (basiert auf M2-Strahlungsdaten).
- ✨ **Verbesserung:** Klar (>300 W/m²): M1 +25% Bonus — Dachgeometrie (Neigung/Azimuth) zahlt sich bei klarem Himmel aus.
- ✨ **Verbesserung:** Mischbewölkt (80–300 W/m²): neutral.
- ✨ **Verbesserung:** Bedeckt/Nebel (<80 W/m²): M2 +30% Bonus — NWP-Diffusstrahlung-Modell ist hier überlegen.
- ✨ **Verbesserung:** Prognose- und Preislogik: Effektive Gewichte im Systemprotokoll.
- 🐛 **Fehlerbehebung:** Speicherregelung: can reach Ziel falsch negativ bei großem PV-Überschuss (>50 kWh).
- 🐛 **Fehlerbehebung:** Prognose- und Preislogik: Ensemble-Gewichtung verlor 40% des Ertrags wenn Solcast (M3) nicht konfiguriert.
- 🐛 **Fehlerbehebung:** Prognose- und Preislogik: Selbstlernender Weight-Updater war seit Verlauf-Buffer-Einführung blind (pred in Kilowattstunden 0.000).
- ✨ **Verbesserung:** Prognose- und Preislogik: Selbstlernendes EWMA-Korrekturfaktor-System (IEA Task 16 Standard).
- ✨ **Verbesserung:** Clearsky-Klassifizierung.
- 🔎 **Diagnose:** Prognose- und Preisdaten migriert auf Version 2 mit neuem Schema: daily Systemprotokoll (90-Tage-Protokoll pro Tag mit actual kwhforecast in Kilowattstunden, Korrekturfaktor rawclearsky class, quarter), seasonal Korrekturfaktor (Q1–Q4 EWMA-Faktoren).
- 🐛 **Fehlerbehebung:** Prognose- und Preislogik: Doppelter Berechnung-Aufruf entfernt.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Eine kurze, sauber abbrechbare Anlaufverzögerung verhindert Zugriffe, bevor die benötigten Dienste bereit sind.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Weboberfläche: Frequenz-Labels im Netzgesundheits-Dialog waren falsch ausgerichtet.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Installations- und Aktualisierungsablauf.

## [4.4.1] – 2026-04-25

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Beim Erreichen des Ziel-SoC wird eine zulässige Ladegrenze statt einer unbeabsichtigten Null-Watt-Vorgabe verwendet.
- 🐛 **Fehlerbehebung:** Die Ladekurve beginnt nicht mehr unterhalb des konfigurierten morgendlichen Mindest-SoC.
- 🐛 **Fehlerbehebung:** Netzbezug wird auch nahe der Sollkurve korrekt ausgeglichen.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Eine aktive Wallboxladung schützt die Hausbatterie vor unbeabsichtigter Entladung.
- 🧱 **Stabilität:** Physikbasierte Überschussregelung und Hysteresen reduzieren unnötige Schaltvorgänge.
- ✨ **Verbesserung:** Die Grundlagen für bidirektionale Wallboxen wurden in der Bedienung und Regelarchitektur berücksichtigt, ohne eine allgemeine Freigabe vorzutäuschen.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Im normalen PV-Betrieb begrenzt das System die Ladeleistung, während das E3DC die Entladung für Hausverbraucher weiterhin autonom regelt.
- 🛡️ **Sicherheit:** Aktive Speicherladung aus dem Netz bleibt im PV-Betrieb gesperrt; preisgesteuerte Ausnahmen benötigen eine ausdrückliche Freigabe.
- 🐛 **Fehlerbehebung:** Die Lernfunktion berücksichtigt wieder kurze reale Lastspitzen und skaliert Tagesziele mit der nutzbaren Speicherkapazität.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Das gemeinsame Leistungsbudget für Wallbox und Wärmepumpe berücksichtigt die vom Batteriesystem aktuell erlaubte Ladeleistung.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Der Forum-Link führt wieder zum aktuellen Diskussionsstand.
- 🐛 **Fehlerbehebung:** Eine Warnung des Systemwächters verschwindet nach nachweislicher Erholung des Speicherdienstes.

### 📦 Distribution und Kompatibilität

- 🧱 **Stabilität:** Docker-Aktualisierung und Startreihenfolge wurden korrigiert, damit Echtzeitwerte vor den darauf aufbauenden Diensten bereitstehen.

## [4.1.0] – 2026-04-23

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Sonnenmodus als Basis (Gerätefreigabe[0]=1).
- ✨ **Verbesserung:** Delta ∈ [-band dn ...
- 🧱 **Stabilität:** 6 Modi mit klaren Hysterese-Bändern.
- ✨ **Verbesserung:** 6A Grundlast nur aktiv wenn can reach Ziel aktiv (Speicher Plan Prognose).
- ⚙️ **Regelung:** Schont den Schütz: E3DC stoppt bei 6A-Deckel autonom, kein aktuelle Regelung Umschaltbefehle.
- ✨ **Verbesserung:** Gradueller Moduswechsel.
- ✨ **Verbesserung:** Phasenerkennung automatisch.
- ✨ **Verbesserung:** Ökobewertung konfigurierbar (ökonomische Netzbewertung, ökonomische PV-Bewertung).
- ⚙️ **Regelung:** Haus-Priorität (Wallbox-Mindest-SoC).

## [4.0.28] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Die Netzbewertung verwendet wieder den unveränderten Messwert; eine längere Mindesthaltezeit begrenzt erneute Leistungsänderungen nach dem Anlaufen.

## [4.0.27] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Die Ladeleistung steigt langsamer an, sodass das E3DC-Energiemanagement zwischen zwei Erhöhungen ausreichend Zeit zum Einregeln erhält.

## [4.0.26] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Nach einer Beruhigungsphase wird die Ladeleistung nur begrenzt angehoben und startet abhängig vom vorherigen Zustand konservativ.

## [4.0.25] – 2026-04-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Die Wallboxregelung trennt die eigene Ladeleistung von der Netzbewertung und erhöht den Strom langsamer, damit kein selbstverstärkender Regelkreis entsteht.

## [4.0.24] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Eine Anlaufphase, eine feste Ruhezeit und die Erkennung schneller Stopps geben dem E3DC-Energiemanagement Zeit zum Einpendeln und verringern Takten.

## [4.0.23] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Ladevorgänge beginnen kontrolliert mit dem Mindeststrom, während geglättete Speicherwerte und ein klar begrenzter Tagesverlauf unnötige Wechsel reduzieren.

## [4.0.22] – 2026-04-23

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Die Heizstabregelung liest die aktuelle Konfiguration und kann ältere Einstellungen weiterhin übernehmen; der zugehörige Dienst wird bei der Installation aktiviert und gestartet.

## [4.0.21] – 2026-04-23

### 🔋 Storage Manager

- ⚙️ **Regelung:** Ein zusätzliches Speicherziel für die Mittagszeit ermöglicht eine zweiteilige Ladekurve und hält am Nachmittag gezielt Kapazität für PV-Energie frei.

## [4.0.20] – 2026-04-23

### 🔋 Storage Manager

- ✨ **Verbesserung:** SoC-Kurve im Weboberfläche fehlt komplett wenn ML-Prognosedaten nicht vorhanden (Speicherregelung).

## [4.0.19] – 2026-04-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Fehlt im Speicherplan die freie Leistung für steuerbare Verbraucher, verwendet die Wallboxregelung einen sicheren Zahlenwert statt eines leeren Zustands; Ladevorgänge können dadurch wieder starten.

## [4.0.18] – 2026-04-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Die Ruhephase der Wallbox beendet sich nicht mehr selbst; wiederkehrendes Ein- und Ausschalten wird verhindert.

## [4.0.17] – 2026-04-23

### 🔋 Storage Manager

- 🔎 **Diagnose:** Diagnostik car inaktiv im Systemprotokoll erklärt: Entsteht beim Moduswechsel wenn RSCP-Status nach Übernahme der Laderegelung noch nicht synchron ist.

## [4.0.16] – 2026-04-23

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Dokumentiert in Wallbox-Betriebsdaten.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Nach der Übergabe an die E3DC-Automatik bleibt der im Portal gewählte Wallboxstrom erhalten und wird nicht auf 6 A zurückgesetzt.

## [4.0.15] – 2026-04-23

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox-Prioritätsstufen Modus 4-9 jetzt linear & konsistent (Speicherregelung, Wallbox-Anbindung).
- ✨ **Verbesserung:** Wallbox-Anbindung.

## [4.0.14] – 2026-04-23

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Soll-Kurve (blau) im Ladekurven-Diagramm immer leer (Speicherregelung).
- ✨ **Verbesserung:** Fehlende Meta-Felder im Speicherplan.

## [4.0.13] – 2026-04-23

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** Batterie lädt trotz Speicherladung-Befehl nicht wenn Wallbox-Priorität aktiv (Speicherregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Alle Einstellungen der PV-Mindestladung sind in Konfigurationsoberfläche und Bereinigung vollständig registriert und gehen bei Aktualisierungen nicht verloren.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Ladekurven-Kachel klickbar mit Weboberfläche Dialog: Zeigt Soll-Kurve (blau gestrichelt), IST-SoC aus Echtzeit-Verlauf (grün) und PV-Prognose (orange, sekundäre Achse) mit "Jetzt"-Linie.

## [4.0.12] – 2026-04-23

### 🔋 Storage Manager

- 🧱 **Stabilität:** Hysterese war symmetrisch ±3% — Batterie lädt trotz starkem PV-Überschuss nicht (Speicherregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** PV-Überschuss-Untergrenze jetzt standardmäßig aktiv (Speicherregelung).

## [4.0.11] – 2026-04-23

### 🔋 Storage Manager

- ✨ **Verbesserung:** Ladekurve bleibt nach Neustart starr auf aktuellem SoC eingefroren (Speicherregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** PV-Überschuss-Untergrenze bei „Kurve getroffen" (Speicherregelung).

## [4.0.10] – 2026-04-23

### 🔋 Storage Manager

- ✨ **Verbesserung:** RSCP Echtzeitdaten Stabilität.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox "Brain-Begrenzung" (Wolken-Glätter).

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Leere numerische Konfigurationsfelder führen nicht mehr zu wiederkehrenden Neustarts der Wallboxregelung.
- 🧱 **Stabilität:** Speicherregelung (Inselnetz-Flattern).
- 📊 **Anzeige/Auswertung:** Web-Oberfläche Konfigurationsseite.
- ✨ **Verbesserung:** Dynamische Konfigurationsbereinigung.
- ✨ **Verbesserung:** Speicherplanung (V4 Crash Korrektur).
- ✨ **Verbesserung:** Systempakete & Debian Trixie.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** EPEX „Keine Zeitfenster" nach Dienst-Neustart.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** V4 Update Berechtigungen.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Modbus Abhängigkeiten.

## [4.0.5] – 2026-04-22

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Ein Syntaxfehler in den Regeln für administrative Installationsaufgaben wurde behoben.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Webportal-Installation.
- 🐛 **Fehlerbehebung:** Installationsassistent-Crash behoben.

## [4.0.4] – 2026-04-22

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Release & Docker-Build.

## [4.0.3] – 2026-04-22

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Speicherregelung (Ladelogik 0W-Limit).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche "Dienste Neustarten" Schaltflächen.

### 📦 Distribution und Kompatibilität

- 🛡️ **Sicherheit:** Watchdog (Pi Schutz) Docker-Bug behoben.
- ✨ **Verbesserung:** Robustere Port-Konfiguration.

## [4.0.1] – 2026-04-21

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Benötigte Hintergrunddienste werden bei der Einrichtung automatisch aktiviert.
- 🐛 **Fehlerbehebung:** Die Regeln für administrative Installationsaufgaben wurden korrigiert.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Rsync für Web-Oberfläche.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent Git Phantom-Bug.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** V3 zu V4 Konfigurations-Migration.

## [3.9.6.2] – 2026-04-18

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** Direkt angebunden openWB Steuerung (Wallboxregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** V4 Autonomie (kWh-Retter).
- 📊 **Anzeige/Auswertung:** Statistik Hangover.

## [3.9.6.1] – 2026-04-17

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** KI-gestützte Wallbox-Logik & V4-Speichermanagement.
- 📊 **Anzeige/Auswertung:** Oberfläche/Bedienung Fehlerbehebung.
- ✨ **Verbesserung:** Architektur Fehlerbehebung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Logik Fehlerbehebung (Ladeplanung).

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Direktvermarktung & Spotmarkt-Integration.

## [3.9.6] – 2026-04-17

### 🔋 Storage Manager

- ✨ **Verbesserung:** Fundamentale Designkorrektur Wallbox-Mindest-SoC vs. dynamischer Mindest-SoC.
- ✨ **Verbesserung:** Wallbox Mindest So c hartes absolutes Untergrenze (nur noch für Modus 10 „Sofort immer" wirksam).
- ✨ **Verbesserung:** Wallbox-Speicherziel (Konfiguration, Standard 90%) = Abend-Ziel kurz vor Sonnenuntergang.
- ✨ **Verbesserung:** Dynamischer Mindest So c laufend berechneter Schutzwall basierend auf PV-Prognose: Wie viel muss die Batterie JETZT mindestens haben, damit das 90%-Ziel abends noch erreichbar ist?
- ✨ **Verbesserung:** Prognose-Integration.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Bidirektionale Wallboxen (V2G / V2H).
- ✨ **Verbesserung:** Kaskaden-Timer für PV-Leistungsanhebung (Trägheitssimulation).
- ✨ **Verbesserung:** Wechselrichter AC-Limit (erzwungene DC-Ladung).
- 🧱 **Stabilität:** Wolken-Hysterese (Tick-Tack Puffer).
- 🛡️ **Sicherheit:** Sonnenmodus Stop-Schutz (22kW-Flash Korrektur).
- 🛡️ **Sicherheit:** 6A Selbstlern-Schutz (Wallbox minimal Leistung meas).
- 📊 **Anzeige/Auswertung:** RSCP-0W Messwertsprünge Filter (Anzeige-Flackern).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Eigenständiger Speicherplanung (V4).
- ✨ **Verbesserung:** ML-Ausbau & Selbstlernende Prognose.
- ✨ **Verbesserung:** Erweiterter Eco-Bewertung – Netzbewusstes Laden & Entladen.
- ✨ **Verbesserung:** Systemautonomie & Resilienz (Inselnetz).
- ✨ **Verbesserung:** Dynamische Lastverschiebung (Haushaltsgeräte).
- ✨ **Verbesserung:** DC/DC-Wandler am DC-Bus (Topologie-Korrektur).
- ✨ **Verbesserung:** Ext PV korrekt von erzwungene DC-Ladung getrennt.
- ✨ **Verbesserung:** Systemprotokoll-Verbesserung.
- ✨ **Verbesserung:** Zeitfenster-genaue Temperaturprognose.
- ✨ **Verbesserung:** Temperaturquelle gehärtet.
- ✨ **Verbesserung:** Prognose- und Preislogik.
- ✨ **Verbesserung:** Plausibilitätsprüfung des Hausverbrauchs bei der Tagesarchivierung.
- 🔎 **Diagnose:** Ein Korrekturwerkzeug bereinigt fehlerhafte historische Energiewerte nachvollziehbar.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Systemprotokoll-Ebene des Safety-Checks korrigiert.
- ✨ **Verbesserung:** Zirkulations-Systemprotokoll.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Direktvermarktung & Spotmarkt-Integration.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Externer Generator in Langzeitstatistik.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Autarkie-Rückfall.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Smart-Home Evolution (Matter & Push).

### 📚 Dokumentation

- ✨ **Verbesserung:** Wallbox-Betriebsdaten überarbeitet.
- ✨ **Verbesserung:** E3DC-Classic als eigenständiges Repo.

## [3.9.5.1] – 2026-04-17

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Netzbezug / Netzeinspeisung Energie-Tags vertauscht.
- ✨ **Verbesserung:** Neues Feature: wurzelzähler invertiert (Konfiguration).

## [3.9.5] – 2026-04-16

### 🔋 Storage Manager

- ✨ **Verbesserung:** Erklärung der Zell-Grenzwerte.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Entkopplung der Wallbox-Ladeplanung vom Speicher.
- ✨ **Verbesserung:** Online-Dienst-Integrations-Indikator.
- ✨ **Verbesserung:** Leere "0W" Ghost-Readings.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Problem "Mehrere SHI Schreibquellen" (Error 816) behoben.
- ✨ **Verbesserung:** Rückkanal-Validierung.

## [3.9.4] – 2026-04-15

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die RSCP-Anmeldung verarbeitet Kennwortangaben wieder zuverlässig.
- ✨ **Verbesserung:** Echtzeitdaten Integration.
- ✨ **Verbesserung:** Tagesertrag Historie.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Prognose- und Preislogik — Ladeplan-Erzeugung mit Battery-Care AI, Ladeplanstatus.
- ✨ **Verbesserung:** Regelungslogik — Leistungsverteilung Leistungsverteilung.
- ✨ **Verbesserung:** Keine Verhaltensänderung.
- ✨ **Verbesserung:** Echter originaler Messzeitstempel aus openWB MQTT-Betriebsdaten, kein aktuelle Zeit.
- ✨ **Verbesserung:** Neuer abort Laden Verarbeitung (sanft).
- ⚙️ **Regelung:** Abort all Laden bleibt als NOT-AUS (physische Sperre, Wallbox 1 locked 1, Regelung-Neustart).
- 📊 **Anzeige/Auswertung:** Oberfläche neu gestaltet.
- 📊 **Anzeige/Auswertung:** Beschreibungstext erklärt den Unterschied der beiden Aktionen direkt in der Oberfläche.
- ✨ **Verbesserung:** Umlaut-Korrektur in Wallbox-Anbindung.
- ✨ **Verbesserung:** PV-Modussteuerung in openWB-Anbindung.
- 🐛 **Fehlerbehebung:** Batterie-Ruhezustand-Bug behoben (Netz-Zwangsladen unterdrückt).
- ✨ **Verbesserung:** Systemprotokoll-Transparenz.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zwei-Phasen Ladestrategie.
- ✨ **Verbesserung:** Phase 1 (Bulk, ~85%).
- ✨ **Verbesserung:** Phase 2 (Pre-Conditioning, ~15%).
- ✨ **Verbesserung:** Just-In-Time Bewertung.
- ✨ **Verbesserung:** 15-Minuten Raster.
- 🔄 **Migration/Kompatibilität:** SoC-Rückfall-Kette.
- 🐛 **Fehlerbehebung:** SoC-Warnung Spam behoben.
- 📊 **Anzeige/Auswertung:** Web-Oberfläche "Verbindungsfehler" bei Vitaldaten behoben.
- 🐛 **Fehlerbehebung:** Falscher Hausverbrauch/Netzbezug behoben.
- ⚙️ **Regelung:** E3DC weather-Regelung Crash nach erstem Zyklus behoben.



### 📦 Distribution und Kompatibilität

- ⚙️ **Regelung:** Docker Installations- und Aktualisierungsablauf — Weather-Regelung lief nie.

## [3.9.3] – 2026-04-14

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** Protokoll-Refactoring Wallbox-Anbindung.
- ✨ **Verbesserung:** 2-Phasen Sofortladen.
- ✨ **Verbesserung:** Echtzeitdaten Verarbeitung.
- ✨ **Verbesserung:** Befehl-Proxy Wallbox-Bedienung.
- 🔎 **Diagnose:** openWB Echtzeit-Status Karte.
- ✨ **Verbesserung:** Vollständiger heller Modus.
- ✨ **Verbesserung:** Quick-Control Schaltflächen.
- ✨ **Verbesserung:** Statusanzeige-Blinken Korrektur.
- ✨ **Verbesserung:** Sofortiger Erst-Fetch.
- ✨ **Verbesserung:** Phase-Switching Statusanzeige.
- 🔄 **Migration/Kompatibilität:** SoC-Rückfall-Kette.
- ✨ **Verbesserung:** 15-Min-Granularität.
- 🔎 **Diagnose:** Energieplanung Status.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche obsolet.

## [3.9.2] – 2026-04-13

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Eigene Fahrzeug-Vorlagen (Offline / Gast-Fahrzeuge).
- ✨ **Verbesserung:** KI-Hausverbrauch Wallbox-Filter (ML-Prognose).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** V4 Speicherplanung (Speicherplanung).
- ✨ **Verbesserung:** V4 PV-Prognose Ensemble Korrektur (48h/72h Interpolation).
- ✨ **Verbesserung:** Korrektur der PV-Prognose (Summierung).
- ✨ **Verbesserung:** Systemprotokoll-Spam Wallboxregelung.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Vollständige Installationsassistent-Integration.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Docker V4 Kompatibilität.

## [3.9.1] – 2026-04-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** 15-Minuten SMARD Intraday-Preise.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Kopfzeile-Navigation.
- ✨ **Verbesserung:** Netzbezug Layout-Stabilität.
- ✨ **Verbesserung:** Spotmarkt Börsenstrompreis Flatline-Korrektur.

### 🛠️ Installation und Update

- 📊 **Anzeige/Auswertung:** Oberfläche-Rückfall bei Dienst-Neustart.

## [3.9.0] – 2026-04-12

### 🔋 Storage Manager

- ✨ **Verbesserung:** Historische PV-Erträge werden mit dem vom E3DC erwarteten Zeitformat abgefragt und aus der verlässlichen Gleichstrom-Erzeugungsmessung gebildet.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox Datenbank-Repair.
- ✨ **Verbesserung:** Absolute Zähler für Tages-Schritte.
- ✨ **Verbesserung:** Der Systemwächter arbeitet stabiler.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Gehirn als Single-Source-of-Truth.
- ✨ **Verbesserung:** Lückenlose SMARD-Daten.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Hybrid-Diagramm Preisschichten.
- ✨ **Verbesserung:** IOS Layout-Korrekturen.
- ✨ **Verbesserung:** Preis-Ticket Korrektur.
- ✨ **Verbesserung:** Speichern-Schaltflächen Überlappung.
- ✨ **Verbesserung:** Mobilansicht-Navbar Color-Coding.

## [3.8.9.2] – 2026-04-11

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** RSCP Auto-Unlock (Release-Kommando).
- ✨ **Verbesserung:** PV-Überschuss Zwangssperren-Ausnahme.
- ✨ **Verbesserung:** Smarter Automatikmodus (Netz).
- 🧱 **Stabilität:** Batterie Virtual-Kickstart & Hysterese.
- 🐛 **Fehlerbehebung:** Neustart und Rechteverwaltung der Hintergrunddienste wurden korrigiert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Zirkulations-Bugfix.

## [3.8.9.1] – 2026-04-10

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Echtzeit-Controls für Beide Wallboxen.
- ✨ **Verbesserung:** DYNAMISCHES UMBENENNEN.
- ✨ **Verbesserung:** Intelligente Sichtbarkeit.
- 🛡️ **Sicherheit:** Physische Wallboxsperre: Eine über die Weboberfläche gesperrte Wallbox wird mit dem offiziellen E3DC-Stoppbefehl abgeschaltet; die Schütze öffnen hörbar.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wolken-Glätter (Min+PV).

## [3.8.9] – 2026-04-10

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Rechtekorrekturen zeigen nur tatsächlich ausgeführte Änderungen und können einen erforderlichen Neustart kontrolliert auslösen.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Konfigurationsseite, Speicherstatus, Hinweistexte und Mobilansicht Wallbox-Schieberegler wurden übersichtlicher gestaltet.

## [3.8.8.12.6] – 2026-04-09

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Installationsassistent Deadlock.
- ✨ **Verbesserung:** Direkt angebundene Wallbox Statusanzeige.

## [3.8.8.12.5] – 2026-04-09

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Die MQTT-Abfrage von Wallboxdaten ist mit aktuellen Mosquitto-Versionen kompatibel; ein Verbindungsabbruch blockiert die Hauptregelung nicht mehr.

## [3.8.8.12.4] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Batterie-Entladestop.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfigurationsseite Optimierung.
- ✨ **Verbesserung:** Empty-Field Handling.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker-Dienst Detection.

## [3.8.8.12.3] – 2026-04-09

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Hausverbrauch-Korrektur.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** MQTT Hub Multi-Broker SoC.

## [3.8.8.12.2] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB 2.0 MQTT-Kanal-Automatik.
- ✨ **Verbesserung:** Dummy-Wallbox Visibilität.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Persistenz-Korrektur (Weboberfläche Abgleich).
- ✨ **Verbesserung:** Wallbox Amperage Float-Korrektur.
- ✨ **Verbesserung:** Bare-Metal Schreibrechte.

## [3.8.8.12.1] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB / MQTT direkt angebunden Unterstützung.
- 📊 **Anzeige/Auswertung:** Web-Oberfläche Konfiguration.

## [3.8.8.12] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Dual-Charger Unterstützung.
- ✨ **Verbesserung:** Echtzeit-Phasenerkennung.
- 📊 **Anzeige/Auswertung:** Echtzeit Oberfläche Telemetrie.
- ✨ **Verbesserung:** Betriebssicherheit.

## [3.8.8.11] – 2026-04-09

### 🔋 Storage Manager

- ✨ **Verbesserung:** Historischer historische Energiemessung Zähler (Gesamt gerettet).

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Warmwasser Sollwert Korrektur.
- ⚙️ **Regelung:** iDM Vorlauf-Regelung für Notabbruch.
- ✨ **Verbesserung:** Erweiterte Speicherplanung vs. Sperrzeiten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wärmepumpen Layout-Vereinheitlichung.
- 📊 **Anzeige/Auswertung:** iDM Modbus-Werte & Oberfläche.
- ✨ **Verbesserung:** Außentemperatur Main-Weboberfläche.

## [3.8.8.10.6] – 2026-04-08

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Modbus Kollisionsverbot (iDM & Energiemanagement).

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Historische Energiemessung Rückfall-Sicherung.

## [3.8.8.10.5] – 2026-04-08

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Exakte Tageszähler (Anti-Drift).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** IOS PWA Monatswahl.

## [3.8.8.10.4] – 2026-04-08

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** BOM-Fehler bei E3DC-Abfrage behoben.
- 🐛 **Fehlerbehebung:** Permission Error auf manuell Leistungsanhebung Statusangaben behoben.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Einstellungen werden im laufenden manuellen Leistungsanhebung sofort übernommen.
- ✨ **Verbesserung:** WW-Sperrzeit-Ausnahme (experimentell).
- ⚙️ **Regelung:** Wärmepumpe data an alle WW-aktivierenden Leistungsanhebung-Steuerung-Aufrufe übergeben.
- 🔎 **Diagnose:** iDM Externe Anforderungen sichtbar.
- 🔎 **Diagnose:** iDM Status-Felder vollständig aus aktuellen Betriebsdaten.
- ✨ **Verbesserung:** Tages-AZ nutzt iDM-eigene kumulative Zähler.
- 📊 **Anzeige/Auswertung:** Optisches Oberfläche-Upgrade (Sensoren).
- ✨ **Verbesserung:** iDM Manueller Leistungsanhebung startet jetzt sofort.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** ASCII-Kompatibilität für Pi-Terminal.

## [3.8.8.10.2] – 2026-04-07

### 🔋 Storage Manager

- ✨ **Verbesserung:** Batterie-Gradient (Energiefluss).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Symmetrische Zeitachse.
- ✨ **Verbesserung:** Gleichmäßige Datenpunktdichte.
- ✨ **Verbesserung:** Viertelstunden-X-Achse.
- ✨ **Verbesserung:** Mitternachts-Überlauf-Korrektur (48h).
- ✨ **Verbesserung:** Mobilansicht Farbschema-Korrektur.
- ✨ **Verbesserung:** Hilfe-Seite Farbschema-Synchronisation.

## [3.8.8.10.1] – 2026-04-07

### 🔋 Storage Manager

- ✨ **Verbesserung:** Batteriedrift (Zellspannung) RSCP Korrektur.

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Web-Oberfläche "Geister-Updates" behoben.

## [3.8.8.10] – 2026-04-07

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Systemprotokoll-Spam Eliminierung.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Kühlung für Kältespeicher (Register 1711 & 1010).
- 🧱 **Stabilität:** Umgekehrte Hysterese & Konfigurierbarkeit.
- ✨ **Verbesserung:** Manueller Sommer-Leistungsanhebung.
- 🔄 **Migration/Kompatibilität:** Smart Netz Vollgas (1006 = 2) Update.

## [3.8.8.9] – 2026-04-07

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Historische Energiemessungen für Peak Shaving bleiben bei Aktualisierungen erhalten.
- 🔎 **Diagnose:** Fehlen einzelne Zelltemperaturen oder Ladezyklen, nutzt die Batterieansicht belastbare Werte des Batterieverbunds.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Prognoseszenarien, dynamische Lastverschiebung und die Auswertung vermiedener Lastspitzen wurden erweitert.
- 🛡️ **Sicherheit:** Inselbetrieb und autonome Rückfallfunktionen bleiben gegenüber Komfortoptimierungen vorrangig.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Unterbrochene Modbus-Verbindungen werden erkannt und sauber neu aufgebaut.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Matter- und Push-Anbindungen wurden als optionale Smart-Home-Schnittstellen erweitert.

## [3.8.8.8.2] – 2026-04-06

### 🔋 Storage Manager

- ✨ **Verbesserung:** Verbesserte Triggersicherheit.
- 📊 **Anzeige/Auswertung:** Prozess-Visualisierung (Oberfläche).
- ✨ **Verbesserung:** Automatische Kollisionsvermeidung.
- ✨ **Verbesserung:** iDM Wärmepumpen Integration.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Bessere Fehlermeldung.

## [3.8.8.8] – 2026-04-06

### 🔋 Storage Manager

- ✨ **Verbesserung:** Die Übersicht zeigt die insgesamt vermiedenen Lastspitzen.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Hausverbrauch-Bereinigung.

### ♨️ Wärmepumpe und Wärme

- 📊 **Anzeige/Auswertung:** Modbus Vorgaben-Oberfläche.
- ✨ **Verbesserung:** Vorlauf-Korrektur.
- ✨ **Verbesserung:** Sommer/Winter Indikator.
- ✨ **Verbesserung:** Dynamischer Leistungsanhebung-Change.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Historische Energiemessung.
- 📊 **Anzeige/Auswertung:** Lade-Fahrplan (Regler) Anzeige.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Doppelter Menüpunkt behoben.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker: Web-Push (pywebpush) Korrektur.
- ✨ **Verbesserung:** Die Echtzeitverarbeitung erkennt Docker-Installationen zuverlässiger.

## [3.8.8.7] – 2026-04-05

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Menüpunkt 1 wiederhergestellt.
- ✨ **Verbesserung:** RSCP-Diagnosewerkzeug Erstinstallation über Menü.

## [3.8.8.6] – 2026-04-05

### 🔋 Storage Manager

- ✨ **Verbesserung:** Neues Vitals-Weboberfläche ("Batterie-Gesundheit").
- ✨ **Verbesserung:** Zustand of Health (SOH) pro Pack.
- ✨ **Verbesserung:** Zell-Temperatur (Min/Max).
- ✨ **Verbesserung:** Zell-Drift (Spannungs-Spread).
- ✨ **Verbesserung:** Firmware & Software-Version.
- ✨ **Verbesserung:** Automatische Filterung (Geister-Packs).
- ✨ **Verbesserung:** RSCP-Diagnosewerkzeug Automatisch-Installation.
- 🔄 **Migration/Kompatibilität:** E3DC Firmware-Kompatibilität.
- ✨ **Verbesserung:** E3DC Firmware-Bug-Filter.
- ✨ **Verbesserung:** Drittanbieter-Abhängigkeit.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Rechte-Integration.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Vitals-Ansicht.
- ✨ **Verbesserung:** Kachel-Stabilisierung (Mobilansicht).

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Update-Regelung Integration.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Konfigurierbare Fahrzeugnamen im MQTT-Hub.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker-Unterstützung.

## [3.8.8.5] – 2026-04-05

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Finale 3-Schalter-Logik.
- ✨ **Verbesserung:** Lösung des Umbenennungs-Problems.
- ✨ **Verbesserung:** Legalität des Control Nodes.

## [3.8.8.4] – 2026-04-05

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Hardware vs. Software Fail-Safe.
- ✨ **Verbesserung:** Grundlast-Filter (Noise Elimination).

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Echtzeit-Anzeige JIT-Extraktion.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Shelly Pro 3EM Schnittstelle-Unterstützung (Gen2).

## [3.8.8.3] – 2026-04-04

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Der Neustart der Wärmepumpenanbindung aus der Weboberfläche wurde abgesichert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Laufende Verbindung Lock-Problem gelöst.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Matter Oberfläche-Anzeige.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Modbus TCP Korrektur ("Broken Pipe").
- ✨ **Verbesserung:** M dns Proxy (Avahi).
- ✨ **Verbesserung:** 3 Echtzeit-Schalter.
- ✨ **Verbesserung:** Wallbox: An wenn Fahrzeug lädt (>50W).
- ✨ **Verbesserung:** PV-Produktion: An wenn Solaranlage produziert (>500W).
- ✨ **Verbesserung:** Netz-Einspeisung: An wenn Strom-Überschuss ins Netz fließt (>200W).
- ✨ **Verbesserung:** Automations-Trigger.
- 🧱 **Stabilität:** LAN-Pairing stabil.
- ✨ **Verbesserung:** Dienstverwaltung-Abhängigkeit.
- ✨ **Verbesserung:** Installationsassistent-Prerequisite.
- ✨ **Verbesserung:** Weboberfläche-Integration.

## [3.8.8.2] – 2026-04-03

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Peak-Ersparnis (Kuppe).
- ✨ **Verbesserung:** Dynamisches Netzeinspeise-Limit.
- ✨ **Verbesserung:** Geisterlinien Korrektur.
- ✨ **Verbesserung:** Permanente Aktivierung.
- ✨ **Verbesserung:** Langzeit-Archiv (Langzeitdatenbank).

## [3.8.8.1] – 2026-04-03

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Die erzeugten Systemdienste verwenden eine zeitgemäße Protokollausgabe und vermeiden wiederkehrende Warnungen auf aktuellen Linux-Distributionen.
- 🔎 **Diagnose:** Der Echtzeitstatus bleibt bei parallelen Zugriffen zuverlässig verfügbar.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Die iDM-Wärmepumpe erhält den verfügbaren PV-Überschuss direkt und stufenlos über Modbus TCP. Begrenzte Aktualisierungsintervalle und ein Überhitzungsschutz verhindern unnötige Stellwertwechsel.
- 🐛 **Fehlerbehebung:** Nicht vorhandene Messgrößen einer Luft/Wasser-Wärmepumpe werden nicht mehr durch künstliche Platzhalterwerte ersetzt; Historie und Anzeige enthalten nur tatsächlich verfügbare Daten.
- 🔎 **Diagnose:** Der an die iDM-Wärmepumpe übertragene PV-Überschuss ist in der Wärmepumpenansicht in Echtzeit sichtbar.
- ✨ **Verbesserung:** Dynamische COP-Farben.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Echtzeit-Diagramm Rückfall.
- 📊 **Anzeige/Auswertung:** Dynamische Diagramm-Auflösung.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Virtual Device Node (Hintergrunddienst).
- ✨ **Verbesserung:** Sicherer Zertifikats-Speicher.
- ✨ **Verbesserung:** Hidden Weboberfläche Pairing-GUI.
- ✨ **Verbesserung:** Erfolgreicher Koppelungs-Durchbruch.

## [3.8.8] – 2026-04-02

### 📈 Direktvermarktung und Strompreise

- ⚙️ **Regelung:** Bei negativen Strompreisen kann eine ausdrücklich freigegebene automatische Ladung starten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Bedienaktionen und der Plug-and-Charge-Dialog wurden ergänzt.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Zeitüberschreitungen bei Shelly-Geräten werden sauber behandelt.

## [3.8.7.13] – 2026-04-02

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Notstrom & Inselbetrieb-Erkennung.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Fahrzeugwechsel SoC Interpolation Korrektur.
- ✨ **Verbesserung:** Intelligente Ladeplanung Ladezeit-Berechnung.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Granulare Event-Steuerung.
- ✨ **Verbesserung:** HA-Failover & Boot-Meldungen.
- ✨ **Verbesserung:** Git Berechtigungen (Rechte-Reparatur).
- ✨ **Verbesserung:** Prognose-Korrektur (Zeitzonen & 48h-Horizont).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wallbox GUI Layout.
- ✨ **Verbesserung:** Datenverlust beim Speichern.
- ✨ **Verbesserung:** Dynamischer Ladeplan-Zeitstrahl.
- 📊 **Anzeige/Auswertung:** Diagramm-Skalierung (Langzeit-Archiv).

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Smart-Update Benachrichtigung.
- 🔄 **Migration/Kompatibilität:** Installationsassistent automatische Aktualisierung optionale Aktivierung.

## [3.8.7.12] – 2026-04-02

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Leere SoC-Meldungen von EVCC beim Abstecken eines Fahrzeugs führen nicht mehr zum Abbruch des Energiemanagements.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Präziser Tagesstart (Midnight-Hangover Korrektur).
- ✨ **Verbesserung:** VAPID Security-Fundament.

### ♨️ Wärmepumpe und Wärme

- 📊 **Anzeige/Auswertung:** Heizstab Oberfläche-Rückfall.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Editor-Dialog Korrektur.
- ✨ **Verbesserung:** Wallbox-Ladehistorie Detailansicht.
- ✨ **Verbesserung:** Geräte-Registrierung via PWA.
- ✨ **Verbesserung:** Delta-Energiezählung.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Home Assistant Auth Unterstützung.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.8.7.11] – 2026-04-01

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Die Wallboxsteuerung verarbeitet Rückmeldungen in einer eindeutigen Reihenfolge.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Kein Datenverlust bei Updates.
- 🛡️ **Sicherheit:** Automatischer SD-Karten-Schutz.



### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Delta-Energiezählung.
- ✨ **Verbesserung:** HTML5 direkt angebunden Pickers.
- ✨ **Verbesserung:** Dynamische Daten-Limits (Min/Max).
- ✨ **Verbesserung:** Grenzenlos Analysieren.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.8.7.10] – 2026-03-31

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Modbus laufende Verbindung (Standleitung).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Korrektur für blockiertes Weboberfläche (Ladezeiten).
- ✨ **Verbesserung:** Offline-Caching & Micro-Timeouts.

## [3.8.7.9] – 2026-03-31

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Dynamisches Diagramm-Limit.
- ✨ **Verbesserung:** Korrektur Reference Error (Statische Strompreise).
- ✨ **Verbesserung:** Fehlende Prognosedaten erzeugen keine Warnung mehr in der Weboberfläche.
- ✨ **Verbesserung:** Echtzeitverbindung-Systemprotokoll Silence.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Betriebsarten-Zuordnungen (Wärmepumpen-Bedienung).
- ✨ **Verbesserung:** Reaktivierung der Detail-Kacheln.
- 🐛 **Fehlerbehebung:** Die Außentemperatur der Wärmepumpe wird trotz historisch unterschiedlicher Bezeichnungen korrekt angezeigt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Massiver Datenbank Speed-Up.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Keine redundanten Update-Abfragen.
- ✨ **Verbesserung:** Vollautomatische Rechteprüfung.

## [3.8.7.8] – 2026-03-31

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Statistik-Upgrade (Jahr/Monat Filter & Deep Drilldown).
- ✨ **Verbesserung:** Reboot-Datensicherheit.
- 📊 **Anzeige/Auswertung:** Korrektur Statistik-JS.
- ✨ **Verbesserung:** System-Stabilität.

### 📈 Direktvermarktung und Strompreise

- 📊 **Anzeige/Auswertung:** Präzise Strompreis-Anzeige.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Visualisierungs-Paket für Langzeit-Bilanzen.

## [3.8.7.7] – 2026-03-31

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zuluftregister (Adr 1060).
- ⚙️ **Regelung:** Aktive Überschuss-Steuerung (Adr 74/76).
- ✨ **Verbesserung:** Performance-Leistungsanhebung.
- ✨ **Verbesserung:** Präzise Tages-Arbeitszahl (AZ).
- 📊 **Anzeige/Auswertung:** Oberfläche-Erweiterung.
- 📊 **Anzeige/Auswertung:** Statistik Hardware-Abgleich.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Tiered Price Weboberfläche Unterstützung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Die Wallboxzuordnung wurde zuverlässiger.
- ✨ **Verbesserung:** Mobilansicht Optimierung (Heizstab).
- ✨ **Verbesserung:** Korrektur Wärmepumpen-Bedienung.
- ✨ **Verbesserung:** Wiederherstellung iDM-Navigation.
- ✨ **Verbesserung:** Heizstab-Visualisierung (Wattage).
- ✨ **Verbesserung:** Navigation-Unlinking.
- ✨ **Verbesserung:** Bereinigung-Routine.
- ✨ **Verbesserung:** Release-Packaging.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Verbesserte evcc-Anbindung.

## [3.8.7.6] – 2026-03-29

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** CO₂-Baum (Desktop & Mobilansicht).
- ✨ **Verbesserung:** Mobilansicht Integration.
- ✨ **Verbesserung:** Farbschema-Wechsel Crash Korrektur.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Strompreis-Korrektur (Astronomische Werte).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Zentriertes 4-Spalten-Layout.
- ✨ **Verbesserung:** Interaktive Legende.
- ✨ **Verbesserung:** Mobilansicht Energiebilanz-Statusanzeige.

## [3.8.7.5] – 2026-03-29

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Echtzeit-Power Korrektur.
- ✨ **Verbesserung:** Bluelink SoC-Interpolation.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Direkten Webaufruf Latency Korrektur.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Korrektur iDM-Crash.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Farbschema-Kompatibilität (Bright Modus).

## [3.8.7.4] – 2026-03-29

### 🔌 Wallbox Manager

- 📊 **Anzeige/Auswertung:** Direkt angebunden Dual-Car Oberfläche Unterstützung.
- ✨ **Verbesserung:** Intelligente Ladeplanung Abgleich.
- 📊 **Anzeige/Auswertung:** Oberfläche-Indikator.

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Statistik-Integrität.
- 📊 **Anzeige/Auswertung:** Oberfläche-Entkopplung.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Verbesserte iDM-Symbolik.
- ✨ **Verbesserung:** Interaktive Betriebsart.
- ✨ **Verbesserung:** Optimiertes iDM-Zuordnungen.
- ✨ **Verbesserung:** Tages-AZ Berechnung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Statischer Tarif (Kostensimulation).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Editor für Tageswerte.

## [3.8.7.3] – 2026-03-29

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Korrektur Wallbox-Summation.
- 📊 **Anzeige/Auswertung:** Zweispaltige Wallbox-Oberfläche.
- ✨ **Verbesserung:** Unabhängige SoC-Kalkulation.
- ✨ **Verbesserung:** Exklusive Zuweisung & Gast-Modus.
- ✨ **Verbesserung:** Dynamische Ladeplanung (manueller Ladeplan).
- ✨ **Verbesserung:** Multi-Car MQTT SoC.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Individuelles Styling.
- ✨ **Verbesserung:** Layout-Entzerrung (Anti-Overlap).
- ✨ **Verbesserung:** Desktop-Optimierung.
- ✨ **Verbesserung:** Mobilansicht-Optimierung.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** iDM Modbus-TCP Dienst.
- ✨ **Verbesserung:** Word-Swap Automatik.
- ✨ **Verbesserung:** Neues Datenformat (Wärmepumpendaten).
- ✨ **Verbesserung:** Passives Monitoring-Konzept.
- 🔄 **Migration/Kompatibilität:** Installationsassistent-Update.

## [3.8.7.2] – 2026-03-28

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Zweit-Wallbox Integration.
- ✨ **Verbesserung:** Getrennte Echtzeit-Visualisierung.
- 📊 **Anzeige/Auswertung:** Unabhängige Statistik.
- ✨ **Verbesserung:** Hausverbrauchs-Korrektur.
- ✨ **Verbesserung:** Konfigurations-Wizard.

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Langzeit-Statistik (Heatpump Hide).
- ✨ **Verbesserung:** Hauptsystem-Only Notifications.
- 🔎 **Diagnose:** Cluster-Status im Report.

## [3.8.7.1] – 2026-03-28

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Oberfläche-Stabilisierung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Energiemanagement HT/NT-Leistungsanhebung.
- ✨ **Verbesserung:** Wallbox Kostenvorschau.
- ✨ **Verbesserung:** Luxtronik Call-Safety.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Automatisches Querformat Layout.
- ✨ **Verbesserung:** Intelligentes Ausblenden (Preis-Trend).

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Anti-Spam Filter (Boot-Debounce).
- ✨ **Verbesserung:** Energiefluss-Branding.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Host-Networking Unterstützung.

## [3.8.7] – 2026-03-27

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Anti-Korruptions-Filter (Langzeit-Diagramm).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Netzqualität Weboberfläche (Schieflast-Ampel).
- ✨ **Verbesserung:** Daily Min/Max Peaks.
- ⚙️ **Regelung:** Konfigurierbare SLS-Grenze.
- ✨ **Verbesserung:** Wallbox-Sub-Metering.
- ✨ **Verbesserung:** Feststehend Kopfzeile mit Milchglaseffekt-Effekt.
- ✨ **Verbesserung:** Uhranzeige im heller Modus repariert.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Intelligentes Modbus laufende Verbindung & Spam-Filter.
- 🐛 **Fehlerbehebung:** Luxtronik Sommer-Modus Modbus Korrektur (Fehler 1313).
- ✨ **Verbesserung:** Docker-Aware Maintenance Skripte.

## [3.8.6.9] – 2026-03-27

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Fahrzeug-Vorlagen Speichern (Neu!).
- ✨ **Verbesserung:** Präzise Gast-Reichweitenberechnung.
- ✨ **Verbesserung:** Manueller Fahrzeug-SoC & Restladezeit (Interpolation).
- ✨ **Verbesserung:** Fahrzeug-Detail Ansicht.
- 📊 **Anzeige/Auswertung:** Oberfläche Konsistenz.
- ✨ **Verbesserung:** Dynamische Ladezeit-Berechnung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Dezente Kachel-Details (Auge-Symbole).
- ✨ **Verbesserung:** PV-Kachel Reorganisation.
- ✨ **Verbesserung:** Batterie-Kachel Design.
- ✨ **Verbesserung:** Intelligentes Ausblenden (Fremd-Wallboxen).

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Tagesstatistik Upgrade.
- ✨ **Verbesserung:** MQTT IP/Port Parse.

## [3.8.6.8] – 2026-03-26

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Langzeit-Diagramm Abweichung (Heute).
- ✨ **Verbesserung:** Phantom-Tag Generator.
- ✨ **Verbesserung:** Exakte Einspeisezähler.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Minifizierung & Performance.
- ✨ **Verbesserung:** Batterie-Kachel Alarme.
- 📊 **Anzeige/Auswertung:** Wärmepumpen-Phantom Oberfläche (Shelly).

## [3.8.6.7] – 2026-03-26

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Multi-Wallbox Vorbereitung.
- ✨ **Verbesserung:** Installationsassistent-Anpassung.



### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Endlos-Update-Loop.

## [3.8.6.6] – 2026-03-26

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Kugelsicherer Energiezählung.

### 🖥️ Weboberfläche

- 🔄 **Migration/Kompatibilität:** Nahtloses Weboberfläche-Update.
- ✨ **Verbesserung:** Echtzeit-Feedback.

## [3.8.6.5] – 2026-03-25

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Kaskadierte Entladung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wärmepumpen-Phantomverbrauch.
- ✨ **Verbesserung:** Fehlender Hausverbrauch.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent Rettungs-Ring.

## [3.8.6.4] – 2026-03-25

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** Agnostische V2H-Steuerung.
- ✨ **Verbesserung:** MQTT automatische Erkennung.
- ✨ **Verbesserung:** Energiefluss Visualisierung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Erweiterte KI-Szenarien.
- ✨ **Verbesserung:** Dynamische Lastverschiebung (Haushaltsgeräte).
- ✨ **Verbesserung:** Kostensimulations-Korrektur.
- ✨ **Verbesserung:** Systemprotokoll-Reihenfolge.

## [3.8.6.3] – 2026-03-25

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** go-e & Betriebsdaten MQTT Unterstützung.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Schnittstelle Upgrade.
- ✨ **Verbesserung:** Strompreis-Editor Korrektur.

## [3.8.6.2] – 2026-03-24

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Kontextsensitives Menü.
- ✨ **Verbesserung:** Systemprotokoll-Spam Filter.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Docker Echtzeit-Anzeige.
- ✨ **Verbesserung:** KI-Rohdaten Visualisierung.
- 📊 **Anzeige/Auswertung:** Fahrzeuganzeige (openWB).

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Start-Logik.

## [3.8.6.1] – 2026-03-24

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Direkt angebunden openWB-Integration.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Survival Modus (Inselnetz).
- ✨ **Verbesserung:** Scikit-Learn Integration.

## [3.8.6] – 2026-03-24

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Prädiktive Analytik & Machine Learning.
- ✨ **Verbesserung:** Systemautonomie & Resilienz (Inselnetz).
- ✨ **Verbesserung:** Phasengenaue Auslastungs-Visualisierung.
- ✨ **Verbesserung:** Netzdienliche Verbrauchersteuerung & V2X.
- 🔎 **Diagnose:** Frühwarnsystem (Auto-Diagnose).
- ✨ **Verbesserung:** Wärmepumpen-Fehlerspeicher.
- ✨ **Verbesserung:** Watchtower-Systemprotokoll.
- 🧱 **Stabilität:** Auskühlschutz (Hysterese).
- ✨ **Verbesserung:** Physikalischer SoC-Filter.
- ✨ **Verbesserung:** Verborgene Voreinstellungen werden bei der Konfigurationsprüfung erkannt.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Mobilansicht Strompreis-Ticker.

### 🖥️ Weboberfläche

- 🛡️ **Sicherheit:** Weboberfläche PIN-Schutz.
- 📊 **Anzeige/Auswertung:** E3DC Echtzeit-Anzeige.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Smart-Home Evolution (Matter & Push).

## [3.8.5.6] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Die lokale Lernfunktion berücksichtigt den durchschnittlichen Verbrauch von Haus, Wärmepumpe und Wallbox über mehrere Tage.
- ⚙️ **Regelung:** Ist kein Fahrzeug als steuerbarer Verbraucher verfügbar, kann eine ausdrücklich freigegebene Wärmepumpe günstige Energie nutzen.

### 📦 Distribution und Kompatibilität

- 🛡️ **Sicherheit:** Docker-Installationen verwenden ein isoliertes Netzwerk mit ausdrücklich zugeordneten externen Anschlüssen.
- 🐛 **Fehlerbehebung:** Die Echtzeit-Diagnose friert auf Bare-Metal-Systemen nicht mehr ein; frische Installationen enthalten die für das Echtzeit-Diagramm benötigte Abhängigkeit.

## [3.8.5.5] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Altlasten-Entfernung.

### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Zentrale Diagnose-Station.
- ✨ **Verbesserung:** Weboberfläche Bugfix.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Das Docker-Image unterstützt sowohl Intel-/AMD-Systeme als auch verbreitete Raspberry-Pi-Architekturen und kann dadurch auf NAS-Systemen und Mini-PCs betrieben werden.
- 📊 **Anzeige/Auswertung:** Docker Oberfläche-Sperre.

## [3.8.5.4] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Exakte Tageszähler.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Port-Routing Korrektur.

## [3.8.5.3] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Frühere Steuerungstrukturierte Daten Erweiterung.
- ✨ **Verbesserung:** Echtzeitverbindung Port-Kollision.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Modbus-Verbindungen können dauerhaft gehalten und bei Unterbrechungen erneuert werden.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** HA-Standby Docker Korrektur.

## [3.8.5.2] – 2026-03-23

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Interaktive Port-Wahl.
- ✨ **Verbesserung:** Transparente Installation.
- ✨ **Verbesserung:** Sandbox & Monitoring.
- ✨ **Verbesserung:** Watchdog-Pausierung.

## [3.8.5.1] – 2026-03-23

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Windows-Reparatur.

## [3.8.5] – 2026-03-22

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Shelly Wallbox-Integration.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Restlose Iframe-Entfernung.
- ✨ **Verbesserung:** Sicherungen Erweiterung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** DV-Balken (Börsen-Verkauf).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche Konsistenz.

## [3.8.4] – 2026-03-22

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Dynamische Fahrzeug-Tabs.
- ✨ **Verbesserung:** Intelligente Wallbox-Zuweisung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Beliebig viele Fahrzeuge.
- ✨ **Verbesserung:** Google Maps Integration.
- ✨ **Verbesserung:** Intelligenter Konfigurationsseite.
- 🛡️ **Sicherheit:** Eigene lokale Startanpassungen werden bei Web-Aktualisierungen nicht überschrieben und in Sicherungen einbezogen.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Universal-Shelly-Engine.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Bridge-Modus.

## [3.8.3.1] – 2026-03-21

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** WICHTIGER UPDATE-HINWEIS.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Doppelte Telegram-Nachrichten (HA).

## [3.8.3] – 2026-03-21

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Reale Stromkosten (Langzeitdatenbank).
- ✨ **Verbesserung:** Direkt angebunden frühere Steuerungstrukturierte Daten.
- ✨ **Verbesserung:** Heller Modus Neugestaltung.
- ✨ **Verbesserung:** Energiemanagement Netz-Puffer.
- ✨ **Verbesserung:** HA-Cluster Langzeit-Abgleich.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Smartes Preis-Weboberfläche.
- 📊 **Anzeige/Auswertung:** Preis & Kosten-Diagramm.



### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Git-Update Korrektur.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.8.2] – 2026-03-20

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Echtzeit-Lade-Energiezählung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Langzeit-Statistiken.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** MQTT automatische Erkennung.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Vollständiger Docker-Unterstützung.
- ⚙️ **Regelung:** Zentraler Ladeplan Regelung.
- ✨ **Verbesserung:** Saubere Deinstallation.

## [3.8.1] – 2026-03-20

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Geräuschlose Überwachung.
- ✨ **Verbesserung:** Verdichter-Erkennung.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Git-Update Korrektur.
- 🔄 **Migration/Kompatibilität:** Aktuelle Steuerung3.7 Kompatibilität.
- 🔎 **Diagnose:** Nach dem Ende des Installers erscheint eine verständliche Zusammenfassung von Erfolgen und Fehlern.

## [3.8.0] – 2026-03-19

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Atomares Betriebsdaten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Millisekunden-Rendering.
- ✨ **Verbesserung:** Ressourcen-Befreiung.
- ✨ **Verbesserung:** Interaktive Diagramme.
- ✨ **Verbesserung:** Smartes Gedächtnis.
- ✨ **Verbesserung:** Absolutwerte-Modus.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Offizieller Docker-Unterstützung.
- ✨ **Verbesserung:** Erweiterte Docker-Nutzung.
- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.7.1] – 2026-03-18

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Erweitertes Standby.
- ✨ **Verbesserung:** Watchdog-Korrektur.
- 📊 **Anzeige/Auswertung:** Oberfläche-Entkopplung.
- ✨ **Verbesserung:** Geister-Benachrichtigungen.
- ✨ **Verbesserung:** Temperatur-Auslesung.

## [3.7.0] – 2026-03-18

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Gastfahrzeug Erkennung.
- 🧱 **Stabilität:** Verfeinerte Lade-Hysterese.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Ressourcenschonend.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Performance-Leistungsanhebung.
- ✨ **Verbesserung:** Modbus-Entlastung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Heizstab-Visualisierung.
- ✨ **Verbesserung:** Dynamisches Layout.
- ✨ **Verbesserung:** Butterweiche Animationen.

## [3.6.0] – 2026-03-17

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Multi-EV Unterstützung.
- ✨ **Verbesserung:** Datenbank-Archivar.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Weboberfläche-Anzeige.
- ✨ **Verbesserung:** Weboberfläche-Graphen.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Bugfix Installationsassistent.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Smart Home Adapter.
- ✨ **Verbesserung:** Telegram-Leistungsanhebung.
- ✨ **Verbesserung:** Lokaler MQTT-Broker.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Containerisierung.

## [3.5.2] – 2026-03-16

### 🔋 Storage Manager

- ✨ **Verbesserung:** Doppel-Batterie Unterstützung.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Ultra-flüssige Oberfläche.
- ✨ **Verbesserung:** Apache Webweiterleitung.
- ✨ **Verbesserung:** Automatische Wiederherstellung.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Entkoppelte Architektur.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.5.1] – 2026-03-16

### ☀️ PV, Prognose und Energiemanagement

- 🛡️ **Sicherheit:** Gerätenamen-Schutz.
- ✨ **Verbesserung:** Phantom-Ordner Bug.
- ✨ **Verbesserung:** Konfigurationsseite Case-Sensitivity.

## [3.5.0] – 2026-03-16

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Kugelsicherer Hintergrunddienste.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-benötigte Systemkomponente Integration.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Zentraler Notification-Dienst.

## [3.4.3] – 2026-03-15

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Sicherheitsnetz Installationsassistent-Nutzer.

## [3.4.2] – 2026-03-15

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Unbeabsichtigte Abweichungen der Einstellungen werden verhindert.
- ✨ **Verbesserung:** Watchdog HA-Awareness.
- ✨ **Verbesserung:** Proaktive Zwischenspeicher-Rechte.
- ✨ **Verbesserung:** Rechte-Reparatur Leistungsanhebung.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Web-Update Korrektur.

## [3.4.1] – 2026-03-15

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Aktualisierung, Rechteverwaltung, Hochverfügbarkeit und Systemwächter wurden gemeinsam auf den korrigierten Installationsstand gebracht.

## [3.4.0] – 2026-03-15

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Aktiv/Passiv Cluster.
- ✨ **Verbesserung:** Hot & Warm Standby.
- ✨ **Verbesserung:** Echtzeit-Abgleich.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Integration.

## [3.3.6] – 2026-03-15

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Systemprotokolle werden begrenzt und rotiert; die Konsolenausgabe verarbeitet Umlaute zuverlässig.
- 🛡️ **Sicherheit:** Die Rechteprüfung erkennt und korrigiert unpassende Zugriffsrechte kontrolliert.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Der optionale Telegram-Bericht fasst den morgendlichen Anlagenstatus zusammen.

## [3.3.4] – 2026-03-14

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Dynamische Grundlast-Kompensation.
- ✨ **Verbesserung:** Thermische Preisanpassung (COP-Logik).
- ✨ **Verbesserung:** Auskühlschutz (PV-Pause).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Konfigurationsseite.

## [3.3.3] – 2026-03-14

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Die Energieverwaltung berücksichtigt dynamisch geänderte Einstellungen bereits beim Systemstart.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Sicherung und Rückfall wurden in den Aktualisierungsablauf aufgenommen.

## [3.3.2] – 2026-03-14

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Neue Tagesstatistik.
- ✨ **Verbesserung:** Historien-Auswahl.
- ✨ **Verbesserung:** Autarkie-Glättung.
- ✨ **Verbesserung:** Datenauswertung-Performance.
- ✨ **Verbesserung:** Häufig gelesene Echtzeitwerte belasten den Datenträger deutlich weniger.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Autarkie & Eigenverbrauch.
- ✨ **Verbesserung:** Bedienung-Optimierung.
- ✨ **Verbesserung:** Flüssigere Diagramme.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent-Leistungsanhebung.
- 🔄 **Migration/Kompatibilität:** Korrektur (Self-Update).

## [3.3.1] – 2026-03-13

### 🛠️ Installation und Update

- 📊 **Anzeige/Auswertung:** Korrektur (Diagramm-Installation).

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Korrektur (Release-Ablauf).

## [3.3.0] – 2026-03-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Energiemanagement Caching.
- ✨ **Verbesserung:** Einheitlicher Konfiguration Editor.
- ✨ **Verbesserung:** Priorisierte Ansicht.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Zentralisierung.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Robuster automatische Aktualisierung.
- 🔄 **Migration/Kompatibilität:** Zentrale Update-Richtlinie.
- ✨ **Verbesserung:** Umfassende Rechte-Korrektur.
- 🔄 **Migration/Kompatibilität:** Dokumentations-Update.

## [3.2.8] – 2026-03-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Energiemanagement Caching.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche Bündelung.
- ✨ **Verbesserung:** Einheitlicher Konfiguration Editor.
- ✨ **Verbesserung:** Priorisierte Ansicht.

## [3.2.7] – 2026-03-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfiguration Editor.
- ✨ **Verbesserung:** Rechte-Management.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Unattended-Korrektur.

## [3.2.6] – 2026-03-11

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Smart Charging Oberfläche.
- ✨ **Verbesserung:** Die Konfigurationsseite blendet Wärmepumpeneinstellungen aus, wenn die Luxtronik-Anbindung deaktiviert ist.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Web-Portal Update.
- ⚙️ **Regelung:** Prozess-Steuerung.

## [3.2.5] – 2026-03-11

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zentrale Konfiguration.
- ✨ **Verbesserung:** Smart-Logik (PV-Pause).
- ✨ **Verbesserung:** Energiemanagement.



### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Konfiguration Editor.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Automatisches Update.
- 🔄 **Migration/Kompatibilität:** Update-Richtlinie (Aktualisierungsrichtlinie).

## [3.2.4] – 2026-03-11

### ☀️ PV, Prognose und Energiemanagement

- 🛡️ **Sicherheit:** Reboot-Sicherheit.
- ⚙️ **Regelung:** Intelligente Pausen-Steuerung.
- ✨ **Verbesserung:** Bugfix (Abgleich-Check).
- ✨ **Verbesserung:** Bugfix (Schnittstelle-Call).
- ✨ **Verbesserung:** Die Verwaltung der Systemprotokolle wurde verbessert.

### 📈 Direktvermarktung und Strompreise

- 📊 **Anzeige/Auswertung:** Korrektur-Tarif-Anzeige.

## [3.2.3] – 2026-03-11

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Sicherheits-Neustart.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Luxtronik-Konfiguration.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Release-Optimierung.

## [3.2.2] – 2026-03-10

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Erweiterte Speicherplanung.
- ✨ **Verbesserung:** Sicherheits-Neustart.
- 🛡️ **Sicherheit:** Reboot-Sicherheit.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Bugfix (Permissions).

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Neuer Installationsassistent.

## [3.2.1] – 2026-03-09

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Diese Wartungsversion führte den in 3.2.0 dokumentierten Funktionsstand fort und aktualisierte das ausgelieferte Installationspaket.

## [3.2.0] – 2026-03-09

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** PV-Prognose-Pause.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Negativ-Preis-Leistungsanhebung.
- ✨ **Verbesserung:** Logik-Optimierung.
- ✨ **Verbesserung:** Komplett neu strukturierte Einstellungsseite (Wärmepumpen-Bedienung).
- 📊 **Anzeige/Auswertung:** Auswahl des Rücklauf-Sensors (Intern/Extern) für Anzeige und Regelung.
- ✨ **Verbesserung:** Einstellbare Verzögerungen für Stop und manuellen Leistungsanhebung.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Anzeige von Warmwasser- und Rücklauftemperaturen direkt in der Kachel.
- ✨ **Verbesserung:** Detaillierte Statusanzeige (Auto PV, Auto €, Auto Pause).

## [3.1.2] – 2026-03-08

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Strompreis-Leistungsanhebung.
- ✨ **Verbesserung:** Zwangs-Leistungsanhebung.

## [3.1.1] – 2026-03-08

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** In Echtzeit Energiefluss.

## [3.1.0] – 2026-03-08

### ♨️ Wärmepumpe und Wärme

- 🔄 **Migration/Kompatibilität:** Luxtronik, Strompreisassistent und Systemdienste wurden in einen gemeinsamen Installations- und Aktualisierungsablauf überführt.

## [3.0.1] – 2026-03-07

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Rechte-Management.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Globaler Schalter.
- ✨ **Verbesserung:** Vollständige Integration.
- ✨ **Verbesserung:** Auto-Leistungsanhebung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Auto-Leistungsanhebung Statusanzeige.
- ✨ **Verbesserung:** Mobilansicht Navigation.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Dienst-Integration.
- 🔄 **Migration/Kompatibilität:** Cloudflare-Kompatibilität.
- ✨ **Verbesserung:** Echtzeit-Systemprotokoll.
- ⚙️ **Regelung:** Prozess-Steuerung.
- 📊 **Anzeige/Auswertung:** Oberfläche-Feedback.

## [3.0.0] – 2026-03-07

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Die Luxtronik-Anbindung wurde mit Auslesen, Regelung und manueller PV-Anhebung in den Installationsassistent aufgenommen.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Statusprüfung, Berechtigungen und Installationsablauf berücksichtigen die neue Wärmepumpenanbindung.

## [2.6.5] – 2026-03-05

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Rechteprüfung und Aktualisierungsablauf wurden gemeinsam gehärtet, damit bestehende Installationen zuverlässig auf den neuen Stand wechseln.

## [2.6.4.1] – 2026-03-05

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Weboberfläche und Hintergrunddienste können gemeinsam benötigte aktuelle Betriebsdaten wieder zuverlässig verwenden.

## [2.6.4] – 2026-03-04

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** Status-Visualisierung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Echtzeit-Grabber Dienst.
- ✨ **Verbesserung:** Atomares Schreiben.
- ✨ **Verbesserung:** Watchdog-Bereinigung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Erweiterte Details.
- 📊 **Anzeige/Auswertung:** Echtzeit-Diagramm.
- ✨ **Verbesserung:** Weboberfläche-Validierung.

## [2.6.3] – 2026-03-04

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Konflikt-Erkennung.

## [2.6.2.1] – 2026-03-03

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die Erzeugung der Erstkonfiguration wurde korrigiert und gemeinsam mit dem Installationspaket aktualisiert.

## [2.6.2] – 2026-03-03

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wurzelzähler-Unterstützung.
- 🧱 **Stabilität:** Veraltete oder doppelte Einstellungen werden zuverlässig bereinigt.
- ✨ **Verbesserung:** Web-Konfiguration Korrektur.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Symbole-Caching Korrektur.
- 📊 **Anzeige/Auswertung:** Diagramm-Stabilität.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Die Installation arbeitet zuverlässiger mit administrativen Rechten.

## [2.6.1] – 2026-03-02

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Klickbare Kacheln.
- ✨ **Verbesserung:** Mobilansicht Optimierung.
- ✨ **Verbesserung:** Diagramme passen sich nun dynamisch der Bildschirmgröße an (Responsive).
- 🔎 **Diagnose:** Trennung von "Echtzeit-Status" und "Prognose" in eigene Reiter für mehr Übersichtlichkeit.
- ✨ **Verbesserung:** Prognose-Korrektur.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Neue Zeitauswahl (6h, 12h, 24h, 48h) direkt über dem Diagramm.
- 📊 **Anzeige/Auswertung:** Detailwerte werden übersichtlich über dem Diagramm eingeblendet.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Korrektur beim Auslesen negativer Werte bei Wechselrichter-Phasen.
- 📊 **Anzeige/Auswertung:** Verbesserte Skalierung der Diagramm-Achsen für negative Werte (Einspeisung/Entladen).
- 🔄 **Migration/Kompatibilität:** Update-Benachrichtigung.

## [2.6.0] – 2026-03-02

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Das Preisdiagramm markiert Tageswechsel sowie Höchst- und Tiefstpreise zuverlässiger und passt sich unterschiedlichen Bildschirmgrößen an.
- ✨ **Verbesserung:** PV-Prognose, Preisangaben und historische Ansichten wurden übersichtlicher angeordnet.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Die Webaktualisierung berücksichtigt bestehende Installationen und optionale Integrationen.

## [2.5.8] – 2026-03-01

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Notfall-Modus (Menu 99).
- 🔎 **Diagnose:** Erweiterter Status-Check.

## [2.5.7] – 2026-03-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Systemprotokoll-Viewer.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Preis-Graph.
- ✨ **Verbesserung:** Installationsassistent.

## [2.5.6] – 2026-03-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Tageswechsel-Logik.
- ✨ **Verbesserung:** Watchdog aktualisieren.

## [2.5.5] – 2026-02-28

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die Installation des optionalen Systemwächters wurde korrigiert; das Installationspaket erhielt den zugehörigen Stand.

## [2.5.4] – 2026-02-28

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Automatischer Neustart bei Abstürzen (Neustart always).
- ✨ **Verbesserung:** Gezielter Neustart.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Bereinigung von "toten" Anzeige-Sessions vor dem Start.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Erzwingen & Neustart.
- ✨ **Verbesserung:** Installation erzwingen (Re-Install), auch wenn die Version aktuell ist.
- ✨ **Verbesserung:** Installationsassistent.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Migration zu Dienstverwaltung.

## [2.5.3] – 2026-02-27

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Diese Zwischenversion aktualisierte ausschließlich Versionsangabe und Installationspaket; gegenüber 2.5.2 kam keine weitere Produktfunktion hinzu.

## [2.5.2] – 2026-02-27

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Watchdog-Overhaul.
- ✨ **Verbesserung:** Täglicher Statusbericht.
- ✨ **Verbesserung:** Multi-IP Überwachung.
- ✨ **Verbesserung:** Router-IP Konfiguration.
- ✨ **Verbesserung:** Benutzer-Flexibilität.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die zeitgesteuerte Ausführung wurde korrigiert.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Telegram-Robustheit.

## [2.5.1] – 2026-02-27

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Darkmode.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** Die zeitgesteuerte Ausführung wurde abgesichert.

## [2.5.0] – 2026-02-27

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Neuer Einstellungen.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Redundanz-Bereinigung.
- ✨ **Verbesserung:** Bugfix Sommerzeit.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent (Diagrammerstellung).
- ✨ **Verbesserung:** Konfiguration (Konfiguration).

## [2.4.2] – 2026-02-26

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Benutzerfreundlichkeit.
- 🔄 **Migration/Kompatibilität:** Ein Fehler wurde behoben, durch den der Installationsassistent nach einem Selbst-Update erneut nach dem Benutzernamen fragte, obwohl dieser bereits konfiguriert war.
- ✨ **Verbesserung:** Die Menü-Abfrage wurde personalisiert und zeigt nun den aktuellen Installationsbenutzer an (z. B. Auswahl (pi):).

## [2.4.1] – 2026-02-26

### 🔌 Wallbox Manager

- 📊 **Anzeige/Auswertung:** Die Weboberfläche zeigt die Kosten eines Wallbox-Ladevorgangs an.
- ✨ **Verbesserung:** Die Wallbox-Einstellungen sind über ein einheitliches Zahnradsymbol erreichbar.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Desktop- und Mobilansicht erhielten besser lesbare historische Diagramme und Hinweisfelder.
- ✨ **Verbesserung:** Berührungsgesten öffnen Erläuterungen auf Mobilgeräten zuverlässig.
- ✨ **Verbesserung:** Die zuletzt gewählte Ladeleistung wird im Browser gespeichert und beim nächsten Besuch wiederhergestellt.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Einstellungen werden unabhängig von unbeabsichtigter Groß- und Kleinschreibung zuverlässig erkannt.
- ✨ **Verbesserung:** Neue Einstellungen können über den Konfigurationseditor ergänzt werden.

## [2.4.0] – 2026-02-25

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Echtzeit-Fortschrittsanzeige im Dialog-Fenster.
- 🐛 **Fehlerbehebung:** Regelmäßige Abfrage-Mechanismus verhindert Timeouts bei langsamen Verbindungen (Cloudflare-Korrektur).
- 🐛 **Fehlerbehebung:** Visuelles Feedback (Grüner Haken / Rotes Kreuz) bei Erfolg/Fehler.
- ✨ **Verbesserung:** Headless-Installationsassistent.

## [2.3.4] – 2026-02-25

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Rechteverwaltung und Einbindung der temporären Betriebsdaten wurden für bestehende Installationen nachgebessert.

## [2.3.3] – 2026-02-25

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Diese Korrekturversion aktualisierte ausschließlich Versionsangabe und Installationspaket; der fachliche Produktstand von 2.3.2 blieb unverändert.

## [2.3.2] – 2026-02-25

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Szenarien für 7.2 kW, 11 kW und 22 kW Ladeleistung.
- 🔄 **Migration/Kompatibilität:** Auto-Aktualisierung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Automatische Wiederherstellung.
- ✨ **Verbesserung:** Rechte-Management.
- ✨ **Verbesserung:** Sofortige Klarheit (Bedienung).
- ✨ **Verbesserung:** Kosten-Transparenz.
- ✨ **Verbesserung:** Echtzeit-Feedback.
- ✨ **Verbesserung:** Professionelle Optik.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Komplettes Neugestaltung.
- ✨ **Verbesserung:** Echtzeitdaten Kacheln.
- 📊 **Anzeige/Auswertung:** Intelligente Strompreis-Anzeige.
- ✨ **Verbesserung:** Dynamischer Balken mit Farbcodierung (Grün/Gelb/Rot) je nach Preisniveau (Günstig/Teuer).
- ✨ **Verbesserung:** Trend-Indikatoren (Pfeile) zeigen steigende oder fallende Preise an.
- 📊 **Anzeige/Auswertung:** Anzeige der Tages-Minima und -Maxima.
- 📊 **Anzeige/Auswertung:** Multi-View Diagramm.
- ✨ **Verbesserung:** Smart regelmäßige Abfrage.

## [2.3.1] – 2026-02-25

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die Berechtigungsverwaltung wurde von einer veralteten Kopie bereinigt; die Einrichtung des Systemwächters ist nun nachvollziehbar dokumentiert.

## [2.3.0] – 2026-02-24

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Echtzeitwerte und historische Verläufe wurden für Desktop und Mobilansicht umfassend neu gegliedert.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Ein optionaler Wächter überwacht Netzwerkverbindung und laufende Kernprozesse und berücksichtigt beim Neustart eine geordnete Abschaltzeit.
- 🔎 **Diagnose:** Statusmeldungen können zusätzlich über Telegram zugestellt werden.

## [2.2.2] – 2026-02-20

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Numerische Eingaben mit deutschem Dezimalkomma werden robuster verarbeitet; die freie Wahl des Installationskontos wurde korrigiert.

## [2.2.1] – 2026-02-20

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Das Installationskonto lässt sich über einen eigenen Menüpunkt ändern; Einstellungen und Berechtigungen werden gemeinsam angepasst.

## [2.2.0] – 2026-02-19

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Eine neue Mobilansicht macht Echtzeitwerte und zentrale Bedienfunktionen auf kleinen Bildschirmen zugänglich.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Protokollierung und wiederkehrende Installationsaufgaben wurden robuster in den Installationsassistenten eingebunden.

## [2.1.0] – 2026-02-16

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Bei der Erstinstallation kann das lokale Installationskonto frei gewählt werden; Sicherung, Rückfall und Rechteverwaltung berücksichtigen diese Auswahl.

## [2.0.0] – 2026-02-13

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Die Diagrammansicht erhielt mehrere Messwertachsen und markiert den Beginn von Wallbox-Ladevorgängen auf der Strompreiskurve.
- 🧱 **Stabilität:** Verständliche Hinweise unterstützen bei fehlenden Berechtigungen; eine Aktualisierung der Seite erzeugt das Diagramm nicht unnötig oft neu.

## [1.1.1] – 2026-02-12

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Konfigurationsassistent, Installation, Rückfall, Deinstallation und Systemeinrichtung wurden auf den gemeinsamen MQTT-fähigen Installationsstand gebracht.

## [1.1.0] – 2026-02-11

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Die lokale MQTT-Anbindung an openWB wurde ergänzt. Erreichbarkeit, Anmeldung und Empfang des gewählten Datenkanals lassen sich nachvollziehbar prüfen.

## [1.0.3] – 2026-02-11

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Aktualisierungen starten die Anwendung wieder korrekt, stellen benötigte Berechtigungen her und übernehmen die neue Versionsangabe, ohne das Installationsverzeichnis zu verlieren.

## [1.0.2] – 2026-02-11

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Ein doppelt bereitgestelltes Installationsarchiv wurde entfernt; die Einstiegshinweise wurden entsprechend bereinigt.

## [1.0.1] – 2026-02-11

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Ein Installationspaket für die Diagrammerstellung wurde in die Distribution aufgenommen.

## [1.0.0] – 2026-02-11

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Der Installationsassistent erhielt eine automatische Aktualisierungsprüfung und die Anbindung an veröffentlichte GitHub-Versionen.
- 🔄 **Migration/Kompatibilität:** Installation, Aktualisierung und Versionierung wurden erstmals gemeinsam beschrieben.
