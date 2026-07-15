# V2H / V2G: Statusanzeige und technische Vorbereitung

E3DC-Control 5.3.2b erkennt und visualisiert bidirektionale Leistungsflüsse einer kompatiblen Wallbox. Ein negativer Wallbox-Leistungswert wird im Dashboard als Entladung aus dem Fahrzeug dargestellt. Bei openWB Pro werden außerdem gemeldete Fähigkeitsdaten wie `v2g_ready` sowie maximale Lade- und Entladeleistung in den Wallbox-Status übernommen.

## Funktionsumfang in 5.3.2b

- V2H-Leistungsfluss im Dashboard erkennen und darstellen;
- negative Wallboxleistung in Diagrammen von normaler Ladung unterscheiden;
- von einer kompatiblen Wallbox gemeldete V2G-Fähigkeits- und Leistungsdaten auslesen;
- vorhandene Fahrzeug-SoC- und Wallboxdaten als Grundlage für eine spätere Schutzlogik bereitstellen.

## Noch nicht freigegeben

E3DC-Control startet, regelt oder beendet in 5.3.2b keine V2H-/V2G-Entladung. Es gibt keinen allgemein freigegebenen V2H-Schalter, keinen V2G-Einspeisesollwert und keine zugesicherte SoC-Abschaltung über den Wallbox-Manager. Die Anzeige eines negativen Leistungsflusses ist daher keine Bestätigung, dass E3DC-Control die Entladung steuert.

Für den Betrieb gelten weiterhin ausschließlich die Schutzgrenzen und Freigaben des Fahrzeugs, der Wallbox, des Wechselrichters sowie des jeweiligen Herstellers. Nutzer dürfen sich für Mindest-SoC, Netzfreigabe oder Abschaltung nicht auf E3DC-Control 5.3.2b verlassen.

Eine spätere aktive Integration benötigt einen ausdrücklich unterstützten Treiber, bestätigte Befehlsrückmeldungen, eindeutige Hardware-Owner-Regeln, Fahrzeug- und Hausspeichergrenzen sowie eigene Gesamt- und Sicherheitstests. Bis dahin bleibt der Pfad read-only.
