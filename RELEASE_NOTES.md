# v5.4.0a

E3DC-Control v5.4.0a ist ein enger Kompatibilitäts-Hotfix auf Basis von
v5.4.0. Die Regelungsarchitektur und ihre Schutzverträge bleiben unverändert.

## Korrekturen in v5.4.0a

- Das normale Bare-Metal-Update installiert keine optionalen Matter-Pakete
  mehr. Node.js, npm, Avahi und D-Bus werden ausschließlich bei einer
  ausdrücklich gestarteten Matter-Installation gemeinsam geprüft und
  installiert. Ein Apt-Konflikt dieser optionalen Pakete blockiert damit kein
  E3DC-Control-Update mehr.
- Der Web-Updater prüft seinen privilegierten Installer-Wrapper jetzt vor der
  sudo-Freigabe gegen den veröffentlichten Git-Stand. Eine reine
  CRLF-Beschädigung kann kontrolliert auf die exakten Release-Bytes repariert
  werden; andere Abweichungen brechen sicher ab und werden eindeutig erklärt.
- Alte Shelly-EM-Zähler der ersten Generation können über ihre lokale
  read-only-Status-API eingebunden werden. Die automatische Erkennung fällt
  nach einer nicht unterstützten RPC-Antwort auf Gen1 zurück; Kanal und Summe
  werden explizit ausgewertet, fehlende oder ungültige Messwerte bleiben
  unbekannt.

## Update und Docker

Bare-Metal-Nutzer können v5.4.0a über den Web- oder Konsolen-Updater
installieren. Das veröffentlichte Container-Image trägt den Tag
`v5.4.0a`; `latest` wird erst nach bestandener Kandidaten- und
Attestierungsprüfung auf denselben Digest gesetzt. Der vorgesehene öffentliche
Rückfallstand bleibt `v5.3.2b`.

## Funktionsumfang der Basis v5.4.0

E3DC-Control v5.4.0 bündelt die neue Energie-Arbitration für Speicher,
Direktvermarktung, Wallbox und Wärmeverbraucher mit einem transaktionalen
Update-, Backup- und Wiederherstellungsvertrag.

## Wichtigste Änderungen

- Ein eindeutiger Regel-Owner und ein unmittelbar vor jedem Hardwareausgang
  geprüfter Anlagenkontext verhindern konkurrierende Aktorzugriffe.
- Ungültige Markt-, Wallbox- oder Anlagendaten werden als inaktiv oder
  unbekannt behandelt und nicht als alte Freigabe beziehungsweise gültige
  `0 W` fortgeschrieben.
- Plan, Slot, Marktfenster, Freigabe, Geräteanforderung und Rücklesung bleiben
  über dieselbe Identität gebunden. Interne DC-PV und zusätzliche AC-Erzeuger
  werden typisiert bilanziert; DC- und Netzpunktdruck werden nicht addiert.
- Wallboxaktionen oder der Verlust eines Wallboxkontexts stoppen keine bereits
  laufende Wärmepumpe eigenständig. Hardwarebefehle bleiben an frische,
  treiberspezifische Rückmeldungen gebunden.
- Der Watchdog führt nur noch ein einmaliges, geordnetes Quiesce aus. Er sendet
  keine eigene RSCP-, Wallbox-, Wärmepumpen-, Phasen- oder CP-Sequenz.
- Update, Rollback und Web-Planung brechen bei unvollständigem Backup, Timeout,
  Signal oder Teilfehler ab und stellen den letzten gültigen Konfigurations-
  und Dienstzustand wieder her.
- Legacy-ML-Pickles werden nicht geladen. Neue Modelle liegen privat,
  manifest- und hashgebunden in einem separaten persistenten Store.

## E3/DC-Wallbox: bestätigungsgebundene Community-Kompatibilität

- Für efy, Easy Connect und bestehende E3/DC-Wallboxen bleibt der sechs Byte
  lange `WB_REQ_SET_EXTERN`-/WBchar6-Laufzeitpfad für Modus, Strom und den
  episodischen Start/Stop erhalten. Ein Startimpuls ist höchstens einmal je
  frisch bestätigter physischer Stop-Episode zulässig.
- `Nur Status` ist eine bewusste Backendwahl und keine generelle Aussage, dass
  die Wallbox nicht unterstützt wird. Neue E3/DC-Konfigurationen bieten die
  WBchar6-Kompatibilitätsregelung sichtbar als empfohlenen Community-Pfad an;
  eine ausdrücklich gespeicherte Deaktivierung bleibt erhalten.
- Direkte Sun-/Auto-/Abort-, Maximalstrom- und native Phasenbefehle sind kein
  Bestandteil dieses Stable-Releases. Sie bleiben unabhängig vom beobachteten
  Readback gesperrt; der bestätigungsgebundene WBchar6-Pfad ist davon getrennt.
- Netzstrom-Arbitrage bleibt in 5.4.0 wirkungslos. Vorhandene Altwerte werden
  kompatibel erhalten, können aber keinen ausführbaren Speicher-Owner erzeugen.

## openWB Pro: geschützter Phasenwechsel

- Ein Phasenwechsel läuft über getrennte Managerzyklen: zuerst 0 A und danach
  bei frischem Nullleistungs-Readback das Phasenziel. Die openWB besitzt mit
  `phasetarget` die CP-Signalisierung; E3DC-Control sendet dafür keinen zweiten
  CP-Wire-Befehl.
- Der Wiederanlauf bleibt gesperrt, solange CP noch aktiv, der Status stale
  oder unbekannt, die Zielphase nicht frisch bestätigt oder die
  phasenwechselbezogene Schutzzeit von mindestens 480 Sekunden noch nicht
  abgelaufen ist. Ein crashfester Intent-/ACK-Zustand verhindert das blinde
  Wiederholen eines unbestätigten Phasenausgangs.

## Wallbox-Start, Balancing und ruhige PV-Kurve

- Eine angesteckte und freigegebene openWB Pro verwirft abgelaufene
  Phasenreservierungen und veraltete Nullanker. Nach bestätigter Bereitschaft
  wird die positive Startfreigabe erneut projiziert, ohne Umstecken oder
  wiederholte CP-Schaltungen.
- Das Mehr-Wallbox-Balancing rechnet mit L1/L2/L3-Stromvektoren, realer
  Phasenzahl und Netzpunktreserve. Einphasige und dreiphasige Stromwerte werden
  weder pauschal summiert noch als gleiche Ladeleistung behandelt.
- `PV-Kurve ruhig` folgt dem nachhaltigen PV-/Ladekurvenbudget. Eine bereits
  laufende Ladung darf kurze Einbrüche mit höchstens 75 Wh Batteriestützung
  überbrücken; ein Kaltstart oder Phasenwechsel wird nicht aus dem Speicher
  finanziert. `PV + Akku` bleibt ein eigener Modus.

## iDM-Diagnose und mobiles Energiefluss-Layout

- Der manuelle iDM-Scanner liest Register 1006 genau einmal als dokumentiertes
  Input-Register. Eine semantische Zuordnung erfolgt nur bei passend gebundenem
  Modell, Protokoll, Firmware und Unit-ID; fehlende Angaben bleiben unbekannt.
  Für Register 1006 existiert kein Schreibpfad.
- Energiefluss-Badges speichern Desktop- und Mobile-Positionen feldgenau mit
  getrennten Revisionen. Tablet- und Querformatansichten trennen Quellen und
  Verwendung und melden erfolgreiche oder kollidierende Speicherungen sichtbar.

## Erhaltene Produktfunktionen

- Matter bleibt mit Weboberfläche und drei read-only Statusschaltern erhalten.
  Neue Commissioning-Daten werden installationsindividuell und privat
  gespeichert; bestehende Fabrics werden nicht gelöscht.
- Shadow bleibt als read-only Vergleichs-/Testinstanz ohne Hardwarebefehle und
  ohne automatischen Failover-Writer erhalten.
- V2H-/V2G-Telemetrie bleibt sichtbar; aktive bidirektionale Steuerung ist
  weiterhin nicht freigegeben.
- Klassisches und modernes Frontend, Direktvermarktung, Wallbox-, Wärme- und
  Speicherfunktionen bleiben Bestandteil des Releases.

## Update und Rückfall

Der Wechsel aus einer älteren, nicht verwandten Historie erfolgt über den
geprüften Installer-/Bootstrapweg, nicht über `git pull`. Vor dem Umschreiben
ist ein externes, manifestiertes und prüfsummengesichertes Backup Pflicht.

Einziger vorgesehener öffentlicher Rückfallstand ist der sanitierte Root
`v5.3.2b`. Ein Rückfall wird nur angeboten, wenn Tag, Commit-SHA und Artefakt
in der veröffentlichten Update-Policy exakt übereinstimmen.

## Docker

Die Images werden aus dem veröffentlichten Git-Stand über GitHub Actions
gebaut. `latest` ist ausschließlich für v5.4.0a vorgesehen; der Rollback-Tag
bleibt `v5.3.2b`. Matter-Abhängigkeiten stammen aus der Lockdatei, und das
anlagenspezifische ML-Modell liegt in einem separaten persistenten Volume.

Matter ist weiterhin ein nicht zertifizierter lokaler Integrationspfad.
V2H/V2G ist read-only, und Shadow besitzt keine Aktorfreigabe.
