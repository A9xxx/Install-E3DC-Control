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
                ? (provisional ? 'Evidenzsammlung' : 'Diagnostisch')
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
        setMetric('pv-forecast-diagnostic-rmse', finiteMetric('quadratische_fehlerwurzel_wh'), ' Wh/15 min');
        setMetric('pv-forecast-diagnostic-skill', finiteMetric('persistenz_skill_score_pct'), ' %', true);

        const comparedSlots = Math.max(0, Number(diagnostic.compared_slots) || 0);
        const relevantDays = Math.max(0, Number(diagnostic.yield_relevant_days) || 0);
        const relevantSlots = Math.max(0, Number(diagnostic.yield_relevant_slots) || 0);
        const sampleElement = document.getElementById('pv-forecast-diagnostic-sample');
        if (sampleElement) {
            sampleElement.textContent = comparedSlots > 0
                ? `${comparedSlots.toLocaleString('de-DE')} verglichene 15-Minuten-Fenster einschließlich Nacht · davon ${relevantSlots.toLocaleString('de-DE')} Ertragsfenster · ${relevantDays.toLocaleString('de-DE')} UTC-Tage mit Ertrag`
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
        const progress = diagnostic.quality_progress;
        const progressAge = Date.now() / 1000 - Number(progress?.calculated_at_utc_s);
        const progressFresh = progress?.schema_version === 'pv_forecast_quality_progress_v1'
            && progress.available === true && progress.reason === 'ok' && progress.decision_use_allowed === false
            && /^sha256:[0-9a-f]{64}$/.test(progress.topology_revision || '')
            && /^sha256:[0-9a-f]{64}$/.test(progress.method_revision || '')
            && /^sha256:[0-9a-f]{64}$/.test(progress.observation_binding_revision || '')
            && progress.topology_revision === diagnostic.topology_revision
            && progress.method_revision === diagnostic.forecast_issue_contract?.method_revision
            && Number.isInteger(progress.calculated_at_utc_s) && Number.isInteger(progress.evaluation_end_utc_s)
            && progress.evaluation_end_utc_s === progress.calculated_at_utc_s - 3600
            && progressAge >= 0 && progressAge <= 1800 && Array.isArray(progress.source_quality)
            && progress.source_quality.length === 2
            && new Set(progress.source_quality.map(item => item?.signal)).size === 2
            && progress.source_quality.every(item => item && ['pv_e3dc_dc', 'pv_external_ac'].includes(item.signal)
                && ['observed_slots', 'quality_slots', 'meter_confirmed_slots', 'eligible_slots', 'excluded_slots']
                    .every(key => Number.isInteger(item[key]) && item[key] >= 0)
                && item.eligible_slots <= item.meter_confirmed_slots && item.meter_confirmed_slots <= item.quality_slots
                && item.quality_slots <= item.observed_slots && item.eligible_slots + item.excluded_slots === item.observed_slots);
        const archivedQualitySources = Array.isArray(diagnostic.diagnostic_details?.source_quality)
            ? diagnostic.diagnostic_details.source_quality.filter(item => item && typeof item === 'object') : [];
        const qualitySources = progressFresh ? progress.source_quality : archivedQualitySources;
        const archivedExternalQuality = archivedQualitySources.find(item => item.signal === 'pv_external_ac');
        const externalQuality = qualitySources.find(item => item.signal === 'pv_external_ac');
        if (contractElement) {
            const isPointForecast = valueContract.distribution_type === 'deterministic_point';
            const p50Proven = valueContract.p50_claim === 'proven';
            const externalAcMissing = externalAcEvidence
                && externalAcEvidence.status === 'EVIDENCE_LIMIT';
            const parts = [
                isPointForecast ? 'Punktprognose' : 'Prognosevertrag nicht belegt',
                p50Proven ? 'P50 bestätigt' : 'kein belegtes P50'
            ];
            if (externalQuality && Number(externalQuality.meter_confirmed_slots) > 0) {
                parts.push((progressFresh ? '' : 'Archivstand: ') + (externalQuality.calibration?.status === 'validated'
                    ? 'Zusatz-WR: Korrekturfaktor auf späteren Tagen geprüft'
                    : 'Zusatz-WR: Erzeugungszähler bestätigt, Kalibrierung sammelt Daten'));
            } else if (externalAcMissing) {
                parts.push('Zusatz-WR ohne getrennte Ist-Kalibrierung');
            }
            if (qualitySources.some(item => Number(item.quality_slots) > 0)) {
                parts.push(progressFresh ? 'Begrenzungsfilter für neue Kalibrierfenster aktiv' : 'Archivstand: Begrenzungsfilter belegt');
            } else if (
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
                horizonElement.textContent = 'Prognosevorlauf: noch keine revisionsgebundenen Stichproben.';
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
                        `${String(bucket.label || bucket.bucket_id || 'Vorlauf')}: ${count.toLocaleString('de-DE')} Fenster (${Number(bucket.yield_relevant_slots || 0).toLocaleString('de-DE')} ertragsrelevant)`
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
                horizonElement.textContent = `Prognosevorlauf ab Ausgabe: ${horizonParts.join(' · ')}`;
            }
        }
        const container = card || box;
        let explanation = document.getElementById('pv-forecast-diagnostic-details');
        if (!explanation && container && typeof document.createElement === 'function') {
            const fold = document.createElement('details');
            fold.className = 'mt-2';
            const heading = document.createElement('summary');
            heading.textContent = 'Datengrundlage, Tagesvergleich und Prognosestufen';
            fold.appendChild(heading);
            explanation = document.createElement('div');
            explanation.id = 'pv-forecast-diagnostic-details';
            explanation.className = 'mt-2 text-muted';
            fold.appendChild(explanation);
            container.appendChild(fold);
        }
        if (explanation) {
            const fmt = (value, digits = 1) => value === null || value === undefined || !Number.isFinite(Number(value))
                ? '–' : Number(value).toLocaleString('de-DE', { maximumFractionDigits: digits });
            const lines = [
                'Ertragsfenster: Ist oder Prognose mindestens 25 Wh. Positiver Richtungsversatz bedeutet mehr Ist-Ertrag als vorhergesagt; negativ bedeutet weniger.',
                `WAPE = Summe absoluter Fehler / verglichene Ist-Energie. Abdeckung vorhandener Prognosen: ${fmt(diagnostic.compared_slots, 0)} / ${fmt(diagnostic.eligible_forecast_slots, 0)}; fehlende Prognoseausgaben sind darin nicht enthalten.`,
                `RMSE: ${fmt(finiteMetric('quadratische_fehlerwurzel_wh'))} Wh/15 min. Skill gegen Tagespersistenz: ${fmt(finiteMetric('persistenz_skill_score_pct'))} % bei ${fmt(diagnostic.persistence_compared_slots, 0)} Ertragsfenstern; positiv bedeutet besser.`,
                'Gesamtvergleich: je Fenster die letzte vorher archivierte Ausgabe; die Vorläufe sind gemischt. UTC-Ertragstage können unvollständig sein.'
            ];
            const details = diagnostic.diagnostic_details;
            if (details && details.schema_version === 'pv_forecast_diagnostic_details_v1' && details.decision_use_allowed === false) {
                const date = value => Number(value) > 0 ? new Date(Number(value) * 1000).toLocaleString('de-DE', { timeZone: 'UTC' }) + ' UTC' : '–';
                lines.push(`Zusätzliche Detailaufzeichnung: ${date(details.period_start_utc_s)} bis ${date(details.period_end_utc_s)}.`);
                lines.push(progressFresh
                    ? `Aktueller Kalibrier-Sammelstand: ${date(progress.calculated_at_utc_s)}; abgeschlossene Messfenster bis ${date(progress.evaluation_end_utc_s)}. Aktualisierung etwa alle 15 Minuten, mit einer Stunde Abschlussabstand. Die Kennzahlen des archivierten Berichts bleiben unverändert.`
                    : 'Aktueller Kalibrier-Sammelstand nicht verfügbar. Die folgenden Quellen- und Kalibrierwerte zeigen den archivierten Bericht.');
                lines.push(`Erwartetes Tageslicht nach Sonnenfenster-Schätzung: ${fmt(details.compared_daylight_slots, 0)} / ${fmt(details.expected_daylight_slots, 0)} Fenster (${fmt(details.daylight_coverage_pct)} %). ${fmt(details.daylight_unknown_slots, 0)} Fenster mit unbekannter Tageslichtzuordnung; ${fmt(details.unarchived_slots, 0)} Lücken zwischen erster und letzter Ausgabe.`);
                const reasons = { topology_unbound: 'Zuordnung fehlt', forecast_not_fresh: 'Prognose veraltet', forecast_value_missing: 'Prognosewert fehlt', observation_missing_or_invalid: 'Ist fehlt/ungültig', below_25_wh: 'unter 25 Wh' };
                const counts = details.exclusion_counts || {};
                lines.push('Ausschlüsse in der Detailaufzeichnung: ' + Object.entries(reasons).map(([key, label]) => `${label}: ${fmt(counts[key], 0)}`).join(' · ') + '.');
                const daily = details.frozen_daily || {};
                lines.push(`Eingefrorene Tagesprognose: ${fmt(daily.compared_days, 0)} vollständig verglichene UTC-Tage aus ${fmt(daily.forecast_days, 0)} Ausgaben mit 96 Fenstern; MAE ${fmt(daily.mae_wh)} Wh/Tag, Richtungsversatz ${fmt(daily.bias_wh)} Wh/Tag. Je Tag genau eine vollständige, vor Tagesbeginn erzeugte und archivierte Ausgabe.`);
                const external = details.external_observation || {};
                lines.push(`Zusatz-WR-Messung im archivierten Bericht: ${fmt(external.complete_slots, 0)} vollständige und ${fmt(external.incomplete_slots, 0)} lückenhafte Viertelstunden. ` + (Number(archivedExternalQuality?.meter_confirmed_slots) > 0
                    ? 'Die Erzeugungszuordnung beruht auf der Betreiberbestätigung in der Config und gilt ab Erfassung.'
                    : 'Zeitgewichtete vorhandene Live-Messwerte; keine unabhängig belegte Bruttomessung. Clipping und Abschaltung bleiben unbekannt.'));
                for (const source of qualitySources) {
                    const fit = source.calibration || {};
                    const states = source.state_seconds || {};
                    const label = source.signal === 'pv_e3dc_dc' ? 'E3DC-DC' : 'Zusatz-WR';
                    lines.push(`${label}: ${fmt(source.eligible_slots, 0)} für Kalibrierung geeignete Viertelstunden; ${fmt(source.excluded_slots, 0)} ausgeschlossen oder unbekannt. AC-Grenze ${fmt(source.ac_limit_w, 0)} W. Abgeregelt ${fmt(Number(states.curtailed || 0) / 60)} min, Clipping-Verdacht ${fmt(Number(states.clipping_suspected || 0) / 60)} min, abgeschaltet ${fmt(Number(states.shutdown || 0) / 60)} min, Zustand unbekannt ${fmt(Number(states.unknown || 0) / 60)} min.`);
                    const fitting = fit.status === 'validated' ? `Korrekturfaktor ${fmt(fit.factor, 3)} auf späteren Tagen geprüft`
                        : fit.status === 'not_improved' ? 'Korrektur verbessert die späteren Prüftage nicht ausreichend'
                            : 'Kalibrierung sammelt unabhängige Tage';
                    lines.push(`${label}: ${fitting}; ${fmt(fit.compared_days, 0)} Tage, ${fmt(fit.training_slots, 0)} Lernfenster und ${fmt(fit.validation_slots, 0)} spätere Prüffenster. Prüf-MAE roh ${fmt(fit.validation_raw_mae_wh)} Wh, korrigiert ${fmt(fit.validation_corrected_mae_wh)} Wh.`);
                }
                if (qualitySources.length) lines.push('Die neue Kalibrierung ist diagnostisch: Faktoren werden noch nicht automatisch auf die Ladekurve angewandt. Abregelung, Clipping-Verdacht, Abschaltung und unbekannte Zustände werden nur im gesonderten Kalibriervergleich ausgeschlossen; Gesamtkennzahlen enthalten sie weiterhin. Der Abregelschutz bleibt aktiv. Ohne Vergleichsanlage ist vermiedene Abregelung eine Abschätzung; eine P50-Aussage erfordert eine eigene statistische Prüfung.');
                const names = { provider_m1_raw: 'Forecast.Solar roh', provider_m2_raw: 'Open-Meteo roh', provider_m3_raw: 'Solcast roh', ensemble_before_bias: 'Ensemble vor Bias', bias_corrected_before_caps: 'Nach Bias, vor Limits', displayed_postprocessed: 'Endprognose' };
                const filteredStages = (details.quality_filtered_stage_metrics || []).filter(item => Number(item.compared_slots) > 0);
                if (filteredStages.length) lines.push('Begrenzungsgefilterter Vergleich im archivierten Bericht: ' + filteredStages.map(item => `${item.signal === 'pv_e3dc_dc' ? 'E3DC-DC' : 'Zusatz-WR'} ${names[item.stage] || ''}: ${fmt(item.compared_slots, 0)} Fenster, MAE ${fmt(item.mae_wh)} Wh`).join(' · ') + '.');
                const stages = (details.stage_metrics || []).filter(item => Number(item.compared_slots) > 0);
                if (stages.length) {
                    lines.push('Prognosestufen, jeweils eigene Ertragsfenster: ' + stages.map(item => `${item.signal === 'pv_e3dc_dc' ? 'E3DC-DC' : 'Zusatz-WR'} ${names[item.stage] || ''}: ${fmt(item.compared_slots, 0)} Fenster, MAE ${fmt(item.mae_wh)} Wh, Bias ${fmt(item.bias_wh)} Wh`).join(' · ') + '. Unterschiede bei den Fallzahlen erlauben keinen direkten Modellvergleich.');
                } else {
                    lines.push('Getrennte Prognosestufen sammeln Vergleichsdaten; ältere Ausgaben werden nicht nachträglich rekonstruiert.');
                }
            } else {
                lines.push('Zeitraum, eingefrorene Tagesausgaben und getrennte Modellstufen sind in dieser älteren Zusammenfassung noch nicht enthalten.');
            }
            explanation.textContent = lines.join('\n');
            explanation.style.whiteSpace = 'pre-line';
        }
    }

    global.updatePvForecastDiagnostics = updatePvForecastDiagnostics;
})(window);
