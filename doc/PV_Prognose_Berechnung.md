# PV-Prognose im Betrieb

Die PV-Prognose liefert die erwartete Erzeugung für Dashboard, Ladekurve,
Pre-Dump und Direktvermarktung. Dieses Dokument beschreibt die für Betreiber
relevanten Datenquellen, Einstellungen, Anzeigen und Diagnosewege.

## Datenquellen

E3DC-Control kann je nach Konfiguration Wetter- und PV-Daten aus den im
Konfigurationseditor angebotenen Quellen verwenden. Zugangsdaten werden lokal
gespeichert und dürfen weder in Screenshots noch in Diagnosebeiträgen
veröffentlicht werden.

Für eine belastbare Prognose müssen mindestens folgende Angaben stimmen:

- Standort oder die vom gewählten Anbieter benötigte Anlagenkennung;
- installierte PV-Leistung;
- Ausrichtung und Neigung der belegten Dachflächen;
- Zeitzone und Systemzeit;
- optional getrennte Angaben für weitere PV-Flächen oder Zusatzwechselrichter.

Fehlt eine externe Quelle vorübergehend, verwendet das System nur die lokal
verfügbaren, als gültig bewerteten Daten. Fehlende Slots werden nicht als
bestätigte Erzeugung ausgegeben.

## Sichtbares Verhalten

Das Dashboard zeigt die erwartete PV-Leistung und die daraus abgeleitete
Energie über den Prognosezeitraum. Speicherziele und flexible Verbraucher
verwenden dieselbe freigegebene Prognosebasis. Korrekturen dürfen die
konfigurierten Anlagen- und Leistungsgrenzen nicht überschreiten.

Die Direktvermarktung hält Endkundentarif und Day-Ahead-Marktpreis getrennt.
Ein fehlender Marktpreis wird nicht durch einen Tarifwert ersetzt. Ohne
geeignete Markt- und Prognosedaten entsteht kein aktiver
Direktvermarktungsbefehl.

## Plausibilitätsgrenzen

- Negative PV-Erzeugung wird nicht als Prognose übernommen.
- Werte oberhalb der konfigurierten Anlagenleistung werden begrenzt oder als
  unplausibel verworfen.
- Veraltete Daten werden als solche gekennzeichnet und lösen keine neue aktive
  Regelentscheidung aus.
- Ein Prognosefehler hebt Reserve-, Netz-, Speicher- oder
  Verbraucher-Schutzgrenzen nicht auf.

## Vergleich mit der E3/DC-Historie

E3DC-Control kann Prognoseausgaben zusätzlich mit der nativen
15-Minuten-Historie des E3/DC-Systems vergleichen. Diese Funktion ist eine
reine Betriebsdiagnose und standardmäßig ausgeschaltet:

- Sie verändert weder Speicherbefehle noch Prognosemodelle oder Einstellungen.
- Ein eigener, niedrig priorisierter Dienst führt die Auswertung getrennt von
  Live-Daten, Prognoseerzeugung und Speicherregelung aus.
- Prognoseausgaben werden frühestens im Abstand von sechs Stunden archiviert.
  Die normale E3/DC-Historienanfrage umfasst höchstens 16 abgeschlossene
  15-Minuten-Slots.
- Der belegte RSCP-Historienvertrag enthält einen Platzhalter mit Graph-Index
  0 und danach die Nutzslots 1 bis N. Die Float32-Indizes werden mit enger
  Toleranz ausgewertet; zusätzlich muss der E3/DC-Summencontainer der über
  15 Minuten integrierten DC-Leistung entsprechen. Bei einer Abweichung bleibt
  der gesamte Abruf ungültig.
- Prognose- und Messzeilen sind unveränderlich und idempotent. Abgeschlossene
  Rohdaten außerhalb der letzten 90 UTC-Tage dürfen im Rahmen der
  Aufbewahrung gelöscht werden. Bei 256 MiB stoppt die Ablage fail-closed.
- Verglichen wird nur die prognostizierte E3/DC-DC-Erzeugung mit der
  E3/DC-DC-Historie. Ein externer AC-Wechselrichter wird ohne eigenen,
  ausdrücklich gebundenen Messnachweis nicht hinzugeschätzt.
- Messwert und Prognose müssen zur gleichen Topologie-Revision gehören.
- Ein Slot wird frühestens 60 Minuten nach seinem Ende ausgewertet. Dadurch
  werden noch nicht abgeschlossene oder noch nachlaufende Historienwerte nicht
  vorschnell bewertet.
- Zeitgrenzen werden als UTC-Zeitstempel gespeichert. Sommer- und
  Winterzeitwechsel erzeugen dadurch weder doppelte noch fehlende
  15-Minuten-Slots.
- Fehlende oder ungültige Werte bleiben `null`. Eine echte gemessene
  Nullerzeugung bleibt dagegen ein gültiger Messwert.
- Die zusammengefasste Auswertung entsteht höchstens einmal täglich. Das
  Webportal liest ausschließlich eine kleine, atomar veröffentlichte
  Zusammenfassung und niemals die private Rohdatenbank.

Die Diagnose verwendet bewusst verständliche, projekteigene Bezeichnungen:

- **Trefferabweichung**: durchschnittliche Größe der
  Abweichung je ertragsrelevantem 15-Minuten-Slot;
- **Richtungsversatz**: zeigt, ob die Prognose im Mittel
  unter oder über der später gemessenen Energie lag; ein positiver Wert
  bedeutet mehr gemessene als prognostizierte Energie;
- **Energiegewichtete Gesamtabweichung**: Summe der absoluten
  Slotabweichungen im Verhältnis zur gemessenen Energie;
- **Vergleichsabdeckung**: Anteil der auswertbaren Prognoseslots mit
  gültigem E3/DC-Historienwert.

Die Anzeige nennt zusätzlich den Zeitraum, die tatsächlich ertragsrelevanten
Fenster, Ausschlussgründe, die quadratische Fehlergröße (RMSE) und den
Vergleich mit einer einfachen Referenz einschließlich der jeweiligen Fallzahl.
Ein Vergleichstag kann auch nur teilweise beobachtet sein. Eine hohe
Vergleichsabdeckung beweist deshalb noch keinen vollständig erfassten
Tageslichtzeitraum; fehlende Prognose- und Messabschnitte werden getrennt
ausgewiesen.

Bis mindestens 96 ertragsrelevante Slots aus mindestens sieben
Vergleichstagen vorliegen, kennzeichnet das System die Werte als
**vorläufig**. Unvollständige Vergleichsabdeckung bleibt ebenfalls vorläufig.
Die Bezeichnung **Evidenzsammlung** bedeutet Datensammlung, nicht automatisch
ein lernendes Modell. Auch danach bleiben die Werte Diagnosewerte. Es gibt
keine automatische Modellauswahl und keine Rückwirkung auf die Regelung.

### Getrennte Prognosestufen und Tagesenergie

Neu erfasste Ausgaben halten Rohmodelle, die Prognose vor der bestehenden
Bias-Korrektur, den korrigierten Wert und die endgültige Anzeige getrennt fest.
Ausgabezeit, Parameterstand und Zielgröße werden unveränderlich gebunden.
Eine spätere Korrektur überschreibt keine frühere Prognose. Alte Archive ohne
diese Stufen werden nicht nachträglich mit vermeintlich damaligen Werten gefüllt.
Die bereits vorhandene Bias-Korrektur des Prognoseproduzenten bleibt unverändert;
die Diagnose ergänzt keinen zweiten Regelkreis.

Der Tagesenergievergleich verwendet ausschließlich eine bereits vor Beginn des
UTC-Tages erzeugte und erfasste, vollständige Tagesprognose. Er setzt außerdem
vollständige Istwerte voraus. Nachträglich zusammengesetzte Viertelstunden aus
verschiedenen Prognoseausgaben gelten nicht als solche Tagesprognose.

### Zusatzwechselrichter und Messqualität

Der AC-Anteil bleibt vom E3/DC-DC-Vergleich getrennt. Bei passend gebundener
externer Livequelle werden vorhandene Leistungsmessungen zeitgewichtet im RAM
zu Viertelstundenenergie integriert. Es entstehen keine zusätzlichen
Geräteabfragen. Abgeschlossene Werte werden gesammelt mit dem regulären
Diagnosezyklus archiviert, nicht bei jeder Liveprobe auf den Datenträger
geschrieben. Fehlende oder zu weit auseinanderliegende Proben bleiben Lücken.

Die externe E3/DC-Leistungsmeldung ist keine unabhängig bestätigte
Bruttoerzeugungsmessung des Zusatzwechselrichters. Ohne entsprechende Nachweise
bleiben Abregelung, Clipping und Abschaltung unbekannt. Deskriptive Vergleiche
der tatsächlich gemessenen Energie sind trotzdem sichtbar; daraus folgt keine
Freigabe zur automatischen Kalibrierung. Eine Punktprognose wird dadurch auch
nicht zu einer belegten P50-Prognose.

## Konfiguration prüfen

1. Im Konfigurationseditor den Bereich **PV und Prognose** öffnen.
2. Anlagenleistung, Flächen und Datenquelle prüfen.
3. Den angebotenen Verbindungstest ausführen.
4. Im Dashboard Zeitstempel und Datenquelle kontrollieren.
5. Nach einer Änderung mindestens einen vollständigen Aktualisierungszyklus
   abwarten.

## Datenschutzgerechte Diagnose

Für Supportfälle das redigierte Diagnosepaket der Installationszentrale
verwenden. Vor der Weitergabe insbesondere Standort, Anlagenkennungen,
Zugangsdaten, lokale Adressen und reale Messreihen prüfen. Rohe
Konfigurationen, Logs oder Datenbanken nicht öffentlich bereitstellen. Das gilt
auch für die privaten Rohdaten der Prognosediagnose unter
`/var/lib/e3dc-control/forecast-evidence/`, weil sie reale Erzeugungs- und
Prognoseverläufe enthält. Dieser Pfad liegt absichtlich außerhalb des
Webverzeichnisses.

Unter Docker wird derselbe Pfad als eigenes benanntes Volume eingebunden. Die
Diagnose bleibt auch dort standardmäßig ausgeschaltet. Erst nach
`forecast_diagnostics_enable=1` und einem Containerneustart beginnt der
niedrig priorisierte Diagnosedienst mit der rein lesenden Historienabfrage. Bei
`Aus` bleibt eine eventuell vorhandene private Datenbank unangetastet und die
Weboberfläche kennzeichnet die Diagnose ausdrücklich als ausgeschaltet.
Beim Wechsel zwischen Bare Metal und Docker werden diese privaten Rohdaten
nicht übertragen; die Diagnose beginnt im neuen Laufzeitmodell bewusst mit
einer frischen Vergleichshistorie.

Typische Prüfungen auf dem eigenen System:

```bash
systemctl status e3dc-weather-manager --no-pager
systemctl status e3dc-storage-simulator --no-pager
systemctl status e3dc-forecast-evidence --no-pager
```

Die Installationszentrale zeigt zusätzlich, ob Quelle, Zeitstempel und
Prognosedienst verfügbar sind. Ein Neustart oder eine Reparatur sollte erst
nach einem Backup und über den vorgesehenen Installerweg erfolgen.
