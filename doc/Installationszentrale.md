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
- `Job-Test`: prüft den Ramdisk-Jobpfad ohne Systemänderung.
- `Installieren`: installiert ein optionales Modul erst nach expliziter
  Schreibfreigabe über den root-kontrollierten Modul-Launcher.
- `Neustart` / `Stop`: nutzt nur erlaubte Dienste aus dem Katalog.
- `Rückbau`: ist nur für optionale Module erlaubt und legt vorher ein Backup
  unter `/srv/e3dc-control-backups/web_installer/<Zeit>-<Aktion>-<Modul>` an.

## Einrichtung nach dem Speichern im Config-Editor

Der Config-Editor speichert die Konfiguration. Dienste richtet ausschließlich
die Installationszentrale ein.

- Braucht eine geänderte Einstellung einen fehlenden, gestoppten oder
  deaktivierten Dienst oder einen Neustart, erscheint vor dem Speichern der
  Dialog „Dienste aktualisieren“ mit den betroffenen Modulen. Angeboten werden
  nur Module, deren Einstellungen sich geändert haben.
- `Nur speichern` speichert ohne Einrichtung. `Speichern und in der
  Installationszentrale einrichten` speichert und öffnet die
  Installationszentrale, die den Einrichtungsplan selbst abarbeitet.
- Der Plan entsteht auf dem Server aus dem gespeicherten Stand. Er enthält
  höchstens die im Dialog bestätigten Module und ist an die gespeicherte
  Konfiguration und die angemeldete Sitzung gebunden. Ein nicht gestarteter
  Plan verfällt nach 30 Minuten.
- Abhängigkeiten kommen zuerst, etwa Luxtronik Live vor dem Wärmepumpen
  Manager. Je Schritt zeigt die Seite den Zustand (Ausstehend, Warten, Läuft,
  Abgeschlossen, Fehlgeschlagen, Blockiert, Übersprungen) und den Grund.
- Ist eine Abhängigkeit installiert, aber noch nicht aktiv, wartet der Schritt
  bis zu 60 Sekunden. Fehlt ihre Unit, wird er sofort blockiert. Ein Fehler
  überspringt nur die abhängigen Schritte; unabhängige Schritte laufen weiter.
  `Erneut versuchen` wiederholt nur fehlgeschlagene, blockierte und
  übersprungene Schritte.
- Nach einem Neuladen setzt die Seite einen gestarteten Plan fort, auch wenn
  die Installationszentrale über das Menü geöffnet wird; erledigte Schritte
  laufen nicht noch einmal. Liegt der letzte Schritt mehr als 30 Minuten
  zurück, läuft der Plan erst nach „Einrichtung fortsetzen“ weiter.
  Wird die Konfiguration während des Plans
  erneut gespeichert, hält die Einrichtung mit „Konfiguration wurde geändert –
  Einrichtung angehalten. Konfiguration erneut speichern.“ an. Ein bereits
  laufender Modulauftrag wird dabei nicht abgebrochen.
- Dienstschritte nutzen dieselbe Schutzschicht wie die Dienstkarten. Für
  Kerndienste wie die Live-Daten plant die Einrichtung nach einer
  Konfigurationsänderung nur einen Neustart. Ein Start oder Neustart gilt erst
  als erfolgreich, wenn der Dienst danach aktiv ist.
- Hochverfügbarkeit und Shadow richtet der Plan nicht ein; der Hinweis nennt
  Konsole 49 bzw. das Systemupdate. Im Docker-Betrieb gibt es keinen
  Einrichtungsplan, der Hinweis verweist auf den Compose-Weg.
- Der Raw-Import im Config-Editor zeigt nach dem Import die betroffenen Module
  an und verwendet nach der Bestätigung denselben Plan.

## Rechte und Systemreparatur

- `Nur Rechte prüfen`: liest Besitzer, Gruppe und Modus der bekannten Pfade
  und meldet Befunde, ohne Inhalte oder Dienste zu verändern.
- `Rechte reparieren`: korrigiert ausschließlich Metadaten (Besitzer, Gruppe
  und Modus). Lokal geänderte Dateien benötigen eine tokengebundene Freigabe
  der vollständigen Dateiliste. Inhalte bleiben unverändert; kein Backup,
  Releasewechsel oder Dienstneustart.
- `System reparieren`: startet denselben root-kontrollierten Systemjob wie
  `System Update`: verifiziertes Vollbackup, kurzer Dienststopp,
  Stable-Abgleich, Rechteprojektion und Dienstneustart. Lokale Inhaltsänderungen
  werden vor dem Start angezeigt und benötigen eine ausdrückliche Freigabe.
  Dieselbe Version kann erneut installiert werden.

Im Docker-Betrieb ist `System reparieren` deaktiviert. Produktdateien und
Rechte kommen aus dem Image; bei Problemen erfolgt der Container-Neustart
oder die Neuerstellung auf dem Docker-Host.

## Service-Katalog und Pflichtdienste

Der zentrale Service-Katalog ist die führende Quelle für Installationszentrale,
Update, Rechte-Reparatur und Service-Wrapper. Pflichtdienste sind alle Module
mit `optional=False`. Ihre Units entsprechen der Kernliste
`INSTALL_CENTER_CORE_SERVICES` des Updaters. Die sieben Pflichtdienste sind:

- `e3dc-live`
- `e3dc-epex-manager`
- `e3dc-weather-manager`
- `e3dc-storage-simulator`
- `e3dc-storage-manager`
- `e3dc-websocket`
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

## Zugriffsschutz

Mit gesetzter Web-PIN sind die Installationszentrale und geschützte Steuerungsansichten nur nach Anmeldung erreichbar. Ohne Web-PIN kann jeder Rechner im Heimnetz Modulaktionen auslösen; die Zentrale zeigt dazu einen Hinweis am Seitenende. Nach fünf falschen PIN-Versuchen ist die Anmeldung für zehn Minuten gesperrt, auch mit der richtigen PIN. Schreibende Aktionen erfordern eine gültige Anmeldung und die Bestätigung über das jeweilige Formular.

## HA und Shadow

Die Rollenfelder sind schreibgeschützt: Rollenwechsel nur über Konsole 49. Für HA gilt „Einrichtung über Konsole 49“. Für Shadow gibt es keinen geführten Weg; die Rolle wird über das Systemupdate berücksichtigt. Die Karten dienen der Diagnose vorhandener Installationen. HA und Shadow sind nur auf Bare Metal vorgesehen.

## Regelruhe-Diagnose

„Regelruhe prüfen“ öffnet die Auswertung und startet eine lokale Analyse. Quelle, Zeitraum, Dienste und Musterabstand auswählen und die Auswertung erneut starten:

- „Aktuelle Verlaufsdaten“ liest Entscheidungsverläufe aus `logs`, `ramdisk` und `data`.
- „Aktueller Prozess“ begrenzt die Analyse auf die ermittelte Startzeit der gewählten Dienste. Im Docker wird automatisch „Historie: neueste Records“ vorgewählt. Ist eine Startzeit nicht ermittelbar, diesen Zeitraum auswählen.
- „Diagnose-ZIP“ wertet ein ausgewähltes lokales ZIP mit maximal 30 MB aus.
- „Letzte Auswertung“ liest das zuletzt gespeicherte Ergebnis aus der RAM-Disk; es wird keine neue Analyse gestartet.

Der Musterabstand beträgt wahlweise 60, 180, 300 oder 600 Sekunden. Speicher-Owner/State/Contract werden immer mit mindestens 600 Sekunden ausgewertet; das Fenster bleibt auch bei Auswahl von 600 Sekunden sichtbar. „Forum-Text kopieren“ übernimmt die bereinigte Ergebnisbeschreibung. Es werden keine Geräte gesteuert.

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

## Diagnosepaket

### Paket für Supportfälle

Das Diagnosepaket wird lokal erzeugt und enthält nur technische Hilfsdaten:

- maskierte Konfiguration ohne Passwörter, PINs, Tokens, E-Mail-Adressen und
  Standortwerte,
- Installer-Status, Moduldiagnose und Jobstatus,
- ausgewählte Logs,
- ausgewählte Ramdisk-Dateien wie Live-Daten, Speicherplan und Modulstatus.

Kontonamen von Geräte- und Dienstkonten sowie Fahrzeugpositionen werden
maskiert. IP-Adressen, der Hostname, MQTT-Topics, Fahrzeugnamen und
Gerätekennungen werden pseudonymisiert: Seriennummern von Speicher, Wallbox und
Zähler, MAC-Adressen, RFID-Tags, Fahrzeug-IDs und FIN, Klimageräte-IDs sowie
Anlagen-IDs von Tarif- und Prognosediensten. Ein Pseudonym nennt die Art der Kennung und eine Prüfsumme,
etwa `[serial-1a2b3c4d5e]`. Gleiche Kennungen erhalten in allen Paketen einer
Anlage dasselbe Pseudonym, damit sich Pakete derselben Anlage vergleichen
lassen. Der Schlüssel dafür liegt nur auf der Anlage
(`/var/www/html/data/diagnostic_pseudonym.key`) und wird nie mitgeliefert; ohne
ihn lassen sich die Kennungen nicht zurückrechnen. Wer die Zuordnung zu älteren
Paketen aufheben möchte, löscht diese Datei; beim nächsten Paket entsteht ein
neuer Schlüssel. Kann die Anlage die Datei nicht lesen oder anlegen, gilt der
Schlüssel nur für das eine Paket; das Manifest nennt das unter
`pseudonym_scope`. Modellbezeichnungen, Firmwarestände, Dienstnamen, Messwerte und
technische Zustände bleiben enthalten, weil sie für die Fehlersuche wichtig
sind. Die Laufzeitdateien der Anlage bleiben unverändert. Kennungen im Freitext
ohne erkennbaren Feldnamen werden nicht in jedem Fall erkannt; das Paket sollte
deshalb vor dem Versenden kurz geöffnet und geprüft werden.

## Docker

Das Diagnosepaket funktioniert auch im Docker-Betrieb, solange der Container
die üblichen Pfade lesen kann:

- `/var/www/html/data`
- `/var/www/html/logs`
- `/var/www/html/ramdisk`

Die ZIP-Erzeugung benötigt keine systemd-Rechte. Falls die PHP-Erweiterung
`ZipArchive` fehlt, nutzt die WebUI einen eingebauten ZIP-Fallback. Journal-
Zeilen können im Docker-Betrieb fehlen; das Diagnosepaket bleibt trotzdem
brauchbar.

Echte systemd-Installationen sind Bare-Metal-Funktionen. Docker-Module werden
über feste Prozess-Mappings und die Container-Startlogik behandelt, nicht über
freie systemd-Kommandos.

## Wärmepumpen- und Heizungsmodule

Die Installationszentrale unterscheidet den gemeinsamen Wärmepumpen-Manager
von den Live-Treibern und Zusatzheizungen:

- `heatpump` / `energy_manager`: gemeinsame Budget- und Regelentscheidung.
- `lux_live`: Luxtronik-Livewerte über WebSocket, wenn Luxtronik ausgewählt ist.
- `idm_live`: IDM-Livewerte über Modbus TCP, wenn IDM ausgewählt und eine IDM-IP gesetzt ist.
- `stiebel_live`: Stiebel-Eltron-ISG/WPM-Livewerte, wenn Stiebel Eltron ISG / WPM ausgewählt und eine ISG-IP gesetzt ist.
- `dimplex_live`: Dimplex-WPM-Touch/NWPM-Livewerte über Modbus TCP, wenn Dimplex WPM ausgewählt und eine Dimplex-IP gesetzt ist.
- `heizstab`: Manager für Heizstäbe, Shelly-Heizverbraucher und Shelly-Pro3EM-Messungen. Als Heizstab/BWWP darf er parallel zu Luxtronik, IDM, Stiebel oder Dimplex laufen. Bei WP-Typ Shelly Pro3EM ohne native WP ist er zugleich die einzige WP-Messung; der Wärmepumpen-Manager wird dann ohne SG-Ready-Shelly nicht installiert.

Die Live-Treiber arbeiten read-only. Für ihre Einrichtung werden unter
**Smart Home & Verbrauchsprognose** der Schalter **WP-/Verbrauchslogging aktivieren**,
der passende **Wärmepumpen Typ** und die zugehörige IP-Adresse gesetzt.
Für einen zusätzlichen Heizstab reichen dagegen **Heizstab aktiv** oder eine
Heizstab-/Shelly-Adresse; WP-/Verbrauchslogging ist dafür keine Voraussetzung.
Im Docker-Betrieb entstehen keine systemd-Services; die passenden Prozesse
starten beim nächsten Containerstart über die Container-Startlogik.
