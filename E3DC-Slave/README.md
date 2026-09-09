# E3DC-Slave – eigenständiger Testregler

Optionales Beispiel für einen zweiten S10 hinter dem Hauszähler des ersten S10.
Der Master veröffentlicht sein Verbraucherbudget über MQTT. Das Skript liest
dieses Budget und steuert ausschließlich den Slave über RSCP. Es wird nicht
automatisch installiert oder im E3DC-Control-Container gestartet.

## Bestehendes Skript aktualisieren

Altes Skript anhalten. `s10_slave.py`, `slave_policy.py` und `power_limits.py`
gemeinsam in einen neuen Ordner kopieren. Die eigene `config.json` übernehmen;
Adressen, Kapazitäten, Reserven und Nachtfreigabe bleiben damit erhalten.
Unter `controller` kann `"discharge_start_w": 100` eingetragen werden.
Auch ohne diesen neuen Eintrag gelten 100 W als Vorgabe.

## Einrichtung und Start

Linux mit Python ab 3.10, `python-e3dc` (Paketname `pye3dc`) und `paho-mqtt` 2.x verwenden.
Bei einer Neueinrichtung in einer eigenen Python-Umgebung:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install pye3dc 'paho-mqtt>=2,<3'
cp config.example.json config.json
```

Slave-Adresse, MQTT-Zugang und passende Leistungsgrenzen in `config.json`
eintragen. 3000 W sind Beispielwerte, keine allgemeine Gerätevorgabe.
Zugangsdaten stehen ausschließlich in den Umgebungsvariablen `S10_USER`,
`S10_PASSWORD`, `S10_RSCP_KEY` und bei MQTT-Anmeldung `S10_MQTT_PASSWORD`.
Konfiguration und Logdateien nicht öffentlich hochladen.

Beobachten (keine Steuerbefehle):

```bash
python3 s10_slave.py --config config.json > beobachtung.jsonl
```

Aktiv regeln:

```bash
python3 s10_slave.py --config config.json --execute > betrieb.jsonl 2> betrieb-fehler.log
```

Auf dem Slave muss vorher die andere Speicherregelung beendet sein. In einer
E3DC-Control-Version mit dem Schalter **Speicherregelung aktiv** diesen in der
Konfiguration ausschalten und die bestätigte Freigabe abwarten. Danach kann
E3DC-Control dort zur Anzeige weiterlaufen. Bei älteren Versionen den bisherigen
Speicherregler stoppen. Der Master bleibt aktiv. Der lokale Prozesslock schützt
nur Aufrufe mit derselben Konfigurationsdatei; andere Regler erkennt er nicht.

## Entladestart und Nachtbetrieb

Im aktiven Betrieb liest das Skript die Gerätegrenzen und setzt nötigenfalls
einmal die native Entlade-Startschwelle auf mindestens 100 W. Bereits höhere
Schwellen und die gelesenen maximalen Lade-/Entladeleistungen bleiben erhalten;
auch ein vorhandenes Null-Limit wird nicht aufgehoben. Die Begrenzungsfunktion
wird dafür aktiviert. Erst ein passender Rücklesewert erlaubt Regelbefehle.

Nach der einmaligen Einrichtung wird bei abweichendem Rücklesewert noch zweimal
mit kurzem Abstand ausschließlich gelesen. Die angeforderten und zuletzt
gelesenen Werte stehen im Log; eine bleibende Abweichung beendet den Lauf.
Auch eine fehlende Schreibantwort wird nur durch Rücklesen geklärt.

Alle 30 Sekunden und vor einem Wiederanlauf nach Verbindungsfehlern wird
ausschließlich zurückgelesen. Veränderte Einstellungen beenden den Lauf;
es gibt keine automatische erneute Schreibschleife.
`settings_readback_confirmed` bestätigt die Einstellung,
noch nicht die tatsächliche Batterie- oder Wechselrichterreaktion. Diese muss
am Gerät bzw. an den Messwerten geprüft werden.

Eigene Entladebefehle unter 100 W werden ebenfalls vermieden. Zum Einschalten
muss der berechnete Slave-Anteil mindestens 175 W erreichen (100 W plus
75 W Hysterese). Der erste Schritt beträgt 100 W, danach höchstens 50 W je
fünf Sekunden. Fällt der Bedarf unter 100 W, wird die Entladung beendet.
Die Schwelle ist keine Begrenzung der maximalen Entladeleistung.

Für die Nacht `night_enabled` auf `true` setzen und beide **nutzbaren**
Kapazitäten in Wh sowie die Reserven eintragen. `night_enabled` allein aktiviert
keine Ausgänge: Dafür ist weiterhin `--execute` erforderlich. Der Nachtanteil
richtet sich nach der verfügbaren Energie oberhalb der jeweiligen Reserve.
Die Bilanz ist wegen unbekannter Umwandlungsverluste eine Näherung.

Beim Anhalten nach begonnenen Regelbefehlen versucht das Skript IDLE mit 0 W.
Die Gerätegrenzen einschließlich Startschwelle bleiben gesetzt. Ihre vorherigen
Werte stehen im ersten Logeintrag `power_settings.before`; das Skript setzt sie
nicht beim Beenden automatisch zurück. Nach Prozessende ist die Batterie nicht
weiter überwacht oder dauerhaft durch das Skript gesperrt.
`final_power_settings` enthält mit `last_known: true` den letzten bekannten
Rücklesestand und ist keine neue Bestätigung beim Beenden.

## Verbindungsfehler und Wiederanlauf

Messwerte, Grenzwert-Rücklesungen und Leistungsaufträge verwenden gemeinsam eine
offene RSCP-Verbindung. Die Verbindung wird bei einem Fehler oder beim Beenden
geschlossen. Dadurch entstehen zwischen Lesen und Regeln keine unnötigen neuen
Anmeldungen. Das vermeidet eigene häufige Sitzungswechsel; eine vom Gerät mit
`RSCP_ERR_ALREADY_IN_USE` gemeldete Belegung wird dadurch nicht übergangen. Der
Code allein beweist nicht, dass ein zweites Programm läuft, und benennt keinen
Besitzer der Belegung.

Bekannte RSCP-Leseabfragen erhalten höchstens drei Versuche mit kurzem Abstand.
Schreibaufträge erhalten weiterhin genau einen Versuch. Ein ausgeschöpfter
`SendError` im Regelbetrieb wird als `rscp_error` mit der betroffenen Phase in
der JSON-Datei protokolliert. Die weitere Verbindung wird nach 5, 10, 20 und
höchstens 30 Sekunden erneut geprüft. Sechs Fehler ohne vollständige Erholung
beenden den Lauf. Dafür reichen einzelne erfolgreiche IDLE-Aufrufe nicht aus:
Auch frische Messdaten und bestätigte Gerätegrenzen müssen wieder vorliegen.
Anmelde-, Einstellungs- und sonstige Programmfehler bleiben Abbruchgründe.

Nach einem Fehler verwirft das Skript den bisherigen MQTT-Datensatz und den
alten Leistungssollwert. Aktive Ladung oder Entladung darf erst nach neuen,
frischen Master- und Slave-Daten sowie unverändert zurückgelesenen Gerätegrenzen
wieder beginnen: zunächst IDLE, mindestens 20 Sekunden Neutralzeit, dann die
normale kleine Rampe. Das Skript spielt keinen fehlgeschlagenen Lade- oder
Entladeauftrag erneut ab. Solange die Verbindung gestört ist, bleibt die
tatsächliche Wirkung des letzten Auftrags unbestätigt; dessen geräteseitiger
Timeout wird dadurch nicht ersetzt.

`invalid_or_missing_sample` bedeutet, dass noch keine verwendbaren Messdaten
vorliegen, beispielsweise unmittelbar nach dem MQTT-Start. Es wird dann kein
Lade- oder Entladeauftrag berechnet. Die Master-Instanz muss für den Betrieb
weiter aktuelle MQTT-Daten liefern; ihr Abschalten entzieht dem Slave-Regler
seine Datengrundlage. Bei einem Abbruch stehen `stopped` und eine
gegebenenfalls unbestätigte abschließende IDLE-Anforderung ebenfalls im JSON-Log.
Die zusätzliche Datei `betrieb-fehler.log` bewahrt die Bibliotheksmeldungen auf.

Die Startzeile `runtime_diagnostics` nennt die Diagnosekennung
`rscp_persistent_session_v3` und die installierte `pye3dc`-Version, soweit ermittelbar.
Bei Fehlern enthält `error_chain` höchstens sechs verkettete Fehlertypen und
gegebenenfalls numerische Betriebssystem-Fehlercodes (`errno`), ohne Meldungstexte
oder Paketdaten. Bei einer dekodierten RSCP-Fehlerantwort wird zusätzlich der
bekannte Fehlername als `rscp_error_code` erfasst. Unbekannte Werte und freie
Exception-Texte werden nicht übernommen. `error_chain_truncated` kennzeichnet
eine begrenzte Kette.
`attempted_command` nennt beim `rscp_error` den versuchten Leistungsauftrag;
bei einem vorherigen Lesefehler steht dort `null`. Die Phase `set_power` umfasst
auch einen gegebenenfalls nötigen Verbindungsaufbau und den Antwortempfang.
Diese Angaben ändern weder Wiederholungen noch Neutralzeit oder Regelung.

## Daten und Grenzen des Versuchs

Laden erfordert frisches MQTT-Budget und gleichzeitig gemessene Einspeisung;
Netzbezug oder Master-Entladung stoppen die Slave-Ladung. Der Puffer beträgt
standardmäßig 200 W. Reserven, Voll-Erkennung, Neutralzeit und Richtungswechsel
begrenzen die Befehle. Veraltete Daten und Verbindungsverlust verhindern neue
Lade- und Entladeaufträge. Ein ungeklärter Lade-, Entlade- oder Einstellungsauftrag
wird nicht automatisch erneut gesendet.

Der Master-MQTT-Zeitstempel bezeichnet bisher den Veröffentlichungszeitpunkt.
Die Prüfung kann eine vom Publisher erneut datierte alte Messung nicht sicher
erkennen. Die Leistungsreaktion muss im Feld beobachtet werden. Dieses Beispiel
ist noch keine vollständig integrierte Verwaltung mehrerer E3DC-Speicher.

## Technische Referenz und Lizenz

Die verwendeten Methoden, Vorzeichen und Rücklesefelder wurden gegen
[python-e3dc](https://github.com/fsantini/python-e3dc/blob/master/e3dc/_e3dc.py)
und die RSCP-Tags in `Installer/rscp_client.py` geprüft. Das Beispiel steht wie
E3DC-Control unter AGPL-3.0-or-later; siehe `LICENSE` im Hauptverzeichnis.
