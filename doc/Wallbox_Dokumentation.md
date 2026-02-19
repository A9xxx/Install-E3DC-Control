# Wallbox.php – Kurzdokumentation

## Zweck
Die Seite `Wallbox.php` bündelt die Wallbox-Steuerung in einer Oberfläche:
- Direktsteuerung (Sofortaktionen und direkte Ladedauer)
- Automatik-Einstellungen (`Wbhour`, `Wbvon`, `Wbbis`)
- Anzeige geplanter Ladezeiten aus `e3dc.wallbox.txt`

## Dateiquellen
- Wallbox-Datei (Planung): `e3dc.wallbox.txt`
- Konfiguration: `e3dc.config.txt`
- Installationspfad: wird über `helpers.php` (`getInstallPaths`) ermittelt

## Bereich 1: Geplante Ladezeiten
Die Anzeige liest `e3dc.wallbox.txt` und zeigt:
- Anzahl geplanter Slots
- Gesamtzeit (Slots × 15 Minuten)
- Gruppierung nach Datum (chronologisch)
- Kennzeichnung pro Slot:
  - `D` = Direktsteuerung
  - `A` = Ladezeitenautomatik

## Bereich 2: Direktsteuerung
Eingabe `zwei` (0–24 oder 99):
- `99` = Laden sofort starten
- `0` = Ladezeiten löschen

Buttons:
- Speichern
- Sofort (99)
- Löschen (0)

Hinweis:
Nach erfolgreichem Schreiben wird kurz gewartet (0,5 s).
Beim Löschen wird zusätzlich kurz gepollt, damit neu erzeugte Automatik-Zeiten sichtbar werden.

## Bereich 3: Automatik
Parameter:
- `Wbhour` (ganze Zahl, Stunden)
- `Wbvon` (nur volle Stunde, 0–23)
- `Wbbis` (nur volle Stunde, 0–23)

Beim Speichern:
1. Validierung der Eingaben
2. Optional: Wenn aktuelle Zeit bereits nach `Wbvon` liegt und Checkbox aktiv ist,
   wird `Wbvon` auf die nächste volle Stunde gesetzt.
3. Sicherheitslogik:
   - Nur bei `Wbhour > 0`
   - Bei Alt-/Konfliktfall wird zuerst `Wbhour = 0` geschrieben,
     5 Sekunden gewartet, danach die neuen Werte gesetzt.

## Case-Insensitivität
Konfigurationskeys werden unabhängig von Groß-/Kleinschreibung verarbeitet.
Beispiele: `Wbvon`, `wbvon`, `WBVON`.

## Wartungshinweise
- Bei Dateirechten-Problemen zuerst Besitz und Rechte der Dateien prüfen.
- Die Seite nutzt zentrale Hilfsfunktionen aus `helpers.php`.
- Frühere Logik aus `auto.php` wurde in `Wallbox.php` zusammengeführt.

## Typische Fehlerbilder
1. **Speichern funktioniert nicht (Datei-Zugriff verweigert)**
  - Ursache: fehlende Rechte auf `e3dc.wallbox.txt` oder `e3dc.config.txt`
  - Lösung: Besitz/Rechte korrigieren, z. B. `chown <user>:www-data` und `chmod 664`

2. **Nach „Löschen (0)“ sind Ladezeiten nicht sofort sichtbar**
  - Ursache: Automatik schreibt zeitversetzt wieder in `e3dc.wallbox.txt`
  - Lösung: Die Seite pollt bereits kurz nach; ggf. einmal neu laden

3. **Automatik-Speichern blockiert wegen Zeitprüfung**
  - Ursache: aktuelle Zeit liegt nach `Wbvon`, Checkbox nicht gesetzt
  - Lösung: Checkbox aktivieren, damit `Wbvon` auf die nächste volle Stunde gesetzt wird

4. **Wbvon/Wbbis werden als ungültig abgewiesen**
  - Ursache: Eingabe nicht als ganze Stunde
  - Lösung: Nur Werte `0` bis `23` verwenden (werden intern als `HH:00` gespeichert)
