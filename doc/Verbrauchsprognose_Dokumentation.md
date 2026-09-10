# Lokale Verbrauchsprognose

E3DC-Control kann aus lokalen Betriebs- und Statistikdaten eine Prognose für
Haus- und Wärmepumpenverbrauch bilden. Die Prognose unterstützt Ladekurve und
Pre-Dump, besitzt aber keinen eigenen Hardwareausgang: Speicher-, Wallbox- und
Wärmebefehle bleiben bei ihren jeweiligen Managern und Schutzgrenzen.

Die lokale Prognose liefert ausschließlich Eingabedaten für Pre-Dump und die
zentrale, ownergebundene Energieentscheidung. Sie setzt weder Sollwerte noch
Hardwarebefehle und ändert keine Reserve- oder Gerätegrenze.

## Betrieb und Datenschutz

- Trainings- und Prognosedaten bleiben auf der Installation.
- Das private Modellverzeichnis liegt außerhalb des Webroots unter
  `/var/lib/e3dc-control/ml`. Auf Bare Metal gehört es dem bestätigten
  Installationskonto; im Docker-Container verwendet es ausschließlich das
  feste Laufzeitkonto `e3dc-runtime`. Der Webbenutzer erhält keinen Zugriff.
- Modell, Manifest und Prüfsumme werden atomar geschrieben und beim Laden
  gemeinsam geprüft.
- Alte ungebundene Modelldateien aus dem Web-Datenverzeichnis werden nicht
  geladen oder übernommen.
- Modell- und Trainingsdaten gehören zum verifizierten Backup-/Restoreumfang.

Docker-Sicherungen erfolgen über die Host-Volumes bei gestopptem Container
mit erhaltenen numerischen Eigentümern und Dateirechten. Das allgemeine
Vollbackup-Menü innerhalb des Containers unterstützt die getrennten privaten
Laufzeitdaten nicht. Einzelheiten stehen in der
[Docker-Dokumentation](Docker_Dokumentation.md).

## Fallback

Fehlt ein gültiges Modell, ist das Manifest unlesbar oder stimmt die Prüfsumme
nicht, wird keine alte Modelldatei geladen. E3DC-Control verwendet bis zu einem
Neutraining den konservativen, lokal berechneten Fallback. Das hebt weder
Reserve-, Netz-, Speicher- noch Verbraucher-Schutzgrenzen auf.

## Prüfung und Reparatur

Das Installationscenter zeigt, ob eine aktuelle Prognose und genügend lokale
Trainingsdaten vorhanden sind. Ein Modell wird nur über den vorgesehenen
Installer-/Dienstpfad neu aufgebaut; Dateien im privaten Modellverzeichnis
sollten nicht manuell kopiert, umbenannt oder durch Downloads ersetzt werden.

Auf Bare Metal bei einer Reparatur zuerst ein verifiziertes Backup erstellen
und anschließend den portablen Installer verwenden:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

Unter Docker erfolgt die Wartung über den aktuellen Host-Updater und den
geprüften Containerstart. Beim Rückfall auf ältere Root-Images werden Modelle
aus dem unprivilegierten Betrieb privat archiviert, damit das ältere Image
sie nicht als Root lädt; ein benötigtes Modell wird neu trainiert.

Rohe Statistikdaten, Prognosedateien oder Modellartefakte dürfen nicht in
öffentlichen Supportbeiträgen bereitgestellt werden.
