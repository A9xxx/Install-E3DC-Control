<?php
require_once __DIR__.'/helpers.php';
requireWebAuth(true);
session_write_close();
// Nur lesender Tagesexport: feste Quelle und typisierte, personenfreie Felder.
header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');
header('Content-Disposition: attachment; filename="heat_tariff_shift_day.json"');
$path = __DIR__ . '/ramdisk/heat_tariff_shift_day.json';
if (!is_file($path) || is_link($path) || filesize($path) > 2097152) {
    http_response_code(404);
    echo json_encode(['error' => 'diagnostic_unavailable']);
    return;
}
$data = json_decode((string)file_get_contents($path), true);
if (!is_array($data) || !preg_match('/^\d{4}-\d{2}-\d{2}$/D', (string)($data['day'] ?? '')) || !is_array($data['records'] ?? null)) {
    http_response_code(503);
    echo json_encode(['error' => 'diagnostic_invalid']);
    return;
}
$numeric = static function($value) {
    return (is_int($value) || is_float($value)) && is_finite((float)$value) ? $value : null;
};
$blockers = ['mode_off', 'device_unsupported', 'auto_off', 'heat_policy_off', 'window_or_price_invalid', 'forecast_invalid', 'daily_state_unknown', 'duration_config_invalid', 'heat_need_unknown', 'pv_covers_need', 'no_heat_need', 'power_profile_missing', 'ww_temperature_missing', 'hz_temperature_missing', 'targets_reached', 'outdoor_temperature_limit', 'scope_invalid', 'ww_duration_missing', 'planned_start_pending', 'ww_duration_does_not_fit_window', 'minimum_does_not_fit_window', 'daily_maximum', 'remaining_energy_below_minimum', 'higher_priority_owner', 'storage_grant_pending', 'daily_state_not_durable', 'tariff_runtime_data_invalid', 'peak_shaving_owner', 'peak_shaving_priority', 'peak_shaving_context_missing'];
foreach (['data_fresh', 'reserve_free', 'emergency_free', 'connection_free', 'storage_free', 'device_free', 'source_free', 'holiday_free', 'restart_free', 'user_free'] as $gate) {
    $blockers[] = $gate . '_missing_or_blocked';
}
$records = [];
foreach (array_slice($data['records'], -1440) as $row) {
    if (!is_array($row)) continue;
    $out = [];
    foreach (['ts', 'target_c', 'start_ts', 'remaining_daily_s', 'used_s', 'used_kwh'] as $key) $out[$key] = $numeric($row[$key] ?? null);
    foreach (['would_start', 'commands_allowed'] as $key) $out[$key] = is_bool($row[$key] ?? null) ? $row[$key] : null;
    $out['mode'] = in_array($row['mode'] ?? null, ['off', 'shadow', 'active'], true) ? $row['mode'] : null;
    $out['target'] = in_array($row['target'] ?? null, ['ww', 'hz'], true) ? $row['target'] : null;
    $out['blockers'] = array_values(array_intersect($blockers, is_array($row['blockers'] ?? null) ? array_filter($row['blockers'], 'is_string') : []));
    $forecast = is_array($row['forecast'] ?? null) ? $row['forecast'] : [];
    $out['forecast'] = ['valid' => is_bool($forecast['valid'] ?? null) ? $forecast['valid'] : null];
    foreach (['need_kwh', 'pv_cover_kwh', 'missing_kwh', 'horizon_end_ts'] as $key) $out['forecast'][$key] = $numeric($forecast[$key] ?? null);
    $out['forecast']['pv_method'] = in_array($forecast['pv_method'] ?? null, ['P10', '70% P50', '70% P50 + P10', 'p10', 'p50x0.7', 'pointx0.7', 'p10 + p50x0.7', 'p10 + pointx0.7', 'p50x0.7 + pointx0.7', 'p10 + p50x0.7 + pointx0.7'], true) ? $forecast['pv_method'] : null;
    $out['forecast']['load_method'] = in_array($forecast['load_method'] ?? null, ['p50', 'point', 'p50 + point'], true) ? $forecast['load_method'] : null;
    $records[] = $out;
}
echo json_encode(['day' => $data['day'], 'records' => $records], JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
