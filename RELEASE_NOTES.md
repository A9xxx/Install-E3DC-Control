# E3DC-Control v5.4.6c

Dieses Update verbessert die PV-Ladung an festen E3DC-Wallboxen, die Leistungsverteilung zwischen Wallbox und Wärmepumpe sowie die Kompatibilität älterer Docker-Installationen.

## Wallboxen

- Feste, durch Python stromgeregelte E3DC-Wallboxen können unbekannte Fahrzeuge mit 6 A je verfügbarer Phase erkennen. Dafür werden mindestens 1.380 W echter freier PV-Anteil und eine zentrale Freigabe für die mögliche Mehrleistung benötigt. Die tatsächlich genutzten Phasen werden aus mehreren vollständigen Messungen ermittelt; eine reine Statusanzeige erhält keine Steuerfreigabe.
- Ein gemeinsames dauerhaft gespeichertes Konto begrenzt die zusätzliche PV-Deckungslücke auf höchstens 40 Wh und die Probe auf höchstens 30 Sekunden. Kleinere konfigurierte Grenzen bleiben wirksam. Abstecken oder Dienstneustart erneuern das Guthaben nicht. Fehlende Messungen und harte Schutzgrenzen stoppen die Probe.
- Erkannte Phasen bleiben über Ladepausen erhalten. Bei ausreichendem PV-Angebot geht die Ladung ohne erzwungenen Stopp in die normale Regelung über. Nach einem Defizitstopp bleibt ein normaler PV-finanzierter Wiederanlauf unter den bestehenden Wiederanlaufzeiten möglich.
- Laufende zweite Wallboxen werden mit ihrer gebundenen Leistung berücksichtigt; freie Leistung eines Fahrzeugs kann wieder dem anderen Ladepunkt zugutekommen. Reservierte Startleistung darf keinen weiteren ruhenden Ladepunkt starten.
- Phasenwechsel, CP-Unterbrechung, Wiederanlauf und Erkennungsprobe werden gegenseitig abgestimmt. Der bestehende Schutzabstand für einen weiteren Phasenwechsel darf den bestätigten Wiederanlauf nicht blockieren. Herstellereigene efy-Automatik sowie die bestehenden openWB- und go-e-Steuerwege behalten ihre eigenen Rollen.

## Wärmepumpe und Verbraucherpriorität

- Luxtronik-Heizung und Warmwasser erhalten getrennte PV-Aufträge. Ein Warmwasser-Zeitfenster blockiert keinen berechtigten Heizungsauftrag; eine ausbleibende Reaktion beendet den zugrunde liegenden Wärmebedarf nicht stillschweigend. Ohne passenden PV-, Preis-, Zeitplan- oder manuellen Auftrag entsteht kein optionaler Warmwasser-Boost.
- Die zentrale Verteilung berücksichtigt tatsächliche Aufnahme, Startreserve und konfigurierte Verbraucherpriorität. Die Wallbox erhält den freigegebenen Rest. Bei Wärmepumpenvorrang wird die tatsächliche Rücknahme von Wallboxleistung vor einem neuen Verdichterstart abgewartet.
- Mindestlaufzeit und Wiedereinschaltschutz orientieren sich am physischen Verdichterlauf. Akku- und Netzüberbrückung werden getrennt begrenzt und dauerhaft bilanziert. Eine optionale Startfreigabe benötigt ein eingetragenes elektrisches Leistungsprofil und ausdrücklich erlaubte Kontingente.

## Docker

- Der Host-Updater akzeptiert bei bekannten Compose-Altvorlagen auch den von älteren Compose-Versionen ausdrücklich ausgegebenen Standardwert `external: false`. Externe Volumes und zusätzliche Treiberoptionen bleiben gesondert geschützt.
- Der Privilegienschutz kann auf älteren Kerneln ohne `NoNewPrivs`-Statusfeld direkt über die Kernelabfrage nachgewiesen werden. Dort muss zusätzlich der gesamte Container mit `no-new-privileges` gestartet werden; UID-, Gruppen- und Capability-Prüfungen bleiben bestehen.

## Updatehinweise

**Luxtronik:** Vor neuen automatischen PV-Boosts im Config Editor unter **PV-Überschuss-Boost → Luxtronik: Leistungsprofil und Energie zur Überbrückung** die maximale elektrische Aufnahme sowie erlaubte Akku-/Netzleistung und Wh-Kontingente eintragen. Fehlende Angaben lassen optionale PV-Starts warten; sie blockieren keine Installation und ersetzen keine herstellerseitige Heizungs- oder Warmwasserregelung. Ein einzelner gemessener Warmwasser-Betriebspunkt ist keine garantierte Geräteobergrenze.

**Docker:** Zuerst den verwendeten Host-Helfer `Installer/docker_compose_update.py` aktualisieren; ein neues Image ersetzt diese Hostdatei nicht. Benötigte Volumes bei gestopptem Container mit numerischen Eigentümern und Dateirechten sichern. Keine zusätzliche Compose-Option `user:` setzen. Ein fester Pin muss bewusst auf `v5.4.6c` geändert werden.

Updates und Container-Neuerstellungen bei beendeter Fahrzeugladung und ohne laufenden Phasenwechsel durchführen. Private Wallbox-Steuerzustände überleben einen Neustart desselben Containers, aber keine Neuerstellung. Rückfälle auf ältere Root-Images ausschließlich über den aktuellen Host-Updater ausführen. Aus Bridge zuerst dieselbe aktuelle Runtime-Version im Hostprofil wiederherstellen und ihren gesunden Start prüfen.

Einzelheiten stehen in [Native Wallbox](doc/Native_Wallbox.md), [Luxtronik](doc/Luxtronik.md), [Docker](doc/Docker_Dokumentation.md) und [Update](doc/Update.md).
