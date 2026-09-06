# E3DC-Control: Fahrzeug Integration (SoC)

Um Ladefenster, Pre-Dump, Ziel-SoC und die Wallbox-Regelung sauber zu nutzen,
sollte das System den aktuellen Ladestand (SoC) deines Elektroautos kennen.
Das System bietet dir dafür vier komfortable Wege:

## Grundregel: nur bestätigter SoC steuert

Ein Fahrzeugprofil allein ist noch kein gesicherter Ladestand. E3DC-Control
nutzt einen Fahrzeug-SoC für Anzeige, Ziel-SoC, Restladezeit und `Auto voll`
erst dann, wenn er aus einer bestätigten Quelle kommt:

- Wallbox/openWB/openWB Pro liefert den SoC selbst,
- Bluelink oder MQTT liefert einen plausiblen Fahrzeug-SoC,
- der Nutzer setzt den aktuellen SoC bewusst über **SoC setzen**.

Unbestätigte Werte aus alten Sessions, gespeicherten Profilen oder der
Einfachansicht werden nicht als Regelbasis übernommen und normalerweise als
`-- SoC` angezeigt. Ab 5.4.5a darf ein frisch beobachteter openWB-SoC mit
Quelle und Alter als ausdrücklich rein lesender Wert erscheinen, wenn er zur
aktuellen Stecksession oder zu einem eindeutig passenden Fahrzeugprofil
gehört. Diese Beobachtung bestätigt keinen Regel-SoC: Ziel-SoC, `Auto voll`,
Planung und Hardwarebefehle bleiben am getrennten bestätigten Session- und
Fahrzeugvertrag geschlossen.

Nach Abstecken und erneutem Anstecken braucht die Session für SoC-basierte
Entscheidungen wieder einen frischen bestätigten SoC oder eine neue manuelle
Eingabe. Die Wallbox darf trotzdem nach PV, Budget, Preisfenster oder kWh-Ziel
laden; nur SoC-basierte Abschlüsse und Zielladungen bleiben ohne gesicherten
Wert aus.

## openWB Pro ohne eigenen Fahrzeug-SoC

Die reine Einstellung **PWM** auf der Maintenance-Seite der Pro bietet keine
SoC-Abfrage über das Ladekabel. Dafür ist bei einem kompatiblen Fahrzeug
**PWM mit Fahrzeugerkennung und SoC Auslesung** vorgesehen. Die Hersteller-
Einstellung muss gespeichert werden und startet die Pro neu. Manche Fahrzeuge
nehmen nach dieser zusätzlichen Kommunikation vorübergehend keine AC-Ladung
an; deshalb nicht während einer laufenden Ladung ungeprüft umstellen.
E3DC-Control ändert diese Geräteinstellung nicht automatisch.
Die Kabelabfrage erfolgt üblicherweise zum Ladestart, danach wird auch bei
openWB anhand der geladenen Energie weitergerechnet. Details und Hinweise zur
Kompatibilität stehen in der [openWB-Herstelleranleitung](https://wiki.openwb.de/doku.php?id=openwb:vc:2.2.0:software:fahrzeug-infos:pro-proplus).

Nicht jeder Ladevorgang liefert über die openWB Pro einen Fahrzeug-SoC.
Fehlt dieser Wert, kann ein eindeutig zugeordnetes Fahrzeugprofil mit
bestätigtem Cloud-SoC die laufende Stecksession verankern. Aus der anschließend
gemessenen Ladeenergie, der eingestellten Akkukapazität und dem Lade-Wirkungsgrad
wird der SoC weitergerechnet. Das bleibt ein Schätzwert, keine neue Messung
des Fahrzeugs; der ursprüngliche Quellzeitpunkt wird nicht verjüngt.

Ein fahrzeugseitiges AC-Ladelimit bleibt unabhängig vom Ziel in E3DC-Control
wirksam. Das Auto kann deshalb bei seinem eigenen Limit aufhören, obwohl
unsere Hochrechnung noch etwas darunter liegt. Ein Stromangebot allein ist
kein Beleg einer laufenden Ladung; maßgeblich sind die tatsächlichen
Phasenströme und die gemessene Ladeleistung. Aus 0 W allein lässt sich weder
ein erreichtes Fahrzeugziel noch ein Fehler sicher ableiten.

Ein gültiger Anker darf innerhalb derselben Sitzung bis zur bestehenden
Acht-Stunden-Grenze fortgeführt werden, auch wenn der Roh-Cloudwert für eine
neue Verankerung inzwischen zu alt wäre. Abstecken, ein zurückgesetzter
Ladezähler oder eine widersprüchliche Fahrzeugzuordnung erlauben keine
unveränderte Übernahme in eine neue Sitzung. Liefert die openWB Pro einen
frischen, bestätigten Geräte-SoC, verwendet die Wallboxregelung diesen direkt
und baut keinen zusätzlichen Cloud-Schätzanker auf.

Bei aktiv geregelter openWB Pro und eingerichteter Bluelink-Zuordnung kann
ein fehlender brauchbarer SoC beim Anstecken einen automatischen Abruf für
genau das zugeordnete Cloudfahrzeug auslösen. Pro Stecksession wird höchstens
ein Auftrag gestellt; schnelle erneute Steckvorgänge respektieren zusätzlich
das eingestellte Cloudintervall. Ein vorhandener manueller Abruf wird nicht
überschrieben. Ausgeschaltete oder manuell pausierte Ladepunkte lösen keinen
automatischen Abruf aus. Ein Cloudfehler startet keine Wiederholungsschleife
und verändert weder Ladefreigabe noch Phasen- oder Stromregelung.

---

## Weg 1: Der autarke Bluelink-Client (Hyundai & Kia)
Wenn du ein Fahrzeug von Hyundai oder Kia besitzt, bietet der Installer einen eigenen, autarken Client, der den SoC direkt von den Herstellerservern abruft.

**Einrichtung (Token erstellen):**
Hyundai und Kia nutzen ein hCaptcha, weshalb der Login über ein Skript am Computer erfolgen muss.
1. Lade dir das empfohlene Python-Hilfsskript auf deinen Windows/Mac-PC herunter: Anleitung im EVCC Wiki
2. Führe das Skript aus und logge dich im sich öffnenden Chrome-Fenster mit deinen Bluelink-Daten ein.
3. Kopiere den im Terminal generierten, sehr langen `refresh_token`.
4. Führe den Menüpunkt **107 (Hyundai/Kia SoC-Abfrage einrichten)** im E3DC-Installer aus.
5. Füge dort deinen Token ein.

**Konfiguration im Web-Dashboard:**
Im Config-Editor unter der neuen Gruppe **Fahrzeug Integration (Bluelink)** kannst du nun jederzeit:
* Den `refresh_token` erneuern.
* Die `bluelink_vin` (Fahrgestellnummer) eintragen, falls du mehrere Autos hast.
* Die `bluelink_interval` (Refresh-Rate in Minuten) anpassen. Wir empfehlen **15 bis 30 Minuten**, um die 12V-Batterie des Autos nicht unnötig durch ständiges Aufwecken zu entladen.

---

## Weg 2: EVCC (Über MQTT)
Wenn du EVCC bereits nutzt, kann dieses System den Auto-SoC an E3DC-Control senden.

**Voraussetzung:** Ein MQTT-Broker. Du kannst den eingebauten Broker im Installer über **Menüpunkt 106 (Lokalen MQTT-Broker installieren)** aktivieren.

**Einrichtung:**
1. Trage in deiner `evcc.yaml` den MQTT-Broker ein:
   ```yaml
   mqtt:
     broker: DEINE_E3DC_PI_IP:1883
     topic: evcc
   ```
2. Öffne den **Config-Editor** im E3DC Web-Dashboard.
3. Gehe zur Gruppe **Smart Home MQTT-Hub**.
4. Trage bei `mqtt_hub_sub_soc_topic` das korrekte Topic deines Autos ein.
   *(Beispiel: `evcc/vehicles/db:4/soc` oder `evcc/loadpoints/1/vehicleSoc`)*

Wichtig: `evcc/loadpoints/1/chargePower` ist die Ladeleistung der Wallbox und gehoert in den Bereich **Wallbox-Leistung per MQTT** (`wb_topic`), nicht in das SoC-Feld.

---

## Weg 3: Home Assistant (Über MQTT)
Wenn dein Auto bereits in Home Assistant integriert ist, kannst du eine einfache Automation erstellen, die den SoC an E3DC-Control sendet.

**Einrichtung:**
1. Erstelle in HA eine Automation, die bei Änderung des Auto-Sensors (`sensor.mein_auto_battery_level`) auslöst.
2. Sende als Aktion den neuen Zustand (als Zahl) an ein frei wählbares MQTT-Topic (z.B. `e3dc/set/car_soc`).
3. Trage genau dieses Topic in deinem E3DC Web-Dashboard im Config Editor unter `mqtt_hub_sub_soc_topic` ein.

---

## Weg 4: Manuelle Web-Eingabe (Ohne Cloud-Integration)
Wenn dein Fahrzeug über keine App oder offene Cloud-Schnittstelle verfügt, kannst du dennoch vom intelligenten Lademanagement (und V2H-Schutz) profitieren!

**Einrichtung & Ablauf:**
1. Wechsle im Dashboard-Menü auf **Wallbox**.
2. Oben in **Fahrzeugzuordnung** wählst du pro Wallbox das passende Fahrzeug. Die Auswahl wird automatisch gespeichert; ein separater Button für die Zuordnung ist nicht mehr nötig.
3. Bevor oder während du das Fahrzeug ansteckst, liest du den aktuellen Ladestand in Prozent (%) im Auto ab, wählst dein Fahrzeugprofil und trägst den SoC bei der passenden Wallbox ein. Klicke dann auf **SoC setzen**.
4. **Magie:** Das Dashboard speichert diesen Wert. Der im Hintergrund laufende *Energy Manager* registriert, wie viel Energie (kWh) exakt in das Auto fließt, und **kalkuliert (interpoliert) den SoC in Echtzeit automatisch mit!**
5. Im Dashboard siehst du den errechneten SoC (inkl. 2 Nachkommastellen) mit dem Zusatz `(Manuell)` sowie die dynamische Restladezeit.
6. **Aufgeräumte UI:** In der Fahrzeug-Detailansicht werden für diese manuell berechneten Autos automatisch alle irrelevaten Cloud-Werte (wie 12V Batterie, GPS Standort, Türen & Klappen) komplett ausgeblendet.

---

## Flotten-Management (Multi-EV Support) & Dual-Wallbox UI
Das System ist für den Betrieb mehrerer Fahrzeuge optimiert. Wenn du z. B. zwei Fahrzeuge über MQTT (`mqtt_hub_sub_soc_topic` und `mqtt_hub_sub_soc_topic_2`) anbindest:
*   **DUAL-WALLBOX UI:** Im Dashboard (Seitenleiste und Energiefluss-Diagramm) ordnet das System nun das erste angesteckte Fahrzeug optisch vollautomatisch der "Wallbox 1" zu und das zweite der "Wallbox 2" – perfekt für das Monitoring paralleler Ladevorgänge.
*   **DASHBOARD-TABS:** Das System erkennt die verschiedenen Fahrzeuge (`bluelink_vin` oder MQTT-Topics) und erstellt automatisch für jedes Auto einen eigenen Reiter (Tab) in der Detailansicht.
*   **WALLBOX-ZUORDNUNG:** Im Hauptmenüpunkt "Wallbox" kannst du oben in einer eigenen Karte exakt konfigurieren, welches Fahrzeug an Wallbox 1 oder 2 lädt. Die Auswahl wird sofort gespeichert und ist besonders wichtig für Wallboxen ohne eigene Fahrzeugerkennung.
*   **AUTOMATISCHE PLANUNG:** Änderungen am Ladeziel, die du direkt in der App deines Autos machst, werden vom System zeitnah erkannt und lokal synchronisiert.
