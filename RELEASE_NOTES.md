# E3DC-Control v5.4.6

Dieses Update verbessert die Sicherheit der Docker-Dienste und Weboberfläche sowie die Darstellung von Speicherregelung und Ladekurven.

## Sicherheit und Docker

- Die EMS-Python-Dienste laufen im Container unter einem eigenen Konto ohne Root-Rechte. Start und Healthcheck prüfen die tatsächlichen Prozessrechte. Private Modelle und Prognosebelege werden vor dem Dienststart geprüft und auf das Laufzeitkonto übernommen. Administrative Initialisierung und Apache-Master behalten ihre erforderlichen Rechte.
- Fahrzeugnamen und Meldungen des E3/DC-Leistungsmessertests werden sicher als Text dargestellt. Ungültige Kartenkoordinaten erzeugen keinen Link.
- Eine optionale Bridge-Vorlage veröffentlicht nur den Webport. Hostnetz bleibt der Standard. Bridge setzt passende Geräteadressen voraus und unterstützt keine Matter-/mDNS-, Link-Local- oder Host-Loopback-Abhängigkeiten. Einrichtung und Einschränkungen stehen in der [Docker-Dokumentation](doc/Docker_Dokumentation.md#optionaler-bridge-betrieb).

## Speicheranzeige und Ladekurven

- Die Speicheranzeige unterscheidet **AUTO**, **DC only** und **DC + Zusatz-PV**. Sie kennzeichnet, ob die Ladegrenze bereits bestätigt ist. Die Grenze in Watt beschreibt den erlaubten Laderahmen, nicht die gemessene Batterieladung.
- Eine gültige Sollkurve bleibt in der kleinen Vorschau sichtbar, wenn die SoC-Prognose fehlt. Fehlende Prognosen werden benannt; veraltete oder ungültige Pläne bleiben ausgeblendet. **Prognosestart** bezeichnet den Start des erwarteten Speicherverlaufs und wird nicht mehr als Sollwert ausgegeben.
- Die Millisekunden-Zeitstempel der Ladekurvenprojektion werden auch unter 32-Bit-PHP korrekt verarbeitet. Docker benötigt weiterhin ein 64-Bit-System.

## Updatehinweise

Nach dem Update das Dashboard neu laden.

**Docker:** Vor dem Upgrade den verwendeten Host-Helfer `Installer/docker_compose_update.py` aktualisieren; ein neues Image ersetzt diese Hostdatei nicht. Benötigte Volumes bei gestopptem Container mit numerischen Eigentümern und Dateirechten auf dem Host sichern. Keine zusätzliche Compose-Option `user:` setzen.

Updates und Container-Neuerstellungen bei beendeter Fahrzeugladung und ohne laufenden Phasenwechsel durchführen. Private Wallbox-Steuerzustände überleben einen Neustart desselben Containers, aber keine Neuerstellung. Einen Rückfall auf ältere Root-Images ausschließlich über den aktuellen Host-Updater ausführen. Modelle aus dem unprivilegierten Betrieb werden dabei privat archiviert; das ältere Image trainiert bei Bedarf neu. Einzelheiten zu Sicherung, Migration und Rückfall stehen in der [Docker-Dokumentation](doc/Docker_Dokumentation.md).

Aus dem Bridge-Betrieb zuerst dieselbe aktuelle Runtime-Version im Hostprofil neu aufbauen und deren gesunden Start prüfen. Erst danach folgt der reguläre Rückfall auf das ältere Root-Image.
