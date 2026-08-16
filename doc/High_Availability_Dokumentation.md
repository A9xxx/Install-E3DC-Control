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
2. Wähle im Menü "Erweiterungen" -> **"High Availability (Cluster) einrichten"**.
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

Alle Parameter können komfortabel im **Config Editor** unter der Kategorie **High Availability (Cluster)** angepasst werden.

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
*   **Auto-Failover AUS (Warm Standby):** Der Slave sichert nur die Daten des Masters. Fällt der Master aus, erhältst du nur eine Warnung, aber der Slave greift nicht ein. Du kannst ihn im Notfall manuell zum Master befördern (im Config Editor).

### Auto-Recover
Wenn diese Option aktiviert ist, zieht sich der Master nach jedem gewöhnlichen Neustart (z.B. nach einem System-Update) die neuesten Diagramm-Daten aus dem Standby-Slave. Dies schließt winzige Lücken im Live-Graphen.

---

## 5. Tipps für Updates im Cluster-Betrieb

Um bei manuellen Software-Updates keinen Fehlalarm oder ein ungeplantes Failover auszulösen, befolge diese einfache Reihenfolge:

1.  Aktualisiere **immer zuerst den Slave (Standby)** über das Web-Interface.
2.  Aktualisiere **danach den Master (Aktiv)**. 

*Erklärung:* Da der Master während des Updates für 1-2 Minuten offline ist, wird der Slave den Countdown starten. Da das Timeout aber auf 15 Minuten steht, wird der Zähler beim Wiederhochfahren des Masters einfach wieder auf Null gesetzt. Du bleibst also geschützt.
