<?php
require_once 'helpers.php';
$paths = getInstallPaths();
?>
<!DOCTYPE html>
<html lang="de" data-bs-theme="dark">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hilfe & FAQ - E3DC-Control</title>
    <link href="assets/vendor/bootstrap/css/bootstrap.min.css" rel="stylesheet">
    <link rel="stylesheet" href="assets/vendor/fontawesome/css/all.min.css">
    <style>
        body {
            font-family: 'Segoe UI', Roboto, sans-serif;
            padding-bottom: 50px;
        }
        .help-header {
            background: linear-gradient(135deg, #0d6efd 0%, #0dcaf0 100%);
            padding: 60px 0;
            margin-bottom: 40px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }
        .search-container {
            max-width: 600px;
            margin: -30px auto 0;
            position: relative;
            z-index: 10;
        }
        .search-input {
            height: 60px;
            border-radius: 30px;
            border: none;
            box-shadow: 0 10px 25px rgba(0,0,0,0.3);
            padding-left: 25px;
            font-size: 1.1rem;
            background: var(--bs-body-bg);
            color: var(--bs-body-color);
        }
        .faq-card {
            background: var(--bs-secondary-bg);
            border: 1px solid var(--bs-border-color);
            border-radius: 12px;
            margin-bottom: 20px;
            transition: transform 0.2s, box-shadow 0.2s;
        }
        .faq-card:hover {
            transform: translateY(-3px);
            box-shadow: 0 5px 15px rgba(0,0,0,0.3);
        }
        .faq-question {
            padding: 20px;
            cursor: pointer;
            font-weight: 600;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .faq-answer {
            padding: 0 20px 20px;
            color: var(--bs-secondary-color);
            display: none;
            border-top: 1px solid var(--bs-border-color);
            padding-top: 15px;
        }
        .faq-answer code {
            background: var(--bs-tertiary-bg);
            color: #0dcaf0;
            padding: 2px 6px;
            border-radius: 4px;
        }
        .faq-answer pre {
            background: var(--bs-tertiary-bg);
            padding: 15px;
            border-radius: 8px;
            margin-top: 10px;
            color: #2ecc71;
            font-size: 0.9rem;
        }
        .tag {
            font-size: 0.75rem;
            padding: 3px 10px;
            border-radius: 15px;
            background: rgba(13, 202, 240, 0.15);
            color: #0dcaf0;
            margin-right: 10px;
        }
        mark {
            background: #ffc107;
            color: #000;
            padding: 0;
        }
        .nav-link-back {
            color: #fff;
            text-decoration: none;
            opacity: 0.8;
            transition: opacity 0.2s;
        }
        .nav-link-back:hover { opacity: 1; color: #fff; }
        .text-accent { color: #0dcaf0; }
    </style>
    <script>
        // Theme aus localStorage laden (gleicher Key wie das Haupt-Dashboard)
        (function() {
            try {
                var t = localStorage.getItem('theme') || 'dark';
                document.documentElement.setAttribute('data-bs-theme', t);
            } catch(e) {}
        })();
    </script>
</head>
<body>

<div class="help-header text-center">
    <div class="container">
        <div class="d-flex justify-content-between align-items-center mb-4">
            <a href="index.php" class="nav-link-back"><i class="fas fa-arrow-left me-2"></i>Dashboard</a>
            <span class="badge bg-success text-light">v5.4.3j Stable</span>
        </div>
        <h1 class="display-4 fw-bold">Hilfe & Support</h1>
        <p class="lead opacity-75">Häufige Fragen und Lösungen rund um E3DC-Control.</p>
    </div>
</div>

<div class="container">
    <div class="search-container mb-5">
        <div class="input-group">
            <span class="input-group-text bg-white border-0 ps-4"><i class="fas fa-search text-muted"></i></span>
            <input type="text" id="helpSearch" class="form-control search-input" placeholder="Suche nach evcc, tibber, backup, shadow, etc...">
        </div>
    </div>

    <div class="row" id="faqContainer">
        <div class="col-12 faq-item" data-tags="docker image stable rollback update">
            <div class="card bg-card border-0 shadow-sm"><div class="card-body">
                <h5 class="card-title"><span class="tag">Docker</span> Wie prüfe ich Image und Update?</h5>
                <p>Die mitgelieferte Compose-Datei verwendet standardmäßig <code>image: "ghcr.io/a9xxx/install-e3dc-control:${E3DC_IMAGE_TAG:-latest}"</code>. Ohne Pin folgt sie dem geprüften Stable-Tag <code>latest</code>. Ein fester Tag bleibt bei <code>pull</code> absichtlich fest; für einen bewussten Pin wird zum Beispiel <code>E3DC_IMAGE_TAG=v5.4.3j</code> in <code>.env</code> gesetzt.</p>
                <pre>cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
if [ -f ./docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./docker_compose_update.py
elif [ -f ./Installer/docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py
else
  echo "docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen." >&2
  exit 2
fi
sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo
sudo docker compose logs --tail=80 e3dc-control</pre>
                <p>Der Host-Helfer zieht das gewählte GHCR-Image ausdrücklich, bindet seine SHA-256-ID und OCI-Version vor dem Start, wartet auf den imagegebundenen Healthcheck und verlangt zwei identische Laufzeit-Snapshots. Scheitert Start, Warten, Snapshot oder Versionsabgleich, stoppt er den Kandidaten und bestätigt dessen Stillstand.</p>
                <p>Der nicht mehr gepflegte Watchtower ist wegen seines weitreichenden Docker-Socket-Zugriffs kein Standardstart. Für den bewussten Opt-in muss in <code>.env</code> zusätzlich <code>E3DC_WATCHTOWER_ENABLE=true</code> gesetzt und danach das Profil <code>auto-update</code> gestartet werden. Ohne das Label-Opt-in bleibt auch ein versehentlich gestarteter Watchtower wirkungslos.</p>
                <p><strong>HA-Abgrenzung:</strong> Docker ist nur mit exakt <code>ha_mode=off</code> freigegeben. HA-Master/-Slave und Shadow bleiben Bare-Metal-Betriebsarten. Der Container projiziert den persistenten Instanzrollenanker create-once auf <code>off</code> und stoppt vor jedem Hardware-Writer, wenn Konfiguration und Anker nicht exakt passen. Beim ersten Wechsel einer nativen Installation verhindert ausschließlich der Installer-Menüpunkt <strong>31</strong> die Doppelsteuerung durch kontrolliertes Stoppen und Deaktivieren aller Host-Dienste. Zusätzlich werden manuelle Hardware-Writer und Legacy-Screens über zwei stabile <code>/proc</code>-Snapshots erkannt und blockieren die Migration; der Installer beendet sie nicht. Ein vorhandener E3DC-Container, eine vorhandene Compose-Datei oder bereits verwaltete E3DC-Docker-Daten stoppen diesen Migrationsweg vor der ersten Änderung; bestehende Docker-Installationen nutzen den Compose-Updateweg.</p>
                <p>Der Docker-Rückfall erfolgt ausschließlich auf ein in der Update-Policy mit <code>docker_supported</code> freigegebenes Image. <code>v5.3.2b</code> ist nicht als Bare-Metal-Programm-Rückfall freigegeben.</p>
            </div></div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-layer-group me-2"></i>Bedienansichten: einfache und erweiterte Ansicht</h4>
        <div class="col-12 faq-item" data-tags="einfache ansicht erweiterte ansicht config wallbox waermepumpe bwwp heizstab assistent verbraucher">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">Ansichten</span>
                        Warum gibt es eine einfache und eine erweiterte Ansicht?
                    </h5>
                    <p><strong>Die einfache Ansicht ist für Einrichtung und täglichen Betrieb gedacht.</strong> Sie zeigt nur die wichtigsten Werte für E3DC, Speicher, Wallbox, Wärmepumpe/Verbraucher, Tarif und Standort. Die erweiterte Ansicht bleibt die vollständige bisherige Konfiguration mit Suche, Spezialparametern, Diagnose und Systemwerkzeugen.</p>
                    <ul>
                        <li>Die Ansicht selbst wird nur lokal im Browser gespeichert. Sie ändert keine Anlagenlogik und ist nicht global für alle Nutzer.</li>
                        <li>Beide Ansichten schreiben in dieselbe zentrale Datei <code>data/e3dc_v4.json</code>.</li>
                        <li>Eine vorhandene Wärmepumpe wird in der einfachen Ansicht mit Typ und IP angezeigt, z.B. <code>Luxtronik · 192.0.2.88</code>.</li>
                        <li><strong>WP-Assistent:</strong> vorhandene Wärmepumpe prüfen, aktivieren oder Typ wechseln.</li>
                        <li><strong>BWWP/Heizstab:</strong> Zusatzverbraucher ergänzen, ohne die vorhandene Wärmepumpe zu ersetzen.</li>
                        <li>Die Wallbox-Seite trennt einfache Bedienung nach Energiequelle, Ladeabsicht und Ziel; Phasen, Treiber und Schutzzeiten bleiben in der erweiterten Ansicht.</li>
                    </ul>
                    <p class="mb-0">Die ausführliche Projekt-Dokumentation liegt in <code>doc/Frontend_Ansichten.md</code>.</p>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-charging-station me-2"></i>openWB Primary: PV-geführt oder Direktpfad</h4>
        <div class="col-12 faq-item" data-tags="openwb primary direktpfad sofortladen chargecurrent soc energiemenge secondary pv wallbox">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">openWB</span>
                        Was bedeutet openWB Primary im PV- und Direktpfad?
                    </h5>
                    <p><strong>openWB Primary bleibt der Modus, in dem openWB den Ladepunkt selbst führt.</strong> E3DC-Control liest Leistung und Status aus und führt damit den Speicherrahmen.</p>
                    <ul>
                        <li><strong>Primary PV-geführt:</strong> openWB bleibt im PV-Modus, führt Ladepunkt, PV-Logik und Phasen; E3DC-Control beobachtet die Wallboxleistung und führt den Hausspeicher. In PV und PV + Akku sendet E3DC-Control keine Strom-, Stop- oder Watchdog-Vorgaben an openWB Primary.</li>
                        <li><strong>Primary-Direktpfad:</strong> Wenn E3DC-Control in Primary aktiv Strom vorgibt, nutzt openWB den dokumentierten Sofortladen-Strom <code>chargecurrent</code>. openWB zeigt dann Sofortladen.</li>
                        <li><strong>Netzladen/Ladefenster:</strong> E3DC-Control darf Start, Stop und Stromstärke vorgeben, erzwingt aber keine Phasenumschaltung. Wenn trotz hoher Vorgabe nur 1-phasige Leistung ankommt, erscheint eine Diagnosewarnung <code>Primary 1p</code>.</li>
                        <li><strong>Wichtig:</strong> openWB-Sofortladen-Limits wie Ziel-SoC oder Energiemenge bleiben im Primary-Direktpfad wirksam und können die Ladung beenden.</li>
                        <li><strong>401 Unauthorized:</strong> Falls openWB Benutzerverwaltung/HTTP-Auth erzwingt, werden <code>wb_user</code> und <code>wb_pass</code> als Basic-Auth für die openWB-simpleAPI genutzt. Ohne gesetzten Benutzer sendet E3DC-Control keinen Auth-Header.</li>
                        <li><strong>Aktive E3DC-Control-Stromführung:</strong> Dafür ist openWB Software als <strong>Secondary</strong> oder eine openWB Pro direkt über <code>connect.php</code> der sauberere Pfad.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-tower-broadcast me-2"></i>ENTSO-E als Preis-Fallback</h4>
        <div class="col-12 faq-item" data-tags="entsoe smard fallback api token transparency platform strompreis epex preisquelle">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">Tarif</span>
                        Wie nutze ich ENTSO-E als SMARD-Fallback?
                    </h5>
                    <p><strong>SMARD bleibt die Standardquelle für Börsenstrompreise.</strong> Wenn SMARD keine ausreichenden Zukunftsslots liefert, kann E3DC-Control mit einem ENTSO-E-Token dieselben DE-LU-Day-Ahead-Preise viertelstündlich direkt über die Transparency Platform laden. Erst danach folgt aWATTar als Stundenfallback.</p>
                    <ol>
                        <li>Auf der ENTSO-E Transparency Platform einen Account erstellen und die E-Mail-Adresse bestätigen.</li>
                        <li>Eine E-Mail an <code>transparency@entsoe.eu</code> senden: Betreff <code>RESTful API access</code>, im Text die registrierte E-Mail-Adresse nennen.</li>
                        <li>Die Freigabe abwarten. ENTSO-E nennt dafür üblicherweise bis zu drei Werktage.</li>
                        <li>Nach der Freigabe im Account den RESTful API Security Token erzeugen.</li>
                        <li>Im Config-Editor unter <em>Tarif</em> den ENTSO-E-Token als Fallback-Token eintragen und mit <em>ENTSO-E testen</em> prüfen.</li>
                    </ol>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-shield-halved me-2"></i>Stable 5.4.3j: gebundener Altübergang</h4>
        <div class="col-12 faq-item" data-tags="5.4.3j stable update 5.4.2d ziel snapshot installationsnutzer">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.3j</span>
                        Was korrigiert das Stable-Release 5.4.3j?
                    </h5>
                    <ul>
                        <li><strong>Gezielter Altübergang:</strong> Der flaglose, root-eigene Ziel-Snapshot eines alten 5.4.2d-Aufrufers kann den lokalen Installationsnutzer wieder sicher binden, obwohl der Aufrufer <code>E3DC_BOOTSTRAP_USER</code> entfernt.</li>
                        <li><strong>Enge Eigentümerprüfung:</strong> Die Ersatzbindung ist nur bei fehlender Variable zulässig. Repository und <code>.git</code> müssen nach dem Root-Lock demselben gültigen lokalen Nicht-Root-Nutzer gehören und werden unmittelbar vor dem Finalizer erneut geprüft. Ein bereits gesetzter Nutzerwert bleibt unverändert, muss aber exakt diesem Eigentümer entsprechen.</li>
                        <li><strong>Fail-closed:</strong> Root, <code>www-data</code>, unterschiedliche oder fremde Eigentümer, ein abweichender Nutzerwert und ein ausgetauschtes Repository bleiben gesperrt. Nach dem Finalizer wird die Aufruferumgebung wiederhergestellt.</li>
                        <li><strong>Privater Docker-Matter-Storage:</strong> Der Container bindet vorhandene Verzeichnisse und reguläre Einzel-Link-Dateien nofollow an dieselbe Mountgrenze; Symlinks, Sonderdateien oder Identitätsdrift stoppen den Start. Nach der descriptorgebundenen Härtung auf <code>0700</code>/<code>0600</code> startet der Matter-Worker mit <code>umask 077</code>, sodass auch neue persistente Fabric-, Endpoint- und Sessiondateien höchstens <code>0600</code> erhalten.</li>
                        <li><strong>Keine Regelungsänderung:</strong> HA-, Wallbox-, Speicher-, Wärme- und Direktvermarktungslogik entsprechen unverändert 5.4.3i; Matter-Protokoll und Kopplung bleiben unverändert.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-shield-halved me-2"></i>Stable 5.4.3i: Update, HA und openWB Pro</h4>
        <div class="col-12 faq-item" data-tags="5.4.3i stable update ha matter openwb pro phasen wake-up konsole">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.3i</span>
                        Was bringt das Stable-Release 5.4.3i?
                    </h5>
                    <ul>
                        <li><strong>Altübergang:</strong> Ein älterer 5.4.2-Bestand bindet den lokalen Installationsnutzer aus der Repository-Eigentümerstruktur und reicht ihn sicher an den Ziel-Finalizer weiter.</li>
                        <li><strong>HA-Geheimnisse:</strong> Pairingdatei samt temporärer Schreibdatei, Matter-Storage, Config-Backups, <code>e3dc.config.txt</code>, V4-Temp-/Backup-Dateien und V4-Cache bleiben knotenlokal. Die Web-PIN wird nicht in die gefilterte Partnerkonfiguration übernommen; der Cache folgt dem Schutzmodus <code>0660</code>/<code>0664</code>.</li>
                        <li><strong>HA-Altbestände:</strong> Der Sync arbeitet ohne <code>--delete</code>. Wer HA bereits vor 5.4.3i genutzt hat, prüft deshalb beide Knoten auf alte Kopien und rotiert betroffene Zugangsdaten, Web-PIN oder Matter-Kopplung. <code>e3dc_stats.db</code> samt WebPush-Abonnements bleibt bewusst repliziert.</li>
                        <li><strong>openWB Pro:</strong> Ein bis drei Wake-up-Versuche sind möglich, Standard sind drei. Bei Einstellung <code>1</code> darf der erste vollständig belegte Versuch sperren. Boolesche, nicht endliche oder nicht ganzzahlige Werte sind ungültig und fallen in beiden Pfaden auf drei zurück. Nach einem sicheren Phasenwechsel läuft der Strom wieder an; die 480-Sekunden-Sperre schützt nur vor einem weiteren Phasenwechsel.</li>
                        <li><strong>Einmaliger Übergang:</strong> Installationen bis einschließlich 5.4.3f besitzen den Launcher noch nicht. Der erste Wechsel auf 5.4.3i erfolgt daher über die administrative Konsole; danach steht der Dashboard-Weg bereit.</li>
                        <li><strong>Regelung:</strong> Speicher-, Wärme- und Direktvermarktungslogik entsprechen unverändert 5.4.3h.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-layer-group me-2"></i>Stable 5.4.3f: Wartung für Update, Wallbox und Speicherplanung</h4>
        <div class="col-12 faq-item" data-tags="5.4.3f stable update logrotate venv bookworm wallbox direktvermarktung">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.3f</span>
                        Was bringt das Stable-Release 5.4.3f?
                    </h5>
                    <ul>
                        <li><strong>Update:</strong> Historische Gruppen-Schreibrechte im eindeutig gebundenen Benutzer-venv werden eng entfernt und anschließend erneut verifiziert. Die Bare-Metal-Logrotate-Datei wird als reines LF-UTF-8 atomar projiziert und vom echten Systemparser geprüft.</li>
                        <li><strong>Erstinstallation:</strong> Auf einem frischen Bookworm erfolgt der HTTP-Nachweis geschützter Apache-Laufzeitpfade erst, nachdem der Webbaum atomar veröffentlicht wurde.</li>
                        <li><strong>Wallbox:</strong> Beim Wechsel auf <em>Aus / autonom</em> wird nur die alte Startversuchs-Evidenz verworfen. Stecksession, Ladeende-Latch, Manager-Nullanker und Phasenreservation bleiben geschützt.</li>
                        <li><strong>Direktvermarktung:</strong> Das Exportbudget wird pro lokalem Markttag geführt; Auswahlschritt, Anforderung, Ausgabe und Hardwarewirkung werden getrennt in der Speicherhistorie archiviert.</li>
                        <li><strong>Docker:</strong> Der aktuelle Stable-Kandidat wird erst nach realem Containerstart, Digest-, SBOM- und Provenance-Prüfung auf <code>latest</code> befördert.</li>
                        <li><strong>Installation und Update:</strong> Eine frische Bookworm-Installation läuft in einer festen, geprüften Reihenfolge. Fehler werden verständlich gemeldet und ein vorhandener funktionierender Zustand wird nicht durch eine unvollständige Installation ersetzt.</li>
                        <li><strong>Docker:</strong> Installation und Update werden auf dem Docker-Host ausgeführt. Architektur, vorhandene Instanzen und die tatsächlich gestarteten Dienste werden geprüft; ein fehlerhafter neuer Container wird wieder gestoppt.</li>
                        <li><strong>Speicher und Direktvermarktung:</strong> Netzladen benötigt eine belegte Gesamtunterdeckung. DV-Plan, tatsächliche Wirkung und Ausführungseigentümer bleiben getrennt, damit keine konkurrierenden Speicherbefehle entstehen.</li>
                        <li><strong>Wallbox und openWB:</strong> Neue Stecksessions, Reichweite, Startantwort und Ladeende werden zuverlässiger erkannt. Ein kurzer 0-W-Wert beendet die Ladung nicht, und die Phasenwechselsperre blockiert nur einen weiteren Wechsel.</li>
                        <li><strong>Wärmepumpe und Heizstab:</strong> Wallbox, Wärmepumpe und Heizstab teilen den verfügbaren Leistungsrahmen nach der eingestellten Priorität. Nicht genutzte Startleistung wird wieder freigegeben, ohne Schutzzeiten oder Mindestlaufzeiten zu umgehen.</li>
                        <li><strong>Prognose und Ladekurve:</strong> Mit Direktvermarktung erscheint nur die DV-Prognose, sonst nur die Standardprognose. Der aktuelle SoC wird als eigener frischer Messpunkt gezeigt; die Diagramme laden schneller und besitzen einen übersichtlicheren Zeitstrahl.</li>
                        <li><strong>Sicherheit und Rückfall:</strong> Fehlende, veraltete oder widersprüchliche Daten bleiben ohne neue Hardwarefreigabe. Update und Rückfall prüfen Dienste, Rollen und Gesundheit vollständig; Diagnose- und Shadow-Funktionen bleiben ohne Steuerwirkung.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.2d: Updateabschluss und Rücklauf</h4>
        <div class="col-12 faq-item" data-tags="5.4.2d stable hotfix update systemd dienst endzustand masken ruecklauf rollback">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.2d</span>
                        Was korrigiert das Stable-Release 5.4.2d?
                    </h5>
                    <ul>
                        <li><strong>Dienst-Endzustand:</strong> Der Updater bewertet erforderliche Dienste nach Enable und Restart anhand ihres belegten systemd-Endzustands. Ein Zwischen-Rückgabecode allein erzeugt keinen falschen Fehlschlag mehr.</li>
                        <li><strong>Optionale Units:</strong> Eine nicht installierte optionale Unit wird beim verifizierten Maskenrücklauf als legitimer fehlender Zustand behandelt.</li>
                        <li><strong>Fail-closed bleibt:</strong> Echte Start-, Masken- oder Wiederherstellungsabweichungen brechen weiterhin ab und halten Writer sowie Aktoren sicher gestoppt.</li>
                        <li><strong>Keine EMS-Änderung:</strong> Speicher-, Direktvermarktungs-, Wallbox-, Wärme-, Prognose- und Hardwaresteuerung entsprechen unverändert 5.4.2c.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.2c: Wallbox, Octopus Heat und Docker</h4>
        <div class="col-12 faq-item" data-tags="5.4.2c stable hotfix wallbox modus 5 netzladen predump octopus heat eco docker">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.2c</span>
                        Was korrigiert das Stable-Release 5.4.2c?
                    </h5>
                    <ul>
                        <li><strong>Wallbox-Netzladeslot:</strong> Ein frischer, ausdrücklich gültiger Modus-5-Netzladeslot fällt nach bestandenen harten Schutzprüfungen nicht mehr durch den rein wirtschaftlichen Pre-Dump-Floor auf 0 A.</li>
                        <li><strong>Speicher bleibt geschützt:</strong> Der Slot erzwingt keine Batterieentladung für das Fahrzeug. Nutzer-<code>Aus</code>, manuelle Sperren, Notstromreserve, Hardwarelimits und Datenfrische bleiben vorrangig.</li>
                        <li><strong>Octopus Heat:</strong> Die festen Niedrigtariffenster werden unabhängig vom Eco-Modus über eine lokale Tarifzeitachse abgebildet. Die Wärmepumpe benötigt weiterhin jede aktuelle Freigabe; veraltete oder unpassende Pläne schließen fail-closed.</li>
                        <li><strong>Docker:</strong> Dokumentation und Compose-Hinweise unterscheiden den GHCR-Normalweg vom lokalen Entwickler-Selbstbau und erklären die getrennten Daten-, Log-, ML- und Prognosebeleg-Volumes.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.2b: Alt-Updater-Übergang</h4>
        <div class="col-12 faq-item" data-tags="5.4.2b stable hotfix update alt updater finalizer snapshot rollback">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.2b</span>
                        Was korrigiert das Stable-Release 5.4.2b?
                    </h5>
                    <ul>
                        <li><strong>Erneute Zielbindung:</strong> Ein bereits vor dem Zielwechsel gestarteter Alt-Updater bindet Installationswurzel, Ziel-Commit, Version, Release-Tag und alle benötigten Finalizer-Dateien erneut an den freigegebenen Stand.</li>
                        <li><strong>Versiegelte Fortsetzung:</strong> Die privilegierte Weiterverarbeitung startet ausschließlich aus einem privaten, root-eigenen und schreibgeschützten Snapshot. Nur genau ein passender SHA-/Tag-Erfolgsmarker wird akzeptiert.</li>
                        <li><strong>Kein falscher Rollback:</strong> Eine reine Bereinigungsabweichung nach bereits erfolgreichem Finalizerlauf bleibt sichtbar, löst aber keinen Rollback des erfolgreichen Zielstands aus.</li>
                        <li><strong>Keine EMS-Änderung:</strong> Speicher-, Direktvermarktungs-, Wallbox-, Wärme-, Prognose- und Hardwaresteuerung entsprechen unverändert 5.4.2a.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.2a: Speicher, Heizstab und Alt-Updater</h4>
        <div class="col-12 faq-item" data-tags="5.4.2a stable hotfix speicher kurve power settings ems user charge limit auto max charge power dc first netzladen heizstab pv-auto aus pro3em alt updater service helper prognosediagnose">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.2a</span>
                        Was korrigiert das Stable-Release 5.4.2a?
                    </h5>
                    <ul>
                        <li><strong>Keine Selbst-Rückkopplung:</strong> Ein <code>EMS_USER_CHARGE_LIMIT</code>-Readback aus frischen, validen <code>POWER_SETTINGS</code> gilt nur bei ausdrücklich konfigurierter <code>maximumladeleistung</code> und einer strikt unter 50 W liegenden Abweichung zu <code>EMS_MAX_CHARGE_POWER</code> als reflektierter flüchtiger Laderahmen. Andernfalls bleibt die USER-Grenze wirksam.</li>
                        <li><strong>Kurvenrückstand:</strong> Liegt der Speicher hinter der Ladekurve, öffnet der Manager den Laderahmen in <code>AUTO</code> nur bei positiver, frischer E3/DC-only-Evidenz bis <code>MAX_CHARGE_POWER</code>. Unbekannte oder veraltete Pfadzuordnung bleibt fail-closed.</li>
                        <li><strong>Offene Hausversorgung:</strong> Entladen bleibt offen. Bei zusätzlicher AC-PV wird der Laderahmen weiterhin sanft und DC-first auf die frisch belegte interne E3/DC-PV-Leistung nachgeführt.</li>
                        <li><strong>Lokales Heizstab-Aus ist hart:</strong> <code>PV-AUTO AUS</code> und der Heizstab-Hauptschalter verhindern positive Anforderungen aus PV-Überschuss, Pre-Dump, Marktpfad und manuellem Vollgas. Der bestätigte AUS-Zustand wird gehalten.</li>
                        <li><strong>Pro3EM bleibt eigenständig:</strong> Fehlen in einer Alt- oder Teilkonfiguration Relais-ID oder Steuerfreigabe, bleibt der Pro3EM im Messbetrieb. Ein separat freigegebener Pro3EM-Wärmepumpenpfad bleibt vom lokalen Heizstab-<code>PV-AUTO AUS</code> unabhängig; das globale <code>AUTO AUS</code> stoppt und hält beide Pfade.</li>
                        <li><strong>Alt-Updater:</strong> Ein aus 5.4.0a weiterlaufender alter Service-Helper blockiert den Releasewechsel nicht mehr an der ausgeschalteten Prognosediagnose. Bei ausdrücklich aktivierter Diagnose bleibt ein unvollständiger Helper-Vertrag fail-closed.</li>
                        <li><strong>Sicherer Produktpfad:</strong> Der root-eigene Prüfsnapshot liefert ausschließlich den verifizierten Finalizer-Code. Logs, systemd-Units, Notifier-Rechte, Web-Wrapper und Sudoers-Einträge werden gegen die gebundene Produktinstallation erzeugt und verweisen nach dem Löschen des Snapshots nicht auf dessen temporären Pfad.</li>
                        <li><strong>Keine Netzladefreigabe:</strong> Der Hotfix fordert weder <code>GRID</code> noch einen aktiven Ladebefehl an. Die übrigen Funktionen und Schutzverträge entsprechen unverändert 5.4.2.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-battery-three-quarters me-2"></i>Stable 5.4.2: Speicherplanung und Diagnose</h4>
        <div class="col-12 faq-item" data-tags="5.4.2 stable direktvermarktung hold auto e3dc pv ladebegrenzung peak shaving prognose diagnose installation wallbox">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.2</span>
                        Was bringt das Stable-Release 5.4.2?
                    </h5>
                    <ul>
                        <li><strong>Direktvermarktung:</strong> Der Tag ist lückenlos in 15-Minuten-Abschnitte geplant. Ein gebundener Abschnitt <em>Speicherplatz halten</em> sperrt Laden und lässt Hausversorgung aus dem Speicher zu. Nach dem letzten PV-Speicherfenster folgt wieder normaler E3/DC-AUTO-Betrieb, sofern kein stärkerer Speicherentscheider wirkt.</li>
                        <li><strong>DV-Planer-Shadow:</strong> Ein zusätzlicher wirkungsloser Diagnosevertrag prüft die fünf eindeutigen Speicheraktionen gegen Planbindung, Datenfrische, Topologie, Netzpunkt und Reserve. Er verändert weder die laufende Regelung noch Plan-/Slot-Identitäten.</li>
                        <li><strong>E3/DC-PV-Ladebegrenzung:</strong> Geplante Speicherladung kann sanft auf die frische E3/DC-PV-Leistung begrenzt werden. Entladen bleibt in AUTO offen; Leistung eines zusätzlichen AC-Wechselrichters erhöht den Rahmen nicht.</li>
                        <li><strong>Peak Shaving:</strong> Die neue Lastspitzenbegrenzung schützt feste Zähler-Viertelstunden mit Sicherheitsabstand, Hysterese, Messlückenprüfung und einem Speicherpuffer oberhalb der Notstromreserve. Sie ist standardmäßig aus.</li>
                        <li><strong>PV-Prognosediagnose:</strong> Ein optionaler read-only Dienst vergleicht die sichtbare E3/DC-DC-Punktprognose nach Erfassungs-Vorlauf mit abgeschlossener Historie. Rohdaten bleiben privat; ein P50 wird nicht behauptet und es gibt keine Rückwirkung auf Prognosemodell oder Regelung.</li>
                        <li><strong>Installation:</strong> Frische, unvollständige und widersprüchliche Installationen werden getrennt behandelt. Fehler werden bis zum Exitcode weitergegeben und nicht mehr als erfolgreicher Abschluss angezeigt. Ein aus 5.4.0a gestarteter Altprozess kann nach dem verifizierten Baumwechsel mit seinem gecachten Backup-Validator sicher fortfahren.</li>
                        <li><strong>Oberfläche:</strong> Tarifoptionen, Speicherzustand und Zwei-Wallbox-Konfiguration sind klarer gruppiert und in verständlicher Sprache beschrieben.</li>
                    </ul>
                </div>
            </div>
        </div>

        <div class="col-12 faq-item" data-tags="diagnose shadow waerme intent prognose quantil keine regelwirkung">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Diagnose</span>
                        Was bedeutet der Wärme-Intent im Shadow?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Der Wärme-Intent ist ein lokaler, revisionsgebundener Diagnosevertrag. Er vergleicht einen möglichen Wärmebedarf mit konservativer PV-Deckung und zeigt, ob Plan, Zeitslot und Prognoseevidenz vollständig zusammenpassen.</p>
                    <ul>
                        <li><strong>Keine Hardwarewirkung:</strong> Der Vertrag ist immer <code>shadow_only</code>, erlaubt keine Befehle und kann weder Wärmepumpe noch Heizstab ansteuern.</li>
                        <li><strong>Keine automatische Freigabe:</strong> Auch ein vollständig belegter Intent wählt keinen aktiven Regler aus und ändert keine Konfiguration.</li>
                        <li><strong>Quantile statt Scheinsicherheit:</strong> Eine bloße Punktprognose beziehungsweise ein einzelner Medianwert reicht für eine konservative Wärmeentscheidung nicht aus.</li>
                        <li><strong>Hochpreis bleibt Shadow:</strong> Allgemeine teure Preisfenster dürfen ohne eigenen ausdrücklichen Aktivierungsvertrag keine Wärmepumpenpause und keinen Warmwasserabbruch auslösen.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable 5.4.1d: Klima-, Update- und Vitals-Wartung</h4>
        <div class="col-12 faq-item" data-tags="5.4.1d stable docker klima shelly update backup ml lock batterie vitals dcb int32">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.1d</span>
Was korrigiert das Stable-Release 5.4.1d?
                    </h5>
                    <ul>
                        <li><strong>Klima im Docker:</strong> Ein beim Containerstart aktivierter read-only Messworker übernimmt Deaktivierung und Shelly-Kanalwechsel im nächsten Abfragezyklus. Für die erstmalige Aktivierung aus <code>Aus</code> ist wie bei anderen optionalen Docker-Diensten ein Container-Neustart erforderlich.</li>
                        <li><strong>ML-Backup:</strong> Der laufende Updater kann vor dem Backup ausschließlich einen eindeutig sicheren und unbelegten Alt-Lock auf den Installationsbenutzer und Modus <code>0600</code> normalisieren. Modell, Manifest und Lockinhalt bleiben unverändert.</li>
                        <li><strong>Bereits blockierte Altstände:</strong> Updater bis einschließlich 5.4.1c benötigen bei der exakten Meldung <code>Unsicherer privater ML-Eintrag: .ml_model.lock</code> einmalig den dokumentierten Metadaten-Feldfix. Die Sperrdatei darf nicht gelöscht werden.</li>
                        <li><strong>Batterie-Vitals:</strong> Ein bestätigter DCB-Packindex darf als <code>Uint16</code> oder <code>Int32</code> zurückkommen. Negative, nichtnumerische oder vom angeforderten Pack abweichende Werte bleiben gesperrt.</li>
                        <li><strong>Releaseumfang:</strong> Speicher-, Wallbox-, Wärme- und Direktvermarktungsentscheidungen entsprechen unverändert 5.4.1c.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-layer-group me-2"></i>Stable 5.4.1: Wallbox-, Update- und Diagnosekonsolidierung</h4>
        <div class="col-12 faq-item" data-tags="5.4.1 stable openwb pro phasenwechsel balancing update docker frequenz sg ready shelly sicherheit batterie vitals dcb">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.1</span>
                        Was bringt das Stable-Release 5.4.1?
                    </h5>
                    <ul>
                        <li><strong>openWB Pro:</strong> Start, Pause, Ladeende und Phasenwechsel bleiben an frische Stecksession und bestätigte Rückmeldungen gebunden. Die 480-Sekunden-Sperre verhindert nur einen weiteren Phasenwechsel.</li>
                        <li><strong>Balancing:</strong> Ein- und dreiphasige Fahrzeuge werden anhand ihrer tatsächlichen Phasenzahl leistungsfair statt nach einer pauschalen Ampere-Summe verteilt. Die konfigurierbare Phasenzuordnung bleibt Diagnose; ohne echten PCC-RMS-Stromvektor bleibt eine einphasige Freigabe über 20 A gesperrt.</li>
                        <li><strong>Update und Docker:</strong> Der unterstützte Erstwechsel aus 5.3.2b behält Konfiguration und installierte optionale Dienste. Docker-Build, Attestierungsprüfung und Stable-Promotion laufen als zusammenhängende Transaktion.</li>
                        <li><strong>Diagnose:</strong> Netzfrequenz, SG-Ready-/Shelly-Aktivität und externe Wetterlade-Konflikte sind sichtbar. Batterie-Vitals fragt jeden DCB-Pack mit seinem typisierten Index ab und bindet vorhandene Antwortindizes. Fehlende neue optionale Statuswerte bleiben unbekannt.</li>
                        <li><strong>Sicherheit:</strong> Watchtower ist nur noch ein bewusster Opt-in; gemeldete Status- und Fehlertexte werden im Konfigurationseditor nicht als ungeprüftes HTML eingesetzt.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.0e: Dienstgeneration des Alt-Updaters</h4>
        <div class="col-12 faq-item" data-tags="5.4.0e hotfix update 5.3.2b dienste module install center">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.0e</span>
                        Was korrigiert das Stable-Release 5.4.0e?
                    </h5>
                    <ul>
                        <li><strong>Definierter 5.3.2b-Übergang:</strong> Der noch laufende Alt-Updater startet nach dem Git-Wechsel ausschließlich die Pflichtdienste und die vor dem Wechsel bereits installierten, in der eingefrorenen Konfiguration aktiven Zusatzdienste. Deaktivierte Zusatzdienste bleiben aus.</li>
                        <li><strong>Erster Aufruf:</strong> Für diesen einmaligen Übergang ausdrücklich <code>installer_main.py --update-e3dc</code> an der administrativen Konsole verwenden, nicht den interaktiven Installer-Menüpunkt. Ältere Installationen wechseln zuerst über den dokumentierten Bootstrap auf 5.3.2b.</li>
                        <li><strong>Keine unerwartete Aktivierung:</strong> Vorhandene Konfigurationsfelder allein installieren oder starten keine neue Wallbox-, Wärme- oder Integrationssteuerung.</li>
                        <li><strong>Sichtbare Diagnose:</strong> Konfigurierte, aber bislang nicht installierte Zusatzmodule werden im Updateprotokoll genannt und können anschließend bewusst über das Install-Center eingerichtet werden.</li>
                        <li><strong>Unveränderte Regelung:</strong> Betriebskonfiguration, openWB-Pro-Regelung und andere fachliche Reglerbytes wurden nicht verändert.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.0d: Rechtevertrag des Alt-Updaters</h4>
        <div class="col-12 faq-item" data-tags="5.4.0d hotfix update 5.3.2a 5.3.2b rechte wallbox planer lockdatei">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.0d</span>
                        Was korrigiert das Stable-Release 5.4.0d?
                    </h5>
                    <ul>
                        <li><strong>Alt-Updater:</strong> Private Verzeichnisse werden auch unter einem <code>setgid</code>-Datenordner exakt auf <code>0700</code> gesetzt. Die gemeinsame Wallbox-Lockdatei verwendet in beiden Schreibpfaden einheitlich <code>0600</code>.</li>
                        <li><strong>Wiederherstellung:</strong> Breite Webroot-Reparaturen überspringen private Matter- und Wallbox-Bäume und verändern deren Eigentümer oder Modi nicht.</li>
                        <li><strong>Sicherheitsgrenze:</strong> Der private Planer-Transaktionsbaum bleibt ausschließlich dem Webserver-Benutzer vorbehalten; die Prüfung wurde nicht gelockert.</li>
                        <li><strong>Wallbox-Regelung:</strong> Die in 5.4.0c geprüfte openWB-Pro-Regelung bleibt unverändert.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.0c: Alt-Updater und openWB Pro</h4>
        <div class="col-12 faq-item" data-tags="5.4.0c hotfix update 5.3.2b pep668 sudoers openwb pro pause soc stecksession ladeende phasenwechsel">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.0c</span>
                        Was korrigiert das Stable-Release 5.4.0c?
                    </h5>
                    <ul>
                        <li><strong>Alt-Updater:</strong> Der Web-Update-Pfad aus 5.3.2b übernimmt nach dem Git-Wechsel den neuen Rechtevertrag. Vom Updater selbst angehaltene Dienste sind kein Rechtefehler; leere Paketlisten starten kein System-<code>pip</code>. Für den ersten Wechsel muss das Benutzer-venv bereits vorhanden sein. Ist der alte privilegierte Web-Launcher selbst fehlend oder nicht ausführbar, ist einmalig die SSH-Reparatur mit dem ausdrücklich dokumentierten <code>--update-e3dc</code>-Aufruf erforderlich.</li>
                        <li><strong>Sudoers:</strong> Klar abgegrenzte fremde ioBroker-Freigaben bleiben unverändert und blockieren das E3DC-Control-Update nicht. Fremde direkte E3DC-<code>systemctl</code>-Freigaben bleiben gesperrt.</li>
                        <li><strong>openWB Pro:</strong> Der Start folgt nach bestätigtem Anstecken zügig dem verfügbaren Budget. Der Phasenwechsel nutzt eine kurze sichere CP-Unterbrechung; die folgenden 480 Sekunden sperren nur einen weiteren Phasenwechsel und nicht die Ladung.</li>
                        <li><strong>Pause und SoC:</strong> Eine Pause gilt erst nach bestätigtem STOP. Der SoC-Fallback verwendet ausschließlich die aktuelle Stecksession und überschreibt keine echten Fahrzeug- oder Wallboxwerte.</li>
                        <li><strong>Ladeende:</strong> Ein bestätigtes Ladeende bleibt über einen Manager-Neustart gesperrt, bis eine neue Stecksession, eine ausdrückliche Nutzeränderung oder eine echte Wiederaufnahme belegt ist.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.0b: Update-, Wallbox- und Speicherkompatibilität</h4>
        <div class="col-12 faq-item" data-tags="5.4.0b hotfix update pep668 venv docker wrapper sudoers systemd maske openwb pro stecksession cp phasenwechsel pv kurve zero budget power settings readback headroom">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.0b</span>
                        Was korrigiert das Stable-Release 5.4.0b?
                    </h5>
                    <ul>
                        <li><strong>Update:</strong> Python-Abhängigkeiten werden auf PEP-668-Systemen im gebundenen Benutzer-venv installiert. Docker-Installationen zeigen stattdessen die notwendigen <code>docker compose</code>-Befehle für den Host.</li>
                        <li><strong>Transaktion:</strong> Wrapper, sudoers und kanonische systemd-Masken werden vor einer Änderung gebunden gesichert und bei einem Teilfehler kontrolliert zurückgesetzt. Der neue Zielstand wird nach dem Git-Wechsel nur aus dem freigegebenen Zielbaum finalisiert.</li>
                        <li><strong>openWB Pro:</strong> Ein bestätigtes Ab- und Wiederanstecken erzeugt eine neue Stecksession. Die Stromfreigabe erfolgt vor einem optionalen, begrenzten CP-Wake-up, der im Automatikmodus eine unterstützte Geräte-API verlangt; bereits passende Phasen werden ohne unnötigen Wechsel übernommen.</li>
                        <li><strong>PV-Kurve:</strong> Alle unterstützten Wallboxpfade verwenden denselben Halte-/Stoppvertrag bei fehlendem PV-Budget. Kurze Batteriestützung einer laufenden Ladung bleibt im vorhandenen Energiebudget erlaubt.</li>
                        <li><strong>Speicher:</strong> Eine unklare <code>POWER_SETTINGS</code>-SET-Antwort gilt nur bei exakt passendem GET-Readback als bestätigt. Frische Hardwaregrenzen können den konfigurierten Ladewert absenken, aber temporäre Grenzen werden nicht als neue Ladefähigkeit geplant.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-screwdriver-wrench me-2"></i>Stable-Hotfix 5.4.0a: Update- und Shelly-Kompatibilität</h4>
        <div class="col-12 faq-item" data-tags="5.4.0a hotfix update apt matter wrapper crlf sudo shelly em gen1 klima">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.0a</span>
                        Was korrigiert der Stable-Hotfix 5.4.0a?
                    </h5>
                    <ul>
                        <li><strong>Core-Update:</strong> Optionale Node.js-, npm-, Avahi- und D-Bus-Pakete werden nur noch bei einer ausdrücklich gestarteten Matter-Installation benötigt. Konflikte dieser Pakete blockieren das normale E3DC-Control-Update nicht mehr.</li>
                        <li><strong>Web-Updater:</strong> Der Installer-Wrapper wird vor der sudo-Freigabe gegen die veröffentlichten Git-Bytes geprüft. Eine reine CRLF-Shebang-Beschädigung lässt sich kontrolliert reparieren; unbekannte Abweichungen brechen sicher ab.</li>
                        <li><strong>Shelly EM Gen1:</strong> Alte Shelly-EM-Zähler können im Klima-Verbraucher über ihre lokale read-only-Status-API eingebunden werden. Kanal 0, Kanal 1 oder die Summe werden ausdrücklich ausgewählt; ungültige Messwerte bleiben unbekannt.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-shield-halved me-2"></i>Basis-Release 5.4.0: sichere Energie-Arbitration und transaktionale Migration</h4>
        <div class="col-12 faq-item" data-tags="5.4.0 stable release owner context backup rollback matter shadow wallbox waermepumpe">
            <div class="card bg-card border-0 shadow-sm">
                <div class="card-body">
                    <h5 class="card-title">
                        <span class="tag">5.4.0</span>
                        Was ändert das Stable-Release 5.4.0?
                    </h5>
                    <p><strong>5.4.0 ordnet Speicher, Direktvermarktung, Wallbox und Wärmeverbraucher einem eindeutigen Regel-Owner zu.</strong> Kontext und Owner werden unmittelbar vor Hardwareausgängen geprüft; Update-, Backup- und Webaktionen hinterlassen bei einem Fehler keinen halben Zustand.</p>
                    <ul>
                        <li><strong>Speicher und Markt:</strong> Ungültige Provider-, Cache- oder Anlagendaten erzeugen einen explizit inaktiven Plan. Reserve und permanente Gerätegrenzen bleiben unangetastet.</li>
                        <li><strong>Headroom:</strong> Interne DC-PV und zusätzliche AC-Erzeuger werden getrennt bilanziert. DC- und Netzpunktdruck werden mit dem größeren Wert bewertet und nicht doppelt addiert.</li>
                        <li><strong>Wallbox:</strong> openWB Pro kann eine bestätigte Startfreigabe nach einem veralteten Nullzustand ohne Umstecken erneut übernehmen. Mehrere Ladepunkte werden anhand ihrer L1/L2/L3-Ströme und der Netzpunktreserve verteilt; ein- und dreiphasige Amperewerte werden nicht pauschal addiert. Die strengere leistungsfaire Zuteilung und die konservative PCC-RMS-Freigabe gelten ab 5.4.1. Die ruhige PV-Kurve erlaubt bei laufender Ladung höchstens 75 Wh Batteriestützung.</li>
                        <li><strong>Wärmepumpe:</strong> Wallboxaktionen oder der Verlust eines Wallboxkontexts stoppen keine bereits laufende Wärmepumpe eigenständig. Hardwarebefehle bleiben an frische, treiberspezifische Rückmeldungen gebunden.</li>
                        <li><strong>iDM-Diagnose:</strong> Der manuelle Scanner liest Register 1006 genau einmal per FC04. Er schreibt weder dieses Register noch andere Register.</li>
                        <li><strong>Mobile Energieflüsse:</strong> Desktop- und Mobile-Positionen besitzen getrennte Revisionen; gleichzeitige Änderungen werden erkannt statt überschrieben.</li>
                        <li><strong>Fehlerpfad:</strong> Der Watchdog beendet Writer geordnet und sendet keine zweite rohe RSCP-, Wallbox- oder Wärmepumpensequenz.</li>
                        <li><strong>Update und Rollback:</strong> Ein leeres oder unlesbares Backup ist ein harter Fehler. Der Wechsel zum bereinigten Verlauf erfolgt über den Installer-/Bootstrapweg und nicht über <code>git pull</code>.</li>
                        <li><strong>Matter:</strong> Smart Home bleibt erhalten. Neue Kopplungsdaten werden installationsindividuell und privat gespeichert; bestehende Fabrics werden nicht gelöscht.</li>
                        <li><strong>Shadow und V2X:</strong> Shadow bleibt eine read-only Vergleichsinstanz ohne Hardwareausgang. V2H-/V2G-Telemetrie bleibt sichtbar, aktive bidirektionale Steuerung ist nicht freigegeben.</li>
                        <li><strong>Rollback:</strong> Der sanitierte Root <code>v5.3.2b</code> bleibt als Docker-Rückfall-Image verfügbar. Auf Bare Metal bleibt die Wiederherstellung aus einem verifizierten Datei-Backup der unterstützte Rückweg.</li>
                    </ul>
                </div>
            </div>
        </div>

        <h4 class="mb-4 text-accent"><i class="fas fa-charging-station me-2"></i>Wallbox & Laden</h4>

        <!-- Native Wallbox Manager -->
        <div class="col-12 faq-item" data-tags="wallbox native python manager wbmode go-e openwb mqtt multi laden">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Wie aktiviere ich den "Native Wallbox Manager" (go-e) und warum ist wbmode=0 extrem wichtig?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Seit v3.8.8 bietet E3DC-Control einen <strong>eigenständigen (nativen) Python Wallbox Manager</strong>. Dieser ermöglicht Multi-Wallbox-Laden (2 Ladepunkte) mit einer Vorrang-Regelung (z.B. WB1 zuerst, Rest an WB2) und entkoppelt die Ladelogik komplett von der veralteten C++ Steuerung.</p>
                    <p>Damit sich die alte C++ Steuerung und der neue Python Manager nicht gegenseitig stören (Flapping), <strong>MUSS</strong> zwingend in der Konfiguration die klassische Wallbox-Steuerung deaktiviert werden!</p>
                    <pre>wbmode = 0</pre>
                    <p>Ist dies gesetzt, können Sie in der neuen Sektion <em>"Native Wallbox Regelung"</em> im Konfigurations-Editor die IP-Adressen und den gewünschten Prioritäts-Modus einstellen.</p>
                </div>
            </div>
        </div>

        <!-- E3/DC wallbox transport, family and backend -->
        <div class="col-12 faq-item" data-tags="wallbox e3dc efy easy connect multi connect rscp wbchar6 backend capability">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Wie unterscheiden sich E3/DC-Wallboxfamilie, RSCP-Transport und Steuer-Backend?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p><strong>efy, easy connect und multi connect nutzen denselben E3/DC-RSCP-Transport &uuml;ber das Hauskraftwerk.</strong> Ein erreichbarer Ladepunkt oder <code>WB_EXTERN_DATA_ALG</code> beweist deshalb keine bestimmte Produktfamilie. Dashboard und Wallbox-Seite zeigen Familie, Firmware, beobachteten RSCP-Typ, Read-Capability und Backend getrennt.</p>
                    <p>Drei vorhandene, typg&uuml;ltige Sun-/Auto-/Abort-Readbacks beweisen allein noch keine Schreibsemantik. Sie werden deshalb nur diagnostisch angezeigt. Direkte Sun-/Auto-/Abort-, Maximalstrom- und native Phasenbefehle sind in diesem Stable-Release bedingungslos no-send. Ein beobachteter numerischer RSCP-Typ wird nicht global einer Familie zugeordnet.</p>
                    <p><strong>E3/DC efy/Easy &ndash; WBchar6-Kompatibilit&auml;tsregelung</strong> bleibt der empfohlene Community-Laufzeitpfad f&uuml;r Modus, Strom und episodischen Start/Stop. efy und Multi Connect erhalten h&ouml;chstens einen Start-Toggle je frisch best&auml;tigter Stop-Episode. F&uuml;r Easy Connect sind h&ouml;chstens drei explizite Startimpulse mit mindestens 60&nbsp;Sekunden Abstand erlaubt; jeder Versuch braucht erneut einen frischen Stop-Readback und endet sofort bei best&auml;tigter Ladung. Nach einem direkten Schreibfehler gibt es im selben Zyklus keinen WBchar6-Retry.</p>
                    <p>Bei neuen E3/DC-Konfigurationen ist dieser Kompatibilit&auml;tspfad sichtbar vorausgew&auml;hlt. Wer bewusst <em>Nur Status</em> w&auml;hlt, erh&auml;lt keine E3/DC-Regelbefehle; eine ausdr&uuml;cklich gespeicherte <code>0</code> wird bei Updates nicht &uuml;berschrieben. Mode&nbsp;0, Beobachten und Freigabe bleiben ohne eigene frische Ownership schreibstumm.</p>
                    <div class="alert alert-warning mb-0 border-0">
                        Native E3/DC-Phasenumschaltung und der direkte Maximalstrom-Setter bleiben gesperrt. F&uuml;r openWB Pro laufen 0&nbsp;A und <code>phasetarget</code> in getrennten Managerzyklen. <code>phasetarget</code> besitzt die CP-Signalisierung; ein zus&auml;tzlicher CP-Impuls bleibt kurz. Nach frischem CP-inaktiv- und Zielphasen-Readback darf die Ladung wieder anlaufen; mindestens 480&nbsp;Sekunden sind nur bis zum n&auml;chsten Phasenwechsel gesperrt.
                    </div>
                </div>
            </div>
        </div>

        <!-- openWB Autoerkennung -->
        <div class="col-12 faq-item" data-tags="wallbox openwb autoerkennung primary secondary simpleapi ladepunkt">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Wie funktioniert die automatische openWB-Erkennung?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>E3DC-Control liest openWB Software 2.x nur aus: Ladepunkte werden über <code>get_chargepoint_all</code> und, wenn erreichbar, über das V1-Config-Topic geprüft. Die openWB selbst wird dabei nicht umgestellt.</p>
                    <p>Erkennt der Treiber einen internen openWB-Ladepunkt oder den Primary-/Serienpfad, nutzt er den Primary-simpleAPI-Pfad. Erkennt oder erzwingt die Konfiguration Secondary/Modbus, bleibt der Sollstrom- und Heartbeat-Pfad aktiv.</p>
                    <p>Meldet eine openWB zwei Ladepunkte und ist WB2 noch leer, kann E3DC-Control den zweiten Ladepunkt zur Laufzeit anzeigen und budgetieren. Nach drei nicht bestätigten Schreibbefehlen wird die Steuerung kurz pausiert und der Fehler sichtbar im Frontend gemeldet.</p>
                    <p>Bleibt eine angesteckte und freigegebene openWB Pro nach einem abgelaufenen Nullzustand stehen, verwirft der Manager nur veraltete eigene Startanker und projiziert die positive Freigabe nach frischer Bereitschaft erneut. Umstecken, ein Manager-Neustart oder wiederholte CP-Schaltungen sind dafür nicht erforderlich.</p>
                </div>
            </div>
        </div>

        <!-- 16A vs 22kW Limitierung -->
        <div class="col-12 faq-item" data-tags="wallbox 16a 32a 11kw 22kw ladestrom limit grenze einphasig dreiphasig">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Warum lädt meine Wallbox nicht mit voller Leistung (nur 16 Ampere / 3.6kW)?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Das System übermittelt Lademengen als Ampere-Vorgabe (z.B. "16A"). Die effektiv aus dem Netz gezogene Leistung hängt physikalisch von der angeschlossenen Phasenanzahl ab:</p>
                    <ul>
                        <li><strong>Einphasiges Laden (L1):</strong> 16 Ampere * 230 Volt = ~3.680 Watt (3,6 kW)</li>
                        <li><strong>Dreiphasiges Laden:</strong> 16 Ampere * 230 Volt * 3 = ~11.040 Watt (11 kW)</li>
                    </ul>
                    <p>Wenn Ihr Auto nur einphasig lädt, ist bei 16A physikalisch bei 3.6kW Schluss. Haben Sie eine zugelassene <strong>22 kW Wallbox</strong> installiert und wollen bis zu 32A ins Auto schicken (7.2kW einphasig / 22kW dreiphasig), heben Sie im Konfigurations-Editor den globalen Fallback <strong>Max. Ladestrom</strong> oder in <em>Wallbox</em> gezielt <strong>WB1 Max A</strong>/<strong>WB2 Max A</strong> an. So kann z.B. WB1 mit 32A und WB2 weiter mit 16A begrenzt bleiben.</p>
                    <p>Beim Betrieb mehrerer Ladepunkte addiert E3DC-Control die angezeigten Amperewerte nicht als einzelne Gesamtsumme. Maßgeblich für die leistungsfaire Verteilung sind die reale Phasenzahl sowie Fahrzeug- und Ladepunktgrenzen; gemessene L1/L2/L3-Ströme am Ladepunkt bleiben zusätzliche Diagnose. Solange kein bestätigter phasenaufgelöster PCC-RMS-Stromvektor vorhanden ist, bleibt der Hausanschlussschutz konservativ; aus phasenbezogener Wirkleistung wird keine zusätzliche Amperefreigabe abgeleitet.</p>
                </div>
            </div>
        </div>

        <!-- UI Glitches & Phase Summation -->
        <div class="col-12 faq-item" data-tags="ui glitch hausverbrauch 0 watt einbruch anzeige fehler asynchron phasen addition summation">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">UI</span>
                        Warum bricht der Hausverbrauch (Live-Dashboard) manchmal sekundenlang auf 0 Watt ein?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Die interne E3DC-Firmware hat unter Volllast ein kleines Synchronisationsproblem: Sie fragt den Root-Zähler, die Batterie und die Wallbox nicht auf die exakte Millisekunde zeitgleich ab.</p>
                    <p>Zieht Ihre Wallbox asymmetrisch Strom (taktendes Laden), verrechnet E3DC interne Zählerstände, die wenige Millisekunden auseinander liegen. Das resultiert in sogenannten "Glitches", bei denen der E3DC-Hausverbrauch oft extrem zuckt oder auf 0W fällt.</p>
                    <p><strong>Die Lösung (Phase-Summation):</strong> Ab Version v3.9.6 nutzt E3DC-Control eine Anti-Glitch Fallbacksicherung. Bei unplausiblen E3DC-Daten berechnet PHP den echten Hausverbrauch über die physikalische Summe der einzelnen Netz-Phasen (<code>Haus = PV + Grid - Wallbox - Batterie</code>) vollautomatisch und ruckelfrei nach.</p>
                </div>
            </div>
        </div>

        <!-- Wallbox-Modi & Preislimit -->
        <div class="col-12 faq-item" data-tags="wallbox modi preislimit sofortladen puffer wolken abschaltung grid clamp">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Was machen die neuen Wallbox-Modi?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p><strong>Aus / autonom:</strong> ist NGNA. E3DC-Control beobachtet die Wallbox, sendet aber keine laufenden Ladebefehle. Nur ein bewusster Wechsel auf <code>Aus</code> in der Wallbox-WebUI gibt die Wallbox einmalig auf ihre Grundeinstellung frei.</p>
                    <p><strong>PV-Kurve ruhig:</strong> lädt entlang der Speicher-Ladekurve mit Hysterese. Kurze Wolken und Lastwechsel werden geglättet, damit die Wallbox nicht taktet. Eine bereits laufende Ladung darf dafür kurzzeitig eine auf 75&nbsp;Wh begrenzte Batteriestützung nutzen; ein Kaltstart oder Phasenwechsel wird nicht aus dem Hausspeicher finanziert. Der Modus <strong>PV + Akku</strong> bleibt davon getrennt.</p>
                    <p><strong>Grundladung stabil:</strong> hält bewusst eine 6A-Grundladung, solange wbminSoC beziehungsweise das Speicherziel erreichbar bleibt. Das ist die Anti-Flatter-Variante für empfindliche Fahrzeuge und Wallboxen.</p>
                    <p><strong>PV + Akku bis Untergrenze:</strong> das Auto darf PV und Hausakku bis zur Hausakku-Reserve nutzen. Bis zu dieser Untergrenze lädt das Auto normal; Netz bleibt aus. Wenn die Wallbox mehr Leistung will, stützt der Akku darunter nur Hausverbrauch und Wärmepumpe.</p>
                    <p><strong>Sofort bis Preislimit:</strong> startet sofort mit PV und Speicher. Netzstrom wird nur genutzt, wenn der aktuelle Preis unter dem eingestellten Wallbox-Preislimit liegt. Damit wird kein Auto versehentlich zu extremen Preisen geladen.</p>
                </div>
            </div>
        </div>

        <!-- Mobile UI Schiebemenü -->
        <div class="col-12 faq-item" data-tags="ui dashboard archiv menü schiebemenü navigation mobile">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">UI</span>
                        Wo finde ich auf dem Smartphone das alte "Archiv" oder das vollständige Menü?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Das mobile Dashboard wurde modernisiert, um Platz zu sparen. Das überladene, horizontale Top-Menü wurde ab Version 4.0 durch ein platzsparendes <strong>seitliches Schiebemenü (Offcanvas)</strong> ersetzt.</p>
                    <p>Sie erreichen das Menü über das Hamburger-Icon <code><i class="fas fa-bars"></i></code> oben links in der Ecke. Das alte "Archiv" wurde in diesem Zuge vollständig entfernt, da Langzeit-Auswertungen nun über "Verlauf" und "Langzeit-Statistiken" gebündelt abrufbar sind.</p>
                    <p>Ebenso zeigt das Energiefluss-Diagramm nun bei statischen Stromtarifen anstelle pauschaler `0.0 ct` automatisch den qualitativen <strong>Eco-Score</strong> (0-100) an, sodass Sie sehen wie gut das System aktuell lädt.</p>
                </div>
            </div>
        </div>

        <!-- Stealth Proxy / Hausverbrauch -->
        <div class="col-12 faq-item" data-tags="wallbox hausverbrauch entladen batterie 127.0.0.1 proxy stealth openwb">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Hausverbrauch zu hoch beim Laden (Batterie entlädt)? Die 127.0.0.1 Lösung.
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn eine externe Wallbox (z.B. go-e, openWB oder evcc) lädt, darf diese Leistung nicht doppelt als Hausverbrauch gezählt werden. In V4 macht das der Python-/PHP-Livepfad direkt über echte Wallbox-Messwerte.</p>
                    <p>Empfohlen ist heute ein direktes Wallbox-Leistungstopic wie <code>evcc/loadpoints/1/chargePower</code> im Bereich <strong>Wallbox-Leistung per MQTT</strong>. Die alte 127.0.0.1-Loopback-Konfiguration bleibt nur als Legacy-Fallback erhalten.</p>
                    <p><strong>Legacy-Konfiguration:</strong></p>
                    <ul>
                        <li><code>openwb = true</code></li>
                        <li><code>openwb_ip = 127.0.0.1</code></li>
                    </ul>
                    <p>Das Dashboard erkennt frische Wallbox-Messwerte automatisch und korrigiert Hausverbrauch, Energiefluss, Historie und Planung.</p>
                </div>
            </div>
        </div>

        <!-- Docker Background Service Info -->
        <div class="col-12 faq-item" data-tags="docker container restart aktivieren wallbox python manager">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag bg-secondary">Docker</span>
                        Warum startet die Wallbox im Docker nicht, obwohl Native Regelung angeschaltet wurde?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn Sie E3DC-Control als <strong>Docker-Container</strong> betreiben, werden Hintergrunddienste wie der Native Wallbox Manager oder Smart-Home-Hubs aus Container-Architektur-Gründen immer <strong>nur einmalig beim Booten</strong> des Containers durch die <code>entrypoint.sh</code> gestartet.</p>
                    <p>Wenn Sie das Feature also gerade ganz frisch im Konfigurations-Editor nachträglich <strong>eingeschaltet</strong> und gespeichert haben, läuft der verantwortliche Python-Hintergrundprozess aktuell schlichtweg noch nicht!</p>
                    <div class="alert alert-warning mb-0 mt-3 border-0">
                        <strong>🔌 Lösung:</strong> Erzeuge den gesamten Docker-Container neu und warte auf den imagegebundenen Healthcheck. Beim Hochfahren liest der Container Deine neue Konfiguration und startet das Ladeprogramm dauerhaft mit.
                        <pre>cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
if [ -f ./docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./docker_compose_update.py
elif [ -f ./Installer/docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py
else
  echo "docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen." >&2
  exit 2
fi
sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo --recreate-current</pre>
                    </div>
                </div>
            </div>
        </div>

        <!-- E3DC native Wallbox: physischer Abbruch bei Ladefenster -->
        <div class="col-12 faq-item" data-tags="wallbox e3dc native ladefenster abbruch physisch abgebrochen mode sonnenmodus netz boerse scheduler nacht">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        E3DC native Wallbox: Ladefenster starten, aber die Wallbox bricht sofort ab ("physisch abgebrochen")
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Im Log erscheint kurz nach dem Ladefenster-Start:</p>
                    <pre>WB1 START: 0A -> 9A (delta=0.0% fz=1.00 budget=0W)
WB1 hat Ladevorgang physisch abgebrochen (Versuch 1/3)!</pre>
                    <p><strong>Ursache (v&lt; 4.5.5):</strong> Die E3DC native Wallbox wurde beim Scheduler-/B&ouml;rsenladen immer in <strong>Mode=1 (Sonnenmodus / PV-Only)</strong> gestartet. In diesem Modus erlaubt die E3DC-Firmware keinen Netzbezug &mdash; nachts oder bei <code>budget=0W</code> gibt es keine PV-Quelle, und E3DC bricht sofort physisch ab.</p>
                    <p><strong>Warum ist <code>budget=0W</code> bei einem Ladefenster normal?</strong><br>
                    Das Budget-Signal (<code>wb_pv_budget.json</code>) repr&auml;sentiert den aktuellen PV-&Uuml;berschuss. Bei Nacht-/B&ouml;rsenladen ist es gewollt, dass kein PV-Surplus vorhanden ist &mdash; der Strom soll ja g&uuml;nstig aus dem Netz kommen!</p>
                    <p><strong>Fix (ab v4.5.5):</strong> Bei aktiven Ladefenstern (<code>price_optimizing_active</code>) oder erlaubtem Netzbezug (<code>effective_allow_grid</code>) wechselt der Wallbox Manager jetzt automatisch in <strong>Mode=2 (Netzmodus)</strong>, der Laden aus PV&nbsp;+&nbsp;Netz&nbsp;+&nbsp;Batterie erlaubt. Das <code>take_control()</code> sorgt zus&auml;tzlich daf&uuml;r, dass der Heartbeat-Thread nicht auf PV-Only zur&uuml;ckf&auml;llt.</p>
                    <div class="alert alert-warning mb-0 mt-3 border-0">
                        <strong>Betroffene Hardware:</strong> Ausschlie&szlig;lich E3DC native Wallboxen (<code>wb_type=e3dc</code>). openWB und go-e sind nicht betroffen &mdash; diese steuern &uuml;ber HTTP und kennen keine Mode=1/Mode=2 Unterscheidung.
                    </div>
                </div>
            </div>
        </div>

        <!-- Batterie-Kapazit&auml;t: RSCP Brutto vs. reale Nutzkapazit&auml;t -->
        <div class="col-12 faq-item" data-tags="batterie kapazitaet speicher rscp brutto nutzbar vitals speichergroesse kwh abweichung">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Batterie</span>
                        Im Konfigurations-Editor steht "32.7 kWh (Brutto)" aber Vitals zeigt nur 17.7 kWh &mdash; was stimmt?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Der Konfigurations-Editor liest aus den Live-Daten getrennte Schrankwerte und daraus gebildete Systemwerte:</p>
                    <ul>
                        <li><strong><code>bat_total_full_cap_kwh</code> (Brutto/System):</strong> Die aufsummierte Nennkapazit&auml;t aller erkannten Batterieschr&auml;nke laut E3DC/BMS.</li>
                        <li><strong><code>bat_total_usable_kwh</code> (Nutzbar/System):</strong> Die aufsummierte nutzbare Kapazit&auml;t aller erkannten Batterieschr&auml;nke. <strong>Er ist der relevante RSCP-Live-Wert f&uuml;r Plausibilit&auml;t und Fallbacks.</strong></li>
                        <li><strong><code>bat_usable_kwh</code>, <code>bat1_usable_kwh</code> ...:</strong> Einzelne Schrankwerte. Bei Speichererweiterungen darf <code>bat_usable_kwh</code> nicht als Gesamtsystem gelesen werden.</li>
                    </ul>
                    <p>Ab <strong>v5.1.x</strong> zeigt der Konfigurations-Editor bevorzugt <code>bat_total_usable_kwh</code> als prim&auml;ren Wert ("nutzbar") an. Vitals nutzt ebenfalls die Schrank-/Pack-Summe und bleibt die beste Detailansicht.</p>
                    <p><strong>Was sollte ich als <code>speichergroesse</code> konfigurieren?</strong><br>
                    Den Wert, den Vitals als <em>"Im Neuzustand nutzbar"</em> ausweist. Dieser Wert ist die tats&auml;chliche Planungsgrundlage f&uuml;r den Storage Simulator und die Ladekurven.</p>
                </div>
            </div>
        </div>

        <!-- Heizstab/optionale Dienste im Docker aktivieren -->
        <div class="col-12 faq-item" data-tags="docker heizstab shelly wp waermepumpe dienst aktivieren wp_type entrypoint">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag bg-secondary">Docker</span>
                        Wie aktiviere ich den Heizstab-Dienst (oder andere optionale Dienste) im Docker-Container?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Optionale Dienste wie <strong>Heizstab</strong>, <strong>Shelly Pro3EM</strong>, <strong>IDM/Luxtronik/Stiebel/Dimplex W&auml;rmepumpe</strong> oder <strong>Wallbox Manager</strong> werden im Docker nur einmalig beim Container-Start durch <code>entrypoint.sh</code> gestartet. Die Aktivierung erfolgt in zwei Schritten:</p>
                    <p><strong>Klimaverbrauchsmessung:</strong> Ein bereits gestarteter read-only Worker &uuml;bernimmt Deaktivierung und Shelly-Kanalwechsel im n&auml;chsten Zyklus. Wird die Klimamessung aus <code>Aus</code> erstmals aktiviert, ist auch daf&uuml;r ein Container-Neustart erforderlich. Eine aktive Klimasteuerung ist davon nicht umfasst.</p>
                    <ol>
                        <li><strong>Konfigurations-Editor:</strong> Im passenden Bereich die sichtbaren Schalter und Auswahlfelder setzen und speichern:
                            <ul>
                                <li><strong>Smart Home &amp; Verbrauchsprognose:</strong> <strong>WP-/Verbrauchslogging aktivieren</strong> einschalten und bei <strong>Wärmepumpen Typ</strong> Luxtronik, IDM, Stiebel Eltron ISG / WPM oder Dimplex WPM Touch / NWPM wählen.</li>
                                <li><strong>Stiebel:</strong> zusätzlich <strong>ISG IP-Adresse</strong> eintragen; <strong>Hz aus Web</strong> nur aktivieren, wenn die ISG-Prozessdaten-Seite aus dem Container erreichbar ist.</li>
                                <li><strong>Dimplex:</strong> zusätzlich die <strong>Dimplex IP-Adresse</strong> des NWPM-Moduls eintragen; Port 502 und Smart-Grid-Register 5167 bleiben im Regelfall unverändert. <strong>Dunkelgrün</strong> bleibt standardmäßig aus, weil Dimplex damit auch elektrische Wärmeerzeuger anfordern kann.</li>
                                <li><strong>Heizstab / Shelly:</strong> die sichtbaren Shelly-/Heizstab-Felder im Frontend ausfüllen, nicht die internen Roh-Keys suchen.</li>
                            </ul>
                        </li>
                        <li><strong>Container neu erzeugen</strong> (liest beim n&auml;chsten Boot die neue Konfiguration und wartet auf den Healthcheck):
                            <pre>cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
if [ -f ./docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./docker_compose_update.py
elif [ -f ./Installer/docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py
else
  echo "docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen." >&2
  exit 2
fi
sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo --recreate-current</pre>
                        </li>
                    </ol>
                    <p>Die internen Config-Keys sind nur noch f&uuml;r Diagnose und Support interessant. Im normalen Betrieb reicht die Frontend-Auswahl plus anschlie&szlig;ender Container-Neustart.</p>
                    <p>Nach Updates übernimmt der Host-Helfer die aufgelöste Image-Auswahl, den expliziten Pull sowie den gebundenen Start- und Rückfallvertrag:</p>
                    <pre>cd "${E3DC_DOCKER_PATH:-$HOME/e3dc-docker}"
if [ -f ./docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./docker_compose_update.py
elif [ -f ./Installer/docker_compose_update.py ]; then
  E3DC_DOCKER_HELPER=./Installer/docker_compose_update.py
else
  echo "docker_compose_update.py fehlt; aktuellen Release-Verwaltungsbaum bereitstellen." >&2
  exit 2
fi
sudo python3 "$E3DC_DOCKER_HELPER" --compose-dir . --sudo
sudo docker compose logs --tail=80 e3dc-control</pre>
                    <p>Das gilt auch f&uuml;r die &uuml;brigen im Container-Startskript angebundenen optionalen Dienste, etwa Wallbox Manager und Bluelink.</p>
                </div>
            </div>
        </div>

        <!-- EVCC / MQTT Integration -->
        <div class="col-12 faq-item" data-tags="evcc wallbox mqtt ladevorgang power leistung">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Ladevorgang von evcc wird im Dashboard nicht angezeigt?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn die Ladeleistung von evcc nicht erscheint, prüfen Sie folgende Punkte:</p>
                    <ul>
                        <li><strong>Zweite Wallbox aktiv?</strong> Wenn Sie nur eine evcc-Wallbox haben, darf in der Konfiguration <code>wb2_ip</code> und <code>wb2_topic</code> <strong>nicht</strong> befüllt sein, sonst versucht das System evcc als Zweit-Wallbox zu behandeln.</li>
                        <li><strong>MQTT Topics:</strong> evcc sendet die Ladeleistung standardmäßig auf <code>evcc/loadpoints/1/chargePower</code>. Dieses Topic gehoert im Config Editor unter <strong>Schnittstellen & MQTT</strong> in <strong>Wallbox-Leistung per MQTT</strong> -> <code>wb_topic</code>, nicht in das Fahrzeug-SoC-Feld.</li>
                        <li><strong>SoC getrennt lassen:</strong> <code>evcc/loadpoints/1/vehicleSoc</code> bleibt bei <code>mqtt_hub_sub_soc_topic</code>. Ladeleistung und Fahrzeug-SoC sind zwei getrennte MQTT-Abos.</li>
                        <li><strong>Zugangsdaten:</strong> Falls Ihr Mosquitto-Broker passwortgeschützt ist, müssen die Daten für den direkten Wallbox-Leistungsbroker bei <code>wb_user</code> & <code>wb_pass</code> eingetragen werden.</li>
                        <li><strong>localhost vs. IP:</strong> Wenn Mosquitto als Add-on in Home Assistant läuft, funktioniert <code>localhost</code> im Terminal meist nicht. Nutzen Sie die echte IP Ihres HA-Systems (z.B. <code>192.0.2.150</code>).</li>
                    </ul>
                    <pre>mosquitto_sub -h 192.0.2.150 -v -t "evcc/#" -u USER -P PASSWORD</pre>
                </div>
            </div>
        </div>

        <!-- e3dc.wallbox.out / txt -->
        <div class="col-12 faq-item" data-tags="wallbox e3dcwallboxtxt txt out datei ladefenster zeitleiste zeiten">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Fehlende Ladefenster / Funktion der e3dc.wallbox.txt?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Die Datei <code>e3dc.wallbox.txt</code> ist für die V4-Steuerung über das Web-UI nicht mehr relevant. Sie bleibt nur als Legacy-/Migrationsdatei erhalten. Neue Ladeplanung und Wallbox-Modi liegen in <code>data/e3dc_v4.json</code> und in den Ramdisk-Planungsdateien.</p>
                    <p><strong>Wie funktioniert es stattdessen?</strong><br>
                    Nach dem Speichern erzeugt der private Planer die Datei <code>native_wallbox_schedule_wb1.json</code> beziehungsweise <code>native_wallbox_schedule_wb2.json</code>. Die ausgewählten 15-Minuten-Abschnitte erscheinen auf der Seite <strong>Wallbox</strong> im Ladeplan und in der Zeitleiste. Bei <strong>Auto</strong> wählt der Planer innerhalb des vorgegebenen Zeitraums die günstigsten Abschnitte; die sichtbaren gelben Planblöcke sind die tatsächlich ausgewählten Ladezeiten.</p>
                    <p><strong>Warum kann ein morgiges Fenster fehlen?</strong><br>
                    Feste Tarife, Octopus Heat und Spezialtarife werden aus ihrem täglich wiederkehrenden Tarifprofil geplant und benötigen dafür keine morgigen EPEX-Slots. Dynamische Tarife wie Tibber oder aWATTar bleiben dagegen gesperrt, bis die zukünftigen Preise veröffentlicht wurden. Eine fehlgeschlagene Kandidatenplanung verändert weder Konfiguration noch bestehenden Ladeplan.</p>
                </div>
            </div>
        </div>

        <!-- Zwei Wallboxen -->
        <div class="col-12 faq-item" data-tags="wallbox wb2 ip adresse ausblenden">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">UI</span>
                        Warum werden mir zwei Wallboxen im Energiefluss angezeigt?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Das Dashboard blendet die zweite Wallbox automatisch ein, sobald in der Konfiguration entweder eine <strong>IP-Adresse</strong> bei <code>wb2_ip</code> oder ein <strong>Topic</strong> bei <code>wb2_topic</code> hinterlegt ist.
                    <br><br>
                    <strong>Lösung:</strong> Leeren Sie diese beiden Felder im Konfigurations-Editor, wenn Sie nur eine Ladestation besitzen.
                </div>
            </div>
        </div>

        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-tools me-2"></i>Troubleshooting & Fehleranalyse</h4>

        <!-- Alle Dienste NICHT INSTALLIERT trotz Installer-Meldung "läuft" -->
        <div class="col-12 faq-item" data-tags="nicht installiert dienste services dashboard leer keine daten pi5 erstinstall journalctl failed crashed ramdisk">
            <div class="faq-card" style="border-left: 3px solid #dc3545;">
                <div class="faq-question">
                    <div>
                        <span class="tag" style="background:rgba(220,53,69,0.15);color:#dc3545;">Häufig</span>
                        Dashboard zeigt alle Dienste als "Nicht installiert" – obwohl der Installer "aktiv" meldet?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Das ist das häufigste Problem bei einer Erstinstallation. Das Bild zeigt alle Dienste als <strong>"NICHT INSTALLIERT"</strong>, obwohl der Installer sie als aktiv gemeldet hat.</p>
                    <p>Die Ursache: Die Dienste <strong>starten, crashen sofort, und systemd markiert sie als <code>failed</code></strong>. Die Web-UI kann diesen Zustand nicht von "nie installiert" unterscheiden und zeigt deshalb "NICHT INSTALLIERT" – ohne Fehlermeldung.</p>

                    <p><strong>Schritt 1: Diagnose per SSH</strong></p>
                    <pre>
# Welcher Dienst crasht und warum?
sudo systemctl status e3dc-live.service --no-pager

# Die letzten Fehlermeldungen im Detail
journalctl -u e3dc-live.service -n 30 --no-pager

# Ist die Ramdisk gemountet? (ohne die läuft gar nichts)
ls /var/www/html/ramdisk/ &amp;&amp; echo "OK" || echo "RAMDISK FEHLT!"</pre>

                    <p><strong>Häufigste Ursachen und ihre Fehlermeldungen:</strong></p>
                    <table class="table table-sm table-bordered mt-2" style="font-size:0.85rem;">
                        <thead><tr><th>Ursache</th><th>Symptom in <code>journalctl</code></th></tr></thead>
                        <tbody>
                            <tr><td>E3DC-IP / Passwort noch nicht konfiguriert</td><td><code>Connection refused</code> oder <code>Invalid credentials</code></td></tr>
                            <tr><td>Ramdisk nicht gemountet</td><td><code>FileNotFoundError: /var/www/html/ramdisk/live_data_py.json</code></td></tr>
                            <tr><td>Python-Venv kaputt oder falscher Pfad</td><td><code>No module named 'rscp_client'</code></td></tr>
                            <tr><td>e3dc_v4.json fehlt oder ist leer</td><td><code>JSONDecodeError</code> oder <code>KeyError: 'server_ip'</code></td></tr>
                        </tbody>
                    </table>

                    <p><strong>Schritt 2: Konfiguration prüfen</strong></p>
                    <pre>
# Ist die E3DC-IP konfiguriert?
cat /var/www/html/data/e3dc_v4.json | python3 -c \
  "import sys,json; d=json.load(sys.stdin); print('IP:', d.get('server_ip','FEHLT!'))"</pre>

                    <div class="alert alert-warning mt-3 mb-0 border-0">
                        <strong>💡 Datenschutz:</strong> Erstellen Sie für Supportfälle das redigierte Diagnosepaket in der Installationszentrale. Prüfen Sie es vor jeder Weitergabe. Rohe Journal-, Konfigurations- oder Prozessausgaben können Host-, Pfad-, Nutzer- und Betriebsdaten enthalten und gehören nicht in öffentliche Beiträge.
                    </div>
                </div>
            </div>
        </div>

        <!-- Venv User Path Crash -->

        <div class="col-12 faq-item" data-tags="absturz dienst startet nicht venv pi admin failed to locate executable python3">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">System</span>
                        Dienst-Fehler: "Failed to locate executable &lt;HOME&gt;/.venv_e3dc/bin/python3"
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn E3DC-Control unter einem anderen Linux-Benutzer als <code>pi</code> installiert wurde oder die Python-Umgebung fehlt, können Dienste mit einem venv-Pfadfehler aussteigen.</p>
                    <p><strong>Lösung:</strong> Im Installer zuerst <em>Systempakete vorbereiten</em> ausführen. Alternativ im Expertenmenü <em>Python-Umgebung neu aufbauen</em> wählen. Der Installer legt die Umgebung passend zum Installationsbenutzer an und schreibt die Dienstpfade neu.</p>
                </div>
            </div>
        </div>

        <!-- Storage Simulator Empty Value Crash -->
        <div class="col-12 faq-item" data-tags="simulator crash absturz valueerror float leer string">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">System</span>
                        Storage Simulator stürzt ab: "ValueError: could not convert string to float"
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Dieser Fehler trat auf, wenn in der Web-Konfiguration wichtige Felder wie "Speicherziel (SoC)" oder "Einspeiselimit" komplett leer gelassen wurden (statt z.B. 0.0 einzutragen). Der Simulator versuchte, das leere Feld zu berechnen und stürzte ab.</p>
                    <p><strong>Lösung:</strong> Installieren Sie das Update <strong>4.0.6</strong>. Der Simulator fängt nun leere Config-Felder sicher ab und nutzt stattdessen intelligente Fallback-Standardwerte, wodurch das System selbst bei fehlerhafter Benutzereingabe dauerhaft stabil bleibt.</p>
                </div>
            </div>
        </div>

        <!-- Warte auf E3DC / IP Fehler -->
        <div class="col-12 faq-item" data-tags="warte e3dc gelbes schild keine werte no route to host ip adresse zahlendreher">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Verbindung</span>
                        Das Dashboard bleibt gelb ("Warte auf E3DC...") und Werte aktualisieren nicht.
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn das Dashboard minutenlang das gelbe Hinweisschild zeigt, kann der Python-Dienst <code>e3dc-live.service</code> sich nicht per RSCP am Hauskraftwerk anmelden. Häufigste Ursache ist eine falsche IP-Adresse oder ein Tippfehler. Dokumentationsbeispiel: <code>192.0.2.41</code> statt <code>192.0.2.36</code>; tragen Sie ausschließlich die Adresse Ihres eigenen Systems ein.</p>
                    <p><strong>Fehler-Analyse:</strong> Öffnen Sie das Terminal (WinSCP / SSH) und prüfen Sie den Dienst:</p>
                    <pre>journalctl -u e3dc-live -n 20 --no-pager</pre>
                    <p>Sehen Sie dort einen <code>[Errno 113] No route to host</code> oder <code>Connection refused</code>, prüfen Sie im Dashboard unter <em>Konfiguration -> E3DC Auth</em> sofort Ihre eingetragene "E3DC IP-Adresse".</p>
                </div>
            </div>
        </div>

        <!-- PiGuard Bootloop / Screen Freeze -->
        <div class="col-12 faq-item" data-tags="bootloop piguard watchdog freeze systemd neustart docker absturz legacy">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Watchdog</span>
                        Watchdog schickt ständige Reboot-Alarme oder Docker-Spam?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Der System-Watchdog (PiGuard) reagiert sensibel auf klemmende Dienste. Prüfen Sie zuerst, ob die V4-Dienste aktiv sind und ob alte Legacy-Prozesse den RSCP-Port 5033 blockieren.</p>
                    <pre>systemctl is-active e3dc-live e3dc-storage-manager apache2
journalctl -u e3dc-live -n 80 --no-pager</pre>
                    <p>Falls ein alter Legacy-Dienst noch existiert, kann er deaktiviert werden: <code>sudo systemctl disable --now e3dc.service</code>. Danach <code>sudo systemctl restart e3dc-live piguard</code> ausfuehren.</p>
                </div>
            </div>
        </div>

        <!-- Live Service Crash (ValueError) -->
        <div class="col-12 faq-item" data-tags="value error int crash port leer abruch">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">System</span>
                        Der e3dc-live Dienst stürzt ab mit "ValueError: invalid literal for int()".
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Dieser Fehler entstand vor Version 4.0.2, wenn im Konfigurations-Editor das Feld <strong>E3DC Port</strong> versehentlich komplett leer gelassen wurde. Beim Speichern legte das System einen leeren String <code>""</code> an. Beim Konvertieren dieses Strings in eine reine Zahl stürzte das Live-Skript ab.</p>
                    <p><strong>Lösung:</strong> Installieren Sie das Hotfix-Update v4.0.2 oder tragen Sie im Dashboard unter <em>E3DC System & Auth</em> händisch den Standard-Port <strong>5033</strong> ein.</p>
                </div>
            </div>
        </div>

        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-server me-2"></i>System & Sicherheit</h4>

        <div class="col-12 faq-item" data-tags="konfiguration download sicherheit raw redacted web pin zugangsdaten">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Sicherheit</span>
                        Wie lade ich die Konfiguration sicher herunter?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Der <strong>Redacted Download</strong> maskiert Passwörter, Tokens und andere Geheimnisse und ist für Diagnose und Support vorgesehen.</p>
                    <p><strong>Raw-Download enthält Zugangsdaten und wird nur bei gesetzter Web-PIN angeboten.</strong> Prüfen Sie diese Datei vor jeder Weitergabe und behandeln Sie sie wie ein Passwort.</p>
                </div>
            </div>
        </div>

        <!-- Backup -->
        <div class="col-12 faq-item" data-tags="backup datensicherung wiederherstellung s3 ftp dropbox">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">System</span>
                        Wie erstelle ich ein Backup meiner Daten und Einstellungen?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Das System legt Update- und Wiederherstellungsbackups in einem konfigurierten Sicherungsverzeichnis außerhalb des Installationsbaums ab. Jedes vollständige Backup besitzt ein Manifest und SHA-256-Prüfsummen; leere oder unlesbare Sicherungen gelten als Fehler.
                    <p>Für ein zusätzliches externes Ziel wählen Sie im Installer <em>Cloud-/Rclone-Backup</em>. Konfiguration, Statistikdatenbank, Matter-Kopplungsdaten und persistente Betriebszustände werden nur dann als gesichert gemeldet, wenn die Pflichtdateien lesbar geprüft wurden.</p>
                </div>
            </div>
        </div>

        <!-- High Availability -->
        <div class="col-12 faq-item" data-tags="ha standby master slave cluster hochverfügbarkeit">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">HA</span>
                        Was bedeutet die Anzeige "Standby" (Slave) im Cluster?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Im High-Availability-Modus (HA) werden zwei Systeme parallel betrieben. Einer ist der <strong>Master</strong> (aktiv, steuert das Hauskraftwerk), der andere ist der <strong>Slave</strong> (Standby, horcht auf den Master).
                    <p>Bei erkanntem Heartbeat-Verlust kann der Standby-Knoten nach erfolgreicher Kontext- und Rollenprüfung übernehmen. Die tatsächliche Umschaltzeit hängt von Heartbeat, Dienstzustand und Installation ab; vor der Anlagenfreigabe muss der eindeutige Writer bestätigt sein.</p>
                </div>
            </div>
        </div>

        <!-- Update -->
        <div class="col-12 faq-item" data-tags="update version aktualisierung git pull">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Update</span>
                        Wie führe ich ein systemweites Update durch?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Ein Update wird über den sichernden Installer durchgeführt:
                    <ol>
                        <li>Über das Dashboard (Kachel Konfiguration -> "Update suchen").</li>
                        <li>Bei aktuellen Ständen direkt über den Menüpunkt <em>Update</em> im Installer.</li>
                    </ol>
                    <p>Neuere Releases übergeben den Wechsel noch vor Backup und Dienststopp an einen bytegenau versiegelten Updater des Ziel-Releases. Erst dieser interpretiert seine eigenen Dienst-, Paket- und Wiederanlaufverträge und besitzt Backup sowie Rückweg. Beim ersten Sprung von einer älteren Version ohne diesen Vertrag gelten technisch noch deren bisherige Zeitgrenzen; ab dem anschließend installierten Stand laufen weitere Updates vollständig über den Ziel-Updater.</p>
                    <p>Der Dashboard-Button startet ausschließlich das reguläre Update über einen argumentlosen root-eigenen Systemjob. Der Launcher bindet den unveränderten veröffentlichten Ausgangsstand an dessen Remote-Tag und führt den Installer aus einem versiegelten Snapshot aus. Freie Pfade, Release-Tags, Rückfälle, Rechte-Reparaturen und Neuinstallationen sind über diesen Webweg nicht zulässig.</p>
                    <p>Ist der exakt veröffentlichte Stand bereits vollständig installiert, endet ein normales Update ohne Backup und ohne Dienstunterbrechung. Dabei werden keine Produkt- oder Webdateien und kein Dienstzustand verändert; die für die Zielprüfung erforderlichen Git-Metadaten dürfen aktualisiert werden. Eine bewusst gewünschte Reparatur oder Neuinstallation derselben Version bleibt eine administrative Konsolenaktion mit <code>bash "$E3DC_INSTALL_PATH/e3dc-setup" --reinstall-current</code>.</p>
                    <p>Der Release-Finalizer zeigt seine Phasen und alle 30 Sekunden ein Lebenszeichen. Erst nach 30 Minuten Finalizerlauf wird hart abgebrochen; danach versucht der Installer die verifizierte Wiederherstellung des Ausgangszustands. Nur bei vollständigem Dienst-, Rollen- und Gesundheitsnachweis werden die Writer wieder freigegeben, andernfalls bleiben sie fail-closed gestoppt. Backup und Wiederherstellung selbst liegen außerhalb dieses Zeitlimits.</p>
                    <p><strong>Einmalige Ausnahme für 5.3.2b:</strong> Den ersten Wechsel auf das aktuelle Stable-Release ausschließlich über die folgende vollständige administrative SSH-Kette starten. Der Web-Launcher ist in diesem Altstand noch nicht vorhanden. Der interaktive Menüpunkt ist für diesen Hybridwechsel nicht freigegeben, weil der 5.3.2b-Altprozess bereits zusätzliche Module geladen hat.</p>
                    <pre>export E3DC_INSTALL_PATH="$HOME/Install"
test -f "$E3DC_INSTALL_PATH/installer_main.py"
test -x "$HOME/.venv_e3dc/bin/python3"
cd "$E3DC_INSTALL_PATH"
sudo /usr/bin/python3 installer_main.py --fix-permissions
sudo /usr/bin/python3 -I -B -u installer_main.py --check
sudo /usr/bin/python3 -I -B -u installer_main.py --update-e3dc
cat VERSION
systemctl --failed --no-pager</pre>
                    <p>Liegt E3DC-Control nicht unter <code>$HOME/Install</code>, wird nur die erste Zeile an den tatsächlichen absoluten Installationspfad angepasst. Schlägt eine der beiden <code>test</code>-Zeilen fehl, dort stoppen.</p>
                    <p>Verwenden Sie für den einmaligen Wechsel auf die bereinigte Historie keinen manuellen <code>git pull --ff-only</code>-Ablauf. Der Installer erstellt und prüft zuerst das externe Backup und validiert anschließend Zielstand, Dienste und Weboberfläche.</p>
                    Updates werden im Changelog oben rechts im Dashboard signalisiert.
                </div>
            </div>
        </div>

        <!-- E3DC Classic vs V4 -->
        <div class="col-12 faq-item" data-tags="c++ classic v4 python version kern unterschied">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">System</span>
                        Was ist der Unterschied zwischen "E3DC Classic" und "V4"?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p><strong>E3DC-Control V4</strong> (dieses System) verwendet native Python-Dienste für Live-Daten, Speicherplanung, Wallbox, Wärmeintegration und Prognosen. Die Installationszentrale zeigt, welche Dienste auf dem eigenen System aktiviert sind.</p>
                    <p>Für Nutzer, die diesen modernen Funktionsumfang nicht benötigen und <strong>ausschließlich</strong> das bewährte ursprüngliche C++-Steuerungsprogramm von Eba-M (Sonnenmodus-Steuerung) laufen lassen wollen, wurde das Projekt <strong>E3DC-Classic</strong> ausgegliedert. Beide Varianten werden weiterhin supportet.</p>
                </div>
            </div>
        </div>

        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-microchip me-2"></i>Automation & Smart Home</h4>

        <!-- MQTT Hub -->
        <div class="col-12 faq-item" data-tags="mqtt hub smarthome iobroker homeassistant">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">SmartHome</span>
                        Wie binde ich die Daten in Home Assistant oder ioBroker ein?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Der <strong>Smart Home MQTT-Hub</strong> sendet alle Live-Daten (PV, Batterie, Haus, Grid) an Ihren Broker.
                    <p>Standard-Präfix ist <code>e3dc/</code>. In Home Assistant können Sie diese via MQTT-Integration abonnieren. Beispiel-Topic für den Hausverbrauch: <code>e3dc/live/home_w</code>. Das Dashboard nutzt den WebSocket ausschließlich über den gleichursprünglichen Webserver-Pfad <code>/ws</code>; ein direkter LAN-Port ist nicht erforderlich.</p>
                </div>
            </div>
        </div>

        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-car me-2"></i>Fahrzeuge</h4>

        <!-- Interpolation SoC -->
        <div class="col-12 faq-item" data-tags="soc auto fahrzeug bluelink interpolation hyundai kia">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Fahrzeug</span>
                        Warum weicht der SoC im Dashboard vom Auto-Display ab?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Auto-APIs (wie Bluelink oder Kia Connect) haben oft Limits für Cloud-Anfragen. Damit der Ladestand flüssig wandert, berechnen wir die <strong>Interpolation</strong> basierend auf der geladenen Energie (kWh) und der Akkukapazität.
                    <br><br>
                    Prüfen Sie den Wert <code>car_capacity</code> in Ihrer Konfiguration. Ist dieser zu klein, steigt der berechnete SoC schneller als der echte Wert. Beim nächsten Cloud-Sync wird der Wert dann unschön "zurückgesetzt".
                </div>
            </div>
        </div>

        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-calculator me-2"></i>Bilanzen & Kosten</h4>

        <!-- Kosten & Ersparnis -->
        <div class="col-12 faq-item" data-tags="kosten ersparnis preis geld euro statistik bilanz">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Finanzen</span>
                        Wie werden Kosten und Ersparnisse in der Statistik berechnet?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Das System nutzt die <strong>echten dynamischen Strompreise</strong> (z.B. von Tibber oder Awattar), falls konfiguriert.
                    <p>Die <strong>Kosten</strong> ergeben sich aus dem Netzbezug multipliziert mit dem Preis zum jeweiligen Zeitpunkt. Die <strong>Ersparnis</strong> berechnet sich aus dem Eigenverbrauch (PV + Batterie) multipliziert mit dem Netzpreis, den man in diesem Moment gespart hat.</p>
                </div>
            </div>
        </div>

        <!-- Wallbox History Repair -->
        <div class="col-12 faq-item" data-tags="wallbox history historie repair reparatur csv db doppelt hausverbrauch korrektur double accounting">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Bilanzen</span>
                        Wie korrigiere ich historische Wallbox-Ladedaten und den Hausverbrauch?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn in der Vergangenheit Ladedaten der Wallbox nicht korrekt in der Langzeit-Statistik erfasst und fälschlicherweise dem Hausverbrauch zugeschlagen wurden ("Double-Accounting"), gibt es ab Version v3.9.0 das <strong>Wallbox History Repair Tool</strong>.</p>
                    <p>Öffnen Sie <code>repair_wb_history.php</code> direkt im Browser, melden Sie sich bei Bedarf mit der Web-PIN an und bestätigen Sie die Reparatur im Formular. Erst danach liest das Tool die hochdetaillierten Original-Sitzungen aus dem Schatten-Log (<code>wb_sessions.csv</code>) aus, summiert sie kalendergenau und überschreibt die SQLite-Datenbank (<code>e3dc_stats.db</code>) rückwirkend. Der Hausverbrauch wird mathematisch bereinigt, womit die Gesamtbilanz (auch für vergangene Monate) wieder exakt stimmt.</p>
                </div>
            </div>
        </div>

        <!-- Netzbezug und Hausverbrauch reparieren -->
        <div class="col-12 faq-item" data-tags="netzbezug hausverbrauch grid_in grid_out vertauscht autarkie doppelt doublecounting reparatur datebank history">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Bilanzen</span>
                        Warum stimmt der Hausverbrauch in der Langzeithistorie nicht mehr (Netzbezug fehlt) oder Einspeisung/Bezug sind vertauscht?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn Anlagen einen PV-Überschuss aufweisen, wurde in der E3DC-History (bis v3.9.5) der Netzbezug fälschlicherweise als "Einspeisung" und die Einspeisung als "Bezug" gespeichert. Ebenso konnte es vorkommen, dass in Datenbanken (z.B. aus Altsystemen) der Netzbezug nicht korrekt zum Hausverbrauch addiert wurde, wodurch der errechnete Hausverbrauch geringer als der Netzbezug war.</p>
                    <p><strong>Lösung ab v3.9.6:</strong> Das Live-System archiviert die Werte nun immer plausibel geprüft. Um die Fehler der Vergangenheit in der SQLite-Datenbank auszubügeln, können Sie im Terminal folgenden Reparatur-Befehl ausführen (dieser korrigiert historische Fehler):</p>
                    <pre>python3 &lt;INSTALL_PATH&gt;/Installer/repair_home_grid_doublecounting.py --force</pre>
                </div>
            </div>
        </div>

        <!-- Invertierter Wurzelzähler -->
        <div class="col-12 faq-item" data-tags="zähler wurzelzähler invertiert vorzeichen e3dc netz strom positiv negativ">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">System</span>
                        Der "Netz"-Wert im Dashboard zeigt Einspeisung, obwohl ich Strom beziehe (Pfeile falsch)?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>In seltenen Fällen ist der E3DC-Wurzelzähler im Haus physikalisch falsch herum verdrahtet ("invertiert"), was dazu führt, dass E3DC positive statt negative Watt-Werte beim Bezug sendet.</p>
                    <p>Lösung: Aktivieren Sie im Konfigurations-Editor (Sektion: E3DC System & Auth) den Haken <strong>"Software Invertierung" (Wurzelzähler invertiert)</strong>. Dies dreht das Vorzeichen im UI dynamisch um.</p>
                </div>
            </div>
        </div>

        <!-- Externer Balken -->
        <div class="col-12 faq-item" data-tags="ext_pv externer generator wechselrichter bhkw pv grün balken langzeit">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Bilanzen</span>
                        Wieso taucht mein externes BHKW / 2. Wechselrichter in der Langzeithistorie separat als grüner Balken auf?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Bisher wurde Energie von externen Stromerzeugern (BHKW, Fremd-Wechselrichter), die über einen Leistungsmesser am E3DC-System angeschlossen sind, in der Statistik oft versteckt abgerechnet.</p>
                    <p>Ab v3.9.6 wird in der Langzeitauswertung und in den Live-Statistiken diese Energie nun sauber vom reinen Speichersystem-Ertrag (gelb) getrennt und als ehrlicher, <strong>separater grüner Balken (Ext_PV_Energy_kWh)</strong> dargestellt, der auch exakt mit in die Autarkie-Berechnung einfließt.</p>
                </div>
            </div>
        </div>

        <!-- IDM / Tages-AZ -->
        <div class="col-12 faq-item" data-tags="idm wärmepumpe jaz tages-az effizienz">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Heizung</span>
                        Was ist der Unterschied zwischen JAZ und Tages-AZ?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    Die JAZ (Jahresarbeitszahl) ist ein Durchschnittswert über ein ganzes Jahr. Bei IDM-Wärmepumpen zeigen wir im Dashboard die <strong>Tages-AZ</strong> an.
                    <p>Diese berechnet sich aus der am aktuellen Tag erzeugten Wärmemenge geteilt durch den elektrischen Verbrauch. Dies ermöglicht eine präzise Überwachung der Effizienz bei unterschiedlichen Außentemperaturen.</p>
                </div>
            </div>
        </div>

        <!-- Modbus Vorgaben WP -->
        <div class="col-12 faq-item" data-tags="idm waermepumpe pv grundlast register 74 takt taktschutz rampe heartbeat">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">IDM</span>
                        Wie funktioniert die iDM PV-Grundlast und der neue Taktschutz?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ab v4.8.9 kann der Energy Manager der iDM über Register 74 einen begrenzten PV-Überschuss in kW melden, z.B. maximal 2.0 kW. Rampe, Deadband, Heartbeat und Mindest-Schreibabstand sind im Config-Editor einstellbar.</p>
                    <p>Es werden dabei keine Temperatur-Sollwerte zyklisch geschrieben. Der PV-Boost besitzt zusätzlich Mindestlaufzeit und Wiedereinschaltsperre, damit die Wärmepumpe bei Wolkenwechseln nicht ständig taktet.</p>
                    <p>Der manuelle iDM-Scanner dient ausschließlich der Diagnose: Er liest Input-Register 1006 einmal per FC04. Ohne passend gebundenes Modell, Protokoll, Firmware und Unit-ID bleibt der Rohwert unbewertet; der Scanner schreibt keine Register.</p>
                </div>
            </div>
        </div>

        <div class="col-12 faq-item" data-tags="wärmepumpe modbus vorgabe sollwert heizung warmwasser kühlung idm luxtronik">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wärmepumpe</span>
                        Wo sehe ich, welche Vorgaben an die Wärmepumpe gesendet werden?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Das Wärmepumpen-Dashboard zeigt in Echtzeit ganz unten im Block <strong>"MODBUS SET-WERTE (VORGABEN)"</strong> exakt die Werte an, die durch den E3DC-Manager via Modbus auf die Anlage geschrieben werden:</p>
                    <ul>
                        <li><strong>Bei IDM-Anlagen:</strong> Drei getrennte Kacheln für Heizung, Warmwasser und Kühlung (0 = Auto-Betrieb der Anlage, 1 = PV-Überschuss-Anforderung aktiv).</li>
                        <li><strong>Bei Luxtronik-Anlagen:</strong> Den gesetzten Boost-Modus (z. B. "Setpoint") und die konkret vom Dashboard aufoktroyierte Soll-Temperatur für Heiz- und Warmwasserkreise.</li>
                    </ul>
                </div>
            </div>
        </div>

        <!-- Status-Check Log -->
        <div class="col-12 faq-item" data-tags="wärmepumpe log status-check boost ohne software-anforderung reset error idm luxtronik spam">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wärmepumpe</span>
                        Was bedeutet die Log-Meldung: "Status-Check: WP ist im Boost ... ohne Software-Anforderung"?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Wenn Sie (bei Luxtronik-Anlagen) eigene Zeitschaltuhren oder Komfort-Temperatur-Timer im Display der Wärmepumpe eingestellt haben, springt die Anlage physisch in den "Boost/Party-Modus". Da dieser Befehl nicht von E3DC-Control stammte, hat die Sicherheitslogik in der Vergangenheit ständig vor einem Modbus-Konflikt gewarnt ("spam").</p>
                    <p>Ab v3.9.6 erkennt E3DC-Control nun eigenständig, ob ein hoher Setpoint (>22°C) von einer WP-internen Zeitschaltung stammt und loggt diese Information <strong>nur noch lautlos</strong> im Hintergrund (DEBUG Level), anstatt das Log zu überfluten.</p>
                </div>
            </div>
        </div>

        <!-- Batterie & Vitals Sektion -->
        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-heartbeat me-2"></i>Batterie &amp; Vitals</h4>

        <!-- Verbindungsfehler RSCP -->
        <div class="col-12 faq-item" data-tags="vitals batterie rscp verbindung fehler soh zyklus aes">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Vitals</span>
                        Vitals-Seite zeigt "Verbindungsfehler zum RSCP" – was tun?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Das Batterie-Vitals Dashboard kommuniziert direkt mit der E3DC via dem RSCP-Protokoll (Port 5033). Mögliche Ursachen:</p>
                    <ul>
                        <li><strong>RSCP nicht aktiviert:</strong> Im E3DC-Portal unter <em>Mein E3DC &gt; RSCP</em> muss der Zugang aktiv sein und ein AES-Passwort gesetzt werden.</li>
                        <li><strong>Falsche Zugangsdaten:</strong> Prüfen Sie in der Config <code>e3dc_user</code>, <code>e3dc_password</code>, <code>server_ip</code> und <code>aes_password</code>.</li>
                        <li><strong>RSCPGui fehlt:</strong> Das Diagnose-Paket wurde möglicherweise nicht installiert. Führen Sie im Installer Schritt 1 (Systempakete) erneut aus oder klonen Sie es manuell:</li>
                    </ul>
                    <pre>git clone https://github.com/rxhan/RSCPGui.git ~/RSCPGui</pre>
                </div>
            </div>
        </div>

        <!-- E3DC History 0xFFFFFFFF -->
        <div class="col-12 faq-item" data-tags="0xffffffff 4294967295 rscp history historie datenbank fehler ffffffff uint64">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">System</span>
                        Historische Datenabfragen geben den Wert 0xFFFFFFFF (4294967295) zurück?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Dies ist ein bekannter E3DC-RSCP "Quirk" (Eigenheit) beim Abfragen der historischen Ertrags-Datenbank (<code>TAG_DB_REQ_HISTORY_DATA_DAY</code> etc.).</p>
                    <p>Wenn man für die Zeitanfragen den herkömmlichen 12-Byte <code>Timestamp</code> Typ statt einem 8-Byte UNIX-Timestamp (<code>Uint64</code>) übergibt, antwortet die E3DC Firmware nicht mit einem ordnungsgemäßen "Type Error". Stattdessen sendet sie stumm den maximalen 32-Bit Overflow Wert <strong>4294967295 (0xFFFFFFFF)</strong> als Payload zurück.</p>
                    <p>Ab E3DC-Control v3.9.0 senden die Python-Skripte automatisch die erzwungenen Unix-Timestamps, um korrekte und lückenlose Historien zu generieren.</p>
                </div>
            </div>
        </div>

        <!-- Keine Temperaturen / 0 mV Drift -->
        <div class="col-12 faq-item" data-tags="vitals batterie temperatur drift spannung 0 null firmware">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Vitals</span>
                        Temperaturen fehlen oder Zell-Drift zeigt 0 mV an?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Dies ist ein bekanntes Verhalten bei bestimmten E3DC-Firmwareversionen (z.B. P10_2025). Die Firmware verpackt Zelltemperaturen in einem zusätzlichen, verschachtelten <code>BAT_DATA</code>-Container, statt sie flach zurückzugeben.</p>
                    <p>Zusätzlich enthält diese Firmware-Version einen Bug, bei dem <strong>Zellspannungswerte (~3.85V)</strong> fälschlicherweise in die Temperaturliste eingemischt und leere Sensoren als <strong>0.0°C</strong> aufgefüllt werden. Das Dashboard filtert beides automatisch heraus.</p>
                    <p>Falls die Werte trotzdem nicht erscheinen: Stellen Sie sicher, dass die neueste Version von <code>vital_stats.py</code> auf dem System läuft (ab v3.8.8.6).</p>
                </div>
            </div>
        </div>

        <!-- Was bedeutet Zell-Drift? -->
        <div class="col-12 faq-item" data-tags="vitals batterie drift spread spannung zelle degradation balancing">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Vitals</span>
                        Was ist der Zell-Drift und wie hoch darf er sein?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Der <strong>Zell-Drift</strong> (in mV) ist die Differenz zwischen der Zelle mit der höchsten und der niedrigsten Spannung in einem Batterie-Pack. Er ist der wichtigste Frühindikator für den Zustand einer Lithium-Batterie:</p>
                    <ul>
                        <li>🟢 <strong>&lt; 30 mV:</strong> Hervorragend – alle Zellen sind im Gleichgewicht</li>
                        <li>🟡 <strong>30–50 mV:</strong> Gut – normaler Betrieb, kein Handlungsbedarf</li>
                        <li>🟠 <strong>50–100 mV:</strong> Erhöht – im Auge behalten, ggf. Service kontaktieren</li>
                        <li>🔴 <strong>&gt; 100 mV:</strong> Kritisch – mögliche Zelldegradation oder Balancing-Problem, E3DC-Service kontaktieren</li>
                    </ul>
                    <p>Ein hoher Drift entsteht, wenn einzelne Zellen altern und nicht mehr die gleiche Kapazität wie ihre Nachbarn halten. Das BMS (Battery Management System) versucht, dies durch Balancing auszugleichen, hat aber physikalische Grenzen.</p>
                </div>
            </div>
        </div>

        <!-- Warum werden nicht alle Packs angezeigt? -->
        <div class="col-12 faq-item" data-tags="vitals batterie packs schrank ghost leer fehlt">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Vitals</span>
                        Warum werden weniger Packs angezeigt als physisch vorhanden?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Die E3DC-RSCP-API gibt für jeden möglichen Speichersteckplatz einen Container zurück – auch für leere. Das Vitals-Dashboard erkennt "Ghost Packs" (leere Steckplätze ohne echte Daten) automatisch und blendet diese aus.</p>
                    <p>Sollte ein tatsächlich vorhandenes Pack fehlen, prüfen Sie ob alle Batterie-Module korrekt verbunden und im E3DC-Portal sichtbar sind.</p>
                </div>
            </div>
        </div>

        <!-- E3/DC-PV-Ladebegrenzung -->
        <div class="col-12 faq-item" data-tags="e3dc pv ladebegrenzung dcdc dc first zusatzwechselrichter auto max charge power speicher">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Speicher</span>
                        Was bewirkt „Laden an E3DC-PV koppeln“?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Die Option gibt dem E3/DC für Kurvenladung und DV-PV-Speichern einen sanften, flüchtigen Laderahmen: Die vom Storage-Simulator geplante Ladeleistung bleibt die Obergrenze, wird aber zusätzlich auf die aktuelle E3/DC-PV-Leistung begrenzt. Leistung eines zusätzlichen AC-Wechselrichters erhöht diesen Rahmen nicht.</p>
                    <ul>
                        <li>E3/DC bleibt in <strong>AUTO</strong>; Entladen für wechselnden Hausverbrauch bleibt jederzeit möglich.</li>
                        <li>Sinkt die E3/DC-PV-Leistung, wird die Ladegrenze nachgeführt.</li>
                        <li>Fehlt ein frischer, gültiger PV-Split, werden diese PV-basierten Ladepfade sicher auf 0 W begrenzt.</li>
                        <li>Die Funktion setzt ausschließlich flüchtige EMS-Grenzen und verändert keine dauerhaften Geräteeinstellungen.</li>
                    </ul>
                    <p>Das ist ein DC-first-Rahmen, aber keine physikalische Garantie, dass im E3/DC zu jedem Zeitpunkt ausschließlich DC-Leistung in die Batterie fließt. Preis- und ausdrücklich freigegebenes Netzladen bleiben eigenständige Verträge.</p>
                </div>
            </div>
        </div>

        <!-- Peak Shaving am Netzbezug -->
        <div class="col-12 faq-item" data-tags="peak shaving lastspitze netzbezug viertelstunde puffer reserve hysterese netz nachladen">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Lastspitzen</span>
                        Wie funktioniert Peak Shaving am Netzbezug?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Peak Shaving begrenzt den mittleren Netzbezug in einer festen Zähler-Viertelstunde. Beim Begrenzen und Halten bleibt E3/DC in AUTO; der Storage Manager setzt nur den flüchtigen Lade- oder Entladerahmen, der zum Schutz der eingestellten Grenze erforderlich ist. Die Funktion fordert keine Netzeinspeisung an.</p>
                    <ul>
                        <li>Der Speicherpuffer muss oberhalb der wirksamen Notstromreserve liegen.</li>
                        <li>Sicherheitsabstand, Leistungs- und SoC-Hysterese sowie Freigabe-Entprellung verhindern Flattern.</li>
                        <li>Bei einer zu großen Messlücke bleibt die Regelung passiv und beginnt erst mit einer neuen vollständigen Viertelstunde.</li>
                        <li>Netz-Nachladung des Puffers ist ein eigener, standardmäßig ausgeschalteter Opt-in, verwendet vorübergehend den angeforderten Netzlademodus und bleibt an Viertelstunden- und Hausanschlussgrenze gebunden.</li>
                    </ul>
                </div>
            </div>
        </div>

        <!-- PV-Prognosediagnose -->
        <div class="col-12 faq-item" data-tags="pv prognose diagnose e3dc historie 15 minuten trefferabweichung richtungsversatz vergleichsabdeckung">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Prognose</span>
                        Was macht die optionale PV-Prognosediagnose?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ein eigener niedrig priorisierter Dienst vergleicht gespeicherte E3/DC-DC-Prognosen mit abgeschlossenen nativen 15-Minuten-Historienslots. Er zeigt Trefferabweichung, Richtungsversatz, energiegewichtete Gesamtabweichung und Vergleichsabdeckung sowohl insgesamt als auch getrennt nach dem Erfassungs-Vorlauf.</p>
                    <ul>
                        <li>Die Funktion ist standardmäßig <strong>aus</strong>. Dann erfolgen weder Historienabfrage noch Datenbankschreibzugriff.</li>
                        <li>Rohdaten liegen privat außerhalb des Webverzeichnisses; das Dashboard erhält nur eine kleine sanitierte Zusammenfassung.</li>
                        <li>Vor mindestens 96 ertragsrelevanten Slots aus sieben Vergleichstagen und ohne vollständige Forecast-/Ist-Abdeckung bleiben Werte als vorläufig gekennzeichnet.</li>
                        <li>Der aktuelle Wert ist eine deterministische Punktprognose. Er gilt nicht automatisch als P50; echte Quantile benötigen ein ausdrücklich gebundenes Quantilniveau und die Konvention <code>cdf</code> oder <code>exceedance</code>.</li>
                        <li>Nur E3/DC-DC-PV besitzt derzeit ein gültiges Forecast-/Ist-Paar. Zusatzwechselrichter, Haus, Wärme und Wallbox bleiben ohne eigene validierte Ist-Historie <code>EVIDENCE_LIMIT</code>.</li>
                        <li>Abregelung, Wechselrichter-Clipping und externe Abschaltung sind noch nicht als eigener Qualitätsfilter gebunden. Die Werte bleiben sichtbar diagnostisch, dürfen aber keine verfügbare PV-Leistung oder Regelungsfreigabe beweisen.</li>
                        <li>Die Diagnose ändert weder Prognosemodell noch Konfiguration oder Speicherregelung.</li>
                    </ul>
                </div>
            </div>
        </div>

        <!-- PV-Einspeisebegrenzung -->
        <div class="col-12 faq-item" data-tags="pv einspeisebegrenzung abregelung einspeise limit netzeinspeise grenze kuppe ersparnis">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">PV-Einspeisebegrenzung</span>
                        Was zeigt die grüne Fläche im Diagramm?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Die PV-Einspeisebegrenzung hält die Einspeisung unter dem konfigurierten <strong>Netzeinspeiselimit</strong>. Im Verlaufs- und Hybrid-Diagramm zeigt die <strong>grüne Kuppe</strong> den PV-Überschuss, der ohne Speicherpufferung abgeregelt worden wäre.</p>
                    <p>Das Overlay ist ab v3.8.8.2 standardmäßig aktiv. Die rote gestrichelte Linie zeigt die dynamische Grenze (reagiert auf Hausverbrauch in Echtzeit).</p>
                    <p>Diese Darstellung ist nicht das neue <strong>Peak Shaving am Netzbezug</strong>. Dort begrenzt der Speicher teure Viertelstunden-Bezugsspitzen aus dem Stromnetz.</p>
                </div>
            </div>
        </div>

        <!-- Hybrid-Diagramm -->
        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-chart-line me-2"></i>Diagramme</h4>

        <div class="col-12 faq-item" data-tags="hybrid diagramm verlauf prognose zeitfenster 6h 12h 24h 48h mitte zukunft vergangenheit">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Hybrid-Chart</span>
                        Wie funktioniert das Hybrid-Diagramm (Live + Prognose)?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Das Hybrid-Diagramm verbindet den historischen Verlauf (durchgezogene Linien) nahtlos mit der Energie-Prognose (gestrichelte Linien) in einem einzigen Chart.</p>
                    <p>Die <strong>Zeitfenster-Buttons</strong> (6h / 12h / 24h / 48h) teilen das Fenster exakt hälftig auf:</p>
                    <ul>
                        <li><strong>6h</strong> → 3h Vergangenheit + 3h Zukunft</li>
                        <li><strong>12h</strong> → 6h Vergangenheit + 6h Zukunft</li>
                        <li><strong>24h</strong> → 12h Vergangenheit + 12h Zukunft</li>
                        <li><strong>48h</strong> → 24h Vergangenheit + 24h Zukunft</li>
                    </ul>
                    <p>Die aktuelle Uhrzeit liegt damit immer <strong>genau in der Mitte</strong> des Charts. Die X-Achse zeigt saubere <strong>15-Minuten-Ticks</strong> (:00 / :15 / :30 / :45).</p>
                </div>
            </div>
        </div>

        <!-- Dark/Bright Mode -->
        <div class="col-12 faq-item" data-tags="dark mode bright light theme hell dunkel batterie gradient hintergrund flackern">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">UI</span>
                        Der Batterie-Hintergrund im Energiefluss bleibt nach Theme-Wechsel im alten Modus?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ab v3.8.8.10.2 reagiert der Batterie-SOC-Gradient im Energiefluss-Diagramm <strong>sofort</strong> auf den Dark/Bright-Mode-Wechsel – kein Strg+F5 oder Datenpoll mehr nötig.</p>
                    <p>Die Lösung basiert auf CSS Custom Properties (<code>--bat-fill</code>, <code>--bs-body-bg</code>), die der Browser nativ und synchron aktualisiert. Falls auf älteren Systemen noch ein Flackern auftreten sollte, hilft ein einmaliges Hard-Refresh (<code>Strg+F5</code>), um den Browser-Cache zu leeren.</p>
                </div>
            </div>
        </div>

        <!-- Web-Push -->
        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-bell me-2"></i>Benachrichtigungen</h4>

        <!-- Telegram UI -->
        <div class="col-12 faq-item" data-tags="telegram watchdog bot token chatid meldung script setup konfiguration">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Telegram</span>
                        Wo stelle ich Telegram für den System-Watchdog ein?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Früher mussten Telegram-Daten kompliziert im Installer-Terminal über ein Bash-Skript übergeben werden. Ab E3DC-Control v3.9.0 pflegen Sie Token und Chat-ID <strong>bequem im Web-Interface</strong> unter <em>Konfiguration -> Telegram & Watchdog</em>.</p>
                    <p>Sobald Sie die Daten dort speichern, übernimmt der Pi-Watchdog diese Änderungen vollautomatisch "live" im Hintergrund ohne weiteren Installer-Aufruf und sendet bei Systemhängern sofort Notfall-Meldungen auf Ihr Smartphone.</p>
                </div>
            </div>
        </div>

        <div class="col-12 faq-item" data-tags="web push push notification benachrichtigung pwa smartphone handy alarm">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Web-Push</span>
                        Wie richte ich Push-Benachrichtigungen auf dem Smartphone ein?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>E3DC-Control unterstützt native Web-Push-Benachrichtigungen (kein App-Store nötig). Voraussetzung ist die PWA-Installation:</p>
                    <ol>
                        <li>Öffnen Sie das Dashboard im Browser (Chrome/Safari) über Ihre Cloudflare-URL oder lokale IP.</li>
                        <li>Installieren Sie die Seite als PWA (<em>"Zum Home-Bildschirm hinzufügen"</em>).</li>
                        <li>Öffnen Sie die PWA und navigieren Sie zu <strong>Konfiguration → Web-Push</strong>.</li>
                        <li>Klicken Sie auf <strong>"Dieses Gerät registrieren"</strong> und bestätigen Sie die Browser-Erlaubnis.</li>
                    </ol>
                    <p>Benachrichtigungen werden gesendet für: Ziel-SoC erreicht, Netzausfall (Inselbtrieb), Updates verfügbar, Wallbox angesteckt und Automatik-Boost-Start. Interaktive <strong>Action-Buttons</strong> (z.B. "Sofortladen Max") ermöglichen direktes Reagieren ohne Dashboard öffnen zu müssen.</p>
                </div>
            </div>
        </div>

        <!-- Pre-Dump und Quell-Erholung -->
        <h4 class="mt-5 mb-4 text-accent"><i class="fas fa-brain me-2"></i>Lademanagement & KI</h4>

        <!-- Storage Simulator & EPEX -->
        <div class="col-12 faq-item" data-tags="storage simulator ki batterie forecast epex ladeplan v4">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">V4</span>
                        Wie arbeitet der neue Storage Simulator (V4) & Wallbox Ladeplan?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ab <strong>E3DC-Control v3.9.2</strong> wurde die Batterieladung von starren C++ Parametern auf dynamische KI-gestützte Python Logik umgestellt. Der auf Python basierende <code>storage_simulator</code> läuft als Endlosschleife und zieht alle 15 Minuten Daten heran:</p>
                    <ul>
                        <li><strong>Wetterverlauf:</strong> PV-Erzeugung (korrigiert auf 15-Min-Blöcke).</li>
                        <li><strong>Börsenpreise:</strong> EPEX-Preise der günstigsten Stunden.</li>
                    </ul>
                    <p>Daraus generiert der <code>wallbox_manager</code> kontinuierlich Ladepläne (z.B. <code>native_wallbox_schedule.json</code>). Diese Pläne greift die UI ab und zeigt Ladefenster auch dann sofort an, wenn das Altsystem offline ist.</p>
                </div>
            </div>
        </div>

        <!-- Wallbox Mode 0 Freigabe -->
        <div class="col-12 faq-item" data-tags="wallbox mode 0 e3dc autonom beobachten rscp heartbeat freigabe">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Was bedeutet Mode 0 bei E3DC-native und Fremd-Wallboxen?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ab v4.8.9 ist Mode 0 wirklich still: Bei E3DC-nativen Wallboxen bedeutet <strong>Nur beobachten, E3DC regelt</strong>, dass E3DC-Control keine externen RSCP-Wallbox-Befehle und keinen Heartbeat mehr sendet. Die E3DC-Firmware regelt dann allein.</p>
                    <p>Bei Fremd-Wallboxen bedeutet Mode 0 <strong>Nur beobachten, Wallbox regelt</strong>. Der Wallbox Manager überspringt diese Wallbox vollständig, während andere aktive Wallboxen weiter nach ihrem eigenen Modus geregelt werden können.</p>
                </div>
            </div>
        </div>

        <!-- Wallbox AUTO und wbminSoC -->
        <div class="col-12 faq-item" data-tags="wallbox auto openwb wbminsoc hysterese heartbeat netzbezug freilauf">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Wallbox</span>
                        Warum sendet E3DC-Control im AUTO-Modus keinen Heartbeat mehr?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ab v4.9.0 ist <code>AUTO</code> nur noch ein Freigabe-Befehl. Wenn der E3DC bereits im AUTO-Modus ist, sendet Python keinen wiederholten RSCP-Befehl mehr. Die E3DC-Firmware regelt Hausversorgung, Speicher und Netzpunkt dann allein.</p>
                    <p><strong>Wetterbasiertes Laden im E3/DC:</strong> Wenn E3DC-Control die Ladekurve führt, sollte diese E3/DC-Funktion ausgeschaltet sein. Sie ist ein zweiter Ladeplaner und kann Ladekapazität zurückhalten, obwohl E3DC-Control bereits eine passende AUTO-Ladeobergrenze bereitstellt. Die Open-Meteo-/Forecast-Prognose von E3DC-Control bleibt davon unabhängig aktiv. Bei gleichzeitigem E3/DC-Status <em>Laden gesperrt</em> und <em>Warten auf Sonnenschein</em> zeigt E3DC-Control das externe Veto an, verändert die Geräteeinstellung aber nicht automatisch.</p>
                    <p>Ab v4.9.1a gibt die Nachtfreigabe den E3DC bei <code>PV=0W</code> wieder in <code>AUTO</code> frei, statt die Tagesladekurve nachts mit <code>IDLE</code> oder Autodump zu erzwingen. Aktiver Pre-Discharge ist davon ausgenommen und läuft weiter.</p>
                    <p>Ab v4.9.1b folgt Pre-Discharge einer eigenen Entladerampe mit Hysterese. Die normale Ladekurve kann den morgendlichen Pre-Dump dadurch nicht mehr zu frueh pausieren.</p>
                    <p>Ab v4.9.1c sendet die aktive Ladekurve ihren berechneten <code>iFc</code>-Wert direkt als Lade- oder Entlade-Führung. Dadurch entstehen keine kurzen Gegenkorrekturen mehr, bei denen zuerst ein um 100 W versetzter Wert und danach sofort der begrenzte Wert an den E3DC gesendet wird.</p>
                    <p>Ab v4.9.2 nutzt der Preis-Boost das Verhalten des C++-Vorgängers <code>awtest=3</code>: Im günstigen Preisfenster bleibt der Speicher per <code>GRID</code> im Ladepfad, während freigegebene Wallboxen und Wärmepumpen Netzleistung nutzen dürfen. Nach Ende des Preisfensters prüfen alle Dienste die Fensterzeit selbst und fallen wieder auf Ladekurve bzw. Autonom-Regelung zurück.</p>
                    <p>Das ist wichtig, weil wiederholte AUTO-Befehle bestimmte Anlagen alle 30 Sekunden leicht anstossen konnten. Sichtbar war das als kurze Welle aus Batterie-Laden und Netzeinspeisung.</p>
                    <p>Für openWB und openWB Pro trennt V4 jetzt öffentliche Modi von der internen Treiberlogik. <code>Aus / autonom</code> sendet keine Ladebefehle, <code>PV-Kurve ruhig</code> regelt weich entlang der Speicher-Kurve, und <code>Sofort bis Preislimit</code> öffnet Netzstrom nur bei freigegebener Preisgrenze. Die Batterieentladung hat eine SoC-Hysterese, damit sie an wbminSoC nicht taktet.</p>
                </div>
            </div>
        </div>

        <!-- TL-Bremse, 0%-Altanker und Autodump -->
        <div class="col-12 faq-item" data-tags="ladekurve tl bremse autodump wolken storage manager 0 prozent morning soc docker idle discharge">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Ladekurve</span>
                        Warum steht TL-BREMSE im Log, aber der Speicher lädt trotzdem?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Bis v4.7.8 konnte die TL-BREMSE im Log korrekt erscheinen, real aber noch <code>AUTO</code> senden. Damit war die harte Ladebegrenzung praktisch wieder freigegeben. Ab <strong>v4.7.9</strong> sendet die harte TL-Bremse echtes <code>IDLE</code> mit niedrigem Lade-Limit.</p>
                    <p><code>storage_morning_soc=0</code> bedeutet ab v4.7.9 "kein Morgen-Deckel" und nicht mehr "Kurve bei 0% starten". Bereits eingefrorene 0%-Altanker werden verworfen und beim nächsten Planlauf sauber neu aufgebaut.</p>
                    <p>Wenn der Speicher oberhalb der Ladekurve liegt und Last oder Wolken den PV-Überschuss druecken, bleibt der E3DC im normalen Betrieb autonom. Aktives Entladen ist nur noch ein Schutzpfad für Pre-Dump, Preislogik und manuelle Befehle. Abregelschutz, Pre-Dump, aWATTar und Notreserve haben immer Vorrang, damit daraus keine Regelschleife entsteht.</p>
                    <pre>storage_morning_soc = 0   # Morgen-Deckel aus</pre>
                </div>
            </div>
        </div>

        <div class="col-12 faq-item" data-tags="predump pre-dump quell-erholung speicher ladekurve entladen batterie prognose laden">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">Speicher</span>
                        Wie werden Pre-Dump und Quell-Erholung geführt?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Speicher-Entladung, Quell-Erholung und Ladekurve werden zentral durch Storage Simulator und Storage Manager geführt:</p>
                    <ul>
                        <li><strong>Pre-Dump:</strong> entlädt geplant bis zur eingestellten Untergrenze und hält dabei Verbraucher, Netzgrenze und Kurvenstart im Blick.</li>
                        <li><strong>Quell-Erholung:</strong> pausiert die Wärmepumpe nur mit Storage-Manager-Auftrag, damit die Wärmequelle vor einer erwarteten PV-Kante regenerieren kann.</li>
                    </ul>
                    <p>Die Wärmequelle wird in der Wärmepumpen-Konfiguration gesetzt. Quell-Erholung ist nur für Sole/Erdreich, Grundwasser oder Direktverdampfung freigegeben; Luft-Wärmepumpen und unbekannte Quellen werden nicht pausiert.</p>
                    <p><strong>Hinweis:</strong> Der Energy Manager ist dabei nur noch Aktor. Er entscheidet nicht eigenständig gegen die Storage-Manager-Führung.</p>
                </div>
            </div>
        </div>

        <!-- Temperatur KI Prognose -->
        <div class="col-12 faq-item" data-tags="wetter temperatur prognose ml machine learning luxtronik archiv weather_forecast ai ki wärmepumpe verbrauch">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">KI</span>
                        Wie genau prognostiziert die KI den zukünftigen Strom-Verbrauch? (Luxtronik-Archiv)
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ein wichtiger Faktor für zukünftigen Strombedarf ist der reale Wärmepumpen-Verbrauch (Heizen/Kühlen), der massiv von der Außentemperatur abhängt. Das "Machine-Learning" (ML) von E3DC-Control lernt deshalb stündlich Ihr Hausverhalten.</p>
                    <p>Ab v3.9.6 liest das KI-Training historische Außen-Temperaturen aus Ihrem verlässlichen <strong>Luxtronik- / Wärmepumpen-Archiv</strong> in der Datenbank ein und gleicht diese mit der slot-genauen echten <strong>Wetter-Vorhersage für morgen (Open-Meteo API)</strong> ab. So weiß die Software im Voraus stets akkurat, auf welches exakte Temperaturniveau das Haus nachts absinken wird und wie hoch der reale Strombedarf zum Zuheizen sein wird.</p>
                </div>
            </div>
        </div>

        <!-- IDM Kühlung -->
        <div class="col-12 faq-item" data-tags="idm kühlung kühlen sommer kältespeicher register khl boost kühlmodus">
            <div class="faq-card">
                <div class="faq-question">
                    <div>
                        <span class="tag">IDM</span>
                        Wie funktioniert die PV-Überschuss-Kühlung bei IDM-Wärmepumpen?
                    </div>
                    <i class="fas fa-chevron-down"></i>
                </div>
                <div class="faq-answer">
                    <p>Ab v3.8.8.10 steuert E3DC-Control auch die <strong>Kühlfunktion</strong> der IDM-Wärmepumpe via PV-Überschuss:</p>
                    <ul>
                        <li>Sobald die Außentemperatur die Heizgrenze überschreitet (<strong>Sommermodus</strong>), fordert der Energy Manager zusätzlich Kühlenergie an (Register 1711).</li>
                        <li>Die Zieltemperatur des Kältespeichers wird im Dashboard unter <strong>Kühlung Soll (KHL)</strong> eingestellt (Register 1010).</li>
                        <li>Hysterese: Das System kühlt nur, wenn die Ist-Temperatur mehr als 2°C über dem Sollwert liegt.</li>
                        <li>Auch der <strong>manuelle Boost-Button</strong> im Dashboard aktiviert im Sommer automatisch die Kühlung parallel zur Warmwasserbereitung.</li>
                    </ul>
                </div>
            </div>
        </div>

    </div>

    <div id="noResults" class="text-center py-5" style="display: none;">
        <i class="fas fa-search fs-1 text-muted mb-3"></i>
        <h4>Keine Hilfethemen gefunden.</h4>
        <p class="text-muted">Versuchen Sie es mit anderen Begriffen wie "MQTT", "Solar" oder "Tibber".</p>
    </div>
</div>

<script src="assets/vendor/bootstrap/js/bootstrap.bundle.min.js"></script>
<script>
    // FAQ Toggle
    document.querySelectorAll('.faq-question').forEach(q => {
        q.addEventListener('click', () => {
            const card = q.parentElement;
            const answer = card.querySelector('.faq-answer');
            const isOpen = answer.style.display === 'block';
            answer.style.display = isOpen ? 'none' : 'block';
            q.querySelector('i').style.transform = isOpen ? 'rotate(0deg)' : 'rotate(180deg)';
        });
    });

    // Suche
    const searchInput = document.getElementById('helpSearch');
    const items = document.querySelectorAll('.faq-item');
    const noResults = document.getElementById('noResults');

    searchInput.addEventListener('input', (e) => {
        const term = e.target.value.toLowerCase();
        let hasResults = false;

        items.forEach(item => {
            const content = item.innerText.toLowerCase();
            const tags = item.getAttribute('data-tags').toLowerCase();

            if (content.includes(term) || tags.includes(term)) {
                item.style.display = 'block';
                hasResults = true;
            } else {
                item.style.display = 'none';
            }
        });

        noResults.style.display = hasResults ? 'none' : 'block';
    });

    // Direkt-Suche via URL Parameter
    const urlParams = new URLSearchParams(window.location.search);
    if (urlParams.has('q')) {
        searchInput.value = urlParams.get('q');
        searchInput.dispatchEvent(new Event('input'));
    }
</script>

</body>
</html>
