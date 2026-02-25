1. Intelligentes History-Backup
Das Skript backup_history.php, das deine täglichen Sicherungen erstellt, wurde grundlegend überarbeitet. Es kopiert nicht mehr die gesamte 48-Stunden-Datei, sondern liest sie intelligent aus und speichert nur die Daten des jeweils gestrigen Tages. Außerdem setzt es automatisch die korrekten Dateiberechtigungen (pi:www-data), damit der Webserver die Backups auch lesen kann.

2. Korrekte Anzeige von Archiv-Diagrammen
Das Hauptproblem, dass beim Auswählen eines alten Datums im Dropdown-Menü trotzdem die Live-Daten angezeigt wurden, ist behoben. Die gesamte Kette von der Weboberfläche (history.php) über das Steuer-Skript (run_live_history.php) bis zum Python-Skript (plot_live_history.py) wurde korrigiert. Das Python-Skript akzeptiert jetzt einen Dateipfad und zeichnet genau die Daten, die ihm übergeben werden.

3. Optimierungen am Diagramm
Die Darstellung des Diagramms selbst wurde mehrfach verbessert:

Korrekter Hausverbrauch: Der Verbrauch der Wärmepumpe wird nun korrekt aus dem Gesamthausverbrauch herausgerechnet.
Batterie-Logik: Die vertauschten Bezeichnungen für "Laden" und "Entladen" der Batterie wurden korrigiert.
Lesbarkeit: Die Linien für Netzbezug und Einspeisung sind jetzt dicker. Zudem beginnen alle Linien sauber auf der Nulllinie und "schweben" nicht mehr in der Luft.
Mobile Ansicht: Die Legende wurde durch kürzere Bezeichnungen und kleinere Schrift kompakter gestaltet, was die Darstellung auf Smartphones deutlich verbessert.
4. Verbesserte Systemprüfung (Installer)
Die permissions.py wurde erweitert, um die Stabilität des Systems langfristig zu sichern:

Neue Dateien: Alle heute geänderten und neu hinzugekommenen Dateien und Ordner (wie das Backup-Verzeichnis) werden nun von der Rechteprüfung erfasst.
Cronjob-Prüfung: Der Installer kann jetzt alle wichtigen Cronjobs (sowohl die periodischen als auch die @reboot-Jobs) überprüfen. Die Prüfung ist dabei so intelligent, dass sie nur die Cronjobs als "fehlend" meldet, deren zugehörige Skripte auch wirklich installiert sind. Das verhindert Fehlalarme bei optionalen Modulen.