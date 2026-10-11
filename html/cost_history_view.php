<?php
/** Gemeinsame Kostenansicht für den mobilen Verlauf und das Desktopdiagramm. */
if (empty($costHistoryEnabled)) return;
?>
<div id="costHistoryPanel" class="cost-history d-none" data-today="<?= date('Y-m-d') ?>">
    <h5 class="text-info">Kostenhistorie (experimentell)</h5>
    <p class="small text-body-secondary">Zeitraum im Kalender auswählen. Monate zeigen Tage, Tage zeigen außerhalb der Jahresansicht Stunden und Stunden zeigen Viertelstunden.</p>
    <div id="costHistoryToday" class="cost-tiles" aria-label="Kosten im ausgewählten Zeitraum"></div>
    <div class="d-flex flex-wrap gap-2 align-items-end mb-3">
        <div class="btn-group btn-group-sm" role="group" aria-label="Zeitraum auswählen">
            <?php foreach (['day' => 'Tag', 'week' => 'Woche', 'month' => 'Monat', 'year' => 'Jahr'] as $period => $label): ?>
            <input type="radio" class="btn-check" name="costHistoryPeriod" id="costPeriod-<?= $period ?>" autocomplete="off" <?= $period === 'day' ? 'checked' : '' ?> onchange="setCostHistoryPeriod('<?= $period ?>')">
            <label class="btn btn-outline-secondary" for="costPeriod-<?= $period ?>"><?= $label ?></label>
            <?php endforeach; ?>
        </div>
        <input type="date" class="form-control form-control-sm" style="width:auto" aria-label="Tag oder Datum der Woche" id="costHistoryDate" value="<?= date('Y-m-d') ?>" onchange="loadCostHistory()" autocomplete="off">
        <input type="month" class="form-control form-control-sm" style="width:auto" aria-label="Monat" id="costHistoryMonth" value="<?= date('Y-m') ?>" onchange="loadCostHistory()" hidden autocomplete="off">
        <select class="form-select form-select-sm" style="width:auto" aria-label="Jahr" id="costHistoryYear" onchange="loadCostHistory()" hidden autocomplete="off"><option value="<?= date('Y') ?>"><?= date('Y') ?></option></select>
        <button type="button" class="btn btn-sm btn-outline-secondary" onclick="downloadCostHistoryCsv()">Download CSV</button>
    </div>
    <p id="costHistoryBackfill" role="status" class="small text-body-secondary"></p>
    <p id="costHistoryStatus" role="status" class="small text-body-secondary"></p>
    <div class="cost-scroll" tabindex="0" role="region" aria-label="Kosten je Tag, Stunde und Viertelstunde"><table class="cost-table">
        <thead><tr><th>Zeitraum</th><th class="cost-flow">Netzbezug</th><th class="cost-flow">Einspeiseerlös</th><th>Saldo</th><th>Hausverbrauch bewertet</th><th>Status</th></tr></thead>
        <tbody id="costHistoryRows"></tbody>
    </table></div>
    <div class="cost-explanation"><h6>Was die Statusangaben bedeuten</h6><ul>
        <li><span class="cost-state cost-ok">vollständig</span> Festgeschrieben, alle Messwerte und Preise vorhanden.</li>
        <li><span class="cost-state cost-provisional">vorläufig</span> Noch nicht festgeschrieben – die Werte werden stündlich um :55 gespeichert und ändern sich danach nicht mehr.</li>
        <li><span class="cost-state cost-estimated">geschätzt</span> Ein Teil beruht auf einer Annahme, z. B. Tarif vor dem Einschalten der Kostenansicht oder Wert des Akkuinhalts beim Start. Mit der Maus auf eine Angabe zeigen oder antippen, um den genauen Grund zu sehen.</li>
        <li><span class="cost-state cost-gap">Lücke</span> Es fehlen Messwerte oder Preise; die betroffenen Beträge bleiben leer statt erfunden. Teilsummen sind gekennzeichnet.</li>
    </ul><p class="small text-body-secondary mb-0">Hausverbrauch bewertet: Netzstrom zum Bezugspreis, PV-Strom zum entgangenen Einspeiseerlös und Akkustrom zum Mischpreis seiner Ladungen. Der Akku-Startwert wird aus bis zu 30 Tagen Minutenhistorie hergeleitet; der älteste Inhalt wird mit der Einspeisevergütung angesetzt. Ohne Rückblick gilt diese Vergütung direkt. Geschätzte Anteile bleiben gekennzeichnet. Die Bewertung ist kein zusätzlicher Rechnungsposten. Der Saldo enthält gegebenenfalls die anteilige DV-Grundgebühr; DV-Erlöse nach Gebühren sind eine Näherung.</p></div>
</div>
<style>
.cost-history [hidden]{display:none!important}
.cost-history{min-width:0;max-width:100%;color:var(--bs-body-color)}
.cost-tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(145px,1fr));gap:10px;margin-bottom:14px}
.cost-tile,.cost-explanation{background:var(--bs-tertiary-bg);border:1px solid var(--bs-border-color);border-radius:10px;padding:10px 12px}
.cost-tile b{font-size:20px;display:block}.cost-history small{color:var(--bs-secondary-color)}
.cost-scroll{max-width:100%;overflow-x:auto;border-radius:10px}.cost-table{width:100%;border-collapse:collapse;background:var(--bs-tertiary-bg)}
.cost-table th,.cost-table td{padding:8px 10px;border-bottom:1px solid var(--bs-border-color);text-align:right;vertical-align:top}
.cost-table th:first-child,.cost-table td:first-child{text-align:left}.cost-table th{font-size:12px;color:var(--bs-secondary-color)}
.cost-table button.cost-expand{color:inherit;border:0;padding:0;background:transparent;text-align:left;white-space:nowrap}
.cost-table .cost-level-1 td:first-child{padding-left:28px}.cost-table .cost-level-2 td:first-child{padding-left:52px}.cost-level-2{font-size:13px}
.cost-total{font-weight:600;border-top:2px solid var(--bs-border-color)}
.cost-table tr[data-expand]{cursor:pointer}.cost-table tr[data-expand]:hover{background:var(--bs-secondary-bg)}
.cost-state{font-size:12px;border-radius:10px;padding:2px 8px;white-space:nowrap;cursor:help;display:inline-block}
.cost-ok{background:var(--bs-success-bg-subtle);color:var(--bs-success-text-emphasis)}
.cost-estimated{background:var(--bs-warning-bg-subtle);color:var(--bs-warning-text-emphasis)}
.cost-provisional{background:var(--bs-primary-bg-subtle);color:var(--bs-primary-text-emphasis)}
.cost-gap{background:var(--bs-danger-bg-subtle);color:var(--bs-danger-text-emphasis)}
.cost-table .cost-empty td{text-align:left;color:var(--bs-secondary-color)}.cost-explanation{margin-top:14px;font-size:13px}.cost-explanation li{margin:3px 0}
@media(max-width:640px){.cost-table .cost-flow{display:none}.cost-table th,.cost-table td{padding:7px 6px}.cost-history input{max-width:100%}}
</style>
<script src="<?= htmlspecialchars(getAssetUrl('cost_history.js')) ?>"></script>
