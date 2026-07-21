# Notice

Copyright (C) 2026 A9x and contributors.

## Herkunft und Inspiration

E3DC-Control V4 ist eine eigenständige Python-Neuimplementierung und
Weiterentwicklung der Speicher-, Wallbox- und Energiemanagement-Logik für
E3DC-Systeme.

Die Regelphilosophie und viele fachliche Entscheidungen sind vom langjährig
bewährten C++-Projekt von Eberhard Mayer (Eba-M/E3DC-Control) inspiriert.
Der ursprüngliche C++-Code wurde als Referenz gelesen und verstanden, aber
nicht einfach kopiert. Ziel war es, die robuste Regelbasis des C++-Systems in
eine modulare Python-V4-Architektur mit Wetterprognose, ML-Prognose,
Multi-Anker-Ladekurve, Preislogik und moderner Weboberfläche zu übertragen.

Die Nutzung des ursprünglichen Projekts als fachliche Grundlage erfolgte mit
Zustimmung des Entwicklers. Dafür gilt unser ausdrücklicher Dank.

## RSCP-Regelbasis

Ein wesentlicher Teil der Stabilisierung der Python-V4-Speicherregelung
entstand aus dem direkten Abgleich mit der ursprünglichen C++-Regellogik.

Insbesondere die Zuordnung einer Ladeanforderung zum E3DC-RSCP-Modus wurde aus
der fachlichen Beschreibung des C++-Verhaltens abgeleitet:

- `Req_Load == 0` -> `MODE_IDLE`
- `Req_Load == maximumLadeleistung` -> `MODE_AUTO`
- `0 < Req_Load < maximumLadeleistung` -> `MODE_CHARGE`
- `Req_Load < 0` -> `MODE_DISCHARGE`
- `Req_Load > maximumLadeleistung` -> `MODE_GRID`

Diese Logik wurde in Python eigenständig neu implementiert und mit der
V4-Prognose-, Ladekurven-, Wallbox- und Preislogik kombiniert.

## Lizenz

E3DC-Control V4 steht unter der GNU Affero General Public License v3.0 oder
später (`AGPL-3.0-or-later`).

Private Nutzung, Anpassung und Community-Weiterentwicklung sind ausdrücklich
willkommen.

Wer dieses Projekt, abgeleitete Versionen oder darauf basierende Dienste
öffentlich bereitstellt, verteilt oder kommerziell nutzt, muss die Bedingungen
der AGPL einhalten und den vollständigen zugehörigen Quellcode offenlegen.

Kommerzielle Sonderlizenzen oder Integrationen außerhalb der AGPL sind nur
nach vorheriger schriftlicher Zustimmung möglich.

## Drittcode

Einzelne Dateien oder Bibliotheken können eigene Copyright- oder
Lizenzhinweise enthalten. Diese Hinweise bleiben unberührt.

Die Weboberfläche liefert folgende feste Drittanbieter-Abhängigkeiten lokal
aus, damit beim Öffnen keine automatischen CDN-Anfragen entstehen:

- Bootstrap 5.3.0 (MIT)
- Bootstrap Icons 1.11.3 (MIT)
- Font Awesome Free 6.5.1 (Icons/Fonts/Code unter den im Paket genannten freien Lizenzen)
- Chart.js 4.5.1 (MIT)
- chartjs-plugin-zoom 2.0.1 (MIT)
- Hammer.js 2.0.7 (MIT)
- jQuery 3.6.0 (MIT)

Die zugehörigen Lizenztexte liegen jeweils unter
`html/assets/vendor/<paket>/LICENSE.txt` und werden zusammen mit den lokalen
Assets ausgeliefert.

Die lokale Matter-Bridge verwendet die fest in
`Installer/matter/package-lock.json` aufgelösten Laufzeitpakete
`@project-chip/matter-node.js` und `@project-chip/matter.js` 0.7.1
(Apache-2.0). Das Lockfile bindet auch die transitiven Paketversionen und
Integritätswerte. Für diese Pakete gelten die jeweils mitgelieferten
Lizenzhinweise. Vor Veröffentlichung eines Release-Images werden dessen SBOM
und Lizenzbericht separat erzeugt und geprüft.
