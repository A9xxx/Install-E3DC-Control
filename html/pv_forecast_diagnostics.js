(function exposePvForecastDiagnostics(global) {
    'use strict';

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

    global.updatePvForecastDiagnostics = updatePvForecastDiagnostics;
})(window);
