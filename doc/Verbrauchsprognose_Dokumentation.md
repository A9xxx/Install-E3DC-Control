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
  `/var/lib/e3dc-control/ml` und ist nur für das bestätigte Installationskonto
  zugänglich.
- Modell, Manifest und Prüfsumme werden atomar geschrieben und beim Laden
  gemeinsam geprüft.
- Alte ungebundene Modelldateien aus dem Web-Datenverzeichnis werden nicht
  geladen oder übernommen.
- Modell- und Trainingsdaten gehören zum verifizierten Backup-/Restoreumfang.

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

Bei einer Reparatur zuerst ein verifiziertes Backup erstellen und anschließend
den portablen Installer verwenden:

```bash
export E3DC_INSTALL_PATH="/absoluter/pfad/zur/installation"
bash "$E3DC_INSTALL_PATH/e3dc-setup" --check
```

Rohe Statistikdaten, Prognosedateien oder Modellartefakte dürfen nicht in
öffentlichen Supportbeiträgen bereitgestellt werden.
