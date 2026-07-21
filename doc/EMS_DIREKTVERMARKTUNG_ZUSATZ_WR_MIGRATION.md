# Direktvermarktung: Zusatzwechselrichter bei Update und Rollback

Diese Betriebsinformation beschreibt, wie die Konfiguration eines über Shelly
geschalteten Zusatzwechselrichters bei Update, Wiederherstellung und Rollback
erhalten bleibt.

## Konfigurationsvertrag

Die aktuelle Konfiguration verwendet die Schlüsselgruppe
`direct_marketing_aux_inverter_shelly_*`. Ältere Installationen können noch die
vorherige Schlüsselgruppe enthalten. Der Installer übernimmt einen vollständigen
Altvertrag einmalig über eine SHA-256-gebundene Erkennung. Nach erfolgreicher,
atomarer Übernahme werden ausschließlich die neutralen Schlüssel fortgeführt;
es gibt weder Dual-Write noch einen lesbaren Alt-Alias in Runtime, API oder UI.

Die zusammengehörigen Werte werden immer als eine Einheit behandelt:

- Steuerungsübernahme (`override`);
- Geräteadresse (`ip`);
- Relaislogik (`invert`);
- Freigabe der dynamischen Entsperrung (`dynamic_unblock_enable`);
- Leistungsschwelle der dynamischen Entsperrung (`unblock_threshold_w`).

Unvollständige, ungültige oder widersprüchliche Werte werden nicht vermischt.
Das System wechselt dann auf den sicheren lokalen Modus, deaktiviert die
dynamische Entsperrung und sendet keinen Schaltbefehl. Ein Update bricht in
diesem Zustand vor Rechte- und Serviceänderungen fail-closed ab. Es gibt keine
automatische Reparatur durch bloßes Speichern in der Weboberfläche: Der Vertrag
muss anhand des verifizierten Backups administrativ vollständig und eindeutig
aufgelöst werden, bevor das Update erneut gestartet wird.

## Verhalten beim Update

Vor einer Konfigurationsmigration wird das geschützte Betriebsbackup im
dedizierten Verzeichnis `data/config_backups/aux_inverter_migration` erstellt.
Dieses Verzeichnis bleibt auf Modus 0700 und seine regulären Dateien auf 0600;
die allgemeinen, von der authentifizierten Weboberfläche verwalteten
Konfigurationsbackups behalten ihren bisherigen Gruppenvertrag. Erst danach
werden die zusammengehörigen Werte geprüft und atomar in die aktive
Konfiguration übernommen. Ein Abbruch darf weder eine teilweise geschriebene
Konfiguration noch einen neuen Hardwareauftrag hinterlassen.

Vorhandene Manual-Locks, Guard-Zeiten und Laufzeitzustände werden konservativ
zusammengeführt:

- Ein manueller Zustand „Wechselrichter AUS“ hat Vorrang.
- Die späteste bekannte Schutzfrist bleibt maßgeblich.
- Eine noch laufende 600-Sekunden-Sperre wird durch Update oder Neustart nicht
  verkürzt.
- Widersprüchliche Relaiszustände sperren neue Schaltbefehle, bis der Zustand
  wieder eindeutig bestätigt ist.

Die konfigurierte NC-/NO-Relaislogik bleibt erhalten.

## Anzeige und Bedienung

Die Zusatz-PV-Anzeige ist in Desktop- und Mobilansicht verfügbar. Sichtbarkeit,
gemessene Leistung, Relaiszustand, manuelle Bedienbarkeit und externes
Einspeiselimit werden getrennt bewertet. Dadurch bleibt die Anlage sichtbar,
auch wenn eine manuelle Schaltaktion aus Sicherheitsgründen gesperrt ist.

Die Oberfläche zeigt keine Geräteadresse in Diagnose- oder Statusmeldungen an.
Sie erzeugt außerdem keinen eigenen Regelpfad: Alle Schaltentscheidungen bleiben
beim zuständigen Runtime-Owner.

## Wiederherstellung und Rollback

Eine Wiederherstellung übernimmt Konfiguration, Manual-Lock, Guard-Zustand und
die zugehörigen persistenten Laufzeitdaten gemeinsam. Nach der Wiederherstellung
müssen folgende Punkte geprüft werden:

1. Zusatzwechselrichter wird in der Oberfläche angezeigt.
2. Relaislogik entspricht der Installation.
3. Ein vorhandener Manual-Lock ist weiterhin aktiv.
4. Eine laufende Schutzfrist wurde nicht verkürzt.
5. Bei unklarem Zustand bleibt die Schaltfunktion gesperrt.

Der eingefrorene Rollback-Stand v5.3.2b versteht bereits den neutralen
Konfigurationsvertrag. Deshalb bleibt die aktive kanonische Konfiguration bei
einem Rückfall von v5.4.0 auf v5.3.2b direkt lesbar; ein spezieller
Alt-Schlüssel-Restore existiert nicht. Der Releasewechsel erstellt weiterhin
ein verifiziertes Systembackup. Ein Rollback ohne lesbares,
prüfsummengesichertes Systembackup wird abgebrochen.

## Störungsbehebung

Wenn der Zusatzwechselrichter nach einem Update nicht steuerbar ist:

1. Konfiguration in der Weboberfläche öffnen und die fünf zusammengehörigen
   Angaben prüfen.
2. Relaislogik, Geräteadresse und Leistungsschwelle mit der realen Installation vergleichen.
3. Manual-Lock und angezeigte Schutzfrist prüfen.
4. Bei widersprüchlichem Status keine manuelle Umgehung erzwingen.
5. Das redigierte Diagnosepaket aus dem Installationscenter verwenden; rohe
   Konfigurationen oder Logs nicht öffentlich weitergeben.

Die dynamische Entsperrung bleibt ohne ausdrückliche Aktivierung ausgeschaltet.
