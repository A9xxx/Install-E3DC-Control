# 📘 Changelog

Dieser Changelog dokumentiert die nutzerrelevante Produktgeschichte aller veröffentlichten Versionen. Personen-, Anlagen- und interne Entwicklungsbezüge wurden eng anonymisiert; technische Änderungen und ihre Nutzerwirkung bleiben erhalten.

## 🙏 Danksagung

Danke an die Community für Rückmeldungen, Praxiserfahrungen und die gemeinsame Weiterentwicklung. Historische Einzelzuordnungen werden in diesem bereinigten Changelog nicht geführt.

## [5.4.0] – 2026-07-21

### 🔋 Speicher und Direktvermarktung

- 🛡️ **Sicherheit:** Pro Domäne und Aktor gibt es genau einen Regel-Owner. Plan, Slot, Marktfenster, Freigabe, Geräteanforderung und Rücklesung bleiben über dieselbe Entscheidung gebunden.
- ⚙️ **Regelung:** Interne DC-PV und zusätzliche AC-Erzeuger werden getrennt bilanziert. DC- und Netzpunktdruck werden mit dem größeren Wert bewertet und nicht doppelt addiert.
- 🧱 **Stabilität:** Ungültige oder veraltete Markt-, Anlagen- und Rücklesedaten führen zu einer inaktiven Freigabe. Diagnosekandidaten können keine Hardwarewirkung erhalten.
- 🛡️ **Sicherheit:** Netzstrom-Arbitrage bleibt in 5.4.0 wirkungslos. Bestehende Altwerte werden kompatibel erhalten, erzeugen aber weder Owner noch Speicherbefehl.
- 🛡️ **Sicherheit:** Notstromreserve, Gerätegrenzen und dauerhafte RSCP-Einstellungen bleiben von Optimierung und Update unberührt.

### 🔌 Wallboxen und Fahrzeuge

- 🐛 **Fehlerbehebung:** Eine angesteckte und freigegebene openWB Pro verwirft abgelaufene eigene Phasenreservierungen und veraltete Nullanker. Nach bestätigter Bereitschaft wird die positive Startfreigabe ohne Umstecken oder Manager-Neustart erneut projiziert.
- ⚙️ **Regelung:** Das Mehr-Wallbox-Balancing verwendet die tatsächlichen L1/L2/L3-Stromvektoren, die reale Phasenzahl, Fahrzeug- und Ladepunktgrenzen sowie die Netzpunktreserve. Ein- und dreiphasige Amperewerte werden nicht pauschal addiert.
- 🧱 **Stabilität:** Die ruhige PV-Kurve folgt dem nachhaltigen PV- und Ladekurvenbudget mit Hysterese und Mindestlaufzeit. Eine bereits laufende Ladung darf kurze Einbrüche mit höchstens 75 Wh Batteriestützung überbrücken; Kaltstart und Phasenwechsel werden nicht aus dem Speicher finanziert.
- 🛡️ **Sicherheit:** Geschützte openWB-Phasenwechsel setzen zuerst 0 A, übergeben anschließend das Phasenziel an `phasetarget` und warten mindestens 480 Sekunden sowie auf frische, bestätigte Rückmeldungen. E3DC-Control sendet dabei keinen zweiten CP-Befehl. Direkte E3/DC-Sun-/Auto-/Abort-, Maximalstrom- und native Phasenbefehle bleiben gesperrt.
- 🔄 **Kompatibilität:** Der bestätigungsgebundene WBchar6-Pfad für vorhandene E3/DC-Wallboxen sowie openWB, openWB Pro und go-e bleiben unterstützt. Ein ausdrücklich deaktivierter Ladepunkt bleibt im Nur-Status-Betrieb.

### ♨️ Wärme und iDM

- 🛡️ **Sicherheit:** Wallboxaktionen oder der Verlust eines Wallboxkontexts stoppen keine bereits laufende Wärmepumpe eigenständig. Hardwarebefehle bleiben an frische, treiberspezifische Rückmeldungen gebunden.
- 🔎 **Diagnose:** Der manuelle iDM-Scanner liest Input-Register 1006 genau einmal per FC04. Ohne passend gebundenes Modell, Protokoll, Firmware und Unit-ID bleibt der Rohwert unbewertet; der Scanner schreibt keine Register.
- 🧱 **Stabilität:** Teilweise bestätigte Schreibfolgen gelten als Fehler. Die Steuerung fällt ausschließlich auf einen bestätigten sicheren Zustand zurück.

### 🖥️ Weboberfläche und Diagnose

- ✨ **Verbesserung:** Desktop- und Mobile-Layout besitzen getrennte Revisionen. Gleichzeitige Änderungen werden erkannt, unbekannte Felder bleiben erhalten und Tablet-/Querformatansichten ordnen Energiequellen und Verbraucher ohne abgeschnittene Badges an.
- 🔎 **Diagnose:** Regelruhe, Owner, Freigabe, ACK, Readback und Hardwarewirkung werden getrennt ausgewiesen. Fehlende Historie wird als Beweisgrenze statt als vermeintliche Ruhe behandelt.

### 📦 Installation, Update und Distribution

- 🛡️ **Sicherheit:** Update, Backup, Rollback und Web-Planung arbeiten transaktional. Unvollständige Sicherungen, Timeouts und Teilfehler brechen ab und erhalten den letzten konsistenten Konfigurations- und Dienstzustand.
- 🔐 **Datenschutz:** Lokale Konfigurationen, Sicherungen, Diagnosen, Zugangsdaten und Repository-Historie sind aus dem Docker-Buildkontext ausgeschlossen.
- 🔄 **Migration/Kompatibilität:** Einziger vorgesehener öffentlicher Rückfallstand ist der in `UPDATE_POLICY.json` exakt gebundene Release-/Rollback-Tag `v5.3.2b`.

## [5.3.2b] – 2026-07-15

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Der fachliche Produktstand von 5.3.2a bleibt unverändert; Versions-, Aktualisierungs- und Rückfallhinweise wurden auf 5.3.2b fortgeschrieben.
- 🛡️ **Sicherheit:** Personen-, Anlagen- und private Betriebsbezüge wurden in öffentlichen Texten und Beispielen neutralisiert, ohne die Produktfunktionen zu verändern.

## [5.3.2a] – 2026-07-11

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Änderungen von Leistungsgrenzen gelten erst nach einer passenden Geräterückmeldung als übernommen. Eine garantierte Wiederaufnahme nach einer 0-W-Begrenzung wird nicht behauptet.
- 🧱 **Stabilität:** Abweichende Rückmeldungen lösen einen kontrollierten, begrenzten Wiederholungsversuch aus.
- 🔎 **Diagnose:** Vorgabe, Geräteantwort und anschließend gelesener Istzustand werden getrennt ausgewiesen.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Reservierte Leistung bleibt während eines Wallbox-Phasenwechsels für Wärmeverbraucher erhalten.
- 🧱 **Stabilität:** Kurze Ladeunterbrechungen werden korrekt eingeordnet und lösen keine konkurrierenden Neustarts aus.

### 📈 Direktvermarktung und Strompreise

- ⚙️ **Regelung:** Viertelstundenpreise werden lückenlos verarbeitet; aktive Eingriffe bleiben auf belastbare Day-Ahead-Preise beschränkt.
- 🛡️ **Sicherheit:** Tarifpreise und Erlöse aus Direktvermarktung bleiben getrennt. Laden, Entladen und Speicherplatzschaffen erfolgen nur bei tatsächlichem Bedarf.
- 🔎 **Diagnose:** Absicht, Ausführung und bestätigte Wirkung werden getrennt dargestellt.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Direktvermarktung, tatsächlich vermarktete Energie und die beteiligten PV-Quellen werden nachvollziehbarer dargestellt.
- 🛡️ **Sicherheit:** Der Status einer 0-W-Begrenzung ist sichtbar; neutrale Beispiele ersetzen frühere Anlagen- und Personenbezüge.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Klassische und moderne Ansicht, bestehende Smart-Home-Kopplungen und die Nur-Lese-Vergleichsinstanz bleiben unterstützt.
- 🔄 **Migration/Kompatibilität:** Der direkte Rückfall auf 5.3.2 bleibt vorgesehen; experimentelle Folgefunktionen gehören nicht zu diesem Stand.

## [5.3.2] – 2026-07-10

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Automatik-Freilauf sendet keinen 0-W-Befehl.
- 🐛 **Fehlerbehebung:** Leistungsbudget-Rückfall bleibt netzneutral.
- 🛡️ **Sicherheit:** Harte Stopps bleiben erhalten.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Phasenfolge rückt nur nach Erfolg weiter.
- 🧱 **Stabilität:** Häufige Wechsel-Historie kann nicht dauerhaft blockieren.
- ✨ **Verbesserung:** openWB und go-e präzisiert.
- ✨ **Verbesserung:** Ladevorgang-Identität zentralisiert.

## [5.3.1j] – 2026-07-10

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** SHI bleibt auch in NORMAL offen.

## [5.3.1i] – 2026-07-10

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Nur dokumentierte FC04-Bereiche.
- ✨ **Verbesserung:** Verbindung und Schreibschutz unverändert.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Gezielter Tag-Fetch.
- ✨ **Verbesserung:** Git als Installationsnutzer.

## [5.3.1h] – 2026-07-10

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Gemeinsamer Betriebsdaten-Zwischenspeicher und kompakter Steuervertrag.
- ✨ **Verbesserung:** Persistente Akquise als optionale Aktivierung.
- ✨ **Verbesserung:** Entscheidungsverlauf v2.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** SHI-Auftrag und physischer Zustand getrennt.
- 🛡️ **Sicherheit:** Warmwasserlauf folgt der realen Verdichterflanke.
- ⚙️ **Regelung:** Quell-Erholung ohne Wolkenflattern.
- ✨ **Verbesserung:** Weiche Luxtronik-Sperre benannt.
- ✨ **Verbesserung:** Kühlanforderung geschützt und diagnostizierbar.

### 📈 Direktvermarktung und Strompreise

- 🛡️ **Sicherheit:** Notstromreserve mit lokalem Veto.
- ⚙️ **Regelung:** EEG-weich und Negativpreis-hart.

## [5.3.1g] – 2026-07-10

### 🔋 Storage Manager

- 🔎 **Diagnose:** Status- und Verlauf-Schreiblast gedämpft.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Veraltet- und Messwertsprünge-Messwerte entwertet.
- ⚙️ **Regelung:** Langsame Budgets gefiltert, Fast-Netz roh.
- 🧱 **Stabilität:** Wallbox-Status und MQTT-Verbindung werden robuster aktualisiert.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Quellerholung respektiert laufende PV-Peaks.

### 📈 Direktvermarktung und Strompreise

- ⚙️ **Regelung:** Eco-Profil ohne Batterieverkauf.
- ✨ **Verbesserung:** Zusatz-WR-Schütz geschützt.
- ✨ **Verbesserung:** Zusatz-WR im Energiefluss.

## [5.3.1f] – 2026-07-09

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Phase 7 (produktive Umschaltung) vorbereitet & aktiviert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Phase H6 produktiv geschaltet.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Phase D7 & D8 implementiert.
- ✨ **Verbesserung:** Shelly-Zusatzwechselrichter-Aktor.

## [5.3.1e] – 2026-07-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Falsche Protection-Klassifikation verhindert.

### 📈 Direktvermarktung und Strompreise

- 🛡️ **Sicherheit:** Keine neue Regelwirkung.

## [5.3.1d] – 2026-07-08

### 🔋 Storage Manager

- 🔎 **Diagnose:** Speicher-Entscheider als klar getrennte Diagnosezustände sichtbar.
- 🛡️ **Sicherheit:** Keine neue Regelwirkung.

## [5.3.1c] – 2026-07-08

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Fahrzeug-Ladeende-Vertrag nutzt konfigurierten Mindeststrom.

## [5.3.1b] – 2026-07-08

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Ladevorgang-Verträge weiter extrahiert.
- 🛡️ **Sicherheit:** Stopp- und Ladeende-Semantik klarer.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Marktwert Solar bleibt sichtbar.
- ✨ **Verbesserung:** Statusgründe verständlicher.

## [5.3.1a] – 2026-07-07

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro übernimmt den Wechsel zurück zur PV-geführten Ladung zuverlässig.
- 🛡️ **Sicherheit:** Keine Zusatzbefehle beim ruhigen Moduswechsel.
- 🐛 **Fehlerbehebung:** Observe-only bleibt treiberlos.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Kanonische EMS-Entscheidungsfläche.

## [5.3.1] – 2026-07-07

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Sicherer Ladestart mit CP-Aufwecken.
- 🛡️ **Sicherheit:** Phasenwechsel strikt stromlos.
- 🐛 **Fehlerbehebung:** Ladeende und EMS-Stopp getrennt.
- ✨ **Verbesserung:** Ein Geräteanbindung-Ausgang bleibt erhalten.
- 🐛 **Fehlerbehebung:** PV-Kurvenstart nutzt reale freie PV-Leistung.
- 🧱 **Stabilität:** Export-Senke bei weicher Speicherreserve.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Marktplan unterdrückt veraltete Low-Price-Holds.
- 🐛 **Fehlerbehebung:** PV-Speicherfenster werden energie-budgetiert.
- 🔎 **Diagnose:** Marktwert Solar Monitor.

## [5.3.0i] – 2026-07-07

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Frühes Markt-Exportaufnehmen nutzt Automatik-Ladelimit statt Netz-Befehl.
- 🧱 **Stabilität:** Fälliges Netzladen bleibt Vollleistung.
- 🔎 **Diagnose:** Markt-Absorb-Kante sichtbar.

## [5.3.0h] – 2026-07-07

### 🔋 Storage Manager

- 🧱 **Stabilität:** Markt-Netzladen absorbiert Echtzeit-Export bedarfsgerecht.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** PV-Speichern holt Kurvenrückstand in Abregel-/Negativpreisfenstern auf.
- 🔎 **Diagnose:** Nachholregelung transparent sichtbar.

## [5.3.0g] – 2026-07-07

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Octopus-Late-Fill bleibt im gültigen Billing-Fenster.
- 🐛 **Fehlerbehebung:** Aktiver Marktvertrag übersteuert Echtzeit-Export-Wartezustand.
- 🔎 **Diagnose:** Marktplan im Echtzeit-Status und kompakter Diagnoseansicht sichtbar.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro bleibt während Phase-Wait startfähig.
- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeug Ladung ended löst bei SoC unter Ziel.
- 🐛 **Fehlerbehebung:** 2p-Fahrzeugprofile werden unterstützt.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Direktvermarktung nutzt separates Marktpreis-Detailansicht.
- 🧱 **Stabilität:** Negative Speicherreserve-Holds schließen neutrale Lücken.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** EEG-Einspeisevergütung im Tagesergebnis.

## [5.3.0f] – 2026-07-07

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Installationskonto-Ermittlung für abweichende Systemnutzer.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wetterwarnungs-Statusanzeige auf Mobilgeräten antippbar.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Der Installer unterstützt abweichende Installationsverzeichnisse zuverlässig.

## [5.3.0e] – 2026-07-06

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Installationskonto liest geschützte Konfiguration zuverlässig.
- 🧱 **Stabilität:** Alte Dienst-Templates nutzen Webserverkonto.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** PV-Tagesertrag mit externem AC-Zusatzwechselrichter.
- 🐛 **Fehlerbehebung:** DV-SoC-Vorschau slotgenau.

## [5.3.0d] – 2026-07-06

### 🔋 Storage Manager

- 🧱 **Stabilität:** RSCP-Zugangsdaten bleiben Pflichtwerte.
- 🐛 **Fehlerbehebung:** EEG-/PV-Speicher-Schwelle schlägt Ökobewertung.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Fahrzeug-Fehlertexte vor eingeschleusten Webinhalten geschützt gerendert.



### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** Externe E3DC-/Luox-Abregelung bleibt führend.
- 🧱 **Stabilität:** Weiche DV-Entscheider-Wechsel entprellt.
- 🔎 **Diagnose:** DV-Preisdomäne und Abregelkontext sichtbar.
- ✨ **Verbesserung:** DV-Vorschau im hellen Modus nachgeschärft.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Shelly EM Mini Gen4 für Klimaanlagen robuster gelesen.
- 🐛 **Fehlerbehebung:** openWB-Pro-Phasenanzeige nach Stop beruhigt.
- 🔎 **Diagnose:** Späte RSCP-Powermeter-Indizes erkannt.

## [5.3.0c] – 2026-07-06

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Netz-PM-Delta entprellt.
- 🔎 **Diagnose:** Diagnosegrund bleibt sichtbar.
- ✨ **Verbesserung:** Neue Einstellungen verbessern die Bewertung schneller Änderungen der Netzleistung.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** Speicherreserve vor Billigpreis-/PV-Speicherfenstern.
- 🐛 **Fehlerbehebung:** Verkauf und PV-Speichern teilen eine Planquelle.
- ✨ **Verbesserung:** Direktvermarktungsvorschau kontrastreicher.

## [5.3.0b] – 2026-07-05

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Updateprüfungen laufen nur noch über den geschützter Installationsweg.
- 🧱 **Stabilität:** Footer ermittelt die Version ohne Git-Aufruf aus dem Webserver.
- 🔎 **Diagnose:** Das Installationszentrum zeigt die installierte Produktversion ohne zusätzliche Systemaufrufe an.

## [5.3.0a] – 2026-07-05

### 🔋 Storage Manager

- 🔎 **Diagnose:** RSCP-Netzwerkausfälle sind keine Zero-Steuerung-Messwerte mehr.
- 🔎 **Diagnose:** Speicherreserve-Plateau-Grund sichtbar.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Gespeicherte Fahrzeugprofile bleiben im Dropdown erhalten.
- 🐛 **Fehlerbehebung:** Fahrzeugseite verliert Custom-Profil nicht bei abgesteckter openWB.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** DV-PV-Speichern hält EMS-Laderahmen an der 300-W-Kante.
- ✨ **Verbesserung:** DV-Verkauf wird getrennt von normaler Speicherentladung gezeigt.
- 🧱 **Stabilität:** Speicherreserve-Discharge stoppt am Zielplateau.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** BOM-Prüfung scannt keine alten Sicherungen mehr.
- 🧱 **Stabilität:** Sicherungen-Retention erweitert.
- 🐛 **Fehlerbehebung:** Schwere Archivkopien werden nicht mehr dupliziert.

## [5.3.0] – 2026-07-05

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** openWB Pro Startkante gehärtet.
- 🐛 **Fehlerbehebung:** Mobilansicht Rücksprünge aus der Installationszentrale.
- ✨ **Verbesserung:** Direktvermarktung im Konfigurationsseite erweitert.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Regelwirksame Messwertglitches sichtbarer.
- ✨ **Verbesserung:** Betreiberwarnungen und Tarifkontext erweitert.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Prognosebasiertes Eco+-PV-Speichern.
- 🛡️ **Sicherheit:** Externe Einspeisebegrenzung bleibt führend.
- ✨ **Verbesserung:** Negativpreis-Speicherreserve.
- 🧱 **Stabilität:** Exportlimit-0-Aufnahme beruhigt.
- 🔎 **Diagnose:** Zusatz-WR und DC-only sichtbar.

## [5.2.8k] – 2026-07-04

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Prognose-Marktregelung lädt oder hält den Speicher nicht mehr automatisch.
- 🛡️ **Sicherheit:** PV-autark zuerst für den normalen Marktregelung.
- 🛡️ **Sicherheit:** Echtzeit-PV hat Vorrang vor normalem Markt-Netzladen.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Markt-Wallboxfreigabe bleibt explizit.

### 🔗 Schnittstellen und Smart Home

- 🧱 **Stabilität:** Shelly-Zustand über Docker-Recreate synchronisiert.

## [5.2.8j] – 2026-07-03

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Die Regelruhediagnose kennzeichnet Messwertsprünge, die tatsächlich eine Regelentscheidung beeinflussen.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** Kurze Messwertsprünge unterbrechen das Speichern von PV-Energie für die Direktvermarktung nicht mehr; eine Hysterese beruhigt die Entscheidung.

## [5.2.8i] – 2026-07-03

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Octopus-Heat-Netzladen nutzt günstigste Abrechnungsfenster.
- 🧱 **Stabilität:** Aktive Entladebesitzer über kurze Echtzeit-Messwertsprünge gehalten.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Forum-kompaktes Diagnosepaket.
- 🔎 **Diagnose:** Analysepakete bleiben vollständig.

## [5.2.8h] – 2026-07-03

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Marktregelung bleibt bei guter Prognose neutral.
- 🐛 **Fehlerbehebung:** Markt-Automatik-Freigabe ist wieder einmalig.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Installationszentrale aus dem Konfigurationsseite erreichbar.

## [5.2.8g] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Markt-Netzladen-Kandidaten in der Prognose sichtbar.

## [5.2.8f] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Octopus-Heat-Marktregelung wartet auf echte Abrechnungspreise.
- 🐛 **Fehlerbehebung:** Zukünftige Markt-Netzladefenster pro Zeitfenster bewertet.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Messwertsprünge-Situationen maschinenlesbar auswertbar.
- 🔎 **Diagnose:** Betriebsdaten/GZ-Rohlogs bleiben auswertetauglich.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Verkaufsfenster werden bis zum nächsten Nachladefenster budgetiert.
- 🧱 **Stabilität:** DV-PV-Speichern resynchronisiert veraltet Mini-Caps.
- 🔎 **Diagnose:** PV-Speicher-Resync sichtbar.

## [5.2.8e] – 2026-07-02

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Markt-Netzladen respektiert Reserve und Zielkurve.
- 🧱 **Stabilität:** Wallbox-Entladeschutz bleibt bei Echtzeit-Messwertsprünge ruhig.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Webauthentifizierung-Ausnahme geschlossen.
- 🛡️ **Sicherheit:** PIN-Prüfung gehärtet.
- 🛡️ **Sicherheit:** Kommandozeile-Jobs im Webverzeichnis gegen HTTP-Aufruf geschützt.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** Watchdog-Installationsassistent validiert Router-IPs.
- 🛡️ **Sicherheit:** Installationsassistent-Paketcheck quote-sicherer.

## [5.2.8d] – 2026-07-02

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Shelly-SG-Ready akzeptiert gemeinsamen Leistungsanhebung-Aufruf.
- 🔎 **Diagnose:** Leistungsmessung sauber eingeordnet.

### 📈 Direktvermarktung und Strompreise

- 🧱 **Stabilität:** DV-PV-Speichern beruhigt.
- 🐛 **Fehlerbehebung:** DV-PV-Speichern hart auf PV-Überschuss gedeckelt.
- 🔎 **Diagnose:** Externe AC-Zusatzwechselrichter getrennt.
- 🛡️ **Sicherheit:** Preisqualität als harter Rückfall.

## [5.2.8c] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Notstrom-/Rückfallreserve als Start-SoC.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Hauptsteuerung observe-only schreibt keine fiktive Start-Historie.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Diagnose-ZIP maschinenlesbarer.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Speicher-Regelung-Leistungsbudget startet SG-Ready-PV-Leistungsanhebung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** PV-Überschuss in niedrigen Direktvermarktungsfenstern speichern.
- 🧱 **Stabilität:** Batterieeinspeisung und Netzladen bleiben getrennte Freigaben.
- 🔎 **Diagnose:** Eigenes Direktvermarktungs-Diagnosepaket.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** SoC-Prognose-Kopfzeile mobil lesbarer.
- ✨ **Verbesserung:** Installationszentrale kehrt mobil zurück.

## [5.2.8b] – 2026-07-02

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Veröffentlichte Ladekurve wandert nicht nach unten.
- 🔎 **Diagnose:** Untergrenze-Clamps sichtbar.

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** Fahrzeug nimmt weniger als angeboten.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** EWMA-/Messwertsprünge-Diagnose im Diagnose-ZIP.
- 🔎 **Diagnose:** Power-Decision-Stability in Regelung-Historien.
- 🧱 **Stabilität:** EWMA bleibt diagnostisch.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** Docker-Installation prüfbarer.

## [5.2.8a] – 2026-07-01

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Geschützte Preis-/Netzladung entlädt den Akku nicht in das Fahrzeug.
- 🔎 **Diagnose:** Wallbox-Speicher-Audit.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** ENTSO-E-Daten werden revisionsfest normalisiert.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Shelly 1 SG-Ready ohne direkt angebunden Wärmepumpe.
- 🧱 **Stabilität:** Taktschutz gilt auch für reines SG-Ready.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** ENTSO-E als 15-Minuten-Rückfall.

## [5.2.8] – 2026-07-01

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Geplantes Netzladen unter Wallbox-Mindest-SoC hält den Speichervertrag.
- 🧱 **Stabilität:** Einzelne unplausible Echtzeitmesswerte reißen den Vertrag nicht auf.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** PV und PV+Akku bleiben openWB-geführt.
- 🐛 **Fehlerbehebung:** Simple Schnittstelle-Status wird korrekt normalisiert.
- 🔎 **Diagnose:** Warnung bei 1-phasigem Netzladen.
- 🔎 **Diagnose:** Wallbox-Details tragen Hauptsteuerung-Warnfelder.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Diagnosearchive nennen zuverlässig die installierte Produktversion.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Tibber liefert jetzt 15-Minuten-Zeitfenster.
- ✨ **Verbesserung:** Tibber-Verbindungsprüfung zeigt die echte Auflösung.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** HA-Kommandos ohne Shell-Interpolation.
- 🛡️ **Sicherheit:** Docker-Installation prüfbarer.
- 🛡️ **Sicherheit:** Der Zugriff auf lokale ML-Modelle und Prognosedaten wurde abgesichert.
- 🛡️ **Sicherheit:** Force-Discharge bricht ohne konfigurierte Zugangsdaten ab.

## [5.2.7d] – 2026-07-01

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Preisfenster laden mit vollem Netzbudget.
- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC-Preisfenster schont den Speicher.
- 🐛 **Fehlerbehebung:** openWB/openWB Pro stoppt bei Wallbox-Mindest-SoC-Untergrenze und Nullbudget härter.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfigurationsseite erweitert.
- 🔎 **Diagnose:** EWMA-/Deadband-Export für Entscheidungswerte.
- 🔎 **Diagnose:** Plausibilitäts-Forensik.
- 🛡️ **Sicherheit:** Offene Web-Endpunkte geschützt.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Tibber als echte Tarifquelle.



### 🔗 Schnittstellen und Smart Home

- 🛡️ **Sicherheit:** Matter-Neustart über Dienst-geschützter Aufruf.

## [5.2.7c] – 2026-06-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Geplante Netz-/Preisstarts lösen alte Stop-Sperrzustände.
- 🧱 **Stabilität:** Schutz gegen Flattern bleibt aktiv.

## [5.2.7b] – 2026-06-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro startet bei ausreichendem Leistungsbudget direkt dreiphasig.
- 🧱 **Stabilität:** Geräteanbindung bleibt reiner openWB-Adapter.
- 🐛 **Fehlerbehebung:** Befehl-Schutz lässt legitime openWB-Pro-Startbefehle durch.
- ✨ **Verbesserung:** openWB Pro lädt real.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Keine leeren Preise bei veraltet SMARD-Daten.
- 🐛 **Fehlerbehebung:** Statische Tarife blockieren Sofort bis Preislimit nicht mehr.
- 🐛 **Fehlerbehebung:** EMS-Netzleistung bleibt gültig, wenn PM-Phasen fehlen.
- ✨ **Verbesserung:** Regelruhe bleibt grün.

## [5.2.7a] – 2026-06-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Harte Stops greifen trotz idle Ladevorgang bei echter Ladung.
- 🐛 **Fehlerbehebung:** Keine direkt angebunden 6A-Batterieladung unter Wallbox-Mindest-SoC-Untergrenze.
- 🧱 **Stabilität:** Batterie-Drain-Nullbudget ist ein E3DC-Hardstop.
- 📊 **Anzeige/Auswertung:** Stop-Anzeige läuft nicht 20 Sekunden nach.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** PV-Freigabe bleibt ruhig.

## [5.2.7] – 2026-06-29

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Frühere manuelle Batterievorgaben bleiben nach ihrem Ende nicht fälschlich in der Anzeige stehen.
- 🐛 **Fehlerbehebung:** Das morgendliche Ladeziel blockiert am Abend keine noch verfügbare PV-Energie.
- 📊 **Anzeige/Auswertung:** Bei vollständig erwarteter PV-Deckung zeigt die Prognose nur wirksame Kurveneinstellungen; interne Vergleichseinstellungen bleiben außerhalb der normalen Bedienung.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB bleibt bei PV- und PV-plus-Akku-Führung zuverlässig in der gewählten Betriebsart.
- 🧱 **Stabilität:** Ohne unterstützte Phasenumschaltung entstehen keine wiederkehrenden Wechsel zwischen Mindeststrom und Stopp.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wetterwarnungen verwenden passende Symbole; die Kopfzeile und der Mobilansicht Energiering wurden übersichtlicher.

## [5.2.6h] – 2026-06-29

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Pro-Phasenwechsel nach 1p-Rückschaltung beruhigt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Unvollständige Mini-Tage ziehen die Hausprognose nicht mehr herunter.
- 🧱 **Stabilität:** Plausibilitätsprüfung-Obergrenze bleibt konservativ, aber realistisch.
- 🐛 **Fehlerbehebung:** Ladekurve springt nicht mehr über Tagesgrenzen.
- ✨ **Verbesserung:** Ladekurve und voraussichtliche Ladung getrennt.

## [5.2.6g] – 2026-06-28

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** ML-Verbrauchsprognose nach Reboot wiederhergestellt.
- 🐛 **Fehlerbehebung:** Keine dauerhaften 500W/300W-Rückfall bei vorhandenem Modell.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Datum im Hybrid-Hinweistexte.
- 🔎 **Diagnose:** Tageswechsel sichtbar.

## [5.2.6f] – 2026-06-28

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicherreserve-Entladung nur bei fehlendem Speicherplatz.
- 🔎 **Diagnose:** Speicherreserve-Druck sichtbar.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Klima als eigener Prognose-Verbraucher.
- 🐛 **Fehlerbehebung:** Hausverbrauch-Kopfzeile nutzt bereinigten Speicherplan.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Klima in der Prognose-Kopfzeile.
- 🔎 **Diagnose:** In Echtzeit- und Prognose-Artefakte erweitert.

## [5.2.6e] – 2026-06-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Optionaler Kurven-Feinregelung.
- ✨ **Verbesserung:** Freies E3DC-Automatik bleibt frei.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Hauptsteuerung nutzt erkannte Ladepunktnummer.
- 🔎 **Diagnose:** Ladepunktquelle sichtbar.

## [5.2.6d] – 2026-06-28

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Halteentscheidungen zentralisiert.
- 🧱 **Stabilität:** Strom reduzieren vor Stop.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Keine Ansicht-Flackerei mehr.
- ✨ **Verbesserung:** Hintergrund-Tabs pollen schonender.

## [5.2.6c] – 2026-06-28

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicherreserve-Reserve führt laufenden Kurvenrahmen weiter.
- 🧱 **Stabilität:** E3DC-Automatik bleibt frei entladbar.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Start- und Phasenwechsel-Fenster zentral gehalten.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Moderne Akku-SoC-Anzeige beruhigt.
- ✨ **Verbesserung:** Akku-SoC direkt am Batteriesymbol.

## [5.2.6b] – 2026-06-27

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Octopus-Heat-Fenster bleiben sauber begrenzt.
- 🧱 **Stabilität:** Just-in-Time bleibt erhalten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Akku-SoC direkt am Batteriesymbol.
- 🐛 **Fehlerbehebung:** Klassische Kompaktansicht wieder konsistent.

## [5.2.6a] – 2026-06-27

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Manuelles Batterieladen bleibt führend.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Moderne Weboberfläche-Kompaktansicht bleibt auf moderne Optik begrenzt.
- 🐛 **Fehlerbehebung:** Weboberfläche-Auswahl wird zuverlässig gespeichert.
- 🔄 **Migration/Kompatibilität:** Rückfallkette aktualisiert.

## [5.2.6] – 2026-06-27

### 🔋 Storage Manager

- 🧱 **Stabilität:** Der optionale gleitende Prognosehorizont wurde für wechselnde Tagesverläufe stabilisiert.
- 🐛 **Fehlerbehebung:** Günstige Preisfenster werden ruhiger genutzt; die Notstromreserve verhindert dabei keine fachlich zulässige Netzladung.
- 🛡️ **Sicherheit:** Ungültige Echtzeitmesswerte lösen keinen aktiven Eingriff aus.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** openWB und openWB Pro setzen Stromänderungen sowie Phasenübergänge kontrollierter um.
- 🔄 **Migration/Kompatibilität:** Die Rückfallkette wurde auf den freigegebenen Vorgängerstand aktualisiert.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Prognose, Marktladung und die gemeinsame Budgetierung steuerbarer Verbraucher wurden weiter zusammengeführt.
- ✨ **Verbesserung:** Die selbstlernende Prognose berücksichtigt neue Messwerte, ohne bei fehlenden Daten unsichere Stellbefehle auszugeben.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Kompakt-, Normal- und Detailansicht wurden vereinheitlicht; Klimadaten können als eigener Verbraucher angezeigt werden.
- 🛡️ **Sicherheit:** Eine vorbereitete Klimaanbindung bleibt ohne aktive Schaltbefehle.

## [5.2.5a] – 2026-06-25

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kein falsches Automatik 0 W bei Unterkurven-Rückstand.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** RSCP-Messwertsätze bekommen Plausibilitätsflags statt versteckter 0-W-Werte.
- 🧱 **Stabilität:** Speicher und Wallbox nutzen nur gültige Leistungsframes aktiv.
- 🐛 **Fehlerbehebung:** Wallbox-Stromschritte bleiben treiberscharf.
- 🧱 **Stabilität:** 1p-Pflicht wird plausibilisiert.
- 🔎 **Diagnose:** Veralteter Fahrzeug-SoC wird sichtbar.
- 🔄 **Migration/Kompatibilität:** Stable-Rückfall bleibt v5.2.5.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche bleibt aktuell, Historie bleibt sauber.

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Diagnose-ZIPs werden stärker komprimiert.

## [5.2.5] – 2026-06-24

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Fehlende Preise sind kein günstiges Netzladefenster.
- 🐛 **Fehlerbehebung:** Netzladen braucht freigegebene Verbraucher.
- 🐛 **Fehlerbehebung:** Speicher-Netzladen endet am echten Bedarf.
- 🔎 **Diagnose:** Marktregelung-Schwelle sichtbar.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Wallbox-Mindest-SoC verhält sich wie PV-Kurve ruhig, bis die Reserve offen ist.
- 🧱 **Stabilität:** Oberhalb Wallbox-Mindest-SoC darf das Auto netzneutral übernehmen.
- 🧱 **Stabilität:** Phasen-/Stop-Kanten sind gehärtet.
- 🐛 **Fehlerbehebung:** Energiebilanz plausibilisiert Direkt angebundene Wallbox-Zähler.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Regelruhe bleibt scharf auf echte Kanten.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Statische Strompreise bleiben im Diagramm durchgängig.

## [5.2.4g] – 2026-06-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Strom-Schrittweite liegt im Wallboxtreiber.
- ✨ **Verbesserung:** openWB-Schrittweite dokumentiert.
- 🐛 **Fehlerbehebung:** Direkt angebundene Wallbox-Tageszähler plausibilisiert.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Energiebilanz klar benannt.
- 🔎 **Diagnose:** Reine Kurven-Führung entwarnt.
- ✨ **Verbesserung:** Marktregelung bleibt Erprobungsstand.

## [5.2.4f] – 2026-06-23

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Wartende Wallbox-Mindest-SoC-Autos ziehen keinen Speicher-Entscheider mehr.
- 🧱 **Stabilität:** Oberhalb Wallbox-Mindest-SoC bleibt der E3DC autonom, solange die Wallbox die Leistung aufnehmen kann.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC folgt unterhalb der Reserve der PV-Kurve ruhig.
- 🐛 **Fehlerbehebung:** PV-only-Untergrenze begrenzt laufende Direkt angebundene Wallboxen auf echten PV-Überschuss.
- 🧱 **Stabilität:** 1p/3p-Übergänge unter Untergrenze-Schutz schneller abgesichert.
- ✨ **Verbesserung:** Weboberfläche zeigt die Wallbox-Untergrenze direkt im Modus-Statusanzeige.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Fahrzeug voll und SoC-Anzeige brauchen bestätigte Quellen.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Doppelte Wallbox-Logzeilen verhindert.
- 🔎 **Diagnose:** Regelruhe-Diagnose gruppiert reine Kurvenführung.
- ✨ **Verbesserung:** Betriebsdokumente, Dienstkatalog und Rechteprüfung aktualisiert.

## [5.2.4e] – 2026-06-22

### 🔋 Storage Manager

- 🧱 **Stabilität:** Kleine Automatik-Rückkehrkanten bleiben ruhig.
- 🧱 **Stabilität:** Kein Überschuss -> CHARGE braucht wieder echten Ladebedarf.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Verlorenes Speicherbudget beendet alte Wärmepumpen-Leistungsanhebung zuverlässig.
- 🔎 **Diagnose:** Defizitgrund klarer.
- ✨ **Verbesserung:** Marktregelung bleibt Erprobungsstand.

## [5.2.4d] – 2026-06-22

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Eine geschützte openWB-Anbindung kann sich optional authentifizieren, statt an einer Zugriffsverweigerung zu scheitern.
- 📊 **Anzeige/Auswertung:** Die Hauptsteuerung der Wallbox wird in der Oberfläche eindeutig benannt.

## [5.2.4c] – 2026-06-22

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Marktregelung-Verbraucher speichern wieder zuverlässig.
- 🐛 **Fehlerbehebung:** Kein unnötiger Wärmepumpen-Regelung-Neustart.
- ✨ **Verbesserung:** Marktregelung-Einstellungen erklärt.

## [5.2.4b] – 2026-06-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Zielkorridor öffnet keinen 12-kW-Speicherbedarf mehr.
- 🧱 **Stabilität:** Regeln Entscheider bleibt ruhig.
- 🐛 **Fehlerbehebung:** Wallbox übernimmt Speicher-Überladung oberhalb berechneter Ladebedarf.



### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Forumtexte lassen sich auch in Browsern mit eingeschränkter Zwischenablage kopieren.
- 📊 **Anzeige/Auswertung:** Regelruhe-Auswertung mit Zeitraum.
- ✨ **Verbesserung:** Strompreislinien mit sauberen Flanken.

## [5.2.4a] – 2026-06-21

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Marktregelung nur bei prognostiziertem Energiemangel.
- 🐛 **Fehlerbehebung:** Markt-Entladesperre öffnet keine Ladegrenze.
- 🧱 **Stabilität:** Kurvenladung folgt gemessener Unterladung gedämpft.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Manuelle Wallbox-Pause löst alte Speicher-Holds.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Marktregelung und V5-Wording sichtbarer.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Telegram-Statistik akzeptiert Oberfläche-Zahlenwerte.

## [5.2.4] – 2026-06-20

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Abendziel blockiert zu frühen Freilauf.
- 🧱 **Stabilität:** Prognose-100-Ziele stärker geführt.
- 🐛 **Fehlerbehebung:** Autonome Wallbox ersetzt nicht die Speicherkurve.
- 🐛 **Fehlerbehebung:** EMS-Power-Settings werden aus 0 W rearmt.
- ✨ **Verbesserung:** Manuelle Batterietrainings laufen länger.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Beobachten mit Speicher-Regelung.
- ✨ **Verbesserung:** Einfache und erweiterte Ansicht konsistent.
- 🛡️ **Sicherheit:** Hausakku-Reserve besser bedienbar.
- ✨ **Verbesserung:** Wording geschärft.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Octopus Heat unterstützt günstige Preisfenster.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Fehlgeschlagene Tagesstatistik gilt nicht als erledigt.
- ✨ **Verbesserung:** Netzfluss richtungsabhängig eingefärbt.
- 📊 **Anzeige/Auswertung:** Netzqualität-Anzeige umbruchfest.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Telegram-Tagesstatistik scheitert nicht mehr still.

## [5.2.3d] – 2026-06-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nach einer kurzen Freigabe an die E3DC-Automatik übernimmt die Ladekurve ihre Führung wieder unmittelbar. Dadurch entstehen keine wiederkehrenden Ladeimpulse durch eine verzögerte Rückkehr in den Haltezustand.

## [5.2.3c] – 2026-06-18

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Ladekurvenkante bleibt ruhig bei PV-/Exportumfeld.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Manuelle Pause endet beim Abstecken.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Ausgeschaltete Wärmepumpen werden heruntergelernt.
- 🐛 **Fehlerbehebung:** Alte ML-Prognosen werden nicht als aktuell angezeigt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Batterie-Detailanzeige zeigt Zähler-/Bilanzdifferenzen.

## [5.2.3b] – 2026-06-18

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** E3DC-Wallbox-Untergrenze wird wirksam respektiert.
- 🔎 **Diagnose:** Untergrenze sichtbar und konsistent.
- 🐛 **Fehlerbehebung:** Moduswechsel beendet manuelle Pause.
- 🐛 **Fehlerbehebung:** Direkt an E3DC angebunden PV-Kurve nach Pause-Freigabe entprellt.
- 🧱 **Stabilität:** Mindeststrom-Stop nutzt ein Import-Integral.
- 🐛 **Fehlerbehebung:** Pause-Anzeige überall konsistent.
- ✨ **Verbesserung:** Energiequelle und Ladeabsicht klar getrennt.
- ⚙️ **Regelung:** Statuszeile ohne versteckte Planung.
- ✨ **Verbesserung:** Minimalistische Wallbox-Ansicht.
- 🛡️ **Sicherheit:** Beobachten bleibt Observe-only.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Beobachtete Wärmepumpenläufe sind keine Schaltbefehle.

## [5.2.3a] – 2026-06-17

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** PV- und Akkuladen berücksichtigt wieder die gewählte Hausakku-Reserve.
- 🐛 **Fehlerbehebung:** Keine neue direkte Startfreigabe direkt nach Stop.
- ✨ **Verbesserung:** Betriebsart, Ladeplan und globale Grenzwerte getrennt.
- ⚙️ **Regelung:** Statuszeile ohne versteckte Planung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Sichtbare Texte verwenden deutsche Umlaute.

## [5.2.3] – 2026-06-17

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Minimalistische Wallbox-Ansicht.
- ✨ **Verbesserung:** SoC- oder kWh-Ziel.
- ✨ **Verbesserung:** Laiengerechtes Wording.
- 🛡️ **Sicherheit:** Regelung aus bleibt Observe-only.



### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Quell-Erholung taktet nicht mehr bei kurzen Request-Lücken.
- 🔎 **Diagnose:** Haltezustände-Zustand sichtbar.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Octopus Heat behält den normalen Arbeitspreis.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Einfache und erweiterte Ansichten.
- ✨ **Verbesserung:** Konfigurationsseite aufgeräumt.
- ✨ **Verbesserung:** Installationszentrale direkt öffnen.
- ✨ **Verbesserung:** Ansichten dokumentiert.

## [5.2.2d] – 2026-06-16

### 🔋 Storage Manager

- 🧱 **Stabilität:** Reale Wallboxladung behält den Speicher-Entscheider.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Pro startet nicht mehr aus Scheinüberschuss.
- 🛡️ **Sicherheit:** Direktvermarktung blockiert den Direktsteuerung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Regelruhe-Zeitleiste ohne Überlagerung.
- 🐛 **Fehlerbehebung:** Wärmepumpen-Detailanzeige erklärt Zählerdifferenzen.

## [5.2.2c] – 2026-06-16

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** V4-Bereinigung behält gültige Wärmepumpenfelder.

## [5.2.2b] – 2026-06-16

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicher-Entscheider-Gerangel beseitigt.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** PV-Kurve ruhig schützt das Abendziel.
- 🧱 **Stabilität:** Mindestleistungs-Kanten nutzbar.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Entscheider-/Zustand-Pendeln erkannt.
- 🔎 **Diagnose:** Keine falschen Kurven-Alarme.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Wetterbadge fachlich korrigiert.
- ✨ **Verbesserung:** Mobilansicht Tagesstatistik erweitert.

## [5.2.2a] – 2026-06-16

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Speicherreserve-Begriffe entschärft.
- ✨ **Verbesserung:** Eigener Schalter für Speicherreserve-Entladung.
- 🔎 **Diagnose:** Sperrgründe und Tagesbudget sichtbar.
- 🔎 **Diagnose:** PV-Prognose plausibilisiert.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Kurvenwerte logisch sortiert.
- ✨ **Verbesserung:** Mobilansicht Konfiguration einheitlich.

## [5.2.2] – 2026-06-15

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Nur-Lese-Vergleichsmodus wird beim Speichern erkannt.
- 🧱 **Stabilität:** Dienste werden auch aktiviert, gestartet oder neu gestartet.
- 🛡️ **Sicherheit:** Keine direkten direkter Dienstaufruf-Sonderwege im Konfigurationsseite.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Dienstverwaltung im Web-Installationsassistent.

## [5.2.1h] – 2026-06-15

### 🔋 Storage Manager

- 🧱 **Stabilität:** Kleine und große Speicher werden mit passenden Reserve- und Leistungsgrenzen behandelt.
- 🐛 **Fehlerbehebung:** Eine vollständige PV-Prognose führt im Zielkorridor nicht mehr zu unnötigem Nachladen.
- 🔎 **Diagnose:** Gründe für Reserve-, Lade- und Halteentscheidungen sind besser nachvollziehbar.

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** openWB Pro, bidirektionale Wallboxen sowie Abfahrts- und Pendelenergie werden in der gemeinsamen Fahrzeugplanung berücksichtigt.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wetterqualität, selbstlernende Prognose und die gemeinsame Planung steuerbarer Verbraucher wurden erweitert.
- ✨ **Verbesserung:** Haushaltsgeräte können als flexible Verbraucher berücksichtigt werden.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Direktvermarktung und Preisdifferenzgeschäfte werden als getrennte wirtschaftliche Anwendungsfälle dargestellt.

## [5.2.1g] – 2026-06-15

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Pendelrahmen gehärtet.
- 🧱 **Stabilität:** openWB Pro Phasenpause ruhiger.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Härtungsfelder 3-7 als Verträge sichtbar.
- 🧱 **Stabilität:** Anker bleiben erklärbar.

### 📈 Direktvermarktung und Strompreise

- 🛡️ **Sicherheit:** Direktvermarktung bleibt abgegrenzt.

## [5.2.1f] – 2026-06-15

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Historischer Abregel-Speicherreserve.
- 🐛 **Fehlerbehebung:** Kaltwind-/Temperaturfaktor.
- 🐛 **Fehlerbehebung:** Komfortboden bleibt Speicherreserve-begrenzt.
- 🐛 **Fehlerbehebung:** Manuelle Lade-/Entladeziele werden sauber quittiert.
- 🛡️ **Sicherheit:** Manuelle SoC-Anker bleiben prognosebegrenzt.
- 🐛 **Fehlerbehebung:** Keine wandernden manuellen Anker.

## [5.2.1e] – 2026-06-14

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität nutzt Verbraucher gezielter.
- 🐛 **Fehlerbehebung:** Prognose 100% folgt wieder der Ladekurve.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Zentraler Multi-Wallbox-Allokationsvertrag.
- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC gilt auch bei Direkt angebundene Wallbox ruhiger.
- 🐛 **Fehlerbehebung:** 1p/3p-Startkanten gehärtet.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Tagesstatistik zeigt kWh-Retter.

## [5.2.1d] – 2026-06-13

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Normale openWB bekommt einen Nebensteuerung-Ladevorgang-Vertrag.
- 🧱 **Stabilität:** go-e bekommt denselben Stufe-Zustand-Vertrag.
- 🔎 **Diagnose:** HTTP-/Stufe-Zustand-Wallboxen sind sichtbar.
- 🐛 **Fehlerbehebung:** Phantom-Entscheider vermeiden.

## [5.2.1c] – 2026-06-13

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** openWB Pro bekommt einen expliziten Ladevorgang-Vertrag.
- 🐛 **Fehlerbehebung:** Angebotene Ampere zählen nicht als echte Ladung.
- 🔎 **Diagnose:** openWB-Pro-Zustände erreichen Weboberfläche-Details.

## [5.2.1b] – 2026-06-13

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** E3DC-Automatik bleibt bei aktiver geregelter Wallbox frei.
- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC-/Zielmodus pendelt nicht mehr gegen die Ladekurve.
- 🐛 **Fehlerbehebung:** Direkt an E3DC angebunden-PV-Starts übernehmen Ladevorgang-Entscheider sauber.
- 🧱 **Stabilität:** PV-Kurve für alle Wallboxen ruhiger.
- 🐛 **Fehlerbehebung:** Feste 3-phasige Direkt angebundene Wallboxen werden korrekt budgetiert.

## [5.2.1a] – 2026-06-13

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Geplante Ladefenster starten wieder aktiv.
- 🐛 **Fehlerbehebung:** Alter Stoppsperre blockiert keinen neuen Planstart.

## [5.2.1] – 2026-06-12

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Gehärteter Produktionsvertrag.
- 🐛 **Fehlerbehebung:** Kein zyklischer harter Stop ohne Grund.
- 🐛 **Fehlerbehebung:** Kein Startimpuls ohne physikalisches Leistungsbudget.
- 🐛 **Fehlerbehebung:** Ladeende wird exakt geführt.
- 🐛 **Fehlerbehebung:** Phantomladen nach Ladeende entfernt.
- 🔎 **Diagnose:** RSCP-Fehler sichtbar.
- ✨ **Verbesserung:** Direkt angebunden Tagesenergie exakt gezählt.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Wallbox-Betriebsdaten schreibt race-frei.
- 🐛 **Fehlerbehebung:** Wärmepumpenanzeige bleibt bei ausgeschaltetem Heizstab sichtbar.

### 📚 Dokumentation

- ✨ **Verbesserung:** Direkt angebunden-Wallbox-Vertrag dokumentiert.

## [5.2.0] – 2026-06-11

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** E3DC-Multi-Wallboxen werden robuster erkannt.
- 🐛 **Fehlerbehebung:** Phantomladen wird härter abgefangen.
- 🧱 **Stabilität:** Direkt angebundene Wallboxen halten ruhiger.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Langzeitbilanz neu strukturiert.
- ✨ **Verbesserung:** Konfiguration Download/Upload integriert.
- 🐛 **Fehlerbehebung:** Stufenpreise und Tageswechsel.
- 🔄 **Migration/Kompatibilität:** Dienste, Rechte und Aktualisierung geprüft.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Eigener Direktvermarktungszweig.
- ✨ **Verbesserung:** Wirtschaftlichkeitsprüfung sichtbar.
- ✨ **Verbesserung:** EEG-/Marktprämienvergleich.
- 🛡️ **Sicherheit:** Netzstrom-Arbitrage bleibt bewusst experimentell.
- ✨ **Verbesserung:** Direktvermarktungs-Vorschau.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Shelly-/Wärmepumpenmonitoring.

## [5.1.8j] – 2026-06-10

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Fehlende E3DC-Peakleistung ist nicht mehr 0 k Wärmepumpe.
- 🔎 **Diagnose:** Prognose kann Ursache sauber unterscheiden.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** V4-Bereinigung entfernt lokale lokale Installationsangaben nicht mehr.

## [5.1.8i] – 2026-06-10

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Ladekurve lässt den Speicher wieder laden.
- 🐛 **Fehlerbehebung:** Dachflächen akzeptieren deutsche Dezimalkommas.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** E3DC-0-k Wärmepumpe blockiert den Prognose nicht mehr.

## [5.1.8h] – 2026-06-09

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfiguration Download/Upload.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Pro 3 em-Messung läuft auch ohne Fahrzeugsteuerung.
- 🐛 **Fehlerbehebung:** Relais bleibt bei Nur-Messen unangetastet.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** V4-Rollback verdrahtet.

## [5.1.8g] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Eindeutiger Nur-Lese-Vergleichsmodus-Statusanzeige in der Kopfzeile.
- 🐛 **Fehlerbehebung:** Entscheidungs-Statusanzeige nutzt Nur-Lese-Vergleichsmodus-Datenstände.

## [5.1.8f] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Nur-Lese-Vergleichsmodus-Livequelle sichtbar.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Nur-Lese-Vergleichsmodus-Weboberfläche liest Hauptsystem-Datenstände.

## [5.1.8e] – 2026-06-08

### 🔋 Storage Manager

- ✨ **Verbesserung:** Optionaler Zielkurven-Modus Prognose 100.
- ✨ **Verbesserung:** Konfigurationsseite-Auswahl ergänzt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Voreinstellungen des Nur-Lese-Vergleichsmodus werden zuverlässig gespeichert.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Passive Stiebel-Kühlung wird nicht mehr als Standby angezeigt.

## [5.1.8d] – 2026-06-08

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Livewerte nutzen dieselbe Normalisierung.
- 🧱 **Stabilität:** Wallbox-Leerlauf-Wiederholungsverzögerung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Edge-Datenstände für in Echtzeit- und Speicherplandaten.
- 🛡️ **Sicherheit:** Zentrale Speicherentscheidung bleibt führend.
- 🔎 **Diagnose:** Kurzer Echtzeit-Status-Datenstände-Zwischenspeicher.
- 🔎 **Diagnose:** Dienst-Lastprofile und Diagnose-Datenstände.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Wiederholungsverzögerung für Hintergrundtabs.

## [5.1.8c] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Nur-Lese-Vergleichsmodus-Client als echte Nur-Lese Vergleichsinstanz vorbereitet.
- 🛡️ **Sicherheit:** Kein Failover, keine Hardwarebefehle.



### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Webdaten-Verzeichnis gehärtet.

### 📚 Dokumentation

- ✨ **Verbesserung:** Anleitung für Betriebsumgebungen ergänzt.

## [5.1.8b] – 2026-06-08

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Hausverbrauch nicht aus asynchroner Bilanz hochziehen.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** WPM-M3.21-Register sauber angebunden.
- 🐛 **Fehlerbehebung:** Keine Phantom-Kältespeicherwerte.
- ✨ **Verbesserung:** Dimplex-Leistung ehrlich markiert.
- ✨ **Verbesserung:** Dimplex-Register dokumentiert.
- 🐛 **Fehlerbehebung:** Mitteltemperatur wird nicht mehr erfunden.
- 🔄 **Migration/Kompatibilität:** Saison-Rückfall bleibt intern erlaubt.

### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Uwe-Fall abgesichert.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Mindestanforderung Speicherplatz angehoben.
- ✨ **Verbesserung:** Administrative Installationsaufgaben benötigen keine direkte Anmeldung als Systemverwalter mehr.

## [5.1.8a] – 2026-06-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Zwischenziele bleiben harte Anker.
- 🐛 **Fehlerbehebung:** UTC-Hosts verschieben keine Uhrzeiten mehr.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Prognose-Kopfleiste bleibt nicht bei 0.0 kWh hängen.
- 🐛 **Fehlerbehebung:** Netzphasen bei Wurzelzähler-Seriennummern.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Standby statt irreführendem WW-/Kühlbetrieb.
- 🐛 **Fehlerbehebung:** WW-Leistungsquelle gewinnt gegen Kühlanforderung.
- 🐛 **Fehlerbehebung:** Mehr Heizkreis- und Speicherwerte im Weboberfläche.
- 🔎 **Diagnose:** Heizleistung bei Stiebel sichtbar.
- ✨ **Verbesserung:** Leistungsquelle lesbar.

## [5.1.8] – 2026-06-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die Kapazität mehrerer Batterieschränke wird als Gesamtsystem plausibel zusammengeführt.
- 🔎 **Diagnose:** Korrekturen an Kapazitätswerten bleiben nachvollziehbar.



### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Langzeit-PV-Erträge bleiben auch bei einem unvollständigen PV-Zähler nutzbar.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Die Prognoseanzeige bleibt bei vorübergehend fehlenden Strompreisdaten nutzbar.

## [5.1.7] – 2026-06-07

### 🔋 Storage Manager

- 🧱 **Stabilität:** Abregelreserve vor Online-Dienst-Edge-Spitzen.
- ✨ **Verbesserung:** Echter Abregeldruck bleibt Pflichtladung.
- 🔎 **Diagnose:** Reserve getrennt sichtbar.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Laufender Sommer-/Warmwasserbetrieb wird nicht mehr als Standby angezeigt.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Sicherungen-Limit gegen Altlasten.
- ✨ **Verbesserung:** Manuelles Sicherungen-Pruning.

## [5.1.6] – 2026-06-07

### 🔋 Storage Manager

- 🔎 **Diagnose:** Glättung bleibt sichtbar.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Schlafende Neben-Wallbox bleibt ruhig.
- ✨ **Verbesserung:** Gemeinsame Wallbox-Grundregeln.
- 🧱 **Stabilität:** Fahrzeugzuordnung pro Zeitfenster robust.

### ☀️ PV, Prognose und Energiemanagement

- 🔄 **Migration/Kompatibilität:** Prerelease-Schiene freigegeben.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Expertenmenü neu gegliedert.

## [5.1.5f] – 2026-06-06

### 🔋 Storage Manager

- 🔎 **Diagnose:** Status nennt den führenden Regelung.
- ✨ **Verbesserung:** Autonome Wallboxladung klarer.
- 🔎 **Diagnose:** Entscheider-Felder im Echtzeit-Status.
- 🐛 **Fehlerbehebung:** Ladekurve reagiert ohne Neustart auf neue Ziele.
- 🐛 **Fehlerbehebung:** Langzeit-Quellenbilanz bleibt physikalisch.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Ziel Wallbox-Mindest-SoC taktet nicht mehr hektisch.
- 🧱 **Stabilität:** Kurze Wolken werden gehalten.
- 🐛 **Fehlerbehebung:** Direkt an E3DC angebunden Hardstops entschärft.
- 🐛 **Fehlerbehebung:** Sofort bis Preislimit lässt PV weiter laden.
- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC trennt Speicherladung und Auto-Leistungsbudget.
- 🧱 **Stabilität:** Priorität ist kein Monopol mehr.
- 🔎 **Diagnose:** Ladepriorität wird in den Wallbox-Details sichtbar.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Nachtwerte werden gekappt.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Kühlbetrieb bleibt im Weboberfläche sichtbar.
- 🐛 **Fehlerbehebung:** WPM-Touch-Normalzustand 0.
- 🐛 **Fehlerbehebung:** SG-Freigabe ohne Besitzer wird zurückgesetzt.
- 🐛 **Fehlerbehebung:** Echtzeitverbindung-Daten robust gelesen.

## [5.1.5e] – 2026-06-05

### ♨️ Wärmepumpe und Wärme

- 🔎 **Diagnose:** WPM-Softwarestand wird gelesen.
- 🔎 **Diagnose:** SG-Rohwerte bleiben sichtbar.
- 🔎 **Diagnose:** Leistungsregister nachvollziehbar.
- ✨ **Verbesserung:** Optionales Feld für WPM Software.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker-Installationen bieten keine unpassende lokale Dienstinstallation mehr an.
- ✨ **Verbesserung:** Richtiger Docker-Ablauf.

## [5.1.5d] – 2026-06-05

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Keine wandernden jüngste Einträge-Hilfsanker.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ziel Wallbox-Mindest-SoC nähert sich ruhiger an.
- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeug-SoC springt nach Regelung-Neustart nicht mehr hoch.



### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Web-Update fängt Darstellung statt Betriebsdaten ab.
- 🐛 **Fehlerbehebung:** MQTT-Hub-Installationsassistent ist V4-tauglich.

## [5.1.5c] – 2026-06-05

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kurvenanker wandern nicht mehr.
- 🧱 **Stabilität:** Erreichbares Tagesziel bleibt Diagnose, nicht neuer Anker.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Phantomladung nach Stop gelöscht.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Phasenbelastung wieder sichtbar.
- 🐛 **Fehlerbehebung:** EMS-Netzleistung bleibt führend.
- ✨ **Verbesserung:** Wurzelzähler klarer erklärt.
- 🐛 **Fehlerbehebung:** Harte Notstromreserve in der Prognose.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Negativpreis-Leistungsanhebung nur für EPEX/Börse.
- 🐛 **Fehlerbehebung:** Speicher-Arbitrage nutzt Octopus-Endkundenpreis.
- ✨ **Verbesserung:** Wallbox-Preislimit geschärft.

## [5.1.5b] – 2026-06-05

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Echte Startanker bleiben sichtbar stabil.
- 🐛 **Fehlerbehebung:** Kein wandernder Morgenanker.
- 🧱 **Stabilität:** Hoher berechneter Ladebedarf darf früher laden.
- ✨ **Verbesserung:** Erreichbarkeit verständlicher.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Mobilansicht Update-Feld.
- 🐛 **Fehlerbehebung:** Update-direkten Webaufruf nutzt den aktuellen Einstieg.

## [5.1.5a] – 2026-06-04

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeug-SoC überlebt Regelung-Neustart.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfigurierte Dienste können automatisch installiert werden.
- ✨ **Verbesserung:** Wärmepumpen-Konfiguration typspezifisch.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Dimplex WPM Touch / NWPM via Modbus TCP.
- ✨ **Verbesserung:** Konfigurierbare Dimplex-Register.
- ✨ **Verbesserung:** SG-Ready-Steuerung.
- 🔎 **Diagnose:** Livewerte im Wärmepumpen-Weboberfläche.
- ✨ **Verbesserung:** Offizielle Dimplex-Dokumentation abgeglichen.
- ✨ **Verbesserung:** Heizstab/BWWP-Zusatzfelder werden eingeklappt.

## [5.1.5] – 2026-06-04

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** openWB-Phantomleistung nach Stop reduziert.
- 🧱 **Stabilität:** openWB Pro Wakeup robuster.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Kontrollierte Wallboxen pausieren unter dem Speicher-Untergrenze hart.
- 🐛 **Fehlerbehebung:** Speicherziel bleibt das Tagesziel.
- 🐛 **Fehlerbehebung:** Ruhiger Wiederanlauf oberhalb des Floors.
- 🐛 **Fehlerbehebung:** E3DC bleibt für Hauslasten frei.
- 🧱 **Stabilität:** Speicherladung bleibt Wallbox-unabhängig.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-ISG-Kühlstatus korrekt gelesen.
- 🧱 **Stabilität:** Ladekurven-Änderungen werden frischer übernommen.
- 🛡️ **Sicherheit:** Installationsverzeichnis gehärtet.

## [5.1.4h] – 2026-05-31

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Physikalischer Ampere-Deckel greift sofort.
- 🧱 **Stabilität:** Keine harten Neustart-Sprünge nach oben.
- 🛡️ **Sicherheit:** E3DC bleibt in Automatik.

## [5.1.4g] – 2026-05-31

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Berechneter Ladebedarf ist wieder feste Führungsgröße.
- 🧱 **Stabilität:** Wallbox-Leistungsbudget sieht dieselbe berechneter Ladebedarf-Führung.
- ✨ **Verbesserung:** Begriff geschärft.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Entladung bleibt im PV-Modus offen.

## [5.1.4f] – 2026-05-31

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** PV-Kurve ruhig bleibt PV-only.
- 🐛 **Fehlerbehebung:** Speicher schützt die Kurve bei aktiver Wallbox.
- ✨ **Verbesserung:** Zwei Zwischenziele für die Ladekurve.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Startfreigaben werden gehalten.
- 🧱 **Stabilität:** Phasenwechsel werden entprellt.
- 🔎 **Diagnose:** Mehr Pro-Diagnosen.

## [5.1.4e] – 2026-05-30

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Direkt angebunden Wallboxdaten werden berücksichtigt.

## [5.1.4d] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC-Halten gibt wieder frei.
- 🧱 **Stabilität:** Hysterese bleibt ruhig.
- 🐛 **Fehlerbehebung:** Direkt angebundene Wallbox-Bilanz gegen Messversatz.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Warnungen werden unabhängig vom PV-Prognose aktualisiert.
- 🐛 **Fehlerbehebung:** Wetterwarnungs-Statusanzeige erkennt mehr reale Signale.

## [5.1.4c] – 2026-05-30

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Kein falscher Luxtronik-Zwang in der Diagnose.
- 🐛 **Fehlerbehebung:** Stiebel-Fehlermeldung verständlicher.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Systempakete gezielt vorbereiten.
- ✨ **Verbesserung:** Headless-Paketmodus.
- 🧱 **Stabilität:** Paketliste zentralisiert.

## [5.1.4b] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Mindest-SoC bleibt Speicherziel während externer Ladung.
- 🧱 **Stabilität:** Kein Rückfall zur höheren Tageskurve bei erreichtem Wallbox-Mindest-SoC.
- 🛡️ **Sicherheit:** Externe Wallbox bleibt Nebenbetrieb.

## [5.1.4a] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Externe Wallbox bleibt Nebenbetrieb.
- 🧱 **Stabilität:** Wallbox-Mindest-SoC ist die einzige Sondergrenze.
- 🧱 **Stabilität:** Kurze Wallbox-Intent-Aussetzer verlieren den Speicher-Entscheider nicht.
- 🛡️ **Sicherheit:** Keine Speicherentladung in externe Wallbox.

## [5.1.4] – 2026-05-30

### 🔋 Storage Manager

- ✨ **Verbesserung:** Zwei Kurven statt einer.
- 🐛 **Fehlerbehebung:** Speicherreserve nur aus echtem Abregeldruck.
- 🧱 **Stabilität:** Große und kleine Speicher werden unterschieden.
- 🛡️ **Sicherheit:** Abendziel vor schönem Speicherreserve.
- 🛡️ **Sicherheit:** Gezieltes Freihalten von Speicherkapazität bleibt letzter aktiver Eingriff.
- 🔎 **Diagnose:** Abregelschutz wird sichtbar.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Hauptsteuerung-Ladefenster takten nicht mehr bei kurzem 0-W-Leistungsbudget.
- 🧱 **Stabilität:** openWB/openWB Pro Netzfenster starten sanfter.
- 🛡️ **Sicherheit:** Autonome openWB bleibt autonom und schützt den Speicher.
- 🐛 **Fehlerbehebung:** openWB Pro startet robuster.
- 🐛 **Fehlerbehebung:** Tagesbilanz trennt externe openWB-Leistung wieder vom Hausverbrauch.

## [5.1.3f] – 2026-05-30

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Direkt an E3DC angebundene Wallboxen werden nicht mehr fälschlich verriegelt.
- 🧱 **Stabilität:** Wiederholter Startversuch arbeitet nicht gegen eigene Stops.
- 🐛 **Fehlerbehebung:** Keine Phantomleistung aus RSCP-Restwerten.

## [5.1.3e] – 2026-05-30

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Volle oder startverweigernde Fahrzeuge werden nicht wiederholt angesteuert.
- 🐛 **Fehlerbehebung:** Eigene Stopps gelten nicht als abgeschlossenes Fahrzeugladen; nach Ladeende bleibt keine Phantomleistung stehen.
- 🧱 **Stabilität:** Ohne bestätigte Ladeleistung wird der Strom nicht weiter erhöht.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik erhält identische Warmwasserziele nicht mehr zyklisch erneut; iDM-Daten bleiben bei einer leeren Rückmeldung erhalten.

## [5.1.3d] – 2026-05-29

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Keine hängende Wallbox-Leistung nach Ladeende.
- ✨ **Verbesserung:** Browser-Glättung folgt derselben AHA-Logik.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Kerndienste werden beim Update gesammelt gestartet.
- 🧱 **Stabilität:** Dienstverwaltung-Startlimits werden vor dem finalen Neustart bereinigt.

## [5.1.3c] – 2026-05-29

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Pro-Reserve und alte Zählerwerte robuster.
- 🐛 **Fehlerbehebung:** Octopus-Heat-Kurve ohne Lücken.
- ✨ **Verbesserung:** Systemvoraussetzungen ergänzt.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Installationszentrum findet den echten Installationsassistent robuster.
- ✨ **Verbesserung:** Konfigurierte Dienste werden beim Speichern vorbereitet.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** MQTT-Hub installiert seine aktuelle Regelung Abhängigkeit.
- 🧱 **Stabilität:** Status-Topics sind keine Messwertfehler.

## [5.1.3b] – 2026-05-29

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Bestätigter Start, danach zügig Zielstrom.
- 🐛 **Fehlerbehebung:** Kein Rückfall auf 6 A bei widersprüchlichen openWB-Pro-Werten.
- 🛡️ **Sicherheit:** Start- und Ende-Regeln bleiben getrennt.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Solcast-Tagesbudget konfigurierbar.
- ✨ **Verbesserung:** Weboberfläche an aktuellen Solcast-Stand angepasst.

## [5.1.3a] – 2026-05-29

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Aus heißt aus, Ende heißt Ende.
- 🐛 **Fehlerbehebung:** Keine Startloops nach vollem Fahrzeug.
- 📊 **Anzeige/Auswertung:** Eindeutige Anzeige statt falscher Gewissheit.
- 🛡️ **Sicherheit:** NGNA bleibt still.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Auftrag endet bei Ziel-SoC.
- 🧱 **Stabilität:** Wartende Fenster schreiben nicht zyklisch.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** iDM-Kühlgrenze besser auffindbar.

## [5.1.3] – 2026-05-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Wallbox-Mindest-SoC-Rückfall friert die Kurve nicht mehr scheinbar ein.
- 🐛 **Fehlerbehebung:** Kurven-Neuberechnung bleibt nachvollziehbar.
- ✨ **Verbesserung:** Geplante Fremdlast-Stützung.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Wallbox-Mindest-SoC bleibt Schutzgrenze.
- 🐛 **Fehlerbehebung:** Phantomladen im Standby unterdrückt.
- 🧱 **Stabilität:** Modus-Prioritäten bleiben getrennt.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Standardisierte Diagnosepakete.

## [5.1.2d] – 2026-05-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Abregelschutz ignoriert Verbraucherrauschen.
- 🧱 **Stabilität:** Kurven-Rückstand holt bei aktiver Wallbox echte PV-Reste nach.
- 🧱 **Stabilität:** Prognose-Automatik nutzt freie Rest-PV vollständiger.
- 🧱 **Stabilität:** Lernender Korrekturrahmen bleibt bis zur Kurve aktiv.
- 🛡️ **Sicherheit:** Autonome Wallbox respektiert Wallbox-Mindest-SoC.
- ✨ **Verbesserung:** openWB-Reichweite wird lokal interpoliert.

### ☀️ PV, Prognose und Energiemanagement

- 🛡️ **Sicherheit:** Rechte-Reparatur respektiert den Standby-System-Standby.
- 🔎 **Diagnose:** Healthcheck unterscheidet Hauptsystem und Standby-System.

### ♨️ Wärmepumpe und Wärme

- 🧱 **Stabilität:** Eigene Kühlgrenze für iDM-Leistungsanhebung.
- ✨ **Verbesserung:** Konfigurierbare Kühlfreigabe.
- 🧱 **Stabilität:** Quell-Erholung bekommt eindeutigen Besitzer.
- 🐛 **Fehlerbehebung:** Stiebel/Shelly-Tagesverbrauch zählt wieder.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** HA-Standby-System startet keine Schreibdienste nach Update.

## [5.1.2c] – 2026-05-28

### 🔋 Storage Manager

- 🧱 **Stabilität:** Lernender Korrekturrahmen gegen Wellenladung.
- 📊 **Anzeige/Auswertung:** Ruhigere Anzeige im Speicherregelung.

## [5.1.2b] – 2026-05-28

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Autoerkennung führt die sichtbare Konfiguration.
- ✨ **Verbesserung:** Ladepunkte als Auswahl.
- 🛡️ **Sicherheit:** NGNA bleibt wirklich beobachtend.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Langzeit-Archivar als Kernsystem.
- 🧱 **Stabilität:** Kernsysteme werden vollständig sichergestellt.

## [5.1.2a] – 2026-05-27

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Komfort-Netz-Rückfall explizit.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB-Autoerkennung.
- ✨ **Verbesserung:** Weboberfläche zeigt erkannte Rolle.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Weboberfläche-Update pollingfest.
- 🧱 **Stabilität:** Update-Verarbeitung defensiv geladen.
- 🔄 **Migration/Kompatibilität:** Fehlerbehebung-Suffixe korrekt sortiert.

## [5.1.2] – 2026-05-27

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität zieht sicheren Speicherreserve ab.
- 🛡️ **Sicherheit:** Netz-Rückfall bleibt Restbedarf.
- ✨ **Verbesserung:** Komfort-gezieltes Freihalten von Speicherkapazität klar getrennt.
- 🐛 **Fehlerbehebung:** Normaler Kurvenrückstand holt weniger defensiv auf.
- 🧱 **Stabilität:** Laderahmen wird auch bei normalem Rückstand nachgeführt.
- ✨ **Verbesserung:** Kurvenanzeige nutzt Regel-SoC.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Tageszähler.
- 🔎 **Diagnose:** Kompakte Diagnose ohne Wärmepumpe stabil.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Docker-Rückfall als Host-Befehl.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Gezielt zurück auf Release.

## [5.1.1] – 2026-05-26

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Harte Kurvenanker werden nachgeführt.
- 🧱 **Stabilität:** Abendziel wird früher abgesichert.
- 📊 **Anzeige/Auswertung:** Anzeige spricht von Laderahmen.

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** openWB-Fremdregelung sichtbarer.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Tageswerte nach Mitternacht.
- 🔎 **Diagnose:** Kältespeicher im Verlauf.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Neuinstallation trotz aktuellem Git-Stand.
- 🧱 **Stabilität:** Weboberfläche-Abgleich gehärtet.
- 🛡️ **Sicherheit:** Keine doppelte Rechte-Reparatur.
- ✨ **Verbesserung:** Konsolenmenü aufgeräumt.

## [5.1.0] – 2026-05-25

### 🔋 Storage Manager

- 🧱 **Stabilität:** Kurvenladung wird sanfter geführt.
- 🐛 **Fehlerbehebung:** Sanfter Freilauf startet nicht mehr zu früh.
- 🐛 **Fehlerbehebung:** Kein Vollgas-Sprung beim Wallbox-Start.
- 🔎 **Diagnose:** Wirksamer Auftrag wird nachvollziehbar.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Gezieltes Freihalten von Speicherkapazität folgt dem Ziel bis zum Kurvenstart.
- ✨ **Verbesserung:** Verbraucherbudget klarer benannt.
- ✨ **Verbesserung:** Geplante Lastfenster.
- ✨ **Verbesserung:** Lastfenster-Schutzwerte sind feste Regeln.
- 🛡️ **Sicherheit:** Lastfenster unter 2 kW werden abgewiesen.
- 🛡️ **Sicherheit:** Zeitfenster werden direkt validiert.
- ✨ **Verbesserung:** Hinweistexte für Lastfenster.
- 🛡️ **Sicherheit:** Keine unplanbare Dauerlast-Raterei.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Speicherregelung ist Energie-Besitzer.
- ✨ **Verbesserung:** Quell-Erholung für geeignete Wärmepumpen.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Regelungsdokumentation aktualisiert.

## [5.0.6] – 2026-05-23

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität wird zeitlich geführt.
- 🧱 **Stabilität:** Verbrauchersteuerung für BEV/Wärmepumpe.
- 🧱 **Stabilität:** Später Kurvenrückstand darf Rest-PV mitnehmen.
- 🛡️ **Sicherheit:** Ziel erreicht heißt Freigabe.
- 🛡️ **Sicherheit:** Keine unplanbare Speicher-Preisstrategie.

### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Entscheidungslogs werden komprimiert.
- 🔎 **Diagnose:** EMS-Reaktionszeit sichtbar.
- 📊 **Anzeige/Auswertung:** Wallbox-Anzeige präzisiert.
- ✨ **Verbesserung:** PV-Kacheln zeigen Ist plus Restprognose.

## [5.0.5f] – 2026-05-23

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Hausversorgung bleibt freigegeben.
- 🧱 **Stabilität:** Wallbox-Regelung respektiert den Boden hart.
- 🐛 **Fehlerbehebung:** Diagnosegrund korrigiert.

## [5.0.5e] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nachtverbrauch nutzt echte saisonale Nachtdaten.
- 🔎 **Diagnose:** Max erreichbar bleibt nachvollziehbar.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox-Befehl-Sperre mit Datum.
- 🧱 **Stabilität:** Gezieltes Speicherplatzschaffen über die Wallbox bleibt kontrolliert geregelt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Unplausible PV-Plateaus werden physikalisch gekappt.

## [5.0.5d] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Aufholleistung skaliert wieder mit dem Speicher.
- 🧱 **Stabilität:** Lineares Taper-Band an der Kurve.
- 🐛 **Fehlerbehebung:** Kein Frei/0-W-Flattern nach Erreichen der Kurve.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wirksamer Ladeauftrag statt nur berechneter Ladebedarf.
- 🔎 **Diagnose:** Herleitung im Hinweistexte.
- ✨ **Verbesserung:** Keine Wallbox-Warteanzeige ohne Direkt angebundene Wallbox.

## [5.0.5c3] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Deutlicher aktueller Kurvenrückstand wird nachgezogen.
- 🛡️ **Sicherheit:** Keine Rückkehr zu Vollgas-Aufholjagden.
- 🔎 **Diagnose:** Rückstand in den Reglerdaten sichtbar.
- 🐛 **Fehlerbehebung:** Aktive EMS-Limits sind keine Hardware-Warnung.
- ✨ **Verbesserung:** Feld klarer benannt.

## [5.0.5c2] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Keine ungeführte Automatik-Aufholjagd unter der Kurve.
- 🧱 **Stabilität:** Berechneter Ladebedarf und Abregelbedarf werden sauber kombiniert.
- 🧱 **Stabilität:** Hysterese gegen Modusflattern.

### 🖥️ Weboberfläche

- 🛡️ **Sicherheit:** Keine Rückkehr zu direkten Web-Git-Rechten.
- ✨ **Verbesserung:** Kein erzwungener Echtzeit-Fetch vor jedem Dialog.
- 🔎 **Diagnose:** Schnellere Diagnose bei Startproblemen.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Update-Check nutzt den geschützter Installationsweg.

## [5.0.5c1] – 2026-05-22

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** RSCP-Derating bleibt Gegencheck.
- 🧱 **Stabilität:** Kein sinusfoermiges Gegenregeln.
- 🐛 **Fehlerbehebung:** Batterie- und Hausleistungs-Rückkopplung entfernt.
- 🐛 **Fehlerbehebung:** Berechneter Ladebedarf wird oberhalb der Momentankurve nicht mehr vorgezogen.
- 🛡️ **Sicherheit:** 300-W-Mindestfreigabe bei aktivem Abregelschutz.
- 🧱 **Stabilität:** Ladeverluste werden über den Netzpunkt ausgeregelt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Abregel-Ladebedarf vor berechneter Ladebedarf.
- 🔎 **Diagnose:** Ziel- und Freigabegrenze sichtbar.

## [5.0.5c] – 2026-05-22

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität endet vor dem Beginn der Ladekurve.
- 🛡️ **Sicherheit:** Eine prognostizierte Wärmepumpenlast gilt nicht ohne Bestätigung als verfügbare Energiesenke.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ladefenster und ein bestätigtes Ladeende stoppen zuverlässig; ein volles Fahrzeug bleibt bis zum nächsten Steckvorgang gesperrt.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Abregelgrenze und Nachlauf wurden konservativer ausgelegt; große Fahrzeuglasten werden auch nahe der Wechselrichtergrenze sauber berücksichtigt.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Die 24- und 48-Stunden-Ansicht läuft wieder gleitend weiter; der Hausverbrauch wird bei aktiven EMS-Grenzen geglättet.

## [5.0.5b1] – 2026-05-21

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Elektrische Leistung als MQTT-Rückfall akzeptiert.

## [5.0.5b] – 2026-05-21

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Prognosejahr wird aus echtem Datum berechnet.
- ✨ **Verbesserung:** Prognosemonat als Hinweistexte.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Seite zeigt keine Luxtronik-Reste mehr.
- 🐛 **Fehlerbehebung:** Alte Echtzeit-Dienste werden beim Typwechsel deaktiviert.
- 🐛 **Fehlerbehebung:** Stiebel-Tagesenergie fließt in Verlauf und Hausverbrauchsbereinigung.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Octopus Heat verfälscht den Eco-Bewertung nicht mehr.

## [5.0.5a] – 2026-05-21

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Abregel-Schutz wird nach echtem Druck wieder freigegeben.
- 🛡️ **Sicherheit:** Echte PV-Spitzen bleiben geschützt.

## [5.0.5] – 2026-05-21

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Stiebel-Livedaten werden im Weboberfläche bevorzugt verarbeitet.
- 🧱 **Stabilität:** Hz-Webscraping hat automatischen Wiederholungsverzögerung.
- ✨ **Verbesserung:** Weboberfläche-Hinweis ergänzt.

## [5.0.4h] – 2026-05-21

### 🔋 Storage Manager

- 🔎 **Diagnose:** Kompakte Diagnose unterscheidet weiche Kurvenführung und echte Auffälligkeiten.
- 🐛 **Fehlerbehebung:** RSCP-Starteinstellungen werden zuverlässig gespeichert.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB-Software-Hauptsteuerung bewusst steuerbar.
- 🛡️ **Sicherheit:** openWB Pro bleibt eigener Direktsteuerung.
- 🐛 **Fehlerbehebung:** openWB-Pro-Phantomladung abgefangen.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Vitalwerte als PDF speicherbar.

## [5.0.4g] – 2026-05-20

### 🔋 Storage Manager

- ✨ **Verbesserung:** DWD-/Open-Meteo-Unwetterwächter.
- 🛡️ **Sicherheit:** Regeleingriff und Netzladen getrennt.
- ✨ **Verbesserung:** Nachtreserve und Netz-Morgenpuffer.
- ✨ **Verbesserung:** Winterereignisse berücksichtigt.
- ✨ **Verbesserung:** Batterie-Grunddaten entzerrt.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Externer Shelly-Leistungsmesser für Stiebel.
- 🐛 **Fehlerbehebung:** Docker-Installationszentrum blockiert Stiebel nicht mehr falsch.
- 🐛 **Fehlerbehebung:** ISG-Prozessdaten-Hz ist optionale Aktivierung und loggt ruhiger.
- 🔎 **Diagnose:** ISG-Register robuster gelesen.
- 🐛 **Fehlerbehebung:** Stiebel-Registeroffset explizit abgesichert.
- ✨ **Verbesserung:** WPMG-Phasenleistung als optionaler Direktwert.
- ✨ **Verbesserung:** Stiebel- und Docker-Doku erweitert.

## [5.0.4f] – 2026-05-20

### 🔋 Storage Manager

- 🔎 **Diagnose:** Tages-KPIs für Speicher, Energiemanagement und Wallboxen.
- 🐛 **Fehlerbehebung:** Implausible E3DC-Kapazitätsrohwerte werden plausibilisiert.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Speicherentscheidungen mit Regelbesitzer.
- 🔎 **Diagnose:** Wärmebudget sauber getrennt.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Stiebel-Eltron-Echtzeit-Dienst vorbereitet.
- 🛡️ **Sicherheit:** Keine Modbus-Schreibzugriffe im Echtzeit-Dienst.
- 🔎 **Diagnose:** Wärmepumpen-Diagnose generisch benannt.
- ✨ **Verbesserung:** Stiebel-Eltron-Dokumentation ergänzt.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** MQTT-Hub loggt ruhiger.

## [5.0.4e] – 2026-05-20

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kein hartes Vollgas bis zur SoC-Toleranz.
- 🔎 **Diagnose:** Neue Vergleichsdaten.
- ⚙️ **Regelung:** Mobilansicht Speicher-Regelung-Karte öffnet die Ladekurve.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Bright-Modus-Kontraste verbessert.
- ✨ **Verbesserung:** Redundantes Soll entfernt.
- 🐛 **Fehlerbehebung:** Energie-Flow-Layout bleibt updatesicher.
- 🐛 **Fehlerbehebung:** Flow-Punkte treffen die Nodes.

## [5.0.4d] – 2026-05-20

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Harte Entladung nur als Rückfall.
- 🔎 **Diagnose:** Kompakte Diagnose unterscheidet Verbraucher-Wartezustand und Rückfall.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Hardware-6A ist keine E3DC-Control-Startfreigabe.
- 🐛 **Fehlerbehebung:** Kein falscher Startimpuls bei Leistungsbudget 0.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Prognosegrafik erfindet keinen gezieltes Freihalten von Speicherkapazität mehr.
- ✨ **Verbesserung:** Prognose-Pause im Bright Modus lesbarer.
- ✨ **Verbesserung:** RSCP-Echtzeit-Statusanzeige besser erkennbar.

## [5.0.4c] – 2026-05-19

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Vitals-Direktseite lädt Helfer selbst.

## [5.0.4b] – 2026-05-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Saubere Freilauf-Übergabe am Kurvenende.
- 🐛 **Fehlerbehebung:** Batterie-Vitals laufen wieder unter Apache.



### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Alte intelligente Ladeplanung-/Ladeplanung-Navigation entfernt.
- 🔎 **Diagnose:** Mobilansicht Speicher-Status integriert.
- ✨ **Verbesserung:** Mobilansicht Ringanzeige bereinigt.
- 🐛 **Fehlerbehebung:** Darkmode-Schalter mobil repariert.
- 🧱 **Stabilität:** Batterie-Kachel bleibt stabil.
- 🔎 **Diagnose:** Echte Fehler im Vital-Weboberfläche sichtbar.

## [5.0.4a] – 2026-05-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kurvenladung gibt den Speicher nicht mehr komplett frei.
- 🐛 **Fehlerbehebung:** Neustart-/Haltezeit-Kante beseitigt.
- 🧱 **Stabilität:** Automatik ist wieder Übergabe statt Regelbefehl.
- 🛡️ **Sicherheit:** Keine neue Modusanzeige.

## [5.0.4] – 2026-05-19

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Kurvenführung bleibt im E3DC-Automatik.
- 🐛 **Fehlerbehebung:** Preis-/Zeitfenster-Halten nutzt Entladegrenzen statt Ruhezustand.
- 🐛 **Fehlerbehebung:** Alte Nebenregelkreise entfernt.
- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität führt den Kurvenstart explizit.
- 🧱 **Stabilität:** Folgetage eindeutig.
- 🐛 **Fehlerbehebung:** Dynamische EMS-Momentanwerte nicht mehr als Hardware-Limits.
- 🐛 **Fehlerbehebung:** E3DC-Packkapazität wird auf Schrankebene normalisiert.
- ✨ **Verbesserung:** Interne V5-Regelungseinstellungen aus der normalen Konfiguration ausgeblendet.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Entscheidungsfunktionen getrennt.
- 🛡️ **Sicherheit:** Befehl-Sperre bleibt alleiniger Schreibzugang.

## [5.0.3e] – 2026-05-18

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Vor Morgenanker wird bei PV wieder geregelt.
- 🐛 **Fehlerbehebung:** Nacht-Automatik und Morgen-PV getrennt.
- 🧱 **Stabilität:** Kompakte Diagnose zeigt die neue Kante eindeutig.

## [5.0.3d] – 2026-05-18

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nachtbetrieb bleibt vor dem Morgenanker in Automatik.
- 🐛 **Fehlerbehebung:** Nachtanker-Fehler beseitigt.
- 🧱 **Stabilität:** Kurvenwerte werden zwischen aktiven Ankern interpoliert.

## [5.0.3c] – 2026-05-17

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Konfigurierbare Wallbox-Schaltzeiten pro Ladepunkt.
- 🐛 **Fehlerbehebung:** Kein automatisches Rückspringen auf PV-Modus.
- 🐛 **Fehlerbehebung:** Tageswerte mit Fremd-Wallbox bereinigt.
- 🐛 **Fehlerbehebung:** Langzeit-Archiv repariert aktuelle Fehlzeilen.
- 🧱 **Stabilität:** openWB und openWB Pro strikt getrennt.
- 📊 **Anzeige/Auswertung:** Capability-Anzeige angepasst.

## [5.0.3b] – 2026-05-17

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Deaktiviert / Keine Wallbox ist hart NGNA.
- 🐛 **Fehlerbehebung:** openWB wechselt nicht mehr unbeabsichtigt in den PV-Modus zurück.
- 🧱 **Stabilität:** openWB und openWB Pro getrennt behandelt.
- 🧱 **Stabilität:** Frische 3p-Kommandos gewinnen gegen veraltet openWB-Status.
- 🔎 **Diagnose:** Letzter Wallbox/openWB-Befehl sichtbar.

## [5.0.3a] – 2026-05-17

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität verhindert vermeidbaren Netzbezug.
- 🐛 **Fehlerbehebung:** Proaktive Kurvenbremse oberhalb der Ladekurve.
- 🧱 **Stabilität:** Kurvenführung bleibt netzschonend.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB-Pro-Fahrzeugidentität stabilisiert.
- 🧱 **Stabilität:** openWB/openWB Pro rampen schneller und ruhiger.
- 🐛 **Fehlerbehebung:** openWB-Pro-Startfreigabe bei 0-Leistungsbudget gehärtet.
- ✨ **Verbesserung:** openWB-Pro-Firmwareupdate auf Nutzerwunsch.

## [5.0.3] – 2026-05-17

### 🔋 Storage Manager

- ✨ **Verbesserung:** Speicher-Entscheidungsrecorder.
- ✨ **Verbesserung:** Voller Regelung-Entscheidungsbaum.
- ⚙️ **Regelung:** Diagnosefenster zeigt Regelung-Entscheidungen.
- 🧱 **Stabilität:** Formale Speicher-Zustandswechsel.
- 🧱 **Stabilität:** Prognose-Vertrauen als Regelgröße.
- 🐛 **Fehlerbehebung:** Nacht-Idle bei Netzbezug gehärtet.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Wallbox Befehl-Sperre.
- 🔎 **Diagnose:** Wallbox Befehl-Sperre Diagnose.
- 🐛 **Fehlerbehebung:** openWB/openWB Pro Fahrzeugerkennung.
- 🧱 **Stabilität:** openWB/openWB Pro Startimpuls beruhigt.
- ✨ **Verbesserung:** Ladeplanung gegen Fehlbedienung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zentraler Konfigurationsprüfung.
- ✨ **Verbesserung:** Ladekurven-Vorschau in der Konfiguration.



### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Top-Menü und Wärme-Status bereinigt.
- 🛡️ **Sicherheit:** Seiten-Neustarts laufen über Dienst-geschützter Aufruf.

## [5.0.2e] – 2026-05-16

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Bezugspunkte der Ladekurve bleiben innerhalb ihres Zeitfensters stabil.

### ☀️ PV, Prognose und Energiemanagement

- 🧱 **Stabilität:** Ungültige Echtzeitdaten bleiben wirkungslos; eine Haltezeit verhindert Pendelbewegungen zwischen Automatik und aktiver Regelung.
- 🔎 **Diagnose:** Die wirksamen Feineinstellungen sind in der Diagnose sichtbar.

## [5.0.2d] – 2026-05-15

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Gastfahrzeuge mit unbekannter Phasenzahl.



### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Watchdog-Nachlauf nach Updates.
- 🐛 **Fehlerbehebung:** Rechte-Reparatur meldet Erfolg korrekt.
- 🐛 **Fehlerbehebung:** Dienst-Restarts normalisiert.

## [5.0.2c] – 2026-05-15

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Morgige Ladekurve nach Sonnenuntergang sichtbar.
- ✨ **Verbesserung:** Prognosechart zeigt die Sollkurve.
- 🐛 **Fehlerbehebung:** Fahrzeuganzeige wird beim Abstecken bereinigt.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Watchdog-Startphase mit regelmäßige Statusmeldung.
- 🐛 **Fehlerbehebung:** Notfall-Neustart nutzt Dienst-geschützter Aufruf.

## [5.0.2b] – 2026-05-15

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Watchdog pausiert bei Updates.
- ✨ **Verbesserung:** Der installierte Systemwächter erhält Korrekturen bei Aktualisierungen automatisch.

## [5.0.2a] – 2026-05-15

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Kurvenbremse auf Kurven-Plateaus.
- 🛡️ **Sicherheit:** Netzwächter für echte Lastsprünge.
- 🐛 **Fehlerbehebung:** PV-Kurve und Ziel-Wallbox-Mindest-SoC sauber getrennt.
- 🧱 **Stabilität:** Wallbox-Hochlauf und Fahrzeugphasen gehärtet.

## [5.0.2] – 2026-05-15

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Solcast-Mehrdach-Hinweis hervorgehoben.
- 🐛 **Fehlerbehebung:** Wärme-Regelungs-Statusanzeige nur bei Wärmeverbraucher.

## [5.0.1] – 2026-05-15

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Neuer Speicherregelung ist jetzt kanonisch.
- 🐛 **Fehlerbehebung:** Frühere manuelle Vorgaben werden bei der Aktualisierung übernommen.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zwei Solcast-Dachflächen mit einem Account.
- ✨ **Verbesserung:** Solcast-Hilfe erweitert.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Weboberfläche-Update nutzt den sicheren geschützter Installationsweg.
- 🐛 **Fehlerbehebung:** Rechte-Reparatur aus dem Weboberfläche bleibt geschützter Aufruf-konform.

## [5.0.0] – 2026-05-15

### 🔋 Storage Manager

- ✨ **Verbesserung:** Speicherregelung Next.
- 🐛 **Fehlerbehebung:** Aktive RSCP-Eingriffe werden gehalten.
- 🛡️ **Sicherheit:** Entladeleistung respektiert Nutzereingabe.
- 🛡️ **Sicherheit:** Notstromreserve und Reserven bleiben geschützt.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** NGNA wirklich One-Shot.
- ✨ **Verbesserung:** Ladepriorität nur bei Dual-Wallbox.
- 🧱 **Stabilität:** openWB, openWB Pro und go-e verwenden eine gemeinsame Wallboxregelung.
- 🐛 **Fehlerbehebung:** Geplante Ladefenster enden hart.
- ✨ **Verbesserung:** Fahrzeug-SoC je Wallbox stabiler.

### 🖥️ Weboberfläche

- ⚙️ **Regelung:** Regelung-Statusanzeige vereinheitlicht.
- 🔎 **Diagnose:** Wiederkehrende Hinweise der Speicher-, Wallbox- und Wärmeregelung werden gedrosselt; Warnungen, Fehler und wichtige Zustandswechsel bleiben sichtbar.
- ✨ **Verbesserung:** Energiefluss Layout bleibt erhalten.

## [4.9.9d] – 2026-05-14

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik/Energie-Regelung startet wieder sauber.

## [4.9.9c] – 2026-05-14

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Direkt an E3DC angebundene Wallbox startet robuster.
- 🐛 **Fehlerbehebung:** Fahrzeugzuordnung stabilisiert.
- 🐛 **Fehlerbehebung:** Keine Phantom-Fahrzeuge an freier WB2.

## [4.9.9b] – 2026-05-13

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Energiefluss Layout bleibt nach Updates erhalten.

## [4.9.9a] – 2026-05-13

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Ladekurve vor dem ersten Stuetzwert korrekt.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Prognose-Halt für alle aktiven Wallbox-Modi.
- 🐛 **Fehlerbehebung:** openWB Pro fällt im Prognose-Halt nicht mehr auf 1p zurück.
- 🛡️ **Sicherheit:** Prognose-Automatik ist Haltefreigabe, kein Vollgas.
- 🧱 **Stabilität:** Frühere Steuerungnahe Wallbox-Nachführung.
- 🧱 **Stabilität:** E3DC bleibt häufiger autonom.
- 🔎 **Diagnose:** Freies Wallbox-Leistungsbudget sichtbar.
- 🐛 **Fehlerbehebung:** Fahrzeugprofile kommen ins Weboberfläche.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik-Zustände vereinfacht.
- 🐛 **Fehlerbehebung:** Ladeplanung trennt WB1 und WB2 sichtbar.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Energie-Flow-Layout-Editor.
- ✨ **Verbesserung:** Standardlayout per Schaltflächen.
- 🐛 **Fehlerbehebung:** Einstellungen speichern bleibt sichtbar.
- 🐛 **Fehlerbehebung:** Speicher-Regelung-Status bleibt ohne Wallbox erhalten.

## [4.9.9] – 2026-05-12

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox-Modi bereinigt.
- 🐛 **Fehlerbehebung:** Wallbox-Modus und Fahrzeugzuordnung speichern wieder sicher.
- 🛡️ **Sicherheit:** Aus ist wirklich aus.
- ✨ **Verbesserung:** Geplantes Netzladen in allen aktiven Modi.
- 🧱 **Stabilität:** Speicher-/Wallbox-Übergänge abgesichert.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Direkte evcc/openWB-Wallboxleistung wirkt sofort.
- 📊 **Anzeige/Auswertung:** Deaktivierte Wallboxen verschwinden aus der Oberfläche.
- 🐛 **Fehlerbehebung:** Dienstverwaltung-Startlimit korrekt platziert.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** MQTT-Konfigurationswechsel greift ohne Handarbeit.

## [4.9.8i] – 2026-05-12

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Ziel-Wallbox-Mindest-SoC deckelt externe Wallboxen sauber.
- 🧱 **Stabilität:** Direkt an E3DC angebundene Wallbox am 6A/3p-Minimum beruhigt.
- 🛡️ **Sicherheit:** Phasen- und Startlogik respektiert den wirksamen Deckel.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Neuinstallation liefert Weboberfläche nicht mehr als Klartext aus.
- 🐛 **Fehlerbehebung:** Web-Update-Vorprüfung reparierbar.

## [4.9.8h] – 2026-05-12

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Preisplan-Speicherschutz dreistufig.
- 🐛 **Fehlerbehebung:** Ziel-Wallbox-Mindest-SoC flacht die Speicher-Ladekurve ab.
- 🧱 **Stabilität:** openWB/openWB-Pro-Regelung beruhigt.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Hausverbrauch stabiler bei externer Wallboxlast.
- ✨ **Verbesserung:** Prognose-Azimuth frei eingebbar.
- ✨ **Verbesserung:** Prognose-Zeile aufgeraeumt.

## [4.9.8g] – 2026-05-12

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Echtzeit-Status bleibt Weboberfläche-kompatibel.
- 🧱 **Stabilität:** Hausabsicherung bleibt im Blick.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Per-Wallbox-Maximalstrom.
- ✨ **Verbesserung:** Wallbox-Bedienung entschlackt.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Direkte MQTT-Wallboxleistung.
- ✨ **Verbesserung:** MQTT-Konfiguration sichtbarer.

## [4.9.8f] – 2026-05-11

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Installations- und Aktualisierungsablauf direkt startbar.
- 🐛 **Fehlerbehebung:** Wallbox-Netzpreislimit begrenzt nur Modus 5.
- ✨ **Verbesserung:** Historische openWB-Echtzeit-Direktsteuerung entfernt.
- ✨ **Verbesserung:** Bright-Modus-Konsistenz verbessert.
- 🐛 **Fehlerbehebung:** MQTT/HA-Eingang ignoriert leere Messwerte.
- ✨ **Verbesserung:** Fieser-Kardinal-Workaround dokumentiert.

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Docker-Erststart bleibt wartend statt tot.
- 🐛 **Fehlerbehebung:** Das Installationszentrum arbeitet im Docker-Betrieb robuster.

## [4.9.8e] – 2026-05-11

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Gezieltes Freihalten von Speicherkapazität ist wieder eindeutig deaktivierbar.
- 🐛 **Fehlerbehebung:** Startfenster 0 ist Auto statt Tot-Schalter.
- ✨ **Verbesserung:** Wetterwarnungs-Statusanzeige.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** openWB Echtzeit-Tasten wieder robust.
- ✨ **Verbesserung:** Direktladen klar benannt.
- 🛡️ **Sicherheit:** Web-Schaltflächen sind echte Schaltflächen.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wärmepumpe farblich getrennt.
- 🐛 **Fehlerbehebung:** Strompreis-Kurven ohne falsche Flanken.
- 🐛 **Fehlerbehebung:** Release-Datum stabil.
- 🐛 **Fehlerbehebung:** Docker-Portprüfung robuster.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Die Aktualität wird für jeden MQTT- und Home-Assistant-Messwert getrennt bewertet.

## [4.9.8d] – 2026-05-11

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Gewitter-/PV-Einbruch beruhigt.
- 🧱 **Stabilität:** Netz-Wächter nutzt reale Entladung.
- ✨ **Verbesserung:** Klarnamen im Weboberfläche.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** MQTT/HA-Wärmepumpe sichtbar.

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Docker-Installationen erhalten wieder zuverlässig das aktuelle Container-Abbild.
- ✨ **Verbesserung:** Docker-Volumes erklärt.

## [4.9.8c] – 2026-05-11

### 🔋 Storage Manager

- 🧱 **Stabilität:** Gezieltes Freihalten von Speicherkapazität wartet sauber auf Verbraucher.
- 🐛 **Fehlerbehebung:** Kurvenbremse entlastet über Wallbox.
- 🧱 **Stabilität:** Übergänge abgesichert.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Normale openWB nutzt offizielle Simple Schnittstelle-Modi.
- 🧱 **Stabilität:** openWB Pro Start- und Phasenlogik beruhigt.
- 🐛 **Fehlerbehebung:** BEV-voll-Sperre entfernt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Scheinleistung für openWB/openWB Pro sichtbar.
- 🐛 **Fehlerbehebung:** Hausverbrauch bei Fremdverbrauchern geglättet.
- 🐛 **Fehlerbehebung:** HA/io Broker-Messwerte dokumentiert und abgesichert.

## [4.9.8b] – 2026-05-10

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Verschachtelte Alt-Repositories ignoriert.
- 🐛 **Fehlerbehebung:** Versionsvergleich robuster.

## [4.9.8a] – 2026-05-10

### 🖥️ Weboberfläche

- 🧱 **Stabilität:** Der Link zum PV-Forum öffnet wieder zuverlässig den neuesten Beitrag.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Phantom-Statusanzeige im Web-Oberfläche-Update entfernt.
- 🐛 **Fehlerbehebung:** Self-Update-Check nutzt frische Werte.
- 🐛 **Fehlerbehebung:** Git-Vergleich robuster.

## [4.9.8] – 2026-05-10

### 🔋 Storage Manager

- 🧱 **Stabilität:** Ladekurvenführung beruhigt.
- 🧱 **Stabilität:** Netz-Wächter und Abregelschutz sauberer platziert.
- 🧱 **Stabilität:** Preis-/Netzladen begrenzt.

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Neue reduzierte Modusstrategie.
- 🧱 **Stabilität:** Schütz-Flattern reduziert.
- 🐛 **Fehlerbehebung:** openWB/openWB Pro Tageszähler haben Vorrang.
- 🐛 **Fehlerbehebung:** Zwei-Wallbox-Statistik konsolidiert.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Tageswerte in den Kacheln.
- 🐛 **Fehlerbehebung:** PV-Kachel gehärtet.
- 🐛 **Fehlerbehebung:** SoC-Prognose-Kopf rechnet Tages-Restwerte.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Diagnosepakete maskieren MQTT-Zugangsdaten.

## [4.9.7g] – 2026-05-10

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB-Startschleife gedrosselt.
- ✨ **Verbesserung:** Abbruchzähler robuster.
- ✨ **Verbesserung:** openWB-Pro-SoC wird auch während der Ladung nutzbar.
- ✨ **Verbesserung:** Unbekannte openWB-Fahrzeuge können direkt zugeordnet werden.
- 🐛 **Fehlerbehebung:** Reichweite und geladene Reichweite werden getrennt behandelt.
- 🐛 **Fehlerbehebung:** Bluelink/Online-Dienst-Werte haben Vorrang vor Schätzwerten.
- 🐛 **Fehlerbehebung:** Dual-Wallbox-Fahrzeugzuordnung bleibt Zeitfenster-treu.
- 🐛 **Fehlerbehebung:** openWB/openWB-Pro-Steckerstatus bleibt auch bei 0 W sichtbar.
- ✨ **Verbesserung:** Wallbox-Modi sind verständlicher benannt.
- 🐛 **Fehlerbehebung:** Leistungsänderungen der Wallbox erfolgen in ruhigeren Schritten.
- 🐛 **Fehlerbehebung:** Preisfenster schützen den Hausakku.
- 🐛 **Fehlerbehebung:** Kurvenauslauf ist ruhiger.
- 🐛 **Fehlerbehebung:** WB2-Ladeplanung nutzt den richtigen Wallbox-Typ.

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Diagnosepakete sind forumstauglich kompakter.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Wärmepumpe sauber deaktivierbar.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Leere oder defekte Betriebsdaten blockiert HA nicht mehr.
- 🐛 **Fehlerbehebung:** Aktualisierungen korrigieren veraltete Installationsangaben automatisch.
- 🐛 **Fehlerbehebung:** ML-Artefakte bekommen gemeinsame Web-Rechte.
- ⚙️ **Regelung:** Docker-Planung nach ML-Lernprozess klarer eingeordnet.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Mittagsziel erzwingt Ladekurvenführung.
- 🐛 **Fehlerbehebung:** Kurventoleranz greift wieder bei 3% statt 30%.
- ✨ **Verbesserung:** Ursi-Fall abgesichert.
- 🐛 **Fehlerbehebung:** Lokale ML-Modelle erhalten zuverlässig die benötigten Zugriffsrechte.
- ✨ **Verbesserung:** Speicher-Simulator loggt ML-Verbrauchsquellen eindeutig.
- 🐛 **Fehlerbehebung:** ML-Lernprozess kann in Docker aus der Echtzeit-Historie starten.

## [4.9.7b] – 2026-05-08

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Eingestelltes Mittagsziel bleibt harte Nutzervorgabe.
- ✨ **Verbesserung:** Abendziel bleibt getrennte Erreichbarkeitsfrage.
- ✨ **Verbesserung:** Optionale Integrationen können über das Installationszentrum eingerichtet werden.
- ✨ **Verbesserung:** Konfigurationsbuttons springen zum richtigen Feld.
- 🐛 **Fehlerbehebung:** Die Einrichtung optionaler Integrationen prüft die benötigten Zugriffsrechte.
- 🐛 **Fehlerbehebung:** Kein falsches Gruen bei sofort crashenden Diensten.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Externe Wallbox-Leistung wird aus dem Hausverbrauch herausgerechnet.
- ✨ **Verbesserung:** SoC-Auslesung sauber abgesichert.
- ✨ **Verbesserung:** openWB Pro und Dual-Wallbox-Betrieb integriert.
- 🐛 **Fehlerbehebung:** Energiefluss und Wallbox-Kacheln für zwei Ladepunkte.
- 🐛 **Fehlerbehebung:** SoC-Prognose wieder regelungsnah.
- ✨ **Verbesserung:** Versionierung bereinigt.
- 🐛 **Fehlerbehebung:** Veraltet Multi-Connect-/Wallbox-Summenwerte werden herausgefiltert.
- 🐛 **Fehlerbehebung:** Kein aktives Laden ohne verbundenes Fahrzeug.

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Prognose springt nach Tagesende auf den nächsten PV-Tag.
- 🐛 **Fehlerbehebung:** Prognose bleibt physikalischer und glatter.
- 🐛 **Fehlerbehebung:** Wallbox-Verlauf pro Ladepunkt trennbar.
- 🐛 **Fehlerbehebung:** Ökobewertung blockiert gezieltes Freihalten von Speicherkapazität nicht mehr pauschal.

### 🖥️ Weboberfläche

- 🛡️ **Sicherheit:** Etappen-Hinweise durch Betriebsmodell ersetzt.
- ✨ **Verbesserung:** Installationszentrale dokumentiert.
- 🔎 **Diagnose:** Diagnosepaket für Bare Metal und Docker.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** WB1/WB2 per MQTT getrennt nutzbar.
- ✨ **Verbesserung:** MQTT-Wizard, Hilfe und Smart-Home-Doku erweitert.
- 🔎 **Diagnose:** Diagnosepaket enthält MQTT-Eingangsdaten.
- ✨ **Verbesserung:** MQTT-Hub im Docker erklärt.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** ML-Modell bei frischen Docker-Installationen erklärt.

## [4.9.6] – 2026-05-06

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die E3DC-Automatik ist wieder der neutrale Grundzustand und wird nicht durch alte Lade- oder Entladegrenzen festgehalten.
- 🛡️ **Sicherheit:** Notstromreserve, Abregelschutz und Netzbezugsschutz haben Vorrang vor Komfort- und Preisoptimierung.
- ⚙️ **Regelung:** Unterhalb der Ladekurve bleibt der Speicher regelbar; ein unerreichbares Tagesziel löst keine Kurvenjagd aus.
- 🧱 **Stabilität:** Morgenpuffer, Tagesziel und Schlechtwetterreserve werden mit Hysterese und stabilen Bezugspunkten geführt.
- 🔎 **Diagnose:** Kapazität, Zelltemperaturen, Ladezyklen und SoH mehrerer Batterieschränke werden plausibel zusammengeführt.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Hausanschlussgrenze und Mindest-SoC schützen auch Installationen mit mehreren Wallboxen.
- 🐛 **Fehlerbehebung:** Phantomleistung, Messversatz und ein veralteter Kabelstatus werden nicht mehr als echte Ladung gewertet.
- 🧱 **Stabilität:** Start, Stopp und Leistungsänderungen erfolgen nur bei bestätigter Ladung; laufende Stromvorgaben überstehen einen Neustart.
- 📊 **Anzeige/Auswertung:** Reichweite, Steckerzustand und mehrere Ladepunkte werden eindeutig dargestellt.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Der Speicherregelung bleibt alleiniger Entscheider für Speicherbefehle; Wallbox und Wärmepumpe melden nur ihren Leistungsbedarf.
- 🐛 **Fehlerbehebung:** Hausverbrauch, SoC-Prognose und PV-Prognose werden nicht durch einzelne Ausreißer oder doppelt gezählte Verbraucher verfälscht.
- 🛡️ **Sicherheit:** Gezieltes Freihalten von Speicherkapazität endet bei schlechterer Prognose und verhindert vermeidbaren Netzbezug.

### ♨️ Wärmepumpe und Wärme

- 🧱 **Stabilität:** Ein modulierender Heizstab reagiert bei kleinem PV-Überschuss ruhiger und wird im Energiefluss nicht doppelt gezählt.
- 🐛 **Fehlerbehebung:** Unterbrochene Modbus-Verbindungen erzeugen keine schnellen Wiederholungen; echte Fehler bleiben von der Leistungsbilanz getrennt.

### 📈 Direktvermarktung und Strompreise

- 🐛 **Fehlerbehebung:** Der Tageswechsel erzeugt keinen falschen Nullpreis; zusammenhängende Preisfenster bleiben ohne Unterbrechung nutzbar.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Planwerte und Echtzeitspitzen, Wallboxzustände sowie Ladekurve und Prognose werden klarer getrennt.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** MQTT und Home Assistant erhielten eine geführte Einrichtung, nachvollziehbare Datenkanäle und automatische Geräteerkennung.
- 🐛 **Fehlerbehebung:** Matter-Schalter erhalten eindeutige Namen; ein Zurücksetzen der Kopplung bricht die Weboberfläche nicht ab.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Docker unterstützt frei zugeordnete Webanschlüsse; Aktualisierung, vollständiger Produktumfang und Hinweise für Synology-Installationen wurden korrigiert.

## [4.9.3] – 2026-05-01

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Nacht- und Abendfreigabe zeigen wieder echtes Automatik.
- ✨ **Verbesserung:** Keine Kurvenjagd nach PV-Ende.
- 🐛 **Fehlerbehebung:** Max erreichbar bezieht sich auf den angezeigten Tag.
- ✨ **Verbesserung:** Dauerzustände werden gedrosselt geloggt.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB/go-e erzeugen keinen heimlichen Ladeplan mehr.
- ✨ **Verbesserung:** Wallbox-Ladeplanung ohne Plan-Spam.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Systemwächter reagiert weniger nervös auf kurze Dienst-Neustarts.
- 🐛 **Fehlerbehebung:** Rechte- und Altprozess-Bereinigung robuster.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Luxtronik-Werte gehen nicht mehr auf dem Weg ins Weboberfläche verloren.
- 🔎 **Diagnose:** Wiederkehrende Null-Watt-Hinweise der Wärmepumpen werden reduziert; Schutzentscheidungen und echte Zustandswechsel bleiben sichtbar.
- 🐛 **Fehlerbehebung:** Langzeit- und Peak-Saving-Anzeigen konsistenter.
- ✨ **Verbesserung:** Hilfe, README und Fach-Dokumentation auf 4.9.3 aktualisiert.

## [4.9.2] – 2026-05-01

### 🔋 Storage Manager

- ✨ **Verbesserung:** Abregelschutz berücksichtigt Kurvennachlauf.
- ⚙️ **Regelung:** Abregelschutz-Rampe gegen Sollwert-Springen.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB/go-e Phantomwerte und Preisboost-Freigabe.
- ✨ **Verbesserung:** Systemwächter startet Speicherregelung vor Failsafe neu.
- ✨ **Verbesserung:** Ladekurven-Erklärung erweitert.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Negativpreis-/Preis-Leistungsanhebung für Speicher, Wallbox und Wärmepumpe.
- ✨ **Verbesserung:** Sicherer Ausstieg aus dem Preis-Leistungsanhebung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Preis-Leistungsanhebung und Ladekurvenstatus im Weboberfläche.

## [4.9.1c] – 2026-05-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Ladekurven-berechneter Ladebedarf wird direkt gesendet.
- ✨ **Verbesserung:** Keine doppelten CHRG-Pulse durch berechneter Ladebedarf-Obergrenze.
- ✨ **Verbesserung:** Wallbox-Prioritäten werden ohne widersprüchliche Speicherentladung umgesetzt.

## [4.9.1b] – 2026-05-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Vorgelagerte Entladung nutzt eigene Entladerampe.
- 🧱 **Stabilität:** Trajektorien-Hysterese für vorgelagerte Entladung.
- ✨ **Verbesserung:** Systemwächter-regelmäßige Statusmeldung in vorgelagerte Entladung-Pausen.

## [4.9.1a] – 2026-04-30

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Keine Tagesladekurven-Eingriffe bei PV 0W.
- ✨ **Verbesserung:** Nachtfreigabe nach defensivem Start-Ruhezustand.
- 🐛 **Fehlerbehebung:** Zweite Solcast-Konfiguration überlebt Docker-Neustart.
- 🐛 **Fehlerbehebung:** Mehrere Solcast-Prognosen werden korrekt zusammengeführt.
- 🐛 **Fehlerbehebung:** Solcast-Zwischenspeicher wird bei geaenderter Resource-ID erneuert.
- 🐛 **Fehlerbehebung:** ML-Prognose wird atomar geschrieben.

## [4.9.0] – 2026-04-30

### 🔋 Storage Manager

- ⚙️ **Regelung:** Die Ladekurve dient als weiches Zwischenziel und bremst den normalen PV-Betrieb nicht unnötig aus.
- 🛡️ **Sicherheit:** Abregelschutz und Notstromreserve haben Vorrang vor der Ladekurve.
- 🐛 **Fehlerbehebung:** Eine zu steile Ladekurve, fehlerhafte Null-Prozent-Ziele und verlorene Zielzeitpunkte nach Neustarts wurden korrigiert.
- ✨ **Verbesserung:** Manuelle Lade- und Entladevorgaben sind wieder verfügbar und werden nicht durch alte Sperrzustände blockiert.
- 🧱 **Stabilität:** Kurvenziele bleiben in einem rollierenden Zeitfenster stabil und reagieren nicht auf einzelne Messwertsprünge.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Der Mindest-SoC der Hausbatterie wird mit Hysterese geschützt; Laden stoppt bei echtem Netzbezug kontrolliert.
- ⚙️ **Regelung:** E3DC-Wallbox und openWB erhalten getrennte, klar zugeordnete Betriebsarten und Leistungsbudgets.
- 🧱 **Stabilität:** Stromänderungen erfolgen in begrenzten Schritten; Start, Pause und Wiederaufnahme werden zeitlich beruhigt.
- 📊 **Anzeige/Auswertung:** Wallbox-Betriebsarten und Feineinstellungen werden in verständlichem Klartext angezeigt.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Die Regelung hält den Speicher nahe der Zielkurve, nutzt drohende Abregelung aber weiterhin vorrangig zum Laden.
- 🐛 **Fehlerbehebung:** Netzbezug, unvollständige SoC-Messungen und veraltete Kurvenpunkte führen nicht mehr zu sprunghaften Lade- oder Entladevorgaben.
- 🧱 **Stabilität:** Wolkenphasen, Neustarts und kurzfristige Prognoseabweichungen werden mit Hysterese und begrenzten Leistungsänderungen abgefangen.
- ✨ **Verbesserung:** Verbrauchsprognose und Wetterbewertung liefern stündlich aktualisierte Ladeziele.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** PV-Überschuss und zulässige Speicherleistung werden bei der Freigabe von Wärmepumpe und Heizstab gemeinsam berücksichtigt.
- 🐛 **Fehlerbehebung:** Ein frisches Null-Watt-Leistungsbudget wird als echtes Stoppsignal behandelt.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Ladekurve, Ist-SoC und Prognose werden gemeinsam dargestellt; Wallbox- und Wärmeeinstellungen sind verständlicher gegliedert.

## [4.6.9] – 2026-04-28

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** EP-Reserve PV-Überschuss: Batterie lud nur 300W statt voller Kapazität.



### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Ladeleistung flackert 2000W↔3000W bei Speicherladung (Über-Kurve-Bremse).
- 🐛 **Fehlerbehebung:** Automatik-Modus: Speichersteuerung verursacht Leistungsvorgabe Konflikt.
- ✨ **Verbesserung:** Minimales Ladelimit 300W durchgängig.

## [4.6.8] – 2026-04-28

### ☀️ PV, Prognose und Energiemanagement

- 🔎 **Diagnose:** Ursache (Timing-Bug).
- ✨ **Verbesserung:** Betroffene Anlagen.

## [4.6.7] – 2026-04-28

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Speicherregelung - SURVIVAL-Zustand sperrte Laden auch bei PV-Überschuss.
- ✨ **Verbesserung:** Betroffene Anlagen.
- 🐛 **Fehlerbehebung:** Installations- und Aktualisierungsablauf - Messwertfilter fehlte in file definitions.

## [4.6.6] – 2026-04-28

### 🔋 Storage Manager

- ✨ **Verbesserung:** Speicherregelung — Falsche RSCP-Moduswahl „Weit/Über Kurve".

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** WR-Speicherreserve-Check (NEU).
- ✨ **Verbesserung:** Netz-Schutz für Speicherladung-Limitierung.
- 🐛 **Fehlerbehebung:** Leistungsbegrenzung — 300W Minimum für Vorgabe der maximalen Ladeleistung.

## [4.6.5] – 2026-04-27

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die maximale Entladeleistung lässt sich wieder zuverlässig speichern; ein verborgenes doppeltes Eingabefeld überschreibt den Nutzerwert nicht mehr.
- ✨ **Verbesserung:** Speicher Automatik Entladung threshold (%, Standard: 20).
- 🐛 **Fehlerbehebung:** Speicherregelung: Vorgabe der maximalen Ladeleistung wurde für kleine Werte von der Firmware ignoriert.
- ✨ **Verbesserung:** Speicherregelung: Morgen SoC Ladekurve startete beim aktuellen SoC (82%) statt bei morgendliches Speicherziel (25%).
- 🔄 **Migration/Kompatibilität:** Speicherregelung — Morgen SoC Ladekurve Deckel, today Plan Zeitfenster Rückfall.
- 🐛 **Fehlerbehebung:** Speicherregelung: Discharge-manuelle Vorgabe wurde bei jedem PV > 0W abgebrochen.
- ✨ **Verbesserung:** Speicherregelung: Speichersteuerung im PV-Normalbetrieb eingefroren die Batterie.
- ✨ **Verbesserung:** Wann zuletzt korrekt.

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Wallbox-Anbindung - Modus 9 lud nur mit PV-Leistung (4kW statt 11kW).
- ✨ **Verbesserung:** Batterieschutz bei Wallbox-Mindest-SoC.
- 🐛 **Fehlerbehebung:** Max-Schaltflächen (Sofortladen direkt angebunden) setzte falschen Modus.
- ✨ **Verbesserung:** Wallbox-Betriebsdaten als verbindliche Dokumentation.
- ✨ **Verbesserung:** Wallbox Modus params Kommentare.
- ✨ **Verbesserung:** Dropdown-Abschnitte klar beschriftet.
- 🐛 **Fehlerbehebung:** Safety-Watcher drosselte "Sofortladen" nach 45s auf 6A.
- ✨ **Verbesserung:** E3DC Direkt angebundene Wallbox: Ladefenster schlagen fehl (physischer Abbruch).

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Energiemanagement - erweiterte Speicherplanung setzte Wallboxmodus 10 (Netz erlaubt).
- 🐛 **Fehlerbehebung:** Speicherregelung - EP-Reserve sperrte auch Laden (EMS-Ruhezustand-Fehler).
- 🧱 **Stabilität:** Speichern-Schaltflächen in aufklappbaren Bereichen funktionieren nun auch unter iOS Safari zuverlässig.
- ✨ **Verbesserung:** Speicherregelung - "Über Kurve" vernichtete PV-Energie am MPPT.
- ⚙️ **Regelung:** E3DC-Automatik + berechneter Ladebedarf in Watt E3DC nimmt berechneter Ladebedarf in Watt als Basis-Sollwert aus PV.
- ✨ **Verbesserung:** Zusätzlich absorbiert E3DC automatisch den PV-Überschuss über Haus+Netz-Limit.
- 🛡️ **Sicherheit:** Eine bewusst gestartete Speicherentladung wird nur bei bestätigter E3DC-Abregelung beendet; ein bloßer Hinweis auf hohe PV-Auslastung erzeugt lediglich eine Warnung.
- ✨ **Verbesserung:** PV minus Haus in Watt als korrektes Gesamt-Leistungsbudget.
- ✨ **Verbesserung:** Speicher Automatik Entladung PV Schutz in Watt (Standard: 500W).
- ✨ **Verbesserung:** Geplanter Eco-Dump (aktiv Speicherplatzschaffen in Watt aus Speicherplan).
- 🐛 **Fehlerbehebung:** Speicherregelung: Speicherladung ersetzt E3DC-Automatik + Leistungsbegrenzung bei Über-Kurve.
- 🐛 **Fehlerbehebung:** Speicherregelung: Wolken-Erkennung bei Über-Kurve.
- ✨ **Verbesserung:** Wärmepumpendaten (geschrieben von Wärmepumpenanbindung) wurde nur für Wärmepumpentyp 2 gelesen, nie für Wärmepumpentyp 3.
- 🔎 **Diagnose:** Der Wärmepumpendaten-Überschreibe-Block lief auch für Wärmepumpentyp 3 und hat den bereits korrekt gesetzten Wert auf 0W zurückgesetzt (erkennbar an "Quelle: Wärmepumpendaten" in der Diagnose-Seite trotz Wärmepumpentyp 3)..
- 🐛 **Fehlerbehebung:** Speicherregelung: noon Ziel SoC wird nach dem ersten Tageslauf eingefroren.
- 🐛 **Fehlerbehebung:** Speicherregelung: Post-Dump-Anker verhindert aggressives Nachladen.
- ✨ **Verbesserung:** Speicherplan: noon Ziel SoC wird im Plan exportiert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** ELWA: bis zum nächsten internen 60s-Zeitüberschreitung.
- ✨ **Verbesserung:** Generisch/Shelly: theoretisch unbegrenzt Korrektur: Bei Automatik Modus inaktiv wird jetzt sofort 0W geschrieben (Heizstab-Modbus) und der Shelly explizit auf AUS gesetzt.



### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Hidden-Input name="save all" als Rückfall.
- 🐛 **Fehlerbehebung:** Speicherregelung: today Plan Zeitfenster Rückfall (ohne zeitlicher Sollverlauf) nutzte simulierte SOC-Werte als Zielkurve.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Grundlast-Filter für Shelly Pro 3 em (Unterverteilung).
- ✨ **Verbesserung:** Die direkte Shelly-Abfrage verwendet die für Shelly Pro 3EM konfigurierte Geräteadresse.
- ✨ **Verbesserung:** Daten fließen automatisch in Echtzeitdaten und ML-Lernprozess ein.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Weboberfläche - Docker Hot-Start via pkill + nohup.

## [4.4.6] – 2026-04-26

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Speicherregelung: vorgelagerte Entladung verhindert PV-Abregelung bei großen Anlagen.
- ✨ **Verbesserung:** Neuer Ansatz — Hardware-Realitäts-Simulation.
- ✨ **Verbesserung:** HW-Simulation: Simuliert was die E3DC-Firmware wirklich tut — lädt ungekürzt bis SoC 100% (kein Ziel-SoC-Obergrenze), mit konservativem Mindest-Eigenverbrauch 300W statt ML-Schätzung.
- ⚙️ **Regelung:** Vorgelagerte Entladung Ramp: Plant automatisch Entladung (Zeitfenster zwischen jetzt und Speicher full Zeitpunkt). Progressives Leistungsprofil: Früh morgens maximale Entladeleistung (bis 4500W), linear abnehmend auf 1500W nahe dem PV-Peak.
- ✨ **Verbesserung:** Konfigurierbarer Min-SoC: ökonomisch Speicherplatzschaffen minimal SoC (Standard 10%) schützt die Batterie vor Tiefentladung.
- 🐛 **Fehlerbehebung:** Speicherregelung: Kurvenstart nach Eco-Dump auf Post-Dump-SoC gesetzt.
- ✨ **Verbesserung:** Root-Cause (zweistufig).
- ✨ **Verbesserung:** Can reach Ziel = (maximal SoC today >= target 0.95) prüfte das Ergebnis der gedrosselten Simulation statt die physikalische Machbarkeit.
- ✨ **Verbesserung:** Minimal Ladung in Watt: Garantiert dass required in Wattstunden in den verfügbaren Zeitfenster physikalisch erreichbar ist (dynamisch nachgeführt).

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Spalte 1 (links, flex-grow).
- ✨ **Verbesserung:** Spalte 3 (rechts).

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Speicherregelung: Eco-Dump hat Vorrang vor Kurven-Logik.
- 🐛 **Fehlerbehebung:** Speicherregelung: Hysterese-Zone sperrt nie die Entladung.
- ✨ **Verbesserung:** Prognose- und Preislogik: Phase A — Horizont-abhängige Modellgewichtung (IEA Task 16 Standard).
- ✨ **Verbesserung:** Kurzfristige Prognosen gewichten aktuelle Wettermodelle stärker als längerfristige Vorhersagen.
- ✨ **Verbesserung:** 24–48h (Mittelfrist): Gleitender Übergang M1↔M2.
- ✨ **Verbesserung:** Prognose- und Preislogik: Phase B — Wetterklassen-Gewichtung (basiert auf M2-Strahlungsdaten).
- ✨ **Verbesserung:** Klar (>300 W/m²): M1 +25% Bonus — Dachgeometrie (Neigung/Azimuth) zahlt sich bei klarem Himmel aus.
- ✨ **Verbesserung:** Mischbewölkt (80–300 W/m²): neutral.
- ✨ **Verbesserung:** Bedeckt/Nebel (<80 W/m²): M2 +30% Bonus — NWP-Diffusstrahlung-Modell ist hier überlegen.
- ✨ **Verbesserung:** Prognose- und Preislogik: Effektive Gewichte im Systemprotokoll.
- 🐛 **Fehlerbehebung:** Speicherregelung: can reach Ziel falsch negativ bei großem PV-Überschuss (>50 kWh).
- 🐛 **Fehlerbehebung:** Prognose- und Preislogik: Ensemble-Gewichtung verlor 40% des Ertrags wenn Solcast (M3) nicht konfiguriert.
- 🐛 **Fehlerbehebung:** Prognose- und Preislogik: Selbstlernender Weight-Updater war seit Verlauf-Buffer-Einführung blind (pred in Kilowattstunden 0.000).
- ✨ **Verbesserung:** Prognose- und Preislogik: Selbstlernendes EWMA-Korrekturfaktor-System (IEA Task 16 Standard).
- ✨ **Verbesserung:** Clearsky-Klassifizierung.
- 🔎 **Diagnose:** Prognose- und Preisdaten migriert auf Version 2 mit neuem Schema: daily Systemprotokoll (90-Tage-Protokoll pro Tag mit actual kwhforecast in Kilowattstunden, Korrekturfaktor rawclearsky class, quarter), seasonal Korrekturfaktor (Q1–Q4 EWMA-Faktoren).
- 🐛 **Fehlerbehebung:** Prognose- und Preislogik: Doppelter Berechnung-Aufruf entfernt.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Eine kurze, sauber abbrechbare Anlaufverzögerung verhindert Zugriffe, bevor die benötigten Dienste bereit sind.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Weboberfläche: Frequenz-Labels im Netzgesundheits-Dialog waren falsch ausgerichtet.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Installations- und Aktualisierungsablauf.

## [4.4.1] – 2026-04-25

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Beim Erreichen des Ziel-SoC wird eine zulässige Ladegrenze statt einer unbeabsichtigten Null-Watt-Vorgabe verwendet.
- 🐛 **Fehlerbehebung:** Die Ladekurve beginnt nicht mehr unterhalb des konfigurierten morgendlichen Mindest-SoC.
- 🐛 **Fehlerbehebung:** Netzbezug wird auch nahe der Sollkurve korrekt ausgeglichen.

### 🔌 Wallbox Manager

- 🛡️ **Sicherheit:** Eine aktive Wallboxladung schützt die Hausbatterie vor unbeabsichtigter Entladung.
- 🧱 **Stabilität:** Physikbasierte Überschussregelung und Hysteresen reduzieren unnötige Schaltvorgänge.
- ✨ **Verbesserung:** Die Grundlagen für bidirektionale Wallboxen wurden in der Bedienung und Regelarchitektur berücksichtigt, ohne eine allgemeine Freigabe vorzutäuschen.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Im normalen PV-Betrieb begrenzt das System die Ladeleistung, während das E3DC die Entladung für Hausverbraucher weiterhin autonom regelt.
- 🛡️ **Sicherheit:** Aktive Speicherladung aus dem Netz bleibt im PV-Betrieb gesperrt; preisgesteuerte Ausnahmen benötigen eine ausdrückliche Freigabe.
- 🐛 **Fehlerbehebung:** Die Lernfunktion berücksichtigt wieder kurze reale Lastspitzen und skaliert Tagesziele mit der nutzbaren Speicherkapazität.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Das gemeinsame Leistungsbudget für Wallbox und Wärmepumpe berücksichtigt die vom Batteriesystem aktuell erlaubte Ladeleistung.

### 🖥️ Weboberfläche

- 🐛 **Fehlerbehebung:** Der Forum-Link führt wieder zum aktuellen Diskussionsstand.
- 🐛 **Fehlerbehebung:** Eine Warnung des Systemwächters verschwindet nach nachweislicher Erholung des Speicherdienstes.

### 📦 Distribution und Kompatibilität

- 🧱 **Stabilität:** Docker-Aktualisierung und Startreihenfolge wurden korrigiert, damit Echtzeitwerte vor den darauf aufbauenden Diensten bereitstehen.

## [4.1.0] – 2026-04-23

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Sonnenmodus als Basis (Gerätefreigabe[0]=1).
- ✨ **Verbesserung:** Delta ∈ [-band dn ...
- 🧱 **Stabilität:** 6 Modi mit klaren Hysterese-Bändern.
- ✨ **Verbesserung:** 6A Grundlast nur aktiv wenn can reach Ziel aktiv (Speicher Plan Prognose).
- ⚙️ **Regelung:** Schont den Schütz: E3DC stoppt bei 6A-Deckel autonom, kein aktuelle Regelung Umschaltbefehle.
- ✨ **Verbesserung:** Gradueller Moduswechsel.
- ✨ **Verbesserung:** Phasenerkennung automatisch.
- ✨ **Verbesserung:** Ökobewertung konfigurierbar (ökonomische Netzbewertung, ökonomische PV-Bewertung).
- ⚙️ **Regelung:** Haus-Priorität (Wallbox-Mindest-SoC).

## [4.0.28] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Die Netzbewertung verwendet wieder den unveränderten Messwert; eine längere Mindesthaltezeit begrenzt erneute Leistungsänderungen nach dem Anlaufen.

## [4.0.27] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Die Ladeleistung steigt langsamer an, sodass das E3DC-Energiemanagement zwischen zwei Erhöhungen ausreichend Zeit zum Einregeln erhält.

## [4.0.26] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Nach einer Beruhigungsphase wird die Ladeleistung nur begrenzt angehoben und startet abhängig vom vorherigen Zustand konservativ.

## [4.0.25] – 2026-04-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Die Wallboxregelung trennt die eigene Ladeleistung von der Netzbewertung und erhöht den Strom langsamer, damit kein selbstverstärkender Regelkreis entsteht.

## [4.0.24] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Eine Anlaufphase, eine feste Ruhezeit und die Erkennung schneller Stopps geben dem E3DC-Energiemanagement Zeit zum Einpendeln und verringern Takten.

## [4.0.23] – 2026-04-23

### 🔌 Wallbox Manager

- 🧱 **Stabilität:** Ladevorgänge beginnen kontrolliert mit dem Mindeststrom, während geglättete Speicherwerte und ein klar begrenzter Tagesverlauf unnötige Wechsel reduzieren.

## [4.0.22] – 2026-04-23

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Die Heizstabregelung liest die aktuelle Konfiguration und kann ältere Einstellungen weiterhin übernehmen; der zugehörige Dienst wird bei der Installation aktiviert und gestartet.

## [4.0.21] – 2026-04-23

### 🔋 Storage Manager

- ⚙️ **Regelung:** Ein zusätzliches Speicherziel für die Mittagszeit ermöglicht eine zweiteilige Ladekurve und hält am Nachmittag gezielt Kapazität für PV-Energie frei.

## [4.0.20] – 2026-04-23

### 🔋 Storage Manager

- ✨ **Verbesserung:** SoC-Kurve im Weboberfläche fehlt komplett wenn ML-Prognosedaten nicht vorhanden (Speicherregelung).

## [4.0.19] – 2026-04-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Fehlt im Speicherplan die freie Leistung für steuerbare Verbraucher, verwendet die Wallboxregelung einen sicheren Zahlenwert statt eines leeren Zustands; Ladevorgänge können dadurch wieder starten.

## [4.0.18] – 2026-04-23

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Die Ruhephase der Wallbox beendet sich nicht mehr selbst; wiederkehrendes Ein- und Ausschalten wird verhindert.

## [4.0.17] – 2026-04-23

### 🔋 Storage Manager

- 🔎 **Diagnose:** Diagnostik car inaktiv im Systemprotokoll erklärt: Entsteht beim Moduswechsel wenn RSCP-Status nach Übernahme der Laderegelung noch nicht synchron ist.

## [4.0.16] – 2026-04-23

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Dokumentiert in Wallbox-Betriebsdaten.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Nach der Übergabe an die E3DC-Automatik bleibt der im Portal gewählte Wallboxstrom erhalten und wird nicht auf 6 A zurückgesetzt.

## [4.0.15] – 2026-04-23

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox-Prioritätsstufen Modus 4-9 jetzt linear & konsistent (Speicherregelung, Wallbox-Anbindung).
- ✨ **Verbesserung:** Wallbox-Anbindung.

## [4.0.14] – 2026-04-23

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Soll-Kurve (blau) im Ladekurven-Diagramm immer leer (Speicherregelung).
- ✨ **Verbesserung:** Fehlende Meta-Felder im Speicherplan.

## [4.0.13] – 2026-04-23

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** Batterie lädt trotz Speicherladung-Befehl nicht wenn Wallbox-Priorität aktiv (Speicherregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Alle Einstellungen der PV-Mindestladung sind in Konfigurationsoberfläche und Bereinigung vollständig registriert und gehen bei Aktualisierungen nicht verloren.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Ladekurven-Kachel klickbar mit Weboberfläche Dialog: Zeigt Soll-Kurve (blau gestrichelt), IST-SoC aus Echtzeit-Verlauf (grün) und PV-Prognose (orange, sekundäre Achse) mit "Jetzt"-Linie.

## [4.0.12] – 2026-04-23

### 🔋 Storage Manager

- 🧱 **Stabilität:** Hysterese war symmetrisch ±3% — Batterie lädt trotz starkem PV-Überschuss nicht (Speicherregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** PV-Überschuss-Untergrenze jetzt standardmäßig aktiv (Speicherregelung).

## [4.0.11] – 2026-04-23

### 🔋 Storage Manager

- ✨ **Verbesserung:** Ladekurve bleibt nach Neustart starr auf aktuellem SoC eingefroren (Speicherregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** PV-Überschuss-Untergrenze bei „Kurve getroffen" (Speicherregelung).

## [4.0.10] – 2026-04-23

### 🔋 Storage Manager

- ✨ **Verbesserung:** RSCP Echtzeitdaten Stabilität.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox "Brain-Begrenzung" (Wolken-Glätter).

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Leere numerische Konfigurationsfelder führen nicht mehr zu wiederkehrenden Neustarts der Wallboxregelung.
- 🧱 **Stabilität:** Speicherregelung (Inselnetz-Flattern).
- 📊 **Anzeige/Auswertung:** Web-Oberfläche Konfigurationsseite.
- ✨ **Verbesserung:** Dynamische Konfigurationsbereinigung.
- ✨ **Verbesserung:** Speicherplanung (V4 Crash Korrektur).
- ✨ **Verbesserung:** Systempakete & Debian Trixie.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** EPEX „Keine Zeitfenster" nach Dienst-Neustart.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** V4 Update Berechtigungen.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Modbus Abhängigkeiten.

## [4.0.5] – 2026-04-22

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Ein Syntaxfehler in den Regeln für administrative Installationsaufgaben wurde behoben.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Webportal-Installation.
- 🐛 **Fehlerbehebung:** Installationsassistent-Crash behoben.

## [4.0.4] – 2026-04-22

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Release & Docker-Build.

## [4.0.3] – 2026-04-22

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Speicherregelung (Ladelogik 0W-Limit).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche "Dienste Neustarten" Schaltflächen.

### 📦 Distribution und Kompatibilität

- 🛡️ **Sicherheit:** Watchdog (Pi Schutz) Docker-Bug behoben.
- ✨ **Verbesserung:** Robustere Port-Konfiguration.

## [4.0.1] – 2026-04-21

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Benötigte Hintergrunddienste werden bei der Einrichtung automatisch aktiviert.
- 🐛 **Fehlerbehebung:** Die Regeln für administrative Installationsaufgaben wurden korrigiert.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Rsync für Web-Oberfläche.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent Git Phantom-Bug.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** V3 zu V4 Konfigurations-Migration.

## [3.9.6.2] – 2026-04-18

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** Direkt angebunden openWB Steuerung (Wallboxregelung).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** V4 Autonomie (kWh-Retter).
- 📊 **Anzeige/Auswertung:** Statistik Hangover.

## [3.9.6.1] – 2026-04-17

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** KI-gestützte Wallbox-Logik & V4-Speichermanagement.
- 📊 **Anzeige/Auswertung:** Oberfläche/Bedienung Fehlerbehebung.
- ✨ **Verbesserung:** Architektur Fehlerbehebung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Logik Fehlerbehebung (Ladeplanung).

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Direktvermarktung & Spotmarkt-Integration.

## [3.9.6] – 2026-04-17

### 🔋 Storage Manager

- ✨ **Verbesserung:** Fundamentale Designkorrektur Wallbox-Mindest-SoC vs. dynamischer Mindest-SoC.
- ✨ **Verbesserung:** Wallbox Mindest So c hartes absolutes Untergrenze (nur noch für Modus 10 „Sofort immer" wirksam).
- ✨ **Verbesserung:** Wallbox-Speicherziel (Konfiguration, Standard 90%) = Abend-Ziel kurz vor Sonnenuntergang.
- ✨ **Verbesserung:** Dynamischer Mindest So c laufend berechneter Schutzwall basierend auf PV-Prognose: Wie viel muss die Batterie JETZT mindestens haben, damit das 90%-Ziel abends noch erreichbar ist?
- ✨ **Verbesserung:** Prognose-Integration.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Bidirektionale Wallboxen (V2G / V2H).
- ✨ **Verbesserung:** Kaskaden-Timer für PV-Leistungsanhebung (Trägheitssimulation).
- ✨ **Verbesserung:** Wechselrichter AC-Limit (erzwungene DC-Ladung).
- 🧱 **Stabilität:** Wolken-Hysterese (Tick-Tack Puffer).
- 🛡️ **Sicherheit:** Sonnenmodus Stop-Schutz (22kW-Flash Korrektur).
- 🛡️ **Sicherheit:** 6A Selbstlern-Schutz (Wallbox minimal Leistung meas).
- 📊 **Anzeige/Auswertung:** RSCP-0W Messwertsprünge Filter (Anzeige-Flackern).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Eigenständiger Speicherplanung (V4).
- ✨ **Verbesserung:** ML-Ausbau & Selbstlernende Prognose.
- ✨ **Verbesserung:** Erweiterter Eco-Bewertung – Netzbewusstes Laden & Entladen.
- ✨ **Verbesserung:** Systemautonomie & Resilienz (Inselnetz).
- ✨ **Verbesserung:** Dynamische Lastverschiebung (Haushaltsgeräte).
- ✨ **Verbesserung:** DC/DC-Wandler am DC-Bus (Topologie-Korrektur).
- ✨ **Verbesserung:** Ext PV korrekt von erzwungene DC-Ladung getrennt.
- ✨ **Verbesserung:** Systemprotokoll-Verbesserung.
- ✨ **Verbesserung:** Zeitfenster-genaue Temperaturprognose.
- ✨ **Verbesserung:** Temperaturquelle gehärtet.
- ✨ **Verbesserung:** Prognose- und Preislogik.
- ✨ **Verbesserung:** Plausibilitätsprüfung des Hausverbrauchs bei der Tagesarchivierung.
- 🔎 **Diagnose:** Ein Korrekturwerkzeug bereinigt fehlerhafte historische Energiewerte nachvollziehbar.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Systemprotokoll-Ebene des Safety-Checks korrigiert.
- ✨ **Verbesserung:** Zirkulations-Systemprotokoll.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Direktvermarktung & Spotmarkt-Integration.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Externer Generator in Langzeitstatistik.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Autarkie-Rückfall.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Smart-Home Evolution (Matter & Push).

### 📚 Dokumentation

- ✨ **Verbesserung:** Wallbox-Betriebsdaten überarbeitet.
- ✨ **Verbesserung:** E3DC-Classic als eigenständiges Repo.

## [3.9.5.1] – 2026-04-17

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Netzbezug / Netzeinspeisung Energie-Tags vertauscht.
- ✨ **Verbesserung:** Neues Feature: wurzelzähler invertiert (Konfiguration).

## [3.9.5] – 2026-04-16

### 🔋 Storage Manager

- ✨ **Verbesserung:** Erklärung der Zell-Grenzwerte.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Entkopplung der Wallbox-Ladeplanung vom Speicher.
- ✨ **Verbesserung:** Online-Dienst-Integrations-Indikator.
- ✨ **Verbesserung:** Leere "0W" Ghost-Readings.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Problem "Mehrere SHI Schreibquellen" (Error 816) behoben.
- ✨ **Verbesserung:** Rückkanal-Validierung.

## [3.9.4] – 2026-04-15

### 🔋 Storage Manager

- 🐛 **Fehlerbehebung:** Die RSCP-Anmeldung verarbeitet Kennwortangaben wieder zuverlässig.
- ✨ **Verbesserung:** Echtzeitdaten Integration.
- ✨ **Verbesserung:** Tagesertrag Historie.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Prognose- und Preislogik — Ladeplan-Erzeugung mit Battery-Care AI, Ladeplanstatus.
- ✨ **Verbesserung:** Regelungslogik — Leistungsverteilung Leistungsverteilung.
- ✨ **Verbesserung:** Keine Verhaltensänderung.
- ✨ **Verbesserung:** Echter originaler Messzeitstempel aus openWB MQTT-Betriebsdaten, kein aktuelle Zeit.
- ✨ **Verbesserung:** Neuer abort Laden Verarbeitung (sanft).
- ⚙️ **Regelung:** Abort all Laden bleibt als NOT-AUS (physische Sperre, Wallbox 1 locked 1, Regelung-Neustart).
- 📊 **Anzeige/Auswertung:** Oberfläche neu gestaltet.
- 📊 **Anzeige/Auswertung:** Beschreibungstext erklärt den Unterschied der beiden Aktionen direkt in der Oberfläche.
- ✨ **Verbesserung:** Umlaut-Korrektur in Wallbox-Anbindung.
- ✨ **Verbesserung:** PV-Modussteuerung in openWB-Anbindung.
- 🐛 **Fehlerbehebung:** Batterie-Ruhezustand-Bug behoben (Netz-Zwangsladen unterdrückt).
- ✨ **Verbesserung:** Systemprotokoll-Transparenz.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zwei-Phasen Ladestrategie.
- ✨ **Verbesserung:** Phase 1 (Bulk, ~85%).
- ✨ **Verbesserung:** Phase 2 (Pre-Conditioning, ~15%).
- ✨ **Verbesserung:** Just-In-Time Bewertung.
- ✨ **Verbesserung:** 15-Minuten Raster.
- 🔄 **Migration/Kompatibilität:** SoC-Rückfall-Kette.
- 🐛 **Fehlerbehebung:** SoC-Warnung Spam behoben.
- 📊 **Anzeige/Auswertung:** Web-Oberfläche "Verbindungsfehler" bei Vitaldaten behoben.
- 🐛 **Fehlerbehebung:** Falscher Hausverbrauch/Netzbezug behoben.
- ⚙️ **Regelung:** E3DC weather-Regelung Crash nach erstem Zyklus behoben.



### 📦 Distribution und Kompatibilität

- ⚙️ **Regelung:** Docker Installations- und Aktualisierungsablauf — Weather-Regelung lief nie.

## [3.9.3] – 2026-04-14

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** Protokoll-Refactoring Wallbox-Anbindung.
- ✨ **Verbesserung:** 2-Phasen Sofortladen.
- ✨ **Verbesserung:** Echtzeitdaten Verarbeitung.
- ✨ **Verbesserung:** Befehl-Proxy Wallbox-Bedienung.
- 🔎 **Diagnose:** openWB Echtzeit-Status Karte.
- ✨ **Verbesserung:** Vollständiger heller Modus.
- ✨ **Verbesserung:** Quick-Control Schaltflächen.
- ✨ **Verbesserung:** Statusanzeige-Blinken Korrektur.
- ✨ **Verbesserung:** Sofortiger Erst-Fetch.
- ✨ **Verbesserung:** Phase-Switching Statusanzeige.
- 🔄 **Migration/Kompatibilität:** SoC-Rückfall-Kette.
- ✨ **Verbesserung:** 15-Min-Granularität.
- 🔎 **Diagnose:** Energieplanung Status.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche obsolet.

## [3.9.2] – 2026-04-13

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Eigene Fahrzeug-Vorlagen (Offline / Gast-Fahrzeuge).
- ✨ **Verbesserung:** KI-Hausverbrauch Wallbox-Filter (ML-Prognose).

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** V4 Speicherplanung (Speicherplanung).
- ✨ **Verbesserung:** V4 PV-Prognose Ensemble Korrektur (48h/72h Interpolation).
- ✨ **Verbesserung:** Korrektur der PV-Prognose (Summierung).
- ✨ **Verbesserung:** Systemprotokoll-Spam Wallboxregelung.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Vollständige Installationsassistent-Integration.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Docker V4 Kompatibilität.

## [3.9.1] – 2026-04-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** 15-Minuten SMARD Intraday-Preise.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Kopfzeile-Navigation.
- ✨ **Verbesserung:** Netzbezug Layout-Stabilität.
- ✨ **Verbesserung:** Spotmarkt Börsenstrompreis Flatline-Korrektur.

### 🛠️ Installation und Update

- 📊 **Anzeige/Auswertung:** Oberfläche-Rückfall bei Dienst-Neustart.

## [3.9.0] – 2026-04-12

### 🔋 Storage Manager

- ✨ **Verbesserung:** Historische PV-Erträge werden mit dem vom E3DC erwarteten Zeitformat abgefragt und aus der verlässlichen Gleichstrom-Erzeugungsmessung gebildet.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Wallbox Datenbank-Repair.
- ✨ **Verbesserung:** Absolute Zähler für Tages-Schritte.
- ✨ **Verbesserung:** Der Systemwächter arbeitet stabiler.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Gehirn als Single-Source-of-Truth.
- ✨ **Verbesserung:** Lückenlose SMARD-Daten.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Hybrid-Diagramm Preisschichten.
- ✨ **Verbesserung:** IOS Layout-Korrekturen.
- ✨ **Verbesserung:** Preis-Ticket Korrektur.
- ✨ **Verbesserung:** Speichern-Schaltflächen Überlappung.
- ✨ **Verbesserung:** Mobilansicht-Navbar Color-Coding.

## [3.8.9.2] – 2026-04-11

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** RSCP Auto-Unlock (Release-Kommando).
- ✨ **Verbesserung:** PV-Überschuss Zwangssperren-Ausnahme.
- ✨ **Verbesserung:** Smarter Automatikmodus (Netz).
- 🧱 **Stabilität:** Batterie Virtual-Kickstart & Hysterese.
- 🐛 **Fehlerbehebung:** Neustart und Rechteverwaltung der Hintergrunddienste wurden korrigiert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Zirkulations-Bugfix.

## [3.8.9.1] – 2026-04-10

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Echtzeit-Controls für Beide Wallboxen.
- ✨ **Verbesserung:** DYNAMISCHES UMBENENNEN.
- ✨ **Verbesserung:** Intelligente Sichtbarkeit.
- 🛡️ **Sicherheit:** Physische Wallboxsperre: Eine über die Weboberfläche gesperrte Wallbox wird mit dem offiziellen E3DC-Stoppbefehl abgeschaltet; die Schütze öffnen hörbar.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wolken-Glätter (Min+PV).

## [3.8.9] – 2026-04-10

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Rechtekorrekturen zeigen nur tatsächlich ausgeführte Änderungen und können einen erforderlichen Neustart kontrolliert auslösen.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Konfigurationsseite, Speicherstatus, Hinweistexte und Mobilansicht Wallbox-Schieberegler wurden übersichtlicher gestaltet.

## [3.8.8.12.6] – 2026-04-09

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Installationsassistent Deadlock.
- ✨ **Verbesserung:** Direkt angebundene Wallbox Statusanzeige.

## [3.8.8.12.5] – 2026-04-09

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Die MQTT-Abfrage von Wallboxdaten ist mit aktuellen Mosquitto-Versionen kompatibel; ein Verbindungsabbruch blockiert die Hauptregelung nicht mehr.

## [3.8.8.12.4] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Batterie-Entladestop.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfigurationsseite Optimierung.
- ✨ **Verbesserung:** Empty-Field Handling.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker-Dienst Detection.

## [3.8.8.12.3] – 2026-04-09

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Hausverbrauch-Korrektur.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** MQTT Hub Multi-Broker SoC.

## [3.8.8.12.2] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB 2.0 MQTT-Kanal-Automatik.
- ✨ **Verbesserung:** Dummy-Wallbox Visibilität.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Persistenz-Korrektur (Weboberfläche Abgleich).
- ✨ **Verbesserung:** Wallbox Amperage Float-Korrektur.
- ✨ **Verbesserung:** Bare-Metal Schreibrechte.

## [3.8.8.12.1] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** openWB / MQTT direkt angebunden Unterstützung.
- 📊 **Anzeige/Auswertung:** Web-Oberfläche Konfiguration.

## [3.8.8.12] – 2026-04-09

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Dual-Charger Unterstützung.
- ✨ **Verbesserung:** Echtzeit-Phasenerkennung.
- 📊 **Anzeige/Auswertung:** Echtzeit Oberfläche Telemetrie.
- ✨ **Verbesserung:** Betriebssicherheit.

## [3.8.8.11] – 2026-04-09

### 🔋 Storage Manager

- ✨ **Verbesserung:** Historischer historische Energiemessung Zähler (Gesamt gerettet).

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Warmwasser Sollwert Korrektur.
- ⚙️ **Regelung:** iDM Vorlauf-Regelung für Notabbruch.
- ✨ **Verbesserung:** Erweiterte Speicherplanung vs. Sperrzeiten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wärmepumpen Layout-Vereinheitlichung.
- 📊 **Anzeige/Auswertung:** iDM Modbus-Werte & Oberfläche.
- ✨ **Verbesserung:** Außentemperatur Main-Weboberfläche.

## [3.8.8.10.6] – 2026-04-08

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Modbus Kollisionsverbot (iDM & Energiemanagement).

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Historische Energiemessung Rückfall-Sicherung.

## [3.8.8.10.5] – 2026-04-08

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Exakte Tageszähler (Anti-Drift).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** IOS PWA Monatswahl.

## [3.8.8.10.4] – 2026-04-08

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** BOM-Fehler bei E3DC-Abfrage behoben.
- 🐛 **Fehlerbehebung:** Permission Error auf manuell Leistungsanhebung Statusangaben behoben.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Einstellungen werden im laufenden manuellen Leistungsanhebung sofort übernommen.
- ✨ **Verbesserung:** WW-Sperrzeit-Ausnahme (experimentell).
- ⚙️ **Regelung:** Wärmepumpe data an alle WW-aktivierenden Leistungsanhebung-Steuerung-Aufrufe übergeben.
- 🔎 **Diagnose:** iDM Externe Anforderungen sichtbar.
- 🔎 **Diagnose:** iDM Status-Felder vollständig aus aktuellen Betriebsdaten.
- ✨ **Verbesserung:** Tages-AZ nutzt iDM-eigene kumulative Zähler.
- 📊 **Anzeige/Auswertung:** Optisches Oberfläche-Upgrade (Sensoren).
- ✨ **Verbesserung:** iDM Manueller Leistungsanhebung startet jetzt sofort.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** ASCII-Kompatibilität für Pi-Terminal.

## [3.8.8.10.2] – 2026-04-07

### 🔋 Storage Manager

- ✨ **Verbesserung:** Batterie-Gradient (Energiefluss).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Symmetrische Zeitachse.
- ✨ **Verbesserung:** Gleichmäßige Datenpunktdichte.
- ✨ **Verbesserung:** Viertelstunden-X-Achse.
- ✨ **Verbesserung:** Mitternachts-Überlauf-Korrektur (48h).
- ✨ **Verbesserung:** Mobilansicht Farbschema-Korrektur.
- ✨ **Verbesserung:** Hilfe-Seite Farbschema-Synchronisation.

## [3.8.8.10.1] – 2026-04-07

### 🔋 Storage Manager

- ✨ **Verbesserung:** Batteriedrift (Zellspannung) RSCP Korrektur.

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Web-Oberfläche "Geister-Updates" behoben.

## [3.8.8.10] – 2026-04-07

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Systemprotokoll-Spam Eliminierung.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Kühlung für Kältespeicher (Register 1711 & 1010).
- 🧱 **Stabilität:** Umgekehrte Hysterese & Konfigurierbarkeit.
- ✨ **Verbesserung:** Manueller Sommer-Leistungsanhebung.
- 🔄 **Migration/Kompatibilität:** Smart Netz Vollgas (1006 = 2) Update.

## [3.8.8.9] – 2026-04-07

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Historische Energiemessungen für Peak Shaving bleiben bei Aktualisierungen erhalten.
- 🔎 **Diagnose:** Fehlen einzelne Zelltemperaturen oder Ladezyklen, nutzt die Batterieansicht belastbare Werte des Batterieverbunds.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Prognoseszenarien, dynamische Lastverschiebung und die Auswertung vermiedener Lastspitzen wurden erweitert.
- 🛡️ **Sicherheit:** Inselbetrieb und autonome Rückfallfunktionen bleiben gegenüber Komfortoptimierungen vorrangig.

### ♨️ Wärmepumpe und Wärme

- 🐛 **Fehlerbehebung:** Unterbrochene Modbus-Verbindungen werden erkannt und sauber neu aufgebaut.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Matter- und Push-Anbindungen wurden als optionale Smart-Home-Schnittstellen erweitert.

## [3.8.8.8.2] – 2026-04-06

### 🔋 Storage Manager

- ✨ **Verbesserung:** Verbesserte Triggersicherheit.
- 📊 **Anzeige/Auswertung:** Prozess-Visualisierung (Oberfläche).
- ✨ **Verbesserung:** Automatische Kollisionsvermeidung.
- ✨ **Verbesserung:** iDM Wärmepumpen Integration.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Bessere Fehlermeldung.

## [3.8.8.8] – 2026-04-06

### 🔋 Storage Manager

- ✨ **Verbesserung:** Die Übersicht zeigt die insgesamt vermiedenen Lastspitzen.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Hausverbrauch-Bereinigung.

### ♨️ Wärmepumpe und Wärme

- 📊 **Anzeige/Auswertung:** Modbus Vorgaben-Oberfläche.
- ✨ **Verbesserung:** Vorlauf-Korrektur.
- ✨ **Verbesserung:** Sommer/Winter Indikator.
- ✨ **Verbesserung:** Dynamischer Leistungsanhebung-Change.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Historische Energiemessung.
- 📊 **Anzeige/Auswertung:** Lade-Fahrplan (Regler) Anzeige.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Doppelter Menüpunkt behoben.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker: Web-Push (pywebpush) Korrektur.
- ✨ **Verbesserung:** Die Echtzeitverarbeitung erkennt Docker-Installationen zuverlässiger.

## [3.8.8.7] – 2026-04-05

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Menüpunkt 1 wiederhergestellt.
- ✨ **Verbesserung:** RSCP-Diagnosewerkzeug Erstinstallation über Menü.

## [3.8.8.6] – 2026-04-05

### 🔋 Storage Manager

- ✨ **Verbesserung:** Neues Vitals-Weboberfläche ("Batterie-Gesundheit").
- ✨ **Verbesserung:** Zustand of Health (SOH) pro Pack.
- ✨ **Verbesserung:** Zell-Temperatur (Min/Max).
- ✨ **Verbesserung:** Zell-Drift (Spannungs-Spread).
- ✨ **Verbesserung:** Firmware & Software-Version.
- ✨ **Verbesserung:** Automatische Filterung (Geister-Packs).
- ✨ **Verbesserung:** RSCP-Diagnosewerkzeug Automatisch-Installation.
- 🔄 **Migration/Kompatibilität:** E3DC Firmware-Kompatibilität.
- ✨ **Verbesserung:** E3DC Firmware-Bug-Filter.
- ✨ **Verbesserung:** Drittanbieter-Abhängigkeit.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Rechte-Integration.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Vitals-Ansicht.
- ✨ **Verbesserung:** Kachel-Stabilisierung (Mobilansicht).

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Update-Regelung Integration.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Konfigurierbare Fahrzeugnamen im MQTT-Hub.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker-Unterstützung.

## [3.8.8.5] – 2026-04-05

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Finale 3-Schalter-Logik.
- ✨ **Verbesserung:** Lösung des Umbenennungs-Problems.
- ✨ **Verbesserung:** Legalität des Control Nodes.

## [3.8.8.4] – 2026-04-05

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Hardware vs. Software Fail-Safe.
- ✨ **Verbesserung:** Grundlast-Filter (Noise Elimination).

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Echtzeit-Anzeige JIT-Extraktion.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Shelly Pro 3EM Schnittstelle-Unterstützung (Gen2).

## [3.8.8.3] – 2026-04-04

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Der Neustart der Wärmepumpenanbindung aus der Weboberfläche wurde abgesichert.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Laufende Verbindung Lock-Problem gelöst.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Matter Oberfläche-Anzeige.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Modbus TCP Korrektur ("Broken Pipe").
- ✨ **Verbesserung:** M dns Proxy (Avahi).
- ✨ **Verbesserung:** 3 Echtzeit-Schalter.
- ✨ **Verbesserung:** Wallbox: An wenn Fahrzeug lädt (>50W).
- ✨ **Verbesserung:** PV-Produktion: An wenn Solaranlage produziert (>500W).
- ✨ **Verbesserung:** Netz-Einspeisung: An wenn Strom-Überschuss ins Netz fließt (>200W).
- ✨ **Verbesserung:** Automations-Trigger.
- 🧱 **Stabilität:** LAN-Pairing stabil.
- ✨ **Verbesserung:** Dienstverwaltung-Abhängigkeit.
- ✨ **Verbesserung:** Installationsassistent-Prerequisite.
- ✨ **Verbesserung:** Weboberfläche-Integration.

## [3.8.8.2] – 2026-04-03

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Peak-Ersparnis (Kuppe).
- ✨ **Verbesserung:** Dynamisches Netzeinspeise-Limit.
- ✨ **Verbesserung:** Geisterlinien Korrektur.
- ✨ **Verbesserung:** Permanente Aktivierung.
- ✨ **Verbesserung:** Langzeit-Archiv (Langzeitdatenbank).

## [3.8.8.1] – 2026-04-03

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Die erzeugten Systemdienste verwenden eine zeitgemäße Protokollausgabe und vermeiden wiederkehrende Warnungen auf aktuellen Linux-Distributionen.
- 🔎 **Diagnose:** Der Echtzeitstatus bleibt bei parallelen Zugriffen zuverlässig verfügbar.

### ♨️ Wärmepumpe und Wärme

- ⚙️ **Regelung:** Die iDM-Wärmepumpe erhält den verfügbaren PV-Überschuss direkt und stufenlos über Modbus TCP. Begrenzte Aktualisierungsintervalle und ein Überhitzungsschutz verhindern unnötige Stellwertwechsel.
- 🐛 **Fehlerbehebung:** Nicht vorhandene Messgrößen einer Luft/Wasser-Wärmepumpe werden nicht mehr durch künstliche Platzhalterwerte ersetzt; Historie und Anzeige enthalten nur tatsächlich verfügbare Daten.
- 🔎 **Diagnose:** Der an die iDM-Wärmepumpe übertragene PV-Überschuss ist in der Wärmepumpenansicht in Echtzeit sichtbar.
- ✨ **Verbesserung:** Dynamische COP-Farben.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Echtzeit-Diagramm Rückfall.
- 📊 **Anzeige/Auswertung:** Dynamische Diagramm-Auflösung.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Virtual Device Node (Hintergrunddienst).
- ✨ **Verbesserung:** Sicherer Zertifikats-Speicher.
- ✨ **Verbesserung:** Hidden Weboberfläche Pairing-GUI.
- ✨ **Verbesserung:** Erfolgreicher Koppelungs-Durchbruch.

## [3.8.8] – 2026-04-02

### 📈 Direktvermarktung und Strompreise

- ⚙️ **Regelung:** Bei negativen Strompreisen kann eine ausdrücklich freigegebene automatische Ladung starten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Bedienaktionen und der Plug-and-Charge-Dialog wurden ergänzt.

### 🔗 Schnittstellen und Smart Home

- 🐛 **Fehlerbehebung:** Zeitüberschreitungen bei Shelly-Geräten werden sauber behandelt.

## [3.8.7.13] – 2026-04-02

### 🔋 Storage Manager

- 🛡️ **Sicherheit:** Notstrom & Inselbetrieb-Erkennung.

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Fahrzeugwechsel SoC Interpolation Korrektur.
- ✨ **Verbesserung:** Intelligente Ladeplanung Ladezeit-Berechnung.

### ☀️ PV, Prognose und Energiemanagement

- ⚙️ **Regelung:** Granulare Event-Steuerung.
- ✨ **Verbesserung:** HA-Failover & Boot-Meldungen.
- ✨ **Verbesserung:** Git Berechtigungen (Rechte-Reparatur).
- ✨ **Verbesserung:** Prognose-Korrektur (Zeitzonen & 48h-Horizont).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Wallbox GUI Layout.
- ✨ **Verbesserung:** Datenverlust beim Speichern.
- ✨ **Verbesserung:** Dynamischer Ladeplan-Zeitstrahl.
- 📊 **Anzeige/Auswertung:** Diagramm-Skalierung (Langzeit-Archiv).

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Smart-Update Benachrichtigung.
- 🔄 **Migration/Kompatibilität:** Installationsassistent automatische Aktualisierung optionale Aktivierung.

## [3.8.7.12] – 2026-04-02

### 🔌 Wallbox Manager

- 🐛 **Fehlerbehebung:** Leere SoC-Meldungen von EVCC beim Abstecken eines Fahrzeugs führen nicht mehr zum Abbruch des Energiemanagements.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Präziser Tagesstart (Midnight-Hangover Korrektur).
- ✨ **Verbesserung:** VAPID Security-Fundament.

### ♨️ Wärmepumpe und Wärme

- 📊 **Anzeige/Auswertung:** Heizstab Oberfläche-Rückfall.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Editor-Dialog Korrektur.
- ✨ **Verbesserung:** Wallbox-Ladehistorie Detailansicht.
- ✨ **Verbesserung:** Geräte-Registrierung via PWA.
- ✨ **Verbesserung:** Delta-Energiezählung.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Home Assistant Auth Unterstützung.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.8.7.11] – 2026-04-01

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Die Wallboxsteuerung verarbeitet Rückmeldungen in einer eindeutigen Reihenfolge.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Kein Datenverlust bei Updates.
- 🛡️ **Sicherheit:** Automatischer SD-Karten-Schutz.



### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Delta-Energiezählung.
- ✨ **Verbesserung:** HTML5 direkt angebunden Pickers.
- ✨ **Verbesserung:** Dynamische Daten-Limits (Min/Max).
- ✨ **Verbesserung:** Grenzenlos Analysieren.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.8.7.10] – 2026-03-31

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Modbus laufende Verbindung (Standleitung).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Korrektur für blockiertes Weboberfläche (Ladezeiten).
- ✨ **Verbesserung:** Offline-Caching & Micro-Timeouts.

## [3.8.7.9] – 2026-03-31

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Dynamisches Diagramm-Limit.
- ✨ **Verbesserung:** Korrektur Reference Error (Statische Strompreise).
- ✨ **Verbesserung:** Fehlende Prognosedaten erzeugen keine Warnung mehr in der Weboberfläche.
- ✨ **Verbesserung:** Echtzeitverbindung-Systemprotokoll Silence.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Betriebsarten-Zuordnungen (Wärmepumpen-Bedienung).
- ✨ **Verbesserung:** Reaktivierung der Detail-Kacheln.
- 🐛 **Fehlerbehebung:** Die Außentemperatur der Wärmepumpe wird trotz historisch unterschiedlicher Bezeichnungen korrekt angezeigt.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Massiver Datenbank Speed-Up.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Keine redundanten Update-Abfragen.
- ✨ **Verbesserung:** Vollautomatische Rechteprüfung.

## [3.8.7.8] – 2026-03-31

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Statistik-Upgrade (Jahr/Monat Filter & Deep Drilldown).
- ✨ **Verbesserung:** Reboot-Datensicherheit.
- 📊 **Anzeige/Auswertung:** Korrektur Statistik-JS.
- ✨ **Verbesserung:** System-Stabilität.

### 📈 Direktvermarktung und Strompreise

- 📊 **Anzeige/Auswertung:** Präzise Strompreis-Anzeige.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Visualisierungs-Paket für Langzeit-Bilanzen.

## [3.8.7.7] – 2026-03-31

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zuluftregister (Adr 1060).
- ⚙️ **Regelung:** Aktive Überschuss-Steuerung (Adr 74/76).
- ✨ **Verbesserung:** Performance-Leistungsanhebung.
- ✨ **Verbesserung:** Präzise Tages-Arbeitszahl (AZ).
- 📊 **Anzeige/Auswertung:** Oberfläche-Erweiterung.
- 📊 **Anzeige/Auswertung:** Statistik Hardware-Abgleich.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Tiered Price Weboberfläche Unterstützung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Die Wallboxzuordnung wurde zuverlässiger.
- ✨ **Verbesserung:** Mobilansicht Optimierung (Heizstab).
- ✨ **Verbesserung:** Korrektur Wärmepumpen-Bedienung.
- ✨ **Verbesserung:** Wiederherstellung iDM-Navigation.
- ✨ **Verbesserung:** Heizstab-Visualisierung (Wattage).
- ✨ **Verbesserung:** Navigation-Unlinking.
- ✨ **Verbesserung:** Bereinigung-Routine.
- ✨ **Verbesserung:** Release-Packaging.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Verbesserte evcc-Anbindung.

## [3.8.7.6] – 2026-03-29

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** CO₂-Baum (Desktop & Mobilansicht).
- ✨ **Verbesserung:** Mobilansicht Integration.
- ✨ **Verbesserung:** Farbschema-Wechsel Crash Korrektur.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Strompreis-Korrektur (Astronomische Werte).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Zentriertes 4-Spalten-Layout.
- ✨ **Verbesserung:** Interaktive Legende.
- ✨ **Verbesserung:** Mobilansicht Energiebilanz-Statusanzeige.

## [3.8.7.5] – 2026-03-29

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Echtzeit-Power Korrektur.
- ✨ **Verbesserung:** Bluelink SoC-Interpolation.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Direkten Webaufruf Latency Korrektur.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Korrektur iDM-Crash.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Farbschema-Kompatibilität (Bright Modus).

## [3.8.7.4] – 2026-03-29

### 🔌 Wallbox Manager

- 📊 **Anzeige/Auswertung:** Direkt angebunden Dual-Car Oberfläche Unterstützung.
- ✨ **Verbesserung:** Intelligente Ladeplanung Abgleich.
- 📊 **Anzeige/Auswertung:** Oberfläche-Indikator.

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Statistik-Integrität.
- 📊 **Anzeige/Auswertung:** Oberfläche-Entkopplung.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Verbesserte iDM-Symbolik.
- ✨ **Verbesserung:** Interaktive Betriebsart.
- ✨ **Verbesserung:** Optimiertes iDM-Zuordnungen.
- ✨ **Verbesserung:** Tages-AZ Berechnung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Statischer Tarif (Kostensimulation).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Editor für Tageswerte.

## [3.8.7.3] – 2026-03-29

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Korrektur Wallbox-Summation.
- 📊 **Anzeige/Auswertung:** Zweispaltige Wallbox-Oberfläche.
- ✨ **Verbesserung:** Unabhängige SoC-Kalkulation.
- ✨ **Verbesserung:** Exklusive Zuweisung & Gast-Modus.
- ✨ **Verbesserung:** Dynamische Ladeplanung (manueller Ladeplan).
- ✨ **Verbesserung:** Multi-Car MQTT SoC.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Individuelles Styling.
- ✨ **Verbesserung:** Layout-Entzerrung (Anti-Overlap).
- ✨ **Verbesserung:** Desktop-Optimierung.
- ✨ **Verbesserung:** Mobilansicht-Optimierung.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** iDM Modbus-TCP Dienst.
- ✨ **Verbesserung:** Word-Swap Automatik.
- ✨ **Verbesserung:** Neues Datenformat (Wärmepumpendaten).
- ✨ **Verbesserung:** Passives Monitoring-Konzept.
- 🔄 **Migration/Kompatibilität:** Installationsassistent-Update.

## [3.8.7.2] – 2026-03-28

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Zweit-Wallbox Integration.
- ✨ **Verbesserung:** Getrennte Echtzeit-Visualisierung.
- 📊 **Anzeige/Auswertung:** Unabhängige Statistik.
- ✨ **Verbesserung:** Hausverbrauchs-Korrektur.
- ✨ **Verbesserung:** Konfigurations-Wizard.

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Langzeit-Statistik (Heatpump Hide).
- ✨ **Verbesserung:** Hauptsystem-Only Notifications.
- 🔎 **Diagnose:** Cluster-Status im Report.

## [3.8.7.1] – 2026-03-28

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Oberfläche-Stabilisierung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Energiemanagement HT/NT-Leistungsanhebung.
- ✨ **Verbesserung:** Wallbox Kostenvorschau.
- ✨ **Verbesserung:** Luxtronik Call-Safety.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Automatisches Querformat Layout.
- ✨ **Verbesserung:** Intelligentes Ausblenden (Preis-Trend).

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Anti-Spam Filter (Boot-Debounce).
- ✨ **Verbesserung:** Energiefluss-Branding.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Host-Networking Unterstützung.

## [3.8.7] – 2026-03-27

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Anti-Korruptions-Filter (Langzeit-Diagramm).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Netzqualität Weboberfläche (Schieflast-Ampel).
- ✨ **Verbesserung:** Daily Min/Max Peaks.
- ⚙️ **Regelung:** Konfigurierbare SLS-Grenze.
- ✨ **Verbesserung:** Wallbox-Sub-Metering.
- ✨ **Verbesserung:** Feststehend Kopfzeile mit Milchglaseffekt-Effekt.
- ✨ **Verbesserung:** Uhranzeige im heller Modus repariert.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Intelligentes Modbus laufende Verbindung & Spam-Filter.
- 🐛 **Fehlerbehebung:** Luxtronik Sommer-Modus Modbus Korrektur (Fehler 1313).
- ✨ **Verbesserung:** Docker-Aware Maintenance Skripte.

## [3.8.6.9] – 2026-03-27

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Fahrzeug-Vorlagen Speichern (Neu!).
- ✨ **Verbesserung:** Präzise Gast-Reichweitenberechnung.
- ✨ **Verbesserung:** Manueller Fahrzeug-SoC & Restladezeit (Interpolation).
- ✨ **Verbesserung:** Fahrzeug-Detail Ansicht.
- 📊 **Anzeige/Auswertung:** Oberfläche Konsistenz.
- ✨ **Verbesserung:** Dynamische Ladezeit-Berechnung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Dezente Kachel-Details (Auge-Symbole).
- ✨ **Verbesserung:** PV-Kachel Reorganisation.
- ✨ **Verbesserung:** Batterie-Kachel Design.
- ✨ **Verbesserung:** Intelligentes Ausblenden (Fremd-Wallboxen).

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Tagesstatistik Upgrade.
- ✨ **Verbesserung:** MQTT IP/Port Parse.

## [3.8.6.8] – 2026-03-26

### ☀️ PV, Prognose und Energiemanagement

- 📊 **Anzeige/Auswertung:** Langzeit-Diagramm Abweichung (Heute).
- ✨ **Verbesserung:** Phantom-Tag Generator.
- ✨ **Verbesserung:** Exakte Einspeisezähler.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Minifizierung & Performance.
- ✨ **Verbesserung:** Batterie-Kachel Alarme.
- 📊 **Anzeige/Auswertung:** Wärmepumpen-Phantom Oberfläche (Shelly).

## [3.8.6.7] – 2026-03-26

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Multi-Wallbox Vorbereitung.
- ✨ **Verbesserung:** Installationsassistent-Anpassung.



### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Endlos-Update-Loop.

## [3.8.6.6] – 2026-03-26

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Kugelsicherer Energiezählung.

### 🖥️ Weboberfläche

- 🔄 **Migration/Kompatibilität:** Nahtloses Weboberfläche-Update.
- ✨ **Verbesserung:** Echtzeit-Feedback.

## [3.8.6.5] – 2026-03-25

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Kaskadierte Entladung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wärmepumpen-Phantomverbrauch.
- ✨ **Verbesserung:** Fehlender Hausverbrauch.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent Rettungs-Ring.

## [3.8.6.4] – 2026-03-25

### 🔌 Wallbox Manager

- ⚙️ **Regelung:** Agnostische V2H-Steuerung.
- ✨ **Verbesserung:** MQTT automatische Erkennung.
- ✨ **Verbesserung:** Energiefluss Visualisierung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Erweiterte KI-Szenarien.
- ✨ **Verbesserung:** Dynamische Lastverschiebung (Haushaltsgeräte).
- ✨ **Verbesserung:** Kostensimulations-Korrektur.
- ✨ **Verbesserung:** Systemprotokoll-Reihenfolge.

## [3.8.6.3] – 2026-03-25

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** go-e & Betriebsdaten MQTT Unterstützung.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Schnittstelle Upgrade.
- ✨ **Verbesserung:** Strompreis-Editor Korrektur.

## [3.8.6.2] – 2026-03-24

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Kontextsensitives Menü.
- ✨ **Verbesserung:** Systemprotokoll-Spam Filter.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Docker Echtzeit-Anzeige.
- ✨ **Verbesserung:** KI-Rohdaten Visualisierung.
- 📊 **Anzeige/Auswertung:** Fahrzeuganzeige (openWB).

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Start-Logik.

## [3.8.6.1] – 2026-03-24

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Direkt angebunden openWB-Integration.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Survival Modus (Inselnetz).
- ✨ **Verbesserung:** Scikit-Learn Integration.

## [3.8.6] – 2026-03-24

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Prädiktive Analytik & Machine Learning.
- ✨ **Verbesserung:** Systemautonomie & Resilienz (Inselnetz).
- ✨ **Verbesserung:** Phasengenaue Auslastungs-Visualisierung.
- ✨ **Verbesserung:** Netzdienliche Verbrauchersteuerung & V2X.
- 🔎 **Diagnose:** Frühwarnsystem (Auto-Diagnose).
- ✨ **Verbesserung:** Wärmepumpen-Fehlerspeicher.
- ✨ **Verbesserung:** Watchtower-Systemprotokoll.
- 🧱 **Stabilität:** Auskühlschutz (Hysterese).
- ✨ **Verbesserung:** Physikalischer SoC-Filter.
- ✨ **Verbesserung:** Verborgene Voreinstellungen werden bei der Konfigurationsprüfung erkannt.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Mobilansicht Strompreis-Ticker.

### 🖥️ Weboberfläche

- 🛡️ **Sicherheit:** Weboberfläche PIN-Schutz.
- 📊 **Anzeige/Auswertung:** E3DC Echtzeit-Anzeige.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Smart-Home Evolution (Matter & Push).

## [3.8.5.6] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Die lokale Lernfunktion berücksichtigt den durchschnittlichen Verbrauch von Haus, Wärmepumpe und Wallbox über mehrere Tage.
- ⚙️ **Regelung:** Ist kein Fahrzeug als steuerbarer Verbraucher verfügbar, kann eine ausdrücklich freigegebene Wärmepumpe günstige Energie nutzen.

### 📦 Distribution und Kompatibilität

- 🛡️ **Sicherheit:** Docker-Installationen verwenden ein isoliertes Netzwerk mit ausdrücklich zugeordneten externen Anschlüssen.
- 🐛 **Fehlerbehebung:** Die Echtzeit-Diagnose friert auf Bare-Metal-Systemen nicht mehr ein; frische Installationen enthalten die für das Echtzeit-Diagramm benötigte Abhängigkeit.

## [3.8.5.5] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Altlasten-Entfernung.

### 🖥️ Weboberfläche

- 🔎 **Diagnose:** Zentrale Diagnose-Station.
- ✨ **Verbesserung:** Weboberfläche Bugfix.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Das Docker-Image unterstützt sowohl Intel-/AMD-Systeme als auch verbreitete Raspberry-Pi-Architekturen und kann dadurch auf NAS-Systemen und Mini-PCs betrieben werden.
- 📊 **Anzeige/Auswertung:** Docker Oberfläche-Sperre.

## [3.8.5.4] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Exakte Tageszähler.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Port-Routing Korrektur.

## [3.8.5.3] – 2026-03-23

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Frühere Steuerungstrukturierte Daten Erweiterung.
- ✨ **Verbesserung:** Echtzeitverbindung Port-Kollision.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Modbus-Verbindungen können dauerhaft gehalten und bei Unterbrechungen erneuert werden.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** HA-Standby Docker Korrektur.

## [3.8.5.2] – 2026-03-23

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Interaktive Port-Wahl.
- ✨ **Verbesserung:** Transparente Installation.
- ✨ **Verbesserung:** Sandbox & Monitoring.
- ✨ **Verbesserung:** Watchdog-Pausierung.

## [3.8.5.1] – 2026-03-23

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Windows-Reparatur.

## [3.8.5] – 2026-03-22

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Shelly Wallbox-Integration.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Restlose Iframe-Entfernung.
- ✨ **Verbesserung:** Sicherungen Erweiterung.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** DV-Balken (Börsen-Verkauf).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche Konsistenz.

## [3.8.4] – 2026-03-22

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Dynamische Fahrzeug-Tabs.
- ✨ **Verbesserung:** Intelligente Wallbox-Zuweisung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Beliebig viele Fahrzeuge.
- ✨ **Verbesserung:** Google Maps Integration.
- ✨ **Verbesserung:** Intelligenter Konfigurationsseite.
- 🛡️ **Sicherheit:** Eigene lokale Startanpassungen werden bei Web-Aktualisierungen nicht überschrieben und in Sicherungen einbezogen.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Universal-Shelly-Engine.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Bridge-Modus.

## [3.8.3.1] – 2026-03-21

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** WICHTIGER UPDATE-HINWEIS.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Doppelte Telegram-Nachrichten (HA).

## [3.8.3] – 2026-03-21

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Reale Stromkosten (Langzeitdatenbank).
- ✨ **Verbesserung:** Direkt angebunden frühere Steuerungstrukturierte Daten.
- ✨ **Verbesserung:** Heller Modus Neugestaltung.
- ✨ **Verbesserung:** Energiemanagement Netz-Puffer.
- ✨ **Verbesserung:** HA-Cluster Langzeit-Abgleich.

### 📈 Direktvermarktung und Strompreise

- ✨ **Verbesserung:** Smartes Preis-Weboberfläche.
- 📊 **Anzeige/Auswertung:** Preis & Kosten-Diagramm.



### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Git-Update Korrektur.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.8.2] – 2026-03-20

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Echtzeit-Lade-Energiezählung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Langzeit-Statistiken.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** MQTT automatische Erkennung.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Vollständiger Docker-Unterstützung.
- ⚙️ **Regelung:** Zentraler Ladeplan Regelung.
- ✨ **Verbesserung:** Saubere Deinstallation.

## [3.8.1] – 2026-03-20

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Geräuschlose Überwachung.
- ✨ **Verbesserung:** Verdichter-Erkennung.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Git-Update Korrektur.
- 🔄 **Migration/Kompatibilität:** Aktuelle Steuerung3.7 Kompatibilität.
- 🔎 **Diagnose:** Nach dem Ende des Installers erscheint eine verständliche Zusammenfassung von Erfolgen und Fehlern.

## [3.8.0] – 2026-03-19

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Atomares Betriebsdaten.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Millisekunden-Rendering.
- ✨ **Verbesserung:** Ressourcen-Befreiung.
- ✨ **Verbesserung:** Interaktive Diagramme.
- ✨ **Verbesserung:** Smartes Gedächtnis.
- ✨ **Verbesserung:** Absolutwerte-Modus.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Offizieller Docker-Unterstützung.
- ✨ **Verbesserung:** Erweiterte Docker-Nutzung.
- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.7.1] – 2026-03-18

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Erweitertes Standby.
- ✨ **Verbesserung:** Watchdog-Korrektur.
- 📊 **Anzeige/Auswertung:** Oberfläche-Entkopplung.
- ✨ **Verbesserung:** Geister-Benachrichtigungen.
- ✨ **Verbesserung:** Temperatur-Auslesung.

## [3.7.0] – 2026-03-18

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Gastfahrzeug Erkennung.
- 🧱 **Stabilität:** Verfeinerte Lade-Hysterese.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Ressourcenschonend.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Performance-Leistungsanhebung.
- ✨ **Verbesserung:** Modbus-Entlastung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Heizstab-Visualisierung.
- ✨ **Verbesserung:** Dynamisches Layout.
- ✨ **Verbesserung:** Butterweiche Animationen.

## [3.6.0] – 2026-03-17

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Multi-EV Unterstützung.
- ✨ **Verbesserung:** Datenbank-Archivar.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Weboberfläche-Anzeige.
- ✨ **Verbesserung:** Weboberfläche-Graphen.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Bugfix Installationsassistent.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Smart Home Adapter.
- ✨ **Verbesserung:** Telegram-Leistungsanhebung.
- ✨ **Verbesserung:** Lokaler MQTT-Broker.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Docker Containerisierung.

## [3.5.2] – 2026-03-16

### 🔋 Storage Manager

- ✨ **Verbesserung:** Doppel-Batterie Unterstützung.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Ultra-flüssige Oberfläche.
- ✨ **Verbesserung:** Apache Webweiterleitung.
- ✨ **Verbesserung:** Automatische Wiederherstellung.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Entkoppelte Architektur.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Beim geordneten Beenden in einer System- oder Docker-Umgebung werden laufende Statistiken vor dem Prozessende gespeichert.

## [3.5.1] – 2026-03-16

### ☀️ PV, Prognose und Energiemanagement

- 🛡️ **Sicherheit:** Gerätenamen-Schutz.
- ✨ **Verbesserung:** Phantom-Ordner Bug.
- ✨ **Verbesserung:** Konfigurationsseite Case-Sensitivity.

## [3.5.0] – 2026-03-16

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Kugelsicherer Hintergrunddienste.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-benötigte Systemkomponente Integration.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Zentraler Notification-Dienst.

## [3.4.3] – 2026-03-15

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Sicherheitsnetz Installationsassistent-Nutzer.

## [3.4.2] – 2026-03-15

### ☀️ PV, Prognose und Energiemanagement

- 🐛 **Fehlerbehebung:** Unbeabsichtigte Abweichungen der Einstellungen werden verhindert.
- ✨ **Verbesserung:** Watchdog HA-Awareness.
- ✨ **Verbesserung:** Proaktive Zwischenspeicher-Rechte.
- ✨ **Verbesserung:** Rechte-Reparatur Leistungsanhebung.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Web-Update Korrektur.

## [3.4.1] – 2026-03-15

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Aktualisierung, Rechteverwaltung, Hochverfügbarkeit und Systemwächter wurden gemeinsam auf den korrigierten Installationsstand gebracht.

## [3.4.0] – 2026-03-15

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Aktiv/Passiv Cluster.
- ✨ **Verbesserung:** Hot & Warm Standby.
- ✨ **Verbesserung:** Echtzeit-Abgleich.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Integration.

## [3.3.6] – 2026-03-15

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Systemprotokolle werden begrenzt und rotiert; die Konsolenausgabe verarbeitet Umlaute zuverlässig.
- 🛡️ **Sicherheit:** Die Rechteprüfung erkennt und korrigiert unpassende Zugriffsrechte kontrolliert.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Der optionale Telegram-Bericht fasst den morgendlichen Anlagenstatus zusammen.

## [3.3.4] – 2026-03-14

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Dynamische Grundlast-Kompensation.
- ✨ **Verbesserung:** Thermische Preisanpassung (COP-Logik).
- ✨ **Verbesserung:** Auskühlschutz (PV-Pause).

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Konfigurationsseite.

## [3.3.3] – 2026-03-14

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Die Energieverwaltung berücksichtigt dynamisch geänderte Einstellungen bereits beim Systemstart.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Sicherung und Rückfall wurden in den Aktualisierungsablauf aufgenommen.

## [3.3.2] – 2026-03-14

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Neue Tagesstatistik.
- ✨ **Verbesserung:** Historien-Auswahl.
- ✨ **Verbesserung:** Autarkie-Glättung.
- ✨ **Verbesserung:** Datenauswertung-Performance.
- ✨ **Verbesserung:** Häufig gelesene Echtzeitwerte belasten den Datenträger deutlich weniger.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Autarkie & Eigenverbrauch.
- ✨ **Verbesserung:** Bedienung-Optimierung.
- ✨ **Verbesserung:** Flüssigere Diagramme.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent-Leistungsanhebung.
- 🔄 **Migration/Kompatibilität:** Korrektur (Self-Update).

## [3.3.1] – 2026-03-13

### 🛠️ Installation und Update

- 📊 **Anzeige/Auswertung:** Korrektur (Diagramm-Installation).

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Korrektur (Release-Ablauf).

## [3.3.0] – 2026-03-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Energiemanagement Caching.
- ✨ **Verbesserung:** Einheitlicher Konfiguration Editor.
- ✨ **Verbesserung:** Priorisierte Ansicht.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche-Zentralisierung.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Robuster automatische Aktualisierung.
- 🔄 **Migration/Kompatibilität:** Zentrale Update-Richtlinie.
- ✨ **Verbesserung:** Umfassende Rechte-Korrektur.
- 🔄 **Migration/Kompatibilität:** Dokumentations-Update.

## [3.2.8] – 2026-03-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Energiemanagement Caching.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Weboberfläche Bündelung.
- ✨ **Verbesserung:** Einheitlicher Konfiguration Editor.
- ✨ **Verbesserung:** Priorisierte Ansicht.

## [3.2.7] – 2026-03-12

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Konfiguration Editor.
- ✨ **Verbesserung:** Rechte-Management.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Unattended-Korrektur.

## [3.2.6] – 2026-03-11

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Smart Charging Oberfläche.
- ✨ **Verbesserung:** Die Konfigurationsseite blendet Wärmepumpeneinstellungen aus, wenn die Luxtronik-Anbindung deaktiviert ist.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Web-Portal Update.
- ⚙️ **Regelung:** Prozess-Steuerung.

## [3.2.5] – 2026-03-11

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Zentrale Konfiguration.
- ✨ **Verbesserung:** Smart-Logik (PV-Pause).
- ✨ **Verbesserung:** Energiemanagement.



### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Konfiguration Editor.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Automatisches Update.
- 🔄 **Migration/Kompatibilität:** Update-Richtlinie (Aktualisierungsrichtlinie).

## [3.2.4] – 2026-03-11

### ☀️ PV, Prognose und Energiemanagement

- 🛡️ **Sicherheit:** Reboot-Sicherheit.
- ⚙️ **Regelung:** Intelligente Pausen-Steuerung.
- ✨ **Verbesserung:** Bugfix (Abgleich-Check).
- ✨ **Verbesserung:** Bugfix (Schnittstelle-Call).
- ✨ **Verbesserung:** Die Verwaltung der Systemprotokolle wurde verbessert.

### 📈 Direktvermarktung und Strompreise

- 📊 **Anzeige/Auswertung:** Korrektur-Tarif-Anzeige.

## [3.2.3] – 2026-03-11

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Sicherheits-Neustart.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Luxtronik-Konfiguration.

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Release-Optimierung.

## [3.2.2] – 2026-03-10

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Erweiterte Speicherplanung.
- ✨ **Verbesserung:** Sicherheits-Neustart.
- 🛡️ **Sicherheit:** Reboot-Sicherheit.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Bugfix (Permissions).

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Neuer Installationsassistent.

## [3.2.1] – 2026-03-09

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Diese Wartungsversion führte den in 3.2.0 dokumentierten Funktionsstand fort und aktualisierte das ausgelieferte Installationspaket.

## [3.2.0] – 2026-03-09

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** PV-Prognose-Pause.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Negativ-Preis-Leistungsanhebung.
- ✨ **Verbesserung:** Logik-Optimierung.
- ✨ **Verbesserung:** Komplett neu strukturierte Einstellungsseite (Wärmepumpen-Bedienung).
- 📊 **Anzeige/Auswertung:** Auswahl des Rücklauf-Sensors (Intern/Extern) für Anzeige und Regelung.
- ✨ **Verbesserung:** Einstellbare Verzögerungen für Stop und manuellen Leistungsanhebung.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Anzeige von Warmwasser- und Rücklauftemperaturen direkt in der Kachel.
- ✨ **Verbesserung:** Detaillierte Statusanzeige (Auto PV, Auto €, Auto Pause).

## [3.1.2] – 2026-03-08

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Strompreis-Leistungsanhebung.
- ✨ **Verbesserung:** Zwangs-Leistungsanhebung.

## [3.1.1] – 2026-03-08

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** In Echtzeit Energiefluss.

## [3.1.0] – 2026-03-08

### ♨️ Wärmepumpe und Wärme

- 🔄 **Migration/Kompatibilität:** Luxtronik, Strompreisassistent und Systemdienste wurden in einen gemeinsamen Installations- und Aktualisierungsablauf überführt.

## [3.0.1] – 2026-03-07

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Rechte-Management.

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Globaler Schalter.
- ✨ **Verbesserung:** Vollständige Integration.
- ✨ **Verbesserung:** Auto-Leistungsanhebung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Auto-Leistungsanhebung Statusanzeige.
- ✨ **Verbesserung:** Mobilansicht Navigation.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Dienst-Integration.
- 🔄 **Migration/Kompatibilität:** Cloudflare-Kompatibilität.
- ✨ **Verbesserung:** Echtzeit-Systemprotokoll.
- ⚙️ **Regelung:** Prozess-Steuerung.
- 📊 **Anzeige/Auswertung:** Oberfläche-Feedback.

## [3.0.0] – 2026-03-07

### ♨️ Wärmepumpe und Wärme

- ✨ **Verbesserung:** Die Luxtronik-Anbindung wurde mit Auslesen, Regelung und manueller PV-Anhebung in den Installationsassistent aufgenommen.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Statusprüfung, Berechtigungen und Installationsablauf berücksichtigen die neue Wärmepumpenanbindung.

## [2.6.5] – 2026-03-05

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Rechteprüfung und Aktualisierungsablauf wurden gemeinsam gehärtet, damit bestehende Installationen zuverlässig auf den neuen Stand wechseln.

## [2.6.4.1] – 2026-03-05

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Weboberfläche und Hintergrunddienste können gemeinsam benötigte aktuelle Betriebsdaten wieder zuverlässig verwenden.

## [2.6.4] – 2026-03-04

### 🔌 Wallbox Manager

- 🔎 **Diagnose:** Status-Visualisierung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Echtzeit-Grabber Dienst.
- ✨ **Verbesserung:** Atomares Schreiben.
- ✨ **Verbesserung:** Watchdog-Bereinigung.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Erweiterte Details.
- 📊 **Anzeige/Auswertung:** Echtzeit-Diagramm.
- ✨ **Verbesserung:** Weboberfläche-Validierung.

## [2.6.3] – 2026-03-04

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Konflikt-Erkennung.

## [2.6.2.1] – 2026-03-03

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die Erzeugung der Erstkonfiguration wurde korrigiert und gemeinsam mit dem Installationspaket aktualisiert.

## [2.6.2] – 2026-03-03

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Wurzelzähler-Unterstützung.
- 🧱 **Stabilität:** Veraltete oder doppelte Einstellungen werden zuverlässig bereinigt.
- ✨ **Verbesserung:** Web-Konfiguration Korrektur.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Symbole-Caching Korrektur.
- 📊 **Anzeige/Auswertung:** Diagramm-Stabilität.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Die Installation arbeitet zuverlässiger mit administrativen Rechten.

## [2.6.1] – 2026-03-02

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Klickbare Kacheln.
- ✨ **Verbesserung:** Mobilansicht Optimierung.
- ✨ **Verbesserung:** Diagramme passen sich nun dynamisch der Bildschirmgröße an (Responsive).
- 🔎 **Diagnose:** Trennung von "Echtzeit-Status" und "Prognose" in eigene Reiter für mehr Übersichtlichkeit.
- ✨ **Verbesserung:** Prognose-Korrektur.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Neue Zeitauswahl (6h, 12h, 24h, 48h) direkt über dem Diagramm.
- 📊 **Anzeige/Auswertung:** Detailwerte werden übersichtlich über dem Diagramm eingeblendet.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Korrektur beim Auslesen negativer Werte bei Wechselrichter-Phasen.
- 📊 **Anzeige/Auswertung:** Verbesserte Skalierung der Diagramm-Achsen für negative Werte (Einspeisung/Entladen).
- 🔄 **Migration/Kompatibilität:** Update-Benachrichtigung.

## [2.6.0] – 2026-03-02

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Das Preisdiagramm markiert Tageswechsel sowie Höchst- und Tiefstpreise zuverlässiger und passt sich unterschiedlichen Bildschirmgrößen an.
- ✨ **Verbesserung:** PV-Prognose, Preisangaben und historische Ansichten wurden übersichtlicher angeordnet.

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Die Webaktualisierung berücksichtigt bestehende Installationen und optionale Integrationen.

## [2.5.8] – 2026-03-01

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Notfall-Modus (Menu 99).
- 🔎 **Diagnose:** Erweiterter Status-Check.

## [2.5.7] – 2026-03-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Systemprotokoll-Viewer.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Preis-Graph.
- ✨ **Verbesserung:** Installationsassistent.

## [2.5.6] – 2026-03-01

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Tageswechsel-Logik.
- ✨ **Verbesserung:** Watchdog aktualisieren.

## [2.5.5] – 2026-02-28

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die Installation des optionalen Systemwächters wurde korrigiert; das Installationspaket erhielt den zugehörigen Stand.

## [2.5.4] – 2026-02-28

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Automatischer Neustart bei Abstürzen (Neustart always).
- ✨ **Verbesserung:** Gezielter Neustart.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Bereinigung von "toten" Anzeige-Sessions vor dem Start.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Erzwingen & Neustart.
- ✨ **Verbesserung:** Installation erzwingen (Re-Install), auch wenn die Version aktuell ist.
- ✨ **Verbesserung:** Installationsassistent.

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Migration zu Dienstverwaltung.

## [2.5.3] – 2026-02-27

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Diese Zwischenversion aktualisierte ausschließlich Versionsangabe und Installationspaket; gegenüber 2.5.2 kam keine weitere Produktfunktion hinzu.

## [2.5.2] – 2026-02-27

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Watchdog-Overhaul.
- ✨ **Verbesserung:** Täglicher Statusbericht.
- ✨ **Verbesserung:** Multi-IP Überwachung.
- ✨ **Verbesserung:** Router-IP Konfiguration.
- ✨ **Verbesserung:** Benutzer-Flexibilität.

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die zeitgesteuerte Ausführung wurde korrigiert.

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Telegram-Robustheit.

## [2.5.1] – 2026-02-27

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Mobilansicht Darkmode.

### 🛠️ Installation und Update

- 🛡️ **Sicherheit:** Die zeitgesteuerte Ausführung wurde abgesichert.

## [2.5.0] – 2026-02-27

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Neuer Einstellungen.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Redundanz-Bereinigung.
- ✨ **Verbesserung:** Bugfix Sommerzeit.

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Installationsassistent (Diagrammerstellung).
- ✨ **Verbesserung:** Konfiguration (Konfiguration).

## [2.4.2] – 2026-02-26

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Benutzerfreundlichkeit.
- 🔄 **Migration/Kompatibilität:** Ein Fehler wurde behoben, durch den der Installationsassistent nach einem Selbst-Update erneut nach dem Benutzernamen fragte, obwohl dieser bereits konfiguriert war.
- ✨ **Verbesserung:** Die Menü-Abfrage wurde personalisiert und zeigt nun den aktuellen Installationsbenutzer an (z. B. Auswahl (pi):).

## [2.4.1] – 2026-02-26

### 🔌 Wallbox Manager

- 📊 **Anzeige/Auswertung:** Die Weboberfläche zeigt die Kosten eines Wallbox-Ladevorgangs an.
- ✨ **Verbesserung:** Die Wallbox-Einstellungen sind über ein einheitliches Zahnradsymbol erreichbar.

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Desktop- und Mobilansicht erhielten besser lesbare historische Diagramme und Hinweisfelder.
- ✨ **Verbesserung:** Berührungsgesten öffnen Erläuterungen auf Mobilgeräten zuverlässig.
- ✨ **Verbesserung:** Die zuletzt gewählte Ladeleistung wird im Browser gespeichert und beim nächsten Besuch wiederhergestellt.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Einstellungen werden unabhängig von unbeabsichtigter Groß- und Kleinschreibung zuverlässig erkannt.
- ✨ **Verbesserung:** Neue Einstellungen können über den Konfigurationseditor ergänzt werden.

## [2.4.0] – 2026-02-25

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Echtzeit-Fortschrittsanzeige im Dialog-Fenster.
- 🐛 **Fehlerbehebung:** Regelmäßige Abfrage-Mechanismus verhindert Timeouts bei langsamen Verbindungen (Cloudflare-Korrektur).
- 🐛 **Fehlerbehebung:** Visuelles Feedback (Grüner Haken / Rotes Kreuz) bei Erfolg/Fehler.
- ✨ **Verbesserung:** Headless-Installationsassistent.

## [2.3.4] – 2026-02-25

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Rechteverwaltung und Einbindung der temporären Betriebsdaten wurden für bestehende Installationen nachgebessert.

## [2.3.3] – 2026-02-25

### 📦 Distribution und Kompatibilität

- 🔄 **Migration/Kompatibilität:** Diese Korrekturversion aktualisierte ausschließlich Versionsangabe und Installationspaket; der fachliche Produktstand von 2.3.2 blieb unverändert.

## [2.3.2] – 2026-02-25

### 🔌 Wallbox Manager

- ✨ **Verbesserung:** Szenarien für 7.2 kW, 11 kW und 22 kW Ladeleistung.
- 🔄 **Migration/Kompatibilität:** Auto-Aktualisierung.

### ☀️ PV, Prognose und Energiemanagement

- ✨ **Verbesserung:** Automatische Wiederherstellung.
- ✨ **Verbesserung:** Rechte-Management.
- ✨ **Verbesserung:** Sofortige Klarheit (Bedienung).
- ✨ **Verbesserung:** Kosten-Transparenz.
- ✨ **Verbesserung:** Echtzeit-Feedback.
- ✨ **Verbesserung:** Professionelle Optik.

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Komplettes Neugestaltung.
- ✨ **Verbesserung:** Echtzeitdaten Kacheln.
- 📊 **Anzeige/Auswertung:** Intelligente Strompreis-Anzeige.
- ✨ **Verbesserung:** Dynamischer Balken mit Farbcodierung (Grün/Gelb/Rot) je nach Preisniveau (Günstig/Teuer).
- ✨ **Verbesserung:** Trend-Indikatoren (Pfeile) zeigen steigende oder fallende Preise an.
- 📊 **Anzeige/Auswertung:** Anzeige der Tages-Minima und -Maxima.
- 📊 **Anzeige/Auswertung:** Multi-View Diagramm.
- ✨ **Verbesserung:** Smart regelmäßige Abfrage.

## [2.3.1] – 2026-02-25

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Die Berechtigungsverwaltung wurde von einer veralteten Kopie bereinigt; die Einrichtung des Systemwächters ist nun nachvollziehbar dokumentiert.

## [2.3.0] – 2026-02-24

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Echtzeitwerte und historische Verläufe wurden für Desktop und Mobilansicht umfassend neu gegliedert.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Ein optionaler Wächter überwacht Netzwerkverbindung und laufende Kernprozesse und berücksichtigt beim Neustart eine geordnete Abschaltzeit.
- 🔎 **Diagnose:** Statusmeldungen können zusätzlich über Telegram zugestellt werden.

## [2.2.2] – 2026-02-20

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Numerische Eingaben mit deutschem Dezimalkomma werden robuster verarbeitet; die freie Wahl des Installationskontos wurde korrigiert.

## [2.2.1] – 2026-02-20

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Das Installationskonto lässt sich über einen eigenen Menüpunkt ändern; Einstellungen und Berechtigungen werden gemeinsam angepasst.

## [2.2.0] – 2026-02-19

### 🖥️ Weboberfläche

- ✨ **Verbesserung:** Eine neue Mobilansicht macht Echtzeitwerte und zentrale Bedienfunktionen auf kleinen Bildschirmen zugänglich.

### 🛠️ Installation und Update

- 🧱 **Stabilität:** Protokollierung und wiederkehrende Installationsaufgaben wurden robuster in den Installationsassistenten eingebunden.

## [2.1.0] – 2026-02-16

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Bei der Erstinstallation kann das lokale Installationskonto frei gewählt werden; Sicherung, Rückfall und Rechteverwaltung berücksichtigen diese Auswahl.

## [2.0.0] – 2026-02-13

### 🖥️ Weboberfläche

- 📊 **Anzeige/Auswertung:** Die Diagrammansicht erhielt mehrere Messwertachsen und markiert den Beginn von Wallbox-Ladevorgängen auf der Strompreiskurve.
- 🧱 **Stabilität:** Verständliche Hinweise unterstützen bei fehlenden Berechtigungen; eine Aktualisierung der Seite erzeugt das Diagramm nicht unnötig oft neu.

## [1.1.1] – 2026-02-12

### 🛠️ Installation und Update

- 🔄 **Migration/Kompatibilität:** Konfigurationsassistent, Installation, Rückfall, Deinstallation und Systemeinrichtung wurden auf den gemeinsamen MQTT-fähigen Installationsstand gebracht.

## [1.1.0] – 2026-02-11

### 🔗 Schnittstellen und Smart Home

- ✨ **Verbesserung:** Die lokale MQTT-Anbindung an openWB wurde ergänzt. Erreichbarkeit, Anmeldung und Empfang des gewählten Datenkanals lassen sich nachvollziehbar prüfen.

## [1.0.3] – 2026-02-11

### 🛠️ Installation und Update

- 🐛 **Fehlerbehebung:** Aktualisierungen starten die Anwendung wieder korrekt, stellen benötigte Berechtigungen her und übernehmen die neue Versionsangabe, ohne das Installationsverzeichnis zu verlieren.

## [1.0.2] – 2026-02-11

### 📦 Distribution und Kompatibilität

- 🐛 **Fehlerbehebung:** Ein doppelt bereitgestelltes Installationsarchiv wurde entfernt; die Einstiegshinweise wurden entsprechend bereinigt.

## [1.0.1] – 2026-02-11

### 📦 Distribution und Kompatibilität

- ✨ **Verbesserung:** Ein Installationspaket für die Diagrammerstellung wurde in die Distribution aufgenommen.

## [1.0.0] – 2026-02-11

### 🛠️ Installation und Update

- ✨ **Verbesserung:** Der Installationsassistent erhielt eine automatische Aktualisierungsprüfung und die Anbindung an veröffentlichte GitHub-Versionen.
- 🔄 **Migration/Kompatibilität:** Installation, Aktualisierung und Versionierung wurden erstmals gemeinsam beschrieben.
