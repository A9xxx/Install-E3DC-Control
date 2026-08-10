<?php
// klima.php - Statusseite für gemessene Klimaanlage und vorbereitete Toshiba-Anbindung

if (session_status() !== PHP_SESSION_ACTIVE) {
    session_start();
}
require_once __DIR__ . '/helpers.php';

if (basename($_SERVER['SCRIPT_NAME'] ?? '') === 'klima.php') {
    requireWebAuth(false);
}

function e3dcClimateEsc($value) {
    return htmlspecialchars((string)$value, ENT_QUOTES, 'UTF-8');
}

function e3dcClimateReadJson($path) {
    if (!is_readable($path)) {
        return [];
    }
    $data = json_decode((string)file_get_contents($path), true);
    return is_array($data) ? $data : [];
}

function e3dcClimateCfg($cfg, $key, $default = '') {
    $value = $cfg[$key] ?? $default;
    if ($value === null || $value === '') {
        return $default;
    }
    return $value;
}

function e3dcClimateFloat($value, $default = null) {
    if ($value === null || $value === '') {
        return $default;
    }
    if (is_string($value)) {
        $value = str_replace(',', '.', $value);
    }
    return is_numeric($value) ? (float)$value : $default;
}

function e3dcClimateFmtW($value) {
    $num = e3dcClimateFloat($value);
    if ($num === null) {
        return '-- W';
    }
    if (abs($num) >= 1000) {
        return number_format($num / 1000, 2, ',', '.') . ' kW';
    }
    return number_format($num, 0, ',', '.') . ' W';
}

function e3dcClimateFmtKwh($value) {
    $num = e3dcClimateFloat($value);
    if ($num === null) {
        return '-- kWh';
    }
    return number_format($num, 2, ',', '.') . ' kWh';
}

function e3dcClimateFmtTemp($value) {
    $num = e3dcClimateFloat($value);
    if ($num === null) {
        return '-- °C';
    }
    return number_format($num, 1, ',', '.') . ' °C';
}

function e3dcClimateBoolText($value) {
    return cfgBool($value, false) ? 'ja' : 'nein';
}

function e3dcClimateStatusBadge($label, $class = 'secondary') {
    return '<span class="badge text-bg-' . e3dcClimateEsc($class) . '">' . e3dcClimateEsc($label) . '</span>';
}

function e3dcClimateInfoRow($label, $value, $muted = false) {
    $valueClass = $muted ? 'text-muted' : 'fw-bold';
    return '<div class="d-flex justify-content-between gap-3 small py-1 border-bottom border-secondary border-opacity-10">'
        . '<span class="text-muted">' . e3dcClimateEsc($label) . '</span>'
        . '<span class="' . $valueClass . ' text-end">' . $value . '</span>'
        . '</div>';
}

function e3dcClimateReasonLabel($reason) {
    $map = [
        'disabled' => 'Regelung deaktiviert',
        'mode_off' => 'Modus Aus',
        'local_adapter_not_available' => 'Lokaler Adapter noch nicht verfügbar',
        'toshiba_cloud_disabled' => 'Toshiba Cloud nicht freigegeben',
        'toshiba_cloud_config_incomplete' => 'Cloud-Zugangsdaten unvollständig',
        'toshiba_cloud_readonly' => 'Toshiba Cloud wird read-only ausgelesen',
        'toshiba_cloud_read_failed' => 'Toshiba Cloud konnte nicht gelesen werden',
        'toshiba_cloud_rate_limited' => 'Toshiba Cloud wartet nach Ratenbegrenzung',
        'toshiba_adapter_not_implemented' => 'Toshiba-Adapter vorbereitet, noch ohne Kommandos',
    ];
    $reason = (string)$reason;
    return $map[$reason] ?? $reason;
}

function e3dcClimateFirst($data, $keys) {
    foreach ($keys as $key) {
        if (isset($data[$key]) && $data[$key] !== '' && $data[$key] !== null) {
            return $data[$key];
        }
    }
    return null;
}

$confResult = loadE3dcConfig();
$cfg = empty($confResult['error']) ? ($confResult['config'] ?? []) : [];
$climateLoad = e3dcClimateReadJson('/var/www/html/ramdisk/climate_load.json');
$climateControl = e3dcClimateReadJson('/var/www/html/ramdisk/climate_control.json');
$schedule = is_array($climateControl['schedule'] ?? null) ? $climateControl['schedule'] : [];

$enabled = cfgBool(e3dcClimateCfg($cfg, 'climate_enable', '0'), false);
$controlEnabled = cfgBool(e3dcClimateCfg($cfg, 'climate_control_enable', '0'), false);
$cloudEnabled = cfgBool(e3dcClimateCfg($cfg, 'climate_toshiba_cloud_enable', '0'), false);
$name = (string)($climateLoad['name'] ?? e3dcClimateCfg($cfg, 'climate_name', 'Klimaanlage'));
$isOnline = cfgBool($climateLoad['online'] ?? false, false);
$isActive = cfgBool($climateLoad['active'] ?? false, false);
$hasCredentials = cfgBool($climateControl['credentials_configured'] ?? false, false)
    || ((string)e3dcClimateCfg($cfg, 'climate_toshiba_username', '') !== '' && (string)e3dcClimateCfg($cfg, 'climate_toshiba_password', '') !== '');
$deviceIdsRaw = (string)e3dcClimateCfg($cfg, 'climate_toshiba_device_ids', '');
$deviceIds = [];
foreach (explode(',', $deviceIdsRaw) as $deviceId) {
    $deviceId = trim($deviceId);
    if ($deviceId !== '') {
        $deviceIds[] = $deviceId;
    }
}
if (!empty($climateControl['device_ids']) && is_array($climateControl['device_ids'])) {
    $deviceIds = $climateControl['device_ids'];
}

$serviceStatus = file_exists('/.dockerenv') ? 'docker' : e3dcSystemdServiceStatus('e3dc-climate-live');
$controlServiceStatus = file_exists('/.dockerenv') ? 'docker' : e3dcSystemdServiceStatus('e3dc-climate-control');
$ageS = e3dcClimateFloat($climateLoad['age_s'] ?? null);
if ($ageS === null && isset($climateLoad['ts'])) {
    $ageS = max(0, time() - (int)$climateLoad['ts']);
}
$loadAgeLabel = $ageS === null ? '--' : number_format($ageS, 0, ',', '.') . ' s';
$targetTemp = e3dcClimateFirst($climateControl, ['target_temp_c']);
if ($targetTemp === null) {
    $targetTemp = $schedule['target_temp_c'] ?? e3dcClimateCfg($cfg, 'climate_day_temp_c', '24.0');
}
$profile = (string)($schedule['profile'] ?? 'day');
$profileLabel = $profile === 'night' ? 'Nacht' : 'Tag';
$roomTemp = e3dcClimateFirst($climateControl, ['room_temp_c', 'indoor_temp_c', 'current_temp_c']);
$outsideTemp = e3dcClimateFirst($climateControl, ['outside_temp_c', 'outdoor_temp_c']);
$reason = (string)($climateControl['reason'] ?? ($controlEnabled ? 'toshiba_adapter_not_implemented' : 'disabled'));
$cloudConnected = cfgBool($climateControl['cloud_connected'] ?? false, false);
$cloudDeviceCount = (int)($climateControl['cloud_device_count'] ?? ($climateControl['device_count'] ?? 0));
$configuredDeviceCount = (int)($climateControl['configured_device_count'] ?? 0);
$unmatchedDeviceIds = [];
if (isset($climateControl['unmatched_device_ids']) && is_array($climateControl['unmatched_device_ids'])) {
    $unmatchedDeviceIds = array_values(array_filter(array_map('strval', $climateControl['unmatched_device_ids'])));
}
$cloudLastRead = (string)($climateControl['cloud_last_read_iso'] ?? '');
$cloudDuration = e3dcClimateFloat($climateControl['cloud_duration_s'] ?? null);
$cloudDurationLabel = $cloudDuration === null ? '--' : number_format($cloudDuration, 2, ',', '.') . ' s';
$cloudError = (string)($climateControl['cloud_error'] ?? '');
$cloudRateLimited = cfgBool($climateControl['cloud_rate_limited'] ?? false, false);
$cloudHttpStatus = isset($climateControl['cloud_http_status']) ? (int)$climateControl['cloud_http_status'] : 0;
$cloudRetryAt = (string)($climateControl['cloud_retry_at_iso'] ?? '');
$cloudRetryInS = isset($climateControl['cloud_retry_in_s']) ? max(0, (int)$climateControl['cloud_retry_in_s']) : 0;
$cloudRetryLabel = $cloudRetryAt !== ''
    ? $cloudRetryAt . ($cloudRetryInS > 0 ? ' (' . (string)ceil($cloudRetryInS / 60) . ' min)' : '')
    : '--';
$cloudDevices = [];
if (isset($climateControl['devices']) && is_array($climateControl['devices'])) {
    foreach ($climateControl['devices'] as $device) {
        if (is_array($device)) {
            $cloudDevices[] = $device;
        }
    }
}
?>

<div class="card border-info shadow-sm">
    <div class="card-header bg-info bg-opacity-10 border-info d-flex flex-wrap justify-content-between align-items-center gap-2">
        <div>
            <h5 class="mb-0 text-info fw-bold"><i class="fas fa-snowflake me-2"></i><?= e3dcClimateEsc($name) ?></h5>
            <div class="small text-muted">Eigene Klima-Ansicht für Messung, Toshiba-Status und spätere Regelprofile.</div>
        </div>
        <div class="d-flex flex-wrap gap-2">
            <?= $enabled ? e3dcClimateStatusBadge('Messung aktiv', $isOnline ? 'success' : 'warning') : e3dcClimateStatusBadge('Messung aus', 'secondary') ?>
            <?= $controlEnabled ? e3dcClimateStatusBadge('Regelprofil aktiv', 'info') : e3dcClimateStatusBadge('Regelung aus', 'secondary') ?>
            <?= $cloudEnabled ? e3dcClimateStatusBadge('Cloud freigegeben', 'primary') : e3dcClimateStatusBadge('Cloud aus', 'secondary') ?>
        </div>
    </div>
    <div class="card-body">
        <?php if (!$enabled): ?>
            <div class="alert alert-secondary small mb-3">
                Die Klimaanlage ist noch nicht als eigener Verbraucher aktiviert. Die Seite bleibt vorbereitet; Messwerte erscheinen nach Aktivierung der Klima-Messung in der Konfiguration.
            </div>
        <?php endif; ?>

        <div class="row g-3">
            <div class="col-12 col-lg-6">
                <div class="card bg-body-tertiary border-info border-opacity-50 h-100">
                    <div class="card-body">
                        <h6 class="text-info fw-bold border-bottom border-info border-opacity-25 pb-2 mb-3">
                            <i class="fas fa-gauge-high me-2"></i>Live-Messung
                        </h6>
                        <div class="display-6 fw-bold mb-1"><?= e3dcClimateEsc(e3dcClimateFmtW($climateLoad['power_w'] ?? null)) ?></div>
                        <div class="small mb-3 <?= $isActive ? 'text-success' : 'text-muted' ?>">
                            <?= $isActive ? 'Klimaanlage läuft' : 'kein relevanter Klimaverbrauch' ?>
                        </div>
                        <?= e3dcClimateInfoRow('Heute', e3dcClimateEsc(e3dcClimateFmtKwh($climateLoad['daily_kwh'] ?? null))) ?>
                        <?= e3dcClimateInfoRow('Gesamtzähler', e3dcClimateEsc(e3dcClimateFmtKwh($climateLoad['energy_total_kwh'] ?? null))) ?>
                        <?= e3dcClimateInfoRow('Quelle', e3dcClimateEsc($climateLoad['source'] ?? e3dcClimateCfg($cfg, 'climate_meter_type', ''))) ?>
                        <?= e3dcClimateInfoRow('Zähler-IP', e3dcClimateEsc($climateLoad['ip'] ?? e3dcClimateCfg($cfg, 'climate_meter_ip', ''))) ?>
                        <?= e3dcClimateInfoRow('Phase/Kanal', e3dcClimateEsc(($climateLoad['phase'] ?? e3dcClimateCfg($cfg, 'climate_meter_phase', '')) . ' / ' . ($climateLoad['meter_channel'] ?? '--'))) ?>
                        <?= e3dcClimateInfoRow('Strom / Spannung', e3dcClimateEsc(($climateLoad['current_a'] ?? '--') . ' A / ' . ($climateLoad['voltage_v'] ?? '--') . ' V')) ?>
                        <?= e3dcClimateInfoRow('Leistungsfaktor', e3dcClimateEsc($climateLoad['pf'] ?? '--')) ?>
                        <?= e3dcClimateInfoRow('Messalter', e3dcClimateEsc($loadAgeLabel)) ?>
                        <?= e3dcClimateInfoRow('Dienst', e3dcClimateEsc($serviceStatus)) ?>
                    </div>
                </div>
            </div>

            <div class="col-12 col-lg-6">
                <div class="card bg-body-tertiary border-primary border-opacity-50 h-100">
                    <div class="card-body">
                        <h6 class="text-primary fw-bold border-bottom border-primary border-opacity-25 pb-2 mb-3">
                            <i class="fas fa-cloud me-2"></i>Toshiba Cloud
                        </h6>
                        <?= e3dcClimateInfoRow('Provider', e3dcClimateEsc($climateControl['provider'] ?? e3dcClimateCfg($cfg, 'climate_control_provider', 'toshiba_cloud'))) ?>
                        <?= e3dcClimateInfoRow('Modus', e3dcClimateEsc($climateControl['mode'] ?? e3dcClimateCfg($cfg, 'climate_control_mode', 'off'))) ?>
                        <?= e3dcClimateInfoRow('Zugangsdaten vorhanden', e3dcClimateEsc($hasCredentials ? 'ja' : 'nein')) ?>
                        <?= e3dcClimateInfoRow('Geräteauswahl', e3dcClimateEsc(empty($deviceIds) ? 'automatisch' : implode(', ', $deviceIds))) ?>
                        <?= e3dcClimateInfoRow('Cloud verbunden', e3dcClimateEsc($cloudConnected ? 'ja' : 'nein')) ?>
                        <?= e3dcClimateInfoRow('Geräte gefunden', e3dcClimateEsc((string)$cloudDeviceCount)) ?>
                        <?= e3dcClimateInfoRow('Config-Zuordnung', e3dcClimateEsc(empty($deviceIds) ? 'automatisch' : ($configuredDeviceCount . ' von ' . count($deviceIds) . ' erkannt'))) ?>
                        <?= e3dcClimateInfoRow('Letzter Lesezugriff', e3dcClimateEsc($cloudLastRead !== '' ? $cloudLastRead : '--')) ?>
                        <?= e3dcClimateInfoRow('Lesedauer', e3dcClimateEsc($cloudDurationLabel)) ?>
                        <?php if ($cloudRateLimited): ?>
                            <?= e3dcClimateInfoRow('HTTP-Status', e3dcClimateEsc($cloudHttpStatus > 0 ? (string)$cloudHttpStatus : '429')) ?>
                            <?= e3dcClimateInfoRow('Nächster Cloudversuch', e3dcClimateEsc($cloudRetryLabel)) ?>
                        <?php endif; ?>
                        <?= e3dcClimateInfoRow('Kommandos erlaubt', e3dcClimateEsc(e3dcClimateBoolText($climateControl['commands_allowed'] ?? false))) ?>
                        <?= e3dcClimateInfoRow('Status', e3dcClimateEsc(e3dcClimateReasonLabel($reason))) ?>
                        <?= e3dcClimateInfoRow('Dienst', e3dcClimateEsc($controlServiceStatus)) ?>
                        <?php if ($cloudError !== ''): ?>
                            <div class="alert alert-warning small mt-3 mb-0"><?= e3dcClimateEsc($cloudError) ?></div>
                        <?php elseif (!empty($unmatchedDeviceIds)): ?>
                            <div class="alert alert-warning small mt-3 mb-0">
                                Nicht zugeordnet: <?= e3dcClimateEsc(implode(', ', $unmatchedDeviceIds)) ?>. Erlaubt sind Cloud-Namen wie Oben/Unten oder technische ID-Endungen.
                            </div>
                        <?php else: ?>
                            <div class="alert alert-info small mt-3 mb-0">
                                Toshiba wird nur gelesen: Temperaturen, Sollwert und Modus kommen aus der Cloud; Befehle bleiben gesperrt.
                            </div>
                        <?php endif; ?>
                    </div>
                </div>
            </div>

            <?php if (!empty($cloudDevices)): ?>
                <div class="col-12">
                    <div class="card bg-body-tertiary border-info border-opacity-50">
                        <div class="card-body">
                            <h6 class="text-info fw-bold border-bottom border-info border-opacity-25 pb-2 mb-3">
                                <i class="fas fa-snowflake me-2"></i>Toshiba-Geräte
                            </h6>
                            <div class="table-responsive">
                                <table class="table table-sm table-hover align-middle mb-0">
                                    <thead>
                                        <tr>
                                            <th>Gerät</th>
                                            <th>Config</th>
                                            <th>Status</th>
                                            <th>Modus</th>
                                            <th>Soll</th>
                                            <th>Innen</th>
                                            <th>Außen</th>
                                            <th>Lüfter</th>
                                            <th class="text-end">Cloud-ID</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        <?php foreach ($cloudDevices as $device): ?>
                                            <?php
                                                $state = isset($device['state']) && is_array($device['state']) ? $device['state'] : [];
                                                $deviceName = (string)($device['name'] ?? '');
                                                if ($deviceName === '') {
                                                    $deviceName = 'Toshiba ' . (string)($device['ac_id_tail'] ?? '');
                                                }
                                                $idTail = (string)($device['ac_id_tail'] ?? '');
                                                $uniqueTail = (string)($device['unique_id_tail'] ?? '');
                                                $idLabel = trim($idTail . ($uniqueTail !== '' ? ' / ' . $uniqueTail : ''));
                                                $configSelector = (string)($device['config_selector'] ?? '');
                                                if ($configSelector === '') {
                                                    $configSelector = empty($deviceIds) ? 'automatisch' : 'nicht gewählt';
                                                }
                                            ?>
                                            <tr>
                                                <td class="fw-semibold"><?= e3dcClimateEsc($deviceName) ?></td>
                                                <td><?= e3dcClimateEsc($configSelector) ?></td>
                                                <td><?= e3dcClimateEsc($state['status'] ?? '--') ?></td>
                                                <td><?= e3dcClimateEsc($state['mode'] ?? '--') ?></td>
                                                <td><?= e3dcClimateEsc(e3dcClimateFmtTemp($state['target_temp_c'] ?? null)) ?></td>
                                                <td><?= e3dcClimateEsc(e3dcClimateFmtTemp($state['indoor_temp_c'] ?? null)) ?></td>
                                                <td><?= e3dcClimateEsc(e3dcClimateFmtTemp($state['outdoor_temp_c'] ?? null)) ?></td>
                                                <td><?= e3dcClimateEsc($state['fan'] ?? '--') ?></td>
                                                <td class="text-end text-muted small"><?= e3dcClimateEsc($idLabel !== '' ? $idLabel : '--') ?></td>
                                            </tr>
                                        <?php endforeach; ?>
                                    </tbody>
                                </table>
                            </div>
                        </div>
                    </div>
                </div>
            <?php endif; ?>

            <div class="col-12">
                <div class="card bg-body-tertiary border-secondary border-opacity-50">
                    <div class="card-body">
                        <h6 class="fw-bold border-bottom border-secondary border-opacity-25 pb-2 mb-3">
                            <i class="fas fa-sliders me-2 text-info"></i>Temperaturen und Einstellungen
                        </h6>
                        <div class="row g-3">
                            <div class="col-6 col-md-3">
                                <div class="small text-muted">Innenraum</div>
                                <div class="fs-5 fw-bold"><?= e3dcClimateEsc($roomTemp === null ? 'noch nicht ausgelesen' : e3dcClimateFmtTemp($roomTemp)) ?></div>
                            </div>
                            <div class="col-6 col-md-3">
                                <div class="small text-muted">Außen</div>
                                <div class="fs-5 fw-bold"><?= e3dcClimateEsc($outsideTemp === null ? 'noch nicht ausgelesen' : e3dcClimateFmtTemp($outsideTemp)) ?></div>
                            </div>
                            <div class="col-6 col-md-3">
                                <div class="small text-muted">Aktuelles Ziel</div>
                                <div class="fs-5 fw-bold"><?= e3dcClimateEsc(e3dcClimateFmtTemp($targetTemp)) ?></div>
                            </div>
                            <div class="col-6 col-md-3">
                                <div class="small text-muted">Profil</div>
                                <div class="fs-5 fw-bold"><?= e3dcClimateEsc($profileLabel) ?></div>
                            </div>
                        </div>
                        <div class="row g-2 mt-3">
                            <div class="col-12 col-md-4"><?= e3dcClimateInfoRow('Tag-Ziel', e3dcClimateEsc(e3dcClimateFmtTemp(e3dcClimateCfg($cfg, 'climate_day_temp_c', '24.0')))) ?></div>
                            <div class="col-12 col-md-4"><?= e3dcClimateInfoRow('Nacht-Ziel', e3dcClimateEsc(e3dcClimateFmtTemp(e3dcClimateCfg($cfg, 'climate_night_temp_c', '26.0')))) ?></div>
                            <div class="col-12 col-md-4"><?= e3dcClimateInfoRow('Nachtfenster', e3dcClimateEsc(e3dcClimateCfg($cfg, 'climate_night_start', '22:00') . ' - ' . e3dcClimateCfg($cfg, 'climate_night_end', '06:00'))) ?></div>
                            <div class="col-12 col-md-4"><?= e3dcClimateInfoRow('Nacht Eco', e3dcClimateEsc(e3dcClimateBoolText(e3dcClimateCfg($cfg, 'climate_night_eco_enable', '1')))) ?></div>
                            <div class="col-12 col-md-4"><?= e3dcClimateInfoRow('Nacht Leise', e3dcClimateEsc(e3dcClimateBoolText(e3dcClimateCfg($cfg, 'climate_night_quiet_enable', '1')))) ?></div>
                            <div class="col-12 col-md-4"><?= e3dcClimateInfoRow('High Power Tag', e3dcClimateEsc(e3dcClimateBoolText(e3dcClimateCfg($cfg, 'climate_high_power_enable', '0')))) ?></div>
                        </div>
                    </div>
                </div>
            </div>
        </div>

        <div class="d-flex flex-wrap gap-2 mt-3">
            <a class="btn btn-outline-info btn-sm" href="index.php?view=climate">
                <i class="fas fa-chart-line me-1"></i>Verbrauchsverlauf
            </a>
            <a class="btn btn-outline-secondary btn-sm" href="index.php?seite=config#conf_climate_toshiba_username">
                <i class="fas fa-gear me-1"></i>Toshiba Cloud Zugangsdaten
            </a>
        </div>
    </div>
</div>
