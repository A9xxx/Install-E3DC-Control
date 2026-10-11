/* Gemeinsame Kalenderansicht mit Stunden, Viertelstunden und sichtbarem CSV-Ausschnitt. */
(function () {
    const expanded = new Set();
    let current = null;
    let visible = [];
    let controller = null;
    let period = 'day';
    let initialized = false;
    const details = new Map();
    const get = id => document.getElementById(id);
    const finite = value => typeof value === 'number' && Number.isFinite(value);
    const format = (value, digits = 2) => finite(value) ? value.toLocaleString('de-DE', {minimumFractionDigits: digits, maximumFractionDigits: digits}) : '–';
    const money = value => finite(value) ? format(value) + ' €' : '–';
    const energy = value => finite(value) ? format(value) + ' kWh' : '–';
    const valueOf = (row, field) => finite(row[field]) ? row[field] : row.amounts?.[field]?.sum_eur;
    const partialNote = (row, field) => {
        const part = row.amounts?.[field];
        return part && part.missing_quarters > 0 && finite(part.sum_eur)
            ? 'Teilsumme · ' + part.missing_quarters + ' Viertelstunden ohne Wert' : '';
    };
    const dateKey = timestamp => new Intl.DateTimeFormat('sv-SE', {timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit'}).format(new Date(timestamp * 1000));
    const clock = timestamp => new Intl.DateTimeFormat('de-DE', {timeZone: 'Europe/Berlin', hour: '2-digit', minute: '2-digit'}).format(new Date(timestamp * 1000));
    const labels = {
        minute_history_incomplete: 'Es fehlen Minutenwerte. Für diese Viertelstunde können keine verlässlichen Beträge berechnet werden.',
        aggregation_incomplete: 'Der Zeitraum ist noch nicht vollständig erfasst. Angezeigt werden nur die vorhandenen Teilwerte.',
        billing_price_missing: 'Der damals gültige Bezugspreis fehlt. Netzstrom kann deshalb nicht bewertet werden.',
        feedin_tariff_missing: 'Die Einspeisevergütung fehlt. Einspeisung, PV-Strom und der Akku-Startinhalt können deshalb nicht bewertet werden.',
        feedin_price_missing: 'Für diese Zeit fehlt der Einspeisepreis.',
        dv_market_price_missing: 'Der damalige Marktpreis der Direktvermarktung fehlt. Der Einspeiseerlös bleibt deshalb leer.',
        dv_fee_basis_missing: 'Die Berechnungsgrundlage für die Vermarktungsgebühren fehlt. Der Erlös nach Gebühren bleibt deshalb leer.',
        historical_configuration_estimated: 'Tarif geschätzt: Vor dem Einschalten wurde der Tarifstand nicht mitgeschrieben. Verwendet werden der damals gemessene Bezugspreis und die heutige Einspeisekonfiguration.',
        dv_revenue_estimate: 'Der Erlös der Direktvermarktung nach Gebühren und mit EEG-Mindestwert ist eine Näherung.',
        home_value_unknown: 'Für die Hausbewertung fehlen Messwerte oder der Preis einer verwendeten Energiequelle.',
        battery_initial_value_unknown: 'Der gespeicherte Akku-Startwert hat keinen belegten Preis. Bereits festgeschriebene Werte werden nicht verändert.',
        battery_archive_anchor_pending: 'Akku-Startwert wird beim nächsten Archivlauf hergeleitet',
        battery_anchor_tariff_missing: 'Der Akkuinhalt kann ohne Einspeisevergütung nicht geschätzt bewertet werden',
        battery_value_unknown: 'Energieinhalt oder Mischpreis des Akkus sind nicht ausreichend belegt. Die betroffene Bewertung bleibt leer.',
        battery_initial_value_estimated: 'Akku-Startwert geschätzt: Der älteste belegte Inhalt wird mit der Einspeisevergütung bewertet; spätere Ladungen mit ihren jeweiligen Preisen.',
        battery_gap_estimated: 'Akkuwert nach einer Messlücke geschätzt: Die Energie wird an Ladezustand und nutzbarer Kapazität verankert. Der bisherige Mischpreis bleibt, ein Zuwachs erhält die Einspeisevergütung.',
        battery_value_estimated: 'Ein Anteil des Akkuinhalts beruht noch auf einer Schätzung des Startwerts, einer Messlücke oder des Ladetarifs.',
        battery_consumption_estimated: 'Im Haus wurde Akkustrom verbraucht, dessen Wert teilweise geschätzt ist.',
        cost_total_archive_pending: 'Die Gesamtsumme wird beim nächsten Archivlauf aus den vorhandenen Kostenwerten aufgebaut. Bis dahin bleiben die Beträge leer.',
        cost_history_not_recorded: 'Keine Kostenwerte vorhanden. Der Stand der einmaligen Hintergrundprüfung steht oberhalb der Tabelle.'
    };
    const measurements = {pv: 'PV-Erzeugung', grid_in: 'Netzbezug', grid_out: 'Einspeisung', bat_in: 'Akkuladung', bat_out: 'Akkuentladung', home: 'Hausverbrauch', wb: 'Wallbox 1', wb2: 'Wallbox 2', wp: 'Wärmepumpe', hs: 'Heizstab', climate: 'Klimaanlage'};
    function status(row) {
        const reasons = (row.reasons || []).map(reason => labels[reason] || (reason.startsWith('measurement_missing_')
            ? 'Die Messung für ' + (measurements[reason.slice(20)] || 'einen Verbraucher') + ' fehlt oder ist unvollständig.'
            : 'Ein benötigter Wert ist nicht ausreichend belegt.'));
        if (!row.sealed) reasons.push('Vorläufig: Die Zeitspanne ist noch nicht abgeschlossen oder noch nicht festgeschrieben. Die Speicherung erfolgt stündlich um :55.');
        if ((row.home_includes || []).length) reasons.push('Haus inkl. ungemessener Verbraucher: ' + row.home_includes.map(key => measurements[key] || 'zusätzlicher Verbraucher').join(', ') + '. Diese Energie wird genau einmal im Haus gezählt.');
        const estimated = row.battery_estimated || (finite(row.battery_estimated_share) && row.battery_estimated_share > 0)
            || (row.reasons || []).some(reason => /estimated|estimate$/.test(reason));
        if (row.battery_estimated && !(row.reasons || []).includes('battery_value_estimated')) reasons.push(labels.battery_value_estimated);
        if (finite(row.battery_estimated_share) && row.battery_estimated_share > 0) reasons.push('Geschätzter Anteil am verbleibenden Akkuinhalt: ' + format(row.battery_estimated_share * 100, 1) + ' %.');
        const partial = Object.keys(row.amounts || {}).map(field => partialNote(row, field)).filter(Boolean);
        reasons.push(...partial);
        const pending = (row.reasons || []).includes('battery_archive_anchor_pending');
        const gap = row.no_data || [row.grid_cost_eur, row.feedin_revenue_eur, row.net_cost_eur, ...(pending ? [] : [row.home_cost_eur])].some(value => !finite(value))
            || row.recording_gap || (row.reasons || []).includes('minute_history_incomplete');
        const word = partial.length ? 'Teilsumme' : gap ? 'Lücke' : pending ? 'vorläufig' : estimated ? 'geschätzt' : !row.sealed || !row.complete ? 'vorläufig' : 'vollständig';
        return {word, className: gap ? 'gap' : estimated ? 'estimated' : word === 'vorläufig' ? 'provisional' : 'ok',
            text: reasons.length ? Array.from(new Set(reasons)).join(' • ') : 'Festgeschrieben, alle Messwerte und Preise vorhanden.'};
    }
    function node(tag, text, className) {
        const element = document.createElement(tag);
        if (text !== undefined) element.textContent = text;
        if (className) element.className = className;
        return element;
    }
    function average(row) { return finite(row.home_cost_eur) && finite(row.home_kwh) && row.home_kwh > 0 ? row.home_cost_eur * 100 / row.home_kwh : null; }
    function amount(row, field, sub, className) {
        const td = node('td', undefined, className);
        td.append(node('span', money(valueOf(row, field))), node('br'), node('small', [partialNote(row, field), sub].filter(Boolean).join(' · ')));
        if (!finite(row[field])) td.title = status(row).text;
        return td;
    }
    function dayLabel(row) {
        return new Intl.DateTimeFormat('de-DE', {timeZone: 'Europe/Berlin', weekday: 'short', day: '2-digit', month: '2-digit', year: 'numeric'}).format(new Date(row.ts_start * 1000));
    }
    const resolutionLabel = {month: 'Monat', day: 'Tag', hour: 'Stunde', quarter: 'Viertelstunde', total: 'Gesamt'};
    const monthLabel = row => new Intl.DateTimeFormat('de-DE', {timeZone: 'Europe/Berlin', month: 'long', year: 'numeric'}).format(new Date(row.ts_start * 1000));
    const offsetOf = row => ((row.label || '').match(/[+-]\d{2}:\d{2}$/) || [''])[0];
    async function read(params, request) {
        const timer = setTimeout(() => request.abort(), 15000);
        try {
            const response = await fetch('get_cost_history.php?' + new URLSearchParams(params), {cache: 'no-store', signal: request.signal});
            const result = await response.json();
            if (!response.ok || result.error) throw new Error(result.error || 'Kosten derzeit nicht verfügbar.');
            return result;
        } finally { clearTimeout(timer); }
    }
    function render() {
        const output = get('costHistoryRows');
        output.replaceChildren(); visible = [];
        function add(row, level, resolution, label) {
            const info = status(row), key = resolution + ':' + row.ts_start;
            const tr = node('tr', undefined, 'cost-level-' + level + (row.no_data ? ' cost-empty' : ''));
            const first = node('td');
            visible.push({row, level, resolution, label, status: info});
            if (row.no_data && resolution !== 'total') {
                first.textContent = label; const message = node('td', labels.cost_history_not_recorded); message.colSpan = 5;
                tr.append(first, message); output.append(tr); return;
            }
            const next = resolution === 'month' ? 'day' : resolution === 'day' && period !== 'year' ? 'hour' : resolution === 'hour' ? 'quarter' : null;
            if (next) {
                const button = node('button', (expanded.has(key) ? '▾ ' : '▸ ') + label, 'cost-expand');
                button.type = 'button'; button.setAttribute('aria-expanded', String(expanded.has(key)));
                async function toggle() {
                    if (expanded.has(key)) { expanded.delete(key); render(); return; }
                    const request = controller;
                    try {
                        if (!details.has(key)) {
                            const date = dateKey(row.ts_start);
                            const data = await read({period: resolution === 'month' ? 'month' : 'day', date, resolution: next}, request);
                            if (controller !== request) return;
                            if (!data.enabled) { window.loadCostHistory(); return; }
                            details.set(key, data.rows.filter(child => next !== 'quarter' || (child.ts_start >= row.ts_start && child.ts_start < row.ts_start + 3600)));
                        }
                        if (controller !== request) return;
                        expanded.add(key); render();
                    } catch (error) {
                        if (controller === request) get('costHistoryStatus').textContent = error.name === 'AbortError' ? 'Zeitüberschreitung beim Laden. Erneut versuchen.' : error.message;
                    }
                }
                button.addEventListener('click', event => { event.stopPropagation(); toggle(); });
                tr.dataset.expand = key; tr.addEventListener('click', toggle); first.append(button);
            } else first.textContent = label;
            const badge = node('span', info.word, 'cost-state cost-' + info.className);
            badge.title = info.text; badge.tabIndex = 0; badge.setAttribute('aria-label', info.word + ': ' + info.text);
            badge.addEventListener('click', event => { event.stopPropagation(); get('costHistoryStatus').textContent = info.text; });
            badge.addEventListener('keydown', event => { if (event.key === 'Enter' || event.key === ' ') { event.preventDefault(); get('costHistoryStatus').textContent = info.text; } });
            const stateCell = node('td'); stateCell.append(badge);
            const avg = average(row);
            tr.append(first, amount(row, 'grid_cost_eur', energy(row.grid_in_kwh), 'cost-flow'), amount(row, 'feedin_revenue_eur', energy(row.grid_out_kwh), 'cost-flow'),
                amount(row, 'net_cost_eur', ''), amount(row, 'home_cost_eur', finite(avg) ? 'Ø ' + format(avg, 1) + ' ct/kWh' : energy(row.home_kwh)), stateCell);
            output.append(tr);
            if (expanded.has(key)) {
                const children = details.get(key) || [];
                const showOffset = new Set(children.map(offsetOf)).size > 1 || label.match(/[+-]\d{2}:\d{2}$/);
                for (const child of children) {
                    const text = next === 'day' ? dayLabel(child) : clock(child.ts_start) + (next === 'hour' ? '–' + clock(child.ts_start + 3540) : '')
                        + (showOffset && offsetOf(child) ? ' ' + offsetOf(child) : '');
                    add(child, level + 1, next, text);
                }
            }
        }
        const showOffset = new Set(current.rows.map(offsetOf)).size > 1;
        const rows = current.resolution === 'hour' ? current.rows : [...current.rows].reverse();
        for (const row of rows) {
            const label = current.resolution === 'month' ? monthLabel(row) : current.resolution === 'day' ? dayLabel(row)
                : clock(row.ts_start) + '–' + clock(row.ts_start + 3540) + (showOffset && offsetOf(row) ? ' ' + offsetOf(row) : '');
            add(row, 0, current.resolution, label);
        }
        // Die Gesamtzeile bleibt unabhängig vom gewählten Zeitraum und ist nie aufklappbar.
        add(current.total || {no_data: true, sealed: true, reasons: ['cost_history_not_recorded']}, 0, 'total', current.total ? current.total.label : 'Gesamt seit Beginn der Aufzeichnung');
        output.lastChild.classList.add('cost-total');
    }
    function periodCaption(data) {
        const today = get('costHistoryPanel').dataset.today;
        if (period === 'day') return data.start === today ? 'Heute' : data.start.split('-').reverse().join('.');
        if (period === 'week') return data.start <= today && today < data.end ? 'Diese Woche' : 'Woche ab ' + data.start.split('-').reverse().join('.');
        if (period === 'month') return monthLabel({ts_start: new Date(data.start + 'T12:00:00+01:00').getTime() / 1000});
        return data.start.slice(0, 4);
    }
    function renderTiles(data) {
        const tiles = get('costHistoryToday'); tiles.replaceChildren();
        const row = data.summary || {no_data: true, sealed: true, reasons: ['cost_history_not_recorded']};
        const caption = periodCaption(data);
        for (const [label, field, sub] of [
            [caption + ' Netzbezug', 'grid_cost_eur', energy(row.grid_in_kwh)],
            [caption + ' Einspeiseerlös', 'feedin_revenue_eur', energy(row.grid_out_kwh)],
            [caption + ' Saldo', 'net_cost_eur', 'Netzbezug minus Erlös, ggf. DV-Grundgebühr'],
            [caption + ' Hausverbrauch bewertet', 'home_cost_eur', energy(row.home_kwh) + (finite(average(row)) ? ' · Ø ' + format(average(row), 1) + ' ct/kWh' : '')]
        ]) {
            const tile = node('div', undefined, 'cost-tile'); tile.title = status(row).text;
            tile.append(node('small', label), node('b', money(valueOf(row, field))), node('small', [partialNote(row, field), sub].filter(Boolean).join(' · '))); tiles.append(tile);
        }
    }
    function updatePickers() {
        get('costHistoryDate').hidden = period === 'month' || period === 'year';
        get('costHistoryMonth').hidden = period !== 'month';
        get('costHistoryYear').hidden = period !== 'year';
        for (const name of ['day', 'week', 'month', 'year']) get('costPeriod-' + name).checked = period === name;
    }
    window.setCostHistoryPeriod = function (value) {
        if (!['day', 'week', 'month', 'year'].includes(value)) return;
        period = value; updatePickers(); window.loadCostHistory();
    };
    window.loadCostHistory = async function () {
        const panel = get('costHistoryPanel'); if (!panel) return;
        const message = get('costHistoryStatus');
        if (!initialized) {
            const today = panel.dataset.today || dateKey(Date.now() / 1000);
            get('costHistoryDate').value = today; get('costHistoryMonth').value = today.slice(0, 7);
            get('costHistoryYear').value = today.slice(0, 4); period = 'day'; initialized = true; updatePickers();
        }
        const date = get(period === 'month' ? 'costHistoryMonth' : period === 'year' ? 'costHistoryYear' : 'costHistoryDate').value;
        if (!/^(\d{4}|\d{4}-\d{2}|\d{4}-\d{2}-\d{2})$/.test(date)) { message.textContent = 'Ein gültiges Datum auswählen.'; return; }
        if (controller) controller.abort();
        const request = new AbortController(); controller = request;
        message.textContent = 'Kosten werden geladen…';
        try {
            const data = await read({period, date, resolution: period === 'year' ? 'month' : period === 'day' ? 'hour' : 'day'}, request);
            if (controller !== request) return;
            if (!data.enabled) {
                current = null; visible = []; get('costHistoryRows').replaceChildren(); get('costHistoryToday').replaceChildren();
                window.switchCostHistoryView(false, false); panel.classList.add('d-none');
                const control = get('costHistorySwitch'); if (control) control.classList.add('d-none');
                get('costHistoryBackfill').textContent = ''; message.textContent = 'Kostenansicht ausgeschaltet.'; return;
            }
            current = data; expanded.clear(); details.clear();
            const year = get('costHistoryYear'), selected = year.value;
            const firstYear = Number((data.total && data.total.date || panel.dataset.today).slice(0, 4));
            const lastYear = Number(panel.dataset.today.slice(0, 4));
            year.replaceChildren();
            for (let at = lastYear; at >= firstYear; at--) { const option = node('option', String(at)); option.value = String(at); year.append(option); }
            year.value = selected;
            render(); renderTiles(data);
            get('costHistoryBackfill').textContent = data.backfill ? data.backfill.text : '';
            message.textContent = 'Zeitraum: ' + periodCaption(data) + '. CSV enthält die sichtbaren Zeilen; Summen und Details nicht nochmals addieren.';
        } catch (error) {
            if (controller === request) {
                current = null; visible = []; get('costHistoryRows').replaceChildren(); get('costHistoryToday').replaceChildren(); get('costHistoryBackfill').textContent = '';
                message.textContent = error.name === 'AbortError' ? 'Zeitüberschreitung beim Laden. Erneut versuchen.' : error.message;
            }
        }
    };
    window.downloadCostHistoryCsv = function () {
        if (!current) return;
        const quote = value => '"' + String(value ?? '').replace(/"/g, '""') + '"';
        const lines = [['Auflösung', 'Datum', 'Zeitraum', 'Netzbezug EUR', 'Netzbezug kWh', 'Einspeiseerlös EUR', 'Einspeisung kWh', 'Saldo EUR', 'Hausbewertung EUR', 'Haus kWh', 'Haus Ø ct/kWh', 'Status', 'Grund', 'Hinweis', 'Netzbezug ohne Wert', 'Einspeiseerlös ohne Wert', 'Saldo ohne Wert', 'Hausbewertung ohne Wert']];
        for (const entry of visible) {
            const row = entry.row;
            const values = ['grid_cost_eur', 'grid_in_kwh', 'feedin_revenue_eur', 'grid_out_kwh', 'net_cost_eur', 'home_cost_eur', 'home_kwh'].map(key => finite(valueOf(row, key)) ? String(valueOf(row, key)).replace('.', ',') : '');
            lines.push([resolutionLabel[entry.resolution], row.date || dateKey(row.ts_start), entry.label, ...values,
                finite(average(row)) ? String(average(row)).replace('.', ',') : '', entry.status.word, entry.status.text, entry.resolution === 'total' ? 'nicht mit den übrigen Zeilen addieren' : '',
                ...['grid_cost_eur', 'feedin_revenue_eur', 'net_cost_eur', 'home_cost_eur'].map(field => row.amounts?.[field]?.missing_quarters ?? '')]);
        }
        const csv = '\ufeff' + lines.map(line => line.map(quote).join(';')).join('\r\n') + '\r\n';
        const url = URL.createObjectURL(new Blob([csv], {type: 'text/csv;charset=utf-8'}));
        const link = node('a'); link.href = url; link.download = 'kostenhistorie.csv'; link.click();
        setTimeout(() => URL.revokeObjectURL(url), 1000);
    };
    window.switchCostHistoryView = function (show, reload = true) {
        const control = get('costHistorySwitch'), desktop = get('desktopCostHistory'), panel = get('costHistoryPanel');
        if (!control || !desktop || !panel) return;
        show = !!show && document.body.getAttribute('data-chart-mode') === 'price';
        for (const element of [get('main-refresh-btn'), document.querySelector && document.querySelector('.card-header .btn-chart-flip')]) {
            if (element) element.style.visibility = show ? 'hidden' : '';
        }
        desktop.hidden = !show; panel.classList.toggle('d-none', !show);
        const surface = get('primaryChartSurface'); if (surface) surface.hidden = show;
        for (const [id, selected] of [['costHistoryButton', show], ['costDiagramButton', !show]]) {
            const button = get(id); if (button) { button.classList.toggle('active', selected); button.setAttribute('aria-pressed', String(selected)); }
        }
        if (show) {
            for (const id of ['live-controls', 'diagramDetails']) { const element = get(id); if (element) element.style.display = 'none'; }
            if (typeof activateJsChartMode === 'function') activateJsChartMode('costs');
            if (reload) window.loadCostHistory();
        } else if (reload && typeof switchChartMode === 'function') switchChartMode('price');
    };
})();
