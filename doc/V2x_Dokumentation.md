# V2H/V2G-Telemetrie (read-only)

E3DC-Control erkennt negative Wallboxleistung als bidirektionalen Energiefluss und stellt diesen Zustand im Dashboard und über die lokale Telemetrie dar. Die Integration ist derzeit ausschließlich lesend: Sie startet oder stoppt keine Entladung, ändert keine Wallboxleistung, schaltet keine Phasen und betätigt weder CP noch ein Schütz.

## Anzeige und Grenzwerte

Bei erkanntem Energiefluss vom Fahrzeug zum Haus zeigt das Dashboard den V2H/V2G-Zustand und die Flussrichtung an. Mit `v2h_enable=1` wird zusätzlich die lokale Grenzwertauswertung aktiviert:

- `v2h_min_soc` ist die Warnschwelle für den Fahrzeug-SoC.
- `v2h_bat_soc_limit` ist die Warnschwelle für den Haus-Speicher-SoC.

Das Ergebnis ist eine read-only Empfehlung (`allowed`) samt Begründung. Auch bei einer Grenzwertverletzung erzeugt E3DC-Control keinen Hardwarebefehl. Der Betreiber muss die Schutz- und Abschaltlogik im zertifizierten bidirektionalen Lade- beziehungsweise Fahrzeugsystem konfigurieren.

## Home Assistant und MQTT

Der lokale MQTT-Hub kann den Sensor `binary_sensor.e3dc_ctrl_v2h_allowed` bereitstellen. Er dient nur als Zustandsinformation für eine externe, vom Betreiber verantwortete Automation. Ein Wechsel des Sensors führt innerhalb von E3DC-Control zu keiner Wallboxaktion.

## Sicherheitsgrenze

Aktive V2H-/V2G-Steuerung ist nicht Bestandteil dieses Releases. Vor einer späteren Freigabe sind ein dokumentierter Gerätevertrag, eine eindeutige Writer-/Owner-Lease, ein dokumentierter Herstellernachweis und ein bestätigter Hardware-Safe-State erforderlich.
