# Tagesstatistik nachberechnen

Das Tagesarchiv verwendet dieselbe Energiebilanz wie die historische Tagesansicht.
Der Hausverbrauch ist PV + Netzbezug − Einspeisung + Batterieentladung − Batterieladung,
abzüglich jedes einzeln gemessenen Verbrauchers genau einmal. Nicht einzeln gemessene
Verbraucher bleiben im Hausverbrauch. Unbekannte Mengen und Kosten bleiben unbekannt.

Vergangene Tage werden beim Stundenlauf und beim Start nicht automatisch geändert.
Die folgenden Wartungsbefehle betreffen höchstens die letzten 30 abgeschlossenen
Kalendertage. Sie benötigen die Rechte des bestehenden Archivdienstes auf das
Web-Datenverzeichnis sowie PHP CLI, das auch die bestehenden geplanten Webaufgaben nutzen.

## Trockenlauf

Aus dem Installationsverzeichnis:

```bash
python3 -B Installer/sqlite_archiver.py --nachberechnen --trockenlauf
```

`--trockenlauf` ist der Standard; ohne diese Option schreibt `--nachberechnen` ebenfalls
nichts. Die JSON-Ausgabe enthält für jeden Tag Status, alte und neue Werte sowie deren
Differenz in kWh beziehungsweise Euro. Fehlende Messungen oder Preise haben den Wert
`null` und einen Herkunftsgrund. Es werden keine fehlenden Statistikzeilen angelegt.

Als Quelle dienen ausschließlich `data/history_backups/history_YYYY-MM-DD.txt` im
Webverzeichnis. Vollständige Sicherungen benötigen gültige, streng aufsteigende
Zeitstempel desselben Berliner Kalendertages, einen ersten Messpunkt spätestens
fünf Minuten nach Tagesbeginn, einen letzten höchstens fünf Minuten vor Tagesende
und keine Lücke von einer Stunde oder mehr. Das entspricht der Intervallgrenze der
Tagesberechnung. Sommer- und Winterzeit werden berücksichtigt. Eine beschädigte,
unvollständige oder fehlende Sicherung wird mit Grund übersprungen.

## Anwenden

```bash
python3 -B Installer/sqlite_archiver.py --nachberechnen --anwenden
```

Vor der ersten Änderung entsteht unter `data/statistics_backups/tagesbilanz-…/`
ein konsistentes vollständiges SQLite-Backup einschließlich vorhandener WAL-Daten.
Integritätsprüfung, Vergleich der betroffenen Ausgangswerte und SHA-256-Prüfsumme
müssen erfolgreich sein. Der ausgegebene Backup-Pfad enthält `e3dc_stats.db` und
`backup.json`. Fehlschläge verhindern die Änderung; vorhandene Teilsicherungen sind
kein erfolgreicher Wartungslauf.

Aktualisiert werden ausschließlich die berechneten Energie-, Verteilungs- und
Kostenspalten sowie Autarkie und Eigenverbrauch der geeigneten Tage. Haus und
Verbraucher erhalten dieselbe Bilanz: Eine nicht mehr belegte Einzelmessung bleibt
`null`, ihr Verbrauch wird nicht zusätzlich zum bereits enthaltenen Hauswert gezählt.
Bestehende Peak-Shaving-Einsparwerte (`saved_*`), andere Tage und ML-Daten bleiben erhalten.
Schema und Spaltentypen bleiben unverändert, sodass ältere Statistikleser kompatibel
bleiben. Ein zweiter Aufruf mit denselben Quelldaten schreibt nichts und legt kein
weiteres Backup an.

Archiv und Wartung verwenden die SQLite-Schreibsperre (`BEGIN IMMEDIATE`); die
Wartezeit auf eine belegte Datenbank ist auf 30 Sekunden begrenzt. Lesende Zugriffe
bleiben möglich. Ein Dienststopp ist nicht erforderlich. Das Backup wartet ebenfalls
höchstens 30 Sekunden; ein PHP-Berechnungsaufruf höchstens 60 Sekunden.

## Rückweg

Die Ausgabe nennt den vollständigen passenden Befehl, beispielsweise:

```bash
python3 -B Installer/sqlite_archiver.py --wiederherstellen /var/www/html/data/statistics_backups/tagesbilanz-BEISPIEL
```

Der Rückweg prüft Backup-Prüfsumme und Integrität und setzt ausschließlich die zuvor
geänderten Spalten aus dem Backup in einer SQLite-Transaktion zurück. Später
hinzugekommene Tage und andere Daten bleiben erhalten. Wurden dieselben Spalten
inzwischen anderweitig verändert, bricht er vor Änderungen ab. Wiederholung ist
wirkungslos. Die gespeicherten Werte sind exakt wiederhergestellt; SQLite kann
technische Dateikopfwerte und die Seitenbelegung ändern, die gesamte DB-Datei ist
daher nicht notwendigerweise bytegleich.

## Andere Pfade und Docker

Der Standard-Webpfad ist `/var/www/html`. Bei abweichenden Pfaden erhält jeder Befehl
zusätzlich `--web-root /pfad/zum/webverzeichnis`. Der Rückwegbefehl enthält diesen Pfad.

Im Container wird derselbe Befehl mit dessen vorhandenem Dienstnutzer und innerhalb
des persistent eingebundenen Webverzeichnisses ausgeführt, zum Beispiel:

```bash
docker compose exec DIENST python3 -B /app/pi/Install/Installer/sqlite_archiver.py --nachberechnen --trockenlauf
```

`DIENST` und den Installationspfad aus der eigenen Compose-Konfiguration übernehmen.
Für das Anwenden `--trockenlauf` durch `--anwenden` ersetzen. Die Sicherung liegt im
Datenvolume und muss für den Rückweg erhalten bleiben. Keine Änderungen am Image,
kein Containerneustart und kein Dienststopp sind für den Wartungsaufruf nötig.
