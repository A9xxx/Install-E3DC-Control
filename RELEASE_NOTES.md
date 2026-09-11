# E3DC-Control v5.4.6a

Dieses Korrekturupdate behebt den Docker-Startabbruch nach 5.4.6 und verbessert die Übernahme älterer Compose-Installationen.

## Docker-Korrekturen

- Das interne Laufzeitmodul ist nach dem Wechsel auf das unprivilegierte Konto wieder erreichbar. Das Image legt sein Verzeichnis mit den erforderlichen Suchrechten an und prüft den tatsächlichen Privilegienwechsel bereits beim Bau. Die Rechteprüfungen bleiben erhalten.
- Der Host-Updater erkennt zusätzlich die bekannte alte Named-Volume-Vorlage mit festem `latest`: sowohl die ursprünglichen Daten-/Logvolumes als auch die vollständig um drei private Standardvolumes ergänzte Form. Gültige direkt eingetragene Web-Port- und Bindadressen bleiben erhalten. Projekt- und Volumezuordnung werden weiterhin geprüft.
- Scheitert nach einem Update auch der Rückstart des vorherigen Images oder dessen Prüfung, stoppt der Host-Updater den eindeutig zugeordneten Rückfallcontainer. Ein unbestätigter Stillstand wird ausdrücklich gemeldet.

## Updatehinweise

**Zuerst den verwendeten Host-Helfer `Installer/docker_compose_update.py` aktualisieren.** Ein neues Containerimage ersetzt diese Datei auf dem Docker-Host nicht. Danach das Update aus dem bestehenden Compose-Verzeichnis mit dem aktuellen Helfer ausführen. Ein fester Image-Pin muss bewusst auf `v5.4.6a` geändert werden; `latest` folgt dem korrigierten Stable-Image.

Benötigte Volumes bei gestopptem Container mit erhaltenen numerischen Eigentümern und Dateirechten auf dem Host sichern. Updates und Container-Neuerstellungen bei beendeter Fahrzeugladung und ohne laufenden Phasenwechsel durchführen. Keine zusätzliche Compose-Option `user:` setzen. Private Wallbox-Steuerzustände überleben einen Neustart desselben Containers, aber keine Neuerstellung.

Meldet der Helfer einen unsicheren Compose-Dateimodus wie `0777`, die Rechte der einzelnen Compose-Datei auf dem Host korrigieren. Die Rechteprüfung wird nicht umgangen; eine Rechtekorrektur allein ersetzt keine benötigte Volume-Migration.

Rückfälle auf ältere Root-Images benötigen weiterhin den aktuellen Host-Updater. Aus Bridge zuerst dieselbe aktuelle Runtime-Version im Hostprofil wiederherstellen und deren gesunden Start prüfen. Einzelheiten zu Update, Dateirechten, Sicherung und Rückfall stehen in der [Docker-Dokumentation](doc/Docker_Dokumentation.md).
