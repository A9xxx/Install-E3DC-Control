# Changelog

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
