# RAM-Disk: Betriebs- und Schutzvertrag

E3DC-Control hält häufig wechselnde Live-Daten unter
`/var/www/html/ramdisk`. Dieser Pfad muss exakt ein `tmpfs` sein. Ein bloß
vorhandenes Verzeichnis auf dem Root-Dateisystem ist kein zulässiger
Ersatzbetrieb, weil dadurch hochfrequente Schreibzugriffe auf SD-Karte oder SSD
fallen würden.

## Bare-Metal mit systemd

Installation und Update legen für die feste Positivliste der
E3DC-Produktdienste ein verwaltetes Drop-in an:

```text
/etc/systemd/system/<dienst>.service.d/20-e3dc-ramdisk-tmpfs.conf
```

Das Drop-in enthält drei voneinander unabhängige Schutzebenen:

1. `RequiresMountsFor=/var/www/html/ramdisk` bindet den Dienst an den
   systemd-Mountvertrag.
2. `After=var-www-html-ramdisk.mount` ordnet den Start hinter dem Mount ein.
3. Ein direktes `ExecStartPre=/usr/bin/findmnt ...` akzeptiert nur einen
   Mountpunkt mit dem Dateisystemtyp `tmpfs`.

Ein Root-Fallback wie `TARGET="/"`, ein Bind-Mount oder ein anderes
Dateisystem am richtigen Pfad wird abgewiesen. Nach drei Fehlstarts innerhalb
von fünf Minuten begrenzt systemd weitere automatische Startversuche. Nach der
Reparatur muss der fehlgeschlagene Dienst deshalb gegebenenfalls mit
`systemctl reset-failed` zurückgesetzt werden.

Watchdog, alter Grabber, Apache und die Mount-/Reparaturpfade selbst gehören
bewusst nicht zur Positivliste. Sie müssen einen fehlenden Mount diagnostizieren
und reparieren können, ohne von ihm abhängig zu sein.

## Prüfung und Reparatur

Der aktuelle Zustand lässt sich ohne Schreibzugriff prüfen:

```bash
/usr/bin/findmnt --kernel --first-only \
  --target /var/www/html/ramdisk \
  --noheadings --pairs --output TARGET,FSTYPE

systemctl status var-www-html-ramdisk.mount
systemctl status e3dc-live.service
```

Zulässig ist ausschließlich:

```text
TARGET="/var/www/html/ramdisk" FSTYPE="tmpfs"
```

Ist der Mount ausgefallen, zuerst die Ursache in `/etc/fstab` und im
Mount-Status beheben. Danach:

```bash
sudo mount /var/www/html/ramdisk
/usr/bin/findmnt --kernel --first-only \
  --mountpoint /var/www/html/ramdisk \
  --types tmpfs --noheadings --output TARGET
sudo systemctl reset-failed e3dc-live.service
sudo systemctl restart e3dc-live.service
```

Weitere Produktdienste erst starten, wenn die zweite `findmnt`-Prüfung exakt
`/var/www/html/ramdisk` ausgibt. Der reguläre Installer beziehungsweise das
Update prüft und aktualisiert alle verwalteten Drop-ins gemeinsam.

## Docker

Docker verwendet keine systemd-Drop-ins im Container. Die mitgelieferte
Compose-Datei mountet deshalb direkt:

```yaml
tmpfs:
  - /var/www/html/ramdisk:size=32M,uid=33,gid=33,mode=2775
```

Der Container-Entrypoint prüft diesen Mount vor Apache und vor jedem
Python-/Node-Dienst mit `/usr/bin/findmnt`. Fehlt das exakte `tmpfs`, beendet er
den Start mit Fehler. Ein manuell gestarteter Container ohne den
Compose-`tmpfs`-Vertrag fällt somit nicht auf sein beschreibbares
Root-Dateisystem zurück.

Der konfigurierte Docker-Vertrag kann auf dem Host geprüft werden:

```bash
docker inspect --format '{{json .HostConfig.Tmpfs}}' e3dc-control
```

## Datenverhalten

Inhalte der RAM-Disk gehen bei Neustart oder Remount bewusst verloren.
Live-Dienste erzeugen sie aus frischen Messungen neu. Fachlich notwendige,
begrenzte Wiederanlaufstände liegen getrennt im persistenten Datenbereich.
Sicherheitsereignisse und Restart-Checkpoints dürfen daher nicht pauschal in
die RAM-Disk verschoben oder abgeschaltet werden.
