# Mobile Historie

Die mobile Historie wird über `mobile.php?seite=history` geöffnet. `mobile.php` bindet dafür die ausgelieferte Ansicht `history.php` ein. Es werden keine separaten Diagrammprozesse oder nicht mitgelieferten Hilfsskripte benötigt.

## Bedienung

- Für Live-Daten stehen 6, 12, 24 und 48 Stunden zur Auswahl.
- Die Ansichten Leistung, PV, Batterie, Netz, Strompreis sowie – wenn konfiguriert – Wallbox und Wärmepumpe können direkt umgeschaltet werden.
- Der Update-Knopf lädt die gewählte Ansicht neu; beim Öffnen startet kein automatischer Hintergrundjob.
- Das Archiv zeigt vorhandene, vom System erkannte Tagesdateien als 24-Stunden-Ansicht.
- Die Werte können für die Darstellung auf Absolutwerte umgeschaltet werden.

## Messwerte im Leistungsverlauf

E3DC-Control speichert die Messzeit der Probe. Einzelne ungültige Messwerte
bleiben im Leistungsverlauf unbekannt; gültige Nullwerte bleiben erhalten.
Die Diagramme stellen fehlende Werte als Lücken dar. E3DC-Control speichert
eine Probe mit unveränderter Messzeit nicht erneut.

## Daten und Datenschutz

Die Diagramme werden im Browser aus den lokalen Systemdaten aufgebaut. Die dafür benötigten JavaScript- und CSS-Bibliotheken werden mit E3DC-Control ausgeliefert; die Historienansicht lädt keine Assets von externen CDNs.

Fehlen Archivtage, ist zuerst die im Installationscenter angezeigte Statistik-/Backup-Konfiguration zu prüfen. Es sind keine manuellen Cronjobs für veraltete Diagrammskripte einzurichten.
