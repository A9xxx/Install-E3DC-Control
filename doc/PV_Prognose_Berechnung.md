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
Konfigurationen, Logs oder Datenbanken nicht öffentlich bereitstellen.

Typische Prüfungen auf dem eigenen System:

```bash
systemctl status e3dc-weather-manager --no-pager
systemctl status e3dc-storage-simulator --no-pager
```

Die Installationszentrale zeigt zusätzlich, ob Quelle, Zeitstempel und
Prognosedienst verfügbar sind. Ein Neustart oder eine Reparatur sollte erst
nach einem Backup und über den vorgesehenen Installerweg erfolgen.
