# Experimentelle Kostenhistorie

Der Schalter „Kostenansicht je Viertelstunde“ liegt im Config-Editor unter Tarif & Strompreise und ist standardmäßig aus. Nach dem Einschalten erscheint im Verlauf „Kostenhistorie (experimentell)“. Der Zeitraum wird über „Tag“, „Woche“, „Monat“ oder „Jahr“ und den Kalender daneben gewählt; die Ansicht beginnt mit dem heutigen Tag. Monate zeigen ihre Tage, Tage außerhalb der Jahresansicht ihre Stunden und Stunden ihre Viertelstunden. Die Kacheln oberhalb der Tabelle beziehen sich auf den gewählten Zeitraum, etwa „Heute“ oder „Diese Woche“. Die letzte Tabellenzeile „Gesamt seit Beginn der Aufzeichnung“ bleibt unabhängig vom gewählten Zeitraum. Auf dem Desktop liegt dieselbe Ansicht im Schnellzugriff „Kosten“ hinter dem Umschalter „Diagramm | Kostenhistorie (experimentell)“. „Download CSV“ exportiert die sichtbaren Tabellenzeilen einschließlich aufgeklappter Stunden und Viertelstunden; die Spalte Auflösung unterscheidet Summen und Details, die nicht nochmals addiert werden dürfen. Ein Dienstneustart ist nicht nötig.

Die Minutenhistorie zeichnet ab dem Einschalten Bezugspreis, Preisquelle und den damaligen Einspeisetarif auf. Der vorhandene stündliche Archivlauf schreibt abgeschlossene Viertelstunden in die zusätzliche Tabelle `quarter_hour_costs` der Statistikdatenbank. Bereits gespeicherte Einträge werden bei einem späteren Tarifwechsel nicht geändert. Zwischen den Archivläufen ergänzt die Anzeige abgeschlossene Viertelstunden der letzten beiden Stunden vorläufig und ausschließlich lesend. Bei ausgeschaltetem Schalter werden weder neue Kostendaten geschrieben noch angezeigt. Bereits gespeicherte Werte bleiben erhalten.

Der Hausverbrauch stammt aus derselben Energiebilanz wie die Tagesstatistik. Jede einzeln gemessene Last wird genau einmal berücksichtigt. Ohne Einzelmessung bleibt der Verbraucher im Haus enthalten. Viertelstunden nutzen die Integration der Minutenleistungen; Tageszähler werden nicht auf Viertelstunden umgedeutet. Die Verteilung auf PV, Netz und Akku ist eine bilanzielle Zuordnung.

Netzbezug wird mit dem damaligen Bezugspreis bewertet. PV-Strom im Haus und PV-Ladung des Akkus erhalten den entgangenen Einspeiseerlös. Netzladung erhält den Bezugspreis der Ladezeit. Beim Entladen gilt der Mischpreis des Akkuinhalts. Diese Hausbewertung ist kein zusätzlicher Rechnungsposten: Der Netzsaldo enthält Netzbezugskosten abzüglich Einspeiseerlös und gegebenenfalls den zeitanteiligen festen DV-Monatsbeitrag.

Beim ersten Start wird der Akkuwert aus den vorhandenen Minutenhistorien der letzten höchstens 30 Tage hergeleitet. Am ältesten verfügbaren Zeitpunkt wird SoC × nutzbare Kapazität mit der Einspeisevergütung bewertet. Danach gilt dieselbe Mischrechnung wie im Betrieb, einschließlich Netzladung zum damaligen Bezugspreis. Fehlen ältere Daten, wird der Startinhalt direkt mit der Einspeisevergütung angesetzt. Fehlt diese Vergütung, bleibt die Bewertung leer mit Grund.

Nach einer Messlücke wird die Energiemenge erneut an SoC × Kapazität verankert. Der bisherige Mischpreis bleibt erhalten; ein Zuwachs wird mit der Einspeisevergütung bewertet. Weil die Ladequellen während der Lücke nicht belegt sind, wird der verbleibende Inhalt als geschätzt markiert. Der geschätzte Anteil wird mit dem Akkuinhalt proportional entladen und durch belegte Ladungen verdünnt. Er bleibt gekennzeichnet, solange mehr als 0,000000001 kWh davon verbleiben (ausschließlich numerische Rundungstoleranz). Energie, Mischpreis und geschätzte Energie werden je Viertelstunde gespeichert. Auch ältere gespeicherte Zustände mit unbekanntem Anfangswert werden vor der ersten neuen Viertelstunde aus dem verfügbaren Rückblick hergeleitet. Bereits festgeschriebene Zeilen bleiben unverändert. Fehlt der Rückblick, greift die gekennzeichnete Startschätzung mit der Einspeisevergütung. Wandlungsverluste und Kapazitätsdrift sind nicht gesondert modelliert.

Die Statuswörter „vollständig“, „vorläufig“, „geschätzt“ und „Lücke“ haben verständliche Gründe als Tooltip und beim Antippen. Leere Beträge erscheinen als „–“ mit Grund. Tage ohne Kostenwerte nennen die einmalige Nachberechnung. Nach dem Einschalten ergänzt der stündliche Archivlauf einmalig die abgeschlossenen Tage der letzten 30 Tage, für die Minutenhistorien vorliegen: je Lauf höchstens einen Kalendertag und höchstens 60 Sekunden. Vor dem ersten Schreiben legt er eine Sicherung an; der Fortschritt erscheint als Hinweis über der Tabelle. Der Wartungsbefehl unten bleibt für einen gezielten Rückblick mit Trockenlauf zuständig.

Bei Direktvermarktung wird der Marktpreis um den konfigurierten Erlösaufschlag und die volumenabhängigen Gebühren einschließlich einer nicht erstattbaren Umsatzsteuer korrigiert. Bei aktiver EEG-Förderung wird mindestens der konfigurierte anzulegende Wert angesetzt. Diese vertragliche Näherung ersetzt keine Abrechnung des Vermarkters. Ein fehlender Marktpreis oder eine fehlende Gebührenbasis ergeben einen unbekannten Erlös, niemals einen erfundenen Nullpreis. Ohne DV gilt der konfigurierte EEG-Einspeisetarif.

Mindestens zehn Proben und eine vollständige Intervallabdeckung sind nötig. Messabstände über zwei Minuten gelten als Lücke. Unvollständige Viertelstunden erhalten unbekannte Energiemengen und Kosten mit Begründung. Zeitstempel unterscheiden auch die doppelte Herbststunde; Tagesaggregate umfassen entsprechend 23 oder 25 Stunden.

## Einmaliger Rückblick auf 30 abgeschlossene Tage

Vorher den experimentellen Schalter einschalten. Im Installationsverzeichnis ausführen:

```bash
python3 -B Installer/sqlite_archiver.py --kosten-nachberechnen
python3 -B Installer/sqlite_archiver.py --kosten-nachberechnen --anwenden
```

Der Standard ist ein Trockenlauf ohne Änderung. `--anwenden` erstellt zuerst mit der bestehenden SQLite-Backup-API eine geprüfte vollständige Sicherung einschließlich bestätigter WAL-Daten. Anschließend werden ausschließlich fehlende Viertelstunden ergänzt. Wiederholung verändert keine vorhandenen Werte. Fehlende Historientage werden ausgewiesen; ihre Viertelstunden bleiben als unvollständig gekennzeichnet. Die maximale Datenmenge umfasst die letzten 30 abgeschlossenen Berliner Kalendertage.

Alte Minutenhistorien belegen den damaligen Bezugspreis. Sie enthalten keine historischen DV-Marktpreise. DV-Erlöse bleiben deshalb unbekannt. Der heutige konfigurierte EEG-Satz wird beim Rückblick als Schätzung gekennzeichnet und anschließend festgeschrieben. Ein zwischenzeitlicher früherer Anbieter- oder Vergütungswechsel ist aus diesen alten Dateien nicht rekonstruierbar.

Das Ergebnis nennt das Backupverzeichnis. Der Rückweg lautet:

```bash
python3 -B Installer/sqlite_archiver.py --kosten-wiederherstellen BACKUPVERZEICHNIS
```

Er entfernt ausschließlich die durch diese Wartung ergänzten und seitdem unveränderten Viertelstunden. Andere Statistiken bleiben erhalten. Die leere zusätzliche Tabelle darf bestehen bleiben; ältere Software ignoriert sie. Der Rückweg erhält die Datenwerte, garantiert jedoch keine bytegleiche SQLite-Datei. Ein abweichendes Webverzeichnis wird in allen Befehlen mit `--web-root PFAD` angegeben. In Docker werden die Befehle innerhalb der betreffenden Instanz ausgeführt.

Die Web-Vorschau liest nur ihr aktuelles Minutenfenster. Bei einem alten oder unbewerteten Akku-Anschlusswert erscheint „Akku-Startwert wird beim nächsten Archivlauf hergeleitet“. Den Rückblick übernimmt ausschließlich der Archivlauf. Auch gespeicherter Inhalt ohne Mischpreis wird dabei neu hergeleitet; fehlt der Rückblick oder enthält er Lücken, wird der Inhalt mit der belegten Einspeisevergütung als Schätzung verankert. Ohne diese Vergütung bleibt die Bewertung leer und nennt den Grund. Festgeschriebene Zeilen bleiben unverändert.
