# Notice

Copyright (C) 2026 A9x and contributors.

## Herkunft und Inspiration

E3DC-Control V4 ist eine eigenstaendige Python-Neuimplementierung und
Weiterentwicklung der Speicher-, Wallbox- und Energiemanagement-Logik fuer
E3DC-Systeme.

Die Regelphilosophie und viele fachliche Entscheidungen sind vom langjaehrig
bewaehreten C++-Projekt von Eberhard Mayer (Eba-M/E3DC-Control) inspiriert.
Der urspruengliche C++-Code wurde als Referenz gelesen und verstanden, aber
nicht einfach kopiert. Ziel war es, die robuste Regelbasis des C++-Systems in
eine modulare Python-V4-Architektur mit Wetterprognose, ML-Prognose,
Multi-Anker-Ladekurve, Preislogik und moderner Weboberflaeche zu uebertragen.

Die Nutzung des urspruenglichen Projekts als fachliche Grundlage erfolgte mit
Zustimmung des Entwicklers. Dafuer gilt unser ausdruecklicher Dank.

## RSCP-Regelbasis

Ein wesentlicher Teil der Stabilisierung der Python-V4-Speicherregelung
entstand aus dem direkten Abgleich mit der urspruenglichen C++-Regellogik.

Insbesondere die Zuordnung einer Ladeanforderung zum E3DC-RSCP-Modus wurde aus
der fachlichen Beschreibung des C++-Verhaltens abgeleitet:

- `Req_Load == 0` -> `MODE_IDLE`
- `Req_Load == maximumLadeleistung` -> `MODE_AUTO`
- `0 < Req_Load < maximumLadeleistung` -> `MODE_CHARGE`
- `Req_Load < 0` -> `MODE_DISCHARGE`
- `Req_Load > maximumLadeleistung` -> `MODE_GRID`

Diese Logik wurde in Python eigenstaendig neu implementiert und mit der
V4-Prognose-, Ladekurven-, Wallbox- und Preislogik kombiniert.

## Lizenz

E3DC-Control V4 steht unter der GNU Affero General Public License v3.0 oder
spaeter (`AGPL-3.0-or-later`).

Private Nutzung, Anpassung und Community-Weiterentwicklung sind ausdruecklich
willkommen.

Wer dieses Projekt, abgeleitete Versionen oder darauf basierende Dienste
oeffentlich bereitstellt, verteilt oder kommerziell nutzt, muss die Bedingungen
der AGPL einhalten und den vollstaendigen zugehoerigen Quellcode offenlegen.

Kommerzielle Sonderlizenzen oder Integrationen ausserhalb der AGPL sind nur
nach vorheriger schriftlicher Zustimmung moeglich.

## Drittcode

Einzelne Dateien oder Bibliotheken koennen eigene Copyright- oder
Lizenzhinweise enthalten. Diese Hinweise bleiben unberuehrt.

Die Weboberflaeche liefert folgende feste Drittanbieter-Abhaengigkeiten lokal
aus, damit beim Oeffnen keine automatischen CDN-Anfragen entstehen:

- Bootstrap 5.3.2 (MIT)
- Bootstrap Icons 1.11.3 (MIT)
- Font Awesome Free 6.5.1 (Icons/Fonts/Code unter den im Paket genannten freien Lizenzen)
- Chart.js 4.4.2 (MIT)
- chartjs-plugin-zoom 2.0.1 (MIT)
- Hammer.js 2.0.8 (MIT)
- jQuery 3.6.0 (MIT)

Die zugehoerigen Lizenztexte liegen jeweils unter
`html/assets/vendor/<paket>/LICENSE.txt` und werden zusammen mit den lokalen
Assets ausgeliefert.

Die lokale Matter-Bridge verwendet die fest in
`Installer/matter/package-lock.json` aufgeloesten Laufzeitpakete
`@project-chip/matter-node.js` und `@project-chip/matter.js` 0.7.1
(Apache-2.0). Deren transitive Pakete stehen laut den jeweiligen
Paketmetadaten unter MIT- oder ISC-Lizenzen. Die npm-Pakete samt ihren
Lizenzdateien werden beim Image-Build aus dem Lockfile installiert und im
SBOM des Release-Images ausgewiesen.

