# Enterprise High Availability Cluster (HA)

Dieses Dokument beschreibt die Einrichtung und Funktionsweise des E3DC-Control Failover-Clusters. 
Dieses System ermöglicht den Betrieb eines zweiten Raspberry Pis als "Hot Standby" oder "Warm Standby", um im Falle eines Hardware- oder Netzwerkausfalls des Haupt-Systems nahtlos die Kontrolle über die Ladesteuerung und die Wärmepumpe zu übernehmen.

---

## 1. Architektur (Master / Slave)

Das Cluster-System basiert auf einer **Aktiv/Passiv (Master/Slave)** Architektur.

*   **Der Master (Aktiv):** Steuert im Normalfall die Anlage (E3DC und Wärmepumpe). Er sichert fortlaufend im Hintergrund seine Konfiguration, seine Ramdisk-Daten und die E3DC-Historie über das Netzwerk (`rsync` via `ssh`) auf den Slave.
*   **Der Slave (Standby):** Befindet sich im "Schlafmodus". Alle steuernden Dienste (wie `e3dc` oder `energy_manager`) sind gestoppt. Er überwacht den Master durch regelmäßige Ping-Signale (Heartbeat).

---

## 2. Einrichtung (In 3 Schritten)

### Voraussetzungen
*   Zwei Raspberry Pis im selben Netzwerk.
*   Beide müssen statische IP-Adressen besitzen.
*   Auf beiden muss E3DC-Control vollständig installiert sein.

### Schritt 1: Zertifikatstausch (Installer)
Damit der Master seine Daten automatisch zum Slave kopieren kann, müssen sich die Geräte vertrauen.
Setze auf jedem System den zuvor geprüften absoluten Produktpfad als
`E3DC_INSTALL_PATH`.

1. Starte auf dem **Master** den Installer (`bash "$E3DC_INSTALL_PATH/e3dc-setup"`).
2. Wähle im Hauptmenü **`7) Expertenmenü`** und dort unter „Erweiterungen & Smart Home“ den Punkt **`49) High Availability (Cluster)`**.
3. Wähle die Rolle `1` (Master).
4. Gib die IP-Adresse des **Slaves** ein und bestätige das Passwort des Slaves. Das System tauscht nun die SSH-Keys aus.
5. Wiederhole den exakt gleichen Vorgang auf dem **Slave**, wähle dort aber Rolle `2` (Slave) und gib die IP des Masters ein.

### Schritt 2: Dashboard Kontrolle
Nach der Installation erscheint in der Web-Oberfläche beider Systeme ganz oben ein Cluster-Status-Schild. 
*   Auf dem Master sollte es grün leuchten: `[Master (Sync OK)]`
*   Auf dem Slave sollte es grau leuchten: `[Standby]`

---

## 3. Die Cluster-Ereignisse

### 🚨 Der Failover (Ausfall des Masters)
Wenn der Master nicht mehr auf Ping-Anfragen reagiert, beginnt der Slave einen Countdown (Standard: 15 Minuten). Das Verhindert, dass kurze Reboots (z.B. nach einem Update) als Ausfall gewertet werden.

Nach Ablauf des Countdowns passiert Folgendes:
1.  **Alarmierung:** Der Slave sendet eine rote Telegram-Nachricht: *"🚨 E3DC FAILOVER: Master ist offline! Backup-Pi übernimmt."*
2.  **Aktivierung:** Der Slave startet blitzschnell seine lokalen Dienste (`e3dc` und `energy_manager` erwachen aus dem Standby). Da er durch den Master fortlaufend mit den historischen Daten gefüttert wurde, setzt er exakt dort an, wo der Master stehen geblieben ist.
3.  **Dashboard:** Das Status-Schild auf dem Slave blinkt rot: `[FAILOVER]`.

### ✅ Das Failback (Rückkehr des Masters)
Wenn du das Problem am Master-Pi behoben hast (z.B. neues Netzteil oder Kabel) und ihn wieder einschaltest, passiert die Rückgabe automatisch und sicher:

1.  Der Master fährt hoch. Durch den "Auto-Recover" Modus merkt er, dass er offline war, stoppt sofort seine Dienste und zieht sich die frisch gesammelten Daten vom aktiven Slave zurück (um Datenlücken zu verhindern).
2.  Der Slave bemerkt, dass der Master wieder da ist.
3.  Der Slave stoppt sofort seine eigene Steuerung, spiegelt den letzten Rest seiner Daten zum Master und versetzt den Master wieder in den Arbeitsmodus.
4.  **Alarmierung:** Du erhältst eine grüne Telegram-Nachricht: *"✅ E3DC FAILBACK: Master ist wieder da!"*

---

## 4. Konfiguration & Features

Die Cluster-Parameter wie Ausfall-Timeout, Sync-Intervall und automatisches Failover lassen sich im **Config Editor** unter der Kategorie **HA Master/Slave Cluster** anpassen. Die Rolle und den Partner richtet dagegen der Installer ein (Schritt 1): Das Rollenfeld im Config-Editor ändert nur die Konfiguration, nicht die im System hinterlegte Rolle, und genügt für einen Rollenwechsel nicht.

### Konfiguration einspielen und Ersatzhardware
Import und Rollback behalten Rolle, Partner und Gerätenamen eines HA-Knotens bei; die HA-Rolle aus der Datei wird nicht übernommen. Maßgeblich für Ersatzhardware und Rollenwechsel ist die folgende Tabelle. Eine bestandene Rollenprüfung bestätigt weder laufende Dienste noch die tatsächliche Hardwaresteuerung. Die fertig zusammengeführte Konfiguration darf beim Import, Rollback und normalen Speichern höchstens **64 KiB (65.536 Byte)** groß sein, einschließlich Formatierung und abschließendem Zeilenumbruch. Größere Dateien würden die Dienste nicht lesen; deshalb wird das Speichern vor jeder Änderung abgelehnt.

### Rolle und Rollenanker

Vor jeder Änderung klären, welcher Knoten tatsächlich regelt. Ein ausgefallener oder nicht erreichbarer Partner ist **nicht** automatisch stillgelegt. Stilllegung bedeutet: keine Hardware-Schreibmöglichkeit und kein unkontrollierter Wiederanlauf, beispielsweise physisch ausgeschaltet und gegen Wiedereinschalten gesichert. Bei unklarer Zuständigkeit keine weitere Regelinstanz freigeben. Rolle oder Rollenanker nicht ändern, um eine Sperre zu umgehen – sonst könnten zwei Anlagen denselben Speicher regeln.

Der Installerweg **„7) Expertenmenü“ → „49) High Availability (Cluster)“** ist für Master/Slave vorgesehen. Er benötigt eine funktionierende SSH-Verbindung zum Partner und startet den HA-Dienst; er ist kein reiner Einstellungsdialog. Er bietet weder Shadow noch einen Rückbau auf `off` an. Eine automatische Failover-/Failbackbeschreibung ersetzt keinen Nachweis der sicheren Übergabe im konkreten Zustand.

| Situation | Sicherer Weg | Vorbedingung | Auf keinen Fall |
|---|---|---|---|
| Ersatzhardware für eine Einzelanlage | Altgerät stilllegen, Ersatz als Einzelanlage mit passender lokaler Rollenbindung einrichten, Sicherung importieren und Rollenprüfung sowie Betrieb getrennt kontrollieren. | Kein alter Regler und kein aktiver HA-Partner für denselben Speicher; Gerätezuordnung geklärt. | Altes und neues Gerät gleichzeitig regeln lassen oder fremden Anker ungeprüft überschreiben. |
| Ersatz eines Masters bei laufendem Partner | Altmaster gegen Rückkehr sichern, aktuellen Owner feststellen. Ersatz ohne zusätzliche Hardwareautorität vorbereiten; Master/Slave über den Installer nur innerhalb einer geprüften Übergabe einrichten. Bei ungeklärter Übergabe Unterstützung im Forum anfragen. | Alter Master stillgelegt, Partner erreichbar, genau ein aktiver Owner und bestätigter Übergabeweg. | Ersatzmaster durch Dateiimport freigeben oder „Einzelbetrieb ohne HA“ wählen, während der Partner regelt. |
| Ersatz eines Slaves bei laufendem Master | Alten Slave gegen Rückkehr sichern, Ersatz über den Installer als Slave mit dem Master verbinden, Sicherung importieren und Standby prüfen. | Master eindeutig aktiv und per SSH erreichbar; Ersatz übernimmt nicht unkontrolliert. | Ersatz vorübergehend als Master oder Einzelanlage betreiben. |
| Partner ausgefallen oder unerreichbar | Netzpartition von Ausfall unterscheiden und alten Partner sicher stilllegen. Für den Wiederaufbau einen erreichbaren Partner oder einen gesondert geprüften Wiederherstellungsweg benötigen; derzeit kein geführter Weg ohne Partner, Unterstützung im Forum anfragen. | Rückkehr eines früheren Writers ausgeschlossen; einziger künftiger Owner festgelegt. | Nichterreichbarkeit als Stilllegung ansehen oder Partner-IP zur Umgehung leeren. |
| Anker fehlt | Herkunft und beabsichtigte Rolle prüfen. Nur bei eindeutiger Rollenbindung kann eine bewusst freigegebene Systemreparatur den Anker neu binden; danach Rolle, Peer, Host und Zulassung prüfen. | Widerspruchsfreie Rollenquellen, kein zweiter Writer; Reparaturumfang akzeptiert. | Fehlenden Anker automatisch als Einzelanlage deuten oder `off` erzwingen. |
| Anker beschädigt | Schaden und Rollenquellen prüfen; Reparatur nur bei rekonstruierbarer Rolle und bekanntem Owner. Bei Widerspruch Unterstützung im Forum anfragen. | Rolle unabhängig vom beschädigten Anker belegt. | Inhalt als „keine HA-Rolle“ behandeln oder bis zum grünen Status Rollen raten. |
| Anker unlesbar | Ursache (Leserechte, Dateiform, Eigentümer oder Beschädigung) prüfen lassen; Rolle und Owner unverändert lassen. | Geklärte Ursache und sichere Rollenbindung. | Datei löschen, allgemein schreibbar machen oder Schutzrechte absenken. |
| Anker von anderem Gerät | Herkunft des Mediums klären; bei Umbenennung oder Ersatz die Rückkehr des Ursprungsgeräts ausschließen. Neue Rollenbindung nur nach geklärter Gerätezuordnung. | Altgerät und möglicher HA-Partner berücksichtigt; kein paralleler Writer. | Reparatur einer Kopie einer weiterlaufenden Anlage als Standardweg nutzen. |
| Rollenfeld und Rollenanker widersprechen sich | Quellen und tatsächlichen Owner abgleichen; derzeit kein allgemeiner geführter Korrekturweg, Unterstützung im Forum anfragen. | Eindeutige Zielrolle, Gerätezuordnung und sicherer Übergabeweg. | Rollenfeld oder Anker nur ändern, damit die Sperre verschwindet. |
| Shadow einrichten | Separaten Bare-Metal-Snapshotpfad gemäß Shadow-Dokumentation einrichten. Für einen nicht belegten Rollenankerwechsel gibt es derzeit keinen geführten Weg; Unterstützung im Forum anfragen. | Aktive Anlage alleiniger Hardwareowner; keine aktiven Writer auf der Shadow. | Menü 49 als Shadow-Installer ansehen oder Shadow bei Partnerausfall aktivieren. |
| Rückkehr vom HA-Paar zur Einzelanlage | Anderen Knoten dauerhaft stilllegen; HA-Dienst, Rolle und Anker innerhalb eines geprüften Rückbaus konsistent umstellen. Derzeit kein geführter Rückbauweg; Unterstützung im Forum anfragen. | Kein Wiederanlauf des anderen Knotens; HA-Lease und Dienste berücksichtigt. | Das Importkästchen als HA-Deinstallation verwenden oder nur Rolle/Peer ändern. |
| HA-Sicherung für Einzelbetrieb importieren | Erst anderen Knoten des früheren Paars und ersetztes Altgerät sicher stilllegen. Nur im angebotenen Sonderfall „Für Einzelbetrieb ohne HA importieren/wiederherstellen“ bestätigen; Rollenprüfung danach beachten. | Wirklich Einzelbetrieb beabsichtigt; alle konkurrierenden Writer ausgeschlossen. | Kästchen für ein weiter betriebenes HA-Paar nutzen oder als Ankerreparatur ansehen. |
| Docker-Ziel mit HA-/Shadow-Sicherung | Docker nur mit `ha_mode=off`; Master/Slave/Shadow benötigen Bare Metal. Einzelimport nur mit den obigen Stilllegungsbedingungen und unveränderten Importgates. | Kein parallel steuernder Hostprozess oder Container. | Sperren umgehen oder einen zusätzlichen Container als passiven Ersatz annehmen. |

**„System reparieren“ ist kein reiner Ankerknopf:** Die Aktion umfasst Backup, Stable-Abgleich, Rechteänderungen und Dienstneustarts. Bei fremdem oder widersprüchlichem Anker ist sie keine allgemeine Reparaturempfehlung. Eine Kartenkopie kann den alten Hostnamen behalten; „Anker passt“ allein schließt einen parallelen Klon nicht aus.

Das nicht vorausgewählte Kästchen bestätigt den **anderen Knoten des früheren HA-Paars**, nicht das gerade ersetzte Altgerät. Ist dieser Knoten aktiv, vorübergehend ausgeschaltet oder nur unerreichbar, darfst du es nicht verwenden. Auch das ersetzte Gerät darf nicht weiterregeln. Die Bestätigung schaltet kein Gerät ab und repariert keinen Rollenanker.

### Smart Config-Sync (Schutz vor Split-Brain)
Wenn du am Master im Config-Editor Einstellungen veränderst (z.B. ein neues Preislimit setzt), wird diese Änderung **automatisch** binnen 60 Sekunden auf den Slave übertragen.
*Die Intelligenz:* Das System überträgt nur generelle Einstellungen. Die Cluster-spezifischen Variablen (Rolle, IP-Adresse) des Slaves werden bei der Synchronisation geschützt, um ein Chaos (Split-Brain-Syndrom) zu verhindern.

Konfigurations- und Matter-Geheimnisse bleiben lokal pro Knoten. Der Master
stellt für den Slave nur eine gefilterte Konfiguration ohne Passwörter,
API-Tokens, private Schlüssel und `web_pin` bereit; der Slave mischt seine
eigenen lokalen Werte wieder ein. Für ein echtes Failover müssen die benötigten
Zugangsdaten und die Web-PIN deshalb auf beiden Geräten einmal lokal
hinterlegt sein.

Folgende Pfade werden weder per Push noch Pull übertragen und von der
allgemeinen Rechteprojektion nicht verändert:

- `ramdisk/matter_pairing.json` einschließlich der temporären Schreibdatei,
- `ramdisk/e3dc_config_cache.json` einschließlich seiner atomaren
  Schreibdatei `.e3dc_config_cache.*`,
- `data/matter-storage`,
- `data/config_backups`,
- `data/e3dc.config.txt`,
- `data/e3dc_v4.json.tmp`, `data/e3dc_v4.json.bak*` und die atomaren
  Schreibdateien `data/.e3dc_v4_*`,
- `ramdisk/rule_calm_analysis.json`, `ramdisk/watchdog.update_pause` und
  `ramdisk/watchdog.update_grace`,
- `data/.wallbox_plan_jobs`.

Der V4-Laufzeitcache enthält eine Projektion der Konfiguration und folgt deren
Schutzmodus: `0660` im Standardmodus oder `0664` im ausdrücklich gewählten
Kompatibilitätsmodus. Matter-Fabric, Pairingdatei und Commissioning-Zugangsdaten
bleiben ebenfalls knotenlokal. Eine Matter-Kopplung bleibt bei Updates
desselben Knotens erhalten; ein Standby-Knoten muss bei Bedarf separat
gekoppelt werden.

Reguläre synchronisierte Daten behalten den gemeinsamen HA-Vertrag. Dazu
gehört bewusst auch `data/e3dc_stats.db` einschließlich gespeicherter
WebPush-Abonnements. Die Aussage „knotenlokal“ gilt daher ausdrücklich für
Konfigurations- und Matter-Geheimnisse, nicht pauschal für jede Datei mit
Zugangsdatencharakter. Kurzlebige atomare Schreibdateien der Wallbox-
Sessionaggregation und der Live-JSON-Projektion werden ebenfalls nicht
übertragen oder umgehärtet; ihre regulären Zielartefakte bleiben Teil des
HA-Abgleichs. Die Pull-Härtung folgt keinen Symlinks und überschreitet keine
Dateisystemgrenze.

> **Wichtig für bestehende HA-Installationen:** Der Abgleich arbeitet ohne
> `--delete`. Neue Ausschlüsse verhindern weitere Übertragungen, entfernen aber
> keine bereits auf den Partner kopierten Dateien. Wenn HA schon vor 5.4.3i
> aktiv war, prüfe beide Knoten auf alte Kopien. Entferne sie erst nach klarer
> Zuordnung und Sicherung des weiterhin benötigten Originals. Waren
> Konfigurations- oder Matter-Geheimnisse auf dem jeweils anderen Knoten
> vorhanden, rotiere die betroffenen Zugangsdaten beziehungsweise die Web-PIN
> und kopple Matter bei Bedarf neu.

### Hot Standby vs. Warm Standby
Du kannst bestimmen, wie aggressiv der Slave eingreifen soll (`Auto-Failover`):

*   **Auto-Failover AN (Hot Standby):** Der Standard. Der Slave übernimmt bei Ausfall vollautomatisch.
*   **Auto-Failover AUS (Warm Standby):** Der Slave sichert nur die Daten des Masters. Fällt der Master aus, erhältst du nur eine Warnung, aber der Slave greift nicht ein. Die Rolle eines Knotens wechselt nur der Installer (`e3dc-setup`, Hauptmenü `7) Expertenmenü` → `49) High Availability (Cluster)`, dort Rolle `1` (Master)); das Rollenfeld im Config-Editor genügt dafür nicht. Der Installer richtet dabei auch die SSH-Verbindung zum Partner ein und bricht ab, wenn dieser nicht erreichbar ist. Bei einem ausgefallenen, nicht erreichbaren Master lässt sich der Slave auf diesem Weg derzeit nicht zum Master machen; siehe „Rolle und Rollenanker“, Zeile „Partner ausgefallen oder unerreichbar“.

### Auto-Recover
Wenn diese Option aktiviert ist, zieht sich der Master nach jedem gewöhnlichen Neustart (z.B. nach einem System-Update) die neuesten Diagramm-Daten aus dem Standby-Slave. Dies schließt winzige Lücken im Live-Graphen.

---

## 5. Tipps für Updates im Cluster-Betrieb

Um bei manuellen Software-Updates keinen Fehlalarm oder ein ungeplantes Failover auszulösen, befolge diese einfache Reihenfolge:

1.  Aktualisiere **immer zuerst den Slave (Standby)** über das Web-Interface.
2.  Aktualisiere **danach den Master (Aktiv)**. 

*Erklärung:* Da der Master während des Updates für 1-2 Minuten offline ist, wird der Slave den Countdown starten. Da das Timeout aber auf 15 Minuten steht, wird der Zähler beim Wiederhochfahren des Masters einfach wieder auf Null gesetzt. Du bleibst also geschützt.
