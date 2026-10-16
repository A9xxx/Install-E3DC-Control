<?php
// Streng begrenzter öffentlicher Diagnoseauszug ohne Originalkennungen.
function forecastEntityArchiveProjection($raw) {
    if (!is_array($raw) || ($raw['schema_version'] ?? '') !== 'pv_entity_archive_v1'
        || ($raw['decision_use_allowed'] ?? null) !== false) return null;
    $number = static function ($v) {
        return !is_bool($v) && is_numeric($v) && is_finite((float)$v) && (float)$v >= 0 ? (float)$v : null;
    };
    $token = static function ($v, $prefix) {
        return is_string($v) && preg_match('/^' . $prefix . '[0-9]+$/D', $v) ? $v : null;
    };
    $reasons = ['ok', 'entity_source_missing', 'binding_changed', 'source_stale', 'target_missing',
        'global_horizontal_missing', 'sun_below_limit', 'measurement_gaps', 'invalid_sample',
        'no_distinct_samples', 'reference_unverified', 'reference_missing', 'diagnostic_budget', 'collecting', 'location_missing_or_invalid', 'diagnostic_error'];
    $reason = static fn($v) => in_array($v, $reasons, true) ? $v : 'reference_missing';
    $out = ['schema_version' => 'pv_entity_archive_v1', 'decision_use_allowed' => false,
        'status' => $reason($raw['status'] ?? ''), 'issued_at_utc_s' => $number($raw['issued_at_utc_s'] ?? null),
        'excerpt_limit' => 32, 'entities' => [], 'observations' => []];
    if (is_array($raw['budget'] ?? null)) {
        foreach (['reserved_bytes', 'used_bytes', 'retention_days', 'dropped_values', 'dropped_observations'] as $k)
            $out['budget'][$k] = $number($raw['budget'][$k] ?? null);
    }
    foreach (['observation_window_start', 'observation_window_end'] as $k) $out[$k] = $number($raw[$k] ?? null);
    foreach (array_slice(is_array($raw['entities'] ?? null) ? $raw['entities'] : [], 0, 32) as $r) {
        if (!is_array($r) || !in_array($r['model'] ?? '', ['m1', 'm2', 'm3'], true)
            || !in_array($r['kind'] ?? '', ['surface', 'provider_resource'], true)) continue;
        $item = ['model' => $r['model'], 'kind' => $r['kind'], 'fresh' => ($r['fresh'] ?? null) === true,
            'reason' => $reason($r['reason'] ?? ''), 'clarity_reason' => $reason($r['clarity_reason'] ?? ''), 'groups' => []];
        foreach (['entity', 'surface', 'resource'] as $k) $item[$k] = $token($r[$k] ?? null, $k . '_');
        foreach (['target', 'source_at', 'raw_wh', 'contribution_wh', 'w0', 'w1', 'clarity', 'issued_at', 'evaluated_at', 'ttl_s', 'ghi'] as $k) $item[$k] = $number($r[$k] ?? null);
        foreach (array_slice(is_array($r['groups'] ?? null) ? $r['groups'] : [], 0, 16) as $g) {
            if (is_array($g)) $item['groups'][] = ['group' => $token($g['group'] ?? null, 'group_'), 'share' => $number($g['share'] ?? null), 'measurement_boundary' => in_array($g['measurement_boundary'] ?? '', ['E3DC_DC', 'EXTERNAL_AC'], true) ? $g['measurement_boundary'] : null];
        }
        $item['daylight'] = is_bool($r['daylight'] ?? null) ? $r['daylight'] : null;
        $item['historical'] = ($r['historical'] ?? null) === true;
        foreach ([
            'source_time_basis' => ['response_capture', 'unknown'],
            'freshness_reason' => ['fresh', 'source_missing', 'time_missing', 'time_future', 'ttl_exceeded', 'unknown'],
            'ghi_reason' => ['ok', 'source_missing', 'not_provided', 'support_missing', 'unknown'],
            'selection_reason' => ['daylight_value', 'no_valid_daylight_value', 'provenance_unknown']
        ] as $k => $allowed) $item[$k] = in_array($r[$k] ?? null, $allowed, true) ? $r[$k] : 'unknown';
        $out['entities'][] = $item;
    }
    foreach (array_slice(is_array($raw['observations'] ?? null) ? $raw['observations'] : [], 0, 32) as $r) {
        if (!is_array($r) || !in_array($r['tracker'] ?? '', ['mppt_1', 'mppt_2', 'mppt_3', 'reference_dc', 'reference_ac'], true)) continue;
        $item = ['tracker' => $r['tracker'], 'valid' => ($r['valid'] ?? null) === true,
            'restart' => ($r['restart'] ?? null) === true, 'reason' => $reason($r['reason'] ?? ''),
            'tracker_quality' => 'unknown', 'surface_binding' => null, 'surface_binding_reason' => 'reference_missing',
            'clarity_valid' => false, 'clarity_reason' => $reason($r['clarity_reason'] ?? ''),
            'clarity_source_group' => $token($r['clarity_source_group'] ?? null, 'group_'),
            'classified_groups' => [], 'clarity_source' => 'other_group_clear_model', 'reference_independently_proven' => false];
        foreach (['target', 'observed_at', 'energy_wh', 'partial_energy_wh', 'covered_seconds', 'uncovered_seconds',
            'samples', 'gaps', 'invalid_samples', 'clock_regressions', 'max_gap_s', 'source_age_s', 'last_source_at', 'age_at_close_s',
            'voltage_mean', 'voltage_min', 'voltage_max', 'current_mean', 'current_min', 'current_max',
            'observed_clarity', 'clear_model_power_w'] as $k) $item[$k] = $number($r[$k] ?? null);
        if (!$item['valid']) $item['energy_wh'] = null;
        foreach (array_slice(is_array($r['classified_groups'] ?? null) ? $r['classified_groups'] : [], 0, 16) as $g) $item['classified_groups'][] = $token($g, 'group_');
        $out['observations'][] = $item;
    }
    return $out;
}
