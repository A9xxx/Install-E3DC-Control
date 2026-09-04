<?php
require_once __DIR__ . '/helpers.php';
sendNoCacheHeaders();
requireWebAuth(true);
if (($_SERVER['REQUEST_METHOD'] ?? 'GET') === 'POST') {
    e3dcRequireCsrfToken(true);
}
header('Content-Type: application/json; charset=utf-8');

const RULE_CALM_MAX_UPLOAD_BYTES = 31457280;
const RULE_CALM_HISTORY_FILE = '/var/www/html/ramdisk/rule_calm_analysis.json';
const RULE_CALM_PUBLIC_SCHEMA = 'e3dc_rule_calm_public_v1';
const RULE_CALM_CLI_SCHEMA = 'e3dc_decision_history_analysis_cli_v1';
const RULE_CALM_PRIVACY_NOTE = 'Die Auswertung bleibt lokal auf dem System. Es werden nur Entscheidungsverläufe gelesen; Steuerbefehle werden nicht ausgeführt.';

function ruleCalmJsonFlags($pretty = false) {
    $flags = JSON_UNESCAPED_UNICODE;
    if ($pretty) {
        $flags |= JSON_PRETTY_PRINT;
    }
    if (defined('JSON_INVALID_UTF8_SUBSTITUTE')) {
        $flags |= JSON_INVALID_UTF8_SUBSTITUTE;
    }
    return $flags;
}

function ruleCalmJson($payload) {
    echo json_encode($payload, ruleCalmJsonFlags());
    exit;
}

function ruleCalmError($message, $extra = []) {
    ruleCalmJson(array_merge([
        'success' => false,
        'error' => $message,
        'privacy_note' => RULE_CALM_PRIVACY_NOTE,
    ], is_array($extra) ? $extra : []));
}

function ruleCalmResolveInstallPath() {
    $paths = function_exists('getInstallPaths') ? getInstallPaths() : [];
    $candidates = [];
    if (is_array($paths) && !empty($paths['valid']) && !empty($paths['install_path'])) {
        $candidates[] = $paths['install_path'];
    }
    $candidates[] = '/app/pi/Install';
    foreach ($candidates as $candidate) {
        $candidate = rtrim((string)$candidate, '/');
        if ($candidate !== '' && is_file($candidate . '/Tools/decision_history_analysis.py')) {
            return $candidate;
        }
    }
    return '';
}

function ruleCalmSafeInt($value, $default, $min, $max) {
    if (!is_numeric($value)) {
        return $default;
    }
    $num = (int)round((float)$value);
    return max($min, min($max, $num));
}

function ruleCalmNormalizeServices($raw) {
    $allowed = ['wallbox', 'storage', 'heatpump', 'ems'];
    if (!is_array($raw)) {
        $raw = is_string($raw) ? explode(',', $raw) : [];
    }
    $services = [];
    foreach ($raw as $service) {
        $service = strtolower(trim((string)$service));
        if ($service === 'wp' || $service === 'waerme' || $service === 'wärme') {
            $service = 'heatpump';
        } elseif ($service === 'speicher') {
            $service = 'storage';
        }
        if (in_array($service, $allowed, true) && !in_array($service, $services, true)) {
            $services[] = $service;
        }
    }
    return $services ?: $allowed;
}

function ruleCalmRequestedServices() {
    return ruleCalmNormalizeServices($_GET['service'] ?? $_POST['service'] ?? []);
}

function ruleCalmValidatedServiceList($value) {
    if (!is_array($value)) {
        return null;
    }
    $allowed = ['wallbox', 'storage', 'heatpump', 'ems'];
    $result = [];
    foreach ($value as $service) {
        if (!is_string($service) || !in_array($service, $allowed, true)) {
            return null;
        }
        if (!in_array($service, $result, true)) {
            $result[] = $service;
        }
    }
    return $result;
}

function ruleCalmRequestedLimit() {
    return ruleCalmSafeInt($_GET['limit'] ?? $_POST['limit'] ?? 1200, 1200, 50, 5000);
}

function ruleCalmRequestedScope() {
    $scope = strtolower(trim((string)($_GET['scope'] ?? $_POST['scope'] ?? 'manager_restart')));
    return in_array($scope, ['latest', 'manager_restart'], true) ? $scope : 'manager_restart';
}

function ruleCalmScopeLabel($scope, $cutoff_time = '') {
    if ($scope === 'manager_restart') {
        return $cutoff_time !== ''
            ? 'seit Manager-Neustart ' . $cutoff_time
            : 'seit Manager-Neustart';
    }
    return 'Historie: neueste Records (prozessübergreifend)';
}

function ruleCalmServiceLabel($service) {
    $labels = [
        'storage' => 'Speicher',
        'wallbox' => 'Wallbox',
        'heatpump' => 'Wärmepumpe',
        'ems' => 'EMS',
        'ems_decision' => 'EMS-Entscheidungen',
        'wallbox_start_stop' => 'Wallbox Start/Stop',
        'wallbox_phase' => 'Wallbox Phasen',
        'storage_owner' => 'Speicher-Owner',
        'storage_contract_owner' => 'Speicher-Contract',
        'storage_execution_class' => 'Speicher-Ausführung',
        'storage_state' => 'Speicher-State',
        'storage_state_reason' => 'Speicher-Grund',
        'storage_value_update' => 'Speicher-Wertupdate',
        'storage_live_plausibility' => 'Speicher-Messwerte',
    ];
    return $labels[$service] ?? $service;
}

function ruleCalmServiceUnit($service) {
    $units = [
        'storage' => 'e3dc-storage-manager.service',
        'wallbox' => 'e3dc-wallbox-manager.service',
        'heatpump' => 'energy_manager.service',
        'ems' => 'e3dc-storage-manager.service',
    ];
    return $units[$service] ?? '';
}

function ruleCalmParseSystemdTimestamp($value) {
    $text = trim((string)$value);
    if ($text === '' || $text === 'n/a') {
        return null;
    }
    $parsed = strtotime($text);
    if ($parsed !== false) {
        return (float)$parsed;
    }
    if (preg_match('/(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})/', $text, $m)) {
        $parsed = strtotime($m[1] . ' ' . $m[2]);
        return $parsed === false ? null : (float)$parsed;
    }
    return null;
}

function ruleCalmSystemdServiceStartTs($unit) {
    $unit = trim((string)$unit);
    if ($unit === '' || !preg_match('/^[A-Za-z0-9_.@-]+$/', $unit)) {
        return null;
    }
    $systemctl = '/usr/bin/systemctl';
    if (!is_executable($systemctl)) {
        return null;
    }
    $process = e3dcRunArgvProcess([
        $systemctl,
        'show',
        $unit,
        '--property=ExecMainStartTimestamp',
        '--property=ActiveEnterTimestamp',
        '--value',
    ], 5.0, ['max_output_bytes' => 16384]);
    if (!$process['success']) {
        return null;
    }
    $out = preg_split('/\R/', (string)$process['stdout']) ?: [];
    foreach ($out as $line) {
        $ts = ruleCalmParseSystemdTimestamp($line);
        if ($ts !== null) {
            return $ts;
        }
    }
    return null;
}

function ruleCalmCurrentScopeContext($scope, $services) {
    $services = ruleCalmNormalizeServices($services);
    $context = [
        'scope' => $scope,
        'scope_label' => ruleCalmScopeLabel($scope),
        'cutoff_ts' => null,
        'cutoff_time' => '',
        'service_start_times' => [],
    ];
    if ($scope !== 'manager_restart') {
        return $context;
    }
    $starts = [];
    foreach ($services as $service) {
        $unit = ruleCalmServiceUnit($service);
        $ts = ruleCalmSystemdServiceStartTs($unit);
        $context['service_start_times'][$service] = [
            'unit' => $unit,
            'ts' => $ts,
            'time' => ruleCalmTimeLabel($ts),
        ];
        if ($ts !== null) {
            $starts[] = (float)$ts;
        }
    }
    if ($starts) {
        $context['cutoff_ts'] = max($starts);
        $context['cutoff_time'] = ruleCalmTimeLabel($context['cutoff_ts']);
        $context['scope_label'] = ruleCalmScopeLabel($scope, $context['cutoff_time']);
    } else {
        $context['scope_label'] = 'seit Manager-Neustart (Startzeit nicht ermittelbar)';
    }
    return $context;
}

function ruleCalmFindInputPath() {
    $candidates = [
        '/var/www/html/logs',
        '/var/www/html/ramdisk',
        '/var/www/html/data',
    ];
    foreach ($candidates as $candidate) {
        if (!is_dir($candidate)) {
            continue;
        }
        $matches = glob($candidate . '/*decision_history*') ?: [];
        $latest = glob($candidate . '/*decision_latest.json') ?: [];
        if (!empty($matches) || !empty($latest)) {
            return $candidate;
        }
    }
    return '/var/www/html/logs';
}

function ruleCalmInputHasDecisionData($path) {
    if (!is_dir($path)) {
        return is_file($path);
    }
    $matches = glob(rtrim($path, '/') . '/*decision_history*') ?: [];
    $latest = glob(rtrim($path, '/') . '/*decision_latest.json') ?: [];
    return !empty($matches) || !empty($latest);
}

function ruleCalmDecisionPatterns($service) {
    $patterns = [
        'storage' => ['storage_decision_latest.json', 'storage_decision_history*.jsonl*'],
        'wallbox' => ['wallbox_decision_latest.json', 'wallbox_decision_history*.jsonl*'],
        'heatpump' => ['energy_decision_latest.json', 'energy_decision_history*.jsonl*'],
        'ems' => ['ems_decision_latest.json', 'ems_decision_surface.json'],
    ];
    return $patterns[$service] ?? [];
}

function ruleCalmFindLatestDecisionFiles($services) {
    $roots = ['/var/www/html/ramdisk', '/var/www/html/data', '/var/www/html/logs'];
    $files = [];
    foreach ($services as $service) {
        $history_matches = [];
        $latest_matches = [];
        foreach ($roots as $root) {
            if (!is_dir($root)) {
                continue;
            }
            foreach (ruleCalmDecisionPatterns($service) as $pattern) {
                foreach (glob(rtrim($root, '/') . '/' . $pattern) ?: [] as $candidate) {
                    if (is_file($candidate) && is_readable($candidate)) {
                        if (strpos(basename($candidate), 'decision_history') !== false) {
                            $history_matches[] = $candidate;
                        } else {
                            $latest_matches[] = $candidate;
                        }
                    }
                }
            }
        }
        $matches = $history_matches ?: $latest_matches;
        usort($matches, function($a, $b) {
            $ma = @filemtime($a) ?: 0;
            $mb = @filemtime($b) ?: 0;
            if ($ma === $mb) {
                return strcmp($b, $a);
            }
            return $mb <=> $ma;
        });
        if ($matches) {
            $files[$service] = $matches[0];
        }
    }
    return $files;
}

function ruleCalmRemoveTree($path) {
    if (!is_dir($path)) {
        return;
    }
    foreach (scandir($path) ?: [] as $entry) {
        if ($entry === '.' || $entry === '..') {
            continue;
        }
        $child = rtrim($path, '/') . '/' . $entry;
        if (is_dir($child) && !is_link($child)) {
            ruleCalmRemoveTree($child);
        } else {
            @unlink($child);
        }
    }
    @rmdir($path);
}

function ruleCalmReadCurrentTail($source, $limit, $since_ts = null) {
    $limit = max(1, (int)$limit);
    $since_ts = $since_ts !== null ? (float)$since_ts : null;
    $lines = [];
    $total = 0;
    $filtered_total = 0;
    $is_gz = strtolower(substr((string)$source, -3)) === '.gz';
    if ($is_gz && function_exists('gzopen')) {
        $handle = @gzopen($source, 'rb');
        if ($handle) {
            while (!gzeof($handle)) {
                $line = trim((string)gzgets($handle));
                if ($line === '' || $line[0] !== '{') {
                    continue;
                }
                $total++;
                $line_ts = ruleCalmLineTs($line);
                if ($since_ts !== null && ($line_ts === null || $line_ts < $since_ts)) {
                    continue;
                }
                $filtered_total++;
                $lines[] = $line;
                if (count($lines) > $limit) {
                    array_shift($lines);
                }
            }
            gzclose($handle);
        }
    } else {
        $handle = @fopen($source, 'rb');
        if ($handle) {
            while (($line = fgets($handle)) !== false) {
                $line = trim((string)$line);
                if ($line === '' || $line[0] !== '{') {
                    continue;
                }
                $total++;
                $line_ts = ruleCalmLineTs($line);
                if ($since_ts !== null && ($line_ts === null || $line_ts < $since_ts)) {
                    continue;
                }
                $filtered_total++;
                $lines[] = $line;
                if (count($lines) > $limit) {
                    array_shift($lines);
                }
            }
            fclose($handle);
        }
    }
    return [$lines, $total, $filtered_total];
}

function ruleCalmLineTs($line) {
    $record = json_decode((string)$line, true);
    if (!is_array($record)) {
        return null;
    }
    return ruleCalmTs($record['ts'] ?? $record['time'] ?? null);
}

function ruleCalmStageEmptyDecisionFile($source, $target_dir, $since_ts = null, $total = 0, $filtered_total = 0) {
    $base = basename((string)$source);
    $target_base = preg_replace('/\.gz$/i', '', $base);
    if ($target_base === '') {
        $target_base = 'decision_history.jsonl';
    }
    $target = rtrim($target_dir, '/') . '/' . $target_base;
    if (@file_put_contents($target, '') === false) {
        return [null, []];
    }
    if (!@chmod($target, 0600)) {
        @unlink($target);
        return [null, []];
    }
    return [$target, [
        'tail' => true,
        'records' => 0,
        'total_records' => $total,
        'filtered_records' => $filtered_total,
        'since_ts' => $since_ts,
        'since_time' => ruleCalmTimeLabel($since_ts),
        'first_ts' => null,
        'last_ts' => null,
        'first_time' => '',
        'last_time' => '',
    ]];
}

function ruleCalmStageCurrentDecisionFile($source, $target_dir, $limit, $since_ts = null) {
    $base = basename((string)$source);
    $is_history = strpos($base, 'decision_history') !== false;
    if (!$is_history) {
        if ($since_ts !== null) {
            $line = @file_get_contents($source);
            $line_ts = $line !== false ? ruleCalmLineTs($line) : null;
            if ($line_ts === null || $line_ts < (float)$since_ts) {
                return ruleCalmStageEmptyDecisionFile($source, $target_dir, $since_ts, $line !== false ? 1 : 0, 0);
            }
        }
        $target = rtrim($target_dir, '/') . '/' . $base;
        if (!@copy($source, $target) || !@chmod($target, 0600)) {
            @unlink($target);
            return [null, []];
        }
        return [$target, [
            'tail' => false,
            'records' => 1,
            'total_records' => 1,
            'filtered_records' => 1,
            'since_ts' => $since_ts,
            'since_time' => ruleCalmTimeLabel($since_ts),
        ]];
    }

    [$lines, $total, $filtered_total] = ruleCalmReadCurrentTail($source, $limit, $since_ts);
    if (!$lines) {
        if ($since_ts !== null) {
            return ruleCalmStageEmptyDecisionFile($source, $target_dir, $since_ts, $total, $filtered_total);
        }
        return [null, []];
    }
    $target_base = preg_replace('/\.gz$/i', '', $base);
    if (!preg_match('/\.jsonl$/i', $target_base)) {
        $target_base .= '.jsonl';
    }
    $target = rtrim($target_dir, '/') . '/' . $target_base;
    if (@file_put_contents($target, implode("\n", $lines) . "\n") === false) {
        return [null, []];
    }
    if (!@chmod($target, 0600)) {
        @unlink($target);
        return [null, []];
    }
    $first_ts = ruleCalmLineTs($lines[0]);
    $last_ts = ruleCalmLineTs($lines[count($lines) - 1]);
    return [$target, [
        'tail' => true,
        'records' => count($lines),
        'total_records' => $total,
        'filtered_records' => $filtered_total,
        'since_ts' => $since_ts,
        'since_time' => ruleCalmTimeLabel($since_ts),
        'first_ts' => $first_ts,
        'last_ts' => $last_ts,
        'first_time' => ruleCalmTimeLabel($first_ts),
        'last_time' => ruleCalmTimeLabel($last_ts),
    ]];
}

function ruleCalmRecordSummary($record_windows) {
    $times_first = [];
    $times_last = [];
    $since_times = [];
    $records = 0;
    $filtered_records = 0;
    $total_records = 0;
    foreach ($record_windows as $window) {
        if (!is_array($window)) {
            continue;
        }
        $records += (int)($window['records'] ?? 0);
        $filtered_records += (int)($window['filtered_records'] ?? $window['records'] ?? 0);
        $total_records += (int)($window['total_records'] ?? $window['records'] ?? 0);
        if (($window['since_ts'] ?? null) !== null) {
            $since_times[] = (float)$window['since_ts'];
        }
        if (($window['first_ts'] ?? null) !== null) {
            $times_first[] = (float)$window['first_ts'];
        }
        if (($window['last_ts'] ?? null) !== null) {
            $times_last[] = (float)$window['last_ts'];
        }
    }
    $first = $times_first ? min($times_first) : null;
    $last = $times_last ? max($times_last) : null;
    return [
        'records' => $records,
        'filtered_records' => $filtered_records,
        'total_records' => $total_records,
        'since_ts' => $since_times ? max($since_times) : null,
        'since_time' => $since_times ? ruleCalmTimeLabel(max($since_times)) : '',
        'first_ts' => $first,
        'last_ts' => $last,
        'first_time' => ruleCalmTimeLabel($first),
        'last_time' => ruleCalmTimeLabel($last),
    ];
}

function ruleCalmPrepareCurrentInput($services, $limit, $scope_context = null) {
    $scope_context = is_array($scope_context) ? $scope_context : ruleCalmCurrentScopeContext('latest', $services);
    $since_ts = $scope_context['cutoff_ts'] ?? null;
    $files = ruleCalmFindLatestDecisionFiles($services);
    if (!$files) {
        return [ruleCalmFindInputPath(), 'Aktuelle Entscheidungsverläufe', 'current_history', [], [], [], $scope_context];
    }
    $tmp_root = '/var/www/html/tmp/rule_calm_current';
    if (!is_dir($tmp_root) && !@mkdir($tmp_root, 0700, true)) {
        return [ruleCalmFindInputPath(), 'Aktuelle Entscheidungsverläufe', 'current_history', array_values($files), [], [], $scope_context];
    }
    @chmod($tmp_root, 0700);
    try {
        $token = bin2hex(random_bytes(4));
    } catch (Throwable $e) {
        $token = uniqid('', false);
    }
    $target_dir = $tmp_root . '/' . date('Ymd-His') . '-' . $token;
    if (!@mkdir($target_dir, 0700, true)) {
        return [ruleCalmFindInputPath(), 'Aktuelle Entscheidungsverläufe', 'current_history', array_values($files), [], [], $scope_context];
    }
    @chmod($target_dir, 0700);
    $record_windows = [];
    foreach ($files as $service => $source) {
        [$target, $window] = ruleCalmStageCurrentDecisionFile($source, $target_dir, $limit, $since_ts);
        if (!$target) {
            ruleCalmRemoveTree($target_dir);
            return [ruleCalmFindInputPath(), 'Aktuelle Entscheidungsverläufe', 'current_history', array_values($files), [], [], $scope_context];
        }
        $record_windows[$service] = $window;
    }
    register_shutdown_function(function() use ($target_dir) {
        ruleCalmRemoveTree($target_dir);
    });
    return [
        $target_dir,
        'Aktuelle Entscheidungsverläufe',
        'current_history',
        array_values($files),
        $record_windows,
        ruleCalmRecordSummary($record_windows),
        $scope_context,
    ];
}

function ruleCalmManifest() {
    $services = ['storage', 'wallbox', 'heatpump', 'ems'];
    $files = ruleCalmFindLatestDecisionFiles($services);
    $input_path = ruleCalmFindInputPath();
    $history = is_file(RULE_CALM_HISTORY_FILE);
    ruleCalmJson([
        'success' => true,
        'schema' => RULE_CALM_PUBLIC_SCHEMA,
        'generated_at' => date('c'),
        'install_path_found' => ruleCalmResolveInstallPath() !== '',
        'privacy_note' => RULE_CALM_PRIVACY_NOTE,
        'sources' => [
            [
                'key' => 'current',
                'label' => 'Aktuelle Verlaufsdaten',
                'available' => !empty($files) || ruleCalmInputHasDecisionData($input_path),
                'file_count' => count($files),
            ],
            [
                'key' => 'upload',
                'label' => 'Diagnose-ZIP hochladen',
                'available' => true,
                'max_bytes' => RULE_CALM_MAX_UPLOAD_BYTES,
            ],
            [
                'key' => 'history',
                'label' => 'Letzte Auswertung',
                'available' => $history,
            ],
        ],
    ]);
}

function ruleCalmHistory() {
    if (!is_file(RULE_CALM_HISTORY_FILE)) {
        ruleCalmError('Noch keine gespeicherte Regelruhe-Auswertung vorhanden');
    }
    $raw = @file_get_contents(RULE_CALM_HISTORY_FILE);
    $payload = $raw ? json_decode($raw, true) : null;
    if (!is_array($payload)) {
        ruleCalmError('Gespeicherte Regelruhe-Auswertung ist nicht lesbar');
    }
    $schema = (string)($payload['schema'] ?? '');
    if ($schema === RULE_CALM_PUBLIC_SCHEMA) {
        $completeness = strtoupper((string)($payload['completeness'] ?? ''));
        $services = ruleCalmValidatedServiceList($payload['services'] ?? null);
        $analyzed = ruleCalmValidatedServiceList($payload['analyzed_services'] ?? null);
        $missing = ruleCalmValidatedServiceList($payload['missing_services'] ?? null);
        $partition = array_values(array_unique(array_merge($analyzed ?? [], $missing ?? [])));
        sort($partition);
        $expected = $services ?? [];
        sort($expected);
        if (!in_array($completeness, ['COMPLETE', 'PARTIAL'], true)
            || $services === null || $analyzed === null || $missing === null || !$analyzed
            || $partition !== $expected
            || ($completeness === 'COMPLETE' && $missing)
            || ($completeness === 'PARTIAL' && !$missing)) {
            ruleCalmError('Gespeicherte Regelruhe-Auswertung enthält keinen gültigen Vollständigkeitsvertrag');
        }
    } else {
        ruleCalmError('Gespeicherte Regelruhe-Auswertung verwendet ein unbekanntes Datenformat');
    }
    $payload['success'] = true;
    $payload['from_history'] = true;
    $payload['privacy_note'] = $payload['privacy_note'] ?? RULE_CALM_PRIVACY_NOTE;
    ruleCalmJson($payload);
}

function ruleCalmUploadPath() {
    if ($_SERVER['REQUEST_METHOD'] !== 'POST') {
        ruleCalmError('Diagnose-ZIP Upload benötigt POST');
    }
    if (empty($_FILES['diagnose_zip']) || !is_array($_FILES['diagnose_zip'])) {
        ruleCalmError('Keine Diagnose-ZIP übergeben');
    }
    $file = $_FILES['diagnose_zip'];
    if (($file['error'] ?? UPLOAD_ERR_NO_FILE) !== UPLOAD_ERR_OK) {
        ruleCalmError('Diagnose-ZIP konnte nicht hochgeladen werden', ['upload_error' => (int)($file['error'] ?? -1)]);
    }
    $size = (int)($file['size'] ?? 0);
    if ($size <= 0 || $size > RULE_CALM_MAX_UPLOAD_BYTES) {
        ruleCalmError('Diagnose-ZIP ist leer oder größer als 30 MB', ['size_bytes' => $size]);
    }
    $name = basename((string)($file['name'] ?? 'diagnose.zip'));
    if (strtolower(pathinfo($name, PATHINFO_EXTENSION)) !== 'zip') {
        ruleCalmError('Bitte eine Diagnose-Datei im ZIP-Format auswählen');
    }
    $safe_name = preg_replace('/[^A-Za-z0-9._-]+/', '_', $name);
    $tmp_root = '/var/www/html/tmp/rule_calm_uploads';
    if (!is_dir($tmp_root) && !@mkdir($tmp_root, 0700, true)) {
        ruleCalmError('Temporäres Upload-Verzeichnis ist nicht verfügbar');
    }
    @chmod($tmp_root, 0700);
    try {
        $token = bin2hex(random_bytes(4));
    } catch (Throwable $e) {
        $token = uniqid('', false);
    }
    $target = $tmp_root . '/' . date('Ymd-His') . '-' . $token . '-' . $safe_name;
    if (!is_uploaded_file($file['tmp_name']) || !@move_uploaded_file($file['tmp_name'], $target)) {
        ruleCalmError('Diagnose-ZIP konnte nicht gespeichert werden');
    }
    @chmod($target, 0600);
    register_shutdown_function(function() use ($target) {
        if (is_file($target)) {
            @unlink($target);
        }
    });
    return [$target, 'Lokal hochgeladenes Diagnosepaket', 'diagnose_zip_upload'];
}

function ruleCalmTs($value) {
    if ($value === null || $value === '') {
        return null;
    }
    if (is_numeric($value)) {
        $ts = (float)$value;
        if ($ts > 10000000000) {
            $ts = $ts / 1000.0;
        }
        return $ts > 0 ? $ts : null;
    }
    $parsed = strtotime((string)$value);
    return $parsed === false ? null : (float)$parsed;
}

function ruleCalmTimeLabel($ts) {
    if ($ts === null) {
        return '';
    }
    return date('Y-m-d H:i:s', (int)round((float)$ts));
}

function ruleCalmPublicCheckName($value) {
    $name = strtolower(trim((string)$value));
    $allowed = [
        'wallbox', 'wallbox_start_stop', 'wallbox_phase', 'storage', 'storage_owner',
        'storage_contract_owner', 'storage_execution_class', 'storage_state',
        'storage_state_reason', 'storage_value_update', 'storage_live_plausibility',
        'storage_decision_path', 'storage_budget_executor_shadow', 'heatpump',
        'ems', 'ems_decision',
    ];
    return in_array($name, $allowed, true) ? $name : 'other';
}

function ruleCalmPublicActor($value) {
    $text = trim((string)$value);
    if ($text === 'storage_decision_path' || preg_match('/^actor-[0-9a-f]{10}$/', $text)) {
        return $text;
    }
    return $text === '' ? '' : 'actor-' . substr(hash('sha256', $text), 0, 10);
}

function ruleCalmPublicAction($value) {
    $text = strtoupper(trim((string)$value));
    if ($text === '') {
        return '';
    }
    if (preg_match('/^(START|STOP|ON|OFF|PAUSE|RESUME|IDLE|HOLD|RELEASE|VALUE_UPDATE|UNKNOWN|UNKNOWN_PATH|CURVE|DIRECT_MARKETING|MARKET_DIRECT|MARKET_PRICE|PREDUMP|PROTECTION|WALLBOX_SUPPORT|MANUAL|STORAGE_ACTIVE|E3DC_AUTO|E3DC_AUTONOM|E3DC|WALLBOX|STORAGE|MARKET|AUTO|AUTO_FREE|AUTO_LIMITED|AUTO_RELEASE|CHRG|DISCH|GRID|AUTO_GUARD|INVALID_SAMPLE|DISCHARGE_OWNER_HOLD|CHARGE_OWNER_HOLD|WB_MINSOC_HOLD|AUTO_LIMIT_HOLD|MANUAL_OVERRIDE_HOLD|EVIDENCE_LIMIT|PARALLEL_WB_AUTO|[123]P)$/', $text)
        || preg_match('/^STATE-[0-9A-F]{10}$/', $text)) {
        return $text;
    }
    return 'STATE-' . substr(hash('sha256', $text), 0, 10);
}

function ruleCalmLaneGroup($check) {
    $check = ruleCalmPublicCheckName($check);
    if ($check === 'storage_live_plausibility') {
        return 'data_quality';
    }
    if (in_array($check, [
        'storage_owner', 'storage_contract_owner', 'storage_state',
        'storage_state_reason', 'storage_decision_path',
    ], true)) {
        return 'decision_path';
    }
    return 'execution';
}

function ruleCalmPublicPattern($value) {
    $text = strtolower(trim((string)$value));
    if ($text === '') {
        return '';
    }
    return preg_match('/^[a-z0-9_:-]{1,80}$/', $text)
        ? $text
        : 'pattern-' . substr(hash('sha256', $text), 0, 10);
}

function ruleCalmForumActorLabel($value) {
    return $value === 'storage_decision_path' ? 'Speicher-Entscheidungspfad' : $value;
}

function ruleCalmForumActionLabel($value) {
    $labels = [
        'PROTECTION' => 'Schutzpfad',
        'CURVE' => 'Ladekurve',
        'DIRECT_MARKETING' => 'Direktvermarktung',
        'MARKET_DIRECT' => 'Direktvermarktung',
        'MARKET_PRICE' => 'Marktpreis',
        'PREDUMP' => 'Vorentladung',
        'WALLBOX_SUPPORT' => 'Wallbox-Unterstützung',
        'MANUAL' => 'Manuell',
        'STORAGE_ACTIVE' => 'Speicher aktiv',
        'E3DC_AUTO' => 'E3DC Auto',
        'E3DC_AUTONOM' => 'E3DC autonom',
    ];
    return $labels[$value] ?? $value;
}

function ruleCalmForumPatternLabel($value) {
    $labels = [
        'protection_curve_protection' => 'Schutzpfad → Ladekurve → Schutzpfad',
        'curve_protection_curve' => 'Ladekurve → Schutzpfad → Ladekurve',
    ];
    return $labels[$value] ?? $value;
}

function ruleCalmPublicCounts($counts) {
    $public = [];
    foreach (is_array($counts) ? $counts : [] as $name => $value) {
        if (!is_numeric($value)) {
            continue;
        }
        $key = ruleCalmPublicAction($name);
        if ($key === '') {
            continue;
        }
        $public[$key] = ($public[$key] ?? 0) + (int)$value;
    }
    return $public;
}

function ruleCalmPublicNumericMap($values) {
    $public = [];
    foreach (is_array($values) ? $values : [] as $name => $value) {
        if (!is_numeric($value)) {
            continue;
        }
        $key = ruleCalmPublicCheckName($name);
        if ($key !== 'other') {
            $public[$key] = (float)$value;
        }
    }
    return $public;
}

function ruleCalmPublicEvidenceLimits($values) {
    $public = [];
    $allowed = ['storage_live_typed_missing', 'storage_output_typed_missing'];
    foreach (is_array($values) ? $values : [] as $name => $value) {
        if (in_array($name, $allowed, true) && is_numeric($value)) {
            $public[$name] = max(0, (int)$value);
        }
    }
    return $public;
}

function ruleCalmCompactChecks($summary) {
    $public = [];
    foreach (is_array($summary['checks'] ?? null) ? $summary['checks'] : [] as $name => $check) {
        if (!is_array($check)) {
            continue;
        }
        $key = ruleCalmPublicCheckName($name);
        $public[$key] = [
            'ok' => ($check['ok'] ?? false) === true,
            'counts' => ruleCalmPublicCounts($check['counts'] ?? []),
        ];
    }
    return $public;
}

function ruleCalmPublicRecordWindows($record_windows) {
    $public = [];
    foreach (is_array($record_windows) ? $record_windows : [] as $service => $window) {
        if (!is_array($window)) {
            continue;
        }
        $key = ruleCalmPublicCheckName($service);
        $public[$key] = [
            'tail' => !empty($window['tail']),
            'records' => (int)($window['records'] ?? 0),
            'total_records' => (int)($window['total_records'] ?? 0),
            'filtered_records' => (int)($window['filtered_records'] ?? 0),
            'since_time' => (string)($window['since_time'] ?? ''),
            'first_time' => (string)($window['first_time'] ?? ''),
            'last_time' => (string)($window['last_time'] ?? ''),
        ];
    }
    return $public;
}

function ruleCalmPublicScopeContext($scope_context) {
    $scope_context = is_array($scope_context) ? $scope_context : [];
    $scope = ($scope_context['scope'] ?? '') === 'manager_restart' ? 'manager_restart' : 'latest';
    $starts = [];
    foreach (is_array($scope_context['service_start_times'] ?? null) ? $scope_context['service_start_times'] : [] as $service => $info) {
        $key = ruleCalmPublicCheckName($service);
        if ($key === 'other' || !is_array($info)) {
            continue;
        }
        $starts[$key] = ['time' => (string)($info['time'] ?? '')];
    }
    return [
        'scope' => $scope,
        'scope_label' => (string)($scope_context['scope_label'] ?? ''),
        'cutoff_time' => (string)($scope_context['cutoff_time'] ?? ''),
        'service_start_times' => $starts,
    ];
}

function ruleCalmNormalizeEvent($event, $lane, $alert = false, $pattern = '', $age_s = null) {
    if (!is_array($event)) {
        return null;
    }
    $ts = ruleCalmTs($event['ts'] ?? $event['time'] ?? null);
    return [
        'lane' => ruleCalmPublicCheckName($lane),
        'actor' => ruleCalmPublicActor($event['actor'] ?? ''),
        'action' => ruleCalmPublicAction($event['action'] ?? ''),
        'target_reachable' => array_key_exists('target_reachable', $event) ? (bool)$event['target_reachable'] : true,
        'ts' => $ts,
        'time' => ruleCalmTimeLabel($ts),
        'alert' => (bool)$alert,
        'pattern' => ruleCalmPublicPattern($pattern),
        'age_s' => $age_s !== null ? (float)$age_s : null,
    ];
}

function ruleCalmTimelineAdd(&$items, $item) {
    if (!is_array($item)) {
        return;
    }
    $key = implode('|', [
        $item['lane'] ?? '',
        $item['actor'] ?? '',
        $item['action'] ?? '',
        $item['ts'] ?? '',
        $item['pattern'] ?? '',
    ]);
    if (!isset($items[$key]) || (!empty($item['alert']) && empty($items[$key]['alert']))) {
        $items[$key] = $item;
    }
}

function ruleCalmCompactViolations($summary) {
    $violations = [];
    $checks = is_array($summary['checks'] ?? null) ? $summary['checks'] : [];
    foreach ($checks as $name => $check) {
        if (!is_array($check)) {
            continue;
        }
        $raw_findings = array_merge(
            is_array($check['violations'] ?? null) ? $check['violations'] : [],
            is_array($check['hints'] ?? null) ? $check['hints'] : []
        );
        foreach ($raw_findings as $violation) {
            if (!is_array($violation)) {
                continue;
            }
            $severity = strtolower(trim((string)($violation['severity'] ?? '')));
            if (!in_array($severity, ['info', 'warning'], true)) {
                $severity = (($check['ok'] ?? false) === true) ? 'info' : 'warning';
            }
            $alert = $severity !== 'info' && (($check['ok'] ?? false) !== true || $severity === 'warning');
            $events = [];
            foreach (($violation['events'] ?? []) as $event) {
                $normalized = ruleCalmNormalizeEvent(
                    $event,
                    $name,
                    $alert,
                    (string)($violation['type'] ?? 'pattern'),
                    isset($violation['age_s']) ? (float)$violation['age_s'] : null
                );
                if ($normalized) {
                    $events[] = $normalized;
                }
            }
            $times = array_values(array_filter(array_map(function($item) {
                return $item['ts'] ?? null;
            }, $events), function($value) {
                return $value !== null;
            }));
            $first_ts = $times ? min($times) : ruleCalmTs($violation['first_ts'] ?? null);
            $last_ts = $times ? max($times) : ruleCalmTs($violation['last_ts'] ?? null);
            $violations[] = [
                'check' => ruleCalmPublicCheckName($name),
                'lane_group' => ruleCalmLaneGroup($name),
                'type' => ruleCalmPublicPattern($violation['type'] ?? 'pattern'),
                'actor' => ruleCalmPublicActor($violation['actor'] ?? ''),
                'severity' => $severity,
                'alert' => $alert,
                'age_s' => isset($violation['age_s']) ? (float)$violation['age_s'] : null,
                'count' => isset($violation['count']) ? (int)$violation['count'] : null,
                'first_ts' => $first_ts,
                'last_ts' => $last_ts,
                'first_time' => ruleCalmTimeLabel($first_ts),
                'last_time' => ruleCalmTimeLabel($last_ts),
                'events' => array_slice($events, 0, 6),
            ];
        }
    }
    usort($violations, function($a, $b) {
        $priority = ['data_quality' => 0, 'decision_path' => 1, 'execution' => 2];
        $pa = $priority[$a['lane_group'] ?? 'execution'] ?? 3;
        $pb = $priority[$b['lane_group'] ?? 'execution'] ?? 3;
        if ($pa !== $pb) {
            return $pa <=> $pb;
        }
        if (!empty($a['alert']) !== !empty($b['alert'])) {
            return !empty($a['alert']) ? -1 : 1;
        }
        return (float)($a['first_ts'] ?? 0) <=> (float)($b['first_ts'] ?? 0);
    });
    return array_slice($violations, 0, 30);
}

function ruleCalmBuildTimeline($summary, $violations) {
    $items = [];
    $samples = is_array($summary['event_samples'] ?? null) ? $summary['event_samples'] : [];
    foreach ($samples as $lane => $events) {
        if (!is_array($events)) {
            continue;
        }
        foreach ($events as $event) {
            ruleCalmTimelineAdd($items, ruleCalmNormalizeEvent($event, $lane));
        }
    }
    foreach ($violations as $violation) {
        foreach (($violation['events'] ?? []) as $event) {
            ruleCalmTimelineAdd($items, $event);
        }
    }
    $timeline = array_values($items);
    usort($timeline, function($a, $b) {
        $ta = $a['ts'] ?? 0;
        $tb = $b['ts'] ?? 0;
        if ($ta == $tb) {
            return strcmp(($a['lane'] ?? '') . ($a['actor'] ?? ''), ($b['lane'] ?? '') . ($b['actor'] ?? ''));
        }
        return $ta <=> $tb;
    });
    return array_slice($timeline, -160);
}

function ruleCalmTimelineSummary($timeline) {
    $times = array_values(array_filter(array_map(function($item) {
        return $item['ts'] ?? null;
    }, $timeline), function($value) {
        return $value !== null;
    }));
    if (!$times) {
        return ['first_time' => '', 'last_time' => '', 'count' => count($timeline)];
    }
    return [
        'first_time' => ruleCalmTimeLabel(min($times)),
        'last_time' => ruleCalmTimeLabel(max($times)),
        'count' => count($timeline),
    ];
}

function ruleCalmBuildForumSummary($summary, $violations, $context) {
    $status = (string)($summary['status'] ?? 'UNKNOWN');
    $control_status = strtoupper((string)($summary['control_status'] ?? $status));
    $data_quality_status = strtoupper((string)($summary['data_quality_status'] ?? 'NOT_ANALYZED'));
    $completeness = (string)($summary['completeness'] ?? 'LEGACY');
    $records = is_array($summary['records'] ?? null) ? $summary['records'] : [];
    $events = is_array($summary['events'] ?? null) ? $summary['events'] : [];
    $default_gap_s = (int)($context['min_gap_s'] ?? 180);
    $effective_gaps = is_array($summary['effective_min_gap_s'] ?? null) ? $summary['effective_min_gap_s'] : [];
    $services = array_map('ruleCalmServiceLabel', $context['services'] ?? []);
    $window_parts = ['Fenster: ' . $default_gap_s . 's'];
    $owner_gaps = [];
    foreach (['storage_contract_owner', 'storage_owner', 'storage_state'] as $name) {
        $gap = (int)($effective_gaps[$name] ?? 0);
        if ($gap > $default_gap_s) {
            $owner_gaps[] = $gap;
        }
    }
    if ($owner_gaps) {
        $window_parts[] = 'Contract/Owner/State-Fenster: ' . max($owner_gaps) . 's';
    }
    $control_label = $control_status === 'PASS'
        ? 'OK'
        : ($control_status === 'FAIL' ? 'auffällig' : ($control_status === 'EVIDENCE_LIMIT' ? 'EVIDENCE_LIMIT' : $control_status));
    $data_quality_labels = [
        'PASS' => 'OK',
        'HINT' => 'Hinweis',
        'FAIL' => 'auffällig',
        'EVIDENCE_LIMIT' => 'EVIDENCE_LIMIT',
        'NOT_ANALYZED' => 'nicht ausgewertet',
    ];
    $data_quality_label = $data_quality_labels[$data_quality_status] ?? 'nicht ausgewertet';
    $lines = [];
    $lines[] = 'Regelruhe: ' . $control_label . ' · Datenqualität: ' . $data_quality_label
        . ($completeness === 'PARTIAL' ? ' · TEILWEISE / EVIDENCE_LIMIT' : '');
    $scope_context = is_array($context['scope_context'] ?? null) ? $context['scope_context'] : [];
    $scope_label = (string)($context['scope_label'] ?? $scope_context['scope_label'] ?? '');
    $source_line = 'Quelle: ' . (string)($context['source_label'] ?? 'Entscheidungsverlauf');
    if ($scope_label !== '') {
        $source_line .= ' · Zeitraum: ' . $scope_label;
    }
    $lines[] = $source_line;
    $record_summary = is_array($context['record_summary'] ?? null) ? $context['record_summary'] : [];
    if (!empty($record_summary['first_time']) && !empty($record_summary['last_time'])) {
        $lines[] = 'Geprüfter Record-Zeitraum: ' . $record_summary['first_time'] . ' bis ' . $record_summary['last_time'];
    }
    if (($scope_context['scope'] ?? '') === 'latest') {
        $lines[] = 'Einordnung: historische, prozessübergreifende Sicht; Auffälligkeiten sind kein Beleg für den aktuellen Prozess.';
    }
    if (!empty($scope_context['service_start_times']) && is_array($scope_context['service_start_times'])) {
        $starts = [];
        foreach ($scope_context['service_start_times'] as $service => $info) {
            if (!is_array($info) || empty($info['time'])) {
                continue;
            }
            $starts[] = ruleCalmServiceLabel($service) . ' ' . $info['time'];
        }
        if ($starts) {
            $lines[] = 'Manager-Neustarts: ' . implode(', ', $starts);
        }
    }
    $lines[] = 'Dienste: ' . implode(', ', $services) . ' · ' . implode(' · ', $window_parts);
    if ($completeness === 'PARTIAL') {
        $missing = array_map('ruleCalmServiceLabel', $summary['missing_services'] ?? []);
        $analyzed = array_map('ruleCalmServiceLabel', $summary['analyzed_services'] ?? []);
        $lines[] = 'Ausgewertet: ' . implode(', ', $analyzed) . ' · Ohne Records: ' . implode(', ', $missing);
    }
    $lines[] = 'Records: Speicher ' . (int)($records['storage'] ?? 0)
        . ', Wallbox ' . (int)($records['wallbox'] ?? 0)
        . ', Wärmepumpe ' . (int)($records['heatpump'] ?? 0)
        . ', EMS ' . (int)($records['ems'] ?? 0);
    $lines[] = 'Zustandswechsel: Storage ' . (int)($events['storage'] ?? 0)
        . ', Storage-Contract ' . (int)($events['storage_contract_owner'] ?? 0)
        . ', Storage-Ausführung ' . (int)($events['storage_execution_class'] ?? 0)
        . ', Storage-Owner ' . (int)($events['storage_owner'] ?? 0)
        . ', Storage-State ' . (int)($events['storage_state'] ?? 0)
        . ', Storage-Werte ' . (int)($events['storage_value_update'] ?? 0)
        . ', Storage-Messwerte ' . (int)($events['storage_live_plausibility'] ?? 0)
        . ', WB Start/Stop ' . (int)($events['wallbox_start_stop'] ?? 0)
        . ', WB Phasen ' . (int)($events['wallbox_phase'] ?? 0)
        . ', WP ' . (int)($events['heatpump'] ?? 0)
        . ', EMS ' . (int)($events['ems_decision'] ?? 0);
    $data_quality_findings = array_values(array_filter($violations, function($violation) {
        return ($violation['lane_group'] ?? '') === 'data_quality';
    }));
    $control_findings = array_values(array_filter($violations, function($violation) {
        return ($violation['lane_group'] ?? '') !== 'data_quality' && !empty($violation['alert']);
    }));
    $format_findings = function($findings, $limit) use ($effective_gaps, $default_gap_s) {
        $result = [];
        foreach (array_slice($findings, 0, $limit) as $violation) {
            $sample = '';
            $event_texts = [];
            foreach (array_slice($violation['events'] ?? [], 0, 3) as $event) {
                $event_texts[] = trim((!empty($event['time']) ? $event['time'] . ' ' : '') . ruleCalmForumActionLabel($event['action'] ?? ''));
            }
            if ($event_texts) {
                $sample = ' (' . implode(' -> ', $event_texts) . ')';
            }
            $check = (string)($violation['check'] ?? '');
            $check_gap_s = (int)($effective_gaps[$check] ?? $default_gap_s);
            $age_text = '';
            if ($violation['age_s'] !== null) {
                $age_text = ' innerhalb ' . round((float)$violation['age_s']) . 's';
                if ($check_gap_s > 0 && $check_gap_s !== $default_gap_s) {
                    $age_text .= ' (Fenster ' . $check_gap_s . 's)';
                }
            }
            $result[] = '- ' . ruleCalmServiceLabel($violation['check'])
                . ': ' . ruleCalmForumPatternLabel($violation['type'])
                . ($violation['actor'] !== '' ? ' bei ' . ruleCalmForumActorLabel($violation['actor']) : '')
                . $age_text
                . $sample;
        }
        return $result;
    };
    if ($data_quality_findings) {
        $lines[] = $data_quality_status === 'HINT' ? 'Datenqualitätshinweis:' : 'Datenqualitätsbefunde:';
        $lines = array_merge($lines, $format_findings($data_quality_findings, 4));
    } else {
        $lines[] = 'Datenqualität: kein Messwertbefund im ausgewerteten Speicherverlauf.';
    }
    if ($control_findings) {
        $lines[] = 'Regelauffälligkeiten:';
        $lines = array_merge($lines, $format_findings($control_findings, 6));
    } else {
        $lines[] = 'Regelauffälligkeiten: kein belegtes Execution-/Ausgangs-Ping-Pong im gewählten Fenster.';
    }
    return implode("\n", $lines);
}

function ruleCalmWriteHistory($payload) {
    $ramdisk = dirname(RULE_CALM_HISTORY_FILE);
    if (!(is_dir($ramdisk) || @mkdir($ramdisk, 0700, true))) {
        return;
    }
    $encoded = json_encode($payload, ruleCalmJsonFlags(true));
    if (!is_string($encoded)) {
        return;
    }
    try {
        $suffix = bin2hex(random_bytes(8));
    } catch (Throwable $e) {
        return;
    }
    $tmp = RULE_CALM_HISTORY_FILE . '.' . $suffix . '.tmp';
    $handle = @fopen($tmp, 'x');
    if ($handle === false) {
        return;
    }
    @chmod($tmp, 0600);
    $written = @fwrite($handle, $encoded . "\n");
    @fflush($handle);
    @fclose($handle);
    if ($written === false || !@rename($tmp, RULE_CALM_HISTORY_FILE)) {
        @unlink($tmp);
        return;
    }
    @chmod(RULE_CALM_HISTORY_FILE, 0600);
}

function ruleCalmRun($input_path, $source_label, $input_kind, $services = null, $source_files = [], $record_windows = [], $record_summary = [], $scope_context = []) {
    $install_path = ruleCalmResolveInstallPath();
    if ($install_path === '') {
        ruleCalmError('decision_history_analysis.py wurde im Installationspfad nicht gefunden');
    }

    $python = is_file('/opt/venv/bin/python3') ? '/opt/venv/bin/python3' : '/usr/bin/python3';
    $analysis_runner = $install_path . '/Tools/decision_history_analysis.py';
    $min_gap_s = ruleCalmSafeInt($_GET['min_gap_s'] ?? $_POST['min_gap_s'] ?? 180, 180, 30, 1800);
    $limit = ruleCalmRequestedLimit();
    $services = is_array($services) ? ruleCalmNormalizeServices($services) : ruleCalmRequestedServices();

    $cmd = [
        $python,
        $analysis_runner,
        $input_path,
        '--json',
        '--min-gap-s',
        (string)$min_gap_s,
        '--limit-per-service',
        (string)$limit,
    ];
    foreach ($services as $service) {
        $cmd[] = '--service';
        $cmd[] = $service;
    }
    $process = e3dcRunArgvProcess(
        $cmd,
        20.0,
        ['cwd' => $install_path, 'max_output_bytes' => 1048576]
    );
    $exit_code = (int)$process['exit_code'];
    if ($process['timed_out'] || (int)$process['signal'] > 0) {
        ruleCalmError('Regelruhe-Analyse wurde nicht vollständig ausgeführt', [
            'exit_code' => $exit_code,
            'timed_out' => (bool)$process['timed_out'],
            'signal' => (int)$process['signal'],
        ]);
    }
    $raw = (string)$process['stdout'];
    $summary = json_decode($raw, true);
    if (!is_array($summary)) {
        ruleCalmError('Regelruhe-Analyse lieferte kein gültiges JSON', [
            'exit_code' => $exit_code,
            'output_available' => $raw !== '' || (string)$process['stderr'] !== '',
        ]);
    }
    if (($summary['schema'] ?? '') !== RULE_CALM_CLI_SCHEMA) {
        ruleCalmError('Regelruhe-Analyse lieferte ein unbekanntes Datenformat', [
            'exit_code' => $exit_code,
        ]);
    }
    $status = strtoupper((string)($summary['status'] ?? 'UNKNOWN'));
    $control_status = strtoupper((string)($summary['control_status'] ?? 'UNKNOWN'));
    $data_quality_status = strtoupper((string)($summary['data_quality_status'] ?? 'UNKNOWN'));
    $valid_exit = ($status === 'PASS' && $exit_code === 0) || ($status === 'FAIL' && $exit_code === 1);
    $expected_status = in_array('FAIL', [$control_status, $data_quality_status], true)
        || in_array('EVIDENCE_LIMIT', [$control_status, $data_quality_status], true)
        ? 'FAIL'
        : 'PASS';
    if (!$valid_exit
        || !in_array($control_status, ['PASS', 'FAIL', 'EVIDENCE_LIMIT'], true)
        || !in_array($data_quality_status, ['PASS', 'HINT', 'FAIL', 'EVIDENCE_LIMIT', 'NOT_ANALYZED'], true)
        || $status !== $expected_status) {
        ruleCalmError('Regelruhe-Analyse wurde nicht erfolgreich und vollständig ausgewertet', [
            'exit_code' => $exit_code,
            'status' => in_array($status, ['PASS', 'FAIL', 'ERROR'], true) ? $status : 'UNKNOWN',
        ]);
    }
    $analyzed_services = ruleCalmValidatedServiceList($summary['analyzed_services'] ?? null);
    $missing_services = ruleCalmValidatedServiceList($summary['missing_services'] ?? null);
    $reported_services = ruleCalmValidatedServiceList($summary['services'] ?? null);
    $completeness = strtoupper((string)($summary['completeness'] ?? ''));
    $partition = array_values(array_unique(array_merge($analyzed_services ?? [], $missing_services ?? [])));
    sort($partition);
    $expected_services = $services;
    sort($expected_services);
    $reported_sorted = $reported_services ?? [];
    sort($reported_sorted);
    if (!is_array($summary['records'] ?? null)
        || !is_array($summary['events'] ?? null)
        || !is_array($summary['checks'] ?? null)
        || $analyzed_services === null || $missing_services === null || $reported_services === null
        || !$analyzed_services || $partition !== $expected_services || $reported_sorted !== $expected_services
        || !in_array($completeness, ['COMPLETE', 'PARTIAL'], true)
        || ($completeness === 'COMPLETE' && $missing_services)
        || ($completeness === 'PARTIAL' && !$missing_services)) {
        ruleCalmError('Regelruhe-Analyse enthielt einen ungültigen Vollständigkeitsvertrag', [
            'exit_code' => $exit_code,
        ]);
    }
    foreach ($analyzed_services as $service) {
        if (!array_key_exists($service, $summary['records']) || (int)$summary['records'][$service] <= 0) {
            ruleCalmError('Regelruhe-Analyse enthielt keine auswertbaren Entscheidungsdaten', ['exit_code' => $exit_code]);
        }
    }

    $findings = ruleCalmCompactViolations($summary);
    $violations = array_values(array_filter($findings, function($finding) {
        return !empty($finding['alert']) && ($finding['lane_group'] ?? '') !== 'data_quality';
    }));
    $data_quality_findings = array_values(array_filter($findings, function($finding) {
        return ($finding['lane_group'] ?? '') === 'data_quality';
    }));
    $timeline = ruleCalmBuildTimeline($summary, $findings);
    $public_scope_context = ruleCalmPublicScopeContext($scope_context);
    $context = [
        'source_label' => $source_label,
        'services' => $services,
        'analyzed_services' => $analyzed_services,
        'missing_services' => $missing_services,
        'completeness' => $completeness,
        'min_gap_s' => $min_gap_s,
        'effective_min_gap_s' => ruleCalmPublicNumericMap($summary['effective_min_gap_s'] ?? []),
        'scope_label' => $public_scope_context['scope_label'] ?? '',
        'scope_context' => $public_scope_context,
        'record_summary' => $record_summary,
    ];
    $payload = [
        'success' => true,
        'schema' => RULE_CALM_PUBLIC_SCHEMA,
        'generated_at' => date('c'),
        'input_kind' => $input_kind,
        'source_label' => $source_label,
        'source_file_count' => count($source_files),
        'record_windows' => ruleCalmPublicRecordWindows($record_windows),
        'record_summary' => $record_summary,
        'scope' => $public_scope_context['scope'],
        'scope_label' => $public_scope_context['scope_label'],
        'scope_context' => $public_scope_context,
        'privacy_note' => RULE_CALM_PRIVACY_NOTE,
        'min_gap_s' => $min_gap_s,
        'limit_per_service' => $limit,
        'services' => $services,
        'analyzed_services' => $analyzed_services,
        'missing_services' => $missing_services,
        'completeness' => $completeness,
        'status' => $status,
        'control_status' => $control_status,
        'data_quality_status' => $data_quality_status,
        'records' => ruleCalmPublicNumericMap($summary['records'] ?? []),
        'events' => ruleCalmPublicNumericMap($summary['events'] ?? []),
        'checks' => ruleCalmCompactChecks($summary),
        'effective_min_gap_s' => ruleCalmPublicNumericMap($summary['effective_min_gap_s'] ?? []),
        'evidence_limits' => ruleCalmPublicEvidenceLimits($summary['evidence_limits'] ?? []),
        'violations' => $violations,
        'data_quality_findings' => $data_quality_findings,
        'findings' => $findings,
        'timeline' => $timeline,
        'timeline_summary' => ruleCalmTimelineSummary($timeline),
        'forum_summary' => ruleCalmBuildForumSummary($summary, $findings, $context),
        'exit_code' => $exit_code,
    ];

    ruleCalmWriteHistory($payload);
    ruleCalmJson($payload);
}

$action = strtolower(trim((string)($_GET['action'] ?? $_POST['action'] ?? 'analyze')));
if ($action === '' || $action === 'current') {
    $action = 'analyze';
}
if ($action === 'manifest') {
    ruleCalmManifest();
}
if ($action === 'history') {
    ruleCalmHistory();
}
if ($action === 'analyze_upload') {
    [$upload_path, $source_label, $input_kind] = ruleCalmUploadPath();
    ruleCalmRun($upload_path, $source_label, $input_kind, ruleCalmRequestedServices());
}
if ($action !== 'analyze') {
    ruleCalmError('Unbekannte Diagnose-Aktion');
}

$services = ruleCalmRequestedServices();
$limit = ruleCalmRequestedLimit();
$scope = ruleCalmRequestedScope();
$scope_context = ruleCalmCurrentScopeContext($scope, $services);
if ($scope === 'manager_restart' && ($scope_context['cutoff_ts'] ?? null) === null) {
    ruleCalmError('Die aktuelle Prozessgrenze konnte nicht ermittelt werden; historische Records werden nicht ersatzweise als aktuell bewertet.');
}
[$input_path, $source_label, $input_kind, $source_files, $record_windows, $record_summary, $scope_context] = ruleCalmPrepareCurrentInput($services, $limit, $scope_context);
ruleCalmRun($input_path, $source_label, $input_kind, $services, $source_files, $record_windows, $record_summary, $scope_context);
