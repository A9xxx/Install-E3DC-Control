# Smart Home Matter Bridge

Die Matter-Bridge bringt Live-Werte und einfache Schaltzustände von E3DC-Control lokal in Apple Home, Google Home und andere Matter-fähige Smart-Home-Systeme. Sie ist eine lokale, nicht zertifizierte read-only Statusintegration und arbeitet ohne Cloud.

## Datenfluss

```text
E3DC RSCP -> e3dc_live.py -> Ramdisk JSON -> Matter Bridge
```

Die Bridge nutzt die bestehenden Live-Daten aus der V4-Architektur. Sie liest keine Legacy-Config direkt.

## Aktivierung

Aktiviere Matter im Config-Editor. Gespeichert wird in:

```text
/var/www/html/data/e3dc_v4.json
```

Typische Parameter:

```ini
matter_bridge = 1
```

Die Bridge verwendet den festen Matter-Standardport `5540`. Dieser Port ist
derzeit nicht als Konfigurationsparameter freigegeben.

Matter ist ein optionales Modul. Das normale E3DC-Control-Update installiert
deshalb keine Node.js-, npm-, Avahi- oder D-Bus-Pakete. Erst die ausdrücklich
gestartete Matter-Installation prüft diese Paketgruppe gemeinsam und bricht bei
einem Solverfehler ab, ohne die Core-Aktualisierung zu blockieren.

Die Node-Abhängigkeiten sind vollständig in `package-lock.json` gebunden und
werden mit `npm ci --omit=dev --ignore-scripts` installiert. Die Bridge nutzt
Matter.js `0.12.6`; nicht benötigte QR- und TypeScript-Laufzeitpakete gehören
nicht zum installierten Produktionsbaum. Die Installation verlangt dafür
Node.js 18 oder neuer und bricht mit einer eindeutigen Meldung ab, wenn das
optionale Matter-Modul auf einem älteren System gestartet wird.

## Bestehende Kopplungen bei Aktualisierungen

Die Aktualisierung auf Matter.js `0.12.6` bleibt bewusst auf der kompatiblen
Matter.js-Geräte-API und verwendet den vorhandenen Storage weiter. Sie löscht
keine Kopplungen und verlangt keinen Werksreset. Für einen bewusst gewünschten
Neustart der Kopplung verwende ausschließlich **Auf Kopplung zurücksetzen** auf
der Matter-Seite; lösche `matter-storage` oder die Pairingdatei nicht manuell.

## Kopplung zurücksetzen

Der Rücksetzknopf verlangt eine angemeldete Websitzung, den gültigen
CSRF-Beleg und eine zusätzliche Browserbestätigung. Der Webserver erhält dabei
keinen allgemeinen Zugriff auf Fabric-, Session- oder Schlüsseldateien:

- Auf Bare Metal führt nur die einzelne freigegebene Wrapper-Aktion den Reset
  als Root aus. Sie startet einen vorher aktiven Dienst erst nach einem
  erneuten Binden von Konfiguration, Unit-Zustand sowie Eigentümer- und
  Gruppenvertrag. `matter_bridge` muss aktuell aktiviert, die Unit aktiviert
  beziehungsweise laufzeitaktiviert, startbar und weiterhin `inactive/dead`
  sein. Nutzer-`Aus` gewinnt; jeder Resetfehler nach dem Stop lässt Matter
  gestoppt. Eine ungültige oder widersprüchliche Konfiguration stoppt vor jeder
  Storage-Änderung.
- In Docker schreibt die Weboberfläche einen privaten versionierten Auftrag
  und löst den vorgesehenen Container-Neustart aus. Erst der root-eigene
  Startwächter des neuen Images darf diesen Auftrag vor dem Matter-Worker
  verbrauchen. Ein älteres Image ohne diese Fähigkeit lässt den Auftrag liegen
  und folgt weiter seinem bisherigen Startvertrag. Die Matter-Seite zeigt in
  diesem Kontext den Auftrag und Container-Neustart, aber keine
  Bare-Metal-systemd- oder Installationsmeldung.
- Der Root-Marker wird vollständig in einer zufälligen privaten
  `.matter-storage-reset-stage-<32 Kleinhexzeichen>` geschrieben und das
  Stage-Verzeichnis synchronisiert. Erst die atomare Veröffentlichung ohne
  Überschreiben als feste `.matter-storage-reset-quarantine.prepare` macht ihn
  autoritativ. Danach folgen nur die festen Prepare-, Quarantäne- und
  Receipt-Namen. Das Parent-Receipt entsteht WAL-artig als zweiter Hardlink
  desselben Marker-Inodes; nach dem Parent-`fsync` wird der interne Name
  entfernt und die Quarantäne synchronisiert. Nur die exakt belegte
  Dual-Link-Phase mit zwei Namen ist wiederaufnehmbar; zusätzliche Links sind
  eine Kollision.
- Zufällige Stage-Reste bleiben unabhängig von Inhalt oder Marker immer
  unverbindlicher Scratch. Sie werden nie gesucht, adoptiert, fortgesetzt oder
  gelöscht und blockieren einen neuen Versuch nicht. Der reservierte
  Namensraum aus Stage-Präfix sowie festen Prepare-, Quarantäne- und
  Receipt-Namen wird von allgemeinen Update-, Rechte- und HA-Läufen weder
  synchronisiert noch rekursiv normalisiert. Eine
  unmarkierte oder fremd markierte Datei beziehungsweise Quarantäne wird nicht
  gelöscht, umbenannt, auf einen anderen Eigentümer gesetzt oder in ihren Modi
  verändert. Fremde Mounts, ausgetauschte Roots, Symlinks und Identitätsdrift
  bleiben ebenfalls vor einer ungebundenen Mutation gesperrt.
- Historische Besitz- oder Modusfehler werden nicht durch den Reset geraten
  oder pauschal repariert. Im Docker-Container darf nur der ohnehin
  vorgeschaltete Startup-Preflight einen vollständig bindbaren regulären Baum
  auf den vorgesehenen Besitzer und die privaten Modi normalisieren. Alle
  anderen Fälle benötigen weiterhin die reguläre Update- beziehungsweise
  Rechte-Reparatur und danach einen erneut bewusst ausgelösten Reset.
- Läuft das gemeinsame Zeitbudget ab oder tritt ein Teilfehler auf, bindet nur
  ein Marker an einem festen Transaktionsnamen den erreichten Zustand für einen
  sicheren erneuten Versuch. Zufällige Stage-Reste, Pairingdatei und
  Docker-Auftrag autorisieren niemals das Löschen eines vorhandenen
  reservierten Namensraums.

Meldet die Seite eine unmarkierte Namenskollision, prüfe und sichere zuerst den
genau genannten Pfad. Benenne ausschließlich diesen Pfad anschließend
reversibel und ohne Überschreiben um, beispielsweise:

```bash
sudo mv --no-clobber -- "<gemeldeter-pfad>" "<gemeldeter-pfad>.admin-YYYYMMDD-HHMMSS"
```

Lösche den Bestand nicht und ändere Eigentümer oder Modi nicht auf Verdacht.
Löse nach der Umbenennung den bestätigten Reset erneut aus.

Die Matter-Seite liest den sichtbaren Kopplungsstatus begrenzt und ohne
Symlink-Folge. Eine fehlende, zu große oder schematisch ungültige Pairingdatei
wird nicht als gültige Kopplung dargestellt und blockiert die Seite nicht.
Nur ein wirklich unsicherer reservierter Auftragsknoten oder eine fremde
Mount-/Rootidentität benötigt noch die konkret angezeigte, eng begrenzte
Admin-Reparatur; normale Altinstallationen sollen daran nicht hängen bleiben.

In einem HA-Verbund bleiben dieser private Storage, der reservierte
Transaktionsnamensraum einschließlich Stage-Präfix, Prepare, Quarantäne und
Parent-Receipt, die Pairingdatei
`/var/www/html/ramdisk/matter_pairing.json` und ihre temporäre Schreibdatei
knotenlokal. Sie werden nicht über den allgemeinen Datensync übertragen oder
von dessen anschließender Rechteprojektion verändert. Ein Standby-Knoten
benötigt daher bei Bedarf eine eigene Matter-Kopplung; ein transparentes
Fabric-Failover ist derzeit nicht Bestandteil des HA-Vertrags.

Im Docker-Container prüft ein root-eigener Descriptorwächter den persistenten
Matter-Storage vor der startseitigen Härtung. Er folgt keinen Symlinks und lässt nur
Verzeichnisse sowie reguläre Einzel-Link-Dateien innerhalb derselben
Dateisystem- und Mountgrenze zu. Sonderdateien, Mehrfachidentitäten, ein
ausgetauschter Root oder ein veränderter Namenssatz stoppen den Container. Erst
danach werden gebundene Verzeichnisse auf `0700` und Dateien auf `0600`
gehärtet; unmittelbar vor Node.js muss dieselbe Rootidentität noch einmal
vollständig grün sein. Der Matter-Worker setzt zusätzlich `umask 077`, sodass
auch neue Fabric-, Endpoint-, Event- und Sessiondateien während der Laufzeit
höchstens mit `0600` entstehen. Matter-Protokoll, Pairing und mDNS-Verhalten
ändern sich dadurch nicht.

Der HA-Abgleich arbeitet ohne `--delete`. Neue Ausschlüsse entfernen deshalb
keine Matter-Dateien, die eine frühere Version bereits auf den Partner kopiert
hat. Wenn HA schon vor 5.4.3i aktiv war, prüfe beide Knoten. Entferne eine alte
Kopie erst nach eindeutiger Zuordnung und Sicherung des weiterhin benötigten
Originals; war Pairing- oder Fabric-Material auf dem anderen Knoten vorhanden,
kopple Matter bei Bedarf neu.

Eine spätere Umstellung auf die neue native `ServerNode`-API wird getrennt
angekündigt: Deren Storageformat ist laut Matter.js-Migrationsvertrag nicht mit
der bisherigen Geräte-API kompatibel und erfordert eine neue Kopplung.

## Dienst

Bare Metal:

```bash
sudo systemctl restart e3dc-matter-bridge
sudo systemctl status e3dc-matter-bridge
```

Docker:

```bash
docker restart e3dc-control
docker exec e3dc-control tail -f /var/www/html/logs/matter_bridge.log
```

## Verfügbare Entitäten

Die Bridge stellt aktuell genau drei virtuelle, ausschließlich lesende
Statusschalter bereit:

- `E3DC Wallbox aktiv`: EIN bei gemessener Wallboxleistung über 50 W.
- `E3DC PV produziert`: EIN bei gemessener PV-Leistung über 500 W.
- `E3DC Einspeisung aktiv`: EIN bei gemessener Netzeinspeisung über 500 W.

Die Schalter zeigen Zustände an. Eine Betätigung in Apple Home, Google Home
oder einem anderen Matter-System erzeugt keinen Wallbox-, PV- oder
Netzsteuerbefehl. Weitere Entitäten wie Hausverbrauch, Batterie-SoC,
Wärmepumpe oder Notstrom werden in dieser Version nicht bereitgestellt.

## Hinweise

- Matter ist lokal und reagiert empfindlich auf mDNS/IPv6/Firewall-Probleme.
- Wenn ein Gerät nicht auftaucht, zuerst `avahi-daemon` und den Matter-Dienst prüfen.
- Die stabile MQTT-Integration bleibt für Home Assistant weiterhin die robustere Option.
