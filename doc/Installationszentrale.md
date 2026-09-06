# Installationszentrale

Die Installationszentrale ist die sichere WebUI-Seite für Diagnose,
Konfiguration, Modulinstallation, Neustart und Rückbau von E3DC-Control
Diensten.

## Sicherheitsmodell

- Die WebUI nutzt nur den zentralen Service-Katalog.
- Freie Shell-Befehle, freie Dateipfade und beliebige Dienstnamen sind nicht
  erlaubt.
- Der alte C++-Dienst `e3dc.service` ist kein Installationsziel der WebUI.
- Core-Module wie Live-Daten, Storage Simulator und Storage Manager können
  diagnostiziert und neu gestartet, aber nicht über die WebUI deinstalliert
  werden.
- Optionale Module werden vor echten Schreibjobs gesichert. Der Rückbau
  entfernt nur die systemd-Unit und lässt Config, Historie, Logs und Scripts
  unangetastet.

## Standardaktionen

- `Diagnose`: liest Dienststatus, Konfiguration, Alive-Datei, Logauszug und
  Journal-Zeilen.
- `Konfiguration`: zeigt die für das Modul relevanten Config-Felder direkt in
  einem Dialog und speichert nur erlaubte Werte.
- `Job-Test`: prüft den späteren Ramdisk-Jobpfad ohne Systemänderung.
- `Installieren`: installiert ein optionales Modul erst nach expliziter
  Schreibfreigabe über den Installer-Wrapper.
- `Neustart` / `Stop`: nutzt nur erlaubte Dienste aus dem Katalog.
- `Rückbau`: ist nur für optionale Module erlaubt und legt vorher ein Backup
  an.

## Service-Katalog und Pflichtdienste

Der zentrale Service-Katalog ist die führende Quelle für Installationszentrale,
Update, Rechte-Reparatur und Service-Wrapper. Pflichtdienste sind alle Module
mit `optional=False`, aktuell insbesondere:

- `e3dc-live`
- `e3dc-epex-manager`
- `e3dc-weather-manager`
- `e3dc-storage-simulator`
- `e3dc-storage-manager`
- `e3dc-notifier`

Optionale Dienste werden nur geprüft oder repariert, wenn ihre systemd-Unit
installiert ist. Dadurch bleiben nicht konfigurierte Integrationen ruhig, aber
inaktive installierte Dienste werden als Reparaturkandidat sichtbar.

## Logging

Python-Dienste schreiben ihre eigenen rotierenden Logdateien unter
`/var/www/html/logs`. Der Wallbox-Manager schreibt `wallbox_manager.log` selbst;
seine systemd-Unit leitet `StandardOutput` und `StandardError` ins Journal. Das
verhindert doppelte Logzeilen, weil Python-Logger und systemd nicht parallel in
dieselbe Datei appendieren.

## Diagnosepaket

### Regelruhe und Datenqualität

Die Regelruhe-Diagnose trennt Entscheidungen, bestätigte Ausgangsbeobachtungen,
Änderungen dieser Ausgänge und bestätigte neue Ausgabetransaktionen.
Ein erneut gelesener unveränderter Ausgang ist kein neuer Schreibbefehl.
Wärmepumpen-Entscheidungszustände sind keine gezählten Verdichterstarts.

Kontextzeilen beschreiben einen Verlauf, sind aber keine Regelzyklen. Sie
werden separat gezählt und erzeugen allein keine fehlenden Live-Nachweise.
`EVIDENCE_LIMIT` nennt konkrete verbleibende Grenzen, etwa ein älteres
Datenformat, veraltete oder unbestätigte Rücklesungen, eine ungültige
Transaktionsbindung, beschädigte Zeilen oder zusammengefasste Zeiträume.

Zusammenfassungen mit null gemeldeten Zwischenänderungen ersetzen keine
lückenlose Folge einzelner Beobachtungen. Alte Daten werden nicht nachträglich
als bestätigte Hardwareausführung ausgegeben. Belegte Pfadkonflikte und echte
Ausgangswechsel bleiben auch bei eingeschränkter Datenqualität sichtbar.

### Paket für Supportfälle

Das Diagnosepaket wird lokal erzeugt und enthält nur technische Hilfsdaten:

- maskierte Konfiguration ohne Passwörter, Tokens, E-Mail-Adressen und
  Standortwerte,
- Installer-Status, Moduldiagnose und Jobstatus,
- ausgewählte Logs,
- ausgewählte Ramdisk-Dateien wie Live-Daten, Speicherplan und Modulstatus.

LAN-IPs, Dienstnamen und technische Zustände bleiben bewusst enthalten, weil
sie für die Fehlersuche wichtig sind. Das Paket sollte vor dem Versenden kurz
geöffnet und geprüft werden.

## Docker

Das Diagnosepaket funktioniert auch im Docker-Betrieb, solange der Container
die üblichen Pfade lesen kann:

- `/var/www/html/data`
- `/var/www/html/logs`
- `/var/www/html/ramdisk`

Die ZIP-Erzeugung benoetigt keine systemd-Rechte. Falls die PHP-Erweiterung
`ZipArchive` fehlt, nutzt die WebUI einen eingebauten ZIP-Fallback. Journal-
Zeilen können im Docker-Betrieb fehlen; das Diagnosepaket bleibt trotzdem
brauchbar.

Echte systemd-Installationen sind Bare-Metal-Funktionen. Docker-Module werden
über feste Prozess-Mappings und die Container-Startlogik behandelt, nicht über
freie systemd-Kommandos.

## Waermepumpen-Module

Die Installationszentrale unterscheidet den gemeinsamen Waermepumpen-Manager
von den Live-Treibern:

- `heatpump` / `energy_manager`: gemeinsame Budget- und Regelentscheidung.
- `lux_live`: Luxtronik-Livewerte, wenn im Frontend Luxtronik ausgewaehlt ist.
- `idm_live`: IDM-Livewerte, wenn im Frontend IDM ausgewaehlt und eine IDM-IP
  gesetzt ist.
- `stiebel_live`: Stiebel-Eltron-ISG/WPM-Livewerte, wenn im Frontend Stiebel
  Eltron ISG / WPM ausgewaehlt und eine ISG-IP gesetzt ist.

Bei Stiebel ist der Live-Treiber read-only. Er wird installiert, wenn
im Config-Editor unter **Smart Home & Verbrauchsprognose** der Schalter
**WP-/Verbrauchslogging aktivieren** eingeschaltet, der **Wärmepumpen Typ**
auf **Stiebel Eltron ISG / WPM** gestellt und die **ISG IP-Adresse**
eingetragen ist. Im Docker-Betrieb wird kein systemd-Service erzeugt; der
Prozess startet beim naechsten Containerstart aus der `entrypoint.sh`.
