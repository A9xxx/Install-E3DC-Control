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

Linux mit Python ab 3.10, `python-e3dc` und `paho-mqtt` 2.x verwenden.
Bei einer Neueinrichtung in einer eigenen Python-Umgebung:

```bash
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install python-e3dc 'paho-mqtt>=2,<3'
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
python3 s10_slave.py --config config.json --execute > betrieb.jsonl
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

Alle 30 Sekunden wird ausschließlich zurückgelesen. Veränderte oder nicht
lesbare Einstellungen beenden den Lauf; es gibt keine automatische erneute
Schreibschleife. `settings_readback_confirmed` bestätigt die Einstellung,
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

## Daten und Grenzen des Versuchs

Laden erfordert frisches MQTT-Budget und gleichzeitig gemessene Einspeisung;
Netzbezug oder Master-Entladung stoppen die Slave-Ladung. Der Puffer beträgt
standardmäßig 200 W. Reserven, Voll-Erkennung, Neutralzeit und Richtungswechsel
begrenzen die Befehle. Veraltete Daten und Verbindungsverlust stoppen sie.
Ein ungeklärter RSCP-Auftrag wird nicht automatisch erneut gesendet.

Der Master-MQTT-Zeitstempel bezeichnet bisher den Veröffentlichungszeitpunkt.
Die Prüfung kann eine vom Publisher erneut datierte alte Messung nicht sicher
erkennen. Die Leistungsreaktion muss im Feld beobachtet werden. Dieses Beispiel
ist noch keine vollständig integrierte Verwaltung mehrerer E3DC-Speicher.

## Technische Referenz und Lizenz

Die verwendeten Methoden, Vorzeichen und Rücklesefelder wurden gegen
[python-e3dc](https://github.com/fsantini/python-e3dc/blob/master/e3dc/_e3dc.py)
und die RSCP-Tags in `Installer/rscp_client.py` geprüft. Das Beispiel steht wie
E3DC-Control unter AGPL-3.0-or-later; siehe `LICENSE` im Hauptverzeichnis.
