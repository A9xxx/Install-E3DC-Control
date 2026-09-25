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
