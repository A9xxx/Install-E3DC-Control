# Pre-Dump und Hard-Pre-Dump

Pre-Dump ist ein vorbereitender Speicherpfad für sehr starke PV-Tage. Er schafft
vor der erwarteten PV-Spitze Platz im Hausspeicher, damit später weniger Energie
an Einspeise- oder Wechselrichtergrenzen abgeregelt wird.

Pre-Dump ist kein normales Nachtziel und kein Ersatz für den Morgenpuffer:

- `storage_predump_min_soc`: tiefste erlaubte Reserve für aktives
  Vorab-Entladen.
- `storage_morning_soc`: gewünschte Startreserve am Kurvenbeginn.
- `target_soc`: Tagesziel zum Freilauf, meist nahe 95 Prozent.

## Grundregel

Pre-Dump darf nur im Vorab-Fenster vor dem Kurvenstart aktiv entladen. Sobald
die Ladekurve gestartet ist, darf Pre-Dump keine zusätzliche Entladung mehr
auslösen. Ab dann führt nur noch die Ladekurve den Speicher: sie hält
Speicherplatz, begrenzt Ladung oder erlaubt Abregelschutz, startet aber keinen
nachträglichen Pre-Dump.

Erreicht der Speicher das Pre-Dump-Minimum, wird nicht die normale
Batterieentladung für das Haus gesperrt. Die Untergrenze beendet nur die aktive
Vorab-Entladung beziehungsweise stoppt freigegebene Pre-Dump-Verbraucher wie
eine Wallbox.

## Planung

Der Storage Simulator schreibt für den aktiven Plan explizit:

- geplante Pre-Dump-Energie in Wh,
- Start- und Endzeitfenster,
- Pre-Dump-Ziel-SoC,
- Begründung, warum Pre-Dump geplant oder nicht geplant ist.

Diese Werte werden in der Ladekurven-Anzeige als `Pre-Dump x kWh` und als Grund
sichtbar gemacht.

Seit 5.2.2a trennt die Anzeige drei Werte bewusst:

- `Adaptiver Headroom`: freier Speicherplatz und Reserve im späteren
  Abregel-Druckfenster. Das beschreibt die Kurven- und Diagnosebandbreite.
- `Headroom-Reserve max`: der größte Headroom, den die Kurve aus Rohdruck,
  historischen PV-Spitzen, Kaltwind-/Temperaturfaktor und Live-Cloud-Edge
  plausibel vorhalten kann. Das ist kein offener Entladeauftrag.
- `Pre-Dump-Bedarf`: die tatsächlich geplante Vorab-Entladeenergie bis zum
  Kurvenstart. Nur dieser Wert darf einen aktiven Pre-Dump auslösen.

Ein Header wie `6,4 kWh frei / Reserve max 10,7 kWh` bedeutet daher nicht, dass
noch 4,3 kWh fehlen. Er bedeutet: aktuell sind 6,4 kWh Speicherplatz frei, und
die Tageslogik kann im Extremfall bis 10,7 kWh Headroom als Reserveband
berücksichtigen. Ob dafür aktiv vorab entladen wird, steht ausschließlich beim
separaten `Pre-Dump-Bedarf`.

Die Planung darf zukünftige Pre-Dump- oder Kurvenstartanker nicht einfach auf
den aktuellen SoC hochziehen. Nur bereits gestartete, aktive Tagesanker dürfen
an den aktuellen Verlauf geglättet werden. Dadurch bleibt ein geplanter
Pre-Dump-Startwert auch dann erhalten, wenn der Speicher am Vorabend noch höher
steht.

## Grundberechnung

Normaler Pre-Dump entlädt nicht den gesamten prognostizierten PV-Überschuss.
Zuerst wird geprüft, ob der Speicher im späteren Druckfenster ohnehin noch
genug sicheren Platz bis zum Tagesziel hat.

```text
Rohdruck = Summe(max(0, PV - sichere Verbraucher - Einspeiselimit))
Sicherer Headroom = (Ziel-SoC - erwarteter SoC beim ersten Druckfenster) * Speichergröße
Pre-Dump-Bedarf = max(0, Rohdruck - sicherer Headroom)
Pre-Dump-Ziel = Pre-Dump-Bedarf + kleiner Regelpuffer, begrenzt durch Pre-Dump-Minimum
```

Der Rohdruck ist also nur die potenziell gefährdete Energie. Er wird erst dann
zum aktiven Dump-Bedarf, wenn er nicht mehr in den sicheren Speicherplatz bis
zum Ziel-SoC passt. Als sichere Verbraucher zählen Hauslast und nur solche
WP-/Wallbox-/Lastpfade, die die Prognose bewusst als verlässlich behandeln darf.

Grenzbeispiel mit genug Headroom:

- Speichergröße: 35 kWh
- Ziel-SoC: 95 %
- erwarteter SoC beim ersten Druckfenster: 80 %
- sicherer Headroom: 15 % von 35 kWh = 5,25 kWh
- Rohdruck: 3,0 kWh

```text
Pre-Dump-Bedarf = max(0, 3,0 kWh - 5,25 kWh) = 0,0 kWh
```

Obwohl PV zeitweise über Hausverbrauch plus Einspeiselimit liegt, ist kein
aktiver Pre-Dump nötig. Der Speicher kann den Druck aufnehmen, ohne das Tagesziel
zu überschreiten.

Grenzbeispiel mit echtem Pre-Dump-Bedarf:

- Speichergröße: 35 kWh
- Ziel-SoC: 95 %
- erwarteter SoC beim ersten Druckfenster: 92 %
- sicherer Headroom: 3 % von 35 kWh = 1,05 kWh
- Rohdruck: 6,0 kWh

```text
Pre-Dump-Bedarf = max(0, 6,0 kWh - 1,05 kWh) = 4,95 kWh
```

Hier reicht der vorhandene Headroom nicht. Pre-Dump schafft nur den fehlenden
Restplatz plus einen kleinen Regelpuffer, nicht pauschal den gesamten Rohdruck.

## Headroom ist nicht automatisch Pre-Dump

Headroom ist zuerst ein Speicherplatz- und Kurvenband. Die Regelung versucht ihn
in dieser Reihenfolge zu halten:

1. Ladekurve und Oberkante so setzen, dass der Speicher nicht unnötig früh voll
   wird.
2. Ladegrenzen im E3DC-AUTO-Betrieb reduzieren, wenn der Speicher oberhalb des
   Zielkorridors liegt.
3. Nur vor Kurvenstart und nur bei echtem `Pre-Dump-Bedarf` aktiv entladen.
4. Nach Kurvenstart optional kurze Headroom-DISCH-Impulse nutzen, wenn der neue
   Headroom-Schalter das erlaubt.

Die Begriffe `Bedarf`, `fehlt` und `nötig` sollen deshalb nur noch für echte
Regelaufträge verwendet werden. Eine maximale Headroom-Reserve ist dagegen ein
Diagnosewert für den ungünstigsten plausiblen Tagesverlauf, nicht automatisch
Energie, die morgens heraus muss.

## Punktlandung statt Pulver verschießen

Der Manager entlädt nicht am Anfang des erlaubten Fensters sofort mit maximaler
Leistung. Er berechnet eine Trajektorie zum Kurvenstart:

```text
Restenergie bis Ziel / Restzeit bis Kurvenstart = nötige mittlere Entladung
```

Daraus entsteht ein gerundetes Verbraucher- oder Entladebudget. Wenn ein
Verbraucher erst später sinnvoll gestartet werden kann, wird der Start so weit
wie möglich nach hinten geschoben, solange das Ziel rechnerisch noch erreichbar
bleibt.

## Verbraucherbudget

Freigegebene Verbraucher wie Wallbox, Wärmepumpe oder Heizstab können
Pre-Dump-Energie aufnehmen. Das Budget beschreibt dabei nicht "Leistung der
Wärmepumpe", sondern die gewünschte Entnahme aus dem Speicher. Wenn die Sonne
den Verbraucher bereits deckt, kann das reale Speicher-Entladen deutlich
kleiner sein als das angebotene Budget.

Aktuell gilt:

- Wallbox: Mindestleistung, Phasen und Fahrzeugfähigkeit werden beachtet. Ein
  BEV mit hoher Mindestleistung wird eher kurz und passend gestartet als über
  Stunden am unteren Rand getaktet.
- Wärmepumpe: Der Energy Manager meldet die echte elektrische Leistung in die
  Ramdisk. Der Storage Manager übernimmt diese Leistung als `WP_Power` und
  zieht sie aus dem reinen Hausverbrauch heraus, damit sie im Pre-Dump nicht
  doppelt gezählt wird.
- Heizstab/Shelly: wird nur genutzt, wenn er ausdrücklich als Pre-Dump-Senke
  freigegeben ist.

Eine prognostizierte Wärmepumpe wird nicht als garantierter Verbraucher
angenommen. Gerade im Sommer läuft häufig nur Warmwasser, und der tatsächliche
Bedarf kann wegfallen.

## Abregelschutz: Netz-Dump ist integriert

Beim normalen Abregelschutz-Pre-Dump ist aktives Entladen ins Netz ein
integrierter Schutzpfad. Lokale Verbraucher haben Vorrang, aber wenn der
berechnete Restbedarf sonst nicht erreichbar ist, darf der Speicher als
Fallback ins Netz entladen. Das ist keine Komfortentscheidung, sondern Teil des
Abregelschutzes.

Der Netz-Dump ist begrenzt durch:

- verbleibende Zeit bis Kurvenstart,
- verbleibende Pre-Dump-Energie,
- maximale Entladeleistung,
- konfiguriertes Einspeiselimit und Abregelpuffer,
- live erkennbare Einspeisung und Batterieentladung.

Wenn das Netz bereits nahe an der Einspeisegrenze ist, darf Pre-Dump nicht
blind zusätzliche Leistung ins Netz drücken. Dann bleibt nur lokaler Verbrauch
oder ein späterer, kleinerer Fallback.

## Komfort-/Fixziel-Pre-Dump

Hard-Pre-Dump ist der optionale Komfort- beziehungsweise Fixziel-Modus für
Nutzer, die den Speicher bewusst bis zu einem festen Wert entladen wollen. Das
ist strenger als der normale dynamische Pre-Dump und dient nicht automatisch dem
Abregelschutz.

- Normaler Pre-Dump berechnet den nötigen Platz aus Prognose, Speicherstand,
  PV-Spitze, sicherem Headroom und Verbraucherannahme. Netz-Dump ist dort als
  Abregelschutz-Fallback integriert.
- Komfort-/Fixziel-Pre-Dump setzt ein festes Ziel und lässt die gewünschten
  Senken bewusst wählen: Wärmepumpe, Wallbox, Heizstab und optional Netz.
- Netz-Fallback im Komfortpfad ist eine Betreiberentscheidung. Ist er nicht
  erlaubt, wartet der Komfort-Dump auf freigegebene Verbraucher oder normalen
  Hausverbrauch und entlädt nicht aktiv ins Netz.

Typische Konstellationen:

- Warmwasser morgens aus Akkuenergie: Im Sommer soll die Wärmepumpe vor dem
  PV-Tag einmal bewusst laufen, damit morgens warmes Wasser bereitsteht. Dafür
  wird Wärmepumpe als Senke aktiviert und ein realistischer Ziel-SoC gesetzt,
  zum Beispiel nicht bis zum Pre-Dump-Minimum, sondern nur so weit, wie die
  Warmwasserladung voraussichtlich braucht.
- Auto morgens aus dem Akku nachladen: Wenn tagsüber keine Lademöglichkeit
  besteht oder das Auto vor der PV-Spitze wegfährt, kann die Wallbox als
  Komfort-Senke freigegeben werden. Das ist dann kein Abregelschutz, sondern
  eine bewusste Verschiebung von Akkuenergie ins Fahrzeug.
- Heizstab oder andere lokale Last nutzen: Wenn ein Betreiber elektrische Wärme
  bewusst bevorzugt, kann der Heizstab/Shelly als Senke dienen. Sinnvoll ist das
  nur mit einem Ziel-SoC, der die Nacht- und Notreserve respektiert.
- Netz als bewusster letzter Ausweg: Wer auch ohne lokale Last aktiv entladen
  will, kann im Komfortpfad Netz-Fallback erlauben. Das kann wirtschaftlich
  schlechter sein und sollte deshalb nicht automatisch aus einer Komfortabsicht
  folgen.

Komfort-/Fixziel-Pre-Dump ist für klare Betreiberwünsche gedacht, nicht als
Standardautomatik. Ein realistischer Ziel-SoC ist wichtiger als ein möglichst
tiefer Zielwert.

## Abregelschutz nach Pre-Dump

Nach Kurvenstart übernimmt der Abregelschutz, wenn tatsächlich Einspeise- oder
Wechselrichterdruck entsteht. Er ist ein harter Schutzpfad, aber nicht klebend:

- Die wirksame Grenze ist die konservativere aus konfiguriertem Einspeiselimit
  und live gemeldetem Derating-Limit.
- DC-Leistung oberhalb der Wechselrichterleistung zählt zum physikalischen
  Abregeldruck, wenn sie ohne Speicher nicht nutzbar wäre.
- Ohne echten Druck fällt die Regelung zur ruhigen Kurvenführung zurück.

Bei großen Verbrauchern wie einem 11-kW-BEV wird die reale Wallboxleistung
einbezogen. Wenn das BEV die AC-Leistung bereits aufnimmt, muss der Speicher
nicht zusätzlich dieselbe Leistung laden. Nur echte Exportgefahr,
Wechselrichterdruck oder Ladekurvenbedarf rechtfertigen eine Speicheranforderung.

## Aktive Headroom-Entladung nach Kurvenstart

Headroom-Entladung ist nicht Pre-Dump. Sie gehört zur laufenden
Kurven-/Abregelreserve und kann nach Kurvenstart kurze `DISCH`-Impulse setzen,
wenn alle Schutzbedingungen erfüllt sind:

- PV läuft bereits und es gibt Exportraum am Netzpunkt.
- Der SoC liegt oberhalb der aktuellen Kurven-Unterkante.
- Headroom-Reserve ist aktiv, aber echter Abregeldruck blockiert den Impuls
  nicht.
- Keine Wallbox- oder Kurvenladung hat Vorrang.
- Tageslimit und Mindestpause lassen einen weiteren Impuls zu.

Die sichtbaren Schalter und Grenzen sind:

- `storage_headroom_discharge_enable`: erlaubt oder verbietet aktive
  Headroom-DISCH-Impulse.
- `storage_headroom_discharge_daily_limit_pct`: Tageslimit in Prozent der
  Speicherkapazität. `10` bedeutet maximal 10 Prozent Batterieenergie pro Tag
  nur für Headroom-DISCH.
- `storage_headroom_discharge_cooldown_min`: Mindestpause nach einem aktiven
  Headroom-Impuls.

Wenn `storage_headroom_discharge_enable = 0` ist, darf Headroom weiter über
Ladegrenzen, Kurvenunterkante und Oberkante wirken. Es wird dann aber kein
aktiver Headroom-DISCH-Befehl mehr gesendet. Auf älteren 5.2.2-Systemen war
Pre-Dump aus noch nicht gleichbedeutend mit Headroom-Entladung aus; seit 5.2.2a
ist dieser Pfad deshalb separat konfigurierbar.

## Diagnose

Wenn ein Pre-Dump-Tag auffällig ist, sind diese Felder wichtig:

- `storage_plan.json`: `predump_dump_wh`, `predump_start_ts`,
  `predump_end_ts`, `predump_reason`
- `target_curve_meta`: `adaptive_headroom_available_wh`,
  `adaptive_headroom_required_wh`, `adaptive_headroom_buffer_wh`,
  `headroom_reserve_pressure_wh`, `headroom_reserve_source`,
  `curtailment_pressure_wh`, `curtailment_unavoidable_wh`
- Ladekurven-Modal: `frei / Reserve max`, `Pre-Dump x kWh` und Begründung
- Storage-Entscheidungen/R5: Regelbesitzer, Kurvenabstand, EMS-Limits,
  Verbraucherbudget, Netz-Dump-Fallback, `parallel_headroom_discharge`
- Wallbox-R5: echte Wallboxleistung, Budget, Stop-Grund
- Energy-R5: Wärmepumpenleistung und Mindesthaltezeit
- Config: `storage_headroom_discharge_enable`,
  `storage_headroom_discharge_daily_limit_pct`,
  `storage_headroom_discharge_cooldown_min`

Eine plausible Kurve endet morgens nicht am Pre-Dump-Minimum, sondern am
Kurvenstart/Morgenpuffer. Das Pre-Dump-Minimum ist nur die tiefste erlaubte
Reserve für aktive Vorab-Entladung.
