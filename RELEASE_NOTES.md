# E3DC-Control v5.4.6d

Dieses Wartungsupdate verbessert Docker-Updates mit eigenen Compose-Dateien, die Speicherplanung und die Bedienung der Wärmepumpe.

## Docker

Der Host-Updater unterstützt ausdrücklich ausgewählte Compose-Dateien und Ergänzungen, Projekt- und Dienstnamen, mehrere getrennte Instanzen sowie Bind-Mounts. Kompatible eigene Ergänzungen bleiben erhalten; geprüft wird die tatsächlich zusammengeführte Konfiguration des ausgewählten E3DC-Dienstes. Neu angelegte leere Datenordner werden gezielt vorbereitet. Vorhandene Datenordner erhalten bei unpassenden Rechten eine konkrete Fehlermeldung.

Ein bestehender Container-Hostname wird bei fehlender Compose-Vorgabe für den persistenten Rollenanker übernommen. Ein bereits widersprüchlicher Rollenanker benötigt weiterhin eine gezielte Prüfung. Nach einem fehlgeschlagenen Update wird eine zuvor gestoppte Altinstanz ausdrücklich als gestoppt gemeldet.

## Speicher und Tarife

- Manuelles positives Laden ist auch an der Notstromreserve möglich. Entladen und Export bleiben dort gesperrt; Nutzer-Aus, Inselbetrieb, ungültige Messdaten und Anschlussgrenzen behalten Vorrang.
- Die Freigabe zum Netzladen schließt Speicherhalten ein und aktiviert den zugehörigen Schalter. Speicherhalten allein bleibt wählbar.
- Die Halteplanung berücksichtigt die zeitliche Reihenfolge von Verbrauch, PV und bekannten Preisen. Spätere PV verdeckt keine frühere Versorgungslücke; die weiche Ladekurve wird nicht als zusätzliche harte Hausreserve behandelt.
- Heat-/Sondertarifpreise werden über den bekannten Planungshorizont berücksichtigt. Eco-Aus widerruft die normale Marktfreigabe auch bei noch vorhandenen älteren Plänen.

## Wärmepumpe und Bedienung

- Luxtronik hält bei aktivem Warmwasser-Timer den Normal-/Eco-Sollwert auch nach Erreichen der Temperatur. Temporäre Boosts werden weiterhin zurückgenommen; eine Absenkung wartet auf das bestätigte Ende eines laufenden Warmwasserzyklus. Bestätigte identische Werte werden nicht fortlaufend neu geschrieben.
- Die PV-Regelung kann bewusst auf Istaufnahme mit Wh-Wächtern umgestellt werden. Voreinstellungen erleichtern die Einrichtung; bestehende Konfigurationen behalten ihre bisherige reservierte Betriebsart. Quellen-Aus und harte Schutzgrenzen bleiben vorrangig. Im messwertgeführten Modus kann ein positives Wh-Kontingent während der geschützten Mindestlaufzeit überschritten werden; die Anzeige weist dies aus.
- WP-Freigaben stehen gesammelt im Smart-Home-/WP-Bereich. Unwirksame Einstellungen zeigen ihre Voraussetzungen. Leere optionale Wallbox-Modusfelder verhindern das Speichern nicht mehr. Timer-Tooltips sind angebunden.
- Die allgemeine WP-Preisverschiebung löst weiterhin keine zusätzlichen Heizläufe aus. Ein wirkungsloser alter Freigabewert kann im Hinweisfeld deaktiviert werden. Bestehende, gesondert freigegebene Negativpreis- und Pre-Dump-Funktionen sind davon getrennt.

## Wallboxen

Die Modellwahl efy bzw. Multi Connect II bindet die herstellereigene Sonnenmodus-Automatik. Eine reine PV-Freigabe bleibt bei wechselndem Budget im Sonnenmodus. Reale Ladung bleibt auch bei einer konservativ aus Leistung abgeleiteten Stromuntergrenze im zentralen Wh-Wächter; daraus entsteht keine zusätzliche Strom- oder Phasenfreigabe. Gültig zugeteiltes Budget wird auch bei autonomer Phasenwahl energetisch bilanziert.

**Betriebsgrenze:** E3DC übernimmt weiterhin die automatische Phasenwahl. Ein 6-A-Angebot erzwingt keine einzelne Phase. Ein zuverlässiger einphasiger Wiederanlauf der efy bei kleinem PV-Budget ist noch nicht bestätigt; dieses Update enthält keine direkte externe 1-/3-Phasensteuerung.

## Updatehinweise

**Docker:** Zuerst den tatsächlich verwendeten Host-Helfer aus diesem Release aktualisieren. Ein neues Image ersetzt diese Hostdatei nicht. Bei eigenen Dateinamen und Ergänzungen den vollständigen Dateisatz gemäß [Docker-Dokumentation](doc/Docker_Dokumentation.md) ausdrücklich auswählen. Ein fester Image-Pin muss bewusst auf `v5.4.6d` geändert werden.

Vor dem Update Daten bei gestopptem Container mit numerischen Eigentümern und Rechten sichern. Updates bei beendeter Fahrzeugladung und ohne laufenden Phasenwechsel durchführen. Kein zusätzliches `user:` setzen. Bei vorhandenen lokalen Korrekturimages zuerst deren dokumentierten Übergang beachten. Rückfälle auf ältere Root-Images ausschließlich über den aktuellen Host-Updater ausführen.

**Luxtronik:** Den aktiven Warmwasser-Timer und Normal-/Eco-Sollwert prüfen. Die neue PV-Betriebsart wird bewusst gewählt; bestehende Kontingente werden nicht ungefragt ersetzt.

Weitere Einzelheiten: [Speicher](doc/Speicher_Ladesteuerung_Ablauf.md), [Luxtronik](doc/Luxtronik.md), [Wallbox](doc/Native_Wallbox.md), [Konfiguration](doc/Frontend_Ansichten.md) und [Update](doc/Update.md).
