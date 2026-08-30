/**
 * Globals and Shared Functions (Desktop & Mobile)
 */
let flowAnimationCache = {};
let priceTendencyHtml = '';
let statsViewActive = false;
let currentStatsDate = 'today';
const WALLBOX_DISPLAY_HOLD_MS = 45000;
let wallboxDisplayCache = {};
const FLOW_COLOR_DEFAULTS = {
    pv: '#ffc107',
    external_pv: '#22c55e',
    grid: '#6c757d',
    grid_import: '#ef4444',
    grid_export: '#2ecc71',
    home: '#0dcaf0',
    battery: '#198754',
    battery_charge: '#2ecc71',
    wallbox: '#2ecc71',
    wallbox2: '#34d399',
    heatpump: '#f97316',
    heater: '#fd7e14',
    climate: '#38bdf8',
    generation: '#22c55e',
    consumption: '#0dcaf0',
    center: '#0d6efd'
};
const FLOW_LABEL_DEFAULTS = {
    pv: 'E3DC-PV', external_pv: 'Zusatz-WR', grid: 'Netz', battery: 'Speicher',
    home: 'Haus', wallbox: 'Wallbox 1', wallbox2: 'Wallbox 2', heatpump: 'Wärmepumpe',
    heater: 'Heizstab', climate: 'Klima', generation: 'Erzeugung', consumption: 'Verbrauch',
    center: 'E3DC-Control'
};
let flowEditorState = null;

function wallboxConfiguredFlag(data, wallboxNo = 1) {
    if (!data || typeof data !== 'object') return false;
    const secondWallbox = Number(wallboxNo) === 2;
    const flagKey = secondWallbox ? 'wb2_configured' : 'wb_configured';

    // Sobald das Backend die Konfiguration ausdrücklich projiziert, ist
    // dieses Flag verbindlich. Alte Leistungs-, Lock- oder Fahrzeugwerte
    // dürfen eine explizit deaktivierte Wallbox nicht wieder einblenden.
    if (Object.prototype.hasOwnProperty.call(data, flagKey)) {
        const value = data[flagKey];
        if (value === true || value === 1) return true;
        if (value === false || value === 0 || value === null || value === undefined) return false;
        return ['1', 'true', 'yes', 'on'].includes(String(value).trim().toLowerCase());
    }

    // Kompatibilität für ältere Live-Endpunkte ohne Konfigurationsflag:
    // Nur echte Konfigurationsmetadaten oder physische Aktivität gelten als
    // Nachweis. Das regulär vorhandene WB2-Leistungsfeld mit 0 W genügt nicht.
    const prefix = secondWallbox ? 'wb2' : 'wb';
    const nativeType = String(data[`${prefix}_native_type`] || '').trim().toLowerCase();
    const configuredRole = String(data[`${prefix}_configured_role`] || '').trim().toLowerCase();
    const typeConfigured = nativeType !== '' && !['none', 'off', 'disabled', '0', 'false'].includes(nativeType);
    const roleConfigured = configuredRole !== '' && !['none', 'off', 'disabled'].includes(configuredRole);
    const power = Math.abs(Number(data[prefix]) || 0);
    const connected = data[`${prefix}_locked`] === true
        || data[`${prefix}_locked`] === 1
        || data[`${prefix}_locked`] === '1'
        || (!secondWallbox && (
            data.wb_plug === true
            || data.wb_plug === 1
            || data.wb_plug === '1'
        ));
    return typeConfigured || roleConfigured || connected || power > 50;
}

function normalizeFlowColor(value, fallback = '#6c757d') {
    const raw = String(value || '').trim().toLowerCase();
    if (/^#[0-9a-f]{6}$/.test(raw)) return raw;
    const short = raw.match(/^#([0-9a-f])([0-9a-f])([0-9a-f])$/);
    if (short) return '#' + short[1] + short[1] + short[2] + short[2] + short[3] + short[3];
    return fallback;
}

function getEnergyFlowColors() {
    const stored = window.UI_ENERGY_FLOW && window.UI_ENERGY_FLOW.colors ? window.UI_ENERGY_FLOW.colors : {};
    return {...FLOW_COLOR_DEFAULTS, ...stored};
}

function normalizeFlowLabel(value) {
    return String(value ?? '').replace(/[\u0000-\u001f\u007f<>]/g, '').trim().slice(0, 32);
}

function getEnergyFlowLabels() {
    const stored = window.UI_ENERGY_FLOW && window.UI_ENERGY_FLOW.labels ? window.UI_ENERGY_FLOW.labels : {};
    return {...stored};
}

function getFlowLabel(key) {
    return normalizeFlowLabel(getEnergyFlowLabels()[key]) || FLOW_LABEL_DEFAULTS[key] || key;
}

function applyEnergyFlowLabels(container = document.getElementById('flow-view')) {
    if (!container) return;
    container.querySelectorAll('[data-flow-label-key]').forEach(label => {
        label.textContent = getFlowLabel(label.dataset.flowLabelKey);
    });
    container.querySelectorAll('[data-flow-node]').forEach(node => {
        const key = node.dataset.flowNode;
        const handle = node.querySelector('.flow-drag-handle');
        if (key && handle) handle.setAttribute('aria-label', `${getFlowLabel(key)} verschieben`);
    });
}

function getFlowColor(key, fallback = null) {
    const colors = getEnergyFlowColors();
    return normalizeFlowColor(colors[key], fallback || FLOW_COLOR_DEFAULTS[key] || '#6c757d');
}

function flowColorAlpha(key, alpha, fallback = null) {
    const hex = getFlowColor(key, fallback);
    const r = parseInt(hex.slice(1, 3), 16);
    const g = parseInt(hex.slice(3, 5), 16);
    const b = parseInt(hex.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function applyFlowNodeColor(node, color) {
    if (!node) return;
    const hex = normalizeFlowColor(color);
    const glow = node.closest('[data-flow-layout="mobile"]') ? 15 : 20;
    node.style.setProperty('--flow-node-color', hex);
    node.style.borderColor = hex;
    node.style.color = hex;
    node.style.boxShadow = `0 0 ${glow}px ${flowColorAlphaRaw(hex, 0.38)}`;
}

function flowColorAlphaRaw(hex, alpha) {
    const clean = normalizeFlowColor(hex);
    const r = parseInt(clean.slice(1, 3), 16);
    const g = parseInt(clean.slice(3, 5), 16);
    const b = parseInt(clean.slice(5, 7), 16);
    return `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

function applyFlowSelectorColor(selector, color) {
    document.querySelectorAll(selector).forEach(node => applyFlowNodeColor(node, color));
}

function getGridFlowStatus(gridVal) {
    if (gridVal < -15) {
        return {
            key: 'grid_export',
            hex: getFlowColor('grid_export', '#2ecc71')
        };
    }
    if (gridVal > 15) {
        return {
            key: 'grid_import',
            hex: getFlowColor('grid_import', '#ef4444')
        };
    }
    return {
        key: 'grid',
        hex: getFlowColor('grid', '#6c757d')
    };
}

function flowNodePosition(node) {
    if (!node) return {x: 50, y: 50};
    const x = parseFloat(node.style.left || node.dataset.flowX || node.dataset.defaultX || '50');
    const y = parseFloat(node.style.top || node.dataset.flowY || node.dataset.defaultY || '50');
    return {
        x: Number.isFinite(x) ? x : 50,
        y: Number.isFinite(y) ? y : 50
    };
}

function flowCanvas(container) {
    if (!container) return null;
    return container.querySelector('[data-flow-canvas]') || container;
}

function flowNodeRenderedCenter(container, node) {
    if (!container || !node) return flowNodePosition(node);
    const canvas = flowCanvas(container);
    const containerRect = canvas.getBoundingClientRect();
    const nodeRect = node.getBoundingClientRect();
    if (!containerRect.width || !containerRect.height || !nodeRect.width || !nodeRect.height) {
        return flowNodePosition(node);
    }
    return {
        x: ((nodeRect.left + nodeRect.width / 2 - containerRect.left) / containerRect.width) * 100,
        y: ((nodeRect.top + nodeRect.height / 2 - containerRect.top) / containerRect.height) * 100
    };
}

function flowNodeByKey(container, key) {
    return container ? container.querySelector(`[data-flow-node="${key}"]`) : null;
}

function updateEnergyFlowLines(container = document.getElementById('flow-view')) {
    if (!container) return;
    container.querySelectorAll('[data-flow-line][data-flow-from][data-flow-to]').forEach(line => {
        const fromNode = flowNodeByKey(container, line.dataset.flowFrom);
        const toNode = flowNodeByKey(container, line.dataset.flowTo);
        const from = flowNodeRenderedCenter(container, fromNode);
        const to = flowNodeRenderedCenter(container, toNode);
        line.setAttribute('x1', `${from.x}%`);
        line.setAttribute('y1', `${from.y}%`);
        line.setAttribute('x2', `${to.x}%`);
        line.setAttribute('y2', `${to.y}%`);
    });
}

function setEnergyFlowGenerationAggregateVisible(container, visible) {
    if (!container) return false;
    const generation = container.querySelector('[data-flow-node="generation"]');
    const active = visible === true && !!generation;
    if (generation) generation.hidden = !active;
    container.classList.toggle('flow-has-generation-aggregate', active);
    container.querySelectorAll('[data-flow-generation-aggregate="1"]').forEach(line => {
        line.style.display = active ? '' : 'none';
    });
    container.querySelectorAll('#flow-line-pv, #flow-dot-pv, #flow-line-external-pv, #flow-dot-external-pv').forEach(line => {
        line.dataset.flowTo = active ? 'generation' : 'center';
    });
    ['pv', 'external_pv', 'generation'].forEach(key => {
        const node = container.querySelector(`[data-flow-node="${key}"]`);
        if (!node || node.dataset.flowPositionCustom === '1') return;
        const x = Number(active ? node.dataset.flowAggregateX : node.dataset.flowDirectX);
        const y = Number(active ? node.dataset.flowAggregateY : node.dataset.flowDirectY);
        if (!Number.isFinite(x) || !Number.isFinite(y)) return;
        node.style.left = `${x}%`;
        node.style.top = `${y}%`;
        node.dataset.flowX = String(x);
        node.dataset.flowY = String(y);
    });
    updateEnergyFlowLines(container);
    return active;
}

function applyEnergyFlowBaseColors(container = document.getElementById('flow-view')) {
    if (!container) return;
    container.querySelectorAll('[data-flow-color-key]').forEach(el => {
        const color = getFlowColor(el.dataset.flowColorKey);
        if (el.classList && el.classList.contains('flow-node')) applyFlowNodeColor(el, color);
        if (el.classList && (el.classList.contains('flow-line') || el.classList.contains('flow-dots'))) {
            el.setAttribute('stroke', color);
        }
    });
    const centerImg = container.querySelector('[data-flow-node="center"] img');
    if (centerImg) centerImg.style.boxShadow = `0 0 30px ${getFlowColor('center')}`;
}

function snapshotEnergyFlow(container) {
    const nodes = {};
    container.querySelectorAll('[data-flow-node]').forEach(node => {
        const key = node.dataset.flowNode;
        nodes[key] = {
            ...flowNodePosition(node),
            custom: node.dataset.flowPositionCustom === '1'
        };
    });
    const ui = window.UI_ENERGY_FLOW || {};
    return {
        nodes,
        colors: {...getEnergyFlowColors()},
        labels: {...getEnergyFlowLabels()},
        revisions: {...(ui.revisions || {})},
        revision: String(ui.revision || '')
    };
}

function setFlowNodePosition(container, key, x, y, options = {}) {
    const node = flowNodeByKey(container, key);
    if (!node) return;
    const canvas = flowCanvas(container);
    const containerRect = canvas.getBoundingClientRect();
    const nodeRect = node.getBoundingClientRect();
    const radiusXPct = containerRect.width > 0 && nodeRect.width > 0
        ? ((nodeRect.width / 2 + 2) / containerRect.width) * 100
        : 4;
    const radiusYPct = containerRect.height > 0 && nodeRect.height > 0
        ? ((nodeRect.height / 2 + 2) / containerRect.height) * 100
        : 4;
    const minX = Math.max(1, radiusXPct);
    const minY = Math.max(1, radiusYPct);
    const px = Math.max(minX, Math.min(100 - minX, x));
    const py = Math.max(minY, Math.min(100 - minY, y));
    node.style.left = `${px}%`;
    node.style.top = `${py}%`;
    node.dataset.flowX = String(Math.round(px * 100) / 100);
    node.dataset.flowY = String(Math.round(py * 100) / 100);
    if (options.custom === true) node.dataset.flowPositionCustom = '1';
    if (key === 'battery') {
        const back = container.querySelector('[data-flow-back="battery"]');
        if (back) {
            back.style.left = `${px}%`;
            back.style.top = `${py}%`;
            back.dataset.flowX = node.dataset.flowX;
            back.dataset.flowY = node.dataset.flowY;
        }
    }
    updateEnergyFlowLines(container);
}

function clearEnergyFlowDrag(event = null, options = {}) {
    if (!flowEditorState) return false;
    const active = flowEditorState.dragging || flowEditorState.pendingDrag;
    if (!active) return false;
    const force = options.force === true;
    const eventPointerId = event && event.pointerId !== undefined ? event.pointerId : undefined;
    if (!force && eventPointerId !== undefined && eventPointerId !== active.pointerId) return false;
    const handle = active.handle || null;
    const node = active.node || null;
    if (handle && active.pointerId !== undefined) {
        try {
            if (typeof handle.hasPointerCapture !== 'function' || handle.hasPointerCapture(active.pointerId)) {
                handle.releasePointerCapture(active.pointerId);
            }
        } catch (e) {}
    }
    if (node) node.classList.remove('flow-dragging');
    flowEditorState.dragging = null;
    flowEditorState.pendingDrag = null;
    return true;
}

function collectEnergyFlowNodes(container) {
    const nodes = {};
    container.querySelectorAll('[data-flow-node]').forEach(node => {
        const key = node.dataset.flowNode;
        const pos = flowNodePosition(node);
        nodes[key] = {x: Math.round(pos.x * 100) / 100, y: Math.round(pos.y * 100) / 100};
    });
    return nodes;
}

function setFlowEditorSelected(container, key) {
    if (!container || !flowEditorState) return;
    flowEditorState.selected = key;
    container.querySelectorAll('.flow-node.flow-selected').forEach(n => n.classList.remove('flow-selected'));
    const node = flowNodeByKey(container, key);
    if (node) node.classList.add('flow-selected');
    const select = container.querySelector('[data-flow-color-select]');
    const input = container.querySelector('[data-flow-color-input]');
    const labelInput = container.querySelector('[data-flow-label-input]');
    const fixedLabel = key === 'center';
    if (select && [...select.options].some(opt => opt.value === key)) select.value = key;
    if (input) input.value = getFlowColor(key);
    if (labelInput) {
        labelInput.disabled = fixedLabel;
        labelInput.value = fixedLabel ? '' : normalizeFlowLabel(getEnergyFlowLabels()[key] || '');
        labelInput.placeholder = fixedLabel ? 'Feste Beschriftung' : (FLOW_LABEL_DEFAULTS[key] || 'Anzeigename');
    }
}

function restoreEnergyFlowSnapshot(container, snapshot) {
    if (!container || !snapshot) return;
    Object.entries(snapshot.nodes || {}).forEach(([key, pos]) => {
        setFlowNodePosition(container, key, pos.x, pos.y);
        const node = flowNodeByKey(container, key);
        if (node) node.dataset.flowPositionCustom = pos.custom === true ? '1' : '0';
    });
    window.UI_ENERGY_FLOW = window.UI_ENERGY_FLOW || {};
    window.UI_ENERGY_FLOW.colors = {...FLOW_COLOR_DEFAULTS, ...(snapshot.colors || {})};
    window.UI_ENERGY_FLOW.labels = {...(snapshot.labels || {})};
    window.UI_ENERGY_FLOW.revisions = {...(snapshot.revisions || {})};
    window.UI_ENERGY_FLOW.revision = String(snapshot.revision || '');
    applyEnergyFlowBaseColors(container);
    applyEnergyFlowLabels(container);
    updateEnergyFlowLines(container);
}

function autoDistributeEnergyFlow(container) {
    setFlowNodePosition(container, 'center', 50, 50);
    const nodes = [...container.querySelectorAll('[data-flow-node]')]
        .filter(node => !(node.hidden && node.dataset.flowOptional === '1'))
        .map(node => node.dataset.flowNode)
        .filter(key => key && key !== 'center');
    if (nodes.length === 0) return;
    const layout = container.dataset.flowLayout || 'desktop';
    const rx = layout === 'mobile' ? 34 : 33;
    const ry = layout === 'mobile' ? 34 : 32;
    const preferred = ['generation', 'pv', 'external_pv', 'battery', 'consumption', 'home', 'heatpump', 'wallbox2', 'wallbox', 'heater', 'climate', 'grid'];
    const ordered = preferred.filter(key => nodes.includes(key)).concat(nodes.filter(key => !preferred.includes(key)));
    const hasGenerationAggregate = nodes.includes('generation');
    const hasConsumptionAggregate = nodes.includes('consumption');
    if (hasGenerationAggregate || hasConsumptionAggregate) {
        const mobile = layout === 'mobile';
        if (nodes.includes('battery')) setFlowNodePosition(container, 'battery', 50, mobile ? 14 : 16);
        if (nodes.includes('grid')) setFlowNodePosition(container, 'grid', 50, mobile ? 86 : 84);
        if (hasGenerationAggregate) {
            setFlowNodePosition(container, 'generation', mobile ? 30 : 35, 50);
            if (nodes.includes('pv')) setFlowNodePosition(container, 'pv', mobile ? 10 : 15, 32);
            if (nodes.includes('external_pv')) setFlowNodePosition(container, 'external_pv', mobile ? 10 : 15, 68);
        }
        if (hasConsumptionAggregate) {
            setFlowNodePosition(container, 'consumption', mobile ? 70 : 65, 50);
            const consumers = ['home', 'heatpump', 'wallbox', 'wallbox2', 'heater', 'climate'].filter(key => nodes.includes(key));
            consumers.forEach((key, idx) => {
                const start = mobile ? 10 : 12;
                const range = mobile ? 80 : 76;
                const y = consumers.length === 1 ? 50 : start + idx * (range / Math.max(1, consumers.length - 1));
                setFlowNodePosition(container, key, mobile ? 90 : 85, y);
            });
        }
        updateEnergyFlowLines(container);
        container.querySelectorAll('[data-flow-node]').forEach(node => {
            if (!node.hidden) node.dataset.flowPositionCustom = '1';
        });
        return;
    }
    ordered.forEach((key, idx) => {
        const angle = (-90 + idx * (360 / ordered.length)) * Math.PI / 180;
        setFlowNodePosition(container, key, 50 + Math.cos(angle) * rx, 50 + Math.sin(angle) * ry, {custom: true});
    });
}

function energyFlowColorPatch(current, original) {
    const patch = {};
    Object.keys(FLOW_COLOR_DEFAULTS).forEach(key => {
        const fallback = FLOW_COLOR_DEFAULTS[key];
        const next = normalizeFlowColor(current && current[key], fallback);
        const before = normalizeFlowColor(original && original[key], fallback);
        if (next !== before) patch[key] = next;
    });
    return patch;
}

function energyFlowLabelPatch(current, original) {
    const patch = {};
    Object.keys(FLOW_LABEL_DEFAULTS).forEach(key => {
        if (key === 'center') return;
        const next = normalizeFlowLabel(current && current[key]);
        const before = normalizeFlowLabel(original && original[key]);
        if (next !== before) patch[key] = next;
    });
    return patch;
}

function energyFlowSaveResultMatches(result, payload) {
    const state = result && result.ui_energy_flow;
    const savedNodes = state && state[payload.layout] && state[payload.layout].nodes;
    if (!savedNodes || !state.revisions || !String(state.revision || '')) return false;
    const nodeMatch = Object.entries(payload.nodes || {}).every(([key, expected]) => {
        const actual = savedNodes[key];
        return actual
            && Math.abs(Number(actual.x) - Number(expected.x)) <= 0.011
            && Math.abs(Number(actual.y) - Number(expected.y)) <= 0.011;
    });
    if (!nodeMatch) return false;
    const colors = state.colors || {};
    const colorsMatch = Object.entries(payload.colors_patch || {}).every(([key, expected]) => (
        normalizeFlowColor(colors[key], FLOW_COLOR_DEFAULTS[key]) === normalizeFlowColor(expected, FLOW_COLOR_DEFAULTS[key])
    ));
    if (!colorsMatch) return false;
    const labels = state.labels || {};
    return Object.entries(payload.labels_patch || {}).every(([key, expected]) => (
        normalizeFlowLabel(labels[key]) === normalizeFlowLabel(expected)
    ));
}

function setEnergyFlowSaveStatus(container, text, state = '') {
    const status = container && container.querySelector('[data-flow-save-status]');
    if (!status) return;
    status.textContent = text || '';
    status.classList.toggle('is-success', state === 'success');
    status.classList.toggle('is-error', state === 'error');
}

function setEnergyFlowEditorBusy(container, busy) {
    if (!container) return;
    const controls = container.querySelectorAll(
        '[data-flow-edit], [data-flow-save], [data-flow-cancel], [data-flow-auto], ' +
        '[data-flow-color-select], [data-flow-color-input], [data-flow-label-input], .flow-drag-handle'
    );
    if (busy) {
        clearEnergyFlowDrag(null, {force: true});
        container.dataset.flowSaving = '1';
        container.classList.add('flow-saving');
        controls.forEach(control => {
            control.dataset.flowBusyWasDisabled = control.disabled ? '1' : '0';
            control.disabled = true;
        });
        return;
    }
    controls.forEach(control => {
        control.disabled = control.dataset.flowBusyWasDisabled === '1';
        delete control.dataset.flowBusyWasDisabled;
    });
    delete container.dataset.flowSaving;
    container.classList.remove('flow-saving');
}

function applyEnergyFlowSavedState(container, state, layout) {
    if (!container || !state) return;
    window.UI_ENERGY_FLOW = state;
    const savedNodes = state[layout] && state[layout].nodes;
    Object.entries(savedNodes || {}).forEach(([key, position]) => {
        setFlowNodePosition(container, key, Number(position.x), Number(position.y));
        const node = flowNodeByKey(container, key);
        if (node) node.dataset.flowPositionCustom = '1';
    });
    applyEnergyFlowBaseColors(container);
    applyEnergyFlowLabels(container);
    updateEnergyFlowLines(container);
}

async function saveEnergyFlowLayout(container, original = null) {
    const baseline = original || snapshotEnergyFlow(container);
    const payload = {
        schema_version: 'energy_flow_layout_patch_v2',
        action: 'save_energy_flow_layout',
        layout: container.dataset.flowLayout || 'desktop',
        nodes: collectEnergyFlowNodes(container),
        colors_patch: energyFlowColorPatch(getEnergyFlowColors(), baseline.colors || {}),
        labels_patch: energyFlowLabelPatch(getEnergyFlowLabels(), baseline.labels || {}),
        base_revisions: {...(baseline.revisions || {})}
    };
    const response = await fetch(window.location.pathname, {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/json',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
        },
        body: JSON.stringify(payload)
    });
    const responseText = await response.text();
    let result = null;
    try { result = JSON.parse(responseText); } catch (e) {}
    if (!response.ok || !result || !result.success) {
        const error = new Error((result && result.error) || 'Layout konnte nicht gespeichert werden.');
        error.status = response.status;
        throw error;
    }
    if (!energyFlowSaveResultMatches(result, payload)) throw new Error('Gespeichertes Layout konnte nicht bestätigt werden.');
    applyEnergyFlowSavedState(container, result.ui_energy_flow, payload.layout);
    return result;
}

function initEnergyFlowLayoutEditor() {
    document.querySelectorAll('.flow-container[data-flow-layout]').forEach(container => {
        if (container.dataset.flowEditorReady === '1') return;
        container.dataset.flowEditorReady = '1';
        applyEnergyFlowBaseColors(container);
        applyEnergyFlowLabels(container);
        initEnergyFlowHoverPanel(container);
        requestAnimationFrame(() => updateEnergyFlowLines(container));

        const editBtn = container.querySelector('[data-flow-edit]');
        const saveBtn = container.querySelector('[data-flow-save]');
        const cancelBtn = container.querySelector('[data-flow-cancel]');
        const autoBtn = container.querySelector('[data-flow-auto]');
        const colorSelect = container.querySelector('[data-flow-color-select]');
        const colorInput = container.querySelector('[data-flow-color-input]');
        const labelInput = container.querySelector('[data-flow-label-input]');

        const beginEdit = () => {
            setEnergyFlowSaveStatus(container, '');
            flowEditorState = {container, original: snapshotEnergyFlow(container), selected: 'pv', dragging: null, pendingDrag: null};
            container.classList.add('flow-editing');
            setFlowEditorSelected(container, 'pv');
        };
        const endEdit = () => {
            clearEnergyFlowDrag(null, {force: true});
            container.classList.remove('flow-editing');
            container.querySelectorAll('.flow-node.flow-selected').forEach(n => n.classList.remove('flow-selected'));
            flowEditorState = null;
        };

        if (editBtn) editBtn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (container.dataset.flowSaving === '1') return;
            if (container.classList.contains('flow-editing')) endEdit();
            else beginEdit();
        });
        if (cancelBtn) cancelBtn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (container.dataset.flowSaving === '1') return;
            restoreEnergyFlowSnapshot(container, flowEditorState && flowEditorState.original);
            endEdit();
        });
        if (autoBtn) autoBtn.addEventListener('click', (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (container.dataset.flowSaving === '1') return;
            autoDistributeEnergyFlow(container);
        });
        if (saveBtn) saveBtn.addEventListener('click', async (event) => {
            event.preventDefault();
            event.stopPropagation();
            if (container.dataset.flowSaving === '1') return;
            const oldHtml = saveBtn.innerHTML;
            setEnergyFlowEditorBusy(container, true);
            saveBtn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
            setEnergyFlowSaveStatus(container, 'Speichern…');
            try {
                await saveEnergyFlowLayout(container, flowEditorState && flowEditorState.original);
                endEdit();
                setEnergyFlowSaveStatus(container, 'Gespeichert', 'success');
            } catch (err) {
                console.error('Energy-Flow Layout konnte nicht gespeichert werden', err);
                setEnergyFlowSaveStatus(container, 'Nicht gespeichert', 'error');
                alert(err && err.message ? err.message : 'Layout konnte nicht gespeichert werden.');
            } finally {
                saveBtn.innerHTML = oldHtml;
                setEnergyFlowEditorBusy(container, false);
            }
        });
        if (colorSelect) colorSelect.addEventListener('change', () => setFlowEditorSelected(container, colorSelect.value));
        if (colorInput) colorInput.addEventListener('input', () => {
            const key = colorSelect ? colorSelect.value : (flowEditorState && flowEditorState.selected) || 'pv';
            window.UI_ENERGY_FLOW = window.UI_ENERGY_FLOW || {};
            window.UI_ENERGY_FLOW.colors = {...getEnergyFlowColors(), [key]: normalizeFlowColor(colorInput.value)};
            applyEnergyFlowBaseColors(container);
            setFlowEditorSelected(container, key);
        });
        if (labelInput) labelInput.addEventListener('input', () => {
            const key = (flowEditorState && flowEditorState.selected) || 'pv';
            if (key === 'center') return;
            const alias = normalizeFlowLabel(labelInput.value);
            window.UI_ENERGY_FLOW = window.UI_ENERGY_FLOW || {};
            window.UI_ENERGY_FLOW.labels = {...getEnergyFlowLabels()};
            if (alias) window.UI_ENERGY_FLOW.labels[key] = alias;
            else delete window.UI_ENERGY_FLOW.labels[key];
            applyEnergyFlowLabels(container);
        });

        container.querySelectorAll('[data-flow-node]').forEach(node => {
            const key = node.dataset.flowNode;
            node.addEventListener('click', (event) => {
                if (!container.classList.contains('flow-editing') || container.dataset.flowSaving === '1') return;
                event.preventDefault();
                event.stopImmediatePropagation();
                event.stopPropagation();
                setFlowEditorSelected(container, key);
            });
            let handle = node.querySelector('.flow-drag-handle');
            if (!handle) {
                handle = document.createElement('button');
                handle.type = 'button';
                handle.className = 'flow-drag-handle';
                handle.setAttribute('aria-label', `${getFlowLabel(key)} verschieben`);
                handle.title = 'Knoten verschieben';
                handle.innerHTML = '<i class="fas fa-arrows-up-down-left-right" aria-hidden="true"></i>';
                node.appendChild(handle);
            }
            handle.addEventListener('click', (event) => {
                if (!container.classList.contains('flow-editing')) return;
                event.preventDefault();
                event.stopImmediatePropagation();
                event.stopPropagation();
            });
            const clearDrag = (event = null) => clearEnergyFlowDrag(event);
            handle.addEventListener('pointerdown', (event) => {
                if (!container.classList.contains('flow-editing') || !flowEditorState || container.dataset.flowSaving === '1') return;
                if (event.isPrimary === false || (event.pointerType === 'mouse' && event.button !== 0)) return;
                event.stopPropagation();
                clearEnergyFlowDrag(null, {force: true});
                setFlowEditorSelected(container, key);
                flowEditorState.pendingDrag = {
                    key,
                    pointerId: event.pointerId,
                    x: event.clientX,
                    y: event.clientY,
                    handle,
                    node
                };
                try { handle.setPointerCapture(event.pointerId); } catch (e) {}
            });
            handle.addEventListener('pointermove', (event) => {
                if (!flowEditorState) return;
                const pending = flowEditorState.pendingDrag;
                if (!flowEditorState.dragging && pending && pending.key === key && pending.pointerId === event.pointerId) {
                    if (Math.hypot(event.clientX - pending.x, event.clientY - pending.y) < 7) return;
                    flowEditorState.dragging = {key, pointerId: event.pointerId, handle, node};
                    flowEditorState.pendingDrag = null;
                    node.classList.add('flow-dragging');
                }
                if (!flowEditorState.dragging
                    || flowEditorState.dragging.key !== key
                    || flowEditorState.dragging.pointerId !== event.pointerId) return;
                event.preventDefault();
                event.stopPropagation();
                const rect = flowCanvas(container).getBoundingClientRect();
                const x = ((event.clientX - rect.left) / rect.width) * 100;
                const y = ((event.clientY - rect.top) / rect.height) * 100;
                setFlowNodePosition(container, key, x, y, {custom: true});
            });
            handle.addEventListener('pointerup', clearDrag);
            handle.addEventListener('pointercancel', clearDrag);
            handle.addEventListener('lostpointercapture', clearDrag);
        });
    });
}

window.addEventListener('resize', () => {
    document.querySelectorAll('.flow-container[data-flow-layout]').forEach(container => updateEnergyFlowLines(container));
});
window.addEventListener('orientationchange', () => {
    clearEnergyFlowDrag(null, {force: true});
});
window.addEventListener('blur', () => clearEnergyFlowDrag(null, {force: true}));
document.addEventListener('visibilitychange', () => {
    if (document.hidden) clearEnergyFlowDrag(null, {force: true});
});

function wallboxDisplayLooksActive(data, key) {
    if (!data) return false;
    const suffix = key === 'wb2' ? '2' : '';
    const power = Math.abs(parseFloat(data[key] || 0));
    const phasePower = Math.abs(parseFloat(data[`${key}_p1`] || 0)) +
        Math.abs(parseFloat(data[`${key}_p2`] || 0)) +
        Math.abs(parseFloat(data[`${key}_p3`] || 0));
    const status = key === 'wb' ? String(data.wb_status || '').toLowerCase() : '';
    const chargingLike =
        data[`wb${suffix}_charging`] === true ||
        (key === 'wb' && data.charging_active === true) ||
        status.includes('lad') ||
        status.includes('läd');

    return phasePower > 50 || (power > 50 && chargingLike) || chargingLike;
}

function smoothWallboxDisplayValue(data, key) {
    if (!data || data[key] === undefined) return;

    const now = Date.now();
    const suffix = key === 'wb2' ? '2' : '';
    const kvaKey = `${key}_kva`;
    const pfKey = `${key}_power_factor`;
    const phaseKeys = [`${key}_p1`, `${key}_p2`, `${key}_p3`];
    const phaseSum = phaseKeys.reduce((sum, phaseKey) => sum + Math.abs(parseFloat(data[phaseKey] || 0)), 0);
    let currentPower = Math.abs(parseFloat(data[key] || 0));
    const active = wallboxDisplayLooksActive(data, key);
    const prev = wallboxDisplayCache[key];

    if (!active && currentPower > 50) {
        data[key] = 0;
        currentPower = 0;
        delete wallboxDisplayCache[key];
    }

    if (phaseSum > 50 && currentPower <= 50) {
        data[key] = phaseSum;
        currentPower = phaseSum;
        data[`wb${suffix}_display_held`] = true;
    }

    if (key === 'wb') {
        const ampLimit = Math.max(parseFloat(data.set_amp || 0), parseFloat(data.cap_amp || 0));
        const phases = Math.max(1, parseInt(data.wb_phases || data.detected_phases || 0, 10) || 1);
        const expectedPower = ampLimit * 230 * phases;
        const implausibleMax = currentPower > 1000 &&
            ((expectedPower > 0 && currentPower > expectedPower * 1.45) || (expectedPower <= 0 && currentPower > 18000));
        if (implausibleMax && prev && now - prev.ts < WALLBOX_DISPLAY_HOLD_MS) {
            data[key] = prev.power;
            currentPower = prev.power;
            data[`wb${suffix}_display_held`] = true;
        }
    }

    if (active && currentPower > 50) {
        wallboxDisplayCache[key] = {
            power: currentPower,
            kva: parseFloat(data[kvaKey] || 0),
            powerFactor: parseFloat(data[pfKey] || 0),
            ts: now,
            locked: data[`${key}_locked`] === true,
            plug: data[`${key}_plug`] === true
        };
        return;
    }

    if (active && prev && now - prev.ts < WALLBOX_DISPLAY_HOLD_MS) {
        data[key] = prev.power;
        data[`wb${suffix}_display_held`] = true;
        if (prev.kva > 0 && !(parseFloat(data[kvaKey] || 0) > 0)) data[kvaKey] = prev.kva;
        if (prev.powerFactor > 0 && !(parseFloat(data[pfKey] || 0) > 0)) data[pfKey] = prev.powerFactor;
        if (prev.locked || prev.plug) {
            data[`${key}_locked`] = true;
            data[`${key}_plug`] = true;
        }
    }
}

function smoothWallboxDisplayValues(data) {
    smoothWallboxDisplayValue(data, 'wb');
    smoothWallboxDisplayValue(data, 'wb2');
}

/**
 * Helper function for dynamic battery colors and icons
 */
function getBatStatus(batVal, soc, notstromReserve) {
    const redThreshold = (notstromReserve || 0) + 10;
    const isLowSoc = soc <= redThreshold;

    let iconShape = 'fa-battery-full';
    if (soc <= redThreshold) iconShape = 'fa-battery-quarter';
    else if (soc < 50) iconShape = 'fa-battery-half';
    else if (soc < 80) iconShape = 'fa-battery-three-quarters';

    if (batVal > 0) return { hex: getFlowColor('battery_charge', '#2ecc71'), txt: 'text-success', bg: 'bg-success', fill: flowColorAlpha('battery_charge', 0.3, '#2ecc71'), icon: iconShape };
    if (batVal < 0) {
        if (isLowSoc) return { hex: '#dc3545', txt: 'text-danger', bg: 'bg-danger', fill: 'rgba(220, 53, 69, 0.2)', icon: iconShape };
        else return { hex: getFlowColor('battery', '#198754'), txt: 'text-warning', bg: 'bg-warning', fill: flowColorAlpha('battery', 0.2, '#198754'), icon: iconShape };
    }
    if (isLowSoc) return { hex: '#dc3545', txt: 'text-danger', bg: 'bg-danger', fill: 'rgba(220, 53, 69, 0.2)', icon: iconShape };
    else return { hex: getFlowColor('grid', '#6c757d'), txt: 'text-muted', bg: 'bg-secondary', fill: flowColorAlpha('grid', 0.2, '#6c757d'), icon: iconShape };
}

/**
 * E3DC SoC-Glättung (Physical Constraint Filter)
 * Verhindert unschöne Sprünge im Diagramm, wenn das E3DC-BMS den SoC während
 * der Be- oder Entladung plötzlich rekalibriert (oft bei LFP-Zellen).
 */
function applyPhysicalSocFilter(soc, bat) {
    if (!soc || !bat || soc.length !== bat.length || soc.length === 0) return soc;
    let filtered = new Array(soc.length);
    filtered[0] = soc[0];

    for (let i = 1; i < soc.length; i++) {
        let curr = soc[i];
        let prev = filtered[i - 1];
        let bw = bat[i] || 0;

        if (curr === null || curr === undefined || prev === null || prev === undefined) {
            filtered[i] = curr;
            continue;
        }

        // Bei extrem großen Sprüngen (>5%) sofort den neuen Wert akzeptieren (System-Neustart)
        if (Math.abs(curr - prev) > 5.0) {
            filtered[i] = curr;
            continue;
        }

        if (bw < -50) {
            // Entladen: SoC darf physikalisch nicht steigen. Zieht weich (+0.05%) nach.
            if (curr > prev) filtered[i] = Math.min(curr, prev + 0.05);
            else filtered[i] = curr;
        } else if (bw > 50) {
            // Laden: SoC darf physikalisch nicht fallen. Sinkt weich (-0.05%) ab.
            if (curr < prev) filtered[i] = Math.max(curr, prev - 0.05);
            else filtered[i] = curr;
        } else {
            // Ruhemodus (-50W bis +50W): Rausch-Sprünge sanft abfedern
            if (Math.abs(curr - prev) > 0.3) {
                if (curr > prev) filtered[i] = Math.min(curr, prev + 0.05);
                else filtered[i] = Math.max(curr, prev - 0.05);
            } else filtered[i] = prev;
        }
    }
    return filtered.map(v => v !== null && v !== undefined ? Number(v.toFixed(2)) : v);
}

function applyForecastSocSmoothing(soc, startIndex = 0) {
    if (!soc || soc.length < 5) return soc;

    const values = soc.map(v => (v === null || v === undefined || Number.isNaN(Number(v))) ? null : Number(v));
    const smoothed = [...values];
    const weights = [1, 2, 3, 2, 1];
    const start = Math.max(0, Math.min(values.length - 1, startIndex));

    for (let i = start; i < values.length; i++) {
        if (values[i] === null) {
            smoothed[i] = values[i];
            continue;
        }

        let total = 0;
        let weightSum = 0;
        for (let k = -2; k <= 2; k++) {
            const idx = i + k;
            if (idx < start || idx < 0 || idx >= values.length || values[idx] === null) continue;
            const w = weights[k + 2];
            total += values[idx] * w;
            weightSum += w;
        }
        smoothed[i] = weightSum > 0 ? Math.max(0, Math.min(100, total / weightSum)) : values[i];
    }

    if (values[start] !== null) smoothed[start] = values[start];
    if (values[values.length - 1] !== null) smoothed[values.length - 1] = values[values.length - 1];
    return smoothed.map(v => v !== null && v !== undefined ? Number(v.toFixed(2)) : v);
}

function vehicleSocAgeText(ageSeconds) {
    const age = Number(ageSeconds);
    if (!Number.isFinite(age) || age < 0) return '';
    if (age < 90) return 'vor weniger als 1 Min.';
    if (age < 3600) return `vor ${Math.floor(age / 60)} Min.`;
    if (age < 48 * 3600) {
        const hours = Math.floor(age / 3600);
        const minutes = Math.floor((age % 3600) / 60);
        return `vor ${hours} Std.${minutes > 0 ? ` ${minutes} Min.` : ''}`;
    }
    return `vor ${Math.floor(age / 86400)} Tagen`;
}

function bluelinkRefreshDisplayInfo(data = {}, vehicle = null) {
    const refresh = data && data.bluelink_refresh && typeof data.bluelink_refresh === 'object'
        ? data.bluelink_refresh
        : null;
    if (!refresh || refresh.schema !== 'bluelink_refresh_status_v1') {
        return {available: false, warning: false, detail: '', shortText: ''};
    }
    const vehicleMeta = vehicle && vehicle.soc_meta && typeof vehicle.soc_meta === 'object'
        ? vehicle.soc_meta
        : null;
    const sourceAge = vehicleMeta && Object.prototype.hasOwnProperty.call(vehicleMeta, 'age_s')
        ? vehicleMeta.age_s
        : refresh.source_age_s;
    const sourceAgeText = vehicleSocAgeText(sourceAge);
    const lastError = refresh.last_error_active === true
        && refresh.last_error && typeof refresh.last_error === 'object'
        ? refresh.last_error
        : null;
    const errorLabels = {
        timeout: 'Zeitüberschreitung',
        rate_limited: 'Cloud-Limit erreicht',
        authentication_failed: 'Anmeldung abgewiesen',
        vehicle_data_missing: 'Fahrzeugdaten fehlen',
        api_error: 'Cloud-Fehler'
    };
    if (lastError) {
        const modeLabel = lastError.mode === 'force' ? 'Fahrzeug-Aufwecken' : 'Cloud-Abruf';
        const errorLabel = errorLabels[lastError.code] || 'fehlgeschlagen';
        return {
            available: true,
            warning: true,
            detail: `${modeLabel}: ${errorLabel}${sourceAgeText ? `; Fahrzeugstand ${sourceAgeText}` : ''}`,
            shortText: errorLabel
        };
    }
    if (refresh.status === 'failed') {
        const errorLabel = errorLabels[refresh.error_code] || 'Cloud-Abruf fehlgeschlagen';
        return {
            available: true,
            warning: true,
            detail: `${errorLabel}${sourceAgeText ? `; Fahrzeugstand ${sourceAgeText}` : ''}`,
            shortText: errorLabel
        };
    }
    if (refresh.status === 'success_source_unchanged') {
        return {
            available: true,
            warning: false,
            detail: `Cloud-Abruf erfolgreich, Fahrzeugstand unverändert${sourceAgeText ? ` (${sourceAgeText})` : ''}`,
            shortText: 'Fahrzeugstand unverändert'
        };
    }
    if (refresh.status === 'success_source_partial') {
        const missing = Number(refresh.response_missing_source_count || 0);
        const missingText = missing > 0
            ? `; bei ${missing} Fahrzeug${missing === 1 ? '' : 'en'} fehlt der Rohzeitpunkt`
            : '; mindestens ein Fahrzeugzeitpunkt fehlt';
        return {
            available: true,
            warning: true,
            detail: `Cloud-Abruf nur teilweise belastbar${missingText}${sourceAgeText ? `; jüngster belegter Stand ${sourceAgeText}` : ''}`,
            shortText: 'Fahrzeugzeitpunkt teilweise'
        };
    }
    if (refresh.status === 'success_source_unknown') {
        return {
            available: true,
            warning: true,
            detail: 'Cloud-Abruf ohne belastbaren Fahrzeugzeitpunkt; der SoC bleibt nur als veralteter Anzeigewert erhalten',
            shortText: 'Fahrzeugzeitpunkt fehlt'
        };
    }
    return {
        available: true,
        warning: false,
        detail: sourceAgeText ? `Fahrzeugstand ${sourceAgeText}` : 'Cloud-Abruf erfolgreich',
        shortText: ''
    };
}

function vehicleSocDisplayInfo(vehicle, decimals = 1) {
    const meta = vehicle && vehicle.soc_meta && typeof vehicle.soc_meta === 'object'
        ? vehicle.soc_meta
        : {};
    const rawValue = meta.value !== undefined && meta.value !== null ? meta.value : (vehicle ? vehicle.soc : null);
    const value = parseFloat(rawValue);
    const displayUsable = meta.display_usable !== undefined
        ? meta.display_usable === true
        : Number.isFinite(value) && value >= 0 && value <= 100;
    if (!displayUsable || !Number.isFinite(value) || value < 0 || value > 100) {
        return {known: false, valueText: '--', qualifier: '', standText: '', fullText: '--', stale: true};
    }

    const sourceClass = String(meta.class || vehicle.soc_source_class || '').toLowerCase();
    const transportClass = String(meta.transport_class || '').toLowerCase();
    const stale = meta.stale === true || vehicle.soc_stale === true || transportClass === 'cached';
    const metaHasSourceTs = Object.prototype.hasOwnProperty.call(meta, 'source_ts');
    const vehicleHasSourceTs = !!vehicle && Object.prototype.hasOwnProperty.call(vehicle, 'soc_source_ts');
    const sourceTsRaw = metaHasSourceTs
        ? meta.source_ts
        : (vehicleHasSourceTs ? vehicle.soc_source_ts : null);
    const sourceTsParsed = Number(sourceTsRaw);
    const sourceTs = Number.isFinite(sourceTsParsed) && sourceTsParsed > 0
        ? Math.trunc(sourceTsParsed)
        : 0;
    const sourceAge = meta.age_s !== undefined && meta.age_s !== null
        ? Number(meta.age_s)
        : (sourceTs > 0 ? Math.max(0, Date.now() / 1000 - sourceTs) : null);
    const qualifier = sourceClass === 'estimated'
        ? 'geschätzt'
        : (sourceClass === 'manual' ? 'manuell' : (sourceClass === 'cloud' ? 'Cloud' : 'gemessen'));
    const digits = Math.max(0, Math.min(2, parseInt(decimals, 10) || 0));
    const valueText = value.toLocaleString('de-DE', {minimumFractionDigits: digits, maximumFractionDigits: digits}) + ' %';
    let standText = '';
    if (sourceTs > 0) {
        const d = new Date(sourceTs * 1000);
        const pad = n => String(n).padStart(2, '0');
        standText = `Stand ${pad(d.getDate())}.${pad(d.getMonth() + 1)}. ${pad(d.getHours())}:${pad(d.getMinutes())}`;
    }
    const ageText = vehicleSocAgeText(sourceAge);
    const fullText = `${valueText} ${qualifier}${standText ? `, ${standText}` : ''}${ageText ? ` (${ageText})` : ''}`;
    return {known: true, value, valueText, qualifier, standText, ageText, fullText, stale, sourceClass, transportClass, sourceTs};
}

function vehicleSourceSyncDisplayInfo(socInfo, cloudRefresh = null) {
    const info = socInfo && typeof socInfo === 'object' ? socInfo : {};
    const refresh = cloudRefresh && typeof cloudRefresh === 'object' ? cloudRefresh : null;
    if (Number.isFinite(Number(info.sourceTs)) && Number(info.sourceTs) > 0) {
        const sourceDate = new Date(Number(info.sourceTs) * 1000);
        return {
            text: sourceDate.toLocaleString('de-DE') + (info.ageText ? ` (${info.ageText})` : ''),
            title: refresh && refresh.detail ? refresh.detail : ''
        };
    }
    return {
        text: refresh && refresh.available ? 'Kein neuer Fahrzeugzeitpunkt' : '--',
        title: refresh && refresh.detail ? refresh.detail : 'Fahrzeug-Quellzeitpunkt nicht verfügbar'
    };
}

function vehicleSocKnown(vehicle) {
    return vehicleSocDisplayInfo(vehicle, 1).known;
}

function wallboxPrimaryVehicleActive(data, id, power, locked) {
    const valWb = Math.abs(parseFloat(power) || 0);
    return locked === true
        || valWb > 50
        || (id === 'wb' ? data?.wb_plug === true : data?.wb2_plug === true);
}

function renderWallboxPrimaryVehicleName(titleElement, carName, active) {
    if (!titleElement) return '';
    if (titleElement._e3dcBaseHtml === undefined) {
        titleElement._e3dcBaseHtml = titleElement.innerHTML;
        titleElement._e3dcBaseTitle = titleElement.getAttribute('title') || String(titleElement.textContent || '').trim();
    }
    const displayName = active ? String(carName || '').trim() : '';
    if (displayName) {
        titleElement.textContent = '';
        const icon = document.createElement('i');
        icon.className = 'fas fa-car-side me-1';
        titleElement.appendChild(icon);
        titleElement.appendChild(document.createTextNode(displayName));
        titleElement.setAttribute('title', displayName);
    } else {
        titleElement.innerHTML = titleElement._e3dcBaseHtml;
        titleElement.setAttribute('title', titleElement._e3dcBaseTitle);
    }
    return String(titleElement.textContent || '').trim();
}

function updateVehiclePage(data) {
    const tabsEl = document.getElementById('vehicleTabs');
    const contentEl = document.getElementById('vehicleTabsContent');
    if (!tabsEl || !contentEl) return;

    // Globales Flag für forceSocUpdate()-Schutz setzen
    window._hasBluelink = !!data.has_bluelink;

    // "Fahrzeug aufwecken" Button nur anzeigen wenn Bluelink konfiguriert ist
    const fzBtn = document.getElementById('fz-update-btn');
    if (fzBtn) {
        if (data.has_bluelink) {
            fzBtn.style.display = '';
            if (data.car_force_running) {
                fzBtn.innerHTML = '<i class="fas fa-sync-alt fa-spin me-1"></i> Wecke auf...';
                fzBtn.disabled = true;
            } else {
                fzBtn.innerHTML = '<i class="fas fa-sync-alt me-1"></i> Fahrzeug aufwecken';
                fzBtn.disabled = false;
            }
        } else {
            fzBtn.style.display = 'none';
        }
    }

    const vehicles = data.vehicles || [];
    // Prüfe, ob die Struktur der Tabs neu gezeichnet werden muss (Anzahl, Namen oder Status geändert)
    let needsRedraw = (tabsEl.children.length !== vehicles.length);
    if (!needsRedraw) {
        vehicles.forEach((v, idx) => {
            const currentTabTitle = $(tabsEl.children[idx]).find('.nav-link').text().trim();
            if (currentTabTitle !== v.name) needsRedraw = true;

            // Auch wenn sich der Typ (Gast vs. Cloud) ändert, müssen wir die Struktur ggf. anpassen
            const isIntegrated = (v.last_updated_at || v.odometer || v.bat_12v || v.id);
            const isDummy = !!(v.is_manual && !isIntegrated);
            const wasInterpolated = $(contentEl.children[idx]).data('interpolated') === true;
            if (wasInterpolated !== isDummy) needsRedraw = true;
        });
    }

    if (vehicles.length === 0) {
        const errMsg = data.car_error ? data.car_error : 'Warte auf Fahrzeugdaten...';
        const alertEl = document.createElement('div');
        alertEl.className = 'alert alert-warning mt-3';
        const iconEl = document.createElement('i');
        iconEl.className = 'fas fa-info-circle me-2';
        iconEl.setAttribute('aria-hidden', 'true');
        alertEl.appendChild(iconEl);
        alertEl.appendChild(document.createTextNode(errMsg));
        contentEl.textContent = '';
        contentEl.appendChild(alertEl);
        return;
    }

    const loading = document.getElementById('fz-loading');
    if (loading) loading.style.display = 'none';

    if (needsRedraw) {
        tabsEl.innerHTML = '';
        contentEl.innerHTML = '';

        vehicles.forEach((v, idx) => {
            const isActive = idx === 0 ? 'active' : '';
            const isShow = idx === 0 ? 'show active' : '';

            // Ein "Dummy" ist ein Fahrzeug, das NUR aus einer manuellen Session besteht
            // und keine echten Hintergrund-Daten (wie Cloud-Sync, Odo oder 12V) hat.
            const isIntegrated = (v.last_updated_at || v.odometer || v.bat_12v || v.id);
            const isDummy = (v.is_manual && !isIntegrated);

            tabsEl.innerHTML += `
                <li class="nav-item" role="presentation">
                    <button class="nav-link ${isActive} fw-bold" data-bs-toggle="pill" data-bs-target="#pane-${idx}" type="button" role="tab" style="border-radius: 20px; padding: 8px 20px; margin-right: 5px;">
                        <i class="fas fa-car me-2"></i>${v.name}
                    </button>
                </li>
            `;

            contentEl.innerHTML += `
                <div class="tab-pane fade ${isShow}" id="pane-${idx}" role="tabpanel" data-interpolated="${isDummy}">
                    <div class="row g-4 mb-4 text-center">
                        <div class="col-6 col-md-4"><div class="p-3 bg-body-tertiary rounded-4 border border-secondary-subtle h-100"><div class="small text-muted mb-1 text-uppercase fw-bold">Fahrzeug-SoC</div><div class="fs-1 fw-bolder text-success" id="fz-soc-${idx}">--</div><div class="small text-muted mt-1" id="fz-soc-meta-${idx}">Quelle nicht verfügbar</div></div></div>
                        <div class="col-6 col-md-4"><div class="p-3 bg-body-tertiary rounded-4 border border-secondary-subtle h-100"><div class="small text-muted mb-1 text-uppercase fw-bold">Reichweite</div><div class="fs-2 fw-bolder text-info" id="fz-range-${idx}">--</div></div></div>
                        ${isDummy ? '' : `<div class="col-12 col-md-4"><div class="p-3 bg-body-tertiary rounded-4 border border-secondary-subtle h-100"><div class="small text-muted mb-1 text-uppercase fw-bold">12V Batterie</div><div class="fs-2 fw-bolder text-warning" id="fz-12v-${idx}">--</div></div></div>`}
                    </div>
                    <div class="row g-3">
                        <div class="col-md-6"><ul class="list-group list-group-flush rounded-4 border border-secondary-subtle shadow-sm">
                            <li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-plug me-2"></i>Kabel-Status</span><span class="fw-bold" id="fz-plugged-${idx}">--</span></li>
                            <li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-bullseye me-2"></i>Lade-Ziel (Auto)</span><span class="fw-bold text-success" id="fz-target-${idx}">--</span></li>
                            ${isDummy ? '' : `<li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-map-marker-alt me-2"></i>Standort</span><span class="fw-bold" id="fz-home-${idx}">--</span></li>`}
                            ${isDummy ? '' : `<li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-tachometer-alt me-2"></i>Kilometerstand</span><span class="fw-bold" id="fz-odo-${idx}">--</span></li>`}
                        </ul></div>
                        <div class="col-md-6"><ul class="list-group list-group-flush rounded-4 border border-secondary-subtle shadow-sm">
                            <li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-history me-2"></i>Cloud-Sync</span><span class="fw-bold" id="fz-last-update-${idx}">--</span></li>
                            ${isDummy ? '' : `<li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-lock me-2"></i>Türen / Schloss</span><span class="fw-bold" id="fz-locked-${idx}">--</span></li>`}
                            ${isDummy ? '' : `<li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-door-closed me-2"></i>Türen & Klappen</span><span class="fw-bold" id="fz-doors-${idx}">--</span></li>`}
                            ${isDummy ? '' : `<li class="list-group-item bg-transparent d-flex justify-content-between align-items-center py-3"><span class="text-muted"><i class="fas fa-car-burst me-2"></i>Reifendruck</span><span class="fw-bold" id="fz-tires-${idx}">--</span></li>`}
                        </ul></div>
                    </div>
                </div>
            `;
        });
    }

    vehicles.forEach((v, idx) => {
        const socInfo = vehicleSocDisplayInfo(v, 1);
        const cloudRefresh = socInfo.sourceClass === 'cloud'
            ? bluelinkRefreshDisplayInfo(data, v)
            : null;
        const socMetaParts = socInfo.known
            ? [socInfo.qualifier, socInfo.standText, socInfo.ageText]
            : [];
        if (cloudRefresh && cloudRefresh.detail) socMetaParts.push(cloudRefresh.detail);
        const socEl = $(`#fz-soc-${idx}`);
        socEl.text(socInfo.valueText)
            .removeClass('text-success text-warning text-muted')
            .addClass(socInfo.known ? (socInfo.stale || socInfo.sourceClass === 'estimated' ? 'text-warning' : 'text-success') : 'text-muted');
        $(`#fz-soc-meta-${idx}`)
            .text(socInfo.known ? socMetaParts.filter(Boolean).join(' · ') : 'Quelle nicht verfügbar')
            .toggleClass('text-warning', socInfo.known && (socInfo.stale || socInfo.sourceClass === 'estimated' || (cloudRefresh && cloudRefresh.warning)))
            .toggleClass('text-muted', !socInfo.known || (!socInfo.stale && socInfo.sourceClass !== 'estimated' && !(cloudRefresh && cloudRefresh.warning)));
        let rangeText = v.range_km ? v.range_km + ' km' : '--';
        if (socInfo.known && socInfo.stale && v.range_km) rangeText = 'zuletzt ' + rangeText;
        $(`#fz-range-${idx}`).text(rangeText);
        $(`#fz-12v-${idx}`).text(v.bat_12v ? v.bat_12v + '%' : '--');
        $(`#fz-odo-${idx}`).text(v.odometer ? v.odometer.toLocaleString('de-DE') + ' km' : '--');
        $(`#fz-target-${idx}`).text(v.target_soc ? v.target_soc + '%' : '--');

        if (v.is_locked === true) $(`#fz-locked-${idx}`).html('<i class="fas fa-lock text-success"></i> Verriegelt');
        else if (v.is_locked === false) $(`#fz-locked-${idx}`).html('<i class="fas fa-unlock text-danger"></i> Entriegelt');
        else $(`#fz-locked-${idx}`).text('--');

        if (v.is_plugged_in === true || v.is_plugged_in == 1) $(`#fz-plugged-${idx}`).html('<i class="fas fa-plug text-success"></i> Angesteckt');
        else $(`#fz-plugged-${idx}`).html('<i class="fas fa-plug text-secondary"></i> Abgesteckt');

        let locHtml = '--';
        if (v.is_at_home === true) locHtml = '<i class="fas fa-home text-info"></i> Zuhause';
        else if (v.is_at_home === false) locHtml = '<i class="fas fa-road text-warning"></i> Unterwegs';
        if (v.car_lat && v.car_lon && locHtml !== '--') {
            let gmapsUrl = `https://www.google.com/maps/search/?api=1&query=${v.car_lat},${v.car_lon}`;
            $(`#fz-home-${idx}`).html(`<a href="${gmapsUrl}" target="_blank" class="text-decoration-none" style="color: inherit;" title="Auf Google Maps anzeigen">${locHtml} <i class="fas fa-external-link-alt small text-muted ms-1"></i></a>`);
        } else {
            $(`#fz-home-${idx}`).html(locHtml);
        }

        const sourceSync = vehicleSourceSyncDisplayInfo(socInfo, cloudRefresh);
        $(`#fz-last-update-${idx}`)
            .text(sourceSync.text)
            .attr('title', sourceSync.title);

        if (v.doors_open === true) $(`#fz-doors-${idx}`).html('<i class="fas fa-door-open text-warning"></i> Offen');
        else if (v.doors_open === false) $(`#fz-doors-${idx}`).html('<i class="fas fa-door-closed text-success"></i> Zu');
        else $(`#fz-doors-${idx}`).text('--');

        if (v.tire_warning === true) $(`#fz-tires-${idx}`).html('<i class="fas fa-exclamation-triangle text-danger"></i> Warnung');
        else if (v.tire_warning === false) $(`#fz-tires-${idx}`).html('<i class="fas fa-check-circle text-success"></i> OK');
        else $(`#fz-tires-${idx}`).text('--');
    });
}

function updateVehicleWidgets(data) {
    const badgesWb1 = [$('#val-car-soc'), $('#f-val-car-soc')];
    const badgesWb2 = [$('#val-car-soc2'), $('#f-val-car-soc2')];

    if (data.vehicles && data.vehicles.length > 0) {
        const vehicles = Array.isArray(data.vehicles) ? data.vehicles : [];
        const isPlugged = (v) => v && (v.is_plugged_in === true || v.is_plugged_in == 1 || v.is_charging === true);
        const compactId = (value) => String(value || '').toLowerCase().replace(/[^a-z0-9]/g, '');
        const bySlot = (slot) => {
            const candidates = vehicles.filter(v => parseInt(v.wb_slot || 0) === slot && isPlugged(v));
            return candidates.find(v => v.soc_profile_bound === true && vehicleSocKnown(v))
                || candidates.find(v => vehicleSocKnown(v))
                || candidates[0];
        };
        const unslottedPlugged = vehicles.filter(v => isPlugged(v) && !parseInt(v.wb_slot || 0));
        const slotFallback = (slot) => {
            const prefix = slot === 2 ? 'wb2' : 'wb';
            const alias = slot === 2 ? 'wb2' : 'wb1';
            const connected = slot === 2
                ? (data.wb2_locked === true || data.wb2_plug === true || data.wb2_charging === true || (parseFloat(data.wb2) || 0) > 50)
                : (data.wb_plug === true || data.wb_charging === true || (parseFloat(data.wb) || 0) > 50);
            if (!connected) return null;
            const name = data[`${alias}_car_name`] || data[`${prefix}_car_name`] || '';
            const ids = [
                data[`${alias}_car_id`], data[`${prefix}_car_id`],
                data[`${alias}_vehicle_id`], data[`${prefix}_vehicle_id`],
                data[`${alias}_rfid_tag`], data[`${prefix}_rfid_tag`]
            ].map(compactId).filter(Boolean);
            if (!name && ids.length === 0) return null;
            const normalizeName = (value) => String(value || '').trim().toLocaleLowerCase('de-DE').replace(/\s+/g, ' ');
            const nameKey = normalizeName(name);
            const matched = vehicles.find(v => {
                const vehicleIds = [v.id, v.profile_id, v.vehicle_id, v.cloud_vehicle_id, v.rfid_tag].map(compactId);
                const idMatch = ids.length > 0 && vehicleIds.some(id => id && ids.includes(id));
                const vName = normalizeName(v.name);
                return idMatch || (nameKey && vName && nameKey === vName);
            }) || {};
            return Object.assign({}, matched, {
                name: name || matched.name || 'Fahrzeug',
                wb_slot: slot,
                is_plugged_in: true,
                is_charging: slot === 2 ? (data.wb2_charging === true || (parseFloat(data.wb2) || 0) > 50) : (data.wb_charging === true || (parseFloat(data.wb) || 0) > 50)
            });
        };

        let activeV1 = bySlot(1) || null;
        let activeV2 = bySlot(2) || null;

        if (!activeV1 && data.wb_plug === true) activeV1 = slotFallback(1) || unslottedPlugged.shift() || null;
        if (!activeV2 && data.wb2_locked === true) activeV2 = slotFallback(2) || unslottedPlugged.shift() || null;

        const isGuest1 = data.wb_plug === true && !activeV1;
        const isGuest2 = data.wb2_locked === true && !activeV2;

        const escapeBadgeHtml = (value) => String(value ?? '').replace(/[&<>"']/g, ch => ({
            '&': '&amp;',
            '<': '&lt;',
            '>': '&gt;',
            '"': '&quot;',
            "'": '&#39;'
        }[ch]));
        const compactVehicleName = (name) => {
            const raw = String(name || '').trim();
            const key = raw.toLowerCase().replace(/\s+/g, ' ');
            if (!raw) return '';
            if (key === 'gast / manuell' || key === 'gast-fahrzeug' || key === 'gastfahrzeug') return 'Gast';
            return raw;
        };

        const updateBadges = (badges, activeV, isGuest) => {
            badges.forEach(el => {
                if (!el.length) return;

                const activeSocInfo = activeV ? vehicleSocDisplayInfo(activeV, 0) : null;
                const activeIsCloud = !!(activeSocInfo && activeSocInfo.sourceClass === 'cloud');
                const activeBluelinkRefreshInfo = activeIsCloud
                    ? bluelinkRefreshDisplayInfo(data, activeV)
                    : null;
                if (data.car_force_running && activeIsCloud) {
                    if (!el.html().includes('fa-spin')) el.html('<i class="fas fa-sync fa-spin"></i>').show();
                } else if (isGuest && !activeV) {
                    el.html('(Gast)').css('cursor', 'default').attr('title', 'Gast-Fahrzeug lädt').show();
                } else if (activeV) {
                    const dispName = compactVehicleName(activeV.name);
                    const socInfo = vehicleSocDisplayInfo(activeV, 0);
                    const socKnown = socInfo.known;
                    const soctxt = socKnown ? `${socInfo.valueText} ${socInfo.qualifier}` : '--';
                    const badgeParts = [];
                    if (dispName) badgeParts.push(`<strong>${escapeBadgeHtml(dispName)}</strong>`);
                    badgeParts.push(`${escapeBadgeHtml(soctxt)} SoC`);
                    const titleParts = [];
                    if (activeV.name) titleParts.push(String(activeV.name).trim());
                    titleParts.push(socKnown ? socInfo.fullText : 'Fahrzeug-SoC nicht verfügbar');
                    if (socKnown && activeV.range_km) titleParts.push(`${activeV.range_km} km`);
                    if (activeBluelinkRefreshInfo && activeBluelinkRefreshInfo.detail) {
                        titleParts.push(activeBluelinkRefreshInfo.detail);
                    }
                    let txt = badgeParts.join(' | ');
                    const staleSocDetail = socInfo.stale
                        ? `Fahrzeug-SoC veraltet${socInfo.ageText ? ` (${socInfo.ageText})` : '; Quellzeit fehlt'}`
                        : '';
                    const badgeWarningDetail = staleSocDetail
                        || (activeBluelinkRefreshInfo && activeBluelinkRefreshInfo.warning
                            ? activeBluelinkRefreshInfo.detail
                            : '');
                    if (badgeWarningDetail) {
                        titleParts.push(badgeWarningDetail);
                        txt += ` <i class="fas fa-exclamation-triangle text-warning ms-1" title="${escapeBadgeHtml(badgeWarningDetail)}"></i>`;
                    }
                    el.html(txt).css('cursor', 'pointer').attr('title', titleParts.filter(Boolean).join(' | ') + '\nSoC vom Auto abrufen (Aufwecken)').show();
                } else {
                    el.hide();
                }
            });
        };

        updateBadges(badgesWb1, activeV1, isGuest1);
        updateBadges(badgesWb2, activeV2, isGuest2);
    } else {
        badgesWb1.forEach(el => {
            if (el.length) {
                if (data.wb_soc && data.wb_soc > 0) {
                    el.html(`<strong>${data.wb_soc.toFixed(1)}%</strong> SoC`).css('cursor', 'default').attr('title', 'SoC (von Wallbox gespiegelt)').show();
                } else {
                    el.hide();
                }
            }
        });
        badgesWb2.forEach(el => { if (el.length) el.hide(); });
    }

    // IMMER aufrufen, damit die Fahrzeug-Seite auf Fehler/Ladezeiten reagieren kann!
    if (typeof updateVehiclePage === 'function') updateVehiclePage(data);
}

function setMobileFlowView(view) {
    const normalized = view === 'ring' ? 'ring' : 'classic';
    const classic = document.getElementById('m-flow-classic-view');
    const ring = document.getElementById('m-flow-ring-view');
    const btnClassic = document.getElementById('m-flow-tab-classic');
    const btnRing = document.getElementById('m-flow-tab-ring');

    if (!classic || !ring) return;
    classic.style.display = normalized === 'classic' ? '' : 'none';
    ring.style.display = normalized === 'ring' ? '' : 'none';

    if (btnClassic) {
        btnClassic.classList.toggle('active', normalized === 'classic');
        btnClassic.setAttribute('aria-pressed', normalized === 'classic' ? 'true' : 'false');
    }
    if (btnRing) {
        btnRing.classList.toggle('active', normalized === 'ring');
        btnRing.setAttribute('aria-pressed', normalized === 'ring' ? 'true' : 'false');
    }

    try { localStorage.setItem('mobileFlowView', normalized); } catch (e) {}
}

function initMobileFlowView() {
    if (!document.getElementById('m-flow-classic-view')) return;
    let preferred = 'classic';
    try { preferred = localStorage.getItem('mobileFlowView') || 'classic'; } catch (e) {}
    setMobileFlowView(preferred);
}

function storageDispatchRuntimeForDisplay(data = {}) {
    if (!data || typeof data !== 'object') return null;
    const runtime = data.storage_dispatch_runtime;
    const meta = data.storage_plan_meta;
    const curve = Array.isArray(data.storage_sim_curve) ? data.storage_sim_curve : [];
    if (!runtime || typeof runtime !== 'object' || runtime.schema_version !== 'storage_dispatch_runtime_v1') return null;
    if (!meta || typeof meta !== 'object') return null;
    const planId = typeof meta.plan_id === 'string' ? meta.plan_id : '';
    const slotId = typeof runtime.slot_id === 'string' ? runtime.slot_id : '';
    if (!planId || !slotId || runtime.plan_id !== planId) return null;
    const slotMatches = curve.some(point => point && typeof point === 'object'
        && point.plan_id === planId
        && point.slot_id === slotId);
    return slotMatches ? runtime : null;
}

function directMarketingSelectedActionFallbackViewModel(data, planId, planMeta) {
    const limited = reasonCode => ({state: 'evidence_limit', reasonCode, slots: [], series: {pvStoreW: [], economicExportW: [], chargeBlock: []}});
    const contract = data && typeof data === 'object' ? data.direct_marketing_selected_action_fallback : null;
    const trajectory = data && typeof data === 'object' ? data.direct_marketing_trajectory : null;
    if (!contract || typeof contract !== 'object') return limited('DIRECT_MARKETING_ACTION_FALLBACK_MISSING');
    const trajectoryStatusAllowed = ['TRAJECTORY_AXIS_EVIDENCE_LIMIT', 'PASSIVE_POLICY_BINDING_MISSING'].includes(trajectory && trajectory.status);
    const passivePolicyBindingMetaValid = !trajectory || trajectory.status !== 'PASSIVE_POLICY_BINDING_MISSING'
        || (trajectory.meta && typeof trajectory.meta === 'object' && !Array.isArray(trajectory.meta)
            && trajectory.meta.candidate_effect === false
            && trajectory.meta.shadow_effect === false
            && trajectory.meta.runtime_authorization_separate === true);
    if (!trajectory || typeof trajectory !== 'object' || trajectory.schema_version !== 'direct_marketing_trajectory_v1' || trajectory.active !== true || trajectory.complete !== false || !trajectoryStatusAllowed || trajectory.reason_code !== trajectory.status || !passivePolicyBindingMetaValid || trajectory.plan_id !== planId || !Array.isArray(trajectory.slots) || trajectory.slots.length !== 0 || !/^sha256:[0-9a-f]{64}$/.test(String(trajectory.trajectory_revision || ''))) return limited('DIRECT_MARKETING_ACTION_FALLBACK_TRAJECTORY_NOT_AXIS_LIMITED');
    if (contract.schema_version !== 'direct_marketing_selected_action_fallback_v1' || contract.active !== true || contract.complete !== true || contract.plan_id !== planId || contract.trajectory_revision !== trajectory.trajectory_revision || !/^sha256:[0-9a-f]{64}$/.test(String(contract.projection_revision || '')) || !Array.isArray(contract.slots) || contract.slots.length === 0) return limited(contract.reason_code || 'DIRECT_MARKETING_ACTION_FALLBACK_INVALID');
    const canonicalJson = value => {
        if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
        if (value && typeof value === 'object') return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
        return JSON.stringify(value);
    };
    if (!planMeta || typeof planMeta !== 'object' || !planMeta.input_revisions || canonicalJson(contract.input_revisions) !== canonicalJson(planMeta.input_revisions) || canonicalJson(contract.input_revisions) !== canonicalJson(trajectory.input_revisions)) return limited('DIRECT_MARKETING_ACTION_FALLBACK_INPUT_REVISION_MISMATCH');
    const durationMs = Number(contract.slot_duration_s) * 1000;
    const validFromTs = Number(contract.valid_from_ts_ms);
    const horizonEndTs = Number(contract.horizon_end_ts_ms);
    if (!Number.isFinite(durationMs) || durationMs <= 0 || !Number.isFinite(validFromTs) || !Number.isFinite(horizonEndTs) || horizonEndTs <= validFromTs) return limited('DIRECT_MARKETING_ACTION_FALLBACK_HORIZON_INVALID');
    let previousEnd = null;
    const slots = [];
    for (const source of contract.slots) {
        if (!source || typeof source !== 'object') return limited('DIRECT_MARKETING_ACTION_FALLBACK_SLOT_INVALID');
        const start = Number(source.start_ts_ms);
        const end = Number(source.end_ts_ms);
        const roles = source.selected === true && source.executable === true && source.commands_allowed === true;
        const noRoles = source.selected === false && source.executable === false && source.commands_allowed === false;
        if (!/^sha256:[0-9a-f]{64}$/.test(String(source.slot_id || '')) || !Number.isFinite(start) || !Number.isFinite(end) || end - start !== durationMs || (previousEnd === null && start !== validFromTs) || (previousEnd !== null && start !== previousEnd) || (!roles && !noRoles)) return limited('DIRECT_MARKETING_ACTION_FALLBACK_SLOT_BINDING_INVALID');
        let action = null;
        let plannedW = null;
        if (roles) {
            action = typeof source.action === 'string' ? source.action.trim().toUpperCase() : '';
            plannedW = Number(source.planned_w);
            const positivePowerAction = action === 'PV_STORE' || action === 'ECONOMIC_EXPORT' || action === 'DV_CURVE_CHARGE';
            const zeroPowerAction = action === 'CHARGE_BLOCK_WAIT';
            if ((!positivePowerAction && !zeroPowerAction) || !Number.isFinite(plannedW) || (positivePowerAction && plannedW <= 0) || (zeroPowerAction && Math.abs(plannedW) > 0.01) || !/^sha256:[0-9a-f]{64}$/.test(String(source.action_id || '')) || typeof source.window_id !== 'string' || source.window_id.trim() === '' || typeof source.segment_id !== 'string' || source.segment_id.trim() === '' || typeof source.source_action !== 'string' || source.source_action.trim() === '' || typeof source.source_mode !== 'string' || source.source_mode.trim() === '') return limited('DIRECT_MARKETING_ACTION_FALLBACK_ACTION_BINDING_INVALID');
        } else if (source.action !== null || source.planned_w !== null || source.action_id !== null || source.window_id !== null || source.segment_id !== null || source.source_action !== null || source.source_mode !== null) return limited('DIRECT_MARKETING_ACTION_FALLBACK_PASSIVE_ROLE_INVALID');
        slots.push({slotId: source.slot_id, startTs: start, endTs: end, plannedAllowed: roles, action, plannedW});
        previousEnd = end;
    }
    if (previousEnd !== horizonEndTs) return limited('DIRECT_MARKETING_ACTION_FALLBACK_HORIZON_INVALID');
    return {state: 'complete', reasonCode: null, slots, series: {pvStoreW: slots.map(slot => slot.plannedAllowed && (slot.action === 'PV_STORE' || slot.action === 'DV_CURVE_CHARGE') ? slot.plannedW : null), economicExportW: slots.map(slot => slot.plannedAllowed && slot.action === 'ECONOMIC_EXPORT' ? slot.plannedW : null), chargeBlock: slots.map(slot => slot.plannedAllowed && slot.action === 'CHARGE_BLOCK_WAIT' ? 1 : null)}};
}

function directMarketingTrajectoryViewModel(data = {}) {
    const disabled = !data || typeof data !== 'object' || data.direct_marketing_enabled !== true;
    const inactive = {
        active: false,
        state: 'inactive',
        reasonCode: 'DIRECT_MARKETING_DISABLED',
        planId: '',
        meta: null,
        slots: [],
        series: {soc: [], pvStoreW: [], economicExportW: [], headroomProjectionW: [], chargeBlock: []}
    };
    if (disabled) return inactive;

    const contract = data.direct_marketing_trajectory;
    const planMeta = data.storage_plan_meta;
    const planId = planMeta && typeof planMeta === 'object' && typeof planMeta.plan_id === 'string'
        ? planMeta.plan_id
        : '';
    const actionFallback = directMarketingSelectedActionFallbackViewModel(data, planId, planMeta);
    const evidenceLimit = reasonCode => {
        const headroomContractInvalid = String(reasonCode || '').includes('HEADROOM_PROJECTION');
        const actionFallbackAllowed = actionFallback.state === 'complete' && !headroomContractInvalid;
        return ({
        active: true,
        state: actionFallbackAllowed ? 'actions_only' : 'evidence_limit',
        reasonCode: reasonCode || 'DIRECT_MARKETING_TRAJECTORY_INCOMPLETE',
        actionReasonCode: actionFallback.reasonCode,
        planId,
        meta: contract && typeof contract === 'object' && contract.meta && typeof contract.meta === 'object'
            ? contract.meta
            : null,
        slots: actionFallbackAllowed ? actionFallback.slots : [],
        series: {
            soc: [],
            pvStoreW: actionFallbackAllowed ? actionFallback.series.pvStoreW : [],
            economicExportW: actionFallbackAllowed ? actionFallback.series.economicExportW : [],
            headroomProjectionW: [],
            chargeBlock: actionFallbackAllowed ? actionFallback.series.chargeBlock : []
        }
    });
    };
    if (!/^sha256:[0-9a-f]{64}$/.test(planId)) return evidenceLimit('DIRECT_MARKETING_PLAN_ID_INVALID');
    if (!contract || typeof contract !== 'object') return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_MISSING');
    if (contract.schema_version !== 'direct_marketing_trajectory_v1') return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_SCHEMA_INVALID');
    if (contract.active !== true) return evidenceLimit(contract.reason_code || 'DIRECT_MARKETING_TRAJECTORY_NOT_ACTIVE');
    if (contract.complete !== true) return evidenceLimit(contract.reason_code || contract.status || 'DIRECT_MARKETING_TRAJECTORY_INCOMPLETE');
    if (contract.plan_id !== planId) return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_PLAN_MISMATCH');
    if (!/^sha256:[0-9a-f]{64}$/.test(String(contract.trajectory_revision || ''))) return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_REVISION_INVALID');
    if (!contract.meta || typeof contract.meta !== 'object') return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_META_MISSING');
    if (!Array.isArray(contract.slots) || contract.slots.length === 0) return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_SLOTS_MISSING');

    const canonicalJson = value => {
        if (Array.isArray(value)) return '[' + value.map(canonicalJson).join(',') + ']';
        if (value && typeof value === 'object') {
            return '{' + Object.keys(value).sort().map(key => JSON.stringify(key) + ':' + canonicalJson(value[key])).join(',') + '}';
        }
        return JSON.stringify(value);
    };
    if (!planMeta.input_revisions || canonicalJson(contract.input_revisions) !== canonicalJson(planMeta.input_revisions)) {
        return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_INPUT_REVISION_MISMATCH');
    }

    const finite = value => typeof value === 'number' && Number.isFinite(value);
    const finiteOrNull = value => value === null || finite(value);
    const slotDurationS = Number(contract.slot_duration_s);
    if (!Number.isFinite(slotDurationS) || slotDurationS <= 0) return evidenceLimit('DIRECT_MARKETING_SLOT_DURATION_INVALID');
    const validFromTs = Number(contract.valid_from_ts_ms);
    const horizonEndTs = Number(contract.horizon_end_ts_ms);
    if (!Number.isFinite(validFromTs) || !Number.isFinite(horizonEndTs) || horizonEndTs <= validFromTs) {
        return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_HORIZON_INVALID');
    }
    let previousEnd = null;
    let previousSocEnd = null;
    let previousSlotId = null;
    const headroomProjectionIds = new Set();
    const hasHeadroomProjectionContract = contract.slots.some(slot => slot && typeof slot === 'object'
        && (slot.action === 'HEADROOM_EXPORT' || slot.action_role === 'PROJECTION_ONLY'
            || slot.projection_only === true || slot.headroom_projection !== null && slot.headroom_projection !== undefined));
    const slots = [];
    for (const source of contract.slots) {
        if (!source || typeof source !== 'object') return evidenceLimit('DIRECT_MARKETING_SLOT_INVALID');
        const start = Number(source.start_ts_ms);
        const end = Number(source.end_ts_ms);
        const action = typeof source.action === 'string' ? source.action.trim().toUpperCase() : '';
        const selection = source.selection;
        const pv = source.pv_w;
        const loads = source.loads_w;
        const provenance = source.provenance && typeof source.provenance === 'object' && !Array.isArray(source.provenance)
            ? source.provenance
            : null;
        if (typeof source.slot_id !== 'string' || source.slot_id === ''
            || !Number.isFinite(start) || !Number.isFinite(end) || end <= start
            || Math.abs((end - start) - slotDurationS * 1000) > 1
            || (previousEnd !== null && start !== previousEnd)
            || !finite(source.soc_start_pct) || !finite(source.soc_end_pct)
            || !finite(source.battery_w) || !finite(source.grid_w)
            || !finite(source.residual_before_storage_w) || !finite(source.residual_after_storage_w)
            || action === ''
            || !selection || typeof selection !== 'object'
            || typeof selection.selected !== 'boolean'
            || typeof selection.executable !== 'boolean'
            || typeof selection.commands_allowed !== 'boolean'
            || !pv || typeof pv !== 'object'
            || !finite(pv.total) || !finiteOrNull(pv.e3dc_dc) || !finiteOrNull(pv.external_ac)
            || !loads || typeof loads !== 'object'
            || !finite(loads.house) || !finite(loads.heat) || !finiteOrNull(loads.wp)
            || !finiteOrNull(loads.climate) || !finite(loads.wallbox) || !finite(loads.total)) {
            return evidenceLimit('DIRECT_MARKETING_SLOT_EVIDENCE_INCOMPLETE');
        }
        if (source.soc_start_pct < 0 || source.soc_start_pct > 100
            || source.soc_end_pct < 0 || source.soc_end_pct > 100
            || (previousSocEnd !== null && Math.abs(source.soc_start_pct - previousSocEnd) > 0.0015)) {
            return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_SOC_CONTINUITY_INVALID');
        }
        const loadsTotal = loads.house + loads.heat + loads.wallbox;
        const expectedGrid = loads.total + source.battery_w - pv.total;
        const expectedResidualBefore = pv.total - loads.total;
        const expectedResidualAfter = expectedResidualBefore - source.battery_w;
        if (Math.abs(loadsTotal - loads.total) > 0.01
            || Math.abs(expectedGrid - source.grid_w) > 0.01
            || Math.abs(expectedResidualBefore - source.residual_before_storage_w) > 0.01
            || Math.abs(expectedResidualAfter - source.residual_after_storage_w) > 0.01
            || Math.abs(expectedResidualAfter + source.grid_w) > 0.01) {
            return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_BALANCE_INVALID');
        }
        if (!provenance) return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_PROVENANCE_INVALID');
        const socProjectionContract = String(provenance.soc_projection_contract || '');
        const standardPassthrough = socProjectionContract === 'canonical_standard_soc_passthrough_v1';
        const standardTransition = socProjectionContract === 'canonical_standard_transition_rebased_v1';
        const currentTransition = start === validFromTs;
        const expectedTransitionAnchorTs = currentTransition
            ? Number(contract.generated_at_ts_ms)
            : start;
        const expectedTransitionDurationS = (end - expectedTransitionAnchorTs) / 1000;
        const standardTransitionDurationValid =
            provenance.integration_duration_contract === 'canonical_standard_transition_duration_v1'
            && Number.isInteger(provenance.integration_anchor_ts_ms)
            && Number.isInteger(expectedTransitionAnchorTs)
            && provenance.integration_anchor_ts_ms === expectedTransitionAnchorTs
            && start <= provenance.integration_anchor_ts_ms
            && provenance.integration_anchor_ts_ms < end
            && finite(provenance.integration_duration_s)
            && provenance.integration_duration_s > 0
            && provenance.integration_duration_s <= slotDurationS
            && finite(expectedTransitionDurationS)
            && Math.abs(provenance.integration_duration_s - expectedTransitionDurationS) <= 0.001;
        const transitionKeys = ['soc_transition_contract', 'predecessor_slot_id', 'canonical_standard_start_soc_pct', 'rebased_start_soc_pct', 'standard_requested_battery_w', 'integration_duration_contract', 'integration_anchor_ts_ms', 'integration_duration_s'];
        const transitionFieldPresent = transitionKeys.some(key => Object.prototype.hasOwnProperty.call(provenance, key));
        const standardTransitionValid = standardTransition
            && provenance.soc_transition_contract === 'canonical_standard_transition_rebased_v1'
            && action === 'PASSIVE_NORMAL'
            && provenance.predecessor_slot_id === previousSlotId
            && finite(provenance.canonical_standard_start_soc_pct)
            && finite(provenance.rebased_start_soc_pct)
            && finite(provenance.standard_requested_battery_w)
            && standardTransitionDurationValid
            && Math.abs(provenance.rebased_start_soc_pct - source.soc_start_pct) <= 0.0015;
        if ((standardTransition && !standardTransitionValid)
            || (!standardTransition && transitionFieldPresent)
            || (!standardPassthrough && !standardTransition
                && socProjectionContract !== 'direct_marketing_energy_integrator_v1')) {
            return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_SOC_PROJECTION_CONTRACT_INVALID');
        }
        const selectedRole = selection.selected === true
            && selection.executable === true
            && selection.commands_allowed === true
            && typeof selection.action_id === 'string' && /^sha256:[0-9a-f]{64}$/.test(selection.action_id)
            && typeof selection.window_id === 'string' && selection.window_id !== '';
        const delegation = source.delegation;
        const standardProjectionBinding = source.standard_projection_binding;
        const standardProjectionBindingKeys = standardProjectionBinding
            && typeof standardProjectionBinding === 'object'
            && !Array.isArray(standardProjectionBinding)
            ? Object.keys(standardProjectionBinding).sort()
            : [];
        const standardProjectionBindingValid = standardProjectionBinding
            && typeof standardProjectionBinding === 'object'
            && !Array.isArray(standardProjectionBinding)
            && JSON.stringify(standardProjectionBindingKeys) === JSON.stringify([
                'commands_allowed', 'executable', 'hardware_effect',
                'projection_only', 'schema', 'source_revision', 'source_schema'
            ])
            && standardProjectionBinding.schema === 'canonical_standard_projection_binding_v1'
            && standardProjectionBinding.projection_only === true
            && standardProjectionBinding.executable === false
            && standardProjectionBinding.commands_allowed === false
            && standardProjectionBinding.hardware_effect === false
            && standardProjectionBinding.source_schema === 'direct_marketing_headroom_projection_plan_v1'
            && /^sha256:[0-9a-f]{64}$/.test(String(standardProjectionBinding.source_revision || ''));
        const delegatedPvStore = action === 'PV_STORE'
            && delegation && typeof delegation === 'object'
            && standardProjectionBinding === null
            && selection.selected === false
            && delegation.schema_version === 'direct_marketing_future_pv_store_delegation_v1'
            && delegation.active === true && delegation.commands_allowed === true
            && delegation.action === 'PV_STORE' && delegation.no_grid_charge === true
            && delegation.pv_store_source_contract === 'E3DC_DC'
            && Number.isFinite(Number(delegation.valid_until_ts_ms))
            && Number(delegation.valid_until_ts_ms) >= end
            && Number.isFinite(Number(delegation.max_curve_charge_w))
            && Number(delegation.max_curve_charge_w) > 0;
        const projectionOnlyMarker = action === 'HEADROOM_EXPORT'
            || Object.prototype.hasOwnProperty.call(source, 'action_role')
            || Object.prototype.hasOwnProperty.call(source, 'projection_only')
            || Object.prototype.hasOwnProperty.call(source, 'hardware_effect')
            || Object.prototype.hasOwnProperty.call(source, 'headroom_projection')
            || Object.prototype.hasOwnProperty.call(selection, 'projected_action')
            || Object.prototype.hasOwnProperty.call(selection, 'projected_w')
            || Object.prototype.hasOwnProperty.call(selection, 'projection_id');
        let projectionRole = false;
        let projectedW = null;
        let projectionEffectiveStartTs = null;
        let projectionEffectiveDurationS = null;
        if (projectionOnlyMarker) {
            const selectionKeys = Object.keys(selection).sort();
            const expectedSelectionKeys = ['commands_allowed', 'executable', 'projected_action', 'projected_w', 'projection_id', 'selected'];
            const binding = source.headroom_projection;
            const bindingSlot = binding && typeof binding === 'object' && binding.slot && typeof binding.slot === 'object'
                ? binding.slot
                : null;
            projectedW = selection.projected_w;
            const projectionId = String(selection.projection_id || '');
            const bindingKeys = binding && typeof binding === 'object' && !Array.isArray(binding)
                ? Object.keys(binding).sort()
                : [];
            const bindingSlotKeys = bindingSlot && !Array.isArray(bindingSlot)
                ? Object.keys(bindingSlot).sort()
                : [];
            const expectedBindingKeys = ['projection_only', 'projection_plan_revision', 'schema', 'slot'];
            const expectedBindingSlotKeys = ['commands_allowed', 'duration_s', 'effective_duration_s', 'effective_start_ts', 'effective_window_duration_s', 'effective_window_end_ts', 'effective_window_start_ts', 'end_ts', 'energy_basis', 'executable', 'forecast_absorption_wh', 'hardware_effect', 'headroom_deficit_wh', 'headroom_export_budget_id', 'headroom_export_budget_wh', 'headroom_export_slot_energy_wh', 'headroom_export_slot_id', 'headroom_free_before_wh', 'headroom_required_wh', 'next_charge_end_ts', 'next_charge_start_ts', 'projected_action', 'projected_mode', 'projected_power_w', 'projected_source_action', 'projection_horizon_contract', 'projection_id', 'projection_only', 'protected_reserve_wh', 'reserve_floor_soc_pct', 'segment_id', 'sellable_wh', 'start_ts', 'target_soc_pct', 'window_end_ts', 'window_id', 'window_start_ts'];
            const energyBinding = provenance.headroom_energy_binding;
            const energyBindingKeys = energyBinding && typeof energyBinding === 'object' && !Array.isArray(energyBinding)
                ? Object.keys(energyBinding).sort()
                : [];
            const expectedEnergyBindingKeys = ['applied_ac_discharge_w', 'applied_stored_delta_wh', 'axis_duration_s', 'bounded', 'bounding_status', 'desired_ac_discharge_w', 'discharge_efficiency', 'effective_duration_s', 'effective_start_ts', 'energy_basis', 'hardware_discharge_limit_w', 'limiting_factors', 'requested_stored_delta_wh', 'reserve_ac_discharge_limit_w', 'reserve_available_stored_wh', 'schema', 'slot_energy_ac_discharge_limit_w', 'stored_delta_rate_w'];
            const provenanceKeys = Object.keys(provenance).sort();
            const expectedProvenanceKeys = ['action_source', 'balance_source', 'candidate_effect', 'headroom_energy_binding', 'pv_axis_evidence_class', 'shadow_effect', 'soc_projection_contract'];
            projectionEffectiveStartTs = bindingSlot ? bindingSlot.effective_start_ts : null;
            projectionEffectiveDurationS = bindingSlot ? bindingSlot.effective_duration_s : null;
            const effectiveEndTs = finite(projectionEffectiveStartTs) && finite(projectionEffectiveDurationS)
                ? projectionEffectiveStartTs + projectionEffectiveDurationS * 1000
                : null;
            const energyNumericFields = ['stored_delta_rate_w', 'requested_stored_delta_wh', 'discharge_efficiency', 'desired_ac_discharge_w', 'hardware_discharge_limit_w', 'reserve_available_stored_wh', 'slot_energy_ac_discharge_limit_w', 'reserve_ac_discharge_limit_w', 'applied_ac_discharge_w', 'applied_stored_delta_wh'];
            const limitingFactors = energyBinding && Array.isArray(energyBinding.limiting_factors)
                ? energyBinding.limiting_factors
                : null;
            const allowedLimitingFactors = ['desired_ac_discharge_w', 'hardware_discharge_limit_w', 'slot_energy_ac_discharge_limit_w', 'reserve_ac_discharge_limit_w'];
            const boundingStatusValid = energyBinding && (
                energyBinding.bounding_status === 'UNBOUNDED' && energyBinding.bounded === false
                    && Math.abs(energyBinding.applied_ac_discharge_w - energyBinding.desired_ac_discharge_w) <= 0.001
                || energyBinding.bounding_status === 'BOUNDED' && energyBinding.bounded === true
                    && energyBinding.applied_ac_discharge_w > 0.001
                    && energyBinding.applied_ac_discharge_w + 0.001 < energyBinding.desired_ac_discharge_w
                || energyBinding.bounding_status === 'ZERO_BOUNDED' && energyBinding.bounded === true
                    && energyBinding.applied_ac_discharge_w <= 0.001
                    && energyBinding.desired_ac_discharge_w > 0.001
            );
            const forbiddenTopLevelAuthority = ['requested_w', 'plan_action', 'gate', 'selected', 'executable', 'commands_allowed', 'runtime_effect_claim_allowed', 'action_id', 'window_id', 'segment_id', 'source_action', 'source_mode', 'headroom_export_gate']
                .some(key => Object.prototype.hasOwnProperty.call(source, key));
            const roleValid = action === 'HEADROOM_EXPORT'
                && source.action_role === 'PROJECTION_ONLY'
                && source.projection_only === true
                && source.hardware_effect === false
                && forbiddenTopLevelAuthority === false
                && JSON.stringify(selectionKeys) === JSON.stringify(expectedSelectionKeys)
                && selection.selected === false
                && selection.executable === false
                && selection.commands_allowed === false
                && selection.projected_action === 'HEADROOM_EXPORT'
                && finite(projectedW) && projectedW >= 0
                && /^headroom-slot:[0-9a-f]{64}$/.test(projectionId)
                && !headroomProjectionIds.has(projectionId)
                && binding && typeof binding === 'object'
                && JSON.stringify(bindingKeys) === JSON.stringify(expectedBindingKeys)
                && binding.schema === 'direct_marketing_headroom_projection_binding_v1'
                && binding.projection_only === true
                && /^sha256:[0-9a-f]{64}$/.test(String(binding.projection_plan_revision || ''))
                && bindingSlot
                && JSON.stringify(bindingSlotKeys) === JSON.stringify(expectedBindingSlotKeys)
                && bindingSlot.projection_id === projectionId
                && bindingSlot.headroom_export_slot_id === projectionId
                && Number(bindingSlot.start_ts) === start
                && Number(bindingSlot.end_ts) === end
                && bindingSlot.projected_action === 'HEADROOM_EXPORT'
                && bindingSlot.projection_only === true
                && bindingSlot.executable === false
                && bindingSlot.commands_allowed === false
                && bindingSlot.hardware_effect === false
                && bindingSlot.energy_basis === 'stored_battery_energy_delta_before_discharge_loss_v1'
                && bindingSlot.duration_s === slotDurationS
                && finite(projectionEffectiveStartTs)
                && projectionEffectiveStartTs >= start && projectionEffectiveStartTs < end
                && finite(projectionEffectiveDurationS)
                && projectionEffectiveDurationS > 0 && projectionEffectiveDurationS <= slotDurationS
                && Math.abs(effectiveEndTs - end) <= 0.001
                && finite(bindingSlot.effective_window_start_ts)
                && finite(bindingSlot.effective_window_end_ts)
                && finite(bindingSlot.effective_window_duration_s)
                && bindingSlot.effective_window_start_ts <= projectionEffectiveStartTs
                && bindingSlot.effective_window_end_ts >= end
                && bindingSlot.effective_window_duration_s >= projectionEffectiveDurationS
                && finite(bindingSlot.projected_power_w) && bindingSlot.projected_power_w > 0
                && finite(bindingSlot.headroom_export_slot_energy_wh) && bindingSlot.headroom_export_slot_energy_wh > 0
                && energyBinding && typeof energyBinding === 'object'
                && JSON.stringify(energyBindingKeys) === JSON.stringify(expectedEnergyBindingKeys)
                && JSON.stringify(provenanceKeys) === JSON.stringify(expectedProvenanceKeys)
                && energyBinding.schema === 'direct_marketing_headroom_energy_binding_v1'
                && energyBinding.energy_basis === bindingSlot.energy_basis
                && energyBinding.axis_duration_s === slotDurationS
                && energyBinding.effective_start_ts === projectionEffectiveStartTs
                && energyBinding.effective_duration_s === projectionEffectiveDurationS
                && energyBinding.stored_delta_rate_w === bindingSlot.projected_power_w
                && energyBinding.requested_stored_delta_wh === bindingSlot.headroom_export_slot_energy_wh
                && energyNumericFields.every(key => finite(energyBinding[key]) && energyBinding[key] >= 0)
                && energyBinding.discharge_efficiency > 0 && energyBinding.discharge_efficiency <= 1
                && energyBinding.applied_stored_delta_wh <= energyBinding.requested_stored_delta_wh + 0.001
                && energyBinding.applied_ac_discharge_w <= energyBinding.hardware_discharge_limit_w + 0.001
                && energyBinding.applied_ac_discharge_w <= energyBinding.slot_energy_ac_discharge_limit_w + 0.001
                && energyBinding.applied_ac_discharge_w <= energyBinding.reserve_ac_discharge_limit_w + 0.001
                && limitingFactors && limitingFactors.length > 0
                && limitingFactors.every((factor, index) => allowedLimitingFactors.includes(factor)
                    && limitingFactors.indexOf(factor) === index)
                && boundingStatusValid
                && Math.abs(energyBinding.applied_ac_discharge_w - projectedW) <= 0.001
                && delegation === null
                && source.passive_binding === null
                && source.standard_projection_binding === null
                && provenance.action_source === 'direct_marketing.headroom_projection_plan'
                && provenance.candidate_effect === false
                && provenance.shadow_effect === false
                && source.battery_w <= 0
                && Math.abs(Math.abs(source.battery_w) - projectedW) <= 0.001
                && finite(source.hard_reserve_soc_pct)
                && source.soc_end_pct + 0.002 >= source.hard_reserve_soc_pct;
            if (!roleValid) return evidenceLimit('DIRECT_MARKETING_HEADROOM_PROJECTION_ROLE_INVALID');
            headroomProjectionIds.add(projectionId);
            projectionRole = true;
        }
        const selectedAction = action === 'PV_STORE' || action === 'ECONOMIC_EXPORT' || action === 'CHARGE_BLOCK_WAIT' || action === 'DV_CURVE_CHARGE';
        const passiveBinding = source.passive_binding;
        const passiveMetadataClear = ['action_id', 'window_id', 'segment_id', 'source_action', 'source_mode', 'pv_store_source_contract']
            .every(key => Object.prototype.hasOwnProperty.call(selection, key) && selection[key] === null);
        const passiveBindingValid = passiveBinding && typeof passiveBinding === 'object'
            && passiveBinding.schema === 'direct_marketing_passive_normal_binding_v1'
            && standardProjectionBinding === null;
        const transitionWithoutPassiveBinding = standardProjectionBindingValid
            && passiveBinding === null && (standardPassthrough || standardTransitionValid);
        const passiveRole = action === 'PASSIVE_NORMAL'
            && selection.selected === false && selection.executable === false && selection.commands_allowed === false
            && finite(selection.requested_w) && selection.requested_w === 0
            && passiveMetadataClear && delegation === null
            && (passiveBindingValid || transitionWithoutPassiveBinding);
        if ((selection.selected === true && delegation !== null)
            || (selectedRole && standardProjectionBinding !== null)
            || (!projectionRole && selectedAction && !selectedRole && !delegatedPvStore)
            || (!projectionRole && !selectedAction && !passiveRole)) {
            return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_ACTION_ROLE_INVALID');
        }
        if (action === 'PV_STORE' || action === 'DV_CURVE_CHARGE') {
            const dcOnly = delegatedPvStore || selection.pv_store_source_contract === 'E3DC_DC';
            const capW = delegatedPvStore ? Number(delegation.max_curve_charge_w) : Number(selection.requested_w);
            if (source.battery_w < -0.01
                || source.battery_w > source.residual_before_storage_w + 0.01
                || !Number.isFinite(capW) || capW <= 0 || source.battery_w > capW + 0.01
                || (dcOnly && (!finite(pv.e3dc_dc) || source.battery_w > pv.e3dc_dc + 0.01))) {
                return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_PV_STORE_PHYSICS_INVALID');
            }
        }
        if (action === 'ECONOMIC_EXPORT') {
            const requestedW = Number(selection.requested_w);
            if (!Number.isFinite(requestedW) || requestedW <= 0
                || source.battery_w > 0.01 || Math.abs(source.battery_w) > requestedW + 0.01) {
                return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_EXPORT_PHYSICS_INVALID');
            }
        }
        if (action === 'CHARGE_BLOCK_WAIT' && source.battery_w > 0.01) {
            return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_CHARGE_BLOCK_PHYSICS_INVALID');
        }
        const runtime = storageDispatchRuntimeForDisplay(data);
        const runtimeForSlot = runtime && runtime.slot_id === source.slot_id ? runtime : null;
        slots.push({
            slotId: source.slot_id,
            startTs: start,
            endTs: end,
            socStartPct: source.soc_start_pct,
            socEndPct: source.soc_end_pct,
            batteryW: source.battery_w,
            gridW: source.grid_w,
            pvW: {...pv},
            loadsW: {...loads},
            residualBeforeStorageW: source.residual_before_storage_w,
            residualAfterStorageW: source.residual_after_storage_w,
            action,
            plannedAllowed: selectedRole || delegatedPvStore,
            plannedRole: projectionRole ? 'projection' : (delegatedPvStore ? 'delegation' : (selectedRole ? 'selected' : 'passive')),
            plannedW: projectionRole ? null : (selectedRole ? Number(selection.requested_w) : (delegatedPvStore ? Number(delegation.max_curve_charge_w) : null)),
            projectedW: projectionRole ? projectedW : null,
            projectionEffectiveStartTs: projectionRole ? projectionEffectiveStartTs : null,
            projectionEffectiveDurationS: projectionRole ? projectionEffectiveDurationS : null,
            candidate: null,
            planned: {...selection},
            runtime: runtimeForSlot,
            effect: runtimeForSlot && Object.prototype.hasOwnProperty.call(runtimeForSlot, 'hardware_effect')
                ? {hardwareEffect: runtimeForSlot.hardware_effect}
                : null,
            reasonCode: source.reason_code || null,
            provenance: {...provenance}
        });
        previousEnd = end;
        previousSocEnd = source.soc_end_pct;
        previousSlotId = source.slot_id;
    }
    if (slots[0].startTs !== validFromTs || slots[slots.length - 1].endTs !== horizonEndTs) {
        return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_HORIZON_MISMATCH');
    }

    const contractSlotById = new Map(slots.map(slot => [slot.slotId, slot]));
    const publishedCurve = Array.isArray(data.storage_sim_curve) ? data.storage_sim_curve : [];
    for (const point of publishedCurve) {
        if (!point || typeof point.slot_id !== 'string') continue;
        const bound = contractSlotById.get(point.slot_id);
        if (bound && Number(point.ts) !== bound.startTs) return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_SLOT_BINDING_MISMATCH');
    }
    const publishedSlotIds = Array.isArray(data.storage_slot_id) ? data.storage_slot_id : [];
    const publishedTimestamps = Array.isArray(data.timestamps) ? data.timestamps : [];
    for (let index = 0; index < Math.min(publishedSlotIds.length, publishedTimestamps.length); index += 1) {
        const slotId = publishedSlotIds[index];
        if (typeof slotId !== 'string' || slotId === '') continue;
        const bound = contractSlotById.get(slotId);
        if (!bound || Number(publishedTimestamps[index]) !== bound.startTs) {
            return evidenceLimit('DIRECT_MARKETING_TRAJECTORY_SLOT_BINDING_MISMATCH');
        }
    }

    const soc = [{x: slots[0].startTs, y: slots[0].socStartPct}]
        .concat(slots.map(slot => ({x: slot.endTs, y: slot.socEndPct})));
    const fallbackBound = actionFallback.state === 'complete'
        && actionFallback.slots.length === slots.length
        && slots.every((slot, index) => actionFallback.slots[index].slotId === slot.slotId
            && actionFallback.slots[index].startTs === slot.startTs
            && actionFallback.slots[index].endTs === slot.endTs);
    return {
        active: true,
        state: 'complete',
        reasonCode: null,
        planId,
        meta: {...contract.meta},
        slots,
        series: {
            soc,
            pvStoreW: fallbackBound ? actionFallback.series.pvStoreW : slots.map(slot => slot.plannedAllowed && (slot.action === 'PV_STORE' || slot.action === 'DV_CURVE_CHARGE') ? Math.abs(slot.batteryW) : null),
            economicExportW: fallbackBound ? actionFallback.series.economicExportW : slots.map(slot => slot.plannedAllowed && slot.action === 'ECONOMIC_EXPORT' ? Math.abs(slot.batteryW) : null),
            headroomProjectionW: slots.map(slot => slot.plannedRole === 'projection' && slot.action === 'HEADROOM_EXPORT' ? slot.projectedW : null),
            chargeBlock: fallbackBound ? actionFallback.series.chargeBlock : slots.map(slot => slot.plannedAllowed && slot.action === 'CHARGE_BLOCK_WAIT' ? 1 : null)
        }
    };
}

function storageTrajectoryNumberOrNull(value) {
    if (value === null || value === undefined || typeof value === 'boolean') return null;
    if (typeof value === 'string' && value.trim() === '') return null;
    const parsed = Number(value);
    return Number.isFinite(parsed) ? parsed : null;
}

function storageBaseTrajectoryEvidence(rawBase, planId) {
    const evidenceLimit = reasonCode => ({state: 'evidence_limit', reasonCode, soc: []});
    if (!Array.isArray(rawBase) || rawBase.length === 0) {
        return evidenceLimit('STORAGE_BASE_FORECAST_MISSING');
    }
    if (rawBase.length < 2) {
        return evidenceLimit('STORAGE_BASE_FORECAST_TOO_SHORT');
    }
    if (rawBase.some(point => !point || typeof point !== 'object')) {
        return evidenceLimit('STORAGE_BASE_POINT_INVALID');
    }
    if (rawBase.some(point => typeof point.plan_id !== 'string' || point.plan_id.trim() === '')) {
        return evidenceLimit('STORAGE_BASE_PLAN_BINDING_MISSING');
    }
    if (rawBase.some(point => !/^sha256:[0-9a-f]{64}$/.test(point.plan_id))) {
        return evidenceLimit('STORAGE_BASE_PLAN_BINDING_INVALID');
    }
    if (rawBase.some(point => point.plan_id !== planId)) {
        return evidenceLimit('STORAGE_BASE_PLAN_MISMATCH');
    }
    if (rawBase.some(point => typeof point.slot_id !== 'string' || point.slot_id.trim() === '')) {
        return evidenceLimit('STORAGE_BASE_SLOT_BINDING_MISSING');
    }
    if (rawBase.some(point => {
        const ts = storageTrajectoryNumberOrNull(point.ts);
        return ts === null || ts <= 0;
    })) {
        return evidenceLimit('STORAGE_BASE_TIMESTAMP_INVALID');
    }
    if (rawBase.some(point => storageTrajectoryNumberOrNull(point.soc) === null)) {
        return evidenceLimit('STORAGE_BASE_SOC_INVALID');
    }
    return {
        state: 'complete',
        reasonCode: null,
        soc: rawBase.map(point => ({
            ts: storageTrajectoryNumberOrNull(point.ts),
            soc: storageTrajectoryNumberOrNull(point.soc),
            slotId: point.slot_id.trim()
        })).sort((a, b) => a.ts - b.ts)
    };
}

function storageTrajectoryViewModel(data = {}) {
    const meta = data && data.storage_plan_meta && typeof data.storage_plan_meta === 'object'
        ? data.storage_plan_meta
        : {};
    const planId = typeof meta.plan_id === 'string' ? meta.plan_id : '';
    const rawBase = Array.isArray(data && data.storage_sim_curve) ? data.storage_sim_curve : [];
    return {
        planId,
        base: storageBaseTrajectoryEvidence(rawBase, planId),
        directMarketing: directMarketingTrajectoryViewModel(data)
    };
}

function directMarketingTrajectorySeriesForTimestamps(data, timestamps) {
    const view = directMarketingTrajectoryViewModel(data);
    const empty = () => Array.isArray(timestamps) ? timestamps.map(() => null) : [];
    if (!['complete', 'actions_only'].includes(view.state) || !Array.isArray(timestamps)) {
        return {view, soc: empty(), pvStoreW: empty(), economicExportW: empty(), headroomProjectionW: empty(), chargeBlock: empty()};
    }
    const slotIndexAt = ts => view.slots.findIndex(slot => slot.startTs <= ts && ts < slot.endTs);
    const mapped = timestamps.map(rawTs => {
        const ts = Number(rawTs);
        if (!Number.isFinite(ts)) return null;
        const slotIndex = slotIndexAt(ts);
        if (slotIndex < 0) return null;
        const slot = view.slots[slotIndex];
        return {
            slot,
            soc: view.state === 'complete' ? slot.socStartPct : null,
            pvStoreW: view.series.pvStoreW[slotIndex] ?? null,
            economicExportW: view.series.economicExportW[slotIndex] ?? null,
            headroomProjectionW: slot.plannedRole === 'projection'
                && Number.isFinite(slot.projectionEffectiveStartTs)
                && Number.isFinite(slot.projectionEffectiveDurationS)
                && ts >= slot.projectionEffectiveStartTs
                && ts < slot.projectionEffectiveStartTs + slot.projectionEffectiveDurationS * 1000
                    ? (view.series.headroomProjectionW[slotIndex] ?? null)
                    : null,
            chargeBlock: view.series.chargeBlock[slotIndex] ?? null
        };
    });
    return {
        view,
        soc: mapped.map(point => point ? point.soc : null),
        pvStoreW: mapped.map(point => point ? point.pvStoreW : null),
        economicExportW: mapped.map(point => point ? point.economicExportW : null),
        headroomProjectionW: mapped.map(point => point ? point.headroomProjectionW : null),
        chargeBlock: mapped.map(point => point ? point.chargeBlock : null)
    };
}

function storageTargetCurveForDisplay(targetCurve, curveAnchors) {
    const normalize = points => {
        if (!Array.isArray(points)) return [];
        const byTimestamp = new Map();
        points.forEach(point => {
            if (!point || typeof point !== 'object') return;
            const ts = Number(point.ts);
            const soc = Number(point.soc ?? point.target_soc);
            if (!Number.isFinite(ts) || !Number.isFinite(soc)) return;
            byTimestamp.set(ts, {
                ts,
                soc: Math.max(0, Math.min(100, soc))
            });
        });
        return Array.from(byTimestamp.values()).sort((a, b) => a.ts - b.ts);
    };

    const canonical = normalize(targetCurve);
    if (canonical.length >= 2) {
        return {points: canonical, source: 'target_curve'};
    }

    // Der kanonische Plan kann während einer gemischten Updategeneration
    // bereits gültige, eingefrorene Stundenanker liefern, obwohl die
    // slotweise Zielprojektion noch fehlt. Da die Sollkurve per Vertrag
    // zwischen genau diesen Ankern interpoliert wird, ist das keine
    // erfundene Prognose, sondern eine verlustfreie Anzeige-Rückfallebene.
    const anchors = normalize(curveAnchors);
    if (anchors.length >= 2) {
        return {points: anchors, source: 'curve_anchors_fallback'};
    }

    return {
        points: canonical,
        source: canonical.length ? 'single_target_point' : 'missing'
    };
}

function cacheStorageCurveData(data) {
    if (!data || typeof data !== 'object') return;
    const previousData = window._storageLiveData && typeof window._storageLiveData === 'object'
        ? window._storageLiveData
        : {};
    const previousPlanId = previousData.storage_plan_meta?.plan_id
        || window._storagePlanMeta?.plan_id
        || '';
    const incomingPlanId = data.storage_plan_meta && typeof data.storage_plan_meta === 'object'
        ? (data.storage_plan_meta.plan_id || '')
        : '';
    const planChanged = Boolean(previousPlanId && incomingPlanId && previousPlanId !== incomingPlanId);
    const mergedData = {...previousData, ...data};
    const clearClassicalCurves = data.storage_plan_meta
        && data.storage_plan_meta.clear_classical_curves === true;

    if (clearClassicalCurves) {
        ['storage_target_curve', 'storage_sim_curve', 'storage_curve_anchors']
            .forEach(key => { mergedData[key] = []; });
    }

    // Live-WebSocket-Telegramme enthalten häufig nur Messwerte. Sie dürfen
    // deshalb einen zuvor vollständig geladenen Plan samt Soll-/PV-Kurven
    // nicht mit implizit fehlenden Feldern leeren. Bei einem belegten
    // Planwechsel werden dagegen alte Kurven verworfen, bis die neuen,
    // planidentisch gebundenen Daten eintreffen.
    if (planChanged) {
        [
            'storage_target_curve',
            'storage_soc_min_curve',
            'storage_soc_ceiling_curve',
            'storage_sim_curve',
            'storage_curve_anchors',
            'direct_marketing_trajectory',
            'direct_marketing_selected_action_fallback'
        ].forEach(key => {
            if (!Object.prototype.hasOwnProperty.call(data, key)) mergedData[key] = [];
        });
        if (!Object.prototype.hasOwnProperty.call(data, 'effective_storage_plan')) {
            mergedData.effective_storage_plan = null;
        }
    }

    window._storageLiveData = mergedData;
    if (Array.isArray(mergedData.storage_curve_anchors)) window._storageCurveAnchors = mergedData.storage_curve_anchors;
    else if (!Array.isArray(window._storageCurveAnchors)) window._storageCurveAnchors = [];
    const targetCurveDisplay = storageTargetCurveForDisplay(
        mergedData.storage_target_curve,
        window._storageCurveAnchors
    );
    window._storageSollCurve = targetCurveDisplay.points;
    window._storageSollCurveSource = targetCurveDisplay.source;
    if (Array.isArray(mergedData.storage_soc_min_curve)) window._storageSocMinCurve = mergedData.storage_soc_min_curve;
    else if (!Array.isArray(window._storageSocMinCurve)) window._storageSocMinCurve = [];
    if (Array.isArray(mergedData.storage_soc_ceiling_curve)) window._storageSocCeilingCurve = mergedData.storage_soc_ceiling_curve;
    else if (!Array.isArray(window._storageSocCeilingCurve)) window._storageSocCeilingCurve = [];
    if (Array.isArray(mergedData.storage_sim_curve)) window._storageSimCurve = mergedData.storage_sim_curve;
    else if (!Array.isArray(window._storageSimCurve)) window._storageSimCurve = [];
    window._storageDispatchRuntime = storageDispatchRuntimeForDisplay(mergedData);
    window._storageDispatchRuntimeBinding = mergedData.storage_dispatch_runtime && typeof mergedData.storage_dispatch_runtime === 'object'
        ? (window._storageDispatchRuntime ? 'bound' : 'mismatch')
        : 'missing';
    if (mergedData.storage_plan_meta && typeof mergedData.storage_plan_meta === 'object') window._storagePlanMeta = mergedData.storage_plan_meta;
    else if (!window._storagePlanMeta) window._storagePlanMeta = {};
    window._storageTrajectoryViewModel = storageTrajectoryViewModel(mergedData);
    if (mergedData.direct_marketing && typeof mergedData.direct_marketing === 'object') window._directMarketingPlan = mergedData.direct_marketing;
    else if (mergedData.storage_plan_meta && mergedData.storage_plan_meta.direct_marketing && typeof mergedData.storage_plan_meta.direct_marketing === 'object') window._directMarketingPlan = mergedData.storage_plan_meta.direct_marketing;
    else if (!window._directMarketingPlan) window._directMarketingPlan = {};
    if (mergedData.direct_marketing_monitor && typeof mergedData.direct_marketing_monitor === 'object') window._directMarketingMonitor = mergedData.direct_marketing_monitor;
    else if (mergedData.storage_plan_meta && mergedData.storage_plan_meta.direct_marketing_monitor && typeof mergedData.storage_plan_meta.direct_marketing_monitor === 'object') window._directMarketingMonitor = mergedData.storage_plan_meta.direct_marketing_monitor;
    else if (!window._directMarketingMonitor) window._directMarketingMonitor = {};
    if (mergedData.direct_marketing_daily_report && typeof mergedData.direct_marketing_daily_report === 'object') window._directMarketingDailyReport = mergedData.direct_marketing_daily_report;
    else if (mergedData.storage_plan_meta && mergedData.storage_plan_meta.direct_marketing_daily_report && typeof mergedData.storage_plan_meta.direct_marketing_daily_report === 'object') window._directMarketingDailyReport = mergedData.storage_plan_meta.direct_marketing_daily_report;
    else if (!window._directMarketingDailyReport) window._directMarketingDailyReport = {};
    const auxInverterState = mergedData.direct_marketing_aux_inverter_shelly
        || mergedData.storage_plan_meta?.direct_marketing_aux_inverter_shelly;
    if (auxInverterState && typeof auxInverterState === 'object') window._directMarketingAuxInverterShellyState = auxInverterState;
    else if (!window._directMarketingAuxInverterShellyState) window._directMarketingAuxInverterShellyState = {};
    if (mergedData.market_value_solar && typeof mergedData.market_value_solar === 'object') window._marketValueSolar = mergedData.market_value_solar;
    else if (mergedData.storage_plan_meta && mergedData.storage_plan_meta.market_value_solar && typeof mergedData.storage_plan_meta.market_value_solar === 'object') window._marketValueSolar = mergedData.storage_plan_meta.market_value_solar;
    else if (!window._marketValueSolar) window._marketValueSolar = {};
    if (mergedData.storage_reason != null) window._storageReason = mergedData.storage_reason || '';
    else if (window._storageReason == null) window._storageReason = '';
    if (mergedData.soc !== undefined && mergedData.soc !== null && !isNaN(parseFloat(mergedData.soc))) {
        window._storageLiveSoc = parseFloat(mergedData.soc);
    }
    if (mergedData.storage_curve_control_soc !== undefined && mergedData.storage_curve_control_soc !== null && !isNaN(parseFloat(mergedData.storage_curve_control_soc))) {
        window._storageControlSoc = parseFloat(mergedData.storage_curve_control_soc);
    }
    renderStorageCurveSparkline(mergedData);
}

function downsampleStorageSparkline(points, maxPoints = 48) {
    if (!Array.isArray(points) || points.length <= maxPoints) return Array.isArray(points) ? points.slice() : [];
    const sorted = points.slice().sort((a, b) => a.ts - b.ts);
    const kept = [sorted[0]];
    const interior = sorted.slice(1, -1);
    const buckets = Math.max(1, Math.floor((maxPoints - 2) / 2));
    const bucketSize = interior.length / buckets;
    for (let bucket = 0; bucket < buckets; bucket += 1) {
        const start = Math.floor(bucket * bucketSize);
        const end = Math.max(start + 1, Math.floor((bucket + 1) * bucketSize));
        const slice = interior.slice(start, end);
        if (!slice.length) continue;
        const min = slice.reduce((best, point) => point.soc < best.soc ? point : best, slice[0]);
        const max = slice.reduce((best, point) => point.soc > best.soc ? point : best, slice[0]);
        [min, max].sort((a, b) => a.ts - b.ts).forEach(point => {
            if (kept[kept.length - 1].ts !== point.ts) kept.push(point);
        });
    }
    kept.push(sorted[sorted.length - 1]);
    return kept.slice(0, maxPoints);
}

function storageSparklineSeries(data = {}) {
    const trajectory = storageTrajectoryViewModel(data);
    const planId = trajectory.planId;
    if (!/^sha256:[0-9a-f]{64}$/.test(planId)) {
        return {state: 'missing_plan', reasonCode: 'STORAGE_PLAN_ID_INVALID', planId: '', forecast: [], target: []};
    }
    const projectionDisplay = storageSparklineProjectionDisplay(data);
    if (projectionDisplay) {
        return {state: 'projection_hidden', reasonCode: projectionDisplay.reasonCode, planId, forecast: [], target: []};
    }
    if (trajectory.base.state !== 'complete') {
        const stateByReason = {
            STORAGE_BASE_FORECAST_MISSING: 'missing_forecast',
            STORAGE_BASE_FORECAST_TOO_SHORT: 'forecast_too_short',
            STORAGE_BASE_POINT_INVALID: 'invalid_point',
            STORAGE_BASE_PLAN_BINDING_MISSING: 'missing_plan_binding',
            STORAGE_BASE_PLAN_BINDING_INVALID: 'invalid_plan_binding',
            STORAGE_BASE_PLAN_MISMATCH: 'plan_mismatch',
            STORAGE_BASE_SLOT_BINDING_MISSING: 'missing_slot_id',
            STORAGE_BASE_TIMESTAMP_INVALID: 'invalid_timestamp',
            STORAGE_BASE_SOC_INVALID: 'invalid_soc'
        };
        return {
            state: stateByReason[trajectory.base.reasonCode] || 'invalid_forecast',
            reasonCode: trajectory.base.reasonCode || 'STORAGE_BASE_EVIDENCE_LIMIT',
            planId,
            forecast: [],
            target: []
        };
    }
    const forecast = trajectory.base.soc
        .map(point => ({ts: point.ts, soc: Math.max(0, Math.min(100, point.soc))}));
    if (forecast.length < 2) {
        return {state: 'forecast_too_short', reasonCode: 'STORAGE_BASE_FORECAST_TOO_SHORT', planId, forecast: [], target: []};
    }
    const target = (Array.isArray(data.storage_target_curve) ? data.storage_target_curve : [])
        .filter(point => point && storageTrajectoryNumberOrNull(point.ts) !== null
            && storageTrajectoryNumberOrNull(point.soc ?? point.target_soc) !== null)
        .map(point => ({
            ts: storageTrajectoryNumberOrNull(point.ts),
            soc: storageTrajectoryNumberOrNull(point.soc ?? point.target_soc)
        }))
        .map(point => ({ts: point.ts, soc: Math.max(0, Math.min(100, point.soc))}))
        .sort((a, b) => a.ts - b.ts);
    return {
        state: 'bound',
        reasonCode: null,
        planId,
        source: 'soc',
        forecast: downsampleStorageSparkline(forecast),
        target: downsampleStorageSparkline(target)
    };
}

function storageSparklineProjectionDisplay(data = {}) {
    const meta = data.storage_plan_meta && typeof data.storage_plan_meta === 'object'
        ? data.storage_plan_meta
        : {};
    const nestedEffectivePlan = meta.effective_storage_plan && typeof meta.effective_storage_plan === 'object'
        ? meta.effective_storage_plan
        : {};
    const effectivePlan = data.effective_storage_plan && typeof data.effective_storage_plan === 'object'
        ? data.effective_storage_plan
        : nestedEffectivePlan;
    const hidden = meta.clear_classical_curves === true || effectivePlan.clear_classical_curves === true;
    if (!hidden) return null;

    const planId = String(meta.plan_id || '').trim();
    const binding = effectivePlan.binding && typeof effectivePlan.binding === 'object'
        ? effectivePlan.binding
        : {};
    const action = String(effectivePlan.effective_action || '').trim().toUpperCase();
    const bindingAction = String(binding.action || '').trim().toUpperCase();
    const status = String(effectivePlan.status || '').trim().toUpperCase();
    const lifecycle = effectivePlan.lifecycle && typeof effectivePlan.lifecycle === 'object'
        ? effectivePlan.lifecycle
        : {};
    const lifecycleEffectConfirmed = lifecycle.effect_confirmed === true;
    const expectedEffectiveStatus = action ? `DIRECT_MARKETING_${action}_EFFECTIVE` : '';
    const expectedPendingStatus = action ? `DIRECT_MARKETING_${action}_PENDING` : '';
    const expectedLifecycleStatus = lifecycleEffectConfirmed
        ? expectedEffectiveStatus
        : expectedPendingStatus;
    const canonicalEffectivePlan = effectivePlan.schema_version === 'storage_effective_plan_v1'
        && effectivePlan.consistent === true
        && /^sha256:[0-9a-f]{64}$/.test(planId)
        && binding.plan_id === planId
        && action !== ''
        && bindingAction === action
        && expectedLifecycleStatus !== ''
        && status === expectedLifecycleStatus;
    if (!canonicalEffectivePlan) {
        return {
            curveState: 'hidden-dv-evidence-limit',
            text: 'DV-Wirkung nicht belegt',
            reasonCode: 'STORAGE_CLASSICAL_CURVE_HIDDEN_DV_EVIDENCE_LIMIT'
        };
    }
    const effectConfirmed = lifecycleEffectConfirmed;
    const requestObserved = lifecycle.requested === true
        || lifecycle.issued === true
        || lifecycle.retained === true
        || lifecycle.retained_effect === true;
    const projectionStage = effectConfirmed ? 'effective' : (requestObserved ? 'requested' : 'planned');
    const stagedDisplay = (curveState, reasonCode, effectiveText, requestedText, plannedText) => ({
        curveState: effectConfirmed ? curveState : `${curveState}-${projectionStage}`,
        text: effectConfirmed ? effectiveText : (requestObserved ? requestedText : plannedText),
        reasonCode: effectConfirmed ? reasonCode : `${reasonCode}_${projectionStage.toUpperCase()}`
    });
    const hasAction = value => action === value || status.includes(value);
    if (hasAction('CHARGE_BLOCK_WAIT')) {
        return stagedDisplay(
            'hidden-dv-hold',
            'STORAGE_CLASSICAL_CURVE_HIDDEN_DV_CHARGE_BLOCK',
            'DV: Laden gesperrt / Halten',
            'DV: Ladesperre angefordert',
            'DV: Ladesperre geplant'
        );
    }
    if (hasAction('ECONOMIC_EXPORT')) {
        return stagedDisplay(
            'hidden-dv-export',
            'STORAGE_CLASSICAL_CURVE_HIDDEN_DV_EXPORT',
            'DV-Export aktiv',
            'DV-Export angefordert',
            'DV-Export geplant'
        );
    }
    if (hasAction('HEADROOM_EXPORT')) {
        return stagedDisplay(
            'hidden-dv-headroom',
            'STORAGE_CLASSICAL_CURVE_HIDDEN_DV_HEADROOM',
            'DV: Speicherplatz schaffen',
            'DV: Speicherplatzfreigabe angefordert',
            'DV: Speicherplatzfreigabe geplant'
        );
    }
    if (hasAction('PV_STORE') || hasAction('DV_CURVE_CHARGE')) {
        return stagedDisplay(
            'hidden-dv-store',
            'STORAGE_CLASSICAL_CURVE_HIDDEN_DV_STORE',
            'DV: PV speichern',
            'DV: PV-Speicherung angefordert',
            'DV: PV-Speicherung geplant'
        );
    }
    if (hasAction('GRID_CHARGE')) {
        return stagedDisplay(
            'hidden-dv-grid-charge',
            'STORAGE_CLASSICAL_CURVE_HIDDEN_DV_GRID_CHARGE',
            'DV: Netzladen',
            'DV: Netzladen angefordert',
            'DV: Netzladen geplant'
        );
    }
    if (effectivePlan.direct_marketing_active === true || status.startsWith('DIRECT_MARKETING_')) {
        return stagedDisplay(
            'hidden-dv',
            'STORAGE_CLASSICAL_CURVE_HIDDEN_DV',
            'DV-Regelung aktiv',
            'DV-Regelung angefordert',
            'DV-Regelung geplant'
        );
    }
    return {curveState: 'hidden', text: 'SoC-Prognose bewusst ausgeblendet', reasonCode: 'STORAGE_CLASSICAL_CURVE_HIDDEN'};
}

function storageSparklineUnavailableDisplay(data = {}, series = {}) {
    if (series.state === 'projection_hidden') {
        return storageSparklineProjectionDisplay(data)
            || {curveState: 'hidden', text: 'SoC-Prognose bewusst ausgeblendet'};
    }
    const states = {
        missing_plan: {curveState: 'missing', text: 'Kein Speicherplan'},
        missing_forecast: {curveState: 'missing', text: 'Keine SoC-Prognose'},
        forecast_too_short: {curveState: 'incomplete', text: 'SoC-Prognose noch unvollständig'},
        invalid_point: {curveState: 'invalid', text: 'SoC-Prognose unvollständig'},
        missing_plan_binding: {curveState: 'invalid', text: 'SoC-Prognose ohne Planbindung'},
        invalid_plan_binding: {curveState: 'invalid', text: 'SoC-Prognose mit ungültiger Planbindung'},
        plan_mismatch: {curveState: 'mismatch', text: 'Planrevision passt nicht'},
        missing_slot_id: {curveState: 'invalid', text: 'SoC-Prognose ohne Slot-Bindung'},
        invalid_timestamp: {curveState: 'invalid', text: 'SoC-Prognose mit ungültiger Zeit'},
        invalid_soc: {curveState: 'invalid', text: 'SoC-Prognose mit ungültigem SoC'},
        invalid_forecast: {curveState: 'invalid', text: 'SoC-Prognose ungültig'}
    };
    return states[series.state] || states.missing_forecast;
}

function storageSparklineSvgPoints(points, domain, width = 240, height = 44) {
    if (!Array.isArray(points) || points.length < 2) return '';
    const minTs = domain.minTs;
    const maxTs = domain.maxTs;
    const minSoc = domain.minSoc;
    const maxSoc = domain.maxSoc;
    if (!(maxTs > minTs) || !(maxSoc > minSoc)) return '';
    return points.map(point => {
        const x = 2 + ((point.ts - minTs) / (maxTs - minTs)) * (width - 4);
        const y = height - 3 - ((point.soc - minSoc) / (maxSoc - minSoc)) * (height - 8);
        return `${x.toFixed(2)},${y.toFixed(2)}`;
    }).join(' ');
}

function renderStorageCurveSparkline(data = {}) {
    const wrap = document.getElementById('stat-regler-sparkline');
    const forecastLine = document.getElementById('stat-regler-sparkline-forecast');
    const targetLine = document.getElementById('stat-regler-sparkline-target');
    const state = document.getElementById('stat-regler-sparkline-state');
    if (!wrap || !forecastLine || !targetLine || !state) return;
    const neutralAriaLabel = 'Ladekurvenvorschau';
    const sourceLabel = 'Standard-SoC-Prognose';
    const sourceDetail = 'Basis-SoC ohne DV-, Pre-Dump- und Netzladewirkung';
    const clear = (curveState, text) => {
        forecastLine.setAttribute('points', '');
        targetLine.setAttribute('points', '');
        wrap.dataset.state = curveState;
        wrap.dataset.runtimeState = curveState;
        wrap.dataset.curveSource = '';
        wrap.removeAttribute('title');
        wrap.setAttribute('aria-label', text ? `${neutralAriaLabel}: ${text}` : neutralAriaLabel);
        state.textContent = text || sourceLabel;
    };
    const meta = data.storage_plan_meta && typeof data.storage_plan_meta === 'object' ? data.storage_plan_meta : {};
    let planTs = Number(meta.generated_at_ts ?? meta.ts ?? data.storage_plan_ts ?? 0);
    if (planTs > 1e12) planTs /= 1000;
    const stale = data.storage_plan_stale === true || meta.stale === true || meta.fresh === false
        || (planTs > 0 && Date.now() / 1000 - planTs > 1800);
    if (stale) {
        clear('stale', 'SoC-Prognose veraltet');
        return;
    }
    const series = storageSparklineSeries(data);
    if (series.state !== 'bound') {
        const unavailable = storageSparklineUnavailableDisplay(data, series);
        clear(unavailable.curveState, unavailable.text);
        return;
    }
    const domainPoints = series.forecast.concat(series.target);
    const minTs = series.forecast[0].ts;
    const maxTs = series.forecast[series.forecast.length - 1].ts;
    let minSoc = Math.min(...domainPoints.map(point => point.soc));
    let maxSoc = Math.max(...domainPoints.map(point => point.soc));
    const minSpan = 12;
    if (maxSoc - minSoc < minSpan) {
        const center = (minSoc + maxSoc) / 2;
        minSoc = Math.max(0, center - minSpan / 2);
        maxSoc = Math.min(100, minSoc + minSpan);
        minSoc = Math.max(0, maxSoc - minSpan);
    } else {
        const pad = Math.min(4, (maxSoc - minSoc) * 0.08);
        minSoc = Math.max(0, minSoc - pad);
        maxSoc = Math.min(100, maxSoc + pad);
    }
    const domain = {minTs, maxTs, minSoc, maxSoc};
    forecastLine.setAttribute('points', storageSparklineSvgPoints(series.forecast, domain));
    targetLine.setAttribute('points', storageSparklineSvgPoints(
        series.target.filter(point => point.ts >= minTs && point.ts <= maxTs),
        domain
    ));
    wrap.dataset.state = 'fresh';
    wrap.dataset.curveSource = series.source;
    const runtime = storageDispatchRuntimeForDisplay(data);
    const runtimePresent = data.storage_dispatch_runtime && typeof data.storage_dispatch_runtime === 'object';
    if (runtime) {
        const commandsAllowed = runtime.selected === true
            && runtime.executable === true
            && runtime.commands_allowed === true;
        const budgetW = Math.max(0, Number(runtime.charge_budget_w) || 0, Number(runtime.export_budget_w) || 0);
        wrap.dataset.runtimeState = commandsAllowed ? 'allowed' : 'blocked';
        state.textContent = commandsAllowed ? `${sourceLabel} · aktiv` : `${sourceLabel} · gebunden`;
        const owner = typeof runtime.owner === 'string' && runtime.owner ? runtime.owner : 'unbekannt';
        const detail = commandsAllowed
            ? `Ausführung freigegeben${budgetW > 0 ? ` · Budget ${Math.round(budgetW).toLocaleString('de-DE')} W` : ''}`
            : `Ausführung gesperrt${runtime.block_reason_code ? ` · ${runtime.block_reason_code}` : ''}`;
        wrap.title = `${sourceDetail} aus ${series.planId} · Sollkurve gestrichelt · Owner ${owner} · ${detail}`;
        wrap.setAttribute('aria-label', `${sourceLabel}. ${sourceDetail}. Owner ${owner}. ${detail}`);
    } else if (runtimePresent) {
        wrap.dataset.runtimeState = 'mismatch';
        wrap.title = `${sourceDetail} aus ${series.planId}; Runtime nicht an diese Planrevision gebunden.`;
        wrap.setAttribute('aria-label', `${sourceLabel}. ${sourceDetail}. Runtime nicht an diese Planrevision gebunden.`);
        state.textContent = `${sourceLabel} · Runtime ungebunden`;
    } else {
        wrap.dataset.runtimeState = 'missing';
        wrap.title = `${sourceDetail} aus ${series.planId}; Sollkurve gestrichelt.`;
        wrap.setAttribute('aria-label', `${sourceLabel}. ${sourceDetail}. Sollkurve gestrichelt.`);
        state.textContent = sourceLabel;
    }
}

async function postDirectMarketingDashboardAction(fields) {
    const form = new FormData();
    Object.entries(fields || {}).forEach(([key, value]) => form.append(key, String(value)));
    form.append('csrf_token', String(window.E3DC_CSRF_TOKEN || ''));
    const response = await fetch('index.php', {
        method: 'POST',
        headers: {'X-Requested-With': 'XMLHttpRequest'},
        body: form
    });
    let payload = null;
    try { payload = await response.json(); } catch (error) { payload = null; }
    if (!response.ok || !payload || payload.success !== true) {
        throw new Error(payload?.error || `HTTP ${response.status}`);
    }
    return payload;
}

async function toggleDirectMarketingAuxInverterShellyLock(button) {
    if (!button || button.disabled) return;
    const state = getDirectMarketingAuxInverterShellyState(window._storageLiveData || {}) || {};
    const currentlyLocked = state.manual_locked === true || button.getAttribute('aria-pressed') === 'true';
    const nextLocked = !currentlyLocked;
    button.disabled = true;
    try {
        await postDirectMarketingDashboardAction({
            action: 'set_direct_marketing_aux_inverter_shelly_lock',
            locked: nextLocked ? '1' : '0'
        });
        window._directMarketingAuxInverterShellyState = {
            ...state,
            manual_locked: nextLocked,
            status: nextLocked ? 'manual_locked' : (state.desired_wr_on === false ? 'wr_off' : 'wr_on')
        };
        updateEnergyFlowAuxInverterShellyBadge(window._storageLiveData || {});
    } catch (error) {
        window.alert(`Zusatz-WR-Sperre konnte nicht gespeichert werden: ${error.message}`);
    } finally {
        button.disabled = false;
    }
}

function initMobileStorageCurveTrigger() {
    const strip = document.getElementById('m-storage-strip');
    if (!strip) return;
    const openCurve = () => {
        if (typeof showStorageCurveModal === 'function') showStorageCurveModal();
    };
    strip.addEventListener('click', openCurve);
    strip.addEventListener('keydown', event => {
        if (event.key !== 'Enter' && event.key !== ' ') return;
        event.preventDefault();
        openCurve();
    });
}

function formatRingPower(w, compact = false) {
    w = Math.abs(parseFloat(w) || 0);
    if (w >= 1000) {
        const digits = compact ? 1 : 1;
        return (w / 1000).toLocaleString('de-DE', {minimumFractionDigits: digits, maximumFractionDigits: digits}) + ' kW';
    }
    return Math.round(w).toLocaleString('de-DE') + ' W';
}

function setRingText(id, text) {
    const el = document.getElementById(id);
    if (el) el.textContent = text;
}

function setRingSegment(id, value, maxValue, maxLength, dashOffset) {
    const el = document.getElementById(id);
    if (!el) return;
    const raw = Math.abs(parseFloat(value) || 0);
    if (raw < 20 || maxValue <= 0) {
        el.style.strokeDasharray = '0 100';
        el.style.opacity = '0';
        return;
    }
    const length = Math.max(3, Math.min(maxLength, (raw / maxValue) * maxLength));
    el.style.strokeDasharray = length.toFixed(1) + ' ' + (100 - length).toFixed(1);
    el.style.strokeDashoffset = String(dashOffset);
    el.style.opacity = '0.96';
}

function setRingPathSegment(id, length, offset) {
    const el = document.getElementById(id);
    if (!el) return;
    if (!Number.isFinite(length) || length <= 0.1) {
        el.style.strokeDasharray = '0 100';
        el.style.strokeDashoffset = '0';
        el.style.opacity = '0';
        return;
    }
    const len = Math.max(2.2, Math.min(96, length));
    el.style.strokeDasharray = len.toFixed(1) + ' ' + (100 - len).toFixed(1);
    el.style.strokeDashoffset = String(-(offset || 0));
    el.style.opacity = '0.96';
}

function setRingStackSegments(segments, totalValue, maxLength = 86, gap = 1.4) {
    const active = segments
        .map(s => ({...s, value: Math.max(0, Math.abs(parseFloat(s.value) || 0))}))
        .filter(s => s.value >= 20);

    segments.forEach(s => setRingPathSegment(s.id, 0, 0));
    if (!active.length) return;

    const total = Math.max(1000, totalValue || active.reduce((sum, s) => sum + s.value, 0));
    const available = Math.max(18, maxLength - Math.max(0, active.length - 1) * gap);
    let offset = 0;
    active.forEach((s, idx) => {
        const remaining = Math.max(0, maxLength - offset);
        const proportional = (s.value / total) * available;
        const length = idx === active.length - 1
            ? Math.min(remaining, Math.max(2.2, proportional))
            : Math.min(remaining, Math.max(2.2, proportional));
        setRingPathSegment(s.id, length, offset);
        offset += length + gap;
    });
}

function setRingRow(id, text, visible = true, muted = false) {
    const row = document.getElementById(id);
    if (!row) return;
    row.style.display = visible ? '' : 'none';
    row.classList.toggle('muted-zero', !!muted);
    const span = row.querySelector('span');
    if (span) span.textContent = text;
}

function placeRingRow(rowId, listId) {
    const row = document.getElementById(rowId);
    const list = document.getElementById(listId);
    if (row && list && row.parentElement !== list) list.appendChild(row);
}

function pickMobileRingVehicle(data) {
    const vehicles = Array.isArray(data && data.vehicles) ? data.vehicles : [];
    if (vehicles.length === 0) return null;
    const plugged = vehicles.find(v => v && (v.is_plugged_in === true || v.is_plugged_in === 1 || v.is_charging === true));
    const withSoc = vehicles.find(v => v && vehicleSocKnown(v));
    return plugged || withSoc || vehicles[0];
}

function updateMobileRingFlow(data, values) {
    if (!document.getElementById('m-flow-ring-view') || !data) return;
    values = values || {};

    const pv = Math.max(0, parseFloat(values.pv || 0));
    const bat = parseFloat(values.bat || 0);
    const grid = parseFloat(values.grid || 0);
    const home = Math.max(0, parseFloat(values.home || 0));
    const wb = Math.max(0, parseFloat(values.wb || 0));
    const wb2 = Math.max(0, parseFloat(values.wb2 || 0));
    const wp = Math.max(0, parseFloat(values.wp || 0));
    const hs = Math.max(0, parseFloat(values.hs || 0));
    const climate = Math.max(0, parseFloat(values.climate ?? data.climate_power_w ?? data.climate ?? 0) || 0);
    const consumption = Math.max(0, home + wb + wb2 + wp + hs + climate);
    const exportW = grid < -50 ? Math.abs(grid) : 0;
    const importW = grid > 50 ? grid : 0;
    const batCharge = bat > 50 ? bat : 0;
    const batDischarge = bat < -50 ? Math.abs(bat) : 0;
    const wpTotal = wp + hs;
    const inputTotal = pv + batDischarge + importW;
    const outputTotal = home + wpTotal + climate + wb + wb2 + batCharge + exportW;
    const storageSoc = Number.isFinite(parseFloat(data.soc)) ? Math.max(0, Math.min(100, parseFloat(data.soc))) : null;

    setRingText('m-ring-pv-text', formatRingPower(pv));
    const pvDetail = document.getElementById('m-ring-pv-detail');
    if (pvDetail) {
        const detailHtml = livePvSourceSplitHtml(data, ' · ', true);
        const detailTitle = livePvSourceSplitText(data, '\n', false);
        if (detailHtml) {
            pvDetail.innerHTML = detailHtml;
            pvDetail.style.display = 'block';
            pvDetail.title = detailTitle;
        } else {
            pvDetail.innerHTML = '';
            pvDetail.style.display = 'none';
            pvDetail.title = '';
        }
    }
    setRingText('m-ring-consumption-text', 'Verbrauch: ' + formatRingPower(consumption));
    setRingText('m-ring-home-text', formatRingPower(home));
    setRingText('m-ring-soc', storageSoc !== null ? 'Speicher ' + Math.round(storageSoc) + '%' : 'Speicher --%');
    setRingPathSegment('m-ring-arc-soc', storageSoc !== null ? storageSoc : 0, 0);

    const gridRow = document.getElementById('m-ring-grid-row');
    const gridIcon = document.getElementById('m-ring-grid-icon');
    if (exportW > 20) {
        placeRingRow('m-ring-grid-row', 'm-ring-output-list');
        if (gridRow) { gridRow.classList.add('export'); gridRow.classList.remove('import'); }
        if (gridIcon) gridIcon.className = 'fas fa-arrow-right';
        setRingText('m-ring-grid-text', 'Einspeisung ' + formatRingPower(exportW));
        if (gridRow) gridRow.style.display = '';
    } else if (importW > 20) {
        placeRingRow('m-ring-grid-row', 'm-ring-input-list');
        if (gridRow) { gridRow.classList.add('import'); gridRow.classList.remove('export'); }
        if (gridIcon) gridIcon.className = 'fas fa-plug';
        setRingText('m-ring-grid-text', 'Bezug ' + formatRingPower(importW));
        if (gridRow) gridRow.style.display = '';
    } else {
        if (gridRow) { gridRow.classList.remove('export', 'import'); }
        if (gridIcon) gridIcon.className = 'fas fa-arrow-right';
        setRingText('m-ring-grid-text', 'Netz 0 W');
        if (gridRow) gridRow.style.display = 'none';
    }

    const batRow = document.getElementById('m-ring-bat-row');
    const batIcon = document.getElementById('m-ring-bat-icon');
    if (bat > 50) {
        placeRingRow('m-ring-bat-row', 'm-ring-output-list');
        if (batRow) { batRow.classList.add('charge'); batRow.classList.remove('discharge'); }
        if (batIcon) batIcon.className = 'fas fa-arrow-right-to-bracket';
        setRingRow('m-ring-bat-row', 'in Speicher ' + formatRingPower(bat), true);
    } else if (bat < -50) {
        placeRingRow('m-ring-bat-row', 'm-ring-input-list');
        if (batRow) { batRow.classList.add('discharge'); batRow.classList.remove('charge'); }
        if (batIcon) batIcon.className = 'fas fa-arrow-right-from-bracket';
        setRingRow('m-ring-bat-row', 'aus Speicher ' + formatRingPower(bat), true);
    } else {
        if (batRow) { batRow.classList.remove('charge', 'discharge'); }
        if (batIcon) batIcon.className = 'fas fa-car-battery';
        setRingRow('m-ring-bat-row', 'Speicher ruht', false, true);
    }
    setRingRow('m-ring-home-row', 'Haus ' + formatRingPower(home), home > 20);
    setRingRow('m-ring-wp-row', 'WP ' + formatRingPower(wpTotal), wpTotal > 20);
    setRingRow('m-ring-climate-row', 'Klima ' + formatRingPower(climate), climate > 20);
    setRingRow('m-ring-wb-row', 'WB1 ' + formatRingPower(wb), wb > 50);
    setRingRow('m-ring-wb2-row', 'WB2 ' + formatRingPower(wb2), wb2 > 50);
    setRingText('m-ring-vehicle', '');

    setRingStackSegments([
        {id: 'm-ring-arc-pv', value: pv},
        {id: 'm-ring-arc-bat-out', value: batDischarge},
        {id: 'm-ring-arc-grid-import', value: importW}
    ], inputTotal, 88, 1.4);
    setRingStackSegments([
        {id: 'm-ring-arc-house', value: home},
        {id: 'm-ring-arc-wp', value: wpTotal},
        {id: 'm-ring-arc-climate', value: climate},
        {id: 'm-ring-arc-wb', value: wb},
        {id: 'm-ring-arc-wb2', value: wb2},
        {id: 'm-ring-arc-bat-in', value: batCharge},
        {id: 'm-ring-arc-grid-export', value: exportW}
    ], outputTotal, 88, 1.4);
}

function mobileStoragePct(value, digits = 1) {
    const n = parseFloat(value);
    if (!Number.isFinite(n)) return '--';
    return n.toFixed(digits) + '%';
}

function mobileStoragePower(value) {
    return formatWatts(value).replace(/<[^>]*>?/gm, '');
}

function mobileStorageKwhFromWh(value) {
    const n = parseFloat(value);
    if (!Number.isFinite(n)) return '-- kWh';
    return (n / 1000).toFixed(1) + ' kWh';
}

function mobileStorageAnchorText(anchor) {
    if (!anchor) return '--';
    const time = anchor.t || mobileStorageTime(anchor.ts);
    const soc = mobileStoragePct(anchor.soc, 0);
    return time !== '--' ? time + ' ' + soc : soc;
}

function mobileStorageTime(ts) {
    const raw = parseFloat(ts);
    if (!Number.isFinite(raw) || raw <= 0) return '--';
    const ms = raw > 100000000000 ? raw : raw * 1000;
    return new Date(ms).toLocaleTimeString('de-DE', {hour: '2-digit', minute: '2-digit'});
}

function directMarketingTimestampMs(value) {
    const raw = parseFloat(value);
    if (!Number.isFinite(raw) || raw <= 0) return 0;
    return raw > 100000000000 ? raw : raw * 1000;
}

function directMarketingPlanWindowTimes(windowEntry = {}, nowMs = Date.now()) {
    const formatter = new Intl.DateTimeFormat('de-DE', {
        timeZone: 'Europe/Berlin',
        year: 'numeric',
        month: '2-digit',
        day: '2-digit',
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23'
    });
    const localParts = value => {
        const ms = directMarketingTimestampMs(value);
        if (!ms) return null;
        const parts = Object.fromEntries(
            formatter.formatToParts(new Date(ms))
                .filter(part => part.type !== 'literal')
                .map(part => [part.type, part.value])
        );
        const year = parseInt(parts.year, 10);
        const month = parseInt(parts.month, 10);
        const day = parseInt(parts.day, 10);
        const hour = parseInt(parts.hour, 10) % 24;
        const minute = parseInt(parts.minute, 10);
        if (![year, month, day, hour, minute].every(Number.isFinite)) return null;
        return {
            year,
            month,
            day,
            dayIndex: Math.floor(Date.UTC(year, month - 1, day) / 86400000),
            time: String(hour).padStart(2, '0') + ':' + String(minute).padStart(2, '0')
        };
    };
    const startParts = localParts(windowEntry.start_ts);
    const endParts = localParts(windowEntry.end_ts);
    const nowParts = localParts(nowMs);
    const fallbackStart = windowEntry.start_t || mobileStorageTime(windowEntry.start_ts);
    const fallbackEnd = windowEntry.end_t || mobileStorageTime(windowEntry.end_ts);
    if (!startParts || !nowParts) return {start: fallbackStart, end: fallbackEnd};

    const prefixForDay = parts => {
        const delta = parts.dayIndex - nowParts.dayIndex;
        if (delta === 0) return '';
        if (delta === 1) return 'Morgen, ';
        return String(parts.day).padStart(2, '0') + '.'
            + String(parts.month).padStart(2, '0') + '., ';
    };
    const start = prefixForDay(startParts) + startParts.time;
    const end = !endParts
        ? fallbackEnd
        : (endParts.dayIndex === startParts.dayIndex ? '' : prefixForDay(endParts)) + endParts.time;
    return {start, end};
}

function getDirectMarketingPlan(data) {
    if (data && data.direct_marketing && typeof data.direct_marketing === 'object') return data.direct_marketing;
    if (data && data.storage_plan_meta && data.storage_plan_meta.direct_marketing && typeof data.storage_plan_meta.direct_marketing === 'object') {
        return data.storage_plan_meta.direct_marketing;
    }
    if (window._directMarketingPlan && typeof window._directMarketingPlan === 'object') return window._directMarketingPlan;
    return null;
}

function getDirectMarketingMonitor(data) {
    if (data && data.direct_marketing_monitor && typeof data.direct_marketing_monitor === 'object') return data.direct_marketing_monitor;
    if (data && data.storage_plan_meta && data.storage_plan_meta.direct_marketing_monitor && typeof data.storage_plan_meta.direct_marketing_monitor === 'object') {
        return data.storage_plan_meta.direct_marketing_monitor;
    }
    if (window._directMarketingMonitor && typeof window._directMarketingMonitor === 'object') return window._directMarketingMonitor;
    return null;
}

function getDirectMarketingDailyReport(data) {
    if (data && data.direct_marketing_daily_report && typeof data.direct_marketing_daily_report === 'object') return data.direct_marketing_daily_report;
    if (data && data.storage_plan_meta && data.storage_plan_meta.direct_marketing_daily_report && typeof data.storage_plan_meta.direct_marketing_daily_report === 'object') {
        return data.storage_plan_meta.direct_marketing_daily_report;
    }
    if (window._directMarketingDailyReport && typeof window._directMarketingDailyReport === 'object') return window._directMarketingDailyReport;
    return null;
}

function getDirectMarketingAuxInverterShellyState(data) {
    if (data && data.direct_marketing_aux_inverter_shelly && typeof data.direct_marketing_aux_inverter_shelly === 'object') {
        return data.direct_marketing_aux_inverter_shelly;
    }
    if (data && data.storage_plan_meta && data.storage_plan_meta.direct_marketing_aux_inverter_shelly && typeof data.storage_plan_meta.direct_marketing_aux_inverter_shelly === 'object') {
        return data.storage_plan_meta.direct_marketing_aux_inverter_shelly;
    }
    if (window._directMarketingAuxInverterShellyState && typeof window._directMarketingAuxInverterShellyState === 'object') {
        return window._directMarketingAuxInverterShellyState;
    }
    return null;
}

function getMarketValueSolar(data) {
    if (data && data.market_value_solar && typeof data.market_value_solar === 'object') return data.market_value_solar;
    if (data && data.storage_plan_meta && data.storage_plan_meta.market_value_solar && typeof data.storage_plan_meta.market_value_solar === 'object') {
        return data.storage_plan_meta.market_value_solar;
    }
    if (window._marketValueSolar && typeof window._marketValueSolar === 'object') return window._marketValueSolar;
    return null;
}

function marketValueSolarStatusLabel(status) {
    const cached = String(status || '').startsWith('cached_');
    const normalized = String(status || '').replace(/^cached_/, '');
    const labels = {
        preliminary: 'vorläufig',
        disabled: 'deaktiviert',
        missing_credentials: 'Zugangsdaten fehlen',
        unavailable: 'nicht verfügbar',
        no_solar_data: 'keine Solar-Hochrechnung',
        no_price_data: 'keine Preise',
        no_price_matches: 'keine Preiszuordnung'
    };
    const label = labels[normalized] || normalized.replace(/_/g, ' ');
    return cached && label ? 'Cache: ' + label : label;
}

function formatMarketValueSolarSummary(report, compact = false) {
    if (!report || typeof report !== 'object') return '';
    const value = parseFloat(report.solar_weighted_market_value_ct);
    const status = marketValueSolarStatusLabel(report.status);
    const prefix = compact ? 'MW Solar' : 'Marktwert Solar';
    if (Number.isFinite(value)) {
        const formatted = value.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' ct/kWh';
        return `${prefix}: ${formatted}${status ? ' (' + status + ')' : ''}`;
    }
    if (status) return `${prefix}: ${status}`;
    return '';
}

function formatMarketValueSolarTitle(report) {
    if (!report || typeof report !== 'object') return '';
    const lines = [formatMarketValueSolarSummary(report, false)];
    if (report.month) lines.push('Monat: ' + report.month);
    if (report.method) lines.push('Methode: ' + report.method);
    if (report.control_effect) lines.push('Regelwirkung: ' + report.control_effect);
    if (report.enabled === false) lines.push('Monitor ist deaktiviert.');
    if (report.status === 'missing_credentials') lines.push('Netztransparenz Client-ID/Secret fehlen.');
    const slots = report.slots && typeof report.slots === 'object' ? report.slots : {};
    if (slots.matched != null || slots.solar != null) lines.push('Slots: ' + (slots.matched ?? 0) + '/' + (slots.solar ?? 0));
    const quality = report.quality && typeof report.quality === 'object' ? report.quality : {};
    if (quality.completeness_pct != null) lines.push('Abdeckung: ' + parseFloat(quality.completeness_pct).toLocaleString('de-DE', {maximumFractionDigits: 1}) + ' %');
    if (Array.isArray(report.warnings) && report.warnings.length) lines.push('Hinweise: ' + report.warnings.join(', '));
    return lines.filter(Boolean).join('\n');
}

function directMarketingNormalizeMode(mode) {
    const normalized = String(mode || 'off').trim().toLowerCase().replace(/[-\s]+/g, '_');
    if (normalized === 'eco+' || normalized === 'ecoplus') return 'eco_plus';
    return normalized;
}

function directMarketingExplicitlyDisabled(plan, monitor) {
    const monitorState = String((monitor && monitor.state) || '').toLowerCase();
    const monitorMode = directMarketingNormalizeMode(monitor && monitor.mode);
    const planMode = directMarketingNormalizeMode(plan && plan.mode);
    const planReason = String((plan && plan.reason) || '').toLowerCase();
    const monitorHasMode = Boolean(monitor && Object.prototype.hasOwnProperty.call(monitor, 'mode'));
    const planHasMode = Boolean(plan && Object.prototype.hasOwnProperty.call(plan, 'mode'));
    const blockedReasons = Array.isArray(plan && plan.blocked_reasons)
        ? plan.blocked_reasons.map(reason => String(reason || '').toLowerCase())
        : [];
    return Boolean(
        (monitor && monitor.enabled === false)
        || monitorState === 'off'
        || (monitorHasMode && monitorMode === 'off')
        || (planHasMode && planMode === 'off')
        || planReason === 'disabled'
        || blockedReasons.includes('disabled')
    );
}

function isDirectMarketingVisible(data, plan, monitor) {
    if (directMarketingExplicitlyDisabled(plan, monitor)) return false;
    if (data && data.direct_marketing_active === true) return true;
    if (monitor && monitor.enabled === true) return true;
    const mode = directMarketingNormalizeMode((monitor && monitor.mode) || (plan && plan.mode));
    return ['safe', 'eco', 'eco_plus', 'arbitrage'].includes(mode);
}

function directMarketingModeLabel(mode) {
    const normalized = directMarketingNormalizeMode(mode);
    if (normalized === 'safe') return 'Safe';
    if (normalized === 'eco') return 'Eco';
    if (normalized === 'eco_plus') return 'Eco+';
    if (normalized === 'arbitrage') return 'Arbitrage';
    return 'aus';
}

function directMarketingIsHoldAction(action) {
    return [
        'eco_plus_negative_headroom_hold',
        'keep_headroom',
        'arbitrage_keep_headroom',
        'policy_headroom_hold',
        'policy_charge_block_wait',
        'direct_marketing_charge_block_wait',
        'charge_block_wait'
    ].includes(String(action || '').toLowerCase());
}

function directMarketingRuntimePhysicalAction(data = {}) {
    const runtime = storageDispatchRuntimeForDisplay(data);
    if (!runtime || String(runtime.owner || '').toLowerCase() !== 'storage_manager') return null;
    const invariant = runtime.plan_runtime_selection_invariant;
    const phase5 = runtime.phase5 && typeof runtime.phase5 === 'object' ? runtime.phase5 : {};
    const requested = runtime.requested && typeof runtime.requested === 'object' ? runtime.requested : {};
    if (
        !invariant
        || invariant.valid !== true
        || phase5.schema_version !== 'storage_dispatch_phase5_v1'
        || runtime.selected !== true
        || runtime.executable !== true
        || runtime.commands_allowed !== true
        || requested.dispatch_authorized !== true
        || (requested.confirmed !== true && !(requested.hardware_effect === true && requested.issued === true))
        || requested.hardware_effect !== true
    ) {
        return null;
    }

    const runtimeSlot = (Array.isArray(data.storage_sim_curve) ? data.storage_sim_curve : []).find(point => (
        point && typeof point === 'object'
        && point.plan_id === runtime.plan_id
        && point.slot_id === runtime.slot_id
    ));
    const runtimeStartMs = directMarketingTimestampMs(runtimeSlot && runtimeSlot.ts);
    const runtimeEndMs = directMarketingTimestampMs(runtimeSlot && runtimeSlot.end_ts);

    const actualAction = String(runtime.actual_manager_action || '').toUpperCase();
    const phase5Action = String(phase5.selected_action || '').toUpperCase();
    const candidateAction = String(runtime.candidate && runtime.candidate.action || '').toUpperCase();
    const chargeBlockActions = new Set([
        'CHARGE_BLOCK_WAIT',
        'DIRECT_MARKETING_CHARGE_BLOCK_WAIT',
        'DIRECT_MARKETING_CHARGE_BLOCK_WAIT_SAFE_FALLBACK'
    ]);
    if (
        chargeBlockActions.has(actualAction)
        && chargeBlockActions.has(phase5Action)
    ) {
        const request = phase5.request && typeof phase5.request === 'object' ? phase5.request : {};
        const target = request.target && typeof request.target === 'object' ? request.target : {};
        const readback = runtime.readback && typeof runtime.readback === 'object' ? runtime.readback : {};
        const readbackValues = readback.values && typeof readback.values === 'object' ? readback.values : {};
        const translation = phase5.translation && typeof phase5.translation === 'object'
            ? phase5.translation
            : {};
        const boundedReadback = request.bounded_zero_readback && typeof request.bounded_zero_readback === 'object'
            ? request.bounded_zero_readback
            : {};
        const boundedZeroW = Math.max(
            0,
            parseFloat(
                translation.bounded_zero_readback_max_w
                ?? boundedReadback.accepted_readback_max_w
                ?? 0
            ) || 0
        );
        const requestedZeroCharge = target.limits_used === true
            && parseFloat(target.max_charge_w) === 0;
        const confirmedZeroCharge = readback.confirmed === true
            && readback.fresh === true
            && readbackValues.limits_used === true
            && Number.isFinite(parseFloat(readbackValues.max_charge_w))
            && parseFloat(readbackValues.max_charge_w) >= 0
            && parseFloat(readbackValues.max_charge_w) <= boundedZeroW;
        if (!requestedZeroCharge || !confirmedZeroCharge) return null;
        const chargeBlock = phase5.charge_block_contract && typeof phase5.charge_block_contract === 'object'
            ? phase5.charge_block_contract
            : {};
        const sourceWindow = chargeBlock.source_window && typeof chargeBlock.source_window === 'object'
            ? chargeBlock.source_window
            : {};
        const schema = String(chargeBlock.schema || '');
        const allowedSchemas = new Set([
            'phase5_direct_marketing_default_charge_guard_v1',
            'phase5_charge_block_wait_contract_v1',
            'phase5_pv_store_wait_charge_block_v1',
            'phase5_direct_marketing_restrictive_fallback_v1'
        ]);
        const canonicalAction = String(
            chargeBlock.canonical_direct_marketing_action || sourceWindow.action || candidateAction
        ).toUpperCase();
        const defaultPvStoreGuard = schema === 'phase5_direct_marketing_default_charge_guard_v1';
        const identityValid = chargeBlock.valid === true
            && allowedSchemas.has(schema)
            && (!chargeBlock.plan_id || chargeBlock.plan_id === runtime.plan_id)
            && (!chargeBlock.slot_id || chargeBlock.slot_id === runtime.slot_id)
            && (!sourceWindow.slot_id || sourceWindow.slot_id === runtime.slot_id)
            && (!defaultPvStoreGuard || (
                chargeBlock.charge_authorized === false
                && candidateAction === 'PV_STORE'
                && canonicalAction === 'PV_STORE'
            ));
        const sourceStartMs = directMarketingTimestampMs(sourceWindow.start_ts_ms || sourceWindow.start_ts);
        const sourceEndMs = directMarketingTimestampMs(sourceWindow.end_ts_ms || sourceWindow.end_ts);
        const sourceBoundsValid = !sourceStartMs && !sourceEndMs
            ? true
            : Boolean(
                sourceStartMs > 0
                && sourceEndMs > sourceStartMs
                && runtimeStartMs === sourceStartMs
                && runtimeEndMs === sourceEndMs
            );
        if (!identityValid || !sourceBoundsValid) return null;
        return {
            action: 'charge_block_wait',
            rawAction: phase5Action,
            holdActive: true,
            hardwareEffect: true,
            planId: runtime.plan_id,
            slotId: runtime.slot_id,
            startTs: sourceStartMs || runtimeStartMs,
            endTs: sourceEndMs || runtimeEndMs
        };
    }

    const displayActions = {
        PV_STORE: 'eco_plus_store_pv_candidate',
        DV_CURVE_CHARGE: 'eco_plus_curve_charge_candidate',
        ECONOMIC_EXPORT: 'eco_plus_export_candidate',
        HEADROOM_EXPORT: 'policy_headroom_export'
    };
    const action = displayActions[actualAction] || '';
    return action ? {
        action,
        rawAction: actualAction,
        holdActive: false,
        hardwareEffect: true,
        planId: runtime.plan_id,
        slotId: runtime.slot_id,
        startTs: runtimeStartMs,
        endTs: runtimeEndMs
    } : null;
}

function directMarketingActionLabel(action) {
    const normalized = String(action || '').toLowerCase();
    const labels = {
        negative_price_market_window: 'Marktfenster ohne eigene Speicheraktion',
        keep_headroom: 'Speicherplatz freihalten',
        safe_house_supply: 'Haus versorgen',
        eco_plus_house_supply: 'Haus versorgen',
        eco_plus_negative_headroom_hold: 'Speicherplatz halten',
        eco_plus_store_pv_candidate: 'PV speichern',
        eco_plus_curve_charge_candidate: 'Kurvenladung im E3DC-AUTO-Rahmen',
        eco_plus_export_candidate: 'Verkaufskandidat',
        arbitrage_keep_headroom: 'Speicherplatz freihalten',
        arbitrage_grid_charge_candidate: 'Netzladekandidat',
        arbitrage_export_candidate: 'Verkaufskandidat',
        policy_headroom_hold: 'Speicherplatz halten',
        policy_headroom_export: 'Speicherplatz schaffen',
        policy_charge_block_wait: 'Speicherplatz halten',
        direct_marketing_charge_block_wait: 'Speicherplatz halten',
        charge_block_wait: 'Speicherplatz halten',
        policy_force_charge_pv: 'PV speichern',
        policy_force_export: 'Hochpreisverkauf',
        policy_hold: 'Energie halten',
        policy_normal: 'E3DC-Freilauf'
    };
    return labels[normalized] || 'Unbekannte Aktion';
}

function directMarketingWindowText(windowEntry) {
    if (!windowEntry || typeof windowEntry !== 'object') return '';
    const start = windowEntry.start_t || mobileStorageTime(windowEntry.start_ts);
    const end = windowEntry.end_t || mobileStorageTime(windowEntry.end_ts);
    const action = directMarketingActionLabel(windowEntry.action);
    const price = Number.isFinite(parseFloat(windowEntry.avg_market_ct))
        ? parseFloat(windowEntry.avg_market_ct).toFixed(2).replace('.', ',') + ' ct/kWh'
        : '';
    return [start + '-' + end, action, price].filter(Boolean).join(' ');
}

function directMarketingBlockerLabel(reason) {
    const normalized = String(reason || '').toLowerCase();
    const labels = {
        disabled: 'ausgeschaltet',
        config_disabled: 'Config aus',
        plan_missing: 'Plan fehlt',
        plan_inactive: 'Plan inaktiv',
        commands_not_allowed: 'Beobachtung: keine Befehle',
        plan_expired: 'Plan abgelaufen',
        no_timeline: 'keine Preisdaten',
        market_price_missing: 'Marktpreis fehlt',
        direct_market_price_missing: 'Direktmarktpreis fehlt',
        stale_market_price: 'Preisdaten veraltet',
        unsupported_price_resolution: 'Preisauflösung unpassend',
        unsupported_settlement_basis: 'Abrechnungsbasis nur zur Analyse',
        no_current_window: 'kein aktuelles Fenster',
        no_candidate_windows: 'keine Fenster',
        window_observe_only: 'nur Beobachtung',
        export_not_enabled: 'Einspeisung aus',
        grid_charge_not_enabled: 'Netzladen aus',
        arbitrage_experimental_disabled: 'Arbitrage gesperrt',
        profit_below_threshold: 'Marge zu klein',
        pv_shift_below_threshold_for_export: 'PV-Verschiebespread unter Freigabeschwelle',
        pv_shift_below_threshold_for_pv_store: 'PV-Speicher-Spread unter Freigabeschwelle',
        low_price_headroom: 'Speicherplatz vor PV-Speichern',
        negative_price_headroom: 'Speicherplatz vor Negativpreis',
        pv_store_headroom_prioritized: 'Speicherplatz für PV-Speichern priorisiert',
        negative_price_headroom_prioritized: 'Speicherplatz für Negativpreis priorisiert',
        export_energy_prioritized: 'Verkaufsenergie auf beste Fenster verteilt',
        pv_store_energy_budget_prioritized: 'PV-Ladeenergie auf beste Fenster verteilt',
        pv_store_not_enabled: 'PV-Speichern aus',
        pv_store_grid_import_guard: 'PV-Speichern: Netzbezug',
        pv_store_surplus_below_min: 'PV-Überschuss zu klein',
        pv_store_dc_surplus_below_min: 'E3DC-DC-PV zu klein',
        pv_store_charge_power_below_min: 'PV-Speichern unter Minimum',
        negative_headroom_disabled: 'Speicherplatz vor Negativpreis aus',
        negative_price_export_allowed: 'Negativpreis-Verkauf erlaubt',
        negative_headroom_already_available: 'Speicherplatz bereits frei',
        reserve_floor_reached: 'Reserve erreicht',
        wallbox_active: 'Wallbox aktiv',
        target_soc_reached: 'Ziel-SoC erreicht',
        house_connection_limited: 'Hausanschluss limitiert',
        grid_charge_power_below_min: 'Netzladen unter Minimum',
        export_power_below_min: 'Einspeisung unter Minimum',
        export_headroom_limited: 'Verkaufsreserve limitiert',
        controller_owner_mismatch: 'Regler-Konflikt',
        plan_owner_mismatch: 'Planregler ungültig',
        contract_version_mismatch: 'Vertrag-Version falsch',
        mode_mismatch: 'Modus passt nicht'
    };
    if (normalized.startsWith('blocked by margin:')) {
        const match = normalized.match(/expected\s+profit\s+(-?\d+(?:\.\d+)?)\s+eur\s*<\s*threshold\s+(-?\d+(?:\.\d+)?)\s+eur/);
        const threshold = match ? parseFloat(match[2]) : NaN;
        return Number.isFinite(threshold)
            ? 'Mindestgewinn knapp nicht erreicht: erwarteter Gewinn unter ' + threshold.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' €'
            : 'Mindestgewinn knapp nicht erreicht';
    }
    if (normalized === 'economic_export_window_profit_below_user_minimum') return 'Mindestgewinn knapp nicht erreicht';
    if (normalized === 'economic_export_policy_not_executable') return 'Verkaufsregel aktuell nicht ausführbar';
    if (normalized === 'policy_contract_blocked') return 'Ausführungsvertrag nicht freigegeben';
    if (normalized.startsWith('price_quality_blocked:')) return 'Preisqualität blockiert';
    if (normalized.startsWith('live_values_missing:')) return 'Live-Messwert fehlt';
    return labels[normalized] || ('Nicht übersetzter Diagnosecode: ' + String(reason));
}

function directMarketingProjectedReasonLabel(entry) {
    if (entry && typeof entry === 'object') {
        const code = String(entry.code || '');
        if (code.toLowerCase() === 'economic_export_window_profit_below_user_minimum') {
            const threshold = parseFloat(entry.minimum_profit_eur);
            return Number.isFinite(threshold)
                ? 'Mindestgewinn knapp nicht erreicht: erwarteter Gewinn unter ' + threshold.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' €'
                : 'Mindestgewinn knapp nicht erreicht';
        }
        return directMarketingBlockerLabel(code);
    }
    return directMarketingBlockerLabel(entry);
}

function directMarketingReasonGroups(monitor) {
    monitor = monitor && typeof monitor === 'object' ? monitor : {};
    const typed = ['global_gate_blockers', 'candidate_rejections', 'allocation_diagnostics']
        .some(key => Array.isArray(monitor[key]));
    if (typed) {
        return {
            blockers: (monitor.global_gate_blockers || []).map(directMarketingProjectedReasonLabel),
            candidates: (monitor.candidate_rejections || []).map(directMarketingProjectedReasonLabel),
            diagnostics: (monitor.allocation_diagnostics || []).map(directMarketingProjectedReasonLabel)
        };
    }
    const diagnostics = new Set(['export_energy_prioritized', 'pv_store_energy_budget_prioritized']);
    const groups = {blockers: [], candidates: [], diagnostics: []};
    (Array.isArray(monitor.blocked_reasons) ? monitor.blocked_reasons : []).forEach(reason => {
        const normalized = String(reason || '').toLowerCase();
        if (diagnostics.has(normalized)) groups.diagnostics.push(directMarketingBlockerLabel(reason));
        else if (normalized.startsWith('blocked by margin:') || normalized === 'profit_below_threshold') groups.candidates.push(directMarketingBlockerLabel(reason));
        else groups.blockers.push(directMarketingBlockerLabel(reason));
    });
    return groups;
}

function directMarketingSelectedExportKwh(plan) {
    plan = plan && typeof plan === 'object' ? plan : {};
    const decisions = [];
    if (plan.policy_decision && typeof plan.policy_decision === 'object') decisions.push(plan.policy_decision);
    if (Array.isArray(plan.policy_timeline)) decisions.push(...plan.policy_timeline);
    const seen = new Set();
    let energyWh = 0;
    decisions.forEach(decision => {
        if (!decision || typeof decision !== 'object' || decision.commands_allowed !== true) return;
        const selected = decision.selected_window && typeof decision.selected_window === 'object' ? decision.selected_window : null;
        const execution = decision.execution_window && typeof decision.execution_window === 'object' ? decision.execution_window : null;
        if (!selected || !execution) return;
        const action = String(selected.action || execution.action || '').toLowerCase();
        if (!['eco_plus_export_candidate', 'arbitrage_export_candidate'].includes(action)) return;
        const start = parseFloat(execution.start_ts || selected.start_ts || decision.start_ts || 0);
        const end = parseFloat(execution.end_ts || selected.end_ts || decision.end_ts || 0);
        const powerW = Math.max(0, parseFloat((decision.storage_budget || {}).export_budget_w ?? execution.max_power_w ?? selected.max_power_w ?? 0) || 0);
        const key = [decision.slot_id || '', selected.window_id || selected.market_window_id || '', start, end, powerW].join('|');
        if (seen.has(key) || !(end > start) || powerW <= 0) return;
        seen.add(key);
        energyWh += powerW * ((end - start) / 3600000);
    });
    return energyWh > 0 ? energyWh / 1000 : 0;
}

function directMarketingBlockerSummary(reasons, limit = 2) {
    if (!Array.isArray(reasons) || !reasons.length) return '';
    const labels = reasons
        .map(directMarketingBlockerLabel)
        .filter(Boolean)
        .filter((label, idx, arr) => arr.indexOf(label) === idx);
    if (!labels.length) return '';
    const shown = labels.slice(0, limit);
    const rest = labels.length - shown.length;
    return shown.join(', ') + (rest > 0 ? ` +${rest}` : '');
}

function directMarketingProfitText(value) {
    const n = parseFloat(value);
    if (!Number.isFinite(n)) return '';
    const prefix = n >= 0 ? '+' : '';
    return prefix + n.toFixed(2).replace('.', ',') + ' ct/kWh';
}

function directMarketingSalesWindowContract(monitor, plan) {
    monitor = monitor && typeof monitor === 'object' ? monitor : {};
    plan = plan && typeof plan === 'object' ? plan : {};
    const policy = plan.policy_decision && typeof plan.policy_decision === 'object'
        ? plan.policy_decision
        : {};
    const selected = policy.selected_window && typeof policy.selected_window === 'object'
        ? policy.selected_window
        : {};
    const action = String(monitor.current_action || selected.action || '').toLowerCase();
    const candidate = action === 'eco_plus_export_candidate' || action === 'arbitrage_export_candidate';
    const targetState = String(monitor.policy_target_state || policy.dv_target_state || '').toUpperCase();
    const commandsAllowed = monitor.commands_allowed === true || policy.commands_allowed === true;
    const selectedForExecution = monitor.selected === true || Object.keys(selected).length > 0;
    const executionWindow = policy.execution_window && typeof policy.execution_window === 'object'
        ? policy.execution_window
        : null;
    const executable = monitor.executable === true || executionWindow !== null;
    const storageBudget = policy.storage_budget && typeof policy.storage_budget === 'object'
        ? policy.storage_budget
        : {};
    const exportBudgetW = Math.max(0, parseFloat(
        monitor.policy_export_budget_w != null
            ? monitor.policy_export_budget_w
            : storageBudget.export_budget_w
    ) || 0);
    return {
        action,
        candidate,
        selected: selectedForExecution,
        executable,
        targetState,
        commandsAllowed,
        exportBudgetW,
        active: candidate
            && selectedForExecution
            && executable
            && targetState === 'FORCE_EXPORT'
            && commandsAllowed
            && exportBudgetW > 0
    };
}

function directMarketingMarginGateText(monitor, plan, action = '') {
    monitor = monitor && typeof monitor === 'object' ? monitor : {};
    plan = plan && typeof plan === 'object' ? plan : {};
    const policy = plan.policy_decision && typeof plan.policy_decision === 'object'
        ? plan.policy_decision
        : {};
    const policyEconomics = policy.economics && typeof policy.economics === 'object'
        ? policy.economics
        : {};
    const economics = Object.keys(policyEconomics).length
        ? policyEconomics
        : ((monitor.economics && typeof monitor.economics === 'object')
            ? monitor.economics
            : ((plan.economics && typeof plan.economics === 'object') ? plan.economics : {}));
    const normalized = String(action || monitor.current_action || '').toLowerCase();
    const marginRaw = economics.margin_ct_kwh != null
        ? economics.margin_ct_kwh
        : directMarketingSpreadForAction(normalized, economics);
    const minimumRaw = economics.user_min_margin_ct != null
        ? economics.user_min_margin_ct
        : economics.min_profit_ct_per_kwh;
    const margin = parseFloat(marginRaw);
    const minimum = parseFloat(minimumRaw);
    if (!Number.isFinite(margin)) return '';
    const parts = ['Entscheidungsmarge ' + directMarketingProfitText(margin)];
    if (Number.isFinite(minimum)) {
        parts.push('Mindestmarge ' + directMarketingProfitText(minimum));
        parts.push('Fehlbetrag ' + directMarketingProfitText(Math.max(0, minimum - margin)));
    }
    return parts.join(' | ');
}

function directMarketingKwhText(value) {
    const n = parseFloat(value);
    if (!Number.isFinite(n)) return '';
    return n.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' kWh';
}

function directMarketingTopCountsText(counts, limit = 2, mapper = value => value) {
    if (!counts || typeof counts !== 'object') return '';
    const entries = Object.entries(counts)
        .filter(([, count]) => parseFloat(count) > 0)
        .sort((a, b) => parseFloat(b[1]) - parseFloat(a[1]))
        .slice(0, limit)
        .map(([key, count]) => mapper(key) + ' ' + Math.round(parseFloat(count)));
    return entries.join(', ');
}

function directMarketingHtmlEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }[ch]));
}

function directMarketingReasonLabel(reason) {
    const normalized = String(reason || '').toLowerCase();
    const labels = {
        low_price: 'Billigpreis',
        negative_price: 'Negativpreis',
        threshold_below_eeg: 'unter EEG-Schwelle',
        profitable_high_price: 'Hochpreis',
        profitable_low_price: 'Billigpreis',
        high_price_house_supply: 'Hochpreis-Hausversorgung',
        neutral_current_house_supply: 'aktueller E3DC-AUTO-Slot',
        neutral_dv_slot: 'normaler E3DC-AUTO-Zeitraum',
        reserve_for_higher_profit: 'für höheres Verkaufsfenster reserviert',
        reserved_for_higher_profit: 'für höheres Verkaufsfenster reserviert',
        higher_profit_reserved: 'für höheres Verkaufsfenster reserviert',
        policy_passive_house_supply: 'expliziter PASSIVE_NORMAL-Vertrag; E3DC-AUTO',
        plan_projection_gap: 'EVIDENCE_LIMIT: keine eindeutige wirksame Slotprojektion',
        plan_projection_overlap: 'EVIDENCE_LIMIT: überlappende wirksame Fenster'
    };
    return labels[normalized] || normalized.replace(/_/g, ' ');
}

function directMarketingWindowVisual(action, executionContract = null) {
    const normalized = String(action || '').toLowerCase();
    if (executionContract && executionContract.runtimeHoldActive === true && executionContract.holdActive === true) {
        return {label: 'Speicherplatz halten', icon: 'fa-lock', color: '#f59e0b'};
    }
    if (normalized === 'negative_price_market_window') {
        return {label: 'Negativpreis-Marktfenster', icon: 'fa-chart-line', color: '#0ea5e9'};
    }
    if (normalized === 'direct_marketing_plan_evidence_gap') {
        return {label: 'Planlücke', icon: 'fa-triangle-exclamation', color: '#ef4444'};
    }
    if (normalized === 'eco_plus_export_candidate' || normalized === 'arbitrage_export_candidate') {
        return executionContract && (executionContract.active || executionContract.planned)
            ? {label: 'Verkaufsfenster', icon: 'fa-arrow-up', color: '#22c55e'}
            : {label: 'Verkaufskandidat', icon: 'fa-search', color: '#94a3b8'};
    }
    if (normalized === 'arbitrage_grid_charge_candidate') {
        return {label: 'Netzladefenster', icon: 'fa-plug', color: '#38bdf8'};
    }
    if (normalized === 'eco_plus_store_pv_candidate') {
        return {label: 'PV speichern', icon: 'fa-battery-half', color: '#f59e0b'};
    }
    if (normalized === 'eco_plus_curve_charge_candidate') {
        return {label: 'Kurvenladung im E3DC-AUTO-Rahmen', icon: 'fa-chart-line', color: '#8b5cf6'};
    }
    if (normalized === 'keep_headroom' || normalized === 'arbitrage_keep_headroom') {
        return {label: 'Speicherplatz freihalten', icon: 'fa-battery-half', color: '#f59e0b'};
    }
    if (directMarketingIsHoldAction(normalized)) {
        if (
            executionContract
            && executionContract.planned
            && !executionContract.holdActive
            && executionContract.targetState === 'HEADROOM_EXPORT'
            && executionContract.exportBudgetW > 0
        ) {
            return {label: 'Speicherplatz schaffen', icon: 'fa-arrow-up', color: '#22c55e'};
        }
        return executionContract && executionContract.holdActive
            ? {label: 'Speicherplatz halten', icon: 'fa-lock', color: '#f59e0b'}
            : {label: 'Speicherplatzreserve', icon: 'fa-battery-empty', color: '#f59e0b'};
    }
    if (normalized === 'safe_house_supply' || normalized === 'eco_plus_house_supply') {
        return {label: 'Hausversorgung', icon: 'fa-home', color: '#a78bfa'};
    }
    return {label: 'Preisfenster', icon: 'fa-clock', color: '#94a3b8'};
}

function directMarketingSpreadForAction(action, economics) {
    const normalized = String(action || '').toLowerCase();
    economics = economics && typeof economics === 'object' ? economics : {};
    if (normalized === 'eco_plus_export_candidate' || normalized === 'eco_plus_store_pv_candidate') {
        return economics.pv_shift_spread_ct_per_kwh;
    }
    if (normalized === 'arbitrage_grid_charge_candidate' || normalized === 'arbitrage_export_candidate') {
        return economics.grid_spread_ct_per_kwh;
    }
    return null;
}

function directMarketingSpreadLabelForAction(action) {
    const normalized = String(action || '').toLowerCase();
    if (normalized === 'eco_plus_export_candidate' || normalized === 'eco_plus_store_pv_candidate') {
        return 'PV-Verschiebespread';
    }
    if (normalized === 'arbitrage_grid_charge_candidate' || normalized === 'arbitrage_export_candidate') {
        return 'Netz-Verschiebespread';
    }
    return 'Verschiebespread';
}

function directMarketingWindowExecutionContract(windowEntry, monitor, plan, physicalAction = null) {
    windowEntry = windowEntry && typeof windowEntry === 'object' ? windowEntry : {};
    monitor = monitor && typeof monitor === 'object' ? monitor : {};
    plan = plan && typeof plan === 'object' ? plan : {};
    const action = String(windowEntry.action || '').toLowerCase();
    const marketOnly = action === 'negative_price_market_window' || windowEntry.market_window_only === true;
    if (marketOnly) {
        return {
            action,
            selected: false,
            executable: false,
            commandsAllowed: false,
            targetState: 'MARKET_WINDOW_ONLY',
            exportBudgetW: 0,
            chargeBudgetW: 0,
            planned: false,
            passivePlanHint: false,
            passivePlanEffective: false,
            active: false,
            executionBlocked: false,
            executorGateBound: false,
            physicalActionBound: false,
            candidateOnly: false,
            marketOnly: true,
            blockReason: ''
        };
    }
    const start = parseFloat(windowEntry.start_ts || 0) || 0;
    const end = parseFloat(windowEntry.end_ts || 0) || 0;
    const decisions = [];
    if (plan.policy_decision && typeof plan.policy_decision === 'object') decisions.push(plan.policy_decision);
    if (Array.isArray(plan.policy_timeline)) decisions.push(...plan.policy_timeline.filter(item => item && typeof item === 'object'));

    const overlaps = (leftStart, leftEnd, rightStart, rightEnd) => (
        leftStart > 0 && leftEnd > leftStart && rightStart > 0 && rightEnd > rightStart
            ? leftStart < rightEnd && leftEnd > rightStart
            : true
    );
    const policyRowsForWindow = Array.isArray(plan.policy_timeline)
        ? plan.policy_timeline.filter(item => {
            if (!item || typeof item !== 'object') return false;
            const policyStart = parseFloat(item.start_ts || 0) || 0;
            const policyEnd = parseFloat(item.end_ts || 0) || 0;
            return policyStart > 0
                && policyEnd > policyStart
                && overlaps(start, end, policyStart, policyEnd);
        })
        : [];
    const slotDuration = start < 100000000000 ? 900 : 900000;
    const atomicPolicyAmbiguous = end > start
        && end - start <= slotDuration + 0.000001
        && policyRowsForWindow.length > 1;
    const decision = !atomicPolicyAmbiguous ? decisions.find(item => {
        const selected = item.selected_window && typeof item.selected_window === 'object'
            ? item.selected_window
            : {};
        if (String(selected.action || '').toLowerCase() !== action) return false;
        const policyStart = parseFloat(item.start_ts || 0) || 0;
        const policyEnd = parseFloat(item.end_ts || 0) || 0;
        if (
            start > 0
            && end > start
            && policyStart > 0
            && policyEnd > policyStart
            && !overlaps(start, end, policyStart, policyEnd)
        ) {
            return false;
        }
        const selectedStart = parseFloat(selected.start_ts || item.start_ts || 0) || 0;
        const selectedEnd = parseFloat(selected.end_ts || item.end_ts || 0) || 0;
        return overlaps(start, end, selectedStart, selectedEnd);
    }) || null : null;
    const selectedWindow = decision && decision.selected_window && typeof decision.selected_window === 'object'
        ? decision.selected_window
        : {};
    const executionWindow = decision && decision.execution_window && typeof decision.execution_window === 'object'
        ? decision.execution_window
        : null;
    const targetState = String((decision && decision.dv_target_state) || '').toUpperCase();
    const storageBudget = decision && decision.storage_budget && typeof decision.storage_budget === 'object'
        ? decision.storage_budget
        : {};
    const exportBudgetW = Math.max(0, parseFloat(storageBudget.export_budget_w) || 0);
    const chargeBudgetW = Math.max(0, parseFloat(storageBudget.charge_budget_w) || 0);
    const selected = Boolean(decision && Object.keys(selectedWindow).length);
    const executable = Boolean(executionWindow);
    const planCommandsAllowed = Boolean(decision && decision.commands_allowed === true);
    const isExport = action === 'eco_plus_export_candidate' || action === 'arbitrage_export_candidate';
    const isPvStore = action === 'eco_plus_store_pv_candidate';
    const isCurveCharge = action === 'eco_plus_curve_charge_candidate';
    const isHeadroom = directMarketingIsHoldAction(action);
    const plannedHeadroomHold = selected && executable && planCommandsAllowed && isHeadroom && (
        (targetState === 'HEADROOM_EXPORT' && storageBudget.headroom_hold_active === true)
        || targetState === 'CHARGE_BLOCK_WAIT'
    );
    const monitorTargetState = String(monitor.policy_target_state || '').toUpperCase();
    const monitorAction = String(monitor.current_action || '').toLowerCase();
    const monitorCurrentWindow = monitor.current_window && typeof monitor.current_window === 'object'
        ? monitor.current_window
        : {};
    const monitorStart = parseFloat(monitorCurrentWindow.start_ts || 0) || 0;
    const monitorEnd = parseFloat(monitorCurrentWindow.end_ts || 0) || 0;
    const runtimeWindowMatches = Boolean(
        windowEntry.current === true
        || (
            start > 0
            && end > start
            && monitorStart > 0
            && monitorEnd > monitorStart
            && start < monitorEnd
            && monitorStart < end
        )
    );
    const runtimeHoldAction = [
        'policy_headroom_hold',
        'policy_charge_block_wait',
        'direct_marketing_charge_block_wait',
        'charge_block_wait'
    ].includes(monitorAction);
    const runtimeHoldActive = Boolean(
        monitor.active === true
        && isHeadroom
        && runtimeWindowMatches
        && runtimeHoldAction
        && (
            (monitorTargetState === 'HEADROOM_EXPORT' && monitor.headroom_hold_active === true)
            || monitorTargetState === 'CHARGE_BLOCK_WAIT'
        )
    );
    const physicalStart = directMarketingTimestampMs(physicalAction && physicalAction.startTs);
    const physicalEnd = directMarketingTimestampMs(physicalAction && physicalAction.endTs);
    const windowStart = directMarketingTimestampMs(start);
    const windowEnd = directMarketingTimestampMs(end);
    const physicalHasBounds = physicalStart > 0 && physicalEnd > physicalStart;
    const physicalWindowMatches = Boolean(
        physicalAction
        && (
            (
                physicalHasBounds
                && windowStart > 0
                && windowEnd > windowStart
                && physicalStart < windowEnd
                && windowStart < physicalEnd
            )
            || (!physicalHasBounds && windowEntry.current === true)
        )
    );
    const physicalActionName = String(physicalAction && physicalAction.action || '').toLowerCase();
    const physicalActionMatches = Boolean(
        physicalAction
        && (
            physicalActionName === action
            || (physicalAction.holdActive === true && (isPvStore || isHeadroom))
        )
    );
    const physicalActionBound = Boolean(
        !isCurveCharge
        && physicalAction
        && physicalAction.hardwareEffect === true
        && physicalWindowMatches
        && physicalActionMatches
    );
    const executorGate = monitor.policy_executor_gate && typeof monitor.policy_executor_gate === 'object'
        ? monitor.policy_executor_gate
        : {};
    const executorGateBound = Boolean(
        physicalActionBound
        && monitor.policy_commands_allowed === true
        && executorGate.allowed === true
        && String(executorGate.plan_id || '') !== ''
        && String(executorGate.slot_id || '') !== ''
        && String(executorGate.plan_id) === String(physicalAction.planId || '')
        && String(executorGate.slot_id) === String(physicalAction.slotId || '')
    );
    const commandsAllowed = Boolean(planCommandsAllowed && executorGateBound);
    const physicalHoldActive = Boolean(physicalActionBound && physicalAction.holdActive === true);
    const effectiveRuntimeHoldActive = physicalHoldActive && (
        runtimeHoldActive || physicalAction.hardwareEffect === true
    );
    const holdActive = plannedHeadroomHold || effectiveRuntimeHoldActive;
    const selectedPvStoreStarts = [];
    if (plan.policy_decision && typeof plan.policy_decision === 'object') {
        selectedPvStoreStarts.push(plan.policy_decision);
    }
    if (Array.isArray(plan.policy_timeline)) {
        selectedPvStoreStarts.push(...plan.policy_timeline.filter(item => item && typeof item === 'object'));
    }
    const firstSelectedPvStoreTs = selectedPvStoreStarts
        .filter(item => {
            const selectedWindow = item.selected_window && typeof item.selected_window === 'object'
                ? item.selected_window
                : {};
            return item.commands_allowed === true
                && item.execution_window
                && (
                    String(item.dv_target_state || '').toUpperCase() === 'FORCE_CHARGE_PV'
                    || String(selectedWindow.action || '').toLowerCase() === 'eco_plus_store_pv_candidate'
                    || String(item.dv_target_state || '').toUpperCase() === 'DV_CURVE_CHARGE'
                    || String(selectedWindow.action || '').toLowerCase() === 'eco_plus_curve_charge_candidate'
                );
        })
        .map(item => {
            const executionWindow = item.execution_window && typeof item.execution_window === 'object'
                ? item.execution_window
                : {};
            const selectedWindow = item.selected_window && typeof item.selected_window === 'object'
                ? item.selected_window
                : {};
            return parseFloat(
                executionWindow.start_ts
                || selectedWindow.start_ts
                || item.start_ts
                || 0
            ) || 0;
        })
        .filter(value => {
            if (value <= 0) return false;
            const valueMs = value > 100000000000 ? value : value * 1000;
            const holdStartMs = start > 100000000000 ? start : start * 1000;
            return valueMs >= Math.max(Date.now() - 60000, holdStartMs);
        })
        .sort((left, right) => left - right)[0] || 0;
    const effectiveUntilTs = Math.max(
        0,
        parseFloat(monitor.headroom_next_start_ts || 0) || firstSelectedPvStoreTs
    );
    const planned = Boolean(
        selected
        && executable
        && planCommandsAllowed
        && (
            (isExport && targetState === 'FORCE_EXPORT' && exportBudgetW > 0)
            || (isPvStore && targetState === 'FORCE_CHARGE_PV' && chargeBudgetW > 0)
            || (isCurveCharge && targetState === 'DV_CURVE_CHARGE' && chargeBudgetW > 0)
            || (
                isHeadroom
                && (
                    plannedHeadroomHold
                    || (targetState === 'HEADROOM_EXPORT' && exportBudgetW > 0)
                )
            )
        )
    );
    const active = effectiveRuntimeHoldActive || (
        selected && executable && commandsAllowed && (
            (isExport && targetState === 'FORCE_EXPORT' && exportBudgetW > 0)
            || (isPvStore && targetState === 'FORCE_CHARGE_PV' && chargeBudgetW > 0)
            || (isHeadroom && targetState === 'HEADROOM_EXPORT' && exportBudgetW > 0)
        )
    );
    const passivePlanHint = isCurveCharge && planned;
    const passivePlanEffective = Boolean(
        isCurveCharge
        && physicalAction
        && physicalAction.hardwareEffect === true
        && physicalWindowMatches
        && physicalActionName === action
        && String(physicalAction.rawAction || '').toUpperCase() === 'DV_CURVE_CHARGE'
    );
    const nowMs = Date.now();
    const applicableNow = windowEntry.current === true || Boolean(
        windowStart > 0
        && windowEnd > windowStart
        && windowStart <= nowMs
        && nowMs < windowEnd
    );
    const planAction = isExport || isPvStore || isCurveCharge || isHeadroom;
    const executionBlocked = !isCurveCharge
        && planAction
        && !active
        && !passivePlanEffective
        && (!planned || applicableNow);
    const candidateOnly = planAction && !active && !passivePlanEffective && !planned;
    const executionBlockReason = isCurveCharge && planned
        ? ''
        : !physicalActionBound
        ? 'kein gebundener Hardwareeffekt'
        : (!executorGateBound && !effectiveRuntimeHoldActive
            ? String(executorGate.reason || 'Executor-Freigabe nicht gebunden')
            : '');
    const blockReason = String(
        (decision && (decision.block_reason_code || decision.block_reason))
        || (monitor && (monitor.block_reason_code || monitor.block_reason))
        || executionBlockReason
        || (executionBlocked ? 'aktuell nicht ausgeführt' : '')
    );
    return {
        action,
        selected,
        executable,
        commandsAllowed,
        targetState,
        exportBudgetW,
        chargeBudgetW,
        planned,
        passivePlanHint,
        passivePlanEffective,
        active,
        executionBlocked,
        executorGateBound,
        physicalActionBound,
        holdActive,
        runtimeHoldActive: effectiveRuntimeHoldActive,
        effectiveUntilTs,
        candidateOnly,
        blockReason
    };
}

function directMarketingWindowCanonicalBinding(windowEntry, monitor = null, plan = null) {
    windowEntry = windowEntry && typeof windowEntry === 'object' ? windowEntry : {};
    monitor = monitor && typeof monitor === 'object' ? monitor : {};
    plan = plan && typeof plan === 'object' ? plan : {};
    const action = String(windowEntry.action || '').toLowerCase();
    const entryStart = parseFloat(windowEntry.start_ts || 0) || 0;
    const entryEnd = parseFloat(windowEntry.end_ts || 0) || 0;
    const validBounds = (start, end) => Number.isFinite(start) && Number.isFinite(end) && start > 0 && end > start;
    const ownId = String(windowEntry.window_id || windowEntry.export_plateau_id || '');
    const exportPlateauAction = [
        'eco_plus_export_candidate',
        'arbitrage_export_candidate'
    ].includes(action);
    const ownStart = parseFloat(
        windowEntry.source_window_start_ts
        || (exportPlateauAction ? windowEntry.export_plateau_origin_start_ts : 0)
        || 0
    ) || 0;
    const ownEnd = parseFloat(
        windowEntry.source_window_end_ts
        || (exportPlateauAction ? windowEntry.export_plateau_end_ts : 0)
        || 0
    ) || 0;
    if (ownId || validBounds(ownStart, ownEnd)) {
        return {
            id: ownId,
            sourceStart: validBounds(ownStart, ownEnd) ? ownStart : entryStart,
            sourceEnd: validBounds(ownStart, ownEnd) ? ownEnd : entryEnd,
            contract: windowEntry
        };
    }

    const gate = monitor.policy_executor_gate && typeof monitor.policy_executor_gate === 'object'
        ? monitor.policy_executor_gate
        : {};
    const gateWindow = gate.plan_window && typeof gate.plan_window === 'object' ? gate.plan_window : {};
    const gateExecution = gate.execution_window && typeof gate.execution_window === 'object' ? gate.execution_window : {};
    const gateSelected = gate.selected_window && typeof gate.selected_window === 'object' ? gate.selected_window : {};
    const gateAction = String(gateWindow.action || gateExecution.action || gateSelected.action || '').toLowerCase();
    const gateExportPlateauAction = [
        'eco_plus_export_candidate',
        'arbitrage_export_candidate'
    ].includes(gateAction);
    const gateStart = parseFloat(
        gateWindow.source_window_start_ts
        || gateExecution.plan_window_start_ts
        || (gateExportPlateauAction ? gateWindow.export_plateau_origin_start_ts : 0)
        || 0
    ) || 0;
    const gateEnd = parseFloat(
        gateWindow.source_window_end_ts
        || gateExecution.plan_window_end_ts
        || (gateExportPlateauAction ? gateWindow.export_plateau_end_ts : 0)
        || 0
    ) || 0;
    const gateId = String(
        gateWindow.window_id
        || gateWindow.export_plateau_id
        || gateExecution.window_id
        || gateSelected.window_id
        || ''
    );
    if (
        gate.allowed === true
        && action
        && action === gateAction
        && validBounds(gateStart, gateEnd)
        && validBounds(entryStart, entryEnd)
        && gateStart <= entryStart
        && entryEnd <= gateEnd
    ) {
        return {id: gateId, sourceStart: gateStart, sourceEnd: gateEnd, contract: gateWindow};
    }

    const planWindows = Array.isArray(plan.windows) ? plan.windows : [];
    const exactMatches = planWindows.filter(candidate => {
        if (!candidate || typeof candidate !== 'object') return false;
        if (String(candidate.action || '').toLowerCase() !== action) return false;
        const start = parseFloat(candidate.source_window_start_ts || candidate.start_ts || 0) || 0;
        const end = parseFloat(candidate.source_window_end_ts || candidate.end_ts || 0) || 0;
        return validBounds(start, end) && start === entryStart && end === entryEnd;
    });
    if (exactMatches.length === 1) {
        const candidate = exactMatches[0];
        return {
            id: String(candidate.window_id || candidate.export_plateau_id || ''),
            sourceStart: parseFloat(candidate.source_window_start_ts || candidate.start_ts || 0) || entryStart,
            sourceEnd: parseFloat(candidate.source_window_end_ts || candidate.end_ts || 0) || entryEnd,
            contract: candidate
        };
    }
    return {id: '', sourceStart: entryStart, sourceEnd: entryEnd, contract: windowEntry};
}

function directMarketingWindowClock(value) {
    const raw = parseFloat(value);
    if (!Number.isFinite(raw) || raw <= 0) return '--:--';
    const ms = raw > 100000000000 ? raw : raw * 1000;
    return new Intl.DateTimeFormat('de-DE', {
        timeZone: 'Europe/Berlin',
        hour: '2-digit',
        minute: '2-digit',
        hourCycle: 'h23'
    }).format(new Date(ms));
}

function directMarketingWindowContractKey(windowEntry, binding = null, monitor = null, plan = null) {
    windowEntry = windowEntry && typeof windowEntry === 'object' ? windowEntry : {};
    binding = binding && typeof binding === 'object' ? binding : {};
    const contract = binding.contract && typeof binding.contract === 'object' ? binding.contract : windowEntry;
    monitor = monitor && typeof monitor === 'object' ? monitor : {};
    plan = plan && typeof plan === 'object' ? plan : {};
    const value = field => contract[field] ?? windowEntry[field] ?? '';
    return JSON.stringify([
        value('export_segment_id'),
        value('export_segment_budget_source'),
        value('export_segment_budget_wh'),
        value('export_segment_load_reserve_wh'),
        value('export_segment_selected_wh'),
        value('profit_contract_id'),
        value('profit_contract_version'),
        value('expected_profit_ct_per_kwh'),
        value('net_sell_ct'),
        value('avg_market_ct'),
        value('avg_billing_ct'),
        value('execution_contract_id'),
        value('execution_contract_version'),
        value('owner_contract_version'),
        String(plan.controller_owner || monitor.controller_owner || ''),
        String(plan.plan_owner || monitor.plan_owner || '')
    ]);
}

function directMarketingWindowKey(windowEntry, binding = null, monitor = null, plan = null) {
    if (!windowEntry || typeof windowEntry !== 'object') return '';
    binding = binding && typeof binding === 'object'
        ? binding
        : directMarketingWindowCanonicalBinding(windowEntry, monitor, plan);
    const semanticIdentity = binding.id
        ? 'id:' + String(binding.id)
        : ((binding.sourceStart > 0 && binding.sourceEnd > binding.sourceStart)
            ? 'source:' + String(binding.sourceStart) + ':' + String(binding.sourceEnd)
            : 'slice:' + String(windowEntry.start_ts || windowEntry.start_t || '') + ':' + String(windowEntry.end_ts || windowEntry.end_t || ''));
    return [
        String(windowEntry.action || 'unknown').toLowerCase(),
        semanticIdentity,
        directMarketingWindowContractKey(windowEntry, binding, monitor, plan)
    ].join(':');
}

function collectDirectMarketingWindows(report, monitor, plan) {
    const merged = new Map();
    const addOne = (windowEntry, source) => {
        if (!windowEntry || typeof windowEntry !== 'object') return;
        const binding = directMarketingWindowCanonicalBinding(windowEntry, monitor, plan);
        const baseKey = directMarketingWindowKey(windowEntry, binding, monitor, plan);
        if (!baseKey) return;
        let key = baseKey;
        const previous = merged.get(baseKey);
        if (previous && binding.id) {
            const previousStart = parseFloat(previous._source_window_start_ts || previous.start_ts || 0) || 0;
            const previousEnd = parseFloat(previous._source_window_end_ts || previous.end_ts || 0) || 0;
            const overlaps = previousStart > 0 && previousEnd > previousStart
                && binding.sourceStart > 0 && binding.sourceEnd > binding.sourceStart
                && previousStart < binding.sourceEnd && binding.sourceStart < previousEnd;
            if (!overlaps) key += ':disjoint:' + String(binding.sourceStart) + ':' + String(binding.sourceEnd);
        }
        const current = merged.get(key) || {_source: source};
        const wasCurrent = current.current === true;
        const entryStart = parseFloat(windowEntry.start_ts || 0) || 0;
        const entryEnd = parseFloat(windowEntry.end_ts || 0) || 0;
        Object.entries(windowEntry).forEach(([field, value]) => {
            if (value !== undefined && value !== null && value !== '') current[field] = value;
        });
        if (windowEntry.current === true && entryStart > 0 && entryEnd > entryStart) {
            current._current_slice_start_ts = entryStart;
            current._current_slice_end_ts = entryEnd;
        }
        if (binding.id) current._canonical_window_id = binding.id;
        if (binding.sourceStart > 0 && binding.sourceEnd > binding.sourceStart) {
            current.start_ts = binding.sourceStart;
            current.end_ts = binding.sourceEnd;
            current._source_window_start_ts = binding.sourceStart;
            current._source_window_end_ts = binding.sourceEnd;
            current.start_t = directMarketingWindowClock(binding.sourceStart);
            current.end_t = directMarketingWindowClock(binding.sourceEnd);
        }
        if (wasCurrent || windowEntry.current === true) current.current = true;
        if (!current._source) current._source = source;
        merged.set(key, current);
    };
    const addMany = (items, source) => {
        if (!Array.isArray(items)) return;
        items.forEach(item => addOne(item, source));
    };

    if (monitor && typeof monitor === 'object') {
        addOne(monitor.current_window, 'monitor');
        addMany(monitor.upcoming_windows, 'monitor');
    }
    if (plan && typeof plan === 'object') {
        addMany(plan.market_windows, 'market');
        addMany(plan.windows, 'plan');
    }
    if (!merged.size && report && typeof report === 'object') addMany(report.windows, 'report');

    return Array.from(merged.values()).sort((a, b) => {
        const at = parseFloat(a.start_ts || 0) || 0;
        const bt = parseFloat(b.start_ts || 0) || 0;
        if (at !== bt) return at - bt;
        return String(a.action || '').localeCompare(String(b.action || ''));
    });
}

function directMarketingWindowBounds(item) {
    const entry = item && item.windowEntry && typeof item.windowEntry === 'object'
        ? item.windowEntry
        : {};
    const start = parseFloat(entry.start_ts || 0) || 0;
    const end = parseFloat(entry.end_ts || 0) || 0;
    return {start, end, valid: start > 0 && end > start};
}

function directMarketingExactDisplayValue(value) {
    if (value === null) return ['null'];
    if (value === undefined) return ['undefined'];
    if (Array.isArray(value)) {
        return ['array', value.map(item => directMarketingExactDisplayValue(item))];
    }
    const type = typeof value;
    if (type === 'number') {
        if (Number.isNaN(value)) return ['number', 'NaN'];
        if (!Number.isFinite(value)) return ['number', value > 0 ? 'Infinity' : '-Infinity'];
        if (Object.is(value, -0)) return ['number', '-0'];
        return ['number', value];
    }
    if (type === 'object') {
        return ['object', Object.keys(value).sort().map(key => [
            key,
            directMarketingExactDisplayValue(value[key])
        ])];
    }
    return [type, value];
}

function directMarketingNeutralHouseSupplyDisplaySignature(item) {
    const entry = item && item.windowEntry && typeof item.windowEntry === 'object'
        ? item.windowEntry
        : {};
    const execution = item && item.execution && typeof item.execution === 'object'
        ? item.execution
        : {};
    if (
        String(entry.action || '').toLowerCase() !== 'eco_plus_house_supply'
        || String(entry.reason || '').toLowerCase() !== 'neutral_dv_slot'
        || entry.current === true
    ) {
        return '';
    }
    const bounds = directMarketingWindowBounds(item);
    if (!bounds.valid) return '';
    const effectContract = String(entry.effect_contract || '').toUpperCase();
    if (effectContract && effectContract !== 'LEGACY_AUTO_FRAME_PASSTHROUGH') return '';
    const nonPassiveEntry = [
        'selected',
        'executable',
        'commands_allowed',
        'active',
        'hold_active',
        'runtime_hold_active',
        'candidate_only',
        'market_window_only'
    ].some(field => entry[field] === true);
    const nonPassiveExecution = [
        'selected',
        'executable',
        'commandsAllowed',
        'planned',
        'passivePlanHint',
        'passivePlanEffective',
        'active',
        'executionBlocked',
        'executorGateBound',
        'physicalActionBound',
        'holdActive',
        'runtimeHoldActive',
        'candidateOnly',
        'marketOnly'
    ].some(field => execution[field] === true);
    const targetState = String(execution.targetState || '').toUpperCase();
    const nonPassiveBudget = ['exportBudgetW', 'chargeBudgetW'].some(field => {
        const value = parseFloat(execution[field]);
        return Number.isFinite(value) && Math.abs(value) > 0.000001;
    });
    const nonPassivePower = ['max_power_w', 'theoretical_kwh', 'export_segment_selected_wh'].some(field => {
        const value = parseFloat(entry[field]);
        return Number.isFinite(value) && Math.abs(value) > 0.000001;
    });
    if (
        nonPassiveEntry
        || nonPassiveExecution
        || nonPassiveBudget
        || nonPassivePower
        || (targetState && !['NORMAL', 'PASSIVE_NORMAL', 'HOUSE_SUPPLY'].includes(targetState))
        || directMarketingWindowPriceSignature(item) !== '[null,null,null,null]'
    ) {
        return '';
    }
    const reservationFields = [
        'window_id',
        'market_window_id',
        'export_plateau_id',
        'export_segment_id',
        'profit_contract_id',
        'execution_contract_id',
        'reservation_id',
        'reservation_reason',
        'reserved',
        'export_segment_selected_wh',
        'export_segment_budget_wh',
        'export_budget_w',
        'charge_budget_w'
    ];
    const hasReservation = reservationFields.some(field => {
        if (!Object.prototype.hasOwnProperty.call(entry, field)) return false;
        const value = entry[field];
        if (typeof value === 'boolean') return value;
        if (typeof value === 'number') return Number.isFinite(value) && Math.abs(value) > 0.000001;
        if (typeof value === 'string') return value.trim() !== '';
        if (Array.isArray(value)) return value.length > 0;
        return value && typeof value === 'object' && Object.keys(value).length > 0;
    });
    if (hasReservation) return '';

    const ignoredEntryFields = new Set([
        'start_ts',
        'end_ts',
        'start_t',
        'end_t',
        '_source_window_start_ts',
        '_source_window_end_ts',
        '_source',
        'current',
        'slot_count',
        '_compacted_slot_count',
        '_display_compaction'
    ]);
    const semanticEntry = Object.fromEntries(
        Object.entries(entry).filter(([field]) => !ignoredEntryFields.has(field))
    );
    return JSON.stringify(directMarketingExactDisplayValue({
        windowEntry: semanticEntry,
        execution
    }));
}

function directMarketingCompactNeutralHouseSupplyWindows(items) {
    return (Array.isArray(items) ? items : []).reduce((result, item) => {
        const previous = result[result.length - 1];
        const itemSignature = directMarketingNeutralHouseSupplyDisplaySignature(item);
        const previousSignature = directMarketingNeutralHouseSupplyDisplaySignature(previous);
        if (!previous || !itemSignature || itemSignature !== previousSignature) {
            result.push(item);
            return result;
        }
        const previousBounds = directMarketingWindowBounds(previous);
        const itemBounds = directMarketingWindowBounds(item);
        if (
            !previousBounds.valid
            || !itemBounds.valid
            || Math.abs(itemBounds.start - previousBounds.end) > 0.000001
        ) {
            result.push(item);
            return result;
        }
        const entry = {...previous.windowEntry};
        entry.end_ts = itemBounds.end;
        entry.end_t = mobileStorageTime(itemBounds.end);
        entry._compacted_slot_count = Math.max(
            1,
            parseInt(previous.windowEntry._compacted_slot_count || 1, 10)
        ) + Math.max(1, parseInt(item.windowEntry._compacted_slot_count || 1, 10));
        entry.slot_count = entry._compacted_slot_count;
        entry._display_compaction = 'neutral_house_supply';
        result[result.length - 1] = {
            windowEntry: entry,
            execution: {...previous.execution}
        };
        return result;
    }, []);
}

function directMarketingPolicyBounds(policy) {
    policy = policy && typeof policy === 'object' ? policy : {};
    const start = parseFloat(policy.start_ts || 0) || 0;
    const end = parseFloat(policy.end_ts || 0) || 0;
    return {start, end, valid: start > 0 && end > start};
}

function directMarketingPolicyEnvelopeValid(plan) {
    plan = plan && typeof plan === 'object' ? plan : {};
    const mode = String(plan.mode || '').trim().toLowerCase().replace(/\+/g, '_plus');
    const flags = plan.flags && typeof plan.flags === 'object' ? plan.flags : {};
    return mode === 'eco_plus'
        && plan.active === true
        && plan.shadow === false
        && String(plan.controller_owner || '').toLowerCase() === 'storage_manager'
        && String(plan.plan_owner || '').toLowerCase() === 'direct_marketing:eco_plus'
        && Number.isInteger(plan.owner_contract_version)
        && plan.owner_contract_version === 1
        && flags.commands_allowed === true;
}

function directMarketingPassiveNormalPolicyForSegment(plan, start, end) {
    if (!directMarketingPolicyEnvelopeValid(plan)) return null;
    const policies = (Array.isArray(plan.policy_timeline) ? plan.policy_timeline : []).filter(policy => {
        const bounds = directMarketingPolicyBounds(policy);
        return bounds.valid && bounds.start <= start && end <= bounds.end;
    });
    if (policies.length !== 1) return null;
    const policy = policies[0];
    const selectedWindow = policy.selected_window && typeof policy.selected_window === 'object'
        ? policy.selected_window
        : null;
    const budget = policy.storage_budget && typeof policy.storage_budget === 'object'
        ? policy.storage_budget
        : {};
    const binding = policy.passive_normal_binding && typeof policy.passive_normal_binding === 'object'
        ? policy.passive_normal_binding
        : {};
    const effectContract = String(policy.effect_contract || binding.effect_contract || '').toUpperCase();
    const policyBounds = directMarketingPolicyBounds(policy);
    const selectedStart = parseFloat(selectedWindow && selectedWindow.start_ts || 0) || 0;
    const selectedEnd = parseFloat(selectedWindow && selectedWindow.end_ts || 0) || 0;
    const selectedWindowId = String(selectedWindow && selectedWindow.window_id || 'passive_normal_window');
    const policyActionId = String(policy.policy_action_id || '');
    const policySlotId = String(policy.policy_slot_id || '');
    const bindingKeys = Object.keys(binding).sort();
    const expectedBindingKeys = [
        'action',
        'end_ts',
        'policy_action_id',
        'policy_slot_id',
        'schema',
        'selected_end_ts',
        'selected_start_ts',
        'start_ts',
        'window_id'
    ].sort();
    const sha256Id = value => /^sha256:[0-9a-f]{64}$/.test(String(value || ''));
    const strongBinding = binding.schema === 'direct_marketing_passive_normal_binding_v1'
        && String(selectedWindow && selectedWindow.action || '').toLowerCase() === 'eco_plus_house_supply'
        && sha256Id(policyActionId)
        && sha256Id(policySlotId)
        && policyActionId === 'sha256:b2744e2b4da4e6d15c4ae3bdf7146bce8de245dfaff1025b3cbcffb678f1820d'
        && binding.policy_action_id === policyActionId
        && binding.policy_slot_id === policySlotId
        && binding.action === 'eco_plus_house_supply'
        && policyBounds.valid
        && selectedStart > 0
        && selectedEnd > selectedStart
        && selectedStart <= start
        && end <= selectedEnd
        && binding.start_ts === policyBounds.start
        && binding.end_ts === policyBounds.end
        && binding.selected_start_ts === selectedStart
        && binding.selected_end_ts === selectedEnd
        && binding.window_id === selectedWindowId
        && JSON.stringify(bindingKeys) === JSON.stringify(expectedBindingKeys)
        && Math.abs(parseFloat(budget.charge_budget_w || 0) || 0) <= 0.000001
        && Math.abs(parseFloat(budget.export_budget_w || 0) || 0) <= 0.000001;
    const commonContract = policy.schema === 'direct_marketing_policy_v1'
        && policy.blocked === false
        && policy.commands_allowed === false
        && String(policy.dv_target_state || '').toUpperCase() === 'NORMAL'
        && String(policy.source_action || '').toLowerCase() === 'eco_plus_house_supply'
        && Object.prototype.hasOwnProperty.call(policy, 'executable_action')
        && policy.executable_action === null;
    return commonContract && strongBinding
        ? {policy, effectContract: effectContract || 'LEGACY_AUTO_FRAME_PASSTHROUGH'}
        : null;
}

function directMarketingTimelineSlotCount(start, end) {
    const slotDuration = start < 100000000000 ? 900 : 900000;
    return Math.max(1, Math.round((end - start) / slotDuration));
}

function directMarketingTimelineClip(item, start, end) {
    const slotCount = directMarketingTimelineSlotCount(start, end);
    const sourceEntry = item && item.windowEntry && typeof item.windowEntry === 'object'
        ? item.windowEntry
        : {};
    const currentSliceStart = parseFloat(sourceEntry._current_slice_start_ts || 0) || 0;
    const currentSliceEnd = parseFloat(sourceEntry._current_slice_end_ts || 0) || 0;
    const current = sourceEntry.current === true && (
        !(currentSliceStart > 0 && currentSliceEnd > currentSliceStart)
        || (currentSliceStart < end && start < currentSliceEnd)
    );
    return {
        windowEntry: {
            ...sourceEntry,
            start_ts: start,
            end_ts: end,
            start_t: mobileStorageTime(start),
            end_t: mobileStorageTime(end),
            current,
            slot_count: slotCount,
            _compacted_slot_count: slotCount
        },
        execution: {...(item.execution || {})}
    };
}

function directMarketingTimelineMergeSignature(item) {
    const entry = item && item.windowEntry && typeof item.windowEntry === 'object'
        ? item.windowEntry
        : {};
    const ignored = new Set([
        'start_ts',
        'end_ts',
        'start_t',
        'end_t',
        'slot_count',
        '_compacted_slot_count',
        '_display_compaction'
    ]);
    const semanticEntry = Object.fromEntries(
        Object.entries(entry).filter(([field]) => !ignored.has(field))
    );
    return JSON.stringify(directMarketingExactDisplayValue({
        windowEntry: semanticEntry,
        execution: item && item.execution && typeof item.execution === 'object'
            ? item.execution
            : {}
    }));
}

function directMarketingTimelineEvidence(start, end, overlap = false) {
    const slotCount = directMarketingTimelineSlotCount(start, end);
    const blockReason = overlap
        ? 'EVIDENCE_LIMIT: überlappende wirksame Fenster'
        : 'EVIDENCE_LIMIT: keine eindeutige wirksame Slotprojektion';
    return {
        windowEntry: {
            action: 'direct_marketing_plan_evidence_gap',
            reason: overlap ? 'plan_projection_overlap' : 'plan_projection_gap',
            start_ts: start,
            end_ts: end,
            start_t: mobileStorageTime(start),
            end_t: mobileStorageTime(end),
            slot_count: slotCount,
            _compacted_slot_count: slotCount,
            effect_contract: 'EVIDENCE_LIMIT'
        },
        execution: {
            action: 'direct_marketing_plan_evidence_gap',
            selected: false,
            executable: false,
            commandsAllowed: false,
            planned: false,
            passivePlanHint: false,
            passivePlanEffective: false,
            active: false,
            executionBlocked: true,
            executorGateBound: false,
            physicalActionBound: false,
            holdActive: false,
            runtimeHoldActive: false,
            effectiveUntilTs: 0,
            candidateOnly: false,
            marketOnly: false,
            blockReason
        }
    };
}

function directMarketingCompleteOperationalTimeline(
    operational,
    decorated,
    monitor,
    plan,
    physicalAction = null
) {
    plan = plan && typeof plan === 'object' ? plan : {};
    const base = (Array.isArray(operational) ? operational : [])
        .map((item, index) => ({item, index, bounds: directMarketingWindowBounds(item)}))
        .filter(record => record.bounds.valid);
    const rawPlanBounds = (Array.isArray(plan.windows) ? plan.windows : [])
        .map(windowEntry => directMarketingWindowBounds({windowEntry}))
        .filter(bounds => bounds.valid);
    const policyBounds = directMarketingPolicyEnvelopeValid(plan)
        ? (Array.isArray(plan.policy_timeline) ? plan.policy_timeline : [])
            .filter(policy => policy && policy.schema === 'direct_marketing_policy_v1')
            .map(directMarketingPolicyBounds)
            .filter(bounds => bounds.valid)
        : [];
    const horizonBounds = rawPlanBounds.concat(policyBounds);
    if (!horizonBounds.length) {
        return base
            .sort((left, right) => left.bounds.start - right.bounds.start || left.bounds.end - right.bounds.end)
            .map(record => record.item);
    }
    const horizonStart = Math.min(...horizonBounds.map(bounds => bounds.start));
    const horizonEnd = Math.max(...horizonBounds.map(bounds => bounds.end));
    if (!(horizonStart > 0 && horizonEnd > horizonStart)) return base.map(record => record.item);

    const boundaries = new Set([horizonStart, horizonEnd]);
    const addBounds = bounds => {
        if (bounds.valid && bounds.end > horizonStart && bounds.start < horizonEnd) {
            boundaries.add(Math.max(horizonStart, bounds.start));
            boundaries.add(Math.min(horizonEnd, bounds.end));
        }
    };
    base.forEach(record => addBounds(record.bounds));
    base.forEach(record => {
        const entry = record.item && record.item.windowEntry && typeof record.item.windowEntry === 'object'
            ? record.item.windowEntry
            : {};
        addBounds({
            start: parseFloat(entry._current_slice_start_ts || 0) || 0,
            end: parseFloat(entry._current_slice_end_ts || 0) || 0,
            valid: (parseFloat(entry._current_slice_start_ts || 0) || 0) > 0
                && (parseFloat(entry._current_slice_end_ts || 0) || 0)
                    > (parseFloat(entry._current_slice_start_ts || 0) || 0)
        });
    });
    policyBounds.forEach(addBounds);
    (Array.isArray(decorated) ? decorated : []).forEach(item => {
        if (item && item.execution && item.execution.marketOnly !== true) {
            addBounds(directMarketingWindowBounds(item));
        }
    });
    const points = Array.from(boundaries).sort((left, right) => left - right);
    const atomic = [];
    for (let index = 1; index < points.length; index += 1) {
        const start = points[index - 1];
        const end = points[index];
        if (!(end > start)) continue;
        const rawCovering = base
            .filter(record => record.bounds.start <= start && end <= record.bounds.end);
        if (rawCovering.length > 1) {
            atomic.push({
                item: directMarketingTimelineEvidence(start, end, true),
                mergeKey: 'evidence:OVERLAPPING_EFFECTIVE_WINDOWS'
            });
            continue;
        }
        const covering = rawCovering
            .map(record => {
                const clipped = directMarketingTimelineClip(record.item, start, end);
                clipped.execution = directMarketingWindowExecutionContract(
                    clipped.windowEntry,
                    monitor,
                    plan,
                    physicalAction
                );
                return {record, clipped};
            })
            .filter(candidate => (
                candidate.clipped.execution.marketOnly !== true
                && (
                    candidate.clipped.execution.candidateOnly !== true
                    || candidate.clipped.execution.active === true
                )
            ));
        if (covering.length === 1) {
            atomic.push({
                item: covering[0].clipped,
                mergeKey: `operational:${covering[0].record.index}:${directMarketingTimelineMergeSignature(covering[0].clipped)}`
            });
            continue;
        }
        const passive = directMarketingPassiveNormalPolicyForSegment(plan, start, end);
        if (passive) {
            const slotCount = directMarketingTimelineSlotCount(start, end);
            const windowEntry = {
                action: 'eco_plus_house_supply',
                reason: 'policy_passive_house_supply',
                start_ts: start,
                end_ts: end,
                start_t: mobileStorageTime(start),
                end_t: mobileStorageTime(end),
                slot_count: slotCount,
                _compacted_slot_count: slotCount,
                effect_contract: passive.effectContract,
                _display_compaction: 'policy_passive_house_supply'
            };
            atomic.push({
                item: {
                    windowEntry,
                    execution: directMarketingWindowExecutionContract(
                        windowEntry,
                        monitor,
                        plan,
                        physicalAction
                    )
                },
                mergeKey: `passive:${passive.effectContract}`
            });
            continue;
        }
        atomic.push({
            item: directMarketingTimelineEvidence(start, end, false),
            mergeKey: 'evidence:NO_EFFECTIVE_WINDOW_CONTRACT'
        });
    }

    const compacted = atomic.reduce((result, segment) => {
        const previous = result[result.length - 1];
        const previousBounds = previous ? directMarketingWindowBounds(previous.item) : {valid: false};
        const segmentBounds = directMarketingWindowBounds(segment.item);
        if (
            !previous
            || previous.mergeKey !== segment.mergeKey
            || !previousBounds.valid
            || !segmentBounds.valid
            || previousBounds.end !== segmentBounds.start
        ) {
            result.push(segment);
            return result;
        }
        previous.item = directMarketingTimelineClip(
            previous.item,
            previousBounds.start,
            segmentBounds.end
        );
        return result;
    }, []);
    return directMarketingCompactNeutralHouseSupplyWindows(compacted.map(segment => segment.item));
}

function directMarketingWindowPriceSignature(item) {
    const entry = item && item.windowEntry && typeof item.windowEntry === 'object'
        ? item.windowEntry
        : {};
    const value = field => {
        const parsed = parseFloat(entry[field]);
        return Number.isFinite(parsed) ? parsed : null;
    };
    return JSON.stringify([
        value('avg_billing_ct'),
        value('avg_market_ct'),
        value('avg_net_sell_ct'),
        value('net_sell_ct')
    ]);
}

function directMarketingNeutralHoldProjection(item) {
    const entry = item && item.windowEntry && typeof item.windowEntry === 'object'
        ? item.windowEntry
        : {};
    const action = String(entry.action || '').toLowerCase();
    const reason = String(entry.reason || '').trim().toLowerCase();
    return directMarketingIsHoldAction(action)
        && (
            ['policy_charge_block_wait', 'direct_marketing_charge_block_wait', 'charge_block_wait'].includes(action)
            || reason === 'neutral dv wait slot'
            || reason === 'headroom reservation hold'
    );
}

function directMarketingHoldCompactionSignature(item, includeSlotWindowId = true) {
    const entry = item && item.windowEntry && typeof item.windowEntry === 'object'
        ? item.windowEntry
        : {};
    const execution = item && item.execution && typeof item.execution === 'object'
        ? item.execution
        : {};
    return JSON.stringify(directMarketingExactDisplayValue({
        action: String(entry.action || '').toLowerCase(),
        reason: String(entry.reason || '').toLowerCase(),
        windowId: includeSlotWindowId ? String(entry.window_id || '') : '',
        businessWindowId: String(entry.business_window_id || ''),
        sourceWindowId: String(
            entry.source_window_id
            || entry.headroom_source_window_id
            || ''
        ),
        marketWindowId: String(entry.market_window_id || ''),
        exportPlateauId: String(entry.export_plateau_id || ''),
        exportSegmentId: String(entry.export_segment_id || ''),
        reservationId: String(entry.reservation_id || ''),
        targetState: String(execution.targetState || '').toUpperCase(),
        selected: execution.selected === true,
        executable: execution.executable === true,
        commandsAllowed: execution.commandsAllowed === true,
        planned: execution.planned === true,
        passivePlanHint: execution.passivePlanHint === true,
        passivePlanEffective: execution.passivePlanEffective === true,
        active: execution.active === true,
        executionBlocked: execution.executionBlocked === true,
        physicalActionBound: execution.physicalActionBound === true,
        executorGateBound: execution.executorGateBound === true,
        holdActive: execution.holdActive === true,
        runtimeHoldActive: execution.runtimeHoldActive === true,
        candidateOnly: execution.candidateOnly === true,
        exportBudgetW: Number.isFinite(parseFloat(execution.exportBudgetW))
            ? parseFloat(execution.exportBudgetW)
            : null,
        chargeBudgetW: Number.isFinite(parseFloat(execution.chargeBudgetW))
            ? parseFloat(execution.chargeBudgetW)
            : null,
        blockReason: String(execution.blockReason || '')
    }));
}

function directMarketingMergeHoldItems(left, right, keepPreferredBounds = false) {
    const score = item => {
        const entry = item.windowEntry || {};
        const bounds = directMarketingWindowBounds(item);
        const priced = directMarketingWindowPriceSignature(item) !== '[null,null,null,null]';
        return (priced ? 100 : 0)
            + (String(entry.action || '').toLowerCase() === 'eco_plus_negative_headroom_hold' ? 20 : 0)
            + (entry.current === true ? 10 : 0)
            + (item.execution && item.execution.runtimeHoldActive ? 5 : 0)
            + (bounds.valid ? Math.min(4, (bounds.end - bounds.start) / 900000) : 0);
    };
    const preferred = score(right) > score(left) ? right : left;
    const secondary = preferred === left ? right : left;
    const preferredBounds = directMarketingWindowBounds(preferred);
    const leftBounds = directMarketingWindowBounds(left);
    const rightBounds = directMarketingWindowBounds(right);
    const entry = {
        ...(secondary.windowEntry || {}),
        ...(preferred.windowEntry || {})
    };
    if (
        !keepPreferredBounds
        && leftBounds.valid
        && rightBounds.valid
    ) {
        entry.start_ts = Math.min(leftBounds.start, rightBounds.start);
        entry.end_ts = Math.max(leftBounds.end, rightBounds.end);
        entry.start_t = mobileStorageTime(entry.start_ts);
        entry.end_t = mobileStorageTime(entry.end_ts);
    } else if (preferredBounds.valid) {
        entry.start_ts = preferredBounds.start;
        entry.end_ts = preferredBounds.end;
        entry.start_t = preferred.windowEntry.start_t || mobileStorageTime(entry.start_ts);
        entry.end_t = preferred.windowEntry.end_t || mobileStorageTime(entry.end_ts);
    }
    entry.current = left.windowEntry.current === true || right.windowEntry.current === true;
    const mergedBounds = directMarketingWindowBounds({windowEntry: entry});
    const slotDuration = mergedBounds.start < 100000000000 ? 900 : 900000;
    entry._compacted_slot_count = mergedBounds.valid
        ? Math.max(1, Math.round((mergedBounds.end - mergedBounds.start) / slotDuration))
        : Math.max(1, parseInt(left.windowEntry._compacted_slot_count || 1, 10))
            + Math.max(1, parseInt(right.windowEntry._compacted_slot_count || 1, 10));
    entry.slot_count = entry._compacted_slot_count;
    const leftExecution = left.execution || {};
    const rightExecution = right.execution || {};
    const blockReasons = [leftExecution.blockReason, rightExecution.blockReason]
        .map(value => String(value || '').trim())
        .filter(Boolean)
        .filter((value, index, values) => values.indexOf(value) === index);
    return {
        windowEntry: entry,
        execution: {
            ...secondary.execution,
            ...preferred.execution,
            selected: leftExecution.selected === true || rightExecution.selected === true,
            executable: leftExecution.executable === true || rightExecution.executable === true,
            commandsAllowed: leftExecution.commandsAllowed === true || rightExecution.commandsAllowed === true,
            planned: leftExecution.planned === true || rightExecution.planned === true,
            passivePlanHint: leftExecution.passivePlanHint === true || rightExecution.passivePlanHint === true,
            passivePlanEffective: leftExecution.passivePlanEffective === true || rightExecution.passivePlanEffective === true,
            active: leftExecution.active === true || rightExecution.active === true,
            holdActive: leftExecution.holdActive === true || rightExecution.holdActive === true,
            runtimeHoldActive: leftExecution.runtimeHoldActive === true || rightExecution.runtimeHoldActive === true,
            candidateOnly: leftExecution.candidateOnly === true && rightExecution.candidateOnly === true,
            effectiveUntilTs: Math.max(
                parseFloat(leftExecution.effectiveUntilTs || 0) || 0,
                parseFloat(rightExecution.effectiveUntilTs || 0) || 0
            ),
            blockReason: blockReasons.join('; ')
        }
    };
}

function directMarketingCompactHoldWindows(items) {
    const ordered = Array.isArray(items) ? items.slice().sort((left, right) => {
        const leftBounds = directMarketingWindowBounds(left);
        const rightBounds = directMarketingWindowBounds(right);
        return leftBounds.start - rightBounds.start || leftBounds.end - rightBounds.end;
    }) : [];
    const deduplicated = [];
    ordered.forEach(item => {
        if (!directMarketingIsHoldAction(item && item.windowEntry && item.windowEntry.action)) {
            deduplicated.push(item);
            return;
        }
        const itemBounds = directMarketingWindowBounds(item);
        const matchIndex = deduplicated.findIndex(existing => {
            if (!directMarketingIsHoldAction(existing && existing.windowEntry && existing.windowEntry.action)) return false;
            const itemAction = String(item && item.windowEntry && item.windowEntry.action || '').toLowerCase();
            const existingAction = String(existing && existing.windowEntry && existing.windowEntry.action || '').toLowerCase();
            if (!itemAction || itemAction !== existingAction) return false;
            const existingBounds = directMarketingWindowBounds(existing);
            if (!itemBounds.valid || !existingBounds.valid) return false;
            if (itemBounds.start >= existingBounds.end || existingBounds.start >= itemBounds.end) return false;
            const samePrice = directMarketingWindowPriceSignature(item) === directMarketingWindowPriceSignature(existing);
            const itemUnpriced = directMarketingWindowPriceSignature(item) === '[null,null,null,null]';
            const existingUnpriced = directMarketingWindowPriceSignature(existing) === '[null,null,null,null]';
            const exact = itemBounds.start === existingBounds.start && itemBounds.end === existingBounds.end;
            const itemContained = itemBounds.start >= existingBounds.start && itemBounds.end <= existingBounds.end;
            const existingContained = existingBounds.start >= itemBounds.start && existingBounds.end <= itemBounds.end;
            const sameContract = directMarketingHoldCompactionSignature(item)
                === directMarketingHoldCompactionSignature(existing);
            return sameContract && (
                (exact && samePrice)
                || (directMarketingNeutralHoldProjection(item) && itemContained && (itemUnpriced || samePrice))
                || (directMarketingNeutralHoldProjection(existing) && existingContained && (existingUnpriced || samePrice))
            );
        });
        if (matchIndex < 0) {
            deduplicated.push(item);
            return;
        }
        const existing = deduplicated[matchIndex];
        const keepPreferredBounds = !(
            directMarketingNeutralHoldProjection(existing)
            && directMarketingNeutralHoldProjection(item)
        );
        deduplicated[matchIndex] = directMarketingMergeHoldItems(existing, item, keepPreferredBounds);
    });

    return deduplicated.reduce((result, item) => {
        const previous = result[result.length - 1];
        if (!previous || !directMarketingNeutralHoldProjection(previous) || !directMarketingNeutralHoldProjection(item)) {
            result.push(item);
            return result;
        }
        const previousBounds = directMarketingWindowBounds(previous);
        const itemBounds = directMarketingWindowBounds(item);
        const sameContract = directMarketingWindowPriceSignature(previous)
            === directMarketingWindowPriceSignature(item)
            && directMarketingHoldCompactionSignature(previous, false)
                === directMarketingHoldCompactionSignature(item, false);
        if (
            previousBounds.valid
            && itemBounds.valid
            && Math.abs(itemBounds.start - previousBounds.end) <= 1000
            && sameContract
        ) {
            result[result.length - 1] = directMarketingMergeHoldItems(previous, item);
        } else {
            result.push(item);
        }
        return result;
    }, []);
}

function directMarketingPruneCoveredHoldDiagnostics(items, operational) {
    const activeHolds = (Array.isArray(operational) ? operational : []).filter(item => (
        directMarketingIsHoldAction(item && item.windowEntry && item.windowEntry.action)
        && item.execution
        && item.execution.holdActive === true
        && item.execution.active === true
    ));
    return (Array.isArray(items) ? items : []).filter(item => {
        if (!directMarketingIsHoldAction(item && item.windowEntry && item.windowEntry.action)) return true;
        const bounds = directMarketingWindowBounds(item);
        if (!bounds.valid) return true;
        return !activeHolds.some(activeItem => {
            const activeBounds = directMarketingWindowBounds(activeItem);
            return activeBounds.valid
                && activeBounds.start <= bounds.start
                && bounds.end <= activeBounds.end;
        });
    });
}

function directMarketingActiveHoldDisplay(data) {
    const plan = getDirectMarketingPlan(data);
    const monitor = data && data.direct_marketing_monitor && typeof data.direct_marketing_monitor === 'object'
        ? data.direct_marketing_monitor
        : (data && data.storage_plan_meta && data.storage_plan_meta.direct_marketing_monitor
            && typeof data.storage_plan_meta.direct_marketing_monitor === 'object'
            ? data.storage_plan_meta.direct_marketing_monitor
            : null);
    const physicalAction = directMarketingRuntimePhysicalAction(data);
    const physicalHoldActive = physicalAction && physicalAction.holdActive === true;
    // Eine Planung oder Monitor-Projektion ist noch keine wirksame Speicherregelung.
    // HOLD wird erst angezeigt, wenn der gebundene Storage-Manager-Ausgang samt
    // frischem POWER_SETTINGS-Readback den Ladeblock bestätigt.
    if (!physicalHoldActive) return null;
    const mode = directMarketingModeLabel(
        directMarketingNormalizeMode((monitor && monitor.mode) || (plan && plan.mode))
    );
    const untilTs = Math.max(
        directMarketingTimestampMs(physicalAction && physicalAction.endTs)
    );
    const until = untilTs > 0 ? mobileStorageTime(untilTs) : '';
    const status = `${until && until !== '--' ? 'Laden bis ' + until + ' Uhr gesperrt' : 'Laden gesperrt'} · Entladen erlaubt`;
    return {
        title: 'Speicherplatz halten',
        target: '',
        curve: status,
        status,
        badge: `Speicherregelung · DV ${mode} · bestätigt`,
        source: 'confirmed_runtime'
    };
}

function directMarketingPlannedHoldHint(data = {}) {
    const plan = getDirectMarketingPlan(data);
    const monitor = data && data.direct_marketing_monitor && typeof data.direct_marketing_monitor === 'object'
        ? data.direct_marketing_monitor
        : (data && data.storage_plan_meta && data.storage_plan_meta.direct_marketing_monitor
            && typeof data.storage_plan_meta.direct_marketing_monitor === 'object'
            ? data.storage_plan_meta.direct_marketing_monitor
            : null);
    if (!isDirectMarketingVisible(data, plan, monitor)) return '';
    const monitorAction = String(monitor && monitor.current_action || '').toLowerCase();
    const monitorTargetState = String(monitor && monitor.policy_target_state || '').toUpperCase();
    const monitorPlansHold = Boolean(
        monitor
        && directMarketingIsHoldAction(monitorAction)
        && (
            monitorTargetState === 'CHARGE_BLOCK_WAIT'
            || (monitorTargetState === 'HEADROOM_EXPORT' && monitor.headroom_hold_active === true)
        )
    );
    const plannedWindow = collectDirectMarketingWindows(null, monitor, plan).some(windowEntry => (
        windowEntry
        && windowEntry.current === true
        && directMarketingIsHoldAction(windowEntry.action)
    ));
    return monitorPlansHold || plannedWindow ? 'Geplant: Speicherplatz halten' : '';
}

function storageOperationalDisplay(data = {}) {
    const confirmedHold = directMarketingActiveHoldDisplay(data);
    if (confirmedHold) {
        return {
            state: 'hold',
            label: confirmedHold.title,
            detail: confirmedHold.status,
            badge: confirmedHold.badge,
            color: '#f59e0b',
            active: true,
            holdActive: true,
            confirmed: true,
            hideCurveDetails: true,
            plannedHint: '',
            source: confirmedHold.source
        };
    }

    const stateRaw = String(data.storage_state || '').trim();
    const state = stateRaw.toLowerCase();
    const unconfirmedHoldStates = new Set([
        'direct_marketing_phase5_pv_store_wait',
        'direct_marketing_charge_block_wait',
        'direct_marketing_eco_plus_headroom_hold'
    ]);
    const autoStates = new Set([
        '',
        'auto',
        'normal',
        'auto_night_release',
        'parallel_evening_release',
        'target_reached_auto',
        ...unconfirmedHoldStates
    ]);
    const colors = {
        auto: '#818cf8',
        normal: '#818cf8',
        auto_night_release: '#818cf8',
        parallel_evening_release: '#818cf8',
        target_reached_auto: '#818cf8',
        charge: '#f59e0b',
        discharge: '#10b981',
        idle: '#6b7280',
        price_override: '#06b6d4',
        cheap_grid_charge: '#22c55e',
        grid_charge: '#06b6d4',
        morning_autonomy: '#38bdf8',
        emergency_power: '#ef4444',
        stopped: '#ef4444'
    };
    const plannedHint = directMarketingPlannedHoldHint(data);
    return {
        state: autoStates.has(state) ? 'auto' : state,
        label: autoStates.has(state)
            ? 'AUTO'
            : storageStateDisplayLabel(stateRaw, data.storage_state_label),
        detail: '',
        badge: autoStates.has(state) ? 'E3/DC regelt frei' : '',
        color: colors[state] || '#adb5bd',
        active: !autoStates.has(state),
        holdActive: false,
        confirmed: false,
        hideCurveDetails: false,
        plannedHint,
        source: stateRaw ? 'storage_state' : 'missing'
    };
}

function directMarketingPriceText(windowEntry) {
    const billing = parseFloat(windowEntry && windowEntry.avg_billing_ct);
    const netSell = parseFloat(
        windowEntry && (windowEntry.avg_net_sell_ct ?? windowEntry.net_sell_ct)
    );
    const market = parseFloat(windowEntry && windowEntry.avg_market_ct);
    const marketSource = String(
        windowEntry && windowEntry.market_price_source || ''
    ).trim();
    const marketResolutionMin = parseFloat(
        windowEntry && windowEntry.market_price_resolution_min
    );
    const format = value => value.toLocaleString(
        'de-DE',
        {minimumFractionDigits: 2, maximumFractionDigits: 2}
    ) + ' ct/kWh';
    const parts = [];
    if (Number.isFinite(billing)) parts.push('Abrechnung ' + format(billing));
    if (Number.isFinite(market)) {
        const marketMeta = [];
        if (marketSource) marketMeta.push(marketSource);
        if (Number.isFinite(marketResolutionMin) && marketResolutionMin > 0) {
            marketMeta.push(
                marketResolutionMin.toLocaleString(
                    'de-DE',
                    {maximumFractionDigits: 1}
                ) + ' Min'
            );
        }
        parts.push(
            'Börse ' + format(market)
            + (marketMeta.length ? ' (' + marketMeta.join(' · ') + ')' : '')
        );
    }
    if (Number.isFinite(netSell)) parts.push('Verkauf netto ' + format(netSell));
    return parts.join(' · ');
}

function directMarketingPriceBreakdownHtml(windowEntry, economics) {
    windowEntry = windowEntry && typeof windowEntry === 'object' ? windowEntry : {};
    economics = economics && typeof economics === 'object' ? economics : {};
    const isPvStore = String(windowEntry.action || '').toLowerCase() === 'eco_plus_store_pv_candidate';
    const grossSell = parseFloat(windowEntry.avg_gross_sell_ct ?? windowEntry.gross_sell_ct);
    const feeCost = parseFloat(windowEntry.avg_fee_cost_ct ?? windowEntry.fee_cost_ct);
    const opportunity = isPvStore ? parseFloat(economics.pv_shift_opportunity_ct) : NaN;
    const spread = isPvStore ? parseFloat(economics.pv_shift_spread_ct_per_kwh) : NaN;
    const format = value => value.toLocaleString(
        'de-DE',
        {minimumFractionDigits: 2, maximumFractionDigits: 2}
    ) + ' ct/kWh';
    const parts = [];
    if (Number.isFinite(grossSell)) parts.push('Marktpreis brutto ' + format(grossSell));
    if (Number.isFinite(feeCost)) parts.push('Gebühren ' + format(feeCost));
    if (Number.isFinite(opportunity)) parts.push('Plan-Opportunitätskosten ' + format(opportunity));
    if (Number.isFinite(spread)) parts.push('PV-Verschiebespread ' + format(spread));
    if (!parts.length) return '';
    return `
        <details class="small mt-1">
            <summary class="text-muted" style="cursor:pointer;">Rechenweg</summary>
            <span class="d-block text-muted mt-1">${directMarketingHtmlEscape(parts.join(' | '))}</span>
        </details>
    `;
}

function directMarketingAllocationSummaryHtml(allocation) {
    allocation = allocation && typeof allocation === 'object' ? allocation : {};
    if (allocation.evaluated !== true) return '';
    const nonNegativeNumber = value => {
        const parsed = parseFloat(value);
        return Number.isFinite(parsed) ? Math.max(0, parsed) : null;
    };
    const nonNegativeInteger = value => {
        const parsed = parseInt(value, 10);
        return Number.isFinite(parsed) ? Math.max(0, parsed) : null;
    };
    const requestedWh = nonNegativeNumber(allocation.requested_stored_wh);
    const selectedWh = nonNegativeNumber(allocation.selected_stored_wh);
    const remainingWh = nonNegativeNumber(allocation.remaining_stored_wh);
    const candidateSlots = nonNegativeInteger(allocation.candidate_slot_count);
    const selectedSlots = nonNegativeInteger(allocation.selected_slot_count);
    const marginal = allocation.marginal_slot && typeof allocation.marginal_slot === 'object'
        ? allocation.marginal_slot
        : {};
    const sourceContract = allocation.source_contract && typeof allocation.source_contract === 'object'
        ? allocation.source_contract
        : {};
    const marginalNetSell = parseFloat(marginal.net_sell_ct);
    const summaryParts = [];
    if (selectedWh !== null && requestedWh !== null && requestedWh > 0) {
        summaryParts.push(
            directMarketingKwhText(selectedWh / 1000)
            + ' von '
            + directMarketingKwhText(requestedWh / 1000)
        );
    } else if (selectedWh !== null) {
        summaryParts.push(directMarketingKwhText(selectedWh / 1000));
    } else if (requestedWh !== null && requestedWh > 0) {
        summaryParts.push('Bedarf ' + directMarketingKwhText(requestedWh / 1000));
    }
    if (candidateSlots !== null && candidateSlots > 0 && selectedSlots !== null) {
        summaryParts.push(selectedSlots + '/' + candidateSlots + ' Slots');
    }
    if (Number.isFinite(marginalNetSell)) {
        summaryParts.push(
            'Grenzpreis Verkauf netto '
            + marginalNetSell.toLocaleString(
                'de-DE',
                {minimumFractionDigits: 2, maximumFractionDigits: 2}
            )
            + ' ct/kWh'
        );
    }
    if (!summaryParts.length) return '';
    const allocationText = summaryParts.join(' · ');
    const allocationLine = allocationText ? ('Allokation: ' + allocationText) : '';
    const detailParts = [
        'Negativpreis-Schutz zuerst, innerhalb der Preisklasse Verkauf netto aufsteigend',
        'Slotleistung = Minimum aus Batterielimit, PV-Prognose und Restbedarf'
    ];
    if (sourceContract.aux_ac_used === true) {
        detailParts.push('Quelle E3DC-DC plus ausdrücklich freigegebener Zusatz-WR-AC');
        const dcDeficitWh = nonNegativeNumber(sourceContract.dc_forecast_deficit_wh);
        if (dcDeficitWh !== null && dcDeficitWh > 50) {
            detailParts.push(
                'nachgewiesene DC-Prognoseunterdeckung '
                + directMarketingKwhText(dcDeficitWh / 1000)
            );
        }
    } else {
        detailParts.push('Quelle nur E3DC-DC');
        if (sourceContract.aux_ac_user_release === true) {
            detailParts.push('Zusatz-WR-AC aktuell nicht benötigt oder nicht belastbar freigegeben');
        }
    }
    if (remainingWh !== null && remainingWh > 50) {
        detailParts.push('noch ' + directMarketingKwhText(remainingWh / 1000) + ' offen');
    }
    return `
        <details class="mt-1">
            <summary class="text-info" style="cursor:pointer;">PV-Ladeallokation: ${directMarketingHtmlEscape(allocationText)}</summary>
            <span class="d-block text-muted mt-1">${directMarketingHtmlEscape(detailParts.join(' | '))}</span>
        </details>
    `;
}

function directMarketingWindowDurationH(windowEntry) {
    if (!windowEntry || typeof windowEntry !== 'object') return null;
    const start = parseFloat(windowEntry.start_ts || 0);
    const end = parseFloat(windowEntry.end_ts || 0);
    if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
    return (end - start) / 3600000;
}

function directMarketingWindowDisplayKwh(windowEntry) {
    const selectedWh = parseFloat(windowEntry && windowEntry.export_segment_selected_wh);
    const raw = parseFloat(windowEntry && windowEntry.theoretical_kwh);
    const power = parseFloat(windowEntry && windowEntry.max_power_w);
    const durationH = directMarketingWindowDurationH(windowEntry);
    const powerKwh = Number.isFinite(power) && power > 0 && Number.isFinite(durationH) && durationH > 0
        ? (power * durationH) / 1000
        : null;
    if (Number.isFinite(selectedWh) && selectedWh > 0) {
        const selectedKwh = selectedWh / 1000;
        return powerKwh !== null ? Math.min(selectedKwh, powerKwh) : selectedKwh;
    }
    if (Number.isFinite(raw) && raw > 0) {
        if (powerKwh !== null && raw > powerKwh + 0.05) return powerKwh;
        return raw;
    }
    return powerKwh !== null && powerKwh > 0 ? powerKwh : null;
}

function directMarketingEnergyText(windowEntry) {
    const parts = [];
    const kwh = directMarketingWindowDisplayKwh(windowEntry);
    const power = parseFloat(windowEntry && windowEntry.max_power_w);
    if (Number.isFinite(kwh) && kwh > 0) parts.push(directMarketingKwhText(kwh));
    if (Number.isFinite(power) && power > 0) parts.push(mobileStoragePower(power));
    return parts.join(' bei ');
}

function directMarketingSegmentBudgetText(windowEntry) {
    if (!windowEntry || typeof windowEntry !== 'object') return '';
    const source = String(windowEntry.export_segment_budget_source || '').toLowerCase();
    if (!source) return '';
    const nextRecharge = windowEntry.export_segment_next_recharge_ts
        ? mobileStorageTime(windowEntry.export_segment_next_recharge_ts)
        : '';
    const segmentStart = windowEntry.export_segment_start_ts
        ? mobileStorageTime(windowEntry.export_segment_start_ts)
        : '';
    const segmentEnd = windowEntry.export_segment_end_ts
        ? mobileStorageTime(windowEntry.export_segment_end_ts)
        : '';
    if (source === 'current_soc') {
        return nextRecharge
            ? 'Budget: aktueller Akku bis PV-Speichern ' + nextRecharge
            : 'Budget: aktueller Akku';
    }
    if (source.includes('forecast_pv_surplus')) {
        return segmentStart
            ? 'Budget: PV-Nachladung ab ' + segmentStart
            : 'Budget: PV-Nachladung';
    }
    if (segmentEnd) return 'Budgetfenster bis ' + segmentEnd;
    return '';
}

function directMarketingWindowRowHtml(windowEntry, economics, executionContract = null) {
    executionContract = executionContract || {};
    const visual = directMarketingWindowVisual(windowEntry.action, executionContract);
    const {start, end} = directMarketingPlanWindowTimes(windowEntry);
    const action = directMarketingActionLabel(windowEntry.action);
    const actionDetail = action === visual.label ? '' : action;
    const price = directMarketingPriceText(windowEntry);
    const energy = directMarketingEnergyText(windowEntry);
    const spread = directMarketingProfitText(directMarketingSpreadForAction(windowEntry.action, economics));
    const spreadLabel = directMarketingSpreadLabelForAction(windowEntry.action);
    const reason = directMarketingReasonLabel(windowEntry.reason);
    const compactedSlotCount = Math.max(
        1,
        parseInt(windowEntry._compacted_slot_count || windowEntry.slot_count || 1, 10)
    );
    const compactedSlots = compactedSlotCount > 1
        ? compactedSlotCount + ' zusammenhängende Viertelstunden'
        : '';
    const budget = directMarketingSegmentBudgetText(windowEntry);
    const effectiveUntil = executionContract.holdActive && executionContract.effectiveUntilTs
        ? mobileStorageTime(executionContract.effectiveUntilTs)
        : '';
    const holdEffect = executionContract.holdActive
        ? (
            executionContract.runtimeHoldActive
                ? `Laden gesperrt${effectiveUntil && effectiveUntil !== '--' ? ' bis ' + effectiveUntil + ' Uhr' : ''}; Entladen bleibt erlaubt`
                : `Laden in diesem Planabschnitt gesperrt${effectiveUntil && effectiveUntil !== '--' ? ' bis ' + effectiveUntil + ' Uhr' : ''}; Entladen bleibt erlaubt`
        )
        : '';
    const planInterval = executionContract.runtimeHoldActive
        && start
        && end
        ? `Planabschnitt ${start}-${end}`
        : '';
    const marginClass = String(windowEntry.margin_class || windowEntry.market_window_margin_class || '');
    const marginSummary = executionContract.marketOnly
        ? (marginClass === 'mixed'
            ? `gemischter Netto-Grenzerlös: ${parseInt(windowEntry.positive_margin_slot_count || 0, 10)} Slot(s) positiv, ${parseInt(windowEntry.nonpositive_margin_slot_count || 0, 10)} nicht positiv`
            : (marginClass === 'positive'
                ? 'Netto-Grenzerlös in allen Slots positiv: Einspeisung wirtschaftlich zulässig'
                : (marginClass === 'nonpositive'
                    ? 'Netto-Grenzerlös nicht positiv: Einspeisung begrenzen'
                    : (marginClass.includes('invalid') ? 'Abrechnungsvertrag unvollständig: Einspeisung fail-closed begrenzen' : ''))))
        : '';
    const exportConstraint = windowEntry.hard_export_limit_active
        || windowEntry.export_constraint_class === 'negative_hard'
        || windowEntry.export_constraint_class === 'negative_net_revenue_hard'
        || windowEntry.export_constraint_class === 'negative_margin_invalid_hard'
        ? `Netto-Grenzerlös nicht positiv/ungültig | Exportlimit ${Math.max(0, parseInt(windowEntry.hard_export_limit_w ?? windowEntry.curtail_export_limit_w ?? 0, 10) || 0).toLocaleString('de-DE')} W angefordert`
        : (windowEntry.export_constraint_class === 'eeg_soft' || windowEntry.pv_store_price_class === 'eeg_soft' || windowEntry.pv_store_soft_threshold
            ? 'EEG-Schwelle weich | Einspeisung zulässig'
            : '');
    const executionState = executionContract.marketOnly
        ? 'reines Marktfenster; PV_STORE und Exportfreigabe werden getrennt geplant'
        : executionContract.executionBlocked
        ? 'aktuell nicht ausgeführt: ' + (executionContract.blockReason || 'keine gebundene Runtime-Ausführung')
        : executionContract.holdActive
        ? (executionContract.runtimeHoldActive ? 'Regelwirkung aktiv' : 'zur Ausführung eingeplant')
        : executionContract.active
        ? 'zur Ausführung freigegeben'
        : executionContract.passivePlanEffective
        ? 'Kurvenladung im E3DC-AUTO-Rahmen bestätigt'
        : executionContract.passivePlanHint && executionContract.planned
        ? 'Kurvenladung im E3DC-AUTO-Rahmen eingeplant'
        : executionContract.planned
        ? 'zur Ausführung eingeplant'
        : (executionContract.candidateOnly ? 'nicht freigegeben: ' + (executionContract.blockReason || 'keine ausführbare Planbindung') : '');
    const detailParts = [holdEffect, planInterval, price, energy, marginSummary, spread ? spreadLabel + ' ' + spread : '', reason, compactedSlots, exportConstraint, budget, executionState].filter(Boolean);
    const priceBreakdown = directMarketingPriceBreakdownHtml(windowEntry, economics);
    const titleParts = [
        visual.label + ': ' + (start || '--') + ' bis ' + (end || '--'),
        actionDetail,
        ...detailParts
    ].filter(Boolean);
    const passiveHouseSupply = String(windowEntry.action || '').toLowerCase()
        === 'eco_plus_house_supply'
        && executionContract.executionBlocked !== true
        && executionContract.candidateOnly !== true;
    const currentBadge = windowEntry.current === true || executionContract.runtimeHoldActive
        ? (executionContract.runtimeHoldActive
            ? '<span class="badge bg-warning bg-opacity-10 text-warning border border-warning border-opacity-25">jetzt wirksam</span>'
            : executionContract.passivePlanEffective
            ? '<span class="badge bg-success bg-opacity-10 text-success border border-success border-opacity-25">AUTO-Rahmen bestätigt</span>'
            : executionContract.passivePlanHint && executionContract.planned
            ? '<span class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-25">AUTO-Kurvenrahmen geplant</span>'
            : executionContract.active
            ? '<span class="badge bg-success bg-opacity-10 text-success border border-success border-opacity-25">aktuell und freigegeben</span>'
            : passiveHouseSupply
            ? '<span class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-25">E3DC-AUTO aktiv</span>'
            : '<span class="badge bg-secondary bg-opacity-10 text-secondary border border-secondary border-opacity-25">aktuell nicht ausgeführt</span>')
        : '';
    const displayStart = executionContract.runtimeHoldActive ? 'jetzt' : start;
    const displayEnd = executionContract.runtimeHoldActive && effectiveUntil && effectiveUntil !== '--'
        ? effectiveUntil
        : end;

    return `
        <div class="d-flex gap-2 align-items-start py-2 border-top border-secondary border-opacity-10" title="${directMarketingHtmlEscape(titleParts.join('\n'))}">
            <span class="d-inline-flex align-items-center justify-content-center rounded-circle flex-shrink-0" style="width:24px;height:24px;border:1px solid ${visual.color};color:${visual.color};background:${visual.color}18;">
                <i class="fas ${visual.icon}" style="font-size:0.72rem;"></i>
            </span>
            <span class="flex-grow-1">
                <span class="d-flex flex-wrap gap-2 align-items-center">
                    <span class="fw-bold" style="color:${visual.color};">${directMarketingHtmlEscape(visual.label)}</span>
                    <span class="text-body">${directMarketingHtmlEscape((displayStart || '--') + '-' + (displayEnd || '--'))}</span>
                    ${currentBadge}
                    ${actionDetail ? `<span class="text-muted">${directMarketingHtmlEscape(actionDetail)}</span>` : ''}
                </span>
                <span class="d-block text-muted">${directMarketingHtmlEscape(detailParts.join(' | ') || 'noch ohne Detailwerte')}</span>
                ${priceBreakdown}
            </span>
        </div>
    `;
}

function renderDirectMarketingCurveSection(data = null) {
    const section = document.getElementById('sc-direct-marketing-section');
    if (!section) return;
    const summaryEl = document.getElementById('sc-direct-marketing-summary');
    const windowsEl = document.getElementById('sc-direct-marketing-windows');
    data = data || window._storageLiveData || {};
    const plan = getDirectMarketingPlan(data);
    const monitor = getDirectMarketingMonitor(data);
    const report = getDirectMarketingDailyReport(data);
    const marketValueSolar = getMarketValueSolar(data);
    const marketValueSolarTextOnly = formatMarketValueSolarSummary(marketValueSolar, false);
    const marketValueSolarTitleOnly = formatMarketValueSolarTitle(marketValueSolar);

    if (!isDirectMarketingVisible(data, plan, monitor)) {
        if (marketValueSolarTextOnly) {
            section.style.display = '';
            if (summaryEl) {
                summaryEl.innerHTML = `
                    <div class="d-flex flex-wrap gap-2 align-items-center mb-2" title="${directMarketingHtmlEscape(marketValueSolarTitleOnly)}">
                        <span class="badge bg-info bg-opacity-10 text-info border border-info border-opacity-25">Analyse</span>
                        <span class="text-info fw-bold">${directMarketingHtmlEscape(marketValueSolarTextOnly)}</span>
                        <span class="text-muted">keine Regelwirkung</span>
                    </div>
                    <div class="text-muted">Der Monitor berechnet den vorläufigen Monatswert-Solar-Trend aus Solar-Hochrechnung und Spotpreisen. Er steuert keinen Speicher- oder Netzbefehl.</div>
                `;
            }
            if (windowsEl) windowsEl.innerHTML = '';
        } else {
            section.style.display = 'none';
            if (summaryEl) summaryEl.innerHTML = '';
            if (windowsEl) windowsEl.innerHTML = '';
        }
        return;
    }

    const windows = collectDirectMarketingWindows(report, monitor, plan);
    const modeRaw = directMarketingNormalizeMode((monitor && monitor.mode) || (plan && plan.mode));
    section.style.display = '';
    const economics = (monitor && monitor.economics && typeof monitor.economics === 'object') ? monitor.economics
        : ((report && report.latest_economics && typeof report.latest_economics === 'object') ? report.latest_economics
            : ((plan && plan.economics && typeof plan.economics === 'object') ? plan.economics : {}));
    const modeLabel = directMarketingModeLabel(modeRaw || (report && report.latest_summary && report.latest_summary.mode));
    const shadow = (monitor && (monitor.shadow === true || monitor.commands_allowed === false))
        || (plan && plan.flags && plan.flags.commands_allowed === false);
    const physicalAction = directMarketingRuntimePhysicalAction(data);
    const active = Boolean(physicalAction) || (monitor && monitor.active === true);
    const stateText = active ? 'aktiv'
        : (shadow ? 'Beobachtung, keine Befehle'
            : ((monitor && monitor.state === 'waiting') ? 'wartet auf Fenster' : 'beobachtet'));
    const stateColor = active ? 'text-success' : (shadow ? 'text-warning' : 'text-muted');
    const owner = (monitor && monitor.plan_owner) || (plan && plan.plan_owner) || 'direct_marketing';
    const reasonGroups = directMarketingReasonGroups(monitor);
    const uniqueReasons = values => values.filter(Boolean).filter((label, idx, arr) => arr.indexOf(label) === idx).slice(0, 4);
    const uniqueBlockers = uniqueReasons(reasonGroups.blockers);
    const uniqueCandidates = uniqueReasons(reasonGroups.candidates);
    const uniqueDiagnostics = uniqueReasons(reasonGroups.diagnostics);
    const pvSpread = directMarketingProfitText(economics.pv_shift_spread_ct_per_kwh);
    const gridSpread = directMarketingProfitText(economics.grid_spread_ct_per_kwh);
    const spreadParts = [];
    if (pvSpread) spreadParts.push('<span>PV-Verschiebespread <span class="' + (parseFloat(economics.pv_shift_spread_ct_per_kwh) >= 0 ? 'text-success' : 'text-danger') + ' fw-bold">' + directMarketingHtmlEscape(pvSpread) + '</span></span>');
    if (gridSpread) spreadParts.push('<span>Netz-Verschiebespread <span class="' + (parseFloat(economics.grid_spread_ct_per_kwh) >= 0 ? 'text-success' : 'text-danger') + ' fw-bold">' + directMarketingHtmlEscape(gridSpread) + '</span></span>');
    const reportParts = [];
    if (report && typeof report === 'object') {
        const selectedExportKwh = directMarketingSelectedExportKwh(plan);
        const wearBudget = plan && plan.battery_wear_budget && typeof plan.battery_wear_budget === 'object'
            ? plan.battery_wear_budget
            : {};
        const dailyBudgetKwh = Math.max(0, parseFloat(wearBudget.daily_export_limit_wh || 0) || 0) / 1000;
        const legacyTheoreticalExportKwh = parseFloat(report.theoretical_export_kwh || 0);
        const gridKwh = parseFloat(report.theoretical_grid_charge_kwh || 0);
        const profit = parseFloat(report.theoretical_window_profit_eur || 0);
        const realExportKwh = parseFloat(report.real_export_kwh || 0);
        const realPvStoreKwh = parseFloat(report.real_pv_store_kwh || 0);
        const realRevenue = parseFloat(report.real_export_revenue_eur || 0);
        if (realExportKwh > 0) reportParts.push('Ist-Verkauf ' + directMarketingKwhText(realExportKwh));
        if (realPvStoreKwh > 0) reportParts.push('Ist-PV-Speichern ' + directMarketingKwhText(realPvStoreKwh));
        if (realRevenue > 0) reportParts.push('Ist-Erlös ' + realRevenue.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' EUR');
        if (selectedExportKwh > 0) reportParts.push('Ausgewählter Plan-Verkauf ' + directMarketingKwhText(selectedExportKwh));
        if (dailyBudgetKwh > 0) reportParts.push('Theoretisches Tagesbudget ' + directMarketingKwhText(dailyBudgetKwh));
        else if (legacyTheoreticalExportKwh > 0) reportParts.push('Theoretisches Tagesbudget ' + directMarketingKwhText(legacyTheoreticalExportKwh));
        if (gridKwh > 0) reportParts.push('Plan-Netzladen ' + directMarketingKwhText(gridKwh));
        if (profit > 0) reportParts.push('Plan-Fensterwert ' + profit.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' EUR');
    }
    const marketValueSolarText = formatMarketValueSolarSummary(marketValueSolar, false);
    const marketValueSolarTitle = formatMarketValueSolarTitle(marketValueSolar);
    if (marketValueSolarText) reportParts.push(marketValueSolarText);
    const allocationHtml = directMarketingAllocationSummaryHtml(
        plan && plan.pv_store_allocation
    );

    if (summaryEl) {
        summaryEl.innerHTML = `
            <div class="d-flex flex-wrap gap-2 align-items-center mb-2">
                <span class="badge bg-success bg-opacity-10 text-success border border-success border-opacity-25">${directMarketingHtmlEscape(modeLabel)}</span>
                <span class="${stateColor} fw-bold">${directMarketingHtmlEscape(stateText)}</span>
                <span class="text-muted">Regler ${directMarketingHtmlEscape(owner)}</span>
                ${spreadParts.map(part => '<span class="text-muted">' + part + '</span>').join('')}
            </div>
            <div class="text-muted">Die Entscheidungsmarge ist das harte Freigabegate nach Gebühren, Wirkungsgrad, Batteriealterung und Sicherheitsaufschlag. Der PV-/Netz-Verschiebespread beschreibt dagegen nur den Preisabstand der jeweiligen Energiequelle.</div>
            ${uniqueBlockers.length ? '<div class="mt-1 text-warning">Aktuell blockiert: ' + directMarketingHtmlEscape(uniqueBlockers.join(', ')) + '</div>' : ''}
            ${uniqueCandidates.length ? '<div class="mt-1 text-muted">Nicht ausgewählte Kandidaten: ' + directMarketingHtmlEscape(uniqueCandidates.join(', ')) + '</div>' : ''}
            ${uniqueDiagnostics.length ? '<div class="mt-1 text-info">Allokationshinweise: ' + directMarketingHtmlEscape(uniqueDiagnostics.join(', ')) + '</div>' : ''}
            ${reportParts.length ? '<div class="mt-1 text-muted" title="' + directMarketingHtmlEscape(marketValueSolarTitle) + '">Tagesfenster: ' + directMarketingHtmlEscape(reportParts.join(' | ')) + '</div>' : ''}
            ${allocationHtml}
        `;
    }

    if (windowsEl) {
        const decorated = windows.map(windowEntry => ({
            windowEntry,
            execution: directMarketingWindowExecutionContract(windowEntry, monitor, plan, physicalAction)
        }));
        const marketWindows = decorated.filter(item => item.execution.marketOnly);
        const operationalBase = directMarketingCompactNeutralHouseSupplyWindows(
            directMarketingCompactHoldWindows(
                decorated.filter(item => !item.execution.marketOnly && (!item.execution.candidateOnly || item.execution.active))
            )
        );
        const operational = directMarketingCompleteOperationalTimeline(
            operationalBase,
            decorated,
            monitor,
            plan,
            physicalAction
        );
        const diagnostic = directMarketingPruneCoveredHoldDiagnostics(
            directMarketingCompactHoldWindows(
                decorated.filter(item => item.execution.candidateOnly && !item.execution.active)
            ),
            operational
        );
        const shown = operational.slice(0, 10);
        const hidden = operational.slice(10);
        if (!shown.length) {
            windowsEl.innerHTML = '<div class="border-top border-secondary border-opacity-10 pt-2 text-muted">Keine freigegebenen Verkaufs-, Lade- oder Speicherplatzfenster im aktuellen Plan.</div>';
        } else {
            windowsEl.innerHTML = '<div class="border-top border-secondary border-opacity-10 pt-2 fw-bold text-muted">Wirksamer Plan</div>'
                + shown.map(item => directMarketingWindowRowHtml(item.windowEntry, economics, item.execution)).join('')
                + (hidden.length ? `
                    <details class="mt-1">
                        <summary class="text-muted" style="cursor:pointer;">${hidden.length} weitere Fenster anzeigen</summary>
                        ${hidden.map(item => directMarketingWindowRowHtml(item.windowEntry, economics, item.execution)).join('')}
                    </details>
                ` : '');
        }
        if (diagnostic.length) {
            windowsEl.innerHTML += `
                <details class="mt-2">
                    <summary class="text-muted" style="cursor:pointer;">${diagnostic.length} nicht freigegebene Kandidaten (Diagnose)</summary>
                    ${diagnostic.map(item => directMarketingWindowRowHtml(item.windowEntry, economics, item.execution)).join('')}
                </details>
            `;
        }
        if (marketWindows.length) {
            windowsEl.innerHTML = '<div class="border-top border-secondary border-opacity-10 pt-2 fw-bold text-muted">Bekannte Marktfenster (keine Aktionswirkung)</div>'
                + marketWindows.map(item => directMarketingWindowRowHtml(item.windowEntry, economics, item.execution)).join('')
                + windowsEl.innerHTML;
        }
    }
}

function renderDirectMarketingDashboardStatus(data = null) {
    const wrap = document.getElementById('wb-stor-dv-status');
    if (!wrap) return;
    const badge = document.getElementById('wb-stor-dv-badge');
    const detail = document.getElementById('wb-stor-dv-detail');
    data = data || window._storageLiveData || {};
    const plan = getDirectMarketingPlan(data);
    const monitor = getDirectMarketingMonitor(data);
    const report = getDirectMarketingDailyReport(data);
    const marketValueSolar = getMarketValueSolar(data);
    const marketValueSolarText = formatMarketValueSolarSummary(marketValueSolar, true);
    const marketValueSolarTitle = formatMarketValueSolarTitle(marketValueSolar);

    const isDvActive = isDirectMarketingVisible(data, plan, monitor);
    const hasActiveMwSolar = marketValueSolarText && report && report.enabled !== false && report.status !== 'disabled';

    if (!isDvActive && !hasActiveMwSolar) {
        wrap.style.display = 'none';
        if (badge) badge.textContent = '--';
        if (detail) {
            detail.textContent = '--';
            detail.title = '';
        }
        return;
    }

    if (!isDvActive && hasActiveMwSolar) {
        wrap.style.display = '';
        if (badge) {
            badge.textContent = 'MW Solar';
            badge.className = 'badge rounded-pill bg-info bg-opacity-25 text-info border border-info border-opacity-25';
            badge.title = marketValueSolarTitle;
        }
        if (detail) {
            detail.textContent = marketValueSolarText;
            detail.title = marketValueSolarTitle;
        }
        return;
    }

    const windows = collectDirectMarketingWindows(report, monitor, plan);
    const modeRaw = directMarketingNormalizeMode((monitor && monitor.mode) || (plan && plan.mode));
    wrap.style.display = '';
    const flags = plan && plan.flags && typeof plan.flags === 'object' ? plan.flags : {};
    const physicalAction = directMarketingRuntimePhysicalAction(data);
    const commandsAllowed = Boolean(physicalAction)
        || (monitor && monitor.commands_allowed === true)
        || flags.commands_allowed === true;
    const active = Boolean(physicalAction) || (monitor && monitor.active === true);
    const shadow = (monitor && monitor.shadow === true) || (plan && plan.shadow === true) || !commandsAllowed;
    const modeLabel = directMarketingModeLabel(modeRaw || (report && report.latest_summary && report.latest_summary.mode));
    const controllableActions = new Set([
        'eco_plus_negative_headroom_hold',
        'eco_plus_store_pv_candidate',
        'eco_plus_export_candidate',
        'arbitrage_export_candidate',
        'arbitrage_grid_charge_candidate'
    ]);
    const isNotPastWindow = windowEntry => {
        const endRaw = parseFloat(windowEntry && windowEntry.end_ts);
        if (!Number.isFinite(endRaw) || endRaw <= 0) return true;
        const endMs = endRaw > 10000000000 ? endRaw : endRaw * 1000;
        return endMs >= (Date.now() - 60000);
    };
    const firstPreferredWindow = (...groups) => {
        const flattened = groups.flatMap(group => Array.isArray(group) ? group : []);
        const future = flattened.filter(isNotPastWindow);
        return future.find(windowEntry => controllableActions.has(String(windowEntry.action || '').toLowerCase()))
            || future[0]
            || null;
    };
    const currentWindow = (monitor && monitor.current_window && typeof monitor.current_window === 'object')
        ? monitor.current_window
        : windows.find(windowEntry => windowEntry.current === true);
    const nextWindow = currentWindow || firstPreferredWindow(
        monitor && monitor.upcoming_windows,
        plan && plan.windows,
        windows
    );
    const action = String(
        (physicalAction && physicalAction.action)
        || (monitor && monitor.current_action)
        || (nextWindow && nextWindow.action)
        || ''
    );
    const salesWindow = directMarketingSalesWindowContract(monitor, plan);
    const reasonGroups = directMarketingReasonGroups(monitor);
    const blockers = (reasonGroups.blockers || []).filter(Boolean);
    const blockerText = blockers.filter((label, idx, arr) => arr.indexOf(label) === idx).slice(0, 2).join(', ');
    const economics = (monitor && monitor.economics && typeof monitor.economics === 'object') ? monitor.economics
        : ((plan && plan.economics && typeof plan.economics === 'object') ? plan.economics : {});
    const spread = directMarketingProfitText(directMarketingSpreadForAction(action, economics) ?? economics.pv_shift_spread_ct_per_kwh);
    const marginGateText = salesWindow.candidate
        ? directMarketingMarginGateText(monitor, plan, action)
        : '';
    const power = !physicalAction && nextWindow && (!salesWindow.candidate || salesWindow.active)
        ? directMarketingEnergyText(nextWindow)
        : '';
    const pvSource = livePvSourceInfo(data);
    const externalPvText = pvSource.external > 20
        ? getFlowLabel('external_pv') + ' ' + mobileStoragePower(pvSource.external) + (pvSource.locked ? ' für Akku gesperrt' : '')
        : '';
    const windowText = !physicalAction && nextWindow && (!salesWindow.candidate || salesWindow.active)
        ? directMarketingWindowText(nextWindow)
        : '';
    const actionText = physicalAction
        ? directMarketingActionLabel(physicalAction.action)
        : salesWindow.candidate && !salesWindow.active
        ? 'keine Verkaufsfreigabe'
        : (salesWindow.active ? 'Verkaufsfenster' : (action ? directMarketingActionLabel(action) : ''));
    const displayActive = physicalAction ? true : (salesWindow.candidate ? salesWindow.active : active);
    const statusText = physicalAction
        ? 'aktiv'
        : salesWindow.candidate
        ? (salesWindow.active ? 'aktiv' : 'wirtschaftlich gesperrt')
        : (active ? 'aktiv' : (shadow ? 'Beobachtung' : 'bereit'));
    const detailParts = [
        actionText,
        windowText,
        power,
        externalPvText,
        marginGateText || (spread ? ('Verschiebespread ' + spread) : ''),
        marketValueSolarText,
        blockerText ? ('Begrenzt: ' + blockerText) : ''
    ].filter(Boolean);
    const titleLines = [
        'Direktvermarktung ' + modeLabel + ' ' + statusText,
        'Regler: ' + ((monitor && monitor.plan_owner) || (plan && plan.plan_owner) || 'storage_manager'),
        commandsAllowed ? 'Befehle erlaubt' : 'Keine Befehle',
        pvSource.locked ? 'Nur E3DC-DC-PV laden: Zusatz-WR ist für Akkuladung gesperrt, nicht physisch abgeschaltet.' : '',
        ...detailParts,
        marketValueSolarTitle
    ].filter(Boolean);

    if (badge) {
        const lockIcon = pvSource.locked
            ? '<i class="fas fa-lock me-1" title="Zusatz-WR für Akkuladung gesperrt"></i>'
            : '';
        badge.innerHTML = lockIcon + directMarketingHtmlEscape(modeLabel + ' ' + statusText);
        badge.className = 'badge rounded-pill ' + (
            displayActive
                ? 'bg-success bg-opacity-25 text-success border border-success border-opacity-25'
                : (shadow
                    ? 'bg-warning bg-opacity-25 text-warning border border-warning border-opacity-25'
                    : 'bg-info bg-opacity-25 text-info border border-info border-opacity-25')
        );
        badge.title = titleLines.join('\n');
    }
    if (detail) {
        detail.textContent = detailParts.join(' | ') || (commandsAllowed ? 'wartet auf Marktfenster' : 'keine aktiven Befehle');
        detail.title = titleLines.join('\n');
    }
}

function formatDirectMarketingDailyReportSummary(report, compact = false) {
    if (!report || typeof report !== 'object' || !report.cycles) return '';
    const windows = Array.isArray(report.windows) ? report.windows.length : 0;
    const exportKwh = parseFloat(report.theoretical_export_kwh || 0);
    const pvStoreKwh = parseFloat(report.theoretical_pv_store_kwh || 0);
    const gridKwh = parseFloat(report.theoretical_grid_charge_kwh || 0);
    const realExportKwh = parseFloat(report.real_export_kwh || 0);
    const realPvStoreKwh = parseFloat(report.real_pv_store_kwh || 0);
    const economics = report.latest_economics && typeof report.latest_economics === 'object' ? report.latest_economics : {};
    const pvSpread = directMarketingProfitText(economics.pv_shift_spread_ct_per_kwh);
    const gridSpread = directMarketingProfitText(economics.grid_spread_ct_per_kwh);
    const blockerText = directMarketingTopCountsText(report.blocker_counts, compact ? 1 : 2, directMarketingBlockerLabel);
    const energyParts = [];
    if (realPvStoreKwh > 0) energyParts.push('Ist PV speichern ' + directMarketingKwhText(realPvStoreKwh));
    if (realExportKwh > 0) energyParts.push('Ist Export ' + directMarketingKwhText(realExportKwh));
    if (pvStoreKwh > 0) energyParts.push('Plan PV speichern ' + directMarketingKwhText(pvStoreKwh));
    if (exportKwh > 0) energyParts.push('Plan Export ' + directMarketingKwhText(exportKwh));
    if (gridKwh > 0) energyParts.push('Plan Netzladen ' + directMarketingKwhText(gridKwh));
    if (!energyParts.length && windows > 0) energyParts.push(windows + ' Fenster');
    const spreadParts = [];
    if (pvSpread) spreadParts.push('PV ' + pvSpread);
    if (gridSpread) spreadParts.push('Netz ' + gridSpread);
    const prefix = compact ? 'DV-Report' : 'DV-Tagesreport';
    const main = energyParts.length ? energyParts.join(', ') : 'noch keine Fenster';
    const tail = [spreadParts.join(' | '), blockerText ? 'Begrenzt: ' + blockerText : ''].filter(Boolean).join(' | ');
    return tail ? `${prefix}: ${main} | ${tail}` : `${prefix}: ${main}`;
}

function formatDirectMarketingDailyReportTitle(report) {
    if (!report || typeof report !== 'object' || !report.cycles) return '';
    const lines = [formatDirectMarketingDailyReportSummary(report, false)];
    lines.push('Zyklen: ' + report.cycles + ' | Beobachtung: ' + (report.shadow_cycles || 0) + ' | aktiv: ' + (report.active_cycles || 0));
    if (report.commands_allowed_cycles) lines.push('Steuerung erlaubt in Zyklen: ' + report.commands_allowed_cycles);
    const modes = directMarketingTopCountsText(report.mode_counts, 4, directMarketingModeLabel);
    if (modes) lines.push('Modi: ' + modes);
    const actions = directMarketingTopCountsText(report.window_action_counts, 5, directMarketingActionLabel);
    if (actions) lines.push('Fenster: ' + actions);
    const blockers = directMarketingTopCountsText(report.blocker_counts, 5, directMarketingBlockerLabel);
    if (blockers) lines.push('Begrenzt: ' + blockers);
    if (report.best_pv_shift_spread_ct_per_kwh != null) lines.push('Bester PV-Verschiebespread: ' + directMarketingProfitText(report.best_pv_shift_spread_ct_per_kwh));
    if (report.best_grid_spread_ct_per_kwh != null) lines.push('Bester Netz-Verschiebespread: ' + directMarketingProfitText(report.best_grid_spread_ct_per_kwh));
    if (parseFloat(report.theoretical_window_profit_eur || 0) > 0) {
        lines.push('Theoretischer Fensterwert: ' + parseFloat(report.theoretical_window_profit_eur).toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' EUR');
    }
    if (parseFloat(report.real_export_revenue_eur || 0) > 0) {
        lines.push('Realisierter Exporterlös: ' + parseFloat(report.real_export_revenue_eur).toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' EUR');
    }
    if (parseFloat(report.real_expected_profit_eur || 0) !== 0) {
        lines.push('Realisierte Policy-Marge: ' + parseFloat(report.real_expected_profit_eur).toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' EUR');
    }
    return lines.filter(Boolean).join('\n');
}

function formatDirectMarketingMonitorSummary(monitor, plan = null, compact = false) {
    if (!monitor || typeof monitor !== 'object') return '';
    const modeRaw = String(monitor.mode || (plan && plan.mode) || 'off').toLowerCase();
    const mode = directMarketingModeLabel(modeRaw);
    const state = String(monitor.state || '').toLowerCase();
    if (modeRaw === 'off' || monitor.enabled === false || state === 'off') {
        return compact ? 'DV aus' : 'Direktvermarktung aus';
    }
    const shadow = monitor.shadow === true || monitor.commands_allowed === false;
    const prefix = compact ? 'DV ' + mode : 'Direktvermarktung ' + mode;
    const status = monitor.active === true ? ' aktiv' : (shadow ? ' Beobachtung' : '');
    const windowEntry = monitor.current_window || (Array.isArray(monitor.upcoming_windows) ? monitor.upcoming_windows[0] : null);
    const actionText = windowEntry ? directMarketingWindowText(windowEntry) : '';
    const blockers = directMarketingBlockerSummary(monitor.blocked_reasons, compact ? 1 : 2);
    const profit = directMarketingProfitText(monitor.expected_profit_ct_per_kwh);
    const salesWindow = directMarketingSalesWindowContract(monitor, plan);
    if (salesWindow.candidate) {
        const margin = directMarketingMarginGateText(monitor, plan, salesWindow.action);
        if (salesWindow.active) {
            return `${prefix}: Verkaufsfenster aktiv${margin ? ' | ' + margin : ''}`;
        }
        return `${prefix}: keine Verkaufsfreigabe${margin ? ' | ' + margin : ''}`;
    }
    if (monitor.active === true && actionText) {
        return `${prefix}${status}: ${actionText}${profit ? ' ' + profit : ''}`;
    }
    if (blockers) {
        return `${prefix}${status}: ${blockers}${profit ? ' (' + profit + ')' : ''}`;
    }
    return actionText ? `${prefix}${status}: ${actionText}` : `${prefix}${status}: wartet`;
}

function formatDirectMarketingSummary(plan, compact = false) {
    if (!plan || typeof plan !== 'object') return '';
    const monitor = getDirectMarketingMonitor(null);
    if (monitor && typeof monitor === 'object' && Object.keys(monitor).length) {
        const monitorSummary = formatDirectMarketingMonitorSummary(monitor, plan, compact);
        if (monitorSummary) return monitorSummary;
    }
    const mode = directMarketingModeLabel(plan.mode);
    if (String(plan.mode || '').toLowerCase() === 'off' || plan.reason === 'disabled') {
        return compact ? 'DV aus' : 'Direktvermarktung aus';
    }
    const shadow = plan.shadow === true || (plan.flags && plan.flags.commands_allowed === false);
    const windows = Array.isArray(plan.windows) ? plan.windows : [];
    const policy = plan.policy_decision && typeof plan.policy_decision === 'object' ? plan.policy_decision : {};
    const budget = policy.storage_budget && typeof policy.storage_budget === 'object' ? policy.storage_budget : {};
    const executableSale = policy.commands_allowed === true
        && policy.blocked !== true
        && String(policy.dv_target_state || '').toUpperCase() === 'FORCE_EXPORT'
        && (parseFloat(budget.export_budget_w) || 0) > 0;
    const nextWindow = executableSale && windows.length ? directMarketingWindowText(windows[0]) : '';
    const prefix = compact ? 'DV ' + mode : 'Direktvermarktung ' + mode;
    const suffix = shadow ? ' Beobachtung' : '';
    return nextWindow ? `${prefix}${suffix}: ${nextWindow}` : `${prefix}${suffix}: keine Fenster`;
}

function formatDirectMarketingMonitorTitle(monitor, plan = null) {
    if (!monitor || typeof monitor !== 'object') return '';
    const lines = [formatDirectMarketingMonitorSummary(monitor, plan, false)];
    lines.push('Regler: Speicherregelung');
    lines.push('Plan: ' + directMarketingModeLabel((plan && plan.mode) || monitor.mode));
    lines.push('Steuerung erlaubt: ' + (monitor.commands_allowed === true ? 'ja' : 'nein'));
    if (monitor.current_action) lines.push('Aktuelles Fenster: ' + directMarketingActionLabel(monitor.current_action));
    const reserve = monitor.reserve && typeof monitor.reserve === 'object' ? monitor.reserve : {};
    const economics = monitor.economics && typeof monitor.economics === 'object' ? monitor.economics : {};
    if (reserve.effective_min_soc_pct != null) lines.push('Reserve: ' + parseFloat(reserve.effective_min_soc_pct).toFixed(1).replace('.', ',') + ' %');
    if (reserve.available_export_wh != null) lines.push('Spielraum: ' + mobileStorageKwhFromWh(reserve.available_export_wh));
    if (economics.pv_shift_spread_ct_per_kwh != null) lines.push('PV-Verschiebespread: ' + directMarketingProfitText(economics.pv_shift_spread_ct_per_kwh));
    if (economics.grid_spread_ct_per_kwh != null) lines.push('Netz-Verschiebespread: ' + directMarketingProfitText(economics.grid_spread_ct_per_kwh));
    if (Array.isArray(monitor.blocked_reasons) && monitor.blocked_reasons.length) {
        lines.push('Blockiert: ' + monitor.blocked_reasons.map(directMarketingBlockerLabel).join(', '));
    }
    return lines.filter(Boolean).join('\n');
}

function formatDirectMarketingTitle(plan) {
    if (!plan || typeof plan !== 'object') return '';
    const monitor = getDirectMarketingMonitor(null);
    if (monitor && typeof monitor === 'object' && Object.keys(monitor).length) {
        const monitorTitle = formatDirectMarketingMonitorTitle(monitor, plan);
        if (monitorTitle) return monitorTitle;
    }
    const lines = [formatDirectMarketingSummary(plan, false)];
    const flags = plan.flags && typeof plan.flags === 'object' ? plan.flags : {};
    const reserve = plan.reserve && typeof plan.reserve === 'object' ? plan.reserve : {};
    const economics = plan.economics && typeof plan.economics === 'object' ? plan.economics : {};
    lines.push('Regler: Speicherregelung');
    lines.push('Steuerung erlaubt: ' + (flags.commands_allowed === true ? 'ja' : 'nein'));
    if (reserve.effective_min_soc_pct != null) lines.push('Reserve: ' + parseFloat(reserve.effective_min_soc_pct).toFixed(1).replace('.', ',') + ' %');
    if (reserve.available_export_wh != null) lines.push('Spielraum: ' + mobileStorageKwhFromWh(reserve.available_export_wh));
    if (economics.best_spread_ct_per_kwh != null) lines.push('Verschiebespanne nach Kosten: ' + parseFloat(economics.best_spread_ct_per_kwh).toFixed(2).replace('.', ',') + ' ct/kWh');
    if (Array.isArray(plan.blocked_reasons) && plan.blocked_reasons.length) {
        lines.push('Blockiert: ' + plan.blocked_reasons.map(directMarketingBlockerLabel).join(', '));
    }
    return lines.filter(Boolean).join('\n');
}

function setMobileStorageText(id, text, visible = true) {
    const el = document.getElementById(id);
    if (!el) return;
    el.textContent = text;
    el.style.display = visible ? '' : 'none';
}

function storageStateDisplayLabel(state, providedLabel = '') {
    const normalized = String(state || '').toLowerCase();
    const labels = {
        direct_marketing_eco_plus_pv_store: 'PV speichern',
        direct_marketing_phase5_pv_store_wait: 'Speicherplatz halten',
        direct_marketing_charge_block_wait: 'Speicherplatz halten',
        direct_marketing_charge_block_wait_safe_fallback: 'Speicherplatz halten (Schutzbetrieb)',
        direct_marketing_phase5_restrictive_fallback: 'Speicherplatz halten (Schutzbetrieb)',
        direct_marketing_eco_plus_headroom_hold: 'Speicherplatz halten',
        direct_marketing_eco_plus_headroom_export: 'Speicherplatz schaffen',
        direct_marketing_eco_plus_export: 'Hochpreisverkauf',
        direct_marketing_arbitrage_grid_charge: 'Preisgesteuertes Netzladen',
        direct_marketing_arbitrage_export: 'Hochpreisverkauf'
    };
    return labels[normalized] || providedLabel || String(state || 'Speicherregelung').replace(/_/g, ' ');
}

function updateMobileStorageStrip(data) {
    const strip = document.getElementById('m-storage-strip');
    if (!strip || !data) return;
    cacheStorageCurveData(data);

    const hasStorage = !!(data.storage_state || data.storage_state_label || data.storage_curve_soc_target != null || data.storage_plan_meta);
    if (!hasStorage) {
        strip.style.display = 'none';
        return;
    }

    const stateRaw = String(data.storage_state || '').toLowerCase();
    const eveningRelease = stateRaw === 'parallel_evening_release';
    const autoLimit = data.storage_auto_limit && typeof data.storage_auto_limit === 'object' ? data.storage_auto_limit : null;
    const limitsActive = data.ems_power_limits_active === true || (autoLimit && autoLimit.enabled === true);
    const targetSoc = data.storage_curve_soc_target != null
        ? data.storage_curve_soc_target
        : (data.storage_plan_meta && (data.storage_plan_meta.planning_target_soc || data.storage_plan_meta.target_soc));
    const curveNow = data.storage_curve_soc_now;
    const ifcW = data.storage_ifc_w != null ? parseFloat(data.storage_ifc_w) : null;
    const chargeReqW = data.storage_charge_request_w != null
        ? parseFloat(data.storage_charge_request_w)
        : (data.bat_charge_req_w != null ? parseFloat(data.bat_charge_req_w) : null);
    const wbCurveReserveW = data.wallbox_curve_reserve_w != null ? parseFloat(data.wallbox_curve_reserve_w) : null;
    const wbCurveReserveTargetW = data.wallbox_curve_reserve_target_w != null ? parseFloat(data.wallbox_curve_reserve_target_w) : null;
    const abregelW = data.storage_abregel_req_w != null ? parseFloat(data.storage_abregel_req_w) : null;
    const adaptiveFloorSoc = data.storage_adaptive_soc_floor != null ? parseFloat(data.storage_adaptive_soc_floor) : null;
    const adaptiveCeilingSoc = data.storage_adaptive_soc_ceiling != null ? parseFloat(data.storage_adaptive_soc_ceiling) : null;
    const adaptiveActive = data.storage_adaptive_curve_active === true
        || data.storage_adaptive_curve_active === 1
        || data.storage_adaptive_curve_active === '1'
        || (Number.isFinite(adaptiveFloorSoc) && Number.isFinite(adaptiveCeilingSoc));
    const adaptiveHeadroomRequiredWh = data.storage_adaptive_headroom_required_wh != null ? parseFloat(data.storage_adaptive_headroom_required_wh) : null;
    const curtailmentPressureWh = data.storage_curtailment_pressure_wh != null ? parseFloat(data.storage_curtailment_pressure_wh) : null;
    const adaptiveHeadroomOpenWh = Number.isFinite(adaptiveHeadroomRequiredWh) ? Math.max(0, adaptiveHeadroomRequiredWh) : null;
    const curtailmentUnavoidableWh = data.storage_curtailment_unavoidable_wh != null ? parseFloat(data.storage_curtailment_unavoidable_wh) : null;
    const eveningShortfallWh = data.storage_evening_shortfall_wh != null ? parseFloat(data.storage_evening_shortfall_wh) : null;
    const abregelActive = data.storage_abregel_active === true
        || data.storage_abregel_active === 1
        || data.storage_abregel_active === '1'
        || stateRaw === 'parallel_curve_charge_cap';
    const emsCharge = data.ems_max_charge_power_w != null
        ? data.ems_max_charge_power_w
        : (autoLimit && autoLimit.max_charge_w != null ? autoLimit.max_charge_w : data.storage_max_charge_w);
    const emsDischarge = data.ems_max_discharge_power_w != null
        ? data.ems_max_discharge_power_w
        : (autoLimit && autoLimit.max_discharge_w != null ? autoLimit.max_discharge_w : data.storage_max_discharge_w);
    const anchors = Array.isArray(data.storage_curve_anchors) ? data.storage_curve_anchors : [];
    const meta = data.storage_plan_meta || {};
    const curveMeta = meta.target_curve_meta || {};
    const controlSoc = data.storage_curve_control_soc != null && !isNaN(parseFloat(data.storage_curve_control_soc))
        ? parseFloat(data.storage_curve_control_soc)
        : (data.soc != null && !isNaN(parseFloat(data.soc)) ? parseFloat(data.soc) : null);
    const activeTarget = _storageActiveCurveTarget(meta, controlSoc);
    const firstAnchor = anchors.length ? anchors[0] : null;
    const lastAnchor = anchors.length ? anchors[anchors.length - 1] : null;
    const releaseText = curveMeta.curve_end_t
        ? curveMeta.curve_end_t + ' ' + mobileStoragePct(curveMeta.curve_end_soc || targetSoc, 0)
        : mobileStorageAnchorText(lastAnchor);
    const storageDisplay = storageOperationalDisplay(data);
    const directMarketingHold = storageDisplay.holdActive ? storageDisplay : null;

    strip.style.display = 'block';
    strip.classList.toggle('storage-active', storageDisplay.active || !!limitsActive || stateRaw.includes('charge') || stateRaw.includes('schutz'));
    strip.classList.toggle('storage-free', storageDisplay.state === 'auto' || eveningRelease || (!limitsActive && stateRaw.includes('auto')));

    setMobileStorageText('m-storage-mode-pill', directMarketingHold
        ? storageDisplay.badge
        : (limitsActive ? 'EMS aktiv' : 'EMS frei'));
    setMobileStorageText('m-storage-title', storageDisplay.label);
    if (directMarketingHold) {
        setMobileStorageText('m-storage-soll', '', false);
    } else if (eveningRelease) {
        setMobileStorageText('m-storage-soll', 'Freilauf');
    } else if (activeTarget.mode === 'floor_catchup' || activeTarget.mode === 'ceiling_hold') {
        setMobileStorageText('m-storage-soll', 'Regelziel');
    } else if (adaptiveActive && Number.isFinite(adaptiveFloorSoc) && Number.isFinite(adaptiveCeilingSoc)) {
        setMobileStorageText('m-storage-soll', 'Band ' + mobileStoragePct(adaptiveFloorSoc, 0) + '-' + mobileStoragePct(adaptiveCeilingSoc, 0));
    } else {
        setMobileStorageText('m-storage-soll', 'Soll ' + mobileStoragePct(targetSoc, 0));
    }

    if (directMarketingHold) {
        setMobileStorageText('m-storage-curve', storageDisplay.detail);
    } else if (eveningRelease) {
        setMobileStorageText('m-storage-curve', 'Freilauf erreicht');
    } else if (activeTarget.mode === 'floor_catchup' || activeTarget.mode === 'ceiling_hold') {
        setMobileStorageText('m-storage-curve', activeTarget.label);
    } else if (adaptiveActive && Number.isFinite(adaptiveFloorSoc) && Number.isFinite(adaptiveCeilingSoc)) {
        setMobileStorageText('m-storage-curve', 'Korridor ' + mobileStoragePct(adaptiveFloorSoc, 1) + ' -> ' + mobileStoragePct(adaptiveCeilingSoc, 1));
    } else if (curveNow != null || targetSoc != null) {
        setMobileStorageText('m-storage-curve', 'Kurve ' + mobileStoragePct(curveNow) + ' -> ' + mobileStoragePct(targetSoc));
    } else {
        setMobileStorageText('m-storage-curve', 'Kurve --');
    }
    const chargeAcceptanceDiagnostic = data.storage_charge_acceptance_diagnostic
        && typeof data.storage_charge_acceptance_diagnostic === 'object'
        ? data.storage_charge_acceptance_diagnostic
        : null;
    const chargeAcceptanceActive = chargeAcceptanceDiagnostic
        && chargeAcceptanceDiagnostic.active === true
        && chargeAcceptanceDiagnostic.control_effect === false
        && chargeAcceptanceDiagnostic.display_text === 'Ladeannahme begrenzt – Ursache unklar';
    if (chargeAcceptanceActive && !directMarketingHold) {
        setMobileStorageText('m-storage-curve', chargeAcceptanceDiagnostic.display_text);
    }
    const requestParts = [];
    if (chargeAcceptanceActive) requestParts.push(chargeAcceptanceDiagnostic.display_text);
    if (abregelActive && abregelW !== null && Math.abs(abregelW) > 0) requestParts.push('Abregel ' + mobileStoragePower(abregelW));
    if (!(abregelActive && abregelW !== null && Math.abs(abregelW) > 0) && curtailmentPressureWh !== null && curtailmentPressureWh > 0) {
        requestParts.push('Abregeldruck ' + mobileStorageKwhFromWh(curtailmentPressureWh));
    }
    if (adaptiveHeadroomOpenWh !== null && adaptiveHeadroomOpenWh > 0) {
        requestParts.push('Headroom freihalten ' + mobileStorageKwhFromWh(adaptiveHeadroomOpenWh));
    }
    if (eveningShortfallWh !== null && eveningShortfallWh > 0) {
        requestParts.push('Abendziel +' + mobileStorageKwhFromWh(eveningShortfallWh));
    }
    if (wbCurveReserveW !== null && wbCurveReserveW > 0) {
        requestParts.push('iFc-Führung ' + mobileStoragePower(wbCurveReserveW));
    } else if (chargeReqW !== null && chargeReqW > 0) {
        requestParts.push('Rahmen ' + mobileStoragePower(chargeReqW));
    } else if (ifcW !== null && ifcW > 0) {
        requestParts.push('Bedarf wartet ' + mobileStoragePower(ifcW));
    }
    setMobileStorageText(
        'm-storage-ifc',
        directMarketingHold ? '' : requestParts.join(' | '),
        !directMarketingHold && requestParts.length > 0
    );
    const requestEl = document.getElementById('m-storage-ifc');
    if (requestEl) {
        const titleParts = [];
        if (wbCurveReserveW !== null && wbCurveReserveW > 0) {
            titleParts.push('Aktive iFc-Führung bleibt beim Speicher: ' + mobileStoragePower(wbCurveReserveW));
            if (wbCurveReserveTargetW !== null && wbCurveReserveTargetW > 0) {
                titleParts.push('iFc-Ziel: ' + mobileStoragePower(wbCurveReserveTargetW));
            }
        } else if (chargeReqW !== null) titleParts.push('Wirksamer Laderahmen: ' + mobileStoragePower(chargeReqW));
        if (adaptiveActive && Number.isFinite(adaptiveFloorSoc) && Number.isFinite(adaptiveCeilingSoc)) {
            titleParts.push('Zielkorridor: ' + mobileStoragePct(adaptiveFloorSoc, 1) + ' bis ' + mobileStoragePct(adaptiveCeilingSoc, 1));
        }
        if (activeTarget.mode === 'floor_catchup') titleParts.push('Aktives Regelziel: Unterkante ' + mobileStoragePct(activeTarget.floor, 1));
        if (activeTarget.mode === 'ceiling_hold') titleParts.push('Aktives Regelziel: Oberkante ' + mobileStoragePct(activeTarget.ceiling, 1));
        if (curtailmentPressureWh !== null) titleParts.push('Abregeldruck über den PV-Tag: ' + mobileStorageKwhFromWh(curtailmentPressureWh));
        if (curtailmentUnavoidableWh !== null && curtailmentUnavoidableWh > 0) titleParts.push('Nicht durch Speicher vermeidbarer Druck: ' + mobileStorageKwhFromWh(curtailmentUnavoidableWh));
        if (adaptiveHeadroomOpenWh !== null) titleParts.push('Bis zum Druckfenster freizuhalten: ' + mobileStorageKwhFromWh(adaptiveHeadroomOpenWh));
        if (eveningShortfallWh !== null && eveningShortfallWh > 0) titleParts.push('Abendziel-Risiko: ' + mobileStorageKwhFromWh(eveningShortfallWh));
        if (ifcW !== null && ifcW > 0) titleParts.push('Kurvenbedarf iFc: ' + mobileStoragePower(ifcW));
        if (data.storage_curve_control_soc != null) titleParts.push('Regel-SoC: ' + mobileStoragePct(data.storage_curve_control_soc, 1));
        if (data.storage_curve_gap_pct != null) {
            const gapPct = parseFloat(data.storage_curve_gap_pct);
            if (!isNaN(gapPct)) {
                titleParts.push((gapPct >= 0 ? 'Rückstand zur Kurve: ' : 'Vorsprung zur Kurve: ') + Math.abs(gapPct).toFixed(1) + ' %');
            }
        }
        if (data.storage_curve_catchup_cap_w != null && parseFloat(data.storage_curve_catchup_cap_w) > 0) {
            titleParts.push('Aufhol-Kappe: ' + mobileStoragePower(data.storage_curve_catchup_cap_w));
        }
        requestEl.title = titleParts.join('\n');
    }

    if (directMarketingHold) {
        setMobileStorageText('m-storage-ems', '', false);
    } else if (limitsActive) {
        setMobileStorageText('m-storage-ems', 'EMS Lade ' + mobileStoragePower(emsCharge || 0) + ' | Entl. ' + mobileStoragePower(emsDischarge || 0));
    } else {
        setMobileStorageText('m-storage-ems', 'EMS frei');
    }

    const anchorWrap = document.getElementById('m-storage-anchors');
    const showAnchors = !directMarketingHold && !!(firstAnchor || lastAnchor || curveMeta.curve_end_t);
    if (anchorWrap) anchorWrap.style.display = showAnchors ? '' : 'none';
    setMobileStorageText('m-storage-start', mobileStorageAnchorText(firstAnchor), showAnchors);
    setMobileStorageText('m-storage-now', mobileStoragePct(curveNow, 1), showAnchors && curveNow != null);
    setMobileStorageText('m-storage-release', releaseText, showAnchors);

    const reason = String(data.storage_reason || '').replace(/\s+/g, ' ').trim();
    const displayReason = directMarketingHold
        ? storageDisplay.detail
        : (storageDisplay.plannedHint || reason || 'Noch kein Storage-Manager-Status vorhanden.');
    setMobileStorageText('m-storage-reason', displayReason);
    const reasonEl = document.getElementById('m-storage-reason');
    if (reasonEl) reasonEl.title = directMarketingHold ? storageDisplay.badge : (reason || storageDisplay.plannedHint || '');
}

document.addEventListener('DOMContentLoaded', () => {
    initMobileFlowView();
    initMobileStorageCurveTrigger();
});

function formatWatts(w) {
    w = parseFloat(w);
    if (isNaN(w)) return '--';
    const isMobile = document.body.classList.contains('mode-mobile');
    const cls = isMobile ? 'unit' : 'val-unit';
    const sp = isMobile ? ' ' : '';
    if (Math.abs(w) >= 1000) {
        return (w / 1000).toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + `<span class="${cls}">${sp}kW</span>`;
    }
    return Math.round(w).toLocaleString('de-DE') + `<span class="${cls}">${sp}W</span>`;
}

function normalizeKwh(value, fallback = null, max = 2000) {
    const n = parseFloat(value);
    if (!Number.isFinite(n) || n < -0.01 || n > max) return fallback;
    return Math.max(0, n);
}

function formatKwh(value, digits = 1) {
    const n = normalizeKwh(value, null);
    if (n === null) return '-- kWh';
    return n.toLocaleString('de-DE', {minimumFractionDigits: digits, maximumFractionDigits: digits}) + ' kWh';
}

function preferKwh(primary, fallback) {
    const p = normalizeKwh(primary, null);
    const f = normalizeKwh(fallback, null);
    if (p !== null && (p > 0 || f === null || f <= 0)) return p;
    return f;
}

function sumKwh(stats, keys) {
    if (!stats) return 0;
    return keys.reduce((sum, key) => sum + normalizeKwh(stats[key], 0), 0);
}

function pctOf(part, total) {
    const t = normalizeKwh(total, 0);
    return t > 0 ? (normalizeKwh(part, 0) / t) * 100 : 0;
}

function updateKwhText(selector, value, digits = 1, hideInvalid = false) {
    const el = $(selector);
    if (!el.length) return;
    const n = normalizeKwh(value, null);
    if (n === null) {
        el.text('-- kWh');
        if (hideInvalid) el.parent().hide();
    } else {
        el.text(formatKwh(n, digits));
        el.parent().show();
    }
}

function pvYieldSourceTitle(data, pvToday) {
    const sources = data && data.sources && typeof data.sources === 'object' ? data.sources : {};
    const source = sources.pv_total_source || '';
    const total = formatKwh(pvToday, 1);
    if (source === 'integrated_total_with_external_ac') {
        const parts = [`PV-Ertrag gesamt: ${total}`];
        const e3dc = normalizeKwh(sources.pv_e3dc_exact_kwh, null);
        const external = normalizeKwh(sources.pv_external_integrated_kwh, null);
        if (e3dc !== null) parts.push(`E3DC/DC: ${formatKwh(e3dc, 1)}`);
        if (external !== null && external > 0.05) parts.push(`externer AC-Anteil: ca. ${formatKwh(external, 1)}`);
        return parts.join(' | ');
    }
    if (source === 'exact_e3dc_counter') return `PV-Ertrag heute laut E3DC-Zähler: ${total}`;
    if (source === 'power_integral') return `PV-Ertrag heute aus Leistungsintegration: ${total}`;
    if (sources.pv_total_export_fallback_kwh != null) return `PV-Ertrag heute mit Export-Fallback: ${total}`;
    return `PV-Ertrag heute: ${total}`;
}

function updatePvYieldTitle(data, pvToday) {
    const el = $('#pv-yield-today');
    if (!el.length) return;
    el.closest('.badge').attr('title', pvYieldSourceTitle(data, pvToday));
}

function flowPlainWatts(value) {
    return formatWatts(value).replace(/<[^>]*>?/gm, '');
}

function flowHoverEscape(value) {
    return String(value ?? '').replace(/[&<>"']/g, ch => ({
        '&': '&amp;',
        '<': '&lt;',
        '>': '&gt;',
        '"': '&quot;',
        "'": '&#39;'
    }[ch]));
}

function flowHoverKwh(value, digits = 1) {
    const n = normalizeKwh(value, null);
    if (n === null) return '-- kWh';
    return n.toLocaleString('de-DE', {minimumFractionDigits: digits, maximumFractionDigits: digits}) + ' kWh';
}

function flowHoverItemsHtml(items) {
    const clean = (items || [])
        .map(item => ({...item, value: normalizeKwh(item.value, 0)}))
        .filter(item => item.value > 0.03);
    const total = clean.reduce((sum, item) => sum + item.value, 0);
    if (total <= 0.03) return '<div class="flow-hover-note">Noch keine Tagesaufteilung vorhanden.</div>';
    const bar = clean.map(item => {
        const width = Math.max(3, (item.value / total) * 100);
        return `<span class="flow-hover-seg" style="width:${width.toFixed(2)}%;background:${flowHoverEscape(item.color)}"></span>`;
    }).join('');
    const rows = clean.map(item => `
        <div class="flow-hover-row">
            <span><span class="flow-hover-dot" style="background:${flowHoverEscape(item.color)}"></span>${flowHoverEscape(item.label)}</span>
            <strong>${flowHoverKwh(item.value, item.value >= 10 ? 1 : 2)}</strong>
        </div>
    `).join('');
    return `<div class="flow-hover-bar">${bar}</div>${rows}`;
}

function flowHoverHtml(card) {
    if (!card) return '';
    return `
        <div class="flow-hover-title">
            <span>${flowHoverEscape(card.title)}</span>
            <span class="flow-hover-now">${flowHoverEscape(card.now || '--')}</span>
        </div>
        <div class="flow-hover-meta">
            <span>${flowHoverEscape(card.totalLabel || 'Heute')}</span>
            <strong>${flowHoverEscape(card.total || '--')}</strong>
        </div>
        ${flowHoverItemsHtml(card.items)}
        ${card.note ? `<div class="flow-hover-note">${flowHoverEscape(card.note)}</div>` : ''}
    `;
}

function positionEnergyFlowHoverPanel(container, node) {
    const panel = container ? container.querySelector('[data-flow-hover-panel]') : null;
    if (!container || !node || !panel || !panel.classList.contains('is-visible')) return;
    const c = flowCanvas(container).getBoundingClientRect();
    const n = node.getBoundingClientRect();
    const p = panel.getBoundingClientRect();
    let left = n.right - c.left + 12;
    let top = n.top - c.top + (n.height / 2) - (p.height / 2);
    if (left + p.width > c.width - 8) left = n.left - c.left - p.width - 12;
    left = Math.max(8, Math.min(left, c.width - p.width - 8));
    top = Math.max(8, Math.min(top, c.height - p.height - 8));
    panel.style.left = `${left}px`;
    panel.style.top = `${top}px`;
}

function initEnergyFlowHoverPanel(container) {
    if (!container || container.dataset.flowHoverReady === '1') return;
    container.dataset.flowHoverReady = '1';
    const panel = container.querySelector('[data-flow-hover-panel]');
    if (!panel) return;
    const hide = () => {
        panel.classList.remove('is-visible');
        panel.setAttribute('aria-hidden', 'true');
    };
    container.querySelectorAll('.flow-node[data-flow-node]').forEach(node => {
        if (node.dataset.flowNode === 'center') return;
        node.addEventListener('mouseenter', () => {
            if (container.classList.contains('flow-editing')) return;
            const html = node.dataset.flowHoverHtml || '';
            if (!html) return;
            panel.innerHTML = html;
            panel.classList.add('is-visible');
            panel.setAttribute('aria-hidden', 'false');
            requestAnimationFrame(() => positionEnergyFlowHoverPanel(container, node));
        });
        node.addEventListener('mousemove', () => positionEnergyFlowHoverPanel(container, node));
        node.addEventListener('mouseleave', hide);
    });
}

function updateEnergyFlowHoverCards(data, values = {}) {
    const container = document.getElementById('flow-view');
    if (!container || !data) return;
    initEnergyFlowHoverPanel(container);
    const stats = data.stats || {};
    const kwh = key => normalizeKwh(stats[key], 0);
    const sum = keys => keys.reduce((total, key) => total + kwh(key), 0);
    const pvWb = sum(['pv_wb_kwh', 'pv_wb2_kwh']);
    const batWb = sum(['bat_wb_kwh', 'bat_wb2_kwh']);
    const gridWb = sum(['grid_wb_kwh', 'grid_wb2_kwh']);
    const wb1Total = preferKwh(data.wb_daily_kwh, stats.total_wb_kwh);
    const wb2Total = preferKwh(data.wb2_daily_kwh, stats.total_wb2_kwh);
    const wpTotal = preferKwh(data.wp_daily_display_kwh, stats.total_wp_display_kwh ?? stats.total_wp_kwh);
    const climateTotal = preferKwh(data.climate_daily_kwh, stats.total_climate_kwh);
    const wpSourceItems = [
        {label: 'PV', value: kwh('pv_wp_kwh'), color: getFlowColor('pv')},
        {label: 'Batterie', value: kwh('bat_wp_kwh'), color: getFlowColor('battery')},
        {label: 'Netz', value: kwh('grid_wp_kwh'), color: getFlowColor('grid')}
    ];
    const wpTotalValue = normalizeKwh(wpTotal, null);
    const wpSourceSum = wpSourceItems.reduce((total, item) => total + normalizeKwh(item.value, 0), 0);
    const wpUnassigned = wpTotalValue !== null ? wpTotalValue - wpSourceSum : 0;
    const wpCounterSplitGap = wpUnassigned > 0.05;
    if (wpCounterSplitGap) {
        wpSourceItems.push({label: 'nicht zugeordnet', value: wpUnassigned, color: '#94a3b8'});
    }
    const batteryTargetItems = [
        {label: 'zu Haus', value: kwh('bat_home_kwh'), color: getFlowColor('home')},
        {label: 'zu Wallbox', value: batWb, color: getFlowColor('wallbox')},
        {label: 'zu WP', value: kwh('bat_wp_kwh'), color: getFlowColor('heatpump')},
        {label: 'zu Klima', value: kwh('bat_climate_kwh'), color: getFlowColor('climate')},
        {label: 'ins Netz / Verkauf', value: kwh('bat_grid_kwh'), color: getFlowColor('grid_export')}
    ];
    const batteryOutTotalValue = normalizeKwh(stats.total_bat_out_kwh, null);
    const batteryTargetSum = batteryTargetItems.reduce((total, item) => total + normalizeKwh(item.value, 0), 0);
    const batteryUnassigned = batteryOutTotalValue !== null ? batteryOutTotalValue - batteryTargetSum : 0;
    const batteryCounterSplitGap = batteryUnassigned > 0.05;
    if (batteryCounterSplitGap) {
        batteryTargetItems.push({label: 'nicht zugeordnet', value: batteryUnassigned, color: '#94a3b8'});
    }
    const hasExternalPvNode = !!container.querySelector('[data-flow-node="external_pv"]');
    const pvSourceDestinationItems = prefix => {
        if (!Object.prototype.hasOwnProperty.call(stats, `${prefix}_home_kwh`)) return [];
        return [
            {label: 'Haus', value: kwh(`${prefix}_home_kwh`), color: getFlowColor('home')},
            {label: 'Batterie', value: kwh(`${prefix}_bat_kwh`), color: getFlowColor('battery_charge')},
            {label: 'Wallbox', value: sum([`${prefix}_wb_kwh`, `${prefix}_wb2_kwh`]), color: getFlowColor('wallbox')},
            {label: 'WP', value: kwh(`${prefix}_wp_kwh`), color: getFlowColor('heatpump')},
            {label: 'Klima', value: kwh(`${prefix}_climate_kwh`), color: getFlowColor('climate')},
            {label: 'Netz', value: kwh(`${prefix}_grid_kwh`), color: getFlowColor('grid')}
        ];
    };
    const aggregatePvDestinationItems = [
        {label: 'Haus', value: kwh('pv_home_kwh'), color: getFlowColor('home')},
        {label: 'Batterie', value: kwh('pv_bat_kwh'), color: getFlowColor('battery_charge')},
        {label: 'Wallbox', value: pvWb, color: getFlowColor('wallbox')},
        {label: 'WP', value: kwh('pv_wp_kwh'), color: getFlowColor('heatpump')},
        {label: 'Klima', value: kwh('pv_climate_kwh'), color: getFlowColor('climate')},
        {label: 'Netz', value: kwh('pv_grid_kwh') || kwh('total_grid_out_kwh'), color: getFlowColor('grid')}
    ];
    const e3dcPvDestinationItems = hasExternalPvNode
        ? pvSourceDestinationItems('pv_e3dc')
        : aggregatePvDestinationItems;
    const externalPvDestinationItems = pvSourceDestinationItems('pv_external');
    const pvSourceAttributionNote = 'Tagesaufteilung: bilanzielle Zuordnung aus den gemessenen Leistungsintervallen.';
    const pvToday = hasExternalPvNode
        ? preferKwh(stats.pv_e3dc_kwh, null)
        : preferKwh(data.pv_today_kwh, stats.total_pv_kwh);
    const pvSplitNote = livePvSourceSplitText(data, ' | ', false);
    const pvSource = livePvSourceInfo(data);
    const gridNow = Math.round(values.grid ?? data.grid ?? 0);
    const batNow = Math.round(values.bat ?? data.bat ?? 0);
    const hsNow = Math.max(0, values.hs ?? data.hs_power ?? 0);
    const climateNow = Math.max(0, values.climate ?? data.climate_power_w ?? data.climate ?? 0);
    const cards = {
        pv: {
            title: hasExternalPvNode ? 'E3DC-PV' : 'Sonne',
            now: flowPlainWatts(values.pv ?? data.pv ?? 0),
            totalLabel: 'Ertrag heute',
            total: flowHoverKwh(pvToday, 1),
            items: e3dcPvDestinationItems,
            note: pvSplitNote
                ? pvSplitNote
                    + (pvSource.locked ? '. DC-only aktiv: Zusatz-WR ist für Akkuladung gesperrt.' : '')
                    + (hasExternalPvNode ? ' ' + pvSourceAttributionNote : '')
                : ''
        },
        external_pv: {
            title: getFlowLabel('external_pv'),
            now: flowPlainWatts(pvSource.external),
            totalLabel: 'Ertrag heute',
            total: flowHoverKwh(stats.pv_external_kwh, 1),
            items: externalPvDestinationItems,
            note: [
                (getDirectMarketingAuxInverterShellyState(data) || {}).status === 'manual_locked'
                    ? 'Manuell gesperrt.'
                    : livePvSourceSplitText(data, ' | ', false),
                pvSourceAttributionNote
            ].filter(Boolean).join(' ')
        },
        home: {
            title: 'Haus',
            now: flowPlainWatts(values.home ?? data.home ?? 0),
            totalLabel: 'Verbrauch heute',
            total: flowHoverKwh(stats.total_home_kwh, 1),
            items: [
                {label: 'PV', value: kwh('pv_home_kwh'), color: getFlowColor('pv')},
                {label: 'Batterie', value: kwh('bat_home_kwh'), color: getFlowColor('battery')},
                {label: 'Netz', value: kwh('grid_home_kwh'), color: getFlowColor('grid')}
            ]
        },
        grid: {
            title: gridNow < 0 ? 'Netzeinspeisung' : 'Netzbezug',
            now: flowPlainWatts(Math.abs(gridNow)),
            totalLabel: 'Bezug / Einspeisung',
            total: `${flowHoverKwh(stats.total_grid_in_kwh, 1)} / ${flowHoverKwh(stats.total_grid_out_kwh, 1)}`,
            items: [
                {label: 'Hausbezug', value: kwh('grid_home_kwh'), color: getFlowColor('home')},
                {label: 'Batterie', value: kwh('grid_bat_kwh'), color: getFlowColor('battery_charge')},
                {label: 'Wallbox', value: gridWb, color: getFlowColor('wallbox')},
                {label: 'WP', value: kwh('grid_wp_kwh'), color: getFlowColor('heatpump')},
                {label: 'Klima', value: kwh('grid_climate_kwh'), color: getFlowColor('climate')},
                {label: 'PV-Einspeisung', value: kwh('pv_grid_kwh'), color: getFlowColor('pv')},
                {label: 'Batterie-Verkauf', value: kwh('bat_grid_kwh'), color: getFlowColor('battery')}
            ]
        },
        battery: {
            title: batNow > 0 ? 'Batterie lädt' : (batNow < 0 ? 'Batterie entlädt' : 'Batterie'),
            now: flowPlainWatts(Math.abs(batNow)),
            totalLabel: 'Ladung / Entladung',
            total: `${flowHoverKwh(stats.total_bat_in_kwh, 1)} / ${flowHoverKwh(stats.total_bat_out_kwh, 1)}`,
            items: batteryTargetItems,
            note: batteryCounterSplitGap
                ? `SoC ${Math.round(data.soc || 0)}% | Tageswert aus Batterie-Zähler; Ziel-Aufteilung aus Leistungsbilanz.`
                : `SoC ${Math.round(data.soc || 0)}%`
        },
        wallbox: {
            title: 'Wallbox 1',
            now: flowPlainWatts(Math.abs(values.wb ?? data.wb ?? 0)),
            totalLabel: 'Heute / Session',
            total: `${flowHoverKwh(wb1Total, 1)} / ${flowHoverKwh(data.wb_session_kwh, 2)}`,
            items: [
                {label: 'PV', value: kwh('pv_wb_kwh'), color: getFlowColor('pv')},
                {label: 'Batterie', value: kwh('bat_wb_kwh'), color: getFlowColor('battery')},
                {label: 'Netz', value: kwh('grid_wb_kwh'), color: getFlowColor('grid')}
            ]
        },
        wallbox2: {
            title: 'Wallbox 2',
            now: flowPlainWatts(Math.abs(values.wb2 ?? data.wb2 ?? 0)),
            totalLabel: 'Heute / Session',
            total: `${flowHoverKwh(wb2Total, 1)} / ${flowHoverKwh(data.wb2_session_kwh, 2)}`,
            items: [
                {label: 'PV', value: kwh('pv_wb2_kwh'), color: getFlowColor('pv')},
                {label: 'Batterie', value: kwh('bat_wb2_kwh'), color: getFlowColor('battery')},
                {label: 'Netz', value: kwh('grid_wb2_kwh'), color: getFlowColor('grid')}
            ]
        },
        heatpump: {
            title: 'Wärmepumpe',
            now: flowPlainWatts(values.wp ?? data.wp ?? 0),
            totalLabel: 'Verbrauch heute',
            total: flowHoverKwh(wpTotal, 1),
            items: wpSourceItems,
            note: wpCounterSplitGap ? 'Tageswert aus WP-Zähler; Quellen aus Leistungsbilanz.' : ''
        },
        heater: {
            title: 'Heizstab',
            now: flowPlainWatts(hsNow),
            totalLabel: 'Status',
            total: data.elwa_status || data.hs_mode || '--',
            items: [],
            note: data.elwa_water_temp_c != null ? `Wasser ${Number(data.elwa_water_temp_c).toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1})} °C` : ''
        },
        climate: {
            title: data.climate_name || 'Klimaanlage',
            now: flowPlainWatts(climateNow),
            totalLabel: 'Verbrauch heute',
            total: flowHoverKwh(climateTotal, 2),
            items: [
                {label: 'PV', value: kwh('pv_climate_kwh'), color: getFlowColor('pv')},
                {label: 'Batterie', value: kwh('bat_climate_kwh'), color: getFlowColor('battery')},
                {label: 'Netz', value: kwh('grid_climate_kwh'), color: getFlowColor('grid')}
            ],
            note: data.climate_online === false ? 'Zähler aktuell offline oder veraltet.' : (data.climate_phase ? `Phase ${String(data.climate_phase).toUpperCase()}` : '')
        }
    };
    Object.entries(cards).forEach(([key, card]) => {
        const node = flowNodeByKey(container, key);
        if (node) node.dataset.flowHoverHtml = flowHoverHtml(card);
    });
}

function updateDashboardDailyTiles(data) {
    const stats = data && data.stats ? data.stats : {};
    const pvToday = preferKwh(data && data.pv_today_kwh, stats.total_pv_kwh);

    updateKwhText('#pv-yield-today', pvToday, 1);
    updatePvYieldTitle(data, pvToday);
    updateKwhText('#home-today', stats.total_home_kwh, 1);
    updateKwhText('#bat-in-today', stats.total_bat_in_kwh, 1);
    updateKwhText('#bat-out-today', stats.total_bat_out_kwh, 1);
    updateKwhText('#grid-in-today', stats.total_grid_in_kwh, 1);
    updateKwhText('#grid-out-today', stats.total_grid_out_kwh, 1);
    updateKwhText('#wb-daily-value', preferKwh(data && data.wb_daily_kwh, stats.total_wb_kwh), 1);
    updateKwhText('#wb2-daily-value', preferKwh(data && data.wb2_daily_kwh, stats.total_wb2_kwh), 1);
    updateKwhText('#wp-today-value', preferKwh(data && data.wp_daily_display_kwh, stats.total_wp_display_kwh ?? stats.total_wp_kwh), 1);
}

function setModernCardState(elementId, state) {
    const el = document.getElementById(elementId);
    if (!el) return;
    const cfg = state || {};
    const present = cfg.present !== false;
    const active = present && cfg.active === true;
    const offline = cfg.offline === true;
    el.classList.toggle('modern-active', active);
    el.classList.toggle('modern-idle', present && !active && !offline);
    el.classList.toggle('modern-inactive', !active || offline);
    el.classList.toggle('modern-offline', offline);
}

function heatSgReadyPresentation(data) {
    const live = data || {};
    const state = String(live.wp_sg_ready_state || '');
    const valid = live.wp_sg_ready_valid === true;
    const active = live.wp_sg_ready_active === true;
    const blocked = state === 'blocked';
    const visible = valid && ((state === 'boost' && active) || blocked);
    const source = String(live.wp_sg_ready_source || '');
    const ageRaw = live.wp_sg_ready_age_s;
    const parsedAgeS = ageRaw === null || ageRaw === undefined || ageRaw === ''
        ? Number.NaN
        : Number(ageRaw);
    const ageSuffix = Number.isFinite(parsedAgeS) && parsedAgeS >= 0
        ? ` Alter ${Math.round(parsedAgeS)} s.`
        : '';
    const label = String(live.wp_sg_ready_label || (
        blocked ? 'SG-Ready EVU-Sperre' : 'SG-Ready aktiv'
    ));
    const sourceTitle = (source === 'shelly_relay_confirmed_readback'
        ? 'Shelly-Relaisstatus durch Readback bestätigt.'
        : (
            source === 'dimplex_modbus_confirmed_readback'
                || source === 'dimplex_modbus_live_readback'
                ? 'Dimplex-SG-Ready-Register durch Readback bestätigt.'
                : 'Bestätigter SG-Ready-Aktorstatus.'
        )) + ageSuffix;
    return {
        visible,
        blocked,
        label,
        sourceTitle,
        iconClass: blocked ? 'fa-ban' : 'fa-toggle-on'
    };
}

function updateModernDashboardActivity(data, values) {
    if (!document.body || !document.body.classList.contains('frontend-modern')) return;
    const wb1Configured = wallboxConfiguredFlag(data, 1);
    const wb2Configured = wallboxConfiguredFlag(data, 2);
    const num = (value) => {
        const parsed = parseFloat(value);
        return Number.isFinite(parsed) ? parsed : 0;
    };
    const gridVal = num(values && values.gridVal);
    const wb1Power = num(data && data.wb);
    const wb2Power = num(data && data.wb2);
    const wpPower = num(values && values.wpVal);
    const hsPower = num(values && values.hsVal);
    const climatePower = num(values && values.climateVal);
    const wb1Present = data && (data.wb_plug === true || data.wb_plug === 1 || data.wb_plug === '1');
    const wb2Locked = data && (data.wb2_locked === true || data.wb2_locked === 1 || data.wb2_locked === '1');
    const wpSgReadyVisible = heatSgReadyPresentation(data).visible;
    setModernCardState('card-wb-wrapper', {active: Math.abs(wb1Power) > 50 || wb1Present, present: wb1Configured});
    setModernCardState('card-wb2-wrapper', {active: Math.abs(wb2Power) > 50 || wb2Locked, present: wb2Configured});
    setModernCardState('card-wp-wrapper', {active: wpPower >= 100 || wpSgReadyVisible, present: true});
    setModernCardState('card-climate-wrapper', {
        active: climatePower > 50,
        present: data && data.climate_online !== false,
        offline: data && data.climate_online === false
    });
    setModernCardState('card-hs-wrapper', {active: hsPower >= 50, present: true});
    const gridContainer = document.getElementById('val-grid-container');
    const gridCard = gridContainer ? gridContainer.closest('.card') : null;
    if (gridCard) {
        gridCard.classList.toggle('modern-grid-import', gridVal > 50);
        gridCard.classList.toggle('modern-grid-export', gridVal < -50);
        gridCard.classList.toggle('modern-grid-idle', Math.abs(gridVal) <= 50);
    }
}

function calculateLiveAutarky(home, wb, wp, grid, climate = 0) {
    let totalVerbrauch = home + wb + wp + Math.max(0, climate || 0);
    let netzBezug = grid > 0 ? grid : 0;

    // Kleine Netzbezüge (Regelabweichungen des WR) ignorieren
    if (netzBezug < 30) netzBezug = 0;
    if (totalVerbrauch <= 0) return 100;

    let autarky = ((totalVerbrauch - netzBezug) / totalVerbrauch) * 100;
    // Schwankungen im obersten Bereich unterdrücken (>= 97% -> 100%)
    if (autarky >= 97) autarky = 100;

    return Math.max(0, Math.min(100, autarky));
}

function loadStatsForDate(val, mode) {
    currentStatsDate = val;
    if (val === 'today') {
        if (typeof fetchData === 'function') fetchData();
        if (typeof updateDashboard === 'function') updateDashboard();
    } else {
        fetch((mode === 'mobile' ? 'mobile.php' : 'index.php') + '?action=get_daily_stats&file=' + encodeURIComponent(val))
        .then(r => r.json())
        .then(data => { updateStatsUI(data, mode); });
    }
}

function updateDailySavedStats(data, prefix) {
    const card = document.getElementById(prefix + 'detail-card-saved');
    if (!card) return;

    const saved = (data && data.saved && typeof data.saved === 'object') ? data.saved : {};
    const readKwh = (...values) => {
        for (const value of values) {
            const num = parseFloat(value);
            if (Number.isFinite(num)) return num;
        }
        return 0;
    };

    const derating = readKwh(saved.derating_today_kwh, data && data.saved_td, data && data.saved_derating_today);
    const inverter = readKwh(saved.inverter_today_kwh, data && data.saved_wb, data && data.saved_inverter_today);
    let total = readKwh(saved.total_today_kwh, data && data.saved_u);
    const computedTotal = derating + inverter;
    if (total <= 0 && computedTotal > 0) total = computedTotal;
    const alltimeDerating = readKwh(saved.derating_total_kwh, data && data.alltime_derating);
    const alltimeInverter = readKwh(saved.inverter_total_kwh, data && data.alltime_inverter);
    let alltimeTotal = readKwh(saved.total_alltime_kwh, saved.total_saved_kwh, data && data.alltime_total);
    const computedAlltimeTotal = alltimeDerating + alltimeInverter;
    if (alltimeTotal <= 0 && computedAlltimeTotal > 0) alltimeTotal = computedAlltimeTotal;
    const hasAlltime = alltimeTotal > 0.0001 || alltimeDerating > 0.0001 || alltimeInverter > 0.0001;

    const setKwh = (id, value) => {
        const el = document.getElementById(prefix + id);
        if (el) el.innerText = (value || 0).toFixed(2) + ' kWh';
    };

    setKwh('stat-saved-total', total);
    setKwh('stat-saved-derating', derating);
    setKwh('stat-saved-inverter', inverter);
    setKwh('stat-saved-total-alltime', alltimeTotal);

    const alltimeRow = document.getElementById(prefix + 'stat-saved-alltime-row');
    if (alltimeRow) alltimeRow.style.display = hasAlltime ? '' : 'none';
    const alltimeLabel = document.getElementById(prefix + 'stat-saved-alltime-label');
    if (alltimeLabel && data && data.alltime_start_date) {
        alltimeLabel.textContent = '';
        const alltimeMain = document.createElement('span');
        alltimeMain.textContent = 'Gesamt gerettet:';
        const alltimeDate = document.createElement('span');
        alltimeDate.className = 'd-block text-muted';
        alltimeDate.style.fontSize = '0.85em';
        alltimeDate.textContent = 'seit ' + data.alltime_start_date;
        alltimeLabel.appendChild(alltimeMain);
        alltimeLabel.appendChild(alltimeDate);
    } else if (alltimeLabel) {
        alltimeLabel.textContent = 'Gesamt gerettet:';
    }

    card.style.display = (total > 0.0001 || derating > 0.0001 || inverter > 0.0001 || hasAlltime) ? '' : 'none';
}

function updateStatsUI(data, mode) {
    const prefix = mode === 'mobile' ? 'm-' : '';
    updateDailySavedStats(data, prefix);

    if (data.autarky_day !== undefined) { const elA = document.getElementById(prefix + 'stat-overlay-autarky'); if (elA) elA.innerText = Math.round(data.autarky_day) + '%'; }
    if (data.selfcon_day !== undefined) { const elS = document.getElementById(prefix + 'stat-overlay-selfcon'); if (elS) elS.innerText = Math.round(data.selfcon_day) + '%'; }

    if (data.stats) {
        const stats = data.stats;
        const setStat = (id, val, pct) => { const el = document.getElementById(prefix + id); if (el) el.innerText = `${(val||0).toFixed(2)} kWh (${(pct||0).toFixed(0)}%)`; };
        const setStatVisible = (id, rowId, val, pct, threshold = 0.05) => {
            setStat(id, val, pct);
            const row = document.getElementById(prefix + rowId);
            if (row) row.style.display = (Math.abs(parseFloat(val || 0)) > threshold) ? '' : 'none';
        };

        const elPvTotal = document.getElementById(prefix + 'stat-pv-total');
        if (elPvTotal) elPvTotal.innerText = `${(stats.total_pv_kwh||0).toFixed(2)} kWh`;
        const elBatTotal = document.getElementById(prefix + 'stat-bat-total');
        if (elBatTotal) elBatTotal.innerText = `${(stats.total_bat_out_kwh||0).toFixed(2)} kWh`;
        const elGridTotal = document.getElementById(prefix + 'stat-grid-total');
        if (elGridTotal) elGridTotal.innerText = `${(stats.total_grid_in_kwh||0).toFixed(2)} kWh`;

        const pvWb = sumKwh(stats, ['pv_wb_kwh', 'pv_wb2_kwh']);
        const batWb = sumKwh(stats, ['bat_wb_kwh', 'bat_wb2_kwh']);
        const gridWb = sumKwh(stats, ['grid_wb_kwh', 'grid_wb2_kwh']);
        const climateTotalRaw = parseFloat(stats.total_climate_kwh);
        const climateTotal = Number.isFinite(climateTotalRaw) ? climateTotalRaw : sumKwh(stats, ['pv_climate_kwh', 'grid_climate_kwh', 'bat_climate_kwh']);

        setStat('stat-pv-e3dc', stats.pv_e3dc_kwh, stats.pv_e3dc_pct);
        setStat('stat-pv-external', stats.pv_external_kwh, stats.pv_external_pct);
        const externalPvStat = document.getElementById(prefix + 'stat-pv-external');
        if (externalPvStat) {
            externalPvStat.title = 'Aus der externen Momentanleistung abgeleiteter Tageswert (ca.); kein direkter Energiezähler.';
        }
        setStatVisible('stat-pv-source-rest', 'stat-pv-source-rest-row', stats.pv_source_rest_kwh, stats.pv_source_rest_pct);
        setStat('stat-pv-home', stats.pv_home_kwh, stats.pv_home_pct); setStat('stat-pv-bat', stats.pv_bat_kwh, stats.pv_bat_pct);
        setStat('stat-pv-wb', pvWb, pctOf(pvWb, stats.total_pv_kwh)); setStat('stat-pv-wp', stats.pv_wp_kwh, stats.pv_wp_pct);
        setStat('stat-pv-climate', stats.pv_climate_kwh, pctOf(stats.pv_climate_kwh, stats.total_pv_kwh));
        setStat('stat-pv-grid', stats.pv_grid_kwh, stats.pv_grid_pct);
        setStat('stat-bat-home', stats.bat_home_kwh, stats.bat_home_pct); setStat('stat-bat-wb', batWb, pctOf(batWb, stats.total_bat_out_kwh));
        setStat('stat-bat-wp', stats.bat_wp_kwh, stats.bat_wp_pct);
        setStat('stat-bat-climate', stats.bat_climate_kwh, pctOf(stats.bat_climate_kwh, stats.total_bat_out_kwh));
        setStat('stat-bat-grid', stats.bat_grid_kwh, stats.bat_grid_pct);
        setStat('stat-grid-home', stats.grid_home_kwh, stats.grid_home_pct); setStat('stat-grid-bat', stats.grid_bat_kwh, stats.grid_bat_pct);
        setStat('stat-grid-wb', gridWb, pctOf(gridWb, stats.total_grid_in_kwh)); setStat('stat-grid-wp', stats.grid_wp_kwh, stats.grid_wp_pct);
        setStat('stat-grid-climate', stats.grid_climate_kwh, pctOf(stats.grid_climate_kwh, stats.total_grid_in_kwh));

        // Zusatzwerte für Mix-Center (Einspeisung, Bat-Laden)
        const setHidden = (id, val) => { const el = document.getElementById(prefix + id); if (el) el.innerText = `${(val||0).toFixed(2)} kWh`; };
        setHidden('stat-grid-out-total', stats.total_grid_out_kwh);
        setHidden('stat-bat-in-total', stats.total_bat_in_kwh);

        // Energiebilanz Legende aktualisieren (mobile + desktop)
        const setMix = (id, v) => { const e = document.getElementById(prefix + id); if (e) e.innerText = (v||0).toFixed(1); };
        setMix('stat-mix-pv', stats.total_pv_kwh);
        setMix('stat-mix-bat', stats.total_bat_out_kwh);
        setMix('stat-mix-grid', stats.total_grid_in_kwh);
        setMix('stat-mix-feedin', stats.total_grid_out_kwh);
        setMix('stat-mix-bat-in', stats.total_bat_in_kwh);
        setMix('stat-mix-climate', climateTotal);

        // CO2-Baum & Wert berechnen
        const CO2_FACTOR = 0.38;
        const pvSelf = Math.max(0, (stats.total_pv_kwh||0) - (stats.total_grid_out_kwh||0));
        const co2 = (pvSelf + (stats.total_bat_out_kwh||0)) * CO2_FACTOR;
        const co2El = document.getElementById(prefix + 'stat-co2-value');
        if (co2El) co2El.innerText = co2.toFixed(1);

        const autarkyVal = data.autarky_day || 0;
        const treeEl = document.getElementById(prefix + 'co2-tree');
        if (treeEl) {
            let tree, sz;
            if (autarkyVal >= 95)      { tree = '🌲🌳🌲'; sz = prefix ? '1.6rem' : '2.2rem'; }
            else if (autarkyVal >= 80) { tree = '🌲🌳';   sz = prefix ? '1.7rem' : '2.4rem'; }
            else if (autarkyVal >= 60) { tree = '🌳';      sz = prefix ? '1.8rem' : '2.8rem'; }
            else if (autarkyVal >= 40) { tree = '🪴';      sz = prefix ? '1.8rem' : '2.5rem'; }
            else if (autarkyVal >= 20) { tree = '🌿';      sz = prefix ? '1.8rem' : '2.5rem'; }
            else                       { tree = '🌱';      sz = prefix ? '1.8rem' : '2.5rem'; }
            treeEl.innerText = tree;
            treeEl.style.fontSize = sz;
        }
    }

    if (data.costs) {
        const formatEuro = (val, withPlus = false) => {
            const num = Number(val || 0);
            const sign = withPlus && num > 0 ? '+ ' : '';
            return sign + num.toFixed(2) + ' €';
        };
        const setCost = (id, val, options = {}) => {
            const el = document.getElementById(prefix + id);
            if (el) el.innerText = formatEuro(val, Boolean(options.plus));
        };
        const costTotal = Number(data.costs.total || 0);
        const saveTotal = Number(data.costs.save_total || 0);
        const eegRevenue = Number(data.costs.eeg_revenue || 0);
        const netCostTotal = costTotal - eegRevenue;

        setCost('stat-cost-total', netCostTotal);
        setCost('stat-cost-home', data.costs.home);
        setCost('stat-cost-wb', (data.costs.wb || 0) + (data.costs.wb2 || 0));
        setCost('stat-cost-wp', data.costs.wp);
        setCost('stat-cost-climate', data.costs.climate);

        setCost('stat-save-total', saveTotal);
        setCost('stat-save-home', data.costs.save_home);
        setCost('stat-save-wb', (data.costs.save_wb || 0) + (data.costs.save_wb2 || 0));
        setCost('stat-save-wp', data.costs.save_wp);
        setCost('stat-save-climate', data.costs.save_climate);

        let resultTotal = Number.isFinite(Number(data.costs.result_total))
            ? Number(data.costs.result_total)
            : saveTotal + eegRevenue - costTotal;
        setCost('stat-result-total', resultTotal);
        const costTotalEl = document.getElementById(prefix + 'stat-cost-total');
        if (costTotalEl) {
            costTotalEl.classList.remove('text-danger', 'text-success', 'text-body');
            costTotalEl.classList.add(netCostTotal < -0.005 ? 'text-success' : (netCostTotal > 0.005 ? 'text-danger' : 'text-body'));
        }

        const eegActive = Boolean(data.costs.eeg_enabled && data.costs.eeg_in_support && Number(data.costs.eeg_tariff_ct || 0) > 0);
        const eegRow = document.getElementById(prefix + 'stat-eeg-row');
        const eegNote = document.getElementById(prefix + 'stat-eeg-note');
        if (eegRow) eegRow.style.display = eegActive ? '' : 'none';
        setCost('stat-eeg-total', eegRevenue, { plus: true });
        if (eegNote) {
            if (eegActive) {
                const gridText = Number(data.costs.eeg_grid_out_kwh || 0).toLocaleString('de-DE', { minimumFractionDigits: 1, maximumFractionDigits: 1 });
                const tariffText = Number(data.costs.eeg_tariff_ct || 0).toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 3 });
                eegNote.innerText = `${gridText} kWh x ${tariffText} ct/kWh`;
                eegNote.style.display = '';
            } else {
                eegNote.innerText = '';
                eegNote.style.display = 'none';
            }
        }

        const dvReport = (data.direct_marketing_daily_report && typeof data.direct_marketing_daily_report === 'object')
            ? data.direct_marketing_daily_report
            : (data.storage_plan_meta && data.storage_plan_meta.direct_marketing_daily_report && typeof data.storage_plan_meta.direct_marketing_daily_report === 'object'
                ? data.storage_plan_meta.direct_marketing_daily_report
                : null);
        const dvRevenue = dvReport ? Number(dvReport.real_net_export_revenue_eur) : NaN;
        const dvEnergy = dvReport ? Number(dvReport.real_export_kwh) : NaN;
        const dvSaleRow = document.getElementById(prefix + 'stat-dv-battery-sale-row');
        const dvSaleEl = document.getElementById(prefix + 'stat-dv-battery-sale');
        const dvSaleNote = document.getElementById(prefix + 'stat-dv-battery-sale-note');
        const dvSaleValid = Number.isFinite(dvRevenue) && Number.isFinite(dvEnergy) && dvEnergy > 0;
        if (dvSaleRow) dvSaleRow.style.display = dvSaleValid ? '' : 'none';
        if (dvSaleEl) dvSaleEl.innerText = dvSaleValid ? formatEuro(dvRevenue, true) : '—';
        if (dvSaleNote) {
            dvSaleNote.innerText = dvSaleValid
                ? `${dvEnergy.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1})} kWh · Ø ${(dvRevenue * 100 / dvEnergy).toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2})} ct/kWh · separat vom Endergebnis`
                : '';
            dvSaleNote.style.display = dvSaleValid ? '' : 'none';
        }

        const elAvg = document.getElementById(prefix + 'stat-avg-price');
        if (elAvg) elAvg.innerText = (data.costs.avg_price || 0).toFixed(1) + ' ct/kWh';

        // Mobile: Kosten-Bereich einblenden wenn Daten da sind
        const mCostCard = document.getElementById(prefix + 'detail-card-costs');
        if (mCostCard && prefix === 'm-') mCostCard.style.display = '';
    }
}

function toggleStatsView(mode = 'desktop') {
    statsViewActive = !statsViewActive;
    const elId = mode === 'mobile' ? 'm-stats-view' : 'stats-view';
    const chevronId = mode === 'mobile' ? 'm-stats-chevron' : null;
    const el = document.getElementById(elId);
    if (el) el.style.display = statsViewActive ? 'block' : 'none';
    if (chevronId) { const ch = document.getElementById(chevronId); if (ch) ch.className = statsViewActive ? 'fas fa-chevron-up text-muted' : 'fas fa-chevron-down text-muted'; }

    // Mobile: Beim Öffnen sofort Stats laden damit CO2-Baum & Bilanz angezeigt werden
    if (statsViewActive && mode === 'mobile') {
        fetch('mobile.php?action=get_daily_stats&file=today')
            .then(r => r.json())
            .then(data => { updateStatsUI(data, 'mobile'); })
            .catch(() => {});
    }
}

/**
 * Berechnet Sonnenstand und theoretische PV-Leistung.
 * Benötigt die globalen Konstanten: PV_STRINGS, LAT, LON
 */

function getSunPosition() {
    if (typeof LAT === 'undefined' || typeof LON === 'undefined') return null;
    const now = new Date();
    const rad = Math.PI / 180;
    const start = new Date(now.getFullYear(), 0, 0);
    const diff = now - start;
    const dayOfYear = Math.floor(diff / (1000 * 60 * 60 * 24));

    const B = (360 / 365) * (dayOfYear - 81) * rad;
    const declination = 23.45 * Math.sin(B) * rad;
    const eot = 9.87 * Math.sin(2 * B) - 7.53 * Math.cos(B) - 1.5 * Math.sin(B);

    const lst = now.getUTCHours() + now.getUTCMinutes() / 60 + LON / 15 + eot / 60;
    const omega = (lst - 12) * 15 * rad;
    const latRad = LAT * rad;

    const sinEl = Math.sin(latRad) * Math.sin(declination) + Math.cos(latRad) * Math.cos(declination) * Math.cos(omega);
    const el = Math.asin(sinEl);

    return { el, sinEl, declination, omega, latRad };
}

function isDaytime() {
    const pos = getSunPosition();
    if (!pos) return true;
    // Tag = Elevation > -3 Grad (bürgerliche Dämmerung beginnt bei -6, aber für PV/Optik ist -3 gut)
    return (pos.el * 180 / Math.PI) > -3;
}

function getTheoreticalPower() {
    if (typeof PV_STRINGS === 'undefined' || !PV_STRINGS || PV_STRINGS.length === 0) return 0;

    const pos = getSunPosition();
    if (!pos || pos.el < 0) return 0; // Nacht (geometrisch)

    const cosAzS = (Math.sin(pos.latRad) * pos.sinEl - Math.sin(pos.declination)) / (Math.cos(pos.latRad) * Math.cos(pos.el));
    let sunAz = Math.acos(Math.min(1, Math.max(-1, cosAzS)));
    if (pos.omega < 0) sunAz = -sunAz;

    let totalW = 0;
    const airMass = 1 / Math.max(0.05, pos.sinEl);
    const transmission = (typeof PV_ATMOSPHERE !== 'undefined') ? PV_ATMOSPHERE : 0.7;
    const intensityFactor = 1.35 * Math.pow(transmission, Math.pow(airMass, 0.678));

    for (let s of PV_STRINGS) {
        const tiltRad = s.tilt * (Math.PI / 180);
        const panelAz = s.azimuth * (Math.PI / 180);
        const cosTheta = pos.sinEl * Math.cos(tiltRad) + Math.cos(pos.el) * Math.sin(tiltRad) * Math.cos(sunAz - panelAz);
        if (cosTheta > 0) totalW += s.power * cosTheta * intensityFactor;
    }
    return totalW;
}

/**
 * SYSTEM FUNCTIONS (Shared between Desktop & Mobile)
 */

function restartService(skipConfirm = false) {
    if (!skipConfirm && !confirm('🚨 NOTFALL-NEUSTART & RESET 🚨\n\nMöchtest du alle Dienste neustarten?\nDabei werden auch aktive Boost-Vorgänge (Fahrzeug, Wärmepumpe, Batteriepuffer) zurückgesetzt und genullt.\n\nE3DC-Control beendet sich vorher sicher (Daten speichern).')) return;

    fetch('index.php?action=restart_service', {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
        }
    })
        .then(async response => {
            let data = null;
            try { data = await response.json(); } catch (_) { data = null; }
            if (!response.ok || !data || data.success !== true) {
                throw new Error(data && data.message ? String(data.message) : ('HTTP ' + response.status));
            }
            return data;
        })
        .then(() => {
                alert("✓ Alle Dienste wurden zurückgesetzt und neu gestartet.\nDas Web-Interface ist kurzzeitig eventuell nicht erreichbar.");
        })
        .catch(err => alert("✗ Neustart nicht bestätigt: " + err.message));
}

function fixPermissions(btnId = 'btn-repair-permissions') {
    // Rechteprojektion, Backup, Releaseabgleich und Dienstneustart gehören
    // demselben root-eigenen, argumentlosen Systemjob. Es gibt absichtlich
    // keinen zweiten privilegierten Webpfad für nutzerbeschreibbaren Code.
    return startInstallerUpdate(btnId);
}

function showWatchdogLog() {
    const modalEl = document.getElementById('watchdogModal');
    if (!modalEl) return;
    const modal = new bootstrap.Modal(modalEl);
    modal.show();

    const contentEl = document.getElementById('watchdog-log-content');
    if(contentEl) contentEl.innerText = 'Lade Protokoll...';

    // Action ist in helpers.php definiert und überall verfügbar
    fetch('index.php?action=watchdog_log')
        .then(r => r.text())
        .then(text => {
            if(contentEl) contentEl.innerText = text;
        })
        .catch(e => {
            if(contentEl) contentEl.innerText = 'Fehler beim Laden.';
        });
}

function showHALog() {
    const modalEl = document.getElementById('haModal');
    if (!modalEl) return;
    const modal = new bootstrap.Modal(modalEl);
    modal.show();

    const contentEl = document.getElementById('ha-log-content');
    if(contentEl) contentEl.innerText = 'Lade Protokoll...';

    // Dynamischer Aufruf relativ zur aktuellen Datei (index.php oder mobile.php)
    fetch('?action=get_ha_log')
        .then(r => r.text())
        .then(text => {
            if(contentEl) {
                const reversedText = text.trim().split('\n').reverse().join('\n');
                contentEl.innerText = reversedText;
            }
        })
        .catch(e => {
            if(contentEl) contentEl.innerText = 'Fehler beim Laden.';
        });
}

function handleConnectionClick() {
    const badge = document.getElementById('connection-status');
    if (!badge) return;

    // Check auf Klassen (Bootstrap Farben) oder Text
    const isOffline = badge.classList.contains('bg-danger') || badge.classList.contains('bg-warning') || badge.innerText === 'Offline' || badge.innerText.includes('Veraltet');

    if (isOffline) {
        if (confirm("Verbindungsprobleme erkannt.\nMöchtest du den E3DC-Service neu starten?")) {
            restartService();
        } else {
            badge.innerText = "Lade...";
            if (typeof fetchData === 'function') fetchData(); // Desktop
            else if (typeof updateDashboard === 'function') updateDashboard(); // Mobile
            else location.reload();
        }
    } else {
        badge.innerText = "Aktualisiere...";
        if (typeof fetchData === 'function') fetchData();
        else if (typeof updateDashboard === 'function') updateDashboard();
        else location.reload();
    }
}

function forceSocUpdate() {
    // Schutz: Nur aufwecken wenn Bluelink Token konfiguriert ist
    if (window._hasBluelink === false || window._forceSocUpdatePending === true) return;

    const socBadges = document.querySelectorAll('#val-car-soc, #f-val-car-soc, #val-car-soc2, #f-val-car-soc2');

    // Schutz: Nicht aufwecken, wenn es ein Gast-Auto ist
    let isGuest = false;
    socBadges.forEach(el => {
        if (el && el.innerText.includes('Gast')) {
            isGuest = true;
        }
    });

    if (isGuest) return;

    const previousBadges = [];
    socBadges.forEach(el => {
        if (el) previousBadges.push([el, el.innerHTML]);
        if (el) el.innerHTML = '<i class="fas fa-sync fa-spin"></i>';
    });

    const fzBtn = document.getElementById('fz-update-btn');
    const previousButtonHtml = fzBtn ? fzBtn.innerHTML : '';
    if (fzBtn) {
        fzBtn.innerHTML = '<i class="fas fa-sync-alt fa-spin me-1"></i> Wecke auf...';
        fzBtn.disabled = true;
    }

    window._forceSocUpdatePending = true;
    const restore = () => {
        previousBadges.forEach(([el, html]) => { el.innerHTML = html; });
        if (fzBtn) {
            fzBtn.innerHTML = previousButtonHtml;
            fzBtn.disabled = false;
        }
    };
    const request = e3dcPostAction('action=force_soc').then(async response => {
        let payload = null;
        try {
            payload = await response.json();
        } catch (_error) {
            throw new Error('Ungültige Antwort beim Aufwecken des Fahrzeugs.');
        }
        if (!response.ok || payload.success !== true) {
            throw new Error(payload.message || 'Die Fahrzeug-Anforderung wurde abgelehnt.');
        }
        return payload;
    });
    const timeout = new Promise((_, reject) => {
        setTimeout(() => reject(new Error('Zeitüberschreitung beim Aufwecken des Fahrzeugs.')), 10000);
    });
    Promise.race([request, timeout])
        .then(payload => {
            if (fzBtn) {
                fzBtn.innerHTML = '<i class="fas fa-check me-1"></i> Anfrage angenommen';
            }
            setTimeout(() => {
                restore();
                window._forceSocUpdatePending = false;
            }, 2000);
        })
        .catch(error => {
            restore();
            window._forceSocUpdatePending = false;
            alert(error && error.message ? error.message : 'Das Fahrzeug konnte nicht aufgeweckt werden.');
        });
}

function e3dcActionEndpoint() {
    if (window.E3DC_ACTION_ENDPOINT) return window.E3DC_ACTION_ENDPOINT;
    const entry = (window.location.pathname.split('/').pop() || 'index.php').toLowerCase();
    return entry === 'mobile.php' ? 'mobile.php' : 'index.php';
}

function e3dcActionUrl(query) {
    const q = String(query || '').replace(/^\?/, '');
    return e3dcActionEndpoint() + (q ? '?' + q : '');
}

function e3dcPostAction(query, values = {}) {
    const body = new URLSearchParams();
    Object.entries(values || {}).forEach(([key, value]) => {
        body.set(key, String(value));
    });
    body.set('csrf_token', String(window.E3DC_CSRF_TOKEN || ''));
    return fetch(e3dcActionUrl(query), {
        method: 'POST',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
        },
        body
    });
}

const E3DC_LIVE_AUTH_RELOAD_KEY = 'e3dc-live-auth-reload-at';
const E3DC_LIVE_AUTH_RELOAD_MIN_GAP_MS = 30000;

function e3dcLiveAuthBlocked() {
    return window.E3DC_LIVE_AUTH_BLOCKED === true;
}

function e3dcClearLiveAuthRecovery() {
    window.E3DC_LIVE_AUTH_BLOCKED = false;
    delete window.E3DC_LIVE_AUTH_RELOAD_ATTEMPTED;
    try {
        sessionStorage.removeItem(E3DC_LIVE_AUTH_RELOAD_KEY);
    } catch (_error) {
        // Gesperrter Session-Storage darf einen erfolgreichen Live-Lesezugriff nicht entwerten.
    }
}

function e3dcFetchLiveJson(url = 'get_live_json.php', options = {}) {
    const token = String(window.E3DC_CSRF_TOKEN || '');
    const body = new URLSearchParams();
    body.set('csrf_token', token);
    return fetch(String(url || 'get_live_json.php'), {
        method: 'POST',
        cache: 'no-store',
        credentials: 'same-origin',
        headers: {
            'Content-Type': 'application/x-www-form-urlencoded',
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': token
        },
        signal: options && options.signal ? options.signal : undefined,
        body
    });
}

async function e3dcReadLiveJsonResponse(response) {
    let payload = null;
    try {
        payload = await response.json();
    } catch (_error) {
        payload = null;
    }
    if (!response.ok) {
        const reason = payload && typeof payload === 'object'
            ? String(payload.error || payload.reason || payload.message || '')
            : '';
        const error = new Error(reason || ('HTTP ' + response.status));
        error.httpStatus = Number(response.status || 0);
        error.payload = payload;
        throw error;
    }
    return payload;
}

function e3dcHandleLiveAuthFailure(error) {
    const status = Number(error && error.httpStatus ? error.httpStatus : 0);
    const payload = error && error.payload && typeof error.payload === 'object'
        ? error.payload
        : {};
    const reason = String(payload.error || payload.reason || payload.message || '').trim();
    if (status !== 401 && status !== 403) return false;
    if (reason === 'PIN erforderlich') {
        window.E3DC_LIVE_AUTH_BLOCKED = true;
        window.location.replace(e3dcActionUrl('seite=lock'));
        return true;
    }
    if (reason !== 'CSRF-Token ungültig') return false;

    window.E3DC_LIVE_AUTH_BLOCKED = true;
    if (window.E3DC_LIVE_AUTH_RELOAD_ATTEMPTED === true) return true;
    const now = Date.now();
    let lastReloadAt = 0;
    try {
        lastReloadAt = Number(sessionStorage.getItem(E3DC_LIVE_AUTH_RELOAD_KEY) || 0);
        const reloadDue = !Number.isFinite(lastReloadAt)
            || lastReloadAt <= 0
            || lastReloadAt > now
            || (now - lastReloadAt) >= E3DC_LIVE_AUTH_RELOAD_MIN_GAP_MS;
        if (!reloadDue) return true;
        sessionStorage.setItem(E3DC_LIVE_AUTH_RELOAD_KEY, String(now));
    } catch (_error) {
        // Ohne sessiongebundenen Reload-Beleg nicht automatisch neu laden:
        // so bleibt ein Auth-Fehler auch bei gesperrtem Storage schleifenfrei.
        return true;
    }
    window.E3DC_LIVE_AUTH_RELOAD_ATTEMPTED = true;
    window.location.reload();
    return true;
}

async function e3dcParseJsonResponse(response, context = 'Anfrage') {
    const text = await response.text();
    if (!response.ok) {
        throw new Error(context + ': HTTP ' + response.status);
    }
    const trimmed = text.trim();
    if (!trimmed) {
        throw new Error(context + ': leere Antwort');
    }
    try {
        return JSON.parse(trimmed);
    } catch (parseErr) {
        const preview = trimmed.slice(0, 160).replace(/\s+/g, ' ');
        if (/^<!doctype html/i.test(trimmed) || /^<html/i.test(trimmed)) {
            throw new Error(context + ': Webserver lieferte HTML statt JSON. Bitte Seite nach dem Update neu laden. Vorschau: ' + preview);
        }
        throw new Error(context + ': ungültige JSON-Antwort (' + parseErr.message + '). Vorschau: ' + preview);
    }
}

// Installer / Diagramm Update Logik
function resetInstallerUpdateBadge() {
    const badge = document.getElementById('update-badge-installer');
    const btn = document.getElementById('btn-update-installer');
    if (badge) {
        badge.style.display = 'none';
        badge.innerText = '!';
    }
    if (btn) {
        btn.classList.remove('btn-info', 'text-dark');
        btn.classList.add('btn-outline-info');
    }
}

function checkInstallerUpdate(force = false) {
    const query = 'action=check_self_update' + (force ? '&force=1&t=' + Date.now() : '');
    e3dcPostAction(query)
    .then(r => e3dcParseJsonResponse(r, 'Web-UI Update-Check'))
    .then(data => {
        const badge = document.getElementById('update-badge-installer');
        const btn = document.getElementById('btn-update-installer');
        const missing = Number(data && data.missing);
        if (data.success && Number.isFinite(missing) && missing > 0) {
            if(badge) {
                badge.style.display = 'inline-block';
                badge.innerText = missing;
            }
            if(btn) {
                btn.classList.remove('btn-outline-info');
                btn.classList.add('btn-info', 'text-dark');
            }
        } else {
            resetInstallerUpdateBadge();
        }
    }).catch(e => console.error("Self update check failed", e));
}

// Beim Laden nur informativ prüfen. Der Update-Start ist weder von diesem
// Netzwerkcheck noch von einem Versionsvergleich abhängig.
document.addEventListener('DOMContentLoaded', () => checkInstallerUpdate(false));

function startInstallerUpdate(btnId = 'btn-update-installer') {
    const btn = document.getElementById(btnId);
    let origText = '';
    if(btn) {
        origText = btn.innerHTML;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Starte...';
        btn.disabled = true;
    }
    const question = "Möchtest Du E3DC-Control auf den veröffentlichten Stable-Stand aktualisieren oder die installierte Version reparieren?\n\nDer Updater erstellt zuerst ein Backup und startet die Dienste nach dem kurzen Dateiaustausch neu.";
    if (!confirm(question)) {
        if (btn) { btn.innerHTML = origText; btn.disabled = false; }
        return;
    }
    startInstallerUpdateRun(btn, origText);
}

function startInstallerUpdateRun(btn, origText) {
    if(btn) {
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i> Starte...';
        btn.disabled = true;
    }
    resetInstallerUpdateBadge();

    const modalEl = document.getElementById('updateModal');
    const modal = modalEl && window.bootstrap ? new bootstrap.Modal(modalEl) : null;
    const title = document.getElementById('update-modal-title');
    const log = document.getElementById('update-log');
    const spinner = document.getElementById('update-spinner');
    const closeBtn = document.getElementById('update-close-btn');
    const finishBtn = document.getElementById('update-finish-btn');
    const details = document.getElementById('update-details');
    const updateStartedAt = Date.now();

    if (title) title.innerText = "System Update";
    if (log) log.innerText = "Starte System Update...\n";
    if (details) details.open = false;
    if (spinner) spinner.className = "fas fa-sync fa-spin me-2";
    if (closeBtn) closeBtn.style.display = 'none';
    if (finishBtn) {
        finishBtn.disabled = true;
        finishBtn.innerText = "Schließen";
        finishBtn.onclick = null;
    }
    e3dcRenderInstallerUpdateStatus({logText: "", running: true}, updateStartedAt);
    if (modal) modal.show();

    e3dcPostAction('action=run_self_update&t=' + Date.now(), {
        reinstall: '0'
    })
    .then(r => e3dcParseJsonResponse(r, 'System-Update-Start'))
    .then(data => {
        if (data && data.success) {
            if (log) log.innerText = (data.message || "Update gestartet.") + "\nWarte auf Log-Ausgabe...\n";
            pollInstallerUpdate(log, spinner, closeBtn, finishBtn, btn, origText, updateStartedAt);
        } else {
            const msg = "Update konnte nicht gestartet werden:\n" + ((data && data.message) || "Unbekannter Fehler");
            if (log) log.innerText = msg;
            if (spinner) {
                spinner.classList.remove('fa-spin', 'fa-sync');
                spinner.classList.add('fa-times-circle', 'text-danger');
            }
            if (closeBtn) closeBtn.style.display = 'block';
            if (finishBtn) finishBtn.disabled = false;
            e3dcRenderInstallerUpdateStatus({logText: msg, running: false, errorFound: true}, updateStartedAt);
            if (details) details.open = true;
            if (!modal) alert(msg);
            if(btn) { btn.innerHTML = origText; btn.disabled = false; }
            checkInstallerUpdate(true);
        }
    })
    .catch(err => {
        const msg = "Update-Start konnte keine gültige JSON-Antwort lesen:\n" + err.message
                  + "\n\nPrüfe das Update-Protokoll weiter; während des Webdatei-Tauschs kann die Oberfläche kurz HTML liefern.";
        if (log) log.innerText = msg;
        if (!modal) alert(msg);
        if (log) {
            pollInstallerUpdate(log, spinner, closeBtn, finishBtn, btn, origText, updateStartedAt);
        } else if(btn) {
            btn.innerHTML = origText;
            btn.disabled = false;
        }
    });
}

function e3dcFormatInstallerUpdateElapsed(startedAt) {
    const seconds = Math.max(0, Math.floor((Date.now() - startedAt) / 1000));
    const minutes = Math.floor(seconds / 60);
    return minutes + " min " + String(seconds % 60).padStart(2, "0") + " s";
}

function e3dcRenderInstallerUpdateStatus(updateStatus, startedAt) {
    const status = (updateStatus && typeof updateStatus === "object") ? updateStatus : {};
    const logText = (typeof status.logText === "string") ? status.logText : "";
    const summary = document.getElementById('update-status-summary');
    const title = document.getElementById('update-status-title');
    const detail = document.getElementById('update-status-detail');
    const step = document.getElementById('update-status-step');
    const elapsed = document.getElementById('update-status-elapsed');
    const progress = document.getElementById('update-progress-bar');
    const details = document.getElementById('update-details');
    if (!summary || !title || !detail || !step || !elapsed || !progress) return;

    let titleText = "Update wird vorbereitet";
    let detailText = "Die Anlage arbeitet weiter.";
    let stepText = "Start";
    let progressWidth = 5;
    let alertClass = "alert-info";
    let badgeClass = "bg-info text-dark";
    const servicesConfirmed = logText.includes("[STATUS] Regelung und Weboberfläche laufen wieder.");

    if (logText.includes("[1/4]")) {
        titleText = "Sicherung und Integritätsprüfung";
        detailText = "Die Anlage arbeitet während dieser längsten Phase normal weiter.";
        stepText = "Schritt 1 von 4";
        progressWidth = 22;
    }
    if (logText.includes("[2/4]")) {
        titleText = "Kurze Anlagenunterbrechung";
        detailText = "Regelung und Weboberfläche sind für den kontrollierten Dateiaustausch angehalten.";
        stepText = "Schritt 2 von 4";
        progressWidth = 48;
        alertClass = "alert-warning";
        badgeClass = "bg-warning text-dark";
    }
    if (logText.includes("[3/4]")) {
        titleText = "Produktdateien und Rechte werden aktualisiert";
        detailText = "Die kurze kontrollierte Anlagenunterbrechung dauert noch an.";
        stepText = "Schritt 3 von 4";
        progressWidth = 70;
        alertClass = "alert-warning";
        badgeClass = "bg-warning text-dark";
    }
    if (logText.includes("[4/4]")) {
        titleText = "Dienste werden gestartet und geprüft";
        detailText = "Regelung und Weboberfläche kehren jetzt kontrolliert in Betrieb zurück.";
        stepText = "Schritt 4 von 4";
        progressWidth = 88;
    }
    if (servicesConfirmed) {
        titleText = "Anlage läuft wieder";
        detailText = "Nur Abschlussbereinigung und Backup-Limit werden noch geprüft.";
        stepText = "Abschlussprüfung";
        progressWidth = 96;
        alertClass = "alert-info";
        badgeClass = "bg-info text-dark";
    }
    if (status.successFound) {
        titleText = "Update erfolgreich abgeschlossen";
        detailText = "Regelung, Weboberfläche und Rückfallweg wurden bestätigt.";
        stepText = "Fertig";
        progressWidth = 100;
        alertClass = "alert-success";
        badgeClass = "bg-success";
    } else if (status.abortedFound) {
        titleText = "Update wurde beendet";
        detailText = "Die technischen Details nennen den bestätigten Systemzustand und den nächsten Schritt.";
        stepText = "Beendet";
        progressWidth = 100;
        alertClass = "alert-secondary";
        badgeClass = "bg-secondary";
        if (details) details.open = true;
    } else if (status.errorFound || status.exitFailed || status.completionFailed) {
        titleText = "Update benötigt Aufmerksamkeit";
        detailText = "Die technischen Details nennen Ursache, Systemzustand und Lösung.";
        stepText = "Fehler";
        alertClass = "alert-danger";
        badgeClass = "bg-danger";
        if (details) details.open = true;
    }

    summary.className = "alert " + alertClass + " mb-2";
    title.textContent = titleText;
    detail.textContent = detailText;
    step.className = "badge " + badgeClass + " text-nowrap";
    step.textContent = stepText;
    elapsed.textContent = "Laufzeit: " + e3dcFormatInstallerUpdateElapsed(startedAt);
    progress.style.width = progressWidth + "%";
    progress.classList.toggle("progress-bar-animated", !status.successFound && !status.errorFound && !status.exitFailed && !status.completionFailed && !status.abortedFound);
}

function e3dcClassifyInstallerUpdatePoll(data) {
    const payload = (data && typeof data === "object") ? data : {};
    const logText = (typeof payload.log === "string") ? payload.log : "";
    const running = payload.running === true;
    const releaseCompletionFound = /(?:^|\r?\n)\[OK\]\s+Update abgeschlossen\.\s*(?:\r?\n)+Version:\s*v?\d+\.\d+\.\d+[A-Za-z0-9._-]*/i.test(logText);
    const successMarkerFound = releaseCompletionFound ||
                               logText.includes("Update erfolgreich abgeschlossen") ||
                               logText.includes("Update abgeschlossen") ||
                               /\[OK\]\s+self-update auf [0-9a-f]{40} abgeschlossen\./i.test(logText) ||
                               logText.includes("Du bist auf dem neuesten Stand");
    const exitCode = Number.isInteger(payload.exit_code) ? payload.exit_code : null;
    const exitKnown = exitCode !== null;
    const exitOk = exitKnown && exitCode === 0;
    const exitFailed = exitKnown && exitCode !== 0;
    const completionSucceeded = payload.completion === "success";
    const completionFailed = payload.completion === "failed";
    const abortedFound = logText.includes("Vorgang abgebrochen");
    const errorFound = /(traceback|exception|critical|fatal|permission denied|REPAIR_REQUIRED|\[!\]\s+(?:self-update|web-update) fehlgeschlagen|self-update fehlgeschlagen:|web-update kann nicht starten|konnte update-prozess nicht starten)/i.test(logText);
    const successFound = !running &&
                         !exitFailed &&
                         !completionFailed &&
                         !errorFound &&
                         (exitOk ||
                          (!running && completionSucceeded) ||
                          (!running && !exitKnown && successMarkerFound));

    return {
        logText,
        running,
        exitCode,
        exitKnown,
        exitFailed,
        completionFailed,
        abortedFound,
        errorFound,
        successFound,
    };
}

function pollInstallerUpdate(log, spinner, closeBtn, finishBtn, btn, origText, updateStartedAt = Date.now()) {
    let tick = 0;
    let stoppedPolls = 0;
    let transientPollErrors = 0;
    let lastUpdateStatus = {logText: "", running: true};
    const maxStoppedGracePolls = 6;
    const maxTransientPollErrors = 120;
    const maxPollTicks = 60 * 60;
    const interval = setInterval(() => {
        tick++;
        fetch(e3dcActionUrl('action=poll_self_update&t=' + Date.now()))
            .then(async r => {
                const text = await r.text();
                if (!r.ok) {
                    throw new Error('HTTP ' + r.status);
                }
                if (!text.trim()) {
                    throw new Error('Leere Update-Antwort');
                }
                try {
                    return JSON.parse(text);
                } catch (parseErr) {
                    throw new Error('Ungültige Update-Antwort: ' + parseErr.message);
                }
            })
            .then(data => {
                transientPollErrors = 0;
                const updateStatus = e3dcClassifyInstallerUpdatePoll(data);
                const {
                    logText,
                    running,
                    exitCode,
                    exitKnown,
                    exitFailed,
                    completionFailed,
                    abortedFound,
                    errorFound,
                    successFound,
                } = updateStatus;
                lastUpdateStatus = updateStatus;
                e3dcRenderInstallerUpdateStatus(updateStatus, updateStartedAt);
                if (log && logText) {
                    log.innerText = logText;
                    log.scrollTop = log.scrollHeight;
                }

                if (!running && !successFound && !exitKnown && !completionFailed && !abortedFound && !errorFound && tick < maxPollTicks) {
                    stoppedPolls++;
                    if (stoppedPolls <= maxStoppedGracePolls) {
                        if (stoppedPolls === 1 && log) {
                            log.innerText += "\n\n[INFO] Update-Prozess beendet, warte auf Abschlussstatus und letzte Logzeilen...";
                        }
                        return;
                    }
                } else {
                    stoppedPolls = 0;
                }

                if (!running || successFound || exitKnown || completionFailed || abortedFound || errorFound || tick >= maxPollTicks) {
                    clearInterval(interval);
                    const ok = successFound && !exitFailed && !completionFailed && !errorFound;
                    if (spinner) {
                        spinner.classList.remove('fa-spin', 'fa-sync');
                        if (abortedFound && !ok) {
                            spinner.classList.add('fa-info-circle', 'text-info');
                        } else {
                            spinner.classList.add(ok ? 'fa-check-circle' : 'fa-times-circle', ok ? 'text-success' : 'text-danger');
                        }
                    }
                    if (log && !ok) {
                        if (abortedFound) {
                            log.innerText += "\n\n[INFO] System Update wurde abgebrochen.";
                        } else if (tick >= maxPollTicks) {
                            log.innerText += "\n\n[HINWEIS] Die Weboberfläche beendet das Polling nach 60 Minuten. "
                                + "Der Updateprozess wird dadurch nicht beendet; bitte Konsolen- oder Diagnose-Log prüfen.";
                        } else if (exitFailed) {
                            log.innerText += "\n\n[FEHLER] System Update beendet mit Exitcode " + exitCode + ".";
                        } else {
                            log.innerText += "\n\n[FEHLER] System Update beendet, aber ohne eindeutige Erfolgsmeldung.";
                        }
                    }
                    if (closeBtn) closeBtn.style.display = 'block';
                    if (finishBtn) {
                        finishBtn.disabled = false;
                        finishBtn.innerText = ok ? "Neu laden" : "Schließen";
                        finishBtn.onclick = ok ? () => location.reload() : null;
                    }
                    if(btn) { btn.innerHTML = origText; btn.disabled = false; }
                    checkInstallerUpdate(true);
                }
            })
            .catch(err => {
                transientPollErrors++;
                if (transientPollErrors <= maxTransientPollErrors && tick < maxPollTicks) {
                    e3dcRenderInstallerUpdateStatus(lastUpdateStatus, updateStartedAt);
                    const statusDetail = document.getElementById('update-status-detail');
                    if (statusDetail) {
                        statusDetail.textContent = "Die Weboberfläche wird gerade neu gestartet. "
                            + "Der Updateauftrag läuft unabhängig weiter.";
                    }
                    if (log && (transientPollErrors === 1 || transientPollErrors % 15 === 0)) {
                        log.innerText += "\n\n[INFO] Update-Oberfläche kurz nicht erreichbar (" + transientPollErrors + "/" + maxTransientPollErrors + "): " + err.message + "\nPolling läuft weiter...";
                    }
                    return;
                }
                clearInterval(interval);
                if (log) log.innerText += "\n\nPolling-Fehler: " + err;
                e3dcRenderInstallerUpdateStatus(
                    {...lastUpdateStatus, running: false, errorFound: true},
                    updateStartedAt
                );
                if (spinner) {
                    spinner.classList.remove('fa-spin', 'fa-sync');
                    spinner.classList.add('fa-times-circle', 'text-danger');
                }
                if (closeBtn) closeBtn.style.display = 'block';
                if (finishBtn) finishBtn.disabled = false;
                if(btn) { btn.innerHTML = origText; btn.disabled = false; }
            });
    }, 1000);
}

// Gemeinsame Update-Logik
function startSystemUpdate(btnId = null) {
    return startInstallerUpdate(btnId || 'btn-update-installer');
}

function pollUpdate(log, spinner, closeBtn, finishBtn) {
    let tick = 0;
    let stoppedPolls = 0;
    let transientPollErrors = 0;
    const maxStoppedGracePolls = 6;
    const maxTransientPollErrors = 8;
    const interval = setInterval(() => {
        tick++;
        fetch(e3dcActionUrl('action=run_update&mode=poll&t=' + Date.now()))
            .then(async r => {
                const text = await r.text();
                if (!r.ok) {
                    throw new Error('HTTP ' + r.status);
                }
                if (!text.trim()) {
                    throw new Error('Leere Update-Antwort');
                }
                try {
                    return JSON.parse(text);
                } catch (parseErr) {
                    throw new Error('Ungültige Update-Antwort: ' + parseErr.message);
                }
            })
            .then(data => {
                transientPollErrors = 0;
                if (typeof data.log === 'string') log.innerText = data.log;
                const modalBody = log.parentElement;
                modalBody.scrollTop = modalBody.scrollHeight;

                const logText = data.log || "";
                const releaseCompletionFound = /(?:^|\r?\n)\[OK\]\s+Update abgeschlossen\.\s*(?:\r?\n)+Version:\s*v?\d+\.\d+\.\d+[A-Za-z0-9._-]*/i.test(logText);
                const successFound = releaseCompletionFound ||
                                     logText.includes("Update erfolgreich abgeschlossen") ||
                                     logText.includes("Update abgeschlossen") ||
                                     logText.includes("Du bist auf dem neuesten Stand") ||
                                     logText.includes("Vorgang abgebrochen");
                const exitCode = Number.isInteger(data.exit_code) ? data.exit_code : null;
                const exitKnown = exitCode !== null;
                const exitOk = exitKnown && exitCode === 0;
                const exitFailed = exitKnown && exitCode !== 0;
                const errorFound = /(traceback|exception|critical|fatal|permission denied|web-update kann nicht starten|konnte prozess nicht starten|konnte update-prozess nicht starten)/i.test(logText);

                if (!data.running && !successFound && !exitKnown && tick < 360) {
                    stoppedPolls++;
                    if (stoppedPolls <= maxStoppedGracePolls) {
                        if (stoppedPolls === 1) {
                            log.innerText += "\n\n[INFO] Update-Prozess beendet, warte auf Abschlussstatus und letzte Logzeilen...";
                        }
                        return;
                    }
                } else {
                    stoppedPolls = 0;
                }

                if (!data.running || successFound || exitKnown || tick >= 360) {
                    clearInterval(interval);
                    setTimeout(() => {
                        spinner.classList.remove('fa-spin', 'fa-sync');
                        const ok = (successFound || exitOk || data.success === true) && !exitFailed && !errorFound;
                        if (ok) {
                            spinner.classList.add('fa-check-circle', 'text-success');
                            log.innerText += "\n\n✓ Update beendet. Bitte Seite neu laden.";
                            // Optional: Reload triggern oder Badges resetten
                            if(typeof checkInstallerUpdate === 'function') checkInstallerUpdate();
                        } else {
                            spinner.classList.add('fa-times-circle', 'text-danger');
                            if (tick >= 360) {
                                log.innerText += "\n\n✗ Update beendet: Zeitüberschreitung beim Warten auf den Abschluss.";
                            } else if (exitFailed) {
                                log.innerText += "\n\n✗ Update beendet mit Exitcode " + exitCode + ".";
                            } else {
                                log.innerText += "\n\n✗ Update beendet, aber ohne eindeutigen Abschlussstatus.";
                            }
                        }
                        closeBtn.style.display = 'block';
                        finishBtn.disabled = false;
                        finishBtn.innerText = ok ? "Neu laden" : "Schließen";
                        finishBtn.onclick = ok ? () => location.reload() : null;
                    }, 500);
                }
            })
            .catch(err => {
                transientPollErrors++;
                if (transientPollErrors <= maxTransientPollErrors && tick < 120) {
                    console.info("Update poll transient:", err);
                    if (log && transientPollErrors === 1) {
                        log.innerText += "\n\n[INFO] Update-Oberfläche kurz nicht erreichbar: " + err.message + "\nPolling läuft weiter...";
                    }
                    return;
                }
                clearInterval(interval);
                if (log) log.innerText += "\n\nPolling-Fehler: " + err.message;
                if (spinner) {
                    spinner.classList.remove('fa-spin', 'fa-sync');
                    spinner.classList.add('fa-times-circle', 'text-danger');
                }
                closeBtn.style.display = 'block';
                finishBtn.disabled = false;
            });
    }, 1000);
}

let releaseRollbackState = null;

function selectedReleaseRollback() {
    const select = document.getElementById('rollback-release-select');
    if (!releaseRollbackState || !select) return null;
    return (releaseRollbackState.releases || []).find(r => r.tag === select.value) || null;
}

function updateReleaseRollbackPreview() {
    const release = selectedReleaseRollback();
    const warning = document.getElementById('rollback-warning');
    const preview = document.getElementById('rollback-command-preview');
    const runLog = document.getElementById('rollback-run-log');
    const copyBtn = document.getElementById('rollback-copy-btn');
    if (!releaseRollbackState || !release || !warning || !preview || !copyBtn) return;

    if (runLog) {
        runLog.style.display = 'none';
        runLog.innerText = '';
    }

    if (releaseRollbackState.docker) {
        warning.className = 'alert alert-info py-2 small mb-3';
        warning.innerText = 'Docker-Rückfall wird bewusst nicht im Container ausgeführt. Bitte diese Befehle auf dem Docker-Host ausführen.';
        preview.innerText = release.docker_commands || '';
        copyBtn.style.display = 'inline-block';
    } else {
        warning.className = 'alert alert-warning py-2 small mb-3';
        warning.innerText = 'Bare-Metal-Rückfälle werden im Web nicht gestartet. Verwende die verifizierte Backup-Wiederherstellung über die administrative Konsole.';
        preview.innerText = release.bare_metal_summary || '';
        copyBtn.style.display = 'none';
    }
}

function openReleaseRollback() {
    const modalEl = document.getElementById('releaseRollbackModal');
    const select = document.getElementById('rollback-release-select');
    const warning = document.getElementById('rollback-warning');
    const preview = document.getElementById('rollback-command-preview');
    const currentBadge = document.getElementById('rollback-current-version');
    const stableBadge = document.getElementById('rollback-stable-version');
    const envBadge = document.getElementById('rollback-environment');
    if (!modalEl || !select || !warning || !preview) return;

    warning.className = 'alert alert-warning py-2 small mb-3';
    warning.innerText = 'Release-Liste wird geladen...';
    preview.innerText = 'Lade...';
    select.innerHTML = '';

    fetch('index.php?action=release_rollback_options&t=' + Date.now())
        .then(r => r.json())
        .then(data => {
            if (!data || !data.success) throw new Error((data && data.message) || 'Release-Liste konnte nicht gelesen werden');
            releaseRollbackState = data;
            if (currentBadge) currentBadge.innerText = 'Aktuell: ' + (data.current_version ? 'v' + String(data.current_version).replace(/^v/, '') : '--');
            if (stableBadge) stableBadge.innerText = 'Stable: ' + (data.stable_release || '--');
            if (envBadge) envBadge.innerText = data.docker ? 'Docker: Host-Befehle' : 'Bare Metal: Wiederherstellungshinweise';
            (data.releases || []).forEach(release => {
                const opt = document.createElement('option');
                opt.value = release.tag;
                opt.textContent = `${release.label || release.tag}${release.current ? ' (installiert)' : ''}${release.stable ? ' [Stable]' : ''}`;
                select.appendChild(opt);
            });
            if (!select.options.length) {
                if (envBadge && !data.docker) envBadge.innerText = 'Bare Metal: kein Programm-Rückfall';
                warning.innerText = data.empty_message || 'Keine validierte Rückfallversion hinterlegt.';
                preview.innerText = '';
                select.disabled = true;
                const copyBtn = document.getElementById('rollback-copy-btn');
                if (copyBtn) copyBtn.style.display = 'none';
                return;
            }
            select.disabled = false;
            select.onchange = updateReleaseRollbackPreview;
            updateReleaseRollbackPreview();
        })
        .catch(err => {
            warning.className = 'alert alert-danger py-2 small mb-3';
            warning.innerText = 'Fehler: ' + err.message;
            preview.innerText = '';
        });

    new bootstrap.Modal(modalEl).show();
}

function setReleaseRollbackCopyFeedback(message, success) {
    const button = document.getElementById('rollback-copy-btn');
    if (!button) return;
    if (!button.dataset.originalHtml) button.dataset.originalHtml = button.innerHTML;
    if (!button.dataset.originalClass) button.dataset.originalClass = button.className;
    if (button._copyFeedbackTimer) clearTimeout(button._copyFeedbackTimer);
    button.innerHTML = '<i class="fas ' + (success ? 'fa-check' : 'fa-exclamation-triangle') + ' me-2"></i>' + message;
    button.className = button.dataset.originalClass + (success ? ' text-success' : ' text-danger');
    button._copyFeedbackTimer = setTimeout(() => {
        button.innerHTML = button.dataset.originalHtml;
        button.className = button.dataset.originalClass;
    }, 2500);
}

async function copyReleaseRollbackCommands() {
    const preview = document.getElementById('rollback-command-preview');
    const text = preview ? preview.innerText.trim() : '';
    if (!text) {
        setReleaseRollbackCopyFeedback('Keine Befehle', false);
        return;
    }

    try {
        if (navigator.clipboard && navigator.clipboard.writeText) {
            await navigator.clipboard.writeText(text);
        } else {
            const helper = document.createElement('textarea');
            helper.value = text;
            helper.setAttribute('readonly', '');
            helper.style.position = 'fixed';
            helper.style.opacity = '0';
            document.body.appendChild(helper);
            let copied = false;
            try {
                helper.select();
                copied = typeof document.execCommand === 'function' && document.execCommand('copy');
            } finally {
                helper.remove();
            }
            if (!copied) throw new Error('Zwischenablage nicht verfügbar.');
        }
        setReleaseRollbackCopyFeedback('Kopiert', true);
    } catch (error) {
        setReleaseRollbackCopyFeedback('Kopieren fehlgeschlagen', false);
    }
}

function startReleaseRollback() {
    const release = selectedReleaseRollback();
    const log = document.getElementById('rollback-run-log');
    const preview = document.getElementById('rollback-command-preview');
    const runBtn = document.getElementById('rollback-run-btn');
    if (!release || !log || !runBtn || (releaseRollbackState && releaseRollbackState.docker)) return;

    const msg = `Rückfall auf ${release.tag} wirklich starten?\n\nDabei wird vorher ein Backup erstellt, danach werden Dienste gestoppt, der Release-Tag ausgecheckt und ein Gesundheitstest ausgeführt.`;
    if (!confirm(msg)) return;

    runBtn.disabled = true;
    if (preview) preview.style.display = 'none';
    log.style.display = 'block';
    log.innerText = 'Starte Release-Rückfall...\n';

    e3dcPostAction('action=run_release_rollback&mode=start&t=' + Date.now(), {
        confirm: '1',
        tag: release.tag
    })
        .then(r => e3dcParseJsonResponse(r, 'Release-Rückfall-Start'))
        .then(data => {
            if (data.status === 'started' || data.status === 'running') {
                pollReleaseRollback(log, runBtn);
            } else {
                log.innerText += '\nFehler: ' + (data.message || 'Unbekannter Fehler');
                runBtn.disabled = false;
            }
        })
        .catch(err => {
            log.innerText += '\nNetzwerkfehler: ' + err;
            runBtn.disabled = false;
        });
}

function pollReleaseRollback(log, runBtn) {
    const interval = setInterval(() => {
        fetch('index.php?action=run_release_rollback&mode=poll&t=' + Date.now())
            .then(r => r.json())
            .then(data => {
                if (typeof data.log === 'string') log.innerText = data.log;
                log.parentElement.scrollTop = log.parentElement.scrollHeight;
                const logText = data.log || '';
                const done = logText.includes('Release-Rückfall auf') || logText.includes('Update abgeschlossen');
                if (!data.running || done) {
                    clearInterval(interval);
                    log.innerText += '\n\nVorgang beendet.';
                    if (runBtn) {
                        runBtn.disabled = false;
                        runBtn.innerText = 'Erneut starten';
                    }
                }
            })
            .catch(() => {});
    }, 1000);
}

/**
 * DASHBOARD & CHART LOGIC (Moved from index.php)
 */

let themeSaveFeedbackTimer = null;

function showThemeSaveFeedback(message, success) {
    const status = document.getElementById('theme-save-status');
    if (!status) return;
    if (themeSaveFeedbackTimer) clearTimeout(themeSaveFeedbackTimer);
    status.textContent = message;
    status.className = 'small ms-1 ' + (success ? 'text-success' : 'text-danger');
    status.hidden = false;
    themeSaveFeedbackTimer = setTimeout(() => {
        status.textContent = '';
        status.hidden = true;
    }, 3000);
}

function applyDarkModeTheme(darkMode, icon = null) {
    const theme = darkMode ? 'dark' : 'light';
    const html = document.documentElement;
    const body = document.body;
    const targetIcon = icon || document.getElementById('darkmode-icon') || document.getElementById('mobile-darkmode-icon');

    html.setAttribute('data-bs-theme', theme);
    html.setAttribute('data-theme', theme);
    if (body) {
        body.setAttribute('data-bs-theme', theme);
        body.setAttribute('data-theme', theme);
    }
    if (targetIcon) {
        const tone = targetIcon.classList && targetIcon.classList.contains('text-secondary') ? 'text-secondary' : 'text-warning';
        targetIcon.className = (darkMode ? 'fas fa-sun ' : 'fas fa-moon ') + tone;
    }
}

function notifyThemeChanged() {
    window.dispatchEvent(new CustomEvent('themeChanged'));
    const body = document.body;
    if (body && body.classList.contains('mode-mobile')) {
        const page = window.E3DC_PAGE || '';
        if ((page === 'live' || page === 'hybrid') && typeof updateDiagram === 'function') updateDiagram();
        else if (page === 'forecast' && typeof updateForecast === 'function') updateForecast();
        else if (page === 'history' && typeof window.triggerHistoryUpdate === 'function') window.triggerHistoryUpdate();
    } else if (typeof refreshData === 'function') {
        refreshData(false);
    }
}

function toggleDarkMode(el) {
    if (typeof DARK_MODE === 'undefined') return;
    const previousDarkMode = DARK_MODE;
    DARK_MODE = !DARK_MODE;
    const icon = el || document.getElementById('darkmode-icon') || document.getElementById('mobile-darkmode-icon');
    applyDarkModeTheme(DARK_MODE, icon);
    try { localStorage.setItem('theme', DARK_MODE ? 'dark' : 'light'); } catch (e) {}

    const saveRequest = e3dcPostAction('', {
        action: 'save_setting',
        key: 'darkmode',
        value: DARK_MODE ? '1' : '0'
    })
        .then(async response => {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const result = await response.text();
            if (result.trim() !== 'ok') throw new Error('Einstellung wurde nicht bestätigt.');
            showThemeSaveFeedback('Gespeichert', true);
        })
        .catch(() => {
            DARK_MODE = previousDarkMode;
            applyDarkModeTheme(DARK_MODE, icon);
            try { localStorage.setItem('theme', DARK_MODE ? 'dark' : 'light'); } catch (e) {}
            showThemeSaveFeedback('Nicht gespeichert – zurückgesetzt', false);
            notifyThemeChanged();
        });

    void saveRequest;
    setTimeout(notifyThemeChanged, 100);
}

function toggleForecast(el) {
    if (typeof SHOW_FORECAST === 'undefined') return;
    const previousShowForecast = SHOW_FORECAST;
    SHOW_FORECAST = !SHOW_FORECAST;

    const applyForecastIcon = () => {
        if (!el || !el.classList) return;
        el.classList.toggle('fa-eye', SHOW_FORECAST);
        el.classList.toggle('fa-eye-slash', !SHOW_FORECAST);
    };
    applyForecastIcon();

    const saveRequest = e3dcPostAction('', {
        action: 'save_setting',
        key: 'show_forecast',
        value: SHOW_FORECAST ? '1' : '0'
    })
        .then(async response => {
            if (!response.ok) throw new Error('HTTP ' + response.status);
            const result = await response.text();
            if (result.trim() !== 'ok') throw new Error('Einstellung wurde nicht bestätigt.');
            showThemeSaveFeedback('Prognose gespeichert', true);
            if (typeof fetchData === 'function') fetchData();
        })
        .catch(() => {
            SHOW_FORECAST = previousShowForecast;
            applyForecastIcon();
            showThemeSaveFeedback('Prognose nicht gespeichert – zurückgesetzt', false);
            if (typeof fetchData === 'function') fetchData();
        });

    void saveRequest;
}

function switchChartMode(mode, view = 'normal') {
    if (view !== 'normal' && (mode === 'hybrid' || mode === 'forecast')) {
        mode = 'live';
    }

    // Den bisherigen Aktualisierungstakt sofort beenden. Antworten älterer
    // Diagramm-Anfragen dürfen den neu gewählten Modus nicht überschreiben.
    activateJsChartMode(mode);

    // Setze chart-mode Flag auf dem Body element für CSS Scoping (z.B. header-regler-plan)
    document.body.setAttribute('data-chart-mode', mode);

    const select = document.getElementById('chart-mode-select');
    if (select) {
        select.value = mode;
        Array.from(select.options).forEach(opt => {
            if (opt.value === 'hybrid' || opt.value === 'forecast') {
                opt.style.display = (view === 'normal') ? '' : 'none';
                opt.hidden = (view !== 'normal');
                opt.disabled = (view !== 'normal');
            }
        });
        select.style.display = (mode === 'flow') ? 'none' : 'inline-block';
    }

    const flipBtn = document.querySelector('.card-header .btn-chart-flip');
    if (flipBtn) flipBtn.style.display = (mode === 'flow') ? 'none' : 'inline-block';

    const refreshBtn = document.getElementById('main-refresh-btn');
    if (refreshBtn) refreshBtn.style.display = (mode === 'flow') ? 'none' : 'inline-block';

    if (typeof CURRENT_VIEW !== 'undefined') CURRENT_VIEW = view;

    // --- NEU: Statistik-Overlay schließen, wenn ein anderes Diagramm aufgerufen wird ---
    if (typeof statsViewActive !== 'undefined' && statsViewActive) {
        statsViewActive = false;
        const statsElDesktop = document.getElementById('stats-view');
        if (statsElDesktop) statsElDesktop.style.display = 'none';

        const statsElMobile = document.getElementById('m-stats-view');
        if (statsElMobile) statsElMobile.style.display = 'none';
        const chevron = document.getElementById('m-stats-chevron');
        if (chevron) chevron.className = 'fas fa-chevron-down text-muted';
    }

    // Reset History Dropdown
    if (mode === 'live') {
        $('#history-select-normal').val('');
        $('#history-select-wp').val('');
    }

    const flowView = document.getElementById('flow-view');
    const title = document.getElementById('chart-title');
    const liveControls = document.getElementById('live-controls');
    const archiveSelect = document.getElementById('archive-select');
    const jsContainer = document.getElementById('liveChartContainer');
    if (mode !== 'forecast' && typeof renderDirectMarketingForecastChart === 'function') {
        renderDirectMarketingForecastChart({direct_marketing_enabled: false});
    }

    // Highlight aktiver Button in den Schnellzugriffen
    document.querySelectorAll('.quick-action-btn').forEach(btn => {
        if (btn.dataset.mode === mode) {
            btn.classList.remove('btn-outline-secondary');
            btn.classList.add('btn-info', 'text-dark');
        } else {
            btn.classList.remove('btn-info', 'text-dark');
            btn.classList.add('btn-outline-secondary');
        }
    });

    if(flowView) flowView.style.display = 'none';

    // Forecast-Tagessummen-Leiste: nur im Prognose-Modus anzeigen
    const forecastSummaryBar = document.getElementById('forecast-kwh-summary');
    if (forecastSummaryBar) {
        if (mode === 'forecast') {
            // wird von loadJsForecastChart befüllt – jetzt einfach sichtbar lassen
        } else {
            forecastSummaryBar.style.display = 'none';
            forecastSummaryBar.innerHTML = '';
        }
    }
    const forecastDiagnosticCard = document.getElementById('pv-forecast-diagnostic-card');
    if (forecastDiagnosticCard && mode !== 'forecast' && mode !== 'hybrid') {
        forecastDiagnosticCard.hidden = true;
    }

    if (mode === 'flow') {
        if(title) title.innerHTML = '<i class="fas fa-project-diagram me-2 text-info"></i>Live Energiefluss';
        if(liveControls) liveControls.style.display = 'none';
        if(archiveSelect) archiveSelect.style.display = 'none';
        if(jsContainer) jsContainer.style.display = 'none';
        if(flowView) flowView.style.display = 'flex';
        if(typeof fetchData === 'function') fetchData();
    } else if (mode === 'price') {

        if(title) title.innerHTML = '<i class="fas fa-euro-sign me-2 text-primary"></i>Strompreis & Kosten';
        if(liveControls) liveControls.style.display = 'flex';

        document.getElementById('history-select-normal').style.display = 'inline-block';
        document.getElementById('history-select-wp').style.display = 'none';
        if(archiveSelect) archiveSelect.style.display = 'none';

        if (jsContainer) {
            jsContainer.style.display = 'block';
            let hours = 24;
            const histNorm = document.getElementById('history-select-normal');
            const file = histNorm ? histNorm.value : null;
            loadJsPriceChart(hours, file);
        }
    } else if (mode === 'forecast') {
        if(title) title.innerHTML = '<i class="fas fa-chart-line me-2 text-secondary"></i>SoC Prognose';
        if(liveControls) liveControls.style.display = 'none';
        if(archiveSelect) archiveSelect.style.display = 'none';

        if (jsContainer) {
            jsContainer.style.display = 'block';
            if (typeof loadJsForecastChart === 'function') loadJsForecastChart('');
        }
    } else if (mode === 'hybrid') {
        if(title) title.innerHTML = '<i class="fas fa-chart-pie me-2 text-secondary"></i>Hybrid (Verlauf + Prognose)';
        if(liveControls) liveControls.style.display = 'flex';

        document.getElementById('history-select-normal').style.display = 'inline-block';
        document.getElementById('history-select-wp').style.display = 'none';
        if(archiveSelect) archiveSelect.style.display = 'none';

        if (jsContainer) {
            jsContainer.style.display = 'block';
            let hours = 6;
            const activeBtn = document.querySelector('#live-controls .btn.active');
            if (activeBtn) hours = parseInt(activeBtn.innerText);

            const histNorm = document.getElementById('history-select-normal');
            const file = histNorm ? histNorm.value : null;
            loadJsHybridChart(hours, file);
        }
    } else if (mode === 'live') {
        let titleText = 'Leistungsverlauf';
        if (view === 'pv') titleText = 'PV Strings';
        if (view === 'grid') titleText = 'Netz & WR Phasen';
        if (view === 'bat') titleText = 'Batterie Details';
        if (view === 'wb') titleText = 'Wallbox 1 Phasen';
        if (view === 'wb2') titleText = 'Wallbox 2 Phasen';
        if (view === 'hs') titleText = 'Heizstab Details';
        if (view === 'wp') titleText = 'Wärmepumpe Details';
        if (view === 'climate') titleText = 'Klima Verlauf';

        if(title) title.innerHTML = '<i class="fas fa-chart-area me-2 text-secondary"></i>' + titleText;
        if(liveControls) liveControls.style.display = 'flex';

        // Toggle correct dropdown
        if (view === 'wp') {
            document.getElementById('history-select-normal').style.display = 'none';
            document.getElementById('history-select-wp').style.display = 'inline-block';
        } else {
            document.getElementById('history-select-normal').style.display = 'inline-block';
            document.getElementById('history-select-wp').style.display = 'none';
        }

        if(archiveSelect) archiveSelect.style.display = 'none';

        if (jsContainer) {
            jsContainer.style.display = 'block';
        }

        let hours = 6;
        const activeBtn = document.querySelector('#live-controls .btn.active');
        if (activeBtn) hours = parseInt(activeBtn.innerText);
        updateChart(hours, null);
        if(typeof fetchData === 'function') fetchData();
    }
}

function updateChart(hours, btn) {
    if(btn) {
        $('.btn-group-custom .btn').removeClass('active');
        $(btn).addClass('active');
        $('#live-controls select').val('');
    }

    const jsContainer = document.getElementById('liveChartContainer');

    const histSelect = document.getElementById('history-select-normal') || document.getElementById('mobileHistorySelect');
    const isLive = !histSelect || histSelect.value === "";
    const modeSelect = document.getElementById('chart-mode-select');
    const modeValue = modeSelect ? modeSelect.value : '';

    if (isLive && jsContainer) {
        jsContainer.style.display = 'block';
        if (modeValue === 'hybrid') {
            loadJsHybridChart(hours);
        } else if (modeValue === 'price') {
            loadJsPriceChart(hours);
        } else if (modeValue === 'forecast') {
            if (typeof loadJsForecastChart === 'function') loadJsForecastChart('');
        } else {
            loadJsLiveChart(hours);
        }
        return;
    }

}

function updateChartHistory(file) {
    if (!file) {
        updateChart(6, document.querySelector('.btn-group-custom .btn:first-child'));
        return;
    }

    $('.btn-group-custom .btn').removeClass('active');

    const jsContainer = document.getElementById('liveChartContainer');

    const modeSelect = document.getElementById('chart-mode-select');
    const modeValue = modeSelect ? modeSelect.value : '';

    if (jsContainer) {
        jsContainer.style.display = 'block';
        if (modeValue === 'hybrid') {
            loadJsHybridChart(24, file);
        } else if (modeValue === 'price') {
            loadJsPriceChart(24, file);
        } else if (modeValue === 'forecast') {
            if (typeof loadJsForecastChart === 'function') loadJsForecastChart(file);
        } else {
            loadJsLiveChart(24, file);
        }
        return;
    }
}

function loadArchive(file) {
    const jsContainer = document.getElementById('liveChartContainer');
    if (jsContainer) {
        jsContainer.style.display = 'block';
        if (typeof loadJsForecastChart === 'function') loadJsForecastChart(file);
    }
}

function refreshData(isAuto = false) {
    if(typeof fetchData === 'function') fetchData();

    const btn = document.getElementById('main-refresh-btn');
    if (btn && !btn.dataset.originalHtml) {
        btn.dataset.originalHtml = btn.innerHTML;
    }
    const originalHtml = btn ? btn.dataset.originalHtml : '';

    if(btn) {
        btn.disabled = true;
        btn.innerHTML = '<i class="fas fa-spinner fa-spin"></i>';
    }

    const modeSelect = document.getElementById('chart-mode-select');
    const modeValue = modeSelect ? modeSelect.value : '';
    const isLive = modeValue === 'live' || modeValue === 'hybrid';
    const isForecast = modeValue === 'forecast';
    const isPrice = modeValue === 'price';
    const jsContainer = document.getElementById('liveChartContainer');
    if (jsContainer && jsContainer.style.display === 'block') {
        if (isLive) {
            let hours = 6;
            const activeBtn = document.querySelector('#live-controls .btn.active');
            if (activeBtn) hours = parseInt(activeBtn.innerText);
            const histNorm = document.getElementById('history-select-normal') || document.getElementById('mobileHistorySelect');
            const file = histNorm ? histNorm.value : null;
            if (modeValue === 'hybrid') loadJsHybridChart(hours, file);
            else loadJsLiveChart(hours, file);
        } else if (isForecast) {
            if (typeof loadJsForecastChart === 'function') loadJsForecastChart('');
        } else if (isPrice) {
            const histNorm = document.getElementById('history-select-normal') || document.getElementById('mobileHistorySelect');
            const file = histNorm ? histNorm.value : null;
            if (typeof loadJsPriceChart === 'function') loadJsPriceChart(currentLiveHours, file);
        }
    }

    if(btn) {
        setTimeout(() => { btn.disabled = false; btn.innerHTML = originalHtml; }, 300);
    }
}

// --- DIAGRAMM EINSTELLUNGEN SPEICHERN (LOCALSTORAGE) ---
function getHiddenDatasets() {
    try { return JSON.parse(localStorage.getItem('e3dc_hidden_datasets')) || {}; } catch(e) { return {}; }
}
function saveHiddenDataset(label, isHidden) {
    let hidden = getHiddenDatasets();
    hidden[label] = isHidden;
    localStorage.setItem('e3dc_hidden_datasets', JSON.stringify(hidden));
}
function applyHiddenState(datasets) {
    let hidden = getHiddenDatasets();
    datasets.forEach(ds => {
        if (ds.e3dcAlwaysVisible) {
            ds.hidden = false;
            return;
        }
        if (hidden[ds.label] !== undefined) {
            ds.hidden = hidden[ds.label];
        }
    });
    return datasets;
}

function pushStorageTargetCurveDataset(datasets, targetCurve, segment = null) {
    if (!targetCurve || !Array.isArray(targetCurve) || !targetCurve.some(v => v !== null && v !== undefined)) {
        return;
    }
    const dataset = {
        label: 'Ladekurve (%)',
        data: targetCurve,
        borderColor: '#38bdf8',
        borderDash: [6, 4],
        fill: false,
        tension: 0.35,
        cubicInterpolationMode: 'monotone',
        pointRadius: 0,
        borderWidth: 2,
        yAxisID: 'y1',
        spanGaps: false,
        order: 4,
        e3dcAlwaysVisible: true
    };
    if (segment) dataset.segment = segment;
    datasets.push(dataset);
}

function pushMarketChargeDataset(datasets, marketCharge, segment = null) {
    if (!marketCharge || !Array.isArray(marketCharge) || !marketCharge.some(v => Number(v) > 0)) {
        return;
    }
    const dataset = {
        label: 'Markt-Netzladen',
        data: marketCharge,
        backgroundColor: 'rgba(37, 99, 235, 0.24)',
        borderColor: '#2563eb',
        type: 'bar',
        borderWidth: 1,
        yAxisID: 'y',
        order: 1,
        e3dcAlwaysVisible: true
    };
    if (segment) dataset.segment = segment;
    datasets.push(dataset);
}

function pushPredumpHeadroomDataset(datasets, predumpW, segment = null) {
    if (!predumpW || !Array.isArray(predumpW) || !predumpW.some(v => Number(v) > 0)) {
        return;
    }
    const dataset = {
        label: 'Speicherplatz schaffen (aktuell freigegeben)',
        data: predumpW.map(v => Math.max(0, Number(v) || 0)),
        backgroundColor: 'rgba(245, 158, 11, 0.32)',
        borderColor: '#f59e0b',
        type: 'bar',
        borderWidth: 1,
        yAxisID: 'y',
        order: 2,
        e3dcAlwaysVisible: true
    };
    if (segment) dataset.segment = segment;
    datasets.push(dataset);
}

function pushPredumpCandidateDataset(datasets, candidateW, segment = null) {
    if (!candidateW || !Array.isArray(candidateW) || !candidateW.some(v => Number(v) > 0)) {
        return;
    }
    const dataset = {
        label: 'Predump-Kandidat (Simulation)',
        data: candidateW.map(v => Math.max(0, Number(v) || 0)),
        backgroundColor: 'rgba(148, 163, 184, 0.18)',
        borderColor: '#94a3b8',
        borderDash: [4, 4],
        type: 'bar',
        borderWidth: 1,
        yAxisID: 'y',
        order: 3,
        hidden: true
    };
    if (segment) dataset.segment = segment;
    datasets.push(dataset);
}

function updateForecastProjectionStatus(data, directMarketingView = null) {
    const container = document.getElementById('liveChartContainer');
    if (!container) return;
    let status = container.querySelector('[data-forecast-projection-status]');
    if (!status) {
        status = document.createElement('div');
        status.dataset.forecastProjectionStatus = '1';
        status.style.cssText = 'position:absolute;left:10px;right:10px;bottom:6px;z-index:3;padding:4px 8px;border-radius:6px;font-size:.78rem;text-align:center;pointer-events:none;';
        container.appendChild(status);
    }
    const standardForecastAvailable = data
        && !data.error
        && Array.isArray(data.labels)
        && data.labels.length > 0;
    if (!standardForecastAvailable) {
        status.textContent = 'Ladekurve derzeit nicht verfügbar: Prognosedaten fehlen oder sind ungültig.';
        status.style.background = 'rgba(220,53,69,.18)';
        status.style.color = '#dc3545';
        status.hidden = false;
        return;
    }
    const dvView = directMarketingView && typeof directMarketingView === 'object'
        ? directMarketingView
        : null;
    if (dvView && dvView.active === true && dvView.state === 'complete') {
        status.hidden = true;
        status.textContent = '';
        return;
    }
    if (dvView && dvView.active === true && dvView.state !== 'complete') {
        const dvReasons = {
            HEADROOM_PROJECTION_POLICY_BINDING_INVALID: 'DV-Projektionsbindung ist nicht konsistent',
            HEADROOM_PROJECTION_PLAN_INVALID: 'DV-Headroom-Projektion ist ungültig',
            DIRECT_MARKETING_TRAJECTORY_INCOMPLETE: 'DV-Trajektorie ist unvollständig',
            DIRECT_MARKETING_TRAJECTORY_MISSING: 'DV-Trajektorie fehlt'
        };
        const dvReason = dvReasons[dvView.reasonCode]
            || String(dvView.reasonCode || 'DV-Trajektorie ist unvollständig');
        status.textContent = dvView.state === 'actions_only'
            ? `Standard-Prognose sichtbar; der DV-SoC-Verlauf ist unvollständig (${dvReason}). Ausgewählte DV-Aktionen ersetzen die Standardkurve nicht.`
            : `Standard-Prognose sichtbar; der DV-Fahrplan ist unvollständig (${dvReason}).`;
        status.style.background = 'rgba(245,158,11,.18)';
        status.style.color = '#f59e0b';
        status.hidden = false;
        return;
    }
    const contract = data && typeof data.storage_projection_status === 'object'
        ? data.storage_projection_status
        : null;
    if (!contract) {
        status.hidden = true;
        status.textContent = '';
        return;
    }
    if (contract.status === 'coherent' && contract.plan_fresh !== false) {
        status.hidden = true;
        status.textContent = '';
        return;
    }
    const reasons = {
        PLAN_INVALID: 'kanonischer Plan ungültig',
        PLAN_CHANGED_DURING_READ: 'Planrevision wechselte während des Abrufs',
        RUNTIME_INVALID: 'aktuelle Runtime ungültig',
        RUNTIME_PLAN_MISMATCH: 'Runtime gehört zu einer anderen Planrevision',
        RUNTIME_SLOT_MISMATCH: 'aktueller Runtime-Slot fehlt im Plan',
        SNAPSHOT_MISSING: 'Plan-/Runtime-Snapshot fehlt'
    };
    const reason = reasons[contract.reason_code] || String(contract.reason_code || 'unbekannter Status');
    if (contract.status === 'plan_only' && contract.plan_fresh !== false) {
        status.textContent = `Planung sichtbar, aktuelle Freigabe nicht zugeordnet: ${reason}.`;
        status.style.background = 'rgba(245,158,11,.18)';
        status.style.color = '#f59e0b';
    } else {
        const unavailableReason = contract.plan_fresh === false ? 'Planrevision ist abgelaufen' : reason;
        status.textContent = `Ladekurve derzeit nicht als aktuell verfügbar: ${unavailableReason}.`;
        status.style.background = 'rgba(220,53,69,.18)';
        status.style.color = '#dc3545';
    }
    status.hidden = false;
}

function clearForecastProjectionStatus() {
    const container = document.getElementById('liveChartContainer');
    const status = container
        ? container.querySelector('[data-forecast-projection-status]')
        : null;
    if (!status) return;
    status.hidden = true;
    status.textContent = '';
}

function updatePvForecastDiagnostics(data) {
    const card = document.getElementById('pv-forecast-diagnostic-card');
    const box = document.getElementById('pvForecastDiagnosticBox');
    if (!card && !box) return;

    const diagnostic = data && typeof data.pv_forecast_diagnostics === 'object' && data.pv_forecast_diagnostics !== null
        ? data.pv_forecast_diagnostics
        : (data && typeof data.metrics === 'object' ? data : null);
    const status = diagnostic ? String(diagnostic.status || '') : '';
    if (!diagnostic || status === 'aus') {
        if (card) card.hidden = true;
        if (box) box.hidden = true;
        return;
    }

    if (box) box.hidden = false;
    if (card) card.hidden = false;
    const available = diagnostic.available === true || ['diagnostisch', 'vorläufig', 'belastbar'].includes(status.toLowerCase());
    const provisional = diagnostic.provisional === true || status.toLowerCase() === 'vorläufig';
    const statusElement = document.getElementById('pv-forecast-diagnostic-status');
    if (statusElement) {
        statusElement.textContent = available
            ? (provisional ? 'Lernphase' : 'Diagnostisch')
            : 'Noch keine Auswertung';
        statusElement.className = available
            ? `badge ${provisional ? 'text-bg-warning' : 'text-bg-success'}`
            : 'badge text-bg-secondary';
    }

    const metrics = diagnostic.metrics && typeof diagnostic.metrics === 'object'
        ? diagnostic.metrics
        : {};
    const finiteMetric = key => {
        if (metrics[key] === null || metrics[key] === undefined || metrics[key] === '') {
            return null;
        }
        const value = Number(metrics[key]);
        return Number.isFinite(value) ? value : null;
    };
    const setMetric = (id, value, unit, signed = false) => {
        const element = document.getElementById(id);
        if (!element) return;
        if (value === null) {
            element.textContent = '–';
            return;
        }
        const prefix = signed && value > 0 ? '+' : '';
        element.textContent = `${prefix}${value.toLocaleString('de-DE', {
            minimumFractionDigits: 1,
            maximumFractionDigits: 1
        })}${unit}`;
    };
    setMetric(
        'pv-forecast-diagnostic-hit',
        finiteMetric('trefferabweichung_wh'),
        ' Wh/15 min'
    );
    setMetric(
        'pv-forecast-diagnostic-direction',
        finiteMetric('richtungsversatz_wh'),
        ' Wh/15 min',
        true
    );
    setMetric(
        'pv-forecast-diagnostic-energy',
        finiteMetric('energiegewichtete_gesamtabweichung_pct'),
        ' %'
    );
    setMetric(
        'pv-forecast-diagnostic-coverage',
        finiteMetric('vergleichsabdeckung_pct'),
        ' %'
    );

    const comparedSlots = Math.max(0, Number(diagnostic.compared_slots) || 0);
    const relevantDays = Math.max(0, Number(diagnostic.yield_relevant_days) || 0);
    const sampleElement = document.getElementById('pv-forecast-diagnostic-sample');
    if (sampleElement) {
        sampleElement.textContent = comparedSlots > 0
            ? `${comparedSlots.toLocaleString('de-DE')} verglichene 15-Minuten-Fenster · ${relevantDays.toLocaleString('de-DE')} Ertragstage`
            : 'Noch keine vergleichbaren Fenster';
    }

    const valueContract = diagnostic.forecast_value_contract
        && typeof diagnostic.forecast_value_contract === 'object'
        ? diagnostic.forecast_value_contract
        : {};
    const sourceDiagnostics = Array.isArray(diagnostic.source_diagnostics)
        ? diagnostic.source_diagnostics
        : [];
    const externalAcEvidence = sourceDiagnostics.find(item =>
        item && item.signal === 'pv_external_ac'
    );
    const observationQuality = diagnostic.observation_quality
        && typeof diagnostic.observation_quality === 'object'
        ? diagnostic.observation_quality
        : {};
    const contractElement = document.getElementById('pv-forecast-diagnostic-contract');
    if (contractElement) {
        const isPointForecast = valueContract.distribution_type === 'deterministic_point';
        const p50Proven = valueContract.p50_claim === 'proven';
        const externalAcMissing = externalAcEvidence
            && externalAcEvidence.status === 'EVIDENCE_LIMIT';
        const parts = [
            isPointForecast ? 'Punktprognose' : 'Prognosevertrag nicht belegt',
            p50Proven ? 'P50 bestätigt' : 'kein belegtes P50'
        ];
        if (externalAcMissing) {
            parts.push('Zusatz-WR ohne getrennte Ist-Kalibrierung');
        }
        if (
            observationQuality.curtailment_exclusion_status === 'EVIDENCE_LIMIT'
            || observationQuality.inverter_clipping_exclusion_status === 'EVIDENCE_LIMIT'
        ) {
            parts.push('Abregel-/Clippingzeiten noch nicht ausfilterbar');
        }
        contractElement.textContent = parts.join(' · ') + '.';
    }

    const horizonElement = document.getElementById('pv-forecast-diagnostic-horizons');
    if (horizonElement) {
        const buckets = Array.isArray(diagnostic.lead_time_buckets)
            ? diagnostic.lead_time_buckets
            : [];
        const comparedBuckets = buckets.filter(bucket =>
            bucket && Number(bucket.compared_slots) > 0
        );
        if (comparedBuckets.length === 0) {
            horizonElement.textContent = 'Erfassungs-Vorlauf: noch keine revisionsgebundenen Stichproben.';
        } else {
            const horizonParts = comparedBuckets.map(bucket => {
                const bucketMetrics = bucket.metrics && typeof bucket.metrics === 'object'
                    ? bucket.metrics
                    : {};
                const bucketNumber = key => {
                    const raw = bucketMetrics[key];
                    if (raw === null || raw === undefined || raw === '') return null;
                    const parsed = Number(raw);
                    return Number.isFinite(parsed) ? parsed : null;
                };
                const wape = bucketNumber('energiegewichtete_gesamtabweichung_pct');
                const bias = bucketNumber('richtungsversatz_wh');
                const count = Math.max(0, Number(bucket.compared_slots) || 0);
                const values = [
                    `${String(bucket.label || bucket.bucket_id || 'Vorlauf')}: ${count.toLocaleString('de-DE')} Fenster`
                ];
                if (wape !== null) {
                    values.push(`WAPE ${wape.toLocaleString('de-DE', { maximumFractionDigits: 1 })} %`);
                }
                if (bias !== null) {
                    const prefix = bias > 0 ? '+' : '';
                    values.push(`Bias ${prefix}${bias.toLocaleString('de-DE', { maximumFractionDigits: 1 })} Wh`);
                }
                if (bucket.provisional === true) {
                    values.push('vorläufig');
                }
                return values.join(', ');
            });
            horizonElement.textContent = `Erfassungs-Vorlauf: ${horizonParts.join(' · ')}`;
        }
    }
}

function normalizeElectricityPriceSeries(price) {
    if (!Array.isArray(price)) {
        return [];
    }
    return price.map(v => {
        if (v === null || v === undefined || v === '') {
            return null;
        }
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
    });
}

function e3dcFiniteNumberOrNull(value) {
    if (value === null || value === undefined || value === '') return null;
    const n = Number(value);
    return Number.isFinite(n) ? n : null;
}

function chartSeriesHasFiniteValue(values) {
    return Array.isArray(values) && values.some(value => e3dcFiniteNumberOrNull(value) !== null);
}

function chartSeriesHasMagnitude(values, minAbs = 20) {
    return Array.isArray(values) && values.some(value => {
        const n = e3dcFiniteNumberOrNull(value);
        return n !== null && Math.abs(n) > minAbs;
    });
}

function liveGridPhaseValues(data) {
    if (!data) return null;
    const values = [
        e3dcFiniteNumberOrNull(data.grid_p1),
        e3dcFiniteNumberOrNull(data.grid_p2),
        e3dcFiniteNumberOrNull(data.grid_p3)
    ];
    return values.every(value => value !== null) ? values : null;
}

function liveGridPhaseCompactText(data) {
    const values = liveGridPhaseValues(data);
    return values ? `${Math.round(values[0])} | ${Math.round(values[1])} | ${Math.round(values[2])} W` : '';
}

function liveGridPhaseLabeledText(data, separator = ' | ') {
    const values = liveGridPhaseValues(data);
    return values ? `L1: ${Math.round(values[0])}W${separator}L2: ${Math.round(values[1])}W${separator}L3: ${Math.round(values[2])}W` : '';
}

function livePvSourceInfo(data) {
    data = data || {};
    const monitor = getDirectMarketingMonitor(data) || {};
    const totalRaw = e3dcFiniteNumberOrNull(data.pv_total_w ?? data.pv);
    const hasExternalPayloadValue = Object.prototype.hasOwnProperty.call(data, 'pv_external_w')
        && data.pv_external_w !== null
        && data.pv_external_w !== '';
    const externalPayloadRaw = hasExternalPayloadValue
        ? e3dcFiniteNumberOrNull(data.pv_external_w)
        : null;
    const externalRaw = externalPayloadRaw !== null
        ? externalPayloadRaw
        : e3dcFiniteNumberOrNull(monitor.pv_external_ac_w);
    const e3dcReportedRaw = Object.prototype.hasOwnProperty.call(data, 'pv_e3dc_w')
        ? e3dcFiniteNumberOrNull(data.pv_e3dc_w)
        : null;
    const hasMeasurementValidity = Object.prototype.hasOwnProperty.call(data, 'pv_external_power_valid');
    const measurementValid = data.pv_external_power_valid === true
        || (!hasMeasurementValidity && externalRaw !== null);
    const measurementSource = String(data.pv_external_source || '').trim().toLowerCase();
    const topologyPresent = data.pv_external_topology_present === true || data.pv_external_capable === true;
    const topologyValid = data.pv_external_topology_valid === true;
    const total = totalRaw !== null ? Math.max(0, totalRaw) : 0;
    const external = measurementValid && externalRaw !== null
        ? Math.max(0, Math.min(externalRaw, total))
        : 0;
    const legacyBalanceToleranceW = totalRaw !== null
        ? Math.max(75, Math.abs(totalRaw) * 0.05)
        : 75;
    const legacyExternalEvidence = !hasMeasurementValidity
        && hasExternalPayloadValue
        && externalPayloadRaw !== null
        && externalPayloadRaw > 20
        && totalRaw !== null
        && totalRaw >= 0
        && e3dcReportedRaw !== null
        && e3dcReportedRaw >= 0
        && totalRaw - e3dcReportedRaw > 20
        && Math.abs(totalRaw - (e3dcReportedRaw + externalPayloadRaw)) <= legacyBalanceToleranceW;
    // Eine Quelle, eine Bilanz: der E3/DC-Anteil ist stets der Rest zum bereits
    // einmal enthaltenen externen Anteil und wird nicht nochmals addiert.
    const e3dc = Math.max(0, total - external);
    const guardRaw = e3dcFiniteNumberOrNull(data.pv_external_charge_guard_w ?? monitor.pv_store_external_ac_guard_w);
    const guard = guardRaw !== null ? Math.max(0, guardRaw) : 0;
    const dcOnly = data.pv_dc_only_configured === true || monitor.pv_store_dc_only === true;
    const dcOnlyActive = data.pv_dc_only_active === true;
    const locked = dcOnlyActive && (data.pv_external_charge_locked === true || external > guard);
    return {
        total,
        e3dc,
        external,
        measurementValid,
        measurementValidityReported: hasMeasurementValidity,
        measurementSource,
        legacyExternalEvidence,
        topologyPresent,
        topologyValid,
        topologySource: String(data.pv_external_topology_source || 'none'),
        topologyEvidenceState: String(data.pv_external_topology_evidence_state || 'unknown'),
        dcOnly,
        dcOnlyActive,
        locked,
        guard
    };
}

function livePvSourceSplitText(data, separator = ' | ', compact = false) {
    const pv = livePvSourceInfo(data);
    if (!(pv.external > 20 || (pv.locked && pv.dcOnlyActive))) return '';
    const e3dcLabel = compact && !getEnergyFlowLabels().pv ? 'E3DC' : getFlowLabel('pv');
    const externalLabel = getFlowLabel('external_pv');
    const parts = [
        `${e3dcLabel} ${flowPlainWatts(pv.e3dc)}`,
        `${externalLabel} ${pv.measurementValid ? flowPlainWatts(pv.external) : '--'}${pv.locked ? ' gesperrt' : ''}`
    ];
    return parts.join(separator);
}

function livePvSourceSplitHtml(data, separator = ' | ', compact = false) {
    const pv = livePvSourceInfo(data);
    if (!(pv.external > 20 || (pv.locked && pv.dcOnlyActive))) return '';
    const e3dcLabel = compact && !getEnergyFlowLabels().pv ? 'E3DC' : getFlowLabel('pv');
    const externalLabel = getFlowLabel('external_pv');
    const lock = pv.locked
        ? ' <i class="fas fa-lock text-warning" title="Nur E3DC-DC-PV laden: Zusatz-WR ist für Akkuladung gesperrt"></i>'
        : '';
    return `${e3dcLabel} ${flowPlainWatts(pv.e3dc)}${separator}${externalLabel} ${pv.measurementValid ? flowPlainWatts(pv.external) : '--'}${lock}`;
}

function externalPvTopologyVisual(data, nodeState = {}) {
    const pv = livePvSourceInfo(data);
    const configured = nodeState.configured === true;
    const controlAvailable = nodeState.controlAvailable === true;
    const positiveLiveEvidence = (
        pv.measurementValidityReported
        && pv.measurementValid
        && pv.external > 20
        && pv.measurementSource === 'e3dc_add_power'
    ) || pv.legacyExternalEvidence;
    return {
        visible: pv.topologyPresent || configured || positiveLiveEvidence,
        measurementValid: pv.measurementValid,
        valueText: pv.measurementValid ? flowPlainWatts(pv.external) : '--',
        controlAvailable,
        positiveLiveEvidence,
        topologySource: pv.legacyExternalEvidence ? 'legacy_power_balance' : pv.topologySource,
        evidenceState: pv.legacyExternalEvidence ? 'compatible_payload' : pv.topologyEvidenceState
    };
}

function livePvBreakdownHtml(data) {
    const parts = [];
    const split = livePvSourceSplitHtml(data);
    if (split) parts.push(split);
    if (data && data.dc0_w !== undefined) {
        parts.push(`String 1: ${data.dc0_w}W`);
        parts.push(`String 2: ${data.dc1_w}W`);
    }
    return parts.join(' | ');
}

function updateEnergyFlowPvSplit(data, container = document.getElementById('flow-view')) {
    const detail = document.getElementById('f-val-pv-split');
    if (!detail) return;
    const layout = container && container.dataset ? String(container.dataset.flowLayout || '') : '';
    const isMobileFlow = layout === 'mobile';
    const visual = externalPvTopologyVisual(data);
    const hasSeparateNode = visual && visual.visible;

    const html = livePvSourceSplitHtml(data, isMobileFlow ? '<br>' : ' | ', true);
    const title = livePvSourceSplitText(data, '\n', false);
    const node = document.getElementById('f-node-pv');
    if (html && !hasSeparateNode) {
        detail.innerHTML = html;
        detail.style.display = '';
        detail.title = title;
        if (node) node.title = 'PV gesamt\n' + title;
    } else {
        detail.innerHTML = '';
        detail.style.display = 'none';
        detail.title = '';
        if (node) node.title = title ? 'PV gesamt\n' + title : '';
    }
}

const DIRECT_MARKETING_LUOX_BADGE_VIOLATION_DELAY_MS = 20000;

function directMarketingLuoxBadgeVisual(data, violationAgeMs = 0) {
    const monitor = getDirectMarketingMonitor(data) || {};
    const execution = monitor.export_execution && typeof monitor.export_execution === 'object'
        ? monitor.export_execution
        : {};
    const derating = execution.external_derating && typeof execution.external_derating === 'object'
        ? execution.external_derating
        : (monitor.external_derating && typeof monitor.external_derating === 'object' ? monitor.external_derating : {});
    const requestedLimitW = execution.requested_limit_w !== null
        && execution.requested_limit_w !== undefined
        && Number.isFinite(Number(execution.requested_limit_w))
        ? Number(execution.requested_limit_w)
        : null;
    const deratingLimitW = derating.limit_w !== null
        && derating.limit_w !== undefined
        && Number.isFinite(Number(derating.limit_w))
        ? Number(derating.limit_w)
        : null;
    const hardZeroRequested = execution.requested === true && requestedLimitW !== null && requestedLimitW <= 0;
    const externalZeroSignal = derating.active === true && deratingLimitW !== null && deratingLimitW <= 0;
    if (!hardZeroRequested && !externalZeroSignal) {
        return {visible: false, state: 'hidden', label: '', iconClass: 'fas fa-shield-alt', title: ''};
    }

    const source = String(derating.source || execution.expected_execution_owner || '').toLowerCase();
    const ownerLabel = source.includes('luox') ? 'LUOX' : 'Extern';
    const executionState = String(execution.state || '');
    const externalOwnerConfirmed = execution.external_owner_confirmed === true;
    const complianceConfirmed = execution.compliance_confirmed === true && externalOwnerConfirmed;
    const violation = execution.violation === true
        || ['external_owner_grid_violation', 'violated_unavoidable'].includes(executionState);
    const persistentViolation = execution.unavoidable === true
        || (violation && Math.max(0, Number(violationAgeMs) || 0) >= DIRECT_MARKETING_LUOX_BADGE_VIOLATION_DELAY_MS);
    const gridExportW = Math.max(0, Number(execution.grid_export_w) || 0);
    const violationW = Math.max(0, Number(execution.violation_w) || 0);
    const toleranceW = Math.max(0, Number(execution.grid_tolerance_w) || 0);
    const limitW = requestedLimitW !== null ? requestedLimitW : Math.max(0, deratingLimitW || 0);

    let state = 'settling';
    let label = externalOwnerConfirmed ? `${ownerLabel} regelt` : '0 W angefordert';
    let iconClass = 'fas fa-hourglass-half';
    let statusText = externalOwnerConfirmed
        ? `${ownerLabel}-Einspeisebegrenzung erkannt; der Netzpunkt schwingt noch ein.`
        : 'Einspeisebegrenzung 0 W angefordert; externe Bestätigung steht noch aus.';
    if (persistentViolation) {
        state = 'violation';
        label = '0 W prüfen';
        iconClass = 'fas fa-exclamation-triangle';
        statusText = `${ownerLabel}-Einspeisebegrenzung wird am Netzpunkt weiterhin überschritten.`;
    } else if (complianceConfirmed) {
        state = 'confirmed';
        label = `${ownerLabel} 0 W`;
        iconClass = 'fas fa-shield-alt';
        statusText = `${ownerLabel}-Einspeisebegrenzung 0 W und Netzpunktwirkung bestätigt.`;
    }

    const title = [
        statusText,
        `Einspeiselimit: ${flowPlainWatts(limitW)}`,
        `Netzeinspeisung: ${flowPlainWatts(gridExportW)}`,
        violationW > 0 ? `Überschreitung nach Toleranz: ${flowPlainWatts(violationW)}` : '',
        toleranceW > 0 ? `Messtoleranz: ${flowPlainWatts(toleranceW)}` : ''
    ].filter(Boolean).join('\n');
    return {visible: true, state, label, iconClass, title};
}

function updateEnergyFlowLuoxBadge(data, container = document.getElementById('flow-view')) {
    const badge = container ? container.querySelector('#f-pv-zero-export-badge') : null;
    if (!badge) return;
    const monitor = getDirectMarketingMonitor(data) || {};
    const execution = monitor.export_execution && typeof monitor.export_execution === 'object'
        ? monitor.export_execution
        : {};
    const executionState = String(execution.state || '');
    const violation = execution.violation === true
        || ['external_owner_grid_violation', 'violated_unavoidable'].includes(executionState);
    const nowMs = Date.now();
    if (violation) {
        const violationSinceMs = Number(window._directMarketingLuoxViolationSinceMs);
        if (!Number.isFinite(violationSinceMs) || violationSinceMs <= 0) {
            window._directMarketingLuoxViolationSinceMs = nowMs;
        }
    } else {
        window._directMarketingLuoxViolationSinceMs = null;
    }
    const currentViolationSinceMs = Number(window._directMarketingLuoxViolationSinceMs);
    const violationAgeMs = violation && Number.isFinite(currentViolationSinceMs) && currentViolationSinceMs > 0
        ? Math.max(0, nowMs - currentViolationSinceMs)
        : 0;
    const visual = directMarketingLuoxBadgeVisual(data, violationAgeMs);
    badge.hidden = !visual.visible;
    badge.classList.remove('is-confirmed', 'is-settling', 'is-violation');
    if (!visual.visible) {
        badge.removeAttribute('title');
        badge.removeAttribute('aria-label');
        return;
    }
    badge.classList.add(`is-${visual.state}`);
    badge.title = visual.title;
    badge.setAttribute('aria-label', visual.title.replace(/\n/g, '. '));
    const icon = badge.querySelector('i');
    const label = badge.querySelector('#f-pv-zero-export-label');
    if (icon) icon.className = visual.iconClass;
    if (label) label.textContent = visual.label;
}

function updateEnergyFlowAuxInverterShellyBadge(data, container = document.getElementById('flow-view')) {
    const node = container ? container.querySelector('[data-flow-node="external_pv"]') : null;
    if (!node) return;
    const state = getDirectMarketingAuxInverterShellyState(data) || {};
    const monitor = getDirectMarketingMonitor(data) || {};
    if (Object.keys(state).length) window._directMarketingAuxInverterShellyState = state;
    const pvSource = livePvSourceInfo(data);
    const externalW = pvSource.external;
    const visual = externalPvTopologyVisual(data, {
        configured: node.dataset.externalPvConfigured === '1' || state.ip_configured === true,
        controlAvailable: state.lock_available === true && state.control_available === true
    });
    const capable = visual.visible;
    node.hidden = !capable;
    setEnergyFlowGenerationAggregateVisible(container, capable);
    container.querySelectorAll('#flow-line-external-pv, #flow-dot-external-pv').forEach(line => {
        line.style.display = capable ? '' : 'none';
    });
    if (!capable) return;
    const status = String(state.status || (externalW > 20 ? 'wr_on' : 'unknown'));
    const manualLocked = state.manual_locked === true || status === 'manual_locked';
    const priceLocked = status === 'wr_off' && !manualLocked;
    const wrEnabled = state.desired_wr_on === true || ['wr_on', 'load_unblocked'].includes(status);
    const producing = pvSource.measurementValid && externalW > 20;
    const commandStatus = String(state.command_status || '');
    const remaining = Math.max(0, parseInt(state.switch_lock_remaining_s || 0, 10) || 0);
    const exportConstraintClass = String(monitor.export_constraint_class || state.export_constraint_class || '');
    const exportExecution = monitor.export_execution && typeof monitor.export_execution === 'object'
        ? monitor.export_execution
        : {};
    const exportExecutionState = String(exportExecution.state || '');
    let exportExecutionText = '';
    if (exportExecutionState === 'external_confirmed') {
        exportExecutionText = 'Exportlimit: externer Regler und Netzpunktwirkung bestätigt';
    } else if (exportExecutionState === 'best_effort_storage_absorption') {
        exportExecutionText = 'Exportlimit: nur bestmögliche Speicheraufnahme, nicht extern bestätigt';
    } else if (exportExecutionState === 'violated_unavoidable') {
        exportExecutionText = `Exportlimit nicht einhaltbar: ${flowPlainWatts(exportExecution.violation_w || 0)} Restexport`;
    } else if (exportExecution.requested === true) {
        exportExecutionText = 'Exportlimit angefordert, physische Bestätigung steht aus';
    }
    const value = node.querySelector('#f-val-external-pv');
    const label = node.querySelector('#f-label-external-pv');
    const lockIcon = node.querySelector('#f-external-pv-lock');
    const button = node.querySelector('#f-external-pv-lock-btn');

    if (value) value.textContent = visual.valueText;
    node.classList.toggle('is-producing', producing);
    node.classList.toggle('is-manual-locked', manualLocked);
    node.classList.toggle('is-price-locked', priceLocked);
    const stateColor = manualLocked ? '#dc3545' : (priceLocked ? '#f59e0b' : getFlowColor('external_pv', '#22c55e'));
    container.querySelectorAll('#flow-line-external-pv, #flow-dot-external-pv').forEach(line => line.setAttribute('stroke', stateColor));
    if (!manualLocked && !priceLocked) {
        applyFlowSelectorColor('.node-external-pv', stateColor);
    }
    if (lockIcon) lockIcon.style.display = (manualLocked || priceLocked) ? '' : 'none';

    let labelText = 'Zusatz-WR';
    if (manualLocked) labelText = 'Manuell gesperrt';
    else if (status === 'load_unblocked') labelText = 'Lastfreigabe';
    else if (priceLocked) labelText = 'Negativpreis gesperrt';
    else if (status === 'local_fallback') labelText = 'Lokal gesteuert';
    else if (status === 'http_error') labelText = 'Shelly-Fehler';
    else if (wrEnabled) labelText = 'Aktiv';
    const displayLabel = getFlowLabel('external_pv');
    node.dataset.flowStatus = labelText;
    if (label) label.textContent = displayLabel;

    if (button) {
        const lockAvailable = visual.controlAvailable;
        button.hidden = !lockAvailable;
        button.disabled = !lockAvailable;
        button.setAttribute('aria-pressed', manualLocked ? 'true' : 'false');
        button.title = manualLocked ? `Sperre für ${displayLabel} aufheben` : `${displayLabel} manuell sperren`;
        button.setAttribute('aria-label', button.title);
        const icon = button.querySelector('i');
        if (icon) icon.className = manualLocked ? 'fas fa-lock-open' : 'fas fa-lock';
    }

    const title = [
        `${displayLabel}: ${labelText}`,
        `Leistung: ${visual.valueText}`,
        `Topologie: ${visual.evidenceState} (${visual.topologySource})`,
        !pvSource.measurementValid ? 'Momentanleistung derzeit ungültig oder nicht verfügbar' : '',
        state.price_ct_kwh !== null && state.price_ct_kwh !== undefined ? `Marktpreis: ${Number(state.price_ct_kwh).toLocaleString('de-DE', {maximumFractionDigits: 3})} ct/kWh` : '',
        exportConstraintClass === 'eeg_soft' ? 'EEG-weich: PV-Einspeisung zulässig' : '',
        exportConstraintClass === 'negative_hard' ? 'Negativpreis-hart: Gesamt-Export vermeiden' : '',
        exportExecutionText,
        state.load_w !== null && state.load_w !== undefined ? `Last: ${flowPlainWatts(state.load_w)} / Freigabe ab ${flowPlainWatts(state.unblock_threshold_w || 0)}` : '',
        remaining > 0 ? `Schaltsperre: ${Math.ceil(remaining / 60)} min` : '',
        commandStatus === 'hysteresis_hold' ? 'Zustandswechsel durch Schützschutz zurückgehalten' : '',
        state.error ? `Fehler: ${state.error}` : ''
    ].filter(Boolean).join('\n');
    node.title = title;
}

function updateEnergyFlowPvNodes(data, container = document.getElementById('flow-view')) {
    const pvSource = livePvSourceInfo(data);
    updateEnergyFlowPvSplit(data, container);
    updateEnergyFlowLuoxBadge(data, container);
    updateEnergyFlowAuxInverterShellyBadge(data, container);
    const externalNode = container ? container.querySelector('[data-flow-node="external_pv"]') : null;
    const externalVisible = !!(externalNode && !externalNode.hidden);
    const mainPvW = externalVisible ? pvSource.e3dc : pvSource.total;
    const main = document.getElementById('f-val-pv');
    if (main) main.textContent = flowPlainWatts(mainPvW);
    return {mainPvW, externalPvW: externalVisible ? pvSource.external : 0};
}

function updateEnergyFlowAggregates(values = {}, container = document.getElementById('flow-view')) {
    const generationW = Math.max(0, Number(values.pv || 0)) + Math.max(0, Number(values.external_pv || 0));
    const consumptionW = ['home', 'wallbox', 'wallbox2', 'wp', 'hs', 'climate']
        .reduce((sum, key) => sum + Math.max(0, Number(values[key] || 0)), 0);
    const generation = container ? container.querySelector('#f-val-generation') : null;
    const consumption = container ? container.querySelector('#f-val-consumption') : null;
    if (generation) generation.textContent = flowPlainWatts(generationW);
    if (consumption) consumption.textContent = flowPlainWatts(consumptionW);
    return {generationW, consumptionW};
}

function chartRawY(ctx) {
    const raw = ctx ? ctx.raw : null;
    if (raw && typeof raw === 'object' && Object.prototype.hasOwnProperty.call(raw, 'y')) {
        return raw.y;
    }
    if (ctx && ctx.parsed && typeof ctx.parsed === 'object' && Object.prototype.hasOwnProperty.call(ctx.parsed, 'y')) {
        return ctx.parsed.y;
    }
    return raw;
}

function chartRawIndex(ctx) {
    const raw = ctx ? ctx.raw : null;
    if (raw && typeof raw === 'object' && Number.isFinite(Number(raw.x))) {
        return Math.max(0, Math.round(Number(raw.x)));
    }
    return ctx && Number.isFinite(Number(ctx.dataIndex)) ? Math.max(0, Number(ctx.dataIndex)) : 0;
}

function electricityPriceTooltipLabel(ctx, ecoScore = null) {
    const price = chartRawY(ctx);
    let text = ` Preis: ${price !== null && price !== undefined ? price : '--'} ct/kWh`;
    if (Array.isArray(ecoScore)) {
        const idx = Math.min(ecoScore.length - 1, chartRawIndex(ctx));
        const score = idx >= 0 ? ecoScore[idx] : null;
        if (score !== null && score !== undefined) {
            text += ` | Eco-Score: ${score}`;
        }
    }
    return text;
}

function pushElectricityPriceDataset(datasets, price, yAxisID = 'y2', label = 'Strompreis', options = {}) {
    const series = normalizeElectricityPriceSeries(price);
    if (!series.some(v => v !== null)) {
        return false;
    }
    datasets.push({
        label: label,
        data: series,
        borderColor: options.borderColor || '#8b5cf6',
        backgroundColor: options.backgroundColor || (options.fill ? 'rgba(139, 92, 246, 0.15)' : 'rgba(139, 92, 246, 0)'),
        fill: options.fill === true,
        stepped: 'before',
        tension: 0,
        pointRadius: 0,
        borderWidth: options.borderWidth || 1.5,
        yAxisID: yAxisID,
        spanGaps: false,
        order: options.order || 10
    });
    return true;
}

// --- DIAGRAMM UMSCHALT-LOGIK (ABSOLUTWERTE) ---
let chartFlipNegatives = localStorage.getItem('e3dc_chart_flip') === 'true';
function toggleChartFlip(applyOnly = false) {
    if (applyOnly !== true) {
        chartFlipNegatives = !chartFlipNegatives;
        localStorage.setItem('e3dc_chart_flip', chartFlipNegatives);
    }
    document.querySelectorAll('.btn-chart-flip').forEach(btn => {
        if (chartFlipNegatives) btn.classList.add('active');
        else btn.classList.remove('active');
    });

    if (applyOnly === true) return;

    const modeSelect = document.getElementById('chart-mode-select');
    const isForecast = (modeSelect && modeSelect.value === 'forecast') || window.location.search.includes('seite=forecast');
    const isHybridMobile = window.location.search.includes('seite=hybrid');

    if (isForecast) {
        if (typeof loadJsForecastChart === 'function') loadJsForecastChart('');
    } else if (isHybridMobile) {
        const hist = document.getElementById('mobileHistorySelect');
        const file = hist ? hist.value : '';
        if (typeof loadJsHybridChart === 'function') loadJsHybridChart(currentLiveHours, file);
    } else {
        const hist = document.getElementById('history-select-normal') || document.getElementById('mobileHistorySelect');
        const file = hist ? hist.value : '';
        const modeSelect = document.getElementById('chart-mode-select');
        const modeValue = modeSelect ? modeSelect.value : '';
        if (modeValue === 'hybrid') {
            if (typeof loadJsHybridChart === 'function') loadJsHybridChart(currentLiveHours, file);
        } else if (modeValue === 'price') {
            if (typeof loadJsPriceChart === 'function') loadJsPriceChart(currentLiveHours, file);
        } else {
            if (typeof loadJsLiveChart === 'function') loadJsLiveChart(currentLiveHours, file);
        }
    }
}

document.addEventListener('DOMContentLoaded', () => {
    toggleChartFlip(true);
    initEnergyFlowLayoutEditor();
});

// Stabile Tages-PV-Prognose – direkt aus PHP (logic.php berechnet, kein Async nötig)
let STABLE_PV_TODAY_KWH = (typeof STABLE_PV_TODAY_KWH_PHP !== 'undefined') ? STABLE_PV_TODAY_KWH_PHP : 0;


let liveLineChart = null;
let _directMarketingForecastChartInstance = null;
let currentLiveHours = 6;
let autoRefreshJsChart = null;
let activeJsChartMode = '';
let jsChartRequestGeneration = 0;

function chartTimestampMs(value) {
    let timestamp = Number(value);
    if (!Number.isFinite(timestamp) || timestamp <= 0) return null;
    if (timestamp < 100000000000) timestamp *= 1000;
    return timestamp;
}

function currentLiveSocForChart() {
    const liveData = window._storageLiveData && typeof window._storageLiveData === 'object'
        ? window._storageLiveData
        : {};
    const houseBattery = liveData.house_battery_soc && typeof liveData.house_battery_soc === 'object'
        ? liveData.house_battery_soc
        : null;
    if (!houseBattery) return null;

    const sourceTimestamp = chartTimestampMs(houseBattery.source_ts);
    const sourceAgeMs = sourceTimestamp === null ? null : Date.now() - sourceTimestamp;
    const declaredAgeRaw = houseBattery.age_s;
    const declaredAgePresent = declaredAgeRaw !== null
        && declaredAgeRaw !== undefined
        && declaredAgeRaw !== '';
    const declaredAgeS = !declaredAgePresent
        ? Number.NaN
        : Number(declaredAgeRaw);
    const computedAgeS = sourceAgeMs === null ? null : sourceAgeMs / 1000;
    if (sourceAgeMs === null
        || sourceAgeMs < -5000
        || sourceAgeMs > 5 * 60 * 1000
        || (declaredAgePresent && !Number.isFinite(declaredAgeS))
        || (Number.isFinite(declaredAgeS) && (declaredAgeS < 0 || declaredAgeS > 5 * 60))
        || (Number.isFinite(declaredAgeS)
            && computedAgeS !== null
            && Math.abs(computedAgeS - declaredAgeS) > 30)
    ) {
        return null;
    }

    const value = Number(houseBattery.value);
    return Number.isFinite(value) && value >= 0 && value <= 100 ? value : null;
}

function currentSocMarkerForTimestamps(timestamps, maxDistanceMs = 30 * 60 * 1000) {
    const soc = currentLiveSocForChart();
    const normalized = Array.isArray(timestamps) ? timestamps.map(chartTimestampMs) : [];
    const data = normalized.map(() => null);
    if (soc === null || normalized.length === 0) return {soc: null, data, index: -1};

    const nowMs = Date.now();
    let nearestIndex = -1;
    let nearestDistance = Number.POSITIVE_INFINITY;
    normalized.forEach((timestamp, index) => {
        if (timestamp === null) return;
        const distance = Math.abs(timestamp - nowMs);
        if (distance < nearestDistance) {
            nearestDistance = distance;
            nearestIndex = index;
        }
    });
    if (nearestIndex < 0 || nearestDistance > maxDistanceMs) return {soc, data, index: -1};
    data[nearestIndex] = soc;
    return {soc, data, index: nearestIndex};
}

function directMarketingSocProjectionForTimestamps(view, timestamps = []) {
    if (!view || view.active !== true || view.state !== 'complete'
        || !view.series || !Array.isArray(view.series.soc)
        || !Array.isArray(timestamps) || timestamps.length === 0) {
        return null;
    }
    const points = view.series.soc
        .map(point => ({
            x: chartTimestampMs(point && point.x),
            y: Number(point && point.y),
        }))
        .filter(point => point.x !== null && Number.isFinite(point.y))
        .sort((left, right) => left.x - right.x);
    if (points.length < 2) return null;

    let cursor = 0;
    const projection = timestamps.map(value => {
        const timestamp = chartTimestampMs(value);
        if (timestamp === null || timestamp < points[0].x
            || timestamp > points[points.length - 1].x) {
            return null;
        }
        while (cursor + 1 < points.length && points[cursor + 1].x <= timestamp) {
            cursor += 1;
        }
        return points[cursor].y;
    });
    return projection.some(Number.isFinite) ? projection : null;
}

function buildCompactChartTimeContext(timestamps, fallbackLabels, gridColor, textColor, isDarkMode, maxTicksLimit = 8) {
    const sourceTimestamps = Array.isArray(timestamps) ? timestamps : [];
    const sourceLabels = Array.isArray(fallbackLabels) ? fallbackLabels : [];
    const count = Math.max(sourceTimestamps.length, sourceLabels.length);
    const dateFormatter = new Intl.DateTimeFormat('de-DE', {
        timeZone: 'Europe/Berlin', weekday: 'short', day: '2-digit', month: '2-digit'
    });
    const dayKeyFormatter = new Intl.DateTimeFormat('sv-SE', {
        timeZone: 'Europe/Berlin', year: 'numeric', month: '2-digit', day: '2-digit'
    });
    const timeFormatter = new Intl.DateTimeFormat('de-DE', {
        timeZone: 'Europe/Berlin', hour: '2-digit', minute: '2-digit', hourCycle: 'h23'
    });
    let zoneFormatter;
    try {
        zoneFormatter = new Intl.DateTimeFormat('de-DE', {
            timeZone: 'Europe/Berlin', timeZoneName: 'shortOffset'
        });
    } catch (error) {
        zoneFormatter = new Intl.DateTimeFormat('de-DE', {
            timeZone: 'Europe/Berlin', timeZoneName: 'short'
        });
    }
    const baseTimeLabels = [];
    const localSlotKeys = [];
    const zoneLabels = [];
    const localSlotCounts = new Map();
    const dateParts = [];
    const daySeparatorIndices = new Set();
    let previousDayKey = '';

    for (let index = 0; index < count; index += 1) {
        const timestamp = chartTimestampMs(sourceTimestamps[index]);
        const date = timestamp !== null ? new Date(timestamp) : null;
        const validDate = date && Number.isFinite(date.getTime());
        const dayKey = validDate ? dayKeyFormatter.format(date) : '';
        const datePart = validDate ? dateFormatter.format(date) : '';
        const timeLabel = validDate ? timeFormatter.format(date) : String(sourceLabels[index] || '');
        const localSlotKey = validDate ? `${dayKey}|${timeLabel}` : '';
        const zonePart = validDate
            ? zoneFormatter.formatToParts(date).find(part => part.type === 'timeZoneName')
            : null;
        if (dayKey && previousDayKey && dayKey !== previousDayKey) daySeparatorIndices.add(index);
        if (dayKey) previousDayKey = dayKey;
        if (localSlotKey) localSlotCounts.set(localSlotKey, (localSlotCounts.get(localSlotKey) || 0) + 1);
        baseTimeLabels.push(timeLabel);
        localSlotKeys.push(localSlotKey);
        zoneLabels.push(zonePart ? zonePart.value : '');
        dateParts.push(datePart);
    }

    const timeLabels = baseTimeLabels.map((timeLabel, index) => (
        localSlotKeys[index]
        && localSlotCounts.get(localSlotKeys[index]) > 1
        && zoneLabels[index]
            ? `${timeLabel} ${zoneLabels[index]}`
            : timeLabel
    ));
    const dateTimeLabels = timeLabels.map((timeLabel, index) => (
        dateParts[index] ? `${dateParts[index]} ${timeLabel}` : timeLabel
    ));

    const compactTickIndices = new Set();
    const boundedTickLimit = Math.max(2, Number(maxTicksLimit) || 8);
    if (count > 0) compactTickIndices.add(0);
    if (count > 1) compactTickIndices.add(count - 1);
    daySeparatorIndices.forEach(index => compactTickIndices.add(index));
    const remainingTickSlots = Math.max(0, boundedTickLimit - compactTickIndices.size);
    for (let slot = 1; slot <= remainingTickSlots; slot += 1) {
        const index = Math.round((slot * (count - 1)) / (remainingTickSlots + 1));
        if (index >= 0 && index < count) compactTickIndices.add(index);
    }

    const chartDataIndex = context => {
        const tickValue = Number(context && context.tick ? context.tick.value : Number.NaN);
        return Number.isFinite(tickValue) ? Math.round(tickValue) : -1;
    };

    return {
        labels: timeLabels,
        tooltipTitle: items => {
            if (!items || !items.length) return '';
            const index = items[0].dataIndex;
            return dateTimeLabels[index] || timeLabels[index] || '';
        },
        xScale: {
            afterBuildTicks: scale => {
                if (!scale || !Array.isArray(scale.ticks)) return;
                scale.ticks = scale.ticks.filter(tick => {
                    const value = Number(tick && tick.value);
                    return Number.isFinite(value) && compactTickIndices.has(Math.round(value));
                });
            },
            grid: {
                color: ctx => daySeparatorIndices.has(chartDataIndex(ctx))
                    ? (isDarkMode ? 'rgba(148, 163, 184, 0.65)' : 'rgba(100, 116, 139, 0.5)')
                    : gridColor,
                lineWidth: ctx => daySeparatorIndices.has(chartDataIndex(ctx)) ? 2 : 1
            },
            ticks: {
                color: textColor,
                autoSkip: false,
                maxTicksLimit: boundedTickLimit,
                maxRotation: 0,
                callback: function(value, index) {
                    const numericValue = Number(value);
                    const dataIndex = Number.isFinite(numericValue) ? Math.round(numericValue) : index;
                    const timeLabel = timeLabels[dataIndex]
                        || (this.getLabelForValue ? this.getLabelForValue(value) : String(value));
                    if (dataIndex === 0 || daySeparatorIndices.has(dataIndex)) {
                        const datePart = dateParts[dataIndex] || '';
                        return datePart ? [datePart, timeLabel] : timeLabel;
                    }
                    return timeLabel;
                }
            }
        }
    };
}

function renderDirectMarketingForecastChart(data = {}, options = {}) {
    const surface = document.getElementById('directMarketingForecastSurface');
    const primarySurface = document.getElementById('primaryChartSurface');
    const canvas = document.getElementById('directMarketingForecastChart');
    const stateEl = document.getElementById('directMarketingForecastState');
    const view = directMarketingTrajectoryViewModel(data);
    // Die physikalische Standard-Prognose bleibt immer die sichtbare Fläche.
    // Eine vollständige DV-Trajektorie ersetzt dort ausschließlich die
    // geplante SoC-Linie; diese alte Exklusivfläche bleibt daher verborgen.
    const showDirectMarketing = false;
    if (showDirectMarketing || view.active !== true) clearForecastProjectionStatus();
    if (primarySurface) primarySurface.style.display = showDirectMarketing ? 'none' : '';
    if (!surface || !canvas) return view;
    if (!showDirectMarketing) {
        surface.style.display = 'none';
        if (stateEl) stateEl.textContent = '';
        if (_directMarketingForecastChartInstance) {
            _directMarketingForecastChartInstance.destroy();
            _directMarketingForecastChartInstance = null;
        }
        return view;
    }
    surface.style.display = '';
    if (_directMarketingForecastChartInstance) {
        _directMarketingForecastChartInstance.destroy();
        _directMarketingForecastChartInstance = null;
    }
    if (!['complete', 'actions_only'].includes(view.state) || typeof Chart === 'undefined') {
        if (stateEl) {
            const currentSoc = currentLiveSocForChart();
            const currentSocText = currentSoc !== null ? ` · aktueller SoC ${currentSoc.toFixed(1)}%` : '';
            stateEl.textContent = ['complete', 'actions_only'].includes(view.state)
                ? 'Diagrammbibliothek nicht verfügbar'
                : `EVIDENCE_LIMIT: ${view.reasonCode || 'DV-Trajektorie unvollständig'}${currentSocText}`;
        }
        return view;
    }
    const pointTimestamps = Array.from(new Set(
        view.slots.map(slot => slot.startTs)
            .concat(view.slots
                .filter(slot => slot.plannedRole === 'projection' && Number.isFinite(slot.projectionEffectiveStartTs))
                .map(slot => slot.projectionEffectiveStartTs))
            .concat([view.slots[view.slots.length - 1].endTs])
    )).sort((left, right) => left - right);
    const slotIndexForTimestamp = timestamp => view.slots.findIndex(slot => slot.startTs <= timestamp && timestamp < slot.endTs);
    const seriesForPoints = (values, projectionOnly = false) => pointTimestamps.map(timestamp => {
        const slotIndex = slotIndexForTimestamp(timestamp);
        if (slotIndex < 0) return null;
        const slot = view.slots[slotIndex];
        if (projectionOnly && !(slot.plannedRole === 'projection'
            && Number.isFinite(slot.projectionEffectiveStartTs)
            && Number.isFinite(slot.projectionEffectiveDurationS)
            && timestamp >= slot.projectionEffectiveStartTs
            && timestamp < slot.projectionEffectiveStartTs + slot.projectionEffectiveDurationS * 1000)) return null;
        return values[slotIndex] ?? null;
    });
    const socForPoints = pointTimestamps.map(timestamp => {
        const slotIndex = slotIndexForTimestamp(timestamp);
        return slotIndex >= 0 ? view.slots[slotIndex].socStartPct : view.slots[view.slots.length - 1].socEndPct;
    });
    const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    const tickColor = isDark ? '#adb5bd' : '#6c757d';
    const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.07)';
    const timeContext = buildCompactChartTimeContext(pointTimestamps, [], gridColor, tickColor, isDark, 8);
    const currentSoc = currentSocMarkerForTimestamps(pointTimestamps);
    if (stateEl) {
        const currentSocText = currentSoc.soc !== null ? ` · aktueller SoC ${currentSoc.soc.toFixed(1)}%` : '';
        stateEl.textContent = view.state === 'actions_only'
            ? `SoC-Prognose EVIDENCE_LIMIT · nur ausgewählte Planaktionen${currentSocText}`
            : `Kanonischer DV-Plan · keine Runtime-/Wirkbestätigung${currentSocText}`;
    }
    const showSocAxis = view.state === 'complete' || currentSoc.index >= 0;
    _directMarketingForecastChartInstance = new Chart(canvas, {
        type: 'line',
        data: {labels: timeContext.labels, datasets: [
            ...(view.state === 'complete' ? [{label: 'DV-SoC-Prognose', data: socForPoints, borderColor: '#8b5cf6', backgroundColor: 'rgba(139,92,246,0.08)', borderWidth: 2.5, pointRadius: 0, tension: 0, stepped: 'after', yAxisID: 'ySoc', order: 1}] : []),
            ...(currentSoc.index >= 0 ? [{label: 'Aktueller SoC (Messwert)', data: currentSoc.data, showLine: false, borderColor: '#22c55e', backgroundColor: '#22c55e', pointRadius: 6, pointHoverRadius: 8, pointStyle: 'circle', yAxisID: 'ySoc', order: 0}] : []),
            {label: 'PV speichern (Plan)', data: seriesForPoints(view.series.pvStoreW).map(value => value === null ? null : value / 1000), type: 'bar', borderColor: '#3b82f6', backgroundColor: 'rgba(59,130,246,0.38)', borderWidth: 1, borderSkipped: false, yAxisID: 'yPower', order: 3},
            {label: 'Wirtschaftlicher Export (Plan)', data: seriesForPoints(view.series.economicExportW).map(value => value === null ? null : value / 1000), type: 'bar', borderColor: '#10b981', backgroundColor: 'rgba(16,185,129,0.38)', borderWidth: 1, borderSkipped: false, yAxisID: 'yPower', order: 3},
            {label: 'Headroom-Export (Prognose, keine Ausführung)', data: seriesForPoints(view.series.headroomProjectionW, true).map(value => value === null ? null : value / 1000), type: 'bar', borderColor: '#06b6d4', backgroundColor: 'rgba(6,182,212,0.28)', borderWidth: 1, borderDash: [4, 3], borderSkipped: false, yAxisID: 'yPower', order: 4},
            {label: 'Laden gesperrt / Halten (Plan)', data: seriesForPoints(view.series.chargeBlock), type: 'bar', borderColor: '#f59e0b', backgroundColor: 'rgba(245,158,11,0.16)', borderWidth: 0, borderSkipped: false, barPercentage: 1, categoryPercentage: 1, yAxisID: 'yState', order: 10}
        ]},
        options: {
            responsive: true, maintainAspectRatio: false, animation: false,
            interaction: {mode: 'index', intersect: false},
            plugins: {
                legend: {display: true, position: 'top', labels: {color: tickColor, boxWidth: 12, font: {size: 11}}},
                tooltip: {callbacks: {
                    title: timeContext.tooltipTitle,
                    label: ctx => {
                        const label = ctx.dataset && ctx.dataset.label ? ctx.dataset.label : '';
                        if (ctx.raw === null) return '';
                        if (label === 'DV-SoC-Prognose') return `DV-SoC: ${Number(ctx.raw).toFixed(1)}%`;
                        if (label === 'Aktueller SoC (Messwert)') return `Aktueller SoC: ${Number(ctx.raw).toFixed(1)}%`;
                        if (label === 'Laden gesperrt / Halten (Plan)') return label;
                        return `${label}: ${Number(ctx.raw).toFixed(2)} kW`;
                    }
                }}
            },
            scales: {
                x: timeContext.xScale,
                ...(showSocAxis ? {ySoc: {type: 'linear', position: 'left', min: 0, max: 100, ticks: {color: '#8b5cf6', callback: value => value + '%'}, grid: {color: gridColor}}} : {}),
                yPower: {type: 'linear', position: 'right', min: 0, ticks: {color: tickColor, callback: value => value + ' kW'}, grid: {display: false}},
                yState: {type: 'linear', display: false, min: 0, max: 1, grid: {display: false}}
            }
        }
    });
    return view;
}

function stopAutoRefreshJsChart() {
    if (autoRefreshJsChart !== null) {
        clearInterval(autoRefreshJsChart);
        autoRefreshJsChart = null;
    }
}

function activateJsChartMode(mode) {
    const normalizedMode = String(mode || '');
    if (activeJsChartMode !== normalizedMode) {
        activeJsChartMode = normalizedMode;
        jsChartRequestGeneration += 1;
        stopAutoRefreshJsChart();
    }
    return jsChartRequestGeneration;
}

function beginJsChartRequest(mode) {
    activateJsChartMode(mode);
    stopAutoRefreshJsChart();
    jsChartRequestGeneration += 1;
    return jsChartRequestGeneration;
}

function isCurrentJsChartRequest(mode, requestGeneration) {
    return activeJsChartMode === mode && jsChartRequestGeneration === requestGeneration;
}

function scheduleJsChartAutoRefresh(mode, requestGeneration, callback) {
    if (!isCurrentJsChartRequest(mode, requestGeneration)) return;
    stopAutoRefreshJsChart();
    autoRefreshJsChart = setInterval(() => {
        if (isCurrentJsChartRequest(mode, requestGeneration)) callback();
    }, 60000);
}

function loadJsLiveChart(hours, file = null) {
    renderDirectMarketingForecastChart({direct_marketing_enabled: false});
    const requestGeneration = beginJsChartRequest('live');
    currentLiveHours = hours;

    let url = 'get_chart_data.php?hours=' + hours;
    if (file) url += '&file=' + encodeURIComponent(file);

    fetch(url)
        .then(r => r.json())
        .then(data => {
            if (!isCurrentJsChartRequest('live', requestGeneration)) return;
            if (data.error) return;

            // Forecast-SoC ist eine geplante Kurve, keine BMS-Messreihe.
            if (data.soc && data.bat) {
                data.soc = applyPhysicalSocFilter(data.soc, data.bat);
            }

            const isDarkMode = typeof DARK_MODE !== 'undefined' ? DARK_MODE : true;
            const textColor = isDarkMode ? '#aaa' : '#666';
            const gridColor = isDarkMode ? '#333' : '#e9ecef';

            const mapFlip = (arr) => chartFlipNegatives && arr ? arr.map(value => {
                const n = e3dcFiniteNumberOrNull(value);
                return n === null ? null : Math.abs(n);
            }) : arr;
            const dashIfNeg = (arr) => (ctx) => {
                if (ctx.p0DataIndex === undefined) return undefined;
                const p0 = arr ? e3dcFiniteNumberOrNull(arr[ctx.p0DataIndex]) : null;
                const p1 = arr ? e3dcFiniteNumberOrNull(arr[ctx.p1DataIndex]) : null;
                return chartFlipNegatives && ((p0 !== null && p0 < 0) || (p1 !== null && p1 < 0)) ? [4, 4] : undefined;
            };

            let datasets = [];
            let yAxes = { y: { type: 'linear', display: true, position: 'left', grid: { color: gridColor }, ticks: { color: textColor } } };

            if (CURRENT_VIEW === 'pv') {
                const hasExternalPv = chartSeriesHasMagnitude(data.pv_external_w);
                datasets = [
                    { label: 'Sonne Gesamt', data: data.pv, borderColor: getFlowColor('pv', '#ffc107'), backgroundColor: flowColorAlpha('pv', 0.15, '#ffc107'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 }
                ];
                if (hasExternalPv) {
                    datasets.push(
                        { label: 'E3DC-PV', data: data.pv_e3dc_w, borderColor: '#f59e0b', borderDash: [3, 3], tension: 0.3, pointRadius: 0, borderWidth: 2 },
                        { label: 'Zusatz-WR', data: data.pv_external_w, borderColor: '#22c55e', borderDash: [6, 4], tension: 0.3, pointRadius: 0, borderWidth: 2 }
                    );
                }
                datasets.push(
                    { label: 'String 1', data: data.dc0_w, borderColor: '#fd7e14', borderDash: [5, 5], tension: 0.3, pointRadius: 0, borderWidth: 2 },
                    { label: 'String 2', data: data.dc1_w, borderColor: '#e83e8c', borderDash: [5, 5], tension: 0.3, pointRadius: 0, borderWidth: 2 }
                );
            } else if (CURRENT_VIEW === 'grid') {
                const gridPhaseDatasets = [];
                if (chartSeriesHasFiniteValue(data.grid_p1)) gridPhaseDatasets.push({ label: 'L1', data: mapFlip(data.grid_p1), borderColor: '#8b5cf6', tension: 0.3, pointRadius: 0, borderWidth: 1.5, segment: { borderDash: dashIfNeg(data.grid_p1) } });
                if (chartSeriesHasFiniteValue(data.grid_p2)) gridPhaseDatasets.push({ label: 'L2', data: mapFlip(data.grid_p2), borderColor: '#ec4899', tension: 0.3, pointRadius: 0, borderWidth: 1.5, segment: { borderDash: dashIfNeg(data.grid_p2) } });
                if (chartSeriesHasFiniteValue(data.grid_p3)) gridPhaseDatasets.push({ label: 'L3', data: mapFlip(data.grid_p3), borderColor: '#14b8a6', tension: 0.3, pointRadius: 0, borderWidth: 1.5, segment: { borderDash: dashIfNeg(data.grid_p3) } });
                datasets = [
                    { label: 'Netz Gesamt', data: mapFlip(data.grid), borderColor: getFlowColor('grid', '#6c757d'), backgroundColor: flowColorAlpha('grid', 0.15, '#6c757d'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, segment: { borderDash: dashIfNeg(data.grid) } },
                    { label: 'WR Gesamt', data: mapFlip(data.ac_total), borderColor: '#ffc107', borderDash: [5, 5], tension: 0.3, pointRadius: 0, borderWidth: 2, segment: { borderDash: dashIfNeg(data.ac_total) } },
                    ...gridPhaseDatasets
                ];
            } else if (CURRENT_VIEW === 'wb' || CURRENT_VIEW === 'wb2') {
                const isWb2View = CURRENT_VIEW === 'wb2';
                const wbTotal = isWb2View ? (data.wb2 || []) : (data.wb || []);
                const wbP1 = isWb2View ? (data.wb2_p1 || []) : (data.wb_p1 || []);
                const wbP2 = isWb2View ? (data.wb2_p2 || []) : (data.wb_p2 || []);
                const wbP3 = isWb2View ? (data.wb2_p3 || []) : (data.wb_p3 || []);
                const wbLabel = isWb2View ? 'Wallbox 2' : 'Wallbox 1';
                const wbColor = getFlowColor(isWb2View ? 'wallbox2' : 'wallbox', isWb2View ? '#34d399' : '#2ecc71');
                datasets = [
                    { label: wbLabel, data: wbTotal, borderColor: wbColor, backgroundColor: flowColorAlpha(isWb2View ? 'wallbox2' : 'wallbox', 0.15, wbColor), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2 },
                    { label: 'L1', data: wbP1, borderColor: '#8b5cf6', tension: 0.3, pointRadius: 0, borderWidth: 1.5 },
                    { label: 'L2', data: wbP2, borderColor: '#ec4899', tension: 0.3, pointRadius: 0, borderWidth: 1.5 },
                    { label: 'L3', data: wbP3, borderColor: '#14b8a6', tension: 0.3, pointRadius: 0, borderWidth: 1.5 }
                ];
            } else if (CURRENT_VIEW === 'bat') {
                datasets = [
                    { label: 'Batterie Leistung', data: mapFlip(data.bat), borderColor: getFlowColor('battery', '#198754'), backgroundColor: flowColorAlpha('battery', 0.15, '#198754'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.bat) } },
                    { label: 'Spannung', data: data.bat_v, borderColor: '#f59e0b', tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' },
                    { label: 'Strom', data: mapFlip(data.bat_a), borderColor: '#0ea5e9', tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y2', segment: { borderDash: dashIfNeg(data.bat_a) } }
                ];
                if (data.bat1_v && data.bat1_v.some(v => v !== null && v > 0)) {
                    datasets.push({ label: 'Spannung K2', data: data.bat1_v, borderColor: '#fbbf24', borderDash: [4, 4], tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' });
                    datasets.push({ label: 'Strom K2', data: data.bat1_a, borderColor: '#38bdf8', borderDash: [4, 4], tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y2' });
                }
                yAxes['y1'] = { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { color: textColor } };
                yAxes['y2'] = { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { color: textColor } };
            } else if (CURRENT_VIEW === 'hs') {
                datasets = [{ label: 'Heizstab Leistung (W)', data: data.hs || [], borderColor: getFlowColor('heater', '#fd7e14'), backgroundColor: flowColorAlpha('heater', 0.15, '#fd7e14'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y' }];
            } else if (CURRENT_VIEW === 'climate') {
                datasets = [{ label: 'Klima Leistung (W)', data: data.climate || [], borderColor: getFlowColor('climate', '#38bdf8'), backgroundColor: flowColorAlpha('climate', 0.16, '#38bdf8'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y' }];
            } else if (CURRENT_VIEW === 'wp') {
                datasets = [{ label: 'WP Leistung (W)', data: data.wp, borderColor: getFlowColor('heatpump', '#f97316'), backgroundColor: flowColorAlpha('heatpump', 0.16, '#f97316'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y' }];
                if (data.wp_vl && data.wp_vl.some(v => v !== null)) {
                    datasets.push({ label: 'Vorlauf (°C)', data: data.wp_vl, borderColor: '#ef4444', tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' });
                    datasets.push({ label: 'Rücklauf (°C)', data: data.wp_rl, borderColor: '#3b82f6', tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' });
                    datasets.push({ label: 'Warmwasser (°C)', data: data.wp_ww, borderColor: '#f59e0b', tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' });
                    datasets.push({ label: 'Außentemp. (°C)', data: data.wp_at, borderColor: '#10b981', borderDash: [4, 4], tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y1' });
                }
                if (data.wp_kaelte && data.wp_kaelte.some(v => v !== null)) {
                    datasets.push({ label: 'Kältespeicher (°C)', data: data.wp_kaelte, borderColor: '#38bdf8', tension: 0.3, pointRadius: 0, borderWidth: 2.5, yAxisID: 'y1' });
                    if (data.wp_kaelte_soll && data.wp_kaelte_soll.some(v => v !== null)) {
                        datasets.push({ label: 'Kältespeicher Soll (°C)', data: data.wp_kaelte_soll, borderColor: '#7dd3fc', borderDash: [5, 4], tension: 0.2, pointRadius: 0, borderWidth: 1.5, yAxisID: 'y1' });
                    }
                }
                if (data.wp_freq && data.wp_freq.some(v => v !== null)) {
                    datasets.push({ label: 'Frequenz (Hz)', data: data.wp_freq, borderColor: '#8b5cf6', tension: 0.3, pointRadius: 0, borderWidth: 1, yAxisID: 'y2' });
                }
                if (datasets.some(d => d.yAxisID === 'y1')) {
                    yAxes['y1'] = { type: 'linear', display: true, position: 'right', title: {display: false}, grid: { drawOnChartArea: false }, ticks: { color: textColor } };
                }
                if (datasets.some(d => d.yAxisID === 'y2')) {
                    yAxes['y2'] = { type: 'linear', display: true, position: 'right', title: {display: false}, grid: { drawOnChartArea: false }, ticks: { color: textColor } };
                }
            } else {
                datasets = [
                    { label: 'Sonne (PV)', data: data.pv, borderColor: getFlowColor('pv', '#ffc107'), backgroundColor: flowColorAlpha('pv', 0.15, '#ffc107'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 }
                ];

                // --- NEU: PEAK SHAVING OVERLAY ---
                if (typeof SHOW_PEAK_SHAVING !== 'undefined' && SHOW_PEAK_SHAVING && typeof E3DC_LIMITS !== 'undefined' && E3DC_LIMITS.einspeise > 0) {
                    let limitLineData = data.pv.map((_, i) => {
                        let h = data.home ? (data.home[i] || 0) : 0;
                        let w = data.wp ? (data.wp[i] || 0) : 0;
                        let c = data.climate ? (data.climate[i] || 0) : 0;
                        let maxAc = h + w + c + E3DC_LIMITS.einspeise;
                        let rawLimit = E3DC_LIMITS.wr > 0 ? Math.min(E3DC_LIMITS.wr, maxAc) : maxAc;
                        return Math.min(rawLimit, data.pv[i]);
                    });

                    let kuppeData = [...data.pv];
                    let gridLimit = data.pv.map((_, i) => {
                        let h = data.home ? (data.home[i] || 0) : 0;
                        let w = data.wp ? (data.wp[i] || 0) : 0;
                        let c = data.climate ? (data.climate[i] || 0) : 0;
                        let b = (data.wb ? (data.wb[i] || 0) : 0) + (data.wb2 ? (data.wb2[i] || 0) : 0);
                        let gl = E3DC_LIMITS.einspeise + h + w + c + Math.abs(b);
                        return chartFlipNegatives ? gl : -gl;
                    });

                    datasets.push({ label: '__HIDDEN__Abregel-Limit', data: limitLineData, showLine: false, borderColor: 'rgba(32, 201, 151, 0)', backgroundColor: 'rgba(32, 201, 151, 0)', borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, hoverBorderWidth: 0, fill: false, tension: 0.3, yAxisID: 'y', order: 9 });
                    datasets.push({ label: 'Peak-Ersparnis', data: kuppeData, showLine: false, borderColor: 'rgba(32, 201, 151, 0)', fill: { target: '-1', above: 'rgba(32, 201, 151, 0.7)', below: 'rgba(32, 201, 151, 0)' }, tension: 0.3, pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, borderWidth: 0, yAxisID: 'y', order: 8 });
                    datasets.push({ label: 'Netzeinspeise-Limit', data: gridLimit, borderColor: 'rgba(255, 0, 0, 0.5)', borderDash: [5, 5], fill: false, tension: 0.3, pointRadius: 0, borderWidth: 1, yAxisID: 'y', order: 11 });
                }

                datasets.push(
                    { label: 'Hausverbrauch', data: data.home, borderColor: getFlowColor('home', '#0dcaf0'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 },
                    { label: 'Batterie', data: mapFlip(data.bat), borderColor: getFlowColor('battery', '#198754'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.bat) }, order: 10 },
                    { label: 'Netz', data: mapFlip(data.grid), borderColor: getFlowColor('grid', '#6c757d'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.grid) }, order: 10 },
                    { label: 'SoC (%)', data: data.soc, borderColor: '#20c997', tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y1', order: 5 }
                );
                pushStorageTargetCurveDataset(datasets, data.storage_target_curve);
                pushMarketChargeDataset(datasets, data.market_charge);
                pushPredumpCandidateDataset(datasets, data.predump_candidate_w);
                pushPredumpHeadroomDataset(datasets, data.predump_w);
                if (data.wb && data.wb.length > 0 && Math.max(...data.wb.map(Math.abs)) > 0) datasets.push({ label: 'Wallbox 1', data: mapFlip(data.wb), borderColor: getFlowColor('wallbox', '#2ecc71'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.wb) }, order: 10 });
                if (data.wb2 && data.wb2.length > 0 && Math.max(...data.wb2.map(Math.abs)) > 0) datasets.push({ label: 'Wallbox 2', data: mapFlip(data.wb2), borderColor: getFlowColor('wallbox2', '#34d399'), borderDash: [2, 3], tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.wb2) }, order: 10 });
                if (data.hs && data.hs.length > 0 && Math.max(...data.hs) > 0) datasets.push({ label: 'Heizstab', data: data.hs, borderColor: getFlowColor('heater', '#fd7e14'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 });
                if (data.wp && data.wp.length > 0 && Math.max(...data.wp) > 0) datasets.push({ label: 'Wärmepumpe', data: data.wp, borderColor: getFlowColor('heatpump', '#f97316'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 });
                if (data.climate && data.climate.length > 0 && Math.max(...data.climate) > 0) datasets.push({ label: 'Klima', data: data.climate, borderColor: getFlowColor('climate', '#38bdf8'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 });
                if (data.dv_grid && data.dv_grid.length > 0 && Math.max(...data.dv_grid) > 0) datasets.push({ label: 'Direktvermarktung (Verkauf)', data: data.dv_grid, backgroundColor: 'rgba(16, 185, 129, 0.6)', borderColor: '#10b981', type: 'bar', borderWidth: 1, yAxisID: 'y', order: 0 });

                yAxes['y1'] = { type: 'linear', display: true, position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: textColor } };
                if (pushElectricityPriceDataset(datasets, data.price, 'y2', 'Strompreis')) {
                    yAxes['y2'] = { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { color: textColor } };
                }
            }

            datasets = applyHiddenState(datasets);

            if (liveLineChart) {
                liveLineChart.resetZoom();
                liveLineChart.data.labels = data.labels; liveLineChart.data.datasets = datasets;
                liveLineChart.options.scales = { x: liveLineChart.options.scales.x, ...yAxes };
                liveLineChart.update('none');
            } else {
                const ctx = document.getElementById('liveChartCanvas').getContext('2d');
                liveLineChart = new Chart(ctx, {
                    type: 'line', data: { labels: data.labels, datasets: datasets },
                    options: {
                        responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
                        plugins: {
                            legend: {
                                position: 'top',
                                labels: {
                                    usePointStyle: true, boxWidth: 8, padding: 15, color: textColor,
                                    filter: function(item, chart) { return item.text && !item.text.includes('__HIDDEN__'); }
                                },
                                onClick: function(e, legendItem, legend) {
                                    const index = legendItem.datasetIndex;
                                    const ci = legend.chart;
                                    const isHidden = ci.isDatasetVisible(index);
                                    if (isHidden) ci.hide(index); else ci.show(index);
                                    legendItem.hidden = isHidden;
                                    saveHiddenDataset(legendItem.text, isHidden);
                                }
                            },
                            tooltip: {
                                filter: function(item) {
                                    if (item.dataset.label && item.dataset.label.includes('__HIDDEN__')) return false;
                                    if (item.dataset.label === 'Peak-Ersparnis') {
                                        let limitIdx = item.chart.data.datasets.findIndex(d => d.label === '__HIDDEN__Abregel-Limit');
                                        if (limitIdx >= 0) {
                                            let limitVal = item.chart.data.datasets[limitIdx].data[item.dataIndex];
                                            if (chartRawY(item) <= limitVal) return false;
                                        }
                                    }
                                    return true;
                                },
                                callbacks: { label: (ctx) => {
                                let unit = 'W'; let l = ctx.dataset.label;
                                if (l && l.includes('__HIDDEN__')) return null;
                                if (l.includes('(%)') || l === 'SoC (%)') unit = '%'; else if (l.includes('Spannung')) unit = 'V'; else if (l.includes('Strompreis')) unit = 'ct/kWh'; else if (l.includes('Strom')) unit = 'A';
                                else if (l.includes('(°C)')) unit = '°C'; else if (l.includes('(Hz)')) unit = 'Hz';
                                let cleanLabel = l.replace(/\s\([^)]+\)/, ''); // Entfernt "(°C)" aus der Anzeige im Text

                                let origVal = chartRawY(ctx);
                                if (chartFlipNegatives) {
                                    if (l === 'Batterie Leistung' || l === 'Batterie') origVal = data.bat[ctx.dataIndex];
                                    else if (l === 'Netz Gesamt' || l === 'Netz') origVal = data.grid[ctx.dataIndex];
                                    else if (l === 'Wallbox' || l === 'Wallbox 1' || l === 'Wallbox Gesamt') origVal = data.wb[ctx.dataIndex];
                                    else if (l === 'Wallbox 2') origVal = data.wb2[ctx.dataIndex];
                                    else if (l === 'WR Gesamt') origVal = data.ac_total[ctx.dataIndex];
                                    else if (l === 'L1' && CURRENT_VIEW === 'grid') origVal = data.grid_p1[ctx.dataIndex];
                                    else if (l === 'L2' && CURRENT_VIEW === 'grid') origVal = data.grid_p2[ctx.dataIndex];
                                    else if (l === 'L3' && CURRENT_VIEW === 'grid') origVal = data.grid_p3[ctx.dataIndex];
                                    else if (l === 'Strom') origVal = data.bat_a[ctx.dataIndex];
                                    else if (l === 'Strom K2') origVal = data.bat1_a[ctx.dataIndex];
                                }

                                let val = chartRawY(ctx);
                                if (cleanLabel === 'Peak-Ersparnis') {
                                    let limitIdx = ctx.chart.data.datasets.findIndex(d => d.label === '__HIDDEN__Abregel-Limit');
                                    if (limitIdx >= 0) {
                                        val = Math.round(val - ctx.chart.data.datasets[limitIdx].data[ctx.dataIndex]);
                                    }
                                } else if (cleanLabel === 'Batterie' || cleanLabel === 'Batterie Leistung') {
                                    cleanLabel = origVal > 0 ? 'Laden' : (origVal < 0 ? 'Entladen' : 'Batterie');
                                    val = Math.abs(val);
                                } else if (cleanLabel === 'Netz' || cleanLabel === 'Netz Gesamt') {
                                    cleanLabel = origVal > 0 ? 'Netzbezug' : (origVal < 0 ? 'Einspeisung' : 'Netz');
                                    val = Math.abs(val);
                                } else if (cleanLabel === 'Wallbox' || cleanLabel === 'Wallbox 1' || cleanLabel === 'Wallbox 2' || cleanLabel === 'Wallbox Gesamt') {
                                    cleanLabel = origVal < -50 ? `${cleanLabel} V2H` : cleanLabel;
                                    val = Math.abs(val);
                                }
                                return ` ${cleanLabel}: ${val} ${unit}`;
                            } } },
                            zoom: {
                                pan: { enabled: true, mode: 'x' },
                                zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' }
                            }
                        }, scales: { x: { grid: { color: gridColor }, ticks: { maxTicksLimit: 12, color: textColor } }, ...yAxes }
                    }
                });
                const canvas = document.getElementById('liveChartCanvas');
                if (canvas) canvas.ondblclick = () => { if (liveLineChart) liveLineChart.resetZoom(); };
            }

            scheduleJsChartAutoRefresh('live', requestGeneration, () => {
                const container = document.getElementById('liveChartContainer');
                if (container && container.style.display === 'block') loadJsLiveChart(currentLiveHours);
            });
        });
}

// Die Prognose verwendet ein eigenes rollendes 72-Stunden-Zeitfenster.
// Ihre Achse darf deshalb keine kürzeren Datums-Closures eines zuvor
// angezeigten Hybrid-Diagramms übernehmen.
function buildForecastChartTimeContext(data, gridColor, textColor, isDarkMode) {
    const sourceLabels = Array.isArray(data && data.labels) ? data.labels : [];
    const timestamps = Array.isArray(data && data.timestamps) ? data.timestamps : [];
    return buildCompactChartTimeContext(timestamps, sourceLabels, gridColor, textColor, isDarkMode, 8);
}

function renderForecastKwhSummary(data = {}) {
    const forecastBar = document.getElementById('forecast-kwh-summary');
    if (!forecastBar) return false;

    // Jeder neue Forecast-Vertrag ersetzt den bisherigen DOM-Stand. Fehlt die
    // Tagessumme, darf kein älterer Wert sichtbar bleiben.
    forecastBar.innerHTML = '';
    forecastBar.style.display = 'none';
    const summary = data && typeof data.daily_summary === 'object'
        ? data.daily_summary
        : null;
    if (!summary) return false;

    const fmt = (value) => {
        const number = parseFloat(value);
        return Number.isFinite(number) ? number.toFixed(1) : '?';
    };
    const hasWpData = (summary.tomorrow && summary.tomorrow.wp_kwh > 0)
        || (summary.day_after && summary.day_after.wp_kwh > 0);
    const hasClimateData = (summary.tomorrow && summary.tomorrow.climate_kwh > 0)
        || (summary.day_after && summary.day_after.climate_kwh > 0);
    const summaryValue = (icon, title, value) =>
        `<span class="forecast-summary-value" title="${title}">${icon} <b>${fmt(value)}</b> kWh</span>`;
    const dayBlock = (key, label, day, consumptionLabel) => {
        if (!day || typeof day !== 'object') return '';
        const consumption = [summaryValue('🏠', consumptionLabel, day.home_kwh)];
        if (day.wp_kwh > 0 || (hasWpData && document.getElementById('val-wp'))) {
            consumption.push(summaryValue('🔥', `${label}: Wärmepumpe`, day.wp_kwh));
        }
        if (day.climate_kwh > 0 || hasClimateData) {
            consumption.push(summaryValue('❄', `${label}: Klima`, day.climate_kwh));
        }
        return `<span class="forecast-summary-day forecast-summary-${key}">
            <span class="forecast-summary-label text-secondary fw-normal">${label}</span>
            <span class="forecast-summary-lines">
                <span class="forecast-summary-line forecast-summary-yield">${summaryValue('☀', `${label}: PV-Ertrag`, day.pv_kwh)}</span>
                <span class="forecast-summary-line forecast-summary-consumption">${consumption.join('')}</span>
            </span>
        </span>`;
    };
    const html = [
        dayBlock('today', 'Heute', summary.today, 'Heute: Restprognose Verbrauch ab jetzt'),
        dayBlock('tomorrow', 'Morgen', summary.tomorrow, 'Morgen: Verbrauch'),
        dayBlock('day-after', 'Übermorgen', summary.day_after, 'Übermorgen: Verbrauch')
    ].filter(Boolean).join('');
    if (!html) return false;

    forecastBar.innerHTML = html;
    forecastBar.style.display = '';
    return true;
}

function loadJsForecastChart(file = '') {
    const requestGeneration = beginJsChartRequest('forecast');
    const url = file ? 'get_forecast_data.php?file=' + encodeURIComponent(file) : 'get_forecast_data.php';
    renderForecastKwhSummary({});

    fetch(url)
        .then(r => r.json())
        .then(data => {
            if (!isCurrentJsChartRequest('forecast', requestGeneration)) return;
            renderForecastKwhSummary(data);
            const directMarketingView = renderDirectMarketingForecastChart(data, {exclusive: true});
            updateForecastProjectionStatus(data, directMarketingView);
            if (data.error || !data.labels || data.labels.length === 0) return;





            // SoC kommt aus der physikalischen Batterie-Bilanz der Prognose.
            // Keine nachträgliche Wert-Glättung, sonst kann die Linie vor
            // echter Batterieladung ansteigen.

            const isDarkMode = typeof DARK_MODE !== 'undefined' ? DARK_MODE : true;
            const textColor = isDarkMode ? '#aaa' : '#666';
            const gridColor = isDarkMode ? '#333' : '#e9ecef';
            const forecastTimeContext = buildForecastChartTimeContext(data, gridColor, textColor, isDarkMode);

            const mapFlip = (arr) => chartFlipNegatives && arr ? arr.map(Math.abs) : arr;
            const dashIfNeg = (arr) => (ctx) => chartFlipNegatives && arr && (arr[ctx.p0DataIndex] < 0 || arr[ctx.p1DataIndex] < 0) ? [4, 4] : undefined;
            const predumpHeadroomW = Array.isArray(data.predump_w)
                ? data.predump_w.map(v => Math.max(0, parseFloat(v) || 0))
                : [];
            const predumpCandidateW = Array.isArray(data.predump_candidate_w)
                ? data.predump_candidate_w.map(v => Math.max(0, parseFloat(v) || 0))
                : [];
            const forecastSocCurrent = !data.storage_projection_status
                || data.storage_projection_status.soc_curve_current !== false;
            const directMarketingSoc = directMarketingSocProjectionForTimestamps(
                directMarketingView,
                data.timestamps || []
            );
            const useDirectMarketingSoc = Array.isArray(directMarketingSoc);
            const currentSoc = currentSocMarkerForTimestamps(data.timestamps || []);
            let datasets = [
                { label: 'Sonne (PV)', data: data.pv, borderColor: getFlowColor('pv', '#ffc107'), backgroundColor: flowColorAlpha('pv', 0.15, '#ffc107'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 },
                { label: 'Hausverbrauch', data: data.home, borderColor: getFlowColor('home', '#0dcaf0'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 },
                { label: 'Batterie', data: mapFlip(data.bat), borderColor: getFlowColor('battery', '#198754'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.bat) }, order: 10 },
                { label: 'Netz', data: mapFlip(data.grid), borderColor: getFlowColor('grid', '#6c757d'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.grid) }, order: 10 }
            ];
            pushPredumpCandidateDataset(datasets, predumpCandidateW);
            pushPredumpHeadroomDataset(datasets, predumpHeadroomW);
            datasets.push({
                label: useDirectMarketingSoc
                    ? 'DV-SoC-Prognose (%)'
                    : (forecastSocCurrent
                        ? 'Standard-SoC-Prognose (%)'
                        : 'SoC-Planung (nicht aktuell) (%)'),
                data: useDirectMarketingSoc ? directMarketingSoc : data.soc,
                borderColor: useDirectMarketingSoc ? '#8b5cf6' : '#20c997',
                backgroundColor: useDirectMarketingSoc
                    ? 'rgba(139,92,246,0.08)'
                    : 'rgba(32,201,151,0.08)',
                tension: useDirectMarketingSoc ? 0 : 0.45,
                cubicInterpolationMode: useDirectMarketingSoc ? undefined : 'monotone',
                stepped: useDirectMarketingSoc ? 'after' : false,
                pointRadius: 0,
                borderWidth: useDirectMarketingSoc ? 2.5 : 2,
                borderDash: useDirectMarketingSoc || forecastSocCurrent ? undefined : [5, 5],
                yAxisID: 'y1',
                order: 5
            });
            if (currentSoc.index >= 0) {
                datasets.push({
                    label: 'Aktueller SoC (Messwert)',
                    data: currentSoc.data,
                    showLine: false,
                    borderColor: '#22c55e',
                    backgroundColor: '#22c55e',
                    pointRadius: 6,
                    pointHoverRadius: 8,
                    pointStyle: 'circle',
                    yAxisID: 'y1',
                    order: 0
                });
            }
            pushStorageTargetCurveDataset(datasets, data.storage_target_curve);
            pushMarketChargeDataset(datasets, data.market_charge);

            if (data.wb && data.wb.length > 0 && Math.max(...data.wb.map(Math.abs)) > 0) datasets.push({ label: 'Wallbox 1', data: mapFlip(data.wb), borderColor: getFlowColor('wallbox', '#2ecc71'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.wb) }, order: 10 });
            if (data.wb2 && data.wb2.length > 0 && Math.max(...data.wb2.map(Math.abs)) > 0) datasets.push({ label: 'Wallbox 2', data: mapFlip(data.wb2), borderColor: getFlowColor('wallbox2', '#34d399'), borderDash: [2, 3], tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNeg(data.wb2) }, order: 10 });
            if (data.wp && data.wp.length > 0 && Math.max(...data.wp) > 0) datasets.push({ label: 'Wärmepumpe', data: data.wp, borderColor: getFlowColor('heatpump', '#f97316'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 });
            if (data.climate && data.climate.length > 0 && Math.max(...data.climate) > 0) datasets.push({ label: 'Klima', data: data.climate, borderColor: getFlowColor('climate', '#38bdf8'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', order: 10 });
            if (data.dv_grid && data.dv_grid.length > 0 && Math.max(...data.dv_grid) > 0) datasets.push({ label: 'Direktvermarktung (Verkauf)', data: data.dv_grid, backgroundColor: 'rgba(16, 185, 129, 0.6)', borderColor: '#10b981', type: 'bar', borderWidth: 1, yAxisID: 'y', order: 0 });

            let yAxes = {
                y: { type: 'linear', display: true, position: 'left', grid: { color: gridColor }, ticks: { color: textColor } },
                y1: { type: 'linear', display: true, position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: textColor } }
            };

            if (pushElectricityPriceDataset(datasets, data.price, 'y2', 'Strompreis')) {
                yAxes['y2'] = { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { color: textColor } };
            }

            datasets = applyHiddenState(datasets);
            const forecastTooltipFilter = function(item) {
                if (item.dataset && item.dataset.label && item.dataset.label.includes('__HIDDEN__')) return false;
                return true;
            };
            const forecastTooltipLabel = (ctx) => {
                let unit = 'W'; let l = ctx.dataset.label;
                if (l && l.includes('__HIDDEN__')) return '';
                if (l.includes('(%)') || l.includes('SoC')) unit = '%'; else if (l.toLowerCase().includes('preis')) unit = 'ct/kWh';
                let cleanLabel = l.replace(/\s\([^)]+\)/, '');

                let origVal = chartRawY(ctx);
                if (chartFlipNegatives) {
                    if (l === 'Batterie') origVal = data.bat[ctx.dataIndex];
                    else if (l === 'Wallbox' || l === 'Wallbox 1') origVal = data.wb[ctx.dataIndex];
                    else if (l === 'Wallbox 2') origVal = data.wb2[ctx.dataIndex];
                    else if (l === 'Netz') origVal = data.grid[ctx.dataIndex];
                }

                let val = chartRawY(ctx);
                if (cleanLabel === 'Batterie' || cleanLabel === 'Batterie Leistung') {
                    cleanLabel = origVal > 0 ? 'Laden' : (origVal < 0 ? 'Entladen' : 'Batterie');
                    val = Math.abs(val);
                } else if (cleanLabel === 'Netz' || cleanLabel === 'Netz Gesamt') {
                    cleanLabel = origVal > 0 ? 'Netzbezug' : (origVal < 0 ? 'Einspeisung' : 'Netz');
                    val = Math.abs(val);
                } else if (cleanLabel === 'Wallbox' || cleanLabel === 'Wallbox 1' || cleanLabel === 'Wallbox 2' || cleanLabel === 'Wallbox Gesamt') {
                    cleanLabel = origVal < -50 ? `${cleanLabel} V2H` : cleanLabel;
                    val = Math.abs(val);
                }
                return ` ${cleanLabel}: ${val} ${unit}`;
            };

            if (liveLineChart) {
                liveLineChart.resetZoom();
                liveLineChart.options.plugins.legend.display = true;
                liveLineChart.data.labels = forecastTimeContext.labels; liveLineChart.data.datasets = datasets;
                // Alle zeitabhängigen Optionen ersetzen, damit die Prognose
                // weder Achse noch Tooltip-Callbacks des Hybrid-Charts erbt.
                liveLineChart.options.plugins.tooltip = {
                    ...(liveLineChart.options.plugins.tooltip || {}),
                    filter: forecastTooltipFilter,
                    callbacks: {
                        title: forecastTimeContext.tooltipTitle,
                        label: forecastTooltipLabel
                    }
                };
                liveLineChart.options.scales = { x: forecastTimeContext.xScale, ...yAxes };
                liveLineChart.update('none');
            } else {
                const ctx = document.getElementById('liveChartCanvas').getContext('2d');
                liveLineChart = new Chart(ctx, {
                    type: 'line', data: { labels: forecastTimeContext.labels, datasets: datasets },
                    options: {
                        responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
                        plugins: {
                            legend: {
                                position: 'top',
                                labels: {
                                    usePointStyle: true, boxWidth: 8, padding: 15, color: textColor,
                                    filter: function(item) { return item.text && !item.text.includes('__HIDDEN__'); }
                                },
                                onClick: function(e, legendItem, legend) {
                                    const index = legendItem.datasetIndex;
                                    const ci = legend.chart;
                                    const isHidden = ci.isDatasetVisible(index);
                                    if (isHidden) ci.hide(index); else ci.show(index);
                                    legendItem.hidden = isHidden;
                                    saveHiddenDataset(legendItem.text, isHidden);
                                }
                            },
                            tooltip: {
                                filter: forecastTooltipFilter,
                                callbacks: {
                                    title: forecastTimeContext.tooltipTitle,
                                    label: forecastTooltipLabel
                                }
                            },
                            zoom: {
                                pan: { enabled: true, mode: 'x' },
                                zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' }
                            }
                        }, scales: { x: forecastTimeContext.xScale, ...yAxes }
                    }
                });
                const canvas = document.getElementById('liveChartCanvas');
                if (canvas) canvas.ondblclick = () => { if (liveLineChart) liveLineChart.resetZoom(); };
            }
        })
        .catch(err => {
            if (isCurrentJsChartRequest('forecast', requestGeneration)) {
                console.error("Fehler beim Laden des Prognose-Diagramms:", err);
            }
        });
}


function loadJsHybridChart(hours, file = null) {
    const requestGeneration = beginJsChartRequest('hybrid');
    currentLiveHours = hours;

    // Hälfte der Zeit für Vergangenheit, Hälfte für Zukunft
    const pastHours = hours / 2;
    const futureHours = hours / 2;

    // Hilfsfunktionen für Zeitrechnung
    const parseClockMins = (label) => {
        const match = String(label || '').match(/^(\d{1,2}):(\d{2})/);
        if (!match) return null;
        const h = Number(match[1]);
        const m = Number(match[2]);
        if (!Number.isFinite(h) || !Number.isFinite(m)) return null;
        return (((h % 24) + 24) % 24) * 60 + Math.max(0, Math.min(59, m));
    };
    const liftClockMins = (clockMins, refMins) => {
        let mins = clockMins;
        if (refMins !== null && refMins !== undefined && Number.isFinite(refMins)) {
            mins = clockMins + Math.floor(refMins / 1440) * 1440;
            while (mins < refMins - 60) mins += 1440;
            while (mins > refMins + 36 * 60) mins -= 1440;
        }
        return mins;
    };
    const fmtTime = (totalMins) => {
        const m = ((totalMins % 1440) + 1440) % 1440;
        return `${String(Math.floor(m / 60)).padStart(2,'0')}:${String(m % 60).padStart(2,'0')}`;
    };
    const hybridDateBase = new Date();
    hybridDateBase.setHours(0, 0, 0, 0);
    const hybridDateFormatter = new Intl.DateTimeFormat('de-DE', { weekday: 'short', day: '2-digit', month: '2-digit' });
    const formatHybridDatePart = (totalMins) => {
        if (totalMins === null || totalMins === undefined || !Number.isFinite(totalMins)) return '';
        const date = new Date(hybridDateBase.getTime() + Math.round(totalMins) * 60000);
        return hybridDateFormatter.format(date);
    };

    let urlLive = 'get_chart_data.php?hours=' + pastHours;
    if (file) urlLive += '&file=' + encodeURIComponent(file);

    let urlFore = 'get_forecast_data.php';
    if (file) urlFore += '?file=' + encodeURIComponent(file);

    Promise.all([
        fetch(urlLive).then(r => r.json()),
        fetch(urlFore).then(r => r.json())
    ]).then(([data, forecastData]) => {
        if (!isCurrentJsChartRequest('hybrid', requestGeneration)) return;
        if (data.error) return;
        updateForecastProjectionStatus(forecastData);
        updatePvForecastDiagnostics(forecastData);
        renderDirectMarketingForecastChart(forecastData);
        if (forecastData.error || !forecastData.labels) forecastData = { labels: [], pv: [], home: [], bat: [], grid: [], soc: [] };

        const isDarkMode = typeof DARK_MODE !== 'undefined' ? DARK_MODE : true;
        const textColor = isDarkMode ? '#aaa' : '#666';
        const gridColor = isDarkMode ? '#333' : '#e9ecef';
        const mapFlip = (arr) => chartFlipNegatives && arr ? arr.map(Math.abs) : arr;

        // --- Live-Daten auf 15-Minuten-Buckets downsampeln ---
        // Damit beide Chart-Hälften gleiche Punkt-Dichte haben und "Jetzt" in der Mitte liegt
        const SLOT = 15;
        const rawLabels = data.labels || [];
        const hasWb1 = !!(data.wb && data.wb.length > 0);
        const hasWb2 = !!(data.wb2 && data.wb2.length > 0);
        const hasWp = !!(data.wp && data.wp.length > 0);
        const hasHs = !!(data.hs && data.hs.length > 0);
        const hasLiveClimate = !!(data.climate && data.climate.length > 0);
        const hasForecastClimate = !!(forecastData.climate && forecastData.climate.some(v => v !== null && v !== undefined));
        const hasClimate = hasLiveClimate || hasForecastClimate;
        const hasPrice = !!(data.price);
        const hasDvGrid = !!(data.dv_grid);

        const rawAbsMins = [];
        let prevLiveAbsMins = null;
        rawLabels.forEach((lbl, i) => {
            const clockMins = parseClockMins(lbl);
            if (clockMins === null) {
                rawAbsMins[i] = null;
                return;
            }
            const absMins = liftClockMins(clockMins, prevLiveAbsMins);
            rawAbsMins[i] = absMins;
            prevLiveAbsMins = absMins;
        });

        const buckets = {};
        rawLabels.forEach((lbl, i) => {
            const mins = rawAbsMins[i];
            if (mins === null || !Number.isFinite(mins)) return;
            const slotKey = Math.floor(mins / SLOT) * SLOT;
            if (!buckets[slotKey]) buckets[slotKey] = { count: 0 };
            const b = buckets[slotKey];
            const add = (k, v) => { if (v !== null && v !== undefined) b[k] = (b[k] || 0) + v; };
            add('pv', data.pv[i]); add('home', data.home[i]); add('bat', data.bat[i]);
            add('grid', data.grid[i]); add('soc', data.soc[i]);
            if (hasWb1) add('wb', data.wb[i]);
            if (hasWb2) add('wb2', data.wb2[i]);
            if (hasWp) add('wp', data.wp[i]);
            if (hasHs) add('hs', data.hs[i]);
            if (hasLiveClimate) add('climate', data.climate[i]);
            if (hasPrice) add('price', data.price[i]);
            if (hasDvGrid) add('dv_grid', data.dv_grid[i]);
            b.count++;
        });

        const slotKeys = Object.keys(buckets).map(Number).sort((a, b) => a - b);

        // Finde Startpunkt für Prognose (Nahtloser Übergang ab letztem Live-Slot)
        const lastLiveMins = slotKeys.length > 0 ? slotKeys[slotKeys.length - 1] : null;
        const forecastAbsMins = [];
        let prevForecastMins = null;
        for (let i = 0; i < forecastData.labels.length; i++) {
            const clockMins = parseClockMins(forecastData.labels[i]);
            if (clockMins === null) {
                forecastAbsMins[i] = null;
                continue;
            }
            const refMins = prevForecastMins !== null ? prevForecastMins : lastLiveMins;
            const absMins = liftClockMins(clockMins, refMins);
            forecastAbsMins[i] = absMins;
            prevForecastMins = absMins;
        }

        let startIndex = file ? 0 : forecastData.labels.length;
        if (!file && lastLiveMins !== null) {
            for (let i = 0; i < forecastAbsMins.length; i++) {
                const fMins = forecastAbsMins[i];
                if (fMins !== null && fMins > lastLiveMins) {
                    startIndex = i;
                    break;
                }
            }
        }

        let labels = [], pv = [], home = [], bat = [], grid = [], soc = [], storageTargetCurve = [], marketCharge = [], predumpHeadroomW = [], predumpCandidateW = [], wb = [], wb2 = [], wp = [], hs = [], climate = [], price = [], dv_grid = [];
        const labelDateParts = [];
        const labelDateTimes = [];
        const daySeparatorIndices = new Set();
        const rememberHybridLabelDate = (totalMins, fallbackLabel = '') => {
            const idx = labels.length - 1;
            const timeLabel = labels[idx] || fallbackLabel || '';
            const datePart = formatHybridDatePart(totalMins);
            const previousDate = labelDateParts.length ? labelDateParts[labelDateParts.length - 1] : '';
            if (datePart && previousDate && previousDate !== datePart) daySeparatorIndices.add(idx);
            labelDateParts.push(datePart);
            labelDateTimes.push(datePart ? `${datePart} ${timeLabel}` : timeLabel);
        };
        let pv_m1 = [], pv_m2 = [], pv_m3 = [], pv_ensemble = [];
        const forecastByAbsSlot = {};
        forecastAbsMins.forEach((absMins, i) => {
            if (absMins === null || !Number.isFinite(absMins)) return;
            forecastByAbsSlot[Math.floor(absMins / SLOT) * SLOT] = i;
        });

        slotKeys.forEach(slotMin => {
            const b = buckets[slotMin], n = b.count || 1;
            const fmstr = fmtTime(slotMin);
            labels.push(fmstr);
            rememberHybridLabelDate(slotMin, fmstr);
            pv.push(Math.round((b.pv || 0) / n));
            home.push(Math.round((b.home || 0) / n));
            bat.push(Math.round((b.bat || 0) / n));
            grid.push(Math.round((b.grid || 0) / n));
            soc.push(+((b.soc || 0) / n).toFixed(1));
            storageTargetCurve.push(null);
            marketCharge.push(0);
            predumpHeadroomW.push(0);
            predumpCandidateW.push(0);
            if (hasWb1) wb.push(Math.round((b.wb || 0) / n));
            if (hasWb2) wb2.push(Math.round((b.wb2 || 0) / n));
            if (hasWp) wp.push(Math.round((b.wp || 0) / n));
            if (hasHs) hs.push(Math.round((b.hs || 0) / n));
            if (hasClimate) climate.push(Math.round((b.climate || 0) / n));
            if (hasPrice) price.push(b.price !== undefined ? +((b.price / n).toFixed(2)) : null);
            dv_grid.push(Math.round((b.dv_grid || 0) / n));

            // History (Live-Daten) mit vergangenen Wetter-Prognosen auffüllen
            let m1 = null, m2 = null, m3 = null, ens = null;
            if (forecastData && forecastData.labels) {
                // Nur exakt passende absolute Slots verwenden; HH:MM allein kollidiert über Mitternacht.
                const j = forecastByAbsSlot[slotMin];
                if (j !== undefined) {
                    m1 = forecastData.pv_m1 ? forecastData.pv_m1[j] : null;
                    m2 = forecastData.pv_m2 ? forecastData.pv_m2[j] : null;
                    m3 = forecastData.pv_m3 ? forecastData.pv_m3[j] : null;
                    ens = forecastData.pv_ensemble ? forecastData.pv_ensemble[j] : null;
                }
            }
            pv_m1.push(m1); pv_m2.push(m2); pv_m3.push(m3); pv_ensemble.push(ens);
        });
        if (!hasPrice) price = new Array(labels.length).fill(null);

        const historyLength = labels.length;

        // Prognose auf futureHours begrenzen
        const lastMins = lastLiveMins !== null ? lastLiveMins : 0;
        const endMinutes = file ? Infinity : lastMins + futureHours * 60;

        for (let i = startIndex; i < forecastData.labels.length; i++) {
            const fMins = forecastAbsMins[i];
            if (!file && forecastData.labels[i]) {
                if (fMins === null || !Number.isFinite(fMins)) continue;
                if (fMins > endMinutes) break;
            }
            labels.push((fMins !== null && Number.isFinite(fMins)) ? fmtTime(fMins) : forecastData.labels[i]);
            rememberHybridLabelDate(fMins, forecastData.labels[i]);
            pv.push(forecastData.pv[i] || 0);
            home.push(forecastData.home[i] || 0);
            bat.push(forecastData.bat[i] || 0);
            grid.push(forecastData.grid[i] || 0);
            soc.push(forecastData.soc[i] || 0);
            storageTargetCurve.push(forecastData.storage_target_curve ? (forecastData.storage_target_curve[i] ?? null) : null);
            marketCharge.push(forecastData.market_charge ? (forecastData.market_charge[i] || 0) : 0);
            predumpHeadroomW.push(forecastData.predump_w ? Math.max(0, parseFloat(forecastData.predump_w[i]) || 0) : 0);
            predumpCandidateW.push(forecastData.predump_candidate_w ? Math.max(0, parseFloat(forecastData.predump_candidate_w[i]) || 0) : 0);
            if (hasWb1) wb.push(forecastData.wb ? (forecastData.wb[i] || 0) : 0);
            if (hasWb2) wb2.push(forecastData.wb2 ? (forecastData.wb2[i] || 0) : 0);
            if (hasWp && forecastData.wp) wp.push(forecastData.wp[i] || 0);
            if (hasHs) hs.push(0);
            if (hasClimate) climate.push(forecastData.climate ? (forecastData.climate[i] || 0) : 0);
            if (hasPrice) price.push(forecastData.price ? (forecastData.price[i] ?? null) : null);
            dv_grid.push(forecastData.dv_grid ? (forecastData.dv_grid[i] || 0) : 0);

            pv_m1.push(forecastData.pv_m1 ? (forecastData.pv_m1[i] ?? null) : null);
            pv_m2.push(forecastData.pv_m2 ? (forecastData.pv_m2[i] ?? null) : null);
            pv_m3.push(forecastData.pv_m3 ? (forecastData.pv_m3[i] ?? null) : null);
            pv_ensemble.push(forecastData.pv_ensemble ? (forecastData.pv_ensemble[i] ?? null) : null);
        }

        // Forecast-Teil weich anzeigen; der Live-Anteil bleibt unverändert.
        // SoC-Prognose nicht nachträglich glätten: sie folgt der Batterie-Bilanz.

        const dashIfNegOrFore = (arr, baseDash = undefined) => (ctx) => {
                if (ctx.p0DataIndex === undefined) return baseDash;
            const isFore = ctx.p0DataIndex >= historyLength - 1;
            const isNeg = chartFlipNegatives && arr && (arr[ctx.p0DataIndex] < 0 || arr[ctx.p1DataIndex] < 0);
            if (isFore && isNeg) return [5, 5];
            if (isFore) return [5, 5];
            if (isNeg) return [2, 2];
            return baseDash;
        };

        let datasets = [];
        if (dv_grid.length > 0 && Math.max(...dv_grid) > 0) {
            datasets.push({ label: 'Direktvermarktung (Verkauf)', data: dv_grid, backgroundColor: 'rgba(16, 185, 129, 0.6)', borderColor: '#10b981', type: 'bar', borderWidth: 1, yAxisID: 'y', order: 0 });
        }
        const forecastSocCurrent = !forecastData.storage_projection_status
            || forecastData.storage_projection_status.soc_curve_current !== false;
        pushPredumpCandidateDataset(datasets, predumpCandidateW, { borderDash: dashIfNegOrFore(null, [4, 4]) });
        pushPredumpHeadroomDataset(datasets, predumpHeadroomW, { borderDash: dashIfNegOrFore(null) });

        datasets.push(
            { label: 'Sonne (PV)', data: pv, borderColor: getFlowColor('pv', '#ffc107'), backgroundColor: flowColorAlpha('pv', 0.15, '#ffc107'), fill: true, tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(null) }, order: 10 }
        );

        // --- NEU: PEAK SHAVING OVERLAY ---
        if (typeof SHOW_PEAK_SHAVING !== 'undefined' && SHOW_PEAK_SHAVING && typeof E3DC_LIMITS !== 'undefined' && E3DC_LIMITS.einspeise > 0) {
            let limitLineData = pv.map((_, i) => {
                let h = home[i] || 0;
                let w = wp[i] || 0;
                let c = climate[i] || 0;
                let maxAc = h + w + c + E3DC_LIMITS.einspeise;
                let rawLimit = E3DC_LIMITS.wr > 0 ? Math.min(E3DC_LIMITS.wr, maxAc) : maxAc;
                return Math.min(rawLimit, pv[i]);
            });

            let kuppeData = [...pv];
            let gridLimit = pv.map((_, i) => {
                let h = home[i] || 0;
                let w = wp[i] || 0;
                let c = climate[i] || 0;
                let b = (wb[i] || 0) + (wb2[i] || 0);
                let gl = E3DC_LIMITS.einspeise + h + w + c + Math.abs(b);
                return chartFlipNegatives ? gl : -gl;
            });

            datasets.push({ label: '__HIDDEN__Abregel-Limit', data: limitLineData, showLine: false, borderColor: 'rgba(32, 201, 151, 0)', backgroundColor: 'rgba(32, 201, 151, 0)', borderWidth: 0, pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, hoverBorderWidth: 0, fill: false, tension: 0.3, yAxisID: 'y', order: 9 });
            datasets.push({ label: 'Peak-Ersparnis', data: kuppeData, showLine: false, borderColor: 'rgba(32, 201, 151, 0)', fill: { target: '-1', above: 'rgba(32, 201, 151, 0.7)', below: 'rgba(32, 201, 151, 0)' }, tension: 0.3, pointRadius: 0, pointHoverRadius: 0, pointHitRadius: 0, borderWidth: 0, yAxisID: 'y', order: 8 });
            datasets.push({ label: 'Netzeinspeise-Limit', data: gridLimit, borderColor: 'rgba(255, 0, 0, 0.5)', borderDash: [5, 5], fill: false, tension: 0.3, pointRadius: 0, borderWidth: 1, yAxisID: 'y', order: 11 });
        }

        datasets.push(
            { label: 'Hausverbrauch', data: home, borderColor: getFlowColor('home', '#0dcaf0'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(null) }, order: 10 },
            { label: 'Batterie', data: mapFlip(bat), borderColor: getFlowColor('battery', '#198754'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(bat) }, order: 10 },
            { label: 'Netz', data: mapFlip(grid), borderColor: getFlowColor('grid', '#6c757d'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(grid) }, order: 10 }
        );
        datasets.push({
            label: forecastSocCurrent
                ? 'Standard-SoC-Prognose (%)'
                : 'SoC-Planung (nicht aktuell) (%)',
            data: soc,
            borderColor: '#20c997',
            backgroundColor: 'rgba(32,201,151,0.08)',
            tension: 0.45,
            cubicInterpolationMode: 'monotone',
            stepped: false,
            pointRadius: 0,
            borderWidth: 2,
            yAxisID: 'y1',
            segment: { borderDash: dashIfNegOrFore(null, forecastSocCurrent ? undefined : [5, 5]) },
            order: 4
        });
        const hybridCurrentSoc = file ? null : currentLiveSocForChart();
        if (hybridCurrentSoc !== null && labels.length > 0) {
            const markerIndex = Math.max(0, Math.min(labels.length - 1, historyLength > 0 ? historyLength - 1 : 0));
            const markerData = labels.map((label, index) => index === markerIndex ? hybridCurrentSoc : null);
            datasets.push({
                label: 'Aktueller SoC (Messwert)',
                data: markerData,
                showLine: false,
                borderColor: '#22c55e',
                backgroundColor: '#22c55e',
                pointRadius: 6,
                pointHoverRadius: 8,
                pointStyle: 'circle',
                yAxisID: 'y1',
                order: 0
            });
        }
        pushStorageTargetCurveDataset(datasets, storageTargetCurve, { borderDash: dashIfNegOrFore(null, [6, 4]) });
        pushMarketChargeDataset(datasets, marketCharge, { borderDash: dashIfNegOrFore(null, [6, 4]) });

        if (wb.length > 0 && Math.max(...wb.map(Math.abs)) > 0) datasets.push({ label: 'Wallbox 1', data: mapFlip(wb), borderColor: getFlowColor('wallbox', '#2ecc71'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(wb) }, order: 10 });
        if (wb2.length > 0 && Math.max(...wb2.map(Math.abs)) > 0) datasets.push({ label: 'Wallbox 2', data: mapFlip(wb2), borderColor: getFlowColor('wallbox2', '#34d399'), borderDash: [2, 3], tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(wb2, [2, 3]) }, order: 10 });
        if (hs.length > 0 && Math.max(...hs) > 0) datasets.push({ label: 'Heizstab', data: hs, borderColor: getFlowColor('heater', '#fd7e14'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(null) }, order: 10 });
        if (wp.length > 0 && Math.max(...wp) > 0) datasets.push({ label: 'Wärmepumpe', data: wp, borderColor: getFlowColor('heatpump', '#f97316'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(null) }, order: 10 });
        if (climate.length > 0 && Math.max(...climate) > 0) datasets.push({ label: 'Klima', data: climate, borderColor: getFlowColor('climate', '#38bdf8'), tension: 0.3, pointRadius: 0, borderWidth: 2, yAxisID: 'y', segment: { borderDash: dashIfNegOrFore(null) }, order: 10 });

        let yAxes = {
            y: { type: 'linear', display: true, position: 'left', grid: { color: gridColor }, ticks: { color: textColor } },
            y1: { type: 'linear', display: true, position: 'right', min: 0, max: 100, grid: { drawOnChartArea: false }, ticks: { color: textColor } }
        };

        const hasPriceAxis = pushElectricityPriceDataset(datasets, price, 'y2', 'Strompreis');
        const hasMarketWindowPrice = pushElectricityPriceDataset(
            datasets,
            directMarketingMarketPrice,
            'y2',
            'DV Marktpreis (Verkauf)',
            {borderColor: '#0ea5e9', borderWidth: 3, order: 9}
        );
        const hasMarketWindowNetSell = pushElectricityPriceDataset(
            datasets,
            directMarketingMarketNetSell,
            'y2',
            'DV Netto-Verkaufspreis',
            {borderColor: '#22c55e', borderWidth: 2.5, order: 8}
        );
        if (hasPriceAxis || hasMarketWindowPrice || hasMarketWindowNetSell) {
            yAxes['y2'] = { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false }, ticks: { color: textColor } };
        }

        datasets = applyHiddenState(datasets);
        const hybridTooltipTitle = (items) => {
            if (!items || !items.length) return '';
            const idx = items[0].dataIndex;
            return labelDateTimes[idx] || labels[idx] || '';
        };
        const buildHybridXScale = () => ({
            grid: {
                color: (ctx) => daySeparatorIndices.has(ctx.index) ? (isDarkMode ? 'rgba(148, 163, 184, 0.65)' : 'rgba(100, 116, 139, 0.5)') : gridColor,
                lineWidth: (ctx) => daySeparatorIndices.has(ctx.index) ? 2 : 1
            },
            ticks: {
                maxRotation: 0,
                autoSkip: true,
                maxTicksLimit: 8,
                color: textColor,
                callback: function(value, index) {
                    const label = this.getLabelForValue ? this.getLabelForValue(value) : (labels[index] || value);
                    const datePart = labelDateParts[index] || '';
                    if (datePart && (index === 0 || daySeparatorIndices.has(index))) return [datePart, label];
                    return label;
                }
            }
        });

        if (liveLineChart) {
            liveLineChart.resetZoom();
            liveLineChart.options.plugins.legend.display = true;
            liveLineChart.data.labels = labels; liveLineChart.data.datasets = datasets;
            if (liveLineChart.options.plugins.tooltip && liveLineChart.options.plugins.tooltip.callbacks) {
                liveLineChart.options.plugins.tooltip.callbacks.title = hybridTooltipTitle;
            }
            liveLineChart.options.scales = { x: buildHybridXScale(), ...yAxes };
            liveLineChart.update('none');
        } else {
            const ctx = document.getElementById('liveChartCanvas').getContext('2d');
            liveLineChart = new Chart(ctx, {
                type: 'line', data: { labels: labels, datasets: datasets },
                options: {
                    responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false },
                    plugins: {
                        legend: {
                            position: 'top',
                            labels: {
                                usePointStyle: true, boxWidth: 8, padding: 15, color: textColor,
                                filter: function(item, chart) { return item.text && !item.text.includes('__HIDDEN__'); }
                            },
                            onClick: function(e, legendItem, legend) {
                                const index = legendItem.datasetIndex;
                                const ci = legend.chart;
                                const isHidden = ci.isDatasetVisible(index);
                                if (isHidden) ci.hide(index); else ci.show(index);
                                legendItem.hidden = isHidden;
                                saveHiddenDataset(legendItem.text, isHidden);
                            }
                        },
                            tooltip: {
                            filter: function(item) {
                                if (item.dataset && item.dataset.label && item.dataset.label.includes('__HIDDEN__')) return false;
                                if (item.dataset && item.dataset.label === 'Peak-Ersparnis') {
                                    let limitIdx = item.chart.data.datasets.findIndex(d => d.label === '__HIDDEN__Abregel-Limit');
                                    if (limitIdx >= 0) {
                                        let limitVal = item.chart.data.datasets[limitIdx].data[item.dataIndex];
                                            if (chartRawY(item) <= limitVal) return false;
                                    }
                                }
                                return true;
                            },
                            callbacks: { title: hybridTooltipTitle, label: (ctx) => {
                            let unit = 'W'; let l = ctx.dataset.label;
                            if (l && l.includes('__HIDDEN__')) return '';
                            if (l.includes('(%)') || l.includes('SoC')) unit = '%'; else if (l.toLowerCase().includes('preis')) unit = 'ct/kWh';
                            let cleanLabel = l.replace(/\s\([^)]+\)/, '');

                            let origVal = chartRawY(ctx);
                            if (chartFlipNegatives) {
                                if (l === 'Batterie') origVal = bat[ctx.dataIndex];
                                    else if (l === 'Wallbox' || l === 'Wallbox 1') origVal = wb[ctx.dataIndex];
                                    else if (l === 'Wallbox 2') origVal = wb2[ctx.dataIndex];
                                else if (l === 'Netz') origVal = grid[ctx.dataIndex];
                            }

                            let val = chartRawY(ctx);
                            if (cleanLabel === 'Peak-Ersparnis') {
                                let limitIdx = ctx.chart.data.datasets.findIndex(d => d.label === '__HIDDEN__Abregel-Limit');
                                if (limitIdx >= 0) {
                                    val = Math.round(val - ctx.chart.data.datasets[limitIdx].data[ctx.dataIndex]);
                                }
                            } else if (cleanLabel === 'Batterie' || cleanLabel === 'Batterie Leistung') {
                                cleanLabel = origVal > 0 ? 'Laden' : (origVal < 0 ? 'Entladen' : 'Batterie');
                                val = Math.abs(val);
                            } else if (cleanLabel === 'Netz' || cleanLabel === 'Netz Gesamt') {
                                cleanLabel = origVal > 0 ? 'Netzbezug' : (origVal < 0 ? 'Einspeisung' : 'Netz');
                                val = Math.abs(val);
                            } else if (cleanLabel === 'Wallbox' || cleanLabel === 'Wallbox 1' || cleanLabel === 'Wallbox 2' || cleanLabel === 'Wallbox Gesamt') {
                                cleanLabel = origVal < -50 ? `${cleanLabel} V2H` : cleanLabel;
                                val = Math.abs(val);
                            }
                            return ` ${cleanLabel}: ${val} ${unit}`;
                        } } },
                        zoom: {
                            pan: { enabled: true, mode: 'x' },
                            zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' }
                        }
	                    }, scales: { x: buildHybridXScale(), ...yAxes }
                }
            });
            const canvas = document.getElementById('liveChartCanvas');
            if (canvas) canvas.ondblclick = () => { if (liveLineChart) liveLineChart.resetZoom(); };
        }

        scheduleJsChartAutoRefresh('hybrid', requestGeneration, () => {
            const container = document.getElementById('liveChartContainer');
            if (container && container.style.display === 'block') loadJsHybridChart(currentLiveHours, file);
        });
    })
    .catch(err => {
        if (isCurrentJsChartRequest('hybrid', requestGeneration)) {
            console.error("Fehler beim Laden des Hybrid-Diagramms:", err);
        }
    });
}

function loadJsPriceChart(hours, file = null) {
    const requestGeneration = beginJsChartRequest('price');
    currentLiveHours = hours;

    let urlLive = 'get_chart_data.php?hours=' + hours;
    if (file) urlLive += '&file=' + encodeURIComponent(file);

    let urlFore = 'get_forecast_data.php';
    if (file) urlFore += '?file=' + encodeURIComponent(file);

    let urlStats = 'get_live_json.php';
    if (file) urlStats = (window.location.pathname.includes('mobile.php') ? 'mobile.php' : 'index.php') + '?action=get_daily_stats&file=' + encodeURIComponent(file);

    Promise.all([
        fetch(urlLive).then(r => r.json()),
        fetch(urlFore).then(r => r.json()),
        (urlStats.startsWith('get_live_json.php')
            ? e3dcFetchLiveJson(urlStats)
            : fetch(urlStats)
        ).then(r => r.json())
    ]).then(([data, forecastData, statsData]) => {
        if (!isCurrentJsChartRequest('price', requestGeneration)) return;
        if (data.error) return;
        if (forecastData.error || !forecastData.labels) forecastData = { labels: [], price: [] };

        const isDarkMode = typeof DARK_MODE !== 'undefined' ? DARK_MODE : true;
        const textColor = isDarkMode ? '#aaa' : '#666';
        const gridColor = isDarkMode ? '#333' : '#e9ecef';

        let labels = [...data.labels];
        let price = data.price ? [...data.price] : new Array(labels.length).fill(null);
        let ecoScore = data.eco_score ? [...data.eco_score] : new Array(labels.length).fill(null);

        const historyLength = labels.length;
        let startIndex = 0;
        let lastLiveMins = 0;
        let lastLiveLabel = labels[labels.length - 1];
        if (!file && lastLiveLabel) {
            let [lh, lm] = lastLiveLabel.split(':').map(Number);
            lastLiveMins = lh * 60 + lm;
            for (let i = 0; i < forecastData.labels.length; i++) {
                let [fh, fm] = forecastData.labels[i].split(':').map(Number);
                let fMins = fh * 60 + fm;
                if (fMins > lastLiveMins || (lastLiveMins > 1380 && fMins < 120)) { startIndex = i; break; }
            }
        }

        const endMinutes = file ? Infinity : lastLiveMins + hours * 60;
        let prevFMins = lastLiveMins;

        for (let i = startIndex; i < forecastData.labels.length; i++) {
            if (!file && forecastData.labels[i]) {
                let [fh, fm] = forecastData.labels[i].split(':').map(Number);
                let fMins = fh * 60 + fm;
                while (fMins < prevFMins - 60) fMins += 1440;
                if (fMins > endMinutes) break;
                prevFMins = fMins;
            }
            labels.push(forecastData.labels[i]);
            if (forecastData.price) price.push(forecastData.price[i] ?? null);
            if (forecastData.eco_score) ecoScore.push(forecastData.eco_score[i] ?? null);
        }

        let datasets = [];
        let yAxes = {};
        if (pushElectricityPriceDataset(datasets, price, 'y', 'Strompreis (ct/kWh)', { fill: true, borderWidth: 2 })) {
            yAxes['y'] = { type: 'linear', display: true, position: 'left', grid: { color: gridColor }, ticks: { color: textColor } };
        }

        if (ecoScore.some(s => s !== null && s !== undefined)) {
            datasets.push({ label: 'Eco-Score', data: ecoScore, borderColor: '#10b981', backgroundColor: 'rgba(16, 185, 129, 0.1)', fill: false, stepped: true, tension: 0, pointRadius: 0, borderWidth: 2, yAxisID: 'y1', spanGaps: true, borderDash: [5, 5] });
            yAxes['y1'] = { type: 'linear', display: true, position: 'right', grid: { drawOnChartArea: false, color: gridColor }, min: 0, max: 100, ticks: { color: '#10b981' } };
        }

        let costs = statsData.costs || (statsData.stats && statsData.stats.costs) || null;
        if (costs && costs.avg_price > 0 && price.some(p => p !== null)) {
            datasets.push({ label: 'Ø-Bezugspreis', data: new Array(labels.length).fill(costs.avg_price), borderColor: '#0dcaf0', borderDash: [4, 4], tension: 0, pointRadius: 0, borderWidth: 2, fill: false, yAxisID: 'y' });
        }

        if (liveLineChart) {
            liveLineChart.resetZoom();
            liveLineChart.options.plugins.legend.display = true;
            liveLineChart.data.labels = labels; liveLineChart.data.datasets = datasets;
            liveLineChart.options.scales = { x: liveLineChart.options.scales.x, ...yAxes };
            liveLineChart.options.plugins.tooltip.callbacks.label = (ctx) => {
                if (ctx.dataset.label === 'Strompreis (ct/kWh)') {
                    return electricityPriceTooltipLabel(ctx, ecoScore);
                }
                return ` ${ctx.dataset.label}: ${chartRawY(ctx)}`;
            };
            liveLineChart.update('none');
        } else {
            const ctx = document.getElementById('liveChartCanvas').getContext('2d');
            liveLineChart = new Chart(ctx, { type: 'line', data: { labels: labels, datasets: datasets }, options: { responsive: true, maintainAspectRatio: false, interaction: { mode: 'index', intersect: false }, plugins: { legend: { position: 'top', labels: { usePointStyle: true, boxWidth: 8, padding: 15, color: textColor, filter: function(item) { return item.text && !item.text.includes('__HIDDEN__'); } }, onClick: function(e, legendItem, legend) { const index = legendItem.datasetIndex; const ci = legend.chart; const isHidden = ci.isDatasetVisible(index); if (isHidden) ci.hide(index); else ci.show(index); legendItem.hidden = isHidden; saveHiddenDataset(legendItem.text, isHidden); } }, tooltip: { callbacks: { label: (ctx) => { if (ctx.dataset.label === 'Strompreis (ct/kWh)') { return electricityPriceTooltipLabel(ctx, ecoScore); } else { return ` ${ctx.dataset.label}: ${chartRawY(ctx)}`; } } } }, zoom: { pan: { enabled: true, mode: 'x' }, zoom: { wheel: { enabled: true }, pinch: { enabled: true }, mode: 'x' } } }, scales: { x: { grid: { color: gridColor }, ticks: { maxTicksLimit: 12, color: textColor } }, ...yAxes } } });
        }

        const detailsEl = document.getElementById('diagramDetails');
        if (detailsEl) {
            if (costs) {
                const eegRevenue = Number(costs.eeg_revenue || 0);
                const netCost = Number(costs.total || 0) - eegRevenue;
                let c = `Ø-Bezugspreis: <span class="text-body">${costs.avg_price} ct/kWh</span> | Tageskosten netto: <span class="text-body fw-bolder">${netCost.toFixed(2)} €</span>`;
                if (eegRevenue > 0) c += ` | EEG: <span class="text-success fw-bolder">+ ${eegRevenue.toFixed(2)} €</span>`;
                c += '<br>';
                c += `<span class="text-muted small">Anteilig: Haus ${costs.home} € | WB ${costs.wb} € | WP ${costs.wp} € | Bat ${costs.bat} €</span>`;
                detailsEl.innerHTML = c; detailsEl.style.display = 'block';
            } else {
                detailsEl.style.display = 'none';
            }
        }

        scheduleJsChartAutoRefresh('price', requestGeneration, () => {
            const container = document.getElementById('liveChartContainer');
            if (container && container.style.display === 'block') loadJsPriceChart(currentLiveHours, file);
        });
    }).catch(e => {
        if (isCurrentJsChartRequest('price', requestGeneration)) {
            console.error("Fehler beim Laden des Preis-Diagramms:", e);
        }
    });
}

window.addEventListener('themeChanged', () => {
    if (liveLineChart) {
        const isDarkMode = typeof DARK_MODE !== 'undefined' ? DARK_MODE : true;
        const t = isDarkMode ? '#aaa' : '#666'; const g = isDarkMode ? '#333' : '#e9ecef';
        liveLineChart.options.plugins.legend.labels.color = t;
        liveLineChart.options.scales.x.grid.color = g; liveLineChart.options.scales.x.ticks.color = t;
        for (let s in liveLineChart.options.scales) {
            if (s.startsWith('y')) {
                if (liveLineChart.options.scales[s].grid) liveLineChart.options.scales[s].grid.color = g;
                if (liveLineChart.options.scales[s].ticks) liveLineChart.options.scales[s].ticks.color = t;
            }
        }
        liveLineChart.update('none');
    }
});

/**
 * ============================================================
 * =               CORE LIVE DATA PROCESSING                  =
 * ============================================================
 */

function _formatWeatherAlertTime(value) {
    if (!value) return '';
    const raw = typeof value === 'string' ? value.trim() : value;
    const numeric = typeof raw === 'number'
        ? raw
        : (/^\d+(\.\d+)?$/.test(String(raw)) ? Number(raw) : null);
    const d = numeric !== null
        ? new Date(numeric < 100000000000 ? numeric * 1000 : numeric)
        : new Date(raw);
    if (Number.isNaN(d.getTime())) return '';
    const now = new Date();
    const today = now.toDateString();
    const tomorrow = new Date(now.getTime() + 86400000).toDateString();
    const hm = d.toLocaleTimeString('de-DE', { hour: '2-digit', minute: '2-digit' });
    if (d.toDateString() === today) return 'Heute ' + hm;
    if (d.toDateString() === tomorrow) return 'Morgen ' + hm;
    return d.toLocaleDateString('de-DE', { day: '2-digit', month: '2-digit' }) + ' ' + hm;
}

function _weatherAlertEntries(alert) {
    return alert && Array.isArray(alert.alerts) ? alert.alerts : [];
}

let weatherAlertDetailTitle = 'Wetterhinweis';
let weatherAlertDetailText = '';

function _weatherAlertText(alert) {
    if (!alert) return '';
    const parts = [
        alert.title,
        alert.summary,
        alert.event,
        alert.headline,
        alert.description,
        alert.instruction,
        alert.risk && alert.risk.reason,
        alert.risk && alert.risk.source
    ];
    _weatherAlertEntries(alert).forEach(entry => {
        parts.push(
            entry.event,
            entry.headline,
            entry.description,
            entry.instruction,
            entry.severity,
            entry.region,
            entry.event_codes && entry.event_codes.GROUP,
            entry.event_codes && entry.event_codes.II
        );
    });
    return parts.filter(Boolean).join(' ').toLowerCase();
}

function _weatherAlertEntryTitle(entry) {
    if (!entry) return 'Wetterwarnung';
    const headline = String(entry.headline || '').trim();
    const event = String(entry.event || '').trim();
    if (headline && event && !headline.toLowerCase().includes(event.toLowerCase())) {
        return headline + ' (' + event + ')';
    }
    return headline || event || 'Wetterwarnung';
}

function _weatherAlertEntryPeriod(entry) {
    if (!entry) return 'Zeitraum nicht angegeben';
    const start = _formatWeatherAlertTime(entry.start || entry.onset || entry.effective || entry.start_ts);
    const end = _formatWeatherAlertTime(entry.end || entry.expires || entry.valid_to || entry.end_ts);
    if (start && end) return 'Gültig: ' + start + ' bis ' + end;
    if (start) return 'Gültig ab: ' + start;
    if (end) return 'Gültig bis: ' + end;
    return 'Zeitraum nicht angegeben';
}

function _weatherAlertDetailLines(alert) {
    if (!alert) return ['Keine Wetterdetails verfügbar.'];
    const entries = _weatherAlertEntries(alert);
    const lines = [];
    entries.slice(0, 5).forEach((entry, index) => {
        const item = entry || {};
        const prefix = entries.length > 1 ? (index + 1) + '. ' : '';
        lines.push(prefix + _weatherAlertEntryTitle(item));
        lines.push(_weatherAlertEntryPeriod(item));
        if (item.region) lines.push('Gebiet: ' + item.region);
        const level = parseInt(item.level || 0, 10);
        const severityParts = [
            item.severity ? String(item.severity) : null,
            Number.isFinite(level) && level > 0 ? 'Stufe ' + level : null
        ].filter(Boolean);
        if (severityParts.length) lines.push('Schwere: ' + severityParts.join(' / '));
        if (index < Math.min(entries.length, 5) - 1) lines.push('');
    });
    if (entries.length > 5) {
        lines.push('');
        lines.push('+' + (entries.length - 5) + ' weitere Warnungen');
    }

    const risk = alert.risk || {};
    if (!entries.length && risk.active) {
        lines.push(alert.title || 'Gewitterrisiko');
        lines.push('Gültig ab: ' + (_formatWeatherAlertTime(alert.start || risk.time) || 'jetzt'));
        if (risk.reason) lines.push(String(risk.reason));
    } else if (!entries.length) {
        lines.push(alert.summary || alert.title || 'Wetterdaten am Anlagenstandort.');
        const period = _weatherAlertEntryPeriod(alert);
        if (period !== 'Zeitraum nicht angegeben') lines.push(period);
    }

    const controlSummary = alert.control_summary || (alert.storm_guard && alert.storm_guard.control_summary);
    if (controlSummary) {
        lines.push('');
        lines.push('Regelung: ' + controlSummary);
    }
    if (alert.stale) {
        lines.push('');
        lines.push('Datenstand: Wetterdaten veraltet.');
    }
    return lines.filter(line => line !== null && line !== undefined && String(line).trim() !== '');
}

function _weatherAlertDetailText(alert) {
    return _weatherAlertDetailLines(alert).join('\n');
}

function _ensureWeatherAlertDetailsModal() {
    let modalEl = document.getElementById('weather-alert-details-modal');
    if (modalEl) return modalEl;
    document.body.insertAdjacentHTML('beforeend', `
        <div class="modal fade" id="weather-alert-details-modal" tabindex="-1" aria-hidden="true">
            <div class="modal-dialog modal-dialog-centered modal-dialog-scrollable">
                <div class="modal-content">
                    <div class="modal-header">
                        <h5 class="modal-title" id="weather-alert-details-title">Wetterhinweis</h5>
                        <button type="button" class="btn-close" data-bs-dismiss="modal" aria-label="Schließen"></button>
                    </div>
                    <div class="modal-body" id="weather-alert-details-body"></div>
                </div>
            </div>
        </div>
    `);
    return document.getElementById('weather-alert-details-modal');
}

function showWeatherAlertDetails(event) {
    if (event) {
        event.preventDefault();
        event.stopPropagation();
    }
    const title = weatherAlertDetailTitle || 'Wetterhinweis';
    const detailText = weatherAlertDetailText || 'Keine Wetterdetails verfügbar.';
    const modalEl = _ensureWeatherAlertDetailsModal();
    const titleEl = document.getElementById('weather-alert-details-title');
    const bodyEl = document.getElementById('weather-alert-details-body');
    if (titleEl) titleEl.textContent = title;
    if (bodyEl) {
        bodyEl.textContent = detailText;
        bodyEl.style.whiteSpace = 'pre-line';
    }
    if (modalEl && window.bootstrap && window.bootstrap.Modal) {
        let modal = window.bootstrap.Modal.getInstance(modalEl);
        if (!modal) modal = new window.bootstrap.Modal(modalEl);
        modal.show();
    } else {
        alert(title + '\n\n' + detailText);
    }
}

function bindWeatherAlertBadgeDetails(badge) {
    badge
        .css('cursor', 'pointer')
        .attr('role', 'button')
        .attr('tabindex', '0')
        .off('click.weatherAlert keydown.weatherAlert')
        .on('click.weatherAlert', showWeatherAlertDetails)
        .on('keydown.weatherAlert', function(event) {
            if (event.key === 'Enter' || event.key === ' ') showWeatherAlertDetails(event);
        });
}

function _weatherAlertPrimaryText(alert) {
    if (!alert) return '';
    const entries = _weatherAlertEntries(alert);
    const first = entries.length ? entries[0] : null;
    const parts = [
        alert.title,
        alert.summary,
        alert.event,
        alert.headline,
        alert.description,
        alert.instruction
    ];
    if (first) {
        parts.push(
            first.event,
            first.headline,
            first.description,
            first.instruction,
            first.severity,
            first.region,
            first.event_codes && first.event_codes.GROUP,
            first.event_codes && first.event_codes.II
        );
    } else {
        parts.push(alert.risk && alert.risk.reason);
    }
    return parts.filter(Boolean).join(' ').toLowerCase();
}

function _weatherAlertLevel(alert) {
    const levels = [
        parseInt(alert && alert.highest_level || 0, 10),
        parseInt(alert && alert.risk && alert.risk.level || 0, 10)
    ];
    _weatherAlertEntries(alert).forEach(entry => levels.push(parseInt(entry.level || 0, 10)));
    return Math.max(0, ...levels.filter(Number.isFinite));
}

function _weatherAlertHasSignal(alert) {
    if (!alert) return false;
    const entries = _weatherAlertEntries(alert);
    const risk = alert.risk || {};
    const text = _weatherAlertText(alert);
    const level = _weatherAlertLevel(alert);
    if (alert.active === true || alert.thunderstorm_active === true || risk.active === true) return true;
    if (entries.length > 0 || level > 0) return true;
    if (/gewitter|unwetter|sturm|orkan|hagel|warnung|glätte|frost|schnee|regen|wind/.test(text)) {
        return !/keine\s+(aktive\s+)?(dwd-)?warnung|keine\s+wetterwarnung|keine\s+erhoehte|keine\s+erhöhte/.test(text);
    }
    return false;
}

function _weatherAlertIsThunder(alert) {
    const text = _weatherAlertText(alert);
    return !!(alert && alert.thunderstorm_active)
        || _weatherAlertEntries(alert).some(entry => entry && entry.thunderstorm)
        || /gewitter|thunder|convective|konvektiv|hagel/.test(text);
}

function _weatherAlertHasWeatherData(alert) {
    if (!alert) return false;
    const risk = alert.risk || {};
    return _weatherAlertHasSignal(alert)
        || !!alert.stale
        || risk.weather_code !== undefined
        || risk.precip_mm !== undefined
        || risk.showers_mm !== undefined
        || !!risk.reason;
}

function _weatherAlertVisual(alert) {
    const risk = (alert && alert.risk) || {};
    const text = _weatherAlertText(alert);
    const primaryText = _weatherAlertPrimaryText(alert);
    const level = _weatherAlertLevel(alert);
    const code = Number(risk.weather_code);
    const hasWeatherCode = Number.isFinite(code);
    const precip = Number(risk.precip_mm || 0) + Number(risk.showers_mm || 0);
    const negativeWeatherText = /keine\s+(aktive\s+)?(dwd-)?warnung|keine\s+wetterwarnung|keine\s+erhoehte|keine\s+erhöhte|no active|no elevated/.test(text);
    const negativePrimaryText = /keine\s+(aktive\s+)?(dwd-)?warnung|keine\s+wetterwarnung|keine\s+erhoehte|keine\s+erhöhte|no active|no elevated/.test(primaryText);
    const isThunderCode = hasWeatherCode && [95, 96, 99].includes(code);
    const isThunder = !negativeWeatherText && (_weatherAlertIsThunder(alert) || isThunderCode);
    const isHeat = !negativeWeatherText && /hitze|hitzewarnung|heiß|heiss|heat|temperature|temperatur/.test(text);
    const isWinter = !negativeWeatherText && (/schnee|glätte|glaette|frost|eisregen|glatteis|snow|ice/.test(text) || (hasWeatherCode && ((code >= 71 && code <= 77) || code === 85 || code === 86)));
    const isRain = !negativeWeatherText && (precip > 0.2 || /regen|starkregen|schauer|niederschlag|überflutung|ueberflutung|hochwasser|rain|shower|flood/.test(text) || (hasWeatherCode && [51, 53, 55, 61, 63, 65, 80, 81, 82].includes(code)));
    const isWind = !negativeWeatherText && /sturm|orkan|wind|böe|boe|boeen|böen|storm|gale|gust/.test(text);
    const primaryHeat = !negativePrimaryText && /hitze|hitzewarnung|heiß|heiss|heat|temperature|temperatur/.test(primaryText);
    const primaryWinter = !negativePrimaryText && /schnee|glätte|glaette|frost|eisregen|glatteis|snow|ice/.test(primaryText);
    const primaryRain = !negativePrimaryText && /regen|starkregen|schauer|niederschlag|überflutung|ueberflutung|hochwasser|rain|shower|flood/.test(primaryText);
    const primaryWind = !negativePrimaryText && /sturm|orkan|wind|böe|boe|boeen|böen|storm|gale|gust/.test(primaryText);
    const hour = new Date().getHours();
    const isNight = hour < 6 || hour >= 21;
    const isWarning = (alert && (alert.active || risk.active) && level > 0) || isThunder;

    if (isWarning) {
        let cls = 'bg-body-tertiary text-info border border-info-subtle';
        if (level >= 3) cls = 'bg-body-tertiary text-danger border border-danger-subtle';
        else if (level === 2) cls = 'bg-body-tertiary text-warning border border-warning-subtle';
        let icon = 'fa-exclamation-triangle';
        let label = 'Wetterwarnung';
        let textLabel = '';
        if (primaryHeat || isHeat) {
            icon = 'fa-temperature-high';
            label = 'Hitzewarnung';
            textLabel = 'Hitze';
        } else if (primaryWind || isWind) {
            icon = 'fa-wind';
            label = /orkan/.test(text) ? 'Orkanwarnung' : 'Sturmwarnung';
            textLabel = /orkan/.test(text) ? 'Orkan' : 'Sturm';
        } else if (primaryWinter || isWinter) {
            icon = 'fa-snowflake';
            label = 'Winterwarnung';
            textLabel = 'Winter';
        } else if (primaryRain || isRain) {
            icon = 'fa-cloud-showers-heavy';
            label = /hochwasser|überflutung|ueberflutung|flood/.test(text) ? 'Hochwasserwarnung' : 'Regenwarnung';
            textLabel = /hochwasser|überflutung|ueberflutung|flood/.test(text) ? 'Flut' : 'Regen';
        } else if (isThunder) {
            icon = 'fa-cloud-bolt';
            label = 'Gewitterwarnung';
        }
        return {
            icon,
            text: textLabel,
            cls,
            label
        };
    }
    if (alert && alert.stale) {
        return {
            icon: 'fa-clock',
            text: 'Wetter',
            cls: 'bg-body-tertiary text-secondary border border-secondary-subtle',
            label: 'Wetterdaten veraltet'
        };
    }
    if (isWinter) {
        return {
            icon: 'fa-snowflake',
            text: 'Schnee',
            cls: 'bg-body-tertiary text-info border border-info-subtle',
            label: 'Schnee'
        };
    }
    if (isRain) {
        return {
            icon: 'fa-cloud-showers-heavy',
            text: 'Regen',
            cls: 'bg-body-tertiary text-primary border border-primary-subtle',
            label: 'Regen'
        };
    }
    if ((!negativeWeatherText && /wolke|bewölkt|bewoelkt|bedeckt|cloud|overcast/.test(text)) || (hasWeatherCode && [2, 3, 45, 48].includes(code))) {
        const partly = code === 2 || /leicht|teils|partly|wechselnd/.test(text);
        return {
            icon: partly ? (isNight ? 'fa-cloud-moon' : 'fa-cloud-sun') : 'fa-cloud',
            text: partly ? 'Wolkig' : 'Wolken',
            cls: 'bg-body-tertiary text-secondary border border-secondary-subtle',
            label: partly ? 'Wolkig' : 'Bewölkt'
        };
    }
    if (hasWeatherCode && (code === 0 || code === 1)) {
        return {
            icon: isNight ? 'fa-moon' : 'fa-sun',
            text: 'Klar',
            cls: 'bg-body-tertiary text-warning border border-warning-subtle',
            label: 'Klarer Himmel'
        };
    }
    return {
        icon: isNight ? 'fa-moon' : 'fa-sun',
        text: isNight ? 'Nacht' : 'Tag',
        cls: 'bg-body-tertiary text-secondary border border-secondary-subtle',
        label: isNight ? 'Nacht' : 'Tag'
    };
}

function updateWeatherAlert(data) {
    const alert = data && data.weather_alert ? data.weather_alert : null;
    const badge = $('#weather-alert-badge, #m-weather-alert-badge');
    const hasSignal = _weatherAlertHasSignal(alert);
    const isStale = !!(alert && alert.stale);
    if (!hasSignal && !isStale && !_weatherAlertHasWeatherData(alert)) {
        badge.hide();
        return;
    }

    const visual = _weatherAlertVisual(alert);
    const neutralWeather = !hasSignal && !isStale;
    const title = isStale && !hasSignal
        ? 'Wetterdaten veraltet'
        : (neutralWeather && visual.label ? visual.label : (alert.title || visual.label || 'Wetterhinweis'));
    const summary = neutralWeather
        ? ((alert.risk && alert.risk.reason) || 'Wetterdaten am Anlagenstandort.')
        : (alert.summary || (alert.risk && alert.risk.reason) || 'Wetterdaten am Anlagenstandort.');
    const startText = _formatWeatherAlertTime(alert.start || (alert.risk && alert.risk.time));
    const endText = _formatWeatherAlertTime(alert.end);
    let meta = startText ? ' | ' + startText : '';
    if (endText) meta += ' bis ' + endText;
    if (isStale) meta += ' | Daten alt';
    weatherAlertDetailTitle = title;
    weatherAlertDetailText = _weatherAlertDetailText(alert);
    const detailLabel = (title + '. ' + weatherAlertDetailText.replace(/\s+/g, ' ')).slice(0, 260);

    badge
        .removeClass('bg-info bg-warning bg-danger bg-secondary bg-body-tertiary text-dark text-white text-light text-info text-primary text-secondary text-warning text-danger border border-info-subtle border-primary-subtle border-secondary-subtle border-warning-subtle border-danger-subtle')
        .addClass(visual.cls)
        .attr('title', title + ': ' + summary + meta)
        .attr('aria-label', detailLabel)
        .show();
    bindWeatherAlertBadgeDetails(badge);
    badge.find('i').attr('class', 'fas ' + visual.icon + (visual.text ? ' me-1' : ''));
    $('#weather-alert-badge-text, #m-weather-alert-badge-text').text(visual.text).toggle(!!visual.text);
}

function getShadowBadgeVisual(data) {
    if (!data) return null;
    const isShadow = data.shadow_mode === true
        || data.shadow_only === true
        || String(data.ha_mode || '').toLowerCase() === 'shadow';
    if (!isShadow) return null;

    const status = String(data.shadow_sync_status || '').toUpperCase();
    const reason = String(data.shadow_sync_reason || '');
    const source = String(data.shadow_live_source || data.live_source || '');
    const master = String(data.shadow_master_url || '');
    const age = data.shadow_snapshot_age_s != null ? Number(data.shadow_snapshot_age_s) : null;
    const ageText = Number.isFinite(age) ? (age.toFixed(age >= 10 ? 0 : 1) + ' s') : '--';
    const lines = [
        'Shadow-System: read-only Testinstanz, keine lokale Hardwaresteuerung.',
        status ? ('Sync: ' + status + (reason ? ' / ' + reason : '')) : 'Sync: --',
        source ? ('Livequelle: ' + source) : null,
        'Snapshot-Alter: ' + ageText,
        master ? ('Master: ' + master) : null
    ].filter(Boolean);

    let classes = ['badge', 'rounded-pill', 'me-1', 'bg-info', 'text-dark'];
    if (status === 'OK') {
        classes = ['badge', 'rounded-pill', 'me-1', 'bg-info', 'text-dark'];
    } else if (status === 'WARN') {
        classes = ['badge', 'rounded-pill', 'me-1', 'bg-warning', 'text-dark'];
    } else if (status === 'PAUSED' || status === 'ERROR' || status === 'DISABLED') {
        classes = ['badge', 'rounded-pill', 'me-1', 'bg-danger', 'text-white'];
    } else {
        classes = ['badge', 'rounded-pill', 'me-1', 'bg-secondary', 'text-light'];
    }

    return {
        className: classes.join(' '),
        html: '<i class="fas fa-layer-group me-1"></i>SHADOW',
        title: lines.join('\n')
    };
}

function publishE3dcLiveData(data) {
    if (!data || typeof data !== 'object') return;
    window.E3DC_LAST_LIVE_DATA = data;
    if (typeof window.dispatchEvent === 'function' && typeof CustomEvent === 'function') {
        window.dispatchEvent(new CustomEvent('e3dc:live-data', { detail: data }));
    }
}

function setDashboardWallboxPauseButton(wbIdx, paused, pending = false) {
    const btn = $(`[data-dashboard-wb-pause="${wbIdx}"]`);
    if (!btn.length) return;
    btn.attr('data-paused', paused ? '1' : '0')
        .toggleClass('btn-warning', paused)
        .toggleClass('btn-outline-secondary', !paused)
        .prop('disabled', pending)
        .attr('title', paused ? 'Automatik fortsetzen' : 'Wallbox manuell pausieren')
        .attr('aria-label', `Wallbox ${wbIdx} ${paused ? 'fortsetzen' : 'pausieren'}`);
    const icon = btn.find('i');
    icon.attr('class', `fas ${pending ? 'fa-spinner fa-spin' : (paused ? 'fa-play' : 'fa-pause')}`);
}

function bindDashboardWallboxPauseButtons() {
    $('[data-dashboard-wb-pause]').off('click.wbpause').on('click.wbpause', function(e) {
        e.preventDefault();
        e.stopPropagation();
        const btn = $(this);
        const wbIdx = String(btn.data('dashboard-wb-pause') || '1');
        const paused = btn.attr('data-paused') === '1';
        const nextPaused = !paused;
        const formData = new FormData();
        formData.append('save_wb_manual_pause_ajax', '1');
        formData.append('wb_id', wbIdx);
        formData.append('manual_pause', nextPaused ? '1' : '0');
        formData.append('csrf_token', String(window.E3DC_CSRF_TOKEN || ''));
        setDashboardWallboxPauseButton(wbIdx, nextPaused, true);
        fetch('Wallbox.php', {
            method: 'POST',
            headers: {
                'X-Requested-With': 'XMLHttpRequest',
                'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
            },
            body: formData
        })
            .then(res => {
                return res.json().catch(() => null).then(payload => {
                    if (!res.ok || !payload || payload.ok !== true) {
                        throw new Error(payload && payload.message ? String(payload.message) : ('HTTP ' + res.status));
                    }
                    return payload;
                });
            })
            .then(payload => {
                setDashboardWallboxPauseButton(wbIdx, !!payload.manual_pause, false);
            })
            .catch(() => {
                setDashboardWallboxPauseButton(wbIdx, paused, false);
                alert('Fehler beim Speichern der Wallbox-Pause!');
            });
    });
}

function triggerWallboxForceStart(wbIdx) {
    wbIdx = String(wbIdx || '1');
    const btn = $(`[data-dashboard-wb-force-start="${wbIdx}"]`);
    const icon = btn.find('i');
    icon.attr('class', 'fas fa-spinner fa-spin');
    btn.prop('disabled', true);
    const formData = new FormData();
    formData.append('trigger_wb_force_start_ajax', '1');
    formData.append('wb_id', wbIdx);
    formData.append('csrf_token', String(window.E3DC_CSRF_TOKEN || ''));
    fetch('Wallbox.php', {
        method: 'POST',
        headers: {
            'X-Requested-With': 'XMLHttpRequest',
            'X-CSRF-Token': String(window.E3DC_CSRF_TOKEN || '')
        },
        body: formData
    })
        .then(res => {
            return res.text().then(text => {
                let payload = null;
                try {
                    payload = JSON.parse(text);
                } catch (_) {
                    payload = null;
                }
                if (!res.ok || !payload || payload.ok !== true) {
                    const detail = payload && (payload.message || payload.error || payload.code);
                    throw new Error(detail ? String(detail) : `Wallbox-Sofortstart fehlgeschlagen (HTTP ${res.status}).`);
                }
                return payload;
            });
        })
        .then(payload => {
            if (!payload || payload.ok !== true) {
                throw new Error((payload && (payload.message || payload.error)) || 'Wallbox-Sofortstart wurde abgelehnt.');
            }
            icon.attr('class', 'fas fa-check text-success');
            setTimeout(() => {
                icon.attr('class', 'fas fa-play');
                btn.prop('disabled', false);
            }, 2500);
        })
        .catch(err => {
            icon.attr('class', 'fas fa-exclamation-triangle text-danger');
            alert(err && err.message ? err.message : 'Wallbox-Sofortstart ist fehlgeschlagen.');
            setTimeout(() => {
                icon.attr('class', 'fas fa-play');
                btn.prop('disabled', false);
            }, 3000);
        });
}

window.triggerWallboxForceStart = triggerWallboxForceStart;
if (typeof $ === 'function') bindDashboardWallboxPauseButtons();

function processLiveData(data) {
    if (!data) return;
    const wb1Configured = wallboxConfiguredFlag(data, 1);
    const wb2Configured = wallboxConfiguredFlag(data, 2);
    cacheStorageCurveData(data);
    renderDirectMarketingDashboardStatus(data);
    smoothWallboxDisplayValues(data);
    publishE3dcLiveData(data);

    if (data.forecast && data.forecast.length > 0) {
        FORECAST_DATA = data.forecast;
    }

    const now = Math.floor(Date.now() / 1000);
    const age = now - (data.ts || 0);
    const statusBadge = $('#connection-status');

    statusBadge.removeClass('bg-secondary bg-success bg-danger bg-warning text-dark text-white text-light');
    if (!data.ts || data.ts === 0) {
        statusBadge.addClass('bg-secondary text-light').text('Warte auf E3DC...');
    } else if (age > 300) {
        statusBadge.addClass('bg-warning text-dark').text('Veraltet (' + Math.floor(age/60) + 'm)');
    } else {
        statusBadge.addClass('bg-success text-white').text('Online');
    }

    // CPU Load & Temp
    if (data.cpu_load !== undefined) {
        const cpuVal = parseFloat(data.cpu_load);
        let tempText = '--°C';
        let tempVal = null;
        if (data.cpu_temp !== undefined && data.cpu_temp !== null) {
            tempVal = parseFloat(data.cpu_temp);
            tempText = tempVal.toFixed(1) + '°C';
        }
        $('#cpu-badge').html('CPU: ' + cpuVal.toFixed(2) + ' | ' + tempText).show();
        $('#cpu-badge').attr('title', 'CPU Load (1min) / Temperatur');
        $('#cpu-badge').removeClass('text-secondary text-warning text-danger');
        if (cpuVal > 2.0 || (tempVal && tempVal > 70)) $('#cpu-badge').addClass('text-danger');
        else if (cpuVal > 1.0 || (tempVal && tempVal > 60)) $('#cpu-badge').addClass('text-warning');
        else $('#cpu-badge').addClass('text-secondary');
    }

    // HA Badge Logik
    const haBadge = $('#ha-badge');
    const shadowBadge = getShadowBadgeVisual(data);
    if (shadowBadge) {
        haBadge.show();
        haBadge.attr('class', shadowBadge.className);
        haBadge.attr('title', shadowBadge.title);
        haBadge.html(shadowBadge.html);
    } else if (data.ha && data.ha.mode && data.ha.mode !== 'off') {
        haBadge.show();
        haBadge.removeClass('bg-success bg-danger bg-warning bg-secondary text-white text-dark text-light pulsating');
        haBadge.attr('title', 'HA Status');
        if (data.ha.mode === 'master') {
            if (data.ha.peer_online) haBadge.addClass('bg-success text-white').attr('title', 'HA Master: Sync OK').html('<i class="fas fa-server me-1"></i>Master');
            else haBadge.addClass('bg-danger text-white').html('<i class="fas fa-server me-1"></i>Slave offline!');
        } else if (data.ha.mode === 'slave') {
            if (data.ha.state === 'failover') haBadge.addClass('bg-danger text-white pulsating').html('<i class="fas fa-exclamation-triangle me-1"></i>FAILOVER');
            else if (data.ha.peer_online) haBadge.addClass('bg-secondary text-light').html('<i class="fas fa-server me-1"></i>Standby');
            else haBadge.addClass('bg-warning text-dark').html('<i class="fas fa-server me-1"></i>Master offline?');
        }
    } else { haBadge.hide(); }

    // Rauschen (Floating Point Imprecision / V2H-Standby) bei Wallbox filtern
    if (wb1Configured && data.wb !== undefined && Math.abs(data.wb) < 50) {
        data.wb = 0;
    }
    if (wb2Configured && data.wb2 !== undefined && Math.abs(data.wb2) < 50) {
        data.wb2 = 0;
    }

    // Berechnungen
    let homeVal = Number.isFinite(parseFloat(data.home)) ? parseFloat(data.home) : (data.home_raw || 0);
    let heatRodVal = data.hs_power || 0;
    if (heatRodVal < 0) heatRodVal = 0;
    if (homeVal < 0) homeVal = 0;

    const wb1Power = wb1Configured ? (parseFloat(data.wb) || 0) : 0;
    const wb2Power = wb2Configured ? (parseFloat(data.wb2) || 0) : 0;
    const wbVal = wb1Power + wb2Power;
    const batVal = Math.round(data.bat);
    const batAbs = Math.abs(batVal);
    const houseSocValue = data.house_battery_soc && Number.isFinite(parseFloat(data.house_battery_soc.value))
        ? parseFloat(data.house_battery_soc.value)
        : parseFloat(data.soc);
    const batStat = getBatStatus(batVal, houseSocValue, data.notstrom_reserve);
    const gridVal = Math.round(data.grid);
    const gridAbs = Math.abs(gridVal);
    const wpVal = parseFloat(data.wp) || 0;
    const climateVal = Math.max(0, parseFloat(data.climate_power_w ?? data.climate ?? 0) || 0);

    // Live Autarkie
    let autarkieLive = calculateLiveAutarky(homeVal, wbVal, wpVal, gridVal, climateVal);
    if (document.getElementById('val-autarky-live')) $('#val-autarky-live').text(autarkieLive.toFixed(0) + '%');

    // Header Values Update (Nur auf Unterseiten vorhanden)
        if (document.getElementById('head-pv')) {
        const fmtHead = (v) => formatWatts(v).replace(/<[^>]*>?/gm, '');
        $('#head-pv').text(fmtHead(data.pv)); $('#head-bat').text(fmtHead(batAbs));
        $('#head-soc').text(Math.round(houseSocValue) + '%').attr('title', 'Hausakku-SoC');
        $('#head-home').text(fmtHead(homeVal)); $('#head-grid').text(fmtHead(gridAbs)); $('#head-wb').text(fmtHead(wbVal));
        if(document.getElementById('head-wp')) $('#head-wp').text(fmtHead(wpVal));
        if(document.getElementById('head-climate')) $('#head-climate').text(fmtHead(climateVal));
    }

    // Außentemperatur-Badge (Auf JEDER Seite vorhanden!)
    if(document.getElementById('head-out-temp')) {
        let outTempStr = '';
        if (data.wp_zuluft_temp !== undefined && data.wp_zuluft_temp !== null && data.wp_zuluft_temp !== '') {
            outTempStr = parseFloat(data.wp_zuluft_temp).toFixed(1) + ' °C';
        } else if (data['Außentemperatur'] !== undefined && data['Außentemperatur'] !== null && data['Außentemperatur'] !== '') {
            outTempStr = parseFloat(data['Außentemperatur']).toFixed(1) + ' °C';
        }

        if (outTempStr !== '') {
            $('#head-out-temp').text(outTempStr).show();
        } else {
            // Wenn wir gar keine WP-Sensordaten haben
            $('#head-out-temp').hide();
        }
    }

    // Icon-Updates für Mini-Header (nur auf Unterseiten)
    if (document.getElementById('head-pv')) {
        const hGridIcon = $('#head-icon-grid');
        hGridIcon.removeClass('text-secondary text-success text-danger');
        if (gridVal > 0) hGridIcon.addClass('text-danger'); else if (gridVal < 0) hGridIcon.addClass('text-success'); else hGridIcon.addClass('text-secondary');

        const hBatIcon = $('#head-icon-bat');
        hBatIcon.removeClass('text-success text-warning text-danger text-muted').addClass(batStat.txt);

        const hWbIcon = $('#head-icon-wb');
        if (wbVal > 0) hWbIcon.removeClass('text-secondary text-success').addClass('text-info pulsating'); else if (wbVal < 0) hWbIcon.removeClass('text-secondary text-info').addClass('text-success pulsating'); else hWbIcon.removeClass('text-info text-success pulsating').addClass('text-secondary');
    }

    // Preis- & Eco-Score UI Updates (Global für Dashboard & Unterseiten)
    const modernFrontendActive = !!(document.body && document.body.classList.contains('frontend-modern'));
    const compactDetailActive = !!(document.body && document.body.classList.contains('detail-compact'));
    const normalDetailActive = !!(modernFrontendActive && document.body.classList.contains('detail-normal'));
    const verboseModernDetailActive = !!(modernFrontendActive && document.body.classList.contains('detail-detail'));
    const showDashboardGridPrice = !(compactDetailActive || normalDetailActive);
    const showDashboardEcoScore = !(compactDetailActive || normalDetailActive || verboseModernDetailActive);
    if (data.price_ct !== undefined && data.price_ct !== null) {
        $('#head-price').text(data.price_ct.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' ct');
        $('#val-price').text(data.price_ct.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' ct');
        $('#card-price-container').attr('style', showDashboardGridPrice ? 'display: flex !important;' : 'display: none !important;');

        const hPriceIcon = $('#head-icon-price');
        hPriceIcon.removeClass('text-secondary text-success text-danger text-warning');
        if (data.price_level === 'cheap') hPriceIcon.addClass('text-success');
        else if (data.price_level === 'expensive') hPriceIcon.addClass('text-danger');
        else hPriceIcon.addClass('text-warning');
    }

    // V4 Eco-Score Integration
    if (data.optimization_score !== undefined && data.optimization_score !== null) {
        $('#head-eco-score').text(Math.round(data.optimization_score));
        $('#val-eco-score').text(Math.round(data.optimization_score));
        $('#head-eco-container').attr('style', 'display: inline-block;');
        $('#val-eco-container').attr('style', showDashboardEcoScore ? 'display: inline-flex !important; cursor: pointer;' : 'display: none !important;');
    } else {
        $('#head-eco-container').hide();
        $('#val-eco-container').hide();
    }

    if (data.notstrom_status === 1 || data.notstrom_status === 4) $('#notstrom-alert, #m-notstrom-alert').attr('style', 'display: flex !important;');
    else $('#notstrom-alert, #m-notstrom-alert').attr('style', 'display: none !important;');

    if (data.system_warning) {
        $('#watchdog-alert, #m-watchdog-alert').attr('style', 'display: flex !important;');
        $('#watchdog-alert-text, #m-watchdog-alert-text').html(data.system_warning);
    } else {
        $('#watchdog-alert, #m-watchdog-alert').attr('style', 'display: none !important;');
    }

    updateWeatherAlert(data);

    updateVehicleWidgets(data);

    if (document.getElementById('val-pv')) {
        $('#val-pv').html(formatWatts(data.pv));
        const socText = `${Math.round(houseSocValue)}%`;
        const socTitle = (data.notstrom_reserve && data.notstrom_reserve > 0)
            ? `Hausakku-SoC ${socText}, Notstromreserve ${data.notstrom_reserve.toFixed(1)}%`
            : `Hausakku-SoC ${socText}`;
        $('#val-soc')
            .removeClass('text-success text-warning text-danger text-muted')
            .addClass(batStat.txt)
            .text(socText)
            .attr('title', socTitle)
            .attr('aria-label', socTitle);

        updateDashboardDailyTiles(data);

        const isDay = (typeof isDaytime === 'function') ? isDaytime() : true;
        const iconPvBox = $('#icon-pv-box'); const iconPv = $('#icon-pv');
        if (isDay) {
            if (iconPv.hasClass('fa-moon')) { iconPv.removeClass('fa-moon').addClass('fa-sun'); iconPvBox.removeClass('bg-secondary text-secondary').addClass('bg-warning text-warning'); }
        } else {
            if (iconPv.hasClass('fa-sun')) { iconPv.removeClass('fa-sun').addClass('fa-moon'); iconPvBox.removeClass('bg-warning text-warning').addClass('bg-secondary text-secondary'); }
        }

        const sollVal = (typeof getTheoreticalPower === 'function') ? getTheoreticalPower() : 0;

        let totalForecastKwh = 0;
        let remainingForecastKwh = 0;
        if (typeof FORECAST_DATA !== 'undefined' && Array.isArray(FORECAST_DATA)) {
            const intervalHours = 0.25;
            let currentHour = new Date().getHours() + (new Date().getMinutes() / 60);

            FORECAST_DATA.forEach(entry => {
                const h = parseFloat(entry.h);
                const watts = parseFloat(entry.w);
                if (Number.isFinite(h) && h >= 0 && h < 24 && Number.isFinite(watts) && watts >= 0 && watts < 100000) {
                    let energy = (watts * intervalHours) / 1000;
                    totalForecastKwh += energy;
                    if (h >= currentHour) {
                        remainingForecastKwh += energy;
                    }
                }
            });

            // Wenn awattardebug.23.txt verfügbar: stabilen Gesamtertrag verwenden
            const stablePvToday = normalizeKwh(typeof STABLE_PV_TODAY_KWH !== 'undefined' ? STABLE_PV_TODAY_KWH : null, null);
            if (stablePvToday !== null && stablePvToday > 0) {
                totalForecastKwh = stablePvToday;
            }
        }


        const pvDetails = $('#val-pv-details');
        if (typeof SHOW_FORECAST !== 'undefined' && SHOW_FORECAST && (sollVal > 10 || totalForecastKwh > 0)) {
            $('#val-pv-soll').text(sollVal >= 1000 ? (sollVal/1000).toFixed(2) + 'kW' : Math.round(sollVal) + 'W');
            pvDetails.show();
        } else {
            pvDetails.hide();
        }

        totalForecastKwh = normalizeKwh(totalForecastKwh, 0);
        remainingForecastKwh = Math.min(normalizeKwh(remainingForecastKwh, 0), totalForecastKwh);
        const measuredPvToday = normalizeKwh(data && data.pv_today_kwh, null);
        if (measuredPvToday !== null && totalForecastKwh > 0) {
            remainingForecastKwh = Math.min(remainingForecastKwh, Math.max(0, totalForecastKwh - measuredPvToday));
        }
        if (typeof SHOW_FORECAST !== 'undefined' && SHOW_FORECAST && totalForecastKwh > 0) {
            $('#val-pv-forecast').text(formatKwh(totalForecastKwh, 1)).parent().show();
            $('#val-pv-forecast-remain').text(formatKwh(remainingForecastKwh, 1)).parent().show();
        } else {
            $('#val-pv-forecast').parent().hide();
            $('#val-pv-forecast-remain').parent().hide();
        }

        $('#val-home').html(formatWatts(homeVal));
        const homeForecastDetails = $('#home-forecast-details');
        if (data.ml_home_kwh !== undefined && data.ml_home_kwh !== null) {
            $('#val-home-forecast').text(data.ml_home_kwh.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' kWh');
            homeForecastDetails.show();
        } else {
            homeForecastDetails.hide();
        }
        const wpForecastDetails = $('#wp-forecast-details');
        if (data.ml_wp_kwh !== undefined && data.ml_wp_kwh !== null) {
            $('#val-wp-forecast').text(data.ml_wp_kwh.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' kWh');
            wpForecastDetails.show();
        } else {
            wpForecastDetails.hide();
        }

        if (data.autarky_day !== undefined) $('#val-autarky-day').text(Math.round(data.autarky_day) + '%');
        if (data.selfcon_day !== undefined) $('#val-selfcon-day').text(Math.round(data.selfcon_day) + '%');
        if (document.getElementById('climate-card-today-value') || document.getElementById('climate-today')) {
            const climateToday = normalizeKwh(data.climate_daily_kwh, null);
            const climateStatsToday = data.stats ? normalizeKwh(data.stats.total_climate_kwh, null) : null;
            const climateTodayValue = climateToday !== null ? climateToday : climateStatsToday;
            if (document.getElementById('climate-today')) {
                $('#climate-today').text(formatKwh(climateTodayValue, 2));
            }
            if (document.getElementById('climate-card-today-value')) {
                $('#climate-card-today-value').text(formatKwh(climateTodayValue, 2));
            }
        }
        if (document.getElementById('val-climate-card')) {
            $('#val-climate-card').html(formatWatts(climateVal));
            const climateIcon = $('#icon-climate-card');
            climateIcon.removeClass('bg-info text-info bg-secondary text-secondary pulsating');
            if (data.climate_online === false) {
                climateIcon.addClass('bg-secondary text-secondary');
            } else if (climateVal > 50) {
                climateIcon.addClass('bg-info text-info pulsating');
            } else {
                climateIcon.addClass('bg-info text-info');
            }
            const climateStatus = $('#climate-card-status');
            if (climateStatus.length) {
                const statusParts = [];
                if (data.climate_online === false) statusParts.push('Zähler offline');
                else if (climateVal > 50) statusParts.push('aktiv');
                else statusParts.push('bereit');
                const climateForecastKwh = normalizeKwh(data.ml_climate_kwh, null);
                if (climateForecastKwh !== null && climateForecastKwh > 0) {
                    statusParts.push('Prognose ' + climateForecastKwh.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + ' kWh');
                }
                if (data.climate_source) statusParts.push(String(data.climate_source));
                if (data.climate_phase) statusParts.push('Phase ' + String(data.climate_phase).toUpperCase());
                climateStatus.text(statusParts.join(' · ')).show();
            }
        }

        if (currentStatsDate === 'today' && data.stats) updateStatsUI(data, 'desktop');

        const pvBreakdown = livePvBreakdownHtml(data);
        if (pvBreakdown) $('#pv-strings-detail').html(pvBreakdown).show(); else $('#pv-strings-detail').hide();
        const gridPhaseSummary = liveGridPhaseCompactText(data);
        if (data.ac0_w !== undefined || gridPhaseSummary || data.grid_pm_available === false) {
            let details = '';
            if (gridPhaseSummary) {
                details += `<div style="white-space:nowrap; font-size:0.9rem; font-weight:bold; margin-bottom:3px;">${gridPhaseSummary}</div>`;
            } else if (data.grid_pm_available === false) {
                details += `<div style="white-space:nowrap; font-size:0.8rem; opacity:0.8; margin-bottom:3px;">Netzphasen nicht verfügbar</div>`;
            }
            if (data.ac0_w !== undefined) details += `<div style="white-space:nowrap; font-size:0.8rem; opacity:0.8;">WR: ${data.ac0_w} | ${data.ac1_w} | ${data.ac2_w} W</div>`;
            $('#grid-details').html(details).show();
        } else { $('#grid-details').hide(); }
        if (data.wb_p1 !== undefined && (data.wb_p1 > 0 || data.wb_p2 > 0 || data.wb_p3 > 0) && data.wb > 0) $('#wb-details').html(`L1: ${data.wb_p1}W | L2: ${data.wb_p2}W | L3: ${data.wb_p3}W`).show(); else $('#wb-details').hide();

        if (data.bat_v !== undefined) {
            let batDet = `K1: ${data.bat_v}V | ${data.bat_a}A`;
            if (data.bat1_v && data.bat1_v > 0) batDet += ` | K2: ${data.bat1_v}V | ${data.bat1_a}A`;
            $('#bat-details').html(batDet).show();
        } else { $('#bat-details').hide(); }

        const updateSingleWallboxUI = (id, power, locked, mode, p1, p2, p3, session, apparentKva, powerFactor, setAmp, capAmp, statusAmp, offeredCurrentRaw, currentStepAmp, fractionalCurrentSupported, peaks, carName) => {
            const valWb = Math.abs(parseFloat(power) || 0);
            const isLocked = locked === true;
            const wbId = id === 'wb' ? '' : '2';
            const kva = Math.max(0, parseFloat(apparentKva) || 0);
            const pf = Math.max(0, Math.min(1, parseFloat(powerFactor) || 0));
            const currentSetAmp = Math.max(0, parseFloat(setAmp) || 0);
            const currentCapAmp = Math.max(0, parseFloat(capAmp) || 0);
            const currentStatusAmp = Math.max(0, parseFloat(statusAmp) || 0);
            const rawOfferedAmp = Math.max(0, parseFloat(offeredCurrentRaw) || 0);
            const stepAmp = Math.max(0, parseFloat(currentStepAmp) || 1);
            const fineAmpSupported = fractionalCurrentSupported === true
                || fractionalCurrentSupported === 1
                || fractionalCurrentSupported === '1'
                || String(fractionalCurrentSupported).toLowerCase() === 'true'
                || stepAmp <= 0.11
                || (rawOfferedAmp > 0 && Math.abs(rawOfferedAmp - Math.round(rawOfferedAmp)) > 0.001);
            const displaySetAmp = fineAmpSupported && rawOfferedAmp > 0 ? rawOfferedAmp : currentSetAmp;
            const ampPrecision = fineAmpSupported && displaySetAmp > 0 ? 1 : 0;
            const fmtAmp = (amp, minPrecision = 0) => minPrecision > 0 || !Number.isInteger(amp)
                ? amp.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1})
                : String(amp);
            const wallboxConnected = wallboxPrimaryVehicleActive(data, id, valWb, isLocked);
            const carDisplayName = wallboxConnected ? String(carName || '').trim() : '';
            renderWallboxPrimaryVehicleName(document.getElementById(`wb${wbId}-title`), carDisplayName, !!carDisplayName);

            $(`#val-wb${wbId}`).html(formatWatts(valWb));
            const wbIcon = $(`#icon-wb${wbId}`);
            const wbLockOverlay = $(`#wb${wbId}-lock-overlay`);

            if (power > 0) {
                wbIcon.removeClass('bg-secondary text-secondary bg-warning text-warning bg-success text-success').addClass('bg-info text-info pulsating');
                if (isLocked) wbLockOverlay.show(); else wbLockOverlay.hide();
            } else if (power < 0) {
                wbIcon.removeClass('bg-secondary text-secondary bg-warning text-warning bg-info text-info').addClass('bg-success text-success pulsating');
                if (isLocked) wbLockOverlay.show(); else wbLockOverlay.hide();
            } else {
                wbIcon.removeClass('bg-info text-info bg-warning text-warning bg-success text-success pulsating').addClass('bg-secondary text-secondary');
                if (isLocked) wbLockOverlay.show(); else wbLockOverlay.hide();
            }

            let activePhases = 0;
            if (p1 > 10) activePhases++; if (p2 > 10) activePhases++; if (p3 > 10) activePhases++;

            let phText = "";
            if (p1 !== undefined && (p1 > 0 || p2 > 0 || p3 > 0)) {
                if (power > 0 && activePhases === 0) activePhases = 1;
                phText = ` (${activePhases}-ph)`;
            }

            const statusEl = $(`#wb${wbId}-status`);
            if (power > 0) {
                let t = `Lädt${phText}`;
                if (mode !== undefined && mode !== 0 && mode !== null) t += ` | Mode ${mode}`;
                statusEl.text(t);
            }
            else if (power < 0) statusEl.html(`<span class="text-success fw-bold"><i class="fas fa-bolt"></i> V2H Entladen</span>`);
            else if (isLocked) {
                let t = `Verbunden`;
                if (mode !== undefined && mode !== 0 && mode !== null) t += ` | Mode ${mode}`;
                statusEl.text(t);
            }
            else statusEl.text('Bereit');

            if (p1 !== undefined && (p1 > 0 || p2 > 0 || p3 > 0) && valWb > 0) {
                const pfInline = pf > 0 ? ` · LF ${pf.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : '';
                const kvaInline = kva > 0.05 ? ` | Schein ${kva.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2})} kVA${pfInline}` : '';
                const detailTitleParts = ['Die große Anzeige ist Wirkleistung in W/kW.'];
                if (kva > 0.05) detailTitleParts.push('kVA ist Scheinleistung aus Spannung x Strom je Phase.');
                if (pf > 0) detailTitleParts.push(`LF ${pf.toFixed(2)} erklärt die Differenz zwischen kW und kVA.`);
                if (displaySetAmp > 0) detailTitleParts.push(`Regel-Soll ${fmtAmp(displaySetAmp, ampPrecision)} A.`);
                if (fineAmpSupported && rawOfferedAmp > 0) detailTitleParts.push(`0,1-A-Feinregelung aktiv.`);
                if (currentCapAmp > 0) detailTitleParts.push(`Regel-Cap ${fmtAmp(currentCapAmp)} A.`);
                if (currentStatusAmp > 0) detailTitleParts.push(`Wallbox-Statusstrom ${fmtAmp(currentStatusAmp)} A.`);
                const detailTitle = detailTitleParts.join('\n');
                const ampInline = displaySetAmp > 0 ? ` | Soll ${fmtAmp(displaySetAmp, ampPrecision)} A` : '';
                $(`#wb${wbId}-details`).html(`L1: ${p1}W | L2: ${p2}W | L3: ${p3}W${kvaInline}${ampInline}`).attr('title', detailTitle).show();
            } else {
                $(`#wb${wbId}-details`).removeAttr('title').hide();
            }

            let showSessionContainer = false;
            if (kva > 0.05 && valWb > 50) {
                const pfText = pf > 0 ? ` · LF ${pf.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2})}` : '';
                const kvaText = kva.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kVA' + pfText;
                $(`#wb${wbId}-kva`)
                    .text(kvaText)
                    .attr('title', `Scheinleistung aus Spannung x Strom je Phase.${pf > 0 ? ` Leistungsfaktor ca. ${pf.toFixed(2)}.` : ''} Die große Anzeige bleibt die Wirkleistung in W.`)
                    .show();
                showSessionContainer = true;
            } else {
                $(`#wb${wbId}-kva`).hide();
            }

            if (session !== undefined && session !== null && (session > 0 || isLocked)) {
                $(`#wb${wbId}-session`).html(session.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kWh Session').show();
                showSessionContainer = true;
            } else {
                $(`#wb${wbId}-session`).hide();
            }
            if (showSessionContainer) {
                $(`#wb${wbId}-session-container`).show();
            } else if ($(`#wb${wbId}-time-target`).is(':hidden') && $(`#wb${wbId}-time-full`).is(':hidden')) {
                $(`#wb${wbId}-session-container`).hide();
            }

            if (peaks && peaks[`wb${wbId}_max`] > 0) {
                $(`#val-wb${wbId}-max`).text(formatWatts(peaks[`wb${wbId}_max`]).replace(/<[^>]*>?/gm, ''));
                $(`#wb${wbId}-peak-detail`).show();
            } else {
                $(`#wb${wbId}-peak-detail`).hide();
            }
        };

        const updateWallboxIdentityUI = (slot) => {
            const prefix = slot === 2 ? 'wb2' : 'wb';
            const identity = data[`wb${slot}_vehicle_identity`] || {};
            const assigned = identity.assigned || {};
            const liveVehicle = identity.live_vehicle || {};
            const chargeProfile = identity.charge_profile || {};
            const parts = [];
            if (chargeProfile.name) parts.push(`Ladeprofil: ${chargeProfile.name}`);
            if (liveVehicle.name) {
                parts.push(`Live-Fahrzeug: ${liveVehicle.name}`);
            } else if (chargeProfile.name && liveVehicle.stable_identity_present !== true) {
                parts.push('Live-Fahrzeug: nicht stabil erkannt');
            }
            if (assigned.name) parts.push(`E3DC-Zuordnung: ${assigned.name}`);
            const el = $(`#${prefix}-identity`);
            if (!el.length) return;
            if (parts.length) {
                el.text(parts.join(' · ')).attr('title', parts.join('\n')).show();
            } else {
                el.text('').removeAttr('title').hide();
            }
        };

        // Nur konfigurierte Wallboxen anzeigen und aktualisieren. Insbesondere
        // ist ein reguläres WB2-Nullfeld kein Nachweis für eine zweite Wallbox.
        $('#card-wb-wrapper').toggle(wb1Configured);
        if (wb1Configured) {
            updateSingleWallboxUI('wb', wb1Power, data.wb_plug, data.wb_mode, data.wb_p1, data.wb_p2, data.wb_p3, data.wb_session_kwh, data.wb_kva, data.wb_power_factor, data.wb_set_amp, data.wb_cap_amp, data.wb_status_amp, data.wb_offered_current_raw, data.wb_current_step_amp, data.wb_fractional_current_supported, data.peaks, data.wb_display_car_name || data.wb_car_name);
            updateWallboxIdentityUI(1);
            setDashboardWallboxPauseButton('1', data.wb_manual_pause === true || data.wb_manual_pause === 1 || data.wb_manual_pause === '1');
        }

        // Wallbox 2 updaten (falls vorhanden)
        if (wb2Configured) {
            $('#card-wb2-wrapper').show();
            updateSingleWallboxUI('wb2', wb2Power, data.wb2_locked, data.wb2_mode, data.wb2_p1, data.wb2_p2, data.wb2_p3, data.wb2_session_kwh, data.wb2_kva, data.wb2_power_factor, data.wb2_set_amp, data.wb2_cap_amp, data.wb2_status_amp, data.wb2_offered_current_raw, data.wb2_current_step_amp, data.wb2_fractional_current_supported, data.peaks, data.wb2_display_car_name || data.wb2_car_name);
            updateWallboxIdentityUI(2);
            setDashboardWallboxPauseButton('2', data.wb2_manual_pause === true || data.wb2_manual_pause === 1 || data.wb2_manual_pause === '1');
        } else {
            $('#card-wb2-wrapper').hide();
        }
        bindDashboardWallboxPauseButtons();

        // Header Wallbox Icon Pulsieren (Summe)
        const hWbIcon = $('#head-icon-wb');
        const anyWbCharging = wb1Power > 10 || wb2Power > 10;
        const anyWbV2H = wb1Power < -10 || wb2Power < -10;
        if (anyWbCharging) hWbIcon.removeClass('text-secondary text-success').addClass('text-info pulsating');
        else if (anyWbV2H) hWbIcon.removeClass('text-secondary text-info').addClass('text-success pulsating');
        else hWbIcon.removeClass('text-info text-success pulsating').addClass('text-secondary');

        // Fahrzeug-Badge und Ladezeiten (jetzt Dual-fähig)
        if (data.vehicles && data.vehicles.length > 0) {
            let pluggedIn = data.vehicles.filter(v => v.is_plugged_in === true || v.is_plugged_in == 1);
            let activeV = pluggedIn.length > 0 ? pluggedIn[0] : data.vehicles[0];

            if (activeV && activeV.time_remaining_mins != null) {
                let mins = Math.round(activeV.time_remaining_mins);
                let h = Math.floor(mins / 60); let m = mins % 60;
                $('#wb-time-full').html(`<i class="fas fa-battery-full me-1"></i> ${h}:${String(m).padStart(2,'0')}h`).show();

                if (activeV.time_to_target_mins) {
                    let tgtMins = Math.round(activeV.time_to_target_mins);
                    let th = Math.floor(tgtMins / 60); let tm = tgtMins % 60;
                    $('#wb-time-target').html(`<i class="fas fa-bullseye me-1"></i> ${th}:${String(tm).padStart(2,'0')}h`).show();
                } else {
                    $('#wb-time-target').hide();
                }
                $('#wb-session-container').show();
            } else {
                $('#wb-time-full, #wb-time-target').hide();
            }
        } else {
            $('#wb-time-full, #wb-time-target').hide();
        }

        // Steuere die Sichtbarkeit des Session-Containers
        if ($('#wb-session').is(':hidden') && $('#wb-kva').is(':hidden') && $('#wb-time-target').is(':hidden') && $('#wb-time-full').is(':hidden')) {
            $('#wb-session-container').hide();
        }

        $('#val-wp').html(formatWatts(wpVal));
        if (wpVal < 100) {
            $('#icon-wp').removeClass('bg-info text-info bg-danger text-danger pulsating blink').addClass('bg-secondary text-muted').css('animation', 'none');
        } else {
            $('#icon-wp').removeClass('bg-secondary text-muted');
            if (data.wp_type == 1) {
                $('#icon-wp').removeClass('bg-info text-info').addClass('bg-danger text-danger');
            } else {
                $('#icon-wp').removeClass('bg-danger text-danger').addClass('bg-info text-info');
            }
        }

        const wpStatusBadge = $('#wp-status-badge');

        // Synthetisiere data.wp_mode für IDM/Luxtronik, falls das Backend nur Ext-Flags liefert.
        if (data.wp_mode === undefined || data.wp_mode === null) {
            if (data.idm_ext_ww === 1) data.wp_mode = 1;
            else if (data.idm_ext_hz === 1) data.wp_mode = 0;
            else if (data.idm_ext_khl === 1) data.wp_mode = 2;
            else if (wpVal >= 100) data.wp_mode = 99; // Laeuft (ohne explizite ext. Anforderung)
        }

        // Beruhigte WP-Status Logik gegen Script-Flicker
        if (data.wp_mode !== undefined && data.wp_mode !== null) {
            wpStatusBadge.show();
            wpStatusBadge.removeClass('bg-secondary bg-warning bg-danger bg-primary bg-info text-dark text-white pulsating blink').css('animation', 'none');
            switch(parseInt(data.wp_mode)) {
                case 99: wpStatusBadge.addClass('bg-danger text-white').html('<i class="fas fa-fire-alt"></i> Läuft'); break;
                case 0: wpStatusBadge.addClass('bg-warning text-dark').html('<i class="fas fa-fire"></i> Heizen'); break;
                case 1: wpStatusBadge.addClass('bg-danger text-white').html('<i class="fas fa-hot-tub"></i> WW'); break;
                case 2: wpStatusBadge.addClass('bg-primary text-white').html('<i class="fas fa-wind"></i> Kühlen'); break;
                case 3: wpStatusBadge.addClass('bg-primary text-white').text('EVU'); break;
                case 4: wpStatusBadge.addClass('bg-info text-dark').html('<i class="fas fa-snowflake"></i> Abtauen'); break;
                case 5: wpStatusBadge.addClass('bg-secondary text-white').text('Standby'); break;
                default: wpStatusBadge.hide();
            }
        }
        // Wir verstecken das Badge NUR, wenn wirklich gar kein Mode-Info vorliegt (Standby/Fehler)
        // Aber wir lassen es stehen, wenn es vorher "Standby" war (kein Flickern)
        else if (!wpStatusBadge.text().includes('Standby')) {
            // Badge nur ausblenden wenn kein Boost läuft (verhindert Flicker beim WP-Hochlauf)
            if (data.wp_boost_active !== true && data.mb_state !== 'RUNNING') {
                wpStatusBadge.hide();
            }
        }

        const wpSeasonBadge = $('#wp-season-badge');
        if (wpSeasonBadge.length && data.wp_season_label) {
            const isWinter = data.wp_season === 'winter';
            wpSeasonBadge
                .show()
                .removeClass('bg-secondary bg-info bg-primary text-dark text-white')
                .addClass(isWinter ? 'bg-primary text-white' : 'bg-info text-dark')
                .html(`<i class="fas ${isWinter ? 'fa-snowflake' : 'fa-sun'}"></i> ${data.wp_season_label}`);
            if (data.wp_season_temp != null && data.wp_heating_limit_temp != null) {
                wpSeasonBadge.attr('title', `Aussen ${Number(data.wp_season_temp).toFixed(1)}C / Heizgrenze ${Number(data.wp_heating_limit_temp).toFixed(1)}C`);
            }
        } else {
            wpSeasonBadge.hide();
        }

        const wpSgReadyBadge = $('#wp-sg-ready-badge');
        const wpSgReady = heatSgReadyPresentation(data);
        if (wpSgReadyBadge.length && wpSgReady.visible) {
            wpSgReadyBadge
                .show()
                .toggleClass('pulsating', !wpSgReady.blocked)
                .removeClass('bg-success bg-danger bg-secondary text-white text-dark')
                .addClass(wpSgReady.blocked ? 'bg-danger text-white' : 'bg-success text-white')
                .attr('title', wpSgReady.sourceTitle)
                .empty()
                .append($('<i>', {class: `fas ${wpSgReady.iconClass} me-1`}))
                .append(document.createTextNode(wpSgReady.label));
        } else {
            wpSgReadyBadge.removeClass('pulsating').hide().empty();
        }

        if (data.wp_boost_active === true || data.wp_predump_boost === true || data.wp_market_plan === true || data.wp_price_boost === true || data.wp_pause_active === true || data.wp_manual_boost === true) {
            const badge = $('#wp-auto-boost');
            badge.addClass('pulsating').show();
            const ownerReason = data.heat_manager_owner_reason || data.heat_manager_reason || '';
            if (data.wp_predump_boost === true) { badge.html('<i class="fas fa-magic"></i> Pre-Dump'); badge.attr('title', ownerReason || 'Pre-Dump-Wärmefreigabe aktiv'); }
            else if (data.wp_pause_active === true) { badge.html('<i class="fas fa-hourglass-half"></i> Quell-Erholung'); badge.attr('title', ownerReason || 'Quell-Erholung aktiv'); }
            else if (data.wp_market_plan === true) { badge.html('<i class="fas fa-chart-line"></i> Marktfenster'); badge.attr('title', ownerReason || 'Marktvertrag aktiv'); }
            else if (data.wp_price_boost === true) { badge.html('<i class="fas fa-euro-sign"></i> Preisfenster'); badge.attr('title', ownerReason || 'Preisfenster aktiv'); }
            else if (data.wp_manual_boost === true) { badge.html('<i class="fas fa-hand-paper"></i> Manuell'); badge.attr('title', ownerReason || 'Manuelle Wärmefreigabe aktiv'); }
            else { badge.html('<i class="fas fa-fire"></i> Wärmebudget'); badge.attr('title', ownerReason || 'Wärmebudget aktiv'); }
        } else { $('#wp-auto-boost').removeClass('pulsating').hide(); }

        const mbBadge = $('#wp-morning-boost');
        if (data.mb_state === 'RUNNING') {
            let icon = 'fa-battery-bolt'; let text = 'Morgen-Boost';
            if (data.mb_prio === 'wallbox') { icon = 'fa-car-battery'; text = 'WB Boost'; } else if (data.mb_prio === 'heatpump') { icon = 'fa-fan'; text = 'WP Boost'; }
            mbBadge.addClass('pulsating').html(`<i class="fas ${icon} me-1"></i> ${text}`).show();
        } else { mbBadge.removeClass('pulsating').hide(); }

        if (data.wp_ww_temp != null) { $('#val-wp-ww').text(data.wp_ww_temp.toFixed(1)); $('#wp-temps').show(); }
        if (data.wp_rl_temp != null) { $('#wp-rl-label').text(data.wp_rl_source === 'external' ? 'RL-Ext:' : 'RL:'); $('#val-wp-rl').text(data.wp_rl_temp.toFixed(1)); $('#wp-rl-container').show(); $('#wp-temps').show(); }
        else { $('#wp-temps').hide(); }

        $('#val-bat-container').html(formatWatts(batAbs));

        let batTimeText = '';
        if (typeof BAT_CAPACITY !== 'undefined' && BAT_CAPACITY > 0 && batAbs > 50) {
            let hours = 0; let soc = parseFloat(data.soc) || 0;
            if (batVal > 0 && soc < 100) {
                hours = ((100 - soc) / 100 * BAT_CAPACITY * 1000) / batVal;
                if (hours > 0 && hours < 48) { let h = Math.floor(hours); let m = Math.round((hours - h) * 60); batTimeText = `(voll: ${h}:${m.toString().padStart(2, '0')}h)`; }
            } else if (batVal < 0 && soc > 0) {
                hours = (soc / 100 * BAT_CAPACITY * 1000) / batAbs;
                if (hours > 0 && hours < 48) { let h = Math.floor(hours); let m = Math.round((hours - h) * 60); batTimeText = `(leer: ${h}:${m.toString().padStart(2, '0')}h)`; }
            }
        }
        const batTimeBadge = $('#val-bat-time');
        if (batTimeText) {
            batTimeBadge.text(batTimeText).removeClass('is-placeholder').attr('aria-label', batTimeText);
        } else {
            batTimeBadge.text('--').addClass('is-placeholder').attr('aria-label', 'Keine Batteriezeit');
        }

        const batIcon = $('#icon-bat'); const batContainer = $('#val-bat-container');
        batIcon.removeClass('text-success text-warning text-danger text-muted bg-success bg-warning bg-danger bg-secondary pulsating');
        batContainer.removeClass('text-success text-warning text-danger text-muted');

        batIcon.addClass(batStat.txt + ' ' + batStat.bg);
        batContainer.addClass(batStat.txt);
        $('#icon-bat i').removeClass('fa-battery-full fa-battery-three-quarters fa-battery-half fa-battery-quarter fa-battery-empty').addClass(batStat.icon);

        $('#val-grid-container').html(formatWatts(gridAbs));
        const gridIcon = $('#icon-grid'); const gridContainer = $('#val-grid-container');
        if (gridVal > 0) {
            gridIcon.removeClass('text-secondary text-success text-danger').addClass('text-danger');
            gridContainer.removeClass('text-body text-success text-danger').addClass('text-danger');
        } else if (gridVal < 0) {
            gridIcon.removeClass('text-secondary text-success text-danger').addClass('text-success');
            gridContainer.removeClass('text-body text-success text-danger').addClass('text-success');
        } else {
            gridIcon.removeClass('text-success text-danger').addClass('text-secondary');
            gridContainer.removeClass('text-success text-danger').addClass('text-body');
        }

        if (data.price_ct !== undefined && data.price_ct !== null) {
            $('#val-price').html(data.price_ct.toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + '&nbsp;<span style="font-size:0.7rem; color:var(--bs-secondary-color);">ct</span>');

            // Dummy updates for old elements to avoid errors
            if (data.price_min_ct !== undefined && data.price_min_ct !== null) $('#val-price-min').text(data.price_min_ct);
            if (data.price_max_ct !== undefined && data.price_max_ct !== null) $('#val-price-max').text(data.price_max_ct);

            let min = (data.price_min_ct !== undefined && data.price_min_ct !== null) ? data.price_min_ct : 0;
            let max = (data.price_max_ct !== undefined && data.price_max_ct !== null) ? data.price_max_ct : 50;
            let isFlat = (max - min) < 0.1;

            $('#card-price-container').attr('style', showDashboardGridPrice ? 'display: flex !important;' : 'display: none !important;');

            const badge = $('#card-price-container .badge');
            badge.removeClass('border-success border-danger border-warning border-info');

            if (isFlat) {
                $('#price-trend').html('<i class="fas fa-tag text-info" title="Festpreis / Fixtarif"></i>');
                badge.addClass('border-info');
            } else {
                if (data.price_level === 'cheap') { badge.addClass('border-success'); }
                else if (data.price_level === 'expensive') { badge.addClass('border-danger'); }
                else { badge.addClass('border-warning'); }

                const prices = data.prices || [];
                const priceStartHour = data.price_start_hour;
                const priceInterval = data.price_interval || 1.0;
                let trendIcon = '<i class="fas fa-minus text-secondary"></i>';

                if (prices.length > 1 && priceStartHour !== null) {
                    const dNowPrice2 = new Date();
                    const curGmtDec = dNowPrice2.getUTCHours() + (dNowPrice2.getUTCMinutes() / 60);
                    let hourDiff = curGmtDec - priceStartHour;
                    if (hourDiff < 0) hourDiff += 24;

                    let idx = Math.floor(hourDiff / priceInterval);
                    if (prices[idx] !== undefined && prices[idx+1] !== undefined) {
                        const diff = prices[idx+1] - prices[idx];
                        if (diff > 0.1) trendIcon = '<i class="fas fa-arrow-trend-up text-danger" title="Preis steigend"></i>';
                        else if (diff < -0.1) trendIcon = '<i class="fas fa-arrow-trend-down text-success" title="Preis fallend"></i>';
                        else trendIcon = '<i class="fas fa-arrow-right text-info" title="Preis stabil"></i>';
                    }
                }
                $('#price-trend').html(trendIcon);
            }
        }

        const detailsEl = document.getElementById('diagramDetails');
        const modeSelect = document.getElementById('chart-mode-select');
        const isPriceMode = (modeSelect && modeSelect.value === 'price') || (typeof CURRENT_VIEW !== 'undefined' && CURRENT_VIEW === 'price');

        if (detailsEl && !isPriceMode) {
            let content = '';
            if (CURRENT_VIEW === 'pv') {
                const pvBreakdownText = livePvBreakdownHtml(data);
                content = pvBreakdownText ? `Gesamt: ${data.pv}W | ${pvBreakdownText}` : `Gesamt: ${data.pv}W`;
            } else if (CURRENT_VIEW === 'grid') {
                const phaseText = liveGridPhaseLabeledText(data);
                content = phaseText ? `Netz: ${data.grid}W (${phaseText})` : `Netz: ${data.grid}W`;
                if (data.ac0_w !== undefined) {
                    const wrTotal = (data.ac0_w || 0) + (data.ac1_w || 0) + (data.ac2_w || 0);
                    content += ` | WR: ${wrTotal}W (L1: ${data.ac0_w} | L2: ${data.ac1_w} | L3: ${data.ac2_w})`;
                }
            } else if (CURRENT_VIEW === 'wb') {
                if (data.wb_p1 !== undefined) content = `WB 1: ${data.wb_p1}W | ${data.wb_p2}W | ${data.wb_p3}W`;
            } else if (CURRENT_VIEW === 'wb2') {
                if (data.wb2_p1 !== undefined) content = `WB 2: ${data.wb2_p1}W | ${data.wb2_p2}W | ${data.wb2_p3}W`;
            } else if (CURRENT_VIEW === 'hs') {
                const hsActual = data.hs_power || 0;
                const hsReq = data.hs_requested_w || data.hs_target_w || 0;
                content = `Heizstab Ist: ${hsActual}W`;
                if (hsReq > 0 && Math.abs(hsReq - hsActual) > 20) content += ` | Anforderung: ${hsReq}W`;
                if (data.elwa_water_temp_c != null) content += ` | Wasser: ${Number(data.elwa_water_temp_c).toFixed(1)}°C`;
                if (data.elwa_status) content += ` | Status: ${data.elwa_status}`;
            } else if (CURRENT_VIEW === 'climate') {
                const climateActual = data.climate_power_w || data.climate || 0;
                content = `Klima: ${climateActual}W`;
                if (data.climate_daily_kwh != null) content += ` | Heute: ${Number(data.climate_daily_kwh).toFixed(3)} kWh`;
                if (data.climate_source) content += ` | Quelle: ${data.climate_source}`;
                if (data.climate_phase) content += ` | Phase: ${String(data.climate_phase).toUpperCase()}`;
            } else if (CURRENT_VIEW === 'wp') {
                if (data.wp !== undefined) {
                    content = `WP-Leistung: ${data.wp}W`;
                    let ww = data.wp_ww_temp || (data.data && data.data.Warmwasser_Ist);
                    let rl = data.wp_rl_temp || (data.data && data.data.Ruecklauf_Ist);
                    let vl = data.wp_vl_temp || (data.data && data.data.Vorlauf_Ist);
                    let khl = data.wp_kaelte_temp || (data.data && (data.data.Kaeltespeicher_Ist || data.data['Kältespeicher_Ist']));
                    if (ww) content += ` | WW: ${ww.toFixed(1)}°C`;
                    if (rl) content += ` | RL: ${rl.toFixed(1)}°C`;
                    if (vl) content += ` | VL: ${vl.toFixed(1)}°C`;
                    if (khl) content += ` | Kältespeicher: ${khl.toFixed(1)}°C`;
                }
            } else if (CURRENT_VIEW === 'bat') {
                if (data.bat_v !== undefined) {
                    content = `K1: ${data.bat_v}V | ${data.bat_a}A`;
                    if (data.bat1_v && data.bat1_v > 0) content += ` &nbsp;&bull;&nbsp; K2: ${data.bat1_v}V | ${data.bat1_a}A`;
                }
            }
            if (content) { detailsEl.innerHTML = content; detailsEl.style.display = 'block'; } else { detailsEl.style.display = 'none'; }
        }
    }

    let hsVal = data.hs_power || 0;
    if (hsVal < 0) hsVal = 0;
    if (document.getElementById('val-hs-card')) {
        $('#val-hs-card').html(formatWatts(hsVal));
        if (hsVal > 0) $('#icon-hs-card').removeClass('bg-secondary text-secondary').addClass('bg-warning text-warning pulsating');
        else $('#icon-hs-card').removeClass('bg-warning text-warning pulsating').addClass('bg-secondary text-secondary');

        const hsBadge = document.getElementById('hs-status-badge');
        if (hsBadge) {
            const elwaStatus = data.elwa_status || '';
            const hsMode = data.hs_mode || '';
            let label = '';
            let cls = 'badge bg-secondary text-white';
            if (elwaStatus === 'Heizen' || elwaStatus === 'Boost') {
                label = elwaStatus;
                cls = 'badge bg-warning text-dark';
            } else if (elwaStatus === 'Fertig') {
                label = 'Fertig';
                cls = 'badge bg-success text-white';
            } else if (hsMode === 'pre_dump') {
                label = 'Pre-Dump';
                cls = 'badge bg-success text-white';
            } else if (hsMode === 'grid_follow') {
                label = 'überschuss';
                cls = 'badge bg-success text-white';
            } else if (hsMode === 'pv_auto') {
                label = hsVal > 0 ? 'Auto' : 'Bereit';
                cls = hsVal > 0 ? 'badge bg-info text-dark border' : 'badge bg-secondary text-white';
            } else if (elwaStatus) {
                label = elwaStatus;
            }
            hsBadge.className = cls;
            hsBadge.textContent = label;
            hsBadge.style.display = label ? '' : 'none';
            hsBadge.title = data.hs_reason || '';
        }
    }

    updateModernDashboardActivity(data, {gridVal, wpVal, hsVal, climateVal});

    const flowView = document.getElementById('flow-view');
    if (flowView && flowView.style.display !== 'none') {
        $('#f-node-wb, #flow-line-wb, #flow-dot-wb').toggle(wb1Configured);
        $('#f-node-wb2, #flow-line-wb2, #flow-dot-wb2').toggle(wb2Configured);
        updateEnergyFlowLines(flowView);
        const updateFlow = (id, val, reverse) => {
            const el = document.getElementById(id); if (!el) return;
            if (!flowAnimationCache[id]) flowAnimationCache[id] = { speed: null, gap: null, reverse: null, stopped: false };
            const cache = flowAnimationCache[id];
            if (Math.abs(val) < 15) { if (!cache.stopped) { el.classList.add('stopped'); cache.stopped = true; } return; }
            const absVal = Math.abs(val); let newGap = 30;
            if (absVal > 6000) newGap = 10; else if (absVal > 3000) newGap = 15; else if (absVal > 1000) newGap = 20;
            if (cache.stopped) { el.classList.remove('stopped'); cache.stopped = false; }
            if (cache.gap !== newGap) { el.style.strokeDasharray = `0 ${newGap}`; cache.gap = newGap; }
            let targetRate = 0.15 + (absVal / 10000) * 0.85; targetRate = Math.min(1.5, Math.max(0.15, targetRate));
            if (cache.reverse !== reverse) { if (reverse) el.classList.add('reverse'); else el.classList.remove('reverse'); cache.reverse = reverse; }
            if (typeof el.getAnimations === 'function') { const anims = el.getAnimations(); if (anims.length > 0) anims[0].playbackRate = targetRate; }
            else { let newSpeed = (absVal < 500) ? 6.0 : ((absVal < 2000) ? 5.0 : ((absVal < 4500) ? 4.0 : ((absVal < 9000) ? 3.0 : 2.0))); if (cache.speed !== newSpeed) { el.style.animationDuration = newSpeed + 's'; cache.speed = newSpeed; } }
        };
        const pvFlow = updateEnergyFlowPvNodes(data, flowView); $('#f-val-home').text(formatWatts(homeVal).replace(/<[^>]*>?/gm, ''));
        const flowAggregates = updateEnergyFlowAggregates({pv: pvFlow.mainPvW, external_pv: pvFlow.externalPvW, home: homeVal, wallbox: wb1Power, wallbox2: wb2Power, wp: wpVal, hs: hsVal, climate: climateVal}, flowView);
        if (wb1Configured) $('#f-val-wb').text(formatWatts(Math.abs(wb1Power)).replace(/<[^>]*>?/gm, ''));
        if (wb2Configured) $('#f-val-wb2').text(formatWatts(Math.abs(wb2Power)).replace(/<[^>]*>?/gm, ''));
        $('#f-val-wp').text(formatWatts(wpVal).replace(/<[^>]*>?/gm, ''));
        $('#f-val-climate').text(formatWatts(climateVal).replace(/<[^>]*>?/gm, ''));
        $('#f-val-grid').text(formatWatts(gridVal).replace(/<[^>]*>?/gm, '')); $('#f-val-bat').text(formatWatts(batAbs).replace(/<[^>]*>?/gm, ''));
        $('#f-val-hs').text(formatWatts(hsVal).replace(/<[^>]*>?/gm, '')); $('#f-lbl-soc').text(Math.round(data.soc) + '% SoC');
        const hsTempNode = $('#f-val-hs-temp');
        if (hsTempNode.length) {
            if (data.elwa_water_temp_c != null) {
                let tempText = Number(data.elwa_water_temp_c).toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + '°C';
                if (data.elwa_target_temp_c != null) tempText += ' / ' + Number(data.elwa_target_temp_c).toLocaleString('de-DE', {minimumFractionDigits: 0, maximumFractionDigits: 1}) + '°C';
                hsTempNode.text(tempText).show();
            } else {
                hsTempNode.hide();
            }
        }

        const setWbNodeStyle = (nodeSelector, lineId, dotId, power) => {
            if (power < 0) {
                const pvColor = getFlowColor('pv', '#ffc107');
                applyFlowSelectorColor(nodeSelector, pvColor);
                $('#' + lineId + ', #' + dotId).attr('stroke', pvColor);
            } else {
                const isWb2 = nodeSelector.includes('wb-2');
                const wbColor = getFlowColor(isWb2 ? 'wallbox2' : 'wallbox', isWb2 ? '#34d399' : '#2ecc71');
                applyFlowSelectorColor(nodeSelector, wbColor);
                $('#' + lineId + ', #' + dotId).attr('stroke', wbColor);
            }
        };
        if (wb1Configured) setWbNodeStyle('.node-wb-1', 'flow-line-wb', 'flow-dot-wb', wb1Power);
        if (wb2Configured) setWbNodeStyle('.node-wb-2', 'flow-line-wb2', 'flow-dot-wb2', wb2Power);

        $('#f-node-bat').toggleClass('charging', batVal > 0);
        $('#flow-line-bat, #flow-dot-bat').attr('stroke', batStat.hex);
        applyFlowSelectorColor('.node-bat', batStat.hex);

        let socPct = Math.round(data.soc);
        const batEl = document.getElementById('f-node-bat');
        if (batEl) {
            batEl.style.setProperty('--bat-fill', batStat.fill);
            batEl.style.setProperty('--bat-soc', socPct + '%');
            batEl.style.setProperty('--bat-soc-top', Math.min(100, socPct + 10) + '%');
        }
        if (data.wp_boost_active === true) { $('.node-wp').addClass('boost'); $('#flow-line-wp, #flow-dot-wp').attr('stroke', '#dc3545'); } else { $('.node-wp').removeClass('boost'); applyFlowSelectorColor('.node-wp', getFlowColor('heatpump', '#f97316')); $('#flow-line-wp, #flow-dot-wp').attr('stroke', getFlowColor('heatpump', '#f97316')); }
        const gridStat = getGridFlowStatus(gridVal);
        applyFlowSelectorColor('.node-grid', gridStat.hex);
        $('#flow-line-grid, #flow-dot-grid').attr('stroke', gridStat.hex);
        applyFlowSelectorColor('.node-climate', getFlowColor('climate', '#38bdf8'));
        $('#flow-line-climate, #flow-dot-climate').attr('stroke', getFlowColor('climate', '#38bdf8'));
        if (wb1Configured && data.wb_locked === true) { $('#f-wb-lock').show(); } else { $('#f-wb-lock').hide(); }
        if (wb2Configured && data.wb2_locked === true) { $('#f-wb2-lock').show(); } else { $('#f-wb2-lock').hide(); }

        updateFlow('flow-dot-pv', pvFlow.mainPvW, false);
        updateFlow('flow-dot-external-pv', pvFlow.externalPvW, false);
        updateFlow('flow-dot-generation', flowAggregates.generationW, false);
        updateFlow('flow-dot-consumption', flowAggregates.consumptionW, false);
        updateFlow('flow-dot-home', homeVal, false);
        updateFlow('flow-dot-wb', wb1Power, wb1Power < 0);
        updateFlow('flow-dot-wb2', wb2Power, wb2Power < 0);
        updateFlow('flow-dot-wp', wpVal, false);
        updateFlow('flow-dot-hs', hsVal, false);
        updateFlow('flow-dot-climate', climateVal, false);
        updateFlow('flow-dot-grid', gridVal, gridVal < 0);
        updateFlow('flow-dot-bat', batVal, batVal > 0);
        $('#f-val-climate').html(formatWatts(climateVal));
        updateEnergyFlowHoverCards(data, {pv: pvFlow.mainPvW, external_pv: pvFlow.externalPvW, home: homeVal, grid: gridVal, bat: batVal, wb: wb1Power, wb2: wb2Power, wp: wpVal, hs: hsVal, climate: climateVal});
    }

    // Daily Min/Max Peaks
    if (data.peaks) {
        const p = data.peaks;
        const showPeaks = (typeof SHOW_FORECAST !== 'undefined' && SHOW_FORECAST);
        const fmt = (v) => formatWatts(v).replace(/<[^>]*>?/gm, '');

        if (showPeaks && p.pv_max > 0) {
            $('#val-pv-max').text(fmt(p.pv_max));
            $('#pv-peak-detail').show();
        } else { $('#pv-peak-detail').hide(); }

        if (showPeaks && p.home_max > 0) {
            $('#val-home-max').text(fmt(p.home_max));
            $('#val-home-min').text(fmt(p.home_min));
            $('#home-peak-detail').show();
        } else { $('#home-peak-detail').hide(); }

        if (showPeaks && (p.bat_max_in > 0 || p.bat_max_out > 0)) {
            $('#val-bat-max-in').text(fmt(p.bat_max_in));
            $('#val-bat-max-out').text(fmt(p.bat_max_out));
            $('#bat-peak-detail').show();
        } else { $('#bat-peak-detail').hide(); }

        if (showPeaks && (p.grid_max_in > 0 || p.grid_max_out > 0)) {
            $('#val-grid-max-in').text(fmt(p.grid_max_in));
            $('#val-grid-max-out').text(fmt(p.grid_max_out));
            $('#grid-peak-detail').show();
        } else { $('#grid-peak-detail').hide(); }

        if (showPeaks && p.wb_max > 0) {
            $('#val-wb-max').text(fmt(p.wb_max));
            $('#wb-peak-detail').show();
        } else { $('#wb-peak-detail').hide(); }

        if (showPeaks && p.wp_max > 0) {
            $('#val-wp-max').text(fmt(p.wp_max));
            $('#wp-peak-detail').show();
        } else { $('#wp-peak-detail').hide(); }
    }

    if ($('#gridHealthModal').is(':visible')) {
        updateGridHealthUI(data);
    }
}

function processMobileData(data) {
    try {
        if (!data) return;
        const wb1Configured = wallboxConfiguredFlag(data, 1);
        const wb2Configured = wallboxConfiguredFlag(data, 2);
        smoothWallboxDisplayValues(data);
        publishE3dcLiveData(data);
        const timeElem = document.getElementById('live-time');
        if (timeElem) timeElem.innerText = data.time;

        const now = Math.floor(Date.now() / 1000);
        const age = now - (data.ts || 0);
        const statusBadge = document.getElementById('connection-status');
        if (statusBadge) {
            if (!data.ts || data.ts === 0) {
                statusBadge.className = 'badge rounded-pill bg-secondary text-light'; statusBadge.innerText = 'Lade...';
            } else if (age > 300) {
                statusBadge.className = 'badge rounded-pill bg-warning text-dark'; statusBadge.innerText = 'Veraltet';
            } else {
                statusBadge.className = 'badge rounded-pill bg-success text-white'; statusBadge.innerText = 'Online';
            }
        }

        const haBadge = document.getElementById('ha-badge');
        const shadowBadge = getShadowBadgeVisual(data);
        if (haBadge && shadowBadge) {
            haBadge.style.display = 'inline-block';
            haBadge.className = shadowBadge.className;
            haBadge.title = shadowBadge.title;
            haBadge.innerHTML = shadowBadge.html;
        } else if (haBadge && data.ha && data.ha.mode && data.ha.mode !== 'off') {
            haBadge.style.display = 'inline-block'; haBadge.className = 'badge rounded-pill me-1 ';
            haBadge.title = 'HA Status';
            if (data.ha.mode === 'master') {
                if (data.ha.peer_online) { haBadge.classList.add('bg-success', 'text-white'); haBadge.title = 'HA Master: Sync OK'; haBadge.innerHTML = '<i class="fas fa-server me-1"></i>Master'; }
                else { haBadge.classList.add('bg-danger', 'text-white'); haBadge.innerHTML = '<i class="fas fa-server me-1"></i>Slave offline!'; }
            } else if (data.ha.mode === 'slave') {
                if (data.ha.state === 'failover') { haBadge.classList.add('bg-danger', 'text-white', 'pulse-active'); haBadge.innerHTML = '<i class="fas fa-exclamation-triangle me-1"></i>FAILOVER'; }
                else if (data.ha.peer_online) { haBadge.classList.add('bg-secondary', 'text-light'); haBadge.innerHTML = '<i class="fas fa-server me-1"></i>Standby'; }
                else { haBadge.classList.add('bg-warning', 'text-dark'); haBadge.innerHTML = '<i class="fas fa-server me-1"></i>Master offline?'; }
            }
        } else if (haBadge) { haBadge.style.display = 'none'; }

        updateWeatherAlert(data);

        if (data.notstrom_status === 1 || data.notstrom_status === 4) $('#m-notstrom-alert').attr('style', 'display: flex !important;');
        else $('#m-notstrom-alert').attr('style', 'display: none !important;');

        const updateFlow = (id, val, reverse) => {
            const el = document.getElementById(id); if (!el) return;
            if (!flowAnimationCache[id]) flowAnimationCache[id] = { speed: null, gap: null, reverse: null, stopped: false };
            const cache = flowAnimationCache[id];
            if (Math.abs(val) < 15) { if (!cache.stopped) { el.classList.add('stopped'); cache.stopped = true; } return; }
            const absVal = Math.abs(val); let newGap = 30;
            if (absVal > 6000) newGap = 10; else if (absVal > 3000) newGap = 15; else if (absVal > 1000) newGap = 20;
            if (cache.stopped) { el.classList.remove('stopped'); cache.stopped = false; }
            if (cache.gap !== newGap) { el.style.strokeDasharray = `0 ${newGap}`; cache.gap = newGap; }
            let targetRate = 0.15 + (absVal / 10000) * 0.85; targetRate = Math.min(1.5, Math.max(0.15, targetRate));
            if (cache.reverse !== reverse) { if (reverse) el.classList.add('reverse'); else el.classList.remove('reverse'); cache.reverse = reverse; }
            if (typeof el.getAnimations === 'function') { const anims = el.getAnimations(); if (anims.length > 0) anims[0].playbackRate = targetRate; }
            else { let newSpeed = (absVal < 500) ? 6.0 : ((absVal < 2000) ? 5.0 : ((absVal < 4500) ? 4.0 : ((absVal < 9000) ? 3.0 : 2.0))); if (cache.speed !== newSpeed) { el.style.animationDuration = newSpeed + 's'; cache.speed = newSpeed; } }
        };

        // Rauschen filtern
        if (wb1Configured && data.wb !== undefined && Math.abs(data.wb) < 50) data.wb = 0;
        if (wb2Configured && data.wb2 !== undefined && Math.abs(data.wb2) < 50) data.wb2 = 0;

        let pv = data.pv || 0; let bat = data.bat || 0; let grid = data.grid || 0;
        let h = Number.isFinite(parseFloat(data.home)) ? parseFloat(data.home) : (data.home_raw || 0);
        let hsVal = data.hs_power || 0; if (hsVal < 0) hsVal = 0;
        if (h < 0) h = 0;
        let wb = wb1Configured ? (data.wb || 0) : 0;
        let wb2 = wb2Configured ? (data.wb2 || 0) : 0;
        let wp = data.wp || 0;
        let climate = Math.max(0, parseFloat(data.climate_power_w ?? data.climate ?? 0) || 0);

        updateMobileStorageStrip(data);
        updateMobileRingFlow(data, {pv: pv, bat: bat, grid: grid, home: h, wb: wb, wb2: wb2, wp: wp, hs: hsVal, climate: climate});
        $('#f-node-wb, #flow-line-wb, #flow-dot-wb').toggle(wb1Configured);
        $('#f-node-wb2, #flow-line-wb2, #flow-dot-wb2').toggle(wb2Configured);
        updateEnergyFlowLines(document.getElementById('flow-view'));

        const mobileFlowView = document.getElementById('flow-view');
        const pvFlow = updateEnergyFlowPvNodes(data, mobileFlowView); $('#f-val-home').html(formatWatts(h));
        const flowAggregates = updateEnergyFlowAggregates({pv: pvFlow.mainPvW, external_pv: pvFlow.externalPvW, home: h, wallbox: wb, wallbox2: wb2, wp, hs: hsVal, climate}, mobileFlowView);
        const yieldTag = $('#f-val-pv-yield');
        if (data.pv_today_kwh != null && data.pv_today_kwh > 0) yieldTag.text(data.pv_today_kwh.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kWh').show();
        else yieldTag.hide();

        // WB Sessions
        if (wb1Configured && data.wb_session_kwh != null && (data.wb_session_kwh > 0 || data.wb_plug === true)) $('#f-val-wb-session').text(data.wb_session_kwh.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kWh').show();
        else $('#f-val-wb-session').hide();

        if (wb2Configured && data.wb2_session_kwh != null && (data.wb2_session_kwh > 0 || data.wb2_locked === true)) $('#f-val-wb2-session').text(data.wb2_session_kwh.toLocaleString('de-DE', {minimumFractionDigits: 2, maximumFractionDigits: 2}) + ' kWh').show();
        else $('#f-val-wb2-session').hide();

        updateVehicleWidgets(data);

        if (wb1Configured) $('#f-val-wb').html(formatWatts(wb));
        if (wb2Configured) $('#f-val-wb2').html(formatWatts(wb2));
        $('#f-val-wp').html(formatWatts(wp)); $('#f-val-grid').html(formatWatts(grid));
        $('#f-val-hs').html(formatWatts(hsVal)); $('#f-val-climate').html(formatWatts(climate)); $('#f-val-bat').html(formatWatts(Math.abs(bat)));
        const hsTempNode = $('#f-val-hs-temp');
        if (hsTempNode.length) {
            if (data.elwa_water_temp_c != null) {
                let tempText = Number(data.elwa_water_temp_c).toLocaleString('de-DE', {minimumFractionDigits: 1, maximumFractionDigits: 1}) + '°C';
                if (data.elwa_target_temp_c != null) tempText += ' / ' + Number(data.elwa_target_temp_c).toLocaleString('de-DE', {minimumFractionDigits: 0, maximumFractionDigits: 1}) + '°C';
                hsTempNode.text(tempText).show();
            } else {
                hsTempNode.hide();
            }
        }

        const batStat = getBatStatus(bat, data.soc, data.notstrom_reserve);
        let resText = (data.notstrom_reserve && data.notstrom_reserve > 0) ? ` (R: ${data.notstrom_reserve.toFixed(0)}%)` : '';
        $('#f-lbl-soc').text(Math.round(data.soc) + '% SoC' + resText);
        $('#f-node-bat .fa-icon').removeClass('fa-battery-full fa-battery-three-quarters fa-battery-half fa-battery-quarter fa-battery-empty').addClass(batStat.icon);

        let autarkieLive = calculateLiveAutarky(h, wb, wp, grid, climate);
        if (document.getElementById('m-val-autarky-live')) $('#m-val-autarky-live').text(autarkieLive.toFixed(0) + '%');
        if (data.autarky_day !== undefined) $('#m-val-autarky-day').text(Math.round(data.autarky_day) + '%');
        if (data.selfcon_day !== undefined) $('#m-val-selfcon-day').text(Math.round(data.selfcon_day) + '%');

        if (currentStatsDate === 'today' && data.stats) updateStatsUI(data, 'mobile');

        const setMobileWbNodeStyle = (selector, lineId, dotId, power, colorKey, fallbackColor) => {
            const color = power < 0 ? getFlowColor('pv', '#ffc107') : getFlowColor(colorKey, fallbackColor);
            applyFlowSelectorColor(selector, color);
            $('#' + lineId + ', #' + dotId).attr('stroke', color);
        };
        if (wb1Configured) setMobileWbNodeStyle('.node-wb-1', 'flow-line-wb', 'flow-dot-wb', wb, 'wallbox', '#2ecc71');
        if (wb2Configured) setMobileWbNodeStyle('.node-wb-2', 'flow-line-wb2', 'flow-dot-wb2', wb2, 'wallbox2', '#34d399');

        $('#f-node-bat').toggleClass('charging', bat > 0);
        $('#flow-line-bat, #flow-dot-bat').attr('stroke', batStat.hex);
        applyFlowSelectorColor('.node-bat', batStat.hex);

        const socPct = Math.round(data.soc);
        const batEl2 = document.getElementById('f-node-bat');
        if (batEl2) {
            batEl2.style.setProperty('--bat-fill', batStat.fill);
            batEl2.style.setProperty('--bat-soc', socPct + '%');
            batEl2.style.setProperty('--bat-soc-top', Math.min(100, socPct + 10) + '%');
        }
        if (data.wp_boost_active === true) { $('.node-wp').addClass('boost'); $('#flow-line-wp, #flow-dot-wp').attr('stroke', '#dc3545'); }
        else { $('.node-wp').removeClass('boost'); applyFlowSelectorColor('.node-wp', getFlowColor('heatpump', '#f97316')); $('#flow-line-wp, #flow-dot-wp').attr('stroke', getFlowColor('heatpump', '#f97316')); }
        const gridStat = getGridFlowStatus(grid);
        applyFlowSelectorColor('.node-grid', gridStat.hex);
        $('#flow-line-grid, #flow-dot-grid').attr('stroke', gridStat.hex);
        applyFlowSelectorColor('.node-climate', getFlowColor('climate', '#38bdf8'));
        $('#flow-line-climate, #flow-dot-climate').attr('stroke', getFlowColor('climate', '#38bdf8'));
        if (wb1Configured && data.wb_locked === true) { $('#f-wb-lock').show(); } else { $('#f-wb-lock').hide(); }
        if (wb2Configured && data.wb2_locked === true) { $('#f-wb2-lock').show(); } else { $('#f-wb2-lock').hide(); }

        updateFlow('flow-dot-pv', pvFlow.mainPvW, false); updateFlow('flow-dot-external-pv', pvFlow.externalPvW, false); updateFlow('flow-dot-home', h, false);
        updateFlow('flow-dot-generation', flowAggregates.generationW, false); updateFlow('flow-dot-consumption', flowAggregates.consumptionW, false);
        updateFlow('flow-dot-wb', wb, wb < 0); updateFlow('flow-dot-wb2', wb2, wb2 < 0);
        updateFlow('flow-dot-wp', wp, false); updateFlow('flow-dot-hs', hsVal, false);
        updateFlow('flow-dot-climate', climate, false);
        updateFlow('flow-dot-grid', grid, grid < 0); updateFlow('flow-dot-bat', bat, bat > 0);
        updateEnergyFlowHoverCards(data, {pv: pvFlow.mainPvW, external_pv: pvFlow.externalPvW, home: h, grid, bat, wb, wb2, wp, hs: hsVal, climate});

        $('.node-pv').off('click.e3dcchart').on('click.e3dcchart', () => toggleDiagram('pv')); $('.node-bat').off('click.e3dcchart').on('click.e3dcchart', () => toggleDiagram('bat'));
        $('.node-grid').off('click.e3dcchart').on('click.e3dcchart', () => toggleDiagram('grid'));
        $('.node-wb-1').off('click.e3dcchart');
        if (wb1Configured) $('.node-wb-1').on('click.e3dcchart', (e) => { e.preventDefault(); e.stopPropagation(); toggleDiagram('wb'); });
        $('.node-wb-2').off('click.e3dcchart');
        if (wb2Configured) $('.node-wb-2').on('click.e3dcchart', (e) => { e.preventDefault(); e.stopPropagation(); toggleDiagram('wb2'); });
        $('.node-wp').off('click.e3dcchart').on('click.e3dcchart', () => toggleDiagram('wp'));
        $('.node-hs').off('click.e3dcchart').on('click.e3dcchart', () => toggleDiagram('hs'));
        $('.node-climate').off('click.e3dcchart').on('click.e3dcchart', () => toggleDiagram('climate'));
        $('#card-wb-wrapper').off('click.wbchart').toggle(wb1Configured);
        if (wb1Configured) {
            $('#card-wb-wrapper').on('click.wbchart', (e) => {
                if ($(e.target).closest('a,button,input,select,textarea,.badge,.btn').length) return;
                e.preventDefault();
                e.stopPropagation();
                toggleDiagram('wb');
            });
        }
        $('#card-wb2-wrapper').off('click.wbchart').toggle(wb2Configured);
        if (wb2Configured) {
            $('#card-wb2-wrapper').on('click.wbchart', (e) => {
                if ($(e.target).closest('a,button,input,select,textarea,.badge,.btn').length) return;
                e.preventDefault();
                e.stopPropagation();
                toggleDiagram('wb2');
            });
        }
        const pCard = document.getElementById('card-price'); if (pCard) pCard.onclick = () => toggleDiagram('price');

        const priceVal = document.getElementById('val-price');
        const priceCard = document.getElementById('card-price');
        const priceNum = Number(data.price_ct);
        if (priceVal) {
            function gmtToLocal(slot) {
                const val = parseFloat(slot); if (isNaN(val)) return '--:--';
                const gmtHour = Math.floor(val); const gmtMin = Math.round((val - gmtHour) * 60);
                const now = new Date(); const date = new Date(); date.setUTCHours(gmtHour, gmtMin, 0, 0);
                let dayLabel = (date.getDate() !== now.getDate()) ? 'morgen' : 'heute';
                return dayLabel + ' ' + String(date.getHours()).padStart(2, '0') + ':' + String(date.getMinutes()).padStart(2, '0');
            }
            let minPrice = '--'; let minTime = '--:--'; let maxPrice = '--'; let maxTime = '--:--';
            if (typeof data.price_min_ct === 'number') minPrice = data.price_min_ct.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });
            if (typeof data.price_max_ct === 'number') maxPrice = data.price_max_ct.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 });

            let pMinVal = (data.price_min_ct !== undefined && data.price_min_ct !== null) ? Number(data.price_min_ct) : 0;
            let pMaxVal = (data.price_max_ct !== undefined && data.price_max_ct !== null) ? Number(data.price_max_ct) : 50;
            let isFlat = Math.abs(pMaxVal - pMinVal) < 0.1;
            if (priceCard) priceCard.style.display = 'block';

            minTime = gmtToLocal(data.price_min_slot); maxTime = gmtToLocal(data.price_max_slot);
            let prices = (data.prices && data.prices.length > 0) ? data.prices : (typeof PRICE_HISTORY !== 'undefined' ? PRICE_HISTORY : []);
            let startHour = typeof PRICE_START_HOUR !== 'undefined' ? PRICE_START_HOUR : 0;
            let interval = typeof PRICE_INTERVAL !== 'undefined' ? PRICE_INTERVAL : 1.0;
            if (typeof USE_STATIC_CHART !== 'undefined' && USE_STATIC_CHART) { prices = typeof PRICE_HISTORY !== 'undefined' ? PRICE_HISTORY : []; }
            else if (data.prices && data.prices.length > 0) {
                 if (data.price_start_hour !== undefined && data.price_start_hour !== null) startHour = data.price_start_hour;
                 if (data.price_interval !== undefined && data.price_interval !== null) interval = data.price_interval;
            }

            priceTendencyHtml = ''; let hourDiff = 0;
            if (isFlat) {
                priceTendencyHtml = '<i class="fas fa-tag text-info ms-2" style="font-size: 0.7em; vertical-align: middle;" title="Festpreis"></i>';
            } else if (prices && prices.length > 0) {
                const dNow = new Date(); const curGmtDec = dNow.getUTCHours() + (dNow.getUTCMinutes() / 60);
                hourDiff = curGmtDec - startHour; if (hourDiff < 0) hourDiff += 24;
                let idx = Math.floor(hourDiff / interval);
                if (prices[idx] !== undefined && prices[idx+1] !== undefined) {
                    const diff = prices[idx+1] - prices[idx];
                    const isExtreme = Math.abs(diff) > 5; const blinkClass = isExtreme ? ' blink-extreme' : '';
                    if (diff > 0.1) priceTendencyHtml = '<i class="fas fa-arrow-trend-up text-danger ms-2' + blinkClass + '" style="font-size: 0.7em; vertical-align: middle;" title="Preis steigend"></i>';
                    else if (diff < -0.1) priceTendencyHtml = '<i class="fas fa-arrow-trend-down text-success ms-2' + blinkClass + '" style="font-size: 0.7em; vertical-align: middle;" title="Preis fallend"></i>';
                    else priceTendencyHtml = '<i class="fas fa-arrow-right text-info ms-2" style="font-size: 0.7em; vertical-align: middle;" title="Preis stabil"></i>';
                }
            }

            // Eco-Score Fallback for Static/HT Prices
            let displayEcoScore = false;
            let ecoValueToDisplay = '--';
            if ((isFlat || data.price_source === 'static_v4') && (data.pure_eco_score !== undefined || data.optimization_score !== undefined)) {
                displayEcoScore = true;
                let ecoNum = data.pure_eco_score !== undefined ? data.pure_eco_score : data.optimization_score;
                ecoValueToDisplay = Number(ecoNum).toFixed(0);
            }

            const fPrice = document.getElementById('f-val-price');
            if (fPrice) {
                fPrice.style.display = 'block';
                if (displayEcoScore) {
                    fPrice.innerHTML = '<i class="fas fa-leaf text-success me-1"></i>' + ecoValueToDisplay;
                    fPrice.style.color = '#10b981';
                    fPrice.title = 'Aktueller Eco-Score';
                } else {
                    fPrice.innerText = (Number.isFinite(priceNum) ? priceNum.toLocaleString('de-DE', { minimumFractionDigits: 1, maximumFractionDigits: 1 }) : '--') + ' ct';
                    fPrice.style.color = 'var(--text-body)';
                    if (isFlat) fPrice.style.color = 'var(--text-body)';
                    else if (data.price_level === 'cheap') fPrice.style.color = '#10b981';
                    else if (data.price_level === 'expensive') fPrice.style.color = '#f43f5e';
                    else fPrice.style.color = '#fbbf24';
                    fPrice.title = 'Aktueller Strompreis';
                }
            }

            if (priceVal) {
                if (displayEcoScore) {
                     priceVal.innerHTML = '<i class="fas fa-leaf text-success me-1 text-opacity-75"></i>' + ecoValueToDisplay + '<span class="unit ps-1"> Score</span>';
                } else {
                     priceVal.innerHTML = (Number.isFinite(priceNum) ? priceNum.toLocaleString('de-DE', { minimumFractionDigits: 2, maximumFractionDigits: 2 }) : '--') + '<span class="unit"> ct/kWh</span>' + priceTendencyHtml;
                }
            }
            const trendIconEl = document.getElementById('price-trend-icon');
            if (trendIconEl) trendIconEl.innerHTML = displayEcoScore ? '' : priceTendencyHtml;

            const minEl = document.getElementById('price-val-min'); const maxEl = document.getElementById('price-val-max');
            if (minEl) { minEl.style.display = isFlat ? 'none' : 'block'; minEl.innerHTML = '<span style="color:#10b981">' + minPrice + '<span class="unit" style="font-size:0.65em;margin-left:2px"> ct</span></span><br><span style="font-size:0.65em;color:#aaa;font-weight:normal">' + minTime + '</span>'; }
            if (maxEl) { maxEl.style.display = isFlat ? 'none' : 'block'; maxEl.innerHTML = '<span style="color:#f43f5e">' + maxPrice + '<span class="unit" style="font-size:0.65em;margin-left:2px"> ct</span></span><br><span style="font-size:0.65em;color:#aaa;font-weight:normal">' + maxTime + '</span>'; }

            const chart = document.getElementById('price-chart'); const line = document.getElementById('price-line'); const label = document.getElementById('price-time-label');
            const dayLine = document.getElementById('price-line-day'); const yesterdayLine = document.getElementById('price-line-yesterday');
            const dayOverlay = document.getElementById('price-overlay-tomorrow'); const dayLabel = document.getElementById('price-label-tomorrow'); const yesterdayLabel = document.getElementById('price-label-yesterday');

            if (isFlat) {
                if (chart) chart.style.display = 'none'; if (line) line.style.display = 'none'; if (label) label.style.display = 'none';
                if (dayLine) dayLine.style.display = 'none'; if (yesterdayLine) yesterdayLine.style.display = 'none';
                if (dayOverlay) dayOverlay.style.display = 'none'; if (dayLabel) dayLabel.style.display = 'none'; if (yesterdayLabel) yesterdayLabel.style.display = 'none';
            } else if (chart && prices && prices.length > 0) {
                if (chart) chart.style.display = 'block';
                const min = Math.min(...prices); const max = Math.max(...prices); const range = max - min || 1;
                let pathData = "M 0 100"; let minIndices = []; let maxIndices = [];
                for (let i = 0; i < prices.length; i++) {
                    if (Math.abs(prices[i] - min) < 0.001) minIndices.push(i); if (Math.abs(prices[i] - max) < 0.001) maxIndices.push(i);
                    const xStart = (i / prices.length) * 240; const xEnd = ((i + 1) / prices.length) * 240;
                    const y = 100 - ((prices[i] - min) / range * 60 + 5);
                    pathData += ` L ${xStart} ${y} H ${xEnd}`;
                    if (i < prices.length - 1) { const nextY = 100 - ((prices[i+1] - min) / range * 60 + 5); pathData += ` V ${nextY}`; }
                }
                pathData += " L 240 100 Z";
                let bars = ""; const slotW = 240 / prices.length; const getY = (p) => 100 - ((p - min) / range * 60 + 5);
                maxIndices.forEach(i => { bars += `<rect x="${i*slotW}" y="${getY(prices[i])}" width="${slotW}" height="${100-getY(prices[i])}" fill="#f43f5e" fill-opacity="0.4" />`; });
                minIndices.forEach(i => { bars += `<rect x="${i*slotW}" y="${getY(prices[i])}" width="${slotW}" height="${100-getY(prices[i])}" fill="#10b981" fill-opacity="0.7" />`; });

                const dNowPrice3 = new Date(); const xPosPercent = (hourDiff / (prices.length * interval)) * 100;
                chart.innerHTML = `<path d="${pathData}" fill="currentColor" fill-opacity="0.25" stroke="none" />` + bars;
                if (xPosPercent >= 0 && xPosPercent <= 100) {
                    line.style.left = xPosPercent + '%'; line.style.display = 'block'; label.style.left = xPosPercent + '%'; label.style.display = 'block';
                    label.innerText = dNowPrice3.getHours().toString().padStart(2, '0') + ':' + dNowPrice3.getMinutes().toString().padStart(2, '0');
                    if (xPosPercent > 85) label.style.transform = 'translateX(-100%)'; else if (xPosPercent < 15) label.style.transform = 'translateX(0%)'; else label.style.transform = 'translateX(-50%)';
                }
                if (dayLine) {
                    const totalHours = prices.length * interval;
                    const nowLocal = new Date(); const localHours = nowLocal.getHours() + (nowLocal.getMinutes() / 60);
                    let posToday = hourDiff - localHours; let posTomorrow = hourDiff + (24 - localHours);
                    const pctToday = (posToday / totalHours) * 100; const pctTomorrow = (posTomorrow / totalHours) * 100;
                    if (pctTomorrow > 0 && pctTomorrow < 100) { dayLine.style.left = pctTomorrow + '%'; dayLine.style.display = 'block'; if (dayOverlay) { dayOverlay.style.left = pctTomorrow + '%'; dayOverlay.style.display = 'block'; } }
                    else { dayLine.style.display = 'none'; if (dayOverlay) { if (pctTomorrow <= 0) { dayOverlay.style.left = '0%'; dayOverlay.style.display = 'block'; } else { dayOverlay.style.display = 'none'; } } }
                    if (dayLabel) dayLabel.style.display = (pctTomorrow < 100) ? 'block' : 'none';
                    if (pctToday > 0 && pctToday < 100) { yesterdayLine.style.left = pctToday + '%'; yesterdayLine.style.display = 'block'; }
                    else { yesterdayLine.style.display = 'none'; }
                    if (yesterdayLabel) yesterdayLabel.style.display = (pctToday > 0) ? 'block' : 'none';
                }
            }
            if (typeof updateLastUpdateDisplay === 'function') updateLastUpdateDisplay();

            if (isFlat) { priceVal.className = 'value text-body'; if (priceCard) { priceCard.style.background = 'var(--bg-card)'; priceCard.style.borderColor = 'var(--border-card)'; } }
            else if (priceNum < 10) { priceVal.className = 'value text-success price-ultra-cheap'; if (priceCard) { priceCard.style.background = 'rgba(16, 185, 129, 0.25)'; priceCard.style.borderColor = '#10b981'; } }
            else if (data.price_level === 'cheap') { priceVal.className = 'value text-success'; if (priceCard) { priceCard.style.background = 'rgba(16, 185, 129, 0.13)'; priceCard.style.borderColor = '#2d3748'; } }
            else if (data.price_level === 'expensive') { priceVal.className = 'value text-danger'; if (priceCard) { priceCard.style.background = 'rgba(244, 63, 94, 0.13)'; priceCard.style.borderColor = '#2d3748'; } }
            else { priceVal.className = 'value'; priceVal.style.color = '#fbbf24'; if (priceCard) { priceCard.style.background = 'rgba(251, 191, 36, 0.13)'; priceCard.style.borderColor = '#2d3748'; } }
        }

        const detailsEl = document.getElementById('diagramDetails');
        const diagContainer = document.getElementById('diagramContainer');
        if (detailsEl && diagContainer && diagContainer.style.display !== 'none' && (typeof CURRENT_VIEW !== 'undefined' && CURRENT_VIEW !== 'price')) {
            let content = '';
            if (CURRENT_VIEW === 'pv') { const pvBreakdownText = livePvBreakdownHtml(data); content = pvBreakdownText ? `Gesamt: ${data.pv}W | ${pvBreakdownText}` : `Gesamt: ${data.pv}W`; }
            else if (CURRENT_VIEW === 'grid') { const phaseText = liveGridPhaseLabeledText(data); content = phaseText ? `Netz: ${data.grid}W (${phaseText})` : `Netz: ${data.grid}W`; if (data.ac0_w !== undefined) { const wrTotal = (data.ac0_w || 0) + (data.ac1_w || 0) + (data.ac2_w || 0); content += `<br>WR: ${wrTotal}W (L1: ${data.ac0_w} | L2: ${data.ac1_w} | L3: ${data.ac2_w})`; } }
            else if (CURRENT_VIEW === 'wb') { if (data.wb_p1 !== undefined) content = `WB 1 L1: ${data.wb_p1}W | L2: ${data.wb_p2}W | L3: ${data.wb_p3}W`; }
            else if (CURRENT_VIEW === 'wb2') { if (data.wb2_p1 !== undefined) content = `WB 2 L1: ${data.wb2_p1}W | L2: ${data.wb2_p2}W | L3: ${data.wb2_p3}W`; }
            else if (CURRENT_VIEW === 'hs') { const hsActual = data.hs_power || 0; const hsReq = data.hs_requested_w || data.hs_target_w || 0; content = `Heizstab Ist: ${hsActual}W`; if (hsReq > 0 && Math.abs(hsReq - hsActual) > 20) content += ` | Anforderung: ${hsReq}W`; if (data.elwa_water_temp_c != null) content += ` | Wasser: ${Number(data.elwa_water_temp_c).toFixed(1)}°C`; if (data.elwa_status) content += ` | Status: ${data.elwa_status}`; }
            else if (CURRENT_VIEW === 'climate') { const climateActual = data.climate_power_w || data.climate || 0; content = `Klima: ${climateActual}W`; if (data.climate_daily_kwh != null) content += ` | Heute: ${Number(data.climate_daily_kwh).toFixed(3)} kWh`; if (data.climate_source) content += ` | Quelle: ${data.climate_source}`; if (data.climate_phase) content += ` | Phase: ${String(data.climate_phase).toUpperCase()}`; }
            else if (CURRENT_VIEW === 'wp') { if (data.wp !== undefined) { content = `WP-Leistung: ${data.wp}W`; let ww = data.wp_ww_temp || (data.data && data.data.Warmwasser_Ist); let rl = data.wp_rl_temp || (data.data && data.data.Ruecklauf_Ist); let vl = data.wp_vl_temp || (data.data && data.data.Vorlauf_Ist); let khl = data.wp_kaelte_temp || (data.data && (data.data.Kaeltespeicher_Ist || data.data['Kältespeicher_Ist'])); if (ww) content += ` | WW: ${ww.toFixed(1)}°C`; if (rl) content += ` | RL: ${rl.toFixed(1)}°C`; if (vl) content += ` | VL: ${vl.toFixed(1)}°C`; if (khl) content += ` | Kältespeicher: ${khl.toFixed(1)}°C`; } }
            else if (CURRENT_VIEW === 'bat') { if (data.bat_v !== undefined) { content = `K1: ${data.bat_v}V | ${data.bat_a}A`; if (data.bat1_v && data.bat1_v > 0) content += ` &nbsp;&bull;&nbsp; K2: ${data.bat1_v}V | ${data.bat1_a}A`; } }
            if (content) { detailsEl.innerHTML = content; detailsEl.style.display = 'block'; } else { detailsEl.style.display = 'none'; }
        }
    } catch (err) { console.error("Mobile Data Processing Error:", err); }
}

// --- Grid Health Dashboard Logic ---
function showGridHealthModal() {
    const el = document.getElementById("gridHealthModal");
    if (!el) return;
    let m = bootstrap.Modal.getInstance(el);
    if (!m) m = new bootstrap.Modal(el);
    m.show();

    // Attempt an immediate render before next polling tick
    if (typeof getCurrentLiveData === 'function' && getCurrentLiveData()) {
        updateGridHealthUI(getCurrentLiveData());
    }
}

function resolveGridFrequencyDisplay(data) {
    const candidates = [
        {
            reportedValid: data && data.grid_frequency_valid === true,
            value: data ? data.grid_frequency_hz : null,
            source: data ? data.grid_frequency_source : '',
            ageS: data ? data.grid_frequency_age_s : null
        },
        {
            reportedValid: data && data.pvi_frequency_valid === true,
            value: data ? data.pvi_frequency_hz : null,
            source: data ? data.pvi_frequency_source : '',
            ageS: null
        }
    ];
    const selected = candidates.find(candidate => (
        candidate.reportedValid
        && typeof candidate.value === 'number'
        && Number.isFinite(candidate.value)
        && candidate.value >= 45
        && candidate.value <= 55
    ));
    if (!selected) {
        return {
            valid: false,
            frequencyHz: null,
            source: '',
            ageS: null,
            badgeClass: 'bg-secondary',
            textClass: 'text-muted',
            iconClass: 'fa-circle-question',
            message: 'Keine bestätigten oder frischen Frequenzdaten verfügbar.'
        };
    }

    const frequencyHz = selected.value;
    const differenceHz = Math.abs(frequencyHz - 50.0);
    if (differenceHz < 0.05) {
        return {
            valid: true,
            frequencyHz,
            source: String(selected.source || ''),
            ageS: selected.ageS,
            badgeClass: 'bg-success',
            textClass: 'text-success',
            iconClass: 'fa-circle-check',
            message: 'Messwert liegt nahe 50 Hz.'
        };
    }
    if (differenceHz < 0.2) {
        return {
            valid: true,
            frequencyHz,
            source: String(selected.source || ''),
            ageS: selected.ageS,
            badgeClass: 'bg-info text-dark',
            textClass: 'text-info',
            iconClass: 'fa-circle-info',
            message: 'Messwert weicht leicht von 50 Hz ab.'
        };
    }
    return {
        valid: true,
        frequencyHz,
        source: String(selected.source || ''),
        ageS: selected.ageS,
        badgeClass: 'bg-warning text-dark',
        textClass: 'text-warning',
        iconClass: 'fa-triangle-exclamation',
        message: 'Messwert weicht deutlich von 50 Hz ab. Messquelle und Zeitstempel prüfen.'
    };
}

function updateGridHealthUI(data) {
    if (!data) return;

    // --- Netzfrequenz-Anzeige ---
    // Reine Messwertdarstellung: Aus einem einzelnen lokalen Frequenzwert
    // werden keine übergeordneten Netz- oder Systemzustände abgeleitet.
    const frequencyDisplay = resolveGridFrequencyDisplay(data);
    if (frequencyDisplay.valid) {
        const freq = frequencyDisplay.frequencyHz;
        const CENTER = 50.0;
        const RANGE  = 0.3;   // Skalenbreite: 49.7 bis 50.3 Hz
        // Marker-Position: 0%=links(49.7), 50%=mitte(50.0), 100%=rechts(50.3)
        const pct = Math.max(0, Math.min(100, ((freq - (CENTER - RANGE)) / (2 * RANGE)) * 100));
        $('#gh-freq-marker').show().css('left', pct + '%');

        const ageText = typeof frequencyDisplay.ageS === 'number'
            ? `, Alter ${frequencyDisplay.ageS.toFixed(1)} s`
            : '';
        $('#gh-freq-badge')
            .removeClass('bg-success bg-info bg-warning bg-danger text-dark')
            .addClass(frequencyDisplay.badgeClass);
        $('#gh-freq-badge')
            .text(freq.toFixed(2) + ' Hz')
            .attr(
                'title',
                frequencyDisplay.source
                    ? `Messquelle: ${frequencyDisplay.source}${ageText}`
                    : `Bestätigte Live-Messung${ageText}`
            );
        $('#gh-freq-text')
            .empty()
            .append(
                $('<span>')
                    .addClass(frequencyDisplay.textClass)
                    .append($('<i>').addClass(`fas ${frequencyDisplay.iconClass} me-1`))
                    .append(document.createTextNode(frequencyDisplay.message))
            );
    } else {
        $('#gh-freq-marker').hide();
        $('#gh-freq-badge')
            .text('-- Hz')
            .removeClass('bg-success bg-info bg-warning bg-danger text-dark')
            .addClass('bg-secondary')
            .attr('title', 'Keine bestätigte Live-Messung');
        $('#gh-freq-text').text(frequencyDisplay.message);
    }


    // Lese Absicherung (Default 63A) aus globaler PHP Konfiguration
    const maxAmps = typeof GRID_MAX_AMPS !== 'undefined' ? parseFloat(GRID_MAX_AMPS) : 63;
    const maxWattsPerPhase = maxAmps * 230;

    $('#gh-max-scale').text(`Max: ${maxAmps}A`);

    const alertEl = $('#gridHealthAlert');
    const iconEl = $('#gridHealthIcon');
    const titleEl = $('#gridHealthTitle');
    const textEl = $('#gridHealthText');

    alertEl.removeClass('alert-success alert-warning alert-danger bg-success bg-warning bg-danger text-white text-dark text-light border');
    iconEl.removeClass('fa-check-circle fa-exclamation-triangle fa-skull-crossbones text-success text-warning text-danger');

    const phaseValues = liveGridPhaseValues(data);
    if (!phaseValues) {
        $('#gh-l1-w, #gh-l2-w, #gh-l3-w').text('-- W');
        $('#gh-l1-a, #gh-l2-a, #gh-l3-a').text('-- A');
        $('#gh-l1-bar, #gh-l2-bar, #gh-l3-bar')
            .css('width', '0%')
            .removeClass('bg-info bg-success bg-warning bg-danger text-dark text-white')
            .addClass('bg-secondary');
        alertEl.addClass('alert-success bg-dark text-light border');
        iconEl.addClass('fa-check-circle text-success');
        titleEl.text('Netzpunkt OK');
        textEl.html('Die Gesamt-Netzleistung ist verfügbar.<br><span class="small text-muted">Optionale PM-Netzphasen werden von diesem E3DC/PM-Index nicht geliefert.</span>');
    } else {
        const [l1, l2, l3] = phaseValues;
        const absL1 = Math.abs(l1);
        const absL2 = Math.abs(l2);
        const absL3 = Math.abs(l3);

        const l1a = (absL1 / 230).toFixed(1);
        const l2a = (absL2 / 230).toFixed(1);
        const l3a = (absL3 / 230).toFixed(1);

        const l1pct = Math.min(100, Math.max(0, (absL1 / maxWattsPerPhase) * 100));
        const l2pct = Math.min(100, Math.max(0, (absL2 / maxWattsPerPhase) * 100));
        const l3pct = Math.min(100, Math.max(0, (absL3 / maxWattsPerPhase) * 100));

        $('#gh-l1-w').text(`${Math.round(l1)} W`);
        $('#gh-l2-w').text(`${Math.round(l2)} W`);
        $('#gh-l3-w').text(`${Math.round(l3)} W`);

        $('#gh-l1-a').text(`${l1a} A`);
        $('#gh-l2-a').text(`${l2a} A`);
        $('#gh-l3-a').text(`${l3a} A`);

        $('#gh-l1-bar').css('width', `${l1pct}%`);
        $('#gh-l2-bar').css('width', `${l2pct}%`);
        $('#gh-l3-bar').css('width', `${l3pct}%`);

        const setBarColor = (id, pct, val) => {
            let bg = 'bg-info';
            if (pct > 95) bg = 'bg-danger';
            else if (pct > 80) bg = 'bg-warning text-dark';
            else if (val < -100) bg = 'bg-success';

            $(id).removeClass('bg-info bg-success bg-warning bg-danger bg-secondary text-dark text-white').addClass(bg);
        };

        setBarColor('#gh-l1-bar', l1pct, l1);
        setBarColor('#gh-l2-bar', l2pct, l2);
        setBarColor('#gh-l3-bar', l3pct, l3);

        // Schieflast-Kalkulation (Diff zwischen höchster am stärksten belasteter Phase vs geringster rel. Netzrichtung)
        // Einfache Version: Max |Phase| - Min |Phase|
        // Achtung: Wenn Phase 1 = -4000 (Einspeisung) und Phase 2 = +4000 (Bezug) -> echtes Delta ist 8000W! (Normgerecht).
        const phases = [l1, l2, l3];
        const maxP = Math.max(...phases);
        const minP = Math.min(...phases);
        const schief = Math.abs(maxP - minP);

        // > 4600W (4.6kVA) ist die deutsche Schieflastgrenze
        if (schief > 4600) {
            alertEl.addClass('alert-danger bg-danger text-white');
            iconEl.addClass('fa-exclamation-triangle');
            titleEl.text('Schieflast Warnung!');
            textEl.html(`Asymmetrie von <b>${Math.round(schief)} W</b> erkannt! (VDE-Normgrenze > 4.6 kW)`);
        } else if (schief > 3500) {
            alertEl.addClass('alert-warning bg-warning text-dark');
            iconEl.addClass('fa-exclamation-triangle');
            titleEl.text('Auslastung erhöht');
            textEl.html(`Zunehmende Asymmetrie (${Math.round(schief)} W) auf den Phasen.`);
        } else {
            alertEl.addClass('alert-success bg-dark text-light border');
            $('border').addClass('border-secondary');
            iconEl.addClass('fa-check-circle text-success');
            titleEl.text('Netzsymmetrie OK');
            textEl.html(`Die Phasen sind gut ausbalanciert.<br><span class="small text-muted">Max. Delta: ${Math.round(schief)} W</span>`);
        }
    }

    // Wallbox-Phasen (nur einblenden falls WB läuft oder connected)
    const wbActive = typeof data.wb_p1 !== 'undefined' && (Math.abs(data.wb_p1) > 20 || Math.abs(data.wb_p2) > 20 || Math.abs(data.wb_p3) > 20);
    if (wbActive) {
        $('#gh-wb-container').show();
        $('#gh-wb-l1').text(`${Math.round(data.wb_p1)} W`);
        $('#gh-wb-l2').text(`${Math.round(data.wb_p2)} W`);
        $('#gh-wb-l3').text(`${Math.round(data.wb_p3)} W`);

        let phasesActive = 0;
        if (Math.abs(data.wb_p1) > 50) phasesActive++;
        if (Math.abs(data.wb_p2) > 50) phasesActive++;
        if (Math.abs(data.wb_p3) > 50) phasesActive++;

        let mBadge = $('#gh-wb-mode');
        mBadge.text(`${phasesActive}p Laden`);
        if (phasesActive === 1) mBadge.removeClass('bg-success bg-primary').addClass('bg-warning text-dark');
        else mBadge.removeClass('bg-warning text-dark').addClass('bg-primary');
    } else {
        $('#gh-wb-container').hide();
    }
}

// ============================================================
// Ladekurven-Chart Modal
// ============================================================
let _storageCurveChartInstance = null;
let _directMarketingTrajectoryChartInstance = null;
let _storageCurveChartScriptLoading = false;
let _storageCurvePendingSocPoints = [];

function _fmtStoragePct(value) {
    return value !== undefined && value !== null && !isNaN(parseFloat(value))
        ? parseFloat(value).toFixed(1) + '%'
        : '--';
}

function _storageCurveInterp(curve, ts) {
    if (!Array.isArray(curve) || curve.length === 0) return null;
    if (ts <= curve[0].ts) return curve[0].soc;
    if (ts >= curve[curve.length - 1].ts) return curve[curve.length - 1].soc;
    for (let i = 0; i < curve.length - 1; i++) {
        if (ts >= curve[i].ts && ts <= curve[i + 1].ts) {
            const duration = Math.max(1, curve[i + 1].ts - curve[i].ts);
            const frac = (ts - curve[i].ts) / duration;
            return curve[i].soc + (curve[i + 1].soc - curve[i].soc) * frac;
        }
    }
    return null;
}

function _storageActiveCurveTarget(meta, socNow = null, ts = Date.now()) {
    const floor = _storageCurveInterp(window._storageSocMinCurve || [], ts);
    const target = _storageCurveInterp(window._storageSollCurve || [], ts);
    const ceiling = _storageCurveInterp(window._storageSocCeilingCurve || [], ts);
    const soc = socNow !== null && socNow !== undefined && !isNaN(parseFloat(socNow))
        ? parseFloat(socNow)
        : null;
    if (soc !== null && floor !== null && soc < floor - 0.3) {
        return {
            mode: 'floor_catchup',
            label: `Unterkante ${_fmtStoragePct(floor)}`,
            colorClass: 'text-success',
            floor,
            target,
            ceiling,
            soc
        };
    }
    if (soc !== null && ceiling !== null && soc > ceiling + 0.3) {
        return {
            mode: 'ceiling_hold',
            label: `Oberkante ${_fmtStoragePct(ceiling)}`,
            colorClass: 'text-warning',
            floor,
            target,
            ceiling,
            soc
        };
    }
    if (target !== null) {
        return {
            mode: 'target_curve',
            label: `Sollkurve ${_fmtStoragePct(target)}`,
            colorClass: 'text-info',
            floor,
            target,
            ceiling,
            soc
        };
    }
    if (floor !== null) {
        return {
            mode: 'floor',
            label: `Unterkante ${_fmtStoragePct(floor)}`,
            colorClass: 'text-success',
            floor,
            target,
            ceiling,
            soc
        };
    }
    return {
        mode: 'none',
        label: '--',
        colorClass: 'text-muted',
        floor,
        target,
        ceiling,
        soc
    };
}

function _setStorageActiveTargetBadge(activeTarget) {
    const el = document.getElementById('sc-active-target');
    if (!el) return;
    el.textContent = activeTarget && activeTarget.label ? activeTarget.label : '--';
    el.classList.remove('text-success', 'text-warning', 'text-info', 'text-muted');
    el.classList.add((activeTarget && activeTarget.colorClass) || 'text-muted');
    el.title = activeTarget && activeTarget.mode === 'floor_catchup'
        ? 'Ist-SoC liegt unter der Zielkorridor-Unterkante; die aktuelle Ladung baut zuerst diese Unterkante auf.'
        : 'Aktiver Bezugspunkt der Speicherregelung im Ladekurvenbild.';
}

function _formatStorageReason(reason) {
    if (!reason) return 'Noch kein Reglerstatus vorhanden.';
    const text = String(reason);
    const field = name => {
        const m = text.match(new RegExp(name + '=([^|\\s]+)'));
        return m ? m[1].trim() : null;
    };
    const bracketPct = name => {
        const m = text.match(new RegExp('\\[' + name + '\\s+([0-9.,]+)%\\]'));
        return m ? m[1].replace('.', ',') + ' Prozentpunkte' : null;
    };
    const time = (text.match(/^\[([0-9:]+)\]/) || [])[1] || '--:--';
    const soc = field('SOC');
    const target = field('Ziel');
    const grid = field('Grid');
    const pv = field('PV');
    const state = text.includes('FREILAUF') ? 'Freilauf' : (text.split('] ')[1] || text).split('|')[0].trim();

    if (text.includes('PRICE_BOOST_GRID')) {
        return `${time}: Preis-Boost aktiv. Der Speicher wird im günstigen Stromfenster gezielt geladen und darf dafür Netzstrom nutzen; ` +
            `Wallbox und Wärmepumpe werden nur freigegeben, wenn sie in der Konfiguration erlaubt sind. ` +
            `Nach dem Preisfenster endet der Boost automatisch und die normale Ladekurve übernimmt wieder. ` +
            `Aktuell: SoC ${soc || '--'}, Ziel ${target || '--'}, PV ${pv || '--'}, Netz ${grid || '--'}.`;
    }

    if (text.includes('AUTO-RUHE') || text.includes('TL_AUTO_QUIET')) {
        return `${time}: Auto-Ruhe aktiv. Die sichere Ladeleistung ist gerade zu klein oder der Netzpunkt ist zu unruhig. ` +
            `Der E3DC darf kurz autonom regeln; sobald genug PV-Reserve da ist, folgt der Speicher wieder der Ladekurve. ` +
            `Aktuell: SoC ${soc || '--'}, Ziel ${target || '--'}, PV ${pv || '--'}, Netz ${grid || '--'}.`;
    }

    if (text.includes('KURVEN-AUTO') || text.includes('PV-Einbruch') || text.includes('tl_curve_auto_relief')) {
        return `${time}: Kurven-Auto aktiv. Die PV-Leistung ist im Vergleich zur Prognose stark eingebrochen. ` +
            `E3DC-AUTO ist freigegeben; Wallbox-Entlastung und Kurven-Dump bleiben gesperrt. ` +
            `Aktuell: SoC ${soc || '--'}, Ziel ${target || '--'}, PV ${pv || '--'}, Netz ${grid || '--'}.`;
    }

    if (text.includes('AUTO-FREIGABE') || text.includes('TL_AUTO_RELEASE') || text.includes('TL-AUTO')) {
        return `${time}: Auto-Freigabe aktiv. Das Tagesziel ist laut Prognose nicht mehr sicher erreichbar oder heute kommt keine relevante PV mehr. ` +
            `E3DC-AUTO ist freigegeben, statt die Ladekurve zu erzwingen. ` +
            `Aktuell: SoC ${soc || '--'}, Ziel ${target || '--'}, PV ${pv || '--'}, Netz ${grid || '--'}.`;
    }

    if (text.includes('FREILAUF')) {
        const silent = text.includes('kein Senden');
        return `${time}: ${state}. Der Speicher liegt bei ${soc || '--'} und darf autonom arbeiten; ` +
            `${silent ? 'es wird kein neuer RSCP-Befehl gesendet.' : 'der E3DC ist freigegeben.'} ` +
            `Ziel ist ${target || '--'}, PV ${pv || '--'}, Netz ${grid || '--'}.`;
    }

    if (text.includes('WB-KURVENENTLASTUNG') || text.includes('WB-Kurvenentlastung') || text.includes('tl_brake_wb_relief_guard')) {
        return `${time}: WB-Kurvenentlastung aktiv. Der Speicher liegt oberhalb der Sollkurve und stuetzt die Wallbox ruhig am Netzpunkt.`;
    }

    if (text.includes('KURVEN-BREMSE') || text.includes('TL-BREMSE')) {
        return `${time}: Ladekurven-Bremse aktiv. Der Speicher ist oberhalb der Sollkurve, daher wird nicht weiter aktiv geladen, solange kein Abregelschutz greift.`;
    }

    if (text.includes('ABREGELSCHUTZ')) {
        const nowLag = bracketPct('Kurvennachlauf');
        const targetLag = bracketPct('Zielnachlauf') || bracketPct('TL-Zielnachlauf');
        let msg = `${time}: Abregelschutz aktiv. Der Speicher nimmt PV-Spitzen auf, damit weniger Ertrag an der Anlagen- oder WR-Grenze verloren geht.`;
        if (targetLag) {
            msg += ` Zielnachlauf: Der Speicher liegt ${targetLag} unter dem nächsten Zwischenziel, deshalb nutzt die Regelung zusätzlichen PV-Überschuss zum Aufholen.`;
        } else if (nowLag) {
            msg += ` Kurvennachlauf: Der Speicher liegt ${nowLag} unter der aktuellen Sollkurve, deshalb wird aktiv zur Kurve aufgeladen.`;
        }
        if (text.includes('[Rampe') || text.includes('[Hysterese]')) {
            msg += ' Rampe und Hysterese glaetten den Sollwert, damit die Ladung ruhig bleibt.';
        }
        return msg;
    }

    if (text.includes('KURVEN-HALT') || text.includes('TL-IDLE')) {
        return `${time}: Kurven-Halt aktiv. Der Speicher liegt am nächsten Kurvenziel; es wird kein aktiver Laderahmen gesetzt, solange kein Abregelschutz oder Netzwächter eingreifen muss.`;
    }

    if (text.includes('KURVEN-HALTEWAECHTER')) {
        return `${time}: Kurven-Haltewaechter aktiv. Kurzer Netzbezug wurde erkannt, daher darf der Speicher gegensteuern, statt starr im Halt zu bleiben.`;
    }

    if (text.includes('KURVEN-DUMP') || text.includes('TL-AUTODUMP')) {
        return `${time}: Kurven-Entladung aktiv. Der Speicher liegt deutlich oberhalb der Kurve und gibt kontrolliert Energie frei.`;
    }

    if (text.includes('PRE-DISCH')) {
        return `${time}: Pre-Dump aktiv. Der Speicher schafft vor der PV-Spitze gezielt Platz für späteren Ertrag.`;
    }

    return text;
}

function _renderStorageCurveExplanation(meta, reason) {
    const box = document.getElementById('sc-explain-box');
    if (!box) return;
    const dayLabel = meta.display_day_label || 'Heute';
    const target = _fmtStoragePct(meta.target_soc);
    const morning = _fmtStoragePct(meta.config_morning_soc ?? meta.morning_target);
    const predumpMin = _fmtStoragePct(meta.config_predump_min_soc ?? meta.predump_min_soc);
    const predumpEnabled = !(
        meta.predump_enabled === false ||
        meta.config_predump_enabled === false ||
        String(meta.predump_enabled ?? meta.config_predump_enabled ?? '1') === '0'
    );
    const headroomDischargeEnabled = !(
        meta.config_headroom_discharge_enabled === false ||
        meta.config_headroom_discharge_enabled === 0 ||
        String(meta.config_headroom_discharge_enabled ?? '1').toLowerCase() === 'false' ||
        String(meta.config_headroom_discharge_enabled ?? '1') === '0'
    );
    const curveMeta = meta.target_curve_meta || {};
    const wallboxTargetActive = curveMeta.wallbox_target_soc_active === true;
    const wallboxTargetSoc = wallboxTargetActive ? _fmtStoragePct(curveMeta.wallbox_target_soc) : '';
    const wallboxFloorActive = curveMeta.wallbox_floor_soc_active === true;
    const wallboxFloorSoc = wallboxFloorActive ? _fmtStoragePct(curveMeta.wallbox_floor_soc) : '';
    const weatherReserveActive = meta.weather_reserve_active === true || curveMeta.weather_reserve_active === true;
    const weatherReserveNeedWh = parseFloat(meta.weather_reserve_need_wh ?? curveMeta.weather_reserve_need_wh ?? 0) || 0;
    const weatherReserveNeedText = weatherReserveNeedWh > 0 ? `${(weatherReserveNeedWh / 1000).toFixed(1)} kWh` : '';
    const weatherReserveTarget = meta.planning_target_soc ?? curveMeta.planning_target_soc;
    const weatherBaseTarget = meta.target_soc ?? curveMeta.config_target_soc;
    const predumpDumpWh = parseFloat(meta.predump_dump_wh ?? curveMeta.predump_dump_wh ?? 0) || 0;
    const predumpKwh = `${(predumpDumpWh / 1000).toFixed(1)} kWh`;
    const predumpReason = String(meta.predump_reason ?? curveMeta.predump_reason ?? '').trim();
    const predumpCurveHold = predumpDumpWh <= 250
        && /(kurvenpuffer|adaptive ladekurve|pv-druckfenster|pre-dump-rechnung|abregeldruck)/i.test(predumpReason);
    const adaptiveFloorSoc = meta.adaptive_soc_floor ?? curveMeta.adaptive_soc_floor;
    const adaptiveCeilingSoc = meta.adaptive_soc_ceiling ?? curveMeta.adaptive_soc_ceiling;
    const adaptiveStorageClass = String(meta.adaptive_storage_class ?? curveMeta.adaptive_storage_class ?? '').trim();
    const adaptiveStorageKwh = parseFloat(meta.adaptive_storage_kwh ?? curveMeta.adaptive_storage_kwh ?? 0) || 0;
    const adaptiveComfortSoc = meta.adaptive_comfort_soc ?? curveMeta.adaptive_comfort_soc;
    const adaptiveComfortFloorSoc = meta.adaptive_comfort_floor_soc ?? curveMeta.adaptive_comfort_floor_soc;
    const adaptiveComfortActive = meta.adaptive_comfort_active === true || curveMeta.adaptive_comfort_active === true;
    const adaptiveComfortLimited = meta.adaptive_comfort_limited_by_headroom === true || curveMeta.adaptive_comfort_limited_by_headroom === true;
    const adaptiveHeadroomRequiredWh = parseFloat(meta.adaptive_headroom_required_wh ?? curveMeta.adaptive_headroom_required_wh ?? 0) || 0;
    const adaptiveHeadroomBufferWh = parseFloat(meta.adaptive_headroom_buffer_wh ?? curveMeta.adaptive_headroom_buffer_wh ?? 0) || 0;
    const curtailmentPressureWh = parseFloat(meta.curtailment_pressure_wh ?? curveMeta.curtailment_pressure_wh ?? 0) || 0;
    const adaptiveHeadroomOpenWh = Math.max(0, adaptiveHeadroomRequiredWh);
    const curtailmentUnavoidableWh = parseFloat(meta.curtailment_unavoidable_wh ?? curveMeta.curtailment_unavoidable_wh ?? meta.predump_unavoidable_clipping_wh ?? curveMeta.predump_unavoidable_clipping_wh ?? 0) || 0;
    const headroomReserveLivePvW = parseFloat(meta.headroom_reserve_live_pv_w ?? curveMeta.headroom_reserve_live_pv_w ?? 0) || 0;
    const headroomReserveForecastNowW = parseFloat(meta.headroom_reserve_forecast_now_w ?? curveMeta.headroom_reserve_forecast_now_w ?? 0) || 0;
    const headroomReserveForecastRatioRaw = parseFloat(meta.headroom_reserve_forecast_ratio ?? curveMeta.headroom_reserve_forecast_ratio ?? 0) || 0;
    const headroomReserveForecastRatio = headroomReserveForecastRatioRaw > 0
        ? headroomReserveForecastRatioRaw
        : (headroomReserveForecastNowW > 0 ? headroomReserveLivePvW / headroomReserveForecastNowW : 0);
    const headroomReserveSource = String(meta.headroom_reserve_source ?? curveMeta.headroom_reserve_source ?? '').trim();
    const forecastLooksLow = headroomReserveLivePvW >= 1200 && (
        (headroomReserveForecastNowW >= 100 && headroomReserveForecastRatio >= 1.6) ||
        (headroomReserveForecastNowW < 500 && headroomReserveSource.includes('live_cloud_edge'))
    );
    const headroomDischargeTodayWh = parseFloat(meta.headroom_discharge_today_wh ?? 0) || 0;
    const headroomDischargeLimitWh = parseFloat(meta.headroom_discharge_daily_limit_wh ?? 0) || 0;
    const headroomDischargeRemainingWh = parseFloat(meta.headroom_discharge_daily_remaining_wh ?? 0) || 0;
    const headroomDischargeCooldownRemainingS = parseFloat(meta.headroom_discharge_cooldown_remaining_s ?? 0) || 0;
    const headroomDischargeDailyBlocked = meta.headroom_discharge_daily_blocked === true;
    const headroomDischargeCooldownActive = meta.headroom_discharge_cooldown_active === true || headroomDischargeCooldownRemainingS > 0;
    const headroomDischargeBlockedReason = String(meta.headroom_discharge_blocked_reason ?? '').trim();
    const latestChargeStartTs = parseFloat(meta.latest_charge_start_ts ?? curveMeta.latest_charge_start_ts ?? 0) || 0;
    const eveningShortfallWh = parseFloat(meta.evening_shortfall_wh ?? curveMeta.evening_shortfall_wh ?? 0) || 0;
    const whKwh = wh => `${(Math.max(0, parseFloat(wh) || 0) / 1000).toFixed(1)} kWh`;
    const headroomSocRaw = parseFloat(window._storageControlSoc ?? window._storageLiveSoc ?? meta.control_soc ?? meta.soc ?? curveMeta.control_soc ?? NaN);
    const physicalHeadroomFreeWh = Number.isFinite(headroomSocRaw) && adaptiveStorageKwh > 0
        ? Math.max(0, (100 - headroomSocRaw) * adaptiveStorageKwh * 10)
        : 0;
    const physicalHeadroomText = physicalHeadroomFreeWh > 0
        ? ` Aktuell sind bis 100% etwa ${whKwh(physicalHeadroomFreeWh)} Akkuplatz frei. Dieser physische Platz ist eine Momentaufnahme; entscheidend ist, dass der benötigte Headroom bis zum PV-Druckfenster nicht vorher belegt wird.`
        : '';
    const headroomBufferText = adaptiveHeadroomBufferWh > 0
        ? ` In diesem Freihaltewert stecken ${whKwh(adaptiveHeadroomBufferWh)} Regelpuffer.`
        : '';
    const fmtPlanTs = ts => {
        const raw = parseFloat(ts || 0) || 0;
        if (raw <= 0) return '';
        return new Date(raw > 10000000000 ? raw : raw * 1000).toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit'});
    };
    const hardPredumpActive = meta.hard_predump_enabled === true || curveMeta.hard_predump_enabled === true;
    const predumpGridText = hardPredumpActive
        ? 'Beim Komfort-/Fixziel-Pre-Dump wird Netz-Dump nur genutzt, wenn Netz-Fallback (Komfort) aktiv ist.'
        : 'Beim Abregelschutz bleibt Netz-Dump ein begrenzter Fallback, wenn lokale Verbraucher nicht reichen.';
    const predumpPlanText = predumpDumpWh > 250
        ? `Speicherplatz-Bedarf laut Prognose: ${predumpKwh}. Das ist kein Garantiewert: aktiv entladen wird nur im Pre-Dump-Fenster und bevorzugt über erlaubte Verbraucher wie Wärmepumpe, Wallbox oder Heizstab. ${predumpGridText}`
        : (predumpCurveHold
            ? 'Aktives Vorab-Entladen läuft nicht; die Kurve hält den benötigten Platz.'
            : 'Heute ist kein aktives Vorab-Entladen eingeplant.');
    let headroomDischargeText = headroomDischargeEnabled
        ? 'Aktive Headroom-Entladung: eingeschaltet. Entlade-Impulse (DISCH) sind nur bei laufender PV, Exportreserve und SoC oberhalb der aktuellen Unterkante erlaubt.'
        : 'Aktive Headroom-Entladung: ausgeschaltet. Die Reserve wirkt nur über Ladegrenze und Kurve.';
    if (headroomDischargeEnabled && headroomDischargeLimitWh > 0) {
        headroomDischargeText += ` Heute genutzt: ${whKwh(headroomDischargeTodayWh)} von ${whKwh(headroomDischargeLimitWh)}.`;
        if (headroomDischargeDailyBlocked || headroomDischargeRemainingWh <= 0) {
            headroomDischargeText += ' Heute ist das Entlade-Limit erreicht.';
        } else {
            headroomDischargeText += ` Verbleibend: ${whKwh(headroomDischargeRemainingWh)}.`;
        }
    }
    if (headroomDischargeEnabled && headroomDischargeCooldownActive) {
        headroomDischargeText += ` Pause noch ${Math.ceil(headroomDischargeCooldownRemainingS / 60)} min.`;
    } else if (headroomDischargeEnabled && headroomDischargeBlockedReason === 'cooldown') {
        headroomDischargeText += ' Pause aktiv.';
    }
    const pvForecastWarningText = forecastLooksLow
        ? ` Plausibilitäts-Hinweis: Live-PV ${Math.round(headroomReserveLivePvW)} W liegt deutlich über Prognose jetzt ${Math.round(headroomReserveForecastNowW)} W${headroomReserveForecastRatio > 0 ? ` (Faktor ${headroomReserveForecastRatio.toFixed(1)})` : ''}. Prüfe Ausrichtung, kWp und Solcast-Site.`
        : '';
    let weatherReserveText = 'Heute kein Pre-Dump: schlechte Prognose. Energie bleibt im Speicher; die Regelung fährt die Schlechtwetter-Kurve.';
    if (weatherReserveNeedText) weatherReserveText += ` Erwartetes 48h-Defizit: ${weatherReserveNeedText}.`;
    if (weatherBaseTarget != null && weatherReserveTarget != null && Math.abs(parseFloat(weatherReserveTarget) - parseFloat(weatherBaseTarget)) > 0.2) {
        weatherReserveText += ` Speicherziel wird vorsorglich von ${parseFloat(weatherBaseTarget).toFixed(0)}% auf ${parseFloat(weatherReserveTarget).toFixed(0)}% angehoben.`;
    }
    const effectiveProjectionHidden = meta.clear_classical_curves === true;
    const canReachTarget = effectiveProjectionHidden
        ? null
        : !(meta.can_reach_target === false || meta.can_reach_target === 0 || meta.can_reach_target === '0');
    const targetReachState = String(meta.target_reach_state ?? curveMeta.target_reach_state ?? (canReachTarget ? 'reachable' : 'unreachable_auto'));
    const targetReachReason = String(meta.target_reach_reason ?? curveMeta.target_reach_reason ?? '').trim();
    const targetReachRecheckActive = !(
        meta.target_reach_recheck_active === false ||
        meta.target_reach_recheck_active === 0 ||
        meta.target_reach_recheck_active === '0'
    );
    const targetReachStatusOnly = !(
        meta.target_reach_status_only === false ||
        meta.target_reach_status_only === 0 ||
        meta.target_reach_status_only === '0'
    );
    const targetReachStableS = parseFloat(meta.target_reach_stable_s ?? curveMeta.target_reach_stable_s ?? NaN);
    const fmtDuration = seconds => {
        const raw = Math.max(0, parseFloat(seconds) || 0);
        if (raw < 60) return `${Math.round(raw)} s`;
        if (raw < 7200) return `${Math.round(raw / 60)} min`;
        return `${(raw / 3600).toFixed(1)} h`;
    };
    const targetReachStableText = Number.isFinite(targetReachStableS)
        ? ` Status seit ${fmtDuration(targetReachStableS)} stabil.`
        : '';
    const targetReachSurplusWh = parseFloat(meta.target_reach_surplus_wh ?? curveMeta.target_reach_surplus_wh ?? NaN);
    const targetReachRequiredWh = parseFloat(meta.target_reach_required_wh ?? curveMeta.target_reach_required_wh ?? NaN);
    const targetReachEnergyText = Number.isFinite(targetReachSurplusWh) && Number.isFinite(targetReachRequiredWh)
        ? ` Rest-PV ${whKwh(targetReachSurplusWh)}, Bedarf mit Reserve ${whKwh(targetReachRequiredWh)}.`
        : '';
    const simMaxSoc = _fmtStoragePct(meta.sim_max_soc_pct ?? meta.max_soc_pct);
    const reachableSoc = _fmtStoragePct(meta.max_reachable_soc ?? (canReachTarget ? meta.target_soc : meta.max_soc_pct));
    const startAnchorTs = meta.start_anchor_ts ?? curveMeta.start_anchor_ts ?? meta.ladestart_ts;
    const startAnchorTime = meta.start_anchor_t || curveMeta.start_anchor_t || fmtPlanTs(startAnchorTs);
    const startAnchorSoc = meta.start_anchor_soc ?? curveMeta.start_anchor_soc ?? meta.ladestart_soc;
    const q = meta.q_ratio !== undefined && meta.q_ratio !== null ? parseFloat(meta.q_ratio) : null;
    let qText = 'normaler Kurvenverlauf';
    if (q !== null && !isNaN(q)) {
        if (meta.has_target_curve === false) {
            qText = 'Vorschau: Die echte Morgenkurve wird beim nächsten Tagesplan eingefroren.';
        } else if (q > 3.0) {
            qText = 'späte Kurve: viel PV im Verhältnis zum Speicher, daher bleibt vormittags länger Platz frei.';
        } else if (q < 0.5) {
            qText = 'frühe Kurve: kaum Reservebedarf für eine späte PV-Spitze, daher steigt das Soll früher an.';
        } else {
            qText = 'ausgewogene Kurve: Speicherplatz wird gehalten, aber nicht unnötig lange blockiert.';
        }
    }
    const activeTarget = _storageActiveCurveTarget(meta, window._storageControlSoc ?? window._storageLiveSoc);
    const curveStatus = effectiveProjectionHidden
        ? 'Die Direktvermarktung führt den aktuellen Slot; eine klassische Sollkurve wird erst nach bestätigter Freigabe wieder angezeigt.'
        : (window._storageSollCurveSource === 'curve_anchors_fallback'
        ? 'Die blaue Sollkurve wird aus den gültigen, eingefrorenen Kurvenankern rekonstruiert, weil die slotweise Zielprojektion in diesem Snapshot fehlt. Die Regelung selbst bleibt unverändert.'
        : (meta.has_target_curve === false
        ? 'Für diesen Tag liegt noch keine eingefrorene Sollkurve vor; angezeigt wird die Vorschau aus der Simulation.'
        : (targetReachState === 'unreachable_auto'
            ? 'Blaue Linie: geplante Ladekurve bis zum Tagesziel. Sie bleibt sichtbar, auch wenn das Tagesziel laut aktueller Prognose nicht erreichbar ist; die voraussichtliche Speicherladung zeigt separat, ob der SoC dieser Planung folgt.'
            : 'Blaue Linie: geplanter Tagespfad bis zum Freilauf-SoC. Sie ist kein harter Sekunden-Sollwert; die Regelung arbeitet mit Zielkorridor, Hysterese und ruhigem Nachladen. Liegt der Speicher unter der aktuellen Unterkante, wird zuerst in den Zielkorridor zurückgeladen. Ist das Tagesziel mit sicherer Rest-PV erreichbar, darf der gleitende Prognosehorizont die Ladung entspannen.')));
    const hasAdaptiveBand = adaptiveFloorSoc != null && adaptiveCeilingSoc != null
        && !isNaN(parseFloat(adaptiveFloorSoc)) && !isNaN(parseFloat(adaptiveCeilingSoc));
    const latestChargeText = fmtPlanTs(latestChargeStartTs);
    const targetReachBaseText = targetReachReason || (canReachTarget
        ? 'Tagesziel erreichbar: Zielkurve aktiv. Die Prognose wird bei jedem Planlauf neu geprüft.'
        : 'Tagesziel aktuell nicht erreichbar: E3DC AUTO. Der E3DC nutzt realen PV-Überschuss autonom; Entladung bleibt geschützt. Die Prognose wird bei jedem Planlauf neu geprüft.');
    const targetReachServoText = targetReachStatusOnly
        ? ' Dieser Status schaltet nichts selbst; Rückkehr zur Kurve und AUTO-Freigabe bleiben beim zentralen Kurven-Servo.'
        : '';
    const targetReachRecheckText = targetReachRecheckActive
        ? ''
        : ' Achtung: laufende Prognose-Neubewertung ist nicht aktiv markiert.';
    const targetReachText = effectiveProjectionHidden
        ? (targetReachReason || 'Ziel, Erreichbarkeit und Ladeleistung bleiben bis zur bestätigten Wirkung unbekannt.')
        : targetReachState === 'unreachable_auto'
        ? `${targetReachBaseText} Restprognose bis ${reachableSoc}. Punktlandungs-Simulation: ${simMaxSoc}.${targetReachEnergyText}${targetReachStableText}${targetReachServoText}${targetReachRecheckText}`
        : `${targetReachBaseText} Fachlich erreichbar: ${reachableSoc}. Punktlandungs-Simulation: ${simMaxSoc}.${targetReachEnergyText}${targetReachStableText}${targetReachServoText}${targetReachRecheckText}`;

    const rows = [
        {icon:'fa-route', color:'#4dabf7', title:'Soll-Kurve', text: curveStatus},
        {
            icon:'fa-map-pin',
            color:'#38bdf8',
            title:'Startanker',
            text:`Startpunkt der heutigen Kurve: ${startAnchorTime || '--:--'} bei ${_fmtStoragePct(startAnchorSoc)}. Adaptive Unterkanten sind Diagnose-/Regelband, kein neuer Startanker; Unter- und Oberkanten verschieben diesen Startpunkt nicht.`
        },
        {icon:'fa-chart-line', color:'#a78bfa', title:'Prognose-SoC', text:'Violette Linie: erwarteter Speicherverlauf aus der Simulation. Bei künftigen Tagen kann sie eine Vorschau sein; die echte Sollkurve wird erst beim Tagesplan eingefroren.'},
        {icon:'fa-battery-half', color:'#51cf66', title:'Morgen-Puffer', text:`Eingestellte Startreserve: ${morning}. Liegt der Speicher morgens darunter, darf der E3DC autonom aufbauen.`},
        {
            icon:'fa-arrow-down-short-wide',
            color: predumpEnabled ? '#51cf66' : '#94a3b8',
            title: predumpEnabled && predumpCurveHold
                ? 'Pre-Dump/Kurvenpuffer'
                : (predumpEnabled ? 'Pre-Dump-Bedarf' : 'Pre-Dump aus'),
            text: predumpEnabled
                ? `Untergrenze für aktives Vorab-Entladen: ${predumpMin}. ${predumpPlanText} ${predumpReason || 'Kein aktiver Pre-Dump für diesen Plan.'}`
                : `Aktives Vorab-Entladen ist deaktiviert. Die Prognose nutzt ${predumpMin} nicht als Entladeziel und plant keinen Pre-Dump.`
        },
        {icon:'fa-sun', color: forecastLooksLow ? '#f59e0b' : '#ffc107', title:'PV-Prognose', text:`Gelbe Fläche: erwartete PV-Leistung für ${dayLabel}. Sehr kleine Dämmerungswerte unter 100 W werden im Diagramm ausgeblendet.${pvForecastWarningText}`},
        {icon:'fa-flag-checkered', color:'#22d3ee', title:effectiveProjectionHidden ? 'Wirksamer Speicherplan' : 'Tagesziel', text:effectiveProjectionHidden ? targetReachText : `Gewünschter Speicherstand zum Freilauf: ${target}. ${targetReachText}`},
        {icon:'fa-wave-square', color:'#38bdf8', title:'Kurvenform', text:qText}
    ];

    if (activeTarget.mode === 'floor_catchup') {
        rows.unshift({
            icon:'fa-crosshairs',
            color:'#22c55e',
            title:'Aktives Regelziel',
            text:`Ist-SoC ${_fmtStoragePct(activeTarget.soc)} liegt unter der aktuellen Unterkante ${_fmtStoragePct(activeTarget.floor)}. Deshalb lädt die Regelung zuerst ruhig in den Zielkorridor zurück; die blaue Tageskurve bleibt die Orientierung.`
        });
    } else if (activeTarget.mode === 'ceiling_hold') {
        rows.unshift({
            icon:'fa-crosshairs',
            color:'#f97316',
            title:'Aktives Regelziel',
            text:`Ist-SoC ${_fmtStoragePct(activeTarget.soc)} liegt oberhalb der Oberkante ${_fmtStoragePct(activeTarget.ceiling)}. Die Regelung hält Speicherplatz frei, solange kein Abregelschutz Vorrang hat.`
        });
    }

    if (hasAdaptiveBand) {
        rows.splice(1, 0, {
            icon:'fa-layer-group',
            color:'#22c55e',
            title:'Zielkorridor',
            text:`Aktueller Regelkorridor: Unterkante ${_fmtStoragePct(adaptiveFloorSoc)} verhindert zu spätes Laden, Oberkante ${_fmtStoragePct(adaptiveCeilingSoc)} hält Platz für PV-Spitzen frei. Liegt der Ist-SoC darunter, wird zuerst in diesen Korridor zurückgeladen.`
        });
    }

    if (adaptiveStorageClass) {
        const isLargeStorage = adaptiveStorageClass === 'large';
        const comfortText = adaptiveComfortActive && adaptiveComfortFloorSoc != null
            ? (adaptiveComfortLimited
                ? `Komfortwunsch ${adaptiveComfortSoc != null ? _fmtStoragePct(adaptiveComfortSoc) : '--'}; wegen Headroom auf ${_fmtStoragePct(adaptiveComfortFloorSoc)} begrenzt.`
                : `Komfort-Unterkante ${_fmtStoragePct(adaptiveComfortFloorSoc)} aktiv.`)
            : (isLargeStorage
                ? 'Kein zusätzliches Komfortband aktiv, weil Headroom oder Kurve bereits vorgibt.'
                : 'Kleine Speicher folgen früher der Zielkurve, damit das Tagesziel nicht zu spät kommt.');
        rows.splice(hasAdaptiveBand ? 2 : 1, 0, {
            icon:'fa-battery-three-quarters',
            color: isLargeStorage ? '#22c55e' : '#f59e0b',
            title: isLargeStorage ? 'Großer Speicher' : 'Kleiner Speicher',
            text:`Kapazität ${adaptiveStorageKwh > 0 ? adaptiveStorageKwh.toFixed(1) + ' kWh' : '-- kWh'}. ${comfortText}`
        });
    }

    rows.splice(hasAdaptiveBand ? 2 : 1, 0, {
        icon:'fa-shield-halved',
        color:'#38bdf8',
        title:'Adaptiver Headroom',
        text:`Prognostizierter Abregeldruck ${whKwh(curtailmentPressureWh)} über den restlichen PV-Tag. Das ist das Risiko, nicht die Energie, die sofort freigemacht werden muss. Der Wert ist bereits nach Einspeiselimit und sicherem Grundverbrauch berechnet; kurzfristige Hausverbrauchs-Spitzen zählen nicht vollständig als sichere Senke. Aktuell sollen bis zum PV-Druckfenster etwa ${whKwh(adaptiveHeadroomOpenWh)} Speicherplatz nicht vorher belegt werden. Die adaptive Kurve hält den Zielkorridor dafür niedriger und begrenzt frühe Ladung.${headroomBufferText}${physicalHeadroomText} Wenn dieser Platz aktuell schon frei ist, muss die Regelung ihn vor allem erhalten; aktive Entladung ist nur eine begrenzte Zusatzmaßnahme, falls der SoC vor der PV-Spitze zu hoch liegt. ${headroomDischargeText} Nicht durch Speicher vermeidbarer Druck: ${whKwh(curtailmentUnavoidableWh)}.`
    });

    if (eveningShortfallWh > 0 || latestChargeStartTs > 0) {
        rows.splice(hasAdaptiveBand ? 3 : 2, 0, {
            icon:'fa-flag-checkered',
            color: eveningShortfallWh > 0 ? '#f59e0b' : '#22c55e',
            title: eveningShortfallWh > 0 ? 'Abendziel-Risiko' : 'Abendziel',
            text: eveningShortfallWh > 0
                ? `Dem Abendziel fehlen laut Restprognose ${whKwh(eveningShortfallWh)}. Spätester Ladestart: ${latestChargeText || 'jetzt'}.`
                : `Abendziel laut Restprognose erreichbar. Spätestens ab ${latestChargeText || '--'} müsste wieder geladen werden, falls bis dahin zu wenig Energie im Speicher ist.`
        });
    }

    if (wallboxTargetActive) {
        rows.splice(2, 0, {
            icon:'fa-charging-station',
            color:'#f59e0b',
            title:'WB-Rückfallziel',
            text:`Modus PV + Akku bis Untergrenze ist aktiv und das Tagesziel ist laut Restprognose nicht erreichbar. Die Kurve endet vorübergehend bei ${wallboxTargetSoc}, damit die Wallbox nur bis zur Hausakku-Reserve stützt. Netz bleibt aus.`
        });
    } else if (wallboxFloorActive) {
        rows.splice(2, 0, {
            icon:'fa-charging-station',
            color:'#f59e0b',
            title:'WB-Speicherboden',
            text:`Modus PV + Akku bis Untergrenze ist aktiv. Das Speicher-Tagesziel bleibt bei ${target}; die Wallbox-Stütze endet bei ${wallboxFloorSoc}. Netz bleibt aus.`
        });
    }

    if (weatherReserveActive) {
        rows.splice(3, 0, {
            icon:'fa-cloud-rain',
            color:'#f59e0b',
            title:'Pre-Dump pausiert',
            text: weatherReserveText
        });
    }

    const reasonText = String(reason || '');
    if (reasonText.includes('PRICE_BOOST_GRID')) {
        rows.push({
            icon:'fa-bolt',
            color:'#22c55e',
            title:'Preis-Boost',
            text:'Günstiges Stromfenster: Der Speicher darf gezielt mit Netzstrom laden; freigegebene Verbraucher dürfen mitlaufen. Nach Fensterende folgt wieder die Ladekurve.'
        });
    }
    if (reasonText.includes('Zielnachlauf') || reasonText.includes('TL-Zielnachlauf') || reasonText.includes('Kurvennachlauf')) {
        rows.push({
            icon:'fa-arrow-up',
            color:'#f59e0b',
            title:'Zielnachlauf',
            text:'Wenn der Speicher hinter der Sollkurve oder dem nächsten Zwischenziel liegt, nutzt der Abregelschutz freien PV-Überschuss zum Aufholen.'
        });
    }

    const explanationOrder = {
        'PV-Prognose': 10,
        'Tagesziel': 20,
        'Morgen-Puffer': 30,
        'Startanker': 40,
        'Kurvenform': 50,
        'Soll-Kurve': 60,
        'Prognose-SoC': 70,
        'Großer Speicher': 80,
        'Kleiner Speicher': 80,
        'Zielkorridor': 90,
        'Aktives Regelziel': 100,
        'Adaptiver Headroom': 110,
        'Pre-Dump-Bedarf': 120,
        'Pre-Dump/Kurvenpuffer': 120,
        'Pre-Dump aus': 120,
        'Pre-Dump pausiert': 125,
        'WB-Rückfallziel': 130,
        'WB-Speicherboden': 130,
        'Abendziel-Risiko': 140,
        'Preis-Boost': 150,
        'Zielnachlauf': 160
    };
    const orderedRows = rows
        .map((row, index) => ({...row, _order: explanationOrder[row.title] ?? 900, _index: index}))
        .sort((a, b) => (a._order - b._order) || (a._index - b._index));

    box.innerHTML = orderedRows.map(r => `
        <div class="d-flex gap-2 align-items-start mb-2">
            <span class="d-inline-flex align-items-center justify-content-center rounded-circle flex-shrink-0" style="width:24px;height:24px;border:1px solid ${r.color};color:${r.color};background:${r.color}18;">
                <i class="fas ${r.icon}" style="font-size:0.72rem;"></i>
            </span>
            <span><span class="fw-bold" style="color:${r.color};">${r.title}</span> <span class="text-muted">- ${r.text}</span></span>
        </div>
    `).join('');

    const reasonEl = document.getElementById('sc-reason-text');
    if (reasonEl) reasonEl.textContent = _formatStorageReason(reason);
}

function showStorageCurveModal() {
    const el = document.getElementById('storageCurveModal');
    if (!el) return;
    let m = bootstrap.Modal.getInstance(el);
    if (!m) m = new bootstrap.Modal(el);
    m.show();

    // Meta-Infos befüllen
    const meta = window._storagePlanMeta || {};
    const curveMeta = meta.target_curve_meta || {};
    const currentSoc = currentLiveSocForChart();
    const standardChartWrap = document.getElementById('sc-standard-chart-wrap');
    const directMarketingChartWrap = document.getElementById('sc-direct-marketing-chart-wrap');
    // Auch bei vollständigem DV-Plan bleibt die Standard-Ladekurve mit ihrer
    // PV-Ertragskurve sichtbar. Nur ihre geplante SoC-Linie wird ausgetauscht.
    if (standardChartWrap) standardChartWrap.style.display = '';
    if (directMarketingChartWrap) directMarketingChartWrap.style.display = 'none';
    $('#sc-current-soc').text(currentSoc !== null ? `${currentSoc.toFixed(1)}%` : '--%');
    $('#sc-modal-day').text(meta.display_day_label || 'Heute');
    if (curveMeta.wallbox_target_soc_active) {
        $('#sc-target-soc').text(_fmtStoragePct(meta.target_soc) + ' / WB ' + _fmtStoragePct(curveMeta.wallbox_target_soc));
    } else if (curveMeta.wallbox_floor_soc_active) {
        $('#sc-target-soc').text(_fmtStoragePct(meta.target_soc) + ' / WB-Boden ' + _fmtStoragePct(curveMeta.wallbox_floor_soc));
    } else {
        $('#sc-target-soc').text(_fmtStoragePct(meta.target_soc));
    }
    _setStorageActiveTargetBadge(_storageActiveCurveTarget(meta, currentSoc));
    $('#sc-morning-target').text(_fmtStoragePct(meta.config_morning_soc ?? meta.morning_target));
    $('#sc-predump-min').text(_fmtStoragePct(meta.config_predump_min_soc ?? meta.predump_min_soc));
    const predumpDumpWh = parseFloat(meta.predump_dump_wh ?? curveMeta.predump_dump_wh ?? 0) || 0;
    $('#sc-predump-kwh').text(`${(predumpDumpWh / 1000).toFixed(1)} kWh`);
    $('#sc-predump-kwh').toggleClass('text-warning', predumpDumpWh > 250).toggleClass('text-info', predumpDumpWh <= 250);
    const pvForecastKwh = parseFloat(meta.pv_forecast_kwh ?? curveMeta.pv_forecast_kwh ?? 0) || 0;
    const curtailmentPressureWh = parseFloat(meta.curtailment_pressure_wh ?? curveMeta.curtailment_pressure_wh ?? 0) || 0;
    const adaptiveHeadroomRequiredWh = parseFloat(meta.adaptive_headroom_required_wh ?? curveMeta.adaptive_headroom_required_wh ?? 0) || 0;
    const eveningShortfallWh = parseFloat(meta.evening_shortfall_wh ?? curveMeta.evening_shortfall_wh ?? 0) || 0;
    $('#sc-pv-forecast-kwh').text(pvForecastKwh > 0 ? `${pvForecastKwh.toFixed(1)} kWh` : '-- kWh');
    $('#sc-curtailment-kwh').text(`${(curtailmentPressureWh / 1000).toFixed(1)} kWh`);
    $('#sc-curtailment-kwh').toggleClass('text-warning', curtailmentPressureWh > 250).toggleClass('text-info', curtailmentPressureWh <= 250);
    $('#sc-headroom-kwh').text(`freihalten ${(adaptiveHeadroomRequiredWh / 1000).toFixed(1)} kWh / Druck ${(curtailmentPressureWh / 1000).toFixed(1)} kWh`);
    $('#sc-headroom-kwh').toggleClass('text-warning', adaptiveHeadroomRequiredWh > 250).toggleClass('text-info', adaptiveHeadroomRequiredWh <= 250);
    $('#sc-evening-risk').text(eveningShortfallWh > 0 ? `+${(eveningShortfallWh / 1000).toFixed(1)} kWh` : 'OK');
    $('#sc-evening-risk').toggleClass('text-warning', eveningShortfallWh > 0).toggleClass('text-success', eveningShortfallWh <= 0);
    const intermediateAnchors = Array.isArray(curveMeta.intermediate_anchors)
        ? curveMeta.intermediate_anchors
        : (curveMeta.noon_anchor_active ? [{soc: curveMeta.noon_anchor_soc, t: curveMeta.noon_anchor_t}] : []);
    if (intermediateAnchors.length) {
        $('#sc-noon-target').text(intermediateAnchors.map((anchor, idx) => {
            const soc = parseFloat(anchor.soc);
            const label = anchor.label || ('Z' + (idx + 1));
            return label + ' ' + (Number.isFinite(soc) ? soc.toFixed(1) + '%' : '--%') + ' @ ' + (anchor.t || '--:--');
        }).join(' · '));
        $('#sc-noon-wrap').show();
    } else {
        $('#sc-noon-wrap').hide();
    }
    const modalReachableSoc = _fmtStoragePct(meta.max_reachable_soc ?? meta.max_soc_pct);
    const modalSimSoc = _fmtStoragePct(meta.sim_max_soc_pct ?? meta.max_soc_pct);
    $('#sc-max-soc').text(modalReachableSoc === modalSimSoc ? modalReachableSoc : `${modalReachableSoc} / Sim ${modalSimSoc}`);
    if (meta.q_ratio !== undefined && meta.q_ratio !== null) {
        const q = parseFloat(meta.q_ratio);
        $('#sc-qratio').text(q.toFixed(2));
        $('#sc-qratio-wrap').css('color', q > 3.0 ? '#ffc107' : (q > 1.5 ? '#4dabf7' : '#51cf66'));
    }
    if (meta.ts) {
        const d = new Date(meta.ts * 1000);
        $('#sc-plan-ts').text(d.toLocaleTimeString('de-DE', {hour:'2-digit', minute:'2-digit'}) + ' Uhr');
    }
    _renderStorageCurveExplanation(meta, window._storageReason || '');
    renderDirectMarketingCurveSection(window._storageLiveData || null);

    const displayStart = meta.display_day_start ? parseInt(meta.display_day_start, 10) : null;
    const today0 = displayStart ? new Date(displayStart) : new Date();
    today0.setHours(0,0,0,0);

    if (_directMarketingTrajectoryChartInstance) {
        _directMarketingTrajectoryChartInstance.destroy();
        _directMarketingTrajectoryChartInstance = null;
    }

    const renderModalChart = () => _renderStorageCurveChart([]);
    $(el).one('shown.bs.modal', renderModalChart);
    renderModalChart();
    setTimeout(renderModalChart, 150);

    // Historische IST-SoC-Linie für die Standard-Ladekurve nachladen
    const historyUrl = 'get_live_json.php?storage_curve_history=1&day_start_ms='
        + encodeURIComponent(today0.getTime());
    $.getJSON(historyUrl, function(historyData) {
        const socPoints = Array.isArray(historyData && historyData.points)
            ? historyData.points
                .map(point => ({
                    ts: parseInt(point.ts, 10),
                    soc: parseFloat(point.soc),
                }))
                .filter(point => Number.isFinite(point.ts) && Number.isFinite(point.soc))
                .sort((a, b) => a.ts - b.ts)
            : [];
        _renderStorageCurveChart(socPoints);
    }).fail(function() {
        _renderStorageCurveChart([]);
    });


}

function _renderStorageCurveChart(socPoints) {
    const canvas = document.getElementById('storageCurveChart');
    if (!canvas) return;
    _storageCurvePendingSocPoints = Array.isArray(socPoints) ? socPoints : [];

    // Chart.js aus dem lokal mitgelieferten, lizenzierten Vendor-Bundle laden.
    function doRender() {
        const currentSocPoints = _storageCurvePendingSocPoints;
        if (_storageCurveChartInstance) {
            _storageCurveChartInstance.destroy();
            _storageCurveChartInstance = null;
        }

        const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
        const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.07)';
        const tickColor = isDark ? '#adb5bd' : '#6c757d';

        const sollCurve = window._storageSollCurve || [];
        const minCurve = window._storageSocMinCurve || [];
        const ceilingCurve = window._storageSocCeilingCurve || [];
        const simCurve  = window._storageSimCurve  || [];
        const curveAnchors = window._storageCurveAnchors || [];
        const meta = window._storagePlanMeta || {};
        const nowMs = Date.now();

        // Gemeinsame Zeitachse: Wir erzeugen ein festes 15-Minuten Raster für den ganzen Tag (00:00 - 24:00),
        // damit das Diagramm immer die volle Breite einnimmt und nicht gestaucht wird.
        const displayStart = meta.display_day_start ? parseInt(meta.display_day_start, 10) : null;
        const today0 = displayStart ? new Date(displayStart) : new Date();
        today0.setHours(0,0,0,0);
        const fixedGrid = [];
        for (let m = 0; m <= 24 * 60; m += 15) {
            fixedGrid.push(today0.getTime() + m * 60000);
        }

        // Wir verwenden *ausschließlich* das feste 15-Minuten-Raster für die X-Achse!
        // Das verhindert, dass hochfrequente Daten (z.B. jede Minute ein Live-Wert)
        // die kategoriale X-Achse verzerren und den restlichen Tag zusammenquetschen.
        const sortedTs = fixedGrid;
        const timeContext = buildCompactChartTimeContext(sortedTs, [], gridColor, tickColor, isDark, 7);

        // Hilfsfunktion: Interpoliere Wert bei gegebenem Timestamp
        const interp = (curve, ts) => {
            return _storageCurveInterp(curve, ts);
        };

        // Für IST-SoC nur Punkte von heute
        const interpIST = ts => {
            if (currentSocPoints.length === 0) return null;
            if (ts < currentSocPoints[0].ts || ts > currentSocPoints[currentSocPoints.length-1].ts) return null;
            return interp(currentSocPoints, ts);
        };

        // PV-Prognose aus simCurve (pv_w)
        const interpPV = ts => {
            if (simCurve.length === 0) return null;
            if (ts < simCurve[0].ts || ts > simCurve[simCurve.length-1].ts) return null;
            // Nächster Wert
            for (let i = 0; i < simCurve.length-1; i++) {
                if (ts >= simCurve[i].ts && ts <= simCurve[i+1].ts) {
                    const frac = (ts - simCurve[i].ts) / (simCurve[i+1].ts - simCurve[i].ts);
                    const watts = simCurve[i].pv_w + (simCurve[i+1].pv_w - simCurve[i].pv_w) * frac;
                    return watts >= 100 ? watts / 1000 : null; // kW, Dämmerungs-Restwerte ausblenden
                }
            }
            return null;
        };

        const interpSimSoc = ts => {
            if (simCurve.length === 0) return null;
            if (ts < simCurve[0].ts || ts > simCurve[simCurve.length-1].ts) return null;
            return interp(simCurve, ts);
        };

        const rawSollData = sortedTs.map(ts => interp(sollCurve, ts));
        const minData = sortedTs.map(ts => interp(minCurve, ts));
        const ceilingData = sortedTs.map(ts => interp(ceilingCurve, ts));
        const istData  = sortedTs.map(ts => interpIST(ts));
        const pvData   = sortedTs.map(ts => interpPV(ts));
        const sollData = rawSollData;
        const simSocData = sortedTs.map(ts => interpSimSoc(ts));
        const liveSoc = currentLiveSocForChart();
        // Die Historie bleibt eine eigene IST-Linie. Sie darf keinen alten
        // Punkt als scheinbar aktuellen Messwert oder aktiven Zielanker ausgeben.
        const chartLiveSoc = liveSoc;
        const activeTarget = _storageActiveCurveTarget(meta, chartLiveSoc, nowMs);
        _setStorageActiveTargetBadge(activeTarget);
        const activeFloorData = activeTarget.mode === 'floor_catchup'
            ? sortedTs.map(ts => ts >= nowMs - 8 * 60000 ? interp(minCurve, ts) : null)
            : sortedTs.map(() => null);
        const intermediateCurveAnchors = curveAnchors.filter(a => a && (a.kind === 'noon' || a.kind === 'intermediate'));
        const intermediateData = sortedTs.map(ts => {
            const anchor = intermediateCurveAnchors.find(a => Math.abs(ts - a.ts) <= 8 * 60000);
            return anchor ? parseFloat(anchor.soc) : null;
        });

        // Direktvermarktungs-Aktionsbänder für den heutigen Tag
        const dvView = directMarketingTrajectoryViewModel(window._storageLiveData || {});
        const dvSlots = Array.isArray(dvView.slots) ? dvView.slots : [];
        const interpDvKw = (actionType, ts) => {
            const slot = dvSlots.find(s => ts >= s.startTs && ts < s.endTs);
            if (!slot) return null;
            if (actionType === 'pv_store' && (slot.action === 'PV_STORE' || slot.action === 'DV_CURVE_CHARGE')) {
                return slot.plannedW && slot.plannedW > 0 ? slot.plannedW / 1000 : null;
            }
            if (actionType === 'export' && slot.action === 'ECONOMIC_EXPORT') {
                return slot.plannedW && slot.plannedW > 0 ? slot.plannedW / 1000 : null;
            }
            return null;
        };
        const interpHeadroomProjection = ts => {
            const slot = dvSlots.find(s => ts >= s.startTs && ts < s.endTs);
            if (!slot || slot.plannedRole !== 'projection' || slot.action !== 'HEADROOM_EXPORT') return null;
            return Number.isFinite(slot.projectedW) && slot.projectedW >= 0 ? slot.projectedW / 1000 : null;
        };
        const interpDvHold = ts => {
            const slot = dvSlots.find(s => ts >= s.startTs && ts < s.endTs);
            if (!slot) return null;
            if (slot.action === 'CHARGE_BLOCK_WAIT') return 1;
            return null;
        };
        const dvPvStoreData = sortedTs.map(ts => interpDvKw('pv_store', ts));
        const dvExportData = sortedTs.map(ts => interpDvKw('export', ts));
        const dvHeadroomProjectionData = sortedTs.map(ts => interpHeadroomProjection(ts));
        const dvHoldData = sortedTs.map(ts => interpDvHold(ts));
        const directMarketingSoc = directMarketingSocProjectionForTimestamps(dvView, sortedTs);
        const useDirectMarketingSoc = Array.isArray(directMarketingSoc);

        // Jetzt-Linie: Index des ersten Timestamps >= nowMs
        const nowIdx = (nowMs >= sortedTs[0] && nowMs <= sortedTs[sortedTs.length - 1])
            ? sortedTs.findIndex(ts => ts >= nowMs)
            : -1;

        const currentSocData = sortedTs.map((ts, index) => (
            index === nowIdx && chartLiveSoc !== null ? chartLiveSoc : null
        ));

        _storageCurveChartInstance = new Chart(canvas, {
            type: 'line',
            data: {
                labels: timeContext.labels,
                datasets: [
                    {
                        label: 'Soll-SoC',
                        data: sollData,
                        borderColor: '#4dabf7',
                        backgroundColor: 'rgba(77,171,247,0.1)',
                        borderWidth: 2.5,
                        borderDash: [6, 3],
                        pointRadius: 0,
                        tension: 0.3,
                        fill: false,
                        yAxisID: 'ySoc',
                    },
                    {
                        label: 'Aktives Regelziel',
                        data: activeFloorData,
                        borderColor: '#22c55e',
                        backgroundColor: 'rgba(34,197,94,0.16)',
                        borderWidth: 3.2,
                        borderDash: [],
                        pointRadius: 0,
                        tension: 0.18,
                        fill: false,
                        yAxisID: 'ySoc',
                    },
                    {
                        label: 'Unterkante',
                        data: minData,
                        borderColor: '#22c55e',
                        backgroundColor: 'rgba(34,197,94,0.08)',
                        borderWidth: 2,
                        borderDash: [2, 4],
                        pointRadius: 0,
                        tension: 0.25,
                        fill: false,
                        yAxisID: 'ySoc',
                    },
                    {
                        label: 'Oberkante',
                        data: ceilingData,
                        borderColor: '#f97316',
                        backgroundColor: 'rgba(249,115,22,0.08)',
                        borderWidth: 2,
                        borderDash: [8, 4],
                        pointRadius: 0,
                        tension: 0.25,
                        fill: false,
                        yAxisID: 'ySoc',
                    },
                    {
                        label: useDirectMarketingSoc ? 'DV-SoC-Prognose' : 'Standard-SoC-Prognose',
                        data: useDirectMarketingSoc ? directMarketingSoc : simSocData,
                        borderColor: useDirectMarketingSoc ? '#8b5cf6' : '#a78bfa',
                        backgroundColor: useDirectMarketingSoc
                            ? 'rgba(139,92,246,0.08)'
                            : 'rgba(167,139,250,0.08)',
                        borderWidth: useDirectMarketingSoc ? 2.5 : 2,
                        borderDash: useDirectMarketingSoc ? [] : [3, 3],
                        pointRadius: 0,
                        tension: useDirectMarketingSoc ? 0 : 0.25,
                        stepped: useDirectMarketingSoc ? 'after' : false,
                        fill: false,
                        hidden: false,
                        yAxisID: 'ySoc',
                    },
                    ...(chartLiveSoc !== null && nowIdx >= 0 ? [{
                        label: 'Aktueller SoC (Messwert)',
                        data: currentSocData,
                        showLine: false,
                        borderColor: '#22c55e',
                        backgroundColor: '#22c55e',
                        pointRadius: 6,
                        pointHoverRadius: 8,
                        pointStyle: 'circle',
                        yAxisID: 'ySoc',
                    }] : []),
                    {
                        label: 'IST-SoC',
                        data: istData,
                        borderColor: '#51cf66',
                        backgroundColor: 'rgba(81,207,102,0.1)',
                        borderWidth: 2.5,
                        pointRadius: 0,
                        tension: 0.3,
                        fill: false,
                        yAxisID: 'ySoc',
                    },
                    {
                        label: 'PV-Prognose (kW)',
                        data: pvData,
                        borderColor: 'rgba(255,176,0,0.65)',
                        backgroundColor: 'rgba(255,176,0,0.08)',
                        borderWidth: 1.5,
                        pointRadius: 0,
                        tension: 0.3,
                        fill: true,
                        yAxisID: 'yPV',
                    },
                    ...(dvView.active ? [
                        {
                            label: 'DV: PV speichern (Plan)',
                            data: dvPvStoreData,
                            type: 'bar',
                            borderColor: '#3b82f6',
                            backgroundColor: 'rgba(59,130,246,0.32)',
                            borderWidth: 1,
                            borderSkipped: false,
                            yAxisID: 'yPV',
                            order: 7,
                        },
                        {
                            label: 'DV: Wirtschaftl. Export (Plan)',
                            data: dvExportData,
                            type: 'bar',
                            borderColor: '#10b981',
                            backgroundColor: 'rgba(16,185,129,0.32)',
                            borderWidth: 1,
                            borderSkipped: false,
                            yAxisID: 'yPV',
                            order: 7,
                        },
                        {
                            label: 'DV: Headroom-Export (Prognose, keine Ausführung)',
                            data: dvHeadroomProjectionData,
                            type: 'bar',
                            borderColor: '#06b6d4',
                            backgroundColor: 'rgba(6,182,212,0.26)',
                            borderWidth: 1,
                            borderDash: [4, 3],
                            borderSkipped: false,
                            yAxisID: 'yPV',
                            order: 8,
                        },
                        {
                            label: 'DV: Laden gesperrt / Halten',
                            data: dvHoldData,
                            type: 'bar',
                            borderColor: '#f59e0b',
                            backgroundColor: 'rgba(245,158,11,0.16)',
                            borderWidth: 0,
                            borderSkipped: false,
                            barPercentage: 1,
                            categoryPercentage: 1,
                            yAxisID: 'yState',
                            order: 15,
                        }
                    ] : []),
                    {
                        label: 'Zwischenziel',
                        data: intermediateData,
                        showLine: false,
                        borderColor: '#f59e0b',
                        backgroundColor: '#f59e0b',
                        pointRadius: intermediateCurveAnchors.length ? 5 : 0,
                        pointHoverRadius: intermediateCurveAnchors.length ? 7 : 0,
                        pointStyle: 'triangle',
                        yAxisID: 'ySoc',
                    }
                ]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                animation: false,
                interaction: { mode: 'index', intersect: false },
                plugins: {
                    legend: {
                        display: true,
                        position: 'top',
                        labels: {
                            color: tickColor,
                            boxWidth: 12,
                            font: { size: 11 }
                        }
                    },
                    tooltip: {
                        callbacks: {
                            title: timeContext.tooltipTitle,
                            label: ctx => {
                                const label = ctx.dataset && ctx.dataset.label ? ctx.dataset.label : '';
                                if (label === 'Soll-SoC') return 'Soll: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'Aktives Regelziel') return 'Aktives Regelziel: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'Unterkante') return 'Unterkante: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'Oberkante') return 'Oberkante: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'Standard-SoC-Prognose') return label + ': ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'DV-SoC-Prognose') return 'DV-SoC: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'Aktueller SoC (Messwert)') return 'Aktueller SoC: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'IST-SoC') return 'IST:  ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                if (label === 'PV-Prognose (kW)') return 'PV:   ' + (ctx.raw !== null ? ctx.raw.toFixed(2) + ' kW' : '--');
                                if (label === 'DV: PV speichern (Plan)') return 'DV PV speichern: ' + (ctx.raw !== null ? Number(ctx.raw).toFixed(2) + ' kW' : '--');
                                if (label === 'DV: Wirtschaftl. Export (Plan)') return 'DV Export: ' + (ctx.raw !== null ? Number(ctx.raw).toFixed(2) + ' kW' : '--');
                                if (label === 'DV: Headroom-Export (Prognose, keine Ausführung)') return 'DV Headroom-Prognose: ' + (ctx.raw !== null ? Number(ctx.raw).toFixed(2) + ' kW · keine Ausführung' : '--');
                                if (label === 'DV: Laden gesperrt / Halten') return 'DV: Laden gesperrt / Halten';
                                if (label === 'Zwischenziel') return 'Zwischenziel: ' + (ctx.raw !== null ? ctx.raw.toFixed(1) + '%' : '--');
                                return '';
                            }
                        }
                    }
                },
                scales: {
                    x: timeContext.xScale,
                    ySoc: {
                        type: 'linear',
                        position: 'left',
                        min: 0,
                        max: 100,
                        ticks: { color: '#4dabf7', callback: v => v + '%', stepSize: 20 },
                        grid: { color: gridColor },
                        title: { display: true, text: 'SoC (%)', color: tickColor, font: { size: 11 } }
                    },
                    yPV: {
                        type: 'linear',
                        position: 'right',
                        min: 0,
                        ticks: { color: 'rgba(255,176,0,0.8)', callback: v => v + ' kW' },
                        grid: { display: false },
                        title: { display: true, text: 'PV (kW)', color: 'rgba(255,176,0,0.8)', font: { size: 11 } }
                    },
                    yState: {
                        type: 'linear',
                        display: false,
                        min: 0,
                        max: 1,
                        grid: { display: false }
                    }
                }
            },
            plugins: [{
                // Vertikale "Jetzt"-Linie
                id: 'nowLine',
                afterDraw: chart => {
                    if (nowIdx < 0) return;
                    const meta = chart.getDatasetMeta(0);
                    if (!meta.data[nowIdx]) return;
                    const x = meta.data[nowIdx].x;
                    const ctx = chart.ctx;
                    const yAxis = chart.scales.ySoc;
                    ctx.save();
                    ctx.beginPath();
                    ctx.strokeStyle = 'rgba(255,255,255,0.5)';
                    ctx.lineWidth = 1.5;
                    ctx.setLineDash([4, 4]);
                    ctx.moveTo(x, yAxis.top);
                    ctx.lineTo(x, yAxis.bottom);
                    ctx.stroke();
                    ctx.fillStyle = 'rgba(255,255,255,0.7)';
                    ctx.font = '11px sans-serif';
                    ctx.fillText('Jetzt', x + 4, yAxis.top + 14);
                    ctx.restore();
                }
            }]
        });
    }

    // Chart.js laden falls nicht vorhanden
    if (typeof Chart === 'undefined') {
        if (_storageCurveChartScriptLoading) return;
        _storageCurveChartScriptLoading = true;
        const s = document.createElement('script');
        s.src = 'assets/vendor/chart.js/chart.umd.min.js';
        s.onload = function() {
            _storageCurveChartScriptLoading = false;
            doRender();
        };
        s.onerror = function() {
            _storageCurveChartScriptLoading = false;
        };
        document.head.appendChild(s);
    } else {
        doRender();
    }
}

function _renderDirectMarketingTrajectoryChart() {
    const wrap = document.getElementById('sc-direct-marketing-chart-wrap');
    const canvas = document.getElementById('directMarketingTrajectoryChart');
    const stateEl = document.getElementById('sc-direct-marketing-chart-state');
    if (!wrap || !canvas) return;
    const data = window._storageLiveData || {};
    const view = directMarketingTrajectoryViewModel(data);
    if (view.active !== true) {
        wrap.style.display = 'none';
        if (stateEl) stateEl.textContent = '';
        if (_directMarketingTrajectoryChartInstance) {
            _directMarketingTrajectoryChartInstance.destroy();
            _directMarketingTrajectoryChartInstance = null;
        }
        return;
    }

wrap.style.display = '';
    if (_directMarketingTrajectoryChartInstance) {
        _directMarketingTrajectoryChartInstance.destroy();
        _directMarketingTrajectoryChartInstance = null;
    }
    // Gemeinsame Zeitachse: Wir erzeugen exakt dasselbe feste 15-Minuten Raster für den ganzen Tag (00:00 - 24:00),
    // damit Standard-Ladekurve und Direktvermarktungs-Fahrplan 1:1 vertikal gekoppelt und direkt vergleichbar sind.
    const meta = window._storagePlanMeta || {};
    const displayStart = meta.display_day_start ? parseInt(meta.display_day_start, 10) : null;
    const today0 = displayStart ? new Date(displayStart) : new Date();
    today0.setHours(0,0,0,0);
    const fixedGrid = [];
    for (let m = 0; m <= 24 * 60; m += 15) {
        fixedGrid.push(today0.getTime() + m * 60000);
    }
    const sortedTs = fixedGrid;
    const isDark = document.documentElement.getAttribute('data-bs-theme') === 'dark';
    const gridColor = isDark ? 'rgba(255,255,255,0.08)' : 'rgba(0,0,0,0.07)';
    const tickColor = isDark ? '#adb5bd' : '#6c757d';
    const timeContext = buildCompactChartTimeContext(sortedTs, [], gridColor, tickColor, isDark, 7);

    const rawSlots = Array.isArray(view.slots) ? view.slots : [];
    const interpDvSoc = ts => {
        const slot = rawSlots.find(s => ts >= s.startTs && ts < s.endTs);
        if (slot) return slot.socStartPct;
        if (rawSlots.length > 0) {
            if (ts < rawSlots[0].startTs) return rawSlots[0].socStartPct;
            if (ts >= rawSlots[rawSlots.length - 1].endTs) return rawSlots[rawSlots.length - 1].socEndPct;
        }
        return null;
    };
    const interpDvPvStore = ts => {
        const slot = rawSlots.find(s => ts >= s.startTs && ts < s.endTs);
        if (slot && slot.plannedAllowed && (slot.action === 'PV_STORE' || slot.action === 'DV_CURVE_CHARGE')) {
            return slot.plannedW && slot.plannedW > 0 ? slot.plannedW / 1000 : null;
        }
        return null;
    };
    const interpDvExport = ts => {
        const slot = rawSlots.find(s => ts >= s.startTs && ts < s.endTs);
        if (slot && slot.plannedAllowed && slot.action === 'ECONOMIC_EXPORT') {
            return slot.plannedW && slot.plannedW > 0 ? slot.plannedW / 1000 : null;
        }
        return null;
    };
    const interpHeadroomProjection = ts => {
        const slot = rawSlots.find(s => ts >= s.startTs && ts < s.endTs);
        if (!slot || slot.plannedRole !== 'projection' || slot.action !== 'HEADROOM_EXPORT') return null;
        return Number.isFinite(slot.projectedW) && slot.projectedW >= 0 ? slot.projectedW / 1000 : null;
    };
    const interpDvHold = ts => {
        const slot = rawSlots.find(s => ts >= s.startTs && ts < s.endTs);
        if (slot && slot.plannedAllowed && slot.action === 'CHARGE_BLOCK_WAIT') {
            return 1;
        }
        return null;
    };

    const socData = view.state === 'complete'
        ? sortedTs.map(ts => interpDvSoc(ts))
        : sortedTs.map(() => null);
    const pvStoreKw = sortedTs.map(ts => interpDvPvStore(ts));
    const exportKw = sortedTs.map(ts => interpDvExport(ts));
    const headroomProjectionKw = sortedTs.map(ts => interpHeadroomProjection(ts));
    const chargeBlock = sortedTs.map(ts => interpDvHold(ts));

    // Jetzt-Linie: Index des ersten Timestamps >= nowMs
    const nowMs = Date.now();
    const nowIdx = (nowMs >= sortedTs[0] && nowMs <= sortedTs[sortedTs.length - 1])
        ? sortedTs.findIndex(ts => ts >= nowMs)
        : -1;
    const chartLiveSoc = currentLiveSocForChart();
    const currentSocData = sortedTs.map((ts, index) => (
        index === nowIdx && chartLiveSoc !== null ? chartLiveSoc : null
    ));

    if (stateEl) {
        const currentSocText = chartLiveSoc !== null ? ` · aktueller SoC ${chartLiveSoc.toFixed(1)}%` : '';
        stateEl.textContent = view.state === 'actions_only'
            ? `SoC-Prognose EVIDENCE_LIMIT · nur ausgewählte Planaktionen${currentSocText}`
            : `Kanonischer DV-Plan · Ausführung und Hardwarewirkung separat${currentSocText}`;
    }

    _directMarketingTrajectoryChartInstance = new Chart(canvas, {
        type: 'line',
        data: {
            labels: timeContext.labels,
            datasets: [
                ...(view.state === 'complete' ? [{
                    label: 'DV-SoC-Prognose',
                    data: socData,
                    borderColor: '#8b5cf6',
                    backgroundColor: 'rgba(139,92,246,0.08)',
                    borderWidth: 2.5,
                    pointRadius: 0,
                    tension: 0,
                    stepped: 'after',
                    yAxisID: 'ySoc',
                    order: 1
                }] : []),
                ...(chartLiveSoc !== null && nowIdx >= 0 ? [{
                    label: 'Aktueller SoC (Messwert)',
                    data: currentSocData,
                    showLine: false,
                    borderColor: '#22c55e',
                    backgroundColor: '#22c55e',
                    pointRadius: 6,
                    pointHoverRadius: 8,
                    pointStyle: 'circle',
                    yAxisID: 'ySoc',
                    order: 0
                }] : []),
                {
                    label: 'PV speichern (Plan)',
                    data: pvStoreKw,
                    type: 'bar',
                    borderColor: '#3b82f6',
                    backgroundColor: 'rgba(59,130,246,0.38)',
                    borderWidth: 1,
                    borderSkipped: false,
                    yAxisID: 'yPower',
                    order: 3
                },
                {
                    label: 'Wirtschaftlicher Export (Plan)',
                    data: exportKw,
                    type: 'bar',
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16,185,129,0.38)',
                    borderWidth: 1,
                    borderSkipped: false,
                    yAxisID: 'yPower',
                    order: 3
                },
                {
                    label: 'Headroom-Export (Prognose, keine Ausführung)',
                    data: headroomProjectionKw,
                    type: 'bar',
                    borderColor: '#06b6d4',
                    backgroundColor: 'rgba(6,182,212,0.28)',
                    borderWidth: 1,
                    borderDash: [4, 3],
                    borderSkipped: false,
                    yAxisID: 'yPower',
                    order: 4
                },
                {
                    label: 'Laden gesperrt / Halten (Plan)',
                    data: chargeBlock,
                    type: 'bar',
                    borderColor: '#f59e0b',
                    backgroundColor: 'rgba(245,158,11,0.16)',
                    borderWidth: 0,
                    borderSkipped: false,
                    barPercentage: 1,
                    categoryPercentage: 1,
                    yAxisID: 'yState',
                    order: 10
                }
            ]
        },
        options: {
            responsive: true,
            maintainAspectRatio: false,
            animation: false,
            interaction: {mode: 'index', intersect: false},
            plugins: {
                legend: {display: true, position: 'top', labels: {color: tickColor, boxWidth: 12, font: {size: 11}}},
                tooltip: {
                    callbacks: {
                        title: timeContext.tooltipTitle,
                        label: ctx => {
                            const label = ctx.dataset && ctx.dataset.label ? ctx.dataset.label : '';
                            if (ctx.raw === null) return '';
                            if (label === 'DV-SoC-Prognose') return `DV-SoC: ${Number(ctx.raw).toFixed(1)}%`;
                            if (label === 'Aktueller SoC (Messwert)') return `Aktueller SoC: ${Number(ctx.raw).toFixed(1)}%`;
                            if (label === 'Laden gesperrt / Halten (Plan)') return label;
                            return `${label}: ${Number(ctx.raw).toFixed(2)} kW`;
                        }
                    }
                }
            },
            scales: {
                x: timeContext.xScale,
                ySoc: {
                    type: 'linear',
                    position: 'left',
                    min: 0,
                    max: 100,
                    ticks: {color: '#8b5cf6', callback: e => e + '%'},
                    grid: {color: gridColor},
                    title: {display: true, text: 'DV-SoC (%)', color: '#8b5cf6'}
                },
                yPower: {
                    type: 'linear',
                    position: 'right',
                    min: 0,
                    ticks: {color: tickColor, callback: e => e + ' kW'},
                    grid: {display: false},
                    title: {display: true, text: 'geplante Leistung (kW)', color: tickColor}
                },
                yState: {type: 'linear', display: false, min: 0, max: 1, grid: {display: false}}
            }
        },
        plugins: [{
            // Vertikale "Jetzt"-Linie (exakt deckungsgleich zur oberen Ladekurve)
            id: 'nowLine',
            afterDraw: chart => {
                if (nowIdx < 0) return;
                const meta = chart.getDatasetMeta(0);
                if (!meta.data[nowIdx]) return;
                const x = meta.data[nowIdx].x;
                const ctx = chart.ctx;
                const yAxis = chart.scales.ySoc;
                if (!yAxis) return;
                ctx.save();
                ctx.beginPath();
                ctx.strokeStyle = 'rgba(255,255,255,0.5)';
                ctx.lineWidth = 1.5;
                ctx.setLineDash([4, 4]);
                ctx.moveTo(x, yAxis.top);
                ctx.lineTo(x, yAxis.bottom);
                ctx.stroke();
                ctx.fillStyle = 'rgba(255,255,255,0.7)';
                ctx.font = '11px sans-serif';
                ctx.fillText('Jetzt', x + 4, yAxis.top + 14);
                ctx.restore();
            }
        }]
    });
}

// Signalisiert den Inline-Transportgates, dass alle Live-Consumer definiert sind.
window.e3dcSolarScriptReady = true;
window.dispatchEvent(new CustomEvent('e3dc:solar-ready'));
