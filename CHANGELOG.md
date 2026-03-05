# Changelog

## [2026.03.04] - Wallbox-Status, Diagramm-Details & Installer-Fixes

### 🔌 Wallbox & UI
*   **Status-Visualisierung:** Die Wallbox-Kachel zeigt nun an, ob das Auto verriegelt ist (Schloss-Icon) und in welchem Modus (WBMode) geladen wird.
*   **Phasen-Erkennung:** Automatische Ermittlung und Anzeige der aktiven Phasenanzahl (1-ph / 3-ph) beim Laden.
*   **Sperr-Status:** Ein gelbes Auto-Icon signalisiert nun "Angesteckt & Verriegelt", auch wenn gerade nicht geladen wird.
   **Bugfix:** Korrektur der Farbumschaltung (Gelb -> Blau) beim Start des Ladevorgangs in der Wallbox-Kachel.

### 📊 Diagramm & Details
*   **Erweiterte Details:** Beim Klick auf die Netz-Kachel (Desktop & Mobile) wird nun zusätzlich zu den Einzelphasen auch die Gesamtleistung von Netz und Wechselrichter (WR) angezeigt.
*   **Live-Diagramm:** Im Netz-Detail-Diagramm (`plot_live_history.py`) werden nun zwei neue Linien gezeichnet: "Netz Gesamt" und "WR Gesamt" (gestrichelt), um die Summenleistung besser zu visualisieren.

### 🔧 Installer & Updates
*   **Portabilität:** Der Installer und das Update-System (`self_update.py`) unterstützen nun beliebige Installationsverzeichnisse (z.B. Git-Clones). Updates werden immer im aktuellen Verzeichnis ausgeführt, statt stur `~/Install` zu erzwingen.
*   **Konflikt-Erkennung:** `installer_main.py` warnt nun beim Start, wenn eine parallele Installation im Standardpfad (`~/Install`) gefunden wird, um Verwirrung bei Konfigurationen zu vermeiden.
*   **Sudo-Rechte:** Die Einrichtung der Web-Rechte (`permissions.py`, `diagrammphp.py`) wurde erweitert. Es werden nun zuverlässig beide Sudoers-Dateien (`010_e3dc_web_update` und `010_e3dc_web_git`) erstellt und mit dem korrekten, dynamischen Pfad zum Installer verknüpft.
*   **Bugfix:** Ein `UnboundLocalError` in `system.py` bei Neuinstallationen (wenn kein Backup vorhanden war) wurde behoben.
*   **Transparenz:** Der Installer zeigt beim Start nun explizit das Arbeitsverzeichnis und die geladene Konfigurationsdatei an.

### ⚙️ System & Stabilität
*   **Live-Grabber Service:** Umstellung des `get_live.sh` Skripts auf einen echten Systemd-Service (`e3dc-grabber`). Dies erhöht die Stabilität gegenüber der alten Screen-Lösung.
*   **Anti-Spike Logik:** Doppelte Absicherung gegen fehlerhafte "0"-Werte in Diagrammen:
    1.  **Atomares Schreiben:** Der Grabber nutzt nun `mv` für atomare Datei-Updates in der RAM-Disk.
    2.  **PHP-Validierung:** `get_live_json.php` filtert ungültige Lesevorgänge aktiv aus, bevor sie in die Historie gelangen.
*   **Ramdisk-Rechte:** Korrektur der Berechtigungen für `/var/www/html/ramdisk` auf `2775` (Setgid). Dies stellt sicher, dass neu erstellte Dateien automatisch der Gruppe `www-data` gehören.
*   **Watchdog-Cleanup:** Optimierung des Crontab-Eintrags für den täglichen Bericht (Parameter `status`), um Dateisystem-Fehler (`=5.0` Datei) zu beheben.
*   **Deinstallation:** Die Routine entfernt nun auch den neuen Grabber-Service sauber.

## [2026.03.03] - PWA & Caching Optimierung

### 📱 PWA (Progressive Web App)
*   **Separate Manifeste:** Desktop (`manifest.json`) und Mobile (`manifest_mobile.json`) nutzen nun eigene Konfigurationen, um die korrekte Startseite (`index.php` bzw. `mobile.php`) beim "Add to Homescreen" zu garantieren.
*   **Icon-Caching Fix:** App-Icons wurden in das Hauptverzeichnis verschoben und umbenannt (`app-icon-*.png`), um hartnäckige Cloudflare-Caching-Probleme zu umgehen und die Installation zuverlässig zu ermöglichen.
*   **Service Worker:** Grundlegende Integration (`sw.js`) hinzugefügt, um die PWA-Installationskriterien moderner Browser zu erfüllen.

### 🔧 System & Konfiguration
*   **Wurzelzähler-Support:** Berücksichtigung der Variable `wurzelzaehler` (0, 3, 6) für das korrekte Auslesen der Netz-Phasenwerte in der Live-Ansicht.
*   **Config-Bereinigung:** Automatische Erkennung und Entfernung von doppelten Variablen (Case-Insensitive) in der `e3dc.config.txt` durch die Rechteprüfung. Der Editor verhindert nun auch das Anlegen von Duplikaten.
*   **Venv-Sicherheit:** Das Python Virtual Environment wird bei Neuinstallationen (git clone) nun temporär gesichert und wiederhergestellt, um Datenverlust zu vermeiden. Der Pfad wird explizit in `e3dc_paths.json` gespeichert.
*   **Diagramm-Stabilität:** Verbesserte Prozess-Steuerung (Lockfile-Handling) für das Live-Diagramm, um Hänger beim Theme-Wechsel zu vermeiden.
*   **Sudo-Kompatibilität:** Korrektur der Pfad-Ermittlung in allen Installer-Skripten. Das Virtual Environment und Konfigurationsdateien werden nun auch bei Ausführung mit `sudo` korrekt im Benutzerverzeichnis (`/home/pi`) statt in `/root` gesucht.
*   **Web-Config Fix:** `installer_main.py` erzwingt nun das Anlegen der `e3dc_paths.json` mit korrekten Berechtigungen (`www-data`), falls diese fehlt. Dies behebt Probleme, bei denen PHP das venv nicht finden konnte.

## [2026.03.02] - Mobile UI & Chart-Optimierung

### � Interaktive Diagramme & Details
*   **Klickbare Kacheln:** Ein Klick auf PV, Batterie, Netz oder Wallbox öffnet nun eine detaillierte Diagramm-Ansicht für den jeweiligen Bereich (Desktop & Mobile).
*   **Detail-Daten:**
    *   **PV:** Anzeige der einzelnen Strings (Leistung & Spannung).
    *   **Netz:** Anzeige der einzelnen Phasen für Netzbezug und Wechselrichter.
    *   **Batterie:** Anzeige von Spannung und Stromstärke zusätzlich zur Leistung.
    *   **Wallbox:** Anzeige der einzelnen Phasenleistungen.
*   **Mobile Optimierung:**
    *   Diagramme passen sich nun dynamisch der Bildschirmgröße an (Responsive).
    *   Neue Zeitauswahl (6h, 12h, 24h, 48h) direkt über dem Diagramm.
    *   Detailwerte werden übersichtlich über dem Diagramm eingeblendet.
    *   Trennung von "Live-Status" und "Prognose" in eigene Reiter für mehr Übersichtlichkeit.

### ⚙️ System & Installer
*   **Venv-Migration:** Das Python Virtual Environment wird nun standardmäßig im Home-Verzeichnis (`~/.venv_e3dc`) erstellt, um Konflikte mit dem Git-Repository zu vermeiden. Alte Venvs im Programmordner werden automatisch bereinigt.
*   **Legacy-Cleanup:** Der Installer erkennt und entfernt nun veraltete Autostart-Einträge in `/etc/rc.local`, die zu doppelten E3DC-Instanzen führen konnten.
*   **Bugfixes:**
    *   Korrektur beim Auslesen negativer Werte bei Wechselrichter-Phasen.
    *   Verbesserte Skalierung der Diagramm-Achsen für negative Werte (Einspeisung/Entladen).

### �📱 Mobile Ansicht (`mobile.php`)
*   **Preis-Chart Logik:** Die Berechnung der Zeitlinien (Start von Heute/Morgen) wurde korrigiert. Sie basiert nun auf der relativen Position zur aktuellen Uhrzeit, wodurch die Beschriftungen auch bei Charts, die gestern beginnen, korrekt positioniert sind.
*   **Hintergrund-Visualisierung:** Tiefst- und Höchstpreise werden nun durch dezente, farbige Balken (Grün für Min, Rot für Max) direkt im Hintergrund-Chart markiert.
*   **Layout-Optimierung:**
    *   Die Min/Max-Preiswerte wurden in die oberen Ecken der Kachel verschoben, um die Lesbarkeit zu verbessern.
    *   Die Abstände in der Photovoltaik-Kachel (Prognose-Zeile) wurden für ein ausgewogeneres Design angepasst.
    *   Ein fehlendes Label für "Gestern" wurde im Preis-Chart ergänzt.

### ⚙️ Installer & Backend
*   **Intelligente Installation:** "Alles installieren" erkennt nun vorhandene Konfigurationsdateien, installiert das Diagramm-System automatisch mit und richtet den Watchdog (Silent) ein.
*   **Modulare Deinstallation:** Neues Menü zum gezielten Entfernen einzelner Komponenten (Watchdog, RAM-Disk, Webportal) oder des gesamten Systems.
*   **Service-Härtung:** Systemd-Services warten nun explizit auf eine aktive Netzwerkverbindung (`network-online.target`), um Startfehler zu vermeiden.
*   **Menü-Optimierung:** Untermenüs sind nun einfacher bedienbar (Nummerierung 1-N) und sicherer (keine globalen Hotkeys mehr).
*   **Diagnose:** Der Status-Check zeigt nun auch CPU-Temperatur und RAM-Disk-Status an. Der Notfall-Modus prüft zusätzlich auf fehlende Systempakete.

### 💾 Backup & Sicherheit
*   **Erweiterter Umfang:** Backups beinhalten nun auch Watchdog-Skripte, Systemd-Service-Dateien, `e3dc_paths.json` und alle Spezial-Konfigurationen.
*   **Sicherheits-Backup:** Vor jedem Rollback oder Update wird automatisch ein Backup des aktuellen Zustands erstellt ("Safety First").
*   **Prozess-Sicherheit:** Alle Datei-Operationen (Install, Uninstall, Restore) stoppen nun zuverlässig den laufenden Systemdienst, um Datenkorruption zu verhindern.
*   **Virtual Environment:** Volle Unterstützung für Python venv (`.venv`) bei Installation, Updates und Kompilierung, um Konflikte mit System-Paketen zu vermeiden (PEP 668).
*   **Smart-Install:** `install_all.py` erkennt nun automatisch eine bestehende venv-Umgebung und nutzt diese weiter, ohne erneut zu fragen.
*   **Venv-Flexibilität:** Der Name der Umgebung ist nun konfigurierbar (Standard: `.venv_e3dc`). Der Installer scannt nach vorhandenen Umgebungen und lässt den Benutzer wählen.
*   **System-Integration:** Status-Check, Notfall-Modus und Web-Interface (`helpers.php`) lesen den venv-Namen dynamisch aus der Konfiguration.
*   **Web-Update Fix:** Das System-Update über das Webportal läuft nun stabil durch (Headless-Modus für Rechtekorrektur), ohne bei Rückfragen hängen zu bleiben.
*   **Prognose-Korrektur:** Die PV-Prognose wird nun korrekt gegen GMT-Zeit abgeglichen. Das Diagramm zeigt die Zeiten wieder in lokaler Zeitzone (Berlin) an.
*   **Update-Benachrichtigung:** Nach einem Update (Erfolg oder Fehler) wird nun automatisch eine Telegram-Nachricht gesendet (sofern konfiguriert).
*   **Bugfix Telegram:** Doppelte Kodierung von Zeilenumbrüchen (`%0A`) in Benachrichtigungen behoben.

## [2026.03.01] - Watchdog-Optimierung & Bugfixes

### 🖥️ Installer & Menü-System
*   **Kategorien:** Das Hauptmenü ist nun übersichtlich in Bereiche unterteilt (Installation, Konfiguration, System, Erweiterungen, Backup).
*   **Suchfunktion:** Mit `s` kann im Menü nach Befehlen gesucht werden.
*   **Notfall-Modus (Menu 99):** Ein neuer Assistent führt bei Problemen automatisch Rechte-Reparatur, Service-Einrichtung und Watchdog-Check nacheinander aus.
*   **Erweiterter Status-Check:** Prüft nun auch Internetverbindung, zeigt Service-Logs bei Fehlern an und gibt konkrete Lösungsvorschläge.

### 🛡️ Watchdog (`install_watchdog.py`)
*   **Tageswechsel-Logik:** Die Datei-Überwachung (`{day}`-Platzhalter) sucht nun intelligent nach der *neuesten* passenden Datei, statt stur den aktuellen Wochentag zu prüfen. Dies verhindert Fehlalarme beim Tageswechsel (Mitternacht), wenn E3DC-Control aufgrund von Zeitzonen-Differenzen (GMT) noch in die alte Datei schreibt.
*   **Service-Reload:** Der `piguard`-Service wird nach Änderungen im Installer-Menü automatisch neu gestartet, um die neue Konfiguration sofort zu aktivieren.
*   **Log-Viewer:** Automatische Einrichtung der Leserechte für das Web-Interface, damit das Watchdog-Protokoll direkt im Browser angezeigt werden kann.
*   **Watchdog aktualisieren:** Um die neuen Funktionen zu aktivieren, muss der Watchdog einmalig neu installiert werden. Starte dazu den Installer, wähle "Watchdog & Telegram konfigurieren" und dann **Punkt 1 (Komplett neu installieren / reparieren)**. Deine bisherigen Einstellungen werden automatisch vorgeschlagen und können einfach mit Enter bestätigt werden.

### 📱 Web-Interface (Desktop & Mobile)
*   **Watchdog-Status:** Ein neues Schild-Icon in der Kopfzeile visualisiert den Status des Wächters:
    *   **Grün:** Alles OK, Dienst läuft.
    *   **Gelb:** Warnung (z.B. Protokoll-Datei veraltet).
    *   **Rot:** Dienst gestoppt oder nicht verfügbar.
*   **Interaktivität:** Ein Klick auf das Icon öffnet die letzten 50 Zeilen des Watchdog-Logs zur schnellen Diagnose.
*   **Online-Status:** Die "Online"-Anzeige ist nun auch in der mobilen Ansicht verfügbar.
    *   **Smart-Action:** Ein Klick auf das Badge aktualisiert die Daten sofort.
    *   **Selbstheilung:** Ist der Status "Offline" oder "Veraltet", bietet ein Klick darauf direkt den Neustart des E3DC-Services an.
*   **Cleanup:** Redundante Funktionen (Changelog-Modal, Diagramm-Update-Logik) wurden in die zentrale `helpers.php` ausgelagert, um den Code sauberer und wartbarer zu halten.
*   **Mobile Preis-Graph:** Intelligente Dateiauswahl: Ab 18 Uhr wird bevorzugt die Mittags-Datei (`awattardebug.11.txt`) geladen, um bereits die Preise des nächsten Tages anzuzeigen. Ab 6 Uhr morgens wechselt die Ansicht wieder auf die Nacht-Datei (`23.txt`) für den aktuellen Tag.
*   **Code-Refactoring:** Massive Aufräumaktion im Backend.
    *   **Zentralisierung:** Alle Hilfsskripte (`run_now.php`, `save_setting.php`, `status.php`, `run_update.php`, `archiv_diagramm.php`) wurden in die zentrale `helpers.php` integriert.
    *   **Vereinfachung:** `Wallbox.php`, `archiv.php` und `history.php` nutzen nun gemeinsame Funktionen aus `helpers.php` statt eigenen Code.
    *   **Installer:** Die Rechteprüfung (`permissions.py`) wurde an die neue Dateistruktur angepasst und prüft keine gelöschten Dateien mehr.

## [2026.02.28] - Service-Migration, Watchdog-Intelligenz & Update-Kontrolle

### ⚙️ System-Dienst & Autostart
*   **Migration zu Systemd:** E3DC-Control wird nun als echter Systemdienst (`e3dc.service`) verwaltet.
    *   Ersetzt den alten Crontab-Autostart für mehr Stabilität.
    *   Automatischer Neustart bei Abstürzen (`Restart=always`).
    *   Bereinigung von "toten" Screen-Sessions vor dem Start.
*   **Web-Steuerung:** Neuer Button "Service Neustart" im Web-Interface (Desktop & Mobile), um E3DC-Control bequem neu zu starten.

### 🛡️ Watchdog (`install_watchdog.py`)
*   **Hänger-Erkennung:** Der Watchdog kann nun überwachen, ob eine Datei (z.B. Logfile) regelmäßig aktualisiert wird. Stoppt die Aktualisierung (>15 Min), wird der Dienst neu gestartet.
*   **Dynamische Dateinamen:** Unterstützung für den Platzhalter `{day}` (z.B. `protokoll.{day}.txt`), der automatisch durch den aktuellen Wochentag (Mo, Di, ...) ersetzt wird.
*   **Gezielter Neustart:** Bei Problemen mit E3DC-Control (Screen fehlt oder Hänger) wird nur der Dienst neu gestartet, nicht mehr der ganze Raspberry Pi.
*   **Feedback:** Push-Benachrichtigung bei erfolgreichem Service-Neustart durch den Watchdog.
*   **Schnellstart:** Wartezeit beim Start von 5 Min auf 60 Sek verkürzt inkl. sofortiger Log-Meldung.
*   **Speicher-Warnung:** Überwachung des SD-Karten-Speicherplatzes (Warnung bei >90% Belegung).
*   **Bugfix:** Korrektur der `{day}`-Platzhalter-Ersetzung mittels `sed` für maximale Kompatibilität.

### 🔄 Update-System
*   **Erzwingen & Reset:** Neue Optionen beim Update:
    *   Installation erzwingen (Re-Install), auch wenn die Version aktuell ist.
    *   Lokale Änderungen verwerfen (`git reset --hard`) oder behalten (Stash).
*   **Sichtbarkeit:** Rote Badges im Dashboard (Zahnrad & Button) weisen auf verfügbare Updates hin.
*   **Konfiguration:** Automatische Update-Prüfung kann in der Config (`check_updates`) deaktiviert werden.

### 📱 UI & Komfort
*   **Mobile:** Optimierte Button-Stile (Outline), die erst bei Bedarf (z.B. Update verfügbar) farbig hervorgehoben werden.
*   **Installer:** Automatische Einrichtung der nötigen `sudo`-Rechte für den Webserver (`git`, `systemctl`).

## [2026.02.27] - UI-Feinschliff, Logik-Optimierung & Cleanup

### 📱 Mobile Ansicht (`mobile.php`)
*   **Bugfix:** Die Richtung des Preistendenz-Pfeils wurde korrigiert (war invertiert).
*   **Lesbarkeit:** Die Schriftfarbe der Batterie-Anzeige passt sich nun besser an den Light-Mode an.
*   **Animation:** Das Pulsieren bei Leistung wurde verlangsamt (Faktor 4) für eine ruhigere Optik. Die Wallbox pulsiert nun ebenfalls aktiv beim Laden.
*   **UX:** Der Diagramm-Button zeigt nun den Status ("einblenden"/"ausblenden") und passt sich dem Farbschema an.

### 🖥️ Desktop Dashboard (`index.php`)
*   **Daten-Aktualität:** Warnung ("Veraltet"), wenn die Live-Daten älter als 5 Minuten sind (z.B. bei Verbindungsabbruch).
*   **Performance:** Der automatische Diagramm-Refresh prüft nun intelligent, ob ein Update überhaupt nötig ist (15-Minuten-Raster), um den Pi zu entlasten.
*   **Optik:** Preis-Trendpfeil und Balken sind nun vollständig Dark/Light-Mode kompatibel.

### 🔌 Wallbox & UI (`Wallbox.php`, `config_editor.php`)
*   **Design:** Eingabefelder und Aktions-Buttons wurden modernisiert (abgerundet `rounded-pill`, fetter Rahmen) für bessere Bedienbarkeit auf Touch-Screens.
*   **Editor:** Verbesserte Lesbarkeit von Tooltips im Dark Mode und kontrastreichere Buttons.

### ⚙️ System & Logik
*   **Neuer Parameter:** `pvatmosphere` (Standard 0.7) erlaubt die Feinjustierung der PV-Sollkurve an die atmosphärische Dämpfung.
*   **Robustheit:** Der Konfigurations-Parser (`logic.php`) akzeptiert nun auch Kommas in Zahlenwerten (z.B. `15,4` kWp).
*   **Caching:** Aggressiveres Cache-Busting für Live-Daten und JavaScript-Dateien verhindert Anzeigefehler nach Updates.

### 📊 Diagramm-Generator (`plot_soc_changes.py`)
*   **Redundanz-Bereinigung:** Die Darstellung der Wallbox-Punkte wurde entfernt, da die neue `Wallbox.php` eine detailliertere Timeline bietet.
*   **Optik:** Die Kurven für PV, Wärmepumpe und Außentemperatur werden nun geglättet (`spline`) dargestellt, was das Diagramm ruhiger und moderner wirken lässt.
*   **Bugfix Sommerzeit:** Die Berechnung der Sommerzeitumstellung (DST) wurde korrigiert, um Fehler in Jahren zu vermeiden, in denen der 31. März ein Sonntag ist.
*   **Stabilität:** Das Parsing der Zeitstempel aus der `awattardebug.txt` wurde robuster gestaltet (mathematische Berechnung statt String-Splitting).
*   **Code-Qualität:** Zentrale Pfad-Konstanten eingeführt und ungenutzten Code entfernt.
*   **Mobile Darkmode:** Der Hintergrund des Diagramms wird im Mobile-Modus nun explizit dunkel gesetzt (`#1a1f29`), um Transparenz-Probleme in Iframes zu beheben.

### ️ Watchdog & Sicherheit
*   **Watchdog-Overhaul (`install_watchdog.py`):** Komplett überarbeiteter Installer mit interaktivem Menü. Ermöglicht nun die Änderung des Gerätenamens und der Telegram-Einstellungen ohne Neuinstallation.
*   **Täglicher Statusbericht:** Neuer, konfigurierbarer `DAILY`-Modus für `boot_notify.sh`, der Uptime und CPU-Temperatur meldet. Der Zeitpunkt ist im Installer frei wählbar.
*   **Telegram-Robustheit:** Umstellung auf `--data-urlencode` in `boot_notify.sh`, um Probleme mit Sonderzeichen und Leerzeichen in Nachrichten zu beheben.
*   **Multi-IP Überwachung:** Der Watchdog kann nun mehrere IP-Adressen (z.B. Router und Google DNS) überwachen. Ein Reboot erfolgt erst, wenn *alle* Ziele nicht erreichbar sind.
*   **Router-IP Konfiguration:** Die zu überwachende(n) IP-Adresse(n) können nun im Installer-Menü konfiguriert werden.
*   **Benutzer-Flexibilität:** Der Watchdog prüft nun dynamisch den Screen-Prozess des Installationsbenutzers (statt hardcoded `pi`), was die Kompatibilität mit anderen Benutzernamen erhöht.

### 🔧 Installer & Stabilität
*   **Crontab-Fix ("Quoting Hell"):** `permissions.py`, `ramdisk.py` und `screen_cron.py` nutzen nun temporäre Dateien statt Shell-Pipes, um Cronjobs zu schreiben. Dies behebt Abstürze bei Anführungszeichen oder Emojis in Befehlszeilen.
*   **Encoding:** Explizite `utf-8` Kodierung beim Schreiben von Systemdateien verhindert Fehler bei der Verwendung von Emojis.
*   **Flexibilität:** Die Rechteprüfung (`permissions.py`) toleriert nun angepasste Ausführungszeiten für den täglichen Statusbericht.
*   **Installer (`diagrammphp.py`):** Die Abfrage, ob die Wallbox im Diagramm angezeigt werden soll, wurde entfernt.
*   **Config (`diagram_config.json`):** Der veraltete Parameter `enable_wallbox` wurde aus der Konfiguration entfernt.
*   **Health-Check (`permissions.py`):** Erweiterte Prüfung, ob der Watchdog-Service (`piguard`) aktiv ist und automatischer Start bei Ausfall.

## [2026.02.26] - Bugfixes & UI-Verbesserungen

### 🐞 Bugfixes
*   **PV-Prognose (`mobile.php`):** Die Berechnung des theoretischen PV-Sollwerts wurde korrigiert. Sie berücksichtigt nun die atmosphärische Dämpfung (Air Mass), was zu deutlich realistischeren Werten bei tiefem Sonnenstand (morgens/abends) führt.
*   **Diagramm-Kompatibilität (`plot_live_history.py`):** Ein Fehler (`Invalid property: 'titlefont'`) wurde behoben, der auf Systemen mit neueren Plotly-Versionen (v4/v5+) zum Absturz der Diagrammerstellung führte.
*   **Desktop-Dashboard (`index.php`):** Ein Fehler wurde behoben, bei dem das Aktualisieren des Leistungsverlaufs-Diagramms fehlschlug.
*   **Wallbox-Anzeige (`Wallbox.php`):** Ein Anzeigefehler wurde korrigiert, durch den immer alle drei Ladekosten-Szenarien gleichzeitig sichtbar waren.
*   **Variablen-Groß-/Kleinschreibung:** Diverse PHP-Skripte und Python-Dateien wurden robuster gemacht, um Fehler durch inkonsistente Groß-/Kleinschreibung in Konfigurationsvariablen (z.B. `awmwst` vs. `AWMwSt`) zu verhindern.
*   **Self-Update (`self_update.py`):** Ein Fehler wurde behoben, der dazu führte, dass bei einem Self-Update bestehende `.json`-Konfigurationsdateien überschrieben wurden. Die unnötige Abfrage von bereits konfigurierten Werten wurde ebenfalls korrigiert.
*   **Ramdisk (`ramdisk.py`):** Der `systemctl daemon-reload` Befehl wird nun nach Änderungen an der `fstab` ausgeführt, um Systemwarnungen zu vermeiden und sicherzustellen, dass die Ramdisk korrekt eingebunden wird.

### 🔧 Installer & Wartung
*   **Robustheit:** Der Installer prüft nun vor dem Klonen, ob `git` installiert ist, und verhindert so Fehler bei einer unvollständigen System-Einrichtung.
*   **Benutzerfreundlichkeit:**
    *   Ein Fehler wurde behoben, durch den der Installer nach einem Selbst-Update erneut nach dem Benutzernamen fragte, obwohl dieser bereits konfiguriert war.
    *   Die Menü-Abfrage wurde personalisiert und zeigt nun den aktuellen Installationsbenutzer an (z.B. `Auswahl (pi):`).

### ✨ UI & UX-Verbesserungen
*   **Wallbox-Ladekosten (`Wallbox.php`):**
    *   Die Auswahl der Ladeleistung (z.B. 7.2, 11 kW) für die Kostenschätzung ist nun in der `e3dc.config.txt` (`wbcostpowers`) frei konfigurierbar.
    *   Die zuletzt gewählte Ladeleistung wird im Browser gespeichert und beim nächsten Besuch automatisch wiederhergestellt (Standard ist 11 kW).
    *   Die Tooltips auf der Lade-Timeline funktionieren nun auch auf Touch-Geräten (durch Antippen).
*   **Konfigurations-Editor (`config_editor.php`):**
    *   Es können nun direkt über die Weboberfläche neue Variablen zur `e3dc.config.txt` hinzugefügt werden.
    *   Die Lesbarkeit von Hinweistexten im Dark-Mode wurde verbessert.
*   **Mobile Historie (`history.php`):** Das Diagramm wird beim Wechsel auf den Reiter "Historie" nicht mehr automatisch aktualisiert, sondern erst nach einem Klick auf den "Update"-Button, um unnötige Ladevorgänge zu vermeiden.
*   **Design-Anpassungen:**
    *   Der "Steuern"-Button auf der Wallbox-Kachel im Desktop-Dashboard wurde durch ein dezentes Zahnrad-Icon ersetzt.

---

## [2026.02.25] - Dashboard 2.0 & Wallbox-Intelligenz

### 🖥️ Desktop Dashboard (`index.php`)
*   **Komplettes Redesign:** Neues Grid-Layout, das die volle Bildschirmbreite nutzt.
*   **Live-Daten Kacheln:** Echtzeit-Visualisierung von PV, Batterie, Hausverbrauch, Netz, Wallbox und Wärmepumpe.
*   **Intelligente Strompreis-Anzeige:**
    *   Dynamischer Balken mit Farbcodierung (Grün/Gelb/Rot) je nach Preisniveau (Günstig/Teuer).
    *   Trend-Indikatoren (Pfeile) zeigen steigende oder fallende Preise an.
    *   Anzeige der Tages-Minima und -Maxima.
*   **Multi-View Diagramm:** Nahtloser Wechsel zwischen SoC-Prognose, Live-Leistungsverlauf und Archiv-Ansicht direkt im Dashboard.
*   **Smart Polling:** Asynchrone Datenaktualisierung mit Lade-Animationen für ein flüssiges Nutzererlebnis.
*   **Routing:** Integrierte Navigation zu Unterseiten (Wallbox, Config, Archiv) ohne die Hauptseite zu verlassen.

### 🔌 Wallbox & Ladeplanung (`Wallbox.php`)
*   **Visuelle 24h-Timeline:** Neue rollierende Ansicht (zentriert auf "Jetzt"), die vergangene und geplante Ladefenster grafisch darstellt.
*   **Kostenvorschau:** Automatische Berechnung der voraussichtlichen Ladekosten für den geplanten Zeitraum (basierend auf aWATTar-Preisen).
    *   Szenarien für 7.2 kW, 11 kW und 22 kW Ladeleistung.
*   **Echtzeit-Status:** Neue Status-Card am Kopf der Seite zeigt sofort an, wenn aktiv geladen wird (inkl. Leistung).
*   **Robustheit:** Umstellung auf Parsing der maschinenlesbaren `e3dc.wallbox.out` für präzisere Datenverarbeitung.
*   **Auto-Refresh:** Die Seite aktualisiert sich automatisch, sobald ein neuer Ladeplan berechnet wurde.
*   **Quick-Actions:** Schnellzugriff für "Stop" (0h) und "Sofort Laden" (99h).

### 📱 Mobile Ansicht (`mobile.php`)
*   **Preis-Chart:** Ein Area-Chart im Hintergrund der Preiskachel visualisiert den Tagesverlauf.
*   **Warnsystem:** Blinkende Indikatoren warnen vor starken Preissprüngen in den nächsten Stunden.
*   **Konsistenz:** Übernahme der Farb-Logik (Grün/Rot) für Preisniveaus vom Desktop.

### 🔄 Update-System & Wartung
*   **Web-Update:** E3DC-Control kann nun direkt über das Web-Portal aktualisiert werden (Desktop & Mobile).
    *   Echtzeit-Fortschrittsanzeige im Modal-Fenster.
    *   Polling-Mechanismus verhindert Timeouts bei langsamen Verbindungen (Cloudflare-Fix).
    *   Visuelles Feedback (Grüner Haken / Rotes Kreuz) bei Erfolg/Fehler.
*   **Headless-Installer:** Der Installer unterstützt nun einen `--unattended` Modus für automatisierte Abläufe.
*   **BOM-Bereinigung:** Automatisches Entfernen von Windows-Steuerzeichen (Byte Order Mark) aus Skripten zur Vermeidung von Syntaxfehlern.

### 🛠️ Technik & Backend
*   **Self-Healing:** `run_now.php` erkennt und bereinigt nun automatisch verwaiste Lockfiles (> 5 Min), um Systemhänger zu vermeiden.
*   **Rechte-Management:** Der Installer (`check_permissions.sh`) prüft nun auch Schreibrechte für temporäre Web-Verzeichnisse (`tmp/`, `ramdisk/`).
*   **Performance:** Optimiertes Caching und Polling-Intervalle zur Entlastung des Raspberry Pi.

---

### ✨ Mehrwert der Änderungen
*   **Sofortige Klarheit (UX):** Keine Interpretation von Textlisten mehr nötig – ein Blick auf die Timeline oder das Dashboard genügt.
*   **Kosten-Transparenz:** Du siehst vorab, was dich das Laden kosten wird.
*   **Live-Feedback:** Direkte Rückmeldung über Aktionen ohne manuelles Neuladen der Seite.
*   **Professionelle Optik:** Einheiltiche Designsprache (Dark Mode, Bootstrap 5) über alle Ansichten hinweg.

---
*Generiert für E3DC-Control Release Candidate*
