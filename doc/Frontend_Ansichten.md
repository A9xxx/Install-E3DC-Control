# Frontend-Ansichten: Einfach und erweitert

## Dashboard-Layout

Für das Hauptdashboard stehen zwei produktive Layouts zur Verfügung:

- **Klassisch** nutzt die bewährte Anordnung.
- **Modern** nutzt die Grid-/Badge-Anordnung und hebt aktive Verbraucher sowie Netzbezug und Netzeinspeisung stärker hervor.

Die Auswahl wird als `frontend_variant=classic` oder `frontend_variant=modern` zentral gespeichert. Beide Layouts unterstützen die Informationsdichte `compact`, `normal` und `detail`. Ein Update darf eine bestehende Modern-Auswahl nicht auf Klassisch zurücksetzen.

Diese Layoutauswahl ist von den nachfolgend beschriebenen einfachen und erweiterten Bedienansichten zu unterscheiden.

E3DC-Control hat an den großen Bedienflächen zwei Ansichten:

- **Einfache Ansicht** für normale Einrichtung und täglichen Betrieb.
- **Erweiterte Ansicht** für alle Detailparameter, Diagnose- und Sonderfälle.

Die einfache Ansicht ist keine eigene Konfigurationsebene. Beide Ansichten schreiben in dieselbe zentrale Datei `data/e3dc_v4.json`. Die Umschaltung der Ansicht wird nur lokal im Browser gespeichert und ändert keine Anlagenlogik.

## Grundprinzip

Die einfache Ansicht zeigt nur Werte, die für einen stabilen Betrieb häufig gebraucht werden oder für technisch weniger versierte Nutzer verständlich sind. Alles, was tiefer in Geräteprotokolle, Hysterese, Dienstinstallation, MQTT, Speziallogik oder Diagnose eingreift, bleibt in der erweiterten Ansicht.

Die erweiterte Ansicht bleibt die vollständige bisherige Ansicht. Sie enthält die Suche, alle Parametergruppen, Systemwerkzeuge und die detaillierten Assistenten.

## Config-Editor

Die einfache Config-Ansicht ist als Raster aufgebaut:

1. **E3DC Verbindung**
   IP-Adresse, RSCP-Port und Zugangsdaten.

2. **Speicherbetrieb**
   Speichergröße, Hausreserve, Ladeleistung, Einspeiselimit, PV-Kurve und Pre-Dump.

3. **Wallbox**
   Aktivierung, Wallbox-Typen und IP-Adressen der Ladepunkte. Ladeprofile, Phasen, Zeitverhalten und Treiberdetails bleiben in der erweiterten Ansicht und auf der Wallbox-Seite.

4. **Wärmepumpe / Verbraucher**
   Aktivierung, Automatik, erkannter Typ und IP-Adresse. Wenn bereits eine native Wärmepumpe konfiguriert ist, zeigt die einfache Ansicht den erkannten Typ und die IP an, zum Beispiel `Luxtronik · 192.0.2.88`.

5. **Tarif**
   Fester Tarif, EPEX, Tibber, Octopus Heat oder Spezialtarif. ENTSO-E kann als 15-Minuten-Fallback für SMARD hinterlegt werden. Spezialtarife werden direkt in der normalen Config gespeichert, nicht mehr in einer separaten `e3dc.strompreise.txt`.

6. **Standort & PV**
   Standort und erste PV-Fläche für die Prognose.

## Wärmepumpe, BWWP und Heizstab

Eine native Wärmepumpe und ein Zusatzverbraucher sind fachlich getrennt:

- **WP-Assistent**
  Öffnet die erweiterte Wärmepumpen-Gruppe. Dort wird eine vorhandene Wärmepumpe geprüft, aktiviert oder der Typ gewechselt. Das betrifft Luxtronik, IDM, Stiebel ISG oder Dimplex.

- **BWWP/Heizstab**
  Öffnet direkt den Zusatzverbraucher-Bereich. Dort wird ein Heizstab oder eine Brauchwasser-Wärmepumpe ergänzt, ohne die vorhandene native Wärmepumpe zu ersetzen.

- **Dienste**
  Öffnet die Installationszentrale. Sie prüft, ob der passende Dienst fehlt, gestoppt oder wegen geänderter Konfiguration neu gestartet werden muss.

Wichtig: Eine vorhandene native Wärmepumpe darf beim Hinzufügen eines Heizstabs oder einer BWWP nicht überschrieben werden. Der Heizstab ist ein Zusatzverbraucher.

## Wallbox-Seite

Die einfache Wallbox-Ansicht reduziert die Bedienung auf drei Entscheidungen:

- **Energiequelle**
  `PV`, `PV + Akku` oder `Netz erlaubt`.

- **Ladeabsicht**
  `Überschuss`, `Fertig bis`, `Sofort` oder `Beobachten`.

- **Ziel**
  Ziel-SoC oder direkte Lademenge in kWh. Nutzer ohne Fahrzeug-SoC-Auslesung
  können einen manuell bestätigten Start-SoC setzen oder ein kWh-Ziel verwenden.
  Ohne bestätigten Regel-SoC nutzt die Wallbox keine SoC-basierte Abschaltung.
  Ein frisch beobachteter openWB-Wert kann ab 5.4.5a mit Quelle und Alter als
  reine Anzeige erscheinen, wenn aktuelle Stecksession oder Fahrzeugprofil
  eindeutig passen. Er öffnet weder Planung noch Hardwarebefehl; andere
  unbestätigte Werte bleiben als `-- SoC` sichtbar.

Die Bedeutungen sind:

- **PV**
  Die PV-Kurve und der Hausspeicher bleiben führend. Die Wallbox nutzt stabilen Überschuss mit Hysterese.

- **PV + Akku**
  Das Auto darf PV und Hausspeicher bis zur sichtbaren Hausakku-Reserve nutzen. Bis zu dieser Untergrenze lädt das Auto normal; Netz bleibt aus. Unterhalb der Reserve stützt der Akku nur Hausverbrauch und Wärmepumpe.

- **Netz erlaubt**
  Netzstrom ist erlaubt. In `Fertig bis` wird preisorientiert geplant, in `Sofort` wird bewusst direkt geladen. `Überschuss` bleibt ein PV-/PV+Akku-Pfad und wird in der Oberfläche nicht mit `Netz erlaubt` kombiniert.

- **Überschuss**
  Lädt nur mit freiem PV-Überschuss. Bei `PV + Akku` darf der Hausspeicher bis zur sichtbaren Hausakku-Reserve stützen.

- **Fertig bis**
  Speichert Ziel und Zielzeit. Mit `Netz erlaubt` entsteht daraus ein preisorientierter Ladeplan; mit `PV + Akku` wird der bestehende Modus `Akku bis Abfahrt` genutzt.

- **Sofort-Preislimit**
  Dieses Limit gilt nur für spontanes Netzladen im Modus `Sofort`. Geplante Ladungen und automatisch berechnete Ladefenster werden dadurch nicht gekürzt, blockiert oder gelöscht.

  Jeder bewusst neu gespeicherte Sofortauftrag wird als eigene, einmalig
  quittierte Nutzerintention an den Wallbox Manager übergeben. Bei einer
  openWB Pro darf sie ausschließlich eine vollständig verbrauchte
  Startversuchs-Episode derselben aktuellen Stecksession neu öffnen. Sie
  umgeht weder das eingestellte Preislimit noch Nutzer-`Aus`, Not-Aus,
  Speicherreserve, Netzpunkt-, Phasen- oder Hardwaregrenzen.

- **Statuszeile**
  Die einfache Wallbox-Ansicht trennt bewusst zwischen Betriebsart und Planwerten. Bei `Überschuss` steht nur die aktive Betriebsart. Bei `PV + Akku` nennt sie die Hausakku-Reserve und den Hinweis, dass unterhalb davon nur Hausverbrauch und Wärmepumpe aus dem Akku gestützt werden. Bei `Fertig bis` werden Zielwert und Zielzeit angezeigt. Bei `Sofort` wird nur das Sofort-Preislimit angezeigt, wenn Netzstrom erlaubt ist.

- **Beobachten**
  E3DC-Control beobachtet nur und sendet keine Ladebefehle. Mit `PV` folgt der Speicher seiner normalen Ladekurve. Mit `PV + Akku` führt nur der Storage Manager den Speicher bis zur Hausakku-Reserve; wenn das Auto nicht mehr erkannt wird, gilt wieder die normale Ladekurve.

Die erweiterte Wallbox-Ansicht enthält weiterhin alle bisherigen Details wie Modi, Phasen, Schützschutz, Verzögerungen, Fahrzeugdaten und Treiberoptionen.

## Warum die Ansicht nicht global gesetzt wird

Die Ansicht ist bewusst browserlokal:

- Ein erfahrener Nutzer kann auf seinem Gerät die erweiterte Ansicht offen lassen.
- Ein anderer Nutzer sieht auf seinem Tablet oder Smartphone die einfache Ansicht.
- Es gibt keine versteckte globale Anlagenumschaltung nur durch UI-Navigation.

Die eigentliche Anlagenlogik hängt ausschließlich an den gespeicherten Config-Werten, nicht an der sichtbaren Ansicht.
