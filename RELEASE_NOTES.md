# v5.3.2b – Stable-Release auf bereinigter Historienbasis

`v5.3.2b` veröffentlicht den fachlich geprüften Produktstand als bereinigten,
parentlosen Rollback-Root. Keine ältere öffentliche Git-Historie ist sein
Vorfahr. Commit-SHA, Tree-ID, Git-Archiv-Hash und OCI-Digest werden im
getrennten Freigabemanifest mit dem annotierten Tag dokumentiert.

## Nutzerrelevante Änderungen

- Der fachliche Produktumfang entspricht dem vollständig geprüften
  Releasebaum; zusätzliche Produktlogik ist nicht enthalten.
- Updates und Wiederherstellungen verwenden ein externes, manifestiertes
  Backup mit SHA-256-Prüfung und brechen bei leeren, unvollständigen oder
  unlesbaren Sicherungen hart ab.
- Der einmalige Wechsel aus einer älteren oder nicht verwandten Git-Historie
  läuft über den geprüften Installer-/Bootstrapweg; ein gewöhnliches
  `git pull --ff-only` ist dafür nicht vorgesehen.
- Lokale Hilfe-Assets werden ohne automatische CDN-Aufrufe ausgeliefert.
  Diagnosehinweise fordern nicht zur öffentlichen Weitergabe roher Logs auf.
- Matter bleibt als lokale, nicht zertifizierte read-only Bridge erhalten.
  Kopplungsdaten bleiben lokal und werden in Backups einbezogen.
- Shadow-Vergleichsbetrieb, modernes Frontend sowie read-only V2H-/V2G-
  Telemetrie bleiben erhalten. Eine aktive V2H-/V2G-Steuerung ist nicht
  freigegeben.
- Morning Boost und Superintelligenz werden über die dokumentierten
  Pre-Dump-/Ladekurvenpfade abgebildet; die bisherigen Alt-Schalter werden
  nicht wieder aktiviert.

## Veröffentlichungsvertrag

- `v5.3.2b` ist der parentlose Root und besitzt keinen älteren öffentlichen
  Rollback.
- Das Image erhält nur versionierte Tags; `latest` darf niemals auf R0 zeigen.
- GitHub-Release und GHCR-Image entstehen erst nach einer separaten manuellen
  Freigabe der exakten Git-Objekte. Ein erfolgreicher lokaler Prüfnachweis ist
  weder ein Image-Build noch eine Veröffentlichungsfreigabe.
