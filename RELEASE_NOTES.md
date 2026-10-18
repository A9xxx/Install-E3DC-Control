# E3DC-Control v5.5.5b

E3DC-Control 5.5.5b ist ein Korrekturupdate auf Basis von 5.5.5a. Wärmepumpe und Heizstab erhalten bei unbekanntem Ladezustand des Hausakkus keine neuen Freigaben mehr, eine laufende Wärmepumpe behält dabei ihre Mindestlaufzeit. Für die E3/DC Multi Connect II lässt sich das Modell auswählen, damit nur noch die Wallbox die Phasen umschaltet. Ohne eigenen Standort nutzt die PV-Prognose eine neutrale Vorgabe.

## Änderungen

- **Wärmepumpe und Heizstab bei unbekanntem Ladezustand:**
  - Fehlende, beschädigte oder veraltete Livedaten sowie fehlende, ungültige oder unplausible Werte geben im automatischen Regelpfad des Heizstab-Managers keine neuen Starts, Leistungs-, Boost-, Markt- oder Vorab-Entladefreigaben. Bisher galt ein fehlender Wert als 0 %; bei „Mindest-SOC (%)“ = 0 konnte daraus eine Freigabe entstehen. Gültige 0 % bleiben ein Messwert.
  - Eine von E3DC-Control eingeschaltete Wärmepumpe am Shelly-Pro3EM-Relais läuft ihre Mindestlaufzeit zu Ende und erhält danach einen Aus-Auftrag, statt sofort abgeschaltet zu werden. Kehrt vorher ein gültiger Wert zurück, gelten die normalen Regeln; nach bestätigtem Aus gilt die Wiedereinschaltsperre.
  - Der Laufzeitbeleg bleibt über Dienstneustarts erhalten. Nach einem Neustart des Rechners beginnen Mindestlaufzeit und Wiedereinschaltsperre konservativ mit voller Dauer; ein Start kann sich dadurch verzögern.
  - Ein unabhängiger, automatisch geregelter Heizstab (ELWA, generisch per Modbus, Shelly) wird bei unbekanntem Ladezustand zurückgenommen; fremde Ausgänge und der manuelle Vollgasauftrag sind nicht betroffen.
- **E3/DC Multi Connect II:**
  - Neues Feld „Modell“ bei „Wallbox 1 Modell / API“ beziehungsweise „Wallbox 2 Modell / API“ = „E3/DC Multi Connect I/II“, Auswahl „nicht festgelegt“ (Standard, bisheriges Verhalten), „Multi Connect I“ oder „Multi Connect II“.
  - Mit „Multi Connect II“ schaltet im Sonnenmodus nur die Wallbox die Phasen; die eigene 6-A-Phasenerkennung von E3DC-Control läuft dann nicht mehr parallel.
  - Eine geänderte Modellwahl übernimmt der Wallbox-Manager mit einem einmaligen automatischen Neustart, wie bei einem Wechsel des Wallbox-Typs; im Docker-Betrieb startet dabei der Container einmal neu.
- **PV-Prognose:** Ohne eigenen Breiten- und Längengrad unter „Standort & PV-Prognose“ gilt eine neutrale Vorgabe (Mitte Deutschlands). Die Konfigurationsprüfung empfiehlt dann, den eigenen Standort einzutragen; sie blockiert nichts. Gespeicherte eigene Werte bleiben unverändert.

## Hinweise

- **Update:** Von 5.5.5a ist keine Konfigurationsänderung nötig. Wer eine Multi Connect II betreibt, wählt nach dem Update im Config-Editor im Feld „Modell“ „Multi Connect II“. Wer bisher keinen eigenen Standort eingetragen hat, trägt ihn unter „Standort & PV-Prognose“ nach, sonst gilt die Prognose für die neutrale Vorgabe.
- **Nachweisgrenze:** Die Änderungen sind mit Simulationen, Gerätemodellen und Regressionstests geprüft. Ein Praxisnachweis auf mehreren Anlagen liegt noch nicht vor; die Multi Connect II ist noch auf keiner realen Anlage erprobt.
- **Bekannte Grenzen der Wallbox-Regelung:** Die in 5.5.5a beschriebenen Grenzen bestehen weiter.
- **WebSocket-Dienst:** Der bisherige E3DC-WebSocket-Dienst wird mit Version 5.5.6 entfernt. Für externe Smart-Home-Anbindungen steht der MQTT-Hub mit den dokumentierten Topics zur Verfügung; das Webportal nutzt weiterhin HTTP-Abfragen.

## Updatehinweise

- **Bare Metal:** Wie gewohnt über „System Update“ aktualisieren und anschließend die Seite neu laden.
- **Docker:** Mit eingerichtetem Watchtower über „System Update“, sonst im bestehenden Compose-Ordner auf dem Host mit `sudo docker compose pull` und `sudo docker compose up -d` aktualisieren. Ein fester Pin wird bewusst auf `E3DC_IMAGE_TAG=v5.5.5b` geändert.
- **Von 5.5.5 oder älter:** Das Update führt direkt auf 5.5.5b; die Updatehinweise von 5.5.5a und 5.5.5 gelten weiter.

## Rückfall

Rückfall auf 5.5.5a: Docker in `.env` `E3DC_IMAGE_TAG=v5.5.5a` setzen und `sudo docker compose up -d` ausführen; auf Bare Metal ist das beim Update angelegte verifizierte Backup der Rückweg. Einzelheiten stehen in der [Rückfall-Dokumentation](doc/Rollback.md), Abschnitt „Rückfall von 5.5.5b auf 5.5.5a“. Unter 5.5.5a entfallen die oben genannten Korrekturen wieder.

---

# E3DC-Control v5.5.5a

E3DC-Control 5.5.5a ist ein Korrekturupdate „Hardwareschutz“ auf Basis von 5.5.5. Es verfeinert die Stromregelung der Wallbox, behebt ein Start-Stopp-Flattern bei der experimentellen zentralen PV-Aufteilung, sichert nativ angebundene E3DC-Wallboxen gegen einen nicht ankommenden Schutzstopp, einen Regelabbruch und einen verlorenen Phasenwechsel-Schutz nach Neustart und ergänzt Schutzfälle im Speicherregler und im Energy Manager der Wärmepumpe. Neue Einstellungen, Dienste oder Dateiformate gibt es nicht.

## Änderungen

- **Wallbox, Stromregelung:**
  - Die gewählte Schrittweite („WB1 Strom-Schritt“ / „WB2 Strom-Schritt“ unter „Wallbox & Fahrzeug“ → „Ladepunkte: Ströme und Phasen (optional)“, Auswahl „1 A“, „0,5 A“, „0,1 A“) bleibt bis zum Strombefehl erhalten. Sicherheitsgrenzen werden immer abgerundet.
  - Eine laufende Ladung erreicht den freigegebenen Zielstrom nach mindestens 45 Sekunden kohärenter Deckung und bestätigten Messzyklen ohne weitere 2-A-Treppe. Absenkungen warten nicht auf diese Bestätigung; Watt-, Hausanschluss- und Phasengrenzen bleiben bindend.
  - Kurze ungültige Wallbox-Messproben (höchstens 15 Sekunden) setzen die laufende Bestätigung einer Anhebung nicht zurück. Dabei wird weder ein Strombefehl gesendet noch ein ungültiger Wert übernommen.
  - Die Ladeleistung einer externen Wallbox (zum Beispiel go-e) wird nicht ein zweites Mal vom Budget abgezogen; ein Pendeln des Sollstroms nach einem Lastsprung entfällt.
- **Wallbox, einphasiger Wiederanlauf:** Bei umschaltfähigen Wallboxen wird die einphasige Fortsetzung geprüft, wenn der Anteil für das dreiphasige Mindestangebot nicht reicht. Die Sperre von mindestens 480 Sekunden nach einem Phasenwechsel der openWB Pro und die 600-Sekunden-Prognosebeobachtung vor dem Abstieg bleiben unverändert. Der Hilfetext zu „Beharrung 3p→1p (s)“ erklärt diese Verlängerung jetzt.
- **Zentrale PV-Aufteilung (experimentell, standardmäßig aus):** Nur bei eingeschalteter Option wirkt die Korrektur. Eine Wallbox im Modus „PV-Kurve ruhig“ startete teils für wenige Sekunden und wurde sofort wieder gestoppt, im Abstand von wenigen Minuten wiederholt. E3DC-Control rechnet die Aufteilung jetzt bereits bei gestecktem Fahrzeug; der Akku wird nur bei Ladung, bestätigtem Start oder ausreichendem Kandidaten zusätzlich begrenzt. Der Prognosestopp läuft bei aktivem Rahmen über das begrenzte Energiekonto; Schutzstopps bleiben sofort wirksam. Einstellung: „Speicher & Konfiguration“ → „Experimentelle Speicherregelung“ → „Zentrale PV-Aufteilung Akku/Wallbox (experimentell)“, Auswahl „Aus (Standard)“ oder „Ein“.
- **Native E3DC-Wallbox:**
  - Ein Schutzstopp (zum Beispiel wegen der Untergrenze „wbminSoC“) erreicht jetzt auch eine Box, die nach einer Freigabe selbständig mit hohem Strom zu laden beginnt. Zuvor konnte er beim ersten Versuch herabgestuft werden und danach verloren gehen, sodass das Fahrzeug ohne PV aus dem Akku weiterlud. Der Stopp bleibt jetzt vorgemerkt und wird gesendet, sobald ein frischer Status (höchstens 15 Sekunden) eine laufende Ladung belegt; bleibt die Wirkung aus, folgt er nach 30 Sekunden einmal erneut. Eine Box ohne belegte Ladung erhält keinen Stoppbefehl, weil dieser die Box umschaltet. Der Fehler bestand bereits in 5.5.5.
  - Ein unbekannter rückgemeldeter Stromdeckel bricht den Regelzyklus nicht mehr ab und erteilt keine Startfreigabe.
  - Die Sperre nach einem Phasenwechsel (mindestens 480 Sekunden) bleibt nach einem Dienst- oder Rechnerneustart erhalten.
  - Die Sitzungsenergie wird aus der gemeldeten Ladeleistung gebildet, wenn der Gerätezähler fehlt oder bei Leistung 0 meldet. Sie wird neustartfest gesichert, für beide Ladepunkte im Dashboard angezeigt und vom Ladeziel „Lademenge“ („Wallbox & Fahrzeug“ → „Fahrzeug und Ladeziel“ → „Ladeziel festlegen als“ → „Lademenge“) sowie der Fahrzeug-SoC-Schätzung genutzt. Messlücken bleiben als Untergrenze gekennzeichnet.
- **Speicherregler:**
  - Der Halt eines geschützten Entlade- oder Lademodus bei ungültigen Messwerten endet nach der Haltefrist (Standard 20 Sekunden) ab dem letzten gültigen Messzeitpunkt und verlängert sich nicht mehr bei jedem Folgewert; ein Uhrrücksprung verlängert ihn nicht.
  - Ein gemeldeter Inselbetrieb (Code 1 oder 4) hat auch bei einem unplausiblen Messwert Vorrang; gerundete oder nicht ganzzahlige Werte zählen nicht.
- **Wärmepumpe (Energy Manager):** Automatische Starts (PV-Überschuss, Preis/Markt, Vorab-Entladung, Erneuerung eines positiven Signals) beachten wie der Knopf „WW sofort“ eine fremde Sperre, also einen von außen ausgeschalteten Pause-Kontakt („EVU-Pause Shelly IP (Sperre)“) oder das Dimplex-Signal „Rot“. Unbekannte oder über 90 Sekunden alte Rücklesung hält den automatischen Start zurück. E3DC-Control nimmt ein eigenes SG-Ready-Angebot („SG-Ready Shelly IP (Boost)“) einmal zurück und schreibt den fremden Kontakt nicht; danach gelten Wiederanlaufsperre und neue PV-Qualifikation, auch nach einem Neustart des Energy Managers. Ein Pause-Kontakt ohne SG-Kontakt behält das bisherige Verhalten. Mindestlaufzeit eines laufenden Verdichters und Nutzer-Aus bleiben unverändert.
- **Diagnose:** Die Wallbox-Diagnose enthält je Ladepunkt den Block `fine_regulation` mit Dauer und Einspeisung im Zustand „drei Phasen, 0 A, einphasig gedeckt“, Freigabegrund, Phasenbeleg und Herkunft des letzten Stromausgangs.

## Hinweise

- **Update:** Von 5.5.5 ist keine Konfigurationsänderung nötig. Alle Einstellungen bleiben erhalten; es kommen keine neuen Einstellungen, Dienste oder Dateiformate hinzu.
- **Nachweisgrenze:** Die Änderungen sind mit Simulationen, Gerätemodellen und Regressionstests geprüft. Ein Praxisnachweis auf mehreren Anlagen liegt noch nicht vor.
- **Feine Stromstufen:** Eine Schrittweite unter 1 A nur wählen, wenn Wallbox und Firmware Dezimal-Ampere umsetzen.
- **Wärmepumpe:** Die Fremdsperren-Erkennung gilt für Shelly-Pausekontakte mit eingerichtetem SG-Kontakt und für Dimplex-SG. Andere Stopp-Wege der Wärmepumpe sind nicht verändert.
- **Bekannte Grenzen der Wallbox-Regelung:** Nach dem Start erhöht die Wallbox den Strom in bestätigten Schritten; bis dahin kann PV-Leistung ins Netz gehen. Bei einer kurzen Wolke kann eine Ladung in Einzelfällen stoppen und nach der Wiedereinschaltverzögerung neu starten. Die experimentelle „Zentrale PV-Aufteilung Akku/Wallbox“ (standardmäßig aus) kann die Ladegrenze des Akkus häufig wechseln und PV für einen noch nicht gestarteten Ladevorgang zurückhalten.
- **WebSocket-Dienst:** Der bisherige E3DC-WebSocket-Dienst wird mit Version 5.5.6 entfernt. Für externe Smart-Home-Anbindungen steht der MQTT-Hub mit den dokumentierten Topics zur Verfügung; das Webportal nutzt weiterhin HTTP-Abfragen. Die Luxtronik-Anbindung bleibt unverändert.

## Updatehinweise

- **Bare Metal:** Wie gewohnt über „System Update“ aktualisieren und anschließend die Seite neu laden. Meldet die Dienststeuerung eine tatsächliche Abweichung, nennt sie „System reparieren (Backup + Stable-Abgleich)“ als nächsten Schritt.
- **Docker:** Mit eingerichtetem Watchtower über „System Update“, sonst im bestehenden Compose-Ordner auf dem Host mit `sudo docker compose pull` und `sudo docker compose up -d` aktualisieren. Ein fester Pin wird bewusst auf `E3DC_IMAGE_TAG=v5.5.5a` geändert.
- **Von 5.5.4 oder älter:** Das Update führt direkt auf 5.5.5a; die Updatehinweise von 5.5.5 gelten weiter.

## Rückfall

Rückfall auf 5.5.5: Docker in `.env` `E3DC_IMAGE_TAG=v5.5.5` setzen und `sudo docker compose up -d` ausführen; auf Bare Metal ist das beim Update angelegte verifizierte Backup der Rückweg. Einzelheiten stehen in der [Rückfall-Dokumentation](doc/Rollback.md), Abschnitt „Rückfall von 5.5.5a auf 5.5.5“. Die Einstellungen bleiben dabei unverändert, weil 5.5.5a keine neuen Einstellungen einführt. Unter 5.5.5 entfallen die oben genannten Korrekturen wieder.

---

# E3DC-Control v5.5.5

E3DC-Control 5.5.5 verbessert das Zusammenspiel von Speicher, Wallbox und Wärmepumpe und lernt die Lastprognose aus dem tatsächlichen Verlauf. Die eingestellte Verbraucherpriorität gilt auch beim Start. Fehlende oder langsam gemeldete Wallboxdaten bremsen den Speicher nicht mehr allein wegen der Datenlücke. Automatische Wärmepumpen-Zusatzfreigaben beachten „Min SoC“. Neu sind die Anzeige einer MQTT-Wärmepumpe, zwei getrennte Klima-Messpunkte und die openWB-Anmeldung über verschlüsselte Verbindungen. Die zentrale PV-Aufteilung und die Notstrom-Vorhaltung sind experimentell und standardmäßig aus.

## Änderungen

- **Speicher und Wallbox:**
  - Die optionale zentrale PV-Aufteilung berücksichtigt zuerst den Speicher-Kurvenbedarf. Einstellung: „Speicher & Konfiguration“ → „Experimentelle Speicherregelung“ → „Zentrale PV-Aufteilung Akku/Wallbox (experimentell)“, standardmäßig „Aus (Standard)“.
  - Fällt eine Wallboxmessung aus, regelt der Speicher ohne diesen Ladepunkt weiter; Dashboard und Wallbox-Kachel warnen. Bei externen Wallboxen bleiben solche Zeiten als „Haus inkl. Wallbox (unbekannt)“ in der Bilanz und gehen nicht in das Lernen der Hausverbrauchsprognose ein.
- **Wallbox-Statusproben bis 30 Sekunden frisch:** Speicherregelung und Live-Anzeige akzeptieren gültige Wallbox-Statusproben bis zu einem Alter von 30 Sekunden.
  - Langsam antwortende openWB-Boxen lösen dadurch kein Umschalten der Entladesperre und keine falsche Meldung „Wallbox ohne Daten“ mehr allein wegen des Probenalters aus.
  - Die Frischegrenze für E3DC-Livedaten bleibt bei 15 Sekunden; ausdrücklich ungültige Messungen werden nicht gültig.
- **Wiederanlauf und kurze Messwertlücken:** Auch nach einem ausgeführten Direktstopp einer laufenden Ladung gilt der bestehende Wiederanlauf-Abstand. Kurz unplausible Messwerte können unter engen Bedingungen ein bestätigtes Angebot ohne neuen Schreibbefehl halten, höchstens zehn Sekunden innerhalb von 60 Sekunden. Schutzstopps bleiben unmittelbar wirksam.
- **Notstrom-Vorhaltung:**
  - Im selben Einstellungsbereich bietet „Notstrom-Vorhaltung“ die Auswahl „Aus (Standard)“, „Beobachten (keine Speicherbefehle)“ und „Halten“. Den Bedarf legen „Notstrom-Vorhalt am Abend (kWh Speicherinhalt)“, „Bezugszeit (Ortszeit)“ und „Versorgung mindestens bis (bestätigte Ortszeit)“ fest.
  - „Reserve-Netzladen (bei Halten)“ ist separat standardmäßig aus; dafür gilt „Höchster Bezugspreis (ct/kWh)“.
  - Die Funktion ist experimentell. Halten erhöht unter Umständen den Netzbezug; Nachladen verursacht Bezugskosten. E3DC-Control prüft weder die Notstromfähigkeit noch die Verluste der Anlage.
- **openWB und Fahrzeug:**
  - Eigene Konten und eine Zertifikatsbestätigung je Wallbox stehen unter „Wallbox & Fahrzeug“ → „Wallbox 1 – openWB Benutzerverwaltung“ beziehungsweise „Wallbox 2 – openWB Benutzerverwaltung“ zur Verfügung. Mit Konto wird der Status bevorzugt über verschlüsseltes MQTT gelesen.
  - Die Fahrzeuganzeige bevorzugt die laufende Stecksession. Cloud-Badges zeigen veraltete oder fehlgeschlagene Abfragen; Warnfarbe und Warnzeichen folgen dabei derselben Bedingung, auch bei zwischengespeicherten, veralteten Schätzwerten.
  - Bluelink pausiert Anmeldeversuche nach einer Kontoabmeldung.
- **Lastprognose:**
  - E3DC-Control trainiert die Haus- und Wärmepumpenprognose aus bis zu 30 Tagen gesicherter Minutenhistorie aller Wochentage und ergänzt den laufenden Tag. Unvollständige Tage, größere Messlücken und Abschnitte mit unbekanntem Wallboxanteil werden ausgeschlossen.
  - Gemessene Außentemperaturen fließen nur bei ausreichender Abdeckung ein. Fehlt für die Vorhersage die Wetterprognose, gilt das gemessene Trainingsmittel derselben Ortszeitstunde.
  - Die Plausibilitätsprüfung greift vor der Korrektur anhand früherer Prognoseabweichungen. Ein fehlgeschlagenes Nachtraining wird frühestens nach sechs Stunden wiederholt.
  - Unter „Standort & PV-Prognose“ stellt „Bisheriges ML-Training verwenden“ → „Ein“ das bisherige Verfahren wieder her; Standard ist „Aus“.
- **Wärmepumpe:**
  - Neue automatische PV-, Preis- und Tariffenster-Zusatzfreigaben beginnen erst ab „Min SoC (%)“ unter „PV-Überschuss-Boost“ → „Freigabe und Saison“. Bei aktivem Taktschutz behält ein laufender Verdichter seine belegte Mindestlaufzeit; vorübergehend sind dadurch Netzbezug oder Entladung unter Min SoC möglich. Schutzgrenzen bleiben vorrangig.
  - Neu ist die Auswahl „MQTT-Wärmepumpe (nur Anzeige)“ unter „Wärmepumpen Typ“; sie wird nicht automatisch aktiviert und steuert kein Gerät. Liegt nur die elektrische Summe aus Wärmepumpe und Zusatzheizung vor, wird ausschließlich `in/heatpump/power_w` gesendet; daraus entsteht kein COP.
  - Stiebel-ISG-Sollwerte werden nicht mehr als Isttemperaturen dargestellt; nicht belegte Messungen bleiben unbekannt.
- **Preissteuerung:**
  - Wirtschaftliches Netzladen berücksichtigt die tatsächlich benötigte Energiemenge und ausführbare spätere Ladefenster. Ohne eigene Bestandswerte beträgt die Mindestmenge 5 % der nutzbaren Speicherkapazität; die Werte stehen unter „Tarif & Strompreise“ → „Details zur Speicher-Netzladung“.
  - Spezialtarif und Octopus Heat erhalten ihre lokale Preisachse auch ohne Börsendaten. Ein verbindliches externes Luox-Steuerungsrecht sperrt automatisches Markt- und Reserve-Netzladen.
- **Klima, MQTT und Diagnose:**
  - Ein zweiter Klima-Messpunkt ergänzt Einzelwerte und Langzeitaufteilungen; Einrichtung über „Name Anlage 2“, „Zweiter Shelly-Zähler IP“, „Zählertyp Anlage 2“ und „Phase / Kanal“ im Klima-Bereich.
  - Unter „Schnittstellen & MQTT“ stehen „Werte zyklisch wiederholen (s)“ (Standard 0 = aus), „WP-Leistungs-Topic (optional)“ und „MQTT-Fahrzeug-Freigabeliste (optional)“.
  - Die standardmäßig ausgeschaltete „PV-Prognosediagnose“ unter „Standort & PV-Prognose“ ergänzt Quellen- und Messlücken je Fläche und Tracker, ohne die Regelung zu ändern.
- **Update und Betrieb:** Eine passende Web-Dienststeuerung wird nicht mehr allein durch einen alten Git-Stand blockiert. PiGuard-Bindung und Rechte zusätzlicher Webdateien werden beim Update berücksichtigt. Meldungen der Installationszentrale bleiben auch bei ungültigen Zeichen lesbar.

## Updatehinweise

- **Bare Metal:** Wie gewohnt über „System Update“ aktualisieren und anschließend die Seite neu laden. Meldet die Dienststeuerung eine tatsächliche Abweichung, nennt sie „System reparieren (Backup + Stable-Abgleich)“ als nächsten Schritt.
- **Docker:** Mit eingerichtetem Watchtower über „System Update“, sonst im bestehenden Compose-Ordner auf dem Host mit `sudo docker compose pull` und `sudo docker compose up -d` aktualisieren. Ein fester Pin wird bewusst auf `E3DC_IMAGE_TAG=v5.5.5` geändert.
- **Neue Optionen:** Zentrale PV-Aufteilung, Notstrom-Vorhaltung und Reserve-Netzladen werden nicht automatisch eingeschaltet. Eigene gespeicherte Mindestmengen für Netzladung bleiben erhalten.
- **MQTT-Fahrzeugliste:** Eine leere Liste lässt den bisherigen generischen Eingang offen.
  - Nach Aktivierung gelten ausgeschlossene oder noch nicht eindeutig zugeordnete alte Werte nicht als bestätigter Fahrzeug-SoC. Bei aktiver Zielplanung rechnet E3DC-Control dann mit 0 %; bestehende Netzladefenster bleiben möglich.
  - Vor Aktivierung die exakten Fahrzeugnamen prüfen.
- **Messwerte und Statistik:** Bei Stiebel können zuvor ersatzweise angezeigte Werte entfallen. Alte Klima-Zeiträume werden nicht nachträglich auf einzelne Anlagen verteilt. Bei fehlenden externen Wallboxdaten bleibt deren Verbrauch im Hauswert enthalten; für bekannte planbare Lasten steht „Tarif & Strompreise“ → „Geplante Lastfenster“ zur Verfügung.
- **WebSocket-Dienst:** Der bisherige E3DC-WebSocket-Dienst wird mit Version 5.5.6 entfernt. Für externe Smart-Home-Anbindungen steht der MQTT-Hub mit den dokumentierten Topics zur Verfügung; das Webportal nutzt weiterhin HTTP-Abfragen. Die Luxtronik-Anbindung bleibt unverändert.

## Rückfall

Die freigegebenen Rückfallziele bleiben unverändert. Vor einem Rückfall die [Rückfall-Dokumentation](doc/Rollback.md) lesen, Abschnitt „Rückfall von 5.5.5 auf 5.5.4“. Ein Rückfall auf 5.5.4 entfernt die in 5.5.5 neuen Einstellungen aus der aktiven Konfiguration; die vorher angelegte Sicherung enthält sie weiterhin. Ist an der openWB die Benutzerverwaltung oder HTTPS-Zwang aktiv, fällt die openWB-Anbindung unter 5.5.4 aus.

---

# E3DC-Control v5.5.4

5.5.4 bringt die Installationszentrale auf Bare Metal wieder in Betrieb: Module werden per Klick installiert und entfernt, die Dienste laufen mit dem Python-venv der Installation, und Karten, Dialoge und Konsolenmenüs sind gegen den tatsächlichen Stand geprüft. Die Speicherführung bei beobachteten Wallboxen und das Abendziel in `PV + Akku bis Untergrenze` sind neu geordnet, der Startknopf ändert den Lademodus nicht mehr, ein an der openWB gesetzter Stop gilt, und die openWB-Pro-Bereitschaft nach Ladeende hält nur noch mit Budget. Der PV-Boost der Wärmepumpe wird gehalten statt getaktet, das globale „Automatik darf steuern“ wirkt auch auf den Heizstab, die Lade- und Entladegrenzen folgen den Gerätegrenzen, unterhalb der Korridor-Untergrenze lädt der Speicher frei, und der Hausverbrauch der Tagesstatistik kommt aus der Energiebilanz. Neu und experimentell (Standard aus) ist eine Kostenhistorie je Viertelstunde. Dazu kommen Korrekturen für 32-Bit-Systeme, die Web-PIN-Sperrseite und eine „Jetzt“-Anzeige aus einer Quelle. Das Update benötigt keine zwingende Konfigurationsänderung.

## Installation und Installationszentrale

- Module werden auf Bare Metal wieder per Klick installiert und entfernt, über einen festen, root-eigenen Launcher mit Freigabeliste, Backup-Snapshot und zeitlich begrenztem Auftrag. Nach einem Raw-Import werden die Auto-Install-Regeln ausgewertet; „Blockiert“ erscheint nur mit Grund. Ohne Web-PIN gibt es einen Hinweis, keine Sperre. In Docker verweist die Oberfläche auf den Compose-Weg.
- Nach dem Speichern im Config-Editor richtet die Installationszentrale die betroffenen Dienste selbst ein: nur bestätigte, geänderte Module, Abhängigkeiten zuerst, Zustand und Grund je Schritt, Fortsetzen nach dem Neuladen und „Erneut versuchen“ nur für gescheiterte Schritte. Die Erstinstallation richtet die Rechte-Reparatur gleich mit ein. `pyserial` wird mit `pymodbus` installiert; ohne es startete der Heizstab-Dienst auf frischen Installationen nicht.
- Optionale Python-Dienste laufen mit dem Python-venv der Installation; ein Modul darf starten, sobald der Kerndienst installiert und aktiv ist.
- Karten zeigen den tatsächlichen Zustand: „Installiert, nicht aktiv“ statt „Installieren“, alle Blockergründe, Beschriftungen statt Schlüssel, kein „Installieren“ für Hochverfügbarkeit, Shadow und PV-Prognosediagnose; die HA-Rolle ist nur über Konsole 49 änderbar. Der MQTT-Hub und die Matter-Bridge melden ein Lebenszeichen, damit sie in Docker nicht fälschlich als „inaktiv“ erscheinen.
- Modul-Konfiguration ohne sichtbare Zugangsdaten, Wärmepumpentyp 6, Heizstab-Dialog mit Shelly-3EM-Feldern, PV-Prognosekarte mit Schalter „PV-Prognosediagnose“. Diagnosepaket und Backup-Status geben keine Pfade oder Geheimnisse preis.
- Konsole: Wärmepumpentyp und Adressen landen in der aktiven Konfiguration; Konsole 41 bietet alle Typen wie der Config-Editor und stoppt nur ausgeschlossene Dienste; Konsole 45 führt `npm ci` als Installationsbenutzer aus; Konsole 48 schreibt MQTT-Zugangsdaten nur noch in die aktive Konfiguration; die Vollentfernung läuft auch ohne Docker durch, entfernt den Rollenanker und fragt vor eingreifenden Optionen zurück; der Notfallmodus respektiert Hochverfügbarkeits-Standby und Shadow.
- Der WebSocket-Dienst ist Pflichtdienst; Karte und Dokumentation sagen, dass die Weboberfläche ihn derzeit nicht nutzt.

## Wallbox

- Beobachtete Wallboxen (openWB 2.x als Primary, `Aus`) wirken nur noch auf die Entladeseite des Speichers; die Ladekurve bleibt beim normalen Kurvenregler, `wbminsoc` hebt sie in `PV-Kurve ruhig` nicht an.
- openWB 2.x: Ladepriorität und eigener Lademodus werden gelesen, angezeigt und beratend bewertet, nie geschrieben. Ein an der openWB gesetzter Stop gilt als Nutzerpause bis zum Abstecken oder zur nächsten Wahl; ein dort gewähltes „Sofort“ bleibt stehen.
- `PV + Akku bis Untergrenze`: Reicht die PV nicht für Akku und Fahrzeug, endet die heutige Ladekurve bei `wbminsoc` statt beim normalen Abendziel, für alle Wallboxen.
- openWB Pro: Bestätigtes Abstecken beendet den Lückendeckel des Speichers. Ohne bekannte Korridorlage gibt es in `PV-Kurve ruhig` keine Akkustützung.
- Der Startknopf hebt Sperren auf und weckt das Fahrzeug, ändert aber den Lademodus nicht mehr; Schutzstopps bleiben; bei `Aus` sendet er keinen Befehl. Ein Wechsel in einen Modus mit Akkustützung hebt die Wiederanlaufsperre nach einem Kaskaden-Stop auf. Die Karte „Ladedauer (Stunden)“ der alten Wallbox-Ansicht ist entfernt.
- openWB Pro hält das Mindestangebot nach Fahrzeug-Ladeende nur noch mit ausreichendem Budget; ohne Budget folgt einmal 0 A, bei Rückkehr ein einzelner Strombefehl ohne Weckimpuls oder Phasenwechsel. Kurze Datenlücken halten ein stehendes Angebot höchstens 45 s, heben es aber nie an. Bei mehreren Wallboxen wird die fremde Verpflichtung genau einmal abgezogen. Der schnelle Start nach dem Abstecken bleibt.

## Speicher und Tarif

- Lade- und Entladegrenze folgen dem Gerätemaximum des Speichers; höhere oder unplausible Konfigurationswerte werden ersetzt und in der Konfigurationsprüfung gemeldet. Abgelehnte Leistungsvorgaben werden nicht mehr in jedem Zyklus wiederholt.
- Unterhalb der Korridor-Untergrenze setzt der Speicher keine Ladegrenze mehr unter den PV-Überschuss; E3DC lädt frei, mit Hysterese an der Untergrenze. Innerhalb des Korridors bleibt die Ladegrenze mit Abstand.
- Der Tarif-Halt gilt auch beim Netzboost der Wärmepumpe: Der Speicher deckt dann nur den Hausrest ohne Wärmepumpe.
- Feste Tarife mit kurzem günstigem Fenster: „günstig“ kommt aus den Tarifstufen; das Netzladen bleibt an das Fenster gebunden und lädt bis Ziel oder Fensterende. Die Vorschau im Config-Editor liest den Tagesplan.
- Der Planer rechnet ab dem aktuellen Zeitpunkt; vergangene Viertelstunden zählen nicht mehr als künftige Ladung. Vier Doppelzählungen im Speicherregler und im Diagramm sind behoben.

## Wärmepumpe und Heizstab

- Ist die Wärmepumpe nicht erreichbar, läuft der Energy Manager ohne Logik-Ausnahme weiter: ohne gültige Daten keine neue Freigabe, Mindestlaufzeiten bleiben.
- Der PV-Boost im Messwertbetrieb hält, solange die Voraussetzungen bestehen: keine fiktiven Lückenbuchungen, kein Takt, kein Ende bei erreichtem Ziel oder Saisonwechsel; nur der gemessene Netzanteil der Wärmepumpe zählt. Die Mindestlaufzeit bleibt als Hardwareschutz; `Aus` nimmt den Auftrag zurück, der Verdichter läuft seine Mindestlaufzeit zu Ende.
- Der Netzbezug-Stopp greift nur bei anhaltendem, der Wärmepumpe zugeordnetem Bezug (30 s über 2.500 W); ohne Einzelmessung zählt der gesamte Netzbezug abzüglich 200 W. Die Hausanschlussgrenze wirkt sofort.
- Tarif-Aufträge enden im Tarif-Halt nicht durch den SoC-Schutz innerhalb der Mindestlaufzeit; fehlende Luxtronik-Daten verlängern den Mindestlaufhalt um 60 s.
- SG-Ready: Die Heizgrenze sperrt die Freigabe nicht mehr; Warmwasser und Rücklauf werden unabhängig geprüft, ohne gültige Temperatur gibt es kein Signal.
- Die Quell-Erholung bremst einen PV-Boost nicht mehr bei deutlicher Einspeisung mit Prognosedeckung; geplante Fenster behalten sie.
- Saisonanzeige und Regelung zeigen dieselbe Saison; eine Rücknahme-Vormerkung aus einer einzelnen Lücke heilt, wenn die Anforderung zurückkehrt.
- Heizstab: Das globale „Automatik darf steuern“ wirkt auch auf Heizstab, ELWA, Shelly-Heizlüfter und das Wärmepumpen-Relais; ein beim Start eingeschaltet vorgefundenes eigenes Relais läuft seine Mindestlaufzeit zu Ende. Die ELWA wird je Zyklus über eine Verbindung angesprochen, die Zeitgrenze höchstens stündlich neu geschrieben, die Karte nennt Grund und nächsten Versuch statt eines Spinners. Zeigen Heizstab-Dienst und Energy Manager auf dasselbe Shelly-Relais, warnen Config-Editor und Installationszentrale; geschaltet wird wie bisher.

## Statistik, Kosten und Anzeige

- Der Hausverbrauch der Tagesstatistik stammt aus der Energiebilanz abzüglich jedes gemessenen Verbrauchers genau einmal. Tageswerte ab dem Update können deshalb von früheren abweichen; die letzten 30 Tage lassen sich mit einem Wartungsbefehl nachberechnen (Trockenlauf, Backup, Rückweg).
- Kostenhistorie je Viertelstunde (experimentell, Standard aus): festgeschriebene Preise, Akku-Mischpreis, Zeitraum Tag/Woche/Monat/Jahr mit Kalender, Zeile „Gesamt seit …“, CSV-Export; einmalige automatische Ergänzung der letzten 30 Tage nach dem Einschalten.
- Hybriddiagramm: Hinweisfeld abseits des Zeigers, Marktpreis der Direktvermarktung auch für vergangene Viertelstunden; saubere Preisflanken bei vergangenen Zeiträumen.
- 32-Bit-Systeme: Zeitachse der SoC-Prognose, Planbindung, Direktvermarktungs-Prüfungen und Kostenabrechnung sind korrigiert.
- Web-PIN: Die Sperrseite lädt nicht mehr ständig neu; nach der Anmeldung geht es auf die angeforderte Seite zurück, nur innerhalb der Anwendung; Matter- und Systemmetrik-Seiten zeigen die Sperrseite statt einer abgebrochenen Seite.
- „Jetzt“ auf Karte, Diagramm, Mobil und Kopfzeile kommt aus einer Quelle und nennt Korridorlage und tatsächliche Regelart; „E3DC regelt frei“ erscheint nur bei tatsächlich freier Regelung.
- Regelruhe-Diagnose per POST mit CSRF-Schutz, Docker-Vorauswahl „neuester Lauf“, ZIP bis 30 MB; Oberflächentexte ohne Du-Form.

## Updatehinweise

- **Bare Metal:** Das Update wie gewohnt über **System Update** in der Weboberfläche starten; danach die Seite neu laden. Der Modul-Launcher der Installationszentrale wird dabei eingerichtet.
- **Docker:** Mit eingerichtetem Watchtower über **System Update**, sonst auf dem Host im bestehenden Compose-Ordner mit `sudo docker compose pull` und `sudo docker compose up -d` aktualisieren. Einen festen Pin in `.env` bewusst auf `E3DC_IMAGE_TAG=v5.5.4` ändern. Die Modul-Installation per Klick gibt es in Docker nicht; die Oberfläche verweist auf den Compose-Weg.
- **Konfiguration:** Keine zwingende Änderung. Die Kostenhistorie bleibt standardmäßig aus (Schalter „Kostenansicht je Viertelstunde“ im Config-Editor unter Tarif & Strompreise). Liegt die konfigurierte Lade- oder Entladeleistung über dem Gerätemaximum des Speichers oder unter 300 W, gilt der Gerätewert; die Konfigurationsprüfung weist darauf hin.
- **Heizstab:** Steht „Automatik darf steuern“ auf Aus, sendet der Heizstab-Dienst ab diesem Update keine Schaltbefehle mehr an Heizstab, ELWA, Shelly-Heizlüfter oder das Wärmepumpen-Relais; bisher schaltete er trotz globalem Aus. Wer den Heizstab weiter automatisch betreiben möchte, lässt die Automatik eingeschaltet oder prüft die lokale Einstellung „PV-Auto“ des Heizstabs.
- **Statistik:** Der Hausverbrauch der Tagesstatistik wird ab dem Update nach der Energiebilanz berechnet. Vergangene Tage bleiben unverändert, bis der Wartungsbefehl aus `doc/Statistik_Wartung.md` ausgeführt wird (Standard ist ein Trockenlauf).
- **Wallbox:** Der Startknopf ändert den Lademodus nicht mehr. Wer bisher den Startknopf zum Moduswechsel genutzt hat, wählt den Modus auf der Wallbox-Seite. Die Karte „Ladedauer (Stunden)“ der alten Ansicht entfällt. Die openWB-Pro-Bereitschaft nach Fahrzeug-Ladeende hält nur noch mit Budget.
- **Wärmepumpe:** `Aus` nimmt den Auftrag sofort zurück, ein laufender Verdichter behält seine Mindestlaufzeit (Hardwareschutz).
- **Konsole:** Konsole 48 schreibt MQTT-Zugangsdaten nur noch in die aktive Konfiguration; bestehende Einträge in der Altdatei bleiben erhalten. Der WebSocket-Dienst bleibt installiert und ist Pflichtdienst, auch wenn die Weboberfläche ihn derzeit nicht nutzt.
- **Von 5.5.3c oder älter:** Das Update führt direkt auf 5.5.4. Dafür gelten die Updatehinweise von 5.5.3c, 5.5.3b, 5.5.3a und 5.5.3 weiter unten.

## Bekannte Grenzen

- Die Kostenhistorie ist experimentell: Der Erlös bei Direktvermarktung ist eine gekennzeichnete Näherung, alte Minutenhistorien enthalten keine historischen Marktpreise, und der Ressourcenbedarf auf kleinen Systemen (Raspberry Pi mit SD-Karte) ist noch nicht im Feld gemessen. Rückmeldungen dazu sind willkommen.
- Die Modul-Installation per Klick ist auf Bare Metal eingerichtet; Docker verwendet weiter den Compose-Weg.
- Hinter einem TLS-Proxy mit Pfadpräfix fällt der Rücksprung nach der PIN-Anmeldung auf das Dashboard zurück.
- Ein an der openWB gesetzter Stop wird nur als Nutzerpause erkannt, wenn der Statuswechsel innerhalb von 10 s beobachtet wird; bei längeren Abfragezyklen erscheint „Herkunft unbekannt“.
- Die Gerätegrenzen des Speichers aus den Systemdaten sind erst auf wenigen Anlagen und Firmwareständen bestätigt; ohne gültige Systemdaten gilt die Konfiguration wie bisher.
- Bei gleicher Shelly-Adresse und gleichem Relais für Heizstab-Dienst und Energy Manager gibt es nur eine Warnung; eine Besitzregel folgt in einer späteren Version.
- Innerhalb des Korridors bleibt die bisherige Ladegrenze mit Abstand; nur unterhalb der Untergrenze lädt der Speicher frei.
- Der WebSocket-Dienst wird von der Weboberfläche derzeit nicht genutzt; ob er entfällt oder wieder genutzt wird, ist offen.

## Rückfall

Die freigegebenen Rückfallziele bleiben unverändert. Für Docker gilt der in der Update-Policy ausgewiesene Docker-Rollback-Root; auf Bare Metal bleibt ein verifiziertes Datei-Backup der sichere Rückweg. Vor einem Rückfall die [Rückfall-Dokumentation](doc/Rollback.md) lesen. Ein Rückfall auf 5.5.3c oder älter stellt den bisherigen Hausverbrauch der Tagesstatistik nicht automatisch wieder her; die Wartungsbefehle haben einen eigenen Rückweg.

---

# E3DC-Control v5.5.3c

E3DC-Control 5.5.3c ist ein Korrekturupdate für die Einrichtung des Energy
Managers (Wärmepumpe, SG-Ready, Heizstab) bei einer Neuinstallation auf Bare
Metal. Wallbox-, Speicher-, Wärmepumpen- und Hardwarelogik entsprechen
unverändert 5.5.3b.

## Korrekturen

- **Energy Manager ließ sich nicht einrichten:** Beim Einrichten des Energy
  Managers brach der Installer mit „Pflichtpfad ist nicht eindeutig
  systemd-tauglich: …/bin/python3“ ab. Der Python-Interpreter einer normalen
  Programmumgebung (venv) ist eine Verknüpfung; deren Ziel wird an anderer
  Stelle bereits geprüft. Die Einrichtung akzeptiert ihn jetzt. Die
  Programmdateien der Dienste dürfen weiterhin keine Verknüpfung sein.
  Bereits laufende Energy-Manager-Installationen waren nicht betroffen.

## Updatehinweise

- **Bestehende Installationen:** Wie gewohnt über **System Update** bzw. das
  Docker-Update; eine Konfigurationsänderung ist nicht nötig. Ein fester Pin in
  `.env` wird bewusst auf `v5.5.3c` geändert.
- **Neuinstallation, bei der der Energy Manager abbrach:** Die übrige
  Installation ist vollständig. Im Webportal über **System Update** auf 5.5.3c
  aktualisieren, danach im Installationsordner `bash ./e3dc-setup` starten und
  im Expertenmenü den Energy Manager erneut einrichten.
- **Von 5.5.3a oder älter:** Das Update führt direkt auf 5.5.3c. Dafür gelten
  die Updatehinweise von 5.5.3b und älter weiter unten.

---

# E3DC-Control v5.5.3b

E3DC-Control 5.5.3b ist ein Korrekturupdate für die Neuinstallation auf
Bare Metal. Wallbox-, Speicher-, Wärmepumpen- und Hardwarelogik entsprechen
unverändert 5.5.3a.

## Korrekturen

- **Neuinstallation brach bei den Systemrechten ab:** Seit 5.4.5 verlangte die
  Einrichtung von Web-Wrapper und sudoers einen bereits installierten
  Stable-Updater, den es bei einer Neuinstallation noch nicht gibt. Die
  Installation endete in Schritt 1 mit „Der root-eigene Stable-Updater ist
  nicht sicher nutzbar“. Bei einer Neuinstallation werden Wrapper und sudoers
  jetzt einmalig aus dem geklonten Stand eingerichtet, solange noch keine
  dieser Dateien existiert. Bestehende Installationen behalten die Prüfung
  über den Stable-Updater.
- **Raspberry Pi OS Trixie (Debian 13):** Trixie legt neue Dateien
  gruppenbeschreibbar an. Die Schutzprüfungen lehnten den geklonten Ordner
  deshalb ab („Apache-Schutz für Daten-, Log-, Ramdisk- und Temp-Pfade konnte
  nicht aktiviert werden“). Der Installer entzieht den Programmdateien und dem
  Python-venv jetzt das Gruppen- und Fremdschreibrecht und legt Dateien des
  Installationsbenutzers mit sicheren Rechten an.
- **Watchdog auf Bookworm:** systemd 252 (Debian 12) meldet einen noch nicht
  eingerichteten Dienst anders als neuere Versionen; die Watchdog-Einrichtung
  brach daran ab. Das ist behoben.
- **systemd 257 (Debian 13):** Für noch nicht eingerichtete Dienste meldet
  systemd 257 weniger Angaben. Die Dienstprüfung und die Watchdog-Einrichtung
  werten das jetzt richtig aus, ohne die übrigen Prüfungen abzuschwächen.
- **Webportal bei der Neuinstallation:** Die Schutzprobe für Laufzeitordner
  akzeptiert für einen noch nicht angelegten Ordner eine fehlende Seite (HTTP
  404). Sobald ein Ordner existiert, gilt weiterhin ausschließlich „Zugriff
  verweigert“ (HTTP 403).
- **Erneuter Versuch nach einem Abbruch:** Ein abgebrochener
  Installationsversuch lässt sich mit derselben Anleitung erneut starten und
  wird fortgesetzt.

## Updatehinweise

- **Bestehende Installationen:** Wie gewohnt über **System Update** bzw. das
  Docker-Update; eine Konfigurationsänderung ist nicht nötig. Ein fester Pin in
  `.env` wird bewusst auf `v5.5.3b` geändert.
- **Abgebrochene Neuinstallation:** Im Installationsordner `git pull` ausführen
  und die Installation mit `bash ./e3dc-setup` erneut starten.
- **Von 5.5.3 oder älter:** Das Update führt direkt auf 5.5.3b. Dafür gelten
  die Updatehinweise von 5.5.3a und 5.5.3 weiter unten.
- **Rückfall:** Ein Rückfall auf 5.5.3a oder älter hebt diese Korrekturen
  wieder auf; laufende Installationen sind davon nicht betroffen.

---

# E3DC-Control v5.5.3a

E3DC-Control 5.5.3a ist ein Korrekturupdate für Docker-Installationen mit
Wärmepumpen-Steuerung. Wallbox-, Speicher-, Wärmepumpen- und Hardwarelogik
entsprechen unverändert 5.5.3.

## Korrektur

- **Neustart desselben Containers:** Nach einem Neustart des Rechners, nach
  `docker compose restart` oder nach `docker compose up -d` auf einen
  bestehenden Container blieben die Regeldienste gestoppt. Im Log stand
  „Private Laufzeitdaten konnten nicht sicher migriert werden“. Ursache: Die
  Startprüfung kannte die Zustandsdateien der Wärmepumpen-Steuerung im privaten
  Steuerordner nicht. Sie lässt jetzt genau diese drei Dateien samt ihrer
  temporären Dateien zu, mit bis zu 1 MiB je Datei. Die Zustände bleiben beim
  Neustart erhalten.
- **Verständliche Meldung:** Andere unbekannte Einträge sperren weiterhin den
  Start der Regeldienste. Die Meldung nennt jetzt die Art des Speichers und den
  Dateinamen, zum Beispiel
  `private_store_unknown_entry kind=control name=…`.

## Updatehinweise

- **Docker:** Wie gewohnt über den Knopf **System Update** (mit eingerichtetem
  Watchtower) oder auf dem Host mit `sudo docker compose pull` und
  `sudo docker compose up -d`. Läuft der Container bereits in der oben
  beschriebenen Schleife, ist die Weboberfläche nicht erreichbar; dann die
  beiden Befehle auf dem Host ausführen. Ein fester Pin in `.env` wird bewusst
  auf `v5.5.3a` geändert.
- **Bare Metal:** Nicht betroffen. Das Update kann wie gewohnt über
  **System Update** eingespielt werden; eine Konfigurationsänderung ist nicht
  nötig.
- **Von 5.5.2 oder älter:** Das Update führt direkt auf 5.5.3a. Dafür gelten
  die Updatehinweise von 5.5.3 weiter unten.
- **Rückfall:** Ein Rückfall auf 5.5.3 oder älter hebt diese Korrektur wieder
  auf.

---

# E3DC-Control v5.5.3

5.5.3 korrigiert die Zeitbasis der E3/DC-Historie und die Tagesbilanz, behebt eine dauerhaft gesperrte Wärmepumpen-Startfreigabe bei Stiebel ISG mit SG-Ready und gibt der Lastspitzenkappung Vorrang vor dem Tarif-Halt. Neu sind das experimentelle Tariffenster-Heizen (Standard Aus), ein messwertgeführter PV-Boost ohne Startreservierung, eine Wärmepumpen-Vorschau mit Pumpensignalen und Hover-Texte für alle Felder des Config-Editors. Das Update benötigt keine zwingende Konfigurationsänderung.

## Wallbox

- In `PV-Kurve ruhig` gilt unter dem Kurvenkorridor ein Wolken-Kontingent. Am Kontingentende folgt im selben Zyklus einmalig Weiterladen aus PV, ein getragener 1p-Abstieg oder Stop; der Speicher stützt bis zur Bestätigung höchstens 30 s.
- Nach einem Kaskadenabstieg von 3p auf 1p startet die openWB Pro mit dem konfigurierten Mindeststrom statt fest mit 6 A.
- Stammt der externe Hausverbrauchsanteil aus einem Altwert, startet keine externe Wallbox und erhöht weder Strom noch Phasenzahl; Absenkungen und Schutzabschaltungen wirken sofort.

## Speicher

- Batterie-Vitals nutzt bei fehlendem Modul-SoH den BMS-Wert `BAT_ASOC` als gekennzeichnete Näherung je Schrank.
- Verbraucherleistungen zählen in Budget und Bilanz genau einmal, auch in gemischten Installationen mit E3/DC- und externen Wallboxen.
- Eine aktive Lastspitzenkappung hat Vorrang vor dem Tarif-Halt: Sie kappt Viertelstundenspitzen mit dem Akku auch während eines Boosts und sperrt den Tarifstart nicht.

## Wärmepumpe

- Tariffenster-Heizen (experimentell, Standard Aus) verschiebt bei Octopus Heat belegte Wärmebedarfe in günstige Zeitfenster; Schattenbetrieb ohne Steuerbefehl ist möglich.
- `heat_price_boost_scope` gilt für Tarif- und Negativpreis-Boost, `heat_grid_boost_max_outdoor_c` begrenzt Heizungs-Boosts nach Außentemperatur.
- Im Luxtronik-Messwertbetrieb reserviert der PV-Boost keine feste Leistung; ein gehaltener Boost darf bei Kurvenbedarf weiterlaufen, wenn die Restprognose Akkuziel und Wärme deckt.
- Die Warmwasser-Grundstellung folgt dem Timer auch während E3DC-Datenlücken; Ferien- oder Frostschutzmodus der Wärmepumpe sperrt neue Boosts.
- **Stiebel ISG mit SG-Ready-Schreiben:** Die Startfreigabe lief bisher ab, bevor geschrieben wurde, und blieb danach gesperrt. Jetzt gelten mindestens 150 s Startfenster, und nach einer abgelaufenen Freigabe wird wieder entsperrt.
- `grid_start_limit` unter 1500 W Betrag wird nie erreicht; die Konfigurationsprüfung warnt jetzt davor.

## Prognose und Statistik

- Die E3/DC-Historie lag bisher um eine bzw. zwei Stunden zu spät. Sie wird jetzt in lokaler Gerätezeit angefragt; ältere Tage liest die Prognosediagnose automatisch nach.
- Die Tagesbilanz für Haus, Netz und Akku umfasst jetzt den Kalendertag (bisher in der Sommerzeit 22 bis 22 Uhr). Tageswerte ab dem Update können deshalb von früheren abweichen.

## Webportal und Diagnose

- Wärmepumpen-Vorschau mit Pumpensignalen (HUP, BUP, BOSUP, ZIP, EVU) und Durchfluss; `wp_heating_circuit_pump` wählt das Heizkreispumpensignal.
- Hover-Texte für alle Felder im Config-Editor.
- `heat_tariff_diagnostics.php` liefert einen lesenden Tagesexport für das Tariffenster-Heizen.

## Updatehinweise

- **Bare Metal:** Das Update wie gewohnt über **System Update** in der Weboberfläche starten. Nach erfolgreichem Update die Seite neu laden.
- **Docker:** Mit eingerichtetem Watchtower über **System Update**, sonst auf dem Host im bestehenden Compose-Ordner mit `sudo docker compose pull` und `sudo docker compose up -d` aktualisieren. Einen festen Pin in `.env` bewusst auf `E3DC_IMAGE_TAG=v5.5.3` ändern. Einen laufenden Watchtower vor einem manuellen Helferaufruf pausieren (`sudo docker compose --profile auto-update stop watchtower`) und danach wieder starten.
- **Konfiguration:** Tariffenster-Heizen bleibt standardmäßig aus (`heat_tariff_shift_mode = off`). Neue Schlüssel werden mit sicheren Standards vorbelegt.
- **Auswertungen:** In `waermepumpe.json` und den Luxtronik-Archivzeilen enthalten `ZUP` und `Ventil.-BOSUP` ab dieser Version den Ein/Aus-Wert; der Prozentwert steht unter `ZUP %` bzw. `Ventil.-BOSUP %`.
- **Von 5.4.x:** Zusätzlich gelten die Hinweise von 5.5.0 und 5.5.2 weiter unten. Details stehen in [doc/Update.md](doc/Update.md).

## Rückfall

Die freigegebenen Rückfallziele bleiben unverändert. Für Docker gilt der in der Update-Policy ausgewiesene Docker-Rollback-Root; auf Bare Metal bleibt ein verifiziertes Datei-Backup der sichere Rückweg. Vor einem Rückfall die [Rückfall-Dokumentation](doc/Rollback.md) lesen.

---

# E3DC-Control v5.5.2

5.5.2 beruhigt die Wallbox-Regelung, führt die Fahrzeug-SoC-Hochrechnung innerhalb derselben Stecksession fort und ergänzt den monatlichen Batterie-Vitalverlauf. Wärmepumpenaufträge überbrücken kurze Datenlücken; PV-Sollwerte und Warmwasser-Timer werden zuverlässiger gehalten. Die neue Wärmepumpenansicht und der Stiebel-ISG-SG-Ready-Ausgang sind experimentell und standardmäßig aus. Das Update benötigt keine Konfigurationsänderung.

## Wallbox

- Ein gemeinsamer Strombeschluss je Regelzyklus und Ladepunkt gilt jetzt auch für schnelle Netzbegrenzung, Keepalive und Startwiederholung. Ein fälliger Grid-Wächter reserviert seine Obergrenze vor der normalen Regelung; er senkt ausschließlich gemessen ladende Wallboxen mit einem Angebot über 6 A auf 6 A, erhöht keinen Strom und startet keine Ladung.
- openWB Pro: Wiederanlauf nach einem Phasenwechsel und Start-Weckimpuls greifen wieder über den gemeinsamen Befehlsweg. Der neue Schlüssel `wb<n>_openwb_pro_unplug_offer` bestimmt je Ladepunkt das einmalige Angebot nach erkanntem Abstecken: `safe` (Standard, auch bei ungültigem Wert) setzt 0 A; `fast_start` setzt 6 A für einen schnelleren Start beim nächsten Anstecken. Danach übernimmt die normale Regelung; Nutzer-`Aus` bleibt maßgeblich.
- Die Defizitkaskade führt einen erforderlichen Phasenabstieg auch im Startfenster vollständig aus. Ob der Speicher das Startfenster stützen kann, ergibt sich aus seiner wirksamen Entladereichweite, Hausverbrauch und PV-Leistung; unbekannte Speicherdaten geben keine Stützung frei.
- `wb_grid_import_settle_s` (Standard 10 s, 0 = aus, höchstens 30 s) gibt dem Speicher nach neuem Netzbezug und einer Stromanhebung Zeit zum Ausgleichen. Das gilt nur innerhalb seiner belegten Reichweite; Hausanschlussgrenzen, Nutzer-`Aus`, Notstromreserve und fehlende Stützungsfreigabe wirken sofort.
- Bei einzelnen ungültigen Liveproben bleibt der zuletzt ausgeführte Strom höchstens 10 s nach der letzten gültigen Probe stehen, je Ladepunkt insgesamt höchstens 10 s in 60 s und nur mit geeignetem vorherigem Messbeleg. Es gibt dabei keine Anhebung; harte Schutzgrenzen wirken sofort.
- In `PV + Akku bis Untergrenze` und `Sofort bis Preislimit` ohne Preis-/Netzfenster verwendet die Haltezone an `wbminsoc` den veröffentlichten PV-Rahmen des Speicherreglers. Unter der Untergrenze entsteht daraus keine neue Akkustützung. Die Abrundung auf zulässige Stromstufen berücksichtigt kleine numerische Abweichungen.
- Die Steckeranzeige leitet eine Verriegelung nur noch aus einem tatsächlich gemeldeten Verriegelungsbit ab; ein gestecktes Kabel allein zeigt kein geschlossenes Schloss.

## Fahrzeug-SoC

- Die Hochrechnung läuft innerhalb derselben bestätigten Stecksession auch mit älterem Cloudanker weiter. Bis 20 Prozentpunkte über dem bestätigten Fahrzeugwert darf sie regeln; darüber bleibt sie als „geschätzt, unbestätigt“ reine Anzeige. Eine fehlende Bestätigung durch den Wallbox-Dienst nach fünf Minuten sperrt die Regelwirkung ebenfalls.
- Neuere Cloudwerte werden am Messzeitpunkt verankert und gegen die seitdem geladene Energie geprüft. Widersprüchliche Korrekturen sperren die Schätzung der Stecksession. Eine nicht ausreichend eingrenzbare Lücke im Zählerverlauf bleibt reine Anzeige (`soc_anchor_history_incomplete`), statt ein Ladeziel freizugeben.

## Speicher

- Die Stützungsfreigabe der Wallbox (`battery_support_authorized`, `battery_support_reason`) steht bei einer ausdrücklichen Entscheidung im selben Zyklus im veröffentlichten Wallbox-Rahmen. Haus- und Wallboxverbrauch werden anhand zeitlich zusammengehöriger Messungen abgeglichen, damit einzelne verzögerte Messwerte kein doppeltes Budget erzeugen.
- Die PV-only-Entladebegrenzung erhält eine Zustandsverriegelung mit 500-W-Einschaltgrenze und einem bestätigten Leerlauf aller gesteckten Ladepunkte unter 300 W über 45 s zum Lösen. Ein Totband von 200 W beruhigt die Nachführung; harte Begrenzungen wirken sofort, ungültige Messwerte belegen keinen Leerlauf.
- Eine Wärmepumpen-PV-Freigabe lässt den bestehenden Speicherausgang unverändert. Sie erzeugt keinen eigenen `IDLE`-/`DISCH`-Befehl und keine zusätzliche Entladegrenze.
- Die Vitals-Seite zeigt einen monatlichen Batterie-Vitalverlauf mit SoH, Zyklen, Kapazität, Temperaturen und Zellspannungsspreizung. Bei vollständiger Tageshistorie kommt die Lade-/Entladeenergie des Vormonats hinzu. Die Aufzeichnung unter `data/battery_vitals_history.json` ist rein diagnostisch, auf 240 Monatsstände begrenzt und hat keine Regelwirkung; fehlende Werte bleiben `null` mit Qualitätsgrund.

## Wärmepumpe

- Im Messwertbetrieb beendet ein Verdichter, der wegen der Anlagenhysterese noch nicht startet, den PV-Boost nicht mehr über die allgemeine Nachfrageprüfung. Ein gesetzter Sollwert gilt weiterhin nicht als Nachweis eines Verdichterlaufs.
- Der Luxtronik-Warmwasser-Komforttimer stellt die Grundstellung ohne positive Boost-Haltezeit ein. Am SG-Ready-Ausgang läuft eine Timer-Nachfrage weiterhin über den zentralen Startweg mit Budget, Signalhalt und Wiedereinschaltsperre.
- Kurze E3DC-Datenlücken brechen laufende Aufträge nicht sofort ab: automatische PV-, Preis- und Pre-Dump-Aufträge werden bis zu 45 s, Nutzeraufträge und der Warmwasser-Timer bis zu 300 s überbrückt. Währenddessen startet kein neuer Auftrag und kein Sollwert steigt. Unbekannte Lückendauer und unabhängige Schutzgründe erlauben keine verlängerte Freigabe.
- Die Wiedereinschaltsperre `wp_restart_block_min` (Standard 20 min) zählt ab gemessenem Verdichterstillstand; die zusätzliche Sperre nach einer Rücknahme bleibt bestehen, maßgeblich ist das spätere Ende. Ohne messbaren Stillstand verwendet die Regelung die vorhandenen Rücknahme- und Stillstandsbelege konservativ.
- `manual_boost_min_soc` (Standard 25 %) ist die Startschwelle für den manuellen Boost. Ein späteres Unterschreiten beendet ihn regulär nach Mindestlaufzeit und Signalhalt, nicht sofort; die unabhängige Sicherheitsabschaltung unter `min_soc` minus 5 Prozentpunkten bleibt wirksam. iDM, Dimplex und SG Ready folgen denselben Regeln für Datenlücken und manuellen Boost.
- Stiebel ISG kann mit `stiebel_isg_sg_ready_write = 1` experimentell SG-Ready-Eingang 1 schreiben (Standard `0`, aus). Der Ausgang folgt der zentralen Entscheidung, schreibt bei Zustandswechseln und liest zurück. Ein konfigurierter Shelly-SG-/EVU-Kontakt hat Vorrang. Voraussetzungen, Grenzen bei Verbindungsverlust und Testanleitung stehen in der [Stiebel-Dokumentation](doc/Stiebel_Eltron_ISG.md).
- Shelly-SG-Relais an Stiebel werden anhand ihres tatsächlichen Relaiszustands bewertet; eigener Heizbetrieb der Wärmepumpe gilt nicht als fremder SG-Befehl. Wiederholtes fremdes Einschalten wird diagnostiziert und nicht fortlaufend überschrieben.
- „Warmwasser sofort“ benötigt an SG-Kontakten und iDM keinen PV-Überschuss und keine positive Budgethöhe, aber weiterhin ein frisches Speicher-Budget und die bestehenden Start- und Schutzfreigaben. Nutzer-`Aus` räumt zugehörige Anforderungsmerker auf; unterbrochene Befehle zeigen ihren Haltegrund.
- Warmwasser-Befehle außerhalb von „Warmwasser sofort“ brechen bei Nicht-Luxtronik-Wärmepumpen (iDM, Dimplex, Shelly-SG) den Regelzyklus nicht mehr wegen einer unbelegten internen Variable ab.

## Webportal

- Neue experimentelle Wärmepumpenansicht mit Anlagenbild aus Messwerten, Mobilansicht und gemerkter Ansicht. `wp_page_preview_enable = 1` schaltet sie frei (Standard `0`, aus); die Bedienaktionen bleiben dieselben. `wp_buffer_sensor` (Standard `none`) wählt optional einen Pufferfühler ausschließlich für die Anzeige. Die [Ansichten-Dokumentation](doc/Frontend_Ansichten.md) enthält eine kurze Testanleitung.
- Der Zwischenspeicher der Hausanzeige ist für die HA-Synchronisierung lesbar.

## Diagnose

- Der Live-Dienst schreibt den E3DC-Anmeldenamen nicht mehr ins Log; Host und Port bleiben sichtbar. Auch die Duplikatbereinigung und die Migration alter Luxtronik-Konfigurationen maskieren Anmeldenamen. Die RSCP-Diagnose blendet Authentifizierungsdaten und den Schlüssel aus.
- `luxtronik.json` enthält den Haltegrund je Wärmepumpenkanal, Haltegrund und Haltentscheidung des PV-Boosts (`heatpump_pv_hold_reason`, `heatpump_pv_hold_decision`) sowie die zuletzt angenommene Rücknahme des Boost-Signals (`heatpump_pv_last_signal_release`).
- Das Speicherprotokoll zählt jeden gesendeten `POWER_SETTINGS`-SET und fasst wiederholte Ausgaben zusammen. Eine Sammelzeile erscheint spätestens nach 60 s; Wechsel in oder aus „Entladen 0 W“ bleiben einzeln sichtbar, andere Grenzklassenwechsel erhalten höchstens alle 10 s eine eigene Zeile.

## Update

- Die Gesundheitsprüfung des Containers wertet einen normalen Zustandswechsel laufender Prozesse nicht mehr als Fehler. Auf langsamen Systemen brach das Update über den Host-Helfer dadurch bisher gelegentlich ab.
- Die Rechte-Reparatur überspringt zwischenzeitlich verschwundene oder ersetzte Laufzeitdateien, statt deswegen die gesamte Reparatur abzubrechen.
- Aufbewahrte Belege früherer abgebrochener Updates werden verständlich gemeldet.
- Installierte Dienste aus dem Dienstkatalog werden beim Update an das aktive venv gebunden. Ein Hinweis zum manuellen Löschen eines alten venv erscheint erst, wenn keine Dienstdefinition mehr darauf verweist.

## Updatehinweise

- **Bare Metal:** Das Update wie gewohnt über **System Update** in der Weboberfläche starten. Der Installer installiert den veröffentlichten Stable-Stand. Nach erfolgreichem Update die Seite neu laden.
- **Docker:** Mit eingerichtetem Watchtower über **System Update**, sonst auf dem Host im bestehenden Compose-Ordner mit `sudo docker compose pull` und `sudo docker compose up -d` aktualisieren. Einen festen Pin in `.env` bewusst auf `E3DC_IMAGE_TAG=v5.5.2` ändern. Bei älteren Installationen vorher den tatsächlich verwendeten Host-Updater nach der [Docker-Dokumentation](doc/Docker_Dokumentation.md) aktualisieren; das Containerimage ersetzt diese Hostdatei nicht. Container bei beendeter Fahrzeugladung und ohne laufenden Phasenwechsel neu erstellen.
- **Konfiguration:** Die neuen experimentellen Funktionen bleiben ohne Opt-in aus. Für die Wärmepumpenansicht und das Stiebel-Schreiben gelten die verlinkten Testanleitungen; die Betriebsdokumentation nennt Voraussetzungen und Rücknahme.
- **Von 5.4.x:** Zusätzlich gelten die Update- und Konfigurationshinweise sowie die bekannten Einschränkungen von 5.5.0 weiter unten. Details zum aktuellen Updateweg stehen in [doc/Update.md](doc/Update.md).

## Rückfall

Die freigegebenen Rückfallziele bleiben unverändert. Für Docker gilt der in der Update-Policy ausgewiesene Docker-Rollback-Root; auf Bare Metal bleibt ein verifiziertes Datei-Backup der sichere Rückweg. Vor einem Rückfall die [Rückfall-Dokumentation](doc/Rollback.md) lesen. Die Korrekturen und neuen Funktionen von 5.5.2 stehen auf älteren Programmständen nicht zur Verfügung.

---

# E3DC-Control v5.5.1

E3DC-Control 5.5.1 verbessert an der openWB Pro und am E3DC-Direktvertrag die Phasenreservierung und den bestätigten Wiederanlauf der Hochschaltung 1p→3p, ergänzt ein optionales 10-Minuten-Fenster für einen früheren 3p-Start, härtet den Wärmepumpen-Kanalbesitz (geordnete Rücknahme, alleiniger SHI-Schreiber) und ergänzt einen experimentellen Netzboost bei Negativpreisen. Das Diagnosepaket der Installationszentrale pseudonymisiert jetzt Geräte- und Netzwerkkennungen, und die API akzeptiert zusätzlich den Header `Authorization: Bearer`. Alle neuen Schalter sind standardmäßig ausgeschaltet; eine Konfigurationsänderung ist für das Update nicht nötig.

## Wallbox

- **Hochschaltung 1p→3p, Reservierung:** An der openWB Pro und am E3DC-Direktvertrag reserviert der Speicherregler vor jedem Geräteausgang jetzt die bisher laufende Leistung beziehungsweise den Wiederanlauf mit 6 A je Phase (rund 4,14 kW), nicht mehr den bisherigen einphasigen Strom auf drei Phasen umgerechnet (bisher bis zu rund 20 kW). Bei mehreren Wallboxen bleibt die Stromzuteilung zum Schutz des Hausanschlusses weiterhin dreiphasig; die dadurch mögliche Begrenzung auf etwa ein Drittel der Leistung bleibt eine bekannte Einschränkung.
- **Wartegrenze und Rückzug:** Ein Auftrag endet ohne Geräteausgang, wenn 90 s lang keine ausreichende Freigabe kommt, eine erteilte Freigabe 90 s ungenutzt bleibt oder die Regelung die Hochschaltung zwei Zyklen lang nicht mehr anfordert. Danach folgt der nächste Versuch frühestens nach `wb_phase_retry_block_s` (Standard 900 s); Vorlauf, Export-Wh-Konto und das 10-min-Fenster werden erst danach neu verdient.
- **Bestätigter Wiederanlauf:** Nach dem Phasenwechsel begrenzt die Regelung jeden Strom auf die freigegebene Reservierung, bis der Wiederanlauf unter Last bestätigt ist (mindestens drei Messungen über 500 W, stabil über 10 s). Bis zum Ende der Reservierung bleibt die Umrechnung dreiphasig; ein zwei- beziehungsweise einphasig ladendes Fahrzeug erreicht in dieser Zeit deshalb höchstens etwa zwei Drittel beziehungsweise ein Drittel des Budgets. Diese bekannte Grenze bleibt unverändert.
- **Experimentelles 10-min-Fenster 1p→3p** (`wb_phase_up_window_enable`, Standard aus, nur mit Energie-Phasenpolitik): Trägt der Überschuss im 10-Minuten-Mittel das 3p-Minimum plus 15 % ohne Einbruch in den letzten 2 Minuten, schaltet eine laufende einphasige Ladung ohne Vorlauf und Export-Wh-Konto auf drei Phasen; eine neue Ladung mit dreiphasigem Fahrzeugprofil startet direkt dreiphasig. Der bisherige Weg über Vorlauf oder Export-Wh-Konto bleibt daneben wirksam. Eine Testanleitung steht in der [Wallbox-Dokumentation](doc/Native_Wallbox.md), Abschnitt „Experimentelles 10-min-Fenster 1p→3p“.
- **`PV + Akku bis Untergrenze`, `Sofort bis Preislimit`, `Akku bis Abfahrt`:** Bei offenem wbminSoC-Tor stützt der Speicher die Wallbox jetzt unabhängig von der Kurvenlage (Grund `wbminsoc_floor_open`) und lädt dabei weiter nach der Ladekurve. Unterhalb von `wbminsoc` verhalten sich diese Modi wie `PV-Kurve ruhig`.
- **Echte Messwertvalidität:** Fehlende, ungültige oder veraltete Wallbox-Messwerte liefern jetzt `null` statt einer echten Nullmessung `0` (`wb`/`wb2`, `wb_observation`/`wb2_observation`). Dashboard und Wallbox-Seite zeigen dafür „–“ beziehungsweise „unbekannt“ statt 0 W und kennzeichnen eine nur von der Box gemeldete statt gemessene Phasenzahl als „(gemeldet)“.
- **Haltezeit vereinheitlicht:** Die Sperre nach einem Schützwechsel gilt jetzt für jeden Wallbox-Typ gleich, auch am E3DC-Direktvertrag; das experimentelle 10-min-Fenster beginnt in dieser Sperre ebenfalls neu.

## Speicher

- **Speicher halten sperrt nur die Entladung:** Hält die Börsenpreis-Optimierung vorhandene Akkuenergie für spätere teure Stunden zurück, bleibt das Laden aus PV-Überschuss im Rahmen der Ladekurve jetzt frei. Bisher wurde während des Haltens auch das PV-Laden gesperrt und der Überschuss eingespeist.
- **Wärmepumpen-Brücke:** Führt ein Schutzwächter den Speicher gerade im Automatikbetrieb, überschreibt die Übersetzung einer Wärmepumpen-Freigabe diesen Zustand nicht mehr mit einer eigenen Speichervorgabe.
- **Eine Stellgröße je Regelkreis:** Der Laderahmen (`iFc`) ist die obere Stellgröße der Speicherladung, die Entladegrenze legt nur fest, wen der Speicher versorgen darf, und Verbraucher regeln ausschließlich am verbleibenden PV-Budget. In `PV + Akku bis Untergrenze` bleibt der Laderahmen dabei aktiv, statt den Speicher in den freien Automatikbetrieb zu schalten, damit auch ein großer Speicher nicht vorzeitig voll ist.
- **Flash-Schreibschutz erweitert:** Der AUTO-Laderahmen (`EMS_REQ_SET_POWER_SETTINGS`) wird jetzt wie ein möglicherweise dauerhaft gespeichertes Register behandelt: nur bei echter Änderung geschrieben, nicht schützende Änderungen höchstens alle 30 s, jeweils mit Rücklese-Bestätigung.
- **Backup-Prüfung gehärtet:** Die Manifest-Liste der Backup-Kodierungen muss jetzt genau zu den tatsächlich verwendeten Kodierungen der Dateieinträge passen; ein Backup mit widersprüchlicher Kodierungsliste gilt als ungültig.

## Wärmepumpe

- **Ausschalten und Grundzustand:** „Automatik darf steuern“ aus nimmt einen laufenden PV-Boost jetzt geordnet zurück – Heizung über SHI-Modus 0, Warmwasser über einen Reset auf die konfigurierte Untergrenze `ww_eco`. Weicht die Rückmeldung ab, wird die Rücknahme bis zu dreimal im 60-Sekunden-Abstand gesendet; bleibt der Grundzustand unbestätigt, erscheint auf der Wärmepumpenseite ein Alarm, der zusätzlich per Push gemeldet wird.
- **Wiedereinschaltsperre erweitert:** Sie gilt jetzt auch nach der schutzbedingten Rücknahme eines bereits ausgespielten PV-Auftrags, dann mindestens 10 Minuten, auch bei `wp_restart_block_min = 0`. Nach zwei Entzügen aus demselben Geräteschutzgrund bleibt der PV-Boost bis zum nächsten Tag gesperrt.
- **Robusterer Kanalbesitz nach Neustart:** Ein bei Storage bereits bestätigter Abschluss desselben Auftrags wird nach einem Neustart auch dann übernommen, wenn der SHI-Kanal inzwischen einer anderen Steuerung gehört oder die gesicherte Rückmeldung den Entzug nicht mehr enthält. Ein einmal bestätigter Entzug bleibt für denselben Auftrag bestätigt, auch wenn danach eine andere Steuerung (Preis-Boost, Pre-Dump, Preis-Pause) den Kanal wieder auf den externen Sollwert setzt.
- **SHI-Status lesen:** Der Status wird nur noch aus den belegten Registerbereichen gelesen; das bisherige Blocklesen, das manche Regler mit einem Modbus-Fehler (Exception 2) beantworten, entfällt.
- **Alleiniger Schreiber:** Nur der Energy Manager schreibt die SHI-Register, über genau einen Treiber und eine serialisierte Sitzung; andere Dienste und die Weboberfläche übergeben eigene Befehle als Auftrag an den Energy Manager.
- **Experimenteller Netzboost** (`price_boost_enable`, „Experimentellen Netzboost aktivieren“, Standard aus): Schaltet den vorhandenen Negativpreis-Boost für die Luxtronik frei. Bisher blieb diese Auswahl wirkungslos (reine Diagnose); jetzt kann die Wärmepumpe bei einem echten Negativpreisfenster, Wärmebedarf und einer aktuellen Zusage der Speicherregelung tatsächlich boosten. Günstige, aber positive Preisfenster lösen weiterhin keinen Boost aus. Eine Testanleitung steht in der [Luxtronik-Dokumentation](doc/Luxtronik.md), Abschnitt „Netzboost (experimentell)“.

## Webportal

- Die Kennzeichnung prognosebasierter Abschnitte der Ladekurve verwendet einen sauber definierten lokalen Tagesbeginn; die bisherige PHP-Warnung beim Laden der Live-Daten entfällt.

## Diagnose und Datenschutz

- **Pseudonymisiertes Diagnosepaket:** Das Diagnosepaket der Installationszentrale pseudonymisiert jetzt IP-Adressen, Hostname, MQTT-Topics, Fahrzeugnamen sowie Seriennummern von Speicher, Wallbox und Zähler, MAC-Adressen, RFID-Tags, Fahrzeug-IDs und FIN, Klimageräte-IDs und Anlagen-IDs von Tarif- und Prognosediensten; Kontonamen von Geräte- und Dienstkonten sowie Fahrzeugpositionen werden maskiert. Gleiche Kennungen erhalten in allen Paketen einer Anlage dasselbe Pseudonym, damit sich Pakete vergleichen lassen; der dafür nötige Schlüssel liegt ausschließlich lokal auf der Anlage und wird nie mitgeliefert. Modellbezeichnungen, Firmwarestände, Dienstnamen und Messwerte bleiben wie bisher enthalten, weil sie für die Fehlersuche wichtig sind; das Paket sollte vor dem Versenden trotzdem kurz geöffnet und geprüft werden.
- **Konfigurations-Download:** „Einstellungen ohne Zugangsdaten“ maskiert jetzt zusätzlich Standort- und RSCP-Zugangsdaten; er enthält weiterhin IP-Adressen und Gerätekennungen und ist deshalb nicht zum Teilen gedacht.

## High Availability

- Die Dokumentation stellt jetzt klar, dass Rolle und Partner eines HA-Knotens ausschließlich über den Installer eingerichtet werden (`e3dc-setup`, Hauptmenü „7) Expertenmenü“ → „49) High Availability (Cluster)“); das Rollenfeld im Config-Editor genügt dafür nicht. Import und Rollback behalten Rolle, Partner und Gerätenamen eines Knotens bei. Eine neue Tabelle in der [HA-Dokumentation](doc/High_Availability_Dokumentation.md) beschreibt den sicheren Weg für Ersatzhardware, einen ausgefallenen Partner sowie einen fehlenden oder beschädigten Rollenanker.

## API

- Der API-Schlüssel kann jetzt gleichwertig per `X-API-PIN` oder `Authorization: Bearer` gesendet werden; bisher wurde `Authorization: Bearer` mit HTTP `403` abgewiesen. Beide Wege teilen sich Prüfung und Sperre nach Fehlversuchen. Sendet ein Client beide Header, gilt der Wert aus `Authorization: Bearer`. **Achtung:** Ein vorgeschalteter Proxy oder Anmeldedienst, der einen eigenen `Authorization: Bearer`-Header durchreicht, löst dadurch bei jeder Anfrage einen Fehlversuch aus und kann die eigene Adresse sperren; dieser Header sollte deshalb nicht an E3DC-Control weitergereicht werden.

## Update

- **Platzprüfung in `/run`:** Vor dem Vollbackup und erneut unmittelbar vor dem Dienststopp prüft der Updater den freien Platz in `/run`. Sind dort weniger als 16 MB frei, bricht das Update mit `E3DC-UPD-RUN-SPACE-001` ab, bevor Dienste gestoppt oder Dateien ersetzt werden; knapp über der Reserve erscheint nur eine Warnung. Abhilfe bis zum nächsten Neustart: `sudo mount -o remount,size=384M /run`.
- npm-Abhängigkeiten optionaler Module wie der Matter-Bridge werden jetzt auf der Festplatte vorbereitet, nicht im heruntergeladenen Release unter `/run`; ein fehlgeschlagenes `systemctl daemon-reload` wird einmal automatisch wiederholt.
- Ein vor dem Update aktiver `piguard`-Watchdog, der noch ein anderes venv nutzt, wird nach dem bestätigten Start des Updates automatisch an das neue aktive venv gebunden; scheitert die Neubindung, rollt der Installer auf den vorherigen Watchdog-Zustand zurück und meldet eine Warnung, ohne die neue Version zurückzunehmen.

## Updatehinweise

- **Bare Metal:** Das Update wie gewohnt über **System Update** in der Weboberfläche starten. Eine Konfigurationsänderung ist nicht nötig, da alle neuen Schalter standardmäßig aus sind.
- **Docker:** Wie gewohnt über den Knopf **System Update** (mit eingerichtetem Watchtower) oder auf dem Host mit `sudo docker compose pull` und `sudo docker compose up -d`. Ein fester Pin in `.env` wird bewusst auf `v5.5.1` geändert.
- **Von 5.4.x:** Das Update führt direkt auf 5.5.1. Dafür gelten zusätzlich die Updatehinweise, die Konfigurationshinweise und die bekannten Einschränkungen von 5.5.0 weiter unten.

## Rückfall

Ein Rückfall auf 5.5.0a oder 5.5.0 hebt die in 5.5.1 geänderten Wallbox-, Wärmepumpen- und Diagnosekorrekturen wieder auf. Neu eingetragene experimentelle Schalter (`wb_phase_up_window_enable`, `price_boost_enable`) wirken nach dem Rückfall nicht mehr; ihre Werte bleiben in der Konfiguration erhalten und wirken nach einer erneuten Aktualisierung auf 5.5.1 wieder.

---

# E3DC-Control v5.5.0a

E3DC-Control 5.5.0a ist ein Sicherheitsupdate für die Web-PIN. Wallbox-,
Speicher-, Wärmepumpen- und Hardwarelogik entsprechen unverändert 5.5.0.

## Sicherheitskorrektur

- **Sperre auch für den API-Zugriff:** Die Sperre nach Fehlversuchen (5 falsche
  Versuche, danach 10 Minuten Sperre) gilt jetzt auch für den API-Zugriff per
  Header (`X-API-PIN`). Bisher ließ sich die PIN über diesen Header ohne Sperre
  durchprobieren. Während einer Sperre wird auch
  die richtige PIN abgewiesen.
- **Sperre je Client-Adresse:** Der Sperrschlüssel hängt nur noch von der
  Client-Adresse ab (IPv4-Adresse bzw. IPv6-/64-Netz). Ein wechselnder
  User-Agent umgeht die Sperre nicht mehr.
- **Client-Adresse hinter einem lokalen Tunnel:** `CF-Connecting-IP` wird nur
  bei Verbindungen von `127.0.0.1` bzw. `::1` berücksichtigt, also bei einem
  lokal angebundenen Tunnel; aus dem Netz kommende Anfragen können sich damit
  keine fremde Adresse geben.
- **Vollständige Zählung:** Parallele Anfragen können die Sperre nicht mehr
  überholen; Fehlversuche werden auch unter Last vollständig gezählt.

## Empfehlungen

- **Web-PIN:** Eine PIN mit mindestens 6 Zeichen verwenden; Buchstaben und
  Ziffern sind erlaubt. Eine kurze, rein numerische PIN ist deutlich leichter
  zu erraten.
- **Widgets und Skripte:** Die Sperre gilt je Client-Adresse. Ein Widget mit
  veralteter PIN sperrt deshalb auch die Browser-Anmeldung vom selben Gerät
  bzw. Netz. Nach einer PIN-Änderung zuerst alle Widgets und Skripte anpassen.
- **Weiterer lokaler Reverse-Proxy:** Wer auf demselben System zusätzlich einen
  anderen Reverse-Proxy betreibt, beachtet den Hinweis in der
  [API-Dokumentation](doc/API_Documentation.md), Abschnitt „Sperre nach
  Fehlversuchen“.

## Updatehinweise

- **Bare Metal:** Das Update wie gewohnt über **System Update** in der
  Weboberfläche starten. Eine Konfigurationsänderung ist nicht nötig.
- **Docker:** Von 5.5.0 aus wie gewohnt über den Knopf **System Update** (mit
  eingerichtetem Watchtower) oder auf dem Host mit `sudo docker compose pull`
  und `sudo docker compose up -d`. Ein fester Pin in `.env` wird bewusst auf
  `v5.5.0a` geändert.
- **Von 5.4.x:** Das Update führt direkt auf 5.5.0a. Dafür gelten die
  Updatehinweise, die Konfigurationshinweise und die bekannten Einschränkungen
  von 5.5.0 weiter unten.
- **Rückfall:** Ein Rückfall auf 5.5.0 oder älter hebt diese Korrektur wieder
  auf.

---

# E3DC-Control v5.5.0

Dieses Release bringt an der openWB Pro ein Startfenster und einen messbasierten einphasigen Stromdeckel, eine ruhigere und robustere Phasenumschaltung, ein Akku-Wh-Konto für die Wallbox-Stützung in `PV-Kurve ruhig`, zeitgerichtetes Netzladen des Speichers mit Ladeprofil, einen saisonalen Luxtronik-PV-Boost, die Bluelink-Anmeldung mit Benutzer und Passwort, einen optionalen, rein lesenden Zusatzwechselrichter und unter Docker den Update-Knopf über Watchtower. Bitte vor dem Update die Abschnitte „Updatehinweise“, „Konfiguration prüfen“ und „Bekannte Einschränkungen“ lesen.

## Wallbox

- **Startfenster openWB Pro:** Nach dem Anstecken bietet der Manager 6 A an und hält das Angebot mindestens `openwb_pro_start_hold_s` (Standard 180 s) ohne 0 A, auch wenn das Budget kurz einbricht. Nimmt das Fahrzeug nicht an, folgt je Wiederholzyklus (`openwb_pro_start_retry_cycle_s`, Standard 300 s) höchstens ein Weckimpuls; nach drei Zyklen bleibt das Angebot mit der Meldung „Fahrzeug lädt trotz Freigabe nicht“ stehen. Harte Gründe wie Nutzer-`Aus`, Hausanschlussgrenze oder das Ende eines geplanten Ladefensters beenden das Angebot weiterhin sofort. Nach einem Phasenwechsel ohne laufende Ladung läuft die Box wieder an, auch nach Force-Start, Aus/Ein, Ablauf der Wartefrist und Neustart des Wallbox-Managers; zur Ausnahme bei einem schon vor dem Wechsel angebotenen Startstrom siehe „Bekannte Einschränkungen“.
- **Einphasiger Stromdeckel openWB Pro:** Einphasiges Laden über 20 A ist möglich, wenn eine einphasige Obergrenze über 20 A eingetragen ist (`wb<n>_openwb_pro_1p_max_amp`, leer = 20 A), die Hausabsicherung ausdrücklich eingetragen ist (`grid_max_amps`), die Netzphase des Ladepunkts gesetzt ist (`wb<n>_grid_phase`) und die Zuordnung beim Laden automatisch nachgewiesen wurde. Der Deckel folgt dem Bezug je Netzphase aus dem E3DC-Wurzelzähler und einem Schieflast-Wächter (`grid_pcc_imbalance_max_a`, Standard 20 A). Fehlt eine Voraussetzung oder ist eine Messung älter als 10 s, gilt weiterhin 20 A.
- **Einphasige Fahrzeuge an der openWB Pro:** Ist ein Fahrzeug mit einer Phase hinterlegt, schaltet der Manager die Phasen der Box nicht mehr um (bisher vor dem Start auf eine Phase); die Box behält ihre Einstellung. Mindestleistung und Budget richten sich nach dem Fahrzeug (1,38 kW), der einphasige Stromdeckel nach den gemessenen Phasen. Im Fahrzeugprofil deshalb die tatsächliche Phasenzahl eintragen: Ein dreiphasiges Fahrzeug mit einphasigem Profil lädt an einer Box im 3p-Modus dreiphasig und stoppt ohne ausreichendes Budget nach kurzer Zeit.
- **Phasenwechsel:** An der openWB Pro schaltet der Abstieg von drei auf eine Phase erst nach 480 s dauerhaft zu wenig Budget (bisher 60 s); Schutzgründe verkürzen auf 60 s. An der openWB Pro und bei eingeschalteter E3DC-Direktphasensteuerung bewertet die Hochschaltung den Überschuss gegen einen Referenzstrom aus dreiphasigem Mindeststrom plus Puffer (an der openWB Pro etwa 20 A), wartet also nicht auf den höheren einphasigen Deckel, und schaltet nach einem Vorlauf von 60 s oder mit vollem Export-Wh-Konto (120 Wh). Kurze Wolkenlücken setzen den Vorlauf dort nicht mehr zurück, und das Startfenster hält eine fällige Hochschaltung nicht mehr auf.
- **Akku-Wh-Konto in `PV-Kurve ruhig`:** Der Hausspeicher stützt eine laufende Ladung nur mit 0,5 % der Speicherkapazität (mindestens 50 Wh, `wb_curve_floor_support_wh`). Danach endet die Stützung über Absenken, Phasenwechsel und Stopp; ein neuer Start in derselben Stecksession folgt erst, wenn PV-Überschuss die Mindestleistung 120 s lang deckt. In `PV + Akku bis Untergrenze` gilt dieses Kontingent nicht; dort stützt der Speicher wie bisher bis zur Untergrenze.
- **Hausakku-Untergrenze in `PV + Akku bis Untergrenze`:** Erreicht der Hausspeicher `wbminsoc` und trägt das PV-Budget der Wallbox die Mindestleistung der aktuellen Phasenzahl nicht, geht eine laufende Ladung direkt auf den Mindeststrom – ohne PV sofort, bei noch anliegender PV nach 20 s Bestätigung. Das Wh-Konto läuft danach genau einmal bis zu seiner Schwelle weiter (`wb_min_current_import_stop_wh`, Standard 40 Wh), dann stoppt die Ladung; eine dreiphasig ladende openWB Pro wechselt stattdessen auf eine Phase, wenn das PV-Budget das einphasige Minimum trägt. Bisher wurde an der Untergrenze schrittweise abgesenkt, und das Konto begann bei jeder Stufe neu. Die Akkustützung bleibt unterhalb der Untergrenze geschlossen und öffnet erst oberhalb der Untergrenze plus Hysterese; ein neuer Start in derselben Stecksession folgt wie nach jedem Kaskaden-Stopp erst, wenn PV-Überschuss die Mindestleistung 120 s lang deckt. Dasselbe gilt in `Sofort bis Preislimit` außerhalb eines Preis- oder Netzfensters.
- **Mehrere Ladepunkte:** Zuteilung und Phasenwahl richten sich nach dem Anteil des einzelnen Ladepunkts; eine reine PV-Ladung unterhalb ihrer Zuteilung läuft mit Mindeststrom weiter, statt zwischen Stopp und Start zu pendeln.
- **Bedienung:** „Netz erlaubt + Fertig bis“ ist mit bestätigtem Fahrzeug-SoC an den Ladeplan gebunden. Manuelle Ladepläne mit fester Startuhrzeit können täglich wiederholt werden. Die Phasenzahl eines Fahrzeugprofils ist auf der Wallbox-Seite änderbar; ein fehlgeschlagenes Speichern nennt den Grund.

## Speicher und Netzladen

- **Zeitgerichtetes Netzladen:** Der Netzladebedarf reicht bis zum nächsten nutzbaren günstigen Preisfenster. Erwartete PV wird zuerst angerechnet, aus dem Netz wird nur der Rest am Ende des Fensters geladen, und ein erreichtes Ziel löst im selben Fenster keinen zweiten Netzladestart aus. Der Speicher wird damit nur so weit aus dem Netz geladen, wie es bis zum nächsten günstigen Fenster nötig ist.
- **Ladeprofil:** Wirtschaftlich, Ausgeglichen, Komfort oder Eigene Einstellungen (`market_charge_profile`). Komfort lädt im Preisfenster bis „Speicher max.“, solange der Preis unter dem Komfort-Preislimit liegt (`market_price_limit_ct`, leer = Mittelpreis des Tarifs). Beim Update wird das Profil ohne Verhaltensänderung aus den bisherigen Werten gesetzt.
- **Ruhiges Halten:** Der Netzladevertrag gilt bis zum Fensterende; das Halten des Speichers wird über Viertelstundengrenzen überbrückt, statt kurz in den Automatikbetrieb zu fallen. Unterhalb des Notstrom-Reservebodens bleibt das Ladeziel konstant.
- **Später Vollstand nur mit Grund:** In „Prognose auf 100%“ wird 100 % kurz vor dem PV-Ende nur noch bei bindendem Einspeiselimit, Abregeldruck, Direktvermarktung oder Pre-Dump geplant; sonst geht PV-Überschuss am Mittag zuverlässiger in den Speicher.
- **DC-first mit Zusatz-Wechselrichter:** Kurze Einbrüche des E3/DC-PV-Angebots überbrückt der Laderahmen bis zu 600 s. Liefert der Zusatz-Wechselrichter Leistung, bleibt es beim sofortigen Kappen. Ausnahme bei gefährdetem Ladeziel (Rückstand unter der Korridor-Untergrenze, Abendziel-Rückstand, spätester Ladebeginn erreicht oder harter Kurvenanker verfehlt): Dann bleiben Halten und Freigabe auch bei Leistung des Zusatz-Wechselrichters wirksam, und der Speicher kann aus Zusatz-AC-PV laden – aber nur, solange das E3/DC-PV-Angebot unter der Einschaltschwelle liegt. Liefert die E3/DC-PV mehr, begrenzt der Laderahmen die Ladung weiter auf die E3/DC-PV-Leistung.
- **Ruhigere Ladegrenze:** Im Kurvenladebetrieb schreibt der Manager nicht schützende Änderungen der AUTO-Ladegrenze höchstens alle 30 s. Schützende Absenkungen (etwa 0-W-Halt, Kurve oberhalb, Planwert 0) wirken sofort, ebenso die Freigabe bei Netzbezug und eine Öffnung nahe der Einspeisegrenze. Abregelung und Einspeisegrenze, Direktvermarktung, eine ladende Wallbox in `PV-Kurve ruhig` und die Zielkorridor-Schnellladung führen die Ladegrenze wie bisher in beide Richtungen ohne Wartezeit. Innerhalb von 4,5 s nach einem eigenen, per Rücklesen bestätigten Schreibvorgang löst ein älterer Live-Wert, der noch den vorherigen Wert zeigt, keinen zweiten Schreibvorgang mit demselben Wert aus.

## Wärmepumpe

- **Luxtronik-PV-Boost:** Saisonkanäle (Winter Heizung und Warmwasser, Sommer Warmwasser), einstellbare Anlagenhysterese (`wp_pv_hz_hysteresis_k`, `wp_pv_ww_hysteresis_k`) und Wolkenüberbrückung (`wp_pv_boost_release_s`, Standard 300 s). Ein Verdichterstopp beendet den Boost nicht mehr.
- **Warmwasser sofort:** Der Knopf ist ein Nutzerbefehl, unabhängig vom PV-Überschuss. Er endet regulär mit erreichter Zieltemperatur; eine Quell-Erholungspause unterbricht ihn nicht mehr.
- Die Wärmepumpen-Seite zeigt den Warmwasser-Verdichter mit Ist- und Sollfrequenz; nach einem Geräteupdate werden neue Messwertkennungen automatisch zugeordnet.

## Fahrzeuge und Zusatzwechselrichter

- **Bluelink:** Die Anmeldung erfolgt mit Benutzer (E-Mail), Passwort, optionaler PIN und Marke; der bisherige Token entfällt. Fehlerursachen werden benannt; ein laufender Dienst ohne Zugangsdaten wartet, statt neu zu starten. Wurde die Anbindung bisher nur mit Token betrieben, bleibt sie nach dem Update aus, bis Benutzer und Passwort eingetragen sind.
- **Zusatzwechselrichter:** Ein Sungrow-String-Wechselrichter kann rein lesend per Modbus TCP angezeigt werden. Die Regelung nutzt weiterhin den E3DC-Messwert. Einzelheiten: [Zusatzwechselrichter](doc/Zusatzwechselrichter.md).

## Docker und Wartung

- **Update-Knopf:** Der Knopf **System Update** in der Weboberfläche und das **Auto-Update** im Config-Editor geben Watchtower das Signal, das neue Image zu laden und den Container neu zu erstellen. Einmalige Freischaltung mit Token; ohne Watchtower zeigt der Knopf die Host-Befehle.
- **Rechte im Container:** Der Konfigurationseditor speichert im Container auch unter „Weitere Parameter“; die Rechteprüfung verwendet ein eigenes Container-Modell.
- **Wartung:** Backups werden je Datei komprimiert, die RAM-Disk wächst auf 64 MB, alte Release-Umgebungen werden bereinigt, die Klima-Historie wird nach 90 Tagen bereinigt.

## Behoben

- Die Update-Prüfung der Weboberfläche meldete immer, GitHub sei nicht erreichbar; der Hinweis auf eine neue Version erscheint wieder.
- Die globalen Fahrzeugwerte unter „Fahrzeug und Ladeziel“ bleiben erhalten; bisher löschte die Konfigurationsbereinigung sie beim Containerstart sowie bei Reparatur, Installation und Rückfall.
- Der automatische SoC-Abruf, der Docker-Start des Bluelink-Dienstes und der Knopf „Fahrzeug aufwecken“ setzen jetzt Benutzer und Passwort voraus – wie die Anmeldung selbst.
- „Sofort bis Preislimit“ lässt sich an E3DC-, openWB- und go-e-Wallboxen wieder speichern.

Die vollständige Liste steht im [Changelog](CHANGELOG.md).

## Updatehinweise

**Bare Metal:** Das Update wie gewohnt über **System Update** in der Weboberfläche oder auf der Konsole mit `sudo /usr/local/sbin/e3dc-web-update-launcher` starten. Der Updater legt vorab ein verifiziertes Vollbackup an, vergrößert die eigene RAM-Disk von 32 auf 64 MB und startet die Kerndienste sowie die installierten Zusatzdienste neu. Updates bei beendeter Fahrzeugladung und ohne laufenden Phasenwechsel durchführen; eine laufende openWB-Pro-Ladung wird nach dem Neustart übernommen.

**Hinweis auf 5.5.0 in älteren Installationen:** Bare-Metal-Installationen mit 5.4.4c bis 5.4.6d zeigen den Hinweis auf 5.5.0 wegen eines Fehlers ihrer Update-Prüfung nicht an. Das Update lässt sich trotzdem wie beschrieben starten. Eine Docker-Installation mit 5.4.x zeigt grundsätzlich keinen Versionshinweis; ihre Weboberfläche nennt höchstens die Host-Befehle, und der Wechsel auf 5.5.0 läuft über den Host-Weg (siehe „Docker“).

**Docker:** Der Knopf **System Update** in der Weboberfläche (und das **Auto-Update** im Config-Editor) aktualisiert unter Docker ab 5.5.0 über Watchtower. Das Update von 5.4.x auf 5.5.0 selbst läuft noch über den Host-Weg; ab dem nächsten Release genügt der Knopf. Dafür auf dem Docker-Host im Compose-Ordner zuerst die Compose-Datei auf den Stand dieses Release bringen: Die unveränderte mitgelieferte `docker-compose.yml` mit benannten Volumes wird über einen aktualisierten Checkout oder eine neu bezogene Datei ersetzt. Eine Compose-Datei aus der Docker-Einrichtung des Installers bindet `./data` und `./logs` als Ordner ein und darf nicht durch die Vorlage ersetzt werden, weil der Container sonst mit leeren Volumes ohne Konfiguration startet. Dort sowie bei OMV- oder eigenen Dateien werden die Änderungen einzeln übertragen: beim E3DC-Dienst `E3DC_WATCHTOWER_API_URL` und `E3DC_WATCHTOWER_API_TOKEN` sowie ein Watchtower-Label, das `true` ergibt (ältere Dateien tragen `${E3DC_WATCHTOWER_ENABLE:-false}`; dann die Zeile wie in der Vorlage auf `${E3DC_WATCHTOWER_ENABLE:-true}` ändern), beim Watchtower-Dienst das Image `ghcr.io/nicholas-fedor/watchtower:1`, im Hostnetz-Betrieb `network_mode: host` und die `WATCHTOWER_HTTP_API_*`-Werte der Vorlage, ohne `WATCHTOWER_POLL_INTERVAL` und `DOCKER_API_VERSION`. Die vollständige Liste steht in der [Docker-Dokumentation](doc/Docker_Dokumentation.md) unter „Watchtower einmalig freischalten“. Einen festen Pin in `.env` bewusst auf `v5.5.0` ändern. Danach `sudo docker compose pull` und `sudo docker compose up -d` ausführen. Wer den Host-Helfer verwendet, aktualisiert zuerst den Helfer aus diesem Release und stoppt einen laufenden Watchtower vorher. Anschließend Watchtower einmalig freischalten, wie in der [Docker-Dokumentation](doc/Docker_Dokumentation.md) unter „Updates: Weboberfläche, Host und Watchtower“ beschrieben. Die neue Vorlage mountet die RAM-Disk mit 64 MB.

**System Update** aktualisiert nur Container, deren Image aus der Registry (`ghcr.io/a9xxx/install-e3dc-control`) gezogen wurde. Ein selbst gebautes Image wird von Watchtower nicht geprüft; dann gelten die Host-Befehle `docker compose pull` / `up -d` bzw. der eigene Build. Watchtower entfernt nach erfolgreichem Wechsel das alte Image (`WATCHTOWER_CLEANUP=true`); ein Rückfall zieht die gewünschte Version per Tag (`E3DC_IMAGE_TAG`) neu. Nach einem Watchtower-Wechsel erstellt ein späteres `docker compose up -d` den Container einmal neu (gleiches Image, Daten bleiben).

Vor dem Update benötigte Volumes bei gestopptem Container mit erhaltenen numerischen Eigentümern und Rechten sichern. Container-Neuerstellungen bei beendeter Fahrzeugladung durchführen.

## Konfiguration prüfen

1. **Hausabsicherung:** Für einphasiges Laden über 20 A an der openWB Pro `grid_max_amps` (und bei Bedarf `grid_wallbox_reserve_amps`) ausdrücklich eintragen, `wb<n>_grid_phase` je openWB Pro setzen und die einphasige Obergrenze `wb<n>_openwb_pro_1p_max_amp` auf den gewünschten Wert über 20 A setzen (leer = 20 A; der eingestellte Maximalstrom `wb<n>_max_amp` begrenzt zusätzlich). Der Nachweis der Zuordnung entsteht bei der ersten einphasigen Ladung. Wer den messbasierten Deckel nicht möchte, setzt `wb_pcc_phase_basis` auf `off`.
2. **Bluelink:** Benutzer (E-Mail), Passwort, gegebenenfalls PIN und Marke im Konfigurationseditor unter „Fahrzeug Integration (Bluelink)“ oder auf der Wallbox-Seite eintragen. Ohne diese Angaben bleibt die Fahrzeuganbindung inaktiv. Eine Zwei-Faktor-Anmeldung des Kontos wird nicht unterstützt.
3. **Ladeprofil:** Das Profil wird beim Update aus den bisherigen Werten gesetzt. Wer Marge oder Sicherheitskorrektur angepasst hatte, erhält „Eigene Einstellungen“ mit unveränderten Werten und kann danach ein anderes Profil wählen.
4. **Wärmepumpe im Hauswert:** `storage_home_wp_split` steht standardmäßig auf `auto`. Wer seine Messanordnung kennt, wählt fest `include` (Wärmepumpe im E3DC-Hauswert enthalten) oder `separate`.
5. **Fahrzeugprofile:** Für einphasige Fahrzeuge im Fahrzeugprofil die Phasenzahl 1 eintragen. Umgekehrt stoppt ein dreiphasiges Fahrzeug mit versehentlich einphasigem Profil an einer dreiphasig eingestellten Box ohne passendes Budget und fällt in derselben Stecksession nicht auf eine Phase zurück. Das Profil muss deshalb zur tatsächlichen Phasenzahl des Fahrzeugs passen.
6. **Zusatzwechselrichter:** Optional; ohne Einrichtung ändert sich nichts.

**Globale Fahrzeugwerte:** Eine Wallbox ohne eigene Ziel-Einstellungen folgt den globalen Fahrzeugwerten im Config-Editor, die die einfache Ansicht von Wallbox 1 mitschreibt. Auch die Abstimmung von Wärmepumpe und Fahrzeugladung (PV-Pause/Boost, Meldung „Ladeziel erreicht“) nutzt ohne laufende Ladesitzung diesen globalen Ziel-Ladestand. Diese Werte bleiben jetzt auch in Docker über Neustarts erhalten. Für ein eigenes Ziel an Wallbox 2 deren einfache Ansicht speichern.

Neue Einstellungen gelten mit ihren Standardwerten; bisher nur intern gelesene Schlüssel bleiben beim Update erhalten.

## Rückfall

**Docker:** Ein Rückfall auf 5.4.6d erfolgt auf dem Host mit `E3DC_IMAGE_TAG=v5.4.6d` in `.env` und `sudo docker compose up -d`; ein Rückfall auf ein älteres Root-Image bleibt dem Host-Helfer vorbehalten. Beim Rückfall auf 5.4.x entfernt die Konfigurationsbereinigung der älteren Version die in 5.5.0 neuen Einstellungen (u. a. Bluelink-Zugang, Zusatzwechselrichter, Marktprofil, neue Wallbox-Phasenparameter). Die beim Start angelegte Sicherung unter `data/config_backups/` enthält sie weiterhin; nach einer erneuten Aktualisierung auf 5.5.0 die Werte dort nachsehen und neu eintragen. Die Bluelink-Anbindung von 5.4.x benötigt ihren bisherigen Refresh-Token, den 5.5.0 aus der Konfiguration entfernt hat; er steht in der vor dieser Bereinigung angelegten Sicherung unter `data/config_backups/` und muss nach dem Rückfall wieder eingetragen werden. Automatische Sicherungen in diesem Ordner werden auf die 20 neuesten gekürzt; neue entstehen bei jedem Containerstart und beim Speichern im Konfigurationseditor. Die benötigte Sicherung deshalb rechtzeitig an einen anderen Ort kopieren: die mit dem Refresh-Token direkt nach dem Update auf 5.5.0, die mit den neuen Werten direkt nach dem Rückfall.

**Bare Metal:** Der Rückweg ist das beim Update angelegte verifizierte Backup (Installer-Hauptmenü `6) Backup erstellen / verwalten`). Es stellt Programmstand und Konfiguration von vor dem Update wieder her; unter 5.5.0 neu eingetragene Werte wie Bluelink-Zugang oder Zusatzwechselrichter sind darin nicht enthalten. Sicherungspunkte werden ab 5.5.0 je Datei komprimiert, Update-Backups ebenso wie manuell angelegte, und ältere Versionen lesen dieses Format nicht. Nach dem Rückfall auf 5.4.x lassen sich die unter 5.5.0 angelegten Sicherungspunkte deshalb erst nach einer erneuten Aktualisierung auf 5.5.0 wiederherstellen. Einzelheiten: [Rollback](doc/Rollback.md).

## Bekannte Einschränkungen

- Das Netzladen nach Preisfenstern erkennt günstige Zeiten über das günstigste Viertel des Tages. Hat ein Tarif nur ein kurzes günstiges Fenster (weniger als etwa 5–6 Stunden pro Tag), wird es nicht als Ladefenster erkannt, und der Speicher wird dort nicht aus dem Netz geladen. Tarife mit längeren Niedrigpreiszeiten (zum Beispiel Wärmepumpentarife mit zwei Fenstern oder HT/NT-Tarife mit langer Nachtzeit) sind nicht betroffen. Eine tarifbewusste Erkennung ist für eine Folgeversion geplant.
- E3DC efy und Multi Connect II behalten standardmäßig ihre herstellereigene Phasenwahl. Eine direkte externe Phasensteuerung ist nur als experimenteller, standardmäßig ausgeschalteter Schalter enthalten (`wb_e3dc_direct_phase_control_enable`). Er schreibt Geräteeinstellungen der Wallbox und ist nicht für den Dauerbetrieb empfohlen; die Konfigurationsprüfung warnt, solange er eingeschaltet ist.
- Bare-Metal-Installationen mit 5.4.4c bis 5.4.6d zeigen den Hinweis auf 5.5.0 wegen des Fehlers ihrer Update-Prüfung nicht an; unter Docker zeigt 5.4.x grundsätzlich keinen Versionshinweis (siehe Updatehinweise).
- Den einphasigen Stromdeckel mit Schieflast-Wächter (`grid_pcc_imbalance_max_a`) gibt es nur an der openWB Pro. An allen anderen Ladepunkten (E3DC, openWB, go-e) begrenzen beim einphasigen Laden weiterhin nur der eingestellte Maximalstrom (`wb<n>_max_amp`) und die Hausabsicherung. Wer die Unsymmetriegrenze seines Netzbetreibers (üblich 4,6 kVA ≙ 20 A je Außenleiter) einhalten muss und an einem solchen Ladepunkt einphasig lädt, stellt dort 20 A ein. Das begrenzt an diesem Ladepunkt auch das dreiphasige Laden, bei einer 32-A-Wallbox von 22 kW auf 13,8 kW.
- Der Schieflast-Wächter der openWB Pro bewertet die Differenz der Bezugsströme am Netzpunkt (Einspeisung zählt als 0 A), nicht die vorzeichenbehaftete Differenz der Phasenleistungen. Entlädt oder speist der Hausspeicher gleichmäßig über alle drei Phasen, kann eine openWB Pro mit einer einphasigen Grenze über 20 A nachts bis etwa 28–29 A und tagsüber je nach Einspeisung bis zur eingestellten Grenze laden; ohne jeden Ausgleich (leerer Akku, keine PV) bleibt es bei 20 A. Wer einphasig strikt bei 20 A bleiben muss, lässt `wb<n>_openwb_pro_1p_max_amp` leer (Standard 20 A). Ein schon unter 5.4.x eingetragener Wert über 20 A wirkt nach dem Update, sobald `grid_max_amps` und `wb<n>_grid_phase` eingetragen sind und die Phasenzuordnung beim Laden nachgewiesen wurde. Rückweg: `wb_pcc_phase_basis` auf `off` setzen (fest 20 A).
- An der openWB Pro kann eine Stecksession nach einem Phasenwechsel ohne laufende Ladung stehen bleiben: Wurde in dieser Stecksession schon vor dem Wechsel ein Startstrom angeboten und nimmt das Fahrzeug den Wiederanlauf nach dem Wechsel nicht an, bietet der Manager nach seinem eigenen Stopp (0 A) nicht von selbst erneut Strom an. Force-Start, ein Wechsel auf `Aus` und zurück oder Umstecken lösen diesen Zustand. Eine Korrektur ist für eine Folgeversion geplant.
- An der Hausakku-Untergrenze in `PV + Akku bis Untergrenze` können Absenkung und Stopp wie bisher länger dauern, wenn gleichzeitig ein kleiner, anhaltender Netzbezug ansteht oder mehrere Wallboxen in unterschiedlichen Modi laden; der Hausspeicher entlädt dann weiter unter die Untergrenze. Eine Korrektur ist für eine Folgeversion geplant.
- Wird in der Fahrzeugauswahl einer Wallbox ein automatisch erkannter Eintrag statt des gespeicherten Fahrzeugprofils gewählt (beide können denselben Namen tragen), rechnet der Fahrzeug-SoC nicht weiter hoch und bleibt auf dem letzten Wert stehen. Abhilfe: das gespeicherte Profil wählen und den Start-SoC neu setzen. Eine Korrektur folgt.
- Im Kurvenladebetrieb wird die Ladegrenze des Speichers weiterhin aus Messwerten nachgeführt. Nicht schützende Änderungen gehen höchstens alle 30 s an den E3DC, schützende Absenkungen und die Freigabe bei Netzbezug sofort. Bei wechselnder Bewölkung oder Last folgt die Ladegrenze dadurch verzögert, und einzelne Abschnitte speisen mehr ein. Abregelung und Führung an einer Einspeisegrenze, Direktvermarktung, eine ladende Wallbox in `PV-Kurve ruhig` und die Zielkorridor-Schnellladung schreiben wie bisher ohne Bremse in beide Richtungen; dort kann die Ladegrenze öfter wechseln. Eine ruhigere Führung ist für eine Folgeversion geplant.
- Bei eingeschalteter E3/DC-PV-Ladebegrenzung (`storage_dc_first_charge_limit_enable`) kann Leistung des Zusatz-Wechselrichters trotz Abendziel-Rückstand ins Netz gehen, weil der Laderahmen der E3/DC-PV folgt, sobald diese über der Einschaltschwelle liefert. Eine Korrektur ist für eine Folgeversion geplant.
- Der Watchdog (`piguard`) prüft beim Start die Python-Umgebung, mit der er eingerichtet wurde. Das Update richtet ihn nicht auf die neue Python-Umgebung um. Eine Python-Umgebung, die der Watchdog noch nutzt, löscht das Update deshalb nicht und meldet sie auch nicht als entfernbar. Startet `piguard` nach einem Update nicht, weil diese Umgebung fehlt, richtet `e3dc-setup` → Menü 15 „Watchdog & Telegram konfigurieren“ → „Komplett neu installieren / reparieren“ ihn auf die aktive Umgebung um. Das automatische Umrichten ist für eine Folgeversion geplant.

Weitere Einzelheiten: [Wallbox](doc/Native_Wallbox.md), [Speicher](doc/Speicher_Ladesteuerung_Ablauf.md), [Börsenpreis-Optimierung](doc/Boersenpreis_Optimierung.md), [Konfiguration und Regelung](doc/V4_Konfiguration_und_Regelung.md), [Luxtronik](doc/Luxtronik.md), [Fahrzeug-Integration](doc/Fahrzeug_Integration.md), [Zusatzwechselrichter](doc/Zusatzwechselrichter.md), [Docker](doc/Docker_Dokumentation.md), [Update](doc/Update.md) und [Rollback](doc/Rollback.md).
