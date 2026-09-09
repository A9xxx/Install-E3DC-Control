# E3DC-Control v5.4.5f

## Speicher, Direktvermarktung und Wärme

- Die PV-Ladeleistung in Eco+ berücksichtigt günstige Verkaufspreise auch ohne Verkaufs- oder Negativpreisfenster. Die bestehende Sicherheitsladekurve, DC-Quellentrennung, Reserven und Abregelschutz bleiben bindend. Ein vollständiger Ladeaufschub bis zur billigsten Stunde ist nicht enthalten.
- Preisbedingter Entladeschutz erhält eine unabhängig berechnete Speicherladegrenze. Der Start einer nur beobachteten Wallbox öffnet dadurch nicht mehr allein die maximale Batterieladeleistung.
- Eine erst am Folgetag beginnende Ladekurve verursacht außerhalb ihres Vorhaltefensters keine wechselnden Ladepausen durch schwankende Rest-PV. Die Anzeige nennt den Kurvenbeginn mit Datum.
- Nach einer ungenutzten Wärmepumpen-Startfreigabe kann bei bestätigtem Stillstand erneut ein Budget angefragt werden. Nutzersperren und die zentrale Freigabe bleiben maßgeblich.

## Verbindungen und Docker

- Der native RSCP-Client empfängt vollständige TCP-Frames und prüft angekündigte Prüfsummen korrekt. Unvollständige oder beschädigte Antworten entwerten die Verbindung; unbestätigte Schreibaufträge werden nicht automatisch wiederholt.
- Native E3DC-Wallboxen übernehmen geänderte RSCP-Verbindungseinstellungen über den bestehenden Dienstneustart.
- Das eigenständige Skript im Ordner E3DC-Slave verwendet eine gemeinsame RSCP-Verbindung, begrenzte Lesewiederholungen und eine datensparsame Fehlerdiagnose. Ein Wiederanlauf benötigt frische Master-Daten und bestätigte Gerätegrenzen. Die 100-W-Entladestartschwelle bleibt erhalten; das Skript wird nicht automatisch installiert.
- Docker-Updates berücksichtigen erkannte Neustartphasen des alten Containers. Start und Konfigurationsmigration stellen bei sicher gebundenen Dateien die benötigten Rechte für das Speichern her. Unzulässige Dateieigentümer und Verknüpfungen bleiben gesperrt.

## Anzeige und Bedienung

- Vitals unterscheidet konfigurierte Nutzkapazität und BMS-Werte. Ein doppelter SOH-Abschlag und pauschale Ableitungen der Neuzustandskapazität entfallen; Schätzwerte bleiben gekennzeichnet.
- Die PV-Diagnose zeigt den aktuellen Kalibriersammelstand mit eigener Zeitbasis. Historische Kennzahlen bleiben erhalten. Automatische Ladekurvenkorrektur und eine statistisch belegte P50-Aussage sind weiterhin nicht enthalten.
- Zeitachsen und Tooltips werden bei Ansichtswechseln neu gebunden. Verlauf und Prognose erhalten feste Uhrzeitraster und hervorgehobene Tageswechsel. DV-Hybrid und geglättete Planverläufe erhalten Datenlücken und echte Abschaltkanten.
- Der Speicherschalter erhält seine bestätigte Regler-Rückmeldung über einen geschützten Statuszugang.

## Updatehinweise

Nach dem Update das Dashboard neu laden. Docker-Nutzer aktualisieren auch den Host-Helfer `Installer/docker_compose_update.py`; ein Containerimage ersetzt diese Hostdatei nicht. Der dokumentierte Updateweg und die bestehenden Rückfallgrenzen bleiben bestehen.

Für das eigenständige Slave-Skript die zusammengehörigen Python-Dateien aktualisieren und die eigene Konfiguration erhalten. Der Master muss weiterhin aktuelle MQTT-Daten liefern. Die Meldung `RSCP_ERR_ALREADY_IN_USE` allein beweist weder einen fremden Regler noch eine durch dieses Update bereits beseitigte Gerätebelegung.
